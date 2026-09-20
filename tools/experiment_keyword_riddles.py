#!/usr/bin/env python
# coding: utf-8
"""G1-A 实验 —— **极简关键词种子 -> AI 自由形成核心海龟汤 -> 再结构化**。

## 这个脚本是**实验**, 不是生产

    ✅ 单独存在, 不接生产, 不改 RIDDLE_SYSTEM
    ✅ 不碰 curated 池 / Blueprint scheduler / Judge / QA / Reveal
    ✅ 不写任何生产文件(只往 --out 目录写)

任务书 §六: 生产链 `RIDDLE_SYSTEM` 保持原样。本脚本的**第一阶段**
用自己的一套 prompt(`STAGE1_SYSTEM` / `_TOOL_IDEA`), **不**复用
`RIDDLE_SYSTEM` —— 那正是本实验要对照的东西。

## 实验思想(任务书 §一)

外部题源(haiguitang 那类)的生成形状是:

    关键词: 老人, 借书
      -> 模型先围绕**少量关键词**形成一个 谜面 + 谜底

而不是先接受一份复杂 Blueprint 再命题作文。本实验就是测后者换成
前者之后, 出来的题是否更接近自然题库的语感。

## 两阶段(§三 / §四)

    阶段 1(§三): 只产生 canonical idea
        输入只有 `关键词: X，Y[, Z]`
        输出只有 title / puzzle / answer
        —— **不**同时想 facts / atoms / completion / discovery_beats

    阶段 2(§四): 结构化
        阶段 1 的 puzzle + answer **固定**, 交给既有的结构化能力
        (curated 编译链: `spec_from_tool` + Reviewer + truth audit +
         `validate_spec`), 目标是 facts / core_answer /
        completion_fact_ids / solve_atoms / fair_clues /
        discovery_beats / observed signature。
        —— **不得**改变阶段 1 的核心机制。允许 schema 修复, 不允许
            "为了 schema 把题重新写成另一道题"。

## target Blueprint = None(§五, 本实验的核心)

    20 道的 `target Blueprint = None`

用 `make_unconstrained_blueprint()` —— 即 curated 链识别为
"**没有** target 骨架"的那个哨兵。于是稍后记录的
`mechanism / solution_shape / domain / relation` 是**分类结果**,
不是创作指令。

## 关键词(§二)

固定 seed, 程序抽取, **不人工挑**。10 组 x 2 keywords + 10 组 x 3
keywords = 20 组, 由 `--seed` 决定(默认 20260920)。抽到的原样写进
报告 —— 包括那些看起来不好出题的组合。

## 用法

    # 正式跑 20 组(会调 LLM)
    .venv/Scripts/python.exe tools/experiment_keyword_riddles.py \\
        --out data/g1a --seed 20260920

    # 只打印抽到的 20 组关键词(不调 LLM)
    .venv/Scripts/python.exe tools/experiment_keyword_riddles.py --draw-only
"""

from __future__ import annotations

import argparse
import concurrent.futures
import io
import json
import logging
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from story.config import Config  # noqa: E402
#: G2: 抽词逻辑已经**搬进生产模块** `story/keyword_seed.py` —— 方向是
#: `story/ <- tools/`, 绝不允许反过来。本脚本现在只是它的一个调用方,
#: 所以"实验抽到的词"与"生产会抽到的词"永远是同一份实现, 不会漂。
#:
#: G3: 生产的词源从人工 `KEYWORD_BANK` 换成了真实 haiguitang corpus
#: (`story/keyword_corpus.py` + `KeywordBag`)。本实验脚本**保留**
#: `draw_keyword_groups` 路径 —— 它的 20 组是 G1-A/G1-B 报告里逐字列过的
#: **历史数据**, 换掉就没法复现那两份报告了。
#:
#: ⚠️ 任务书 §九: 本脚本**不得**维护第二份 corpus / 第二份词库。它现在
#: 只 import 生产实现, 自己一行词表都没有。要看 G3 的真实抽词, 用
#: `--draw-corpus`(它直接调生产 `KeywordBag`, 见下)。
from story.keyword_seed import (  # noqa: E402
    KEYWORD_BANK, KEYWORD_BANK_VERSION, KEYWORD_SEED_VERSION, _SLOTS,
    derive_session_seed, draw_keyword_groups, keywords_line, load_bag,
)
from story.llm import (  # noqa: E402
    AnthropicMessagesClient, PuzzleWriter, validate_spec,
)
from story.puzzle import PuzzleSpec  # noqa: E402
from tools.curated_compiler import (  # noqa: E402
    CuratedCompiler, hard_check_reasons, make_unconstrained_blueprint,
    spec_from_tool,
)

log = logging.getLogger("hgt.g1a")

# ======================================================================
# 一、关键词库(§二) —— **已搬进 `story/keyword_seed.py`**
# ======================================================================
#
# 这一段原来是本脚本的本地副本。G2 把它搬进生产模块, 因为:
#
#   1. 生产(普通 AI `PoolPrefetcher`)现在真的要用它 —— 让生产去 import
#      一个 `tools/` 下的实验脚本是荒唐的;
#   2. 两份实现迟早会漂: 实验改一个词而生产没改, "实验结论"就不再描述
#      生产行为了。
#
# 现在**唯一**的实现是 `story.keyword_seed`, 本脚本从它 import。
# 下面的名字保持可用(报告代码与 `--draw-only` 都还在用), 但不再有副本:
#
#     KEYWORD_BANK / _SLOTS / _dedupe / draw_keyword_groups / keywords_line
#
# ⚠️ 抽词序列**逐位不变** —— `--draw-only` 的输出 md5 仍必须是
#    `049c7073412f906f962e32f4ff20e3fe`(G1-A/G1-B 的基线)。改词库或改
#    抽取方式都会让 G1 两批数据无法对比, 那正是本实验最不该引入的变量。



# ----------------------------------------------------------------------
# truth audit 的**只读观测**
# ----------------------------------------------------------------------
#
# 为什么需要它: `compile_one` 拿到审计结论后**只**用它当场 continue,
# 从不回写 spec 或 metrics(那是 `gen_spec` 才做的事)。于是从外面
# 无法回答 §七 要的 "truth audit result" —— 一条过了审计的 accepted
# 记录与一条根本没跑到审计的记录, 磁盘上长得**一模一样**。
#
# 这里在调用点包一层**只读**记录: 返回值一个字节都不改, 判定权仍然
# 完全在 `compile_one` 手里。观测层坏了也不能影响实验 —— 所以整个
# 包装体在 try/except 里。

_AUDIT_LOG: dict = {}


def _g1a_key(rec: dict) -> str:
    return "g1a:%02d" % rec.get("index", 0)


def _wrap_audit_observation(writer: PuzzleWriter) -> None:
    """给 `writer._audit_with_retry` 套一层记录(幂等)。"""
    if getattr(writer, "_g1a_audit_wrapped", False):
        return
    orig = writer._audit_with_retry

    def _observed(spec, should_continue=None):      # noqa: ANN001
        ta = orig(spec, should_continue=should_continue)
        try:
            eid = str(getattr(spec, "external_id", "") or "")
            if eid:
                _AUDIT_LOG[eid] = {
                    "ok": (None if ta is None else bool(
                        ta.get("narrator_truthful")
                        and ta.get("mechanism_consistent"))),
                    "technical": bool((ta or {}).get("technical")),
                    "issues": list((ta or {}).get("conflicts") or []),
                    "why": str((ta or {}).get("why") or ""),
                }
        except Exception:                           # noqa: BLE001
            log.exception("记录 truth audit 结果失败(不影响判定)")
        return ta

    writer._audit_with_retry = _observed             # type: ignore[assignment]
    writer._g1a_audit_wrapped = True                 # type: ignore[attr-defined]


# ======================================================================
# 二、阶段 1 prompt(§三) —— 只产生 canonical idea
# ======================================================================
#
# §三 的每一条都落在这里。"简单也可以 / 单机关也可以 / 普通生活常识
# 即可 / 不要求多层反转 / 不要求悲剧 / 不要求复杂人物背景 / 不要求职业
# 设定 / 不为显得高级增加第二机关" —— 这些是**负向要求**, 必须明写,
# 否则模型会自动往"更高级"的方向爬(那是它被训练出来的偏好)。
#
# ⚠️ 这一段**不**提 facts / atoms / completion / discovery_beats。
#    这正是 §三 要求的: "暂时不要让它同时想着"那些。

STAGE1_SYSTEM = """你是中文「海龟汤」(情境推理谜题)的出题人。全程用中文。

用户会给你 2~3 个**普通生活关键词**。请围绕这些关键词, 自己想一个
自然的谜题, 直接给出:

    title   —— 极短标题(可留空)
    puzzle  —— 谜面, 1~3 句
    answer  —— 谜底, 直接解释谜面里那个反常点

## 硬要求

1. 谜面要有一个**清楚的反常点**: 有一件事看起来不该发生 / 说不通,
   读者会想问"为什么会这样"。
2. 谜底必须**直接解释**那个反常点。读者看完谜底要能说"哦, 原来如此"。
3. 谜面结尾要是一个问句。
4. 用第三人称客观叙述, 不要"我"。
5. 只用关键词和**普通生活常识**, 不要引入输入之外的冷门知识。

## 以下都可以, 不要为了"显得高级"而回避

* **简单也可以** —— 一句话能问明白的题是合格的题。
* **单机关也可以** —— 不需要两个诡计叠在一起。
* **普通生活常识即可** —— 柴米油盐、邻里日常都行。
* 不要求多层反转, 不要求悲剧。
* 不要求复杂人物背景, 不要求职业设定。
* **不为显得高级增加第二机关** —— 一个干净的诡计胜过两个勉强的。

## 不要做的事

* 不要写提示、附注、谜底剧透在谜面里。
* 不要把关键词生硬地塞进去 —— 让它们自然地出现在情境里。
* 不要写需要看外部图片 / 听音频 / 上某个软件才能答的题。

先在心里想一个**自然的**情境, 再写出来。不要套模板。
"""

_TOOL_IDEA = {
    "name": "emit_idea",
    "description": "输出一个海龟汤的谜面与谜底(只有这三样)",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "极短标题(可留空)"},
            "puzzle": {"type": "string",
                       "description": "谜面: 1~3 句, 结尾是问句, 有一个清楚的反常点"},
            "answer": {"type": "string",
                       "description": "谜底: 直接解释谜面里的反常点"},
        },
        "required": ["puzzle", "answer"],
    },
}


def stage1_user_prompt(g: dict) -> str:
    return (keywords_line(g) + "\n\n"
            "请围绕这几个关键词写一道中文海龟汤。\n"
            "直接给出谜面与谜底。")


# ======================================================================
# 三、阶段 2: 结构化(§四)
# ======================================================================
#
# 复用 curated 编译链的**既有结构化能力**, 但输入不是外部语料, 而是
# 阶段 1 刚生成的那道题。
#
# 做法: 造一条**与 RawCuratedPuzzle 同形**的轻量记录(duck-typed),
# 把 stage1 的 puzzle/answer 当作 canonical 的 surface/bottom 喂给
# `CuratedCompiler.compile_one` —— 那条路径已经具备:
#
#     compile(结构化) -> validate_spec -> Reviewer -> truth audit
#     -> validate_curated -> accepted
#
# 且它**天然就是 target Blueprint = None**(§五): curated 链不派
# Blueprint, `spec_from_tool` 把 `bp=None` 如实保留成
# `make_unconstrained_blueprint()` 哨兵。

class _IdeaRec:
    """把阶段 1 的产出包装成 `RawCuratedPuzzle` 的形状(只读)。

    ⚠️ 这是**实验用的鸭子类型**, 不是新的数据模型。字段集合刻意与
    `tools.curated_common.RawCuratedPuzzle` 对齐, 因为
    `build_user_prompt()` / `spec_from_tool()` 要读它们。

    `source` 标成 `g1a:keyword-seed` —— 一眼能看出它不是外部语料,
    将来若有人误把它写进 curated 池, 溯源也立刻看得出来。
    """

    def __init__(self, g: dict, puzzle: str, answer: str, title: str = ""):
        self.external_id = "g1a:%02d" % g["index"]
        self.source = "g1a:keyword-seed"
        self.source_url = ""
        self.source_kind = "dataset"
        self.question_author = "g1a"
        self.question_author_url = ""
        self.answer_author = "g1a"
        self.answer_author_url = ""
        self.question_license = "experiment-only"
        self.answer_license = "experiment-only"
        self.title = title or ""
        self.surface = puzzle
        self.bottom = answer
        self.language = "zh"
        self.original_language = "zh"
        self.translated = False
        self.tags = list(g["keywords"])
        self.question_score = None
        self.answer_score = None
        self.question_license_inference = "experiment"
        self.answer_license_inference = "experiment"
        self.question_created_at = None
        self.answer_created_at = None
        self.question_id = None
        self.answer_id = None
        self.content_safety = ""
        #: 关键词原样带上 —— 报告要用, 也让溯源里能看出"这题是从哪
        #: 几个词长出来的"。
        self.g1a_keywords = list(g["keywords"])


# ======================================================================
# 四、跑一条
# ======================================================================

def run_one(compiler: CuratedCompiler, writer: PuzzleWriter, g: dict,
            client: AnthropicMessagesClient, temperature=None,
            max_stage1_attempts: int = 2,
            max_stage2_attempts: int = 4) -> dict:
    """一条 = 阶段 1 + 阶段 2。返回**逐条记录**(报告直接写它)。

    ## 阶段 1 的 attempts 语义

    §七 要 "generation attempts"。这里记的是**阶段 1 实际发出的调用
    次数** —— 阶段 2 的稿数由 `info` 单独带出(`compile_attempts`)。

    ## `max_stage2_attempts` 为什么比生产默认(2)高

    生产链 `compile_one` 默认 `max_attempts=2`, 那是**配合 LazyCurator
    重试**设计的: 一稿失败不重要, 后台下一拍还有机会。

    本实验**没有下一拍** —— 一次不成就永久丢掉这一组数据。而实测
    网关在 `_TOOL_CURATED` 这个 16 字段的大 schema 上**间歇性**返回
    `stop_reason=tool_use` 但 `input={}`(不是 token 触顶, 是网关抖动;
    同样的 body 连发 5 次全成功, 所以它不可预测)。给到 4 稿能把这
    类抖动和"这道题真的结构化不出来"分开 —— 报告里的
    `stage2_attempts` 会把两者如实记下来。
    """

    t0 = time.monotonic()
    rec: dict = {
        "index": g["index"],
        "group": g["group"],
        "keywords": list(g["keywords"]),
        "slots": list(g["slots"]),
        "seed": g["seed"],
        "seed_used": g["seed_used"],
        # ---- 阶段 1 ----
        "stage1_attempts": 0,
        "stage1_error": "",
        "title": "",
        "puzzle": "",
        "answer": "",
        # ---- 阶段 2 ----
        "stage2_attempts": 0,
        "stage2_stage": "",
        "stage2_reject_reasons": [],
        "core_answer": "",
        "observed_signature": {},
        "review_decision": "",
        "review_issues": [],
        "truth_audit": {},
        "validate_ok": False,
        "validate_reasons": [],
        # ---- 汇总 ----
        "final": "invalid",
        "puzzle_chars": 0,
        "answer_chars": 0,
        "elapsed_s": 0.0,
    }

    # ---------------- 阶段 1: 只产生 canonical idea ----------------
    idea = None
    last_err = ""
    for attempt in range(1, max(1, max_stage1_attempts) + 1):
        rec["stage1_attempts"] = attempt
        res = client.messages(STAGE1_SYSTEM, stage1_user_prompt(g),
                              max_tokens=1500, tool=_TOOL_IDEA,
                              temperature=temperature)
        ti = res.tool_input
        if ti:
            from story.llm import _unwrap_tool_input as _unw
            d = _unw(ti)
            pz = str(d.get("puzzle", "") or "").strip()
            an = str(d.get("answer", "") or "").strip()
            if pz and an:
                idea = {"title": str(d.get("title", "") or "").strip(),
                        "puzzle": pz, "answer": an}
                break
            last_err = "阶段 1 返回的谜面或谜底为空"
        else:
            last_err = res.error or "阶段 1 没有 tool_input"
        log.warning("[%02d] 阶段 1 第 %d 稿失败: %s",
                    g["index"], attempt, last_err[:100])

    if idea is None:
        rec["stage1_error"] = last_err
        rec["final"] = "stage1_failed"
        rec["elapsed_s"] = round(time.monotonic() - t0, 1)
        return rec

    rec["title"] = idea["title"]
    rec["puzzle"] = idea["puzzle"]
    rec["answer"] = idea["answer"]
    rec["puzzle_chars"] = len(idea["puzzle"])
    rec["answer_chars"] = len(idea["answer"])

    # ---------------- 阶段 2: 结构化(§四) ----------------
    #
    # ⚠️ 阶段 1 的 puzzle/answer 在这里**冻结**。下面用的是 curated
    # 编译链: 它以 surface/bottom 为 canonical, 在 prompt 里明确要求
    # "你是编辑不是出题人"。所以它做的是**结构化**, 不是重写。
    #
    # Reviewer 仍可能要求重出 —— 那正是我们要观测的现象(见报告),
    # 不是要绕开的东西。这里**不**为了通过率放宽任何门。
    irec = _IdeaRec(g, idea["puzzle"], idea["answer"], idea["title"])
    _AUDIT_LOG.pop(irec.external_id, None)
    spec, info = compiler.compile_one(
        irec, recent=None, blueprint=None,
        max_attempts=max_stage2_attempts)

    rec["stage2_stage"] = info.get("stage", "")
    rec["stage2_reject_reasons"] = list(info.get("reject_reasons") or [])
    rec["stage2_attempts"] = int(info.get("attempts") or 0)
    rec["review_decision"] = str(
        getattr(writer, "_last_review_decision", "") or "")
    rec["review_issues"] = list(
        getattr(writer, "_last_review_issues", None) or [])
    if info.get("compile_checks"):
        rec["compile_checks"] = info["compile_checks"]
    if info.get("review_checks"):
        rec["review_checks"] = info["review_checks"]

    if spec is None:
        rec["final"] = {
            "ai_gate": "rejected_ai_gate",
            "hard_gate": "rejected_hard_gate",
            "review": "rejected_review",
            "review_technical": "technical_defer",
            "post_review_validate": "rejected_post_review_validate",
            "post_review_curated": "rejected_post_review_curated",
            "audit": "rejected_audit",
        }.get(rec["stage2_stage"], "rejected")
        rec["elapsed_s"] = round(time.monotonic() - t0, 1)
        return rec

    rec["core_answer"] = spec.core_answer or ""
    rec["observed_signature"] = spec.signature.to_dict()
    rec["puzzle_chars"] = len(spec.puzzle or "")
    rec["answer_chars"] = len(spec.core_answer or spec.answer or "")

    # ---- truth audit 结果 ----
    #
    # ⚠️ 这里**不能**读 `spec.metrics["truth_audit_ok"]`。
    #
    # 那个键只有 `gen_spec`(AI 原创链)会写; curated 链走的是
    # `compile_one`, 它把审计结论**只**用于当场 continue, 从不回写
    # spec/metrics。所以从外面看, 一道过了审计的题与一道没跑过审计
    # 的题长得一模一样 —— 报告会印成 "0 / 0", 看上去像"审计全没过"。
    #
    # 解法是在**调用点**观测: `_audit_with_retry` 每次被调都记一笔
    # (见 `_Observation` 包装)。这是**只读**观测, 不改任何判定 ——
    # 与 `PoolPrefetcher` 注入 probe 的做法同源。
    rec["truth_audit"] = dict(_AUDIT_LOG.get(_g1a_key(rec), {}) or {})

    # ---- validate_spec(§四: 正常走 validate_spec) ----
    vr = validate_spec(spec)
    rec["validate_ok"] = bool(vr.ok and not vr.fixable)
    rec["validate_reasons"] = list(vr.errors) + list(vr.fixable)

    rec["final"] = "valid" if rec["validate_ok"] else "invalid_validate"
    rec["elapsed_s"] = round(time.monotonic() - t0, 1)
    return rec


# ======================================================================
# 五、报告(§七 / §八)
# ======================================================================

#: 我的**人工观察**(§五 会原样印进报告)。
#:
#: ⚠️ 这一节是**判断**, 不是数据 —— 所以它明写在这里, 与 §二/§三 的
#: 机器产出分开, 读者一眼能看出哪部分是"跑出来的", 哪部分是"我读出来
#: 的"。数据在 `data/g1a/run.json`, 想推翻下面的判断请对着它看。
OBSERVATION_NOTES = [
    "### 我的观察(基于本批 5 道, 不是结论)",
    "",
    "**结论: 倾向于'值得作为候选生成器继续测', 但本批样本太小, "
    "不足以定论。**",
    "",
    "支持的理由:",
    "",
    "1. **谜面确实像自然题库的题。** 5 道的谜面都短(median 33 字)、"
    "都是一个清楚的单反常点, 读起来与 haiguitang 那类题**同一语感** ——"
    "这恰恰是 Blueprint 命题作文最容易丢掉的东西:",
    "   * `[05]` \"图书馆只有六层, 他每天爬到七楼\" —— 一句话, 干净。",
    "   * `[02]` \"先上顶楼再下到三楼\" —— 反常点一目了然。",
    "2. **关键词是自然长在情境里的**, 没有硬塞。`[05]` 的\"上楼\"+\"图书馆\""
    "直接构成谜面本身; `[02]` 的\"老人\"+\"上楼\"同理。没有一个词是被"
    "强行安上去的。",
    "3. **没有'为显得高级加第二机关'。** 5 道全是单机关, 这正是 §三 明确"
    "允许、而 Blueprint 链倾向于惩罚的形状。",
    "4. **observed signature 是\"读出来的\", 不是\"派下去的\"** ——"
    "`hidden_function` / `information_gap` / `misunderstood_object` 都是"
    "结构化阶段事后判的。§五 的设计目的达到了。",
    "",
    "反对 / 需要警惕的理由:",
    "",
    "1. **5 道里有 2 道没过(3/5)。** `[03]` 被硬门拒"
    "(`accepted=false`), `[04]` 被 truth audit 拒(叙事自相矛盾: "
    "\"被推下去摔断腿\"与\"每天在桥下理发\"的因果在审计眼里不成立)。"
    "这个通过率**不算高**。",
    "   * ⚠️ 但这**不能**直接读成\"keyword 生成更差\": 本批没有对照组"
    "(没有跑同规模的 Blueprint 链)。要下这个判断, 必须补一组对照。",
    "2. **`[04]` 暴露了一个真实风险: 自由成题容易滑向悲剧 / 重口。**"
    "两条关键词(\"理发师\"+\"桥\")自己会长出\"推下桥\"这种情节 ——"
    "而 §三 明说\"不要求悲剧\"。若要走这条路, curated-v5 的口径与"
    "safety 门**必须**同样作用在它身上(本实验里它们确实起作用了 ——"
    "`[04]` 就是被 truth audit 拦下的)。",
    "3. **本批全部是 2-key**(前 5 组恰好都是)。**3-key 一道都没跑** ——"
    "3 个词是否会让模型硬塞、或反而更容易成形, 这里**没有数据**。",
    "",
    "### 建议",
    "",
    "值得继续, 但下一轮应该补:",
    "",
    "* 一组**同规模对照**(Blueprint 链跑同样 5 组关键词), 否则无法回答"
    "\"是否更好\"。",
    "* **3-key 组**至少要跑到, 否则 §二 的 3-key 设计等于没测。",
    "* 统计 `[04]` 那类**悲剧倾向**的出现率 —— 如果高, 说明这条路"
    "需要额外的口径约束。",
]

#: `_report_only` 模式下报告里写的说明(诚实标注提前收手)。
SKIP_NOTE = ("本轮按用户指示**提前收手**: 用户要求 5 道即可(加快进度)。 "
             "下面 5 道是 seed 20260920 抽到的**前 5 组**(全部 2-key)。 "
             "第 6~20 组的词已抽出但**未跑**, 不在此报告中冒充结果。")

#: G1-B: 同一脚本的 3-key 批(与 G1-A 的 2-key 批对照)。
G1B_NOTE = ("G1-B: 同一 seed、同一 stage1/stage2/Reviewer/truth audit, "
            "只把 `--key-count` 换成 3。抽到的 5 组是第 11~15 组 —— "
            "抽取序列与 G1-A **逐字相同**, 所以两批可比。"
            "**没有**为 3-key 增加任何 prompt 规则。")

#: G1-B 的决策结论(§决策规则)。
DECISION_NOTE = [
    "### 决策",
    "",
    "> **默认采用 2-key。**",
    "",
    "依据(对照表见 `data/audit/G1B_2key_vs_3key.md`):",
    "",
    "* 两者 **valid 持平(3/5)**、stage1 成题率持平(5/5)、"
    "truth audit 持平(3/3) —— 3-key **没有**输在通过率上。",
    "* 但 3-key 的**谜面中位长度 64 字 vs 2-key 33 字(近两倍)**, "
    "而这是直播场景 —— 谜面越长, 观众听完抓住反常点的成本越高。",
    "* 5 道 3-key 里 **2 道出现\\\"为一个词硬造一层身份\\\"**"
    "(`[14]` 为塞\\\"服务员\\\"把图书馆改成咖啡馆; `[15]` 为塞\\\"相册\\\""
    "加上\\\"失散多年的儿子认亲\\\")。2-key 批**没有**这个现象。",
    "* 第 3 个词带来的是**背景复杂度**, 不是**故事自然度** —— "
    "而任务书的三条判据里, 只有\\\"更自然/更有变化\\\"这一条支持 3-key, "
    "它**没有**在样本上成立。",
    "",
    "即:\\\"valid 不差\\\"成立, 但\\\"更自然\\\"**不成立**、"
    "\\\"没有硬塞\\\"**不成立**。按决策规则取保守分支。",
]


def _median(xs: list) -> float:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return 0.0
    n = len(xs)
    return float(xs[n // 2]) if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def summarize(items: list) -> dict:
    n = len(items)
    valid = [i for i in items if i["final"] == "valid"]
    two = [i for i in items if i["group"] == "2key"]
    three = [i for i in items if i["group"] == "3key"]
    rev = {}
    for i in items:
        d = (i.get("review_decision") or "").lower()
        if d:
            rev[d] = rev.get(d, 0) + 1
    stage1_ok = [i for i in items if not i.get("stage1_error")]
    #: 只有**走到过审计**(= 出了 spec)的题才有审计结论。没走到的记
    #: "n/a", **不**计入 pass —— 否则"没跑"会被读成"不过"。
    audited = [i for i in valid if (i.get("truth_audit") or {}).get("ok")
               is not None]
    audit_pass = [i for i in audited if (i.get("truth_audit") or {}).get("ok")]
    return {
        "n": n,
        "valid": len(valid),
        "stage1_ok": len(stage1_ok),
        "review": rev,
        "audit_pass": len(audit_pass),
        "audit_ran": len(audited),
        "validate_pass": len([i for i in valid if i.get("validate_ok")]),
        "puzzle_median": _median([i["puzzle_chars"] for i in valid]),
        "answer_median": _median([i["answer_chars"] for i in valid]),
        "two_total": len(two),
        "two_valid": len([i for i in two if i["final"] == "valid"]),
        "three_total": len(three),
        "three_valid": len([i for i in three if i["final"] == "valid"]),
        "stage1_attempts": sum(i.get("stage1_attempts", 0) for i in items),
        "stage2_attempts": sum(i.get("stage2_attempts", 0) for i in items),
    }


def write_report(path: str, meta: dict, groups: list, items: list) -> None:
    """§七 + §八: 逐条记录 + **全部 20 道原样列出**。"""
    s = summarize(items)
    L: list = []
    w = L.append

    w("# G1-A —— 关键词种子 -> 自由成题 -> 再结构化(实验)")
    w("")
    w("> **本轮不是要求证明 keyword generation 一定更好。**")
    w("> 只回答一个问题: 这种极简关键词起题方式, 是否值得成为")
    w("> AI-original 的**新候选生成器**?")
    w("")
    w("**基线** main `%s`" % meta.get("base_main", ""))
    w("**seed** `%s` · **2-key** 10 组 · **3-key** 10 组 · 共 **%d** 组"
      % (meta.get("seed"), s["n"]))
    w("**target Blueprint** = `None`(全 20 道) —— `mechanism / "
      "solution_shape / domain / relation` 是**分类结果**, 不是创作指令")
    w("**生产链** `RIDDLE_SYSTEM` **未改**; 本脚本是独立实验文件")
    w("")
    if meta.get("note"):
        w("> ⚠️ %s" % meta["note"])
        w("")

    w("## 一、实验命令")
    w("")
    w("```")
    w(meta.get("cmd", ""))
    w("```")
    w("")

    # ---------------- 全部样本原样(§八) ----------------
    w("## 二、全部 %d 道完整样本(§八: 原样列出, 不删丑题)" % s["n"])
    w("")
    w("失败题也原样保留失败原因 —— **没有一道被删掉**, 包括丑题和故障题。")
    w("")
    for i, g in zip(items, groups):
        star = " ✅" if i["final"] == "valid" else " ❌"
        w("### [%02d] `%s`%s" % (i["index"], i["group"], star))
        w("")
        w("**关键词**: `%s`  (槽位 %s, seed_used=%s)"
          % ("，".join(i["keywords"]), "+".join(i["slots"]), i["seed_used"]))
        w("")
        if i.get("stage1_error"):
            w("**阶段 1 失败**: %s" % i["stage1_error"])
            w("")
            w("---")
            w("")
            continue
        w("**title**: %s" % (i["title"] or "(空)"))
        w("")
        w("**puzzle**(谜面):")
        w("")
        w("> %s" % (i["puzzle"] or "").replace("\n", "\n> "))
        w("")
        w("**core_answer**(谜底):")
        w("")
        w("> %s" % (i["core_answer"] or i["answer"] or "(无)").replace(
            "\n", "\n> "))
        w("")
        if i["final"] != "valid" and i.get("answer"):
            w("**阶段 1 原 answer**(未过审, 仅存档):")
            w("")
            w("> %s" % i["answer"].replace("\n", "\n> "))
            w("")
        w("| 项 | 值 |")
        w("|---|---|")
        w("| final | **%s** |" % i["final"])
        w("| stage 2 stage | `%s` |" % i.get("stage2_stage", ""))
        w("| puzzle chars | %s |" % i["puzzle_chars"])
        w("| answer chars | %s |" % i["answer_chars"])
        w("| 阶段1 attempts | %s |" % i.get("stage1_attempts", 0))
        w("| 阶段2 attempts | %s |" % i.get("stage2_attempts", 0))
        w("| Reviewer | `%s` |" % (i.get("review_decision") or "(无)"))
        if i.get("review_issues"):
            w("| Reviewer issues | %s |" % ", ".join(
                str(x) for x in i["review_issues"][:6]))
        ta = i.get("truth_audit") or {}
        if ta.get("ok") is True:
            _ta = "pass"
        elif ta.get("ok") is False:
            _ta = ("fail: " + "; ".join(str(x) for x in
                                        (ta.get("issues") or [])[:3])
                   if ta.get("issues") else "fail")
        else:
            _ta = "n/a(阶段 2 未走到)"
        w("| truth audit | %s |" % _ta)
        # ⚠️ 被判 "没走到阶段 2" 的题 validate_spec **根本没跑** ——
        # 不能印成 "fail:"(那会让人以为它结构不合格)。三态分清:
        #   pass / fail:<理由> / n/a(阶段 2 未走到, validate 没执行)
        if i.get("validate_ok"):
            _vr = "pass"
        elif i.get("final") == "valid":
            _vr = "pass"
        elif i.get("core_answer") or i.get("stage2_stage") in (
                "post_review_validate", "post_review_curated"):
            _vr = ("fail: "
                   + "; ".join(i.get("validate_reasons") or ["(无理由)"])[:200])
        else:
            _vr = "n/a(阶段 2 未走到, validate 未执行)"
        w("| validate_spec | %s |" % _vr)
        if i.get("stage2_reject_reasons"):
            w("| 拒因 | %s |"
              % ", ".join(str(x) for x in i["stage2_reject_reasons"]))
        w("| 用时 | %ss |" % i.get("elapsed_s"))
        w("")
        sig = i.get("observed_signature") or {}
        if sig:
            w("**observed signature**(§五: 分类结果, 不是创作指令):")
            w("")
            w("```")
            w(json.dumps(sig, ensure_ascii=False, indent=2))
            w("```")
            w("")
        w("---")
        w("")

    # ---------------- 汇总(§七) ----------------
    w("## 三、汇总(§七)")
    w("")
    w("| 指标 | 值 |")
    w("|---|---|")
    w("| %d 道成功生成数(stage 2 valid) | **%d / %d** |"
      % (s["n"], s["valid"], s["n"]))
    w("| 阶段 1 出题成功数 | %d / %d |" % (s["stage1_ok"], s["n"]))
    w("| Reviewer pass/fix/rewrite | %s |"
      % (json.dumps(s["review"], ensure_ascii=False) or "(无)"))
    w("| truth audit pass | %d / %d(走到审计的题) |"
      % (s["audit_pass"], s["audit_ran"]))
    w("| validate pass | %d |" % s["validate_pass"])
    w("| puzzle 长度 median | %s 字 |" % s["puzzle_median"])
    w("| answer(core_answer) 长度 median | %s 字 |" % s["answer_median"])
    w("| 2-key 成功率 | %d / %d |" % (s["two_valid"], s["two_total"]))
    w("| 3-key 成功率 | %d / %d |" % (s["three_valid"], s["three_total"]))
    w("| 阶段1 总调用 | %d |" % s["stage1_attempts"])
    w("| 阶段2 总稿数 | %d |" % s["stage2_attempts"])
    w("")

    # ---------------- 抽到的关键词(§二: 原样写入) ----------------
    w("## 四、程序抽到的 20 组关键词(§二: 原样, 不人工挑)")
    w("")
    w("| # | 组 | 槽位 | 关键词 | seed_used |")
    w("|---|---|---|---|---|")
    for g in groups:
        w("| %02d | %s | %s | `%s` | %s |"
          % (g["index"], g["group"], "+".join(g["slots"]),
             "，".join(g["keywords"]), g["seed_used"]))
    w("")

    # ---------------- 人工观察 ----------------
    w("## 五、人工观察(§十: 只回答'值不值得成为候选生成器')")
    w("")
    w("对着 §二 的样本逐条看这几件事 —— 这是本实验真正要回答的:")
    w("")
    w("1. 谜面读起来像不像**自然题库**里的题(而不是'命题作文')?")
    w("2. 反常点是不是**一句话就说清楚**了?")
    w("3. 谜底有没有**直接解释**那个反常点(而不是绕开)?")
    w("4. 有没有'为了显得高级'硬加的第二机关?")
    w("5. 关键词是**自然长在情境里**, 还是硬塞进去的?")
    w("")
    w("> 丑题、怪题、失败题**都在上面**, 没有一个被删掉。")
    w("")
    for _ln in OBSERVATION_NOTES:
        w(_ln)
    w("")

    io.open(path, "w", encoding="utf-8").write("\n".join(L))
    log.info("写入 %s(行数=%d)", path, len(L))


# ======================================================================
# 六、CLI
# ======================================================================
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="experiment_keyword_riddles",
        description="G1-A: 关键词种子 -> 自由成题 -> 再结构化(实验)")
    ap.add_argument("--out", default="data/g1a",
                    help="输出目录(逐条 JSON + 报告)")
    ap.add_argument("--seed", type=int, default=20260920,
                    help="关键词抽取 seed(固定, 不要为了提高通过率换)")
    ap.add_argument("--base-main", default="",
                    help="报告里写的基线 main SHA")
    ap.add_argument("--max-stage1-attempts", type=int, default=2,
                    help="阶段 1 单条最多几稿(默认 2)")
    ap.add_argument("--max-stage2-attempts", type=int, default=4,
                    help=("阶段 2 单条最多几稿(默认 4; 生产 compile_one "
                          "默认只有 2, 因为生产有 LazyCurator 重试, "
                          "而实验没有下一拍 —— 见 run_one 的说明)"))
    ap.add_argument("--temperature", type=float, default=None,
                    help="阶段 1 temperature(默认沿用网关/配置)")
    ap.add_argument("--limit", type=int, default=0,
                    help="只跑前 N 组(调试用; 正式跑留 0 = 全 20)")
    ap.add_argument("--key-count", type=int, default=0, choices=(0, 2, 3),
                    help=("只跑几 key 的组: 0=两组都跑(默认, G1-A 行为), "
                          "2 / 3 = 只跑那一组。**过滤**而非重抽 —— 抽取序列"
                          "不变, 所以与 G1-A 的对应组逐字相同。"))
    ap.add_argument("--concurrency", type=int, default=3,
                    help=("并发组数(默认 3)。组与组完全独立, 唯一共享物是"
                          "无状态 HTTP 连接; writer 是**每组一个**, 因为"
                          "它的侧信道是实例属性, 共享会把 A 组审稿结论"
                          "记到 B 组头上。"))
    ap.add_argument("--draw-only", action="store_true",
                    help="只打印抽到的关键词, 不调 LLM")
    ap.add_argument("--draw-corpus", type=int, default=0, metavar="N",
                    help=("G3: 打印**生产 corpus** 抽到的前 N 个 pair"
                          "(不调 LLM)。走的是与生产完全相同的 `KeywordBag`,"
                          " 所以这行输出就是实播会拿到的词。"))
    ap.add_argument("--corpus", default="",
                    help="corpus 路径(默认 data/keyword2_seed_pairs.json)")
    ap.add_argument("--session-seed", type=int, default=None,
                    help=("keyword session seed。默认由 --seed 派生"
                          "(`derive_session_seed`), 与生产同一条路径。"))
    ap.add_argument("--report-only", default="",
                    help=("从已跑好的 items.json 重新生成报告(不调 LLM)。"
                          "传 items.json 的路径。用于提前收手后按已完成的"
                          "样本出报告 —— 报告与数据必须一致, 不能手写。"))
    ap.add_argument("--log-level", default="INFO")
    ap.add_argument("--log-file", default="")
    return ap


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, a.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        **({"filename": a.log_file, "filemode": "a"} if a.log_file else {}))
    if a.log_file:
        logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))

    groups = draw_keyword_groups(a.seed, a.key_count)

    # ---- 从已跑好的数据重新出报告(不调 LLM) ----
    #
    # 提前收手时用它: 报告必须与**真实跑出来的数据**一致。手写报告
    # 会立刻产生"H4 报告里说的是 A, 数据里是 B"的漂移。
    if a.report_only:
        with io.open(a.report_only, encoding="utf-8") as f:
            blob = json.load(f)
        items = blob["items"]
        groups = [g for g in blob.get("groups", groups)
                  if g["index"] in {i["index"] for i in items}]
        # seed 同时在顶层(早先的 items.json)与 meta 里(run.json)——
        # 两处都认, 免得重建出来的命令里写 `--seed None`。
        _seed = blob.get("seed") or (blob.get("meta") or {}).get("seed")
        meta = {"seed": _seed, "base_main": a.base_main,
                "note": blob.get("note", ""),
                "cmd": (
                    "# 正式跑(会调 LLM; 并发 3 路)\n"
                    ".venv/Scripts/python.exe -X utf8 "
                    "tools/experiment_keyword_riddles.py \\\n"
                    "    --out %s --seed %s --limit %s --concurrency 3\n"
                    "\n"
                    "# 只抽词不调 LLM(验证 seed 可复现)\n"
                    ".venv/Scripts/python.exe -X utf8 "
                    "tools/experiment_keyword_riddles.py --draw-only\n"
                    "\n"
                    "# 从已跑好的数据重建报告(不调 LLM)\n"
                    ".venv/Scripts/python.exe -X utf8 "
                    "tools/experiment_keyword_riddles.py \\\n"
                    "    --report-only %s"
                    % (os.path.dirname(a.report_only) or ".", _seed,
                       len(items), a.report_only))}
        rep = os.path.join("data", "audit", "G1A_keyword_seed_report.md")
        write_report(rep, meta, groups, items)
        s = summarize(items)
        print("从 %s 重建报告: %d 条, valid=%d"
              % (a.report_only, s["n"], s["valid"]))
        print("  -> %s" % rep)
        return 0

    if a.draw_only:
        print("seed=%s  (2-key x10 + 3-key x10)" % a.seed)
        for g in groups:
            print("  [%02d] %-5s %-28s %s"
                  % (g["index"], g["group"], "+".join(g["slots"]),
                     "，".join(g["keywords"])))
        return 0

    # ---- G3: 打印**生产 corpus** 的抽取序列(不调 LLM) ----
    #
    # 这一条走的是生产路径本身(`load_bag` + `KeywordBag.draw`), 不是副本
    # —— 任务书 §九 要的就是"实验不再维护第二份关键词逻辑"。
    if a.draw_corpus:
        from story.keyword_corpus import DEFAULT_CORPUS_PATH
        from story.keyword_seed import describe_bag
        ss = (a.session_seed if a.session_seed is not None
              else derive_session_seed(a.seed))
        bag, meta = load_bag(a.corpus or DEFAULT_CORPUS_PATH, ss)
        print(describe_bag(meta, ss))
        print("seed=%s" % a.seed)
        for _ in range(int(a.draw_corpus)):
            d = bag.draw()
            print("  [%02d] round %d  %s"
                  % (d["index"], d["round"], "，".join(d["keywords"])))
        return 0

    if a.limit and a.limit > 0:
        groups = groups[:a.limit]
        log.warning("--limit %d: 只跑前 %d 组(不是完整 20 组)",
                    a.limit, len(groups))

    os.makedirs(a.out, exist_ok=True)
    cfg = Config()
    client = AnthropicMessagesClient(cfg.llm)
    #: 计数器 —— 报告里的调用数必须是真的。**必须线程安全**: 并发跑时
    #: 多个 worker 同时 `messages()`, 非原子的 `+=` 会丢计数。
    calls = {"n": 0}
    _calls_lock = threading.Lock()
    _orig = client.messages

    def _counting(*args, **kw):
        with _calls_lock:
            calls["n"] += 1
        return _orig(*args, **kw)

    client.messages = _counting  # type: ignore[assignment]

    # ---------------- 并发跑(默认 3 路) ----------------
    #
    # 为什么可以并发: 组与组之间**完全独立** —— 每组有自己的关键词、
    # 自己的 `_AUDIT_LOG` 槽位(按 external_id 键)、自己的记录。
    # 唯一的共享物是 HTTP 连接(无状态), 所以并发的正确性没有疑问。
    #
    # ⚠️ 为什么**不**共用一个 `PuzzleWriter` 实例: `PuzzleWriter` 上有
    # `_last_review_decision` / `_last_review_issues` / `_last_review_checks`
    # 这些**实例属性**侧信道(`__post_init__` 里初始化的那份)。多个
    # 组同时跑, 后写的一方会覆盖先写的一方 —— 那会把 A 组的审稿结论
    # 记到 B 组头上。所以**每组一个 writer**, 各自持有自己的侧信道。
    #
    # client 共享是安全的: 它只持有配置与一个无状态 urllib 调用。
    workers = max(1, int(a.concurrency))
    log.info("并发 %d 路跑 %d 组", workers, len(groups))
    items: list = []
    t0 = time.monotonic()
    #: 逐组落盘的锁 —— 多个 worker 同时写 items.json 会互相截断。
    _io_lock = threading.Lock()
    _done: dict = {}

    def _one(g: dict):
        w = PuzzleWriter(client=client, runtime_cfg=cfg)
        _wrap_audit_observation(w)
        comp = CuratedCompiler(w)
        try:
            return run_one(comp, w, g, client,
                           temperature=a.temperature,
                           max_stage1_attempts=a.max_stage1_attempts,
                           max_stage2_attempts=a.max_stage2_attempts)
        except Exception as e:                  # noqa: BLE001
            log.exception("[%02d] 跑挂", g["index"])
            return {"index": g["index"], "group": g["group"],
                    "keywords": list(g["keywords"]),
                    "slots": list(g["slots"]),
                    "seed": g["seed"], "seed_used": g["seed_used"],
                    "final": "crashed",
                    "stage1_error": "%s: %s" % (type(e).__name__, e),
                    "puzzle": "", "answer": "", "core_answer": "",
                    "title": "", "puzzle_chars": 0, "answer_chars": 0,
                    "stage1_attempts": 0, "stage2_attempts": 0,
                    "observed_signature": {}, "review_decision": "",
                    "review_issues": [], "truth_audit": {},
                    "validate_ok": False, "validate_reasons": [str(e)],
                    "stage2_stage": "", "stage2_reject_reasons": [],
                    "elapsed_s": 0.0}

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_one, g): g for g in groups}
        for fut in concurrent.futures.as_completed(futs):
            g = futs[fut]
            r = fut.result()
            with _io_lock:
                _done[r["index"]] = r
                items = [_done[k] for k in sorted(_done)]
                # 逐条落盘 —— 跑挂了也不丢已完成的题。
                with io.open(os.path.join(a.out, "items.json"), "w",
                             encoding="utf-8") as f:
                    json.dump({"seed": a.seed, "items": items,
                               "groups": groups}, f, ensure_ascii=False,
                              indent=2)
            log.info("[%02d] final=%s stage=%s", r["index"], r["final"],
                     r.get("stage2_stage") or r.get("stage1_error", ""))

    items = [_done[k] for k in sorted(_done)]
    elapsed = round(time.monotonic() - t0, 1)
    meta = {"seed": a.seed, "base_main": a.base_main,
            "cmd": " ".join([".venv/Scripts/python.exe", "-X", "utf8",
                             "tools/experiment_keyword_riddles.py",
                             "--out", a.out, "--seed", str(a.seed)]),
            "elapsed_s": elapsed, "llm_calls": calls["n"]}
    with io.open(os.path.join(a.out, "run.json"), "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "summary": summarize(items),
                   "items": items, "groups": groups},
                  f, ensure_ascii=False, indent=2)

    rep = os.path.join("data", "audit", "G1A_keyword_seed_report.md")
    write_report(rep, meta, groups, items)

    s = summarize(items)
    print()
    print("=" * 62)
    print("G1-A 实验完成")
    print("=" * 62)
    print("  seed              : %s" % a.seed)
    print("  组数              : %d" % s["n"])
    print("  阶段1 出题成功    : %d / %d" % (s["stage1_ok"], s["n"]))
    print("  阶段2 final valid : %d / %d" % (s["valid"], s["n"]))
    print("  2-key 成功率      : %d / %d" % (s["two_valid"], s["two_total"]))
    print("  3-key 成功率      : %d / %d" % (s["three_valid"], s["three_total"]))
    print("  puzzle median     : %s 字" % s["puzzle_median"])
    print("  answer median     : %s 字" % s["answer_median"])
    print("  Reviewer          : %s" % json.dumps(s["review"], ensure_ascii=False))
    print("  LLM 调用          : %d" % calls["n"])
    print("  用时              : %ss" % elapsed)
    print("  -> 报告 %s" % rep)
    print("  -> 逐条 %s" % os.path.join(a.out, "run.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
