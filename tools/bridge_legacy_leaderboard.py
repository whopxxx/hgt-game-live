#!/usr/bin/env python
# coding: utf-8
"""把“升级前仍在运行的旧直播”胜场旁路迁移到永久排行榜。

适用场景:
- 直播进程是在 leaderboard 持久化功能上线前启动的；
- 不能现在停播/重启；
- 旧进程仍持续写 data/puzzle.jsonl + data/danmaku.jsonl，
  本地渲染服务仍可 GET /state。

迁移依据:
- puzzle.jsonl: session + puzzle_index + reason + winner
- danmaku.jsonl / /state.danmaku: user_name -> user_id
- leaderboard.jsonl: 新版永久榜

安全策略:
- 每一题使用稳定 event_id = legacy:<session>:<puzzle_index>，重复运行幂等；
- 如果新版 Director 已经写过 <session>:<puzzle_index>，也会跳过，避免双算；
- 昵称映射到 0 个或多个 UID 时绝不猜，保持 unresolved；
- 可用 --map "昵称=UID" 在本机显式消歧；
- 工具只 append 永久榜，不修改旧直播进程和旧日志。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from story.leaderboard import LeaderboardLedger  # noqa: E402


def _read_jsonl(path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
    except FileNotFoundError:
        pass
    return rows


def _fetch_state(url: str, timeout: float = 2.0) -> dict[str, Any]:
    try:
        req = Request(url, headers={"Cache-Control": "no-cache"})
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310 - localhost tool
            obj = json.loads(resp.read().decode("utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _parse_map_args(items: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in items:
        if "=" not in raw:
            raise ValueError(f"--map 必须是 昵称=UID: {raw!r}")
        name, uid = raw.split("=", 1)
        name, uid = name.strip(), uid.strip()
        if not name or not uid:
            raise ValueError(f"--map 昵称/UID 不能为空: {raw!r}")
        out[name] = uid
    return out


def _latest_session(puzzles: list[dict[str, Any]]) -> str:
    for rec in reversed(puzzles):
        sid = str(rec.get("session", "") or "").strip()
        if sid:
            return sid
    return ""


def _uid_candidates(danmaku: list[dict[str, Any]],
                    state: dict[str, Any]) -> dict[str, set[str]]:
    by_name: dict[str, set[str]] = defaultdict(set)

    def add(row: Any) -> None:
        if not isinstance(row, dict):
            return
        name = str(row.get("user_name", "") or "").strip()
        uid = str(row.get("user_id", "") or "").strip()
        if name and uid:
            by_name[name].add(uid)

    for row in danmaku:
        add(row)
    for row in (state.get("danmaku") or []):
        add(row)
    return by_name


def _event_ids(path: str) -> set[str]:
    out: set[str] = set()
    for rec in _read_jsonl(path):
        eid = str(rec.get("event_id", "") or "").strip()
        if eid:
            out.add(eid)
    return out


def sync_once(*, puzzle_path: str, danmaku_path: str,
              leaderboard_path: str, state_url: str,
              session: str = "", manual_map: dict[str, str] | None = None
              ) -> dict[str, Any]:
    puzzles = _read_jsonl(puzzle_path)
    sid = str(session or "").strip() or _latest_session(puzzles)
    if not sid:
        return {
            "session": "", "solved": 0, "imported": 0, "already": 0,
            "unresolved": {}, "error": "还找不到当前 session；至少等一题揭晓后再试",
        }

    current = [
        r for r in puzzles
        if str(r.get("session", "") or "").strip() == sid
    ]
    solved = [
        r for r in current
        if str(r.get("reason", "") or "").strip() == "solved"
        and str(r.get("winner", "") or "").strip()
    ]

    state = _fetch_state(state_url)
    candidates = _uid_candidates(_read_jsonl(danmaku_path), state)
    overrides = dict(manual_map or {})
    existing = _event_ids(leaderboard_path)

    ledger = LeaderboardLedger(path=leaderboard_path, enabled=True)
    ledger.load()

    imported = 0
    already = 0
    unresolved: dict[str, dict[str, Any]] = {}
    winner_counts: dict[str, int] = defaultdict(int)

    # puzzle.jsonl 是 append-only；按文件顺序迁移，保证同 UID 改名时
    # LeaderboardLedger 最终显示最近一次获胜昵称。
    for rec in solved:
        name = str(rec.get("winner", "") or "").strip()
        winner_counts[name] += 1
        try:
            puzzle_index = int(rec.get("puzzle_index"))
        except (TypeError, ValueError):
            unresolved[name] = {
                "wins": winner_counts[name], "reason": "bad_puzzle_index",
                "uids": [],
            }
            continue

        direct_eid = f"{sid}:{puzzle_index}"
        legacy_eid = f"legacy:{sid}:{puzzle_index}"
        if direct_eid in existing or legacy_eid in existing:
            already += 1
            continue

        if name in overrides:
            uid = overrides[name]
        else:
            uids = sorted(candidates.get(name, set()))
            if len(uids) != 1:
                item = unresolved.setdefault(name, {
                    "wins": 0,
                    "reason": "no_uid" if not uids else "ambiguous_uid",
                    "uids": uids,
                })
                item["wins"] += 1
                continue
            uid = uids[0]

        if ledger.record_win(uid, name, event_id=legacy_eid):
            existing.add(legacy_eid)
            imported += 1

    # /state 的旧榜通常只公开 Top3。只做一致性诊断，不据此造事件。
    visible = {}
    for row in (state.get("leaderboard") or []):
        if isinstance(row, dict):
            name = str(row.get("user_name", "") or "").strip()
            try:
                count = int(row.get("solved_count", 0))
            except (TypeError, ValueError):
                continue
            if name:
                visible[name] = count
    mismatches = {
        name: {"state": count, "archive": winner_counts.get(name, 0)}
        for name, count in visible.items()
        if winner_counts.get(name, 0) != count
    }

    return {
        "session": sid,
        "solved": len(solved),
        "imported": imported,
        "already": already,
        "unresolved": unresolved,
        "mismatches": mismatches,
        "top": ledger.top(10),
        "error": "",
    }


def _print_report(report: dict[str, Any]) -> None:
    if report.get("error"):
        print("ERROR:", report["error"], flush=True)
        return
    print(
        f"[bridge] session={report['session']} solved={report['solved']} "
        f"本轮新增={report['imported']} 已存在={report['already']}",
        flush=True,
    )
    for name, info in report.get("unresolved", {}).items():
        reason = info.get("reason")
        uids = info.get("uids") or []
        if reason == "ambiguous_uid":
            why = f"同名对应多个 UID: {', '.join(uids)}"
        elif reason == "no_uid":
            why = "尚未观察到 UID；等该用户再发弹幕，或用 --map 本机指定"
        else:
            why = reason
        print(f"  未迁移: {name!r} ×{info.get('wins', 0)} — {why}",
              flush=True)
    for name, pair in report.get("mismatches", {}).items():
        print(
            f"  WARNING: /state 与 puzzle archive 不一致: {name!r} "
            f"state={pair['state']} archive={pair['archive']}",
            flush=True,
        )
    if report.get("top"):
        print("  永久榜 Top10:", flush=True)
        for row in report["top"]:
            print(
                f"    {row['rank']:>2}. {row['user_name']} "
                f"{row['solved_count']}题",
                flush=True,
            )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="旧直播运行中：持续把胜场旁路同步到永久排行榜")
    ap.add_argument("--state-url", default="http://127.0.0.1:8765/state")
    ap.add_argument("--puzzle-log", default=os.path.join("data", "puzzle.jsonl"))
    ap.add_argument("--danmaku-log", default=os.path.join("data", "danmaku.jsonl"))
    ap.add_argument("--leaderboard", default=os.path.join("data", "leaderboard.jsonl"))
    ap.add_argument("--session", default="",
                    help="显式旧 session id；默认取 puzzle.jsonl 最新 session")
    ap.add_argument("--map", action="append", default=[], metavar="昵称=UID",
                    help="本机人工消歧，可重复；不要提交到仓库")
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args(argv)

    if args.interval <= 0:
        ap.error("--interval 必须 > 0")
    try:
        manual = _parse_map_args(args.map)
    except ValueError as e:
        ap.error(str(e))

    pinned_session = str(args.session or "").strip()
    print("旧直播排行榜桥接已启动。Ctrl+C 只停止桥接，不影响直播。", flush=True)
    print(f"目标永久榜: {args.leaderboard}", flush=True)

    last_signature = None
    try:
        while True:
            report = sync_once(
                puzzle_path=args.puzzle_log,
                danmaku_path=args.danmaku_log,
                leaderboard_path=args.leaderboard,
                state_url=args.state_url,
                session=pinned_session,
                manual_map=manual,
            )
            if not pinned_session and report.get("session"):
                # 一旦识别到当前旧直播 session，进程生命周期内固定它。
                # 后续即使 puzzle.jsonl 出现别的 session，也不能串台迁移。
                pinned_session = str(report["session"])
            signature = json.dumps({
                "session": report.get("session"),
                "solved": report.get("solved"),
                "imported": report.get("imported"),
                "already": report.get("already"),
                "unresolved": report.get("unresolved"),
                "mismatches": report.get("mismatches"),
                "error": report.get("error"),
            }, ensure_ascii=False, sort_keys=True)
            if signature != last_signature or report.get("imported"):
                _print_report(report)
                last_signature = signature
            if args.once:
                return 1 if report.get("error") else 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n桥接已停止；直播进程未受影响。", flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
