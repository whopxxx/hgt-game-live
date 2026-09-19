#!/usr/bin/env python
# coding: utf-8
"""运行: uv run tests/test_curated_v2.py（**完全离线, 无网络**）。

Batch H3-A 的离线回归: **curated-v2 题型准入 + 决策账本**。

## 这批要守的三件事

  1. **q10000 永远进不来**(任务书六)。"卡车烧油变轻"这类单点物理
     脑筋急转弯在 H2 的九条判据下能全过 —— 它有反常、有唯一解释、
     有"因果反转"。它不是质量不合格, 是**题型定义太宽**。
     所以这里测的不是"tag=physics 就拒"(那只杀一道题), 而是
     **哪怕把 physics tag 删掉, 这种故事结构仍然被拒**。
  2. **决策账本区分四态**(任务书三)。accepted / rejected 是终态,
     technical_defer / interrupted 必须**可重试** —— 把 timeout 记成
     rejected 会永久吃掉一道可能的好题, 而且没有任何地方会显示丢了。
  3. **旧 policy 自动隔离**(任务书十九)。H2 按 v1 收的题(H2 的
     `curated_pool.jsonl` 里那 31 道)不能因为"池文件还在"就继续可播。
     靠代码判定, 不靠人工删文件。

## 为什么这些必须是离线测试

它们断言的是"我们怎么判模型给的答案 / 账本语义 / 池准入"——纯逻辑。
真正的 LLM 调用留给 H3-C 的小样本验证(那才需要真钱真网络)。
"""
from __future__ import annotations

import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import curated_compiler as CC  # noqa: E402
from tools import curated_ledger as CL  # noqa: E402
from tools.curated_common import RawCuratedPuzzle  # noqa: E402

FAIL = [0]


def check(name, cond, extra=""):
    if cond:
        print(f"  ok  {name}")
        return
    print(f"  FAIL {name}  {extra}")
    FAIL[0] += 1


@contextmanager
def tmpdir():
    d = tempfile.mkdtemp(prefix="hgt_h3_")
    try:
        yield d
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


# ======================================================================
# q10000 —— 永久回归 fixture
# ======================================================================
#: Puzzling SE q10000 的真实结构(不是逐字原文, 但是同一个故事):
#: 一辆恰好 10000 磅的卡车要过限重 10000 磅的桥, 车没超重。
#: 谜底 = 车开了一段路, 烧掉了燃料, 所以变轻了。
#:
#: ⚠️ 这里**刻意不带 tags**。真实记录上挂着 lateral-thinking + physics,
#: 而 tests 里必须能证明"**删掉 physics 标签也照样拒**" —— 否则这道
#: 回归测的只是标签黑名单, 换个标签就绕过去了。
_Q10000_SURFACE = (
    "一辆卡车装载后总重恰好 10000 磅。它要通过一座限重 10000 磅的桥, "
    "司机没有卸货, 也没有绕路, 桥也没有塌。为什么?")
_Q10000_BOTTOM = (
    "卡车在到达桥之前已经开了一段路, 消耗了燃料, 所以实际重量略小于 "
    "10000 磅。")


def mk_truck_rec(**kw) -> RawCuratedPuzzle:
    """q10000 型记录。**没有** physics tag(见上)。"""
    d = dict(
        external_id="pse:q:10000", source="Puzzling Stack Exchange",
        source_url="https://puzzling.stackexchange.com/q/10000",
        source_kind="stackexchange",
        question_author="Q", answer_author="A",
        question_license="CC BY-SA 4.0", answer_license="CC BY-SA 4.0",
        title="10,000 pound truck",
        surface=_Q10000_SURFACE, bottom=_Q10000_BOTTOM,
        language="en", original_language="en",
        tags=[])
    d.update(kw)
    return RawCuratedPuzzle(**d)


def _qc_good(**kw):
    """十二条全部合格(含 single_trick=False 这个反向项)。"""
    qc = {k: True for k in CC.CURATED_CHECKS_V2}
    qc["single_trick"] = False
    qc.update(kw)
    return qc


def _truck_tool(**kw):
    """模型对 q10000 的**真实误判**: 九条全给它 true, 于是 H2 收了。

    这是 H2 实跑里发生的事 —— 模型认为它有 clear_anomaly(反常)、
    unique_explanation(就是烧油)、has_reversal(因果反转)、
    detail_recontextualized("10000 磅"这个细节揭晓后含义变了)。
    每一条单看都说得通, 合起来却是在描述一个**知识点**, 不是一个故事。
    """
    d = {
        "accepted": True,
        # H2 的九条 —— 全部通过(这是关键: 光靠九条拦不住它)
        "quality_checks": _qc_good(
            # v2 三条: 故事门在这里应该拦下来
            story_reconstruction=False,
            multi_step_deduction=False,
            single_trick=True),
        "style_tags": ["causal_flip"],
        "content_style": ["逻辑"],
        "title": "10000 磅的卡车",
        "puzzle": _Q10000_SURFACE,
        "answer": _Q10000_BOTTOM,
        "core_answer": "开了一段路烧掉燃料, 重量降到 10000 磅以下。",
    }
    d.update(kw)
    return d


def test_q10000_is_rejected_by_story_gate():
    """**本批最关键的回归**: q10000 型被故事门拒。

    它九条全过 —— 所以拒它的**必须**是 v2 那三条, 不能是别的门
    (否则换个门它又能进来)。
    """
    print("\n[H3-A] q10000 被 v2 故事门拒(**九条全过**)")
    d = _truck_tool()
    # 先确认"九条真的全过" —— 否则这条测试可能只是在测别的门
    nine_ok = all(d["quality_checks"].get(k) is True
                  for k in CC.CURATED_CHECKS)
    check("前提: H2 的九条**全部通过**(所以 H2 拦不住它)", nine_ok)
    ok, why = CC.check_tool_result(d)
    check("**v2 判它不合格**", not ok, why)
    sgr = CC.story_gate_reasons(d)
    check("**点名 single_trick**", "single_trick" in sgr, sgr)
    check("**点名 not_story_reconstruction**",
          "not_story_reconstruction" in sgr, sgr)
    check("**点名 no_multi_step_deduction**",
          "no_multi_step_deduction" in sgr, sgr)


def test_q10000_rejected_without_physics_tag():
    """**删掉 physics 标签也照样拒**(任务书六的明确要求)。

    这条防的是"用标签黑名单糊弄过去": 如果哪天有人把
    `physics` 加进 NON_STORY_TAGS 就宣布修好了, 那么一道**没有**
    physics 标签但结构相同的题仍然会溜进来。所以 fixture 不带标签。
    """
    print("\n[H3-A] 没有 physics tag 也一样拒(测的是结构不是标签)")
    rec = mk_truck_rec(tags=[])
    check("fixture 确实没有 physics tag",
          "physics" not in rec.tags, rec.tags)
    d = _truck_tool()
    ok, _ = CC.check_tool_result(d)
    check("**仍然被拒**", not ok)


def test_q10000_via_compile_loop():
    """端到端: 走 `compile_one`, q10000 拿不到 spec, 且**不重试**。

    结构性判定 -> 重试不会变好 -> 只烧一次 LLM。这一点很实际:
    Lazy Curator 每次运行只处理有限条数, 在垃圾题上重试等于挤占
    好题的预算。
    """
    print("\n[H3-A] q10000 端到端: 无 spec 且不重试")
    from tools.curated_compiler import CuratedCompiler
    from tests.test_curated_compile import _writer
    from story.llm import LLMResult
    w, fc = _writer([LLMResult(tool_input=_truck_tool(), model="m")])
    spec, info = CuratedCompiler(w).compile_one(mk_truck_rec(), recent=[],
                                                max_attempts=3)
    check("**没有 spec**", spec is None, info)
    check("**只调了一次 LLM**(结构性拒绝不重试)",
          len(fc.calls) == 1, len(fc.calls))
    check("stage 标 story_gate", info["stage"] == "story_gate", info)
    check("story_gate 带原因", info.get("story_gate"), info)


def test_good_story_still_passes():
    """**反向确认**: 一道真海龟汤必须仍然能过 —— 否则我们只是把门焊死。

    用 test_curated_compile 里那道灯塔题(它有真正的身份/物品意义反转),
    证明 v2 不是"什么都拒"。
    """
    print("\n[H3-A] 反向确认: 真海龟汤仍然过")
    from tests.test_curated_compile import _compile_tool
    d = _compile_tool()
    ok, why = CC.check_tool_result(d)
    check("**真故事通过 v2**", ok, why)
    check("故事门无话可说", CC.story_gate_reasons(d) == [],
          CC.story_gate_reasons(d))


def test_inverted_check_direction():
    """`single_trick` 是**反向**判据(true = 坏)。方向写反是致命的。

    如果哪天有人把它和其余十一条一样当成 true = 好, 那么:
      好题(single_trick=False) -> 被判不合格 -> **全部拒**
      卡车题(single_trick=True) -> 被判合格 -> **放进来**
    两个方向同时错, 而且错了以后测试若还是用
    `{k: True for k in CHECKS}` 构造 fixture, 会一起绿。
    """
    print("\n[H3-A] single_trick 方向(反向判据)")
    check("声明为反向", "single_trick" in CC._INVERTED_CHECKS)
    # 缺这一项 -> 不合格(不能默认成"没问题")
    qc = _qc_good()
    del qc["single_trick"]
    ok, why = CC.check_tool_result({"accepted": True, "quality_checks": qc})
    check("**缺 single_trick -> 拒**(不能默认通过)", not ok, why)
    # 明确 False -> 合格
    ok2, _ = CC.check_tool_result(
        {"accepted": True, "quality_checks": _qc_good()})
    check("**明确 False -> 合格**", ok2)


def test_story_gate_beats_self_reported_accepted():
    """模型自报 accepted=true 也不作数 —— 代码独立判一次。

    这是"prompt 是请求, 代码才是保证"的具体体现。
    """
    print("\n[H3-A] 自报 accepted=true 不能覆盖故事门")
    d = _truck_tool(accepted=True)
    check("模型确实自称 accepted", d["accepted"] is True)
    ok, _ = CC.check_tool_result(d)
    check("**代码仍然拒**", not ok)
    check("**故事门独立表态**", CC.story_gate_reasons(d) != [])


# ======================================================================
# 决策账本
# ======================================================================
def test_ledger_four_states_are_distinct():
    """四态语义必须真的不同(任务书三)。"""
    print("\n[H3-A] 账本四态")
    with tmpdir() as d:
        p = os.path.join(d, "dec.jsonl")
        led = CL.DecisionLedger(p)
        rec = mk_truck_rec()
        PV = CC.CURATED_POLICY_VERSION

        check("初始: 未处理", not led.is_settled(rec, PV))

        led.record(rec, decision=CL.TECHNICAL_DEFER, policy_version=PV,
                   stage="compile_call", reasons=["gateway_timeout"])
        check("**technical_defer 不是终态**(下次要重试)",
              not led.is_settled(rec, PV), led.last(rec, PV))

        led.record(rec, decision=CL.INTERRUPTED, policy_version=PV,
                   stage="interrupted")
        check("**interrupted 也不是终态**", not led.is_settled(rec, PV))

        led.record(rec, decision=CL.REJECTED, policy_version=PV,
                   stage="story_gate", reasons=["single_trick"])
        check("**rejected 是终态**", led.is_settled(rec, PV))

        # 最后一条为准
        last = led.last(rec, PV)
        check("最后一条决策是 rejected",
              last.get("decision") == CL.REJECTED, last)
        check("reasons 保留", "single_trick" in last["reasons"], last)


def test_accepted_is_terminal_and_stats():
    print("\n[H3-A] accepted 终态 + 统计")
    with tmpdir() as d:
        p = os.path.join(d, "dec.jsonl")
        led = CL.DecisionLedger(p)
        PV = CC.CURATED_POLICY_VERSION
        r1 = mk_truck_rec(external_id="pse:q:1")
        r2 = mk_truck_rec(external_id="pse:q:2")
        led.record(r1, decision=CL.ACCEPTED, policy_version=PV)
        led.record(r2, decision=CL.REJECTED, policy_version=PV,
                   reasons=["single_trick"])
        check("r1 settled", led.is_settled(r1, PV))
        check("r1 accepted", led.is_accepted(r1, PV))
        check("r2 不是 accepted", not led.is_accepted(r2, PV))
        st = led.stats(PV)
        check("统计 accepted=1", st[CL.ACCEPTED] == 1, st)
        check("统计 rejected=1", st[CL.REJECTED] == 1, st)


def test_different_policy_reopens_the_question():
    """policy 变了 -> 旧结论不适用, 必须重审(任务书三)。"""
    print("\n[H3-A] policy bump -> 重新可审")
    with tmpdir() as d:
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        rec = mk_truck_rec()
        led.record(rec, decision=CL.REJECTED,
                   policy_version="curated-v1", reasons=["single_trick"])
        check("v1 下是终态", led.is_settled(rec, "curated-v1"))
        check("**v2 下要重审**(旧结论按旧标准下的)",
              not led.is_settled(rec, "curated-v2"))


def test_content_change_reopens_the_question():
    """**内容变了 -> 重审**。SE 帖子可以被编辑, 而 id 不变。

    只按 external_id 判"处理过"会把一道**已经被作者改过**的题当成
    旧题跳过 —— 用的是它改之前的结论。
    """
    print("\n[H3-A] 内容变化 -> 重新可审")
    with tmpdir() as d:
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        PV = CC.CURATED_POLICY_VERSION
        rec = mk_truck_rec()
        led.record(rec, decision=CL.ACCEPTED, policy_version=PV)
        check("原内容: 已终结", led.is_settled(rec, PV))

        edited = mk_truck_rec(bottom=_Q10000_BOTTOM + " 补充说明。")
        check("**编辑后: 要重审**", not led.is_settled(edited, PV),
              "同一 id 内容变了, 不该沿用旧结论")


def test_ledger_survives_reload():
    """账本是 append-only, 重开进程后结论仍在。"""
    print("\n[H3-A] 账本跨进程保持")
    with tmpdir() as d:
        p = os.path.join(d, "dec.jsonl")
        PV = CC.CURATED_POLICY_VERSION
        rec = mk_truck_rec()
        led1 = CL.DecisionLedger(p)
        led1.record(rec, decision=CL.REJECTED, policy_version=PV,
                    reasons=["single_trick"])
        led2 = CL.DecisionLedger(p)          # 新进程
        check("重开后仍然是终态", led2.is_settled(rec, PV))
        check("重开后 rejected",
              led2.last(rec, PV).get("decision") == CL.REJECTED)


def test_half_written_line_is_skipped():
    """半截行(上次写到一半被杀)-> 跳过, 不抛。

    丢它是安全的: 那条题下次会重审。抛异常则会让整个账本不可读 ——
    那是把小事故升级成大事故。
    """
    print("\n[H3-A] 半截行不致命")
    with tmpdir() as d:
        p = os.path.join(d, "dec.jsonl")
        PV = CC.CURATED_POLICY_VERSION
        led = CL.DecisionLedger(p)
        rec = mk_truck_rec()
        led.record(rec, decision=CL.REJECTED, policy_version=PV)
        with open(p, "a", encoding="utf-8") as f:
            f.write('{"external_id": "pse:q:9", "deci')   # 半截
        led2 = CL.DecisionLedger(p)
        check("**不抛且旧结论仍在**", led2.is_settled(rec, PV))
        check("半截行没被算进来", len(led2.rows) == 1, len(led2.rows))


def test_illegal_decision_rejected():
    """拼错的 decision 会被**拒绝写入**, 而不是静默接受。

    一个既非终态也非可重试的值会让行为取决于下游怎么读 —— 那种
    记录等于悄悄改变语义。
    """
    print("\n[H3-A] 非法 decision 被拒")
    rec = mk_truck_rec()
    try:
        CL.make_decision(rec, decision="rejcted", policy_version="v")
        check("**非法值应抛异常**", False, "没有抛")
    except ValueError:
        check("非法值抛 ValueError", True)


def test_content_hash_ignores_id():
    """content_hash 只跟内容走, 不跟 id 走。"""
    print("\n[H3-A] content_hash 只认内容")
    a = mk_truck_rec(external_id="pse:q:1")
    b = mk_truck_rec(external_id="pse:q:2")
    check("同内容不同 id -> 同 hash",
          CL.content_hash_of(a) == CL.content_hash_of(b))
    c = mk_truck_rec(bottom="完全不同的谜底")
    check("同 id 不同内容 -> 不同 hash",
          CL.content_hash_of(a) != CL.content_hash_of(c))


# ======================================================================
# 池准入: 旧 policy 自动隔离
# ======================================================================
def test_old_curated_policy_is_quarantined():
    """**v1 的 curated 题在 v2 下不可播**(任务书十九)。

    这是"不依赖人工删文件"的核心保证: H2 那 31 道题仍在池文件里,
    但过不了准入门 -> 不计库存 / 不被 pop_next 返回。
    """
    print("\n[H3-A] 旧 curated policy 自动隔离")
    from story.pool import PuzzlePool
    from tests.test_pool import good_spec, _curated_spec

    s_v2 = _curated_spec()
    check("v2 题带当前 policy",
          s_v2.curated_policy_version == CC.CURATED_POLICY_VERSION)

    s_v1 = _curated_spec()
    s_v1.curated_policy_version = "curated-v1"      # 模拟 H2 的产物
    ok2, why2 = PuzzlePool._validate_pool_spec(s_v2)
    check("**v2 题过门**", ok2, why2)
    ok1, why1 = PuzzlePool._validate_pool_spec(s_v1)
    check("**v1 题被隔离**", not ok1, why1)
    check("隔离理由点名 curated 政策",
          "curated 政策" in why1, why1)

    # 空值(老 archive 没有这把键)-> 同样隔离
    s_old = _curated_spec()
    s_old.curated_policy_version = ""
    oko, whyo = PuzzlePool._validate_pool_spec(s_old)
    check("**缺字段的老题也被隔离**", not oko, whyo)

    # 自由生成的题**不受**这条约束(它们本就不该声明 curated 政策)
    free = good_spec()
    check("自由生成题 source_type 为空", not free.source_type)
    okf, whyf = PuzzlePool._validate_pool_spec(free)
    check("**自由生成题不受 curated 门影响**", okf, whyf)


def test_curated_policy_version_round_trips():
    """`curated_policy_version` / `content_style` 必须能存档再读回。

    读不回来的后果很隐蔽: 一道**合格**的 v2 题重新载入后
    `curated_policy_version` 变成空串 -> 被自己的池隔离 ->
    表现为"编译成功了却播不出来"。
    """
    print("\n[H3-A] 新字段存档往返")
    from story.puzzle import PuzzleSpec
    from tests.test_pool import _curated_spec
    s = _curated_spec()
    s.content_style = ["悬疑", "细思极恐"]
    s.curated_policy_version = CC.CURATED_POLICY_VERSION

    d = s.to_archive()
    check("archive 里有 curated_policy_version",
          d.get("curated_policy_version") == CC.CURATED_POLICY_VERSION, d.get("curated_policy_version"))
    check("archive 里有 content_style",
          d.get("content_style") == ["悬疑", "细思极恐"], d.get("content_style"))

    back = PuzzleSpec.from_dict(d)
    check("**读回后 policy 不变**",
          back.curated_policy_version == CC.CURATED_POLICY_VERSION,
          back.curated_policy_version)
    check("读回后 content_style 不变",
          back.content_style == ["悬疑", "细思极恐"], back.content_style)


def test_provenance_survives_reviewer_for_new_fields():
    """`_apply_review` 会**重建** spec —— 新字段漏了就会静默丢失。

    这是 H2 踩过的同一个坑(`_apply_review` 曾把 8 个溯源字段全丢了)。
    content_style / curated_policy_version 同属"只有显式列出才能活下来"。
    """
    print("\n[H3-A] 新字段熬过 _apply_review 重建")
    import inspect
    from story import llm as L
    src = inspect.getsource(L.PuzzleWriter._apply_review)
    check("**curated_policy_version 被带过**",
          "curated_policy_version" in src)
    check("**content_style 被带过**", "content_style" in src)


def test_new_fields_not_in_snapshot_repr():
    """curated 溯源**不下发前端** —— 新字段也必须守这条。"""
    print("\n[H3-A] 新字段不进 Snapshot")
    from story.state import Snapshot
    fields = set(getattr(Snapshot, "__dataclass_fields__", {}))
    for name in ("content_style", "curated_policy_version", "style_tags",
                 "attribution"):
        check(f"Snapshot 没有 {name}", name not in fields)


def test_story_gate_stage_depth():
    """`story_gate` 与 `ai_gate` **同级** —— 它是 AI 门的加强版。"""
    print("\n[H3-A] story_gate 深度与 ai_gate 同级")
    check("story_gate 在深度表里", "story_gate" in CC._STAGE_DEPTH)
    check("**深度相同**",
          CC._STAGE_DEPTH["story_gate"] == CC._STAGE_DEPTH["ai_gate"],
          (CC._STAGE_DEPTH.get("story_gate"),
           CC._STAGE_DEPTH.get("ai_gate")))


def test_prompt_declares_v2_rules():
    """prompt 里必须**真的写着** v2 三条 —— 否则模型不知道要判它们。"""
    print("\n[H3-A] prompt 声明 v2 三条")
    sysp = CC.CURATED_COMPILE_SYSTEM
    for k in ("story_reconstruction", "multi_step_deduction",
              "single_trick"):
        check(f"prompt 提到 {k}", k in sysp)
    check("prompt 给出硬门表述", "硬门" in sysp)
    check("prompt 有正例/反例对照", "烧油" in sysp or "卡车" in sysp)


# ======================================================================
def main():
    tests = [
        test_q10000_is_rejected_by_story_gate,
        test_q10000_rejected_without_physics_tag,
        test_q10000_via_compile_loop,
        test_good_story_still_passes,
        test_inverted_check_direction,
        test_story_gate_beats_self_reported_accepted,
        test_ledger_four_states_are_distinct,
        test_accepted_is_terminal_and_stats,
        test_different_policy_reopens_the_question,
        test_content_change_reopens_the_question,
        test_ledger_survives_reload,
        test_half_written_line_is_skipped,
        test_illegal_decision_rejected,
        test_content_hash_ignores_id,
        test_old_curated_policy_is_quarantined,
        test_curated_policy_version_round_trips,
        test_provenance_survives_reviewer_for_new_fields,
        test_new_fields_not_in_snapshot_repr,
        test_story_gate_stage_depth,
        test_prompt_declares_v2_rules,
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
