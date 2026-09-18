#!/usr/bin/env python
# coding: utf-8
"""运行: uv run tests/test_canonical.py（完全离线, 无网络）。

Step 05: **只证明当前错误确实存在**, 不修。

数据源是 Step 00 冻结的真实 fixture:
    tests/fixtures/live_quality_regressions.jsonl

那是一道真实的 `canonical_fact_conflict`:
    谜面  教堂那口停了的老钟 / 母亲说"你爸就是被这一刻叫走的"
    事实   f1: 那口钟的指针被管风琴师**人为拨快**过二十分钟
    观众   "钟出故障走快了之后再停止吗"
    真实输出  verdict=是   (touched f1/f2)
    应有输出  verdict=不是 (因为 f1 说"人为拨快", 与"故障"互斥)

## 关于"读 fixture"的方式

用 `json.loads(line)`(Step 00B 的裁定), **不做字节/哈希比较** —— fixture
里记的 sha256 验证的是当年冻结的源文件, 不是这个文件自己的跨平台字节。

## 这个套件跑的是什么

真的调 `PuzzleWriter.answer()` / `PuzzleWriter.judge()`, 但用一个**回放
真实输出**的假 client: 我们不是在测模型, 而是在测**代码链路**会不会把
明知与 canonical facts 冲突的裁决原样放行。真实日志里它就是放行了。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from story.llm import LLMResult, PuzzleWriter  # noqa: E402
from story.puzzle import PuzzleFact, SolveAtom  # noqa: E402

FAIL = [0]
FIXTURE = Path(__file__).resolve().parent / "fixtures" / \
    "live_quality_regressions.jsonl"


def check(name, cond, extra=""):
    if cond:
        print(f"  ok  {name}")
        return
    print(f"  FAIL {name}  {extra}")
    FAIL[0] += 1


def load_fixture() -> dict:
    """读第一条 fixture(JSONL, 一行)。"""
    with open(FIXTURE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                return json.loads(line)
    raise AssertionError("fixture 是空的")


class _ReplayClient:
    """把 fixture 里记录的真实模型输出原样回放。

    这不是 mock 掉被测逻辑 —— `answer()` 的 prompt 拼装、裁决解析、
    `touched_fact_ids` 过滤、Final Judge 闸门, 全都是**真的在跑**。
    回放的只是"模型当时说了什么"这一个外部事实。
    """

    class cfg:
        model = "replay"

    def __init__(self, tool_input=None, error=None):
        self.tool_input = tool_input
        self.error = error
        self.calls = []

    def messages(self, system, user, max_tokens=None, tool=None,
                 temperature=None, timeout=None, max_retries=None):
        self.calls.append({"system": system, "user": user, "tool": tool,
                           "temperature": temperature})
        return LLMResult(tool_input=self.tool_input, error=self.error)


class _RuntimeCfg:
    """最小 runtime config —— 只要温度字段存在即可。"""
    answer_temperature = 0.0
    judge_temperature = 0.0
    review_temperature = 0.2
    riddle_temperature = 0.8


def _spec_from_fixture(fx: dict):
    facts = [PuzzleFact.from_dict(f) for f in (fx.get("facts") or [])]
    atoms = [SolveAtom.from_dict(a, i)
             for i, a in enumerate(fx.get("solve_atoms") or [])]
    return facts, atoms


# ======================================================================
def test_fixture_is_the_real_one():
    print("\n[S05-1] fixture 就是 Step 00 冻结的那条")
    fx = load_fixture()
    check("id 对得上", fx.get("id") == "live-20260918-d3da-idx7-qid665",
          fx.get("id"))
    check("category = canonical_fact_conflict",
          fx.get("category") == "canonical_fact_conflict", fx.get("category"))
    check("stage = answer", fx.get("stage") == "answer", fx.get("stage"))
    check("qid = 665", fx.get("source", {}).get("qid") == 665,
          fx.get("source", {}).get("qid"))
    check("canonical verdict 是『不是』",
          fx.get("expected_invariants", {}).get("canonical_verdict") == "不是",
          fx.get("expected_invariants"))
    # f1 必须真的是"人为拨快" —— 那是这个冲突的根
    f1 = [f for f in fx.get("facts", []) if f.get("id") == "f1"]
    check("f1 记录的是『人为拨快』", f1 and "人为拨快" in f1[0].get("text", ""),
          f1)


def test_answer_replay_returns_wrong_verdict():
    """**核心**: 回放真实输出 -> 链路原样给出『是』, 与 canonical 冲突。

    这一步只**证明错误存在**。它现在就该是"红的语义"(断言的是当前错误
    行为), 所以这里的断言写成"当前确实错了" —— Step 07 之后再回来改成
    "不再错"。
    """
    print("\n[S05-2] answer() 回放 -> 当前给出的裁决与 canonical 冲突")
    fx = load_fixture()
    facts, atoms = _spec_from_fixture(fx)
    real = fx["raw_model_output"]

    cli = _ReplayClient(tool_input={
        "answers": [{
            "id": fx["source"]["qid"],
            "verdict": real["verdict"],
            "solution_candidate": real["solution_candidate"],
            "touched_fact_ids": real["touched_fact_ids"],
            "comment": real["comment"],
        }]
    })
    w = PuzzleWriter(client=cli, runtime_cfg=_RuntimeCfg())
    results, err = w.answer(
        puzzle=fx["puzzle"], answer=fx["answer"], transcript=[],
        qid=fx["source"]["qid"], user_name="观众",
        text=fx["viewer_text"], judge_solve=True,
        solve_atoms=atoms, facts=facts)

    check("answer() 没有报错", err is None, err)
    check("拿到一条裁决", len(results) == 1, results)
    got = results[0].verdict if results else None
    canonical = fx["expected_invariants"]["canonical_verdict"]
    check(f"当前裁决是 {real['verdict']!r}(回放的真实输出)", got == real["verdict"],
          got)
    check(f"**与 canonical {canonical!r} 冲突 —— 错误确实存在**",
          got != canonical, f"got={got!r} canonical={canonical!r}")
    # 冲突的机理: 观众说的是"故障", 而 f1 说"人为拨快", 两者互斥。
    # 记录 touched f1 说明模型**看到了**那条事实却仍然判『是』。
    check("模型当时确实碰了 f1(看得到事实仍判错)",
          "f1" in (real.get("touched_fact_ids") or []), real)


def test_answer_prompt_does_include_canonical_facts():
    """所以这不是"事实没送到模型面前"的问题 —— Answer 阶段事实是全的。

    fixture 的日志行明细里有 `answer_prompt`, 而 DETAIL 日志里能确认
    Answer 的 user 消息带了 `【事实表(判定依据)】`。这里从**请求体**直接
    验证: 事实表真的在 prompt 里。
    """
    print("\n[S05-3] Answer prompt 里确实有事实表")
    fx = load_fixture()
    facts, atoms = _spec_from_fixture(fx)
    cli = _ReplayClient(tool_input={"answers": [{
        "id": 1, "verdict": "不是", "solution_candidate": False,
        "touched_fact_ids": [], "comment": ""}]})
    w = PuzzleWriter(client=cli, runtime_cfg=_RuntimeCfg())
    w.answer(puzzle=fx["puzzle"], answer=fx["answer"], transcript=[],
             qid=1, user_name="观众", text=fx["viewer_text"],
             judge_solve=True, solve_atoms=atoms, facts=facts)
    user = cli.calls[0]["user"]
    check("prompt 带【事实表】", "事实表" in user, user[:200])
    check("f1 的『人为拨快』在 prompt 里", "人为拨快" in user, user[:400])
    check("观众原话在 prompt 里", fx["viewer_text"] in user, user[-300:])


def test_judge_prompt_omits_canonical_facts():
    """Step 07 的靶子: **Final Judge 的 prompt 里没有事实表**。

    `judge()` 收了 `facts` 参数却从不使用 —— 它只看谜面/谜底/atoms。
    于是"canonical facts 是唯一判定依据"这条规则在 Judge 阶段不成立。
    这条现在断言的是**当前缺陷**(所以叫 omits)。
    """
    print("\n[S05-4] Final Judge prompt **不含**事实表(当前缺陷)")
    fx = load_fixture()
    facts, atoms = _spec_from_fixture(fx)
    cli = _ReplayClient(tool_input={
        "is_guess": True, "cause_hit": False, "mechanism_hit": False,
        "key_fact_hit": False, "matched_atoms": []})
    w = PuzzleWriter(client=cli, runtime_cfg=_RuntimeCfg())
    w.judge(puzzle=fx["puzzle"], answer=fx["answer"],
            text="父亲被钟声叫走了", solve_atoms=atoms, facts=facts)
    user = cli.calls[0]["user"]
    check("judge prompt 有谜面", "谜面" in user, user[:120])
    check("judge prompt 有谜底", "谜底" in user, user[:120])
    # 当前状态: 事实表**不在**里面 —— Step 07 要把它加进去。
    #
    # ⚠️ 不能用"文本里有没有『人为拨快』"来判断: 那道事实的措辞**也会**
    # 出现在【谜底】里(谜底本来就在讲同一件事)。用渲染后的**结构标记**
    # `- f1 [core]` 才精确 —— 那是 `_facts_block` 独有的格式。
    check("**judge prompt 目前没有事实表块**(Step 07 要修的点)",
          "【事实表" not in user and "- f1 [" not in user, user[:400])
    check("judge 也拿不到 f1 的 id(无 facts 块)", "- f1 [" not in user,
          user[:400])


def test_judge_facts_param_is_accepted_but_unused():
    """`judge()` 签名收了 `facts`, 但**逐字**确认它没进 prompt。

    这条比上一条更直接: 同一组参数传 facts / 不传 facts, 请求体**完全
    相同** —— 说明该参数当前是死的。
    """
    print("\n[S05-5] facts 参数当前是死的(传与不传请求体相同)")
    fx = load_fixture()
    facts, atoms = _spec_from_fixture(fx)
    ti = {"is_guess": True, "cause_hit": True, "mechanism_hit": True,
          "key_fact_hit": True, "matched_atoms": [0, 1]}

    c1 = _ReplayClient(tool_input=ti)
    w1 = PuzzleWriter(client=c1, runtime_cfg=_RuntimeCfg())
    w1.judge(puzzle=fx["puzzle"], answer=fx["answer"], text="x",
             solve_atoms=atoms, facts=facts)

    c2 = _ReplayClient(tool_input=ti)
    w2 = PuzzleWriter(client=c2, runtime_cfg=_RuntimeCfg())
    w2.judge(puzzle=fx["puzzle"], answer=fx["answer"], text="x",
             solve_atoms=atoms, facts=None)

    check("传 facts 与不传 facts, judge 请求体完全相同(参数当前无效)",
          c1.calls[0]["user"] == c2.calls[0]["user"],
          "两者不同 -> 说明 facts 其实被用了")


def main():
    tests = [
        test_fixture_is_the_real_one,
        test_answer_replay_returns_wrong_verdict,
        test_answer_prompt_does_include_canonical_facts,
        test_judge_prompt_omits_canonical_facts,
        test_judge_facts_param_is_accepted_but_unused,
    ]
    for t in tests:
        t()
    print()
    if FAIL[0]:
        print(f"FAILED: {FAIL[0]} 项")
        return 1
    print("PASS: Step 05 canonical regression —— 当前错误已复现(尚未修)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
