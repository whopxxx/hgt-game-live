#!/usr/bin/env python
# coding: utf-8
"""直播进程与离线补池守护进程之间的极小心跳协议。

守护进程只在直播不活跃时直接写 generated pool。这样有两个好处:

1. 直播运行时没有第二个进程与 Director 同时写 pool.jsonl；
2. 守护进程不会和直播 QA / 出题抢同一个 LLM 网关。

Director 每 2 秒原子刷新一次 JSON 心跳。进程崩溃时文件可能残留，但
守护进程只认最近若干秒内的时间戳，因此会自动恢复补池。
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

DEFAULT_PATH = os.path.join("data", "live_heartbeat.json")
DEFAULT_STALE_SECONDS = 10.0
DEFAULT_INTERVAL_SECONDS = 2.0


def write_live_heartbeat(path: str = DEFAULT_PATH, *, phase: str = "",
                         session_id: str = "") -> bool:
    """原子刷新直播心跳。失败返回 False，不影响直播主流程。"""
    if not path:
        return False
    rec = {
        "pid": os.getpid(),
        "ts": time.time(),
        "phase": str(phase or ""),
        "session_id": str(session_id or ""),
    }
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
        os.replace(tmp, path)
        return True
    except (OSError, TypeError, ValueError):
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False


def read_live_heartbeat(path: str = DEFAULT_PATH) -> dict[str, Any]:
    """读取心跳；任何损坏/缺失都按空记录处理。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            rec = json.load(f)
        return rec if isinstance(rec, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def live_is_active(path: str = DEFAULT_PATH,
                   stale_seconds: float = DEFAULT_STALE_SECONDS,
                   now: float | None = None) -> bool:
    """最近 stale_seconds 内有心跳则认为直播正在运行。"""
    rec = read_live_heartbeat(path)
    try:
        ts = float(rec.get("ts"))
    except (TypeError, ValueError):
        return False
    current = time.time() if now is None else float(now)
    age = current - ts
    # 系统时钟小幅回拨时宁可多暂停一会儿，避免两个进程同时补池。
    return age <= max(0.0, float(stale_seconds))


def clear_live_heartbeat(path: str = DEFAULT_PATH) -> bool:
    """仅删除属于当前 pid 的心跳，避免旧进程误删新进程的 lease。"""
    rec = read_live_heartbeat(path)
    try:
        owner = int(rec.get("pid"))
    except (TypeError, ValueError):
        return False
    if owner != os.getpid():
        return False
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False
