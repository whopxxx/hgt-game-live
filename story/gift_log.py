#!/usr/bin/env python
# coding: utf-8
"""结构化礼物事件落盘(gifts.jsonl)—— Issue #42 §5。

## 职责边界

- 一条**成功解析**的 `WebcastGiftMessage` = 一行 JSON。**不**合并
  连击生命周期(repeat_end=0/1 都记)、**不**因 group 相同去重 ——
  真实现场已确认"2 个实际礼物 -> 4 条 WebcastGiftMessage", 本文件
  记录的是 raw semantic 协议事件, 不是"真实礼物单位"账本。
- 字段是**协议原样映射**(见 `gift_record`), 不做任何价值/兑换换算;
  `diamond_count` 只是礼物标价, 与 AI 玩家额度/SummonLedger 无关。
- 绝不记录 Cookie / HTTP header / raw protobuf payload。

## 写入责任

Engine 保持纯状态机, 不做磁盘 I/O。本文件由 **Director(orchestration)
层**在消费事件时调用; 文件位于本场 run directory(`<run_dir>/gifts.jsonl`,
见 director 的 run directory 逻辑)。

## fail-soft

打开/写盘失败只 warning(同类错误只报第一次, 之后只计数, 不刷屏),
**永不抛** —— 礼物日志绝没有能力把直播搞挂。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime

log = logging.getLogger("story.gift_log")


def gift_record(ev, ts: str = "") -> dict:
    """Gift InteractionEvent -> gifts.jsonl 的一行(纯映射, 无业务换算)。

    字段表是 Issue #42 §5 的闭集。`ts` 用本机墙钟(ISO, 毫秒) —— 协议里
    没有可信的单调时钟字段, 落盘时刻就是排障时最想要的时刻。
    """
    return {
        "ts": ts or datetime.now().isoformat(timespec="milliseconds"),
        "user_id": str(getattr(ev, "user_id", "") or ""),
        "user_name": str(getattr(ev, "user_name", "") or ""),
        "gift_id": str(getattr(ev, "gift_id", "") or ""),
        "gift_name": str(getattr(ev, "gift_name", "") or ""),
        "gift_combo": bool(getattr(ev, "gift_combo", False)),
        "gift_type": int(getattr(ev, "gift_type", 0) or 0),
        "diamond_count": int(getattr(ev, "diamond_count", 0) or 0),
        "combo_count": int(getattr(ev, "combo_count", 0) or 0),
        "repeat_count": int(getattr(ev, "repeat_count", 0) or 0),
        "total_count": int(getattr(ev, "total_count", 0) or 0),
        "repeat_end": int(getattr(ev, "repeat_end", 0) or 0),
        "group_id": str(getattr(ev, "group_id", "") or ""),
        "group_count": int(getattr(ev, "group_count", 0) or 0),
        "send_type": int(getattr(ev, "send_type", 0) or 0),
        "trace_id": str(getattr(ev, "trace_id", "") or ""),
        "log_id": str(getattr(ev, "log_id", "") or ""),
        "message_id": str(getattr(ev, "message_id", "") or ""),
        "envelope_msg_id": str(getattr(ev, "envelope_msg_id", "") or ""),
    }


class GiftJsonlWriter:
    """append-only UTF-8 JSONL writer。

    线程安全: 消费线程(Director._consume)是单线程, 锁只是防御性的 ——
    未来若有多处写入点, 这里已经不会踩坏彼此。
    """

    def __init__(self, path: str):
        self.path = str(path)
        self._lock = threading.Lock()
        self._fp = None
        #: 已成功写入的行数(可观测, 不进日志正文)。
        self.written = 0
        self.write_errors = 0
        self._warned = False

    # ------------------------------------------------------------------
    def _warn_once(self, msg: str) -> None:
        """第一次 warning, 之后只计数 —— 不得刷屏, 更不得抛。"""
        self.write_errors += 1
        if self._warned:
            return
        self._warned = True
        try:
            log.warning("%s (同类错误本次运行只报这一次)", msg)
        except Exception:                       # noqa: BLE001
            pass

    def precreate(self) -> None:
        """空文件先建出来 —— 直播第一秒 tail 就有目标(哪怕一条礼物都还没有)。

        失败只 warning: 落盘失败本来就该降级, 更不能挡住直播启动。
        """
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)),
                        exist_ok=True)
            with open(self.path, "a", encoding="utf-8"):
                pass
        except OSError as e:
            self._warn_once(f"gifts.jsonl 预创建失败: {e}")

    def write(self, ev) -> None:
        """落一条礼物事件。**永不抛** —— 见模块 docstring 的 fail-soft。"""
        try:
            rec = gift_record(ev)
        except Exception as e:                  # noqa: BLE001
            self._warn_once(f"礼物事件映射失败: {e}")
            return
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        with self._lock:
            try:
                if self._fp is None:
                    os.makedirs(os.path.dirname(os.path.abspath(self.path)),
                                exist_ok=True)
                    self._fp = open(self.path, "a", encoding="utf-8")
                self._fp.write(line)
                self._fp.flush()
                self.written += 1
            except OSError as e:
                self._warn_once(f"gifts.jsonl 写盘失败: {e}")
                # 关掉坏句柄, 下一条礼物重新尝试打开 —— 磁盘满/网络盘
                # 抖一下不该让本 session 从此再也写不进任何礼物。
                try:
                    if self._fp:
                        self._fp.close()
                except Exception:               # noqa: BLE001
                    pass
                self._fp = None

    def close(self) -> None:
        with self._lock:
            try:
                if self._fp:
                    self._fp.close()
            except Exception:                   # noqa: BLE001
                pass
            self._fp = None
