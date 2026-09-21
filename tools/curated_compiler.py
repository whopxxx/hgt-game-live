#!/usr/bin/env python
# coding: utf-8
"""H2: Curated Compiler —— **AI 是题库编辑, 不是出题人**。

## 这个模块存在的理由

现状: 每道题都由 AI 现场造。代价不只是慢 —— 是**风格**。实播反复出现

    特殊岗位规则 / 设备冷门功能 / 某制度规定 / 某系统真实用途

这类题**逻辑上成立, 但没有认知反转**: 观众听完谜底的反应是"哦", 不是
"原来如此"。而网上现成的好题(SE 的 situation / TurtleBench)里, 身份 /
时间 / 空间 / 视角 / 物品意义 这类真反转的密度高得多。

所以 Batch H 的路线是**换题源**, 而不是继续调 prompt 让 AI 写得更像。

## 铁律: 不许重新创作故事

    surface(谜面) 与 bottom(谜底) 是 **canonical source**。

AI **可以**:
    翻译(英->中) / 精简表述 / 结构化(core_answer/facts/beats/...)

AI **不可以**:
    改写核心真相 / 添加新的关键因果 / 为通过 schema 编造不存在的线索

如果原题不适合直播 -> **reject**, 而不是"修成另一题"。这是本模块与
`PuzzleWriter.gen_spec` 最根本的区别: gen_spec 的任务是**发明一道好题**,
本模块的任务是**把一道已有的好题搬进我们的 schema**。

## 为什么不复用 gen_spec

`gen_spec` 的第一步是调 `emit_riddle` 让模型**写一个新谜题**。对 curated
题那一步是**有害**的 —— 它会顺手把故事改了。所以:

    gen_spec:      emit_riddle(发明) -> validate -> review -> audit -> gate
    curated:       translate/compile(搬运) -> validate -> review -> audit -> gate

后四道门**完全一样**(复用现有实现, 不自建一套)。差的只是第一步。

## 复用哪些(不自建)

    validate_spec              结构/通关合同硬门
    PuzzleWriter._review_spec_with_retry  Reviewer(带技术重试)
    PuzzleWriter._audit_with_retry        truth audit
    quality.too_similar        文本去重

## **不**复用的: `cross_puzzle_gate`(题型分布)

产品边界: **题型 / recent-10 / diversity quota 只对 AI 原创链具有硬约束。
下载获得的 external curated 不得因为题型分布被拒绝。**

`cross_puzzle_gate` 的输出在 curated 编译里只作为**非阻塞 diagnostic**
落进 `info["diversity_signals"]`, 永远不 `continue`。原因:

    observed classification  !=  admission requirement

那些维度(mechanism / shape / death / grief / domain / relation / emotion /
reveal_mode / procedural / dark-tone / 结构等价)是 **selection metadata** ——
live pool 的 Pass 1 拿它们做**软排序**(H4-E), 而不是准入判据。

⚠️ 早先这里是 `xbad = cross_puzzle_gate(...) -> continue`。当时 LazyCurator
恰好传 `recent=[]`, 所以**没有触发** —— 是 latent policy bug。不要"修回去"。

**不新增第四个内容审核 LLM**(任务书明确)。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from story.puzzle import (
    DiscoveryBeat, FairClue, PuzzleBlueprint, PuzzleFact, PuzzleSignature,
    PuzzleSpec, SolveAtom, quote_in_puzzle,
)
from story.quality import (
    QUALITY_POLICY_VERSION, Quotas, cross_puzzle_gate, validate_reveal_adherence,
    validate_spec,
)

log = logging.getLogger("hgt.compiler")

#: curated 编译的 prompt 版本。与 riddle/check 分开 —— 它是**另一条链**,
#: 混用同一个版本号会让"这批题是哪条链产的"无法区分。
CURATED_PROMPT_VERSION = "curated-v1"

#: **准入政策**版本(H3-A)。与 `QUALITY_POLICY_VERSION` **刻意分开**。
#:
#: 为什么不能共用: quality-v8 是**AI 原创题**的内容政策(通关合同 / 发现
#: 层次 / 基调配比…), 而这里要管的是**另一件事** —— "一道外部来的题,
#: 够不够格算海龟汤"。两件事的修订节奏完全不同: 我们可能为了收紧题型
#: 把 curated 提到 v3, 却一点都不想动原创链的政策(反之亦然)。共用一个
#: 号会让"改了一边"看起来像"两边的题都过期了"。
#:
#: 它同时是**旧库存的隔离开关**:
#:     spec.curated_policy_version != CURATED_POLICY_VERSION
#:     -> 过不了题池准入门 -> 不计库存 / 播不出
#: 于是 H2 那批按 v1 收的题(含 q10000 那种)会**自动**失去 live
#: eligibility, 不需要任何人手工删文件。
#:
#: ## v3: 外部知识依赖(§五/§六)
#:
#: v2 的三条(story_reconstruction / multi_step_deduction / single_trick)
#: 把"卡车烧油"那一类挡掉了, 但**实测还有三类漏网**:
#:
#:     turtlebench:7c53678ff933  十八楼按不到按钮        <- 经典脑筋急转弯
#:     pse:q:106200              吉普车泥泞车辙          <- 纯物理单机制
#:     pse:q:103926              2<3 看成心形            <- 平台/渲染冷知识
#:
#: 三个的共同点不是"物理"或"电梯", 而是:
#:
#:     谜底依赖一个**普通观众不知道的外部知识点**才有机会解出
#:
#: 产品要求(已冻结): 观众必须能只靠 谜面 + 是/否问答 + **普通生活常识**
#: 逐步恢复故事。知道某个外部知识点才有机会解出的题**不适合直播**。
#:
#: 所以 v3 加一条正交判据 `no_external_knowledge_dependency`, 而**不是**
#: 给 elevator/physics/jeep/render 写关键词黑名单 —— 黑名单挡不住下一个
#: 没被想起来的词, 而且会误伤真故事(一道关于电梯的**身份**题是好的)。
CURATED_POLICY_VERSION = "curated-v5"

#: v5 是一次**产品政策调整**, 不是又一次收紧(任务书 §一)。
#:
#: v2~v4 这条链一直在追"每一道都是高难度、多层故事型海龟汤"。那个目标
#: 对**直播**是错的。直播要的是:
#:
#:     内容合规 / 观众能参与 / 谜底揭晓说得通 / 有一点趣味 —— 即可。
#:
#: 于是 v5 把判据**劈成两半**:
#:
#:   hard gate(内容不合格, 仍然 reject)
#:     clear_anomaly / yes_no_progress / reasonable_explanation /
#:     no_obscure_system / no_external_media / livestream_safe
#:
#:   soft signal(**不得单独导致 rejected**, 只落账本/排序)
#:     not_pure_puzzle / has_reversal / detail_recontextualized /
#:     story_reconstruction / multi_step_deduction / single_trick
#:
#: 这一刀砍掉的是"因为不够精彩而拒一道能玩的题"。它**没有**动 safety,
#: 也没有动"谜底要能解释谜面" —— 那两条是产品底线, 见 §十/§十六。
#:
#: ⚠️ v4 的 decision **不继承**(版本号变了, 账本按 policy 隔离), 所以
#: v4 里被故事门误杀的题会在 v5 下重新审一次。这是刻意的。

#: v5 的**真硬门**。唯一一处定义, prompt / 代码门 / Reviewer 契约 /
#: 测试全部读它 —— 任何"再抄一份清单"的写法都会在下一次改政策时漂移。
#:
#: ⚠️ `unique_explanation` 这个**字段名**保留(外部题源/prompt 已在用),
#: 但语义在 v5 变了:
#:
#:     v2~v4: 谜底必须**唯一**解释谜面(数学意义的唯一解)
#:     v5:    谜底要**具体且自洽**地解释谜面主要反常点
#:
#: 换句话说, 不再用"现实世界只能有这一种可能"卡题 —— 那会把大量
#: 正常的海龟汤判死, 因为任何故事在现实中都可能有别的解释。要求的是
#: "不是随便编一个同样可能的背景", 即:
#:
#:     谜底能具体、合理地解释谜面主要反常点
#:
#: 代码层无法判定"具体"与"自洽", 所以这一条**由模型判**(见 prompt),
#: 代码只判它有没有被回答(fail closed)。
CURATED_HARD_CHECKS = (
    "clear_anomaly", "unique_explanation", "yes_no_progress",
    "no_obscure_system", "no_external_media", "livestream_safe",
)

#: v5 的 soft signal —— 继续计算、落账本、供以后排序, 但**不得单独
#: 导致 rejected**(任务书 §三/§十二)。
#:
#: 保留它们是有价值的: §十二 说库存充足时优先更有反转/更曲折的题,
#: 库存不足时简单题一样能播。没有这几个信号就做不到那个排序。
#:
#: ⚠️ `single_trick` 仍然是**反向**信号(true = 更简单), 但它的方向
#: 从此只影响**排序**, 不再影响**准入**。
CURATED_SOFT_SIGNALS = (
    "not_pure_puzzle", "has_reversal", "detail_recontextualized",
    "story_reconstruction", "multi_step_deduction", "single_trick",
)

#: v4 修的是一个**污染源**, 不是收紧判据(任务书 §一~§三)。
#:
#: v3 之前, `spec_from_tool` 里写着:
#:
#:     blueprint=bp or PuzzleBlueprint()
#:
#: Lazy Curator 正确传了 `blueprint=None`(curated 题**没有** target
#: Blueprint —— 题目已经存在, 我们只是搬运它), 但那个 `or` 把 None 变成
#: 了一份**带真实默认约束**的 Blueprint, 随后 `_review_spec` 又把它当
#: **硬约束**印进审稿 prompt:
#:
#:     【本题 Blueprint 硬约束(题若违反它就是不合格)】
#:
#: 于是已有 canonical 题会因为"不是 stranger / 不是 neutral / 不是
#: instant / 不是 information_gap / 包含 death / 包含 past_trauma"被
#: 要求推倒重出。那不是内容不合格, 是**把 AI 原创题的目标骨架套到了
#: 外部已有题上**。
#:
#: 职责必须分清:
#:
#:     AI original:  choose Blueprint -> 创作符合它的题 -> Reviewer 查 adherence
#:     curated:      已有 canonical puzzle -> 判断它**本身**好不好 -> 结构化
#:
#: v4 起 curated 链**不传** target Blueprint 给 Reviewer, 且
#: `spec.blueprint` 允许为 None("无目标约束")。观察到的 signature 仍然
#: 照常记录 —— observed classification != target requirement。

#: curated 题的 `blueprint` 字段用**这一个**哨兵实例表示"无目标约束"。
#:
#: 为什么不是 `None`: `PuzzleSpec.to_archive()` / 池读取 / `cross_puzzle_gate`
#: 都直接摸 `spec.blueprint.<field>`, 让它是 None 会在那些地方炸。一个
#: 全字段"无要求"的实例既满足"必须存在"的旧契约, 又不会假装自己是目标。
#: 判别方式是 `is_unconstrained_blueprint()` —— **不是**比字段值(默认值
#: 恰好长得像它, 拿值比较会把真默认 Blueprint 误判成无约束)。
def make_unconstrained_blueprint() -> PuzzleBlueprint:
    """构造"无 target 约束"的 Blueprint 实例。

    ⚠️ 它的字段值与 `PuzzleBlueprint()` 默认值**完全相同** —— 这是刻意
    的(它必须是一个合法 Blueprint)。区分它靠的是**身份**
    (`is_unconstrained_blueprint`), 不是值。
    """
    bp = PuzzleBlueprint()
    bp._unconstrained = True          # type: ignore[attr-defined]
    return bp


def is_unconstrained_blueprint(bp: Any) -> bool:
    """这份 blueprint 是"没有目标约束"吗?

    判据是**显式标记**, 不是值比较。值比较会有一个真实误判:
    `PuzzleBlueprint()` 的默认值长得和它一模一样, 而那份默认值在
    AI 原创链里是**真指令** —— 把它当成"无约束"会让原创链静默丢掉
    审稿人的 adherence 检查。
    """
    return bool(getattr(bp, "_unconstrained", False))


# ======================================================================
# 提示词
# ======================================================================
CURATED_COMPILE_SYSTEM = """你是**题库编辑**, 不是出题人。全程用中文。

你会拿到一道**已经存在**的谜题: 它的谜面(surface)与谜底(bottom)。
你的任务是把它们**搬进**我们的结构化 schema, 而**不是**重新创作。

═══ 铁律: surface 与 bottom 是 canonical source ═══

**允许**:
  - 把英文谜面/谜底**翻译**成中文(逐句对应, 不合并不省略)
  - **精简表述**: 删掉与谜底无关的装饰性铺陈, 但不得删掉任何**推理
    需要**的细节
  - 把谜底拆成结构化字段(core_answer / facts / discovery_beats / ...)

**禁止**(任何一条都会让这道题被代码判不合格):
  - **改写核心真相** —— 谜底讲的机制必须与原文**完全一致**
  - **添加新的关键因果** —— 原文没有的人物、动机、机关一律不许加
  - **为通过 schema 编造不存在的线索** —— fair_clue 必须逐字来自谜面
  - 因为"这样更戏剧化"而调整情节

如果你判断**这道题本身不适合**我们的直播, 请把 `accepted` 设为 false
并给出 `reject_reasons`, **不要**试图把它改成另一道题。

═══ 判据(逐条回答) ═══

1. `clear_anomaly`  谜面是否形成一个**清楚的反常点**?(观众听完会想
   "这不对劲")
2. `unique_explanation` 谜底是否能**具体、合理地解释谜面的主要反常点**?

   ⚠️ **不是**要求"现实世界只能有这一种可能"。那是数学意义的唯一解,
   任何正常故事在现实中都可能有别的解释, 用它卡题会把大量能玩的题
   判死。

   要的是: **谜底不是随口编的一个同样可能的背景** —— 它得真的指向
   谜面那个反常点, 而不是"也可能是别的"。自问: "听完谜底, 那个
   '不对劲'的地方被解释掉了吗, 还是只是换了个说法?" 后者 false。

3. `yes_no_progress` 能否通过是/否问答**逐步逼近**谜底?
4. `no_obscure_system` 是否**不依赖**冷门职业制度、设备冷门功能、
   某系统真实用途这类"查了才知道"的知识?
5. `not_pure_puzzle` 是否**不是**数学题 / 字谜 / 图形题 / 纯知识问答?
6. `has_reversal` 是否有**认知反转** —— 身份 / 关系 / 时间 / 空间 /
   视角 / 物品意义 / 因果 中至少一种, 揭晓后要让人"重新理解一遍"?
7. `detail_recontextualized` 揭晓后, 谜面里至少有一个细节的**意义
   发生了变化**?
8. `no_external_media` 是否**不需要**看图片 / 表格 / 附件就能玩?
9. `livestream_safe` 内容是否适合公开直播展示?

**1~4 与 8~9 是硬门。** 有一条不满足就 `accepted=false`。

**5~7 只是信号** —— 它们**不构成拒绝理由**。一道题没有反转、
不是纯谜题之外的什么, 只要还能玩, 就可以收。填 false 不等于拒题,
它只是告诉代码"这道题偏简单", 以后库存充足时会优先播别的。

═══ 关于 10~13: 只看第 13 条 ═══

10. `story_reconstruction`  玩家最后是不是在**重建一个故事模型**?
11. `multi_step_deduction`  有没有至少两个彼此不同的发现阶段?
12. `single_trick`          ⚠️ 反向: true = 只有一个知识点。
13. `no_external_knowledge_dependency`  观众只靠谜面 + 是/否问答 +
    **普通生活常识**有没有机会解出来?

**只有第 13 条是硬门。10 / 11 / 12 是信号, 照样不影响 accepted。**

必须说清楚: 这三条不理想**不是缺陷**。一道"十八楼够不到按钮"式的
单点脑筋急转弯在直播里**很好用** —— 观众能参与、揭晓说得通、有点
趣味。它 `single_trick=true`、`multi_step_deduction=false`, 但那
**不是拒绝的理由**。如实填, 让它进信号统计, 题照收。

**第 13 条(硬门)的判据 —— 直播口径:**

允许(不算外部知识, 填 true):
  - 日常生活常识
  - 简单直觉物理(东西会掉、水会湿、火会烫)
  - 常见物品用途(伞挡雨、钥匙开锁)
  - 普通社会经验(人会撒谎、要还钱、上班要打卡)

不允许(解题**必须**知道它才解得出来 -> 填 false):
  - 专业知识(法律/医学/工程/化学的具体条文或数值)
  - 行业内部规定、机构制度、办事流程
  - 冷门设备功能(绝大多数人没见过它怎么用)
  - 罕见科学知识(需要定量关系才想得到的物理/化学)
  - 特定网站 / 软件 / 平台的机制或渲染行为
  - 只有知道某个专有事实才能解(某零件的名字、某平台的规则)

判据(自问): **"揭晓之后, 普通观众是会说'哦, 原来如此', 还是会问
'这个规则是什么, 我根本没听过'?"**

  前者 -> 可以。
  后者 -> `no_external_knowledge_dependency=false`, 拒。

⚠️ 这一条**不是**"不能涉及专业内容": 题里出现医生、出现电梯、出现
汽车都没问题。不合格的是**解题必须知道那个外部知识点**。

**其余判据: 如实填, 不要为了"让题通过"而改答案。**

⚠️ 常见的错误判断(请自查):
  - "这题不够曲折" -> 曲折是**信号**, 不是准入门。简单题可以收。
  - "只有一个知识点" -> 同上。填 `single_trick=true`, 题照收。
  - "没有反转" -> 同上。填 `has_reversal=false`, 题照收。
  - "逻辑成立但没意思" -> "没意思"不是拒绝理由; 判断它**能不能玩**。
  - "现实里确实有这样的设备规定" -> 成立 != 公平。冷知识题在直播里
    **猜不出来**, 那才是问题(第 13 条)。

═══ 翻译(C 部分) ═══

若原题是英文:
  - **事实一一对应**: 数字 / 关系 / 时间 / 地点一律不改
  - **不是文学改写**: 不要润色成散文, 不要加形容词
  - 若这题**依赖英文词形或双关**无法翻译 -> `accepted=false`,
    `reject_reasons=["language_dependent"]`, **不要硬翻**

═══ 结构化(D 部分) ═══

- `core_answer`: 一句话(≤60 汉字, **不换行**)直接回答谜面最后那个问句。
  普通人一听就懂。
- `answer`: 完整谜底(2-4 句)。**第一句必须正面解释核心反常**。
- `completion_fact_ids`: **通关合同**, 1~2 条。它回答"房间最少要公开
  确认哪几件事, 这题就算解出来了"。**不是**"谜底要点"。
  只能指向 kind=core 且 visibility=hidden 的 fact。
  **题目允许有层次, 通关必须简单。**
- `facts`: 4~10 条。一条 = 一个可独立被问到的命题。至少 1 条 exclusion。
- `discovery_beats`: 2~4 个**发现阶段**(正常玩下来会一层层想通什么)。
  **它没有胜负权**, 只是叙事层次。至少一条指向 completion。
- `solve_atoms`: 1~4 条, 用 fact_ids 指向 facts。身份/时间/物品/因果
  反转用 `key`, 只有真有因果链才用 cause/mechanism。
- `fair_clues`: **逐字**摘自你**将要输出的 `puzzle` 字段**(不是原始
  外文 surface, 也不是任何改写前的版本)。摘一段你自己写下的连续文字,
  一模一样的字符。
  ⚠️ 这是**代码**会逐字校验的: 你引的字符串必须能在你的 `puzzle` 里
  原样找到。所以**先定稿 `puzzle`, 再从里面复制**(不要凭记忆重写一遍,
  那几乎必然差一两个字 —— 实测这是最高频的失败原因)。
  谜面里确实没有可回溯的线索 -> **reject**(不要从谜底倒灌一条 clue)。
- `hints`: 3 条, ≤30 字, 由浅入深, 不剧透。
- `signature`: 如实回传这道题的机制/解法形状/领域/关系/情绪等。

═══ 必须回传 observed_signature 与 quality_checks ═══

`observed_signature` 要**如实**反映这道题, 不是照抄某个目标。
`quality_checks` 里**十三条**判据逐条填 true/false(见上)。
**十三条都要填** —— 代码按 fail closed 判: 少填一项等于该项不合格。

⚠️ 但**只有六条会拒题**: `clear_anomaly` / `unique_explanation` /
`yes_no_progress` / `no_obscure_system` / `no_external_media` /
`livestream_safe` / `no_external_knowledge_dependency`。
其余七条是**信号**, 填 false / true 都不影响这道题收不收 ——
它们只用来在库存充足时排序。所以**如实填, 不要为了让题通过而美化**,
也不要因为某条信号不好就 `accepted=false`。

═══ 这是什么产品 ═══

**你不是在评选文学性最强的海龟汤。这是直播娱乐题库。**

简单、经典、单反转、脑筋急转弯式的题目**都可以接受**, 前提是:

    能玩 / 谜底说得通 / 不依赖冷门外部知识 / 适合直播

不要因为 `not_a_story` / `no_multi_step` / `single_trick` /
`no_reversal` 就 `accepted=false`。那些是**风格差异**, 不是不合格。
"""

_TOOL_CURATED = {
    "name": "emit_curated",
    "description": "把一道**已有**谜题搬进结构化 schema(不是新出一道题)",
    "input_schema": {
        "type": "object",
        "properties": {
            "accepted": {
                "type": "boolean",
                "description": ("**六条硬门全部为 true** 才填 true; "
                                "否则 false。(信号类判据不参与这个决定)"),
            },
            "reject_reasons": {
                "type": "array", "items": {"type": "string"},
                "description": ("accepted=false 时必填。可用: "
                                "language_dependent / no_anomaly / "
                                "depends_on_obscure_system / "
                                "needs_external_media / unsafe / "
                                "no_reasonable_explanation / ambiguous / "
                                "external_knowledge_dependency / "
                                "cannot_progress_by_yes_no"),
            },
            "quality_checks": {
                "type": "object",
                "properties": {
                    "clear_anomaly": {"type": "boolean",
                                      "description": "硬门"},
                    "unique_explanation": {
                        "type": "boolean",
                        "description": (
                            "硬门。谜底能否**具体、合理地解释谜面主要"
                            "反常点**?\n"
                            "⚠️ **不是**'现实世界只能有这一种可能' —— "
                            "那是数学意义的唯一解, 会误杀大量正常题。\n"
                            "要的是: 谜底不是随口编的一个同样可能的背景, "
                            "而是真的解释掉了那个'不对劲'。"),
                    },
                    "yes_no_progress": {"type": "boolean",
                                        "description": "硬门"},
                    "no_obscure_system": {"type": "boolean",
                                          "description": "硬门"},
                    "not_pure_puzzle": {
                        "type": "boolean",
                        "description": ("**信号**(不影响准入)。不是数学/"
                                        "字谜/图形/纯知识问答。"),
                    },
                    "has_reversal": {
                        "type": "boolean",
                        "description": ("**信号**(不影响准入)。揭晓后要"
                                        "重新理解一遍。没有反转的题照样"
                                        "可以收。"),
                    },
                    "detail_recontextualized": {
                        "type": "boolean",
                        "description": ("**信号**(不影响准入)。某细节揭晓"
                                        "后意义变了。"),
                    },
                    "no_external_media": {"type": "boolean",
                                          "description": "硬门"},
                    "livestream_safe": {
                        "type": "boolean",
                        "description": ("硬门。适合公开直播展示。\n"
                                        "死亡作为**剧情事实**可以; "
                                        "重口、过度刺激、以极端伤害本身"
                                        "作为噱头 -> false。"),
                    },
                    # ---- v2 三条。v5 起**全是信号**。 ----
                    "story_reconstruction": {
                        "type": "boolean",
                        "description": (
                            "**信号**(不影响准入)。玩家最终是在"
                            "**重建一个故事模型**吗(身份/关系/时间/"
                            "空间/目的/因果/视角/物品意义)?\n"
                            "纯知识点 -> false。但 false **不拒题** —— "
                            "如实填即可。"),
                    },
                    "multi_step_deduction": {
                        "type": "boolean",
                        "description": (
                            "**信号**(不影响准入)。是否有**至少两个"
                            "彼此不同、都会改变玩家理解**的发现阶段?\n"
                            "单点题填 false, 但它**照样可以收**。"),
                    },
                    "single_trick": {
                        "type": "boolean",
                        "description": (
                            "⚠️ 反向字段: true = 只有一个知识点。\n"
                            "**它是信号, 不影响准入** —— 单点脑筋急转弯"
                            "在直播里很好用。如实填。"),
                    },
                    "no_external_knowledge_dependency": {
                        "type": "boolean",
                        "description": (
                            "**硬门。** 普通观众只靠谜面 + 是/否问答 + "
                            "**普通生活常识**有没有机会解出来?\n"
                            "允许(不算外部知识): 日常生活常识 / 简单"
                            "直觉物理 / 常见物品用途 / 普通社会经验。\n"
                            "不允许(填 false): 专业知识 / 行业内部规定 / "
                            "具体法律医学知识 / 冷门设备功能 / 罕见科学"
                            "知识 / 特定网站软件平台机制 / 只有知道某个"
                            "专有事实才能解。\n"
                            "判据: 揭晓后普通观众会说**'哦, 原来如此'**"
                            "(可以), 还是会问**'这个规则是什么, 我根本"
                            "没听过'**(拒绝)?\n"
                            "⚠️ 题里出现医生/电梯/汽车都没问题; 不合格的"
                            "是**解题必须知道那个外部知识点**。"),
                    },
                },
                "required": ["clear_anomaly", "unique_explanation",
                             "yes_no_progress", "no_obscure_system",
                             "not_pure_puzzle", "has_reversal",
                             "detail_recontextualized", "no_external_media",
                             "livestream_safe",
                             "story_reconstruction", "multi_step_deduction",
                             "single_trick",
                             "no_external_knowledge_dependency"],
                "description": (
                    "**十三条都要填**(代码 fail closed: 少填一项 = 该项"
                    "不合格), 但**只有六条会拒题**: clear_anomaly / "
                    "unique_explanation / yes_no_progress / "
                    "no_obscure_system / no_external_media / "
                    "livestream_safe / no_external_knowledge_dependency。\n"
                    "其余七条(not_pure_puzzle / has_reversal / "
                    "detail_recontextualized / story_reconstruction / "
                    "multi_step_deduction / single_trick)是**信号** —— "
                    "填什么都不影响这道题收不收, 只用于以后排序。\n"
                    "⚠️ **不要为了让题通过而美化信号, 也不要因为信号不好"
                    "就 accepted=false。**"),
            },
            "content_style": {
                "type": "array", "items": {"type": "string"},
                "description": ("**内容风格**标签(可多选), 如 悬疑 / "
                                "细思极恐 / 反差 / 意外 / 情感 / 亲情 / "
                                "悲剧 / 恐怖氛围 / 逻辑 / 脑洞。"
                                "与 style_tags(结构型反转)不同, 这里描述"
                                "的是**观感**。"),
            },
            "style_tags": {
                "type": "array", "items": {"type": "string"},
                "description": ("这道题的认知反转类型, 如 identity_flip / "
                                "perspective_flip / time_flip / "
                                "space_flip / object_meaning / causal_flip"),
            },
            "title": {"type": "string",
                      "description": "短标题(中文)"},
            "puzzle": {
                "type": "string",
                "description": ("谜面(2-3 句, 第三人称客观事实, 结尾是问句)。"
                                "翻译题在这里给**中文**谜面。"
                                "**不得**改变原题的事实。"),
            },
            "answer": {
                "type": "string",
                "description": "完整谜底(2-4 句, 第一句正面解释核心反常)",
            },
            "core_answer": {
                "type": "string",
                "description": ("一句话核心答案(≤60 汉字, 不换行, 直接回答"
                                "谜面最后那个问句)"),
            },
            "completion_fact_ids": {
                "type": "array", "minItems": 1, "maxItems": 2,
                "items": {"type": "string"},
                "description": ("通关合同: 1~2 条 kind=core 且 "
                                "visibility=hidden 的 fact id。"
                                "support/exclusion 绝不能填这里。"),
            },
            "facts": {
                "type": "array", "minItems": 4, "maxItems": 10,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "text": {"type": "string"},
                        "kind": {"type": "string",
                                 "enum": ["core", "support", "exclusion"]},
                        "visibility": {"type": "string",
                                       "enum": ["public", "hidden"]},
                        "hintable": {"type": "boolean"},
                    },
                    "required": ["id", "text", "kind", "visibility"],
                },
            },
            "discovery_beats": {
                "type": "array", "minItems": 2, "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "text": {"type": "string"},
                        "fact_ids": {"type": "array",
                                     "items": {"type": "string"}},
                    },
                    "required": ["id", "text"],
                },
                "description": ("2~4 个发现阶段(叙事层次, **不是**通关条件)。"
                                "至少一条指向 completion 里的 fact。"),
            },
            "solve_atoms": {
                "type": "array", "minItems": 1, "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "role": {"type": "string",
                                 "enum": ["cause", "mechanism", "key",
                                          "support"]},
                        "text": {"type": "string"},
                        "fact_ids": {"type": "array",
                                     "items": {"type": "string"}},
                        "required": {"type": "boolean"},
                    },
                    "required": ["id", "role", "text"],
                },
            },
            "fair_clues": {
                "type": "array", "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "quote": {"type": "string",
                                  "description": ("**逐字**摘自你自己输出的 "
                                                  "`puzzle` 字段的一段连续"
                                                  "文字。代码会逐字校验, "
                                                  "所以要从定稿的 puzzle 里"
                                                  "**复制**, 不要凭记忆重写。")},
                        "supports_atoms": {"type": "array",
                                           "items": {"type": "string"}},
                    },
                    "required": ["quote"],
                },
            },
            "hints": {
                "type": "array", "minItems": 3, "maxItems": 3,
                "items": {"type": "string"},
            },
            "observed_signature": {
                "type": "object",
                "properties": {
                    "mechanism_family": {"type": "string"},
                    "solution_shape": {"type": "string"},
                    "domain": {"type": "string"},
                    "relation": {"type": "string"},
                    "emotion_mode": {"type": "string"},
                    "time_shape": {"type": "string"},
                    "death": {"type": "boolean"},
                    "past_trauma": {"type": "boolean"},
                    "long_term_profession": {"type": "boolean"},
                    "repeated_ritual": {"type": "boolean"},
                },
                "required": ["mechanism_family", "solution_shape"],
            },
        },
        "required": ["accepted", "quality_checks"],
    },
}


#: ⚠️ v5 起这三份清单**不再是准入判据** —— 见 `CURATED_HARD_CHECKS`。
#: 它们现在只描述"模型会被问哪些问题"与"哪些问题归哪一类", 供:
#:
#:   - prompt 渲染(哪些是硬门, 哪些只是信号)
#:   - 账本落盘(soft signal 以后排序用)
#:   - 测试遍历(不手写清单)
#:
#: 别名 `CURATED_CHECKS` 保留给 H2 的调用方。
CURATED_CHECKS = (
    "clear_anomaly", "unique_explanation", "yes_no_progress",
    "no_obscure_system", "not_pure_puzzle", "has_reversal",
    "detail_recontextualized", "no_external_media", "livestream_safe",
)

#: v2 全量判据 = H2 那九条 + H3 新三条。
#:
#: ## 为什么新三条是**独立字段**而不是"把 has_reversal 判严一点"
#:
#: 因为那是两个不同的问题:
#:
#:     has_reversal       "有没有反转?"        <- 卡车烧油:**有**
#:     story_reconstruction "这是不是个故事?"  <- 卡车烧油:**不是**
#:
#: 把它塞进 has_reversal 会让那一条同时承担两个问题, 于是模型在
#: prompt 里看到的是"有没有反转", 判的却是"是不是故事" —— 这种
#: 名实不符是判据漂移最常见的起点。分开之后, 每一条都能被单独
#: 质疑和单独修改。
CURATED_CHECKS_V2 = CURATED_CHECKS + (
    "story_reconstruction", "multi_step_deduction", "single_trick",
)

#: v3 全量判据 = v2 那十二条 + `no_external_knowledge_dependency`。
#:
#: 为什么它是**独立**一条而不是"把 no_obscure_system 判严一点":
#: `no_obscure_system`(H2 第 4 条)问的是"要不要查冷门职业制度", 而它
#: **实测判得太松** —— 十八楼那道题在它下面是 true(按钮高度不算"职业
#: 制度"), 于是漏了进来。真正要问的是一个更宽的问题:
#:
#:     这道题的**核心解法**是不是依赖一个外部知识点?
#:
#: 物理冷知识、平台渲染规则、单一机关用途、文字游戏 … 全都属于"外部
#: 知识点", 但它们各自都不像"冷门职业制度"。所以这条把范围写全。
CURATED_CHECKS_V3 = CURATED_CHECKS_V2 + ("no_external_knowledge_dependency",)

#: v5 全量 = v3 那十三条(字段不变, 语义变了):
#:
#:   - 前九条里 6/7(`has_reversal` / `detail_recontextualized`)降为 soft;
#:   - 10~12(`story_reconstruction` / `multi_step_deduction` /
#:     `single_trick`)**全部**降为 soft;
#:   - 13(`no_external_knowledge_dependency`)**仍是硬门**, 但判据放宽到
#:     直播口径(见 prompt 与 `no_external_knowledge_reasons`)。
#:
#: 所以清单本身不变 —— 变的是**哪几条有准入权**。保留这个名字是为了
#: 让"模型要回答哪些问题"这件事不随政策漂移: 题目降级成信号之后,
#: 那些信号仍然要**问**(否则排序没有数据), 只是**不再拒题**。
CURATED_CHECKS_V5 = CURATED_CHECKS_V3

#: 模型要回答的**全部**问题(= v3 那十三条), 但只有 `CURATED_HARD_CHECKS`
#: 里的六条有准入权。
#:
#: ⚠️ 为什么不干脆只问六条: soft signal 是 §十二 排序的**数据源**。
#: 只问六条等于把"这道题有没有反转"这个信息永久丢掉, 以后想按趣味
#: 排序时**没有数据可排**, 而补回来要重跑整个语料。
#:
#: 所以: 问十三条, 判六条, 记十三条。
CURATED_CHECK_ORDER = CURATED_CHECKS_V5

#: `single_trick` 是**反向**判据(true = 坏), 其余全是 true = 好。
#: 混在一起遍历会写错方向, 所以显式列出来。
_INVERTED_CHECKS = ("single_trick",)


def check_value_ok(name: str, value: Any) -> bool:
    """一条 `quality_checks` 的值**取对了方向**吗?

    这是判据方向的**唯一定义处**。审稿链的 fail-closed 门
    (`story.llm._apply_review`) 与编译期的 `check_tool_result` 都读它。

    ## 为什么必须抽出来

    早先 `_apply_review` 写的是 `qc.get(n) is not True` —— 那对
    **反向**判据(`single_trick`: true = 坏)是错的: 一道好题会填
    `single_trick=False`, 于是被判成"未全过" -> **每一道题都被拒**。

    而且症状极具误导性: 日志里只有一句"quality_checks 未全过
    (single_trick)", 看起来像模型答错了, 实际是代码读反了方向。

    所以两处**必须**用同一个函数, 不能各写一份 —— 各写一份就是
    "两份判据迟早漂移"的老问题, 而这里的漂移代价是整条链全灭。
    """
    if name in _INVERTED_CHECKS:
        return value is False          # 反向: 必须**明确**是 False
    return value is True               # 正向: 必须**明确**是 True

#: 「故事四问」的字段名 —— **保留为信号清单, 不再是硬门**(任务书 §三/§六)。
#:
#: v2~v4 这四条是准入硬门: 任意一条不过 -> reject。v5 起它们
#: **不得单独导致 rejected**, 理由:
#:
#:     story_reconstruction=false  -> 可以 accepted(§三 明写)
#:     multi_step_deduction=false  -> 可以 accepted
#:     single_trick=true           -> 可以 accepted(十八楼那种轻量竞猜)
#:     no_external_knowledge_...   -> 见下, **唯一例外**
#:
#: ⚠️ 为什么 `no_external_knowledge_dependency` 留在这个元组里却又
#: 仍然有准入权: 它测的是"**能不能玩**"(冷知识题观众猜不出来), 不是
#: "够不够精彩"。任务书 §四 明确保留它, 只放宽语义。把它留在这份
#: 元组里是为了**落盘口径**一致(四字段一起记), 准入判定另走
#: `CURATED_HARD_CHECKS`。
#:
#: 名字保留 `STORY_GATE_FIELDS` 会让读者以为它还是门 —— 但改名会牵动
#: 账本字段名与三个测试文件。改成 `STORY_SIGNAL_FIELDS` 并提供旧名
#: 别名, 让语义在**代码里**写清楚(下面那行就是)。
STORY_SIGNAL_FIELDS = ("story_reconstruction", "multi_step_deduction",
                       "single_trick", "no_external_knowledge_dependency")

#: 兼容别名 —— 账本字段 `compile_checks` / `review_checks` 按它取四字段。
STORY_GATE_FIELDS = STORY_SIGNAL_FIELDS


def story_gate_from_review(rev: Optional[dict]) -> list:
    """把 **Reviewer 的复核**结果翻成"故事信号不理想"的原因列表。

    ## ⚠️ v5: 这个函数的结果**不再拒题**

    v2~v4 里它的返回值直接写进 `decision=rejected`。v5 起它是**纯信号**:
    调用方(`CuratedCompiler.compile_one`)只把它记进 `info["story_signal"]`
    与账本, **不改变命运**。所以本函数保留下来是为了:

      1. 落盘(§十二: soft signal 以后用于排序);
      2. 报告里能看出"这批题的层次分布如何";
      3. 让"降级"这件事有一个**单一入口**, 而不是把判定散掉。

    名字里的 `gate` 是历史遗留 —— 它现在不是门。真正的门是
    `CURATED_HARD_CHECKS`。

    ⚠️ 与原版的一处**行为差异**: 原版对"Reviewer 没回答"返回
    `["story_review_missing"]`(fail closed -> 技术失败)。v5 里 soft
    signal 缺失**什么都不代表** —— 一道题没被问"是不是故事", 不影响
    它能不能播。所以这里 `None` / `{}` 一律返回 `[]`(无信号)。

    ↑ 这一条是 §七 的关键: "缺少这些 soft 字段也不要技术失败"。
    """
    if not isinstance(rev, dict):
        return []
    out: list = []
    # 只有**明确答了且答得不理想**才算一个信号。缺项 = 没信号。
    if rev.get("single_trick") is True:
        out.append("single_trick")
    if rev.get("story_reconstruction") is False:
        out.append("not_story_reconstruction")
    if rev.get("multi_step_deduction") is False:
        out.append("no_multi_step_deduction")
    # ---- H4-D1 §三: 这两项也降成信号 ----
    #
    # ⚠️ 它们**曾经是 curated 的硬门** —— H4-D 第一版把它们当成
    # `no_external_media` / `livestream_safe` 的替身(那张假映射见
    # `story/llm.py` 的 `_CURATED_HARD_CHECK_FIELDS`)。语义不成立, 而且
    # `reasoning_beats_nonredundant` 就是 `multi_step_deduction` 的另一种
    # 说法 —— 把它当门等于把刚拆掉的门装回去。现在它们回到信号的
    # 位置: 记录、供以后排序、**不拒题**。
    if rev.get("dramatic_payoff") is False:
        out.append("no_dramatic_payoff")
    if rev.get("reasoning_beats_nonredundant") is False:
        out.append("flat_reasoning_beats")
    # ⚠️ 这一条**不是** soft —— 它是硬门, 由 `hard_check_reasons` 处理。
    # 这里**故意不报**: 一道冷知识题会同时出现在两处, 而报告里
    # "story_signal" 那一栏混进一个准入理由会让人误以为它是信号。
    return out



def check_tool_result(d: dict, *, checks: tuple = CURATED_HARD_CHECKS
                      ) -> tuple:
    """判"这道题能不能收"。返回 `(ok, reasons)`。

    ## v5: 只判**硬门**, 且不因 soft signal 拒题

    v2~v4 这里遍历的是全量十三条, 于是 `story_reconstruction=false`
    或 `single_trick=true` 会直接拒题。那正是 §三 要废掉的: 一道
    "十八楼按按钮"式的轻量竞猜很适合直播, 却因为**不够复杂**被拒。

    v5 起遍历 `CURATED_HARD_CHECKS` 六条。soft signal(含
    `single_trick` 这种反向项)**一条都不参与** —— 所以这个函数里
    不再需要 `_INVERTED_CHECKS` 的方向处理; 六条硬门全是 true = 好。

    ⚠️ **仍然 fail closed**: 硬门缺项 = 不合格。理由不变 —— 一条
    没说清自己有没有反常点的题, 凭什么信它? 放宽的是**判据的范围**,
    不是**判据的严格度**。

    ⚠️ `accepted=false` 仍然先判: 模型明确说不行时, 它给的
    `reject_reasons` 最准确, 直接采信。
    """
    reasons: list = []
    if d.get("accepted") is not True:
        reasons.append("accepted=false")
        reasons.extend(str(x) for x in (d.get("reject_reasons") or []) if x)
        return False, reasons
    qc = d.get("quality_checks")
    if not isinstance(qc, dict):
        reasons.append("quality_checks 缺失")
        return False, reasons

    # ---- 逐条 fail closed(缺项 = 不合格) ----
    #
    # v5: 方向只有一个 —— 六条硬门全是 true = 好。反向判据
    # (`single_trick`)已经**不在**这份清单里, 所以这里不再需要
    # `check_value_ok` 的分支。留着 `check_value_ok` 是因为
    # `_apply_review` 与账本落盘仍在用它(它们要处理 soft signal 的方向)。
    bad = [k for k in checks if qc.get(k) is not True]
    if bad:
        reasons.append("quality_checks 未全过: " + ", ".join(bad))
        return False, reasons

    # ---- §四: 第 13 条(冷知识)也是硬门, 但**不在**上面六条里 ----
    #
    # 它挂在 `CURATED_HARD_CHECKS` 之外是**刻意**的: 前六条问"能不能玩"
    # 的内容性质, 这一条问"公不公平" —— 两者在不同阶段被引用(prompt
    # 分两段讲, 报告也要分开统计)。所以这里显式补判一次, 而不是把它
    # 塞进那个元组让读者以为它是同一种门。
    ekr = no_external_knowledge_reasons(qc)
    if ekr:
        reasons.append("quality_checks 未全过: " + ", ".join(ekr))
        return False, reasons
    return True, []


def story_gate_reasons(d: dict) -> list:
    """**soft signal** 的独立判定, 返回"不理想"的原因(理想则空列表)。

    ## ⚠️ v5: 这个函数**不再拒绝任何题**

    v2~v4 里它读编译模型自报的 `quality_checks`, 任意一条不理想就
    reject。v5 起它的唯一用途是**落盘 + 报告**:

        info["story_signal"] = story_gate_reasons(d)

    排序(§十二)以后会读这个信号: 库存充足时优先 signal 干净(有反转、
    有层次)的题; 库存不足时 signal 不理想的题一样能播。

    ⚠️ 名字里的 `gate` 是历史遗留。它现在**不是门** —— 真正的门是
    `hard_check_reasons`。改名会牵动三个测试文件, 所以保留名字但把
    语义写在这里。

    返回的四项与原版同名(`single_trick` / `not_story_reconstruction` /
    `no_multi_step_deduction`), 但判据方向改成"**明确**不理想"才算:
    缺项 = 没信号, 不是坏信号(§七: 缺 soft 字段不算技术失败)。
    """
    qc = d.get("quality_checks") if isinstance(d.get("quality_checks"),
                                               dict) else {}
    out: list = []
    if qc.get("single_trick") is True:
        out.append("single_trick")
    if qc.get("story_reconstruction") is False:
        out.append("not_story_reconstruction")
    if qc.get("multi_step_deduction") is False:
        out.append("no_multi_step_deduction")
    return out


def hard_check_reasons(d: dict) -> list:
    """**v5 硬门**的独立判定(与 `check_tool_result` 同源, 但给精准标签)。

    与 `check_tool_result` 分开是为了让拒绝原因带上**可统计的标签** ——
    "quality_checks 未全过: clear_anomaly" 这种串既难统计也看不出是
    哪一类问题。

    ⚠️ **唯一定义处**: prompt 渲染 / 代码门 / 报告分类全读
    `CURATED_HARD_CHECKS`, 不另抄一份清单。

    包含 §四 的冷知识判定: `no_external_knowledge_dependency` 不是
    "涉及专业内容就拒", 而是"**解题必须知道那个外部知识点**"才拒。
    见 `EXTERNAL_KNOWLEDGE_ALLOWED` / `EXTERNAL_KNOWLEDGE_FORBIDDEN`。
    """
    qc = d.get("quality_checks") if isinstance(d.get("quality_checks"),
                                               dict) else {}
    return ([k for k in CURATED_HARD_CHECKS if qc.get(k) is not True]
            + no_external_knowledge_reasons(qc))


#: §四: 冷知识判定的**允许/禁止**清单 —— prompt 与 Reviewer 契约共用。
#:
#: 核心判据(用户口径):
#:
#:     揭晓后普通观众会说"哦, 原来如此"        -> 可以
#:     揭晓后普通观众还要问"这个规则是什么"    -> 拒绝
#:
#: 这两句话是**产品判据**, 不是技术判据 —— 所以它写在代码里(单一
#: 定义处), 而不是散在三份 prompt 里各写一遍。
EXTERNAL_KNOWLEDGE_ALLOWED = (
    "日常生活常识", "简单直觉物理", "常见物品用途", "普通社会经验",
)

EXTERNAL_KNOWLEDGE_FORBIDDEN = (
    "专业知识", "行业内部规定", "具体法律或医学知识", "冷门设备功能",
    "罕见科学知识", "特定网站/软件/平台机制", "只有知道某个专有事实才能解",
)


def no_external_knowledge_reasons(qc: dict) -> list:
    """§四 的冷知识**细分**判定 —— 只在一个地方实现。

    `quality_checks["no_external_knowledge_dependency"] is not True` 时
    返回原因列表, 否则返回 []。

    ⚠️ 为什么单独抽出来: 这一条是 v5 里**唯一**从 v2 保留下来、语义
    却变了的故事类判据。它的措辞直接决定"卡车烧油能不能进"这类问题,
    而那种判定一旦在两处各写一份(prompt 一份 / 报告一份), 下次改
    口径必然漂移。所以判据的**文案**在这里, prompt 引用它。
    """
    if qc.get("no_external_knowledge_dependency") is True:
        return []
    return ["external_knowledge_dependency"]


def build_user_prompt(rec, *, target_blueprint: Optional[PuzzleBlueprint] = None
                      ) -> str:
    """把一条 RawCuratedPuzzle 拼成编译请求。

    ⚠️ 消息里**显式声明**"这是已有题目, 不要重新创作" —— 光靠 system
    prompt 不够: 模型很容易滑回"编一道类似的题"。把原文放在显眼位置
    并明确标注来源, 能显著降低改写率。
    """
    parts = [
        "下面是一道**已经存在**的谜题。请把它**搬进**我们的 schema。",
        "",
        f"【来源】{rec.source}",
    ]
    if rec.source_url:
        parts.append(f"【原帖】{rec.source_url}")
    if rec.title:
        parts.append(f"【原标题】{rec.title}")
    parts += [
        "",
        "═══ 谜面(surface, canonical —— 不得改变其事实)═══",
        rec.surface,
        "",
        "═══ 谜底(bottom, canonical —— 不得改变其真相)═══",
        rec.bottom,
        "",
    ]
    if rec.original_language and rec.original_language != "zh":
        parts += [
            f"⚠️ 原题是 **{rec.original_language}**。请把它翻译成中文。",
            "翻译要求: 事实一一对应(数字/关系/时间/地点不改), "
            "不是文学改写。若依赖词形或双关无法翻译 -> reject 并填 "
            "language_dependent。",
            "",
        ]
    if target_blueprint is not None:
        parts += [
            "【本题的 Blueprint(代码层已决定, 只能照它设计)】",
            target_blueprint.describe(),
            "",
        ]
    parts.append("请按 schema 输出。记住: 你是**编辑**, 不是出题人。")
    return "\n".join(parts)


# ======================================================================
# 结果 → PuzzleSpec
# ======================================================================
def _unwrap(ti: Any) -> dict:
    """兼容被包了一层的 tool_input(`{"input": {...}}` 之类)。"""
    if not isinstance(ti, dict):
        return {}
    for k in ("input", "arguments", "parameters", "result"):
        inner = ti.get(k)
        if isinstance(inner, dict) and ("accepted" in inner
                                        or "quality_checks" in inner):
            return inner
    return ti


def spec_from_tool(d: dict, rec, bp: Optional[PuzzleBlueprint]
                   ) -> PuzzleSpec:
    """把模型回传的 dict 转成 PuzzleSpec(**不做校验**)。

    与 `_spec_from_tool` 的关键差别: 这里**注入 provenance**。curated 题
    必须永远带着"它从哪来", 否则版权与排查都无从谈起(H2-F/H2-H)。

    ## v4: `bp=None` 不再被替换成一份默认 Blueprint(任务书 §一/§三)

    旧实现是 `blueprint=bp or PuzzleBlueprint()`。对 curated 题那是**错的**:
    题目已经存在, 我们不对它下达目标骨架, 所以 `bp` 本来就该是 None。
    那个 `or` 把它变成一份带真实默认约束的 Blueprint, 而下游
    `_review_spec` 会把它当硬约束印给审稿人 -> 已有 canonical 题因为
    "不是 stranger / 不是 neutral / 不是 instant"被要求推倒重出。

    None 现在如实保留为"无目标约束"的哨兵实例, 由
    `is_unconstrained_blueprint()` 识别。
    """
    sig_raw = d.get("observed_signature") or {}
    sig = PuzzleSignature.from_dict(sig_raw)
    spec = PuzzleSpec(
        title=str(d.get("title") or rec.title or "").strip(),
        puzzle=str(d.get("puzzle") or "").strip(),
        answer=str(d.get("answer") or "").strip(),
        core_answer=str(d.get("core_answer") or "").strip(),
        completion_fact_ids=[str(x).strip()
                             for x in (d.get("completion_fact_ids") or [])
                             if str(x).strip()],
        facts=[PuzzleFact.from_dict(x) for x in (d.get("facts") or [])],
        solve_atoms=[SolveAtom.from_dict(x, i)
                     for i, x in enumerate(d.get("solve_atoms") or [])],
        discovery_beats=[DiscoveryBeat.from_dict(x)
                         for x in (d.get("discovery_beats") or [])],
        fair_clues=[FairClue.from_dict(x) for x in (d.get("fair_clues") or [])],
        hints=[str(h).strip() for h in (d.get("hints") or [])
               if str(h).strip()],
        # v4: 不再 `bp or PuzzleBlueprint()`。None = 无目标约束(见上)。
        blueprint=(bp if bp is not None else make_unconstrained_blueprint()),
        signature=sig,
        prompt_version=CURATED_PROMPT_VERSION,
        quality_policy_version=QUALITY_POLICY_VERSION,
    )
    # ---- provenance(H2-F): 只进 archive / attribution, **不进前端** ----
    spec.source_type = "curated"                              # type: ignore[attr-defined]
    spec.external_source = rec.source                         # type: ignore[attr-defined]
    spec.external_id = rec.external_id                        # type: ignore[attr-defined]
    spec.source_url = rec.source_url                          # type: ignore[attr-defined]
    spec.license = rec.question_license                       # type: ignore[attr-defined]
    spec.answer_license = rec.answer_license                   # type: ignore[attr-defined]
    spec.attribution = {                                      # type: ignore[attr-defined]
        "question_author": rec.question_author,
        "question_author_url": rec.question_author_url,
        "answer_author": rec.answer_author,
        "answer_author_url": rec.answer_author_url,
        "question_license": rec.question_license,
        "answer_license": rec.answer_license,
        "source_url": rec.source_url,
        "modified": True,
        "modification": ("Chinese translation + structural compilation"
                         if rec.original_language != "zh"
                         else "structural compilation"),
    }
    spec.style_tags = list(d.get("style_tags") or [])         # type: ignore[attr-defined]
    #: 内容风格(H3-A)与结构型 style_tags 分开 —— 见 PuzzleSpec 的字段说明。
    spec.content_style = list(d.get("content_style") or [])   # type: ignore[attr-defined]
    # ---- H3-A: curated 准入政策版本 ----
    # 这是**旧库存的隔离开关**: 题池准入门要求它等于当前
    # CURATED_POLICY_VERSION。H2 那批(v1, 含 q10000)落盘时没有这把键
    # -> 读出来是空串 -> 自动失去 live eligibility, 不需要手工清理。
    spec.curated_policy_version = CURATED_POLICY_VERSION      # type: ignore[attr-defined]
    # ---- H3-D3 §一-4: accepted 必须绑定**内容**哈希 ----
    #
    # 账本的判据键是 `(external_id, content_hash, policy)` 三元组。早先
    # 池的准入门只按 `external_id + policy` 查 —— 于是同一 external_id
    # 的**旧内容**被 accepted, 会给**新内容**发通行证(SE 帖子可以被
    # 编辑, 内容变了而问题号不变)。
    #
    # 这里把来源侧 `surface + bottom` 的哈希写进 spec。算法必须与
    # `curated_ledger.content_hash_of(rec)` **逐字一致** —— 用同一个函数
    # 而不是各写一份, 否则池门算出的 key 在账本里永远查不到, 整批题
    # 会被静默判成"未提交"。
    from tools.curated_ledger import content_hash_of
    spec.curated_content_hash = content_hash_of(rec)          # type: ignore[attr-defined]
    return spec


def validate_curated(spec: PuzzleSpec, bp: Optional[PuzzleBlueprint] = None
                     ) -> tuple:
    """curated 题的额外确定性校验。返回 `(ok, reasons)`。

    在现有 `validate_spec` 之外, curated 还要多守两条:

      1. **fair_clue 必须逐字来自谜面**(H2-E)。`validate_spec` 已经查
         了 quote 在不在谜面里, 但它是 `can_fix`(交给审稿人改)。对
         curated 题这**不够**: 审稿人"改"的方式很可能是**改谜面去迁就
         quote**, 而谜面是 canonical source, 不许动。所以这里升级为
         硬拒 —— 宁可丢掉一道题, 也不能让编辑改动了原作者的事实。

      2. **source_type 必须是 curated**。防的是"将来某个调用方把
         curated 编译器拿去跑自由生成", 那时 provenance 会撒谎。
    """
    reasons: list = []
    if not spec.puzzle or not spec.answer:
        reasons.append("谜面或谜底为空")
        return False, reasons
    if getattr(spec, "source_type", "") != "curated":
        reasons.append("source_type 不是 curated(provenance 缺失)")
    clues = spec.fair_clues or []
    if not clues:
        # H2-E: 谜面里没有可回溯线索 -> reject, **不要**从谜底倒灌。
        reasons.append("没有 fair_clue(谜面无可回溯线索, 应 reject)")
    for c in clues:
        if not c.quote:
            reasons.append("fair_clue 缺 quote")
        elif spec.puzzle and not quote_in_puzzle(c.quote, spec.puzzle):
            reasons.append(
                f"fair_clue 的 quote 不在谜面里({c.quote[:30]!r}) —— "
                f"curated 题**不得改谜面**去迁就 quote")
    return (not reasons), reasons


#: `_repair_hints` 的第二个参数要一份 `vb`(blueprint 校验结果), 而
#: curated 链**没有** target blueprint 可验 —— 传一个"永远通过"的桩,
#: 因为那个检查在这里不适用(见 v4 的 Blueprint 说明)。
class _Ok:
    ok = True
    fixable: tuple = ()
    errors: tuple = ()


def _requote_from_puzzle(puzzle: str) -> str:
    """从谜面里**逐字**挑一段连续文字当 fair_clue。

    §八: canonical puzzle 不许为迁就 fair_clue 被修改 —— 所以修法只能是
    **改 quote**, 且新 quote 必须是谜面里原样存在的一段。

    策略(确定性, 无 LLM, 可复现):
      1. 按句读切分(。！？；\\n), 取**最长**的一句 —— 它最可能携带
         可回溯的线索;
      2. 去掉首尾空白后若仍非空且确实在谜面里 -> 用它;
      3. 都挑不出来 -> 返回空串(调用方据此判"修不了")。

    刻意**不**做"截取任意子串": 一段没有语义边界的半句话不是线索,
    它只会让 `validate_spec` 的另一条(线索指不到 completion)再次失败。
    """
    import re as _re
    text = (puzzle or "").strip()
    if not text:
        return ""
    parts = [p.strip() for p in _re.split(r"[。！？；\n]+", text)]
    parts = [p for p in parts if p]
    if not parts:
        return ""
    parts.sort(key=len, reverse=True)
    for p in parts:
        if quote_in_puzzle(p, text):
            return p
    return ""


def _merge_duplicate_core_facts(spec: PuzzleSpec) -> bool:
    """把**文本完全相同**(归一后)的 core+hidden facts 归并成一条。

    §九: "如果 canonical 题内容很好但 compiler 拆成了 4 个: 优先重新
    归并事实"。这里只做代码能**确定**的那一半 —— 两条文本一模一样的
    事实本来就是同一条, 合并它不改任何真相。

    语义等价但措辞不同的**不做** —— 那需要判断, 而判断错了就是"为了
    过 schema 改真相"。那些走 technical_defer。

    返回 True 表示真的合并过。所有引用(completion_fact_ids /
    solve_atoms.fact_ids / discovery_beats.fact_ids)重指到保留下来的 id。
    """
    from story.puzzle import normalize_for_match
    core_hidden = [f for f in (spec.facts or [])
                   if f.kind == "core" and f.visibility == "hidden"]
    by_key: dict = {}
    drop: dict = {}          # 被合并掉的 id -> 保留下来的 id
    for f in core_hidden:
        k = normalize_for_match(f.text or "")
        if not k:
            continue
        if k in by_key:
            drop[f.id] = by_key[k]
        else:
            by_key[k] = f.id
    if not drop:
        return False

    def _remap(ids):
        out, seen = [], set()
        for i in (ids or []):
            j = drop.get(i, i)
            if j not in seen:
                seen.add(j)
                out.append(j)
        return out

    spec.facts = [f for f in spec.facts if f.id not in drop]
    spec.completion_fact_ids = _remap(spec.completion_fact_ids)
    for a in (spec.solve_atoms or []):
        a.fact_ids = _remap(a.fact_ids)
    for b in (getattr(spec, "discovery_beats", None) or []):
        b.fact_ids = _remap(b.fact_ids)
    log.info("core hidden facts 归并: %s", drop)
    return True


# ======================================================================
# 编译器
# ======================================================================
class CuratedCompiler:
    """把外部题库记录编译成 `PuzzleSpec`。

    它**持有**一个 `PuzzleWriter` 来复用审稿/审计/客户端 —— 那几段与
    出题链共用, 不另写一份(否则两条链的质量口径迟早漂移)。
    """

    def __init__(self, writer):
        self.writer = writer

    # ------------------------------------------------------------------
    def _repair_spec(self, spec: PuzzleSpec, vr) -> Optional[PuzzleSpec]:
        """§七/§八: 对**确定性可修**的结构问题做一次窄修复。

        返回修好之后的 spec, 或 None 表示"修不了"(调用方按原路走)。

        ## 铁律: **canonical puzzle 不许为迁就 fair_clue 被修改**

        这条继续有效。但审计已经证明旧行为是**确定性误分类**:

            同一种"酒吧止嗝"题:
              一条 accepted
              另一条仅因为 fair_clue quote 抄错一个字就被 rejected

        正确修法是**修 fair_clue.quote**, 不是 reject 整道题 —— 而且
        重选 quote 必须**逐字取自现有 puzzle**, 一个字都不许改谜面。

        ## 分词修什么

            hints 超长 / 数量对         -> 复用 `writer._repair_hints`(一次 LLM,
                                          但它只改 hints, 不碰任何 canonical 字段)
            fair_clue quote 不在谜面   -> **确定性**重选(0 LLM):
                                          在谜面里找一句连续文字替换

        ## 修不了的一律返回 None

        completion fact 引用不存在的 fact / core hidden facts > 3 这类
        **改变真相才能修**的问题, 不许在这里硬修 —— 那会违反"编辑不改
        canonical 真相"。它们继续走 technical_defer(见 §九)。
        """
        fixed = False

        # ---- ① fair_clue quote: 确定性重选(0 LLM 调用) ----
        #
        # §八: "让 repair 从现有 puzzle 中逐字重新选择一个 quote"。所以
        # 这是纯字符串操作 —— 不需要也不应该问模型(问了它就可能顺手
        # 改写谜面, 那正是禁止的)。
        bad_clues = [c for c in (spec.fair_clues or [])
                     if c.quote and spec.puzzle
                     and not quote_in_puzzle(c.quote, spec.puzzle)]
        if bad_clues:
            for c in bad_clues:
                q = _requote_from_puzzle(spec.puzzle)
                if not q:
                    return None            # 谜面里挑不出连续文字 -> 修不了
                log.info("fair_clue 重选 quote: %r -> %r", c.quote[:24], q[:24])
                c.quote = q
            fixed = True

        # ---- ② hints: 只在**全部** fixable 都是提示相关时才动 ----
        #
        # 与 `_repair_hints` 的触发条件一致: 混进别的类别说明问题不在
        # hints —— 那时补 hints 只是掩盖。这里额外要求谜面/谜底一个字
        # 都不会变(窄修复窄到只碰 hints)。
        hint_fixable = [f for f in (vr.fixable or []) if "提示" in f]
        if hint_fixable and len(hint_fixable) == len(vr.fixable or []):
            try:
                # ⚠️ `enforce_blueprint=False`: curated 题**没有** target
                # blueprint(v4), 传 True 会让它拿一份默认骨架去判这道
                # canonical 题 —— 正是本轮要消灭的那个错误。
                new_spec, did = self.writer._repair_hints_if_only_issue(
                    spec, vr, _Ok(), enforce_blueprint=False)
                if did:
                    spec = new_spec
                    fixed = True
            except Exception:                   # noqa: BLE001
                log.exception("hints 窄修复异常, 放弃")

        # ---- ③ §九: core hidden facts 超限 -> 先试**归并重复事实** ----
        #
        # `core hidden facts <= 3` 是**我们内部的直播 schema / completion
        # 契约**设计, 不是"世界上超过 3 个隐藏事实的海龟汤都是坏题"。
        #
        # 如果 canonical 题内容很好但 compiler 把同一个事实拆成了多条,
        # 正确做法是**归并**它 —— 那不改真相。所以这里做一个**确定性**
        # 归并: 文本(归一后)相同的 core+hidden 事实合成一条, 并把所有
        # 引用重指过去。
        #
        # ⚠️ 只能做**文本相同**的归并。语义等价但措辞不同的两条事实
        # 要不要合并, 代码判不了 —— 硬判就是"为了过 schema 改真相"。
        # 那种情况返回 None, 走 technical_defer(§九明确允许)。
        n_core = len(spec.core_hidden_facts())
        if n_core > 3 and _merge_duplicate_core_facts(spec):
            fixed = True

        return spec if fixed else None

    # ------------------------------------------------------------------
    def compile_one(self, rec, *, recent: Optional[list] = None,
                    blueprint: Optional[PuzzleBlueprint] = None,
                    max_attempts: int = 2,
                    should_continue: Optional[Any] = None
                    ) -> tuple:
        """编译一道。返回 `(spec, info)`。

        `spec` 为 None 表示没收(AI 拒绝 / 硬校验不过 / 跨题门拒)。
        `info` 里带 `accepted` / `reject_reasons` / `stage` —— **不要**
        把没收的原因只打进日志: 验收报告要按原因分类统计, 而日志是要
        靠人肉数的。

        ## `should_continue`(H3-B): 给直播让路

        一个无参回调, 返回 False 表示"现在别审了"。**每个昂贵的门之前**
        都会重新问一次 —— 因为一次编译可能跑十几秒到几十秒(编译 +
        审稿 + 审计, 还可能要重试), 期间观众完全可能开始提问、或者
        下一题要上了。这些资源必须归直播。

        中断时 `info["interrupted"] = True` —— 调用方据此写
        `interrupted` 决策(**不是** rejected), 那条题下次还能重审。

        与 G4-C 的让路检查同一个思路: 协作式, 在**边界**上让, 不抢占。
        """
        info: dict = {"external_id": getattr(rec, "external_id", ""),
                      "accepted": False, "reject_reasons": [], "stage": ""}
        client = self.writer.client
        user = build_user_prompt(rec, target_blueprint=blueprint)

        #: §七: 每道题的**窄修复预算**。1 次 —— 修不掉就走 technical_defer,
        #: 下次再说。**不是**无限重试: 那会把预算烧在一个持续坏掉的
        #: 结构上, 而这一整轮什么也产不出。
        repair_budget = 1
        _repairs: list = []

        def _live_busy() -> bool:
            """直播需要资源 -> True。回调坏了也不能让编译崩溃。"""
            if should_continue is None:
                return False
            try:
                return not should_continue()
            except Exception:                   # noqa: BLE001
                log.exception("should_continue 回调抛异常, 按'让路'处理")
                return True

        last_err = ""
        #: 本次编译**走到过的最深一道门**。
        #:
        #: 为什么要记"最深"而不是"最后一次": 重试会让后面的稿子覆盖
        #: 前面的 stage。实测: 第 1 稿一路走到跨题门被拒(那是最有价值
        #: 的信息 —— 题本身没问题, 是分布重复), 第 2 稿却因为网关抖动
        #: 在 compile_call 就挂了, 于是报告里 stage 变成 "compile_call",
        #: 把一个"分布问题"记成了"网关问题"。运维照着这个数字去查网络,
        #: 方向完全错了。
        #:
        #: 深度按管道顺序定: 越靠后越有价值(说明前面的门都过了)。
        deepest = ""
        for attempt in range(1, max(1, max_attempts) + 1):
            # ---- 让路检查 ①: 调 LLM 之前 ----
            if _live_busy():
                return None, _mark_interrupted(info, "before_compile")

            res = client.messages(CURATED_COMPILE_SYSTEM, user,
                                  max_tokens=4000, tool=_TOOL_CURATED,
                                  temperature=self.writer._temperature(
                                      "generate_temperature"),
                                  stage="puzzle.structure")
            if not res.tool_input:
                last_err = res.error or "空 tool_input"
                _bump(info, "compile_call")
                log.warning("编译第 %d 稿没有 tool_input: %s",
                            attempt, last_err[:100])
                continue

            d = _unwrap(res.tool_input)
            # ---- §十二: 留住题型审核证据(**在命运分叉之前**) ----
            #
            # `quality_checks` 是**编译模型**对十三判据的逐条回答。Reject
            # Audit 发现它运行完就丢 —— 事后无法回答"当时那四项到底判了
            # 什么"。所以每条出口(accepted / 各 stage 的 reject)都带上它。
            #
            # 只留**四字段**(故事门那四条)而不留全部十三项: 那四项才是
            # 本批要复盘的对象, 其余九条已在 reasons 里有精准表述。
            _qc = d.get("quality_checks")
            if isinstance(_qc, dict):
                # §十二: 十三条全落盘(不再只留四条)—— soft signal 是以后
                # 排序的数据源, 运行完就丢等于永久失去它。
                info["compile_checks"] = {
                    k: _qc.get(k) for k in CURATED_CHECK_ORDER}
            # ---- ① AI 审题门 ----
            ok, reasons = check_tool_result(d)
            if not ok:
                info["accept_reasons"] = reasons
                # ---- 分支 A: 模型**明确**说不行 ----
                #
                # 它自己给的 reason(`not_a_story` / `language_dependent`
                # / …)是最准确的原因, 直接采信并**不重试**(重试只会让它
                # 换个说法硬凑 accepted=true, 那正是最不想看到的)。
                #
                # ⚠️ 这一支排在信号判定**前面**: 一份 `accepted=false` 的
                # 回复往往**没有** quality_checks, 此时信号层只能笼统地说
                # "三个信号都不理想" —— 那是噪音, 会把模型给出的精准原因
                # 挤掉。信号层的职责是记录分布, 不是替模型解释它为什么说
                # 不行。
                if d.get("accepted") is not True:
                    info["reject_reasons"] = reasons
                    info["stage"] = "ai_gate"
                    log.info("AI 拒收 %s: %s",
                             info["external_id"], "; ".join(reasons)[:120])
                    return None, info

                # ---- 分支 B: 模型说行, 但**硬门**说不行 ----
                #
                # v5: 这里只可能是**六条硬门**里的某一条(soft signal 已经
                # 不参与准入)。它**不重试**: 那六条判的都是这道题**本身**
                # 的性质(有没有反常点 / 能不能问 / 要不要冷知识), 再审十次
                # 还是同一个答案。重试只会把本该给好题的预算烧在垃圾上。
                #
                # ⚠️ 走到这里 `accepted=true` 而 `check_tool_result` 说
                # 不合格, 说明是"模型自报通过但硬门有 false" —— 那正是
                # §七 说的"自相矛盾时以逐条判据为准"。
                hcr = hard_check_reasons(d)
                if hcr:
                    info["hard_gate"] = hcr
                    info["reject_reasons"] = hcr
                    info["stage"] = "hard_gate"
                    log.info("v5 硬门拒收 %s: %s",
                             info["external_id"], ", ".join(hcr))
                    return None, info

                # ---- 分支 C: 自相矛盾(accepted=true 但 qc 有 false) ----
                #
                # v5 里走到这里只可能是 **soft signal** 有 false —— 那
                # **不构成拒绝**。落个信号, 继续往下走。
                #
                # ⚠️ 这一支以前是 `continue`(重出整稿)。现在不能重出:
                # 一道 soft signal 不理想的题**本身是合格的**, 重出只会
                # 把它换成另一道题 —— 而那道新题未必更好, 却一定烧了
                # 一次调用。§三 明写这四个信号可以 accepted。
                log.info("v5 soft signal 不理想(不影响准入) %s: %s",
                         info["external_id"],
                         "; ".join(reasons)[:120])

            # ---- ①b v5 硬门(独立于 check_tool_result 再判一次) ----
            #
            # 走到这里说明 `check_tool_result` 已经过了(六条硬门全合格),
            # 所以这道门通常**不会**触发。留着是为了 defense in depth:
            # 将来若有人把 `check_tool_result` 的 checks 参数收窄, 硬门
            # 仍然独立生效。
            #
            # ⚠️ 这一段曾经**重复了三遍**(H3-A 的编辑事故)。它现在只判
            # 一次 —— 三份同样的判定里只要有一份被将来改坏, 行为就开始
            # 取决于"走到哪一份", 而那是无法从日志里看出来的。
            hcr = hard_check_reasons(d)
            if hcr:
                info["hard_gate"] = hcr
                info["reject_reasons"] = hcr
                info["stage"] = "hard_gate"
                log.info("v5 硬门拒收 %s: %s",
                         info["external_id"], ", ".join(hcr))
                # 结构性判定, 重试不会变好 -> 不重试。
                return None, info

            # ---- ①c v5: soft signal 只记录, **不拒题** ----
            #
            # 这一段是 §三/§六 的落点。它以前是 `if sgr: return None, info`
            # (reject), 现在只写 `info["story_signal"]`。
            #
            # 为什么不删掉整段: §十二 要求保留这些信号供**以后排序**。
            # 库存充足时优先更有反转/更曲折的题; 库存不足时简单题一样
            # 能播。没有这一步, 那个排序无从谈起。
            sgr = story_gate_reasons(d)
            if sgr:
                info["story_signal"] = sgr
                log.info("v5 故事信号(仅记录) %s: %s",
                         info["external_id"], ", ".join(sgr))

            spec = spec_from_tool(d, rec, blueprint)
            spec.usage, spec.model = res.usage, res.model
            #: 内容风格(与结构型 style_tags 分开存, 供验收按观感分类)。
            spec.content_style = list(d.get("content_style") or [])  # type: ignore[attr-defined]

            # ---- ② 结构硬门(与出题链同一套) ----
            vr = validate_spec(spec)
            if not vr.ok:
                last_err = vr.why()
                # ---- §七: 确定性**可修**的结构问题 -> 先修一次 ----
                #
                # `vr.fixable` 是代码已经点名、且**不需要动 canonical
                # 内容**就能改的项(hints 数量/长度、fact id 拼错之类)。
                # 早先直接 `continue` 重出整稿 —— 那等于因为"提示只有
                # 1 条"就把一道好题重造一遍, 而且下一稿是**另一道题**。
                #
                # 现在先试一次**窄修复**(只动被点名的那一项, 谜面谜底
                # 一个字都不碰)。修成了就继续走后面的门。
                #
                # ⚠️ 这是**有界**的: `repair_budget` 每道题只有 1 次,
                # 用完就退化成原来的行为。不修成技术债, 也不无限烧钱。
                if vr.fixable and repair_budget > 0:
                    repair_budget -= 1
                    fixed = self._repair_spec(spec, vr)
                    if fixed is not None:
                        spec = fixed
                        _repairs.append("validate:" + ",".join(vr.fixable))
                        info["repairs"] = list(_repairs)
                        log.info("第 %d 稿窄修复(%s)后继续",
                                 attempt, ", ".join(vr.fixable))
                        vr = validate_spec(spec)
                if not vr.ok:
                    _bump(info, "validate")
                    log.info("第 %d 稿结构不过: %s", attempt, last_err[:120])
                    continue
            # ---- ③ curated 额外门(fair_clue 逐字 / provenance) ----
            cok, creasons = validate_curated(spec, blueprint)
            if not cok:
                last_err = "; ".join(creasons)
                _bump(info, "curated_validate")
                info["reject_reasons"] = creasons
                log.info("第 %d 稿 curated 校验不过: %s",
                         attempt, last_err[:120])
                # fair_clue 不在谜面里是**结构性的**: 再审一次也不太可能
                # 变好, 而且放任它会让审稿人改谜面 -> 那正是禁止的。
                break
            # ---- 让路检查 ②: 编译过了, 审稿之前 ----
            if _live_busy():
                return None, _mark_interrupted(info, "before_review")

            # ---- ③b 故事门的**独立复核**(§六, H3-D3 起并入审稿) ----
            #
            # 为什么不能只信 `story_gate_reasons`: 它读的是**编译模型
            # 自己填的** quality_checks。模型一旦自信地判错(十八楼那道
            # 题在它眼里是"物品用途反转"), 代码层无从知道。
            #
            # H3-D 里这一层是**独立的一次 LLM 调用**(`story_review`)。
            # H3-D3 §一-2 把它并进了**紧跟着的审稿调用**: `_TOOL_CHECK`
            # 的 `quality_checks` 现在多带四个题型字段, 而
            # `_QUALITY_CHECK_FIELDS` 对 curated 题是**十二项 fail
            # closed** —— 也就是说, 审稿人不填 / 填 false, 那一稿在
            # `_review_spec` 里就**已经**被拒了, 根本走不到这里。
            #
            # 这条 accepted 路径因此从 4 次 LLM 降到 3 次
            # (compile / review / audit), 符合任务书要求。
            #
            # ⚠️ 那为什么**还留着** `story_gate_from_review` 这一层?
            # 因为它是**独立的 judge**: `_review_spec` 看的是"这一稿能不能
            # 用"(结构/真实性/好不好玩), 十二项里那四条题型问题只是顺带
            # 回答; 这里做的是**把题型单独再拎出来判一次**。两层是 AND
            # 关系 —— 任一层说不合格就拒。宁可少收一道题, 不可放过一道
            # 脑筋急转弯。

            # ---- ④ Reviewer(**复用现有审稿人**) ----
            reviewed, why, need_rewrite, technical = (
                self.writer._review_spec_with_retry(
                    spec, spec.blueprint, must_fix=vr.must_fix(),
                    own_fix_focus=list(vr.fixable),
                    should_continue=should_continue))
            # ---- ③b 故事复核(**v5 起只是信号**) ----
            #
            # 判在 `reviewed is None` **之前**, 因为这两件事是**不同**的
            # 拒绝理由, 而报告要分得开。
            #
            # ⚠️ v5: 这一段**不再拒题**(§六)。它以前是:
            #
            #     if sgr2 and _answered: return None, info   # reject
            #
            # 于是"Reviewer 觉得这题不够曲折"就能杀掉一道能播的题。现在
            # 它只写 `info["review_signal"]`, 与编译侧信号并列供排序用。
            #
            # ⚠️ **但 `reviewed is None` 仍然要处理** —— 那是审稿**没成**
            # (技术失败 / 结构不合格), 与"信号不理想"是两件事。下面
            # 单独判, 不能因为降级了信号就把技术失败也放过去。
            srev = getattr(self.writer, "_last_review_checks", None)
            if isinstance(srev, dict):
                # §十二: Reviewer 侧十三条全落盘(审计证据, 不进前端)。
                info["review_checks"] = {k: srev.get(k)
                                         for k in CURATED_CHECK_ORDER}
            sgr2 = story_gate_from_review(srev)
            if sgr2:
                info["review_signal"] = sgr2
                log.info("v5 复核故事信号(仅记录) %s: %s",
                         info["external_id"], ", ".join(sgr2))
            if reviewed is None:
                last_err = why
                # ---- §一-3: 技术失败 == technical_defer, **不是** rejected ----
                #
                # 审稿超时 / 网关错 / 空 tool_input / schema 坏掉, 都不是
                # "这道题不行", 而是"这次没审成"。记成 rejected 会让那道
                # 题**永久消失**, 而它可能是一道好题。
                #
                # 判据已经由 `_review_spec_with_retry` 分好了(第 4 个
                # 返回值), 这里只负责**把技术失败标成技术 stage** ——
                # `_classify` 靠 `info["technical"]` / 技术 stage 把它翻成
                # technical_defer。
                st = "review_technical" if technical else "review"
                _bump(info, st)
                if technical:
                    # 标记成"技术性未审成", 供上层复核(与 `_mark_interrupted`
                    # 同一个语义: 可重试, 不是内容拒绝)。
                    info["technical"] = True
                log.info("第 %d 稿审稿未过(%s): %s",
                         attempt, st, str(why)[:120])
                continue
            spec = reviewed
            vr2 = validate_spec(spec)
            if not vr2.ok or vr2.fixable:
                last_err = "; ".join(vr2.errors + vr2.fixable)
                _bump(info, "post_review_validate")
                continue
            cok2, creasons2 = validate_curated(spec, blueprint)
            if not cok2:
                last_err = "; ".join(creasons2)
                _bump(info, "post_review_curated")
                info["reject_reasons"] = creasons2
                break
            # ---- 让路检查 ③: 审计之前 ----
            if _live_busy():
                return None, _mark_interrupted(info, "before_audit")
            # ---- ⑤ truth audit(**复用**) ----
            ta = self.writer._audit_with_retry(spec)
            if ta is not None and not (ta.get("narrator_truthful")
                                       and ta.get("mechanism_consistent")):
                last_err = str(ta.get("why") or "叙事真实性审计不过")
                st = ("truth_audit_technical" if ta.get("technical")
                      else "truth_audit")
                _bump(info, st)
                log.info("第 %d 稿 truth audit 不过: %s",
                         attempt, last_err[:120])
                continue
            # ---- ⑥ reveal adherence ----
            ra = validate_reveal_adherence(spec, spec.blueprint)
            if ra:
                last_err = "; ".join(ra)
                _bump(info, "reveal_adherence")
                continue
            # ---- ⑦ 跨题分布: **只记录, 不拒绝**(产品边界, H4-F) ----
            #
            # external curated 的准入**不看题型分布**。
            #
            #     题型 / recent-10 / diversity quota
            #         只对 **AI 原创链** 具有硬约束。
            #     下载获得的 external curated
            #         **不得因为题型分布被拒绝**。
            #
            # 所以下面这些**全都不是** reject / retry 理由:
            #   same mechanism / same solution_shape / death quota /
            #   past_trauma / grief / information_gap / domain / relation /
            #   emotion / reveal_mode / procedural / dark-tone target /
            #   mechanism+solution 结构等价 / 任何其他 cross_puzzle_gate 项
            #
            # 它们是 **selection metadata, 不是 admission criteria**:
            # 观察到的分类照常写进 `spec.signature`(见下), live pool 的
            # Pass 1 还要拿它做**软排序**(H4-E)。但
            #
            #     observed classification  !=  admission requirement
            #
            # ⚠️ 这里**曾经**是 `xbad = cross_puzzle_gate(...) -> continue`。
            # 当时 LazyCurator 恰好传 `recent=[]` 所以没触发, 是 **latent
            # policy bug**。任何"修回去"的改动都是把 AI 原创链的配额硬门
            # 误加到外部题上 —— 那正是本次要清除的。
            #
            # 真正的 curated 硬门在别处, **不放宽**: curated-v5 内容门
            # (clear_anomaly / unique_explanation / yes_no_progress /
            # no_obscure_system / no_external_media / livestream_safe)、
            # truth audit(narrator_truthful / mechanism_consistent)、
            # schema/结构合法性(validate_spec)、真 identity duplicate、
            # 文本 near-duplicate(⑧)。
            rcfg = self.writer._cfg()
            try:
                _xsoft = cross_puzzle_gate(
                    spec, recent,
                    Quotas.from_config(rcfg) if rcfg is not None else None,
                    spec.blueprint)
            except Exception:                   # noqa: BLE001
                _xsoft = []
            if _xsoft:
                # 非阻塞 diagnostic —— 只落进 info / 日志, 供人工观察
                # "这批外部题的分布长什么样", **绝不** continue。
                info["diversity_signals"] = list(_xsoft)
                log.info("curated 分布信号(仅记录, 不拒绝) %s: %s",
                         info["external_id"], "; ".join(_xsoft)[:160])
            # ---- ⑧ 与已出过的题太像(**真硬门: 文本 near-duplicate**) ----
            dup = _too_similar_pub(spec.puzzle, recent)
            if dup:
                last_err = f"与最近某题太像: {dup[:40]}"
                _bump(info, "too_similar")
                continue
            # ---- 让路检查 ④: 全部门都过了, **写盘之前** ----
            #
            # 这一处最要紧: 写盘会同时产生"入池 + attribution"两个副作用,
            # 而它们必须在直播不忙时完成(写盘本身很快, 但如果恰好赶上
            # 磁盘紧张, 抢的就是直播的 IO)。更要紧的是语义 —— 这次
            # 让路发生在**任何终态产生之前**, 所以那条题下次会原样重审,
            # 不会留下"decision 说 accepted 而池里没有"的半状态。
            if _live_busy():
                return None, _mark_interrupted(info, "before_commit")

            info["accepted"] = True
            info["style_tags"] = list(getattr(spec, "style_tags", []) or [])
            info["content_style"] = list(
                getattr(spec, "content_style", []) or [])
            info["attempts"] = attempt
            log.info("编译成功(%s, 第 %d 稿): %s",
                     info["external_id"], attempt, spec.puzzle[:40])
            return spec, info

        # 循环走完仍未成功: stage 取**走到过的最深一道门** —— 那比
        # "最后一稿死在哪"有信息量得多(见上面 `_deepest` 的说明)。
        info["stage"] = info.pop("_deepest", "") or "unknown"
        info["reject_reasons"] = info.get("reject_reasons") or [last_err]
        return None, info


#: 管道深度 —— 越靠后说明这道题**走得越远**(前面的门都过了), 因此
#: 作为"没收原因"越有信息量。见 `CuratedCompiler.compile_one` 的说明。
#:
#: ⚠️ `story_gate` 的深度是 **1** —— 与 `ai_gate` 同级。它是 AI 门的
#: v2 加强版(同一个位置的更严格判定), 不是"比 AI 门更深的一道门"。
#: 把它的深度排在 ai_gate 之上会让"被故事门拒了"看起来像"走得更远"。
_STAGE_DEPTH = {
    "compile_call": 0,
    "ai_gate": 1,
    "story_gate": 1,
    "validate": 2,
    "curated_validate": 3,
    "review": 4,
    "review_technical": 4,
    "post_review_validate": 5,
    "post_review_curated": 5,
    "truth_audit": 6,
    "truth_audit_technical": 6,
    "reveal_adherence": 7,
    # ⚠️ H4-F: 这里**曾经**有 `"cross_gate": 8`。跨题分布自本批起不再是
    # curated 的门(产品边界: 外部题不因题型分布被拒), 所以那个 stage
    # **永远不会再产生**。留着它反而是一个"看起来该有这道门"的误导 ——
    # 删掉, 免得以后有人照着它把拒绝逻辑装回去。
    "too_similar": 9,
}


def _mark_interrupted(info: dict, where: str) -> dict:
    """标记"这次是给直播让路中断的"。

    `interrupted=True` 是调用方写决策的依据 —— 它决定那条题**下次还能
    重审**。绝不能让它退化成 rejected(那会永久丢题), 所以这个标记
    和 `stage="interrupted"` 是**成对**出现的, 由这个函数统一设,
    不靠调用点各自记得。
    """
    info["interrupted"] = True
    info["stage"] = "interrupted"
    info["interrupt_at"] = where
    info["reject_reasons"] = [f"interrupted:{where}"]
    log.info("curated interrupted: %s (%s)", info.get("external_id", ""),
             where)
    return info


def _bump(info: dict, stage: str) -> None:
    """更新"走到过的最深一道门"。"""
    info["_deepest"] = _pick_stage(info.get("_deepest", ""), stage)


def _pick_stage(cur: str, new: str) -> str:
    """取两者里**更深**的那个(深度相同取新的)。"""
    if not cur:
        return new
    if _STAGE_DEPTH.get(new, -1) >= _STAGE_DEPTH.get(cur, -1):
        return new
    return cur


def _too_similar_pub(puzzle: str, recent: Optional[list]) -> str:
    """与最近窗口的谜面做文本近似检查。

    `recent` 是 signature 列表(跨题门要的那种), 里面**没有谜面文本**,
    所以这里只对 `recent` 里带 `puzzle` 属性的项做检查。传进来的通常是
    Engine 的 recent_signatures —— 那就退化成"不检查"。文本去重的主力
    是 H1-E 的语料层(跨来源), 这里只是补一道。

    刻意**不**自己维护一份"已编译的谜面"列表: 那会引入跨题状态, 而
    curated 编译是离线批处理 —— 状态应该显式传进来。
    """
    texts = [str(getattr(s, "puzzle", "") or "") for s in (recent or [])]
    texts = [t for t in texts if t]
    if not texts:
        return ""
    from story.quality import too_similar
    return too_similar(puzzle, texts)
