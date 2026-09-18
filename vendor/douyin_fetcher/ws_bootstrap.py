#!/usr/bin/env python
# coding: utf-8
"""WS bootstrap 状态生成(Step 12B)。

## 这个模块解决什么

抖音 WS 连接要在 URL 里带 `cursor` 与 `internal_ext`。上游 `liveMan.py`
的做法是**写死一组 2024-07 的值**(`t-1721106114633`), 我们实测那组值能连上、
能收到 chat/member/like/social, 但**收不到 Gift**。

## 证据来源(重要: 这是外部实现, 不是官方协议定义)

外部当前实现 `JaneEyre3007/douyin-js` 的 `genCursorInternalExt()` **不经过
任何 HTTP bootstrap**, 直接取 `Date.now()` 在**本地**构造这两个串:

    sec    = floor(now_ms / 1000)
    base   = sec << 32
    r      = base + random16
    h      = base + random32
    wrds_v = base + random16

    cursor       = t-{now_ms}_r-{r}_d-1_u-1_h-{h}
    internal_ext = internal_src:dim
                   |wss_push_room_id:{room_id}
                   |wss_push_did:{user_unique_id}
                   |first_req_ms:{now_ms}
                   |fetch_time:{now_ms}
                   |seq:1
                   |wss_info:0-{now_ms}-0-0
                   |wrds_v:{wrds_v}

它的 README 明确说"本地还原 signature / cursor / internal_ext, 直接连接
WebSocket"。

## 必须写清楚的边界

⚠️ **这是"外部当前实现采用的做法", 不是"抖音官方协议定义"。**
我们采用它是为了做一个**单变量实验**: 用它替换掉那组 2024 写死值, 其余
(room / WS host / signature / handler / proto / 礼物操作)**全部不动**,
看 Gift 是否出现。

- 若出现 -> 强证据: 旧 stale bootstrap 影响了收到的消息集合;
- 若仍为 0 -> bootstrap 假设降级, 转去查 WS host / signature / 订阅条件。

## 为什么单独一个模块

它是一个**纯函数**: 不碰网络、不碰全局时钟、不碰全局随机 —— `now_ms` 与
`rng` 都由调用方注入。这样测试可以完全确定性地验证"同样的输入产生同样的
输出", 而不必依赖真实时间或随机数(那会让测试变 flaky, 且无法复现线上)。

零新依赖。
"""

from __future__ import annotations

import random
from typing import Optional

#: `internal_src` 的取值。外部实现用的是 `dim`。
_INTERNAL_SRC = "dim"

#: 随机低位的位宽。外部实现: r 用 16 位、h 用 32 位、wrds_v 用 16 位。
_R_BITS = 16
_H_BITS = 32
_WRDS_BITS = 16


def _rand_bits(rng: random.Random, bits: int) -> int:
    """取 `bits` 位随机整数(低位)。"""
    return rng.getrandbits(bits)


def generate_ws_bootstrap(room_id, user_unique_id, now_ms: int,
                          rng: Optional[random.Random] = None) -> dict:
    """本地生成 WS 连接要用的 `cursor` 与 `internal_ext`。

    Args:
        room_id: 真实房间号(`liveMan.room_id`, 不是 web_rid)。
        user_unique_id: 客户端身份 id。
        now_ms: 当前毫秒时间戳。**必须注入** —— 不要在函数内部读时钟,
            否则测试无法确定性验证。
        rng: 随机源。**必须可注入** —— 理由同上。

    Returns:
        `{"cursor": str, "internal_ext": str}`。

    不抛异常: 任何输入都给出一组确定可用的字符串(空输入退化为空串)。
    """
    rng = rng if rng is not None else random.Random()
    try:
        now_ms = int(now_ms)
    except (TypeError, ValueError):
        now_ms = 0

    sec = now_ms // 1000
    base = sec << 32

    r = base + _rand_bits(rng, _R_BITS)
    h = base + _rand_bits(rng, _H_BITS)
    wrds_v = base + _rand_bits(rng, _WRDS_BITS)

    cursor = f"t-{now_ms}_r-{r}_d-1_u-1_h-{h}"
    internal_ext = (
        f"internal_src:{_INTERNAL_SRC}"
        f"|wss_push_room_id:{room_id}"
        f"|wss_push_did:{user_unique_id}"
        f"|first_req_ms:{now_ms}"
        f"|fetch_time:{now_ms}"
        f"|seq:1"
        f"|wss_info:0-{now_ms}-0-0"
        f"|wrds_v:{wrds_v}"
    )
    return {"cursor": cursor, "internal_ext": internal_ext}
