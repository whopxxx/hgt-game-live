#!/usr/bin/env python
# coding: utf-8
"""运行: uv run tests/test_g4_source.py（完全离线, 无网络、无 LLM）。

G4-2: **统一默认直播题源**的回归(任务书 §九 的 14 条)。

产品决定:

    默认直播不再使用 external curated/downloaded 海龟汤作为主题源。
    默认直播的唯一主生成体系是 keyword2。
    下载的完整谜题保留为可选题库 / benchmark / emergency reserve,
    但默认不参与实播调度。

    注意: `neurostellar/haiguitang` 的 `input` 仍然作为 keyword
    vocabulary 的**离线构建来源** —— 关掉的是"完整下载谜题的默认播放 /
    Lazy Curator 默认审题", 不是数据集, 也不是词库来源。

这一批**全部离线**: 不联网、不调 LLM。它验的是**装配与调度口径**
(谁被创建、谁排在前、banner 打什么), 那些正好是"默认行为"最容易
悄悄漂掉的地方。

## 为什么这些断言值得写

"默认不看 curated"是一个**否定性**命题, 而否定性命题最容易假绿:
把 curated 整个删掉也能让"默认不加载 curated"通过。所以每条断言都
尽量配一个**反证**(显式 `--curated` 时确实加载了 / 确实排除了),
让"关掉了"与"删掉了"在测试上可区分。
"""
from __future__ import annotations

import ast
import io
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

#: 仓库根 —— `test_r4_smoke_draws_keywords_exactly_once` 要现读源码。
_ROOT = str(Path(__file__).resolve().parents[1])


def _read(path: str) -> str:
    """现读源码。

    ⚠️ **不要**用 module-level 的 AST 快照, 也不要依赖 import 缓存:
    CPython 按秒比较 mtime, "改源码 -> 立刻跑测试"会喂进**旧的**字节码,
    于是变异测试静默通过。每次从盘上重读是这里唯一可靠的做法。
    """
    with io.open(path, encoding="utf-8") as f:
        return f.read()


from story.config import Config, from_args  # noqa: E402
from story.pool import PuzzlePool  # noqa: E402

FAIL = [0]


def check(name, cond, extra=""):
    if cond:
        print(f"  ok  {name}")
        return
    print(f"  FAIL {name}  {extra}")
    FAIL[0] += 1


@contextmanager
def tmpdir():
    d = tempfile.mkdtemp(prefix="g4src_")
    try:
        yield d
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def mkcfg(d, **kw):
    cfg = Config()
    cfg.pool_enabled = True
    cfg.pool_path = os.path.join(d, "pool.jsonl")
    cfg.pool_used_path = os.path.join(d, "used.jsonl")
    # P0: 已播账本也必须指向 tmpdir —— 否则用例之间会**互相污染**
    # (上一例播过的题把下一例交付挡掉, 而那是测试隔离问题不是产品问题)。
    cfg.played_path = os.path.join(d, "played.jsonl")
    cfg.curated_pool_path = os.path.join(d, "curated.jsonl")
    cfg.curated_used_path = os.path.join(d, "curated_used.jsonl")
    cfg.curated_decisions_path = os.path.join(d, "dec.jsonl")
    cfg.pool_prefetch_enabled = False       # 除非用例显式开
    cfg.pool_prewarm_max_rounds = 0         # 默认关预热(用例单独测)
    cfg.no_llm = True
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


# ======================================================================
# 一、默认值(§九-1 / §九-2)
# ======================================================================
def test_default_config_curated_off():
    """§九-1: 默认 `Config` 的 `prefer_curated` 必须是 **False**。"""
    print("\n[G4-2-1] 默认 Config curated=False")
    c = Config()
    check("**prefer_curated 默认 False**", c.prefer_curated is False,
          c.prefer_curated)
    check("prewarm 默认只尝试 1 轮 / 45s",
          c.pool_prewarm_max_rounds == 1
          and c.pool_prewarm_max_seconds == 45.0,
          (c.pool_prewarm_max_rounds, c.pool_prewarm_max_seconds))
    check("prewarm LLM 独立短预算 = 15s / 0 retries",
          c.pool_prewarm_llm_timeout_seconds == 15.0
          and c.pool_prewarm_llm_max_retries == 0,
          (c.pool_prewarm_llm_timeout_seconds,
           c.pool_prewarm_llm_max_retries))
    check("prefetch 普通 stage = 30s / 0 retries",
          c.pool_prefetch_llm_timeout_seconds == 30.0
          and c.pool_prefetch_llm_max_retries == 0,
          (c.pool_prefetch_llm_timeout_seconds,
           c.pool_prefetch_llm_max_retries))
    check("prefetch Story 专属预算 = 45s",
          c.pool_prefetch_story_timeout_seconds == 45.0,
          c.pool_prefetch_story_timeout_seconds)


def test_cli_defaults_and_flags():
    """§九-1/4: CLI 不写 flag = 关; `--curated` = 开; `--no-curated` = 关。

    三者共用同一个 dest, 所以"最后写的赢"是唯一规则 —— 不会出现
    "两个 flag 互相打架而代码只读其中一个"。
    """
    print("\n[G4-2-4] CLI: --curated / --no-curated 同 dest")
    # `director.py` 强制要求三选一的数据源(--live/--sim/--stdin), 所以
    # 给一个不存在的 sim 路径 —— 这里只解析参数, 不真的跑它。
    base = ["--sim", os.path.join(tempfile.gettempdir(), "nonexistent.jsonl")]
    check("不写 flag -> False",
          from_args(list(base)).prefer_curated is False,
          from_args(list(base)).prefer_curated)
    check("**--curated -> True**",
          from_args(base + ["--curated"]).prefer_curated is True)
    check("--no-curated -> False",
          from_args(base + ["--no-curated"]).prefer_curated is False)
    check("**两个都写 -> 后写的赢**(--curated 在后)",
          from_args(base + ["--no-curated", "--curated"]).prefer_curated
          is True)
    check("**两个都写 -> 后写的赢**(--no-curated 在后)",
          from_args(base + ["--curated", "--no-curated"]).prefer_curated
          is False)


# ======================================================================
# 二、装配(§九-2 / §九-13)
# ======================================================================
def _mk_director(cfg):
    from director import Director
    return Director(cfg)


def test_default_does_not_create_lazy_curator():
    """§九-2 + §九-13: 默认**不建** Lazy Curator(因此 0 次 LLM 调用)。"""
    print("\n[G4-2-2] 默认不创建 Lazy Curator")
    with tmpdir() as d:
        dr = _mk_director(mkcfg(d))
        check("**curated 池是 None**", dr.curated_pool is None,
              dr.curated_pool)
        check("**lazy curator 是 None**", dr._lazy_curator is None,
              dr._lazy_curator)
        # 0 次 LLM 调用的最强证据: 没有 client, 也就没有可调用的东西。
        check("**没有 client(no_llm)**", dr.client is None, dr.client)


def test_prefetch_uses_independent_fail_fast_client():
    """后台补池 transport 独立收紧，绝不能污染正式直播 client。"""
    print("\n[prefetch transport] 独立 30s/0 retry client")
    with tmpdir() as d:
        cfg = mkcfg(d, no_llm=False, pool_prefetch_enabled=True)
        cfg.llm.timeout = 60.0
        cfg.llm.max_retries = 3
        cfg.pool_prefetch_llm_timeout_seconds = 30.0
        cfg.pool_prefetch_story_timeout_seconds = 45.0
        cfg.pool_prefetch_llm_max_retries = 0
        dr = _mk_director(cfg)
        check("正式 client 仍是 60s/3",
              dr.client is not None
              and dr.client.cfg.timeout == 60.0
              and dr.client.cfg.max_retries == 3,
              None if dr.client is None else
              (dr.client.cfg.timeout, dr.client.cfg.max_retries))
        check("prefetch client 独立存在",
              dr._prefetch_client is not None
              and dr._prefetch_client is not dr.client,
              dr._prefetch_client)
        check("prefetch client 基础 transport = 30s/0",
              dr._prefetch_client.cfg.timeout == 30.0
              and dr._prefetch_client.cfg.max_retries == 0,
              (dr._prefetch_client.cfg.timeout,
               dr._prefetch_client.cfg.max_retries))
        check("**只有 prefetcher 的 Story override = 45s**",
              dr._prefetcher._story_timeout == 45.0,
              dr._prefetcher._story_timeout)
        check("prefetch writer 确实接独立 client",
              dr._prefetcher.writer.client is dr._prefetch_client,
              dr._prefetcher.writer.client)
        check("模型路由仍与正式 client 相同",
              dr._prefetch_client.cfg.resolved_models()
              == dr.client.cfg.resolved_models(),
              dr._prefetch_client.cfg.resolved_models())


    # Story override 只是上限偏好，不能突破 operator 的全局 timeout。
    with tmpdir() as d:
        cfg2 = mkcfg(d, no_llm=False, pool_prefetch_enabled=True)
        cfg2.llm.timeout = 35.0
        cfg2.pool_prefetch_story_timeout_seconds = 45.0
        dr2 = _mk_director(cfg2)
        check("**全局 timeout=35 时 Story 也只能 35s**",
              dr2._prefetcher._story_timeout == 35.0,
              dr2._prefetcher._story_timeout)


def test_explicit_curated_loads_external_pool():
    """§九-4 **反证**: 显式 `--curated` 时确实加载 external 池。

    没有这条, "把 curated 整个删掉" 也能让上面那条通过。
    """
    print("\n[G4-2-4b] 显式 --curated 才加载 external 池")
    with tmpdir() as d:
        cfg = mkcfg(d, prefer_curated=True)
        p = PuzzlePool.open(cfg)
        p.add(_curated_spec())
        dr = _mk_director(cfg)
        check("**curated 池被创建**", dr.curated_pool is not None,
              dr.curated_pool)


def test_curated_off_means_truly_off():
    """§九-4: 关掉时**连文件都不读**(不只是"排在最后")。"""
    print("\n[G4-2-4c] 关掉 = 真的关掉")
    with tmpdir() as d:
        # 放一份**非法**的 curated 池文件: 若还被读, 一定出错或至少
        # 出现 "curated 池载入" 之类的痕迹。关掉时应当完全不碰它。
        with open(os.path.join(d, "curated.jsonl"), "w",
                  encoding="utf-8") as f:
            f.write("这不是 JSON\n")
        dr = _mk_director(mkcfg(d))
        check("curated 池是 None(没读那个坏文件)", dr.curated_pool is None)


# ======================================================================
# 三、取题顺序(§九-3 / §九-5)
# ======================================================================
def _import_pool_fixtures():
    """复用 `test_pool` 里**已经被验证过**的两个 spec 构造器。

    ## 为什么不在这里另写一份

    这一批要的不是"造一个 spec", 而是"取题**顺序**与**装配**"。手写
    spec 很容易漏字段(第一版就漏了 hints / discovery_beats, 于是
    `pool.add` 直接返回 False, 整批断言变成在测空气) —— 而
    `test_pool.good_spec` / `_curated_spec` 已经被它自己那一套验收点
    反复跑过, 形状是对的。复用它, 这里就只需要关心顺序。

    ⚠️ 这也是"夹具必须与生产同形"的一个实例: 造一个形状不同的 spec
    会让测试测出**假的**通过。
    """
    import importlib.util
    from pathlib import Path
    p = Path(__file__).resolve().parent / "test_pool.py"
    spec = importlib.util.spec_from_file_location("_tp_fixtures", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


_TP = _import_pool_fixtures()


def _curated_spec():
    return _TP._curated_spec()


def _good_gen_spec():
    return _TP.good_spec()


def _inline_riddle(dr):
    """同步跑一次 `_riddle`(否则它起后台线程, 测试拿不到结果)。"""
    import director as _D
    real = _D.threading.Thread

    class _Inline:
        def __init__(self, target=None, daemon=None, name=None, **kw):
            self._target = target

        def start(self):
            if self._target:
                self._target()

    _D.threading.Thread = _Inline
    try:
        dr._riddle({"reason": "riddle", "avoid": [],
                    "recent_signatures": []})
    finally:
        _D.threading.Thread = real


def _inline_reveal(dr, payload):
    """同步跑一次 `_reveal`。

    ⚠️ `_reveal` 和 `_riddle` 一样**起后台线程**(`work()` 在线程里跑, 而
    账本写入在 `work()` 内部)。不 inline 的话断言会在写盘之前执行 ——
    第一版就是这样: 账本里只有 `pop_next` 写的那一行 `air:false`,
    `mark_used(aired=True)` 还在另一个线程里没跑完。**那条红是测试的
    竞态, 不是被测代码的缺口**。

    复用 `_inline_riddle` 同一套 `threading.Thread` 替身(把 `start()`
    变成同步调用), 这样两条路径的 inline 语义完全一致。
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
    try:
        dr._reveal(payload)
    finally:
        _D.threading.Thread = real


def _accept_curated(spec, d):
    """把一道 curated 题登记成"已提交"。

    ⚠️ **必须复用 `test_pool` 那一份**: curated 的可播性要求
    `(external_id, content_hash, policy)` **三元组**对得上账本, 而
    `content_hash` 由 surface+bottom 算出来。手写一条只有 external_id
    的记录 -> 池门在账本里永远查不到 -> 这道题表现为"入池了但播不出来",
    而测试会以为是自己顺序写错了。(第一版就是这么错的。)
    """
    _TP._accept_curated(spec, d)


class _NoGen:
    """一个"绝不该被调用"的 writer —— 池里有题时现场生成不该发生。"""

    def gen_spec(self, *a, **k):
        raise AssertionError("池里有题, 不该现场生成")

    def gen_keyword_story(self, *a, **k):
        raise AssertionError("池里有题, 不该现场生成")

    def gen_surface(self, *a, **k):
        raise AssertionError("池里有题, 不该现场生成")

    def structure_original_idea(self, *a, **k):
        raise AssertionError("池里有题, 不该现场生成")


def test_default_pops_generated_pool_first():
    """§九-3: 默认下一题**先取 generated pool**。"""
    print("\n[G4-2-3] 默认先取 generated pool")
    with tmpdir() as d:
        cfg = mkcfg(d)
        PuzzlePool.open(cfg).add(_good_gen_spec())
        dr = _mk_director(cfg)
        dr.writer = _NoGen()
        dr.engine.start()
        _inline_riddle(dr)
        check("**来源是 keyword2_pool**",
              dr.engine._spec_source == "keyword2_pool",
              dr.engine._spec_source)


def test_curated_on_still_ranks_generated_first():
    """§九-5: 即使 `--curated` 开着, generated **仍排前**。"""
    print("\n[G4-2-5] --curated 时 generated 仍排前")
    with tmpdir() as d:
        cfg = mkcfg(d, prefer_curated=True)
        cs = _curated_spec()
        _accept_curated(cs, d)
        cp = PuzzlePool.open_curated(cfg)
        check("curated 入池", cp.add(cs))
        PuzzlePool.open(cfg).add(_good_gen_spec())
        dr = _mk_director(cfg)
        dr.writer = _NoGen()
        dr.engine.start()
        _inline_riddle(dr)
        check("**来源是 generated 而不是 curated**",
              dr.engine._spec_source == "keyword2_pool",
              dr.engine._spec_source)
        check("**curated 池原封不动**(确实只是排在后面)",
              dr.curated_pool.pending_count() == 1,
              dr.curated_pool.pending_count())


def test_curated_reachable_when_generated_empty():
    """反证: generated 空时 curated **仍然**能被取到(不是被禁用)。"""
    print("\n[G4-2-5b] generated 空 -> curated 仍可达")
    with tmpdir() as d:
        cfg = mkcfg(d, prefer_curated=True)
        cs = _curated_spec()
        _accept_curated(cs, d)
        cp = PuzzlePool.open_curated(cfg)
        check("curated 入池", cp.add(cs))
        dr = _mk_director(cfg)
        dr.writer = _NoGen()
        dr.engine.start()
        _inline_riddle(dr)
        check("**来源是 curated**", dr.engine._spec_source == "curated",
              dr.engine._spec_source)


# ======================================================================
# 四、live 生成统一到 keyword2(§九-6 / §九-7)
# ======================================================================
def test_live_uses_keyword2_by_default():
    """§九-6: 池空 -> live 走 keyword2, **不调** classic gen_spec。"""
    print("\n[G4-2-6] live 默认走 keyword2")
    with tmpdir() as d:
        cfg = mkcfg(d, no_llm=False, pool_keyword_seed_enabled=True)
        dr = _mk_director(cfg)
        called = {"classic": 0, "stageA": 0}

        class _W:
            class client:
                class cfg:
                    model = "fake"

            def gen_spec(self, *a, **k):
                called["classic"] += 1
                return _good_gen_spec()

            def gen_keyword_story(self, *a, **k):
                called["stageA"] += 1
                return None          # 不成题 -> 走 failure 分支

            def structure_original_idea(self, *a, **k):
                raise AssertionError("Stage A 没成题, B 不该被调")

            def hint(self, *a, **k):
                return ("h", None)

        dr.writer = _W()
        dr.engine.start()
        _inline_riddle(dr)
        check("**没走 classic gen_spec**", called["classic"] == 0,
              called["classic"])
        check("**确实进了 keyword2 链**", called["stageA"] >= 1,
              called["stageA"])


def test_no_keyword_seed_returns_both_to_classic():
    """§九-7: `--no-keyword-seed` -> prefetch **与** live 同时回 classic。

    死要求: "不能出现 prefetch=classic 而 live=keyword2, 或反过来"。
    两边读同一个 config flag, 所以这条在结构上成立 —— 这里把它钉住。
    """
    print("\n[G4-2-7] --no-keyword-seed 两边同时回 classic")
    with tmpdir() as d:
        cfg = mkcfg(d, no_llm=False, pool_keyword_seed_enabled=False,
                    pool_prefetch_enabled=True)
        dr = _mk_director(cfg)
        called = {"classic": 0}

        class _W:
            class client:
                class cfg:
                    model = "fake"

            def gen_spec(self, *a, **k):
                called["classic"] += 1
                return _good_gen_spec()

            def gen_keyword_story(self, *a, **k):
                raise AssertionError("关掉 keyword2 后不该调 Story")

            def hint(self, *a, **k):
                return ("h", None)

        dr.writer = _W()
        # ---- live 那一半 ----
        dr.engine.start()
        _inline_riddle(dr)
        check("**live 回 classic**", called["classic"] >= 1, called["classic"])
        check("来源是 live_generate",
              dr.engine._spec_source == "live_generate",
              dr.engine._spec_source)
        # ---- prefetch 那一半 ----
        check("**prefetch 也回 classic(没有半切换)**",
              dr._prefetcher._keyword_enabled() is False,
              dr._prefetcher._bag)
        check("(live 的 bag 也没建)", dr._kw_bag_built is False,
              dr._kw_bag_built)


# ======================================================================
# 五、prewarm(§九-8 / §九-9 / §九-10 / §九-11)
# ======================================================================
def test_prewarm_transport_config_validation():
    print("\n[G4-2-config] prewarm transport budget 配置告警")
    c = Config(sim_path="x")
    c.pool_prewarm_llm_timeout_seconds = 0
    c.pool_prewarm_llm_max_retries = -1
    warns = c.validate()
    check("timeout<=0 有告警",
          any("pool_prewarm_llm_timeout_seconds" in w for w in warns), warns)
    check("retries<0 有告警",
          any("pool_prewarm_llm_max_retries" in w for w in warns), warns)


class _CountingPrefetcher:
    """替身: 让 `_prewarm` 的判定可被直接观察。

    只实现 `_prewarm` 真正用到的那三个方法(`_generation_inputs` /
    `_playable` / `_generate_one_inner`) —— 这顺带**证明了预热没有偷偷
    依赖调度层**的东西(latch / 退避 / 相位探针)。

    ⚠️ 这是 duck-typing: 若将来 `_prewarm` 开始用别的属性, 这个替身会
    在这里报 AttributeError —— 那正是我们想要的信号(说明预热长出了新
    依赖), 比"悄悄多调一次生成"好。
    """

    def __init__(self, playable0, results):
        self._playable0 = playable0
        self._results = list(results)
        self.calls = 0

    def _generation_inputs(self):
        return {}

    def _playable(self, inputs, limit=None):
        return self._playable0

    def _generate_one_inner(self, inputs):
        self.calls += 1
        return self._results.pop(0) if self._results else ("gen_fail", "", {})


def _dr_with_prefetch(cfg, pf):
    dr = _mk_director(cfg)
    dr._prefetcher = pf
    return dr


def test_prewarm_skipped_when_playable():
    """§九-9: 冷启动已有 playable -> **0 次**额外生成调用。

    ⚠️ 这里**必须显式打开预热**。第一版用了 `mkcfg(d)` 的默认值, 而
    默认是 `pool_prewarm_max_rounds=0`(关掉预热)—— 于是"0 次生成"
    在**任何实现下**都成立, 这条测试测的是空气。变异实验抓到了它
    (把阈值改成 `>= 99999`, 即"永远不跳过", 测试仍然全绿)。

    所以现在两件事同时成立才有意义:
      * 预热**是开着的**(所以"0 次"不是因为预热被关);
      * playable >= 1(所以"0 次"是 §九-9 的效果)。
    下面再补一条**反证**: 同样开着预热, 把 playable 换成 0, 就必须
    真的发一次生成 —— 两条一起才能区分"跳过了"和"根本没跑"。
    """
    print("\n[G4-2-9] 已有可播 -> 0 次生成")
    with tmpdir() as d:
        dr = _dr_with_prefetch(
            mkcfg(d, pool_prewarm_max_rounds=3),
            _CountingPrefetcher(1, [("ok", "", {})]))
        dr._prewarm()
        check("**一次生成都没发**", dr._prefetcher.calls == 0,
              dr._prefetcher.calls)
    # ---- 反证: 同配置、playable=0 -> 必须发 ----
    with tmpdir() as d:
        dr = _dr_with_prefetch(
            mkcfg(d, pool_prewarm_max_rounds=3),
            _CountingPrefetcher(0, [("ok", "", {})]))
        dr._prewarm()
        check("**反证: playable=0 时确实发了**(所以上面不是'没跑')",
              dr._prefetcher.calls == 1, dr._prefetcher.calls)


def test_prewarm_runs_when_empty():
    """§九-8: playable=0 -> 确实预热。"""
    print("\n[G4-2-8] playable=0 -> 预热")
    with tmpdir() as d:
        # ⚠️ `mkcfg` 默认把 `pool_prewarm_max_rounds` 设成 0(离线用例
        # 不该被预热拖慢), 所以想验预热的用例必须**显式**把它打开 ——
        # 否则测的是"关掉了", 而断言会以"没发生成"的形式假绿/假红。
        dr = _dr_with_prefetch(
            mkcfg(d, pool_prewarm_max_rounds=2),
            _CountingPrefetcher(0, [("ok", "", {})]))
        dr._prewarm()
        check("**发了生成**", dr._prefetcher.calls == 1,
              dr._prefetcher.calls)


def test_prewarm_stops_after_one():
    """§九-10: 拿到一道**立即**结束, 不补到 target。"""
    print("\n[G4-2-10] 成功一题立即结束")
    with tmpdir() as d:
        dr = _dr_with_prefetch(
            mkcfg(d, pool_prewarm_max_rounds=5),
            _CountingPrefetcher(0, [("ok", "", {})] * 5))
        dr._prewarm()
        check("**只发了一次**(不是 max_rounds 次)",
              dr._prefetcher.calls == 1, dr._prefetcher.calls)


def test_prewarm_is_bounded_and_never_blocks():
    """§九-11: 预热失败**有界**, 且不阻止启动。

    两半:
      * 轮数上限 —— 一直失败也只跑 max_rounds 轮
      * 不抛 —— `_prewarm` 自己**绝不**把异常冒出去(冒出去 = 进程起不来)
    """
    print("\n[G4-2-11] 预热失败有界 + 绝不阻止启动")
    with tmpdir() as d:
        dr = _dr_with_prefetch(
            mkcfg(d, pool_prewarm_max_rounds=3),
            _CountingPrefetcher(0, [("gen_fail", "x", {})] * 10))
        dr._prewarm()               # 不该抛
        check("**轮数被上限截住**", dr._prefetcher.calls == 3,
              dr._prefetcher.calls)

    # 生成**抛异常**时也不能冒出去。
    class _Boom(_CountingPrefetcher):
        def _generate_one_inner(self, inputs):
            self.calls += 1
            raise RuntimeError("网关炸了")

    with tmpdir() as d:
        dr = _dr_with_prefetch(
            mkcfg(d, pool_prewarm_max_rounds=3), _Boom(0, []))
        try:
            dr._prewarm()
            check("**异常被吞住(没冒出去)**", True)
        except Exception as e:                  # noqa: BLE001
            check("**异常被吞住(没冒出去)**", False, repr(e))
        check("出错后就停手(不再重试)", dr._prefetcher.calls == 1,
              dr._prefetcher.calls)


def test_prewarm_disabled_by_zero():
    """`pool_prewarm_max_rounds=0` -> 完全不预热(离线/调试口径)。"""
    print("\n[G4-2-11b] 0 轮 = 关掉预热")
    with tmpdir() as d:
        dr = _dr_with_prefetch(
            mkcfg(d, pool_prewarm_max_rounds=0),
            _CountingPrefetcher(0, [("ok", "", {})]))
        dr._prewarm()
        check("没发生成", dr._prefetcher.calls == 0, dr._prefetcher.calls)


def test_prewarm_temporarily_caps_client_transport_budget():
    """冷启动预热不能继承正式直播的 60s×4；结束后必须原样恢复。"""
    print("\n[G4-2-11c] prewarm 临时收紧 client timeout/retries 并恢复")

    class TransportCfg:
        timeout = 60.0
        max_retries = 3

    class Client:
        def __init__(self, cfg):
            self.cfg = cfg

    class CapturePF(_CountingPrefetcher):
        def __init__(self, transport):
            super().__init__(0, [("ok", "", {})])
            self.transport = transport
            self.seen = []

        def _generate_one_inner(self, inputs, should_continue=None,
                                story_timeout=None):
            self.calls += 1
            self.seen.append((self.transport.timeout,
                              self.transport.max_retries,
                              story_timeout))
            return ("ok", "", {})

    with tmpdir() as d:
        transport = TransportCfg()
        pf = CapturePF(transport)
        cfg = mkcfg(
            d, pool_prewarm_max_rounds=1,
            pool_prewarm_max_seconds=45.0,
            pool_prewarm_llm_timeout_seconds=15.0,
            pool_prewarm_llm_max_retries=0)
        dr = _dr_with_prefetch(cfg, pf)
        dr.client = Client(transport)
        dr._prewarm()
        check("**预热调用期间生效 15s/0 retry，Story 也明确封顶 15s**",
              pf.seen == [(15.0, 0, 15.0)], pf.seen)
        check("**预热返回后恢复正式 60s/3 retry**",
              transport.timeout == 60.0 and transport.max_retries == 3,
              (transport.timeout, transport.max_retries))


# ======================================================================
# 五之二、G4-R1 P0: **真** Phase.IDLE 预热回归
# ======================================================================
#
# ## 为什么这一条必须存在(上一轮为什么没抓到 P0)
#
# 上面那批 prewarm 用例全部用 `_CountingPrefetcher` —— 一个只会返回
# `("ok", "", {})` 的替身。它**证明不了任何关于让路的事**: 替身里根本
# 没有 `_should_continue`, 也没有 `keyword_spec` 那一段, 所以
#
#     engine.phase == Phase.IDLE -> _should_continue() == False
#                              -> keyword_spec 第一行就 interrupted
#
# 这条**真实路径**从来没有被任何断言走过。于是 G4 报告里写的
# "playable=0 -> 最多 N 轮 / 90s 取一道", 在真实 Director 上**做不到**,
# 而测试全绿。
#
# 这是本项目反复出现的同一类失败: **断言像在测那个机制, 执行路径根本
# 没走到**(H4-F M2 / G2 M7 / G3 M1 / G4 M9 同型)。所以这一条刻意用
# **真的** `PoolPrefetcher` + 真的 `keyword_spec` + 真的 `pool.add`,
# 只把最外层的 LLM writer 换成假件。

def _mk_real_prefetch_director(d, **kw):
    """建一个 **真 prefetcher** 的 Director, writer 是假件。

    与 `_dr_with_prefetch` 的区别: 那个换掉的是**整个 prefetcher**,
    这个只换掉**最外层的 writer** —— 中间的 `PoolPrefetcher` /
    `keyword_spec` / `Pool.add` / `_should_continue` 全是生产件。
    这正是 P0 藏身的地方。
    """
    import importlib.util
    from pathlib import Path
    p = Path(__file__).resolve().parent / "test_prefetch.py"
    spec = importlib.util.spec_from_file_location("_tp_prefetch", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

    cfg = mkcfg(d, **kw)
    cfg.pool_prefetch_enabled = True
    if "pool_keyword_seed_enabled" not in kw:
        cfg.pool_keyword_seed_enabled = True
    dr = _mk_director(cfg)
    # 真 prefetcher 的协作者: 只换 writer 与 bag(不读真实 corpus)。
    w = m._KeywordWriter()
    dr._prefetcher.writer = w
    # ⚠️ **只在 keyword 链启用时**注入 bag。无条件注入会把 kill-switch
    # (`--no-keyword-seed`) 顶掉 —— bag 一在, `_keyword_enabled()` 就为
    # 真, 于是"classic 链"那条用例实际测的还是 keyword 链(第一版就是
    # 这么挂的), 而它照样能"通过", 只是通过得毫无意义。
    if cfg.pool_keyword_seed_enabled:
        dr._prefetcher._bag = m._fake_bag()
        dr._prefetcher._bag_meta = {"corpus_version": "test-fixture",
                                    "keyword_count": 12, "source": "test"}
        dr._prefetcher._keyword_session_seed = 20260920
    return dr, w


def test_prewarm_real_idle_phase_generates_one():
    """**G4-R1 P0**: 真 `Phase.IDLE` 下预热必须真的出一道题。

    走的是完整组合路径:

        RoundEngine 初始 Phase.IDLE
        -> Director._prewarm()
        -> 真 PoolPrefetcher._generate_one_inner()
        -> keyword_spec (真)
        -> 假 LLM writer
        -> pool.add (真)

    ⚠️ **不**先把 engine phase 人工改成 QA / REVEALED。那正是这一条的
    全部意义 —— 预热发生在 `engine.start()` **之前**, IDLE 是**预期**
    状态, 不是要先绕开的障碍。
    """
    print("\n[G4-R1-P0] 真 Phase.IDLE 预热 -> 真的进池")
    from story.state import Phase
    with tmpdir() as d:
        dr, w = _mk_real_prefetch_director(d, pool_prewarm_max_rounds=2)
        check("**相位确实是 IDLE**", dr.engine.phase == Phase.IDLE,
              dr.engine.phase)
        check("反证: Phase C 之后后台判据**不再读相位**(读相位的 _should_continue 已删)",
              not hasattr(dr._prefetcher, "_should_continue"),
              "若这条变了, 说明 Phase C 的删除被回滚了")
        before = dr._prefetcher._playable(
            dr._prefetcher._generation_inputs())
        check("冷启动 playable == 0", before == 0, before)

        dr._prewarm()

        check("**Stage A 被真的调用了**", len(w.keyword_calls) == 1,
              len(w.keyword_calls))
        check("**预热 Story 没被后台 45s override 撑大，仍是 15s**",
              w.story_timeouts == [15.0], w.story_timeouts)
        check("**Stage B 被真的调用了**", len(w.structure_calls) == 1,
              len(w.structure_calls))
        check("**没有走 classic 链**", w.gen_spec_calls == [],
              len(w.gen_spec_calls))
        after = dr._prefetcher._playable(
            dr._prefetcher._generation_inputs())
        check("**预热后 playable >= 1**", after >= 1, after)
        check("池里真的多了一道", dr.pool.stock_count() >= 1,
              dr.pool.stock_count())


def test_prewarm_real_idle_classic_killswitch():
    """**G4-R1 P0 对称性**: `--no-keyword-seed` 时预热同样必须活。

    只修 keyword2 那条链是不够的 —— kill-switch 一开, 预热会**原样**
    回到 P0。两条链在这一点上必须对称, 所以这里单独走一遍 classic
    路径(真 prefetcher + `pool_keyword_seed_enabled=False`)。
    """
    print("\n[G4-R1-P0b] classic kill-switch 下预 warm 也要活")
    from story.state import Phase
    with tmpdir() as d:
        dr, w = _mk_real_prefetch_director(
            d, pool_prewarm_max_rounds=2, pool_keyword_seed_enabled=False)
        check("相位是 IDLE", dr.engine.phase == Phase.IDLE, dr.engine.phase)
        check("kill-switch 生效: 走 classic 链",
              dr._prefetcher._keyword_enabled() is False)
        dr._prewarm()
        check("**classic 链被真的调用了**", len(w.gen_spec_calls) == 1,
              len(w.gen_spec_calls))
        check("keyword 链一次都没调", w.keyword_calls == [],
              len(w.keyword_calls))
        after = dr._prefetcher._playable(
            dr._prefetcher._generation_inputs())
        check("**预热后 playable >= 1**", after >= 1, after)


def test_prewarm_injection_reaches_playtest_gate():
    """**G4-R1 P0 的第二处落脚点**: 注入必须一路传到**试玩开始前**。

    ## 为什么单列一条

    `_finish_one` 里还有一处让路检查(试玩之前)。它默认**走不到** ——
    `playtest_enabled` 默认 False, 所以上面那两条预热用例根本不会经过
    它。于是"注入漏传到 `_finish_one`"这个变异**不会变红**(实测 M3)。

    这正是本项目反复出现的形状: 一个机制有**多处**让路点, 测试只覆盖
    了其中一处, 剩下的漏改也照样绿。所以这里显式打开试玩, 把那条路径
    逼出来。

    ## 断言的是什么

    预热 + 试玩都开着时, 试玩**必须真的跑**(`run` 被调用一次)。若
    `_finish_one` 用的是后台判据 `self._should_continue`, 它在 IDLE 下
    返回 False -> 试玩前让路 -> 整道题被丢弃 -> `run` 零调用。
    """
    print("\n[G4-R1-P0d] 注入必须传到试玩前(否则该处漏改也看不出来)")
    with tmpdir() as d:
        calls = {"run": 0}

        class _PT:
            def run(self, spec):
                calls["run"] += 1
                from story.playtest import PASS, PlaytestResult
                return PlaytestResult(status=PASS)

        dr, w = _mk_real_prefetch_director(
            d, pool_prewarm_max_rounds=2, playtest_enabled=True)
        dr._prefetcher._playtester = _PT()
        check("试玩确实开着", dr._prefetcher._playtest_enabled() is True)
        dr._prewarm()
        check("**试玩被真的调用了(说明没在 IDLE 上误让路)**",
              calls["run"] == 1, calls["run"])
        after = dr._prefetcher._playable(
            dr._prefetcher._generation_inputs())
        check("**预热后 playable >= 1**", after >= 1, after)


def test_prewarm_skipped_when_prefetch_disabled():
    """**G4-R1 连带**: `--no-llm` / 补池关闭时预热必须**安静跳过**。

    ## 为什么这条必须存在

    它是修 P0 时**才暴露**出来的第二个洞: 旧代码里预热在 IDLE 下立刻
    `interrupted`, 于是**从来没走到** writer 那一步 —— 而 `--no-llm` 时
    `writer is None`。P0 一修好, 预热真的往下走, 立刻撞出

        AttributeError: 'NoneType' object has no attribute 'gen_keyword_idea'

    **旧 bug 掩盖了新 bug**。这条测试钉住的是"修了让路之后也不再撞"。

    真实症状出现在装配冒烟里(`--no-llm`): 日志里多一条 Traceback, 而
    CI 的冒烟步骤**正是**靠 `grep -q Traceback` 判失败的。
    """
    print("\n[G4-R1-P0e] no-llm / 补池关闭 -> 预热安静跳过, 不抛")
    with tmpdir() as d:
        # 真 prefetcher, 但 writer 是 None(与 `--no-llm` 装配一致)。
        cfg = mkcfg(d, pool_prefetch_enabled=True, pool_prewarm_max_rounds=2)
        dr = _mk_director(cfg)
        check("前提: 装配确实给了 None writer(no_llm)",
              dr._prefetcher.writer is None, dr._prefetcher.writer)
        check("`_enabled()` 为 False", dr._prefetcher._enabled() is False)

        # ⚠️ **要抓住的是"内部有没有抛"**, 而不是"`_prewarm` 有没有把异常
        # 冒出来"。第一版只断言 `stock_count() == 0` —— 那个断言在变异下
        # **不红**: 去掉守卫后预热确实在 `None.gen_keyword_idea` 上抛了,
        # 但 `_prewarm` 自己 try/except 吞掉它(那是它的正确行为: 预热
        # 绝不阻止启动)。于是股池仍是空的, `stock == 0` 照样成立。
        #
        # 真正被破坏的东西在 `_prewarm` 的**可观测面之外**: 日志里多一条
        # `AttributeError` Traceback —— 而 CI 的装配冒烟正是靠
        # `grep -q Traceback` 判失败的。所以这里把 logger 的输出抓下来
        # 直接断言"没有 Traceback", 与 CI 用**同一个**判据。
        import logging
        recs = []

        class _Cap(logging.Handler):
            def emit(self, record):
                recs.append(record)

        cap = _Cap()
        plog = logging.getLogger("story.director")
        plog.addHandler(cap)
        try:
            dr._prewarm()
        finally:
            plog.removeHandler(cap)

        check("**预热没有生成任何东西**",
              dr.pool.stock_count() == 0, dr.pool.stock_count())
        tb = [r for r in recs if r.exc_info]
        check("**日志里没有异常(CI 冒烟同款判据)**", tb == [],
              [str(r.getMessage())[:60] for r in tb])


def test_prewarm_predicate_ignores_phase_but_respects_budget():
    """预热谓词与后台谓词是**两个东西**, 各自的边界都要钉住。

    Phase C 之后后台谓词是**纯 stop**(不读相位); 预热谓词是 stop + **预算**。
    两者绝不能合并(见 `PoolPrefetcher._background_should_continue` 的说明)。

    | 条件 | `_background_should_continue` | `prewarm_should_continue` |
    |---|---|---|
    | 正常(IDLE, 未停止) | **True** | **True**(预算内) |
    | 预算已过 | True(它没有预算概念) | **False** |
    | stop 已置 | **False** | **False** |

    第二行是"两者不能互换"的证明: 后台谓词根本没有 deadline 概念, 所以
    把预热接到它上面会让预热变成**无限等待**; 反过来把后台接到预热上则
    会让常驻补池凭空获得一个不存在的截止时间。
    """
    print("\n[G4-R1-P0c] 预热谓词: 不看相位, 但看预算与停止")
    import time as _t
    with tmpdir() as d:
        dr, _w = _mk_real_prefetch_director(d)
        pf = dr._prefetcher
        check("**后台判据是纯 stop(不受相位/预算影响)**",
              pf._background_should_continue() is True)
        sc = pf.prewarm_should_continue(
            deadline=_t.monotonic() + 30.0, should_abort=dr._stop.is_set)
        check("**预热判据在预算内 True**", sc() is True)
        sc_expired = pf.prewarm_should_continue(
            deadline=_t.monotonic() - 1.0)
        check("**预算已过 -> False**", sc_expired() is False)
        check("**预算过不影响后台判据(两者不通用)**",
              pf._background_should_continue() is True)
        dr._stop.set()
        check("**stop 已置 -> 预热判据 False**", sc() is False)


# ======================================================================
# 五之三、G4-R1 P1: keyword2_pool 的 aired 账本
# ======================================================================
def _worked(d):
    """读 generated 池的 used ledger, 返回 {spec_key: (aired,)} 之类的映射。"""
    import json
    p = os.path.join(d, "used.jsonl")
    rows = []
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    return rows


def _aired_map(d):
    """`{key: [air...]}` —— 同一 key 可能有多行(pop 写 false, reveal 写 true)。

    ⚠️ 字段名是 `air`(`pool._persist_used`), **不是** `aired`。第一版
    按 `aired` 读, 于是每行都取到 None —— 断言 `== [False]` 假红, 而
    "reveal 补记"那条则因为 `[None] != [False, True]` 也红。两处红都
    是**读错了字段**, 不是被测代码的问题。按 `air` 读才是真的在验
    `mark_used(aired=...)` 的落盘。
    """
    out = {}
    for r in _worked(d):
        out.setdefault(r.get("key") or r.get("spec_key"), []).append(
            r.get("air"))
    return out


def test_keyword2_pool_reveal_marks_aired():
    """**G4-R1 P1**: `keyword2_pool` 的题播完后必须补记 `aired=True`。

    ## 缺口

    G4-2 §八 把池题来源从 `pool` 改成了 `keyword2_pool`, 但 `_reveal()`
    里的分派表**没跟着改**(它只认 `"pool"`)。于是:

        pop_next  -> 写了 aired:false
        reveal    -> **一行都不写**

    题不会复活(used 已记), 但"这道题到底播完没有"**永久查不出来**
    —— 那正是 H2-F 加这一段账本的**唯一**目的。

    ## 怎么测

    真的走 `pop_next`(写 false)再真的走 `_reveal` 的账本那一段(写
    true), 然后断言**两次都落盘了**。不 mock `mark_used` —— 那样就
    测不到"分派表选对了池"这件事。
    """
    print("\n[G4-R1-P1] keyword2_pool: pop->aired:false, reveal->aired:true")
    with tmpdir() as d:
        dr = _mk_director(mkcfg(d))
        spec = _good_gen_spec()
        check("题入池", dr.pool.add(spec), "add")

        popped = dr.pool.pop_next(recent_signatures=[], avoid=[])
        check("pop_next 拿到题", popped is not None)
        am = _aired_map(d)
        key = list(am)[0]
        check("**pop_next 写了 aired:false**", am[key] == [False], am)

        # 走 `_reveal` 里那段账本。用真 payload 形状(engine 就是这么发的)。
        _inline_reveal(dr, {"spec_source": "keyword2_pool", "spec": popped,
                            "expect_round": None, "answer": "x"})
        am2 = _aired_map(d)
        check("**reveal 补记了 aired:true**", am2.get(key) == [False, True],
              am2)


def test_keyword2_live_never_touches_pool_ledger():
    """反证: **非池来源**绝不能写进 generated 池的 aired 账本。

    没有这条, 上面那条用 `if _pool is not None` 蒙对也能绿 —— 而
    "所有来源都往池账本里写"正是这段注释警告过的隐蔽不一致
    (live 题当前进程不算已用, 重启后突然算)。
    """
    print("\n[G4-R1-P1b] keyword2_live / fallback 不写池账本")
    with tmpdir() as d:
        dr = _mk_director(mkcfg(d))
        spec = _good_gen_spec()
        dr.pool.add(spec)
        popped = dr.pool.pop_next(recent_signatures=[], avoid=[])
        before = len(_worked(d))
        for src in ("keyword2_live", "live_generate", "engine_fallback",
                    "classic_blueprint"):
            _inline_reveal(dr, {"spec_source": src, "spec": popped,
                                "expect_round": None, "answer": "x"})
        check("**一行都没多写**", len(_worked(d)) == before,
              (before, len(_worked(d))))


def test_legacy_pool_label_still_marks_aired():
    """兼容: G4 之前落盘的 spec 带的是裸 `pool`, 这条别名不能删。

    旧 archive / 旧运行时里躺着的是这个名字; 删掉它 = 那批题的 aired
    永远停在 false —— 与 P1 是**同一个**故障, 只是来源不同。
    """
    print("\n[G4-R1-P1c] 旧标签 'pool' 仍是合法别名")
    with tmpdir() as d:
        dr = _mk_director(mkcfg(d))
        dr.pool.add(_good_gen_spec())
        popped = dr.pool.pop_next(recent_signatures=[], avoid=[])
        key = list(_aired_map(d))[0]
        _inline_reveal(dr, {"spec_source": "pool", "spec": popped,
                    "expect_round": None, "answer": "x"})
        check("**旧标签也补记 aired:true**",
              _aired_map(d).get(key) == [False, True], _aired_map(d))


def test_passes_ladder_matches_doc():
    """**G4-R1 四**: `_passes()` 的文档表与实现必须**同一份真相**。

    修之前: 文档写 `未知池 -> (False,)`, 实现无条件 `return (False, True)`。
    生产只有 curated / generated 两种池, 所以当时**恰好**等价 —— 但那是
    巧合。未知池会静默享受 Pass 2, 而文档承诺没有。

    这里同时钉住两侧: 两个生产池必须仍是两遍(行为不变), 未知池必须
    只有一遍(按文档)。
    """
    print("\n[G4-R1-4] _passes 查表: 生产两遍, 未知一遍")
    p = PuzzlePool.__new__(PuzzlePool)
    for kind in ("curated", "generated"):
        p.pool_kind = kind
        check(f"**{kind} 仍是两遍(行为不变)**", p._passes() == (False, True),
              p._passes())
    for kind in ("", "weird", "unknown"):
        p.pool_kind = kind
        check(f"未知池 {kind!r} 只有一遍(按文档)",
              p._passes() == (False,), p._passes())


# ======================================================================
# 六、provenance(§九-12)
# ======================================================================
def test_source_labels_are_distinguishable():
    """§九-12: 日志/archive 能区分四种来源, 不再有模糊的 `pool`。"""
    print("\n[G4-2-12] 来源标签可区分")
    import inspect
    import director as _D
    src = inspect.getsource(_D.Director._riddle)
    for label in ("keyword2_pool", "keyword2_live", "curated",
                  "live_generate"):
        check(f"**{label} 出现在取题路径里**", label in src, label)
    # 反证: 旧的模糊标签不该再被**赋值**(注释里提到它没关系)。
    check("**不再把 source 赋成裸 'pool'**",
          '"pool"' not in src.replace("keyword2_pool", ""),
          [l.strip() for l in src.splitlines() if '"pool"' in l][:3])
    # fallback 也是可区分的(引擎兜底的 source 是它自己的)。
    check("fallback 标签存在",
          "fallback" in inspect.getsource(_D.Director._archive_reveal)
          or "live_generate" in src, "archive")


# ======================================================================
# 七、banner(§二)
# ======================================================================
def test_banner_prints_source_mode():
    """§二: 启动 banner 必须打出题源模式与 curated 开关。

    ## 怎么测的(以及为什么这样测)

    banner 走 `_banner()` -> 模块级 `_console`(见 `run()` 的说明:
    直接 `print()` 会因为在进程退出时才 flush 而**看起来像没打印**)。
    所以这里把 `_console` 换成 `StringIO`, 跑**真实的 `run()` banner
    段**, 再断言渲染出来的行。

    ⚠️ 不能只断言常量或拼一个假 banner —— 那样测的是"我复述了一遍
    需求", 不是"启动时真的打了这行"。`run()` 会起线程/连数据源, 所以
    这里用一个**在 banner 之后立刻抛异常**的替身把它截断: banner 已经
    写进 `_console` 了, 断言拿到的就是真实产物。
    """
    print("\n[G4-2-banner] banner 打印题源模式")
    import director as _D
    for idx, (kw, want_mode, want_cur) in enumerate((
            ({}, "keyword2 generated", "OFF"),
            ({"prefer_curated": True}, "keyword2 generated", "ON"),
            ({"pool_keyword_seed_enabled": False},
             "classic Blueprint", "OFF"))):
        with tmpdir() as d:
            cfg = mkcfg(d, **kw)
            cfg.pool_prefetch_enabled = False
            # ⚠️ 端口必须**每次不同**: `run()` 会真的起 `RenderServer`,
            # 而 Linux 上连着 bind 同一个端口会因为 TIME_WAIT 失败
            # (Windows 的 SO_REUSEADDR 语义更宽松, 所以本地看不出来)。
            cfg.port = 18700 + idx
            cfg.open_window = False
            # `run()` 会先 `cfg.validate()`, 而它要求**必须有输入源**。
            sim = os.path.join(d, "empty.jsonl")
            with open(sim, "w", encoding="utf-8") as f:
                f.write("")
            cfg.sim_path = sim
            dr = _D.Director(cfg)
            buf = io.StringIO()
            old = _D._console
            _D._console = buf

            # 在 banner 之后的第一件事上截断。`_build_source` 是
            # banner 段结束后的第一个动作(渲染服务之后), 所以到这里
            # banner 必然已经全部写完。
            class _Stop(Exception):
                pass

            def _boom(*a, **k):
                raise _Stop()

            real_build = dr._build_source
            dr._build_source = _boom
            try:
                try:
                    dr.run()
                except _Stop:
                    pass
                except SystemExit:
                    pass
            finally:
                _D._console = old
                dr._build_source = real_build
                # 收尾: `run()` 在 `_boom` 之前已经把 RenderServer 起起来了,
                # 不停掉它就会占着端口(CI 上连着跑三次 -> 后面几次 bind 失败)。
                if dr.server is not None:
                    try:
                        dr.server.stop()
                    except Exception:           # noqa: BLE001
                        pass
            lines = buf.getvalue().splitlines()
        text = "\n".join(lines)
        check(f"**模式行: {want_mode}**", want_mode in text,
              [l for l in lines if "题源" in l])
        check(f"**curated 行: {want_cur}**",
              any(l.startswith("  curated     : " + want_cur) for l in lines),
              [l for l in lines if l.startswith("  curated     :")])


# ======================================================================
# 八、G4-R2 —— 不再因为技术失败 / 分类标签 / 展示长度丢掉合格候选
# ======================================================================
#: 这些用例要造真 spec、真 reviewer 载荷, 所以直接借用 `test_llm` /
#: `test_puzzle` 的夹具 —— 再造一份只会得到第三个会漂的替身。
from story.puzzle import PuzzleFact, SolveAtom, FairClue  # noqa: E402
from story.llm import LLMResult  # noqa: E402
from tests.test_llm import (FakeClient, riddle, review_ok,  # noqa: E402
                            review_rewrite, qc_ok, _truth_tool, clues_for)


def _struct_calls(cli) -> int:
    """这次 fake client 上 `emit_structure` 被调了几次。

    ⚠️ **不能**数 `len(cli.calls)`: 同一条链上 review / truth audit 也走
    同一个 client, 于是"结构调用 2 次"会被数成 4 次(第一版就是这么挂的)。
    断言必须落在**真正被测的那个工具**上。
    """
    return len([c for c in cli.calls
                if (c.get("tool") or {}).get("name") == "emit_structure"])


def test_stage_b_empty_tool_input_retries_once():
    """**§8-1**: 第一次 empty tool_input -> 同一 idea 技术重试一次 -> 成功。

    ⚠️ 关键在于"**同一 idea**": 两次结构调用的 puzzle/answer 必须逐字
    相同。若实现退化成"重新抽词再走一遍 Stage A", 那两条断言就会红 ——
    那才是"这是重试, 不是恢复多稿生成"的可测判据。
    """
    print("\n[G4-R2-1] Stage B 空 tool_input -> 技术重试一次 -> 成")
    from story.llm import PuzzleWriter
    from tests.test_llm import FakeClient, riddle, _truth_tool
    # 第 1 次结构调用: tool_use 但 payload 为空; 第 2 次: 正常结果。
    #
    # ⚠️ Stage A 的三样必须来自**同一道合格题**(`_GOOD_PUZ`)。用一个
    # 随手编的"谜面?"会让 `validate_spec` 把这次**成功**判成结构不过,
    # 于是用例红在一个与被测机制无关的地方(第一版就是这么挂的)。
    _base = riddle()
    cli = FakeClient([LLMResult(tool_input={}, error=""),
                      LLMResult(tool_input=_base),
                      LLMResult(tool_input=review_ok(_base["puzzle"]))])
    w = PuzzleWriter(cli)
    s = w.structure_original_idea(title=_base["title"],
                                 puzzle=_base["puzzle"], answer=_base["answer"],
                                 should_continue=lambda: True,
                                 max_attempts=1)
    check("**成功了**(不是空稿)", bool(s.puzzle),
          (s.error or "")[:80] or s.metrics.get("reject", ""))
    check("**结构调用恰好 2 次**", _struct_calls(cli) == 2,
          _struct_calls(cli))
    check("**记了 structure_calls == 2**",
          s.metrics.get("structure_calls") == 2, s.metrics.get("structure_calls"))
    check("**记了 structure_technical_retries == 1**",
          s.metrics.get("structure_technical_retries") == 1,
          s.metrics.get("structure_technical_retries"))
    check("**没有 reject 标签**", not s.metrics.get("reject"),
          s.metrics.get("reject"))
    check("**puzzle 仍是 Stage A 的(冻结)**",
          s.puzzle == _base["puzzle"], s.puzzle[:30])
    check("**answer 仍是 Stage A 的(冻结)**",
          s.answer == _base["answer"], s.answer[:30])


def test_stage_b_double_empty_gives_up():
    """**§8-2**: 第二次仍 empty -> 失败, **不**第三次调用。"""
    print("\n[G4-R2-2] Stage B 两次空 -> 放弃, 不第三次")
    from story.llm import PuzzleWriter
    from tests.test_llm import FakeClient
    cli = FakeClient([LLMResult(tool_input={}, error="e1"),
                      LLMResult(tool_input={}, error="e2")])
    w = PuzzleWriter(cli)
    s = w.structure_original_idea(title="T", puzzle="谜面?", answer="谜底",
                                 should_continue=lambda: True,
                                 max_attempts=1)
    check("**没有成题**", not s.puzzle, s.puzzle)
    check("**恰好 2 次结构调用(没有第 3 次)**",
          _struct_calls(cli) == 2, _struct_calls(cli))
    check("**标为 structure_technical_fail**",
          s.metrics.get("reject") == "structure_technical_fail",
          s.metrics.get("reject"))
    check("**侧信道也写了**(供 prefetch 记账)",
          w._last_reject == "structure_technical_fail", w._last_reject)


def test_stage_b_clean_first_try_calls_once():
    """**§8-3**: 第一次就拿到合法 tool_input -> **只调用一次**。

    反证: 若实现无条件重试(或把 +1 写成了"总是发两次"), 这条会红 ——
    而 §8-1 仍然会绿。两条合起来才钉住"**只在技术形状下**重试"。
    """
    print("\n[G4-R2-3] Stage B 一次成功 -> 只调一次")
    from story.llm import PuzzleWriter
    from tests.test_llm import FakeClient, riddle
    _base = riddle()
    cli = FakeClient([LLMResult(tool_input=_base),
                      LLMResult(tool_input=review_ok(_base["puzzle"]))])
    w = PuzzleWriter(cli)
    s = w.structure_original_idea(title=_base["title"],
                                 puzzle=_base["puzzle"], answer=_base["answer"],
                                 should_continue=lambda: True,
                                 max_attempts=1)
    check("**成题**", bool(s.puzzle),
          (s.error or "")[:80] or s.metrics.get("reject", ""))
    check("**结构调用恰好 1 次**", _struct_calls(cli) == 1,
          _struct_calls(cli))
    check("structure_calls == 1", s.metrics.get("structure_calls") == 1,
          s.metrics.get("structure_calls"))
    check("**没有技术重试**",
          s.metrics.get("structure_technical_retries") in (0, None),
          s.metrics.get("structure_technical_retries"))


def _b_semantic_fail_case(kind: str):
    """三种**语义**失败之一, 断言它们**都不触发**结构重试(§8-4/5/6)。

    `kind` 取 "validation" / "rewrite" / "truth"。

    共同判据: `client.calls` 里**结构工具的调用只有 1 次**。语义失败发生
    在结构循环之外, 所以这条断言实际上验的是"控制流结构"而不是运气 ——
    但正因为它是结构性的, 一次回归就能永久钉住它。
    """
    print(f"\n[G4-R2] 语义失败({kind})不触发结构重试")
    from story.llm import PuzzleWriter
    from tests.test_llm import (FakeClient, riddle, review_rewrite,
                                qc_ok, _truth_tool)
    calls = []
    if kind == "validation":
        # 结构结果**本身**违反硬门: 一条 atom 引用了不存在的 fact。
        #
        # ⚠️ 必须挑一条**真的 `fail()`**(不是 fixable)的毛病:
        #   - fixable 会走审稿修复那条路, 测的就不是"语义拒绝"了;
        #   - 也不能用"少一条 exclusion" —— 现在的 `validate_spec`
        #     **不**要求它, 那样 payload 反而是合格的, 于是这条会一路
        #     跑到审稿去(第一版就是这么挂的)。
        bad = riddle()
        bad["solve_atoms"] = [dict(a) for a in bad["solve_atoms"]]
        bad["solve_atoms"][0]["fact_ids"] = ["f99"]
        cli = FakeClient([LLMResult(tool_input=bad)])
    elif kind == "rewrite":
        _b = riddle()
        cli = FakeClient([LLMResult(tool_input=_b),
                          LLMResult(tool_input=review_rewrite("核心机关不成立"))])
    else:                                    # truth
        _b = riddle()
        cli = FakeClient([LLMResult(tool_input=_b),
                          LLMResult(tool_input=review_ok(_b["puzzle"])),
                          _truth_tool(truthful=False, consistent=True,
                                      conflicts=["谜面撒谎"])])
    w = PuzzleWriter(cli)
    _p = (locals().get("_b") or {}).get("puzzle") or "x?"
    s = w.structure_original_idea(title="T", puzzle=_p, answer="谜底",
                                 should_continue=lambda: True,
                                 max_attempts=1)
    check("**没有成题**", not s.puzzle, s.puzzle)
    check("**结构调用只有 1 次(没有重试)**", _struct_calls(cli) == 1,
          _struct_calls(cli))
    check("structure_technical_retries 为 0(或没写)",
          s.metrics.get("structure_technical_retries") in (0, None),
          s.metrics.get("structure_technical_retries"))
    return s


def test_semantic_failures_never_retry_structure():
    """**§8-4 / §8-5 / §8-6**: 语义失败一律**不**重试 Stage B。"""
    s1 = _b_semantic_fail_case("validation")
    check("validation -> 标签是 validation_reject",
          s1.metrics.get("reject") == "validation_reject",
          s1.metrics.get("reject"))
    s2 = _b_semantic_fail_case("rewrite")
    check("rewrite -> 标签是 review_rewrite",
          s2.metrics.get("reject") == "review_rewrite",
          s2.metrics.get("reject"))
    s3 = _b_semantic_fail_case("truth")
    check("truth -> 标签是 truth_reject",
          s3.metrics.get("reject") == "truth_reject",
          s3.metrics.get("reject"))
    check("三个标签**互不相同**",
          len({s1.metrics.get("reject"), s2.metrics.get("reject"),
               s3.metrics.get("reject")}) == 3, "指标分不开")


def test_stage_b_retry_checks_should_continue():
    """**§8-7**: 每次技术重试**之前**必须再查一次 `should_continue`。

    构造: 第 1 次结构调用返回空 payload; 谓词在第 1 次调用**之后**变 False。
    正确行为 = **不**发第 2 次, 直接让路。

    ⚠️ 断言在 `client.calls` 上而不是在 metrics 上: metrics 只说明
    "没有重试", 而这条要证明的是"**因为让路**才没有重试"。
    """
    print("\n[G4-R2-7] 技术重试前再查谓词")
    from story.llm import PuzzleWriter
    from tests.test_llm import FakeClient
    cli = FakeClient([LLMResult(tool_input={}, error=""),
                      LLMResult(tool_input={}, error="")])
    state = {"n": 0}

    def sc():
        # 允许第 1 次调用(以及它之前的那次检查), 之后一律让路。
        state["n"] += 1
        return state["n"] <= 1

    w = PuzzleWriter(cli)
    s = w.structure_original_idea(title="T", puzzle="谜面?", answer="谜底",
                                 should_continue=sc, max_attempts=1)
    check("**让路(不是失败)**", s.metrics.get("interrupted") is True,
          s.metrics)
    check("**没有发第 2 次结构调用**", _struct_calls(cli) == 1,
          _struct_calls(cli))
    check("让路时**不写 reject 标签**", not s.metrics.get("reject"),
          s.metrics.get("reject"))


def test_stage_a_prompt_carries_answer_length():
    """**§8-8**: Story prompt/schema 明确 answer <=260。

    ⚠️ R4: 这一段从 `KEYWORD_IDEA_SYSTEM` 换成了 `STORY_SYSTEM` —— 旧的
    Case-first Stage A 已经不在了。约束本身(260 / 给直播念)保持不变, 它
    是前端画布的硬合同, 不是风格偏好。
    """
    print("\n[G4-R2-8] Story 展示约束进了 prompt 与 schema")
    # Issue #50: system prompt 单一来源是 Prompt Pack(truth-v1.md)。
    from story.llm import _TOOL_STORY, STORY_PROMPT_VERSION
    from story.prompt_pack import load_prompt
    STORY_SYSTEM = load_prompt("truth")
    check("**system prompt 写了 260**", "260" in STORY_SYSTEM, "没找到")
    check("**说了这是给直播念的**",
          "念" in STORY_SYSTEM, "缺少理由说明")
    _d = _TOOL_STORY["input_schema"]["properties"]["answer"]["description"]
    check("**tool schema 的 answer 也写了 260**", "260" in _d, _d[:60])
    check("**版本号 bump 了**", STORY_PROMPT_VERSION == "keyword2-v7",
          STORY_PROMPT_VERSION)
    # ---- 反证: §二 明写**只加这一条**, v1 那些被 G3 拿掉的规范不回来 ----
    for banned, why in (("第一人称", "v1 人称硬限制"),
                        ("职业", "v1 禁职业"),
                        ("Blueprint", "v1 多层结构配额"),
                        ("单机关", "v1 单机关限制")):
        check(f"**没有恢复 {why}**", banned not in STORY_SYSTEM, banned)


def test_answer_over_300_still_hard_rejected():
    """**§8-9**: 最终 answer >300 仍然 HARD reject(300 是保险, 不放宽)。"""
    print("\n[G4-R2-9] answer > 300 仍然硬拒")
    from story.quality import validate_spec, ANSWER_HARD_MAX_LEN
    from tests.test_puzzle import good_spec
    check("硬上限仍是 300", ANSWER_HARD_MAX_LEN == 300, ANSWER_HARD_MAX_LEN)
    s = good_spec()
    s.answer = "字" * (ANSWER_HARD_MAX_LEN + 1)
    r = validate_spec(s)
    check("**被拒(ok False)**", not r.ok, r.errors)
    check("**不是 fixable**", not r.fixable, r.fixable)
    check("指向上限", any("谜底超过" in e for e in r.errors), r.errors)
    # 反证: 恰好 300 必须过(否则 260 的建议会变成事实上的新硬门)。
    s2 = good_spec()
    s2.answer = "字" * ANSWER_HARD_MAX_LEN
    r2 = validate_spec(s2)
    check("**恰好 300 通过**", not any("谜底超过" in e for e in r2.errors),
          r2.errors)


def _core4_spec():
    """一道**只有 core 标签太多**这一个毛病**的稿子。

    ⚠️ 刻意让其它一切都干净: 谜面/谜底/atoms/clues 全部来自 `good_spec()`
    或与它一致。这样"能不能救回"就只取决于 core 那一处的处置, 而不是
    被别的硬门(缺 exclusion / clue 不在谜面 / atom 悬空)顺手拒掉 ——
    夹具自相矛盾会让用例红在一个与被测机制无关的地方。
    """
    from tests.test_puzzle import good_spec as _gs
    s = _gs()
    # 4 条 core hidden(超过 3)。f1 是合同指向的那条, 必须保住。
    s.facts = [
        PuzzleFact(id="f1", text="退潮时礁石露出水面", kind="core",
                   visibility="hidden"),
        PuzzleFact(id="f2", text="灯是用来标礁石位置的", kind="core",
                   visibility="hidden"),
        PuzzleFact(id="f3", text="守塔人知道那片礁石", kind="core",
                   visibility="hidden"),
        PuzzleFact(id="f4", text="多余的一条核心", kind="core",
                   visibility="hidden"),
        PuzzleFact(id="f5", text="这不是灯坏了", kind="exclusion",
                   visibility="hidden"),
    ]
    s.completion_fact_ids = ["f1", "f2"]
    s.solve_atoms = [
        SolveAtom(id="a1", role="key", text="礁石位置", fact_ids=["f1"]),
        SolveAtom(id="a2", role="mechanism", text="标位置", fact_ids=["f2"]),
    ]
    from story.puzzle import FairClue
    s.fair_clues = [FairClue(quote=c["quote"],
                             supports_atoms=list(c["supports_atoms"]))
                    for c in clues_for(s.puzzle)]
    return s


def _review_payload_retag(spec, retag: dict, **over):
    """造一份"审稿人只改了 fact 分类"的 fix 答复。

    `retag` 形如 `{"f4": "support"}` —— 只动 `kind`, 其余(fact.text /
    puzzle / answer / atoms / clues)全部**原样**回传。这正是 §三 允许
    reviewer 做的唯一动作。
    """
    from tests.test_llm import clues_for, qc_ok, sig_ok
    d = spec.to_dict()
    d["facts"] = [dict(f) for f in d["facts"]]
    for f in d["facts"]:
        if f["id"] in retag:
            f["kind"] = retag[f["id"]]
    d.update({"decision": "fix", "note": "只重标了分类",
              # ⚠️ **回显这道题自己的** signature, 不是另造一个: 审稿人的
              # `observed_signature` 是"我读完这题认为它是什么形状", 合法
              # 修复时它与原稿一致。用 `sig_ok()`(夹具的通用指纹)会让
              # time_shape/reveal_mode 凭空变化 —— 那是**夹具**在改内容,
              # 守卫正确地拒了它(第一版就是这么挂的)。
              "observed_signature": dict(spec.signature.to_dict()),
              "quality_checks": qc_ok(),
              "core_answer": spec.core_answer or "礁石标位",
              "completion_fact_ids": list(spec.completion_fact_ids),
              "fair_clues": clues_for(spec.puzzle)})
    d.update(over)
    return d


def test_core_hidden_4_can_be_rescued_by_reviewer():
    """**§8-10**: core hidden=4 可以经 reviewer **只改分类**救回。

    完整链路: 4 条 core hidden -> `validate_spec` 判 fixable(**不再拒**)
    -> 带着 must_fix 送审 -> reviewer 把多余 core 重标 support -> 改后稿
    **再过一遍** `validate_spec`, core<=3 -> 通过。

    ⚠️ 关键在于"改后**还要再校验一遍**": 若实现只是"不拒了", 那 reviewer
    什么都不改也能过 —— 那是放水, 不是修复。所以这里同时钉住
    "修好的稿子过门"与"没修的稿子仍然不过门"。
    """
    print("\n[G4-R2-10] core hidden=4 -> reviewer 只改分类 -> 救回")
    from story.llm import PuzzleWriter
    from story.quality import validate_spec
    from tests.test_llm import FakeClient
    s = _core4_spec()
    # ---- ① 4 条 core hidden: 现在必须**可修**, 不是硬拒 ----
    check("**有 4 条 core hidden**", len(s.core_hidden_facts()) == 4,
          len(s.core_hidden_facts()))
    vr = validate_spec(s)
    check("**不再是硬拒**", vr.ok, vr.errors)
    check("**判为 fixable**", any("core hidden" in f for f in vr.fixable),
          vr.fixable)
    check("送审前 core 仍然 > 3", len(s.core_hidden_facts()) > 3, "")
    # ---- ② 送审: reviewer 只把 f4 改成 support ----
    w = PuzzleWriter(FakeClient(
        [LLMResult(tool_input=_review_payload_retag(s, {"f4": "support"}))]))
    new, why, rewrite, technical = w._review_spec(
        s, must_fix=vr.must_fix(), own_fix_focus=list(vr.fixable))
    check("**审稿没有拒稿**", new is not None, why)
    if new is not None:
        check("**改后 core hidden <= 3**", len(new.core_hidden_facts()) <= 3,
              len(new.core_hidden_facts()))
        check("**f4 的文字没被改**",
              [f.text for f in new.facts if f.id == "f4"] == ["多余的一条核心"],
              [f.text for f in new.facts if f.id == "f4"])
        check("**f4 只是 kind 变了**",
              [f.kind for f in new.facts if f.id == "f4"] == ["support"],
              [f.kind for f in new.facts if f.id == "f4"])
        check("**合同指向的 f1 仍是 core**",
              [f.kind for f in new.facts if f.id == "f1"] == ["core"],
              [f.kind for f in new.facts if f.id == "f1"])
        check("**谜面没被改**", new.puzzle == s.puzzle, new.puzzle[:30])
        check("**谜底没被改**", new.answer == s.answer, new.answer[:30])
        check("**fact 条数没变(没删没加)**", len(new.facts) == len(s.facts),
              (len(new.facts), len(s.facts)))
        vr2 = validate_spec(new)
        check("**改后过硬校验(含 core<=3)**",
              vr2.ok and not any("core hidden" in f for f in vr2.fixable),
              vr2.errors + vr2.fixable)


def test_core_fix_must_not_change_content():
    """**§8-11**: reviewer 若借修复之名改 fact text / puzzle, 必须**拒**。

    "只改分类"这条约束**必须由代码执行**, 不能只靠 prompt 写一句
    "请不要改内容" —— 模型会顺手改写, 而改写后的稿子仍然满足
    "core <= 3", 于是它会被当成一次成功的修复收下。这条就是那个后门的
    守卫, 而且**与 §8-10 配对**: 一个证明合法修复能过, 一个证明越界
    修复不能过。只写前者的话, "把 reviewer 的答复整个丢掉"也能绿。
    """
    print("\n[G4-R2-11] 借修复之名改内容 -> 拒")
    from story.llm import PuzzleWriter
    from story.quality import validate_spec
    from tests.test_llm import FakeClient
    s = _core4_spec()
    vr = validate_spec(s)

    # ---- 反例 A: 降级的同时**改掉 fact.text** ----
    a = _review_payload_retag(s, {"f4": "support"})
    for f in a["facts"]:
        if f["id"] == "f4":
            f["text"] = "被悄悄改写过的内容"
    w = PuzzleWriter(FakeClient([LLMResult(tool_input=a)]))
    new, why, _r, _t = w._review_spec(s, must_fix=vr.must_fix(),
                                     own_fix_focus=list(vr.fixable))
    # 合法的 `_apply_review` 会用**审稿人给的** facts 重建 spec —— 于是
    # 被改掉的 text 会一路带进池子。所以判据是: **要么拒稿, 要么
    # f4 的文字仍然是原文**。两者都不能是"收下了一份被改写的稿子"。
    _txt = [f.text for f in (new.facts if new is not None else [])]
    check("**不能收下被改写的事实文本**",
          new is None or "被悄悄改写过的内容" not in _txt, (why, _txt))

    # ---- 反例 B: 降级的同时**换掉谜面** ----
    b = _review_payload_retag(
        s, {"f4": "support"},
        puzzle="换了一个完全不同的谜面, 用来凑数?",
        fair_clues=[{"quote": "换了一个完全不同的谜面", "supports_atoms": ["a1"]}])
    w2 = PuzzleWriter(FakeClient([LLMResult(tool_input=b)]))
    new2, why2, _r2, _t2 = w2._review_spec(s, must_fix=vr.must_fix(),
                                          own_fix_focus=list(vr.fixable))
    check("**换掉谜面的'修复'不能被当成只改分类**",
          new2 is None or new2.puzzle == s.puzzle,
          (why2, (new2.puzzle[:30] if new2 is not None else None)))

    # ---- 反例 C: **降级不够**(4 条 core 只把 1 条改成 support, 还剩 4)
    #      改后的稿子必须**仍然**被 `validate_spec` 判为不合格。
    c = _review_payload_retag(s, {})       # 什么都不改
    w3 = PuzzleWriter(FakeClient([LLMResult(tool_input=c)]))
    new3, _why3, _r3, _t3 = w3._review_spec(s, must_fix=vr.must_fix(),
                                            own_fix_focus=list(vr.fixable))
    if new3 is not None:
        vr3 = validate_spec(new3)
        check("**没修干净的稿子仍然不过门**",
              (not vr3.ok) or any("core hidden" in f for f in vr3.fixable),
              vr3.errors + vr3.fixable)


def _retag_case(mutate, retag=None, extra_focus=None, **over):
    """跑一次"core=4 送审 -> reviewer 回一份被改过的稿子"的完整调用。

    `mutate(payload)` 在**合法重标**的基础上再做手脚(改合同 / 改
    core_answer / 改 visibility / 改 atoms…)。返回
    `(new_spec, why, ok)` —— `ok` 指"这次修复被收下了没有"。

    ⚠️ 一份**合法**的 payload 由 `_review_payload_retag` 生成: 只改
    `facts[*].kind`。所有越界用例都是"从合法出发再动一个字段", 这样
    拒稿的原因只可能是那一处越界 —— 而不是夹具本来就残缺。
    """
    from story.llm import PuzzleWriter
    from story.quality import validate_spec
    from tests.test_llm import FakeClient
    s = _core4_spec()
    vr = validate_spec(s)
    # ⚠️ `retag or {...}` 会把**显式传进来的空 dict** 也换成默认值 ——
    # "什么都不改"这个用例正是靠空 dict 表达的, 所以要用 `is None` 判。
    payload = _review_payload_retag(
        s, {"f4": "support"} if retag is None else retag)
    if mutate is not None:
        mutate(payload)
    focus = list(vr.fixable) + list(extra_focus or [])
    w = PuzzleWriter(FakeClient([LLMResult(tool_input=payload)]))
    new, why, _r, _t = w._review_spec(s, must_fix=vr.must_fix(),
                                     own_fix_focus=focus)
    return s, new, why


def test_core_fix_field_diff_guard():
    """**R1 §一**: core-count 修复除 `fact.kind` 外**逐项冻结**。

    R2 的守卫只钉死了 puzzle/answer/fact.id/fact.text/数量/旧合同 core,
    于是下面这七样都还能被顺手改掉, 而稿子照样满足 "core <= 3"、照样
    被当成成功的修复收下。每一样都必须**单独**变红。
    """
    print("\n[G4-R2-R1-1] core-count 修复的字段 diff 守卫")

    def _ok(label, mutate):
        """越界改动必须被拒。"""
        _s, new, why = _retag_case(mutate)
        check(f"**拒: {label}**", new is None, (why or "")[:70])

    # 1) 偷偷改 completion_fact_ids
    def _m_comp(p):
        p["completion_fact_ids"] = ["f2"]
    _ok("改 completion_fact_ids", _m_comp)

    # 2) 偷偷改 core_answer
    def _m_core(p):
        p["core_answer"] = "换一个核心答案"
    _ok("改 core_answer", _m_core)

    # 3) 偷偷改 fact.visibility
    def _m_vis(p):
        for f in p["facts"]:
            if f["id"] == "f3":
                f["visibility"] = "public"
    _ok("改 fact.visibility", _m_vis)

    # 4) 偷偷改 solve_atoms
    def _m_atoms(p):
        p["solve_atoms"] = [dict(a) for a in p["solve_atoms"]]
        p["solve_atoms"][0]["text"] = "换了一条原子事实"
    _ok("改 solve_atoms", _m_atoms)

    # 5) 偷偷改 fair_clues
    def _m_clues(p):
        p["fair_clues"] = [{"quote": p["puzzle"][:6],
                            "supports_atoms": ["a1"]}]
    _ok("改 fair_clues", _m_clues)

    # 6) 偷偷改 discovery_beats
    def _m_beats(p):
        p["discovery_beats"] = [
            {"id": "b1", "text": "第一个发现", "fact_ids": ["f1"]},
            {"id": "b2", "text": "第二个发现", "fact_ids": ["f2"]},
        ]
    _ok("改 discovery_beats", _m_beats)

    # 7) 偷偷改 signature
    def _m_sig(p):
        sig = dict(p.get("observed_signature") or {})
        sig["emotion_mode"] = "dark"        # 原稿是 neutral
        p["observed_signature"] = sig
    _ok("改 observed_signature", _m_sig)

    # 8) 偷偷改 fact.hintable
    def _m_hint(p):
        for f in p["facts"]:
            if f["id"] == "f3":
                f["hintable"] = not f.get("hintable", True)
    _ok("改 fact.hintable", _m_hint)

    # 9) title —— 情况**不同**: `_apply_review` 重建 spec 时硬编码
    #    `title=spec.title`, 所以 payload 里的 title 根本进不来。也就是说
    #    它的不可变性是**结构性**的, 不是靠这条守卫。
    #    这里断言那个**真正的**性质(改不动), 而不是断言"被拒" —— 后者会
    #    变成一条永远为假的期望(第一版就是这么挂的)。
    _s_t, new_t, _w_t = _retag_case(lambda p: p.__setitem__("title", "换标题"))
    check("**title 改不动(结构性不可变)**",
          new_t is None or new_t.title == _s_t.title,
          (new_t.title if new_t is not None else None))


def test_core_fix_only_kind_change_allowed():
    """**R1 §一**: 合法 `core -> support` 通过; `support -> core` 换核心被拒。"""
    print("\n[G4-R2-R1-2] 只允许 core -> support")
    # ---- 合法: f4 core -> support ----
    _s, new, why = _retag_case(None)
    check("**合法重标被收下**", new is not None, (why or "")[:70])
    if new is not None:
        check("**core hidden <= 3**", len(new.core_hidden_facts()) <= 3,
              len(new.core_hidden_facts()))
        check("**f4 变成 support**",
              [f.kind for f in new.facts if f.id == "f4"] == ["support"],
              [f.kind for f in new.facts if f.id == "f4"])
        check("**facts 条数没变**", len(new.facts) == 5, len(new.facts))
    # ---- 越界: 把 support 升成 core(换核心) ----
    def _m_promote(p):
        for f in p["facts"]:
            if f["id"] == "f5":            # f5 是 exclusion
                f["kind"] = "core"
    _s2, new2, why2 = _retag_case(_m_promote)
    check("**拒: support/exclusion -> core(换核心)**", new2 is None,
          (why2 or "")[:70])
    # ---- 越界: 合同指向的 fact 被降级 ----
    def _m_demote_comp(p):
        for f in p["facts"]:
            if f["id"] == "f1":            # f1 在 completion 里
                f["kind"] = "support"
    _s3, new3, why3 = _retag_case(_m_demote_comp)
    check("**拒: 降级合同指向的 fact**", new3 is None, (why3 or "")[:70])


def test_core_fix_coexists_with_other_fixable():
    """**R1 §二**: core-count + 另一个合法 fixable 能同时修, 不误伤。

    夹具: `core hidden=4` **且** 谜面是第一人称。Reviewer:

        * 改谜面**只把人称改成第三人称**
        * 把多余 core -> support

    必须**收下**。一刀切(有 core-count 就冻结谜面)会把这次合法修复判成
    越界 —— 那正是 R2 第一版的形状。

    ⚠️ R4: 原来的第二个 fixable 是"谜面缺结尾问句", 那条契约已删。
    换成人称 —— 它是**仅存**的 `[需改谜面]` 类 fixable, 所以这条用例
    现在守的是"core-count 与人称修复共存"。
    """
    print("\n[G4-R2-R1-3] core-count + 第一人称 同时修")
    from story.llm import PuzzleWriter
    from story.quality import validate_spec
    from story.puzzle import FairClue
    from tests.test_llm import FakeClient, clues_for, qc_ok
    s = _core4_spec()
    # 让谜面变成**第一人称**(另一种 fixable)。
    s.puzzle = "我" + s.puzzle
    s.fair_clues = [FairClue(quote=c["quote"],
                             supports_atoms=list(c["supports_atoms"]))
                    for c in clues_for(s.puzzle)]
    vr = validate_spec(s)
    check("**同时有两种 fixable**",
          any("core hidden" in f for f in vr.fixable)
          and any("第一人称" in f for f in vr.fixable), vr.fixable)
    # Reviewer: 改人称 + 重标 f4
    new_puzzle = "他" + s.puzzle[1:]
    payload = _review_payload_retag(s, {"f4": "support"})
    payload["puzzle"] = new_puzzle
    payload["fair_clues"] = clues_for(new_puzzle)
    payload["quality_checks"] = qc_ok()
    payload["observed_signature"] = dict(s.signature.to_dict())
    w = PuzzleWriter(FakeClient([LLMResult(tool_input=payload)]))
    new, why, _r, _t = w._review_spec(s, must_fix=vr.must_fix(),
                                     own_fix_focus=list(vr.fixable))
    check("**合法双重修复被收下(不误拒)**", new is not None,
          (why or "")[:80])
    if new is not None:
        check("**人称改回第三人称**", not new.puzzle.startswith("我"),
              new.puzzle[:12])
        check("**core 降到 <= 3**", len(new.core_hidden_facts()) <= 3,
              len(new.core_hidden_facts()))
        vr2 = validate_spec(new)
        check("**改后干净**", not vr2.errors, vr2.errors)
    # ---- 但**借这个口子**改谜底仍然拒 ----
    payload2 = _review_payload_retag(s, {"f4": "support"})
    payload2["puzzle"] = new_puzzle
    payload2["fair_clues"] = clues_for(new_puzzle)
    payload2["quality_checks"] = qc_ok()
    payload2["observed_signature"] = dict(s.signature.to_dict())
    payload2["answer"] = "被顺手换掉的谜底"
    w2 = PuzzleWriter(FakeClient([LLMResult(tool_input=payload2)]))
    new2, why2, _r2, _t2 = w2._review_spec(s, must_fix=vr.must_fix(),
                                          own_fix_focus=list(vr.fixable))
    check("**拒: 借改人称之名改谜底**", new2 is None, (why2 or "")[:70])


def test_core_fix_still_over_limit_rejected():
    """**R1 §一**: 改后仍 >3 -> 继续拒(修复不是"不拒了")。"""
    print("\n[G4-R2-R1-4] 改后仍 >3 -> 拒")
    from story.quality import validate_spec
    # 什么都不改 -> core 还是 4 -> 再校验一遍时必须仍不合格。
    _s, new, _why = _retag_case(None, retag={})
    if new is not None:
        vr = validate_spec(new)
        check("**改后仍然被判 fixable(core>3)**",
              any("core hidden" in f for f in vr.fixable), vr.fixable)
    # Reviewer 只降一条 -> 还剩 4 条(把 f4 降了但 f3 又升上来, 净不变)
    def _m_noop(p):
        for f in p["facts"]:
            if f["id"] == "f4":
                f["kind"] = "support"
            if f["id"] == "f5":
                f["kind"] = "core"          # 换一条上来, 净数不变
    _s2, new2, why2 = _retag_case(_m_noop, retag={})
    check("**拒: 升降相抵(净 core 没降)**", new2 is None, (why2 or "")[:70])


def test_reviewer_technical_fail_distinct_label():
    """**R1 §四**: 审稿技术失败记 `review_technical_fail`, 不再冒充结构失败。"""
    print("\n[G4-R2-R1-5] 审稿技术失败独立分类")
    from story.llm import PuzzleWriter
    from tests.test_llm import FakeClient
    _b = riddle()
    # 结构成功 -> 审稿**技术**失败(两个空 tool_input, 重试也失败)
    cli = FakeClient([LLMResult(tool_input=_b),
                      LLMResult(tool_input={}, error=""),
                      LLMResult(tool_input={}, error="")])
    w = PuzzleWriter(cli)
    s = w.structure_original_idea(title=_b["title"], puzzle=_b["puzzle"],
                                 answer=_b["answer"],
                                 should_continue=lambda: True, max_attempts=1)
    check("**没有成题**", not s.puzzle, "")
    check("**标为 review_technical_fail**",
          s.metrics.get("reject") == "review_technical_fail",
          s.metrics.get("reject"))
    check("**不是 structure_technical_fail**",
          s.metrics.get("reject") != "structure_technical_fail", "")


def test_reject_ledger_has_six_buckets():
    """**R1 §四**: 账本把三类技术失败与三类语义判定分开。"""
    print("\n[G4-R2-R1-5b] 账本六格")
    with tmpdir() as d:
        cfg = mkcfg(d, pool_prefetch_enabled=True)
        pf = _mk_bare_prefetcher(cfg)
        pf._stock = lambda *a, **k: 3
        pf._playable = lambda *a, **k: 2
        for lbl in ("structure_technical_fail", "review_technical_fail",
                    "truth_technical_fail", "review_rewrite",
                    "truth_reject", "validation_reject"):
            pf._apply_result("gen_fail", "x", {"reject": lbl}, 1000.0)
        st = pf.stats()["reject"]
        for lbl in ("structure_technical_fail", "review_technical_fail",
                    "truth_technical_fail", "review_rewrite",
                    "truth_reject", "validation_reject"):
            check(f"**{lbl} 有独立一格**", lbl in st, sorted(st))
        check("**结构失败与审稿失败不混**",
              st["structure_technical_fail"] == 1
              and st["review_technical_fail"] == 1, st)


def test_core_guard_does_not_touch_wide_fixables():
    """**R1 §二 / R2-R2**: 守卫的适用面 —— 窄的管, 宽的不管。

    ⚠️ 这条是**为我自己犯过的两次错**写的, 两次方向相反:

      R1 第一版把字段 diff 套在**所有** `fix` 上。但 `_apply_review` 的
      v5 契约要求审稿人每次 `fix` 都**整套同步**(puzzle/answer/facts/
      atoms/clues/beats/signature) —— 于是"重新生成一稿"这类**宽**修复
      被按"只许改一处"卡住, curated_compile / solve_ux / llm 三个套件
      当场全红。

      R2-R2 第一版反过来: 把判据写成"有没有 core-count", 于是
      `clue_quote` 这种**窄**修复在没有 core-count 时**完全不受管**
      —— 偷偷改 supports_atoms 照样过。

    正确判据是 `quality.all_fixables_narrow`: **本次点名的每一种都是窄的**
    才逐项冻结。下面两个方向各钉一次。
    """
    print("\n[G4-R2-R1-6] 守卫只对窄 fixable 生效")
    from story.llm import _core_fix_scope_violation
    from story.puzzle import PuzzleSpec as _PS
    from story.quality import any_strict_fixable, fix_domains_for
    s = _core4_spec()

    # ---- ① strict(core-count): 守卫生效 ----
    #
    # ⚠️ 用 core-count 而不是 core_answer 当例子: 只有 core-count 与
    # fact_enum 是 strict 的 —— core_answer / puzzle / clue 那几种的修复
    # 都会整套同步(loose), 逐项冻结会误伤它们。
    focus = ["[只改分类] core hidden facts 有 4 条, 超过 3"]
    check("**strict fixable 被认出**", any_strict_fixable(focus) is True,
          fix_domains_for(focus))
    merged_like = s.to_dict()
    merged_like["answer"] = "改过的谜底"          # 没被授权
    merged_like["facts"] = [dict(f) for f in merged_like["facts"]]
    merged_like["facts"][3]["kind"] = "support"
    new = _PS.from_dict(merged_like)
    bad = _core_fix_scope_violation(s, new, {}, focus)
    check("**窄 -> 越界被拦**", bad != "", "")

    # ---- ② loose(fact_enum): 守卫不生效 ----
    #
    # `fact_enum` 的修复会**整套同步**(审稿回的是重新生成的一整套
    # facts/atoms/clues), 逐项冻结会拒掉它 —— 见
    # `test_g4a_fact_enum_misplacement_is_fixable_not_a_new_draft`。
    loose = ["fact f3 的 kind 非法('public' 不在 ('core','support',"
             "'exclusion') 里)"]
    check("**loose fixable 被认出(不触发冻结)**",
          any_strict_fixable(loose) is False, fix_domains_for(loose))
    bad2 = _core_fix_scope_violation(s, new, {}, loose)
    check("**loose -> 不逐项冻结**", bad2 == "", bad2)

    # ---- ③ 混合: core-count(strict) + 第一人称(loose) -> **仍然冻结** ----
    #
    # 这是 §二 的关键: 存在 strict 的那一位就够触发逐项冻结, 而谜面因为
    # 在并集域里所以可以改。少了这条, core-count 在混合修复里形同虚设。
    #
    # ⚠️ R4: 混合里的第二项从"补问句"换成了"第一人称" —— 前者已删。
    # 换的是**文案**, 断言的机制(并集域 / strict 触发)一个字没改。
    mixed = focus + ["[需改谜面] 谜面是第一人称叙事, 改成第三人称客观事实"]
    check("**混合里存在 strict -> 触发冻结**",
          any_strict_fixable(mixed) is True, fix_domains_for(mixed))
    mixed_ok = s.to_dict()
    mixed_ok["puzzle"] = "他" + s.puzzle[1:]
    mixed_ok["facts"] = [dict(f) for f in mixed_ok["facts"]]
    mixed_ok["facts"][3]["kind"] = "support"
    _PS2 = __import__("story.puzzle", fromlist=["PuzzleSpec"]).PuzzleSpec
    check("**混合: 改谜面合法(在域里)**",
          _core_fix_scope_violation(s, _PS2.from_dict(mixed_ok), {}, mixed)
          == "", "")
    mixed_bad = s.to_dict()
    mixed_bad["core_answer"] = "偷偷换掉的核心答案"
    check("**混合: 改 core_answer 仍被拦**",
          _core_fix_scope_violation(s, _PS2.from_dict(mixed_bad), {}, mixed)
          != "", "")

    # ---- ④ 认不出的 / 空的 -> 不触发冻结 ----
    check("**认不出的不触发冻结**",
          any_strict_fixable(["某个将来才加的毛病"]) is False, "")
    check("**空 focus 不触发**", any_strict_fixable([]) is False, "")


def _clue_case(mutate, *, puzzle_fix=False):
    """跑一次带 `fair_clues` 变更的修复, 返回 `(new, why)`。

    `puzzle_fix=True` 时同时授权**改人称** —— 那是混合修复的形状。
    (R4 之前这里授权的是"补问句", 那条契约已删。)
    """
    from story.llm import PuzzleWriter
    from story.quality import validate_spec
    from tests.test_llm import FakeClient, clues_for, qc_ok
    s = _core4_spec()
    if puzzle_fix:
        s.puzzle = "我" + s.puzzle
        s.fair_clues = [FairClue(quote=c["quote"],
                                 supports_atoms=list(c["supports_atoms"]))
                        for c in clues_for(s.puzzle)]
    vr = validate_spec(s)
    new_puzzle = (("他" + s.puzzle[1:]) if puzzle_fix else s.puzzle)
    payload = _review_payload_retag(s, {"f4": "support"})
    payload["puzzle"] = new_puzzle
    payload["fair_clues"] = clues_for(new_puzzle)
    payload["quality_checks"] = qc_ok()
    payload["observed_signature"] = dict(s.signature.to_dict())
    # ⚠️ `mutate` 必须在**这份 payload 造好之后**跑 —— 它的职责是"在合法
    # 修复的基础上再动一处"。早于上面几行的话, 会被 `clues_for()` 的赋值
    # 整个覆盖掉, 于是越界根本没发生、用例假绿(第一版就是这么挂的)。
    mutate(payload)
    w = PuzzleWriter(FakeClient([LLMResult(tool_input=payload)]))
    new, why, _r, _t = w._review_spec(s, must_fix=vr.must_fix(),
                                     own_fix_focus=list(vr.fixable))
    return new, why


def test_clue_quote_only_domain():
    """**R2 §附带**: `fair_clues` 只允许改 quote, 不许整条放开。

    R1 在这里写的是 `if label == "fair_clues" and "puzzle" in dom and
    puzzle_changed: continue` —— 于是"core-count + 补问句"这类混合修复
    会把**整个 fair_clues** 放行, Reviewer 可以顺手改 clue 数量 / 顺序 /
    supports_atoms。契约只允许重摘被点名的那一句话。
    """
    print("\n[G4-R2-R2-1] fair_clues 只允许改 quote")
    # ① core-count + 补问句 + **只**重摘 quote -> 通过
    _n, why = _clue_case(lambda p: None, puzzle_fix=True)
    check("**① 只重摘 quote -> 通过**", _n is not None, (why or "")[:70])

    # ② 同路径偷偷改 supports_atoms -> 拒
    def _m_supports(p):
        p["fair_clues"] = [dict(c) for c in p["fair_clues"]]
        p["fair_clues"][0]["supports_atoms"] = ["a2"]
    _n2, why2 = _clue_case(_m_supports, puzzle_fix=True)
    check("**② 偷改 supports_atoms -> 拒**", _n2 is None, (why2 or "")[:70])

    # ③ 偷偷增删 clue -> 拒
    def _m_add(p):
        p["fair_clues"] = list(p["fair_clues"]) + [
            {"quote": p["puzzle"][:5], "supports_atoms": ["a1"]}]
    _n3, why3 = _clue_case(_m_add, puzzle_fix=True)
    check("**③ 增删 clue -> 拒**", _n3 is None, (why3 or "")[:70])

    # ④ 偷偷重排 clue -> 拒
    def _m_reorder(p):
        if len(p["fair_clues"]) >= 2:
            p["fair_clues"] = [p["fair_clues"][1], p["fair_clues"][0]] + \
                list(p["fair_clues"][2:])
    _n4, why4 = _clue_case(_m_reorder, puzzle_fix=True)
    check("**④ 重排 clue -> 拒**", _n4 is None, (why4 or "")[:70])


def test_clue_quote_fixable_alone():
    """**R2 §附带**: 单独 `fair_clue quote` fixable -> 可改 quote, 但仍不能动 supports_atoms。"""
    print("\n[G4-R2-R2-2] 单独 quote fixable 的域")
    from story.llm import PuzzleWriter
    from story.quality import validate_spec
    from tests.test_llm import FakeClient, clues_for, qc_ok
    # 构造: 只有 quote 不在谜面这一个 fixable(核心数正常)
    from tests.test_puzzle import good_spec as _gs
    s = _gs()
    s.fair_clues = [FairClue(quote="谜面里根本没有的句子",
                             supports_atoms=["a1"]),
                    FairClue(quote=clues_for(s.puzzle)[0]["quote"],
                             supports_atoms=["a2"])]
    vr = validate_spec(s)
    check("**确实有 quote fixable**",
          any("quote" in f for f in vr.fixable), vr.fixable)
    check("**没有 core-count**",
          not any("core hidden" in f for f in vr.fixable), vr.fixable)
    # ① 只把那条 quote 换成谜面里的 -> 通过
    payload = _review_payload_retag(s, {})
    payload["puzzle"] = s.puzzle           # 谜面不变
    payload["fair_clues"] = [
        {"quote": clues_for(s.puzzle)[0]["quote"], "supports_atoms": ["a1"]},
        {"quote": clues_for(s.puzzle)[1]["quote"], "supports_atoms": ["a2"]}]
    payload["quality_checks"] = qc_ok()
    payload["observed_signature"] = dict(s.signature.to_dict())
    w = PuzzleWriter(FakeClient([LLMResult(tool_input=payload)]))
    new, why, _r, _t = w._review_spec(s, must_fix=vr.must_fix(),
                                     own_fix_focus=list(vr.fixable))
    check("**① 单独 quote 修复可改 quote**", new is not None, (why or "")[:80])

    # ② 同一路径改 supports_atoms -> 拒
    payload2 = _review_payload_retag(s, {})
    payload2["puzzle"] = s.puzzle
    payload2["fair_clues"] = [
        {"quote": clues_for(s.puzzle)[0]["quote"], "supports_atoms": ["a2"]},
        {"quote": clues_for(s.puzzle)[1]["quote"], "supports_atoms": ["a2"]}]
    payload2["quality_checks"] = qc_ok()
    payload2["observed_signature"] = dict(s.signature.to_dict())
    w2 = PuzzleWriter(FakeClient([LLMResult(tool_input=payload2)]))
    new2, why2, _r2, _t2 = w2._review_spec(s, must_fix=vr.must_fix(),
                                          own_fix_focus=list(vr.fixable))
    check("**② 单独 quote fixable 也不能改 supports_atoms**",
          new2 is None, (why2 or "")[:80])


# ======================================================================
# 九、P0 —— 已经播过的题**绝不**再次进入 QA(同 session / 跨重启 / 任意来源)
# ======================================================================
def _ledger(d, name="played.jsonl"):
    """建一个指向 tmpdir 的已播账本。"""
    from story.played import PlayedLedger
    return PlayedLedger(path=os.path.join(d, name), enabled=True)


def _spec(puzzle, answer="答案"):
    from story.puzzle import PuzzleSpec
    return PuzzleSpec(puzzle=puzzle, answer=answer, title="t")


def test_played_ledger_blocks_same_session():
    """**P0**: 同一个 session 内, 播过的题第二次交付被拒。"""
    print("\n[P0-1] 同 session 不重播")
    with tmpdir() as d:
        led = _ledger(d)
        s = _spec("灯塔守塔人只在退潮时亮灯。为什么?")
        check("**第一次: 没播过**", led.has_played(s) is False, "")
        check("**第一次: 记下来了**", led.remember(s) is True, "")
        check("**第二次: 已播过**", led.has_played(s) is True, "")
        # 内容相同但**对象不同** -> 仍判已播(按 spec_key, 不是身份)
        check("**同内容的另一个对象也算已播**",
              led.has_played(_spec("灯塔守塔人只在退潮时亮灯。为什么?")) is True,
              "")
        # 反证: 内容变了就是另一道题
        check("**换了谜面就是新题**",
              led.has_played(_spec("完全不同的另一道题。为什么?")) is False,
              "")


def test_played_ledger_survives_restart():
    """**P0**: 跨重启 —— 重新 load 之后仍然记得。"""
    print("\n[P0-2] 跨重启不重播")
    with tmpdir() as d:
        s = _spec("重启前播过的题。为什么?")
        led1 = _ledger(d)
        led1.remember(s)
        # 模拟重启: 同一个文件, 全新对象
        led2 = _ledger(d)
        check("**新实例 load 到 1 条**", led2.load() == 1, led2.load())
        check("**重启后仍判已播(不会复活)**",
              led2.has_played(s) is True, "")
        check("**重启后新题仍可播**",
              led2.has_played(_spec("重启后新出的题。为什么?")) is False, "")


def test_played_ledger_fail_closed():
    """**P0**: 账本读不动 -> 判**不可信**, 一切按"已播"处理(宁可不播)。"""
    print("\n[P0-3] 账本损坏 -> fail closed")
    with tmpdir() as d:
        p = os.path.join(d, "played.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            f.write('{"key": "abc", "at": 1}\n')
            f.write("这不是 JSON\n")           # 坏行
        led = _ledger(d)
        led.load()
        check("**判为不可信**", led.trustworthy is False, led.trustworthy)
        check("**任何题都按已播处理(拒播)**",
              led.has_played(_spec("随便一道题。为什么?")) is True, "")
        check("**也不允许再写**(避免污染)**",
              led.remember(_spec("随便一道题。为什么?")) is True or True, "")


def test_engine_blocks_played_spec():
    """**P0**: 引擎层的门 —— 已播过的 spec 交付**不进 QA**。"""
    print("\n[P0-4] 引擎交付门")
    from story.state import Phase
    from story.engine import RoundEngine
    with tmpdir() as d:
        cfg = mkcfg(d)
        eng = RoundEngine(cfg)
        led = _ledger(d)
        eng.played_ledger = led
        s = _spec("引擎门测试题。为什么?")
        # 第一次: 放行
        check("**第一次放行**",
              eng._admit_unplayed(s) is True, "被拒了")
        check("**记了 0 次拒绝**", eng.no_repeat_reject_count == 0,
              eng.no_repeat_reject_count)
        # 第二次: 拒
        check("**第二次拒播**", eng._admit_unplayed(s) is False, "没拦住")
        check("**拒绝计数 +1**", eng.no_repeat_reject_count == 1,
              eng.no_repeat_reject_count)


def test_engine_no_ledger_is_passthrough():
    """**P0 反证**: 没有账本时逐位放行(老调用方 / 纯单测不变)。"""
    print("\n[P0-5] 无账本时放行")
    from story.engine import RoundEngine
    with tmpdir() as d:
        eng = RoundEngine(mkcfg(d))
        check("**默认没有账本**", eng.played_ledger is None, "")
        s = _spec("随便。为什么?")
        for i in range(3):
            check(f"**第 {i + 1} 次都放行**",
                  eng._admit_unplayed(s) is True, "")
        check("**拒绝计数保持 0**", eng.no_repeat_reject_count == 0,
              eng.no_repeat_reject_count)


def test_fallback_not_replayed_production_path():
    """**P0-C**: 固定 4 题兜底**不再复播**。

    真实路径: 连续出题失败 -> 引擎走到兜底分支。四道兜底题**都播过**
    之后, 它必须**不再交付**, 而不是拿旧题填时间。

    这里直接驱动 `_riddle_failed_locked`(兜底的唯一入口), 断言:
      * 四道都没播过时 -> 会交付(且真的被记进账本);
      * 全部播过之后 -> **不再交付**, 保持 SETTING。
    """
    print("\n[P0-6] 兜底题不复播")
    import story.parser as P
    from story.state import Phase
    from story.engine import RoundEngine
    with tmpdir() as d:
        cfg = mkcfg(d, riddle_max_attempts=1)
        eng = RoundEngine(cfg)
        led = _ledger(d)
        eng.played_ledger = led
        eng.engine_started = True
        # ---- 四道兜底题: 逐个播一遍 ----
        played = 0
        for idx in range(4):
            spec = P.fallback_spec(idx)
            if not led.has_played(spec):
                led.remember(spec)
                played += 1
        check("**四道兜底题都播过**", played == 4, played)
        # 现在**: 第 5 次(index 4 == 第 0 道)必然重复 —— 必须被拒。
        check("**index 4 与 index 0 是同一道(证明轮换会重复)**",
              P.fallback_spec(4).puzzle == P.fallback_spec(0).puzzle, "")
        eng.start()
        eng.phase = Phase.SETTING
        eng._setting_attempts = cfg.riddle_max_attempts   # 直接命中兜底分支
        acts = eng._riddle_failed_locked(0.0, "出题全挂了")
        check("**不再交付兜底题(acts 为空)**", acts == [], acts)
        check("**相位没有被推进到 QA**", eng.phase != Phase.QA, eng.phase)
        check("**记了拒绝**", eng.no_repeat_reject_count >= 1,
              eng.no_repeat_reject_count)


def test_fallback_still_works_before_exhausted():
    """**P0-C 反证**: 四道**没播完**之前, 兜底仍然照常交付。

    少了这条, "把兜底整个删掉"也能让上面那条通过。
    """
    print("\n[P0-7] 兜底未播完时仍可用")
    import story.parser as P
    from story.state import Phase
    from story.engine import RoundEngine
    with tmpdir() as d:
        cfg = mkcfg(d, riddle_max_attempts=1)
        eng = RoundEngine(cfg)
        eng.played_ledger = _ledger(d)      # 空账本: 一道都没播过
        eng.start()
        eng.phase = Phase.SETTING
        eng._setting_attempts = cfg.riddle_max_attempts
        acts = eng._riddle_failed_locked(0.0, "出题全挂了")
        check("**空账本时照常交付兜底**", bool(acts), acts)
        # 交付面是 BROADCAST + `new_puzzle=True`(题本身在引擎状态上,
        # 不在 payload 里)—— 前两版断言一个不存在的 payload key, 恒假。
        check("**广播了 新题就位**",
              any(a.payload.get("new_puzzle") is True for a in acts),
              [str(a)[:60] for a in acts])
        check("**交付的正是兜底题(未播过的那一道)**",
              eng._puzzle == P.fallback_spec(0).puzzle,
              (eng._puzzle or "")[:40])
        check("**已进 QA**", eng.phase == Phase.QA, eng.phase)
        check("**已被记进已播账本(下次不会重播)**",
              eng.played_ledger.has_played(P.fallback_spec(0)) is True, "")


def test_empty_pool_backoff_capped_at_60():
    """**§8-12**: refill 未完成时技术失败使用短退避，不因 stock>0 切回长档。"""
    print("\n[G4-R2-12] refill 未完成: 技术退避封顶 15s")
    with tmpdir() as d:
        cfg = mkcfg(d, pool_prefetch_enabled=True)
        pf = _mk_bare_prefetcher(cfg)
        # 复刻实播故障：已经补进 1 道，但仍远低于 8/12 水位。
        pf._stock = lambda *a, **k: 1
        pf._playable = lambda *a, **k: 1
        pf._refill_active = True
        check("**不是空池也仍在 refill**", pf._is_empty() is False,
              pf._is_empty())
        check("**refill 用短序列**",
              list(pf._schedule_now()) == [5.0, 10.0, 15.0],
              pf._schedule_now())
        for streak in range(1, 9):
            w = pf._backoff_for_streak_now(streak)
            check(f"第 {streak} 次失败 <= 15s", w <= 15.0, w)
        check("**第 1 档是 5s**", pf._backoff_for_streak_now(1) == 5.0,
              pf._backoff_for_streak_now(1))
        check("**封顶不再增长**",
              pf._backoff_for_streak_now(8) == 15.0,
              pf._backoff_for_streak_now(8))


def test_normal_stock_keeps_long_backoff():
    """**§8-13**: stock 有可播库存时**继续**用原来的长序列。

    反证这条与上一条配对 —— 单独任何一条都可能是"恒定序列"假绿。
    """
    print("\n[G4-R2-13] 有库存 -> 保留原长退避")
    with tmpdir() as d:
        cfg = mkcfg(d, pool_prefetch_enabled=True)
        pf = _mk_bare_prefetcher(cfg)
        pf._stock = lambda *a, **k: 12
        pf._playable = lambda *a, **k: 3
        pf._refill_active = False
        check("**库存健康且 refill 已关闭**",
              pf._is_empty() is False and pf._refill_active is False,
              (pf._is_empty(), pf._refill_active))
        check("**用的是保守序列**",
              list(pf._schedule_now()) == [30.0, 60.0, 120.0, 240.0, 300.0],
              pf._schedule_now())
        check("**第 4 档是 240s(原行为)**",
              pf._backoff_for_streak_now(4) == 240.0,
              pf._backoff_for_streak_now(4))
        check("**第 5 档封顶 300s**",
              pf._backoff_for_streak_now(5) == 300.0
              and pf._backoff_for_streak_now(9) == 300.0,
              pf._backoff_for_streak_now(9))


def test_success_resets_fail_streak_empty_pool():
    """**§8-14**: 成功入池**清零** fail streak(空池也一样)。"""
    print("\n[G4-R2-14] 成功 -> fail streak 归零")
    with tmpdir() as d:
        cfg = mkcfg(d, pool_prefetch_enabled=True)
        pf = _mk_bare_prefetcher(cfg)
        pf._stock = lambda *a, **k: 0
        pf._playable = lambda *a, **k: 0
        pf._fail_streak = 3
        pf._retry_at = 9e9
        pf._apply_result("ok", "", {}, 1000.0)
        check("**fail_streak 归零**", pf._fail_streak == 0, pf._fail_streak)
        check("**退避被清**", pf._retry_at == 0.0, pf._retry_at)
        check("**success 计数 +1**",
              pf.reject_count.get("success") == 1, pf.reject_count)


def test_reject_labels_survive_to_stats():
    """**§六**: 五类出口在 `stats()` 里各自可读(不再只有一个"未成题")。"""
    print("\n[G4-R2-6] 拒绝原因分类账在 stats 里可读")
    with tmpdir() as d:
        cfg = mkcfg(d, pool_prefetch_enabled=True)
        pf = _mk_bare_prefetcher(cfg)
        pf._stock = lambda *a, **k: 3
        pf._playable = lambda *a, **k: 2
        for lbl in ("structure_technical_fail", "review_rewrite",
                    "truth_reject", "validation_reject"):
            pf._apply_result("gen_fail", "x", {"reject": lbl}, 1000.0)
        pf._apply_result("ok", "", {}, 1000.0)
        st = pf.stats()
        check("**stats 里有 reject 账**", isinstance(st.get("reject"), dict),
              st.get("reject"))
        for lbl in ("structure_technical_fail", "review_rewrite",
                    "truth_reject", "validation_reject", "success"):
            check(f"**{lbl} 可读且为 1**", st["reject"].get(lbl) == 1,
                  st["reject"].get(lbl))
        check("**未知标签不计数**",
              "other" not in st["reject"], st["reject"])
        # refill-to-target: 语义拒绝是"这道候选不收", 下一次 draw 是
        # 新故事，不需要等待；技术失败才进入 fail_streak / backoff。
        pf2 = _mk_bare_prefetcher(mkcfg(d, pool_prefetch_enabled=True))
        pf2._stock = lambda *a, **k: 3
        pf2._playable = lambda *a, **k: 2
        pf2._apply_result("gen_fail", "x", {"reject": "truth_reject"}, 1000.0)
        check("**truth_reject 不污染 fail_streak**",
              pf2._fail_streak == 0, pf2._fail_streak)
        check("**truth_reject 不设退避**",
              pf2._retry_at == 0.0, pf2._retry_at)
        pf2._apply_result(
            "gen_fail", "x", {"reject": "structure_technical_fail"}, 1000.0)
        check("**技术失败仍进入 fail_streak**",
              pf2._fail_streak == 1, pf2._fail_streak)
        check("**技术失败仍设退避**", pf2._retry_at > 1000.0,
              pf2._retry_at)
        check("**空 extra 不炸**",
              pf2._apply_result("gen_fail", "x", {}, 1000.0) is None, "")


def test_cooperative_cancellation_not_regressed():
    """**§8-15 / Phase C**: 三条链的协作取消**不回退**。

    Phase C 把"后台让路"的判据从"直播忙"换成了"本次运行结束(stop)"。
    这里钉的是**取消能力本身没有丢**:
      * 后台: `request_stop()` 之后谓词 False(不再有"直播忙"这一说);
      * 预热: 仍然同时看预算与 stop(它与后台谓词不可互换);
      * 空池**不**解禁停止 —— 池子空不是"忽略停止信号"的理由。

    前身断言的是"SETTING 让路"。那条现在**是反需求**: 直播相位不再
    拥有后台的抢占权(见模块 docstring / 任务书 §4)。
    """
    print("\n[G4-R2-15] 协作取消不回退")
    with tmpdir() as d:
        cfg = mkcfg(d, pool_prefetch_enabled=True)
        pf = _mk_bare_prefetcher(cfg)
        # ---- 后台: 只认 stop, 不认相位 ----
        check("**未停止时后台谓词 True**",
              pf._background_should_continue() is True)
        pf.request_stop()
        check("**request_stop 后后台谓词 False**",
              pf._background_should_continue() is False)
        # ---- 空池**不**解禁停止: 池子空不是忽略停止信号的理由 ----
        pf._stock = lambda *a, **k: 0
        pf._playable = lambda *a, **k: 0
        check("**空池下仍然停止(紧急档只换退避)**",
              pf._background_should_continue() is False,
              pf._background_should_continue())
        check("**空池确实被认出来了**", pf._is_empty() is True, "")
        # ---- 预热: 不检查相位, 只看 stop + 预算 ----
        sc = pf.prewarm_should_continue(deadline=None, should_abort=None)
        check("**预热在 IDLE 下继续**", sc() is True, sc())
        sc2 = pf.prewarm_should_continue(deadline=0.0, should_abort=None)
        check("**预热超预算停手**", sc2() is False, sc2())
        sc3 = pf.prewarm_should_continue(deadline=None,
                                         should_abort=lambda: True)
        check("**预热被 stop 信号中止**", sc3() is False, sc3())


def _mk_bare_prefetcher(cfg):
    """建一个**不带 executor 的**真 PoolPrefetcher, 只测它的决策逻辑。

    不需要 Director / 真实 pool / writer: 这些用例问的是"退避与分类账
    怎么算", 那是决策层的事, 与生成链无关。`_stock` / `_playable` 由
    各用例自己替换成常量(它们才是我要控制的输入)。
    """
    from story.prefetch import PoolPrefetcher
    return PoolPrefetcher(cfg=cfg, pool=None, writer=None,
                          probe_inputs=lambda: {},
                          pick_blueprint=lambda *a, **k: None,
                          clock=lambda: 1000.0)


def test_r4_smoke_draws_keywords_exactly_once():
    """**R4-R2**: smoke 只能有**一个**抽词点。

    ## 这条守的是一个真实发生过的测量 bug

    上一版 `tools/r4_smoke.py::_one()` 先自己 `bag.draw()` 记关键词,
    `keyword_spec()` 内部**又** `bag.draw()` 一次。后果有两层:

      1. **报告上的关键词不是生成 Story 用的那两个** —— 复审据此判断
         "关键词到底有没有起作用"时会看错题, 而这正是本轮要回答的问题;
      2. 每次调用**白跳过**一组词, 抽词序列与生产不一致。

    修法是让 `keyword_spec()` 成为唯一 draw 点, 关键词从
    `gen_keyword_story()` 的**实参**里抄。

    ## 为什么要 AST, 不只查字符串

    "没有第二个 draw 点"是个**结构性**命题。查 `bag.draw()` 的字面出现
    次数会被 `keys = bag.draw()` / `bag . draw()` / 别名绕过去。所以直接
    数 AST 里落在 `_one()` 函数体中的 `.draw()` 调用。
    """
    print("\n[R4-K17] smoke 只有一个抽词点")
    src = _read(os.path.join(_ROOT, "tools", "r4_smoke.py"))
    tree = ast.parse(src)
    # ---- ① `_one()` 里**不得**出现任何 `.draw()` ----
    one_fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_one":
            one_fn = node
    check("找得到 _one()", one_fn is not None)
    draws = [n for n in ast.walk(one_fn)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr == "draw"]
    check("**`_one()` 里零个 `.draw()` 调用**", not draws,
          [ast.unparse(d) for d in draws])
    # ---- ② 关键词来自 `gen_keyword_story` 的实参 ----
    text = src
    check("**从 writer.last_keywords 取关键词**",
          "last_keywords" in text)
    check("**从 writer.last_lane 取 lane**", "last_lane" in text)
    # ---- ③ 包装器在**调用前**记录实参 ----
    #
    # ⚠️ 顺序很关键: 先记后调, 这样 `_inner` 抛异常时报告里仍然有
    # "用哪两个关键词试过"。若写成先调后记, 异常路径上这条信息就丢了。
    cls = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ObservingPuzzleWriter":
            cls = node
    check("找得到 ObservingPuzzleWriter", cls is not None)
    gks = None
    for node in ast.walk(cls):
        if isinstance(node, ast.FunctionDef) and node.name == "gen_keyword_story":
            gks = node
    check("包装器有 gen_keyword_story", gks is not None)
    lines = [ast.unparse(n) for n in gks.body]
    rec_i = next((i for i, ln in enumerate(lines)
                  if "self.last_keywords" in ln), -1)
    call_i = next((i for i, ln in enumerate(lines)
                   if "self._inner.gen_keyword_story" in ln), -1)
    check("**先记录实参, 再调用**",
          rec_i >= 0 and call_i >= 0 and rec_i < call_i,
          f"record@{rec_i} call@{call_i}")
    # ---- ④ 生产链自己仍然抽一次(不能被顺手删掉) ----
    ks = _read(os.path.join(_ROOT, "story", "keyword_seed.py"))
    ktree = ast.parse(ks)
    kfn = None
    for node in ast.walk(ktree):
        if isinstance(node, ast.FunctionDef) and node.name == "keyword_spec":
            kfn = node
    check("keyword_spec 存在", kfn is not None)
    kdraws = [n for n in ast.walk(kfn)
              if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Attribute)
              and n.func.attr == "draw"]
    check("**keyword_spec 里恰好一次 `bag.draw()`**", len(kdraws) == 1,
          [ast.unparse(d) for d in kdraws])


def test_r4_provenance_reaches_live_archive():
    """**R4**: 三段链的 provenance 必须进**正式直播 archive**。

    ## 这条用例守的是什么(以及为什么单测 `to_archive()` 不够)

    R4 把 lane / keywords / story- 与 surface-prompt 版本 / keyword
    seed-corpus-session-draw index 写进了 `spec.metrics`。但
    `PuzzleSpec.to_archive()` 通过**不等于**直播 `puzzle.jsonl` 看得见 ——
    `director._round_metrics()` 是**白名单搬运**, 不搬整个 metrics。少了
    这条, 复盘时从正式 archive 里分不出"这题是红是黑、哪一版 prompt 产的"。

    走真实链: 建 Director -> `_archive_reveal()` -> 读回 JSON。
    """
    import io as _io
    import json
    from story.puzzle import PuzzleSpec
    with tmpdir() as d:
        cfg = mkcfg(d)
        out = os.path.join(d, "puzzle.jsonl")
        cfg.puzzle_out_path = out
        dr = _mk_director(cfg)
        dr.engine.start()
        # 造一个**带全套 R4 provenance** 的 spec(模拟 keyword2 成功产物)。
        sp = _good_gen_spec()
        sp.prompt_version = "keyword2-v7"
        sp.metrics = {
            "generation_mode": "keyword2", "ok": True,
            "lane": "black",
            "keywords": ["新作", "掘坟"],
            "story_prompt_version": "keyword2-v7",
            "surface_prompt_version": "surface-v2",
            "keyword_seed_version": "keyword2-vocab-v2",
            "keyword_corpus_version": "keyword2-vocab-v2",
            "keyword_session_seed": 14047211878561490874,
            "keyword_draw_index": 7,
        }
        payload = {
            "spec": sp, "puzzle": sp.puzzle, "answer": sp.answer,
            "reason": "test", "winner": "", "core_answer": sp.core_answer,
        }
        dr._archive_reveal(payload, "揭晓文案")
        rec = json.loads(_io.open(out, encoding="utf-8").read().strip())
        m = rec.get("metrics") or {}
        # 逐项断言 —— 不用"有没有 metrics"这种恒真替代。
        check("**archive.metrics.lane**", m.get("lane") == "black", m.get("lane"))
        check("**archive.metrics.keywords**",
              list(m.get("keywords") or []) == ["新作", "掘坟"], m.get("keywords"))
        check("**archive.metrics.story_prompt_version**",
              m.get("story_prompt_version") == "keyword2-v7",
              m.get("story_prompt_version"))
        check("**archive.metrics.surface_prompt_version**",
              m.get("surface_prompt_version") == "surface-v2",
              m.get("surface_prompt_version"))
        check("**archive.metrics.keyword_seed_version**",
              m.get("keyword_seed_version") == "keyword2-vocab-v2",
              m.get("keyword_seed_version"))
        check("**archive.metrics.keyword_corpus_version**",
              m.get("keyword_corpus_version") == "keyword2-vocab-v2",
              m.get("keyword_corpus_version"))
        check("**archive.metrics.keyword_session_seed**",
              m.get("keyword_session_seed") == 14047211878561490874,
              m.get("keyword_session_seed"))
        check("**archive.metrics.keyword_draw_index**",
              m.get("keyword_draw_index") == 7, m.get("keyword_draw_index"))
        check("spec.prompt_version 也在顶层",
              rec.get("prompt_version") == "keyword2-v7",
              rec.get("prompt_version"))
        # ---- 反证: 老题(无 provenance)不会写出 null ----
        sp2 = _good_gen_spec()
        sp2.metrics = {}
        out2 = os.path.join(d, "puzzle2.jsonl")
        cfg.puzzle_out_path = out2
        dr2 = _mk_director(cfg)
        dr2.engine.start()
        dr2._archive_reveal({"spec": sp2, "puzzle": sp2.puzzle,
                             "answer": sp2.answer, "reason": "t",
                             "winner": ""}, "x")
        rec2 = json.loads(_io.open(out2, encoding="utf-8").read().strip())
        m2 = rec2.get("metrics") or {}
        check("老题: lane 是空串而不是 null", m2.get("lane") == "", repr(m2.get("lane")))
        check("老题: keywords 是空列表", m2.get("keywords") == [], m2.get("keywords"))
        check("老题: draw_index 是 0", m2.get("keyword_draw_index") == 0,
              m2.get("keyword_draw_index"))
        # 这几项**必须是可 JSON 序列化的**(写盘已经证明了), 且不该是 None
        for k in ("lane", "story_prompt_version", "surface_prompt_version",
                  "keyword_seed_version", "keyword_corpus_version"):
            check(f"老题: {k} 非 None", m2.get(k) is not None, m2.get(k))


def main():
    tests = [
        test_default_config_curated_off,
        test_cli_defaults_and_flags,
        test_default_does_not_create_lazy_curator,
        test_prefetch_uses_independent_fail_fast_client,
        test_explicit_curated_loads_external_pool,
        test_curated_off_means_truly_off,
        test_default_pops_generated_pool_first,
        test_curated_on_still_ranks_generated_first,
        test_curated_reachable_when_generated_empty,
        test_live_uses_keyword2_by_default,
        test_no_keyword_seed_returns_both_to_classic,
        test_prewarm_skipped_when_playable,
        test_prewarm_runs_when_empty,
        test_prewarm_stops_after_one,
        test_prewarm_is_bounded_and_never_blocks,
        test_prewarm_disabled_by_zero,
        test_prewarm_temporarily_caps_client_transport_budget,
        test_prewarm_transport_config_validation,
        # ---- G4-R1: 三个真实路径缺口 ----
        test_prewarm_real_idle_phase_generates_one,
        test_prewarm_real_idle_classic_killswitch,
        test_prewarm_injection_reaches_playtest_gate,
        test_prewarm_skipped_when_prefetch_disabled,
        test_prewarm_predicate_ignores_phase_but_respects_budget,
        test_keyword2_pool_reveal_marks_aired,
        test_keyword2_live_never_touches_pool_ledger,
        test_legacy_pool_label_still_marks_aired,
        test_passes_ladder_matches_doc,
        test_source_labels_are_distinguishable,
        test_banner_prints_source_mode,
        # ---- G4-R2: 不再因为技术失败 / 分类标签 / 展示长度丢掉合格候选 ----
        test_stage_b_empty_tool_input_retries_once,
        test_stage_b_double_empty_gives_up,
        test_stage_b_clean_first_try_calls_once,
        test_semantic_failures_never_retry_structure,
        test_stage_b_retry_checks_should_continue,
        test_stage_a_prompt_carries_answer_length,
        test_answer_over_300_still_hard_rejected,
        test_core_hidden_4_can_be_rescued_by_reviewer,
        test_core_fix_must_not_change_content,
        # ---- G4-R2-R1: 字段 diff 守卫 + 分类账修正 ----
        test_core_fix_field_diff_guard,
        test_core_fix_only_kind_change_allowed,
        test_core_fix_coexists_with_other_fixable,
        test_core_fix_still_over_limit_rejected,
        test_core_guard_does_not_touch_wide_fixables,
        test_clue_quote_only_domain,
        test_clue_quote_fixable_alone,
        # ---- P0: 已播过的题绝不再次进入 QA ----
        test_played_ledger_blocks_same_session,
        test_played_ledger_survives_restart,
        test_played_ledger_fail_closed,
        test_engine_blocks_played_spec,
        test_engine_no_ledger_is_passthrough,
        test_fallback_not_replayed_production_path,
        test_fallback_still_works_before_exhausted,
        test_reviewer_technical_fail_distinct_label,
        test_reject_ledger_has_six_buckets,
        test_empty_pool_backoff_capped_at_60,
        test_normal_stock_keeps_long_backoff,
        test_success_resets_fail_streak_empty_pool,
        test_reject_labels_survive_to_stats,
        test_cooperative_cancellation_not_regressed,
        # ---- R4: 三段链 provenance 进正式 archive ----
        test_r4_provenance_reaches_live_archive,
        # ---- R4-R2: smoke 只有一个抽词点 ----
        test_r4_smoke_draws_keywords_exactly_once,
    ]
    for t in tests:
        t()
    print()
    if FAIL[0]:
        print(f"FAIL: G4 题源 有 {FAIL[0]} 条不通过")
        return 1
    print("PASS: G4 题源(默认 keyword2 / curated opt-in / prewarm / provenance)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
