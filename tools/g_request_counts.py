# -*- coding: utf-8 -*-
"""G 批次验收: **请求计数对照**(任务书 §"最关键的验收指标")。

任务书明确要求: "不要只报告测试全绿, 必须用**请求计数**证明效率真的
变了。" 这个脚本就是那个证明 —— 它跑三个合成场景, 打印每种链路的
LLM 调用次数。

    Candidate A: core_answer=81 + clue quote 轻微错误 + Reviewer 首次技术失败

    旧链可能: generator / generator / reviewer / generator ...
    新链应该: generator x1 / reviewer retry+fix / truth audit

    场景 B: Reveal 剩 20 秒  -> 不该启动 25s prefetch
    场景 C: prefetch 已发 generator, 返回前切 SETTING
            -> generator 返回 -> STOP -> reviewer=0 / audit=0 / next draft=0

用法: .venv/Scripts/python.exe tools/g_request_counts.py
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tests"))

import test_llm as T                                    # noqa: E402
from story.llm import LLMResult, PuzzleWriter            # noqa: E402

BAR = "=" * 66


def _count(fc, name):
    return sum(1 for c in fc.calls
               if (c.get("tool") or {}).get("name") == name)


def _report(title, fc):
    names = [(c.get("tool") or {}).get("name") for c in fc.calls]
    print("  " + title)
    print("    实际序列: " + repr(names))
    print("    generator 调用: %d" % _count(fc, "emit_riddle"))
    print("    reviewer  调用: %d" % _count(fc, "emit_review"))
    print("    audit     调用: %d" % _count(fc, "emit_truth_audit"))
    print("    hint_fix  调用: %d" % _count(fc, "emit_hint_fix"))
    print("    **总请求数: %d**" % len(fc.calls))
    return _count(fc, "emit_riddle")


def _obs(spec):
    """G4 可观测性: repair 救回 / 硬拒 的对照。"""
    m = spec.metrics or {}
    rep = m.get("candidate_repair_count", 0)
    hard = m.get("hard_reject_before_review_count", 0)
    print("    **repair 救回: %d / 硬拒(审稿前): %d**" % (rep, hard))
    rr = m.get("repair_reasons") or {}
    if rr:
        print("    按原因: " + ", ".join("%s=%d" % kv
                                        for kv in sorted(rr.items())))
    return rep, hard


def scenario_a():
    """核心场景: 81 字 core + clue quote 错 + Reviewer 首次技术失败。"""
    print("")
    print(BAR)
    print("场景 A: core_answer=81 + clue quote 轻微错误 + Reviewer 首次技术失败")
    print(BAR)
    P = T._GOOD_PUZ
    bad_clues = [{"quote": "灯只在退潮的时候亮着", "supports_atoms": ["a1"]}]
    fc = T.FakeClient([
        # (1) 生成: core 81 字 + quote 与谜面差几个字
        LLMResult(tool_input=T.riddle(core_answer="他" * 81,
                                      fair_clues=bad_clues), model="m"),
        # (2) Reviewer 第一次: 输出触顶(技术失败)
        LLMResult(tool_input={},
                  error="输出触顶(max_tokens=3500, 实出 3499), "
                        "工具调用没写完; 需要调大 max_tokens", model="m"),
        # (3) Reviewer 重试(**同一稿**): 修好 core + 重摘 quote
        LLMResult(tool_input=T.review_fix(P, core_answer="一句话核心答案。"),
                  model="m"),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    spec = w.gen_spec(blueprint=fc.default_blueprint)
    gens = _report("新链(本批次之后)", fc)
    print("    最终 core_answer 字数: %d" % len(spec.core_answer or ""))
    quotes_ok = all((c.quote or "") in (spec.puzzle or "")
                    for c in (spec.fair_clues or []))
    print("    最终 quote 都在谜面里: %s" % quotes_ok)
    print("    出题成功: %s" % bool(spec.puzzle))
    _obs(spec)
    print("")
    print("  >>> **generator 请求数 = %d**(任务书要求: 仍然只有 1 次)" % gens)
    return gens


def _mkpf(cfg, pool, writer, ex, probe):
    from story.prefetch import PoolPrefetcher
    return PoolPrefetcher(
        cfg=cfg, pool=pool, writer=writer, probe=probe,
        probe_inputs=lambda: {"avoid": [], "recent_signatures": []},
        pick_blueprint=lambda recent, rng=None: None, executor=ex)


class _StubWriter:
    """最小 writer: 生成一道能入池的题; 让路时返回 interrupted spec。"""

    def __init__(self):
        self.calls = 0
        self.predicate_seen = False

    def gen_spec(self, should_continue=None, **kw):
        from story.puzzle import PuzzleSpec
        self.calls += 1
        self.predicate_seen = should_continue is not None
        if should_continue is not None and not should_continue():
            s = PuzzleSpec(puzzle="")
            s.error = ""
            s.metrics = {"interrupted": True, "ok": False}
            return s
        return PuzzleSpec(puzzle="gen-%d?" % self.calls, answer="a")


class _CountEx:
    def __init__(self, before=None):
        self.n = 0
        self._before = before

    def submit(self, fn, *a, **kw):
        from concurrent.futures import Future
        f = Future()
        if self._before:
            self._before()
        try:
            f.set_result(fn(*a, **kw))
        except BaseException as e:              # noqa: BLE001
            f.set_exception(e)
        self.n += 1
        return f

    def shutdown(self, **kw):
        pass


def _mkcfg(d, **kw):
    from story.config import Config
    kw.setdefault("pool_min_size", 2)
    kw.setdefault("pool_target_size", 5)
    return Config(sim_path="x", no_llm=True,
                  pool_path=os.path.join(d, "pool.jsonl"),
                  pool_used_path=os.path.join(d, "used.jsonl"), **kw)


def scenario_prefetch_guard():
    """Reveal 剩 20 秒 -> 不该启动一轮 25s prefetch。"""
    print("")
    print(BAR)
    print("场景 B: Reveal 只剩 20 秒 -> 不启动 25s prefetch")
    print(BAR)
    import tempfile
    from story.pool import PuzzlePool
    from story.state import Phase

    d = tempfile.mkdtemp()
    cfg = _mkcfg(d, pool_prefetch_budget_seconds=25.0,
                 pool_prefetch_guard_margin_seconds=5.0,
                 pool_reveal_start_guard_seconds=30.0)
    # 池子**真的缺货**才会走到 guard 那一关 —— 库存够时 latch 根本不打开,
    # 提交数自然是 0, 那条断言就是假的(测的是夹具不是实现)。
    results = []
    for left, label in ((20.0, "剩 20s(<= effective guard 30s)"),
                        (45.0, "剩 45s(> effective guard 30s)")):
        pool = PuzzlePool.open(cfg)
        ex, wr = _CountEx(), _StubWriter()
        pf = _mkpf(cfg, pool, wr, ex, probe=lambda left=left: {
            "phase": Phase.REVEALED, "pending": 0, "inflight": 0,
            "hint_inflight": False, "reveal_inflight": False,
            "reveal_remaining_seconds": left, "puzzle_index": 3,
            "stopped": False})
        pf.on_tick()
        results.append((left, ex.n, pf._refill_active))
        print("    %s: 提交数 = %d, latch = %s, effective_guard = %.0fs"
              % (label, ex.n, pf._refill_active, pf._effective_guard_s))
    ok = results[0][1] == 0 and results[1][1] == 1
    print("")
    print("  >>> 剩 20s 提交 0 次 / 剩 45s 提交 1 次 -> %s"
          % ("通过" if ok else "**不通过**"))
    return ok


def scenario_prefetch_yield():
    """prefetch 已发 generator -> 返回前切 SETTING -> 全部停手。"""
    print("")
    print(BAR)
    print("场景 C: prefetch 已发 generator, 返回前切 SETTING")
    print(BAR)
    import tempfile
    from story.pool import PuzzlePool
    from story.state import Phase

    d = tempfile.mkdtemp()
    cfg = _mkcfg(d)
    state = {"busy": False}

    def probe():
        return {"phase": Phase.SETTING if state["busy"] else Phase.QA,
                "pending": 0, "inflight": 0, "hint_inflight": False,
                "reveal_inflight": False, "reveal_remaining_seconds": None,
                "puzzle_index": 4, "stopped": False}

    pool = PuzzlePool.open(cfg)
    wr = _StubWriter()
    # 提交那一刻把相位切忙: 精确复现"请求已发出, 返回时直播已开始"
    ex = _CountEx(before=lambda: state.__setitem__("busy", True))
    pf = _mkpf(cfg, pool, wr, ex, probe=probe)
    pf.on_tick()      # QA 空闲 -> 真的提交出去
    pf.on_tick()      # 应用结果
    st = pf.stats()
    print("    gen_spec 收到取消谓词: %s" % wr.predicate_seen)
    print("    gen_spec 调用次数: %d(应为 1 —— 已发出的无法取消)" % wr.calls)
    print("    interrupted 计数: %d" % st["interrupted"])
    print("    generation_fail 计数: %d(让路**不算失败**)" % st["generation_fail"])
    print("    退避: %s(让路不设退避)" % st["backoff_until"])
    ok = (wr.predicate_seen and wr.calls == 1
          and st["interrupted"] == 1 and st["generation_fail"] == 0
          and st["backoff_until"] == 0.0)
    print("")
    print("  >>> 谓词已传 / gen_spec=1 / interrupted=1 / fail=0 / 退避=0"
          " -> %s" % ("通过" if ok else "**不通过**"))
    return ok



def scenario_d_fact_enum():
    """**G4-A**: fact kind=public / visibility=hidden -> repair 而不是换稿。

    这是 G2 的真实漏口: 只有**完整互换**才会被自动纠正, 半错位落到
    r.fail -> 整稿扔掉 -> 重新生成。实播日志里那三条
    `fact f7 kind 非法: public` 走的正是这条路。
    """
    print("")
    print(BAR)
    print("场景 D: fact kind=public / visibility=hidden")
    print(BAR)
    P = T._GOOD_PUZ
    bad = T.riddle()
    bad["facts"][2] = dict(bad["facts"][2])
    bad["facts"][2]["kind"] = "public"
    bad["facts"][2]["visibility"] = "hidden"
    fixed = T.review_fix(P)
    fixed["facts"] = [dict(f) for f in fixed["facts"]]
    fixed["facts"][2]["kind"] = "support"
    fixed["facts"][2]["visibility"] = "public"
    fc = T.FakeClient([
        LLMResult(tool_input=bad, model="m"),
        LLMResult(tool_input=fixed, model="m"),
        T._truth_tool(truthful=True, consistent=True),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    spec = w.gen_spec(blueprint=fc.default_blueprint)
    gens = _report("修复同一稿", fc)
    _obs(spec)
    kinds = [(f.id, f.kind) for f in (spec.facts or [])]
    print("    最终 fact kind: %s" % kinds)
    print("    出题成功: %s" % bool(spec.puzzle))
    ok = gens == 1 and bool(spec.puzzle)
    print("")
    print("  >>> **generator = %d(应为 1: 修 enum 不换稿)** -> %s"
          % (gens, "通过" if ok else "**不通过**"))
    return ok


def scenario_e_prefill():
    """**G4-B**: prefill 必须真的看得见池内 signature。

    旧实现直接摸 `pool._items` 并 `isinstance(rec, dict)` —— 而里面装的
    是 `PuzzleSpec` **对象**, 于是永远返回 []。预热会连补同 pair 的题
    并打印"达标", 真开播时库存立刻塌。
    """
    print("")
    print(BAR)
    print("场景 E: prefill 看得见池内 signature(真实 PuzzlePool)")
    print(BAR)
    import random
    import tempfile
    import prefill_pool as PF
    from story.pool import PuzzlePool
    from story.quality import Quotas, choose_blueprint

    d = tempfile.mkdtemp()
    cfg = _mkcfg(d)
    sys.path.insert(0, os.path.join(_ROOT, "tests"))
    import test_prefetch as TP                        # noqa: E402
    pool = PuzzlePool.open(_mkcfg(tempfile.mkdtemp()))
    pool.add(TP.variant(100))
    sigs = PF._recent_sigs(pool, cfg)
    print("    池内 1 道 -> prefill recent = %d 条" % len(sigs))
    # 调度器必须避开已有的 exact pair
    if sigs:
        blocked = (sigs[0].mechanism_family, sigs[0].solution_shape)
    else:
        blocked = None
    hits = 0
    for seed in range(60):
        bp = choose_blueprint(sigs, rng=random.Random(seed), quotas=Quotas())
        if blocked and (bp.mechanism_family, bp.solution_shape) == blocked:
            hits += 1
    print("    **60 个种子选中已有 pair 的次数: %d(应为 0)**" % hits)
    ok = len(sigs) == 1 and hits == 0
    print("")
    print("  >>> recent 非空 且 不自我重复 -> %s"
          % ("通过" if ok else "**不通过**"))
    return ok

def main() -> int:
    print(BAR)
    print("Batch G 验收 —— 请求计数对照")
    print(BAR)
    gens = scenario_a()
    ok_b = scenario_prefetch_guard()
    ok_c = scenario_prefetch_yield()
    ok_d = scenario_d_fact_enum()
    ok_e = scenario_e_prefill()
    print("")
    print(BAR)
    ok = (gens == 1) and ok_b and ok_c and ok_d and ok_e
    print("结论: generator 请求数 = %d(要求 1); B %s; C %s; D %s; E %s"
          % (gens, "通过" if ok_b else "不通过",
             "通过" if ok_c else "不通过",
             "通过" if ok_d else "不通过",
             "通过" if ok_e else "不通过"))
    print("总体: %s" % ("**全部通过**" if ok else "**有不通过项**"))
    print(BAR)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
