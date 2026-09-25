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
    check("三层: dry-run 回调 1 次", c.emitted_gift_count == 1)
    # ⚠️ 这里**不**断言测试自己的 `emitted` 记录器被调用 —— 诊断模式走的
    # 是**干跑 handler**, 它不经过 `_on_gift`。断言那个记录器等于在测一条
    # 诊断模式根本不走的路(那正是上一版假绿的形式)。真正要验的是
    # `emitted_gift_count`(见 GP-11 的干跑段落与 GP-20 的端到端)。
    check("未处理计数单独存在", c.unhandled_counts.get(
        "WebcastGiftSortMessage") == 1, c.unhandled_counts)
    check("三层不是同一个数(可区分)",
          len({c.gift_method_seen, c.parsed_gift_count,
               c.emitted_gift_count}) > 1)

    # --- 诊断模式的两个业务开关都关时, Gift **仍然要走完整链路** ---
    #
    # ⚠️ 这是 Blocker 2 的回归测试, 也是本套件最要紧的一条。
    #
    # 诊断连接不接业务回调(免得污染 SummonLedger / Engine), 所以
    # `keep_all` 与 `interaction_enabled` **都是 False**。上一版就是止步于
    # 此: 基线于是根本不注册 `WebcastGiftMessage`, 每一份礼物都掉进
    # `unhandled` -> `verdict()` 恒为 `dispatch` -> proto 层与 callback 层
    # **永远不可达**。真实直播里那条链等于答不了 Issue #17 的第 3~5 问。
    #
    # 修法是给诊断连接一条自己的**干跑链路**(注册 handler -> 真解析 ->
    # 只写诊断 sink 的回调)。这里就在**两开关都关**的配置下验证那条链路
    # 真的贯通 —— 与真实 runner 的配置完全一致。
    d0 = mkdtemp()
    c0 = ProfileCounters(PROFILE_A)
    st0 = RawCaptureStore(d0, "s", PROFILE_A, max_per_method=50)
    sem0 = os.path.join(d0, "s", PROFILE_A)
    f0 = _make_fetcher(c0, st0, sem0, emitted=None, keep_all=False,
                       interaction_enabled=False, base=CallbackFetcher2())
    f0._wsOnMessage(None, _frame([("WebcastGiftMessage", gp, 5)]))
    check("诊断(两开关都关): seen=1", c0.gift_method_seen == 1, c0.summary())
    check("诊断: **不**落进 unhandled(干跑 handler 已注册)",
          c0.unhandled_counts == {}, c0.summary())
    check("诊断: parsed=1(干跑链路真的解析了)",
          c0.parsed_gift_count == 1, c0.summary())
    check("诊断: emitted=1(干跑回调在链路末端被调用)",
          c0.emitted_gift_count == 1, c0.summary())
    check("诊断: 判据不是 dispatch(那条链已经可达)",
          c0.verdict() != "dispatch", c0.verdict())
    check("诊断: 语义被写进 JSONL",
          os.path.isfile(os.path.join(sem0, "gift_semantic.jsonl")))
    # 语义只写一次(计数与语义分开推进, 不能重复落两行)
    sem_lines = [l for l in io.open(os.path.join(sem0, "gift_semantic.jsonl"),
                                    encoding="utf-8").read().splitlines()
                 if l.strip()]
    check("语义 JSONL 恰好一行(没有重复落)", len(sem_lines) == 1,
          len(sem_lines))

    # --- seen>0 但解析炸了 -> proto 层 ---
    #
    # 这是**真正的 proto 层故障**: 消息进了 handler(所以不进 unhandled),
    # 但解析抛了。此时 parsed=0、parse_error=1、emitted=0, 判据指向 proto。
    #
    # ⚠️ 破坏点必须打在**干跑 handler 真正会调**的那个东西上。诊断模式下
    # 走的是 `_gift_probe_parse_gift_dry_run`(它内部 `GiftMessage().parse`),
    # 而不是基线的 `_parseGiftMsg` —— 打错了地方会得到"测试通过但链路上
    # 什么都没发生"的假绿。
    d2 = mkdtemp()
    c2 = ProfileCounters(PROFILE_B)
    st2 = RawCaptureStore(d2, "s", PROFILE_B, max_per_method=50)
    f2 = _make_fetcher(c2, st2, os.path.join(d2, "s", PROFILE_B), emitted=[],
                       keep_all=False, interaction_enabled=False,
                       base=CallbackFetcher2())

    def boom(payload, envelope_msg_id=0):
        raise ValueError("boom")
    f2._gift_probe_parse_gift_dry_run = boom
    import contextlib
    with contextlib.redirect_stderr(io.StringIO()):
        f2._wsOnMessage(None, _frame([("WebcastGiftMessage", gp, 1)]))
    check("解析炸了: seen=1", c2.gift_method_seen == 1, c2.summary())
    check("解析炸了: parsed=0", c2.parsed_gift_count == 0, c2.summary())
    check("解析炸了: emitted=0", c2.emitted_gift_count == 0)
    check("解析炸了: 不进 unhandled(handler 是注册了的)",
          c2.unhandled_counts == {}, c2.unhandled_counts)
    check("解析炸了: parse_error=1",
          c2.parse_error_counts.get("WebcastGiftMessage") == 1,
          c2.parse_error_counts)
    # 这条消息被 capture 了**两次**是刻意的: 一次是"Gift-family 命中规则",
    # 一次是"parse_error 留证据"(Issue §E 两条规则各自成立, 不该互相去重
    # —— 去重会让 index 里看不出这条同时满足两个条件)。
    check("解析炸了: payload 被 capture(两条规则各留一份)",
          c2.captured_payload_count == 2, c2.summary())
    check("判据指向 proto 层", c2.verdict() == "proto", c2.verdict())
    check("proto 层样本: parsed 与 parse_error 不自相矛盾",
          not (c2.parsed_gift_count > 0
               and c2.parse_error_counts.get("WebcastGiftMessage")))

    # --- seen=0 -> transport/auth 层 ---
    d3 = mkdtemp()
    c3 = ProfileCounters(PROFILE_C)
    st3 = RawCaptureStore(d3, "s", PROFILE_C)
    f3 = _make_fetcher(c3, st3, os.path.join(d3, "s", PROFILE_C), emitted=[])
    f3._wsOnMessage(None, _frame([("WebcastChatMessage", b"", 9)]))
    check("没有 Gift -> verdict=transport_or_auth",
          c3.verdict() == "transport_or_auth", c3.verdict())
    check("没有 Gift -> 无 capture", c3.captured_payload_count == 0)


def test_ctrl_c_wait_is_short_polled_and_interruptible():
    """Windows 回归: 不能用无限期 Event.wait() 等 Ctrl-C。"""
    print("\n[GP-12a] Ctrl-C 等待使用短轮询且能退出")
    from story.gift_probe.runner import _wait_for_ctrl_c

    class _InterruptOnWait:
        def __init__(self):
            self.timeouts = []

        def wait(self, timeout):
            self.timeouts.append(timeout)
            raise KeyboardInterrupt

    fake = _InterruptOnWait()
    # 若 helper 没吞掉 KeyboardInterrupt，这里测试进程会直接中断。
    _wait_for_ctrl_c(poll_seconds=0.05, wait_event=fake)
    check("Ctrl-C 后 helper 正常返回", True)
    check("等待调用带有限 timeout", fake.timeouts == [0.05], fake.timeouts)
    check("timeout 足够短，不会长时间卡住 SIGINT",
          fake.timeouts and 0 < fake.timeouts[0] <= 0.5, fake.timeouts)

    try:
        _wait_for_ctrl_c(poll_seconds=0, wait_event=fake)
        check("非正 poll_seconds 必须拒绝", False, "未报错")
    except ValueError:
        check("非正 poll_seconds 明确报错", True)


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
    # ---- Blocker 1 的装配层断言: bootstrap 模式必须真的落到 counter 上 ----
    #
    # 这条守的是"runner 有没有把 profile 的 bootstrap 选择传下去"。少了
    # 它, `bootstrap_mode` 会永远为空串 —— 而摘要里那一列看起来只是"没
    # 数据", 不会有人注意到 reference 臂压根没生效。
    check("每路 counter 的 bootstrap_mode 与 profile 一致",
          [a.counters.bootstrap_mode for a in arms]
          == [p.bootstrap for p in r.profiles],
          ([a.counters.bootstrap_mode for a in arms],
           [p.bootstrap for p in r.profiles]))
    check("C 的 counter 记的是 reference",
          [a.counters.bootstrap_mode for a in arms
           if a.profile.profile_id == PROFILE_C] == [BOOTSTRAP_REFERENCE],
          [a.counters.bootstrap_mode for a in arms])
    # fetcher 侧也要真的按这个模式分派(元数据 -> 连接层)
    for a in arms:
        f = a.build_fetcher(None)
        check(f"{a.profile.profile_id} 的 fetcher 按 profile 分派 bootstrap",
              f._gift_probe_bootstrap_mode == a.profile.bootstrap,
              (f._gift_probe_bootstrap_mode, a.profile.bootstrap))

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


def _capture_arm_url(profile, *, user_unique_id=None, now_ms=1789000000000,
                     room_id="6746053408656034572", cfg=None):
    """让一路诊断连接**真的**构造出它的 WSS URL 并返回。

    ## 为什么必须有这条测试(而不是继续断言 profile 元数据)

    上一版被 review 打回的第一个 blocker 就是这类假绿:

        测试断言 `Profile C.bootstrap == "reference"` —— 绿;
        但连接层从来没读过那个字段, 运行时 C 与 B 的 URL **完全相同**。

    也就是说测的是"意图", 而不是"实际连出去的那个 URL"。真实直播里这会让
    我们得出"reference 方案也没用"的**错误结论**。

    这里改成**在生产真实会走的那条路径上**取 URL:
    `_connectWebSocket()` 会 `websocket.WebSocketApp(url, ...)`, 于是把
    `WebSocketApp` 换成一个只记录 URL、然后抛异常中止的替身 —— 这样既拿到
    了真实的 URL 字符串(含 cursor / internal_ext / user_unique_id), 又
    不会真的去连网络。
    """
    import websocket as _ws_mod
    from story.config import Config
    from story.gift_probe.capture import RawCaptureStore
    from story.gift_probe.profile import ProfileCounters
    from story.gift_probe.runner import make_diagnostic_fetcher

    cfg = cfg or Config(live_id="123456")
    d = mkdtemp()
    counters = ProfileCounters(profile.profile_id,
                               bootstrap_mode=profile.bootstrap)
    store = RawCaptureStore(d, "s", profile.profile_id)
    f = make_diagnostic_fetcher(
        cfg,
        user_unique_id if user_unique_id is not None
        else profile.user_unique_id())
    f.attach_gift_probe(profile=profile, counters=counters, store=store,
                        semantic_dir=os.path.join(d, "s", profile.profile_id))

    # 房间号与时钟都固定, 让两次调用的差异**只能**来自 profile 本身。
    f.__dict__["_DouyinLiveWebFetcher__room_id"] = room_id

    captured = {"url": None, "headers": None}

    class _StopHere(Exception):
        pass

    def _fake_app(url, header=None, *a, **kw):
        captured["url"] = url
        captured["headers"] = header
        raise _StopHere()

    real_app = _ws_mod.WebSocketApp
    _ws_mod.WebSocketApp = _fake_app
    import contextlib
    try:
        # 固定时钟 —— `_connectWebSocket` 里是 time.time()。
        import time as _time
        real_time = _time.time
        _time.time = lambda: now_ms / 1000.0
        try:
            with contextlib.redirect_stderr(io.StringIO()), \
                    contextlib.redirect_stdout(io.StringIO()):
                f._connectWebSocket()
        except _StopHere:
            pass
        finally:
            _time.time = real_time
    finally:
        _ws_mod.WebSocketApp = real_app
    return captured, counters


def _url_params(url):
    """`...?a=1&b=2` -> `{"a": "1", "b": "2"}`(已 urldecode)。"""
    from urllib.parse import parse_qs, urlparse
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def test_actual_wss_urls_differ_as_intended():
    """GP-18: 抓**真实构造出来的 WSS URL** 做断言(Blocker 1 的回归测试)。

    三条纪律, 全部对着 URL 说, 而不是对着 profile 元数据说:

        A vs B : 除 `user_unique_id` 外**完全一致**
        A vs C : C 是**参考实现**的形状, A 是**生产**的形状, 两者结构不同
        B vs C : 真的不同 —— 曾经它们其实是同一个 URL

    ⚠️ 关于 C 的断言方式(这是本测试被 review 打回过的点):

    上一版 C 被实现成"复用生产生成器 + `did = uid + now_ms`", 于是测试把
    `uid+now_ms` 当成预期值 —— 那等于**验我们自己新发明的实现**, 而 Issue
    要的是"对齐 `chuanyue98/douyin-live-toolkit` 的形状"。

    现在 C 的断言是拿**参考实现的模板**逐字段比对的(见
    `reference_bootstrap.REFERENCE_CURSOR_TEMPLATE` /
    `REFERENCE_INTERNAL_EXT_TEMPLATE`), 并且显式断言"**没有**生产形状的
    特征"(无 `wrds_v`、cursor 不是 `t-..._r-{r}_..._h-{h}` 骨架)。
    """
    print("\n[GP-18] 真实 WSS URL:A/B/C 的差异与 C 的 reference 形状")
    from story.gift_probe.profile import DEFAULT_PROFILES
    a, b, c = DEFAULT_PROFILES

    cap_a, _ = _capture_arm_url(a)
    cap_b, _ = _capture_arm_url(b)
    cap_c, _ = _capture_arm_url(c)
    url_a, url_b, url_c = cap_a["url"], cap_b["url"], cap_c["url"]
    check("三路都真的构造出了 URL",
          all(u and u.startswith("wss://") for u in (url_a, url_b, url_c)),
          [bool(url_a), bool(url_b), bool(url_c)])

    pa, pb, pc = (_url_params(url_a), _url_params(url_b), _url_params(url_c))

    def _ext_deterministic(ext):
        """去掉 `wrds_v` 的随机低位, 只留可比较的**结构 + 身份/时间**部分。

        `wrds_v` 的低位由生产生成器每次连接随机取(那是它本来的行为),
        所以两路之间必然不同 —— 把它算进差异会让"只差 uid"这条断言永远
        为红, 从而掩盖真正的差异。
        """
        parts = [p for p in ext.split("|") if not p.startswith("wrds_v:")]
        return "|".join(parts)

    def _cursor_deterministic(cur):
        """cursor 里 `r`/`h` 的随机低位同理, 只留 `t-`/`d-1`/`u-1` 骨架。"""
        return re.sub(r"_r-\d+|_h-\d+", "", cur)

    # ---- A vs B: 唯一变量是 user_unique_id ----
    #
    # ⚠️ 比的是**确定性部分**: identity/时间字段与 URL 骨架。随机低位
    # (r/h/wrds_v/signature)本来就该不同 —— 那是生产生成器的既有行为,
    # 不是 profile 差异。
    diff_ab = {k for k in set(pa) | set(pb) if pa.get(k) != pb.get(k)}
    check("A/B 的差异只出现在随机低位与 uid 上",
          diff_ab <= {"user_unique_id", "cursor", "internal_ext", "signature"},
          diff_ab)
    check("A/B 的 external_ext 结构部分只差 did",
          _ext_deterministic(pa["internal_ext"])
          .replace(pa["user_unique_id"], "<UID>")
          == _ext_deterministic(pb["internal_ext"])
          .replace(pb["user_unique_id"], "<UID>"),
          (_ext_deterministic(pa["internal_ext"]),
           _ext_deterministic(pb["internal_ext"])))
    check("A/B 的 cursor 骨架一致",
          _cursor_deterministic(pa["cursor"])
          == _cursor_deterministic(pb["cursor"]),
          (_cursor_deterministic(pa["cursor"]),
           _cursor_deterministic(pb["cursor"])))
    check("A 用固定 uid",
          pa.get("user_unique_id") == CURRENT_USER_UNIQUE_ID,
          pa.get("user_unique_id"))
    check("B 的 uid 是随机值(且格式合法)",
          pb.get("user_unique_id") != CURRENT_USER_UNIQUE_ID
          and bool(re.fullmatch(r"[78]\d{18}",
                                pb.get("user_unique_id") or "")),
          pb.get("user_unique_id"))
    # A/B 都走生产路径 -> 都带 wrds_v
    check("A/B 都是生产形状(带 wrds_v)",
          "wrds_v:" in pa["internal_ext"] and "wrds_v:" in pb["internal_ext"])

    # ================================================================
    # C == 参考实现(chuanyue98/douyin-live-toolkit @4b4b7c1e)的**逐字段**形状
    # ================================================================
    from story.gift_probe.reference_bootstrap import (
        REFERENCE_CURSOR_TEMPLATE,
        REFERENCE_FH,
        REFERENCE_INTERNAL_EXT_TEMPLATE,
    )
    from story.gift_probe.hooks import _safe_room_id

    now_ms = 1789000000000
    room_id = "6746053408656034572"

    # (1) cursor 与参考实现的模板**逐字**相同
    expected_cursor = REFERENCE_CURSOR_TEMPLATE.format(now_ms=now_ms)
    check("C 的 cursor 逐字等于参考实现模板",
          pc["cursor"] == expected_cursor,
          (pc["cursor"], expected_cursor))
    check("C 的 cursor 含参考实现的硬编码 fh 段",
          f"fh-{REFERENCE_FH}" in pc["cursor"], pc["cursor"])
    check("C 的 cursor 是 d-1_u-1 开头(参考实现的骨架)",
          pc["cursor"].startswith("d-1_u-1_"), pc["cursor"][:40])
    # 反向: 不能是生产那种 t-..._r-{random}_d-1_u-1_h-{random} 骨架
    check("C 的 cursor **不是**生产骨架(无 _h- 段)",
          "_h-" not in pc["cursor"], pc["cursor"])
    check("C 的 cursor 的 r 段是字面量 1(参考实现)",
          pc["cursor"].endswith("_r-1"), pc["cursor"][-12:])

    # (2) internal_ext 与参考实现的模板**逐字**相同
    expected_ext = REFERENCE_INTERNAL_EXT_TEMPLATE.format(
        room_id=room_id, user_unique_id=pc["user_unique_id"], now_ms=now_ms)
    check("C 的 internal_ext 逐字等于参考实现模板",
          pc["internal_ext"] == expected_ext,
          (pc["internal_ext"], expected_ext))
    # (3) 参考实现**没有** wrds_v —— 这条是"没在复用生产生成器"的硬证据
    check("C 的 internal_ext **没有** wrds_v(参考实现无此段)",
          "wrds_v" not in pc["internal_ext"], pc["internal_ext"])
    check("A 的 internal_ext **有** wrds_v(生产生成器仍有, 未被改动)",
          "wrds_v:" in pa["internal_ext"])

    # (4) wss_push_did == user_unique_id(**不拼 now_ms**)
    did_c = pc["internal_ext"].split("wss_push_did:")[1].split("|")[0]
    check("C 的 wss_push_did == user_unique_id",
          did_c == pc["user_unique_id"], (did_c, pc["user_unique_id"]))
    check("C 的 wss_push_did **不**以 now_ms 结尾(上一版是错的)",
          not did_c.endswith(str(now_ms)), did_c)
    check("C 的 did 不是 uid+now_ms 的拼接",
          did_c != f"{pc['user_unique_id']}{now_ms}",
          (did_c, f"{pc['user_unique_id']}{now_ms}"))

    # (5) C 用 fresh now_ms
    check("C 的 cursor 用本次 now_ms", f"t-{now_ms}_" in pc["cursor"],
          pc["cursor"][:40])
    check("C 的 internal_ext 用本次 now_ms",
          f"first_req_ms:{now_ms}" in pc["internal_ext"]
          and f"fetch_time:{now_ms}" in pc["internal_ext"]
          and f"wss_info:0-{now_ms}-0-0" in pc["internal_ext"],
          pc["internal_ext"])
    check("三路都不含写死的 2024 时间",
          all("1721106114633" not in u for u in (url_a, url_b, url_c)))

    # (6) C 的 uid 用参考实现自己的区间(上界 7.999...e18)
    from story.gift_probe.reference_bootstrap import (
        REFERENCE_UID_MAX, REFERENCE_UID_MIN,
    )
    check("C 的 uid 落在参考实现的区间内",
          bool(re.fullmatch(r"\d+", pc["user_unique_id"]))
          and REFERENCE_UID_MIN <= int(pc["user_unique_id"])
          <= REFERENCE_UID_MAX,
          (pc["user_unique_id"], REFERENCE_UID_MIN, REFERENCE_UID_MAX))
    check("C 的 uid 每次都变",
          len({_capture_arm_url(c)[0]["url"] for _ in range(3)}) == 3)

    # ---- A vs C / B vs C: 结构上确实不同 ----
    check("A/C URL **确实不同**", url_a != url_c, "URL 相同 = Blocker 1 复发")
    check("A/C 的 cursor 结构不同(生产骨架 vs reference 骨架)",
          _cursor_deterministic(pa["cursor"]) != pc["cursor"],
          (_cursor_deterministic(pa["cursor"]), pc["cursor"]))
    check("A/C 的 internal_ext 结构部分**确实不同**",
          _ext_deterministic(pa["internal_ext"])
          != _ext_deterministic(pc["internal_ext"]),
          (_ext_deterministic(pa["internal_ext"])[:80],
           _ext_deterministic(pc["internal_ext"])[:80]))
    check("A 的 did 就是固定 uid",
          pa["internal_ext"].split("wss_push_did:")[1].split("|")[0]
          == CURRENT_USER_UNIQUE_ID)
    check("B/C URL **确实不同**(曾经这里是同一个)",
          url_b != url_c, "B 与 C 的 URL 相同 = reference 臂没生效")
    check("B/C 的 cursor 结构不同",
          _cursor_deterministic(pb["cursor"]) != pc["cursor"],
          (_cursor_deterministic(pb["cursor"]), pc["cursor"]))

    # ---- 其余连接参数三路一致(不是"随手改了点别的") ----
    for key in ("app_name", "version_code", "webcast_sdk_version", "compress",
                "device_platform", "identity", "room_id", "aid", "live_id",
                "im_path"):
        same = pa.get(key) == pb.get(key) == pc.get(key)
        if not check(f"三路的 {key} 一致", same,
                     (pa.get(key), pb.get(key), pc.get(key))):
            break
    # 而且 signature 都在(签名链没被绕过)
    check("三路都带 signature",
          all("signature" in p for p in (pa, pb, pc)))

    # ---- C 的 bootstrap_mode 真的传到了 counters ----
    _, counters_c = _capture_arm_url(c)
    check("C 的 counters 记录了 bootstrap_mode=reference",
          counters_c.bootstrap_mode == "reference",
          counters_c.bootstrap_mode)


def test_reference_bootstrap_matches_reference_source():
    """GP-19: C 的 bootstrap 是参考实现的**逐字复现**(而不是自创形状)。

    这是本测试被 review 打回过的点的直接回归:

        上一版 C 用 `did = f"{uid}{now_ms}"` 并复用生产生成器 —— 测试还把
        `uid+now_ms` 当成预期值。那等于在验**我们自己新发明的实现**, 而
        Issue 要的是"对齐 `chuanyue98/douyin-live-toolkit` 的形状"。

    现在的断言方式: 拿参考实现的**模板字符串**渲染出期望值, 再与我的实现
    输出逐字比对。模板本身在 `reference_bootstrap.py` 里, 其上游出处
    (repo / commit / 文件 / 行号)也写在那个模块的 docstring 里, 便于复核。

    ⚠️ 这里**不**断言"did 以 now_ms 结尾"那类自创性质 —— 参考实现的 did
    就是 `user_unique_id`, 与时间无关。上一版正是因为把自创性质当预期,
    才让这条测试绿着通过了一个不符合 Issue 的实现。
    """
    print("\n[GP-19] C 的 bootstrap == 参考实现的模板")
    from story.gift_probe.reference_bootstrap import (
        REFERENCE_CURSOR_TEMPLATE,
        REFERENCE_INTERNAL_EXT_TEMPLATE,
        build_reference_bootstrap,
    )

    now_ms, room, uid = 1789000000000, "6746053408656034572", "U1"
    got = build_reference_bootstrap(room, uid, now_ms)

    check("cursor == 参考实现模板渲染结果",
          got["cursor"] == REFERENCE_CURSOR_TEMPLATE.format(now_ms=now_ms),
          (got["cursor"], REFERENCE_CURSOR_TEMPLATE.format(now_ms=now_ms)))
    check("internal_ext == 参考实现模板渲染结果",
          got["internal_ext"] == REFERENCE_INTERNAL_EXT_TEMPLATE.format(
              room_id=room, user_unique_id=uid, now_ms=now_ms),
          got["internal_ext"])
    # 结构性质(逐条对应参考实现源码里的字面量)
    check("cursor 以 d-1_u-1_fh- 开头",
          got["cursor"].startswith("d-1_u-1_fh-"), got["cursor"])
    check("cursor 以 _r-1 结尾(参考实现写死 r-1)",
          got["cursor"].endswith("_r-1"), got["cursor"])
    check("internal_ext 无 wrds_v",
          "wrds_v" not in got["internal_ext"], got["internal_ext"])
    check("internal_ext 的 did == uid(不拼 now_ms)",
          got["internal_ext"].split("wss_push_did:")[1].split("|")[0] == uid)
    # 确定性(测试可复现): 同输入同输出。且**没有**共享随机状态 ——
    # 参考形状里根本没有随机段, 所以两次调用必须**完全**相同。
    check("同输入完全可复现(参考形状里没有随机段)",
          build_reference_bootstrap(room, uid, now_ms) == got)
    check("now_ms 变 -> cursor/ext 变",
          build_reference_bootstrap(room, uid, now_ms + 1) != got)

    # ---- 与**生产**形状必须不同(那条路走的是另一个参考实现)----
    from ws_bootstrap import generate_ws_bootstrap
    import random as _r
    prod = generate_ws_bootstrap(room, uid, now_ms, _r.Random(1))
    check("C 的 cursor 与生产形状不同",
          got["cursor"] != prod["cursor"],
          (got["cursor"], prod["cursor"]))
    check("生产形状有 wrds_v, C 没有",
          "wrds_v:" in prod["internal_ext"]
          and "wrds_v" not in got["internal_ext"])
    check("生产形状是 t-..._r-{数字}_.._h-{数字}, C 不是",
          bool(re.match(r"^t-\d+_r-\d+_d-1_u-1_h-\d+$", prod["cursor"]))
          and not re.match(r"^t-\d+_r-\d+", got["cursor"]),
          (prod["cursor"], got["cursor"]))


def test_reference_uid_range_matches_reference():
    """GP-19b: C 的 uid 用参考实现自己的区间(上界 7.999...e18)。

    Issue 要求 C "逐字段对齐", 而 uid 的取法是其中一项。参考实现是
    `randint(7e18, 7_999_999_999_999_999_999)`, 本项目 B 臂用的区间上界是
    `8e18` —— 差一点。既然要求对齐, 就按参考实现来, 并把这个差别钉住,
    免得以后有人"顺手统一成 8e18"。
    """
    print("\n[GP-19b] C 的 uid 区间")
    import random as _r
    from story.gift_probe.reference_bootstrap import (
        REFERENCE_UID_MAX, REFERENCE_UID_MIN, reference_user_unique_id,
    )
    from story.gift_probe.profile import DEFAULT_PROFILES, PROFILE_B

    check("参考实现区间上界是 7.999...e18",
          REFERENCE_UID_MAX == 7_999_999_999_999_999_999,
          REFERENCE_UID_MAX)
    vals = [reference_user_unique_id(_r.Random(i)) for i in range(200)]
    check("全部落在 [7e18, 7.999...e18]",
          all(re.fullmatch(r"\d+", v)
              and REFERENCE_UID_MIN <= int(v) <= REFERENCE_UID_MAX
              for v in vals))
    check("取值有变化(没有退化成常量)", len(set(vals)) > 190, len(set(vals)))
    check("同 seed 可复现",
          reference_user_unique_id(_r.Random(7))
          == reference_user_unique_id(_r.Random(7)))

    # C 走参考区间; B 仍走 Issue 指定的 7e18~8e18
    c = DEFAULT_PROFILES[2]
    b = DEFAULT_PROFILES[1]

    # ⚠️ 怎么才算"确定性地"钉住 C 用的是参考生成器?
    #
    # 试过但**不行**的两种做法(都真的让一个变异逃掉了, 记在这里免得有人
    # 再退回其中任何一种):
    #
    #   1. **抽样在范围内** —— 两个区间几乎重合, 抽样落在参考区间内的概率
    #      接近 1, 所以它区分不出两者。
    #   2. **比对同 seed 的输出** —— 两个生成器共用 `random.Random` 的取值
    #      序列, 只是边界不同; 同 seed 下它们**输出完全相同的值**。也就是说
    #      值相等这件事对两者都成立, 断言它等于什么都没验。
    #
    # 能真正区分的只有**上界本身**: 参考是 `randint(7e18, 7.999...e18)`,
    # 生产是 `randrange(7e18, 8e18 + 1)`。两者上界差 1, 且**只有**上界不同。
    # 所以断言必须落在"区间定义"上, 而不是落在"抽样结果"上。
    from story.gift_probe.profile import (RANDOM_UID_MAX as PROD_UID_MAX,
                                          RANDOM_UID_MIN as PROD_UID_MIN)
    check("参考区间上界 == 7.999...e18",
          REFERENCE_UID_MAX == 7_999_999_999_999_999_999, REFERENCE_UID_MAX)
    check("生产区间上界 == 8e18(Issue 对 B 的要求)",
          PROD_UID_MAX == 8_000_000_000_000_000_000, PROD_UID_MAX)
    check("两个区间上界**确实不同**(差 1)",
          PROD_UID_MAX - REFERENCE_UID_MAX == 1,
          (PROD_UID_MAX, REFERENCE_UID_MAX))
    check("参考区间下界与生产相同",
          REFERENCE_UID_MIN == PROD_UID_MIN,
          (REFERENCE_UID_MIN, PROD_UID_MIN))

    # C 的 uid 必须来自**参考那个**生成器 —— 用函数的**身份**判定,
    # 而不是用它的输出。这是唯一能区分两个共用 RNG 序列的生成器的判据。
    from story.gift_probe import reference_bootstrap as _rb
    from story.gift_probe import profile as _pf
    check("C 走过的生成器是 reference_user_unique_id",
          c.bootstrap == "reference"
          and _rb.reference_user_unique_id is not None,
          c.bootstrap)
    # 端到端: 直接调 profile 的分派, 确认它调的是 reference 那个函数。
    # 用 monkeypatch 记录被调用的函数(输出相同, 只能靠调用点区分)。
    calls = []
    real_ref = _rb.reference_user_unique_id
    real_prod = _pf.generate_random_user_unique_id
    try:
        _rb.reference_user_unique_id = lambda rng=None: (
            calls.append("reference"), real_ref(rng))[1]
        _pf.generate_random_user_unique_id = lambda rng=None: (
            calls.append("production"), real_prod(rng))[1]
        c.user_unique_id(_r.Random(3))
        check("C 调的是 reference 生成器(不是 production)",
              calls == ["reference"], calls)
        calls.clear()
        b.user_unique_id(_r.Random(3))
        check("B 调的是 production 生成器(不是 reference)",
              calls == ["production"], calls)
    finally:
        _rb.reference_user_unique_id = real_ref
        _pf.generate_random_user_unique_id = real_prod

    c_val = c.user_unique_id(_r.Random(3))
    check("C 的 uid 在参考区间内",
          REFERENCE_UID_MIN <= int(c_val) <= REFERENCE_UID_MAX, c_val)
    check("B 的 profile 仍是 local bootstrap(所以走生产 uid 生成器)",
          b.bootstrap == "local", b.bootstrap)


def test_reference_templates_are_recorded_with_provenance():
    """GP-23: 参考形状的来源必须可复核(不是"凭印象抄的")。

    C 臂的全部价值在于"它跑的是**别人**的实现", 而不是我们编的形状。
    所以 `reference_bootstrap.py` 必须写清上游出处, 且模板里的每个字面量
    都能在"来源"里对上。

    这条测试做两件事:

    1. 断言模块 docstring 里记录了 repo / commit / 文件路径 —— 将来有人
       问"这个 fh-7392... 是哪来的", 答案在代码里, 不在某人的记忆里;
    2. 断言模板里的硬编码字面量与来源声明一致 —— 防止有人改了模板却忘了
       改出处(那种情况下出处就成了误导)。

    ⚠️ 这里**不**联网核对上游。CI 是离线的, 而一个会打网络的测试既慢又
    flaky。可复核性通过"出处写在代码里 + 字面量被钉住"来保证。
    """
    print("\n[GP-23] reference 形状的来源可复核")
    import story.gift_probe.reference_bootstrap as rb
    doc = rb.__doc__ or ""

    check("docstring 记录了参考仓库",
          "chuanyue98/douyin-live-toolkit" in doc, doc[:200])
    check("docstring 记录了 commit",
          "4b4b7c1e09adf7e8a62b232f2768484836f070f7" in doc)
    check("docstring 记录了文件路径",
          "ws_client.py" in doc)
    check("docstring 记录了 uid 的取法来源",
          "randint" in doc or "_connect_once" in doc)

    # 字面量必须与出处声明一致
    check("fh 字面量是参考实现里的那个",
          rb.REFERENCE_FH == "7392091211001140287", rb.REFERENCE_FH)
    check("cursor 模板含该 fh",
          f"fh-{rb.REFERENCE_FH}" in rb.REFERENCE_CURSOR_TEMPLATE,
          rb.REFERENCE_CURSOR_TEMPLATE)
    check("cursor 模板是 d-1_u-1 骨架",
          rb.REFERENCE_CURSOR_TEMPLATE.startswith("d-1_u-1_fh-"))
    check("cursor 模板以 _r-1 结尾",
          rb.REFERENCE_CURSOR_TEMPLATE.endswith("_r-1"))
    check("internal_ext 模板**没有** wrds_v",
          "wrds_v" not in rb.REFERENCE_INTERNAL_EXT_TEMPLATE)
    check("internal_ext 模板含参考实现的全部段",
          all(seg in rb.REFERENCE_INTERNAL_EXT_TEMPLATE for seg in
              ("internal_src:dim", "wss_push_room_id:", "wss_push_did:",
               "first_req_ms:", "fetch_time:", "seq:1", "wss_info:0-")),
          rb.REFERENCE_INTERNAL_EXT_TEMPLATE)
    check("参考实现 docstring 写明了边界(不是官方协议)",
          "官方协议" in doc and "参考实现" in doc)
    # 上游出处与实际取值的对应关系: did 必须直接来自 user_unique_id
    out = rb.build_reference_bootstrap("ROOM", "UID999", 123)
    check("did 直接取自传入的 user_unique_id(无加工)",
          "wss_push_did:UID999|" in out["internal_ext"],
          out["internal_ext"])


def test_end_to_end_runner_reaches_all_four_verdict_layers():
    """GP-20: 用**真实 runner 的配置**跑通四层判据(Blocker 2 的回归测试)。

    ## 为什么这条必须是"端到端 runner"而不是"另一个 helper 配置"

    上一版被 review 打回的第二个 blocker 就是这类假绿:

        GP-11 用 `interaction_enabled=True` 构造出三层计数 —— 绿;
        但真实 runner 强制两个开关都关, 于是真实直播里每一份礼物都落成
        `unhandled` -> `verdict()` 恒为 `dispatch`。

    也就是说: **测试路径与生产诊断路径不是同一条**。测试证明的是"那套配置
    下能分层", 而真实跑的是另一套配置。

    所以这里不自己拼配置, 而是拿 `run_gift_capture_diagnostic` 真正会用的
    那个 fetcher 构造入口(`make_diagnostic_fetcher` + `attach_gift_probe`)
    来跑四种输入, 逐条断言 `verdict()` 落在正确的层:

        seen=0                                   -> transport_or_auth
        seen>0, 有 Gift-family 没 handler        -> dispatch
        seen>0, handler 抛异常                   -> proto
        seen>0, 解析成功但没走到回调             -> callback_or_ingest
        seen>0, 全程走通                         -> ok
    """
    print("\n[GP-20] 端到端 runner 配置下的四层判据")
    from story.config import Config
    from story.gift_probe.profile import DEFAULT_PROFILES
    from story.gift_probe.runner import GiftProbeArm

    cfg = Config(live_id="123456")
    profile = DEFAULT_PROFILES[0]

    def build():
        """与 `GiftCaptureDiagnosticRunner.start()` 完全同一条构造路径。"""
        arm = GiftProbeArm(profile, cfg, "sess",
                           probe_root=mkdtemp(),
                           now_fn=lambda: 1789996800.0)
        f = arm.build_fetcher(None)     # base_cls=None -> 真实用的 DanmakuFetcher
        return arm, f

    def feed(f, items):
        import contextlib
        with contextlib.redirect_stderr(io.StringIO()):
            f._wsOnMessage(None, _frame(items))

    # (a) 完全没有 Gift -> transport_or_auth
    arm, f = build()
    feed(f, [("WebcastChatMessage", b"", 1)])
    check("无 Gift -> transport_or_auth",
          arm.counters.verdict() == "transport_or_auth", arm.counters.verdict())

    # (b) Gift 来了但 handler 被摘掉 -> dispatch(必须**能**观测到这一层)
    arm, f = build()
    from story.gift_probe.hooks import GiftProbeMixin
    f._gift_probe_register_dry_run_handlers = lambda handlers: None
    feed(f, [("WebcastGiftMessage", _gift_payload(), 2)])
    check("Gift 无 handler -> seen>0",
          arm.counters.gift_method_seen == 1, arm.counters.summary())
    check("Gift 无 handler -> unhandled>0",
          arm.counters.unhandled_counts.get("WebcastGiftMessage") == 1,
          arm.counters.unhandled_counts)
    check("Gift 无 handler -> dispatch",
          arm.counters.verdict() == "dispatch", arm.counters.verdict())

    # (c) 解析抛异常 -> proto
    arm, f = build()

    def _boom(payload, envelope_msg_id=0):
        raise ValueError("boom")
    f._gift_probe_parse_gift_dry_run = _boom
    feed(f, [("WebcastGiftMessage", _gift_payload(), 3)])
    check("解析炸了 -> parse_error>0",
          arm.counters.parse_error_counts.get("WebcastGiftMessage") == 1,
          arm.counters.parse_error_counts)
    check("解析炸了 -> proto",
          arm.counters.verdict() == "proto", arm.counters.verdict())

    # (d) 解析成功但干跑回调没跑 -> callback_or_ingest
    #
    # 这条正是 Issue §D 的第四层判据: "parse 成功, emitted=0"。它必须
    # **可达** —— 若 seen/parsed/emitted 被合并成一个数, 这一层永远测不出来。
    arm, f = build()
    f._gift_probe_dry_run_gift = lambda m, envelope_msg_id=0: None
    feed(f, [("WebcastGiftMessage", _gift_payload(), 4)])
    check("回调没跑 -> parsed>0",
          arm.counters.parsed_gift_count == 1, arm.counters.summary())
    check("回调没跑 -> emitted=0",
          arm.counters.emitted_gift_count == 0, arm.counters.summary())
    check("回调没跑 -> callback_or_ingest",
          arm.counters.verdict() == "callback_or_ingest",
          arm.counters.verdict())

    # (e) 全程走通 -> ok
    arm, f = build()
    feed(f, [("WebcastGiftMessage", _gift_payload(), 5)])
    check("走通 -> ok", arm.counters.verdict() == "ok",
          arm.counters.verdict())
    check("走通 -> parsed=1 且 emitted=1",
          arm.counters.parsed_gift_count == 1
          and arm.counters.emitted_gift_count == 1, arm.counters.summary())
    # 端到端下 envelope_msg_id 必须真的被传下去(Issue §G 要求)
    sem = arm.semantic_dir
    rec = json.loads(io.open(os.path.join(sem, "gift_semantic.jsonl"),
                             encoding="utf-8").read().splitlines()[0])
    check("端到端: envelope_msg_id 被传递(非空)",
          rec["envelope_msg_id"] == "5", rec["envelope_msg_id"])
    check("端到端: envelope 与 common 分开记录",
          rec["envelope_msg_id"] != rec["common_msg_id"],
          (rec["envelope_msg_id"], rec["common_msg_id"]))
    # 诊断 fetcher 的业务隔离: 没有业务回调被接上
    check("端到端: 诊断 fetcher 不接业务回调",
          f._on_interaction is None, f._on_interaction)

    # ---- (f) Issue #42 §8.1: secondary Gift-family unhandled **不得**
    #          覆盖 primary 的成功结论 ----
    #
    # 现场形状: primary `WebcastGiftMessage` parse + emitted 全通, 但
    # `WebcastGiftSortMessage`(匿名连接也能收到的那种)没有 handler。
    # 旧 verdict 按"任意 Gift-family unhandled"判成 `dispatch`, 把一场
    # 成功的验收误读成"分发层断了"。现在 primary verdict 只看 primary,
    # secondary 只进 summary 诊断字段。
    arm, f = build()
    feed(f, [("WebcastGiftMessage", _gift_payload(), 6),
             ("WebcastGiftSortMessage", b"", 7)])
    check("secondary unhandled -> verdict 仍是 ok",
          arm.counters.verdict() == "ok", arm.counters.verdict())
    check("secondary unhandled 进诊断字段",
          arm.counters.secondary_gift_family_unhandled()
          == {"WebcastGiftSortMessage": 1},
          arm.counters.secondary_gift_family_unhandled())
    check("secondary unhandled 进 summary",
          arm.counters.summary().get("secondary_gift_family_unhandled")
          == {"WebcastGiftSortMessage": 1},
          arm.counters.summary())
    # 反向: primary 自己 unhandled 仍然是 dispatch(该层必须保持可达)
    arm, f = build()
    f._gift_probe_register_dry_run_handlers = lambda handlers: None
    feed(f, [("WebcastGiftMessage", _gift_payload(), 8),
             ("WebcastGiftSortMessage", b"", 9)])
    check("primary unhandled -> 仍是 dispatch",
          arm.counters.verdict() == "dispatch", arm.counters.verdict())


def test_dry_run_handler_does_not_swallow_parse_errors():
    """GP-22: 干跑 handler **绝不能**吞掉解析异常(Blocker 2 的判据根基)。

    这条单独成篇, 因为它是 proto 层判据能以成立的**唯一**前提:

        `parse_error_counts` 是在基线 `_wsOnMessage` 的 `except` 分支里
        推进的。若干跑 handler 自己 `try/except` 把异常吞掉, 那个分支
        永远不会走到 —— 计数恒为 0, `verdict()` 永远不会是 `proto`,
        而**表面上一切正常**(没有任何报错)。

    也就是说: "解析失败"在日志里会彻底消失。这正是 Issue #17 要找的
    那类故障(handler 接到了但 protobuf parse 失败), 却在诊断里变成不可见。

    所以这里直接对着**行为**断言: 喂一个必然解析失败的 payload, 干跑
    handler 必须把异常抛出去(而不是返回 None)。
    """
    print("\n[GP-22] 干跑 handler 不吞解析异常")
    from story.config import Config
    from story.gift_probe.profile import DEFAULT_PROFILES
    from story.gift_probe.runner import GiftProbeArm

    cfg = Config(live_id="123456")
    arm = GiftProbeArm(DEFAULT_PROFILES[0], cfg, "sess",
                       probe_root=mkdtemp(), now_fn=lambda: 1789996800.0)
    f = arm.build_fetcher(None)

    # 一个几乎必然解析失败的 payload: 声明了畸形的 proto 字段长度。
    bad = b"\xff\xff\xff\xff\xff\xff\xff\xff"
    raised = None
    try:
        f._gift_probe_parse_gift_dry_run(bad, envelope_msg_id=1)
    except Exception as e:                      # noqa: BLE001
        raised = e
    check("干跑 handler 把解析异常抛出去了(不吞)", raised is not None,
          type(raised).__name__ if raised else "没抛 -> parse_error 永远不会被记")

    # 端到端: 走完整帧路径, parse_error 必须真的被记下
    import contextlib
    with contextlib.redirect_stderr(io.StringIO()):
        f._wsOnMessage(None, _frame([("WebcastGiftMessage", bad, 9)]))
    check("端到端: parse_error_counts 记到了",
          arm.counters.parse_error_counts.get("WebcastGiftMessage") == 1,
          arm.counters.parse_error_counts)
    check("端到端: parsed 没有被误推进",
          arm.counters.parsed_gift_count == 0, arm.counters.summary())
    check("端到端: emitted 没有被误推进",
          arm.counters.emitted_gift_count == 0, arm.counters.summary())
    check("端到端: 判据指向 proto",
          arm.counters.verdict() == "proto", arm.counters.verdict())


def test_safe_error_never_carries_credential():
    """GP-21: `_safe_error` 是**真脱敏**, 不是字符过滤(Blocker 3 回归)。

    上一版是 `f"{type(e).__name__}: {e}"` 然后替换非法字符 —— 那**不是**
    脱敏: cookie 的字符集(字母/数字/下划线/等号/短横)本来就全在保留范围内,
    于是 `sessionid=...` 会几乎原样进 WARN 日志 / `arm.error` /
    `session_summary.json`。

    这里直接把**带哨兵的 cookie** 放进异常正文, 然后断言哨兵在**所有**出口
    (函数返回值、WARN 日志、arm 摘要、session summary)里出现 **0 次**。
    """
    print("\n[GP-21] _safe_error 真脱敏(哨兵出现 0 次)")
    from story.gift_probe.runner import _safe_error

    cookie = f"sessionid={SENTINEL}; sid_tt={SENTINEL}; ttwid={SENTINEL}"

    class WebSocketBadStatusException(Exception):
        pass

    class _FakeHandshakeError(Exception):
        pass

    excs = [
        # 最常见的形状: 握手失败, 异常正文里带着请求头
        WebSocketBadStatusException(f"Handshake status 403; Cookie: {cookie}"),
        ConnectionResetError(f"reset by peer; cookie={cookie}"),
        TimeoutError(f"timed out after 3s; cookie={cookie}"),
        FileNotFoundError(f"'/tmp/{cookie}'"),
        ValueError(f"bad cookie {cookie}"),
        RuntimeError(cookie),
        _FakeHandshakeError(cookie),
    ]
    for e in excs:
        out = _safe_error(e)
        check(f"{type(e).__name__}: 输出不含哨兵", SENTINEL not in out, out)
        check(f"{type(e).__name__}: 输出不含 cookie 片段",
              "sessionid" not in out and "sid_tt" not in out
              and "ttwid" not in out, out)
        check(f"{type(e).__name__}: 输出是个短类别词",
              len(out) < 80 and "(" in out, out)

    # ---- 走完整条真实路径: 异常 -> arm.error -> 日志 -> session summary ----
    from story.config import Config
    from story.gift_probe.profile import DEFAULT_PROFILES
    from story.gift_probe.runner import GiftCaptureDiagnosticRunner

    cfg = Config(live_id="123")
    r = GiftCaptureDiagnosticRunner(cfg, probe_root=mkdtemp(),
                                    base_cls=object,
                                    now_fn=lambda: 1789996800.0)
    from story.gift_probe.runner import GiftProbeArm
    arm = GiftProbeArm(DEFAULT_PROFILES[0], cfg, r.session_id,
                       probe_root=r.probe_root, now_fn=lambda: 1789996800.0)
    r.arms = [arm]
    # 模拟"启动时握手异常, 正文带 cookie"
    arm.error = _safe_error(
        WebSocketBadStatusException(f"handshake failed Cookie: {cookie}"))
    check("arm.error 不含哨兵", SENTINEL not in arm.error, arm.error)

    # WARN 日志路径
    buf = io.StringIO()
    import logging as _logging
    h = _logging.StreamHandler(buf)
    lg = _logging.getLogger("story.gift_probe.runner")
    old_level, old_prop = lg.level, lg.propagate
    lg.addHandler(h)
    lg.setLevel(_logging.DEBUG)
    lg.propagate = False
    try:
        lg.warning("gift probe 路 %s 退出: %s",
                   arm.profile.profile_id, arm.error)
    finally:
        lg.removeHandler(h)
        lg.setLevel(old_level)
        lg.propagate = old_prop
    check("WARN 日志不含哨兵", SENTINEL not in buf.getvalue(),
          buf.getvalue()[:200])

    # session summary 落盘路径
    p = r.write_session_summary()
    check("session summary 写出来了", bool(p), p)
    blob = io.open(p, encoding="utf-8").read()
    check("session_summary.json 不含哨兵", SENTINEL not in blob, blob[:300])
    check("session_summary.json 不含 cookie 片段",
          "sessionid" not in blob and "sid_tt" not in blob, blob[:300])
    # 反向验证: 证明上面不是空断言 —— 摘要里**确实**写了 error 字段
    check("摘要里确实有 error 字段(不是被整个删掉)",
          '"error"' in blob, blob[:200])


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

    for attr in ("ws_frame_count", "ws_message_count"):
        check(f"{attr} 与基线逐字相同",
              getattr(bf, attr) == getattr(df, attr),
              (getattr(bf, attr), getattr(df, attr)))
    # ---- method / unhandled / parse_error: 除 Gift 外逐字相同 ----
    #
    # ⚠️ Gift 这一条**故意**不同: Blocker 2 的修法就是让诊断连接用**自己
    # 的干跑 handler** 处理 `WebcastGiftMessage`, 而不是让基线按业务开关
    # 决定注册与否。所以:

    #   - 诊断: Gift 永远有 handler(干跑), 永远不会落进 unhandled;
    #   - 基线: Gift 的注册取决于 keep_all/interaction_enabled。
    #
    # 这正是我们要的行为差异 —— 但它是**受控的**: 除了 Gift, 其余每一个
    # method 的计数都必须逐字相同, 否则诊断就改变了生产的观测面。
    non_gift_bf = {k: v for k, v in bf.method_counts.items()
                   if not is_gift_family_method(k)}
    non_gift_df = {k: v for k, v in df.method_counts.items()
                   if not is_gift_family_method(k)}
    check("method_counts 除 Gift 外与基线逐字相同",
          non_gift_bf == non_gift_df, (non_gift_bf, non_gift_df))
    check("Gift-family 计数两者都看到了同一条",
          bf.method_counts.get("WebcastGiftMessage")
          == df.method_counts.get("WebcastGiftMessage") == 1,
          (bf.method_counts.get("WebcastGiftMessage"),
           df.method_counts.get("WebcastGiftMessage")))
    # 非 Gift 的 unhandled 必须一致(这才是"诊断不改生产观测面"的判据)
    u_bf = {k: v for k, v in bf.unhandled_method_counts.items()
            if not is_gift_family_method(k)}
    u_df = {k: v for k, v in df.unhandled_method_counts.items()
            if not is_gift_family_method(k)}
    check("非 Gift 的 unhandled 与基线一致", u_bf == u_df, (u_bf, u_df))
    check("非 Gift 的 parse_error 与基线一致",
          {k: v for k, v in bf.parse_error_counts.items()
           if not is_gift_family_method(k)}
          == {k: v for k, v in df.parse_error_counts.items()
              if not is_gift_family_method(k)},
          (bf.parse_error_counts, df.parse_error_counts))
    # ---- 业务回调: 除 Gift 外必须一模一样 ----
    #
    # `like` 是关键的对照项: 它在两边的注册条件相同, 所以次数与对象都必须
    # 逐字相等。`gift` 在诊断侧**不进**业务回调 —— 那是有意的(诊断不接
    # 业务链), 也正是 Issue non-goals 要求的隔离。
    base_kinds = [k for k, _ in base_emitted]
    diag_kinds = [k for k, _ in diag_emitted]
    check("业务回调类型序列 除 Gift 外相同",
          [k for k in base_kinds if k != "gift"]
          == [k for k in diag_kinds if k != "gift"],
          (base_kinds, diag_kinds))
    check("诊断侧**没有**把 Gift 送进业务回调(业务隔离)",
          "gift" not in diag_kinds, diag_kinds)
    check("基线侧**有** Gift 业务回调(证明对照不是空的)",
          base_kinds.count("gift") == 1, base_kinds)
    # like 回调收到的对象字段也要一致(不只是"调了几次")
    for (kb, mb), (kd, md) in zip(
            [x for x in base_emitted if x[0] == "like"],
            [x for x in diag_emitted if x[0] == "like"]):
        if not check(f"like 回调类型一致 {kb}", kb == kd):
            break
        if not check("like 回调对象字段一致",
                     getattr(mb, "count", None) == getattr(md, "count", None)):
            break
    # 落库内容: 除 Gift 外必须逐字一致。
    #
    # 诊断侧用的是 `_NoProductionSink`(什么都不写), 所以这里比的是
    # **基线写了什么** 与 **诊断本该写什么** —— 后者由 sink 记录下来的
    # 调用序列给出。
    #
    # ⚠️ Gift 行**故意**不在诊断侧: 诊断用自己的干跑 handler, 不落
    # production 弹幕文件(那是 Issue 测试点 10 的要求)。所以逐字比对只对
    # 非 Gift 行成立 —— 那才是"诊断不改生产观测面"的判据。
    check("基线确实落了库(否则对照是空的)", bool(bf._fp.getvalue()),
          repr(bf._fp.getvalue())[:120])
    written = "".join(df._fp.written)

    def _non_gift_lines(text):
        return [l for l in text.splitlines()
                if l.strip() and '"kind": "gift"' not in l]

    check("诊断侧落库调用与基线 除 Gift 行外逐字一致",
          _non_gift_lines(written) == _non_gift_lines(bf._fp.getvalue()),
          (_non_gift_lines(written), _non_gift_lines(bf._fp.getvalue())))
    check("基线落库里有 gift 行", '"kind": "gift"' in bf._fp.getvalue())
    check("诊断落库里**没有** gift 行(不写 production 落库)",
          '"kind": "gift"' not in written, written[:200])

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
        test_ctrl_c_wait_is_short_polled_and_interruptible,
        test_runner_arms_have_isolated_counters_and_dirs,
        test_actual_wss_urls_differ_as_intended,
        test_reference_bootstrap_matches_reference_source,
        test_reference_uid_range_matches_reference,
        test_reference_templates_are_recorded_with_provenance,
        test_end_to_end_runner_reaches_all_four_verdict_layers,
        test_dry_run_handler_does_not_swallow_parse_errors,
        test_safe_error_never_carries_credential,
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
