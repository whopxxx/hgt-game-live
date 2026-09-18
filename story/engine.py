#!/usr/bin/env python
# coding: utf-8
"""海龟汤回合状态机 —— 纯函数, 时钟注入。

设计要点(与旧「故事+投票」的根本区别):
    - **没有 COLLECTING/TALLYING**: 提问随到随答, 没有"窗口"要开要关。
      QA 是 ~95% 时间的稳态。
    - **逐条秒回**: 1 条提问 = 1 次 LLM 调用。并发上限 qa_max_inflight(默认5),
      超出排队而非丢弃。
    - **猜中 = 裁决 '揭晓'**: 不额外调 LLM 判定。
    - **冷场两层**: 空闲 -> LLM 提示; 再空闲 -> 零成本重述谜面。
    - 引擎**不做任何 I/O**: tick(now) 是 phase 的唯一写入者, 动作以
      list[EngineAction] 返回, 由 director 落到线程池上执行。

线程契约(沿用旧引擎的硬规则):
    - tick() 只由唯一调度线程调用 -> 是 phase 的唯一写入者
    - submit_danmaku() 只由消费线程调用, 只加锁追加, **返回 []**
      (动作只由 tick 发出, 这样不和调度线程抢)
    - submit_*() 由 LLM worker 线程调用
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Optional

from .config import Config
from . import parser as P
from .puzzle import PuzzleSignature, PuzzleSpec
from .state import (CMD_PREFIX, HINT_TOKENS, NEXT_TOKENS, ActionKind,
                    DanmakuItem, EngineAction, PendingQ, QARec, QAResult,
                    Phase, Snapshot)

log = logging.getLogger("story.engine")

# 比 DEBUG 更低: 只写文件, 不刷控制台。记**每条弹幕的去向**这类明细。
DETAIL = 5
logging.addLevelName(DETAIL, "DETAIL")


def _detail(msg: str, *args) -> None:
    if log.isEnabledFor(DETAIL):
        log.log(DETAIL, msg, *args)


def _atom_dict(a: Any) -> Any:
    """把 solve_atom 归一成 dict。

    接受 `SolveAtom`(有 to_dict)、dict、以及老数据里的纯字符串。
    Engine 内部只认 dict —— 见 `submit_riddle` 里的说明。
    """
    if hasattr(a, "to_dict"):
        return a.to_dict()
    if isinstance(a, dict):
        return dict(a)
    return a


def _clue_dict(c: Any) -> Any:
    """把 fair_clue 归一成 dict(同上)。"""
    if hasattr(c, "to_dict"):
        return c.to_dict()
    if isinstance(c, dict):
        return dict(c)
    return c


# 空闲重述时轮换的引导语(零成本, 让冷场画面"呼吸")
_NUDGES = (
    "有人想问什么吗？发送 #你的问题 向我提问",
    "谜面里的每个细节都可能是线索",
    "想到了就直接说出你的答案，猜中我就揭晓",
    "别怕猜错，问错方向也没关系",
)

# 非 QA 阶段收到 `#问题` 时的反馈文案(方案 §8.1)。
#
# 为什么按 phase 分开: 观众的困惑不一样 ——
#   SETTING   出题要 30-45s(含重试/审稿), 最容易被当成卡死
#   REVEALING 正在生成揭晓措辞, 很短
#   REVEALED  刚揭晓, 有人会接着追问, 而其实该等下一题
# IDLE / STOPPED **不在表里**: 还没开始 / 已结束, 反馈没有意义。
#
# 这些都是 0 成本确定性文案, 不调 LLM。
_ACK_BY_PHASE = {
    Phase.SETTING: "正在准备新题，谜面出现后再发 #问题。",
    Phase.REVEALING: "本题正在揭晓，稍后开启下一题。",
    Phase.REVEALED: "本题已结束，下一题即将开始。",
}


class RoundEngine:
    """海龟汤引擎。纯逻辑, 无 I/O。"""

    def __init__(self, cfg: Config, clock: Callable[[], float] = time.monotonic):
        self.cfg = cfg
        self._clock = clock
        self._lock = threading.RLock()

        self.phase = Phase.IDLE
        self._stopped = False

        # ---- 谜题 ----
        self._puzzle = ""
        self._answer = ""
        self._title = ""
        self._puzzle_index = 0
        self._puzzle_started: Optional[float] = None
        self._used_titles: list[str] = []
        self._hint_pool: list[str] = []      # AI 出题时附带的提示(备用)
        # 出题时定下的原子事实 —— 传给裁判, 让"说中了几条"有据可依
        self._solve_atoms: list = []
        # 公平线索(quote + supports_atoms) —— 一路带到 archive
        self._fair_clues: list = []
        # 最近 N 题的**可比较指纹**, 用于跨题配额与结构去重(方案 §10)。
        # 刻意不放进 Snapshot: 它是内部数据, 前端不需要也不该看到。
        self._recent_signatures: list = []
        # 观众群体**碰到过**哪些 fact(方案 §32)。判断提示方向时用。
        # 刻意叫 touched 不叫 discovered —— 问过 ≠ 确认为真。
        self._touched_fact_ids: set = set()
        self._candidate_count = 0        # 被标为"完整解释尝试"的提问数
        # 当前这题的完整 spec(方案 §49 的收拢方向; 现阶段与上面几个
        # 平铺字段并存, 供 archive 用)
        self._spec: Optional[PuzzleSpec] = None
        #: 这道题的来源(见 submit_riddle)。默认现场生成。
        self._spec_source = "live_generate"
        self._hints_shown: list[str] = []    # **实际展示过**的提示文本
        self._hints_given = 0
        # 失败重试(第三轮 review P1)。语义:
        #   `_hints_given` = 已**成功上屏**的提示数;
        #   `_hint_pending` = 已发出请求、还没回调的槽位;
        #   `_hint_retry_at` = 上一次失败后, 允许再次尝试的时刻。
        # 早先 _hints_given 在**发出请求**时就 +1, 而 worker 失败时
        # 什么都不提交 -> 那个槽位白白损失, 观众少一条提示。
        self._hint_pending = False
        self._hint_retry_at = 0.0
        self._hint_text = ""
        self._setting_attempts = 0
        self._setting_deadline: Optional[float] = None

        # ---- 揭晓 ----
        self._solved = False
        self._solved_by = ""
        self._revealed = ""
        self._reveals = 0
        self._reveal_deadline: Optional[float] = None
        self._reveal_pending_reason = ""
        self._next_puzzle_deadline: Optional[float] = None
        self._reveal_timeout: Optional[float] = None

        # ---- 问答 ----
        self._qid_seq = 0
        self._pending: list[PendingQ] = []
        self._inflight: dict[int, PendingQ] = {}
        self._inflight_at: dict[int, float] = {}
        self._history: list[QARec] = []      # 喂回 LLM
        self._qa_log: list[QARec] = []       # 上屏(尾窗, 会截断)
        #: Step 09: 本题**完整**的问答存档(append-only, 永不截断)。
        #: `qa_log` 是 UI 尾窗, `qa_archive` 是分析用的全量。
        self._qa_archive: list[QARec] = []
        self._qa_total = 0
        self._verdict_counts: dict[str, int] = {}
        self._last_ask: dict[tuple[str, str], float] = {}   # 去重
        self._last_activity = 0.0
        self._restate_at = 0.0
        self._restate_n = 0
        self._last_manual_hint = 0.0        # 观众 #提示 的节流
        self._last_ack_at = float("-inf")   # 非 QA 阶段 #问题 的 ACK 节流

        # ---- 弹幕 / 观众 ----
        self._danmaku: list[DanmakuItem] = []
        self._danmaku_cap = 60
        self._dm_seq = 0                    # 弹幕全局序号(单调递增)
        # ---- Q12: 重放识别 ----
        # 主路径: 按平台 msg_id 精确去重(有 ID 时)。
        # 降级路径: 没有 ID 时, 只在 reconnect guard 窗口内、且与最近历史
        #   **大量精确重复**时才抑制(见 `_is_replay_locked`)。
        #
        # 早先的"1.5s 内 3 条 -> 整批丢弃 + 压制全场 5 秒"已删除:
        # 它假设"真人不可能 1.5 秒连发 3 条", 而 40 人房间看到关键提示时
        # 1.5 秒 3 条完全可能是真互动 —— 直播越热误伤越多。
        self._seen_msg_ids: "OrderedDict[str, float]" = OrderedDict()
        self._msg_id_cap = max(1, int(getattr(cfg, "msg_id_cache_size", 2000) or 2000))
        self._guard_until = 0.0             # replay guard 到期时刻
        # guard 的三个内部状态(见 `_is_replay_locked`):
        #   baseline  —— guard 开启瞬间快照的近期指纹(**冻结**, 防自我增强)
        #   pending   —— 疑似但未确认的消息缓冲(确认前不上屏)
        #   streak    —— incoming 连续命中 baseline 的条数
        #   confirmed —— 已达阈值, 窗口内后续同类一律丢弃
        self._guard_baseline: set = set()
        self._guard_pending: list = []
        self._guard_streak = 0
        self._guard_confirmed = False
        #: guard 缓冲放行时产生、待交付给调用方的动作(Q12c)。
        #: 见 `submit_danmaku` 里"降级路径"那段 —— 丢掉它就等于把
        #: 缓冲里 `#提示`/`#下一题` 的动作静默吞了。
        self._released_acts: list[EngineAction] = []
        self._replays = 0                   # 判定为重放并抑制的条数
        self._dup_ids = 0                   # 因 msg_id 重复而丢弃的条数
        self._viewers: set[str] = set()

        # ---- 统计 ----
        self.round_index = 0                 # 兼容: == 已出题数
        self._questions_total = 0
        self._answered_total = 0
        self._solved_total = 0
        self._dropped = 0

        # ---- 调试 ----
        self.last_usage: Optional[dict] = None
        self.model: Optional[str] = None
        self.model_requested: Optional[str] = None
        self.last_error: Optional[str] = None
        self.source = ""
        self.reconnects = 0
        self.reconnect_fails = 0            # 连续重建失败次数(0=正常)

        self._notice = ""
        self._phase_hint = ""

    # ==================================================================
    # 生命周期
    # ==================================================================
    def start(self, now: Optional[float] = None) -> list[EngineAction]:
        now = self._now(now)
        with self._lock:
            if self._stopped or self.phase != Phase.IDLE:
                return []
            self._last_activity = now
            return self._enter_setting_locked(now, reason="riddle")

    def stop(self, reason: str = "") -> list[EngineAction]:
        with self._lock:
            if self._stopped:
                return []
            self._stopped = True
            self.phase = Phase.STOPPED
            self._notice = reason or "已停止"
            log.info("引擎停止: %s", self._notice)
            return [EngineAction(ActionKind.BROADCAST, {
                "notice": self._notice, "phase_changed": True})]

    def on_stream_ended(self) -> list[EngineAction]:
        return self.stop("直播已结束，感谢观看")

    def on_disconnect(self) -> None:
        with self._lock:
            self.reconnects += 1
            self._notice = "弹幕连接中断，正在重连…"

    def on_reconnect(self) -> None:
        """**已经确认发生了一次真实重连** -> 立刻冻结 baseline、开 guard。

        ## 语义很纯: 调用它 == 重连已确认

        **首次建连的过滤是传输层(`LiveSource`)的责任**, 不在引擎这层。
        早先引擎也留了一道 `_ever_connected` 判断, 理由是"第二道保险" ——
        但组合起来**不是保险, 是双重消耗**:

            连接#1  LiveSource 吞掉            -> 引擎根本没收到
            连接#2  LiveSource 放行 -> 引擎又当首次吞掉
            连接#3  guard 才真正开

        也就是第一次真实重连被白白吃掉了。所以那层判断已删除。
        `LiveSource` 掌握真实的 connection epoch, 只有它有资格判断
        "首次还是重连"。

        ## guard 的作用

        抖音是在重连之后把那批旧弹幕原样重发的, 所以"该防重放"这件事
        只在这个窗口内成立。出了窗口, 哪怕内容真的一模一样, 也当真人
        发言 —— 一个人在几分钟后一字不差又说一遍, 本来就更可能是真的
        又问了。

        只影响**降级路径**(没有 msg_id 的消息)。有 ID 的走精确去重,
        任何时候都能识别, 不依赖这个窗口。
        """
        now = self._now(None)
        with self._lock:
            self._notice = "弹幕已重连"
            self._guard_until = now + self.cfg.replay_guard_seconds
            # baseline 在此**冻结**: 重放要和"重连前收到的那些"比。
            # 冻结是必须的 —— 否则重放的前几条写进 `_danmaku` 之后,
            # 后面的就能匹配到刚写进去的自己, 阈值形同虚设。
            self._guard_baseline = {
                (str(d.user_id), P.simplify_for_dedupe(d.content, 200))
                for d in self._danmaku
            }
            self._guard_pending = []
            self._guard_streak = 0
            self._guard_confirmed = False
            _detail("重放 guard 开启 %.0fs(至 %.0f), baseline %d 条",
                    self.cfg.replay_guard_seconds, self._guard_until,
                    len(self._guard_baseline))

    # ==================================================================
    # 输入: 弹幕(ws 线程 -> 消费线程)
    # ==================================================================
    def submit_danmaku(self, user_id, user_name: str, content: str,
                       now: Optional[float] = None,
                       message_id: str = "") -> list[EngineAction]:
        """弹幕入口。先过**重放识别**, 再交给 `_accept_danmaku` 真正处理。

        为什么需要重放识别: 实测抖音在**重连后会把之前整批弹幕原样重发**
        (11 条挤在同一秒, 内容和一分钟前那批一模一样)。那批重放会:
          - 在弹幕轨道上又刷一遍(看着像乱飞)
          - 被当成新提问再答一遍(重复回答)

        ## Q12: 两条路径(分层降级)

        **主路径 —— 有 `message_id` 就精确去重。** 平台消息 ID 是唯一的,
        所以"同一条消息又来一遍"能被**精确**识别, 不看时间、不看密度、
        不看内容。真人爆发(40 人同时刷)永不误杀, 因为每条 ID 都不同。

        **降级 —— 没有 ID 时(或上游不给)**: 只在 **reconnect guard**
        窗口内, 且与最近历史**大量精确重复**时才抑制(见 `_is_replay_locked`)。
        单纯"来得很密"不再构成重放证据 —— 那正是旧机制误杀真人的原因。
        """
        now = self._now(now)
        with self._lock:
            if self._stopped:
                _detail("弹幕丢弃[已停播] %s: %s", user_name, content[:30])
                return []
            # ---- 主路径: msg_id 精确去重 ----
            if message_id:
                if message_id in self._seen_msg_ids:
                    _detail("弹幕丢弃[msg_id 重复 %s] %s: %s",
                            message_id, user_name, content[:30])
                    self._dup_ids += 1
                    return []
                self._seen_msg_ids[message_id] = now
                # 有界: 超了就丢最旧的。重放只会紧跟在重连之后, 所以
                # 只需要记住"最近的"那批 —— 2000 条足够覆盖任何真实重放。
                while len(self._seen_msg_ids) > self._msg_id_cap:
                    self._seen_msg_ids.popitem(last=False)
                return self._accept_danmaku(str(user_id), user_name,
                                            content, now, message_id)
            # ---- 降级路径: 没有 ID, 只在 guard 窗口内查"历史重复" ----
            # `_is_replay_locked` 返回 True 时有两种含义: 确认丢弃, 或
            # 先押在缓冲里。两者对调用方都是"这条本身先不上屏", 语义一致。
            #
            # ⚠️ 但它**可能顺手放行了缓冲里的旧消息**(某条新消息打断了
            # 疑似 streak)。那些放行产生的动作存在 `_released_acts`,
            # 必须在这里取出来一起返回 —— 否则缓冲里若是 `#提示`/
            # `#下一题`, 它们的 HINT/REVEAL 就静默丢了。
            dropped = self._is_replay_locked(user_name, user_id, content,
                                             now, message_id)
            released, self._released_acts = self._released_acts, []
            if dropped:
                return released
            return released + self._accept_danmaku(
                str(user_id), user_name, content, now, message_id)

    def _is_replay_locked(self, user_name: str, user_id, content: str,
                          now: float, message_id: str = "") -> bool:
        """降级路径的判据(调用方须持锁)。返回 True = 判定为重放, 丢弃。

        ## 判据(方案 §9.2)

        重放的特征是: **重连后到来的一连串消息, 逐条都能在"重连前的
        最近历史"里找到完全匹配**。历史是 `A B C D E F`, 重连后又来
        `A B C D E F` —— 这才是重放。

        所以这里数的是 **incoming 连续命中数**, 不是"某个 fingerprint
        在历史里出现过几次"。这两者差别很大: 历史里 `A B C` 各只出现
        一次时, 后者会全部漏过去(它们各只出现 1 次), 而前者能识别。

        规则:
          - 命中 baseline 里**任意**一条 -> 连续命中数 +1;
          - 没命中 -> **清零**(一条真正的新消息说明这不是重放批次);
          - 连续命中数 >= `replay_guard_min_repeats` -> 确认为重放。

        ## 两个关键的防自我增强措施

        1. **baseline 在 guard 开启时冻结**(`_guard_baseline`)。否则
           重放的前几条一旦进了 `_danmaku`, 后面的就能"匹配到刚写进去的
           自己", 阈值形同虚设。
        2. **确认之前先缓冲**(`_guard_pending`), 不立刻写 `_danmaku` /
           不上屏。否则那 N-1 条"疑似但还没确认"的消息已经漏出去了 ——
           确认之后无法回收。

        代价: 无 ID 的弹幕在 guard 期间会有最多 N 条的延迟(等确认)。
        这是可接受的 —— guard 只在重连后开 20 秒, 而这段时间本来就要
        防重放。
        """
        if now >= self._guard_until:
            # guard 关着: 收尾。
            # ⚠️ 必须**先放行缓冲**再清状态 —— 缓冲里那是"疑似但从未确认"
            # 的消息(连续命中数没到阈值)。窗口都过完了还没确认, 说明它们
            # 大概率就是真人发的, 押着不放了等于永久丢失。
            # 返回值由调用方(submit 或 tick)负责交付。
            if self._guard_pending:
                log.info("guard 结束, 放行 %d 条未确认消息", len(self._guard_pending))
                # 放行动作交给调用方(submit 会取 `_released_acts`,
                # tick 有自己的到期路径), 这里只负责产生。
                self._released_acts.extend(self._flush_guard_pending_locked())
            if self._guard_baseline or self._guard_streak or self._guard_confirmed:
                self._reset_guard_locked()
            return False
        try:
            fp = (str(user_id), P.simplify_for_dedupe(content, 200))
        except Exception:                       # noqa: BLE001
            return False

        # ---- 已确认重放: 窗口内**继续按 baseline**判定 ----
        # 注意不能"一确认就无差别全丢": 重放批次过去之后, 真人还会接着
        # 说话。那些没在 baseline 里出现过的新消息必须放行, 否则就是把
        # 旧机制的误杀换了个触发条件(而且这次是整窗口 20 秒)。
        if self._guard_confirmed:
            if fp in self._guard_baseline:
                self._replays += 1
                return True
            # 新消息: 确认态不再需要缓冲(重放批次已经过去了)
            return False

        # ---- 这一条命中 baseline 吗? ----
        hit = fp in self._guard_baseline
        if hit:
            self._guard_streak += 1
            # 先进缓冲, 等确认。缓冲本身不写 `_danmaku`, 所以不会
            # 污染 baseline, 也不会自我增强。
            # ⚠️ **完整**存下 user_name —— 放行时要原样交回
            # `_accept_danmaku`。少存它就等于把真人变成匿名, 最终
            # 问答流/回答日志/猜中者姓名全错。
            self._guard_pending.append(
                (user_id, user_name, content, now, message_id))
        else:
            # 一条真正的新消息 -> 这不是重放批次。把缓冲里那些"疑似"
            # 的全部**放行**(它们确实可能是真人发的), 然后清零。
            # ⚠️ 放行产生的动作必须**交回给调用方** —— 缓冲里可能
            # 有 `#提示`/`#下一题`, 丢掉动作它们就静默失效了
            # (与早先 `_flush_burst` 踩的是同一个坑)。
            self._released_acts.extend(self._flush_guard_pending_locked())
            self._guard_streak = 0
            return False

        if self._guard_streak >= self.cfg.replay_guard_min_repeats:
            # 连续 N 条都命中历史 -> 确认为重放。缓冲里那 N 条**整批丢弃**。
            n = len(self._guard_pending)
            self._guard_pending.clear()
            self._guard_confirmed = True
            self._replays += n
            log.info("疑似重放: guard 内连续 %d 条命中重连前历史, 整批抑制",
                     n)
            return True
        # 还没到阈值: 这条先**不上屏**(在缓冲里等), 也不算丢弃
        return True

    def _flush_guard_pending_locked(self) -> list:
        """把 guard 缓冲里那些"疑似但未确认"的消息真正放行。

        它们是被误判候选 —— 一条新消息(或窗口到期)证明这不是重放批次,
        所以要补上屏。**返回它们产生的动作**, 由调用方交付:

           - `submit_danmaku` 路径: 追加到 `_released_acts`, 最后和
             当前消息的动作一起返回;
           - `tick` 路径: 直接并入 carry。

        早先这个函数把动作丢了(普通 `#问题` 恰好只入队, 所以看不出),
        但缓冲里若是 `#提示`/`#下一题`, 它们的 HINT/REVEAL 就无声消失
        —— 与 `_flush_burst` 曾经踩的是同一类 bug。
        """
        pend, self._guard_pending = self._guard_pending, []
        acts: list[EngineAction] = []
        for wid, name, content, ts, mid in pend:
            try:
                acts.extend(self._accept_danmaku(wid, name, content, ts, mid))
            except Exception as e:              # noqa: BLE001
                log.error("guard 放行异常: %s", e)
        return acts

    def _reset_guard_locked(self) -> None:
        self._guard_baseline = set()
        self._guard_pending = []
        self._guard_streak = 0
        self._guard_confirmed = False

    def _phase_ack_locked(self, user_name: str, text: str,
                          now: float) -> list[EngineAction]:
        """非 QA 阶段收到 `#问题` 的**确定性反馈**(方案 §8)。须持锁。

        走 `QARec(kind="system")` 进问答流, 而不是新造一条通道:
          - `qa_log` 已有完整渲染链(`_append_qa_locked` -> `renderQa`
            -> `buildRow` -> `.qa-row.kind-*`), 零新管线;
          - `qid < 0` 分支天然满足文档硬要求: **不**计 `_qa_total`、
            **不**进 `_history`(所以不会喂回 LLM)、**不**动
            `verdict_counts` —— 它只是给观众看的状态提示。

        为什么**不**写 `self._notice`: `notice` 是"屏幕上现在该显示什么"
        的持久状态, 而这是一次性事件。混进去会让几秒后 snapshot 里还
        挂着一条过期文案。而且 `notice` 目前前端根本没读
        (`web/app.js` 零命中), 走它等于什么都不显示。

        **全局节流**(不是按观众): 多人同时发时不至于刷屏。代价是窗口内
        其他人仍然静默, 所以窗口取得小(默认 5s)。节流期内**不**刷新
        时间戳, 让 ACK 按固定节奏出现, 而不是"最后一个人说了算"。
        """
        msg = _ACK_BY_PHASE.get(self.phase)
        if not msg:
            return []
        if now - self._last_ack_at < self.cfg.phase_ack_seconds:
            return []
        self._last_ack_at = now
        self._append_qa_locked(QARec(
            qid=-1, user_name="系统", text=msg, verdict="", kind="system",
            ts=now))
        _detail("非 QA 阶段 ACK(%s): %s  <- %s", self.phase.value, msg,
                user_name)
        # 只更新状态/提示文案, 不调 LLM。phase 没变, 所以不带 phase_changed。
        return [EngineAction(ActionKind.BROADCAST, {"phase_changed": False})]

    def _accept_danmaku(self, wid, user_name, content,
                        now, message_id: str = "") -> list[EngineAction]:
        """真正处理一条弹幕(调用方须持锁)。"""
        if True:
            is_cmd = content.strip().startswith(CMD_PREFIX)
            # 抖音的 WebSocket 有时会把**同一条弹幕推送两次**(实测: 同一 ts、
            # 同一人、同一内容成对出现)。这种重复不该上屏两次 ——
            # 视觉上像刷屏, 观众会以为系统坏了。
            # 只在"紧邻的 2 秒内、同一人、同一内容"才判重, 避免误杀
            # 观众隔一会儿真的又问了一遍。
            #
            # ⚠️ Q12b: **有平台 msg_id 时跳过这条启发式**。
            # 既然已经拿到平台唯一 ID, 它就该是唯一的重复判据 —— 否则
            # "不同 ID = 不误杀"这个契约并不成立: 同一人 2 秒内真的发了
            # 两次相同内容(平台给了两个不同 ID), 第二条仍会被这条吃掉。
            # 这条启发式只留给**没有 ID** 的消息(上游没给 / stdin / sim)。
            if (not message_id
                    and self._danmaku
                    and self._danmaku[-1].user_id == wid
                    and self._danmaku[-1].content == content
                    and now - self._danmaku[-1].ts < 2.0):
                _detail("弹幕丢弃[2s 内成对重复] %s: %s", user_name, content[:30])
                return []
            self._dm_seq += 1
            self._danmaku.append(DanmakuItem(wid, user_name, content, is_cmd,
                                             now, self._dm_seq, message_id))
            if len(self._danmaku) > self._danmaku_cap:
                self._danmaku = self._danmaku[-self._danmaku_cap:]
            self._viewers.add(wid)

            if not is_cmd:
                self._last_activity = now
                return []

            raw = content.strip()
            norm = P.simplify_for_dedupe(raw, self.cfg.max_question_len)
            if not norm:
                _detail("弹幕丢弃[清洗后为空] %s: %s", user_name, content[:30])
                return []
            self._last_activity = now

            # 特殊指令
            if norm in NEXT_TOKENS or any(t in norm for t in NEXT_TOKENS):
                if self.phase == Phase.QA and self._reveals < self.cfg.max_reveals_per_puzzle:
                    log.info("观众请求下一题")
                    return self._enter_revealing_locked(now, "skip", "")
                return []
            if norm in HINT_TOKENS or any(t in norm for t in HINT_TOKENS):
                # 观众主动要提示(#提示)。界面上不宣传这个指令(免得大家
                # 一直刷提示 = 剧透), 但观众自己发了就给。
                # 节流: 别让一个人狂刷把提示刷光。
                if (self.phase == Phase.QA
                        and not self._hint_pending
                        and now - self._last_manual_hint > 20):
                    self._last_manual_hint = now
                    # 与定时提示同一纪律: 这里**不**预加 `_hints_given`,
                    # 成功回调时才消耗槽位(第三轮 review P1)。
                    self._hint_pending = True
                    return [EngineAction(ActionKind.HINT, {
                        "level": self._hints_given + 1,
                        "puzzle": self._puzzle, "answer": self._answer,
                        "given": list(self._hints_shown),
                        "spec": self._spec,
                        "touched_fact_ids": sorted(self._touched_fact_ids)})]
                return []

            if self.phase != Phase.QA:
                # 非 QA 阶段收到 #问题: **不再静默吞掉**(方案 §8)。
                # 出题要 30-45s, 这段空窗里观众打字毫无反馈, 最容易被
                # 当成"卡死了"。给一条 0 成本确定性文案。
                ack = self._phase_ack_locked(user_name, content, now)
                if ack:
                    return ack
                _detail("弹幕丢弃[非问答阶段 %s] %s: %s",
                        self.phase.value, user_name, content[:30])
                return []

            # 去重: 同一人 + 同一问题, 窗口内只算一次
            key = (wid, norm)
            last = self._last_ask.get(key)
            if last is not None and now - last < self.cfg.qa_dedupe_seconds:
                _detail("弹幕丢弃[%.0fs 内同一人同一问题] %s: %s",
                        self.cfg.qa_dedupe_seconds, user_name, content[:30])
                return []
            self._last_ask[key] = now
            # 注意: 这里**不设**每人提问数上限。
            # 原来有个"每人每题 8 条"的上限, 但单人直播间会因此彻底卡死
            # (全场只有一个观众, 问满 8 条后就再也问不了了)。
            # 防刷已由上面的一人一问题去重 + 下面的队列封顶覆盖。

            # 队列封顶: 丢最旧
            if len(self._pending) >= self.cfg.pending_cap:
                gone = self._pending.pop(0)
                self._dropped += 1
                log.warning("待答队列已满(%d), 丢弃最旧的一条: %r",
                            self.cfg.pending_cap, gone.text[:30])

            self._qid_seq += 1
            self._questions_total += 1
            self._pending.append(PendingQ(
                qid=self._qid_seq, user_id=wid, user_name=user_name,
                text=P.display_question(raw, self.cfg.max_question_len), ts=now))
            _detail("弹幕收下[提问 #%d] %s: %s  (队列 %d)",
                    self._qid_seq, user_name,
                    P.display_question(raw, self.cfg.max_question_len)[:40],
                    len(self._pending))
            return []

    # ==================================================================
    # 输入: LLM 回调(worker 线程)
    # ==================================================================
    def submit_riddle(self, puzzle: Optional[str], answer: Optional[str] = None,
                      hints: Optional[list] = None, title: Optional[str] = None,
                      error: Optional[str] = None, usage: Optional[dict] = None,
                      model: Optional[str] = None, now: Optional[float] = None,
                      solve_atoms: Optional[list] = None,
                      fair_clues: Optional[list] = None,
                      signature: Optional[dict] = None,
                      spec: Optional[PuzzleSpec] = None,
                      source: str = "live_generate",
                      expect_round: Optional[int] = None
                      ) -> list[EngineAction]:
        """交付一道题。

        ## `expect_round`: 挡住"上一题的 worker 迟到"

        `if self.phase != Phase.SETTING` 只挡住"此刻不在等题"。它**挡不住**
        这一种真实情形:

            第 N 题   worker 发出 (gen_spec, 10–40s)
            ...超时/异常, 引擎回到 SETTING 重试
            第 N+1 题 worker 发出并很快返回 -> 上屏, 进入 QA
            第 N 题   worker **终于**返回 -> 此刻 phase 已经是
                      SETTING(下一题)或 QA

        若正好落在下一题的 SETTING 窗口里, 旧的返回会被当成**新题的**
        交付 —— 谜面是旧的、`_puzzle_index` 却递增, 于是配额/archive/
        used ledger 全部记错题。这是"跨题 stale 写入"。

        `expect_round` 让调用方声明"我这一发是为第几题出的"。与当前
        `round_index` 不符即丢弃。默认 None = 不做该检查(老调用方/测试
        兼容), 但**生产路径必须传**。
        """
        now = self._now(now)
        with self._lock:
            if self._stopped:
                return []
            # ---- 身份校验(stale worker gate) ----
            if expect_round is not None and expect_round != self.round_index:
                log.info("丢弃迟到交付: 该发是为第 %s 题出的, 当前第 %d 题",
                         expect_round, self.round_index)
                return []
            self._touch_meta(usage, model, error)
            if self.phase != Phase.SETTING:
                return []                      # 迟到/重复的回调忽略
            if not puzzle:
                return self._riddle_failed_locked(now, error or "空谜面")
            self._setting_deadline = None
            self._setting_attempts = 0
            self._puzzle = puzzle
            self._answer = answer or ""
            self._title = title or ""
            self._hint_pool = list(hints or [])
            # ---- 类型统一(第三轮 review P0) ----
            # Engine 内部协议固定为 `list[dict]`。这个边界必须收口:
            #   - AI 生成路径经 `_spec_to_riddle()` 传进来的是 dict;
            #   - 结构化兜底传进来的是 `SolveAtom/FairClue` **对象**。
            # 两种类型混在同一条链上会同时坏两件事:
            #   ① `judge()` 的 `isinstance(a, dict)` 判不出 role, 于是
            #      "必须同时命中 cause + mechanism" 这个代码层 gate 被
            #      静默跳过 —— 兜底题又绕回了宽判;
            #   ② REVEAL payload 带着 dataclass 对象落盘 -> json.dumps
            #      直接 TypeError, "出题全挂"时反而把落盘也带崩。
            # 在这里转一次, 后面 generated / fallback / pool 全都一样。
            self._solve_atoms = [_atom_dict(a) for a in (solve_atoms or [])]
            self._fair_clues = [_clue_dict(c) for c in (fair_clues or [])]
            self._spec = spec
            # 这道题**从哪来**(Q8 provenance)。三种取值:
            #   "pool"          题池里挑出来的
            #   "live_generate" 现场生成(含 no-llm 假题)
            #   "fallback"      引擎内部兜底(见 _riddle_failed_locked)
            #
            # 为什么放在 engine 而不是 director: 兜底是**引擎内部**的决定,
            # 发生在 director 的 worker 已经返回失败之后。让 director 去
            # 预测它 = 并行维护一份"当前是哪道题"的状态, 而
            # `Director._current_spec` 正是上一轮专门删掉的东西(它会串题)。
            # engine 才是"此刻哪道题在台上"的唯一无竞争所有者。
            #
            # 显式字段, **绝不从值推断** —— 我们在 blueprint_specified 上
            # 已经踩过一次"从值猜来源"的坑, 两个方向都会猜错。
            self._spec_source = source
            # 记下这题的指纹 —— 下一题的 blueprint 选择与跨题配额要用
            # (方案 §10)。只留最近 window 条, 不放进 Snapshot。
            if signature:
                self._remember_signature_locked(signature)
            self._hints_shown = []
            self._touched_fact_ids = set()
            self._candidate_count = 0
            self._puzzle_index += 1
            self.round_index = self._puzzle_index
            self._puzzle_started = now
            self._last_activity = now
            self._restate_at = now
            self._restate_n = 0
            self._hints_given = 0
            self._hint_pending = False
            self._hint_retry_at = 0.0
            self._hint_text = ""
            self._solved = False
            self._solved_by = ""
            self._revealed = ""
            self._reveals = 0
            self._reveal_deadline = None
            self._next_puzzle_deadline = None
            self._pending.clear()
            self._inflight.clear()
            self._inflight_at.clear()
            self._history.clear()
            self._qa_log.clear()
            self._qa_archive.clear()
            self._qa_total = 0
            self._verdict_counts.clear()
            self._last_ask.clear()
            self._questions_total = 0
            self._answered_total = 0
            self.phase = Phase.QA
            _detail("阶段 -> QA (第 %d 题: %s)", self._puzzle_index,
                    self._puzzle[:40])
            self._notice = "谜题已就位，发送 #你的问题 开始追问"
            self._phase_hint = f"第 {self._puzzle_index} 题 · 发 #问题 提问，猜中即揭晓"
            log.info("第 %d 题就位: %s", self._puzzle_index, self._puzzle[:30])
            # new_puzzle 广播必须让前端先清空, 所以排在最前
            return [EngineAction(ActionKind.BROADCAST, {
                "notice": self._notice, "phase_changed": True,
                "new_puzzle": True})]

    def submit_qa(self, answers: Optional[list[QAResult]] = None,
                  error: Optional[str] = None, usage: Optional[dict] = None,
                  model: Optional[str] = None, now: Optional[float] = None
                  ) -> list[EngineAction]:
        now = self._now(now)
        with self._lock:
            if self._stopped:
                return []
            self._touch_meta(usage, model, error)
            acts: list[EngineAction] = []

            # 把返回的裁决按 qid 落到在途条目上
            for r in (answers or []):
                q = self._inflight.pop(r.qid, None)
                self._inflight_at.pop(r.qid, None)
                if q is None:
                    continue                   # 不在途(过期/重复): 忽略
                if self.phase != Phase.QA:
                    continue
                rec = QARec(qid=q.qid, user_name=q.user_name, text=q.text,
                            verdict=r.verdict, comment=r.comment, kind="qa", ts=now,
                            status=r.status,
                            is_guess=r.is_guess, cause_hit=r.cause_hit,
                            mechanism_hit=r.mechanism_hit,
                            matched_atoms=r.matched_atoms,
                            touched_fact_ids=list(r.touched_fact_ids or []),
                            solution_candidate=r.solution_candidate)
                self._append_qa_locked(rec)
                # 累加"观众已经探索过哪些方向"(方案 §32)。
                # **touched ≠ discovered**: 只表示问过这个方向, 不代表已确认为真。
                for fid in (r.touched_fact_ids or []):
                    self._touched_fact_ids.add(fid)
                if r.solution_candidate:
                    self._candidate_count += 1
                self._answered_total += 1
                # 「未判定」是系统故障, 不是对观众猜测的评价 ——
                # 不进"是/不是/无关"统计, 否则复盘时会把它算成一次
                # "无关", 污染题目难度与猜中率。
                if r.verdict != P.UNAVAILABLE:
                    self._verdict_counts[r.verdict] = \
                        self._verdict_counts.get(r.verdict, 0) + 1
                acts.append(EngineAction(ActionKind.BROADCAST, {
                    "answer": rec.to_json(), "phase_changed": False}))
                if r.verdict == P.SOLVE and \
                        self._reveals < self.cfg.max_reveals_per_puzzle:
                    log.info("第 %d 题被 %s 猜中: %s", self._puzzle_index,
                             q.user_name, q.text[:30])
                    acts.extend(self._enter_revealing_locked(now, "solved",
                                                             q.user_name))
                    return acts

            # 注意: 这里**不做**"孤儿回收"。逐条秒回下, 一次 submit_qa
            # 只对应一条 ANSWER 调用(一个 qid); 其它在途条目属于别的并发
            # 调用, 与本次无关。真正丢失/超时的在途由 tick 的超时逻辑回收。
            if not acts:
                acts.append(EngineAction(ActionKind.BROADCAST, {
                    "notice": self._notice, "phase_changed": False}))
            return acts

    def submit_hint(self, text: Optional[str] = None,
                    error: Optional[str] = None, now: Optional[float] = None
                    ) -> list[EngineAction]:
        now = self._now(now)
        with self._lock:
            if self._stopped or self.phase != Phase.QA:
                return []
            self._hint_pending = False          # 在途结束(成功或失败)
            # ---- 失败: 槽位**不消耗**, 退避后再试同一格 ----
            #
            # 第三轮 review P1: 早先 `_hints_given` 在发出请求时就 +1,
            # 而失败回调什么都不做 -> 那一格永远补不回来, 观众少一条提示。
            # (Q6 之后 hint() 在"三次全泄底"时也会返回 None, 这条路径
            #  因此变成真实可达, 不再是理论问题。)
            #
            # 不能每 tick 立刻重试 —— 网关持续故障时会变成每秒一次的
            # 失败风暴。所以退避 `hint_retry_seconds` 再试, 且仍然受
            # 上面那个 `slot` 上限约束(时间轴走完就揭晓, 不会无限重试)。
            if error or not text:
                self.last_error = error
                self._hint_retry_at = now + self.cfg.hint_retry_seconds
                log.info("提示生成失败(%s), %.0fs 后重试同一格",
                         (error or "空提示")[:40], self.cfg.hint_retry_seconds)
                return []
            text = text.strip()[:80]
            # 文本与上一条相同 -> 不重复上屏(否则冷场时同一句刷屏)
            #
            # 但这**不能**当成成功: 早先这里直接 `return []`, pending 虽然
            # 清了, 却没有设退避 —— 下一次 tick 会立刻再发一次 HINT。冷场
            # 时 Writer 三次都给出"安全但重复"的 `last_safe`, 于是 4Hz 的
            # tick 会变成每秒 4 次重复请求。当成一次失败处理: 清 pending,
            # 退避后再试。
            if text == self._hint_text:
                self._hint_retry_at = now + self.cfg.hint_retry_seconds
                log.info("提示与上一条相同, 不重复上屏, %.0fs 后重试",
                         self.cfg.hint_retry_seconds)
                return []
            # ---- 成功: 现在才真正消耗槽位 ----
            self._hints_given += 1
            self._hint_text = text
            # 记进"实际给过"的历史 —— 下一条提示要靠它告诉 AI 别重复。
            # (以前这里传的是出题时的 _hint_pool, 那个从头到尾不变,
            #  导致 AI 以为"还没给过提示", 第二条和第一条说一样的话。)
            if text not in self._hints_shown:
                self._hints_shown.append(text)
            # 提示历史**保留**(观众要能回看线索); 只挡"与上一条完全相同"的重复。
            rec = QARec(qid=-(1000 + self._hints_given), user_name="提示",
                        text=self._hint_text, verdict="", kind="hint", ts=now)
            self._append_qa_locked(rec)
            self._restate_at = now
            self._phase_hint = (f"提示 {self._hints_given}/{self.cfg.max_hints}："
                                f"{self._hint_text}")
            return [EngineAction(ActionKind.BROADCAST, {
                "hint": self._hint_text, "phase_changed": True})]

    def submit_reveal(self, text: Optional[str] = None,
                      error: Optional[str] = None, now: Optional[float] = None
                      ) -> list[EngineAction]:
        now = self._now(now)
        with self._lock:
            if self._stopped or self.phase != Phase.REVEALING:
                return []
            self._reveal_deadline = None
            self._reveal_timeout = None
            self._revealed = (text or "").strip() or self._answer or \
                (P.FALLBACK_RIDDLES[0][1] if not self._answer else self._answer)
            self.phase = Phase.REVEALED
            _detail("阶段 -> REVEALED (第 %d 题揭晓)", self._puzzle_index)
            self._next_puzzle_deadline = now + self.cfg.reveal_hold_seconds
            reason = self._reveal_pending_reason
            if reason == "solved" and self._solved_by:
                self._notice = f"{self._solved_by} 猜中了！谜底揭晓"
            elif self._solved:
                self._notice = "谜底揭晓"
            else:
                self._notice = "谜底揭晓（本题无人猜中）"
            self._phase_hint = self._notice + "　—　稍后开启新谜题"
            log.info("揭晓(%s): %s", reason, self._revealed[:40])
            return [EngineAction(ActionKind.BROADCAST, {
                "notice": self._notice, "revealed": True, "phase_changed": True})]

    # ==================================================================
    # 调度: tick
    # ==================================================================
    def tick(self, now: Optional[float] = None) -> list[EngineAction]:
        now = self._now(now)
        with self._lock:
            if self._stopped:
                return []
            carry: list[EngineAction] = []
            # ---- Q12: guard 到期时把未确认的缓冲放行 ----
            # 放在 tick 里而不是只放在 submit 路径上: 否则重放/真人消息
            # 之后**没人再说话**时, 那几条会一直押在缓冲里出不来。
            if (self._guard_until and now >= self._guard_until
                    and self._guard_pending):
                log.info("guard 到期, 放行 %d 条未确认消息",
                         len(self._guard_pending))
                carry.extend(self._flush_guard_pending_locked())
                self._reset_guard_locked()
            ph = self.phase
            if ph == Phase.SETTING:
                return carry + self._tick_setting_locked(now)
            if ph == Phase.QA:
                return carry + self._tick_qa_locked(now)
            if ph == Phase.REVEALING:
                return carry + self._tick_revealing_locked(now)
            if ph == Phase.REVEALED:
                return carry + self._tick_revealed_locked(now)
            return carry

    # ------------------------------------------------------------------
    def _tick_setting_locked(self, now: float) -> list[EngineAction]:
        if self._setting_deadline is None or now < self._setting_deadline:
            return []
        return self._riddle_failed_locked(now, "出题超时")

    def _generation_inputs_locked(self) -> dict[str, Any]:
        """生成一道题需要的两个"看历史"的字段 —— **唯一定义处**。

        抽出来是因为它有两个消费者(现场出题 / 后台补池), 而"两条代码
        路径在这两个字段上漂移"已经害过我们一次: 早先重试路径漏带
        `avoid`+`recent_signatures`, 于是**只要发生一次外层 retry 就能
        绕过整个 Q4**。字段定义写两遍, 迟早再漂一次。
        """
        return {
            "avoid": list(self._used_titles[-8:]),
            # **转成 dict** —— 这些会经 director 传给 quality 层, 而
            # payload 是"可序列化的动作描述", 不该塞自定义对象进去
            # (测试里 `.get()` 会直接炸)。
            "recent_signatures": [s.to_dict() for s in self._recent_signatures],
        }

    def _riddle_action_locked(self, reason: str,
                              attempt: int = 0) -> EngineAction:
        """构造 RIDDLE 动作 —— **首轮与重试必须走同一个函数**。

        早先重试时只带 `reason`+`attempt`, 把 `avoid` 和
        `recent_signatures` 丢了。后果很隐蔽:
          - director 收到 `recent_signatures=[]` -> Blueprint Scheduler
            以为"前面一道题都没播过", 配额失效;
          - `avoid=None` -> 文本去重也失效。
        也就是说**只要发生一次外层 retry, 就能绕过整个 Q4**。
        """
        p = {"reason": reason}
        p.update(self._generation_inputs_locked())
        # `expect_round`: 这一发是**为第几题**要的。worker 交付时必须
        # 原样带回, `submit_riddle` 会拿它与当前 `round_index` 比对 ——
        # 不符即丢弃。见 `submit_riddle` 的 `expect_round` 说明。
        #
        # 在**动作生成时**取值(而不是 worker 返回时): 那才是"这一发属于
        # 哪一题"的真实时刻。`_enter_setting_locked` 已经把 round_index
        # 推进到"正在要的这一题", 所以这里读到的就是目标题号。
        p["expect_round"] = self.round_index
        if attempt:
            p["attempt"] = attempt
        return EngineAction(ActionKind.RIDDLE, p)

    def request_riddle_action(self, reason: str = "riddle_deferred"
                              ) -> EngineAction:
        """**给外部线程**用的: 拿一个 RIDDLE 动作(自带加锁)。

        director 的调度线程需要在"上一拍出题被推迟"之后补发一次请求。
        它以前直接调 `_riddle_action_locked()`, 那是个明确带锁语义的私有
        方法 —— 眼下只是读几个字段所以没炸, 但 API 边界不干净。
        """
        with self._lock:
            return self._riddle_action_locked(reason)

    def snapshot_generation_inputs(self) -> dict[str, Any]:
        """给**外部生成线程**用的一致快照: `{"avoid", "recent_signatures"}`。

        后台补池(Q9)要在"决定生成那一刻"取一份 self-consistent 的
        avoid/recent —— 与 `_riddle_action_locked` 走的是同一个
        `_generation_inputs_locked()`, 同一把锁。

        刻意**不**复用 `request_riddle_action()`: 那个返回的是一个
        RIDDLE **动作**(给 director 派发用), 补池只需要两个字段,
        不该被动作的形状绑架 —— 将来 payload 加字段时, 补池不该
        被动地跟着变。
        """
        with self._lock:
            return self._generation_inputs_locked()

    def pressure(self) -> dict[str, Any]:
        """补池用的**只读**压力探针。

        **不放进 `Snapshot`** —— 这是内部生成状态, 前端不需要也不该
        看到(与 `_recent_signatures` 同样的理由)。

        为什么不复用 `_probe()`: 后者明确标注"测试/装配用", 而且它把
        phase 转成 str、字段集是给断言用的, 不是稳定 API。补池是生产
        消费者, 给它一个语义写死的入口。

        为什么必须暴露 hint/reveal 在途: `_hint_pending` / `_reveal_deadline`
        **既不在 Snapshot 里, 也不反映在 `pending_count` 上** —— hint
        在途时 `pending_count` 仍然是 0(`pending_count` 只数观众的
        提问)。少了这两个字段, 补池就会在一次提示生成的同时再打一次
        出题 LLM, 而那正是"补池不能和直播抢网关"要避免的。
        """
        with self._lock:
            return {
                "phase": self.phase,
                "pending": len(self._pending),
                "inflight": len(self._inflight),
                "hint_inflight": bool(self._hint_pending),
                "reveal_inflight": self._reveal_deadline is not None,
                # 用 `_stopped` 而不是 phase == STOPPED: 后者在 `stop()`
                # 里与前者同锁同写, 但 `_stopped` 才是那个真正的标志位
                # (should_stop 也是读它)。补池只关心"还该不该干活"。
                "stopped": bool(self._stopped),
            }

    def _riddle_failed_locked(self, now: float, why: str) -> list[EngineAction]:
        self._setting_attempts += 1
        self.last_error = why
        if self._setting_attempts >= self.cfg.riddle_max_attempts:
            # 用兜底谜题, 保证永不开天窗
            log.warning("出题连续失败(%s), 使用兜底谜题", why)
            # 轮换兜底题 —— 总用同一道, 观众一看就知道出题挂了。
            #
            # **兜底也必须结构化**(第二轮 review P1): 它同样要经过正式
            # Q&A, 只给 puzzle/answer 的话 facts 为空 -> 主持人退回"只看
            # 文学谜底", Final Judge 也没有 atom gate —— 质量系统在这条
            # 路径上等于不存在。
            spec = P.fallback_spec(self._puzzle_index)
            return self.submit_riddle(
                spec.puzzle, spec.answer, list(spec.hints), title=spec.title,
                now=now, solve_atoms=spec.solve_atoms,
                fair_clues=spec.fair_clues, spec=spec,
                # 兜底是第三种来源, 由**引擎自己**标记 —— 见上面的说明。
                source="fallback",
                # 注意: **不传 signature** —— 兜底题不该挤占跨题配额,
                # 否则"出题全挂了"这一事实会污染全局分布统计。
                signature=None)
        log.warning("出题失败(%s), 重试 %d/%d", why, self._setting_attempts,
                    self.cfg.riddle_max_attempts)
        return [self._riddle_action_locked("riddle_retry",
                                           self._setting_attempts)]

    def _tick_qa_locked(self, now: float) -> list[EngineAction]:
        acts: list[EngineAction] = []

        # ① 在途超时 -> **直接判"未判定"**。
        #
        # 这里**绝不重派**。曾经的写法是 `q.ready_at = now + 1.0` 再塞回
        # `_pending`, 但 urllib 请求**无法取消** —— 旧 worker 根本没死,
        # 于是同一 qid 会同时存在多个 worker。2026-09-18 的真实故障:
        # `qa_inflight_timeout=25` 远小于 worker 的真实最坏耗时
        # (`AI_TIMEOUT=60` × (1+`AI_MAX_RETRIES=3`) ≈ 247s), 结果
        #   worker A(13.3s 起) -> 25s 判超时 -> worker B(39.4s 起)
        #   -> 又 25s -> worker C(05.6s 起)
        # 三条提问记录(85.2s / 125.8s / 152.4s)其实是三个 worker 陆续回来,
        # 不是一次调用打印三遍。每个 worker 内还有 4 次 HTTP, 最坏
        # 1 个问题 -> 3 worker -> 12 个请求, 且并行。
        #
        # 为什么 fail-fast 是安全的: `submit_qa` 本来就丢弃"qid 已不在
        # `_inflight`"的迟到结果, 所以旧 worker 最终返回时**已经被忽略**,
        # 不会写坏状态。代价只是"观众要重发一次" —— 而当前行为是
        # "等 152 秒然后收到 3 条重复记录"。
        stale = [qid for qid, t in self._inflight_at.items()
                 if now - t >= self.cfg.qa_inflight_timeout]
        for qid in stale:
            q = self._inflight.pop(qid, None)
            self._inflight_at.pop(qid, None)
            if q is None:
                continue
            # 绝不让提问被静默吞掉(直播上就是"卡了")。
            # 但兜底裁决必须是**未判定**, 不是"无关": 后者在断言
            # "你的猜测与谜底无关", 而我们其实**根本没判断成功**。
            rec = QARec(qid=q.qid, user_name=q.user_name, text=q.text,
                        verdict=P.UNAVAILABLE,
                        comment="刚才网络抖了一下，再发一次吧",
                        kind="qa", ts=now)
            self._append_qa_locked(rec)
            self._answered_total += 1
            acts.append(EngineAction(ActionKind.BROADCAST, {
                "answer": rec.to_json(), "phase_changed": False}))

        # ② 派发: 逐条秒回, 并发上限 qa_max_inflight
        while len(self._inflight) < self.cfg.qa_max_inflight:
            idx = next((i for i, q in enumerate(self._pending)
                        if q.ready_at <= now), None)
            if idx is None:
                break
            q = self._pending.pop(idx)
            self._inflight[q.qid] = q
            self._inflight_at[q.qid] = now
            acts.append(EngineAction(ActionKind.ANSWER, {
                "qid": q.qid, "user_name": q.user_name, "text": q.text,
                "puzzle": self._puzzle, "answer": self._answer,
                "solve_atoms": list(self._solve_atoms),
                "fair_clues": list(self._fair_clues),
                "facts": ([f.to_dict() for f in self._spec.facts]
                          if self._spec else []),
                "transcript": self._transcript_locked(),
                "stats": dict(self._verdict_counts),
                # QA 自己的时延预算(见 config.qa_answer_timeout)。
                # 放进 payload 而不是让 director 去读配置: 派发决策在这里,
                # 预算就该跟这条动作一起走。
                "timeout": self.cfg.qa_answer_timeout,
                "max_retries": self.cfg.qa_answer_retries,
            }))

        # ③ 收尾与提示 —— **单一时间轴**:
        #      t0          出题
        #      t0 + 1×N    提示 1
        #      t0 + 2×N    提示 2
        #      t0 + 3×N    提示 3        (N = hint_seconds)
        #      t0 + 4×N    揭晓
        #    另外: 有人猜中 -> 立即揭晓(由 submit_qa 触发)。提问条数不设上限。
        #
        #    **计时用 _puzzle_started, 与观众活动完全无关** —— 这条时间轴
        #    的唯一目的就是"控制一道题的总时长"。有人一直聊天也要照走,
        #    否则题目会无限拖下去。
        #    也**不要求队列为空**: 人多时提问永远问不完, 若等队列清空,
        #    提示和揭晓就永远不来了。
        elapsed = now - self._puzzle_started if self._puzzle_started is not None else 0.0
        slot = int(elapsed // self.cfg.hint_seconds)   # 当前走到第几个时间格
        if slot >= self.cfg.max_hints + 1:
            log.info("第 %d 题时间轴走完(%.0fs), 揭晓", self._puzzle_index,
                     elapsed)
            acts.extend(self._enter_revealing_locked(now, "giveup", ""))
            return acts
        # 到点就给提示(1..max_hints 格各给一条)。
        #
        # 关键(第三轮 review P1): 这里**不**再预先把 `_hints_given` +1 ——
        # 那是"请求数"不是"展示数"。worker 失败时槽位会白白损失, 观众
        # 少一条提示。改成:
        #   `_hint_pending` 挡住重复派发(一次只允许一条在途),
        #   成功回调时才 `_hints_given += 1`(见 submit_hint),
        #   失败则设一个退避时间, 过一会儿再试同一格 —— 不是每 tick
        #   重试(那会形成失败风暴, 每秒打一次 LLM)。
        if (1 <= slot <= self.cfg.max_hints
                and self._hints_given < slot
                and not self._hint_pending
                and now >= self._hint_retry_at):
            self._hint_pending = True
            acts.append(EngineAction(ActionKind.HINT, {
                "level": self._hints_given + 1,
                "puzzle": self._puzzle, "answer": self._answer,
                "given": list(self._hints_shown),
                # ---- Q6(方案 §31/§32): fact-aware hint 的三样输入 ----
                # `spec` 让 worker 能算出该点拨哪个方向;
                # `touched` 是"玩家群体问过哪些方向" —— 已问过的不该再提示;
                # 两者都**只读**地拷出去, worker 不碰引擎状态。
                "spec": self._spec,
                "touched_fact_ids": sorted(self._touched_fact_ids)}))
            return acts

        if acts:
            return acts

        # ④ 冷场重述(零成本): 长时间没人说话, 把谜面重述一遍并换句引导语
        idle = now - self._last_activity
        if idle >= self.cfg.restate_seconds:
            self._last_activity = now          # 重置, 免得每 tick 都重述
            self._restate_n += 1
            nudge = _NUDGES[self._restate_n % len(_NUDGES)]
            acts.append(EngineAction(ActionKind.BROADCAST, {
                "restate": True, "nudge": nudge,
                "puzzle": self._puzzle, "phase_changed": False}))
            return acts
        return acts

    def _tick_revealing_locked(self, now: float) -> list[EngineAction]:
        # 揭晓生成超时 -> 用已存的谜底兜底, 绝不卡住
        if self._reveal_deadline is not None and now >= self._reveal_deadline:
            log.warning("揭晓生成超时, 使用已存谜底兜底")
            return self.submit_reveal(self._answer or self.cfg.fallback_answer,
                                      now=now)
        return []

    def _tick_revealed_locked(self, now: float) -> list[EngineAction]:
        if self._next_puzzle_deadline is None or now < self._next_puzzle_deadline:
            return []
        self._next_puzzle_deadline = None
        # 记下**谜面本身**(不只是标题) —— 标题常常为空, 光靠标题根本挡不住
        # 重复出题。这里存的是模型真正需要避开的东西。
        if self._puzzle:
            self._used_titles.append(self._puzzle.strip()[:60])
            # 只留最近 12 条, prompt 不至于膨胀
            if len(self._used_titles) > 12:
                self._used_titles = self._used_titles[-12:]
        log.info("谜底展示结束, 开启第 %d 题", self._puzzle_index + 1)
        return self._enter_setting_locked(now, reason="riddle")

    # ==================================================================
    # 阶段进入
    # ==================================================================
    def _enter_setting_locked(self, now: float, reason: str) -> list[EngineAction]:
        self.phase = Phase.SETTING
        _detail("阶段 -> SETTING (第 %d 题开始出题)", self._puzzle_index + 1)
        self._setting_deadline = now + self.cfg.setting_timeout_seconds
        self._setting_attempts = 0
        self._puzzle = ""
        self._answer = ""
        self._solve_atoms = []
        self._fair_clues = []
        self._spec = None
        # 来源也一起重置: 否则上一题是池子来的, 这一题还没回调时
        # REVEAL payload 就可能带着**上一题的**来源(串题)。
        self._spec_source = "live_generate"
        self._title = ""
        self._pending.clear()
        self._inflight.clear()
        self._inflight_at.clear()
        self._notice = "正在准备新谜题…"
        self._phase_hint = "AI 正在出题，请稍候…"
        # 开新题: 先广播让前端清空(与 submit_riddle 的 new_puzzle 呼应), 再请求出题
        acts = [EngineAction(ActionKind.BROADCAST, {
            "notice": self._notice, "phase_changed": True,
            "new_puzzle": True})]
        # 把最近的指纹交给 director —— blueprint 选择与跨题配额都在**代码层**
        # 决定(方案 §7/§10): 先选好硬约束再让模型照着设计, 而不是写一句
        # "换个完全不同的题材"然后指望它理解什么叫"不同"。
        acts.append(self._riddle_action_locked(reason))
        return acts

    def _remember_signature_locked(self, signature) -> None:
        """记下这题的指纹, 只留最近 quality_recent_window 条。"""
        if not signature:
            return
        sig = (signature if isinstance(signature, PuzzleSignature)
               else PuzzleSignature.from_dict(signature))
        self._recent_signatures.append(sig)
        keep = max(1, int(getattr(self.cfg, "quality_recent_window", 10)))
        if len(self._recent_signatures) > keep:
            self._recent_signatures = self._recent_signatures[-keep:]
        _detail("记下第 %d 题指纹: %s/%s (最近 %d 条)",
                self._puzzle_index, sig.mechanism_family, sig.solution_shape,
                len(self._recent_signatures))

    def _enter_revealing_locked(self, now: float, reason: str,
                                winner: str) -> list[EngineAction]:
        self.phase = Phase.REVEALING
        self._reveal_pending_reason = reason
        self._reveals += 1
        if reason == "solved":
            self._solved = True
            self._solved_by = winner
            self._solved_total += 1
        self._reveal_deadline = now + self.cfg.setting_timeout_seconds
        self._pending.clear()
        self._inflight.clear()
        self._inflight_at.clear()
        if reason == "solved":
            self._notice = f"{winner} 猜中了！正在揭晓谜底…"
        elif reason == "skip":
            self._notice = "收到换题请求，正在揭晓…"
        else:
            self._notice = "时间到，正在揭晓谜底…"
        self._phase_hint = self._notice
        return [EngineAction(ActionKind.REVEAL, {
            "reason": reason,
            "winner": winner if reason == "solved" else "",
            "puzzle": self._puzzle, "answer": self._answer,
            # 出题时定下的原子事实与公平线索 —— **必须一路带到 archive**。
            # director._archive_reveal() 早就读这两个字段了, 但 payload 一直
            # 没给, 于是落盘里恒为空数组, 赛后复盘"为什么这条没判中"时
            # 对照不了。实测踩过: 结构齐了, 数据没流过去。
            "solve_atoms": list(self._solve_atoms),
            "fair_clues": list(self._fair_clues),
            # 完整 spec 也带上 —— archive 要按方案 §34 落盘结构化定义
            # (facts/hints/blueprint/signature), 不只是谜面谜底两段文本。
            "spec": self._spec,
            # provenance 一路带到 archive(director._archive_reveal 读它)。
            "spec_source": self._spec_source,
            "transcript": self._transcript_locked(),
        })]

    # ==================================================================
    # 内部辅助
    # ==================================================================
    def _append_qa_locked(self, rec: QARec) -> None:
        """把一条记录追加到问答日志。

        顺序规则(重要):
          - **提示/重述** (qid < 0): 按发生时间追加, 保持原序。
          - **问答** (qid > 0): 并发回答时到达顺序会乱, 按 qid 插入到
            正确位置(它前面的问答都已有更小的 qid)。

        `_qa_total` 只统计**问答**, 不计提示/重述 —— 它是前端"已渲染到
        第几条"的游标, 而提示会被新提示替换(不累积), 计进去会错位。
        """
        if rec.qid >= 0:
            self._qa_total += 1
            pos = len(self._qa_log)
            for i in range(len(self._qa_log) - 1, -1, -1):
                if self._qa_log[i].qid >= 0 and self._qa_log[i].qid > rec.qid:
                    pos = i
                elif self._qa_log[i].qid >= 0:
                    break
            self._qa_log.insert(pos, rec)
            # ---- Step 09: 整题 append-only 的完整存档 ----
            #
            # `_qa_log` 是**上屏窗口**(见下面 120 条截断), 而 `_qa_archive`
            # 是这道题的**全部**记录。两者职责必须分开:
            #   - `_qa_log`    -> UI 尾窗, 可以截断(前端只渲染尾部);
            #   - `_qa_archive`-> 分析/复盘/落盘, **一条都不能丢**。
            #
            # 修之前 snapshot 里的 `qa_archive` 是从 `_qa_log` 现算的 ——
            # 于是"归档"同样只留 120 条, 超出的静默消失。一场直播几百条
            # 问答时, 后面全部丢失, 而且是**看不出来**的丢(字段还在,
            # 只是短了)。
            #
            # 与 `_qa_log` 保持同一顺序(问答按 qid 插到正确位置), 这样
            # 两份记录逐条对得上, 排查时不会互相矛盾。
            if len(self._qa_archive) == len(self._qa_log) - 1:
                # 快路径: 上一拍刚同步过, 直接把新记录按同样规则插进去
                pos_a = len(self._qa_archive)
                for i in range(len(self._qa_archive) - 1, -1, -1):
                    if (self._qa_archive[i].qid >= 0
                            and self._qa_archive[i].qid > rec.qid):
                        pos_a = i
                    elif self._qa_archive[i].qid >= 0:
                        break
                self._qa_archive.insert(pos_a, rec)
            else:
                # 慢路径(理论上不该走到): 直接追加, 保证不漏记。
                # **宁可顺序略有偏差, 也不能丢记录** —— 顺序可以事后按
                # qid 重排, 丢掉的记录无法恢复。
                self._qa_archive.append(rec)
            # 「未判定」**不进 transcript**: 它不是对这条提问的判断, 只是
            # 系统这次没答上。喂回模型会让它以为"未判定"是一种合法裁决,
            # 久而久之开始拿它敷衍。
            if rec.verdict != P.UNAVAILABLE:
                self._history.append(rec)
                self._trim_history_locked()
        else:
            self._qa_log.append(rec)
            self._qa_archive.append(rec)
        # ⚠️ 只截 `_qa_log`(上屏窗口)。`_qa_archive` **永不截断** ——
        # 它命名成 archive 就该是 archive。
        if len(self._qa_log) > 120:
            self._qa_log = self._qa_log[-120:]

    def _trim_history_locked(self) -> None:
        if len(self._history) > self.cfg.qa_max_records:
            self._history = self._history[-self.cfg.qa_max_records:]
        while self._history and sum(
                len(h.text) + len(h.verdict) + 12 for h in self._history
        ) > self.cfg.qa_max_chars:
            self._history.pop(0)

    def _transcript_locked(self) -> list[str]:
        """喂回 LLM 的问答记录(已裁剪) + 机械统计行。"""
        lines = [r.to_line() for r in self._history]
        if self._verdict_counts:
            summary = " / ".join(f"{k} {v}" for k, v in self._verdict_counts.items())
            total = sum(self._verdict_counts.values())
            lines.append(f"【问答统计】已答 {total} 条： {summary}")
        return lines

    def _touch_meta(self, usage, model, error) -> None:
        if usage:
            self.last_usage = usage
        if model:
            self.model = model
        if error:
            self.last_error = error

    def _now(self, override: Optional[float]) -> float:
        return override if override is not None else self._clock()

    # ==================================================================
    # 快照
    # ==================================================================
    def snapshot(self, now: Optional[float] = None) -> Snapshot:
        now = self._now(now)
        with self._lock:
            next_ms = None
            if self.phase == Phase.REVEALED and self._next_puzzle_deadline:
                next_ms = max(0, int((self._next_puzzle_deadline - now) * 1000))
            elapsed = None
            if self.phase in (Phase.QA, Phase.REVEALING) and self._puzzle_started:
                elapsed = max(0, int((now - self._puzzle_started) * 1000))
            # 时间轴倒计时: QA 阶段告诉前端"距离下一条提示/揭晓还有多久"
            ev_ms = None
            ev_kind = ""
            ev_label = ""
            slot = 0
            # 注意: 用 `is not None` 而不是真值判断 —— 测试的 FakeClock 从 0 开始,
            # `_puzzle_started` 会是 0.0, 真值判断会把它当成"没有开始"。
            if self.phase == Phase.QA and self._puzzle_started is not None:
                n = self.cfg.hint_seconds
                slot = int((now - self._puzzle_started) // n)
                remaining = n - ((now - self._puzzle_started) % n)
                ev_ms = max(0, int(remaining * 1000))
                if slot < self.cfg.max_hints:
                    ev_kind = "hint"
                    ev_label = f"距第 {slot + 1} 条提示"
                else:
                    ev_kind = "reveal"
                    ev_label = "距揭晓"
            return Snapshot(
                phase=self.phase,
                puzzle=self._puzzle,
                puzzle_title=self._title,
                puzzle_index=self._puzzle_index,
                puzzle_elapsed_ms=elapsed,
                revealed_answer=self._revealed,
                solved=self._solved,
                solved_by=self._solved_by,
                qa_log=[r.to_json() for r in self._qa_log[-40:]],
                qa_archive=[r.to_archive() for r in self._qa_archive],
                qa_total=self._qa_total,
                pending_count=len(self._pending) + len(self._inflight),
                hint_count=self._hints_given,
                hint_text=self._hint_text,
                next_puzzle_ms=next_ms,
                next_event_ms=ev_ms,
                next_event_kind=ev_kind,
                next_event_label=ev_label,
                timeline_total=self.cfg.max_hints + 1,
                timeline_slot=slot,
                story_index=self._puzzle_index,     # 兼容别名
                danmaku=[d.to_json() for d in self._danmaku[-40:]],
                notice=self._notice or None,
                phase_hint=self._phase_hint,
                stat_questions=self._questions_total,
                stat_answered=self._answered_total,
                stat_solved=self._solved_total,
                stat_dropped=self._dropped,
                stat_viewers_seen=len(self._viewers),
                model=self.model,
                model_requested=self.model_requested,
                last_usage=self.last_usage,
                last_error=self.last_error,
                source=self.source,
                reconnects=self.reconnects,
                reconnect_fails=self.reconnect_fails,
                puzzles_total=self._puzzle_index,
            )

    # ==================================================================
    def should_stop(self) -> bool:
        with self._lock:
            if self._stopped:
                return True
            return (bool(self.cfg.max_puzzles)
                    and self._puzzle_index >= self.cfg.max_puzzles
                    and self.phase == Phase.REVEALED)

    # ---- 测试/装配用的只读探针 ----
    def _probe(self) -> dict[str, Any]:
        with self._lock:
            return {
                "phase": self.phase.value,
                "pending": len(self._pending),
                "inflight": len(self._inflight),
                "history": len(self._history),
                "history_chars": sum(len(h.text) + len(h.verdict) + 12
                                     for h in self._history),
                "dropped": self._dropped,
                "hints": self._hints_given,
                "reveals": self._reveals,
                "puzzle_index": self._puzzle_index,
                "solved": self._solved,
            }
