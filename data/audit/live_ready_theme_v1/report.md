# Live-ready Theme v1 — 库存验收报告

- 生成时间：2026-09-26T12:21:44.949683+00:00
- 正式池：`data/pool.jsonl`；SHA256 `711359702aa627ce23298b74db5daa31dee45d8edf2a58859324ae2e563ea1df`
- used ledger：`data/pool_used.jsonl`；SHA256 `c080ca6798edc35c1600934ea82d4869b3d23e17b8dda8c4cecda68ae8c2cb61`（不存在时为 null）
- ledger trustworthy：`true`
- 统计来源：同一次只读扫描生成的 `inventory.json`。

## 结论

| 指标 | 要求 | 实测 |
| --- | --- | --- |
| distinct current-policy 未播库存 | ≥ 50 | 53 |
| current v2 未播库存 | 元数据 | 45 |
| 五类 eligible | 每类 ≥ 10 | logic 18 / suspense 25 / horror 10 / emotion 30 / brainstorm 16 |

验收达成：`true`。Legacy 题不计五类 eligible；多标签题在 distinct 中只计一次。

## Prefill 历史与正式池事故

- 原 live-ready 命令：`uv run prefill_pool.py --live-ready --concurrency 5 --seed 20260926`。
- 首轮 71 次尝试、45 道入池；补回阶段 10 次尝试、8 道入池。
- 开发排障期间曾误对正式池执行 8 次 `pop_next`；发现后已用真实 LLM 补回。
- 最终验收 dry-run 全部在临时副本上执行，修复后的正式库存未再被 destructive smoke 消耗。
- 本次仅只读重建 manifest，未运行 LLM，新增 attempts=0、accepted=0。
- 正式 pool 与 used ledger 保持 gitignored；manifest 不包含谜面、汤底、观众资料或凭据。

## 元数据分布

- primary：`{"brainstorm": 9, "emotion": 20, "horror": 7, "logic": 1, "suspense": 8}`
- secondary eligible：`{"brainstorm": 7, "emotion": 10, "horror": 3, "logic": 17, "suspense": 17}`
- protocol：`{"haiguitang-v2": 45, "legacy": 8}`
- prompt：`{"haiguitang-generation-v3": 45, "keyword2-v7": 8}`
- quality policy：`{"quality-v13": 53}`
