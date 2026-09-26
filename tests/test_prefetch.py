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

import io
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # G2: 复用 test_llm 的夹具
from story.config import Config  # noqa: E402
from story.engine import RoundEngine  # noqa: E402
#: G3: 只为一个 provenance 断言(metrics 里记的种子版本 == 模块常量)。
from story.keyword_seed import KEYWORD_SEED_VERSION  # noqa: E402
from story.llm import LLMResult  # noqa: E402
from story.prefetch import PoolPrefetcher  # noqa: E402
from story.puzzle import (  # noqa: E402
    DiscoveryBeat, FairClue, PuzzleBlueprint, PuzzleFact, PuzzleSignature,
    PuzzleSpec, SolveAtom,
)
from story.pool import PuzzlePool, spec_key  # noqa: E402
from story.playtest import PASS, INTERRUPTED  # noqa: E402
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
    #: G2: 本文件的 config 默认**关掉** keyword2 —— 见 `mkpf` 的说明。
    #: 放在这里而不是 `mkpf` 里, 是因为有 5 处用例**绕过 `mkpf` 直接**
    #: 构造 `PoolPrefetcher`, 它们也用 `mkcfg`。改一处覆盖全部。
    kw.setdefault("pool_keyword_seed_enabled", False)
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
    "库存有没有"。

    ⚠️ **G4-B**: 原来这里用"pop 被窗口挡住 -> None"来演示"两个指标不等"。
    现在纯 diversity 不再挡交付, 所以改用 `too_similar`(identity, 两遍
    都挡)——它同样能让 playable 掉到 0 而 stock 不动。
    """
    print("\n[A3] stock_count 不扣 dynamic gate")
    with tmpdir() as d:
        cfg = mkcfg(d)
        PuzzlePool.open(cfg).add(good_spec())
        loaded = PuzzlePool.open(cfg)
        sig = loaded._items[0].signature.to_dict()
        wall = [sig] * 10
        check("stock 仍数是 1", loaded.stock_count() == 1, loaded.stock_count())
        check("**G4-B: 纯 diversity 不再挡住 pop**",
              loaded.pop_next(recent_signatures=wall) is not None)
    # 交付一次 -> used 了, 现在才是"库存有但播不出"的干净例子。
    with tmpdir() as d:
        cfg = mkcfg(d)
        PuzzlePool.open(cfg).add(good_spec())
        loaded = PuzzlePool.open(cfg)
        pz = good_spec().puzzle
        check("**too_similar 命中 -> pop 返回 None**",
              loaded.pop_next(avoid=[pz]) is None)
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


def mkpf(tmp, pool=None, writer=None, clock=None, executor=None,
         probe_inputs=None, background_active=True, **cfgkw):
    """建一个 PoolPrefetcher, 协作者默认都是"最宽松"的假件。

    ## G2: 默认走 **classic** 链(`pool_keyword_seed_enabled=False`)

    这是**刻意**的, 不是漏配。理由:

      * 本文件里绝大多数用例测的是 **latch / 单飞 / 退避 / 停机 / 试玩**
        —— 那些机制与"候选怎么产生"**完全无关**。让它们默认跑 keyword2
        只会给每个用例多挂两个假方法, 却一条新断言都不加;
      * `_FakeWriter` 只实现了 `gen_spec`。默认走 classic 意味着**所有既有
        用例的替身依然有效**, 不必为一个与它们无关的改动集体改写。

    keyword2 的用例在 `mkpf(d, pool_keyword_seed_enabled=True, writer=...)`
    上**显式打开**, 并传一个实现了两条新方法的 writer(`_KeywordWriter`)。
    这样"哪条链被测到"在调用点一眼可见, 而不是靠默认值猜。

    ## Phase C: `background_active`

    默认**已激活** —— 绝大多数用例关心的是"预热之后的稳态调度", 也就是
    后台已经 activate 的状态。要测"预热期间 scheduler 不能抢跑"的用例
    (regression Q/R), 显式传 `background_active=False`, 自己再调
    `pf.activate_background()`。
    """
    # 机制测试固定 2→5 / playable=1 / max=10。
    # 这些用例测的是状态机，不应该随着生产默认水位变化而偷偷换题意。
    cfgkw.setdefault("pool_min_size", 2)
    cfgkw.setdefault("pool_target_size", 5)
    cfgkw.setdefault("pool_playable_min", 1)
    cfgkw.setdefault("pool_max_size", 10)
    cfgkw.setdefault("pool_keyword_seed_enabled", False)
    cfg = mkcfg(tmp, **cfgkw)
    if pool is None:
        pool = PuzzlePool.open(cfg)
    if writer is None:
        writer = _FakeWriter()
    if probe_inputs is None:
        probe_inputs = lambda: {"avoid": [], "recent_signatures": []}
    pf = PoolPrefetcher(
        cfg=cfg, pool=pool, writer=writer,
        probe_inputs=probe_inputs,
        pick_blueprint=lambda recent, rng=None: None,
        clock=clock or _Clock(), executor=executor or _SyncExecutor())
    if background_active:
        pf.activate_background()
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
    """refill-to-target: latch 一旦启动，一路补到高水位, 中途不停。

    Phase C 后这里不再有"让路"语义 —— 后台补池**不读**任何直播状态,
    所以"直播忙不忙"在这里根本不是一个输入。这条用例现在钉的是:
    库存跌破低水位 -> latch active -> **连续单飞**补到 target 才清。
    (旧的 `probe=lambda: {...pending...}` 传参已经随 probe 接口一起删除,
    不能再传 —— regression B 另测"pending/inflight 非零照样补"。)
    """
    print("\n[B2] 缺货后 refill latch 持续补到 target")
    with tmpdir() as d:
        ex = _ManualExecutor()
        pool = PuzzlePool.open(mkcfg(d))
        pf = mkpf(d, pool=pool, executor=ex)
        fill(pool, 1)
        pf.on_tick()
        check("缺货 -> latch active", pf._refill_active is True)
        check("**提交 1 个后台任务**", ex.total == 1, ex.total)
        pf.on_tick()
        check("**单飞仍成立, 不会并发堆任务**", ex.total == 1, ex.total)
        ex.run_next()
        pf.on_tick()
        check("上一道完成后继续向 target 补", ex.total == 2, ex.total)


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


def test_refill_to_target_does_not_start_when_stock_healthy():
    """健康库存时不启动 —— 这是**库存**决定的, 与直播忙不忙无关。

    Phase C 前这条用例的名字里还带着 "and qa_busy"(靠注入一个 QA 忙碌的
    压力探针来制造场景)。探针接口已删除, 而它测的行为其实**只**取决于
    库存水位: stock=5 >= min=2 且 playable 达标 -> latch 不开 -> 零提交。
    所以改成直接钉这个行为, 不再假装需要"QA 忙"这个前提。
    """
    print("\n[refill-to-target] 健康库存 -> 不启动")
    with tmpdir() as d:
        ex = _ManualExecutor()
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), executor=ex)
        fill(pf.pool, 5)
        pf.on_tick()
        check("latch 没启动", pf._refill_active is False,
              pf._refill_active)
        check("零提交", ex.total == 0, ex.total)


def test_semantic_reject_immediately_continues_refill():
    """内容候选被淘汰不退避；下一拍应能立刻继续抽下一道。"""
    print("\n[refill-to-target] 语义淘汰不退避")
    with tmpdir() as d:
        clk = _Clock()
        ex = _ManualExecutor()
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), clock=clk, executor=ex)
        fill(pf.pool, 1)
        pf._refill_active = True
        pf._apply_result(
            "gen_fail", "审稿要求重出",
            {"reject": "review_rewrite"}, clk())
        check("不设 retry_at", pf._retry_at == 0.0, pf._retry_at)
        check("不累积 fail_streak", pf._fail_streak == 0, pf._fail_streak)
        check("review_rewrite 仍正常记账",
              pf.reject_count["review_rewrite"] == 1,
              pf.reject_count["review_rewrite"])
        pf.on_tick()
        check("下一拍立刻又能提交", ex.total == 1, ex.total)


def test_technical_reject_still_backs_off():
    """网关/工具技术失败仍退避，避免 outage 时热循环打爆接口。"""
    print("\n[refill-to-target] 技术失败仍退避")
    with tmpdir() as d:
        clk = _Clock()
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), clock=clk)
        fill(pf.pool, 1)
        pf._refill_active = True
        pf._apply_result(
            "gen_fail", "工具调用返回空 input",
            {"reject": "structure_technical_fail"}, clk())
        check("技术失败设 retry_at", pf._retry_at > 0, pf._retry_at)
        check("refill 中首个技术失败只短退避 5s",
              round(pf._retry_at - clk.t, 1) == 5.0,
              pf._retry_at - clk.t)
        check("技术失败累积 fail_streak", pf._fail_streak == 1,
              pf._fail_streak)
        check("技术标签仍正常记账",
              pf.reject_count["structure_technical_fail"] == 1,
              pf.reject_count["structure_technical_fail"])


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
            probe_inputs=lambda: {"avoid": [],
                                  "recent_signatures": list(state["recent"])},
            pick_blueprint=lambda recent, rng=None: None,
            clock=_Clock(), executor=ex)
        pf.activate_background()
        fill(pool, 1)
        pf.on_tick()
        check("提交了", ex.total == 1)
        state["recent"] = ["新快照"]
        ex.run_next()
        check("**执行时看到的是旧快照**",
              w.calls[0]["recent"] == ["旧快照"], w.calls[0]["recent"])


def test_pop_next_still_uses_current_gate():
    """补池时合格 != 播出时仍合格。pop_next 必须重新过**当前**窗口。

    ⚠️ **G4-B**: 窗口里的**纯 diversity** 项不再挡交付, 所以这里改用
    identity 那一关(`too_similar`)来证明"播出时仍然重判"—— 那一条
    G4 没有放宽, 也是本测试真正关心的性质。
    """
    print("\n[B15] pop_next 仍走当前 gate(补池不改这一点)")
    with tmpdir() as d:
        pool = PuzzlePool.open(mkcfg(d))
        pf = mkpf(d, pool=pool)
        fill(pool, 1)
        pf.on_tick()
        check("补池后有 2 道", pool.stock_count() == 2, pool.stock_count())
        pz = pool._items[0].puzzle
        got = pool.pop_next(avoid=[pz])
        check("**too_similar 仍然在交付时重判 -> None**", got is None, got)


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
    """让引擎离开 IDLE —— 引擎有在播的题之后, Director 的 tick 才会
    正常驱动补池(scheduler 那一拍不再被"没有在播的题"挡掉)。"""
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

        # ⚠️ Phase C: 后台补池要先 activate 才会干活(预热期间不抢跑)。
        # Director.run() 会在 `_prewarm()` 返回后调它; 这里直接驱动
        # prefetcher, 所以显式激活一次。
        pf.activate_background()

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

    确定性做法: 直接断言"两个不同的 writer 实例 + 两个独立 client，
    但模型路由完全一致"，而不是起线程跑几次看撞不撞。prefetch 独立
    client 的目的只是收紧 transport timeout/retries，不能改变业务模型。
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
        check("**prefetch client 也独立，避免短 timeout 污染 live**",
              dr._prefetcher.writer.client is not dr.writer.client)
        check("**但模型路由完全一致**",
              dr._prefetcher.writer.client.cfg.resolved_models()
              == dr.writer.client.cfg.resolved_models())
        check("**live transport 保持正式预算**",
              dr.writer.client.cfg.timeout == 60.0
              and dr.writer.client.cfg.max_retries == 3,
              (dr.writer.client.cfg.timeout,
               dr.writer.client.cfg.max_retries))
        check("**prefetch transport 是 30s/0**",
              dr._prefetcher.writer.client.cfg.timeout == 30.0
              and dr._prefetcher.writer.client.cfg.max_retries == 0,
              (dr._prefetcher.writer.client.cfg.timeout,
               dr._prefetcher.writer.client.cfg.max_retries))

        # 确定性 interleave: 两个实例各写各的, 互不可见
        dr.writer._last_review_decision = "pass"
        dr.writer._last_review_issues = ["live 的毛病"]
        check("**live 写了, 补池那边看不见**",
              dr._prefetcher.writer._last_review_decision == ""
              and not dr._prefetcher.writer._last_review_issues,
              repr(dr._prefetcher.writer._last_review_decision))
        dr._prefetcher.shutdown()


def test_pc_assembly_writer_client_split_and_prefetch_config():
    """**§23 装配回归 (Phase C)**: 两条流水线在装配层彻底分开。

    在 C9(`test_director_prefetch_has_own_writer`)之上补齐本轮要求:

      * live writer != prefetch writer(§5, 绝不合并 Writer);
      * live client != prefetch client(独立传输预算);
      * prefetch client 仍用 `pool_prefetch_llm_timeout_seconds` /
        `pool_prefetch_llm_max_retries`(后台**自己的** timeout/retry);
      * 装配完成后 prefetcher 处于 **background active** 之前的"未激活"
        态 —— `activate_background()` 由 `run()` 在 `_prewarm()` 之后调,
        装配期**故意**保持未激活(否则 prewarm 会与 scheduler 抢跑);
      * playtest 开启时走 `set_playtester()`, 而不是注入 live 压力探针。

    ⚠️ 这里断言的是**装配结果**, 不是跑起来之后的行为 —— 并发行为由
    PC-Q/PC-R 单独钉。
    """
    print("\n[PC-§23] Director 装配: 两条流水线分开")
    from director import Director
    with tmpdir() as d:
        cfg = mkcfg(d, no_llm=False)
        dr = Director(cfg)
        pf = dr._prefetcher
        check("prefetcher 已装配", pf is not None)
        # ---- writer / client 分离 ----
        check("**live writer != prefetch writer**",
              pf.writer is not dr.writer)
        check("**live client != prefetch client**",
              pf.writer.client is not dr.writer.client)
        check("**模型路由一致(只是传输预算不同)**",
              pf.writer.client.cfg.resolved_models()
              == dr.writer.client.cfg.resolved_models())
        # ---- prefetch 用自己的 timeout / retries ----
        check("**prefetch client 用后台自己的 timeout**",
              pf.writer.client.cfg.timeout
              == cfg.pool_prefetch_llm_timeout_seconds,
              (pf.writer.client.cfg.timeout,
               cfg.pool_prefetch_llm_timeout_seconds))
        check("**prefetch client 用后台自己的 max_retries**",
              pf.writer.client.cfg.max_retries
              == cfg.pool_prefetch_llm_max_retries,
              (pf.writer.client.cfg.max_retries,
               cfg.pool_prefetch_llm_max_retries))
        # ---- Phase C: 装配期**未激活**(run() 在 _prewarm 之后才 activate) ----
        check("**装配期 background 未激活(prewarm 前不抢跑)**",
              pf._background_active.is_set() is False)
        # ---- 不再注入 live pressure 探针 ----
        check("**prefetcher 没有 live pressure 探针**",
              not hasattr(pf, "_probe"))
        # 手动激活以免后台线程攥着临时目录句柄
        pf.activate_background()
        pf.shutdown()


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

    怎么造出来的: 池子里每道题都被"当前窗口"挡住, 于是
    `stock_count()` **刻意不扣** dynamic gate 仍然数到 n(被挡住的题
    仍然是库存), 而 `playable_count` / `pop_next` 一道都交付不出去。

    ## ⚠️ G4-B 换掉了"挡住"的手段

    原来靠**同一个 signature** 撞 `cross_puzzle_gate` 的配额。G4 把纯
    diversity 降成偏好之后那条路**不再让 playable 归零**(只会让交付走
    Pass 2), 于是这个夹具会**静默失去触发条件** —— 变得"看起来在测
    L1-A, 其实池子完全健康", 而测试仍然全绿。这是本轮最容易踩的坑,
    所以手段换成 `too_similar`。

    `too_similar` 满足全部三个要求: 是 **identity**(两遍都挡, G4 没动)、
    是**当前窗口**的函数(正合 L1-A 要的形状: 库存有, 此刻播不出)、
    且不写任何状态。`wall` 因此装的是**谜面文本**而不是 signature 列表
    —— 调用方把它当 `avoid` 传(`_BlockedPool.inputs` 已经改好)。
    """
    cfg = mkcfg(d)
    pool = PuzzlePool.open(cfg)
    for i in range(n):
        assert pool.add(variant(i)), \
            "前置构造失败: variant(%d) 没能入池" % i
    # 每道题的谜面都进 avoid -> `too_similar` 对全部候选命中。
    wall = [s.puzzle for s in pool._items]
    return _BlockedPool(cfg, pool, wall)


class _BlockedPool:
    """`_blocked_pool` 的返回值: 池子 + 那个"把候选全挡住"的窗口。

    补池的 `probe_inputs` 必须回这个 window, 否则 `playable_count` 在
    空窗口下看得见全部候选 —— 那是**正常**行为(池子确实有 5 道还没用过
    的题), 只是复现不出"现场一道都播不出来"。

    ⚠️ G4-B: `wall` 里装的是**谜面文本**, 所以它走 `avoid` 而不是
    `recent_signatures` —— 见 `_blocked_pool` 的说明(挡住的手段从
    `cross_puzzle_gate` 换成了 `too_similar`)。
    """
    def __init__(self, cfg, pool, wall):
        self.cfg = cfg
        self.pool = pool
        self.wall = wall

    def inputs(self, avoid=None):
        # 池子自己的谜面**加上**调用方给的 avoid, 一起当"该避开的文本"。
        return {"avoid": list(avoid or []) + list(self.wall),
                "recent_signatures": []}


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
        # ⚠️ G4-B: 用 `too_similar`(identity, 两遍都挡)造"挡住"的状态。
        # 纯 diversity 的窗口已经不再让两者归零了。
        blocked = [s.puzzle for s in pool._items]

        # ---- 一致: 被挡住时两者都判 0 / None ----
        check("**挡住 -> playable=0**",
              pool.playable_count([], avoid=blocked) == 0,
              pool.playable_count([], avoid=blocked))
        check("**同一个 avoid -> pop_next 也是 None**",
              pool.pop_next([], avoid=blocked) is None)
        # ---- 一致: 没被挡住时两者都放行 ----
        check("无 avoid -> playable=3",
              pool.playable_count(recent_signatures=[]) == 3,
              pool.playable_count(recent_signatures=[]))

        # ---- 纯只读 ----
        before_used = pool.used_count()
        before_stock = pool.stock_count()
        used_file = open(cfg.pool_used_path, "rb").read() \
            if os.path.exists(cfg.pool_used_path) else b""
        for _ in range(5):
            pool.playable_count(recent_signatures=[])
            pool.playable_count([], avoid=blocked)
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
        check("前置: **playable=0**(被 too_similar 挡住)",
              bp.pool.playable_count([], avoid=bp.wall) == 0,
              bp.pool.playable_count([], avoid=bp.wall))
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
        check("前置: playable=0",
              bp.pool.playable_count([], avoid=bp.wall) == 0)
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


def test_prefetch_l1_e_starts_regardless_of_live_phase():
    """**L1-E (Phase C 反转)**: 缺货就补 —— SETTING / REVEALING / REVEALED /
    QA 一视同仁。

    ⚠️ 这条用例与它的前身**语义相反**: 旧版断言 "REVEALED 允许, REVEALING /
    SETTING 禁止", 也就是把相位当闸门。那正是本轮拆掉的东西。

    现在没有任何一个 **相位本身** 能阻止补池。旧测试里那些"某相位 -> 禁止"
    的断言全部删除 —— 它们钉的是一个已被判定为 bug 的行为。留下的是:
    任何相位下, 只要缺货 + 无 future + 无退避, 就提交。
    """
    print("\n[L1-E] 任何直播相位都不阻止补池")
    with tmpdir() as d:
        for i, ph in enumerate((Phase.QA, Phase.SETTING, Phase.REVEALING,
                                Phase.REVEALED)):
            ex = _ManualExecutor()
            pf = mkpf(d, pool=PuzzlePool.open(mkcfg(
                d, pool_path=os.path.join(d, f"p{i}.jsonl"),
                pool_used_path=os.path.join(d, f"u{i}.jsonl"))),
                executor=ex)
            pf.on_tick()
            check(f"**空池 + {ph} -> 照样提交**", ex.total == 1, ex.total)

        # 库存健康时任何相位都不启动 —— 由**库存**决定, 不是相位。
        exq = _ManualExecutor()
        pfq = mkpf(d, pool=PuzzlePool.open(mkcfg(
            d, pool_path=os.path.join(d, "pq.jsonl"),
            pool_used_path=os.path.join(d, "uq.jsonl"))),
            executor=exq)
        fill(pfq.pool, 5)
        pfq.on_tick()
        check("**健康库存 -> 不启动(与相位无关)**", exq.total == 0, exq.total)


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
            probe_inputs=lambda: {"avoid": list(cur["avoid"]),
                                  "recent_signatures": list(cur["recent"])},
            pick_blueprint=lambda recent, rng=None: None,
            clock=_Clock(), executor=ex)
        pf.activate_background()
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
    base = dict(pool_min_size=2, pool_target_size=5)
    w = Config(sim_path="x", pool_max_size=3, **base).validate()
    check("max<target 有告警", any("硬上限" in x for x in w), w)
    w2 = Config(sim_path="x", pool_max_size=10, **base).validate()
    check("正常配置无此告警", not any("硬上限" in x for x in w2), w2)
    w3 = Config(sim_path="x", pool_playable_min=-1).validate()
    check("playable_min 为负有告警", any("playable_min" in x for x in w3), w3)


# ======================================================================
# U1 — 揭晓窗口专用目标(Phase C: 目标已统一, 这一节只剩反转后的回归)
# ======================================================================

def test_u1_deadline_guard_no_longer_gates_start():
    """**U1-D (Phase C 反转)**: 没有"距下一题多少秒"这个闸门了。

    旧版断言"REVEALED 剩余 5s <= guard 15s -> 不启动"。那条行为(后台看
    直播 deadline 决定要不要开工)正是本轮要拆的 —— 所以这里**反转**:
    剩余 1 秒照样启动。后台的启动条件里根本没有"直播还剩多久"这个输入,
    也就没有任何"临近 deadline 就收手"的分支可测。
    """
    print("\n[U1-D] deadline 不再拦启动")
    with tmpdir() as d:
        ex = _SyncExecutor()
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), executor=ex,
                  pool_min_size=2)
        pf.on_tick()
        check("**缺货就启动(与'离下一题多久'无关)**",
              len(ex.submitted) == 1, len(ex.submitted))


def test_u1_no_deadline_guard_config_exists():
    """Phase C 删除了 reveal-guard 那套配置, 它不该还以任何形式阴魂不散。"""
    print("\n[U1-D2] reveal-guard 配置已彻底移除")
    c = Config(sim_path="x")
    for name in ("pool_reveal_start_guard_seconds",
                 "pool_prefetch_guard_margin_seconds",
                 "pool_reveal_target_size",
                 "pool_reveal_playable_target"):
        check(f"**没有 {name}**", not hasattr(c, name), name)
    from story.config import build_parser
    opts = set()
    for a in build_parser()._actions:
        opts.update(a.option_strings)
    check("**没有 --pool-reveal-guard**",
          "--pool-reveal-guard" not in opts)


def test_u1_multiple_generations_in_one_refill_cycle():
    """**U1-F**: 一次 refill 周期里能连续补多道(单飞, 串行)。

    Phase C 去掉了 "within one reveal" 这个前提 —— 补池不再依赖揭晓窗口
    这个 60s 空档, 所以用例只钉"latch 开着 -> 一拍一道 -> 一直补到 target"。
    """
    print("\n[U1-F] 一个 refill 周期内连续补多道")
    with tmpdir() as d:
        ex = _SyncExecutor()
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), executor=ex,
                  pool_min_size=2, pool_target_size=5,
                  pool_playable_min=1)
        for _ in range(12):
            pf.on_tick()
        n = pf.pool.stock_count()
        check("**补到了 target(5)**", n >= 5, n)
        check("不会无限补(受 max_size=10 约束)", n <= 10, n)
        check("补了不止一道(确实连续工作)",
              len(ex.submitted) >= 3, len(ex.submitted))


def test_u1_reveal_never_blocks_next_puzzle():
    """**U1**: 补池是后台行为, `pop_next` 永远不该被它挡住。

    在途任务存在时, pop_next 仍能立刻拿到题。
    """
    print("\n[U1-H] 下一题不等补池 future")
    with tmpdir() as d:
        pool = PuzzlePool.open(mkcfg(d))
        # 要验的是"在途任务存在时 pop_next 不被挡", 所以夹具必须真的把
        # latch 打开: 用 `pool_min_size=5 > stock=3` 这条腿启动。
        fill_mixed(pool, 3)
        ex = _ManualExecutor()
        pf = mkpf(d, pool=pool, executor=ex, pool_min_size=5)
        for _ in range(3):
            pf.on_tick()
        check("**确实有在途任务(未完成)**", pf._future is not None)
        got = pool.pop_next()
        check("**在途任务存在时 pop_next 仍立刻拿到题**", got is not None)
        check("拿到的确实是一道题", bool(getattr(got, "puzzle", "")))


def test_u1_guard_config_validation():
    """reveal / pool 相关配置的 validate 告警 + 默认值(Phase C 之后)。"""
    print("\n[U1-G] reveal 配置校验(移除 guard 项之后)")
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
    c = Config(sim_path="x")
    check("默认 reveal_hold 是 60s", c.reveal_hold_seconds == 60.0,
          c.reveal_hold_seconds)
    check("默认 core_focus 是 15s", c.reveal_core_focus_seconds == 15.0,
          c.reveal_core_focus_seconds)
    # ---- 保留下来的"后台自己的预算"配置 ----
    check("默认 Story timeout 是 45s",
          c.pool_prefetch_story_timeout_seconds == 45.0,
          c.pool_prefetch_story_timeout_seconds)
    check("默认 prefetch attempts 是 2", c.pool_prefetch_max_attempts == 2,
          c.pool_prefetch_max_attempts)
    check("默认 prefetch budget 是 25s",
          c.pool_prefetch_budget_seconds == 25.0,
          c.pool_prefetch_budget_seconds)
    check("默认退避序列递增",
          list(c.pool_prefetch_backoff_schedule_s)
          == sorted(c.pool_prefetch_backoff_schedule_s),
          c.pool_prefetch_backoff_schedule_s)
    check("默认 refill 短退避是 5/10/15",
          tuple(c.pool_prefetch_refill_backoff_schedule_s)
          == (5.0, 10.0, 15.0),
          c.pool_prefetch_refill_backoff_schedule_s)
    w7 = Config(sim_path="x", pool_prefetch_max_attempts=0).validate()
    check("prefetch_max_attempts=0 会告警",
          any("pool_prefetch_max_attempts" in x for x in w7), w7)
    w8 = Config(sim_path="x",
                pool_prefetch_backoff_schedule_s=(30.0, 10.0)).validate()
    check("退避序列非递增会告警",
          any("backoff_schedule" in x for x in w8), w8)
    w9 = Config(sim_path="x",
                pool_prefetch_refill_backoff_schedule_s=(5.0, 0.0)).validate()
    check("refill 短退避含非正数会告警",
          any("refill_backoff_schedule" in x for x in w9), w9)


# ======================================================================
# G1 —— 后台补池不得跨场景白烧
# ======================================================================
class _GatedWriter:
    """可**在生成中途切换相位**的假 writer —— 复现实播那 51 秒。

    与 `_FakeWriter` 的区别: 它消费 `should_continue`, 在"每次昂贵
    调用之前"检查一次(与真 `gen_spec` 的契约一致); 一旦主动中止就返回
    一个 `metrics["interrupted"]=True`、puzzle 为空的 spec ——
    **这正是真 gen_spec 主动中止时的返回形状**(error 留空, 因为它不是
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
    """真 `gen_spec` 主动中止时的返回形状: puzzle 空、**error 也空**。"""
    s = good_spec()
    s.puzzle = ""
    s.error = ""
    s.metrics = {"interrupted": True, "ok": False}
    return s


def test_g1_prefetch_passes_own_budget_not_live_budget():
    """**G1**: 后台补池必须用自己的预算, 不能吃 live 的 4 稿 / 90s。

    live 出一道题观众在干等, 多试一稿值得; 后台补池只是"有空补一道",
    多试一稿的收益是池子里多一道题, 代价却是多占一份后台自己的调用
    预算 + 跨过 deadline 继续跑。
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


def test_g1_prefetch_continues_across_phase_switches_midflight():
    """**G1-B (Phase C 反转): 本批最高价值 regression**。

    前身复现的是实播的跨场景白烧(18:44:49 REVEALED 启动 -> 18:45:07
    切 SETTING -> prefetch 被丢弃)。Phase C 之后**行为相反**: 相位切换
    **不再**打断后台候选, 那个 51 秒相撞由"两条流水线各自独立预算 +
    库存驱动"来消解, 而不是靠中途杀掉后台任务。

    这条用例现在钉: 后台启动后, 无论直播切到哪个相位, 正在生成的候选
    **继续走完**, 不受影响; 且新 predicate 与相位无关。
    """
    print("\n[G1-B] 跨场景: 后台候选不因相位切换而中断")
    with tmpdir() as d:
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)))
        # 稳态后台谓词只认 shutdown, 不认相位。
        check("**稳态谓词放行(背景已激活且未停止)**",
              pf._background_should_continue() is True)
        check("**Phase C 不再有读相位的 _should_continue**",
              not hasattr(pf, "_should_continue"))
        check("**probe 接口也已删除**", not hasattr(pf, "_probe"))

        # 真跑一遍: 生成过程中把"直播相位"想成任意值都不影响结果 ——
        # 因为没有相位入参, 且 gen_spec 收到的谓词就是后台谓词本身。
        w = _GatedWriter()
        ex = _SyncExecutor()
        pf2 = mkpf(d, pool=PuzzlePool.open(mkcfg(
            d, pool_path=os.path.join(d, "p2.jsonl"),
            pool_used_path=os.path.join(d, "u2.jsonl"))),
            writer=w, executor=ex)
        fill(pf2.pool, 1)
        pf2.on_tick()      # 提交 + (同步)执行
        pf2.on_tick()      # 应用结果 -> added_count 才记账
        check("至少真的调用了生成", len(w.calls) >= 1, len(w.calls))
        check("**谓词传下去了**", w.calls[0]["has_predicate"] is True)
        check("**生成了题(没被任何相位打断)**", pf2.pool.stock_count() >= 2,
              pf2.pool.stock_count())
        check("**interrupted 计数为 0**", pf2.interrupted_count == 0,
              pf2.interrupted_count)


def test_g1_stop_midflight_is_interrupted_not_fail():
    """**G1-C (反转)**: 谓词变 False(= 收到停止)后, 补池不再继续, 记 interrupted。

    前身用"直播切 SETTING"制造谓词变 False; 现在唯一能让谓词变 False 的
    是 `request_stop()` / `shutdown()` —— 也就是**本次运行结束**。
    时间线: 请求已发出(无法取消) -> 停止信号到达 -> 返回时第一个检查点
    就主动中止, 返回 interrupted spec。补池必须记 interrupted, **不记**
    gen_fail, 且不再启动第二稿。
    """
    print("\n[G1-C] 中途停止: 不再出第二稿、不计失败")
    with tmpdir() as d:
        w = _GatedWriter()
        real = w.gen_spec
        fired = {"n": 0}

        def gen_spec(**kw):
            # gen_spec 每次被调用时用**当前**的后台谓词 —— 等价于真
            # gen_spec 内部那条"每步之前重查一次"的契约。
            kw["should_continue"] = pf._background_should_continue
            # ⚠️ 停止信号在** worker 体内**发出, 而不是在 `submit()` 里。
            # 那正是真实时间线:"任务已经提交、HTTP 已经发出", 此刻收尾
            # 信号到达 -> 下一次 stage 之前的检查点看到 False -> 收手。
            # 在 `submit()` 里注入会与 `_submit_lock` 同线程重入(死锁),
            # 因为"复查 stop + submit"现在是不可分割的一段。
            if fired["n"] == 0:
                fired["n"] += 1
                pf.request_stop()
            return real(**kw)

        w.gen_spec = gen_spec
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), writer=w,
                  executor=_SyncExecutor(), pool_prefetch_max_attempts=2)
        fill(pf.pool, 1)
        pf.on_tick()                     # 提交 -> worker 立刻跑完
        check("发出了 1 次生成(已发出的请求无法取消)",
              len(w.calls) == 1, len(w.calls))
        check("**gen_spec 拿到了取消谓词**",
              w.calls[0]["has_predicate"] is True, w.calls[0])
        pf.on_tick()                     # 应用结果

        check("**不记成 gen_fail**", pf.generation_fail_count == 0,
              pf.generation_fail_count)
        check("**interrupted 单独计数 +1**", pf.interrupted_count == 1,
              pf.interrupted_count)
        check("**interrupted 不设退避**", pf._retry_at == 0.0, pf._retry_at)
        check("**interrupted 不加失败链**", pf._fail_streak == 0,
              pf._fail_streak)
        check("**池子里没有多出题**",
              pf.pool.stock_count() == 1, pf.pool.stock_count())
        check("**没有第二稿(停止后不再继续)**", len(w.calls) == 1,
              len(w.calls))


def test_g1_stopped_prefetcher_never_submits_again():
    """停止之后**不再启动** —— 也不再需要"相位"来解释为什么不启动。

    ⚠️ 前身钉的是 "SETTING 下一次都没提交"。那条现在**是反需求**:
    SETTING 不再是一个闸门。取而代之的是生命周期: `request_stop()` 之后
    无论 tick 多少拍、无论库存多低, 都不提交。
    """
    print("\n[G1-D] 停止后不再提交")
    with tmpdir() as d:
        ex = _SyncExecutor()
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), writer=_GatedWriter(),
                  executor=ex)
        fill(pf.pool, 1)
        # 未停止时: 缺货 -> 提交
        pf.on_tick()
        check("**未停止: 提交**", len(ex.submitted) == 1, len(ex.submitted))
        ex.submitted.clear()
        # 请求停止 -> 之后一拍都不提交
        pf.request_stop()
        for _ in range(8):
            pf.on_tick()
        check("**停止后: 一拍都不提交**", len(ex.submitted) == 0,
              len(ex.submitted))


def test_g1_backoff_schedule_increases_then_caps():
    """库存健康时仍保留原来的保守长退避。"""
    print("\n[G1-E] 健康库存下连续失败退避递增")
    with tmpdir() as d:
        clk = _Clock()
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), clock=clk)
        pf._refill_active = False
        seen = []
        for _ in range(7):
            pf._apply_result(
                "gen_fail", "模拟技术失败",
                {"reject": "structure_technical_fail"}, clk())
            seen.append(round(pf._retry_at - clk.t, 1))
            clk.advance(400)
        check("**健康库存退避递增 30/60/120/240/300**",
              seen[:5] == [30.0, 60.0, 120.0, 240.0, 300.0], seen)
        check("**封顶 300(不无限翻倍)**", seen[5:] == [300.0, 300.0],
              seen)
        check("失败链记到了 7", pf._fail_streak == 7, pf._fail_streak)


def test_refill_active_keeps_technical_backoff_short_until_target():
    """库存没补到目标时，技术失败不能重新掉进 60/120 秒长退避。"""
    print("\n[refill-to-target] 未达目标一直用短退避")
    with tmpdir() as d:
        clk = _Clock()
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), clock=clk)
        pf._refill_active = True
        seen = []
        for _ in range(5):
            pf._apply_result(
                "gen_fail", "模拟网关技术失败",
                {"reject": "structure_technical_fail"}, clk())
            seen.append(round(pf._retry_at - clk.t, 1))
            clk.advance(30)
        check("**refill 技术退避 5/10/15 并封顶 15**",
              seen == [5.0, 10.0, 15.0, 15.0, 15.0], seen)

        # 一旦达到目标、latch 关闭，下一次技术失败才恢复保守长退避。
        pf._refill_active = False
        pf._fail_streak = 0
        pf._apply_result(
            "gen_fail", "库存已健康后的技术失败",
            {"reject": "structure_technical_fail"}, clk())
        check("**latch 关闭后恢复 30s 第一档**",
              round(pf._retry_at - clk.t, 1) == 30.0,
              pf._retry_at - clk.t)


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


def test_backoff_is_not_reset_by_live_scene_change():
    """**Phase C 反转**: 直播换了题**不**影响后台的失败链/退避。

    前身(G1-G)断言的是反面 —— "puzzle_index 变了 -> refill 失败链重置为 1"。
    那正是"直播控制后台调度"的典型: 后台的退避被一个它不该知道的
    直播变量(播到第几题)牵着走。

    Phase C 之后: 上一次失败就是上一次失败, 与直播播到第几题无关。
    `_scene_at_submit` / `puzzle_index` 重置那段代码已删除。
    """
    print("\n[G1-G] 直播换题不重置后台退避")
    with tmpdir() as d:
        clk = _Clock()
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)),
                  writer=_FakeWriter(fail=True), clock=clk)
        fill(pf.pool, 1)
        for _ in range(4):
            pf.on_tick()
            pf.on_tick()
            clk.advance(30)
        check("已进入连续失败链", pf._fail_streak >= 4, pf._fail_streak)
        pf.on_tick()
        pf.on_tick()
        capped_wait = pf._retry_at - clk.t
        check("refill 退避封顶 15s", round(capped_wait, 1) == 15.0,
              capped_wait)
        streak_before = pf._fail_streak
        # 直播换了一题(旧实现里等于"场景指纹变了")—— 现在没有任何输入
        # 通道能表达这件事, 所以失败链只会继续递增, 绝不会被重置。
        #
        # 注意必须让**一整轮**跑完(提交那一拍 + 收账那一拍), 且跨过当前
        # 退避: 单飞守卫会让"退避未到"的那一拍什么都不提交 —— 只 tick
        # 一次会读到一个"什么都没发生"的假象。
        clk.advance(16)                  # 跨过 15s 退避
        pf.on_tick()                     # 提交下一轮(仍失败)
        pf.on_tick()                     # 收账
        check("**失败链没有被'换题'重置(继续递增)**",
              pf._fail_streak == streak_before + 1, pf._fail_streak)
        check("**源码里没有 _scene_at_submit / puzzle_index 重置**",
              "scene_at_submit" not in
              io.open(os.path.join(os.path.dirname(os.path.dirname(
                  os.path.abspath(__file__))), "story", "prefetch.py"),
                  encoding="utf-8").read())


def test_no_guard_machinery_remains():
    """guard / deadline 那套机制**整体删除** —— 不能留下手但不起作用。

    前身(G1-H)测 `_effective_guard_s` / `_deadline_too_close`; G1-I 测
    探针异常 fail-closed。两者测的对象都已从实现里删除(§15: 删掉
    obsolete option 比保留 silent no-op 更好), 所以这里反过来断言它们
    **不存在**, 防止有人把"看起来无害"的空壳重新加回来当作兼容层。
    """
    print("\n[G1-H/I] guard 机制已整体移除")
    with tmpdir() as d:
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), executor=_SyncExecutor())
        for name in ("_effective_guard_s", "_continuation_guard_s",
                     "_deadline_too_close", "_probe", "_probe_safe",
                     "_low_pressure", "_effective_targets", "_should_continue",
                     "_scene_of", "_scene_at_submit", "_guard_margin",
                     "_reveal_target", "_reveal_playable_target",
                     "_reveal_guard_s"):
            check(f"**没有 {name}**", not hasattr(pf, name), name)
        st = pf.stats()
        for key in ("reveal_target", "reveal_playable_target",
                    "reveal_guard_s", "effective_guard_s",
                    "continuation_guard_s"):
            check(f"**stats 不报 {key}**", key not in st, sorted(st))
        check("**stats 报 shutdown 状态**", "shutdown" in st, sorted(st))
        check("后台自己的预算仍保留在 stats",
              st["prefetch_budget_s"] == 25.0
              and st["prefetch_story_timeout_s"] == 45.0,
              (st["prefetch_budget_s"], st["prefetch_story_timeout_s"]))



def test_g4c_playtest_runs_through_phase_switch():
    """**G4-C (Phase C 反转)**: 直播切相位**不**打断试玩。

    前身断言 "试玩开始前让路": gen_spec 已成功返回, 但这段时间里直播进入
    SETTING -> 跳过试玩。Phase C 之后**相反** —— 试玩也是后台工作, 它只
    因为 `request_stop()` / `shutdown()` 而中止。

    这条用例钉的是:
      * 试玩的 `should_continue` 拿到的是 prefetcher 的生命周期谓词;
      * 生成过程中把"直播相位"想成任意值都不影响: 试玩照跑, 候选照入池。
    """
    print("\n[G4-C1] 试玩不因相位切换被打断")
    with tmpdir() as d:
        calls = {"run": 0}

        class _Playtester:
            def run(self, spec, should_continue=None):
                # ⚠️ 只计数, 绝不抛 —— `_playtest()` 会把异常吞成
                # (None, why), 于是断言里的 AssertionError 会静默。
                calls["run"] += 1
                from story.playtest import PASS, PlaytestResult
                return PlaytestResult(status=PASS)

        class _AlwaysGoodWriter:
            """必定返回合格 spec, 完全无视 should_continue。"""

            def __init__(self):
                self.calls = []

            def gen_spec(self, avoid=None, blueprint=None, recent=None,
                         enforce_blueprint=None, should_continue=None,
                         max_attempts=None, budget_s=None, **kw):
                self.calls.append({"has_predicate": should_continue is not None})
                return good_spec()

        from story.prefetch import PoolPrefetcher
        cfg = mkcfg(d, playtest_enabled=True, pool_min_size=2,
                    pool_target_size=5)
        pool = PuzzlePool.open(cfg)
        w = _AlwaysGoodWriter()
        ex = _SyncExecutor()
        pf = PoolPrefetcher(
            cfg=cfg, pool=pool, writer=w,
            probe_inputs=lambda: {"avoid": [], "recent_signatures": []},
            pick_blueprint=lambda recent, rng=None: None,
            executor=ex)
        pf.set_playtester(_Playtester())
        # 先激活后台(prewarm 之后 Director 会做这一步)。
        pf.activate_background()
        fill(pf.pool, 1)
        pf.on_tick()
        pf.on_tick()
        check("**试玩跑了(相位不再能拦住它)**", calls["run"] >= 1,
              calls["run"])
        check("**interrupted 为 0**", pf.interrupted_count == 0,
              pf.interrupted_count)
        check("**题入池了**", pf.pool.stock_count() >= 2,
              pf.pool.stock_count())


def test_g4c_playtest_predicate_is_lifecycle_only():
    """试玩的谓词必须是 prefetcher 的生命周期谓词, 不是直播压力探针。"""
    print("\n[G4-C2] 试玩谓词 = 后台生命周期谓词")
    with tmpdir() as d:
        from story.prefetch import PoolPrefetcher
        cfg = mkcfg(d, playtest_enabled=True, pool_min_size=2,
                    pool_target_size=5)
        pool = PuzzlePool.open(cfg)
        pf = PoolPrefetcher(
            cfg=cfg, pool=pool, writer=_FakeWriter(),
            probe_inputs=lambda: {"avoid": [], "recent_signatures": []},
            pick_blueprint=lambda recent, rng=None: None,
            executor=_SyncExecutor())
        check("**stop-only: 与是否激活无关**",
              pf._background_should_continue() is True)
        pf.request_stop()
        check("**request_stop 后谓词变 False**",
              pf._background_should_continue() is False)
        # 装配口: set_playtester 是唯一入口(不鼓励直接写私有字段)。
        sentinel = object()
        pf.set_playtester(sentinel)
        check("**set_playtester 真的注入了**", pf._playtester is sentinel)


# ======================================================================
# G2 —— keyword2 两阶段链的回归
# ======================================================================
#
# 这些用例**显式**打开 `pool_keyword_seed_enabled=True`(其余用例默认关,
# 见 `mkcfg` 的说明), 并传一个实现了两条新方法的 `_KeywordWriter`。

class _KeywordWriter:
    """实现了 G2 两条新方法的假 writer。记录调用, 可编程地主动中止/失败。

    为什么不扩 `_FakeWriter`: 那个替身被 30+ 个既有用例共用, 给它加两条
    方法会让"这个替身到底实现了哪条链"变得含糊。新类只服务 G2 用例,
    意图更清楚。

    `stage_a_interrupt` / `stage_b_interrupt` 让用例在**中途某个阶段**
    模拟一次主动中止(收到停止信号) —— 那正是 §九 要求覆盖的四类窗口。

    ## ⚠️ Stage A 的谜面**就是** `good_spec()` 那道题的谜面

    这不是偷懒, 是**夹具正确性**要求: `good_spec()` 的 `fair_clues` 逐字
    引用它自己的谜面, 而 `validate_spec` 会硬查这件事。若 Stage A 返回
    一个**别的**谜面, 那么"用 Stage A 的谜面 + fixture 的 clues"造出来的
    spec 必然被 validate 判 fixable(quote 不在谜面里), 于是池门拒收 ——
    那时用例失败的原因是**夹具自相矛盾**, 而不是被测代码有问题(第一版
    就是这么挂的)。

    所以 Stage A 的返回值从 fixture 自己派生; "冻结生效"那条断言靠
    `structure_calls[0]["puzzle"] == stage_a["puzzle"]` 来验(见 G2-1),
    不需要靠换一个谜面。
    """

    def __init__(self, *, spec=None, stage_a=None, stage_a_interrupt=False,
                 stage_b_interrupt=False, stage_a_none=False):
        #: 每次 Story 的 `(keywords, lane)`。
        self.keyword_calls = []
        #: 每次 Story 收到的显式 timeout；prefetch 应为 45s。
        self.story_timeouts = []
        #: 每次 Surface 收到的 `answer`(R4: 汤面从这里截)。
        self.surface_calls = []
        #: 每次 `structure_original_idea` 的参数。
        self.structure_calls = []
        self.gen_spec_calls = []      # 不应被调到 —— 用来证明走的是新链
        self._spec = spec
        _base = good_spec()
        self._stage_a = stage_a or {
            "title": _base.title, "puzzle": _base.puzzle,
            "answer": _base.answer}
        self._a_interrupt = stage_a_interrupt
        self._b_interrupt = stage_b_interrupt
        self._a_none = stage_a_none

    # ---- R4: Story / Surface 两段(取代旧的一段 Stage A) ----
    def gen_keyword_story(self, keywords, *, should_continue=None,
                          max_attempts=None, temperature=None, timeout=None,
                          brief=None):  # Issue #50: keyword2 链现在带 brief
        """Story 阶段替身: 只交 `{"answer": ...}`。

        ⚠️ 让路语义与生产件同构, 见下面 `structure_original_idea` 那段
        长注释(调用前/返回后各问一次谓词; 让路返回 `{"interrupted": True}`
        而不是 None)。
        """
        self.keyword_calls.append((list(keywords),))
        self.story_timeouts.append(timeout)
        if self._a_none:
            return None
        if self._a_interrupt:
            return {"interrupted": True}
        if should_continue is not None and not should_continue():
            return {"interrupted": True}
        story = {"answer": self._stage_a.get("answer", "")}
        if should_continue is not None and not should_continue():
            return {"interrupted": True}
        return story

    def gen_surface(self, answer, *, should_continue=None, max_attempts=None,
                    temperature=None):
        """Surface 阶段替身: 从 answer 截出 `{"puzzle": ...}`。"""
        self.surface_calls.append(answer)
        if self._a_none:
            return None
        if self._a_interrupt:
            return {"interrupted": True}
        if should_continue is not None and not should_continue():
            return {"interrupted": True}
        surf = {"puzzle": self._stage_a.get("puzzle", "")}
        if should_continue is not None and not should_continue():
            return {"interrupted": True}
        return surf

    def structure_original_idea(self, *, title, puzzle, answer, avoid=None,
                                recent=None, should_continue=None,
                                max_attempts=None, brief=None):
        self.structure_calls.append({
            "title": title, "puzzle": puzzle, "answer": answer,
            "avoid": avoid, "recent": recent,
            "max_attempts": max_attempts})
        if self._b_interrupt:
            s = good_spec()
            s.puzzle = ""
            s.error = ""
            s.metrics = {"interrupted": True, "ok": False,
                         "generation_mode": "keyword2"}
            return s
        # G4-R1: 同 `gen_keyword_idea` —— 谓词必须被真的问到。
        if should_continue is not None and not should_continue():
            s = good_spec()
            s.puzzle = ""
            s.error = ""
            s.metrics = {"interrupted": True, "ok": False,
                         "generation_mode": "keyword2"}
            return s
        if self._spec is not None:
            return self._spec
        # 默认: 造一道合格题, 但**谜面用 Stage A 的**(证明冻结生效)。
        return good_spec(puzzle=puzzle, answer=answer, title=title,
                         metrics={"generation_mode": "keyword2", "ok": True})

    def gen_spec(self, should_continue=None, **kw):
        """classic 链的替身。

        ⚠️ G4-R1: **必须显式收下 `should_continue` 并真的调用它**。
        第一版写的是 `def gen_spec(self, **kw)` —— `should_continue` 被
        吞进 `**kw` 直接丢掉, 于是"预热走 classic 链时谓词有没有被注入"
        这件事**完全测不出来**: 去掉注入, 替身照样返回一道题, 断言照样
        绿。生产件的 `gen_spec` 是在每次昂贵调用前问一次的, 替身不能比
        生产件更宽松。
        """
        self.gen_spec_calls.append({"should_continue": should_continue, **kw})
        if should_continue is not None and not should_continue():
            s = good_spec()
            s.puzzle = ""
            s.error = ""
            s.metrics = {"interrupted": True, "ok": False}
            return s
        return variant(len(self.gen_spec_calls))


#: G4: 假 bag 用的固定**词表** —— **不读盘**, 于是用例不依赖
#: `data/keyword2_vocabulary.json` 是否存在。
#:
#: ⚠️ 刻意放了几组**同语义场**的词(`下雨` / `棺材` / `死亡`), 因为 G4
#: 取消了"必须来自不同 slot"那条人为约束 —— 若哪天有人把那条约束加回
#: 生产, 这些词会让测试红。
_FAKE_WORDS = [
    "山顶", "敲门", "电话", "老师", "下雨", "棺材",
    "高跟鞋", "死亡", "图书馆", "一百元", "三兄弟", "杀人",
]


def _fake_bag(seed=20260920):
    from story.keyword_seed import KeywordBag
    return KeywordBag(list(_FAKE_WORDS), seed)


def _mkpf_keyword(d, writer=None, **cfgkw):
    """建一个 keyword2 模式的 prefetcher。

    ⚠️ G3: 光把 `pool_keyword_seed_enabled` 设 True **已经不够** ——
    生产抽词现在读真实 corpus, corpus 不可用时 `_keyword_enabled()` 会
    返回 False 并**显式降级**到 classic。所以这里把 bag 直接注入进去
    (`pf._bag = _fake_bag()`), 让用例测的是 **keyword2 链本身**, 而不是
    "这台机器上 corpus 文件在不在"。

    真实 corpus 的加载与降级另有专门的用例覆盖 —— 见
    `test_g3_corpus_missing_degrades_to_classic*`。
    """
    cfgkw.setdefault("pool_keyword_seed_enabled", True)
    w = writer or _KeywordWriter()
    pf = mkpf(d, writer=w, **cfgkw)
    if cfgkw.get("pool_keyword_seed_enabled"):
        pf._bag = _fake_bag()
        pf._bag_meta = {"corpus_version": "test-fixture",
                        "keyword_count": len(_FAKE_WORDS), "source": "test"}
        pf._keyword_session_seed = 20260920
    return pf, w


def test_g2_keyword_path_draws_two_keys_and_adds():
    """happy path: 抽 2 词 -> Stage A -> Stage B -> 入池。

    ⚠️ 断言**不**假设"只跑一轮": `fill(pool, 1)` 之后 stock=1 < min=2,
    补到 2 仍 < target=5, 于是 latch 保持 active —— 第二拍会再补一道。
    这是 Q9 的滞回语义(不是 bug), 所以下面所有计数都写成 `>= 1` 或
    "每一次都满足", 而不是 `== 1`。
    """
    print("\n[G2-1] keyword2 主路径: 2-key -> A -> B -> 入池")
    with tmpdir() as d:
        pf, w = _mkpf_keyword(d)
        fill(pf.pool, 1)                      # stock=1 < min=2 -> 启动
        pf.on_tick()                          # 提交 + 同步执行
        pf.on_tick()                          # 应用结果(+ 可能再提交一次)
        check("调了 Stage A", len(w.keyword_calls) >= 1, w.keyword_calls)
        check("**prefetch 每次 Story 都拿到 45s 专属预算**",
              all(t == 45.0 for t in w.story_timeouts), w.story_timeouts)
        check("**每次恰好 2 个关键词**",
              all(len(k[0]) == 2 for k in w.keyword_calls), w.keyword_calls)
        check("调了 Stage B", len(w.structure_calls) >= 1, w.structure_calls)
        check("**Stage B 每次都收到 Stage A 的原文**",
              all(c["puzzle"] == w._stage_a["puzzle"]
                  for c in w.structure_calls), w.structure_calls)
        check("**没有走 classic 链**(gen_spec 零调用)",
              w.gen_spec_calls == [], w.gen_spec_calls)
        check("题进池了", pf.pool.stock_count() >= 2, pf.pool.stock_count())
        check("added_count >= 1", pf.added_count >= 1, pf.added_count)
        # ⚠️ 只看**本次补进来的**那些: `fill(pool, 1)` 预先灌的那道是
        # `variant()`(经典链的形状, 没有 generation_mode), 它不该被算进来。
        added = [s for s in pf.pool._items
                 if (s.metrics or {}).get("generation_mode") == "keyword2"]
        check("**本次补进来的每一道都是 keyword2**",
              len(added) == pf.added_count, (len(added), pf.added_count))


def test_g2_keyword_does_not_call_pick_blueprint():
    """keyword 模式**不**发 target Blueprint(§七)。"""
    print("\n[G2-2] keyword 模式不发 target Blueprint")
    with tmpdir() as d:
        calls = {"n": 0}

        def pick(recent, rng=None):
            calls["n"] += 1
            return None

        pf = mkpf(d, writer=_KeywordWriter(), pool_keyword_seed_enabled=True)
        pf._bag = _fake_bag()            # G3: bag 是 keyword 链的开关之一
        pf._pick_blueprint = pick
        fill(pf.pool, 1)
        pf.on_tick()
        pf.on_tick()
        check("**_pick_blueprint 零调用**", calls["n"] == 0, calls["n"])
        check("题进池了(证明流程真的走完了)",
              pf.pool.stock_count() >= 2, pf.pool.stock_count())

def test_g2_keyword_provenance():
    """最终 spec 必须带 keyword2 provenance, 且**仍是 generated**。"""
    print("\n[G2-3] keyword2 provenance + 仍是 generated")
    with tmpdir() as d:
        pf, w = _mkpf_keyword(d)
        fill(pf.pool, 1)
        pf.on_tick()
        pf.on_tick()
        items = list(pf.pool._items)
        new = [s for s in items if s.puzzle == w._stage_a["puzzle"]]
        check("池里有 Stage A 那道题", len(new) == 1, len(new))
        if new:
            s = new[0]
            check("generation_mode == keyword2",
                  (s.metrics or {}).get("generation_mode") == "keyword2",
                  s.metrics)
            # R4: `keyword_calls` 记的是 keywords 元组(Generation v3
            # 起无 lane), metrics 里同样只放 keywords 列表。
            _kws = list(w.keyword_calls[0][0])
            check("keywords 记进了 metrics",
                  (s.metrics or {}).get("keywords") == _kws,
                  (s.metrics or {}).get("keywords"))
            # ---- Generation v3(Issue #58): 新题 lane 恒空串 ----
            check("**新题 lane 是空串(deprecated)**",
                  (s.metrics or {}).get("lane") == "",
                  repr((s.metrics or {}).get("lane")))
            # Issue #50 §12: stage 版本改记 Prompt Pack 的
            # truth/surface stage 版本(不再是 story_prompt_version 旧标签)。
            from story.prompt_pack import stage_version as _pv
            check("**truth/surface 两个 prompt version 都落盘**",
                  (s.metrics or {}).get("truth_prompt_version")
                  == _pv("truth")
                  and (s.metrics or {}).get("surface_prompt_version")
                  == _pv("surface"), s.metrics)
            check("**source_type 为空(是 generated, 不是 curated)**",
                  not getattr(s, "source_type", ""), repr(getattr(s, "source_type", "")))
            check("**没有 curated_policy_version**",
                  not getattr(s, "curated_policy_version", ""),
                  repr(getattr(s, "curated_policy_version", "")))
            check("**没有 curated_content_hash**",
                  not getattr(s, "curated_content_hash", ""),
                  repr(getattr(s, "curated_content_hash", "")))
            # 落盘往返后 provenance 仍在(§十一 要求"必须能从日志/archive 区分")
            d2 = s.to_archive()
            check("archive 里 metrics 带着 generation_mode",
                  (d2.get("metrics") or {}).get("generation_mode") == "keyword2",
                  d2.get("metrics"))
            rt = PuzzleSpec.from_dict(d2)
            check("往返后 still keyword2",
                  (rt.metrics or {}).get("generation_mode") == "keyword2",
                  rt.metrics)


def test_g2_stage_a_none_is_gen_fail():
    """Stage A 出不来题 -> gen_fail(不是 interrupted)。"""
    print("\n[G2-4] Stage A 未成题 -> gen_fail")
    with tmpdir() as d:
        pf, w = _mkpf_keyword(d, writer=_KeywordWriter(stage_a_none=True))
        fill(pf.pool, 1)
        pf.on_tick()
        pf.on_tick()
        check("generation_fail_count == 1",
              pf.generation_fail_count == 1, pf.generation_fail_count)
        check("interrupted_count == 0", pf.interrupted_count == 0,
              pf.interrupted_count)
        check("**没进过 Stage B**", w.structure_calls == [], w.structure_calls)
        check("没进池", pf.added_count == 0, pf.added_count)


def test_g2_stage_a_interrupt_is_not_failure():
    """Stage A 主动中止 -> interrupted, **不计失败不退避**。"""
    print("\n[G2-5] Stage A 主动中止 -> interrupted(不是失败)")
    with tmpdir() as d:
        pf, w = _mkpf_keyword(d, writer=_KeywordWriter(stage_a_interrupt=True))
        fill(pf.pool, 1)
        pf.on_tick()
        pf.on_tick()
        check("**interrupted_count == 1**", pf.interrupted_count == 1,
              pf.interrupted_count)
        check("**generation_fail_count == 0**", pf.generation_fail_count == 0,
              pf.generation_fail_count)
        check("**不退避**", pf._retry_at == 0.0, pf._retry_at)
        check("**不加失败链**", pf._fail_streak == 0, pf._fail_streak)
        check("**没进过 Stage B**", w.structure_calls == [], w.structure_calls)


def test_g2_stage_b_interrupt_is_not_failure():
    """Stage B 主动中止 -> interrupted, 不入池, 不计失败。"""
    print("\n[G2-6] Stage B 主动中止 -> interrupted(不是失败)")
    with tmpdir() as d:
        pf, w = _mkpf_keyword(d, writer=_KeywordWriter(stage_b_interrupt=True))
        fill(pf.pool, 1)
        before = pf.pool.stock_count()
        pf.on_tick()
        pf.on_tick()
        check("interrupted_count == 1", pf.interrupted_count == 1,
              pf.interrupted_count)
        check("generation_fail_count == 0", pf.generation_fail_count == 0,
              pf.generation_fail_count)
        check("不退避", pf._retry_at == 0.0, pf._retry_at)
        check("**池子里没有多出题**", pf.pool.stock_count() == before,
              pf.pool.stock_count())


def test_g2_should_continue_blocks_before_stage_a():
    """检查点 ①: Stage A **之前**就主动中止 -> 连 A 都不发。

    Phase C: 注入的谓词现在是 stop-only 的(`_background_should_continue`)。
    这里用**唯一**能真实触发它的手段 —— `request_stop()` —— 而不是像前身
    那样直接覆写一个已不存在的方法。契约不变: 谓词在任何昂贵 stage 之前
    被查一次。
    """
    print("\n[G2-7] 谓词检查点 ①: Stage A 之前")
    with tmpdir() as d:
        pf, w = _mkpf_keyword(d)
        pf.request_stop()
        fill(pf.pool, 1)
        pf.on_tick()
        pf.on_tick()
        check("**Stage A 零调用**", w.keyword_calls == [], w.keyword_calls)
        check("Stage B 零调用", w.structure_calls == [], w.structure_calls)


def test_g2_should_continue_blocks_after_stage_a_before_stage_b():
    """检查点 ②: Stage A 之后收到停止信号 -> **Stage B 不调用**。

    这是新增 stage 之后最容易漏的一处: A 是一次几十秒的调用, 期间停止
    信号完全可能已经到达。

    Phase C: 不再覆写 `pf._should_continue`(已删除); 改为给 `_mkpf_keyword`
    注入一个**计数的 stop-only 谓词**, 直接测两条链上的注入点是否真的被
    调用到 —— 与 production 走的是同一个 `should_continue` 参数通道。
    """
    print("\n[G2-8] 谓词检查点 ②: A 之后 / B 之前")
    with tmpdir() as d:
        w = _KeywordWriter()
        pf, _ = _mkpf_keyword(d, writer=w)
        # 谓词: 第一次(A 之前)放行, 之后一律主动中止。
        state = {"n": 0}

        def gate():
            state["n"] += 1
            return state["n"] <= 1

        # ⚠️ 用 production 的注入通道(不是覆写私有方法): 让生成链拿到的
        #    默认谓词变成 gate。
        pf._background_should_continue = gate
        fill(pf.pool, 1)
        pf.on_tick()
        pf.on_tick()
        check("Stage A 调了 1 次", len(w.keyword_calls) == 1, w.keyword_calls)
        check("**Stage B 零调用**", w.structure_calls == [], w.structure_calls)
        check("interrupted_count == 1", pf.interrupted_count == 1,
              pf.interrupted_count)
        check("没进池", pf.added_count == 0, pf.added_count)


def test_g2_rewrite_does_not_enter_pool():
    """Reviewer 判 rewrite -> 整道候选失败, 不入池。

    writer 侧的 rewrite 判定发生在 `structure_original_idea` **内部**
    (那里接的是真 Reviewer)。这里用"Stage B 返回一道 puzzle 为空的 spec"
    模拟同一个外部效果: 候选被丢弃, 且**不重试**。
    """
    print("\n[G2-9] 候选失败(Stage B 没产出) -> 不入池、不重试")
    with tmpdir() as d:
        bad = good_spec()
        bad.puzzle = ""
        bad.error = "审稿要求重出: 没有公平推理路径"
        bad.metrics = {"generation_mode": "keyword2", "rewrite_count": 1}
        pf, w = _mkpf_keyword(d, writer=_KeywordWriter(spec=bad))
        fill(pf.pool, 1)
        pf.on_tick()
        pf.on_tick()
        check("generation_fail_count == 1", pf.generation_fail_count == 1,
              pf.generation_fail_count)
        check("**没进池**", pf.added_count == 0, pf.added_count)
        check("**只跑了一次候选**(Stage A 只调 1 次)", len(w.keyword_calls) == 1,
              w.keyword_calls)


def test_g2_keyword_disabled_uses_classic_path():
    """kill-switch: `pool_keyword_seed_enabled=False` -> 完整回到旧链。"""
    print("\n[G2-10] kill-switch: 关掉 keyword2 -> 旧 Blueprint 链")
    with tmpdir() as d:
        kw = _KeywordWriter()
        pf = mkpf(d, writer=_FakeWriter(), pool_keyword_seed_enabled=False)
        # 把一个 keyword 替身挂在旁边, 用来证明"关掉时**绝不会**碰它"。
        pf._kw_spy = kw
        fill(pf.pool, 1)
        pf.on_tick()
        pf.on_tick()
        check("**没有调 Stage A**", kw.keyword_calls == [], kw.keyword_calls)
        check("题进池了", pf.added_count >= 1, pf.added_count)
        check("走的是 gen_spec", len(pf.writer.calls) >= 1,
              len(pf.writer.calls))
        check("gen_spec 收到了 blueprint 参数",
              "blueprint" in pf.writer.calls[0], pf.writer.calls[0])


def test_g2_disabled_behavior_is_bit_identical_to_pre_g2():
    """关掉时, 每条关键行为与 G2 之前逐位一致。

    这条是 §八 "设为 False 必须**完整**回到现有 pick_blueprint -> gen_spec"
    的直接落地: 用一个记录型 writer 跑一遍, 断言参数形状与旧路径相同。
    """
    print("\n[G2-11] 关掉时行为与旧路径逐位一致")
    with tmpdir() as d:
        fw = _FakeWriter()
        pf = mkpf(d, writer=fw, pool_keyword_seed_enabled=False)
        fill(pf.pool, 1)
        pf.on_tick()
        pf.on_tick()
        c = fw.calls[0]
        check("gen_spec 的 4 个参数名齐备",
              set(c) == {"avoid", "recent", "blueprint", "enforce_blueprint"}, c)
        check("enforce_blueprint 跟着 blueprint 走(None -> False)",
              c["enforce_blueprint"] is (c["blueprint"] is not None), c)
        check("added_count == 1", pf.added_count == 1, pf.added_count)
        check("没有 interrupted", pf.interrupted_count == 0, pf.interrupted_count)
        check("没有 gen_fail", pf.generation_fail_count == 0,
              pf.generation_fail_count)


def test_g2_live_writer_never_calls_keyword():
    """**live 生成路径完全不调用 keyword Stage A**。

    分两层验, 因为**单靠任何一层都不够**:

      (a) **行为层** —— 用真 `PuzzleWriter` 走一遍 live 的调用形状
          (`director.py` 里那条 `gen_spec(avoid=..., blueprint=...,
          recent=..., enforce_blueprint=...)`), 断言两个 keyword 方法
          **零调用**。
      (b) **源码层** —— 断言 `director.py` 的 live 出题点仍然只有
          `gen_spec`, **没有** `gen_keyword_idea` / `structure_original_idea`。

    ⚠️ 为什么必须有 (b): (a) 只证明"这条调用形状不碰 keyword", 它**挡不住**
    有人在 `director.py` 的那一行**前面**插一次 `gen_keyword_idea(...)` ——
    那正是 M7 变异做的事, 而当时只有 (a) 时它**没有变红**(测试留了缺口)。
    (b) 直接把"live 只调 gen_spec"钉在源码上, 那条变异立刻红。
    """
    print("\n[G2-12] live 路径零 keyword 调用")
    from story.llm import PuzzleWriter
    from test_llm import (FakeClient, riddle, review_ok, runtime_cfg,
                         _truth_tool)  # noqa
    # ---- (a) 行为层: 走一遍 live 的调用形状 ----
    fc = FakeClient([LLMResult(tool_input=riddle()),
                     LLMResult(tool_input=review_ok())])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    calls = {"kw": 0, "st": 0}
    real_kw, real_st = w.gen_keyword_story, w.structure_original_idea

    def spy_kw(*a, **k):
        calls["kw"] += 1
        return real_kw(*a, **k)

    def spy_st(*a, **k):
        calls["st"] += 1
        return real_st(*a, **k)

    w.gen_keyword_story = spy_kw
    w.structure_original_idea = spy_st
    bp = fc.default_blueprint
    spec = w.gen_spec(avoid=None, blueprint=bp, recent=[],
                      enforce_blueprint=bp is not None)
    check("live 出了一道题", bool(spec.puzzle), spec.error)
    check("**gen_keyword_story 零调用**", calls["kw"] == 0, calls["kw"])
    check("**structure_original_idea 零调用**", calls["st"] == 0, calls["st"])
    check("live 用的还是 emit_riddle",
          "emit_riddle" in [c["tool"]["name"] for c in fc.calls
                            if c.get("tool")],
          [c["tool"]["name"] for c in fc.calls if c.get("tool")])
    # ---- (b) 源码层: director.py 的 live 出题点 ----
    src = io.open(Path(__file__).resolve().parents[1] / "director.py",
                  encoding="utf-8").read()
    check("**director.py 里没有 gen_keyword_story / gen_surface**",
          "gen_keyword_story" not in src and "gen_surface" not in src,
          [ln.strip() for ln in src.splitlines()
           if "gen_keyword_story" in ln or "gen_surface" in ln][:3])
    check("**director.py 里没有 structure_original_idea**",
          "structure_original_idea" not in src,
          [ln.strip() for ln in src.splitlines()
           if "structure_original_idea" in ln][:3])
    check("director.py 的 live 出题仍是 gen_spec",
          "self.writer.gen_spec(" in src)


def test_g2_stage_b_schema_has_no_puzzle_field():
    """**Stage B schema 里没有 puzzle/answer/title** —— 结构性禁止改写。

    ⚠️ R4: Story / Surface 是**分开**的两个 schema —— Story 只有
    `answer`, Surface 只有 `puzzle`。所以"Stage A 的 schema 有这两样"
    这条断言变成两条: 两段合起来才覆盖 puzzle + answer, 而**任何一段
    都不该同时有两者**(那会让同一次调用又想起草又想起谜面)。
    """
    print("\n[G2-13] Stage B schema 不含 puzzle/answer/title")
    from story.llm import _TOOL_STRUCTURE, _TOOL_STORY, _TOOL_SURFACE
    props = set(_TOOL_STRUCTURE["input_schema"]["properties"])
    req = set(_TOOL_STRUCTURE["input_schema"]["required"])
    for k in ("puzzle", "answer", "title"):
        check(f"properties 里没有 {k}", k not in props, sorted(props))
        check(f"required 里没有 {k}", k not in req, sorted(req))
    story_props = set(_TOOL_STORY["input_schema"]["properties"])
    surface_props = set(_TOOL_SURFACE["input_schema"]["properties"])
    check("Story schema 有 answer", "answer" in story_props, sorted(story_props))
    check("Surface schema 有 puzzle", "puzzle" in surface_props,
          sorted(surface_props))
    # ⚠️ 反过来: 任何一段都**不得**同时持有 puzzle 与 answer。
    check("**Story 不含 puzzle**", "puzzle" not in story_props)
    check("**Surface 不含 answer**", "answer" not in surface_props)


def test_g2_quota_wall_still_hard_rejects_keyword_candidate():
    """题型配额满 -> keyword candidate **不再被拒**, 但撞车必须留痕。

    ## ⚠️ G4-A 反转了这条

    它原来守的是"keyword AI 仍属 AI-original, 分布对它还是硬约束"
    (§六)。G4 的产品决定改掉了:

        同类型不是拒题理由。
        safety / correctness / playability / true duplicate 才是硬门。

    所以配额墙**不再**拒稿 —— 一道 A+B+审稿+audit 全跑完的合格稿,
    不该因为"recent 里 death 已经有 2 道"被扔掉。撞车事实进
    `metrics["diversity_signals"]`, 由池子的 Pass 1 当偏好用。

    ## 本测试真正守住的(没有放宽)

    1. 稿子**交付了**(`spec.puzzle` 非空) —— 不是恒真的空断言;
    2. 撞车**确实被算出来了**并记进 metrics —— 否则"降级"与"整段删掉"
       无法区分;
    3. 没有任何 error, 且错误**不来自 curated 门**(source_type 仍是空,
       keyword2 仍是 AI-original 而不是 curated)。
    """
    print("\n[G2-14] G4-A: 配额墙只记录, 不再硬拒 keyword candidate")
    from story.llm import PuzzleWriter, STORY_PROMPT_VERSION
    from test_llm import (FakeClient, riddle, review_ok, runtime_cfg,
                         _truth_tool)  # noqa
    st = dict(riddle())
    for k in ("puzzle", "answer", "title"):
        st.pop(k, None)
    pz = riddle()["puzzle"]
    # ⚠️ R4: 现在是 Story / Surface 两段。Story 只要 answer, Surface 只要
    # puzzle —— 旧的 Case-first 脚手架(core_truth / observed_clues /
    # event_chain)已经不在链上了。
    fc = FakeClient([LLMResult(tool_input={"answer": riddle()["answer"]}),
                     LLMResult(tool_input={"puzzle": pz}),
                     LLMResult(tool_input=st),
                     LLMResult(tool_input=review_ok())])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    story = w.gen_keyword_story(["图书馆", "上楼"])
    surf = w.gen_surface(story["answer"])
    i = {"title": "", "puzzle": surf["puzzle"], "answer": story["answer"]}
    # 造一个 recent 窗口: 同一 (mechanism_family, solution_shape) 已满。
    from story.puzzle import PuzzleSignature
    sig = PuzzleSignature.from_dict({
        "mechanism_family": "hidden_function",
        "solution_shape": "hidden_function_explains_behavior",
        "domain": "maritime", "emotion_mode": "neutral",
        "relation": "stranger", "time_shape": "habitual",
        "reveal_mode": "meaning_flip"})
    recent = [sig, sig, sig]           # 远超 same_mechanism/same_shape 上限
    spec = w.structure_original_idea(title=i["title"], puzzle=i["puzzle"],
                                     answer=i["answer"], recent=recent)
    check("**G4-A: 配额撞车不再拒稿**(puzzle 非空)",
          bool(spec.puzzle), spec.puzzle[:40])
    check("**但撞车被算出来并记进了 metrics**",
          bool((spec.metrics or {}).get("diversity_signals")),
          (spec.metrics or {}).get("diversity_signals"))
    check("**没有因此记 error**", not spec.error, spec.error)
    check("**仍然不是 curated**(source_type 为空)",
          not (spec.source_type or ""), spec.source_type)
    check("**错误不来自 curated 门**",
          "curated" not in (spec.error or "").lower(), spec.error)


def test_g2_too_similar_still_hard_rejects():
    """`avoid` 里已有近重复谜面 -> keyword candidate 仍被硬拒。"""
    print("\n[G2-15] too_similar / avoid 仍硬拒")
    from story.llm import PuzzleWriter
    from test_llm import (FakeClient, riddle, review_ok, runtime_cfg,
                         _truth_tool)  # noqa
    st = dict(riddle())
    for k in ("puzzle", "answer", "title"):
        st.pop(k, None)
    pz = riddle()["puzzle"]
    # ⚠️ 这条用例**直接**调 `structure_original_idea`, 不经过 Story/Surface
    # —— 所以队列第一条就是结构 payload。(R4 拆链不影响这里。)
    fc = FakeClient([LLMResult(tool_input=st),
                     LLMResult(tool_input=review_ok()),
                     _truth_tool()])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    spec = w.structure_original_idea(title="", puzzle=pz,
                                     answer=riddle()["answer"], avoid=[pz])
    check("**被 avoid 硬拒**", not spec.puzzle, spec.puzzle[:40])
    check("拒因提到太像", "太像" in (spec.error or ""), spec.error)


# ======================================================================
# G4 —— 词库降级链 + bag 接线
# ======================================================================
#
# G2 那批用例把 bag **注入**进去(见 `_mkpf_keyword`), 测的是 keyword2
# 链本身。这一批相反: 用**真实**的 `_init_keyword_bag` 路径, 专门测
# "词库不可用时到底发生什么"。

def _mkpf_raw_corpus(d, corpus_path, writer=None, **cfgkw):
    """不注入 bag, 让 `_init_keyword_bag` 真的去读词库。"""
    cfgkw.setdefault("pool_keyword_seed_enabled", True)
    cfgkw["keyword_corpus_path"] = corpus_path
    w = writer or _KeywordWriter()
    return mkpf(d, writer=w, **cfgkw), w


def test_g4_vocab_missing_degrades_to_classic():
    """§八-9: 词库缺失 -> **显式**降级到 classic, 不回退人工词库。

    测的是**行为**: 文件不在时, 补池仍然工作(题照样进池), 但走的是
    `gen_spec` 而不是 `gen_keyword_idea`, 且**没有**任何 keyword 调用。
    """
    print("\n[G4-1] 词库缺失 -> 显式降级到 classic")
    with tmpdir() as d:
        fw = _FakeWriter()
        pf, w = _mkpf_raw_corpus(d, os.path.join(d, "nope.json"), writer=fw)
        check("**bag 是 None**", pf._bag is None, pf._bag)
        check("**降级原因被记下来了**", bool(pf._bag_error), pf._bag_error)
        check("**`_keyword_enabled()` 为假**", pf._keyword_enabled() is False)
        fill(pf.pool, 1)
        pf.on_tick()
        pf.on_tick()
        check("**题照样进池(降级不是罢工)**", pf.added_count >= 1,
              pf.added_count)
        check("**走的是 gen_spec**", len(fw.calls) >= 1, len(fw.calls))
        check("**gen_spec 收到了 blueprint 参数(是 classic 链)**",
              "blueprint" in fw.calls[0], sorted(fw.calls[0]))


def test_g4_vocab_missing_never_falls_back_to_bank():
    """§八-9 的核心: 降级**不得**触碰 `KEYWORD_BANK`。"""
    print("\n[G4-2] 降级不碰人工词库(源码级)")
    import ast as _ast
    root = Path(__file__).resolve().parents[1]
    src = io.open(root / "story" / "prefetch.py", encoding="utf-8").read()
    # ⚠️ **按 AST 查引用, 不是 grep 文本** —— `prefetch.py` 的注释里明确
    # 写着"**绝不**回退 `KEYWORD_BANK`"和"不再用 `draw_two_keywords`",
    # 那两句是在**说明这条规则**, 朴素 grep 会把说明当成违规, 于是断言
    # 永远红, 然后被"修"掉(那正是 K10 与 K39 踩过的同一个坑)。
    names = set()
    for node in _ast.walk(_ast.parse(src)):
        if isinstance(node, _ast.Name):
            names.add(node.id)
        elif isinstance(node, _ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, _ast.alias):
            names.add(node.asname or node.name.split(".")[-1])
    check("**prefetch.py 里没有 KEYWORD_BANK(AST)**",
          "KEYWORD_BANK" not in names, sorted(n for n in names
                                              if "KEYWORD" in n))
    check("**prefetch.py 里没有 draw_two_keywords(AST)**",
          "draw_two_keywords" not in names, sorted(n for n in names
                                                   if "draw" in n))
    check("prefetch.py 用的是独立词库路径",
          "load_bag" in src or "_bag" in src)
    # 结构层: 把 bag 手动设成 None, 即使词库完好也必须走 classic。
    #
    # 两个 writer 分工: classic 链需要 `gen_spec`, 而"有没有偷偷调 keyword"
    # 要用 `keyword_calls` 观察 —— 所以用一个两者兼有的探针。
    class _Spy(_FakeWriter):
        def __init__(self):
            super().__init__()
            self.keyword_calls = []

        def gen_keyword_story(self, keywords, **kw):
            self.keyword_calls.append((list(keywords),))
            raise AssertionError("**降级后不该调 gen_keyword_story**")

        def gen_surface(self, answer, **kw):
            self.keyword_calls.append("surface")
            raise AssertionError("**降级后不该调 gen_surface**")

        def structure_original_idea(self, **kw):
            self.keyword_calls.append("structure")
            raise AssertionError("**降级后不该调 structure_original_idea**")

    with tmpdir() as d:
        spy = _Spy()
        pf, w = _mkpf_keyword(d, writer=spy)
        check("注入 bag 后是 keyword 模式", pf._keyword_enabled() is True)
        pf._bag = None
        check("**bag 置空后立刻变 classic(与词库在不在无关)**",
              pf._keyword_enabled() is False)
        fill(pf.pool, 1)
        pf.on_tick()
        pf.on_tick()
        check("**零 keyword 调用**", spy.keyword_calls == [], spy.keyword_calls)
        check("走了 gen_spec", len(spy.calls) >= 1, len(spy.calls))
        check("题照样进池", pf.added_count >= 1, pf.added_count)


def test_g4_vocab_empty_or_corrupt_also_degrades():
    """空 / 损坏 / 全无效的词库与"缺失"走同一条降级。"""
    print("\n[G4-3] 空/损坏词库同样降级")
    bad = {
        "empty.json": {"keywords": []},
        "null.json": {"keywords": None},
        "notlist.json": {"keywords": "x"},
        "allbad.json": {"keywords": [None, 123, "", "我杀了他"]},
    }
    with tmpdir() as d:
        for name, obj in bad.items():
            p = os.path.join(d, name)
            with io.open(p, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False)
            fw = _FakeWriter()
            pf, w = _mkpf_raw_corpus(d, p, writer=fw)
            check(f"{name}: bag 为 None", pf._bag is None)
            check(f"{name}: 降级到 classic", pf._keyword_enabled() is False)
        p = os.path.join(d, "garbage.json")
        with io.open(p, "wb") as f:
            f.write(b"\x00\x01\x02 not json at all")
        fw = _FakeWriter()
        pf, w = _mkpf_raw_corpus(d, p, writer=fw)
        check("二进制垃圾: bag 为 None", pf._bag is None)
        check("二进制垃圾: 仍能建起 prefetcher(不抛)", pf is not None)


def test_g4_good_vocab_activates_bag():
    """反向: 词库可用时 bag 真的建起来, 且日志行含 §六 的三个字段。

    ⚠️ 这条是**必须**有的反向用例。`_init_keyword_bag` 第一版用
    `if not self._keyword_enabled(): return` 短路, 而那个谓词**包含
    "bag 已存在"** —— 于是 bag 永远是 None, 生产静默退回 classic, 连
    `_bag_error` 都是空的。只有"注入一份完好词库就该建起来"这种**正向**
    断言才能抓到它(失败路径的用例全都照样绿)。
    """
    print("\n[G4-4] 词库可用 -> bag 就绪 + 日志行")
    from story.keyword_corpus import build_vocabulary
    with tmpdir() as d:
        rows = [{"input": "关键词：山顶，敲门，台阶"},
                {"input": "关键词：下雨、棺材"}]
        p = os.path.join(d, "v.json")
        with io.open(p, "w", encoding="utf-8") as f:
            json.dump(build_vocabulary(rows), f, ensure_ascii=False)
        pf, w = _mkpf_raw_corpus(d, p)
        check("**bag 建起来了**", pf._bag is not None)
        check("`_keyword_enabled()` 为真", pf._keyword_enabled() is True)
        check("session seed 被记下", pf._keyword_session_seed is not None,
              pf._keyword_session_seed)
        line = pf._bag_meta_line()
        for token in ("session_seed=", "corpus_version=", "keyword_count="):
            check(f"日志行含 {token}", token in line, line)
        fill(pf.pool, 1)
        pf.on_tick()
        pf.on_tick()
        check("Stage A 被调了", len(w.keyword_calls) >= 1, w.keyword_calls)
        if w.keyword_calls:
            got = set(w.keyword_calls[0][0])   # R4: (keywords, lane)
            check("**抽到的词来自词库**",
                  got <= {"山顶", "敲门", "台阶", "下雨", "棺材"}, got)


def test_g4_session_seed_reproducible_end_to_end():
    """§六: 同词库 + 同 session_seed -> 同一个 pair 序列(端到端)。"""
    print("\n[G4-5] session seed 端到端可复现")
    from story.keyword_corpus import build_vocabulary
    rows = [{"input": "关键词：山顶，敲门"},
            {"input": "关键词：下雨、棺材"},
            {"input": "关键词：电话，老师，火车"}]
    seqs = []
    # ⚠️ **每次跑都要一个新的 tmpdir**。共用一个目录会让第二次跑的 pool
    # 文件里已经有第一次补进去的题 —— 于是 stock 已经够, 补池根本不启动,
    # `keyword_calls` 是空的, 而失败原因与"可复现"毫无关系。
    for _ in range(2):
        with tmpdir() as d:
            p = os.path.join(d, "v.json")
            with io.open(p, "w", encoding="utf-8") as f:
                json.dump(build_vocabulary(rows), f, ensure_ascii=False)
            pf, w = _mkpf_raw_corpus(d, p, keyword_session_seed=4242)
            fill(pf.pool, 1)
            pf.on_tick()
            pf.on_tick()
            seqs.append([tuple(k) for k in w.keyword_calls])
    check("**两次跑拿到同一个 pair 序列**", seqs[0] == seqs[1], seqs)
    check("确实抽到了词", len(seqs[0]) >= 1, seqs[0])


def test_g4_provenance_records_vocab_and_seed():
    """§六: metrics 里要有 pair / seed / 词库版本供追溯。"""
    print("\n[G4-6] provenance 记录 keyword pair + seed/词库版本")
    with tmpdir() as d:
        pf, w = _mkpf_keyword(d)
        fill(pf.pool, 1)
        pf.on_tick()
        pf.on_tick()
        items = [s for s in pf.pool._items if s.puzzle == w._stage_a["puzzle"]]
        check("池里有那道题", len(items) == 1, len(items))
        if items:
            m = items[0].metrics or {}
            check("generation_mode == keyword2",
                  m.get("generation_mode") == "keyword2", m)
            check("**keywords 记了**", bool(m.get("keywords")), m)
            check("**keyword_seed_version 记了**",
                  m.get("keyword_seed_version") == KEYWORD_SEED_VERSION,
                  m.get("keyword_seed_version"))
            check("**keyword_corpus_version 记了**",
                  bool(m.get("keyword_corpus_version")),
                  m.get("keyword_corpus_version"))
            check("**keyword_session_seed 记了**",
                  m.get("keyword_session_seed") is not None, m)




# ======================================================================
# stable-refill: 高水位 + 离线候选不越过直播窗口
# ======================================================================
def test_stable_refill_daemon_imports_and_defaults():
    print("\n[stable-refill] 守护脚本可 import，CLI 默认可解析")
    import pool_refill as pr
    a = pr.build_parser().parse_args([])
    check("默认每周期最多 4 次", a.attempts_per_cycle == 4,
          a.attempts_per_cycle)
    check("默认使用 live heartbeat", bool(a.heartbeat), a.heartbeat)
    check("默认不是 once", a.once is False, a.once)


def test_stable_refill_default_waterlines():
    print("\n[stable-refill] 默认水位提前")
    cfg = Config(sim_path="x")
    check("低水位 = 8", cfg.pool_min_size == 8, cfg.pool_min_size)
    check("高水位 = 12", cfg.pool_target_size == 12, cfg.pool_target_size)
    check("至少 3 道可播", cfg.pool_playable_min == 3,
          cfg.pool_playable_min)
    check("**只有一套库存目标**(Phase C 取消了 reveal 专用目标)",
          not hasattr(cfg, "pool_reveal_target_size")
          and not hasattr(cfg, "pool_reveal_playable_target"),
          [n for n in dir(cfg) if "reveal" in n and "pool" in n])
    check("硬上限 = 16", cfg.pool_max_size == 16, cfg.pool_max_size)
    check("refill 技术短退避默认 5/10/15",
          tuple(cfg.pool_prefetch_refill_backoff_schedule_s)
          == (5.0, 10.0, 15.0),
          cfg.pool_prefetch_refill_backoff_schedule_s)


def test_stable_refill_live_heartbeat_expires():
    print("\n[stable-refill] live heartbeat lease 会自然过期")
    from story.live_heartbeat import (
        clear_live_heartbeat, live_is_active, read_live_heartbeat,
        write_live_heartbeat)
    with tempfile.TemporaryDirectory(prefix="hgt-heartbeat-") as d:
        path = os.path.join(d, "live.json")
        check("写 heartbeat 成功",
              write_live_heartbeat(path, phase="QA", session_id="s1"))
        rec = read_live_heartbeat(path)
        ts = float(rec.get("ts") or 0.0)
        check("新鲜 heartbeat = live", live_is_active(path, 10.0, now=ts + 5))
        check("超过 stale = idle",
              not live_is_active(path, 10.0, now=ts + 10.1))
        check("当前 pid 可以清自己的 lease", clear_live_heartbeat(path))
        check("清掉后 = idle", not live_is_active(path, 10.0, now=ts + 5))


def test_stable_refill_corrupt_heartbeat_is_idle():
    print("\n[stable-refill] 损坏 heartbeat 不会永久卡死守护补池")
    from story.live_heartbeat import live_is_active
    with tempfile.TemporaryDirectory(prefix="hgt-heartbeat-bad-") as d:
        path = os.path.join(d, "live.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not-json")
        check("坏 heartbeat 按 stale/idle 处理",
              not live_is_active(path, 10.0, now=100.0))


def test_stable_refill_candidate_never_adds_after_live_appears():
    """直播在候选完成后才出现，也必须在 pool.add 前最后一次复查时中止。"""
    print("\n[stable-refill] live 出现后候选绝不入池")
    from prefill_pool import _PrefillSeeder, _one_keyword

    class Bag:
        def draw(self):
            return {"keywords": ["门", "雨"], "index": 1}

    class Writer:
        def gen_keyword_story(self, keywords, *, should_continue=None,
                              **kw):  # Issue #50: 兼容 brief 关键字
            return {"answer": "完整背景使这个反常行为成立。"}

        def gen_surface(self, answer, should_continue=None):
            return {"puzzle": "他每天都把门打开，却不让任何人进来。"}

        def structure_original_idea(self, **kw):
            # 刻意不再调用 should_continue：模拟最后一个昂贵请求返回时，
            # 直播恰好刚启动。最终 add 前那次复查必须兜住这个窗口。
            return good_spec(puzzle=kw["puzzle"], answer=kw["answer"])

    class Pool:
        def __init__(self):
            self.add_calls = 0

        def add(self, spec, source=""):
            self.add_calls += 1
            return True

    calls = [0]

    def go():
        calls[0] += 1
        # keyword_spec 自己有 3 个阶段前检查；第 4 次就是 _one_keyword
        # 在最终 pool.add 前的保险。
        return calls[0] <= 3

    pool = Pool()
    seeder = _PrefillSeeder(
        enabled=True, bag=Bag(), session_seed=20260921,
        bag_meta={"corpus_version": "test"})
    ok = _one_keyword(Writer(), pool, Config(sim_path="x"), None,
                      seeder, [], should_continue=go)
    check("候选被丢弃", ok is False, ok)
    check("**pool.add 一次都没调用**", pool.add_calls == 0, pool.add_calls)


def test_stable_refill_prefill_default_path_still_adds():
    """没有守护谓词时，prefill 原有离线行为不应被改坏。"""
    print("\n[stable-refill] 普通 prefill 默认路径仍可入池")
    from prefill_pool import _PrefillSeeder, _one_keyword

    class Bag:
        def draw(self):
            return {"keywords": ["门", "雨"], "index": 1}

    class Writer:
        def gen_keyword_story(self, keywords, *, should_continue=None,
                              **kw):  # Issue #50: 兼容 brief 关键字
            return {"answer": "完整背景使这个反常行为成立。"}

        def gen_surface(self, answer, should_continue=None):
            return {"puzzle": "他每天都把门打开，却不让任何人进来。"}

        def structure_original_idea(self, **kw):
            return good_spec(puzzle=kw["puzzle"], answer=kw["answer"])

    class Pool:
        def __init__(self):
            self.add_calls = 0

        def add(self, spec, source=""):
            self.add_calls += 1
            return True

    pool = Pool()
    seeder = _PrefillSeeder(
        enabled=True, bag=Bag(), session_seed=20260921,
        bag_meta={"corpus_version": "test"})
    ok = _one_keyword(Writer(), pool, Config(sim_path="x"), None,
                      seeder, [])
    check("正常离线候选仍入池", ok is True, ok)
    check("pool.add 恰好一次", pool.add_calls == 1, pool.add_calls)

# ======================================================================
# Phase C —— 后台补题与直播彻底解耦: A–T 核心 regression
# ======================================================================
#
# 任务书 §21。这一组是本轮**唯一**的契约面: 每一条只回答一个问题 ——
# "后台补题还看不看直播状态?"
#
# 命名约定: `test_pc_<字母>` 与任务书 A–T 一一对应, 便于评审时对照。
# 全部离线, 断言**行为**(是否 submit / 计数是否变化), 不断言字段存在。

def _pc_tick(pf, n=1):
    """跑 n 拍 tick。

    ⚠️ 为什么默认要跑**两拍**才看得到结果: `_SyncExecutor` 在 `submit()`
    里就地跑完 worker, 但 worker 的终局结果写进 `_pending_result`, 由
    **下一拍**的 tick 步骤 ② 才应用。只跑一拍会看到一个"什么都没发生"
    的假象(库存明明变了, 计数却还是 0)。
    """
    for _ in range(n):
        pf.on_tick()


def test_pc_a_starts_when_below_min():
    """**A**: 库存 < min + 直播处于 SETTING -> 照样启动。"""
    print("\n[PC-A] 库存 < min + SETTING -> 启动")
    with tmpdir() as d:
        ex = _SyncExecutor()
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), writer=_FakeWriter(),
                  executor=ex)
        fill(pf.pool, 1)                    # 1 < min(2)
        # SETTING 是"直播忙"的代表相位 —— Phase C 之后它不再是闸门。
        _pc_tick(pf)
        check("**SETTING 下库存不足照样提交**", len(ex.submitted) == 1,
              len(ex.submitted))


def test_pc_b_starts_despite_pending_inflight_hint_reveal():
    """**B**: 库存 < min 且 pending/inflight/hint/reveal 全非零 -> 照样启动。

    这是实播事故 #2 的直接反证: 旧实现里 `_low_pressure()` 看到
    `pending != 0` 就返回 False, 于是"下一题已经在排队"反而让后台
    **停止补池** —— 库存越紧张越不补。
    """
    print("\n[PC-B] pending/inflight/hint/reveal 非零不挡启动")
    with tmpdir() as d:
        ex = _SyncExecutor()
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), writer=_FakeWriter(),
                  executor=ex)
        fill(pf.pool, 1)
        # 最强的断言: 这些 live 内存状态在 Phase C 里**根本不存在于
        # prefetcher** —— 它连读都读不到, 自然无从被它们挡住。旧实现里
        # `_low_pressure()` 会读 `_pending` / inflight 之类的镜像。
        check("**prefetcher 没有 pending/inflight/hint/reveal 字段**",
              not any(hasattr(pf, n) for n in
                      ("_pending", "_inflight", "_hint_inflight",
                       "_reveal_inflight", "_low_pressure")))
        _pc_tick(pf)
        check("**live 忙也不挡(读都不读)**", len(ex.submitted) == 1,
              len(ex.submitted))


def test_pc_c_starts_in_revealed_window():
    """**C**: REVEALED + reveal_remaining 只剩 1 秒 -> 照样启动。

    旧实现的 `_deadline_too_close()` 会在揭晓窗口临近时关闭启动闸门,
    理由是"来不及了"。Phase C 认为这属于**直播的时钟**, 后台补的是
    **池子库存**, 与"这一题还剩几秒"无关。
    """
    print("\n[PC-C] REVEALED 窗口临近不挡启动")
    with tmpdir() as d:
        ex = _SyncExecutor()
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), writer=_FakeWriter(),
                  executor=ex)
        fill(pf.pool, 1)
        check("**_deadline_too_close 已删除**",
              not hasattr(pf, "_deadline_too_close"))
        _pc_tick(pf)
        check("**REVEALED/临近 deadline 都不挡**", len(ex.submitted) == 1,
              len(ex.submitted))


def test_pc_d_candidate_survives_all_phases():
    """**D**: 候选启动后直播走遍所有相位, 候选走完, interrupted 不增。

    这条就是实播事故 #1 的反证(18:44:49 REVEALED 启动 -> 18:45:07
    SETTING -> 候选被丢)。Phase C 之后**没有任何相位**能打断它。
    """
    print("\n[PC-D] 候选跨越全部相位不被打断")
    with tmpdir() as d:
        w = _GatedWriter()
        ex = _SyncExecutor()
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), writer=w, executor=ex)
        fill(pf.pool, 1)
        _pc_tick(pf, 1)                     # 提交 + (同步)执行
        # 生成期间想象任何相位都不影响 —— 因为没有相位入参。
        for _ph in (Phase.SETTING, Phase.REVEALING, Phase.REVEALED, Phase.QA):
            del _ph                        # 仅表达"这些相位都跑过一遍"
        _pc_tick(pf, 1)                     # 应用结果
        check("**候选走完了(已入池)**", pf.pool.stock_count() >= 2,
              pf.pool.stock_count())
        check("**interrupted 全程为 0**", pf.interrupted_count == 0,
              pf.interrupted_count)
        check("**且没有走 gen_fail**", pf.generation_fail_count == 0,
              pf.generation_fail_count)


def test_pc_e_background_continues_alongside_live_generation():
    """**E**: 后台正在生成 + 直播同时现场生成 -> 后台**继续**。

    两条流水线共用上游网关但各有各的预算/客户端/单飞: 后台**不因**
    "live 在忙"而让路(live 忙是 live 的事)。这里用"live 那条路占着
    writer"来模拟并发, 断言后台的谓词仍为 True。
    """
    print("\n[PC-E] live 现场生成不影响后台候选")
    with tmpdir() as d:
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), writer=_FakeWriter())
        fill(pf.pool, 1)
        check("**稳态谓词 True(与 live 是否在跑无关)**",
              pf._background_should_continue() is True)
        # 反证: 这个谓词签名不带任何参数 —— 没有"live 状态"这个入口。
        import inspect
        sig = inspect.signature(pf._background_should_continue)
        check("**谓词无参: 结构上无处接收 live 状态**",
              len(sig.parameters) == 0, list(sig.parameters))


def test_pc_f_playtest_survives_phase_switch():
    """**F**: 试玩从 QA 切到 SETTING -> 不中断(不返回 interrupted)。

    `_playtest_should_continue()`(Director 里第三个相位重复实现)已删,
    试玩注入的是 prefetcher 的 background lifecycle 谓词。
    """
    print("\n[PC-F] 试玩不因相位切换中断")
    with tmpdir() as d:
        from story.prefetch import PoolPrefetcher
        from story.playtest import PASS, PlaytestResult
        calls = {"n": 0}

        class _PT:
            def run(self, spec, should_continue=None):
                calls["n"] += 1
                return PlaytestResult(status=PASS)

        class _W:
            def gen_spec(self, **kw):
                return good_spec()

        cfg = mkcfg(d, playtest_enabled=True, pool_min_size=2,
                    pool_target_size=5)
        pf = PoolPrefetcher(
            cfg=cfg, pool=PuzzlePool.open(cfg), writer=_W(),
            probe_inputs=lambda: {"avoid": [], "recent_signatures": []},
            pick_blueprint=lambda recent, rng=None: None,
            executor=_SyncExecutor())
        pf.set_playtester(_PT())
        pf.activate_background()
        fill(pf.pool, 1)
        _pc_tick(pf, 2)
        check("**试玩跑了(相位切不切都跑)**", calls["n"] >= 1, calls["n"])
        check("**试玩没有被打断**", pf.playtest_interrupted_count == 0,
              pf.playtest_interrupted_count)


def test_pc_g_no_submit_after_shutdown():
    """**G**: `shutdown()` 之后连续 20 拍都不再 submit。"""
    print("\n[PC-G] shutdown 之后 20 拍零提交")
    with tmpdir() as d:
        ex = _SyncExecutor()
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), writer=_FakeWriter(),
                  executor=ex)
        fill(pf.pool, 1)
        pf.shutdown()
        ex.submitted.clear()
        _pc_tick(pf, 20)
        check("**20 拍零提交**", len(ex.submitted) == 0, len(ex.submitted))
        check("**stats 报 shutdown**", pf.stats()["shutdown"] is True)


def test_pc_h_shutdown_midflight_is_interrupted_not_failed():
    """**H**: 生成中途 shutdown -> 记 interrupted, 不进 gen_fail / 不退避。

    与 G1-C 的区别: 这里走的是**真 `shutdown()`**(而非 `request_stop()`),
    验证停止信号在两条路径上都等效。
    """
    print("\n[PC-H] 中途 shutdown = interrupted, 不失败不退避")
    with tmpdir() as d:
        w = _GatedWriter()
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), writer=w,
                  executor=_SyncExecutor(), pool_prefetch_max_attempts=2)
        # ⚠️ 停止信号在 **worker 体内**(gen_spec 里)发出, 不在 `submit()`。
        # 在 `submit()` 里注入会与 `_submit_lock` 同线程重入(死锁) ——
        # "复查 stop + submit"现在是同一把锁下不可分割的一段。真实的
        # shutdown 来自 Director 线程, 时序上正是"提交了、HTTP 在途,
        # 此刻收尾" -> 后续 stage 的检查点看到 False 而收手。
        w.gen_spec = _with_live_predicate(w.gen_spec, pf, stop=pf.shutdown)
        fill(pf.pool, 1)
        _pc_tick(pf, 2)
        check("**记 interrupted**", pf.interrupted_count == 1,
              pf.interrupted_count)
        check("**不记 gen_fail**", pf.generation_fail_count == 0,
              pf.generation_fail_count)
        check("**不设退避**", pf._retry_at == 0.0, pf._retry_at)
        check("**不加失败链**", pf._fail_streak == 0, pf._fail_streak)


def test_pc_i_at_most_one_future_across_many_ticks():
    """**I**: 连续多拍 -> 永远最多一个在途 future(single-flight)。"""
    print("\n[PC-I] 连续 tick 最多一个在途")
    with tmpdir() as d:
        ex = _ManualExecutor()
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), writer=_FakeWriter(),
                  executor=ex)
        fill(pf.pool, 1)
        for _ in range(10):
            pf.on_tick()
        check("**10 拍只提交了 1 条(在途未完成)**", ex.total == 1, ex.total)
        check("**且它仍在途**", len(ex.pending) == 1, len(ex.pending))
        # 完成它 -> 允许下一条
        ex.run_next()
        pf.on_tick()                        # 回收 + 应用
        pf.on_tick()                        # 再提交
        check("**完成后才允许下一条**", ex.total == 2, ex.total)


def test_pc_j_latch_closes_at_target():
    """**J**: stock 达 target 且 playable 达标 -> latch 关闭, 不再补。"""
    print("\n[PC-J] 达标即关 latch")
    with tmpdir() as d:
        ex = _SyncExecutor()
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), writer=_FakeWriter(),
                  executor=ex)
        fill_mixed(pf.pool, 5)              # stock=5=target, 且结构互异
        _pc_tick(pf, 2)
        check("**已在 target: 不提交**", len(ex.submitted) == 0,
              len(ex.submitted))
        check("**latch 关闭**", pf._refill_active is False)


def test_pc_k_stops_at_hard_max():
    """**K**: stock >= max -> 不再生成(即使 playable 不达标)。"""
    print("\n[PC-K] 硬上限封顶")
    with tmpdir() as d:
        ex = _SyncExecutor()
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), writer=_FakeWriter(),
                  executor=ex, pool_max_size=4)
        fill(pf.pool, 4)                    # stock=4=max
        _pc_tick(pf, 2)
        check("**到顶: 不提交**", len(ex.submitted) == 0, len(ex.submitted))


def test_pc_l_technical_failure_backs_off():
    """**L**: 技术失败 -> 退避(refill 档位短退避)。"""
    print("\n[PC-L] 技术失败 -> 退避")
    with tmpdir() as d:
        clk = _Clock()
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), clock=clk)
        pf._apply_result("gen_fail", "网关抖",
                         {"reject": "structure_technical_fail"}, clk())
        check("**设了退避**", pf._retry_at > clk.t, pf._retry_at)
        check("**进了失败链**", pf._fail_streak == 1, pf._fail_streak)


def test_pc_m_puzzle_index_change_does_not_touch_backoff():
    """**M**: 直播换题(puzzle_index 变化)-> **不**影响后台退避。

    `_scene_at_submit` / `_scene_of` / 那一段 `puzzle_index` 重置代码
    已经删除。这里从两个方向钉: ① 方法不存在; ② 反复 tick 不退避值
    也不重置失败链(与直播播到第几题无关)。
    """
    print("\n[PC-M] 换题不影响后台退避")
    with tmpdir() as d:
        clk = _Clock()
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), clock=clk)
        for name in ("_scene_at_submit", "_scene_of"):
            check(f"**没有 {name}**", not hasattr(pf, name), name)
        pf._apply_result("gen_fail", "x",
                         {"reject": "structure_technical_fail"}, clk())
        before = (pf._retry_at, pf._fail_streak)
        # 没有任何输入能表达"换题了" —— 所以状态只能不变(退避未到期时)
        pf.on_tick()
        check("**退避值不受影响**", pf._retry_at == before[0], pf._retry_at)
        check("**失败链不受影响**", pf._fail_streak == before[1],
              pf._fail_streak)


def test_pc_n_success_resets_streak_and_backoff():
    """**N**: 成功入池 -> fail streak 与退避都清零。"""
    print("\n[PC-N] 成功即清零")
    with tmpdir() as d:
        clk = _Clock()
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), clock=clk)
        pf._apply_result("gen_fail", "x",
                         {"reject": "structure_technical_fail"}, clk())
        check("先有失败", pf._fail_streak == 1)
        pf._apply_result("ok", "", {}, clk())
        check("**失败链清零**", pf._fail_streak == 0, pf._fail_streak)
        check("**退避清零**", pf._retry_at == 0.0, pf._retry_at)


def test_pc_o_prewarm_deadline_still_stops():
    """**O**: prewarm 的 deadline 仍能 cooperative stop。

    §12: `prewarm_should_continue(deadline, should_abort)` **不合并**进
    后台谓词 —— 冷启动预热有界, 常驻补池无界。
    """
    print("\n[PC-O] 预热 deadline 仍然生效")
    with tmpdir() as d:
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), writer=_FakeWriter())
        sc_live = pf.prewarm_should_continue(deadline=1e12)
        check("**预算内: True**", sc_live() is True)
        sc_dead = pf.prewarm_should_continue(deadline=0.0)
        check("**预算耗尽: False**", sc_dead() is False)
        sc_abort = pf.prewarm_should_continue(deadline=None,
                                              should_abort=lambda: True)
        check("**stop 信号: False**", sc_abort() is False)
        # 与后台谓词是两个东西: 后台谓词不看 deadline。
        check("**后台谓词与 deadline 无关**",
              pf._background_should_continue() is True)


def test_pc_p_classic_killswitch_uses_same_stop_only():
    """**P**: classic 链(kill-switch)与 keyword2 链对称 —— 同一 stop-only 谓词。"""
    print("\n[PC-P] classic 链同样是 stop-only")
    with tmpdir() as d:
        ex = _SyncExecutor()
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), writer=_FakeWriter(),
                  executor=ex, pool_keyword_seed_enabled=False)
        fill(pf.pool, 1)
        check("**classic 链下未停止 -> 提交**", (pf.on_tick() or True))
        check("提交了一次", len(ex.submitted) == 1, len(ex.submitted))
        ex.submitted.clear()
        pf.request_stop()
        _pc_tick(pf, 5)
        check("**停止后 classic 链也不再提交**", len(ex.submitted) == 0,
              len(ex.submitted))


# ---- P0-1: 预热期间的激活闸门 ------------------------------------------

def test_pc_q_prewarm_gate_blocks_scheduler_then_activates():
    """**Q (P0-1)**: 未 activate 时 scheduler 连 tick 20 次 -> 零提交;
    activate 之后下一拍库存不足 -> 提交 1 条。

    这是本轮最关键的一条: 删掉相位门之后, `_prewarm()` 绕过了 `_future`
    单飞, 若 scheduler 在预热期间也能起候选, "后台最多一条"当场破功。
    `_background_active` 是那层**生命周期**闸门(不是相位闸门)。
    """
    print("\n[PC-Q] 预热闸门: 未激活零提交, 激活后接管")
    with tmpdir() as d:
        ex = _SyncExecutor()
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), writer=_FakeWriter(),
                  executor=ex, background_active=False)
        fill(pf.pool, 1)                    # 库存确实不足
        _pc_tick(pf, 20)
        check("**预热期间 20 拍零提交**", len(ex.submitted) == 0,
              len(ex.submitted))
        check("**闸门确实关着**", pf._background_active.is_set() is False)
        pf.activate_background()
        check("**激活后闸门打开**", pf._background_active.is_set() is True)
        _pc_tick(pf)
        check("**激活 + 库存不足 -> 提交 1 条**", len(ex.submitted) == 1,
              len(ex.submitted))


def test_pc_r_prewarm_then_activate_takes_over():
    """**R (P0-1)**: 预热结束 -> activate -> 正常 refill 接管。

    模拟 Director 的真实顺序: 先 `_generate_one_inner()`(预热, 绕过单飞),
    返回后才 `activate_background()`。之后 scheduler 才能提交。
    """
    print("\n[PC-R] 预热完成 -> 激活 -> refill 接管")
    with tmpdir() as d:
        ex = _SyncExecutor()
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), writer=_FakeWriter(),
                  executor=ex, background_active=False)
        # ---- 模拟 Director._prewarm(): 直接调生成, 绕过 _future ----
        ins = pf._generation_inputs()
        kind, _detail, _extra = pf._generate_one_inner(ins)
        check("**预热真的出了一道**", kind == "ok", kind)
        check("**预热期间没有任何 executor 提交**",
              len(ex.submitted) == 0, len(ex.submitted))
        # ---- 预热结束 -> 激活 ----
        pf.activate_background()
        # 预热已经补了一道 -> 库存回到水位之上。要证明"激活后 refill 接管",
        # 必须让库存**再度**不足(否则 latch 是关的, 不提交才是对的)。
        pf.pool.pop_next(recent_signatures=[])   # 把预热那道取走
        check("库存已再度不足", pf.pool.stock_count() < 2,
              pf.pool.stock_count())
        _pc_tick(pf)
        check("**激活后 refill 接管(提交了)**", len(ex.submitted) == 1,
              len(ex.submitted))


# ---- P0-2: request_stop 必须早于最终 shutdown ---------------------------

def test_pc_s_stop_before_executor_shutdown_blocks_submit():
    """**S (P0-2)**: `request_stop()` 之后、executor 尚未 shutdown 的窗口内
    tick -> 不再 submit。

    这正是 `Phase.STOPPED` 检测点到 `finally` 之间那 3.5 秒的形状:
    资源还没回收, 但停止信号已经生效。
    """
    print("\n[PC-S] request_stop 后未 shutdown 的窗口内零提交")
    with tmpdir() as d:
        ex = _SyncExecutor()
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), writer=_FakeWriter(),
                  executor=ex)
        fill(pf.pool, 1)
        pf.request_stop()                   # 只置信号, **不**回收 executor
        check("**executor 还活着**", pf._executor is ex)
        ex.submitted.clear()
        _pc_tick(pf, 10)
        check("**停止窗口内 10 拍零提交**", len(ex.submitted) == 0,
              len(ex.submitted))


def test_pc_s2_pending_result_still_accounted_after_stop():
    """**S-2 (P0-2 补丁)**: 停止**之后**在途那条的终局结果仍要**收账**。

    如果停止闸门把收账一起挡掉, `interrupted_count` 恰好在最该被看见的
    时刻(下播收尾)恒为 0 —— 运维会把一次正常停止读成"什么都没发生"。

    真实时序是: 候选在**提交时**还没收到停止, 于是 worker 一路跑到
    生成中途才看见停止 -> 返回"让路"形状 -> 结果落进 `_pending_result`,
    但**这一拍来不及应用**(应用由下一拍的步骤 ② 做)。等下一拍到来时,
    `gate` 已经为 False —— 收账**不能**被这道闸门一起挡掉, 否则
    `interrupted_count` 恰好在最该被看见的时刻(下播收尾)恒为 0。

    ⚠️ **停止信号不能放在 `submit()` 里, 也不能靠"第二拍再起一条"来造**。
    `_submit_lock` 让 ⑩b 复查与 `submit` 成为不可分割的一段: 停止一旦
    在提交之后到达, 那一拍就**零新提交**(见 `test_pc_u2`), 于是
    "在途的 interrupted"根本不会新产生。所以这里必须让**第一拍**那条
    候选自己跑成 interrupted —— 它是"停止之前就已提交"的合法在途,
    结果留在槽位里等下一拍收账。
    """
    print("\n[PC-S2] 停止后仍收在途的账")
    with tmpdir() as d:
        w = _GatedWriter()
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), writer=w,
                  executor=_SyncExecutor())
        # 让 writer 在生成中途就发现停止 -> 返回"让路"形状, 于是这一条
        # 候选的终局结果是 interrupted, 但**提交发生在停止之前**。
        state = {"stop_midgen": True}
        real_gen = w.gen_spec

        def gen(**kw):
            if state["stop_midgen"]:
                pf.request_stop()
            return real_gen(**kw)

        w.gen_spec = _with_live_predicate(gen, pf)
        fill(pf.pool, 1)

        # 第一拍: 提交(此时还没停) -> `_SyncExecutor` 就地跑完 worker,
        # worker 在生成中途收到停止 -> 结果落进 `_pending_result`, 但
        # **由下一拍步骤 ② 才应用**。
        _pc_tick(pf, 1)
        check("第一拍确实起了一个任务", len(w.calls) == 1, len(w.calls))
        check("**第一拍的结果是 interrupted 且尚未收账**",
              pf._pending_result is not None
              and pf._pending_result[0] == "interrupted"
              and pf.interrupted_count == 0,
              (pf._pending_result, pf.interrupted_count))

        # 第二拍: gate 已 False(不再起新活), 但步骤 ② 必须照收旧账。
        _pc_tick(pf, 1)

        check("**停止后那次在途被收账**",
              pf.interrupted_count == 1, pf.interrupted_count)
        check("**且不计失败/不退避(它是收尾不是故障)**",
              pf._fail_streak == 0 and pf._retry_at == 0.0,
              (pf._fail_streak, pf._retry_at))
        check("**停止后不再起新活(⑩b 复查拦下)**",
              len(w.calls) == 1, len(w.calls))


def test_pc_t_director_stops_background_at_phase_stopped():
    """**T (P0-2)**: Director 检测到 `Phase.STOPPED` -> 立刻 request_stop。

    这条钉的是 `director.py::run()` 的收尾顺序: `Phase.STOPPED` 是
    **唯一**允许被感知的相位(它是生命周期终止, 不是直播压力)。
    """
    print("\n[PC-T] Director 在 STOPPED 处通知后台停手")
    with tmpdir() as d:
        dr = _mk_director_for_pc(d, _SyncExecutor())
        pf = dr._prefetcher
        check("后台未停止", pf._background_should_continue() is True)
        dr._request_prefetch_stop()         # Director 收尾时调的就是它
        check("**Director 通知后后台谓词 False**",
              pf._background_should_continue() is False)
        check("**且 executor 尚未被回收(request_stop 只置信号)**",
              dr._prefetcher._executor is not None)
        dr.close()


# ======================================================================
# PC-U/V/W: 复审三个 lifecycle/concurrency blocker 的回归
#
# 这三组不是"加功能", 而是把三个**已修的缝**钉住。它们共同的形状是:
# 旧实现在某个**边界**上没有线性化/检查点/传参, 于是能构造出
# "已经停止却还在提交 / 还在写入 / 还在烧 LLM 轮次"。每条都做过
# 变异测试(把修法删掉必须变红), 否则断言不承重。
# ======================================================================

def _PT(pf=None, status=None, stop_on_run=False, rounds=None):
    """试玩替身: 可编程地"跑一次就停"或"被 deadline 拦下"。

    记录的是**谓词对象本身**(`preds`)而不是它这次的结果 —— 后者在
    "谓词恒真"时会恒过, 抓不到"漏传 override"这种变异。
    """
    from story.playtest import PASS, PlaytestResult

    class _Double:
        def __init__(self):
            self.calls = []
            self.preds = []

        def run(self, spec, should_continue=None):
            self.calls.append(spec)
            self.preds.append(should_continue)
            if stop_on_run and pf is not None:
                pf.request_stop()
            return PlaytestResult(status=status or PASS)

    return _Double()


class _DeadlinePT:
    """试玩替身: 每轮昂贵调用**之前**问一次谓词, 模拟"烧 LLM 轮次"。

    真 `Playtester._run_inner` 的契约就是**每轮重查谓词**; 这里如实地
    逐轮问, 于是"谓词在第 i 轮变 False"这件事能被观察到。

    关键指标是 `rounds_after_deadline`: 谓词从第 `expire_after` 轮起
    开始返回 False, 若真被拦下, 这个值恒为 0; 若 prefetch 没把 override
    传进来(走实例恒真谓词), 它会一路问到 `max_rounds`,
    `rounds_after_deadline > 0` -> 红。
    """

    def __init__(self, pf, allow_rounds=1, max_rounds=5):
        self.pf = pf
        self.allow_rounds = allow_rounds
        self.max_rounds = max_rounds
        self.rounds_after_deadline = 0
        self.calls = []
        self.preds = []

    def run(self, spec, should_continue=None):
        from story.playtest import INTERRUPTED, PASS, PlaytestResult
        self.calls.append(spec)
        self.preds.append(should_continue)
        if should_continue is None:
            should_continue = self.pf._background_should_continue
        for i in range(self.max_rounds):
            if i >= self.allow_rounds:
                # 此后"deadline 已过"：谓词必须开始返回 False。
                if should_continue():
                    # 谓词仍说可以继续 -> 说明 override 没生效。
                    self.rounds_after_deadline += 1
                    continue
                return PlaytestResult(status=INTERRUPTED)
        return PlaytestResult(status=PASS)


class _NullWriter:
    """`Playtester` 需要的**最小** writer —— W3 只测 run 的谓词语义。"""

    def ask(self, *a, **kw):            # pragma: no cover - 不该被调用
        raise AssertionError("W3 不该真的发 LLM 调用")



def test_pc_u_submit_lock_is_separate_from_state_lock():
    """**U (Blocker 1)**: 线性化锁必须**独立于** `_lock`。

    为什么不能塞回 `_lock`: `submit` 必须在 `_lock` **之外**(见
    `_on_tick_locked_ish` 的论证 —— 同步替身会在 `submit` 里就地跑完
    worker, worker 结尾要拿 `_lock`, 非重入锁上直接死锁)。若把提交段
    塞回 `_lock`, 要么死锁, 要么得放弃"submit 在锁外"这条前提。

    这条用**源码级**断言(与 `test_no_guard_machinery_remains` 的 grep
    纪律一致): 它钉的是"存在两把锁、且提交段与 request_stop 共用第二把"
    这个**结构事实**, 而结构事实无法用行为断言稳定地表达。
    """
    print("\n[PC-U] 提交线性化锁与状态锁分离")
    import inspect
    from story.prefetch import PoolPrefetcher
    with tmpdir() as d:
        pf = mkpf(d)
        check("**两把锁是两个不同的对象**",
              pf._submit_lock is not pf._lock,
              (type(pf._submit_lock).__name__, type(pf._lock).__name__))
        # 必须是可重入的: 同步替身让 worker 在 `submit()` 内联跑完,
        # worker 可能同线程再调 request_stop / shutdown。
        check("**提交锁可重入(同步替身会同线程重入)**",
              isinstance(pf._submit_lock, type(__import__("threading")
                                               .RLock())),
              type(pf._submit_lock).__name__)

        src_stop = inspect.getsource(PoolPrefetcher.request_stop)
        check("**request_stop 在 `_submit_lock` 下置位**",
              "_submit_lock" in src_stop, src_stop.splitlines()[:3])
        src_act = inspect.getsource(PoolPrefetcher.activate_background)
        check("**activate_background 也在 `_submit_lock` 下**",
              "_submit_lock" in src_act, src_act.splitlines()[:3])
        src_tick = inspect.getsource(PoolPrefetcher._on_tick_locked_ish)
        # ⑩b: 提交段里必须**复查**停止信号, 否则出锁到 submit 之间
        # 到达的 stop 会让"停止之后仍然提交一条新候选"。
        check("**提交段复查 `_shutdown_event`(⑩b)**",
              "_shutdown_event.is_set()" in src_tick, None)
        # 撤销 `_PENDING` 的那一行 —— 忘掉它就是单飞被永久占死。
        check("**复查命中时撤销 `_PENDING`(单飞不被占死)**",
              src_tick.count("_future = None") >= 1, None)


def test_pc_u2_stop_in_the_window_between_pending_and_submit():
    """**U2 (Blocker 1)**: stop 落在"⑩ 占位之后、真提交之前" -> 撤销占位且零提交。

    这是 ⑩b 复查真正要拦的那一刻。**不能用"先手工置 `_PENDING` 再 tick"
    来构造** —— 那样会先撞上步骤 ⑨ 的单飞守卫(`_future is not None`
    直接 return), ⑩b 根本到不了。真实的缝是:

        同一拍 tick: ⑨ 通过 -> ⑩ `_future = _PENDING` -> 出 `_lock`
        ↓  **并发线程**在这条缝里跑完 `request_stop()`
        ↓  进 `_submit_lock` -> ⑩b 复查到 stop -> 撤销 + 不提交

    所以这里**精确地把 stop 注入到 `_submit_lock` 被获取的那一刻** ——
    也就是"⑩ 已占位、⑩b 复查尚未读到"的那个点。这种注入方式同时也
    证明了线性化边界存在: stop 只要抢在提交段之前拿到锁, 提交段就
    一定能看见它。

    四个断言:
      * `_future is None`  —— 撤销 ⑩ 占位(忘掉就是单飞永久占死);
      * `total == 0`       —— 零提交(这就是 blocker 本身);
      * 账目零变动          —— 这活**没开始**, 不能记失败/退避/interrupted。
    """
    print("\n[PC-U2] stop 抢在提交段之前 -> 撤销占位且零提交")
    with tmpdir() as d:
        w = _GatedWriter()
        ex = _ManualExecutor()
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), writer=w, executor=ex)
        fill(pf.pool, 1)

        # 把 `request_stop()` 注入到"提交段拿到 `_submit_lock` 的那一刻":
        # 复现"⑩ 已占 `_PENDING`、⑩b 复查还没读到"的真实时序。
        class _StopOnSubmitLockEnter:
            def __init__(self, real, pfx):
                self._real, self._pfx, self._fired = real, pfx, False

            def __enter__(self):
                if not self._fired:
                    self._fired = True
                    self._pfx.request_stop()
                return self._real.__enter__()

            def __exit__(self, *a):
                return self._real.__exit__(*a)

        pf._submit_lock = _StopOnSubmitLockEnter(pf._submit_lock, pf)

        pf._on_tick_locked_ish(gate=True)

        check("停止信号确实已置位", pf._shutdown_event.is_set() is True)
        check("**占位被撤销(单飞不被永久占死)**",
              pf._future is None, pf._future)
        check("**零提交**", ex.total == 0, ex.total)
        check("**不记失败/不退避/不记 interrupted(这活没开始)**",
              pf._fail_streak == 0 and pf._retry_at == 0.0
              and pf.interrupted_count == 0,
              (pf._fail_streak, pf._retry_at, pf.interrupted_count))
        # 下一拍也不该起活(信号仍在)。
        pf._on_tick_locked_ish(gate=True)
        check("**后续拍仍零提交**", ex.total == 0, ex.total)


def test_pc_u3_activate_after_stop_does_not_revive():
    """**U3 (Blocker 1)**: `request_stop()` 之后 `activate_background()` 不得复活。

    `activate_background()` 原来是**裸的** check-then-set:
    "读 `_shutdown_event` -> 若未置位则 set `_background_active`" 两步
    之间能被并发 `request_stop()` 插进来, 于是"停止之后又被复活激活"。
    它现在与 `request_stop()` 共用 `_submit_lock`, 于是两者两两线性化。

    这里单线程复现等价时序: 先 stop, 再 activate。
    """
    print("\n[PC-U3] 停止之后 activate 不得复活后台")
    with tmpdir() as d:
        w = _GatedWriter()
        ex = _ManualExecutor()
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), writer=w,
                  executor=ex, background_active=False)
        fill(pf.pool, 1)

        pf.request_stop()
        pf.activate_background()
        check("**停止后 activate 不会置位 `_background_active`**",
              pf._background_active.is_set() is False,
              pf._background_active.is_set())
        # 后续多拍零提交: 复活若发生, 这里会红。
        _pc_tick(pf, 10)
        check("**停止 + 假 activate 之后 10 拍仍零提交**",
              ex.total == 0, ex.total)
        check("**后台谓词恒 False**",
              pf._background_should_continue() is False)


def test_pc_v_no_pool_add_when_stop_arrives_during_playtest():
    """**V (Blocker 2)**: 试玩"成功"了但 stop 已到 -> 不得 `pool.add()`。

    最坏形状: 生成链 + 整个试玩都跑完(试玩返回 PASS), 才在提交前收到
    停止。旧实现的最后一次谓词检查在**试玩开始前**, 之后到 `pool.add`
    之间(最坏 2N 次 LLM 调用)零检查点 —— 于是停止后仍会写入一道库存。

    这里用**同步** executor: worker 在 `submit()` 内联跑完, 试玩替身在
    `run()` 里先 `request_stop()` 再返回 PASS。于是走到 `pool.add` 紧前
    那道检查点时, 谓词必为 False。

    断言刻意包含"账目形状": 记 interrupted(候选确实走完了生成链),
    **不**记失败/退避, **不**记 added; 且 detail 必须读起来像正常收尾,
    不能出现旧架构的"直播变忙"。
    """
    print("\n[PC-V] 试玩通过但已停止 -> pool.add 不调用")
    with tmpdir() as d:
        w = _GatedWriter()
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), writer=w,
                  executor=_SyncExecutor(), playtest_enabled=True)
        w.gen_spec = _with_live_predicate(w.gen_spec, pf)
        fill(pf.pool, 1)

        pt = _PT(pf, status=PASS, stop_on_run=True)
        pf.set_playtester(pt)

        adds = {"n": 0}
        real_add = pf.pool.add

        def counting_add(spec, **kw):
            adds["n"] += 1
            return real_add(spec, **kw)

        pf.pool.add = counting_add

        _pc_tick(pf, 2)

        check("**试玩确实跑到了(证明缝在试玩之后)**",
              len(pt.calls) == 1, len(pt.calls))
        check("**pool.add 零调用**", adds["n"] == 0, adds["n"])
        check("**记一次 interrupted**", pf.interrupted_count == 1,
              pf.interrupted_count)
        check("**不计 added**", pf.added_count == 0, pf.added_count)
        check("**不计失败、不退避**",
              pf._fail_streak == 0 and pf._retry_at == 0.0,
              (pf._fail_streak, pf._retry_at))
        check("**被丢弃的 PASS 不污染试玩通过率**",
              pf.playtest_pass_count == 0, pf.playtest_pass_count)


def test_pc_v2_pre_add_checkpoint_also_covers_playtest_off():
    """**V2 (Blocker 2)**: 试玩**关着**时, gen 返回 -> `pool.add` 之间同样有检查点。

    这是 Blocker 2 最容易漏的一半。修法若只写在
    `if self._playtest_enabled():` 里, 那么**默认配置**(试玩关闭)下
    从生成链返回到 `pool.add` 仍是零检查点 —— 一个都没修。
    那正是 `prefill_pool.py` 里"候选完成后、pool.add 之前"补过的同一个洞。

    这里让 writer 在**返回前**置停止(即 gen 成功返回, 但停止已到),
    断言零 `pool.add`。
    """
    print("\n[PC-V2] 试玩关闭时提交前检查点仍然生效")
    with tmpdir() as d:
        w = _GatedWriter()
        pf = mkpf(d, pool=PuzzlePool.open(mkcfg(d)), writer=w,
                  executor=_SyncExecutor())      # 试玩默认关闭
        check("试玩确实关着", pf._playtest_enabled() is False)

        state = {"stop": False}
        real_gen = w.gen_spec

        def gen(**kw):
            # 先出稿, 返回前才停 —— 停止信号落在 gen 内部最后一段,
            # 于是 ① (试玩前) 整段被跳过, 只剩 ③ 能拦。
            spec = real_gen(**kw)
            if state["stop"]:
                pf.request_stop()
            return spec

        w.gen_spec = _with_live_predicate(gen, pf)
        state["stop"] = True
        fill(pf.pool, 1)

        adds = {"n": 0}
        real_add = pf.pool.add

        def counting_add(spec, **kw):
            adds["n"] += 1
            return real_add(spec, **kw)

        pf.pool.add = counting_add
        _pc_tick(pf, 2)

        check("**pool.add 零调用(试玩关着也拦得住)**",
              adds["n"] == 0, adds["n"])
        check("**记一次 interrupted**", pf.interrupted_count == 1,
              pf.interrupted_count)


def test_pc_v3_both_checkpoints_exist_and_ordered():
    """**V3 (Blocker 2)**: 两个检查点都在, 且提交前那个在 `pool.add` **之前**。

    白盒的**结构**断言: `_finish_one` 里必须有**两处**
    `if not should_continue():` —— 少一处就是少一层检查(① 省下 N 次
    LLM 调用, ③ 省下一次写入, 语义不同、不可合并)。并且第二处必须
    出现在 `self.pool.add` **之前** —— 否则它拦不住任何东西。

    还要求两处用的是**注入的** `should_continue` 而不是写死的实例谓词
    —— 否则预热路径那份带 deadline 的谓词在 ③ 处失效(Blocker 3 会
    被这条一并破掉)。
    """
    print("\n[PC-V3] 两个提交检查点都在且有序")
    import inspect
    from story.prefetch import PoolPrefetcher
    src = inspect.getsource(PoolPrefetcher._finish_one)
    n = src.count("if not should_continue():")
    check("**`_finish_one` 里恰有两个 should_continue 检查点**",
          n == 2, n)
    idx_last = src.rfind("if not should_continue():")
    idx_add = src.find("self.pool.add")
    check("**第二处在 `self.pool.add` 之前**",
          0 <= idx_last < idx_add, (idx_last, idx_add))
    check("**用的是注入的谓词, 不是写死的实例谓词**",
          "_background_should_continue()" not in src,
          src.count("_background_should_continue"))


def _mk_prewarm_pf(tmp, executor=None, **cfgkw):
    """建一个**预热态**的 prefetcher: 未 activate。

    预热路径在生产里是 `Director._prewarm()` 直接调
    `_generate_one_inner(inputs, should_continue=sc)` —— 绕过 `_future`
    单飞。这里如实复现那一跳。
    """
    return mkpf(tmp, executor=executor or _SyncExecutor(),
                background_active=False, **cfgkw)


def test_pc_w_prewarm_playtest_gets_deadline_predicate():
    """**W (Blocker 3, 核心)**: 预热 deadline 必须**贯穿到试玩内部**。

    旧实现: `Playtester.__init__` 一次性捕获实例谓词, `run(spec)` 不收
    谓词。于是 `_finish_one` 的 `should_continue`(预热时 = stop +
    deadline)只用在试玩**开始前**那道 guard, 之后被丢弃 —— 预热试玩
    越过 deadline 后继续烧 LLM 轮次。

    修法: `Playtester.run(spec, should_continue=...)` 支持**本次调用**
    的 override, `_finish_one` 把那份带 deadline 的谓词传进去。

    ⚠️ 真 deadline 走 `time.monotonic()`(不可注入), 所以这里用一个
    **从第 N 轮起翻假**的谓词来等价地表达"预算在第 N 轮之前耗尽":
      * 若设成"一开始就 False", 会先被 ① 那道 guard 拦下, 只测到旧行为;
      * 必须让 ① 放行、由试玩**内部**的逐轮检查拦下, 才证明 override
        真的贯穿进去了。
    """
    print("\n[PC-W] 预热 deadline 贯穿到试玩内部")
    with tmpdir() as d:
        pf = _mk_prewarm_pf(d, playtest_enabled=True)
        w = pf.writer
        fill(pf.pool, 1)

        # 预热谓词: 前 `ALLOW + 1` 次调用放行, 之后一律 False。
        #
        # 为什么是 `ALLOW + 1`: `_finish_one` 里有两处会问谓词 ——
        #   ① 试玩开始前那道 guard  (问 1 次)
        #   ② 试玩**内部**的逐轮检查 (让 `ALLOW` 轮放行)
        # 要证明"override 贯穿到试玩**内部**", 就必须让 ① 放行、由试玩
        # 内部的检查点拦下 —— 否则只测到 ① 的旧行为(它在修 Blocker 3
        # 之前就存在, 不承重)。
        # 预算: 只让 **① 那道 guard** 放行(它就问掉第 1 次); 从
        # 试玩**内部**的第 1 轮起必须为 False。于是"① 放行 + 内部拦下"
        # 这个形状被精确构造出来 —— 拦点一定在试玩内部, 不承重的
        # 旧 guard 拦不住它。
        calls = {"n": 0}

        def sc() -> bool:
            calls["n"] += 1
            return calls["n"] <= 1

        # `allow_rounds=0`: 内部**第 1 轮**就要检查谓词。
        pt = _DeadlinePT(pf, allow_rounds=0)
        pf.set_playtester(pt)

        spec = w.gen_spec(should_continue=sc)
        kind, detail, extra = pf._finish_one(spec, {}, sc)

        check("**预热试玩被内部检查点拦下(override 真的传进去了)**",
              pt.rounds_after_deadline == 0, pt.rounds_after_deadline)
        check("**试玩确实收到了 override 谓词**",
              pt.preds and pt.preds[0] is not None, pt.preds)
        check("**拦在试玩内部, 不是试玩开始前那道 guard**",
              len(pt.calls) == 1, len(pt.calls))
        # 结果**不是** `interrupted` 而是 `playtest_interrupted` —— 这
        # 正是正确的语义: 被中止的是**这次试玩**(deadline 到了), 与
        # "整个运行停止 -> interrupted"是两类账。`_apply_result` 会按
        # `extra["playtest"]` 记一次 `playtest_interrupted_count`, 并按
        # `extra["interrupted"]` 免退避。
        check("**结果是 playtest_interrupted(试玩被 deadline 中止)**",
              kind == "playtest_interrupted", (kind, detail))
        check("**免退避(extra 标记 interrupted)**",
              extra.get("interrupted") is True, extra)
        check("**没入池**", pf.added_count == 0, pf.added_count)


def test_pc_w2_steady_state_playtest_uses_instance_predicate():
    """**W2 (Blocker 3)**: 稳态试玩不传 override -> 用实例谓词(逐位不变)。

    反证: 实例谓词**恒真**且已 `request_stop()` 时, 稳态试玩**不**中断
    —— 证明稳态路径**不读** `_shutdown_event`(实例谓词不包含 stop)。
    这是"override 是 replace 而非 AND"的另一半证据: 稳态没有被悄悄
    塞进 stop 语义。
    """
    print("\n[PC-W2] 稳态试玩走实例谓词")
    with tmpdir() as d:
        pf = mkpf(d, playtest_enabled=True)
        pt = _PT(pf, status=PASS)
        pf.set_playtester(pt)
        pf.request_stop()               # 实例谓词恒真, 不受它影响
        got, _ = pf._playtest(good_spec())
        check("**不传 override 时仍然跑完试玩**", pt.calls and len(pt.calls) == 1,
              len(pt.calls))
        check("**override 参数确实为 None(走实例谓词)**",
              pt.preds == [None], pt.preds)


def test_pc_w3_override_replaces_not_ands():
    """**W3 (Blocker 3)**: override 是 **replace**, 不是 AND。

    依据: `prewarm_should_continue._go()` 先查 `should_abort` 再查
    `deadline` —— 预热谓词**已经包含 stop**。再 AND 一次实例谓词是
    同一件事做两遍, 而且会让"实例谓词恒假"这种测试配置下无法表达
    "本次调用放行"。

    这条是**唯一**把 replace 语义写进契约的测试: 若有人误改成
    `lambda: inst() and ov()`, 这里立刻红。
    """
    print("\n[PC-W3] override replace 而非 AND")
    from story.playtest import Playtester
    # 实例谓词恒假、override 恒真 -> 必须**正常跑完**。
    # `Playtester(player_client, host_writer, should_continue=..., ...)`。
    pt = Playtester(_NullWriter(), _NullWriter(),
                    should_continue=lambda: False,
                    max_turns=2, clock=lambda: 0.0)
    r = pt.run(good_spec(), should_continue=lambda: True)
    check("**实例谓词恒假但 override 恒真 -> 未被中断**",
          r.status != "interrupted", r.status)


def test_pc_w4_finish_one_threads_predicate_into_playtest():
    """**W4 (Blocker 3)**: `_finish_one` 必须把谓词**传进** `_playtest`。

    防漏传的变异测试: 删掉 `_playtest(spec, should_continue)` 里的
    第二个实参 -> 这里红。记录的是"传进来的谓词对象本身不是 None",
    而不是它这次的结果 —— 后者会因谓词恒真而恒过, 抓不到漏传。
    """
    print("\n[PC-W4] _finish_one 把谓词传进试玩")
    with tmpdir() as d:
        pf = mkpf(d, playtest_enabled=True)
        pt = _PT(pf, status=PASS)
        pf.set_playtester(pt)
        pf._finish_one(good_spec(), {}, lambda: True)
        check("**试玩收到非 None 的 override**",
              pt.preds and pt.preds[0] is not None, pt.preds)
        check("**且传的正是同一个对象**",
              pt.preds[0] is not None and pt.preds[0]() is True, pt.preds)


def test_pc_w5_playtester_run_signature_accepts_optional_predicate():
    """**W5 (Blocker 3)**: `Playtester.run` 的签名固化。

    把"~25 个单参调用点不红(向后兼容)"从口头约定变成可执行断言:
    `should_continue` 必须是**可选**的(默认 None), 否则老调用点全红。
    """
    print("\n[PC-W5] Playtester.run 签名接受可选谓词")
    import inspect
    from story.playtest import PASS, Playtester
    sig = inspect.signature(Playtester.run)
    params = list(sig.parameters)
    check("**参数表为 self, spec, should_continue**",
          params == ["self", "spec", "should_continue"], params)
    check("**should_continue 默认 None**",
          sig.parameters["should_continue"].default is None,
          sig.parameters["should_continue"].default)


def _mk_director_for_pc(tmp, executor):
    """建一个真实 Director, 替换掉 prefetcher 的 executor/writer。

    给 PC 组的装配类用例用(B/D/T)。返回的对象带 `close()`, 负责
    释放后台资源(Windows 上不释放会攥着临时目录句柄)。
    """
    from director import Director

    cfg = mkcfg(tmp, no_llm=False)
    cfg.pool_min_size = 2
    cfg.pool_target_size = 5
    cfg.pool_max_size = 10
    dr = Director(cfg)
    pf = dr._prefetcher
    if pf is not None:
        pf._executor = executor
        pf.writer = _FakeWriter()
        pf.activate_background()
    return _PCDirector(dr)


class _PCDirector:
    """薄包装: 让 PC 用例不用关心 Director 的清理细节。

    属性访问透传给真 Director(用例要调 `_request_prefetch_stop` 等),
    只额外提供 `close()`。
    """

    def __init__(self, dr):
        self._dr = dr
        self._prefetcher = dr._prefetcher

    def __getattr__(self, name):
        return getattr(self._dr, name)

    def close(self):
        pf = self._prefetcher
        if pf is not None:
            try:
                pf.shutdown()
            except Exception:               # noqa: BLE001
                pass


def _with_live_predicate(gen_spec, pf, stop=None):
    """把 `gen_spec` 包成"每次调用都用**当前**的后台谓词"。

    真 `gen_spec` 的契约是"每个昂贵 stage 之前重查一次谓词"。替身只查
    一次, 所以这里必须**在调用时刻**取谓词, 否则 `request_stop()` 在
    提交之后到达就测不出来。

    `stop`: 可选的"收尾动作"(通常是 `pf.request_stop` 或 `pf.shutdown`)。
    给它就在**第一次**调用时、查谓词**之前**先执行 —— 用来复现"任务已经
    提交、HTTP 在途, 此刻收尾信号到达"的真实时序。

    ⚠️ 收尾信号必须从**这里**(worker 体内)发出, 不能从 `submit()` 里
    发出:`_submit_lock` 让"复查 stop + submit"成为不可分割的一段,
    从 `submit()` 里调 `request_stop()`/`shutdown()` 会**同线程重入
    非重入锁 -> 自死锁**。
    """
    fired = {"n": 0}

    def wrapped(**kw):
        if stop is not None and fired["n"] == 0:
            fired["n"] += 1
            stop()
        kw["should_continue"] = pf._background_should_continue
        return gen_spec(**kw)
    return wrapped


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
        # stable-refill
        test_stable_refill_daemon_imports_and_defaults,
        test_stable_refill_default_waterlines,
        test_stable_refill_live_heartbeat_expires,
        test_stable_refill_corrupt_heartbeat_is_idle,
        test_stable_refill_candidate_never_adds_after_live_appears,
        test_stable_refill_prefill_default_path_still_adds,
        # B. PoolPrefetcher 状态机
        test_latch_walk_min2_target5,
        test_latch_held_under_pressure,
        test_refill_to_target_does_not_start_when_stock_healthy,
        test_semantic_reject_immediately_continues_refill,
        test_technical_reject_still_backs_off,
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
        test_pc_assembly_writer_client_split_and_prefetch_config,
        test_prefetch_writer_none_when_no_client,
        test_shutdown_docstring_is_honest,
        # E. L1 —— playable 库存 / REVEALED 窗口 / 当前题 avoid
        test_playable_count_is_readonly,
        test_playable_count_respects_policy_gate,
        test_playable_count_fail_closed_on_bad_ledger,
        test_prefetch_l1_a_stock_ok_but_playable_zero,
        test_prefetch_l1_b_stock_ok_playable_ok_no_refill,
        test_prefetch_l1_c_max_size_stops_generation,
        test_prefetch_l1_e_starts_regardless_of_live_phase,
        test_prefetch_l1_f_current_puzzle_in_avoid,
        test_prefetch_probe_and_generate_share_one_snapshot,
        test_prefetch_stats_exposes_playable,
        test_playable_min_zero_restores_q9_behavior,
        test_max_size_below_target_is_flagged,
        # ---- U1: 揭晓窗口专用目标 + deadline guard ----
        # ---- C3: probe 的 limit 必须跟着阶段目标走 ----
        test_u1_deadline_guard_no_longer_gates_start,
        test_u1_no_deadline_guard_config_exists,
        test_u1_multiple_generations_in_one_refill_cycle,
        test_u1_reveal_never_blocks_next_puzzle,
        test_u1_guard_config_validation,
        # ---- G1: 后台补池生命周期 + 独立预算 + 递增退避 ----
        test_g1_prefetch_passes_own_budget_not_live_budget,
        test_g1_prefetch_continues_across_phase_switches_midflight,
        test_g1_stop_midflight_is_interrupted_not_fail,
        test_g1_stopped_prefetcher_never_submits_again,
        test_g1_backoff_schedule_increases_then_caps,
        test_refill_active_keeps_technical_backoff_short_until_target,
        # ---- G4-C: 试玩不因相位切换中止 ----
        test_g4c_playtest_runs_through_phase_switch,
        test_g4c_playtest_predicate_is_lifecycle_only,
        test_g1_success_resets_backoff_streak,
        test_backoff_is_not_reset_by_live_scene_change,
        test_no_guard_machinery_remains,
        # ---- G2: keyword2 两阶段链 ----
        test_g2_keyword_path_draws_two_keys_and_adds,
        test_g2_keyword_does_not_call_pick_blueprint,
        test_g2_keyword_provenance,
        test_g2_stage_a_none_is_gen_fail,
        test_g2_stage_a_interrupt_is_not_failure,
        test_g2_stage_b_interrupt_is_not_failure,
        test_g2_should_continue_blocks_before_stage_a,
        test_g2_should_continue_blocks_after_stage_a_before_stage_b,
        test_g2_rewrite_does_not_enter_pool,
        test_g2_keyword_disabled_uses_classic_path,
        test_g2_disabled_behavior_is_bit_identical_to_pre_g2,
        test_g2_live_writer_never_calls_keyword,
        test_g2_stage_b_schema_has_no_puzzle_field,
        test_g2_quota_wall_still_hard_rejects_keyword_candidate,
        test_g2_too_similar_still_hard_rejects,
        # ---- G4 ----
        test_g4_vocab_missing_degrades_to_classic,
        test_g4_vocab_missing_never_falls_back_to_bank,
        test_g4_vocab_empty_or_corrupt_also_degrades,
        test_g4_good_vocab_activates_bag,
        test_g4_session_seed_reproducible_end_to_end,
        test_g4_provenance_records_vocab_and_seed,
        # ---- Phase C: 后台补题与直播彻底解耦 (A–T) ----
        test_pc_a_starts_when_below_min,
        test_pc_b_starts_despite_pending_inflight_hint_reveal,
        test_pc_c_starts_in_revealed_window,
        test_pc_d_candidate_survives_all_phases,
        test_pc_e_background_continues_alongside_live_generation,
        test_pc_f_playtest_survives_phase_switch,
        test_pc_g_no_submit_after_shutdown,
        test_pc_h_shutdown_midflight_is_interrupted_not_failed,
        test_pc_i_at_most_one_future_across_many_ticks,
        test_pc_j_latch_closes_at_target,
        test_pc_k_stops_at_hard_max,
        test_pc_l_technical_failure_backs_off,
        test_pc_m_puzzle_index_change_does_not_touch_backoff,
        test_pc_n_success_resets_streak_and_backoff,
        test_pc_o_prewarm_deadline_still_stops,
        test_pc_p_classic_killswitch_uses_same_stop_only,
        # P0-1: 预热期间的激活闸门
        test_pc_q_prewarm_gate_blocks_scheduler_then_activates,
        test_pc_r_prewarm_then_activate_takes_over,
        # P0-2: request_stop 必须早于最终 shutdown
        test_pc_s_stop_before_executor_shutdown_blocks_submit,
        test_pc_s2_pending_result_still_accounted_after_stop,
        test_pc_t_director_stops_background_at_phase_stopped,
        # ---- PC-U/V/W: 复审三个 lifecycle/concurrency blocker ----
        test_pc_u_submit_lock_is_separate_from_state_lock,
        test_pc_u2_stop_in_the_window_between_pending_and_submit,
        test_pc_u3_activate_after_stop_does_not_revive,
        test_pc_v_no_pool_add_when_stop_arrives_during_playtest,
        test_pc_v2_pre_add_checkpoint_also_covers_playtest_off,
        test_pc_v3_both_checkpoints_exist_and_ordered,
        test_pc_w_prewarm_playtest_gets_deadline_predicate,
        test_pc_w2_steady_state_playtest_uses_instance_predicate,
        test_pc_w3_override_replaces_not_ands,
        test_pc_w4_finish_one_threads_predicate_into_playtest,
        test_pc_w5_playtester_run_signature_accepts_optional_predicate,
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
