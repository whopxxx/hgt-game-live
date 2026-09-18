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
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from danmaku import ChatMessage           # 从 danmaku 转出(它会先把 vendor 加进路径)
from story.config import Config
from story.ingest import CallbackFetcher, ChatEvent, SimSource, StdinSource

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
    # ---- Q12 ----
    test_synth_msg_ids_are_distinct()
    test_first_frame_callback_fires_once()
    test_missing_common_means_empty_id()
    print("\n" + "=" * 60)
    if FAIL:
        print(f"  {FAIL} 项失败")
        return 1
    print("  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
