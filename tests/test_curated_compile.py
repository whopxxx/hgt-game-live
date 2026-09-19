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


def _compile_tool(**kw):
    """一份**能过全部确定性门**的编译结果。"""
    body = _PUZ.rstrip("?？")
    d = {
        "accepted": True,
        "quality_checks": {k: True for k in CC.CURATED_CHECKS},
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
    """`_apply_review` 要求的 v8 八项(与 curated 的九条是**两套**) 。

    注意这两套判据是**不同**的:
        curated 九条  -> H2-B 的"这题适不适合搬进直播"(ACCEPT/REJECT)
        v8 八项       -> `_apply_review` 对**任何** spec 的交付门
                          (narrator_truthful / mechanism_consistent / …)
    curated 题同样要过后者 —— 它复用同一套审稿人, 不是旁路。
    """
    d = {"narrator_truthful": True, "mechanism_consistent": True,
         "core_answer_direct": True, "completion_contract_minimal": True,
         "concrete_anomaly": True, "clue_recontextualized": True,
         "dramatic_payoff": True, "reasoning_beats_nonredundant": True}
    d.update(kw)
    return d


def _sig_with_observed(**kw):
    """审稿 observed_signature —— 必须带 v4 的两把键, 否则 `_apply_review` 拒。"""
    d = dict(_compile_tool()["observed_signature"])
    d.setdefault("reveal_mode", "meaning_flip")
    d.setdefault("procedural_rule_dependency", False)
    d.update(kw)
    return d


def _review_pass():
    """审稿 pass —— 原样回传整套 bundle(与 `_apply_review` 的要求一致)。"""
    d = _compile_tool()
    d.update({"decision": "pass", "observed_signature": _sig_with_observed(),
              "quality_checks": _qc_v8()})
    return LLMResult(tool_input=d, model="m")


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
    """九条里任意一条 false -> 拒。"""
    print("\n[H2-B] 九条判据逐条 fail closed")
    for k in CC.CURATED_CHECKS:
        d = {"accepted": True,
             "quality_checks": {x: True for x in CC.CURATED_CHECKS}}
        d["quality_checks"][k] = False
        ok, why = CC.check_tool_result(d)
        check(f"{k}=false -> 拒", not ok and k in " ".join(why), why)


def test_ai_gate_requires_all_nine():
    print("\n[H2-B] 九条缺一不可")
    check("九条齐全且全 true -> 过",
          CC.check_tool_result({"accepted": True, "quality_checks":
                                {k: True for k in CC.CURATED_CHECKS}})[0])
    # 缺一条
    qc = {k: True for k in CC.CURATED_CHECKS}
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
    qc = {k: True for k in CC.CURATED_CHECKS}
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
    check("stage 标 ai_gate", info["stage"] == "ai_gate", info)


def test_self_contradiction_retries_then_gives_up():
    """自相矛盾 -> 重试(多半是漏填), 但重试耗尽仍要拒。"""
    print("\n[H2-B] 自相矛盾重试到上限仍拒")
    bad_qc = {k: True for k in CC.CURATED_CHECKS}
    bad_qc["dramatic_payoff"] = False
    bad_qc.pop("dramatic_payoff", None)     # 用"缺一项"更贴近真实漏填
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
        test_prompt_carries_canonical_source,
        test_english_flagged_for_translation,
        test_chinese_source_not_told_to_translate,
        # AI 门
        test_ai_gate_rejects_on_false_check,
        test_ai_gate_requires_all_nine,
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
