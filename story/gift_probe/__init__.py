#!/usr/bin/env python
# coding: utf-8
"""Gift 采集诊断 —— 受控的连接画像对照(Step 12C)。

## 这个包解决什么

遗留问题: **确认直播间里有人送礼, 程序却长期收不到 Gift 事件。**
当前主链已经有 method 计数探针, 但它答不出"**哪种连接画像**能收到" ——
因为我们只有一条连接、一套固定的连接参数, 没有对照组。

本包新增一个**显式诊断模式** (`gift_capture_diagnostic`), 同时开 2~3 路
参数受控的连接, 把每一路的原始证据分开落盘, 让一次真实直播之后日志能
直接回答:

    1. 服务端有没有下发任何 Gift-family method;
    2. 哪种 WS 连接画像能收到;
    3. 收到后是否是 handler 漏接;
    4. handler 接到了是否 protobuf parse 失败;
    5. parse 成功后是否 callback / ingest 链丢失;
    6. 连击 / 重连下哪些 ID 与计数字段稳定。

## 边界(与 Issue 的 non-goals 一致)

- **不接前端公告**、不感谢礼物、不做打赏榜、不做钻石换算;
- **不改变** SummonLedger / AI 玩家 / 正常问答的业务语义;
- **不把** Gift-family method 一律当成"付费礼物已确认" —— 第一阶段只做
  证据分类: `method_seen` / `payload_captured` /
  `parsed_as_primary_gift` / `business_emitted`;
- 诊断连接的失败**不拖垮主直播**(见 `runner.py` 的故障隔离)。

## 安全边界

登录 Cookie 是**凭据**。本包任何模块都不得把它写进日志 / 异常 / repr /
probe index / fixture。唯一允许出现在日志里的形态是
`auth=authenticated` / `auth=anonymous` 两个词之一 —— 没有长度、没有
hash、没有前缀、没有 cookie name 列表。见 `probe.py` 与 `profile.py`。

## vendor 路径

`ws_cookie` / `ws_bootstrap` / `protobuf.douyin` 都在
`vendor/douyin_fetcher/` 下, 与 `danmaku.py` 依赖的是**同一份**实现。
这里把它插进 `sys.path` —— 与 `danmaku.py` 的做法一致(不复制一份出来:
凭据合并与 bootstrap 生成各只有一份真相, 复制会漂移)。
"""

import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))
_VENDOR = _os.path.join(
    _os.path.dirname(_os.path.dirname(_HERE)), "vendor", "douyin_fetcher")

if _os.path.isdir(_VENDOR) and _VENDOR not in _sys.path:
    _sys.path.insert(0, _VENDOR)
