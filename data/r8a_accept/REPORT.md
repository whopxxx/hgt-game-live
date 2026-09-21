# R8-A 验收报告

**分支** `feat/r8a-freegen-policy` · **base** `main @ 9b12f81`
**不做** 伪控制(不跑"红汤 5 + 黑汤 5"), lane 按真实 draw 报告

---

## 一、这一批改了什么

### (a) 自由生成链: 九项门 -> **六项门**

`clue_recontextualized` / `dramatic_payoff` /
`reasoning_beats_nonredundant` 从**硬门**降成 **signal only**。

关键: **不删字段**。它们仍由 Reviewer 回答(索要清单仍是 9 项)、仍写进
`quality_checks`、仍落盘 —— 只是不再拒稿。见
`_FREE_GEN_HARD_CHECK_FIELDS` / `_gate_fields()`。

门里六项一项没少: `narrator_truthful` / `mechanism_consistent` /
`core_answer_direct` / `completion_contract_minimal` /
`concrete_anomaly` / `livestream_safe`。

### (b) safety 口径写硬(**两处同改**)

主审 `livestream_safe` 描述 + 独立 `SAFETY_SYSTEM` 统一成:

```
普通、非血腥的死亡 / 犯罪 / 谋杀 / 亡魂 / 鬼怪 / 恐怖氛围,
即使是谜底核心, 也不因此判 false。

只有明确命中以下边界才 false:
  - 自伤 / 自杀主题或核心动机
  - 性暴力作为核心情节
  - 血腥、具体伤害感官细节
  - 极端伤害本身作为猎奇卖点
  - 儿童 / 家庭严重暴力作为核心冲击点

未明确命中 -> true
```

删掉的泛化措辞(正是它们让模型扩张成"死亡=不安全"):
`"会不会变成拿惨案当乐子"` / `"宁可严一点"` / `"拿不准时判 false"`。

⚠️ 只改一处会让双门 AND 继续卡死 —— 复核放宽了、主审还在拒。

### 版本

```
QUALITY_POLICY_VERSION = quality-v12
SAFETY_PROMPT_VERSION  = safety-v2
CHECK_PROMPT_VERSION   = check-v11
```

`CHECK_PROMPT_VERSION` **也 bump** 了: `livestream_safe` 的描述写在
`_TOOL_CHECK` 里, 而它**就是** Reviewer 的 tool schema —— 属于"Reviewer
prompt / schema 文案真的变了"。不是为凑版本号。

---

## 二、10 次生成: 每次结果与 reject 分类

命令:

```
prefill_pool.py --target 6 --playable 4 --max-attempts 10 \
  --seed 20260921 --keyword-session-seed 20260921
```

| # | 结果 | 分类 | 说明(简) |
|---|---|---|---|
| 1 | gen_fail | **review_rewrite** | 核心机关自相矛盾(洞穴听觉判同伴 vs 救援队) |
| 2 | gen_fail | **truth_reject** | truth audit conflicts 非空 |
| 3 | gen_fail | **review_rewrite** | 修复越权改未授权字段 |
| 4 | gen_fail | **review_rewrite** | 未授权改 `fact.hintable` |
| 5 | gen_fail | **review_rewrite** | 布偶三种功能互斥(藏钱 vs 浸湿) |
| 6 | gen_fail | **structure_technical_fail** | Structurize 连续两次空 tool_input |
| 7 | **入池** | success | lane=black, 49 字 |
| 8 | gen_fail | **validation_reject** | hints 超 30 字, 窄修复没成功 |
| 9 | **入池** | success | lane=red, 72 字 |
| 10 | gen_fail | **validation_reject** | completion fact kind=support |

```
成功入池            2
review_rewrite      4
truth_reject        1
structure_technical 1
validation_reject   2
safety_reject       0   ← 见下
```

### 关键观察 1: `safety_reject` = 0

**这一批没有一道因为 safety 被拒。** 而上一版(v11, 09-21 上午那次 10 次)
有 1 道明确因 safety 被拒(「囚禁他杀且凶手刻意掩盖呼救」)——
那正是新口径下**应当放行**的普通非血腥犯罪。

注意: 0 不等于"门失效"。边界仍然 fail closed, 只是这一批没撞上。

### 关键观察 2: 三项 signal 不再杀人

两道入池题的 `quality_checks` 都是 **`single_trick=false`**:

| 题 | lane | 字数 | 未过项 |
|---|---|---|---|
| 瓜田持枪 | black | 49 | `single_trick` |
| 废弃医院举报信 | red | 72 | `single_trick` |

在 v11 的九项门下这两道**都会被整稿丢掉**。现在它们进来了, 而
`safety` / `narrator_truthful` / `mechanism_consistent` 全过 ——
正是 R8-A 想要的形状: 轻量单点题不再因为"不够戏剧化"被淘汰。

### 关键观察 3: 该 fail closed 的仍然 fail closed

- `truth_reject` 1 次(叙事真实性仍是硬门);
- `review_rewrite` 4 次, 拒因全是**结构性硬伤**: 机关自相矛盾、
  越权修复、功能互斥 —— 不是"不够好玩";
- `structure_technical_fail` 1 次(网关空 tool_input)。

---

## 三、"未知 reject 标签" = 0

Task 0 补的 `safety_reject` / `safety_technical_fail` 桶在真实负载下
生效: 整轮 `grep -c "未知的 reject 标签"` = **0**(v11 那轮会刷)。

---

## 四、没做到目标数(6/4, 实得 2/2)

`max-attempts 10` 打满只成 2 道。**拒因全部是结构性问题, 不是
"普通犯罪被 safety 拒"** —— 所以吞吐瓶颈在**下一环**, 不是本轮改的
这一环。

如实报出, 不改口径去凑数。若下一步要恢复吞吐, 该看的是那 4 次
`review_rewrite` 的结构性硬伤(机关自相矛盾 / 修复越权), 那是另一个
问题域, 不属于 R8-A。

---

## 五、回归

| 套件 | 结果 |
|---|---|
| `test_llm.py` | 3 条**既有**失败(`R4-K2f`×2 / `R7-1`×1, 在 R4 上就已红) |
| `test_engine.py` | 全绿 |
| `test_curated_v2.py` | 全绿 |
| `test_puzzle.py` | 全绿 |
| `test_curated_compile.py` | 全绿 |
| `test_pool` / `test_prefetch` / `test_solve_ux` | 与基线逐项相同 |

新增回归:

- `test_r8a_three_checks_are_signal_only` —— 索要清单仍 9 项 ⊃
  拒稿判据 6 项, 差集恰好是那三项; 门是索要清单的**真子集**(否则会
  拿一个从没被问过的字段拒稿 -> 每道题都拒)。
- `test_r7_main_reviewer_prompt_unchanged` —— **改写**: R7 时它断言
  "主审措辞一字未动", 而 R8-A 是**故意**要动它。现在断言的是
  五条边界仍在 + 泛化措辞已收掉 + 复核仍独立。

版本断言全部随 v12 / safety-v2 / check-v11 更新。

---

## 六、不做的事

- 不改 Story / Surface 创作链
- 不加 Reviewer / 不加评分器
- 不删任何一个 `quality_checks` 字段
- 不做"红汤 5 + 黑汤 5"这种伪控制
- 不为了凑通过率放宽真实性 / 结构 / 安全
