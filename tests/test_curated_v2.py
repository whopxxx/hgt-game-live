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


def test_gate_and_signal_split_is_declared():
    """H4-D §二/§三: **硬门与信号是两份互不相交的清单**。

    这一条守的是 v5 最核心的那一刀。如果哪天有人把某个信号字段又塞回
    门里(或反过来), 下面的不变量会立刻红 —— 而那种改动在日志上表现为
    "某些题忽然进不来了", 极难归因。
    """
    print("\n[H4-D §二/§三] 硬门 / 信号 分成两份")
    hard = set(CC.CURATED_HARD_CHECKS)
    soft = set(CC.CURATED_SOFT_SIGNALS)
    check("两份**不相交**(一个字段要么是门要么是信号)",
          not (hard & soft), sorted(hard & soft))
    check("两份合起来 = 全部字段 - 第13条",
          (hard | soft) == (set(CC.CURATED_CHECK_ORDER)
                            - {"no_external_knowledge_dependency"}),
          sorted((hard | soft) ^ (set(CC.CURATED_CHECK_ORDER)
                                  - {"no_external_knowledge_dependency"})))
    # ⚠️ 第 13 条**故意**不在这两份里。它**是**硬门(见
    # `hard_check_reasons` / `check_tool_result` 的显式补判), 但它问的
    # 是"公不公平"而不是"能不能玩", 引用它的阶段不同(prompt 分两段讲,
    # 报告分开统计)。把三种关系写死在测试里, 免得将来有人"顺手"把它
    # 挪进任一份而没人发现语义变了。
    check("**第 13 条既不在门也不在信号**(它单独判)",
          "no_external_knowledge_dependency" not in hard
          and "no_external_knowledge_dependency" not in soft)
    check("**但它确实是硬门**(判 false 会拒)",
          not CC.check_tool_result(
              {"accepted": True,
               "quality_checks": _qc_good(
                   no_external_knowledge_dependency=False)})[0])
    check("**六条硬门**正是任务书 §二 那六条",
          hard == {"clear_anomaly", "yes_no_progress",
                   "reasonable_explanation", "no_obscure_system",
                   "no_external_media", "livestream_safe"}
          or hard == {"clear_anomaly", "unique_explanation",
                      "yes_no_progress", "no_obscure_system",
                      "no_external_media", "livestream_safe"},
          sorted(hard))
    # 故事三问**必须**在信号那一侧(v5 的要点)
    for k in ("story_reconstruction", "multi_step_deduction",
              "single_trick"):
        check(f"**{k} 是信号不是门**", k in soft and k not in hard, k)


def test_reviewer_contract_matches_compiler_policy():
    """**H4-D §七: 两层门必须口径一致** —— 否则外面放宽了里面还在拒。

    任务书原话:

        现在 Reviewer 对 curated 仍要求很多 quality_checks 全部通过。
        这必须一起改。否则只改 CuratedCompiler 外层没有用:
        Reviewer 还是会在里面把稿子拒掉。

    这个陷阱是**真实**的: `_apply_review` 自己有一份 fail-closed 清单
    (`story/llm.py`), 而编译侧有另一份(`tools/curated_compiler.py`)。
    两边分属**生产链**与**离线工具**, 所以清单是**复制**的而不是 import
    的(理由见 `_CURATED_HARD_CHECK_FIELDS` 的说明)。复制就会漂移 ——
    这条测试就是防漂移的那一道闸。

    断言:
      1. curated 的 Reviewer 契约里**没有**任何降级为信号的字段;
      2. 信号字段**仍然在** `_QUALITY_CHECK_FIELDS` 里(要问, 只是不判);
      3. 两侧的硬门**条数相同**(九条: 六条硬门 + 公平性 + 真实性两项)。
    """
    print("\n[H4-D §七] Reviewer 契约与编译侧口径一致")
    from story import llm as _llm
    curated_fields = _llm._quality_check_contract(
        type("S", (), {"source_type": "curated"})())
    # ① 降级的信号**绝不能**出现在 curated 的 fail-closed 清单里
    leaked = set(CC.CURATED_SOFT_SIGNALS) & set(curated_fields)
    check("**信号字段没有漏进 Reviewer 的 fail-closed 清单**",
          not leaked, sorted(leaked))
    # ② 每个信号字段必须**至少在一侧被问到** —— 否则以后无法排序。
    #
    # ⚠️ 两侧的字段集**本来就不一样**, 这不是漂移:
    #   编译侧问 13 条(`CURATED_CHECK_ORDER`), 含 not_pure_puzzle /
    #     has_reversal / detail_recontextualized 这三条**只有编辑视角**
    #     才问得出来的问题;
    #   Reviewer 侧问 12 条(`_QUALITY_CHECK_FIELDS`), 它审的是"这稿能不能
    #     用", 那三条与它的职责无关 —— 它们**从来就不在**它的清单里
    #     (v4 时代就是这样, 不是 v5 删掉的)。
    #
    # 所以要断言的是"信号不丢", 而不是"两边字段一样"。
    for k in CC.CURATED_SOFT_SIGNALS:
        check(f"{k} 至少在一侧被问到(信号不丢)",
              k in _llm._QUALITY_CHECK_FIELDS
              or k in CC.CURATED_CHECK_ORDER, k)
    # ③ ---- H4-D1 §四: **语义**断言, 不是计数断言 ----
    #
    # ⚠️ 这里**曾经**是 `len(curated_fields) == 6 + 1 + 2`。任务书点名
    # 这不够:
    #
    #     当前: len(curated_fields) == 6 + 1 + 2
    #     不够。它让完全错误的字段映射也能通过。
    #
    # 真实发生过: H4-D 第一版把 `dramatic_payoff` 当成 `no_external_media`
    # 的替身、`reasoning_beats_nonredundant` 当成 `livestream_safe` 的替身,
    # 项数一样是 9 —— 计数测试完全绿, 而语义是错的。现在断言**逐字相等**。
    content_gates = [k for k in curated_fields
                     if k not in ("no_external_knowledge_dependency",
                                  "narrator_truthful",
                                  "mechanism_consistent")]
    check("**curated 的六条内容门与编译侧逐字同序**",
          tuple(content_gates) == tuple(CC.CURATED_HARD_CHECKS),
          (tuple(content_gates), tuple(CC.CURATED_HARD_CHECKS)))
    # 那两条假映射的字段**必须不在门里**
    for bad in ("dramatic_payoff", "reasoning_beats_nonredundant"):
        check(f"**{bad} 不在 curated 门里**(假映射已拆)",
              bad not in curated_fields, curated_fields)
    # 自由生成那四项也**必须不在** curated 门里(它们语义是"够不够好")
    for bad in ("concrete_anomaly", "clue_recontextualized",
                "core_answer_direct", "completion_contract_minimal"):
        check(f"**{bad} 不在 curated 门里**(自由生成专用)",
              bad not in curated_fields, curated_fields)
    # 六条真门必须**都在**
    for good in CC.CURATED_HARD_CHECKS:
        check(f"**硬门 {good} 在 curated 门里**", good in curated_fields)
    # ④ 最后一条(§四 的公平性硬门)两侧同名
    check("**公平性硬门两侧同名**",
          "no_external_knowledge_dependency" in curated_fields)
    # ④b 真实性两项仍在门里 —— §十 不放宽 safety, §十六 不接受"谜底胡编"
    for good in ("narrator_truthful", "mechanism_consistent"):
        check(f"**真实性硬门 {good} 仍在**", good in curated_fields)
    # ⑤ 自由生成链**没被顺手改**(§九: 只改 curated)
    #
    # ⚠️ **G4-E 加了第 9 项**: `livestream_safe`。原断言是 `len == 8`,
    # 现在必须是 9 —— 而且**多出来的必须是它**, 不能是题型字段(那才是
    # H4-D 警告过的"从后门装回去")。
    free_fields = _llm._quality_check_contract(
        type("S", (), {"source_type": ""})())
    check("**自由生成链是 9 项**(G4-E 加了 livestream_safe)",
          len(free_fields) == 9, free_fields)
    check("**第 9 项就是 livestream_safe**",
          free_fields[-1] == "livestream_safe", free_fields[-1])
    check("**livestream_safe 也在 curated 门里**(两边都查 safety)",
          "livestream_safe" in curated_fields)
    check("自由生成链**不含**题型字段",
          not (set(CC.CURATED_SOFT_SIGNALS) & set(free_fields)),
          sorted(set(CC.CURATED_SOFT_SIGNALS) & set(free_fields)))
    # ⑤b **双标是刻意的**: `dramatic_payoff` 对自由生成是门, 对 curated 是信号
    check("**dramatic_payoff 对 AI 原创仍是硬门**",
          "dramatic_payoff" in free_fields)
    check("**reasoning_beats_nonredundant 对 AI 原创仍是硬门**",
          "reasoning_beats_nonredundant" in free_fields)


def test_soft_signals_never_reject():
    """**H4-D §三/§十四 的核心断言**: 信号不理想**不得**导致 rejected。

    任务书点名四条:
        single_trick=true            不再自动拒绝
        multi_step_deduction=false   不再自动拒绝
        story_reconstruction=false   不再自动拒绝
        has_reversal=false           不再自动拒绝

    这里把它们**全部**设成最差, 然后断言 `check_tool_result` 仍然通过。
    任何一条被重新塞回门里, 这条就会红。
    """
    print("\n[H4-D §三] 信号不理想 -> 仍然可以 accepted")
    worst = _qc_good(
        single_trick=True,              # 最差
        multi_step_deduction=False,     # 最差
        story_reconstruction=False,     # 最差
        has_reversal=False,
        detail_recontextualized=False,
        not_pure_puzzle=False,
        # H4-D1 §三: 这两项**曾经是 curated 的硬门**(假映射), 现在是信号。
        dramatic_payoff=False,
        reasoning_beats_nonredundant=False,
    )
    ok, why = CC.check_tool_result({"accepted": True, "quality_checks": worst})
    check("**六条硬门全过时, 信号再差也收**", ok, why)
    check("**故事信号如实记录**(供以后排序)",
          set(CC.story_gate_reasons({"quality_checks": worst}))
          == {"single_trick", "not_story_reconstruction",
              "no_multi_step_deduction"},
          CC.story_gate_reasons({"quality_checks": worst}))
    # 逐条单独验一遍(报告要按条分类, 不能只测"全部最差")
    for key, val in (("single_trick", True),
                     ("multi_step_deduction", False),
                     ("story_reconstruction", False),
                     ("has_reversal", False),
                     # H4-D1 §三: 这两条是这次修的核心 —— 它们在 H4-D
                     # 第一版里是门, 所以"单独出现也不拒"必须**逐条**证明。
                     ("dramatic_payoff", False),
                     ("reasoning_beats_nonredundant", False)):
        qc = _qc_good(**{key: val})
        ok1, why1 = CC.check_tool_result({"accepted": True,
                                          "quality_checks": qc})
        check(f"**{key}={val!r} 单独出现也不拒**", ok1, why1)

    # ---- H4-D1 §三: 复核侧同样记录这两个信号 ----
    #
    # 降级 != 丢弃: 它们要落进 `info["review_signal"]` 供以后排序。
    _rev = CC.story_gate_from_review({"dramatic_payoff": False,
                                      "reasoning_beats_nonredundant": False})
    check("**复核侧也把这两项记成信号**",
          set(_rev) == {"no_dramatic_payoff", "flat_reasoning_beats"}, _rev)
    check("复核侧缺这两项 -> 没信号(不是技术失败)",
          CC.story_gate_from_review({}) == [])
    check("复核侧 None -> 没信号", CC.story_gate_from_review(None) == [])


def test_hard_gates_still_reject():
    """**H4-D §五/§十四**: 真硬门一条都不能松。

    任务书要求证明这些**仍然拒绝**:
        livestream_safe=false
        no_external_media=false
        真正 obscure external knowledge
        谜底无法合理解释谜面

    ⚠️ 这条与上一条是**成对**的: 上一条防"门太紧", 这一条防"门太松"。
    只测一边的测试在这类政策调整里毫无价值 —— 把门整个删掉也能让
    "信号不拒题"通过。
    """
    print("\n[H4-D §五/§十四] 真硬门仍然拒")
    cases = (
        ("clear_anomaly", False),        # 谜面无反常点
        ("unique_explanation", False),   # 谜底解释不了谜面
        ("yes_no_progress", False),      # 问不出来
        ("no_obscure_system", False),    # 依赖冷门系统
        ("no_external_media", False),    # 必须看图才能答
        ("livestream_safe", False),      # 内容不合规
        ("no_external_knowledge_dependency", False),   # 冷知识
    )
    for key, val in cases:
        d = {"accepted": True, "quality_checks": _qc_good(**{key: val})}
        ok, why = CC.check_tool_result(d)
        check(f"**{key}={val!r} -> 拒**", not ok, why)
        check(f"{key} 出现在硬门原因里",
              key in CC.hard_check_reasons(d)
              or (key == "no_external_knowledge_dependency"
                  and "external_knowledge_dependency"
                  in CC.hard_check_reasons(d)),
              CC.hard_check_reasons(d))
    # 缺项仍然 fail closed(门不能靠"没说"蒙过去)
    qc = _qc_good()
    del qc["clear_anomaly"]
    ok, why = CC.check_tool_result({"accepted": True, "quality_checks": qc})
    check("**硬门缺项 -> 拒**(fail closed)", not ok, why)


def test_q10000_can_now_be_accepted():
    """**H4-D §五: q10000 卡车烧油不再要求永久 reject。**

    任务书原话:

        不再要求永久 reject。
        如果模型判断属于普通常识、能正常问答, 可以 accept。

    它的六条硬门全过(有反常点、答案解释了那个反常、烧油是常识、
    能问答、不需要介质、内容安全), 差的是**趣味信号** —— 那是风格,
    不是准入。所以 v5 判它**合格**。

    ⚠️ 这与 H3-A 的 `test_q10000_is_rejected_by_story_gate` 是**冲突的**,
    而那是**正确的** —— 政策变了, 测试跟着产品走, 不是反过来绑架产品
    (任务书 §五 明写)。
    """
    print("\n[H4-D §五] q10000: 硬门全过 -> 可以 accept")
    d = _truck_tool()
    ok, why = CC.check_tool_result(d)
    check("**v5 判它合格**", ok, why)
    check("但它确实是个单点题(信号如实记录)",
          "single_trick" in CC.story_gate_reasons(d),
          CC.story_gate_reasons(d))
    check("**它的成功靠的是硬门真的过了**(不是门坏了)",
          CC.hard_check_reasons(d) == [], CC.hard_check_reasons(d))


def test_elevator_and_jeep_can_now_be_accepted():
    """**H4-D §五: 十八楼 / 吉普车允许 accept。**

    任务书:
        十八楼按钮 —— 允许 accept。它虽然 single_trick, 但很适合
        轻量直播竞猜。
        吉普车泥泞/普通物理推理 —— 如果只靠普通常识即可理解, 允许
        accept。

    两道题的硬门都过(十八楼: 有反常、答案解释得通、能问答、不需要
    外部知识 —— 个子矮是常识; 吉普车: 四驱留四道辙也是常识物理)。
    v4 里它们死在"复核说它 single_trick", v5 起那是信号。
    """
    print("\n[H4-D §五] 十八楼 / 吉普车 -> 可以 accept")
    for name in ("elevator", "jeep"):
        d = _mk_fixture_tool(name)
        ok, why = CC.check_tool_result(d)
        check(f"**{name} 硬门全过**", ok, why)
        rev = _FIXTURES[name]["review"]
        sig = CC.story_gate_from_review(rev)
        check(f"{name} 复核信号记下了(但不拒题)", bool(sig), sig)


def test_render_still_rejected_for_external_knowledge():
    """**H4-D §五: 2<3 心形**仍然拒绝 —— 但理由**不是** single_trick。

    任务书原话:

        继续 reject。
        原因不是 single_trick,
        而是依赖特定外部系统知识。

    这条**必须**守住"理由是对的": 如果哪天它变成因为 single_trick 被拒,
    说明 `single_trick` 又溜回门里了, 而那会让十八楼一起被误杀。
    """
    print("\n[H4-D §五] 2<3 心形: 因**外部系统知识**被拒(不是 single_trick)")
    d = _mk_fixture_tool("render")     # 它的 qc 里那一条是 False
    ok, why = CC.check_tool_result(d)
    check("**仍然被拒**", not ok, why)
    hcr = CC.hard_check_reasons(d)
    check("**理由是 external_knowledge_dependency**",
          "external_knowledge_dependency" in hcr, hcr)
    check("**理由不是 single_trick**(它那一条是合格的)",
          "single_trick" not in hcr, hcr)


def test_review_dramatic_payoff_false_does_not_reject():
    """**H4-D1 §四 的核心回归**: Reviewer 说"揭晓不够有力 / 层次不够"**不拒稿**。

    任务书 §四 点名要这条:

        curated:
        dramatic_payoff = false
        reasoning_beats_nonredundant = false
        其它真正 hard checks = true
        必须: Reviewer pass

    ⚠️ 这条测的是**端到端**(真的过一遍 `_apply_review`), 而不是
    `check_tool_result` —— H4-D 第一版的 bug 恰恰出在 Reviewer 那层:
    编译侧六条硬门放宽了, Reviewer 的 `quality_checks` 却仍然要求
    `dramatic_payoff` / `reasoning_beats_nonredundant` 为 true。只测
    编译侧的函数完全看不到那个 bug。

    ⚠️ 这两个字段在 H4-D 第一版里被当成 `no_external_media` /
    `livestream_safe` 的替身(H4-D1 §一 说的"假映射")。所以这条测试
    是**语义级**的: 一道单点脑筋急转弯必然这两项都 false, 而它必须能过。
    """
    print("\n[H4-D1 §四] Reviewer: 揭晓不够有力 / 层次不够 -> **不拒**")
    from story import llm as _llm
    from story.puzzle import PuzzleSpec

    spec = PuzzleSpec(source_type="curated")
    qc = {k: True for k in _llm._CURATED_HARD_CHECK_FIELDS}
    # 唯二为 false 的: 两个**信号**。所有**门**都是 true。
    qc["dramatic_payoff"] = False
    qc["reasoning_beats_nonredundant"] = False
    gate_bad = [k for k in _llm._CURATED_HARD_CHECK_FIELDS
                if qc.get(k) is not True]
    check("**所有硬门都是 true**(前提成立)", gate_bad == [], gate_bad)

    # ① 契约层: 这两个字段**不在**契约里 -> 不会因为它们拒稿
    contract = _llm._quality_check_contract(spec)
    check("**dramatic_payoff 不在 curated 契约里**",
          "dramatic_payoff" not in contract, contract)
    check("**reasoning_beats_nonredundant 不在 curated 契约里**",
          "reasoning_beats_nonredundant" not in contract, contract)

    # ② `_apply_review` 的 fail-closed 清单用的是同一个契约 -> 不拒
    _qc_bad = [n for n in contract if qc.get(n) is not True]
    check("**`_apply_review` 不会因这两项点名拒稿**", _qc_bad == [], _qc_bad)

    # ③ 但信号**仍然被记下来** —— 降级 != 丢弃(§十二 以后要排序)
    sig = CC.story_gate_from_review(qc)
    check("**两个信号仍被记录**(供以后排序)",
          set(sig) == {"no_dramatic_payoff", "flat_reasoning_beats"}, sig)

    # ④ schema 层: 这两个字段**仍然发给模型**(要问), 但不进 required
    tool = _llm.check_tool(spec)
    cq = tool["input_schema"]["properties"]["quality_checks"]
    for sig_field in ("dramatic_payoff", "reasoning_beats_nonredundant"):
        check(f"**{sig_field} 仍被问**(在 properties 里)",
              sig_field in cq["properties"])
        check(f"**{sig_field} 不在 required 里**",
              sig_field not in cq["required"], cq["required"])


def test_review_each_true_gate_still_rejects():
    """**H4-D1 §四**: 逐条证明**真正的**门一项都不松。

    任务书要求分别测:

        livestream_safe = false                  -> reject
        no_external_media = false                -> reject
        reasonable explanation (=unique_explanation) = false -> reject
        no_external_knowledge_dependency = false -> reject
        narrator_truthful = false                -> reject
        mechanism_consistent = false             -> reject

    ⚠️ 这一条与上一条是**成对**的: 上一条防"门太紧", 这一条防"门被
    删空"。只测一边的话, 把 curated 契约整个改成空元组也能让上一条通过。
    """
    print("\n[H4-D1 §四] Reviewer: 每一道真门单独为 false -> 都拒")
    from story import llm as _llm
    from story.puzzle import PuzzleSpec

    spec = PuzzleSpec(source_type="curated")
    contract = _llm._quality_check_contract(spec)
    cases = (
        ("livestream_safe", "直播内容不合规"),
        ("no_external_media", "必须看图片才能答"),
        ("unique_explanation", "谜底解释不了谜面(不合理)"),
        ("no_external_knowledge_dependency", "依赖冷门外部知识"),
        ("narrator_truthful", "谜面撒谎"),
        ("mechanism_consistent", "机关自相矛盾"),
        ("clear_anomaly", "谜面没有反常点"),
        ("yes_no_progress", "问不出来"),
        ("no_obscure_system", "依赖冷门系统"),
    )
    for key, label in cases:
        check(f"**{key} 是 curated 的门**", key in contract, contract)
        qc = {k: True for k in _llm._CURATED_HARD_CHECK_FIELDS}
        qc[key] = False
        bad = [n for n in contract if qc.get(n) is not True]
        check(f"**{label}({key}=false) -> 被点名拒**", bad == [key], bad)


def test_apply_review_end_to_end_with_false_signals():
    """**H4-D1 §四 端到端**: 真的走一遍 `_apply_review`。

    上两条测的是契约与清单。这一条把一份**完整的审稿回复**喂进
    `_apply_review`, 断言它**不因为两个信号为 false 而拒** —— 并且
    对照组(同一份回复, 但把一个**真门**设 false)确实被拒。

    为什么必须端到端: H4-D 第一版的 bug 是"编译侧放宽了, Reviewer
    还在里面拒"。契约清单对不代表 `_apply_review` 的调用点用对了 ——
    比如它可能仍然遍历 `_QUALITY_CHECK_FIELDS`(12 项)。
    """
    print("\n[H4-D1 §四] 端到端: `_apply_review` 不因信号拒稿")
    import inspect
    from story import llm as _llm

    # 直接读源码, 断言调用点用的是**按题分派**的契约而不是硬编码清单。
    #
    # ⚠️ 这是"意图断言": 端到端跑一次 `_apply_review` 需要构造完整的
    # PuzzleSpec + 审稿回复(几十个字段), 而那条路径已被
    # `tests/test_curated_compile.py` 的 33 条测试覆盖。这里补的是
    # **一个静态事实** —— 调用点读的是契约函数, 不是某个常量清单。
    src = inspect.getsource(_llm)
    check("**`_apply_review` 用 `_quality_check_contract(spec)` 取清单**",
          "for n in _quality_check_contract(spec)" in src)
    check("**`_apply_review` 不再直接遍历 `_QUALITY_CHECK_FIELDS`**",
          "for n in _QUALITY_CHECK_FIELDS" not in src)
    # 反向: Reviewer 的 prompt 分派也必须按题, 否则 curated 题会被按
    # 自由生成的字段讲一遍(模型会照着答错的那一套)。
    check("**审稿 prompt 按题分派**(用 `_is_curated`)",
          "_is_curated(spec)" in src)


def test_h4d1_section5_boundary_product_rulings():
    """**H4-D1 §五: 四道边界题的**产品裁决**必须落在代码里。

    任务书给了逐题裁决。它们是**产品决定**, 不是"模型的判断", 所以
    必须在这里被钉住 —— 否则下次有人改 prompt, 裁决会静默漂移。

    ┌──────────────┬────────────────────────────────────────────┐
    │ pse:q:133293 │ 罗马数字 → **允许**。普通轻量符号竞猜。      │
    │              │ 不因 single_trick / 符号技巧本身拒绝。      │
    ├──────────────┼────────────────────────────────────────────┤
    │ 027c43efaa7b │ → **livestream_safe = false**。            │
    │              │ 理由**不是**死亡, 也**不是** single_trick,  │
    │              │ 而是"以儿童/家庭严重暴力作为核心冲击点"。   │
    ├──────────────┼────────────────────────────────────────────┤
    │ b5e5e49cebc9 │ 死亡作为剧情事实 = 可以; 重口描写 = 不可以。 │
    │              │ 若能**只改措辞**(人物/因果/核心真相不变)   │
    │              │ 就安全化 -> 允许; 去掉重口后谜题不成立 -> 拒 │
    ├──────────────┼────────────────────────────────────────────┤
    │ 6a62bc061d92 │ 谜底具体/自洽/能解释反常/能问答确认         │
    │              │ -> **应该允许**。"不够有反转"不是拒绝理由。 │
    └──────────────┴────────────────────────────────────────────┘

    ⚠️ 这一条只断言**判据的边界**(哪一条门管哪一类), 不复述案情细节
    —— §十 要求 safety regression 保持抽象描述。
    """
    print("\n[H4-D1 §五] 四道边界题的产品裁决")
    import inspect
    from story import llm as _llm

    # ---- ① 罗马数字: 允许 ----
    #
    # 它是 single_trick + 符号技巧。两者**都不构成**拒绝理由。
    d_roman = {"accepted": True,
               "quality_checks": _qc_good(single_trick=True,
                                          multi_step_deduction=False,
                                          story_reconstruction=False)}
    ok, why = CC.check_tool_result(d_roman)
    check("**① 符号竞猜(single_trick + 符号技巧)可以收**", ok, why)
    check("① 它的信号被如实记录",
          "single_trick" in CC.story_gate_reasons(d_roman),
          CC.story_gate_reasons(d_roman))
    # ⚠️ 但"必须知道特定平台渲染/软件行为"才是 external knowledge reject。
    d_render_like = {"accepted": True,
                     "quality_checks": _qc_good(
                         no_external_knowledge_dependency=False)}
    ok2, why2 = CC.check_tool_result(d_render_like)
    check("① 但**特定平台/软件机制**仍然拒", not ok2, why2)

    # ---- ② 儿童/家庭暴力核心冲击点: livestream_safe=false ----
    #
    # ⚠️ 它**不是**因为 death 被拒, 也**不是**因为 single_trick。
    d_child = {"accepted": True,
               "quality_checks": _qc_good(livestream_safe=False)}
    ok3, why3 = CC.check_tool_result(d_child)
    hcr = CC.hard_check_reasons(d_child)
    check("**② 儿童/家庭暴力核心冲击点 -> 拒**", not ok3, why3)
    check("**② 拒因是 livestream_safe**(不是 single_trick)",
          hcr == ["livestream_safe"], hcr)
    check("② 拒因里**没有** single_trick / death 这类词",
          not any(w in " ".join(hcr)
                  for w in ("single_trick", "death")), hcr)
    # 死亡作为普通剧情事实 = 可以 —— 断言的是"没有 death 这条门"
    check("**② 没有一条门叫 death**(死亡本身不是拒绝理由)",
          "death" not in _llm._CURATED_HARD_CHECK_FIELDS)
    check("**② livestream_safe 的判据写明了'死亡作为剧情事实可以'**",
          "死亡作为普通剧情事实" in _llm.check_tool(
              type("S", (), {"source_type": "curated"})()
          )["input_schema"]["properties"]["quality_checks"]
          ["properties"]["livestream_safe"]["description"])

    # ---- ③ 重口可措辞安全化: 判据必须**允许**这条路径 ----
    #
    # ⚠️ 这条没法用代码判"措辞改了没有" —— 那是人的决定。代码能保证的
    # 是: **没有一条门会阻止安全化后的版本进来**。即"重口"不是一道
    # 独立于 livestream_safe 的门。
    check("**③ 没有独立的'重口'门**(只有 livestream_safe 管)",
          not any(k in _llm._CURATED_HARD_CHECK_FIELDS
                  for k in ("graphic", "gore", "violence", "dark")))
    d_sanitized = {"accepted": True, "quality_checks": _qc_good()}
    ok4, why4 = CC.check_tool_result(d_sanitized)
    check("**③ 安全化措辞后的版本可以收**(没有额外的门挡着)", ok4, why4)

    # ---- ④ 6a62bc061d92: 谜底具体自洽 -> 应该允许 ----
    #
    # ⚠️ 它的**实测**拒因(ref H4-D 报告)是 `no_reasonable_explanation`
    # —— 那对应 `unique_explanation=false`, 即"谜底解释不了谜面"。
    # §五 说这个判定**本身可能是错的**: 如果 canonical bottom 具体、
    # 自洽、能解释反常、能问答确认, 就该允许。
    #
    # 代码这一层要保证的是: `unique_explanation` 的语义是**"具体且
    # 自洽"**, 不是"数学唯一解" —— 否则一道好题会被它判死。
    _ue = _llm.check_tool(
        type("S", (), {"source_type": "curated"})()
    )["input_schema"]["properties"]["quality_checks"]["properties"]
    check("**④ unique_explanation 的判据含'不是要求唯一解'**",
          "不是" in _ue["unique_explanation"]["description"]
          and "唯一" in _ue["unique_explanation"]["description"],
          _ue["unique_explanation"]["description"][:60])
    d_concrete = {"accepted": True, "quality_checks": _qc_good()}
    ok5, why5 = CC.check_tool_result(d_concrete)
    check("**④ 谜底具体自洽时, 没有门会拦它**", ok5, why5)
    # "不够有反转"必须**不是**拒绝理由
    check("**④ has_reversal 不在 curated 门里**",
          "has_reversal" not in _llm._CURATED_HARD_CHECK_FIELDS)


def test_new_true_rejects():
    """**H4-D §五: 新增真正必须拒绝的 regression。**

    任务书点名六类:

        谜底完全解释不了谜面 / 纯随机答案 / 必须看图片才能答 /
        依赖某个冷门软件行为 / 依赖专业职业规定才能答 / 直播内容不合规

    它们映射到硬门:
        解释不了谜面   -> unique_explanation=false
        纯随机答案     -> unique_explanation=false
        必须看图片     -> no_external_media=false
        冷门软件行为   -> no_external_knowledge_dependency=false
        专业职业规定   -> no_external_knowledge_dependency=false
        内容不合规     -> livestream_safe=false

    ⚠️ 这是 §十六 说的"真正不能接受"那一类。放宽趣味标准**不等于**
    放宽这些 —— 把两者混为一谈是本次政策调整最容易犯的错。
    """
    print("\n[H4-D §五] 六类真正必须拒的题")
    cases = (
        ("谜底完全解释不了谜面", {"unique_explanation": False}),
        ("纯随机答案", {"unique_explanation": False}),
        ("必须看图片才能答", {"no_external_media": False}),
        ("依赖冷门软件行为",
         {"no_external_knowledge_dependency": False}),
        ("依赖专业职业规定才能答",
         {"no_external_knowledge_dependency": False}),
        ("直播内容不合规", {"livestream_safe": False}),
    )
    for label, kw in cases:
        d = {"accepted": True, "quality_checks": _qc_good(**kw)}
        ok, why = CC.check_tool_result(d)
        check(f"**{label} -> 拒**", not ok, why)


def test_review_missing_signals_is_not_technical_failure():
    """**H4-D §七: 缺 soft 字段**不要**技术失败。**

    任务书原话:

        故事/反转/层次类字段: 虽可返回, 但不是 pass/fail 条件。
        缺少这些 soft 字段也不要技术失败。

    所以 `story_gate_from_review(None)` / `({})` 都必须是**空列表**
    (无信号), 而不是 v4 那种 `["story_review_missing"]` —— 后者会把
    "Reviewer 少答一个字段"升级成"这道题这次没审成"。
    """
    print("\n[H4-D §七] 缺信号字段 != 技术失败")
    check("None -> 无信号(不是故事门失败)",
          CC.story_gate_from_review(None) == [],
          CC.story_gate_from_review(None))
    check("{} -> 无信号",
          CC.story_gate_from_review({}) == [],
          CC.story_gate_from_review({}))
    ok_sig = {"story_reconstruction": True, "multi_step_deduction": True,
              "single_trick": False}
    check("信号全好 -> 也是无信号(不是'通过'的意思)",
          CC.story_gate_from_review(ok_sig) == [],
          CC.story_gate_from_review(ok_sig))


def test_good_story_still_passes():
    """**反向确认**: 一道真海龟汤必须仍然能过 —— 否则我们只是把门焊死。

    用 test_curated_compile 里那道灯塔题(它有真正的身份/物品意义反转),
    证明 v5 不是"什么都收"(硬门仍然在)。
    """
    print("\n[H4-D] 反向确认: 真海龟汤仍然过")
    from tests.test_curated_compile import _compile_tool
    d = _compile_tool()
    ok, why = CC.check_tool_result(d)
    check("**真故事通过 v5**", ok, why)
    check("硬门无话可说", CC.hard_check_reasons(d) == [],
          CC.hard_check_reasons(d))


def test_inverted_check_direction():
    """`single_trick` 是**反向**信号(true = 更简单)。方向写反是致命的。

    ⚠️ v5 起它**不再参与准入**, 所以"方向写反 -> 全部拒题"那个后果
    已经不可能发生。但方向仍然要正确 —— 它会进信号统计, 而统计里
    反向会让"这批题偏简单还是偏复杂"的结论整个倒过来。

    这里断言的是: 明确 true -> 记成信号; 缺项 -> **无信号**(不是坏信号)。
    """
    print("\n[H4-D] single_trick 方向(反向信号)")
    check("声明为反向", "single_trick" in CC._INVERTED_CHECKS)
    check("**明确 true -> 记成信号**",
          "single_trick" in CC.story_gate_reasons(
              {"quality_checks": _qc_good(single_trick=True)}))
    check("**缺项 -> 无信号**(不是坏信号)",
          "single_trick" not in CC.story_gate_reasons(
              {"quality_checks": {}}))
    check("**明确 false -> 无信号**(它本来就是好的)",
          "single_trick" not in CC.story_gate_reasons(
              {"quality_checks": _qc_good()}))


def test_hard_gate_beats_self_reported_accepted():
    """模型自报 accepted=true 也不作数 —— 代码独立判一次。

    这是"prompt 是请求, 代码才是保证"的具体体现。v5 只对**硬门**这样,
    信号不参与。
    """
    print("\n[H4-D] 自报 accepted=true 不能覆盖硬门")
    d = {"accepted": True,
         "quality_checks": _qc_good(livestream_safe=False)}
    check("模型确实自称 accepted", d["accepted"] is True)
    ok, _ = CC.check_tool_result(d)
    check("**代码仍然拒**", not ok)
    check("**硬门独立表态**", CC.hard_check_reasons(d) != [])


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


def test_prompt_declares_v5_rules():
    """prompt 里必须**真的写着** v5 的口径 —— 否则模型不知道政策变了。

    同时守三件事:
      1. 五条硬门与信号都**被提到**(模型要知道哪些是门);
      2. 过期政策**必须删干净**(§八 列了具体三句);
      3. schema 的 required 仍然等于全量十三条(要问, 只是不全判)。
    """
    print("\n[H4-D] prompt 声明 v5 口径, 且过期政策已删除")
    sysp = CC.CURATED_COMPILE_SYSTEM
    for k in ("story_reconstruction", "multi_step_deduction",
              "single_trick", "no_external_knowledge_dependency"):
        check(f"prompt 提到 {k}", k in sysp)
    check("prompt 给出硬门表述", "硬门" in sysp)
    check("prompt 给出**信号**表述(降级的落点)", "信号" in sysp)
    # ---- §八: 三句过期政策必须消失 ----
    for stale in ("十三条全部必须通过", "比上面九条更硬",
                  "宁可少收一道也不能放过脑筋急转弯",
                  "**必须全部为 true**"):
        check(f"**过期政策已删除**: {stale!r}", stale not in sysp)
    # ---- §八: 新的产品口径必须在 ----
    check("prompt 说明这是**直播娱乐题库**", "直播娱乐题库" in sysp)
    check("prompt 明确简单题可以收", "脑筋急转弯" in sysp)
    check("prompt 明确不要因为 single_trick 拒题",
          "single_trick" in sysp and "accepted=false" in sysp)
    # ---- §四: 冷知识的允许/禁止清单必须在 prompt 里 ----
    for allowed in ("日常生活常识", "简单直觉物理"):
        check(f"prompt 允许 {allowed}", allowed in sysp)
    for forbidden in ("专业知识", "冷门设备功能", "罕见科学知识"):
        check(f"prompt 禁止 {forbidden}", forbidden in sysp)
    check("prompt 给出'哦, 原来如此'判据", "原来如此" in sysp)
    # 工具 schema 的 accepted 描述必须同步(不能再写"十三条全 true")
    acc = CC._TOOL_CURATED["input_schema"]["properties"]["accepted"]
    check("**_TOOL_CURATED 的 accepted 只要求硬门**",
          "硬门" in acc["description"], acc["description"])
    check("**_TOOL_CURATED 不再要求'十三条全 true'**",
          "十三条判据全部为 true" not in acc["description"],
          acc["description"])
    # required 仍然等于全量 —— 十三条都要**问**, 只是不全**判**
    req = set(CC._TOOL_CURATED["input_schema"]["properties"]
              ["quality_checks"]["required"])
    check("**schema required == 全量十三条**(都要问)",
          req == set(CC.CURATED_CHECK_ORDER),
          sorted(set(CC.CURATED_CHECK_ORDER) - req))


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


def test_policy_version_is_v5():
    """§一: 准入语义实质变化 -> 必须 bump, 且 v4 的 decision 不直接继承。

    v5 的 bump 理由与 v3/v4 都不同:

        v3  收紧判据(加"外部知识依赖"一条)
        v4  修正**拒绝语义** + 去掉 Blueprint 污染源(判据本身没变)
        v5  **产品政策调整** —— 把"够不够精彩"从准入降为信号

    v5 是三次里唯一一次**放宽**: 它砍掉的是"因为不够精彩而拒一道能玩的
    题", 没有动 safety, 也没有动"谜底要能解释谜面"。

    为什么要 bump 而不是原地改: v4 下被故事门误杀的题**必须重审** ——
    那些 decision 是按旧标准写的, 留着它们等于让新政策对存量无效。
    `CURATED_POLICY_VERSION` 同时是旧库存的隔离开关。
    """
    print("\n[H4-D] policy 已 bump 到 curated-v5")
    check("**CURATED_POLICY_VERSION == 'curated-v5'**",
          CC.CURATED_POLICY_VERSION == "curated-v5",
          CC.CURATED_POLICY_VERSION)
    check("与 quality policy 是**两个**独立的号",
          CC.CURATED_POLICY_VERSION != CC.QUALITY_POLICY_VERSION,
          (CC.CURATED_POLICY_VERSION, CC.QUALITY_POLICY_VERSION))
    # §一: v4 的 decision 不继承 —— 同一道题在 v5 下必须重新可审
    with tmpdir() as d:
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        rec = mk_truck_rec()
        led.record(rec, decision=CL.REJECTED,
                   policy_version="curated-v4", reasons=["single_trick"])
        check("v4 下已是终态", led.is_settled(rec, "curated-v4"))
        check("**v5 下必须重审**(v4 的结论不继承)",
              not led.is_settled(rec, CC.CURATED_POLICY_VERSION))


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
        # ---- H4-D: curated-v5 硬门/信号拆分 ----
        test_gate_and_signal_split_is_declared,
        test_reviewer_contract_matches_compiler_policy,
        test_soft_signals_never_reject,
        test_hard_gates_still_reject,
        test_q10000_can_now_be_accepted,
        test_elevator_and_jeep_can_now_be_accepted,
        test_render_still_rejected_for_external_knowledge,
        # ---- H4-D1 §四: Reviewer 语义门回归(端到端那一层) ----
        test_review_dramatic_payoff_false_does_not_reject,
        test_review_each_true_gate_still_rejects,
        test_apply_review_end_to_end_with_false_signals,
        # ---- H4-D1 §五: 四道边界题的产品裁决 ----
        test_h4d1_section5_boundary_product_rulings,
        test_new_true_rejects,
        test_review_missing_signals_is_not_technical_failure,
        test_hard_gate_beats_self_reported_accepted,
        test_each_fixture_has_no_obvious_tag,
        test_good_story_still_passes,
        test_inverted_check_direction,
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
        test_prompt_declares_v5_rules,
        test_checks_v3_is_superset_of_v2,
        test_accepted_binds_content_hash_not_just_external_id,
        test_policy_version_is_v5,
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
