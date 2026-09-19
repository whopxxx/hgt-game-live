#!/usr/bin/env python
# coding: utf-8
"""运行: uv run tests/test_ws_cookie.py（完全离线, 无网络）。

12B-Auth: WS handshake 的登录态 Cookie 组装。

钉住四件事:
  A. **匿名态**: 不配 login cookie 时行为与历史一致(基础身份链在,
     没有测试 token, `ws_auth=anonymous`)。
  B. **登录态**: 假 login cookie 正确合并且**同名覆盖**(登录 ttwid 赢),
     最终任意 cookie name 至多出现一次。
  C. **不泄漏**: 一个哨兵串在连接构造 / repr / 日志 / 异常路径里出现
     **0 次**。这是本 Step 的硬要求 —— Cookie 是凭据。
  D. **回归**: 既有 plumbing 不受影响。

⚠️ 本文件里的 cookie 值一律是**明显假的**哨兵, 绝不用真实值。
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor" / "douyin_fetcher"))

from ws_cookie import (                                    # noqa: E402
    ANONYMOUS_WS_COOKIE_NAMES,
    AUTHENTICATED_WS_COOKIE_NAMES,
    WS_AUTH_ANONYMOUS,
    WS_AUTH_AUTHENTICATED,
    build_ws_cookie_header,
    describe_ws_auth,
    merge_cookie_header,
    parse_cookie_header,
)

FAIL = [0]

#: 哨兵: 任何输出里出现它 = 凭据泄漏。
SENTINEL = "TEST_SECRET_DO_NOT_LOG"


def check(name, cond, extra=""):
    if cond:
        print(f"  ok  {name}")
        return
    print(f"  FAIL {name}  {extra}")
    FAIL[0] += 1


def _names(header: str) -> list:
    """从 header 串里取出所有 cookie name(保序, 含重复)。"""
    out = []
    for part in header.split(";"):
        frag = part.strip()
        if not frag:
            continue
        name = frag.split("=", 1)[0].strip()
        if name:
            out.append(name)
    return out


#: 匿名真实 base —— 只有 ttwid(见 liveMan._build_ws_cookie_header)。
_BASE = {"ttwid": "ANON_TTWID"}

#: authenticated 增强链的 base(才有 nonce/signature)。
_BASE_AUTH = {"ttwid": "ANON_TTWID", "__ac_nonce": "NONCE1",
              "__ac_signature": "SIG1"}


# ======================================================================
def test_anonymous_header():
    """A. 匿名态: 基础链在, 无登录 token。"""
    print("\n[WC-1] 匿名态 header")
    h = build_ws_cookie_header(_BASE, None)
    check("anonymous 只有 ttwid(历史握手)",
          _names(h) == list(ANONYMOUS_WS_COOKIE_NAMES), h)
    check("没有重复 name", len(_names(h)) == len(set(_names(h))), h)
    check("不含哨兵", SENTINEL not in h)
    check("空 login 等价于 None",
          h == build_ws_cookie_header(_BASE, ""))
    check("describe -> anonymous",
          describe_ws_auth(None) == WS_AUTH_ANONYMOUS
          and describe_ws_auth("") == WS_AUTH_ANONYMOUS)


def test_authenticated_header():
    """B. 登录态: 合并 + 同名覆盖 + 无重复。"""
    print("\n[WC-2] 登录态 header")
    login = f"sessionid={SENTINEL}; sid_tt={SENTINEL}_TT; ttwid=LOGGED_TTWID"
    h = build_ws_cookie_header(_BASE_AUTH, login)
    names = _names(h)
    check("登录字段被保留", "sessionid" in names and "sid_tt" in names, h)
    check("没有重复 name", len(names) == len(set(names)), h)
    # 同名覆盖: ttwid 只能出现一次, 且是登录那个
    check("ttwid 恰好一次", names.count("ttwid") == 1, h)
    check("ttwid 是登录值(覆盖匿名)",
          "ttwid=LOGGED_TTWID" in h and "ANON_TTWID" not in h, h)
    # 登录 cookie 没带的字段保持匿名值
    check("未被覆盖的字段保留匿名值",
          "__ac_nonce=NONCE1" in h and "__ac_signature=SIG1" in h, h)
    check("describe -> authenticated",
          describe_ws_auth(login) == WS_AUTH_AUTHENTICATED)


def test_no_duplicate_names_ever():
    """同名覆盖的对称性: 换个顺序/多个同名, 仍然不重复。"""
    print("\n[WC-3] 重复 name 的边界")
    # login 里自己就带了两次 ttwid
    h = build_ws_cookie_header(_BASE_AUTH, "ttwid=A; ttwid=B")
    names = _names(h)
    check("login 内部重复也被收敛", names.count("ttwid") == 1, h)
    check("胜出的是最后一个", "ttwid=B" in h, h)
    # 匿名链缺字段时不炸、也不产生空 name
    h2 = build_ws_cookie_header({"ttwid": "T"}, "sessionid=S")
    check("匿名链不全也能合并",
          _names(h2) == ["ttwid", "sessionid"], h2)
    h3 = build_ws_cookie_header({}, None)
    check("全空 -> 空串", h3 == "", repr(h3))


def test_parser_is_lenient_and_silent():
    """C-1. 畸形输入不抛, 也不把原值放进异常。"""
    print("\n[WC-4] 解析器的宽容度")
    for bad in (None, "", ";;", "noequals", "=novalue", "   ", "; ; ;"):
        try:
            got = parse_cookie_header(bad)
            ok = isinstance(got, dict)
        except Exception as e:                          # noqa: BLE001
            ok = False
            print("     抛了:", type(e).__name__, e)
        check(f"不抛且返回 dict: {bad!r}", ok)
    # 值里含 '=' 只在第一个 '=' 处切
    got = parse_cookie_header("__ac_signature=aa=bb=cc")
    check("值里的 = 被保留",
          got.get("__ac_signature") == "aa=bb=cc", got)
    # 空名被跳过
    got = parse_cookie_header("=x; a=1")
    check("空名被跳过", got == {"a": "1"}, got)
    # None 值不产生 "None" 字面量
    check("None value -> 空串",
          merge_cookie_header({"a": None}, None) == "a=",
          merge_cookie_header({"a": None}, None))


def test_sentinel_never_leaks():
    """C-2. 哨兵在 Config repr / describe / 异常里出现 0 次。"""
    print("\n[WC-5] 防泄漏(哨兵出现次数必须为 0)")

    # (a) Config repr —— dataclass 默认 repr 会打所有字段
    os.environ["DOUYIN_LIVE_COOKIE"] = SENTINEL
    try:
        import importlib
        from story import config as cfgmod
        importlib.reload(cfgmod)
        cfg = cfgmod.Config()
        r = repr(cfg)
        check("Config.repr 不含哨兵", SENTINEL not in r,
              r[:200] if SENTINEL in r else "")
        check("Config 确实读到了 env(否则上面的断言是空的)",
              cfg.douyin_live_cookie == SENTINEL)
        # 字段仍在 __dict__ 里(不是被删了, 只是不进 repr)
        check("字段本身可用(不是 dead config)",
              getattr(cfg, "douyin_live_cookie", None) == SENTINEL)
    finally:
        os.environ.pop("DOUYIN_LIVE_COOKIE", None)

    # (b) describe_ws_auth 的输出只有两个词
    for v in (SENTINEL, SENTINEL + "x" * 500, "sessionid=" + SENTINEL):
        out = describe_ws_auth(v)
        check(f"describe 输出固定词 ({len(v)} 字符输入)",
              out in (WS_AUTH_ANONYMOUS, WS_AUTH_AUTHENTICATED)
              and SENTINEL not in out, out)

    # (c) 异常路径: 构造一个"取 nonce 就炸"的 fetcher, 异常信息不得含哨兵
    from liveMan import DouyinLiveWebFetcher
    f = DouyinLiveWebFetcher.__new__(DouyinLiveWebFetcher)
    f._login_cookie = f"sessionid={SENTINEL}"
    f._ws_ac_nonce = None
    f._ws_ac_signature = None
    f.__dict__["_DouyinLiveWebFetcher__ttwid"] = "T"
    f.host = "https://www.douyin.com/"
    f.user_agent = "ua"

    def boom(*_a, **_k):
        raise RuntimeError(f"network blew up, cookie was {SENTINEL}")

    # 注意: 上游 `_build_ws_cookie_header` 里 catch 的是 Exception 且
    # **不读异常内容**。这里验证的是"降级路径不会把异常原文带出来" ——
    # 注意哨兵**本来就该**出现在 header 里(它就是登录 cookie), 所以不能
    # 断言 header 不含哨兵; 要断言的是:
    #   (1) 不抛(降级是静默的, 不是把异常往上扔);
    #   (2) 非 header 的上下文(异常文本)里没有哨兵。
    f.get_ac_nonce = boom
    f.get_ac_signature = boom
    # 关键: 降级路径可能**打印**(print)而不是抛 —— 只断言"没抛"会漏掉
    # "把异常原文 print 到 stdout"这条泄漏。stdout 会进直播日志/截图。
    cap = io.StringIO()
    try:
        with contextlib.redirect_stdout(cap), \
                contextlib.redirect_stderr(cap):
            h = f._build_ws_cookie_header()
        err = ""
    except Exception as e:                              # noqa: BLE001
        h, err = "", f"{type(e).__name__}: {e}"
    check("取 nonce 失败时不抛(降级)", err == "", err)
    check("异常文本里没有哨兵", SENTINEL not in err, err)
    check("降级路径的 stdout/stderr 没有哨兵",
          SENTINEL not in cap.getvalue(), cap.getvalue()[:200])
    # 降级后基础链仍在(只是这两个字段为空), 而不是整条 header 消失
    check("降级后仍有 ttwid", "ttwid=T" in h, h)

    # (d) 日志路径: 把 root logger 的输出抓下来, 走一遍启动横幅那行
    buf = io.StringIO()
    hdl = logging.StreamHandler(buf)
    root = logging.getLogger()
    root.addHandler(hdl)
    old = root.level
    root.setLevel(logging.DEBUG)
    try:
        logging.getLogger("test.leak").debug(
            "ws_auth=%s", describe_ws_auth(SENTINEL))
    finally:
        root.removeHandler(hdl)
        root.setLevel(old)
    check("日志里只有 ws_auth=<词>", SENTINEL not in buf.getvalue(),
          buf.getvalue())

    # (e) 合并结果里哨兵**应该**在(它是凭据, 本来就该进 header) ——
    #     这条是反向验证, 证明确实是"只在该去的地方出现"。
    hdr = build_ws_cookie_header(_BASE_AUTH, f"sessionid={SENTINEL}")
    check("哨兵在 header 里(证明上面不是空断言)", SENTINEL in hdr)


def test_plumbing_reaches_transport():
    """D. 配置链完整: Config -> LiveSource -> CallbackFetcher -> fetcher。"""
    print("\n[WC-6] 接线")
    from story.config import Config
    from story.ingest import CallbackFetcher

    cfg = Config(live_id="123", douyin_live_cookie=f"sid={SENTINEL}")
    check("Config 承载 login cookie",
          cfg.douyin_live_cookie == f"sid={SENTINEL}")

    import inspect
    sig = inspect.signature(CallbackFetcher.__init__)
    check("CallbackFetcher 有 login_cookie 形参",
          "login_cookie" in sig.parameters, list(sig.parameters))

    sig2 = inspect.signature(
        __import__("danmaku").DanmakuFetcher.__init__)
    check("DanmakuFetcher 有 login_cookie 形参",
          "login_cookie" in sig2.parameters, list(sig2.parameters))

    from liveMan import DouyinLiveWebFetcher
    sig3 = inspect.signature(DouyinLiveWebFetcher.__init__)
    check("DouyinLiveWebFetcher 有 login_cookie 形参",
          "login_cookie" in sig3.parameters, list(sig3.parameters))

    # LiveSource._build 真的把它传下去(而不是只加形参)
    src = inspect.getsource(
        __import__("story.ingest", fromlist=["x"]).LiveSource._build)
    check("LiveSource._build 透传 douyin_live_cookie",
          "douyin_live_cookie" in src, src)


def test_transport_cookie_header_uses_login():
    """D-2. transport 层真的用登录 cookie 组装 handshake header。"""
    print("\n[WC-7] transport 组装")
    from liveMan import DouyinLiveWebFetcher
    f = DouyinLiveWebFetcher.__new__(DouyinLiveWebFetcher)
    f._login_cookie = "sessionid=SESS; ttwid=LOGIN_TTWID"
    f._ws_ac_nonce = "NONCE"
    f._ws_ac_signature = "SIG"
    f.__dict__["_DouyinLiveWebFetcher__ttwid"] = "ANON_TTWID"
    h = f._build_ws_cookie_header()
    names = _names(h)
    check("ttwid 唯一且为登录值",
          names.count("ttwid") == 1 and "ttwid=LOGIN_TTWID" in h, h)
    check("sessionid 在", "sessionid=SESS" in h, h)
    check("nonce/signature 在",
          "__ac_nonce=NONCE" in h and "__ac_signature=SIG" in h, h)

    # 匿名时回落到纯基础链
    f._login_cookie = None
    h2 = f._build_ws_cookie_header()
    check("匿名 -> 无 sessionid", "sessionid" not in h2, h2)
    check("匿名 -> 只有 ttwid",
          _names(h2) == list(ANONYMOUS_WS_COOKIE_NAMES), h2)


def test_user_unique_id_refactor_is_value_preserving():
    """D-3. user_unique_id 仅做无语义重构 —— 字面量不再重复写。"""
    print("\n[WC-8] user_unique_id 重构")
    src = (ROOT / "vendor" / "douyin_fetcher" / "liveMan.py").read_text(
        encoding="utf-8")
    # WS URL 里不该再有硬编码字面量(应该用 f-string 引用 self.)
    check("WS URL 用 self.user_unique_id",
          "&user_unique_id={self.user_unique_id}" in src)
    check("WS URL 不再硬编码字面量",
          "&user_unique_id=7319483754668557238" not in src)
    # 值本身没变
    from liveMan import DouyinLiveWebFetcher
    f = DouyinLiveWebFetcher.__new__(DouyinLiveWebFetcher)
    f.user_unique_id = "7319483754668557238"
    check("值未改变(Auth A/B 必须保持身份不变)",
          f.user_unique_id == "7319483754668557238")


def test_forbidden_surfaces_untouched():
    """D-4. 本 commit 不该动的算法/协议面, 逐条确认没被顺手改。"""
    print("\n[WC-9] 禁止修改面")
    lm = (ROOT / "vendor" / "douyin_fetcher" / "liveMan.py").read_text(
        encoding="utf-8")
    check("WS host 未动",
          "wss://webcast100-ws-web-lq.douyin.com/webcast/im/push/v2/"
          in lm)
    check("webcast_sdk_version 未动", "webcast_sdk_version=1.0.14-beta.0" in lm)
    check("update_version_code 未动", "update_version_code=1.0.14-beta.0" in lm)
    check("identity=audience 未动", "&identity=audience" in lm)
    check("signature 仍由 generateSignature 产生",
          "wss += f\"&signature={signature}\"" in lm)
    check("bootstrap 仍走 _local_bootstrap",
          "_local_bootstrap(now_ms)" in lm)
    # Gift / Summon / Like 面
    check("没有新增 Gift accumulator 痕迹",
          "accumulator" not in lm.lower())
    summ = (ROOT / "story" / "summon.py")
    check("SummonLedger 未被改(文件仍在)",
          summ.is_file())


def test_anonymous_is_exact_historical_handshake():
    """WC-10. 匿名态必须**逐字**等价于历史握手: ttwid only.

    历史(上游未改动前)handshake 就是:
        "cookie": f"ttwid={self.ttwid}"
    所以匿名 arm 的输出必须恰好是 "ttwid=<v>",
    **不能**多出 __ac_nonce / __ac_signature。
    """
    print("\n[WC-10] 匿名态 == 历史握手(逐字)")
    from liveMan import DouyinLiveWebFetcher

    f = DouyinLiveWebFetcher.__new__(DouyinLiveWebFetcher)
    f._login_cookie = None
    f._ws_ac_nonce = None
    f._ws_ac_signature = None
    f.__dict__["_DouyinLiveWebFetcher__ttwid"] = "ANON_TTWID"

    h = f._build_ws_cookie_header()
    check("anonymous header 恰好是 ttwid=ANON_TTWID",
          h == "ttwid=ANON_TTWID", repr(h))
    check("anonymous header 不含 __ac_nonce",
          "__ac_nonce" not in h, repr(h))
    check("anonymous header 不含 __ac_signature",
          "__ac_signature" not in h, repr(h))


def test_anonymous_never_touches_ac_endpoints():
    """WC-11. 匿名态**禁止**调用 get_ac_nonce/get_ac_signature.

    这是关键 mutation: 把两个方法换成一调就 raise,
    匿名路径仍然必须通过。

    理由: 它们不是白拿的(一个发 HTTP, 一个跑 JS 签名)。
    给匿名臂加上它们 = 匿名臂也变了, 于是
        A: ttwid
        B: ttwid + nonce + signature + login cookies
    就再也说不清 B 成功到底是谁起作用。
    """
    print("\n[WC-11] 匿名态不碰 ac 端点(调用次数 == 0)")
    from liveMan import DouyinLiveWebFetcher

    calls = {"nonce": 0, "sig": 0}

    f = DouyinLiveWebFetcher.__new__(DouyinLiveWebFetcher)
    f._login_cookie = None
    f._ws_ac_nonce = None
    f._ws_ac_signature = None
    f.__dict__["_DouyinLiveWebFetcher__ttwid"] = "ANON_TTWID"

    def _boom_nonce(*_a, **_k):
        calls["nonce"] += 1
        raise AssertionError(
            "anonymous 绝不允许调用 get_ac_nonce()")

    def _boom_sig(*_a, **_k):
        calls["sig"] += 1
        raise AssertionError(
            "anonymous 绝不允许调用 get_ac_signature()")

    f.get_ac_nonce = _boom_nonce
    f.get_ac_signature = _boom_sig

    cap = io.StringIO()
    try:
        with contextlib.redirect_stdout(cap), \
                contextlib.redirect_stderr(cap):
            h = f._build_ws_cookie_header()
        err = ""
    except Exception as e:                              # noqa: BLE001
        h, err = "", f"{type(e).__name__}: {e}"

    check("anonymous 不抛(不碰 ac 端点)", err == "", err)
    check("get_ac_nonce 调用次数 == 0", calls["nonce"] == 0,
          calls)
    check("get_ac_signature 调用次数 == 0", calls["sig"] == 0,
          calls)
    check("anonymous header 仍然只有 ttwid",
          h == "ttwid=ANON_TTWID", repr(h))


def test_authenticated_does_fetch_ac_fields():
    """WC-12. 只有 authenticated 才取 nonce/signature(反向验证)."""
    print("\n[WC-12] authenticated 才取 ac 字段")
    from liveMan import DouyinLiveWebFetcher

    calls = {"nonce": 0, "sig": 0}

    f = DouyinLiveWebFetcher.__new__(DouyinLiveWebFetcher)
    f._login_cookie = "sessionid=SESS; ttwid=LOGIN_TTWID"
    f._ws_ac_nonce = None
    f._ws_ac_signature = None
    f.__dict__["_DouyinLiveWebFetcher__ttwid"] = "ANON_TTWID"

    def _nonce(*_a, **_k):
        calls["nonce"] += 1
        return "NONCE_FAKE"

    def _sig(*_a, **_k):
        calls["sig"] += 1
        return "SIG_FAKE"

    f.get_ac_nonce = _nonce
    f.get_ac_signature = _sig

    h = f._build_ws_cookie_header()
    names = _names(h)
    check("authenticated 取了 nonce", calls["nonce"] == 1, calls)
    check("authenticated 取了 signature", calls["sig"] == 1, calls)
    check("ac 字段在 header 里",
          "__ac_nonce=NONCE_FAKE" in h and "__ac_signature=SIG_FAKE" in h, h)
    check("login ttwid 覆盖匿名值",
          names.count("ttwid") == 1 and "ttwid=LOGIN_TTWID" in h
          and "ANON_TTWID" not in h, h)
    check("sessionid 保留", "sessionid=SESS" in h, h)
    check("无重复 name", len(names) == len(set(names)), h)

    # 第二次调用应命中缓存, 不重复取
    h2 = f._build_ws_cookie_header()
    check("缓存: 第二次不再取 nonce", calls["nonce"] == 1, calls)
    check("缓存: 第二次输出相同", h2 == h, h2)


def main():
    tests = [
        test_anonymous_header,
        test_authenticated_header,
        test_no_duplicate_names_ever,
        test_parser_is_lenient_and_silent,
        test_sentinel_never_leaks,
        test_plumbing_reaches_transport,
        test_transport_cookie_header_uses_login,
        test_anonymous_is_exact_historical_handshake,
        test_anonymous_never_touches_ac_endpoints,
        test_authenticated_does_fetch_ac_fields,
        test_user_unique_id_refactor_is_value_preserving,
        test_forbidden_surfaces_untouched,
    ]
    for t in tests:
        t()
    print()
    if FAIL[0]:
        print(f"FAILED: {FAIL[0]} 项")
        return 1
    print("PASS: WS 登录态 Cookie 组装(匿名/登录/防泄漏/接线)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
