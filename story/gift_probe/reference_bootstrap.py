#!/usr/bin/env python
# coding: utf-8
"""`reference-2026` 臂的 `cursor` / `internal_ext` —— 逐字段对齐公开参考实现。

## 这个模块存在的唯一理由

Issue #17 要求 Profile C 的 `cursor/internal_ext` **形状对齐当前
`chuanyue98/douyin-live-toolkit` 公开实现**。而本项目**生产**的生成器
(`vendor/douyin_fetcher/ws_bootstrap.py`) 是照**另一个**参考实现
(`JaneEyre3007/douyin-js` 的 `genCursorInternalExt`) 写的 —— 两者形状
**不同**:

    JaneEyre3007/douyin-js(= 本项目生产 local 路径)
        cursor       = t-{now}_r-{r}_d-1_u-1_h-{h}
        internal_ext = internal_src:dim|wss_push_room_id:{room}|wss_push_did:{uid}
                       |first_req_ms:{now}|fetch_time:{now}|seq:1
                       |wss_info:0-{now}-0-0|wrds_v:{wrds_v}

    chuanyue98/douyin-live-toolkit(= 本模块, Issue 指定的 reference)
        cursor       = d-1_u-1_fh-7392091211001140287_t-{now_ms}_r-1
        internal_ext = internal_src:dim|wss_push_room_id:{room}|wss_push_did:{uid}
                       |first_req_ms:{now_ms}|fetch_time:{now_ms}|seq:1
                       |wss_info:0-{now_ms}-0-0
                       (无 wrds_v;无 r/h)

所以 C 臂**必须**走本模块, 而不是复用生产生成器 —— 否则"C 没收到 Gift"
只能说明"另一个外部方案的形状也不行", 而我们压根没跑过那个形状。这正是
上一轮 review 打回的点。

## 对齐来源(逐字核对, 不是回忆)

    repo   : https://github.com/chuanyue98/douyin-live-toolkit
    commit : 4b4b7c1e09adf7e8a62b232f2768484836f070f7
    file   : src/douyin_live_toolkit/ws_client.py::_build_wss_url
    uid    : ws_client.py::_connect_once ->
             str(random.randint(7_000_000_000_000_000_000,
                                7_999_999_999_999_999_999))

对应原文(该文件 46~63 行):

    now_ms = int(time.time() * 1000)
    ...
    f"&cursor=d-1_u-1_fh-7392091211001140287_t-{now_ms}_r-1"
    f"&internal_ext=internal_src:dim|wss_push_room_id:{room_id}"
    f"|wss_push_did:{user_unique_id}"
    f"|first_req_ms:{now_ms}|fetch_time:{now_ms}|seq:1|wss_info:0-{now_ms}-0-0"

## 边界(必须写清, 否则这些常量会被当成"协议事实"流传下去)

⚠️ 下面这些是**该参考实现当前写死的取值**, 不是抖音官方协议定义:

    fh-7392091211001140287   —— 参考实现里的硬编码值, 含义未知
    d-1_u-1_r-1              —— 同上
    internal_src:dim         —— 同上

我们逐字复现它们, 是因为本轮要做的是"**复现对方的连接画像**, 看是否
能收到 Gift"。把它们解释成协议含义、或者按"看起来更合理"去改, 都会让
"C 到底跑没跑过那个形状"这个问题失去答案。

参考实现的**其余部分**(WS host / signature / 消息解析 / proto)与本项目
不同, 那些**不**在本轮复现范围内 —— Issue 明确说"其余 room / signature /
handler / proto 与本项目相同"。本轮只对齐 `cursor` / `internal_ext` 这两个
字段, 以及它们依赖的 `user_unique_id`。
"""

from __future__ import annotations

import random
from typing import Optional

#: 参考实现在 cursor 里写死的 `fh` 段。
#: ⚠️ 它是参考实现的硬编码值, 不是我们推导出来的 —— 见模块 docstring。
REFERENCE_FH = "7392091211001140287"

#: 参考实现的 cursor 模板(除 `{now_ms}` 外**逐字**照抄):
#:     d-1_u-1_fh-{REFERENCE_FH}_t-{now_ms}_r-1
REFERENCE_CURSOR_TEMPLATE = (
    "d-1_u-1_fh-" + REFERENCE_FH + "_t-{now_ms}_r-1"
)

#: 参考实现的 internal_ext 模板。注意它**不含** `wrds_v`, 且
#: `wss_push_did` 就是 `user_unique_id` 本身(**不拼 now_ms**)。
REFERENCE_INTERNAL_EXT_TEMPLATE = (
    "internal_src:dim"
    "|wss_push_room_id:{room_id}"
    "|wss_push_did:{user_unique_id}"
    "|first_req_ms:{now_ms}"
    "|fetch_time:{now_ms}"
    "|seq:1"
    "|wss_info:0-{now_ms}-0-0"
)

#: 参考实现生成随机 uid 的区间(闭区间, `randint`)。
REFERENCE_UID_MIN = 7_000_000_000_000_000_000
REFERENCE_UID_MAX = 7_999_999_999_999_999_999


def reference_user_unique_id(rng: Optional[random.Random] = None) -> str:
    """参考实现在**每次连接**时生成的 `user_unique_id`。

    ⚠️ 区间上界与生产用的 `7e18~8e18` **不同**: 参考实现是
    `randint(7e18, 7.999...e18)`, 即上界是 `7999...` 而不是 `8000...`。
    这个差别小到看不出来, 但既然要求"逐字段对齐", 就按参考实现来。

    纯函数, 随机源可注入 —— 测试要能确定性地验证格式与范围。
    """
    r = rng if rng is not None else random.Random()
    return str(r.randint(REFERENCE_UID_MIN, REFERENCE_UID_MAX))


def build_reference_bootstrap(room_id, user_unique_id, now_ms: int) -> dict:
    """按参考实现构造 `cursor` / `internal_ext`。

    Args:
        room_id: 真实房间号(与生产一样是 `liveMan.room_id`)。
        user_unique_id: 本连接的 uid。**同时**作为 `wss_push_did`。
        now_ms: 本次连接的毫秒时间戳。**必须注入** —— 不在函数内部读时钟,
            否则测试无法确定性验证"用的是 fresh now_ms"。

    Returns:
        `{"cursor": str, "internal_ext": str}`。

    不抛异常: 任何输入都给出确定可用的字符串(与生产生成器的容错一致)。

    ⚠️ 与生产生成器的**结构差异**(不是笔误, 是刻意的):
        - cursor 用 `d-1_u-1_fh-..._t-..._r-1` 而不是 `t-..._r-..._d-1_u-1_h-...`;
        - internal_ext **没有** `wrds_v` 段;
        - `wss_push_did` 就是 uid 本身(**不拼 now_ms**)。
    """
    try:
        now_ms = int(now_ms)
    except (TypeError, ValueError):
        now_ms = 0
    cursor = REFERENCE_CURSOR_TEMPLATE.format(now_ms=now_ms)
    internal_ext = REFERENCE_INTERNAL_EXT_TEMPLATE.format(
        room_id=room_id if room_id is not None else "",
        user_unique_id=user_unique_id if user_unique_id is not None else "",
        now_ms=now_ms,
    )
    return {"cursor": cursor, "internal_ext": internal_ext}
