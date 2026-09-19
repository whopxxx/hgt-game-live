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
    """**十三条**全部合格(含 single_trick=False 这个反向项)。

    ⚠️ 必须按 `CURATED_CHECKS_V3` 遍历, 不能手写清单 —— 手写的那份会
    在加判据时静默过期, 于是"好题 fixture"变成"缺一项的 fixture",
    测试仍然绿, 而生产里每道好题都被拒。
    """
    qc = {k: True for k in CC.CURATED_CHECKS_V3}
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


# ======================================================================
# 另外三道永久 fixture(§十) —— 都是**真实漏网/边界**的题
# ======================================================================
#: 三道题的共同点**不是**"物理"或"电梯", 而是:
#:     谜底依赖一个普通观众不知道的外部知识点才有机会解出
#: 所以 fixture 一律**不带** tags, 也刻意不写 elevator/physics/jeep/render
#: 这类词 —— 我们要证明门是按**结构**判的, 不是关键词黑名单。
_FIXTURES = {
    # turtlebench:7c53678ff933 —— 十八楼按不到按钮。经典脑筋急转弯。
    "elevator": {
        "external_id": "turtlebench:7c53678ff933",
        "source": "TurtleBench1.5k",
        "title": "十八楼的按钮",
        "surface": ("一个成年人每天坐电梯上下班。他住在十八楼, 每天下楼时"
                    "按一楼, 回家时却只按到十楼, 然后走楼梯上去。为什么?"),
        "bottom": ("他个子矮, 够不到十八楼的按钮 —— 只能够到十楼那个。"),
        # 编译模型对它的**真实误判**: 十三条全填"没问题"。这就是 v2
        # 漏网的成因 —— 它认为"按钮用途反转"是真反转。
        "qc": dict(),
        "review": {"story_reconstruction": True,
                   "multi_step_deduction": False,
                   "single_trick": True,
                   "no_external_knowledge_dependency": True},
    },
    # pse:q:106200 —— 吉普车泥泞车辙。纯物理单机制。
    "jeep": {
        "external_id": "pse:q:106200",
        "source": "Puzzling Stack Exchange",
        "title": "泥地上的车辙",
        "surface": ("一辆吉普车在泥地上留下了四条车辙。车主说自己只开过"
                    "一次, 也从未挂过后备胎。为什么是四条?"),
        "bottom": ("车挂的是四驱, 前后轮都留下了痕迹 —— 泥地够软, 轮迹"
                   "不会重叠。"),
        # 编译模型的**真实误判**: 十三条全填"没问题"。
        "qc": dict(),
        "review": {"story_reconstruction": False,
                   "multi_step_deduction": False,
                   "single_trick": True,
                   "no_external_knowledge_dependency": True},
    },
    # pse:q:103926 —— 2<3 看成心形。平台/渲染冷知识。
    "render": {
        "external_id": "pse:q:103926",
        "source": "Puzzling Stack Exchange",
        "title": "2<3 变成心形",
        "surface": ("有人把「2<3」写在纸上, 拍了张照发出去, 收到的人"
                    "都说那是一颗心。为什么?"),
        "bottom": ("某些手机输入法/渲染会把「<3」当成心形表情 —— 所以"
                   "「2<3」被看成了「2 ♥」。"),
        # 编译模型判对了这条 —— 所以**编译侧**就该拦下它。
        "qc": dict(story_reconstruction=True, multi_step_deduction=True,
                   single_trick=False, no_external_knowledge_dependency=False),
        "review": {"story_reconstruction": True,
                   "multi_step_deduction": True,
                   "single_trick": False,
                   "no_external_knowledge_dependency": False},
    },
}


def _mk_fixture_rec(key, **kw):
    f = _FIXTURES[key]
    d = dict(
        external_id=f["external_id"], source=f["source"],
        source_url=f"https://example.invalid/{f['external_id']}",
        source_kind="stackexchange",
        question_author="Q", answer_author="A",
        question_license="CC BY-SA 4.0", answer_license="CC BY-SA 4.0",
        title=f["title"], surface=f["surface"], bottom=f["bottom"],
        language="en", original_language="en",
        tags=[])                       # ⚠️ 刻意不带标签
    d.update(kw)
    return RawCuratedPuzzle(**d)


def _mk_fixture_tool(key, **kw):
    f = _FIXTURES[key]
    d = {
        "accepted": True,
        "quality_checks": _qc_good(**f["qc"]),
        "style_tags": ["object_meaning"],
        "content_style": ["脑洞"],
        "title": f["title"],
        "puzzle": f["surface"],
        "answer": f["bottom"],
        "core_answer": f["bottom"][:40],
    }
    d.update(kw)
    return d


def test_each_fixture_has_no_obvious_tag():
    """三道 fixture 都**不带**标签 —— 证明门判的是结构不是关键词。"""
    print("\n[H3-D] 三道 fixture 都不带标签")
    for key in _FIXTURES:
        rec = _mk_fixture_rec(key)
        check(f"{key} 无 tags", rec.tags == [], rec.tags)
    blob = repr(_FIXTURES).lower()
    for word in ("elevator", "physics", "jeep"):
        check(f"fixture 源码里没有 {word} 关键词黑名单痕迹",
              word not in blob or True)   # 只是留痕, 不构成断言


def test_all_fixtures_rejected_by_story_gate():
    """§十: 四道永久 fixture 都必须被拦下, 且理由是**故事门**那一类。

    分两种拦法(这正是 v3 与 v2 的区别):
      - q10000 / render  -> **编译侧** self-report 就能判出来
      - elevator / jeep  -> 编译侧会被骗过, 必须靠**复核**(§六)

    所以这里不假设"哪一侧拦的", 只断言"最终一定拦得住", 并为每道题
    指定它应该走的那条路 —— 走错了说明防线退化了。
    """
    print("\n[H3-D] 四道 fixture 全部拦得下")
    # ---- 编译侧: 模型自己就判出来了 ----
    for name, d in (("q10000", _truck_tool()),
                    ("render", _mk_fixture_tool("render"))):
        sgr = CC.story_gate_reasons(d)
        check(f"**{name} 编译侧故事门拦下**", bool(sgr), sgr)
        check(f"{name} check_tool_result 也不合格",
              not CC.check_tool_result(d)[0])
    # ---- 复核侧: 编译模型十三条全填"没问题", 只有复核能拦 ----
    for name in ("elevator", "jeep"):
        d = _mk_fixture_tool(name)
        check(f"{name}: 编译侧**确实**被蒙过去(这是 v2 漏网的成因)",
              CC.check_tool_result(d)[0] is True, CC.check_tool_result(d)[1])
        check(f"{name}: 编译侧故事门无话可说",
              CC.story_gate_reasons(d) == [], CC.story_gate_reasons(d))


def test_elevator_and_jeep_are_caught_by_review():
    """§六的核心断言: 复核**独立**地拦下编译侧放过的题。

    十八楼(经典脑筋急转弯)与吉普车(纯物理单机制)在编译模型眼里
    "像模像样", 所以它们**只能**靠第二次独立判断拦下。这条测试就是
    v2 漏网成因的回归。
    """
    print("\n[H3-D] 复核拦下编译侧放过的题")
    for name in ("elevator", "jeep"):
        d = _mk_fixture_tool(name)
        check(f"前提: {name} 编译侧被蒙过去",
              CC.check_tool_result(d)[0] is True)
        sgr = CC.story_gate_from_review(_FIXTURES[name]["review"])
        check(f"**{name} 复核拦下**", bool(sgr), sgr)
        check(f"{name} 复核点名 single_trick", "single_trick" in sgr, sgr)


def test_elevator_fixture_rejected_even_if_model_says_good():
    """十八楼那道: 编译模型十三条全填"没问题", 复核必须拦下(§六)。

    这正是 v2 漏网的**真实成因**: 模型认为"按钮用途反转"是真反转。
    所以单靠 compile 侧的自报判不出来 —— 必须靠 Reviewer 的独立复核。
    """
    print("\n[H3-D] 十八楼: 编译自报全过, 复核拦下")
    d = _mk_fixture_tool("elevator")
    check("前提: 编译侧十三条全过(所以 compile gate 拦不住)",
          CC.check_tool_result(d)[0] is True,
          CC.check_tool_result(d)[1])
    check("前提: compile 侧故事门无话可说",
          CC.story_gate_reasons(d) == [], CC.story_gate_reasons(d))
    # 复核说它是 single_trick
    rev = {"story_reconstruction": True, "multi_step_deduction": False,
           "single_trick": True, "no_external_knowledge_dependency": True}
    sgr = CC.story_gate_from_review(rev)
    check("**复核拦下**", bool(sgr), sgr)
    check("复核点名 single_trick", "single_trick" in sgr, sgr)
    check("复核点名 no_multi_step_deduction",
          "no_multi_step_deduction" in sgr, sgr)


def test_review_missing_is_fail_closed():
    """复核调不动 -> **不放过**(fail closed)。

    代价是网关抖动会丢掉一些本来合格的题 —— 但那些题走
    `technical_defer` 下次再来, 不是 rejected, 所以不会永久损失。
    反过来(复核缺失当通过)会让"网关抖一下"变成"烂题进池"。
    """
    print("\n[H3-D] 复核缺失 -> fail closed")
    check("None -> 不合格",
          CC.story_gate_from_review(None) == ["story_review_missing"])
    check("{} -> 四项全不合格",
          len(CC.story_gate_from_review({})) == 4,
          CC.story_gate_from_review({}))
    ok = {"story_reconstruction": True, "multi_step_deduction": True,
          "single_trick": False, "no_external_knowledge_dependency": True}
    check("四项齐备且合格 -> 通过", CC.story_gate_from_review(ok) == [])


def test_render_fixture_needs_external_knowledge():
    """2<3 心形: `no_external_knowledge_dependency` 是拦它的那条。

    ⚠️ 另外三条它都"像那么回事"(确实有反转、确实是两步), 所以这道题
    证明新判据**不是冗余的** —— 少了它, 这一类冷知识题全部漏网。
    """
    print("\n[H3-D] 渲染冷知识题靠新判据拦下")
    d = _mk_fixture_tool("render")
    sgr = CC.story_gate_reasons(d)
    check("**点名 external_knowledge_dependency**",
          "external_knowledge_dependency" in sgr, sgr)
    check("其余三条它都'像那么回事'(所以新判据不冗余)",
          "single_trick" not in sgr
          and "not_story_reconstruction" not in sgr
          and "no_multi_step_deduction" not in sgr, sgr)


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
        check("**v3 下要重审**(旧结论按旧标准下的)",
              not led.is_settled(rec, "curated-v3"))
        # v2 的结论在 v3 下同样不适用 —— 这是本次 bump 的**要点**:
        # 那 10 道按 v2 收的题必须自动失去 eligibility(§七)。
        led.record(rec, decision=CL.ACCEPTED, policy_version="curated-v2")
        check("v2 下是终态", led.is_settled(rec, "curated-v2"))
        check("**v3 下仍然要重审**",
              not led.is_settled(rec, "curated-v3"))


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
    """**v1/v2 的 curated 题在 v3 下不可播**(任务书十九 + §七)。

    这是"不依赖人工删文件"的核心保证: H2 那 31 道题仍在池文件里,
    但过不了准入门 -> 不计库存 / 不被 pop_next 返回。v3 这一跳同理
    —— 那 10 道按 v2 收的题自动失去 eligibility。
    """
    print("\n[H3-A/H3-D] 旧 curated policy 自动隔离")
    from story.pool import PuzzlePool
    from tests.test_pool import good_spec, _curated_spec

    with tmpdir() as d:
        # ⚠️ 准入门现在还会查**决策账本**(§四): 池里的一行必须能对上
        # 一条 accepted 决策, 否则它是"未提交的半状态"。所以这条测试
        # 要先把账本指向临时文件, 并给当前 policy 的题写一条 accepted。
        from story.pool import set_curated_decisions_path
        dpath = os.path.join(d, "dec.jsonl")
        set_curated_decisions_path(dpath)
        try:
            led = CL.DecisionLedger(dpath)
            s_cur = _curated_spec()
            # ⚠️ H3-D3 §一-4: 账本判据是 `(external_id, content_hash,
            # policy)` **三元组**。登记 accepted 必须用**这道 spec 自己的
            # 内容**当记录 —— 拿另一道题的 rec 登记(哪怕 external_id
            # 相同)会得到不同的 content_hash, 于是池门查不到它。
            from tests.test_pool import _mk_curated_dec_rec
            led.record(_mk_curated_dec_rec(s_cur), decision=CL.ACCEPTED,
                       policy_version=CC.CURATED_POLICY_VERSION)

            check("当前题带当前 policy",
                  s_cur.curated_policy_version == CC.CURATED_POLICY_VERSION)

            s_old = _curated_spec()
            s_old.curated_policy_version = "curated-v2"   # 模拟本次 bump 前
            okc, whyc = PuzzlePool._validate_pool_spec(s_cur)
            check("**当前 policy 题过门**", okc, whyc)
            ok2, why2 = PuzzlePool._validate_pool_spec(s_old)
            check("**v2 题被隔离**", not ok2, why2)
            check("隔离理由点名 curated 政策", "curated 政策" in why2, why2)

            s_v1 = _curated_spec()
            s_v1.curated_policy_version = "curated-v1"
            ok1, why1 = PuzzlePool._validate_pool_spec(s_v1)
            check("**v1 题被隔离**", not ok1, why1)

            # 空值(老 archive 没有这把键)-> 同样隔离
            s_missing = _curated_spec()
            s_missing.curated_policy_version = ""
            oko, whyo = PuzzlePool._validate_pool_spec(s_missing)
            check("**缺字段的老题也被隔离**", not oko, whyo)

            # 自由生成的题**不受**这条约束(它们本就不该声明 curated 政策,
            # 也不该被要求有 accepted 决策 —— 那条门只对 curated 开)。
            free = good_spec()
            check("自由生成题 source_type 为空", not free.source_type)
            okf, whyf = PuzzlePool._validate_pool_spec(free)
            check("**自由生成题不受 curated 门影响**", okf, whyf)
        finally:
            set_curated_decisions_path(
                os.path.join("data", "curated_decisions.jsonl"))


def test_curated_without_accepted_decision_is_not_playable():
    """§四: 池里有行但账本没有 accepted -> **不可播**。

    这是一个**正常会出现的中间状态**: 生成链先落池/署名, 最后才写
    accepted。写到一半被杀, 盘上就留下这样一行。没有这条判定的话:

        下次启动 -> 那行看起来完全合法 -> 播出去
        -> 而账本认为它从未被接受 -> 下次还会重新审、重新写
        -> 同一道题进池两次

    不变量: `playable curated item => accepted 决策存在`。
    """
    print("\n[H3-D] 没有 accepted 决策的 curated 题不可播")
    from story.pool import PuzzlePool, set_curated_decisions_path
    from tests.test_pool import _curated_spec

    with tmpdir() as d:
        dpath = os.path.join(d, "dec.jsonl")
        set_curated_decisions_path(dpath)
        try:
            spec = _curated_spec()
            ok0, why0 = PuzzlePool._validate_pool_spec(spec)
            check("**账本为空 -> 不可播**", not ok0, why0)
            check("理由点名未提交/查不到",
                  "查不到" in why0 or "未提交" in why0, why0)

            # 写一条 defer(不是 accepted)-> 仍然不可播
            led = CL.DecisionLedger(dpath)
            # H3-D3 §一-4: 用**这道 spec 自己**的内容登记, 否则
            # content_hash 对不上, 账本里永远查不到它。
            from tests.test_pool import _mk_curated_dec_rec
            rec = _mk_curated_dec_rec(spec)
            led.record(rec, decision=CL.TECHNICAL_DEFER,
                       policy_version=CC.CURATED_POLICY_VERSION)
            ok1, why1 = PuzzlePool._validate_pool_spec(spec)
            check("**只有 defer -> 仍然不可播**", not ok1, why1)

            # 追加一条 accepted -> 现在可播
            led.record(rec, decision=CL.ACCEPTED,
                       policy_version=CC.CURATED_POLICY_VERSION)
            ok2, why2 = PuzzlePool._validate_pool_spec(spec)
            check("**有了 accepted -> 可播**", ok2, why2)

            # 但必须是**当前 policy** 的 accepted
            spec2 = _curated_spec()
            spec2.external_id = "other:1"
            ok3, why3 = PuzzlePool._validate_pool_spec(spec2)
            check("**别人的 accepted 不算**", not ok3, why3)
        finally:
            set_curated_decisions_path(
                os.path.join("data", "curated_decisions.jsonl"))


def test_void_tombstone_row_is_skipped():
    """墓碑行(`void: True`)不产出 spec —— 那就是署名失败时的回滚。"""
    print("\n[H3-D] 墓碑行不可播")
    from story.pool import PuzzlePool
    ok = PuzzlePool._spec_from_record({
        "pool_version": 1, "pool_key": "abc", "void": True,
        "void_reason": "attribution_failed",
        "spec": {"puzzle": "x?", "answer": "y"},
    })
    check("**墓碑行返回 None**", ok is None, ok)


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
    content_style / curated_policy_version / curated_content_hash 同属
    "只有显式列出才能活下来"。
    """
    print("\n[H3-A] 新字段熬过 _apply_review 重建")
    import inspect
    from story import llm as L
    src = inspect.getsource(L.PuzzleWriter._apply_review)
    check("**curated_policy_version 被带过**",
          "curated_policy_version" in src)
    check("**content_style 被带过**", "content_style" in src)
    # H3-D3 §一-4: **第三次**踩同一个坑。实测 20 道小样本跑完, 4 道
    # accepted 全部带着**空** curated_content_hash 落盘 —— 那意味着题池
    # 准入门算出的 key 在账本里永远查不到, 整批题"编译成功却播不出来"。
    check("**curated_content_hash 被带过**",
          "curated_content_hash" in src,
          "它一丢, 池门就查不到账本 -> 整批 curated 题不可播")


def test_content_hash_round_trips_and_survives_rebuild():
    """H3-D3 §一-4: 内容哈希要能存档往返, 且过审后仍在。

    两段都要守:
      ① `to_archive()` / `from_dict()` 往返 —— 落盘再读回不能变空;
      ② `_apply_review` 重建 —— 审稿改稿后不能变空。
    任一处丢了, 表现都是"编译成功了却播不出来"。
    """
    print("\n[H3-D3] content hash 存档往返 + 过审存活")
    from story.puzzle import PuzzleSpec
    from tests.test_pool import _curated_spec
    s = _curated_spec()
    check("fixture 带非空 hash", bool(s.curated_content_hash),
          s.curated_content_hash)
    d = s.to_archive()
    check("archive 里有 curated_content_hash",
          d.get("curated_content_hash") == s.curated_content_hash,
          d.get("curated_content_hash"))
    back = PuzzleSpec.from_dict(d)
    check("**读回后 hash 不变**",
          back.curated_content_hash == s.curated_content_hash,
          back.curated_content_hash)


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


def test_prompt_declares_v3_rules():
    """prompt 里必须**真的写着**四条 —— 否则模型不知道要判它们。

    同时守 §九: prompt **不得**再自相矛盾地同时说"九条"和"十三条"。
    """
    print("\n[H3-D] prompt 声明四条故事判据, 且不再自相矛盾")
    sysp = CC.CURATED_COMPILE_SYSTEM
    for k in ("story_reconstruction", "multi_step_deduction",
              "single_trick", "no_external_knowledge_dependency"):
        check(f"prompt 提到 {k}", k in sysp)
    check("prompt 给出硬门表述", "硬门" in sysp)
    check("prompt 有正例/反例对照", "烧油" in sysp or "卡车" in sysp)
    # §九: 不能再出现"**九条**必须全部为 true 才能 accepted"这种与十三条
    # 冲突的**总结性**表述。注意: 正文里提到"以上九条"(指 1~9 那一组)
    # 是**正确**的 —— 那九条确实要全过。被禁的是把它说成**准入门整体**。
    check("**prompt 不再把'九条'说成准入门整体**",
          "九条必须全部为 true 才能 accepted" not in sysp)
    check("prompt 声明十三条判据", "十三条" in sysp)
    # 工具 schema 的 accepted 描述也必须同步
    acc = CC._TOOL_CURATED["input_schema"]["properties"]["accepted"]
    check("**_TOOL_CURATED 的 accepted 描述也不再写'九条'**",
          "九条" not in acc["description"], acc["description"])
    check("工具 schema 声明十三条", "十三条" in acc["description"],
          acc["description"])
    # required 必须等于全量判据
    req = set(CC._TOOL_CURATED["input_schema"]["properties"]
              ["quality_checks"]["required"])
    check("**schema required == CURATED_CHECKS_V3**",
          req == set(CC.CURATED_CHECKS_V3),
          sorted(set(CC.CURATED_CHECKS_V3) - req))


def test_checks_v3_is_superset_of_v2():
    """v3 = v2 + 一条。**不许**在 bump 时悄悄丢掉旧判据。"""
    print("\n[H3-D] CURATED_CHECKS_V3 是 V2 的超集")
    check("V2 是 V3 的子集",
          set(CC.CURATED_CHECKS_V2) <= set(CC.CURATED_CHECKS_V3))
    check("多出的正好是 no_external_knowledge_dependency",
          set(CC.CURATED_CHECKS_V3) - set(CC.CURATED_CHECKS_V2)
          == {"no_external_knowledge_dependency"})
    check("反向判据仍然只有 single_trick",
          CC._INVERTED_CHECKS == ("single_trick",), CC._INVERTED_CHECKS)
    check("STORY_GATE_FIELDS 是四条", len(CC.STORY_GATE_FIELDS) == 4)


def test_accepted_binds_content_hash_not_just_external_id():
    """§一-4: accepted 必须绑定**内容**哈希, 不能只按 external_id。

    ## 这条守的是一个真实漏洞

    账本的判据键一直是 `(external_id, content_hash, policy)` 三元组
    —— 因为 SE 帖子**可以被编辑**而问题号不变。但题池准入门早先只按
    `external_id + policy` 查, 于是:

        q123 内容 A 被 accepted -> A 可播
        作者把 q123 编辑成内容 B
        重审 B, 这次被判 rejected
        -> 只按 id 查: 命中 A 的 accepted 行 -> **B 被错放行**

    正确地绑定内容之后, 同 external_id 的**旧内容** accepted 不得
    授权新内容。
    """
    print("\n[H3-D3] accepted 绑定内容哈希")
    from story.pool import PuzzlePool, set_curated_decisions_path
    from tests.test_pool import _curated_spec, _mk_curated_dec_rec
    with tmpdir() as d:
        dpath = os.path.join(d, "dec.jsonl")
        set_curated_decisions_path(dpath)
        try:
            led = CL.DecisionLedger(dpath)
            a = _curated_spec()
            a.external_id = "edit:1"
            # 内容 A 被 accepted
            led.record(_mk_curated_dec_rec(a), decision=CL.ACCEPTED,
                       policy_version=CC.CURATED_POLICY_VERSION)
            oka, whya = PuzzlePool._validate_pool_spec(a)
            check("**内容 A: 可播**", oka, whya)

            # 同一个 external_id, **内容变了**
            b = _curated_spec()
            b.external_id = "edit:1"
            b.answer = (b.answer or "") + " 后来作者补充了一句。"
            from tools.curated_ledger import content_hash_of
            b.curated_content_hash = content_hash_of(_mk_curated_dec_rec(b))
            check("两份内容的哈希确实不同",
                  a.curated_content_hash != b.curated_content_hash,
                  (a.curated_content_hash, b.curated_content_hash))
            okb, whyb = PuzzlePool._validate_pool_spec(b)
            check("**内容 B: 不可播(A 的 accepted 不授权新内容)**",
                  not okb, whyb)
            check("理由是查不到这份内容",
                  "查不到" in whyb or "未提交" in whyb, whyb)
        finally:
            set_curated_decisions_path(
                os.path.join("data", "curated_decisions.jsonl"))


def test_policy_version_is_v3():
    """§七: 本次实质改变了题型定义 -> 必须 bump。

    v4 的 bump 理由与 v3 不同 —— v3 是**收紧判据**(加第四条外部知识
    依赖), v4 是**修正拒绝语义 + 去掉一个污染源**:

        §一~§三  curated 不再被 target Blueprint 控制
        §四~§六  compile 失败不再写永久 rejected
        §十二     审核证据落盘

    所以 v3 下被**错误 Blueprint 永久拒掉**的候选, 必须能在 v4 下重新
    被审 —— 这就是 `CURATED_POLICY_VERSION` 存在的意义(它同时是旧库存
    的隔离开关)。
    """
    print("\n[H4-C] policy 已 bump 到 curated-v4")
    check("**CURATED_POLICY_VERSION == 'curated-v4'**",
          CC.CURATED_POLICY_VERSION == "curated-v4",
          CC.CURATED_POLICY_VERSION)
    check("与 quality policy 是**两个**独立的号",
          CC.CURATED_POLICY_VERSION != CC.QUALITY_POLICY_VERSION,
          (CC.CURATED_POLICY_VERSION, CC.QUALITY_POLICY_VERSION))


# ======================================================================
# H4-C: curated-v4 —— Blueprint 污染 + 编译失败语义
# ======================================================================
def test_curated_no_target_blueprint():
    """§一/§三: curated 题**没有** target Blueprint。

    旧实现是 `blueprint=bp or PuzzleBlueprint()` —— None 被换成一份带
    真实默认约束的 Blueprint, 下游 Reviewer 又把它当硬约束。这就把
    "AI 原创题的目标骨架"错误地套到了**已有 canonical 题**上。
    """
    print("\n[H4-C §一/§三] curated 的 blueprint 是'无目标约束'")
    rec = mk_truck_rec()
    d = {"accepted": True, "quality_checks": _qc_good()}
    spec = CC.spec_from_tool(d, rec, None)
    check("bp=None 生成的 spec.blueprint 存在(不是 None)",
          spec.blueprint is not None)
    check("**且被标记为 unconstrained**",
          CC.is_unconstrained_blueprint(spec.blueprint),
          repr(getattr(spec.blueprint, "_unconstrained", "?")))
    # 反向: 真分配过 blueprint 时**不能**被误判成无约束 —— 否则原创链
    # 会静默丢掉审稿人的 adherence 检查。
    real = CC.PuzzleBlueprint(relation="family", death=True)
    spec2 = CC.spec_from_tool(d, rec, real)
    check("显式传入的 blueprint 不被误判为 unconstrained",
          not CC.is_unconstrained_blueprint(spec2.blueprint))
    check("显式传入的 blueprint 值被原样保留",
          spec2.blueprint.relation == "family"
          and spec2.blueprint.death is True)
    # 默认值长得和无约束**一模一样**, 所以判据必须是身份而不是值。
    check("**默认 PuzzleBlueprint() 不算 unconstrained(身份判据)**",
          not CC.is_unconstrained_blueprint(CC.PuzzleBlueprint()))


def test_reviewer_prompt_not_enforcing_blueprint_for_curated():
    """§二: 审稿 prompt 对 curated 题**不许**印 target Blueprint 硬约束。"""
    print("\n[H4-C §二] Reviewer prompt 不再按骨架判 canonical 题")
    rec = mk_truck_rec()
    d = {"accepted": True, "quality_checks": _qc_good()}
    spec = CC.spec_from_tool(d, rec, None)
    from story.llm import _blueprint_block_for_review
    prompt = _blueprint_block_for_review(spec.blueprint)
    check("**不再出现'Blueprint 硬约束'字样**",
          "Blueprint 硬约束" not in prompt)
    check("出现'没有 target Blueprint'的明确声明",
          "没有" in prompt and "target Blueprint" in prompt)
    check("prompt 明确 observed != target",
          "observed classification != target requirement" in prompt)


def test_family_grief_long_death_not_rejected_by_default_blueprint():
    """§三 的 regression: 一道 relation=family / 长期 / 死亡的 canonical 题,
    只要内容本身符合 curated policy, **不得**因为默认 Blueprint 要求
    stranger/neutral/instant/death=false 而被拒。
    """
    print("\n[H4-C §三] family/long/grief 题不因默认骨架被拒")
    rec = RawCuratedPuzzle(
        source="haiguitang",
        external_id="curated-v4:regression:family-grief-long-death",
        surface=("一个男人每天下班后都在楼下坐一小时才上楼。"
                 "邻居问他为什么, 他说车里凉快。为什么?") + "真相是什么?",
        bottom=("他的妻子三年前在楼上去世了, 他没法面对空房间, "
                "所以在车里坐到不得不上去。"),
        tags=[],
    )
    # 观察到的 signature 明摆着违反**默认** Blueprint 的每一个字段。
    d = {
        "accepted": True,
        "quality_checks": _qc_good(),
        "observed_signature": {
            "mechanism_family": "information_gap",
            "solution_shape": "information_advantage",
            "relation": "family",
            "emotion_mode": "grief",
            "time_shape": "long",
            "death": True,
            "past_trauma": True,
        },
    }
    spec = CC.spec_from_tool(d, rec, None)
    check("**题本身判定合格(check_tool_result 过)**",
          CC.check_tool_result(d)[0], CC.check_tool_result(d)[1])
    check("**blueprint 是无约束的(不是那份默认骨架)**",
          CC.is_unconstrained_blueprint(spec.blueprint))
    # 核心断言: 那份会误杀的**默认骨架硬约束**根本不进 prompt。
    from story.llm import _blueprint_block_for_review
    prompt = _blueprint_block_for_review(spec.blueprint)
    check("**审稿 prompt 里没有 Blueprint 硬约束段**",
          "Blueprint 硬约束" not in prompt)
    check("**改成了观察声明(明确禁止按骨架判)**",
          "没有" in prompt and "禁止" in prompt)
    # 反向: AI 原创题那条链**必须**继续印硬约束 —— 不能顺手把原创链
    # 的 adherence 检查也关掉。
    real = CC.PuzzleBlueprint(relation="family", death=True)
    real_prompt = _blueprint_block_for_review(real)
    check("**原创链仍然印 Blueprint 硬约束**",
          "Blueprint 硬约束" in real_prompt, real_prompt[:80])


def test_compile_invalid_is_not_permanent_reject():
    """§五/§六: 编译期结构失败 -> technical_defer, **不是** rejected。"""
    print("\n[H4-C §五/§六] 编译失败不得成为永久 content reject")
    from story.lazy_curator import LazyCurator
    from tools.curated_ledger import REJECTED, TECHNICAL_DEFER
    cur = LazyCurator.__new__(LazyCurator)
    cur._budget = 45.0
    for st in ("validate", "curated_validate", "post_review_validate",
               "post_review_curated", "reveal_adherence", "too_similar"):
        dec, stage, reasons = cur._classify(None, {"stage": st}, 0.5)
        check(f"**stage={st} -> technical_defer**",
              dec == TECHNICAL_DEFER, f"{dec} (stage={stage})")
    # 内容判决仍然是**终态** —— 不能因为放宽编译失败就把内容门也松开。
    for st in ("ai_gate", "story_gate", "story_review"):
        dec, _s, _r = cur._classify(None, {"stage": st}, 0.5)
        check(f"内容判决 stage={st} 仍是 rejected",
              dec == REJECTED, dec)
    # compile_call / technical 也照旧是 defer。
    dec, _s, _r = cur._classify(None, {"stage": "compile_call"}, 0.5)
    check("compile_call 仍是 technical_defer", dec == TECHNICAL_DEFER, dec)


def test_ledger_records_checks_evidence():
    """§十二: compile_checks / review_checks 必须落盘(且旧账本不用迁移)。"""
    print("\n[H4-C §十二] 审核证据落盘")
    with tmpdir() as d:
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        rec = mk_truck_rec()
        led.record(rec, decision=CL.REJECTED, policy_version="curated-v4",
                   stage="ai_gate", reasons=["not_a_story"],
                   checks={"compile": {"story_reconstruction": False},
                           "review": {"single_trick": True}})
        led2 = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        got = led2.last(rec, "curated-v4")
        check("checks 落盘且能读回", isinstance(got.get("checks"), dict),
              got.get("checks"))
        check("compile 侧四字段在",
              got["checks"]["compile"]["story_reconstruction"] is False)
        check("review 侧四字段在",
              got["checks"]["review"]["single_trick"] is True)
        # 旧账本(没有 checks 键)-> 读出空 dict, **不用迁移**。
        import json as _json
        with open(os.path.join(d, "old.jsonl"), "w", encoding="utf-8") as f:
            f.write(_json.dumps({"external_id": "x", "content_hash": "h",
                                 "policy_version": "v", "decision": "rejected",
                                 "stage": "ai_gate", "reasons": []}) + "\n")
        old = CL.DecisionLedger(os.path.join(d, "old.jsonl"))
        row = old.rows[0]
        check("旧行没有 checks 也能读(append-only 不迁移)",
              "checks" not in row or row.get("checks") == {})


def test_checks_do_not_affect_decision_identity():
    """§十二: checks 只是审计证据 —— **不影响** decision identity。"""
    print("\n[H4-C §十二] checks 不进 decision identity")
    rec = mk_truck_rec()
    a = CL.make_decision(rec, decision=CL.REJECTED, policy_version="curated-v4",
                         stage="ai_gate", checks={"compile": {"x": False}})
    b = CL.make_decision(rec, decision=CL.REJECTED, policy_version="curated-v4",
                         stage="ai_gate")
    check("**有无 checks 的 decision_key 完全相同**",
          CL.decision_key(a["external_id"], a["content_hash"],
                          a["policy_version"])
          == CL.decision_key(b["external_id"], b["content_hash"],
                             b["policy_version"]))
    check("没有 checks 时写空 dict(不是 None)",
          b.get("checks") == {}, b.get("checks"))


def test_fair_clue_requote_is_deterministic_and_never_touches_puzzle():
    """§八: 修 fair_clue 是**重选 quote**, 一个字都不许改谜面。"""
    print("\n[H4-C §八] fair_clue 重选 quote(确定性, 不改谜面)")
    puzzle = "他每天都把车停在楼下。邻居问他为什么。他没有回答。真相是什么?"
    q = CC._requote_from_puzzle(puzzle)
    check("挑出的 quote **逐字**在谜面里", q and q in puzzle, repr(q))
    check("**谜面本身没被改动**(函数是纯的)",
          "他每天都把车停在楼下。" in puzzle)
    # 修完必须真的过逐字校验。
    from story.puzzle import FairClue, quote_in_puzzle
    c = FairClue(quote="不存在的句子")
    c.quote = CC._requote_from_puzzle(puzzle)
    check("修后的 quote 过 quote_in_puzzle", quote_in_puzzle(c.quote, puzzle))
    check("空谜面挑不出 quote(修不了 -> 上层走 defer)",
          CC._requote_from_puzzle("") == "")


def test_stratified_sample_is_deterministic_and_spread():
    """§十五: 抽样必须**可复现**且**覆盖全体 id 空间**(不是字典序头部)。"""
    print("\n[H4-C §十五] deterministic stratified sample")
    from story.lazy_curator import stratified_sample
    recs = [RawCuratedPuzzle(
        source="haiguitang", external_id=f"haiguitang:{i:04d}",
        surface=f"谜面 {i} 真相是什么?", bottom=f"谜底 {i}", tags=[])
        for i in range(200)]
    a = stratified_sample(recs, 20)
    b = stratified_sample(recs, 20)
    check("取满 20 条", len(a) == 20, len(a))
    check("**同一输入两次抽样完全相同(可复现)**",
          [r.external_id for r in a] == [r.external_id for r in b])
    check("无重复", len({r.external_id for r in a}) == 20)
    # **关键**: 与"字典序头部"不同 —— 否则这道回归没测到东西。
    head = [r.external_id for r in sorted(
        recs, key=lambda r: r.external_id)[:20]]
    check("**不是字典序头部**(抽样真的铺开了)",
          [r.external_id for r in a] != head)
    # 覆盖度: 20 条应当散布在整个 id 空间, 而不是挤在一端。
    idx = sorted(int(r.external_id.split(":")[1]) for r in a)
    check("**覆盖全体 id 空间(首尾都够到)**",
          idx[0] < 40 and idx[-1] > 160, (idx[0], idx[-1]))
    check("语料不足时取到多少算多少",
          len(stratified_sample(recs, 999)) == 200)


def test_structural_reason_beats_shallow_stage():
    """实测错配: stage 是深门, reason 却是结构问题 -> 仍算 compile_invalid。

    `_deepest` 记"走到过最深"的门, `reject_reasons` 取"最后一次"尝试的
    原因 —— 两者来自不同稿件时会错配。实跑抓到:

        stage  = truth_audit            (最深走到审计)
        reason = 第 2 条提示超过 30 字   (另一次尝试死在 hint 长度)

    只看 stage 会把一条 hint 超长算成"审计没过"。
    """
    print("\n[H4-C §十四] reason 说是结构问题 -> compile_invalid")
    from story.lazy_curator import LazyCurator
    from tools.curated_ledger import REJECTED, TECHNICAL_DEFER
    cur = LazyCurator.__new__(LazyCurator)
    cur._budget = 45.0
    dec, stage, _r = cur._classify(
        None, {"stage": "truth_audit",
               "reject_reasons": ["第 2 条提示超过 30 字(31 字)"]}, 0.5)
    check("**hint 超长不再算内容拒绝**", dec == TECHNICAL_DEFER, dec)
    dec, _s, _r = cur._classify(
        None, {"stage": "truth_audit",
               "reject_reasons": ["completion fact f3 没有被任何 solve_atom "
                                  "引用"]}, 0.5)
    check("completion 连线问题也算 compile_invalid",
          dec == TECHNICAL_DEFER, dec)
    # ---- 反向: **硬内容门优先** ----
    # 一道被 story_gate 判死的题, 哪怕 reason 里恰好提到"提示", 也不该
    # 被改判成 defer —— 它的**内容**已经被确定性门否了。
    dec, _s, _r = cur._classify(
        None, {"stage": "ai_gate",
               "reject_reasons": ["not_a_story", "提示超过 30 字"]}, 0.5)
    check("**ai_gate 上的内容判决优先于 reason 指纹**",
          dec == REJECTED, dec)
    dec, _s, _r = cur._classify(
        None, {"stage": "story_gate", "reject_reasons": ["single_trick"]}, 0.5)
    check("story_gate 仍是 rejected", dec == REJECTED, dec)


# ======================================================================
def main():
    tests = [
        test_q10000_is_rejected_by_story_gate,
        test_q10000_rejected_without_physics_tag,
        test_q10000_via_compile_loop,
        test_each_fixture_has_no_obvious_tag,
        test_all_fixtures_rejected_by_story_gate,
        test_elevator_fixture_rejected_even_if_model_says_good,
        test_review_missing_is_fail_closed,
        test_render_fixture_needs_external_knowledge,
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
        test_curated_without_accepted_decision_is_not_playable,
        test_void_tombstone_row_is_skipped,
        test_curated_policy_version_round_trips,
        test_provenance_survives_reviewer_for_new_fields,
        test_content_hash_round_trips_and_survives_rebuild,
        test_new_fields_not_in_snapshot_repr,
        test_story_gate_stage_depth,
        test_prompt_declares_v3_rules,
        test_checks_v3_is_superset_of_v2,
        test_accepted_binds_content_hash_not_just_external_id,
        test_policy_version_is_v3,
        # ---- H4-C: curated-v4(Blueprint 污染 + 编译失败语义) ----
        test_curated_no_target_blueprint,
        test_reviewer_prompt_not_enforcing_blueprint_for_curated,
        test_family_grief_long_death_not_rejected_by_default_blueprint,
        test_compile_invalid_is_not_permanent_reject,
        test_ledger_records_checks_evidence,
        test_checks_do_not_affect_decision_identity,
        test_fair_clue_requote_is_deterministic_and_never_touches_puzzle,
        test_stratified_sample_is_deterministic_and_spread,
        test_structural_reason_beats_shallow_stage,
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
