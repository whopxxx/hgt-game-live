#!/usr/bin/env python
# coding: utf-8
"""诊断探针 —— 挂在 fetcher 上的三个只读旁路(Step 12C)。

## 三个探针, 各管一件事

    TransportSummaryProbe   实时(每 20~30 秒)结构化 method / transport 摘要
    CaptureProbe            按规则把原始 payload 落盘 + 计数
    GiftSemanticProbe       把 primary GiftMessage 解析出的语义字段落 JSONL

它们都是**观察者**: 不改一行业务流程, 不改 handler 的返回值, 不拦截
消息。诊断连接失败也绝不影响主直播(见 `runner.py`)。

## ⚠️ 这个模块最要紧的一条纪律: 凭据与用户内容不进日志

登录 Cookie 是凭据。它绝不能出现在日志 / 异常 / repr / snapshot /
fixture / PR 里。所以:

- 摘要日志**只**由白名单字段拼出来 —— `_SUMMARY_LOG_FIELDS` 是一份显式
  清单, 不是"把整个 dict 打出来"。任何将来加进 summary 的新字段都**不会**
  自动流进日志, 必须显式加进白名单;
- summary 里连**服务器下发的 method 名**都不直接打 —— 它们被归一化后
  只保留 `[A-Za-z0-9_.-]`, 其余字符换成 `?`(见 `sanitize_method`)。这不是
  洁癖: method 名来自网络, 一个被构造过的 method 串足以在日志里注入
  换行、伪装成别的日志行, 或者把一段偶发落到日志里的凭据带出去。
  归一化之后, 日志行的形状是**我们**决定的;
- 诊断 JSONL 里允许出现 `user_id` / `user_name`(与现有 danmaku `--all`
  一致, 且是本地诊断文件), 但它所在目录被 gitignore 覆盖, 且**普通
  INFO summary 绝不打印用户信息**。

## 与业务链的关系

探针挂在 `CallbackFetcher` 的钩子点上, 但**不改变** `_on_gift` /
`_on_like` 的既有语义: 业务事件照常发, 探针只是额外记一笔。若探针自己
抛异常, 一律吞掉并计数 —— 诊断能力不该成为直播的可用性风险。
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Optional

from .profile import (
    PROFILE_OK,
    ProfileCounters,
    is_gift_family_method,
)
from ws_cookie import WS_AUTH_ANONYMOUS, WS_AUTH_AUTHENTICATED

#: 摘要日志允许出现的字段。**显式白名单** —— 见模块 docstring。
_SUMMARY_LOG_FIELDS = (
    "profile", "connection_generation", "auth", "config_state",
    "ws_frames", "ws_messages", "gift_method_seen", "parsed_gift_count",
    "emitted_gift_count", "captured_payload_count", "capture_skipped_count",
)

#: 摘要日志里 method 名最多打几个(按次数降序)。超出部分只报个数 ——
#: 一个正常直播间有几十种 method, 全打出来会把日志行撑爆, 反而看不见
#: 真正关心的 Gift。
_SUMMARY_TOP_METHODS = 8

#: method 名的合法字符集。其余一律替换(理由见模块 docstring)。
_METHOD_SAFE_RE = re.compile(r"[^A-Za-z0-9_.\-]")

#: 诊断语义 JSONL 的字段表(Issue §G 的闭集)。
_GIFT_SEMANTIC_FIELDS = (
    "gift_id", "gift_name", "combo_count", "repeat_count", "total_count",
    "repeat_end", "group_count", "group_id", "log_id", "trace_id",
    "envelope_msg_id", "common_msg_id", "connection_generation", "profile",
    "user_id", "user_name", "ts",
)


def sanitize_method(method) -> str:
    """把服务端下发的 method 名归一化成日志安全的形状。

    ⚠️ 这条函数**不是**为了好看。method 名是网络输入, 未归一化时它可以
    含有换行、`[`、`=` 等字符 —— 于是攻击者(或一次异常推送)能在我们的
    日志里伪造出一整行假的日志, 或者让"日志里只有白名单字段"这个保证
    失效。归一到固定字符集之后, 日志行的形状由我们自己决定。

    **不能**顺手截断成前 N 个字符: `WebcastGiftMessage` 与
    `WebcastGiftSortMessage` 这类只差中段的 method 会被截成同一个名字,
    而区分它们正是本轮要做的事。
    """
    s = str(method or "")
    safe = _METHOD_SAFE_RE.sub("?", s)
    # 长度上限只为防"一个 1MB 长的 method 名把日志行撑爆"; 200 远超任何
    # 真实 method 名, 不会碰上上面那个截断问题。
    return safe[:200]


def _ordered_top(counts: dict, n: int) -> tuple:
    """按次数降序取前 n 项; 次数相同则按名字排序(结果确定, 便于比对)。"""
    items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return tuple(items[:n]), max(0, len(items) - n)


def sanitize_auth(value) -> str:
    """把任意值收敛成**两个固定词之一**。

    ⚠️ 这是本模块防凭据泄漏的**最后一道闸**, 不是格式化工具。

    摘要里的 `auth` 字段来自 counter, 而 counter 的 `auth` 是在 runner 里
    从 `profile_auth_state()` 赋的 —— 看起来已经足够安全。但"某个字段后来
    被赋成了别的东西"正是这类泄漏在现实里的出现方式: 一次重构、一次
    "顺手把调试信息也塞进去", 凭据就进了每一行日志, 而且**看起来完全
    正常**(日志里本来就该有 auth=... 字样)。

    所以这里不信任上游: 只有**逐字**等于 `authenticated` / `anonymous`
    的值才被放行, 其余一律退化成 `unknown`。凭据的一个子串都不可能
    通过 —— 因为它不可能**恰好等于**那两个词。
    """
    s = str(value or "").strip()
    if s in (WS_AUTH_AUTHENTICATED, WS_AUTH_ANONYMOUS):
        return s
    # 也接受 `authenticated ...` 这种带后缀的残留(见上面的 mutation 形状):
    # 只有当它**以**固定词开头时才取那个词, 后面的一切都丢掉。
    for word in (WS_AUTH_AUTHENTICATED, WS_AUTH_ANONYMOUS):
        if s.startswith(word):
            return word
    return "unknown"


class TransportSummaryProbe:
    """每路 profile 的**实时** method / transport 摘要。

    ## 为什么要实时

    原先 summary 只在**断开时**输出。真实直播里"确认有人送礼了但没收到"
    这个现象需要当场判断, 而下播才看到 summary 等于要等一整场。诊断模式
    下每 20~30 秒打一次, 送完礼几十秒内就能知道该往哪一层查。

    ## 输出形态

    一条 INFO 日志行 + 一个结构化 dict。dict 会被 runner 收集起来, 供
    PR 描述与事后分析使用。**日志行只含白名单字段**(见模块 docstring)。
    """

    def __init__(self, profile_id: str, counters: ProfileCounters, *,
                 interval_seconds: float = 25.0, logger=None):
        self.profile_id = str(profile_id)
        self.counters = counters
        try:
            self.interval_seconds = max(1.0, float(interval_seconds))
        except (TypeError, ValueError):
            self.interval_seconds = 25.0
        self._log = logger
        self._last_at = 0.0
        self.emitted_lines = 0

    # ------------------------------------------------------------------
    def now_text(self, extra: str = "") -> str:
        """拼出这条摘要日志。**只**使用白名单字段 + 归一化后的 method 名。

        单行、无换行 —— 归一化保证了 method 名里不可能有换行, 而白名单里
        的其余字段都是我们自己产生的数字/固定词。

        ⚠️ `auth` 走 `sanitize_auth()` **而不是**直接取 `s.get("auth")`:
        见那条函数的说明 —— 它是防凭据泄漏的最后一道闸, 而"auth 字段被
        赋成别的东西"是这类泄漏在现实里的出现方式。
        """
        s = self.counters.summary()
        top, more = _ordered_top(s.get("methods") or {}, _SUMMARY_TOP_METHODS)
        gift = s.get("gift_family_methods") or {}
        unhandled = _ordered_top(s.get("unhandled") or {}, 5)[0]
        perrs = _ordered_top(s.get("parse_errors") or {}, 5)[0]
        # 逐字段显式取白名单值。`auth` 单独走收敛函数 —— 见 docstring。
        head_parts = []
        for k in _SUMMARY_LOG_FIELDS:
            if k == "auth":
                head_parts.append(f"auth={sanitize_auth(s.get('auth'))}")
            else:
                head_parts.append(f"{k}={s.get(k)}")
        head = " ".join(head_parts)
        body = (f" methods={[ (sanitize_method(m), c) for m, c in top ]}"
                f" methods_more={more}"
                f" gift_family={[ (sanitize_method(m), c) for m, c in sorted(gift.items()) ]}"
                f" unhandled={[ (sanitize_method(m), c) for m, c in unhandled ]}"
                f" parse_errors={[ (sanitize_method(m), c) for m, c in perrs ]}"
                f" verdict={self.counters.verdict()}")
        if extra:
            body += f" {sanitize_method(extra)}"
        return f"【gift probe】{head}{body}"

    # ------------------------------------------------------------------
    def maybe_emit(self, *, force: bool = False, now: Optional[float] = None,
                   extra: str = "") -> Optional[str]:
        """到点了就输出一次; 返回打出的那一行(未输出返回 None)。

        用**单调时钟**(`time.monotonic`)判间隔, 不用墙钟: 直播机器上
        NTP 校时会让墙钟跳一下, 于是"每 25 秒一次"要么连打十次、要么
        几十分钟不打 —— 而这两种情况在排查时都会误导人。
        """
        t = time.monotonic() if now is None else float(now)
        if not force and self._last_at and \
                (t - self._last_at) < self.interval_seconds:
            return None
        self._last_at = t
        self.emitted_lines += 1
        line = self.now_text(extra=extra)
        if self._log is not None:
            try:
                self._log.info("%s", line)
            except Exception:                   # noqa: BLE001
                pass
        return line

    def summary(self) -> dict:
        """结构化摘要(与日志同源, 但字段更全, 供落盘/人工分析)。"""
        s = self.counters.summary()
        s["verdict"] = self.counters.verdict()
        return s


class CaptureProbe:
    """把命中规则的原始 payload 落盘, 并把三层计数维护起来。

    ## 三层计数(可独立区分 —— Issue 要求 11)

        gift_method_seen    服务端下发了 Gift-family method
        parsed_as_primary   其中 primary `WebcastGiftMessage` 解析成功
        emitted             解析成功且交给了业务回调

    三层分开是这一轮的核心诊断能力: 三层全 0 说明服务端没给; 第一层有、
    第二层 0 说明 parse 出问题; 第二层有、第三层 0 说明 callback/ingest
    链断了。合并计数会让这三种完全不同的故障看起来一模一样。

    ## 为什么 payload 的落盘决策**必须**在 parse 之前

    parse 失败时 payload 恰恰是最该留下的证据。若先 parse 再决定落盘, 那么
    "解析失败 -> 抛异常 -> 根本没走到落盘" —— 最需要的样本反而永远采不到。
    """

    def __init__(self, profile_id: str, counters: ProfileCounters, store, *,
                 logger=None):
        self.profile_id = str(profile_id)
        self.counters = counters
        self.store = store
        self._log = logger

    def observe_payload(self, payload, *, method: str,
                        connection_generation: int = 0,
                        envelope_msg_id=0) -> Optional[str]:
        """收到一条消息的原始 payload —— 按规则落盘。

        ⚠️ 这里**不改** payload, **不改**控制流, **不解析**。它只回答
        "这条要不要留下"。
        """
        try:
            fname = self.store.capture(
                payload, method=method,
                connection_generation=connection_generation,
                envelope_msg_id=envelope_msg_id)
        except Exception:                       # noqa: BLE001
            return None
        if fname:
            self.counters.captured_payload_count += 1
        return fname

    def note_parse_error(self, payload, *, method: str,
                         connection_generation: int = 0,
                         envelope_msg_id=0) -> Optional[str]:
        """解析失败时额外留一份 payload。

        Issue §E: "发生 parse_error 的 payload 也保存"。即使这个 method
        不属于 Gift family 也保存 —— 一次意外的 parse 失败本身就是值得
        离线看的证据。
        """
        from .capture import PARSE_STATUS_ERROR
        try:
            fname = self.store.capture(
                payload, method=method,
                connection_generation=connection_generation,
                envelope_msg_id=envelope_msg_id,
                parse_error=True, parse_status=PARSE_STATUS_ERROR)
        except Exception:                       # noqa: BLE001
            return None
        if fname:
            self.counters.captured_payload_count += 1
        return fname


class GiftSemanticProbe:
    """primary `WebcastGiftMessage` 解析成功时的**语义**落盘(Issue §G)。

    为什么单独一个 JSONL 而不是塞进弹幕文件: 弹幕文件是 production 数据
    (`data/danmaku.jsonl`), 而这里是要交给离线分析的诊断证据。写进同一个
    文件既污染 production 语义, 也让"本轮不改 production 数据"这条验收
    标准无法机械验证。

    字段表是 Issue §G 的闭集, 一个不多 —— 尤其**不含** Cookie 或任何
    payload 原文。
    """

    FILENAME = "gift_semantic.jsonl"

    def __init__(self, dir_path: str, profile_id: str, counters, *,
                 now_fn=None, logger=None):
        self.dir = str(dir_path)
        self.path = os.path.join(self.dir, self.FILENAME)
        self.profile_id = str(profile_id)
        self.counters = counters
        self._now = now_fn or (lambda: time.time())
        self._log = logger
        self.write_errors = 0

    def record(self, m, *, envelope_msg_id=0, connection_generation: int = 0
               ) -> Optional[dict]:
        """从解析好的 `GiftMessage` 里抽字段并追加一行。

        取字段一律走 `getattr(..., default)` —— protobuf 版本变化 / 字段
        缺失时不该让诊断本身抛异常。缺字段就是空串/0, 而不是让这一条丢失。
        """
        try:
            common = getattr(m, "common", None)
            user = getattr(m, "user", None)
            gift = getattr(m, "gift", None)
            rec = {
                "ts": self._now(),
                "profile": self.profile_id,
                "connection_generation": int(connection_generation or 0),
                "gift_id": str(getattr(m, "gift_id", "") or ""),
                "gift_name": str(getattr(gift, "name", "") or ""),
                "combo_count": int(getattr(m, "combo_count", 0) or 0),
                "repeat_count": int(getattr(m, "repeat_count", 0) or 0),
                "total_count": int(getattr(m, "total_count", 0) or 0),
                "repeat_end": int(getattr(m, "repeat_end", 0) or 0),
                "group_count": int(getattr(m, "group_count", 0) or 0),
                "group_id": str(getattr(m, "group_id", "") or ""),
                "log_id": str(getattr(m, "log_id", "") or ""),
                "trace_id": str(getattr(m, "trace_id", "") or ""),
                "envelope_msg_id": (str(envelope_msg_id)
                                    if envelope_msg_id else ""),
                "common_msg_id": str(getattr(common, "msg_id", 0) or ""),
                # 用户信息**只**进本地诊断 JSONL(与现有 danmaku --all 一致),
                # 绝不进 INFO 日志。
                "user_id": str(getattr(user, "id", "") or ""),
                "user_name": str(getattr(user, "nick_name", "") or ""),
            }
            os.makedirs(self.dir, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
            return rec
        except Exception:                       # noqa: BLE001
            # 诊断落盘失败不该影响业务链, 也不该让这三层计数失真。
            self.write_errors += 1
            return None

    def fields(self) -> tuple:
        """字段表(只读) —— 测试用它钉住"不多不少"。"""
        return _GIFT_SEMANTIC_FIELDS


def redact_for_log(value) -> str:
    """把任意值变成**安全**的一行文本。

    唯一用途: 当某个诊断值需要进日志时, 强制走这一层。它只保留固定字符
    集并截断 —— 与 `sanitize_method` 同一思路。存在的意义是给"将来有人
    想把 payload 片段或 cookie 打出来看看"留一条**不会泄漏**的路。
    """
    s = re.sub(r"[^A-Za-z0-9_.\-:/=, ]", "?", str(value or ""))
    return s[:200]


def is_ok_state(counters: ProfileCounters) -> bool:
    """这一路是否处于健康观测态(纯便捷函数, 供 runner 汇总)。"""
    return counters.config_state == PROFILE_OK
