#!/usr/bin/env python
# coding: utf-8
"""WS handshake 的 Cookie 组装 + 登录态(12B-Auth)。

## 这个模块解决什么

实测: 游客态 WS 能收到 Chat / Member / Like / Room, 但**多轮
`WebcastGiftMessage = 0`**(即使确认观众真的送了礼)。多个公开实现报告
Gift 需要**登录态 Cookie**。

本模块只做一件事: 把"基础身份 cookie"与"登录态 cookie"**按 cookie name
合并**成一个 header 值。它是纯函数, 不碰网络、不碰 Config、不读环境变量
—— 那些由调用方注入, 于是测试可以完全确定性地验证合并语义。

## 为什么不是字符串拼接

最直觉的写法是 `f"{base}; {login}"`。那是错的:

    ttwid=anonymous; ...; ttwid=logged-in

HTTP 对重复 cookie name 的处理是"服务端自便" —— 有的取第一个, 有的取最后
一个, 有的取全部。于是**能不能登录取决于服务端实现**, 而且症状是静默的:
连接照样建立, Gift 照样不来, 看起来像"假设又错了", 实际上是 cookie 没生效。

所以我们解析成 name -> value 的**映射**再合并, 登录态覆盖同名, 最后序列化
**一次** —— 保证输出里任意 cookie name 至多出现一次。

## ⚠️ 敏感边界(本模块的全部理由)

登录 cookie 是**凭据**。它绝不能出现在: 日志 / 异常 / repr / JSONL /
snapshot / API / UI。本模块的纪律:

- `merge_cookie_header()` 只接收**已解析的映射**, 不接收原始字符串时无法
  意外把整串塞进错误信息;
- `parse_cookie_header()` 对畸形输入**静默跳过**该片段, 不抛带原值的异常;
- `describe_ws_auth()` 只返回 `"anonymous"` / `"authenticated"` 两个词之一
  —— 这是唯一允许出现在日志里的形态(没有长度、前缀、hash)。
"""

from __future__ import annotations

from typing import Mapping, Optional

#: 日志 / 启动横幅里唯一允许出现的认证态字符串。
WS_AUTH_ANONYMOUS = "anonymous"
WS_AUTH_AUTHENTICATED = "authenticated"

#: 匿名模式必须保留的基础身份 cookie 链。
#:
#: ⚠️ 这三个是**给服务端看的身份字段**, 不是凭据。它们本来就已经写死在
#: 上游 `liveMan.py` 的 handshake 里(`ttwid={self.ttwid}`), 本模块只是
#: 把 `__ac_nonce` / `__ac_signature` 也纳入同一套组装, 免得两处各拼一次。
BASE_WS_COOKIE_NAMES = ("ttwid", "__ac_nonce", "__ac_signature")


def describe_ws_auth(login_cookie: Optional[str]) -> str:
    """登录态 -> 可安全打日志的一个词。

    **只看"有没有"**, 绝不看内容 —— 不做长度、前缀、hash, 那些都是
    凭据的部分信息, 累积起来足以做指纹。
    """
    return WS_AUTH_AUTHENTICATED if login_cookie else WS_AUTH_ANONYMOUS


def parse_cookie_header(raw: Optional[str]) -> dict:
    """`"a=1; b=2"` -> `{"a": "1", "b": "2"}`。

    宽松解析, 理由是真实 cookie 串来自浏览器(人工复制), 格式不保证规整:

    - 空 / None -> 空 dict(不抛);
    - 片段没有 `=` 或是空名 -> **跳过**(例如有人多打了一个 `;`);
    - 值里含 `=` -> 只在**第一个** `=` 处切分(`__ac_signature` 的值可能
      含 `=`, 见上游 `get_ac_signature` 的返回值);
    - 名/值两端去空白。

    **畸形输入绝不抛异常, 也绝不把原值放进异常信息** —— 抛带原值的异常
    就等于把凭据写进 traceback。
    """
    out: dict = {}
    if not raw:
        return out
    for part in str(raw).split(";"):
        frag = part.strip()
        if not frag or "=" not in frag:
            continue
        name, _, value = frag.partition("=")
        name = name.strip()
        if not name:
            continue
        out[name] = value.strip()
    return out


def merge_cookie_header(base: Optional[Mapping], login: Optional[Mapping]) -> str:
    """合并基础 cookie 与登录 cookie, 序列化成**单个** header 值。

    合并顺序: `base` -> `login` 覆盖同名。**登录态优先**是刻意的 ——
    登录 cookie 里若自带 `ttwid`, 那个才是账号真正的身份, 而我们匿名取到的
    `ttwid` 是访客身份。反过来让匿名值赢, 就等于"拿了登录 cookie 却仍然以
    访客身份连接", 症状与没有登录态完全一样, 极难排查。

    输出保证: **任意 cookie name 至多出现一次**。
    """
    merged: dict = {}
    for src in (base, login):
        if not src:
            continue
        for name, value in src.items():
            if not name:
                continue
            merged[str(name)] = "" if value is None else str(value)
    return "; ".join(f"{k}={v}" for k, v in merged.items())


def build_ws_cookie_header(base: Optional[Mapping],
                           login_cookie: Optional[str] = None) -> str:
    """匿名 / 登录两态的统一入口。

    - `login_cookie` 为空 -> 纯基础链(与上游原行为等价);
    - 非空 -> 解析后覆盖合并。

    ⚠️ 返回值**是凭据**, 只能交给 `websocket.WebSocketApp(header=...)`。
    不要打日志、不要放进异常、不要落库。
    """
    return merge_cookie_header(base, parse_cookie_header(login_cookie))
