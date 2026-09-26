#!/usr/bin/env python
# coding: utf-8
"""Haiguitang Protocol v1 —— 海龟汤语义合同的**单一代码来源**(Issue #48)。

## 这个模块是什么、不是什么

    是:  协议版本枚举 / category 与 difficulty 枚举 / 各版本的
         completion 条数合同 / v1 确定性校验 / GenerationBrief。
    不是: 第二套 PuzzleSpec validator。

`quality.validate_spec` 里的既有规则(fact id / atom refs / core_answer /
fair clue / discovery beat / hints)是**所有版本共用的共同规则**, 仍然
只住在 quality.py。这里只增加**版本专属合同** —— `validate_spec` 在
合适的入口调用 `validate_protocol`, 两层合起来才是一次完整校验。

## 四个"版本"不要混(协议全文见 haiguitang/protocol/v1.md)

    spec_version            JSON/archive 数据结构版本(现为 5)
    protocol_version        这道题遵循哪套**海龟汤语义合同**(本模块)
    quality_policy_version  当前项目准入质量政策(quality-v13)
    prompt_version          生成 Prompt 版本(riddle-v9 等)

## Phase A 纪律(Issue #48 冻结)

    1. 生产生成器**不**产 v1 —— 当前自由生成继续写
       protocol_version=""(legacy/current schema), 直播照常跑。
    2. 不 bump QUALITY_POLICY_VERSION —— 新 schema 是 opt-in,
       现有 quality-v13 池**不得**被本协议隔离。
    3. 不做隐式升级 —— 旧题缺 difficulty/categories/public_text
       就保持未知, 绝不猜、绝不补、绝不改名 reuse。

零新依赖, 不 import story 其它模块(quality.py 会 import 本模块,
反过来就循环了; PuzzleSpec 用鸭子类型访问, 不需要类型导入)。
"""

from __future__ import annotations

from dataclasses import dataclass

# ======================================================================
# 版本
# ======================================================================
#: 历史 v1 协议(11 类)。语义合同见 `haiguitang/protocol/v1.md`。
#: **已冻结**: v1 的历史语义(complete 2~4 / 11 类枚举)绝不改写 ——
#: 盘上已入池的 v1 题必须继续按 v1 validator 判断。
PROTOCOL_V1 = "haiguitang-v1"

#: 当前协议版本(5 大类)。语义合同 = v1 的全部结构合同, 只把主题枚举
#: 收敛成 5 个稳定大类(体验轴: 观众玩这道题主要获得哪种体验)。
PROTOCOL_V2 = "haiguitang-v2"

#: **当前正式协议**。新生成题一律写 v2; v1 只存在于历史数据。
HAIGUITANG_PROTOCOL_VERSION = PROTOCOL_V2

#: 全部受支持的 protocol_version。
#:
#:     ""              legacy/current production schema(质量合同由
#:                     quality_policy_version 一侧拥有, 协议层不加门)
#:     haiguitang-v1   历史协议(11 类), 只存在于已入池的存量题
#:     haiguitang-v2   当前协议(5 类), 新生成题一律写它
#:
#: **任何其它非空值 fail closed**(见 `validate_protocol`)。绝不能
#: "不认识就当 legacy" —— 否则将来 haiguitang-v999 的数据流进旧程序,
#: 会被静默按 legacy 语义播出。
SUPPORTED_PROTOCOL_VERSIONS = ("", PROTOCOL_V1, PROTOCOL_V2)

#: 与 v1 共享同一套"v1 时代结构合同"的版本集合(completion 2~4 /
#: core hidden 上限 4)。v2 延续 v1 的这些合同(§六: 本轮不改语义),
#: 所以 quality 层的分档判据用集合成员, **不是** `== v1`。
PROTOCOL_V1_STYLE = (PROTOCOL_V1, PROTOCOL_V2)

# ======================================================================
# difficulty —— 固定枚举(协议 §12)
# ======================================================================
#: 难度只允许这三档。**completion 条数与难度是两条独立轴**
#: (协议 §17: 2 facts ≠ easy, 4 facts ≠ hard), 将来用真实直播数据校准。
DIFFICULTIES = ("easy", "medium", "hard")

# ======================================================================
# category —— v1 历史枚举(11 类, 冻结) 与 v2 当前枚举(5 类, 冻结)
# ======================================================================
#: **v1 历史** canonical 主题枚举, 精确 11 类(协议 v1 §13, 已冻结)。
#: 只存在于 `protocol_version="haiguitang-v1"` 的存量题里;
#: 新生成路径**禁止**使用这些值。**不要改拼写, 不要删改** ——
#: 盘上已入池的 v1 题要继续按这份枚举校验。
V1_CATEGORIES = (
    "logic",
    "suspense",
    "horror",
    "twist",
    "brainstorm",
    "family",
    "crime",
    "tragedy",
    "warm",
    "comedy",
    "sci_fi",
)

#: **v2 当前** canonical 主题枚举, 精确 5 类(冻结的产品决定)。
#:
#: 这五类表示"观众玩这道题时, **主要获得的是哪种体验**", 不是简单看
#: 故事里出现了什么元素:
#:
#:     logic       逻辑      payoff 是"推出来了"(因果/时间/身份/规则推导)
#:     suspense    悬疑      调查/犯罪谜团/隐藏事件/真相追查
#:     horror      恐怖      恐惧/诡异/压迫/不安(出现死亡 ≠ horror)
#:     emotion     情感      家庭/爱与失去/悲剧/温情/人际关系
#:     brainstorm  脑洞      非常规设定/意义翻转/荒诞/科幻想象/"原来还能这样"
#:
#: **不要增加第六类, 不要自行改名。** 新题必须由 Contract/Audit 直接
#: 按五类观察, 不能先判旧 11 类再机械映射。
V2_CATEGORIES = (
    "logic",
    "suspense",
    "horror",
    "emotion",
    "brainstorm",
)

#: 当前生成路径使用的类别 alias —— v2 激活后它就是五类。
#: ⚠️ validator **必须**按 spec.protocol_version 选枚举(见
#: `validate_protocol`), 绝不直接用这个 alias 校验历史 v1 数据。
CATEGORIES = V2_CATEGORIES

#: protocol_version -> 该版本的 canonical 类别枚举。
#:
#: 单一查表处: v1 -> 11 类(历史合同), v2 -> 5 类(当前合同),
#: unknown/legacy("") -> 空 —— 调用方应先过 `validate_protocol` 的
#: 版本门, 这里对其它值一律返回空元组(保持**纯查表、不抛**)。
CATEGORY_ENUMS = {
    PROTOCOL_V1: V1_CATEGORIES,
    PROTOCOL_V2: V2_CATEGORIES,
}


def categories_for(protocol_version: str) -> tuple:
    """某协议版本下的 canonical 类别枚举(查表, 不抛)。"""
    return CATEGORY_ENUMS.get(str(protocol_version or ""), ())

#: 一题多主题的条数合同(协议 §13): 1~3, 无重复, 含 primary。
MIN_CATEGORIES = 1
MAX_CATEGORIES = 3

# ======================================================================
# completion 条数合同 —— 按协议版本分档(Issue #48 §10)
# ======================================================================
#: legacy/current( protocol_version="" )维持既有 1~2 条语义。
#: 与 `quality.MAX_COMPLETION_FACTS` 同值 —— 那个常量保持不动,
#: 现有引用(错误文案/注释/测试)继续成立; 这里给出**命名的**分档,
#: 让"放宽只发生在 v1"这件事在代码里可见。
LEGACY_MIN_COMPLETION_FACTS = 1
LEGACY_MAX_COMPLETION_FACTS = 2

#: Protocol v1: 2~4 条。是**允许区间**, 不是"鼓励写 4 条" ——
#: 确定性校验绝不产生"最好写满 4 条"的激励(协议 §11)。
PROTOCOL_V1_MIN_COMPLETION_FACTS = 2
PROTOCOL_V1_MAX_COMPLETION_FACTS = 4

#: Protocol v1 的 core+hidden 事实条数上限(Issue #49 review Blocker 1)。
#:
#: legacy/current 沿用 quality 的 `max_core_hidden=3`。但 v1 的合同
#: 本身要求**每条** completion fact 都是 core+hidden —— 若 core 上限
#: 仍是 3, 一道合法的 4-fact v1 会陷入"既要求 4 条保持 core、又要求
#: core 总数 <=3"的**不可修状态**(validator 把它标成 can_fix, 而
#: 题池门拒绝任何带 fixable 的 spec —— 4-fact v1 永远进不了题池)。
#: 所以 v1 的上限必须**至少容下合同上限**: 4。
PROTOCOL_V1_MAX_CORE_HIDDEN_FACTS = 4


def completion_bounds(protocol_version: str) -> tuple:
    """某协议版本下 completion_fact_ids 的合法条数区间 `(lo, hi)`。

    unknown 版本返回 v1 区间没有任何意义 —— 调用方应先过
    `validate_protocol` 的版本门; 这里对 unknown 一律给 legacy 区间,
    保持函数**纯查表、不抛**。
    """
    if str(protocol_version or "") in PROTOCOL_V1_STYLE:
        return (PROTOCOL_V1_MIN_COMPLETION_FACTS,
                PROTOCOL_V1_MAX_COMPLETION_FACTS)
    return (LEGACY_MIN_COMPLETION_FACTS, LEGACY_MAX_COMPLETION_FACTS)


# ======================================================================
# v1 确定性校验
# ======================================================================
def validate_protocol(spec) -> list:
    """按 spec 自报的 protocol_version 校验**版本专属合同**。

    返回 error 字符串列表(空 = 通过)。由 `quality.validate_spec` 调用,
    也可以独立调用。共同规则**不在这里重复**。

    规则总表:

        所有版本:
            protocol_version 必须在 SUPPORTED_PROTOCOL_VERSIONS 里
            (unknown fail closed —— 绝不当 legacy)。

        legacy(""):
            协议层**零附加约束**。新字段读取时保持原样(包括"没填"),
            这就是向后兼容本身。

        haiguitang-v1 / haiguitang-v2(结构合同相同, 类别枚举不同):
            - completion_fact_ids 条数 2~4(0/1/5+ 都拒);
            - 每条 completion fact 必须有**非空 public_text**
              (canonical text 永远不当展示 fallback —— 协议 §7);
            - difficulty 必须 easy/medium/hard 之一(空也算不合法);
            - primary_category / categories: 1~3 条、无重复、全在
              **该版本自己的枚举**里、primary 在 categories 里;
              v1 = 11 类(历史合同, 冻结), v2 = 5 大类(当前合同)。
            - requested_category 非空时必须属于该版本的枚举。
              **requested != primary 是合法状态**(协议 §14):
              那是"需求没被满足", 不是"题不合法", 归 Phase D 判。

    ⚠️ 类别枚举**必须**按 spec 自报的 protocol_version 查表选择
    (`categories_for`)。历史 v1 题进入这条路径时**继续按旧 11 类
    解释** —— 绝不能因为"当前版本切到 v2"就把 v1 的语义偷偷换掉。
    """
    version = str(getattr(spec, "protocol_version", "") or "").strip()
    if version not in SUPPORTED_PROTOCOL_VERSIONS:
        return [f"unsupported protocol_version: {version!r} "
                f"(supported: {SUPPORTED_PROTOCOL_VERSIONS})"]
    if version not in PROTOCOL_V1_STYLE:
        return []

    proto_tag = version           # "haiguitang-v1" / "haiguitang-v2"
    cats_enum = categories_for(version)   # v1=11 类, v2=5 类

    errs: list = []

    # ---- completion 2~4 ----
    comp = [str(x).strip() for x in (getattr(spec, "completion_fact_ids",
                                              None) or [])
            if str(x).strip()]
    lo, hi = completion_bounds(version)
    if not (lo <= len(comp) <= hi):
        errs.append(f"protocol {proto_tag} 的 completion_fact_ids 有 "
                    f"{len(comp)} 条, 应为 {lo}~{hi} 条")

    # ---- completion fact 的 public_text ----
    # id 存在 / kind=core / visibility=hidden / 无重复 这些**共同规则**
    # 由 validate_spec 的合同块负责; 这里只加 v1/v2 独有的 public_text 门。
    # fact 本身不存在时跳过 —— 缺 id 的错误已由共同规则报, 不重复计。
    by_id = {}
    for f in (getattr(spec, "facts", None) or []):
        fid = str(getattr(f, "id", "") or "").strip()
        if fid:
            by_id[fid] = f
    for fid in comp:
        f = by_id.get(fid)
        if f is None:
            continue
        if not str(getattr(f, "public_text", "") or "").strip():
            errs.append(f"protocol {proto_tag} 的 completion fact {fid} 缺 "
                        f"public_text(canonical text 不得作为展示 fallback)")

    # ---- difficulty ----
    diff = str(getattr(spec, "difficulty", "") or "").strip()
    if diff not in DIFFICULTIES:
        errs.append(f"protocol {proto_tag} 的 difficulty 非法: {diff!r} "
                    f"(应为 {DIFFICULTIES} 之一)")

    # ---- primary / categories ----
    primary = str(getattr(spec, "primary_category", "") or "").strip()
    raw_cats = getattr(spec, "categories", None)
    raw_cats = raw_cats if isinstance(raw_cats, (list, tuple)) else []
    # ---- 原样判(Issue #49 review Blocker 2) ----
    # **不过滤任何条目**。空串 / None / 非字符串 / 不在该版本枚举里的值
    # 都是 validator 必须看见的坏数据 —— 早期版本在这里悄悄丢掉空白条目,
    # `["crime", ""]` 会被洗干净成合法的 `["crime"]`, 与 from_dict 的
    # "parser 不做 validator 的工作"是同一条被冻结的原则(协议 §13)。
    cats = [str(x).strip() for x in raw_cats]
    if not (MIN_CATEGORIES <= len(cats) <= MAX_CATEGORIES):
        errs.append(f"protocol {proto_tag} 的 categories 有 {len(cats)} 条, "
                    f"应为 {MIN_CATEGORIES}~{MAX_CATEGORIES} 条")
    bad_entries = [raw for raw, c in zip(raw_cats, cats)
                   if not isinstance(raw, str) or not c or c not in cats_enum]
    if bad_entries:
        errs.append(f"protocol {proto_tag} 的 categories 含非法值: "
                    f"{bad_entries} (canonical "
                    f"{len(cats_enum)} 类: {cats_enum}; 空串/None/非法类型"
                    f"都是坏数据, 不得被静默洗掉)")
    else:
        dupes = sorted({c for c in cats if cats.count(c) > 1})
        if dupes:
            errs.append(f"protocol {proto_tag} 的 categories 有重复: {dupes}")
        if primary not in cats_enum:
            errs.append(f"protocol {proto_tag} 的 primary_category 非法: "
                        f"{primary!r} (应为 {cats_enum} 之一)")
        elif primary not in cats:
            errs.append(f"protocol {proto_tag} 的 primary_category"
                        f"({primary!r}) 必须在 categories 里")

    # ---- requested_category(provenance, 合法可空) ----
    requested = str(getattr(spec, "requested_category", "") or "").strip()
    if requested and requested not in cats_enum:
        errs.append(f"protocol {proto_tag} 的 requested_category 非法: "
                    f"{requested!r} (应为 {cats_enum} 之一, 或空=自由生成)")

    return errs


# ======================================================================
# GenerationBrief —— 生成意图值对象(Issue #48 §25, Phase B/D 才接线)
# ======================================================================
@dataclass(frozen=True)
class GenerationBrief:
    """一次生成任务的**意图**(要什么), 与最终题的 observed 元数据分开。

        requested_category = ""   自由生成 / curated import / 无定向需求
        difficulty         = ""   不指定难度

    非空值必须来自 V2_CATEGORIES / DIFFICULTIES —— 用 `validate()`
    (确定性, 零 LLM)判。requested 是**意图不是事实**: 它绝不进
    Judge / 答题 / 通关链(协议 §14/§15), 否则"本来要求生成科幻"
    会变成判题偏见。
    """

    requested_category: str = ""
    difficulty: str = ""

    def validate(self) -> list:
        """确定性校验。返回 error 列表(空 = 合法)。

        ⚠️ 这是"**当前新生成请求**"的合同 —— 新生成只走 v2, 所以
        requested_category 只接受**当前五类**。历史 v1 的请求枚举是
        另一回事(读入与校验由 `validate_protocol` 按 spec 的
        protocol_version 分派), 两者不要混在一起。
        """
        errs: list = []
        if self.requested_category and self.requested_category not in V2_CATEGORIES:
            errs.append(f"GenerationBrief.requested_category 非法: "
                        f"{self.requested_category!r} (应为 "
                        f"{V2_CATEGORIES} 之一, 或空=自由生成)")
        if self.difficulty and self.difficulty not in DIFFICULTIES:
            errs.append(f"GenerationBrief.difficulty 非法: "
                        f"{self.difficulty!r} (应为 {DIFFICULTIES} 之一,"
                        f"或空=不指定)")
        return errs

    def require_valid(self) -> "GenerationBrief":
        """fail closed 入口: 非法 brief 直接抛 `ValueError`(Issue #51)。

        接线纪律(#50 任务书 §17 + #51 review Blocker 2): brief 已进入
        真实生成链, 非法值必须在**任何 LLM 调用之前**确定性拒绝 ——
        否则非法 requested_category 要到 Contract/audit 才撞上 validator
        (白烧几次模型调用), 而非法 requested difficulty 只影响 Truth
        创作方向与 metrics, 甚至可能一路成功入池。

        返回 self(方便链式); 调用方**不得**把 ValueError 当成可重试的
        LLM 技术失败 —— 它是代码/调用方错误。
        """
        errs = self.validate()
        if errs:
            raise ValueError("invalid GenerationBrief: " + "; ".join(errs))
        return self

    def to_dict(self) -> dict:
        return {"requested_category": self.requested_category,
                "difficulty": self.difficulty}

    @classmethod
    def from_dict(cls, d) -> "GenerationBrief":
        d = d if isinstance(d, dict) else {}
        return cls(
            requested_category=str(d.get("requested_category", "") or "").strip(),
            difficulty=str(d.get("difficulty", "") or "").strip(),
        )


# ======================================================================
# public_puzzle_meta —— 直播展示的**唯一**元数据来源(5 大类协议 v2)
# ======================================================================
#: 五大类的中文展示名 —— **单一来源**。前端绝不维护第二份
#: enum->中文映射(web/app.js 只渲染后端下发的 label)。
CATEGORY_LABELS = {
    "logic": "逻辑",
    "suspense": "悬疑",
    "horror": "恐怖",
    "emotion": "情感",
    "brainstorm": "脑洞",
}

#: 难度的中文展示名 —— 同上, 单一来源。
DIFFICULTY_LABELS = {
    "easy": "简单",
    "medium": "中等",
    "hard": "困难",
}

#: **legacy v1 -> 直播五类展示** 的确定性兼容映射。
#:
#: ⚠️ 它**只**是 presentation-safe 的兼容层, 不是把旧题重新审一遍:
#:     - 绝不写回 PuzzleSpec / 绝不改盘 / 绝不在线重分类(不做 LLM 调用);
#:     - 新 v2 题绝**不**经过这个 mapping(它自身已经是五类);
#:     - 直播 UI 最终只应出现五大类, 所以旧 v1 的 11 类在这里收敛。
LEGACY_V1_DISPLAY_CATEGORY = {
    # logic -> logic
    "logic": "logic",
    # suspense / crime -> suspense
    "suspense": "suspense",
    "crime": "suspense",
    # horror -> horror
    "horror": "horror",
    # family / tragedy / warm -> emotion
    "family": "emotion",
    "tragedy": "emotion",
    "warm": "emotion",
    # twist / brainstorm / comedy / sci_fi -> brainstorm
    "twist": "brainstorm",
    "brainstorm": "brainstorm",
    "comedy": "brainstorm",
    "sci_fi": "brainstorm",
}


def public_puzzle_meta(spec) -> dict:
    """一道题的**presentation-safe** 展示元数据(快照下发用)。

    返回::

        {
          "difficulty": "medium",
          "difficulty_label": "中等",
          "primary_category": "suspense",
          "primary_category_label": "悬疑",
          "categories": ["suspense", "brainstorm"],
          "category_labels": ["悬疑", "脑洞"],
        }

    规则(全部确定性, 零 LLM, 零 I/O):

        v2                 直接使用 spec 的五类字段(observed);
        haiguitang-v1      通过 `LEGACY_V1_DISPLAY_CATEGORY` 收敛成五类
                           (原始 spec **不被修改**, 兼容层只读);
        legacy("")/无分类  返回 **空 dict** —— 前端安静隐藏,
                           绝不显示"未知 · 未分类";
        非法/未知值         fail safe: 那一条被丢弃, **不给前端编一个
                           分类**(与 validator 的 fail closed 是两码事:
                           这里是展示层, 宁可少显示也不编数据)。

    硬边界:

        - `primary` 永远排在 `categories` 第一位;
        - 映射后去重(保持首次出现顺序);
        - `requested_category` 是生成意图, **绝不出现**在输出里 ——
          它不是 observed 分类事实, 不许下发直播前端;
        - 输出只有上面六把键, 不夹带任何 hidden truth
          (answer / facts / completion ids 全都不在这里)。
    """
    version = str(getattr(spec, "protocol_version", "") or "").strip()
    diff = str(getattr(spec, "difficulty", "") or "").strip()
    primary = str(getattr(spec, "primary_category", "") or "").strip()
    raw_cats = getattr(spec, "categories", None)
    raw_cats = list(raw_cats) if isinstance(raw_cats, (list, tuple)) else []

    def _display(cat: str) -> str:
        """该版本的 category -> 直播五类 key(非法/未知 -> "")。"""
        cat = str(cat or "").strip()
        if not cat:
            return ""
        if version == PROTOCOL_V2:
            return cat if cat in V2_CATEGORIES else ""
        if version == PROTOCOL_V1:
            return LEGACY_V1_DISPLAY_CATEGORY.get(cat, "")
        return ""                      # legacy("")/unknown -> 不展示

    # ---- categories: 按该版本映射/过滤, primary 恒排第一, 去重 ----
    cats: list = []
    for c in ([primary] if primary else []) + [str(x) for x in raw_cats]:
        d = _display(c)
        if d and d not in cats:
            cats.append(d)

    # ---- 组装(缺什么就不给什么; 全缺 -> 空 dict) ----
    out: dict = {}
    if diff in DIFFICULTY_LABELS:
        out["difficulty"] = diff
        out["difficulty_label"] = DIFFICULTY_LABELS[diff]
    if cats:
        out["primary_category"] = cats[0]
        out["primary_category_label"] = CATEGORY_LABELS[cats[0]]
        out["categories"] = cats
        out["category_labels"] = [CATEGORY_LABELS[c] for c in cats]
    return out
