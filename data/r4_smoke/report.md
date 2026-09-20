# R4 production smoke —— Story -> Surface -> Structure

真实生产链(`keyword_spec`)跑 5 红 + 5 黑。
**不重抽、不评分、不排名** —— 直接读原文。

- 共 `10` 道, 成题 `2`

| # | lane | keywords | puzzle 长度 | 结果 |
|---|---|---|---|---|
| 1 | - | 季节、没湿 | 0 | 未成题(gen_fail) |
| 2 | - | 神秘洞穴、眼疾 | 0 | 未成题(gen_fail) |
| 3 | - | 游乐园、解救 | 0 | 未成题(gen_fail) |
| 4 | - | 杀害、院子 | 0 | 未成题(gen_fail) |
| 5 | - | 老人、天花板 | 0 | 未成题(gen_fail) |
| 6 | - | 时间、数字 | 0 | 未成题(gen_fail) |
| 7 | black | 老鼠爬、死老鼠 | 28 | ok |
| 8 | - | 古村落、恐怖 | 0 | 未成题(gen_fail) |
| 9 | black | 冒险、度假 | 40 | ok |
| 10 | - | 老公、半瓶香水 | 0 | 未成题(gen_fail) |

---

## 1. (无 lane) — 关键词: 季节、没湿

**未成题**: `gen_fail`

- Stage B 侧信道: `review_rewrite`

## 2. (无 lane) — 关键词: 神秘洞穴、眼疾

**未成题**: `gen_fail`

- Stage B 侧信道: `review_rewrite`

## 3. (无 lane) — 关键词: 游乐园、解救

**未成题**: `gen_fail`

- Stage B 侧信道: `truth_reject`

## 4. (无 lane) — 关键词: 杀害、院子

**未成题**: `gen_fail`

- Stage B 侧信道: `validation_reject`

## 5. (无 lane) — 关键词: 老人、天花板

**未成题**: `gen_fail`

- Stage B 侧信道: `validation_reject`

## 6. (无 lane) — 关键词: 时间、数字

**未成题**: `gen_fail`

- Stage B 侧信道: `truth_reject`

## 7. black — 关键词: 老鼠爬、死老鼠

### answer(完整汤底)

> 一位潦倒作家写不出新作，便用亡妻生前留下的旧手稿冒充亲笔投稿，一举成名。妻弟读出新作里满是姐姐的笔迹与只有她知道的私事，于是掘开姐姐的坟——棺材里是空的，原来她当年"死"后一直活着，被丈夫囚在地下室逼着代写，成名作只是她"复活"的产物。

### puzzle(极短汤面)

> 妻弟读完姐夫的新作，连夜掘开了姐姐的坟——棺材里是空的。

_(**28 字**)_

### Stage B

- review_decision: `pass`
- truth_audit_ok: `True`

### core_answer

作家成名作其实是妻子写的——她没死，被丈夫囚在地下室代笔；棺材是空的。

### facts

| id | kind | visibility | text |
|---|---|---|---|
| `f1` | support | hidden | 姐夫是潦倒作家，长期写不出新作。 |
| `f2` | core | hidden | 姐夫发表、令他一举成名的"新作"，实际是妻子生前留下的旧手稿/由妻子代笔写成的。 |
| `f3` | core | hidden | 姐姐当年并没有真正死去，葬礼后一直活着，被丈夫囚禁在地下室。 |
| `f4` | support | hidden | 妻弟在新作中读出了姐姐独有的笔迹和只有她才知道的私事，由此断定姐姐还在世（或在被利用）。 |
| `f5` | support | public | 妻弟据此连夜掘开姐姐的坟墓，想确认此事。 |
| `f6` | support | public | 棺材是空的。 |
| `f7` | exclusion | hidden | 这不是鬼怪或超自然事件，也没有灵异代笔。 |
| `f8` | exclusion | hidden | 姐姐并非诈死骗保或单纯私奔，是被丈夫囚禁、胁迫代写。 |
| `f9` | exclusion | hidden | 坟墓不是被盗墓者挖空的，棺内从未装过她的遗体。 |

### completion_fact_ids

`f2`, `f3`

### hints

1. 新作里为什么全是姐姐的笔迹和私事？
2. 姐夫当年"丧妻"办得很潦草，遗体没人见过。
3. 写不出东西的人，可以让别人替他写。

---

## 8. (无 lane) — 关键词: 古村落、恐怖

**未成题**: `gen_fail`

- Stage B 侧信道: `truth_reject`

## 9. black — 关键词: 冒险、度假

### answer(完整汤底)

> 独居老人常年把厨余垃圾留给住在楼下的儿子一家，儿子嫌脏，只把袋子放在门口。邻居小孩翻垃圾找瓶子，被袋中一把没包好的厨刀割破动脉，死在老人门口。老人误以为是猫打翻了袋子，便把刀洗净放回厨房，又顺手把带血的垃圾再倒进楼下垃圾桶。案发后警方只找到一把干净的刀，儿子以为父亲杀人，替他顶了罪。

### puzzle(极短汤面)

> 警察在独居老人家里只找到一把干净的厨刀。老人的儿子却主动向警方承认，人是他杀的。

_(**40 字**)_

### Stage B

- review_decision: `fix`
- truth_audit_ok: `True`
- issue: facts 中 f7 的 kind 为 'public'，不在允许值 ('core','support','exclusion') 内，需改为合法值（support）。

### core_answer

老人没杀人；刀上的血是邻居小孩翻垃圾时被袋中厨刀割破动脉留下的，儿子误以为父亲杀人而替父顶罪。

### facts

| id | kind | visibility | text |
|---|---|---|---|
| `f1` | core | hidden | 厨刀是邻居小孩翻垃圾袋找瓶子时被割破动脉致死的原因，老人并未用刀杀人。 |
| `f2` | core | hidden | 老人以为刀是猫打翻垃圾袋弄脏的，于是把刀洗净放回厨房，又把带血的垃圾丢进楼下垃圾桶。 |
| `f3` | core | hidden | 儿子并不在现场，也没有行凶；他主动认罪是因为认定是父亲杀了人，要替父亲顶罪。 |
| `f4` | support | hidden | 老人独居，平日里把厨余垃圾交给住在楼下的儿子一家处理。 |
| `f5` | support | hidden | 厨刀在垃圾袋里没有包好，刀口外露，才会割伤翻袋的人。 |
| `f6` | support | hidden | 儿子平日里嫌垃圾脏，只把袋子放在门口，并不真正丢弃。 |
| `f7` | support | public | 警方只找到一把干净的厨刀，说明凶器被清洗过，而不是没被使用过。 |
| `f8` | exclusion | hidden | 案情并非儿子动手杀人，也不是父亲动手杀人；两人都没有杀人行为。 |
| `f9` | exclusion | hidden | 现场不存在第二把凶器或外来凶器，也并非入室行凶。 |

### completion_fact_ids

`f1`, `f3`

### hints

1. 刀为什么是干净的？谁把它洗了？
2. 老人独居在家，那天究竟发生了什么事？
3. 儿子不在现场，他凭什么断定是父亲杀的？

---

## 10. (无 lane) — 关键词: 老公、半瓶香水

**未成题**: `gen_fail`

- Stage B 侧信道: `truth_reject`
