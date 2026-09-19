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
    GRIEF_MODES, MECHANISM_FAMILIES, RELATIONS, REVEAL_MODES, SOLUTION_SHAPES,
    TIME_SHAPES,
    PuzzleBlueprint, PuzzleSignature, PuzzleSpec, has_closing_question,
    has_meta_text, is_first_person, quote_in_puzzle,
)

#: 每次改配额规则都要动这里, 并写进 archive —— 下一轮直播才能比较版本。
#:
#: v4(Step 04):
#:   - Prompt 升到 riddle-v4 / check-v4, Reviewer 增加五项单题语义检查:
#:     时间线一致 / 身份一致 / 动作连续 / 线索可回溯 / 隐藏规则依赖;
#:   - `reveal_mode` 与 `procedural_rule_dependency` 作为 **observed**
#:     signature 字段由 Reviewer 如实回传(见 `puzzle.PuzzleSignature`);
#:   - 冻结职责边界: Reviewer 只判单题, **不**读最近窗口配额; 全局配额
#:     归代码层(`signature_counts` / `check_signature` / `cross_puzzle_gate`)。
#: 所以 v3 与 v4 的 archive **不可直接比较**: v4 的 signature 多了两维观
#: 察值, 且 v4 的题是在"审查者会查这五项一致性"的前提下产出的。
#:
#: v3(第二轮 review):
#:   - `time_shape` 退出 blueprint 硬比对, 降为 observed metadata
#:     (v2 里它只有一个默认值 instant, 把大量合理题判死);
#:   - 审稿改稿必须整套同步 facts/atoms/clues/signature, 缺一即整稿拒;
#:   - pass 也要吸收审稿人的 observed_signature;
#:   - 兜底题结构化为 PuzzleSpec。
#: 所以 v2 与 v3 的 archive **不可直接比较**: v2 的 signature.time_shape
#: 是"被强制成 instant", v3 的是"如实观察"。
#:
#: v5(Solve UX):
#:   - **通关合同与文学谜底分开**: `PuzzleSpec.core_answer` +
#:     `completion_fact_ids`(1~2 条, 只允许 core/hidden);
#:   - `solve_atoms` 降级为提示/解释/复盘结构 —— 不再要求
#:     "恰好一条 cause + 一条 mechanism", 改为"至少一条 required atom",
#:     并允许 `role=key`(身份/时间/目标翻转类题的原子事实);
#:   - 通关改由**代码集合覆盖**判定(房间累计已确认事实), 不再走
#:     Final Judge 的 cause_hit + mechanism_hit gate;
#:   - Reviewer 必须回传四项 `quality_checks`, 任一为 false 则整稿拒收。
#:
#: 所以 v4 与 v5 的题**不可直接比较**: v4 的题是在"必须硬造 cause+
#: mechanism"的前提下产出的, 它的 atoms/signature 描述的不是这道题
#: 真实的解法形状。
#: v6(Solve UX 二): **completion fact 必须比 core_answer 更"核心"**
#:   - v5 只要求"1~2 条 core/hidden", 于是生成器很自然地写出一条
#:     **比 core_answer 更细**的合同 —— 例如 core_answer 说"制造虚高成交
#:     记录抬高同类箱子价值", 而 completion 里塞进"鉴定人具有定价权"。
#:     那种细节属于**解释骗局如何运转**的 support, 不是普通观众解出
#:     谜面所必须说出的东西。后果在真实直播里已经出现: 房间明显已经
#:     说出核心机制, 合同却永远覆盖不满, 于是一串"是"之后无语揭晓。
#:   - v6 起 `completion_contract_minimal` 增加"不得严于 core_answer":
#:     做删除测试 —— 删掉某个身份/权限/制度/职业/流程细节后, 观众仍然
#:     能回答谜面最后的问题, 那个细节就不属于 completion。
#:   - `Answer` 侧同时新增 completion verifier(见 `story/llm.py`), 但那
#:     只补 `established_fact_ids`, **不产生第二个胜利入口**。
#:
#: 为什么必须 bump 政策版本而不是兼容 v5: 盘上已经存在按 quality-v5
#: Reviewer 生成的题, 它们正是这次真实故障的来源。不 bump 的话修完
#: prompt 旧题仍然能进直播 —— 所以 v5 一律 quarantine, 不迁移、不猜。
QUALITY_POLICY_VERSION = "quality-v6"

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
#: v5 通关合同里 `core_answer` 的硬上限(汉字数)。推荐 <=60, 上限 80。
#: 为什么要有硬上限: "一句话核心答案"如果写成两三百字, 观众听不出重点,
#: 而揭晓是**确定性**地念它(不再经 LLM 加工) —— 长答案会直接拖垮体验。
CORE_ANSWER_MAX_LEN = 80
#: 通关合同的条数上限。**刻意只有 2** —— 见下面校验里的说明。
MAX_COMPLETION_FACTS = 2


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
    #
    # v5 有通关合同时下限**降到 1**。理由: 身份/时间/目标翻转题的核心
    # 原子事实天然只有一条("门外女人是父亲的亲生女儿"), 硬要求 2 条会
    # 逼生成器再凑一条 —— 那正是本批要消除的"为满足校验而造 atom"。
    # 建议下限保持 2 条, 但这**只作用于无合同的 legacy 题**(老 fixture
    # 与老 archive 不该被这条新规则影响)。
    atoms = spec.solve_atoms or []
    lo = 1 if spec.completion_fact_ids else min_atoms
    if not (lo <= len(atoms) <= max_atoms):
        r.fail(f"solve_atoms 数量 {len(atoms)} 不在 {lo}~{max_atoms}")
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

    # ---- quality-v5 版本硬门 ----
    #
    # ⚠️ **不能**用 `bool(spec.completion_fact_ids)` 当"这是 v5 还是
    # legacy"的唯一判据。后果是:
    #
    #     quality-v5 + completion=[]  ->  悄悄降级成 legacy 通关语义
    #
    # 也就是说, 一道**自称 v5** 的题可以带着旧的 cause+mechanism
    # victory semantics 进 live 库存。v5 标签绝不能与 legacy 通关语义
    # 共存 —— 所以这里按**政策版本**判, 而不是按"有没有填合同"判。
    #
    #   quality-v5            -> 必须有完整 v5 合同(core_answer + 1~2 条)
    #   quality-v4 / 空 / 其它 -> legacy, 合同可以为空(老 archive 不能判死)
    is_v5 = str(spec.quality_policy_version or "") == QUALITY_POLICY_VERSION
    if is_v5:
        if not spec.completion_fact_ids:
            r.fail("当前政策(quality-v6) spec 缺 completion_fact_ids"
                   "(v5 标签不能配 legacy 通关语义)")
        if not (spec.core_answer or "").strip():
            r.fail("当前政策(quality-v6) spec 缺 core_answer")

    # ---- 通关合同(v5) 与 atom 要求 ----
    #
    # 这里把"通关需要什么"与"atoms 长什么样"彻底解耦。历史包袱:
    # v5 之前这一段的判据是"必须恰好一条 required cause + 一条 required
    # mechanism", 于是**每道题都被强行写成因果机制题** —— 身份题只能
    # 硬造一个 cause/mechanism 去满足校验, 而 Final Judge 又拿这套假
    # atoms 当通关闸门。这正是"AI 太保守"的根因之一。
    #
    # 新语义:
    #   completion_fact_ids = 胜利合同(代码做集合覆盖判定)
    #   solve_atoms         = 提示 / 解释 / 复盘结构
    #
    # ⚠️ **向后兼容**: 没有合同的 spec(老 archive / 老 fixture /
    # `from_legacy_riddle_result`)继续走旧 gate。绝不能把老数据一刀切
    # 判死 —— 盘上几百道 v4 题与全部现有测试 fixture 都会炸。
    req = [a for a in atoms if a.required]
    has_contract = bool(spec.completion_fact_ids)
    if has_contract:
        # 合同要求"至少一条 required atom 顶着", 但**不再指定角色**:
        # 身份/时间/目标翻转类题用 key, 只有真有因果链的题才用
        # cause/mechanism。
        if not req:
            r.fail("没有 required 的 solve_atom(至少要有一条)")
    else:
        if not any(a.role == "cause" for a in req):
            r.fail("缺少 required 的 cause atom")
        if not any(a.role == "mechanism" for a in req):
            r.fail("缺少 required 的 mechanism atom")

    # ---- v5 通关合同硬校验 ----
    if has_contract:
        comp = list(spec.completion_fact_ids)
        # (a) core_answer: 必须能"一句话说清", 所以非空 + 限长 + 单行
        ca = (spec.core_answer or "").strip()
        if not ca:
            r.fail("有通关合同但 core_answer 为空(谜底必须能一句话说清)")
        else:
            if len(ca) > CORE_ANSWER_MAX_LEN:
                r.fail(f"core_answer 有 {len(ca)} 字, 超过 "
                       f"{CORE_ANSWER_MAX_LEN} 字上限(要一句话, 不要一段话)")
            if "\n" in spec.core_answer or "\r" in spec.core_answer:
                r.fail("core_answer 不能换行(揭晓时会原样念给观众)")
        # (b) 条数 1~2: **不放宽成 4、5 条**。压不进 2 条说明题太绕,
        #     正确处置是 rewrite, 不是把门槛降低。
        if not (1 <= len(comp) <= MAX_COMPLETION_FACTS):
            r.fail(f"completion_fact_ids 有 {len(comp)} 条, 应为 1~"
                   f"{MAX_COMPLETION_FACTS} 条(超过说明这题太绕, 应重出)")
        seen_c: set = set()
        for fid in comp:
            if fid in seen_c:
                r.fail(f"completion_fact_ids 有重复 id: {fid}")
            seen_c.add(fid)
            if fid not in seen_f:
                r.fail(f"completion_fact_ids 引用了不存在的 fact: {fid}")
                continue
            f = next((x for x in facts if x.id == fid), None)
            if f is None:
                continue
            # (c) 只允许 core + hidden。support/exclusion 是背景与排除项,
            #     永远不得作为通关要求 —— 否则"说出任意一条背景"就能赢。
            if f.kind != "core":
                r.fail(f"completion fact {fid} 的 kind={f.kind!r}, "
                       f"只有 core 可以作为通关要求")
            if f.visibility != "hidden":
                r.fail(f"completion fact {fid} 的 visibility="
                       f"{f.visibility!r}, 只有 hidden 可以作为通关要求"
                       f"(public 的谜面已经告诉了观众)")
        # (d) 公平性: 每条 completion fact 必须被某条 atom 引用 ——
        #     否则它就是一个"完全没有题面抓手"的通关条件, 观众无从推。
        if comp and atoms:
            atom_facts = {fid for a in atoms for fid in (a.fact_ids or [])}
            orphan = [fid for fid in comp if fid not in atom_facts]
            if orphan:
                r.fail("completion fact 没有任何 solve_atom 引用它"
                       "(观众没有推理抓手): " + ", ".join(orphan))

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

    # ---- v5: 线索必须真的通向通关路径(Blocker 4) ----
    #
    # 上一步只查了两件**分开**的事:
    #     completion fact 被某个 atom 引用
    #     fair_clue 支持某个 required atom
    # 这不够 —— 两者可以落在**不同的分支**上: 通关事实走 a1, 而唯一的
    # clue 指向一条与通关无关的 a2。结果是一道"有线索、也有抓手, 但
    # 线索指不到通关条件"的题, 观众永远推不出来。
    #
    # 要求: **至少一条 clue 指向某个引用 completion fact 的 atom**。
    # 刻意**不**要求每个 completion fact 都有自己的 clue —— 那会重新
    # 变得过度保守(合同本来就是"最少要确认什么", 不必每条都从题面
    # 直接可推)。
    #
    # ⚠️ 位置: 必须在 `clues` 绑定**之后**。早先它被放在合同校验块里
    # (那一段在 fair_clues 之前), 于是任何有合同的 spec 走到这里都会
    # UnboundLocalError —— 一个只在"校验真的跑到底"时才暴露的错。
    if has_contract and atoms and clues:
        completion_ids = set(spec.completion_fact_ids)
        completion_atom_ids = {
            a.id for a in atoms
            if completion_ids.intersection(a.fact_ids or [])}
        clued_atom_ids = {
            aid for c in clues for aid in (c.supports_atoms or [])}
        if completion_atom_ids and clued_atom_ids:
            if not (completion_atom_ids & clued_atom_ids):
                r.fail("没有任何 fair_clue 指向通关事实的推理路径"
                       "(线索指不到 completion, 题目不公平)"
                       f" [completion atoms={sorted(completion_atom_ids)}"
                       f", clued={sorted(clued_atom_ids)}]")

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
            ("time_shape", bp.time_shape, TIME_SHAPES),
            # reveal_mode 是 Step 02 的调度**目标**, 只验它在枚举内。
            # 它**不参与下面的逐项比对** —— 见那里的说明。
            ("reveal_mode", bp.reveal_mode, REVEAL_MODES)):
        if val not in allowed:
            r.fail(f"blueprint.{name} 非法: {val!r}")

    # ---- 老数据(没有 signature) -> 不做逐项比对 ----
    # 只有 puzzle/answer 的老 archive 不该被这套规则判死。
    if not _is_v2_spec(spec):
        if not sig.mechanism_family:
            r.warn("signature 缺 mechanism_family(老 spec, 跳过逐项比对)")
        return r

    # ---- which 维度逐项严格比对(方案 review Blocker 7) ----
    #
    # `time_shape` **不在**这个名单里(第二轮 review P1)。
    #
    # 原因: blueprint 里 time_shape 只有一个默认值 instant(SHAPE_FLAGS 只给
    # past_trauma_explains_current_ritual 派了 years_long), 而 instant 在
    # 严格比对下会与大量完全合理的题冲突 —— "每天做某事 / 连续几天 /
    # 长年观察 / 固定规矩"全都被判死。实测 3 个 seed 里就有 1 个因为
    # "长年习惯 vs time_shape=instant" 而多花一轮重出。
    #
    # 于是这个字段从"增加多样性"变成了"提高废稿率"。降为 **observed
    # metadata**: 生成器如实回传, 代码只统计(见 signature_counts), 不拒稿。
    # 等将来真要做 time_shape 维度的配额, 得先有 family × time_shape 的
    # 兼容表, 而不是拿一个默认值去卡所有题。
    #
    # `reveal_mode` 同理**不在这里**(Step 02)。它是一个**目标**: 调度器
    # 挑一个稀缺的结构让模型朝它写, 而 `signature.reveal_mode` 是
    # Reviewer 读完**如实**回传的实际结构。两者不一致时正确处置是
    # **记录下来**(见 Step 04 的 reveal_mode adherence), 不是拒稿 ——
    # 否则模型只要照抄目标值就能通过, 那个观察值立刻失去意义, 配额也就
    # 统计不到真实分布了。
    for name in ("mechanism_family", "solution_shape", "domain",
                 "relation", "emotion_mode"):
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
    # ---- Step 02 ----
    #: 同一 `reveal_mode`(observed)最多几道。
    same_reveal_mode: int = 2
    #: `straight_explanation`(没有翻转的正面解释)上限。
    #: 单独一条, 因为它是"不翻转"这个**默认态**的护栏: 即使每个具体
    #: reveal_mode 都没超 `same_reveal_mode`, 也可能连着好几道都不翻转。
    straight_explanation: int = 2
    #: `neutral` 情绪上限。Step 02 去掉了"永远选 neutral"的固定偏置,
    #: 这条是防止它从另一个方向坍缩(比如全变 warm)。
    neutral_emotion: int = 3
    #: 主要靠制度性设定成立的题上限(`procedural_rule_dependency`, observed)。
    procedural_rule: int = 2

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
            same_reveal_mode=g("quota_same_reveal_mode", 2),
            straight_explanation=g("quota_straight_explanation", 2),
            neutral_emotion=g("quota_neutral_emotion", 3),
            procedural_rule=g("quota_procedural_rule", 2),
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
    """统计最近 window 题的各维度计数。

    ⚠️ 这两个新维度(`reveal_mode` / `procedural_rule_dependency`)统计的
    是 **observed** 值 —— 即 Reviewer 读完如实回传、落进 `spec.signature`
    的那个值。**不是**调度器的目标值。理由: 配额要反映观众真实看到的
    分布; 拿目标值统计的话, 模型不服从时配额会静默跑偏, 且调度意图
    无法审计。

    老题 / 缺失值: `reveal_mode == ""` 表示"没观察过"。它**不进**任何
    具体模式的桶(不会被当成 straight_explanation), 只在
    `__unknown_reveal__` 里留个计数, 便于排查"这批题有多少没观察值"。
    `procedural_rule_dependency` 是 bool, 缺失即 False(它不是"未知",
    而是"默认不依赖" —— 与 Signature 里其余 bool 字段一致)。
    """
    rs = _recent(recent, window)
    c = Counter()
    for s in rs:
        c[f"mech:{s.mechanism_family}"] += 1
        c[f"shape:{s.solution_shape}"] += 1
        c[f"domain:{s.domain}"] += 1
        c[f"relation:{s.relation}"] += 1
        if s.time_shape:
            c[f"time:{s.time_shape}"] += 1    # 只统计, 不参与配额
        if s.emotion_mode:
            c[f"emotion:{s.emotion_mode}"] += 1
        # ---- Step 02: reveal 结构(observed) ----
        if s.reveal_mode:
            c[f"reveal:{s.reveal_mode}"] += 1
        else:
            c["__unknown_reveal__"] += 1
        if s.procedural_rule_dependency:
            c["procedural_rule"] += 1
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
    # ---- Step 02: reveal 结构 / 情绪 / 规则依赖 ----
    # 没有 observed reveal_mode(`""`)时**不判** —— 那是"没观察过", 不是
    # "普通解释"。拿未知当 straight 会把老题和历史统计一起弄脏。
    if sig.reveal_mode:
        # **任何** reveal_mode 都受 same_reveal_mode 约束; straight 额外还
        # 受 straight_explanation 约束 —— 实际上限是两者**较严**的那个。
        # 早先写成 if/else 分支(straight 只查 straight 配额), 于是配置成
        # `same_reveal_mode=1 / straight_explanation=2` 时 straight 仍能出现
        # 两次, 违反"同一结构上限 1"的语义。
        if c.get(f"reveal:{sig.reveal_mode}", 0) >= q.same_reveal_mode:
            bad.append(f"最近 {q.window} 题里揭晓结构 {sig.reveal_mode} 已出现 "
                       f"{c[f'reveal:{sig.reveal_mode}']} 次"
                       f"(上限 {q.same_reveal_mode})")
        if (sig.reveal_mode == "straight_explanation"
                and c.get("reveal:straight_explanation", 0)
                >= q.straight_explanation):
            bad.append(f"最近 {q.window} 题里有 "
                       f"{c['reveal:straight_explanation']} 道'没有翻转的正面解释'"
                       f"(上限 {q.straight_explanation})")
    if (sig.emotion_mode == "neutral"
            and c.get("emotion:neutral", 0) >= q.neutral_emotion):
        bad.append(f"最近 {q.window} 题里有 {c['emotion:neutral']} 道中性情绪题"
                   f"(上限 {q.neutral_emotion})")
    if (sig.procedural_rule_dependency
            and c.get("procedural_rule", 0) >= q.procedural_rule):
        bad.append(f"最近 {q.window} 题里有 {c['procedural_rule']} 道主要靠"
                   f"制度性设定成立(上限 {q.procedural_rule})")
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
    """按解法形状推导 blueprint 的静态标记与时间/情绪形态。

    `time_shape` 在这里**只是给生成器的一个提示**, 不再被 validate_blueprint
    硬比对(第二轮 review P1)。所以即使生成器写出"每天/长年"的题也不会被拒。
    """
    f = dict(SHAPE_FLAGS.get(shape, {}))
    f.setdefault("time_shape", "instant")
    f.setdefault("emotion_mode", emotion_mode)
    return f


def shape_is_compatible_with_emotion(shape: str, emotion: str) -> bool:
    """这个 solution_shape 能不能配这个 emotion?

    `SHAPE_FLAGS` 里被**形状钉死**的情绪是硬约束(`past_trauma_...` 必然
    grief)。没有钉死的 shape 可以自由搭任何情绪。
    """
    pinned = SHAPE_FLAGS.get(shape, {}).get("emotion_mode")
    return pinned is None or pinned == emotion


def _legal_shapes_for(fam: str, emotion: str) -> list:
    """某个 family 下, 与选定 emotion **相容**的 shape。"""
    return [sh for sh in FAMILY_SHAPES.get(fam, ())
            if shape_is_compatible_with_emotion(sh, emotion)]


def family_headroom(quotas: Quotas, recent: Optional[list]) -> dict:
    """每个 family 还剩多少"配额余量" —— **在 family 层算一次**。

    ## 为什么必须有这一步(而不是按展开后的 candidate 计权)

    早先的实现把 `family × shape × emotion × domain × relation` **全展开**
    成候选, 再给每个 candidate 乘所属 family 的 LRU 权重。那有个隐蔽的
    后果:

        普通 shape     -> 能搭全部 EMOTION_MODES  -> 展开项多
        past_trauma    -> emotion 被钉死成 grief   -> 展开项少

    即使两个 family 的 LRU 权重完全相同, 展开项多的那个也天然拿到更多
    "彩票" —— 于是**后面的维度反过来污染了前面的选择概率**。这正是
    hierarchical scheduler 要避免的。

    所以权重必须在 **family 层**算一次, 与该 family 能展开出多少
    emotion/domain/relation 无关。

    返回 `{family: 权重}`, 权重构成:
        - 该 family 在最近窗口内出现的次数越少, 权重越高(weighted-LRU);
        - 没有任何合法 shape 的 family(比如选定 emotion 下全被钉死)
          权重为 0 —— 它这一轮不可选。
    """
    rs = _recent(recent, quotas.window)
    n = max(1, len(rs))
    last_seen: dict = {}
    for i, s in enumerate(rs):           # i=0 是最老的一条
        if s.mechanism_family:
            last_seen[s.mechanism_family] = i
    counts = Counter(s.mechanism_family for s in rs if s.mechanism_family)

    out = {}
    for fam in FAMILY_SHAPES:
        age = last_seen.get(fam, -1)
        # age = -1: 最近窗口里从没出现 -> 最优先
        base = 2.0 + 1.0 / n if age < 0 else 1.0 + (n - age) / n
        # 出现次数越少越好(与 age 正交: age 看"多久没见", count 看"见了几次")
        out[fam] = base / (1.0 + counts.get(fam, 0))
    return out


def _quota_allows(fam: str, shape: str, emotion: str,
                  quotas: Quotas, recent: Optional[list]) -> bool:
    """这个 (family, shape, emotion) 组合过不过**静态**配额?

    只看与 domain/relation 无关的那几条(mechanism / shape / 情绪 /
    trauma 等)。domain/relation 在最后一层单独查。
    """
    bp = PuzzleBlueprint(mechanism_family=fam, solution_shape=shape,
                         domain="", relation="", emotion_mode=emotion)
    sig = signature_of(bp)
    c = signature_counts(recent, quotas.window)
    if sig.mechanism_family and \
            c.get(f"mech:{sig.mechanism_family}", 0) >= quotas.same_mechanism:
        return False
    if sig.solution_shape and \
            c.get(f"shape:{sig.solution_shape}", 0) >= quotas.same_solution_shape:
        return False
    if emotion == "neutral" and \
            c.get("emotion:neutral", 0) >= quotas.neutral_emotion:
        return False
    if sig.trauma_ritual() and c.get("trauma_ritual", 0) >= quotas.trauma_ritual:
        return False
    if sig.grief() and c.get("grief", 0) >= quotas.grief:
        return False
    if sig.death and c.get("death", 0) >= quotas.death:
        return False
    if sig.past_trauma and c.get("past_trauma", 0) >= quotas.past_trauma:
        return False
    if (sig.long_term_profession and sig.repeated_ritual
            and c.get("profession_ritual", 0) >= quotas.profession_ritual):
        return False
    return True


def choose_emotion(recent: Optional[list],
                   rng: Optional[random.Random] = None,
                   quotas: Optional[Quotas] = None) -> str:
    """第 2 层: 独立按 observed 分布挑情绪(**不看 family/shape**)。

    规则:
      - `neutral` 达 `neutral_emotion` 上限后**不可选**;
      - 其余尽量补"最近缺口": 最近窗口里出现得少的情绪优先。
    """
    rng = rng or random.Random()
    q = quotas or Quotas()
    c = signature_counts(recent, q.window)
    ok = []
    for emo in EMOTION_MODES:
        if emo == "neutral" and c.get("emotion:neutral", 0) >= q.neutral_emotion:
            continue
        ok.append(emo)
    if not ok:
        return "neutral"      # 全堵死时的兜底(理论上不可达: neutral 之上还有别的)
    # 缺口越大(计数越少)权重越高 —— 这就是"补最近缺口"。
    weights = [1.0 / (1.0 + c.get(f"emotion:{e}", 0)) for e in ok]
    total = sum(weights)
    pick = rng.random() * total
    acc = 0.0
    for emo, w in zip(ok, weights):
        acc += w
        if pick <= acc:
            return emo
    return ok[-1]


def choose_family_shape(recent: Optional[list], emotion: str,
                        rng: Optional[random.Random] = None,
                        quotas: Optional[Quotas] = None) -> tuple:
    """第 3 层: 在**与选定 emotion 相容**的 shape 里, 按 family 权重选。

    ⚠️ 关键: family 权重**在 family 层算一次**(`family_headroom`), 与
    该 family 能展开出多少 shape/domain/relation **无关**。这样后面的维度
    不会反过来改变 family 被选中的概率。

    返回 `(family, shape)`; 全堵死时返回 `("", "")`。
    """
    rng = rng or random.Random()
    q = quotas or Quotas()
    weights = family_headroom(q, recent)
    # 只留"在这一 emotion 下至少有一个合法 shape 且过静态配额"的 family
    legal: list = []
    for fam, w in weights.items():
        if w <= 0:
            continue
        shapes = [sh for sh in _legal_shapes_for(fam, emotion)
                  if _quota_allows(fam, sh, emotion, q, recent)]
        if shapes:
            legal.append((fam, shapes, w))
    if not legal:
        return "", ""
    total = sum(w for _f, _s, w in legal)
    pick = rng.random() * total
    acc = 0.0
    chosen = legal[-1]
    for item in legal:
        acc += item[2]
        if pick <= acc:
            chosen = item
            break
    fam, shapes, _w = chosen
    # shape 在 family 内部等权随机 —— family 的概率已经在上面定死了,
    # 这里不再额外引入与展开宽度相关的偏见。
    return fam, shapes[rng.randrange(len(shapes))]


def choose_domain_relation(recent: Optional[list], fam: str, shape: str,
                           emotion: str,
                           rng: Optional[random.Random] = None,
                           quotas: Optional[Quotas] = None) -> tuple:
    """第 4 层: 最后挑 domain / relation, 只受各自配额约束。

    ⚠️ 这一层**绝不允许**反向影响 family 概率 —— 它在 family/shape 已经
    定死之后才跑。
    """
    rng = rng or random.Random()
    q = quotas or Quotas()
    c = signature_counts(recent, q.window)
    doms = [d for d in DOMAINS if c.get(f"domain:{d}", 0) < q.same_domain]
    rels = [r for r in RELATIONS if c.get(f"relation:{r}", 0) < q.same_relation]
    dom = doms[rng.randrange(len(doms))] if doms else DOMAINS[0]
    rel = rels[rng.randrange(len(rels))] if rels else RELATIONS[0]
    return dom, rel


def _candidates(quotas: Quotas, recent: Optional[list]) -> list:
    """列出当前**未超配额**的 blueprint 候选(扁平视图, 供内省/测试用)。

    ⚠️ **这不是调度器**。真正的分级选择是
    `choose_reveal_mode -> choose_emotion -> choose_family_shape ->
    choose_domain_relation`(见 `choose_blueprint`)。

    这个函数保留下来是给"我想看看现在有哪些合法组合"这类内省用的 ——
    它是**大笛卡尔积**, 因此**不能**拿它的展开宽度去计权(那正是
    hierarchical scheduler 要避免的"后面的维度污染前面的概率")。
    调度路径不经过它。
    """
    c = signature_counts(recent, quotas.window)
    out = []
    for fam, shapes in FAMILY_SHAPES.items():
        for shape in shapes:
            pinned = SHAPE_FLAGS.get(shape, {}).get("emotion_mode")
            emotions = (pinned,) if pinned else EMOTION_MODES
            for emo in emotions:
                flags = _shape_flags(shape, emo)
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


#: 揭晓结构的选择顺序(Step 02)。**不是**随机均匀 —— 精确按此顺序做
#: "越靠前越优先, 但受 rolling quota 约束":
#:
#:   1. 有翻转的结构(7 种) —— 优先, 因为它们是"意外感"的来源;
#:   2. `straight_explanation` —— 允许, 但**永远排在最后**。
#:
#: 这样既不禁止正面解释(有些题就是不需要翻转), 又保证它不会成为默认。
#: 具体选哪一个由 `choose_reveal_mode` 在配额允许的集合里随机取。
REVEAL_PREFERENCE = (
    "identity_flip",
    "meaning_flip",
    "causal_flip",
    "goal_flip",
    "recontextualization",
    "hidden_stakes",
    "perspective_flip",
    "straight_explanation",     # 永远最后
)


def choose_reveal_mode(recent: Optional[list],
                       rng: Optional[random.Random] = None,
                       quotas: Optional[Quotas] = None) -> str:
    """第 1 层: 按 **observed 缺口** 挑一个未超配额的 reveal_mode。

    ## 配额是 observed, 目标是 target

    配额统计的是 **observed**(Reviewer 读完回传的值, 见
    `signature_counts`)。这里返回的是**调度目标** —— 它是给生成器的
    一个方向, 不是"已经播出的分布"。两者刻意分开:

      - 目标 -> 引导生成器往稀缺的结构走;
      - observed -> 真正记进最近窗口、参与配额的量。

    ## "按缺口选"是什么意思

    不是"没到上限就等权随机"。早先那样写, `identity_flip` 已经出现 1 次
    而 `goal_flip` 是 0 次时并不会优先补 `goal_flip`。现在权重 = 1/(1+出现
    次数), 所以**出现得越少越优先** —— 这才叫"补最近缺口"。

    `straight_explanation` 额外承受 `straight_explanation` 上限, 且在
    权重上**再降一档** —— 允许, 但永远最低优先级。

    挑不到(全被配额堵死)时返回 `straight_explanation` —— 永远有返回值,
    出题链不能因为配额算法卡死。
    """
    rng = rng or random.Random()
    q = quotas or Quotas()
    c = signature_counts(recent, q.window)
    ok_modes = []
    for mode in REVEAL_PREFERENCE:
        # 任何 reveal_mode 都受 same_reveal_mode 约束;
        # straight 额外还受 straight_explanation 约束(取更严的那个)。
        if c.get(f"reveal:{mode}", 0) >= q.same_reveal_mode:
            continue
        if (mode == "straight_explanation"
                and c.get("reveal:straight_explanation", 0)
                >= q.straight_explanation):
            continue
        ok_modes.append(mode)
    if not ok_modes:
        return "straight_explanation"
    weights = []
    for mode in ok_modes:
        w = 1.0 / (1.0 + c.get(f"reveal:{mode}", 0))      # 缺口越大越优先
        if mode == "straight_explanation":
            w *= 0.25                                     # 再降一档
        weights.append(w)
    total = sum(weights)
    pick = rng.random() * total
    acc = 0.0
    for mode, w in zip(ok_modes, weights):
        acc += w
        if pick <= acc:
            return mode
    return ok_modes[-1]


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
    """**分层**选一个 blueprint(方案 §11)。顺序是冻结的:

        observed recent
          ↓ 1. reveal:  按缺口选 target(straight 永远最低优先级)
          ↓ 2. emotion: 独立按 observed 分布选(neutral 有硬上限)
          ↓ 3. family/shape: 只在与该 emotion **相容**的 shape 里选;
          ↓                 family 权重**在 family 层算一次**
          ↓ 4. domain/relation: 最后选

    ## 为什么必须是分层, 不是"大笛卡尔积抽彩票"

    早先的做法是把 `family × shape × emotion × domain × relation` 全展开
    成候选, 再给每个 candidate 乘 family 的 LRU 权重。问题: 一个 family
    若能展开出更多 emotion/domain/relation 组合, 它就天然拿到更多彩票 ——
    **后面的维度反过来污染了前面的选择概率**。例如"普通 shape"能搭全部
    情绪、而"创伤 shape"的情绪被钉死成 grief, 那么即使两者 LRU 权重相同,
    普通 shape 也会被系统性高估。

    分层之后每一层只看自己那层的账, 概率不再跨层泄漏。

    任何一层全堵死时都会退到兜底值, **永远有返回值** —— 出题链不能因为
    配额算法卡死。
    """
    rng = rng or random.Random()
    q = quotas or Quotas()

    # ---- 1. reveal(目标) ----
    target_reveal = choose_reveal_mode(recent, rng=rng, quotas=q)

    # ---- 2. emotion(独立) ----
    emotion = choose_emotion(recent, rng=rng, quotas=q)

    # ---- 3. family / shape(与 emotion 相容) ----
    fam, shape = choose_family_shape(recent, emotion, rng=rng, quotas=q)
    if not fam:
        # 该 emotion 下全被堵死 -> 退回"最久没出现的 family", 用它的第一个
        # 合法 shape; 仍无合法 shape 就用第一个 shape(保底不卡死)。
        bp = _least_recently_seen(q, recent)
        bp.reveal_mode = target_reveal
        shapes = _legal_shapes_for(bp.mechanism_family, emotion) or \
            list(FAMILY_SHAPES.get(bp.mechanism_family, ("information_advantage",)))
        bp.solution_shape = shapes[0]
        bp.emotion_mode = emotion
        dom, rel = choose_domain_relation(recent, bp.mechanism_family,
                                          bp.solution_shape, emotion,
                                          rng=rng, quotas=q)
        bp.domain, bp.relation = dom, rel
        return bp

    # ---- 4. domain / relation(最后, 不影响上面) ----
    dom, rel = choose_domain_relation(recent, fam, shape, emotion,
                                      rng=rng, quotas=q)
    flags = _shape_flags(shape, emotion)
    return PuzzleBlueprint(
        mechanism_family=fam, solution_shape=shape,
        domain=dom, relation=rel,
        emotion_mode=flags["emotion_mode"], time_shape=flags["time_shape"],
        death=flags.get("death", False),
        past_trauma=flags.get("past_trauma", False),
        long_term_profession=flags.get("long_term_profession", False),
        repeated_ritual=flags.get("repeated_ritual", False),
        reveal_mode=target_reveal)


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


# ======================================================================
# 文本近似(3-gram Jaccard)
# ======================================================================
# 从 `llm.py` **下移**到这里。理由: 它是纯文本工具, 不碰任何 LLM 概念,
# 而 `llm.py` 本来就 `from .quality import ...` —— 留在 llm 里会让
# 任何想复用它的人(比如题池)反向依赖 llm, 从而把 urllib/logging 那套
# 初始化一起拖进来。放这里, 依赖方向才是对的。
def ngrams(text: str, n: int = 3) -> set:
    """把文本切成 n-gram 字符集合(只看汉字/数字, 忽略标点空白)。"""
    body = "".join(c for c in (text or "") if "一" <= c <= "鿿" or c.isdigit())
    if len(body) < n:
        return {body} if body else set()
    return {body[i:i + n] for i in range(len(body) - n + 1)}


def too_similar(puzzle: str, used: list,
                threshold: float = 0.22) -> str:
    """新谜面是否和已出过的某条太像? 返回相似的那条, 否则 ""。

    用 3-gram 的 Jaccard 相似度 —— 换个说法重讲同一道题时, 用词会高度
    重叠, 这个指标能抓住。实测标定: 同题改写 ≈0.29, 完全不同 ≈0.00,
    所以阈值取 0.22 落在两者中间(有很宽的余量, 不会误杀同题材新题)。
    """
    a = ngrams(puzzle)
    if not a:
        return ""
    for u in used or []:
        b = ngrams(u)
        if not b:
            continue
        inter = len(a & b)
        union = len(a | b)
        if union and inter / union >= threshold:
            return u
    return ""


def policy_version() -> str:    return QUALITY_POLICY_VERSION


# ======================================================================
# 6.4 Reveal adherence(Step 02 / Batch A closeout)
# ======================================================================
def validate_reveal_adherence(spec: PuzzleSpec,
                              blueprint: Optional[PuzzleBlueprint] = None
                              ) -> list:
    """这道题**实际写成的** reveal 结构, 是不是调度器要的那个? 返回违规原因。

    ## 为什么这是"拒绝", 不是"记录"

    冻结的语义是:

        target   = 调度器的**意图**(给生成器的方向)
        observed = Reviewer 读完之后**如实回传**的事实
        quota    = 按 observed 统计

    但 `target != observed` 有一个直接含义: **这稿没有执行调度目标**。
    它不是"观察到一个有趣偏差", 而是"这次调度落空了"。所以必须拒绝,
    否则调度器形同虚设 —— 目标发出去、没人执行、代码也不管。

    ## 为什么不能塞进 `validate_blueprint()`

    `validate_blueprint` 在 **Reviewer 之前也会被调用**(生成器交稿时先
    自查一遍)。而那时 `spec.signature` 是**生成器自报**的值 —— 拿它跟
    blueprint 比, 等于让生成器自己验自己: 它只要照抄目标就能"通过",
    observed 的独立性当场消失。

    所以这一步单独成一个函数, 只在**两处**调用:

        ① Reviewer 之后(此时 signature 是审稿人的 observed 值)
        ② 题池最终准入(`PuzzlePool._validate_pool_spec`)
           —— 挡住"test 期过了但盘上被改成不一致"的题

    返回空列表 = 一致(或无法判定)。
    """
    if spec is None:
        return []
    bp = blueprint if blueprint is not None else getattr(spec, "blueprint", None)
    if bp is None:
        return []
    # ⚠️ 只在 **blueprint 真的被分配过** 时才做比对。
    #
    # `PuzzleBlueprint.reveal_mode` 的 dataclass 默认值是
    # `straight_explanation`(那是"代码没特别指定时的默认生成方向", 不是
    # "这是一道普通题")。于是"自由生成 / 没有调度器"的题身上也会带着这个
    # 默认值 —— 拿它当目标, 就会要求每一道自由生成的题都写成普通解释。
    #
    # `blueprint_specified` 正是"这次有没有真的分配目标"的显式标记
    # (见 `PuzzleSpec.blueprint_specified` 的说明: 它由 gen_spec 显式写,
    # **绝不从值推断**)。所以用它做闸门, 而不是看 reveal_mode 是否非空。
    if not getattr(spec, "blueprint_specified", False):
        return []
    target = str(getattr(bp, "reveal_mode", "") or "")
    if not target:
        return []
    sig = getattr(spec, "signature", None)
    observed = str(getattr(sig, "reveal_mode", "") or "") if sig else ""
    if not observed:
        # 没观察到 -> **不能**当成"一致"。这是 v4 题必须完整回传的原因
        # (见 `_OBSERVED_SIGNATURE_FIELDS`): 缺观察值等于绕开这一步。
        return [f"缺少 observed reveal_mode, 无法确认是否执行了目标 "
                f"{target!r}"]
    if observed != target:
        return [f"reveal 结构没执行调度目标: target={target!r}, "
                f"observed={observed!r}"]
    return []


# ======================================================================
# 6.5 Hint 焦点选择(方案 §31/§33)
# ======================================================================
def hint_focus(spec: PuzzleSpec, touched: Optional[set] = None,
               max_unknown: int = 3) -> dict:
    """挑出这条提示该**点拨哪个方向**。返回给 hint prompt 用的字典。

    方案 §33 的优先级:

        required solve atom -> 它引用的 facts -> 尚未 touched -> hintable

    为什么必须由代码挑而不是让模型自己看: 提示是要**推进推理**的,
    不是随机说一句谜面里的话。模型看不到 touched 集合(它在引擎里),
    所以"哪个方向还没被探索过"这件事只有代码知道。

    返回::

        {
          "focus_atom": "...",          # 该往哪个原子事实上引(文本)
          "focus_facts": ["f2", ...],   # 它依赖、且还没被碰过的 fact id
          "known_or_touched": ["f1"],   # 已被探索过 —— **不要再提**
          "forbidden_core_terms": [...],# 一旦出现在提示里就等于泄底
        }

    注意 `touched` 的含义是"玩家群体**问过**这个方向", **不代表
    他们已经知道该事实为真** —— 所以叫 touched 不叫 discovered。
    已 touched 的方向不该再提示(浪费一条提示额度)。
    """
    touched = set(touched or ())
    facts = spec.fact_by_id()

    # ---- ① 只考虑 required 的 atoms: 通关必须说中的那几条 ----
    atoms = spec.required_atoms() or list(spec.solve_atoms)

    # ---- ② 优先挑"还有未 touched 依赖"的 atom ----
    scored = []
    for a in atoms:
        deps = [fid for fid in (a.fact_ids or []) if fid in facts]
        unknown = [fid for fid in deps if fid not in touched]
        # hintable=False 的 fact 不能当提示方向(它是排除项或元信息)
        usable = [fid for fid in unknown if getattr(facts[fid], "hintable", True)]
        scored.append((len(usable), len(unknown), a, usable))

    # 未 touched 依赖最多的 atom 优先 —— 那里最"欠点拨"。
    # 全都被碰过时 (0,0,...) 会排在后面, 仍然给出一条"综合"提示。
    scored.sort(key=lambda t: (-t[0], -t[1]))
    _, _, atom, focus_ids = scored[0]

    # ---- ③ 一个都没得挑(全 touched / 全 hintable=False) -> 退而求其次 ----
    if not focus_ids:
        for a in atoms:
            deps = [fid for fid in (a.fact_ids or []) if fid in facts]
            focus_ids = [fid for fid in deps
                         if getattr(facts[fid], "hintable", True)]
            if focus_ids:
                atom = a
                break

    # ---- ④ 泄底词表: 提示里出现这些就等于把答案说了 ----
    # 只收 **core + hidden** 的 fact —— support/exclusion 说出来顶多算
    # 少给一次推理空间, 而 core hidden 就是谜底本身。
    forbidden, seen = [], set()
    for f in spec.core_hidden_facts():
        # 太短的 fact 文本整句塞进"禁止出现"没意义(模型没法避免一个字),
        # 所以这里给它**整条文本**作为"不要说出这个意思"的指引。
        if len(f.text) >= 4 and f.text not in seen:
            seen.add(f.text)
            forbidden.append(f.text)
    # 被挑中的 focus facts 也绝不能直接说出口 —— 提示是"往那边看",
    # 不是"把那条事实念出来"。
    for fid in focus_ids:
        t = facts[fid].text
        if fid not in touched and t not in seen:
            seen.add(t)
            forbidden.append(t)

    return {
        "focus_atom": atom.text,
        "focus_facts": focus_ids[:max_unknown],
        "focus_fact_texts": [facts[fid].text for fid in focus_ids[:max_unknown]],
        "known_or_touched": sorted(touched),
        "forbidden_core_terms": forbidden,
    }


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
