#!/usr/bin/env python
# coding: utf-8
"""H2: Curated Compiler —— **AI 是题库编辑, 不是出题人**。

## 这个模块存在的理由

现状: 每道题都由 AI 现场造。代价不只是慢 —— 是**风格**。实播反复出现

    特殊岗位规则 / 设备冷门功能 / 某制度规定 / 某系统真实用途

这类题**逻辑上成立, 但没有认知反转**: 观众听完谜底的反应是"哦", 不是
"原来如此"。而网上现成的好题(SE 的 situation / TurtleBench)里, 身份 /
时间 / 空间 / 视角 / 物品意义 这类真反转的密度高得多。

所以 Batch H 的路线是**换题源**, 而不是继续调 prompt 让 AI 写得更像。

## 铁律: 不许重新创作故事

    surface(谜面) 与 bottom(谜底) 是 **canonical source**。

AI **可以**:
    翻译(英->中) / 精简表述 / 结构化(core_answer/facts/beats/...)

AI **不可以**:
    改写核心真相 / 添加新的关键因果 / 为通过 schema 编造不存在的线索

如果原题不适合直播 -> **reject**, 而不是"修成另一题"。这是本模块与
`PuzzleWriter.gen_spec` 最根本的区别: gen_spec 的任务是**发明一道好题**,
本模块的任务是**把一道已有的好题搬进我们的 schema**。

## 为什么不复用 gen_spec

`gen_spec` 的第一步是调 `emit_riddle` 让模型**写一个新谜题**。对 curated
题那一步是**有害**的 —— 它会顺手把故事改了。所以:

    gen_spec:      emit_riddle(发明) -> validate -> review -> audit -> gate
    curated:       translate/compile(搬运) -> validate -> review -> audit -> gate

后四道门**完全一样**(复用现有实现, 不自建一套)。差的只是第一步。

## 复用哪些(不自建)

    validate_spec              结构/通关合同硬门
    PuzzleWriter._review_spec_with_retry  Reviewer(带技术重试)
    PuzzleWriter._audit_with_retry        truth audit
    cross_puzzle_gate          跨题分布
    quality.too_similar        文本去重

**不新增第四个内容审核 LLM**(任务书明确)。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from story.puzzle import (
    DiscoveryBeat, FairClue, PuzzleBlueprint, PuzzleFact, PuzzleSignature,
    PuzzleSpec, SolveAtom, quote_in_puzzle,
)
from story.quality import (
    QUALITY_POLICY_VERSION, Quotas, cross_puzzle_gate, validate_reveal_adherence,
    validate_spec,
)

log = logging.getLogger("hgt.compiler")

#: curated 编译的 prompt 版本。与 riddle/check 分开 —— 它是**另一条链**,
#: 混用同一个版本号会让"这批题是哪条链产的"无法区分。
CURATED_PROMPT_VERSION = "curated-v1"


# ======================================================================
# 提示词
# ======================================================================
CURATED_COMPILE_SYSTEM = """你是**题库编辑**, 不是出题人。全程用中文。

你会拿到一道**已经存在**的谜题: 它的谜面(surface)与谜底(bottom)。
你的任务是把它们**搬进**我们的结构化 schema, 而**不是**重新创作。

═══ 铁律: surface 与 bottom 是 canonical source ═══

**允许**:
  - 把英文谜面/谜底**翻译**成中文(逐句对应, 不合并不省略)
  - **精简表述**: 删掉与谜底无关的装饰性铺陈, 但不得删掉任何**推理
    需要**的细节
  - 把谜底拆成结构化字段(core_answer / facts / discovery_beats / ...)

**禁止**(任何一条都会让这道题被代码判不合格):
  - **改写核心真相** —— 谜底讲的机制必须与原文**完全一致**
  - **添加新的关键因果** —— 原文没有的人物、动机、机关一律不许加
  - **为通过 schema 编造不存在的线索** —— fair_clue 必须逐字来自谜面
  - 因为"这样更戏剧化"而调整情节

如果你判断**这道题本身不适合**我们的直播, 请把 `accepted` 设为 false
并给出 `reject_reasons`, **不要**试图把它改成另一道题。

═══ 判据(逐条回答) ═══

1. `clear_anomaly`  谜面是否形成一个**清楚的反常点**?(观众听完会想
   "这不对劲")
2. `unique_explanation` 谜底是否能**唯一**解释谜面?(不是"有可能",
   而是"就是它")
3. `yes_no_progress` 能否通过是/否问答**逐步逼近**谜底?
4. `no_obscure_system` 是否**不依赖**冷门职业制度、设备冷门功能、
   某系统真实用途这类"查了才知道"的知识?
5. `not_pure_puzzle` 是否**不是**数学题 / 字谜 / 图形题 / 纯知识问答?
6. `has_reversal` 是否有**认知反转** —— 身份 / 关系 / 时间 / 空间 /
   视角 / 物品意义 / 因果 中至少一种, 揭晓后要让人"重新理解一遍"?
7. `detail_recontextualized` 揭晓后, 谜面里至少有一个细节的**意义
   发生了变化**?
8. `no_external_media` 是否**不需要**看图片 / 表格 / 附件就能玩?
9. `livestream_safe` 内容是否适合公开直播展示?

**九条必须全部为 true 才能 accepted=true。** 有一条不满足就 reject ——
"逻辑成立但没意思"的题不值得占一个直播位。

═══ 翻译(C 部分) ═══

若原题是英文:
  - **事实一一对应**: 数字 / 关系 / 时间 / 地点一律不改
  - **不是文学改写**: 不要润色成散文, 不要加形容词
  - 若这题**依赖英文词形或双关**无法翻译 -> `accepted=false`,
    `reject_reasons=["language_dependent"]`, **不要硬翻**

═══ 结构化(D 部分) ═══

- `core_answer`: 一句话(≤60 汉字, **不换行**)直接回答谜面最后那个问句。
  普通人一听就懂。
- `answer`: 完整谜底(2-4 句)。**第一句必须正面解释核心反常**。
- `completion_fact_ids`: **通关合同**, 1~2 条。它回答"房间最少要公开
  确认哪几件事, 这题就算解出来了"。**不是**"谜底要点"。
  只能指向 kind=core 且 visibility=hidden 的 fact。
  **题目允许有层次, 通关必须简单。**
- `facts`: 4~10 条。一条 = 一个可独立被问到的命题。至少 1 条 exclusion。
- `discovery_beats`: 2~4 个**发现阶段**(正常玩下来会一层层想通什么)。
  **它没有胜负权**, 只是叙事层次。至少一条指向 completion。
- `solve_atoms`: 1~4 条, 用 fact_ids 指向 facts。身份/时间/物品/因果
  反转用 `key`, 只有真有因果链才用 cause/mechanism。
- `fair_clues`: 从**谜面原文**里逐字摘取, 并注明支持哪条 atom。
  谜面里没有可回溯的线索 -> **reject**(不要从谜底倒灌一条 clue)。
- `hints`: 3 条, ≤30 字, 由浅入深, 不剧透。
- `signature`: 如实回传这道题的机制/解法形状/领域/关系/情绪等。

═══ 必须回传 observed_signature 与 quality_checks ═══

`observed_signature` 要**如实**反映这道题, 不是照抄某个目标。
`quality_checks` 里九项判据逐条填 true/false。
"""

_TOOL_CURATED = {
    "name": "emit_curated",
    "description": "把一道**已有**谜题搬进结构化 schema(不是新出一道题)",
    "input_schema": {
        "type": "object",
        "properties": {
            "accepted": {
                "type": "boolean",
                "description": "九条判据全为 true 才填 true; 否则 false",
            },
            "reject_reasons": {
                "type": "array", "items": {"type": "string"},
                "description": ("accepted=false 时必填。可用: "
                                "language_dependent / not_a_story / "
                                "no_reversal / depends_on_obscure_system / "
                                "needs_external_media / unsafe / "
                                "no_unique_explanation / ambiguous"),
            },
            "quality_checks": {
                "type": "object",
                "properties": {
                    "clear_anomaly": {"type": "boolean"},
                    "unique_explanation": {"type": "boolean"},
                    "yes_no_progress": {"type": "boolean"},
                    "no_obscure_system": {"type": "boolean"},
                    "not_pure_puzzle": {"type": "boolean"},
                    "has_reversal": {"type": "boolean"},
                    "detail_recontextualized": {"type": "boolean"},
                    "no_external_media": {"type": "boolean"},
                    "livestream_safe": {"type": "boolean"},
                },
                "required": ["clear_anomaly", "unique_explanation",
                             "yes_no_progress", "no_obscure_system",
                             "not_pure_puzzle", "has_reversal",
                             "detail_recontextualized", "no_external_media",
                             "livestream_safe"],
            },
            "style_tags": {
                "type": "array", "items": {"type": "string"},
                "description": ("这道题的认知反转类型, 如 identity_flip / "
                                "perspective_flip / time_flip / "
                                "space_flip / object_meaning / causal_flip"),
            },
            "title": {"type": "string",
                      "description": "短标题(中文)"},
            "puzzle": {
                "type": "string",
                "description": ("谜面(2-3 句, 第三人称客观事实, 结尾是问句)。"
                                "翻译题在这里给**中文**谜面。"
                                "**不得**改变原题的事实。"),
            },
            "answer": {
                "type": "string",
                "description": "完整谜底(2-4 句, 第一句正面解释核心反常)",
            },
            "core_answer": {
                "type": "string",
                "description": ("一句话核心答案(≤60 汉字, 不换行, 直接回答"
                                "谜面最后那个问句)"),
            },
            "completion_fact_ids": {
                "type": "array", "minItems": 1, "maxItems": 2,
                "items": {"type": "string"},
                "description": ("通关合同: 1~2 条 kind=core 且 "
                                "visibility=hidden 的 fact id。"
                                "support/exclusion 绝不能填这里。"),
            },
            "facts": {
                "type": "array", "minItems": 4, "maxItems": 10,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "text": {"type": "string"},
                        "kind": {"type": "string",
                                 "enum": ["core", "support", "exclusion"]},
                        "visibility": {"type": "string",
                                       "enum": ["public", "hidden"]},
                        "hintable": {"type": "boolean"},
                    },
                    "required": ["id", "text", "kind", "visibility"],
                },
            },
            "discovery_beats": {
                "type": "array", "minItems": 2, "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "text": {"type": "string"},
                        "fact_ids": {"type": "array",
                                     "items": {"type": "string"}},
                    },
                    "required": ["id", "text"],
                },
                "description": ("2~4 个发现阶段(叙事层次, **不是**通关条件)。"
                                "至少一条指向 completion 里的 fact。"),
            },
            "solve_atoms": {
                "type": "array", "minItems": 1, "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "role": {"type": "string",
                                 "enum": ["cause", "mechanism", "key",
                                          "support"]},
                        "text": {"type": "string"},
                        "fact_ids": {"type": "array",
                                     "items": {"type": "string"}},
                        "required": {"type": "boolean"},
                    },
                    "required": ["id", "role", "text"],
                },
            },
            "fair_clues": {
                "type": "array", "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "quote": {"type": "string",
                                  "description": ("**逐字**出自谜面原文的一"
                                                  "段。不得改写, 不得从谜底"
                                                  "倒灌。")},
                        "supports_atoms": {"type": "array",
                                           "items": {"type": "string"}},
                    },
                    "required": ["quote"],
                },
            },
            "hints": {
                "type": "array", "minItems": 3, "maxItems": 3,
                "items": {"type": "string"},
            },
            "observed_signature": {
                "type": "object",
                "properties": {
                    "mechanism_family": {"type": "string"},
                    "solution_shape": {"type": "string"},
                    "domain": {"type": "string"},
                    "relation": {"type": "string"},
                    "emotion_mode": {"type": "string"},
                    "time_shape": {"type": "string"},
                    "death": {"type": "boolean"},
                    "past_trauma": {"type": "boolean"},
                    "long_term_profession": {"type": "boolean"},
                    "repeated_ritual": {"type": "boolean"},
                },
                "required": ["mechanism_family", "solution_shape"],
            },
        },
        "required": ["accepted", "quality_checks"],
    },
}


#: 九条判据 —— 与 prompt 里的编号一一对应。**必须全为 true**。
#: 口径与 `_apply_review` 的 quality_checks 一致: 缺一项也算不合格
#: (fail closed), 否则模型漏填几项就能把一道烂题放过。
CURATED_CHECKS = (
    "clear_anomaly", "unique_explanation", "yes_no_progress",
    "no_obscure_system", "not_pure_puzzle", "has_reversal",
    "detail_recontextualized", "no_external_media", "livestream_safe",
)


def build_user_prompt(rec, *, target_blueprint: Optional[PuzzleBlueprint] = None
                      ) -> str:
    """把一条 RawCuratedPuzzle 拼成编译请求。

    ⚠️ 消息里**显式声明**"这是已有题目, 不要重新创作" —— 光靠 system
    prompt 不够: 模型很容易滑回"编一道类似的题"。把原文放在显眼位置
    并明确标注来源, 能显著降低改写率。
    """
    parts = [
        "下面是一道**已经存在**的谜题。请把它**搬进**我们的 schema。",
        "",
        f"【来源】{rec.source}",
    ]
    if rec.source_url:
        parts.append(f"【原帖】{rec.source_url}")
    if rec.title:
        parts.append(f"【原标题】{rec.title}")
    parts += [
        "",
        "═══ 谜面(surface, canonical —— 不得改变其事实)═══",
        rec.surface,
        "",
        "═══ 谜底(bottom, canonical —— 不得改变其真相)═══",
        rec.bottom,
        "",
    ]
    if rec.original_language and rec.original_language != "zh":
        parts += [
            f"⚠️ 原题是 **{rec.original_language}**。请把它翻译成中文。",
            "翻译要求: 事实一一对应(数字/关系/时间/地点不改), "
            "不是文学改写。若依赖词形或双关无法翻译 -> reject 并填 "
            "language_dependent。",
            "",
        ]
    if target_blueprint is not None:
        parts += [
            "【本题的 Blueprint(代码层已决定, 只能照它设计)】",
            target_blueprint.describe(),
            "",
        ]
    parts.append("请按 schema 输出。记住: 你是**编辑**, 不是出题人。")
    return "\n".join(parts)


# ======================================================================
# 结果 → PuzzleSpec
# ======================================================================
def _unwrap(ti: Any) -> dict:
    """兼容被包了一层的 tool_input(`{"input": {...}}` 之类)。"""
    if not isinstance(ti, dict):
        return {}
    for k in ("input", "arguments", "parameters", "result"):
        inner = ti.get(k)
        if isinstance(inner, dict) and ("accepted" in inner
                                        or "quality_checks" in inner):
            return inner
    return ti


def spec_from_tool(d: dict, rec, bp: Optional[PuzzleBlueprint]
                   ) -> PuzzleSpec:
    """把模型回传的 dict 转成 PuzzleSpec(**不做校验**)。

    与 `_spec_from_tool` 的关键差别: 这里**注入 provenance**。curated 题
    必须永远带着"它从哪来", 否则版权与排查都无从谈起(H2-F/H2-H)。
    """
    sig_raw = d.get("observed_signature") or {}
    sig = PuzzleSignature.from_dict(sig_raw)
    spec = PuzzleSpec(
        title=str(d.get("title") or rec.title or "").strip(),
        puzzle=str(d.get("puzzle") or "").strip(),
        answer=str(d.get("answer") or "").strip(),
        core_answer=str(d.get("core_answer") or "").strip(),
        completion_fact_ids=[str(x).strip()
                             for x in (d.get("completion_fact_ids") or [])
                             if str(x).strip()],
        facts=[PuzzleFact.from_dict(x) for x in (d.get("facts") or [])],
        solve_atoms=[SolveAtom.from_dict(x, i)
                     for i, x in enumerate(d.get("solve_atoms") or [])],
        discovery_beats=[DiscoveryBeat.from_dict(x)
                         for x in (d.get("discovery_beats") or [])],
        fair_clues=[FairClue.from_dict(x) for x in (d.get("fair_clues") or [])],
        hints=[str(h).strip() for h in (d.get("hints") or [])
               if str(h).strip()],
        blueprint=bp or PuzzleBlueprint(),
        signature=sig,
        prompt_version=CURATED_PROMPT_VERSION,
        quality_policy_version=QUALITY_POLICY_VERSION,
    )
    # ---- provenance(H2-F): 只进 archive / attribution, **不进前端** ----
    spec.source_type = "curated"                              # type: ignore[attr-defined]
    spec.external_source = rec.source                         # type: ignore[attr-defined]
    spec.external_id = rec.external_id                        # type: ignore[attr-defined]
    spec.source_url = rec.source_url                          # type: ignore[attr-defined]
    spec.license = rec.question_license                       # type: ignore[attr-defined]
    spec.answer_license = rec.answer_license                   # type: ignore[attr-defined]
    spec.attribution = {                                      # type: ignore[attr-defined]
        "question_author": rec.question_author,
        "question_author_url": rec.question_author_url,
        "answer_author": rec.answer_author,
        "answer_author_url": rec.answer_author_url,
        "question_license": rec.question_license,
        "answer_license": rec.answer_license,
        "source_url": rec.source_url,
        "modified": True,
        "modification": ("Chinese translation + structural compilation"
                         if rec.original_language != "zh"
                         else "structural compilation"),
    }
    spec.style_tags = list(d.get("style_tags") or [])         # type: ignore[attr-defined]
    return spec


def check_tool_result(d: dict) -> tuple:
    """判"这道题能不能收"。返回 `(ok, reasons)`。

    ## 为什么先判 `accepted` 再判其它

    模型可能 `accepted=true` 但 `quality_checks` 里有 false(自相矛盾)。
    这时**以 quality_checks 为准** —— 它是逐条判据, 比一个总结布尔更
    可信; 而且 fail closed 方向才安全(宁可拒一道好题, 不能放一道烂题)。

    同理 `accepted=false` 但没有 reasons 也算拒(理由缺了不影响结论)。
    """
    reasons: list = []
    if d.get("accepted") is not True:
        reasons.append("accepted=false")
        reasons.extend(str(x) for x in (d.get("reject_reasons") or []) if x)
        return False, reasons
    qc = d.get("quality_checks")
    if not isinstance(qc, dict):
        reasons.append("quality_checks 缺失")
        return False, reasons
    bad = [k for k in CURATED_CHECKS if qc.get(k) is not True]
    if bad:
        # 逐条报出来 —— "九条里哪几条没过"比"没过"有用得多。
        reasons.append("quality_checks 未全过: " + ", ".join(bad))
        return False, reasons
    return True, []


def validate_curated(spec: PuzzleSpec, bp: Optional[PuzzleBlueprint] = None
                     ) -> tuple:
    """curated 题的额外确定性校验。返回 `(ok, reasons)`。

    在现有 `validate_spec` 之外, curated 还要多守两条:

      1. **fair_clue 必须逐字来自谜面**(H2-E)。`validate_spec` 已经查
         了 quote 在不在谜面里, 但它是 `can_fix`(交给审稿人改)。对
         curated 题这**不够**: 审稿人"改"的方式很可能是**改谜面去迁就
         quote**, 而谜面是 canonical source, 不许动。所以这里升级为
         硬拒 —— 宁可丢掉一道题, 也不能让编辑改动了原作者的事实。

      2. **source_type 必须是 curated**。防的是"将来某个调用方把
         curated 编译器拿去跑自由生成", 那时 provenance 会撒谎。
    """
    reasons: list = []
    if not spec.puzzle or not spec.answer:
        reasons.append("谜面或谜底为空")
        return False, reasons
    if getattr(spec, "source_type", "") != "curated":
        reasons.append("source_type 不是 curated(provenance 缺失)")
    clues = spec.fair_clues or []
    if not clues:
        # H2-E: 谜面里没有可回溯线索 -> reject, **不要**从谜底倒灌。
        reasons.append("没有 fair_clue(谜面无可回溯线索, 应 reject)")
    for c in clues:
        if not c.quote:
            reasons.append("fair_clue 缺 quote")
        elif spec.puzzle and not quote_in_puzzle(c.quote, spec.puzzle):
            reasons.append(
                f"fair_clue 的 quote 不在谜面里({c.quote[:30]!r}) —— "
                f"curated 题**不得改谜面**去迁就 quote")
    return (not reasons), reasons


# ======================================================================
# 编译器
# ======================================================================
class CuratedCompiler:
    """把外部题库记录编译成 `PuzzleSpec`。

    它**持有**一个 `PuzzleWriter` 来复用审稿/审计/客户端 —— 那几段与
    出题链共用, 不另写一份(否则两条链的质量口径迟早漂移)。
    """

    def __init__(self, writer):
        self.writer = writer

    # ------------------------------------------------------------------
    def compile_one(self, rec, *, recent: Optional[list] = None,
                    blueprint: Optional[PuzzleBlueprint] = None,
                    max_attempts: int = 2
                    ) -> tuple:
        """编译一道。返回 `(spec, info)`。

        `spec` 为 None 表示没收(AI 拒绝 / 硬校验不过 / 跨题门拒)。
        `info` 里带 `accepted` / `reject_reasons` / `stage` —— **不要**
        把没收的原因只打进日志: 验收报告要按原因分类统计, 而日志是要
        靠人肉数的。
        """
        info: dict = {"external_id": getattr(rec, "external_id", ""),
                      "accepted": False, "reject_reasons": [], "stage": ""}
        client = self.writer.client
        user = build_user_prompt(rec, target_blueprint=blueprint)

        last_err = ""
        #: 本次编译**走到过的最深一道门**。
        #:
        #: 为什么要记"最深"而不是"最后一次": 重试会让后面的稿子覆盖
        #: 前面的 stage。实测: 第 1 稿一路走到跨题门被拒(那是最有价值
        #: 的信息 —— 题本身没问题, 是分布重复), 第 2 稿却因为网关抖动
        #: 在 compile_call 就挂了, 于是报告里 stage 变成 "compile_call",
        #: 把一个"分布问题"记成了"网关问题"。运维照着这个数字去查网络,
        #: 方向完全错了。
        #:
        #: 深度按管道顺序定: 越靠后越有价值(说明前面的门都过了)。
        deepest = ""
        for attempt in range(1, max(1, max_attempts) + 1):
            res = client.messages(CURATED_COMPILE_SYSTEM, user,
                                  max_tokens=4000, tool=_TOOL_CURATED,
                                  temperature=self.writer._temperature(
                                      "generate_temperature"))
            if not res.tool_input:
                last_err = res.error or "空 tool_input"
                _bump(info, "compile_call")
                log.warning("编译第 %d 稿没有 tool_input: %s",
                            attempt, last_err[:100])
                continue

            d = _unwrap(res.tool_input)
            # ---- ① AI 审题门 ----
            ok, reasons = check_tool_result(d)
            if not ok:
                info["accept_reasons"] = reasons
                # 模型明确说"不行" -> **不再重试**。重试只会让它换个说法
                # 硬凑一个 accepted=true, 而那正是我们最不想看到的。
                if d.get("accepted") is not True:
                    info["reject_reasons"] = reasons
                    info["stage"] = "ai_gate"
                    log.info("AI 拒收 %s: %s",
                             info["external_id"], "; ".join(reasons)[:120])
                    return None, info
                # 自相矛盾(accepted=true 但 qc 有 false) -> 重试一稿,
                # 因为那多半是漏填而不是真判 false。
                log.warning("第 %d 稿 quality_checks 未全过: %s",
                            attempt, "; ".join(reasons)[:120])
                _bump(info, "ai_gate")
                continue

            spec = spec_from_tool(d, rec, blueprint)
            spec.usage, spec.model = res.usage, res.model

            # ---- ② 结构硬门(与出题链同一套) ----
            vr = validate_spec(spec)
            if not vr.ok:
                last_err = vr.why()
                _bump(info, "validate")
                log.info("第 %d 稿结构不过: %s", attempt, last_err[:120])
                continue
            # ---- ③ curated 额外门(fair_clue 逐字 / provenance) ----
            cok, creasons = validate_curated(spec, blueprint)
            if not cok:
                last_err = "; ".join(creasons)
                _bump(info, "curated_validate")
                info["reject_reasons"] = creasons
                log.info("第 %d 稿 curated 校验不过: %s",
                         attempt, last_err[:120])
                # fair_clue 不在谜面里是**结构性的**: 再审一次也不太可能
                # 变好, 而且放任它会让审稿人改谜面 -> 那正是禁止的。
                break
            # ---- ④ Reviewer(**复用现有审稿人**) ----
            reviewed, why, need_rewrite, technical = (
                self.writer._review_spec_with_retry(
                    spec, spec.blueprint, must_fix=vr.must_fix(),
                    own_fix_focus=list(vr.fixable)))
            if reviewed is None:
                last_err = why
                st = "review_technical" if technical else "review"
                _bump(info, st)
                log.info("第 %d 稿审稿未过(%s): %s",
                         attempt, st, str(why)[:120])
                continue
            spec = reviewed
            vr2 = validate_spec(spec)
            if not vr2.ok or vr2.fixable:
                last_err = "; ".join(vr2.errors + vr2.fixable)
                _bump(info, "post_review_validate")
                continue
            cok2, creasons2 = validate_curated(spec, blueprint)
            if not cok2:
                last_err = "; ".join(creasons2)
                _bump(info, "post_review_curated")
                info["reject_reasons"] = creasons2
                break
            # ---- ⑤ truth audit(**复用**) ----
            ta = self.writer._audit_with_retry(spec)
            if ta is not None and not (ta.get("narrator_truthful")
                                       and ta.get("mechanism_consistent")):
                last_err = str(ta.get("why") or "叙事真实性审计不过")
                st = ("truth_audit_technical" if ta.get("technical")
                      else "truth_audit")
                _bump(info, st)
                log.info("第 %d 稿 truth audit 不过: %s",
                         attempt, last_err[:120])
                continue
            # ---- ⑥ reveal adherence ----
            ra = validate_reveal_adherence(spec, spec.blueprint)
            if ra:
                last_err = "; ".join(ra)
                _bump(info, "reveal_adherence")
                continue
            # ---- ⑦ 跨题门(**复用**) ----
            rcfg = self.writer._cfg()
            xbad = cross_puzzle_gate(
                spec, recent,
                Quotas.from_config(rcfg) if rcfg is not None else None,
                spec.blueprint)
            if xbad:
                last_err = "; ".join(xbad)
                _bump(info, "cross_gate")
                continue
            # ---- ⑧ 与已出过的题太像 ----
            dup = _too_similar_pub(spec.puzzle, recent)
            if dup:
                last_err = f"与最近某题太像: {dup[:40]}"
                _bump(info, "too_similar")
                continue

            info["accepted"] = True
            info["style_tags"] = list(getattr(spec, "style_tags", []) or [])
            info["attempts"] = attempt
            log.info("编译成功(%s, 第 %d 稿): %s",
                     info["external_id"], attempt, spec.puzzle[:40])
            return spec, info

        # 循环走完仍未成功: stage 取**走到过的最深一道门** —— 那比
        # "最后一稿死在哪"有信息量得多(见上面 `_deepest` 的说明)。
        info["stage"] = info.pop("_deepest", "") or "unknown"
        info["reject_reasons"] = info.get("reject_reasons") or [last_err]
        return None, info


#: 管道深度 —— 越靠后说明这道题**走得越远**(前面的门都过了), 因此
#: 作为"没收原因"越有信息量。见 `CuratedCompiler.compile_one` 的说明。
_STAGE_DEPTH = {
    "compile_call": 0,
    "ai_gate": 1,
    "validate": 2,
    "curated_validate": 3,
    "review": 4,
    "review_technical": 4,
    "post_review_validate": 5,
    "post_review_curated": 5,
    "truth_audit": 6,
    "truth_audit_technical": 6,
    "reveal_adherence": 7,
    "cross_gate": 8,
    "too_similar": 9,
}


def _bump(info: dict, stage: str) -> None:
    """更新"走到过的最深一道门"。"""
    info["_deepest"] = _pick_stage(info.get("_deepest", ""), stage)


def _pick_stage(cur: str, new: str) -> str:
    """取两者里**更深**的那个(深度相同取新的)。"""
    if not cur:
        return new
    if _STAGE_DEPTH.get(new, -1) >= _STAGE_DEPTH.get(cur, -1):
        return new
    return cur


def _too_similar_pub(puzzle: str, recent: Optional[list]) -> str:
    """与最近窗口的谜面做文本近似检查。

    `recent` 是 signature 列表(跨题门要的那种), 里面**没有谜面文本**,
    所以这里只对 `recent` 里带 `puzzle` 属性的项做检查。传进来的通常是
    Engine 的 recent_signatures —— 那就退化成"不检查"。文本去重的主力
    是 H1-E 的语料层(跨来源), 这里只是补一道。

    刻意**不**自己维护一份"已编译的谜面"列表: 那会引入跨题状态, 而
    curated 编译是离线批处理 —— 状态应该显式传进来。
    """
    texts = [str(getattr(s, "puzzle", "") or "") for s in (recent or [])]
    texts = [t for t in texts if t]
    if not texts:
        return ""
    from story.quality import too_similar
    return too_similar(puzzle, texts)
