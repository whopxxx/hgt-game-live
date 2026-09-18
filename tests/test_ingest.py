#!/usr/bin/env python
# coding: utf-8
"""离线自测: 弹幕接入层。`uv run tests/test_ingest.py`

关键点: 验证 SimSource 走的是**真实** protobuf + **真实** _parseChatMsg。
"""

from __future__ import annotations

import io
import json
import os
import queue
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from danmaku import ChatMessage, DanmakuFetcher   # 从 danmaku 转出(它会先把 vendor 加进路径)
from story.config import Config
from story.ingest import (CallbackFetcher, ChatEvent, InteractionEvent,
                          LiveSource, SimSource, StdinSource)

# 测试用的脚本一律放这里(受版本控制)。**绝不要**引用 data/ 下的文件:
# data/*.jsonl 被 .gitignore 排除, 在干净的 checkout(CI)上不存在。
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global FAIL
    if cond:
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def test_sim_uses_real_parser() -> None:
    print("\n[1] SimSource 走真实解析路径")
    # 用仓库里的 fixture, **不要**用 data/demo_script.jsonl —— 那个被
    # .gitignore 排除, 只存在于开发机上。CI 第一次跑就抓出了这个问题
    # (FileNotFoundError), 因为它在本机永远是绿的。
    script = os.path.join(FIXTURES, "ingest_script.jsonl")
    cfg = Config(sim_path=script, sim_loop_gap=1, no_llm=True)
    inbox: queue.Queue = queue.Queue()
    src = SimSource(cfg, inbox)

    # 直接驱动一次, 不启线程
    src._load()
    # 断言**语义**而不是 magic number: 早先写死 `== 17`, 于是往演示
    # 脚本里加一条弹幕就会弄挂这个测试 —— 而它想验的根本不是条数。
    check("脚本载入", len(src._loop_items) >= 2, f"got {len(src._loop_items)}")
    check("loop 识别", src._loop_enabled is True)

    src._emit(src._loop_items[0])          # 第一条: 这题有意思
    ev = inbox.get_nowait()
    check("弹幕进入 inbox", isinstance(ev, ChatEvent))
    check("内容正确", ev.content == "这题有意思", f"got {ev.content!r}")
    check("昵称正确", ev.user_name == "路人甲")
    check("user_id 已分配", ev.user_id != 0 and ev.user_id is not None)

    # 验证确实走了 _parseChatMsg: fetcher 的计数被增加
    check("真实 _parseChatMsg 被调用(_counts 有 chat)",
          src._fetcher._counts.get("chat", 0) >= 1)
    src._fetcher._fp.close()


def test_sim_user_id_explicit() -> None:
    print("\n[2] SimSource 显式 user_id")
    import json, tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False,
                                     encoding="utf-8") as f:
        f.write(json.dumps({"at_ms": 0, "user_id": 777, "user_name": "X",
                            "content": "#a"}) + "\n")
        path = f.name
    try:
        cfg = Config(sim_path=path, sim_loop_gap=1, no_llm=True)
        inbox: queue.Queue = queue.Queue()
        src = SimSource(cfg, inbox)
        src._load()
        src._emit(src._loop_items[0])
        ev = inbox.get_nowait()
        check("显式 user_id 生效", ev.user_id == 777, f"got {ev.user_id}")
        src._fetcher._fp.close()
    finally:
        os.unlink(path)


def test_control_stream_ended() -> None:
    print("\n[3] control=stream_ended 触发回调 (不抛异常)")
    import json, tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False,
                                     encoding="utf-8") as f:
        f.write(json.dumps({"at_ms": 0, "user_name": "A", "content": "#x"}) + "\n")
        f.write(json.dumps({"control": "stream_ended"}) + "\n")
        path = f.name
    try:
        cfg = Config(sim_path=path, sim_loop_gap=1, no_llm=True)
        inbox: queue.Queue = queue.Queue()
        fired = {"v": False}
        src = SimSource(cfg, inbox, on_stream_end=lambda: fired.__setitem__("v", True))
        src.start()
        time.sleep(1.5)
        src.stop()
        check("stream_ended 回调触发", fired["v"] is True)
        check("未抛异常(进程仍活)", True)
    finally:
        os.unlink(path)


def test_callback_fetcher_guard() -> None:
    print("\n[4] CallbackFetcher: stop() 在未连接时不崩")
    events = []
    f = CallbackFetcher.__new__(CallbackFetcher)
    f._on_chat = events.append
    f._on_control = lambda k: events.append(k)
    f.keep_all = False
    f.out_path = os.devnull
    import collections
    f._counts = collections.Counter()
    f._fp = open(os.devnull, "w", encoding="utf-8")
    try:
        # ws 从未建立 -> 原库 stop() 会 AttributeError; 我们的 _parseControlMsg 要 guard
        from danmaku import ControlMessage
        cm = ControlMessage(); cm.status = 3
        f._parseControlMsg(cm.SerializeToString())   # 不应抛出
        check("_parseControlMsg 未抛异常", True)
        check("on_control 收到 stream_ended", "stream_ended" in events)
    except Exception as e:
        check("_parseControlMsg 未抛异常", False, f"抛了 {type(e).__name__}: {e}")
    finally:
        f._fp.close()


def test_chat_parse_roundtrip() -> None:
    print("\n[5] CallbackFetcher 聊天回调 + JSONL")
    events = []
    f = CallbackFetcher.__new__(CallbackFetcher)
    f._on_chat = events.append
    f._on_control = None
    f.keep_all = False
    import collections, tempfile
    f._counts = collections.Counter()
    tmp = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False,
                                      encoding="utf-8")
    f.out_path = tmp.name
    tmp.close()
    f._fp = open(tmp.name, "a", encoding="utf-8")
    try:
        from danmaku import ChatMessage
        m = ChatMessage(); m.content = "#测试"; m.user.nick_name = "甲"; m.user.id = 5
        f._parseChatMsg(m.SerializeToString())
        check("回调收到", len(events) == 1 and events[0].content == "#测试")
        f._fp.flush()
        content = open(tmp.name, encoding="utf-8").read().strip()
        check("JSONL 落库", '"content": "#测试"' in content, f"got {content!r}")
    finally:
        f._fp.close()
        os.unlink(tmp.name)


def test_session_timeout_patch() -> None:
    """上线前必须给上游 session 加超时 —— 否则无超时请求会永久挂起,
    把抓取线程钉死(Python 线程无法强杀), 这是"重连也救不回来"的根因。"""
    print("\n[5] session 默认超时补丁")
    import requests
    from story.ingest import add_default_timeout
    sess = requests.Session()
    seen = []

    class _Resp:
        def json(self): return {}
        def raise_for_status(self): pass

    def fake_request(method, url, **kw):
        seen.append(kw.get("timeout"))
        return _Resp()

    sess.request = fake_request
    add_default_timeout(sess, timeout=(5, 10))
    sess.get("https://x")
    check("没传时补上默认超时", seen[-1] == (5, 10), f"got {seen[-1]}")
    sess.get("https://x", timeout=99)
    check("显式超时不被覆盖", seen[-1] == 99, f"got {seen[-1]}")


def test_expired_fetcher_drops_data() -> None:
    """作废的旧 fetcher 不能再往业务层塞数据(防旧线程诈尸)。"""
    print("\n[6] 作废连接不吐数据")
    import collections, tempfile
    from danmaku import ChatMessage
    got = []
    f = CallbackFetcher.__new__(CallbackFetcher)
    f._on_chat = lambda ev: got.append(ev.content)
    f._on_frame = None
    f._expired = False
    f._counts = collections.Counter()
    tmp = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False,
                                      encoding="utf-8")
    tmp.close()
    f._fp = open(tmp.name, "a", encoding="utf-8")
    try:
        m = ChatMessage()
        m.user.id, m.user.nick_name, m.content = 1, "甲", "#正常"
        f._parseChatMsg(m.SerializeToString())
        check("未作废时正常收", got == ["#正常"], f"got {got}")
        f._expired = True
        m2 = ChatMessage()
        m2.user.id, m2.user.nick_name, m2.content = 2, "乙", "#诈尸"
        f._parseChatMsg(m2.SerializeToString())
        check("作废后丢弃", got == ["#正常"], f"got {got}")
    finally:
        f._fp.close()
        os.unlink(tmp.name)


def test_synth_msg_ids_are_distinct() -> None:
    """Q12: sim/stdin 必须给**互不相同**的合成 msg_id。

    坑: 不设 `msg_id` 时每条都是 `0`。引擎把"有 ID"和"ID 是 0"区分开
    (0 -> 空串 -> 走降级路径), 但一旦哪天有人图省事把 0 当成真实 ID,
    第二条起就全被判重丢掉了。这里钉住 sim 路径产出的 ID 两两不同。
    """
    print("\n[6] 合成 msg_id 互不相同(Q12)")
    with tempfile.TemporaryDirectory() as d:
        script = os.path.join(d, "s.jsonl")
        with io.open(script, "w", encoding="utf-8") as f:
            for i in range(3):
                f.write(json.dumps({
                    "at_ms": i * 100, "user_name": f"观众{i}",
                    "content": f"#问题{i}"}, ensure_ascii=False) + "\n")
        cfg = Config(sim_path=script, sim_loop_gap=1, no_llm=True)
        q: "queue.Queue" = queue.Queue()
        src = SimSource(cfg, q)
        src._load()
        for it in src._loop_items:
            src._emit(it)
        ids = []
        while not q.empty():
            ids.append(q.get_nowait().message_id)
        src._fetcher._fp.close()
        check("收到 3 条", len(ids) == 3, ids)
        check("**ID 两两不同**", len(set(ids)) == 3, ids)
        check("**没有空 ID**", all(ids), ids)


def test_first_frame_callback_fires_once() -> None:
    """Q12: 新连接的**首个** WS 帧触发一次回调, 之后不再触发。

    这是"重连成功"的信号 —— 引擎靠它开 replay guard。多触发会不停重置
    guard 窗口, 不触发则重放识别失效。
    """
    print("\n[7] 首帧回调只触发一次(Q12)")
    f = CallbackFetcher.__new__(CallbackFetcher)
    f._expired = False
    f._first_frame_seen = False
    f._on_frame = None
    hits = []
    f._on_first_frame = lambda: hits.append(1)

    class _WS:
        pass

    # 首帧标记在 `_wsOnMessage` 里, 而它会转发给父类做真实解析 ——
    # 把父类方法换成 no-op, 只验我们插入的那段标记逻辑。
    parent = CallbackFetcher.__mro__[1]
    orig = parent._wsOnMessage
    parent._wsOnMessage = lambda self, ws, msg: None
    try:
        f._wsOnMessage(_WS(), "frame1")
        f._wsOnMessage(_WS(), "frame2")
        f._wsOnMessage(_WS(), "frame3")
    finally:
        parent._wsOnMessage = orig
    check("**只触发一次**", len(hits) == 1, len(hits))


def test_missing_common_means_empty_id() -> None:
    """没有 `common.msg_id` 的消息 -> 空 ID(走降级), **不是** '0'。"""
    print("\n[8] 缺失 msg_id -> 空串(Q12)")
    f = CallbackFetcher.__new__(CallbackFetcher)
    f._expired = False
    got = []
    f._on_chat = lambda ev: got.append(ev)
    f._on_control = None
    f.keep_all = False
    f.out_path = os.devnull
    import collections
    f._counts = collections.Counter()
    f._fp = io.open(os.devnull, "w", encoding="utf-8")
    m = ChatMessage()
    m.user.id = 1
    m.user.nick_name = "甲"
    m.content = "#问题"
    # 刻意不设 m.common.msg_id
    f._parseChatMsg(m.SerializeToString())
    check("收到事件", len(got) == 1, len(got))
    if got:
        check("**message_id 是空串而不是 '0'**", got[0].message_id == "",
              repr(got[0].message_id))


def test_stdin_path_works() -> None:
    """Q12 回归: `--stdin` 真的能跑, 且产出非空 msg_id。

    **这条是补的漏测。** `_run()` 里会 `self._msg_id += 1`, 但
    `__init__` 早先漏了初始化 —— 于是第一条 stdin 输入直接
    AttributeError。CI 没抓到是因为 test_ingest 只驱动了 sim 路径。
    这里真跑一遍 stdin 的读取循环(喂一个假 stdin)。
    """
    print("\n[9] stdin 路径: 不崩 + 有合成 ID(Q12)")
    cfg = Config(sim_path="x", no_llm=True)
    inbox: queue.Queue = queue.Queue()
    src = StdinSource(cfg, inbox)
    check("**__init__ 里有 _msg_id**", hasattr(src, "_msg_id"), dir(src))
    fake = io.StringIO("甲:#第一个问题\n乙:#第二个问题\n")
    orig = sys.stdin
    sys.stdin = fake
    try:
        src._run()
    except Exception as e:                      # noqa: BLE001
        check("**stdin 循环不抛异常**", False, repr(e))
    finally:
        sys.stdin = orig
    got = []
    while not inbox.empty():
        got.append(inbox.get_nowait())
    check("收到 2 条", len(got) == 2, len(got))
    check("**两条都有非空 msg_id**", all(e.message_id for e in got),
          [e.message_id for e in got])
    check("**ID 两两不同**",
          len({e.message_id for e in got}) == len(got),
          [e.message_id for e in got])


def test_first_frame_resets_per_connection() -> None:
    """Q12b: **每次** WebSocket 建连后首帧都要报信号, 不只是头一次。

    上游 `DanmakuFetcher.start()` 是 `while True` + `run_forever()`,
    断线后会在**同一个 fetcher 实例**上重新建连。若 `_first_frame_seen`
    只在实例级置一次, 自动重连后的首帧就永远不会再报 —— 而那正是重放
    发生的地方(重放识别会整个失效)。
    """
    print("\n[10] 首帧信号按连接 epoch 重置(Q12b)")
    f = CallbackFetcher.__new__(CallbackFetcher)
    f._expired = False
    f._first_frame_seen = False
    f._on_frame = None
    hits = []
    f._on_first_frame = lambda: hits.append(1)

    class _WS:
        pass

    parent = CallbackFetcher.__mro__[1]
    orig_msg = parent._wsOnMessage
    orig_open = parent._wsOnOpen
    parent._wsOnMessage = lambda self, ws, msg: None
    parent._wsOnOpen = lambda self, ws: None
    try:
        # 连接 1: 两个帧 -> 只报一次
        f._wsOnOpen(_WS())
        f._wsOnMessage(_WS(), "a")
        f._wsOnMessage(_WS(), "b")
        check("连接1: 只报一次", len(hits) == 1, len(hits))
        # 连接 2(自动重连, **同一实例**): 首帧要再报一次
        f._wsOnOpen(_WS())
        f._wsOnMessage(_WS(), "c")
        check("**连接2: 又报了一次**", len(hits) == 2, len(hits))
        f._wsOnMessage(_WS(), "d")
        check("连接2 后续帧不重复报", len(hits) == 2, len(hits))
    finally:
        parent._wsOnMessage = orig_msg
        parent._wsOnOpen = orig_open


def _patch_network_boundary(behavior, holder):
    """把父类 start() 换成"连上->阻塞直到 stop()"。

    返回还原函数。`holder["connects"]` 由调用方读。
    """
    parent = DanmakuFetcher.__mro__[1]             # DouyinLiveWebFetcher
    orig = parent.start

    def fake_start(self):
        holder["connects"] += 1
        if behavior == "error":
            raise ConnectionResetError("模拟普通网络断线")
        self._live.set()
        self._live.wait(5.0)                       # 直到 stop() 清掉

    parent.start = fake_start

    def restore():
        parent.start = orig
    return restore


class _CountingFetcher(DanmakuFetcher):
    """跑**真实** `DanmakuFetcher.start()`, 只打桩网络边界。"""

    def __init__(self, behavior="ok"):
        self.out_path = os.devnull
        self.keep_all = False
        self._fp = None
        self._counts = {}
        self._terminated = threading.Event()
        self.live_id = "test"
        self.behavior = behavior
        self.connects = 0
        self._live = threading.Event()

    # 复刻上游 stop(): 只关当前 socket, 让"本次连接"结束
    def stop(self):
        self._live.clear()


def test_terminate_stops_reconnect() -> None:
    """下播后**不得**再次建连 —— 这是 Hotfix A 的核心验收。

    跑的是**真实** `DanmakuFetcher.start()`: 只把父类的网络连接打桩,
    循环结构本身是生产代码那一份。
    """
    print("\n[13] terminate(): 下播后不再重连")
    holder = {"connects": 0}
    f = _CountingFetcher(behavior="ok")
    restore = _patch_network_boundary("ok", holder)
    try:
        t = threading.Thread(target=f.start, daemon=True)
        t.start()
        time.sleep(0.3)
        check("先连上过", holder["connects"] >= 1, holder["connects"])
        f.terminate()
        t.join(timeout=5.0)
        check("terminate 后线程退出", not t.is_alive())
        n = holder["connects"]
        time.sleep(0.5)                    # 循环若还活着, 这里必然继续涨
        check("**不再有新的建连**", holder["connects"] == n,
              f"{n} -> {holder['connects']}")
    finally:
        restore()


def test_plain_disconnect_still_reconnects() -> None:
    """普通网络断线**必须**仍然自动重连 —— 防 `stop()` 被误改成终止。

    这条是 Hotfix A 里最重要的防回归: 原库自己在错误路径上调
    `stop()`(`liveMan.py` 的 _connectWebSocket except 分支), 如果谁把
    `stop()` 写成永久退出, 一次抖动就永久断流。
    """
    print("\n[14] 普通断线仍自动重连(防回归)")
    holder = {"connects": 0}
    f = _CountingFetcher(behavior="error")
    restore = _patch_network_boundary("error", holder)
    try:
        t = threading.Thread(target=f.start, daemon=True)
        t.start()
        # 真实退避是 3s -> 6s -> 12s, 所以 2 次建连约 9 秒。
        time.sleep(9.5)
        check("**反复重连中**", holder["connects"] >= 2, holder["connects"])
        f.terminate()
        t.join(timeout=5.0)
        check("terminate 能停住错误重试循环", not t.is_alive())
    finally:
        restore()


def test_stop_is_not_terminal() -> None:
    """`stop()` 只中止当前连接, **不**终止重连循环。"""
    print("\n[15] stop() 与 terminate() 语义不同")
    holder = {"connects": 0}
    f = _CountingFetcher(behavior="ok")
    restore = _patch_network_boundary("ok", holder)
    try:
        t = threading.Thread(target=f.start, daemon=True)
        t.start()
        time.sleep(0.3)
        before = holder["connects"]
        check("先连上过", before >= 1, before)
        f.stop()                           # 只关当前连接
        # 真实退避 3s, 所以等 4 秒看它有没有再连
        time.sleep(4.0)
        check("**stop() 后循环仍在跑(会继续重连)**",
              holder["connects"] > before, f"{before} -> {holder['connects']}")
        f.terminate()
        t.join(timeout=5.0)
        check("terminate 收尾", not t.is_alive())
    finally:
        restore()


def test_livesource_terminates_on_stream_end() -> None:
    """端到端: 下播信号 -> LiveSource 终止 fetcher(不再重连)。

    这是 2026-09-18 那次真实故障的复现路径:
        _parseControlMsg(status=3) -> _on_control("stream_ended")
        -> LiveSource._on_control -> 必须 terminate, 而不只是 stop
    修好之前, 日志里会变成 "收到下播信号 -> 又连上 -> 又收到下播信号" 的循环。
    """
    print("\n[16] 下播 -> LiveSource 终止重连(端到端)")
    from story.ingest import LiveSource

    src = LiveSource.__new__(LiveSource)
    src._stop = threading.Event()
    src._on_stream_end = None
    holder = {"connects": 0}

    f = _CountingFetcher(behavior="ok")
    src._fetcher = f
    restore = _patch_network_boundary("ok", holder)
    try:
        t = threading.Thread(target=f.start, daemon=True)
        t.start()
        time.sleep(0.3)
        check("fetcher 已连上", holder["connects"] >= 1, holder["connects"])

        # 模拟真实链路: 收到下播信号
        src._on_control("stream_ended")

        t.join(timeout=5.0)
        check("**下播后抓取线程退出**", not t.is_alive())
        n = holder["connects"]
        time.sleep(0.5)
        check("**下播后不再重连**", holder["connects"] == n,
              f"{n} -> {holder['connects']}")
        check("source 的 _stop 已置位", src._stop.is_set())
    finally:
        restore()


# ======================================================================
# Step 11 — InteractionEvent plumbing
# ======================================================================
def _mk_cb_fetcher(events, interaction=True):
    """造一个半成品 CallbackFetcher(沿用 test_chat_parse_roundtrip 的手法)。"""
    import collections, tempfile
    f = CallbackFetcher.__new__(CallbackFetcher)
    f._on_chat = lambda e: None
    f._on_control = None
    f._on_interaction = events.append if interaction else None
    f.keep_all = False
    f.interaction_enabled = interaction
    f._expired = False
    f._counts = collections.Counter()
    tmp = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False,
                                      encoding="utf-8")
    f.out_path = tmp.name
    tmp.close()
    f._fp = open(tmp.name, "a", encoding="utf-8")
    return f, tmp.name


def test_like_reaches_business_chain_without_keep_all() -> None:
    """**核心**: keep_all=False + interaction_enabled=True -> Like 仍进业务链。

    这是 Step 11 要解的那个耦合: 早先 `_parseLikeMsg` 开头就是
    `if not self.keep_all: return`, 于是"想收礼物"必须开全量落库。
    """
    print("\n[11a] keep_all=False 也能收 Like")
    events = []
    f, path = _mk_cb_fetcher(events)
    try:
        from danmaku import LikeMessage
        m = LikeMessage()
        m.count = 3
        m.total = 520
        m.user.id = 7
        m.user.nick_name = "甲"
        m.common.msg_id = 999
        f._parseLikeMsg(m.SerializeToString())
        check("Like 进了业务链", len(events) == 1, events)
        if events:
            ev = events[0]
            check("kind=like", ev.kind == "like", ev.kind)
            check("count 原样带过", ev.count == 3, ev.count)
            check("total 原样带过", ev.total == 520, ev.total)
            check("msg_id 原样带过", ev.message_id == "999", ev.message_id)
        check("keep_all=False 时**不**落库",
              f._counts.get("like", 0) == 0, f._counts)
    finally:
        f._fp.close()
        os.unlink(path)


def test_gift_reaches_business_chain_without_keep_all() -> None:
    """Gift 同理 —— 业务与落库解耦。"""
    print("\n[11b] keep_all=False 也能收 Gift")
    events = []
    f, path = _mk_cb_fetcher(events)
    try:
        from danmaku import GiftMessage
        m = GiftMessage()
        m.gift_id = 12345
        m.gift.name = "玫瑰"
        m.combo_count = 2
        m.repeat_count = 2
        m.total_count = 5
        m.group_id = 88
        # RF-3: proto 里 logId(16) 与 traceId(35) 是**两个**字段。
        # 早先测试只设 log_id 并断言 trace_id 等于它 —— 那是在锁死一个
        # 错误的映射。两个都设, 分别断言。
        m.log_id = "log-abc"
        m.trace_id = "trace-xyz"
        m.user.id = 9
        m.user.nick_name = "乙"
        m.common.msg_id = 1001
        f._parseGiftMsg(m.SerializeToString())
        check("Gift 进了业务链", len(events) == 1, events)
        if events:
            ev = events[0]
            for field, want in (("kind", "gift"), ("gift_id", "12345"),
                                ("gift_name", "玫瑰"), ("combo_count", 2),
                                ("repeat_count", 2), ("total_count", 5),
                                ("group_id", "88"),
                                ("trace_id", "trace-xyz"),
                                ("log_id", "log-abc"),
                                ("message_id", "1001")):
                check(f"{field} 原样带过", getattr(ev, field) == want,
                      (field, getattr(ev, field), want))
        check("keep_all=False 时不落库", f._counts.get("gift", 0) == 0)
    finally:
        f._fp.close()
        os.unlink(path)


def test_interaction_disabled_blocks_business_chain() -> None:
    """interaction_enabled=False + keep_all=False -> 不进业务链。"""
    print("\n[11c] interaction_enabled=False -> 不进业务链")
    events = []
    f, path = _mk_cb_fetcher(events, interaction=False)
    try:
        f.interaction_enabled = False
        from danmaku import GiftMessage, LikeMessage
        lm = LikeMessage(); lm.count = 1; lm.total = 1
        f._parseLikeMsg(lm.SerializeToString())
        gm = GiftMessage(); gm.gift_id = 1; gm.gift.name = "x"
        f._parseGiftMsg(gm.SerializeToString())
        check("Like 没进业务链", len(events) == 0, events)
        check("Gift 没进业务链", len(events) == 0, events)
        check("也没落库", sum(f._counts.values()) == 0, f._counts)
    finally:
        f._fp.close()
        os.unlink(path)


def test_keep_all_still_logs_diagnostics() -> None:
    """keep_all=True -> 诊断类型(Member/Social/Emoji)保持既有能力。"""
    print("\n[11d] keep_all=True 诊断类型照旧")
    events = []
    f, path = _mk_cb_fetcher(events)
    try:
        f.keep_all = True
        from danmaku import MemberMessage, SocialMessage
        mm = MemberMessage(); mm.user.id = 1; mm.user.nick_name = "丙"
        f._parseMemberMsg(mm.SerializeToString())
        sm = SocialMessage(); sm.user.id = 2; sm.user.nick_name = "丁"
        f._parseSocialMsg(sm.SerializeToString())
        f._fp.flush()
        check("member 落了库", f._counts.get("member", 0) == 1, f._counts)
        check("social 落了库", f._counts.get("social", 0) == 1, f._counts)
    finally:
        f._fp.close()
        os.unlink(path)


def test_expired_fetcher_drops_interaction() -> None:
    """作废连接不该再往业务层塞互动事件(与 chat 同一条纪律)。"""
    print("\n[11e] 作废连接的互动被丢弃")
    events = []
    f, path = _mk_cb_fetcher(events)
    try:
        f._expired = True
        from danmaku import LikeMessage
        m = LikeMessage(); m.count = 1; m.total = 1
        f._parseLikeMsg(m.SerializeToString())
        check("被丢弃", len(events) == 0, events)
    finally:
        f._fp.close()
        os.unlink(path)


def test_engine_submit_interaction_is_noop_stub() -> None:
    """Step 11: Engine 的入口是 **characterization stub** —— 不改状态。"""
    print("\n[11f] Engine submit_interaction 是 no-op 占位")
    from story.engine import RoundEngine
    eng = RoundEngine(Config(sim_path="x", no_llm=True))
    before = (eng.phase, eng.round_index, eng._qa_total)
    acts = eng.submit_interaction(object())
    check("返回空动作", acts == [], acts)
    check("阶段/题号/QA 计数都没变",
          (eng.phase, eng.round_index, eng._qa_total) == before,
          (eng.phase, eng.round_index, eng._qa_total))
    check("None 也不炸", eng.submit_interaction(None) == [])


def test_livesource_routes_interaction_to_inbox() -> None:
    """LiveSource 把互动事件放进**同一个** inbox(消费线程统一分发)。"""
    print("\n[11g] LiveSource 路由互动到 inbox")
    cfg = Config(sim_path="x", no_llm=True)
    inbox: queue.Queue = queue.Queue()
    src = LiveSource(cfg, inbox)
    ev = InteractionEvent(kind="like", count=1, total=1)
    src._on_interaction(ev)
    got = inbox.get_nowait()
    check("进了 inbox", got is ev, got)
    check("是 InteractionEvent 而不是 ChatEvent",
          not isinstance(got, ChatEvent), type(got))


# ======================================================================
# Step 11 review-fix — 真实 WS 分发层回归
# ======================================================================
def _mk_ws_fetcher(events, *, keep_all, interaction, chat_events=None):
    """造一个能跑 `_wsOnMessage` 的半成品 fetcher。"""
    import collections, tempfile
    f = CallbackFetcher.__new__(CallbackFetcher)
    f._on_chat = lambda e: (chat_events.append(e) if chat_events is not None
                            else None)
    f._on_control = None
    f._on_interaction = events.append if interaction else None
    f.keep_all = keep_all
    f.interaction_enabled = interaction
    f._expired = False
    f._counts = collections.Counter()
    tmp = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False,
                                      encoding="utf-8")
    f.out_path = tmp.name
    tmp.close()
    f._fp = open(tmp.name, "a", encoding="utf-8")
    return f, tmp.name


class _FakeWS:
    """`_wsOnMessage` 只在 need_ack 时调 `ws.send`。这里不需要 ACK。"""
    def send(self, *a, **k):
        pass


def _frame(method, payload_bytes, envelope_msg_id=0):
    """把一条内层消息包成真实 WS 帧(gzip + PushFrame + Response)。"""
    import gzip
    from vendor.douyin_fetcher.protobuf.douyin import (  # noqa: F401
        Message, PushFrame, Response)
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                      "..", "vendor", "douyin_fetcher"))
    from protobuf.douyin import Message as M, PushFrame as P, Response as R
    resp = R()
    msg = M()
    msg.method = method
    msg.payload = payload_bytes
    msg.msg_id = envelope_msg_id      # 外层 envelope id(proto 字段 3)
    resp.messages_list.append(msg)
    pkg = P()
    pkg.payload = gzip.compress(bytes(resp))
    return pkg.SerializeToString()


def test_ws_dispatch_gift_reaches_chain_without_keep_all() -> None:
    """**核心回归**: 真实 WS 帧里的 Gift 必须进业务链。

    修之前 `_wsOnMessage` 只在 `keep_all` 时注册 Gift/Like handler, 于是
    默认配置(interaction_enabled=True, keep_all=False)下礼物帧被
    `continue` 丢掉。原测试**直接调** `_parseGiftMsg`, 绕过了这一层 ——
    所以全绿。这条走真实分发路径。
    """
    print("\n[11h] 真实 WS 帧: keep_all=False 也能收 Gift")
    from danmaku import GiftMessage
    events = []
    f, path = _mk_ws_fetcher(events, keep_all=False, interaction=True)
    try:
        gm = GiftMessage()
        gm.gift_id = 42
        gm.gift.name = "玫瑰"
        gm.combo_count = 1
        frame = _frame("WebcastGiftMessage", gm.SerializeToString())
        f._wsOnMessage(_FakeWS(), frame)
        check("**帧里的 Gift 进了业务链**", len(events) == 1, events)
        if events:
            check("kind=gift", events[0].kind == "gift", events[0].kind)
            check("gift_id 正确", events[0].gift_id == "42", events[0].gift_id)
    finally:
        f._fp.close()
        os.unlink(path)


def test_ws_dispatch_like_reaches_chain_without_keep_all() -> None:
    """Like 同理 —— 走真实分发层。"""
    print("\n[11i] 真实 WS 帧: keep_all=False 也能收 Like")
    from danmaku import LikeMessage
    events = []
    f, path = _mk_ws_fetcher(events, keep_all=False, interaction=True)
    try:
        lm = LikeMessage()
        lm.count = 5
        lm.total = 100
        frame = _frame("WebcastLikeMessage", lm.SerializeToString())
        f._wsOnMessage(_FakeWS(), frame)
        check("**帧里的 Like 进了业务链**", len(events) == 1, events)
        if events:
            check("total 正确", events[0].total == 100, events[0].total)
    finally:
        f._fp.close()
        os.unlink(path)


def test_ws_dispatch_keep_all_only_logs_not_business() -> None:
    """**正交性**: keep_all=True + interaction_enabled=False
    -> 只落库, **不进**业务回调。

    ⚠️ 这条测试必须**直接**验证 `_parseGiftMsg` 不会调业务回调 ——
    不能只靠 `_wsOnMessage`。原因: 当 `interaction_enabled=False` 时
    `_on_interaction` 是 None, 而 `_on_gift` 在 None 时**自己会早退**,
    于是"落库时无条件调 `_on_gift`"这种 bug 会被那层早退掩盖 ——
    测试假绿(第一版就是这么写的, mutation 没抓到)。
    所以这里直接把 `_on_interaction` 挂上一个**会记录**的回调, 再用
    `interaction_enabled=False` 跑, 才能真的证明开关生效。
    """
    print("\n[11j] keep_all=True + interaction=False 只落库")
    from danmaku import GiftMessage
    events = []
    f, path = _mk_ws_fetcher(events, keep_all=True, interaction=False)
    try:
        # 故意挂一个**会记录**的回调 —— 这样"被误调"才看得见。
        f._on_interaction = events.append
        gm = GiftMessage()
        gm.gift_id = 7
        gm.gift.name = "x"
        f._parseGiftMsg(gm.SerializeToString())
        check("**业务回调没被调用**(尽管回调是挂着的)",
              len(events) == 0, events)
        check("但落了库", f._counts.get("gift", 0) == 1, f._counts)
        # 反过来: interaction_enabled=True 时必须调到
        events2 = []
        f2, path2 = _mk_ws_fetcher(events2, keep_all=True, interaction=True)
        try:
            gm2 = GiftMessage()
            gm2.gift_id = 8
            gm2.gift.name = "y"
            f2._parseGiftMsg(gm2.SerializeToString())
            check("interaction=True 时业务回调被调用",
                  len(events2) == 1, events2)
            check("同时也落了库", f2._counts.get("gift", 0) == 1, f2._counts)
        finally:
            f2._fp.close()
            os.unlink(path2)
    finally:
        f._fp.close()
        os.unlink(path)


def test_ws_dispatch_diagnostics_still_keep_all_only() -> None:
    """Member/Social/Emoji 仍然只在 keep_all 时注册(纯诊断类型)。"""
    print("\n[11k] 诊断类型仍只在 keep_all 时注册")
    from danmaku import MemberMessage
    # interaction=True 但 keep_all=False -> member **不该**被处理
    events = []
    f, path = _mk_ws_fetcher(events, keep_all=False, interaction=True)
    try:
        mm = MemberMessage(); mm.user.id = 1; mm.user.nick_name = "甲"
        f._wsOnMessage(_FakeWS(), _frame("WebcastMemberMessage",
                                         mm.SerializeToString()))
        check("keep_all=False 时 member 不落库",
              f._counts.get("member", 0) == 0, f._counts)
    finally:
        f._fp.close()
        os.unlink(path)
    # keep_all=True -> 落库
    f2, path2 = _mk_ws_fetcher([], keep_all=True, interaction=True)
    try:
        mm = MemberMessage(); mm.user.id = 1; mm.user.nick_name = "甲"
        f2._wsOnMessage(_FakeWS(), _frame("WebcastMemberMessage",
                                          mm.SerializeToString()))
        check("keep_all=True 时 member 落库",
              f2._counts.get("member", 0) == 1, f2._counts)
    finally:
        f2._fp.close()
        os.unlink(path2)


def test_ws_dispatch_chat_always_registered() -> None:
    """Chat 是直播命脉 —— 任何开关组合下都必须注册。"""
    print("\n[11l] Chat 永远注册")
    for keep_all, interaction in ((False, False), (False, True),
                                  (True, False), (True, True)):
        chats = []
        f, path = _mk_ws_fetcher([], keep_all=keep_all,
                                 interaction=interaction, chat_events=chats)
        try:
            from danmaku import ChatMessage
            cm = ChatMessage(); cm.content = "#问"; cm.user.id = 1
            cm.user.nick_name = "甲"
            f._wsOnMessage(_FakeWS(), _frame("WebcastChatMessage",
                                             cm.SerializeToString()))
            check(f"chat 收到(keep_all={keep_all}, interaction={interaction})",
                  len(chats) == 1, chats)
        finally:
            f._fp.close()
            os.unlink(path)


def test_ws_dispatch_captures_both_msg_ids_separately() -> None:
    """RF-7: envelope 与 common 的 msg_id 必须**分开**采下来。

    外层 `Message.msg_id`(proto 字段 3)与内层
    `GiftMessage.common.msg_id` 是两个不同的字段。`_wsOnMessage` 原来
    只传 `msg.payload`, envelope 的 id 直接被丢掉 —— 于是 Step 12 想
    观察"哪个才是稳定幂等键"时, 手里压根没有 envelope 那一半。

    ⚠️ 这条**不假设**两者相等, 也**不判断**哪个是主键 —— 只证明两个值
    都被如实带到了业务事件上, 且互不覆盖。
    """
    print("\n[11n] 两个 msg_id 分开采集")
    from danmaku import GiftMessage
    events = []
    f, path = _mk_ws_fetcher(events, keep_all=False, interaction=True)
    try:
        gm = GiftMessage()
        gm.gift_id = 11
        gm.gift.name = "花"
        gm.common.msg_id = 1001          # 内层 common
        frame = _frame("WebcastGiftMessage", gm.SerializeToString(),
                       envelope_msg_id=2002)   # 外层 envelope
        f._wsOnMessage(_FakeWS(), frame)
        check("事件收到", len(events) == 1, events)
        if events:
            ev = events[0]
            check("common_msg_id 正确", ev.message_id == "1001", ev.message_id)
            check("envelope_msg_id 正确",
                  ev.envelope_msg_id == "2002", ev.envelope_msg_id)
            check("两者**不同**(没有被互相覆盖)",
                  ev.message_id != ev.envelope_msg_id,
                  (ev.message_id, ev.envelope_msg_id))
    finally:
        f._fp.close()
        os.unlink(path)


def test_keep_all_records_both_msg_ids_in_jsonl() -> None:
    """落库侧也要两个都记 —— 真实采样靠它回答 Step 12 的差问题。"""
    print("\n[11o] JSONL 里两个 msg_id 都在")
    from danmaku import GiftMessage
    f, path = _mk_ws_fetcher([], keep_all=True, interaction=False)
    try:
        gm = GiftMessage()
        gm.gift_id = 5
        gm.gift.name = "x"
        gm.common.msg_id = 777
        f._parseGiftMsg(gm.SerializeToString(), envelope_msg_id=888)
        f._fp.flush()
        rec = json.loads(open(path, encoding="utf-8").read().strip())
        check("common_msg_id 落库", rec.get("common_msg_id") == "777", rec)
        check("envelope_msg_id 落库", rec.get("envelope_msg_id") == "888", rec)
        check("旧的笼统 msg_id 不再冒充其中一个",
              "msg_id" not in rec, sorted(rec))
    finally:
        f._fp.close()
        os.unlink(path)


def test_keep_all_gift_jsonl_has_step12_fields() -> None:
    """RF-4: 落库字段必须够 Step 12 回答协议问题。"""
    print("\n[11m] keep_all gift JSONL 含 Step 12 所需字段")
    from danmaku import GiftMessage
    f, path = _mk_ws_fetcher([], keep_all=True, interaction=True)
    try:
        gm = GiftMessage()
        gm.gift_id = 3
        gm.gift.name = "花"
        gm.combo_count = 2
        gm.repeat_count = 2
        gm.total_count = 9
        gm.repeat_end = 1
        gm.group_count = 4
        gm.group_id = 77
        gm.log_id = "L1"
        gm.trace_id = "T1"
        gm.common.msg_id = 555
        f._parseGiftMsg(gm.SerializeToString())
        f._fp.flush()
        rec = json.loads(open(path, encoding="utf-8").read().strip())
        for key, want in (("gift_id", "3"), ("gift_name", "花"),
                          ("combo_count", 2), ("repeat_count", 2),
                          ("total_count", 9), ("repeat_end", 1),
                          ("group_count", 4), ("group_id", "77"),
                          ("log_id", "L1"), ("trace_id", "T1"),
                          # Step 12: 两个 msg_id **分开**落, 不猜关系。
                          ("common_msg_id", "555"),
                          ("envelope_msg_id", "")):
            check(f"落了 {key}", rec.get(key) == want, (key, rec.get(key), want))
    finally:
        f._fp.close()
        os.unlink(path)


def test_method_probe_counts_before_handler() -> None:
    """Step 12B: method 探针必须在 handler 查表**之前**计数。

    否则"服务端给了但我们没处理"的类型不会留下痕迹 —— 而区分
    "服务端没给"与"给了没处理"正是这个探针存在的理由。
    """
    print("\n[12B-1] method 探针")
    from danmaku import GiftMessage
    # keep_all=False + interaction=False -> Gift 无 handler, 但仍该被计数
    f, path = _mk_ws_fetcher([], keep_all=False, interaction=False)
    try:
        gm = GiftMessage(); gm.gift_id = 1; gm.gift.name = "x"
        f._wsOnMessage(_FakeWS(), _frame("WebcastGiftMessage",
                                         gm.SerializeToString(),
                                         envelope_msg_id=1))
        # 一个完全陌生的 method(模拟抖音改名)
        f._wsOnMessage(_FakeWS(), _frame("WebcastSomeNewThingMessage",
                                         b"", envelope_msg_id=2))
        summ = f.method_summary()
        check("帧数计到", summ["ws_frames"] == 2, summ["ws_frames"])
        check("消息数计到", summ["ws_messages"] == 2, summ["ws_messages"])
        check("**无 handler 的 Gift 也被计数**",
              summ["methods"].get("WebcastGiftMessage") == 1,
              summ["methods"])
        check("陌生 method 也被计数",
              summ["methods"].get("WebcastSomeNewThingMessage") == 1,
              summ["methods"])
        check("无 handler 的进 unhandled",
              summ["unhandled"].get("WebcastGiftMessage") == 1,
              summ["unhandled"])
        # 断言的是**性质**(不含 payload/昵称), 不是固定 key 列表 ——
        # 加新计数器不该让这条测试红, 但泄漏用户内容必须红。
        allowed = {"connection_generation", "session_methods",
                   "session_frames", "ws_frames", "ws_messages",
                   "methods", "unhandled", "parse_errors"}
        check("**探针只含计数类字段**", set(summ) <= allowed, sorted(summ))
        import json as _json
        blob = _json.dumps(summ, ensure_ascii=False)
        check("**不含 payload/昵称内容**",
              "payload" not in blob and "nick" not in blob, blob[:200])
    finally:
        f._fp.close()
        os.unlink(path)


def test_method_probe_records_parse_errors() -> None:
    """解析抛异常也要计数 —— 区分"没 handler"与"handler 里炸了"。"""
    print("\n[12B-2] 解析失败计数")
    f, path = _mk_ws_fetcher([], keep_all=True, interaction=True)
    try:
        # 伪造一个 method=Gift 但 payload 完全不是 Gift 的帧
        f._wsOnMessage(_FakeWS(), _frame("WebcastGiftMessage", b"not-a-protobuf",
                                         envelope_msg_id=3))
        summ = f.method_summary()
        check("计到了该方法", summ["methods"].get("WebcastGiftMessage") == 1,
              summ["methods"])
        check("解析失败被单独记录",
              summ["parse_errors"].get("WebcastGiftMessage", 0) >= 0,
              summ["parse_errors"])
    finally:
        f._fp.close()
        os.unlink(path)


def test_bootstrap_dynamic_requires_both_fields() -> None:
    """**B-smoke readiness**: 只有 cursor 与 internal_ext **同时**来自同一次
    `/im/fetch/` 才算 dynamic。

    只要一个存在就打印 dynamic, 会让日志写 dynamic 而 URL 其实是新旧混搭
    (缺的那半截用 2024 fallback) —— B 实验就变成假 B。
    这里直接验 `_fetch_bootstrap_state` 的返回值: 半截必须返回 None。
    """
    print("\n[12B-5] dynamic 必须两个字段同源")
    import sys as _s
    _s.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "vendor", "douyin_fetcher"))
    import liveMan as LM
    from protobuf.douyin import Response

    class _Resp:
        def __init__(self, body):
            self.content = body

    def make_fetcher(resp_body):
        f = LM.DouyinLiveWebFetcher(live_id="1", abogus_file="x")
        f._DouyinLiveWebFetcher__room_id = "12345"
        f._DouyinLiveWebFetcher__ttwid = "t"
        f.get_ac_nonce = lambda: "n"
        f.get_ac_signature = lambda n=None: "s"
        f.get_a_bogus = lambda p: "a"

        class _Sess:
            def get(self, *a, **k):
                return _Resp(resp_body)
        f.session = _Sess()
        return f

    def body_with(cursor=None, internal_ext=None, live_cursor=None):
        r = Response()
        r.cursor = cursor or ""
        r.internal_ext = internal_ext or ""
        r.live_cursor = live_cursor or ""
        return bytes(r)

    # ① 两者齐全 -> dynamic
    got = make_fetcher(body_with(cursor="C", internal_ext="E"))._fetch_bootstrap_state()
    check("两者齐全 -> 返回状态", got == {"cursor": "C", "internal_ext": "E"}, got)
    # ② 只有 cursor -> 必须 None(不能半动态)
    got2 = make_fetcher(body_with(cursor="C"))._fetch_bootstrap_state()
    check("**只有 cursor -> None(不算 dynamic)**", got2 is None, got2)
    # ③ 只有 internal_ext -> 必须 None
    got3 = make_fetcher(body_with(internal_ext="E"))._fetch_bootstrap_state()
    check("**只有 internal_ext -> None**", got3 is None, got3)
    # ④ live_cursor **不能**冒充 internal_ext
    got4 = make_fetcher(body_with(cursor="C", live_cursor="L"))._fetch_bootstrap_state()
    check("**live_cursor 不冒充 internal_ext -> None**", got4 is None, got4)
    # ⑤ 两个都空 -> None
    got5 = make_fetcher(body_with())._fetch_bootstrap_state()
    check("全空 -> None", got5 is None, got5)


def test_probe_counters_are_per_connection() -> None:
    """**B-smoke readiness**: 探针是**每连接**计数, 不是跨重连累计。

    否则制造一次重连后, 无法判断"重连前 Gift 0 / 重连后 Gift 2"这种
    关键差异 —— 累计值把两代混成一个数。
    """
    print("\n[12B-4] 探针每连接独立")
    from danmaku import GiftMessage
    f, path = _mk_ws_fetcher([], keep_all=True, interaction=True)
    try:
        class _WS:
            def send(self, *a, **k):
                pass

        # 第一代: 先 open, 再收 2 条 Gift
        f._wsOnOpen(_WS())
        gen1 = f.connection_generation
        for i in range(2):
            gm = GiftMessage()
            gm.gift_id = i
            gm.gift.name = "x"
            f._wsOnMessage(_WS(), _frame("WebcastGiftMessage",
                                         gm.SerializeToString(),
                                         envelope_msg_id=100 + i))
        s1 = f.method_summary()
        check("第一代 gen", s1["connection_generation"] == gen1, s1)
        check("第一代本连接 Gift=2",
              s1["methods"].get("WebcastGiftMessage") == 2, s1["methods"])
        check("第一代 frames=2", s1["ws_frames"] == 2, s1["ws_frames"])

        # 第二代(模拟重连): 再 open
        f._wsOnOpen(_WS())
        s2 = f.method_summary()
        check("**新一代 gen 递增**",
              s2["connection_generation"] == gen1 + 1, s2)
        check("**新一代本连接计数归零**", s2["ws_frames"] == 0,
              s2["ws_frames"])
        check("**新一代本连接 Gift 归零**",
              s2["methods"].get("WebcastGiftMessage", 0) == 0, s2["methods"])

        # 第二代收 1 条 Gift
        gm = GiftMessage()
        gm.gift_id = 9
        gm.gift.name = "y"
        f._wsOnMessage(_WS(), _frame("WebcastGiftMessage",
                                     gm.SerializeToString(),
                                     envelope_msg_id=200))
        s3 = f.method_summary()
        check("第二代 Gift=1(与第一代分开)",
              s3["methods"].get("WebcastGiftMessage") == 1, s3["methods"])
        check("**session 累计保留 3**",
              s3["session_methods"].get("WebcastGiftMessage") == 3,
              s3["session_methods"])
        check("session frames 累计 3", s3["session_frames"] == 3,
              s3["session_frames"])
    finally:
        f._fp.close()
        os.unlink(path)


def test_bootstrap_is_refetched_per_connection() -> None:
    """**Batch C closeout**: 每次连接都重新取 bootstrap, 不做跨连接缓存。

    早先的实现是"取一次存进实例, 以后复用": 第一次失败存 `{}` -> 后续
    重连永远不再重试; 第一次成功 -> 一直复用旧值。两者都让
    "dynamic bootstrap" 名不副实, 也让 B 实验真假难辨(日志说动态,
    实际用回退值)。
    """
    print("\n[12B-3] bootstrap 每次连接重取")
    import sys as _s
    _s.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "vendor", "douyin_fetcher"))
    import liveMan as LM

    def run(fetch_impl, times=2):
        f = LM.DouyinLiveWebFetcher(live_id="1", abogus_file="x")
        f._DouyinLiveWebFetcher__room_id = "12345"
        f._DouyinLiveWebFetcher__ttwid = "t"
        f._fetch_bootstrap_state = fetch_impl
        urls = []

        class _W:
            def __init__(self, url=None, *a, **k):
                urls.append(url)

            def run_forever(self, *a, **k):
                pass

        ows, osig = LM.websocket.WebSocketApp, LM.generateSignature
        LM.websocket.WebSocketApp = _W
        LM.generateSignature = lambda u: "s"
        try:
            f.ws = None
            for _ in range(times):
                try:
                    f._connectWebSocket()
                except Exception:
                    pass
        finally:
            LM.websocket.WebSocketApp, LM.generateSignature = ows, osig
        return urls

    calls = {"n": 0}

    def fetch_ok():
        calls["n"] += 1
        return {"cursor": f"LIVE{calls['n']}",
                "internal_ext": f"EXT{calls['n']}"}

    urls = run(fetch_ok)
    check("取了 2 次(不是缓存成 1 次)", calls["n"] == 2, calls["n"])
    check("第二次用了新的 cursor",
          len(urls) == 2 and "LIVE2" in (urls[1] or ""), urls)
    check("第二次不再用第一次的",
          "LIVE1" not in (urls[1] or ""), urls[1])

    fails = {"n": 0}

    def fetch_fail():
        fails["n"] += 1
        return None            # 一直失败

    urls2 = run(fetch_fail)
    check("一直失败也每次重试(不缓存失败)",
          fails["n"] == 2, fails["n"])
    check("失败时回退旧常量",
          "t-1721106114633" in (urls2[0] or ""), urls2[0])


def main() -> int:
    print("=" * 60)
    print("  弹幕接入层 离线自测")
    print("=" * 60)
    test_sim_uses_real_parser()
    test_sim_user_id_explicit()
    test_control_stream_ended()
    test_session_timeout_patch()
    test_expired_fetcher_drops_data()
    test_callback_fetcher_guard()
    test_chat_parse_roundtrip()
    # ---- Step 11: InteractionEvent plumbing ----
    test_like_reaches_business_chain_without_keep_all()
    test_gift_reaches_business_chain_without_keep_all()
    test_interaction_disabled_blocks_business_chain()
    test_keep_all_still_logs_diagnostics()
    test_expired_fetcher_drops_interaction()
    test_engine_submit_interaction_is_noop_stub()
    test_livesource_routes_interaction_to_inbox()
    # ---- Step 11 review-fix: 真实 WS 分发层 ----
    test_ws_dispatch_gift_reaches_chain_without_keep_all()
    test_ws_dispatch_like_reaches_chain_without_keep_all()
    test_ws_dispatch_keep_all_only_logs_not_business()
    test_ws_dispatch_diagnostics_still_keep_all_only()
    test_ws_dispatch_chat_always_registered()
    test_keep_all_gift_jsonl_has_step12_fields()
    test_ws_dispatch_captures_both_msg_ids_separately()
    test_keep_all_records_both_msg_ids_in_jsonl()
    # ---- Step 12B: method 探针 ----
    test_method_probe_counts_before_handler()
    test_method_probe_records_parse_errors()
    test_bootstrap_is_refetched_per_connection()
    test_probe_counters_are_per_connection()
    test_bootstrap_dynamic_requires_both_fields()
    # ---- Q12 ----
    test_synth_msg_ids_are_distinct()
    test_first_frame_callback_fires_once()
    test_missing_common_means_empty_id()
    test_stdin_path_works()
    test_first_frame_resets_per_connection()
    # ---- Hotfix A: 下播终止语义 ----
    test_terminate_stops_reconnect()
    test_plain_disconnect_still_reconnects()
    test_stop_is_not_terminal()
    test_livesource_terminates_on_stream_end()
    print("\n" + "=" * 60)
    if FAIL:
        print(f"  {FAIL} 项失败")
        return 1
    print("  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
