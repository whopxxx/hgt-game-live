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
from typing import Any, Callable, Optional

from .config import Config
from . import parser as P
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


# 空闲重述时轮换的引导语(零成本, 让冷场画面"呼吸")
_NUDGES = (
    "有人想问什么吗？发送 #你的问题 向我提问",
    "谜面里的每个细节都可能是线索",
    "想到了就直接说出你的答案，猜中我就揭晓",
    "别怕猜错，问错方向也没关系",
)


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
        self._hints_shown: list[str] = []    # **实际展示过**的提示文本
        self._hints_given = 0
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
        self._qa_log: list[QARec] = []       # 上屏
        self._qa_total = 0
        self._verdict_counts: dict[str, int] = {}
        self._last_ask: dict[tuple[str, str], float] = {}   # 去重
        self._last_activity = 0.0
        self._restate_at = 0.0
        self._restate_n = 0
        self._last_manual_hint = 0.0        # 观众 #提示 的节流

        # ---- 弹幕 / 观众 ----
        self._danmaku: list[DanmakuItem] = []
        self._danmaku_cap = 60
        self._dm_seq = 0                    # 弹幕全局序号(单调递增)
        self._burst_start: Optional[float] = None   # 当前缓冲窗口的起点
        self._burst_last: Optional[float] = None    # 缓冲里最后一条的时刻
        self._burst: list = []              # 窗口内缓冲的弹幕(待判定是否重放)
        self._replays = 0                   # 判定为重放并丢弃的批次数
        self._pending_acts: list[EngineAction] = []   # 攒着待发的即时动作
        self._mute_until = 0.0              # 压制弹幕到这个时刻(重放期间)
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
        with self._lock:
            self._notice = "弹幕已重连"

    # ==================================================================
    # 输入: 弹幕(ws 线程 -> 消费线程)
    # ==================================================================
    def submit_danmaku(self, user_id, user_name: str, content: str,
                       now: Optional[float] = None) -> list[EngineAction]:
        """弹幕入口。先过"重放检测", 再交给 _accept_danmaku 真正处理。

        为什么需要重放检测: 实测抖音在**重连后会把之前整批弹幕原样重发**
        (11 条挤在同一秒, 内容和一分钟前那批一模一样)。那批重放会:
          - 在弹幕轨道上又刷一遍(看着像乱飞)
          - 被当成新提问再答一遍(重复回答)
        所以整批丢弃。

        做法: **先缓冲, 后提交** —— 一个短时间窗内收到的先攒着, 窗口结束
        时若条数正常就提交, 超阈值则整批丢弃。这样不必回滚已计票的提问。
        """
        now = self._now(now)
        with self._lock:
            if self._stopped:
                _detail("弹幕丢弃[已停播] %s: %s", user_name, content[:30])
                return []
            # replay_burst_n <= 0: 关掉重放检测(测试用), 直接处理。
            if self.cfg.replay_burst_n <= 0:
                return self._accept_danmaku(str(user_id), user_name,
                                            content, now)
            # 压制期内: 重放往往分批(每 1.5s 窗口刚好凑够阈值丢一批),
            # 批次之间会漏掉零头。所以命中重放后**压制一段时间**,
            # 把整个重放过程一次性挡掉。
            if now < self._mute_until:
                _detail("弹幕丢弃[重放压制中 %.1fs] %s: %s",
                        self._mute_until - now, user_name, content[:30])
                return []
            # 距上一条超过一个窗口 -> 这是**新的一波**。
            # 先把上一波提交掉(它已经安全了: 攒了这么久都没超阈值),
            # 再开始新的一波并重置计数。
            # (用 _burst_last 而不是 _burst_start 判断: 否则隔 20 秒来一条的
            #  真人节奏会被算成"40 秒内涌入 3 条"而误判为重放。)
            if (self._burst_last is None
                    or now - self._burst_last > self.cfg.replay_burst_ms / 1000.0):
                if self._burst:
                    # 提交上一波, 它产生的即时动作(#提示/#下一题)先存着,
                    # 由下一次 tick 统一发出(scheduler 是唯一的动作发出者)。
                    self._pending_acts.extend(self._flush_burst())
                self._burst_start = now
            self._burst.append((str(user_id), user_name, content, now))
            self._burst_last = now
            if len(self._burst) >= self.cfg.replay_burst_n:
                log.warning("疑似弹幕重放(%.0fms 内涌入 %d 条), 丢弃整批并压制 %.1fs",
                            (self._burst_last - self._burst_start) * 1000,
                            len(self._burst), self.cfg.replay_mute_s)
                self._burst = []
                self._burst_start = None
                self._burst_last = None
                self._replays += 1
                self._mute_until = now + self.cfg.replay_mute_s
            return []

    def _flush_burst(self) -> list[EngineAction]:
        """把缓冲区里的弹幕真正收下(调用方须持锁)。

        返回这批里产生的**即时动作**(如 #提示 / #下一题)。
        以前这里是 `-> None`, 把返回值丢了 —— 结果 `#提示` / `#下一题`
        这两个指令**彻底失效**(观众发了没反应)。现在收集起来交给 tick。
        """
        buf, self._burst = self._burst, []
        self._burst_start = None
        self._burst_last = None
        acts: list[EngineAction] = []
        for wid, name, content, ts in buf:
            try:
                acts.extend(self._accept_danmaku(wid, name, content, ts))
            except Exception as e:
                log.error("弹幕处理异常: %s", e)
        return acts

    def _accept_danmaku(self, wid, user_name, content,
                        now) -> list[EngineAction]:
        """真正处理一条弹幕(调用方须持锁)。"""
        if True:
            is_cmd = content.strip().startswith(CMD_PREFIX)
            # 抖音的 WebSocket 有时会把**同一条弹幕推送两次**(实测: 同一 ts、
            # 同一人、同一内容成对出现)。这种重复不该上屏两次 ——
            # 视觉上像刷屏, 观众会以为系统坏了。
            # 只在"紧邻的 2 秒内、同一人、同一内容"才判重, 避免误杀
            # 观众隔一会儿真的又问了一遍。
            if (self._danmaku
                    and self._danmaku[-1].user_id == wid
                    and self._danmaku[-1].content == content
                    and now - self._danmaku[-1].ts < 2.0):
                _detail("弹幕丢弃[2s 内成对重复] %s: %s", user_name, content[:30])
                return []
            self._dm_seq += 1
            self._danmaku.append(DanmakuItem(wid, user_name, content, is_cmd,
                                             now, self._dm_seq))
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
                        and now - self._last_manual_hint > 20):
                    self._last_manual_hint = now
                    self._hints_given += 1
                    return [EngineAction(ActionKind.HINT, {
                        "level": self._hints_given,
                        "puzzle": self._puzzle, "answer": self._answer,
                        "given": list(self._hints_shown)})]
                return []

            if self.phase != Phase.QA:
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
                      fair_clues: Optional[list] = None
                      ) -> list[EngineAction]:
        now = self._now(now)
        with self._lock:
            if self._stopped:
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
            self._solve_atoms = list(solve_atoms or [])
            self._fair_clues = list(fair_clues or [])
            self._hints_shown = []
            self._puzzle_index += 1
            self.round_index = self._puzzle_index
            self._puzzle_started = now
            self._last_activity = now
            self._restate_at = now
            self._restate_n = 0
            self._hints_given = 0
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
                            matched_atoms=r.matched_atoms)
                self._append_qa_locked(rec)
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
            if error or not text:
                self.last_error = error
                return []
            text = text.strip()[:80]
            # 注意: 这里**不递增** _hints_given —— 计数在 tick 发出 HINT
            # 动作时已经加过了。回调只负责把文本放上屏。
            # 文本与上一条相同 -> 不重复上屏(否则冷场时同一句刷屏)
            if text and text == self._hint_text:
                return []
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
            # 只有"**安静下来**"才提交缓冲 —— 即距最后一条已超过窗口时长。
            # 用 _burst_last(最后一条的时刻)判断, 而不是 _burst_start,
            # 否则连续来消息时窗口会一直被重置, 攒着不提交。
            if (self._burst and self._burst_last is not None
                    and now - self._burst_last > self.cfg.replay_burst_ms / 1000.0):
                self._pending_acts.extend(self._flush_burst())
            # 先把攒着的即时动作取出来, 和本次 tick 的动作合并后一起返回
            carry, self._pending_acts = self._pending_acts, []
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

    def _riddle_failed_locked(self, now: float, why: str) -> list[EngineAction]:
        self._setting_attempts += 1
        self.last_error = why
        if self._setting_attempts >= self.cfg.riddle_max_attempts:
            # 用兜底谜题, 保证永不开天窗
            log.warning("出题连续失败(%s), 使用兜底谜题", why)
            # 轮换兜底题 —— 总用同一道, 观众一看就知道出题挂了。
            riddle = P.FALLBACK_RIDDLES[self._puzzle_index % len(P.FALLBACK_RIDDLES)]
            return self.submit_riddle(riddle[0], riddle[1],
                                      list(P.FALLBACK_HINTS),
                                      title="海龟汤", now=now)
        log.warning("出题失败(%s), 重试 %d/%d", why, self._setting_attempts,
                    self.cfg.riddle_max_attempts)
        return [EngineAction(ActionKind.RIDDLE, {
            "reason": "riddle_retry", "attempt": self._setting_attempts})]

    def _tick_qa_locked(self, now: float) -> list[EngineAction]:
        acts: list[EngineAction] = []

        # ① 在途超时 -> 退回队列(带 retry_after, 避免同一 tick 内立刻重打)
        stale = [qid for qid, t in self._inflight_at.items()
                 if now - t >= self.cfg.qa_inflight_timeout]
        for qid in stale:
            q = self._inflight.pop(qid, None)
            self._inflight_at.pop(qid, None)
            if q is None:
                continue
            q.tries += 1
            if q.tries > self.cfg.qa_retry_max:
                # 重试耗尽 —— 绝不让提问被静默吞掉(直播上就是"卡了")。
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
            else:
                q.ready_at = now + 1.0        # 1 秒后才能重派
                self._pending.insert(0, q)

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
                "transcript": self._transcript_locked(),
                "stats": dict(self._verdict_counts),
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
        # 到点就给提示(1..max_hints 格各给一条)
        if 1 <= slot <= self.cfg.max_hints and self._hints_given < slot:
            self._hints_given += 1
            acts.append(EngineAction(ActionKind.HINT, {
                "level": self._hints_given,
                "puzzle": self._puzzle, "answer": self._answer,
                "given": list(self._hints_shown)}))
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
        self._fair_clues: list = []
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
        acts.append(EngineAction(ActionKind.RIDDLE, {
            "reason": reason, "avoid": list(self._used_titles[-8:])}))
        return acts

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
            # 「未判定」**不进 transcript**: 它不是对这条提问的判断, 只是
            # 系统这次没答上。喂回模型会让它以为"未判定"是一种合法裁决,
            # 久而久之开始拿它敷衍。
            if rec.verdict != P.UNAVAILABLE:
                self._history.append(rec)
                self._trim_history_locked()
        else:
            self._qa_log.append(rec)
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
                qa_archive=[r.to_archive() for r in self._qa_log],
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
