#!/usr/bin/env python
# coding: utf-8
"""PuzzleSpec —— 一道题的**结构化事实基准**。

为什么要从 `RiddleResult`(谜面+谜底两段文本)升级到 spec:

    以前一道题就是两段文学文本。于是"观众说到什么程度算猜中"只能靠模型
    读一遍谜底凭感觉判 —— 8 小时直播实测下来, 它频繁宽判:
    "是纪念某个人" / "以前出过事故" 这种万能悲情猜法都能通关。

    现在把"这道题的世界里什么是真的"固定成一份**有限事实表**:
        facts                主持人整局判断"是/不是/无关"的事实空间
        completion_fact_ids  **通关合同** —— 房间必须真正建立的 1~2 条核心事实
        solve_atoms          谜底的分析拆分(提示/解释/复盘), **不是**通关条件
        fair_clues           谜面原文里已经写着、回看能指向谜底的具体事实
        hints                递进提示

    五者职责**必须分开**, 不能混成一种数据:
        facts        ≠ hints   (事实是判定依据, 提示是给方向)
        completion   ≠ atoms   (合同是"达到什么程度算解出", atoms 是"这题怎么拆")
        atoms        ≠ clues   (clues 是题面抓手, 不是判定要求)

    **v5 的关键修正**: 通关不再由"文学谜底 + solve_atoms + support 剧情"
    决定, 而是由 `completion_fact_ids` 的**集合覆盖**决定 —— 房间已公开
    确认的事实会累计, 最后补齐缺口的观众立即触发揭晓。旧模型要求某一个
    观众独自同时说中 cause + mechanism, 于是"共同推理"根本不可能发生,
    观众也普遍反馈"AI 太保守"。

设计原则(方案 §69):
    内容导演权在代码(blueprint/quota/通关规则), 创作能力在 LLM(具体内容),
    事实基准在 PuzzleSpec, 通关权在代码。

零新依赖。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

# ======================================================================
# 枚举(用普通常量而不是 Enum —— 要能原样进出 JSON, 且老数据能宽容读)
# ======================================================================
FACT_KINDS = ("core", "support", "exclusion")
FACT_VISIBILITY = ("public", "hidden")
#: solve_atom 的角色。**这不是通关合同** —— 见 `PuzzleSpec.completion_fact_ids`。
#:
#: v5 之前这里只有 cause/mechanism/support, 而 `validate_spec` 硬性要求
#: "必须恰好一条 required cause + 一条 required mechanism"。后果是**每道题
#: 都被迫写成因果机制题** —— 一道"门外女人到底是谁"的身份题, 生成器只能
#: 硬造一个 cause/mechanism 去满足校验, 于是 atoms 描述的不是这道题真正的
#: 解法, 而 Final Judge 又拿这套假 atoms 当通关闸门, 观众说对了身份却因为
#: "没说清机制"被判没过。
#:
#: v5 起 solve_atoms 降级为**提示 / 解释 / 复盘结构**, 通关改由
#: `completion_fact_ids` 的集合覆盖决定(见 `quality.validate_spec`)。
#: 于是身份、时间、目标翻转这类题可以用 `key` 表达核心翻转, 不必硬造因果。
ATOM_ROLES = ("key", "cause", "mechanism", "support")

#: 机制家族(方案 §8)。第一版刻意只有 12 种 —— 几十种会让配额失去意义。
MECHANISM_FAMILIES = (
    "information_gap",       # 信息差: 有人知道别人不知道的事
    "hidden_function",       # 隐藏功能: 行为有真实用途, 不是表面那个
    "identity_misread",      # 身份误认
    "rule_constraint",       # 规则约束: 有条外部规则逼他这么做
    "causal_reversal",       # 因果倒置: 以为 A 导致 B, 其实 B 导致 A
    "time_reinterpretation",  # 时间错认
    "space_reinterpretation",  # 空间错认
    "goal_reversal",         # 目的倒置: 手段和目的反了
    "observer_misread",      # 观察者误读
    "object_misuse",         # 物品被错当成另一样
    "social_rule",           # 社交规则/人情约束
    "emotional_motive",      # 情绪动机
)

#: 解谜形状(方案 §9)。与 mechanism_family **分开存在** ——
#: 这是防"职业换了但解法没换"的关键: 8 小时直播里 69% 的题都是
#: "做了 N 年从没出过错 + 有个怪规矩", 而 family 判不出这种重复。
SOLUTION_SHAPES = (
    "past_trauma_explains_current_ritual",   # 必须严格限额, 见 quality.py
    "hidden_function_explains_behavior",
    "identity_reversal",
    "information_advantage",
    "rule_constraint",
    "causal_reversal",
    "time_reinterpretation",
    "space_reinterpretation",
    "goal_reversal",
    "misunderstood_object",
    "social_constraint",
)

#: 情绪基调。悲情三档(grief/guilt/memorial)会被 quota 合并统计。
EMOTION_MODES = ("neutral", "absurd", "warm", "tense",
                 "grief", "guilt", "memorial", "eerie")

#: 悲情基调 —— 这三档在配额里算一类(实测 58% 的题都落在里面)。
GRIEF_MODES = frozenset({"grief", "guilt", "memorial"})

#: 揭晓结构 —— "揭晓时观众重新理解了什么"。
#:
#: **与 `emotion_mode` 严格正交**, 这是任务书 §15 冻结的边界:
#:
#:     mechanism_family = 谜题靠什么机关成立
#:     emotion_mode     = 整体是什么气氛
#:     reveal_mode      = 揭晓时观众重新理解了什么
#:
#: 所以这里**没有** `eerie_recontextualization` / `absurd_logic` /
#: `warm_reversal` 这类名字 —— 那些是把"结构"和"气氛"焊在一条轴上的
#: 产物。焊起来之后 Scheduler 就没法分别导演这两件事: 它想要一个
#: "身份翻转", 却被迫同时指定"诡异"; 或者它想要"温馨", 却被迫接受
#: "身份翻转"。组合空间被虚假地压缩了(`eerie + identity_flip` 这种
#: 完全合法的搭配在旧枚举里根本表达不出来)。
#:
#: `straight_explanation` 是"没有结构性翻转, 就是正面解释"那一档 ——
#: 它也**必须**留在枚举里: 配额要能数出"最近 10 题里有几道是普通解释",
#: 数不出来就没法限制它。它是默认值, 也是配额要压制的那一档。
REVEAL_MODES = (
    "recontextualization",   # 同一件事被放进新语境, 意义全变
    "identity_flip",         # 某人/某物的身份与表面相反
    "meaning_flip",          # 某件物品的真实意义与表面用途相反
    "causal_flip",           # 因果被倒置(以为的因其实是果)
    "goal_flip",             # 行为的目的与表面动机相反
    "hidden_stakes",         # 表面平常, 真实的利害关系完全不同
    "perspective_flip",      # 视角/时间/空间被错认
    "straight_explanation",  # 没有翻转, 就是正面解释为什么会这样
)

#: 揭晓结构里**默认**那一档 —— 也是配额要压制的对象。
REVEAL_DEFAULT = "straight_explanation"

DOMAINS = (
    "daily", "commerce", "medical", "transport", "maritime", "aviation",
    "military", "education", "religion", "art", "sport", "nature",
    "construction", "food", "law", "technology", "family", "workplace",
)

RELATIONS = ("stranger", "family", "spouse", "ex", "colleague", "friend",
             "neighbor", "authority", "self")

TIME_SHAPES = ("instant", "single_day", "habitual", "years_long", "generational")


# ======================================================================
@dataclass
class PuzzleFact:
    """主持人在整局游戏中判断"是/不是/无关"时依据的一条确定事实。

    这就是方案参考开源项目 `supplementary_info` 的思路, 但职责更窄:
    它是**判定依据**, 不是提示、不是线索、不是通关要求。

    kind:
      core       解谜核心事实 —— 说不到它就解不开
      support    支撑/背景事实
      exclusion  用来排除常见错误路线("他的行为不是为了纪念死者")
    visibility:
      public     谜面已经明确告诉玩家
      hidden     需要通过提问发现
    hintable:
      是否允许提示直接围绕这个事实引导。核心 mechanism 一般设 False,
      否则提示会变成剧透。
    """

    id: str
    text: str
    kind: str = "support"
    visibility: str = "hidden"
    hintable: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text, "kind": self.kind,
                "visibility": self.visibility, "hintable": bool(self.hintable)}

    @classmethod
    def from_dict(cls, d: Any) -> "PuzzleFact":
        if not isinstance(d, dict):
            return cls(id="", text=str(d or ""))
        return cls(
            id=str(d.get("id", "") or "").strip(),
            text=str(d.get("text", "") or "").strip(),
            kind=_pick(d.get("kind"), FACT_KINDS, "support"),
            visibility=_pick(d.get("visibility"), FACT_VISIBILITY, "hidden"),
            hintable=bool(d.get("hintable", True)),
        )


@dataclass
class DiscoveryBeat:
    """**正常游戏过程中, 观众应当逐层发现的一步**(quality-v8 新增)。

    ## 它解决什么问题

    v7 之前 prompt 里写着"压不进 2 条说明这题太绕, 换一个更简单的骨架"
    —— 那把 `completion_fact_ids`(1~2 条, **什么时候算解出**)当成了
    **整道题的复杂度上限**。结果题目只有 1~2 个信息点, 观众没有推理层次。

    产品规则冻结:

        **题目允许有层次, 通关必须简单。**

    ## 四个概念不能混

        facts               canonical world 的事实空间(主持人判定依据)
        discovery_beats     正常游戏应该**逐层发现什么**(叙事节拍)
        solve_atoms         对谜底的**分析**拆分(提示/复盘用)
        completion_fact_ids **最低胜利要求**(集合覆盖判定)

    例如一道题内部可以有:

        b1 先意识到时间/地点理解错了
        b2 再意识到某物的用途不是表面用途
        b3 最后理解异常行为真正的目的

    而通关仍然只要求 `completion_fact_ids = [f1, f2]`。

    ## ⚠️ 它**没有**任何运行时胜负权

    禁止(任何一条都会让系统自己把题解掉):

        beat 全覆盖     -> solved
        beat 数量       -> solved
        hint            -> 自动建立 beat
        Detective       -> 建立 beat

    Engine 的胜负判定**永远只有**一条:

        completion_fact_ids ⊆ established_fact_ids
        (且 established 只能由真人 QA 写, 见
         `RoundEngine._record_human_established_locked`)

    `discovery_beats` 本轮只用于: 生成质量 / Reviewer / archive,
    以及后续提示与 Clue Tags 的结构基础。**不进前端 Snapshot**。
    """

    id: str
    text: str
    fact_ids: list = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text,
                "fact_ids": list(self.fact_ids or [])}

    @classmethod
    def from_dict(cls, d: Any) -> "DiscoveryBeat":
        if not isinstance(d, dict):
            return cls(id="", text=str(d or ""))
        raw = d.get("fact_ids")
        ids = ([str(x).strip() for x in raw if str(x).strip()]
               if isinstance(raw, list) else [])
        return cls(id=str(d.get("id", "") or "").strip(),
                   text=str(d.get("text", "") or "").strip(),
                   fact_ids=ids)


@dataclass
class SolveAtom:
    """谜底的**分析拆分**(提示 / 解释 / 复盘用)。

    与 `PuzzleFact` 的区别: atom 是**分析视角**, fact 是**事实空间**。
    atom 通过 `fact_ids` 引用 fact, 所以"说中了 atom"与"探明了 fact"是两件事。

    ⚠️ **v5 起 atom 不再是通关合同。** 通关由
    `PuzzleSpec.completion_fact_ids` 的覆盖决定(见 `quality.validate_spec`)。
    atom 的用途是: 给提示系统选方向、给揭晓做复盘结构、给 reviewer 做
    线索回溯检查。它**不**决定观众必须说到什么程度。

    这也是 `role="key"` 存在的理由: 一道身份题的原子事实是"门外女人是
    父亲的亲生女儿", 那是 `key` 而不是 cause/mechanism —— v5 之前它被
    迫伪装成因果链的一环。
    """

    id: str
    role: str          # cause / mechanism / support
    text: str
    fact_ids: list = field(default_factory=list)
    required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "role": self.role, "text": self.text,
                "fact_ids": list(self.fact_ids), "required": bool(self.required)}

    @classmethod
    def from_dict(cls, d: Any, idx: int = 0) -> "SolveAtom":
        if not isinstance(d, dict):
            # 老数据是纯字符串 -> 按位置推 role(第 1 条 cause, 第 2 条 mechanism)
            return cls(id=f"a{idx + 1}", role=_pos_role(idx),
                       text=str(d or "").strip())
        role = str(d.get("role", "") or "").strip().lower()
        if role not in ATOM_ROLES:
            role = _pos_role(idx)
        return cls(
            id=str(d.get("id", "") or "").strip() or f"a{idx + 1}",
            role=role,
            text=str(d.get("text", "") or "").strip(),
            fact_ids=[str(x).strip() for x in (d.get("fact_ids") or [])
                      if str(x).strip()],
            required=bool(d.get("required", True)),
        )


@dataclass
class FairClue:
    """谜面原文里已经写着、回看能指向谜底的一条具体事实。

    关键升级(方案 §5): **可以被代码验证**。
        assert clue.quote in spec.puzzle
    这样"谜面有没有真正的公平线索"第一次变成了确定性条件, 而不是
    审稿人的主观判断。

    quote 必须**逐字**出现在谜面里(只允许全角/半角/引号/空白的轻度归一)。
    不做模糊语义判断。
    """

    quote: str
    supports_atoms: list = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"quote": self.quote,
                "supports_atoms": list(self.supports_atoms)}

    @classmethod
    def from_dict(cls, d: Any) -> "FairClue":
        if not isinstance(d, dict):
            # 老数据: 自由文本 "谜面写了……" -> 剥掉前缀当 quote
            t = str(d or "").strip()
            for pre in ("谜面写了", "谜面写", "题面写了", "题面写"):
                if t.startswith(pre):
                    t = t[len(pre):].strip()
                    break
            return cls(quote=t.strip("「」『』\"'“”‘’：: "))
        return cls(
            quote=str(d.get("quote", "") or "").strip(),
            supports_atoms=[str(x).strip() for x in (d.get("supports_atoms") or [])
                            if str(x).strip()],
        )


@dataclass
class PuzzleBlueprint:
    """**代码层决定的**出题硬约束(方案 §7/§12)。

    不再写"请出一个换一个完全不同的题材"然后指望模型理解什么叫"不同" ——
    代码先选好 blueprint, 生成器必须执行, 审稿人再验它有没有真的执行。
    """

    mechanism_family: str = "information_gap"
    solution_shape: str = "information_advantage"
    domain: str = "daily"
    emotion_mode: str = "neutral"
    time_shape: str = "instant"
    relation: str = "stranger"

    death: bool = False
    past_trauma: bool = False
    long_term_profession: bool = False
    repeated_ritual: bool = False

    # ---- Step 01 新增(只记录, 不改调度) ----
    #: 揭晓结构 —— **代码希望这道题往哪个方向做**。
    #:
    #: 默认 `straight_explanation` 是刻意的: 它表达"代码没有特别指定翻转时
    #: 的默认生成方向", 而不是"这是一道普通题"。Blueprint 是**指令**,
    #: 所以它需要一个确定的默认指令 —— 这一点与 `Signature` 相反
    #: (那边是**观察结果**, 缺失即未知, 见 `PuzzleSignature.reveal_mode`)。
    reveal_mode: str = REVEAL_DEFAULT

    # 注: 这里**刻意没有** `procedural_rule_dependency`。
    #
    # 它是**观察出来的事实**("这道题实际是否依赖题面之外的制度设定"),
    # 不是代码能预先下达的指令 —— 代码在出题之前根本无从知道。所以它只
    # 存在于 `PuzzleSignature`(Reviewer 读完成品后回传)。
    # 放进 Blueprint 会制造一个"代码预先指定规则依赖"的来源, 以后很容易
    # 被误用成"让模型照抄的硬约束", 那正是这个字段要避免的。

    def to_dict(self) -> dict[str, Any]:
        return {
            "mechanism_family": self.mechanism_family,
            "solution_shape": self.solution_shape,
            "domain": self.domain,
            "emotion_mode": self.emotion_mode,
            "time_shape": self.time_shape,
            "relation": self.relation,
            "death": bool(self.death),
            "past_trauma": bool(self.past_trauma),
            "long_term_profession": bool(self.long_term_profession),
            "repeated_ritual": bool(self.repeated_ritual),
            "reveal_mode": self.reveal_mode,
        }

    @classmethod
    def from_dict(cls, d: Any) -> "PuzzleBlueprint":
        d = d if isinstance(d, dict) else {}
        return cls(
            mechanism_family=_pick(d.get("mechanism_family"),
                                   MECHANISM_FAMILIES, "information_gap"),
            solution_shape=_pick(d.get("solution_shape"),
                                 SOLUTION_SHAPES, "information_advantage"),
            domain=_pick(d.get("domain"), DOMAINS, "daily"),
            emotion_mode=_pick(d.get("emotion_mode"), EMOTION_MODES, "neutral"),
            time_shape=_pick(d.get("time_shape"), TIME_SHAPES, "instant"),
            relation=_pick(d.get("relation"), RELATIONS, "stranger"),
            death=bool(d.get("death", False)),
            past_trauma=bool(d.get("past_trauma", False)),
            long_term_profession=bool(d.get("long_term_profession", False)),
            repeated_ritual=bool(d.get("repeated_ritual", False)),
            reveal_mode=_pick(d.get("reveal_mode"), REVEAL_MODES, REVEAL_DEFAULT),
        )

    def describe(self) -> str:
        """给生成器看的硬约束文本。

        ⚠️ **这个字符串直接进生产 Prompt** —— 它由 `_gen_spec_once()` 拼进
        RIDDLE 的 user 消息、由 `_review()` 拼进审稿消息。所以往这里加一行
        就是**改生产行为**, 不是改数据结构。

        ## 为什么 reveal_mode 现在**可以**印出来了

        Step 01 不暴露它, Step 04 让模型理解了它的语义但**仍然**没暴露
        目标值 —— 因为那时 `self.reveal_mode` 恒为默认, 印出去是个伪目标。

        Step 02 落地了真正的 reveal 选择器(`quality.choose_reveal_mode`),
        于是这一行现在携带真实信息: 它是调度器按 rolling quota 挑出的
        **目标结构**。模型应当朝它写, 但**如实报告实际写成了什么** ——
        `reveal_mode adherence` 检查的正是这两个值的差。
        """
        flags = [k for k in ("death", "past_trauma", "long_term_profession",
                             "repeated_ritual") if getattr(self, k)]
        return (
            f"- mechanism_family(诡计类型): {self.mechanism_family}\n"
            f"- solution_shape(解法形状): {self.solution_shape}\n"
            f"- domain(领域): {self.domain}\n"
            f"- relation(人物关系): {self.relation}\n"
            f"- emotion_mode(情绪基调): {self.emotion_mode}\n"
            f"- time_shape(时间形态, 参考): {self.time_shape}\n"
            f"- reveal_mode(揭晓结构, **目标**): {self.reveal_mode}\n"
            f"- 必须为真的标记: {', '.join(flags) if flags else '(无)'}\n"
            f"- 必须为假的标记: "
            f"{', '.join(k for k in ('death', 'past_trauma', 'long_term_profession', 'repeated_ritual') if not getattr(self, k))}"
        )


@dataclass
class PuzzleSignature:
    """一道题的**可比较指纹**, 用于跨题去重与配额(方案 §10)。

    全部字段都是代码可直接统计的 —— 不含任何需要语义判断的东西。
    "职业不同 = 题目不同"这种依据**不在这里**, 因为它正是坍缩的来源。
    """

    mechanism_family: str = ""
    solution_shape: str = ""
    domain: str = ""
    emotion_mode: str = ""
    relation: str = ""
    # 时间形态也进 signature —— 它是要跟 blueprint 严格比对的一维,
    # 只放在 blueprint 上就没法验模型有没有真的照做。
    time_shape: str = ""

    death: bool = False
    past_trauma: bool = False
    long_term_profession: bool = False
    repeated_ritual: bool = False

    # ---- Step 01 新增(只统计, 不参与蓝图逐项比对) ----
    #: 揭晓结构(observed)。与 blueprint 的 `reveal_mode` 同义, 但这里是
    #: **审稿人读完之后如实回传**的值 —— 配额统计用它, 不用自报值。
    #:
    #: 默认 **`""`(未知)**, 不是 `straight_explanation`。理由: Signature 是
    #: **观察结果**, 它上面其余字段(`mechanism_family` / `domain` / …)在
    #: 缺失时也都是 `""`。老题没有这个字段, 真实含义是"没观察过", 不是
    #: "观察结果是普通解释"。若默认成 straight, 几百道历史题会被一律读成
    #: straight, 离线分析就会得到"历史题全是普通解释"这个**假结论**。
    #:
    #: 那"旧题借 `""` 绕过 reveal 配额"怎么办? —— 不在这一步用伪造 observed
    #: 数据去掩盖。正确位置是 Step 03: 旧 policy 题走 quarantine, 根本不
    #: 参与 v4 live inventory。
    reveal_mode: str = ""
    #: 是否主要依赖题面之外的制度性设定(observed)。由 Reviewer 单题判断,
    #: 代码只管最近窗口的配额。
    procedural_rule_dependency: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "mechanism_family": self.mechanism_family,
            "solution_shape": self.solution_shape,
            "domain": self.domain,
            "emotion_mode": self.emotion_mode,
            "relation": self.relation,
            "time_shape": self.time_shape,
            "death": bool(self.death),
            "past_trauma": bool(self.past_trauma),
            "long_term_profession": bool(self.long_term_profession),
            "repeated_ritual": bool(self.repeated_ritual),
            "reveal_mode": self.reveal_mode,
            "procedural_rule_dependency": bool(self.procedural_rule_dependency),
        }

    @classmethod
    def from_dict(cls, d: Any) -> "PuzzleSignature":
        d = d if isinstance(d, dict) else {}
        return cls(
            mechanism_family=str(d.get("mechanism_family", "") or ""),
            solution_shape=str(d.get("solution_shape", "") or ""),
            domain=str(d.get("domain", "") or ""),
            emotion_mode=str(d.get("emotion_mode", "") or ""),
            relation=str(d.get("relation", "") or ""),
            time_shape=str(d.get("time_shape", "") or ""),
            death=bool(d.get("death", False)),
            past_trauma=bool(d.get("past_trauma", False)),
            long_term_profession=bool(d.get("long_term_profession", False)),
            repeated_ritual=bool(d.get("repeated_ritual", False)),
            # 缺失 / 非法一律回 `""`(未知), **不**回落成 straight_explanation。
            # 见字段注释: 那是把"没观察过"伪造成"观察结果是普通解释"。
            reveal_mode=str(d.get("reveal_mode", "") or "")
            if str(d.get("reveal_mode", "") or "") in REVEAL_MODES else "",
            procedural_rule_dependency=bool(
                d.get("procedural_rule_dependency", False)),
        )

    def grief(self) -> bool:
        return self.emotion_mode in GRIEF_MODES

    def trauma_ritual(self) -> bool:
        """既往创伤 + 长年怪规矩 —— 8 小时直播里 69% 的题都是这个形状。"""
        return self.past_trauma and self.repeated_ritual


# ======================================================================
@dataclass
class PuzzleSpec:
    """一道题的完整结构化定义。"""

    id: str = ""
    title: str = ""
    puzzle: str = ""          # 谜面
    answer: str = ""          # 谜底(完整解释, 可有背景与故事性细节)
    #: **核心答案** —— 普通人一听就知道"这题到底怎么回事"的一句话。
    #:
    #: 与 `answer` 的分工(这是 v5 最重要的拆分):
    #:     answer      = 完整解释, 允许背景、补充、故事性细节
    #:     core_answer = 一句话核心, 必须直接回答谜面最后那个问题,
    #:                   不能依赖额外脑补, 推荐 ≤60 汉字, 硬上限 80, 不换行
    #:
    #: 为什么需要它: 直播里"揭晓"过去是让第二个 LLM 把文学谜底重新加工
    #: 一遍, 于是观众等了 1 次额外调用, 拿到的却是一段更绕的文字。有了
    #: core_answer, 揭晓可以**确定性**地先把它原样念出来(零 LLM 调用),
    #: 保证"人话"一定先出现。
    core_answer: str = ""
    #: **通关合同** —— 观众房间必须真正建立的最小核心事实集合(1~2 条)。
    #:
    #: 只允许指向 `kind="core"` 且 `visibility="hidden"` 的 fact。
    #: support / exclusion **永远不得**作为通关要求 —— 它们是背景与排除项,
    #: 不是这道题的解法本身。
    #:
    #: 语义与 solve_atoms **彻底分开**:
    #:     completion_fact_ids = 胜利合同(代码做集合覆盖判定)
    #:     solve_atoms         = 提示 / 解释 / 复盘结构
    #:
    #: 这正是"共同推理"的机制: 房间已公开确认的事实会累计, 最后补齐缺口的
    #: 那位观众立即触发揭晓, 不要求他复述别人已经推出来的部分。
    #:
    #: **1~2 条是硬上限。** 一道题若压不进 2 条, 那是题本身太绕, 应该
    #: rewrite —— 而不是把门槛放宽成 4、5 条。
    completion_fact_ids: list = field(default_factory=list)

    facts: list = field(default_factory=list)          # list[PuzzleFact]
    solve_atoms: list = field(default_factory=list)     # list[SolveAtom]
    #: quality-v8: 正常游戏应逐层发现的步骤(2~4 条)。**没有胜负权** ——
    #: 见 `DiscoveryBeat` 的 docstring。旧 archive 里没有这一项, 读到空表。
    discovery_beats: list = field(default_factory=list)  # list[DiscoveryBeat]
    fair_clues: list = field(default_factory=list)      # list[FairClue]
    hints: list = field(default_factory=list)           # list[str]

    blueprint: PuzzleBlueprint = field(default_factory=PuzzleBlueprint)
    signature: PuzzleSignature = field(default_factory=PuzzleSignature)

    prompt_version: str = ""
    quality_policy_version: str = ""

    # ---- 非持久化的生成元信息(usage/model 等) ----
    usage: Optional[dict] = None
    model: Optional[str] = None
    error: Optional[str] = None
    # ---- 生成/审稿过程指标(方案 §35) ----
    # 放在 spec 上而不是另开一条通道: archive 是**按题**写的, 而这些
    # 数字天然属于"这一题是怎么来的"。由 gen_spec 填, director 落盘。
    # 默认空字典 -> 老调用方/兜底题不必关心它。
    metrics: dict = field(default_factory=dict)
    #: 这道题的 blueprint 是**真的被分配过**(调度器选的), 还是只是
    #: dataclass 默认值? 由 gen_spec 显式写, **绝不从值推断** ——
    #: `information_gap + information_advantage` 是合法调度结果,
    #: 光看值分不出它和"没分配"的区别, 两个方向都会算错分布。
    #: 兜底题 / 自由生成 / 老数据 = False。
    blueprint_specified: bool = False

    # ---- Batch H2-F: curated(外部题库)来源溯源 ----
    #
    # 这些字段**只用于归档 / attribution / 排查 / 版权追踪**, 并且
    # **绝不下发直播前端**。理由:
    #
    #   1. 观众不需要知道题目来自 Stack Exchange;
    #   2. attribution 里有作者昵称与链接, 下发等于把第三方个人信息
    #      推进直播流;
    #   3. 许可条款要求的是"复用者提供署名", 那是**我们**在归档/展示
    #      层面履行的义务, 不是把它塞进每条 API 响应。
    #
    # 默认全空 = "不是 curated 题"(自由生成的老路径)。**不从任何值
    # 推断** —— 与 `blueprint_specified` 同一条原则。
    #:
    #: "curated" 表示来自外部题库; "" 表示自由生成。
    source_type: str = ""
    #: 来源名, 如 "Puzzling Stack Exchange" / "TurtleBench1.5k"。
    external_source: str = ""
    #: 来源内的稳定 id, 如 "pse:q:12345"。
    external_id: str = ""
    #: 原帖/数据集链接。
    source_url: str = ""
    #: question 侧许可证。answer 侧可能不同(跨版本), 所以分开存。
    license: str = ""
    answer_license: str = ""
    #: 完整署名信息(dict)。H2-H 的 ATTRIBUTIONS.jsonl 从这里取。
    attribution: dict = field(default_factory=dict)
    #: AI 审题时打的风格标签(identity_flip / perspective_flip …)。
    #: 验收要按它统计"认知反转占比 >= 70%"。
    style_tags: list = field(default_factory=list)

    # ------------------------------------------------------------------
    # 便捷访问 —— engine / llm 需要"原子事实的文本列表"这类视图
    # ------------------------------------------------------------------
    def atom_by_id(self) -> dict:
        return {a.id: a for a in self.solve_atoms}

    def fact_by_id(self) -> dict:
        return {f.id: f for f in self.facts}

    def atom_lines(self) -> list:
        """给裁判/prompt 用的 "role|text" 行。"""
        return [f"[{a.role}] {a.text}" for a in self.solve_atoms]

    def required_atoms(self) -> list:
        return [a for a in self.solve_atoms if a.required]

    def core_hidden_facts(self) -> list:
        return [f for f in self.facts
                if f.kind == "core" and f.visibility == "hidden"]

    def completion_facts(self) -> list:
        """通关合同指向的 fact 对象(按 completion_fact_ids 的顺序)。

        找不到的 id 直接跳过 —— 调用方拿到的永远是真实存在的 fact。
        但这**不代表**校验通过: `validate_spec` 会独立检查每个 id 都存在。
        """
        by_id = self.fact_by_id()
        return [by_id[fid] for fid in (self.completion_fact_ids or [])
                if fid in by_id]

    def has_completion_contract(self) -> bool:
        """这道题有没有 v5 通关合同?

        这是"走新路径还是 legacy 路径"的**唯一判据** —— 有合同就用代码集合
        覆盖判定胜负, 没有就回落到旧的 solution_candidate + Final Judge。
        绝不用 spec_version / quality_policy_version 去推断, 因为旧 archive
        宽容读出来的 spec 这两者都可能是空的。
        """
        return bool(self.completion_fact_ids)

    #: 序列化时**必须**带上的非内容字段。
    #:
    #: 早先 `to_dict` 只写"内容字段"(puzzle/facts/atoms/...), 把
    #: `usage/model/error/metrics` 全漏了。后果不是"少几个无关紧要的
    #: 元信息", 而是**生成溯源整个丢失**: `metrics` 是
    #: generation_attempts / review_calls / rewrite_count / review_decision /
    #: review_latency_ms_total 唯一的家。任何"存下来再读回来"的用法
    #: (题池、复盘、离线分析)都会静默拿到空 metrics。
    #:
    #: `_apply_review` 一直是**刻意**保留这四个字段的(llm.py), 说明
    #: "该保留"早有共识 —— 只是 to_dict 没跟上。这里把它们显式列出来,
    #: 免得以后再漏。
    _PROVENANCE_KEYS = ("usage", "model", "error", "metrics")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "title": self.title,
            "puzzle": self.puzzle, "answer": self.answer,
            # ---- v5 通关合同(与 answer 分开, 见字段注释) ----
            "core_answer": self.core_answer,
            "completion_fact_ids": list(self.completion_fact_ids or []),
            "facts": [f.to_dict() for f in self.facts],
            "solve_atoms": [a.to_dict() for a in self.solve_atoms],
            "discovery_beats": [b.to_dict() for b in self.discovery_beats],
            "fair_clues": [c.to_dict() for c in self.fair_clues],
            "hints": list(self.hints),
            "blueprint": self.blueprint.to_dict(),
            "signature": self.signature.to_dict(),
            "prompt_version": self.prompt_version,
            "quality_policy_version": self.quality_policy_version,
            "blueprint_specified": bool(self.blueprint_specified),
            # ---- H2-F: curated 溯源 ----
            # 跟着 archive 走(归档/attribution/排查要它), 但**不进前端**。
            "source_type": self.source_type,
            "external_source": self.external_source,
            "external_id": self.external_id,
            "source_url": self.source_url,
            "license": self.license,
            "answer_license": self.answer_license,
            "attribution": dict(self.attribution or {}),
            "style_tags": list(self.style_tags or []),
            # ---- 生成溯源(见 _PROVENANCE_KEYS 的说明) ----
            "usage": self.usage,
            "model": self.model,
            "error": self.error,
            "metrics": dict(self.metrics or {}),
        }

    def to_archive(self) -> dict[str, Any]:
        """落盘用(方案 §34)。含 spec_version, 便于以后迁移。

        `blueprint_specified`: 这道题**有没有**真的被分配 blueprint。
        为什么需要这个布尔: `PuzzleBlueprint()` 的默认值长得和"真的
        分配了 information_gap"一模一样, 落盘后无法区分。分析时若把
        默认值当成真实调度结果, 分布统计就全错了(兜底题/老数据都会
        混进来)。所以显式记一个标记。
        """
        d = self.to_dict()
        d["spec_version"] = 4
        d["blueprint_specified"] = bool(self.blueprint_specified)
        d["signature_present"] = bool(
            self.signature and (self.signature.mechanism_family
                                or self.signature.solution_shape))
        return d

    @classmethod
    def from_dict(cls, d: Any) -> "PuzzleSpec":
        """宽容读入 —— 老的 archive 只有 puzzle/answer 也能读出来。"""
        if not isinstance(d, dict):
            return cls()
        spec = cls(
            id=str(d.get("id", "") or ""),
            title=str(d.get("title", "") or ""),
            puzzle=str(d.get("puzzle", "") or ""),
            answer=str(d.get("answer", "") or ""),
            # ---- v5 通关合同 ----
            # 老 archive 没有这两把键 -> 宽容读成空。空 completion 的语义是
            # "这道题没有 v5 合同", 于是运行时回落到 legacy Final Judge 路径
            # (见 `has_completion_contract`)。**绝不**从 answer 反推一个
            # 合同出来 —— 那等于偷偷给旧题编一个通关条件。
            core_answer=str(d.get("core_answer", "") or ""),
            completion_fact_ids=[str(x).strip()
                                 for x in (d.get("completion_fact_ids") or [])
                                 if str(x).strip()],
            facts=[PuzzleFact.from_dict(x) for x in (d.get("facts") or [])],
            solve_atoms=[SolveAtom.from_dict(x, i)
                         for i, x in enumerate(d.get("solve_atoms") or [])],
            # quality-v8: 旧 archive 没有这一项 -> 读到空表(合法, 只是没
            # 有 v8 的层次信息)。**不**从 atoms/facts 反推 —— 那等于给
            # 旧题编一个它从来没声明过的层次结构。
            discovery_beats=[DiscoveryBeat.from_dict(x)
                             for x in (d.get("discovery_beats") or [])],
            fair_clues=[FairClue.from_dict(x) for x in (d.get("fair_clues") or [])],
            hints=[str(h).strip() for h in (d.get("hints") or []) if str(h).strip()],
            blueprint=PuzzleBlueprint.from_dict(d.get("blueprint")),
            signature=PuzzleSignature.from_dict(d.get("signature")),
            prompt_version=str(d.get("prompt_version", "") or ""),
            quality_policy_version=str(d.get("quality_policy_version", "") or ""),
            blueprint_specified=bool(d.get("blueprint_specified", False)),
            # ---- H2-F: curated 溯源(老 archive 没有 -> 宽容读成空) ----
            source_type=str(d.get("source_type", "") or ""),
            external_source=str(d.get("external_source", "") or ""),
            external_id=str(d.get("external_id", "") or ""),
            source_url=str(d.get("source_url", "") or ""),
            license=str(d.get("license", "") or ""),
            answer_license=str(d.get("answer_license", "") or ""),
            attribution=(dict(d.get("attribution"))
                         if isinstance(d.get("attribution"), dict) else {}),
            style_tags=[str(x).strip() for x in (d.get("style_tags") or [])
                        if str(x).strip()],
            # ---- 生成溯源 ----
            # 用 `.get()` 而不是 `d[...]`: 现存 archive 里绝大多数
            # (实测 data/puzzle.jsonl 105 条中 103 条)是这四把键出现
            # **之前**写的, 必须照样能读出来 —— 读不出就退化成默认值,
            # 绝不能抛异常。老记录的 metrics 为空是**正确**语义
            # ("那时候还没记"), 不是数据损坏。
            usage=(d.get("usage") if isinstance(d.get("usage"), dict)
                   else None),
            model=(str(d.get("model")) if d.get("model") else None),
            error=(str(d.get("error")) if d.get("error") else None),
            metrics=(dict(d.get("metrics"))
                     if isinstance(d.get("metrics"), dict) else {}),
        )
        return spec

    # ------------------------------------------------------------------
    @classmethod
    def from_legacy_riddle_result(cls, r: Any) -> "PuzzleSpec":
        """从老的 `RiddleResult` 迁移(方案 §48 Phase A)。

        `RiddleResult` 没有 facts/blueprint/signature, 所以:
          - facts 由 atoms 反推(**只用来说明"这条题为什么能判", 不假装
            它是完整事实空间**);
          - blueprint/signature 留空 —— 由 quality.py 的调度器补。
        """
        atoms_raw = list(getattr(r, "solve_atoms", None) or [])
        clues_raw = list(getattr(r, "fair_clues", None) or [])
        atoms = [SolveAtom.from_dict(a, i) for i, a in enumerate(atoms_raw)]
        facts = []
        for i, a in enumerate(atoms):
            fid = f"f{i + 1}"
            a.fact_ids = a.fact_ids or [fid]
            facts.append(PuzzleFact(id=fid, text=a.text,
                                    kind="core" if a.role in ("cause", "mechanism")
                                    else "support",
                                    visibility="hidden", hintable=True))
        return cls(
            title=getattr(r, "title", None) or "",
            puzzle=getattr(r, "puzzle", None) or "",
            answer=getattr(r, "answer", None) or "",
            # 老 `RiddleResult` 没有通关合同的概念 —— 它是 legacy 形态,
            # 运行时靠 `solution_candidate` + Final Judge 通关。
            # **不**在这里编一个 completion 出来(见 from_dict 的同款说明)。
            core_answer="",
            completion_fact_ids=[],
            facts=facts,
            solve_atoms=atoms,
            fair_clues=[FairClue.from_dict(c) for c in clues_raw],
            hints=list(getattr(r, "hints", None) or []),
            usage=getattr(r, "usage", None),
            model=getattr(r, "model", None),
            error=getattr(r, "error", None),
        )

    def to_riddle_result(self):
        """反向兼容: 给还在用 RiddleResult 的 director 用(方案 §48 Phase A)。

        atoms 转回 `[{role,text}]` —— 这正是 llm/engine 现在认的形态。
        """
        from .llm import RiddleResult
        return RiddleResult(
            puzzle=self.puzzle or None,
            answer=self.answer or None,
            hints=list(self.hints),
            title=self.title or None,
            error=self.error,
            usage=self.usage, model=self.model,
            # ---- v5 通关合同必须一路带到 runtime ----
            # Engine 靠这两个字段决定"走代码集合覆盖还是走 legacy Final
            # Judge"。漏传 = 新题被当成老题, 通关又回到 cause+mechanism
            # 那条链 —— 正是本批要修的东西。
            core_answer=self.core_answer or "",
            completion_fact_ids=list(self.completion_fact_ids or []),
            solve_atoms=[{"role": a.role, "text": a.text,
                          "id": a.id, "fact_ids": list(a.fact_ids),
                          "required": bool(a.required)}
                         for a in self.solve_atoms],
            fair_clues=[c.to_dict() for c in self.fair_clues],
        )


# ======================================================================
# 归一化辅助
# ======================================================================
def _pick(v: Any, allowed: tuple, default: str) -> str:
    s = str(v or "").strip().lower()
    return s if s in allowed else default


def _pos_role(idx: int) -> str:
    """老数据没给 role 时按位置推: 头两条是 cause / mechanism。"""
    return "cause" if idx == 0 else "mechanism" if idx == 1 else "support"


# 允许的轻度归一: 全角/半角、空白、常见中文引号(方案 §5)
_PUNCT_MAP = {
    "，": ",", "。": ".", "！": "!", "？": "?", "：": ":", "；": ";",
    "（": "(", "）": ")", "“": '"', "”": '"', "‘": "'", "’": "'",
    "「": '"', "」": '"', "『": '"', "』": '"', "、": ",",
}


def normalize_for_match(text: str) -> str:
    """把文本压成"只比字"的形式, 用于 fair clue 的包含判定。

    去掉所有空白与标点, 全角转半角。**不做**模糊语义判断 ——
    只要 quote 的字面内容真的在谜面里出现过就算通过。
    """
    if not text:
        return ""
    out = []
    for ch in text:
        ch = _PUNCT_MAP.get(ch, ch)
        if ch.isspace():
            continue
        if ch in ",.!?:;()\"'-—…·":
            continue
        out.append(ch)
    return "".join(out)


def quote_in_puzzle(quote: str, puzzle: str) -> bool:
    """fair clue 的 quote 是否**真的**出现在谜面里(方案 §5 的硬条件)。

    允许轻度归一, 不做模糊语义判断。
    """
    q = normalize_for_match(quote)
    if not q:
        return False
    return q in normalize_for_match(puzzle)


#: 谜面里不许出现的污染标记(方案 §21)
META_MARKS = ("【谜底】", "【提示】", "【答案】", "【汤底】", "【谜面附注】",
              "【附注】", "【说明】", "谜底揭晓前", "读者可先", "以下提示",
              "提示如下", "解题提示", "本题提示", "先看提示")


def has_meta_text(puzzle: str) -> bool:
    return any(m in (puzzle or "") for m in META_MARKS)


_CLOSING_Q = re.compile(r"[?？][\"'”’」』）)】\s]*$")


def has_closing_question(puzzle: str) -> bool:
    """谜面结尾是不是问句(海龟汤的硬格式要求)。"""
    return bool(_CLOSING_Q.search((puzzle or "").strip()))


_FIRST_PERSON = re.compile(r"我")


def is_first_person(puzzle: str) -> bool:
    """谜面是不是第一人称叙事(引语里的"我"不算)。"""
    if not puzzle:
        return False
    s = re.sub(r"[「『“\"'][^」』”\"']*[」』”\"']", "", puzzle)
    s = re.sub(r"[（(][^）)]*[）)]", "", s)
    return bool(_FIRST_PERSON.search(s))


# ======================================================================
# 运行时 spec 身份(Batch B closeout / Step 06 冻结的 spec_key)
# ======================================================================
#: 运行时身份用的哈希长度。与 `pool.KEY_LEN` 相同纯属巧合 —— 两者是
#: **不同的身份**, 见下面 `runtime_spec_key` 的说明。
RUNTIME_KEY_LEN = 16


def runtime_spec_key(puzzle: str = "", answer: str = "",
                     facts: Optional[list] = None,
                     solve_atoms: Optional[list] = None,
                     fair_clues: Optional[list] = None,
                     core_answer: str = "",
                     completion_fact_ids: Optional[list] = None) -> str:
    """一道题在**运行时**的规范身份(内容哈希)。

    任务书 Step 06 冻结的身份是 `round_index + spec_key +
    quality_policy_version` 三者合用: `round_index` 是**时序**身份(第几
    题), 这个是**内容**身份(这一题的世界是什么)。两者互补 —— 只有
    round 时, "同一题被重出一稿"与"换了一题"分不开; 只有内容时,
    两道内容相同的题会被误判成同一题。

    ## 为什么**不**复用 `story.pool.spec_key()`

    那个是**持久化 used 账本**的 key: 它的哈希输入(含 title/id)一旦改动,
    盘上老记录算出来的 key 就变了 —— 等于所有播过的题集体复活。这是
    绝不能碰的东西。

    而运行时身份要的是"这一题的**世界**是什么", 所以输入固定为:
        puzzle / answer / core_answer / completion_fact_ids
        / facts / solve_atoms / fair_clues
    刻意**不含** title 与 id(它们不是世界的一部分), 也**不含**
    signature / metrics / 时间戳(那些是观察与元信息, 换个说法不该改变
    "这是同一道题"的判断)。

    ## v5: 通关合同**必须**进哈希(硬要求)

    这条是 v5 新增的, 而且是本批最容易漏掉、后果最隐蔽的一条:

        同一个谜面 + 同一个谜底, 只改了 `completion_fact_ids`
        -> 这已经是**另一道题**了(观众要建立的事实不同, 通关时刻不同)

    若它不进哈希, `runtime_spec_key` 会认为"还是那一稿", 于是异步回调
    (ANSWER / HINT / REVEAL)的 `expect_spec_key` 复核**照样通过** ——
    一道按旧合同在飞的 ANSWER 会把 established 写进新合同的题里。这是
    identity bug, 与"新谜底 + 旧 facts"同一类。

    `core_answer` 同理: 它决定揭晓时念给观众听的那句话。

    ## 用途

    Engine 接受一道题时保存它; 这道题相关的异步回调(ANSWER / HINT /
    REVEAL)带上 `expect_spec_key`, 回调在**写状态之前**复核 —— 迟到/
    串题的结果一律丢弃。Step 14 的 Detective reservation 也依赖它。

    不抛异常: 任何输入都返回一个字符串(空题也有确定的 key)。
    """
    def _canon(item) -> dict:
        """把 fact / atom / clue 归一成**完整**的规范字典。

        ⚠️ 早先的实现每项只取 `id + text` —— 那会漏掉:
          - atom 的 `role` / `fact_ids` / `required`;
          - fact 的 `kind` / `visibility` / `hintable`;
          - **`FairClue` 根本没有 `text` 字段**(它只有 quote +
            supports_atoms), 所以不同线索的内容几乎没进哈希 —— 两条
            quote/指向都不同的 clue 会算出同一个 key。
        这些字段都是"这一题的世界"的一部分, 漏掉就等于身份判错。
        """
        if item is None:
            return {}
        if isinstance(item, dict):
            d = dict(item)
        elif hasattr(item, "to_dict"):
            d = dict(item.to_dict())
        else:
            # 裸字符串(老格式 atoms): 归一成一个确定的形状, 不要
            # 让它在下面按 getattr 静默变成空字典。
            return {"_raw": str(item)}
        # 只保留可 JSON 序列化的基本类型; 其余转成字符串, 保证稳定。
        out = {}
        for k in sorted(d):
            v = d[k]
            if isinstance(v, (list, tuple)):
                out[k] = [str(x) for x in v]
            elif isinstance(v, (str, int, float, bool)) or v is None:
                out[k] = v
            else:
                out[k] = str(v)
        return out

    def _canon_list(xs) -> list:
        return [_canon(x) for x in (xs or [])]

    raw = json.dumps({
        "puzzle": str(puzzle or ""),
        "answer": str(answer or ""),
        # v5: 通关合同进哈希 —— 见函数 docstring 的"硬要求"一节。
        # `completion_fact_ids` **排序后**再哈希: 它是一个**集合**语义的
        # 字段("要覆盖这几条"), 顺序不改变含义。不排序的话, 审稿人把
        # fact_ids 顺序调一下就会算出新 key, 把同一道题判成两道。
        "core_answer": str(core_answer or ""),
        "completion_fact_ids": sorted(
            str(x) for x in (completion_fact_ids or []) if str(x).strip()),
        "facts": _canon_list(facts),
        "solve_atoms": _canon_list(solve_atoms),
        "fair_clues": _canon_list(fair_clues),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:RUNTIME_KEY_LEN]
