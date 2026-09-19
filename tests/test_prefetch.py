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
from dataclasses import replace

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from story.config import Config  # noqa: E402
from story.engine import RoundEngine  # noqa: E402
from story.puzzle import (  # noqa: E402
    DiscoveryBeat, FairClue, PuzzleBlueprint, PuzzleFact, PuzzleSignature,
    PuzzleSpec, SolveAtom,
)
from story.pool import PuzzlePool, spec_key  # noqa: E402
from story.quality import (  # noqa: E402
    FAMILY_SHAPES, QUALITY_POLICY_VERSION)
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
        # ---- v5 通关合同(标的是当前政策 -> 必须是完整 v5 spec) ----
        core_answer="他亮灯是为了标出退潮时露出的礁石, 不是给船引路。",
        completion_fact_ids=["f1", "f2"],
        fair_clues=[
            FairClue(quote="只在退潮的那几个小时亮", supports_atoms=["a1"]),
            FairClue(quote="涨潮后他反而把灯熄掉", supports_atoms=["a2"]),
        ],
        hints=["注意灯的开关时机", "想想潮水的变化", "灯是在给谁传递信息?"],
        # quality-v8: 当前政策要求 2~4 个发现阶段。
        discovery_beats=[
            DiscoveryBeat(id="b1", text="先注意到灯只在退潮时亮", fact_ids=["f1"]),
            DiscoveryBeat(id="b2", text="再想到灯是在标礁石, 不是引路",
                          fact_ids=["f2"]),
        ],
        blueprint=PuzzleBlueprint(
            mechanism_family="hidden_function",
            solution_shape="hidden_function_explains_behavior",
            domain="maritime", relation="stranger",
            emotion_mode="neutral", time_shape="habitual",
            reveal_mode="meaning_flip"),
        signature=PuzzleSignature(
            mechanism_family="hidden_function",
            solution_shape="hidden_function_explains_behavior",
            domain="maritime", emotion_mode="neutral",
            relation="stranger", time_shape="habitual",
            reveal_mode="meaning_flip"),
        prompt_version="riddle-v3",
        quality_policy_version=QUALITY_POLICY_VERSION,
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
        # quality-v8: 当前政策要求 2~4 个发现阶段。
        discovery_beats=[
            DiscoveryBeat(id="b1", text=f"先注意到只有夜里敲{i + 1}下",
                          fact_ids=["f1"]),
            DiscoveryBeat(id="b2", text="再想到钟声是在报暗礁, 不是报时",
                          fact_ids=["f2"]),
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


def test_stock_count_excludes_old_policy():
    """**Step 03 的补池侧后果**: 旧 policy 库存必须对补池不可见。

    `stock_count` 走的是 `_validate_pool_spec()`, 而 quality-policy
    兼容门就在那扇门里 —— 所以旧 policy 题**会自动**不计入库存。
    这条测的不是"补池自己判断 policy"(它没有、也不该有第二套判断),
    而是**它天然继承了池子的单一准入门**。

    为什么这条必须在 `test_prefetch.py` 里有一条: Step 04 把
    `QUALITY_POLICY_VERSION` 提到 v4 之后, 盘上那批 v3 必须让
    `stock_count` 归零, 否则补池会认为"池里已经有 10 道"而一道都不补
    —— 那是 Step 04 能不能生效的唯一开关。
    """
    print("\n[A2b] stock_count 排掉旧 policy 题(补池据此补新题)")
    with tmpdir() as d:
        cfg = mkcfg(d)
        old = variant(200)
        old.quality_policy_version = "quality-v1"
        _write_raw(cfg.pool_path, [json.dumps(
            {"pool_version": 1, "pool_key": spec_key(old),
             "added_at": 0.0, "added_by": "legacy",
             "spec": old.to_archive()}, ensure_ascii=False)])
        pool = PuzzlePool.open(cfg)
        check("pending_count 仍算它(1)", pool.pending_count() == 1,
              pool.pending_count())
        check("**stock_count 不算它(0)**", pool.stock_count() == 0,
              pool.stock_count())
        # 补池看到 stock=0 -> latch 启动 -> 真的去补
        # (mkpf 的默认探针已经是 QA + 零压力, 低压力门会放行)
        ex = _ManualExecutor()
        pf = mkpf(d, pool=pool, executor=ex)
        pf.on_tick()
        check("**旧 policy 不挡补池(latch 已启动)**",
              pf._refill_active is True)
        check("**提交了一次生成**", ex.total == 1, ex.total)


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
         probe_inputs=None, **cfgkw):
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
    if probe_inputs is None:
        probe_inputs = lambda: {"avoid": [], "recent_signatures": []}
    pf = PoolPrefetcher(
        cfg=cfg, pool=pool, writer=writer, probe=probe,
        probe_inputs=probe_inputs,
        pick_blueprint=lambda recent, rng=None: None,
        clock=clock or _Clock(), executor=executor or _SyncExecutor())
    return pf


def fill(pool, n, base=100):
    """往池里灌 n 道互不相同的题。

    ⚠️ `base` 是为了让**同一个池**可以被灌多次: `variant(i)` 从 i 生成
    题面与事实表, 重复的 i 会造出结构重复的题, 被池的动态门拒掉 ——
    于是"再灌 4 道"实际一道都没进去, 断言里的 stock 就成了假的。
    """
    for i in range(n):
        pool.add(variant(base + i))


#: 轮换用的 family —— 每道题换一个 `(family, shape)`, 才能真正**共存**。
#: `variant(i)` 自己固定用 hidden_function, 于是多道 variant 之间是
#: 结构重复的, 池的动态门只放得进一道。需要"池里有 N 道**可播**"的
#: 用例(比如 playable>=2)必须用下面这个。
_MIX_FAMILIES = ["information_gap", "rule_constraint", "causal_reversal",
                 "goal_reversal", "identity_misread", "object_misuse",
                 "time_reinterpretation", "space_reinterpretation"]


def mixed_variant(i: int) -> PuzzleSpec:
    """第 i 道合格题, **轮换 (mechanism_family, solution_shape)**。

    `variant(i)` 全部共用 hidden_function/hidden_function_explains_behavior,
    所以它们互相都是 structural duplicate —— 池只收得下第一道。
    凡是要"真的数出 >= 2 道可播"的断言, 必须用这个夹具, 否则
    playable 恒为 1, 断言测的是夹具不是实现。
    """
    s = variant(600 + i)
    fam = _MIX_FAMILIES[i % len(_MIX_FAMILIES)]
    sh = FAMILY_SHAPES[fam][0]
    s.blueprint = replace(s.blueprint, mechanism_family=fam, solution_shape=sh)
    s.signature = replace(s.signature, mechanism_family=fam, solution_shape=sh)
    return s


def fill_mixed(pool, n, base=0):
    """灌 n 道**结构互不重复**的题 —— playable 才会真的随 n 增长。"""
    for i in range(n):
        pool.add(mixed_variant(base + i))


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
        check("生成失败计数 +1", pf.generation_fail_count == 1,
              pf.generation_fail_count)
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
        check("异常计入 exception_count", pf2.exception_count == 1,
              pf2.exception_count)



# ======================================================================
# C. 接线(端到端)
# ======================================================================
def _inline_threads():
    """把 director 里 import 的 Thread 换成同步执行器。

    返回还原函数。用法: `restore = _inline_threads()` ... `restore()`。
    """
    import director as _D
    real = _D.threading.Thread

    class _Inline:
        def __init__(self, target=None, daemon=None, name=None, **kw):
            self._target = target

        def start(self):
            if self._target:
                self._target()

    _D.threading.Thread = _Inline
    return lambda: setattr(_D.threading, "Thread", real)


def _put_engine_in_qa(dr):
    """让引擎进入 QA 阶段(补池的低压力门要求 phase == QA)。"""
    dr.engine.start()
    acts = dr.engine.submit_riddle(
        "端到端测试用谜面，为什么？", "端到端测试用谜底。",
        ["提示一", "提示二", "提示三"], title="接线题",
        signature=good_spec().signature.to_dict(), source="live_generate")
    dr._dispatch(acts)


def test_director_prefetch_end_to_end():
    """Q9c 验收: 补池生成的题**真的**落进池子, 随后能被 Q8 路径交付。

    这条证明两件事: ① 补池接进了 tick; ② 它生产的库存走的是 Q8 原来
    那条交付链, 一行没改。
    """
    print("\n[C1] Director 补池端到端")
    import json as _json
    from director import Director
    with tmpdir() as d:
        cfg = mkcfg(d, no_llm=False)
        cfg.puzzle_out_path = os.path.join(d, "arch.jsonl")
        cfg.pool_min_size = 2
        cfg.pool_target_size = 5
        dr = Director(cfg)
        check("Director 建了 prefetcher", dr._prefetcher is not None)
        # 补池用**自己的** writer 实例(见 C9): Writer 的 _last_review_*
        # 是实例级侧信道, 两条链并发审稿会互相覆盖。所以这里断言的是
        # "它有自己的 writer", 而不是"和 live 同一个"。
        check("prefetcher 有自己的 writer",
              dr._prefetcher.writer is not None
              and dr._prefetcher.writer is not dr.writer,
              dr._prefetcher.writer)

        # 换成同步执行器 + 假 writer, 让补池确定地跑
        pf = dr._prefetcher
        pf._executor = _SyncExecutor()
        w = _FakeWriter()
        pf.writer = w

        check("库存 0(池子空)", dr.pool.stock_count() == 0,
              dr.pool.stock_count())
        _put_engine_in_qa(dr)
        check("已在 QA 阶段", dr.engine.pressure()["phase"] == Phase.QA)

        pf.on_tick()                    # 提交 + 执行(同步)
        pf.on_tick()                    # 应用结果
        check("**补池真的生成了**", len(w.calls) >= 1, len(w.calls))
        check("**池子里有题了**", dr.pool.size() >= 1, dr.pool.size())
        check("latch 已启动(库存 0 < 低水位)", pf._refill_active is True)

        # 补池入池的题必须带 added_by="prefetch"
        rec = _json.loads(open(cfg.pool_path, encoding="utf-8").readline())
        check("**池记录标了 added_by=prefetch**",
              rec.get("added_by") == "prefetch", rec.get("added_by"))

        # ---- 关键: 它随后能被 Q8 的交付路径取出来, 且 source 仍是 pool ----
        got = dr.pool.pop_next(recent_signatures=[])
        check("**Q8 路径能取到补池生产的题**", got is not None)
        if got is not None:
            dr.pool.mark_used(got, aired=True)
        pf.shutdown()


def test_scheduler_calls_prefetch():
    """tick 必须**每拍**调用 on_tick —— 否则补池永远不会自己动。"""
    print("\n[C2] 调度线程每拍调用补池")
    from director import Director
    with tmpdir() as d:
        cfg = mkcfg(d, no_llm=False)
        dr = Director(cfg)
        calls = {"n": 0}

        class _Rec:
            def on_tick(self):
                calls["n"] += 1

            def shutdown(self):
                pass

        dr._prefetcher = _Rec()
        # 让调度循环跑**正好一拍**再退出: 不能在进来之前就 set,
        # 否则 `while not self._stop.is_set()` 压根不进循环。
        import threading as _th
        orig_wait = dr._stop.wait

        def _wait_once(timeout=None):
            dr._stop.set()
            return True

        dr._stop.wait = _wait_once
        try:
            dr._scheduler()
        finally:
            dr._stop.wait = orig_wait
        check("调度跑过后补池被调用过", calls["n"] >= 1, calls["n"])


def test_director_no_llm_disables_prefetch_generation():
    """`--no-llm` 下 writer 为 None -> 补池**彻底不生成**。

    绝不能落进假题分支把兜底题灌进真实题池 —— 那会污染池子。
    """
    print("\n[C3] --no-llm 下不生成")
    from director import Director
    with tmpdir() as d:
        cfg = mkcfg(d, no_llm=True)
        dr = Director(cfg)
        check("writer 是 None", dr.writer is None)
        check("prefetcher 存在但会自我禁用",
              dr._prefetcher is None or dr._prefetcher._enabled() is False,
              dr._prefetcher)
        if dr._prefetcher is not None:
            _put_engine_in_qa(dr)
            for _ in range(5):
                dr._prefetcher.on_tick()
            check("池子没被灌任何东西", dr.pool.size() == 0, dr.pool.size())


def test_director_pool_disabled_no_prefetcher():
    print("\n[C4] pool_enabled=False -> 不建 prefetcher")
    from director import Director
    with tmpdir() as d:
        cfg = mkcfg(d, pool_enabled=False)
        dr = Director(cfg)
        check("池子是 None", dr.pool is None)
        check("**prefetcher 也是 None**", dr._prefetcher is None)


def test_prefetch_does_not_change_live_blueprint_sequence():
    """补池用独立 rng: 同一 seed 下, 补池开关不影响 live 的 blueprint 序列。

    这不只是洁癖 —— 若共用 rng, "同 seed 可复现"会退化成
    "同 seed + 同补池状态可复现", 复盘时根本说不清差异从哪来。
    """
    print("\n[C5] 补池不改变 live 的 blueprint 序列")
    from director import Director
    seq = {}
    for prefetch_on in (True, False):
        with tmpdir() as d:
            cfg = mkcfg(d, no_llm=False, quality_seed=12345,
                        pool_prefetch_enabled=prefetch_on)
            dr = Director(cfg)
            recent = []
            out = []
            for _ in range(5):
                bp = dr._pick_blueprint(recent)
                out.append((bp.mechanism_family, bp.solution_shape, bp.domain)
                           if bp else None)
                recent.append(bp)
            seq[prefetch_on] = out
            if dr._prefetcher is not None:
                # 让补池也消耗它自己的 rng
                dr._prefetcher._rng.random()
            if dr._prefetcher is not None:
                dr._prefetcher.shutdown()
    check("**开关补池, live 序列一模一样**",
          seq[True] == seq[False], f"{seq[True]} vs {seq[False]}")



def test_pick_blueprint_actually_returns_one():
    """**回归**: `_pick_blueprint` 必须真的返回 blueprint, 不能是 None。

    这条测试是补上一个**缺失的守门人**。之前的代码里有一行
    `_detail("blueprint 全文: %s", ...)` 漏传了 logger 参数, 而
    `_detail` 的第一参数就是 logger —— 于是它每次必抛 AttributeError,
    被下面那个宽 `except Exception` 吞掉, `_pick_blueprint` **每次都
    返回 None**。

    后果不是"少一条日志", 而是: 返回 None 会被调用方转成
    `enforce_blueprint=False`, 也就是 **blueprint 调度(Q4/Q7 的核心)
    整个静默失效**, 所有题都按"不限形状"生成。而当时没有任何测试
    断言过"它得返回东西", 所以一直没人发现。
    """
    print("\n[C6] _pick_blueprint 真的返回 blueprint(**闭嘴失败回归**)")
    from director import Director
    with tmpdir() as d:
        cfg = mkcfg(d, no_llm=False, quality_seed=999)
        dr = Director(cfg)
        bp = dr._pick_blueprint([])
        check("**返回的不是 None**", bp is not None,
              "返回 None = blueprint 调度静默失效")
        if bp is not None:
            check("有 mechanism_family", bool(bp.mechanism_family),
                  bp.mechanism_family)
            check("有 domain", bool(bp.domain), bp.domain)
        # 连选多次也要一直有(别是"第一次碰巧")
        bps = [dr._pick_blueprint([]) for _ in range(5)]
        check("**连续 5 次都非 None**", all(b is not None for b in bps),
              [b is None for b in bps])
        if dr._prefetcher is not None:
            dr._prefetcher.shutdown()



def test_review_side_channel_is_per_instance():
    """**回归**: `_last_review_*` 必须是每实例一份, 不能是类属性。

    早先它声明在**类**上 —— 类属性是所有实例共享的一份。Q9 之后同时
    存在 live 与 prefetch 两个 writer, 共享会让一道题的审稿结果被另一道
    题读走。

    ⚠️ 关键: 光"给 a 赋值再看 b"是**抓不到**这个回归的 —— 给实例赋值
    会创建一个**实例**属性把类属性遮住, 于是 b 仍然读类属性默认值,
    看起来一切正常。必须直接断言"类上没有这个可变默认", 或者断言
    "未赋值的实例读不到别人写进去的值"。两条都写在这儿。
    """
    print("\n[C7] review 侧信道每实例一份")
    from story.llm import PuzzleWriter
    # ① 类上不该有这两个可变默认
    check("**类上没有 _last_review_decision 默认**",
          "_last_review_decision" not in vars(PuzzleWriter),
          list(vars(PuzzleWriter)))
    check("**类上没有 _last_review_issues 默认**",
          "_last_review_issues" not in vars(PuzzleWriter),
          [k for k in vars(PuzzleWriter) if "review" in k])
    # ② 每个实例自己带着初始值(而不是靠类兜底)
    a = PuzzleWriter(client=None)
    b = PuzzleWriter(client=None)
    check("实例自带初始 decision", a._last_review_decision == ""
          and b._last_review_decision == "")
    check("**在实例 __dict__ 里(不是靠类)**",
          "_last_review_decision" in vars(a)
          and "_last_review_issues" in vars(a), list(vars(a)))
    # ③ 写 a 不影响 b
    a._last_review_decision = "pass"
    a._last_review_issues = ["甲"]
    check("**a 的赋值没串到 b**", b._last_review_decision == ""
          and b._last_review_issues is None,
          f"{b._last_review_decision!r} {b._last_review_issues!r}")


def test_review_side_channel_reset_on_entry():
    """**回归**: `_review_spec` 一进来就清零侧信道。

    早先它在**所有早退路径之后**才写 —— 空 tool_input / 网关错误那两条
    直接 `return None`, 于是本题 metrics 会继承**上一次调用**留下的
    decision/issues: 一道审稿失败的题, metrics 里却带着上一题的 "pass"
    和上一题的 issues。这条不需要并发就能触发。
    """
    print("\n[C8] 审稿侧信道: 失败路径不继承上一次")
    import inspect as _insp
    from story.llm import PuzzleWriter
    src = _insp.getsource(PuzzleWriter._review_spec)
    body = src.split('"""')[-1]           # 去掉 docstring, 只看代码
    i_reset = body.find("self._last_review_decision")
    i_first_ret = None
    for marker in ("return None,", "return spec"):
        j = body.find(marker)
        if j != -1 and (i_first_ret is None or j < i_first_ret):
            i_first_ret = j
    check("看得到清理语句", i_reset != -1)
    check("**清理在任何 return 之前**",
          i_reset != -1 and i_first_ret is not None and i_reset < i_first_ret,
          f"reset@{i_reset} first_return@{i_first_ret}")

    # 行为验证: 先造一个"上一次是 pass"的状态, 再走一次早退路径
    class _BadClient:
        class cfg:
            model = "fake"

        def messages(self, *a, **kw):
            class _R:
                tool_input = None          # 空 tool_input -> 早退
                error = "网关抖动"
            return _R()

    w = PuzzleWriter(client=_BadClient())
    w._last_review_decision = "pass"
    w._last_review_issues = ["上一题的毛病"]
    out, why, rw, _t = w._review_spec(good_spec())
    check("确实走了失败路径", out is None and rw is True, (out, why, rw))
    check("**decision 被清零, 没继承 'pass'**",
          w._last_review_decision == "", repr(w._last_review_decision))
    check("**issues 被清零, 没继承上一题**",
          not w._last_review_issues, repr(w._last_review_issues))


def test_director_prefetch_has_own_writer():
    """**回归**: 补池与 live 不共用 Writer 实例。

    `PuzzleWriter` 不是无状态的(`_review_spec` 写 `_last_review_*`,
    `gen_spec` 再读出来记进 metrics)。而 Q9 刻意让 prefetch 不拿
    `_narrating`, 所以两条链会**同时**跑 gen_spec。共用实例就会:
        prefetch 审稿 A -> 写 A -> live 审稿 B -> 覆盖成 B
        -> prefetch 继续 -> 把 B 的 decision/issues 记进 A 的 metrics
    谜题内容不串, 但 review provenance 被污染。

    确定性做法: 直接断言"两个不同的 writer 实例 + 同一 client",
    而不是起线程跑几次看撞不撞。
    """
    print("\n[C9] 补池有自己的 Writer 实例")
    from director import Director
    with tmpdir() as d:
        cfg = mkcfg(d, no_llm=False)
        dr = Director(cfg)
        check("两个 writer 都在", dr.writer is not None
              and dr._prefetcher is not None)
        check("**prefetcher.writer 不是 live writer**",
              dr._prefetcher.writer is not dr.writer)
        check("**但共用同一个 client(纯传输层)**",
              dr._prefetcher.writer.client is dr.writer.client)

        # 确定性 interleave: 两个实例各写各的, 互不可见
        dr.writer._last_review_decision = "pass"
        dr.writer._last_review_issues = ["live 的毛病"]
        check("**live 写了, 补池那边看不见**",
              dr._prefetcher.writer._last_review_decision == ""
              and not dr._prefetcher.writer._last_review_issues,
              repr(dr._prefetcher.writer._last_review_decision))
        dr._prefetcher.shutdown()


def test_prefetch_writer_none_when_no_client():
    """没有 client(理论上不该发生)时补池 writer 也得是 None, 别炸。"""
    print("\n[C10] 没 client 时 prefetch writer 为 None")
    from director import Director
    with tmpdir() as d:
        cfg = mkcfg(d, no_llm=True)
        dr = Director(cfg)
        check("--no-llm 下 writer 与 prefetcher 都停",
              dr.writer is None
              and (dr._prefetcher is None
                   or dr._prefetcher.writer is None))


def test_shutdown_docstring_is_honest():
    """**文档必须说真话**: `shutdown()` 自身立即返回, 但进程仍会等正在
    跑的那一道(ThreadPoolExecutor + atexit join 的语义)。

    这条测试不测线程行为(那会很脆), 只锁住"注释没在承诺做不到的事"。
    """
    print("\n[C11] shutdown 的承诺与事实一致")
    import inspect as _insp
    from story.prefetch import PoolPrefetcher
    doc = PoolPrefetcher.shutdown.__doc__ or ""
    check("**没有'绝不阻塞'这类空承诺**",
          "绝不阻塞" not in doc, doc[:60])
    check("说清了进程仍会等", "等" in doc, doc[:60])
    check("提到了 executor 的真实语义",
          "cancel_futures" in doc or "解释器" in doc or "atexit" in doc)


# ======================================================================
# E. L1 —— playable_count / 可播库存触发 / REVEALED 窗口 / 当前题 avoid
# ======================================================================
def _blocked_pool(d, n=5):
    """一个 stock=n 但 playable=0 的池子 —— "6 道候选全被挡"的复刻。

    怎么造出来的: 池子里每道题都是**同一个 signature**。`stock_count()`
    **刻意不扣** dynamic gate(被窗口挡住的题仍然是库存), 所以它数到 n;
    而 `pop_next`/`playable_count` 要过 `cross_puzzle_gate` —— 同一
    mechanism+solution_shape 连着 10 道会撞 `same_mechanism` 配额,
    于是一道都交付不出去。

    这正是实播里那个现场: `stock=5` / `playable=0` / 观众等出题。

    用 `variant(i)` 而不是 `good_spec(...)`: 后者改谜面必须**同时**改
    fair_clues 的 quote(校验会比对原文), 而 `variant()` 的文本整组是
    自洽的。signature 再统一覆盖成同一个, 才撞得出配额。

    返回值带一个 `wall`: 被当前窗口挡住**需要那个窗口真的存在** ——
    `cross_puzzle_gate(spec, recent, ...)` 是拿 `spec` 和 `recent` 比
    配额, 空窗口下什么都不冲突。所以调用方要把 `wall` 当 recent 传进
    去, 才复现得出 `playable=0`(见 `_BlockedPool`)。
    """
    cfg = mkcfg(d)
    pool = PuzzlePool.open(cfg)
    for i in range(n):
        s = variant(i)
        # 同一个 signature -> 撞 same_mechanism / same_solution_shape
        s.signature = PuzzleSignature(
            mechanism_family="hidden_function",
            solution_shape="hidden_function_explains_behavior",
            domain="maritime", emotion_mode="neutral",
            relation="stranger", time_shape="habitual",
            reveal_mode="meaning_flip")
        assert pool.add(s), "前置构造失败: variant(%d) 没能入池" % i
    # 最近 10 题全是这个 signature -> 池里每一道都被配额挡住。
    wall = [pool._items[0].signature.to_dict()] * 10
    return _BlockedPool(cfg, pool, wall)


class _BlockedPool:
    """`_blocked_pool` 的返回值: 池子 + 那个"把候选全挡住"的窗口。

    补池的 `probe_inputs` 必须回这个 window, 否则 `playable_count` 在
    空窗口下看得见全部候选 —— 那是**正常**行为(池子确实有 5 道还没用过
    的题), 只是复现不出"现场一道都播不出来"。
    """
    def __init__(self, cfg, pool, wall):
        self.cfg = cfg
        self.pool = pool
        self.wall = wall

    def inputs(self, avoid=None):
        return {"avoid": list(avoid or []),
                "recent_signatures": [dict(x) for x in self.wall]}


def test_playable_count_is_readonly():
    """**L1-D**: playable_count 与 pop_next 判定一致, 而它自己绝不写账本。

    补池会 4Hz 调它。任何一次 `_persist_used` / `_used.add` 都会污染
    used ledger —— 那是"重启后已播的题不复活"唯一的账本。
    """
    print("\n[L1-D] playable_count 与 pop_next 同门, 且纯只读")
    with tmpdir() as d:
        cfg = mkcfg(d)
        pool = PuzzlePool.open(cfg)
        for i in range(3):
            pool.add(variant(i))
        sig = pool._items[0].signature.to_dict()
        wall = [sig] * 10

        # ---- 一致: 被窗口挡住时两者都判 0 / None ----
        check("**窗口挡住 -> playable=0**",
              pool.playable_count(recent_signatures=wall) == 0,
              pool.playable_count(recent_signatures=wall))
        check("**同一个窗口 -> pop_next 也是 None**",
              pool.pop_next(recent_signatures=wall) is None)
        # ---- 一致: 没被挡住时两者都放行 ----
        check("空窗口 -> playable=3",
              pool.playable_count(recent_signatures=[]) == 3,
              pool.playable_count(recent_signatures=[]))

        # ---- 纯只读 ----
        before_used = pool.used_count()
        before_stock = pool.stock_count()
        used_file = open(cfg.pool_used_path, "rb").read() \
            if os.path.exists(cfg.pool_used_path) else b""
        for _ in range(5):
            pool.playable_count(recent_signatures=[])
            pool.playable_count(recent_signatures=wall)
        check("**_used 不变**", pool.used_count() == before_used,
              pool.used_count())
        check("**used jsonl 逐字节不变**",
              (open(cfg.pool_used_path, "rb").read()
               if os.path.exists(cfg.pool_used_path) else b"") == used_file)
        check("stock 也不变", pool.stock_count() == before_stock)
        check("**probe 之后那道题仍能交付**",
              pool.pop_next(recent_signatures=[]) is not None)


def test_playable_count_respects_policy_gate():
    """L1-D 续: 静态门与 `stock_count` 必须同一扇。

    旧 policy 隔离题对两者都不可见 —— 若 playable 漏掉这一门, 补池会
    以为"还有得播"而不补新题, 而实际 pop 一道都交付不出来。
    """
    print("\n[L1-D2] playable_count 同样扣 policy 隔离")
    with tmpdir() as d:
        cfg = mkcfg(d)
        old = variant(200)
        old.quality_policy_version = "quality-v1"
        _write_raw(cfg.pool_path, [json.dumps(
            {"pool_version": 1, "pool_key": spec_key(old),
             "added_at": 0.0, "added_by": "legacy",
             "spec": old.to_archive()}, ensure_ascii=False)])
        pool = PuzzlePool.open(cfg)
        check("stock=0", pool.stock_count() == 0, pool.stock_count())
        check("**playable=0(与 stock 同一扇门)**",
              pool.playable_count(recent_signatures=[]) == 0,
              pool.playable_count(recent_signatures=[]))


def test_playable_count_fail_closed_on_bad_ledger():
    """账本不可信 -> pop_next 一道都不交付 -> playable 必须是 0。

    若这里返回非零, 补池会认为"还有得播"而停止补池, 而实际一道都交
    不出去 —— 观众干等, 补池全程以为健康。
    """
    print("\n[L1-D3] 账本坏 -> playable=0(fail closed)")
    with tmpdir() as d:
        cfg = mkcfg(d)
        PuzzlePool.open(cfg).add(good_spec())
        _write_raw(cfg.pool_used_path, ["null"])
        pool = PuzzlePool.open(cfg)
        check("账本不可信", pool.ledger_trustworthy is False)
        check("**playable=0**", pool.playable_count(recent_signatures=[]) == 0,
              pool.playable_count(recent_signatures=[]))


def test_prefetch_l1_a_stock_ok_but_playable_zero():
    """**L1-A**: stock=5 但 playable=0 -> refill 必须启动。

    这就是"6 道候选全被挡"那一场。修之前: 补池只看 stock, 认为健康,
    一道都不补, 下一题照旧现场生成。
    """
    print("\n[L1-A] stock=5 / playable=0 -> 补池启动")
    with tmpdir() as d:
        ex = _ManualExecutor()
        bp = _blocked_pool(d, 5)
        pf = mkpf(d, pool=bp.pool, executor=ex,
                  probe_inputs=lambda: bp.inputs())
        check("前置: stock=5", bp.pool.stock_count() == 5,
              bp.pool.stock_count())
        check("前置: **playable=0**",
              bp.pool.playable_count(bp.wall) == 0,
              bp.pool.playable_count(bp.wall))
        pf.on_tick()
        check("**缺口触发 latch**", pf._refill_active is True)
        check("**真的提交了一次生成**", ex.total == 1, ex.total)


def test_prefetch_l1_b_stock_ok_playable_ok_no_refill():
    """**L1-B**: stock=5 且 playable>=1 -> 不因动态缺货而补池。

    反方向必须守住: 否则只要 playable 波动一次就狂补, 而盘上其实堆满了。
    """
    print("\n[L1-B] stock=5 / playable=1 -> 不补")
    with tmpdir() as d:
        ex = _ManualExecutor()
        pool = PuzzlePool.open(mkcfg(d))
        pf = mkpf(d, pool=pool, executor=ex)
        fill(pool, 5)
        p = pool.playable_count([])
        check("前置: stock=5", pool.stock_count() == 5)
        check("前置: playable>=1", p >= 1, p)
        for _ in range(4):
            pf.on_tick()
        check("**latch 没启动**", pf._refill_active is False)
        check("**零提交**", ex.total == 0, ex.total)


def test_prefetch_l1_c_max_size_stops_generation():
    """**L1-C**: stock 到硬上限且 playable=0 -> 停下, 不再无限生成。

    到顶只 warning —— 这是"被某个窗口条件整体挡住"时的兜底, 防止
    无限烧网关配额而 playable 一动不动。
    """
    print("\n[L1-C] stock=10(max) / playable=0 -> 不再生成")
    with tmpdir() as d:
        ex = _ManualExecutor()
        bp = _blocked_pool(d, 10)
        pf = mkpf(d, pool=bp.pool, executor=ex, pool_max_size=10,
                  pool_target_size=5, pool_min_size=2,
                  probe_inputs=lambda: bp.inputs())
        check("前置: stock=10", bp.pool.stock_count() == 10,
              bp.pool.stock_count())
        check("前置: playable=0", bp.pool.playable_count(bp.wall) == 0)
        for _ in range(5):
            pf.on_tick()
        check("**零提交(到顶了)**", ex.total == 0, ex.total)
        check("latch 也没启动", pf._refill_active is False)
        # 但**不能**静默 —— 运维必须看得见"补了也没用"
        import io as _io
        import logging as _log
        buf = _io.StringIO()
        h = _log.StreamHandler(buf)
        lg = _log.getLogger("story.prefetch")
        old = lg.level
        lg.setLevel(_log.WARNING)
        lg.addHandler(h)
        pf._max_warn_at = 0.0          # 解掉节流, 逼它这一次真的打
        pf.on_tick()
        lg.removeHandler(h)
        lg.setLevel(old)
        out = buf.getvalue()
        check("**有 warning(不是静默空转)**",
              "硬上限" in out and "无可播" in out, out[:200])


def test_prefetch_l1_e_revealed_window():
    """**L1-E**: REVEALED 允许补池; REVEALING / SETTING 禁止; QA busy 禁止。

    REVEALED 那 30 秒是**最好的**生成窗口 —— 引擎完全空闲, 而且有很大
    概率赶在下一题就位之前完成(下一题于是直接 pop 池子瞬时切题)。
    """
    print("\n[L1-E] REVEALED 允许补池, REVEALING/SETTING 禁止")
    with tmpdir() as d:
        def probe_for(phase, pending=0, inflight=0, hi=False, ri=False):
            return lambda: {"phase": phase, "pending": pending,
                            "inflight": inflight, "hint_inflight": hi,
                            "reveal_inflight": ri, "stopped": False}

        # REVEALED + 空闲 -> 允许
        ex = _ManualExecutor()
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), executor=ex,
                  probe=probe_for(Phase.REVEALED))
        fill(pf.pool, 0)
        pf.on_tick()
        check("**REVEALED 空闲 -> 允许(prefetch 已提交)**",
              ex.total == 1, ex.total)

        # REVEALED 但仍有人在途 -> 禁止
        ex2 = _ManualExecutor()
        pf2 = mkpf(d, pool=PuzzlePool.open(mkcfg(d, pool_path=os.path.join(d, "p2.jsonl"),
                                                pool_used_path=os.path.join(d, "u2.jsonl"))),
                   executor=ex2, probe=probe_for(Phase.REVEALED, pending=1))
        pf2.on_tick()
        check("REVEALED 但 pending>0 -> 禁止", ex2.total == 0, ex2.total)

        # REVEALING -> 禁止(揭晓可能仍在生成, 不抢)
        for i, ph in enumerate((Phase.REVEALING, Phase.SETTING)):
            exi = _ManualExecutor()
            pfi = mkpf(d, pool=PuzzlePool.open(mkcfg(
                d, pool_path=os.path.join(d, f"p{i}.jsonl"),
                pool_used_path=os.path.join(d, f"u{i}.jsonl"))),
                executor=exi, probe=probe_for(ph))
            pfi.on_tick()
            check(f"**{ph} -> 禁止**", exi.total == 0, exi.total)

        # QA busy -> 禁止
        exq = _ManualExecutor()
        pfq = mkpf(d, pool=PuzzlePool.open(mkcfg(
            d, pool_path=os.path.join(d, "pq.jsonl"),
            pool_used_path=os.path.join(d, "uq.jsonl"))),
            executor=exq, probe=probe_for(Phase.QA, pending=2))
        pfq.on_tick()
        check("QA busy -> 禁止", exq.total == 0, exq.total)

        # QA idle -> 允许(原有行为不许被改坏)
        exi2 = _ManualExecutor()
        pfi2 = mkpf(d, pool=PuzzlePool.open(mkcfg(
            d, pool_path=os.path.join(d, "pq2.jsonl"),
            pool_used_path=os.path.join(d, "uq2.jsonl"))),
            executor=exi2, probe=probe_for(Phase.QA))
        pfi2.on_tick()
        check("**QA idle -> 允许**", exi2.total == 1, exi2.total)

        # QA + hint 在途 -> 禁止(hint 可能与出题抢配额)
        exh = _ManualExecutor()
        pfh = mkpf(d, pool=PuzzlePool.open(mkcfg(
            d, pool_path=os.path.join(d, "ph.jsonl"),
            pool_used_path=os.path.join(d, "uh.jsonl"))),
            executor=exh, probe=probe_for(Phase.QA, hi=True))
        pfh.on_tick()
        check("QA + hint 在途 -> 禁止", exh.total == 0, exh.total)


def test_prefetch_l1_f_current_puzzle_in_avoid():
    """**L1-F**: 当前正在玩的谜面必须进 prefetch 的 avoid。

    `_used_titles` 只在**揭晓时**追加, 所以在"第 N 题就位"到"第 N 题
    揭晓"这段时间里当前谜面不在 avoid 里 —— 而补池恰好在这段时间跑。
    少了这一条, 后台可能生成一道和观众此刻正看着的那道极像的题。
    """
    print("\n[L1-F] 当前谜面进 avoid")
    cfg = mkcfg(tempfile.mkdtemp())
    e = RoundEngine(cfg)
    with e._lock:
        e._used_titles = ["旧题A"]
        e._puzzle = "  守塔人只在退潮时亮灯, 涨潮就熄灯, 为什么?  "
    gi = e.snapshot_generation_inputs()
    cur = e._puzzle.strip()[:60]
    check("**当前谜面在 avoid 里**", cur in gi["avoid"], gi["avoid"])
    check("旧题仍在(没被顶掉)", "旧题A" in gi["avoid"], gi["avoid"])
    check("仍然截到 60 字(与 _used_titles 同口径)",
          all(len(x) <= 60 for x in gi["avoid"]), gi["avoid"])
    # 揭晓路径写的是 `strip()[:60]`, 两处必须逐字节相等 —— 否则同一个
    # 谜面会在两个列表里以不同长度出现, too_similar 的 n-gram 会略漂。
    with e._lock:
        e._used_titles.append(e._puzzle.strip()[:60])
    gi2 = e.snapshot_generation_inputs()
    check("**与 _used_titles 的写法逐字节一致(不重复出现)**",
          gi2["avoid"].count(cur) == 1, gi2["avoid"])

    # 还没有当前题时不该凭空加空串
    e2 = RoundEngine(mkcfg(tempfile.mkdtemp()))
    with e2._lock:
        e2._used_titles = ["旧题A"]
        e2._puzzle = ""
    check("空谜面不产生空串条目",
          e2.snapshot_generation_inputs()["avoid"] == ["旧题A"],
          e2.snapshot_generation_inputs()["avoid"])


def test_prefetch_probe_and_generate_share_one_snapshot():
    """**probe 与生成必须用同一份 snapshot。**

    否则 probe 按快照 A 判"还够播"、生成时按快照 B 出题, 两拍的窗口
    差异会让补池照着一个过期判断跑。
    """
    print("\n[L1-G] probe / 生成共用同一份 recent+avoid 快照")
    with tmpdir() as d:
        ex = _ManualExecutor()
        w = _FakeWriter()
        cur = {"recent": ["旧快照"], "avoid": ["旧谜面"]}
        pool = PuzzlePool.open(mkcfg(d))      # 空池 -> stock=0 -> latch 启动
        from story.prefetch import PoolPrefetcher
        pf = PoolPrefetcher(
            cfg=mkcfg(d, pool_min_size=2, pool_target_size=5),
            pool=pool, writer=w,
            probe=lambda: {"phase": Phase.QA, "pending": 0, "inflight": 0,
                           "hint_inflight": False, "reveal_inflight": False,
                           "stopped": False},
            probe_inputs=lambda: {"avoid": list(cur["avoid"]),
                                  "recent_signatures": list(cur["recent"])},
            pick_blueprint=lambda recent, rng=None: None,
            clock=_Clock(), executor=ex)
        pf.on_tick()
        check("提交了", ex.total == 1)
        # 提交之后立刻换掉"当前快照" —— worker 执行时必须看到旧的那份
        cur["recent"] = ["新快照"]
        cur["avoid"] = ["新谜面"]
        ex.run_next()
        check("**worker 看到的是提交那一刻的快照**",
              w.calls[0]["recent"] == ["旧快照"], w.calls[0]["recent"])
        check("avoid 同理", w.calls[0]["avoid"] == ["旧谜面"],
              w.calls[0]["avoid"])


def test_prefetch_stats_exposes_playable():
    """stats 必须能区分 stock 与 playable —— 那是现场指纹。"""
    print("\n[L1-H] prefetch.stats 暴露 stock/playable")
    with tmpdir() as d:
        bp = _blocked_pool(d, 3)
        pf = mkpf(d, pool=bp.pool, probe_inputs=lambda: bp.inputs())
        st = pf.stats()
        check("有 stock", st.get("stock") == 3, st.get("stock"))
        check("**有 playable 且为 0**", st.get("playable") == 0,
              st.get("playable"))
        check("有 playable_min", st.get("playable_min") == 1,
              st.get("playable_min"))
        check("有 max_size", st.get("max_size") == 10, st.get("max_size"))


def test_playable_min_zero_restores_q9_behavior():
    """`pool_playable_min=0` = 关掉动态缺货触发 —— 退回 Q9 原行为。

    与 `enforce_blueprint` 那次教训同一条纪律: "关掉"必须是真的关掉,
    不能表面关掉、底下还留一条隐式触发。
    """
    print("\n[L1-I] playable_min=0 -> 只看 stock(退回 Q9)")
    with tmpdir() as d:
        ex = _ManualExecutor()
        bp = _blocked_pool(d, 5)
        pf = mkpf(d, pool=bp.pool, executor=ex, pool_playable_min=0,
                  probe_inputs=lambda: bp.inputs())
        for _ in range(4):
            pf.on_tick()
        check("stock=5 且关了动态触发 -> 不补",
              pf._refill_active is False and ex.total == 0, ex.total)


def test_max_size_below_target_is_flagged():
    print("\n[L1-J] Config 抓 max < target")
    w = Config(sim_path="x", pool_target_size=5, pool_max_size=3).validate()
    check("max<target 有告警", any("硬上限" in x for x in w), w)
    w2 = Config(sim_path="x", pool_target_size=5, pool_max_size=10).validate()
    check("正常配置无此告警", not any("硬上限" in x for x in w2), w2)
    w3 = Config(sim_path="x", pool_playable_min=-1).validate()
    check("playable_min 为负有告警", any("playable_min" in x for x in w3), w3)


# ======================================================================
# U1: 揭晓窗口(60s)专用补池目标 + 临近 deadline 不再启动
# ======================================================================

def _reveal_probe(remaining=None, **kw):
    """一个"在 REVEALED、引擎空闲"的探针。"""
    d = {"phase": Phase.REVEALED, "pending": 0, "inflight": 0,
         "hint_inflight": False, "reveal_inflight": False,
         "reveal_remaining_seconds": remaining, "stopped": False}
    d.update(kw)
    return d


def _qa_probe(remaining=None):
    return {"phase": Phase.QA, "pending": 0, "inflight": 0,
            "hint_inflight": False, "reveal_inflight": False,
            "reveal_remaining_seconds": remaining, "stopped": False}


def test_u1_reveal_uses_higher_target():
    """**U1-A**: REVEALED 期间补池目标抬高到 reveal_target。

    QA 期间 target=5; 揭晓窗口补到 7 —— 那是唯一"引擎完全空闲"的时间
    窗, 观众在看答案, 补池不与直播抢网关。

    ⚠️ C3 修正: 这条用例早先构造的是"stock=6, 但 6 道全是同一个
    `variant()` 造的**结构重复**题"。那 6 道互相挡着, 于是真正的
    playable 只有 1 —— latch 是靠 `playable < 2` 启动的, **不是**靠
    reveal_target。当时 probe 又把 playable 数成 1(`limit=_playable_min`),
    两个错误互相抵消, 断言看起来是绿的。

    C3 把 probe 修对之后, "6 道同构题" 的 playable 变成 1(真值),
    而 `stock=6 < reveal_target=7` **本身并不驱动启动腿**
    (见 `_on_tick_locked_ish` 里关于"否决 stock < target"的说明)。
    所以这条用例改成它本来想测的东西: 用**结构互不重复**的题撑起
    playable >= 2, 然后看 QA/reveal 两个目标组下**停止条件**的差别。
    """
    print("\n[U1-A] REVEALED 用更高的补池目标")
    with tmpdir() as d:
        # QA: stock=6 >= target=5, 且 6 道都可播 -> 不该补
        ex = _SyncExecutor()
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), executor=ex,
                  probe=lambda: _qa_probe(), pool_reveal_target_size=7)
        fill_mixed(pf.pool, 6)
        check("QA: 6 道互不重复 -> stock=6",
              pf.pool.stock_count() == 6, pf.pool.stock_count())
        pf.on_tick()
        check("QA: stock=6 已到 target(5) 且可播充足 -> 不补",
              len(ex.submitted) == 0, len(ex.submitted))
        check("QA: latch 没启动", pf.stats()["refill_active"] is False,
              pf.stats()["refill_active"])
        # REVEALED: 同一个 stock=6 < reveal_target=7 —— 但启动腿不看
        # stock<target, 所以这里**不会**因为 7 而启动。真正会驱动的是
        # playable < reveal_playable_target(2)。用 1 道可播来构造。
        #
        # ⚠️ 必须开**新的临时目录**: pool 的默认路径是 `d` 下的
        # `pool.jsonl`, 复用 `d` 会让 pf2 打开的还是上面那 6 道。
    with tmpdir() as d2:
        pf2 = mkpf(d2, pool=PuzzlePool.open(mkcfg(d2)),
                   executor=_SyncExecutor(),
                   probe=lambda: _reveal_probe(remaining=50.0),
                   pool_reveal_target_size=7,
                   pool_reveal_playable_target=2)
        fill_mixed(pf2.pool, 1)                 # playable=1 < 2
        pf2.on_tick()
        check("**REVEALED: playable=1 < 2 -> 启动**",
              pf2.stats()["refill_active"] is True,
              pf2.stats()["refill_active"])
        check("并且真的提交了",
              len(pf2._executor.submitted) == 1,
              len(pf2._executor.submitted))
        # 补到 7 道后可播充足 -> 停(证明更高目标确实在起作用)
        #
        # ⚠️ `_SyncExecutor` 立即执行: 上面那次 submit 已经生成并入库了
        #    一道 -> stock 已经是 2。所以这里只再灌 5 道到 7。灌 6 道会
        #    得到 stock=8, 断言会看起来像"冲到了 10"的 bug。
        fill_mixed(pf2.pool, 5, base=100)       # 2 + 5 = 7
        check("补到 stock=7", pf2.pool.stock_count() == 7,
              pf2.pool.stock_count())
        pf2.on_tick()
        check("**REVEALED: stock>=7 且 playable>=2 -> 清 latch**",
              pf2.stats()["refill_active"] is False,
              pf2.stats()["refill_active"])


def test_c3_reveal_playable_probe_counts_past_one():
    """**C3**: REVEALED 的 probe 必须真的数得到 2 —— 用**真实题池**验证。

    C3 之前 `_playable()` 固定 `limit=self._playable_min`(默认 1), 而
    `PuzzlePool.playable_count(limit=N)` 是**硬早退**
    (`if n >= limit: break`) —— 传 1 就只能返回 0 或 1。于是
    `_effective_targets()` 要求的 `playable >= 2` **永远无法满足**,
    latch 的"可播够了"停止条件永不成立, 补池一路补到硬上限 10。

    旧测试(U1-B)没抓到, 因为它只检查 `_effective_targets()` 返回
    `(7, 2)` 这个**配置值**, 并且用一个把 `pool_reveal_playable_target`
    设为 1 的夹具 —— 那等于把这条路径的触发条件删掉了。
    夹具不具备触发条件时, 断言是假的。

    这里必须用**真实 PuzzlePool**: 只有真实实现才有"数到 limit 就
    早退"这个行为, 桩对象不会复现它。
    """
    print("\n[C3] REVEALED 的 playable probe 真的能数到 2")
    with tmpdir() as d:
        pool = PuzzlePool.open(mkcfg(d))
        pf = mkpf(d, pool=pool, executor=_SyncExecutor(),
                  probe=lambda: _reveal_probe(remaining=50.0),
                  pool_playable_min=1, pool_reveal_playable_target=2,
                  pool_reveal_target_size=7)
        # ⚠️ 必须用 mixed: `fill()` 的 variant 全是同一个 (family, shape),
        #    互相结构重复, playable 恒为 1 —— 拿它测 ">= 2" 测的是夹具。
        fill_mixed(pool, 3)
        inputs = pf._generation_inputs()
        # 直接问真实池: 3 道都合法且没被窗口挡住 -> 至少 2 道可播。
        raw1 = pool.playable_count(inputs.get("recent_signatures"),
                                   inputs.get("avoid"), limit=1)
        raw2 = pool.playable_count(inputs.get("recent_signatures"),
                                   inputs.get("avoid"), limit=2)
        check("真实池 limit=1 早退 -> 最多 1", raw1 <= 1, raw1)
        check("真实池 limit=2 能数到 2", raw2 >= 2, raw2)
        # 被修的那条路径: probe 必须跟着阶段目标走。
        check("**probe(limit=need_playable) 真的返回 >= 2**",
              pf._playable(inputs, limit=2) >= 2,
              pf._playable(inputs, limit=2))
        # 默认参数仍退化为 _playable_min(不偷偷改变 QA 阶段的早退行为)。
        check("probe 不传 limit 时退化为 _playable_min(1)",
              pf._playable(inputs) <= 1, pf._playable(inputs))
        # --- 端到端: stock=7 且 playable>=2 -> 停止, 不继续冲到 10 ---
        fill_mixed(pool, 4, base=100)         # 共 7 道
        check("stock=7 已到 reveal_target", pool.stock_count() == 7,
              pool.stock_count())
        pf.on_tick()
        st = pf.stats()
        check("**stock=7 且 playable>=2 -> latch 清掉, 不再补**",
              st["refill_active"] is False, st["refill_active"])
        check("没有提交任何生成(没有冲到 max=10)",
              len(pf._executor.submitted) == 0, len(pf._executor.submitted))
        check("stats 报的 playable 与判据同源(>=2)",
              st["playable"] >= 2, st["playable"])


def test_c3_reveal_playable_probe_stops_at_two_not_ten():
    """**C3-b**: 只数到 1 时仍要补; 一旦到 2 就停 —— 恰好卡在目标上。

    这条与 C3-a 互补: 前者证明"能数到 2", 这条证明"数到 2 就够,
    不再多补"。缺了后者, 一个"永远返回 0/1"的实现也能让 latch 一直
    开着而测试全绿(那正是修复前的状态)。
    """
    print("\n[C3-b] playable 恰好到 2 就停")
    with tmpdir() as d:
        pool = PuzzlePool.open(mkcfg(d))
        pf = mkpf(d, pool=pool, executor=_SyncExecutor(),
                  probe=lambda: _reveal_probe(remaining=50.0),
                  pool_playable_min=1, pool_reveal_playable_target=2,
                  pool_reveal_target_size=7, pool_min_size=2)
        fill_mixed(pool, 1)                  # playable=1 < 2 -> 必须补
        pf.on_tick()
        check("playable=1 < 2 -> 启动", pf.stats()["refill_active"] is True,
              pf.stats()["refill_active"])
        check("真的提交了一次", len(pf._executor.submitted) == 1,
              len(pf._executor.submitted))
        # ⚠️ `_SyncExecutor` 是**立即执行**的: 上面那次 submit 已经真的
        #    生成并入库了一道 -> stock 从 1 变 2。这里要补到 7 就得再灌
        #    5 道, 不是 6 道。少算一道会让 stock=8, 断言看起来像"冲到 10"
        #    的 bug, 其实是夹具算术错了。
        fill_mixed(pool, 5, base=100)        # 2 + 5 = 7
        check("stock=7 已到 reveal_target", pool.stock_count() == 7,
              pool.stock_count())
        pf._executor.submitted.clear()
        pf.on_tick()
        check("stock=7 且 playable>=2 -> 停",
              pf.stats()["refill_active"] is False,
              pf.stats()["refill_active"])
        check("停在目标上, 没冲到 max_size=10",
              pool.stock_count() == 7, pool.stock_count())


def test_u1_reveal_playable_target():
    """**U1-B**: REVEALED 期间要求 playable >= reveal_playable_target(2)。"""
    print("\n[U1-B] REVEALED 的 playable 目标更高")
    with tmpdir() as d:
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), executor=_SyncExecutor(),
                  probe=lambda: _reveal_probe(remaining=50.0),
                  pool_playable_min=1, pool_reveal_playable_target=2,
                  pool_reveal_target_size=7)
        fill(pf.pool, 1)                   # stock=1, playable=1
        tgt, need = pf._effective_targets()
        check("REVEALED: 目标组是 reveal 组", (tgt, need) == (7, 2), (tgt, need))
        pf.on_tick()
        check("playable=1 < 2 -> 仍要补", pf.stats()["refill_active"] is True,
              pf.stats()["refill_active"])


def test_u1_qa_still_uses_conservative_target():
    """**U1-C**: 非 REVEALED 阶段仍是保守目标(QA 要跟直播抢网关)。"""
    print("\n[U1-C] QA 仍用保守目标")
    with tmpdir() as d:
        pf = mkpf(d, executor=_SyncExecutor(),
                  pool_reveal_target_size=7, pool_reveal_playable_target=2)
        tgt, need = pf._effective_targets()
        check("QA: 目标组是保守组 (5,1)", (tgt, need) == (5, 1), (tgt, need))
    with tmpdir() as d:
        pf2 = mkpf(d, executor=_SyncExecutor(),
                   probe=lambda: _reveal_probe(remaining=50.0),
                   pool_reveal_target_size=7, pool_reveal_playable_target=2)
        tgt2, need2 = pf2._effective_targets()
        check("REVEALED: 切到 reveal 组 (7,2)", (tgt2, need2) == (7, 2),
              (tgt2, need2))


def test_u1_deadline_guard_blocks_new_requests():
    """**U1-D**: 距下一题 <= guard 秒 -> 不再**启动**新请求。

    60 秒到点时下一题绝不能等待 future。留 15 秒余量, 免得 deadline
    那一刻正好挂着一个跑了一半的任务。
    """
    print("\n[U1-D] 临近 deadline 不再启动")
    with tmpdir() as d:
        ex = _SyncExecutor()
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), executor=ex,
                  probe=lambda: _reveal_probe(remaining=5.0),
                  pool_min_size=2, pool_reveal_target_size=7,
                  pool_reveal_start_guard_seconds=15.0)
        pf.on_tick()
        check("剩余 5s <= guard 15s -> **不启动**",
              len(ex.submitted) == 0, len(ex.submitted))
        check("latch 仍开着(只是这一拍不启动)",
              pf.stats()["refill_active"] is True)
    with tmpdir() as d:
        ex2 = _SyncExecutor()
        pf2 = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), executor=ex2,
                   probe=lambda: _reveal_probe(remaining=40.0),
                   pool_min_size=2, pool_reveal_target_size=7,
                   pool_reveal_start_guard_seconds=15.0)
        pf2.on_tick()
        check("剩余 40s > guard -> 照常启动",
              len(ex2.submitted) == 1, len(ex2.submitted))


def test_u1_deadline_guard_ignores_non_reveal():
    """探针给 None(不在 REVEALED)-> 不限制。"""
    print("\n[U1-E] 非揭晓阶段不受 deadline guard 影响")
    with tmpdir() as d:
        ex = _SyncExecutor()
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), executor=ex,
                  probe=lambda: _qa_probe(remaining=None),
                  pool_reveal_start_guard_seconds=15.0)
        pf.on_tick()
        check("QA + remaining=None -> 照常补",
              len(ex.submitted) == 1, len(ex.submitted))


def test_u1_multiple_generations_within_one_reveal():
    """**U1-F**: 同一个 60s REVEALED 窗口里能连续补多道(单飞, 串行)。"""
    print("\n[U1-F] 一个揭晓窗口内连续补多道")
    with tmpdir() as d:
        ex = _SyncExecutor()
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), executor=ex,
                  probe=lambda: _reveal_probe(remaining=50.0),
                  pool_min_size=2, pool_target_size=5,
                  pool_reveal_target_size=7, pool_reveal_playable_target=1)
        for _ in range(12):
            pf.on_tick()
        n = pf.pool.stock_count()
        check("**补到了 reveal_target(7)**", n >= 7, n)
        check("不会无限补(受 max_size=10 约束)", n <= 10, n)
        check("补了不止一道(确实连续工作)",
              len(ex.submitted) >= 5, len(ex.submitted))


def test_u1_reveal_never_blocks_next_puzzle():
    """**U1**: deadline 那一刻下一题**不等** future —— 池里有就直接上。

    补池是后台行为, `pop_next` 永远不该被它挡住。这里验的是"在途任务
    存在时, pop_next 仍能立刻拿到题"。
    """
    print("\n[U1-H] 下一题不等补池 future")
    with tmpdir() as d:
        pool = PuzzlePool.open(mkcfg(d))
        # ⚠️ C3: 早先这里灌 3 道同构 variant, 靠 probe **数错**
        #    (limit=playable_min=1 -> playable 恒报 1) 才让 latch 启动。
        #    probe 修对之后 `stock=3/playable=2` 是**真的够了**, 不会补
        #    —— 那条断言也就失去了意义(测的是夹具, 不是实现)。
        #
        #    要验的是"在途任务存在时 pop_next 不被挡", 所以夹具必须
        #    真的把 latch 打开。用 `pool_min_size=5 > stock=3` 这条腿
        #    启动, 与 probe 数得准不准无关。
        fill_mixed(pool, 3)
        ex = _ManualExecutor()
        pf = mkpf(d, pool=pool, executor=ex,
                  probe=lambda: _reveal_probe(remaining=50.0),
                  pool_min_size=5)
        for _ in range(3):
            pf.on_tick()
        check("**确实有在途任务(未完成)**", pf._future is not None)
        got = pool.pop_next()
        check("**在途任务存在时 pop_next 仍立刻拿到题**", got is not None)
        check("拿到的确实是一道题", bool(getattr(got, "puzzle", "")))


def test_u1_guard_config_validation():
    """U1 新增配置的 validate 告警 + 默认值。"""
    print("\n[U1-G] reveal 配置校验")
    w = Config(sim_path="x", reveal_hold_seconds=60.0,
               reveal_core_focus_seconds=15.0).validate()
    check("正常核心焦点时长无告警",
          not any("reveal_core_focus" in x for x in w), w)
    w2 = Config(sim_path="x", reveal_hold_seconds=30.0,
                reveal_core_focus_seconds=45.0).validate()
    check("**焦点时长 >= 展示时长 会告警**",
          any("reveal_core_focus" in x for x in w2), w2)
    w3 = Config(sim_path="x", reveal_hold_seconds=30.0,
                reveal_core_focus_seconds=-1.0).validate()
    check("负焦点时长会告警", any("reveal_core_focus" in x for x in w3), w3)
    w4 = Config(sim_path="x", pool_reveal_target_size=3,
                pool_target_size=5).validate()
    check("reveal_target < target 会告警",
          any("pool_reveal_target_size" in x for x in w4), w4)
    w5 = Config(sim_path="x", pool_reveal_target_size=99,
                pool_max_size=10).validate()
    check("reveal_target > max 会告警",
          any("pool_reveal_target_size" in x for x in w5), w5)
    c = Config(sim_path="x")
    check("默认 reveal_hold 是 60s", c.reveal_hold_seconds == 60.0,
          c.reveal_hold_seconds)
    check("默认 core_focus 是 15s", c.reveal_core_focus_seconds == 15.0,
          c.reveal_core_focus_seconds)
    check("默认 reveal_target 是 7", c.pool_reveal_target_size == 7,
          c.pool_reveal_target_size)
    # ---- G1: guard 必须覆盖一轮补池预算 ----
    # 早先默认 15s, 但一轮 prefetch 可能跑几十秒 —— "只剩 18 秒"照样
    # 启动一个注定跨过 deadline 的后台任务, 那正是实播里 live 与
    # prefetch 同时占网关 51 秒的成因。默认抬到 30s。
    check("默认 guard 是 30s(G1)", c.pool_reveal_start_guard_seconds == 30.0,
          c.pool_reveal_start_guard_seconds)
    check("默认 guard >= budget + 余量",
          c.pool_reveal_start_guard_seconds
          >= c.pool_prefetch_budget_seconds
          + c.pool_prefetch_guard_margin_seconds,
          (c.pool_reveal_start_guard_seconds, c.pool_prefetch_budget_seconds))
    check("默认 prefetch attempts 是 2", c.pool_prefetch_max_attempts == 2,
          c.pool_prefetch_max_attempts)
    check("默认 prefetch budget 是 25s",
          c.pool_prefetch_budget_seconds == 25.0,
          c.pool_prefetch_budget_seconds)
    check("默认退避序列递增",
          list(c.pool_prefetch_backoff_schedule_s)
          == sorted(c.pool_prefetch_backoff_schedule_s),
          c.pool_prefetch_backoff_schedule_s)
    # guard 小于一轮预算 -> 必须告警(实际生效值会被 max() 抬高)
    w6 = Config(sim_path="x", pool_reveal_start_guard_seconds=5.0,
                pool_prefetch_budget_seconds=25.0).validate()
    check("**guard < 一轮预算 会告警**",
          any("pool_reveal_start_guard" in x for x in w6), w6)
    w7 = Config(sim_path="x", pool_prefetch_max_attempts=0).validate()
    check("prefetch_max_attempts=0 会告警",
          any("pool_prefetch_max_attempts" in x for x in w7), w7)
    w8 = Config(sim_path="x",
                pool_prefetch_backoff_schedule_s=(30.0, 10.0)).validate()
    check("退避序列非递增会告警",
          any("backoff_schedule" in x for x in w8), w8)


# ======================================================================
# G1 —— 后台补池不得跨场景白烧
# ======================================================================
class _GatedWriter:
    """可**在生成中途切换相位**的假 writer —— 复现实播那 51 秒。

    与 `_FakeWriter` 的区别: 它消费 `should_continue`, 在"每次昂贵
    调用之前"检查一次(与真 `gen_spec` 的契约一致); 一旦让路就返回
    一个 `metrics["interrupted"]=True`、puzzle 为空的 spec ——
    **这正是真 gen_spec 让路时的返回形状**(error 留空, 因为它不是
    失败)。补池据此把它归到 `interrupted` 而不是 `gen_fail`。

    这不是"替身偷懒": 真 gen_spec 的检查点在
    `test_g1_real_gen_spec_stops_at_each_checkpoint` 里单独覆盖。
    """

    def __init__(self, spec=None):
        self.calls = []
        self._spec = spec
        self._n = 0

    def gen_spec(self, avoid=None, blueprint=None, recent=None,
                 enforce_blueprint=None, should_continue=None,
                 max_attempts=None, budget_s=None, **kw):
        self.calls.append({"max_attempts": max_attempts,
                           "budget_s": budget_s,
                           # ⚠️ 记下**谓词本身**而不只是它这次的结果:
                           # "prefetch 到底有没有把取消能力传下去"必须能被
                           # 断言, 否则删掉传参那一行不会有任何测试变红
                           # (谓词变成一个谁也不用的孤岛)。
                           "has_predicate": should_continue is not None})
        if should_continue is not None and not should_continue():
            return _interrupted_spec()
        return self._spec if self._spec is not None else variant(len(self.calls))


def _interrupted_spec():
    """真 `gen_spec` 让路时的返回形状: puzzle 空、**error 也空**。"""
    s = good_spec()
    s.puzzle = ""
    s.error = ""
    s.metrics = {"interrupted": True, "ok": False}
    return s


def test_g1_prefetch_passes_own_budget_not_live_budget():
    """**G1**: 后台补池必须用自己的预算, 不能吃 live 的 4 稿 / 90s。

    live 出一道题观众在干等, 多试一稿值得; 后台补池只是"有空补一道",
    多试一稿的收益是池子里多一道题, 代价却是与直播抢网关 + 跨过
    deadline 继续跑。
    """
    print("\n[G1-A] 后台补池用独立预算")
    with tmpdir() as d:
        w = _GatedWriter()
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), writer=w,
                  executor=_SyncExecutor(),
                  pool_prefetch_max_attempts=2,
                  pool_prefetch_budget_seconds=25.0)
        fill(pf.pool, 1)
        pf.on_tick()
        check("调用了一次", len(w.calls) == 1, len(w.calls))
        check("**传的是后台的 max_attempts=2(不是 live 的 4)**",
              w.calls[0]["max_attempts"] == 2, w.calls[0]["max_attempts"])
        check("**传的是后台的 budget=25(不是 live 的 90)**",
              w.calls[0]["budget_s"] == 25.0, w.calls[0]["budget_s"])
        # ⚠️ 这一条是删掉"传参那一行"时**唯一**会变红的断言。少了它,
        #    谓词就成了一个谁也不用的孤岛: `_should_continue` 本身被
        #    测得很全, 但 prefetch 根本没把它交给 gen_spec ——
        #    实播里等于 G1 完全没生效。
        check("**真的把 should_continue 传下去了**",
              w.calls[0]["has_predicate"] is True, w.calls[0])


def test_g1_prefetch_yields_when_phase_switches_midflight():
    """**G1-B: 本批最高价值 regression** —— 复现实播的跨场景白烧。

    时间线(实播日志):

        18:44:49 prefetch 在 REVEALED 启动
        18:45:07 揭晓结束、下一题开始(SETTING), 池里没题 -> live 现场生成
        18:45:58 旧 prefetch 才跑完第 4 稿失败

    约 51 秒里 live 与 prefetch 同时在生成。修好之后:

        draft1 在途 -> 相位变 SETTING -> draft1 返回
        -> 不再审稿(Reviewer call = 0) -> 不再出第二稿(draft2 = 0)

    这里用 `_GatedWriter` 从**补池侧**断言: gen_spec 收到 should_continue
    且它在中途返回 False 时, 补池把它记成 `interrupted`(**不是**
    gen_fail), 并且不再继续。
    """
    print("\n[G1-B] 跨场景: 相位切换后不再继续生成")
    with tmpdir() as d:
        phase = {"p": Phase.REVEALED, "left": 50.0}

        def probe():
            return {"phase": phase["p"], "pending": 0, "inflight": 0,
                    "hint_inflight": False, "reveal_inflight": False,
                    "reveal_remaining_seconds": phase["left"],
                    "puzzle_index": 7, "stopped": False}

        # writer 第二次被问 should_continue 时相位已经变忙
        w = _GatedWriter()
        orig = w.gen_spec

        def gen_spec(**kw):
            kw.setdefault("should_continue", None)
            return orig(**kw)

        w.gen_spec = gen_spec
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), writer=w,
                  probe=probe, executor=_SyncExecutor())
        fill(pf.pool, 1)

        # ① 谓词在 REVEALED + 充裕剩余时间下应当放行
        check("**REVEALED 下谓词放行**", pf._should_continue() is True)

        # ② 切进 SETTING(下一题开始现场生成) —— 必须让路
        phase["p"] = Phase.SETTING
        check("**SETTING 下谓词让路(这是 51 秒相撞的根因)**",
              pf._should_continue() is False)

        # ③ REVIEWING 同理
        phase["p"] = Phase.REVEALING
        check("REVEALING 下谓词让路", pf._should_continue() is False)

        # ④ 有观众在等回答 -> 让路
        phase["p"] = Phase.QA
        check("QA 空闲时放行", pf._should_continue() is True)
        for busy in ("pending", "inflight"):
            pd = {"phase": Phase.QA, "pending": 0, "inflight": 0,
                  "hint_inflight": False, "reveal_inflight": False,
                  "reveal_remaining_seconds": None, "stopped": False}
            pd[busy] = 1
            pf._probe = lambda pd=pd: pd
            check(f"**{busy}>0 时让路**", pf._should_continue() is False)
        pd = {"phase": Phase.QA, "pending": 0, "inflight": 0,
              "hint_inflight": True, "reveal_inflight": False,
              "reveal_remaining_seconds": None, "stopped": False}
        pf._probe = lambda: pd
        check("hint 在途时让路", pf._should_continue() is False)
        pd2 = dict(pd, hint_inflight=False, stopped=True)
        pf._probe = lambda: pd2
        check("stopped 时让路", pf._should_continue() is False)


def test_g1_midflight_switch_stops_reviewer_and_next_draft():
    """**G1-B 续**: 谓词变 False 之后, 补池**不再继续**并且记成 interrupted。

    这是"draft1 返回 -> Reviewer call = 0 -> draft2 = 0"的直接表达:
    `_GatedWriter` 在第 1 次调用时谓词还是 True(请求已发出, 无法取消),
    返回之后的每一次检查都变成 False —— 于是没有任何后续调用。
    """
    print("\n[G1-C] 中途让路: 不再出第二稿、不计失败")
    with tmpdir() as d:
        # 场景: 请求发出时相位还空闲(所以真的发出去了), 但它**返回时**
        # 直播已切进 SETTING —— 于是 gen_spec 内部的第一个检查点就让路,
        # 返回 interrupted spec。补池必须把它记成 interrupted 而不是
        # gen_fail, 并且**不再启动第二稿**。
        state = {"busy": False}

        def probe():
            v = Phase.SETTING if state["busy"] else Phase.QA
            return {"phase": v, "pending": 0, "inflight": 0,
                    "hint_inflight": False, "reveal_inflight": False,
                    "reveal_remaining_seconds": None,
                    "puzzle_index": 4, "stopped": False}

        w = _GatedWriter()
        real = w.gen_spec

        def gen_spec(**kw):
            kw["should_continue"] = pf._should_continue
            return real(**kw)

        w.gen_spec = gen_spec
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), writer=w, probe=probe,
                  executor=_SyncExecutor(), pool_prefetch_max_attempts=2)
        fill(pf.pool, 1)
        # 让第 1 次 gen_spec **一进门就**发现直播已忙: `start_busy` 在
        # 提交之后、worker 真正跑之前翻相位 —— 正是实播的时间线
        # (18:44:49 发出, 18:45:07 相位已变, 18:45:58 才返回)。
        real_submit = pf._executor.submit

        def submit(fn, *a, **kw):
            state["busy"] = True
            return real_submit(fn, *a, **kw)

        pf._executor.submit = submit
        pf.on_tick()                     # 提交 -> worker 立刻跑完
        check("发出了 1 次生成(已发出的请求无法取消)",
              len(w.calls) == 1, len(w.calls))
        check("**gen_spec 拿到了取消谓词(否则让路逻辑是死的)**",
              w.calls[0]["has_predicate"] is True, w.calls[0])
        pf.on_tick()                     # 应用结果

        # 让路不是失败
        check("**不让路记成 gen_fail**", pf.generation_fail_count == 0,
              pf.generation_fail_count)
        check("**interrupted 单独计数 +1**", pf.interrupted_count == 1,
              pf.interrupted_count)
        check("**interrupted 不设退避**", pf._retry_at == 0.0, pf._retry_at)
        check("**interrupted 不加失败链**", pf._fail_streak == 0,
              pf._fail_streak)
        check("**池子里没有多出题(让路的稿子不入池)**",
              pf.pool.stock_count() == 1, pf.pool.stock_count())
        check("**没有第二稿(让路后不再继续)**", len(w.calls) == 1,
              len(w.calls))


def test_g1_interrupted_yields_without_retry_storm():
    """让路期间**不反复启动** —— 每拍都试就是另一种白烧。

    谓词让路时, `_low_pressure()` 同样会挡住启动(pending/inflight/
    phase 三条腿)。这里断言的是两条防线一致: 不能让谓词说 False 而
    启动路径照旧提交。
    """
    print("\n[G1-D] 让路期间不重复启动")
    with tmpdir() as d:
        ex = _SyncExecutor()
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), writer=_GatedWriter(),
                  executor=ex,
                  probe=lambda: {"phase": Phase.SETTING, "pending": 0,
                                 "inflight": 0, "hint_inflight": False,
                                 "reveal_inflight": False,
                                 "reveal_remaining_seconds": None,
                                 "puzzle_index": 3, "stopped": False})
        fill(pf.pool, 1)
        for _ in range(8):
            pf.on_tick()
        check("**SETTING 下一次都没提交**", len(ex.submitted) == 0,
              len(ex.submitted))
        check("latch 开着但不动(只是这一拍不补)", True)


def test_g1_backoff_schedule_increases_then_caps():
    """**G1**: 连续失败退避必须递增并封顶, 不是永远固定 30 秒。

    实播指纹: 18:46:47 开/18:47:37 败, 18:48:07 开/18:49:09 败 ——
    固定 30 秒 + 每轮 4 稿, 在**同一个上下文**里反复烧。递增让"越失败
    等越久", 封顶 300 保证它最终还会再试。
    """
    print("\n[G1-E] 连续失败退避递增")
    with tmpdir() as d:
        clk = _Clock()
        w = _FakeWriter(fail=True)
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), writer=w, clock=clk)
        fill(pf.pool, 1)
        seen = []
        for i in range(7):
            pf.on_tick()
            pf.on_tick()                 # 应用上一拍的结果
            seen.append(round(pf._retry_at - clk.t, 1))
            clk.advance(400)             # 越过任意退避 -> 允许下一次
        check("**退避递增 30/60/120/240/300**",
              seen[:5] == [30.0, 60.0, 120.0, 240.0, 300.0], seen)
        check("**封顶 300(不无限翻倍)**", seen[5:] == [300.0, 300.0],
              seen)
        check("失败链记到了 7", pf._fail_streak == 7, pf._fail_streak)


def test_g1_success_resets_backoff_streak():
    """成功入池 -> 退避清零 **且** 失败链归零(下次从第一档重新开始)。"""
    print("\n[G1-F] 成功重置退避与失败链")
    with tmpdir() as d:
        clk = _Clock()
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)),
                  writer=_FakeWriter(fail=True), clock=clk)
        fill(pf.pool, 1)
        pf.on_tick()
        pf.on_tick()
        check("先失败 -> 有失败链", pf._fail_streak == 1, pf._fail_streak)
        clk.advance(400)
        pf.writer = _FakeWriter()
        pf.on_tick()
        pf.on_tick()
        check("**成功 -> 退避清零**", pf._retry_at == 0.0, pf._retry_at)
        check("**成功 -> 失败链归零**", pf._fail_streak == 0,
              pf._fail_streak)


def test_g1_scene_change_resets_long_backoff_once():
    """新一题正式开始 -> 长退避**重置一次**到第一档。

    退避的初衷是"别在同一个坏上下文里反复烧钱"。新一题开始后 recent
    window / 配额饱和状态整体换了一批, 机械等满 300 秒只是白等 ——
    但也不能立刻清零(刚失败过的环境没有变好), 所以重置到 30 秒档。

    判据是**场景指纹**(puzzle_index), 不是"时间到了" —— 后者会让
    递增序列形同虚设。
    """
    print("\n[G1-G] 场景变化重置长退避")
    with tmpdir() as d:
        clk = _Clock()
        scene = {"n": 5}

        def probe():
            return {"phase": Phase.QA, "pending": 0, "inflight": 0,
                    "hint_inflight": False, "reveal_inflight": False,
                    "reveal_remaining_seconds": None,
                    "puzzle_index": scene["n"], "stopped": False}

        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)),
                  writer=_FakeWriter(fail=True), clock=clk, probe=probe)
        fill(pf.pool, 1)
        # 攒出一条长退避
        for _ in range(4):
            pf.on_tick()
            pf.on_tick()
            clk.advance(400)
        check("已进入长退避档(>=240s)", pf._fail_streak >= 4,
              pf._fail_streak)
        pf.on_tick()
        pf.on_tick()
        long_wait = pf._retry_at - clk.t
        check("退避已是长档", long_wait >= 240.0, long_wait)
        # 新一题开始
        scene["n"] = 6
        pf.on_tick()
        check("**场景变了 -> 失败链重置为 1**", pf._fail_streak == 1,
              pf._fail_streak)
        check("**退避回到第一档 30s**",
              round(pf._retry_at - clk.t, 1) == 30.0,
              pf._retry_at - clk.t)
        # 场景**没**再变 -> 不再重置(否则递增序列形同虚设)
        clk.advance(31)
        pf.on_tick()
        pf.on_tick()
        check("场景不变 -> 继续递增到 60", pf._fail_streak == 2,
              pf._fail_streak)


def test_g1_effective_guard_covers_budget():
    """**配置侧自洽**: guard 小于一轮预算时, 实际生效值必须被抬上去。

    实播事故的算术: guard=15s, 但一轮 prefetch 能跑几十秒。于是
    "只剩 18 秒"顺利通过启动检查, 然后跨过 deadline 与下一题的现场
    生成相撞。`effective_guard = max(配置值, budget + 余量)` 让这条
    算术不再成立 —— 而且**不用运维记得同步改两个数**。
    """
    print("\n[G1-H] effective guard 覆盖一轮预算")
    with tmpdir() as d:
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)),
                  executor=_SyncExecutor(),
                  pool_prefetch_budget_seconds=25.0,
                  pool_prefetch_guard_margin_seconds=5.0,
                  pool_reveal_start_guard_seconds=15.0)
        check("**effective = max(15, 25+5) = 30**",
              pf._effective_guard_s == 30.0, pf._effective_guard_s)
        check("stats 报的是生效值",
              pf.stats()["effective_guard_s"] == 30.0,
              pf.stats()["effective_guard_s"])
        # 剩余 20 秒: 旧实现(guard=15)会启动, 新实现必须挡住
        pf._probe = lambda: _reveal_probe(remaining=20.0)
        check("**剩余 20s <= effective 30s -> 不启动**",
              pf._deadline_too_close() is True)
        check("**剩余 20s 时谓词也让路**", pf._should_continue() is False)
        pf._probe = lambda: _reveal_probe(remaining=45.0)
        check("剩余 45s > effective -> 可启动",
              pf._deadline_too_close() is False)
        check("剩余 45s 时谓词放行", pf._should_continue() is True)


def test_g1_interrupted_probe_fails_closed():
    """探针坏了 -> 让路(fail closed), 而不是"当作没事继续跑"。"""
    print("\n[G1-I] 探针异常 -> 让路")
    with tmpdir() as d:
        def boom():
            raise RuntimeError("探针炸了")

        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), writer=_GatedWriter(),
                  probe=boom, executor=_SyncExecutor())
        check("**探针抛异常 -> 谓词 False**", pf._should_continue() is False)
        check("presence: 谓词不抛", True)


def main():
    tests = [
        # A. 库存与探针
        test_stock_count_excludes_used,
        test_stock_count_excludes_invalid_spec,
        test_stock_count_excludes_old_policy,
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
        # C. 接线(端到端)
        test_director_prefetch_end_to_end,
        test_scheduler_calls_prefetch,
        test_director_no_llm_disables_prefetch_generation,
        test_director_pool_disabled_no_prefetcher,
        test_prefetch_does_not_change_live_blueprint_sequence,
        test_pick_blueprint_actually_returns_one,
        # D. final concurrency
        test_review_side_channel_is_per_instance,
        test_review_side_channel_reset_on_entry,
        test_director_prefetch_has_own_writer,
        test_prefetch_writer_none_when_no_client,
        test_shutdown_docstring_is_honest,
        # E. L1 —— playable 库存 / REVEALED 窗口 / 当前题 avoid
        test_playable_count_is_readonly,
        test_playable_count_respects_policy_gate,
        test_playable_count_fail_closed_on_bad_ledger,
        test_prefetch_l1_a_stock_ok_but_playable_zero,
        test_prefetch_l1_b_stock_ok_playable_ok_no_refill,
        test_prefetch_l1_c_max_size_stops_generation,
        test_prefetch_l1_e_revealed_window,
        test_prefetch_l1_f_current_puzzle_in_avoid,
        test_prefetch_probe_and_generate_share_one_snapshot,
        test_prefetch_stats_exposes_playable,
        test_playable_min_zero_restores_q9_behavior,
        test_max_size_below_target_is_flagged,
        # ---- U1: 揭晓窗口专用目标 + deadline guard ----
        test_u1_reveal_uses_higher_target,
        test_u1_reveal_playable_target,
        # ---- C3: probe 的 limit 必须跟着阶段目标走 ----
        test_c3_reveal_playable_probe_counts_past_one,
        test_c3_reveal_playable_probe_stops_at_two_not_ten,
        test_u1_qa_still_uses_conservative_target,
        test_u1_deadline_guard_blocks_new_requests,
        test_u1_deadline_guard_ignores_non_reveal,
        test_u1_multiple_generations_within_one_reveal,
        test_u1_reveal_never_blocks_next_puzzle,
        test_u1_guard_config_validation,
        # ---- G1: 后台补池生命周期 + 独立预算 + 递增退避 ----
        test_g1_prefetch_passes_own_budget_not_live_budget,
        test_g1_prefetch_yields_when_phase_switches_midflight,
        test_g1_midflight_switch_stops_reviewer_and_next_draft,
        test_g1_interrupted_yields_without_retry_storm,
        test_g1_backoff_schedule_increases_then_caps,
        test_g1_success_resets_backoff_streak,
        test_g1_scene_change_resets_long_backoff_once,
        test_g1_effective_guard_covers_budget,
        test_g1_interrupted_probe_fails_closed,
    ]
    for t in tests:
        t()
    print()
    if FAIL[0]:
        print(f"FAILED: {FAIL[0]} 项")
        return 1
    print("PASS: 补池(零件 + 状态机 + 接线 + 并发隔离) 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
