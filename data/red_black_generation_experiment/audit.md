# 内容约束审计 —— 当前 production 里有哪些规则在塑造"直白解释题"

审计对象：`main` @ `dd40bac`（已建分支 `experiment/red-black-soup-generation`）。
方法：读代码 + 读实播产物，**不读注释就当结论**；每条规则都给出文件与行号。

---

## 0. 先确认真实生成链（与任务书 §二 的假设对照）

```
bag.draw()                              story/keyword_seed.py:575
    ↓ 恰好 2 个不同词（word1 != word2）
_keywords_prompt()                      story/llm.py:2183
    ↓ "关键词：X，Y\n\n请围绕这几个关键词写一道中文海龟汤。\n直接给出谜面与谜底。"
gen_keyword_idea()                      story/llm.py:4770   ← Stage A
    ↓ **单次** tool call，一次吐 6 个字段
core_truth / observed_clues / event_chain / title / puzzle / answer
    ↓ _keyword_idea_shape_error() 结构 fail-closed（story/llm.py:2497）
keyword_spec()                          story/keyword_seed.py:546
    ↓ **只**把 title/puzzle/answer 交下去
structure_original_idea()               story/llm.py:4867   ← Stage B
    ↓ puzzle/answer **冻结**（代码回填 canonical，spec 里 schema 没有这两字段）
facts / solve_atoms / fair_clues / discovery_beats / signature
    ↓ validate_spec()                   story/quality.py:419
    ↓ Reviewer (pass/fix/rewrite)       story/llm.py:5449
    ↓ validate_spec() 再跑一遍（必须完全干净）
    ↓ truth audit                       story/llm.py:5100 附近
    ↓ cross_puzzle_gate —— **SOFT，只记录不拒稿**
    ↓ _too_similar —— 硬拒（文本 near-duplicate）
pool.add() / pop_next()                 story/pool.py:1177 / 1216
```

**与假设的差异（三条，都重要）：**

1. **`cross_puzzle_gate` 已经不是硬门了**（G4-A 降级）。注释里"配额"听起来很硬，
   实际只写进 `metrics["diversity_signals"]`。池子的 `_passes()` 两遍选择才用它。
   所以**配额的"拒稿力"其实在池子那一层**，不在生成链。
2. **Stage B 之后还有 `_too_similar` 硬拒**（`story/llm.py` ⑤）。这是真正会让
   好题消失的文本近重复门。
3. **`validate_blueprint` 对 free-generation 是死代码**。`structure_original_idea`
   用的是 `_unconstrained_blueprint()` 哨兵，`bp.death=False` 之类全不参与
   （见 §3.1）。

---

## 1. 逐条规则表

格式：`规则 / 所在文件:行 / 当前作用 / 可能副作用 / 建议`

### 1.1 谜面长度

| | |
|---|---|
| **规则** | `PUZZLE_HARD_MAX_LEN = 220`，超出 `r.fail` |
| **文件** | `story/quality.py:404`（常量）、`:433`（判定） |
| **当前作用** | 谜面 > 220 字 → **整稿 fail**（不是 can_fix） |
| **可能副作用** | 220 是一条**真实 UI 合同**吗？见 §4 的前端核查 —— 前端**没有**任何 220 常量，只有 `layout()` 从 58px 起逐级降到 46px 再把溢出交给 AutoScroller。所以 220 是**经验值**，不是画布硬边界。 |
| **建议** | **保留 hard max，但先验证数值**。它是"能不能上屏"的合同，不是质量政策 —— 但它挡住的是**长题面**，而长题面正是"把现场说明写全"的症状。**不要**顺手加"必须 2~3 句"。 |

### 1.2 谜底长度

| | |
|---|---|
| **规则** | `ANSWER_HARD_MAX_LEN = 300` / `ANSWER_PREFERRED_MAX_LEN = 260`；Stage A prompt 里写"建议 2~4 句, 不超过 260 字" |
| **文件** | `story/quality.py:405-406`；`story/llm.py:2395`（prompt）、`:2290-2310`（注释） |
| **当前作用** | >300 fail；260 只是建议 |
| **可能副作用** | ⚠️ **这一条比长度更影响风格**：`2~4 句` 是一个**写作格式**约束，写在 prompt 的"运行约束"里。它和"谜底要简洁"是两件事，但模型会把"2~4 句"当成汤底的**结构上限** —— 于是汤底被压成 4 句以内的平铺解释，**没空间做"揭晓后回头看"的重新定性**。 |
| **建议** | **300 hard max 保留**（上屏合同）。**"2~4 句"放宽或删除** —— 只留字数上限，句数交给创作。 |

### 1.3 谜面人称（第一人称）

| | |
|---|---|
| **规则** | `is_first_person()` → `r.can_fix("谜面是第一人称叙事, 改成第三人称客观事实")` |
| **文件** | `story/puzzle.py:999`（判定）、`story/quality.py:445`（can_fix） |
| **当前作用** | can_fix（不是 fail）→ 交给 Reviewer **必须改掉**（`story/llm.py:5555` 的 `hard` 分支） |
| **⚠️ 真实后果** | 这是**硬改写**，不是建议。`_PUZZLE_TOUCH_MARK` 把它归进"必须动谜面"那一类，Reviewer 不改就 `rewrite` → 整稿丢弃。**所以现在第一人称题 100% 被改或被杀。** |
| **判定细节** | 引号内和括号内的"我"被剔除，只看叙述部分。所以 `他说「我不舒服」` 不算。 |
| **建议** | **放宽**（任务书 §十二-A 的产品方向一致）。改成：第一人称是合法形式；只有当"我"造成**指代歧义**（分不清是主持人还是角色）时才 fix。 |

### 1.4 谜面结尾必须是问句

| | |
|---|---|
| **规则** | `has_closing_question()` → `r.can_fix("谜面结尾不是问句, 末尾补一句'为什么?'")` |
| **文件** | `story/puzzle.py:991`、`story/quality.py:441`、`story/llm.py:5557` |
| **当前作用** | 同 1.3 —— can_fix 实际是**硬改**，改不掉就 rewrite |
| **⚠️ 真实后果** | 「每天晚上十二点，她都会听到自己从门外敲门。」这种**自明其问**的谜面会被强行加尾巴。加了之后反而把问题**说死**了，缩小了观众的提问方向 —— 与"制造问题欲"直接冲突。 |
| **建议** | **放宽**（任务书 §十二-B）。判据从"末字符是问号"改成"谜面是否含**可调查的异常**"。 |

### 1.5 谜面元文本

| | |
|---|---|
| **规则** | `has_meta_text()` → can_fix |
| **文件** | `story/puzzle.py:984` |
| **当前作用** | 谜面里混进【谜底】/【提示】就 fix |
| **建议** | **保留**。这是纯粹的格式污染，与创作方向无关。 |

### 1.6 `fair_clues.quote` 必须逐字出现在谜面

| | |
|---|---|
| **规则** | `quote_in_puzzle()` 包含检查；`validate_spec` 里"fair_clue 缺 quote"/"quote 不在谜面里"/"没有 fair_clue"全部 `fail`（**整稿拒**）；"fair_clues 没有标注 supports_atoms"也 fail；"没有 fair_clue 支持任何 required atom" fail |
| **文件** | `story/quality.py:719-801`（判定）、`story/puzzle.py:967`（quote_in_puzzle）、`story/llm.py:2706`（schema description） |
| **当前作用** | **每题必须至少 1 条 fair_clue，且它的 quote 逐字出自谜面**；且必须有 clue 指向 required atom |
| **⚠️ 这是最可疑的一条** | 它把"公平"定义成"**答案的证据必须已经写在谜面上**"。于是生成器为了通过，必须**在汤面里把可推理的证据写足** —— 这正是"汤面 = 完整案情说明"的**直接来源**。海龟汤的公平其实是"**问得出来**"，不是"**已经写着**"。 |
| **建议** | **放宽**（任务书 §十二-C）：拆成两件事 —— ① 谜面必须有**真实异常锚点**（保留 hard）；② 关键事实可以**通过 QA 获得**，不要求逐字预置。`fair_clue.quote` 从"必须逐字"改成"若给了 quote 则必须逐字"（即 quote 变成**可选证据**而非**必需证据**）。 |

### 1.7 `discovery_beats` 必须 2~4 条

| | |
|---|---|
| **规则** | `MIN_DISCOVERY_BEATS = 2` / `MAX = 4`；`validate_spec` `r.fail` |
| **文件** | `story/quality.py:415-416`、`:835-841`（判定）；`story/llm.py:1953`（Stage B schema `minItems: 2`）、`:6095`（Reviewer 漏回也拒） |
| **当前作用** | **两道门**：Stage B schema 强制 2~4；Reviewer 回稿漏了 beats 也拒 |
| **⚠️ 真实后果** | 单核翻转的经典汤**结构上不可能合格** —— 它只有 1 层。于是生成器要么凑 2 层（伪层次），要么整道被杀。这条与任务书 §四.5 描述完全一致。 |
| **建议** | **改成观察指标**（任务书 §十二-D）：允许 1~4 或允许空；从 `validate_spec` 的 fail 降为 metrics。 |

### 1.8 Reviewer 的"好不好玩"四项 —— 自由生成链是硬门

| | |
|---|---|
| **规则** | `_QUALITY_CHECK_FIELDS[:9]` —— `narrator_truthful / mechanism_consistent / core_answer_direct / completion_contract_minimal / concrete_anomaly / clue_recontextualized / dramatic_payoff / reasoning_beats_nonredundant / livestream_safe`，**任一项非 true 即拒稿** |
| **文件** | `story/llm.py:1678`（字段表）、`:1864`（切片）、`:6019-6027`（fail-closed 判定） |
| **当前作用** | Reviewer 回 `decision=pass` 但任一门项为 false → `bad.append` → **整稿拒**（比 rewrite 更狠：rewrite 至少还能重出） |
| **⚠️ 关键发现** | curated 链在 H4-D1 已把这四项降为**信号**（`_CURATED_SIGNAL_FIELDS`，`story/llm.py:1811`），理由写得很清楚：*"一道单点脑筋急转弯不可能有 `reasoning_beats_nonredundant=true` —— 若它还是硬门, 那种题就被全灭"*。**同样的推理对自由生成链完全成立，但那次只有 curated 被放开。** |
| **建议** | **分成两类处理**（任务书 §四.6）：<br>• `narrator_truthful` / `mechanism_consistent` / `core_answer_direct` / `completion_contract_minimal` / `livestream_safe` → **保留 hard**（正确性 + 安全）<br>• `concrete_anomaly` / `clue_recontextualized` / `dramatic_payoff` / `reasoning_beats_nonredundant` → **降为 soft signal** |

### 1.9 `clue_recontextualized` 绑死 `fair_clue.quote`

| | |
|---|---|
| **规则** | Reviewer 判据字段，schema description 在 `story/llm.py:3272` |
| **当前作用** | 硬门之一 |
| **⚠️ 复合放大** | 它问的是"揭晓后有没有重新理解谜面里的**线索**"，而"线索"这个词在本系统里已经被 1.6 定义成 fair_clue（逐字 quote）。于是这条**进一步**要求汤面预置可被重新解读的逐字证据 —— 与 1.6 叠加。 |
| **建议** | 保留方向，**换绑对象**（任务书 §十二-F）：判"整个场景 / 一句对白 / 人物身份 / 行为意义"是否被重新解释，不绑 quote。 |

### 1.10 `dramatic_payoff` / `reasoning_beats_nonredundant` 作为硬门

| | |
|---|---|
| **建议** | 降为**观察信号**（任务书 §十二-E/G）。理由同上：它们在 curated 侧已被证明会全灭轻量题。 |

### 1.11 Stage A：Case-first 顺序本身

| | |
|---|---|
| **规则** | `KEYWORD_IDEA_SYSTEM`（`story/llm.py:2348`）四步：core_truth → observed_clues → event_chain → title/puzzle/answer |
| **当前作用** | 强制"先想清唯一真相" |
| **⚠️ 副作用** | 不是"先想故事"，是**先想一个可被否证的唯一解释**。而且第 3 步写着 *"如果有一条关键步骤在谜面里完全没有痕迹，回到第 2 步补一条线索"* —— 这是**在 prompt 层面**执行 §1.6 的同一条逻辑：**逼线索进谜面**。第 4 步前的那句"谜面中的关键细节应该来自前面已经想好的现场线索"同理。 |
| **建议** | **保留 Case-first 的顺序**（G9 已验证），但**删掉"补线索进谜面"那两句** —— 它们是把"公平"误解成"预置证据"的同一错误的第三个副本。 |

### 1.12 Stage A：单次调用同时完成创作 + 结构 + 文案

| | |
|---|---|
| **规则** | `_TOOL_KEYWORD_IDEA` 六个必填字段一次调用返回 |
| **文件** | `story/llm.py:2415` |
| **当前作用** | 一次 tool call 里模型要同时：想真相、反推线索、排顺序、写谜面、写谜底 |
| **⚠️ 副作用** | 注意力被 schema 的形状**预先切分**。模型不是"先自由想一个故事"，而是"**按六个格子的形状**想一个能填满六格的故事"。这会让创意的搜索空间一开始就被 schema 收窄 —— 正是任务书 §四.2 怀疑的事。 |
| **建议** | **实验验证**（本轮 B/C 臂就是干这个）。 |

### 1.13 只有 2 个关键词，且权重极低

| | |
|---|---|
| **规则** | `_keywords_prompt()` 给 `关键词：X，Y` + "请围绕这几个关键词"；system 末尾一句"关键词自然融入即可，不必把关键词硬做成机关" |
| **文件** | `story/llm.py:2183`、`:2387` |
| **当前作用** | 关键词在 Case-first 的四步里**没有落点**（`_TOOL_KEYWORD_IDEA` 也没有 keywords 字段） |
| **⚠️ 附带发现** | `_keywords_prompt()` 结尾写的是 **"直接给出谜面与谜底"** —— 这是 v3 时代的指令，与 v4 的"先交 core_truth"**矛盾**。属于上一轮接入时漏改的口径。 |
| **建议** | ① 修 `_keywords_prompt` 与 v4 schema 口径（纯 bug）；② 关键词是否有帮助 → **本轮 C1/C2 对照**回答。 |

### 1.14 长度 / 句数的"preferred"值不参与判定

| | |
|---|---|
| **规则** | `PUZZLE_PREFERRED_MAX_LEN = 180` / `ANSWER_PREFERRED_MAX_LEN = 260` |
| **当前作用** | 只印在 prompt 里，`validate_spec` **不用**它们 |
| **建议** | 保留。它们不是门，只是提示。**不要**把它们升级成门。 |

---

## 2. 我**没有**在 production 里找到的东西（澄清）

任务书 §四 列了一批"可能存在的规则"。逐条核查结果：

| 任务书提到的 | 实际 |
|---|---|
| "谜面必须 2~3 句" | ❌ **不存在**。谜面只有 220 字上限，没有句数门。（句数约束只在**谜底**的 prompt 提示里：`2~4 句`） |
| "必须多个 solve atoms" | ⚠️ `validate_spec(min_atoms=2, max_atoms=4)` 参数存在，但**调用时用默认值**；需在 `quality.py` 里确认是否真的 fail（本轮报告 §5 记录实测） |
| "clue 必须逐字存在于谜面" | ✅ 存在，见 §1.6 |
| "必须多个发现阶段" | ✅ 存在，见 §1.7 |
| "限制悲剧、死亡、过去创伤" | ✅ 存在**两套**：① 配额（`quota_death=2` / `quota_past_trauma=2` / `quota_trauma_ritual=1`，`story/quality.py:999-1003`），但 `cross_puzzle_gate` 已降 soft；② blueprint 硬比对（`:962-975`），**对 free-generation 是死代码**（`_unconstrained_blueprint` 不约束） |
| "限制机制类别" | ⚠️ 同上 —— 配额存在但已 soft |
| "限制 reveal structure" | ⚠️ `quota_straight_explanation = 1`（v8 从 2 收紧到 1）——**这是有意的内容 policy**，且同样走 soft 路径 |
| "dark tone 目标带" | ✅ **存在且是硬的**：`quality_dark_tone_min=5 / max=6`（`story/config.py:552`），在**交付时**（`pop_next` → `check_signature`）会重新判定。所以"诡异/紧张"要占最近 10 题的 50~60%。 |

---

## 3. 两个"配额 vs 拒稿"的真实边界

### 3.1 free-generation 的 blueprint 约束是**死的**

`structure_original_idea` 里：

```python
bp = _unconstrained_blueprint()          # story/llm.py:4982
```

这个哨兵带 `_unconstrained` 标记，`validate_blueprint` 的逐项比对在
`_is_v2_spec` + bp 值全空的情况下不会真的卡住题。**所以**
`death=False → 谜底出现"自杀"就 fail` 那条（`quality.py:971`）在 free 链上
**不生效**。

→ **红汤（允许死亡）在生成阶段其实没有 blueprint 阻挡。** 真正会挡的是
① `livestream_safe` 这个 quality_check；② `expression` 层面的 quota（已 soft）；
③ dark-tone 带（要求诡异/紧张占多数，反而**推着**往黑汤走）。

### 3.2 `_too_similar` 是**真·硬门**

`story/llm.py` ⑤：与 `avoid` 里任一谜面文本近重复 → `_bail`。
`prefetch` 传 `avoid=None/[]`；`live` 传当前窗口。所以：
- 后台补池：**几乎不会**被它挡；
- live 现场生成：会被当前窗口挡。

---

## 4. 前端长度合同的核查（任务书 §十二-H 要求"先验证真实 UI"）

`web/app.js:545-580` 的 `layout()`：

```js
const TOP_MIN = 620, TOP_MAX = 1000, BOTTOM_MIN = 620;
const maxPuzzleH = TOP_MAX - 260;          // 谜面可用高度
let fs = 58;
for (let i = 0; i < 8; i++) {
  ...
  if (el.puzzle.scrollHeight <= maxPuzzleH || fs === 46) break;
  fs = Math.max(46, fs - 3);
}
```

**结论：前端没有任何字符数常量。** 它做的是：从 58px 起降字号，最低 46px，
仍然溢出就交给 AutoScroller。

所以 `PUZZLE_HARD_MAX_LEN = 220` / `ANSWER_HARD_MAX_LEN = 300` 是
**经验值**（在 46px 下大约多少字会超出 `TOP_MAX-260 = 740px` 的高度），
不是从 UI 反推出来的合同。保留它们**合理**（避免必然滚动），但
**"220 是硬合同所以不能动"这个说法不成立于代码**。改动时需要真正量一次
46px 下的行高。

---

## 5. 为什么当前内容显得"直白"—— 代码层面的因果链

把上面几条串起来，形成一个闭环：

```
fair_clue.quote 必须逐字在谜面        (§1.6, hard fail)
        +
"没有 fair_clue 支持 required atom" fail (§1.6)
        +
Stage A 第 3 步"关键步骤没痕迹就回去补线索" (§1.11)
        +
clue_recontextualized 绑定 fair_clue   (§1.9, hard)
        ↓
生成器必须在**谜面**里写足"可被重新解读的逐字证据"
        ↓
谜面从"一个让人想问的切片"退化成"完整的现场说明"
        ↓
谜底只剩一件事可做：**把这些已写出的证据串成一个解释**
        ↓
结果 = "出现一个奇怪行为 → 问为什么 → 一个合理现实原因"
```

**"直白"不是 prompt 措辞的问题，是 `fair_clue` 契约的形状问题。**
它把"公平"实现成了"预置证据"，而预置证据一旦写进谜面，谜面就自动
变成了案情摘要 —— 此时谜底无论怎么写都只能是解释，不可能是**重新定性**。

这是本轮最重要的发现，也是 §1.6 应排在最优先放宽的位置的原因。

---

## 6. 规则影响排序（建议先放宽的顺序）

| # | 规则 | 为什么排这个位置 |
|---|---|---|
| 1 | `fair_clues` 必须逐字预置 + 必须支持 required atom（§1.6） | 直接制造"谜面=案情说明"的闭环源头 |
| 2 | "好不好玩"四项作为自由生成的硬门（§1.8/1.9/1.10） | 全灭单核经典汤；且 curated 侧已有先例证明该降为 soft |
| 3 | `discovery_beats` 强制 2~4（§1.7） | 结构上禁止单层翻转 |
| 4 | 结尾必须问号（§1.4） | 把自明其问的谜面改成缩小问题范围 |
| 5 | 第一人称硬改第三人称（§1.3） | 消灭一整类经典形式 |
| 6 | 谜底 "2~4 句" 写作格式（§1.2） | 压掉汤底的重新定性空间 |
| 7 | Stage A 单次调用承载创作+结构+文案（§1.12） | 结构性收窄创意搜索空间（本轮实验验证） |

---

## 7. 本轮**不改** production

以上全部是审计结论 + 建议。代码改动只有 `tools/` 下的实验脚本与
`data/red_black_generation_experiment/` 下的产物。
