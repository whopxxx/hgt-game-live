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

import logging
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

log = logging.getLogger(__name__)

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
QUALITY_POLICY_VERSION = "quality-v8"

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

    def puzzle_touching_fix(self) -> list:
        """**必须改动谜面**才能修好的 fixable 问题(人称/问句/meta)。

        为什么要把这一类单独分出来: 审稿人的 `fix` 分支要求"给出改后的
        谜面" —— 那是它证明自己**真的改过东西**的方式, 对"谜面是第一
        人称"这类毛病完全正确。

        但大多数 fixable 问题**根本不在谜面上**: 压缩 core_answer、
        从谜面重新摘一个 quote、缩短 hint、补一根 atom 连线。对着这些
        要求"交一个新谜面"是荒谬的, 于是旧代码把每一次"只压缩
        core_answer"都判成 `审稿未给出改稿` -> rewrite -> **整题重出**。

        ## 判据是"白名单"而不是"黑名单"

        这里**只列必须动谜面的那几条**, 而不是去给其它每一条打
        "不涉及谜面"的标记。理由: 将来新增一条 fixable 规则时, 忘了
        打标记的后果是"它被当成需要动谜面"(保守, 最多多要一次改稿);
        反过来若用"不涉及"标记, 忘了标的后果是"它被当成不需要动谜面"
        (放任一次真的没改). 保守的那个方向才是对的。

        标记写在那三条 `can_fix()` 的文案里(`_PUZZLE_TOUCH_MARK`)。
        """
        return [f for f in self.fixable if _PUZZLE_TOUCH_MARK in f]

    def only_puzzle_free_fixes(self) -> bool:
        """这次修复是否**完全**不需要动谜面(且确实有要修的东西)。"""
        return bool(self.fixable) and not self.puzzle_touching_fix()

    def fix_reasons(self) -> list:
        """把 `fixable` 归成**短标签**, 供指标/日志按原因分类。

        ---- G4 可观测性 ----
        任务书要求下一场直播能直接看到:

            以前 10 次 hard reject
            现在其中 6 次被 repair 救回

        没有这个分类就只能从日志肉眼看。**不新增任何 LLM 调用**, 纯粹
        是对已经算出来的 `fixable` 文案做归类。

        判据是**子串匹配**, 顺序即优先级(一条文案只归一类)。这与
        `puzzle_touching_fix()` 用文本标记是同一个理由: 文案的**生产者**
        只有本文档里的那几处 `can_fix()`, 多处各写一份判据必然漂移。

        认不出的文案一律归 `"other"` —— 宁可分类不全, 也不要猜错后
        让指标说谎。
        """
        out: list = []
        for f in self.fixable:
            for slug, needle in _FIX_REASON_PATTERNS:
                if needle in f:
                    out.append(slug)
                    break
            else:
                out.append("other")
        return out


#: `fix_reasons()` 的归类表。顺序即优先级 —— 先匹配到的先算。
#: 每加一条新的 `can_fix()` 规则, 这里**应当**同步加一行; 忘了加不会
#: 出错(落到 "other"), 只是指标少一个维度 —— 这个方向比"猜错"安全。
_FIX_REASON_PATTERNS = (
    ("puzzle_question", "谜面结尾不是问句"),
    ("puzzle_person", "谜面是第一人称叙事"),
    ("puzzle_meta", "谜面混进了"),
    ("fact_enum", "的 kind 非法"),
    ("fact_enum", "的 visibility 非法"),
    ("core_length", "core_answer 有"),
    ("linkage", "没有被任何 solve_atom 引用"),
    ("linkage", "指向通关事实的推理路径"),
    ("clue_quote", "fair_clue 缺 quote"),
    ("clue_quote", "的 quote 不在谜面里"),
    ("hint_fix", "条提示超过"),
)


#: 标在"**必须改动谜面**"的修复文案里的记号。
#: 见 `ValidationResult.puzzle_touching_fix` —— 这是白名单, 不是黑名单。
#:
#: 为什么用文本标记而不是另加一个字段: `fixable` 的**消费方**只有一处
#: (转成审稿人的 must_fix 文本), 多一个平行字段就得两处各写一遍判据,
#: 而它们迟早会漂移 —— 那正是 L1 里 `_candidate_block_reason_locked`
#: 的同一个教训。
_PUZZLE_TOUCH_MARK = "[需改谜面]"


# ======================================================================
# 1. spec 结构校验(方案 §21)
# ======================================================================
#: v5 通关合同里 `core_answer` 的硬上限(汉字数)。推荐 <=60, 上限 80。
#: 为什么要有硬上限: "一句话核心答案"如果写成两三百字, 观众听不出重点,
#: 而揭晓是**确定性**地念它(不再经 LLM 加工) —— 长答案会直接拖垮体验。
CORE_ANSWER_MAX_LEN = 80
#: ---- G2-A: core_answer 的**可修**上限 ----
#:
#: 80 字是最终硬门, **不放宽**。但 81~120 字不是"故事坏了", 它是
#: "这句话太长, 压缩一下" —— 一个 reviewer 一句话就能改好的问题。
#:
#: 实播日志里反复出现 `core_answer 81 字` / `core_answer 97 字`, 每一次
#: 都丢掉**整道题**并重新出稿(再花 1 次生成 + 1 次审稿 + 1 次 audit)。
#: 那不是质量政策在起作用, 那是把"改一句话"的活干成了"重造一遍"。
#:
#: 所以分档:
#:     <= 80      通过
#:     81 ~ 120   fixable —— 交给 reviewer **只压缩 core_answer**
#:     > 120      这是真的写偏了(一段话而不是一句话), 硬失败
CORE_ANSWER_FIXABLE_MAX_LEN = 120
#: 通关合同的条数上限。**刻意只有 2** —— 见下面校验里的说明。
#:
#: ⚠️ 它**不是**整道题的复杂度上限。v8 起这一点由 `discovery_beats`
#: 显式承载: 题目可以有 2~4 个发现阶段, 而通关仍然只需 1~2 条事实。
#: 产品规则: **题目允许有层次, 通关必须简单。**
MAX_COMPLETION_FACTS = 2
#: discovery_beats 的条数区间(quality-v8)。少于 2 = 没有层次;
#: 多于 4 = 一道海龟汤塞不下, 观众会跟丢。
MIN_DISCOVERY_BEATS = 2
MAX_DISCOVERY_BEATS = 4


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
            r.can_fix(f"{_PUZZLE_TOUCH_MARK} 谜面结尾不是问句, "
                      f"末尾补一句'为什么?'")
        if is_first_person(spec.puzzle):
            r.can_fix(f"{_PUZZLE_TOUCH_MARK} 谜面是第一人称叙事, "
                      f"改成第三人称客观事实")
        if has_meta_text(spec.puzzle):
            r.can_fix(f"{_PUZZLE_TOUCH_MARK} 谜面混进了【谜底】/【提示】"
                      f"之类的元文本, 删掉它们")

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
            # ---- G2-E: kind/visibility 明显互换 -> **原地换回来** ----
            #
            # 实播日志里反复出现 `fact fN kind 非法: public`。而 `public`
            # 显然是一个 **visibility** 值 —— 模型把两个字段填反了。
            #
            # 这是**唯一**一种代码可以安全自作主张的情况: 只有当
            #     kind 是合法 visibility **且** visibility 是合法 kind
            # 时, 交换两个字段是无歧义的(两个值都各得其所)。
            if (f.kind in FACT_VISIBILITY
                    and f.visibility in FACT_KINDS):
                log.info("fact %s 的 kind/visibility 填反了(%r/%r), "
                         "原地交换", f.id, f.kind, f.visibility)
                f.kind, f.visibility = f.visibility, f.kind
            else:
                # ---- G4-A: 其它 enum 错位 -> **可修**, 不再整稿重造 ----
                #
                # G2 在这里写的是 `r.fail()`, 于是**这条真实故障根本没被
                # 消灭**。模型最常产生的其实不是完整互换, 而是半错位:
                #
                #     kind = "public"      <- 合法 visibility
                #     visibility = "hidden" <- 合法 visibility
                #
                # 此时 `visibility in FACT_KINDS` 为假 -> 不满足上面的
                # 交换条件 -> 落到 fail -> 整稿扔掉。实播日志里那三条
                # `fact f7 kind 非法: public` 走的正是这条路。
                #
                # 现在改成: 交给 reviewer **只修 kind/visibility**。
                # 它是"某个字段填了另一个枚举里的词", 不是"事实内容坏了"。
                r.can_fix(
                    f"fact {f.id} 的 kind 非法({f.kind!r} 不在 {FACT_KINDS} "
                    f"里): **只把 kind 改成合法值之一**({FACT_KINDS}), "
                    f"并确认 visibility 是 {FACT_VISIBILITY} 之一。"
                    f"**保持 fact.id 与 fact.text 原样** —— 不要借机重写"
                    f"这条事实的内容, 也不要增删 facts。")
        if f.visibility not in FACT_VISIBILITY:
            # 同上: 与 kind 那条配对, 但也可能**只有** visibility 错
            # (kind 合法) —— 那种情况上面那条不会触发, 必须在这里兜住。
            if f.kind in FACT_KINDS:
                r.can_fix(
                    f"fact {f.id} 的 visibility 非法({f.visibility!r} 不在 "
                    f"{FACT_VISIBILITY} 里): **只把 visibility 改成 "
                    f"{FACT_VISIBILITY} 之一**。**保持 fact.id 与 fact.text "
                    f"原样**, 不要增删 facts。")

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
            r.fail(f"当前政策({QUALITY_POLICY_VERSION}) spec 缺 "
                   f"completion_fact_ids(v5 标签不能配 legacy 通关语义)")
        if not (spec.core_answer or "").strip():
            r.fail(f"当前政策({QUALITY_POLICY_VERSION}) spec 缺 core_answer")

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
            # ---- G2-A: 分档处置, 最终门不放宽 ----
            # <= 80 通过; 81~120 交给 reviewer **只压缩这句话**;
            # > 120 才是真的写偏了(一段话而不是一句话)。
            #
            # 为什么 81 字不该丢掉整道题: 实播日志里它反复出现, 每次
            # 代价是"重新出稿 + 再审稿 + 再 audit"。而它要改的东西只是
            # 一句话的长度 —— 故事、facts、atoms、clues 全部照旧成立。
            if len(ca) > CORE_ANSWER_FIXABLE_MAX_LEN:
                r.fail(f"core_answer 有 {len(ca)} 字, 超过 "
                       f"{CORE_ANSWER_FIXABLE_MAX_LEN} 字(这已经是一段话, "
                       f"不是一句话了 —— 请重新提炼核心答案)")
            elif len(ca) > CORE_ANSWER_MAX_LEN:
                # 明确告诉 reviewer 该干什么, 以及**不该**干什么 ——
                # 它有权改的只有这一句话。
                r.can_fix(
                    f"core_answer 有 {len(ca)} 字, 超过 {CORE_ANSWER_MAX_LEN} "
                    f"字上限: **只压缩 core_answer 本身**到 "
                    f"{CORE_ANSWER_MAX_LEN} 字以内, 保持同一含义; "
                    f"不要改动谜面/谜底/facts/completion_fact_ids/"
                    f"discovery_beats。")
            if "\n" in spec.core_answer or "\r" in spec.core_answer:
                r.fail("core_answer 不能换行(揭晓时会原样念给观众)")
        # (b) 条数 1~2: **不放宽成 4、5 条**。
        #
        # ⚠️ C6-B: 这里的文案会**原样进 `seen_why`**, 下一稿收到的是
        # "【上一稿不合格的地方】结构问题: ..."。旧文案写的是"超过说明这题
        # 太绕, 应重出" —— 它把 `completion_fact_ids`(**什么时候算解出**)
        # 说成了**整道题的复杂度上限**, 于是模型一边收到 v8 prompt 的
        # "题目允许有层次", 一边收到"这题太绕, 应重出", 重新把题写简单。
        # 那正是 Q2 想消灭的行为, 从 deterministic validator 的反馈链
        # 又钻了回来。
        #
        # 硬拒照旧, 只改**为什么拒、下一稿该怎么修**。
        if not (1 <= len(comp) <= MAX_COMPLETION_FACTS):
            r.fail(f"completion_fact_ids 有 {len(comp)} 条, 应为 1~"
                   f"{MAX_COMPLETION_FACTS} 条。通关合同写得过细: 请收窄为 "
                   f"core_answer 的最小 1~{MAX_COMPLETION_FACTS} 条核心语义; "
                   f"不要因此简化谜题本身, 也不要删除有效的 discovery_beats。")
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
        #
        # ---- G2-C: 区分"内容不存在" 与 "metadata 没连好" ----
        # 这两种情况的处置完全不同:
        #
        #   completion fact **在 facts 里不存在** -> 硬失败。
        #       不是连线问题, 是内容缺失 —— reviewer 改不出来(它不能
        #       凭空造一条事实, 那等于替生成器写题)。
        #
        #   fact 存在, 只是**没有 atom 引用它** -> fixable。
        #       这是一个**连线**问题: 事实在、atom 也在, 只是 `fact_ids`
        #       漏标了。reviewer 完全有能力把它接上, 而重出整题是浪费。
        #
        # ⚠️ 上面 (b) 已经对"引用了不存在的 fact"报过 fail, 所以走到
        #    这里 orphan 里的 id 一定都真实存在 —— 但**不能依赖那个
        #    顺序假设**: 这里显式再查一次 `seen_f`, 免得将来有人调整
        #    校验顺序就把这条静默变成"必定 fixable"。
        if comp and atoms:
            atom_facts = {fid for a in atoms for fid in (a.fact_ids or [])}
            orphan = [fid for fid in comp if fid not in atom_facts]
            missing = [fid for fid in orphan if fid not in seen_f]
            linkable = [fid for fid in orphan if fid in seen_f]
            if missing:
                r.fail("completion fact 引用了不存在的 fact(内容缺失, "
                       "不是连线问题): " + ", ".join(missing))
            if linkable:
                r.can_fix(
                    "completion fact 没有被任何 solve_atom 引用(事实与 "
                    "atom 都在, 只是连线漏了): " + ", ".join(linkable)
                    + " —— 请在现有 atom 的 fact_ids 里补上它, 或在确实"
                      "缺少对应 atom 时补一条 **引用已有 fact** 的 atom。"
                      "**不得**为了通过结构检查而编造推理关系。")
        elif comp and not atoms:
            # 有合同却一条 atom 都没有: reviewer 无从"连线", 这是真缺内容。
            r.fail("有通关合同但没有 solve_atom(观众没有推理抓手)")

    # ---- core hidden facts 上限 ----
    n_core = len(spec.core_hidden_facts())
    if n_core > max_core_hidden:
        r.fail(f"core hidden facts 有 {n_core} 条, 超过 {max_core_hidden}")

    # ---- fair_clues: 必须真的在谜面里 ----
    #
    # ---- G2-B: quote 不在谜面 -> **可修**, 不是整稿重出 ----
    #
    # 实播日志里 `fair_clue quote 不在谜面` 反复出现, 每次丢掉整道题。
    # 但这是**摘录**问题, 不是故事问题: 推理关系是对的, 只是引用的
    # 那句话没有逐字对上谜面(多一个字、少一个标点、或者模型顺手
    # 改写了一下)。
    #
    # 交给 reviewer: **从当前谜面重新逐字摘取**。两条禁令必须写死在
    # 反馈里, 否则它会走捷径:
    #
    #     ✗ 改谜面去迁就这个 quote  —— 那是让题面服务于校验
    #     ✗ 编一个谜面里根本没有的 quote —— 那等于伪造线索
    #
    # 修完必须**再过一次**这个确定性校验(gen_spec 的 ④ 已经这么做了);
    # 仍然不在谜面里, 才 reject candidate。
    clues = spec.fair_clues or []
    if not clues:
        r.fail("没有 fair_clue(谜面里必须有可回溯的线索)")
    for c in clues:
        if not c.quote:
            r.can_fix(
                "fair_clue 缺 quote: 请从当前谜面里**逐字**摘一段作为 "
                "quote —— 不得改动谜面, 也不得编造谜面里没有的句子。")
            continue
        if spec.puzzle and not quote_in_puzzle(c.quote, spec.puzzle):
            r.can_fix(
                f"fair_clue 的 quote 不在谜面里({c.quote[:30]}): 请从**当前"
                f"谜面**重新逐字摘取一段。不得改动谜面去迁就这个 quote, "
                f"也不得编造谜面里没有的句子。")
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
    #
    # ---- G2-C: 同上, 区分"没有公平线索" 与 "线索没连到通关路径" ----
    # 两者都是 fixable 的候选, 但**反馈措辞**必须不同 —— reviewer 拿到
    # 的指令决定了它是"去接一根线"还是"承认这题缺线索, 请求重写"。
    if has_contract and atoms and clues:
        completion_ids = set(spec.completion_fact_ids)
        completion_atom_ids = {
            a.id for a in atoms
            if completion_ids.intersection(a.fact_ids or [])}
        clued_atom_ids = {
            aid for c in clues for aid in (c.supports_atoms or [])}
        if completion_atom_ids and clued_atom_ids:
            if not (completion_atom_ids & clued_atom_ids):
                r.can_fix(
                    "没有任何 fair_clue 指向通关事实的推理路径"
                    "(线索指不到 completion, 题目不公平)"
                    f" [completion atoms={sorted(completion_atom_ids)}"
                    f", clued={sorted(clued_atom_ids)}] —— 请把这些 clue 的 "
                    f"supports_atoms 接到通关路径上的 atom; 如果谜面里"
                    f"**根本没有**能通向通关事实的公平线索, 那是内容缺失, "
                    f"请把 decision 设为 rewrite 而不是硬接一根不存在的线。")

    # ---- hints ----
    # ---- G2-D: 只是"太长"不该丢掉整道题 ----
    # 30 字上限继续保留(最终门不变)。但一条提示超长是**改写一句话**
    # 的事 —— 优先让当前 Reviewer 的 fix bundle 顺手缩短; 若它其余部分
    # 已经 PASS、只剩 hints 不合格, gen_spec 会走一次**只修 hints** 的
    # 窄修复(见 `_repair_hints`)。那一步明确禁止改
    # puzzle / answer / core_answer / facts / completion / atoms / beats。
    hints = spec.hints or []
    if len(hints) != 3:
        r.fail(f"hints 应为 3 条, 实为 {len(hints)}")
    for i, h in enumerate(hints):
        if len(h or "") > max_hint_len:
            r.can_fix(f"第 {i + 1} 条提示超过 {max_hint_len} 字"
                      f"({len(h)} 字): 请**只**把它缩短到 {max_hint_len} 字"
                      f"以内并保持提示意图, 不要改动谜面/谜底/核心答案/"
                      f"facts/completion_fact_ids/discovery_beats。")

    # ---- quality-v8: discovery_beats ----
    #
    # 只做**确定性**校验。这里刻意**不**判断"这两个 beat 语义上是不是
    # 重复" —— 那是 Reviewer 的活(它读得懂语义), 代码硬判只会误伤。
    # 代码能判的是结构: 条数、唯一、非空、引用存在、整组不重复、
    # 至少一条通向通关路径。
    beats = list(getattr(spec, "discovery_beats", None) or [])
    if is_v5:
        # 只对**当前政策**的题强制。旧 archive 读到空表是合法的 ——
        # 那时还没有这个概念, 不能因此判旧题不合格。
        if not (MIN_DISCOVERY_BEATS <= len(beats) <= MAX_DISCOVERY_BEATS):
            r.fail(f"当前政策({QUALITY_POLICY_VERSION}) 的 "
                   f"discovery_beats 有 {len(beats)} 条, 应为 "
                   f"{MIN_DISCOVERY_BEATS}~{MAX_DISCOVERY_BEATS} 条"
                   f"(题目允许有层次 —— 但通关仍只需 "
                   f"completion_fact_ids 那 1~{MAX_COMPLETION_FACTS} 条)")
    if beats:
        known_facts = {f.id for f in (spec.facts or []) if f.id}
        seen_ids: set = set()
        seen_texts: list = []
        for i, b in enumerate(beats):
            bid = str(getattr(b, "id", "") or "").strip()
            btxt = str(getattr(b, "text", "") or "").strip()
            if not bid:
                r.fail(f"discovery_beat #{i + 1} 缺 id")
            elif bid in seen_ids:
                r.fail(f"discovery_beat id 重复: {bid}")
            else:
                seen_ids.add(bid)
            if not btxt:
                r.fail(f"discovery_beat {bid or ('#' + str(i + 1))} 的 text 为空")
            else:
                seen_texts.append(btxt)
            for fid in (getattr(b, "fact_ids", None) or []):
                if known_facts and str(fid).strip() not in known_facts:
                    r.fail(f"discovery_beat {bid or i + 1} 引用了不存在的 "
                           f"fact: {fid}")
        # 整组完全相同 = 没有层次。**逐条**判断是否只差一个词的
        # 语义重复交给 Reviewer(代码判不了)。
        if len(seen_texts) >= 2 and len(set(seen_texts)) == 1:
            r.fail("discovery_beats 整组文本完全相同(没有层次)")
        # 至少一个 beat 要通向通关路径 —— 否则这套层次与"解出这题"
        # 毫无关系, 纯粹是装饰。
        if spec.completion_fact_ids:
            comp = {str(x).strip() for x in spec.completion_fact_ids
                    if str(x).strip()}
            reaches = any(
                comp & {str(x).strip()
                        for x in (getattr(b, "fact_ids", None) or [])}
                for b in beats)
            if not reaches:
                r.fail("discovery_beats 里没有任何一条通向通关事实"
                       "(层次与'解出这题'无关, 是装饰)")

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
    straight_explanation: int = 1
    #: `neutral` 情绪上限。Step 02 去掉了"永远选 neutral"的固定偏置,
    #: 这条是防止它从另一个方向坍缩(比如全变 warm)。
    neutral_emotion: int = 3
    #: 主要靠制度性设定成立的题上限(`procedural_rule_dependency`, observed)。
    procedural_rule: int = 1
    # ---- v8/C4: 最近 window 题的"诡异/紧张"目标带 ----
    #: `eerie` + `tense` 在**滚动窗口**里应落进的区间。
    #:
    #: ⚠️ 这是**目标带**, 不是"每道都必须落在这里"的硬配额 —— 见
    #: `choose_emotion` 的三分支规则。用区间而不是定值: 定值会让调度器
    #: 每轮硬凑, 挤压 absurd/warm/neutral/grief 的空间。
    #:
    #: `min = max = 0` 表示**关掉这条规则**(退回纯缺口加权的旧行为),
    #: 这样老配置/老测试不需要改。
    dark_tone_min: int = 0
    dark_tone_max: int = 0

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
            straight_explanation=g("quota_straight_explanation", 1),
            neutral_emotion=g("quota_neutral_emotion", 3),
            procedural_rule=g("quota_procedural_rule", 1),
            # C4: 目标带来自 Config 的两个整数字段。缺省 0/0 = 关闭,
            # 这让"没配这条规则的调用方"行为与 v8 之前逐位相同。
            dark_tone_min=g("quality_dark_tone_min", 0) or 0,
            dark_tone_max=g("quality_dark_tone_max", 0) or 0,
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
    # ---- C6-A: 诡异基调目标带的**交付**门 ----
    #
    # C4 把 5~6 接进了生成时的 `choose_emotion()`, 但生产路径是
    # prefetch -> 池 -> `pop_next()`, 而池里的题**不会**进 Engine 的
    # recent: 揭晓期连续预生成时, 后一道看不到前一道已经囤了 dark, 于是
    # 能囤出"第 7 道 dark"。真正交付时这里若不管, 滚动窗口就变 7 ——
    # 目标带在**生产路径**上被绕过(C4 的单元测试只连续调 choose_emotion,
    # 测不到这条)。
    #
    # 判据与生成时**同源**(`dark_tone_allowed`), 所以"池中陈旧候选"
    # 会在交付这一刻按**当下** recent 重新判一次 —— 这正是我们要的:
    # 生成前 target + 生成后 gate + 交付时重判, 三层一套规则。
    #
    # `emotion_mode` 为空(没观察到)时**不判** —— 与 `reveal_mode` 同一
    # 条既有原则: 拿未知当某一档会污染统计, 而候选题在交付前 signature
    # 一定由生成器填好, 空值只出现在历史/残缺数据上。
    if sig.emotion_mode and not dark_tone_deliverable(sig.emotion_mode, recent, q):
        dmin, dmax, _on = dark_tone_band(q)
        cur = sum(1 for s in _recent(recent, q.window)
                  if getattr(s, "emotion_mode", "") in DARK_TONE_MODES)
        bad.append(
            f"情绪基调 {sig.emotion_mode} 会把最近 {q.window} 题的"
            f"诡异/紧张数推出目标带 [{dmin}, {dmax}] (当前 {cur} 道)")
    return bad


# ======================================================================
# 4. 结构去重(方案 §10: **不是**"职业不同 = 题目不同")
# ======================================================================
def recent_pairs(recent: Optional[list],
                 window: int = RECENT_WINDOW) -> set:
    """最近窗口里出现过的 `(mechanism_family, solution_shape)` 集合。

    ⚠️ 这是**唯一**的判重键来源 —— `is_structurally_duplicate()` 与
    scheduler 的候选过滤都从它取, 不各写一遍。

    为什么必须共享: 调度器要在"选之前"避开必死的 pair, 而最终 cross gate
    在"生成之后"拒同一个 pair。两处若各写一份判重键, 迟早漂移 ——
    漂移的结果就是调度器高高兴兴选一个后面必被拒的组合, 白烧 3~4 稿
    配额(实播日志里的 rule_constraint/social_constraint 就是这个)。

    刻意**不包含** domain/职业: 方案 §65 明确不要"职业不同 = 题目不同"。
    """
    return {(s.mechanism_family, s.solution_shape)
            for s in _recent(recent, window)
            if s.mechanism_family and s.solution_shape}


def is_structurally_duplicate(sig: PuzzleSignature, recent: Optional[list],
                              window: int = RECENT_WINDOW) -> str:
    """和最近某题**结构上等价**吗? 返回冲突的那个坐标, 否则 ""。

    与 `_too_similar`(3-gram 文本相似)互补: 那个抓"换个说法重讲同一题",
    这个抓"换了个职业但诡计和形状完全一样"。

    判据是 (mechanism_family, solution_shape) 二元组 —— 刻意**不包含**
    domain/职业。方案 §65 明确: 不要再用"职业不同 = 题目不同"。

    判重键来自 `recent_pairs()`, 与 scheduler 的候选过滤**同源**。
    """
    key = (sig.mechanism_family, sig.solution_shape)
    if not key[0] or not key[1]:
        return ""
    if key in recent_pairs(recent, window):
        return f"{key[0]}/{key[1]}"
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
#: ---- G3: 哪些 mechanism_family **天然倾向**于靠"制度/规矩/流程"成立 ----
#:
#: 这不是"禁止"清单, 只是**调度降权**用的先验: 当 `procedural_rule`
#: 配额已经满时, 从这些 family 里选出"主要靠制度成立"的题的概率最高,
#: 而那样的题会被 cross gate 拒。
#:
#: 为什么是"优先从别处选"而不是"永久禁掉": 这些 family 本身完全合法,
#: 而且 rule_constraint / social_rule 覆盖了大量真实题材。真正的问题
#: 只是"配额满了还往那个方向撞"。所以:
#:
#:     还有别的合法 family -> 不选这些
#:     整个候选空间都堵死 -> 允许 fallback, 并打 warning
#:
#: 最终 cross gate 仍然负责拒(defense-in-depth 保留)。
PROCEDURAL_LEANING_FAMILIES = frozenset({
    "rule_constraint", "social_rule",
})

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

    # ---- G3: procedural 配额已满 -> 天然倾向制度的 family 降权 ----
    #
    # 实播日志里最贵的一种浪费:
    #
    #     procedural quota 已经 1/1
    #       ↓ 调度器仍然选 rule_constraint/social_rule
    #       ↓ 模型写出"主要靠制度成立"的题
    #       ↓ cross gate 以'主要靠制度性设定成立'拒掉
    #       ↓ 下一稿又一样
    #
    # 最终 gate 照旧兜底, 但**不该由它当第一道防线**。
    #
    # ⚠️ 降权(×0.15)而不是清零: 清零会让"整个候选空间只剩这两个
    # family 合法"时直接退化到兜底路径, 而那本来是可以正常出题的
    # 情形(题目本身没问题, 只是分布上不理想)。降权保留 fallback
    # 能力, 同时让别的 family 在正常情形下稳定胜出。
    _procedural_full = bool(
        rs and sum(1 for s in rs if s.procedural_rule_dependency)
        >= quotas.procedural_rule)

    out = {}
    for fam in FAMILY_SHAPES:
        age = last_seen.get(fam, -1)
        # age = -1: 最近窗口里从没出现 -> 最优先
        base = 2.0 + 1.0 / n if age < 0 else 1.0 + (n - age) / n
        # 出现次数越少越好(与 age 正交: age 看"多久没见", count 看"见了几次")
        w = base / (1.0 + counts.get(fam, 0))
        if _procedural_full and fam in PROCEDURAL_LEANING_FAMILIES:
            w *= 0.15
        out[fam] = w
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


#: `eerie` + `tense` —— C4 的"诡异/紧张"目标带统计的就是这两档。
#: 定义在这里(而不是各处写字符串)是因为它同时被调度器与测试引用。
DARK_TONE_MODES = ("eerie", "tense")


def _projected_dark_count(recent: Optional[list], q: "Quotas",
                          pick_dark: bool = True) -> int:
    """**下一题选了 `pick_dark` 之后**, 滚动窗口里会有几道 dark。

        sigs = 最近窗口, 若已满先丢掉最老的那道(新题会挤掉它)
        return 剩下这些里的 dark 数 + (1 if pick_dark else 0)

    ## 为什么必须"先丢最老"

    只看当前计数会在满窗时把目标带判错一整题。例: 窗口已有 6 道 dark,
    当前计数 6 >= max 会强制选非 dark —— 但如果最老那题**正好是 dark**,
    加一道 dark 之后窗口仍然是 6(进一出一), 完全在带内。

    ## 为什么下限判定要**同时**看两种选择

    这是 C4 实现里最容易错的一处。下限的语义是"别让窗口掉到 5 以下",
    而掉下去**只需要选一道非 dark**: 窗口 10 道里正好 5 道 dark、且最老
    那道**是 dark** 时, 选非 dark 会把它挤出去 -> 窗口变 4。

    所以下限不能用"选 dark 的预测 >= 5"来判(那等于假设下一题一定是
    dark, 于是 5 道时判定通过、放行任意选择, 结果自由选择挑了非 dark,
    窗口掉到 4 再也回不来 —— 实测 seed=1 就卡在 4)。

    正确判据是**两种选择的预测都要 >= min**:

        pick_dark=True  时 >= min  且  pick_dark=False 时 >= min

    两个都满足才说明"随便选都安全", 才可以放开。只满足 dark 那侧时,
    非 dark 会让窗口跌破下限 -> 强制 dark。
    """
    sigs = _recent(recent, q.window)
    # 加一道 -> 若窗口会溢出, 最老的先出去。
    if q.window > 0 and len(sigs) >= q.window:
        sigs = sigs[1:]
    n = sum(1 for s in sigs
            if getattr(s, "emotion_mode", "") in DARK_TONE_MODES)
    return n + (1 if pick_dark else 0)


def dark_tone_band(quotas: Optional["Quotas"] = None) -> tuple:
    """返回 `(dmin, dmax, band_on)` —— 目标带是否启用, 以及两端。

    `dmax <= 0` 或 `dmin > dmax` 视为**未启用**(0/0 = off)。写在一处,
    是因为调度器和交付门必须对"带到底开没开"有一致的判断 —— 一边认为
    开着、另一边认为关着, 就会出现"生成时被限制、交付时放飞"。
    """
    q = quotas or Quotas()
    dmin, dmax = int(q.dark_tone_min or 0), int(q.dark_tone_max or 0)
    return dmin, dmax, (dmax > 0 and dmin <= dmax)


def dark_tone_allowed(emotion: str, recent: Optional[list],
                      quotas: Optional["Quotas"] = None) -> bool:
    """**这一档情绪此刻能不能出?** —— C4 目标带的唯一共享判据。

    ## 为什么必须共享(而不是让交付门自己判一遍)

    C4 只把目标带接进了**生成时**的 `choose_emotion()`。但生产路径上题的
    实际来源是 prefetch -> 池 -> `pop_next()`, 而 `pop_next()` 只跑
    `cross_puzzle_gate()` -> `check_signature()`, 那里**没有** dark 判据。
    于是揭晓期的连续预生成会这样翻车:

        当前 recent 已有 5 道 dark
        prefetch A 看 recent -> 生成一道 dark 入池
        prefetch B 仍看同一份**已播** recent -> 又生成一道 dark 入池
        (池里的题没进 Engine 的 recent, 后面的 prefetch 不知道前一道已囤)

    真正播题时逐道检查, 但 `check_signature()` 不管 dark —— 第 7 道 dark
    照样交付 -> 滚动窗口变 7, 目标带在**生产路径**上被绕过。单元测试只
    连续调 `choose_emotion()` 是测不到这一条的: 它证明不了池路径也守规矩。

    同源之后是三层的同一套规则:

        生成前 target (choose_emotion)  +  生成后 gate (cross_puzzle_gate)
        +  池中陈旧候选在**交付时**按当下 recent 重新 gate (pop_next)

    ## 规则(与 `choose_emotion` 逐条一致)

        projected > max                       -> 禁止这一侧
        projected < effective_min 且另一侧可行 -> 禁止这一侧
        两侧都低于 min 的 dead-zone            -> 只允许 dark(往带内恢复)
        两侧都在带内                           -> 两侧都允许
        band = 0/0                            -> 不限制

    `effective_min` 在窗口没满时折算成"可达下限"(`min(dmin, filled+1)`),
    理由见 `choose_emotion` 的 docstring —— 窗口里只有 5 道题时, 要求
    projected >= 5 会把 non-dark 全部禁掉, 而那时**唯一**能达到 5 的选择
    就是 dark, 折算只是把这个事实写清楚。
    """
    dmin, dmax, band_on = dark_tone_band(quotas)
    if not band_on:
        return True
    q = quotas or Quotas()
    is_dark = emotion in DARK_TONE_MODES
    proj_self = _projected_dark_count(recent, q, pick_dark=is_dark)
    proj_other = _projected_dark_count(recent, q, pick_dark=not is_dark)
    if proj_self > dmax:
        return False
    filled = len(_recent(recent, q.window))
    effective_min = min(dmin, filled + 1)
    if proj_self < effective_min:
        # 自己够不到下限。只有当**另一侧**也够不到时才是死区 —— 那时
        # 唯一有意义的方向是往带里走, 也就是只放行 dark。
        if proj_other < effective_min:
            return is_dark
        return False
    return True


def dark_tone_deliverable(emotion: str, recent: Optional[list],
                          quotas: Optional["Quotas"] = None) -> bool:
    """**交付/入池**这一刻, 这一档情绪能不能过关? —— `check_signature` 用。

    与 `dark_tone_allowed()` 只差一条: **窗口未满时下限不参与判定**。

    ## 为什么两处必须有这个差别

    生成时 (`choose_emotion`) 与交付时 (`check_signature`) 的容错度本就
    不同 —— 这一点在 C6-A 实测中被一个具体事故逼出来:

        filled = 0 时 effective_min = min(5, 0 + 1) = 1
        非 dark 的投影恒为 0 < 1  ->  被拒

    于是冷启动的**前 5 题全部被强制成 eerie/tense**, 一个刚开播的直播间
    连着 5 道诡异题 —— 那是比"目标带没达标"严重得多的内容事故, 而且它
    不是我们要拦的东西: 交付门要拦的是"**已经播够 10 题**之后还能再塞
    第 7 道 dark 进池"(见 C6-A 的 stale pool regression)。

    生成时的 warm-up 折算**保持原样**: 那是"出题往目标带凑"的调度倾向,
    收窄方向但不阻塞; 交付门是**硬拒**。让交付门也去执行 warm-up 下限,
    等于把"调度偏好"升级成"合法候选判死"。

    上限两边一致(warm-up 期也生效) —— 它只看 `projected`, 与窗口满没满
    无关。
    """
    q = quotas or Quotas()
    if q.window > 0 and len(_recent(recent, q.window)) < q.window:
        dmin, dmax, band_on = dark_tone_band(q)
        if not band_on:
            return True
        # 只守上限: 窗口没满时"超出上限"仍然要拦(否则冷启动也能囤 dark)。
        return _projected_dark_count(
            recent, q, pick_dark=(emotion in DARK_TONE_MODES)) <= dmax
    return dark_tone_allowed(emotion, recent, q)


def choose_emotion(recent: Optional[list],
                   rng: Optional[random.Random] = None,
                   quotas: Optional[Quotas] = None) -> str:
    """第 2 层: 独立按 observed 分布挑情绪(**不看 family/shape**)。

    规则:
      - `neutral` 达 `neutral_emotion` 上限后**不可选**;
      - 其余尽量补"最近缺口": 最近窗口里出现得少的情绪优先。

    ## C4: 滚动窗口的"诡异/紧张"目标带

    v8 把内容基调写进了 prompt, 但 prompt 说了模型也可能连着出 10 道
    温馨题 —— 目标必须**代码化**才有约束力。规则三分支:

        projected_dark < min  -> **强制**从 eerie/tense 里选
        projected_dark >= max -> **强制**从非 dark 里选
        否则                   -> 按缺口权重随机(与旧行为一致)

    `projected_dark` 是"下一题加入之后的窗口"(`_projected_dark_count`)。

    ## 为什么 warm-up 期用"可达下限"而不是直接跳过

    窗口还没满时, 窗口里最多只有 `filled + 1` 道题 —— 前几题无论怎么选
    都不可能立刻满足 `projected >= 5`。但**完全跳过下限**也是错的: 那样
    开头几题会自由漂到 2~3 道 dark, 之后靠 `projected < min` 每次只补
    一道, 而窗口是滚动的 —— 补一道的同时又滚出去一道,**永远追不上**,
    首个完整窗口就锁死在低位(实测 seed=1 停在 2, seed=7 停在 1)。

    所以 warm-up 期的下限取 `min(目标下限, 窗口内可达的最大值)`:

        可达到的最大 dark 数 = 已经出的题数 + 1(这一道)
        有效下限 = min(dmin, 那个最大值)

    这样前几题会**主动**往 dark 上凑(而不是自由漂), 到窗口满时已经
    接近目标带, 之后正常的三分支接手。目标带仍是"尽量", 不是"每题
    都必须" —— warm-up 期它只收窄方向, 不保证逐题命中。
    """
    rng = rng or random.Random()
    q = quotas or Quotas()
    c = signature_counts(recent, q.window)
    dmin, dmax, band_on = dark_tone_band(q)

    ok = []
    for emo in EMOTION_MODES:
        if emo == "neutral" and c.get("emotion:neutral", 0) >= q.neutral_emotion:
            continue
        # ⚠️ 目标带判据走 `dark_tone_allowed()` —— 与 `check_signature()`
        # 的交付门**同一份**实现。在这里重抄一遍三分支的后果, 就是交付门
        # 和生成器对"带内"的理解慢慢漂开(C4 正是栽在这里)。
        if not dark_tone_allowed(emo, recent, q):
            continue
        ok.append(emo)
    if band_on and not ok:
        # ---- 死区: 两种选择都够不到下限 ----
        #
        # 窗口远低于目标带时(例: 10 道里 0 道 dark, 选 dark 也只有 1),
        # 上面两侧都会被拒 -> 候选清空。此时**不能**退回"自由选": 那
        # 等于放弃收敛, 窗口会随机漂移, 永远爬不回带内。
        #
        # 正确动作是**往目标方向走**: 下限够不到时, 唯一有意义的约束
        # 是"别撞上限", 在这个约束里**优先 dark**。
        ok = [e for e in EMOTION_MODES
              if e in DARK_TONE_MODES
              and not (e == "neutral"
                       and c.get("emotion:neutral", 0) >= q.neutral_emotion)]
    if not ok:
        # 兜底: 目标带把某一侧清空时(例如 neutral 用满 + 强制非 dark),
        # 退回"不施加目标带"的候选集, 再由 neutral 那条兜底接管。
        # 绝不返回空 —— 出题链不能因为配额算法卡死。
        ok = [e for e in EMOTION_MODES
              if not (e == "neutral"
                      and c.get("emotion:neutral", 0) >= q.neutral_emotion)]
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

    ## 为什么候选过滤里必须有"结构重复"这一条(S1)

    早先这里只查 `_quota_allows()`(计数配额), 而最终 `cross_puzzle_gate()`
    会用 `is_structurally_duplicate()` 拒掉**同一个 exact pair** —— 两把
    尺子不一样, 于是调度器会主动选一个后面必被拒的组合。实播日志:

        blueprint: rule_constraint / social_constraint
          ↓ recent 里已有完全相同的 pair
          ↓ 第 1/2/4 稿全被 cross gate 拒掉

    烧掉 3~4 稿配额, 而正确答案从一开始就不在合法集合里。

    判重键用 `recent_pairs()` —— 与 cross gate **同源**, 不是各写一份。

    原则: 只要枚举空间里还存在任何合法且不重复的 pair, 就绝不返回结构
    重复的 pair。真全堵死时才返回 `("", "")`, 由兜底路径接手。

    返回 `(family, shape)`; 全堵死时返回 `("", "")`。
    """
    rng = rng or random.Random()
    q = quotas or Quotas()
    weights = family_headroom(q, recent)
    blocked = recent_pairs(recent, q.window)
    # 只留"在这一 emotion 下至少有一个合法 shape、过静态配额、且不与
    # 最近窗口结构重复"的 family
    legal: list = []
    for fam, w in weights.items():
        if w <= 0:
            continue
        shapes = [sh for sh in _legal_shapes_for(fam, emotion)
                  if _quota_allows(fam, sh, emotion, q, recent)
                  and (fam, sh) not in blocked]
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


# ======================================================================
# 5.6 G3: 动态生成约束(告诉生成器"这个方向已经满了")
# ======================================================================
#: 已经被 quota 占满、本次生成**必须避开**的方向。这是给 Generator 的
#: **输入**, 不是给 Reviewer 的跨题职责 —— Reviewer 仍然只审单题。
#
#: ## 为什么要有它
#:
#: 实播日志里的固定形状:
#:
#:     procedural quota 已经 1/1
#:       ↓ 模型继续生成 procedural
#:       ↓ cross gate reject
#:       ↓ 下一稿又 procedural
#:       ↓ 再 reject
#:
#: 最终 gate **必须保留**(它是 defense-in-depth), 但它不该当**第一道**
#: 防线 —— 让模型去写一个"我们早就知道必被拒"的方向, 是纯浪费。
#:
#: ## 与 cross gate 的关系
#:
#: 两者不是二选一, 而是"事前告知 + 事后兜底":
#:
#:     gen_spec 开始 -> 算出饱和约束 -> 注入 prompt   (本函数, 事前)
#:     cross_puzzle_gate                            (事后, 保留)
#:
#: 判据**同源**(都从 `signature_counts` 读), 所以两侧不会漂移。


def saturated_constraints(recent: Optional[list],
                          quotas: Optional[Quotas] = None) -> dict:
    """算出"本次生成必须避开"的方向, 供 prompt 注入。

    返回的 dict 里每一项都是**可读的硬约束**, 空 = 不限:

        {"procedural_rule_dependency": False,   # 已满 -> 本次 MUST false
         "forbidden_reveal_modes": [...],        # 已满的 reveal 结构
         "straight_explanation_full": bool,      # 便捷标志(与上面重叠)
         "recent_pairs": [(fam, shape), ...]}    # 窗口内已用过的结构对

    ⚠️ 判据必须与 `check_signature` **完全一致**, 否则会出现"prompt 说
    可以、gate 说不行"(那正是实播里跨题重复被拒 4 稿的成因)。所以这里
    逐条对着 `check_signature` 的判据写, 并且**只收紧不放松**:
    多告诉模型一条禁令最多让它换个方向, 少告诉一条就是白烧一稿。

    `recent` 为空 -> 什么也不限(第一题本来就没有跨题约束)。
    """
    q = quotas or Quotas()
    rs = _recent(recent, q.window)
    c = signature_counts(recent, q.window)
    out: dict = {
        "procedural_rule_dependency": None,     # None = 不限
        "forbidden_reveal_modes": [],
        "straight_explanation_full": False,
        "recent_pairs": sorted(recent_pairs(recent, q.window)),
    }
    if not rs:
        return out
    # ---- procedural: 已满 -> 本次必须 false ----
    if c.get("procedural_rule", 0) >= q.procedural_rule:
        out["procedural_rule_dependency"] = False
    # ---- straight_explanation 已满 -> 该 reveal 结构禁用 ----
    if c.get("reveal:straight_explanation", 0) >= q.straight_explanation:
        out["straight_explanation_full"] = True
        out["forbidden_reveal_modes"].append("straight_explanation")
    # ---- 其它已经到"同 reveal 模式上限"的 ----
    # `same_reveal_mode` 是**所有** reveal 模式共用的计数上限, 所以这里
    # 逐模式查一遍 —— 到顶的那些都不能再选。
    for mode in REVEAL_MODES:
        if not mode:
            continue
        if c.get(f"reveal:{mode}", 0) >= q.same_reveal_mode:
            if mode not in out["forbidden_reveal_modes"]:
                out["forbidden_reveal_modes"].append(mode)
    out["forbidden_reveal_modes"] = sorted(out["forbidden_reveal_modes"])
    return out


def describe_constraints(con: dict) -> str:
    """把 `saturated_constraints()` 的结果写成给生成器的**硬约束**段落。

    没有约束时返回空字符串 —— **不要**输出一段"没有任何限制"的废话:
    那只会稀释 prompt 里真正的约束。
    """
    if not con:
        return ""
    lines: list = []
    if con.get("procedural_rule_dependency") is False:
        lines.append(
            "- `procedural_rule_dependency` **必须为 false**。"
            "最近窗口里'主要靠制度/规矩/流程才能成立'的题已经到上限了, "
            "再出一道会被跨题门拒掉 —— 那不是题不好, 是分布满了。")
    fbm = list(con.get("forbidden_reveal_modes") or [])
    if fbm:
        lines.append(
            "- 本题的揭晓结构**不得**是: " + " / ".join(fbm)
            + "。它们已经在最近窗口里用满了, 再选会被跨题门拒掉。"
            "请换一种**揭晓时重新解释谜面**的方式。")
    pairs = list(con.get("recent_pairs") or [])
    if pairs:
        shown = ", ".join(f"{a}/{b}" for a, b in pairs[:6])
        lines.append(
            f"- 最近窗口里已经用过这些 (机制/解法) 组合: {shown}。"
            f"**不要**再选其中任何一个 —— 结构重复会被跨题门拒掉。")
    if not lines:
        return ""
    return ("\n\n【本次生成的硬约束(违反会被跨题门拒掉, 白烧一稿)】\n"
            + "\n".join(lines))


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
    """配额堵死所有组合时的兜底: 取最久没出现的 family。

    ⚠️ S1: 这条兜底**也**必须避开最近窗口里的 exact
    `(mechanism_family, solution_shape)` pair。它早先直接取
    `FAMILY_SHAPES[fam][0]` —— 那完全可能正好撞上最近的 pair, 于是
    兜底反而稳定地生成一道必被 cross gate 拒的蓝图。

    原则与 `choose_family_shape` 一致: 只要还有合法且不重复的 pair,
    就绝不返回重复的。只有整个空间真的没有时才退化 —— 那时最终
    cross gate 仍然会拒(defense-in-depth)。
    """
    rs = _recent(recent, quotas.window)
    blocked = recent_pairs(recent, quotas.window)
    used = [s.mechanism_family for s in rs if s.mechanism_family]

    def _first_free_shape(family: str) -> str:
        """该 family 里第一个不在最近 pair 里的 shape(全撞则取第一个)。"""
        shapes = FAMILY_SHAPES.get(family, ("information_advantage",))
        for sh in shapes:
            if (family, sh) not in blocked:
                return sh
        return shapes[0]

    for fam in MECHANISM_FAMILIES:
        if fam not in used:
            return PuzzleBlueprint(mechanism_family=fam,
                                   solution_shape=_first_free_shape(fam))
    # 全都出现过 -> 用出现次数最少的那个(仍避开重复 pair)
    c = Counter(used)
    fam = min(MECHANISM_FAMILIES, key=lambda f: c.get(f, 0))
    return PuzzleBlueprint(mechanism_family=fam,
                           solution_shape=_first_free_shape(fam))


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
