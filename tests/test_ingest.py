#!/usr/bin/env python
# coding: utf-8
"""离线自测: 弹幕接入层。`uv run tests/test_ingest.py`

关键点: 验证 SimSource 走的是**真实** protobuf + **真实** _parseChatMsg。
"""

from __future__ import annotations

import os
import queue
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from story.config import Config
from story.ingest import CallbackFetcher, ChatEvent, SimSource, StdinSource

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
    cfg = Config(sim_path="data/demo_script.jsonl", sim_loop_gap=1, no_llm=True)
    inbox: queue.Queue = queue.Queue()
    src = SimSource(cfg, inbox)

    # 直接驱动一次, 不启线程
    src._load()
    check("脚本载入", len(src._loop_items) == 17, f"got {len(src._loop_items)}")
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
    print("\n" + "=" * 60)
    if FAIL:
        print(f"  {FAIL} 项失败")
        return 1
    print("  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
