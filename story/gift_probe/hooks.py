#!/usr/bin/env python
# coding: utf-8
"""把诊断探针接到抓取链上, 并**保持生产行为逐字不变**(Step 12C)。

## 接线点: 基线自己的两个 `_probe_bump` 扩展点

诊断能力需要三样东西:

    1. 每帧计数(帧数 / 消息数 / method 分布);
    2. 每条消息的**原始 payload**(用于 Gift-family 与 parse_error 落盘);
    3. primary GiftMessage 解析成功后的语义字段。

`DanmakuFetcher._wsOnMessage` 已经有一个很合适的结构 —— 它在每条消息上:

    _m = msg.method
    self._probe_bump("method_counts", _m)      # handler **之前**
    fn = handlers.get(_m)
    if fn is None: ...; continue
    try:
        fn(...)                                # 业务 handler
    except Exception as e:
        self._probe_bump("parse_error_counts", _m)   # handler **之后**
        print(...)

也就是说, "落盘决策"需要的两个时机(payload 还在手上、且分别在 parse 前与
parse 失败时)在基线里**本来就是分开的**。我们只要覆写 `_probe_bump`, 在
"做基线那件事"之外额外挂载:

    method_counts 分支        -> parse **之前** 的落盘决策(A: Gift-family)
    parse_error_counts 分支   -> parse **失败** 时的落盘决策(B: 证据)

## 为什么用 mixin + 动态子类, 而不是改 danmaku.py

`danmaku.py` 是**生产路径**, 直播正在跑的就是它。往里加诊断逻辑会让
"Issue 测试点 13(probe 关闭时生产行为逐字保持)"变成一个要人肉复核的问题。

动态子类的好处是结构性的: 基线那些类**一个字都不用改**, 诊断类只在
**自己**身上加打点, 再 `super()` 交给基线。关掉诊断时跑的就是那份没被
动过的代码 —— 这条保证是机械成立的, 不依赖人记得检查。

## 三层计数各自的推进时机(必须不同, 否则判据失效)

    gift_method_seen      跟 method_counts 同步(服务端给了什么)
    parsed_as_primary     只有 handler **没抛**才算(在 after-dispatch 记)
    emitted               只有业务回调**返回后**才算(在 _on_gift 之后记)

把它们合并成一个"收到就 +1"会让"服务端没给 / parse 炸了 / 回调断了"这
三种完全不同的故障在计数上长得一模一样 —— 而那三种要查的地方毫不相干。
"""

from __future__ import annotations

import gzip
import os
import sys
import threading
from typing import Optional

from .probe import CaptureProbe, GiftSemanticProbe, TransportSummaryProbe
from .profile import ProfileCounters

#: 基线的分发循环要用到的 proto 类型。**只读**用途 —— 与 `danmaku.py`
#: 里 import 的是同几个类, 所以"解析同一帧得到同一结果"是同一份实现。
from protobuf.douyin import PushFrame, Response     # noqa: E402

import websocket                                    # noqa: E402


class _NullLock:
    """无锁占位 —— 与 `with lock` 同形, 省掉各处 `if lock is not None`。"""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_NULL_LOCK = _NullLock()


class GiftProbeMixin:
    """给 fetcher 子类用的诊断 mixin(见模块 docstring)。

    ⚠️ 它**不覆盖**任何业务方法 —— `_parseGiftMsg` / `_on_gift` /
    `_parseChatMsg` / `_wsOnOpen` / `_wsOnClose` 的业务语义必须与生产逐字
    一致。诊断只在 `_probe_bump` 这个**纯计数**扩展点上叠加, 以及覆盖
    `_wsOnMessage` 的开头那一行做帧级打点。
    """

    #: 由 `attach_gift_probe()` 注入。
    _gift_probe_counters: Optional[ProfileCounters] = None
    _gift_probe_capture: Optional[CaptureProbe] = None
    _gift_probe_transport: Optional[TransportSummaryProbe] = None
    _gift_probe_semantic: Optional[GiftSemanticProbe] = None
    _gift_probe_lock: Optional[threading.Lock] = None
    #: 当前处理中的消息的 envelope msg_id(见 `_gift_probe_on_method`)。
    _gift_probe_pending_envelope: int = 0
    #: 当前处理中的消息的 payload。基线的 `_probe_bump` 只带 method 名,
    #: 所以 payload 要单独记住 —— 见 `_gift_probe_on_message`。
    _gift_probe_pending_payload: bytes = b""
    _gift_probe_pending_method: str = ""

    # ------------------------------------------------------------------
    def attach_gift_probe(self, *, profile, counters, store, semantic_dir,
                          interval_seconds: float = 25.0,
                          logger=None) -> None:
        """把三个探针装上。必须在 `start()` **之前**调用。

        线程安全: WS 回调线程与摘要线程会同时碰 counters, 所以所有对
        counters 的读改写都在 `_gift_probe_lock` 下 —— `+= 1` 不是原子
        操作, 漏锁会让计数随机少几条, 而少的那几条**没有任何症状**。
        """
        self._gift_probe_counters = counters
        self._gift_probe_capture = CaptureProbe(
            counters.profile_id, counters, store, logger=logger)
        self._gift_probe_transport = TransportSummaryProbe(
            counters.profile_id, counters,
            interval_seconds=interval_seconds, logger=logger)
        self._gift_probe_semantic = GiftSemanticProbe(
            semantic_dir, counters.profile_id, counters, logger=logger)
        self._gift_probe_lock = threading.Lock()

    # ---- 给 runner 读的只读出口 ---------------------------------------
    def gift_probe_summary(self) -> Optional[dict]:
        t = getattr(self, "_gift_probe_transport", None)
        if t is None:
            return None
        try:
            return t.summary()
        except Exception:                       # noqa: BLE001
            return None

    def log_gift_probe_summary(self, tag: str = "") -> None:
        """强制打一次摘要(连接关闭 / 进程退出时用)。"""
        t = getattr(self, "_gift_probe_transport", None)
        if t is None:
            return
        try:
            t.maybe_emit(force=True, extra=tag)
        except Exception:                       # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    def _gift_probe_on_message_start(self, msg) -> None:
        """基线分发循环处理**每一条**消息之前的钩子。

        必须在这里(而不是在 `_probe_bump` 里)拿到 payload 与 envelope:
        基线的 `_probe_bump("method_counts", _m)` 调用点只传 method 名,
        payload 在那个作用域里就取不到了。

        纯读操作 —— 只把两个值存进 pending, 不改任何东西。
        """
        c = getattr(self, "_gift_probe_counters", None)
        if c is None:
            return
        self._gift_probe_pending_method = str(getattr(msg, "method", "") or "")
        self._gift_probe_pending_payload = \
            getattr(msg, "payload", b"") or b""
        self._gift_probe_pending_envelope = \
            getattr(msg, "msg_id", 0) or 0

    # ------------------------------------------------------------------
    def _gift_probe_before_parse(self) -> None:
        """parse **之前**的落盘决策 —— 顺序约束见模块 docstring。

        放在这里而不是 parse 之后, 是因为"解析失败"的样本恰恰最该留下:
        先 parse 再落盘的话, 失败路径根本走不到落盘那一步。
        """
        cap = getattr(self, "_gift_probe_capture", None)
        c = getattr(self, "_gift_probe_counters", None)
        if cap is None or c is None:
            return
        method = self._gift_probe_pending_method
        try:
            fname = cap.observe_payload(
                self._gift_probe_pending_payload, method=method,
                connection_generation=c.connection_generation,
                envelope_msg_id=self._gift_probe_pending_envelope)
            if fname is None and cap.store.should_capture(method):
                with (getattr(self, "_gift_probe_lock", None) or _NULL_LOCK):
                    c.capture_skipped_count += 1
        except Exception:                       # noqa: BLE001
            pass

    def _gift_probe_on_parse_error(self) -> None:
        """handler 抛异常时留证据(Issue §E)。"""
        cap = getattr(self, "_gift_probe_capture", None)
        c = getattr(self, "_gift_probe_counters", None)
        if cap is None or c is None:
            return
        try:
            cap.note_parse_error(
                self._gift_probe_pending_payload,
                method=self._gift_probe_pending_method,
                connection_generation=c.connection_generation,
                envelope_msg_id=self._gift_probe_pending_envelope)
        except Exception:                       # noqa: BLE001
            pass

    def _gift_probe_after_parse_success(self, payload) -> None:
        """primary GiftMessage 的**业务解析路径**成功 —— 二层计数 + 语义落盘。

        调用点必须满足两个条件, 缺一不可:

        1. 该 method 就是 `WebcastGiftMessage`;
        2. 基线的 handler 已经**成功返回**(没有抛)。

        只有这两条同时成立, `parsed_as_primary_gift` 的含义才是确定的:
        "这条 Gift 的 proto 解析没有炸"。它一旦在 handler 之前推进, 那层
        判据就与 `parse_error_counts` 互相矛盾了。
        """
        c = getattr(self, "_gift_probe_counters", None)
        if c is None:
            return
        msg = _parse_gift_message(payload)
        if msg is None:
            # 基线 handler 没抛但探针解析不出来(理论上不该发生 —— 它们
            # 用同一个消息类)。不推进二层计数, 也不留语义。
            return
        try:
            with (getattr(self, "_gift_probe_lock", None) or _NULL_LOCK):
                c.parsed_gift_count += 1
            sem = getattr(self, "_gift_probe_semantic", None)
            if sem is not None:
                sem.record(msg,
                           envelope_msg_id=self._gift_probe_pending_envelope,
                           connection_generation=c.connection_generation)
        except Exception:                       # noqa: BLE001
            pass

    def _gift_probe_note_emitted_if_really_emitted(self, keep_all_before,
                                                   interaction_before) -> None:
        """在 handler 成功之后判断"业务回调**真的**被调用了吗"。

        ⚠️ 这里**不能**简单地"handler 没抛就算 emitted=1" —— 基线的
        `_parseGiftMsg` 有两个正交开关:

            if not (self.keep_all or self.interaction_enabled):
                return                       # <- 提前返回, 回调**没被调用**
            ...
            if self.interaction_enabled:
                self._on_gift(...)           # <- 只有这里才真的调了业务回调

        默认配置(`interaction_enabled=True`, `keep_all=False`)下两者一致,
        但诊断模式把两个开关都设为 False(它不接业务链), 那时"handler 成功"
        与"业务回调被调用"是**两件不同的事**。把它们混为一谈会让
        `emitted_gift_count` 在诊断模式下恒等于 `parsed_gift_count`,
        于是"parse 成功但 emitted=0"这条判据**永久失效** —— 而那正是本轮
        区分 callback/ingest 层故障的唯一手段。

        判据是开关的取值, 不是"事后猜": 基线里那一行 `if` 的条件就是它,
        这里复现同一个条件(Issue 要求三层计数可独立区分)。
        """
        c = getattr(self, "_gift_probe_counters", None)
        if c is None:
            return
        if not (keep_all_before or interaction_before):
            return                          # 基线提前返回, 回调没跑
        if not interaction_before:
            return                          # 只落库, 不接业务回调
        try:
            with (getattr(self, "_gift_probe_lock", None) or _NULL_LOCK):
                c.emitted_gift_count += 1
        except Exception:                       # noqa: BLE001
            pass

    def _gift_probe_record_semantics_only(self) -> None:
        """把一条 primary `WebcastGiftMessage` 的语义落进 JSONL, **不**推进二层计数。

        只在"基线没有注册这个 handler"时调用(诊断模式的常态)。

        ⚠️ 为什么它不推进 `parsed_gift_count`: 那个计数是 Issue §D 的
        **proto 层判据**("handled > 0, parse_error > 0 -> proto 层"), 它的
        分母是"真的走过业务解析路径的消息"。诊断模式下这些消息**根本没进**
        业务路径(handler 未注册), 把它们算进 proto 层判据会让那层判据
        失去意义 —— 它会显示"解析成功"而实际上业务链压根没碰过这条消息。

        所以这里只做**语义采样**: 让"诊断模式下收到的 Gift 长什么样"能被
        离线分析, 而不去污染那条用于归因的判据。两者的差别写进了
        `profile.py` 的字段注释, 也在测试里被钉住。
        """
        parsed = _parse_gift_message(self._gift_probe_pending_payload)
        if parsed is None:
            return
        c = getattr(self, "_gift_probe_counters", None)
        if c is None:
            return
        try:
            sem = getattr(self, "_gift_probe_semantic", None)
            if sem is not None:
                sem.record(
                    parsed,
                    envelope_msg_id=self._gift_probe_pending_envelope,
                    connection_generation=c.connection_generation)
        except Exception:                       # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    def _gift_probe_on_frame(self) -> None:
        """每收到一个 WS 帧调一次。**失败一律吞掉。**

        诊断探针是旁路。它抛异常不该让帧解析中断 —— 那等于"开了诊断就
        收不到弹幕了", 而症状看起来像平台的锅。
        """
        c = getattr(self, "_gift_probe_counters", None)
        if c is None:
            return
        try:
            lock = getattr(self, "_gift_probe_lock", None)
            with (lock or _NULL_LOCK):
                c.ws_frames += 1
                c.last_frame_at = _monotonic()
                if not c.first_frame_at:
                    c.first_frame_at = c.last_frame_at
            t = getattr(self, "_gift_probe_transport", None)
            if t is not None:
                t.maybe_emit()
        except Exception:                       # noqa: BLE001
            pass

    def _gift_probe_on_connection_open(self) -> None:
        """新一代连接: 更新代际号并**强制打一次**摘要。

        强制打是刻意的: `connection_generation` 是区分"重连前/后"的唯一
        手段, 而重连本身正是要观察的事件(抖音在重连后会重放消息)。只在
        计时器到点时才打的话, 一次短暂重连可能整段被跳过。
        """
        c = getattr(self, "_gift_probe_counters", None)
        if c is None:
            return
        try:
            c.connection_generation = int(
                getattr(self, "connection_generation", 0) or 0)
            t = getattr(self, "_gift_probe_transport", None)
            if t is not None:
                t.maybe_emit(force=True, extra="connection_open")
        except Exception:                       # noqa: BLE001
            pass


def _monotonic() -> float:
    import time
    return time.monotonic()


def _parse_gift_message(payload):
    """尽力把 payload 解析成 `GiftMessage`; 失败返回 None。

    与基线 `_parseGiftMsg` 用**同一个**消息类, 但**不复用**它的调用 ——
    基线那次调用可能因为 `keep_all` / `interaction_enabled` 都为 False 而
    提前返回(见 `DanmakuFetcher._parseGiftMsg` 的第一个 `if`)。
    诊断模式恰恰需要在那种配置下也能看到语义, 所以这里独立解析一次。

    代价: 一条 Gift 几十微秒。诊断模式本来就不是生产吞吐路径, 换来的好处
    是探针与业务开关**完全解耦** —— 不会出现"因为我没开 keep_all, 所以
    诊断也没数据"。
    """
    try:
        from protobuf.douyin import GiftMessage
        return GiftMessage().parse(payload)
    except Exception:                           # noqa: BLE001
        return None


def make_gift_capture_fetcher(base_cls) -> type:
    """构造一个"基线 + 诊断"的 fetcher 类。

    ⚠️ 这里**动态建类**而不是静态写一个子类, 是为了让 `base_cls` 可以是
    `CallbackFetcher`(接真实业务回调)也可以是 `DanmakuFetcher`(纯落库),
    而**基线那两个类都不用被改**。诊断能力是叠加的, 不是分叉的。

    覆写的只有 `_wsOnMessage` / `_wsOnOpen` / `_wsOnClose` / `_on_gift` /
    `_probe_bump`。其中每一个都是"先做基线那件事, 再做诊断那件事", 没有
    一处改变基线的判定条件。
    """

    class GiftCaptureFetcher(GiftProbeMixin, base_cls):    # type: ignore
        """基线 fetcher + gift 诊断探针。"""

        # ---- 帧级 + 逐条消息的 pending 记录 ----
        #
        # 为什么必须覆写 `_wsOnMessage`: 基线调
        # `_probe_bump("method_counts", _m)` 时只传 method 名, 而 payload
        # 与 envelope 在那个作用域里已经取不到了。落盘需要它们, 所以要在
        # **基线的分发循环开始之前**把每条 `msg` 记进 pending。
        #
        # ⚠️ 这里刻意**重放**基线那段分发循环, 而不是"调用基线再补一份
        # 循环" —— 后者会对同一条消息调两次 handler, 那是行为改变(弹幕会
        # 被处理两次)。重放的那段逻辑与基线**逐行对应**, 并由
        # `tests/test_gift_probe.py` 的计数一致性断言钉住(同样的响应用
        # 基线类与诊断类各跑一遍, method/unhandled/parse_error 计数必须
        # 完全相等)。
        def _wsOnMessage(self, ws, message):
            # 基线 `_wsOnMessage` 的**第一句**就是 `_probe_bump("ws_frame_count")`。
            # 这里必须逐字复现: 少它一句, 诊断模式下 `ws_frames` 会恒为 0
            # —— 而"连接还在推帧吗"正是判活的第一手证据。
            self._probe_bump("ws_frame_count")
            self._gift_probe_on_frame()
            package = PushFrame().parse(message)
            response = Response().parse(gzip.decompress(package.payload))
            setattr(self, "ws_message_count",
                    getattr(self, "ws_message_count", 0)
                    + len(response.messages_list))
            c = getattr(self, "_gift_probe_counters", None)
            if c is not None:
                with (getattr(self, "_gift_probe_lock", None) or _NULL_LOCK):
                    c.ws_messages += len(response.messages_list)

            if response.need_ack:
                ack = PushFrame(
                    log_id=package.log_id,
                    payload_type="ack",
                    payload=response.internal_ext.encode("utf-8"),
                ).SerializeToString()
                ws.send(ack, websocket.ABNF.OPCODE_BINARY)

            _ENVELOPE_AWARE = {self._parseLikeMsg, self._parseGiftMsg}
            handlers = {
                "WebcastChatMessage": self._parseChatMsg,
                "WebcastControlMessage": self._parseControlMsg,
            }
            if self.keep_all or self.interaction_enabled:
                handlers.update({
                    "WebcastGiftMessage": self._parseGiftMsg,
                    "WebcastLikeMessage": self._parseLikeMsg,
                })
            if self.keep_all:
                handlers.update({
                    "WebcastMemberMessage": self._parseMemberMsg,
                    "WebcastSocialMessage": self._parseSocialMsg,
                    "WebcastEmojiChatMessage": self._parseEmojiChatMsg,
                })

            for msg in response.messages_list:
                _m = msg.method
                self._gift_probe_on_message_start(msg)
                # 落盘决策必须在 handler **之前** —— 见模块 docstring。
                self._gift_probe_before_parse()
                self._probe_bump("method_counts", _m)
                fn = handlers.get(_m)
                # ⚠️ 探针的解析**必须在** `unhandled` 分支**之前**判断。
                #
                # 基线那行 `continue` 是为"没有 handler 的类型一律忽略"
                # 写的, 而诊断模式恰恰把 Gift 的业务开关**都关了**
                # (`keep_all=False` + `interaction_enabled=False`, 因为它
                # 不接业务链)—— 于是 `WebcastGiftMessage` 在诊断模式下
                # **正好**会走进 unhandled 分支。若把探针的解析放在
                # `continue` 之后, 诊断就会"只要关了业务开关就什么都看不
                # 到", 而那正是本轮要在生产配置下观察的东西。
                #
                # 所以顺序是: 先计数(与基线一致) -> 探针独立解析(与开关
                # 解耦) -> 再按基线的规则决定要不要调 handler。
                if _m == "WebcastGiftMessage":
                    self._gift_probe_record_semantics_only()
                if fn is None:
                    self._probe_bump("unhandled_method_counts", _m)
                    continue
                # ⚠️ 基线的行为是: handler 抛异常 -> **吞掉**, 只在 stderr
                # 打一行。这里必须逐字复现 —— 把异常放出去会直接杀死 WS
                # 回调线程, 那是对生产行为的实质改变(见 GP-13 的对照断言)。
                try:
                    payload = self._gift_probe_pending_payload
                    if fn in _ENVELOPE_AWARE:
                        fn(payload,
                           envelope_msg_id=self._gift_probe_pending_envelope)
                    else:
                        fn(payload)
                except Exception as e:          # noqa: BLE001
                    self._probe_bump("parse_error_counts", _m)
                    print(f"!!! 解析 {msg.method} 失败: "
                          f"{type(e).__name__}: {e}",
                          file=sys.stderr, flush=True)
                    continue

                # ---- 到这里说明 handler 成功返回了 ----
                #
                # 第二层: **业务解析路径**成功了 —— `parsed_as_primary_gift`。
                # 与 `parse_error_counts` 一起构成 Issue §D 的 proto 层判据。
                # 放在 handler **之后**是必须的: 放在之前的话, "handler 抛
                # 异常"的样本会同时呈现 parsed=1 与 parse_error=1, 两层判据
                # 互相矛盾。
                #
                # 第三层: 业务回调**真的**被调用了吗? 按基线那两个开关判 ——
                # 见 `_gift_probe_note_emitted_if_really_emitted` 的说明。
                if _m == "WebcastGiftMessage":
                    self._gift_probe_after_parse_success(
                        self._gift_probe_pending_payload)
                    self._gift_probe_note_emitted_if_really_emitted(
                        keep_all_before=bool(self.keep_all),
                        interaction_before=bool(self.interaction_enabled))

        def _wsOnOpen(self, ws):
            r = super()._wsOnOpen(ws)
            self._gift_probe_on_connection_open()
            return r

        def _wsOnClose(self, ws, *args):
            self.log_gift_probe_summary("断开时")
            return super()._wsOnClose(ws, *args)

        def start(self):
            """启动连接。

            ⚠️ 必须覆盖: 基线 `DanmakuFetcher.start()` 的**第一句**是
            `self._fp = open(self.out_path, "a")` —— 诊断模式没有
            production 落库路径(`out_path = ""`), 那行会直接
            `FileNotFoundError: ''` 把整路连接起不来。

            更糟的是它还会**悄悄换掉**我们装的 `_NoProductionSink`: 即使
            `out_path` 指向一个临时文件能打开, 诊断也会开始往那个文件写
            production 格式的弹幕行 —— 那就等于把诊断数据写进了 production
            数据的形状里, 而 Issue 测试点 10 明确要求两者不相交。

            所以这里在调用基线 start **之前**把 `out_path` 换成一个可丢弃
            的临时路径, 并在 finally 里还原。基线那行 open 于是照常执行
            (不改变它的任何假设), 但写进去的内容落在临时文件里, 而那个
            文件在退出时被删掉 —— 既不进 `data/`, 也不留残留。
            """
            import tempfile
            from .runner import _NoProductionSink

            sink = self._fp if isinstance(self._fp, _NoProductionSink) \
                else None
            saved = self.out_path
            tmp = None
            try:
                fd, tmp = tempfile.mkstemp(prefix="giftprobe_sink_",
                                           suffix=".jsonl")
                os.close(fd)
                self.out_path = tmp
                return super().start()
            finally:
                self.out_path = saved
                # 基线已经在 finally 里关掉了它自己打开的那个句柄; 这里把
                # 临时文件删掉, 并**恢复**我们的 sink —— 恢复是必须的:
                # 基线 start() 的 finally 把 `self._fp` 关掉之后, 后续
                # `_emit` 若再被调用会写到已关闭的文件上。
                if sink is not None:
                    self._fp = sink
                if tmp:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass

        # ---- 纯计数扩展点: 基线之外只加"诊断自己的那本账" ----
        def _probe_bump(self, attr, key=None):
            r = super()._probe_bump(attr, key)
            c = getattr(self, "_gift_probe_counters", None)
            if c is None:
                return r
            if attr == "method_counts" and key is not None:
                with (getattr(self, "_gift_probe_lock", None) or _NULL_LOCK):
                    c.bump_method(str(key))
            elif attr == "unhandled_method_counts" and key is not None:
                with (getattr(self, "_gift_probe_lock", None) or _NULL_LOCK):
                    c.bump_unhandled(str(key))
            elif attr == "parse_error_counts" and key is not None:
                with (getattr(self, "_gift_probe_lock", None) or _NULL_LOCK):
                    c.bump_parse_error(str(key))
                self._gift_probe_on_parse_error()
            return r

    GiftCaptureFetcher.__name__ = f"GiftCapture{base_cls.__name__}"
    GiftCaptureFetcher.__qualname__ = GiftCaptureFetcher.__name__
    return GiftCaptureFetcher
