#!/usr/bin/env python
# coding: utf-8
"""SummonLedger —— 点赞/礼物换算成"推理搭子召唤次数"(Step 13A)。

## 它解决什么

观众通过互动获得"召唤一次 AI 推理搭子"的额度。这个模块**只记账**:
谁贡献了多少、已消耗多少、当前被预约占用多少。它**不**决定什么时候
召唤、也**不**调用任何 AI(那是 Step 14+ 的调度器)。

## 三条不可违反的规则(已冻结)

    100 点赞 = 1 Summon
    任意真实新增礼物单位 = 1 Summon
    所有礼物同权 —— 价格/类型不参与, 不改变能力或优先级

跨题不清零、reveal 不清零。

## 为什么 Like 用 **total 的 high-water**, 不用 count 累加

`LikeMessage` 同时有 `count`(本次)与 `total`(累计)。抖音的 `total` 是
**同一 Session 内的累计值**, 而且**重连后会被重放**(实测: 一次下播变成
每秒一次的重连 + 重复弹幕)。所以:

    ✗ 用 count 累加  -> 重连重放时会重复计
    ✗ 每次 total 相加 -> 那是累计值, 相加会立刻爆炸
    ✗ session baseline 相减 -> 需要知道"本场开始时的 total", 而重连
      之后那个基线会漂

正确的是 **high-water(高水位)**:

    new_high = max(old_high, event.total)
    新增 = floor(new_high / 100) - 已消耗桶数

它天然幂等: 重复的 total 不产生新增, 倒退的 total 不产生新增(也不
rebase —— 见下面的说明)。

## 首次 total **不补历史档位**

第一次收到某场直播的 total 时, 只把它设为基线, **不**立刻结算
`floor(total/100)` —— 否则一进场就会凭空得到几百个 Summon(那些赞
是开播之前观众点的, 不属于"本次互动")。

## total 倒退不 rebase

`523 -> 320 -> 523` 是**合法**的(计数可能按窗口滚动, 或后端抖动)。
把它当成"重新开始"会让我们重复发放; 把它当成异常去 rebase 会让
high-water 失去意义。正确做法: **只取 max, 不解释倒退**。

## 礼物: 这一步**只到 raw event**

Step 12A 已确认「GiftMessage 数量 != 真实礼物单位数」(外部 Issue #88:
只送一个小心心却收到两条相同礼物消息)。所以在 Step 12B 拿到真实样本
之前, 礼物**只保留原始事件**, 绝不映射成 `earn()`。

本模块因此**不**提供任何"收到礼物就 +1"的入口 —— 这是刻意的: 让那条
错误路径在 API 上就不存在, 而不是靠注释提醒。

## reservation(预约)

调度器要"先占后用": 决定召唤时先占住 1 个额度, 失败就释放。
`reservation` 必须带 `token / round_index / spec_key / created_at`,
这样跨题/跨稿的迟到回调能被识别并丢弃(与 Step 06 的 identity 同源)。

零新依赖。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger("story.summon")

#: 多少点赞换 1 次 Summon。
LIKES_PER_SUMMON = 100


@dataclass
class Reservation:
    """一次"已经决定要召唤, 但还没结算"的占用。

    四个身份字段缺一不可:
        token        这次预约的唯一标识(释放/提交时要对得上)
        round_index  属于第几题(时序身份)
        spec_key     属于哪一稿(内容身份, 见 `puzzle.runtime_spec_key`)
        created_at   何时占用(便于排查"占着不放")

    为什么带 round + spec_key: 调度器可能在**换题之后**才收到上一题的
    迟到回调。那时若只凭 token 匹配, 一个被复用的 token 会张冠李戴。
    两个身份一起查, 跨题/跨稿的迟到就都挡得住。
    """

    token: str
    round_index: int = 0
    spec_key: str = ""
    created_at: float = 0.0
    #: 这次预约占用了几个额度(目前恒为 1, 但留成字段以免将来改语义时
    #: 要动所有调用点)。
    amount: int = 1


@dataclass
class SummonLedger:
    """召唤额度的账本。**只记账, 不调度, 不调 AI。**"""

    #: 累计获得(单调不减)。
    summon_earned_total: int = 0
    #: 累计消耗(单调不减)。
    summon_consumed_total: int = 0
    #: 当前占用中的预约(未结算)。
    detective_reservation: Optional[Reservation] = None

    # ---- Like 侧状态 ----
    #: `Like.total` 的历史最高水位。**每场直播一个**。
    likes_total_high_water: int = 0
    #: 已经结算过的"百赞档位"数(= 从点赞里已换出的 Summon 数)。
    #: 必须是**桶数**而不是原始赞数 —— 它是"我已经发过多少个了"的账。
    likes_bucket_consumed: int = 0
    #: 是否已经用第一条 total 初始化过基线。见模块 docstring。
    likes_initialized: bool = False

    #: 即时反馈用的文案序号/内容(Step 13A 只维护状态, 不做前端)。
    interaction_notice_seq: int = 0
    interaction_notice_text: str = ""

    #: 收到的 raw gift 事件数(**仅计数与保留, 绝不换算**)。
    gift_events_seen: int = 0

    # ------------------------------------------------------------------
    # 账面
    # ------------------------------------------------------------------
    @property
    def unconsumed(self) -> int:
        """已获得但尚未消耗的额度。"""
        return max(0, self.summon_earned_total - self.summon_consumed_total)

    @property
    def available(self) -> int:
        """当前**可动用**的额度(扣掉占用中的预约)。"""
        held = self.detective_reservation.amount \
            if self.detective_reservation else 0
        return max(0, self.unconsumed - held)

    @property
    def likes_progress(self) -> int:
        """距离下一次 +1 还差多少赞(`high_water % 100`)。"""
        return self.likes_total_high_water % LIKES_PER_SUMMON

    # ------------------------------------------------------------------
    # 通用赚取 / 预约 / 结算
    # ------------------------------------------------------------------
    def earn(self, n: int = 1) -> int:
        """通用入账。返回本次实际增加的额度。

        ⚠️ **礼物不要走这里** —— 在 Step 12B 之前, "一条 GiftMessage 等于
        几个真实单位"是未知的。给礼物开一条独立的、需要显式 `units` 的
        入口(将来加), 而不是让它误用这个通用口。
        """
        n = int(n or 0)
        if n <= 0:
            return 0
        self.summon_earned_total += n
        return n

    def reserve(self, token: str, round_index: int = 0, spec_key: str = "",
                amount: int = 1, now: Optional[float] = None) -> bool:
        """占住额度。成功返回 True。

        已经有占用 -> False(不覆盖: 覆盖会让前一个 token 永远释放不了,
        那笔额度就永久泄漏)。
        """
        if self.detective_reservation is not None:
            return False
        if self.available < max(1, int(amount or 1)):
            return False
        self.detective_reservation = Reservation(
            token=str(token), round_index=int(round_index or 0),
            spec_key=str(spec_key or ""),
            created_at=float(now if now is not None else time.monotonic()),
            amount=max(1, int(amount or 1)))
        return True

    def commit(self, token: str, round_index: Optional[int] = None,
               spec_key: Optional[str] = None) -> bool:
        """预约兑现(真的召唤了)。成功返回 True 并扣减 available。

        `round_index` / `spec_key` 给了就一并核对 —— 跨题/跨稿的迟到
        回调不该能兑现别人的预约。
        """
        r = self.detective_reservation
        if r is None or r.token != str(token):
            return False
        if round_index is not None and int(round_index) != r.round_index:
            return False
        if spec_key is not None and str(spec_key) != r.spec_key:
            return False
        self.summon_consumed_total += r.amount
        self.detective_reservation = None
        return True

    def release(self, token: str, round_index: Optional[int] = None,
                spec_key: Optional[str] = None) -> bool:
        """预约作废(召唤失败/被打断)。额度**退回**, 不消耗。

        身份核对与 `commit` 一致 —— 否则上一题的迟到 release 会把
        当前这题的预约误释放。
        """
        r = self.detective_reservation
        if r is None or r.token != str(token):
            return False
        if round_index is not None and int(round_index) != r.round_index:
            return False
        if spec_key is not None and str(spec_key) != r.spec_key:
            return False
        self.detective_reservation = None
        return True

    # ------------------------------------------------------------------
    # Like: high-water 换算
    # ------------------------------------------------------------------
    def on_like_total(self, total: int, now: Optional[float] = None) -> int:
        """收到一条 `Like.total`。返回本次换算出的**新增** Summon 数。

        算法(见模块 docstring 的论证):

            首次      -> 只初始化 high_water, 不补历史档位, 返回 0
            之后      -> new_high = max(old_high, total)
                         新增 = floor(new_high/100) - likes_bucket_consumed
                         并更新 likes_bucket_consumed

        幂等性: 重复 total -> new_high 不变 -> 新增 0。
        倒退不 rebase: 320 < 523 时 new_high 仍是 523 -> 新增 0。
        """
        try:
            total = int(total or 0)
        except (TypeError, ValueError):
            return 0
        if total < 0:
            return 0

        if not self.likes_initialized:
            # 首条 total 只作为基线。**不**结算历史档位 —— 那些赞是开播
            # 之前点的, 不属于本次互动。
            self.likes_initialized = True
            self.likes_total_high_water = total
            self.likes_bucket_consumed = total // LIKES_PER_SUMMON
            return 0

        if total <= self.likes_total_high_water:
            # 重复(重连重放)或倒退(合法抖动) -> 什么都不发, 也不 rebase。
            return 0

        self.likes_total_high_water = total
        buckets = total // LIKES_PER_SUMMON
        new = buckets - self.likes_bucket_consumed
        if new <= 0:
            return 0
        self.likes_bucket_consumed = buckets
        self.earn(new)
        return new

    # ------------------------------------------------------------------
    # 礼物: Step 13A 只到 raw event
    # ------------------------------------------------------------------
    def on_gift_event(self, ev: Any = None) -> int:
        """收到一个 raw gift 事件。返回**恒为 0**。

        Step 13A **不换算礼物**: Step 12A 已确认「GiftMessage 数量 != 真实
        礼物单位数」, 而真正的换算规则要等 12B 的真实样本。

        所以这里刻意:
          - 只计数(`gift_events_seen`)供观察;
          - **不**碰 `earn()`, 不看 combo/repeat/total 任何一个字段。

        返回 0 而不是 None: 调用方可以无条件把它当"本次新增额度", 将来
        12B 接上之后签名不用改。
        """
        self.gift_events_seen += 1
        return 0

    # ------------------------------------------------------------------
    # 即时反馈状态(先不做前端)
    # ------------------------------------------------------------------
    def set_notice(self, text: str) -> int:
        """更新即时反馈文案, 返回新的序号。"""
        self.interaction_notice_seq += 1
        self.interaction_notice_text = str(text or "")
        return self.interaction_notice_seq

    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        """给 UI / 排查用的一致快照。"""
        r = self.detective_reservation
        return {
            "earned": self.summon_earned_total,
            "consumed": self.summon_consumed_total,
            "unconsumed": self.unconsumed,
            "available": self.available,
            "reservation": (None if r is None else {
                "token": r.token,
                "round_index": r.round_index,
                "spec_key": r.spec_key,
                "created_at": r.created_at,
                "amount": r.amount,
            }),
            "likes_total_high_water": self.likes_total_high_water,
            "likes_bucket_consumed": self.likes_bucket_consumed,
            "likes_initialized": self.likes_initialized,
            "likes_progress": self.likes_progress,
            "gift_events_seen": self.gift_events_seen,
            "notice_seq": self.interaction_notice_seq,
            "notice_text": self.interaction_notice_text,
        }
