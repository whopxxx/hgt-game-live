# R7 — 独立安全复核(双门 AND)

**base** `main @ f3c239a`(R5 已 squash merge)
**不 merge** · **不混进 PR #9** · **PR #10 (draft)**

---

## 〇、复审修正轮(3 个必修 + rebase)

| # | 问题 | 修法 |
|---|---|---|
| 0 | 分支历史仍挂着旧 R5 commits, diff 夹带 R5 文件 | `rebase --onto f3c239a fd2a001`, 两个 R5 commit 被识别为 already upstream 自动丢弃 |
| 1 | `quality_checks` / `safety_*` 只在 `spec.metrics`, **正式直播 `puzzle.jsonl` 会丢** | 补进 `director._round_metrics()` 白名单 |
| 2 | `safety_verify_calls` 恒记 1, 但技术重试时实际调用 2 次 | `verify_safety` 回传真实 `calls`; 两处调用点照抄 |
| 3 | 技术失败后、重试前直播变忙 -> `interrupted` 被误记成 `safety_technical_fail` | `interrupted` 独立成字段, **优先于** `technical` 被读; gen_spec 走 `break`(让路不是失败, 换稿只会再撞) |

rebase 后 `git diff --stat origin/main...HEAD` **不再含** `prefill_pool.py` /
`data/r5_prefill/*` —— 那是第 0 项的验收。

### ⚠️ rebase 中发现: structure 链的让路曾经是**假绿**

第 3 项在 `gen_spec` 修好后, keyword2 链(`structure_original_idea`)上
**同一个 bug 仍然存在, 且变异测试抓不到**:

```
变异: 删掉 structure 链的 interrupted 分支
      -> 全绿(没有任何行为断言看着它)
```

原因是 `_bail()` 靠 `interrupted["v"]` 决定出口, 而复核自己返回的
`interrupted` **不会自动传进去** —— 必须先 `interrupted["v"] = True`
再 `_bail()`。补了 `[R7-10b]` 用真 `structure_original_idea` 驱动同一条
让路路径后, 该变异变红。**这正是 R7-6 当初警告过的形状**(keyword2 才是
直播真正走的链), 却在 R7-2 自己身上又犯了一次。

## 一、为什么是复核, 不是改判据

R6 对**冻结产物**重跑三次 `check-v10`, 同一份 `puzzle`+`answer`:

```
行 1 (列车上/威胁)   livestream_safe: False / True  / False
行 3 (网络/假发)     livestream_safe: False / True  / 技术失败
```

主审对安全项的判定**本身会抖**。而 `livestream_safe` 是硬门 ——
漏一次, 那道题就上播了。

⚠️ 这个结论的方向很关键: **不是**"判据覆盖不到"(False 出现过, 说明
够得着), 而是"**单次判定不可靠**"。所以修法是加一次独立复核, **不是**
继续堆判据措辞 —— 后者按 R4-R3 的纪律本来也不该做。**这次一行判据
都没动。**

## 二、双门 AND(不是三次多数票)

```
主审 livestream_safe  AND  独立复核 livestream_safe
```

一道 unsafe 题必须**连续被两次独立调用漏判**才可能进池。
比"跑三次完整 Reviewer 取多数"更便宜(复核只看两段文本、只判一项),
也更干净(不做结构/配额/好玩度判定)。

### 顺序

```
Reviewer -> validate -> **safety verify** -> truth audit
```

* 只对**本来要放行**的 candidate 花这次调用;
* 安全没过就**不再发** truth audit —— 省一次调用, 且拒因准确
  (不是"叙事不真实", 是"不适合直播");
* 审的是 **fix 之后**的版本(最终会入池的那份文本)。

### 复核看什么

只看 `puzzle + answer`。**不传** facts / atoms / clues / signature ——
安全判据只依赖文本, 给多了它会开始评论"推理公不公平"(主审判过了),
关注点被摊薄。schema 只要 `livestream_safe: bool` + 一句短 `reason`。

### 技术失败 ≠ 不安全

复核技术失败重试一次; 第二次仍失败则 **fail-closed 不入池**, 但记成
`safety_technical_fail` —— **不是** `livestream_safe=false`。把网关抖动
写成内容判定会让复盘查错方向。与 G2-F「技术失败 ≠ 语义拒绝」同一条纪律。

## 三、版本

| 常量 | 变化 | 理由 |
|---|---|---|
| `QUALITY_POLICY_VERSION` | `quality-v10` → **`quality-v11`** | 接受条件真的变了(见下) |
| `SAFETY_PROMPT_VERSION` | 新增 **`safety-v1`** | 独立开号: 复核措辞会独立演化 |
| `CHECK_PROMPT_VERSION` | **不动** | 主审文案一个字没改 |

bump 的理由(与 v9/v10 都不同, 值得单列):

```
v10: 主审 true                    -> 收
v11: 主审 true **且** 复核 true    -> 收
```

同一份 spec 在 v11 下**可能被拒而 v10 下被收**(复核 false, 或复核技术
失败 fail-closed)。这是**接受结果**变了, 不是"判据说得更细"。严格相等门
会隔离 v10 库存, 否则"只过了单门"的旧题会与新题混池。

**代价: 上线前 prewarm 补池**(与 v9/v10 同型)。

## 四、回归(13 条, 全部变异验证)

| 用例 | 断言 | 变异 → 结果 |
|---|---|---|
| `test_r7_safety_verifier_is_a_second_and_gate` | 双门 AND 四个方向(主审false / 复核false / 双true / 不发多余 audit) | 删 gen_spec 门 → **4 FAIL** |
| `test_r7_safety_technical_fail_is_not_a_safety_reject` | **值非 bool** 也算技术失败, 不伪装 | 强转 bool → **5 FAIL** |
| `test_r7_structure_chain_safety_reject_and_technical_split` | keyword2 链上 false/技术失败**行为层**分开 | 删 false 分支 → **5 FAIL**; 技术失败改记 safety_reject → **2 FAIL** |
| `test_r7_verifier_receives_the_fixed_puzzle` | 复核收到的是 **fix 后**谜面 | 复核挪到 fix 前 → 红(与下条冗余) |
| `test_r7_structure_chain_verifier_runs_after_fix` | 顺序(按**链**定位, 非全文件 index) | 同上 |
| `test_r7_main_reviewer_prompt_unchanged` | 主审九项措辞**未动** | 改措辞 → 红 |
| `test_r7_structure_chain_also_has_the_second_gate` | 两处调用都在 | 只改一处 → 红 |
| `test_r7_safety_verifier_sees_fixed_version_and_narrow_input` | 窄输入(不传 facts/atoms/…) | 传进去 → 红 |
| `test_r7_safety_verify_calls_counts_real_attempts` | 1 次成 / 重试 2 次 / 让路 0 次, **计数必须不同** | 恒记 1 → **FAIL** |
| `test_r7_interrupted_is_not_a_technical_fail` | gen_spec **与** structure 两条链都不许把让路记成技术失败 | 合并回 technical → **2 FAIL**; structure 链不置位 → **FAIL** |
| `test_r7_round_metrics_carries_quality_and_safety_evidence` | 白名单搬运 + 缺省形状 | 删 quality_checks 搬运 → **2 FAIL** |
| `test_r6_*`(4 条) | 上一步的落盘(仍绿) | 见 R6 commit |

**离线套件: 24 套, 与基线逐套件比对 rc / FAIL 数 / traceback 数**
(`test_curated_import` / `test_g4_source` / `test_judge_golden` /
`test_keyword_seed` / `test_pool` / `test_prefetch` / `test_solve_ux`
七套的失败**在 `main @ f3c239a` 上逐项相同**, **非回归**)。

### ⚠️ 两处**既有**假红(不是本轮引入, 记录备查)

`test_llm.py` 的 `[R4-K2f]` 与 `[R7-1]` 各有断言查**日志文本**里有没有
`livestream_safe` / `safety_verifier`, 而那条拒因走的是 `_remember(seen_why)`
侧信道, 只在**重试耗尽**时才由最后一行 `log.warning` 打出来。夹具
`max_attempts=1` 恰好命中, 但 `last.error` 被后一轮覆盖成
`"改稿后仍不合格: ..."` —— 断言拿到空串。

已验证: 这两条在 `fd2a001`(R4)上**就已经红**(那里共 7 条红, R7 后剩 3 条)。
**本轮不修** —— 它们测的是日志措辞而非行为, 修法属于"把断言改成读 metrics"
的独立清理, 混进 PR #10 会让安全门的 diff 不纯。

### ⚠️ 变异测试中发现的**测试自身**问题(已修)

1. **`gen_calls` 过滤器漏了安全复核**。它原本只滤 `emit_truth_audit`;
   新增 `emit_safety_check` 后所有调用计数 +1, 7 条既有用例假红。
   修法与 audit 同型: 加进过滤名单(**过滤 ≠ 允许任意多调**, 过滤后
   仍是精确断言)。
2. **`FakeClient` 缺 `emit_safety_check` 的默认应答**。与 audit 同理,
   复核出现的位置随重试次数变, 按位置取会吃掉后面的队列。加了
   `__safety__` 标记 + 默认放行 —— 既有用例不必集体改队列。
3. **两条断言是"假绿"**:
   * `test_r7_safety_technical_fail_...` 原夹具用 `{"__safety__": True}`
     (**键缺席**), 复核在"键不存在"那支就 `continue`, **永远走不到**
     `isinstance` 检查 —— 变异(强转 bool)照样全绿。改成**键在值错**
     (`"yes"` / `1`), 并补一条"键缺席"的对照。
   * `test_r7_verifier_receives_the_fixed_puzzle` 原用
     `review_ok()`(`decision="pass"`), 而 `pass` 分支里
     `new_p = spec.puzzle` —— 谜面原样不动, "fix 前后两个版本"恰好
     相同, **测不出来**。改用 `review_fix()`。
4. **段定位用了不可靠的边界**。`src.find("\n    def ")` 会**停在注释**
    (`    #: ... def ...`)上, 把方法体截短。改用行首(列 0)的 `def`。

### ⚠️ 一个我**没能**守住的东西(诚实记录)

我一度以为"把 `spec = reviewed` 在 `validate_spec` 两侧挪动"是个漏测,
花了几轮去构造变异。查清之后确认: 那个变换**不改变复核看到的文本**
(两种顺序下 `spec` 都已是 reviewed 版本), 属于**同义变换**, 不是缺口。
把它写成"能守住"才是假保证 —— 所以测试的 docstring 里明确写了不能守。

真正守住"审最终版"的是行为层那条(让 Reviewer 真的改掉谜面)。

## 五、不做的事

* **不改** red/black lane 文案 / Story Prompt / Surface Prompt
* **不改**主审九项判据的任何一个字
* 不加 Reviewer(复核是**窄**复核, 不是第三个内容审核 LLM)
* 不加评分器 / 关键词黑名单
* **不 merge**(等复审)
