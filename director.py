#!/usr/bin/env python
# coding: utf-8
"""竖屏 AI 海龟汤直播 —— 入口。

    AI 出一个诡异谜题 -> 观众发 #问题 追问 -> AI 对每条提问**秒回**裁决
    (是 / 不是 / 无关 / 接近了) -> 有人猜中核心真相 -> 揭晓谜底
    -> 展示 30 秒 -> 自动出下一题

用法:
    uv run director.py --sim data/demo_script.jsonl --no-llm
    uv run director.py --sim data/demo_script.jsonl --reveal-hold 8
    uv run director.py --live 813110862078
    uv run director.py --stdin

线程模型:
    - 调度线程: 唯一驱动 engine.tick(), 是 phase 的唯一写入者
    - ANSWER 池(5 线程): 逐条秒回 -> engine.submit_qa()
    - LLM worker: RIDDLE/HINT/REVEAL -> engine.submit_riddle/hint/reveal()
    - 抓取线程(ws 读): 只 queue.put_nowait
    - 消费线程: 从 queue 取出 -> engine.submit_danmaku()
    - 渲染线程: http.server, 4Hz 推快照
"""

from __future__ import annotations

import logging
import json
import os
import queue
import random
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from story.config import Config, from_args          # noqa: E402
from story.engine import RoundEngine                # noqa: E402
from story.ingest import (ChatEvent, InteractionEvent,  # noqa: E402
                          LiveSource,
                          SimSource, StdinSource)
from story import parser as P                       # noqa: E402
from story.llm import (AnthropicMessagesClient, PuzzleWriter,  # noqa: E402
                       _spec_to_riddle)
from story.pool import PuzzlePool                   # noqa: E402
from story.live_heartbeat import (                   # noqa: E402
    DEFAULT_INTERVAL_SECONDS as LIVE_HEARTBEAT_INTERVAL,
    DEFAULT_PATH as LIVE_HEARTBEAT_PATH,
    clear_live_heartbeat, write_live_heartbeat)
from story.played import PlayedLedger               # noqa: E402
from story.leaderboard import LeaderboardLedger     # noqa: E402
from story.public_player import PublicPlayerCore    # noqa: E402
from story.server import RenderServer, StateHub     # noqa: E402
from story.state import ActionKind, Phase, QAResult  # noqa: E402

log = logging.getLogger("story.director")

# 比 DEBUG(10) 低一级: 只进文件, 不进控制台。
# 用来记"每条弹幕的去向""每次 LLM 调用的输入输出"这类**量大但排查时救命**
# 的东西。控制台看主干, 文件看明细。
DETAIL = 5
logging.addLevelName(DETAIL, "DETAIL")


def _detail(logger: logging.Logger, msg: str, *args) -> None:
    """写一条 DETAIL 日志(只在文件里出现)。"""
    if logger.isEnabledFor(DETAIL):
        logger.log(DETAIL, msg, *args)


# ======================================================================
class _SafeFormatter(logging.Formatter):
    """格式化时把"坏字符"洗掉, 绝不让日志本身抛异常。

    实测: 弹幕里混进非法字节(代理/网关截断、编码错乱)会生成
    **孤立代理对**(如 '\\udcb2'), logging 写流时抛 UnicodeEncodeError,
    然后 logging 模块往 stderr 吐一大段 traceback —— 直播时看着像崩了,
    其实只是某条弹幕的脏字节。日志绝不该因为**被记录的内容**而失败。
    """

    def format(self, record: logging.LogRecord) -> str:
        try:
            return super().format(record)
        except Exception:
            try:
                record.msg = str(record.msg).encode(
                    "utf-8", "replace").decode("utf-8", "replace")
                record.args = None
                return super().format(record)
            except Exception:
                return f"{record.levelname} {record.name}: <日志格式化失败>"


class _SafeStream(logging.StreamHandler):
    """写流失败也不炸(Windows 控制台编码 / 已关闭的管道)。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            super().emit(record)
        except Exception:
            self.handleError = lambda *_: None      # 静音, 不再吐 traceback


#: `setup_logging` 实际用的控制台流。banner 必须写它而不是 `sys.stdout`
#: —— 两者是同一个 fd 的两份 Python 对象, 而 `sys.stdout` 那份是带缓冲
#: 的(见 `run()` 里 `_banner` 的说明)。`None` = 还没 setup 过, 退回
#: `sys.stdout`。
_console = None


def setup_logging(level: str, log_file: str | None = None) -> None:
    """日志装配: 控制台 + (可选)文件。

    控制台只留**主干**信息, 明细全部写文件 —— 直播时控制台滚得太快,
    真要排查还是得翻文件。
    """
    global _console
    root = logging.getLogger()
    root.handlers.clear()

    # 控制台: 按传入 level(默认 INFO)
    # Windows 控制台默认 GBK, 中文日志会变乱码 —— 显式换成 UTF-8 写。
    console = sys.stdout
    try:
        console = open(sys.stdout.fileno(), "w", encoding="utf-8",
                       errors="replace", closefd=False)
    except Exception:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    # banner 与日志共用这一份 —— 顺序才是确定的。
    _console = console
    ch = _SafeStream(console)
    ch.setFormatter(_SafeFormatter(
        "%(asctime)s %(levelname)-5s %(name)s: %(message)s", datefmt="%H:%M:%S"))
    ch.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.addHandler(ch)

    # 文件: **永远记 DETAIL 及以上**, 比控制台详细
    if log_file:
        os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8", errors="replace")
        fh.setFormatter(_SafeFormatter(
            "%(asctime)s %(levelname)-5s %(name)s: %(message)s"))
        fh.setLevel(DETAIL)
        root.addHandler(fh)

    # 文件要收 DETAIL, 所以 root 必须放到 DETAIL
    root.setLevel(min(DETAIL, ch.level))
    # 别让第三方库刷屏
    for noisy in ("urllib3", "websocket"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    log.info("日志文件: %s", log_file or "(未启用, 用 --log-file 开启)")


class Director:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.engine = RoundEngine(cfg)

        # ---- 跨直播累计猜汤榜 ----
        # Engine 保持纯状态机：Director 负责磁盘 I/O。启动时先重放账本，
        # 再把聚合结果一次性注入 Engine；之后每个真人 solved REVEAL 再
        # append 一条胜场事件。默认 enabled 只在生产装配打开，避免离线
        # 单测直接构造 Config 时碰真实 data/。
        self.leaderboard_ledger = LeaderboardLedger(
            path=getattr(cfg, "leaderboard_path", "") or "",
            # 只给真实直播累计。sim/stdin 是调试入口，不能把测试胜场混进
            # 真实观众总榜。
            enabled=bool(getattr(cfg, "live_id", None)))
        self.leaderboard_ledger.load()
        try:
            self.engine.restore_leaderboard(self.leaderboard_ledger.rows())
        except Exception:
            # Ledger 自己已经把坏行过滤掉；这里若仍失败属于代码/结构 bug。
            # 排行榜不能阻止开播，所以记录后退回空榜。
            log.exception("累计猜汤榜恢复到 Engine 失败，本次从空榜继续")
            self.engine.restore_leaderboard([])

        # ---- P0: 全局已播账本(任意来源, 跨重启) ----
        #
        # 挂在 engine 上而不是各处调用方上: `submit_riddle` 是**所有**
        # 交付路径(pool / live_generate / fallback / curated)的唯一收口,
        # 一处接线就覆盖四条链。见 `PlayedLedger` 的模块说明。
        self.played_ledger = PlayedLedger(
            path=getattr(cfg, "played_path", "") or "",
            # ⚠️ **只有生产装配显式打开它**。`PlayedLedger` 自己默认是
            # 关的 —— 几百个既有用例直接 `Config(sim_path="x")` 起引擎
            # (没有 tmpdir), 默认开会把它们的交付互相挡掉。见那个类的
            # `__init__` 说明。
            enabled=True)
        self.played_ledger.load()
        self.engine.played_ledger = self.played_ledger
        self.hub = StateHub()
        self.inbox: "queue.Queue[ChatEvent]" = queue.Queue(maxsize=20000)
        self.source = None
        self.server: RenderServer | None = None
        self.writer: PuzzleWriter | None = None
        self.client: AnthropicMessagesClient | None = None
        self.ai_player: PublicPlayerCore | None = None
        self._stop = threading.Event()
        # ⚠️ 实际上**只有 `_riddle` 用它**(非阻塞 acquire, 失败则
        # `_deferred_rddle=True` 交给下一拍补发)。`_hint` / `_reveal`
        # **不碰**它 —— 早先这里的注释写着 "RIDDLE/HINT/REVEAL 互斥",
        # 那是不成立的, 别照着它"修正"成三者共锁。
        #
        # 为什么这条注释很重要: Q9 的补池**刻意不共用这把锁**(补池持锁
        # 会把 live 出题挤成"推迟到下一拍", 优先级正好倒过来)——
        # 整个 Q9 的锁设计就建立在这个事实上。把三者真改成共锁会把
        # 那个优先级反转重新引入。
        self._narrating = threading.Lock()
        self.segment_counter = 0
        self.session_id = uuid.uuid4().hex
        # ---- stable-refill: live 进程 lease ----
        # 独立补池守护进程只在这个 lease 过期时工作。这里在真正打开
        # pool / LLM 之前先写一次，避免 Director 启动期间与守护进程
        # 同时写 generated pool。崩溃后文件会自然变 stale。
        self._live_heartbeat_path = (
            LIVE_HEARTBEAT_PATH if bool(getattr(cfg, "live_id", None)) else "")
        self._live_heartbeat_thread: threading.Thread | None = None
        if self._live_heartbeat_path:
            write_live_heartbeat(
                self._live_heartbeat_path,
                phase=str(getattr(self.engine.phase, "value",
                                  self.engine.phase) or ""),
                session_id=self.session_id)
        self._archive_failed = False
        # blueprint 调度用的随机源。**不要用全局 random** —— 出题在 worker
        # 线程里跑, 用模块级 random 会和其他代码互相干扰, 复盘也无法重现。
        # quality_seed 给了就固定(可复现), 否则用系统随机种子。
        seed = getattr(cfg, "quality_seed", None)
        self._rng = random.Random(seed)
        if seed is not None:
            log.info("blueprint 调度使用固定 seed=%s(可复现)", seed)
        # 出题 worker 因锁被占而推迟时的标志 —— 下一拍 tick 补发。
        # 出题是直播的命脉, 不能因为"当时正忙"就静默丢掉。
        self._deferred_riddle = False
        # 逐条秒回: 独立的 ANSWER 并发池(与 qa_max_inflight 对齐)
        self._answer_pool: ThreadPoolExecutor | None = None
        if not cfg.no_llm:
            self._answer_pool = ThreadPoolExecutor(
                max_workers=max(1, cfg.qa_max_inflight), thread_name_prefix="answer")

        self.engine.model_requested = cfg.llm.model
        self.engine.source = cfg.source_label

        # ---- Q8: 题池 ----
        # `pool_enabled=False` -> open() 返回 None, 下面整条路径都不碰池子。
        # 用注入的同一把 rng, 保证 quality_seed 固定时出题可复现。
        self.pool = PuzzlePool.open(cfg, rng=self._rng)
        # ---- Batch H2-F/G: curated(外部题库)池 ----
        # **独立实例 + 独立账本**(见 `PuzzlePool.open_curated` 的说明)。
        # 只在 `prefer_curated` 打开时才建 —— 关掉时连文件都不读。
        self.curated_pool = (PuzzlePool.open_curated(cfg, rng=self._rng)
                             if getattr(cfg, "prefer_curated", True)
                             else None)
        if self.curated_pool is None and getattr(cfg, "prefer_curated", True):
            log.info("curated 池不可用(文件缺失或已关闭), 本题只用 AI 池")

        if not cfg.no_llm:
            self.client = AnthropicMessagesClient(cfg.llm)
            # **必须传 runtime_cfg**: temperature 与 quota 定义在 Config 上,
            # 而 client.cfg 是 LLMConfig。漏掉它 -> 这些参数全部静默失效
            # (取到 None, 网关用默认值), 而测试因为 Fake 上有这些字段仍全绿。
            self.writer = PuzzleWriter(client=self.client, runtime_cfg=cfg)
            self.ai_player = PublicPlayerCore(self.client)

        # ---- Q9: 后台补池 ----
        # 只在 QA 阶段、零压力、且库存低于低水位时才在后台预生成。
        # 放在 writer 之后建 —— 它要拿 writer 的引用。(writer 为 None 时
        # 它自我禁用, 所以 `--no-llm` 下不会去灌假题。)
        #
        # 独立的 rng: 与 live 出题分开, 这样"补池开不开"不影响 live 的
        # blueprint 序列(否则同 seed 也复现不出来, 复盘时说不清)。
        self._prefetcher = None
        if self.pool is not None:
            from story.prefetch import PoolPrefetcher
            pf_seed = getattr(cfg, "quality_seed", None)
            pf_rng = random.Random(None if pf_seed is None
                                   else pf_seed ^ 0x9E3779B9)
            # ⚠️ 补池用**另一个 Writer 实例**。
            #
            # `PuzzleWriter` 不是无状态的: `_review_spec` 会把本次审稿的
            # decision/issues 写进实例属性(`_last_review_*`), `gen_spec`
            # 再读出来记进 metrics。而 Q9 刻意让 prefetch 不拿 `_narrating`,
            # 所以 prefetch 与 live 会**同时**跑 gen_spec。共用一个实例
            # 就会出现: prefetch 审稿 A -> 写 decision=A -> 切走 ->
            # live 审稿 B -> 覆盖成 B -> prefetch 继续 -> 把 B 的
            # decision/issues 记进 **A** 的 metrics。
            #
            # 谜题内容不会串, 但 Q7 好不容易建立的 generation/review
            # provenance 会被污染(题以后播出时 archive 带着错误审稿指标)。
            # 每边一个实例就切断了这条侧信道。
            #
            # `client` 仍然共用 —— 它是纯传输层, 无可变业务状态。
            pf_writer = (PuzzleWriter(client=self.client, runtime_cfg=cfg)
                         if self.client is not None else None)

            # ---- Q10: AI 试玩(默认关闭) ----
            # 装配权在这里, 不在 PoolPrefetcher 里 —— 因为 Playtester 需要
            # (a) host_writer = 上面那个 pf_writer, (b) should_continue =
            # 引擎的压力探针, 两者都是本层的协作者。
            #
            # `should_continue` 复用 `_low_pressure_for_playtest`: 试玩比
            # 单次 gen_spec 长得多(2N 次 LLM 调用), 所以它每步之前都要
            # 重查一次"房间还空着吗"。一旦不空, 立刻以 interrupted 让路。
            playtester = None
            if (getattr(cfg, "playtest_enabled", False)
                    and pf_writer is not None):
                from story.playtest import Playtester
                playtester = Playtester(
                    player_client=self.client,
                    host_writer=pf_writer,
                    should_continue=self._playtest_should_continue,
                    max_turns=getattr(cfg, "playtest_max_turns", 10))

            self._prefetcher = PoolPrefetcher(
                cfg=cfg, pool=self.pool, writer=pf_writer,
                probe=self.engine.pressure,
                probe_inputs=self.engine.snapshot_generation_inputs,
                pick_blueprint=self._pick_blueprint,
                rng=pf_rng, playtester=playtester)

        # ---- Batch H3-B: Lazy Curator(按需审外部题) ----
        #
        # 与上面的 `_prefetcher` 是**两条独立的链**:
        #   prefetcher   走"发明"链(choose_blueprint -> gen_spec), 产 AI 题
        #   lazy_curator 走"搬运"链(curated 编译), 产 curated 题
        # 两者的第一步完全不同, 所以各自一个实例、各自一个 writer。
        #
        # 它**不拥有线程** —— 由主循环在自己的节奏里调 `step()`。这样
        # "什么时候可以干活"的判断权留在这一层(它能同时看到 engine 和
        # 两个池), 而 LazyCurator 只管"取一条、审一条、记账"。
        #
        # ⚠️ 只在 curated 池可用时才建: 没有池子就没有落点, 审出来也
        # 无处可放(而且会白烧 LLM)。
        self._lazy_curator = None
        if (self.curated_pool is not None and self.client is not None
                and bool(getattr(cfg, "curated_background_enabled", True))):
            from story.lazy_curator import build_lazy_curator
            lc_writer = PuzzleWriter(client=self.client, runtime_cfg=cfg)
            self._lazy_curator = build_lazy_curator(
                cfg, self.curated_pool, lc_writer, self.engine.pressure,
                generation_probe=self.engine.snapshot_generation_inputs)

        # ---- G4-2 §四: live 现场生成用的 keyword bag(懒建) ----
        # 见 `_keyword_bag` 的说明: 这里**只置空占位**, 真正建在第一次
        # 需要现场生成时。三个字段是本方法的缓存槽。
        self._kw_bag = None
        self._kw_bag_meta: dict = {}
        self._kw_session_seed = None
        self._kw_bag_built = False

    # ------------------------------------------------------------------
    def _keyword_bag(self):
        """G4-2 §四: 现场生成用的 keyword bag。返回 `(bag, meta, seed)`。

        `bag is None` => 走 classic Blueprint 链。三种情况:

            ① `--no-keyword-seed`(kill-switch) —— **prefetch 与 live
               同时**回 classic。两边读同一个 config flag, 所以不会
               出现半切换。
            ② corpus 不可用(显式降级, 与 prefetcher 同一套语义)
            ③ 还没建过(懒建, 见下)

        ## 为什么**懒建**而不是在 `__init__` 里建

        `__init__` 里建会让**每一次**启动都读一遍词库 —— 包括 `--no-llm`
        的冒烟测试、`--sim` 的回放、以及只跑引擎不起题的场景。那些都不
        需要 keyword 词库, 白读一遍盘还会让启动日志多一行容易误读的
        "keyword2 bag 就绪"。

        懒建只在**真的要现场生成**时发生(池空 + 允许现场生成), 而那正是
        需要它的唯一时刻。建失败时降级成 classic —— 与 prefetcher 的
        `_init_keyword_bag` 逐字同语义(打 ERROR, 不抛)。

        ## 与 prefetcher 的 bag 是**两个实例**

        各自 `random.Random(同一个 session_seed)` —— 于是两边从同一个
        种子出发, 但**不共享消费位置**。这是刻意的: live 现场生成是
        稀有路径(池空才走), 让它去推进 prefetch 的 bag 会让"同 seed 下
        补池序列"依赖"这一场现场生成过几次" —— 复盘时说不清。
        两边各自从头抽, 各自可复现。

        ⚠️ 它**不用** `self._rng`: 那把 rng 服务 live 的 blueprint 序列,
        从它取数会改变既有可复现性(与 prefetcher 里那条注释同源)。
        """
        if not bool(getattr(self.cfg, "pool_keyword_seed_enabled", True)):
            return None, {}, None
        if self._kw_bag_built:
            return self._kw_bag, self._kw_bag_meta, self._kw_session_seed
        self._kw_bag_built = True
        try:
            from story.keyword_corpus import DEFAULT_CORPUS_PATH
            from story.keyword_seed import derive_session_seed, load_bag
            path = str(getattr(self.cfg, "keyword_corpus_path", "")
                       or DEFAULT_CORPUS_PATH)
            ss = getattr(self.cfg, "keyword_session_seed", None)
            if ss is None:
                qseed = getattr(self.cfg, "quality_seed", None)
                if qseed is not None:
                    ss = derive_session_seed(qseed)
                else:
                    ss = random.SystemRandom().getrandbits(64)
            self._kw_bag, self._kw_bag_meta = load_bag(path, ss)
            self._kw_session_seed = int(ss)
            log.info("keyword2(live)bag 就绪: %s",
                     __import__("story.keyword_seed", fromlist=["x"]
                                ).describe_bag(self._kw_bag_meta, ss))
        except Exception as e:                       # noqa: BLE001
            self._kw_bag = None
            self._kw_bag_meta = {}
            self._kw_session_seed = None
            log.error("keyword2 corpus 不可用(live 现场生成), "
                      "整条 keyword2 链让位给 classic Blueprint 链"
                      "(不会回退人工词库): %s: %s", type(e).__name__, e)
        return self._kw_bag, self._kw_bag_meta, self._kw_session_seed

    # ------------------------------------------------------------------
    def _playtest_should_continue(self) -> bool:
        """试玩让路谓词: 房间还空着才继续。

        用 `engine.pressure()` 而不是另起一个探针 —— 补池启动试玩时用的
        就是它, 复用同一个判定边界, 免得"能开始试玩"和"能继续试玩"两套
        标准漂移。试玩**开跑**的条件已经由 prefetcher 的 `_low_pressure()`
        保证, 这里只负责"跑着跑着房间忙了就让路"。
        """
        try:
            p = self.engine.pressure()
        except Exception:                       # noqa: BLE001
            log.exception("试玩压力探针异常, 当作中断")
            return False
        if not p or p.get("stopped"):
            return False
        from story.state import Phase
        if p.get("phase") != Phase.QA:
            return False
        if p.get("pending") or p.get("inflight"):
            return False
        if p.get("hint_inflight") or p.get("reveal_inflight"):
            return False
        return True

    # ------------------------------------------------------------------
    def _build_source(self):
        if self.cfg.live_id:
            return LiveSource(self.cfg, self.inbox,
                              on_stream_end=self._on_stream_end,
                              on_reconnect=self._on_reconnect,
                              on_reconnected=self._on_reconnected)
        if self.cfg.sim_path:
            return SimSource(self.cfg, self.inbox,
                             on_stream_end=self._on_stream_end)
        return StdinSource(self.cfg, self.inbox)

    def _on_stream_end(self) -> None:
        self.engine.on_stream_ended()

    def _on_reconnect(self, fails: int = 1) -> None:
        """弹幕连接在重建。连续失败多次 -> 让引擎记下来, 页面能显示异常。"""
        self.engine.on_disconnect()
        self.engine.reconnect_fails = fails

    def _on_reconnected(self) -> None:
        """新连接收到首帧 = 重连成功(Q12)。

        引擎据此开一个 replay guard: 抖音重连后会把断线期间的弹幕原样
        重发一遍, 而这件事只有在"刚重连"的窗口里才需要防。
        """
        self.engine.on_reconnect()

    # ------------------------------------------------------------------
    @staticmethod
    def _find_chrome() -> str | None:
        """找本机 Chrome。找不到返回 None。"""
        cands = [
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
        for p in cands:
            if p and os.path.isfile(p):
                return p
        # 退而求其次: Edge (同样是 Chromium, 支持 --app)
        for p in (os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
                  os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe")):
            if p and os.path.isfile(p):
                return p
        return None

    def _open_live_window(self) -> None:
        """开一个独立的直播输出窗口(无浏览器杂物), 供直播伴侣窗口捕获。

        用 --app 模式 + 独立 user-data-dir, 不干扰用户已开的 Chrome。
        窗口按 1080x1920 请求; 若屏幕放不下, 系统会压到屏幕高度 —— 这是
        系统限制, 页面本身仍按 1080 宽渲染。
        """
        exe = self._find_chrome()
        if not exe:
            log.warning("未找到 Chrome/Edge, 跳过自动开窗。"
                        "请手动打开 http://%s:%d/", self.cfg.host, self.cfg.port)
            return
        profile = os.path.abspath(os.path.join("data", "_liveprofile"))
        os.makedirs(profile, exist_ok=True)
        url = f"http://{self.cfg.host}:{self.cfg.port}/"
        args = [
            exe, f"--app={url}",
            "--start-maximized",          # 最大化: 竖屏内容居中, 左右黑边
            f"--user-data-dir={profile}",
            "--no-first-run", "--no-default-browser-check",
            "--hide-scrollbars",
            "--disable-features=Translate,CalculateNativeWinOcclusion",
        ]
        try:
            self._window_proc = subprocess.Popen(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log.info("已打开直播输出窗口 (Chrome --app, 最大化, 竖屏居中)")
        except Exception as e:
            log.warning("打开直播输出窗口失败: %s", e)

    def _close_live_window(self) -> None:
        p = getattr(self, "_window_proc", None)
        if p is not None:
            try:
                p.terminate()
            except Exception:
                pass
            self._window_proc = None

    # ------------------------------------------------------------------
    def _dispatch(self, actions) -> None:
        """执行引擎返回的动作。

        关键: submit_* 的**返回值也是动作**(例如 submit_qa 可能返回 REVEAL),
        必须回灌到 _run_action, 否则揭晓永远不会发生。
        """
        for a in (actions or []):
            self._run_action(a)

    def _run_action(self, action) -> None:
        k = action.kind
        if k == ActionKind.ANSWER:
            self._answer(action.payload)        # 独立并发池
        elif k == ActionKind.RIDDLE:
            self._riddle(action.payload)        # 单 worker
        elif k == ActionKind.HINT:
            self._hint(action.payload)
        elif k == ActionKind.REVEAL:
            # 真人猜中在 Engine 内已经只计一次；这里把同一事实持久化。
            # event_id = session + round 做第二层幂等，防异常重派同一 action
            # 时跨重启分数翻倍。
            if action.payload.get("reason") == "solved":
                uid = str(action.payload.get("winner_user_id", "") or "").strip()
                name = str(action.payload.get("winner", "") or "").strip()
                round_id = action.payload.get("expect_round")
                event_id = f"{self.session_id}:{round_id}"
                if not self.leaderboard_ledger.record_win(
                        uid, name, event_id=event_id):
                    # 排行榜是增强功能，写失败不能阻断揭晓/直播主线。
                    log.error("累计猜汤榜写入失败，本场内存分数仍保留: "
                              "user=%s round=%s", name, round_id)
            self._reveal(action.payload)
        elif k == ActionKind.AI_PLAYER:
            self._ai_player(action.payload)
        elif k == ActionKind.BROADCAST:
            self.push()

    def _ai_player(self, payload: dict) -> None:
        """执行 AI 玩家的一步；Engine 在每个阶段之间重新核对现场。"""
        def work():
            token = payload.get("token", "")
            round_index = payload.get("expect_round")
            spec_key = payload.get("expect_spec_key", "")
            stage = payload.get("stage")
            try:
                if stage == "move":
                    move = (self.ai_player.ask(
                        payload.get("puzzle", ""),
                        payload.get("transcript", []))
                        if self.ai_player is not None else None)
                    if move is None:
                        acts = self.engine.submit_ai_player_move(
                            token, round_index, spec_key,
                            error="PublicPlayerCore 调用失败")
                    else:
                        acts = self.engine.submit_ai_player_move(
                            token, round_index, spec_key,
                            kind=move[0], text=move[1])
                elif stage == "ask":
                    if not self.engine.ai_player_second_call_allowed(
                            token, round_index, spec_key):
                        self.push()
                        return
                    if self.writer is None:
                        results = [QAResult(
                            qid=0,
                            verdict=self._fake_verdict(payload.get("text", "")))]
                        err = None
                    else:
                        results, err = self.writer.answer(
                            payload.get("puzzle", ""),
                            payload.get("answer", ""),
                            payload.get("transcript", []), 0, "AI玩家",
                            payload.get("text", ""), judge_solve=False,
                            solve_atoms=payload.get("solve_atoms"),
                            facts=payload.get("facts"),
                            completion_fact_ids=[],
                            timeout=payload.get("timeout"),
                            max_retries=payload.get("max_retries"))
                    r = results[0] if results else None
                    acts = self.engine.submit_ai_player_result(
                        token, round_index, spec_key, "ask",
                        payload.get("text", ""),
                        verdict=(r.verdict if r else ""),
                        comment=(r.comment if r else ""),
                        failed=(r is None or r.status != "ok"),
                        error=(err if r is None else None))
                elif stage == "solve":
                    if not self.engine.ai_player_second_call_allowed(
                            token, round_index, spec_key):
                        self.push()
                        return
                    if self.writer is None:
                        jr = None
                    else:
                        jr = self.writer.judge(
                            payload.get("puzzle", ""),
                            payload.get("answer", ""),
                            payload.get("text", ""),
                            payload.get("solve_atoms"),
                            facts=payload.get("facts"),
                            timeout=payload.get("timeout"),
                            max_retries=payload.get("max_retries"))
                    acts = self.engine.submit_ai_player_result(
                        token, round_index, spec_key, "solve",
                        payload.get("text", ""),
                        solved=bool(jr and jr.solved),
                        failed=bool(jr is None or jr.failed),
                        error=(getattr(jr, "error", None) if jr else
                               "Final Judge 调用失败"))
                else:
                    acts = self.engine.submit_ai_player_move(
                        token, round_index, spec_key, error="未知 AI 玩家阶段")
                self._dispatch(acts)
            except Exception as e:                 # noqa: BLE001
                log.exception("AI玩家调用异常: %s", e)
                if stage == "move":
                    acts = self.engine.submit_ai_player_move(
                        token, round_index, spec_key, error=str(e))
                else:
                    acts = self.engine.submit_ai_player_result(
                        token, round_index, spec_key,
                        "ask" if stage == "ask" else "solve",
                        payload.get("text", ""), failed=True, error=str(e))
                self._dispatch(acts)
            self.push()

        threading.Thread(target=work, daemon=True,
                         name="ai-player").start()

    # ---- ANSWER: 逐条秒回, 走并发池 ----
    def _answer(self, payload: dict) -> None:
        """在 ANSWER 池里调 LLM —— 绝不在调度线程做。

        注意: **不做** in-flight 去重锁。并发 5 是刻意的 —— 由引擎的
        qa_max_inflight 控制总量, 池大小与之一致。
        """
        pool = self._answer_pool
        if pool is None:
            # --no-llm: 用固定裁决, 但仍要回填, 否则提问会一直挂着
            verdict = self._fake_verdict(payload.get("text", ""))
            self._dispatch(self.engine.submit_qa(
                [QAResult(qid=payload["qid"], verdict=verdict)],
                expect_round=payload.get("expect_round"),
                expect_spec_key=payload.get("expect_spec_key")))
            self.push()
            return
        pool.submit(self._answer_work, payload)

    def _answer_work(self, payload: dict) -> None:
        qid = payload["qid"]
        try:
            t0 = time.time()
            results, err = self.writer.answer(
                payload["puzzle"], payload.get("answer", ""),
                payload.get("transcript", []), qid,
                payload.get("user_name", ""), payload.get("text", ""),
                solve_atoms=payload.get("solve_atoms"),
                facts=payload.get("facts"),
                # v5 通关合同 —— 由 Engine 从当前这道题带下来。
                # **不让 director 自己推断**: "有没有合同"是 Engine 的
                # 状态(它持有此刻哪道题在台上), director 只搬运。
                # 合同非空 -> `answer()` 不调 Final Judge, 通关由 Engine
                # 对 established facts 做集合覆盖判定。
                completion_fact_ids=payload.get("completion_fact_ids"),
                # v6 completion 复核需要的两个快照 —— 同样由 Engine
                # (dispatch 那一刻)给出, director 只搬运, 不推断。
                #   core_answer: 复核判断"最小语义拆分"的基准
                #   established: 复核只看"当前这句话"是否补上了**还没**
                #                建立的那几条, 所以必须知道房间已有什么
                core_answer=payload.get("core_answer", ""),
                room_established_fact_ids=payload.get("established_fact_ids"),
                # QA 自己的短预算(见 config.qa_answer_timeout):
                # 全局 AI_TIMEOUT=60/重试 3 次是给低频长任务定的, 直播问答
                # 用那个会让观众等 4 分钟。
                timeout=payload.get("timeout"),
                max_retries=payload.get("max_retries"))
            log.info("答 %r -> %.1fs %s", payload.get("text", "")[:16],
                     time.time() - t0,
                     (results[0].verdict if results else f"失败: {err}"))
            if not results:
                # 解析不出 -> 给"未判定", **不是**"无关"。
                # "无关"是断言"你的猜测与谜底无关", 那是错误信息, 会把观众
                # 的思路带偏; "未判定"只说明系统这次没答上, 诚实且不误导。
                results = [QAResult(qid=qid, verdict=P.UNAVAILABLE,
                                    comment="刚才网络抖了一下，再发一次吧",
                                    status="unavailable")]
            self._dispatch(self.engine.submit_qa(
                results, error=err,
                model=getattr(self.writer.client.cfg, "model", None),
                expect_round=payload.get("expect_round"),
                expect_spec_key=payload.get("expect_spec_key")))
        except Exception as e:
            log.exception("回答异常: %s", e)
            self._dispatch(self.engine.submit_qa(
                [QAResult(qid=qid, verdict=P.UNAVAILABLE,
                          comment="刚才网络抖了一下，再发一次吧",
                          status="unavailable")], error=str(e),
                expect_round=payload.get("expect_round"),
                expect_spec_key=payload.get("expect_spec_key")))
        self.push()

    # ---- RIDDLE / HINT / REVEAL: 单 worker(低频) ----
    def _riddle(self, payload: dict) -> None:
        """出题 worker。

        **两条硬纪律**(方案 review Blocker 3):

        ① worker 只"提交结果", 绝不"启动下一次出题"。
           `submit_riddle(None)` 会立刻返回一个新的 RIDDLE 动作, 而
           `_dispatch` 会同步把它跑起来 —— 那时本 worker **还没走到
           finally 释放 `_narrating`**, 于是新 worker 的
           `acquire(blocking=False)` 失败, 直接把 retry 丢掉:
           表现为"出题失败后卡住不动"。
           retry 一律交给 tick 的下一拍。

        ② 失败路径不要继续访问 `r.puzzle` —— `r` 可能为 None,
           那会抛 AttributeError 再触发一次 submit_riddle, 越滚越乱。
        """
        def work():
            if not self._narrating.acquire(blocking=False):
                # 已有在途任务。**不能直接丢弃** —— 出题是直播的命脉,
                # 丢了就开天窗。交给 tick 下一拍重发。
                log.info("已有在途 LLM 任务, 本题出题推迟到下一拍")
                self._deferred_riddle = True
                return
            failure: Optional[str] = None
            try:
                recent = payload.get("recent_signatures") or []
                # ---- Q8: 题池优先 ----
                # 池子里有**此刻**可用的题就直接用, 省掉一次 LLM + 审稿
                # (10–40s)。挑不到再走原来的路径。
                #
                # 注意这一层在 `no_llm` **之前** —— 池子里的题已经生成好
                # 了, 上屏不需要任何 LLM 调用, 所以 `--no-llm` 时也该用它。
                # (早先 `--no-llm` 直接走假题, 池子等于白建。)
                #
                # `pop_next` 契约上不抛、坏了也返回 None, 所以这里不需要
                # 额外的 try —— 它的失败模式就是"回落"。
                # Step 06: 这一发是为第几题要的。原样带给引擎做身份校验。
                expect_round = payload.get("expect_round")
                spec = None
                source = "live_generate"
                # ---- G4-2 §三: 取题顺序 generated -> curated -> 现场生成 ----
                #
                # ⚠️ **curated 不再抢最高优先级**。H2-G 原本把它排在最前
                # ("外部好题的认知反转密度比 AI 现场造的高"), G4-2 推翻了
                # 这条产品决定:
                #
                #     默认直播的唯一主生成体系是 keyword2。
                #     curated 是补充题源, 不是默认主产品。
                #
                # 两个后果:
                #   1. `prefer_curated` 默认 False -> 这一段整体不跑
                #      (curated_pool 是 None), 观众只感受到一套生成风格;
                #   2. 即使显式 `--curated`, generated 池**仍然排前** ——
                #      否则一开 curated 就立刻被外部题淹没, 默认口径
                #      与 opt-in 口径的差别会大到不像同一个产品。
                #
                # `pop_next` 在两处都一样: 准入重校验 + 两遍选择 +
                # 先落盘再交付。所以优先级只影响**顺序**, 不影响**标准**。
                if self.pool is not None:
                    spec = self.pool.pop_next(
                        recent_signatures=recent,
                        avoid=payload.get("avoid"))
                    if spec is not None:
                        source = "keyword2_pool"
                if spec is None and self.curated_pool is not None:
                    spec = self.curated_pool.pop_next(
                        recent_signatures=recent,
                        avoid=payload.get("avoid"))
                    if spec is not None:
                        source = "curated"

                if spec is not None:
                    # 池子里来的: 结构已经齐了, 直接上屏。
                    self._submit_spec(spec, source, expect_round=expect_round)
                elif self.cfg.no_llm or not self.writer:
                    res_p, res_a = self._fake_riddle()
                    self._dispatch(self.engine.submit_riddle(
                        res_p, res_a, list(P.FALLBACK_HINTS),
                        title="海龟汤", model="no-llm", source=source,
                        expect_round=expect_round))
                elif not getattr(self.cfg, "allow_live_generation", True):
                    # ---- H2-G: 现场 AI 生成关闭 ----
                    #
                    # 两个池都空时**不**回到 AI 造题, 而是走引擎自己的
                    # 结构化兜底。理由: 这批要测"题源换掉后风格是不是
                    # 立刻变好", 若池一空就混进 AI 造的题, 测出来的是
                    # 混合风格, 分不清改善来自哪一边。
                    #
                    # ⚠️ 这**不是**开天窗: `submit_riddle(None, error=...)`
                    # 会让引擎走 `_riddle_failed_locked` 的兜底谜题, 直播
                    # 不中断 —— 只是那道题不是 AI 现造的。
                    log.warning("curated / 生成池都没有可播题, 且 "
                                "allow_live_generation=False —— 交回引擎兜底")
                    failure = "池空且现场生成已关闭(H2-G)"
                    self._dispatch(self.engine.submit_riddle(
                        None, error=failure, expect_round=expect_round))
                else:
                    # ---- G4-2 §四: live 现场生成**也走 keyword2** ----
                    #
                    # 原来这里恒为 `pick_blueprint -> gen_spec`(classic 链)。
                    # G4-2 的产品决定是: 默认 keyword2 开启时, 现场生成必须
                    # 与后台补池**同一套链**, 否则观众会看到风格断层 ——
                    # 池子里是 keyword2 的题, 现场生成突然冒出 blueprint
                    # 命题作文。
                    #
                    # kill-switch 仍然是 `--no-keyword-seed`: 显式关掉时
                    # **prefetch 与 live 两边同时**回 classic(§四: "不能
                    # 出现 prefetch=classic 而 live=keyword2, 或反过来")。
                    # 两边读的是同一个 config flag, 所以半切换在结构上
                    # 不可能发生 —— `_keyword_bag()` 与 prefetcher 的
                    # `_keyword_enabled()` 判的是同一个开关。
                    kw_bag, kw_meta, kw_seed = self._keyword_bag()
                    if kw_bag is not None:
                        from story.keyword_seed import keyword_spec
                        spec, why = keyword_spec(
                            self.writer, kw_bag, kw_seed,
                            avoid=payload.get("avoid"), recent=recent,
                            should_continue=None,
                            corpus_version=kw_meta.get("corpus_version", ""))
                        if spec is None:
                            failure = ("keyword2 现场生成未成题(%s)" % why)
                            log.warning("出题失败, 交回引擎走兜底: %s", failure)
                        else:
                            source = "keyword2_live"
                            self._submit_spec(spec, source,
                                              expect_round=expect_round)
                            if not spec.answer:
                                log.info("本题未解析出谜底, 揭晓时将重新生成")
                    else:
                        bp = self._pick_blueprint(recent)
                        spec = self.writer.gen_spec(
                            avoid=payload.get("avoid"), blueprint=bp,
                            recent=recent,
                            # bp is None 时**真的**跳过 blueprint 硬校验,
                            # 而不是退回默认 blueprint。
                            enforce_blueprint=bp is not None)
                        # 只有**通过质量门**的 spec 才允许上直播。
                        # gen_spec 保证: 失败时一定 error 非空且 puzzle 为空。
                        if spec.puzzle and not spec.error:
                            self._submit_spec(spec, source,
                                              expect_round=expect_round)
                            if not spec.answer:
                                log.info("本题未解析出谜底, 揭晓时将重新生成")
                        else:
                            # 记下来, **等释放锁之后**再提交 —— 见上面①
                            failure = spec.error or "没有生成合格谜题"
                            log.warning("出题失败, 交回引擎走兜底: %s", failure)
            except Exception as e:
                log.exception("出题异常: %s", e)
                failure = str(e)
            finally:
                self._narrating.release()

            # ---- 锁已释放, 现在提交失败结果(它会触发下一拍 retry) ----
            if failure is not None:
                try:
                    self._dispatch(self.engine.submit_riddle(
                        None, error=failure, expect_round=expect_round))
                except Exception as e:                # noqa: BLE001
                    log.exception("提交出题失败结果时出错: %s", e)
            self.push()

        threading.Thread(target=work, daemon=True, name="riddle").start()

    def _submit_spec(self, spec, source: str,
                     expect_round=None) -> None:
        """把一道**已通过质量门**的 spec 提交给引擎上屏。

        池子来的题与现场生成的题走这里同一段代码 —— 免得两条路各写
        一遍 `submit_riddle(...)` 参数, 然后其中一条漏传字段(第四轮
        的 P0 就是"两条路参数不一致"造成的)。

        `expect_round`: 这发是**为第几题**生成的。由 worker 从 RIDDLE
        payload 原样带回, 引擎据此丢弃迟到交付(Step 06 的 stale gate)。
        """
        r = _spec_to_riddle(spec)
        self._dispatch(self.engine.submit_riddle(
            r.puzzle, r.answer, r.hints, r.title,
            error=r.error, usage=r.usage, model=r.model,
            solve_atoms=r.solve_atoms, fair_clues=r.fair_clues,
            signature=spec.signature.to_dict(),
            spec=spec, source=source, expect_round=expect_round))

    def _pick_blueprint(self, recent: list, rng=None):
        """选下一条 blueprint(方案 §11 的 weighted-LRU)。

        用固定 seed 的 Random 实例 —— 每次出题都换 seed 会让"同输入不同
        输出", 复盘时无法重现。这里用**进程级** rng, 只保证可注入、可测。

        `rng`: 可选注入。后台补池(Q9)传**它自己的** rng, 这样补池开关
        不会改变 live 路径的 blueprint 序列(否则"同 seed 可复现"会变成
        "同 seed + 同补池状态可复现", 复盘时说不清)。默认 None = 用
        进程级 rng, live 调用点因此完全不受影响。

        返回 None 表示**本轮不施加 blueprint 硬约束**(由调用方转成
        `enforce_blueprint=False`)。注意这与"用默认 blueprint"完全不同:
        早先 None 会被 `gen_spec` 里的 `blueprint or PuzzleBlueprint()`
        悄悄变成 fixed default —— 关掉调度反而让所有题长一个样。
        """
        # 注意: 这里读的是 quality_scheduler_enabled, **不是** pool_enabled。
        # 两者职责不同: 前者管"题型分布受不受控", 后者管"要不要预生成题池"。
        if not getattr(self.cfg, "quality_scheduler_enabled", True):
            log.info("quality_scheduler_enabled=False: 本轮不施加 blueprint")
            return None
        try:
            from story.quality import Quotas, choose_blueprint
            quotas = Quotas.from_config(self.cfg)
            bp = choose_blueprint(recent, rng or self._rng, quotas)
            log.info("本题 blueprint: %s / %s / %s",
                     bp.mechanism_family, bp.solution_shape, bp.domain)
            # ⚠️ `_detail(logger, msg, ...)` 的第一个参数是 **logger**。
            # 早先这里漏传了 logger(只传了格式串), 于是这一行必抛
            # AttributeError('str' object has no attribute 'isEnabledFor')
            # —— 而它下面就是那个宽 `except Exception`, 所以后果是
            # **每一道题的 _pick_blueprint 都返回 None**: blueprint 调度
            # 整个静默失效, 所有题都按"不限形状"生成。修好之前,
            # Q4/Q7 的题型分布控制实际上没在跑。
            _detail(log, "blueprint 全文: %s", bp.describe())
            return bp
        except Exception as e:                       # noqa: BLE001
            # 调度失败不能让出题链断掉 —— 退化成"模型自由发挥"。
            # 这里返回的 None 会被调用方转成 enforce_blueprint=False,
            # 所以"不限形状"这次是真的(早先它其实会退回默认 blueprint)。
            #
            # ⚠️ 用 `log.exception` **带堆栈**: `choose_blueprint` 是纯代码
            # 调度器, 它抛异常基本只可能是**代码 bug**(早先那个漏传
            # logger 的 `_detail` 就是这么藏了一整个季度的)。只打一行
            # message 会让这类错误极难定位; 宽容处理是给"业务上可接受
            # 的失败"的, 不是给 bug 的。C6 能挡"永远返回 None",
            # 但挡不住"某一类 recent 状态才触发"。
            log.exception("blueprint 选择失败, 本题退化为不限形状: %s", e)
            return None

    def _hint(self, payload: dict) -> None:
        def work():
            try:
                if self.cfg.no_llm or not self.writer:
                    text, err = self._fake_hint(payload.get("level", 1)), None
                else:
                    text, err = self.writer.hint(
                        payload.get("puzzle", ""), payload.get("answer", ""),
                        payload.get("level", 1), payload.get("given"),
                        spec=payload.get("spec"),
                        touched_fact_ids=set(payload.get("touched_fact_ids")
                                             or ()))
                # ---- 成功与失败**都必须回调** Engine ----
                #
                # 第四轮 review: 早先只有 `if text:` 才回调, 于是"三次全泄底"
                # (hint() 返回 None) 或抛异常时, Engine 的 `_hint_pending`
                # 永远挂着 True -> 本题后续提示**永久不再派发**。
                # 失败也要回调, 由 Engine 清 pending + 设退避。
                if text:
                    self._dispatch(self.engine.submit_hint(
                        text, expect_round=payload.get("expect_round"),
                        expect_spec_key=payload.get("expect_spec_key")))
                else:
                    self._dispatch(self.engine.submit_hint(
                        None, error=err or "提示生成失败",
                        expect_round=payload.get("expect_round"),
                        expect_spec_key=payload.get("expect_spec_key")))
                self.push()
            except Exception as e:
                log.exception("提示异常: %s", e)
                # 异常同样要清 pending, 否则提示链在这里静默死掉。
                try:
                    self._dispatch(self.engine.submit_hint(
                        None, error=str(e),
                        expect_round=payload.get("expect_round"),
                        expect_spec_key=payload.get("expect_spec_key")))
                    self.push()
                except Exception:
                    log.exception("提示失败回调也异常")

        threading.Thread(target=work, daemon=True, name="hint").start()

    def _reveal(self, payload: dict) -> None:
        def work():
            try:
                core = str(payload.get("core_answer", "") or "").strip()
                if core:
                    # ---- v5: **确定性**揭晓, 0 次额外 LLM 调用 ----
                    #
                    # 旧路径让第二个 LLM 把已经审好的答案**再文学加工
                    # 一遍**。三个坏处, 都在真实直播里出现过:
                    #   1. 观众多等一次往返才看到答案;
                    #   2. 加工过程会把清楚的 core_answer 改复杂;
                    #   3. 二次生成 = 二次引入幻觉的机会。
                    #
                    # 现在先**逐字**念 core_answer, 再附完整解释。
                    # `core_answer` 是生成 + 审稿双重把关过的字段,
                    # 没有任何理由再让第三个模型改写它。
                    text = self._compose_reveal(core, payload.get("answer", ""))
                elif self.cfg.no_llm or not self.writer:
                    text = self._fake_reveal(payload.get("answer", ""))
                else:
                    # legacy: 没有 core_answer 的老题, 保持原路径。
                    text, err = self.writer.reveal(
                        payload.get("puzzle", ""), payload.get("answer", ""),
                        payload.get("reason", ""), payload.get("winner", ""))
                if text:
                    self._archive_reveal(payload, text)
                # 只有**题池来源**的题才补记 air:true —— 忠于这个文件的名字
                # 与职责。早先这里只判断 `spec is not None`, 于是
                # pool / live_generate / fallback 三种来源全都写进
                # pool_used.jsonl, 造成一个隐蔽的状态不一致:
                #   mark_used(aired=True) 只往 `_aired` 加,
                #   而重启后 load() 又把这个 key 加进 `_used` ——
                #   同一道 live_generate 题"当前进程不算已用, 重启后突然算"。
                # 真要做"全局所有播过题的 ledger", 该单独定义, 不要暗中
                # 借这个文件承担第二个职责。
                #
                # `pop_next` 交付时已经写了 air:false, 所以即使这一行丢了,
                # 题也**不会**复活 —— 这行只让"是否真的播完"可查。
                #
                # ---- Batch H2-F: curated 池也要补记 air:true ----
                # 两个池各自有账本, 所以要按**来源**分派到对应的那一个。
                # 早先这里只认 "pool", 于是 curated 题的 aired 永远停在
                # false —— "这道题真的播完了吗"查不出来, 而 curated 池
                # 恰恰是我们最想统计播出情况的那一批。
                #
                # ---- G4 / G4-R1: 来源标签改了, 这张表必须跟着改 ----
                # G4-2 §八 把池题的 provenance 从含糊的 `pool` 拆成了
                # `keyword2_pool` / `curated` / `keyword2_live` /
                # `classic_blueprint` / `engine_fallback`。但**这里**当时
                # 漏了跟着改, 于是:
                #
                #     keyword2_pool 的题 pop_next 时写了 aired:false,
                #     播完后**没有任何一行**把它改成 true。
                #
                # 后果不是题复活(used 已经记下了), 而是**播出完成账本
                # 永久停在未播** —— "这道题到底播完了没有"再也查不出来,
                # 而这正是 H2-F 当初加这一段的**唯一**目的。
                #
                # 所以这里改成**显式分派表**, 而不是 if/else 链:
                #   * 表里没有的来源(live 生成的一切)一律**不写任何池**
                #     —— 它们不在池里, 写进池账本就是上面那段注释警告的
                #     那个隐蔽不一致;
                #   * `pool` 保留为**旧 archive / 旧运行时**的兼容别名
                #     (G4 之前落盘的 spec 带的就是这个名字), 不能删。
                #
                # ⚠️ 新增来源时必须同时改这张表 —— 漏了就是 P1 复发,
                # 而且是**静默**复发(不报错, 只是账本慢慢失真)。
                _src = payload.get("spec_source")
                _pool = {
                    "keyword2_pool": self.pool,     # G4-2 起的生成池主来源
                    "curated": self.curated_pool,   # H2-F
                    "pool": self.pool,              # 兼容 G4 之前的落盘
                }.get(_src)
                # `keyword2_live` / `classic_blueprint` / `engine_fallback`
                # 刻意**不在表里** —— 它们不是池题, 不进池账本。
                if _pool is not None and payload.get("spec") is not None:
                    try:
                        _pool.mark_used(payload.get("spec"), aired=True)
                    except Exception:               # noqa: BLE001
                        # 题池只是加速器, 记不上不能影响揭晓。
                        log.exception("题池 mark_used 异常(忽略)")
                self._dispatch(self.engine.submit_reveal(
                    text, expect_round=payload.get("expect_round"),
                    expect_spec_key=payload.get("expect_spec_key")))
                self.push()
            except Exception as e:
                log.exception("揭晓异常: %s", e)
                self.engine.submit_reveal(
                    None, error=str(e),
                    expect_round=payload.get("expect_round"),
                    expect_spec_key=payload.get("expect_spec_key"))
                self.push()

        threading.Thread(target=work, daemon=True, name="reveal").start()

    def _archive_reveal(self, payload: dict, text: str) -> None:
        """把一题的谜面/谜底/问答落盘。失败即停引擎(沿用旧语义)。

        落盘结构按方案 §34/§35: 不只存 puzzle/answer, 而是**完整 spec**
        (facts/atoms/clues/hints) + blueprint/signature + 一整套 metrics。
        下一轮分析要能直接算, 不必再去日志里刨。
        """
        snap = self.engine.snapshot()
        model = getattr(getattr(self.writer, "client", None), "cfg", None)
        model = getattr(model, "model", "no-llm") if model else "no-llm"
        spec = payload.get("spec")
        spec_d = spec.to_archive() if spec is not None else {}
        record = dict(
            session=self.session_id, puzzle_index=snap.puzzle_index,
            # ---- 版本(方案 §55): 没有它就分不清成绩属于哪一版 ----
            #
            # **从 spec 自己的 archive 取**, 不写死。写死的话每次
            # `PuzzleSpec.to_archive()` 升版, 落盘里都还是旧数字 ——
            # `to_archive` 是 spec 的版本声明方, director 只是搬运工。
            # 没有 spec(理论上不会: REVEAL payload 一定带 spec)时退到
            # 当前值, 不猜。
            spec_version=spec_d.get("spec_version", 3),
            # ---- 来源(Q8) ----
            # "pool" / "live_generate" / "fallback", 由 engine 显式标记并
            # 一路带过来。**不从任何值推断** —— 我们在 blueprint_specified
            # 上已经踩过"从值猜来源"的坑(两个方向都会猜错)。
            # 老记录没有这个字段, 读的时候按 "live_generate" 理解即可。
            source=payload.get("spec_source") or "live_generate",
            prompt_version=spec_d.get("prompt_version", ""),
            quality_policy_version=spec_d.get("quality_policy_version", ""),
            puzzle=payload.get("puzzle", ""),
            answer=payload.get("answer", ""),
            # ---- v5 通关合同(赛后复盘"房间是怎么解出这题的") ----
            # `core_answer` 优先从 spec 取(spec.to_archive 一定有) ——
            # payload 那份只在没有 spec 的老路径上存在。
            core_answer=(spec_d.get("core_answer")
                         or payload.get("core_answer", "")),
            completion_fact_ids=spec_d.get("completion_fact_ids", []),
            reason=payload.get("reason", ""),
            winner=payload.get("winner", ""),
            reveal=text, qa=list(snap.qa_archive),
            # R2: 公开贡献链 —— "这题大家是怎么一起推出来的"。
            #
            # **直接搬 payload 那份**, 不在这里重新推理: 归档发生时
            # Engine 还在 REVEALING、reveal 状态尚未最终提交, payload
            # 是那一刻唯一权威的快照。
            #
            # 形状固定为公开字段(qid/user_name/text/verdict/is_final),
            # 内部 fact ID 在 Engine 侧就已经被筛掉了。要分析内部归属
            # 请看 `qa` 里的 `completion_contribution_fact_ids`。
            reveal_contributors=list(
                payload.get("reveal_contributors") or []),
            # 出题时定下的原子事实与公平线索 —— 赛后复盘
            # "为什么这条没判中"时必须能对照它们。
            #
            # **优先从 spec 取**(第三轮 review): spec.to_archive() 出来
            # 的一定是可 JSON 序列化的 dict。payload 那份是 Engine 传的,
            # 类型由 Engine 保证(它也在边界统一成 dict 了), 但多这一层
            # 兜底可以防"将来某个调用方又塞对象进来"把落盘打崩 ——
            # 落盘失败会**停引擎**, 代价太大, 不值得省这一行。
            solve_atoms=spec_d.get("solve_atoms",
                                   payload.get("solve_atoms", [])),
            fair_clues=spec_d.get("fair_clues",
                                  payload.get("fair_clues", [])),
            # ---- 完整的结构化定义(方案 §34) ----
            facts=spec_d.get("facts", []),
            hints=spec_d.get("hints", []),
            blueprint=spec_d.get("blueprint", {}),
            signature=spec_d.get("signature", {}),
            # 这两个布尔是给**分析**用的: blueprint 的默认值长得和"真的
            # 分配了 information_gap"一模一样, 不标出来, 兜底题与老数据
            # 会被算进调度分布, 统计全错。
            blueprint_specified=spec_d.get("blueprint_specified"),
            signature_present=spec_d.get("signature_present"),
            # ---- metrics(方案 §35) ----
            # **必须把本题的 spec 传进去**(第三轮 review): 早先
            # `_round_metrics` 自己去读 `self._current_spec`, 而那是
            # Director 侧的状态。真实路径会串题:
            #   第 10 题生成成功 -> _current_spec = 第10题
            #   第 11 题连续生成失败 -> Engine 内部走结构化兜底,
            #                        Director 的 _current_spec **没被更新**
            #   揭晓第 11 题 -> payload 是第11题兜底 spec, 但指标读到的
            #                  还是第10题的 -> 兜底题被记成
            #                  generated=true / attempts=2, 全错。
            # 现在只相信 REVEAL payload 里那个 spec。
            metrics=self._round_metrics(snap, spec),
            ts=time.time(), model=model)
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.cfg.puzzle_out_path)),
                        exist_ok=True)
            with open(self.cfg.puzzle_out_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            log.exception("谜题落盘失败，停止推进: %s", self.cfg.puzzle_out_path)
            self._archive_failed = True
            self.engine.stop("谜题落盘失败，请检查磁盘与输出路径")
            self._stop.set()

    def _round_metrics(self, snap, spec=None) -> dict:
        """这一题的运行指标(方案 §35)。

        目标是把"下一轮直播该看什么"直接算好落盘, 而不是事后翻日志:
        最有价值的是 `judge_calls / answer_calls` 与 `solution_candidate_count`
        —— 它们直接回答"candidate 闸门有没有真的省下调用"。
        """
        # ⚠️ Batch B closeout: `qa_archive` 里**也含** hint / nudge 这些
        # 系统记录(kind != "qa"), 直接 len() 会把系统提示算成人类问答 ——
        # `answered_count` / `answer_calls` / `judge_call_rate` 全部虚高,
        # 而这几项正是用来判断"candidate 闸门省了多少调用"的, 虚高就等于
        # 结论反向。所以先按 kind 过滤。
        qa = [r for r in (snap.qa_archive or [])
              if (r.get("kind") or "qa") == "qa"]
        answered = len(qa)
        candidates = sum(1 for r in qa if r.get("solution_candidate"))
        judges = sum(1 for r in qa if r.get("is_guess") is not None)
        unavailable = sum(1 for r in qa if r.get("status") == "unavailable")
        # question_count 用**落盘记录里的提问数**而不是 stat_questions:
        # 后者在 no-llm / 竞态下可能和 answered 对不上(实测见到 0 vs 1),
        # 而 metrics 的用途就是"下一轮直接拿来算", 自相矛盾的数字比没有更糟。
        asked = max(int(snap.stat_questions or 0), answered)
        out = {
            "question_count": asked,
            "answered_count": answered,
            "dropped_count": max(int(snap.stat_dropped or 0),
                                 max(0, asked - answered)),
            "solution_candidate_count": candidates,
            # 裁判调用次数 ≈ 有覆盖结果的记录数(只有 candidate 才会填这些)
            "judge_calls": judges,
            "answer_calls": answered,
            "judge_call_rate": (round(judges / answered, 3) if answered else 0.0),
            "candidate_rate": (round(candidates / answered, 3) if answered else 0.0),
            "hint_calls": snap.hint_count,
            "unavailable_count": unavailable,
            "llm_failures": unavailable,
            "viewers_seen": snap.stat_viewers_seen,
            "duration_ms": snap.puzzle_elapsed_ms,
            "solved": bool(snap.solved),
        }
        # ---- 生成/审稿指标(方案 §35) ----
        # 只看**传入的本题 spec**。绝不回退到 Director 的 _current_spec ——
        # 那是"最近一次生成成功"的题, 兜底题时会指向上一题。
        gen = dict(getattr(spec, "metrics", None) or {})
        out.update({
            "generation_attempts": gen.get("generation_attempts", 0),
            "generation_latency_ms": gen.get("generation_latency_ms", 0),
            "review_calls": gen.get("review_calls", 0),
            "review_decision": gen.get("review_decision", ""),
            "review_issues": gen.get("review_issues", []),
            "rewrite_count": gen.get("rewrite_count", 0),
            "review_latency_ms_total": gen.get("review_latency_ms_total", 0),
            "generated": bool(gen),
        })
        # ---- G4-E: 效率指标落盘 ----
        # 光在 `spec.metrics` 里躺着没用 —— `_archive_reveal()` 只挑上面
        # 那几个字段写进 puzzle.jsonl, 不搬整个 `spec.metrics`。下一场
        # 直播结束后分析正式 archive 时, G4 这几个数**看不见**。
        #
        # 缺省一律 `0 / {}`: 老题、结构化兜底题、`--no-llm` 的假题都没有
        # 这些键, 而"没有"和"0 次"在复盘里是同一个结论, 不该逼下游做
        # `.get(k, 0)` 之外的事(也避免 archive 里出现 null 与 0 混杂)。
        #
        # **不** bump spec_version / quality policy / prompt version: 这几
        # 个纯粹是运行指标, 不改变题的语义, 也不改变任何准入政策。bump
        # 只会把整池旧题无谓地 quarantine 掉。
        out.update({
            "candidate_repair_attempt_count":
                gen.get("candidate_repair_attempt_count", 0),
            "candidate_repair_success_count":
                gen.get("candidate_repair_success_count", 0),
            "hard_reject_before_review_count":
                gen.get("hard_reject_before_review_count", 0),
            "repair_attempt_reasons":
                dict(gen.get("repair_attempt_reasons") or {}),
            "repair_success_reasons":
                dict(gen.get("repair_success_reasons") or {}),
        })
        # ---- R4: 三段式生成链的 provenance ----
        #
        # ⚠️ 这一块与上面 G4-E 是**同一个坑**: `spec.metrics` 里写着不等于
        # 正式 archive 里看得见 —— 本函数是白名单搬运, 不搬整个 metrics。
        # R4 引入了 lane 与两个 prompt version(Story / Surface), 如果不
        # 显式搬过来, 直播 `puzzle.jsonl` 就分不出"这题是红是黑、是哪一版
        # prompt 产的" —— 而那正是下一轮复盘要拿来算的东西。
        #
        # 缺省与上面同规矩: 空串 / 0, 不写 null(老题 / 兜底题 / `--no-llm`
        # 的假题都没有这些键)。
        #
        # ⚠️ 仍然**不** bump spec_version / policy / prompt version —— 这里
        # 只是把已有的事实搬进 archive, 不改任何语义或准入政策。
        out.update({
            "lane": str(gen.get("lane") or ""),
            "keywords": list(gen.get("keywords") or []),
            "story_prompt_version": str(gen.get("story_prompt_version") or ""),
            "surface_prompt_version":
                str(gen.get("surface_prompt_version") or ""),
            "keyword_seed_version": str(gen.get("keyword_seed_version") or ""),
            "keyword_corpus_version":
                str(gen.get("keyword_corpus_version") or ""),
            "keyword_session_seed": gen.get("keyword_session_seed"),
            "keyword_draw_index": int(gen.get("keyword_draw_index") or 0),
        })
        # ---- R7: 主审 quality_checks + 安全复核证据 ----
        #
        # ⚠️ 第三次踩同一个坑(前两次是 G4-E 与 R4 provenance): 这些键在
        # `spec.metrics` 里躺着, 但本函数是**白名单搬运**, 不搬整个 metrics
        # —— 不显式列出来, 正式直播的 `puzzle.jsonl` 里就**没有**。
        #
        # 这一次的代价比前两次更直接: R6 的诊断之所以只能靠"重跑冻结产物"
        # 去猜, 正是因为原始 `quality_checks` 没落盘; 而 R7 的双门 AND 是
        # 靠 `safety_verified` / `safety_technical_fail` 才分得清"判了 false"
        # 与"网关抖了"。这些数不进 archive, 下一轮复盘就要把 R6 的弯路
        # 再走一遍。
        #
        # `quality_checks` 是主审原样返回的九项判定, 直接搬整份 dict ——
        # 它是**证据**, 不是我们能挑字段重算的东西(挑字段就等于二次解释)。
        out.update({
            "quality_checks": dict(gen.get("quality_checks") or {}),
            "safety_verified": gen.get("safety_verified"),
            "safety_reason": str(gen.get("safety_reason") or ""),
            "safety_prompt_version":
                str(gen.get("safety_prompt_version") or ""),
            "safety_verify_calls": int(gen.get("safety_verify_calls") or 0),
            "safety_technical_fail":
                int(gen.get("safety_technical_fail") or 0),
        })
        return out

    # ---- 离线(--no-llm)用的固定内容 ----
    _FAKE_RIDDLES = (
        ("一个男人走进餐厅，点了一碗海龟汤，喝了一口就冲出去自杀了。为什么？",
         "多年前他遭遇海难，同伴给他喝的“海龟汤”其实是同伴自己的肉。"
         "今天他喝到真正的海龟汤，发现味道完全不同，才明白当年喝的是什么，崩溃自杀。"),
        ("一个女人每天都要给丈夫做同样的汤，丈夫喝了一年，"
         "直到有一天她换了配方，丈夫却死了。为什么？",
         "丈夫被妻子长期下慢性毒，而那碗汤正是唯一的解药；"
         "妻子停手后他反而毒发身亡。"),
        ("一个盲人每天都要下楼取牛奶，从不出错。有一天他打开箱门，"
         "突然脸色大变，回家就自杀了。为什么？",
         "平时牛奶箱里除了牛奶还有一个空瓶（邻居小孩帮他取的）。"
         "今天他摸到的只有牛奶、没有空瓶，说明送奶的人换了、那个一直帮他的人不在了——"
         "他意识到自己唯一的朋友已经离开。"),
        ("他每天坐同一班电梯上班。有一天他按了自己常按的楼层，"
         "电梯却停在了别的楼层，他立刻辞职搬家了。为什么？",
         "他是这栋楼的管理员，靠按哪个按钮判断自己有没有瞎——"
         "他按的是盲文可辨的按钮。今天他按对了，电梯却去了别处，"
         "说明电梯被改造过，而他是唯一不知道的人，这份工作已不需要他。"),
    )

    def _fake_riddle(self) -> tuple[str, str]:
        self.segment_counter += 1
        p, a = self._FAKE_RIDDLES[self.segment_counter % len(self._FAKE_RIDDLES)]
        return p, a

    def _fake_verdict(self, text: str) -> str:
        """离线: 用关键词给个像样的裁决, 让演示不至于全是'无关'。

        注意要**保守**: 真实模型只在说到核心真相时才判'揭晓',
        这里也只在明显命中核心词时才揭晓, 否则离线演示会一题一问就结束。
        """
        t = text or ""
        if any(w in t for w in ("人肉", "同伴的肉", "吃了他", "是同伴")):
            return "揭晓"
        if any(w in t for w in ("海龟汤", "味道", "肉汤", "解药")):
            return "接近了"
        if any(w in t for w in ("天气", "名字", "几点", "老板")):
            return "无关"
        if any(w in t for w in ("死", "自杀", "杀", "毒", "病", "毒发")):
            return "是"
        return "不是"

    def _fake_hint(self, level: int) -> str:
        pool = list(P.FALLBACK_HINTS)
        return pool[min(max(level - 1, 0), len(pool) - 1)]

    def _fake_reveal(self, answer: str) -> str:
        return answer or self.cfg.fallback_answer

    @staticmethod
    def _compose_reveal(core_answer: str, answer: str) -> str:
        """v5 确定性揭晓文案 —— **不调 LLM**。

        结构固定:

            【核心答案】
            {core_answer}

            【完整解释】
            {answer}

        为什么第一句必须**逐字**是 core_answer: 它是"普通人一听就懂"
        的那句话, 且已经过生成 + 审稿双重把关。让模型改写它, 就是把
        唯一保证"人话先出现"的东西交给了第三次生成。

        `answer == core_answer` 时**不重复**贴完整解释(题目很短时
        两者会重合, 重复显示像 bug)。
        """
        core = (core_answer or "").strip()
        full = (answer or "").strip()
        lines = ["【核心答案】", core]
        if full and full != core:
            lines += ["", "【完整解释】", full]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    def _heartbeat_phase(self) -> str:
        try:
            return str(getattr(self.engine.phase, "value",
                               self.engine.phase) or "")
        except Exception:
            return ""

    def _live_heartbeat_loop(self) -> None:
        """直播存活 lease。失败只记 warning，绝不能影响主循环。"""
        while not self._stop.is_set():
            if not write_live_heartbeat(
                    self._live_heartbeat_path,
                    phase=self._heartbeat_phase(),
                    session_id=self.session_id):
                log.warning("直播心跳写入失败(离线补池会保守等待): %s",
                            self._live_heartbeat_path)
            self._stop.wait(LIVE_HEARTBEAT_INTERVAL)

    def _start_live_heartbeat(self) -> None:
        if not self._live_heartbeat_path:
            return
        if (self._live_heartbeat_thread is not None
                and self._live_heartbeat_thread.is_alive()):
            return
        # run() 真正启动前再抢先刷新一次，缩短 init -> source.start 之间的窗。
        write_live_heartbeat(
            self._live_heartbeat_path,
            phase=self._heartbeat_phase(),
            session_id=self.session_id)
        self._live_heartbeat_thread = threading.Thread(
            target=self._live_heartbeat_loop, daemon=True,
            name="live-heartbeat")
        self._live_heartbeat_thread.start()

    def _stop_live_heartbeat(self) -> None:
        if not self._live_heartbeat_path:
            return
        try:
            if self._live_heartbeat_thread is not None:
                self._live_heartbeat_thread.join(timeout=0.25)
        except Exception:
            pass
        if not clear_live_heartbeat(self._live_heartbeat_path):
            # 文件可能已经被新进程接管；clear 的 pid 保护会故意返回 False。
            _detail(log, "直播心跳未清理(可能已被新进程接管): %s",
                    self._live_heartbeat_path)

    # ------------------------------------------------------------------
    def _consume(self) -> None:
        """队列 -> 引擎。与 ws 线程解耦。"""
        while not self._stop.is_set():
            try:
                ev = self.inbox.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                # ---- Step 11: 按类型分发 ----
                # 两种事件走**同一个队列**(少一条并发链), 靠类型区分。
                # 互动事件**不**走 submit_danmaku —— 它不是一条提问。
                if isinstance(ev, InteractionEvent):
                    for a in self.engine.submit_interaction(ev):
                        self._run_action(a)
                    continue
                # 弹幕原文走 DEBUG: 平时不刷屏(-v 时才看得到)。
                # 真正值得看的(谁问了什么、AI 怎么答的)由 submit_qa 那边记。
                log.debug("弹幕 %s: %s", ev.user_name, ev.content)
                for a in self.engine.submit_danmaku(
                        ev.user_id, ev.user_name, ev.content,
                        message_id=getattr(ev, "message_id", "")):
                    self._run_action(a)
            except Exception as e:
                log.error("submit 异常: %s", e)

    def _scheduler(self) -> None:
        """唯一驱动 tick 的线程。"""
        interval = 1.0 / max(0.5, self.cfg.tick_hz)
        pushes_since = 0
        while not self._stop.is_set():
            try:
                for a in self.engine.tick():
                    self._run_action(a)
                # 上一拍有出题被推迟(锁被占) -> 现在补发。
                # 仍然走 engine 的 RIDDLE 动作, 所以 avoid/recent 不会丢。
                if self._deferred_riddle:
                    self._deferred_riddle = False
                    if self.engine.phase == Phase.SETTING:
                        log.info("补发被推迟的出题请求")
                        self._run_action(
                            self.engine.request_riddle_action("riddle_deferred"))
                # ---- Q9: 后台补池 ----
                # **只做决策 + 提交**, 生成在 prefetcher 自己的线程里跑。
                # 放在 tick 循环之后: 这一拍的动作(可能刚结束一次 REVEAL、
                # 也可能刚起了新 RIDDLE)已经生效, 再决定补不补。
                # `on_tick` 契约上绝不抛 —— 补池不能影响直播主循环。
                if self._prefetcher is not None:
                    self._prefetcher.on_tick()
                # ---- H3-D: Lazy Curator(**非阻塞**) ----
                #
                # ⚠️ 这里**绝不能**直接调 `step()`。
                #
                # H3-B 第一版就是这么写的, 而本循环是**唯一驱动
                # `engine.tick()` 的线程**。`step()` 会同步跑完
                # compile_one(LLM 编译 + 审稿 + 审计, 十几到几十秒),
                # 期间 tick 定时、hint deadline、reveal deadline、
                # 下一题 deadline、push **全部被拖住** —— 那不是"这一拍
                # 慢一点", 是**心跳停了**。
                #
                # 现在 `on_tick()` 只做两件事: 判断该不该开始 + 非阻塞
                # 提交一个 job —— 契约上**立刻返回**。真正跑 LLM 的是
                # LazyCurator 自己的单线程 worker。single-flight 仍然成立
                # (已有活在跑时提交会被直接丢弃, 不排队)。
                #
                # `max_candidates=1` 在这里的语义是"这一拍要不要开一条"
                # (不是"开几条"): 每拍重新评估一次压力, 用的是**当下**
                # 的状态, 而不是几十秒前排的队。
                if self._lazy_curator is not None:
                    try:
                        self._lazy_curator.on_tick(max_candidates=1)
                    except Exception:           # noqa: BLE001
                        # `on_tick` 契约上绝不抛, 但这里再兜一层: 直播主
                        # 循环**永远**不能因为后台审题而中断。
                        log.exception("lazy curator 调度异常(已忽略)")
                self.push()
                pushes_since += 1
                if self.engine.should_stop():
                    log.info("达到 max_rounds, 停止")
                    self._stop.set()
                    break
            except Exception as e:
                log.exception("调度异常: %s", e)
            self._stop.wait(interval)

    # ------------------------------------------------------------------
    def _prewarm(self) -> None:
        """G4-2 §五: 冷启动有限预热。**绝不抛, 绝不阻止启动。**

        ## 判定

            prefetcher / pool 没建        -> 什么都不做
            playable_count >= 1           -> **一次生成都不发**(§九-9)
            playable_count == 0           -> 最多 `pool_prewarm_max_rounds`
                                             轮 / `pool_prewarm_max_seconds` 秒,
                                             拿到 1 道立刻收工(§九-10)

        ## 为什么直接调 `_generate_one_inner` 而不是 `on_tick`

        `on_tick` 带 latch / 退避 / deadline 一整层"**该不该现在开始**"的
        判定, 而那些判定的前提(相位稳定、压力可读)在 `engine.start()`
        之前**不成立** —— 开播前那一刻探针里没有相位。这里要的是"我就是
        现在要一道", 所以绕过**调度层**, 只复用**执行层**。

        复用的是同一条执行路径: 同一个 `_generate_one_inner` -> 同一套
        Stage A/B / 审稿 / truth audit / 试玩 / `_finish_one` 入池。**没有
        第二份生成实现** —— 那正是这轮反复强调的。

        ## 有界 + 失败不阻止启动

        轮数与秒数**双上限**, 先到者停。最坏情况是"预算烧完一道都没成":
        打 WARNING 然后正常往下走 —— 引擎的兜底谜题会顶上, 直播不中断
        (与 `allow_live_generation=False` 走同一条路径)。预热是**优化**,
        不是可用性前提; 让它能把进程卡死就是把优化写成了单点故障。
        """
        if self._prefetcher is None or self.pool is None:
            return
        # ---- G4-R1: `--no-llm` / 无 client 时**根本不该预热** ----
        #
        # ⚠️ 这一条是 P0 修好之后才**暴露出来**的: 之前预热在 IDLE 下
        # 立刻就 `interrupted` 了(见 `prewarm_should_continue` 的说明),
        # 于是它**从来没走到** writer 那一步 —— 而 `--no-llm` 时
        # `writer is None`。把让路修好之后, 预热真的往下走, 就在
        # Stage A 那一步撞上了 `NoneType` 属性错误。
        #
        # (这里刻意**不写出那个方法名** —— `test_prefetch` 有一条源码级
        # 断言:"director.py 里不出现 Stage A 的方法名", 它守的是 live
        # 路径不许调 Stage A。注释里写出名字会把它误判成违规。)
        #
        # 也就是说: 旧的那个 bug **掩盖**了这一个。两个都得修 ——
        # 修了让路却不修这个, 冒烟立刻出现 Traceback。
        #
        # 判定复用 prefetcher 自己的 `_enabled()`(它已经正确处理了
        # `writer is None` / `pool is None` / `pool_prefetch_enabled`),
        # 而不是在这里再抄一遍条件 —— 两份会漂。
        #
        # ⚠️ 用 `getattr` 取而不是直接调: 测试里的替身 prefetcher
        # (`_CountingPrefetcher` 之类)只实现预热真正用到的那几个方法,
        # 没有 `_enabled`。缺省按"启用"处理 —— 那正是**替身存在时的旧
        # 行为**, 于是既有用例逐位不变。真实 `PoolPrefetcher` 永远有
        # 这个方法, 所以这条兼容分支在**生产路径上不可达**。
        _enabled = getattr(self._prefetcher, "_enabled", None)
        if _enabled is not None and not _enabled():
            log.info("预热跳过: 补池未启用(no_llm / 无 client / 补池关闭)")
            return
        budget = float(getattr(self.cfg, "pool_prewarm_max_seconds", 90.0)
                       or 0.0)
        max_rounds = int(getattr(self.cfg, "pool_prewarm_max_rounds", 2) or 0)
        if budget <= 0 or max_rounds <= 0:
            return
        try:
            inputs = self._prefetcher._generation_inputs()
            if self._prefetcher._playable(inputs) >= 1:
                # §九-9: 已经有得播 -> **0 次**额外生成调用。
                log.info("预热: 池里已有可播题, 跳过(0 次生成)")
                return
        except Exception:                       # noqa: BLE001
            log.exception("预热: 读可播数异常, 跳过预热")
            return

        log.info("预热: 冷启动 playable=0, 最多 %d 轮 / %.0fs 内取一道",
                 max_rounds, budget)
        t0 = time.monotonic()
        # ---- G4-R1: 预热必须注入**它自己的**让路谓词 ----
        #
        # ⚠️ 不注入的话这条路径在真实环境里**一道题都出不来**: 预热跑在
        # `engine.start()` 之前, 此刻 `engine.phase == Phase.IDLE`, 而后台
        # 判据 `_should_continue` 只认 QA / REVEALED —— 于是 Stage A 的
        # 第一笔调用还没发出就已经 `interrupted`。
        #
        # 谓词的语义见 `PoolPrefetcher.prewarm_should_continue`: 只看
        # "停了吗 / 预算到了吗", **不看相位**(预热期间本来就没有直播在跑)。
        try:
            sc = self._prefetcher.prewarm_should_continue(
                deadline=t0 + budget, should_abort=self._stop.is_set)
        except AttributeError:
            # 替身 prefetcher(测试)可能没有这个方法 —— 退回后台判据,
            # 即**行为与 G4-R1 之前逐位相同**。这里刻意不 fail closed:
            # 预热是优化, 为了一个缺失的可选方法而整段不跑是更差的选择。
            sc = None
        for i in range(max_rounds):
            if time.monotonic() - t0 > budget:
                break
            try:
                kind, detail, _extra = self._prefetcher._generate_one_inner(
                    inputs, sc)
            except TypeError:
                # 旧签名(只收 inputs)的替身。同上, 退回旧行为。
                try:
                    kind, detail, _extra = \
                        self._prefetcher._generate_one_inner(inputs)
                except Exception as e:          # noqa: BLE001
                    log.exception("预热第 %d 轮异常: %s", i + 1, e)
                    break
            except Exception as e:              # noqa: BLE001
                log.exception("预热第 %d 轮异常: %s", i + 1, e)
                break
            if kind == "ok":
                log.info("预热成功: 第 %d 轮拿到一道(%.1fs), 立即继续启动",
                         i + 1, time.monotonic() - t0)
                return
            log.info("预热第 %d 轮未成题(%s): %s", i + 1, kind,
                     str(detail)[:100])
            # 下一轮必须重取快照 —— 上一轮的入池/失败会改变 recent/avoid。
            try:
                inputs = self._prefetcher._generation_inputs()
            except Exception:                   # noqa: BLE001
                break
        log.warning("预热未拿到题(%.1fs) —— **正常启动**, "
                    "第一题按 emergency fallback 处理",
                    time.monotonic() - t0)

    def push(self) -> None:
        try:
            self.hub.publish(self.engine.snapshot().to_json())
        except Exception as e:
            log.error("push 失败: %s", e)

    # ------------------------------------------------------------------
    def run(self) -> int:
        cfg = self.cfg

        # ---- 启动 banner 的输出去向 ----
        #
        # ⚠️ 这里**不能**直接 `print()`: `setup_logging()` 为了在 Windows
        # 控制台上正确写中文, 把控制台句柄**重新 open** 成了 UTF-8 流
        # (`open(sys.stdout.fileno(), "w", encoding="utf-8")`), 并把它
        # 交给 logging 用。而 `sys.stdout` 仍然指着原来那个**带缓冲**的
        # 包装 —— 两者是同一个 fd 的两份 Python 对象。
        #
        # 后果实测过: banner 的 `print()` 在进程退出时才 flush, 而那时
        # 日志已经滚完几十行, 于是**读起来像是 banner 根本没打印**。
        # 直接 `| head` / 重定向时更糟 —— 整个 banner 消失。
        # (在干净基线上复现过, 不是本轮引入的。)
        #
        # 所以 banner 走 `_console` 显式 flush。日志用同一个 fd, 顺序
        # 因此是确定的: banner 先出, 日志在后。
        def _banner(line: str = "") -> None:
            out = _console if _console is not None else sys.stdout
            try:
                out.write(line + "\n")
                out.flush()          # ⚠️ 必须 —— 否则 banner 会在退出时才冒出来
            except Exception:        # noqa: BLE001
                try:
                    print(line)
                except Exception:    # noqa: BLE001
                    pass

        _banner("=" * 64)
        _banner("  竖屏 AI 海龟汤直播")
        _banner("=" * 64)
        _banner(f"  输入源      : {cfg.source_label}")
        #: 认证态**只打一个词** —— 绝不打 Cookie 本体/长度/前缀/hash。
        #: 见 vendor/douyin_fetcher/ws_cookie.py 的敏感边界说明。
        if cfg.live_id:
            try:
                from vendor.douyin_fetcher.ws_cookie import describe_ws_auth
            except Exception:                       # pragma: no cover
                from douyin_fetcher.ws_cookie import describe_ws_auth
            _banner(f"  WS 认证态   : ws_auth="
                  f"{describe_ws_auth(getattr(cfg, 'douyin_live_cookie', None))}")
        if cfg.proxy:
            _banner(f"  代理        : {cfg.proxy}")
        else:
            _banner(f"  代理        : 无(直连)")
        _banner(f"  回答节奏    : 逐条秒回, 并发 {cfg.qa_max_inflight}")
        _n = cfg.hint_seconds
        _banner(f"  时间轴      : 出题 -> " + " -> ".join(
            f"{int((i + 1) * _n / 60)}min 提示{i + 1}"
            for i in range(cfg.max_hints)) +
            f" -> {int((cfg.max_hints + 1) * _n / 60)}min 揭晓")
        _banner(f"  揭晓条件    : 有人猜中 / 时间轴走完（提问条数不设上限）")
        _banner(f"  揭晓展示    : {cfg.reveal_hold_seconds:.0f}s 后开下一题")
        _banner(f"  输出 JSONL  : {os.path.abspath(cfg.out_path)}")
        _banner(f"  谜题 JSONL  : {os.path.abspath(cfg.puzzle_out_path)}")
        # 题池状态打出来 —— 否则"到底有没有在用池子"只能靠翻日志。
        if self.pool is None:
            _banner("  题池        : 已关闭(pool_enabled=False)")
        else:
            st = self.pool.stats()
            _banner(f"  题池        : {st['available']}/{st['size']} 道可用, "
                  f"已用 {st['used']} (已播 {st['aired']})")
            # 库存 = 过得了准入门、且未用过的题数。它可能**小于** available
            # (盘上有、但 signature 被改坏所以播不出来)—— 两个都打,
            # 免得看到"可用 5 却一道都取不出来"时无从解释。
            _banner(f"                库存(可播) {st['stock']} 道"
                  + ("" if st.get("trustworthy", True)
                     else "  ⚠️ used 账本不可信 -> 池子本次禁用!"))
        # ---- G4-2 §二: **题源模式** —— 启动时必须一眼看得出来 ----
        #
        # 任务书原文要求的形状:
        #
        #     题源模式: keyword2 generated
        #     curated: OFF
        #
        # 显式开启时再打 `curated: ON`。这一行是**运维读的第一行**, 因为
        # "这一场到底会不会混进下载题" 是 G4-2 唯一想让人看出来的事 ——
        # 早先要翻三四行 curated 池状态 + 一行取题顺序才能拼出来。
        #
        # ⚠️ 它必须**先于**下面那些细节行打印: 细节行回答"状态如何",
        # 这一行回答"这是什么模式"。顺序反了读者会先陷进细节。
        _kw_on = bool(getattr(cfg, "pool_keyword_seed_enabled", True))
        _cur_on = bool(getattr(cfg, "prefer_curated", False))
        if _kw_on:
            _banner("  题源模式    : keyword2 generated")
        else:
            _banner("  题源模式    : classic Blueprint(--no-keyword-seed)")
        _banner(f"  curated     : {'ON' if _cur_on else 'OFF'}"
              + ("(--curated, 排在 generated 之后)"
                 if _cur_on else "(默认关闭; 要用来 --curated)"))
        # ---- Batch H2-F/G: curated 池状态 ----
        # 不打印的话,"这场到底有没有在播外部题库"只能靠翻日志 —— 而
        # 这正是这批唯一想验证的事。**单独一行**, 与 AI 池区分开。
        if not _cur_on:
            _banner("  curated 池  : 未加载(--curated 才加载; Lazy Curator 未启动)")
        elif self.curated_pool is None:
            _banner("  curated 池  : 不可用(文件缺失或已关闭)")
        else:
            cs = self.curated_pool.stats()
            _banner(f"  curated 池  : {cs['available']}/{cs['size']} 道可用, "
                  f"库存(可播) {cs['stock']} 道"
                  + ("" if cs.get("trustworthy", True)
                     else "  ⚠️ used 账本不可信 -> 本次禁用!"))
            if not getattr(cfg, "allow_live_generation", True):
                _banner("                现场 AI 生成已关闭(H2-G): 池空则走兜底")
            # ---- Batch H3-B: Lazy Curator 状态 ----
            # 任务书十七: 启动就要能看到"还有多少候选、审了多少、拒了
            # 多少、库存几位数"。**不打印题底。**
            if self._lazy_curator is None:
                _banner("  lazy curator: 关闭(无 writer / 无 curated 池 / 已关)")
            else:
                ls = self._lazy_curator.status()
                _banner(f"  lazy curator: 开"
                      f"(低水位 {ls['min']} -> 高水位 {ls['target']})")
                _banner(f"    curated candidate : {ls['candidates']}")
                _banner(f"    curated decided   : {ls['decided']}")
                _banner(f"    curated rejected  : {ls['rejected']}")
                _banner(f"    curated accepted  : {ls['accepted']}")
                _banner(f"    curated 库存      : {ls['stock']} 道"
                      f"(可播 {ls['playable']})")
        if self.pool is not None:
            if self._prefetcher is None:
                _banner("  补池        : 关闭(不在 --no-llm 下生成)")
            elif not getattr(cfg, "pool_prefetch_enabled", True):
                _banner("  补池        : 已关闭(--no-prefetch)")
            else:
                _banner(f"  补池        : 低水位 {cfg.pool_min_size} -> "
                      f"高水位 {cfg.pool_target_size}(QA 空闲时后台补)")
                if getattr(cfg, "playtest_enabled", False):
                    _banner(f"  试玩        : 开(最多 {cfg.playtest_max_turns} 轮, "
                          f"猜不中不入池; Player temperature=0)")
                else:
                    _banner("  试玩        : 关(--playtest 开启)")
            _banner(f"                {os.path.abspath(st['path'])}")
        if cfg.no_llm:
            _banner("  LLM         : 已禁用(--no-llm), 使用固定文案")
        else:
            for k, v in cfg.llm.masked().items():
                _banner(f"  LLM {k:11s}: {v}")
            # 多模型之后"到底哪一环用了哪个模型"必须能在启动日志里一眼看到。
            # 只打印**解析后的最终结果**(而不是配置项本身), 因为 stage
            # 可能来自 env / CLI / 全局回退三条路 —— 打印来源没用, 要让
            # operator 确认的是结果。这里只有模型名, 不含 api_key。
            _banner("  LLM model routing:")
            for stage, route in cfg.llm.resolved_routes().items():
                selector = cfg.llm.stage_models.get(stage) or cfg.llm.model
                marker = " (override)" if stage in cfg.llm.stage_models else ""
                if route.alias:
                    shown = f"{selector} -> {route.provider}/{route.model}"
                else:
                    shown = f"{route.provider}/{route.model}"
                _banner(f"    {stage:24s} = {shown}{marker}")
        _banner(f"  渲染页面    : http://{cfg.host}:{cfg.port}/  (?debug=1 开调试)")
        if cfg.open_window:
            _banner(f"  直播窗口    : 已自动打开(最大化, 竖屏居中, 左右黑边)")
            _banner(f"                直播伴侣用【窗口捕获】选它即可")
        else:
            _banner(f"  直播伴侣    : 加浏览器源 -> 上面的地址, 画布 1080x1920")
        _banner("=" * 64)

        for w in cfg.validate():
            log.warning("配置: %s", w)

        # 真直播先声明 lease；离线补池守护进程见到新鲜心跳立即让路。
        self._start_live_heartbeat()

        # 渲染服务
        self.server = RenderServer(self.hub, cfg.host, cfg.port)
        self.server.start()

        # 直播输出窗口(Chrome --app 模式: 无地址栏/标签栏/边框)
        if cfg.open_window:
            self._open_live_window()

        # 输入源
        self.source = self._build_source()
        self.source.start()

        # 工作线程
        threading.Thread(target=self._consume, daemon=True,
                         name="consume").start()
        threading.Thread(target=self._scheduler, daemon=True,
                         name="scheduler").start()

        # ---- G4-2 §五: 冷启动 prewarm ----
        #
        # keyword2 是**两阶段**的(Stage A + Stage B + 审稿 + audit), 一次
        # 现场生成要几十秒。若第一题就让观众等, 开播体验直接垮掉。
        #
        # 所以在进入第一题正式 SETTING **之前**做一次**有限**预热, 目标只是
        # "至少 1 道可播":
        #
        #     * **不是**补到 target(默认 5)。补满要 3~5 轮, 每轮几十秒 ——
        #       那是"开播前先静默三分钟", 对直播来说不可接受。1 道就够
        #       把第一题顶过去, 剩下的由后台补池在 REVEALED 窗口续。
        #     * 有**明确总预算**, 不能无限等。
        #     * 失败(网关抖 / 出的题都不合格)**不阻止启动** —— 按
        #       emergency fallback 处理, 与"池空"完全同一条路径。预热
        #       是优化, 不是可用性前提: 让它能把进程卡死是把优化写成了
        #       单点故障。
        #
        # 已经 playable >= 1 时**一次生成都不发**(§九-9): 那正是"预热"
        # 这个词的反面 —— 已经在跑的后台补池不需要人来踢一脚。
        self._prewarm()

        # 开场: 触发第一段
        for a in self.engine.start():
            self._run_action(a)

        try:
            while not self._stop.is_set():
                if self.engine.phase == Phase.STOPPED:
                    time.sleep(0.5)
                    if self.engine.phase == Phase.STOPPED:
                        # 直播结束后仍让页面可见一会儿
                        log.info("已停止, 3 秒后退出")
                        time.sleep(3)
                        break
                time.sleep(0.3)
        except KeyboardInterrupt:
            log.info("收到 Ctrl-C, 退出")
        finally:
            self._stop.set()
            self._stop_live_heartbeat()
            if self.source:
                try:
                    self.source.stop()
                except Exception:
                    pass
            if self._answer_pool:
                # cancel_futures: 还没开跑的排队任务直接丢掉。
                # 注意它**取消不了已经开始的** urllib 请求 —— 那些只能等
                # 自己超时(所以 QA 的 timeout 要短)。这里至少保证下播后
                # 不再往池子里灌新工作。与 prefetch 那边的写法对齐。
                self._answer_pool.shutdown(wait=False, cancel_futures=True)
            if self._prefetcher is not None:
                # 不阻塞: 在途生成可能长达 90s(gen_spec 的 budget_s),
                # 下播不该等它。
                self._prefetcher.shutdown()
            if self.server:
                self.server.stop()
            self._close_live_window()
            snap = self.engine.snapshot()
            log.info("共完成 %d 题, %d 条弹幕", snap.puzzles_total,
                     len(snap.danmaku))
        return 1 if self._archive_failed else 0


def _split_probe_profiles(raw) -> list:
    """`"a, b"` -> `["a", "b"]`。空白与空段一律丢掉。"""
    return [p.strip() for p in str(raw or "").split(",") if p.strip()]


def main(argv=None) -> int:
    cfg = from_args(argv)
    log_file = cfg.log_file
    if log_file is None:
        # 默认落盘(除非显式传 --log-file ""), 这样直播中途滚掉的明细
        # 事后还能翻。用固定名覆盖, 免得 data/ 里堆一堆日志。
        log_file = os.path.join("data", "run.log")
    setup_logging(cfg.log_level, log_file or None)
    # ---- Step 12C: gift_capture_diagnostic ----
    # 诊断模式**在直播之前**单独跑一段, 不参与 Director 的业务循环 ——
    # 它不接任何业务回调, 也不该被业务生命周期影响。真实直播验收时
    # (Issue §真实直播验收流程)就是这么用的: 先开诊断采一轮证据, 再决定
    # 下一步查哪一层。
    if getattr(cfg, "gift_capture_diagnostic", False):
        return _run_gift_capture_diagnostic(cfg)
    return Director(cfg).run()


def _run_gift_capture_diagnostic(cfg) -> int:
    """跑诊断模式, 并把结果打成一份可读的日志。

    ⚠️ **这是"独占诊断会话", 不是"直播旁路探针"。**

    开启 `--gift-capture-diagnostic` 后本进程**只**跑采集, `Director`
    (游戏主循环 / 问答 / 出题 / UI) **不会启动**, 跑完就退出。也就是说它
    适合"专门开一次直播测试, 先确认礼物到底抓不抓得到", **不**适合
    "游戏照常直播、后台同时采集"。

    把这件事写在这里而不是留给读者推断, 是因为这两种模式在运维上差别很
    大(前者要占用一次直播时段, 后者不用), 而 `--help` 里的一句"额外开
    1~3 路连接"很容易被读成后者。真正的旁路模式需要单独的生命周期接线
    (两套 WS 同时抢代理与风控配额), 不在本轮范围内。

    ⚠️ profile 名字拼错时**直接报错退出**(而不是忽略): 那种情况下实验
    会变成"看起来跑了三路, 其实只跑了两路", 而日志上完全正常。宁可起不来。
    """
    from story.gift_probe.runner import (            # noqa: E402
        DEFAULT_SUMMARY_INTERVAL_SECONDS,
        run_gift_capture_diagnostic,
    )
    names = _split_probe_profiles(getattr(cfg, "gift_probe_profiles", ""))
    try:
        result = run_gift_capture_diagnostic(
            cfg,
            profile_names=names,
            limit=getattr(cfg, "gift_probe_limit", 3),
            probe_root=getattr(cfg, "gift_probe_dir",
                               os.path.join("data", "gift_probe")),
            summary_interval_seconds=float(getattr(
                cfg, "gift_probe_summary_seconds",
                DEFAULT_SUMMARY_INTERVAL_SECONDS)),
            max_per_method=int(getattr(cfg, "gift_probe_max_per_method", 20)))
    except ValueError as e:
        # profile 名拼错 / probe 目录配置非法 —— 这类是**配置错误**,
        # 必须让用户看见, 不能吞。
        log.error("gift probe 配置错误: %s", e)
        print(f"!!! gift probe 配置错误: {e}", file=sys.stderr, flush=True)
        return 2
    log.info("gift probe 结束: session=%s 证据目录=%s",
             result["session_id"], result["session_dir"])
    for p in result["profiles"]:
        # 逐路打一行**结论**: 现场最需要的就是这一行(见 Issue §D 的四层判据)。
        log.info("gift probe 结论 profile=%s auth=%s verdict=%s "
                 "gift_family=%s parsed=%s emitted=%s",
                 p.get("profile"), p.get("auth"), p.get("verdict"),
                 p.get("gift_family_methods"), p.get("parsed_gift_count"),
                 p.get("emitted_gift_count"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
