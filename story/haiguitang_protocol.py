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
#: 新协议版本。语义合同见 `haiguitang/protocol/v1.md`(单一事实来源)。
HAIGUITANG_PROTOCOL_VERSION = "haiguitang-v1"

#: 全部受支持的 protocol_version。
#:
#:     ""              legacy/current production schema(质量合同由
#:                     quality_policy_version 一侧拥有, 协议层不加门)
#:     haiguitang-v1   新协议, 启用 v1 硬合同
#:
#: **任何其它非空值 fail closed**(见 `validate_protocol`)。绝不能
#: "不认识就当 legacy" —— 否则将来 haiguitang-v999 的数据流进旧程序,
#: 会被静默按 legacy 语义播出。
SUPPORTED_PROTOCOL_VERSIONS = ("", HAIGUITANG_PROTOCOL_VERSION)

# ======================================================================
# difficulty —— 固定枚举(协议 §12)
# ======================================================================
#: 难度只允许这三档。**completion 条数与难度是两条独立轴**
#: (协议 §17: 2 facts ≠ easy, 4 facts ≠ hard), 将来用真实直播数据校准。
DIFFICULTIES = ("easy", "medium", "hard")

# ======================================================================
# category —— 固定 11 类(协议 §13)
# ======================================================================
#: canonical 主题枚举, 精确 11 类。**不要改拼写, 不要新增**
#: mystery / romance / dark / emotional / thriller —— 其它自由标签
#: 继续存在于 `content_style` / `style_tags`, 但**不是**主题库存分类。
CATEGORIES = (
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


def completion_bounds(protocol_version: str) -> tuple:
    """某协议版本下 completion_fact_ids 的合法条数区间 `(lo, hi)`。

    unknown 版本返回 v1 区间没有任何意义 —— 调用方应先过
    `validate_protocol` 的版本门; 这里对 unknown 一律给 legacy 区间,
    保持函数**纯查表、不抛**。
    """
    if str(protocol_version or "") == HAIGUITANG_PROTOCOL_VERSION:
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

        haiguitang-v1:
            - completion_fact_ids 条数 2~4(0/1/5+ 都拒);
            - 每条 completion fact 必须有**非空 public_text**
              (canonical text 永远不当展示 fallback —— 协议 §7);
            - difficulty 必须 easy/medium/hard 之一(空也算不合法);
            - primary_category / categories: 1~3 条、无重复、全在
              11 类里、primary 在 categories 里;
            - requested_category 非空时必须是 11 类之一。
              **requested != primary 是合法状态**(协议 §14):
              那是"需求没被满足", 不是"题不合法", 归 Phase D 判。
    """
    version = str(getattr(spec, "protocol_version", "") or "").strip()
    if version not in SUPPORTED_PROTOCOL_VERSIONS:
        return [f"unsupported protocol_version: {version!r} "
                f"(supported: {SUPPORTED_PROTOCOL_VERSIONS})"]
    if version != HAIGUITANG_PROTOCOL_VERSION:
        return []

    errs: list = []

    # ---- completion 2~4 ----
    comp = [str(x).strip() for x in (getattr(spec, "completion_fact_ids",
                                              None) or [])
            if str(x).strip()]
    lo, hi = completion_bounds(version)
    if not (lo <= len(comp) <= hi):
        errs.append(f"protocol v1 的 completion_fact_ids 有 {len(comp)} 条, "
                    f"应为 {lo}~{hi} 条")

    # ---- completion fact 的 public_text ----
    # id 存在 / kind=core / visibility=hidden / 无重复 这些**共同规则**
    # 由 validate_spec 的合同块负责; 这里只加 v1 独有的 public_text 门。
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
            errs.append(f"protocol v1 的 completion fact {fid} 缺 "
                        f"public_text(canonical text 不得作为展示 fallback)")

    # ---- difficulty ----
    diff = str(getattr(spec, "difficulty", "") or "").strip()
    if diff not in DIFFICULTIES:
        errs.append(f"protocol v1 的 difficulty 非法: {diff!r} "
                    f"(应为 {DIFFICULTIES} 之一)")

    # ---- primary / categories ----
    primary = str(getattr(spec, "primary_category", "") or "").strip()
    raw_cats = getattr(spec, "categories", None) or []
    cats = [str(x).strip() for x in raw_cats if str(x).strip()]
    if not (MIN_CATEGORIES <= len(cats) <= MAX_CATEGORIES):
        errs.append(f"protocol v1 的 categories 有 {len(cats)} 条, "
                    f"应为 {MIN_CATEGORIES}~{MAX_CATEGORIES} 条")
    else:
        dupes = sorted({c for c in cats if cats.count(c) > 1})
        if dupes:
            errs.append(f"protocol v1 的 categories 有重复: {dupes}")
        unknown = [c for c in cats if c not in CATEGORIES]
        if unknown:
            errs.append(f"protocol v1 的 categories 含非法值: {unknown} "
                        f"(canonical 11 类: {CATEGORIES})")
        if primary not in CATEGORIES:
            errs.append(f"protocol v1 的 primary_category 非法: {primary!r} "
                        f"(应为 {CATEGORIES} 之一)")
        elif primary not in cats:
            errs.append(f"protocol v1 的 primary_category({primary!r}) "
                        f"必须在 categories 里")

    # ---- requested_category(provenance, 合法可空) ----
    requested = str(getattr(spec, "requested_category", "") or "").strip()
    if requested and requested not in CATEGORIES:
        errs.append(f"protocol v1 的 requested_category 非法: {requested!r} "
                    f"(应为 {CATEGORIES} 之一, 或空=自由生成)")

    return errs


# ======================================================================
# GenerationBrief —— 生成意图值对象(Issue #48 §25, Phase B/D 才接线)
# ======================================================================
@dataclass(frozen=True)
class GenerationBrief:
    """一次生成任务的**意图**(要什么), 与最终题的 observed 元数据分开。

        requested_category = ""   自由生成 / curated import / 无定向需求
        difficulty         = ""   不指定难度

    非空值必须来自 CATEGORIES / DIFFICULTIES —— 用 `validate()`
    (确定性, 零 LLM)判。requested 是**意图不是事实**: 它绝不进
    Judge / 答题 / 通关链(协议 §14/§15), 否则"本来要求生成科幻"
    会变成判题偏见。

    Phase A 只冻结契约与序列化; **不接进** gen_spec / keyword_seed /
    prefetch / Director(那是 Phase B/D)。
    """

    requested_category: str = ""
    difficulty: str = ""

    def validate(self) -> list:
        """确定性校验。返回 error 列表(空 = 合法)。"""
        errs: list = []
        if self.requested_category and self.requested_category not in CATEGORIES:
            errs.append(f"GenerationBrief.requested_category 非法: "
                        f"{self.requested_category!r} (应为 {CATEGORIES} 之一,"
                        f"或空=自由生成)")
        if self.difficulty and self.difficulty not in DIFFICULTIES:
            errs.append(f"GenerationBrief.difficulty 非法: "
                        f"{self.difficulty!r} (应为 {DIFFICULTIES} 之一,"
                        f"或空=不指定)")
        return errs

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
