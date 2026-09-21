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

    KeywordBag 两随机词 + red/black lane -> Story -> Surface -> Structure

四个入口都走 `keyword_seed.keyword_spec()`: live 现场生成 / 后台
`PoolPrefetcher` / Director 冷启动 prewarm / **本脚本**。

但本脚本曾经是**唯一**的例外 —— 它直接 `choose_blueprint ->
writer.gen_spec()`, 也就是 classic 链。后果不是"风格没调好", 而是
**R4 的四轮调优在预热出来的题上完全没生效**:

    盘上的题 quality-v10 是真的(过了同一套 Reviewer + hard gate),
    但它们 prompt_version 全是 riddle-v9, 而不是 keyword2-v7。

于是直播播的是"通过 quality-v10 的任意题", 而不是我们验收过的红黑
keyword2 风格。**Reviewer 通过不等于题源正确。**

现在默认走 `keyword_spec()`; classic 链保留为 kill-switch
(`--no-keyword-seed`), 行为逐位不变。

⚠️ **bag 必须是整进程一只**(`_PrefillSeeder`)。每次 `_one()` 重建会把
draw_index / cooldown / 不放回状态全部重置 —— 表面上"用了 keyword2",
实际上每一轮都是新 session, 每道题都从 index=1 重来。

## 用法

    .venv/Scripts/python.exe prefill_pool.py --target 5
    .venv/Scripts/python.exe prefill_pool.py --target 5 --playable 2
    .venv/Scripts/python.exe prefill_pool.py --target 8 --max-attempts 40
    .venv/Scripts/python.exe prefill_pool.py --target 5 --no-keyword-seed

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
    # ---- R5: 与直播同一条 `--no-keyword-seed` kill-switch ----
    #
    # ⚠️ 参数名与 `story/config.py` 的**逐字相同**。这不是巧合: 它经
    # `_cfg_from_args` 覆盖到 `Config.pool_keyword_seed_enabled`, 于是
    # "预热走哪条链"与"直播走哪条链"读的是**同一个开关** —— 结构上
    # 不可能出现"一边升级、一边忘改"。
    ap.add_argument("--no-keyword-seed", dest="pool_keyword_seed_enabled",
                    action="store_false",
                    help="kill-switch: 预热回到 classic Blueprint 链"
                         "(`choose_blueprint -> gen_spec`, riddle-v9), "
                         "与直播的 --no-keyword-seed 是同一个开关。"
                         "默认走 keyword2(抽 2 词 + red/black lane -> "
                         "Story -> Surface -> Structure)")
    ap.add_argument("--keyword-corpus", dest="keyword_corpus_path", default="",
                    help="keyword2 的 seed 词库路径(默认 "
                         "data/keyword2_vocabulary.json)。不可用时"
                         "**显式降级**到 classic 链, 不会回退人工词库")
    ap.add_argument("--keyword-session-seed", dest="keyword_session_seed",
                    type=int, default=None,
                    help="keyword bag 的 session seed。默认由 quality_seed "
                         "派生; 都没有时随机一次并写进 INFO 日志")
    return ap


def main(argv=None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)
    # 先让 Config 的解析器处理一遍, 拿到池路径 / LLM 配置 / 质量策略 ——
    # 预热必须用与直播**完全一样**的配置, 否则补出来的题可能过不了
    # 直播那侧的池门。
    #
    # ⚠️ R5: 把本脚本自己的三个 keyword 开关**透传**给 Config 解析器。
    # 不透传的话 `--no-keyword-seed` 只改了 `a`, 而 `cfg` 仍是默认的
    # `pool_keyword_seed_enabled=True` —— 于是 kill-switch **看起来
    # 生效了**(参数解析没报错), 实际预热照旧走 keyword2。
    # 这是"参数解析成功但语义没接上"的典型形状, 所以下面有一处断言
    # 把它钉死(而不是靠读代码)。
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

    # ---- R5: kill-switch 真的接上了吗 ----
    #
    # 断言而不是注释: 上面那次透传一旦被删(或 dest 被改名), `cfg` 会
    # 悄悄回到"默认开 keyword2", 而 `a` 说是关的。两条链的题**都合法**
    # (都过同一条质量链), 所以这种漂移不会有任何下游症状 —— 只会让
    # 预热补出一批风格不对的库存, 正是本 issue 要修的东西。
    if bool(getattr(cfg, "pool_keyword_seed_enabled", True)) \
            != bool(a.pool_keyword_seed_enabled):
        log.error("--no-keyword-seed 没有透传到 Config(配置=%s, 参数=%s)"
                  " —— 拒绝在语义不明的状态下预热",
                  getattr(cfg, "pool_keyword_seed_enabled", None),
                  a.pool_keyword_seed_enabled)
        return 1

    seeder = _make_seeder(cfg)

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
        if _one(writer, pool, cfg, rng, a, seeder):
            done += 1
            log.info("入池成功(本次已补 %d 道)", done)
    stock = pool.stock_count()
    playable = pool.playable_count([], [])
    log.warning("尝试耗尽仍未达标: 库存 %d(可播 %d), 目标 %d/%d。"
                "本次补进 %d 道。",
                stock, playable, a.target, a.playable, done)
    return 1


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
    恒为空、不放回状态永远重置。表面上"走了 keyword2", 实际上:

        * `keyword_draw_index` 恒为 1 -> **lane 恒为同一个**
          (`draw_lane(session_seed, 1)` 是个常量), 于是整批预热题
          非红即黑, 红黑混出这条性质直接消失;
        * 同一个词会被连续抽到(bag 的短期重复控制是**跨 draw** 的).

    这两种症状都不会让质量门报警 —— 题照样过审, 只是分布错了。
    所以测试里有专门的变异(mutation): 把初始化挪回 `_one()`, 必须红。

    ## 为什么与直播的 session seed 口径一致

    `PoolPrefetcher._init_keyword_bag` 的规则逐条照搬:

        给了 keyword_session_seed  -> 直接用
        只给了 quality_seed        -> derive_session_seed() 派生
        两个都没给                  -> 随机一次, 并**立刻写进 INFO 日志**

    第三条是刻意的: 随机 seed 只在启动时取一次然后落日志, "这一批的
    pair 序列"事后永远可从日志抄回来重放。若每次抽词都 random, 复盘
    就没有锚点。
    """

    def __init__(self, enabled: bool, bag=None, session_seed=None,
                 bag_meta=None, error: str = ""):
        self.enabled = bool(enabled)
        self.bag = bag
        self.session_seed = session_seed
        self.bag_meta = dict(bag_meta or {})
        self.error = error

    # ---- 只读属性 ----
    @property
    def corpus_version(self) -> str:
        return str(self.bag_meta.get("corpus_version") or "")

    def meta_line(self) -> str:
        from story.keyword_seed import describe_bag
        return describe_bag(self.bag_meta, self.session_seed)


def _make_seeder(cfg: Config) -> _PrefillSeeder:
    """按配置装配 seeder。**corpus 坏了就显式降级, 绝不假装在跑 keyword2。**

    ## 降级为什么必须"响"

    `keyword_corpus.load_vocabulary` 对**任何**问题都抛 `CorpusError`
    (路径空 / 文件不存在 / 解析失败 / 空表)。这里把它 catch 成一条
    ERROR + `enabled=False`, 于是这次预热整条退回 classic 链 —— 与
    `PoolPrefetcher._init_keyword_bag` 的处理**逐条一致**。

    ⚠️ **不**回退人工 `KEYWORD_BANK`。那是 G3 就点名的形状: "看起来在
    跑 keyword2, 其实用人工词"。宁可 classic, 也不要假的 keyword2。

    ⚠️ 也不能静默: 降级后盘上会多出一批 riddle-v9 的题, 而**它们同样
    过 quality-v10**(版本门只看 quality policy, 不看 prompt_version)。
    本条 ERROR 日志是事后唯一能解释"为什么这批风格不对"的东西。
    """
    if not bool(getattr(cfg, "pool_keyword_seed_enabled", True)):
        log.info("预热走 classic Blueprint 链(--no-keyword-seed kill-switch): "
                 "choose_blueprint -> gen_spec, prompt_version=riddle-v9")
        return _PrefillSeeder(enabled=False)

    # 延迟 import: 这两个要读盘, 装配期出错要在这里被 catch 成"显式
    # 降级", 而不是冒泡成 import 错误。
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
                # 与 prefetch 同样用系统熵, **不碰** self._rng —— 那把
                # rng 服务 classic 链的 pick_blueprint, 从它取数会改变
                # 序列。抽完立刻落日志。
                ss = random.SystemRandom().getrandbits(64)
        bag, meta = load_bag(path, ss)
    except Exception as e:                       # noqa: BLE001
        log.error(
            "keyword2 corpus 不可用, **本次预热整条 keyword2 链让位给 "
            "classic Blueprint 链**(不会回退人工词库): %s: %s。"
            "⚠️ 这一轮补出来的题会是 riddle-v9, *不是* keyword2-v7。",
            type(e).__name__, e)
        return _PrefillSeeder(enabled=False,
                              error="%s: %s" % (type(e).__name__, e))

    s = _PrefillSeeder(enabled=True, bag=bag, session_seed=int(ss),
                       bag_meta=meta)
    log.info("keyword2 bag 就绪(**整进程共用这一只**): %s", s.meta_line())
    return s


def _one(writer, pool: PuzzlePool, cfg: Config, rng, a,
         seeder: _PrefillSeeder) -> bool:
    """试生成一道并入池。返回是否真的进了池子。

    ⚠️ 失败**不抛** —— 预热是个循环, 一道不成就试下一道, 让
    `max_attempts` 去兜底。抛出去只会让整个预热停在一道坏题上。

    ## R5: 两条链

        seeder.enabled  -> `keyword_spec`(抽 2 词 + lane -> Story ->
                           Surface -> Structure), 与直播/补池同一条
        否则             -> classic `choose_blueprint -> gen_spec`(逐位不变)

    ⚠️ classic 分支里 `choose_blueprint` 的调用形状**一个字都没改**:
    它现在是 kill-switch 路径, 而 kill-switch 的契约就是"完整回到旧链"。

    ⚠️ `seeder.bag` **不在这里建**。它由 `_make_seeder` 建一次然后跨
    所有 attempt 共用 —— 见 `_PrefillSeeder` 的说明。
    """
    # 每一道都**重新读**库存签名 —— 刚补进去的那道必须立刻进入下一道
    # 的 recent, 否则同一个 pair 会连着补好几道(G4-B)。
    # 两条链共用这一份 recent: keyword2 的 Structure 段与 classic 的
    # `choose_blueprint` 都吃它。
    recent = _recent_sigs(pool, cfg)

    if seeder.enabled:
        return _one_keyword(writer, pool, cfg, a, seeder, recent)
    return _one_classic(writer, pool, cfg, rng, a, recent)


def _one_keyword(writer, pool: PuzzlePool, cfg: Config, a,
                 seeder: _PrefillSeeder, recent: list) -> bool:
    """keyword2 链: 与 live / prefetch **同一个** `keyword_spec` 入口。

    ## 为什么是 import 而不是重写

    这个骨架的价值全在"只有一份"。它里面写着四项 R4 契约: 抽词 + lane
    的**顺序**、让路检查的**位置**(①Story 前 / ②Surface 前 / ③Structure
    前)、provenance 落进 `metrics` 的**字段集**、以及失败原因的标签。
    在预热里抄一份, 这四项迟早与直播漂开 —— 而漂开之后"预热补的题"
    与"直播现场生成的题"就不是同一种东西了, 正是本 issue 要消灭的形状。

    ## 离线让路

    预热**不在直播热路径上**, 它跑在 `engine.start()` 之前, 没有相位。
    `PoolPrefetcher.prewarm_should_continue` 是为**有预算的冷启动**写的:
    它带 deadline 与 stop 信号。这里连预算都不需要(离线工具, Ctrl-C
    直接杀), 所以传一个恒真的谓词。

    ⚠️ **不要**把直播的相位判据搬进来: 那会让预热在 IDLE 下立刻
    `interrupted`, 一道题都出不来(G4-R1 那个 P0 的翻版)。
    """
    from story.keyword_seed import keyword_spec

    def _always_continue() -> bool:
        return True

    spec, reason = keyword_spec(
        writer, seeder.bag, seeder.session_seed,
        avoid=[], recent=recent,
        should_continue=_always_continue,
        corpus_version=seeder.corpus_version)
    if spec is None:
        log.warning("keyword2 未成题(%s; 细因见 metrics.reject)", reason)
        return False
    if not pool.add(spec, source="prefill"):
        log.warning("入池被拒(继续下一道)")
        return False
    return True


def _one_classic(writer, pool: PuzzlePool, cfg: Config, rng, a,
                 recent: list) -> bool:
    """classic 链(逐位不变): `choose_blueprint -> gen_spec` -> riddle-v9。

    它是 `--no-keyword-seed` 的 kill-switch —— 不是"废弃路径", 所以
    不删、不简化。两条链在本文件里**显式并存**。
    """
    from story.quality import Quotas, choose_blueprint

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
