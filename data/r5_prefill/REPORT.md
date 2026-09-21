# R5 — `prefill_pool` 题源统一到 `keyword2-v7 / surface-v2`

**Issue** [#8](https://github.com/whopxxx/hgt-game-live/issues/8) · **base** `main @ fd2a001`
**分支** `fix/prefill-keyword2` · **不 merge**（等复审）

---

## 一、问题不是"红黑没调好"，是预热走错链

四个入口里，**只有一个**没走 `keyword_seed.keyword_spec()`：

| 入口 | R5 之前 | R5 之后 |
|---|---|---|
| live 现场生成 | `keyword_spec` | 不变 |
| 后台 `PoolPrefetcher` | `keyword_spec` | 不变 |
| Director 冷启动 prewarm | `keyword_spec` | 不变 |
| **独立 `prefill_pool.py`** | **`choose_blueprint -> gen_spec`** | **`keyword_spec`** |

后果没有任何下游症状：预热补出来的题**同样过 quality-v10**
（版本门只看 quality policy，不看 `prompt_version`），题能播、Reviewer
通过、不报错。唯一区别是风格 —— 而那正是这轮要播的东西。

实测盘上状态（R5 之前）：

```
54  (quality-v8,  keyword2-v4)     <- 被版本门隔离，不可播
 8  (quality-v10, riddle-v9)       <- 唯一可播的一批，全是 classic
```

也就是说 **R4 四轮调优（红黑 lane / Surface 藏真相 / 安全边界）在
正在播的那批题上一个字都没生效**。

---

## 二、改了什么

### 1. 默认走 `keyword_spec()`（不自建第二份骨架）

```python
spec, reason = keyword_spec(
    writer, seeder.bag, seeder.session_seed,
    avoid=[], recent=recent,
    should_continue=_always_continue,
    corpus_version=seeder.corpus_version)
```

**不**在 `prefill_pool.py` 里重抄一遍 Story/Surface 装配。那个骨架的价值
全在"只有一份"——它写着抽词+lane 的顺序、三处让路检查的位置、provenance
字段集、失败原因标签。抄一份就会漂，漂了之后"预热的题"与"直播现场生成的
题"不是同一种东西，正是本 issue 要消灭的形状。

### 2. `KeywordBag` 整进程一只（`_PrefillSeeder`）

这是最容易修假的点。每次 `_one()` 重建 bag => 每个 draw 都从 index=1
重来 => `draw_lane(session_seed, 1)` 是**常量** => **整批预热题非红即黑**。
红黑混出正是要播的性质，而它消失时质量门一条都不会报警。

session seed 规则逐条照搬 `PoolPrefetcher._init_keyword_bag`：

```
给了 keyword_session_seed  -> 直接用
只给了 quality_seed        -> derive_session_seed() 派生
都没有                     -> 随机一次 + 立刻写进 INFO 日志
```

### 3. classic 保留为 kill-switch

`--no-keyword-seed`（**与 `story/config.py` 同名同 dest**）→ 完整回到
`choose_blueprint -> gen_spec`。classic 链的调用形状**一个字没改**。

⚠️ 参数经 `_cfg_from_args` **透传**到 `Config`，并在 `main` 里断言两者
一致。不透传的话参数解析不报错、`a` 说是关的、`cfg` 仍是默认开 ——
kill-switch 看起来生效了实际没有。

### 4. corpus 坏了 → 显式降级，不假装 keyword2

`CorpusError` → 一条 ERROR（点名"这批会是 riddle-v9"）+ 整条链退回
classic。**不回退人工 `KEYWORD_BANK`**（G3 点名的形状：看起来在跑
keyword2，其实用人工词）。

### 5. 版本号

`STORY_PROMPT_VERSION` / `SURFACE_PROMPT_VERSION` / `CHECK_PROMPT_VERSION` /
`QUALITY_POLICY_VERSION` **一律不动**。本次是**生成入口对齐**，不是
"同一份 spec 收不收变了"——按唯一那条判定标准，不该 bump。

---

## 三、验收

### 3.1 新 prefill 前 5 道

| # | prompt_version | story | surface | lane | keywords | idx | 汤面字数 |
|---|---|---|---|---|---|---|---|
| 1 | `keyword2-v7` | `keyword2-v7` | `surface-v2` | black | 列车上 / 威胁 | 1 | 78 |
| 2 | `keyword2-v7` | `keyword2-v7` | `surface-v2` | red | 流星 / 二哥 | 9 | 40 |
| 3 | `keyword2-v7` | `keyword2-v7` | `surface-v2` | black | 网络 / 假发 | 5 | 44 |
| 4 | `keyword2-v7` | `keyword2-v7` | `surface-v2` | red | 神秘符号 / 老人 | 9 | 79 |
| 5 | `keyword2-v7` | `keyword2-v7` | `surface-v2` | black | 笑 / 计划生育 | 11 | 75 |

**`riddle-v9` 数量 = 0。**（全部 6 道都是 `keyword2-v7`。）

`keyword_draw_index` = 1 / 9 / 5 / 9 / 11 / 14 —— 不是恒为 1，
**但这一列不能直接读成"一只 bag 连续复用"**：同一个进程内 draw index
只递增，不可能从 9 回到 5。真实情况是**两个 prefill 进程**
（`session_seed` 分别是 `17733258656912476509` 与 `14889134290040591661`）：

```
run 1:  1 -> 9                （递增）
run 2:  5 -> 9 -> 11 -> 14    （递增）
```

**结论（bag 确实被复用）成立，证据是"进程内严格递增"**，而不是那条
跨进程合并序列。上一版这里是我读错了。逐题 `session_seed` 见
`data/r5_prefill/SNAPSHOT.md`。

lane 4 black / 2 red，确实混出。

### 3.2 active pool 当前

```
stock_count        = 6
playable_count     = 6
keyword2-v7        = 6
classic riddle-v9  = 0
```

### 3.3 旧 pool 备份位置

```
data/pool.jsonl.bak-r5-classic     （395,758 B, md5 f67a4acd6ca7787a4726af53d46ce2f0）
```

按 issue 要求做的**非破坏性轮换**：备份 → 清空 generated pool → 重新
预热。`played.jsonl`（60 行）与 `curated_used.jsonl` **一行没动**，
历史已播记录不受影响，旧题不会复活。

### 3.4 回归（A~E + 变异）

`tests/test_pool.py` 新增 8 条，每条都做了变异验证：

| 用例 | 断言 | 变异 → 结果 |
|---|---|---|
| `test_r5_prefill_default_goes_through_keyword_spec` | 前三个 tool = `emit_core_story/emit_surface/emit_structure`，无 `emit_riddle`；provenance 齐 | 删默认分支 → **18 FAIL** |
| `test_r5_prefill_reuses_one_bag_across_attempts` | `draw_index` 递增 1/2/3，不是 1/1/1 | 每次重建 bag → **3 FAIL** |
| `test_r5_prefill_kill_switch_really_goes_classic` | tool 回到 `emit_riddle`；**代码层**两个分支都在 | 删分支判断 → **4 FAIL** |
| `test_r5_corpus_broken_degrades_explicitly_not_silently` | ERROR 日志 + `enabled=False` + **行为层**走 classic | 静默重开 → **2 FAIL** |
| `test_r5_prefill_and_prefetch_share_one_generation_mode` | 两边都 import `keyword_spec`，同一个 flag 名 | — |
| `test_r5_cli_switch_reaches_config` | `--no-keyword-seed` 真到 `cfg` | 删透传 → **2 FAIL** |
| `test_r5_prefill_offline_should_continue_is_always_true` | 恒真谓词，无相位/无预算 | 谓词改 False → **16 FAIL** |
| `test_r5_prefill_failure_reports_reject_reason` | 未成题照实记账 | — |

**离线套件：21/23 绿 + 2 个基线就有的 GBK 崩溃**
（`test_solve_ux` 的 `U+2286`、`test_curated_import` 的 `U+00A9`；
异常签名与基线逐字相同，**非回归**）。

#### 顺带修掉一个测试基础设施缺口

变异 M3（删掉 `_one` 里的分支判断）**原本让套件直接崩掉**而不是打印
FAIL —— 后续用例一条都没跑，输出末尾看起来仍像正常结束，退出码也还是
1，很容易被读成"这个变异没被抓住"。现在 `main()` 给**每个用例单独兜
异常**并记成 FAIL，变异测试读到的就是确定的红灯。

---

## 四、不做的事（按 issue）

- 不改 red / black lane 文案
- 不收紧"红汤/黑汤定义"
- 不改 Story Prompt、不改 Surface Prompt
- 不加 Reviewer / 评分器 / 关键词黑名单
- 不 bump quality policy
- 不改直播 QA
- 不 merge（等复审）
