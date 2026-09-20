# G3 —— keyword2 关键词改用真实 haiguitang input + Stage A prompt 收敛

**基线 main** `ea67b0936b15bef9bebc90220f3c0e8c7e27ae8c`（G2 已通过）
**commit** `c14dc74`
**生产链改动** 新增 `story/keyword_corpus.py`、`tools/build_keyword_seed_corpus.py`、
`data/keyword2_seed_pairs.json`（checked-in）；改 `story/keyword_seed.py`、
`story/llm.py`（只改 Stage A 的 prompt 与版本号）、`story/prefetch.py`、`story/config.py`
**未改** `RIDDLE_SYSTEM`（live 用）/ Stage B schema / Reviewer / truth audit /
`validate_spec` / `cross_puzzle_gate` / curated 链 / live generation 路径 /
`QUALITY_POLICY_VERSION` / `RIDDLE_PROMPT_VERSION`

---

## 一、两个剩余偏差都解决了

| # | 偏差 | 这一轮的做法 |
|---|---|---|
| **A** | 关键词来自我们手写的 5×20 词库 | 改成**真实 haiguitang `input`**，离线构建成 checked-in corpus |
| **B** | Stage A prompt 由我们过度规定写作形状 | 收敛到 haiguitang 原始生成口径，只留运行约束 |

**质量门一条都没放宽**，改的只是"候选怎么想出来"。

---

## 二、corpus 构建统计（§交付）

```
source         neurostellar/haiguitang          (只读 input 字段)
raw_rows       3729
two_key_rows   1113      <- 恰好 2 个有效关键词的记录
unique_pairs    301      <- 归一后去重
corpus_version keyword2-seeds-v2
```

**原始件不在版本库里**（`data_external/` 是 gitignore 的，5MB 外部数据）。
所以拆成两步：`tools/build_keyword_seed_corpus.py` **离线**构建（读本地
已下载的 `turtle.json`，**不联网**）→ 产物 `data/keyword2_seed_pairs.json`
**进版本库**，生产只读它。

### 为什么是 1113 → 301

原数据 3729 行里 input 的关键词个数分布是 0:353 / 1:816 / **2:1005** /
3:1368 / 4+:193。2-key 那批经清洗后得 1113 条，归一化去重后 301 对。

**只保留恰好 2-key**：3-key 的记录**不是**"取前两个"—— 那会造出原数据
里不存在的 pair。§十 有专门一条守它（`a` 与 `c` 都必须不在结果里）。

**不按出现频率重复存**：`pairs` 是 unique 的。任务书原话"原数据负责提供
自然关键词空间，我们不继承它的高频词偏置"——存成有权重的表，采样就会
偏向原数据里被反复生成的组合。`two_key_rows` 与 `unique_pairs` 的**差值
是信息**，所以两个都记。

### 确定性清洗（零 LLM）

```
统一中英文逗号 / 顿号 / 分号 / 空白（含全角空格、不换行空格、制表符）
去掉 `关键词：` 前缀
长度 [1, 12]        —— 上界 12 挡"整段谜面误填"（原数据最长 17 字）
字符白名单 [汉字 字母 数字] —— 挡标点垃圾（`他为什么死了？`/`??`）
敏感词表（确定性，不调 LLM）
```

下界是 **1**（原数据里 `110`、`b` 是真实 seed），上界是 **12** 而不是
更松：实测被长度挡掉的 138 个片段长这样 ——

```
17 一位女士去鞋店里买了一双红色高跟鞋
19 一名男子A请另一名男子B签上名字在纸上
24 五人同时到达目的地。加快脚步的四人被淋成了落汤鸡
```

那不是关键词，是误填进 `input` 的谜面。

---

## 三、固定 seed 的前 10 个 pair（§交付，只展示关键词）

`quality_seed = 20260920` → `session_seed = 14611685660579076342`

```
[01] 吵架，广场舞        [06] 桥，跳
[02] 奶奶，橘子          [07] 女明星，毁容
[03] 死亡，闪光灯        [08] 无言，睡觉
[04] 女朋友，水草        [09] 保龄球，医院
[05] 烦心，结婚          [10] 劫匪，旅行团
```

对照 G1/G2 的人工词库（`阳台/司机`、`上楼/老人`、`开车/地下室`…），
这一批的**语感明显是外部题库的**：`死亡/闪光灯`、`桥/跳`、`女明星/毁容`
都是同语义场或叙事性组合 —— 人工词库因为"两词必须来自不同 slot"
**产不出**这类 pair。这正是 §三 要的效果。

复现：

```
uv run tools/experiment_keyword_riddles.py --draw-corpus 10 --seed 20260920
```

（它直接调生产的 `KeywordBag`，不是副本 —— §九）。

---

## 四、抽样：shuffled bag（§三）

```
读 unique 2-key pairs -> 独立 keyword RNG shuffle
  -> 顺序消费 -> 一个 bag 用完之前同一个 pair 不重复
  -> 用完后重新 shuffle 下一轮
```

**为什么必须换掉 `rng.choice()`**：有放回抽样的真实观感是**会重复** ——
301 对里抽 20 次，撞一次的概率约 50%（生日问题）。直播里连着两道题拿到
同一对词是肉眼可见的尴尬。

**洗牌时刻意不避开上一轮的末尾**：把上轮最后一个 pair 强制挪到首位能防
"跨轮重复"，但会破坏**均匀性**，而 §三 要的是"近似均匀采样"。301 对的
表里跨轮撞同一个 pair 的概率是 1/301，不值得拿均匀性去换。

**取消"两个词必须来自不同 slot"** —— 那是 G1 实验人为加的结构，不是
haiguitang 的 seed 分布。G3 起以真实 source pair 为准。`KeywordBag.draw()`
返回的 `slots` 恒为 `[]`（留着这个 key 只为调用方与日志形状不变，不暗示
它还有意义）。

### session seed（§四）

```
给了 keyword_session_seed  -> 直接用
只给了 quality_seed        -> derive_session_seed() 确定性派生
两个都没给                 -> 启动随机一次, 并写进 INFO 日志
```

第三条是刻意的：随机 session seed 只在**启动时**取一次然后落日志，
于是"这一场直播的 pair 序列"事后永远可复现（从日志抄那个数就能重放）。
若每次抽词都 random，复盘时就没有锚点。

派生用 SplitMix64 风格的混合，**不是 `hash()`** —— `hash()` 对 str 有
PYTHONHASHSEED 随机化，跨进程不可复现，正好毁掉这里要的东西。

**三方隔离**：live 出题用 `quality_seed`，classic 补池用
`quality_seed ^ 0x9E3779B9`，keyword 采样再走一层 `derive_session_seed`。
K18 断言 keyword 的派生值**不等于** director 那个 pf_seed，也不等于
quality_seed 本身。

启动日志实测（装配冒烟）：

```
INFO story.prefetch: keyword2 bag 就绪:
  keyword2 session_seed=8615535409869505717 corpus_version=keyword2-seeds-v2 pair_count=301
```

---

## 五、corpus 缺失 = 显式降级（§五）

```
缺失 / 空 / 解析失败 / 全无效
  -> load_corpus 抛 CorpusError
  -> _init_keyword_bag 打 ERROR, bag 留 None
  -> _keyword_enabled() 返回 False
  -> 整条 keyword2 链让位给 classic Blueprint 链
```

### 降级实测（交付项）

删掉 `data/keyword2_seed_pairs.json` 后跑装配冒烟：

```
exit=124    Tracebacks: 0    "题就位": 1
ERROR story.prefetch: keyword2 corpus 不可用, **本次配置整条 keyword2 链
让位给 classic Blueprint 链**(不会回退人工词库):
CorpusError: corpus 文件不存在: data\keyword2_seed_pairs.json
```

**题照样就位**（降级不是罢工），但走的是 `gen_spec`。`config.validate()`
在显式指了不存在的 corpus 时也会多出一条 warning。

### 为什么"不偷偷回退人工词库"要用三层挡

这是 §五 点名的形状，而且它**在行为层看不出来** —— 题照样进池，只是词
的来源偷偷变了。所以：

1. **行为层**：corpus 缺失时 keyword 路径零调用（G3-1）；
2. **结构层**：`_keyword_enabled()` 只看 bag，不看"有没有词库"；
   手动把 bag 置空，即使词库完好也立刻走 classic（G3-2）；
3. **源码层**：`story/prefetch.py` 与 `story/keyword_corpus.py` 里
   **按 AST 查**不得出现 `KEYWORD_BANK` / `draw_two_keywords` 的**引用**
   （K19 / G3-2）。M7 变异（在降级分支里塞一段人工词兜底）-> **红 ×5**。

---

## 六、Stage A prompt 收敛（§六）

`keyword2-v1` → `keyword2-v2`。

### 删掉的（全是**创作形状**硬约束）

```
第三人称 / 1~3 句 / 单机关也可以 / 不为显得高级增加第二机关 /
不要求复杂人物背景, 不要求职业设定 / 不要求悲剧 /
谜面结尾要是一个问句 / 不要套模板
```

这些不是数据集的形状，是**我们的口味**。G1 拿它们做单变量对照是有意义
的；接进生产之后作用就反过来了 —— 变成"我们在教模型写我们想要的题"。

### 保留的核心语义（逐字来自任务书 §六）

> 你是一个中文海龟汤故事生成器。根据给定关键词创造一道海龟汤：谜面应
> 简洁、有悬念或明显的意外/反常，给玩家留下可以提问探索的空间；谜底必须
> 逻辑自洽，能够解释谜面中的悬念与异常。关键词要自然融入情境。可以有
> 自然产生的意外转折，但不要为了数量硬塞额外转折。

### 保留的运行约束（丢了会出直播事故）

全程中文 / 不依赖冷门专业知识 / 不依赖外部图片音频软件 / 适合普通直播
场景 / 输出只有 title-puzzle-answer。

**不提**：facts / atoms / completion / discovery_beats / signature /
Blueprint / quota / recent —— 全部继续交给 Stage B / Reviewer / audit。

---

## 七、版本（§八）

| 常量 | 值 | 说明 |
|---|---|---|
| `KEYWORD_SEED_VERSION` | `keyword2-seeds-v2` | **词源换了**（人工词库 → 真 corpus） |
| `KEYWORD_BANK_VERSION` | `keyword2-v1` | 人工词库的**历史号**，G1 数据按它抽 |
| `KEYWORD_IDEA_PROMPT_VERSION` | `keyword2-v2` | Stage A 改了 |
| `QUALITY_POLICY_VERSION` | **未 bump** | 接受标准一个字没改 |
| `RIDDLE_PROMPT_VERSION` | **未 bump** | live 还在用它 |

三条链仍可从日志/archive 区分：

| 链 | `prompt_version` | `metrics.generation_mode` | `source_type` |
|---|---|---|---|
| classic blueprint AI | `riddle-v9` | （无） | 空 |
| **keyword2 AI** | **`keyword2-v2`** | **`keyword2`** | **空** |
| curated | `curated-v1` | （无） | `curated` |

metrics 追加可追溯字段：`keyword_seed_version` / `keyword_corpus_version` /
`keyword_session_seed`（连同 G2 的 `keywords`）。**只进 metrics/archive/
日志，不进前端**。

---

## 八、回归（§十 的 13 条）

| 要求 | 落在 |
|---|---|
| corpus 只从 input 提词，不用 puzzle/answer 选题 | `K12`（换掉全部 output，pair 表必须逐位不变；再反向往 output 里塞关键词也不得被采到） |
| 只保留合法 2-key | `K13`（3-key **不取前两个**） |
| pair 去重 | `K14`（顺序反过来的同一对只留一份；全角/半角不算不同 pair；产物无频率字段） |
| 固定 session seed 顺序可复现 | `K16` + `G3-5`（端到端，跑两次比对 `keyword_calls`） |
| 一个 bag 耗尽前 pair 不重复 | `K16`（4 对的表发 4 次全不同；第 5 次自动重洗、round 前进） |
| corpus 缺失不静默退回人工词库 | `K19` + `G3-1/2/3`（行为 + 结构 + AST 三层） |
| keyword2-v2 prompt 不再含旧硬约束 | `G3-K1`（8 条逐条查） / `G3-K2`（运行约束必须还在） / `G3-K3`（不提 Stage B 词汇） |
| Stage A schema 仍只有 title/puzzle/answer | `G3-K4` |
| Stage B 仍冻结 canonical puzzle/answer | `G3-K5`（模型硬塞 `puzzle` 也无效） |
| generated hard quality gates 全部不变 | `G3-K6`（两个版本号未 bump + 空壳 spec 仍被拒 + 仍是 generated） |
| live generation 不走 keyword2 | `G2-12`（行为层 + `director.py` 源码层双重断言） |
| curated 不受影响 | `test_curated_compile` / `test_lazy_curator` / `test_curated_v2` 全绿 |
| `--no-keyword-seed` classic path 不变 | `G2-10` / `G2-11`（逐位比对参数形状） |

新增测试：`test_keyword_seed.py` K12~K22（11 条）、`test_prefetch.py`
G3-1~G3-6（6 条）、`test_llm.py` G3-K1~K6（6 条）。**全部登记进各自的
`main()`**（不登记 = 静默不跑，这是本仓测试的既有陷阱）。

---

## 九、变异（11/11 变红）

| # | 变异 | 结果 |
|---|---|---|
| M1b | corpus **按 output 内容**挑 seed（真按谜面选题） | **红 ×4** |
| M2 | 归一化不排序（顺序反的同一对不再去重） | **红 ×2** |
| M3 | 去掉长度上限（超长句进 corpus） | **红 ×2** |
| M4 | 不做敏感词过滤 | **红 ×1** |
| M5 | bag 改回有放回 `choice()` | **红 ×3** |
| M6 | bag 用全局 `random` | **红 ×2** |
| M7 | corpus 缺失时回退人工词库 | **红 ×5** |
| M8 | Stage A prompt 写回 v1 的形状硬约束 | **红 ×6** |
| M9 | Stage A schema 加回 `facts` 字段 | **红 ×2** |
| M10b | Stage B schema 的 `properties` 里加回 `puzzle` | **红 ×2** |
| M11 | `_init_keyword_bag` 写回鸡生蛋的开关判断 | **红 ×6** |

### 两笔如实记录

**1. M1 第一版没变红。** 我第一版写的是"output 为空就跳过"，而夹具**每行
都有 output** —— 变异打在不生效的路径上，绿灯是假的。改成"output 里没有
`真相` 就跳过"（真的按 output 内容挑）之后 4 条红。

教训与 H4-F 的 M2、G2 的 M7 同源：**变异没红不代表代码对，可能是变异本身
没落在被判定的路径上。** 记录在这里，因为它是这一轮唯一的"假绿"。

**2. 一个我自己写进去的鸡生蛋 bug。**

`_init_keyword_bag` 第一版用配置开关短路：

```python
if not self._keyword_enabled():   # ← 错
    return
```

而 `_keyword_enabled()` 的定义是"配置开着 **且 bag 已存在**"。于是：

```
bag 是 None -> _keyword_enabled() 为假 -> 直接 return
  -> bag 永远是 None -> 生产静默退回 classic
  -> 连 _bag_error 都是空的（没有"为什么降级"的日志）
```

是 `test_g3_good_corpus_activates_bag` 抓出来的 —— 它注入一份**完好**的
corpus，却发现 bag 没建起来。已改成只看配置开关，理由写进注释，M11 变异
守住它。

**这笔比它本身更值钱的地方**：如果当时没有这条"注入了好 corpus 就应该
建起来"的**反向**用例，这个 bug 会以"生产静默降级"的形式活到实播 ——
而它不会让任何一条既有断言变红。**只测失败路径是不够的，必须有一个
"正常路径确实生效"的用例。**

---

## 十、验证

- **22 个 CI 离线套件全部 rc=0 / 0 fails**（`test_judge_golden` 按既定
  口径不作 gate —— 联网非确定性；`test_web` 需浏览器，由 CI 提供）。
- **装配冒烟**：`director.py --sim tests/fixtures/smoke_script.jsonl
  --no-llm --no-window --reveal-hold 3 --max-puzzles 2` -> exit=**124**、
  **0 Traceback**、**"题就位"存在**、日志里有 keyword2 bag 那行 INFO。
- **G1 抽取序列未变**：`--draw-only` 仍走人工词库那条历史路径（K4 的 md5
  钉死），G3 一个字都没碰它。
- **新增 LLM 调用 = 0**。没跑 20 道样本（§十：不需要）。

---

## 十一、不动（边界确认）

```
live generation（一阶段 Blueprint 链）        ✅ 未改
curated 链（LazyCurator / CuratedCompiler）   ✅ 未改
H4-F（外部题不因分布被拒）                    ✅ 未改
AI-original quota（cross_puzzle_gate HARD）   ✅ 未放宽
Reviewer / truth audit / validate_spec        ✅ 未改
Stage B schema（仍无 puzzle/answer/title）    ✅ 未改
RIDDLE_SYSTEM                                 ✅ 未改
QUALITY_POLICY_VERSION / RIDDLE_PROMPT_VERSION ✅ 未 bump
safety / 动态 few-shot / Style Card           ✅ 未做
```
