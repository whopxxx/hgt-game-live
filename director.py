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
from story.ingest import (ChatEvent, LiveSource,    # noqa: E402
                          SimSource, StdinSource)
from story import parser as P                       # noqa: E402
from story.llm import (AnthropicMessagesClient, PuzzleWriter,  # noqa: E402
                       _spec_to_riddle)
from story.pool import PuzzlePool                   # noqa: E402
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
        self._stop = threading.Event()
        self._narrating = threading.Lock()          # RIDDLE/HINT/REVEAL 互斥
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

        if not cfg.no_llm:
            self.client = AnthropicMessagesClient(cfg.llm)
            # **必须传 runtime_cfg**: temperature 与 quota 定义在 Config 上,
            # 而 client.cfg 是 LLMConfig。漏掉它 -> 这些参数全部静默失效
            # (取到 None, 网关用默认值), 而测试因为 Fake 上有这些字段仍全绿。
            self.writer = PuzzleWriter(client=self.client, runtime_cfg=cfg)

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
            self._prefetcher = PoolPrefetcher(
                cfg=cfg, pool=self.pool, writer=self.writer,
                probe=self.engine.pressure,
                probe_inputs=self.engine.snapshot_generation_inputs,
                pick_blueprint=self._pick_blueprint,
                rng=pf_rng)

    # ------------------------------------------------------------------
    def _build_source(self):
        if self.cfg.live_id:
            return LiveSource(self.cfg, self.inbox,
                              on_stream_end=self._on_stream_end,
                              on_reconnect=self._on_reconnect)
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
        elif k == ActionKind.BROADCAST:
            self.push()

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
                [QAResult(qid=payload["qid"], verdict=verdict)]))
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
                facts=payload.get("facts"))
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
                model=getattr(self.writer.client.cfg, "model", None)))
        except Exception as e:
            log.exception("回答异常: %s", e)
            self._dispatch(self.engine.submit_qa(
                [QAResult(qid=qid, verdict=P.UNAVAILABLE,
                          comment="刚才网络抖了一下，再发一次吧",
                          status="unavailable")], error=str(e)))
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
                spec = None
                source = "live_generate"
                if self.pool is not None:
                    spec = self.pool.pop_next(
                        recent_signatures=recent,
                        avoid=payload.get("avoid"))
                    if spec is not None:
                        source = "pool"

                if spec is not None:
                    # 池子里来的: 结构已经齐了, 直接上屏。
                    self._submit_spec(spec, source)
                elif self.cfg.no_llm or not self.writer:
                    res_p, res_a = self._fake_riddle()
                    self._dispatch(self.engine.submit_riddle(
                        res_p, res_a, list(P.FALLBACK_HINTS),
                        title="海龟汤", model="no-llm", source=source))
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
                        self._submit_spec(spec, source)
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
                    self._dispatch(self.engine.submit_riddle(None, error=failure))
                except Exception as e:                # noqa: BLE001
                    log.exception("提交出题失败结果时出错: %s", e)
            self.push()

        threading.Thread(target=work, daemon=True, name="riddle").start()

    def _submit_spec(self, spec, source: str) -> None:
        """把一道**已通过质量门**的 spec 提交给引擎上屏。

        池子来的题与现场生成的题走这里同一段代码 —— 免得两条路各写
        一遍 `submit_riddle(...)` 参数, 然后其中一条漏传字段(第四轮
        的 P0 就是"两条路参数不一致"造成的)。
        """
        r = _spec_to_riddle(spec)
        self._dispatch(self.engine.submit_riddle(
            r.puzzle, r.answer, r.hints, r.title,
            error=r.error, usage=r.usage, model=r.model,
            solve_atoms=r.solve_atoms, fair_clues=r.fair_clues,
            signature=spec.signature.to_dict(),
            spec=spec, source=source))

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
            log.warning("blueprint 选择失败, 本题**真的**不限形状: %s", e)
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
                    self._dispatch(self.engine.submit_hint(text))
                else:
                    self._dispatch(self.engine.submit_hint(
                        None, error=err or "提示生成失败"))
                self.push()
            except Exception as e:
                log.exception("提示异常: %s", e)
                # 异常同样要清 pending, 否则提示链在这里静默死掉。
                try:
                    self._dispatch(self.engine.submit_hint(None, error=str(e)))
                    self.push()
                except Exception:
                    log.exception("提示失败回调也异常")

        threading.Thread(target=work, daemon=True, name="hint").start()

    def _reveal(self, payload: dict) -> None:
        def work():
            try:
                if self.cfg.no_llm or not self.writer:
                    text = self._fake_reveal(payload.get("answer", ""))
                else:
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
                if (self.pool is not None
                        and payload.get("spec") is not None
                        and payload.get("spec_source") == "pool"):
                    try:
                        self.pool.mark_used(payload.get("spec"), aired=True)
                    except Exception:               # noqa: BLE001
                        # 题池只是加速器, 记不上不能影响揭晓。
                        log.exception("题池 mark_used 异常(忽略)")
                self._dispatch(self.engine.submit_reveal(text))
                self.push()
            except Exception as e:
                log.exception("揭晓异常: %s", e)
                self.engine.submit_reveal(None, error=str(e))
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
            spec_version=2,
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
            reason=payload.get("reason", ""),
            winner=payload.get("winner", ""),
            reveal=text, qa=list(snap.qa_archive),
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
        qa = list(snap.qa_archive or [])
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

    # ------------------------------------------------------------------
    def _consume(self) -> None:
        """队列 -> 引擎。与 ws 线程解耦。"""
        while not self._stop.is_set():
            try:
                ev = self.inbox.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                # 弹幕原文走 DEBUG: 平时不刷屏(-v 时才看得到)。
                # 真正值得看的(谁问了什么、AI 怎么答的)由 submit_qa 那边记。
                log.debug("弹幕 %s: %s", ev.user_name, ev.content)
                for a in self.engine.submit_danmaku(ev.user_id, ev.user_name,
                                                    ev.content):
                    self._run_action(a)
            except Exception as e:
                log.error("submit_danmaku 异常: %s", e)

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
            if self._prefetcher is None:
                print("  补池        : 关闭(不在 --no-llm 下生成)")
            elif not getattr(cfg, "pool_prefetch_enabled", True):
                print("  补池        : 已关闭(--no-prefetch)")
            else:
                print(f"  补池        : 低水位 {cfg.pool_min_size} -> "
                      f"高水位 {cfg.pool_target_size}(QA 空闲时后台补)")
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
                self._answer_pool.shutdown(wait=False)
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
