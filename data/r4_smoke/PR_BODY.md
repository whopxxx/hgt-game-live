# R4 — 生成链拆成 Story → Surface → Structure

**base** `main @ 62b069b` · **不 merge**（等复审）

> 📌 这份 body 由 `7db6859` 时的仓库实际状态重新生成。之前几版里的版本号
> （`keyword2-v5 / check-v9 / quality-v9`）与 smoke 分布都已过期。

## 目标链

```
KeywordBag 抽 2 个随机关键词
        + lane (red/black, 由 (session_seed, draw_index) 无状态派生)
              ↓
[Story]     只生成完整隐藏汤底          schema: {answer}
              ↓
[Surface]   从汤底单独截一个反常瞬间      schema: {puzzle}
              ↓
[Structure] 当前 PuzzleSpec 结构化        ← 基本不动
              ↓
           直播 QA
```

`observed_clues` / `event_chain` **彻底退出创作链** —— R2 实测 puzzle 在进入
Stage B 之前就已经 78~136 字，而 Stage B 冻结 puzzle，所以不是它写长的。

## 当前版本（**以仓库实际值为准**）

| 常量 | 值 |
|---|---|
| `STORY_PROMPT_VERSION` | **`keyword2-v7`** |
| `SURFACE_PROMPT_VERSION` | **`surface-v2`** |
| `CHECK_PROMPT_VERSION` | **`check-v10`** |
| `QUALITY_POLICY_VERSION` | **`quality-v10`** |

判断"要不要 bump"只有一条标准：**同一份 spec 在新旧两版下收不收会不会不一样。**
会，就 bump。

## 本轮（R4-R4）改了什么

复审评论 `5753721684`：**Surface 仍然会泄底。**

### 一、Surface 判据从否定改成正向

被点名的 61 字汤面：

```
村中世代相传的"祭祖"，其实是把活人送进后山溶洞喂怪物。……
```

而汤底核心答案就是这句 —— 短是短了，谜底已讲掉大半，Stage B 还放行了。

**为什么旧的"不解释原因"没用**：那是个**否定句**，只否掉了显式的因果连接词
（因为/所以）。模型于是不写"因为"，但照样把真相**作为陈述说出来**（"其实是…"）。
否定句约束不了它没提到的那些写法。

改成复审给的正向判据：

```
只写角色当时能看到、听到、知道的表面事实；把"为什么如此"的真相全部藏起来。
```

正向判据是**可执行**的：每写一句都能自问"这是角色当场感知到的，还是叙述者
知道的真相？"后一类不写。`SURFACE_SYSTEM` 与 `_TOOL_SURFACE` 的 description
**两处同步改** —— schema 也是给模型的指令，只改一处会让模型收到两个不一致的判据。

按复审要求：**不加禁止模板、不加 Reviewer、不加评分器。**

### 二、修一处自相矛盾的过期注释

`story/llm.py` 里 G3 那段注释写着 "`QUALITY_POLICY_VERSION` **保持 quality-v8**"，
而实际已是 `quality-v10`，且 R4/R4-R3 确实属于"接受结果变化"。现在把它**明确
标为历史**（G3 当时的结论与理由保留以便复盘），并在前面写清当前状态与判断标准。

## 前几轮（R4 / R4-R1 / R4-R2 / R4-R3）已并入

- **R4** 三段式落地；删掉"谜面必须有结尾问句"整条旧契约；`core_answer` 语义
  改成"解释核心异常"。
- **R4-R1** lane 由 `(session_seed, draw_index)` 无状态派生（修掉"整场只出一个
  lane"的生产 bug）；provenance 进正式 archive；失败样本保留 Story/Surface 原文。
- **R4-R2** smoke 双抽关键词的测量 bug（`_one()` 与 `keyword_spec()` 各抽一次，
  报告上的关键词不是实际生成用的那组，还白跳一组词）；Story 中心目标从"危险/
  冲击"换成"表面反常、背景讲得通"。
- **R4-R3** Story 安全边界；`livestream_safe` 判据逐条写名三类情形（**三处**措辞
  同步，第三处是从源码 AST 还原真文本再断言）。

## 测试

新增 4 条，全部变异验证（改坏必红）：

| 测试 | 变异向量 |
|---|---|
| `test_r4r4_surface_hides_the_why` | 改回"不解释原因" → 4 FAIL；删掉"藏真相" → 2 FAIL；schema 改回旧的 → 1 FAIL |
| `test_r4r3_livestream_safe_names_the_three_cases` | 三处措辞各改回旧概括 → 各 FAIL |
| `test_r4r3_livestream_safe_false_rejects_free_gen` | 门里摘掉该项 / 方向翻转 → FAIL |
| `test_r4_smoke_draws_keywords_exactly_once` | 重新引入双抽 → FAIL |

**22 个离线套件：21 绿 + 2 个基线就有的 GBK 崩溃**（`test_solve_ux` 的
`U+2286`、`test_curated_import` 的 `U+00A9`；已 stash 到基线逐字节比对，
异常签名完全相同，**非回归**）。

## 小样本 smoke：**10 draws，4/10**

口径是 **10 draws（两组 seed 各 5 次，不重抽 / 不评分 / 不排名）**。lane 由
`(session_seed, draw_index)` 无状态派生，**与组名无关** —— 每组内部红黑混出。

与 `data/r4_smoke/report.md` 一致的分布：

| 结果 | 条数 |
|---|---|
| 成题 | **4** |
| `truth_reject` | 2 |
| `review_rewrite` | 2 |
| `validation_reject` | 1 |
| `structure_technical_fail` | 1 |

成题汤面字数：**43 / 66 / 86 / 48**。

### 泄底有没有好转

**这批 10 道汤面的"把真相说出来"标记命中 0 次**（扫描：其实是 / 真相是 /
原来 / 之所以 / 因为 / 导致 / 是为了 / 目的是）。

被点名的那道（同关键词 `古村落 / 恐怖`）本次写成：

```
他终于回到阔别多年的偏远村落探亲。村口几个孩子在玩闹，他注意到每双小手上
都多出一根手指——六指。村民路过时只是笑着摸摸孩子的头，仿佛这是寻常之事。
夜里他翻开泛黄的族谱，越看越冷，推门想逃，族长站在门外，目光落在他右手上，
平静地说了一句话。
```

通篇是主角**当场能看到**的，真相（近亲繁殖）一个字没说。它这次被判
`truth_reject`，**没有被放行**。

> ⚠️ **不要把这段读成"同一道题的 A/B"。** 这是**另一稿**（另一个故事），
> 不是同一个故事的两种截法。它说明的是"**这个失败模式在这一批里没再出现**"，
> 不是"改 prompt 让那道题变好了"。10 条样本不足以给因果。

### 仍然存在的噪声

**6 条未成题**，其中：

| 拒因 | 条数 |
|---|---|
| `truth_reject` | 2 |
| `review_rewrite` | 2 |
| `validation_reject` | 1 |
| `structure_technical_fail` | 1 |

也就是说 **6 条里只有 1 条是网关技术抖动**（空 `tool_input` / 结构调用失败），
**其余 5 条都是内容判定**。这个抖动在 R2/R3 也出现过，不是本轮引入的。

> ⚠️ 但要注意这 5 条内容判定**不等于**"这 5 道题都该拒" —— 它们里的
> `truth_audit_ok` 是 `None`，即**根本没走到审稿**就被挡下了（见下）。
> 所以本批**没有**验证到 `livestream_safe` 的线上判定行为；那条门本轮的
> 保证完全来自离线回归（它走完整 `gen_spec`，含审稿与 truth audit）。

## 部署代价（不要让它悄悄发生）

`story/pool.py` 的准入是 `spec_policy == QUALITY_POLICY_VERSION` 的**严格相等**。
`quality-v8 → v9 → v10` 的两次 bump 会让盘上旧版库存**自动失去 live
eligibility** —— 不迁移、不删除（行仍在 `pool.jsonl`，只是被挡住）。

**上线前需要一次 prewarm 重新补池。** v9→v10 这次比上一次更紧迫：旧库存里
可能**真的**含有不该上播的题。

## 不做的事

- 不 merge PR #4 / #6
- 不加新 Reviewer / 评分器 / 套路黑名单
- 不做自动重抽（失败照实记）
