#!/usr/bin/env python
# coding: utf-8
"""运行: uv run tests/test_curated_compile.py（**完全离线, 无网络**）。

Batch H2 的离线回归: curated 编译器 —— "**AI 是编辑, 不是出题人**"。

## 这批要守的三件事

  1. **不许重新创作故事**(H2 铁律)。测试用假 client 断言:
     - 请求里确实**带上了** canonical 的 surface/bottom(模型看得到原文)
     - 请求里**显式声明**"这是已有题目, 不要重新创作"
     - **绝不调用 `emit_riddle`** —— 那一步会让模型顺手把故事改了
  2. **AI 审题门 fail closed**(H2-B)。九条判据缺一条、或自相矛盾
     (accepted=true 但某条 false), 都必须**拒**。
  3. **fair_clue 不许从谜底倒灌**(H2-E), 且**不许改谜面去迁就 quote**。
     谜面是 canonical source —— 这是 curated 与自由生成最大的区别。

## 为什么这些必须是离线测试

它们断言的是"我们给模型看了什么 / 我们怎么判它的回答" —— 纯逻辑。
放进联网测试里只会变成"这一次模型回了什么", 而那既不稳定也不说明
规则对不对。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from story.llm import LLMResult  # noqa: E402
from story.puzzle import PuzzleSpec  # noqa: E402
from story.quality import QUALITY_POLICY_VERSION  # noqa: E402
from tools import curated_compiler as CC  # noqa: E402
from tools.curated_common import RawCuratedPuzzle  # noqa: E402

FAIL = [0]


def check(name, cond, extra=""):
    if cond:
        print(f"  ok  {name}")
        return
    print(f"  FAIL {name}  {extra}")
    FAIL[0] += 1


# ======================================================================
# 假件
# ======================================================================
_PUZ = "守塔人只在退潮的那几个小时亮灯, 涨潮后反而熄掉。为什么?"
_ANS = "退潮时礁石露出水面, 他亮灯是为了标出礁石的位置, 不是给船引路。"


def mk_rec(**kw) -> RawCuratedPuzzle:
    d = dict(
        external_id="pse:q:999", source="Puzzling Stack Exchange",
        source_url="https://puzzling.stackexchange.com/q/999",
        source_kind="stackexchange",
        question_author="问者", question_author_url="u1",
        answer_author="答者", answer_author_url="u2",
        question_license="CC BY-SA 4.0", answer_license="CC BY-SA 3.0",
        question_license_inference="api", answer_license_inference="api",
        title="灯塔", surface=_PUZ, bottom=_ANS,
        language="en", original_language="en", translated=False,
        tags=["situation"], question_score=10, answer_score=20)
    d.update(kw)
    return RawCuratedPuzzle(**d)


def _qc_v2(**kw):
    """一份**能过 curated 当前全部判据**的 quality_checks。

    H3-A 起判据从九条变成十二条, 其中 `single_trick` 是**反向**的
    (true = 坏); H3-D 又加了 `no_external_knowledge_dependency` 变成
    十三条。所以不能再用 `{k: True for k in CURATED_CHECKS}` ——
    那会把 single_trick 填成 True, 于是每一道题都被故事门拒掉,
    看起来像"编译器全坏了"。

    ⚠️ 按 `CURATED_CHECKS_V3` **遍历**而不是手写清单: 手写的那份会在
    下次加判据时静默过期(这份 fixture 会变成"缺一项", 而生产里每道
    好题都被拒)。名字保留 `_qc_v2` 是因为调用点很多, 它现在是"当前
    政策的合格判据"的意思。
    """
    qc = {k: True for k in CC.CURATED_CHECKS_V3}
    qc["single_trick"] = False
    qc.update(kw)
    return qc


def _compile_tool(**kw):
    """一份**能过全部确定性门**的编译结果。"""
    body = _PUZ.rstrip("?？")
    d = {
        "accepted": True,
        "quality_checks": _qc_v2(),
        "content_style": ["悬疑", "细思极恐"],
        "style_tags": ["object_meaning", "causal_flip"],
        "title": "灯塔",
        "puzzle": _PUZ,
        "answer": _ANS,
        "core_answer": "他亮灯是为了标出退潮露出的礁石, 不是给船引路。",
        "completion_fact_ids": ["f1", "f2"],
        "facts": [
            {"id": "f1", "text": "退潮时礁石露出水面", "kind": "core",
             "visibility": "hidden"},
            {"id": "f2", "text": "灯的真正作用是标示礁石位置",
             "kind": "core", "visibility": "hidden"},
            {"id": "f3", "text": "涨潮后亮灯会误导船只", "kind": "support",
             "visibility": "hidden"},
            {"id": "f4", "text": "不是为了纪念死者", "kind": "exclusion",
             "visibility": "hidden"},
        ],
        "discovery_beats": [
            {"id": "b1", "text": "先注意到灯只在退潮时亮", "fact_ids": ["f1"]},
            {"id": "b2", "text": "再想到灯是在标礁石", "fact_ids": ["f2"]},
        ],
        "solve_atoms": [
            {"id": "a1", "role": "mechanism", "text": "灯是标礁石不是引路",
             "fact_ids": ["f1", "f2"], "required": True},
        ],
        "fair_clues": [
            {"quote": "退潮的那几个小时亮灯", "supports_atoms": ["a1"]},
        ],
        "hints": ["想想潮水", "注意灯的位置", "不是给船看"],
        "observed_signature": {
            "mechanism_family": "hidden_function",
            "solution_shape": "hidden_function_explains_behavior",
            "domain": "maritime", "relation": "stranger",
            "emotion_mode": "neutral", "time_shape": "instant",
            "death": False, "past_trauma": False,
            "long_term_profession": False, "repeated_ritual": False,
        },
    }
    d.update(kw)
    return d


class FakeClient:
    """只回预设结果, 并记录收到的请求。"""

    def __init__(self, results):
        self._results = list(results)
        self.calls = []
        self.cfg = type("C", (), {"model": "fake"})()

    def messages(self, system, user, max_tokens=None, tool=None,
                 temperature=None, timeout=None, max_retries=None):
        self.calls.append({"system": system, "user": user, "tool": tool,
                           "max_tokens": max_tokens})
        name = (tool or {}).get("name")
        if name == "emit_truth_audit":
            # 审计默认通过(与 test_llm 的 FakeClient 同一约定)。
            return LLMResult(tool_input={
                "__truth_audit__": True, "narrator_truthful": True,
                "mechanism_consistent": True, "conflicts": []}, model="m")
        if not self._results:
            return LLMResult(error="no more canned results")
        return self._results.pop(0)


def _runtime_cfg(**kw):
    from story.config import Config
    kw.setdefault("sim_path", "x")
    kw.setdefault("no_llm", False)
    return Config(**kw)


def _writer(results, **kw):
    from story.llm import PuzzleWriter
    fc = FakeClient(results)
    return PuzzleWriter(client=fc, runtime_cfg=_runtime_cfg(**kw)), fc


def _qc_v8(**kw):
    """`_apply_review` 要求的十二项(curated 走过的门)。

    注意有两层判据, 是**不同**的:
        curated 十三条  -> H2-B/H3 的"这题适不适合搬进直播"(编译期 ACCEPT/REJECT)
        十二项          -> `_apply_review` 对**任何** spec 的交付门
                            (narrator_truthful / … / 题型四问)
    curated 题同样要过后者 —— 它复用同一套审稿人, 不是旁路。

    ⚠️ H3-D3 起, 后四项(题型四问)也在 `_apply_review` 的 fail-closed
    清单里 —— 因为 `story_review` 那次**独立调用已被并进审稿**(§一-2)。
    所以这里的默认必须带上它们, 否则每一稿都会卡在
    "quality_checks 未全过(story_reconstruction, …)"。
    """
    d = {"narrator_truthful": True, "mechanism_consistent": True,
         "core_answer_direct": True, "completion_contract_minimal": True,
         "concrete_anomaly": True, "clue_recontextualized": True,
         "dramatic_payoff": True, "reasoning_beats_nonredundant": True,
         # H3-D3: 题型四问(同一次审稿回复里的附带字段)。
         "story_reconstruction": True, "multi_step_deduction": True,
         "single_trick": False, "no_external_knowledge_dependency": True}
    d.update(kw)
    return d


def _sig_with_observed(**kw):
    """审稿 observed_signature —— 必须带 v4 的两把键, 否则 `_apply_review` 拒。"""
    d = dict(_compile_tool()["observed_signature"])
    d.setdefault("reveal_mode", "meaning_flip")
    d.setdefault("procedural_rule_dependency", False)
    d.update(kw)
    return d


def _review_pass(**kw):
    """审稿 pass —— 原样回传整套 bundle(与 `_apply_review` 的要求一致)。

    `quality_checks=...` 可以整体替换那十二项(测题型四问时用)。
    """
    d = _compile_tool()
    qc = kw.pop("quality_checks", None) or _qc_v8()
    d.update({"decision": "pass", "observed_signature": _sig_with_observed(),
              "quality_checks": qc})
    d.update(kw)
    return LLMResult(tool_input=d, model="m")


def _review_with_story_gate(**story_kw):
    """审稿回复: **结构全过, 但题型四问按给定值**。

    H3-D3 起题型问就藏在这份 `quality_checks` 里, 所以"复核拒稿"必须
    由审稿**那一次**答复表达。十八楼/吉普车那类题的真实形态正是这样:
    编译侧十三条全填"没问题", 只有这四项看得出它是 single_trick。
    """
    return _review_pass(quality_checks=_qc_v8(**story_kw))


# ======================================================================
# H3-D §六: 复核是**独立**的判断, 且必须真的生效
# ======================================================================
def test_story_review_rejection_blocks_compilation():
    """**复核拒稿必须真的拦下编译** —— 哪怕编译侧十三条全过。

    这条是 M9 变体逼出来的: 早先的测试全部用"默认通过的复核", 于是
    "把复核结果丢掉"(`sgr2 = []`)这种 mutant **不会被发现**。测试
    全绿, 而生产里十八楼那道题照进不误。

    断言三件事:
      1. 走到了复核那一步(说明它确实在管道里)
      2. **没有 spec**(被拦下了)
      3. stage 标 `story_review`(**不是** story_gate —— 两者是
         不同的门, 报告要分得开)
    """
    print("\n[H3-D] 复核拒稿 -> 编译不出 spec")
    # H3-D3: 复核不再是**独立的一次调用** —— 它就藏在审稿那次回复的
    # `quality_checks` 后四项里。所以这里构造一份"审稿判定:
    # 结构都过、但题型不合格"的回复。
    w, fc = _writer([LLMResult(tool_input=_compile_tool(), model="m"),
                     _review_with_story_gate(
                         story_reconstruction=True,
                         multi_step_deduction=False,
                         single_trick=True)])
    from tools.curated_compiler import CuratedCompiler
    spec, info = CuratedCompiler(w).compile_one(mk_rec(), recent=[])
    check("**没有 spec**", spec is None, info)
    check("**stage 是 story_review**",
          info.get("stage") == "story_review", info.get("stage"))
    check("原因点名 single_trick",
          "single_trick" in (info.get("reject_reasons") or []), info)
    names = [(c["tool"] or {}).get("name") for c in fc.calls]
    check("**没有第四次 LLM**(题型问在审稿里)",
          names.count("emit_review") == 1, names)
    check("**没有任何独立的复核调用**",
          "review_story_gate" not in names, names)


def test_review_missing_story_fields_blocks_compilation():
    """审稿回复里**没有**题型四问 -> fail closed(不放过)。

    这正是 §一-2 合并之后要守住的那条: 复核并进审稿**不等于**取消
    复核。审稿没回答这四个问题, 与"复核调用失败"是同一种情况 ——
    都不能让它进池。

    而且它必须落成 `technical_defer`(可重试), **不是** `rejected`
    (终态) —— 见 §一-3: 网关抖一下不该永久吃掉一道题。
    """
    print("\n[H3-D3] 审稿缺题型四问 -> fail closed + technical_defer")
    qc = _qc_v8()
    for k in ("story_reconstruction", "multi_step_deduction",
              "single_trick", "no_external_knowledge_dependency"):
        qc.pop(k)
    w, fc = _writer([LLMResult(tool_input=_compile_tool(), model="m"),
                     _review_pass(quality_checks=qc)])
    from tools.curated_compiler import CuratedCompiler
    spec, info = CuratedCompiler(w).compile_one(mk_rec(), recent=[])
    check("**没有 spec**", spec is None, info)
    check("stage 是 story_review",
          info.get("stage") == "story_review", info.get("stage"))
    check("原因点名复核缺失",
          "story_review_missing" in (info.get("reject_reasons") or []), info)
    # 这条最要紧: 缺失 == 技术失败 == 可重试。
    from tools.curated_ledger import TECHNICAL_DEFER
    from story.lazy_curator import LazyCurator
    _c = LazyCurator.__new__(LazyCurator)
    _c._budget = 999.0
    d, st, rs = _c._classify(None, info, 1.0)
    check("**判成 technical_defer(可重试)**", d == TECHNICAL_DEFER,
          (d, st, rs))


# ======================================================================
# 1. 铁律: 不许重新创作
# ======================================================================
def test_never_calls_emit_riddle():
    """**最关键的一条**: 编译链绝不调 `emit_riddle`。

    那是"发明一道新题"的工具。curated 的任务是**搬运**, 调它等于让
    模型有机会把原故事改掉 —— 而 H2 明令禁止。

    只要这条红了, 说明有人把编译链接到了出题链上。
    """
    print("\n[H2] **绝不调用 emit_riddle**")
    w, fc = _writer([LLMResult(tool_input=_compile_tool(), model="m"),
                     _review_pass()])
    from tools.curated_compiler import CuratedCompiler
    spec, info = CuratedCompiler(w).compile_one(mk_rec(), recent=[])
    names = [(c["tool"] or {}).get("name") for c in fc.calls]
    check("**没有任何一次 emit_riddle**", "emit_riddle" not in names, names)
    check("用的是 emit_curated", "emit_curated" in names, names)
    check("编译成功", spec is not None, info)
    check("info 标 accepted", info.get("accepted") is True, info)


def test_accepted_path_is_exactly_three_llm_calls():
    """§一-2: 一条 accepted 路径**恰好 3 次** LLM 调用。

        1. emit_curated    (编译)
        2. emit_review     (审稿 —— 题型四问**并进这一次**)
        3. emit_truth_audit(审计)

    ## 这条守的是什么

    H3-D 里"故事复核"是**独立的一次调用**, 于是变成 4 次。任务书
    H3-D3 §一-2 明确要求降到 3 次。这条测试把次数**钉死**:
    将来谁再加一次调用, 它立刻变红。

    ⚠️ 也顺手断言"没有任何独立的复核工具" —— 复核并进来之后,
    `review_story_gate` 这个名字不该再出现在工具调用里。
    """
    print("\n[H3-D3] accepted 路径恰好 3 次 LLM 调用")
    w, fc = _writer([LLMResult(tool_input=_compile_tool(), model="m"),
                     _review_pass()])
    from tools.curated_compiler import CuratedCompiler
    spec, info = CuratedCompiler(w).compile_one(mk_rec(), recent=[])
    check("编译成功", spec is not None, info)
    names = [(c["tool"] or {}).get("name") for c in fc.calls]
    check("**恰好 3 次调用**", len(names) == 3, names)
    check("**顺序是 compile -> review -> audit**",
          names == ["emit_curated", "emit_review", "emit_truth_audit"],
          names)
    check("**没有独立的复核调用**",
          "review_story_gate" not in names, names)


def test_prompt_carries_canonical_source():
    """请求里必须**带上原文**, 并显式声明"这是已有题目"。"""
    print("\n[H2] 请求带 canonical 原文 + 明确'不要创作'")
    w, fc = _writer([LLMResult(tool_input=_compile_tool(), model="m"),
                     _review_pass()])
    from tools.curated_compiler import CuratedCompiler
    CuratedCompiler(w).compile_one(mk_rec(), recent=[])
    first = fc.calls[0]
    user = first["user"]
    check("**谜面原文进了请求**", _PUZ in user)
    check("**谜底原文进了请求**", _ANS in user)
    check("标注了 canonical", "canonical" in user.lower(), user[:200])
    check("**明确'不要重新创作'**",
          "编辑" in user and ("不是出题人" in user or "不得改变" in user),
          user[-200:])
    check("带了来源名", "Puzzling Stack Exchange" in user)
    check("带了原帖链接", "puzzling.stackexchange.com" in user)
    # system prompt 里也要有铁律
    sys_p = first["system"]
    check("system 明令禁止改写核心真相", "改写核心真相" in sys_p)
    check("system 明令禁止添加关键因果", "添加新的关键因果" in sys_p)
    check("system 明令禁止编造线索", "编造" in sys_p)
    check("system 说明可以翻译", "翻译" in sys_p)


def test_english_flagged_for_translation():
    """英文原题要提示翻译 + 词形/双关无法翻时 reject。"""
    print("\n[H2-C] 英文题提示翻译")
    w, fc = _writer([LLMResult(tool_input=_compile_tool(), model="m"),
                     _review_pass()])
    from tools.curated_compiler import CuratedCompiler
    CuratedCompiler(w).compile_one(mk_rec(original_language="en"), recent=[])
    user = fc.calls[0]["user"]
    check("提示翻译成中文", "翻译成中文" in user)
    check("提示事实一一对应", "一一对应" in user)
    check("**提示 language_dependent 可拒**",
          "language_dependent" in user, user[-300:])


def test_chinese_source_not_told_to_translate():
    print("\n[H2-C] 中文题不要求翻译")
    w, fc = _writer([LLMResult(tool_input=_compile_tool(), model="m"),
                     _review_pass()])
    from tools.curated_compiler import CuratedCompiler
    CuratedCompiler(w).compile_one(
        mk_rec(original_language="zh", language="zh"), recent=[])
    user = fc.calls[0]["user"]
    check("没有要求翻译", "翻译成中文" not in user)


# ======================================================================
# 2. AI 审题门(H2-B)
# ======================================================================
def test_ai_gate_rejects_on_false_check():
    """十二条里任意一条不合格 -> 拒(fail closed)。

    ⚠️ `single_trick` 方向相反: 它的**不合格**取值是 `True`。
    用同一句 `qc[k] = False` 去试它, 恰好是**合格**取值 —— 那种测试
    会永远绿, 而它本该是最关键的一条。
    """
    print("\n[H2-B] 十二条判据逐条 fail closed")
    for k in CC.CURATED_CHECKS_V2:
        qc = _qc_v2()
        qc[k] = True if k in CC._INVERTED_CHECKS else False
        ok, why = CC.check_tool_result({"accepted": True,
                                        "quality_checks": qc})
        check(f"{k} 不合格 -> 拒", not ok, why)


def test_ai_gate_requires_all_checks():
    print("\n[H2-B] 十二条缺一不可")
    check("十二条齐全且全合格 -> 过",
          CC.check_tool_result({"accepted": True,
                                "quality_checks": _qc_v2()})[0])
    # 缺一条
    qc = _qc_v2()
    del qc["livestream_safe"]
    ok, why = CC.check_tool_result({"accepted": True, "quality_checks": qc})
    check("**缺一条 -> 拒**", not ok, why)
    # 完全没有 quality_checks
    ok, why = CC.check_tool_result({"accepted": True})
    check("**没有 quality_checks -> 拒**", not ok, why)
    # 不是 dict
    ok, why = CC.check_tool_result({"accepted": True, "quality_checks": []})
    check("quality_checks 类型错 -> 拒", not ok, why)


def test_ai_gate_rejects_explicit_reject():
    print("\n[H2-B] accepted=false -> 拒并带回理由")
    ok, why = CC.check_tool_result(
        {"accepted": False, "reject_reasons": ["language_dependent"]})
    check("拒", not ok)
    check("理由被带回", "language_dependent" in " ".join(why), why)
    # accepted=false 但没给理由 -> 仍然拒
    ok, why = CC.check_tool_result({"accepted": False})
    check("**没给理由也拒**", not ok, why)


def test_ai_gate_is_fail_closed_on_self_contradiction():
    """accepted=true 但某条 false(自相矛盾)-> 以 quality_checks 为准, 拒。

    这是最危险的一种: 模型嘴上说"行", 逐条判据里却写着"没有反转"。
    信那个总结布尔就会把一道没有反转的题放进池子 —— 正是本批要消灭的。
    """
    print("\n[H2-B] **自相矛盾时以逐条判据为准**")
    qc = _qc_v2()
    qc["has_reversal"] = False
    ok, why = CC.check_tool_result({"accepted": True, "quality_checks": qc})
    check("**拒(不信 accepted)**", not ok, why)
    check("点名 has_reversal", "has_reversal" in " ".join(why), why)


def test_explicit_ai_reject_does_not_retry():
    """AI 明确拒收 -> **不重试**(重试只会让它换个说法硬凑 accepted)。"""
    print("\n[H2-B] AI 明确拒收不重试")
    w, fc = _writer([LLMResult(tool_input={
        "accepted": False, "reject_reasons": ["not_a_story"]}, model="m")])
    from tools.curated_compiler import CuratedCompiler
    spec, info = CuratedCompiler(w).compile_one(mk_rec(), recent=[],
                                                max_attempts=3)
    check("没收", spec is None)
    check("**只调了一次**(没重试)", len(fc.calls) == 1, len(fc.calls))
    check("理由带回", "not_a_story" in " ".join(info["reject_reasons"]),
          info)
    # H3-A 后 stage 仍是 ai_gate: 模型**自己**说不行时, 采信它给的原因
    # (它比故事门精准 —— 故事门对一份没有 quality_checks 的回复只能说
    # "三条都不合格")。故事门负责的是"模型说行、其实不行"那种。
    check("stage 标 ai_gate", info["stage"] == "ai_gate", info)


def test_self_contradiction_retries_then_gives_up():
    """自相矛盾 -> 重试(多半是漏填), 但重试耗尽仍要拒。"""
    print("\n[H2-B] 自相矛盾重试到上限仍拒")
    bad_qc = _qc_v2()
    bad_qc.pop("livestream_safe", None)     # 用"缺一项"更贴近真实漏填
    w, fc = _writer([
        LLMResult(tool_input={"accepted": True, "quality_checks": bad_qc},
                  model="m"),
        LLMResult(tool_input={"accepted": True, "quality_checks": bad_qc},
                  model="m"),
    ])
    from tools.curated_compiler import CuratedCompiler
    spec, info = CuratedCompiler(w).compile_one(mk_rec(), recent=[],
                                                max_attempts=2)
    check("最终没收", spec is None)
    check("重试了 2 次", len(fc.calls) == 2, len(fc.calls))


# ======================================================================
# 3. H2-E: fair_clue 与谜面的关系
# ======================================================================
def test_fair_clue_must_be_verbatim_from_puzzle():
    print("\n[H2-E] fair_clue 必须逐字出自谜面")
    spec = CC.spec_from_tool(_compile_tool(), mk_rec(), None)
    ok, why = CC.validate_curated(spec)
    check("正常 fixture 通过", ok, why)
    # quote 不在谜面(mimic 从谜底倒灌)
    d = _compile_tool()
    d["fair_clues"] = [{"quote": "退潮时礁石露出水面",
                        "supports_atoms": ["a1"]}]      # 这是谜底里的话!
    spec2 = CC.spec_from_tool(d, mk_rec(), None)
    ok2, why2 = CC.validate_curated(spec2)
    check("**从谜底倒灌的 quote 被拒**", not ok2, why2)
    check("理由点明不得改谜面",
          any("不得改谜面" in w or "不在谜面里" in w for w in why2), why2)


def test_missing_fair_clue_rejected():
    print("\n[H2-E] 没有 fair_clue -> 拒(不倒灌)")
    d = _compile_tool()
    d["fair_clues"] = []
    spec = CC.spec_from_tool(d, mk_rec(), None)
    ok, why = CC.validate_curated(spec)
    check("**拒**", not ok, why)
    check("理由提到应 reject",
          any("reject" in w for w in why), why)


def test_provenance_required():
    """没有 curated provenance -> 拒(防调用方拿它跑自由生成)。"""
    print("\n[H2-F] provenance 必须是 curated")
    spec = CC.spec_from_tool(_compile_tool(), mk_rec(), None)
    check("正常题 source_type=curated", spec.source_type == "curated")
    spec.source_type = ""
    ok, why = CC.validate_curated(spec)
    check("**清了 source_type -> 拒**", not ok, why)


# ======================================================================
# 4. provenance 注入(H2-F/H2-H)
# ======================================================================
def test_spec_gets_full_provenance():
    print("\n[H2-F/H] 编译产物带完整 provenance")
    rec = mk_rec()
    spec = CC.spec_from_tool(_compile_tool(), rec, None)
    check("source_type", spec.source_type == "curated", spec.source_type)
    check("external_source", spec.external_source == rec.source)
    check("external_id", spec.external_id == rec.external_id)
    check("source_url", spec.source_url == rec.source_url)
    check("license", spec.license == "CC BY-SA 4.0", spec.license)
    check("**answer_license 单独存**",
          spec.answer_license == "CC BY-SA 3.0", spec.answer_license)
    at = spec.attribution or {}
    check("attribution 有 question_author",
          at.get("question_author") == "问者", at)
    check("attribution 有 answer_author",
          at.get("answer_author") == "答者", at)
    check("attribution 有 question_license",
          at.get("question_license") == "CC BY-SA 4.0", at)
    check("attribution 有 answer_license",
          at.get("answer_license") == "CC BY-SA 3.0", at)
    check("attribution 标 modified=True", at.get("modified") is True, at)
    check("**英文题标了翻译**",
          "translation" in str(at.get("modification", "")).lower(), at)
    check("style_tags 带出来",
          spec.style_tags == ["object_meaning", "causal_flip"],
          spec.style_tags)


def test_chinese_source_modification_note():
    print("\n[H2-H] 中文题不标 translation")
    spec = CC.spec_from_tool(_compile_tool(),
                             mk_rec(original_language="zh"), None)
    note = str((spec.attribution or {}).get("modification", "")).lower()
    check("modification 里没有 translation", "translation" not in note, note)
    check("但仍有 structural compilation", "structural" in note, note)


def test_prompt_version_and_policy():
    print("\n[H2] 版本标签")
    spec = CC.spec_from_tool(_compile_tool(), mk_rec(), None)
    check("prompt_version = curated-v1",
          spec.prompt_version == CC.CURATED_PROMPT_VERSION,
          spec.prompt_version)
    check("quality_policy_version = 当前政策",
          spec.quality_policy_version == QUALITY_POLICY_VERSION,
          spec.quality_policy_version)


# ======================================================================
# 5. PuzzleSpec 溯源往返(进 archive, 不进前端)
# ======================================================================
def test_provenance_survives_archive_roundtrip():
    print("\n[H2-F] provenance 过 archive 往返不丢")
    spec = CC.spec_from_tool(_compile_tool(), mk_rec(), None)
    back = PuzzleSpec.from_dict(spec.to_archive())
    check("source_type", back.source_type == "curated")
    check("external_id", back.external_id == "pse:q:999")
    check("license", back.license == "CC BY-SA 4.0")
    check("answer_license", back.answer_license == "CC BY-SA 3.0")
    check("attribution 完整",
          back.attribution.get("answer_author") == "答者", back.attribution)
    check("style_tags 完整", back.style_tags == spec.style_tags)


def test_legacy_spec_has_empty_provenance():
    """老 archive(没这些键)必须照读, 且默认是"非 curated"。"""
    print("\n[H2-F] 老 archive 宽容读入")
    old = PuzzleSpec.from_dict({"puzzle": "旧题面", "answer": "旧谜底"})
    check("source_type 空", old.source_type == "")
    check("external_id 空", old.external_id == "")
    check("attribution 空 dict", old.attribution == {})
    check("style_tags 空 list", old.style_tags == [])
    check("**空 source_type != curated**", old.source_type != "curated")


def test_provenance_not_in_repr_of_snapshot():
    """provenance **不下发前端** —— 守住这条不让它溜进 Snapshot。"""
    print("\n[H2-F] provenance 不进前端 Snapshot")
    from story.state import Snapshot
    fields = set(getattr(Snapshot, "__dataclass_fields__", {}).keys())
    for banned in ("attribution", "external_id", "external_source",
                   "source_url", "license", "style_tags", "source_type"):
        check(f"Snapshot 没有 {banned}", banned not in fields)


# ======================================================================
# 6. 编译循环
# ======================================================================
def test_compile_calls_reviewer_and_audit():
    """编译链要真的走审稿 + truth audit(复用现有门)。"""
    print("\n[H2-D] 编译走完整质量链")
    w, fc = _writer([LLMResult(tool_input=_compile_tool(), model="m"),
                     _review_pass()])
    from tools.curated_compiler import CuratedCompiler
    spec, info = CuratedCompiler(w).compile_one(mk_rec(), recent=[])
    names = [(c["tool"] or {}).get("name") for c in fc.calls]
    check("调了 emit_curated", "emit_curated" in names, names)
    check("**调了 emit_review(审稿)**", "emit_review" in names, names)
    check("**调了 emit_truth_audit**", "emit_truth_audit" in names, names)
    check("成功", spec is not None, info)


def test_compile_rejects_when_clue_not_in_puzzle():
    """模型给了不在谜面里的 quote -> 编译失败(且不重试到天荒地老)。"""
    print("\n[H2-E] quote 不在谜面 -> 编译失败")
    bad = _compile_tool()
    bad["fair_clues"] = [{"quote": "这句话谜面里根本没有",
                          "supports_atoms": ["a1"]}]
    w, fc = _writer([LLMResult(tool_input=bad, model="m")])
    from tools.curated_compiler import CuratedCompiler
    spec, info = CuratedCompiler(w).compile_one(mk_rec(), recent=[],
                                                max_attempts=3)
    check("没收", spec is None)
    check("**没有一直重试**(结构性错误)", len(fc.calls) == 1, len(fc.calls))
    check("stage 标 curated_validate",
          info["stage"] == "curated_validate", info)


def test_compile_handles_empty_tool_input():
    print("\n[H2] 空 tool_input -> 重试, 不崩")
    w, fc = _writer([LLMResult(error="网关抖动"),
                     LLMResult(tool_input=_compile_tool(), model="m"),
                     _review_pass()])
    from tools.curated_compiler import CuratedCompiler
    spec, info = CuratedCompiler(w).compile_one(mk_rec(), recent=[],
                                                max_attempts=2)
    check("第二稿成功", spec is not None, info)


def test_unwrap_nested_tool_input():
    print("\n[H2] 兼容被包一层的 tool_input")
    inner = _compile_tool()
    check("直接给", CC._unwrap(inner) is inner)
    check("包在 input 里", CC._unwrap({"input": inner}) == inner)
    check("包在 arguments 里", CC._unwrap({"arguments": inner}) == inner)
    check("非 dict -> 空", CC._unwrap(None) == {})
    # 不误伤: 本来就是平铺的 dict
    flat = {"accepted": True}
    check("平铺不变", CC._unwrap(flat) == flat)


def test_curated_rejected_when_cross_gate_blocks():
    """跨题门拒 -> 没收(且不该崩)。"""
    print("\n[H2-D] 跨题门生效")
    w, fc = _writer([LLMResult(tool_input=_compile_tool(), model="m"),
                     _review_pass()])
    from tools.curated_compiler import CuratedCompiler
    from story.puzzle import PuzzleSignature
    # 造一个与本题**同结构**的 recent -> 结构重复
    dup = PuzzleSignature(mechanism_family="hidden_function",
                          solution_shape="hidden_function_explains_behavior",
                          domain="maritime", relation="stranger",
                          emotion_mode="neutral", time_shape="instant")
    spec, info = CuratedCompiler(w).compile_one(mk_rec(), recent=[dup])
    check("被跨题门拦下", spec is None, info)
    check("stage 标 cross_gate", info["stage"] == "cross_gate", info)


def test_provenance_survives_reviewer():
    """**审稿重建 spec 时必须带过 curated 溯源** —— 真实踩到的 bug。

    `_apply_review` 是**重建**一个 `PuzzleSpec`, 任何没显式列出的字段都会
    静默回到默认值。早先它只带 `blueprint_specified`/`metrics`/`usage`/
    `model`, 于是 curated 的 provenance 一过审就全空:

        -> `validate_curated` 因 source_type 为空拒稿(白编译一次)
        -> **版权署名整批消失**(法律层面的问题, 不只是数据问题)

    后果比 blueprint_specified 那次更重, 所以单独钉一条。
    """
    print("\n[H2-F] **审稿重建 spec 不丢 curated 溯源**")
    w, fc = _writer([LLMResult(tool_input=_compile_tool(), model="m"),
                     _review_pass()])
    from tools.curated_compiler import CuratedCompiler
    spec, info = CuratedCompiler(w).compile_one(mk_rec(), recent=[])
    check("编译成功(没被 provenance 检查拒)", spec is not None, info)
    if spec is None:
        return
    check("**过审后 source_type 仍是 curated**",
          spec.source_type == "curated", spec.source_type)
    check("external_id 还在", spec.external_id == "pse:q:999",
          spec.external_id)
    check("**license 还在**", spec.license == "CC BY-SA 4.0", spec.license)
    check("**answer_license 还在**",
          spec.answer_license == "CC BY-SA 3.0", spec.answer_license)
    check("**attribution 还在(署名不能丢)**",
          (spec.attribution or {}).get("answer_author") == "答者",
          spec.attribution)
    check("style_tags 还在", spec.style_tags == ["object_meaning",
                                                 "causal_flip"],
          spec.style_tags)
    check("source_url 还在", "puzzling.stackexchange.com" in spec.source_url,
          spec.source_url)


def test_stage_reports_deepest_gate():
    """stage 要报**走到过的最深一道门**, 而不是最后一稿死在哪。

    实测场景: 第 1 稿一路走到跨题门被拒(题本身没问题, 是分布重复),
    第 2 稿因网关抖动在 compile_call 就挂了。若 stage 取"最后一次",
    报告会把一个**分布问题**记成**网关问题** —— 运维照着这个数字去查
    网络, 方向完全错了。
    """
    print("\n[H2] stage 报最深门(不被后续抖动覆盖)")
    from tools.curated_compiler import CuratedCompiler
    from story.puzzle import PuzzleSignature
    w, fc = _writer([LLMResult(tool_input=_compile_tool(), model="m"),
                     _review_pass()])
    dup = PuzzleSignature(mechanism_family="hidden_function",
                          solution_shape="hidden_function_explains_behavior",
                          domain="maritime", relation="stranger",
                          emotion_mode="neutral", time_shape="instant")
    # 第 1 稿走到 cross_gate; 第 2 稿队列空 -> compile_call
    spec, info = CuratedCompiler(w).compile_one(mk_rec(), recent=[dup],
                                                max_attempts=2)
    check("没收", spec is None)
    check("**stage 是 cross_gate(不是 compile_call)**",
          info["stage"] == "cross_gate", info)


def test_stage_does_not_regress_to_shallower():
    print("\n[H2] stage 单调不后退")
    from tools.curated_compiler import _pick_stage
    check("深覆盖浅", _pick_stage("cross_gate", "compile_call")
          == "cross_gate")
    check("更深则更新", _pick_stage("validate", "review") == "review")
    check("同级取新", _pick_stage("review", "review_technical")
          == "review_technical")
    check("空则取新", _pick_stage("", "ai_gate") == "ai_gate")


def test_build_user_prompt_injects_blueprint():
    print("\n[H2] blueprint 前进请求")
    from story.puzzle import PuzzleBlueprint
    bp = PuzzleBlueprint(mechanism_family="hidden_function",
                         solution_shape="hidden_function_explains_behavior")
    user = CC.build_user_prompt(mk_rec(), target_blueprint=bp)
    check("印出了 blueprint 段", "Blueprint" in user)
    check("印出了 mechanism_family", "hidden_function" in user)
    user2 = CC.build_user_prompt(mk_rec())
    check("不给 blueprint 时没有那一段", "Blueprint" not in user2)


# ======================================================================
def main():
    tests = [
        # 铁律
        test_never_calls_emit_riddle,
        # H3-D §六: 复核
        test_story_review_rejection_blocks_compilation,
        test_review_missing_story_fields_blocks_compilation,
        test_accepted_path_is_exactly_three_llm_calls,
        test_prompt_carries_canonical_source,
        test_english_flagged_for_translation,
        test_chinese_source_not_told_to_translate,
        # AI 门
        test_ai_gate_rejects_on_false_check,
        test_ai_gate_requires_all_checks,
        test_ai_gate_rejects_explicit_reject,
        test_ai_gate_is_fail_closed_on_self_contradiction,
        test_explicit_ai_reject_does_not_retry,
        test_self_contradiction_retries_then_gives_up,
        # H2-E
        test_fair_clue_must_be_verbatim_from_puzzle,
        test_missing_fair_clue_rejected,
        test_provenance_required,
        # provenance
        test_spec_gets_full_provenance,
        test_chinese_source_modification_note,
        test_prompt_version_and_policy,
        test_provenance_survives_archive_roundtrip,
        test_legacy_spec_has_empty_provenance,
        test_provenance_not_in_repr_of_snapshot,
        test_provenance_survives_reviewer,
        test_stage_reports_deepest_gate,
        test_stage_does_not_regress_to_shallower,
        test_build_user_prompt_injects_blueprint,
        # 编译循环
        test_compile_calls_reviewer_and_audit,
        test_compile_rejects_when_clue_not_in_puzzle,
        test_compile_handles_empty_tool_input,
        test_unwrap_nested_tool_input,
        test_curated_rejected_when_cross_gate_blocks,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:                  # noqa: BLE001
            import traceback
            print(f"  FAIL {t.__name__} 抛异常: {e}")
            traceback.print_exc()
            FAIL[0] += 1
    print()
    if FAIL[0]:
        print(f"FAILED: {FAIL[0]}")
        return 1
    print(f"ALL PASS ({len(tests)} tests)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
