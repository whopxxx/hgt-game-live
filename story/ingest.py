#!/usr/bin/env python
# coding: utf-8
"""弹幕接入层。

两个输入源, 同一个接口 `DanmakuSource`:
    - LiveSource  接真实抖音房间(包 CallbackFetcher)
    - SimSource   离线回放脚本, 走真实 protobuf + 真实 _parseChatMsg
    - StdinSource 手打弹幕

关键设计: 三者最终都调用 DanmakuFetcher._parseChatMsg(payload)。
所以离线测试测的是**真实路径**, 不是平行假实现。

线程规则(硬约束):
    _parseChatMsg 运行在 websocket 读线程上。它只做 queue.put_nowait 后立即返回,
    绝不做 LLM/网络/重活 —— 否则会卡住全部弹幕并堵塞 socket。
"""

from __future__ import annotations

from datetime import datetime
import functools
import json
import logging
import os
import queue
import sys
import threading
import time
from typing import Callable, Optional, Protocol

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from danmaku import (ChatMessage, ControlMessage,       # noqa: E402
                    DanmakuFetcher, GiftMessage, LikeMessage)
from .config import parse_proxy  # noqa: E402

log = logging.getLogger("story.ingest")


# ======================================================================
def add_default_timeout(session, timeout=(5, 10)):
    """给 requests.Session 的所有请求加默认超时。

    **为什么必须加**: 上游 liveMan.py 里有多处**没有 timeout** 的请求
    (实测至少两处: `get_room_status()` 的 session.get, 以及 `ttwid`
    property 里的 session.get)。网络一抖, 这些请求会**永久挂起** ——
    而它们跑在抓取线程上, 于是线程**杀都杀不掉**(Python 线程无法强杀,
    只能等它自己跑完)。这是"重启后新连接也建不起来"的根本原因。

    补在 `Session.request` 这一层(而不是只补 `get`), 这样上游以后用
    `post()` / `put()` 也一并覆盖。

    timeout=(连接超时, 读取超时), 单位秒。
    """
    original_request = session.request

    @functools.wraps(original_request)
    def request_with_timeout(method, url, **kwargs):
        kwargs.setdefault("timeout", timeout)
        return original_request(method, url, **kwargs)

    session.request = request_with_timeout
    return session


class _StdoutToLog:
    """把第三方库的 print() 收进 logging。

    上游 liveMan.py 里散落着 `print(f"【聊天msg】…")` 之类的调试输出。
    它们**直接写 stdout**, 走不到我们的 logging 里 —— 一开播就刷屏,
    把真正有用的日志淹没(尤其是"首条消息加载"时那一大片)。

    这里在抓取线程运行期间临时把 sys.stdout 换成这个代理, 把每行转成
    log.debug。不改变上游任何代码。
    """

    def __init__(self, logger):
        self._log = logger
        self._buf = ""

    def write(self, s):
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.strip()
            if line:
                self._log.debug("[lib] %s", line)

    def flush(self):
        if self._buf.strip():
            self._log.debug("[lib] %s", self._buf.strip())
            self._buf = ""

    # 有些库会检查 isatty / encoding, 给两个无害实现
    def isatty(self):
        return False

    @property
    def encoding(self):
        return "utf-8"


class ChatEvent:
    """从 ws 线程传到消费线程的轻量事件。"""

    __slots__ = ("user_id", "user_name", "content", "ts", "message_id")

    def __init__(self, user_id, user_name, content, ts, message_id=""):
        self.user_id = user_id
        self.user_name = user_name
        self.content = content
        self.ts = ts
        #: 平台消息唯一 ID(Q12)。空串 = "这条没有 ID", 引擎会退回到
        #: reconnect guard 的指纹去重。**不要**用 `0` 当缺失标记 ——
        #: 它是个合法整数, 拿来当哨兵会让所有缺失 ID 的消息互相判重。
        self.message_id = message_id


class InteractionEvent:
    """Like / Gift 这类**业务互动**事件(Step 11)。

    与 `ChatEvent` 分开: 它们的字段与用途完全不同 —— 聊天走问答链,
    互动走 Summon 计量链(Step 13)。混成一个事件类型会让"这是不是一条
    提问"在每个消费点都要重新判断。

    字段刻意保持**协议原样**(不做业务换算): Step 11 只负责把真实协议
    送进业务链, "多少次点赞 = 1 Summon"是 Step 13 的事。这样 Step 12
    采集到的真实语义可以直接喂给 Step 13, 中间不掺一层翻译。
    """

    __slots__ = ("kind", "user_id", "user_name", "ts", "message_id",
                 "envelope_msg_id", "count", "total", "gift_id", "gift_name",
                 "combo_count", "repeat_count", "total_count",
                 "repeat_end", "group_id", "trace_id", "log_id",
                 "gift_combo", "gift_type", "diamond_count",
                 "group_count", "send_type")

    def __init__(self, kind, user_id=None, user_name=None, ts=0.0,
                 message_id="", envelope_msg_id="", count=0, total=0,
                 gift_id="", gift_name="",
                 combo_count=0, repeat_count=0, total_count=0,
                 repeat_end=0, group_id="", trace_id="", log_id="",
                 gift_combo=False, gift_type=0, diamond_count=0,
                 group_count=0, send_type=0):
        self.kind = kind              # "like" / "gift"
        self.user_id = user_id
        self.user_name = user_name
        self.ts = ts
        #: 平台消息唯一 ID(`common.msg_id`)。空串 = 没有 ID。
        #: **不要**用 0 当缺失标记(见 ChatEvent 的同一说明)。
        self.message_id = message_id
        #: 外层 `Message.msg_id`(proto 字段 3)。与 `message_id` 是
        #: **两个不同的字段**, 分开记录。
        #:
        #: ⚠️ 现在**不判断**哪个才是幂等主键, 也**不假设**两者相等 ——
        #: 那是 Step 12 用真实连续消息观察出来的结论。先如实采下来。
        self.envelope_msg_id = envelope_msg_id
        # ---- like ----
        self.count = count
        self.total = total
        # ---- gift ----
        self.gift_id = gift_id
        self.gift_name = gift_name
        self.combo_count = combo_count
        self.repeat_count = repeat_count
        self.total_count = total_count
        self.repeat_end = repeat_end
        self.group_id = group_id
        #: `GiftMessage.trace_id`(proto 字段 35)。
        #: ⚠️ **不是** `log_id` —— 两个是不同字段(16 / 35), 别互相冒充。
        self.trace_id = trace_id
        #: `GiftMessage.log_id`(proto 字段 16)。单独保留, 供 Step 12 观察。
        self.log_id = log_id
        # ---- Issue #42: 协议字段原样补齐(不赋任何业务语义) ----
        #: `GiftMessage.gift.combo`(bool)—— 礼物本体是否为连击型礼物。
        self.gift_combo = bool(gift_combo)
        #: `GiftMessage.gift.type`。
        self.gift_type = int(gift_type)
        #: `GiftMessage.gift.diamond_count` —— 礼物标价(钻石)。**只记录**,
        #: 不做任何"价值/兑换"换算(那是明确不做的事)。
        self.diamond_count = int(diamond_count)
        #: `GiftMessage.group_count`(proto 字段 4)。只做观察保留。
        self.group_count = int(group_count)
        #: `GiftMessage.send_type`(proto 字段 17)。
        self.send_type = int(send_type)


class DanmakuSource(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...


# ======================================================================
class CallbackFetcher(DanmakuFetcher):
    """DanmakuFetcher + 聊天回调。danmaku.py 一字不改。

    覆盖 _parseChatMsg 而非 _emit: _emit 太晚(在 _fp.write() 之后、被多种
    消息共用); _parseChatMsg 全保真且无需 socket。
    """

    def __init__(self, live_id, out_path, on_chat: Callable[[ChatEvent], None],
                 on_control: Optional[Callable[[str], None]] = None,
                 keep_all: bool = False, proxy: Optional[str] = None,
                 no_proxy: Optional[str] = None,
                 on_interaction: Optional[Callable] = None,
                 interaction_enabled: bool = False,
                 login_cookie: Optional[str] = None):
        super().__init__(live_id, out_path, keep_all=keep_all,
                         interaction_enabled=interaction_enabled,
                         login_cookie=login_cookie)
        self._on_chat = on_chat
        self._on_control = on_control
        #: Step 11: Like/Gift 的业务回调。空 = 只落库(老行为)。
        self._on_interaction = on_interaction
        self._on_frame = None       # 收到任何 WS 帧时的回调(由 LiveSource 设)
        #: 本 fetcher **第一个** WS 帧到达时的回调(Q12)。由 LiveSource 设。
        #: 用来把"新连接真的连上了"变成一个可观察事件 —— 见 `_wsOnMessage`。
        self._on_first_frame = None
        self._first_frame_seen = False
        # 给上游的 requests.Session 加默认超时 —— 否则网络一抖, 它的
        # 无超时 HTTP 请求会永久挂起, 把抓取线程钉死(见 add_default_timeout)。
        try:
            add_default_timeout(self.session, timeout=(5, 10))
        except Exception as e:
            log.warning("给 session 加超时失败(忽略): %s", e)
        # 代理: 抖音必须走代理时, WebSocket 升级请求也得走, 否则会被
        # 代理/网关拒掉(实测: 502 Bad Gateway, 响应头带 proxy-status,
        # 那是代理自己加的 —— 说明请求到了代理, 但它没转成功)。
        self._proxy = parse_proxy(proxy)
        self._no_proxy = no_proxy
        # 代际编号: 每次重建 +1。旧线程"诈尸"回来时, 靠它认领自己已过期,
        # 不再往业务层塞数据(见 _parseChatMsg)。
        self.generation = 0
        self._expired = False       # 被 watchdog 标记作废后, 不再处理任何数据
        if self._proxy:
            log.info("弹幕连接走代理: %s (%s)", proxy, self._proxy[2])

    def start(self):
        """启动连接。有代理时**临时**给 WebSocketApp.run_forever 注入代理参数。

        为什么要临时打补丁: 上游 `liveMan.py` 把 `run_forever()` 写成无参调用,
        而 websocket-client 的代理参数只能通过 run_forever 的关键字传进去。
        这里在连接期间替换掉 run_forever, 连上后再还原 —— 不改上游一行代码。
        """
        if not self._proxy:
            return super().start()
        import websocket as _ws_mod
        orig = _ws_mod.WebSocketApp.run_forever
        host, port, ptype = self._proxy
        skip = [s.strip() for s in (self._no_proxy or "").split(",") if s.strip()]

        def patched(self_ws, *a, **kw):
            kw.setdefault("http_proxy_host", host)
            kw.setdefault("http_proxy_port", port)
            if ptype != "http":
                kw.setdefault("proxy_type", ptype)
            if skip:
                kw.setdefault("http_no_proxy", skip)
            return orig(self_ws, *a, **kw)

        _ws_mod.WebSocketApp.run_forever = patched
        try:
            return super().start()
        finally:
            _ws_mod.WebSocketApp.run_forever = orig

    def _wsOnMessage(self, ws, message):
        """收到**任何** WebSocket 帧 —— 先记时间戳, 再交给上游解析。

        为什么在这里打点: watchdog 之前只看"多久没弹幕", 但**安静的房间
        本来就没弹幕** —— 于是好好的连接每 2 分钟被误判成"停摆"重连一次。

        实际上服务器一直在推帧(心跳 ack、在线人数、礼物、系统事件)。
        只要还有帧进来, 链路就是活的。这里给 `_on_frame` 回调打点,
        watchdog 用它判断"连接还活着吗", 而弹幕时间只用于业务展示。
        """
        try:
            if self._on_frame:
                self._on_frame()
        except Exception:
            pass
        # ---- Q12: "新连接真的连上了" ----
        # 只在**每连接一次**地报出去。
        #
        # ⚠️ 关键: `_first_frame_seen` 必须由 `_wsOnOpen` **每个连接**
        # 重置一次。上游 `DanmakuFetcher.start()` 里是 `while True` +
        # `run_forever()`, 断线后会在**同一个 fetcher 实例**上重新建连 ——
        # 若只在实例级置一次, 自动重连后的首帧就永远不会再报信号, 而这
        # 恰恰是重放发生的地方(commit 早先那版就是错的)。
        if not getattr(self, "_first_frame_seen", True):
            self._first_frame_seen = True
            cb = getattr(self, "_on_first_frame", None)
            if cb:
                try:
                    cb()
                except Exception:
                    log.exception("首帧回调异常(忽略)")
        return super()._wsOnMessage(ws, message)

    def _wsOnOpen(self, ws):
        """每个 WebSocket 连接建立时调用(上游的钩子)。

        在这里把"本连接还没收到过帧"重置 —— 于是**每次**重连成功后,
        第一帧都会重新触发 `_on_first_frame`, 上层才拿得到重连信号。
        """
        self._first_frame_seen = False
        return super()._wsOnOpen(ws)

    def _on_like(self, m, envelope_msg_id=0):
        """一条点赞 -> InteractionEvent。

        **代际检查与 chat 一致**: 被 watchdog 作废的旧连接不该再往业务层
        塞数据(见 `_parseChatMsg` 里那段说明)。
        """
        if getattr(self, "_expired", False):
            return
        if self._on_interaction is None:
            return
        try:
            self._on_interaction(self._like_event(m, envelope_msg_id))
        except Exception as e:                  # noqa: BLE001
            log.error("on_interaction(like) 回调异常: %s", e)

    def _on_gift(self, m, envelope_msg_id=0):
        """一个礼物 -> InteractionEvent。"""
        if getattr(self, "_expired", False):
            return
        if self._on_interaction is None:
            return
        try:
            self._on_interaction(self._gift_event(m, envelope_msg_id))
        except Exception as e:                  # noqa: BLE001
            log.error("on_interaction(gift) 回调异常: %s", e)

    # ---- 协议 -> 事件(纯映射, 不做业务换算) ----
    def _like_event(self, m, envelope_msg_id=0) -> "InteractionEvent":
        raw_mid = getattr(getattr(m, "common", None), "msg_id", 0) or 0
        return InteractionEvent(
            kind="like",
            user_id=getattr(getattr(m, "user", None), "id", None),
            user_name=getattr(getattr(m, "user", None), "nick_name", None),
            ts=time.monotonic(),
            message_id=str(raw_mid) if raw_mid else "",
            envelope_msg_id=str(envelope_msg_id) if envelope_msg_id else "",
            count=int(getattr(m, "count", 0) or 0),
            total=int(getattr(m, "total", 0) or 0))

    def _gift_event(self, m, envelope_msg_id=0) -> "InteractionEvent":
        raw_mid = getattr(getattr(m, "common", None), "msg_id", 0) or 0
        u = getattr(m, "user", None)
        g = getattr(m, "gift", None)
        return InteractionEvent(
            kind="gift",
            user_id=getattr(u, "id", None),
            user_name=getattr(u, "nick_name", None),
            ts=time.monotonic(),
            message_id=str(raw_mid) if raw_mid else "",
            envelope_msg_id=str(envelope_msg_id) if envelope_msg_id else "",
            gift_id=str(getattr(m, "gift_id", "") or ""),
            gift_name=str(getattr(g, "name", "") or ""),
            combo_count=int(getattr(m, "combo_count", 0) or 0),
            repeat_count=int(getattr(m, "repeat_count", 0) or 0),
            total_count=int(getattr(m, "total_count", 0) or 0),
            repeat_end=int(getattr(m, "repeat_end", 0) or 0),
            group_id=str(getattr(m, "group_id", "") or ""),
            # RF-3: trace_id 必须来自 `m.trace_id`(字段 35)。原来错把
            # `m.log_id`(字段 16)塞进来 —— 那是**另一个字段**, 真正的
            # trace_id 被丢掉。Step 12 恰恰要观察 trace/group 在一次
            # combo 内是否稳定, 带错字段进去等于白采。
            trace_id=str(getattr(m, "trace_id", "") or ""),
            log_id=str(getattr(m, "log_id", "") or ""),
            # ---- Issue #42: 以当前 proto 实际存在的字段为准原样透传 ----
            # (douyin.proto: GiftStruct.combo/type/diamondCount,
            #  GiftMessage.groupCount/sendType —— 均存在, 无一伪造。)
            gift_combo=bool(getattr(g, "combo", False) or False),
            gift_type=int(getattr(g, "type", 0) or 0),
            diamond_count=int(getattr(g, "diamond_count", 0) or 0),
            group_count=int(getattr(m, "group_count", 0) or 0),
            send_type=int(getattr(m, "send_type", 0) or 0))

    def _parseChatMsg(self, payload):
        # 代际检查: 这个回调可能来自**已经被废弃的旧连接** —— 旧线程卡在
        # 某个请求上, watchdog 已经起了新 fetcher, 十分钟后旧请求突然返回,
        # 旧线程又活过来继续塞消息。若不挡, 就会新旧两路数据一起进来。
        # (用 getattr 兜底: 测试里常用 __new__ 造半成品实例, 没有该属性。)
        if getattr(self, "_expired", False):
            return
        m = ChatMessage().parse(payload)
        # ---- Q12: 平台消息唯一 ID ----
        # `Common.msg_id` 是 protobuf 字段 2(vendor/.../douyin.py:475)。
        # betterproto 对缺失的 `common` 返回默认实例, `.msg_id` 得 `0` ——
        # 那表示"这条没有 ID", 转成空串交给引擎走降级路径。
        #
        # **不能**把 `0` 当成一个真实 ID: 缺失 ID 的消息会全部拿到 `0`,
        # 于是第二条起全部互相判重、被静默丢弃。这是这套改动最容易踩的坑。
        raw_mid = getattr(getattr(m, "common", None), "msg_id", 0) or 0
        mid = str(raw_mid) if raw_mid else ""
        # 立即交接: 不做任何重活, 不在此线程调 LLM。
        try:
            self._on_chat(ChatEvent(m.user.id, m.user.nick_name, m.content,
                                    time.monotonic(), mid))
        except Exception as e:
            log.error("on_chat 回调异常: %s", e)
        # 保留 JSONL 落库
        self._emit("chat", m.user.id, m.user.nick_name, m.content)

    def _emit(self, kind, user_id=None, user_name=None, content=None,
              extra=None):
        """和上游一样落库, 但**不 print**。

        上游 `_emit` 末尾有一句 `print(f"[{kind}] …")` —— 每来一条弹幕就
        在终端刷一行, 把真正的日志淹没。这里只去掉那句 print, 落库/计数
        逻辑原样保留(直接调用父类实现后再撤掉输出做不到, 所以重写这一小段)。
        """
        rec = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "kind": kind,
            "user_id": str(user_id) if user_id is not None else None,
            "user_name": user_name,
            "content": content,
        }
        if extra:
            rec.update(extra)
        try:
            self._fp.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._fp.flush()
        except Exception as e:
            log.warning("落库失败(忽略): %s", e)
        self._counts[kind] = self._counts.get(kind, 0) + 1

    def _parseControlMsg(self, payload):
        m = ControlMessage().parse(payload)
        if m.status == 3:
            log.info("收到下播信号")
            if self._on_control:
                try:
                    self._on_control("stream_ended")
                except Exception as e:
                    log.error("on_control 回调异常: %s", e)
            # 下播是**终止**, 不是"关掉这次连接": 只 stop() 的话重连循环
            # 下一轮又会连上, 抖音会把同一批弹幕原样重发(实测: 一次下播
            # 变成每秒一次的重连 + 重复弹幕)。terminate() 会置永久标志位,
            # 让 `DanmakuFetcher.start()` 的 while 循环真正退出。
            # 实测: ws 未建立时关连接会抛 AttributeError, 必须 guard
            try:
                self.terminate()
            except Exception as e:
                log.warning("terminate() 失败(忽略): %s", e)


# ======================================================================
class LiveSource:
    """接真实直播间。

    danmaku.py 的重连退避永不重置(既有 bug), 会话后期一次抖动就丢最多 60s,
    且之后一直慢。这里用 watchdog: 监测停摆 -> **重建**一个全新 fetcher
    (退避归零)。不改 danmaku.py。
    """

    def __init__(self, cfg, inbox: "queue.Queue[ChatEvent]",
                 on_stream_end: Optional[Callable[[], None]] = None,
                 on_reconnect: Optional[Callable[[], None]] = None,
                 on_reconnected: Optional[Callable[[], None]] = None):
        self.cfg = cfg
        self.inbox = inbox
        self._on_stream_end = on_stream_end
        #: "**正在重建**" —— watchdog 判定停摆后调用(语义见 `_watch`)。
        self._on_reconnect = on_reconnect
        #: "**重连成功了**"(Q12) —— 新连接的**第一个真实 WS 帧**到达时调用。
        #: 与 `_on_reconnect` 是两件事: 前者是"我要重建了", 后者是"新的
        #: 真的连上了"。重放识别只关心后者 —— 因为抖音是在**重连之后**
        #: 把那批旧弹幕原样重发的。
        self._on_reconnected = on_reconnected
        self._stop = threading.Event()
        self._fetcher: Optional[CallbackFetcher] = None
        #: 是否**曾经**连上过(Q12b)。首次建连不开 replay guard ——
        #: 那时没有"之前那批弹幕"可重放。
        self._ever_connected = False
        self._last_event = time.monotonic()   # 最后一条**弹幕**(业务用)
        self._last_frame = time.monotonic()   # 最后一个 **WS 帧**(判活用)
        self._restarts = 0
        self._restart_lock = threading.Lock()
        self._consecutive_fails = 0      # 连续重建失败次数
        self._thread: Optional[threading.Thread] = None
        self._watchdog: Optional[threading.Thread] = None
        #: "本 session 礼物链已确认可用" —— 第一次真正收到 GiftMessage 时
        #: 置位并打一次 INFO(Issue #42 §2)。挂在 LiveSource 而不是某个
        #: fetcher 实例上: watchdog 会重建 fetcher, flag 掉了就会重复打。
        self._gift_chain_confirmed = False

    # ------------------------------------------------------------------
    def _on_chat(self, ev: ChatEvent) -> None:
        self._last_event = time.monotonic()
        self.inbox.put_nowait(ev)

    def _on_interaction(self, ev) -> None:
        """Like/Gift 业务事件 -> 同一个 inbox。

        用**同一个队列**: 消费线程已经是单线程、已有背压处理, 再开一条
        队列只会多一处要维护的并发与关停顺序。事件类型靠 `isinstance`
        区分(见 director 的分发)。

        **不更新 `_last_event`** —— 它是"最后一条弹幕", 用来判"房间还有
        没有人在说话"。收到礼物不代表有人提问。
        """
        # ---- Issue #42: 礼物链首次确认 ----
        # Cookie 配置了只代表"配置过", 不代表链路真的通。第一条真实
        # GiftMessage 到达业务回调, 才是"可用"的证据。只报一次。
        if (getattr(ev, "kind", "") == "gift"
                and not self._gift_chain_confirmed):
            self._gift_chain_confirmed = True
            log.info("礼物消息链已确认可用")
        self.inbox.put_nowait(ev)

    def _on_frame(self) -> None:
        """收到任何 WS 帧 —— 说明链路还活着。

        和 `_last_event`(弹幕)分开: 安静的房间可能很久没弹幕, 但服务器
        一直在推心跳/在线人数。用**帧**判活才不会误判重连。
        """
        self._last_frame = time.monotonic()

    def _on_control(self, kind: str) -> None:
        if kind == "stream_ended":
            # 下播 = 本 source 的终点。**在这里就设终止位**, 不等 director 的
            # finally: 那条路要等主循环 3 秒宽限才走完, 期间 fetcher 的
            # 重连循环还活着, 会再连上一次并把旧弹幕重发一遍。
            self.stop()
            if self._on_stream_end:
                self._on_stream_end()

    def _build(self) -> CallbackFetcher:
        out = os.path.abspath(self.cfg.out_path)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        f = CallbackFetcher(self.cfg.live_id, out, self._on_chat,
                            self._on_control, keep_all=self.cfg.keep_all,
                            proxy=self.cfg.proxy,
                            no_proxy=self.cfg.no_proxy,
                            on_interaction=self._on_interaction,
                            interaction_enabled=getattr(
                                self.cfg, "interaction_enabled", False),
                            login_cookie=getattr(
                                self.cfg, "douyin_live_cookie", None))
        f._on_frame = self._on_frame
        f._on_first_frame = self._on_first_frame
        return f

    def _on_first_frame(self) -> None:
        """每个新连接的第一个真实帧 —— 这才是"重连成功"(Q12)。

        ⚠️ **首次建连不算重连**: 程序刚启动时也会走到这里, 但那时根本
        没有"之前那批弹幕"可重放。报出去只会让引擎白白开一个 20 秒的
        guard, 把启动后的无 ID 消息押进缓冲。所以头一次只记标记。

        注意它**不替代** `_on_reconnect`: 那个是 watchdog 的"我要重建了"
        (且只在停摆 120s 后触发), 服务于 `reconnect_fails` 告警链。
        这里纯粹是给引擎一个"重放可能马上要来了"的起点。
        """
        if not self._ever_connected:
            self._ever_connected = True
            log.info("弹幕连接已建立(首次, 不算重连)")
            return
        log.info("弹幕连接已重建(收到首帧)")
        if self._on_reconnected:
            try:
                self._on_reconnected()
            except Exception:
                log.exception("on_reconnected 回调异常(忽略)")

    def _run(self) -> None:
        self._fetcher = self._build()
        self._last_event = time.monotonic()
        self._run_fetcher(self._fetcher)

    def _watch(self) -> None:
        interval = max(5.0, self.cfg.stall_seconds / 4)
        while not self._stop.wait(interval):
            # 判活看**帧**(任何数据), 不看弹幕 —— 安静的房间没弹幕是正常的,
            # 用它判活会把好连接每 2 分钟误杀一次。
            idle = time.monotonic() - self._last_frame
            if idle < self.cfg.stall_seconds:
                if self._consecutive_fails:
                    log.info("弹幕连接已恢复(之前连续重建失败 %d 次)",
                             self._consecutive_fails)
                    self._consecutive_fails = 0
                continue
            self._restarts += 1
            self._consecutive_fails += 1
            log.warning("弹幕停摆 %.0fs, 重建抓取连接(第 %d 次, 连续失败 %d 次)",
                        idle, self._restarts, self._consecutive_fails)
            self._restart()
            if self._on_reconnect:
                try:
                    # 告诉上层"这是第几次连续失败" —— 连续多次就是真断了,
                    # 页面可以据此显示"弹幕连接异常"而不是假装在直播。
                    self._on_reconnect(self._consecutive_fails)
                except TypeError:
                    try:
                        self._on_reconnect()
                    except Exception:
                        pass
                except Exception:
                    pass
            # 重建后立刻把计时器归零, 给新连接一个完整的窗口
            # (否则下一轮 tick 又会判定"还在停摆"而反复重建)
            self._last_event = time.monotonic()
            self._last_frame = time.monotonic()
            # 连续失败太多次 -> 别再无脑重试, 把问题**显式喊出来**。
            # 之前的毛病是静默失败: 画面还在放谜题, 但弹幕全收不到,
            # 只有翻日志才发现。
            if self._consecutive_fails == self.cfg.reconnect_alert_after:
                log.error("=" * 56)
                log.error("弹幕连接连续重建 %d 次仍未恢复!", self._consecutive_fails)
                log.error("可能原因: 网络不通 / 房间已下播 / 被抖音风控 / 代理问题")
                log.error("画面上的谜题仍在继续, 但**观众的弹幕收不到了**。")
                log.error("请检查网络后重启, 或 Ctrl-C 退出。")
                log.error("=" * 56)

    def _restart(self) -> None:
        """停摆时重建抓取连接。

        抖音的 WS 有个麻烦特性: 心跳能发出去, 但服务端**不再推数据**,
        socket 也一直不断 —— 所以 `run_forever()` 不会自己返回。

        重建的三条纪律:
          ① **标记旧 fetcher 作废**(`_expired=True`) —— 即使它卡在某个请求上
             后来又"诈尸"返回, 也不会再往业务层塞数据。
          ② **只等 2 秒**, 绝不用 `join()` 无参调用 —— 否则 watchdog 自己
             也会被一起卡死。
          ③ 不等旧线程死干净就起新的 —— 新连接在独立 socket 上, 不受影响。
        """
        if not self._restart_lock.acquire(blocking=False):
            log.debug("已有重建在进行, 跳过")
            return
        try:
            old = self._fetcher
            if old is not None:
                # ① 先作废: 从这一刻起, 旧线程再吐数据一律丢弃
                old._expired = True
                old.generation = -1
                self._force_close(old)
                # ② 只给 2 秒; 收不掉就算了 —— 新连接不依赖它
                t_old = self._thread
                if t_old is not None and t_old.is_alive():
                    t_old.join(timeout=2.0)
                    if t_old.is_alive():
                        log.warning("旧抓取线程 2s 未退出, 已作废并放弃它, "
                                    "直接起新连接")
            self._last_event = time.monotonic()
            self._last_frame = time.monotonic()
            self._fetcher = self._build()
            self._fetcher.generation = self._restarts
            self._thread = threading.Thread(target=self._run_quiet, daemon=True,
                                            name="live-fetcher")
            self._thread.start()
        finally:
            self._restart_lock.release()

    @staticmethod
    def _force_close(f) -> None:
        """尽最大努力让上游 fetcher 的 run_forever() 退出。

        难点: `run_forever()` 卡在 `sock.recv()` 上, 而从**另一个线程**调
        `ws.close()` 只是标记了关闭 + 关掉 socket, 并不会立刻唤醒阻塞中的
        recv —— 实测旧线程 5 秒都不退出, 新旧两个抓取线程互相打架, 结果
        谁也连不上(日志里"停摆 -> 重建 -> 又停摆"的死循环)。

        做法: 除了 close, 还要**强行 shutdown 底层 socket**,
        让 recv 立刻抛异常返回。
        """
        ws = getattr(f, "ws", None)
        if ws is not None:
            # ① 先拿到底层 socket 并 shutdown —— 这一步才能真正唤醒 recv
            sock = getattr(ws, "sock", None)
            if sock is not None:
                try:
                    import socket as _s
                    sock.shutdown(_s.SHUT_RDWR)
                except Exception:
                    pass
            # ② 再走正常 close(触发 on_close)
            try:
                ws.close()
            except Exception:
                pass
        try:
            # 这里**必须**用 stop() 而不是 terminate(): 我们只是在淘汰一个
            # 停摆的旧实例, 它的重连循环该被放行(新 fetcher 已经接管)。
            # 若改成 terminate(), 旧实例被永久终止 —— 看着没问题, 但语义
            # 就变成"淘汰 = 杀死", 和 `LiveSource.stop()`(整体终止)混在一
            # 起, 以后有人复用 _force_close 去关一个还要重连的 fetcher 就会踩坑。
            f.stop()                       # 上游实现: self.ws.close()
        except Exception:
            pass
        # ③ 兜底: 直接关掉 fetcher 持有的底层 socket
        for attr in ("sock", "_sock"):
            s = getattr(f, attr, None)
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass

    def _run_quiet(self) -> None:
        self._run_fetcher(self._fetcher)

    @staticmethod
    def _run_fetcher(f) -> None:
        """跑抓取循环, 期间把上游的 print() 收进 logging。

        上游 liveMan.py 会直接 print 各种调试信息(聊天msg/礼物/心跳…),
        不接管的话开播就刷屏。这里只在本线程内换掉 sys.stdout。
        """
        old = sys.stdout
        sys.stdout = _StdoutToLog(log)
        try:
            f.start()
        except Exception as e:
            log.error("抓取线程退出: %s", e)
        finally:
            try:
                sys.stdout.flush()
            except Exception:
                pass
            sys.stdout = old

    # ------------------------------------------------------------------
    def _needs_cookie_warning(self) -> bool:
        """没有配置 DOUYIN_LIVE_COOKIE -> 需要(且仅需要)提醒一次。

        抽成方法是为了能离线单测 —— start() 里起线程, 不适合做断言现场。
        """
        return not getattr(self.cfg, "douyin_live_cookie", None)

    def start(self) -> None:
        # ---- Issue #42: Cookie fail-open ----
        # 没配 Cookie 直播**照常启动**(弹幕/点赞不受影响), 但礼物消息
        # 匿名连接收不到(现场已确认)。这里只提醒**一次**, 不刷屏;
        # 也绝不输出 Cookie 本体/长度/hash。
        if self._needs_cookie_warning():
            log.warning("未配置 DOUYIN_LIVE_COOKIE —— 礼物消息可能收不到,"
                        " 弹幕与点赞不受影响(本提醒只出现一次)")
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="live-fetcher")
        self._thread.start()
        self._watchdog = threading.Thread(target=self._watch, daemon=True,
                                          name="live-watchdog")
        self._watchdog.start()
        log.info("LiveSource 已启动: %s", self.cfg.live_id)

    def stop(self) -> None:
        """**终止**本 source —— 此后不再有任何重连。

        注意这里用的是 `terminate()` 而不是 `stop()`: 后者只关当前 socket,
        fetcher 的 `while True` 下一轮照样重连。下播后无限重连的根因就在
        这个差别上。`_restart()` 里淘汰旧 fetcher 用的是 `stop()`, 那里
        **必须**保持"允许重试"的语义, 不要跟着改。
        """
        self._stop.set()
        f = self._fetcher
        if f is not None:
            try:
                f.terminate()
            except Exception:
                pass

    @property
    def restarts(self) -> int:
        return self._restarts


# ======================================================================
class SimSource:
    """离线回放脚本。时钟驱动, 可 loop 无人值守。

    脚本每行一个 JSON:
        {"at_ms":1000,"user_name":"观众A","content":"#上楼"}
        {"at_ms":2000,"user_id":123,"user_name":"观众B","content":"#开门"}
        {"loop":true}
        {"control":"stream_ended"}

    at_ms 是相对本轮开始的偏移。loop=true 时每轮从头重发。
    经**真实** protobuf + **真实** _parseChatMsg —— 离线即真实路径。
    """

    def __init__(self, cfg, inbox: "queue.Queue[ChatEvent]",
                 on_stream_end: Optional[Callable[[], None]] = None):
        self.cfg = cfg
        self.inbox = inbox
        self._on_end = on_stream_end
        self._script: list[dict] = []
        self._loop_items: list[dict] = []
        self._controls: list[dict] = []
        self._loop_enabled = False
        self._auto_id = 900000             # 缺省**用户** id 的计数器
        #: 合成**消息** id 的计数器(Q12)。与 `_auto_id` 分属不同命名空间,
        #: 免得用户 id 撞上消息 id。基准取 9 亿, 远离真实平台 id 的量级。
        self._msg_id = 900_000_000
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # 用一个不落库的 fetcher 实例驱动真解析路径
        self._fetcher: Optional[CallbackFetcher] = None

    # ------------------------------------------------------------------
    def _load(self) -> None:
        path = self.cfg.sim_path
        items: list[dict] = []
        loop = False
        with open(path, "r", encoding="utf-8") as f:
            for ln, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith("//"):
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError as e:
                    log.error("脚本第 %d 行不是合法 JSON: %s", ln, e)
                    continue
                if rec.get("loop"):
                    loop = True
                elif rec.get("control"):
                    items.append({"control": rec["control"]})
                elif "content" in rec:
                    items.append({
                        "at_ms": int(rec.get("at_ms", 0)),
                        "user_name": rec.get("user_name", "观众"),
                        "user_id": rec.get("user_id"),
                        "content": rec["content"],
                    })
        self._script = items
        # loop 时不重发 control 行(否则会立刻结束)
        self._loop_items = [it for it in items if "control" not in it]
        self._controls = [it for it in items if "control" in it]
        self._loop_enabled = loop
        log.info("SimSource 载入 %d 条弹幕, loop=%s", len(self._loop_items), loop)

    def _ensure_fetcher(self) -> CallbackFetcher:
        """构造一个真 fetcher 用于驱动 _parseChatMsg。

        用 /dev/null 等价的丢弃文件, 避免污染真实 JSONL。
        """
        if self._fetcher is None:
            # 注意: 不调用 start(), 所以不会有 socket; 仅借用解析路径。
            self._fetcher = CallbackFetcher.__new__(CallbackFetcher)
            self._fetcher._on_chat = lambda ev: self.inbox.put_nowait(ev)
            self._fetcher._on_control = None
            self._fetcher.keep_all = False
            self._fetcher.out_path = os.devnull
            import collections
            self._fetcher._counts = collections.Counter()
            self._fetcher._fp = open(os.devnull, "w", encoding="utf-8")
        return self._fetcher

    def _emit(self, rec: dict) -> None:
        f = self._ensure_fetcher()
        m = ChatMessage()
        m.common.method = "WebcastChatMessage"
        # ---- Q12: 合成一个**单调递增**的 msg_id ----
        # 不设的话每条都是 `msg_id=0` -> 引擎判成"没有 ID"; 而如果哪天
        # 有人把 `0` 当成真实 ID, 第二条起就全被去重丢掉了。
        #
        # **计数器不随 loop 重置**: 循环播放每遍都产生新 ID, 所以 loop
        # 不会被误判成重放(它的语义是"又演一遍", 不是"同一批消息重发")。
        # 真·重复 ID 的场景由专门的测试夹具覆盖。
        self._msg_id += 1
        m.common.msg_id = self._msg_id
        m.user.nick_name = rec["user_name"]
        uid = rec.get("user_id")
        if uid is None:
            self._auto_id += 1
            uid = self._auto_id
        m.user.id = int(uid)
        m.content = rec["content"]
        # 走真实解析路径
        f._parseChatMsg(m.SerializeToString())

    # ------------------------------------------------------------------
    def _run(self) -> None:
        self._load()
        if not self._loop_items:
            log.error("脚本没有可用弹幕行")
            return
        first_pass = True
        while not self._stop.is_set():
            # 每轮: 按 at_ms 排序回放
            base = time.monotonic()
            for rec in sorted(self._loop_items, key=lambda r: r["at_ms"]):
                if self._stop.is_set():
                    return
                wait = rec["at_ms"] / 1000.0 - (time.monotonic() - base)
                if wait > 0 and self._stop.wait(wait):
                    return
                try:
                    self._emit(rec)
                except Exception as e:
                    log.error("模拟弹幕投递失败: %s", e)
            # control 行只在第一遍发送
            if first_pass:
                first_pass = False
                for c in self._controls:
                    if c["control"] == "stream_ended":
                        log.info("SimSource: 脚本触发 stream_ended")
                        if self._on_end:
                            self._on_end()
                        return
            if not self._loop_enabled:
                return
            # loop: 回放一遍后歇一会儿再来(海龟汤没有固定回合长度,
            # 用一个够长的等待, 让脚本大致按一题的节奏循环)
            if self._stop.wait(getattr(self.cfg, "sim_loop_gap", 20.0)):
                return

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="sim-source")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


# ======================================================================
class StdinSource:
    """从标准输入手打弹幕。每行 '内容' 或 '名字: 内容'。"""

    def __init__(self, cfg, inbox: "queue.Queue[ChatEvent]"):
        self.cfg = cfg
        self.inbox = inbox
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._auto_id = 800000
        #: 合成**消息** id 的计数器(Q12)。与 `_auto_id` 分属不同命名空间。
        #: ⚠️ 必须在这里初始化 —— `_run()` 里会 `+= 1`, 漏了的话第一条
        #: stdin 输入就 AttributeError(test_ingest 早先只驱动了 sim 路径,
        #: 所以 CI 没抓到)。
        self._msg_id = 800_000_000

    def _run(self) -> None:
        log.info("StdinSource: 每行输入弹幕(如 `#上楼`), Ctrl+D 结束")
        # Windows 上 sys.stdin 默认按 GBK 解码, 而输入/脚本基本是 UTF-8,
        # 结果中文全变成乱码(实测: "甲:#他是盲人吗" -> "鐢?:#浠栨槸...")。
        # 这里显式按 UTF-8 读, 坏字节用替换字符兜住, 不让它抛异常。
        stream = sys.stdin
        try:
            stream = open(sys.stdin.fileno(), "r", encoding="utf-8",
                          errors="replace", closefd=False)
        except Exception:
            try:
                sys.stdin.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
        try:
            for line in stream:
                if self._stop.is_set():
                    return
                line = line.rstrip("\n").rstrip("\r")
                if not line.strip():
                    continue
                name, content = "我", line
                if ":" in line:
                    a, b = line.split(":", 1)
                    if a.strip() and not a.strip().startswith("#"):
                        name, content = a.strip(), b.strip()
                self._auto_id += 1
                self._msg_id += 1
                self.inbox.put_nowait(ChatEvent(self._auto_id, name, content,
                                                time.monotonic(),
                                                str(self._msg_id)))
        except Exception as e:
            log.error("stdin 读取结束: %s", e)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="stdin-source")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
