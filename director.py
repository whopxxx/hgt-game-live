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
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from story.config import Config, from_args          # noqa: E402
from story.engine import RoundEngine                # noqa: E402
from story.ingest import (ChatEvent, LiveSource,    # noqa: E402
                          SimSource, StdinSource)
from story import parser as P                       # noqa: E402
from story.llm import AnthropicMessagesClient, PuzzleWriter  # noqa: E402
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
        # 逐条秒回: 独立的 ANSWER 并发池(与 qa_max_inflight 对齐)
        self._answer_pool: ThreadPoolExecutor | None = None
        if not cfg.no_llm:
            self._answer_pool = ThreadPoolExecutor(
                max_workers=max(1, cfg.qa_max_inflight), thread_name_prefix="answer")

        self.engine.model_requested = cfg.llm.model
        self.engine.source = cfg.source_label

        if not cfg.no_llm:
            self.client = AnthropicMessagesClient(cfg.llm)
            self.writer = PuzzleWriter(client=self.client)

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
                payload.get("user_name", ""), payload.get("text", ""))
            log.info("答 %r -> %.1fs %s", payload.get("text", "")[:16],
                     time.time() - t0,
                     (results[0].verdict if results else f"失败: {err}"))
            if not results:
                # 解析不出 -> 立刻给一个中性的兜底裁决, 不让提问卡住
                results = [QAResult(qid=qid, verdict="无关", comment="")]
            self._dispatch(self.engine.submit_qa(
                results, error=err,
                model=getattr(self.writer.client.cfg, "model", None)))
        except Exception as e:
            log.exception("回答异常: %s", e)
            self._dispatch(self.engine.submit_qa(
                [QAResult(qid=qid, verdict="无关")], error=str(e)))
        self.push()

    # ---- RIDDLE / HINT / REVEAL: 单 worker(低频) ----
    def _riddle(self, payload: dict) -> threading.Thread:
        def work():
            if not self._narrating.acquire(blocking=False):
                log.debug("已有在途 LLM 任务, 跳过出题")
                return
            try:
                if self.cfg.no_llm or not self.writer:
                    res_p, res_a = self._fake_riddle()
                    self._dispatch(self.engine.submit_riddle(
                        res_p, res_a, list(P.FALLBACK_HINTS),
                        title="海龟汤", model="no-llm"))
                else:
                    r = self.writer.gen_riddle(avoid=payload.get("avoid"))
                    if r.error and not r.puzzle:
                        log.warning("出题失败: %s", r.error)
                    self._dispatch(self.engine.submit_riddle(
                        r.puzzle, r.answer, r.hints, r.title,
                        error=r.error, usage=r.usage, model=r.model))
                    if r.puzzle and not r.answer:
                        log.info("本题未解析出谜底, 揭晓时将重新生成")
                self.push()
            except Exception as e:
                log.exception("出题异常: %s", e)
                self._dispatch(self.engine.submit_riddle(None, error=str(e)))
                self.push()
            finally:
                self._narrating.release()

        t = threading.Thread(target=work, daemon=True, name="riddle")
        t.start()
        return t

    def _hint(self, payload: dict) -> None:
        def work():
            try:
                if self.cfg.no_llm or not self.writer:
                    text = self._fake_hint(payload.get("level", 1))
                else:
                    text, err = self.writer.hint(
                        payload.get("puzzle", ""), payload.get("answer", ""),
                        payload.get("level", 1), payload.get("given"))
                if text:
                    self._dispatch(self.engine.submit_hint(text))
                self.push()
            except Exception as e:
                log.exception("提示异常: %s", e)

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
                self._dispatch(self.engine.submit_reveal(text))
                self.push()
            except Exception as e:
                log.exception("揭晓异常: %s", e)
                self.engine.submit_reveal(None, error=str(e))
                self.push()

        threading.Thread(target=work, daemon=True, name="reveal").start()

    def _archive_reveal(self, payload: dict, text: str) -> None:
        """把一题的谜面/谜底/问答落盘。失败即停引擎(沿用旧语义)。"""
        snap = self.engine.snapshot()
        model = getattr(getattr(self.writer, "client", None), "cfg", None)
        model = getattr(model, "model", "no-llm") if model else "no-llm"
        record = dict(session=self.session_id, puzzle_index=snap.puzzle_index,
                      puzzle=payload.get("puzzle", ""),
                      answer=payload.get("answer", ""),
                      reason=payload.get("reason", ""),
                      winner=payload.get("winner", ""),
                      reveal=text, qa=[r for r in snap.qa_log],
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
