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

#: **准入政策**版本(H3-A)。与 `QUALITY_POLICY_VERSION` **刻意分开**。
#:
#: 为什么不能共用: quality-v8 是**AI 原创题**的内容政策(通关合同 / 发现
#: 层次 / 基调配比…), 而这里要管的是**另一件事** —— "一道外部来的题,
#: 够不够格算海龟汤"。两件事的修订节奏完全不同: 我们可能为了收紧题型
#: 把 curated 提到 v3, 却一点都不想动原创链的政策(反之亦然)。共用一个
#: 号会让"改了一边"看起来像"两边的题都过期了"。
#:
#: 它同时是**旧库存的隔离开关**:
#:     spec.curated_policy_version != CURATED_POLICY_VERSION
#:     -> 过不了题池准入门 -> 不计库存 / 播不出
#: 于是 H2 那批按 v1 收的题(含 q10000 那种)会**自动**失去 live
#: eligibility, 不需要任何人手工删文件。
CURATED_POLICY_VERSION = "curated-v2"


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

═══ 三条新增判据(v2, **这是本版的重点**) ═══

上面九条里, 6/7 两条我们**实测判得太松**: 一道"卡车烧油变轻"的物理
脑筋急转弯, 也能被论证成"有因果反转"(因果 = 烧油导致变轻), 于是混了
进来。问题不在模型不听话, 而在**这两条问的是"有没有反转", 而不是
"这是不是一个故事"**。

所以 v2 补三条**正交**的判据。它们问的是完全不同的问题:

10. `story_reconstruction` —— 玩家最后是在**重建一个故事模型**吗?

    为 true 当且仅当: 玩家最终要恢复的是"**发生了什么**", 包括
    人物身份 / 人物关系 / 时间 / 空间 / 行为目的 / 因果事件 / 视角 /
    物品意义。

    以下**全部为 false**(它们不是故事, 是知识点):
      - 发现一条物理规律(烧油变轻 / 浮力 / 热胀冷缩)
      - 发现一个数学技巧(数字排列 / 概率 / 称重)
      - 知道一个冷知识(某物其实是某物)
      - 知道一条职业规定或制度要求
      - 知道某个设备的冷门用途
      - 猜中一个单独机关

    自问一句: "谜底揭晓后, 观众脑子里是**多了一个故事**, 还是
    **多了一个知识点**?" 后者一律 false。

11. `multi_step_deduction` —— 是否有**至少两个彼此不同、都会改变
    玩家理解**的发现阶段?

    合格(两个发现各自改变理解):
      发现 A: 死者和"陌生人"其实认识      -> 改变**人物关系**
      发现 B: 两人不是在现在见的面        -> 又改变**时间模型**

    不合格(**只有一个**机制的因果展开):
      汽车烧油 -> 汽车变轻 -> 所以没超重
    这只是**同一件事**顺着推三步, 每一步都没有让前面的事实改变含义。
    自问: "第二个发现有没有让我**回头重新理解**第一个发现?" 没有就
    false。

12. `single_trick` —— 是不是**一个知识点就结束**?

    以下任一为 true:
      整个谜底只有一个知识点 / 知道一个小技巧立即结束 /
      只有一个物理规律 / 只有一个文字双关 / 只有一个机关用途 /
      只有一条职业或制度规则 / 不需要重新构造故事世界

    ⚠️ 注意 direction: 这条是**反向**的, true = 坏。很多"脑筋急转弯"
    都在这条上是 true。

**硬门(代码强制, 不是建议):**

    story_reconstruction == true
    multi_step_deduction == true
    single_trick         == false

有一条不满足 -> accepted 必须是 false。**不要**因为"这题别的方面都
很好"而放宽这三条 —— 放过一道卡车烧油, 整批的可信度就没了。

⚠️ 常见的错误判断(请自查):
  - "谜底挺巧妙的" -> 巧妙 != 是故事。物理/数学技巧同样巧妙。
  - "有反转" -> 见上, 6 条判得不严, 用 10/11/12 重新验。
  - "观众会问问题" -> 能问答 != 是海龟汤。物理题也能问答。
  - "这是 situation tag 的题" -> tag 是来源侧标的, 不可信(SE 上
    `lateral-thinking` 混着大量数学/物理/字谜)。只看内容本身。

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
                    # ---- v2 三条新判据。方向见 description。 ----
                    "story_reconstruction": {
                        "type": "boolean",
                        "description": (
                            "玩家最终是在**重建一个故事模型**吗"
                            "(身份/关系/时间/空间/目的/因果/视角/物品意义)?"
                            "纯粹发现一条物理规律 / 一个数学技巧 / "
                            "一个冷知识 / 一条职业规定 -> false。"),
                    },
                    "multi_step_deduction": {
                        "type": "boolean",
                        "description": (
                            "是否有**至少两个彼此不同、都会改变玩家理解**"
                            "的发现阶段? 同一机制的因果展开(烧油->变轻->"
                            "没超重)只算一个, 填 false。"),
                    },
                    "single_trick": {
                        "type": "boolean",
                        "description": (
                            "⚠️ 反向字段: true = **坏**。整个谜底只有"
                            "一个知识点 / 知道一个小技巧就结束 / 只有"
                            "一条规则 -> true。好的海龟汤这里应填 false。"),
                    },
                },
                "required": ["clear_anomaly", "unique_explanation",
                             "yes_no_progress", "no_obscure_system",
                             "not_pure_puzzle", "has_reversal",
                             "detail_recontextualized", "no_external_media",
                             "livestream_safe",
                             "story_reconstruction", "multi_step_deduction",
                             "single_trick"],
            },
            "content_style": {
                "type": "array", "items": {"type": "string"},
                "description": ("**内容风格**标签(可多选), 如 悬疑 / "
                                "细思极恐 / 反差 / 意外 / 情感 / 亲情 / "
                                "悲剧 / 恐怖氛围 / 逻辑 / 脑洞。"
                                "与 style_tags(结构型反转)不同, 这里描述"
                                "的是**观感**。"),
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
#:
#: ⚠️ 别名 `CURATED_CHECKS` 保留给 H2 的调用方。v2 起**真正的判据是
#: `CURATED_CHECKS_V2`** —— 见那里的说明。
CURATED_CHECKS = (
    "clear_anomaly", "unique_explanation", "yes_no_progress",
    "no_obscure_system", "not_pure_puzzle", "has_reversal",
    "detail_recontextualized", "no_external_media", "livestream_safe",
)

#: v2 全量判据 = H2 那九条 + H3 新三条。
#:
#: ## 为什么新三条是**独立字段**而不是"把 has_reversal 判严一点"
#:
#: 因为那是两个不同的问题:
#:
#:     has_reversal       "有没有反转?"        <- 卡车烧油:**有**
#:     story_reconstruction "这是不是个故事?"  <- 卡车烧油:**不是**
#:
#: 把它塞进 has_reversal 会让那一条同时承担两个问题, 于是模型在
#: prompt 里看到的是"有没有反转", 判的却是"是不是故事" —— 这种
#: 名实不符是判据漂移最常见的起点。分开之后, 每一条都能被单独
#: 质疑和单独修改。
CURATED_CHECKS_V2 = CURATED_CHECKS + (
    "story_reconstruction", "multi_step_deduction", "single_trick",
)

#: `single_trick` 是**反向**判据(true = 坏), 其余全是 true = 好。
#: 混在一起遍历会写错方向, 所以显式列出来。
_INVERTED_CHECKS = ("single_trick",)


def check_tool_result(d: dict, *, checks: tuple = CURATED_CHECKS_V2) -> tuple:
    """判"这道题能不能收"。返回 `(ok, reasons)`。

    ## 为什么先判 `accepted` 再判其它

    模型可能 `accepted=true` 但 `quality_checks` 里有 false(自相矛盾)。
    这时**以 quality_checks 为准** —— 它是逐条判据, 比一个总结布尔更
    可信; 而且 fail closed 方向才安全(宁可拒一道好题, 不能放一道烂题)。

    同理 `accepted=false` 但没有 reasons 也算拒(理由缺了不影响结论)。

    ## v2: 硬门是**代码**判的, 不是靠 prompt 说服模型

    新三条(reconstruction / multi_step / single_trick)在 prompt 里已经
    说得很重, 但 prompt 是**请求**, 不是**保证**。所以这里对它们**再
    判一次**: 只要 `story_reconstruction` 不是 true、或 `single_trick`
    是 true, 直接拒 —— 哪怕模型自己填了 `accepted=true`。

    这一层是 q10000 那道题真正的拦截点: 它是"模型判对了但总结布尔写
    反了"时的兜底。
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

    # ---- 逐条 fail closed(缺项 = 不合格) ----
    bad = []
    for k in checks:
        v = qc.get(k)
        if k in _INVERTED_CHECKS:
            # 反向: 必须**明确**为 false。"没说"也算不合格 ——
            # 一条没说自己是 single_trick 的题, 凭什么信它?
            if v is not False:
                bad.append(f"{k}(应 false, 实为 {v!r})")
        else:
            if v is not True:
                bad.append(k)
    if bad:
        reasons.append("quality_checks 未全过: " + ", ".join(bad))
        return False, reasons
    return True, []


def story_gate_reasons(d: dict) -> list:
    """v2 硬门的**独立**判定, 返回不通过的原因(通过则空列表)。

    与 `check_tool_result` 分开刻意为之: 那个函数管"九条 + 三条全部
    为 true", 这个只管**三条里最要害的部分**, 好让拒绝原因能带上
    精准的标签(`single_trick` / `not_story_reconstruction` /
    `no_multi_step_deduction`) —— 验收报告要按原因分类, 而
    "quality_checks 未全过: story_reconstruction" 这种串既难统计
    也看不出是哪一类问题。
    """
    qc = d.get("quality_checks") if isinstance(d.get("quality_checks"),
                                               dict) else {}
    out: list = []
    if qc.get("single_trick") is not False:
        out.append("single_trick")
    if qc.get("story_reconstruction") is not True:
        out.append("not_story_reconstruction")
    if qc.get("multi_step_deduction") is not True:
        out.append("no_multi_step_deduction")
    return out


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
    #: 内容风格(H3-A)与结构型 style_tags 分开 —— 见 PuzzleSpec 的字段说明。
    spec.content_style = list(d.get("content_style") or [])   # type: ignore[attr-defined]
    # ---- H3-A: curated 准入政策版本 ----
    # 这是**旧库存的隔离开关**: 题池准入门要求它等于当前
    # CURATED_POLICY_VERSION。H2 那批(v1, 含 q10000)落盘时没有这把键
    # -> 读出来是空串 -> 自动失去 live eligibility, 不需要手工清理。
    spec.curated_policy_version = CURATED_POLICY_VERSION      # type: ignore[attr-defined]
    return spec


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
                    max_attempts: int = 2,
                    should_continue: Optional[Any] = None
                    ) -> tuple:
        """编译一道。返回 `(spec, info)`。

        `spec` 为 None 表示没收(AI 拒绝 / 硬校验不过 / 跨题门拒)。
        `info` 里带 `accepted` / `reject_reasons` / `stage` —— **不要**
        把没收的原因只打进日志: 验收报告要按原因分类统计, 而日志是要
        靠人肉数的。

        ## `should_continue`(H3-B): 给直播让路

        一个无参回调, 返回 False 表示"现在别审了"。**每个昂贵的门之前**
        都会重新问一次 —— 因为一次编译可能跑十几秒到几十秒(编译 +
        审稿 + 审计, 还可能要重试), 期间观众完全可能开始提问、或者
        下一题要上了。这些资源必须归直播。

        中断时 `info["interrupted"] = True` —— 调用方据此写
        `interrupted` 决策(**不是** rejected), 那条题下次还能重审。

        与 G4-C 的让路检查同一个思路: 协作式, 在**边界**上让, 不抢占。
        """
        info: dict = {"external_id": getattr(rec, "external_id", ""),
                      "accepted": False, "reject_reasons": [], "stage": ""}
        client = self.writer.client
        user = build_user_prompt(rec, target_blueprint=blueprint)

        def _live_busy() -> bool:
            """直播需要资源 -> True。回调坏了也不能让编译崩溃。"""
            if should_continue is None:
                return False
            try:
                return not should_continue()
            except Exception:                   # noqa: BLE001
                log.exception("should_continue 回调抛异常, 按'让路'处理")
                return True

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
            # ---- 让路检查 ①: 调 LLM 之前 ----
            if _live_busy():
                return None, _mark_interrupted(info, "before_compile")

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
                # ---- 分支 A: 模型**明确**说不行 ----
                #
                # 它自己给的 reason(`not_a_story` / `language_dependent`
                # / …)是最准确的原因, 直接采信并**不重试**(重试只会让它
                # 换个说法硬凑 accepted=true, 那正是最不想看到的)。
                #
                # ⚠️ 这一支排在故事门**前面**: 一份 `accepted=false` 的
                # 回复往往**没有** quality_checks, 此时故事门只能笼统地说
                # "三条都不合格" —— 那是噪音, 会把模型给出的精准原因
                # 挤掉。故事门的职责是拦"模型说行、其实不行", 不是替
                # 模型解释它为什么说不行。
                if d.get("accepted") is not True:
                    info["reject_reasons"] = reasons
                    info["stage"] = "ai_gate"
                    log.info("AI 拒收 %s: %s",
                             info["external_id"], "; ".join(reasons)[:120])
                    return None, info

                # ---- 分支 B: 模型说行, 但故事门说不行 ----
                #
                # 这是 q10000 走的那条路: accepted=true, 九条也全 true,
                # 只有 v2 三条不合格。它必须在这里被拦下, **而且不重试**:
                #
                #   single_trick 是**结构性**判定 —— 这道题只有一个知识
                #   点, 再审十次还是只有一个知识点。重试不会变好, 只会把
                #   本该给好题的预算烧在垃圾上。
                #
                # 顺序上这一支必须在"自相矛盾 -> 重试"**之前**(见下),
                # 否则 q10000 会落进"多半是漏填, 再审一稿"那个分支,
                # 白烧 max_attempts 次调用。
                sgr = story_gate_reasons(d)
                if sgr:
                    info["story_gate"] = sgr
                    info["reject_reasons"] = sgr
                    info["stage"] = "story_gate"
                    log.info("v2 故事门拒收 %s: %s",
                             info["external_id"], ", ".join(sgr))
                    return None, info

                # ---- 分支 C: 自相矛盾(accepted=true 但 qc 有 false) ----
                #
                # 走到这里说明三条故事性判据是合格的, 死的是"九条"里的
                # 某一条 —— 那多半是漏填而不是真判 false, 再审一稿合理。
                log.warning("第 %d 稿 quality_checks 未全过: %s",
                            attempt, "; ".join(reasons)[:120])
                _bump(info, "ai_gate")
                continue

            # ---- ①b v2 硬门: 三条都对但我想再确认一次 ----
            #
            # 走到这里说明 `check_tool_result` 已经过了(十二条全合格),
            # 所以这道门通常**不会**触发。留着是为了 defense in depth:
            # 将来若有人把 `check_tool_result` 的 checks 参数收窄(比如
            # 只传 H2 的九条), 故事门仍然独立生效。
            sgr = story_gate_reasons(d)
            if sgr:
                info["story_gate"] = sgr
                info["reject_reasons"] = sgr
                info["stage"] = "story_gate"
                log.info("v2 故事门拒收 %s: %s",
                         info["external_id"], ", ".join(sgr))
                # 结构性判定, 重试不会变好 -> 不重试。
                return None, info
            # **明确**的表态。
            sgr = story_gate_reasons(d)
            if sgr:
                info["story_gate"] = sgr
                info["reject_reasons"] = sgr
                info["stage"] = "story_gate"
                log.info("v2 故事门拒收 %s: %s",
                         info["external_id"], ", ".join(sgr))
                # 结构性判定, 重试不会变好 -> 不重试。
                return None, info

            spec = spec_from_tool(d, rec, blueprint)
            spec.usage, spec.model = res.usage, res.model
            #: 内容风格(与结构型 style_tags 分开存, 供验收按观感分类)。
            spec.content_style = list(d.get("content_style") or [])  # type: ignore[attr-defined]

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
            # ---- 让路检查 ②: 编译过了, 审稿之前 ----
            if _live_busy():
                return None, _mark_interrupted(info, "before_review")
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
            # ---- 让路检查 ③: 审计之前 ----
            if _live_busy():
                return None, _mark_interrupted(info, "before_audit")
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
            # ---- 让路检查 ④: 全部门都过了, **写盘之前** ----
            #
            # 这一处最要紧: 写盘会同时产生"入池 + attribution"两个副作用,
            # 而它们必须在直播不忙时完成(写盘本身很快, 但如果恰好赶上
            # 磁盘紧张, 抢的就是直播的 IO)。更要紧的是语义 —— 这次
            # 让路发生在**任何终态产生之前**, 所以那条题下次会原样重审,
            # 不会留下"decision 说 accepted 而池里没有"的半状态。
            if _live_busy():
                return None, _mark_interrupted(info, "before_commit")

            info["accepted"] = True
            info["style_tags"] = list(getattr(spec, "style_tags", []) or [])
            info["content_style"] = list(
                getattr(spec, "content_style", []) or [])
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
#:
#: ⚠️ `story_gate` 的深度是 **1** —— 与 `ai_gate` 同级。它是 AI 门的
#: v2 加强版(同一个位置的更严格判定), 不是"比 AI 门更深的一道门"。
#: 把它的深度排在 ai_gate 之上会让"被故事门拒了"看起来像"走得更远"。
_STAGE_DEPTH = {
    "compile_call": 0,
    "ai_gate": 1,
    "story_gate": 1,
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


def _mark_interrupted(info: dict, where: str) -> dict:
    """标记"这次是给直播让路中断的"。

    `interrupted=True` 是调用方写决策的依据 —— 它决定那条题**下次还能
    重审**。绝不能让它退化成 rejected(那会永久丢题), 所以这个标记
    和 `stage="interrupted"` 是**成对**出现的, 由这个函数统一设,
    不靠调用点各自记得。
    """
    info["interrupted"] = True
    info["stage"] = "interrupted"
    info["interrupt_at"] = where
    info["reject_reasons"] = [f"interrupted:{where}"]
    log.info("curated interrupted: %s (%s)", info.get("external_id", ""),
             where)
    return info


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
