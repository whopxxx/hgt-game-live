#!/usr/bin/env python
# coding: utf-8
"""H3-A: curated 决策账本(**谁处理过、结论是什么**)。

## 为什么需要它 —— 一个具体的浪费

H2 的判断"这道题处理过了吗"是这么做的(`compile_curated.load_done`):

    读 data/curated_pool.jsonl -> 取出里面所有 external_id -> 这些算处理过

它只知道**成功**的那些。于是:

    第 1 轮: 审了 322 条, 收 27 条 -> curated_pool 里 27 个 id
    第 2 轮: todo = 322 - 27 = 295 条
    第 2 轮又把这 295 条里**已经被拒过**的那些, 原样再审一遍

被拒的题**不会**变少 —— 它们只是没进池。所以每一轮重跑都把同样的
"AI 说不行"再烧一遍。322 条的语料按拒绝率 80% 算, 每轮白烧约 230 次
LLM 调用。

更糟的是它**反着激励**: 想省钱的唯一办法是永远别重跑。

所以真正需要的是**决策账本**: 记下每一条的结论, 无论收还是拒。

## 四种结论, 语义必须严格区分(任务书第三节)

    accepted        已经成功进入 curated_pool。终态(在本 policy 下)。
    rejected        在当前 policy 下**永久**跳过。终态。
    technical_defer 网关抖动 / 解析失败 / 写盘失败等技术问题。**可重试**。
    interrupted     为直播让路。**可重试**。

这个区分不是洁癖, 它直接决定了**会不会丢题**:

  把 timeout 记成 rejected
    -> 那条题再也不会被审, 而它可能是一道好题。一次网络抖动
       永久吃掉一道题, 而且**没有任何地方**会显示"我们丢了几道"。

  把 rejected 记成 technical_defer
    -> 每轮重审同样的垃圾, 白烧钱(回到上面那个问题)。

## 判定键: (external_id, content_hash, policy_version)

三者**全同**才算"处理过了"。任何一个变了就重审:

    policy 变了(v2 -> v3)  -> 旧结论是按旧标准下的, 不适用
    内容变了(hash 不同)    -> 原帖被编辑过, 得重新看

`content_hash` 取的是 surface+bottom 的稳定哈希 —— 这很重要: SE 帖子
可以被编辑, 而 external_id(问题号)不变。只按 id 判"处理过"会把一道
**已经被作者改过**的题当成旧题跳过, 用的是它**改之前**的结论。

## 为什么是 append-only

编译是长跑(几百次 LLM 调用, 中间可能被 Ctrl-C / 断网 / 直播抢资源)。
账本若用"读-改-写", 中途崩溃会丢掉**整份**结论 —— 于是所有题重新审,
而重审又要花钱。追加的最坏情况是丢最后一行。

同一条 key 出现多行时**以最后一行为准**(后写的覆盖先写的):
`technical_defer` 之后重试成功, 会在后面追加一行 `accepted`,
而前面那行 defer 保留 —— 那是真实的处理历史, 不该抹掉。
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

log = logging.getLogger("hgt.curated")

#: 账本默认路径。放在 `data/` 下(已 gitignore)。
DEFAULT_LEDGER = os.path.join("data", "curated_decisions.jsonl")

# ---- 四种结论。**这四个字符串是协议**, 下游按它分支, 不允许自造。 ----
ACCEPTED = "accepted"
REJECTED = "rejected"
TECHNICAL_DEFER = "technical_defer"
INTERRUPTED = "interrupted"

#: 终态集合: 在本 policy 下**不再重审**。
TERMINAL = frozenset({ACCEPTED, REJECTED})
#: 可重试集合: 下次运行时应当再试一次。
RETRYABLE = frozenset({TECHNICAL_DEFER, INTERRUPTED})
ALL_DECISIONS = frozenset({ACCEPTED, REJECTED, TECHNICAL_DEFER, INTERRUPTED})

#: §五/§六/§十四: 这些 stage 的死因是**编译期结构问题**, 不是内容判决。
#:
#: 定义放在**账本**里(而不是 lazy_curator 或 compiler): 它是**报告口径**
#: 的一部分 —— `source_quality()` 要按它把 "compile_invalid" 从
#: technical_defer 里单独数出来。三处各写一份必然漂移, 而漂移的代价是
#: 报告里的数字与实际决策语义不符。
#:
#: ⚠️ 这只是**报告标签**, 决策仍然是 `technical_defer`(可重试)。
#: §六 明确: Ledger **不增加第五种永久状态**。
COMPILE_INVALID_STAGES = frozenset({
    "validate",               # 结构硬门(与出题链同一套)
    "curated_validate",       # fair_clue 逐字 / provenance
    "post_review_validate",   # 审后结构校验
    "post_review_curated",    # 审后 curated 校验
    "reveal_adherence",       # reveal 结构没执行目标
    "too_similar",            # 与最近某题文本太像(分布问题, 非内容)
})

#: 结构问题的**文本指纹** —— 用于"reason 说是结构问题, 但 stage 更浅"
#: 的那种情况。
#:
#: ## 为什么光看 stage 不够(实测)
#:
#: `compile_one` 会记录"走到过的最深一道门"(`_deepest`), 而
#: `reject_reasons` 取的是**最后一次**尝试的原因。两者来自不同稿件时
#: 就会错配。实跑抓到一条:
#:
#:     stage  = truth_audit          (最深走到审计)
#:     reason = 第 2 条提示超过 30 字 (另一次尝试死在 hint 长度)
#:
#: 只看 stage 会把它算成"审计没过"(像内容问题), 而它**其实是**一条
#: hint 超长 —— 结构问题。分类必须读 reason 文本才能纠正。
#:
#: ⚠️ 这些指纹只在**没有更硬的内容判决理由**时才用于归类:
#: `_classify` 先让 `ai_gate` / `story_gate` / `story_review` 这些
#: **确定性内容门**胜出, 免得一个恰好提到"提示"两字的内容拒绝被误归。
COMPILE_INVALID_REASON_MARKS = (
    "提示超过",           # hint 长度
    "hints 应为",         # hint 数量
    "fair_clue",          # quote 溯源
    "completion fact",    # 合同连线
    "core hidden facts",  # 内部上限
    "没有被任何 solve_atom 引用",
    "工具调用返回空 input",
)


def looks_like_compile_invalid(stage: str, reasons: Optional[list]) -> bool:
    """这条决策的**死因**是编译期结构问题吗?

    stage 命中即算; 否则看 reasons 里有没有结构问题的文本指纹。
    见 `COMPILE_INVALID_REASON_MARKS` 的说明。
    """
    if str(stage or "") in COMPILE_INVALID_STAGES:
        return True
    for r in (reasons or []):
        s = str(r)
        if any(m in s for m in COMPILE_INVALID_REASON_MARKS):
            return True
    return False


def content_hash_of(rec: Any, *, n: int = 12) -> str:
    """一条候选记录的**内容**哈希(surface + bottom)。

    刻意不用 external_id: 那个是"哪一帖", 这是"帖子里写了什么"。
    帖子被编辑后 id 不变而内容变 —— 只有内容哈希能发现那件事。
    """
    from tools.curated_common import stable_hash
    return stable_hash(getattr(rec, "surface", ""),
                       getattr(rec, "bottom", ""), n=n)


def decision_key(external_id: str, content_hash: str,
                 policy_version: str) -> tuple:
    """判"是否处理过"的三元组。见模块 docstring。"""
    return (str(external_id or ""), str(content_hash or ""),
            str(policy_version or ""))


def make_decision(rec: Any, *, decision: str, stage: str = "",
                  reasons: Optional[list] = None,
                  policy_version: str = "",
                  style_tags: Optional[list] = None,
                  checks: Optional[dict] = None,
                  ts: Optional[float] = None) -> dict:
    """构造一行账本记录。

    ⚠️ `decision` 必须是四种之一 —— 拼错的值会让那条记录**两个集合都
    进不去**(既非终态也非可重试), 于是它既不重审也不被跳过, 行为
    取决于下游怎么读。所以这里直接拒绝非法值, 而不是"宽容接受"。

    ## `checks` —— 题型审核证据(§十二)

    Reject Audit 的取证结论: `compile` 四字段与 `Reviewer` 四字段
    **运行完就丢**。它们存在于内存, 写账本时被丢掉 —— 于是事后无法回答
    "这道题当时那四项到底判了什么"。

    现在它们作为**可选**字段落盘:

        {"compile": {...四字段...}, "review": {...四字段...}}

    ⚠️ 三条硬约束:
      1. **不进前端** —— 它只进账本, 由 `DecisionLedger` 读, 不序列化
         进 Snapshot / 不下发;
      2. **不影响 decision identity** —— `decision_key` 只读
         `(external_id, content_hash, policy_version)`, 不含 `checks`;
      3. **append-only 继续** —— 旧账本没有这个键是合法的, 读出 `{}`。
         不需要迁移(迁移会重写历史, 而历史是 append-only 的全部意义)。
    """
    if decision not in ALL_DECISIONS:
        raise ValueError(f"非法 decision: {decision!r}(必须是 {sorted(ALL_DECISIONS)})")
    return {
        "external_id": str(getattr(rec, "external_id", "") or ""),
        "source": str(getattr(rec, "source", "") or ""),
        "content_hash": content_hash_of(rec),
        "policy_version": str(policy_version or ""),
        "decision": decision,
        "stage": str(stage or ""),
        "reasons": [str(r) for r in (reasons or []) if r],
        "style_tags": [str(s) for s in (style_tags or []) if s],
        # 只留字典; 没有就写空 dict(不是 None) —— 让下游读的时候不必
        # 到处判 None。
        "checks": dict(checks) if isinstance(checks, dict) else {},
        "ts": float(ts if ts is not None else time.time()),
    }


# ======================================================================
# 读写
# ======================================================================
def append_decision(path: str, rec_decision: dict) -> bool:
    """追加一行(flush + fsync, 不留半截)。

    与 `compile_curated._append_jsonl` 同样的理由: 长跑中途被杀时,
    "重写整份"会丢掉全部历史, "追加"最坏只丢最后一行。
    """
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec_decision, ensure_ascii=False,
                               sort_keys=True, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return True
    except OSError as e:
        log.error("写决策账本失败 %s: %s", path, e)
        return False


def read_decisions(path: str) -> list:
    """读全部决策行(按写入顺序)。坏行跳过并计数, 不抛。"""
    out: list = []
    if not path or not os.path.exists(path):
        return out
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    # 半截行(上次被杀在写盘中间) -> 跳过。它只可能出现在
                    # **最后一行**, 而且丢它是安全的: 那条题下次会重审。
                    continue
                if isinstance(d, dict):
                    out.append(d)
    except OSError as e:
        log.warning("读决策账本失败 %s: %s", path, e)
    return out


def index_decisions(rows: list) -> dict:
    """把决策行折成 `{(id, hash, policy): 最后一条决策}`。

    **最后一条为准**: 同一 key 可以有多行(先 defer 后 accepted)。
    后写的是更新的结论。
    """
    idx: dict = {}
    for d in rows:
        k = decision_key(d.get("external_id", ""), d.get("content_hash", ""),
                         d.get("policy_version", ""))
        idx[k] = d
    return idx


class DecisionLedger:
    """决策账本的门面。**这是"处理过没有"的唯一权威**。

    用法:

        led = DecisionLedger("data/curated_decisions.jsonl")
        if led.is_settled(rec, CURATED_POLICY_VERSION):
            continue                      # 审过了, 不烧 LLM
        ...
        led.record(rec, decision=ACCEPTED, policy_version=..., stage=...)
    """

    def __init__(self, path: str = DEFAULT_LEDGER):
        self.path = path
        self._idx: dict = {}
        self.rows: list = []
        self.reload()

    # ------------------------------------------------------------------
    def reload(self) -> None:
        self.rows = read_decisions(self.path)
        self._idx = index_decisions(self.rows)

    def last(self, rec: Any, policy_version: str) -> Optional[dict]:
        """这条记录在当前 policy 下的**最后一条**决策(没有则 None)。"""
        k = decision_key(getattr(rec, "external_id", ""),
                         content_hash_of(rec), policy_version)
        return self._idx.get(k)

    def is_settled(self, rec: Any, policy_version: str) -> bool:
        """是否**已终结**(accepted/rejected) -> 不必再审。

        注意 `technical_defer` / `interrupted` **不**算终结: 它们
        明确是"下次再来"。这正是这个类存在的主要理由。
        """
        d = self.last(rec, policy_version)
        return bool(d) and d.get("decision") in TERMINAL

    def is_accepted(self, rec: Any, policy_version: str) -> bool:
        d = self.last(rec, policy_version)
        return bool(d) and d.get("decision") == ACCEPTED

    # ------------------------------------------------------------------
    def record(self, rec: Any, *, decision: str, policy_version: str,
               stage: str = "", reasons: Optional[list] = None,
               style_tags: Optional[list] = None,
               checks: Optional[dict] = None) -> bool:
        """写一条决策并**立即**更新内存索引。

        先落盘再更新内存: 反过来的话, 写盘失败时内存会以为"审过了",
        于是一次磁盘故障就让那道题在本轮被静默跳过 —— 而下游(是否
        入池)已经发生了。顺序必须是"盘上先成立"。

        `checks` 是**可选**的审计证据(§十二), 不影响 decision identity。
        """
        d = make_decision(rec, decision=decision, stage=stage,
                          reasons=reasons, policy_version=policy_version,
                          style_tags=style_tags, checks=checks)
        if not append_decision(self.path, d):
            return False
        self.rows.append(d)
        self._idx[decision_key(d["external_id"], d["content_hash"],
                               d["policy_version"])] = d
        return True

    # ------------------------------------------------------------------
    def stats(self, policy_version: str) -> dict:
        """当前 policy 下的分类计数(供启动 banner)。

        只统计**每个 key 的最后一条**, 不是所有行 —— 否则一道"先 defer
        后 accepted"的题会被同时算进两个桶。
        """
        counts = {ACCEPTED: 0, REJECTED: 0, TECHNICAL_DEFER: 0,
                  INTERRUPTED: 0}
        for (eid, _h, pol), d in self._idx.items():
            if pol != policy_version:
                continue
            dec = d.get("decision")
            if dec in counts:
                counts[dec] += 1
        return counts

    def settle_stats(self, policy_version: str) -> dict:
        """按 stage / reasons 统计拒绝原因(供验收报告)。"""
        by_stage: dict = {}
        by_reason: dict = {}
        for (_eid, _h, pol), d in self._idx.items():
            if pol != policy_version:
                continue
            if d.get("decision") != REJECTED:
                continue
            st = str(d.get("stage") or "unknown")
            by_stage[st] = by_stage.get(st, 0) + 1
            for r in (d.get("reasons") or ["unknown"]):
                slug = str(r).split(":")[0].strip()[:40] or "unknown"
                by_reason[slug] = by_reason.get(slug, 0) + 1
        return {"by_stage": by_stage, "by_reason": by_reason}

    def source_quality(self, policy_version: str) -> dict:
        """§十四: source yield 必须拆成**几个不同的数字**, 不能只报
        `accepted / processed`。

        ## 为什么这一个函数很要紧

        "12.5% 通过率"那个数字把**三种完全不同的东西**混成了一个:

            源的内容真的不行          -> content_rejected
            网关抖了 / schema 坏了    -> technical_defer(其中 compile_invalid)
            直播抢资源                -> interrupted

        把它们混起来算"源质量差"是**统计错误**: 一次网关抖动会让 yield
        看起来掉一半, 而源本身没变。Reject Audit 正是卡在这里 ——
        30 条 rejected 里有 6 条其实是编译接线问题, 不是内容判决。

        `compile_invalid` **包含在** `technical_defer` 里(决策仍是可重试),
        但**单独统计** —— 否则"这一类到底有多少"永远看不见。

        真正的 source yield:

            accepted / (accepted + content_rejected)

        —— 分母里**不含** defer / interrupted。
        """
        counts = {ACCEPTED: 0, REJECTED: 0, TECHNICAL_DEFER: 0,
                  INTERRUPTED: 0}
        compile_invalid = 0
        by_stage: dict = {}
        for (_eid, _h, pol), d in self._idx.items():
            if pol != policy_version:
                continue
            dec = d.get("decision")
            if dec not in counts:
                continue
            counts[dec] += 1
            st = str(d.get("stage") or "unknown")
            by_stage[st] = by_stage.get(st, 0) + 1
            if dec == TECHNICAL_DEFER and looks_like_compile_invalid(
                    st, d.get("reasons")):
                compile_invalid += 1
        acc = counts[ACCEPTED]
        crej = counts[REJECTED]
        denom = acc + crej
        return {
            "processed": sum(counts.values()),
            "accepted": acc,
            "content_rejected": crej,
            "technical_defer": counts[TECHNICAL_DEFER],
            "compile_invalid": compile_invalid,
            "interrupted": counts[INTERRUPTED],
            "by_stage": by_stage,
            # 真正的源 yield —— 分母**不含** defer / interrupted。
            "source_yield": (acc / denom) if denom else None,
        }
