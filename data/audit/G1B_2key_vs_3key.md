# G1-B —— 2-key vs 3-key:快速决策实验

**问题**: 2-key 和 3-key, 哪一种更适合作为未来 AI-original 的候选起题方式?

**基线** main `e6125639afa2fc95045fee5e83abfb042fcc4c4b`(G1-A 已通过)
**同一脚本** `tools/experiment_keyword_riddles.py`; 同一 seed `20260920`;
同一套 stage1 / stage2 / Reviewer / truth audit; **未新增任何 prompt 规则**
**生产代码零改动**

## 一、实验命令

```
# 2-key (G1-A, 已有)
.venv/Scripts/python.exe -X utf8 tools/experiment_keyword_riddles.py \
    --out data/g1a --seed 20260920 --limit 5 --concurrency 3

# 3-key (G1-B, 本轮)
.venv/Scripts/python.exe -X utf8 tools/experiment_keyword_riddles.py \
    --out data/g1b --seed 20260920 --key-count 3 --limit 5 --concurrency 3
```

`--key-count` 是 G1-B 新增的**唯一**参数。它是**过滤**不是重抽 —— 
抽取序列逐字不变(默认 `--draw-only` 的输出哈希与 G1-A 相同), 
所以 3-key 拿到的第 11~15 组与 G1-A 里"跑下去会拿到的那几组"一致, 
两批数据可比。

## 二、对照表

| 指标 | 2-key (G1-A) | 3-key (G1-B) |
|---|---|---|
| **valid** | **3 / 5** | **3 / 5** |
| stage1 成题率 | 5 / 5 | 5 / 5 |
| stage1 总调用 | 5 | 5 |
| stage2 总稿数 | 6 | 7 |
| **puzzle 长度 median** | **33 字** | **64 字** |
| answer 长度 median | 41 字 | 44 字 |
| truth audit pass | 3 / 3 | 3 / 3 |
| Reviewer | `{"fix": 4, "pass": 1}` | `{"fix": 4}` |

> **valid 持平 (3/5)**; stage1 成题率持平 (5/5); truth audit 持平 (3/3)。
> 3-key **没有**输在通过率上。它输在**谜面长度**与**背景复杂度**。

## 三、3-key 的 5 道原文

### [11] `电梯，迟到，钟表` ❌

**puzzle**(92 字):

> 小林每天上班都提前十分钟到公司，今天却迟到了半小时。同事问他怎么回事，他说：“我在电梯里只待了三分钟。” 可他进电梯时看了一次钟表，出电梯时又看了一次，两次相差整整三十五分钟。为什么？

**core_answer**(0 字):

> (无 —— 未过审)

**未过审的 stage1 原 answer**(仅存档):

> 他进的那部“电梯”是公司所在大楼的观光电梯，而他人其实在一家医院里——他当天早上突然晕倒被送医，醒来后从病床被推去做检查，进的是医院的运送电梯。他在电梯里因低血糖再次昏迷，护士发现后立即停下电梯抢救，前后耽误了三十五分钟。他迷糊中只记得电梯门开合的“三分钟”感觉，而两次看钟表的记忆其实是同一块挂钟——进电梯前和出电梯后他都在同一楼层、同一个位置看到它，所以他自己以为只过了三分钟。真正的解释是：他在电梯里昏过去又醒过来，中间的时间完全丢失了，而“钟表相差三十五分钟”是因为他看到的其实是电梯外大厅的挂钟，两次都停在同一个位置——他误把出电梯后看到的挂钟当成进电梯前那次的比较对象，实际上进电梯前他看

| 项 | 值 |
|---|---|
| final | **rejected_ai_gate** |
| stage | `ai_gate` |
| 阶段1/阶段2 attempts | 1 / 0 |
| Reviewer | `(无)` |
| truth audit | n/a(未走到) |
| 拒因 | accepted=false, no_reasonable_explanation, ambiguous |

### [12] `打电话，毛巾，同学` ✅

**puzzle**(57 字):

> 深夜，他给同学打电话，电话那头的人却一直在擦毛巾，一句话不说。第二天他才知道，同学昨晚根本没带手机。这是怎么回事？

**core_answer**(31 字):

> 接电话的是正在擦头发的室友，同学本人在洗澡、没把手机带在身边。

**observed signature**: `{"mechanism_family": "identity_misread", "solution_shape": "identity_reversal", "domain": "daily", "emotion_mode": "eerie", "relation": "friend", "time_shape": "single_day", "death": false, "past_trauma": false, "long_term_profession": false, "repeated_ritual": false, "reveal_mode": "identity_flip", "procedural_rule_dependency": false}`

| 项 | 值 |
|---|---|
| final | **valid** |
| stage | `` |
| 阶段1/阶段2 attempts | 1 / 2 |
| Reviewer | `fix` |
| truth audit | pass |

### [13] `同学，睡觉，图书馆` ❌

**puzzle**(47 字):

> 我在图书馆自习时睡着了，醒来发现对面桌上趴着一个睡着的同学，我再也不敢去那家图书馆了。为什么？

**core_answer**(0 字):

> (无 —— 未过审)

**未过审的 stage1 原 answer**(仅存档):

> 那个"同学"其实是我自己。我睡着后，图书馆到了闭馆时间，管理员没注意到角落里的我，锁门离开并拉下了总电闸。我在黑暗中醒来，迷迷糊糊走到对面坐下又睡去，中途还去洗手间的镜子里看见过"另一个人"，其实那就是我自己；第二天早上被人发现时，我才明白那一夜图书馆里只有我一个人，而我对面趴着的"同学"是我自己在另一张桌子上的倒影/错觉——更准确地说，那具"趴着的同学"是我之前坐的位置留下的、我睡着时被管理员以为已经离开而留下的外套和书包堆，我以为那是别人。总之真正让我恐惧的是：那一晚我以为图书馆里有别人，其实从头到尾都只有我，而我对"同学"的记忆全部属于我自己。

| 项 | 值 |
|---|---|
| final | **rejected** |
| stage | `truth_audit` |
| 阶段1/阶段2 attempts | 1 / 0 |
| Reviewer | `fix` |
| truth audit | n/a(未走到) |
| 拒因 | conflicts 非空 |

### [14] `服务员，图书馆，发烧` ✅

**puzzle**(54 字):

> 我在图书馆里叫来服务员，点了杯热咖啡，然后量了体温：39度。我笑了，因为我知道自己发烧不是生病。这是为什么？

**core_answer**(44 字):

> "图书馆"是家咖啡馆，"39度"是刚喝的热饮造成的口温假象，此人在为骗保/骗病假造证据。

**observed signature**: `{"mechanism_family": "object_misuse", "solution_shape": "misunderstood_object", "domain": "daily", "emotion_mode": "neutral", "relation": "self", "time_shape": "instant", "death": false, "past_trauma": false, "long_term_profession": false, "repeated_ritual": false, "reveal_mode": "recontextualization", "procedural_rule_dependency": false}`

| 项 | 值 |
|---|---|
| final | **valid** |
| stage | `` |
| 阶段1/阶段2 attempts | 1 / 1 |
| Reviewer | `fix` |
| truth audit | pass |

### [15] `相册，理发师，搬家` ✅

**puzzle**(69 字):

> 我请理发师上门给卧床多年的母亲剪头发，剪完他收好碎发就走了。第二天，我看见他把那撮头发夹进一本旧相册里带走了。他为什么要带走我母亲的头发？

**core_answer**(76 字):

> "理发师"其实是老人失散多年的儿子（家中子女的哥哥），他借剪发取到老人新生白发的发根，与相册里旧发丝比对确认亲生关系，并把头发夹回属于自己的旧相册认亲。

**observed signature**: `{"mechanism_family": "identity_misread", "solution_shape": "identity_reversal", "domain": "family", "emotion_mode": "warm", "relation": "family", "time_shape": "years_long", "death": false, "past_trauma": true, "long_term_profession": false, "repeated_ritual": true, "reveal_mode": "identity_flip", "procedural_rule_dependency": false}`

| 项 | 值 |
|---|---|
| final | **valid** |
| stage | `` |
| 阶段1/阶段2 attempts | 1 / 4 |
| Reviewer | `fix` |
| truth audit | pass |

## 四、两条决策判据的逐条判定

### 判据 A: 是否明显"为塞第三个词而硬编"?

**是 —— 5 道里 2 道有明显痕迹。**

| # | 关键词 | 痕迹 |
|---|---|---|
| `[14]` | 服务员，图书馆，发烧 | 为让"服务员"成立, 把**图书馆**改写成"是家咖啡馆" |
| `[15]` | 相册，理发师，搬家 | 为让"相册"成立, 加上"失散多年的儿子认亲"整层背景 |

作为对照, 2-key 批**没有**这个现象: 那道"阳台+司机"、"上楼+老人"的题里, 
两个词都直接构成谜面本身, 没有哪个词需要额外解释才能站住。

逐条看关键词是否落进谜面:

| # | 关键词数 | 出现在谜面 | 结果 |
|---|---|---|---|
| `[11]` | 3 | 3 (电梯，迟到，钟表) | rejected_ai_gate |
| `[12]` | 3 | 3 (打电话，毛巾，同学) | valid |
| `[13]` | 3 | 2 (同学，图书馆) | rejected |
| `[14]` | 3 | 3 (服务员，图书馆，发烧) | valid |
| `[15]` | 3 | 2 (相册，理发师) | valid |

> 注意 `[13]`(**2/3**)与 `[15]`(**2/3**): 第三个词**根本没进谜面**。
> 也就是说它不仅没帮上忙, 还白占了一个创作约束。

### 判据 B: 第二机关 / 复杂背景是否明显增加?

**是。**

* 3-key 谜面中位 **64 字**, 2-key **33 字** —— 近两倍。
* `[14]` 需要"图书馆+咖啡馆的双重身份 + 骗保动机"两层才立得住。
* `[15]` 需要"上门理发 + 碎发 + 旧相册 + 失散认亲"四层。
* 相比之下 2-key 的 `[05]`"图书馆只有六层, 他爬到七楼"是**一层**。

## 五、决策

> **默认采用 2-key。**

三条判据里:

| 判据 | 结果 |
|---|---|
| valid 不差于 2-key 太多 | ✅ 持平(3/5 vs 3/5) |
| 故事明显更自然 / 更有变化 | ❌ **不成立** —— 更复杂, 不是更自然 |
| 没有明显硬塞第三个词 | ❌ **不成立** —— 5 道里 2 道有痕迹 |

三条里两条不支持, 按任务书的决策规则取保守分支: **2-key**。

2-key 更简单、调用更稳定(谜面短一半)、风格已经达到目标 —— 
而第 3 个词换来的是**背景复杂度**, 不是**故事自然度**。

> ⚠️ 样本量 5 vs 5, **不做统计显著性主张**。这是快速决策, 不是定论。
> 若将来要翻案, 需要的是更大样本 + 只看"谜面长度"与"硬塞痕迹率"两个
> 可数的量, 而不是重跑本实验。
