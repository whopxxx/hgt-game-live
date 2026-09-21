#!/usr/bin/env python
# coding: utf-8
"""受控的连接画像(profile)定义 —— Step 12C。

## 为什么要"画像"

外部对照结论(Issue §外部调研)说得很清楚: 匿名连接可能收不到
`WebcastGiftMessage`, 而当前可工作的参考实现与我们的连接参数**不止一处
不同**。直接照抄对方全部参数是没用的 —— 那等于同时改了五六个变量, 以后
再出问题还是不知道是谁的锅。

所以这里把差异拆成**三个可归因的臂**, 每臂只动一个维度:

    A  current-auth-control  当前生产画像(基线), 带登录 Cookie
    B  random-uid-only       与 A 完全相同, **唯一变量** = 每连接随机 uid
    C  reference-2026        登录 Cookie + 每连接随机 uid + fresh now_ms
                             cursor/internal_ext(对齐公开参考实现)

这是**诊断/实验**配置, 不是业务配置 —— 它只决定"用哪套连接参数连"。
解析出来的消息、回调、落库形态在三个臂之间完全一致, 否则对照就失去意义。

## 为什么 B 只动 uid

`user_unique_id` 在本项目里是**写死的常量**(`7319483754668557238`),
而公开参考实现每次都换。若固定 uid 真的影响 Gift 订阅, 那么"我们收不到、
别人收得到"就有了一个成本极低的解释。B 就是为这一条准备的: A 与 B 的
URL 除了 `user_unique_id` **一个字符都不许不同**(有测试钉住)。

## 为什么 C 是"reference matching"臂

Issue 明确允许: 若实现者认为 C 同时改两个变量不够干净, 可以作为
"reference matching"臂保留。我们就是这么做的 —— C 的价值不是"单变量
归因", 而是"**先别猜协议, 先确认能不能复现对方的结果**"。一旦 C 收到
Gift 而 A/B 都没有, 下一步就是把 C 的两个变量拆成两个单变量臂(下一轮
Issue 的事, 不在本轮范围)。

## ⚠️ 三个臂的并发上限

默认**最多 3 路**。不是为了省事, 是为了不要无意义地制造大量连接 ——
多开连接本身就可能触发风控, 那会让实验结论变成"又被限流了"。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Optional

from ws_cookie import WS_AUTH_ANONYMOUS, WS_AUTH_AUTHENTICATED

#: 当前生产画像写死的 `user_unique_id`(见 `liveMan.DouyinLiveWebFetcher`)。
#: ⚠️ 这是**诊断画像 A 的复现值**, 不是"生产常量"的唯一定义处 —— 生产那
#: 一份仍在 `vendor/douyin_fetcher/liveMan.py`。两处若漂移, 测试会红。
CURRENT_USER_UNIQUE_ID = "7319483754668557238"

#: 随机 uid 的取值范围(Issue 指定 7e18 ~ 8e18)。抖音的 uid 是 64 位
#: 无符号整数, 这个区间落在真实 uid 的常见量级内。
RANDOM_UID_MIN = 7_000_000_000_000_000_000
RANDOM_UID_MAX = 8_000_000_000_000_000_000

#: bootstrap 来源的两种取值。
#:
#: ⚠️ 先说清一件容易搞错的事(这也是本 Step 被 review 打回的点):
#: 本项目**生产**的 cursor/internal_ext 生成器(`vendor/.../ws_bootstrap.py`)
#: 本来就是照公开参考实现 `JaneEyre3007/douyin-js` 的 `genCursorInternalExt`
#: 写的 —— 也就是说"reference 形状"与"当前 local 生成"在本仓库里**是同一份
#: 实现**。所以 `bootstrap=reference` 如果只是换个标签, 这一臂就形同虚设,
#: 真实直播里"C 也没收到"会得出"reference 方案无效"的**错误结论**。
#:
#: 因此这两个取值的差别必须是**真实存在、可被 URL 断言验证**的:
#:
#: - `local`     : `_local_bootstrap(now_ms)`, 即当前生产路径。身份 id 用
#:                 本 profile 的 `user_unique_id`(A 是固定值)。
#: - `reference` : 走 `_reference_bootstrap(now_ms)`, 它把参考实现里
#:                 **每次连接都换**的那组身份/时间字段显式参数化 —— 特别是
#:                 `wss_push_did`(参考实现每次连接随机)与 `seq`/`wrds_v`
#:                 的每连接重新取值。A 臂的 `did` 全场固定, C 臂每次连接都变。
#:
#: 这样 A/B/C 在 URL 层面**真的**不同, 而且差异是"InternalExt 的身份/时间
#: 字段"这一条被测断言钉住的东西, 不是标签。
BOOTSTRAP_LOCAL = "local"
BOOTSTRAP_REFERENCE = "reference"

#: 允许的 profile id。**闭集** —— 拼错一个名字就报错, 而不是静默退化
#: 成一个"看起来跑了三路、其实两路一模一样"的实验。
PROFILE_A = "current-auth-control"
PROFILE_B = "random-uid-only"
PROFILE_C = "reference-2026"
KNOWN_PROFILES = (PROFILE_A, PROFILE_B, PROFILE_C)

#: 默认并发臂数(Issue: 最多 2~3 路)。
DEFAULT_MAX_CONCURRENT_PROFILES = 3

#: 诊断态默认允许的连接臂数上限。运行时可以再调小, 但**不能**调大过它 ——
#: 上限存在的意义就是防止"顺手把十种组合都打开"。
HARD_MAX_CONCURRENT_PROFILES = 3


def generate_random_user_unique_id(rng: Optional[random.Random] = None) -> str:
    """生成一个 7e18~8e18 范围内的 `user_unique_id`。

    纯函数, **随机源必须可注入** —— 否则测试无法稳定验证"每次连接都变"
    与"格式合法"这两条。

    返回**字符串**: 这个值最终要拼进 URL 的查询串。用 int 的话, 下游某个
    地方一旦做 `str()` 转换就会与真实实现产生格式差异(例如未来的某个
    实现回落到科学计数法), 而这种差异在 URL 里是不可见的。
    """
    r = rng if rng is not None else random.Random()
    return str(r.randrange(RANDOM_UID_MIN, RANDOM_UID_MAX + 1))


@dataclass(frozen=True)
class GiftProbeProfile:
    """一条诊断连接的**连接画像**。

    只描述"怎么连", 不描述"收到之后做什么" —— 后者在所有臂之间必须
    一致, 所以不该出现在这里。

    Attributes:
        profile_id: 闭集里的一个名字(见 `KNOWN_PROFILES`)。
        random_user_unique_id: True = 每次连接现场生成 uid;
            False = 用 `CURRENT_USER_UNIQUE_ID`(当前生产画像)。
        bootstrap: `local` / `reference`。见 `BOOTSTRAP_*`。
        注意: **本对象不持有 Cookie**。它只记录"这一臂是不是走登录态"这
        一个布尔判断的结果(auth mode), 凭据本身由 transport 层从 Config
        取 —— 让凭据在一个会被 repr / 序列化 / 写进 probe index 的数据类
        里出现, 是这类泄漏最常见的入口。
    """

    profile_id: str
    random_user_unique_id: bool = False
    bootstrap: str = BOOTSTRAP_LOCAL
    #: 人类可读的一行说明, 只用于日志与 PR 描述。**不得**放入凭据。
    label: str = ""

    def user_unique_id(self, rng: Optional[random.Random] = None) -> str:
        """本连接该用的 `user_unique_id`。

        `random_user_unique_id=False` 时**逐字**返回生产画像的常量 ——
        A 臂必须是"当前行为"的忠实复现, 否则它就没有资格当控制组。
        """
        if self.random_user_unique_id:
            return generate_random_user_unique_id(rng)
        return CURRENT_USER_UNIQUE_ID

    def describe(self) -> dict:
        """可安全落盘 / 打日志的画像描述(**绝不含凭据**)。"""
        return {
            "profile": self.profile_id,
            "user_unique_id_mode": ("random" if self.random_user_unique_id
                                    else "fixed"),
            "bootstrap": self.bootstrap,
        }


#: 默认三臂。A/B 只差 `user_unique_id`, C 额外把 bootstrap 换成
#: reference 形状并且每次连接都用 fresh now_ms。
DEFAULT_PROFILES = (
    GiftProbeProfile(
        PROFILE_A, random_user_unique_id=False, bootstrap=BOOTSTRAP_LOCAL,
        label="当前生产画像(控制组): 固定 uid + 当前 bootstrap + 登录态"),
    GiftProbeProfile(
        PROFILE_B, random_user_unique_id=True, bootstrap=BOOTSTRAP_LOCAL,
        label="单变量: 与 A 完全相同, 只把 user_unique_id 换成每连接随机"),
    GiftProbeProfile(
        PROFILE_C, random_user_unique_id=True, bootstrap=BOOTSTRAP_REFERENCE,
        label="reference matching: 登录态 + 随机 uid + fresh now_ms bootstrap"),
)


def default_profiles(limit: int = DEFAULT_MAX_CONCURRENT_PROFILES
                     ) -> list:
    """默认臂列表, 受并发上限约束。

    `limit` 会被夹在 `[1, HARD_MAX_CONCURRENT_PROFILES]` 内。夹取而不是
    报错: 用户把 `--gift-probe-profiles 9` 传进来时, 我们要的是"最多三路"
    这个**安全性质**, 而不是让整场直播因为一个参数写大就起不来。
    """
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = DEFAULT_MAX_CONCURRENT_PROFILES
    n = max(1, min(n, HARD_MAX_CONCURRENT_PROFILES, len(DEFAULT_PROFILES)))
    return list(DEFAULT_PROFILES[:n])


def select_profiles(names, limit: int = DEFAULT_MAX_CONCURRENT_PROFILES
                    ) -> list:
    """按名字挑选臂; 未知名 -> `ValueError`。

    **不静默忽略**未知名: 一个拼错的 `--gift-probe-profile ramdom-uid-only`
    若被吞掉, 实验就变成"只跑了 A/C 而报告里写着三路", 而日志上完全正常。
    这种故障比启动失败糟得多。

    顺序按 `names` 给出的顺序**去重后**保留 —— 便于实测时把最关键的一臂
    排在最前面看。
    """
    limit = max(1, min(int(limit), HARD_MAX_CONCURRENT_PROFILES))
    if not names:
        return default_profiles(limit)
    by_id = {p.profile_id: p for p in DEFAULT_PROFILES}
    out = []
    for raw in names:
        name = str(raw or "").strip()
        if not name:
            continue
        if name not in by_id:
            raise ValueError(
                f"未知的 gift probe profile: {name!r}; "
                f"可选: {', '.join(KNOWN_PROFILES)}")
        if by_id[name] not in out:
            out.append(by_id[name])
    if not out:
        return default_profiles(limit)
    return out[:limit]


def describe_auth(login_cookie: Optional[str]) -> str:
    """登录态 -> 可安全打日志的一个词。

    直接复用 `ws_cookie.describe_ws_auth`。**刻意不在这里重新实现一遍**
    —— 认证态的描述只有一个真相来源, 两份实现迟早会漂移成"一边说
    authenticated 一边说 anonymous"。
    """
    from ws_cookie import describe_ws_auth
    return describe_ws_auth(login_cookie)


def login_cookie_is_usable(login_cookie: Optional[str]) -> bool:
    """最小健康检查: 解析后**至少存在一个非空 cookie pair**。

    Issue 要求: "如果可以在不暴露凭据的情况下做最小健康检查, 可仅验证
    '解析后至少存在一个非空 cookie pair', 失败则该 profile 标记
    config_invalid; 不要输出内容。"

    所以这个函数**只返回 bool**, 不返回任何片段、长度、name 列表。
    形如 `";;;"` / `"novalue"` / `"   "` 的串会被判为不可用 —— 它们会让
    连接静默地以"没有登录态"的方式建立, 而日志上却写着 authenticated。
    """
    if not login_cookie:
        return False
    try:
        from ws_cookie import parse_cookie_header
        parsed = parse_cookie_header(login_cookie)
    except Exception:                       # noqa: BLE001
        # 解析器本身出问题不该让诊断模式炸掉 —— 判为不可用即可。
        return False
    return any(
        str(v).strip() for v in parsed.values()
    ) if parsed else False


#: profile 的健康状态(进 summary 的可观察字段)。
PROFILE_OK = "ok"
PROFILE_CONFIG_INVALID = "config_invalid"


def profile_auth_state(profile: GiftProbeProfile,
                       login_cookie: Optional[str]) -> dict:
    """把"这一臂到底进了什么认证态"变成一个可观察的结论。

    返回的字典里**只有** `auth` / `config_state` 两个可安全外露的字段 ——
    没有长度、没有 hash、没有前缀、没有 cookie name 列表(Issue 里点名
    禁止的四样东西)。

    语义:
        - 没有 Cookie         -> auth=anonymous,  state=ok
        - 有 Cookie 且可解析  -> auth=authenticated, state=ok
        - 有 Cookie 但解析不出任何非空 pair
                              -> auth=anonymous, state=config_invalid

    最后那条是**刻意**的: 配了 Cookie 却连一个有效 pair 都解析不出来时,
    连接**实际上**就是匿名的。把它报成 authenticated 会让"三种画像都
    收不到 Gift"这个结论指向完全错误的方向(以为登录态已生效)。
    """
    if not login_cookie:
        return {"auth": WS_AUTH_ANONYMOUS, "config_state": PROFILE_OK}
    if login_cookie_is_usable(login_cookie):
        return {"auth": WS_AUTH_AUTHENTICATED, "config_state": PROFILE_OK}
    return {"auth": WS_AUTH_ANONYMOUS, "config_state": PROFILE_CONFIG_INVALID}


def format_profile_log(profile: GiftProbeProfile, auth_state: dict) -> str:
    """一行可安全打日志的画像描述。

    只拼 `describe()` 与 `auth_state` 里的白名单键 —— 任何将来被塞进
    `auth_state` 的新键都不会自动流进日志, 需要显式加进这里, 于是"不小心
    把凭据打出来"这条路径在结构上就不存在。
    """
    d = profile.describe()
    return (f"profile={d['profile']} uid={d['user_unique_id_mode']} "
            f"bootstrap={d['bootstrap']} "
            f"auth={auth_state.get('auth')} "
            f"config={auth_state.get('config_state')}")


@dataclass
class ProfileCounters:
    """**每个 profile 独占**的计数器(Step 12C 的隔离要求)。

    为什么必须是每臂一个实例、而不是共享一个 dict:
    三路连接的数据**必须串不到一起**。若共用一个计数器, "A 收到 3 条
    Gift、B 收到 0 条"会变成"A/B 各 3 条"或者总量 3 条 —— 那么整个
    对照实验的核心结论(哪种画像能收到)就是错的, 而且看起来完全正常。
    Issue 的 mutation verification 里专门点名了这条。

    计数器只装**方法名与计数**, 不装 payload / uid / 昵称。
    """

    profile_id: str
    connection_generation: int = 0
    ws_frames: int = 0
    ws_messages: int = 0
    method_counts: dict = field(default_factory=dict)
    unhandled_counts: dict = field(default_factory=dict)
    parse_error_counts: dict = field(default_factory=dict)
    #: 三层可分辨的 Gift 计数(Issue 要求 11):
    #:   method_seen       服务端下发了 Gift-family method
    #:   parsed_as_primary 该 method 是 primary GiftMessage 且解析成功
    #:   emitted           解析成功并交给了业务回调
    gift_method_seen: int = 0
    parsed_gift_count: int = 0
    emitted_gift_count: int = 0
    #: 原始 payload 落盘成功的条数(与"命中捕获规则"分开计 ——
    #: 命中规则但因为达到上限被跳过, 不该算 capture 成功)。
    captured_payload_count: int = 0
    #: 达到样本上限而**未**落盘的条数。有它才能区分"没有 Gift"与
    #: "Gift 多得把样本额度打满了" —— 后者会让 capture 数看起来正常。
    capture_skipped_count: int = 0
    auth: str = ""
    config_state: str = ""
    #: 本路**实际生效**的 bootstrap 模式(`local` / `reference`)。
    #: ⚠️ 这不是元数据冗余: Blocker 1 的根因就是"profile 上写着 reference,
    #: 但连接层从来没读过它"。把这个值记进 counters 并被摘要打印, 意味着
    #: "哪条路径真的跑了"是一个**可被观测到**的事实, 而不是靠读 profile
    #: 猜测的意图。
    bootstrap_mode: str = ""
    first_frame_at: float = 0.0
    last_frame_at: float = 0.0

    def bump_method(self, method: str) -> None:
        self.method_counts[method] = self.method_counts.get(method, 0) + 1
        if is_gift_family_method(method):
            self.gift_method_seen += 1

    def bump_unhandled(self, method: str) -> None:
        self.unhandled_counts[method] = \
            self.unhandled_counts.get(method, 0) + 1

    def bump_parse_error(self, method: str) -> None:
        self.parse_error_counts[method] = \
            self.parse_error_counts.get(method, 0) + 1

    def gift_family_methods(self) -> dict:
        """method 名包含 `Gift` 的子集(Issue 明确要求的分类)。

        用**子串**匹配而不是白名单: 公开 proto 参考里已经有一串
        `Webcast*Gift*Message`(`WebcastGiftMessage` /
        `WebcastBindingGiftMessage` / `WebcastGiftPlayEventMessage` /
        `WebcastGiftSortMessage` / `WebcastGiftUpdateMessage` /
        `WebcastLightGiftMessage` / `WebcastGiftEffectGameMessage`), 而且
        平台随时会加新的。白名单会把"新出现的 Gift-family method"静默
        漏掉 —— 那正是本轮要避免的事(见 Issue §4)。
        """
        return {k: v for k, v in sorted(self.method_counts.items())
                if is_gift_family_method(k)}

    def summary(self) -> dict:
        """结构化摘要 —— 只含方法名/计数/认证态词, **不含**任何内容。

        这条摘要必须能在直播中每 20~30 秒安全打印一次, 所以它**不能**
        含 cookie / raw payload / 用户昵称(Issue §D 的硬要求)。
        """
        return {
            "profile": self.profile_id,
            "connection_generation": self.connection_generation,
            "auth": self.auth,
            "config_state": self.config_state,
            "bootstrap_mode": self.bootstrap_mode,
            "ws_frames": self.ws_frames,
            "ws_messages": self.ws_messages,
            "methods": dict(sorted(self.method_counts.items())),
            "gift_family_methods": self.gift_family_methods(),
            "unhandled": dict(sorted(self.unhandled_counts.items())),
            "parse_errors": dict(sorted(self.parse_error_counts.items())),
            "gift_method_seen": self.gift_method_seen,
            "parsed_gift_count": self.parsed_gift_count,
            "emitted_gift_count": self.emitted_gift_count,
            "captured_payload_count": self.captured_payload_count,
            "capture_skipped_count": self.capture_skipped_count,
        }

    def verdict(self) -> str:
        """按 Issue §D 的四层判据给出"该往哪查"的结论。

        这是本包最有价值的一行输出: 真实送礼之后, 不需要等下播、不需要
        猜, 直接看这一路落在哪一层。

            Gift-family = 0                  -> 连接/订阅/认证层
            Gift-family > 0, unhandled > 0   -> dispatch 层
            handled > 0, parse_error > 0     -> proto 层
            parse 成功, emitted = 0          -> callback / ingest 层

        ⚠️ 判据的顺序**就是优先级**, 不要重排。四层是"从外往里"的漏斗:
        服务端没给 -> 给了但没进 handler -> 进了 handler 但解析炸了 ->
        解析成功但业务链没收到。顺序反了会得到自相矛盾的结论(例如同时
        "dispatch"与"proto"), 而那正是现场最难判断的形状。

        ⚠️ `unhandled` 排在 `parse_error` **之前**: 一条 Gift 同时"没有
        handler"与"曾 parse 失败"时, 先要查的是为什么没有 handler ——
        那是更靠外的一层, 修好它之前 proto 层的观察本身都不成立。
        """
        if self.gift_method_seen == 0:
            return "transport_or_auth"      # 服务端根本没给 Gift-family
        if self.gift_family_methods_unhandled():
            return "dispatch"
        if self.gift_parse_errors():
            return "proto"
        if self.parsed_gift_count > 0 and self.emitted_gift_count == 0:
            return "callback_or_ingest"
        return "ok"

    def gift_family_methods_unhandled(self) -> bool:
        return any(is_gift_family_method(k) for k in self.unhandled_counts)

    def gift_parse_errors(self) -> bool:
        return any(is_gift_family_method(k) for k in self.parse_error_counts)


def is_gift_family_method(method) -> bool:
    """method 名是否属于 Gift family。

    ⚠️ **只做名字分类, 不代表"收到了一次付费礼物"** —— Issue §F 明确
    要求第一阶段只做证据分类。真正计费 / 连击去重语义留到真实样本
    确认之后再开后续 Issue。
    """
    return "gift" in str(method or "").lower()
