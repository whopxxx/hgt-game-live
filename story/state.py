#!/usr/bin/env python
# coding: utf-8
"""引擎数据结构(海龟汤版)。

这里只有数据结构, 不含逻辑 —— 便于 engine/parser/llm/server 共享而不产生
循环依赖。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class Phase(str, Enum):
    """海龟汤回合阶段。

    与旧「故事+投票」的区别: **没有 COLLECTING/TALLYING**。
    提问随到随答, 没有"窗口"要开要关。QA 是 ~95% 时间的稳态。

        IDLE -> SETTING -> QA -> REVEALING -> REVEALED -> SETTING -> ...
    """

    IDLE = "idle"             # 未开始
    SETTING = "setting"       # 已请求谜面, 等 LLM 回调
    QA = "qa"                 # 主状态: 谜面上屏, 接受提问/猜谜, 持续应答
    REVEALING = "revealing"   # 已判有人猜中, 请求完整谜底
    REVEALED = "revealed"     # 谜底已上屏, 展示中
    STOPPED = "stopped"       # 终止


# 会被当成"这是提问/指令"的弹幕前缀
CMD_PREFIX = "#"

# 特殊指令(不走 LLM 提问)
CMD_NEXT = "#下一题"
CMD_HINT = "#提示"
NEXT_TOKENS = frozenset({"下一题", "下一关", "换一题", "跳过", "next"})
HINT_TOKENS = frozenset({"提示", "给点提示", "提示一下", "hint"})


class ActionKind(str, Enum):
    """引擎要求外部世界做的事。"""

    RIDDLE = "riddle"         # 调 LLM 出新谜题
    ANSWER = "answer"         # 调 LLM 回答一条提问
    HINT = "hint"             # 调 LLM 生成一条提示
    REVEAL = "reveal"         # 调 LLM 生成揭晓(谜底措辞)
    BROADCAST = "broadcast"   # 只更新状态/提示文案, 不调 LLM
    LOG = "log"


@dataclass
class EngineAction:
    """engine -> 外部世界 的 I/O 请求。engine 自己不做 I/O。"""

    kind: ActionKind
    payload: dict[str, Any] = field(default_factory=dict)


# ======================================================================
# 问答
# ======================================================================
@dataclass
class PendingQ:
    """排队等待回答的一条提问。"""

    qid: int
    user_id: str
    user_name: str
    text: str              # 展示文本(已清洗)
    ts: float = 0.0
    tries: int = 0         # 已重试次数
    ready_at: float = 0.0  # 早于此时刻不派发(重试退避)

    def to_brief(self) -> dict[str, Any]:
        return {"qid": self.qid, "user_name": self.user_name, "text": self.text}


@dataclass
class QAResult:
    """LLM 对一条提问的裁决。放这里(而非 llm.py)避免循环导入。"""

    qid: int
    verdict: str           # 是 / 不是 / 无关 / 揭晓 / 未判定
    comment: str = ""      # 模型附带的解释 -> 当作观众可见的点评
    # ---- 覆盖结果(仅日志/复盘用, 不上屏) ----
    # 裁判不再直接给 bool, 而是逐项报告覆盖情况, 由代码算 solved。
    # 保留这些字段是为了事后能复盘"为什么这条没判中"。
    status: str = "ok"              # ok / unavailable
    is_guess: Optional[bool] = None
    cause_hit: Optional[bool] = None
    mechanism_hit: Optional[bool] = None
    matched_atoms: Optional[list] = None
    # ---- Q5: 这一层就带出来的覆盖信息 ----
    # touched_fact_ids: 提问**碰到了**哪些 fact(不等于确认为真)。
    #   用来告诉提示系统"哪些方向观众已经探索过"(方案 §32/§33)。
    # solution_candidate: 粉丝是否在**尝试完整解释谜底**。只有 true 才调
    #   Final Judge —— 这是把 judge_calls/answer_calls 降下来的闸门。
    touched_fact_ids: Optional[list] = None
    solution_candidate: Optional[bool] = None


@dataclass
class QARec:
    """一条已完成的问答记录(用于展示 + 喂回 LLM 保持一致性)。

    除展示字段外, 还带**裁判覆盖结果** —— 那是复盘的关键数据:
    只有"未中"两个字是没法改 prompt 的, 必须能看到是 cause 没中还是
    mechanism 没中、命中了哪几条 atom。
    """

    qid: int
    user_name: str
    text: str              # 提问
    verdict: str
    comment: str = ""
    kind: str = "qa"       # "qa" | "hint" | "nudge"
    ts: float = 0.0
    # ---- 裁判覆盖结果(仅落盘/复盘, 不上屏) ----
    status: str = "ok"     # ok | unavailable
    is_guess: Optional[bool] = None
    cause_hit: Optional[bool] = None
    mechanism_hit: Optional[bool] = None
    matched_atoms: Optional[list] = None
    touched_fact_ids: Optional[list] = None
    solution_candidate: Optional[bool] = None

    def to_json(self) -> dict[str, Any]:
        # 上屏用: 只给前端展示需要的字段(不暴露内部判定细节)
        return {
            "qid": self.qid,
            "user_name": self.user_name,
            "text": self.text,
            "verdict": self.verdict,
            "comment": self.comment,
            "kind": self.kind,
        }

    def to_archive(self) -> dict[str, Any]:
        """落盘用: 展示字段 + 覆盖结果, 供赛后复盘。"""
        d = self.to_json()
        d.update({
            "status": self.status,
            "is_guess": self.is_guess,
            "cause_hit": self.cause_hit,
            "mechanism_hit": self.mechanism_hit,
            "matched_atoms": self.matched_atoms,
            "touched_fact_ids": self.touched_fact_ids,
            "solution_candidate": self.solution_candidate,
        })
        return d

    def to_line(self) -> str:
        """喂回 LLM 的一行。"""
        c = f" ({self.comment})" if self.comment else ""
        return f"[{self.qid}] {self.user_name}：{self.text} → {self.verdict}{c}"


@dataclass
class DanmakuItem:
    """一条上屏的弹幕(含不参与提问的)。

    seq 是**单调递增**的全局序号。快照推的是"最近 N 条"窗口, 前端靠
    seq 判断哪些是新的 —— 否则每次推送都会把整个窗口重新播一遍
    (表现为"弹幕乱飞")。
    """

    user_id: str
    user_name: str
    content: str
    is_command: bool = False
    ts: float = 0.0
    seq: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "content": self.content,
            "is_command": self.is_command,
        }


# ======================================================================
@dataclass
class Snapshot:
    """engine -> 渲染层 的完整状态。序列化后经 WebSocket 推给浏览器。

    对应两段式布局:
        上半部: puzzle —— 谜面(大字, 固定不动)
        下半部: qa_log —— 持续向上滚动的问答流
    """

    phase: Phase = Phase.IDLE
    # ---- 谜题 ----
    puzzle: str = ""                        # 谜面
    puzzle_title: str = ""
    puzzle_index: int = 0                   # 第几题(从 1 开始); 前端据此清空问答流
    puzzle_elapsed_ms: Optional[int] = None  # 本题已进行毫秒
    revealed_answer: str = ""               # 谜底; 非空 = 已揭晓
    solved: bool = False
    solved_by: str = ""
    # ---- 问答流 ----
    qa_log: list[dict[str, Any]] = field(default_factory=list)
    # 落盘专用的问答流(含裁判覆盖结果)。与 qa_log 分开: 前端不需要
    # 也不该看到内部判定细节, 但复盘**必须**有 —— 否则只能看到"未中",
    # 不知道是 cause 没中还是 mechanism 没中。
    qa_archive: list[dict[str, Any]] = field(default_factory=list)
    qa_total: int = 0                       # 含已滑出快照的条数 -> 前端只追加不重排
    pending_count: int = 0                  # 排队中(未答)的提问数 -> "AI 正在思考…"
    # ---- 提示 / 空闲 ----
    hint_count: int = 0
    hint_text: str = ""
    # ---- 倒计时 ----
    next_puzzle_ms: Optional[int] = None    # REVEALED 阶段: 距下一题的毫秒
    # 时间轴倒计时(QA 阶段): 距离"下一个事件"(下一条提示 / 揭晓)还有多久
    next_event_ms: Optional[int] = None     # 毫秒
    next_event_kind: str = ""               # "hint" | "reveal"
    next_event_label: str = ""              # "距下一条提示" / "距揭晓"
    timeline_total: int = 0                 # 时间轴总格数(= max_hints + 1)
    timeline_slot: int = 0                  # 当前走到第几格
    # ---- 兼容字段: story_index 是 puzzle_index 的别名 ----
    # 现有 app.js 用 story_index 变化来判断"新回合, 清空面板",
    # 保留别名可让那套检测逻辑无需改动即可工作。
    story_index: int = 0
    # ---- 弹幕流 ----
    danmaku: list[dict[str, Any]] = field(default_factory=list)
    notice: Optional[str] = None
    phase_hint: str = ""
    # ---- 统计 ----
    stat_questions: int = 0                 # 本题累计提问数
    stat_answered: int = 0                  # 本题累计已答数
    stat_solved: int = 0                    # 累计猜中次数
    stat_dropped: int = 0                   # 因队列满被丢弃的提问数
    stat_viewers_seen: int = 0              # 累计发言过的观众数
    # ---- 调试面板字段 ----
    model: Optional[str] = None
    model_requested: Optional[str] = None
    last_usage: Optional[dict[str, Any]] = None
    last_error: Optional[str] = None
    source: str = ""
    reconnects: int = 0
    reconnect_fails: int = 0               # 连续重建失败次数(>0 表示连接异常)
    puzzles_total: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "phase": self.phase.value,
            "puzzle": self.puzzle,
            "puzzle_title": self.puzzle_title,
            "puzzle_index": self.puzzle_index,
            "puzzle_elapsed_ms": self.puzzle_elapsed_ms,
            "revealed_answer": self.revealed_answer,
            "solved": self.solved,
            "solved_by": self.solved_by,
            "qa_log": self.qa_log,
            "qa_total": self.qa_total,
            "pending_count": self.pending_count,
            "hint_count": self.hint_count,
            "hint_text": self.hint_text,
            "next_puzzle_ms": self.next_puzzle_ms,
            "next_event_ms": self.next_event_ms,
            "next_event_kind": self.next_event_kind,
            "next_event_label": self.next_event_label,
            "timeline_total": self.timeline_total,
            "timeline_slot": self.timeline_slot,
            "story_index": self.story_index,
            "danmaku": self.danmaku,
            "notice": self.notice,
            "phase_hint": self.phase_hint,
            "stats": {
                "questions": self.stat_questions,
                "answered": self.stat_answered,
                "solved": self.stat_solved,
                "dropped": self.stat_dropped,
                "viewers_seen": self.stat_viewers_seen,
            },
            "debug": {
                "model": self.model,
                "model_requested": self.model_requested,
                "last_usage": self.last_usage,
                "last_error": self.last_error,
                "source": self.source,
                "reconnects": self.reconnects,
                "reconnect_fails": self.reconnect_fails,
                "puzzles_total": self.puzzles_total,
            },
        }
