#!/usr/bin/env python
# coding: utf-8
"""代码级质量控制 —— **完全不调用 LLM**。

方案 §6/§20 的核心主张: 不要把所有判断都押在 reviewer 身上。
每次生成之后, 先走一遍**确定性**检查(schema / 格式 / 引用完整性 / 配额),
不通过就直接毙掉重出, 连 reviewer 都不必花那一次调用。

    硬校验(validate_spec)  <- 结构性的、非黑即白的
    软审阅(reviewer)       <- 需要语义理解的
    跨题门(cross-puzzle)   <- 全局分布

这里放前两者中的"确定性"部分 + 全局配额。

设计原则(方案 §10): **配额全部代码判断, 不写进 reviewer prompt。**
reviewer 没有完整全局状态, 让它管"最近连续几题"只会得到幻觉。
"""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional

from .puzzle import (
    ATOM_ROLES, DOMAINS, EMOTION_MODES, FACT_KINDS, FACT_VISIBILITY,
    GRIEF_MODES, MECHANISM_FAMILIES, RELATIONS, SOLUTION_SHAPES, TIME_SHAPES,
    PuzzleBlueprint, PuzzleSignature, PuzzleSpec, has_closing_question,
    has_meta_text, is_first_person, quote_in_puzzle,
)

#: 每次改配额规则都要动这里, 并写进 archive —— 下一轮直播才能比较版本。
QUALITY_POLICY_VERSION = "quality-v2"

#: 默认看最近多少题
RECENT_WINDOW = 10


# ======================================================================
# 校验结果
# ======================================================================
@dataclass
class ValidationResult:
    """硬校验结果。

    分两档, 因为它们的**处置方式完全不同**:

    `errors` —— 结构性问题, 必须重出。reviewer 改不好
        (例如 atoms 引用了不存在的 fact), 交给它只会浪费一次调用,
        而且它很可能"改"出一个更不一致的版本。

    `fixable` —— **格式问题, 审稿人能就地改好**。
        第一人称叙述 -> 改成第三人称; 结尾没有问句 -> 补一个;
        谜面混进 meta 文本 -> 删掉。这三样都是"改一句话"的事,
        整题重出是浪费(实测: 一稿只差一个人称就被丢掉)。
        这些会被**转成 `must_fix` 交给审稿人**, 而不是直接毙。
    """

    ok: bool = True
    errors: list = field(default_factory=list)    # 必须重出
    fixable: list = field(default_factory=list)   # 交给审稿人改
    warnings: list = field(default_factory=list)

    def fail(self, msg: str) -> None:
        self.ok = False
        self.errors.append(msg)

    def can_fix(self, msg: str) -> None:
        """记一条"审稿人能改"的问题 —— 不算硬失败。"""
        self.fixable.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def why(self) -> str:
        return "; ".join(self.errors)

    def must_fix(self) -> str:
        """转成给审稿人的"已知问题"文本。"""
        return "; ".join(self.fixable)


# ======================================================================
# 1. spec 结构校验(方案 §21)
# ======================================================================
def validate_spec(spec: PuzzleSpec,
                  min_atoms: int = 2, max_atoms: int = 4,
                  max_core_hidden: int = 3,
                  max_hint_len: int = 30) -> ValidationResult:
    r = ValidationResult()
    if spec is None:
        r.fail("spec 为空")
        return r

    # ---- 文本 ----
    if not (spec.puzzle or "").strip():
        r.fail("谜面为空")
    if not (spec.answer or "").strip():
        r.fail("谜底为空")

    # ---- 谜面格式 ----
    # 注意: 这三样归 `can_fix` 而不是 `fail` —— 审稿人改一句话就能救,
    # 整题重出会把一道好题丢掉(实测: 只差一个人称)。
    if spec.puzzle:
        if not has_closing_question(spec.puzzle):
            r.can_fix("谜面结尾不是问句, 末尾补一句'为什么?'")
        if is_first_person(spec.puzzle):
            r.can_fix("谜面是第一人称叙事, 改成第三人称客观事实")
        if has_meta_text(spec.puzzle):
            r.can_fix("谜面混进了【谜底】/【提示】之类的元文本, 删掉它们")

    # ---- facts ----
    facts = spec.facts or []
    seen_f = set()
    for f in facts:
        if not f.id:
            r.fail("fact 缺 id")
        elif f.id in seen_f:
            r.fail(f"fact id 重复: {f.id}")
        seen_f.add(f.id)
        if not f.text:
            r.fail(f"fact {f.id} 文本为空")
        if f.kind not in FACT_KINDS:
            r.fail(f"fact {f.id} kind 非法: {f.kind}")
        if f.visibility not in FACT_VISIBILITY:
            r.fail(f"fact {f.id} visibility 非法: {f.visibility}")

    # ---- solve_atoms ----
    atoms = spec.solve_atoms or []
    if not (min_atoms <= len(atoms) <= max_atoms):
        r.fail(f"solve_atoms 数量 {len(atoms)} 不在 {min_atoms}~{max_atoms}")
    seen_a = set()
    for a in atoms:
        if not a.id:
            r.fail("atom 缺 id")
        elif a.id in seen_a:
            r.fail(f"atom id 重复: {a.id}")
        seen_a.add(a.id)
        if not a.text:
            r.fail(f"atom {a.id} 文本为空")
        if a.role not in ATOM_ROLES:
            r.fail(f"atom {a.id} role 非法: {a.role}")
        for fid in (a.fact_ids or []):
            if fid not in seen_f:
                r.fail(f"atom {a.id} 引用了不存在的 fact: {fid}")

    # ---- 必须有 required cause + required mechanism(方案 §21/§4) ----
    req = [a for a in atoms if a.required]
    if not any(a.role == "cause" for a in req):
        r.fail("缺少 required 的 cause atom")
    if not any(a.role == "mechanism" for a in req):
        r.fail("缺少 required 的 mechanism atom")

    # ---- core hidden facts 上限 ----
    n_core = len(spec.core_hidden_facts())
    if n_core > max_core_hidden:
        r.fail(f"core hidden facts 有 {n_core} 条, 超过 {max_core_hidden}")

    # ---- fair_clues: 必须真的在谜面里 ----
    clues = spec.fair_clues or []
    if not clues:
        r.fail("没有 fair_clue(谜面里必须有可回溯的线索)")
    for c in clues:
        if not c.quote:
            r.fail("fair_clue 缺 quote")
            continue
        if spec.puzzle and not quote_in_puzzle(c.quote, spec.puzzle):
            r.fail(f"fair_clue 的 quote 不在谜面里: {c.quote[:30]}")
        for aid in (c.supports_atoms or []):
            if aid not in seen_a:
                r.fail(f"fair_clue 引用了不存在的 atom: {aid}")

    # ---- 至少一条 clue 支持某个 required atom(方案 §5 最低要求) ----
    if clues and req:
        supported = {aid for c in clues for aid in (c.supports_atoms or [])}
        req_ids = {a.id for a in req}
        v2 = _is_v2_spec(spec)
        if not supported:
            # v2 的 generator/reviewer schema 都**要求** supports_atoms,
            # 所以"一条都没标"在新题上是硬失败 —— 否则 reviewer 一丢,
            # 公平性数据就退化了(方案 review P1)。
            # 老数据没这个字段, 只警告。
            (r.fail if v2 else r.warn)(
                "fair_clues 没有标注 supports_atoms(应指向 required atom)")
        elif not (supported & req_ids):
            (r.fail if v2 else r.warn)(
                "没有 fair_clue 支持任何 required atom(题目缺公平推理路径)")

    # ---- hints ----
    hints = spec.hints or []
    if len(hints) != 3:
        r.fail(f"hints 应为 3 条, 实为 {len(hints)}")
    for i, h in enumerate(hints):
        if len(h or "") > max_hint_len:
            r.fail(f"第 {i + 1} 条提示超过 {max_hint_len} 字")

    return r


# ======================================================================
# 2. blueprint 结构校验
# ======================================================================
def validate_blueprint(spec: PuzzleSpec,
                       blueprint: Optional[PuzzleBlueprint] = None
                       ) -> ValidationResult:
    """题面**实际**符合代码选定的 blueprint 吗?

    方案 §12 的硬要求: blueprint 是代码决定的硬约束, 生成器不能改。
    模型很容易无视 `death=false` 照样写死人, 所以必须回来验。

    **这里全部是 error, 不是 warning**(方案 review Blocker 7)。
    早先 mechanism/solution/relation 只 warn —— 于是代码说"这题必须
    hidden_function / commerce / neutral / stranger", 模型交回
    emotional_motive / family / grief 也照样过。那样 blueprint 就只是
    "建议", 跨题配额也就失去意义(登记的是模型自报的指纹)。

    仅对 **v2 spec** 生效: 老数据没有 signature, 不能一刀切拒掉。
    """
    r = ValidationResult()
    bp = blueprint or spec.blueprint
    if bp is None:
        r.fail("没有 blueprint")
        return r

    sig = spec.signature

    # ---- blueprint 自身的枚举合法性 ----
    for name, val, allowed in (
            ("mechanism_family", bp.mechanism_family, MECHANISM_FAMILIES),
            ("solution_shape", bp.solution_shape, SOLUTION_SHAPES),
            ("domain", bp.domain, DOMAINS),
            ("emotion_mode", bp.emotion_mode, EMOTION_MODES),
            ("relation", bp.relation, RELATIONS),
            ("time_shape", bp.time_shape, TIME_SHAPES)):
        if val not in allowed:
            r.fail(f"blueprint.{name} 非法: {val!r}")

    # ---- 老数据(没有 signature) -> 不做逐项比对 ----
    # 只有 puzzle/answer 的老 archive 不该被这套规则判死。
    if not _is_v2_spec(spec):
        if not sig.mechanism_family:
            r.warn("signature 缺 mechanism_family(老 spec, 跳过逐项比对)")
        return r

    # ---- 逐项严格比对(方案 review Blocker 7) ----
    for name in ("mechanism_family", "solution_shape", "domain",
                 "relation", "emotion_mode", "time_shape"):
        want = getattr(bp, name, "")
        got = getattr(sig, name, "")
        if got != want:
            r.fail(f"blueprint 要求 {name}={want!r}, 但题实际是 {got!r}")

    # ---- 4 个静态标记**双向**比对 ----
    # 早先只查了 False->True 一个方向, 于是 blueprint.past_trauma=True
    # 而实际 False 时不会报 —— 那种题会被登记成"有创伤", 把配额算错。
    for name in ("death", "past_trauma", "long_term_profession",
                 "repeated_ritual"):
        want = bool(getattr(bp, name, False))
        got = bool(getattr(sig, name, False))
        if got != want:
            r.fail(f"blueprint 要求 {name}={want}, 但题实际是 {got}")

    # ---- 廉价的关键词兜底(比自报更可信的高置信度信号) ----
    # 即便模型自报 death=False, 谜底里明确写了自杀/身亡就该拦下。
    if not bp.death:
        for kw in ("自杀", "死去", "去世", "身亡", "丧生", "殉"):
            if kw in (spec.answer or ""):
                r.fail(f"blueprint 要求 death=false, 但谜底出现 {kw!r}")
                break
    return r


def _is_v2_spec(spec: PuzzleSpec) -> bool:
    """是不是"新版" spec(有 signature 的)。

    老 archive 只有 puzzle/answer, 不该被 v2 的严格规则判死。
    """
    sig = spec.signature
    return bool(sig and (sig.mechanism_family or sig.solution_shape
                         or sig.domain))


# ======================================================================
# 3. 跨题配额(方案 §10)
# ======================================================================
@dataclass
class Quotas:
    """最近 RECENT_WINDOW 题内的上限。全部是代码判断。"""

    window: int = RECENT_WINDOW
    same_mechanism: int = 2
    same_solution_shape: int = 2
    death: int = 2
    past_trauma: int = 2
    trauma_ritual: int = 1
    grief: int = 2
    profession_ritual: int = 1
    same_domain: int = 3
    same_relation: int = 3

    @classmethod
    def from_config(cls, cfg: Any) -> "Quotas":
        """从 Config 取(方案 §40)。缺字段时保持默认。"""
        def g(name, dflt):
            v = getattr(cfg, name, None)
            return dflt if v is None else v
        return cls(
            window=g("quality_recent_window", RECENT_WINDOW),
            same_mechanism=g("quota_same_mechanism", 2),
            same_solution_shape=g("quota_same_solution_shape", 2),
            death=g("quota_death", 2),
            past_trauma=g("quota_past_trauma", 2),
            trauma_ritual=g("quota_trauma_ritual", 1),
        )


def _recent(recent: Optional[list], window: int) -> list:
    """取最近 window 题的 signature(老的/坏的数据要能跳过)。"""
    out = []
    for s in (recent or []):
        if isinstance(s, PuzzleSignature):
            out.append(s)
        elif isinstance(s, dict):
            out.append(PuzzleSignature.from_dict(s))
        elif hasattr(s, "signature"):
            out.append(s.signature)
    return out[-window:] if window > 0 else out


def signature_counts(recent: Optional[list],
                     window: int = RECENT_WINDOW) -> dict:
    """统计最近 window 题的各维度计数。"""
    rs = _recent(recent, window)
    c = Counter()
    for s in rs:
        c[f"mech:{s.mechanism_family}"] += 1
        c[f"shape:{s.solution_shape}"] += 1
        c[f"domain:{s.domain}"] += 1
        c[f"relation:{s.relation}"] += 1
        if s.death:
            c["death"] += 1
        if s.past_trauma:
            c["past_trauma"] += 1
        if s.trauma_ritual():
            c["trauma_ritual"] += 1
        if s.grief():
            c["grief"] += 1
        if s.long_term_profession and s.repeated_ritual:
            c["profession_ritual"] += 1
    c["__n__"] = len(rs)
    return dict(c)


def check_signature(sig: PuzzleSignature, recent: Optional[list],
                    quotas: Optional[Quotas] = None) -> list:
    """这道题的指纹**在最近 window 里是否已超配额**? 返回违规原因列表。

    空列表 = 通过。非空 = 必须重出(不是 warning, 是硬拒)。
    """
    q = quotas or Quotas()
    c = signature_counts(recent, q.window)
    bad = []

    if sig.mechanism_family and \
            c.get(f"mech:{sig.mechanism_family}", 0) >= q.same_mechanism:
        bad.append(f"最近 {q.window} 题里 {sig.mechanism_family} 已出现 "
                   f"{c[f'mech:{sig.mechanism_family}']} 次(上限 {q.same_mechanism})")
    if sig.solution_shape and \
            c.get(f"shape:{sig.solution_shape}", 0) >= q.same_solution_shape:
        bad.append(f"最近 {q.window} 题里解法形状 {sig.solution_shape} 已出现 "
                   f"{c[f'shape:{sig.solution_shape}']} 次"
                   f"(上限 {q.same_solution_shape})")
    if sig.death and c.get("death", 0) >= q.death:
        bad.append(f"最近 {q.window} 题里已有 {c['death']} 道死人题"
                   f"(上限 {q.death})")
    if sig.past_trauma and c.get("past_trauma", 0) >= q.past_trauma:
        bad.append(f"最近 {q.window} 题里已有 {c['past_trauma']} 道依赖既往创伤"
                   f"(上限 {q.past_trauma})")
    if sig.trauma_ritual() and c.get("trauma_ritual", 0) >= q.trauma_ritual:
        bad.append(f"最近 {q.window} 题里已有一道'创伤+长年怪规矩'"
                   f"(上限 {q.trauma_ritual})")
    if sig.grief() and c.get("grief", 0) >= q.grief:
        bad.append(f"最近 {q.window} 题里已有 {c['grief']} 道悲情题"
                   f"(上限 {q.grief})")
    if sig.long_term_profession and sig.repeated_ritual and \
            c.get("profession_ritual", 0) >= q.profession_ritual:
        bad.append(f"最近 {q.window} 题里已有一道'多年职业+怪规矩'"
                   f"(上限 {q.profession_ritual})")
    if sig.domain and c.get(f"domain:{sig.domain}", 0) >= q.same_domain:
        bad.append(f"最近 {q.window} 题里领域 {sig.domain} 已出现 "
                   f"{c[f'domain:{sig.domain}']} 次(上限 {q.same_domain})")
    if sig.relation and c.get(f"relation:{sig.relation}", 0) >= q.same_relation:
        bad.append(f"最近 {q.window} 题里关系 {sig.relation} 已出现 "
                   f"{c[f'relation:{sig.relation}']} 次(上限 {q.same_relation})")
    return bad


# ======================================================================
# 4. 结构去重(方案 §10: **不是**"职业不同 = 题目不同")
# ======================================================================
def is_structurally_duplicate(sig: PuzzleSignature, recent: Optional[list],
                              window: int = RECENT_WINDOW) -> str:
    """和最近某题**结构上等价**吗? 返回冲突的那个坐标, 否则 ""。

    与 `_too_similar`(3-gram 文本相似)互补: 那个抓"换个说法重讲同一题",
    这个抓"换了个职业但诡计和形状完全一样"。

    判据是 (mechanism_family, solution_shape) 二元组 —— 刻意**不包含**
    domain/职业。方案 §65 明确: 不要再用"职业不同 = 题目不同"。
    """
    for s in reversed(_recent(recent, window)):
        if (s.mechanism_family and s.mechanism_family == sig.mechanism_family
                and s.solution_shape and s.solution_shape == sig.solution_shape):
            return f"{sig.mechanism_family}/{sig.solution_shape}"
    return ""


# ======================================================================
# 5. Blueprint 选择(weighted-LRU, 方案 §11)
# ======================================================================
#: (mechanism_family, solution_shape) 的合理搭配模板。
#: 不是全排列 —— 有些组合没意义(比如"规则约束"+ "身份倒置")。
#:
#: 注意: 这里**只有 MECHANISM_FAMILIES 里的 key**。曾经的 "trauma_ritual"
#: 不是合法 family, 从它选出来的 blueprint 会被 validate_blueprint 直接毙掉
#: —— 那条路是死的。"创伤 + 长年怪规矩"是一种 **shape+flag 组合**
#: (`past_trauma_explains_current_ritual`), 由 QUOTA_SHAPE_FLAGS 表达。
FAMILY_SHAPES = {
    "information_gap": ("information_advantage", "identity_reversal"),
    "hidden_function": ("hidden_function_explains_behavior",
                        "misunderstood_object"),
    "identity_misread": ("identity_reversal", "information_advantage"),
    "rule_constraint": ("rule_constraint", "social_constraint"),
    "causal_reversal": ("causal_reversal", "goal_reversal"),
    "time_reinterpretation": ("time_reinterpretation",
                              "past_trauma_explains_current_ritual"),
    "space_reinterpretation": ("space_reinterpretation", "misunderstood_object"),
    "goal_reversal": ("goal_reversal", "causal_reversal"),
    "observer_misread": ("identity_reversal", "information_advantage"),
    "object_misuse": ("misunderstood_object", "hidden_function_explains_behavior"),
    "social_rule": ("social_constraint", "rule_constraint"),
    "emotional_motive": ("information_advantage", "past_trauma_explains_current_ritual"),
}

#: 某些解法形状**天然带**的标记。这是蓝图层唯一的"形状 -> 标记"推导表。
#: `past_trauma_explains_current_ritual` 一出, 必然 past_trauma=True;
#: 而"长年怪规矩"正是 ritual, 所以 repeated_ritual 也跟着为真。
#: 它单独被 `quota_trauma_ritual`(=1) 严格限额 —— 8h 直播里坍缩最重的就是它。
SHAPE_FLAGS = {
    "past_trauma_explains_current_ritual": {
        "past_trauma": True, "repeated_ritual": True,
        "time_shape": "years_long", "emotion_mode": "grief"},
}


def _shape_flags(shape: str, emotion_mode: str) -> dict:
    """按解法形状推导 blueprint 的静态标记与时间/情绪形态。"""
    f = dict(SHAPE_FLAGS.get(shape, {}))
    f.setdefault("time_shape", "instant")
    f.setdefault("emotion_mode", emotion_mode)
    return f


def _candidates(quotas: Quotas, recent: Optional[list]) -> list:
    """列出当前**未超配额**的 blueprint 候选。

    做法: 遍历 (family, shape) 搭配, 逐条过 `check_signature`。
    死亡/创伤/长年规矩这些标记按 SHAPE_FLAGS 从解法形状推导
    (past_trauma_explains_current_ritual 必然带 trauma+ritual),
    所以 trauma_ritual 的严格配额在这里自然生效。
    """
    c = signature_counts(recent, quotas.window)
    out = []
    for fam, shapes in FAMILY_SHAPES.items():
        for shape in shapes:
            flags = _shape_flags(shape, "neutral")
            for dom in DOMAINS:
                if c.get(f"domain:{dom}", 0) >= quotas.same_domain:
                    continue
                for rel in RELATIONS:
                    if c.get(f"relation:{rel}", 0) >= quotas.same_relation:
                        continue
                    bp = PuzzleBlueprint(
                        mechanism_family=fam, solution_shape=shape,
                        domain=dom, relation=rel,
                        emotion_mode=flags["emotion_mode"],
                        time_shape=flags["time_shape"],
                        death=flags.get("death", False),
                        past_trauma=flags.get("past_trauma", False),
                        long_term_profession=flags.get("long_term_profession", False),
                        repeated_ritual=flags.get("repeated_ritual", False))
                    sig = signature_of(bp)
                    if not check_signature(sig, recent, quotas):
                        out.append((bp, sig))
    return out


def signature_of(bp: PuzzleBlueprint) -> PuzzleSignature:
    """blueprint -> signature(生成前的预期指纹)。"""
    return PuzzleSignature(
        mechanism_family=bp.mechanism_family,
        solution_shape=bp.solution_shape,
        domain=bp.domain, emotion_mode=bp.emotion_mode,
        relation=bp.relation, time_shape=bp.time_shape,
        death=bp.death, past_trauma=bp.past_trauma,
        long_term_profession=bp.long_term_profession,
        repeated_ritual=bp.repeated_ritual)


def choose_blueprint(recent: Optional[list],
                     rng: Optional[random.Random] = None,
                     quotas: Optional[Quotas] = None) -> PuzzleBlueprint:
    """选一个**未超配额**的 blueprint, 偏向"越久没出现"的 mechanism_family。

    不是全排列随机(方案 §11)。权重用"越久没出现权重越高":
        w(family) = 1 + 最近没出现的题数 / window
    这样长期没用的 family 会被优先捞回来, 而完全随机会让它继续饿着。

    候选为空时(配额把所有组合都堵死了)退化为"最久没出现的 family",
    保证**永远有返回值** —— 出题链不能因为配额算法而卡死。
    """
    rng = rng or random.Random()
    q = quotas or Quotas()
    cands = _candidates(q, recent)
    if not cands:
        return _least_recently_seen(q, recent)

    # ---- 权重: family 越久没出现, 权重越高 ----
    rs = _recent(recent, q.window)
    last_seen = {}
    for i, s in enumerate(rs):       # i=0 是最老的一条
        if s.mechanism_family:
            last_seen[s.mechanism_family] = i
    n = max(1, len(rs))
    weights = []
    for bp, _sig in cands:
        age = last_seen.get(bp.mechanism_family, -1)
        # age = -1 表示最近 window 里从没出现 -> 最优先
        weights.append(1.0 + (n - age) / n if age >= 0 else 2.0 + 1.0 / n)

    total = sum(weights)
    if total <= 0:
        return cands[rng.randrange(len(cands))][0]
    pick = rng.random() * total
    acc = 0.0
    for (bp, _sig), w in zip(cands, weights):
        acc += w
        if pick <= acc:
            return bp
    return cands[-1][0]


def _least_recently_seen(quotas: Quotas,
                         recent: Optional[list]) -> PuzzleBlueprint:
    """配额堵死所有组合时的兜底: 取最久没出现的 family。"""
    rs = _recent(recent, quotas.window)
    used = [s.mechanism_family for s in rs if s.mechanism_family]
    for fam in MECHANISM_FAMILIES:
        if fam not in used:
            shapes = FAMILY_SHAPES.get(fam, ("information_advantage",))
            return PuzzleBlueprint(mechanism_family=fam,
                                   solution_shape=shapes[0])
    # 全都出现过 -> 用出现次数最少的那个
    c = Counter(used)
    fam = min(MECHANISM_FAMILIES, key=lambda f: c.get(f, 0))
    shapes = FAMILY_SHAPES.get(fam, ("information_advantage",))
    return PuzzleBlueprint(mechanism_family=fam, solution_shape=shapes[0])


# ======================================================================
# 6. 跨题门(生成后调用)
# ======================================================================
def cross_puzzle_gate(spec: PuzzleSpec, recent: Optional[list],
                      quotas: Optional[Quotas] = None,
                      blueprint: Optional[PuzzleBlueprint] = None) -> list:
    """一道题在**全局分布**上过关吗? 返回违规原因(空 = 通过)。

    放在 reviewer **之后**再走一遍(方案 §20)。reviewer 只管单题质量,
    全局分布是代码的事。
    """
    q = quotas or Quotas()
    bad = []
    sig = spec.signature
    if not sig.mechanism_family and not sig.solution_shape:
        # 生成器没回传 signature -> 用 blueprint 的预期值代替,
        # 至少还能挡住"同一 blueprint 连续出"。
        bp = blueprint or spec.blueprint
        sig = signature_of(bp)
    bad.extend(check_signature(sig, recent, q))
    dup = is_structurally_duplicate(sig, recent, q.window)
    if dup:
        bad.append(f"与最近某题结构等价: {dup}")
    return bad


def policy_version() -> str:
    return QUALITY_POLICY_VERSION


# ======================================================================
# 7. 自检: 模板表本身不能有死路
# ======================================================================
def check_tables() -> list:
    """`FAMILY_SHAPES` 里不能有非法 family/shape —— 返回问题列表。

    为什么要在**导入时**就查: 这类错**不会报错**, 只会让那部分候选
    永远选不出来。实测写错过两次:
      - `"trauma_ritual"` 不是 mechanism_family;
      - `"observer_misread"` 是 family 不是 solution_shape。
    两次都是"调度器看起来均匀覆盖了所有 family", 其实有一整块是死的。
    """
    bad = []
    for fam, shapes in FAMILY_SHAPES.items():
        if fam not in MECHANISM_FAMILIES:
            bad.append(f"FAMILY_SHAPES 的 key {fam!r} 不是合法 mechanism_family")
        for sh in shapes:
            if sh not in SOLUTION_SHAPES:
                bad.append(f"FAMILY_SHAPES[{fam!r}] 的 shape {sh!r} 不是合法 "
                           f"solution_shape")
    # 每个 family 都要有出路, 否则它在调度里是死的
    for fam in MECHANISM_FAMILIES:
        if fam not in FAMILY_SHAPES:
            bad.append(f"mechanism_family {fam!r} 没有任何 shape 搭配(死路)")
    return bad
