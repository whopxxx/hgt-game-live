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

import io
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
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
    check("prewarm 参数存在且为正",
          c.pool_prewarm_max_rounds > 0 and c.pool_prewarm_max_seconds > 0,
          (c.pool_prewarm_max_rounds, c.pool_prewarm_max_seconds))


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

    def gen_keyword_idea(self, *a, **k):
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

            def gen_keyword_idea(self, *a, **k):
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

            def gen_keyword_idea(self, *a, **k):
                raise AssertionError("关掉 keyword2 后不该调 Stage A")

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
    for kw, want_mode, want_cur in (
            ({}, "keyword2 generated", "OFF"),
            ({"prefer_curated": True}, "keyword2 generated", "ON"),
            ({"pool_keyword_seed_enabled": False},
             "classic Blueprint", "OFF")):
        with tmpdir() as d:
            cfg = mkcfg(d, **kw)
            cfg.pool_prefetch_enabled = False
            # `run()` 会先 `cfg.validate()`, 而它要求**必须有输入源**。
            # 给一个空的 sim 脚本 —— banner 在数据源真正被读之前就打完了
            # (而且下面那个 `_build_source` 替身会在它之前截断)。
            sim = os.path.join(d, "empty.jsonl")
            with open(sim, "w", encoding="utf-8") as f:
                f.write("")
            cfg.sim_path = sim
            dr = _D.Director(cfg)
            buf = io.StringIO()
            old = _D._console
            _D._console = buf

            # 在 banner 之后的第一件事上截断。`_build_source` 是
            # banner 段结束后的第一个动作, 所以到这里 banner 必然已经
            # 全部写完。
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
            lines = buf.getvalue().splitlines()
        text = "\n".join(lines)
        check(f"**模式行: {want_mode}**", want_mode in text,
              [l for l in lines if "题源" in l])
        check(f"**curated 行: {want_cur}**",
              any(l.startswith("  curated     : " + want_cur) for l in lines),
              [l for l in lines if l.startswith("  curated     :")])


def main():
    tests = [
        test_default_config_curated_off,
        test_cli_defaults_and_flags,
        test_default_does_not_create_lazy_curator,
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
        test_source_labels_are_distinguishable,
        test_banner_prints_source_mode,
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
