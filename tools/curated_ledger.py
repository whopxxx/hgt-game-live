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
                  ts: Optional[float] = None) -> dict:
    """构造一行账本记录。

    ⚠️ `decision` 必须是四种之一 —— 拼错的值会让那条记录**两个集合都
    进不去**(既非终态也非可重试), 于是它既不重审也不被跳过, 行为
    取决于下游怎么读。所以这里直接拒绝非法值, 而不是"宽容接受"。
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
               style_tags: Optional[list] = None) -> bool:
        """写一条决策并**立即**更新内存索引。

        先落盘再更新内存: 反过来的话, 写盘失败时内存会以为"审过了",
        于是一次磁盘故障就让那道题在本轮被静默跳过 —— 而下游(是否
        入池)已经发生了。顺序必须是"盘上先成立"。
        """
        d = make_decision(rec, decision=decision, stage=stage,
                          reasons=reasons, policy_version=policy_version,
                          style_tags=style_tags)
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
