#!/usr/bin/env python
# coding: utf-8
"""generated 题池常驻蓄水守护。

职责只有一个: **直播不在运行时**把 current-policy generated pool 补到
稳定水位。直播一旦出现 fresh heartbeat，本进程立即让路；直播期间的
补池仍由 Director 内置 PoolPrefetcher 单飞负责。

这样刻意避免两个 Python 进程同时维护同一份 PuzzlePool 内存快照。守护
进程每次从 live -> idle 后都会重新 load()，先吸收本场 used 账本，再补。
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import sys
import time

from director import setup_logging
from prefill_pool import _make_seeder, _make_writer, _one
from story.config import Config, from_args as _cfg_from_args
from story.live_heartbeat import (
    DEFAULT_PATH as LIVE_HEARTBEAT_PATH,
    DEFAULT_STALE_SECONDS as LIVE_STALE_SECONDS,
    live_is_active,
)
from story.pool import PuzzlePool

log = logging.getLogger("pool_refill")


class _SingleInstance:
    """跨平台 OS 文件锁；进程退出/崩溃后由操作系统自动释放。"""

    def __init__(self, path: str):
        self.path = path
        self._f = None

    def acquire(self) -> bool:
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._f = open(self.path, "a+b")
        try:
            if os.path.getsize(self.path) == 0:
                self._f.write(b"0")
                self._f.flush()
            self._f.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (OSError, IOError):
            self.release()
            return False

    def release(self) -> None:
        if self._f is None:
            return
        try:
            self._f.seek(0)
            if os.name == "nt":
                import msvcrt
                try:
                    msvcrt.locking(self._f.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl
                try:
                    fcntl.flock(self._f.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            try:
                self._f.close()
            except OSError:
                pass
            self._f = None


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="pool_refill",
        description="直播外常驻补 generated 题池；检测到直播 heartbeat 自动暂停")
    ap.add_argument("--target", type=int, default=None,
                    help="目标库存；默认取 Config.pool_target_size")
    ap.add_argument("--playable", type=int, default=None,
                    help="目标可播数；默认取 Config.pool_playable_min")
    ap.add_argument("--attempts-per-cycle", type=int, default=4,
                    help="一次缺货周期最多尝试几道，默认 4")
    ap.add_argument("--interval", type=float, default=15.0,
                    help="库存达标后的巡检间隔秒，默认 15")
    ap.add_argument("--live-poll", type=float, default=2.0,
                    help="直播活跃时的 heartbeat 巡检间隔秒，默认 2")
    ap.add_argument("--heartbeat", default=LIVE_HEARTBEAT_PATH,
                    help="Director 直播 heartbeat 文件")
    ap.add_argument("--live-stale", type=float, default=LIVE_STALE_SECONDS,
                    help="heartbeat 多久未刷新才视为直播已停，默认 10 秒")
    ap.add_argument("--lock", default=os.path.join("data", "pool_refill.lock"),
                    help="守护进程单实例锁文件")
    ap.add_argument("--once", action="store_true",
                    help="只跑一个巡检/补池周期后退出")
    ap.add_argument("--budget", type=float, default=90.0,
                    help="单候选 classic 预算；keyword2 沿自身阶段预算")
    ap.add_argument("--max-per-puzzle", type=int, default=4,
                    help="classic 单候选最多稿数，默认 4")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--log-level", default="INFO")
    ap.add_argument("--log-file", default=os.path.join("data", "pool_refill.log"))
    ap.add_argument("--no-keyword-seed", dest="pool_keyword_seed_enabled",
                    action="store_false")
    ap.add_argument("--keyword-corpus", dest="keyword_corpus_path", default="")
    ap.add_argument("--keyword-session-seed", dest="keyword_session_seed",
                    type=int, default=None)
    return ap


def _config(a) -> Config:
    extra: list[str] = []
    if not a.pool_keyword_seed_enabled:
        extra.append("--no-keyword-seed")
    if a.keyword_corpus_path:
        extra += ["--keyword-corpus", a.keyword_corpus_path]
    if a.keyword_session_seed is not None:
        extra += ["--keyword-session-seed", str(a.keyword_session_seed)]
    return _cfg_from_args(
        ["--sim", "pool-refill", "--no-window", "--max-puzzles", "0"] + extra)


def _active(a) -> bool:
    return live_is_active(a.heartbeat, a.live_stale)


def refill_cycle(pool: PuzzlePool, writer, cfg: Config, rng,
                 seeder, a) -> dict:
    """跑一个缺货周期。每道候选前/中/入池前都可被直播 heartbeat 打断。"""
    if _active(a):
        return {"status": "live", "added": 0}

    # 直播刚结束时，守护进程手里的内存快照是旧的；每个 idle cycle 都
    # reload，先吸收本场新增 used 记录再判断缺不缺。
    pool.load()
    if not pool.ledger_trustworthy:
        log.error("used 账本不可信，守护补池停手；不生成无用库存")
        return {"status": "ledger_untrusted", "added": 0}

    target = max(0, int(a.target if a.target is not None
                        else getattr(cfg, "pool_target_size", 12)))
    playable_target = max(
        0, int(a.playable if a.playable is not None
               else getattr(cfg, "pool_playable_min", 3)))

    def go() -> bool:
        return not _active(a)

    stock = pool.stock_count()
    playable = pool.playable_count([], [])
    if stock >= target and playable >= playable_target:
        return {"status": "full", "added": 0,
                "stock": stock, "playable": playable}

    added = 0
    attempts = max(1, int(a.attempts_per_cycle))
    log.info("守护补池: 库存 %d/目标 %d，可播 %d/目标 %d；本轮最多 %d 次",
             stock, target, playable, playable_target, attempts)
    for i in range(1, attempts + 1):
        if not go():
            log.info("直播 heartbeat 出现，守护补池立即暂停")
            return {"status": "live", "added": added}
        ok = _one(writer, pool, cfg, rng, a, seeder,
                  should_continue=go)
        if not go():
            return {"status": "live", "added": added}
        if ok:
            added += 1
        stock = pool.stock_count()
        playable = pool.playable_count([], [])
        log.info("守护补池 %d/%d: %s；库存 %d，可播 %d",
                 i, attempts, "入池" if ok else "未成", stock, playable)
        if stock >= target and playable >= playable_target:
            return {"status": "full", "added": added,
                    "stock": stock, "playable": playable}

    return {"status": "partial", "added": added,
            "stock": stock, "playable": playable}


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    setup_logging(a.log_level, a.log_file or None)
    lock = _SingleInstance(a.lock)
    if not lock.acquire():
        log.error("已有 pool_refill 在运行；本进程退出，避免双守护并发写池")
        return 2

    try:
        cfg = _config(a)
        if not getattr(cfg, "llm", None):
            log.error("没有可用 LLM 配置，无法守护补池")
            return 1

        target = a.target if a.target is not None else cfg.pool_target_size
        playable = (a.playable if a.playable is not None
                    else cfg.pool_playable_min)
        log.info("守护补池启动: 目标库存=%d 可播=%d；直播活跃时自动暂停",
                 target, playable)

        pool = PuzzlePool.open(cfg)
        if pool is None:
            log.error("generated pool 已关闭，守护补池退出")
            return 1
        writer = _make_writer(cfg)
        seeder = _make_seeder(cfg)
        rng = random.Random(a.seed)

        last = ""
        while True:
            if _active(a):
                if last != "live":
                    log.info("检测到直播 heartbeat：暂停离线补池，由直播内置 prefetch 接管")
                last = "live"
                if a.once:
                    return 0
                time.sleep(max(0.5, float(a.live_poll)))
                continue

            result = refill_cycle(pool, writer, cfg, rng, seeder, a)
            status = str(result.get("status") or "")
            if status != last:
                log.info("守护补池状态: %s", status)
            last = status

            if a.once:
                return 0 if status in ("full", "live") else 1
            # 未达标时短一点重试；已达标后低频巡检。
            wait_s = (float(a.interval) if status == "full"
                      else min(float(a.interval), 5.0))
            time.sleep(max(0.5, wait_s))
    except KeyboardInterrupt:
        log.info("收到 Ctrl-C，守护补池退出")
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
