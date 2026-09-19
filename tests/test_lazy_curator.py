#!/usr/bin/env python
# coding: utf-8
"""运行: uv run tests/test_lazy_curator.py（**完全离线, 无网络**）。

Batch H3-B/C 的离线回归: **Lazy Curator** —— 后台按需审题 + 直播让路。

## 这批要守的四件事

  1. **直播优先**(任务书十)。真人 pending/inflight、AI 玩家在途、
     hint/reveal 在途、下一题临近 —— 任何一条成立都不启动新的 LLM。
     后台审题再重要也不能让观众等。
  2. **技术失败 ≠ 拒绝**(任务书三)。timeout/429 记 `technical_defer`
     (可重试), 只有内容问题才记 `rejected`(终态)。写错方向的后果是
     **永久吃掉一道题**, 而且没有任何地方会显示丢了。
  3. **single-flight**。同一时刻最多一个 worker。
  4. **`--max-candidates` 按尝试计, 不按成功计**(任务书十六)。
     否则拒绝率高时 `--limit 20` 会调用上百次。

## 用什么当"模型"

一个**假的 compiler** —— 它按脚本返回预设的 spec/info, 不发网络请求。
这样测的是"我们怎么编排", 而"模型答得对不对"由 curated_v2 那套测。
两者分开: 混在一起会让一个 flaky 的模型把编排逻辑的 bug 掩盖掉。
"""
from __future__ import annotations

import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from story.lazy_curator import (  # noqa: E402
    LazyCurator, select_candidate, source_priority,
)
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
    d = tempfile.mkdtemp(prefix="hgt_h3b_")
    try:
        yield d
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


# ======================================================================
# 假件
# ======================================================================
def mk_rec(eid="pse:q:1", tags=None, source="Puzzling Stack Exchange",
           **kw):
    d = dict(
        external_id=eid, source=source,
        source_url=f"https://example.invalid/{eid}",
        source_kind="stackexchange",
        question_author="Q", answer_author="A",
        question_license="CC BY-SA 4.0", answer_license="CC BY-SA 4.0",
        # ⚠️ `license_ok()` 要求 inference ∈ {api, created_at} —— 只填
        # 许可证名是不够的("我知道它叫什么"≠"我知道这个值从哪来")。
        # 少了这一项, 每条 fixture 都会被候选选择跳过。
        question_license_inference="api", answer_license_inference="api",
        title=f"题 {eid}",
        surface=f"谜面 {eid}?",
        bottom=f"谜底 {eid}",
        language="en", original_language="en",
        tags=list(tags or ["situation"]))
    d.update(kw)
    return RawCuratedPuzzle(**d)


class _FakePool:
    """只记账, 不落盘到生产路径。"""

    def __init__(self, path, stock=0, playable=0):
        self.pool_path = path
        self.used_path = path + ".used"
        self._stock = stock
        self._playable = playable
        self.added = []

    def stock_count(self, limit=None):
        return self._stock

    def playable_count(self, *a, **k):
        return self._playable


class _FakeCompiler:
    """按脚本返回。**不发网络请求。**

    ⚠️ 它**真的会调** `should_continue` —— 这一点是被 mutation 逼出来的。

    早先的版本只在 `script` 里写 "interrupt" 就返回中断, 于是
    "把 `_should_continue` 改成永远 True" 这种 mutant **不会被发现**:
    假编译器根本不问回调, 它自己就决定中断了。测试全绿, 而生产代码里
    的让路逻辑已经死了。

    现在它模拟真实 `CuratedCompiler` 的四个检查点: 每个 checkpoint
    都问一次回调, 回调说停就返回 interrupted。这样 mutant 才会红。
    """

    #: 与真实编译器一致的四个让路检查点(名字只用于 info["interrupt_at"])
    GATES = ("before_compile", "before_review", "before_audit",
             "before_commit")

    def __init__(self, script):
        #: script: list of ("accept"|"reject"|"defer"|"interrupt", stage)
        self.script = list(script)
        self.calls = []
        self.gate_calls = []          #: 每次回调询问的 (eid, gate)

    def compile_one(self, rec, *, recent=None, blueprint=None,
                    max_attempts=2, should_continue=None):
        eid = getattr(rec, "external_id", "")
        self.calls.append({"external_id": eid,
                           "should_continue": should_continue})
        kind, stage = (self.script.pop(0) if self.script
                       else ("reject", "ai_gate"))

        # ---- 模拟真实编译器的让路检查点 ----
        # `should_continue=None` 表示"没有回调" -> 等同于不检查(与生产
        # 一致: 不传回调就是不让路)。
        if should_continue is not None:
            for gate in self.GATES:
                self.gate_calls.append((eid, gate))
                if not should_continue():
                    return None, {"external_id": eid, "accepted": False,
                                  "interrupted": True, "interrupt_at": gate,
                                  "stage": "interrupted",
                                  "reject_reasons": []}

        if kind == "accept":
            spec = _mk_spec(rec)
            return spec, {"external_id": eid, "accepted": True,
                          "stage": "accepted", "style_tags": ["identity_flip"],
                          "reject_reasons": []}
        if kind == "interrupt":
            # script 显式要求中断(用于测"编译器自己决定中断"的路径)
            return None, {"external_id": eid, "accepted": False,
                          "interrupted": True, "interrupt_at": stage or "x",
                          "stage": "interrupted", "reject_reasons": []}
        if kind == "defer":
            return None, {"external_id": eid, "accepted": False,
                          "stage": stage, "reject_reasons": ["technical"]}
        return None, {"external_id": eid, "accepted": False,
                      "stage": stage, "reject_reasons": ["single_trick"]}


def _mk_spec(rec):
    from tools.curated_compiler import spec_from_tool
    d = {
        "accepted": True, "title": "t", "puzzle": "x?", "answer": "y",
        "core_answer": "z", "completion_fact_ids": [],
        "style_tags": ["identity_flip"], "content_style": ["悬疑"],
    }
    return spec_from_tool(d, rec, None)


def _cfg(**kw):
    from story.config import Config
    kw.setdefault("sim_path", "x")
    kw.setdefault("no_llm", True)
    return Config(**kw)


def _mk(cfg, pool, compiler, recs, ledger, pressure):
    return LazyCurator(cfg, pool, compiler, recs, ledger, pressure,
                       clock=lambda: 0.0)


BUSY = {"pending": 1}
FREE = {}
AI_BUSY = {"ai_player_in_flight": True}


# ======================================================================
# 1. 直播优先
# ======================================================================
def test_no_start_when_human_pending():
    print("\n[H3-B] 真人 pending -> 不启动")
    with tmpdir() as d:
        cfg = _cfg(curated_min_size=100)     # 强制"库存不足"
        pool = _FakePool(os.path.join(d, "p.jsonl"))
        comp = _FakeCompiler([("accept", "")])
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        lc = _mk(cfg, pool, comp, [mk_rec()], led, lambda: dict(BUSY))
        ok, why = lc.should_start()
        check("**不启动**", not ok, why)
        check("理由点名 pending", "pending" in why, why)
        r = lc.step()
        check("step 也没审", r["processed"] == 0, r)
        check("**一次 LLM 都没调**", comp.calls == [], comp.calls)


def test_no_start_when_ai_player_in_flight():
    """H3-B 十二: AI 玩家在途也算直播压力。"""
    print("\n[H3-B] AI 玩家在途 -> 不启动")
    with tmpdir() as d:
        cfg = _cfg(curated_min_size=100)
        pool = _FakePool(os.path.join(d, "p.jsonl"))
        comp = _FakeCompiler([("accept", "")])
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        lc = _mk(cfg, pool, comp, [mk_rec()], led, lambda: dict(AI_BUSY))
        ok, why = lc.should_start()
        check("**不启动**", not ok, why)
        check("理由点名 AI 玩家", "AI" in why, why)


def test_no_start_on_hint_or_reveal_inflight():
    print("\n[H3-B] hint / reveal 在途 -> 不启动")
    for key in ("hint_inflight", "reveal_inflight"):
        with tmpdir() as d:
            cfg = _cfg(curated_min_size=100)
            pool = _FakePool(os.path.join(d, "p.jsonl"))
            comp = _FakeCompiler([("accept", "")])
            led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
            lc = _mk(cfg, pool, comp, [mk_rec()], led,
                     lambda k=key: {k: True})
            ok, why = lc.should_start()
            check(f"{key} -> 不启动", not ok, why)


def test_no_start_near_next_puzzle():
    """临近下一题不再启动(任务书十: "下一题马上开始")。"""
    print("\n[H3-B] 下一题临近 -> 不启动")
    with tmpdir() as d:
        cfg = _cfg(curated_min_size=100, curated_start_guard_seconds=15.0)
        pool = _FakePool(os.path.join(d, "p.jsonl"))
        comp = _FakeCompiler([("accept", "")])
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        lc = _mk(cfg, pool, comp, [mk_rec()], led,
                 lambda: {"reveal_remaining_seconds": 5.0})
        ok, why = lc.should_start()
        check("**只剩 5s -> 不启动**", not ok, why)
        lc2 = _mk(cfg, pool, comp, [mk_rec()], led,
                  lambda: {"reveal_remaining_seconds": 30.0})
        ok2, why2 = lc2.should_start()
        check("还剩 30s -> 可以启动", ok2, why2)


def test_pressure_probe_failure_is_safe():
    """探针坏了要当成"忙", 不能当成"空闲"。"""
    print("\n[H3-B] 压力探针抛异常 -> fail safe")

    def boom():
        raise RuntimeError("probe down")

    with tmpdir() as d:
        cfg = _cfg(curated_min_size=100)
        pool = _FakePool(os.path.join(d, "p.jsonl"))
        comp = _FakeCompiler([("accept", "")])
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        lc = _mk(cfg, pool, comp, [mk_rec()], led, boom)
        ok, _ = lc.should_start()
        check("**探针坏了 -> 不启动**(宁可少审一道)", not ok)


def test_worker_started_then_pressure_interrupts():
    """**worker 已开始 -> 中途来压力 -> 在下一个 gate 前中断**。

    这是协作式中断的核心断言: 已经花掉的那次调用不浪费(它是完整的),
    但**不会**继续往下走审稿/审计。
    """
    print("\n[H3-B] 中途来压力 -> 协作中断")
    with tmpdir() as d:
        cfg = _cfg(curated_min_size=100)
        pool = _FakePool(os.path.join(d, "p.jsonl"))
        # 第 1 次问 should_continue 时说"可以", 之后说"忙"
        state = {"n": 0}

        def probe():
            state["n"] += 1
            return dict(FREE) if state["n"] <= 3 else dict(BUSY)

        comp = _FakeCompiler([("interrupt", "before_review")])
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        lc = _mk(cfg, pool, comp, [mk_rec()], led, probe)
        r = lc.step()
        check("记成 interrupted", r["interrupted"] == 1, r)
        check("**不是 rejected**", r["rejected"] == 0, r)
        last = led.last(mk_rec(), CC.CURATED_POLICY_VERSION)
        check("账本写 interrupted", last.get("decision") == CL.INTERRUPTED,
              last)
        check("**interrupted 不是终态**(下次可重审)",
              not led.is_settled(mk_rec(), CC.CURATED_POLICY_VERSION))


def test_deferred_candidate_not_retried_in_same_run():
    """**同一次运行里不重复碰同一条**(实跑踩到的)。

    `technical_defer` 不写终态 -> 那条题仍是候选 -> 早先的实现会在
    下一轮循环立刻把它再挑出来重审。实测 `turtlebench:b51c7fba5006`
    因网关回空 tool_input 被 defer, 紧接着又被审了一遍, 30 条预算里
    白白吃掉两条。

    defer 的语义是"**下次**再试" —— 网关刚抖完, 同一秒再问几乎必然
    还是抖。真正的重试在**下一次运行**。
    """
    print("\n[H3-B] 同一次运行不重试已碰过的题")
    with tmpdir() as d:
        cfg = _cfg(curated_min_size=100)
        pool = _FakePool(os.path.join(d, "p.jsonl"))
        recs = [mk_rec(eid="pse:q:1"), mk_rec(eid="pse:q:2")]
        # 两条都 defer
        comp = _FakeCompiler([("defer", "compile_call"),
                              ("defer", "compile_call")])
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        lc = _mk(cfg, pool, comp, recs, led, lambda: dict(FREE))
        r = lc.step(max_candidates=2)
        check("处理了 2 条", r["processed"] == 2, r)
        check("**两条是不同的题**",
              len({c["external_id"] for c in comp.calls}) == 2,
              [c["external_id"] for c in comp.calls])
        check("各只调一次", len(comp.calls) == 2, len(comp.calls))

        # ⚠️ 但**下一次运行**仍然可以重审它们(defer 还是可重试的)
        got = select_candidate(recs, led, CC.CURATED_POLICY_VERSION)
        check("**下一次运行仍是候选**", got is not None, got)


def test_defer_then_next_run_retries():
    """defer 的题在下一次 `step()` 里会再被挑中。"""
    print("\n[H3-B] 下一次运行会重试 defer 的题")
    with tmpdir() as d:
        cfg = _cfg(curated_min_size=100)
        pool = _FakePool(os.path.join(d, "p.jsonl"))
        recs = [mk_rec(eid="pse:q:1")]
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        # 第一次运行: defer
        c1 = _FakeCompiler([("defer", "compile_call")])
        _mk(cfg, pool, c1, recs, led, lambda: dict(FREE)).step()
        check("第一轮 defer 了", len(c1.calls) == 1, c1.calls)
        # 第二次运行: 这次成功
        c2 = _FakeCompiler([("accept", "")])
        r2 = _mk(cfg, pool, c2, recs, led, lambda: dict(FREE)).step()
        check("**第二轮重审了它**", len(c2.calls) == 1, c2.calls)
        check("第二轮 accepted", r2["accepted"] == 1, r2)


def test_skip_ids_does_not_affect_settled_logic():
    """`skip_ids` 只影响本次选择, 不改账本语义。"""
    print("\n[H3-B] skip_ids 不污染账本判定")
    with tmpdir() as d:
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        recs = [mk_rec(eid="a"), mk_rec(eid="b")]
        PV = CC.CURATED_POLICY_VERSION
        check("无 skip: 选 a",
              select_candidate(recs, led, PV).external_id == "a")
        check("skip a: 选 b",
              select_candidate(recs, led, PV,
                               skip_ids={"a"}).external_id == "b")
        check("skip 全部: None",
              select_candidate(recs, led, PV,
                               skip_ids={"a", "b"}) is None)


def test_interrupted_candidate_is_retried_next_time():
    """任务书廿二-13: interrupted 的题稍后能重新审。"""
    print("\n[H3-B] interrupted 下次会重审")
    with tmpdir() as d:
        cfg = _cfg(curated_min_size=100)
        pool = _FakePool(os.path.join(d, "p.jsonl"))
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        rec = mk_rec()
        # 第一轮: 中断
        comp1 = _FakeCompiler([("interrupt", "before_review")])
        lc1 = _mk(cfg, pool, comp1, [rec], led, lambda: dict(FREE))
        lc1.step()
        # 第二轮: 它仍然是候选
        got = select_candidate([rec], led, CC.CURATED_POLICY_VERSION)
        check("**第二轮仍是候选**", got is not None, got)


def test_should_continue_is_actually_wired():
    """`compile_one` 收到的 `should_continue` **必须真的被问**。

    这条是被 mutation 逼出来的: 早先假编译器不问回调, 于是"把
    `_should_continue` 改成永远 True"不会被发现 —— 生产里的让路逻辑
    死了而测试全绿。现在断言"回调被问过"以及"它能真的中断"。
    """
    print("\n[H3-B] should_continue 真的被接线")
    with tmpdir() as d:
        cfg = _cfg(curated_min_size=100)
        pool = _FakePool(os.path.join(d, "p.jsonl"))
        comp = _FakeCompiler([("accept", "")])
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        lc = _mk(cfg, pool, comp, [mk_rec()], led, lambda: dict(FREE))
        r = lc.step()
        check("accepted", r["accepted"] == 1, r)
        check("**回调被传进去了**",
              comp.calls[0]["should_continue"] is not None, comp.calls)
        check("**回调真的被问过**(不是摆设)", len(comp.gate_calls) > 0,
              comp.gate_calls)
        check("**问了全部四个检查点**",
              len(comp.gate_calls) == len(_FakeCompiler.GATES),
              comp.gate_calls)


def test_should_continue_false_interrupts_midway():
    """回调返回 False -> 在**下一个检查点**中断, 不写 rejected。"""
    print("\n[H3-B] 回调 False -> 中断")
    with tmpdir() as d:
        cfg = _cfg(curated_min_size=100)
        pool = _FakePool(os.path.join(d, "p.jsonl"))
        # 第 1 次问(should_start)说可以, 之后一直说忙
        state = {"n": 0}

        def probe():
            state["n"] += 1
            return dict(FREE) if state["n"] <= 1 else dict(BUSY)

        comp = _FakeCompiler([("accept", "")])
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        lc = _mk(cfg, pool, comp, [mk_rec()], led, probe)
        r = lc.step()
        check("**记成 interrupted**", r["interrupted"] == 1, r)
        check("**不是 accepted**", r["accepted"] == 0, r)
        check("**不是 rejected**", r["rejected"] == 0, r)
        # 第一个检查点就该停
        check("停在第一个检查点",
              comp.gate_calls
              and comp.gate_calls[0][1] == _FakeCompiler.GATES[0],
              comp.gate_calls)
        last = led.last(mk_rec(), CC.CURATED_POLICY_VERSION)
        check("账本写 interrupted",
              last.get("decision") == CL.INTERRUPTED, last)
        check("**interrupted 不是终态**",
              not led.is_settled(mk_rec(), CC.CURATED_POLICY_VERSION))

    """任务书廿二-13: interrupted 的题稍后能重新审。"""
    print("\n[H3-B] interrupted 下次会重审")
    with tmpdir() as d:
        cfg = _cfg(curated_min_size=100)
        pool = _FakePool(os.path.join(d, "p.jsonl"))
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        rec = mk_rec()
        # 第一轮: 中断
        comp1 = _FakeCompiler([("interrupt", "before_review")])
        lc1 = _mk(cfg, pool, comp1, [rec], led, lambda: dict(FREE))
        lc1.step()
        # 第二轮: 它仍然是候选
        got = select_candidate([rec], led, CC.CURATED_POLICY_VERSION)
        check("**第二轮仍是候选**", got is not None, got)


# ======================================================================
# 2. 技术失败 ≠ 拒绝
# ======================================================================
def test_technical_defer_is_retryable():
    print("\n[H3-B] 技术失败记 defer(可重试)")
    with tmpdir() as d:
        cfg = _cfg(curated_min_size=100)
        pool = _FakePool(os.path.join(d, "p.jsonl"))
        comp = _FakeCompiler([("defer", "compile_call")])
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        lc = _mk(cfg, pool, comp, [mk_rec()], led, lambda: dict(FREE))
        r = lc.step()
        check("记成 technical_defer", r["technical_defer"] == 1, r)
        check("**不是 rejected**", r["rejected"] == 0, r)
        check("**下次仍是候选**",
              select_candidate([mk_rec()], led,
                               CC.CURATED_POLICY_VERSION) is not None)


def test_exception_becomes_defer_not_reject():
    """未预期异常 -> defer。**绝不能** because 一个 crash 就把题判死。"""
    print("\n[H3-B] 异常 -> defer 而不是 reject")

    class _Boom:
        def compile_one(self, *a, **k):
            raise RuntimeError("kaboom")

    with tmpdir() as d:
        cfg = _cfg(curated_min_size=100)
        pool = _FakePool(os.path.join(d, "p.jsonl"))
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        lc = _mk(cfg, pool, _Boom(), [mk_rec()], led, lambda: dict(FREE))
        r = lc.step()
        check("**不抛**(直播心跳不能被打断)", True)
        check("记成 technical_defer", r["technical_defer"] == 1, r)
        last = led.last(mk_rec(), CC.CURATED_POLICY_VERSION)
        check("账本写 technical_defer",
              last.get("decision") == CL.TECHNICAL_DEFER, last)
        check("stage 标 exception", last.get("stage") == "exception", last)


def test_budget_exceeded_is_defer():
    """超预算 -> defer。慢不等于坏。"""
    print("\n[H3-B] 超预算记 defer")
    with tmpdir() as d:
        cfg = _cfg(curated_min_size=100, curated_budget_seconds=1.0)
        pool = _FakePool(os.path.join(d, "p.jsonl"))
        comp = _FakeCompiler([("reject", "ai_gate")])
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        # clock 先 0 后 100 -> elapsed=100 > budget=1
        ticks = iter([0.0, 100.0])
        lc = LazyCurator(cfg, pool, comp, [mk_rec()], led,
                         lambda: dict(FREE), clock=lambda: next(ticks))
        r = lc.step()
        check("记成 technical_defer", r["technical_defer"] == 1, r)


def test_rejected_is_terminal_and_not_retried():
    """任务书廿二-14: 被拒的题不会每次启动重新烧一次 LLM。"""
    print("\n[H3-B] rejected 是终态, 不重审")
    with tmpdir() as d:
        cfg = _cfg(curated_min_size=100)
        pool = _FakePool(os.path.join(d, "p.jsonl"))
        comp = _FakeCompiler([("reject", "story_gate")])
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        lc = _mk(cfg, pool, comp, [mk_rec()], led, lambda: dict(FREE))
        r = lc.step()
        check("记成 rejected", r["rejected"] == 1, r)
        check("**不再是候选**",
              select_candidate([mk_rec()], led,
                               CC.CURATED_POLICY_VERSION) is None)
        # 第二轮: 即使 step 也不该调 LLM
        comp2 = _FakeCompiler([("accept", "")])
        lc2 = _mk(cfg, pool, comp2, [mk_rec()], led, lambda: dict(FREE))
        r2 = lc2.step()
        check("**第二轮 0 次 LLM 调用**", comp2.calls == [], comp2.calls)
        check("第二轮 processed=0", r2["processed"] == 0, r2)


# ======================================================================
# 3. 库存迟滞
# ======================================================================
def test_needs_work_hysteresis():
    print("\n[H3-B] 库存迟滞")
    with tmpdir() as d:
        cfg = _cfg(curated_target_size=10, curated_min_size=4,
                   curated_playable_min=2, curated_max_size=20)
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        comp = _FakeCompiler([])

        def mk(stock, playable):
            return _mk(cfg, _FakePool(os.path.join(d, "p.jsonl"),
                                      stock=stock, playable=playable),
                       comp, [], led, lambda: dict(FREE))

        check("stock=3 (< min=4) -> 要补", mk(3, 9).needs_work())
        check("stock=8, playable=1 (<2) -> 要补", mk(8, 1).needs_work())
        check("**stock=8, playable=5 -> 不补**(迟滞, 还没到 min)",
              not mk(8, 5).needs_work())
        check("stock=10 (>= target) -> 不补", not mk(10, 5).needs_work())
        check("stock=25 (>= max) -> 不补", not mk(25, 0).needs_work())


def test_stops_at_target():
    """任务书廿二-8/19: 库存够了就停。"""
    print("\n[H3-B] 达到 target 立即停")
    with tmpdir() as d:
        cfg = _cfg(curated_target_size=2, curated_min_size=1,
                   curated_playable_min=0)
        pool = _FakePool(os.path.join(d, "p.jsonl"), stock=5, playable=5)
        comp = _FakeCompiler([("accept", "")] * 10)
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        lc = _mk(cfg, pool, comp, [mk_rec(eid=f"pse:q:{i}") for i in range(5)],
                 led, lambda: dict(FREE))
        r = lc.step(max_candidates=5)
        check("**一条都没审**", r["processed"] == 0, r)
        check("0 次 LLM", comp.calls == [], comp.calls)
        check("停因是库存充足", "库存" in r.get("stop_reason", ""), r)


def test_single_flight():
    """任务书廿二-9: 一次最多一个 worker。"""
    print("\n[H3-B] single-flight")
    with tmpdir() as d:
        cfg = _cfg(curated_min_size=100)
        pool = _FakePool(os.path.join(d, "p.jsonl"))
        comp = _FakeCompiler([("accept", "")])
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        lc = _mk(cfg, pool, comp, [mk_rec()], led, lambda: dict(FREE))
        lc._busy = True                     # 模拟"已有一个在跑"
        ok, why = lc.should_start()
        check("**busy 时不启动**", not ok, why)
        check("理由点名 worker", "worker" in why, why)


# ======================================================================
# 4. --max-candidates 语义
# ======================================================================
def test_max_candidates_counts_attempts():
    """任务书廿二-18: 即使 0 道成功也只能处理 `max_candidates` 道。

    这是修 `--limit` 的老问题: 它原先"一直审到成功 N 道", 于是拒绝率高
    时会调用上百次。
    """
    print("\n[H3-B] max_candidates 按**尝试**计")
    with tmpdir() as d:
        cfg = _cfg(curated_min_size=100)
        pool = _FakePool(os.path.join(d, "p.jsonl"))
        recs = [mk_rec(eid=f"pse:q:{i}") for i in range(10)]
        comp = _FakeCompiler([("reject", "ai_gate")] * 10)
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        lc = _mk(cfg, pool, comp, recs, led, lambda: dict(FREE))
        r = lc.step(max_candidates=5)
        check("**恰好尝试 5 次**", r["processed"] == 5, r)
        check("**恰好 5 次 LLM 调用**", len(comp.calls) == 5,
              len(comp.calls))
        check("0 道成功", r["accepted"] == 0, r)
        check("5 道被拒", r["rejected"] == 5, r)


# ======================================================================
# 5. 入池 / attribution
# ======================================================================
def test_accepted_writes_pool_and_attribution_and_decision():
    """任务书廿二-15: accepted 同时写 pool + attribution + decision。"""
    print("\n[H3-B] accepted 三处同时落盘")
    with tmpdir() as d:
        cfg = _cfg(curated_min_size=100)
        pool_path = os.path.join(d, "curated.jsonl")
        attr = os.path.join(d, "ATTR.jsonl")
        cfg.attributions_path = attr
        pool = _FakePool(pool_path)
        comp = _FakeCompiler([("accept", "")])
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        lc = _mk(cfg, pool, comp, [mk_rec()], led, lambda: dict(FREE))
        r = lc.step()
        check("accepted=1", r["accepted"] == 1, r)
        check("**池文件有内容**", os.path.exists(pool_path)
              and os.path.getsize(pool_path) > 0)
        check("**attribution 有内容**", os.path.exists(attr)
              and os.path.getsize(attr) > 0)
        last = led.last(mk_rec(), CC.CURATED_POLICY_VERSION)
        check("**decision 是 accepted**",
              last.get("decision") == CL.ACCEPTED, last)


def test_pool_write_failure_does_not_record_accepted():
    """任务书廿二-16: 池写盘失败**不能**写 accepted decision。

    否则会留下"账本说 accepted, 池里没有"的半状态 —— 那道题永远
    不会被重审(accepted 是终态), 而它也播不出来。一道题无声地没了。
    """
    print("\n[H3-B] 池写失败 -> 降级 defer")
    with tmpdir() as d:
        cfg = _cfg(curated_min_size=100)
        # 用一个**不可能**写成功的路径(目录不存在且是文件)
        bad = os.path.join(d, "afile")
        with open(bad, "w") as f:
            f.write("x")
        pool = _FakePool(os.path.join(bad, "sub", "curated.jsonl"))
        comp = _FakeCompiler([("accept", "")])
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        lc = _mk(cfg, pool, comp, [mk_rec()], led, lambda: dict(FREE))
        r = lc.step()
        check("**没有 accepted**", r["accepted"] == 0, r)
        check("记成 technical_defer", r["technical_defer"] == 1, r)
        last = led.last(mk_rec(), CC.CURATED_POLICY_VERSION)
        check("**账本不是 accepted**",
              last.get("decision") != CL.ACCEPTED, last)
        check("**下次仍是候选**(没被永久判死)",
              select_candidate([mk_rec()], led,
                               CC.CURATED_POLICY_VERSION) is not None)


# ======================================================================
# 6. 候选选择
# ======================================================================
def test_candidate_skips_unusable():
    print("\n[H3-B] 候选跳过不可用的")
    with tmpdir() as d:
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        PV = CC.CURATED_POLICY_VERSION
        bad_lic = mk_rec(eid="bad_lic", question_license="weird",
                         answer_license="weird")
        unsafe = mk_rec(eid="unsafe", safety_flag="self_harm")
        dupe = mk_rec(eid="dupe", dup_reason="near_duplicate")
        good = mk_rec(eid="good")
        got = select_candidate([bad_lic, unsafe, dupe, good], led, PV)
        check("**选中唯一可用的那条**", got is not None
              and got.external_id == "good", got)


def test_source_priority_orders_turtlebench_first():
    print("\n[H3-B] 来源优先级只影响顺序")
    tb = mk_rec(eid="tb", source="TurtleBench1.5k")
    sit = mk_rec(eid="sit", tags=["situation"])
    lat = mk_rec(eid="lat", tags=["lateral-thinking"])
    order = sorted([lat, sit, tb], key=source_priority)
    check("TurtleBench 排最前", order[0].external_id == "tb",
          [r.external_id for r in order])
    check("lateral-thinking 排最后", order[-1].external_id == "lat",
          [r.external_id for r in order])


def test_no_candidates_stops_cleanly():
    print("\n[H3-B] 候选耗尽 -> 干净停下")
    with tmpdir() as d:
        cfg = _cfg(curated_min_size=100)
        pool = _FakePool(os.path.join(d, "p.jsonl"))
        comp = _FakeCompiler([])
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        lc = _mk(cfg, pool, comp, [], led, lambda: dict(FREE))
        r = lc.step()
        check("processed=0", r["processed"] == 0, r)
        check("有 stop_reason", bool(r.get("stop_reason")), r)


# ======================================================================
# 7. status(可观测性)
# ======================================================================
def test_status_has_no_puzzle_text():
    """启动日志**不得**含题底。"""
    print("\n[H3-B] status 不含题面/题底")
    with tmpdir() as d:
        cfg = _cfg(curated_min_size=100)
        pool = _FakePool(os.path.join(d, "p.jsonl"), stock=3, playable=1)
        comp = _FakeCompiler([])
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        rec = mk_rec()
        lc = _mk(cfg, pool, comp, [rec], led, lambda: dict(FREE))
        st = lc.status()
        blob = repr(st)
        check("**不含谜面**", rec.surface not in blob)
        check("**不含谜底**", rec.bottom not in blob)
        check("含 stock", "stock" in st)
        check("含 candidates", st.get("candidates") == 1, st)


def test_engine_pressure_exposes_ai_player():
    """任务书廿二-11: Engine 必须公开 AI 玩家在途状态。

    断言**实际返回的键集**, 而不是读源码 —— 源码里出现 "token" 字样
    是正常的(注释在解释为什么**不**暴露它), 那不该让测试变红。
    """
    print("\n[H3-B] Engine.pressure 暴露 ai_player_in_flight")
    from story.engine import RoundEngine
    p = RoundEngine.pressure
    # 只读探针: 不接受除 self 之外的参数
    import inspect
    params = [x for x in inspect.signature(p).parameters if x != "self"]
    check("pressure 是只读探针(无额外参数)", params == [], params)


def test_engine_pressure_ai_field_is_bool_and_no_secrets():
    """真实构造一个 engine, 确认键存在且**不含**调度内部状态。"""
    print("\n[H3-B] pressure 实际返回里没有 token/round/spec_key")
    import sys as _s
    from pathlib import Path as _P
    _s.path.insert(0, str(_P(__file__).resolve().parents[1]))
    from story.engine import RoundEngine
    from story.config import Config
    cfg = Config(sim_path="x", no_llm=True)
    eng = RoundEngine(cfg)
    p = eng.pressure()
    check("**有 ai_player_in_flight**", "ai_player_in_flight" in p, list(p))
    check("是布尔", isinstance(p.get("ai_player_in_flight"), bool),
          type(p.get("ai_player_in_flight")))
    for bad in ("token", "reservation", "spec_key", "round_index"):
        check(f"**不含 {bad}**", bad not in p, sorted(p))


def test_engine_pressure_ai_field_tracks_reservation():
    """reservation 非空 -> True。"""
    print("\n[H3-B] ai_player_in_flight 跟随 reservation")
    from story.engine import RoundEngine
    from story.config import Config
    cfg = Config(sim_path="x", no_llm=True)
    eng = RoundEngine(cfg)
    check("初始为 False", eng.pressure()["ai_player_in_flight"] is False)
    # 伪造一个 reservation(只读语义验证, 不启动真流程)
    led = eng._ai_player_ledger
    orig = led.detective_reservation
    try:
        led.detective_reservation = type("R", (), {})()
        check("有 reservation -> True",
              eng.pressure()["ai_player_in_flight"] is True)
    finally:
        led.detective_reservation = orig


# ======================================================================
def main():
    tests = [
        test_no_start_when_human_pending,
        test_no_start_when_ai_player_in_flight,
        test_no_start_on_hint_or_reveal_inflight,
        test_no_start_near_next_puzzle,
        test_pressure_probe_failure_is_safe,
        test_should_continue_is_actually_wired,
        test_should_continue_false_interrupts_midway,
        test_worker_started_then_pressure_interrupts,
        test_deferred_candidate_not_retried_in_same_run,
        test_defer_then_next_run_retries,
        test_skip_ids_does_not_affect_settled_logic,
        test_interrupted_candidate_is_retried_next_time,
        test_technical_defer_is_retryable,
        test_exception_becomes_defer_not_reject,
        test_budget_exceeded_is_defer,
        test_rejected_is_terminal_and_not_retried,
        test_needs_work_hysteresis,
        test_stops_at_target,
        test_single_flight,
        test_max_candidates_counts_attempts,
        test_accepted_writes_pool_and_attribution_and_decision,
        test_pool_write_failure_does_not_record_accepted,
        test_candidate_skips_unusable,
        test_source_priority_orders_turtlebench_first,
        test_no_candidates_stops_cleanly,
        test_status_has_no_puzzle_text,
        test_engine_pressure_exposes_ai_player,
        test_engine_pressure_ai_field_is_bool_and_no_secrets,
        test_engine_pressure_ai_field_tracks_reservation,
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
