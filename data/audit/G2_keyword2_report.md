# G2 —— keyword2 接入生产(AI-original 后台补池两阶段起题)

**基线 main** `3e85e2960a3da2b0e460ef133fee315087d6eaac`(G1-B)
**生产链改动** `story/llm.py`(+694 行, **0 删除**)、`story/prefetch.py`、
`story/config.py`、新增 `story/keyword_seed.py`
**未改**: `RIDDLE_SYSTEM` / Reviewer / truth audit / `validate_spec` /
`cross_puzzle_gate` / curated 链 / live generation 路径 / `H4-F`

---

## 一、做了什么

普通 AI `PoolPrefetcher` 的候选产生方式换成:

```
程序随机抽 2 个普通生活关键词
  ↓  Stage A   只生成 title / puzzle / answer(自由成题)
  ↓  Stage B   冻结谜面谜底, 只结构化成 PuzzleSpec
  ↓  现有 generated Reviewer
  ↓  现有 truth audit
  ↓  validate_spec
  ↓  现有 AI-original cross_puzzle_gate HARD
  ↓  too_similar / avoid
  ↓  normal generated pool
```

`pool_keyword_seed_enabled=False` **完整**回到 `pick_blueprint -> gen_spec`。

### 生产**不**复用 `CuratedCompiler`

借的只有 `make_unconstrained_blueprint()` 这一个**纯函数哨兵**(经
`_unconstrained_blueprint()` 延迟导入)。`CuratedCompiler` 本身**没有**被
import —— 它带 external curated 的 policy 语义、provenance 与 curated-v5
准入账本, 而 H4-F 已明确两条链的 diversity policy 不同。

keyword 题落成**普通 generated PuzzleSpec**: `source_type` 为空, 所以
`_is_curated()` 判否 => 审稿人拿到的是 **generated 八项**契约, 不是外部
题库那九项。这是"不得被标成 curated"的**结构性**实现。

---

## 二、provenance:三条链可区分(§十一)

不新增 schema 字段 —— `metrics` 本来就过 `to_dict`/`from_dict` 往返
(`puzzle.py` 的 `_PROVENANCE_KEYS` 含 `"metrics"`), 且**不进前端**。

| 链 | `prompt_version` | `metrics.generation_mode` | `source_type` |
|---|---|---|---|
| classic blueprint AI | `riddle-v9` | (无) | 空 |
| **keyword2 AI** | **`keyword2-v1`** | **`keyword2`** | **空** |
| curated | `curated-v1` | (无) | `curated` |

另记 `metrics["keywords"] = [...]`(只进 archive / 日志)。
`QUALITY_POLICY_VERSION` **未 bump**(接受标准一字未改); `RIDDLE_PROMPT_VERSION`
也**未 bump**(live 仍在用它, 两条链并存)。

---

## 三、让路检查点(§九):4 处

```
① Stage A 之前            prefetch._generate_keyword_one
② Stage A 之后 / B 之前    prefetch._generate_keyword_one   ← 新增 stage 最易漏的一处
③ Stage B 之后 / 审稿前    llm.structure_original_idea
④ 审稿之后 / audit 前      llm.structure_original_idea
(+ 试玩前 —— 既有 G4-C 检查点, 两条链共用)
```

任一 false -> `("interrupted", ...)`。**不计 gen_fail、不退避、不加失败链**。

段内检查点(③④)落在 `structure_original_idea` 里是刻意的: 那里与 `gen_spec`
的写法同源(同一个谓词、同一个 fail-closed 语义), 在 prefetch 侧再抄一遍
只会得到两份会漂的判定。

---

## 四、预算(§十):未调整

```
Stage A          1 attempt   (一次失败就本轮失败)
Stage B compile  1 attempt   (Reviewer 自带 technical retry 保留)
```

`pool_prefetch_budget_seconds` **保持 25.0**。keyword 模式最坏昂贵调用数:

```
Stage A 1 + Stage B compile 1 + Reviewer 1(+1 tech retry) + audit 1(+1 tech retry)
= 6 次
```

**不多于** classic 链(4 稿 × ~3 次 = 12 次上限), 所以 `effective_guard_s
= max(30, 25+5) = 30` 无需改动。**未沿用**实验的 `max_stage2_attempts=4`。

---

## 五、17 条回归 + 7 个变异

### 回归落点

| 文件 | 覆盖 |
|---|---|
| `tests/test_keyword_seed.py`(**新**) | 每题恰好 2 key / 不同 slot / 固定 seed 可复现 / `used_pairs` 生效 / **`--draw-only` md5 == G1 基线** / 不碰全局 `random` / **依赖方向 `story/ <- tools/`** |
| `tests/test_prefetch.py`(新增 15 个 `test_g2_*`) | 不发 target Blueprint / **Stage A 后让路 -> B 不调用** / 让路不计失败不退避 / rewrite 不入池 / quota 墙仍 HARD 拒 / `too_similar` 仍拒 / kill-switch 逐位回到旧链 / **live 路径零 keyword 调用** |
| `tests/test_llm.py`(新增 12 个 `test_g2_keyword_*`) | Stage A 只出三样 / 1 attempt / **canonical 三样被冻结** / **模型硬塞 puzzle 也无效** / schema 无 puzzle 字段 / provenance / **走 generated 八项契约(含反证)** / unconstrained 哨兵 / 两处让路 / rewrite 失败 / audit fail 拒 |

### 变异(全部**变红**)

| # | 变异 | 结果 |
|---|---|---|
| M1 | Stage B 改用 curated `spec_from_tool`(写 `source_type="curated"`) | **红 ×14**(两个套件) |
| M2 | 去掉 Stage A 之后的让路检查点 | **红 ×3** |
| M3 | 让路记成 `gen_fail` | **红 ×4**(`interrupted_count` / `_retry_at` / `_fail_streak` 全红) |
| M4 | schema 加回 `puzzle` 字段并采用模型值 | **红 ×2** |
| M5 | Stage B 发真 target Blueprint | **红 ×2** |
| M6 | `pool_keyword_seed_enabled` 被忽略 | **红** |
| M7 | live 路径也调 `gen_keyword_idea` | **红**(见下) |

> ⚠️ **M7 第一版没有变红** —— 这是一个真实的测试缺口, 记一笔。
> 当时 `test_g2_live_writer_never_calls_keyword` 只驱动 **writer 的调用形状**,
> 证明不了"没人往 `director.py` 那一行前面插一次 keyword 调用"。补了**源码层**
> 断言(`director.py` 里不得出现 `gen_keyword_idea` / `structure_original_idea`)
> 之后 M7 立刻红。教训与 H4-F 的 M2 同源: **变异打在不生效的路径上, 绿灯是假的。**

---

## 六、fake-client 端到端(§交付)

`2-key -> Stage A -> Stage B -> Reviewer -> audit -> generated pool`

```
tools called: emit_keyword_idea, emit_structure, emit_review, emit_truth_audit
stock: 1   added: 1
source_type: ''                 <- generated, 不是 curated
prompt_version: keyword2-v1
generation_mode: keyword2
keywords: ['回家', '教室']      <- 程序抽的 2 个词
puzzle is Stage A's: True       <- 谜面冻结生效
curated_policy_version: ''      <- 没被标成 curated
```

---

## 七、证明 live generation 仍走旧链

三条独立证据:

1. **源码**: `director.py` 的 live 出题点仍是 `self.writer.gen_spec(...)`(未改一行);
   `grep gen_keyword_idea\|structure_original_idea director.py` -> **0**。
2. **`RIDDLE_SYSTEM` 未动**: `story/llm.py` 相对基线的 diff 是
   **+694 / -0**(纯新增), `RIDDLE_SYSTEM = ` 的增删行数为 **0**。
3. **测试**: `test_g2_live_writer_never_calls_keyword` 行为层 + 源码层双重断言,
   且 M7 变异证明它**会红**。

---

## 八、验证

- **21 个离线套件全部 rc=0 / 0 fails**(`test_judge_golden` 按既定口径不作为
  gate —— 联网非确定性; `test_web` 需浏览器, 由 CI 提供)。
- **装配冒烟**: `director.py --sim ... --no-llm --no-window --reveal-hold 3
  --max-puzzles 2` -> exit=**124**、**0 Traceback**、"题就位" 存在。
- **G1 抽取序列未变**: `--draw-only` 输出按 **LF 归一**后的 md5 =
  `9493d2cc11fe03b49d516f5233455dcd`(与 G1-A/G1-B 基线**同一段文本**;
  见下面那笔 CI 事故)。
- CI 加了 `离线套件 keyword_seed` 一步。

### ⚠️ CI 事故: 一条平台相关的绿

第一版 `test_g1_draw_sequence_pinned` 直接对 `subprocess` 的 `r.stdout`
取 md5, 常量写的是**本地**(Windows)那个值 `049c7073...`。本地全绿,
**CI 上 `离线套件 keyword_seed` 挂了**。

根因: `--draw-only` 用 `print()`, Python 的 stdout 在 Windows 上把 `\n`
翻成 `\r\n`, Linux 上不翻 —— 于是同一个抽取序列在两个平台上得到**两个
不同的 md5**。这是一条**假绿**(本地)加一条**假红**(CI)。

修法: 先把输出 `.replace("\r\n", "\n")` 归一, 再取 md5, 常量改成
`9493d2cc...`。两个平台现在一致。

> 教训与 H4-F 的 M2 同源, 但形状不同: 那次是"变异打在不生效的路径上",
> 这次是"断言依赖了运行环境"。**只在本地跑过的绿灯不足以证明它是绿的** ——
> 这条如果没接 CI, 会以"CI 红而本地绿"的形式一直挂着, 而那种红最容易被
> 当成 flake 忽略掉。

---

## 九、不动(边界确认)

```
live generation(一阶段 Blueprint 链)        ✅ 未改
curated 链(LazyCurator / CuratedCompiler)   ✅ 未改, 生产未复用
H4-F(外部题不因分布被拒)                    ✅ 未改
AI-original quota(cross_puzzle_gate HARD)   ✅ 未放宽
Reviewer / truth audit / validate_spec      ✅ 未改
RIDDLE_SYSTEM                                ✅ 未改
safety / 动态 few-shot / Style Card          ✅ 未做
```
