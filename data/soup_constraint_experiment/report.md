# 海龟汤约束剥离实验报告

## 结论摘要

本实验基于 `main@dd40bacb2f7edf8f48cf34333d67796d5516fa72`，实际调用模型为
`deepseek-v4.1-flash`。所有创作调用均为无状态调用；只在 transport/tool payload
不可用时做技术重试，未因“不喜欢结果”重抽。实验脚本只导入 production prompt、
validator 和 Reviewer，不改写任何 production 默认值。

当前系统不是在 Stage B 中把好汤面逐字改成“案情说明”。真实问题是两个来源叠加：

1. **Stage A 已经带有 case-first 倾向。** 它一次生成 `core_truth`、2~4 条
   `observed_clues`、2~3 段 `event_chain` 以及 title/puzzle/answer，并要求关键步骤在
   汤面留下痕迹。10 道 RAW 中，S01、S07、S09 在进入任何结构化后处理前就已经是
   普通误会、励志解释或一眼可解的现实题。
2. **后续更多是在做“幸存者选择”，而不是改写压平。** 当前 Stage B 冻结
   title/puzzle/answer，只生成结构；但 validator、Reviewer、truth audit 的耦合会让
   有气氛的候选因 schema 同步、beat 数量、fair-clue 绑定或审计误判被淘汰。本批仅
   3/10 走完全链。被盲评为“想玩”的 S04、S06、S08、S10 分别死于 Reviewer 的整套
   schema 回传失败、Stage B 技术失败、未授权 metadata 改动、
   `clue_recontextualized` hard check。与此同时，较平的 S01、S07 通过。

因此，本批第一次稳定可见的“变差点”不是某个统一文本改写步骤，而是：

> Stage A 的普通现实先验 + Reviewer/validator 的选择压力，使容易结构化和容易审计的
> 题更容易幸存；好题则会因与题感无关的结构修复失败退出。

同样重要的反证是：shadow `CURRENT SURFACE` 并没有整体变差。盲评中 V0 RAW 为
5 想玩 / 3 可以玩 / 2 不想玩，V2 CURRENT SURFACE 为 7 / 1 / 2。也就是说，
“把任何明确线索从汤面删掉”不是答案；完全关闭 fair clue 的 6 个版本有 5 个“不想玩”。
最小必要约束应保留真实异常锚点和可问答路径，同时去掉人称、问号、多层 beat 和
`fair_clue.quote` 必须直接支撑 required atom 等形式绑定。

## 方法与样本

- 固定随机种子：`20260920`。
- 10 个 production Stage A 原始故事（S01~S10）。
- 另生成 15 个 reality variants、10 个 abstract/few-shot candidates；总计 35 个
  原始创作 candidate。
- 108 个有标签的 surface 实例；按“汤面+汤底”去重后 99 个候选进入盲评。
- 盲评只看到随机号 P001~P099、汤面和汤底，不看到实验标签。
- 盲评与生成使用同一模型供应侧，但使用独立、无状态请求，不携带生成上下文。
- 盲评结果：36 想玩、43 可以玩、20 不想玩。它是小样本定性证据，不是精确评分。
- Stage A 仅 1 次技术重试；shape error 为 0。所有失败输出均保留。

完整数据见同目录的 `raw_results.jsonl`、`ablation_results.jsonl`、
`surface_exposure_results.jsonl`、`style_results.jsonl`、`blind_manifest.jsonl`、
`blind_review.jsonl` 和 `style_classification.jsonl`。

## 真实 production 内容链

以代码而非注释推得的 keyword2 路径如下：

```text
keyword corpus / bag
  -> draw two keywords
  -> Stage A: gen_keyword_idea
       core_truth + observed_clues + event_chain + title + puzzle + answer
  -> Stage B: structure_original_idea
       冻结 title/puzzle/answer，只生成 facts / atoms / completion / clues /
       beats / hints / signature
  -> validate_spec（结构 hard error 直接退出；格式 fixable 交 Reviewer）
  -> Reviewer（pass/fix/rewrite + AI-original quality checks）
  -> validate_spec（复验）
  -> truth audit
  -> cross-puzzle gate（当前题型/配额/近重复为 soft signals）
  -> 可选 playtest
  -> pool admission 再验 policy / structure / signature / safety / true duplicate
  -> live
```

补充分支：keyword corpus 不可用或 keyword2 kill switch 关闭时存在 classic fallback；
curated 使用独立 Reviewer 合同和 ledger；playtest 默认不是内容生成的一部分，而是生成后
可选门；prefetch 与 pool admission 会再次验证。latest main 的 G4 已把 death、grief、
past_trauma、repeated_ritual 等跨题饱和从 hard reject 改为 soft signal。它们仍用于偏好和
诊断，但不应再把“出现死亡”直接挡掉。blueprint 明确要求 `death=false` 时的语义一致性
仍是 hard，这与跨题死亡配额不是一回事。

## 实验 1：V0~V5 轨迹

| ID | RAW 盲评 | 第一次实际损失 | 最终 | 说明 |
|---|---|---|---|---|
| S01 | 可以玩 | Stage A 已平 | PASS | “其实什么都没发生”，后续未造成平庸 |
| S02 | 可以玩 | RAW 自相矛盾 | REJECT / truth audit | 声称真实旧宅，谜底却是复刻屋；拒绝合理 |
| S03 | 想玩 | truth audit | REJECT | audit 把监狱座位/狱警动作作了过强常识推断 |
| S04 | 想玩 | Reviewer/schema repair | REJECT | 第一人称+无问号触发修复，因未回传 beats 整题丢失 |
| S05 | 不想玩* | 无明显损失 | PASS | Reviewer 只补问句；去重盲评把 RAW 视作重复 |
| S06 | 想玩 | Stage B technical | REJECT | 无 tool payload；不是内容失败 |
| S07 | 可以玩 | Stage A 已平 | PASS | Reviewer 修正不真实的“成绩一路飙升”，合理 |
| S08 | 想玩 | Reviewer metadata repair | REJECT | 只需降 core 分类，却误改 `fact.hintable` 被 fail-close |
| S09 | 不想玩 | Stage A 已平 | REJECT / validator | completion facts=3；内容本身也过于直接 |
| S10 | 想玩 | Reviewer hard check | REJECT | `clue_recontextualized=false`，虽整体场景有漂亮重读 |

`*` S05 的盲评原因主要是候选在盲评集内重复；V2 的去重版本为“想玩”。

V3 不改汤面，故不能观察到“Stage B 把汤面越写越长”；它能造成的是结构字段膨胀和
失败概率。DB 对照显示，天然单核的 S01 在强制 2~4 beats 时，被拆成“来电—空摊—
误读—接女儿”四段解释，基本是同一原因的重述。S03 的 beats 更像对谜面逐句复述。
这说明伪复杂度主要出现在 metadata，随后又通过 Reviewer hard check 反向成为候选的
准入压力。

### Reviewer 的实际行为

本批 Reviewer 没有把好汤面系统性扩写成案情说明。可进入 Reviewer 且获得有效文本的
5 个版本中，4 个为“可以玩”、1 个“想玩”；主要改动是机械补问句或一次真实性修正。
真正的伤害是 repair contract：即使只修第一人称或句末问号，quality-v8 也要求
puzzle/answer/core_answer/completion/facts/atoms/fair_clues/discovery_beats 全套非空回传，
并对未授权字段做细粒度 diff。S04、S08 是这个耦合的直接失败样本。

## 实验 2：汤面暴露量

6 个固定 hidden story，各生成 LOW/MEDIUM/HIGH，共 18 个 surface。

| 暴露量 | 想玩 | 可以玩 | 不想玩 | 定性结论 |
|---|---:|---:|---:|---|
| LOW | 2 | 4 | 0 | 问题欲强，但部分答案空间过大，只能靠主持人漫游 |
| MEDIUM | 4 | 2 | 0 | 最稳：核心异常+一个锚点，既有方向又未提前解释 |
| HIGH | 3 | 2 | 1 | 5/6 被独立评估为高泄露，6/6 呈阅读理解倾向 |

独立定性评估中，HIGH 有 5/6 为高答案泄露，且所有 HIGH 都被标为阅读理解倾向；
LOW 全部被预估为需要 8~20 问，但其中一些只能通过宽泛枚举逼近。结合盲评，推荐的
默认形状是 MEDIUM：一个反常场景 + 一个不撒谎的具体锚点。LOW 可作为少数强主持题，
不能成为普遍合同；HIGH 不应由 validator 强迫。

## 实验 3：现实主义约束

每组 5 道。匿名分类结果：

| 组 | 世界类型 | 主要机制 | 盲评 |
|---|---|---|---|
| R1 Reality Preferred | 4 现实 / 1 超自然 | 普通现实2、心理关系2、时空身份1 | 3 想玩 / 2 不想玩 |
| R2 Neutral | 4 现实 / 1 超自然 | 普通现实2、心理关系2、时空身份1 | 3 想玩 / 2 可以玩 |
| R3 Strange Allowed | 5 现实 | 普通现实2、心理关系2、其他1 | 3 想玩 / 2 可以玩 |

“鼓励异常世界”并没有自动产生真正超自然，R3 反而 5/5 被匿名分类为现实；R1 也没有
完全服从“现实优先”。这说明抽象许可的控制力弱。R2 和 R3 都消除了本批的“不想玩”
低谷，R2 的机制自洽与可玩性下限最好。建议 production 不再偏好现实，但也不要设
“必须超自然”硬配额；把 strange-world 当软采样方向，并用自洽世界规则的 few-shot
表达题感。

## 实验 4：Abstract vs structural few-shot

F1 Abstract Only：1 想玩 / 4 可以玩。F2 Structural Few-shot：1 想玩 / 3 可以玩 /
1 不想玩。小样本没有证明 few-shot 总体胜出。

质性差异更有价值：F2 产生了一个冲击更强的心理黑汤，也产生了 P038 的“双重幽灵”
堆料和 P033 的字谜退化。F1 全部被匿名分类为现实；F2 有 1/5 超自然，冲击强的数量
从 0 提到 2，但方差也变大。结论是：few-shot 比“诡异、红汤、黑汤”更能改变分布，
但当前 4 个结构示例还不够约束“海龟汤而不是散文谜/字谜”。适合继续作为 soft style
anchor，并同时放反例；不应写成固定反转模板或 hard validator。

## 实验 5：人称与问号

| 形式 | 想玩 | 可以玩 | 不想玩 |
|---|---:|---:|---:|
| P1 第三人称+问号 | 1 | 4 | 1 |
| P2 第一人称/自然叙述 | 1 | 4 | 1 |
| P3 无显式问号 | 1 | 3 | 2 |

第一人称与第三人称完全同档，没有证据支持第一人称为内容缺陷。无问号版本下限略低，
但失败主要来自生成器输出质量，不是缺少 `？` 本身。句末问号最多是 UI/主持提示，
可在展示层统一加“请找出真相”，不应触发一次全 schema LLM repair。

## 实验 6：fair clue

FC-ON 为 1 想玩 / 2 可以玩 / 3 不想玩；FC-OFF 为 0 / 1 / 5。这里不能得出“删掉
fair clue 更好”。FC-OFF 的模型多次直接输出编辑说明，且即使格式正常，也容易让核心
机制成为汤底里的随机隐藏背景。

需要改的是定义：公平性应该意味着“汤面有一个真实异常锚点，canonical QA 中存在能
逐步确认关键事实的路径”，而不是“至少一段 `fair_clue.quote` 逐字出现在 puzzle，且
直接支持 required atom”。后者会把可通过问答获得的事实误当成必须预先揭露的信息，
并让 `clue_recontextualized` 被错误绑定到某一条 quote。

## 实验 7：discovery beats

DB1（允许 1~4）最能忠实标注自然层次；DB2（强制 2~4）在 S01、S03 等单核故事中
出现同义拆分和解释性复述；DB3（optional）有时仍由模型自发写 3~4 条，说明仅改数字
不能完全消除 schema 诱导。建议允许 1~4，且把 beats 当 playtest/提示编排 metadata；
`reasoning_beats_nonredundant` 应是 offline signal，不应否决一个漂亮单核 reveal。

## 实验 8：blind review

盲评集在固定 seed 下匿名打乱；评审请求不含任何 variant 标签。99 题分布为：

- 想玩：36
- 可以玩：43
- 不想玩：20

局限：生成与盲评虽然是独立无状态上下文，但仍是同一个 `deepseek-v4.1-flash` 模型；
没有真人直播玩家，也没有估计真实提问轮数。模型会把近似候选互相比较，并对重复项
降级，这符合完整盲评集的现实，但会影响单 variant 的绝对分布。所有结论都应作为下一轮
人工盲评和线上 shadow eval 的假设，而不是直接上线依据。

## 逐项审计 production 规则

1. **First-person fix**：从 fixable 降为合法形式。第一人称本身不损害真实性，日记、
   录音、对话还能增强氛围。只对“角色认知”与“全知事实”的归属做 truth audit。
2. **Closing question**：不是游戏必需，是格式偏好。改为 soft 或由 UI 添加统一提示；
   不应触发全结构 Reviewer。
3. **`PUZZLE_HARD_MAX_LEN=220`**：保留 hard。固定大屏 puzzle viewport、overflow 与
   实际滚动回归构成真实 UI 合同；它与创作风格无关。
4. **`ANSWER_HARD_MAX_LEN=300`**：同样保留 hard，属于揭晓展示/runtime 合同。
5. **`fair_clues`**：保留“真实锚点+QA 可达”的 hard 目标；移除“quote 必须直接支撑
   required atom”的硬绑定。quote 可作回看高亮 metadata。
6. **`MIN_DISCOVERY_BEATS=2`**：改为允许 1~4；对天然单核题 beats 可 optional。
7. **`reasoning_beats_nonredundant`**：AI-original 目前仍是 hard，降为 soft/offline eval。
8. **`clue_recontextualized`**：从“某条 fair clue 意义必须变化”改为“整个 surface/scene
   至少一个元素在揭晓后被重新理解”，且单独作为 soft taste signal，不替代真实性。
9. **`dramatic_payoff`**：降为 soft/offline eval。它是审美判断，模型方差大；保留 hard
   会把温和但漂亮的机制题和单核题一起丢掉。
10. **`core_answer_direct`**：保留 hard。它约束主持人通关落点和 reveal，不要求汤面
    提前泄露答案，属于 runtime 合同。
11. **`completion_contract_minimal`**：保留 hard。1~2 个最小完成事实防止永不通关；
    但 Stage B 产出 3 条时应允许确定性压缩或一次结构修复，而不是让 surface 承担代价。
12. **death/grief/past_trauma quotas**：latest main 已将 cross-puzzle 饱和降为 soft，方向
    正确。继续限制的是 `past_trauma + repeated_ritual + grief/memorial` 的模板重复，
    不是 death 本身。红汤/黑汤允许死亡；死亡不能单独充当 reveal。

## Top 5 最该放宽规则

1. 规则：第一人称自动修复
   文件：`story/quality.py`、`story/puzzle.py`
   当前行为：第一人称为 fixable，Reviewer 要改第三人称。
   为什么伤害题感：P2 与 P1 盲评分布相同；S04 因此触发全 schema repair 并丢题。
   建议：第一人称合法，只审计说话者知识边界和字面真实性。
   风险：角色信念可能被误读成全知事实；由归属明确的 truth audit 控制。

2. 规则：谜面必须以问号结尾
   文件：`story/quality.py`、`story/llm.py`
   当前行为：无句末问号为 fixable，并触发 Reviewer。
   为什么伤害题感：S02/S03/S05 只为补一句话支付完整 LLM repair 成本。
   建议：改为 soft；直播 UI 单独显示“请找出真相”。
   风险：极少数陈述不清楚调查目标；由 concrete anomaly 检查解决。

3. 规则：`MIN_DISCOVERY_BEATS=2` + `reasoning_beats_nonredundant` hard
   文件：`story/quality.py`、`story/llm.py`
   当前行为：当前 policy 要求 2~4，AI-original Reviewer 还要求各层非同义。
   为什么伤害题感：DB2 在单核故事里制造解释性拆句；漂亮单反转被迫伪复杂。
   建议：允许 1~4；beats optional metadata；nonredundant 只作 soft signal。
   风险：过薄候选增加；由 QA 可达性和 blind playability 控制。

4. 规则：fair clue quote 必须逐字出现并直接支撑 required atom
   文件：`story/quality.py`、`story/llm.py`
   当前行为：缺 quote、quote 不在 puzzle、无 required-atom path 都是 hard。
   为什么伤害题感：混淆“能通过 QA 获得”与“必须提前给出”，提高 HIGH 暴露压力。
   建议：hard 只保留真实异常锚点和 canonical QA 可达；quote/atom link 作 metadata。
   风险：完全关闭会随机；FC-OFF 5/6 不想玩证明不能把公平性一起删掉。

5. 规则：`dramatic_payoff` / `clue_recontextualized` 作为 AI-original hard gate
   文件：`story/llm.py`
   当前行为：任一 false 都可拒稿；后者还绑定 fair clue。
   为什么伤害题感：S10 整体场景有清晰重读，却因单条 clue 未重释被拒。
   建议：payoff 降 offline/soft；recontextualization 改看整体 scene element。
   风险：普通说明题会增加；应用盲评/池偏好排序，而非正确性 hard gate。

另一个应单独修的工程问题是 Reviewer repair 粒度：surface-only format fix 不应要求模型
重吐整套 facts/atoms/clues/beats。它不是创作规则，但在本批造成了两个明确损失。

## Top 5 绝对不能放宽

1. **Narrator truthful + mechanism consistent**：谜面可误导，不能用无归属事实撒谎；
   S02 的复刻旧宅冲突证明该门必要。
2. **Canonical facts / answer 一致且 QA 可判定**：主持人的“是/否/无关”必须有稳定
   canonical world；开放答案空间不等于事实自相矛盾。
3. **`core_answer_direct` + `completion_contract_minimal`**：保持可通关和明确 reveal；
   这是 runtime 合同，不是要求汤面提前给答案。
4. **No external media + livestream safety**：必须纯文字可玩，并保留内容安全、超时和
   fail-closed 边界；超自然许可不等于放宽伤害细节。
5. **220/300 UI hard max**：保留真实大屏展示上限。若未来 UI 合同改变，应先以几何测试
   证明，而不是由 prompt 任意突破。

此外，“完全依赖冷门知识”和“关键事实无法通过任何提问确认”仍应 hard reject；它们与
本轮放宽形式约束无冲突。

## 五个明显值得保留、却被 production 损失的 case

这里的“变好”指：放宽形式/元数据门后直接保留 RAW，而不是人工重写故事。

### BETTER-01 / S04：第一人称鼠声

原始汤面：

> 我从小听着天花板上的“老鼠爬”长大。那声音总在傍晚出现，节奏很稳，从卧室正中央
> 传下来。可鼠夹、粘鼠板、鼠药一次都没用上，夹层里也找不到鼠粪、啃痕。每逢声音
> 响起，爸妈都会对视，说“等孩子放暑假再修”。十几年后检修口被掀开，我才知道——
> 上面从来没有老鼠。

汤底：楼上漏水，邻居用长杆敲地提醒；楼下父母一直误认成鼠声。

current 处理：第一人称和无问号同时进入 fix，Reviewer 因未非空回传
`discovery_beats` 拒绝整题。放宽后保留 RAW 更像海龟汤：叙述自然、每个异常都有落点，
盲评 P062 为“想玩”。

### BETTER-02 / S10：从公司名单上消失

原始汤面：

> 他居家上班三年，从没迟到过。那天电脑上不了网，怎么也登不进公司的页面。他报修、
> 刷了一天网页，灯都是绿的。傍晚收到消息：“你已经被解雇了，原因从未出现过。”
> 手机流量明明好好的。他做错了什么？

汤底：重装系统删除了公司监控客户端，服务器从此收不到在线记录，系统判定他长期
“从未出现”。

current 处理：Reviewer 因 `clue_recontextualized=false` 拒绝。放宽后看整个场景，
“上不了网/从未出现”在 reveal 后已经完成意义重读；P080 为“想玩”。

### BETTER-03 / S08：空别墅打鼾

原始汤面：

> 城郊别墅空置十年，家具搬空、连电都断了。看守人每到深夜都听见二楼有人打鼾，
> 声音还会慢慢挪到别的房间。他逐间推门，里面一个人也没有。什么在“睡觉”？

汤底：流浪猫睡在屋顶夹层和通风管，呼噜被金属管放大并随换窝移动。

current 处理：Stage B 多给了一条 core，Reviewer 只需改 kind，却顺手改了 f8 的
`hintable`，授权 diff fail-close。放宽的不是真实性，而是让 metadata 分类做确定性修复；
RAW P031 为“想玩”。

### BETTER-04 / S06：“放生”与“放出来”

原始汤面：

> 旧桥落成时有人放鞭炮，说“在桥上放生”。三个月后水下打捞出女尸，手里攥着红漆
> 木条；栏杆内侧满是抓痕，却无人报案失踪。当地人都说那天是“放生”，但谁也说不出
> 放的是什么。

汤底：被拐女子多年后被“放出来”，跑上桥后坠水；围观者听到“放了”，口耳相传成
“放生”。

current 处理：Stage B 两次技术调用仍无 payload，整题退出。不是内容规则导致，说明
技术失败必须与内容拒绝分账；RAW P082 为“想玩”。

### BETTER-05 / S03：监狱会见室

原始汤面：

> 每月第一个周三，他坐在会见室靠窗的位置等铁门打开。今天他把剥好的鸡蛋和酱菜
> 摆在桌上，直到铃响父亲也没来。狱警接电话后在他耳边说了一句。他愣了很久，然后
> 笑了，眼泪却往下掉。

汤底：他在服刑；父亲四年按月来探监，这次前一天去世，临终仍惦记鸡蛋。

current 处理：Reviewer 只补问号，truth audit 却用“靠窗/铁门哪一侧”和“狱警通常
如何通知”等未在 canonical facts 中定义的制度常识判冲突。题底本身“死亡+纪念”偏弱，
应由 taste signal 降级，而不是伪装成真实性冲突。RAW P015 为“想玩”。

## 五个放太松或生成本身失败的 case

### FAIL-01 / S02：误导变成撒谎

汤面断言“醒来时他真的站在童年旧宅”，汤底却说那是复刻房；又把“药水用完”从物理
事实改成“利用价值耗尽”的隐喻。即使 P002 认为有趣，也必须 truth reject。放宽题感
不能放宽字面真实性。

### FAIL-02 / S01：没有 reveal 的暖故事

两通未接、空摊和热炉最终只是父亲提前收摊接女儿，“其实什么都没发生”。信息量不算
太少，但认知变化太弱。它证明 Stage A 可以在任何后处理之前就平庸，删 gate 无法救。

### FAIL-03 / P003：FC-OFF 变成编辑说明

汤面直接写“汤面缺少每月、四年、三小时车程……关键遗言被略去”，不再是故事，而是
对自己缺线索的检讨。盲评“不想玩”。完全关闭 fair clue 既不等于留白，也不等于公平。

### FAIL-04 / P038：超自然堆料但无规则

停摆老钟第十三响本可成立，却在汤底同时塞入“亡妻忌日归来”和“更早原住民呼吸声”
两个无关联存在。氛围有了，canonical rule 没有，玩家只能乱猜。Strange World Allowed
必须与内部规则和 QA 可达性成对出现。

### FAIL-05 / P033：few-shot 退化成字谜

“一根线，千万结……（打一社会现象）”，答案是网络。这不是海龟汤，没有人物事件、
canonical world 或逐步问答空间。few-shot 不应只教“意义翻转”，还要明确排除纯字谜、
散文谜和打一物。

另一个保留的失败是 S08 的 V1：模型把空别墅擅自改成“深山古庙”，汤底仍是别墅管道。
实验没有人工修正它；这证明 shadow editor 也需要冻结事实和 deterministic consistency。

## A~K 直接回答

**A. 第一次明显让内容变差的位置？** 对 3/10 是 Stage A 已平；对好候选，主要是
Reviewer/schema repair 和 hard taste checks 的淘汰，不是 Stage B 文本改写。另有 1 个
Stage B 技术失败、1 个 validator 合同失败、2 个 truth audit 拒绝。

**B. Stage A 本身是否平庸？** 部分是。S01/S07/S09 已平；S04/S06/S08/S10 的 RAW
很好，说明后处理也确实会损失好题。两种原因同时存在。

**C. 哪种暴露量最像海龟汤？** MEDIUM 最稳；LOW 可用于少数强主持题；HIGH 最易变
阅读理解。

**D. 两个关键词有帮助吗？** 10 题全部至少使用一个，6/10 在汤面+汤底中逐字使用两个。
S02/S06 的组合提供了高价值钩子，S01/S09 也会把普通联想固定下来。没有单关键词对照，
不能声称因果优势。

**E. reality/neutral/strange？** Neutral 与 Strange 的可玩性下限相同且优于 Reality
Preferred；但 Strange 抽象指令没有真的增加超自然。推荐 neutral hard boundary + strange
soft sampling。

**F. few-shot 更有效吗？** 能提高强冲击样本和超自然出现率，但没有提高本批总盲评，
且增加字谜/堆料方差。方向有效，示例仍需加入形式反例。

**G. 第一人称合法吗？** 应合法。

**H. 末尾问号继续 hard？** 不应。改 UI/soft。

**I. fair clue 在泄露吗？** 直接 required-atom quote 会产生 HIGH 压力；但完全关闭更差。
重定义而非删除。

**J. discovery beats 制造伪复杂吗？** 会，尤其单核题；允许 1~4/optional。

**K. 哪些 Reviewer hard gate 降 soft？** `dramatic_payoff`、
`reasoning_beats_nonredundant` 降 soft/offline；`clue_recontextualized` 改整体场景定义后作
soft taste signal。`narrator_truthful`、`mechanism_consistent`、`core_answer_direct`、
`completion_contract_minimal`、`livestream_safe` 保持 hard。

## 建议的最小必要约束集

```text
继续 hard：
  字面真实 / 机制自洽 / canonical facts 一致
  QA 可判定且有可达路径
  core answer 直接、completion 最小
  纯文字、直播安全、220/300 UI 合同
  不以冷门知识作为唯一答案

改 soft 或 metadata：
  第一/第三人称、句末问号
  clue 数量、逐字 quote、beat 数量
  多层反转、dramatic payoff
  现实/超自然偏好、死亡/悲情跨题饱和
  整体 recontextualization 的审美强度
```

这套边界允许题目一开始高度开放，但要求主持人的 canonical world 明确，并能通过
Yes/No 问答逐步缩小空间。它不追求现实世界数学意义上的唯一解释。
