#!/usr/bin/env python
# coding: utf-8
"""离线预热题池(G3) —— **开播前**把 current-policy 库存补到目标。

## 为什么需要它

实播日志踩过的坑:

    题池 10 道候选
    但几乎全是 quality-v4/v5/v6/v7
    current v8 playable = 0

于是直播一开始就等于"观众已经在看了, 我们才开始从 0 造新库存"。而
`quality-v8` 的题要过完整质量链(生成 + 审稿 + truth audit + 跨题门),
一道几十秒 —— 让它在**直播的 60 秒揭晓窗口里**补出一个空池, 本来
就不现实。

正确的运营方式:

    先 warm pool  ->  再开播

而不是指望直播中的 Reveal 把一个空 v8 池硬补起来。

## 它**只**做一件事

    往池子里加 current-policy 的题, 直到达成目标或尝试耗尽。

具体到代码, 这意味着本脚本:

    ✓ 用与 prefetch **同一套** writer / 抽词 / 质量链
    ✓ 达到目标就退出(不是无限跑)
    ✗ 不删/不迁移旧 v4~v7 库存(它们仍然躺在盘上, 只是不会被 pop)
    ✗ 不改 used ledger
    ✗ 不起 Engine、不起 web、不碰直播状态机

为什么"不迁移旧题": 那是**伪装**(旧 policy 假装 v8), 而 Q2 冻结的
不变量里明确禁止。旧题被池门隔离是**正确**行为 —— 缺库存就该补新题,
而不是给旧题换标签。

## R5: 题源必须与直播一致(**生产入口漂移**)

R4 把生产链统一成了

    KeywordBag 两随机词 -> Story -> Surface -> Structure

四个入口都走 `keyword_seed.keyword_spec()`: live 现场生成 / 后台
`PoolPrefetcher` / Director 冷启动 prewarm / **本脚本**。

## Issue #60 §17: category-aware bulk builder(并发=5)

live-ready milestone 之后, 开播前的实际运行必须是:

    prefill_pool.py --live-ready --concurrency 5

等价于 `--target-total 50 --target-per-category 10`:

    distinct current-policy 未播库存 >= 50
    logic / suspense / horror / emotion / brainstorm 各 >= 10

并发=5: 最多 5 条 generation pipeline **同时**跑, 每个 worker **独立**
client + PuzzleWriter, 共享**并发安全**的 KeywordBag(#60 §15 的
`_draw_lock`), 调度按 per-category deficit 选 `requested_category`,
最终入池走 pool 的 **atomic final admission**(§16)。

## 与直播互斥(§17)

live-ready prefill 启动前**必须**检查 live heartbeat/lease。检测到
直播正在活跃 -> 拒绝(退出码非 0), 绝不允许 5-worker 与直播进程
同时写正式 pool。没有提供默认绕过此保护的路径。

⚠️ bag 必须是整进程一只(`_PrefillSeeder`)。每次 `_one()` 重建会把
draw_index / cooldown / 不放回状态全部重置 —— 表面上"用了 keyword2",
实际上每一轮都是新 session, 每道题都从 index=1 重来。

## 用法

    .venv/Scripts/python.exe prefill_pool.py --target 5
    .venv/Scripts/python.exe prefill_pool.py --live-ready --concurrency 5
    .venv/Scripts/python.exe prefill_pool.py --target 8 --max-attempts 40

退出码: 0 = 达成目标; 1 = 尝试耗尽仍未达成 / 直播活跃拒绝启动。
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from story.config import Config, from_args as _cfg_from_args
from story.pool import PuzzlePool

#: 复用 director 的日志装配 —— 各自写一份必然漂移。
from director import setup_logging

log = logging.getLogger("prefill")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="prefill_pool",
        description="开播前离线预热题池(只生成 current-policy 的题)")
    ap.add_argument("--target", type=int, default=5,
                    help="目标**库存**数(current-policy 可播题), 默认 5")
    ap.add_argument("--playable", type=int, default=2,
                    help="目标**可播**数(与 target 同时满足才停), 默认 2")
    ap.add_argument("--max-attempts", type=int, default=30,
                    help="最多试生成几道(防止无限烧配额), 默认 30")
    ap.add_argument("--budget", type=float, default=90.0,
                    help="单道题的时间预算秒(默认 90, 与 live 出题一致 —— "
                         "预热不在直播热路径上, 值得多试几稿)")
    ap.add_argument("--max-per-puzzle", type=int, default=4,
                    help="单道题最多几稿(默认 4, 与 live 一致)")
    ap.add_argument("--seed", type=int, default=None,
                    help="调度 rng 种子(默认随机; 给了就可复现)")
    ap.add_argument("--log-level", default="INFO",
                    help="日志级别, 默认 INFO")
    ap.add_argument("--log-file", default="",
                    help="日志文件(默认空 = 只打屏)")
    # 复用直播那套配置解析 —— 池路径 / 质量策略 / LLM 参数都在那儿,
    # 各自再写一份必然漂移。
    ap.add_argument("--no-llm", action="store_true",
                    help="不使用 LLM(只会走兜底, 预热没有意义; 仅供干跑)")
    # ---- R5: 与直播同一条 `--no-keyword-seed` kill-switch ----
    ap.add_argument("--no-keyword-seed", dest="pool_keyword_seed_enabled",
                    action="store_false",
                    help="kill-switch: 预热回到 classic Blueprint 链")
    ap.add_argument("--keyword-corpus", dest="keyword_corpus_path", default="",
                    help="keyword2 的 seed 词库路径")
    ap.add_argument("--keyword-session-seed", dest="keyword_session_seed",
                    type=int, default=None,
                    help="keyword bag 的 session seed。默认由 quality_seed "
                         "派生; 都没有时随机一次并写进 INFO 日志")
    # ---- Issue #60 §17: category-aware bulk ----
    ap.add_argument("--live-ready", action="store_true",
                    help="live-ready preset: 等价于 --target-total 50 "
                         "--target-per-category 10 --max-attempts 150。"
                         "开播前实际运行必须用它(加 --concurrency 5)")
    ap.add_argument("--target-total", type=int, default=None,
                    help="distinct current-policy 未播库存目标(§9)")
    ap.add_argument("--target-per-category", type=int, default=None,
                    help="五类每类 eligible 库存目标(§9)")
    ap.add_argument("--concurrency", type=int, default=1,
                    help="同时跑几条 generation pipeline(live-ready 实际"
                         "运行必须用 5; 上限也是 5)")
    return ap


def _resolve_targets(a) -> tuple:
    """把 --live-ready 展开成 (total, per_category, max_attempts, conc)。"""
    if getattr(a, "live_ready", False):
        total = a.target_total if a.target_total is not None else 50
        per_cat = a.target_per_category if a.target_per_category is not None \
            else 10
        attempts = max(a.max_attempts if a.max_attempts != 30 else 0, 0) \
            or 150
        conc = a.concurrency or 5
        return total, per_cat, attempts, min(5, max(1, conc))
    total = a.target_total if a.target_total is not None else a.target
    per_cat = a.target_per_category or 0
    conc = min(5, max(1, a.concurrency))
    return total, per_cat, a.max_attempts, conc


def _check_live_active() -> bool:
    """§17: 直播活跃 -> 拒绝 bulk prefill。"""
    from story.live_heartbeat import DEFAULT_PATH, live_is_active
    try:
        return live_is_active(DEFAULT_PATH)
    except Exception:                           # noqa: BLE001
        log.exception("读 live heartbeat 异常, 保守按**直播活跃**处理")
        return True


def _pool_ready(pool: PuzzlePool, total: int, per_cat: int,
                cfg: Config) -> tuple:
    """当前库存是否达标。返回 `(ok, stats_dict)`。"""
    distinct = pool.distinct_stock_count()
    by_cat = pool.stock_by_category() if per_cat > 0 else {}
    ok = distinct >= total
    if per_cat > 0:
        ok = ok and all(v >= per_cat for v in by_cat.values()) \
            and len(by_cat) == 5
    return ok, {"distinct": distinct, "by_category": by_cat}


def main(argv=None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)
    total, per_cat, max_attempts, concurrency = _resolve_targets(a)

    extra: list = []
    if not a.pool_keyword_seed_enabled:
        extra.append("--no-keyword-seed")
    if a.keyword_corpus_path:
        extra += ["--keyword-corpus", a.keyword_corpus_path]
    if a.keyword_session_seed is not None:
        extra += ["--keyword-session-seed", str(a.keyword_session_seed)]
    cfg: Config = _cfg_from_args(["--sim", "prefill", "--no-window",
                                  "--max-puzzles", "0"] + extra)
    setup_logging(a.log_level, a.log_file or None)

    if a.no_llm or not getattr(cfg, "llm", None):
        log.error("预热需要一个可用的 LLM 配置 —— --no-llm 下只会走兜底题, "
                  "没有意义。")
        return 1

    if bool(getattr(cfg, "pool_keyword_seed_enabled", True)) \
            != bool(a.pool_keyword_seed_enabled):
        log.error("--no-keyword-seed 没有透传到 Config(配置=%s, 参数=%s)"
                  " —— 拒绝在语义不明的状态下预热",
                  getattr(cfg, "pool_keyword_seed_enabled", None),
                  a.pool_keyword_seed_enabled)
        return 1

    # ---- §17: 直播活跃 -> 拒绝(不提供默认绕过路径) ----
    if _check_live_active():
        log.error("检测到直播正在活跃(live heartbeat 新鲜) —— "
                  "拒绝 bulk prefill: 5-worker 与直播进程同时写正式 pool "
                  "是明确禁止的。请先停播再跑预热。")
        return 1

    log.info("预热目标: distinct>=%d%s, 并发=%d, 尝试上限=%d",
             total, (f", 每类>={per_cat}" if per_cat else ""),
             concurrency, max_attempts)

    seeder = _make_seeder(cfg)

    pool = PuzzlePool.open(cfg)
    ok0, st0 = _pool_ready(pool, total, per_cat, cfg)
    log.info("现有 current-policy 库存 %d (可播 %d)%s",
             st0["distinct"], pool.playable_count([], []),
             (f", 分类 {st0['by_category']}" if st0["by_category"] else ""))
    if ok0:
        log.info("已经达标, 什么也不做。")
        return 0

    rng = random.Random(a.seed)
    writer = _make_writer(cfg)

    # ---- §17: 5 并发 bulk ----
    done_count = {"n": 0}
    lock = threading.Lock()
    attempts = {"n": 0}
    stop_event = threading.Event()

    def _targets_met() -> bool:
        ok, _st = _pool_ready(pool, total, per_cat, cfg)
        return ok

    def _worker(slot: int) -> None:
        """一个 generation pipeline: 独立 writer, 共享 seeder.bag。

        调度: 每道开始前重读 deficit(哪些类还差), 选 requested_category;
        生成走 keyword_spec(生产唯一入口), 入池走 atomic final admission。
        """
        while (not stop_event.is_set()
               and attempts["n"] < max_attempts
               and not _targets_met()):
            with lock:
                if attempts["n"] >= max_attempts:
                    return
                attempts["n"] += 1
                i = attempts["n"]
            requested = _pick_deficit(pool, per_cat)
            log.info("worker-%d 第 %d/%d 次尝试(requested=%s)",
                     slot, i, max_attempts, requested or "自由生成")
            if _one(writer, pool, cfg, rng, a, seeder,
                    requested_category=requested,
                    should_continue=lambda: not stop_event.is_set()):
                done_count["n"] += 1
                log.info("worker-%d 入池成功(本次已补 %d 道)",
                         slot, done_count["n"])

    if concurrency <= 1:
        _worker(0)
    else:
        with ThreadPoolExecutor(max_workers=concurrency,
                                thread_name_prefix="prefill") as ex:
            futures = [ex.submit(_worker, s) for s in range(concurrency)]
            for f in futures:
                f.result()

    ok, st = _pool_ready(pool, total, per_cat, cfg)
    if ok:
        log.info("达成目标: distinct %d%s, 用了 %d 次尝试",
                 st["distinct"],
                 (f", 分类 {st['by_category']}" if st["by_category"] else ""),
                 attempts["n"])
        return 0
    log.warning("尝试耗尽仍未达标: distinct %d%s, 目标 %d%s。"
                "本次补进 %d 道。",
                st["distinct"],
                (f", 分类 {st['by_category']}" if st["by_category"] else ""),
                total, (f"/每类{per_cat}" if per_cat else ""),
                done_count["n"])
    return 1


def _pick_deficit(pool: PuzzlePool, per_cat: int) -> str:
    """按 per-category deficit 选 requested_category(§17)。

    五类各差的量从大到小; 全部达标 -> ""(自由生成, 补 distinct 总量)。
    """
    if per_cat <= 0:
        return ""
    from story.haiguitang_protocol import V2_CATEGORIES
    by_cat = pool.stock_by_category()
    deficits = [(per_cat - by_cat.get(c, 0), c) for c in V2_CATEGORIES]
    deficits = [(d, c) for d, c in deficits if d > 0]
    if not deficits:
        return ""
    deficits.sort(reverse=True)
    return deficits[0][1]


# ======================================================================
# R5: 题源装配 —— 与直播**同一套**入口
# ======================================================================
def _make_writer(cfg: Config):
    """建一个与 prefetch **同构**的 writer(client 共用, 实例独立)。"""
    from story.llm import AnthropicMessagesClient, PuzzleWriter
    client = AnthropicMessagesClient(cfg.llm)
    return PuzzleWriter(client=client, runtime_cfg=cfg)


class _PrefillSeeder:
    """R5: 预热进程的**唯一一只** keyword bag + 它的 session seed。

    ## 为什么必须是"整进程一只"

    `KeywordBag` 是**有状态**的: 它维护 `_recent_kw` / `_recent_pair`
    两个滑动窗口与 `served` 计数, `draw()` 每调一次才推进一格。
    每次 `_one()` 重建 bag => 每道题都从 index=1 开始、cooldown 窗口
    恒为空。⚠️ Issue #60 §15: 并发(5 worker)后 bag 是**共享**的,
    `draw()` 内部已持锁 —— 状态转移原子, index 单调唯一。

    ## 为什么与直播的 session seed 口径一致

    `PoolPrefetcher._init_keyword_bag` 的规则逐条照搬:
        给了 keyword_session_seed  -> 直接用
        只给了 quality_seed        -> derive_session_seed() 派生
        两个都没给                  -> 随机一次, 并**立刻写进 INFO 日志**
    """

    def __init__(self, enabled: bool, bag=None, session_seed=None,
                 bag_meta=None, error: str = ""):
        self.enabled = bool(enabled)
        self.bag = bag
        self.session_seed = session_seed
        self.bag_meta = dict(bag_meta or {})
        self.error = error

    @property
    def corpus_version(self) -> str:
        return str(self.bag_meta.get("corpus_version") or "")

    def meta_line(self) -> str:
        from story.keyword_seed import describe_bag
        return describe_bag(self.bag_meta, self.session_seed)


def _make_seeder(cfg: Config) -> _PrefillSeeder:
    """按配置装配 seeder。**corpus 坏了就显式降级, 绝不假装在跑 keyword2。**"""
    if not bool(getattr(cfg, "pool_keyword_seed_enabled", True)):
        log.info("预热走 classic Blueprint 链(--no-keyword-seed kill-switch)")
        return _PrefillSeeder(enabled=False)

    try:
        from story.keyword_corpus import DEFAULT_CORPUS_PATH
        from story.keyword_seed import derive_session_seed, load_bag
        path = str(getattr(cfg, "keyword_corpus_path", "")
                   or DEFAULT_CORPUS_PATH)
        ss = getattr(cfg, "keyword_session_seed", None)
        if ss is None:
            qseed = getattr(cfg, "quality_seed", None)
            if qseed is not None:
                ss = derive_session_seed(qseed)
            else:
                ss = random.SystemRandom().getrandbits(64)
        bag, meta = load_bag(path, ss)
    except Exception as e:                       # noqa: BLE001
        log.error(
            "keyword2 corpus 不可用, **本次预热整条 keyword2 链让位给 "
            "classic Blueprint 链**(不会回退人工词库): %s: %s。"
            "⚠️ 这一轮补出来的题会是 riddle-v9, *不是* keyword2。",
            type(e).__name__, e)
        return _PrefillSeeder(enabled=False,
                              error="%s: %s" % (type(e).__name__, e))

    s = _PrefillSeeder(enabled=True, bag=bag, session_seed=int(ss),
                       bag_meta=meta)
    log.info("keyword2 bag 就绪(**整进程共用这一只, 并发安全**): %s",
             s.meta_line())
    return s


def _one(writer, pool: PuzzlePool, cfg: Config, rng, a,
         seeder: _PrefillSeeder, requested_category: str = "",
         should_continue=None) -> bool:
    """试生成一道并入池。返回是否真的进了池子。失败**不抛**。"""
    # 每一道都**重新读**库存签名 —— 刚补进去的那道必须立刻进入下一道
    # 的 recent(G4-B)。
    recent = _recent_sigs(pool, cfg)

    if seeder.enabled:
        return _one_keyword(writer, pool, cfg, a, seeder, recent,
                            requested_category=requested_category,
                            should_continue=should_continue)
    return _one_classic(writer, pool, cfg, rng, a, recent,
                        should_continue=should_continue)


def _one_keyword(writer, pool: PuzzlePool, cfg: Config, a,
                 seeder: _PrefillSeeder, recent: list,
                 requested_category: str = "",
                 should_continue=None) -> bool:
    """keyword2 链: 与 live / prefetch **同一个** `keyword_spec` 入口。

    ⚠️ Issue #60 §12: requested_category 通过 GenerationBrief 定向,
    仍走生产唯一入口, 不复制 Prompt/Truth/Surface/Contract 逻辑。
    ⚠️ Issue #60 §16: 入池走 **atomic final admission** —— check 与
    add 在 pool 的同一把锁内, 并发 worker 的相似 candidate 不能都穿过。
    """
    from story.keyword_seed import keyword_spec
    from story.haiguitang_protocol import GenerationBrief

    def _always_continue() -> bool:
        return True

    go = should_continue or _always_continue
    if not go():
        log.info("预热/守护补池让路: 生成前检测到直播已活跃")
        return False

    brief = (GenerationBrief(requested_category=requested_category)
             .require_valid() if requested_category else None)
    spec, reason = keyword_spec(
        writer, seeder.bag, seeder.session_seed,
        avoid=[], recent=recent,
        should_continue=go,
        corpus_version=seeder.corpus_version,
        brief=brief)
    if spec is None:
        if reason == "interrupted":
            log.info("预热/守护补池让路: keyword2 中途检测到直播已活跃")
        else:
            log.warning("keyword2 未成题(%s; 细因见 metrics.reject)", reason)
        return False
    # 最关键的一次复查：候选已经做完，但直播可能刚在最后一个 LLM
    # 请求期间启动。此时宁可丢这一稿，也绝不与直播进程并发写 pool。
    if not go():
        log.info("预热/守护补池让路: 候选完成后直播已活跃，本稿不入池")
        return False
    # ---- Issue #60 §16: atomic final admission ----
    try:
        ok, why = pool.add_with_final_admission(spec, source="prefill",
                                                recent=recent)
    except AttributeError:
        # 替身 pool(测试)只有普通 add —— 行为等价(替身不模拟并发)。
        ok, why = (bool(pool.add(spec, source="prefill")),
                   "pool.add 返回 False")
    if not ok:
        log.warning("入池被拒(%s; 继续下一道)", why)
        return False
    return True


def _one_classic(writer, pool: PuzzlePool, cfg: Config, rng, a,
                 recent: list, requested_category: str = "",
                 should_continue=None) -> bool:
    """classic 链(逐位不变): `choose_blueprint -> gen_spec` -> riddle-v9。"""
    from story.quality import Quotas, choose_blueprint

    bp = choose_blueprint(recent, rng=rng, quotas=Quotas.from_config(cfg))
    go = should_continue or (lambda: True)
    if not go():
        log.info("预热/守护补池让路: classic 生成前直播已活跃")
        return False
    try:
        spec = writer.gen_spec(
            avoid=[], blueprint=bp, recent=recent,
            max_attempts=a.max_per_puzzle, budget_s=a.budget,
            should_continue=go,
            enforce_blueprint=bp is not None)
    except Exception:                           # noqa: BLE001
        log.exception("生成异常(继续下一道)")
        return False
    if spec is None or not getattr(spec, "puzzle", "") or spec.error:
        log.warning("生成失败: %s",
                    (spec.error if spec is not None else "spec=None"))
        return False
    if not go():
        log.info("预热/守护补池让路: classic 候选完成后直播已活跃，本稿不入池")
        return False
    try:
        ok, why = pool.add_with_final_admission(spec, source="prefill",
                                                recent=recent)
    except AttributeError:
        ok, why = (bool(pool.add(spec, source="prefill")),
                   "pool.add 返回 False")
    if not ok:
        log.warning("入池被拒(%s; 继续下一道)", why)
        return False
    return True


def _recent_sigs(pool: PuzzlePool, cfg: Config) -> list:
    """当前库存题的 signature —— 跨题配额要看**盘上真正能播的**有什么。"""
    try:
        return list(pool.stock_signatures())
    except Exception:                           # noqa: BLE001
        log.exception("读取库存签名失败, 本次按空窗口处理")
        return []


if __name__ == "__main__":
    sys.exit(main())
