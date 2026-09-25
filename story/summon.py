#!/usr/bin/env python
# coding: utf-8
"""SummonLedger —— 点赞/礼物换算成"推理搭子召唤次数"(Step 13A)。

## 它解决什么

观众通过互动获得"召唤一次 AI 推理搭子"的额度。这个模块**只记账**:
谁贡献了多少、已消耗多少、当前被预约占用多少。它**不**决定什么时候
召唤、也**不**调用任何 AI(那是 Step 14+ 的调度器)。

## 三条不可违反的规则(已冻结)

    100 点赞 = 1 个"点赞推进 pulse"(Issue #43)
    任意真实新增礼物单位 = 1 Summon
    所有礼物同权 —— 价格/类型不参与, 不改变能力或优先级

## Issue #43: session / round 两级账

一个 pulse 同时等价于两样东西, 而**两样都是"当前题"资源**:

    AI 玩家当前题行动机会 +1
    当前题有效游戏时间推进(秒数由 Engine 决定, 本模块不涉及)

所以账本分两层:

    session 级(跨题**不清零**):
        likes_total_high_water / likes_bucket_consumed / likes_initialized
        lifetime_pulses_total(遥测)
        高水位清了的话, 换题/重连会把旧赞按新题重新算一遍 —— 重复结算。
    round 级(新题开始时 `start_new_round()` 清零):
        summon_earned_total / summon_consumed_total / reservation

`on_like_total` 只负责高水位换算并**返回** pulse 数; 是否入账当前题由
调用方(Engine)按 phase 决定(SETTING/QA 入账; REVEALING/REVEALED/IDLE
只推进高水位与遥测)。本模块不认识 phase, 所以绝不替调用方做这个决定。

## 为什么 Like 用 **total 的 high-water**, 不用 count 累加

`LikeMessage` 同时有 `count`(本次)与 `total`(累计)。

## 证据强度(不要升级这段表述)

真实采集**只完成了单用户、无重连**的一小段(6 条样本), 在那段里观察到:

    total 严格递增, count 表现为批次增量, sum(count) == 最后 total

**尚未确认**:

    - `total` 是全房间累计还是单用户累计(两种解释都兼容那 6 条)
    - 重连时的 replay / reset 行为(那段样本里没有重连)

所以下面选 high-water 是**当前保守的实现策略, 待 12B 验证**,
不是"已被实测证明的唯一正确算法"。它之所以仍然更安全, 是因为:

    ✗ 用 count 累加       -> 若重连会重放, 就会重复计
    ✗ 每次 total 相加     -> total 是累计值, 相加会立刻爆炸
    ✗ session baseline 相减 -> 需要"本场开始时的 total", 重连后基线会漂

这三个风险**现在都还没被排除**, 所以选一个对它们都天然免疫的算法 ——
即使将来证明"其实不会重放", high-water 也只是白保守, 不会算错。

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

`523 -> 320 -> 523` 我们**只取 max, 不解释倒退**。

这是**保守选择**: 倒退在当前样本里没出现过, 它到底是"按窗口滚动"
还是"后端抖动"还是"合法的场次重置"**尚未实测**。把任一猜测写进代码
都有代价 —— 当成"重新开始"会重复发放; 当成异常去 rebase 会让
high-water 失去意义。只取 max 对这三种可能都安全。

(若 12B 证明存在**合法的场次重置**, 那时再加一个显式的重置语义,
而不是靠"倒退就 rebase"这种隐式行为。)

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
    round_index: int
    spec_key: str
    created_at: float = 0.0
    # ⚠️ 刻意**没有** `amount`: 一次召唤恒占 1 个额度。
    #
    # 早先留了 amount 字段(且 reserve/commit 都接受), 于是一次预约可以
    # 消费多个 Summon —— 那与"一次召唤 = 1"的冻结语义不符, 而且让
    # available 的计算多出一个可以配错的旋钮。要改这个语义应当是一次
    # 显式的设计决定, 不是留一个参数等着被误用。


@dataclass
class SummonLedger:
    """召唤额度的账本。**只记账, 不调度, 不调 AI。**

    字段刻意按 session / round 分组排列 —— 见模块 docstring 的两级账。
    """

    # ---- session 级(跨题/揭晓**不清零**) ----
    #: `Like.total` 的历史最高水位。**每场直播一个**。
    likes_total_high_water: int = 0
    #: 已经结算过的"百赞档位"数(= 已经产生过的 pulse 总桶数)。
    #: 必须是**桶数**而不是原始赞数 —— 它是"我已经发过多少个了"的账。
    likes_bucket_consumed: int = 0
    #: 是否已经用第一条 total 初始化过基线。见模块 docstring。
    likes_initialized: bool = False
    #: 遥测: 本场一共产生过多少 pulse(单调不减)。round 级字段清零时
    #: 它不受影响 —— 赛后对账"这场观众贡献了多少推进"靠它。
    lifetime_pulses_total: int = 0

    # ---- round 级(新题开始即清零, 见 `start_new_round`) ----
    #: 本题已获得的 AI 玩家行动机会(= 本题入账的 pulse 数)。
    summon_earned_total: int = 0
    #: 本题已消耗。
    summon_consumed_total: int = 0
    #: 当前占用中的预约(未结算)。reservation 本身带 round + spec_key,
    #: 跨题迟到回调在 Engine 层被身份校验挡下; `start_new_round` 会把
    #: 上一题的预约就地失效。
    detective_reservation: Optional[Reservation] = None

    # ---- 即时反馈 ----
    #: 即时反馈用的文案序号/内容(本模块只维护状态, 不做前端)。
    interaction_notice_seq: int = 0
    interaction_notice_text: str = ""

    #: 收到的 raw gift 事件数(**仅计数与保留, 绝不换算**)。
    gift_events_seen: int = 0

    # ------------------------------------------------------------------
    # round 生命周期
    # ------------------------------------------------------------------
    def start_new_round(self) -> None:
        """新题开始(Engine 进入 SETTING 时调用): **round 级资源清零**。

        清:
            summon_earned_total / summon_consumed_total
                上一题没用完的 AI opportunity 就地作废 —— 它是"当前题
                资源", 不带进下一题(Issue #43 §4)。
            detective_reservation
                上一题的预约就地失效(它的额度属于上一题, 不存在"退回"
                到哪里的说法)。

        绝不清(清了就是 Issue #43 点名要防的重复结算):
            likes_total_high_water / likes_bucket_consumed /
            likes_initialized —— session 级高水位。
            lifetime_pulses_total —— 遥测, 单调。
        """
        self.summon_earned_total = 0
        self.summon_consumed_total = 0
        self.detective_reservation = None

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
        # 一次召唤恒占 1 —— 见 `Reservation` 上关于 amount 的说明。
        held = 1 if self.detective_reservation else 0
        return max(0, self.unconsumed - held)

    @property
    def likes_progress(self) -> int:
        """当前进度的**计数**(`high_water % 100`), 即已攒到第几赞。

        例如 high_water=523 -> 23, 表示距离下一次 +1 还差 77 赞。
        (早先注释写成"还差多少赞", 与返回值不符。)"""
        return self.likes_total_high_water % LIKES_PER_SUMMON

    # ------------------------------------------------------------------
    # 通用赚取 / 预约 / 结算
    # ------------------------------------------------------------------
    def earn(self, n: int = 1) -> int:
        """往**当前题**入账 n 个 AI 玩家行动机会。返回本次实际入账数。

        ⚠️ **礼物不要走这里** —— 在 Step 12B 之前, "一条 GiftMessage 等于
        几个真实单位"是未知的。给礼物开一条独立的、需要显式 `units` 的
        入口(将来加), 而不是让它误用这个通用口。

        ⚠️ Issue #43: 这是 round 级入账 —— Engine 只在 SETTING/QA 消费
        pulse 时调用; 新题开始时 `start_new_round()` 会把它清零。
        """
        n = int(n or 0)
        if n <= 0:
            return 0
        self.summon_earned_total += n
        return n

    def reserve(self, token: str, round_index: int, spec_key: str,
                now: Optional[float] = None) -> bool:
        """占住 1 个额度。成功返回 True。

        ## 三个身份字段**全部必填且非空**

        冻结的契约是 `token + round_index + spec_key` **三者一致**才算
        同一次预约 —— 不是"调用方愿意传就检查"。

        早先的写法是 `round_index: int = 0, spec_key: str = ""`, 且
        commit/release 只在"传了"时才核对。那会让 Step 14 的迟到回调只要
        **token 撞上**就能兑现或释放当前预约 —— 而 token 是调用方自己生成
        的字符串, 撞上并非不可能。空 spec_key 更糟: 它让"哪一稿"这一维
        整条失效, 跨稿的迟到回调完全挡不住。

        所以这里:
          - 三个参数都是**位置必填**(没有默认值, 传漏了是 TypeError);
          - 值为空(空串)同样拒绝 —— 必填但传空等于没填。
        """
        token = str(token or "").strip()
        spec_key = str(spec_key or "").strip()
        if not token or not spec_key:
            log.warning("拒绝预约: token/spec_key 不能为空")
            return False
        # round_index 同样必填, 且必须是真整数 —— `None` 不是"第 0 题",
        # 传 None 说明调用方没有身份信息, 一律拒绝(不能 int(None) 崩掉)。
        if not isinstance(round_index, int) or isinstance(round_index, bool):
            log.warning("拒绝预约: round_index 必须是整数(收到 %r)",
                        round_index)
            return False
        if self.detective_reservation is not None:
            # 不覆盖: 覆盖会让前一个 token 永远释放不了, 那笔额度永久泄漏。
            return False
        if self.available < 1:
            return False
        self.detective_reservation = Reservation(
            token=token, round_index=int(round_index),
            spec_key=spec_key,
            created_at=float(now if now is not None else time.monotonic()))
        return True

    def commit(self, token: str, round_index: int, spec_key: str) -> bool:
        """预约兑现(真的召唤了)。成功返回 True 并消耗 1 个额度。

        三个身份字段**必须全部匹配** —— 见 `reserve` 的说明。
        只匹配 token 就兑现, 是"迟到回调兑现了别人的预约"那条路。
        """
        r = self.detective_reservation
        if r is None:
            return False
        if not self._identity_matches(r, token, round_index, spec_key):
            return False
        self.summon_consumed_total += 1
        self.detective_reservation = None
        return True

    def release(self, token: str, round_index: int, spec_key: str) -> bool:
        """预约作废(召唤失败/被打断)。额度**退回**, 不消耗。

        身份核对与 `commit` **完全一致** —— 否则上一题/上一稿的迟到
        release 会把当前这题的预约误释放。
        """
        r = self.detective_reservation
        if r is None:
            return False
        if not self._identity_matches(r, token, round_index, spec_key):
            return False
        self.detective_reservation = None
        return True

    @staticmethod
    def _identity_matches(r: "Reservation", token, round_index, spec_key) -> bool:
        """预约身份三要素是否**全部**匹配。

        抽成一个函数, 是因为 `commit` 与 `release` 必须用**同一套**判据 ——
        两份手写的比较迟早会漂移, 而漂移的方向必然是其中一处变松。

        ⚠️ `round_index` 必须是**真整数**: `reserve()` 拒绝 `True`
        (bool 是 int 的子类, 但"第 True 题"没有意义), 而这里早先用
        `int(round_index)` —— `int(True) == 1`, 于是 `True` 能匹配第 1 题。
        同一份契约在两个入口判定不一致, 就是漏洞。这里用与 reserve 完全
        相同的判据。
        """
        if not isinstance(round_index, int) or isinstance(round_index, bool):
            return False
        if not isinstance(r.round_index, int) or isinstance(r.round_index, bool):
            return False
        try:
            return (r.token == str(token or "").strip()
                    and round_index == r.round_index
                    and r.spec_key == str(spec_key or "").strip())
        except (TypeError, ValueError):
            return False

    # ------------------------------------------------------------------
    # Like: high-water 换算
    # ------------------------------------------------------------------
    def on_like_total(self, total: int, now: Optional[float] = None) -> int:
        """收到一条 `Like.total`。返回本次产生的**新增 pulse 数**。

        算法(见模块 docstring 的论证):

            首次      -> 只初始化 high_water, 不补历史档位, 返回 0
            之后      -> new_high = max(old_high, total)
                         新增 = floor(new_high/100) - likes_bucket_consumed
                         并更新 likes_bucket_consumed

        幂等性: 重复 total -> new_high 不变 -> 新增 0。
        倒退不 rebase: 320 < 523 时 new_high 仍是 523 -> 新增 0。

        ⚠️ **返回 ≠ 入账**(Issue #43 §4): 返回值只是"产生了几个 pulse"
        这个会话级事实(高水位/桶数/遥测已在此记下)。往**当前题**入账
        是调用方(Engine)的职责 —— 只有 SETTING/QA 该入账; REVEALING /
        REVEALED / IDLE 必须只保留遥测、不得带入任何一题。
        本方法因此**绝不**自己调 `earn()`。
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
        # session 级遥测: 无论哪个 phase, "观众贡献了多少推进"都要累计。
        self.lifetime_pulses_total += new
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
            # round 级
            "earned": self.summon_earned_total,
            "consumed": self.summon_consumed_total,
            "unconsumed": self.unconsumed,
            "available": self.available,
            "reservation": (None if r is None else {
                "token": r.token,
                "round_index": r.round_index,
                "spec_key": r.spec_key,
                "created_at": r.created_at,
            }),
            # session 级
            "likes_total_high_water": self.likes_total_high_water,
            "likes_bucket_consumed": self.likes_bucket_consumed,
            "likes_initialized": self.likes_initialized,
            "likes_progress": self.likes_progress,
            "lifetime_pulses_total": self.lifetime_pulses_total,
            "gift_events_seen": self.gift_events_seen,
            "notice_seq": self.interaction_notice_seq,
            "notice_text": self.interaction_notice_text,
        }
