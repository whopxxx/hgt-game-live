# G4 —— keyword2 改为「独立词库 + 随机重新组合」+ 关键词清洗收紧

**基线 main** `9e940b2d2c5bcf0afb033a3fb607ac5c784b8190`(G3, 未验收)
**本轮只修两处** `corpus = 独立词` / `sampler = 随机重新组合两个词`
**未动** `KEYWORD_IDEA_SYSTEM`(`story/llm.py` 相对 G3 **零 diff**)

---

## 〇、G3 错在哪

G3 把 **pair** 当采样单位:

```
haiguitang input -> 只留恰好 2-key 的行 -> shuffle 那些 pair
```

那不是产品要的随机。它有两个后果:

1. **搭配先验被继承下来。** 抽到的两个词永远**在原始数据里一起出现过** ——
   `三兄弟/杀人` 会一直被一起抽到, 而 `三兄弟/高跟鞋` **永远抽不到**。
   外部题库的选题口味就这样被悄悄抄了进来。
2. **丢掉一半以上的词。** 只读 2-key 行, 于是 `电话/老师/火车` 这一行
   **整行被丢掉**, 三个词一个都进不了词库。实测 3729 行里只有 1005 行是
   2-key —— 剩下 2724 行的词全被扔了。

产品要的是:

```
从 haiguitang input 提取独立关键词词库
运行时随机抽两个不同关键词重新组合
原始 pair 关系不保留
```

---

## 一、`corpus` 从 pair 改成 vocabulary(§1)

**3729 行全部展开**, 不分 1-key / 2-key / 3-key:

```
关键词：A，B，C   ->  词库 += {A, B, C}
```

产物从 `data/keyword2_seed_pairs.json` 改成
**`data/keyword2_vocabulary.json`**, 记录:

```
corpus_version      keyword2-vocab-v1
source              neurostellar/haiguitang
raw_rows            3729
raw_token_count     8123      <- 拆出来的**全部**片段(未过滤)
valid_token_count   7155      <- 通过 is_valid_keyword 的(含重复)
unique_token_count  1144      <- 去重后 == len(keywords)
keywords            [...]     <- 已排序, 同词只存一次
```

三个计数的含义**不要混**:

| 计数 | 含义 |
|---|---|
| `raw_token_count` | 所有片段 —— 含句子碎片 |
| `valid_token_count` | 其中形状合法的 —— **含重复** |
| `unique_token_count` | 去重后的词数 |

`raw - valid` 是**清洗丢掉的量**, `valid - unique` 是**重复的量**。
两个差值都是信息, 所以三个都记。

**不保留频率**: 同词只存一次, 不带它原来的出现次数。存成有权重的表, 采样
就会偏向原数据里被反复写到的词 —— 那是外部题库的高频偏置, 我们不继承。

---

## 二、关键词清洗收紧(§2)

G3 只用 `<= 12 字`, 结果词库里混进大量**句子碎片**:

```
一姐妹母亲去世 / 回家后却把姐姐杀了 / 不久后我把大哥也杀了
我有两个哥哥 / 一名男子A请另一名男子B签上名字在纸上
五人同时到达目的地。加快脚步的四人被淋成了落汤鸡
```

`<=12` 挡不住它们 —— 12 个字的**完整句子**有的是。G4 加了三层:

### (a) 长度上限 12 → **6**

普通关键词(人物称谓/地点/物品/动作/状态/日常概念)极少超过 6 个汉字。
实测被长度挡掉的 138 个片段长这样 —— `一位女士去鞋店里买了一双红色高跟鞋`、
`五人同时到达目的地。加快脚步的四人被淋成了落汤鸡`。那不是关键词。

下界保持 **1**: 原数据里 `110`、`b` 是真实存在的 seed。

### (b) **句子性词素**黑名单(本轮最要紧的一条)

出现任一个就丢掉:

```
人称代词  我 你 他 她 它 咱
虚词      的 了 却 也 就 还 又 都 才 而 并 被 把 让 给 向 从 对 与 和 或
疑问因果  为什么 怎么 怎样 如何 因为 所以 但是 可是 于是 然后 后来 …
量词短语  一个 一名 一位 一只 一条 一件 一场 一次 一种 一群 一堆 …
时间顺序  之后 之前 以后 以前 当天 次日 不久 最后
判断存在  是 有 在 会 能 要 想 说 问 答 发现 觉得 认为 知道 …
副词      同时 一起 立刻 马上 突然 终于 已经 正在 很
```

⚠️ **这份表是逐个查过碰撞才加的**, 不是拍脑袋。两个例子:

* `很` —— 命中 `屋内光线很暗` / `光线很暗` / `成绩很好` 三条, **全是碎片**,
  零假阳性, 所以收。
* `太` —— 会误伤 `太空` 与 `四房姨太太`(真词), 所以**不收**。

这就是"宁可 conservative"的落地方式: **加之前先跑一遍全库看碰撞**。

### (c) 字符白名单 + 敏感词表(§3)

关键词只允许汉字/字母/数字 —— 挡标点、emoji、句读。敏感词表保持
**确定性、无 LLM**; 任务书 §3 明确"不要因为 corpus 来源是公开数据就直接全收"。

**效果**: 整句全部消失。清洗后最长保留词是 6 字, 只有 `屋内光线` 这类
边缘情况被长度放进来(已被 `很` 挡掉)。

---

## 三、Runtime 真正随机组合(§4)

```
word1 = vocabulary 随机抽
word2 = vocabulary 随机抽
要求 word1 != word2
**不保留**它们在原始数据里的搭配关系
```

**不要求**: 不同 slot / 原来是同一 pair / 语义相关 / 人工 compatibility。

`KeywordBag` 的 `slots` 恒为 `[]` —— 独立词库没有槽位概念, 留着这个 key
只为调用方与日志形状不变。

---

## 四、短期重复控制(§5)

产品要的不是"不放回"(那会退化成另一种确定性), 而是:

* 同一个 keyword 不要**连续高频**出现
* 同一个 unordered pair 在**合理窗口**内不重复

做法: `recent_keywords`(窗口 40) / `recent_pairs`(窗口 200) 两个滑动
窗口, 抽样时避开。**两层放宽兜底**:

```
1. 抽 word1 / word2(都避开 recent_keywords)
2. word1 == word2            -> 重抽
3. pair 在 recent_pairs 里    -> 重抽
4. 试满 MAX_TRIES(24)        -> **放宽 keyword cooldown**(只避 pair)
5. 再试满 24 次               -> 完全放宽(只保证 word1 != word2)
```

第 4/5 步是**必须有**的: 抽词函数没有"抽不出来"这个返回态, 调用方没有处理
它的地方。实测 1144 词的库里 100 次抽取只重试 4 次、**0 次放宽**。

---

## 五、RNG(§6)

继续用独立 keyword RNG:

```
给了 keyword_session_seed  -> 直接用
只给了 quality_seed        -> derive_session_seed() 确定性派生
两个都没给                 -> 启动随机一次, 写进 INFO 日志
```

三方隔离: live 出题用 `quality_seed`, classic 补池用
`quality_seed ^ 0x9E3779B9`, keyword 采样再走一层派生。K33 断言 keyword
的派生值**不等于**另外两个。

⚠️ **词表顺序必须确定** —— `KeywordBag` 用 `sorted()` 而不是 `list(set(...))`。
这条的测试**必须在独立进程里跑**: 同进程内两次 `set` 迭代顺序相同, 所以在
进程内比较永远绿, 删掉 `sorted()` 也不会红(变异实测: **0 条失败**)。
K40 用 3 个独立子进程比对输出。

日志实测:

```
INFO story.prefetch: keyword2 bag 就绪:
  keyword2 session_seed=11642316405628668829 corpus_version=keyword2-vocab-v1 keyword_count=1144
```

---

## 六、组合空间报告(§7)

```
raw rows            : 3729
raw tokens          : 8123
valid tokens        : 7155
unique keywords     : 1144
理论 unordered 组合 : 653796 = 1144*(1144-1)/2
```

### 固定 seed 下前 20 组**重新组合**的 pair

`quality_seed = 20260920` → `session_seed = 14611685660579076342`

```
[01] 幽灵船，旅程      [11] 乞丐，海滩
[02] 荒岛，新伤        [12] 名画，婴儿
[03] 跳伞，老朋友      [13] 兄妹，闪光灯
[04] 教授，精灵        [14] 帽子，数字
[05] 古桥，守护        [15] 字条，预知
[06] 面汤，意外        [16] 摆脱嫌疑，汉斯
[07] 推理作家，网友    [17] 车站，心理影响
[08] 男子A，迷宫       [18] 暴风雨，车
[09] 勒死，训练        [19] 苍蝇，怀表
[10] 锁，出口          [20] 铁棒，侧脸
```

复现:

```
uv run tools/build_keyword_seed_corpus.py --report --seed 20260920 --show 20
uv run tools/experiment_keyword_riddles.py --draw-corpus 20 --seed 20260920
```

### 从未在原始 input 里作为同一组出现过的比例(验收核心)

```
原始 input 里出现过的 unordered pair : 2165
抽 20 组  -> 全新 20 / 20  = 100.0%
抽 100 组 -> 全新 100 / 100 = 100.0%
```

**这就是验收核心, 比例是 100%。** 对照 G3: 那时的比例是 **0%** ——
按定义, 从 pair 表里抽出来的每一组都"出现过"。

---

## 七、回归(§8 的 12 条)

| 要求 | 落在 |
|---|---|
| 1. 3-key 行的三个词都会进 vocabulary | **K23**(`电话/老师/火车` 三个都查) |
| 2. 1-key 行也会贡献词 | **K23**(`山地`) |
| 3. 原始 pair 不被当作采样单位 | **K24**(每个元素都是 str 不是 list; 产物无 `pairs` 字段) |
| 4. 两词由独立 vocabulary 抽出 | **K29**(4 个词跑满 6 种组合) + **K37**(100% 全新) |
| 5. 固定 seed 可复现 | **K30**(同进程)+ **K40**(**跨进程**, 见 §五) |
| 6. `word1 != word2` | **K29 / K30 / K31** |
| 7. recent pair 不重复 | **K30**(小词库 40 组严格不重复 **+ 反证**: 关掉窗口就重复) |
| 8. 明显句子残片不进 vocabulary | **K26**(5 条整句 + 逐条判据) |
| 9. corpus 不可用仍显式回 classic | **G4-1/2/3**(行为 + 结构 + AST 三层) |
| 10. Stage A keyword2-v2 prompt 保持不变 | `story/llm.py` **零 diff**; `test_llm.py` G3-K1~K6 仍绿 |
| 11. Stage B / Reviewer / audit / hard quota 全不动 | `test_llm.py` / `test_pool.py` / `test_solve_ux.py` 全绿; `llm.py` 零 diff |
| 12. live / curated 不动 | `test_curated_*` / `test_lazy_curator` 全绿; `director.py` 零 diff |

新增: `test_keyword_seed.py` K23~K40(18 条)、`test_prefetch.py`
G4-1~G4-6(6 条)。**全部登记进 `main()`**。

---

## 八、变异

| # | 变异 | 结果 |
|---|---|---|
| M1 | 只读恰好 2-key 的行(G3 旧行为) | **红 ×7** |
| M2 | 每行只取前两个词 | **红 ×2** |
| M3 | 两词相邻取(不是独立抽) | **红 ×2** |
| M4 | 去掉句子性词素过滤 | **红 ×8** |
| M5 | 长度上限放回 12 | **红 ×2** |
| M6 | 去掉 safety 过滤 | **红 ×5** |
| M7 | 允许 `word1 == word2` | **红 ×5** |
| M8 | 去掉 pair cooldown | **红 ×1**(见下) |
| M9 | 词表不排序 | **红 ×1**(见下) |
| M10 | 用全局 `random` | **红 ×2** + prefetch ×1 |
| M11 | 兜底改成返回空 | **红 ×1**(见下) |
| M12 | 词库缺失时回退人工词库 | **红 ×5** |

---

## 九、三个"变异没红"的坑(全部已修)

这一轮有 **3 条变异第一版没有变红**。它们不是运气问题, 每一条都指向一个
**测试写了但没测到东西**的缺口。逐条记录, 因为这是本轮最有价值的部分。

### 1. M8(去掉 pair cooldown)—— 规模选错了

第一版用 60 个词抽 30 次。**60 个词时 pair 撞车概率本来就接近 0**, 所以
删掉 pair cooldown 这条测试照样绿。

修法: 用一个**小**词库让撞车几乎必然发生 —— 10 个词 = 45 种组合, 抽 40 次。
并加一条**反证**: 把窗口关掉, 同样规模下**必须**出现重复。反证是关键 ——
它证明"没撞车"是因为机制生效, 而不是因为规模太小。

### 2. M9(词表不排序)—— 在同一个进程里测的

`list(set(...))` 的迭代顺序**依赖 PYTHONHASHSEED**, 跨进程会变。但我第一版
在**同一个进程**里建两个 bag 比对 —— 顺序当然一样, 所以删掉 `sorted()`
也不红。

修法: K40 起 **3 个独立子进程** (`subprocess.run`) 比对输出。

### 3. M11(兜底改成返回空)—— 测的是死路径

第一版的"窗口耗尽"配置(40 词 / 窗口 40 / 抽 60 次)其实**到不了第二层兜底**
—— 第一层放宽就够了。于是我把兜底改成"返回空", 测试**照样绿**: 因为我根本
没执行到那行。

修法: 穷举搜索找出**真能走到第二层**的配置(3 个词 / cooldown 2/5), 并直接
断言 `relaxed == 2` 出现过。

> 我另外试了一个把"第一层放宽整个删掉"的变异 —— 它**不红**, 而这次是
> **对的**: 第二层兜底照样保证 `word1 != word2` 与非空, 行为等价。变异的
> 目的不是"必须红", 而是"红了说明测试真的在测东西"。这一条我验证了等价性
> 之后才判定为合理绿。

**共同点**: 三条都是"断言写得很像在测那个机制, 但执行路径根本没走到"。
这与 H4-F 的 M2、G2 的 M7、G3 的 M1 是同一类问题 —— 只是这一轮一次出现了
三条, 说明**变异必须逐条确认它真的落在被判定的路径上**, 不能看到红就打勾。

---

## 十、corpus 缺失 = 显式降级(§8-9)

```
缺失 / 空 / 解析失败 / 全无效
  -> load_vocabulary 抛 CorpusError
  -> _init_keyword_bag 打 ERROR, bag 留 None
  -> _keyword_enabled() 返回 False
  -> 整条 keyword2 链让位给 classic Blueprint 链
```

删掉 `data/keyword2_vocabulary.json` 后跑装配冒烟:

```
exit=124    Tracebacks: 0    "题就位": 1
ERROR story.prefetch: keyword2 corpus 不可用, **本次配置整条 keyword2 链
让位给 classic Blueprint 链**(不会回退人工词库):
CorpusError: 词库文件不存在: data\keyword2_vocabulary.json
```

**不偷偷回退人工词库**用三层挡: 行为(零调用)/ 结构(`_keyword_enabled`
只看 bag)/ **AST 源码层**(不得**引用** `KEYWORD_BANK` / `draw_two_keywords`)。
M12(在降级分支里塞一段人工词兜底)-> **红 ×5**。

---

## 十一、验证

- **22 个 CI 离线套件全部 rc=0 / 0 fails**。
- **装配冒烟**: exit=**124**、**0 Traceback**、**"题就位"存在**、日志有
  `keyword2 bag 就绪: … keyword_count=1144`。
- **新增 LLM 调用 = 0**。没跑样本(§「不需要再跑 20 道 LLM 样本」)。

---

## 十二、不动(边界确认)

```
KEYWORD_IDEA_SYSTEM (keyword2-v2)          ✅ 零 diff
story/llm.py 整个文件                       ✅ 零 diff
Stage B schema / 冻结语义                   ✅ 未改
Reviewer / truth audit / validate_spec     ✅ 未改
cross_puzzle_gate HARD / too_similar       ✅ 未放宽
live generation / director.py              ✅ 未改
curated 链                                 ✅ 未改
QUALITY_POLICY_VERSION                     ✅ 未 bump
RIDDLE_PROMPT_VERSION                      ✅ 未 bump
```

**版本号**: `KEYWORD_SEED_VERSION: keyword2-seeds-v2 -> keyword2-vocab-v1`
(词源换了)。`KEYWORD_BANK_VERSION = keyword2-v1` 保留为历史号(G1 数据按它抽)。
metrics 继续记 `generation_mode=keyword2` + `keywords` +
`keyword_seed_version` / `keyword_corpus_version` / `keyword_session_seed`。
