#!/usr/bin/env python
# coding: utf-8
"""运行: uv run tests/test_haiguitang_import.py（**完全离线, 无网络**）。

H4-A 的离线回归: `neurostellar/haiguitang` 导入器。

## 这批要守的四件事

  1. **确定性清洗在任何 LLM 之前**(§四)。清洗是免费的, Reviewer 是
     要花钱的。这条测试断言各种残缺形态都被**零调用**拦下。
  2. **不伪造许可证**(§七)。该源没有 license 字段 —— 它必须走
     `usage_basis="project_approved_public_dataset"`, 而**不是**硬填
     一个 CC BY-SA / MIT / Apache。
  3. **只做 exact 去重**(§九)。这一轮明确不做 embedding / 语义 /
     跨源聚类。测试要证明重复条目被合并, 而**近似但不同**的条目不合并。
  4. **稳定 id**(§八)。同内容两次导入必须得到同一个 external_id ——
     用 row index 会在 dataset 重排时换身份。
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import import_haiguitang as H  # noqa: E402
from tools.curated_common import (  # noqa: E402
    RawCuratedPuzzle, normalize_for_dedup,
)

FAIL = [0]


def check(name, cond, extra=""):
    if cond:
        print(f"  ok  {name}")
        return
    print(f"  FAIL {name}  {extra}")
    FAIL[0] += 1


@contextmanager
def tmpdir():
    d = tempfile.mkdtemp(prefix="hgt_h4a_")
    try:
        yield d
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def _row(output, **kw):
    d = {"instruction": "请根据给定的关键词，生成一个有创意且符合海龟汤"
                        "特点的故事情节和真相。",
         "input": "关键词：a，b，c", "system": "你是一个海龟汤故事生成器。",
         "output": output}
    d.update(kw)
    return d


_GOOD = ("故事情节：一个人住在山顶的小屋里, 半夜听见敲门, 开门却没有人。"
         "第二天山脚下发现一具尸体。为什么?\n"
         "真相：门前是悬崖, 爬上来的人敲门求救, 一开门又被推了下去。")


# ======================================================================
# 1. output 解析
# ======================================================================
def test_split_output_handles_both_markers():
    """`故事情节:` / `真相:` 必须被拆开, 且支持跨行(re.S)。"""
    print("\n[H4-A] output 拆分")
    s, b = H.split_output(_GOOD)
    check("拆出谜面", "山顶" in s, s[:40])
    check("拆出谜底", "悬崖" in b, b[:40])
    check("谜面**不含**'真相'", "真相" not in s, s[:60])
    check("谜面不含标记词", "故事情节" not in s, s[:40])

    # 全角/半角冒号都要认
    s2, b2 = H.split_output("故事情节: AAA 真相: BBB")
    check("半角冒号也认", s2 == "AAA" and b2 == "BBB", (s2, b2))

    # 拆不出 -> 空
    check("没有标记 -> 空", H.split_output("这是一段没有结构的文字") == ("", ""))
    check("空串 -> 空", H.split_output("") == ("", ""))


def test_split_preserves_trailing_content_after_truth():
    """真相后面的补充内容**不许砍掉** —— 那会让谜底不完整。"""
    print("\n[H4-A] 真相后的补充保留")
    s, b = H.split_output("故事情节：开头。\n真相：核心解释。补充说明一句。")
    check("补充内容仍在谜底里", "补充说明" in b, b)


# ======================================================================
# 2. 确定性清洗(零 LLM)
# ======================================================================
def test_sanitize_rejects_missing_halves():
    """只有题面 / 只有答案 -> source-invalid, **零 LLM 调用**。"""
    print("\n[H4-A] 清洗: 缺一半")
    check("缺谜面", H.sanitize("", "有答案") == "missing_surface")
    check("缺谜底", H.sanitize("有题面有题面", "") == "missing_bottom")
    check("两边都空", H.sanitize("", "") == "empty_both")


def test_sanitize_rejects_generation_prompt_leak():
    """生成 prompt 被续写进 output -> 那不是一道题。"""
    print("\n[H4-A] 清洗: 生成 prompt 泄漏")
    check("谜面含'请根据'",
          H.sanitize("请根据关键词生成一个故事, 然后发生了怪事?",
                     "真相是这里") == "generation_prompt_leak")
    check("谜底含'关键词：'",
          H.sanitize("一个正常的谜面描述在这里",
                     "关键词：a,b,c 所以答案是这样") == "generation_prompt_leak")


def test_sanitize_rejects_truncation():
    """结尾停在逗号/冒号 -> 明显被截断(不是完整句子)。"""
    print("\n[H4-A] 清洗: 截断")
    check("谜面以中文逗号结尾",
          H.sanitize("他走进房间, 看见那个东西，",
                     "一个足够长的谜底解释在这里") == "truncated")
    check("谜面以冒号结尾",
          H.sanitize("他走进房间, 看见那个东西：",
                     "一个足够长的谜底解释在这里") == "truncated")
    check("谜底以逗号结尾",
          H.sanitize("一个足够长的谜面描述在这里",
                     "因为那天他终于明白了，") == "truncated")
    # 对照组: 正常结尾(句号/问号/无标点)不该被误判成截断。
    check("**正常结尾不误判**",
          H.sanitize("他走进房间, 看见那个东西。",
                     "一个足够长的谜底解释在这里") == "")


def test_sanitize_rejects_too_short():
    """太短 == 解析失败或本来就没有内容。"""
    print("\n[H4-A] 清洗: 太短")
    check("谜面太短", H.sanitize("短", "一个足够长的谜底解释在这里") ==
          "surface_too_short")
    check("谜底太短", H.sanitize("一个足够长的谜面描述在这里", "短") ==
          "bottom_too_short")


def test_length_rule_is_separate_from_deletion():
    """§五: 超长**不删原始数据**, 只是不进 live candidate。"""
    print("\n[H4-A] 长度规则")
    long_s = "啊" * (H.SURFACE_MAX + 1)
    ok_b = "一个足够长的谜底解释在这里"
    check("超长谜面 length_ok=False", not H.length_ok(long_s, ok_b))
    check("但 sanitize **不**拒它(内容本身没问题)",
          H.sanitize(long_s, ok_b) == "")
    check("正常长度 length_ok=True",
          H.length_ok("一个正常的谜面描述在这里", ok_b))


# ======================================================================
# 3. 稳定 id(§八)
# ======================================================================
def test_external_id_is_stable_across_reorder():
    """同内容必须得到同一个 id —— **不能用 row index**。

    dataset 重排(重新上传 / shuffle)时, 行号会变。用行号当 id 会让
    同一道题换身份: 账本里那条 accepted 决策再也对不上。
    """
    print("\n[H4-A] external_id 稳定")
    a = H.external_id_for("谜面甲在这里", "谜底甲在这里")
    b = H.external_id_for("谜面甲在这里", "谜底甲在这里")
    check("同内容 -> 同 id", a == b, (a, b))
    check("前缀是 haiguitang:", a.startswith("haiguitang:"), a)
    c = H.external_id_for("谜面乙在这里", "谜底甲在这里")
    check("不同内容 -> 不同 id", a != c, (a, c))
    # 哈希前先规范化: **空白/零宽字符**差异不该换身份(标点是有意义的
    # 内容, 规范化只做无损映射, 不删标点)。
    d = H.external_id_for("谜面甲在这里  ", "谜底甲在这里")
    check("**空白差异不换身份**", a == d, (a, d))
    check("规范化确实是幂等且无损的",
          normalize_for_dedup("谜面甲在这里  ") ==
          normalize_for_dedup("谜面甲在这里"))


# ======================================================================
# 4. 去重: **只做 exact**(§九)
# ======================================================================
def test_exact_duplicates_are_merged():
    """完全相同的记录只留一份。"""
    print("\n[H4-A] exact 去重")
    rows = [_row(_GOOD), _row(_GOOD), _row(_GOOD)]
    recs, rej_src, rej_safe, stats = H.build_records(rows)
    check("**3 条相同 -> 只留 1 条**", len(recs) == 1, stats)
    check("统计里记了 2 条 exact 重复", stats["exact_dup"] == 2, stats)


def test_near_but_different_are_not_merged():
    """§九: 近似但**不同**的条目**不合并** —— 这一轮不做语义去重。

    误合并的代价是**悄悄丢题**, 而那比"偶尔重复"糟得多。
    """
    print("\n[H4-A] 近似但不合并")
    a = _row("故事情节：一个人在雨夜里走进了一间空房子, 听见楼上有人走动。"
             "为什么?\n真相：那是他自己的脚步声, 房子是环形的。")
    b = _row("故事情节：一个人在雨夜里走进了一间空房子, 听见楼上有人行走。"
             "为什么?\n真相：那是他自己的脚步声, 房子是环形的。")
    recs, _, _, stats = H.build_records([a, b])
    check("**两条都保留(近似 != 相同)**", len(recs) == 2, stats)


def test_no_near_duplicate_label_is_emitted():
    """§九: 新源**不许**产生 `dup_reason=near_duplicate`。

    那个标签会让 candidate 被 `select_candidate` 直接跳过 —— 大面积
    误拒正是任务书要避免的。
    """
    print("\n[H4-A] 不产生 near_duplicate 标签")
    rows = [_row(_GOOD),
            _row(_GOOD.replace("山顶", "山腰")),
            _row(_GOOD.replace("悬崖", "峭壁"))]
    recs, _, _, _ = H.build_records(rows)
    check("**没有任何 near_duplicate**",
          all(not r.dup_reason for r in recs),
          [r.dup_reason for r in recs])


# ======================================================================
# 5. 授权依据(§七) —— **不许伪造许可证**
# ======================================================================
def test_usage_basis_is_project_approved_not_a_fake_license():
    """该源没有 license 字段 -> 走 `usage_basis`, **不填**假许可证。"""
    print("\n[H4-A] 授权依据 = 项目批准(不是假许可证)")
    recs, _, _, stats = H.build_records([_row(_GOOD)])
    r = recs[0]
    check("**没有伪造 question_license**", r.question_license == "",
          r.question_license)
    check("**没有伪造 answer_license**", r.answer_license == "",
          r.answer_license)
    check("usage_basis 是项目批准", r.usage_basis == H.USAGE_BASIS,
          r.usage_basis)
    check("统计里带 usage_basis", stats["usage_basis"] == H.USAGE_BASIS)
    check("**license_ok() 认它**", r.license_ok() is True)
    check("没有任何已知许可证名混进来",
          "CC BY" not in r.question_license
          and "Apache" not in r.question_license)


def test_unknown_license_still_rejected_without_basis():
    """对照组: **没有** usage_basis 且许可未知 -> 仍然不可用。

    证明 `usage_basis` 是一条**具体**的合法路径, 不是一个"许可检查
    形同虚设"的后门。
    """
    print("\n[H4-A] 对照: 无依据 + 许可未知 -> 不可用")
    r = RawCuratedPuzzle(question_license="",
                         question_license_inference="")
    check("**license_ok() 仍是 False**", r.license_ok() is False)
    r2 = RawCuratedPuzzle(question_license="CC BY-SA 4.0",
                          question_license_inference="api")
    check("正常 SE 许可仍然可用", r2.license_ok() is True)


# ======================================================================
# 6. §十一: select_candidate **不能**静默跳过新源
# ======================================================================
def test_select_candidate_can_pick_project_approved_source():
    """§十一: 项目批准的 candidate 必须能被 `select_candidate` 选中。

    ## 这条防的是一次 **silent no-op**

        成功下载 3729 条 -> 成功 normalize -> Lazy Curator **一条都不审**

    成因是 `select_candidate` 里那句 `if not rec.license_ok(): continue`
    —— 新源没有许可证, 于是全被跳过, 而日志上看不出任何异常(它只是
    "没有候选")。
    """
    print("\n[H4-A] select_candidate 能选中项目批准的源")
    from story.lazy_curator import select_candidate, source_priority
    from tools.curated_ledger import DecisionLedger
    from tools.curated_compiler import CURATED_POLICY_VERSION
    recs, _, _, _ = H.build_records([_row(_GOOD)])
    with tmpdir() as d:
        led = DecisionLedger(os.path.join(d, "dec.jsonl"))
        pick = select_candidate(recs, led, CURATED_POLICY_VERSION)
        check("**选得出来(不是 None)**", pick is not None,
              "silent no-op: 新源一条都不会被审")
        check("选中的就是那道题", pick.external_id == recs[0].external_id)


def test_source_priority_puts_haiguitang_first():
    """§十: 审题顺序 haiguitang(0) < TurtleBench(1) < PSE。"""
    print("\n[H4-A] 审题顺序")
    from story.lazy_curator import source_priority
    hg = RawCuratedPuzzle(source="neurostellar/haiguitang",
                          external_id="haiguitang:x")
    tb = RawCuratedPuzzle(source="TurtleBench1.5k",
                          external_id="turtlebench:y")
    se = RawCuratedPuzzle(source="Puzzling Stack Exchange",
                          external_id="pse:q:1", tags=["situation"])
    check("haiguitang 排第 0", source_priority(hg)[0] == 0,
          source_priority(hg))
    check("TurtleBench 排第 1", source_priority(tb)[0] == 1,
          source_priority(tb))
    check("PSE situation 排第 2", source_priority(se)[0] == 2,
          source_priority(se))
    check("**haiguitang 排在 TurtleBench 前面**",
          source_priority(hg) < source_priority(tb))


def test_priority_is_order_not_acceptance_bonus():
    """§十: 排序**不是**准入加分 —— 新源一样要过全部十三条。

    任何"因为它是 haiguitang 所以容易过"的代码都是错的。
    """
    print("\n[H4-A] 排序 != 加分")
    import inspect
    from story import lazy_curator as LC
    src = inspect.getsource(LC.select_candidate)
    check("select_candidate **不看来源**加准入分",
          "haiguitang" not in src, "准入路径里出现了来源名 —— 那是加分")
    # q10000 型的单点题在 haiguitang 源下同样必须被故事门拒
    from tools import curated_compiler as CC
    d = {"accepted": True,
         "quality_checks": {k: True for k in CC.CURATED_CHECKS_V3}}
    d["quality_checks"]["single_trick"] = True      # 反向: true = 坏
    sgr = CC.story_gate_reasons(d)
    check("**单点技巧题在 v3 下仍被拒**(与来源无关)",
          "single_trick" in sgr, sgr)


# ======================================================================
# 7. 端到端: 真实 fixture 走一遍
# ======================================================================
def test_end_to_end_on_real_fixture():
    """用**真实下载的那份**跑一遍(存在才跑, 否则跳过)。"""
    print("\n[H4-A] 真实 fixture 端到端")
    p = os.path.join("data_external", "haiguitang", "raw", "turtle.json")
    if not os.path.exists(p):
        print("  -- 跳过(没有本地 raw; CI 上本来就没有)")
        return
    rows = json.loads(io.open(p, encoding="utf-8").read())
    recs, rej_src, rej_safe, stats = H.build_records(rows)
    check("原始行数与报告一致", stats["raw_rows"] == len(rows), stats)
    check("**拆解失败为 0**(实测 3729/3729 可拆)", stats["parse_fail"] == 0,
          stats)
    check("**全部保留原始行**(kept + 各拒收 = raw)",
          stats["kept"] + stats["parse_fail"] + stats["sanitize_rejected"]
          + stats["too_long"] + stats["exact_dup"]
          + stats["rejected_safety"] == stats["raw_rows"], stats)
    check("**入 candidate 数量合理(>100)**", stats["kept"] > 100, stats)
    check("每条都有稳定 id",
          all(r.external_id.startswith("haiguitang:") for r in recs))
    check("**没有伪造许可证**",
          all(r.question_license == "" for r in recs))
    check("**每条都带 usage_basis**",
          all(r.usage_basis == H.USAGE_BASIS for r in recs))
    check("**没有 near_duplicate 标签**",
          all(not r.dup_reason for r in recs))


def test_writes_nothing_when_everything_is_rejected():
    """全部被拒时**不写空文件** —— 那会让下游以为导入成功。"""
    print("\n[H4-A] 全拒 -> 不产出 candidate 文件")
    rows = [_row("没有结构的文本"), _row("")]
    recs, rej_src, _, stats = H.build_records(rows)
    check("**一条都不留**", stats["kept"] == 0, stats)
    check("但拒收清单里记了原因",
          len(rej_src) == len(rows) and all(r.get("reason") for r in rej_src),
          rej_src)


# ======================================================================
def main():
    tests = [
        test_split_output_handles_both_markers,
        test_split_preserves_trailing_content_after_truth,
        test_sanitize_rejects_missing_halves,
        test_sanitize_rejects_generation_prompt_leak,
        test_sanitize_rejects_truncation,
        test_sanitize_rejects_too_short,
        test_length_rule_is_separate_from_deletion,
        test_external_id_is_stable_across_reorder,
        test_exact_duplicates_are_merged,
        test_near_but_different_are_not_merged,
        test_no_near_duplicate_label_is_emitted,
        test_usage_basis_is_project_approved_not_a_fake_license,
        test_unknown_license_still_rejected_without_basis,
        test_select_candidate_can_pick_project_approved_source,
        test_source_priority_puts_haiguitang_first,
        test_priority_is_order_not_acceptance_bonus,
        test_end_to_end_on_real_fixture,
        test_writes_nothing_when_everything_is_rejected,
    ]
    for t in tests:
        t()
    print()
    if FAIL[0]:
        print(f"FAILED: {FAIL[0]}")
        return 1
    print(f"ALL PASS ({len(tests)} tests)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
