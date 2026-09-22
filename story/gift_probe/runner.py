#!/usr/bin/env python
# coding: utf-8
"""`gift_capture_diagnostic` 模式 —— 多连接画像的抓取诊断(Step 12C)。

## 它是什么

一个**显式的诊断模式**(不是默认行为): 同时开 1~3 路参数受控的 WS 连接,
每路把证据写进**自己独占**的目录, 并每 20~30 秒打一次结构化摘要。

## 它**不是**什么(与 Issue non-goals 逐条对齐)

- 不动正常问答 / Like / Summon 的业务语义 —— 诊断连接**不接业务回调**,
  它收到的礼物不会进 Director / Engine / SummonLedger;
- 不把 Gift 接到 UI, 不做感谢公告、不做打赏榜、不做钻石换算;
- 不改变 production 数据路径 —— 每路只写 `data/gift_probe/<session>/<profile>/`;
- **不因为一路失败而杀死别路或主直播** —— 见下面"故障隔离"。

## 故障隔离(这是本轮可用性的核心)

三路连接各自跑在自己的线程里, 任何一路的异常都只影响它自己:

    for p in profiles: 启动线程(互不 join, 互不阻塞)
    任一路抛异常 -> 该路标记 failed, 其余两路照跑

显式**不**做的事: 没有"全部成功才算成功"的同步点, 也没有 `join()` 一个
会阻塞的循环。真实直播里最靠不住的就是网络, 而一个诊断工具最不该有的
行为就是"因为没连上, 把正在跑的直播搞崩"。

## Cookie 安全

凭据**只**在这一层从 Config 取一次, 然后立刻交给 transport (WS handshake)
与 `profile_auth_state()` 做**布尔**判断, 之后不再被本模块引用。本模块
打的每一条日志都只含 `auth=authenticated` / `auth=anonymous` 两个词之一
与 `config_state` —— 没有长度、hash、前缀、cookie name 列表。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Optional

from .capture import (
    DEFAULT_MAX_PER_METHOD,
    DEFAULT_MAX_TOTAL_BYTES,
    DEFAULT_MAX_TOTAL_FILES,
    DEFAULT_PROBE_ROOT,
    RawCaptureStore,
    _session_id_component,
    assert_outside_production_paths,
)
from .hooks import make_gift_capture_fetcher
from .profile import (
    ProfileCounters,
    default_profiles,
    format_profile_log,
    profile_auth_state,
    select_profiles,
)

log = logging.getLogger("story.gift_probe.runner")

#: 摘要间隔。Issue 建议 20~30 秒 —— 取 25: 快到能在送完礼几十秒内看到结论,
#: 又不至于让日志被摘要淹掉。
DEFAULT_SUMMARY_INTERVAL_SECONDS = 25.0


def _new_session_id(now: float) -> str:
    """`YYYYmmdd-HHMMSS` 形式的 session id。

    不用随机数 / uuid: 现场排查时人是按**时间**找目录的("刚才那次测试是
    几点"), 而 uuid 让 `ls` 的输出完全无法定位。
    """
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(now))


class GiftProbeArm:
    """一路诊断连接: 一个 profile + 一套独占的计数器 / 目录 / 探针。

    ⚠️ **每路一个实例, 各自持有自己的 `ProfileCounters` 与
    `RawCaptureStore`** —— Issue 的 mutation verification 专门要求
    "故意让两个 profile 共用 counter -> 隔离测试必须失败"。隔离必须体现
    在**数据结构**上(各自 new 一个), 而不是靠"记得在每个地方带上
    profile 名"这种纪律。
    """

    def __init__(self, profile, cfg, session_id: str, *,
                 probe_root: str = DEFAULT_PROBE_ROOT,
                 max_per_method: int = DEFAULT_MAX_PER_METHOD,
                 max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
                 max_total_files: int = DEFAULT_MAX_TOTAL_FILES,
                 summary_interval_seconds: float =
                 DEFAULT_SUMMARY_INTERVAL_SECONDS,
                 now_fn=None):
        self.profile = profile
        self.cfg = cfg
        self.session_id = session_id
        self.probe_root = probe_root
        self.now_fn = now_fn or time.time
        # 凭据只在这里取一次, 之后只用于布尔判断。
        self._login_cookie = getattr(cfg, "douyin_live_cookie", None)
        auth_state = profile_auth_state(profile, self._login_cookie)
        self.counters = ProfileCounters(
            profile.profile_id,
            auth=auth_state["auth"],
            config_state=auth_state["config_state"],
            # Blocker 1: 把**实际会生效**的 bootstrap 模式记进 counters。
            # 它是 arm 与 fetcher 之间唯一的一致性凭证 —— 测试与摘要有它
            # 才能断言"reference 真的跑了", 而不是"profile 上写着 reference"。
            bootstrap_mode=profile.bootstrap)
        self.store = RawCaptureStore(
            probe_root, session_id, profile.profile_id,
            max_per_method=max_per_method,
            max_total_bytes=max_total_bytes,
            max_total_files=max_total_files,
            now_fn=self.now_fn)
        self.semantic_dir = self.store.dir
        self.summary_interval_seconds = summary_interval_seconds
        self.fetcher = None
        self.thread: Optional[threading.Thread] = None
        self.error: str = ""
        self.started_at = 0.0
        self.stopped_at = 0.0

    # ------------------------------------------------------------------
    def auth_state(self) -> dict:
        return profile_auth_state(self.profile, self._login_cookie)

    # ------------------------------------------------------------------
    def build_fetcher(self, base_cls):
        """构造这一路的 fetcher(诊断探针已装上)。

        每路**独立**构造自己的 `user_unique_id`:
        `profile.user_unique_id()` 在 random 模式下每次调用都产生新值 ——
        这正是 B/C 两臂与 A 臂的唯一区别。

        ⚠️ `user_unique_id` **不进日志**: 它是客户端身份标识, 虽然不如
        Cookie 敏感, 但本轮只需要知道"固定 vs 随机"这个**模式**, 具体值
        对结论没有贡献, 而写进日志就多一处可被指纹化的东西。需要具体值时
        从 WS URL 与 probe 目录里取。
        """
        f = make_diagnostic_fetcher(
            self.cfg, self.profile.user_unique_id(), base_cls=base_cls)
        f.attach_gift_probe(
            profile=self.profile, counters=self.counters, store=self.store,
            semantic_dir=self.semantic_dir,
            interval_seconds=self.summary_interval_seconds,
            logger=log)
        self.fetcher = f
        return f

    # ------------------------------------------------------------------
    def start(self, base_cls) -> threading.Thread:
        """在本路自己的线程里跑。**任何异常都只标记自己失败。**"""
        self.started_at = self.now_fn()

        def _run():
            try:
                f = self.build_fetcher(base_cls)
                f.start()
            except Exception as e:              # noqa: BLE001
                # ⚠️ 只记类型 + 一行截断消息, **不记完整 traceback**:
                # traceback 会带上局部变量, 而这条链上有一个局部变量就是
                # 登录 Cookie(见 liveMan 的 `_build_ws_cookie_header`)。
                # 为了一条诊断日志把凭据写进日志文件, 是不可接受的代价。
                self.error = _safe_error(e)
                # ⚠️ 这里**不**改 `counters.config_state`。
                #
                # 早先想的是"连不上 -> config_invalid", 那是错的:
                # `config_state` 回答的是"**连接配置**能不能用"这个问题
                # (Cookie 配了却解析不出任何 pair), 因此它必须在连接尝试
                # **之前**就确定。把连接期的失败也塞进同一个字段, 会让
                # "配的 Cookie 是坏的"与"网络断了"这两种要查的地方完全
                # 不同的故障在日志里长得一模一样 —— 而那正是本轮要避免的
                # 那类不可归因的观测。
                #
                # 连接失败的痕迹有两个独立出口: `error` 字段(本行)与
                # `verdict=transport_or_auth`(Gift 计数为 0)。
                log.warning("gift probe 路 %s 退出: %s",
                            self.profile.profile_id, self.error)
            finally:
                self.stopped_at = self.now_fn()

        self.thread = threading.Thread(
            target=_run, name=f"gift-probe-{self.profile.profile_id}",
            daemon=True)
        self.thread.start()
        return self.thread

    # ------------------------------------------------------------------
    def summary(self) -> dict:
        s = self.counters.summary()
        s["verdict"] = self.counters.verdict()
        s["error"] = self.error
        s["store"] = self.store.stats()
        return s

    def log_line(self) -> str:
        return format_profile_log(self.profile, self.auth_state())


#: 允许出现在日志/摘要里的**错误类别**。闭集 —— 见 `_safe_error`。
ERR_HANDSHAKE = "handshake_failed"
ERR_RESOLVE = "room_resolve_failed"
ERR_NETWORK = "network_error"
ERR_TIMEOUT = "timeout"
ERR_AUTH = "auth_error"
ERR_OS = "os_error"
ERR_VALUE = "value_error"
ERR_UNKNOWN = "unknown_error"


def _error_category(e: BaseException) -> str:
    """异常 -> 一个**固定类别词**。

    ⚠️ 这份映射是 `_safe_error` 的核心, 不是"给异常起个好听名字"。

    之前那版 `_safe_error` 用 `f"{type(e).__name__}: {e}"` 然后只替换非法
    字符 —— 那**不是脱敏**。凭据的字符集(字母/数字/下划线/短横/等号)本来
    就全在被保留的范围内, 于是形如

        handshake failed Cookie: sessionid=TEST_SECRET_DO_NOT_LOG

    的异常正文会把 `TEST_SECRET_DO_NOT_LOG` 几乎原样带进 WARN 日志、
    `arm.error`、以及落盘的 `session_summary.json`。Issue #17 明确要求
    登录 Cookie **绝不能**进入日志与诊断摘要。

    正确做法是**根本不保留 `str(e)`** —— 只保留"哪一类错误"。分类信息对
    排障足够(是该查网络、该查房间号、还是该查签名/握手), 而它**结构上
    不可能**携带凭据, 因为输出是一个来自闭集的常量。

    分类顺序有意从"最具体"到"最泛": 握手类异常往往是网络类的子类, 先判
    具体的那个才能给出有用的类别。
    """
    name = type(e).__name__
    # 按类名匹配(而不是 import websocket 的异常类): 这里要能在
    # websocket-client 缺席/版本变化的场景下仍然工作 —— 诊断不该因为
    # 一个第三方库的导入问题就崩。
    if "SSLError" in name or "Certificate" in name:
        return ERR_NETWORK
    if "Timeout" in name or "timeout" in str(getattr(e, "args", ("",))[0]):
        return ERR_TIMEOUT
    if "BadStatus" in name or "Handshake" in name or "HandshakeStatus" in name:
        return ERR_HANDSHAKE
    if "WebSocket" in name:
        return ERR_HANDSHAKE
    if "Connection" in name or "Proxy" in name or "DNS" in name \
            or "gaierror" in name:
        return ERR_NETWORK
    if isinstance(e, (TimeoutError,)):
        return ERR_TIMEOUT
    if isinstance(e, ConnectionError):
        return ERR_NETWORK
    if isinstance(e, (FileNotFoundError, PermissionError, IsADirectoryError,
                      NotADirectoryError, OSError)):
        return ERR_OS
    if isinstance(e, (TypeError, ValueError, KeyError, AttributeError)):
        return ERR_VALUE
    return ERR_UNKNOWN


def _safe_error(e: BaseException) -> str:
    """异常 -> **一行安全文本**。绝不包含异常正文。

    ⚠️ 返回的是 `f"{类别}({异常类型名})"`, 例如
    `handshake_failed(WebSocketBadStatusException)`。

    为什么连类型名都保留: 类型名来自 Python 的类定义, **不是**运行时数据
    —— 它不可能包含某个具体的 cookie 值。而它对排障很有用("是
    BadStatus 还是 Timeout")。反之, 异常**正文**几乎总是运行时拼出来的,
    也就几乎总是有机会带上请求上下文(而请求头里就有登录 Cookie)。

    也不要在这里加"截断到 N 字符"的折中: 截断只缩短泄漏, 不消除泄漏,
    而半个凭据与整个凭据在安全上是同一件事(它同样是账号的一部分)。
    """
    try:
        return f"{_error_category(e)}({type(e).__name__})"
    except Exception:                           # noqa: BLE001
        # 连分类都失败时也不能把原异常带出去。
        return ERR_UNKNOWN


def make_diagnostic_fetcher(cfg, user_unique_id, base_cls=None,
                            *, on_chat=None, on_control=None,
                            on_interaction=None,
                            interaction_enabled: bool = False,
                            keep_all: bool = False):
    """构造一个**诊断专用**的 fetcher(基线 + 探针)。

    ⚠️ 参数与基线不是一一对应, 这是刻意的:

    - `on_chat` / `on_control` / `on_interaction` / `interaction_enabled`
      在诊断模式下**只允许为空/False**(传了会抛 `ValueError`)。诊断连接
      绝不该把事件送进业务链 —— Issue non-goals 第一条就是"不改变
      SummonLedger / AI 玩家业务语义", 而"顺手接上回调"正是那种看起来
      无害、实际会让一次诊断测试污染 SummonLedger 的操作。这里**报错**
      而不是"忽略并警告": 静默忽略会让调用方以为回调生效了。
    - `keep_all` 只允许 False(诊断不落 production 弹幕文件), 理由同上。

    保留这些形参(而不是干脆不收)是为了让调用方**显式**表达意图: 相对
    于"参数不存在所以传不进来", "传进来会报错"能立刻告诉写代码的人
    "这条路必须与业务隔离"。
    """
    if on_chat is not None or on_control is not None \
            or on_interaction is not None or interaction_enabled:
        raise ValueError(
            "诊断 fetcher 不接受业务回调 —— gift_capture_diagnostic 必须与 "
            "生产业务链隔离(见 Issue non-goals: 不改变 SummonLedger / "
            "AI 玩家业务语义)。")
    if keep_all:
        raise ValueError(
            "诊断 fetcher 不落 production 弹幕文件(keep_all 固定 False)。")

    base = base_cls
    if base is None:
        import danmaku
        base = danmaku.DanmakuFetcher
    Cls = make_gift_capture_fetcher(base)
    f = Cls.__new__(Cls)
    _init_fetcher_state(f, cfg, getattr(cfg, "douyin_live_cookie", None),
                        user_unique_id)
    return f


def _init_fetcher_state(f, cfg, login_cookie, user_unique_id) -> None:
    """把一路诊断 fetcher 需要的状态建好。

    刻意**不**调用基线的 `__init__`(理由见 `GiftProbeArm.build_fetcher`),
    所以这里显式复现基线 `__init__` 里与**接收消息**有关的那部分状态。
    与 `DanmakuFetcher.__init__` / `CallbackFetcher.__init__` 一一对应,
    没有额外语义。
    """
    import sys

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    vendor = os.path.join(os.path.dirname(here), "vendor", "douyin_fetcher")
    if vendor not in sys.path:
        sys.path.insert(0, vendor)

    from liveMan import DouyinLiveWebFetcher

    # ---- DouyinLiveWebFetcher.__init__ 的状态 ----
    DouyinLiveWebFetcher.__init__(
        f, cfg.live_id,
        abogus_file=os.path.join(vendor, "a_bogus.js"),
        login_cookie=login_cookie or None)
    # ⚠️ 必须在父类 __init__ **之后**覆盖: 父类会把 user_unique_id 设成
    # 那个写死的常量, 而随机 uid 臂的全部意义就在于换掉它。
    f.user_unique_id = user_unique_id

    # ---- DanmakuFetcher.__init__ 的状态(不重开 out_path, 诊断不写它) ----
    f.keep_all = False                 # 诊断**不**落 production 弹幕文件
    f.interaction_enabled = False      # 诊断**不**接业务回调
    f.out_path = ""                    # 空的: 见下面 `_no_production_sink`
    f._fp = _NoProductionSink()
    f._counts = {}
    f.connection_generation = 0
    f.ws_frame_count = 0
    f.ws_message_count = 0
    f.method_counts = {}
    f.unhandled_method_counts = {}
    f.parse_error_counts = {}
    f.session_method_counts = {}
    f.session_frame_count = 0
    f._terminated = threading.Event()

    # ---- CallbackFetcher.__init__ 的回调位(全部为空) ----
    f._on_chat = lambda *_a, **_k: None
    f._on_control = None
    f._on_interaction = None
    f._on_frame = None
    f._on_first_frame = None
    f._first_frame_seen = False
    f.generation = 0
    f._expired = False
    f._proxy = None
    f._no_proxy = None


class _NoProductionSink:
    """一个**什么都不写**的文件替身, 但把写入内容记在内存里。

    `DanmakuFetcher._emit` 会往 `self._fp` 写 production 弹幕行。诊断模式
    收到的消息不该进 `data/danmaku.jsonl` —— 那是 production 数据路径
    (Issue 测试点 10)。用一个吞掉一切的替身比"把 out_path 指到临时文件"
    更干净: 临时文件同样是磁盘写入, 只是换了个地方, 而这里根本不需要它。

    保留 `write` / `flush` / `close` 三个方法即可 —— `_emit` 只用这三个。

    ⚠️ 把内容留在 `written` 里**不是**为了方便调试: Issue 测试点 13 要求
    "probe 关闭时现有生产行为逐字/语义保持", 而"逐字"意味着必须能拿到
    诊断侧**本该写出去**的那串字节, 然后与基线真的写出去的那串做逐字比对。
    没有它就只剩"两个计数器相等"这种弱得多的语义断言。

    内容**只在内存里**, `wrote_to_disk` 恒为 False —— 任何落盘都是 bug。
    """

    #: 恒为 False。存在的意义是让"诊断写了盘"这件事可被断言, 而不是
    #: 靠人去读代码确认。
    wrote_to_disk = False

    def __init__(self):
        self.written: list = []

    def write(self, s):
        # 只记**字符串**长度与内容; `_emit` 传的一定是 str。
        if isinstance(s, str):
            self.written.append(s)
        return 0

    def flush(self):                        # noqa: D102
        return None

    def close(self):                        # noqa: D102
        return None


class GiftCaptureDiagnosticRunner:
    """`gift_capture_diagnostic` 模式的调度者。

    职责边界很清楚: **启动、汇总、停止**。它不解析任何协议、不看任何
    payload —— 那些是探针的事。它只保证:

        - 每路各自的目录与计数器互不串路;
        - 任何一路挂掉都不影响其余路;
        - 停的时候把每一路的最终摘要打出来(现场排查的最后一份证据)。
    """

    def __init__(self, cfg, *, profiles=None, profile_names=None,
                 limit: int = 3, probe_root: str = DEFAULT_PROBE_ROOT,
                 summary_interval_seconds: float =
                 DEFAULT_SUMMARY_INTERVAL_SECONDS,
                 max_per_method: int = DEFAULT_MAX_PER_METHOD,
                 max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
                 max_total_files: int = DEFAULT_MAX_TOTAL_FILES,
                 session_id: Optional[str] = None,
                 base_cls=None, now_fn=None):
        self.cfg = cfg
        self.now_fn = now_fn or time.time
        # 目录检查在**构造期**跑: 配置错了要立刻知道, 而不是等第一帧到来
        # 时才在某个 except 里静默失败。
        assert_outside_production_paths(probe_root)
        self.probe_root = probe_root
        self.session_id = session_id or _new_session_id(self.now_fn())
        if profiles is None:
            profiles = select_profiles(profile_names or [], limit=limit)
        self.profiles = list(profiles)
        self.summary_interval_seconds = summary_interval_seconds
        self.max_per_method = max_per_method
        self.max_total_bytes = max_total_bytes
        self.max_total_files = max_total_files
        self._base_cls = base_cls
        self.arms: list = []

    # ------------------------------------------------------------------
    @property
    def session_dir(self) -> str:
        return os.path.join(os.path.abspath(self.probe_root),
                            _session_id_component(self.session_id))

    def _base(self):
        if self._base_cls is not None:
            return self._base_cls
        # 延后导入: `danmaku` 会往 sys.path 插 vendor 并 monkey-patch
        # `generateSignature`, 那些副作用不该在 import 本模块时就发生。
        import danmaku
        return danmaku.DanmakuFetcher

    # ------------------------------------------------------------------
    def start(self) -> list:
        """并发启动所有臂。返回 arm 列表。

        ⚠️ 每路独立 `try`: 一路 `build_fetcher` 抛了不该让后面的臂起不来。
        新建 arm 对象本身也可能失败(目录不可写), 所以连 arm 的构造都在
        try 里 —— 但要**保留**失败信息, 否则用户会以为"三路都跑了"。
        """
        base = self._base()
        arms = []
        for p in self.profiles:
            try:
                arm = GiftProbeArm(
                    p, self.cfg, self.session_id, probe_root=self.probe_root,
                    max_per_method=self.max_per_method,
                    max_total_bytes=self.max_total_bytes,
                    max_total_files=self.max_total_files,
                    summary_interval_seconds=self.summary_interval_seconds,
                    now_fn=self.now_fn)
            except Exception as e:              # noqa: BLE001
                log.warning("gift probe 路 %s 构造失败: %s",
                            p.profile_id, _safe_error(e))
                continue
            arms.append(arm)
            log.info("gift probe 启动 %s -> %s", arm.log_line(), arm.store.dir)
            try:
                arm.start(base)
            except Exception as e:              # noqa: BLE001
                arm.error = _safe_error(e)
                log.warning("gift probe 路 %s 启动失败: %s",
                            p.profile_id, arm.error)
        if not arms:
            log.error("gift probe: 没有任何一路启动成功 —— 检查 profile "
                      "名字与 probe 目录权限")
        self.arms = arms
        return arms

    # ------------------------------------------------------------------
    def summaries(self) -> list:
        """每路的结构化摘要(供 PR 描述 / 事后分析)。"""
        out = []
        for arm in self.arms:
            try:
                out.append(arm.summary())
            except Exception as e:              # noqa: BLE001
                out.append({"profile": arm.profile.profile_id,
                            "error": _safe_error(e)})
        return out

    # ------------------------------------------------------------------
    def write_session_summary(self) -> Optional[str]:
        """把本次 session 的最终结果写进 `session_dir/session_summary.json`。

        为什么需要它: "三路都连上了但一条 Gift 都没有"是本轮**最可能**
        的结果, 而那个结果在目录里表现为**三个空目录** —— 与"程序根本没
        起来"无法区分。这份摘要把"跑了哪几路、各自的 auth 态与判据、上限
        是多少、有没有连接期错误"固定下来, 于是空目录也读得懂。

        ⚠️ 只写计数、`auth` 两个词、目录与上限 —— **没有** Cookie、没有
        payload、没有用户昵称。这份文件是本包唯一会写到 session 根的产物。
        """
        try:
            os.makedirs(self.session_dir, exist_ok=True)
        except OSError as e:                    # noqa: BLE001
            log.warning("gift probe: 建 session 目录失败: %s", _safe_error(e))
            return None
        path = os.path.join(self.session_dir, "session_summary.json")
        rec = {
            "session_id": self.session_id,
            "live_id": str(getattr(self.cfg, "live_id", "") or ""),
            "profile_count": len(self.arms),
            "limits": {
                "max_per_method": self.max_per_method,
                "max_total_bytes": self.max_total_bytes,
                "max_total_files": self.max_total_files,
                "max_concurrent_profiles": len(self.profiles),
                "summary_interval_seconds": self.summary_interval_seconds,
            },
            "profiles": [a.summary() for a in self.arms],
            # 显式声明本模式的边界 —— 让任何读这份文件的人(包括未来的
            # 自己)不可能把它误当成"礼物业务已经接通了"。
            "scope": {
                "gift_thanks": False,
                "announcement": False,
                "business_semantics_defined": False,
                "produces_production_data": False,
            },
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False, indent=2)
            return path
        except OSError as e:                    # noqa: BLE001
            log.warning("gift probe: 写 session 摘要失败: %s", _safe_error(e))
            return None

    # ------------------------------------------------------------------
    def stop(self, join_timeout: float = 5.0) -> None:
        """停掉所有臂, 并把每路的最终摘要打一次。

        `join_timeout` 存在是因为 `start()` 里 fetcher 的重连循环可能卡在
        网络请求上。诊断模式退出不该为了等它而挂住整个进程 —— 这些是
        daemon 线程, 进程退出时它们本来就会被收掉。
        """
        for arm in self.arms:
            f = arm.fetcher
            if f is not None:
                try:
                    f.log_gift_probe_summary("停止时")
                except Exception:               # noqa: BLE001
                    pass
                try:
                    f.terminate()
                except Exception:               # noqa: BLE001
                    pass
        for arm in self.arms:
            t = arm.thread
            if t is not None and t.is_alive():
                try:
                    t.join(timeout=join_timeout)
                except Exception:               # noqa: BLE001
                    pass
        for arm in self.arms:
            try:
                log.info("gift probe 结束 %s verdict=%s",
                         arm.profile.profile_id,
                         arm.counters.verdict())
            except Exception:                   # noqa: BLE001
                pass


def run_gift_capture_diagnostic(cfg, *, profile_names=None, limit: int = 3,
                                probe_root: str = DEFAULT_PROBE_ROOT,
                                summary_interval_seconds: float =
                                DEFAULT_SUMMARY_INTERVAL_SECONDS,
                                max_per_method: int = DEFAULT_MAX_PER_METHOD,
                                duration_seconds: float = 0.0,
                                base_cls=None, now_fn=None) -> dict:
    """跑一轮诊断(供 CLI 与测试使用)。

    `duration_seconds > 0` 时跑满就停; `0` 表示一直跑到 Ctrl-C —— 真实
    直播验收用后者(见 Issue §真实直播验收流程)。

    返回一个**可落盘 / 可贴进 PR** 的结果字典。⚠️ 里面只有计数、`auth`
    两个词、目录路径与上限值 —— **没有** Cookie、没有 payload、没有用户
    昵称(昵称只进 gitignore 覆盖的语义 JSONL)。
    """
    runner = GiftCaptureDiagnosticRunner(
        cfg, profiles=default_profiles(limit) if not profile_names else None,
        profile_names=profile_names, limit=limit, probe_root=probe_root,
        summary_interval_seconds=summary_interval_seconds,
        max_per_method=max_per_method, base_cls=base_cls, now_fn=now_fn)
    started = time.monotonic()
    try:
        runner.start()
        if duration_seconds and duration_seconds > 0:
            time.sleep(float(duration_seconds))
        else:
            # 一直跑到 Ctrl-C。
            #
            # ⚠️ 不要再用“无限期 Event().wait()”。Windows 控制台下，主线程
            # 卡在一次无超时的底层 wait 时，Ctrl-C / SIGINT 可能不能及时让
            # Python 抛 KeyboardInterrupt，现场表现就是“Ctrl-C 怎么都关
            # 不掉”。改成短超时轮询：最坏约 0.25s 就回到 Python 解释器一次，
            # 让 SIGINT 有机会被处理；worker 仍然都在自己的线程里运行。
            stop_wait = threading.Event()
            try:
                while not stop_wait.wait(0.25):
                    pass
            except KeyboardInterrupt:
                log.info("gift probe: 收到 Ctrl-C，正在停止三路诊断连接…")
    finally:
        runner.stop()
        summary_path = runner.write_session_summary()
    elapsed = time.monotonic() - started
    return {
        "session_id": runner.session_id,
        "session_dir": runner.session_dir,
        "session_summary_path": summary_path,
        "elapsed_seconds": round(elapsed, 1),
        "limits": {
            "max_per_method": max_per_method,
            "max_concurrent_profiles": limit,
            "probe_root": os.path.abspath(probe_root),
        },
        "profiles": runner.summaries(),
    }
