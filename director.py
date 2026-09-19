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


def setup_logging(level: str, log_file: str | None = None) -> None:
    """日志装配: 控制台 + (可选)文件。

    控制台只留**主干**信息, 明细全部写文件 —— 直播时控制台滚得太快,
    真要排查还是得翻文件。
    """
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
                cfg, self.curated_pool, lc_writer, self.engine.pressure)

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
                # ---- Batch H2-G: 取题顺序 curated -> 生成池 -> 现场生成 ----
                #
                # curated 排在最前是这批的**核心主张**: 外部现成好题的
                # 认知反转密度比 AI 现场造的高得多, 所以有 curated 就先用。
                #
                # `pop_next` 在两处都一样: 它自带准入重校验 + 跨题门 +
                # 先落盘再交付。所以 curated 池不会因为"排在前面"而绕过
                # 任何一道门 —— 优先级只影响**顺序**, 不影响**标准**。
                if self.curated_pool is not None:
                    spec = self.curated_pool.pop_next(
                        recent_signatures=recent,
                        avoid=payload.get("avoid"))
                    if spec is not None:
                        source = "curated"
                if spec is None and self.pool is not None:
                    spec = self.pool.pop_next(
                        recent_signatures=recent,
                        avoid=payload.get("avoid"))
                    if spec is not None:
                        source = "pool"

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
                _src = payload.get("spec_source")
                _pool = (self.curated_pool if _src == "curated"
                         else self.pool if _src == "pool" else None)
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

    def push(self) -> None:
        try:
            self.hub.publish(self.engine.snapshot().to_json())
        except Exception as e:
            log.error("push 失败: %s", e)

    # ------------------------------------------------------------------
    def run(self) -> int:
        cfg = self.cfg

        print("=" * 64)
        print("  竖屏 AI 海龟汤直播")
        print("=" * 64)
        print(f"  输入源      : {cfg.source_label}")
        #: 认证态**只打一个词** —— 绝不打 Cookie 本体/长度/前缀/hash。
        #: 见 vendor/douyin_fetcher/ws_cookie.py 的敏感边界说明。
        if cfg.live_id:
            try:
                from vendor.douyin_fetcher.ws_cookie import describe_ws_auth
            except Exception:                       # pragma: no cover
                from douyin_fetcher.ws_cookie import describe_ws_auth
            print(f"  WS 认证态   : ws_auth="
                  f"{describe_ws_auth(getattr(cfg, 'douyin_live_cookie', None))}")
        if cfg.proxy:
            print(f"  代理        : {cfg.proxy}")
        else:
            print(f"  代理        : 无(直连)")
        print(f"  回答节奏    : 逐条秒回, 并发 {cfg.qa_max_inflight}")
        _n = cfg.hint_seconds
        print(f"  时间轴      : 出题 -> " + " -> ".join(
            f"{int((i + 1) * _n / 60)}min 提示{i + 1}"
            for i in range(cfg.max_hints)) +
            f" -> {int((cfg.max_hints + 1) * _n / 60)}min 揭晓")
        print(f"  揭晓条件    : 有人猜中 / 时间轴走完（提问条数不设上限）")
        print(f"  揭晓展示    : {cfg.reveal_hold_seconds:.0f}s 后开下一题")
        print(f"  输出 JSONL  : {os.path.abspath(cfg.out_path)}")
        print(f"  谜题 JSONL  : {os.path.abspath(cfg.puzzle_out_path)}")
        # 题池状态打出来 —— 否则"到底有没有在用池子"只能靠翻日志。
        if self.pool is None:
            print("  题池        : 已关闭(pool_enabled=False)")
        else:
            st = self.pool.stats()
            print(f"  题池        : {st['available']}/{st['size']} 道可用, "
                  f"已用 {st['used']} (已播 {st['aired']})")
            # 库存 = 过得了准入门、且未用过的题数。它可能**小于** available
            # (盘上有、但 signature 被改坏所以播不出来)—— 两个都打,
            # 免得看到"可用 5 却一道都取不出来"时无从解释。
            print(f"                库存(可播) {st['stock']} 道"
                  + ("" if st.get("trustworthy", True)
                     else "  ⚠️ used 账本不可信 -> 池子本次禁用!"))
        # ---- Batch H2-F/G: curated 池状态 ----
        # 不打印的话,"这场到底有没有在播外部题库"只能靠翻日志 —— 而
        # 这正是这批唯一想验证的事。**单独一行**, 与 AI 池区分开。
        if not getattr(cfg, "prefer_curated", True):
            print("  curated 池  : 已关闭(prefer_curated=False)")
        elif self.curated_pool is None:
            print("  curated 池  : 不可用(文件缺失或已关闭)")
        else:
            cs = self.curated_pool.stats()
            print(f"  curated 池  : {cs['available']}/{cs['size']} 道可用, "
                  f"库存(可播) {cs['stock']} 道"
                  + ("" if cs.get("trustworthy", True)
                     else "  ⚠️ used 账本不可信 -> 本次禁用!"))
            if not getattr(cfg, "allow_live_generation", True):
                print("                现场 AI 生成已关闭(H2-G): 池空则走兜底")
            # ---- Batch H3-B: Lazy Curator 状态 ----
            # 任务书十七: 启动就要能看到"还有多少候选、审了多少、拒了
            # 多少、库存几位数"。**不打印题底。**
            if self._lazy_curator is None:
                print("  lazy curator: 关闭(无 writer / 无 curated 池 / 已关)")
            else:
                ls = self._lazy_curator.status()
                print(f"  lazy curator: 开"
                      f"(低水位 {ls['min']} -> 高水位 {ls['target']})")
                print(f"    curated candidate : {ls['candidates']}")
                print(f"    curated decided   : {ls['decided']}")
                print(f"    curated rejected  : {ls['rejected']}")
                print(f"    curated accepted  : {ls['accepted']}")
                print(f"    curated 库存      : {ls['stock']} 道"
                      f"(可播 {ls['playable']})")
        if self.pool is not None:
            if self._prefetcher is None:
                print("  补池        : 关闭(不在 --no-llm 下生成)")
            elif not getattr(cfg, "pool_prefetch_enabled", True):
                print("  补池        : 已关闭(--no-prefetch)")
            else:
                print(f"  补池        : 低水位 {cfg.pool_min_size} -> "
                      f"高水位 {cfg.pool_target_size}(QA 空闲时后台补)")
                if getattr(cfg, "playtest_enabled", False):
                    print(f"  试玩        : 开(最多 {cfg.playtest_max_turns} 轮, "
                          f"猜不中不入池; Player temperature=0)")
                else:
                    print("  试玩        : 关(--playtest 开启)")
            print(f"                {os.path.abspath(st['path'])}")
        if cfg.no_llm:
            print("  LLM         : 已禁用(--no-llm), 使用固定文案")
        else:
            for k, v in cfg.llm.masked().items():
                print(f"  LLM {k:11s}: {v}")
        print(f"  渲染页面    : http://{cfg.host}:{cfg.port}/  (?debug=1 开调试)")
        if cfg.open_window:
            print(f"  直播窗口    : 已自动打开(最大化, 竖屏居中, 左右黑边)")
            print(f"                直播伴侣用【窗口捕获】选它即可")
        else:
            print(f"  直播伴侣    : 加浏览器源 -> 上面的地址, 画布 1080x1920")
        print("=" * 64)

        for w in cfg.validate():
            log.warning("配置: %s", w)

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


def main(argv=None) -> int:
    cfg = from_args(argv)
    log_file = cfg.log_file
    if log_file is None:
        # 默认落盘(除非显式传 --log-file ""), 这样直播中途滚掉的明细
        # 事后还能翻。用固定名覆盖, 免得 data/ 里堆一堆日志。
        log_file = os.path.join("data", "run.log")
    setup_logging(cfg.log_level, log_file or None)
    return Director(cfg).run()


if __name__ == "__main__":
    sys.exit(main())
