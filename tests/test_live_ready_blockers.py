"""Offline regression checks for PR #61 review 5325833407."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from director import Director
from story.config import Config
from story.engine import RoundEngine
from story.pool import PuzzlePool
from story.prefetch import PoolPrefetcher
from story.puzzle import PuzzleFact
from story.state import ActionKind, Phase
from test_engine import _answer_and_submit, boot_v5
from test_interaction import boot_revealed
from tools.live_ready_inventory import build, report


def test_fact_progress():
    facts = [
        PuzzleFact(id="f1", text="HIDDEN CANONICAL ONE", public_text="公开线索一", kind="core"),
        PuzzleFact(id="f2", text="HIDDEN CANONICAL TWO", public_text="公开线索二", kind="core"),
        PuzzleFact(id="f3", text="普通支持事实", public_text="支持线索", kind="support"),
    ]
    eng, clk, sp = boot_v5(facts=facts)
    snap = eng.snapshot().to_json()
    assert snap["fact_progress"] == {"established": 0, "total": 2, "facts": []}
    _answer_and_submit(eng, clk, "a", "甲", "碰到核心吗", verdict="是",
                       touched_fact_ids=["f1"], established_fact_ids=[])
    assert eng.snapshot().fact_progress["established"] == 0
    _answer_and_submit(eng, clk, "b", "乙", "支持事实吗", verdict="是",
                       established_fact_ids=["f3"])
    assert eng.snapshot().fact_progress["established"] == 0
    _answer_and_submit(eng, clk, "c", "丙", "第一条吗", verdict="是",
                       established_fact_ids=["f1"], completion_verified_fact_ids=[])
    assert eng.snapshot().fact_progress["established"] == 0
    _answer_and_submit(eng, clk, "d", "丁", "第一条不是吗", verdict="不是",
                       established_fact_ids=["f1"])
    assert eng.snapshot().fact_progress["established"] == 0
    _answer_and_submit(eng, clk, "e", "戊", "第一条是吗", verdict="是",
                       established_fact_ids=["f1"])
    snap = eng.snapshot().to_json()
    assert snap["fact_progress"] == {"established": 1, "total": 2,
                                      "facts": [{"text": "公开线索一"}]}
    assert "HIDDEN CANONICAL" not in json.dumps(snap, ensure_ascii=False)
    assert "established_fact_ids" not in snap and "completion_fact_ids" not in snap
    _answer_and_submit(eng, clk, "f", "己", "第二条是吗", verdict="是",
                       established_fact_ids=["f2"])
    assert eng.phase == Phase.REVEALING and eng.snapshot().fact_progress == {}
    eng.submit_reveal("已揭晓")
    clk.advance(6)
    eng.tick()
    assert eng.phase == Phase.SETTING and eng.snapshot().fact_progress == {}
    eng.submit_riddle(sp.puzzle, sp.answer, list(sp.hints), spec=sp)
    assert eng.phase == Phase.QA
    assert eng.snapshot().fact_progress == {"established": 0, "total": 2, "facts": []}

    empty = [PuzzleFact(id="f1", text="DO NOT LEAK", public_text="", kind="core"), facts[1]]
    eng2, clk2, _ = boot_v5(facts=empty)
    _answer_and_submit(eng2, clk2, "g", "庚", "第一条", verdict="是",
                       established_fact_ids=["f1"])
    p = eng2.snapshot().fact_progress
    assert p == {"established": 1, "total": 2, "facts": []}
    assert "DO NOT LEAK" not in json.dumps(eng2.snapshot().to_json())


def test_theme_demand_end_to_end():
    class FakePrefetcher:
        def __init__(self):
            self.categories = []

        def request_category(self, category):
            self.categories.append(category)

    eng, clk = boot_revealed()
    director = object.__new__(Director)
    director._prefetcher = FakePrefetcher()
    def dispatch(actions):
        for action in actions:
            if action.kind == ActionKind.THEME_DEMAND:
                director._run_action(action)
    dispatch(eng.submit_danmaku("u1", "甲", "#c"))
    assert director._prefetcher.categories == ["horror"]
    dispatch(eng.submit_danmaku("u2", "乙", "#c"))
    assert director._prefetcher.categories == ["horror"]
    dispatch(eng.submit_danmaku("u1", "甲", "#a"))
    assert director._prefetcher.categories == ["horror", "logic"]
    assert eng._theme_ledger.totals()["horror"] == 1
    assert eng._theme_ledger.totals()["logic"] == 1
    clk.advance(61)
    eng.tick()
    assert not any(a.kind == ActionKind.THEME_DEMAND for a in
                   eng.submit_danmaku("u3", "丙", "#c"))
    assert eng._probe()["pending"] == 0
    for phase in (Phase.QA, Phase.SETTING, Phase.REVEALING):
        eng.phase = phase
        assert not any(a.kind in (ActionKind.THEME_DEMAND, ActionKind.ANSWER)
                       for a in eng.submit_danmaku("u4", "丁", "#c"))


def test_production_executor_and_writer():
    from test_prefetch import variant
    for concurrency in (2, 1):
        with tempfile.TemporaryDirectory() as d:
            cfg = Config(pool_path=str(Path(d) / "pool.jsonl"),
                         pool_used_path=str(Path(d) / "used.jsonl"),
                         pool_prefetch_concurrency=concurrency,
                         pool_keyword_seed_enabled=False,
                         pool_min_size=2, pool_target_size=5,
                         pool_max_size=10)
            pool = PuzzlePool.open(cfg)
            assert pool.add(variant(902))
            pf = PoolPrefetcher(cfg, pool, object(),
                                lambda: {}, lambda *_: None,
                                writer_factory=lambda _: object())
            assert pf._executor._max_workers == concurrency
            barrier = threading.Barrier(concurrency)
            active = 0
            maximum = 0
            lock = threading.Lock()
            def worker(*_):
                nonlocal active, maximum
                with lock:
                    active += 1
                    maximum = max(maximum, active)
                try:
                    barrier.wait(timeout=3)
                finally:
                    with lock:
                        active -= 1
                return ("interrupted", "test worker completed", {})
            pf._generate_one = worker
            pf.activate_background()
            for _ in range(concurrency):
                pf.on_tick()
            futures = [f for f in pf._futures if f is not None]
            assert len(futures) == concurrency
            for future in futures:
                future.result(timeout=5)
            assert maximum == concurrency
            assert len({id(w) for w in pf._writers}) == concurrency
            pf._executor.shutdown(wait=True)
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(pool_path=str(Path(d) / "pool.jsonl"),
                     pool_used_path=str(Path(d) / "used.jsonl"),
                     pool_prefetch_concurrency=2)
        try:
            PoolPrefetcher(cfg, PuzzlePool.open(cfg), object(), lambda: {},
                           lambda *_: None, writer_factory=lambda _: None)
        except ValueError:
            pass
        else:
            raise AssertionError("writer factory failure must fail closed")
        shared = object()
        try:
            PoolPrefetcher(cfg, PuzzlePool.open(cfg), shared, lambda: {},
                           lambda *_: None, writer_factory=lambda _: shared)
        except ValueError:
            pass
        else:
            raise AssertionError("shared writer factory must fail closed")


def test_audit_fixture():
    from test_prefetch import variant
    with tempfile.TemporaryDirectory() as d:
        pool_path, used_path = Path(d) / "pool.jsonl", Path(d) / "used.jsonl"
        cfg = Config(pool_path=str(pool_path), pool_used_path=str(used_path))
        pool = PuzzlePool.open(cfg)
        assert pool.add(variant(901))
        data = build(pool_path, used_path)
        assert data["pool_file_sha256"] == __import__("hashlib").sha256(pool_path.read_bytes()).hexdigest()
        assert data["used_ledger_sha256"] is None
        assert data["observed"]["distinct_stock_count"] == 1
        markdown = report(data)
        assert f"| distinct current-policy 未播库存 | ≥ 50 | {data['observed']['distinct_stock_count']} |" in markdown
        assert " / ".join(f"{c} {n}" for c, n in
                           data["observed"]["stock_by_category"].items()) in markdown
        assert all(k in data["inventory"][0] for k in
                   ("prompt_version", "protocol_version", "quality_policy_version",
                    "categories", "primary_category"))
        used_path.write_text("", encoding="utf-8")
        data2 = build(pool_path, used_path)
        assert data2["used_ledger_sha256"] == __import__("hashlib").sha256(b"").hexdigest()


def test_public_stock_specs():
    from test_pool import _write_raw_pool, good_spec, mkcfg
    with tempfile.TemporaryDirectory() as d:
        current = good_spec()
        old = good_spec(id="old-policy")
        old.quality_policy_version = "quality-v2"
        _write_raw_pool(d, [current, old])
        cfg = mkcfg(d)
        pool = PuzzlePool.open(cfg)
        snapshot = pool.stock_specs()
        assert len(snapshot) == pool.stock_count() == 1
        assert snapshot[0].puzzle == current.puzzle
        snapshot[0].puzzle = "MUTATED"
        snapshot[0].facts[0].text = "MUTATED NESTED"
        assert pool.stock_specs()[0].puzzle == current.puzzle
        assert pool.stock_specs()[0].facts[0].text == current.facts[0].text
        assert pool.stock_count() == 1
        delivered = pool.pop_next(recent_signatures=[])
        assert delivered is not None
        assert pool.stock_specs() == [] and pool.stock_count() == 0
        Path(cfg.pool_used_path).write_text("{broken\n", encoding="utf-8")
        assert PuzzlePool.open(cfg).stock_specs() == []


def test_runtime_manifest_if_present():
    pool_path = ROOT / "data/pool.jsonl"
    if not pool_path.exists():  # CI checkout keeps the runtime pool gitignored.
        return
    used_path = ROOT / "data/pool_used.jsonl"
    data = json.loads((ROOT / "data/audit/live_ready_theme_v1/inventory.json").read_text(encoding="utf-8"))
    digest = __import__("hashlib").sha256
    assert data["pool_file_sha256"] == digest(pool_path.read_bytes()).hexdigest()
    assert data["used_ledger_sha256"] == (digest(used_path.read_bytes()).hexdigest()
                                              if used_path.exists() else None)
    assert data["criteria_met"]
    assert all(k in e and e[k] for e in data["inventory"] if e["eligible_v2"]
               for k in ("prompt_version", "protocol_version",
                         "quality_policy_version", "categories", "primary_category"))
    markdown = (ROOT / "data/audit/live_ready_theme_v1/report.md").read_text(encoding="utf-8")
    assert f"| distinct current-policy 未播库存 | ≥ 50 | {data['observed']['distinct_stock_count']} |" in markdown
    assert " / ".join(f"{c} {n}" for c, n in
                           data["observed"]["stock_by_category"].items()) in markdown


if __name__ == "__main__":
    for test in (test_fact_progress, test_theme_demand_end_to_end,
                 test_production_executor_and_writer, test_audit_fixture,
                 test_public_stock_specs,
                 test_runtime_manifest_if_present):
        test()
        print("PASS", test.__name__)
