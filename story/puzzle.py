#!/usr/bin/env python
# coding: utf-8
"""PuzzleSpec —— 一道题的**结构化事实基准**。

为什么要从 `RiddleResult`(谜面+谜底两段文本)升级到 spec:

    以前一道题就是两段文学文本。于是"观众说到什么程度算猜中"只能靠模型
    读一遍谜底凭感觉判 —— 8 小时直播实测下来, 它频繁宽判:
    "是纪念某个人" / "以前出过事故" 这种万能悲情猜法都能通关。

    现在把"这道题的世界里什么是真的"固定成一份**有限事实表**:
        facts        主持人整局判断"是/不是/无关"的事实空间
        solve_atoms  玩家必须说中的原子事实(引用 facts), cause+mechanism
        fair_clues   谜面原文里已经写着、回看能指向谜底的具体事实
        hints        递进提示

    四者职责**必须分开**, 不能混成一种数据:
        facts        ≠ hints   (事实是判定依据, 提示是给方向)
        facts        ≠ atoms   (atoms 是 facts 的一个子集视角 + 通关要求)
        atoms        ≠ clues   (clues 是题面抓手, 不是判定要求)

设计原则(方案 §69):
    内容导演权在代码(blueprint/quota/通关规则), 创作能力在 LLM(具体内容),
    事实基准在 PuzzleSpec, 通关权在代码。

零新依赖。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

# ======================================================================
# 枚举(用普通常量而不是 Enum —— 要能原样进出 JSON, 且老数据能宽容读)
# ======================================================================
FACT_KINDS = ("core", "support", "exclusion")
FACT_VISIBILITY = ("public", "hidden")
ATOM_ROLES = ("cause", "mechanism", "support")

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
class SolveAtom:
    """玩家必须说中的原子事实。

    与 `PuzzleFact` 的区别: atom 是**通关要求**(有 role 和 required),
    fact 是**事实空间**(所有可判定的事实)。atom 通过 `fact_ids` 引用 fact,
    所以"说中了 atom"与"探明了 fact"是两件事。
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

        ## 为什么这里**仍然**没有 reveal_mode 这一行

        Step 01 刻意不暴露它; Step 04 让模型**理解**了这个字段的语义
        (RIDDLE_SYSTEM / CHECK_SYSTEM 的正交说明 + 两边 tool schema),
        但**没有**在这里暴露一个"目标值"。

        理由是顺序: 这一行印出去的是**调度器想要的** reveal_mode。在
        Step 02 的 reveal 调度器落地之前, `self.reveal_mode` 恒为默认值,
        印出去只会给模型一个"永远等于 straight_explanation 的伪目标",
        反而污染 observed_signature(模型会以为题目就该是普通解释)。

        等 Step 02 有了真正的 reveal 选择器, 再加这一行 —— 那时它才是
        一个有信息量的约束, 也才配得上 `reveal_mode adherence` 检查。
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
    answer: str = ""          # 谜底

    facts: list = field(default_factory=list)          # list[PuzzleFact]
    solve_atoms: list = field(default_factory=list)     # list[SolveAtom]
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
            "facts": [f.to_dict() for f in self.facts],
            "solve_atoms": [a.to_dict() for a in self.solve_atoms],
            "fair_clues": [c.to_dict() for c in self.fair_clues],
            "hints": list(self.hints),
            "blueprint": self.blueprint.to_dict(),
            "signature": self.signature.to_dict(),
            "prompt_version": self.prompt_version,
            "quality_policy_version": self.quality_policy_version,
            "blueprint_specified": bool(self.blueprint_specified),
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
        d["spec_version"] = 2
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
            facts=[PuzzleFact.from_dict(x) for x in (d.get("facts") or [])],
            solve_atoms=[SolveAtom.from_dict(x, i)
                         for i, x in enumerate(d.get("solve_atoms") or [])],
            fair_clues=[FairClue.from_dict(x) for x in (d.get("fair_clues") or [])],
            hints=[str(h).strip() for h in (d.get("hints") or []) if str(h).strip()],
            blueprint=PuzzleBlueprint.from_dict(d.get("blueprint")),
            signature=PuzzleSignature.from_dict(d.get("signature")),
            prompt_version=str(d.get("prompt_version", "") or ""),
            quality_policy_version=str(d.get("quality_policy_version", "") or ""),
            blueprint_specified=bool(d.get("blueprint_specified", False)),
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
