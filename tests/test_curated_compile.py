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
    """审稿回复的 `quality_checks` —— **curated 题走过的那一套**。

    ⚠️ H4-D1 §二: curated 的门**不再是** `concrete_anomaly` /
    `dramatic_payoff` 那六项(那是 H4-D 第一版的假映射, 见
    `story.llm._CURATED_HARD_CHECK_FIELDS`), 而是与编译侧**同名**的
    六条内容门 + 冷知识门 + 两条真实性, 共九项。

    ⚠️ 这份 fixture 必须与 `story.llm._CURATED_HARD_CHECK_FIELDS`
    **逐字一致** —— 少一项, `_apply_review` 会因为"缺字段"拒稿, 而这
    正是 H4-D1 之前 12 条测试同时红掉的原因。测试
    `test_curated_reviewer_contract_is_semantically_honest` 守这条。

    信号五项(`dramatic_payoff` / `reasoning_beats_nonredundant` /
    `story_reconstruction` / `multi_step_deduction` / `single_trick`)
    **也给**, 因为真实回复会给; 但它们是信号, 取任何值都不拒稿。
    """
    d = {"clear_anomaly": True, "unique_explanation": True,
         "yes_no_progress": True, "no_obscure_system": True,
         "no_external_media": True, "livestream_safe": True,
         "no_external_knowledge_dependency": True,
         "narrator_truthful": True, "mechanism_consistent": True,
         # ---- 信号(给全, 但取什么都不影响准入) ----
         "dramatic_payoff": True, "reasoning_beats_nonredundant": True,
         "story_reconstruction": True, "multi_step_deduction": True,
         "single_trick": False}
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
def test_story_review_signal_does_not_block_compilation():
    """**H4-D §三/§六: 复核的题型信号必须**不再**拦下编译。**

    ⚠️ 这条**反转**了 v4 的
    `test_story_review_rejection_blocks_compilation`。那是对的 ——
    政策变了, 测试跟着产品走(任务书 §五)。

    v4 里 Reviewer 说 `single_trick=true` 就整稿被拒, 于是十八楼那种
    "轻量直播竞猜"永远进不来。v5 起它只是一个**信号**。

    断言三件事:
      1. **有 spec**(没被拦下) —— 这是 v5 的核心;
      2. 信号**确实被记下了**(降级 != 丢弃) —— §十二 以后要排序;
      3. 仍然没有第四次 LLM(题型问在审稿里, H3-D3 的预算契约不变)。
    """
    print("\n[H4-D §三] 复核信号不再拦下编译")
    w, fc = _writer([LLMResult(tool_input=_compile_tool(), model="m"),
                     _review_with_story_gate(
                         story_reconstruction=False,
                         multi_step_deduction=False,
                         single_trick=True,
                         no_external_knowledge_dependency=True)])
    from tools.curated_compiler import CuratedCompiler
    spec, info = CuratedCompiler(w).compile_one(mk_rec(), recent=[])
    check("**有 spec(信号不拦题)**", spec is not None, info)
    # 信号必须记下来 —— 否则"降级"就变成了"丢弃", 以后无法排序
    check("**审稿侧信号已记录**",
          "single_trick" in (info.get("review_signal") or []),
          info.get("review_signal"))
    # ⚠️ 编译侧这里是**干净的**(`_compile_tool` 的 qc 里 single_trick=
    # False)。两侧信号**互相独立**正是设计意图: 编译模型和 Reviewer 是
    # 两次独立判断, 合并它们会让"两个独立视角"退化成"一个"。
    check("编译侧独立记录(这份 fixture 里它是干净的)",
          not (info.get("story_signal") or []),
          info.get("story_signal"))
    check("**stage 不是 story_review**(它不再是拒绝 stage)",
          info.get("stage") != "story_review", info.get("stage"))
    names = [(c["tool"] or {}).get("name") for c in fc.calls]
    check("**没有第四次 LLM**(题型问在审稿里)",
          names.count("emit_review") == 1, names)
    check("**没有任何独立的复核调用**",
          "review_story_gate" not in names, names)


def test_review_missing_story_fields_does_not_block_compilation():
    """**H4-D §七: 审稿回复里**没有**题型字段 -> 不拒稿, 也不技术失败。**

    ⚠️ 这条**反转**了 v4 的
    `test_review_missing_story_fields_blocks_compilation`。

    任务书原话:

        故事/反转/层次类字段: 虽可返回, 但不是 pass/fail 条件。
        缺少这些 soft 字段也不要技术失败。

    v4 里缺这四个字段 = 复核缺失 = technical_defer。v5 起它们只是信号,
    不填就是不填 —— 一道题**不该因为 Reviewer 少答了一个风格问题**
    而被判"这次没审成"。

    断言:
      1. **有 spec**;
      2. `story_review_missing` **不出现**在 reject_reasons 里;
      3. 没有任何信号被记成"负面"(缺项 = 无信号)。
    """
    print("\n[H4-D §七] 审稿缺题型字段 -> 不拒稿, 不技术失败")
    qc = _qc_v8()
    # ⚠️ 只去掉**纯信号**那三个。`no_external_knowledge_dependency` 是
    # §四 的**硬门**, 去掉它本来就该拒 —— 把它一起 pop 掉会让这条测试
    # 变成"门少了才算过", 那是与 §七 相反的结论。
    for k in ("story_reconstruction", "multi_step_deduction",
              "single_trick"):
        qc.pop(k)
    w, fc = _writer([LLMResult(tool_input=_compile_tool(), model="m"),
                     _review_pass(quality_checks=qc)])
    from tools.curated_compiler import CuratedCompiler
    spec, info = CuratedCompiler(w).compile_one(mk_rec(), recent=[])
    check("**有 spec(缺信号字段不拦题)**", spec is not None, info)
    check("**story_review_missing 不是拒因**",
          "story_review_missing" not in (info.get("reject_reasons") or []),
          info.get("reject_reasons"))
    check("**没被标成技术失败**", info.get("technical") is not True, info)
    check("缺项 = 无信号(不是负面信号)",
          not (info.get("review_signal") or []),
          info.get("review_signal"))


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
def test_hard_gate_rejects_on_false_check():
    """**H4-D §二: 六条硬门 + 第13条**逐条 fail closed。

    ⚠️ 范围从"十二条"缩到**七条** —— 这正是 v5 的政策。剩下的六条
    (`not_pure_puzzle` / `has_reversal` / `detail_recontextualized` /
    故事三问)是**信号**, 它们不合格**不拒题**(由
    `test_soft_signals_are_not_gates` 反向守着)。
    """
    print("\n[H4-D §二] 硬门逐条 fail closed")
    hard = list(CC.CURATED_HARD_CHECKS) + [
        "no_external_knowledge_dependency"]
    for k in hard:
        qc = _qc_v2()
        qc[k] = False
        ok, why = CC.check_tool_result({"accepted": True,
                                        "quality_checks": qc})
        # 第 13 条的 reason 文本用的是短名, 所以只断言"拒了"
        check(f"{k} 不合格 -> 拒", not ok, why)


def test_soft_signals_are_not_gates():
    """**H4-D §三: 信号不合格**不**拒题** —— 与上一条成对。

    ⚠️ 只测上一条(门要拒)是不够的: 把整份清单都当门也能让上一条绿。
    这一条测的是反方向 —— **信号被排除在门之外**。

    `single_trick` 的方向在这里特别容易搞错: 它的"坏"取值是 `True`,
    所以这里显式用 `True`。
    """
    print("\n[H4-D §三] 信号不合格 -> 不拒题")
    for k in CC.CURATED_SOFT_SIGNALS:
        qc = _qc_v2()
        # `single_trick` 反向(True = 坏), 其余正向(False = 坏)
        qc[k] = True if k in CC._INVERTED_CHECKS else False
        ok, why = CC.check_tool_result({"accepted": True,
                                        "quality_checks": qc})
        check(f"**{k} 不合格 -> 仍然收**", ok, why)


def test_hard_gate_requires_its_checks():
    """硬门**缺一不可**(fail closed); 信号**缺了无所谓**。"""
    print("\n[H4-D §二] 硬门缺一不可 / 信号缺了无所谓")
    check("全部齐全且硬门全合格 -> 过",
          CC.check_tool_result({"accepted": True,
                                "quality_checks": _qc_v2()})[0])
    # 缺一条**硬门**
    for k in CC.CURATED_HARD_CHECKS:
        qc = _qc_v2()
        del qc[k]
        ok, why = CC.check_tool_result({"accepted": True,
                                        "quality_checks": qc})
        check(f"**缺硬门 {k} -> 拒**", not ok, why)
    # 缺一条**信号** -> 仍然过
    for k in CC.CURATED_SOFT_SIGNALS:
        qc = _qc_v2()
        del qc[k]
        ok, why = CC.check_tool_result({"accepted": True,
                                        "quality_checks": qc})
        check(f"**缺信号 {k} -> 仍然过**", ok, why)
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


def test_hard_gate_is_fail_closed_on_self_contradiction():
    """accepted=true 但某条**硬门** false(自相矛盾)-> 以逐条判据为准, 拒。

    这是最危险的一种: 模型嘴上说"行", 逐条判据里却写着"内容不适合直播"。
    信那个总结布尔就会把一道不安全的题放进池子。

    ⚠️ v5 起**信号**的自相矛盾**不拒**(见下 `test_..._signal_...`)。
    这两条合起来才说明"fail closed"的范围被准确收窄了, 而不是被削弱。
    """
    print("\n[H4-D §二] **硬门自相矛盾时以逐条判据为准**")
    qc = _qc_v2()
    qc["livestream_safe"] = False
    ok, why = CC.check_tool_result({"accepted": True, "quality_checks": qc})
    check("**拒(不信 accepted)**", not ok, why)
    check("点名 livestream_safe", "livestream_safe" in " ".join(why), why)


def test_signal_self_contradiction_does_not_reject():
    """**H4-D §三**: 信号与 `accepted` 矛盾时**不拒题**(信号没那个权力)。

    模型 `accepted=true` 而 `has_reversal=false` —— v4 里这是"自相矛盾",
    拒稿。v5 里它**完全正常**: 一道题可以没有反转但仍然很好玩, 而模型
    如实报了"没反转"。这正是政策要允许的情况。
    """
    print("\n[H4-D §三] 信号与 accepted 矛盾 -> 不拒")
    qc = _qc_v2()
    qc["has_reversal"] = False
    qc["story_reconstruction"] = False
    ok, why = CC.check_tool_result({"accepted": True, "quality_checks": qc})
    check("**仍然收**(信号没有否决权)", ok, why)


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


def test_missing_hard_check_rejects_without_retry():
    """缺**硬门** -> 重试(多半是漏填), 但重试耗尽仍要拒。

    ⚠️ 与信号的区别很重要: 硬门缺项**可能**是模型漏填(而不是真判 false),
    所以重试一次划算。信号缺项**不重试也不拒** —— 它压根不重要。

    这条替换了 v4 的 `test_self_contradiction_retries_then_gives_up`
    (那条用的是 `livestream_safe` 缺项, 语义相同, 只是名字与理由更新了)。
    """
    print("\n[H4-D §二] 缺硬门 -> 重试到上限仍拒")
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
    # ⚠️ **一次**, 不是两次。v4 里"缺一条判据"会走"自相矛盾 -> 重试"那条
    # 分支(`continue`), 于是烧掉 max_attempts 次调用。v5 起硬门缺项在
    # `check_tool_result` 里就被判成 `hard_gate` 并**立即返回**(不重试) ——
    # 因为硬门缺项与"模型说行、其实不行"是同一类**结构性**判定: 再怎么
    # 重问, 那六条问的还是同一道题的同一件事。
    #
    # 这条断言把那个预算契约钉死: 一次。改成 2 就说明有人把硬门缺项重新
    # 归到了"重试"那一支, 而那是白烧好题的配额。
    check("**只调了一次**(硬门缺项不重试)", len(fc.calls) == 1,
          len(fc.calls))


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


def test_curated_accepted_even_when_cross_gate_blocks():
    """**产品边界(H4-F)**: 跨题分布**不得**拒绝 external curated。

        题型 / recent-10 / diversity quota 只对 AI 原创链具有硬约束。
        下载获得的 external curated 不得因为题型分布被拒绝。

    内容硬门全过时, 即使 recent 里已有一道**同 mechanism + 同 shape** 的
    题(结构等价 + 配额全满), 也必须 compile **accepted**。

    ⚠️ 这条测试**曾经**断言相反的行为(`被跨题门拦下` / `stage ==
    "cross_gate"`)。那是把 AI 原创链的配额硬门误加到外部题上 ——
    即本次清除的 latent policy bug。断言已按产品边界反转。
    """
    print("\n[H4-F] 跨题分布**不**拒绝 curated")
    w, fc = _writer([LLMResult(tool_input=_compile_tool(), model="m"),
                     _review_pass()])
    from tools.curated_compiler import CuratedCompiler
    from story.puzzle import PuzzleSignature
    # 与本题**同结构**的 recent —— 结构等价 + 配额占满
    dup = PuzzleSignature(mechanism_family="hidden_function",
                          solution_shape="hidden_function_explains_behavior",
                          domain="maritime", relation="stranger",
                          emotion_mode="neutral", time_shape="instant")
    spec, info = CuratedCompiler(w).compile_one(mk_rec(), recent=[dup] * 10)
    check("**仍然 accepted(不被分布拒绝)**", spec is not None, info)
    check("stage 不是 cross_gate", info.get("stage") != "cross_gate", info)
    check("**分布信号被记录(非阻塞 diagnostic)**",
          bool(info.get("diversity_signals")), info.get("diversity_signals"))


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

    实测场景: 第 1 稿一路走到很深的一道门才被拒, 第 2 稿因网关抖动在
    compile_call 就挂了。若 stage 取"最后一次", 报告会把一个**内容问题**
    记成**网关问题** —— 运维照着这个数字去查网络, 方向完全错了。

    ⚠️ 这里**曾经**用 `cross_gate` 当"最深那道门"。跨题分布自 H4-F 起
    **不再是** curated 的门(产品边界: 外部题不因题型分布被拒), 所以改用一个
    **仍然存在**的硬门 —— 文本 near-duplicate(⑧)。
    """
    print("\n[H2] stage 报最深门(不被后续抖动覆盖)")
    from tools.curated_compiler import CuratedCompiler
    w, fc = _writer([LLMResult(tool_input=_compile_tool(), model="m"),
                     _review_pass()])
    # ⑧ 只对带 `puzzle` **属性**的项做检查(dict 不算), 所以用一个
    #    轻量对象带上与本题相同的谜面 -> 触发文本 near-duplicate。
    near_dup = type("R", (), {"puzzle": mk_rec().surface})()
    # 第 1 稿走到 ⑧; 第 2 稿队列空 -> compile_call
    spec, info = CuratedCompiler(w).compile_one(mk_rec(), recent=[near_dup],
                                                max_attempts=2)
    check("没收", spec is None)
    check("**stage 是 too_similar(不是 compile_call)**",
          info["stage"] == "too_similar", info)


def test_stage_does_not_regress_to_shallower():
    print("\n[H2] stage 单调不后退")
    from tools.curated_compiler import _pick_stage
    check("深覆盖浅", _pick_stage("too_similar", "compile_call")
          == "too_similar")
    check("更深则更新", _pick_stage("validate", "review") == "review")
    check("同级取新", _pick_stage("review", "review_technical")
          == "review_technical")
    check("空则取新", _pick_stage("", "ai_gate") == "ai_gate")


def test_h4f_quota_wall_never_rejects_curated():
    """§6-1: 配额墙最满时, 内容硬门全过的 external curated **必须 accepted**。

    逐个维度铺满 recent: mechanism / solution_shape / death / information_gap /
    domain / relation / emotion / reveal_mode / dark-tone —— 一个都不能
    变成 reject 理由。
    """
    print("\n[H4-F] 配额全维铺满 -> 仍 accepted")
    from tools.curated_compiler import CuratedCompiler
    from story.puzzle import PuzzleSignature
    # 与本题完全同签名 -> 同时命中"配额满"与"结构等价"
    same = PuzzleSignature(
        mechanism_family="hidden_function",
        solution_shape="hidden_function_explains_behavior",
        domain="maritime", relation="stranger", emotion_mode="neutral",
        time_shape="instant")
    w, fc = _writer([LLMResult(tool_input=_compile_tool(), model="m"),
                     _review_pass()])
    spec, info = CuratedCompiler(w).compile_one(mk_rec(), recent=[same] * 10)
    check("**配额墙最满 -> 仍 accepted**", spec is not None, info)
    check("没有 reject_reasons",
          not info.get("reject_reasons"), info.get("reject_reasons"))
    check("**分布信号落进 info(非阻塞)**",
          bool(info.get("diversity_signals")), info.get("diversity_signals"))


def test_h4f_same_mechanism_and_shape_still_accepted():
    """§6-2: 同 mechanism + solution 的 external curated 仍可 compile accepted。"""
    print("\n[H4-F] 同 mechanism+shape 仍 accepted")
    from tools.curated_compiler import CuratedCompiler
    from story.puzzle import PuzzleSignature
    same = PuzzleSignature(
        mechanism_family="hidden_function",
        solution_shape="hidden_function_explains_behavior",
        domain="maritime", relation="stranger", emotion_mode="neutral",
        time_shape="instant")
    w, fc = _writer([LLMResult(tool_input=_compile_tool(), model="m"),
                     _review_pass()])
    spec, info = CuratedCompiler(w).compile_one(mk_rec(), recent=[same])
    check("**accepted**(同结构不是拒绝理由)", spec is not None, info)
    check("**signature 完整写入**",
          spec is not None
          and spec.signature.mechanism_family == "hidden_function"
          and spec.signature.solution_shape
          == "hidden_function_explains_behavior",
          getattr(spec, "signature", None))


def test_h4f_death_gap_domain_saturated_still_accepted():
    """§6-3: death / information_gap / domain 配额满时仍 accepted。"""
    print("\n[H4-F] death/gap/domain 配额满 -> 仍 accepted")
    from tools.curated_compiler import CuratedCompiler
    from story.puzzle import PuzzleSignature
    walls = {
        "death": PuzzleSignature(
            mechanism_family="causal_reversal",
            solution_shape="causal_reversal", domain="nature",
            relation="family", emotion_mode="grief", time_shape="instant",
            death=True),
        "information_gap": PuzzleSignature(
            mechanism_family="information_gap",
            solution_shape="information_advantage", domain="daily",
            relation="colleague", emotion_mode="tense",
            time_shape="instant"),
        "domain": PuzzleSignature(
            mechanism_family="object_misuse",
            solution_shape="misunderstood_object", domain="maritime",
            relation="stranger", emotion_mode="neutral",
            time_shape="instant"),
    }
    for label, sig in walls.items():
        w, fc = _writer([LLMResult(tool_input=_compile_tool(), model="m"),
                         _review_pass()])
        spec, info = CuratedCompiler(w).compile_one(mk_rec(), recent=[sig] * 10)
        check(f"**{label} 配额满 -> 仍 accepted**", spec is not None, info)
        check(f"{label}: 没有 reject", not info.get("reject_reasons"),
              info.get("reject_reasons"))


def test_h4f_signature_still_fully_recorded():
    """§6-4: 分类照常**完整记录**(只是不构成准入要求)。"""
    print("\n[H4-F] signature 仍完整记录")
    from tools.curated_compiler import CuratedCompiler
    w, fc = _writer([LLMResult(tool_input=_compile_tool(), model="m"),
                     _review_pass()])
    spec, info = CuratedCompiler(w).compile_one(mk_rec(), recent=[])
    check("accepted", spec is not None, info)
    if spec is None:
        return
    sig = spec.signature
    check("mechanism_family", sig.mechanism_family == "hidden_function",
          sig.mechanism_family)
    check("solution_shape",
          sig.solution_shape == "hidden_function_explains_behavior",
          sig.solution_shape)
    check("domain", sig.domain == "maritime", sig.domain)
    check("relation", sig.relation == "stranger", sig.relation)
    check("emotion_mode", sig.emotion_mode == "neutral", sig.emotion_mode)
    check("death 字段可读", hasattr(sig, "death"))


def test_h4f_true_text_near_duplicate_still_rejected():
    """§6-5: 真文本 near-duplicate **仍然拒绝**(与题型分布无关)。"""
    print("\n[H4-F] 文本近重复仍拒绝")
    from tools.curated_compiler import CuratedCompiler
    near_dup = type("R", (), {"puzzle": mk_rec().surface})()
    w, fc = _writer([LLMResult(tool_input=_compile_tool(), model="m"),
                     _review_pass()])
    spec, info = CuratedCompiler(w).compile_one(mk_rec(), recent=[near_dup])
    check("**被拒(文本重复不是 diversity)**", spec is None, info)
    check("stage 标 too_similar", info.get("stage") == "too_similar", info)


def test_h4f_real_content_hard_gate_still_rejects():
    """§6-6: 真硬门(livestream_safe / narrator_truthful 等)**仍然拒绝**。"""
    print("\n[H4-F] 真内容硬门仍拒绝")
    from tools.curated_compiler import CuratedCompiler
    # livestream_safe=False -> 内容硬门拒
    bad_live = _compile_tool(quality_checks=_qc_v2(livestream_safe=False))
    w, fc = _writer([LLMResult(tool_input=bad_live, model="m")])
    spec, info = CuratedCompiler(w).compile_one(mk_rec())
    check("**livestream_safe=False -> 被拒**", spec is None, info)
    check("**拒因里能看到 livestream_safe**",
          "livestream_safe" in str(info.get("reject_reasons"))
          or "livestream_safe" in str(info.get("stage")),
          (info.get("stage"), info.get("reject_reasons")))

    # narrator_truthful=False (truth audit) -> 拒
    class _AuditFail(FakeClient):
        def messages(self, system, user, max_tokens=None, tool=None,
                     temperature=None, timeout=None, max_retries=None):
            name = (tool or {}).get("name")
            if name == "emit_truth_audit":
                return LLMResult(tool_input={
                    "__truth_audit__": True, "narrator_truthful": False,
                    "mechanism_consistent": True, "conflicts": [],
                    "why": "叙事不实"}, model="m")
            return super().messages(system, user, max_tokens=max_tokens,
                                    tool=tool, temperature=temperature,
                                    timeout=timeout, max_retries=max_retries)

    from story.llm import PuzzleWriter
    fc2 = _AuditFail([LLMResult(tool_input=_compile_tool(), model="m"),
                      _review_pass()])
    w2 = PuzzleWriter(client=fc2, runtime_cfg=_runtime_cfg())
    spec2, info2 = CuratedCompiler(w2).compile_one(mk_rec())
    check("**narrator_truthful=False -> 被拒**", spec2 is None, info2)


def test_h4f_generated_pool_cross_gate_unchanged():
    """§6-7/8: AI 原创链的 cross_puzzle_gate —— **G4-B 改了池子这一侧**。

    原来守的是"external curated 不看分布, AI 原创链照旧看"(产品边界是
    单向的)。**G4 把这条边界推平了**: 产品决定是"同类型不是拒题理由",
    所以 AI 原创池也走两遍 —— Pass 1 偏好不撞的, Pass 2 兜底撞的。

    真正**没有变**的是生成那一侧: `cross_puzzle_gate` 仍然被调用、
    结果仍然算得出来, 只是从"拒稿"变成"记录 + 偏好"。这里断言池子
    交付了题**并且**它确实撞了门(因此走的是 Pass 2), 两条都不是恒真。
    """
    print("\n[H4-F / G4-B] generated pool 的 cross gate 降为偏好")
    import test_pool as TP
    from story.quality import Quotas, cross_puzzle_gate
    with TP.tmpdir() as d:
        pool = TP.PuzzlePool.open(TP.mkcfg(d))
        s = TP.good_spec()
        check("生成池入池", pool.add(s) is True)
        wall = [s.signature.to_dict()] * 10
        # 门**确实**算出了冲突 —— 否则下面那条"仍能交付"是在测空气。
        check("前置: cross gate 确实非空",
              bool(cross_puzzle_gate(s, wall, Quotas.from_config(pool.cfg),
                                     s.blueprint)))
        check("**G4-B: 配额满仍可播(Pass 2)**",
              pool.playable_count(wall) >= 1, pool.playable_count(wall))
        check("**G4-B: pop 同样交付**", pool.pop_next(wall) is not None)
        check("**并且记为 Pass 2**", pool.diversity_reject_count >= 1,
              pool.diversity_reject_count)
        # `cross_puzzle_gate()` 本体对 AI 原创仍然是硬判据
        gate = cross_puzzle_gate(s, wall, Quotas.from_config(pool.cfg),
                                 s.blueprint)
        check("**cross_puzzle_gate 本体仍返回违规**", bool(gate), gate)


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
        test_story_review_signal_does_not_block_compilation,
        test_review_missing_story_fields_does_not_block_compilation,
        test_accepted_path_is_exactly_three_llm_calls,
        test_prompt_carries_canonical_source,
        test_english_flagged_for_translation,
        test_chinese_source_not_told_to_translate,
        # AI 门
        test_hard_gate_rejects_on_false_check,
        test_soft_signals_are_not_gates,
        test_hard_gate_requires_its_checks,
        test_ai_gate_rejects_explicit_reject,
        test_hard_gate_is_fail_closed_on_self_contradiction,
        test_signal_self_contradiction_does_not_reject,
        test_explicit_ai_reject_does_not_retry,
        test_missing_hard_check_rejects_without_retry,
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
        test_curated_accepted_even_when_cross_gate_blocks,
        # ---- H4-F: 产品边界 (external curated 不因题型分布被拒) ----
        test_h4f_quota_wall_never_rejects_curated,
        test_h4f_same_mechanism_and_shape_still_accepted,
        test_h4f_death_gap_domain_saturated_still_accepted,
        test_h4f_signature_still_fully_recorded,
        test_h4f_true_text_near_duplicate_still_rejected,
        test_h4f_real_content_hard_gate_still_rejects,
        test_h4f_generated_pool_cross_gate_unchanged,
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
