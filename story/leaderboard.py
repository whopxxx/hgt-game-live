#!/usr/bin/env python
# coding: utf-8
"""跨直播累计猜汤榜 —— append-only 持久账本。

当前 Engine 内部排行榜按 user_id 聚合，但旧实现只存在内存里：
进程一重启，榜单就清零。本模块把“真人最终解出一题”记成 append-only
JSONL 事件，启动时重放事件恢复累计分数。

设计原则：
- append-only：不整文件重写，减少崩溃/断电把整榜清空的风险；
- fsync：一次胜场写入成功后尽量真正落盘；
- event_id 幂等：Director 用 session_id + round 生成稳定事件 id，
  同一揭晓 action 即使异常重派也不会重复加分；
- fail-open：排行榜不是直播可用性的前提。坏行会报警并跳过，不能因为
  排行榜文件损坏就让整场直播起不来；
- 隐私：只记录现有排行榜已经依赖的 user_id / 最新 user_name，不记录
  弹幕正文、Cookie、礼物 payload 等额外数据。data/*.jsonl 已 gitignore。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

log = logging.getLogger("story.leaderboard")

DEFAULT_PATH = os.path.join("data", "leaderboard.jsonl")
FORMAT_VERSION = 1


def _append_line(path: str, rec: dict) -> bool:
    """追加一行 JSON + flush + fsync。失败只报警，不抛到直播主循环。"""
    try:
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        return True
    except Exception:                       # noqa: BLE001
        log.exception("累计猜汤榜写入失败: %s", path)
        return False


class LeaderboardLedger:
    """真人解题胜场的跨重启累计账本。"""

    def __init__(self, path: str = "", enabled: bool = False):
        # 与 PlayedLedger 一样默认关闭：直接构造 Engine 的离线测试不该碰
        # 仓库真实 data/。生产装配由 Director 显式 enabled=True。
        self.path = str(path or DEFAULT_PATH)
        self.enabled = bool(enabled)
        self._lock = threading.Lock()
        self._rows: dict[str, dict] = {}
        self._event_ids: set[str] = set()
        self.win_sequence = 0
        self.loaded_events = 0
        self.bad_lines = 0

    # ------------------------------------------------------------------
    def _apply_win(self, user_id: str, user_name: str,
                   event_id: str = "") -> None:
        self.win_sequence += 1
        prev = self._rows.get(user_id, {})
        self._rows[user_id] = {
            "user_id": user_id,
            # 昵称可能修改；与旧 session-only 榜一致，显示最近一次获胜昵称。
            "user_name": user_name,
            "solved_count": int(prev.get("solved_count", 0)) + 1,
            # 同分时“先达到这个分数的人”优先：每次加分都更新 sequence。
            "win_sequence": self.win_sequence,
        }
        if event_id:
            self._event_ids.add(event_id)

    # ------------------------------------------------------------------
    def load(self) -> int:
        """重放历史胜场。坏行跳过并报警，直播仍可继续。"""
        with self._lock:
            self._rows = {}
            self._event_ids = set()
            self.win_sequence = 0
            self.loaded_events = 0
            self.bad_lines = 0
            if not self.enabled:
                return 0
            if not os.path.exists(self.path):
                log.info("累计猜汤榜首次启动: %s", self.path)
                return 0
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    for ln, raw in enumerate(f, 1):
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            rec = json.loads(raw)
                        except Exception:       # noqa: BLE001
                            self.bad_lines += 1
                            log.error("累计猜汤榜第 %d 行 JSON 损坏，已跳过", ln)
                            continue
                        if not isinstance(rec, dict) or rec.get("type") != "win":
                            self.bad_lines += 1
                            log.error("累计猜汤榜第 %d 行不是 win 事件，已跳过", ln)
                            continue
                        user_id = str(rec.get("user_id", "") or "").strip()
                        user_name = str(rec.get("user_name", "") or "").strip()
                        event_id = str(rec.get("event_id", "") or "").strip()
                        if not user_id or not user_name:
                            self.bad_lines += 1
                            log.error("累计猜汤榜第 %d 行缺 user_id/user_name，已跳过",
                                      ln)
                            continue
                        if event_id and event_id in self._event_ids:
                            # 幂等保护：历史里若已出现同一个 session+round，
                            # 后一条不能让分数翻倍。
                            log.warning("累计猜汤榜第 %d 行重复 event_id=%s，已跳过",
                                        ln, event_id)
                            continue
                        self._apply_win(user_id, user_name, event_id)
                        self.loaded_events += 1
            except Exception:                   # noqa: BLE001
                # 排行榜是增强功能，不因磁盘故障阻断直播。已读到的前缀仍可用。
                self.bad_lines += 1
                log.exception("累计猜汤榜读取异常，使用已恢复的历史前缀: %s",
                              self.path)

            log.info("累计猜汤榜载入: %d 胜场 / %d 人 / 坏行=%d (%s)",
                     self.loaded_events, len(self._rows), self.bad_lines,
                     self.path)
            return self.loaded_events

    # ------------------------------------------------------------------
    def rows(self) -> list[dict]:
        """给 Engine 的完整恢复快照；包含内部 user_id / win_sequence。"""
        with self._lock:
            return [dict(row) for row in self._rows.values()]

    # ------------------------------------------------------------------
    def record_win(self, user_id: str, user_name: str,
                   event_id: str = "") -> bool:
        """持久记录一次真人胜场。event_id 已存在时幂等成功、不重复加分。"""
        if not self.enabled:
            return True
        uid = str(user_id or "").strip()
        name = str(user_name or "").strip()
        eid = str(event_id or "").strip()
        if not uid or not name:
            log.error("累计猜汤榜拒绝空身份胜场: user_id=%r user_name=%r",
                      uid, name)
            return False
        with self._lock:
            if eid and eid in self._event_ids:
                return True
            ok = _append_line(self.path, {
                "v": FORMAT_VERSION,
                "type": "win",
                "event_id": eid,
                "user_id": uid,
                "user_name": name,
                "at": time.time(),
            })
            if not ok:
                return False
            self._apply_win(uid, name, eid)
            self.loaded_events += 1
            return True

    # ------------------------------------------------------------------
    def top(self, limit: int = 10) -> list[dict]:
        """调试/日志用 TopN；公开字段与前端一致。"""
        with self._lock:
            rows = sorted(
                self._rows.values(),
                key=lambda row: (-row["solved_count"], row["win_sequence"]))
            return [
                {"rank": i + 1, "user_name": row["user_name"],
                 "solved_count": row["solved_count"]}
                for i, row in enumerate(rows[:max(0, int(limit))])
            ]

    def stats(self) -> dict:
        with self._lock:
            return {
                "players": len(self._rows),
                "wins": self.loaded_events,
                "bad_lines": self.bad_lines,
                "path": self.path,
                "enabled": self.enabled,
            }
