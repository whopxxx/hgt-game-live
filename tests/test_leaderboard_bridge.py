#!/usr/bin/env python
# coding: utf-8
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tools.bridge_legacy_leaderboard as bridge
from story.leaderboard import LeaderboardLedger


def _write_jsonl(path: str, rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def check(label, cond, detail=None):
    if not cond:
        raise AssertionError(f"{label}: {detail!r}")
    print("  ok ", label)


def main():
    print("[legacy leaderboard bridge]")
    with tempfile.TemporaryDirectory(prefix="hgt_bridge_") as td:
        puzzle = os.path.join(td, "puzzle.jsonl")
        danmaku = os.path.join(td, "danmaku.jsonl")
        leaderboard = os.path.join(td, "leaderboard.jsonl")

        _write_jsonl(puzzle, [
            {"session": "old", "puzzle_index": 1, "reason": "solved",
             "winner": "OldUser"},
            {"session": "live-1", "puzzle_index": 1, "reason": "solved",
             "winner": "Alice"},
            {"session": "live-1", "puzzle_index": 2, "reason": "timeout",
             "winner": ""},
            {"session": "live-1", "puzzle_index": 3, "reason": "solved",
             "winner": "Bob"},
            {"session": "live-1", "puzzle_index": 4, "reason": "solved",
             "winner": "Same"},
            {"session": "live-1", "puzzle_index": 5, "reason": "solved",
             "winner": "Same"},
        ])
        _write_jsonl(danmaku, [
            {"kind": "chat", "user_id": "u-alice", "user_name": "Alice"},
            {"kind": "chat", "user_id": "u-bob", "user_name": "Bob"},
            {"kind": "chat", "user_id": "u-x", "user_name": "Same"},
            {"kind": "chat", "user_id": "u-y", "user_name": "Same"},
        ])

        original_fetch = bridge._fetch_state
        bridge._fetch_state = lambda _url: {
            "leaderboard": [
                {"rank": 1, "user_name": "Same", "solved_count": 2},
                {"rank": 2, "user_name": "Alice", "solved_count": 1},
                {"rank": 3, "user_name": "Bob", "solved_count": 1},
            ],
            "danmaku": [],
        }
        try:
            r1 = bridge.sync_once(
                puzzle_path=puzzle, danmaku_path=danmaku,
                leaderboard_path=leaderboard, state_url="http://unused")
            check("默认锁定最新 session", r1["session"] == "live-1", r1)
            check("只迁移可唯一识别的两题", r1["imported"] == 2, r1)
            check("同名多 UID 两题都挂起",
                  r1["unresolved"].get("Same", {}).get("wins") == 2, r1)
            check("旧 session 不混入", all(
                row["user_name"] != "OldUser" for row in r1["top"]), r1["top"])

            # 再跑一遍必须幂等。
            r2 = bridge.sync_once(
                puzzle_path=puzzle, danmaku_path=danmaku,
                leaderboard_path=leaderboard, state_url="http://unused",
                session="live-1")
            check("重复运行不重复写", r2["imported"] == 0 and r2["already"] == 2, r2)

            # 如果新版 Director 已经先写 session:round，bridge 不能再造 legacy 事件。
            with open(puzzle, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "session": "live-1", "puzzle_index": 6,
                    "reason": "solved", "winner": "Carol"
                }, ensure_ascii=False) + "\n")
            with open(danmaku, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "kind": "chat", "user_id": "u-carol", "user_name": "Carol"
                }, ensure_ascii=False) + "\n")
            ledger = LeaderboardLedger(path=leaderboard, enabled=True)
            ledger.load()
            check("预置新版 Director 事件",
                  ledger.record_win("u-carol", "Carol", "live-1:6"))
            r3 = bridge.sync_once(
                puzzle_path=puzzle, danmaku_path=danmaku,
                leaderboard_path=leaderboard, state_url="http://unused",
                session="live-1")
            check("已有新版事件不会双算",
                  r3["imported"] == 0 and r3["already"] == 3, r3)

            # 本机显式消歧后，两个 Same 胜场都能安全补入。
            r4 = bridge.sync_once(
                puzzle_path=puzzle, danmaku_path=danmaku,
                leaderboard_path=leaderboard, state_url="http://unused",
                session="live-1", manual_map={"Same": "u-x"})
            check("人工映射补齐歧义胜场", r4["imported"] == 2, r4)
            check("补齐后无 Same unresolved", "Same" not in r4["unresolved"], r4)

            # /state 最近弹幕本身也能提供唯一 UID。
            with open(puzzle, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "session": "live-1", "puzzle_index": 7,
                    "reason": "solved", "winner": "Fish"
                }, ensure_ascii=False) + "\n")
            bridge._fetch_state = lambda _url: {
                "leaderboard": [],
                "danmaku": [{"user_id": "u-fish", "user_name": "Fish"}],
            }
            r5 = bridge.sync_once(
                puzzle_path=puzzle, danmaku_path=danmaku,
                leaderboard_path=leaderboard, state_url="http://unused",
                session="live-1", manual_map={"Same": "u-x"})
            check("/state 最近弹幕可补 UID", r5["imported"] == 1, r5)

            final = LeaderboardLedger(path=leaderboard, enabled=True)
            final.load()
            check("最终共 6 个胜场",
                  final.stats()["wins"] == 6, final.stats())
        finally:
            bridge._fetch_state = original_fetch

    print("ALL OK")


if __name__ == "__main__":
    main()
