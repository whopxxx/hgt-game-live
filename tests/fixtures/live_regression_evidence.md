# Live regression evidence — provenance 与证据边界

本文件记录 `tests/fixtures/live_quality_regressions.jsonl` 里那条 fixture 的
**来源、冻结哈希、以及四类候选各自的证据强度**。

它的作用是让将来读 fixture 的人不必相信任何人的转述 —— 每一处断言都能
回到下面这五个文件的具体行。

---

## 1. 冻结的源文件

调查开始前先把当前 `data/` 里的运行材料复制到仓库外（`/tmp/hgt-step00-evidence/`），
因为 `data/run.log` 使用**固定文件名**，下一次启动会覆盖它。

| 文件 | 字节数 | 行数 | sha256 |
|---|---:|---:|---|
| `data/run.log` | 3863363 | 15003 | `799d3a418b5771c6e40dcfd0d1585b39f0343f4925a07f3bc00accec53751fe7` |
| `data/puzzle.jsonl` | 775643 | 118 | `d68f145539b93f80a401b001be239e376a732463a0d9c4ffd70cd485fc397155` |
| `data/danmaku.jsonl` | 673732 | 4524 | `e0c6b5bfb4405b894cbd874437b7e367277cf5ac30be873041a4fc65b0c2d96a` |
| `data/最近一次直播.md` | 107763 | 1308 | `71f657992a0bc6c3474b149f0847123d9e497683f3ee4ed463949ca349577816` |
| `data/最近20题.md` | 26263 | 315 | `9242960d2241d04b6d1d8901e6759cb483d6eb7a06339d9a0d2852effb1cb849` |

这些原始文件**不入库**（`data/*.log` / `data/*.jsonl` 已被 `.gitignore` 忽略，
且含观众昵称）。fixture 里保存的是**可复核的摘录**加上哈希。

---

## 2. session 对应关系（怎么确认这些材料属于同一场）

`data/run.log` 是一个**追加**文件，里面含 **8 次独立启动**
（13:04 / 13:22 / 13:24 / 13:26 / 13:45 / 17:33 / 17:34 / 18:18）。
**不能跨段拼证据。**

`puzzle.jsonl` 有 `session` 字段，但 `run.log` **没有 session id**。
两者靠**谜面指纹**锚定：

```text
puzzle.jsonl  session d3da7ed30aa142e692fb1ddff49a146e   行 106–118  （13 题）
      ↕  逐题谜面文字匹配
run.log       行 1140–15003
      ↕
时间窗        2026-09-18 18:18:17 → 20:35:01
```

13 道题的 `第 N 题就位` 行号与时间逐题对上，例如：

```text
idx= 1  run.log 行  1149  18:18:17   两个好友约好打一场乒乓球…
idx= 7  run.log 行  7658  19:22:41   教堂晨祷刚散场，母亲把儿…
idx=13  run.log 行 14255  20:31:05   小区里新搬来的护士小吴…
```

**只有这一个 session 带完整 `PuzzleSpec`**（`facts` / `solve_atoms` /
`fair_clues` / `signature`，`quality_policy_version = quality-v3`）。
`puzzle.jsonl` 其余 105 条是 legacy（只有 `puzzle` / `answer`，无 facts）。

---

## 3. 四类候选的原始结论 — 全部 insufficient

任务书 §20 Step 00 列了四类。实际勘察后，**这四类均没有达到 deterministic
fixture 所需的证据标准，因此全部标记为 `insufficient`**，没有一类被固化。

调查过程中另发现一条证据充分的真实缺陷（`canonical_fact_conflict`，
idx=7 / qid=665），它**不属于原任务书的四类**，见 §4。

以下是四类各自的 insufficient 理由。

### 3.1 `timeline_contradiction` → **insufficient**

qid=665 表面上涉及"钟表时间"，容易被归到这一类。**不能这么标。**

这条案例的真实性质是：观众断言「钟因**故障**走快后才停」，
而 canonical `f1` 明确「被管风琴师**人为拨快**」。
两者冲突的是**谓词/成因**（故障 vs 人为），不是时间顺序或时间事实的前后矛盾。

把它标成 `timeline_contradiction` 是分类错误，会让将来读 fixture 的人
去找"两条 QA 对同一时刻给出互斥回答"——那在这条数据里并不存在。

### 3.2 `role_mapping_drift` → **insufficient**

idx=4（地铁让座）有两处原始 Answer/verdict I/O，comment 分别为：

```text
run.log:4093  "方向仍是上下级，反了"      （qid=286，verdict=不是）
run.log:4791  "上下级关系说反了"          （qid=362，verdict=不是）
```

comment 暗示"存在一个正确的关系方向"，但该题 canonical facts（f1–f6）里
**没有任何"上下级 / 面试官-候选人"的关系条目** —— 只有
"此前见过照片或本人"（f1）、"不说话是因为开口就要表明身份"（f3）。

**判"漂移"需要一个正确的锚点**才谈得上"前后不一致"。facts 里没有这个锚点，
所以无法构造可判定的 invariant。仅有 comment 措辞可疑，不足以做 fixture。

### 3.3 `action_continuity` → **insufficient**

唯一候选是 idx=3 qid=174：

```text
puzzle.jsonl:108  qid=174  status=unavailable  verdict=未判定  touched_fact_ids=[]
```

它**没有真实裁决**（网络故障，走的是 fail-fast 的"未判定"），
所以不存在"对同一动作是否发生过给出不兼容回答"的成对样本。

其余含时序词的 QA 经逐条检查都是同一方向的一致回答。

### 3.4 `canonical_parallel_story` → **insufficient**

当前冻结日志确认有大量完整的 **Answer/verdict I/O**：

```text
1408 行含【事实表(判定依据)】(Answer 阶段的输入)
1407 行含 结果={"answers": …}  (Answer 阶段的输出, 对应 _TOOL_ANSWER)
```

但这**并不能自动证明存在完整 Final Judge I/O**。两者是两个不同的模型阶段
（详见 §6），schema 也不同。

本次**没有找到**一条证据充分的案例，能够证明 **Final Judge**
接受了一整套与 canonical facts 冲突的平行解释并判 SOLVE。
qid=665 只是一个**局部** Answer 阶段的谓词冲突，不是"一整套平行故事被接受"，
并且它 `solution_candidate=false`，**根本没有进入 Final Judge**。

因此 `canonical_parallel_story` 仍为 `insufficient`，
**不创建**旧任务书列出的 `tests/fixtures/live_judge_regressions.jsonl`。


---

## 4. 实际固化的类别

### 4.1 `canonical_fact_conflict` → **sufficient**

```text
session:       d3da7ed30aa142e692fb1ddff49a146e
puzzle_index:  7
qid:           665
puzzle.jsonl:  行 112
run.log:       行 7795–7802
```

原始证据链（全部来自冻结文件）：

```text
run.log:7795  弹幕收下[提问 #665] 晚来: 钟出故障走快了之后再停止吗
run.log:7797  user=【谜面】…【事实表(判定依据)】…（证明 facts 真的进了 Answer 阶段的输入）
run.log:7799  结果={"answers": [{"id": 1, "verdict": "是",
                                "solution_candidate": false,
                                "touched_fact_ids": ["f1", "f2"],
                                "comment": "快了二十分钟，但不是故障"}]}
run.log:7800  点评泄露谜底, 已丢弃: '快了二十分钟，但不是故障'
run.log:7801  裁决 '钟出故障走快了之后再停止吗' -> 是 (碰事实=['f1', 'f2'] 候选=False)
run.log:7802  答 '钟出故障走快了之后再停止吗' -> 1.4s 是
```

注意 7799 的 schema 是 `{"answers": [...]}`，对应 `_TOOL_ANSWER`
（`emit_verdict`），即 **Answer/verdict 阶段**，**不是** Final Judge。
该条 `solution_candidate=false`，因此本题**从未进入 Final Judge**
（判定门在 `story/llm.py` 的 `if not judge_solve or not answer or
not r0.solution_candidate: return`），run.log 行 7795–7802 区间内
也**没有任何 `emit_judgement` 行**。

**为什么 expected verdict 是「不是」**：

`f1` 写的是「教堂墙上那口钟的指针被管风琴师**人为拨快**过二十分钟」。
观众问的是「钟出**故障**走快了之后再停止吗」。
「故障」（机械自行失准）与「人为拨快」互斥 —— 不是同一件事。
提问把成因说反了，所以整个断言不能判「是」；按 canonical 应为「不是」。

### 4.2 `verdict_comment_masking` → **sufficient，但它是同一个 fixture 的第二 invariant**

**不要复制成第二条 fixture。** 它是同一份原始 I/O 的另一半：

当前 `story/llm.py` 的 answer 后处理在 comment 命中 `_leaks_answer()` 时
**清空 comment，但保留 verdict**。于是：

```text
模型原始输出: {"verdict": "是", "comment": "快了二十分钟，但不是故障"}
             ↓  comment 被判为泄露谜底
落库/上屏:    {"verdict": "是", "comment": ""}
```

模型自己写出了正确诊断（「但不是故障」），观众却只看到「是」。

这一点记录在 fixture 的 `expected_invariants.corrective_comment_must_not_mask_wrong_verdict`。

---

## 5. 观察统计（**不是测试阈值**）

冻结日志里 `点评泄露谜底, 已丢弃` 共出现 **654 次**：

```text
verdict 分布:  不是 302 ／ 是 237 ／ 无关 115
```

> ⚠️ 这是**当前这一份冻结日志的观察值**，不是代码常量，也不得写成测试阈值。
> 它只说明这个后处理路径在这 8 小时里被触发了 654 次、不是罕见分支。

---

## 6. 两个模型阶段必须严格区分（Answer/verdict ≠ Final Judge）

这是本文档最容易读错的地方，单独列一节。

`PuzzleWriter.answer()` 内部其实是**两层**，用不同的 system prompt、不同的
工具、不同的 schema、不同的日志标签：

| | Answer/verdict 阶段 | Final Judge 阶段 |
|---|---|---|
| 入口 | 每条提问都走 | **只有 `solution_candidate=true` 才走** |
| 代码 | `story/llm.py` `answer()` | `story/llm.py` `judge()` |
| system | `ANSWER_SYSTEM`（503 字） | `JUDGE_SYSTEM`（1349 字） |
| 工具 | `_TOOL_ANSWER` = `emit_verdict` | `_TOOL_JUDGE` = `emit_judgement` |
| 输出 schema | `{"answers":[{"id","verdict","solution_candidate","touched_fact_ids","comment"}]}` | `{"is_guess","cause_hit","mechanism_hit","key_fact_hit","matched_atoms"}` |
| 日志标签 | `裁决 '…' -> 是/不是/无关` | `裁判 '…' -> 未中/命中` |
| 日志行样例 | 7801 | 418 |

因此：

- `结果={"answers": …}` 是 **Answer/verdict 输出**，**不是** Final Judge 输出。
- `【事实表(判定依据)】` 是 **Answer/verdict 输入**（`ANSWER_SYSTEM` 里
  "依据【事实表】判断提问"），**不是** Final Judge 输入。
- Final Judge 的输入是 `【谜面】/【谜底】/【要说到的事实(编号从 0 开始…)】/观众的提问`，
  **不含** `【事实表(判定依据)】` 这个标题。

> ⚠️ 这一点直接关系到后续 Step 05/07，因为那两步要专门验证
> 「**Final Judge 是否真正把 facts 当 canonical world**」。
> 如果在这里把 Answer 阶段误当成 Final Judge，那两步的基线判断就会建立
> 在一个错误的"我以为 Judge 已经看过 facts"的前提上。

本次固化的 qid=665 属于 **Answer/verdict 阶段**（`stage: "answer"`），
其 `solution_candidate=false`，**没有** Final Judge 参与。

---

## 7. 本次未做的事

- 未修改 `story/*.py` / `director.py` / Prompt / Answer / Judge / Engine。
- 未创建 synthetic 的 role / timeline / action / parallel-story fixture。
- 未提交 `run.log` / `puzzle.jsonl` / `danmaku.jsonl`。
- 未新增可执行 regression test（fixture 由 Step 05 消费）。
