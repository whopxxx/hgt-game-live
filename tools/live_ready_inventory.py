"""Read-only live-ready inventory and report from the runtime pool files."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from story.config import Config
from story.haiguitang_protocol import V2_CATEGORIES
from story.pool import PuzzlePool, spec_key


def _sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def build(pool_path: Path, used_path: Path) -> dict:
    before = (_sha(pool_path), _sha(used_path))
    if before[0] is None:
        raise FileNotFoundError(pool_path)
    cfg = Config(pool_path=str(pool_path), pool_used_path=str(used_path))
    pool = PuzzlePool.open(cfg)
    if not pool.ledger_trustworthy:
        raise ValueError("used ledger is not trustworthy")
    sources = {}
    for line in pool_path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and not row.get("void"):
            sources[str(row.get("pool_key", ""))] = str(row.get("added_by", ""))

    entries = []
    for spec in pool._items:
        key = spec_key(spec)
        if key in pool._used or not pool._validate_pool_spec(spec)[0]:
            continue
        protocol = str(spec.protocol_version or "")
        categories = list(spec.categories or []) if protocol == "haiguitang-v2" else []
        entries.append({
            "pool_key": key,
            "protocol_version": protocol,
            "prompt_version": str(spec.prompt_version or ""),
            "quality_policy_version": str(spec.quality_policy_version or ""),
            "primary_category": str(spec.primary_category or "") if categories else "",
            "categories": categories,
            "difficulty": str(spec.difficulty or "") if categories else "",
            "mechanism_family": str(getattr(spec.signature, "mechanism_family", "") or ""),
            "eligible_v2": bool(categories),
            "added_source": sources.get(key, ""),
        })
    after = (_sha(pool_path), _sha(used_path))
    if before != after:
        raise RuntimeError("runtime pool or used ledger changed during audit")
    by_category = {c: sum(c in e["categories"] for e in entries)
                   for c in V2_CATEGORIES}
    observed = {
        "distinct_stock_count": len(entries),
        "distinct_v2_unplayed": sum(e["eligible_v2"] for e in entries),
        "stock_by_category": by_category,
        "primary_counts": dict(Counter(e["primary_category"] for e in entries if e["eligible_v2"])),
        "secondary_eligible_counts": {c: sum(c in e["categories"] and e["primary_category"] != c for e in entries) for c in V2_CATEGORIES},
        "protocol_versions": dict(Counter(e["protocol_version"] or "legacy" for e in entries)),
        "prompt_versions": dict(Counter(e["prompt_version"] for e in entries)),
        "quality_policy_versions": dict(Counter(e["quality_policy_version"] for e in entries)),
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "issue": 60,
        "milestone": "Live-ready Theme v1",
        "pool_path": str(pool_path).replace("\\", "/"),
        "used_ledger_path": str(used_path).replace("\\", "/"),
        "pool_file_sha256": after[0],
        "used_ledger_sha256": after[1],
        "used_ledger_exists": after[1] is not None,
        "ledger_trustworthy": pool.ledger_trustworthy,
        "criteria": {"distinct_current_policy_unplayed_min": 50,
                     "per_category_eligible_min": 10},
        "observed": observed,
        "criteria_met": len(entries) >= 50 and all(n >= 10 for n in by_category.values()),
        "inventory": entries,
    }


def report(data: dict) -> str:
    o = data["observed"]
    cats = o["stock_by_category"]
    counts = " / ".join(f"{c} {cats[c]}" for c in V2_CATEGORIES)
    return f"""# Live-ready Theme v1 — 库存验收报告

- 生成时间：{data['generated_at']}
- 正式池：`{data['pool_path']}`；SHA256 `{data['pool_file_sha256']}`
- used ledger：`{data['used_ledger_path']}`；SHA256 `{data['used_ledger_sha256']}`（不存在时为 null）
- ledger trustworthy：`{str(data['ledger_trustworthy']).lower()}`
- 统计来源：同一次只读扫描生成的 `inventory.json`。

## 结论

| 指标 | 要求 | 实测 |
| --- | --- | --- |
| distinct current-policy 未播库存 | ≥ 50 | {o['distinct_stock_count']} |
| current v2 未播库存 | 元数据 | {o['distinct_v2_unplayed']} |
| 五类 eligible | 每类 ≥ 10 | {counts} |

验收达成：`{str(data['criteria_met']).lower()}`。Legacy 题不计五类 eligible；多标签题在 distinct 中只计一次。

## Prefill 历史与正式池事故

- 原 live-ready 命令：`uv run prefill_pool.py --live-ready --concurrency 5 --seed 20260926`。
- 首轮 71 次尝试、45 道入池；补回阶段 10 次尝试、8 道入池。
- 开发排障期间曾误对正式池执行 8 次 `pop_next`；发现后已用真实 LLM 补回。
- 最终验收 dry-run 全部在临时副本上执行，修复后的正式库存未再被 destructive smoke 消耗。
- 本次仅只读重建 manifest，未运行 LLM，新增 attempts=0、accepted=0。
- 正式 pool 与 used ledger 保持 gitignored；manifest 不包含谜面、汤底、观众资料或凭据。

## 元数据分布

- primary：`{json.dumps(o['primary_counts'], ensure_ascii=False, sort_keys=True)}`
- secondary eligible：`{json.dumps(o['secondary_eligible_counts'], ensure_ascii=False, sort_keys=True)}`
- protocol：`{json.dumps(o['protocol_versions'], ensure_ascii=False, sort_keys=True)}`
- prompt：`{json.dumps(o['prompt_versions'], ensure_ascii=False, sort_keys=True)}`
- quality policy：`{json.dumps(o['quality_policy_versions'], ensure_ascii=False, sort_keys=True)}`
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", type=Path, default=Path("data/pool.jsonl"))
    ap.add_argument("--used", type=Path, default=Path("data/pool_used.jsonl"))
    ap.add_argument("--output", type=Path, default=Path("data/audit/live_ready_theme_v1"))
    args = ap.parse_args()
    data = build(args.pool, args.used)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "inventory.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.output / "report.md").write_text(report(data), encoding="utf-8")
    print(data["observed"], "criteria_met=", data["criteria_met"])


if __name__ == "__main__":
    main()
