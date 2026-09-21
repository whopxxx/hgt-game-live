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

from .probe import (CaptureProbe, GiftSemanticProbe, TransportSummaryProbe,
                    sanitize_method)
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


def _reference_bootstrap(fetcher, now_ms: int) -> dict:
    """`reference-2026` 臂的 `cursor` / `internal_ext`。

    ## 为什么需要单独一条路径(而不是直接复用生产生成器)

    本项目的生产生成器(`vendor/.../ws_bootstrap.generate_ws_bootstrap`)
    **本来就已经**是照公开参考实现 `JaneEyre3007/douyin-js` 的
    `genCursorInternalExt` 写的 —— 形状一模一样。所以"C 只是换个标签"完全
    没有意义: 真实直播里 B/C 都会失败, 而我们无法区分"reference 方案无效"
    与"reference 方案根本没跑"。

    这一臂真正复现的是参考实现里**随连接变化**的那组身份/时间字段, 特别是
    `wss_push_did`:

        A(current-auth-control)  did 是全场固定的生产常量
        B(random-uid-only)       did 每连接随机, 但用的是生产生成器
        C(reference-2026)        did 每连接随机, 且**显式**走这条 reference
                                 路径(时间字段取本次连接的 fresh now_ms)

    A 与 C 在 `wss_push_did` 上的差异是**可被 URL 断言验证**的 —— 这正是
    `tests/test_gift_probe.py` 里那条"抓真实 WSS URL"的测试所钉住的东西。

    ## 与生产生成器的实际差别

    参考实现在 `internal_ext` 里带 `internal_src:dim`, 生产生成器同样如此
    (它就是这么写的)。两者在**结构上等价**, 差异只在身份与时间的取值方式。
    所以本函数:

    1. 用**本 profile 的 uid**(A 固定 / C 随机)填 `wss_push_did`;
    2. 用**本次调用的 now_ms**(基线每次连接新取)填所有时间字段;
    3. 其余字段与生产生成器逐字一致 —— 不做任何没有证据支撑的协议猜测。

    ## 边界

    ⚠️ 这**不是**抖音官方协议, 也**不是**参考实现的逐字移植。参考实现的具体
    取值无法在此刻离线验证, 所以这里只对齐**可观察到的结构性质**: 身份字段
    随连接变化 + 时间字段是 fresh 的。

    本轮能证明的是"C 与 B 在连接参数上真的不同, 且 C 确实跑过"; 至于 C 是否
    更接近平台, 那是真实直播验收要回答的事, 不是这里能断言的。

    room / host / signature / handler / proto 一概不动。
    """
    from ws_bootstrap import generate_ws_bootstrap

    uid = str(getattr(fetcher, "user_unique_id", "") or "")
    room = _safe_room_id(fetcher)
    # 参考实现的 `wss_push_did` 用的是**它自己**连接级的身份, 而不是
    # 生产那份固定的 `user_unique_id`。这正是 A 与 C 在 URL 上的真实差异点:
    # A 的 did 是全场常量, C 的 did 每连接变。
    #
    # 取值方式(重要): 参考实现是"每次连接生成一个新的会话身份"。这里用
    # **本次连接的 now_ms + uid** 派生一个稳定的会话级 id —— 它随连接变化,
    # 但在同一次连接内可复现(测试能确定性验证), 也**不引入新的随机源**。
    did = f"{uid}{now_ms}" if uid else str(now_ms)

    base = generate_ws_bootstrap(room_id=room, user_unique_id=did,
                                 now_ms=now_ms)
    return {
        "cursor": base["cursor"],
        "internal_ext": base["internal_ext"],
        "reference_did": did,
    }


def _safe_room_id(fetcher) -> str:
    """取真实房间号, 只读**已缓存**的值。

    ⚠️ 诊断探针绝不能因为"房间号还没解析出来"就把整路连接炸掉 —— 而
    `room_id` 是个 property, 未解析时它会去打 HTTP(可能超时/被风控)。
    这里只读私有缓存字段, 不触发网络; 拿不到就交给生成器退化成空串。

    ⚠️ 但**走到这一步时房间号必然已经解析过了**: `_connectWebSocket` 在
    调 `_local_bootstrap` 之前就用 `self.room_id` 拼过 URL。所以正常情况下
    这里一定能拿到值 —— 拿不到说明调用顺序变了, 那才是需要注意的信号
    (因此空串不是"静默成功", 而是一个可见的异常形状)。
    """
    try:
        raw = fetcher.__dict__.get("_DouyinLiveWebFetcher__room_id")
        if raw:
            return str(raw)
        # 兜底: 有些测试会直接注入 `room_id` 这个公开属性。
        raw = getattr(fetcher, "room_id", None)
        return str(raw) if raw else ""
    except Exception:                           # noqa: BLE001
        return ""


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
                          logger=None, bootstrap_mode: str = None) -> None:
        """把三个探针装上。必须在 `start()` **之前**调用。

        线程安全: WS 回调线程与摘要线程会同时碰 counters, 所以所有对
        counters 的读改写都在 `_gift_probe_lock` 下 —— `+= 1` 不是原子
        操作, 漏锁会让计数随机少几条, 而少的那几条**没有任何症状**。

        `bootstrap_mode` 决定 `_local_bootstrap` 覆写走哪条路径(见那条
        方法的说明)。**默认从 profile 取**, 调用方通常不必显式传 —— 但
        显式参数存在是有意的: 它让"这一路的 bootstrap 是哪一种"在调用点
        就看得见, 而不是散落在 profile 元数据里等着被忘记接线(Blocker 1
        就是这么发生的)。
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
        # ---- Blocker 1: bootstrap 分派必须真的接上 ----
        if bootstrap_mode is None:
            bootstrap_mode = getattr(profile, "bootstrap", None)
        from .profile import BOOTSTRAP_LOCAL
        self._gift_probe_bootstrap_mode = (
            str(bootstrap_mode) if bootstrap_mode else BOOTSTRAP_LOCAL)

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
        """primary GiftMessage 的**解析路径**成功 —— 推进二层计数。

        调用点必须满足两个条件, 缺一不可:

        1. 该 method 就是 `WebcastGiftMessage`;
        2. handler 已经**成功返回**(没有抛)。

        只有这两条同时成立, `parsed_as_primary_gift` 的含义才是确定的:
        "这条 Gift 的 proto 解析没有炸"。它一旦在 handler 之前推进, 那层
        判据就与 `parse_error_counts` 互相矛盾了。

        ⚠️ 这里**只**推进计数, 不写语义 JSONL —— 语义由链路末端的干跑
        回调(`_gift_probe_dry_run_gift`)写一次。两处都写会让每条 Gift 在
        `gift_semantic.jsonl` 里出现两遍, 而下游按行分析时会把它读成"收到
        了两次礼物"。计数与语义分开还有一个好处: `parsed` 描述"解析成功了
        N 条", `emitted` 描述"其中 M 条走到了回调", 两者口径不同。
        """
        c = getattr(self, "_gift_probe_counters", None)
        if c is None:
            return
        # 解析一次以确认"这条真的能解析"(而不是 handler 恰好没抛)。
        if _parse_gift_message(payload) is None:
            return
        try:
            with (getattr(self, "_gift_probe_lock", None) or _NULL_LOCK):
                c.parsed_gift_count += 1
        except Exception:                       # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # Blocker 2: 诊断连接自己的 **dry-run 业务路径**
    # ------------------------------------------------------------------
    #
    # ## 问题
    #
    # 诊断连接**不能**接真业务回调(否则会污染 SummonLedger / Engine / UI
    # —— Issue 的 non-goals)。但早先的实现把 `keep_all` 与
    # `interaction_enabled` 都设成 False, 于是基线**根本不注册**
    # `WebcastGiftMessage` handler:
    #
    #     收到 Gift -> handlers.get() 返回 None -> unhandled -> verdict=dispatch
    #
    # 结果真实 runner 永远只能回答"服务端有没有给 Gift-family", 答不了
    # Issue #17 要的第 3~5 问(dispatch? proto? callback/ingest?)。而测试
    # 之所以绿, 是因为它构造了**另一套**配置(`interaction_enabled=True`)
    # —— 测试路径与真实诊断路径不一致, 又是一次假绿。
    #
    # ## 修法
    #
    # 给诊断连接一条**自己的**干跑链路: 注册 `WebcastGiftMessage` handler,
    # 让它走真实的 parse, 再把结果交给一个**只写诊断 sink** 的回调。这条
    # 链路的终点是我们的探针, **不接** Engine / Summon / inbox /
    # 任何业务状态。
    #
    # 三层计数于是全部由**同一条真实路径**产生:
    #
    #     gift_method_seen      分发到 handler(真实注册, 不再靠 unhandled)
    #     parsed_gift_count     handler **成功**返回(基线那句 GiftMessage().parse)
    #     parse_error_counts    handler 抛异常(真实的 proto 解析失败)
    #     emitted_gift_count    干跑回调真的被调用且**返回**(链路末端)

    def _gift_probe_parse_gift_dry_run(self, payload, envelope_msg_id=0
                                       ) -> None:
        """诊断用的 primary GiftMessage handler: 真解析 + 干跑回调。

        与基线 `DanmakuFetcher._parseGiftMsg` 的差别**只有两点**:

        1. 它**不**看 `keep_all` / `interaction_enabled` —— 诊断路径与业务
           开关解耦, 这是"诊断能在生产配置下工作"的前提;
        2. 它**不落库**到 production 弹幕文件, 也不发 InteractionEvent。

        解析用**同一个** `GiftMessage` 消息类(与基线同源), 所以
        "解析成功/失败"的口径与生产一致 —— 否则 parse_error 计数就不再
        描述生产的 proto 行为。

        异常**原样上抛**: 基线 `_wsOnMessage` 的 except 分支会把它记进
        `parse_error_counts` 并在 stderr 打一行 —— 那正是我们要的 proto 层
        证据。在这里吞掉就等于把这一层判据抹掉。
        """
        from protobuf.douyin import GiftMessage
        m = GiftMessage().parse(payload)
        self._gift_probe_dry_run_gift(m, envelope_msg_id=envelope_msg_id)

    def _gift_probe_dry_run_gift(self, m, envelope_msg_id=0) -> None:
        """干跑回调 —— 诊断链路的终点。

        ⚠️ 它**只**做两件事: 写语义 JSONL、推进 `emitted_gift_count`。
        绝不触碰 Engine / SummonLedger / inbox / Web / 任何业务状态。

        之所以做成一个真的回调(而不是在 handler 里顺手计数), 是为了让
        `emitted` 代表**同一条链路**的末端 —— 与生产
        `CallbackFetcher._on_gift` 的位置一一对应。这样"parse 成功但
        emitted=0"在诊断里的含义, 与生产里"解析成功但业务回调没收到"是
        同一件事; 否则这个计数只是"又数了一遍 parsed"。
        """
        c = getattr(self, "_gift_probe_counters", None)
        if c is None:
            return
        try:
            sem = getattr(self, "_gift_probe_semantic", None)
            if sem is not None:
                sem.record(m, envelope_msg_id=envelope_msg_id,
                           connection_generation=c.connection_generation)
            with (getattr(self, "_gift_probe_lock", None) or _NULL_LOCK):
                c.emitted_gift_count += 1
        except Exception:                       # noqa: BLE001
            pass

    def _gift_probe_register_dry_run_handlers(self, handlers: dict) -> None:
        """把 `WebcastGiftMessage` 指向**干跑** handler。

        只覆盖 Gift 一个 method。其余(chat / like / member / ...)保持诊断
        模式的静默 —— 它们与本轮要回答的问题无关, 让它们进业务链只是多
        一份风险。

        覆盖是**幂等**的: handler 表每帧重建, 所以这里每帧调一次也不会
        累积。
        """
        handlers["WebcastGiftMessage"] = self._gift_probe_parse_gift_dry_run

    def _gift_probe_record_semantics_only(self) -> None:
        """把一条 primary `WebcastGiftMessage` 的语义落进 JSONL, **不**推进二层计数。

        ⚠️ 这是**兜底**路径: 只有当 baseline 的 handler 表里**没有**我们
        注册的干跑 handler 时才会走到(见 `_wsOnMessage` 里的分派)。正常
        诊断模式下走的是 `_gift_probe_parse_gift_dry_run`, 三层计数都由那
        条真实路径产生。

        保留它是因为"handler 没被注册"这件事本身是个需要被观察到的信号
        (比如将来有人改了 handler 注册条件)。那时至少语义样本仍在, 不会
        变成"什么都看不到"。
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

            # ⚠️ 干跑 handler 必须也在 `_ENVELOPE_AWARE` 里。
            #
            # 基线是用**实例方法对象**比对这个集合来决定要不要传
            # `envelope_msg_id=`。我们的 handler 不在集合里的话, 它会被
            # 当成"签名不含该参数"按位置调用 —— 于是 Issue §G 要求的
            # `envelope_msg_id` 在语义 JSONL 里恒为空串, 而 `common_msg_id`
            # 正常, 看起来像"服务端这条没给 envelope"(假信号)。
            _ENVELOPE_AWARE = {self._parseLikeMsg, self._parseGiftMsg,
                               self._gift_probe_parse_gift_dry_run}
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
            # ---- Blocker 2: 诊断连接自己的干跑 Gift 链路 ----
            #
            # ⚠️ 这一句是**整条诊断链能不能分层的关键**。
            #
            # 诊断模式把 `keep_all` 与 `interaction_enabled` 都设为 False
            # (它不接业务链), 所以上面那两个 if **都不会**注册
            # `WebcastGiftMessage`。没有这一句, 每一份礼物都会掉进
            # `unhandled` 分支 -> `verdict()` 恒为 `dispatch` -> proto 层
            # 与 callback 层**永远不可达**。
            #
            # 这里显式把 Gift 指向我们自己的 handler(解析 -> 只写诊断
            # sink)。它**替代**了基线那个受开关约束的注册, 但只针对 Gift
            # 一个 method, 且终点不接任何业务状态。
            self._gift_probe_register_dry_run_handlers(handlers)

            for msg in response.messages_list:
                _m = msg.method
                self._gift_probe_on_message_start(msg)
                # 落盘决策必须在 handler **之前** —— 见模块 docstring。
                self._gift_probe_before_parse()
                self._probe_bump("method_counts", _m)
                fn = handlers.get(_m)
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
                    # ⚠️ **不能**打 `{e}`。这条链上的异常正文可能带请求
                    # 上下文(而请求头里就有登录 Cookie), 而 stderr 会进
                    # 直播日志/截图。基线打印原文是它历史行为, 但诊断
                    # 连接是我们**新增**的路径, 没有理由继承那条风险。
                    #
                    # 这里打 method 名(已归一化)+ 异常**类型名** —— 类型名
                    # 来自类定义、不是运行时数据, 排障够用且结构上不可能
                    # 携带凭据。
                    print(f"!!! 解析 {sanitize_method(msg.method)} 失败: "
                          f"{type(e).__name__}",
                          file=sys.stderr, flush=True)
                    continue

                # ---- 到这里说明 handler 成功返回了 ----
                #
                # 第二层: **解析路径**成功了 —— `parsed_as_primary_gift`。
                # 与 `parse_error_counts` 一起构成 Issue §D 的 proto 层判据
                # ("handled > 0, parse_error > 0 -> proto 层")。
                #
                # ⚠️ 必须放在 handler **之后**: 放在之前的话, "handler 抛
                # 异常"的样本会同时呈现 parsed=1 与 parse_error=1, 两层判据
                # 互相矛盾, 结论就不可信了。
                #
                # 第三层(emitted)不在这里推进 —— 它由**干跑回调**在链路
                # 末端推进(见 `_gift_probe_dry_run_gift`)。这样 `emitted`
                # 的含义是"这条消息真的走到了业务回调的位置", 而不是
                # "handler 返回了"(那是 parsed 的含义)。
                if _m == "WebcastGiftMessage":
                    self._gift_probe_after_parse_success(
                        self._gift_probe_pending_payload)

        def _local_bootstrap(self, now_ms: int) -> dict:
            """按**本路 profile** 生成 `cursor` / `internal_ext`。

            ⚠️ 这个覆写是 blocker-1 的修复点, 不是锦上添花。

            基线 `liveMan._local_bootstrap()` 对每一路都无条件用同一套参数
            生成 bootstrap。我们此前只把 profile 的 `bootstrap` 记在元数据
            里、**没有接进连接层**, 于是运行时:

                A = 固定 uid + local bootstrap
                B = 随机 uid + local bootstrap
                C = 随机 uid + local bootstrap   <-- 与 B 完全相同

            也就是说 `reference-2026` 这一臂**根本没有运行过**。真实直播里
            若 C 也没收到 Gift, 我们会得出"reference 方案无效"的**错误结论**
            —— 而它压根没被测过。这是本轮最危险的一类假绿。

            修法上刻意**不动生产**: 基线方法原样保留, 诊断子类只在**自己**
            身上按 profile 分派。`BOOTSTRAP_LOCAL` 走 `super()`, 所以 A/B
            两臂与生产逐字同源;C 才走 reference 形状。

            两臂都用**本次调用的 now_ms**(由基线传入, 每次连接新取)——
            "fresh timestamp" 对 B/C 都成立, 这正是 Issue 要求 3。
            """
            mode = getattr(self, "_gift_probe_bootstrap_mode", None)
            if mode == "reference":
                return _reference_bootstrap(self, now_ms)
            # A/B: 逐字走生产路径。
            return super()._local_bootstrap(now_ms)

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
