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

    ✓ 用与 prefetch **同一套** writer / blueprint 调度 / 质量链
    ✓ 达到目标就退出(不是无限跑)
    ✗ 不删/不迁移旧 v4~v7 库存(它们仍然躺在盘上, 只是不会被 pop)
    ✗ 不改 used ledger
    ✗ 不起 Engine、不起 web、不碰直播状态机

为什么"不迁移旧题": 那是**伪装**(旧 policy 假装 v8), 而 Q2 冻结的
不变量里明确禁止。旧题被池门隔离是**正确**行为 —— 缺库存就该补新题,
而不是给旧题换标签。

## 用法

    .venv/Scripts/python.exe prefill_pool.py --target 5
    .venv/Scripts/python.exe prefill_pool.py --target 5 --playable 2
    .venv/Scripts/python.exe prefill_pool.py --target 8 --max-attempts 40

退出码: 0 = 达成目标; 1 = 尝试耗尽仍未达成(不是崩溃, 是"没补够")。
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import sys

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
    return ap


def main(argv=None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)
    # 先让 Config 的解析器处理一遍, 拿到池路径 / LLM 配置 / 质量策略 ——
    # 预热必须用与直播**完全一样**的配置, 否则补出来的题可能过不了
    # 直播那侧的池门。
    cfg: Config = _cfg_from_args(["--sim", "prefill", "--no-window",
                                  "--max-puzzles", "0"])
    setup_logging(a.log_level, a.log_file or None)

    if a.no_llm or not getattr(cfg, "llm", None):
        log.error("预热需要一个可用的 LLM 配置 —— --no-llm 下只会走兜底题, "
                  "没有意义。")
        return 1

    pool = PuzzlePool.open(cfg)
    before_stock = pool.stock_count()
    before_playable = pool.playable_count([], [])
    log.info("预热开始: 现有 current-policy 库存 %d(可播 %d), "
             "目标 库存>=%d 且 可播>=%d",
             before_stock, before_playable, a.target, a.playable)

    if before_stock >= a.target and before_playable >= a.playable:
        log.info("已经达标, 什么也不做。")
        return 0

    rng = random.Random(a.seed)
    writer = _make_writer(cfg)
    done = 0
    for i in range(1, a.max_attempts + 1):
        stock = pool.stock_count()
        playable = pool.playable_count([], [])
        if stock >= a.target and playable >= a.playable:
            log.info("达成目标: 库存 %d(可播 %d), 用了 %d 次尝试",
                     stock, playable, i - 1)
            return 0
        log.info("第 %d/%d 次尝试(当前 库存 %d 可播 %d)",
                 i, a.max_attempts, stock, playable)
        if _one(writer, pool, cfg, rng, a):
            done += 1
            log.info("入池成功(本次已补 %d 道)", done)
    stock = pool.stock_count()
    playable = pool.playable_count([], [])
    log.warning("尝试耗尽仍未达标: 库存 %d(可播 %d), 目标 %d/%d。"
                "本次补进 %d 道。",
                stock, playable, a.target, a.playable, done)
    return 1


def _make_writer(cfg: Config):
    """建一个与 prefetch **同构**的 writer(client 共用, 实例独立)。"""
    from story.llm import AnthropicMessagesClient, PuzzleWriter
    client = AnthropicMessagesClient(cfg.llm)
    return PuzzleWriter(client=client, runtime_cfg=cfg)


def _one(writer, pool: PuzzlePool, cfg: Config, rng, a) -> bool:
    """试生成一道并入池。返回是否真的进了池子。

    ⚠️ 失败**不抛** —— 预热是个循环, 一道不成就试下一道, 让
    `max_attempts` 去兜底。抛出去只会让整个预热停在一道坏题上。
    """
    from story.quality import Quotas, choose_blueprint

    # 每一道都**重新读**库存签名 —— 刚补进去的那道必须立刻进入下一道
    # 的 recent, 否则同一个 pair 会连着补好几道(G4-B)。
    recent = _recent_sigs(pool, cfg)
    bp = choose_blueprint(recent, rng=rng, quotas=Quotas.from_config(cfg))
    try:
        spec = writer.gen_spec(
            avoid=[], blueprint=bp, recent=recent,
            max_attempts=a.max_per_puzzle, budget_s=a.budget,
            enforce_blueprint=bp is not None)
    except Exception:                           # noqa: BLE001
        log.exception("生成异常(继续下一道)")
        return False
    if spec is None or not getattr(spec, "puzzle", "") or spec.error:
        log.warning("生成失败: %s",
                    (spec.error if spec is not None else "spec=None"))
        return False
    if not pool.add(spec, source="prefill"):
        log.warning("入池被拒(继续下一道)")
        return False
    return True


def _recent_sigs(pool: PuzzlePool, cfg: Config) -> list:
    """当前库存题的 signature —— 跨题配额要看**盘上真正能播的**有什么。

    这是预热与直播 prefetch 的一个**有意差别**: 直播的 recent 来自
    Engine(观众已经看过什么), 而预热时还没有观众, 能参考的只有池子
    本身。用池子内容当 recent 才能保证"预热出来的这一批**彼此**不
    结构重复" —— 否则会补进 5 道一模一样的题, 而它们互相挡着,
    playable 仍然是 1。

    ## G4-B: 这里曾经是**永远返回 []** 的

    旧实现直接摸 `pool._items`:

        for rec in pool._items:
            if isinstance(rec, dict):
                sig = rec.get("signature")

    而 `_items` 里装的是 `PuzzleSpec` **对象** —— `isinstance(rec, dict)`
    恒为假, 于是每一轮 recent 都是空的。脚本仍然会打印"达标", 但它
    补出来的 5 道题可能全是同一个 mechanism/shape; 等第一道真播完进入
    Engine 的 recent, 剩下的立刻被动态门挡住。**预热成功, 开播即塌。**

    现在走 `PuzzlePool.stock_signatures()`: 只取 `not used` + 过静态
    校验(含 quality policy 门)的题, 且返回副本。旧 policy / 已 used
    的题不会污染 prefill 的 recent。

    调用方**每一道之后都要重新调这个函数**(见 `main` 的循环)——
    刚补进去的题要立刻进入下一道的 recent, 否则同一个 pair 会连补
    好几道。
    """
    try:
        return list(pool.stock_signatures())
    except Exception:                           # noqa: BLE001
        log.exception("读取库存签名失败, 本次按空窗口处理")
        return []


if __name__ == "__main__":
    sys.exit(main())
