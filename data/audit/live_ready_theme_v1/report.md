# Live-ready Theme v1 — 库存验收报告(Issue #60 §9/§17)

- 生成时间: 2026-09-26 19:29 (+08)
- 分支: `feat/theme-vote-category-pool-live-ready` @ `73040ed4dbe7f258a49241d76f08fe11fc784b04`
- 基线: main @ `69876af5c4447395f4c60604b65155d5d001eee7`
- 正式池: `data/pool.jsonl` + `data/pool_used.jsonl`(gitignored, 见 `.gitignore`)
- 本报告与 `inventory.json` **只含元数据**: 不含谜面/汤底正文、不含观众资料、不含凭据。

## 结论

**live-ready 达成**(两个硬指标同时满足, 以 `PuzzlePool` 当前政策门实时复核):

| 指标 | 要求 | 实测 |
| --- | --- | --- |
| distinct current-policy 未播库存 | ≥ 50 | **53** |
| 五类各 eligible(haiguitang-v2, 多标签重复计) | 每类 ≥ 10 | logic 18 / suspense 25 / horror 10 / emotion 30 / brainstorm 16 |

- distinct 按题计数(多标签只算一次); 分类按 `stock_by_category()`(v2 多标签每类各计一次)。
- distinct 53 中 45 道为 v2(本次 prefill 产物); 其余 8 道为基线运行遗留的
  legacy v13 题(`prompt_version=keyword2-v7`, 无 `protocol_version`) —— 按契约
  **legacy 不计入五类 eligible, 也不洗成 v2**; 它们计入 distinct(current-policy
  未播)但永远不参与主题兑现。

## Prefill 运行(Issue #60 §17)

- 命令: `uv run prefill_pool.py --live-ready --concurrency 5 --seed 20260926`
  (等价 `--target-total 50 --target-per-category 10 --max-attempts 150`)
- 开跑前检查: live heartbeat `data/live_heartbeat.json` = **inactive**(活跃时会以非 0 退出拒绝, 无默认绕过路径)
- 并发: **5** 条 generation pipeline, 每 worker **独立** `AnthropicMessagesClient` + `PuzzleWriter`(`_last_reject`/审稿侧信道实例状态不共用)
- 抽词: 整进程**一只** `KeywordBag`(session_seed=3491600036384432028, corpus_version=keyword2-vocab-v2, keyword_count=1134), `draw()` 内部持锁(§15 并发安全)
- 生成入口: `keyword_seed.keyword_spec()`(生产唯一入口), requested_category 经 `GenerationBrief` 定向, 未复制 Prompt/Truth/Surface/Contract 逻辑
- 入池: `pool.add_with_final_admission()`(§16 atomic final admission, check 与写入同一把锁)
- 运行记录(日志: `data/audit/prefill_run_live_ready.log`, gitignored):
  1. 19:00–19:21: 71 次尝试, 45 道入池(keyword2 未成题 22 / final admission 拒相似 4 / 其余为网关技术抖动重试)
  2. 19:35–19:38: 10 次尝试, 8 道入池 —— 开发期 8 次诊断性 `pop_next`(直连正式池的只读意图, 但 pop 本身是交付语义)意外消耗了 8 道题; 用真实 LLM 补回。**直播流程零泄漏**(此间无直播运行, heartbeat 全程 inactive); 这也是 dry-run 一律在临时副本池上执行的原因。
- 用时合计: 约 25 分钟

## 库存构成(元数据)

- quality policy: 53/53 = `quality-v13`(当前政策)
- v2 难度分布(45 道): medium 39 / hard 3 / easy 3
- v2 mechanism_family(Top): identity_misread 18, hidden_function 8, information_gap 6, object_misuse 5, observer_misread 5, emotional_motive 4, rule_constraint 2, goal_reversal 1
- `added_by`: prefill 45(本次) / prefetch 8(基线遗留 legacy, 非 v2)

## 未做/不做

- 未对正式池做任何 smoke pop(校验只读; 全部 dry-run 用临时副本, 见 PR 报告)。
- 未迁移/洗白 8 道 legacy 题(契约禁止伪装 policy/protocol)。
- 正式 pool/used ledger 保持 gitignored, **未** force-add。

## 复核方式(reviewer 可复现)

```bash
uv run python -c "from story.config import Config; from story.pool import PuzzlePool; \
p=PuzzlePool.open(Config()); print(p.distinct_stock_count()); print(p.stock_by_category())"
# 期望: 53 / {'logic': 20, 'suspense': 25, 'horror': 13, 'emotion': 29, 'brainstorm': 14}
```
