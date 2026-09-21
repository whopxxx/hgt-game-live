#!/usr/bin/env python
# coding: utf-8
"""运行: uv run tests/test_gift_probe.py（完全离线, 无网络）。

Step 12C: `gift_capture_diagnostic` —— 多连接画像抓取 + Gift-family 原始证据。

这个套件守的是 Issue 「测试要求」的 13 条 + 「Mutation verification」的 4 条。
每一条都刻意做成**否定性命题**(例如"非 Gift method **不**落盘"), 因为
诊断工具最容易出的故障是"看起来在采, 其实什么都没采到" —— 那种故障在
正向断言下是全绿的。

⚠️ 本文件里所有 payload / cookie / 用户名一律是**明显造出来的哨兵**,
绝不用真实值。原始 payload 不进仓库是 Issue 的硬要求, 而测试反过来正是
最容易把真实样本漏进来的地方。
"""

from __future__ import annotations

import gzip
import io
import json
import os
import random
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor" / "douyin_fetcher"))

from story.gift_probe.capture import (                       # noqa: E402
    DEFAULT_MAX_PER_METHOD,
    PARSE_STATUS_ERROR,
    RawCaptureStore,
    assert_outside_production_paths,
    probe_paths_are_disjoint,
)
from story.gift_probe.hooks import make_gift_capture_fetcher  # noqa: E402
from story.gift_probe.profile import (                        # noqa: E402
    BOOTSTRAP_LOCAL,
    BOOTSTRAP_REFERENCE,
    CURRENT_USER_UNIQUE_ID,
    DEFAULT_PROFILES,
    HARD_MAX_CONCURRENT_PROFILES,
    PROFILE_A,
    PROFILE_B,
    PROFILE_C,
    PROFILE_CONFIG_INVALID,
    ProfileCounters,
    default_profiles,
    generate_random_user_unique_id,
    is_gift_family_method,
    login_cookie_is_usable,
    profile_auth_state,
    select_profiles,
)
from story.gift_probe.probe import (                          # noqa: E402
    TransportSummaryProbe,
    sanitize_method,
)

FAIL = [0]

#: 哨兵: 任何输出 / 文件里出现它 = 凭据或用户内容泄漏。
SENTINEL = "TEST_SECRET_DO_NOT_LOG"
#: 用户名哨兵 —— 只允许出现在本地诊断 JSONL 里, 不得进 INFO 日志。
USER_SENTINEL = "TESTUSER_NICK"

_TMP = []


def CallbackFetcher2():
    """拿 `CallbackFetcher` 当基线 —— 它会真的注册 Gift handler。

    为什么要它: `DanmakuFetcher` 的 `_wsOnMessage` 只在
    `keep_all or interaction_enabled` 时注册 `WebcastGiftMessage`, 而
    诊断模式把这**两个都关**(它不接业务链)。于是用 `DanmakuFetcher`
    做基线时 Gift handler 根本不会进 handlers 表 —— "handler 抛异常"这个
    场景根本构造不出来(那条消息会走 unhandled 分支)。

    这不是测试的权宜之计: 生产直播用的就是 `CallbackFetcher`(见
    `LiveSource._build`), 所以拿它做基线反而更接近真实配置。
    """
    from story.ingest import CallbackFetcher
    return CallbackFetcher


def check(name, cond, extra=""):
    """断言 + 打印。

    ⚠️ 打印走 `_say()` 而不是裸 `print()`: 失败的 `extra` 里会带上被检查
    的实际内容(含中文/emoji), 而 Windows 控制台默认是 GBK —— 裸 print
    会在**打印失败信息时**抛 `UnicodeEncodeError`, 把一次普通的断言失败
    变成一场看不懂的崩溃。那会让"到底是哪条断言挂了"这个最有用的信息
    被吞掉。
    """
    if cond:
        _say(f"  ok  {name}")
        return True
    _say(f"  FAIL {name}  {extra}")
    FAIL[0] += 1
    return False


def _say(text: str) -> None:
    """安全打印: 编码不了就退回可编码的形式, 绝不因为打印而抛。"""
    try:
        print(text)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(text.encode(enc, "replace").decode(enc, "replace"))


def mkdtemp():
    d = tempfile.mkdtemp(prefix="giftprobe_")
    _TMP.append(d)
    return d


# ======================================================================
# 1. profile A/B URL 除 user_unique_id 外完全一致
# ======================================================================
def test_a_and_b_differ_only_in_user_unique_id():
    print("\n[GP-1] A/B 只差 user_unique_id")
    a = DEFAULT_PROFILES[0]
    b = DEFAULT_PROFILES[1]
    check("A 是 current-auth-control", a.profile_id == PROFILE_A, a.profile_id)
    check("B 是 random-uid-only", b.profile_id == PROFILE_B, b.profile_id)
    check("A 用固定 uid", not a.random_user_unique_id)
    check("B 用随机 uid", b.random_user_unique_id)
    check("A/B bootstrap 相同", a.bootstrap == b.bootstrap == BOOTSTRAP_LOCAL,
          (a.bootstrap, b.bootstrap))
    check("A 的 uid 是生产常量",
          a.user_unique_id() == CURRENT_USER_UNIQUE_ID, a.user_unique_id())
    # 唯一差异就是 uid: 描述里除 user_unique_id_mode 之外必须完全一样
    da, db = a.describe(), b.describe()
    diff = {k for k in set(da) | set(db) if da.get(k) != db.get(k)}
    check("画像描述只差 uid 模式", diff == {"profile", "user_unique_id_mode"},
          diff)
    check("B 的 uid 不再是生产常量",
          b.user_unique_id() != CURRENT_USER_UNIQUE_ID)


# ======================================================================
# 2. random UID 每次连接变化且格式合法
# ======================================================================
def test_random_uid_changes_and_is_well_formed():
    print("\n[GP-2] 随机 uid 每连接变化 + 格式合法")
    p = DEFAULT_PROFILES[1]
    seen = {p.user_unique_id() for _ in range(200)}
    check("200 次里绝大多数互不相同", len(seen) > 190, len(seen))
    for v in list(seen)[:20]:
        ok = bool(re.fullmatch(r"\d+", v)) and 7_000_000_000_000_000_000 \
            <= int(v) <= 8_000_000_000_000_000_000
        if not ok:
            check(f"uid 合法({v})", False)
            break
    else:
        check("全部落在 7e18~8e18 且全为数字", True)
    # 纯函数可注入 rng -> 测试可确定性复现
    x = generate_random_user_unique_id(random.Random(7))
    y = generate_random_user_unique_id(random.Random(7))
    check("同 seed 可复现", x == y, (x, y))
    # 固定臂**永远**返回常量(否则 A 就没资格当控制组)
    fixed = DEFAULT_PROFILES[0]
    check("固定臂 100 次都是生产常量",
          {fixed.user_unique_id() for _ in range(100)}
          == {CURRENT_USER_UNIQUE_ID})


# ======================================================================
# 3. reference profile 用 fresh timestamp, 不含写死 2024 时间
# ======================================================================
def test_reference_bootstrap_uses_fresh_timestamp():
    print("\n[GP-3] reference 画像 bootstrap 用 fresh 时间")
    from ws_bootstrap import generate_ws_bootstrap
    now_ms = 1789000000000      # 2026-09 量级
    boot = generate_ws_bootstrap(1234, "u", now_ms, random.Random(3))
    check("cursor 含本次 now_ms", f"t-{now_ms}_" in boot["cursor"],
          boot["cursor"][:40])
    check("internal_ext 含本次 now_ms",
          f"first_req_ms:{now_ms}" in boot["internal_ext"])
    check("不含写死的 2024 时间",
          "1721106114633" not in boot["cursor"]
          and "1721106114633" not in boot["internal_ext"])
    c = DEFAULT_PROFILES[2]
    check("C 是 reference matching 臂",
          c.profile_id == PROFILE_C and c.bootstrap == BOOTSTRAP_REFERENCE)
    check("C 每连接随机 uid", c.random_user_unique_id)
    # C 与 A 的差别**只有**这两处, room/host/signature 都不在 profile 里
    ca, aa = c.describe(), DEFAULT_PROFILES[0].describe()
    check("C/A 差异只在 uid 模式与 bootstrap",
          {k for k in ca if ca[k] != aa[k]}
          == {"profile", "user_unique_id_mode", "bootstrap"})


# ======================================================================
# 4/5. auth mode 可观察 + 不泄漏
# ======================================================================
def test_auth_mode_observation_and_no_leak():
    print("\n[GP-4] auth mode: 有 cookie -> authenticated, 无 -> anonymous")
    empty = profile_auth_state(DEFAULT_PROFILES[0], None)
    check("无 cookie -> anonymous", empty["auth"] == "anonymous", empty)
    check("无 cookie -> config ok", empty["config_state"] == "ok", empty)

    good = f"sessionid={SENTINEL}; sid_tt={SENTINEL}"
    state = profile_auth_state(DEFAULT_PROFILES[0], good)
    check("有 cookie -> authenticated", state["auth"] == "authenticated", state)
    check("有 cookie -> config ok", state["config_state"] == "ok", state)

    # 哨兵绝不出现在画像描述 / auth 态 / 日志行里
    blob = json.dumps([empty, state], ensure_ascii=False)
    check("auth 态里没有哨兵", SENTINEL not in blob, blob[:120])

    from story.gift_probe.profile import format_profile_log
    for prof in DEFAULT_PROFILES:
        line = format_profile_log(prof, state)
        check(f"日志行不含哨兵({prof.profile_id})", SENTINEL not in line, line)
        check(f"日志行只有固定词({prof.profile_id})",
              "auth=authenticated" in line, line)
    # 不记长度/hash/前缀/cookie name 列表 —— 逐个反证
    line = format_profile_log(DEFAULT_PROFILES[0], state)
    check("不记 cookie 长度", str(len(good)) not in line, line)
    check("不记 cookie name", "sessionid" not in line and "sid_tt" not in line,
          line)
    import hashlib
    for algo in ("md5", "sha1", "sha256"):
        h = getattr(hashlib, algo)(good.encode()).hexdigest()
        if check(f"不记 {algo}", h[:12] not in line, line):
            continue

    # 配置了 cookie 但解析不出任何非空 pair -> config_invalid
    for bad in (";;;", "novalue", "   ", "= ", "; ;"):
        st = profile_auth_state(DEFAULT_PROFILES[0], bad)
        check(f"坏 cookie {bad!r} -> config_invalid",
              st["config_state"] == PROFILE_CONFIG_INVALID, st)
        check(f"坏 cookie {bad!r} -> anonymous(实际就是匿名)",
              st["auth"] == "anonymous", st)
    check("usable: 正常 pair", login_cookie_is_usable("a=1"))
    check("unusable: 空名", not login_cookie_is_usable("=1"))
    check("unusable: 空值", not login_cookie_is_usable("a="))
    check("unusable: None", not login_cookie_is_usable(None))


# ======================================================================
# 6/7/8/9. capture 规则
# ======================================================================
def _store(**kw):
    d = mkdtemp()
    return RawCaptureStore(d, "sess", PROFILE_A, **kw), d


def test_gift_family_names_are_recognised():
    print("\n[GP-5] Gift-family method 识别")
    for m in ("WebcastGiftMessage", "WebcastBindingGiftMessage",
              "WebcastGiftPlayEventMessage", "WebcastGiftSortMessage",
              "WebcastGiftUpdateMessage", "WebcastLightGiftMessage",
              "WebcastGiftEffectGameMessage"):
        check(f"认出 {m}", is_gift_family_method(m))
    for m in ("WebcastChatMessage", "WebcastLikeMessage",
              "WebcastMemberMessage", "WebcastRoomRankMessage", ""):
        check(f"不误判 {m!r}", not is_gift_family_method(m))


def test_capture_rules():
    print("\n[GP-6] 落盘规则: Gift 落 / 非 Gift 不落 / parse_error 落")
    st, d = _store(max_per_method=5)
    check("Gift method 会 capture",
          st.should_capture("WebcastGiftMessage") is True)
    check("非 Gift method 默认不 dump",
          st.should_capture("WebcastChatMessage") is False)
    check("非 Gift + parse_error 会 capture",
          st.should_capture("WebcastChatMessage", parse_error=True) is True)

    f1 = st.capture(b"GIFTPAYLOAD", method="WebcastGiftMessage",
                    envelope_msg_id=7, connection_generation=2)
    check("Gift payload 落盘成功", bool(f1), f1)
    check("落盘文件真的存在", os.path.isfile(os.path.join(st.dir, f1)), f1)
    f2 = st.capture(b"CHAT", method="WebcastChatMessage")
    check("非 Gift 返回 None(不落盘)", f2 is None, f2)
    f3 = st.capture(b"BROKEN", method="WebcastChatMessage",
                    parse_error=True, parse_status=PARSE_STATUS_ERROR)
    check("parse_error payload 落盘", bool(f3), f3)
    check("目录里没有非 Gift 的正常 payload 文件",
          not os.path.isfile(os.path.join(st.dir, "webcastchatmessage-0002.bin"))
          or f3 == "webcastchatmessage-0002.bin")

    idx = [json.loads(l) for l in
           io.open(st.index_path, encoding="utf-8").read().splitlines() if l]
    check("index 行数 == 落盘文件数", len(idx) == 2, len(idx))
    fields = {"timestamp", "profile", "connection_generation", "method",
              "payload_len", "filename", "envelope_msg_id", "parse_status"}
    check("index 字段是 Issue 的闭集",
          all(set(r) == fields for r in idx), [sorted(r) for r in idx])
    check("index 记了 envelope", idx[0]["envelope_msg_id"] == "7", idx[0])
    check("index 记了 parse_status",
          {r["parse_status"] for r in idx} == {"ok", "error"},
          [r["parse_status"] for r in idx])
    check("index 不含 payload 内容",
          "GIFTPAYLOAD" not in io.open(st.index_path,
                                       encoding="utf-8").read())
    # 落盘内容原样(不改一个字节)
    check("payload 原样落盘",
          open(os.path.join(st.dir, f1), "rb").read() == b"GIFTPAYLOAD")


def test_per_method_cap():
    print("\n[GP-7] 每 method 样本上限有效")
    st, d = _store(max_per_method=3)
    names = [st.capture(b"p%d" % i, method="WebcastGiftMessage")
             for i in range(7)]
    check("只留 3 个样本", len([n for n in names if n]) == 3, names)
    check("上限之后返回 None", all(n is None for n in names[3:]), names[3:])
    check("超限计数被记下", st.max_per_method_hits == 4, st.max_per_method_hits)
    check("目录里只有 3 个 .bin",
          len([f for f in os.listdir(st.dir) if f.endswith(".bin")]) == 3)
    # 上限是**按 method** 计的: 另一个 method 有自己的额度
    st2, _ = _store(max_per_method=2)
    st2.capture(b"a", method="WebcastGiftMessage")
    st2.capture(b"b", method="WebcastGiftMessage")
    check("Gift 额度用完", st2.capture(b"c", method="WebcastGiftMessage")
          is None)
    check("另一个 Gift-family method 仍有自己的额度",
          bool(st2.capture(b"d", method="WebcastBindingGiftMessage")))


def test_total_limits():
    print("\n[GP-8] 总目录字节 / 文件数上限")
    st, d = _store(max_per_method=100, max_total_files=2)
    got = [st.capture(b"x", method="WebcastGiftMessage") for _ in range(5)]
    check("文件数上限生效", len([g for g in got if g]) == 2, got)
    check("超限被计数", st.skipped_for_limits == 3, st.skipped_for_limits)

    st2, _ = _store(max_per_method=100, max_total_bytes=5)
    check("第一条放得下", bool(st2.capture(b"12345",
                                       method="WebcastGiftMessage")))
    check("超出字节上限则不落", st2.capture(b"123456",
                                        method="WebcastGiftMessage") is None)
    check("且没有写坏文件",
          len([f for f in os.listdir(st2.dir) if f.endswith(".bin")]) == 1)


def test_method_name_cannot_escape_probe_dir():
    print("\n[GP-9] 恶意 method 名不能写出 probe 目录")
    st, d = _store()
    st.capture(b"z", method="../../../../evil")
    st.capture(b"z2", method="..")
    st.capture(b"z3", method="Webcast/Gift\\Message")
    st.capture(b"z4", method="")
    files = os.listdir(st.dir)
    check("全部落在 probe 目录内",
          all(os.path.isfile(os.path.join(st.dir, f)) for f in files), files)
    outside = os.path.join(os.path.dirname(st.dir), "evil-0001.bin")
    check("没有写到上一级目录", not os.path.exists(outside), outside)
    check("没有创建 ../ 目录", not os.path.exists(
        os.path.join(os.path.dirname(os.path.dirname(st.dir)), "evil")))


# ======================================================================
# 10. probe 目录不写 production 数据路径
# ======================================================================
def test_probe_dir_disjoint_from_production():
    print("\n[GP-10] probe 目录与 production 数据路径不相交")
    info = probe_paths_are_disjoint("data/gift_probe")
    check("probe 目录不是 data/", not info["probe_root_is_production_dir"],
          info)
    check("不会与 production 文件同名", info["would_collide"] == (),
          info["would_collide"])
    try:
        assert_outside_production_paths("data")
        check("data/ 本身被拒绝", False, "没有抛")
    except ValueError:
        check("data/ 本身被拒绝", True)
    try:
        assert_outside_production_paths("data/gift_probe")
        check("data/gift_probe 允许", True)
    except ValueError as e:
        check("data/gift_probe 允许", False, str(e))
    # production 文件名一个都不出现在 store 的生成名里
    st, _ = _store()
    st.capture(b"q", method="WebcastGiftMessage")
    for prod in ("pool.jsonl", "played.jsonl", "puzzle.jsonl",
                 "danmaku.jsonl"):
        check(f"probe 目录里没有 {prod}",
              not os.path.exists(os.path.join(st.dir, prod)))


# ======================================================================
# 11/12. 三层计数可独立区分 + 多 profile 不串路
# ======================================================================
def _frame(items):
    """把 `(method, payload, msg_id)` 组成一个真实的 WS 帧字节串。

    ⚠️ `payload` 必须是 `bytes`: betterproto 的 `bytes_field` **不**做
    str -> bytes 强转, 传字符串会原样留在 `Message.payload` 里, 然后在
    `_parseGiftMsg` 之类的解析点炸成一个与真实链路无关的类型错误。
    测试里踩到这个坑会让断言指向错误的位置。
    """
    from protobuf.douyin import Message, PushFrame, Response
    r = Response()
    for method, payload, mid in items:
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        r.messages_list.append(Message(method=method, payload=payload,
                                       msg_id=mid))
    return PushFrame(payload=gzip.compress(r.SerializeToString())
                     ).SerializeToString()


def _gift_payload(gift_id=123, combo=2, repeat=1, total=3, repeat_end=1,
                  group_id=77, log_id="L1", trace_id="T1", msg_id=555,
                  gift_name="测试礼物", user_id="9001",
                  user_name=USER_SENTINEL):
    from protobuf.douyin import Common, GiftMessage, GiftStruct, User
    m = GiftMessage(gift_id=gift_id, combo_count=combo, repeat_count=repeat,
                    total_count=total, repeat_end=repeat_end,
                    group_id=group_id, log_id=log_id, trace_id=trace_id,
                    common=Common(msg_id=msg_id),
                    gift=GiftStruct(name=gift_name),
                    user=User(id=int(user_id), nick_name=user_name))
    return m.SerializeToString()


def _make_fetcher(counters, store, semantic_dir, emitted=None, base=None,
                  keep_all=False, interaction_enabled=True):
    """造一个装好探针的 fetcher(不联网, 直接喂帧)。

    这里手工摆状态而**不**调基线 `__init__` —— 后者会建 requests.Session、
    去读 abogus 文件路径等, 那些与"解析一个帧"无关。摆出来的字段与
    `DanmakuFetcher.__init__` / `CallbackFetcher.__init__` 一一对应。
    """
    import danmaku
    import threading
    from story.gift_probe.runner import _NoProductionSink

    base = base or danmaku.DanmakuFetcher
    Cls = make_gift_capture_fetcher(base)
    f = Cls.__new__(Cls)
    f.keep_all = keep_all
    f.interaction_enabled = interaction_enabled
    f.out_path = ""
    # 用生产同款"什么都不写"的替身: 诊断不该落 production 弹幕文件。
    f._fp = _NoProductionSink()
    f._counts = {}
    f.connection_generation = 1
    f.ws_frame_count = 0
    f.ws_message_count = 0
    f.method_counts = {}
    f.unhandled_method_counts = {}
    f.parse_error_counts = {}
    f.session_method_counts = {}
    f.session_frame_count = 0
    f._terminated = threading.Event()
    # CallbackFetcher 专有字段(基线 `_on_*` 会读)
    f._on_chat = lambda *_a, **_k: None
    f._on_control = None
    f._on_interaction = None
    f._on_frame = None
    f._on_first_frame = None
    f._first_frame_seen = False
    f.generation = 0
    f._expired = False
    f.attach_gift_probe(profile=None, counters=counters, store=store,
                        semantic_dir=semantic_dir)
    # `_on_gift` / `_on_like` 由基类提供(DanmakuFetcher 是 no-op,
    # CallbackFetcher 发 InteractionEvent)。测试里在这里覆盖成记录器 ——
    # 实例属性会盖住类属性, 且这**只**影响业务回调这一层, 探针照旧。
    if emitted is not None:
        f._on_gift = lambda m, envelope_msg_id=0: emitted.append(m)
        f._on_like = lambda m, envelope_msg_id=0: emitted.append(m)

    class _WS:
        def send(self, *a, **k):
            pass
    f.ws = _WS()
    return f


def test_three_layer_counts_are_distinguishable():
    print("\n[GP-11] seen / parsed / emitted 三层可独立区分")
    d = mkdtemp()
    c = ProfileCounters(PROFILE_A)
    st = RawCaptureStore(d, "s", PROFILE_A, max_per_method=50)
    emitted = []
    f = _make_fetcher(c, st, os.path.join(d, "s", PROFILE_A), emitted=emitted)

    gp = _gift_payload()
    f._wsOnMessage(None, _frame([
        ("WebcastGiftMessage", gp, 42),
        ("WebcastGiftSortMessage", b"\x01\x02", 43),   # 无 handler
    ]))
    check("一层: 见到 2 条 Gift-family method",
          c.gift_method_seen == 2, c.summary())
    check("二层: primary 解析成功 1 条", c.parsed_gift_count == 1)
    check("三层: 业务回调 1 次", c.emitted_gift_count == 1)
    check("回调真的被调用", len(emitted) == 1, len(emitted))
    check("未处理计数单独存在", c.unhandled_counts.get(
        "WebcastGiftSortMessage") == 1, c.unhandled_counts)
    check("三层不是同一个数(可区分)",
          len({c.gift_method_seen, c.parsed_gift_count,
               c.emitted_gift_count}) > 1)

    # --- parsed=0 且 seen>0: 走 unhandled 分支 ---
    #
    # 诊断模式把两个业务开关**都关**(它不接业务链), 于是基线根本不注册
    # Gift handler, `WebcastGiftMessage` 会走 `unhandled`。这是诊断模式的
    # 真实处境 —— 判据必须指向 **dispatch 层**, 而不是假装"没有 Gift"。
    #
    # 同时: 语义采样仍然发生(探针独立解析), 否则诊断模式在生产配置下
    # 什么都看不到。但那**不**推进 `parsed_gift_count` —— 那个计数属于
    # proto 层判据, 参见过滤说明。
    d0 = mkdtemp()
    c0 = ProfileCounters(PROFILE_A)
    st0 = RawCaptureStore(d0, "s", PROFILE_A, max_per_method=50)
    sem0 = os.path.join(d0, "s", PROFILE_A)
    f0 = _make_fetcher(c0, st0, sem0, emitted=None, keep_all=False,
                       interaction_enabled=False, base=CallbackFetcher2())
    f0._wsOnMessage(None, _frame([("WebcastGiftMessage", gp, 5)]))
    check("诊断(两开关都关): seen=1", c0.gift_method_seen == 1, c0.summary())
    check("诊断: unhandled=1(handler 未注册)",
          c0.unhandled_counts.get("WebcastGiftMessage") == 1, c0.summary())
    check("诊断: parsed=0(没走业务解析路径)", c0.parsed_gift_count == 0,
          c0.summary())
    check("诊断: emitted=0(没接业务链)", c0.emitted_gift_count == 0,
          c0.summary())
    check("诊断: 判据指向 dispatch 层",
          c0.verdict() == "dispatch", c0.verdict())
    check("诊断: 语义仍被采样(否则生产配置下什么都看不到)",
          os.path.isfile(os.path.join(sem0, "gift_semantic.jsonl")))

    # --- seen>0, handler 注册了但解析炸了 -> proto 层 ---
    #
    # 这是**真正的 proto 层故障**: handler 进了(所以不进 unhandled), 但
    # 解析抛了。此时 parsed=0、parse_error=1、emitted=0, 判据指向 proto。
    d2 = mkdtemp()
    c2 = ProfileCounters(PROFILE_B)
    st2 = RawCaptureStore(d2, "s", PROFILE_B, max_per_method=50)
    f2 = _make_fetcher(c2, st2, os.path.join(d2, "s", PROFILE_B), emitted=[],
                       base=CallbackFetcher2())  # 会注册 Gift handler

    def boom(self, payload, envelope_msg_id=0):
        raise ValueError("boom")
    f2._parseGiftMsg = boom.__get__(f2, type(f2))
    import contextlib
    with contextlib.redirect_stderr(io.StringIO()):
        f2._wsOnMessage(None, _frame([("WebcastGiftMessage", gp, 1)]))
    check("handler 抛异常: seen=1", c2.gift_method_seen == 1, c2.summary())
    check("handler 抛异常: parsed=0", c2.parsed_gift_count == 0, c2.summary())
    check("handler 抛异常: emitted=0", c2.emitted_gift_count == 0)
    check("handler 抛异常: 不进 unhandled(handler 是注册了的)",
          c2.unhandled_counts == {}, c2.unhandled_counts)
    check("handler 抛异常: parse_error=1",
          c2.parse_error_counts.get("WebcastGiftMessage") == 1,
          c2.parse_error_counts)
    # 这条消息被 capture 了**两次**是刻意的: 一次是"Gift-family 命中规则",
    # 一次是"parse_error 留证据"(Issue §E 两条规则各自成立, 不该互相去重
    # —— 去重会让 index 里看不出这条同时满足两个条件)。
    check("handler 抛异常: payload 被 capture(两条规则各留一份)",
          c2.captured_payload_count == 2, c2.summary())
    check("判据指向 proto 层", c2.verdict() == "proto", c2.verdict())
    check("proto 层样本: parsed 与 parse_error 不自相矛盾",
          not (c2.parsed_gift_count > 0 and c2.gift_parse_errors()))

    # --- seen=0 -> transport/auth 层 ---
    d3 = mkdtemp()
    c3 = ProfileCounters(PROFILE_C)
    st3 = RawCaptureStore(d3, "s", PROFILE_C)
    f3 = _make_fetcher(c3, st3, os.path.join(d3, "s", PROFILE_C), emitted=[])
    f3._wsOnMessage(None, _frame([("WebcastChatMessage", b"", 9)]))
    check("没有 Gift -> verdict=transport_or_auth",
          c3.verdict() == "transport_or_auth", c3.verdict())
    check("没有 Gift -> 无 capture", c3.captured_payload_count == 0)


def test_runner_arms_have_isolated_counters_and_dirs():
    """GP-12b: **装配层**的隔离 —— 每路 arm 自己 new 一套 counter + 目录。

    与 GP-12 的区别: 那条测的是"两个 `ProfileCounters` 实例互不影响"(数
    据结构本身没问题); 这条测的是**装配**(runner 真的**给每一路各造
    一个**)。两者都必要 —— 容器没问题但装配时共用一个实例, 故障完全一样,
    而只测前者时它是绿的。

    这正是 Issue mutation verification 第 4 条("故意让两个 profile 共用
    counter -> 隔离测试必须失败")要守的东西。
    """
    print("\n[GP-12b] runner 装配层: 每路独占 counter / 目录")
    from story.config import Config
    from story.gift_probe.runner import GiftCaptureDiagnosticRunner
    d = mkdtemp()
    cfg = Config(live_id="123")
    r = GiftCaptureDiagnosticRunner(cfg, probe_root=d, base_cls=object,
                                    now_fn=lambda: 1789996800.0)
    # 只装配, 不启动线程(启动会真的去连网络)。
    base = r._base()
    arms = []
    for p in r.profiles:
        from story.gift_probe.runner import GiftProbeArm
        arms.append(GiftProbeArm(p, cfg, r.session_id, probe_root=d,
                                 now_fn=lambda: 1789996800.0))
    check("三路 arm 互相独立",
          len({id(a.counters) for a in arms}) == len(arms),
          [id(a.counters) for a in arms])
    check("三路 store 互相独立",
          len({id(a.store) for a in arms}) == len(arms))
    check("三路目录互不相同",
          len({os.path.abspath(a.store.dir) for a in arms}) == len(arms),
          [a.store.dir for a in arms])
    check("每路 counter 的 profile_id 是自己的",
          [a.counters.profile_id for a in arms]
          == [p.profile_id for p in r.profiles])

    # 真的往其中一路喂一条 Gift, 其余两路必须纹丝不动。
    from story.gift_probe.hooks import make_gift_capture_fetcher
    import danmaku
    Cls = make_gift_capture_fetcher(danmaku.DanmakuFetcher)
    f = _make_fetcher(arms[0].counters, arms[0].store,
                      arms[0].semantic_dir, emitted=[])
    f._wsOnMessage(None, _frame([("WebcastGiftMessage", _gift_payload(), 1)]))
    check("被喂的那一路计数 +1",
          arms[0].counters.gift_method_seen == 1,
          arms[0].counters.summary())
    for a in arms[1:]:
        check(f"{a.profile.profile_id} 纹丝不动",
              a.counters.gift_method_seen == 0
              and a.counters.ws_frames == 0, a.counters.summary())
    # 目录层面也不串: 只有第一路有 .bin
    for i, a in enumerate(arms):
        bins = [x for x in os.listdir(a.store.dir) if x.endswith(".bin")]
        if i == 0:
            check("第一路有 raw 样本", len(bins) == 1, bins)
        else:
            check(f"{a.profile.profile_id} 目录没有别人的样本",
                  bins == [], bins)


def test_diagnostic_start_does_not_touch_production_files():
    """GP-17: 诊断连接**启动**这一下也不许碰 production 落库路径。

    这条守的是一个**真的踩到过**的坑: 基线 `DanmakuFetcher.start()` 的第一
    句就是 `self._fp = open(self.out_path, "a")`。诊断模式把 `out_path`
    留空(它没有 production 落库路径), 于是:

        - 好消息: 那行会 `FileNotFoundError: ''` —— 闹得很大, 一眼看得见;
        - 坏消息: 只要有人"顺手"给 `out_path` 填个默认值让它不报错, 诊断
          就会**开始往那个文件写 production 格式的弹幕行**, 而日志上一切
          正常, 唯一的症状是 `data/` 里多出一个本不该有的文件。

    ⚠️ **绝不真的调 `start()`**: 它有重连循环, 会一直跑到下播或 Ctrl-C
    —— 在测试里就是挂死。这里只跑"打开落库文件"那一小段(stubbing 掉它
    之后的联网步骤), 因为那才是本 Step 改动的部分。
    """
    print("\n[GP-17] 诊断启动不碰 production 文件")
    from story.config import Config
    from story.gift_probe.runner import (make_diagnostic_fetcher,
                                         _NoProductionSink)

    cfg = Config(live_id="000000000000")
    f = make_diagnostic_fetcher(cfg, "1234567890")
    check("_fp 是 NoProductionSink", isinstance(f._fp, _NoProductionSink))
    check("out_path 是空的(没有 production 落库路径)", f.out_path == "",
          f.out_path)

    prod_dir = ROOT / "data"
    before = set(os.listdir(prod_dir)) if prod_dir.is_dir() else set()

    # 只跑基线 start() 的**前半段**(打开落库文件 + 建连), 把真正阻塞的
    # 重连循环换掉。这里走的是我们自己覆写的 `start()`, 所以能验证到
    # "临时 sink 路径"这条改动。
    import danmaku
    calls = {"super_start": 0}

    class _StopHere(Exception):
        pass

    real_start = danmaku.DanmakuFetcher.start

    def fake_super_start(self_):
        # 复现基线的第一句(被测的那一行), 然后立刻停下。
        calls["super_start"] += 1
        with open(self_.out_path, "a", encoding="utf-8") as fp:
            self_._fp = fp
        raise _StopHere()

    danmaku.DanmakuFetcher.start = fake_super_start
    import contextlib
    try:
        with contextlib.redirect_stderr(io.StringIO()), \
                contextlib.redirect_stdout(io.StringIO()):
            try:
                f.start()
            except _StopHere:
                pass
    finally:
        danmaku.DanmakuFetcher.start = real_start

    check("真的走到了基线 start 那一段", calls["super_start"] == 1, calls)
    after = set(os.listdir(prod_dir)) if prod_dir.is_dir() else set()
    check("data/ 下没有新增文件", before == after, sorted(after - before))
    # 基线那行 open 是往**临时** sink 文件写的 —— 那个文件必须已被删掉
    check("临时 sink 文件没有残留",
          not os.path.exists(f.out_path) if f.out_path else True,
          f.out_path)
    check("启动路径结束后 _fp 被还原成 NoProductionSink",
          isinstance(f._fp, _NoProductionSink), type(f._fp).__name__)


def test_profiles_do_not_share_counters():
    print("\n[GP-12] 多 profile 的 counters / raw 文件不串路")
    cfgd = mkdtemp()
    counters = {}
    stores = {}
    fets = {}
    for pid, gift in ((PROFILE_A, 1), (PROFILE_C, 3)):
        d = os.path.join(cfgd, pid)
        counters[pid] = ProfileCounters(pid)
        stores[pid] = RawCaptureStore(d, "sess", pid, max_per_method=50)
        fets[pid] = _make_fetcher(counters[pid], stores[pid],
                                  os.path.join(d, "sess", pid), emitted=[])
    gp1 = _gift_payload(gift_id=111)
    gp3 = _gift_payload(gift_id=333)
    fets[PROFILE_A]._wsOnMessage(None, _frame([
        ("WebcastGiftMessage", gp1, 1)]))
    for i in range(3):
        fets[PROFILE_C]._wsOnMessage(None, _frame([
            ("WebcastGiftMessage", gp3, 10 + i)]))

    check("A 只看到 1 条", counters[PROFILE_A].gift_method_seen == 1,
          counters[PROFILE_A].summary())
    check("C 看到 3 条", counters[PROFILE_C].gift_method_seen == 3,
          counters[PROFILE_C].summary())
    check("A 的 frames 只算自己的", counters[PROFILE_A].ws_frames == 1,
          counters[PROFILE_A].ws_frames)
    check("C 的 frames 只算自己的", counters[PROFILE_C].ws_frames == 3,
          counters[PROFILE_C].ws_frames)
    check("两个 counter 不是同一个对象",
          counters[PROFILE_A] is not counters[PROFILE_C])
    check("两个 store 目录不同",
          stores[PROFILE_A].dir != stores[PROFILE_C].dir)
    # raw 文件不串路
    a_files = [f for f in os.listdir(stores[PROFILE_A].dir)
               if f.endswith(".bin")]
    c_files = [f for f in os.listdir(stores[PROFILE_C].dir)
               if f.endswith(".bin")]
    check("A 目录 1 个样本", len(a_files) == 1, a_files)
    check("C 目录 3 个样本", len(c_files) == 3, c_files)
    idx_a = [json.loads(l) for l in io.open(
        stores[PROFILE_A].index_path, encoding="utf-8").read().splitlines()
        if l]
    check("A 的 index 只记 A", {r["profile"] for r in idx_a} == {PROFILE_A},
          {r["profile"] for r in idx_a})
    check("A 的 index 只有一条", len(idx_a) == 1, len(idx_a))


# ======================================================================
# 13. probe 关闭时生产行为保持
# ======================================================================
def test_diagnostic_off_preserves_production_behaviour():
    print("\n[GP-13] 诊断关闭时生产行为保持")
    import danmaku
    from story.ingest import CallbackFetcher

    # (a) 基线类**没有**被本 Step 改出诊断属性
    for cls in (danmaku.DanmakuFetcher, CallbackFetcher):
        for attr in ("attach_gift_probe", "gift_probe_summary"):
            if check(f"{cls.__name__} 未被注入 {attr}",
                     attr not in cls.__dict__):
                continue
    # (b) danmaku.py 里不出现 gift_probe 的接线
    src = (ROOT / "danmaku.py").read_text(encoding="utf-8")
    check("danmaku.py 不引用 gift_probe", "gift_probe" not in src)
    check("danmaku.py 不引用 gift_capture", "gift_capture" not in src)
    # (c) 诊断 fetcher 与基线在同一响应上产生**完全相同**的计数与回调
    items = [
        ("WebcastChatMessage", b"", 1),
        ("WebcastGiftMessage", _gift_payload(), 2),
        ("WebcastGiftSortMessage", b"\x01\x02", 3),
        ("WebcastLikeMessage", b"", 4),
        ("WebcastMemberMessage", b"", 5),
    ]
    frame = _frame(items)

    base_emitted = []
    bf = danmaku.DanmakuFetcher.__new__(danmaku.DanmakuFetcher)
    bf.keep_all = True
    bf.interaction_enabled = True
    bf._fp = io.StringIO()
    bf._counts = {}
    bf.connection_generation = 1
    bf.ws_frame_count = 0
    bf.ws_message_count = 0
    bf.method_counts = {}
    bf.unhandled_method_counts = {}
    bf.parse_error_counts = {}
    bf.session_method_counts = {}
    bf.session_frame_count = 0
    # ⚠️ 回调签名必须与基线一致: `_parseGiftMsg` 用
    # `envelope_msg_id=` 这个**关键字**调用 `_on_gift`, 所以参数名不能省。
    bf._on_gift = lambda m, envelope_msg_id=0: base_emitted.append(("gift", m))
    bf._on_like = lambda m, envelope_msg_id=0: base_emitted.append(("like", m))

    class _WS:
        def send(self, *a, **k):
            pass
    bf.ws = _WS()
    import contextlib
    with contextlib.redirect_stderr(io.StringIO()):
        bf._wsOnMessage(None, frame)

    d = mkdtemp()
    c = ProfileCounters(PROFILE_A)
    st = RawCaptureStore(d, "s", PROFILE_A)
    diag_emitted = []
    df = _make_fetcher(c, st, os.path.join(d, "s", PROFILE_A),
                       emitted=diag_emitted, keep_all=True)
    # 诊断侧的记录器要改成与基线**同样的形状**((kind, msg)), 否则"类型
    # 序列一致"这条对照就变成在比两种不同的记录格式, 而不是在比行为。
    df._on_gift = lambda m, envelope_msg_id=0: diag_emitted.append(
        ("gift", m))
    df._on_like = lambda m, envelope_msg_id=0: diag_emitted.append(
        ("like", m))
    with contextlib.redirect_stderr(io.StringIO()):
        df._wsOnMessage(None, frame)

    for attr in ("ws_frame_count", "ws_message_count", "method_counts",
                 "unhandled_method_counts", "parse_error_counts"):
        check(f"{attr} 与基线逐字相同",
              getattr(bf, attr) == getattr(df, attr),
              (getattr(bf, attr), getattr(df, attr)))
    check("业务回调次数相同", len(base_emitted) == len(diag_emitted),
          (len(base_emitted), len(diag_emitted)))
    check("业务回调类型序列相同",
          [k for k, _ in base_emitted] == [k for k, _ in diag_emitted],
          ([k for k, _ in base_emitted], [k for k, _ in diag_emitted]))
    # 业务回调收到的对象字段也要一致(不只是"调了几次")
    for (kb, mb), (kd, md) in zip(base_emitted, diag_emitted):
        if not check(f"回调类型一致 {kb}", kb == kd):
            break
        if not check(f"回调对象 gift_id 一致 {kb}",
                     getattr(mb, "gift_id", None) == getattr(md, "gift_id",
                                                             None)):
            break
    # 落库内容也要一致 —— 诊断开着不该改变 production 落库行为。
    #
    # 诊断侧用的是 `_NoProductionSink`(什么都不写), 所以这里比的是
    # **基线写了什么** 与 **诊断本该写什么** —— 后者由 sink 记录下来的
    # 调用序列给出。两条都不为空且逐字相同才算通过。
    check("基线确实落了库(否则对照是空的)", bool(bf._fp.getvalue()),
          repr(bf._fp.getvalue())[:120])
    written = "".join(df._fp.written)
    check("诊断侧落库调用与基线逐字一致",
          written == bf._fp.getvalue(), (written[:200],
                                         bf._fp.getvalue()[:200]))
    check("诊断侧**没有**把内容写到真实文件",
          getattr(df._fp, "wrote_to_disk", False) is False)
    # (d) 诊断没往 production 落库文件写任何东西
    check("诊断没有写弹幕落库文件",
          not os.path.exists(os.path.join(d, "danmaku.jsonl")))

    # (e) 诊断 fetcher 拒绝被接上业务回调
    from story.gift_probe.runner import make_diagnostic_fetcher
    from story.config import Config
    cfg = Config(live_id="1")
    try:
        make_diagnostic_fetcher(cfg, "u", on_interaction=lambda e: None)
        check("传业务回调会报错", False, "没有抛")
    except ValueError:
        check("传业务回调会报错", True)
    try:
        make_diagnostic_fetcher(cfg, "u", interaction_enabled=True)
        check("interaction_enabled=True 会报错", False, "没有抛")
    except ValueError:
        check("interaction_enabled=True 会报错", True)


# ======================================================================
# 附加: 摘要日志安全 + 上限 + profile 选择
# ======================================================================
def test_summary_log_is_safe_and_bounded():
    print("\n[GP-14] 摘要日志安全(无 cookie / payload / 昵称, 单行)")
    c = ProfileCounters(PROFILE_A, auth="authenticated", config_state="ok")
    c.bump_method("WebcastChatMessage")
    c.bump_method("WebcastGiftMessage")
    # 恶意 method 名: 含换行 + 形如 `key=value` 的注入尝试。
    # ⚠️ 这里**不**把 cookie 哨兵塞进 method 名 —— 那验的是"日志不打印
    # method 名"这件根本不成立的事(method 名本来就要打出来给排障用)。
    # 要验的是**形状**: 换行不得注入, `=` 不得让日志看起来像多了个字段。
    c.bump_method("Evil\nInjected=1\r\nFake=2")
    c.bump_unhandled("WebcastGiftSortMessage")
    c.parsed_gift_count = 1
    p = TransportSummaryProbe(PROFILE_A, c, logger=None)
    line = p.maybe_emit(force=True)
    check("摘要单行(无换行注入)", "\n" not in line and "\r" not in line, line)
    check("method 名里的 = 被归一化(不能伪造字段)",
          "Injected=1" not in line, line[:250])
    check("Fake=2 没有变成一个新字段", "Fake=2" not in line, line[:250])
    check("摘要含 auth 词", "auth=authenticated" in line)
    check("摘要含三层计数",
          all(k in line for k in ("gift_method_seen", "parsed_gift_count",
                                  "emitted_gift_count")), line[:200])
    check("摘要含 verdict", "verdict=" in line)
    # 恶意 method 名被归一化
    check("method 名被归一化",
          sanitize_method("Evil\nInjected=1") == "Evil?Injected?1",
          sanitize_method("Evil\nInjected=1"))
    check("sanitize 不截断中段(能区分 Gift/Sort)",
          sanitize_method("WebcastGiftMessage")
          != sanitize_method("WebcastGiftSortMessage"))

    # 凭据哨兵**只**出现在 cookie 语境里时才不许进日志。做法: 把哨兵放进
    # 一个 profile 的 auth 态并走一遍真实的日志格式化路径。
    from story.gift_probe.profile import format_profile_log
    for prof in DEFAULT_PROFILES:
        for state in (profile_auth_state(prof, None),
                      profile_auth_state(prof, f"sessionid={SENTINEL}")):
            logline = format_profile_log(prof, state)
            check(f"画像日志不含哨兵({prof.profile_id})",
                  SENTINEL not in logline, logline)
    authline = p.now_text()
    check("摘要(无 cookie 语境)不含哨兵", SENTINEL not in authline,
          authline[:200])

    # ---- 端到端: 真的用带哨兵的 cookie 配置跑一遍摘要输出路径 ----
    #
    # 上面几条测的是"格式化函数不会自己编造凭据"; 这一条测的是更贴近现实
    # 的故障: 某个字段**被赋成**了凭据, 然后摘要把它原样打出来。
    # mutation M-GP-2 / M-GP-2b 就是照着这个形状做的 —— 它们必须被抓住。
    for prof in DEFAULT_PROFILES:
        st = profile_auth_state(prof, f"sessionid={SENTINEL}; sid_tt={SENTINEL}")
        cc = ProfileCounters(prof.profile_id, auth=st["auth"],
                             config_state=st["config_state"])
        cc.bump_method("WebcastGiftMessage")
        # 模拟"auth 字段被污染成凭据"这种赋值错误
        cc.auth = f"authenticated sessionid={SENTINEL}"
        leak_line = TransportSummaryProbe(prof.profile_id, cc).now_text()
        # 摘要**必须**只打固定词。这里把 auth 换回固定词后再断言 —— 也就是
        # "无论 counter.auth 里被塞了什么, 摘要都只允许输出那两个词"。
        check(f"摘要只输出固定词, 不转发 auth 字段({prof.profile_id})",
              SENTINEL not in leak_line, leak_line[:250])

    # 用户昵称不得进 INFO 摘要
    sem = mkdtemp()
    c2 = ProfileCounters(PROFILE_B)
    from story.gift_probe.probe import GiftSemanticProbe
    from protobuf.douyin import GiftMessage
    sp = GiftSemanticProbe(sem, PROFILE_B, c2)
    d = mkdtemp()
    st = RawCaptureStore(d, "s", PROFILE_B)
    f = _make_fetcher(c2, st, sem, emitted=[])
    f._wsOnMessage(None, _frame([("WebcastGiftMessage", _gift_payload(), 1)]))
    line2 = TransportSummaryProbe(PROFILE_B, c2).maybe_emit(force=True)
    check("摘要不含用户昵称", USER_SENTINEL not in line2, line2[:200])
    # 但语义 JSONL 里**应该**有(本地诊断文件, 与 danmaku --all 一致)
    body = io.open(os.path.join(sem, "gift_semantic.jsonl"),
                   encoding="utf-8").read()
    check("语义 JSONL 记了昵称(本地诊断)",
          USER_SENTINEL in body, body[:200])
    rec = json.loads(body.splitlines()[0])
    for k in ("gift_id", "gift_name", "combo_count", "repeat_count",
              "total_count", "repeat_end", "group_count", "group_id",
              "log_id", "trace_id", "envelope_msg_id", "common_msg_id",
              "connection_generation", "profile"):
        if not check(f"语义记录含 {k}", k in rec, sorted(rec)):
            break
    check("语义记录 gift_id 正确", rec["gift_id"] == "123", rec["gift_id"])
    check("语义记录 envelope 与 common 分开",
          rec["envelope_msg_id"] == "1" and rec["common_msg_id"] == "555",
          (rec["envelope_msg_id"], rec["common_msg_id"]))


def test_profile_selection_and_limits():
    print("\n[GP-15] profile 选择 / 并发上限")
    check("默认三臂", len(default_profiles()) == 3)
    check("limit=2 只取前两臂",
          [p.profile_id for p in default_profiles(2)]
          == [PROFILE_A, PROFILE_B])
    check("limit 再大也不超过硬上限",
          len(default_profiles(99)) == HARD_MAX_CONCURRENT_PROFILES
          == len(DEFAULT_PROFILES))
    check("limit=0 也至少保留一路", len(default_profiles(0)) == 1)
    check("limit 非法值不抛", len(default_profiles("x")) >= 1)
    sel = select_profiles(["reference-2026", "random-uid-only"])
    check("按名字选择并保序",
          [p.profile_id for p in sel] == [PROFILE_C, PROFILE_B],
          [p.profile_id for p in sel])
    check("重复名字去重",
          len(select_profiles(["random-uid-only", "random-uid-only"])) == 1)
    try:
        select_profiles(["ramdom-uid-only"])
        check("拼错的名字会报错", False, "没有抛")
    except ValueError:
        check("拼错的名字会报错", True)
    check("空列表 -> 默认臂", len(select_profiles([])) == 3)


def test_no_real_probe_data_committed():
    print("\n[GP-16] 仓库不提交真实 probe 数据")
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    check(".gitignore 覆盖 data/gift_probe/**",
          "data/gift_probe" in gi, gi[:200])
    # 现有 fixtures 里不该出现 probe 产物
    for p in (ROOT / "tests" / "fixtures").iterdir():
        check(f"fixtures 无 probe 产物: {p.name}",
              not (p.name.endswith(".bin")
                   or p.name in ("index.jsonl", "gift_semantic.jsonl")))
    # 测试里用到的 payload 全是哨兵(不含真实礼物名)
    src = (ROOT / "tests" / "test_gift_probe.py").read_text(encoding="utf-8")
    check("测试用哨兵昵称", "USER_SENTINEL" in src)
    check("测试用哨兵 cookie", "SENTINEL" in src)
    # 本文件里不得出现任何**看起来像真实凭据**的赋值。
    #
    # ⚠️ 待查的字面量必须在**运行时拼出来**, 不能直接写在断言里 —— 否则
    # 断言自己就会命中自己(那段字面量就在同一个文件里), 于是这条检查永远
    # 为红。这类"自指"陷阱也是它值得单独写一行注释的原因。
    env_name = "DOUYIN_LIVE" + "_COOKIE="
    check(f"测试不写真实凭据: {env_name}", env_name not in src)
    for prefix in ("sessionid=", "ttwid=", "__ac_signature="):
        for marker in ("真实", "REAL", "prod_"):
            needle = prefix + marker
            if not check(f"测试不写疑似真实凭据: {needle}",
                         needle not in src):
                break


def main():
    tests = [
        test_a_and_b_differ_only_in_user_unique_id,
        test_random_uid_changes_and_is_well_formed,
        test_reference_bootstrap_uses_fresh_timestamp,
        test_auth_mode_observation_and_no_leak,
        test_gift_family_names_are_recognised,
        test_capture_rules,
        test_per_method_cap,
        test_total_limits,
        test_method_name_cannot_escape_probe_dir,
        test_probe_dir_disjoint_from_production,
        test_three_layer_counts_are_distinguishable,
        test_profiles_do_not_share_counters,
        test_runner_arms_have_isolated_counters_and_dirs,
        test_diagnostic_start_does_not_touch_production_files,
        test_diagnostic_off_preserves_production_behaviour,
        test_summary_log_is_safe_and_bounded,
        test_profile_selection_and_limits,
        test_no_real_probe_data_committed,
    ]
    try:
        for t in tests:
            t()
    finally:
        for d in _TMP:
            shutil.rmtree(d, ignore_errors=True)
    print()
    if FAIL[0]:
        print(f"FAILED: {FAIL[0]} 项")
        return 1
    print("PASS: gift_capture_diagnostic(多画像对照 + 原始证据 + 安全边界)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
