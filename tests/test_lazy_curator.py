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

import json
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
from tools.curated_common import RawCuratedPuzzle, read_jsonl  # noqa: E402

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


#: 空闲的**QA** —— 唯一允许后台审题的稳态。刻意带上 phase: H3-D 起
#: phase 是**白名单**(SETTING/REVEALING/STOPPED 一律禁止), 所以
#: "没有 phase" 与 "phase 是 SETTING" 是**同样**的拒绝。这很重要 ——
#: 早先的 fixture 全靠压力字段判断, 于是"忘了给 phase"会让整批测试
#: 静默走另一条分支。
_QA = "qa"
_REVEALED = "revealed"
FREE = {"phase": _QA}
BUSY = {"phase": _QA, "pending": 1}
AI_BUSY = {"phase": _QA, "ai_player_in_flight": True}


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
                     lambda k=key: dict(FREE, **{k: True}))
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
                 lambda: dict(FREE, reveal_remaining_seconds=5.0))
        ok, why = lc.should_start()
        check("**只剩 5s -> 不启动**", not ok, why)
        lc2 = _mk(cfg, pool, comp, [mk_rec()], led,
                  lambda: dict(FREE, phase=_REVEALED,
                               reveal_remaining_seconds=30.0))
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
# 8. H3-D: phase 白名单(§二)
# ======================================================================
def test_forbidden_phases_never_start():
    """SETTING / REVEALING / STOPPED 一律不启动 —— **哪怕压力全为 0**。

    这是 §二 的核心断言。旧实现只查压力字段, 于是
    `SETTING + 正式 RIDDLE worker 已启动 + Lazy Curator 同时打 LLM`
    是可能的 —— 出题是直播的**主线**, 后台审题抢它的网关会直接变成
    "观众等下一题"。
    """
    print("\n[H3-D] 禁止的 phase 不启动(即使压力全 0)")
    for ph in ("setting", "revealing", "stopped", "idle"):
        with tmpdir() as d:
            cfg = _cfg(curated_min_size=100)
            pool = _FakePool(os.path.join(d, "p.jsonl"))
            comp = _FakeCompiler([("accept", "")])
            led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
            # ⚠️ 除了 phase **一个压力字段都没有** —— 全是 0。
            lc = _mk(cfg, pool, comp, [mk_rec()], led,
                     lambda p=ph: {"phase": p})
            ok, why = lc.should_start()
            check(f"**{ph} -> 不启动**", not ok, why)
            check(f"{ph} 理由点名 phase", "phase" in why, why)
            r = lc.step()
            check(f"{ph} 一次 LLM 都没调", comp.calls == [], comp.calls)


def test_setting_with_riddle_inflight_never_starts():
    """`riddle_inflight` 单独也足以拦住(§二的后半条)。"""
    print("\n[H3-D] 正式出题在途 -> 不启动")
    with tmpdir() as d:
        cfg = _cfg(curated_min_size=100)
        pool = _FakePool(os.path.join(d, "p.jsonl"))
        comp = _FakeCompiler([("accept", "")])
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        lc = _mk(cfg, pool, comp, [mk_rec()], led,
                 lambda: dict(FREE, riddle_inflight=True))
        ok, why = lc.should_start()
        check("**不启动**", not ok, why)
        check("理由点名出题", "出题" in why, why)


def test_qa_and_revealed_are_allowed():
    """QA 与 REVEALED 是**允许**的两个 phase —— 否则补池永不工作。"""
    print("\n[H3-D] QA / REVEALED 允许启动")
    with tmpdir() as d:
        cfg = _cfg(curated_min_size=100)
        pool = _FakePool(os.path.join(d, "p.jsonl"))
        comp = _FakeCompiler([("accept", "")])
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        for ph in (_QA, _REVEALED):
            lc = _mk(cfg, pool, comp, [mk_rec()], led,
                     lambda p=ph: {"phase": p})
            ok, why = lc.should_start()
            check(f"**{ph} 允许**", ok, why)


def test_phase_accepts_enum_object_too():
    """`pressure()` 给的是 Phase **枚举**, 测试假件给字符串 —— 都要认。

    不认的后果很隐蔽: 生产侧传枚举 -> `_phase_ok` 判不出来 ->
    后台审题**永远不启动**, 而所有测试(用字符串)全绿。
    """
    print("\n[H3-D] phase 枚举与字符串都认")
    from story.state import Phase
    with tmpdir() as d:
        cfg = _cfg(curated_min_size=100)
        pool = _FakePool(os.path.join(d, "p.jsonl"))
        comp = _FakeCompiler([("accept", "")])
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        lc = _mk(cfg, pool, comp, [mk_rec()], led,
                 lambda: {"phase": Phase.QA})
        ok, why = lc.should_start()
        check("**Phase.QA 枚举被认**", ok, why)
        lc2 = _mk(cfg, pool, comp, [mk_rec()], led,
                  lambda: {"phase": Phase.SETTING})
        ok2, why2 = lc2.should_start()
        check("**Phase.SETTING 枚举被禁**", not ok2, why2)


def test_pressure_has_riddle_inflight_bool():
    """Engine.pressure() 必须公开 riddle_inflight, 且**只**是布尔。"""
    print("\n[H3-D] pressure 暴露 riddle_inflight")
    from story.engine import RoundEngine
    from story.config import Config
    cfg = Config(sim_path="x", no_llm=True)
    eng = RoundEngine(cfg)
    p = eng.pressure()
    check("**有 riddle_inflight**", "riddle_inflight" in p, sorted(p))
    check("是布尔", isinstance(p.get("riddle_inflight"), bool),
          type(p.get("riddle_inflight")))
    for bad in ("setting_deadline", "setting_attempts", "spec_key"):
        check(f"**不含 {bad}**", bad not in p, sorted(p))


# ======================================================================
# 9. H3-D: refill cycle(§三)
# ======================================================================
def test_refill_cycle_goes_all_the_way_to_target():
    """**3 -> 4 -> 5 -> ... -> 10 一路补到 target**, 不是在 min 就停。

    这是 §三 的核心: 旧语义是"跌破 4 补一两道, 回到 4 就停", 那会让
    库存长期钉在低水位 —— 而低水位正是最危险的(任何一次审题失败都
    直接变成"下一题现场生成", 观众干等)。
    """
    print("\n[H3-D] refill cycle 一路补到 target")
    with tmpdir() as d:
        cfg = _cfg(curated_target_size=10, curated_min_size=4,
                   curated_playable_min=2, curated_max_size=20)
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        comp = _FakeCompiler([])

        def mk(stock, playable=9):
            return _mk(cfg, _FakePool(os.path.join(d, "p.jsonl"),
                                      stock=stock, playable=playable),
                       comp, [], led, lambda: dict(FREE))

        # 从未进入 refill cycle: stock=8 在 min 之上 -> 不启动
        lc_fresh = mk(8)
        check("**从未 refill: stock=8 -> 不启动**",
              not lc_fresh.needs_work())
        check("也还没进入 cycle", not lc_fresh._refilling)

        # 跌破 min -> 进入 cycle
        lc = mk(3)
        check("stock=3 -> 启动", lc.needs_work())
        check("**进入 refill cycle**", lc._refilling)
        # 现在库存涨到 4/5/.../9 —— 只要还没到 target 就必须继续补
        for s in range(4, 10):
            lc._stock = lambda s=s, _lc=lc: (s, 9)
            check(f"cycle 内 stock={s} -> 继续补", lc.needs_work())
            check(f"stock={s} 仍在 cycle", lc._refilling)
        lc._stock = lambda: (10, 9)
        check("stock=10 (>= target) -> 停", not lc.needs_work())
        check("**退出 refill cycle**", not lc._refilling)


def test_playable_below_min_enters_refill_cycle():
    """`playable < playable_min` 也触发一轮 refill cycle。

    它与 stock 那个条件是**两个问题**: 池里可能堆着 8 道但全被当前
    窗口挡住 -> playable=0, 而下一题马上要上。
    """
    print("\n[H3-D] playable 跌破线也进入 cycle")
    with tmpdir() as d:
        cfg = _cfg(curated_target_size=10, curated_min_size=4,
                   curated_playable_min=2, curated_max_size=20)
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        comp = _FakeCompiler([])
        lc = _mk(cfg, _FakePool(os.path.join(d, "p.jsonl"),
                                stock=8, playable=1),
                 comp, [], led, lambda: dict(FREE))
        check("stock=8 但 playable=1 -> 启动", lc.needs_work())
        check("**进入 refill cycle**", lc._refilling)
        # playable 恢复但 stock 还没到 target -> 继续补
        lc._stock = lambda: (8, 9)
        check("**playable 恢复了但仍要补到 target**", lc.needs_work())


def test_max_size_is_a_hard_cap_even_while_refilling():
    """`curated_max_size` 是**绝对**硬上限, 不能被 refilling 绕过。"""
    print("\n[H3-D] max_size 硬上限不被 refilling 绕过")
    with tmpdir() as d:
        cfg = _cfg(curated_target_size=10, curated_min_size=4,
                   curated_playable_min=2, curated_max_size=20)
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        comp = _FakeCompiler([])
        lc = _mk(cfg, _FakePool(os.path.join(d, "p.jsonl"), stock=3),
                 comp, [], led, lambda: dict(FREE))
        check("先进入 cycle", lc.needs_work() and lc._refilling)
        lc._stock = lambda: (25, 25)
        check("**到 max -> 不补**", not lc.needs_work())
        check("**且退出 cycle**", not lc._refilling)


def test_live_pressure_does_not_clear_refill_intent():
    """直播压力只**暂停** worker, 不清除 refill 意图。

    §三 明确: "不要因为一次真人提问就永久清除 refill intent"。
    清掉的后果是每次有人提问就把补水计划归零 —— 而观众提问是
    **常态**, 于是补池永远做不成。
    """
    print("\n[H3-D] 压力不清除 refill 意图")
    with tmpdir() as d:
        cfg = _cfg(curated_target_size=10, curated_min_size=4,
                   curated_playable_min=2, curated_max_size=20)
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        comp = _FakeCompiler([("accept", "")])
        busy = {"phase": _QA, "pending": 1}
        lc = _mk(cfg, _FakePool(os.path.join(d, "p.jsonl"), stock=3),
                 comp, [mk_rec()], led, lambda: dict(busy))
        check("忙 -> 不启动", not lc.should_start()[0])
        check("**但仍然进入了 refill cycle**", lc._refilling)
        # 压力过去 -> 仍然要补(意图没丢)
        lc._pressure = lambda: dict(FREE)
        ok, why = lc.should_start()
        check("**压力过去后仍然要补**", ok, why)


# ======================================================================
# 10. H3-D: 非阻塞调度(§一)
# ======================================================================
def test_on_tick_returns_fast_while_worker_blocks():
    """**§一 的核心断言**: worker 卡在 LLM 里时, `on_tick` 立刻返回。

    这条测的是**调度层**, 不是 `step()` 本身 —— H3-B 那版把 `step()`
    放进 scheduler 线程, 于是 compile_one 一跑十几秒, tick 定时 /
    hint deadline / reveal deadline / 下一题 deadline / push 全部
    被拖住。那不是"这一拍慢一点", 是**心跳停了**。
    """
    print("\n[H3-D] on_tick 在 worker 阻塞时立刻返回")
    import threading
    import time as _time
    with tmpdir() as d:
        cfg = _cfg(curated_min_size=100)
        pool = _FakePool(os.path.join(d, "p.jsonl"))
        release = threading.Event()

        class _BlockingCompiler:
            """模拟一个卡住的 LLM 调用。"""

            def __init__(self):
                self.entered = threading.Event()
                self.calls = []

            def compile_one(self, rec, **kw):
                self.calls.append(getattr(rec, "external_id", ""))
                self.entered.set()
                release.wait(10)          # 卡住, 直到测试放行
                return None, {"external_id": getattr(rec, "external_id", ""),
                              "accepted": False, "stage": "ai_gate",
                              "reject_reasons": ["x"]}

        comp = _BlockingCompiler()
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        lc = _mk(cfg, pool, comp, [mk_rec()], led, lambda: dict(FREE))
        try:
            t0 = _time.monotonic()
            first = lc.on_tick()
            dt = _time.monotonic() - t0
            check("**第一次 on_tick 提交了活**", first["submitted"], first)
            check(f"**立刻返回**({dt*1000:.1f}ms < 500ms)", dt < 0.5, dt)
            check("worker 真的开始跑了", comp.entered.wait(5))
            # worker 卡着的时候, 后续 on_tick 也必须立刻返回
            t1 = _time.monotonic()
            second = lc.on_tick()
            dt2 = _time.monotonic() - t1
            check("**worker 卡住时 on_tick 仍然立刻返回**", dt2 < 0.5, dt2)
            check("**不能重复提交**(single-flight)",
                  not second["submitted"], second)
        finally:
            release.set()
            check("**worker 最终收尾**", lc.wait_idle(10))
            lc.shutdown(wait=True)


def test_on_tick_drops_submit_when_busy():
    """single-flight: 已有活时**丢弃**新提交, 不排队。"""
    print("\n[H3-D] 有活时不排队")
    with tmpdir() as d:
        cfg = _cfg(curated_min_size=100)
        pool = _FakePool(os.path.join(d, "p.jsonl"))
        comp = _FakeCompiler([("accept", "")])
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        lc = _mk(cfg, pool, comp, [mk_rec()], led, lambda: dict(FREE))
        lc._busy = True                      # 模拟"已有一个在跑"
        r = lc.on_tick()
        check("**没有提交**", not r["submitted"], r)
        check("理由点名 worker", "worker" in r["reason"], r)
        check("一次 LLM 都没调", comp.calls == [], comp.calls)


def test_worker_survives_compiler_exception():
    """worker 里抛异常**不能**让 `_busy` 永远卡在 True。"""
    print("\n[H3-D] worker 异常后仍然可再提交")
    with tmpdir() as d:
        cfg = _cfg(curated_min_size=100)
        pool = _FakePool(os.path.join(d, "p.jsonl"))
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))

        class _Boom:
            def __init__(self):
                self.n = 0

            def compile_one(self, rec, **kw):
                self.n += 1
                raise RuntimeError("kaboom")

        comp = _Boom()
        lc = _mk(cfg, pool, comp, [mk_rec(eid="pse:q:1"),
                                   mk_rec(eid="pse:q:2")],
                 led, lambda: dict(FREE))
        try:
            lc.on_tick()
            check("**异常后 worker 收尾**", lc.wait_idle(10))
            check("**`_busy` 没有卡住**", not lc._busy)
            # 还能再提交(说明没有死锁)
            lc5 = lc
            r2 = lc5.on_tick()
            check("**还能再提交**", r2["submitted"], r2)
            lc5.wait_idle(10)
        finally:
            lc.shutdown(wait=True)


def test_step_is_still_synchronous_for_cli():
    """CLI 仍然用**同步** `step()` —— 它是离线批处理, 不需要 worker。

    这条守的是"重构没有把同步路径弄丢": 预热 CLI 直接调 `step()`,
    若它变成必须走 worker 才能干活, 预热就会静默变成 no-op。
    """
    print("\n[H3-D] step() 仍然同步可用(CLI 路径)")
    with tmpdir() as d:
        cfg = _cfg(curated_min_size=100)
        pool = _FakePool(os.path.join(d, "p.jsonl"))
        comp = _FakeCompiler([("accept", "")])
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        lc = _mk(cfg, pool, comp, [mk_rec()], led, lambda: dict(FREE))
        r = lc.step()                        # 直接调, 不等 worker
        check("**同步跑完了**", r["accepted"] == 1, r)
        check("确实调了 LLM", len(comp.calls) == 1, comp.calls)


def test_tried_is_shared_across_ticks():
    """`_tried` 必须**跨 tick** 累积 —— 否则 defer 的题每拍被重审一次。

    这是 H3-D 把 `tried` 从 step 局部变量改成实例状态的**唯一理由**:
    worker 之后, 每次 on_tick 都是一次独立的 `step()` 调用, 局部 set
    会在每次提交时重置。
    """
    print("\n[H3-D] _tried 跨 tick 累积")
    with tmpdir() as d:
        cfg = _cfg(curated_min_size=100)
        pool = _FakePool(os.path.join(d, "p.jsonl"))
        recs = [mk_rec(eid="pse:q:1"), mk_rec(eid="pse:q:2")]
        comp = _FakeCompiler([("defer", "compile_call"),
                              ("defer", "compile_call")])
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        lc = _mk(cfg, pool, comp, recs, led, lambda: dict(FREE))
        lc.step(max_candidates=1)            # 第一次: 碰 q:1
        lc.step(max_candidates=1)            # 第二次: 必须碰 q:2
        check("**两次碰到的是不同的题**",
              len({c["external_id"] for c in comp.calls}) == 2,
              [c["external_id"] for c in comp.calls])
        check("各只调一次", len(comp.calls) == 2, len(comp.calls))


def test_status_exposes_refilling():
    """`refilling` 必须出现在 status 里 —— 否则"补池怎么没动静"无从解释。"""
    print("\n[H3-D] status 暴露 refilling")
    with tmpdir() as d:
        cfg = _cfg(curated_target_size=10, curated_min_size=4,
                   curated_playable_min=2)
        pool = _FakePool(os.path.join(d, "p.jsonl"), stock=3, playable=3)
        comp = _FakeCompiler([])
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        lc = _mk(cfg, pool, comp, [], led, lambda: dict(FREE))
        check("初始未 refill", lc.status().get("refilling") is False)
        lc.needs_work()                      # 触发进入 cycle
        st = lc.status()
        check("**status 里 refilling=True**", st.get("refilling") is True, st)
        check("有 target/min", "target" in st and "min" in st, sorted(st))


# ======================================================================
# 11. H3-D: accepted 是最终 commit marker(§四)
# ======================================================================
def _hist(led, rec, policy_version):
    """账本里这条记录**全部**历史行(不只是最后一条)。

    §四 明确要求: 断言不能只看 `ledger.last(...)`, 必须查**整个
    append-only 历史** —— "池写失败 -> 该 key 从未出现 accepted 行"。
    """
    from tools.curated_ledger import content_hash_of, decision_key
    k = decision_key(getattr(rec, "external_id", ""), content_hash_of(rec),
                     policy_version)
    out = []
    for row in led.rows:
        rowk = decision_key(row.get("external_id", ""),
                            row.get("content_hash", ""),
                            row.get("policy_version", ""))
        if rowk == k:
            out.append(row)
    return out


def test_pool_write_failure_leaves_no_accepted_row_in_history():
    """§四: 池写失败 -> 该 key 在**整个历史**里一条 accepted 都没有。"""
    print("\n[H3-D] 池写失败 -> 历史里从无 accepted")
    with tmpdir() as d:
        cfg = _cfg(curated_min_size=100)
        bad = os.path.join(d, "afile")
        with open(bad, "w") as f:
            f.write("x")
        pool = _FakePool(os.path.join(bad, "sub", "curated.jsonl"))
        comp = _FakeCompiler([("accept", "")])
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        lc = _mk(cfg, pool, comp, [mk_rec()], led, lambda: dict(FREE))
        r = lc.step()
        check("没有 accepted", r["accepted"] == 0, r)
        check("记成 technical_defer", r["technical_defer"] == 1, r)
        hist = _hist(led, mk_rec(), CC.CURATED_POLICY_VERSION)
        check("**历史里没有任何 accepted 行**",
              all(h.get("decision") != CL.ACCEPTED for h in hist), hist)
        check("历史里确实有 defer 行",
              any(h.get("decision") == CL.TECHNICAL_DEFER for h in hist), hist)
        check("stage 点名 pool_write",
              any("pool_write" in str(h.get("stage")) for h in hist), hist)
        check("下次仍是候选",
              select_candidate([mk_rec()], led,
                               CC.CURATED_POLICY_VERSION) is not None)


def test_attribution_write_failure_leaves_no_accepted_and_no_live_item():
    """§四: 署名写失败 -> **不能** accepted, 且不留 active playable 孤儿。

    这是原任务**缺掉**的那条真实测试。旧实现署名失败时只打一条 ERROR
    然后照常 accepted —— 那道题会带着"没有署名"的状态播出去。那不是
    归档完整性问题, 是**版权**问题。
    """
    print("\n[H3-D] 署名写失败 -> 无 accepted + 无活孤儿")
    with tmpdir() as d:
        cfg = _cfg(curated_min_size=100)
        pool_path = os.path.join(d, "curated.jsonl")
        # 让署名路径**不可能**写成功
        bad = os.path.join(d, "blk")
        with open(bad, "w") as f:
            f.write("x")
        cfg.attributions_path = os.path.join(bad, "sub", "ATTR.jsonl")
        pool = _FakePool(pool_path)
        comp = _FakeCompiler([("accept", "")])
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        lc = _mk(cfg, pool, comp, [mk_rec()], led, lambda: dict(FREE))
        r = lc.step()
        check("**没有 accepted**", r["accepted"] == 0, r)
        check("记成 technical_defer", r["technical_defer"] == 1, r)
        hist = _hist(led, mk_rec(), CC.CURATED_POLICY_VERSION)
        check("**历史里没有任何 accepted 行**",
              all(h.get("decision") != CL.ACCEPTED for h in hist), hist)
        check("**下次仍是候选**(可重试)",
              select_candidate([mk_rec()], led,
                               CC.CURATED_POLICY_VERSION) is not None)
        # 池里那一行必须**不可播**: 要么被墓碑作废, 要么没有 accepted 决策
        from story.pool import PuzzlePool
        rows = [r_ for r_ in read_jsonl(pool_path)] if os.path.exists(
            pool_path) else []
        voided = PuzzlePool._voided_keys(rows)
        live = []
        for i, row in enumerate(rows):
            spec = PuzzlePool._spec_from_record(row, voided, i)
            if spec is None:
                continue               # 墓碑/被作废 -> 不可播
            ok, _why = PuzzlePool._validate_pool_spec(spec)
            if ok:
                live.append(spec)
        check("**没有 active playable 的无署名题**", live == [], live)


def test_retry_after_partial_failure_no_duplicates():
    """§四: 部分失败后重试 -> **不能**产生重复的 active pool item。

    场景: 第一次署名失败(池里留了一行 + 墓碑), 第二次重试成功。
    最终必须**恰好一道**可播, 而不是两道(池里有孤儿 + 新的那道)。
    """
    print("\n[H3-D] 部分失败后重试不产生重复")
    from story.pool import PuzzlePool
    with tmpdir() as d:
        cfg = _cfg(curated_min_size=100)
        pool_path = os.path.join(d, "curated.jsonl")
        attr_path = os.path.join(d, "ATTR.jsonl")
        bad = os.path.join(d, "blk")
        with open(bad, "w") as f:
            f.write("x")
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        rec = mk_rec()

        # ---- 第一次: 署名写不进去 -> 池里留一行 + 墓碑 ----
        cfg.attributions_path = os.path.join(bad, "sub", "ATTR.jsonl")
        pool = _FakePool(pool_path)
        lc = _mk(cfg, pool, _FakeCompiler([("accept", "")]),
                 [rec], led, lambda: dict(FREE))
        r1 = lc.step()
        check("第一次没成功", r1["accepted"] == 0, r1)

        # ---- 第二次: 路径正常了 -> 这次应当成功 ----
        cfg.attributions_path = attr_path
        # 账本要指向同一个(它是**最终** commit marker)
        lc2 = _mk(cfg, pool, _FakeCompiler([("accept", "")]),
                  [rec], led, lambda: dict(FREE))
        r2 = lc2.step()
        check("第二次成功", r2["accepted"] == 1, r2)

        # ---- 关键: 池里**只剩一行**有效记录, 恰好一份署名 ----
        #
        # ⚠️ 这里断的是**池记录层**, 不是"validate_spec 过得了" ——
        # 本轮用的 `_FakeCompiler` 产出的 spec 刻意极简(puzzle="x?"),
        # 本来就不该过结构硬门。要验的是**去重**这件事: 孤儿行必须被
        # 墓碑按位置作废, 否则同一道题会进池两次。
        from story.pool import set_curated_decisions_path
        set_curated_decisions_path(led.path)
        try:
            rows = read_jsonl(pool_path)
            voided = PuzzlePool._voided_keys(rows)
            check("有墓碑", bool(voided), voided)
            specs = []
            for i, row in enumerate(rows):
                spec = PuzzlePool._spec_from_record(row, voided, i)
                if spec is not None:
                    specs.append((i, spec))
            check("**池里只剩一行有效记录**(孤儿被按位置作废)",
                  len(specs) == 1, [(i, s.external_id) for i, s in specs])
            check("**剩下的是重试的那一行**",
                  bool(specs) and specs[0][0] == len(rows) - 1,
                  [i for i, _ in specs])
            attrs = read_jsonl(attr_path)
            check("**恰好一份署名**", len(attrs) == 1, len(attrs))
            hist = _hist(led, rec, CC.CURATED_POLICY_VERSION)
            acc = [h for h in hist if h.get("decision") == CL.ACCEPTED]
            check("**恰好一条 accepted 决策**", len(acc) == 1, len(acc))
        finally:
            set_curated_decisions_path(
                os.path.join("data", "curated_decisions.jsonl"))


def test_accepted_decision_is_written_after_side_effects():
    """§四 的顺序断言: accepted **必须**在 pool/署名之后写。

    用一个记录调用顺序的探针来验 —— 这是"顺序"这件事唯一可靠的
    测法(读源码字符串会随注释变动而脆)。
    """
    print("\n[H3-D] accepted 决策写在副作用之后")
    with tmpdir() as d:
        cfg = _cfg(curated_min_size=100)
        pool_path = os.path.join(d, "curated.jsonl")
        attr_path = os.path.join(d, "ATTR.jsonl")
        cfg.attributions_path = attr_path
        pool = _FakePool(pool_path)
        comp = _FakeCompiler([("accept", "")])
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        order = []
        orig_record = led.record

        def spy_record(rec, *, decision, **kw):
            order.append(("ledger", decision))
            return orig_record(rec, decision=decision, **kw)

        lc = _mk(cfg, pool, comp, [mk_rec()], led, lambda: dict(FREE))
        lc.ledger.record = spy_record
        # 也让文件写入顺序可见
        import story.lazy_curator as LC
        orig_append = LC._append_jsonl

        def spy_append(path, rec_):
            tag = "attr" if path == attr_path else (
                "pool" if path == pool_path else "other")
            order.append((tag, rec_.get("void") and "void" or "row"))
            return orig_append(path, rec_)

        LC._append_jsonl = spy_append
        try:
            r = lc.step()
            check("accepted=1", r["accepted"] == 1, r)
        finally:
            LC._append_jsonl = orig_append
        kinds = [o[0] for o in order]
        check("**pool 在 accepted 之前**",
              kinds.index("pool") < kinds.index("ledger"), order)
        check("**attr 在 accepted 之前**",
              kinds.index("attr") < kinds.index("ledger"), order)
        check("accepted 是**最后**一条 ledger 记录",
              [o for o in order if o[0] == "ledger"][-1][1] == CL.ACCEPTED,
              order)
        check("没有墓碑(这次没失败)",
              not any(o[0] == "pool" and o[1] == "void" for o in order), order)


def test_only_accepted_requires_side_effects():
    """非 accepted 的三态**立刻**记 —— 它们不需要等副作用。"""
    print("\n[H3-D] 非 accepted 立刻记账")
    for kind, stage, want in (("reject", "ai_gate", CL.REJECTED),
                              ("defer", "compile_call", CL.TECHNICAL_DEFER)):
        with tmpdir() as d:
            cfg = _cfg(curated_min_size=100)
            pool = _FakePool(os.path.join(d, "p.jsonl"))
            comp = _FakeCompiler([(kind, stage)])
            led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
            lc = _mk(cfg, pool, comp, [mk_rec()], led, lambda: dict(FREE))
            lc.step()
            last = led.last(mk_rec(), CC.CURATED_POLICY_VERSION)
            check(f"{kind} -> 账本写 {want}",
                  last.get("decision") == want, last)


# ======================================================================
# 12. H3-D: CLI eligibility(§八)
# ======================================================================
def test_prewarm_probe_declares_offline_phase():
    """§八/§二: 预热的压力探针**必须**声明一个允许的 phase。

    ⚠️ 这个坑是实测踩到的: H3-D 给 `should_start` 加了 phase 白名单
    之后, 预热里那个 `lambda: {}` 探针(没有 `phase` 键)会被判成
    "不在允许的 phase" -> **整个预热一条都不审**, 报告里只留一句
    `stop_reason: phase=None 不允许后台审题`。

    最坏的地方在于它**看起来像跑完了**: `--show-samples` 照样打印
    (读的是池文件里上一轮的旧样本), 而 `llm_calls` 是 0。没有这条
    测试, 一次"预热成功"的报告可能对应零次调用。

    这里直接驱动 CLI 的 `main()`, 用假 client 断言它真的处理了候选。
    """
    print("\n[H3-D] 预热探针声明 phase(否则整轮空跑)")
    import inspect
    from tools import compile_curated as PRE
    src = inspect.getsource(PRE.main)
    check("**探针显式给了 phase**",
          '"phase"' in src or "'phase'" in src, "探针里没有 phase")
    check("**不是空 dict 探针**", "lambda: {}" not in src)

    # 端到端: 跑一次真的 main(), 断言它**真的处理了**候选。
    import json as _json
    from tests.test_curated_compile import FakeClient, _compile_tool
    from story.llm import LLMResult

    with tmpdir() as d:
        corpus = os.path.join(d, "corpus.jsonl")
        rec0 = mk_rec(eid="pse:q:1")
        with open(corpus, "w", encoding="utf-8") as f:
            f.write(_json.dumps({
                "external_id": rec0.external_id, "source": rec0.source,
                "source_url": rec0.source_url,
                "source_kind": rec0.source_kind,
                "question_author": "Q", "answer_author": "A",
                "question_license": "CC BY-SA 4.0",
                "answer_license": "CC BY-SA 4.0",
                "question_license_inference": "api",
                "answer_license_inference": "api",
                "title": rec0.title, "surface": rec0.surface,
                "bottom": rec0.bottom, "language": "en",
                "original_language": "en", "tags": [],
            }, ensure_ascii=False) + "\n")

        # 假 client: 编译 -> 复核 -> 审稿, 全按"合格"回。
        from tests.test_curated_compile import _review_pass
        fc = FakeClient([LLMResult(tool_input=_compile_tool(), model="m"),
                         _review_pass()])

        orig_make = PRE._make_writer

        def _fake_writer(cfg):
            from story.llm import PuzzleWriter
            return PuzzleWriter(client=fc, runtime_cfg=cfg)

        PRE._make_writer = _fake_writer
        try:
            rc = PRE.main([
                "--corpus", corpus,
                "--pool-out", os.path.join(d, "pool.jsonl"),
                "--attributions", os.path.join(d, "ATTR.jsonl"),
                "--decisions", os.path.join(d, "dec.jsonl"),
                "--target-stock", "3",
                "--max-candidates", "1",
            ])
        finally:
            PRE._make_writer = orig_make
        check("CLI 正常退出", rc == 0, rc)
        check("**真的调了 LLM**(不是空跑)", len(fc.calls) > 0, len(fc.calls))


def test_prewarm_stock_uses_live_policy_eligibility():
    """§八: 池里 10 条 v2 + 2 条 v3, 当前 policy=v3 -> stock 必须是 **2**。

    旧实现按**行数**数, 于是 bump 之后 CLI 会报 stock=10 而
    live 真正能播的只有 2 —— "预热跑完了, 直播一看库存还是空的"。
    """
    print("\n[H3-D] prewarm stock 按 live policy 语义数")
    import json as _json
    from tools.compile_curated import _FakePoolForPrewarm
    from tests.test_pool import _curated_spec
    from story.pool import set_curated_decisions_path

    with tmpdir() as d:
        p = os.path.join(d, "pool.jsonl")
        dpath = os.path.join(d, "dec.jsonl")
        set_curated_decisions_path(dpath)
        try:
            led = CL.DecisionLedger(dpath)
            rows = []
            # ⚠️ 刻意**不改** spec 的谜面: `_curated_spec()` 的 fair_clue
            # 逐字引用它, 改了字就会过不了 `validate_curated`(那正是
            # "fair_clue 必须逐字来自谜面"那条硬门在起作用), 于是这条
            # 测试会变成在测别的东西。区分新旧只用 policy + external_id。
            for i in range(10):
                s = _curated_spec()
                s.external_id = f"old:{i}"
                s.curated_policy_version = "curated-v2"
                rows.append({"pool_version": 1, "pool_key": f"k_old_{i}",
                             "added_at": 0, "spec": s.to_archive()})
            for i in range(2):
                s = _curated_spec()
                s.external_id = f"new:{i}"
                s.curated_policy_version = CC.CURATED_POLICY_VERSION
                rows.append({"pool_version": 1, "pool_key": f"k_new_{i}",
                             "added_at": 0, "spec": s.to_archive()})
                # H3-D3 §一-4: 账本判据是三元素 —— 必须用**这道 spec 自己**
                # 的内容登记, 否则 content_hash 对不上, 池门查不到它。
                from tests.test_pool import _mk_curated_dec_rec
                led.record(_mk_curated_dec_rec(s),
                           decision=CL.ACCEPTED,
                           policy_version=CC.CURATED_POLICY_VERSION)
            with open(p, "w", encoding="utf-8") as f:
                for row in rows:
                    f.write(_json.dumps(row, ensure_ascii=False) + "\n")

            pool = _FakePoolForPrewarm(p)
            check("**stock 是 2, 不是 12**", pool.stock_count() == 2,
                  pool.stock_count())
            check("**playable 也是 2**", pool.playable_count() == 2,
                  pool.playable_count())
        finally:
            set_curated_decisions_path(
                os.path.join("data", "curated_decisions.jsonl"))


# ======================================================================
# 13. H3-D: Director 装配层(§一 的"必须测装配层")
# ======================================================================
def test_scheduler_does_not_block_on_curator():
    """**§一 的装配层断言**: scheduler 调的是**真** `LazyCurator` 时,
    即使它的 worker 正卡在 LLM 里, 循环也照样推进。

    为什么必须在**装配层**测: `test_on_tick_returns_fast_while_worker_blocks`
    只证明 `on_tick` 自身快。但真正会退回"心跳停了"的写法是把
    `step()` 塞进 `_scheduler` —— 那是**装配层的错误**, 单测
    LazyCurator 永远发现不了。

    所以这里装一个**真的** `LazyCurator`(带真的 worker), 让它的
    compiler 卡住, 然后跑 scheduler 并量 tick 间隔。

    ## 为什么不能用"假的慢 curator"来测

    早先我写了一个 `on_tick` 故意 sleep 的假件, 然后断言 scheduler
    不被拖 —— 那**必然失败**, 而且失败得有道理: scheduler 是同步调用
    `on_tick` 的, 假件自己慢, 拖住它就是正确行为。

    真正的不变量是**契约**: `on_tick` 不许慢(它只做判断 + 提交)。
    所以这条测试要用**真件**来验这个契约在装配层成立。
    """
    print("\n[H3-D] 装配层: 真 curator 卡在 worker 里时 tick 照常推进")
    import threading as _th
    import time as _time
    from director import Director
    with tmpdir() as d:
        cfg = _cfg_for_director(d)
        dr = Director(cfg)
        cfg.curated_min_size = 100          # 强制"库存不足" -> 会想开工
        cfg.curated_background_enabled = True   # 见 `_cfg_for_director`

        release = _th.Event()
        entered = _th.Event()

        class _BlockingCompiler:
            def __init__(self):
                self.calls = 0

            def compile_one(self, rec, **kw):
                self.calls += 1
                entered.set()
                release.wait(30)             # 卡住, 直到测试放行
                return None, {"external_id": "x", "accepted": False,
                              "stage": "ai_gate", "reject_reasons": ["x"]}

        comp = _BlockingCompiler()
        lc = LazyCurator(cfg, _FakePool(os.path.join(d, "p.jsonl")), comp,
                         [mk_rec(eid=f"pse:q:{i}") for i in range(20)],
                         CL.DecisionLedger(os.path.join(d, "dec.jsonl")),
                         lambda: dict(FREE), clock=lambda: 0.0)
        dr._lazy_curator = lc
        # ⚠️ 必须先把引擎推进到 **QA**: §二 的 phase 白名单禁止在 IDLE /
        # SETTING 开工。不 start 的话 curator 一次都不会被启动, 这条
        # 测试就变成在测"什么都没发生"。
        #
        # 走 `dr.engine.submit_riddle` 的**真**路径(与 test_pool 的
        # `_inline_riddle` 同一个做法), 而不是自己拼一个 action ——
        # payload 的形状由引擎定义, 手工拼的那份会随它漂移。
        from tests.test_pool import _inline_riddle
        dr.engine.start()
        _inline_riddle(dr)

        ticks = {"n": 0, "gaps": []}
        real_push = dr.push

        def counting_push():
            ticks["n"] += 1
            if ticks["n"] >= 4:
                dr._stop.set()
            return real_push()

        dr.push = counting_push
        from story.ingest import ChatEvent
        for i in range(4):
            dr.inbox.put(ChatEvent(user_id=f"u{i}", user_name="u",
                                   content="hi", ts=0.0))
        t0 = _time.monotonic()
        try:
            dr._scheduler()
        finally:
            elapsed = _time.monotonic() - t0
            release.set()
            lc.wait_idle(10)
            lc.shutdown(wait=True)
        check("worker 真的开始跑了", entered.is_set(), comp.calls)
        check(f"**调度没有停摆**({ticks['n']} 拍 / {elapsed:.2f}s)",
              ticks["n"] >= 3, ticks["n"])
        # 若 on_tick 走了同步 step(), 这里会等于 worker 的阻塞时间。
        check(f"**总耗时远小于 worker 的卡住时间**(实际 {elapsed:.2f}s)",
              elapsed < 2.0, f"{elapsed:.2f}s / {ticks['n']} 拍")
        check("**只提交了一个 job**(single-flight)", comp.calls == 1,
              comp.calls)


def test_on_tick_is_non_blocking_by_contract():
    """`on_tick` 的**契约**断言: 真实实现在 worker 忙时立刻返回。

    这一条与 `test_on_tick_returns_fast_while_worker_blocks` 互补 ——
    那条测"跑起来之后", 这条测"还没跑起来时提交的那一下"。
    """
    print("\n[H3-D] on_tick 契约: 立刻返回")
    import time as _time
    with tmpdir() as d:
        cfg = _cfg(curated_min_size=100)
        pool = _FakePool(os.path.join(d, "p.jsonl"))
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))

        class _Blocking:
            def compile_one(self, rec, **kw):
                _time.sleep(3)
                return None, {"external_id": "x", "accepted": False,
                              "stage": "ai_gate", "reject_reasons": ["x"]}

        lc = _mk(cfg, pool, _Blocking(), [mk_rec()], led,
                 lambda: dict(FREE))
        try:
            t0 = _time.monotonic()
            r = lc.on_tick()
            dt = _time.monotonic() - t0
            check("提交成功", r["submitted"], r)
            check(f"**提交本身立刻返回**({dt*1000:.0f}ms)", dt < 0.3, dt)
        finally:
            lc.wait_idle(10)
            lc.shutdown(wait=True)


def test_scheduler_uses_on_tick_not_step():
    """scheduler **只能**调 `on_tick` —— 调 `step` 就是同步跑 LLM。

    这条是防回归的硬断言: 把 `step` 的名字改回调度路径会立刻变红。
    """
    print("\n[H3-D] 装配层: scheduler 调的是 on_tick")
    import threading as _th
    import inspect
    from director import Director
    src = inspect.getsource(Director._scheduler)
    check("**scheduler 里有 on_tick**", "on_tick" in src)
    check("**scheduler 里没有 lazy_curator.step**",
          "_lazy_curator.step" not in src, src[:200])

    with tmpdir() as d:
        cfg = _cfg_for_director(d)
        dr = Director(cfg)
        calls = {"tick": 0, "step": 0}

        class _Rec:
            def on_tick(self, **kw):
                calls["tick"] += 1
                return {"submitted": False, "reason": ""}

            def step(self, **kw):
                calls["step"] += 1
                return {}

            def status(self):
                return {"candidates": 0, "decided": 0, "accepted": 0,
                        "rejected": 0, "stock": 0, "playable": 0,
                        "target": 10, "min": 4, "refilling": False,
                        "tried": 0}

            def shutdown(self, **kw):
                pass

        dr._lazy_curator = _Rec()
        orig_wait = dr._stop.wait

        def _wait_once(timeout=None):
            dr._stop.set()
            return True

        dr._stop.wait = _wait_once
        try:
            dr._scheduler()
        finally:
            dr._stop.wait = orig_wait
        check("**每拍调 on_tick**", calls["tick"] >= 1, calls)
        check("**一次都没调 step**", calls["step"] == 0, calls)


def test_director_curator_off_scheduler_has_no_thread_leak():
    """`on_tick` 提交的 worker 是**有界**的(最多一个)。

    反复 on_tick 之后线程数不能无限涨 —— §一 明确"不要建无界线程"。
    """
    print("\n[H3-D] worker 线程有界")
    import threading as _th
    import time as _time
    with tmpdir() as d:
        cfg = _cfg(curated_min_size=100)
        pool = _FakePool(os.path.join(d, "p.jsonl"))
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))

        class _Slow:
            def __init__(self):
                self.entered = _th.Event()
                self.n = 0

            def compile_one(self, rec, **kw):
                self.n += 1
                self.entered.set()
                _time.sleep(0.4)
                return None, {"external_id": "x", "accepted": False,
                              "stage": "ai_gate", "reject_reasons": ["x"]}

        comp = _Slow()
        lc = _mk(cfg, pool, comp, [mk_rec(eid=f"pse:q:{i}") for i in range(30)],
                 led, lambda: dict(FREE))
        before = _th.active_count()
        try:
            lc.on_tick()
            comp.entered.wait(5)
            for _ in range(20):
                lc.on_tick()
            after = _th.active_count()
            check(f"**线程数没失控**(+{after - before})", after - before <= 2,
                  (before, after))
            check("**只跑了一个 job**(没有并发编译)", comp.n <= 2, comp.n)
        finally:
            lc.wait_idle(10)
            lc.shutdown(wait=True)


def _cfg_for_director(tmp):
    """装配层测试用的 Config: **所有路径都在临时目录**。

    复制 `test_pool.mkcfg` 的关键隔离项 —— 不隔离的话, 这些用例的
    结果会取决于本机 `data/` 下有没有真实文件(同一份代码两种结果)。
    `curated_background_enabled=False` 让 Director 建不出真 curator,
    装配层测试自己塞假的进去。
    """
    from story.config import Config
    return Config(
        sim_path="x", no_llm=True,
        pool_enabled=True,
        pool_path=os.path.join(tmp, "pool.jsonl"),
        pool_used_path=os.path.join(tmp, "used.jsonl"),
        curated_pool_path=os.path.join(tmp, "curated.jsonl"),
        curated_used_path=os.path.join(tmp, "curated_used.jsonl"),
        prefer_curated=False,
        #: Director 侧不建真 curator(装配层测试自己塞假的)。
        #: ⚠️ 但 `LazyCurator` 自己会读这个开关 —— 塞进去的那个实例
        #: 必须**再打开**它, 否则 `should_start` 会一直说"已关闭"。
        curated_background_enabled=False,
        out_path=os.path.join(tmp, "out.jsonl"),
        puzzle_out_path=os.path.join(tmp, "puzzle.jsonl"),
    )


# ======================================================================
# H3-D3: 交易缺口回归
# ======================================================================
def test_accepted_item_is_immediately_visible_in_live_pool():
    """§一-1: accepted 之后 **本场直播立刻看得见**, 不需要重启。

    这是 H3-D 漏掉的那一环: 落池 + 署名 + accepted 都在盘上成立了,
    但 `PuzzlePool._items` 是启动时的快照 -> `stock_count` 不变,
    `pop_next` 取不到。离线预热把这个洞掩盖了(跑完就退, 下次启动
    自然读得到), 直播里它就是"补了等于没补"。

    这条测试用**真的** `PuzzlePool`(不是替身) 驱动, 否则验的还是假的。
    """
    print("\n[H3-D3] accepted -> 本场内存池立即可见")
    from story.pool import PuzzlePool, set_curated_decisions_path
    from tests.test_pool import _curated_spec
    with tmpdir() as d:
        dpath = os.path.join(d, "dec.jsonl")
        ppath = os.path.join(d, "curated.jsonl")
        upath = ppath + ".used"
        set_curated_decisions_path(dpath)
        try:
            cfg = _cfg(curated_min_size=100)
            cfg.pool_path = ppath
            cfg.pool_used_path = upath
            cfg.curated_pool_path = ppath
            cfg.curated_used_path = upath
            cfg.pool_enabled = True
            cfg.curated_pool_enabled = True
            pool = PuzzlePool.open_curated(cfg)
            check("起始库存 0", pool.stock_count() == 0, pool.stock_count())

            spec = _curated_spec()
            led = CL.DecisionLedger(dpath)
            # 走**生产顺序**: 落池行 -> 署名 -> accepted -> 激活
            from tools.curated_ledger import content_hash_of
            from tests.test_pool import _mk_curated_dec_rec
            rec = _mk_curated_dec_rec(spec)
            spec.curated_content_hash = content_hash_of(rec)
            with open(ppath, "w", encoding="utf-8") as f:
                f.write(json.dumps(
                    {"pool_version": 1, "pool_key": "k1", "added_at": 0,
                     "spec": spec.to_archive()}, ensure_ascii=False) + "\n")
            led.record(rec, decision=CL.ACCEPTED,
                       policy_version=CC.CURATED_POLICY_VERSION)

            check("**没激活之前: 内存池仍然看不见**",
                  pool.stock_count() == 0, pool.stock_count())

            check("**activate_committed 返回 True**",
                  pool.activate_committed(spec) is True)
            check("**激活之后 stock 变成 1**", pool.stock_count() == 1,
                  pool.stock_count())
            got = pool.pop_next(recent_signatures=[], avoid=[])
            check("**pop_next 真的能取到**", got is not None,
                  "取不到 -> 那道题本场不可播")
        finally:
            set_curated_decisions_path(
                os.path.join("data", "curated_decisions.jsonl"))


def test_activate_committed_rejects_spec_that_fails_gate():
    """§一-1: 激活**必须**走准入门 —— 不许把不合格的题塞进内存池。

    这是"不要直接从 LazyCurator 改 `_items`"那条禁令的守卫。绕过门
    直接 append 会造出一道"播得出来但过不了门"的题。
    """
    print("\n[H3-D3] activate 拒绝过不了准入门的题")
    from story.pool import PuzzlePool, set_curated_decisions_path
    from tests.test_pool import _curated_spec
    with tmpdir() as d:
        set_curated_decisions_path(os.path.join(d, "dec.jsonl"))
        try:
            cfg = _cfg(curated_min_size=100)
            cfg.pool_path = os.path.join(d, "c.jsonl")
            cfg.pool_used_path = cfg.pool_path + ".used"
            cfg.curated_pool_path = cfg.pool_path
            cfg.curated_used_path = cfg.pool_used_path
            cfg.pool_enabled = True
            cfg.curated_pool_enabled = True
            pool = PuzzlePool.open_curated(cfg)
            bad = _curated_spec()
            bad.curated_policy_version = ""      # 明确过不了门
            check("**拒绝激活**", pool.activate_committed(bad) is False)
            check("内存池仍是空的", pool.stock_count() == 0,
                  pool.stock_count())
        finally:
            set_curated_decisions_path(
                os.path.join("data", "curated_decisions.jsonl"))


def test_single_flight_reserved_before_submit():
    """§一-6: `_busy` 必须在 submit **之前**占住。

    竞态窗口(早先的真实漏洞):

        tick #1: should_start()->True, submit(A); worker 还没起来
        tick #2: should_start()->True(它看到 _busy 仍是 False)
                 submit(B)
        -> 两个 job 排在同一个单线程池里, 各自选候选、各自打 LLM

    模拟手法: 把 `submit` 换成"只记录、不真跑", 于是 worker 永远不会
    把 `_busy` 置起来 —— 那正是竞态窗口里发生的事。第二次 `on_tick`
    必须被拒。
    """
    print("\n[H3-D3] single-flight: submit 前占位")
    with tmpdir() as d:
        cfg = _cfg(curated_min_size=100)
        pool = _FakePool(os.path.join(d, "p.jsonl"))
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        comp = _FakeCompiler([("accept", "")])
        lc = _mk(cfg, pool, comp, [mk_rec()], led, lambda: dict(FREE))

        class _DeadExec:
            """接受 submit 但**永不执行** —— 精确模拟"worker 还没起来"。"""

            def __init__(self):
                self.n = 0

            def submit(self, fn, *a, **kw):
                self.n += 1

            def shutdown(self, wait=False):
                pass

        lc._exec = _DeadExec()
        lc._worker_enabled = True
        r1 = lc.on_tick()
        check("第一次提交成功", r1["submitted"] is True, r1)
        r2 = lc.on_tick()
        check("**第二次被拒(占位已生效)**", r2["submitted"] is False, r2)
        check("原因点名在途", "在途" in r2["reason"], r2["reason"])
        check("**submit 只被调了一次**", lc._exec.n == 1, lc._exec.n)
        check("**没有真的编译**(worker 从未跑)",
              len(comp.calls) == 0, comp.calls)


# ======================================================================
# H3-D3: 交易缺口回归
# ======================================================================
def test_reviewer_technical_failure_is_defer_not_reject():
    """§一-3: 审稿**技术失败** -> technical_defer(可重试), 绝非 rejected。

    技术失败的定义很窄: timeout / 网关错 / 空 tool_input / schema 坏掉。
    它们的共同点是"**这次没审成**", 不是"这道题不行"。

    记成 rejected 的后果是**永久吃掉一道可能的好题**, 而且没有任何
    地方会显示丢了 —— 那正是 ledger 四种结论存在的主要理由。
    """
    print("\n[H3-D3] 审稿技术失败 -> defer(可重试)")
    for stage in ("review_technical", "compile_call", "truth_audit_technical"):
        with tmpdir() as d:
            cfg = _cfg(curated_min_size=100)
            pool = _FakePool(os.path.join(d, "p.jsonl"))
            comp = _FakeCompiler([("defer", stage)])
            led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
            lc = _mk(cfg, pool, comp, [mk_rec()], led, lambda: dict(FREE))
            r = lc.step()
            check(f"{stage} -> technical_defer", r["technical_defer"] == 1, r)
            check(f"{stage} **不是 rejected**", r["rejected"] == 0, r)
            check(f"{stage} 下次仍是候选",
                  select_candidate([mk_rec()], led,
                                   CC.CURATED_POLICY_VERSION) is not None)

    # 而**明确的**内容拒绝仍必须是 rejected(终态)。
    with tmpdir() as d:
        cfg = _cfg(curated_min_size=100)
        pool = _FakePool(os.path.join(d, "p.jsonl"))
        comp = _FakeCompiler([("reject", "ai_gate")])
        led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
        lc = _mk(cfg, pool, comp, [mk_rec()], led, lambda: dict(FREE))
        r = lc.step()
        check("内容拒绝 -> rejected", r["rejected"] == 1, r)
        check("内容拒绝 -> 下次不再是候选",
              select_candidate([mk_rec()], led,
                               CC.CURATED_POLICY_VERSION) is None)


def test_accepted_ledger_write_failure_is_retryable_single_active():
    """§一-5: 池/署名都成功、**accepted 决策写盘失败** 的故障注入。

    要求(逐条):
        本轮 accepted = 0    —— 没写下去的 accepted 不算数
        不可播               —— 池里那行没有 accepted 决策, 过不了准入门
        可重试               —— 记 defer, 下次仍是候选
        重试后只有 1 个逻辑 active item
        只有 1 份 active provenance
        只有 1 个 accepted transaction

    append-only 的历史里可以留孤儿行(不能删), 但它们必须**不可播**。

    ⚠️ 用 `_curated_spec()`(真的能过准入门的题)而不是 `_mk_spec` 那个
    最小 stub —— 后者连 `validate_spec` 都过不了, 那样这条测试会因为
    "题本身不合格"而变绿, 而我们想验的是**写盘顺序**。
    """
    print("\n[H3-D3] accepted 写盘失败 -> 可重试且不重复")
    from story.pool import PuzzlePool, set_curated_decisions_path
    from tests.test_pool import _curated_spec, _mk_curated_dec_rec
    with tmpdir() as d:
        cfg = _cfg(curated_min_size=100)
        pool_path = os.path.join(d, "curated.jsonl")
        cfg.attributions_path = os.path.join(d, "ATTR.jsonl")
        set_curated_decisions_path(os.path.join(d, "dec.jsonl"))
        try:
            pool = _FakePool(pool_path)
            led = CL.DecisionLedger(os.path.join(d, "dec.jsonl"))
            rec = _mk_curated_dec_rec(_curated_spec())

            class _Comp:
                """返回一道**真的**能过池准入门的 spec。"""

                def __init__(self):
                    self.calls = []

                def compile_one(self, r, **kw):
                    self.calls.append(getattr(r, "external_id", ""))
                    return _curated_spec(), {
                        "external_id": getattr(r, "external_id", ""),
                        "accepted": True, "stage": "accepted",
                        "style_tags": [], "reject_reasons": []}

            # 第一次: 让 accepted 那一行**写不下去**。
            real_record = led.record
            state = {"fail": True}

            def flaky(r, *, decision, policy_version, stage="", reasons=None,
                      style_tags=None, checks=None):
                if decision == CL.ACCEPTED and state["fail"]:
                    state["fail"] = False
                    return False             # 注入: 只失败这一次
                return real_record(r, decision=decision,
                                   policy_version=policy_version,
                                   stage=stage, reasons=reasons,
                                   style_tags=style_tags, checks=checks)

            led.record = flaky
            lc = _mk(cfg, pool, _Comp(), [rec], led, lambda: dict(FREE))
            r1 = lc.step()
            check("**本轮 accepted = 0**", r1["accepted"] == 0, r1)
            check("记成 technical_defer", r1["technical_defer"] == 1, r1)
            hist = _hist(led, rec, CC.CURATED_POLICY_VERSION)
            check("**历史里没有 accepted 行**",
                  all(h.get("decision") != CL.ACCEPTED for h in hist), hist)
            check("**下次仍是候选(可重试)**",
                  select_candidate([rec], led,
                                   CC.CURATED_POLICY_VERSION) is not None)

            def _live():
                rows = read_jsonl(pool_path) if os.path.exists(pool_path) else []
                voided = PuzzlePool._voided_keys(rows)
                out = []
                for i, row in enumerate(rows):
                    sp = PuzzlePool._spec_from_record(row, voided, i)
                    if sp is None:
                        continue
                    ok, _why = PuzzlePool._validate_pool_spec(sp)
                    if ok:
                        out.append(sp)
                return out

            check("**写 accepted 前: 盘上没有任何可播的题**",
                  _live() == [], _live())

            # 第二次(重试): accepted 这次能写下去。
            #
            # ⚠️ 必须**新建一个 curator** 再重试, 不能复用 `lc` ——
            # `_tried` 是**实例**状态, 同一次运行内 defer 过的题不再回头
            # (见 `step` 的说明: defer 的语义是"**下次**再试")。生产里的
            # "下次"就是下一次运行, 所以这里也照那个语义来。
            led.record = real_record
            lc2 = _mk(cfg, pool, _Comp(), [rec], led, lambda: dict(FREE))
            r2 = lc2.step()
            check("重试后 accepted = 1", r2["accepted"] == 1, r2)

            check("**重试后只有 1 个逻辑 active item**", len(_live()) == 1,
                  len(_live()))
            check("**只有 1 个 accepted transaction**",
                  sum(1 for h in led.rows
                      if h.get("decision") == CL.ACCEPTED) == 1,
                  [h.get("decision") for h in led.rows])
            # 署名只有一份 active。第一次署名**成功**了(失败的是 accepted),
            # 所以重试会再写一份 —— 重复的署名不是"错", 但它必须是
            # **同一份内容**(同名同源), 不能出现两个不同的 active 版本。
            attrs = [a for a in read_jsonl(cfg.attributions_path)
                     if a.get("external_id") == rec.external_id]
            check("**署名内容一致(没有两个不同的 active 版本)**",
                  len({json.dumps(a, sort_keys=True) for a in attrs}) == 1,
                  attrs)
        finally:
            set_curated_decisions_path(
                os.path.join("data", "curated_decisions.jsonl"))


# ======================================================================
def main():
    tests = [
        test_accepted_item_is_immediately_visible_in_live_pool,
        test_activate_committed_rejects_spec_that_fails_gate,
        test_single_flight_reserved_before_submit,
        test_reviewer_technical_failure_is_defer_not_reject,
        test_accepted_ledger_write_failure_is_retryable_single_active,
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
        # ---- H3-D ----
        test_forbidden_phases_never_start,
        test_setting_with_riddle_inflight_never_starts,
        test_qa_and_revealed_are_allowed,
        test_phase_accepts_enum_object_too,
        test_pressure_has_riddle_inflight_bool,
        test_refill_cycle_goes_all_the_way_to_target,
        test_playable_below_min_enters_refill_cycle,
        test_max_size_is_a_hard_cap_even_while_refilling,
        test_live_pressure_does_not_clear_refill_intent,
        test_on_tick_returns_fast_while_worker_blocks,
        test_on_tick_drops_submit_when_busy,
        test_worker_survives_compiler_exception,
        test_step_is_still_synchronous_for_cli,
        test_tried_is_shared_across_ticks,
        test_status_exposes_refilling,
        test_pool_write_failure_leaves_no_accepted_row_in_history,
        test_attribution_write_failure_leaves_no_accepted_and_no_live_item,
        test_retry_after_partial_failure_no_duplicates,
        test_accepted_decision_is_written_after_side_effects,
        test_only_accepted_requires_side_effects,
        test_prewarm_probe_declares_offline_phase,
        test_prewarm_stock_uses_live_policy_eligibility,
        # ---- H3-D 装配层 ----
        test_scheduler_does_not_block_on_curator,
        test_scheduler_uses_on_tick_not_step,
        test_director_curator_off_scheduler_has_no_thread_leak,
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
