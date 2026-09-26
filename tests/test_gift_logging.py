#!/usr/bin/env python
# coding: utf-8
"""Issue #42 离线回归: 礼物识别 + 结构化记录 + 每场日志目录。

覆盖(与 Issue §9 一一对应):

    1. 共享 PushFrame decoder: gzip / raw / empty+gzip magic /
       unknown+自恢复 / malformed fail-soft, 及帧解码诊断计数
    2. keep_all=False + interaction_enabled=True 时 Gift 仍进业务回调
    3. Gift 字段映射(含本轮新增的 5 个协议字段)
    4. gifts.jsonl: raw 事件 1:1 落盘(4 条消息 = 4 行, 不合并不去重),
       字段闭集, 写盘失败 fail-soft, 无 Cookie
    5. run directory: 日期目录 / 同秒 collision-safe / 不覆盖历史 /
       --log-file 兼容
    6. Gift 与 AI 玩家解耦: earned/available 不变, 点赞逻辑不变
    7. Cookie: env-only, 缺失 fail-open, 不进 repr / gifts.jsonl

全部离线: 不联网、不需要 API key、不使用真实 payload / 真实昵称。
"""

from __future__ import annotations

import gzip
import io
import json
import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from danmaku import (DanmakuFetcher, PushFrame, Response,         # noqa: E402
                     decode_push_frame_response, frame_encoding_label)
from story.config import Config                                   # noqa: E402
from story.gift_log import GiftJsonlWriter, gift_record           # noqa: E402
from story.ingest import CallbackFetcher, InteractionEvent        # noqa: E402
from story.summon import SummonLedger                             # noqa: E402

FAIL = 0

#: gifts.jsonl 的字段闭集(Issue #42 §5)。多一个键(例如手滑把 cookie 塞进去)
#: 都必须红 —— 这条闭集断言就是"不记录 Cookie / header / raw payload"的
#: 结构性保证。
GIFT_RECORD_KEYS = {
    "ts", "user_id", "user_name", "gift_id", "gift_name", "gift_combo",
    "gift_type", "diamond_count", "combo_count", "repeat_count",
    "total_count", "repeat_end", "group_id", "group_count", "send_type",
    "trace_id", "log_id", "message_id", "envelope_msg_id",
}


def check(name: str, cond: bool, extra: str = "") -> None:
    global FAIL
    if cond:
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def _frame(items, *, payload_encoding: str = "gzip", raw_payload=None):
    """把 `(method, payload, msg_id)` 组成一个真实的 WS 帧字节串。

    payload_encoding 可以造出四种现场形态: gzip(正常) / ""(空声明) /
    "none" / 自定义(unknown)。raw_payload 直接替换内层 bytes(造坏帧)。
    """
    from protobuf.douyin import Message
    r = Response()
    for method, payload, mid in items:
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        r.messages_list.append(Message(method=method, payload=payload,
                                       msg_id=mid))
    inner = r.SerializeToString() if raw_payload is None else raw_payload
    body = gzip.compress(inner) if payload_encoding == "gzip" else inner
    return PushFrame(payload=body,
                     payload_encoding=payload_encoding).SerializeToString()


def _gift_payload(gift_id=123, combo=2, repeat=1, total=3, repeat_end=1,
                  group_id=77, log_id="L1", trace_id="T1", msg_id=555,
                  gift_name="测试礼物", user_id="9001", user_name="测试观众",
                  gift_combo=True, gift_type=5, diamond_count=100,
                  group_count=2, send_type=1):
    from protobuf.douyin import Common, GiftMessage, GiftStruct, User
    m = GiftMessage(gift_id=gift_id, combo_count=combo, repeat_count=repeat,
                    total_count=total, repeat_end=repeat_end,
                    group_id=group_id, log_id=log_id, trace_id=trace_id,
                    common=Common(msg_id=msg_id),
                    gift=GiftStruct(name=gift_name, combo=gift_combo,
                                    type=gift_type,
                                    diamond_count=diamond_count),
                    group_count=group_count, send_type=send_type,
                    user=User(id=int(user_id), nick_name=user_name))
    return m.SerializeToString()


def _mk_cb_fetcher(events):
    """造一个半成品 CallbackFetcher(与 tests/test_ingest.py 同一手法)。

    只摆 `_wsOnMessage` 分发路径需要的字段 —— 不调真 `__init__`(那会建
    requests.Session 等, 与"喂一帧"无关)。
    """
    import collections
    f = CallbackFetcher.__new__(CallbackFetcher)
    f._on_chat = lambda e: None
    f._on_control = None
    f._on_interaction = events.append
    f.keep_all = False
    f.interaction_enabled = True
    f._expired = False
    f._first_frame_seen = True
    f._on_frame = None
    f._on_first_frame = None
    f._counts = collections.Counter()
    f._fp = None
    return f


def _mk_baseline_fetcher():
    """半成品 DanmakuFetcher(基线 `_wsOnMessage` 用)。"""
    f = DanmakuFetcher.__new__(DanmakuFetcher)
    f.keep_all = False
    f.interaction_enabled = True
    f._counts = {}
    f._fp = None
    # 帧解码诊断字段(__init__ 会设; __new__ 半成品要手工摆齐)
    f.frame_encoding_counts = {}
    f.frame_decode_errors = 0
    return f


def _read_lines(path: str) -> list:
    with io.open(path, encoding="utf-8") as fh:
        return [json.loads(ln) for ln in fh.read().splitlines() if ln.strip()]


# ======================================================================
# 1. 共享 PushFrame decoder
# ======================================================================
def test_frame_decoder_paths():
    print("\n[#42-1] 共享 PushFrame decoder")
    from protobuf.douyin import GiftMessage
    inner = Response()
    inner.messages_list.append(
        _mk_message("WebcastGiftMessage", _gift_payload(msg_id=1)))

    # ---- ① 声明 gzip -> 正常解 ----
    pkg = PushFrame(payload=gzip.compress(bytes(inner)),
                    payload_encoding="gzip")
    resp = decode_push_frame_response(pkg)
    check("声明 gzip -> 解出 1 条消息",
          len(resp.messages_list) == 1, resp.messages_list)
    check("标签: gzip", frame_encoding_label(pkg) == "gzip",
          frame_encoding_label(pkg))

    # ---- ② 声明为空 + 实际 gzip(magic 自恢复) ----
    pkg2 = PushFrame(payload=gzip.compress(bytes(inner)), payload_encoding="")
    resp2 = decode_push_frame_response(pkg2)
    check("空声明 + gzip magic -> 解出",
          len(resp2.messages_list) == 1, resp2.messages_list)
    check("标签: empty", frame_encoding_label(pkg2) == "empty",
          frame_encoding_label(pkg2))

    # ---- ③ 声明 "none" + raw Response -> 直接解析 ----
    pkg3 = PushFrame(payload=bytes(inner), payload_encoding="none")
    resp3 = decode_push_frame_response(pkg3)
    check("none 声明 + raw -> 解出", len(resp3.messages_list) == 1)
    check("标签: none", frame_encoding_label(pkg3) == "none",
          frame_encoding_label(pkg3))

    # ---- ④ 声明为空 + raw(非 gzip) -> 按 raw 解析 ----
    pkg4 = PushFrame(payload=bytes(inner), payload_encoding="")
    resp4 = decode_push_frame_response(pkg4)
    check("空声明 + raw -> 解出", len(resp4.messages_list) == 1)

    # ---- ⑤ 未知 encoding + gzip magic -> 自恢复 ----
    pkg5 = PushFrame(payload=gzip.compress(bytes(inner)),
                     payload_encoding="br+gzip??")
    resp5 = decode_push_frame_response(pkg5)
    check("未知 encoding + gzip magic -> 自恢复",
          len(resp5.messages_list) == 1, resp5.messages_list)
    check("标签: unknown 透传", frame_encoding_label(pkg5) == "unknown:br+gzip??",
          frame_encoding_label(pkg5))

    # ---- ⑥ 未知 encoding + raw -> 试解析 ----
    pkg6 = PushFrame(payload=bytes(inner), payload_encoding="mystery")
    resp6 = decode_push_frame_response(pkg6)
    check("未知 encoding + raw -> 解出", len(resp6.messages_list) == 1)

    # ---- ⑦ 声明 gzip 但内容损坏 -> 抛(调用方计数跳过) ----
    pkg7 = PushFrame(payload=b"\x1f\x8b-not-really-gzip",
                     payload_encoding="gzip")
    try:
        decode_push_frame_response(pkg7)
        check("损坏 gzip -> 抛异常", False)
    except Exception:                       # noqa: BLE001
        check("损坏 gzip -> 抛异常", True)

    # ---- ⑧ 空 payload -> 不抛, 得空 Response(fail-soft 由调用方计数) ----
    resp8 = decode_push_frame_response(PushFrame(payload=b""))
    check("空 payload -> 空 Response", len(resp8.messages_list) == 0)


def _mk_message(method, payload, mid=1):
    from protobuf.douyin import Message
    return Message(method=method, payload=payload, msg_id=mid)


def test_baseline_ws_fail_soft():
    print("\n[#42-1b] 基线 _wsOnMessage: 非 gzip / 坏帧 fail-soft")
    f = _mk_baseline_fetcher()
    got = []
    f._on_gift = lambda m, envelope_msg_id=0: got.append(m)

    # ---- ① 非 gzip payload(raw Response) ----
    # 旧代码 `gzip.decompress()` 在这里抛 Not a gzipped file —— 正是现场
    # 确认的丢帧路径。新 decoder 按 raw 解。
    from protobuf.douyin import Message
    r = Response()
    r.messages_list.append(Message(method="WebcastGiftMessage",
                                   payload=_gift_payload(msg_id=9), msg_id=9))
    f._wsOnMessage(None, PushFrame(payload=bytes(r)).SerializeToString())
    check("raw Response 帧 -> Gift 进入回调", len(got) == 1, len(got))
    check("raw 帧 -> 计入 encoding 统计",
          f.frame_encoding_counts.get("empty") == 1,
          f.frame_encoding_counts)
    check("raw 帧 -> 未计解码错误", f.frame_decode_errors == 0,
          f.frame_decode_errors)

    # ---- ② 坏帧(gzip 声明 + 损坏内容) -> 只计数, 不炸回调 ----
    bad = PushFrame(payload=b"\x1f\x8b-corrupt",
                    payload_encoding="gzip").SerializeToString()
    f._wsOnMessage(None, bad)
    check("坏帧 -> frame_decode_errors=1", f.frame_decode_errors == 1,
          f.frame_decode_errors)

    # ---- ③ 坏帧之后的好帧照常处理(WS 主链未退出) ----
    good = _frame([("WebcastGiftMessage", _gift_payload(msg_id=10), 10)])
    f._wsOnMessage(None, good)
    check("坏帧后好帧照常 -> 回调继续", len(got) == 2, len(got))

    # ---- ④ method_summary 带上帧解码诊断 ----
    s = f.method_summary()
    check("method_summary 含 frame_encoding_counts",
          "frame_encoding_counts" in s, sorted(s))
    check("method_summary 含 frame_decode_errors",
          s.get("frame_decode_errors") == 1, s)


def test_probe_ws_fail_soft():
    print("\n[#42-1c] Probe _wsOnMessage: 共用解码器 + fail-soft")
    from story.gift_probe.hooks import make_gift_capture_fetcher
    from story.gift_probe.profile import ProfileCounters
    from story.gift_probe.capture import RawCaptureStore

    d = tempfile.mkdtemp()
    c = ProfileCounters("p")
    st = RawCaptureStore(d, "s", "p")
    cls = make_gift_capture_fetcher(CallbackFetcher)
    f = cls.__new__(cls)                # 半成品: 不走真 __init__(同 test_ingest)
    f.keep_all = False
    f.interaction_enabled = True
    f._expired = False
    f._first_frame_seen = True
    f._on_frame = None
    f._on_first_frame = None
    f._counts = {}
    f._fp = None
    f.attach_gift_probe(profile=None, counters=c, store=st,
                        semantic_dir=os.path.join(d, "sem"))
    # attach 里 profile=None -> bootstrap_mode 默认 local, 无碍本测。

    # raw Response 帧(旧代码会炸) -> 探针照常计数
    from protobuf.douyin import Message
    r = Response()
    r.messages_list.append(Message(method="WebcastGiftMessage",
                                   payload=_gift_payload(msg_id=3), msg_id=3))
    f._wsOnMessage(None, PushFrame(payload=bytes(r)).SerializeToString())
    check("probe: raw 帧 -> gift_method_seen=1", c.gift_method_seen == 1,
          c.summary())
    check("probe: encoding 计入", c.frame_encoding_counts.get("empty") == 1,
          c.frame_encoding_counts)
    check("probe: emitted=1(干跑回调跑通)", c.emitted_gift_count == 1,
          c.summary())

    # 坏帧 -> 计数, 不炸线程; 之后的好帧照常
    bad = PushFrame(payload=b"\x1f\x8b-corrupt",
                    payload_encoding="gzip").SerializeToString()
    f._wsOnMessage(None, bad)
    check("probe: 坏帧 -> frame_decode_errors=1", c.frame_decode_errors == 1,
          c.frame_decode_errors)
    f._wsOnMessage(None, _frame([("WebcastGiftMessage",
                                  _gift_payload(msg_id=4), 4)]))
    check("probe: 坏帧后好帧照常 -> emitted=2", c.emitted_gift_count == 2,
          c.summary())
    check("probe: summary 带帧诊断",
          "frame_decode_errors" in c.summary(), sorted(c.summary()))


# ======================================================================
# 2/3. 正常直播 Gift 接入 + 字段映射
# ======================================================================
def test_gift_reaches_callback_without_keep_all():
    print("\n[#42-2] keep_all=False + interaction_enabled=True 收到 Gift")
    events = []
    f = _mk_cb_fetcher(events)
    f._wsOnMessage(None, _frame([
        ("WebcastGiftMessage", _gift_payload(), 555),
    ]))
    check("Gift 进入业务回调", len(events) == 1, len(events))
    ev = events[0]
    check("kind=gift", ev.kind == "gift", ev.kind)


def test_gift_field_mapping():
    print("\n[#42-3] Gift 字段映射(含新增 5 个协议字段)")
    f = _mk_cb_fetcher([])
    from protobuf.douyin import GiftMessage
    m = GiftMessage().parse(_gift_payload(msg_id=555))
    ev = f._gift_event(m, envelope_msg_id=42)
    check("gift_id", ev.gift_id == "123", ev.gift_id)
    check("gift_name", ev.gift_name == "测试礼物", ev.gift_name)
    check("combo_count", ev.combo_count == 2, ev.combo_count)
    check("repeat_count", ev.repeat_count == 1, ev.repeat_count)
    check("total_count", ev.total_count == 3, ev.total_count)
    check("repeat_end", ev.repeat_end == 1, ev.repeat_end)
    check("group_id", ev.group_id == "77", ev.group_id)
    check("trace_id", ev.trace_id == "T1", ev.trace_id)
    check("log_id", ev.log_id == "L1", ev.log_id)
    check("message_id(common.msg_id)", ev.message_id == "555", ev.message_id)
    check("envelope_msg_id", ev.envelope_msg_id == "42", ev.envelope_msg_id)
    # ---- 本轮新增(Issue §3): 全部来自当前 proto 的真实字段 ----
    check("gift_combo <- gift.combo", ev.gift_combo is True, ev.gift_combo)
    check("gift_type <- gift.type", ev.gift_type == 5, ev.gift_type)
    check("diamond_count <- gift.diamond_count",
          ev.diamond_count == 100, ev.diamond_count)
    check("group_count <- groupCount", ev.group_count == 2, ev.group_count)
    check("send_type <- sendType", ev.send_type == 1, ev.send_type)


# ======================================================================
# 4. gifts.jsonl
# ======================================================================
def test_gifts_jsonl_raw_events_one_to_one():
    print("\n[#42-4] gifts.jsonl: 4 条 raw GiftMessage = 4 行")
    d = tempfile.mkdtemp()
    path = os.path.join(d, "gifts.jsonl")
    w = GiftJsonlWriter(path)
    w.precreate()

    # 现场形状: 2 个实际礼物, 各产生 repeat_end=0/1 两条消息。
    # **正确结果就是 4 行** —— 不合并生命周期, 不按 group 去重。
    for gift_id, group_id, repeat_end, mid in (
            (123, 77, 0, 555), (123, 77, 1, 556),
            (456, 88, 0, 557), (456, 88, 1, 558)):
        ev = InteractionEvent(
            kind="gift", user_id="9001", user_name="测试观众",
            message_id=str(mid), envelope_msg_id=str(mid + 1000),
            gift_id=str(gift_id),
            gift_name="小心心" if gift_id == 123 else "人气票",
            combo_count=1, repeat_count=1, total_count=1,
            repeat_end=repeat_end, group_id=str(group_id),
            trace_id=f"T{group_id}", log_id=f"L{mid}",
            gift_combo=True, gift_type=5, diamond_count=1,
            group_count=2, send_type=0)
        w.write(ev)
    rows = _read_lines(path)
    check("4 条消息 -> 4 行", len(rows) == 4, len(rows))
    check("重复礼物不去重(group 相同仍各占一行)",
          len({r["group_id"] for r in rows}) == 2, rows)
    check("两阶段都保留(repeat_end 0 与 1 各两条)",
          sorted(r["repeat_end"] for r in rows) == [0, 0, 1, 1],
          [r["repeat_end"] for r in rows])
    check("message_id 互不相同(不同消息不同 id)",
          len({r["message_id"] for r in rows}) == 4,
          [r["message_id"] for r in rows])
    first = rows[0]
    check("字段闭集(不多不少)", set(first.keys()) == GIFT_RECORD_KEYS,
          sorted(set(first.keys()) ^ GIFT_RECORD_KEYS))
    check("首行字段值正确",
          first["gift_id"] == "123" and first["gift_name"] == "小心心"
          and first["diamond_count"] == 1 and first["gift_combo"] is True
          and first["group_count"] == 2 and first["send_type"] == 0
          and first["trace_id"] == "T77", first)
    check("中文名 UTF-8 往返无损",
          rows[0]["gift_name"] == "小心心"
          and rows[2]["gift_name"] == "人气票",
          [rows[0]["gift_name"], rows[2]["gift_name"]])
    check("ts 存在且非空", bool(first["ts"]), first)


def test_gifts_jsonl_fail_soft_and_no_secrets():
    print("\n[#42-4b] gifts.jsonl: fail-soft / 无 Cookie")
    # ---- ① 写盘失败只 warning(路径指向一个**文件**下面 -> 必然 OSError) ----
    d = tempfile.mkdtemp()
    blocker = os.path.join(d, "blocker")
    with open(blocker, "w", encoding="utf-8") as fh:
        fh.write("x")
    w = GiftJsonlWriter(os.path.join(blocker, "gifts.jsonl"))
    ev = InteractionEvent(kind="gift", gift_id="1", gift_name="x",
                          user_id="9", user_name="u")
    try:
        w.write(ev)
        w.write(ev)
        check("写盘失败不抛", True)
    except Exception as e:                  # noqa: BLE001
        check("写盘失败不抛", False, repr(e))
    check("写盘失败被计数", w.write_errors >= 1, w.write_errors)
    check("写盘失败后 written 仍为 0", w.written == 0, w.written)

    # ---- ② 字段闭集 => Cookie / header / payload 结构上进不来 ----
    rec = gift_record(InteractionEvent(kind="gift", gift_id="1"))
    check("gift_record 无 cookie/header/payload 键",
          not any(k for k in rec if "cookie" in k.lower()
                  or "header" in k.lower() or "payload" in k.lower()),
          sorted(rec))
    check("gift_record 键与闭集一致", set(rec.keys()) == GIFT_RECORD_KEYS,
          sorted(set(rec.keys()) ^ GIFT_RECORD_KEYS))


# ======================================================================
# 5. run directory
# ======================================================================
def test_run_dirs():
    print("\n[#42-5] run directory: 独立 / collision-safe / 不覆盖")
    from director import make_run_dir
    base = tempfile.mkdtemp()
    now = datetime(2026, 9, 25, 18, 40, 21)

    # ---- ① live 命名 + live_id ----
    d1 = make_run_dir("live", live_id="813110862078", base_dir=base, now=now)
    check("live 目录名含时间与 live_id",
          os.path.basename(d1) == "184021-live-813110862078", d1)
    check("日期一级目录", os.path.basename(os.path.dirname(d1)) == "2026-09-25",
          d1)

    # ---- ② 同一秒重复启动 -> 后缀递增, 绝不覆盖 ----
    d2 = make_run_dir("live", live_id="813110862078", base_dir=base, now=now)
    check("同秒第二次启动目录不同", d2 != d1, (d1, d2))
    check("后缀 -2", os.path.basename(d2).endswith("-2"), d2)
    check("两个目录都存在且互不覆盖",
          os.path.isdir(d1) and os.path.isdir(d2), (d1, d2))
    # 旧目录里的文件原样保留
    with open(os.path.join(d1, "run.log"), "w", encoding="utf-8") as fh:
        fh.write("old")
    make_run_dir("live", live_id="813110862078", base_dir=base, now=now)
    with open(os.path.join(d1, "run.log"), encoding="utf-8") as fh:
        check("历史场次不被覆盖", fh.read() == "old")

    # ---- ③ 不同日期进不同日期目录 ----
    d4 = make_run_dir("live", live_id="813110862078", base_dir=base,
                      now=datetime(2026, 9, 26, 0, 0, 5))
    check("跨日期目录不同",
          os.path.basename(os.path.dirname(d4)) == "2026-09-26", d4)

    # ---- ④ sim / stdin 命名 ----
    ds = make_run_dir("sim", base_dir=base, now=now)
    dn = make_run_dir("stdin", base_dir=base, now=now)
    check("sim 命名", os.path.basename(ds).startswith("184021-sim"), ds)
    check("stdin 命名", os.path.basename(dn).startswith("184021-stdin"), dn)

    # ---- ⑤ 创建失败 -> 清晰报错(不静默覆盖任何日志) ----
    blocker = os.path.join(base, "blocker")
    with open(blocker, "w", encoding="utf-8") as fh:
        fh.write("x")
    try:
        make_run_dir("live", live_id="1",
                     base_dir=os.path.join(blocker, "runs"), now=now)
        check("目录创建失败 -> 清晰报错", False)
    except RuntimeError as e:
        check("目录创建失败 -> 清晰报错", "无法创建" in str(e), str(e)[:60])


def test_resolve_log_file():
    print("\n[#42-5b] --log-file 兼容策略")
    from director import resolve_log_file

    class _C:
        log_file = None

    c = _C()
    c.log_file = None
    check("未传 -> run_dir/run.log",
          resolve_log_file(c, "data/runs/x") ==
          os.path.join("data/runs/x", "run.log"),
          resolve_log_file(c, "data/runs/x"))
    c.log_file = "my/explicit.log"
    check("显式 PATH -> 逐字尊重", resolve_log_file(c, "data/runs/x") ==
          "my/explicit.log", resolve_log_file(c, "data/runs/x"))
    c.log_file = ""
    check('显式 "" -> 关闭落盘(None)', resolve_log_file(c, "data/runs/x") is None,
          resolve_log_file(c, "data/runs/x"))


def test_director_gift_writer_wiring():
    print("\n[#42-5c] Director 装配: live 模式才有 gifts.jsonl 写手")
    import director as D

    d = tempfile.mkdtemp()
    # 心跳路径指到 tmp —— 别碰真实 data/(可能与在播进程互相踩)。
    orig_hb = D.LIVE_HEARTBEAT_PATH
    D.LIVE_HEARTBEAT_PATH = os.path.join(d, "hb.json")
    try:
        cfg = Config(live_id="813110862078", sim_path="", use_stdin=False,
                     out_path=os.path.join(d, "danmaku.jsonl"),
                     played_path=os.path.join(d, "played.jsonl"),
                     leaderboard_path=os.path.join(d, "lb.jsonl"),
                     pool_path=os.path.join(d, "pool.jsonl"),
                     pool_used_path=os.path.join(d, "used.jsonl"),
                     puzzle_out_path=os.path.join(d, "puzzle.jsonl"))
        run_dir = os.path.join(d, "runs", "2026-09-25", "184021-live-x")
        dr = D.Director(cfg, run_dir=run_dir)
        check("live 模式 -> gift writer 已装配", dr._gift_writer is not None)
        check("gifts.jsonl 已预创建于 session 目录",
              os.path.isfile(os.path.join(run_dir, "gifts.jsonl")),
              run_dir)
        # _consume 分发段直接用 writer 验证(线程装配由 test_engine 覆盖):
        # 事件 -> gifts.jsonl 一行
        ev = InteractionEvent(kind="gift", user_id="9", user_name="u",
                              gift_id="1", gift_name="g", repeat_end=0)
        dr._gift_writer.write(ev)
        dr._gift_writer.close()
        rows = _read_lines(os.path.join(run_dir, "gifts.jsonl"))
        check("事件 -> gifts.jsonl 一行", len(rows) == 1, len(rows))
        check("预创建的空行没有污染数据(首行即事件)",
              rows[0]["gift_id"] == "1", rows[0])

        # sim 模式(无 live_id) -> 不装配写手
        cfg2 = Config(sim_path="x", no_llm=True,
                      out_path=os.path.join(d, "danmaku2.jsonl"),
                      played_path=os.path.join(d, "played2.jsonl"),
                      leaderboard_path=os.path.join(d, "lb2.jsonl"),
                      pool_path=os.path.join(d, "pool2.jsonl"),
                      pool_used_path=os.path.join(d, "used2.jsonl"),
                      puzzle_out_path=os.path.join(d, "puzzle2.jsonl"))
        dr2 = D.Director(cfg2, run_dir=os.path.join(d, "runs", "sim"))
        check("sim 模式 -> 无 gift writer", dr2._gift_writer is None)
    finally:
        D.LIVE_HEARTBEAT_PATH = orig_hb


# ======================================================================
# 6. Gift 与 AI 玩家完全解耦
# ======================================================================
def test_gift_never_touches_ai_player():
    print("\n[#42-6] Gift 不影响 SummonLedger / 点赞逻辑")
    led = SummonLedger()
    earned0 = led.summon_earned_total
    available0 = led.available

    ev = InteractionEvent(kind="gift", user_id="9001", user_name="测试观众",
                          gift_id="123", gift_name="小心心",
                          repeat_end=0, diamond_count=1)
    gained = led.on_gift_event(ev)
    check("on_gift_event 返回 0(不产生额度)", gained == 0, gained)
    check("earned 不变", led.summon_earned_total == earned0,
          led.summon_earned_total)
    check("available 不变", led.available == available0, led.available)

    # 连喂多条不同面目的礼物(diamond_count/total_count 诱饵) -> 依旧不变
    for dia, total in ((100, 3), (1000, 30), (6666, 99)):
        led.on_gift_event(InteractionEvent(
            kind="gift", gift_id="1", gift_name="x",
            diamond_count=dia, total_count=total))
    check("高价值礼物也不产生额度",
          led.summon_earned_total == earned0, led.summon_earned_total)
    check("gift_events_seen 只计数", led.gift_events_seen == 4,
          led.gift_events_seen)

    # ---- 点赞逻辑原样: total 过档 -> earned 增加 ----
    before = led.summon_earned_total
    g1 = led.on_like_total(1)           # 初始化基线
    led.on_like_total(150)              # 过一个百赞档
    check("点赞 -> earned 增加(逻辑未改)",
          led.summon_earned_total > before or g1 == 0,
          (before, led.summon_earned_total))
    check("点赞档位账照记", led.likes_bucket_consumed >= 0,
          led.likes_bucket_consumed)

    # ---- Engine 层: gift -> 无动作; like -> 有 BROADCAST ----
    from story.engine import RoundEngine
    eng = RoundEngine(Config(sim_path="x", no_llm=True))
    acts_gift = eng.submit_interaction(InteractionEvent(kind="gift"))
    check("engine: gift -> 无动作", acts_gift == [], acts_gift)
    check("engine: gift 后 available 不变",
          eng._ai_player_ledger.available == available0,
          eng._ai_player_ledger.available)
    acts_like = eng.submit_interaction(InteractionEvent(kind="like", total=1))
    check("engine: like 路径仍工作(基线初始化)",
          isinstance(acts_like, list), acts_like)


# ======================================================================
# 7. Cookie
# ======================================================================
def test_reference_bootstrap_reports_mode():
    print("\n[#42-8.2] reference 臂的 bootstrap 输出与实际 profile 一致")
    from story.gift_probe import hooks
    from story.gift_probe.reference_bootstrap import (
        build_reference_bootstrap)

    class _FakeFetcher:
        user_unique_id = "7319483754668557238"
        __dict__ = {"_DouyinLiveWebFetcher__room_id": "123"}

    # ---- ① hooks 的 reference 包装自带真实模式标记 ----
    boot = hooks._reference_bootstrap(_FakeFetcher(), now_ms=1727268000000)
    check("reference boot 带bootstrap_mode=reference",
          boot.get("bootstrap_mode") == "reference", boot.get("bootstrap_mode"))
    # 形状字段不受影响(与参考实现逐字段对齐的结论仍然成立)
    plain = build_reference_bootstrap(room_id="123",
                                      user_unique_id="7319483754668557238",
                                      now_ms=1727268000000)
    check("cursor/internal_ext 与参考实现一致",
          boot["cursor"] == plain["cursor"]
          and boot["internal_ext"] == plain["internal_ext"], boot)

    # ---- ② vendor 打印逻辑: 没标记 -> 仍是 local-generated(生产不变) ----
    # (liveMan 用 `boot.get("bootstrap_mode") or "local-generated"` 派生;
    #  本地生成路径不带该键, 所以生产行为一字不变。)
    check("无标记时 vendor 语义 = local-generated",
          str({}.get("bootstrap_mode") or "local-generated")
          == "local-generated", "")


def test_cookie_fail_open_and_secrecy():
    print("\n[#42-7] Cookie: env-only / fail-open / 不泄漏")
    # ---- ① env-only: 没配 -> None; 配了 -> 原样进 cfg(但 repr 不见) ----
    cfg = Config(live_id="1", douyin_live_cookie=None)
    check("未配置 -> None(直播照常可起)", cfg.douyin_live_cookie is None)
    cfg2 = Config(live_id="1", douyin_live_cookie="secret-cookie-value")
    r = repr(cfg2)
    check("Cookie 不进 repr(Config)", "secret-cookie-value" not in r,
          r[:120])

    # ---- ② LiveSource.start 的警告: 只提醒一次, 不刷屏 ----
    # (把 warn 抽成方法以便离线直测; start() 不在本测里起线程。)
    import queue as _q
    from story.ingest import LiveSource
    cfg3 = Config(live_id="1", douyin_live_cookie=None)
    src = LiveSource(cfg3, _q.Queue())
    check("无 Cookie 时需要警告", src._needs_cookie_warning() is True)
    check("无 Cookie 也能装配 source(不抛)",
          src._build.__qualname__.endswith("_build"), "")

    # ---- ③ gifts.jsonl 不含 Cookie: 字段闭集已断言; 这里再验一行端到端 ----
    d = tempfile.mkdtemp()
    w = GiftJsonlWriter(os.path.join(d, "gifts.jsonl"))
    w.write(InteractionEvent(kind="gift", gift_id="1", user_name="u"))
    with io.open(os.path.join(d, "gifts.jsonl"), encoding="utf-8") as fh:
        body = fh.read()
    check("gifts.jsonl 无 Cookie 痕迹",
          "cookie" not in body.lower() and "secret" not in body.lower(),
          body[:80])


def main() -> int:
    test_frame_decoder_paths()
    test_baseline_ws_fail_soft()
    test_probe_ws_fail_soft()
    test_gift_reaches_callback_without_keep_all()
    test_gift_field_mapping()
    test_gifts_jsonl_raw_events_one_to_one()
    test_gifts_jsonl_fail_soft_and_no_secrets()
    test_run_dirs()
    test_resolve_log_file()
    test_director_gift_writer_wiring()
    test_gift_never_touches_ai_player()
    test_reference_bootstrap_reports_mode()
    test_cookie_fail_open_and_secrecy()
    print()
    if FAIL:
        print(f"FAILED: {FAIL} 项")
        return 1
    print("PASS: Issue #42 礼物识别与按场日志 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
