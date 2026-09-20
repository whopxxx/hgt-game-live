# G1-A —— 关键词种子 -> 自由成题 -> 再结构化(实验)

> **本轮不是要求证明 keyword generation 一定更好。**
> 只回答一个问题: 这种极简关键词起题方式, 是否值得成为
> AI-original 的**新候选生成器**?

**基线** main `2b426ecc1ec369d8c8e8aae4cd3f50464ed4a6ef`
**seed** `20260920` · **2-key** 10 组 · **3-key** 10 组 · 共 **5** 组
**target Blueprint** = `None`(全 20 道) —— `mechanism / solution_shape / domain / relation` 是**分类结果**, 不是创作指令
**生产链** `RIDDLE_SYSTEM` **未改**; 本脚本是独立实验文件

> ⚠️ 本轮按用户指示**提前收手**: 用户要求 5 道即可(加快进度)。 下面 5 道是 seed 20260920 抽到的**前 5 组**(全部 2-key)。 第 6~20 组的词已抽出但**未跑**, 不在此报告中冒充结果。

## 一、实验命令

```
# 正式跑(会调 LLM; 并发 3 路)
.venv/Scripts/python.exe -X utf8 tools/experiment_keyword_riddles.py \
    --out data/g1a --seed 20260920 --limit 5 --concurrency 3

# 只抽词不调 LLM(验证 seed 可复现)
.venv/Scripts/python.exe -X utf8 tools/experiment_keyword_riddles.py --draw-only

# 从已跑好的数据重建报告(不调 LLM)
.venv/Scripts/python.exe -X utf8 tools/experiment_keyword_riddles.py \
    --report-only data/g1a/run.json
```

## 二、全部 5 道完整样本(§八: 原样列出, 不删丑题)

失败题也原样保留失败原因 —— **没有一道被删掉**, 包括丑题和故障题。

### [01] `2key` ✅

**关键词**: `阳台，司机`  (槽位 place+person, seed_used=20260921)

**title**: 阳台上的司机

**puzzle**(谜面):

> 男人站在自家阳台上，朝楼下按了两下喇叭，随后他死了。为什么？

**core_answer**(谜底):

> 他是偷了工地失窃现金的吊车司机，被楼下人认出花盆里的赃款后报警，他逃跑时坠楼身亡。

| 项 | 值 |
|---|---|
| final | **valid** |
| stage 2 stage | `` |
| puzzle chars | 30 |
| answer chars | 41 |
| 阶段1 attempts | 1 |
| 阶段2 attempts | 2 |
| Reviewer | `fix` |
| Reviewer issues | 三条提示均超过 30 字上限（34/34/38 字） |
| truth audit | pass |
| validate_spec | pass |
| 用时 | 33.6s |

**observed signature**(§五: 分类结果, 不是创作指令):

```
{
  "mechanism_family": "hidden_function",
  "solution_shape": "hidden_function_explains_behavior",
  "domain": "daily",
  "emotion_mode": "tense",
  "relation": "neighbor",
  "time_shape": "single_day",
  "death": true,
  "past_trauma": false,
  "long_term_profession": true,
  "repeated_ritual": false,
  "reveal_mode": "recontextualization",
  "procedural_rule_dependency": false
}
```

---

### [02] `2key` ✅

**关键词**: `上楼，老人`  (槽位 action+person, seed_used=20260922)

**title**: 楼层

**puzzle**(谜面):

> 老人住在三楼，却每天都要先上到顶楼，再走楼梯下到三楼回家。有一天他上到顶楼后，却直接嚎啕大哭起来。为什么？

**core_answer**(谜底):

> 他是盲人，靠数台阶回家；那天台阶数变了，他再也数不回三楼的家。

| 项 | 值 |
|---|---|
| final | **valid** |
| stage 2 stage | `` |
| puzzle chars | 53 |
| answer chars | 31 |
| 阶段1 attempts | 1 |
| 阶段2 attempts | 2 |
| Reviewer | `fix` |
| Reviewer issues | 第1条提示超过30字(36字), 第3条提示超过30字(44字) |
| truth audit | pass |
| validate_spec | pass |
| 用时 | 28.7s |

**observed signature**(§五: 分类结果, 不是创作指令):

```
{
  "mechanism_family": "information_gap",
  "solution_shape": "misunderstood_object",
  "domain": "daily",
  "emotion_mode": "grief",
  "relation": "self",
  "time_shape": "habitual",
  "death": false,
  "past_trauma": false,
  "long_term_profession": false,
  "repeated_ritual": true,
  "reveal_mode": "meaning_flip",
  "procedural_rule_dependency": false
}
```

---

### [03] `2key` ❌

**关键词**: `开车，地下室`  (槽位 action+place, seed_used=20260923)

**title**: 不拔钥匙

**puzzle**(谜面):

> 他每天开车进自家地下室，倒车入库后熄火，却从不拔钥匙就下车走人。邻居说他家根本没有车。这是怎么回事？

**core_answer**(谜底):

> 他是一名学车教练式的地下车库"倒车入库"陪练，其实开的是驾校的教练车；所谓"自家地下室"是他每天下班后把教练车开回驾校地下车库停放——那不是他家。邻居说的"他家"是住宅，当然没有车，因为他开回来的车属于驾校，钥匙也要留在车上交给值班员，所以熄火后不拔钥匙就走。

**阶段 1 原 answer**(未过审, 仅存档):

> 他是一名学车教练式的地下车库"倒车入库"陪练，其实开的是驾校的教练车；所谓"自家地下室"是他每天下班后把教练车开回驾校地下车库停放——那不是他家。邻居说的"他家"是住宅，当然没有车，因为他开回来的车属于驾校，钥匙也要留在车上交给值班员，所以熄火后不拔钥匙就走。

| 项 | 值 |
|---|---|
| final | **rejected_hard_gate** |
| stage 2 stage | `hard_gate` |
| puzzle chars | 50 |
| answer chars | 130 |
| 阶段1 attempts | 1 |
| 阶段2 attempts | 0 |
| Reviewer | `fix` |
| Reviewer issues | 第 3 条提示 31 字，超过 30 字上限 |
| truth audit | n/a(阶段 2 未走到) |
| validate_spec | n/a(阶段 2 未走到, validate 未执行) |
| 拒因 | no_external_media |
| 用时 | 26.5s |

---

### [04] `2key` ❌

**关键词**: `理发师，桥`  (槽位 person+place, seed_used=20260924)

**title**: 桥下的理发师

**puzzle**(谜面):

> 老王每天在桥洞下给人理发，价格便宜，生意很好。可桥上没有一个人敢往下看，他也从不抬头。为什么？

**core_answer**(谜底):

> 老王是被人从桥上推下去摔断了双腿，从此坐在桥洞下,靠捡来的推子给过往的流浪汉、拾荒者理发糊口。他从不抬头，是因为一抬头就会看到当年把他推下去的那个人每天从桥上走过；而桥上的人不敢往下看，是因为往下看就会认出桥下这个被他们以为早已死了的受害者。凶手和帮凶都心照不宣：一个不敢低头，一个不敢抬头，于是这桩旧案就在桥上桥下相安无事地"藏"了下去。

**阶段 1 原 answer**(未过审, 仅存档):

> 老王是被人从桥上推下去摔断了双腿，从此坐在桥洞下,靠捡来的推子给过往的流浪汉、拾荒者理发糊口。他从不抬头，是因为一抬头就会看到当年把他推下去的那个人每天从桥上走过；而桥上的人不敢往下看，是因为往下看就会认出桥下这个被他们以为早已死了的受害者。凶手和帮凶都心照不宣：一个不敢低头，一个不敢抬头，于是这桩旧案就在桥上桥下相安无事地"藏"了下去。

| 项 | 值 |
|---|---|
| final | **rejected** |
| stage 2 stage | `truth_audit` |
| puzzle chars | 47 |
| answer chars | 170 |
| 阶段1 attempts | 1 |
| 阶段2 attempts | 0 |
| Reviewer | `fix` |
| Reviewer issues | 第 2 条提示 33 字，超过 30 字上限。 |
| truth audit | n/a(阶段 2 未走到) |
| validate_spec | n/a(阶段 2 未走到, validate 未执行) |
| 拒因 | core hidden facts 有 4 条, 超过 3 |
| 用时 | 49.2s |

---

### [05] `2key` ✅

**关键词**: `上楼，图书馆`  (槽位 action+place, seed_used=20260925)

**title**: 七楼

**puzzle**(谜面):

> 图书馆一共只有六层。他每天爬楼梯到七楼去自习，从没走错过。为什么？

**core_answer**(谜底):

> 他把入口外那一段公用大台阶也算作"一层"，所以实际只有六层的图书馆在他口中就成了"七楼"。

| 项 | 值 |
|---|---|
| final | **valid** |
| stage 2 stage | `` |
| puzzle chars | 33 |
| answer chars | 45 |
| 阶段1 attempts | 1 |
| 阶段2 attempts | 2 |
| Reviewer | `pass` |
| truth audit | pass |
| validate_spec | pass |
| 用时 | 28.2s |

**observed signature**(§五: 分类结果, 不是创作指令):

```
{
  "mechanism_family": "information_gap",
  "solution_shape": "misunderstood_object",
  "domain": "daily",
  "emotion_mode": "neutral",
  "relation": "stranger",
  "time_shape": "habitual",
  "death": false,
  "past_trauma": false,
  "long_term_profession": false,
  "repeated_ritual": true,
  "reveal_mode": "recontextualization",
  "procedural_rule_dependency": false
}
```

---

## 三、汇总(§七)

| 指标 | 值 |
|---|---|
| 5 道成功生成数(stage 2 valid) | **3 / 5** |
| 阶段 1 出题成功数 | 5 / 5 |
| Reviewer pass/fix/rewrite | {"fix": 4, "pass": 1} |
| truth audit pass | 3 / 3(走到审计的题) |
| validate pass | 3 |
| puzzle 长度 median | 33.0 字 |
| answer(core_answer) 长度 median | 41.0 字 |
| 2-key 成功率 | 3 / 5 |
| 3-key 成功率 | 0 / 0 |
| 阶段1 总调用 | 5 |
| 阶段2 总稿数 | 6 |

## 四、程序抽到的 20 组关键词(§二: 原样, 不人工挑)

| # | 组 | 槽位 | 关键词 | seed_used |
|---|---|---|---|---|
| 01 | 2key | place+person | `阳台，司机` | 20260921 |
| 02 | 2key | action+person | `上楼，老人` | 20260922 |
| 03 | 2key | action+place | `开车，地下室` | 20260923 |
| 04 | 2key | person+place | `理发师，桥` | 20260924 |
| 05 | 2key | action+place | `上楼，图书馆` | 20260925 |

## 五、人工观察(§十: 只回答'值不值得成为候选生成器')

对着 §二 的样本逐条看这几件事 —— 这是本实验真正要回答的:

1. 谜面读起来像不像**自然题库**里的题(而不是'命题作文')?
2. 反常点是不是**一句话就说清楚**了?
3. 谜底有没有**直接解释**那个反常点(而不是绕开)?
4. 有没有'为了显得高级'硬加的第二机关?
5. 关键词是**自然长在情境里**, 还是硬塞进去的?

> 丑题、怪题、失败题**都在上面**, 没有一个被删掉。

### 我的观察(基于本批 5 道, 不是结论)

**结论: 倾向于'值得作为候选生成器继续测', 但本批样本太小, 不足以定论。**

支持的理由:

1. **谜面确实像自然题库的题。** 5 道的谜面都短(median 33 字)、都是一个清楚的单反常点, 读起来与 haiguitang 那类题**同一语感** ——这恰恰是 Blueprint 命题作文最容易丢掉的东西:
   * `[05]` "图书馆只有六层, 他每天爬到七楼" —— 一句话, 干净。
   * `[02]` "先上顶楼再下到三楼" —— 反常点一目了然。
2. **关键词是自然长在情境里的**, 没有硬塞。`[05]` 的"上楼"+"图书馆"直接构成谜面本身; `[02]` 的"老人"+"上楼"同理。没有一个词是被强行安上去的。
3. **没有'为显得高级加第二机关'。** 5 道全是单机关, 这正是 §三 明确允许、而 Blueprint 链倾向于惩罚的形状。
4. **observed signature 是"读出来的", 不是"派下去的"** ——`hidden_function` / `information_gap` / `misunderstood_object` 都是结构化阶段事后判的。§五 的设计目的达到了。

反对 / 需要警惕的理由:

1. **5 道里有 2 道没过(3/5)。** `[03]` 被硬门拒(`accepted=false`), `[04]` 被 truth audit 拒(叙事自相矛盾: "被推下去摔断腿"与"每天在桥下理发"的因果在审计眼里不成立)。这个通过率**不算高**。
   * ⚠️ 但这**不能**直接读成"keyword 生成更差": 本批没有对照组(没有跑同规模的 Blueprint 链)。要下这个判断, 必须补一组对照。
2. **`[04]` 暴露了一个真实风险: 自由成题容易滑向悲剧 / 重口。**两条关键词("理发师"+"桥")自己会长出"推下桥"这种情节 ——而 §三 明说"不要求悲剧"。若要走这条路, curated-v5 的口径与safety 门**必须**同样作用在它身上(本实验里它们确实起作用了 ——`[04]` 就是被 truth audit 拦下的)。
3. **本批全部是 2-key**(前 5 组恰好都是)。**3-key 一道都没跑** ——3 个词是否会让模型硬塞、或反而更容易成形, 这里**没有数据**。

### 建议

值得继续, 但下一轮应该补:

* 一组**同规模对照**(Blueprint 链跑同样 5 组关键词), 否则无法回答"是否更好"。
* **3-key 组**至少要跑到, 否则 §二 的 3-key 设计等于没测。
* 统计 `[04]` 那类**悲剧倾向**的出现率 —— 如果高, 说明这条路需要额外的口径约束。
