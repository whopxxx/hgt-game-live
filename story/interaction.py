#!/usr/bin/env python
# coding: utf-8
"""REVEALED 互动账本(Issue #60)—— 评分 #1~#5 与下一题主题投票 #a~#e。

## 设计边界(§3)

    * **薄的 deterministic ledger, 不依赖 LLM, 不做磁盘 I/O**。
      磁盘由 Director 落盘(round_closeout), Engine 保持纯状态机。
    * authoritative state 按 **user_key -> 值** 保存, 而不是只维护
      不可逆 totals —— 一人一份、可改、最后一次生效。
    * 派生量(distribution / count / sum / average / theme_totals)
      每次现算。N≤5 类、观众量级几百, 现算成本可忽略, 而第二份
      可变 totals 状态迟早与源账本分叉。
    * user_key 优先真实 user_id。**没有 id 时**(stdin / 测试)用
      `name:` 前缀的 fallback —— 可测, 但带前缀避免把两个不同真实
      uid 的"同名"观众合并(平台不会给两个真人同一个 user_id, 但
      可能给同名的)。

## 窗口语义不在账本里

窗口开放/关闭由 Engine 判(它持有 phase 与 deadline); Engine 在窗口
外**不调用** `record_*`。账本自己只保证: 收下的值按 user 覆盖。
"""

from __future__ import annotations

from typing import Any, Optional

#: 五类固定顺序 —— 平票 tie-break 用的**枚举顺序**, 不是主题优先级。
#: 与 `haiguitang_protocol.V2_CATEGORIES` 同序; 这里不 import 避免把
#: 协议模块拉进纯账本(它们都由同一个冻结决定背书, 漂移会被测试拦)。
THEME_CATEGORY_ORDER = ("logic", "suspense", "horror", "emotion",
                        "brainstorm")

#: 下一题主题的弹幕 token(精确匹配, normalize 后): `#a`..`#e`。
THEME_VOTE_CODES = ("a", "b", "c", "d", "e")

#: code -> category。`#A` 经 casefold 归一后同样命中。
THEME_CODE_TO_CATEGORY = dict(zip(THEME_VOTE_CODES, THEME_CATEGORY_ORDER))

#: 主题票的中文 label(Web UI 的 theme_options 用)。
THEME_CATEGORY_LABELS = {
    "logic": "逻辑",
    "suspense": "悬疑",
    "horror": "恐怖",
    "emotion": "情感",
    "brainstorm": "脑洞",
}


def user_key(user_id, user_name: str = "") -> str:
    """互动账本的稳定身份。

    优先真实 user_id; 没有 id 时退到 `name:<user_name>`。前缀刻意的:
    它防的是"两个无 id 的同名观众被合并"与"无 id 观众和有 id 观众
    的 key 恰好撞串" —— 后果只是评分算到同一份, 不至于把一个真人
    的分记给另一个真人。
    """
    uid = str(user_id or "").strip()
    if uid:
        return uid
    name = str(user_name or "").strip()
    return f"name:{name}" if name else "name:<anonymous>"


class RatingLedger:
    """本题评分: `user_key -> 1..5`。可改, 最后一次生效。"""

    def __init__(self) -> None:
        self._by_user: dict[str, int] = {}

    def record(self, key: str, score: int) -> bool:
        """记一票。合法(1..5)返回 True 并覆盖旧值; 非法丢弃返回 False。"""
        try:
            s = int(score)
        except (TypeError, ValueError):
            return False
        if not 1 <= s <= 5:
            return False
        self._by_user[str(key)] = s
        return True

    def stats(self) -> dict[str, Any]:
        dist = {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0}
        for s in self._by_user.values():
            dist[str(s)] += 1
        n = len(self._by_user)
        total = sum(self._by_user.values())
        return {
            "distribution": dist,
            "count": n,
            "sum": total,
            "average": (round(total / n, 3) if n else 0.0),
        }

    def clear(self) -> None:
        self._by_user.clear()

    def __len__(self) -> int:
        return len(self._by_user)


class ThemeVoteLedger:
    """下一题主题投票: `user_key -> category`。一人一票, 可改, 覆盖。"""

    def __init__(self) -> None:
        self._by_user: dict[str, str] = {}

    def record(self, key: str, category: str) -> bool:
        """记一票。只收五类枚举内的值(改票 = 覆盖)。"""
        c = str(category or "")
        if c not in THEME_CATEGORY_ORDER:
            return False
        self._by_user[str(key)] = c
        return True

    def totals(self) -> dict[str, int]:
        out = {c: 0 for c in THEME_CATEGORY_ORDER}
        for c in self._by_user.values():
            out[c] += 1
        return out

    def count(self) -> int:
        return len(self._by_user)

    def select_category(self) -> str:
        """freeze: 最高票 category; 并列按固定五类顺序取先; 无票 ""。

        ⚠️ tie-break 必须依赖**显式枚举顺序**(`THEME_CATEGORY_ORDER`),
        绝不依赖 dict/hash 迭代顺序 —— 那在 CPython 里是插入序、
        跨进程不可复现, 两次平票会选出不同主题。
        """
        totals = self.totals()
        best, best_v = "", 0
        for c in THEME_CATEGORY_ORDER:
            if totals[c] > best_v:
                best, best_v = c, totals[c]
        return best

    def is_tie(self) -> bool:
        """最高票是否由多个 category 并列取得(平票标记, 进 closeout)。"""
        totals = self.totals()
        top = max(totals.values(), default=0)
        if top <= 0:
            return False
        return sum(1 for v in totals.values() if v == top) > 1

    def clear(self) -> None:
        self._by_user.clear()

    def __len__(self) -> int:
        return len(self._by_user)
