#!/usr/bin/env python
# coding: utf-8
"""运行: uv run tests/test_prefetch.py（完全离线, 无网络）。

Q9 后台补池(prefetch)。核心约束:

    Q9 只能往 Q8 已经定义好的 Pool 边界里**生产库存**; 绝不能改变
    pop_next()、used ledger、fail-closed 或交付事务语义。

本文件分两部分:
  A. 库存/探针的零件(stock_count / pressure / generation inputs / 配置)
  B. PoolPrefetcher 状态机(滞回 latch / 单飞 / 退避 / 丢弃)
"""
from __future__ import annotations

from concurrent.futures import Future

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from story.config import Config  # noqa: E402
from story.engine import RoundEngine  # noqa: E402
from story.puzzle import (  # noqa: E402
    FairClue, PuzzleBlueprint, PuzzleFact, PuzzleSignature, PuzzleSpec,
    SolveAtom,
)
from story.pool import PuzzlePool, spec_key  # noqa: E402
from story.state import Phase  # noqa: E402

FAIL = [0]


def check(name, cond, extra=""):
    if cond:
        print(f"  ok  {name}")
        return
    print(f"  FAIL {name}  {extra}")
    FAIL[0] += 1


def good_spec(puzzle=None, answer=None, **kw) -> PuzzleSpec:
    """一个结构上完全合格的 spec。"""
    pz = puzzle or ("灯塔守塔人每晚都亮灯, 但只在退潮的那几个小时亮。涨潮后他"
                    "反而把灯熄掉, 哪怕有船经过也一样, 为此被投诉过好几次。"
                    "为什么?")
    s = PuzzleSpec(
        id="", title="灯塔",
        puzzle=pz,
        answer=answer or "退潮时礁石露出水面, 亮灯是为标出礁石位置; "
                         "涨潮后继续亮反而误导船只。",
        facts=[
            PuzzleFact(id="f1", text="退潮时危险礁石会露出或接近水面", kind="core"),
            PuzzleFact(id="f2", text="灯的真正作用是标示危险礁石的位置", kind="core"),
            PuzzleFact(id="f3", text="涨潮后礁石被淹没, 亮灯反而会误导船只",
                       kind="support", hintable=False),
            PuzzleFact(id="f4", text="他的行为不是为了纪念死者", kind="exclusion",
                       hintable=False),
        ],
        solve_atoms=[
            SolveAtom(id="a1", role="cause",
                      text="退潮使危险礁石成为需要标出的目标", fact_ids=["f1"]),
            SolveAtom(id="a2", role="mechanism",
                      text="灯是在标记礁石, 而不是给船引路",
                      fact_ids=["f2", "f3"]),
        ],
        fair_clues=[
            FairClue(quote="只在退潮的那几个小时亮", supports_atoms=["a1"]),
            FairClue(quote="涨潮后他反而把灯熄掉", supports_atoms=["a2"]),
        ],
        hints=["注意灯的开关时机", "想想潮水的变化", "灯是在给谁传递信息?"],
        blueprint=PuzzleBlueprint(
            mechanism_family="hidden_function",
            solution_shape="hidden_function_explains_behavior",
            domain="maritime", relation="stranger",
            emotion_mode="neutral", time_shape="habitual"),
        signature=PuzzleSignature(
            mechanism_family="hidden_function",
            solution_shape="hidden_function_explains_behavior",
            domain="maritime", emotion_mode="neutral",
            relation="stranger", time_shape="habitual"),
        prompt_version="riddle-v3",
        quality_policy_version="quality-v3",
        metrics={"generation_attempts": 2, "review_calls": 1,
                 "rewrite_count": 0, "review_decision": "pass",
                 "review_latency_ms_total": 3310,
                 "generation_latency_ms": 12400, "ok": True},
        blueprint_specified=True,
    )
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def variant(i: int) -> PuzzleSpec:
    """第 i 道**内部自洽**的合格题。

    不能只改 puzzle/answer —— facts/clues 的 quote 必须与谜面对得上,
    否则 validate_spec 会正确地把它拒掉。所以整组文本一起换。
    """
    return good_spec(
        puzzle=f"编号{i}的守夜人每晚都把钟敲{i + 1}下，白天一声不响。为什么？",
        answer=f"编号{i}的钟声是在给{i + 1}海里外的船报暗礁。",
        title=f"钟{i}",
        facts=[
            PuzzleFact(id="f1", text=f"编号{i}的暗礁只在夜里需要标示", kind="core"),
            PuzzleFact(id="f2", text="钟声的真正用途是报暗礁位置", kind="core"),
            PuzzleFact(id="f3", text="白天能看见暗礁，不需要声音",
                       kind="support", hintable=False),
            PuzzleFact(id="f4", text="他敲钟不是为了报时", kind="exclusion",
                       hintable=False),
        ],
        solve_atoms=[
            SolveAtom(id="a1", role="cause",
                      text="夜里看不见暗礁，需要声音标示", fact_ids=["f1"]),
            SolveAtom(id="a2", role="mechanism",
                      text="钟声是在报暗礁，而不是报时", fact_ids=["f2", "f3"]),
        ],
        fair_clues=[
            FairClue(quote=f"每晚都把钟敲{i + 1}下", supports_atoms=["a1"]),
            FairClue(quote="白天一声不响", supports_atoms=["a2"]),
        ],
    )


def mkcfg(tmp, **kw):
    kw.setdefault("pool_enabled", True)
    kw.setdefault("pool_path", os.path.join(tmp, "pool.jsonl"))
    kw.setdefault("pool_used_path", os.path.join(tmp, "used.jsonl"))
    kw.setdefault("no_llm", True)
    return Config(sim_path="x", **kw)


class tmpdir:
    def __enter__(self):
        self._d = tempfile.TemporaryDirectory()
        return self._d.name

    def __exit__(self, *a):
        # Director 的后台线程在 Windows 上可能还攥着临时目录里的句柄,
        # 删目录时报 PermissionError。那是清理期噪音, 断言都已经跑完了。
        try:
            self._d.cleanup()
        except (PermissionError, OSError):
            pass


def _write_raw(path, lines):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for ln in lines:
            f.write(ln + "\n")


# ======================================================================
# A. 库存与探针
# ======================================================================
def test_stock_count_excludes_used():
    print("\n[A1] stock_count 排掉 used")
    with tmpdir() as d:
        pool = PuzzlePool.open(mkcfg(d))
        for i in range(3):
            check(f"add{i}", pool.add(variant(i)) is True)
        check("stock=3", pool.stock_count() == 3, pool.stock_count())
        got = pool.pop_next(recent_signatures=[])
        check("pop 成功", got is not None)
        check("stock=2", pool.stock_count() == 2, pool.stock_count())
        check("pending=2", pool.pending_count() == 2, pool.pending_count())


def test_stock_count_excludes_invalid_spec():
    """**stock_count 不是 pending_count 的别名。**

    磁盘改坏的题(过不了准入门的)仍被 pending_count 算进去, 但补池
    必须看见它"其实播不出来", 否则会以为池子满了、一道都不补。
    """
    print("\n[A2] stock_count 排掉磁盘改坏的题")
    with tmpdir() as d:
        cfg = mkcfg(d)
        pool = PuzzlePool.open(cfg)
        check("好题入池", pool.add(good_spec()) is True)
        recs = [json.loads(l) for l in
                open(cfg.pool_path, encoding="utf-8") if l.strip()]
        recs[0]["spec"]["signature"]["domain"] = "乱写的值"
        _write_raw(cfg.pool_path,
                   [json.dumps(r, ensure_ascii=False) for r in recs])
        p2 = PuzzlePool.open(cfg)
        check("pending_count 仍算它(1)", p2.pending_count() == 1,
              p2.pending_count())
        check("**stock_count 不算它(0)**", p2.stock_count() == 0,
              p2.stock_count())


def test_stock_count_ignores_recent_window():
    """被当前窗口挡住的题**仍是库存** —— 那是"此刻能不能播", 不是
    "库存有没有"。等最近 N 题滚过去它就能用。"""
    print("\n[A3] stock_count 不扣 dynamic gate")
    with tmpdir() as d:
        cfg = mkcfg(d)
        PuzzlePool.open(cfg).add(good_spec())
        loaded = PuzzlePool.open(cfg)
        sig = loaded._items[0].signature.to_dict()
        wall = [sig] * 10
        check("stock 仍数是 1", loaded.stock_count() == 1, loaded.stock_count())
        check("**但 pop 被窗口挡住 -> None**",
              loaded.pop_next(recent_signatures=wall) is None)
        # 被挡住 = 没交付 = 没标 used -> 库存**不变**。
        check("**被挡住后 stock 仍是 1(没交付就不扣库存)**",
              loaded.stock_count() == 1, loaded.stock_count())
        check("used 也是 0(没交付就没写账本)",
              loaded.used_count() == 0, loaded.used_count())


def test_stock_count_limit_early_exit():
    print("\n[A4] stock_count(limit) 早退")
    with tmpdir() as d:
        pool = PuzzlePool.open(mkcfg(d))
        for i in range(6):
            pool.add(variant(i))
        check("前置: 池里 6 道", pool.stock_count() == 6, pool.stock_count())
        check("limit=2 -> 2", pool.stock_count(limit=2) == 2,
              pool.stock_count(limit=2))
        check("limit=None -> 6", pool.stock_count() == 6, pool.stock_count())
        check("limit=99 -> 6", pool.stock_count(limit=99) == 6)


def test_ledger_trustworthy_is_read_only_view():
    print("\n[A5] ledger_trustworthy")
    with tmpdir() as d:
        cfg = mkcfg(d)
        check("新池可信", PuzzlePool.open(cfg).ledger_trustworthy is True)
        _write_raw(cfg.pool_used_path, ["null"])
        check("坏账本 -> False",
              PuzzlePool.open(cfg).ledger_trustworthy is False)


def test_stats_exposes_stock_and_trustworthy():
    print("\n[A6] stats 暴露 stock / trustworthy")
    with tmpdir() as d:
        cfg = mkcfg(d)
        pool = PuzzlePool.open(cfg)
        pool.add(good_spec())
        st = pool.stats()
        check("stats 有 stock", st.get("stock") == 1, st.get("stock"))
        check("stats 有 trustworthy", st.get("trustworthy") is True)
        check("stats 的 stock 与 stock_count 一致",
              st["stock"] == pool.stock_count())


def test_pressure_probe_shape():
    print("\n[A7] engine.pressure()")
    e = RoundEngine(mkcfg(tempfile.mkdtemp()))
    p = e.pressure()
    check("初始 IDLE", p["phase"] == Phase.IDLE, p["phase"])
    check("hint_inflight False", p["hint_inflight"] is False)
    check("reveal_inflight False", p["reveal_inflight"] is False)
    check("stopped False", p["stopped"] is False)
    check("pending/inflight 是分开的数",
          p["pending"] == 0 and p["inflight"] == 0)


def test_pressure_sees_hint_inflight():
    """hint 在途时 pressure 必须看得见 —— `pending_count` 看不见它
    (它只数观众的提问), 少了这一维补池会和提示生成抢网关。"""
    print("\n[A8] pressure 看得见 hint 在途")
    e = RoundEngine(mkcfg(tempfile.mkdtemp()))
    with e._lock:
        e._hint_pending = True
    check("hint_inflight True", e.pressure()["hint_inflight"] is True)


def test_generation_inputs_shared_by_first_and_retry():
    """首轮 == 重试 == 补池快照。

    这是 Q4 那个 bug 的回归: 早先重试路径漏带 avoid/recent_signatures,
    于是**只要发生一次外层 retry 就能绕过整个 Q4**。三条路径必须同源。
    """
    print("\n[A9] 生成输入: 首轮 == 重试 == 补池快照")
    e2 = RoundEngine(mkcfg(tempfile.mkdtemp()))
    # 先塞状态 —— 否则两边都是空的, "一致"恒真, 抓不到漂移
    with e2._lock:
        e2._used_titles = ["旧题A", "旧题B"]
        e2._recent_signatures = [PuzzleSignature(
            mechanism_family="hidden_function",
            solution_shape="hidden_function_explains_behavior")]
    gi = e2.snapshot_generation_inputs()
    check("快照里有 avoid", gi["avoid"] == ["旧题A", "旧题B"], gi["avoid"])
    check("快照里有 recent", len(gi["recent_signatures"]) == 1)
    first = e2.request_riddle_action("riddle")
    retry = e2._riddle_action_locked("riddle_retry", attempt=1)
    check("首轮带 avoid", first.payload.get("avoid") == gi["avoid"],
          first.payload.get("avoid"))
    check("**重试也带 avoid(否则一次 retry 就绕过整个 Q4)**",
          retry.payload.get("avoid") == gi["avoid"],
          retry.payload.get("avoid"))
    check("**重试也带 recent_signatures**",
          retry.payload.get("recent_signatures") == gi["recent_signatures"])
    check("重试带 attempt 标记", retry.payload.get("attempt") == 1)
    check("三者同源",
          first.payload["avoid"] == retry.payload["avoid"] == gi["avoid"]
          and first.payload["recent_signatures"]
          == retry.payload["recent_signatures"] == gi["recent_signatures"])


def test_inverted_hysteresis_is_flagged():
    print("\n[A10] Config 抓反了的滞回")
    w = Config(sim_path="x", pool_min_size=5, pool_target_size=2).validate()
    check("target<min 有告警", any("滞回反了" in x for x in w), w)
    w2 = Config(sim_path="x", pool_min_size=2, pool_target_size=5).validate()
    check("正常配置无此告警", not any("滞回反了" in x for x in w2), w2)
    w3 = Config(sim_path="x", pool_prefetch_backoff_s=0).validate()
    check("退避<=0 有告警", any("退避" in x for x in w3), w3)


def test_cli_no_prefetch_is_wired():
    """只加 flag 不接线 = dead config(参数形同虚设, 而 --help 里写着)。"""
    print("\n[A11] CLI --no-prefetch 真的接上了")
    from story.config import build_parser, from_args
    a = build_parser().parse_args(["--sim", "x"])
    check("默认开启", a.pool_prefetch_enabled is True, a.pool_prefetch_enabled)
    cfg = from_args(["--sim", "x", "--no-prefetch"])
    check("from_args 真接上", cfg.pool_prefetch_enabled is False,
          cfg.pool_prefetch_enabled)
    cfg2 = from_args(["--sim", "x"])
    check("不带 flag 时为 True", cfg2.pool_prefetch_enabled is True)



# ======================================================================
# B. PoolPrefetcher 状态机
# ======================================================================
class _SyncExecutor:
    """submit 立即执行, 但仍返回一个**已完成的 Future**。

    这样单飞/退避走的是**真实 Future 路径**(没被绕过), 而测试是确定的。
    """
    def __init__(self):
        self.submitted = []

    def submit(self, fn, *a, **kw):
        self.submitted.append((fn, a, kw))
        f = Future()
        try:
            f.set_result(fn(*a, **kw))
        except BaseException as e:              # noqa: BLE001
            f.set_exception(e)
        return f

    def shutdown(self, **kw):
        pass


class _ManualExecutor:
    """只入队不执行 —— 用来测"仍在途"。

    `run_next()` 会**完成 submit 时返回的那个 Future**(而不是新建一个),
    否则 `_future` 永远 pending, 单飞守卫会把后续全部挡掉 —— 那不是
    被测代码的问题, 是替身没把 Future 语义做对。

    `pending` 是**还没执行**的; `total` 是**累计提交过**的。断言"起了几个
    任务"要看 `total` —— `pending` 会随 run_next 减少。
    """
    def __init__(self):
        self.pending = []
        self.total = 0

    def submit(self, fn, *a, **kw):
        f = Future()
        self.pending.append((fn, a, kw, f))
        self.total += 1
        return f

    def run_next(self):
        fn, a, kw, f = self.pending.pop(0)
        try:
            f.set_result(fn(*a, **kw))
        except BaseException as e:              # noqa: BLE001
            f.set_exception(e)
        return f

    def shutdown(self, **kw):
        pass


class _FakeWriter:
    """假生成器。specs 为空时返回一道新的 variant。"""
    def __init__(self, specs=None, fail=False):
        self.calls = []
        self._specs = list(specs or [])
        self.fail = fail
        self._n = 0

    def gen_spec(self, avoid=None, blueprint=None, recent=None,
                 enforce_blueprint=None, **kw):
        self.calls.append({"avoid": avoid, "recent": recent,
                           "blueprint": blueprint,
                           "enforce_blueprint": enforce_blueprint})
        if self.fail:
            s = good_spec()
            s.puzzle = ""
            s.error = "模拟生成失败"
            return s
        if self._specs:
            return self._specs.pop(0)
        self._n += 1
        return variant(self._n)


class _Clock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def mkpf(tmp, pool=None, writer=None, probe=None, clock=None, executor=None,
         **cfgkw):
    """建一个 PoolPrefetcher, 协作者默认都是"最宽松"的假件。"""
    from story.prefetch import PoolPrefetcher
    cfgkw.setdefault("pool_min_size", 2)
    cfgkw.setdefault("pool_target_size", 5)
    cfg = mkcfg(tmp, **cfgkw)
    if pool is None:
        pool = PuzzlePool.open(cfg)
    if writer is None:
        writer = _FakeWriter()
    if probe is None:
        probe = lambda: {"phase": Phase.QA, "pending": 0, "inflight": 0,
                         "hint_inflight": False, "reveal_inflight": False,
                         "stopped": False}
    pf = PoolPrefetcher(
        cfg=cfg, pool=pool, writer=writer, probe=probe,
        probe_inputs=lambda: {"avoid": [], "recent_signatures": []},
        pick_blueprint=lambda recent, rng=None: None,
        clock=clock or _Clock(), executor=executor or _SyncExecutor())
    return pf


def fill(pool, n):
    for i in range(n):
        pool.add(variant(100 + i))


def test_latch_walk_min2_target5():
    """滞回: 库存 5-4-3-2 不补; 2-1 启动; 1-2-3-4 仍 active; 到 5 才清。

    这是契约里"min/target 必须是真正的 hysteresis"的直接落地。若写成
    每拍 `if stock < min`, 补到 2 就停了, target 永远没有意义。
    """
    print("\n[B1] 滞回 latch: min=2 / target=5 完整走一遍")
    with tmpdir() as d:
        ex = _ManualExecutor()
        pool = PuzzlePool.open(mkcfg(d))
        pf = mkpf(d, pool=pool, executor=ex)
        fill(pool, 5)
        check("起始 stock=5", pool.stock_count() == 5, pool.stock_count())
        pf.on_tick()
        check("5: 不补(latch 没启动)", pf._refill_active is False)
        check("5: 零提交", ex.total == 0, ex.total)
        for st in (4, 3, 2):
            pool.pop_next(recent_signatures=[])
            pf.on_tick()
        check("降到 2 仍不补", pf._refill_active is False, "不该启动")
        check("降到 2 零提交", ex.total == 0, ex.total)
        pool.pop_next(recent_signatures=[])
        pf.on_tick()
        check("**降到 1 启动补池周期**", pf._refill_active is True)
        check("启动了 -> 提交了一次", ex.total == 1, ex.total)

        # 现在库存 1, latch active。跑掉那个在途任务(它会 add 一道 -> 库存 2),
        # 然后每拍都在"是否仍 active"上检查: 关键是 **2/3/4 时都不能清**。
        ex.run_next()                       # worker 跑完 -> add 一道 -> 库存 2
        pf.on_tick()                        # 回收 + 决策(并会再提交一道)
        check("补到 2: latch 仍 active(**每拍判 stock<min 会在这里挂**)",
              pf._refill_active is True, "stock=%d" % pool.stock_count())
        check("补到 2 后又提交了下一道", ex.total == 2, ex.total)
        ex.run_next()
        pf.on_tick()
        check("补到 3: 仍 active", pf._refill_active is True,
              "stock=%d" % pool.stock_count())
        ex.run_next()
        pf.on_tick()
        check("补到 4: 仍 active", pf._refill_active is True,
              "stock=%d" % pool.stock_count())
        ex.run_next()                       # -> 库存 5
        pf.on_tick()
        check("**到 5 才清 latch**", pf._refill_active is False,
              "stock=%d" % pool.stock_count())


def test_latch_held_under_pressure():
    """压力高时 latch 只是**被按住**, 不是被放弃 —— 压力一过接着补。"""
    print("\n[B2] 压力高时 latch 保持")
    with tmpdir() as d:
        ex = _ManualExecutor()
        pool = PuzzlePool.open(mkcfg(d))
        busy = {"v": False}
        pf = mkpf(d, pool=pool, executor=ex,
                  probe=lambda: {"phase": Phase.QA,
                                 "pending": 3 if busy["v"] else 0,
                                 "inflight": 0, "hint_inflight": False,
                                 "reveal_inflight": False, "stopped": False})
        fill(pool, 1)
        busy["v"] = True
        pf.on_tick()
        check("压力高 -> latch 仍 active", pf._refill_active is True)
        check("压力高 -> 零提交", ex.total == 0, ex.total)
        busy["v"] = False
        pf.on_tick()
        check("压力过去 -> 恢复提交", ex.total == 1,
              ex.total)


def test_two_ticks_produce_one_task():
    """单飞: 连点多拍只 submit 一次。

    守卫是 _future, **不是** max_workers=1 —— 后者挡不住 tick
    往队列里排任务。
    """
    print("\n[B3] 单飞: 多拍只起一个任务")
    with tmpdir() as d:
        ex = _ManualExecutor()
        pool = PuzzlePool.open(mkcfg(d))
        pf = mkpf(d, pool=pool, executor=ex)
        fill(pool, 1)
        pf.on_tick()
        pf.on_tick()
        pf.on_tick()
        check("**三拍只提交一次**", ex.total == 1, ex.total)


def test_next_task_only_after_done():
    print("\n[B4] 完成之后才起下一个")
    with tmpdir() as d:
        ex = _ManualExecutor()
        pool = PuzzlePool.open(mkcfg(d))
        pf = mkpf(d, pool=pool, executor=ex)
        fill(pool, 1)
        pf.on_tick()
        check("第一次提交", ex.total == 1, ex.total)
        pf.on_tick()
        check("没完成 -> 不起新的", ex.total == 1, ex.total)
        ex.run_next()
        pf.on_tick()
        check("完成 -> 可以起下一个", ex.total == 2,
              ex.total)


def test_max_workers_one_is_not_the_guard():
    """用**真** executor(1) + 慢 writer: 5 拍仍只提交一次。

    证明守卫是 _future 而不是线程池大小。
    """
    print("\n[B5] 真 executor(1) 下仍单飞")
    import threading as _th
    from concurrent.futures import ThreadPoolExecutor as _TPE
    with tmpdir() as d:
        pool = PuzzlePool.open(mkcfg(d))
        release = _th.Event()

        class _SlowWriter:
            def __init__(self):
                self.calls = 0

            def gen_spec(self, **kw):
                self.calls += 1
                release.wait(timeout=5)
                return good_spec()

        w = _SlowWriter()
        pf = mkpf(d, pool=pool, writer=w, executor=_TPE(max_workers=1))
        fill(pool, 1)
        for _ in range(5):
            pf.on_tick()
        check("**5 拍仍只起 1 个生成**", w.calls == 1, w.calls)
        release.set()
        pf._executor.shutdown(wait=True)


def test_gen_failure_sets_backoff():
    """失败 -> 退避; 退避期内不再重试; 到期才重试。

    注意**结果的时序**: 同步替身下, 提交那一拍 worker 就跑完了, 但结果
    是在**下一拍**才被应用的(轮询 `future.done()` 的设计使然 —— 见
    on_tick 的 ①② )。所以断言要看下一拍之后的状态。
    """
    print("\n[B6] 生成失败 -> 退避, 到期才重试")
    with tmpdir() as d:
        clk = _Clock()
        w = _FakeWriter(fail=True)
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), writer=w, clock=clk,
                  pool_prefetch_backoff_s=30.0)
        fill(pf.pool, 1)
        pf.on_tick()
        check("第一次尝试了", len(w.calls) == 1, len(w.calls))
        pf.on_tick()                    # 这一拍才**应用**上一拍的结果
        check("**设了退避**", pf._retry_at > 0, pf._retry_at)
        check("失败计数 +1", pf.fail_count == 1, pf.fail_count)
        pf.on_tick()
        pf.on_tick()
        check("**退避期内不重试**", len(w.calls) == 1, len(w.calls))
        clk.advance(31)
        pf.on_tick()
        check("到期后重试", len(w.calls) == 2, len(w.calls))


def test_add_failure_discards_and_backs_off():
    """契约: add 失败 = 这次生成**丢弃**。

    不留内存副本、不直接上屏 —— 否则池子里会出现"内存一次性库存",
    而它没有 used 行, Q8 的重启语义立刻无法推理。

    这里断言的是**可观察的后果**: 生成出来的那道题没有以任何形式
    留存 —— 既不在池子里, 也没有被推上屏(submit_riddle 零调用),
    而且确实进了退避。
    """
    print("\n[B7] add 失败 -> 丢弃 + 退避")
    with tmpdir() as d:
        clk = _Clock()
        pool = PuzzlePool.open(mkcfg(d))
        real_add = pool.add

        def _refuse(spec, source="manual"):
            return False

        pool.add = _refuse
        w = _FakeWriter()
        pf = mkpf(d, pool=pool, writer=w, clock=clk)
        fill(pool, 1)
        before = pool.size()
        pf.on_tick()
        check("尝试了一次", len(w.calls) == 1, len(w.calls))
        pf.on_tick()
        check("**池子里没有它(丢弃了)**", pool.size() == before, pool.size())
        check("**设了退避**", pf._retry_at > 0, pf._retry_at)
        # 契约的代码级表达: 本模块不该有任何"生成成功就上屏"的出口。
        # 用 AST 而不是 grep —— 文件 docstring 里**提到**了 submit_riddle
        # (在"不得出现"清单里), 纯文本搜索会被自己的注释误伤。
        import ast as _ast
        from story import prefetch as _pfmod
        tree = _ast.parse(_pfmod.__file__ and open(
            _pfmod.__file__, encoding="utf-8").read())
        names = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, _ast.Name):
                names.add(node.id)
        for forbidden in ("pop_next", "mark_used", "remember_avoid",
                          "submit_riddle", "load"):
            check(f"**代码里不出现 {forbidden}**", forbidden not in names)
        pool.add = real_add


def test_success_clears_backoff():
    print("\n[B8] 成功清退避")
    with tmpdir() as d:
        clk = _Clock()
        pool = PuzzlePool.open(mkcfg(d))
        pf = mkpf(d, pool=pool, writer=_FakeWriter(fail=True), clock=clk,
                  pool_prefetch_backoff_s=30.0)
        fill(pool, 1)
        pf.on_tick()
        pf.on_tick()                    # 应用失败结果 -> 有退避
        check("先失败 -> 有退避", pf._retry_at > 0, pf._retry_at)
        clk.advance(31)
        pf.writer = _FakeWriter()
        pf.on_tick()
        pf.on_tick()                    # 应用成功结果
        check("**成功 -> 退避清零**", pf._retry_at == 0.0, pf._retry_at)


def test_failure_does_not_abandon_latch():
    print("\n[B9] 失败不放弃 latch")
    with tmpdir() as d:
        clk = _Clock()
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)),
                  writer=_FakeWriter(fail=True), clock=clk)
        fill(pf.pool, 1)
        pf.on_tick()
        check("latch 仍 active", pf._refill_active is True)
        clk.advance(31)
        pf.on_tick()                    # 应用失败 -> 退避, 但不清 latch
        pf.on_tick()
        check("退避后仍 active(失败不放弃周期)",
              pf._refill_active is True, pf._refill_active)


def test_untrustworthy_ledger_disables_prefetch():
    """账本不可信 -> **完全不补池**, 且不改 _used_trustworthy。

    那时 pop_next 一道都不交付, 灌题只是白烧网关配额。
    """
    print("\n[B10] 账本不可信 -> 不补池")
    with tmpdir() as d:
        cfg = mkcfg(d)
        w = _FakeWriter()
        pool = PuzzlePool.open(cfg)
        fill(pool, 1)
        _write_raw(cfg.pool_used_path, ["null"])
        pool2 = PuzzlePool.open(cfg)
        check("账本确实不可信", pool2.ledger_trustworthy is False)
        pf = mkpf(d, pool=pool2, writer=w)
        for _ in range(5):
            pf.on_tick()
        check("**零生成**", len(w.calls) == 0, len(w.calls))
        check("**latch 也没启动**", pf._refill_active is False)
        check("**没有改 _used_trustworthy**",
              pool2._used_trustworthy is False)


def test_prefetch_never_writes_used_trustworthy():
    print("\n[B11] 补池绝不写 _used_trustworthy")
    with tmpdir() as d:
        pool = PuzzlePool.open(mkcfg(d))
        pf = mkpf(d, pool=pool)
        fill(pool, 1)
        for _ in range(3):
            pf.on_tick()
        check("仍为 True(完整周期)", pool._used_trustworthy is True)


def test_prefetch_never_delivers_or_marks_used():
    """本模块只能往池子里**加**东西。

    绝不能调 pop_next / mark_used —— 那会越过 Q8 的交付事务边界。
    """
    print("\n[B12] 补池不交付、不标 used")
    with tmpdir() as d:
        pool = PuzzlePool.open(mkcfg(d))
        called = {"pop": 0, "mark": 0}
        real_pop, real_mark = pool.pop_next, pool.mark_used

        def _spy_pop(*a, **kw):
            called["pop"] += 1
            return real_pop(*a, **kw)

        def _spy_mark(*a, **kw):
            called["mark"] += 1
            return real_mark(*a, **kw)

        pool.pop_next = _spy_pop
        pool.mark_used = _spy_mark
        pf = mkpf(d, pool=pool)
        fill(pool, 1)
        for _ in range(3):
            pf.on_tick()
        check("**没调过 pop_next**", called["pop"] == 0, called["pop"])
        check("**没调过 mark_used**", called["mark"] == 0, called["mark"])
        check("used 账本没被写", pool.used_count() == 0, pool.used_count())
        # 三拍下来会一路补向高水位 —— 关键是**只增不减**, 且交付路径没被碰。
        check("**池子只多不少(真的在补)**",
              pool.stock_count() > 1, pool.stock_count())


def test_prefetch_does_not_take_narrating():
    """补池用自己的锁。若共用 _narrating, 补池持锁会把 live 出题挤成
    "推迟到下一拍" —— 优先级完全倒过来。"""
    print("\n[B13] 补池不碰 _narrating")
    with tmpdir() as d:
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)))
        fill(pf.pool, 1)
        pf.on_tick()
        check("**prefetcher 没有 _narrating 属性**",
              not hasattr(pf, "_narrating"))
        check("它自己有一把锁", hasattr(pf, "_lock"))


def test_snapshot_taken_at_submit_time():
    """契约: 生成时 recent 用启动任务那一刻的快照。"""
    print("\n[B14] 快照在提交时取")
    with tmpdir() as d:
        ex = _ManualExecutor()
        pool = PuzzlePool.open(mkcfg(d))
        state = {"recent": ["旧快照"]}
        w = _FakeWriter()
        from story.prefetch import PoolPrefetcher
        cfg = mkcfg(d)
        pf = PoolPrefetcher(
            cfg=cfg, pool=pool, writer=w,
            probe=lambda: {"phase": Phase.QA, "pending": 0, "inflight": 0,
                           "hint_inflight": False, "reveal_inflight": False,
                           "stopped": False},
            probe_inputs=lambda: {"avoid": [],
                                  "recent_signatures": list(state["recent"])},
            pick_blueprint=lambda recent, rng=None: None,
            clock=_Clock(), executor=ex)
        fill(pool, 1)
        pf.on_tick()
        check("提交了", ex.total == 1)
        state["recent"] = ["新快照"]
        ex.run_next()
        check("**执行时看到的是旧快照**",
              w.calls[0]["recent"] == ["旧快照"], w.calls[0]["recent"])


def test_pop_next_still_uses_current_gate():
    """补池时合格 != 播出时仍合格。pop_next 必须重新过**当前**窗口。"""
    print("\n[B15] pop_next 仍走当前 gate(补池不改这一点)")
    with tmpdir() as d:
        pool = PuzzlePool.open(mkcfg(d))
        pf = mkpf(d, pool=pool)
        fill(pool, 1)
        pf.on_tick()
        check("补池后有 2 道", pool.stock_count() == 2, pool.stock_count())
        sig = pool._items[0].signature.to_dict()
        wall = [sig] * 10
        got = pool.pop_next(recent_signatures=wall)
        check("**被当前窗口挡住 -> None**", got is None, got)


def test_disabled_prefetch_is_truly_off():
    print("\n[B16] 关掉开关 = 真的不补")
    with tmpdir() as d:
        ex = _ManualExecutor()
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), executor=ex,
                  pool_prefetch_enabled=False)
        fill(pf.pool, 1)
        for _ in range(5):
            pf.on_tick()
        check("零提交", ex.total == 0, ex.total)
        check("latch 也没启动", pf._refill_active is False)


def test_no_writer_disables_prefetch():
    """no_llm -> writer is None -> **彻底不生成**(不能灌假题进池)。"""
    print("\n[B17] writer 为 None -> 不生成")
    with tmpdir() as d:
        ex = _ManualExecutor()
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), writer=None, executor=ex)
        pf.writer = None
        fill(pf.pool, 1)
        for _ in range(5):
            pf.on_tick()
        check("零提交(绝不灌假题)", ex.total == 0, ex.total)


def test_on_tick_never_raises():
    """补池的任何异常都不能冒到 tick 线程上。"""
    print("\n[B18] on_tick 绝不抛")
    with tmpdir() as d:
        class _BoomPool:
            @property
            def ledger_trustworthy(self):
                raise RuntimeError("炸")

            def stock_count(self, **kw):
                raise RuntimeError("炸")

        pf = mkpf(d, pool=_BoomPool())
        try:
            for _ in range(3):
                pf.on_tick()
            ok = True
        except Exception as e:                  # noqa: BLE001
            ok = False
            print("     抛了:", e)
        check("**池子炸了也不抛**", ok)

        class _BoomWriter:
            def gen_spec(self, **kw):
                raise ZeroDivisionError("炸")

        pf2 = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), writer=_BoomWriter())
        fill(pf2.pool, 1)
        try:
            pf2.on_tick()
            ok2 = True
        except Exception as e:                  # noqa: BLE001
            ok2 = False
            print("     抛了:", e)
        check("**生成炸了也不抛**", ok2)
        pf2.on_tick()                   # 应用上一拍的结果
        check("异常也计入退避", pf2._retry_at > 0, pf2._retry_at)
        check("异常也计入失败数", pf2.fail_count == 1, pf2.fail_count)


def main():
    tests = [
        # A. 库存与探针
        test_stock_count_excludes_used,
        test_stock_count_excludes_invalid_spec,
        test_stock_count_ignores_recent_window,
        test_stock_count_limit_early_exit,
        test_ledger_trustworthy_is_read_only_view,
        test_stats_exposes_stock_and_trustworthy,
        test_pressure_probe_shape,
        test_pressure_sees_hint_inflight,
        test_generation_inputs_shared_by_first_and_retry,
        test_inverted_hysteresis_is_flagged,
        test_cli_no_prefetch_is_wired,
        # B. PoolPrefetcher 状态机
        test_latch_walk_min2_target5,
        test_latch_held_under_pressure,
        test_two_ticks_produce_one_task,
        test_next_task_only_after_done,
        test_max_workers_one_is_not_the_guard,
        test_gen_failure_sets_backoff,
        test_add_failure_discards_and_backs_off,
        test_success_clears_backoff,
        test_failure_does_not_abandon_latch,
        test_untrustworthy_ledger_disables_prefetch,
        test_prefetch_never_writes_used_trustworthy,
        test_prefetch_never_delivers_or_marks_used,
        test_prefetch_does_not_take_narrating,
        test_snapshot_taken_at_submit_time,
        test_pop_next_still_uses_current_gate,
        test_disabled_prefetch_is_truly_off,
        test_no_writer_disables_prefetch,
        test_on_tick_never_raises,
    ]
    for t in tests:
        t()
    print()
    if FAIL[0]:
        print(f"FAILED: {FAIL[0]} 项")
        return 1
    print("PASS: 补池(零件 + 状态机) 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
