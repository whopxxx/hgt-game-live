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
    cross_puzzle_gate          跨题分布
    quality.too_similar        文本去重

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
CURATED_POLICY_VERSION = "curated-v4"

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
2. `unique_explanation` 谜底是否能**唯一**解释谜面?(不是"有可能",
   而是"就是它")
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

**以上九条必须全部为 true。** 有一条不满足就该考虑 reject ——
"逻辑成立但没意思"的题不值得占一个直播位。

═══ 十条: 故事性判据(**本版的重点, 比上面九条更硬**) ═══

上面 6/7 两条我们**实测判得太松**: 一道"卡车烧油变轻"的物理脑筋急转弯
也能被论证成"有因果反转"(因果 = 烧油导致变轻), 于是混了进来。问题不在
模型不听话, 而在**那两条问的是"有没有反转", 而不是"这是不是一个故事"**。

所以补下面四条**正交**的判据。它们问的是完全不同的问题, 而且
**有一条不满足就必须 reject** —— 这四条不是加分项, 是准入门。

10. `story_reconstruction` —— 玩家最后是在**重建一个故事模型**吗?

    为 true 当且仅当: 玩家最终要恢复的是"**发生了什么**", 包括
    人物身份 / 人物关系 / 时间 / 空间 / 行为目的 / 因果事件 / 视角 /
    物品意义。

    以下**全部为 false**(它们不是故事, 是知识点):
      - 发现一条物理规律(烧油变轻 / 浮力 / 热胀冷缩)
      - 发现一个数学技巧(数字排列 / 概率 / 称重)
      - 知道一个冷知识(某物其实是某物)
      - 知道一条职业规定或制度要求
      - 知道某个设备的冷门用途
      - 猜中一个单独机关

    自问一句: "谜底揭晓后, 观众脑子里是**多了一个故事**, 还是
    **多了一个知识点**?" 后者一律 false。

11. `multi_step_deduction` —— 是否有**至少两个彼此不同、都会改变
    玩家理解**的发现阶段?

    合格(两个发现各自改变理解):
      发现 A: 死者和"陌生人"其实认识      -> 改变**人物关系**
      发现 B: 两人不是在现在见的面        -> 又改变**时间模型**

    不合格(**只有一个**机制的因果展开):
      汽车烧油 -> 汽车变轻 -> 所以没超重
    这只是**同一件事**顺着推三步, 每一步都没有让前面的事实改变含义。
    自问: "第二个发现有没有让我**回头重新理解**第一个发现?" 没有就
    false。

12. `single_trick` —— 是不是**一个知识点就结束**?

    以下任一为 true:
      整个谜底只有一个知识点 / 知道一个小技巧立即结束 /
      只有一个物理规律 / 只有一个文字双关 / 只有一个机关用途 /
      只有一条职业或制度规则 / 不需要重新构造故事世界

    ⚠️ 注意 direction: 这条是**反向**的, true = 坏。很多"脑筋急转弯"
    都在这条上是 true。

13. `no_external_knowledge_dependency` —— 观众**只靠谜面 + 是/否问答 +
    **普通生活常识**, 有没有机会解出来?

    以下任一作为**核心解法**都算依赖外部知识 -> false:
      - 专业知识(法律/医学/工程/化学…的具体条文或数值)
      - 物理冷知识(浮力/压强/热胀冷缩/相对速度的定量关系)
      - 职业规定或行业惯例
      - 机构制度、办事流程
      - 某个设备的**特殊功能**(绝大多数人没见过它怎么用)
      - 平台/软件的渲染或行为规则
      - 文字游戏(字形 / 谐音 / 双关 —— 换个语言就不成立)
      - 单一机关的用途(知道那个零件叫什么才想得到)

    **普通生活常识不算外部知识**: 人会饿、会累、会怕、会撒谎、
    东西会坏、时间会过去、钱要还、孩子会长大 —— 这些都可以。

    判据(自问): "一个**没读过任何科普、没干过那个职业**的普通观众,
    能不能靠问是/否问题把故事推出来?" 不能 -> false。

    ⚠️ 这一条**不是**"不能涉及专业知识": 题里出现医生、出现物理现象
    都没问题。不合格的是**解题必须知道那个知识点**。

**硬门(代码强制, 不是建议):**

    story_reconstruction            == true
    multi_step_deduction            == true
    single_trick                    == false
    no_external_knowledge_dependency == true

有一条不满足 -> accepted 必须是 false。**不要**因为"这题别的方面都
很好"而放宽这四条 —— 放过一道卡车烧油, 整批的可信度就没了。

⚠️ 常见的错误判断(请自查):
  - "谜底挺巧妙的" -> 巧妙 != 是故事。物理/数学技巧同样巧妙。
  - "有反转" -> 见上, 6 条判得不严, 用 10~13 重新验。
  - "观众会问问题" -> 能问答 != 是海龟汤。物理题也能问答。
  - "这是 situation tag 的题" -> tag 是来源侧标的, 不可信(SE 上
    `lateral-thinking` 混着大量数学/物理/字谜)。只看内容本身。
  - "现实里确实有这样的设备规定" -> 成立 != 公平。冷知识题在直播里
    **猜不出来**, 那才是问题。

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
`quality_checks` 里**十三条**判据逐条填 true/false(1~9 之外还有
10~13 四条故事性/公平性判据, 见上)。**十三条缺一不可** —— 代码
按 fail closed 判: 少填一项等于该项不合格。
"""

_TOOL_CURATED = {
    "name": "emit_curated",
    "description": "把一道**已有**谜题搬进结构化 schema(不是新出一道题)",
    "input_schema": {
        "type": "object",
        "properties": {
            "accepted": {
                "type": "boolean",
                "description": ("**十三条判据全部为 true** 才填 true; "
                                "否则 false"),
            },
            "reject_reasons": {
                "type": "array", "items": {"type": "string"},
                "description": ("accepted=false 时必填。可用: "
                                "language_dependent / not_a_story / "
                                "no_reversal / depends_on_obscure_system / "
                                "needs_external_media / unsafe / "
                                "no_unique_explanation / ambiguous / "
                                "external_knowledge_dependency"),
            },
            "quality_checks": {
                "type": "object",
                "properties": {
                    "clear_anomaly": {"type": "boolean"},
                    "unique_explanation": {"type": "boolean"},
                    "yes_no_progress": {"type": "boolean"},
                    "no_obscure_system": {"type": "boolean"},
                    "not_pure_puzzle": {"type": "boolean"},
                    "has_reversal": {"type": "boolean"},
                    "detail_recontextualized": {"type": "boolean"},
                    "no_external_media": {"type": "boolean"},
                    "livestream_safe": {"type": "boolean"},
                    # ---- v2 三条新判据。方向见 description。 ----
                    "story_reconstruction": {
                        "type": "boolean",
                        "description": (
                            "玩家最终是在**重建一个故事模型**吗"
                            "(身份/关系/时间/空间/目的/因果/视角/物品意义)?"
                            "纯粹发现一条物理规律 / 一个数学技巧 / "
                            "一个冷知识 / 一条职业规定 -> false。"),
                    },
                    "multi_step_deduction": {
                        "type": "boolean",
                        "description": (
                            "是否有**至少两个彼此不同、都会改变玩家理解**"
                            "的发现阶段? 同一机制的因果展开(烧油->变轻->"
                            "没超重)只算一个, 填 false。"),
                    },
                    "single_trick": {
                        "type": "boolean",
                        "description": (
                            "⚠️ 反向字段: true = **坏**。整个谜底只有"
                            "一个知识点 / 知道一个小技巧就结束 / 只有"
                            "一条规则 -> true。好的海龟汤这里应填 false。"),
                    },
                    "no_external_knowledge_dependency": {
                        "type": "boolean",
                        "description": (
                            "普通观众**只靠谜面 + 是/否问答 + 普通生活"
                            "常识**有没有机会解出来?\n"
                            "核心解法依赖以下任一 -> false: 专业知识 / "
                            "物理冷知识 / 职业规定 / 机构制度 / 设备特殊"
                            "功能 / 平台或软件规则 / 文字游戏 / 单一机关"
                            "用途。\n"
                            "普通生活常识(会饿、会累、会撒谎、东西会坏、"
                            "时间会过去)不算外部知识。\n"
                            "自问: 一个没读过科普、没干过那个职业的普通"
                            "观众, 能不能靠问是/否问题推出来?"),
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


#: 九条判据 —— 与 prompt 里的编号一一对应。**必须全为 true**。
#: 口径与 `_apply_review` 的 quality_checks 一致: 缺一项也算不合格
#: (fail closed), 否则模型漏填几项就能把一道烂题放过。
#:
#: ⚠️ 别名 `CURATED_CHECKS` 保留给 H2 的调用方。v2 起**真正的判据是
#: `CURATED_CHECKS_V2`** —— 见那里的说明。
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

#: 故事硬门的四条 —— prompt 与代码都按这份清单判。**唯一定义处**:
#: Reviewer 的独立复核(`curated_story_review`)也读它, 免得两处漂移。
STORY_GATE_FIELDS = ("story_reconstruction", "multi_step_deduction",
                     "single_trick", "no_external_knowledge_dependency")


def story_gate_from_review(rev: Optional[dict]) -> list:
    """把 **Reviewer 的独立复核**结果翻成不通过原因列表。

    ## 为什么需要第二双眼睛(§六)

    `story_gate_reasons()` 读的是**第一次 compile** 时模型自己填的
    `quality_checks`。它有一个结构性弱点: 模型一旦自信地判错
    (`single_trick=false`), 代码层**无法知道它错了** —— 这正是
    `turtlebench:7c53678ff933`(十八楼按不到按钮)漏网的原因: 编译
    模型认为"物品用途反转"是真反转, 于是十三条全填 true。

    解法不是加第四个审核 LLM(任务书明确禁止), 而是**让现有的
    Reviewer 顺手回答同一组问题**。Reviewer 是**独立的一次调用**,
    读过同一道题但任务不同(它审的是"这稿能不能用"), 所以它的判断
    与编译模型的判断**互相独立** —— 两个独立判断都说没问题才算过。

    这是 defense in depth 的**串联**, 不是投票: 任意一边说不合格就
    reject。宁可少收一道题, 不可放过一道脑筋急转弯。

    返回 [] 表示复核通过(或 Reviewer 没有给这一项 —— 见下)。

    ⚠️ **缺失 = 不合格**(fail closed), 与 `check_tool_result` 同一条
    原则: 一条没说自己是故事题的题, 凭什么信它? 唯一的例外见
    `curated_story_review` 的 `required=False` 说明。
    """
    if not isinstance(rev, dict):
        return ["story_review_missing"]
    out: list = []
    if rev.get("single_trick") is not False:
        out.append("single_trick")
    if rev.get("story_reconstruction") is not True:
        out.append("not_story_reconstruction")
    if rev.get("multi_step_deduction") is not True:
        out.append("no_multi_step_deduction")
    if rev.get("no_external_knowledge_dependency") is not True:
        out.append("external_knowledge_dependency")
    return out



def check_tool_result(d: dict, *, checks: tuple = CURATED_CHECKS_V3) -> tuple:
    """判"这道题能不能收"。返回 `(ok, reasons)`。

    ## 为什么先判 `accepted` 再判其它

    模型可能 `accepted=true` 但 `quality_checks` 里有 false(自相矛盾)。
    这时**以 quality_checks 为准** —— 它是逐条判据, 比一个总结布尔更
    可信; 而且 fail closed 方向才安全(宁可拒一道好题, 不能放一道烂题)。

    同理 `accepted=false` 但没有 reasons 也算拒(理由缺了不影响结论)。

    ## v2: 硬门是**代码**判的, 不是靠 prompt 说服模型

    新三条(reconstruction / multi_step / single_trick)在 prompt 里已经
    说得很重, 但 prompt 是**请求**, 不是**保证**。所以这里对它们**再
    判一次**: 只要 `story_reconstruction` 不是 true、或 `single_trick`
    是 true, 直接拒 —— 哪怕模型自己填了 `accepted=true`。

    这一层是 q10000 那道题真正的拦截点: 它是"模型判对了但总结布尔写
    反了"时的兜底。
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
    bad = []
    for k in checks:
        v = qc.get(k)
        # 方向由 `check_value_ok` 统一裁定(反向判据必须**明确**为 false
        # —— "没说"也算不合格: 一条没说自己是 single_trick 的题, 凭什么信它?)
        if not check_value_ok(k, v):
            bad.append(k if k not in _INVERTED_CHECKS
                       else f"{k}(应 false, 实为 {v!r})")
    if bad:
        reasons.append("quality_checks 未全过: " + ", ".join(bad))
        return False, reasons
    return True, []


def story_gate_reasons(d: dict) -> list:
    """v3 硬门的**独立**判定, 返回不通过的原因(通过则空列表)。

    与 `check_tool_result` 分开刻意为之: 那个函数管"十三条全部为 true",
    这个只管**四条故事性里最要害的部分**, 好让拒绝原因能带上精准的标签
    (`single_trick` / `not_story_reconstruction` / `no_multi_step_deduction`
    / `external_knowledge_dependency`) —— 验收报告要按原因分类, 而
    "quality_checks 未全过: story_reconstruction" 这种串既难统计
    也看不出是哪一类问题。

    `story_reconstruction` / `multi_step_deduction` /
    `no_external_knowledge_dependency` 是 true = 好;
    `single_trick` 是**反向**(false = 好)。
    """
    qc = d.get("quality_checks") if isinstance(d.get("quality_checks"),
                                               dict) else {}
    out: list = []
    if qc.get("single_trick") is not False:
        out.append("single_trick")
    if qc.get("story_reconstruction") is not True:
        out.append("not_story_reconstruction")
    if qc.get("multi_step_deduction") is not True:
        out.append("no_multi_step_deduction")
    if qc.get("no_external_knowledge_dependency") is not True:
        out.append("external_knowledge_dependency")
    return out


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
                                      "generate_temperature"))
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
                info["compile_checks"] = {
                    k: _qc.get(k) for k in STORY_GATE_FIELDS}
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
                # ⚠️ 这一支排在故事门**前面**: 一份 `accepted=false` 的
                # 回复往往**没有** quality_checks, 此时故事门只能笼统地说
                # "三条都不合格" —— 那是噪音, 会把模型给出的精准原因
                # 挤掉。故事门的职责是拦"模型说行、其实不行", 不是替
                # 模型解释它为什么说不行。
                if d.get("accepted") is not True:
                    info["reject_reasons"] = reasons
                    info["stage"] = "ai_gate"
                    log.info("AI 拒收 %s: %s",
                             info["external_id"], "; ".join(reasons)[:120])
                    return None, info

                # ---- 分支 B: 模型说行, 但故事门说不行 ----
                #
                # 这是 q10000 走的那条路: accepted=true, 九条也全 true,
                # 只有 v2 三条不合格。它必须在这里被拦下, **而且不重试**:
                #
                #   single_trick 是**结构性**判定 —— 这道题只有一个知识
                #   点, 再审十次还是只有一个知识点。重试不会变好, 只会把
                #   本该给好题的预算烧在垃圾上。
                #
                # 顺序上这一支必须在"自相矛盾 -> 重试"**之前**(见下),
                # 否则 q10000 会落进"多半是漏填, 再审一稿"那个分支,
                # 白烧 max_attempts 次调用。
                sgr = story_gate_reasons(d)
                if sgr:
                    info["story_gate"] = sgr
                    info["reject_reasons"] = sgr
                    info["stage"] = "story_gate"
                    log.info("v2 故事门拒收 %s: %s",
                             info["external_id"], ", ".join(sgr))
                    return None, info

                # ---- 分支 C: 自相矛盾(accepted=true 但 qc 有 false) ----
                #
                # 走到这里说明三条故事性判据是合格的, 死的是"九条"里的
                # 某一条 —— 那多半是漏填而不是真判 false, 再审一稿合理。
                log.warning("第 %d 稿 quality_checks 未全过: %s",
                            attempt, "; ".join(reasons)[:120])
                _bump(info, "ai_gate")
                continue

            # ---- ①b v3 硬门(独立于 check_tool_result 再判一次) ----
            #
            # 走到这里说明 `check_tool_result` 已经过了(十三条全合格),
            # 所以这道门通常**不会**触发。留着是为了 defense in depth:
            # 将来若有人把 `check_tool_result` 的 checks 参数收窄(比如
            # 只传 H2 的九条), 故事门仍然独立生效。
            #
            # ⚠️ 这一段曾经**重复了三遍**(H3-A 的编辑事故)。它现在只判
            # 一次 —— 三份同样的判定里只要有一份被将来改坏, 行为就开始
            # 取决于"走到哪一份", 而那是无法从日志里看出来的。
            sgr = story_gate_reasons(d)
            if sgr:
                info["story_gate"] = sgr
                info["reject_reasons"] = sgr
                info["stage"] = "story_gate"
                log.info("v3 故事门拒收 %s: %s",
                         info["external_id"], ", ".join(sgr))
                # 结构性判定, 重试不会变好 -> 不重试。
                return None, info

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
            # ---- ③b 故事门的**独立复核**(§六, H3-D3 起并入审稿) ----
            #
            # 判在 `reviewed is None` **之前**, 因为这两件事是**不同**的
            # 拒绝理由, 而报告要分得开:
            #
            #     题型不合格   -> stage=story_review, reasons=single_trick,…
            #     结构不合格   -> stage=review
            #
            # 审稿那次答复里的十二项是**一起**判的, 所以"题型四项挂了"
            # 与"结构八项挂了"都会让 `_review_spec` 返回 None。如果把它们
            # 混成一句"审稿未过", 验收报告就分不清"这批外部题有多少是
            # 题型不对"与"有多少是稿子质量不行" —— 而那恰恰是本批要
            # 回答的问题(新源的 yield 到底好不好)。
            #
            # ⚠️ 判据方向由 `story_gate_from_review` 处理(它读的是
            # `check_value_ok` 同一份方向定义)。
            srev = getattr(self.writer, "_last_review_checks", None)
            if isinstance(srev, dict):
                # §十二: Reviewer 侧四字段同样落盘(审计证据, 不进前端)。
                info["review_checks"] = {k: srev.get(k)
                                         for k in STORY_GATE_FIELDS}
            sgr2 = story_gate_from_review(srev)
            _answered = isinstance(srev, dict) and any(
                k in srev for k in STORY_GATE_FIELDS)
            if sgr2 and _answered:
                # 只有在**审稿人确实回答了那四项**时才把拒稿归因到题型 ——
                # 否则一份"结构不合格、题型压根没答"的回复会被误报成
                # "题型不合格", 那会污染验收统计。
                info["story_gate"] = sgr2
                info["reject_reasons"] = sgr2
                info["stage"] = "story_review"
                log.info("v3 故事复核拒收 %s: %s",
                         info["external_id"], ", ".join(sgr2))
                # 复核判的是**题型的本质**: 再审十次, 它还是同一类题。
                # 不重试。
                return None, info
            if reviewed is None and not _answered:
                # ---- §一-3: 题型四问**没答** -> 技术失败, 可重试 ----
                #
                # 这不是"这道题不行", 而是"这次没审成" —— 网关可能回了一份
                # 结构合格但缺字段的答复, 也可能压根没答完。两种情况换一次
                # 网络都可能好。记成 rejected 会永久吃掉一道题, 而那正是
                # §一-3 明令禁止的方向。
                #
                # 判据**必须**与"复核被并进审稿"这件事一致: 并进来之后,
                # "复核缺失"与"审稿技术失败"是同一件事。
                info["technical"] = True
                info["reject_reasons"] = ["story_review_missing"]
                info["stage"] = "story_review"
                log.info("第 %d 稿题型四问缺失 -> technical_defer: %s",
                         attempt, info["external_id"])
                return None, info
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
            # ---- ⑦ 跨题门(**复用**) ----
            rcfg = self.writer._cfg()
            xbad = cross_puzzle_gate(
                spec, recent,
                Quotas.from_config(rcfg) if rcfg is not None else None,
                spec.blueprint)
            if xbad:
                last_err = "; ".join(xbad)
                _bump(info, "cross_gate")
                continue
            # ---- ⑧ 与已出过的题太像 ----
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
    "cross_gate": 8,
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
