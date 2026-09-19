#!/usr/bin/env python
# coding: utf-8
"""AI 试玩(Q10)—— 让一个**看不到谜底**的模型去猜, 用来发现"收敛不了的题"。

## 这个模块回答的问题

    一个完全看不到谜底的 AI 玩家, 只凭谜面和主持人**公开**的回答,
    能不能逐步收敛到系统认可的完整解?

## 第一验收点: 隐藏信息隔离

Player 与 Host 的可见范围必须**结构性**不同:

    ┌──── 隐藏区 ────────────────────────────────┐
    │ answer / facts / solve_atoms              │
    │ signature / blueprint / judge coverage    │
    └───────────────┬───────────────────────────┘
                    │ 只输出公开裁决(verdict + comment)
                    ↓
    谜面 ─────→ AI Player ─────→ 下一句话
                    ↑
              是/不是/无关 + 公开点评

**Player 的 prompt 里一个字都不能出现隐藏区内容。** 这不是"靠提示词
请模型别偷看" —— 是构造上不给。`_player_prompt()` 是唯一的 prompt
构造点, 它只接 `puzzle` 与 `_public_transcript()` 的输出。

## 为什么 Player 不复用 PuzzleWriter

`PuzzleWriter` 带 `_review_spec` 的实例侧信道(`_last_review_*`), 而
Q9 已经为 live/prefetch 各留了一个实例。Player 侧**不需要**任何
PuzzleWriter 的能力(它只做一次强制工具调用), 所以这里直接持
`client`, 不 new 第三个 writer —— 少一份要维护的侧信道纪律。

Host 侧则**相反**: 必须复用生产 `writer.answer(...)`, 由调用方把
它自己那个实例传进来(`host_writer`)。这样 Host 的可见性/判定链与直播
**逐字相同**, 试玩才有意义。

## 为什么 Host 必须走生产 `answer()` 而不是直接调 `judge()`

因为 `answer()` 里有一道 `solution_candidate` 闸门: 只有模型认为
"观众在尝试完整解释谜底"时才会送 Final Judge。若试玩绕过它直接判,
我们就测不到这条闸门 —— 而"AI 玩家已经说全了, 但生产链没把它送进
裁判"恰是直播里真实会发生、且试玩**应该**暴露的故障。

## v1 不加提示

直播真实的提示路径是 Q6 的 fact-aware 动态 hint(看 touched facts +
泄底检查), 与 `spec.hints` 不等价。喂 `spec.hints[0]` 测的是一条直播
里不存在的路径。Q10 v1 只回答"正常 QA 本身有没有收敛路径"。

## 结论的强度(必须写进文档)

AI 试玩 **不是**"人类一定觉得好玩/能解"的证明。同一个模型可能凭训练
语料或套路猜中(经典题尤甚)。它是一个**发现明显不可收敛题**的闸门,
不是最终质量评分器。

零新依赖。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import parser as P
from .public_player import (
    MOVE_ASK, MOVE_GIVE_UP, MOVE_SOLVE, MOVES,            # noqa: F401
    PUBLIC_PLAYER_SYSTEM, PUBLIC_PLAYER_TOOL,
    PublicPlayerCore, build_prompt as _public_prompt,
    sanitize_transcript as _sanitize, unwrap_tool_input,
)

log = logging.getLogger("story.playtest")

#: 试玩结局。**技术故障与"题烂"必须分开** —— 前者不该记进质量统计。
PASS = "pass"                  # 在预算内说出完整解
UNSOLVED = "unsolved"          # 用完轮数 / 主动放弃, 没猜中
UNAVAILABLE = "unavailable"    # 技术故障(网关/解析), **不评价这道题**
INTERRUPTED = "interrupted"    # 直播突然变忙, 主动让路

#: 结局 -> 后台策略。**唯一定义处**, 补池(Q10c)直接读它, 不要各处
#: 重推一遍 —— "四种结局要不要退避"这种表散在两处就一定会漂移。
#:
#: 为什么 UNSOLVED 也要退避: 关键不是"惩罚失败", 是**限流**。一次
#: playtest 最坏会连打 2N 次 LLM 调用; 若 UNSOLVED 后下一拍立刻再生成
#: 一题再试玩, 即使有 single-flight, 也会排成一条连续的昂贵调用链。
#:
#: 为什么 INTERRUPTED 不退避: 它只是"直播忙, 我主动让路", 不是失败。
#: 计退避会让运维数据把"直播活跃"误读成"试玩大量失败"。
OUTCOME_POLICY = {
    PASS:        {"drop": False, "backoff": False, "counted_fail": False},
    UNSOLVED:    {"drop": True,  "backoff": True,  "counted_fail": True},
    UNAVAILABLE: {"drop": True,  "backoff": True,  "counted_fail": True},
    INTERRUPTED: {"drop": True,  "backoff": False, "counted_fail": False},
}

#: Player 每轮的动作类型。**只用于日志与策略**, 不决定是否调裁判 ——
#: ask 与 solve 的文本都走同一条生产 `answer()`。否则会出现"玩家其实
#: 已经猜全了, 但因为 action 写成 ask 所以系统故意不判"的假环境。
#:
#: Step 10: 定义移到 `public_player`(公共层), 这里保留同名别名 ——
#: 既有的 `from story.playtest import MOVE_ASK` 调用点不受影响。
_MOVES = MOVES

#: 强制工具调用。Step 10: 定义移到 `public_player`(公共层),
#: 这里保留同名别名供既有调用点使用。
_PLAYER_TOOL = PUBLIC_PLAYER_TOOL

#: Player 的系统提示。**刻意只描述任务, 不含任何本题信息。**
#: Step 10: 定义移到 `public_player`(公共层), 保留同名别名。
_PLAYER_SYSTEM = PUBLIC_PLAYER_SYSTEM


# ======================================================================
# 结果
# ======================================================================
@dataclass
class PlaytestResult:
    """一次试玩的结局。

    `turns` 是**实际用掉**的轮数(不是上限)。`transcript` 只含 Player
    真正看到过的内容 —— 对外可以存进 metrics, 内部 fact id / matched
    atoms **绝不混进来**。
    """

    status: str = UNAVAILABLE
    turns: int = 0
    final_text: str = ""
    duration_ms: int = 0
    transcript: list = field(default_factory=list)   # [{role, text, verdict}]
    error: str = ""
    #: UNSOLVED 的细分原因: "give_up" / "max_turns"。**只进日志与 metrics,
    #: 不参与策略** —— 两者的处理完全一样(丢弃 + 退避)。留着是因为
    #: "玩家主动放弃"和"用光轮数"对读题的人意义不同。
    reason: str = ""

    @property
    def passed(self) -> bool:
        return self.status == PASS

    def policy(self) -> dict:
        """本结局对应的后台策略。未知 status 一律按**最保守**处理:
        丢弃 + 退避 + 计入失败 —— 出了表就说明代码有 bug, 此时宁可
        当作失败停下, 也不要漏掉退避而开始连续烧调用。
        """
        return OUTCOME_POLICY.get(
            self.status,
            {"drop": True, "backoff": True, "counted_fail": True})

    def to_metrics(self) -> dict:
        """落进 `spec.metrics["playtest"]` 的形状。

        用 metrics 而不是给 PuzzleSpec 加一级字段: metrics 的 round-trip
        已经在 Q8 验证过无损, 不必再扩大一次重建面。

        `transcript` **不再截断** —— 它最多 `max_turns + 1` 条, 每条
        Player 侧已截 200 字。截它会破坏 round-trip 可比较性。
        """
        return {
            "status": self.status,
            "turns": self.turns,
            "duration_ms": self.duration_ms,
            "final_text": self.final_text[:200],
            "transcript": self.transcript,
            "reason": self.reason,
        }


# ======================================================================
# 试玩器
# ======================================================================
class Playtester:
    """跑一次 AI 试玩。**只在后台候选上运行, 绝不在直播热路径。**

    Args:
        player_client: **纯传输层** client, 只用来调 Player。可以是 None
            (那时试玩直接 unavailable —— 不生成假结局)。
        host_writer: 生产 `PuzzleWriter` 实例, 由调用方传入(通常是
            prefetcher 自己那个), 用来跑 Host 的裁决/裁判。
        should_continue: 无参谓词, **每一次 LLM 调用之前**都查一次。
            返回 False 就立刻以 `interrupted` 退出(不 backoff)。
        max_turns: 轮数上限。
        clock: 注入时钟(测试用)。
    """

    def __init__(self, player_client: Any, host_writer: Any,
                 should_continue: Optional[Callable[[], bool]] = None,
                 max_turns: int = 10,
                 clock: Callable[[], float] = time.monotonic):
        self.client = player_client
        self.host = host_writer
        # Step 10: 玩家侧走公共层。`PublicPlayerCore` **不持有**
        # PuzzleWriter / PuzzleSpec, API 也不收隐藏区字段 —— 隔离是
        # 构造性的。这里传的就是原来那个 player_client, 行为零变化。
        self.player = PublicPlayerCore(player_client)
        self._should_continue = should_continue or (lambda: True)
        self._max_turns = max(1, int(max_turns))
        self._clock = clock

    # ------------------------------------------------------------------
    def run(self, spec: Any) -> PlaytestResult:
        """试玩一道题。**绝不抛异常** —— 任何意外都变成 unavailable。"""
        t0 = self._clock()
        try:
            r = self._run_inner(spec)
        except Exception as e:                  # noqa: BLE001
            log.exception("试玩异常: %s", e)
            r = PlaytestResult(status=UNAVAILABLE, error=str(e))
        r.duration_ms = int((self._clock() - t0) * 1000)
        return r

    def _run_inner(self, spec: Any) -> PlaytestResult:
        puzzle = getattr(spec, "puzzle", "") or ""
        answer = getattr(spec, "answer", "") or ""
        if not puzzle or not answer:
            return PlaytestResult(status=UNAVAILABLE, error="spec 缺谜面或谜底")
        if self.client is None or self.host is None:
            return PlaytestResult(status=UNAVAILABLE, error="没有 client/writer")

        tr = PlaytestResult(transcript=[{"role": "puzzle", "text": puzzle}])
        for turn in range(1, self._max_turns + 1):
            # ---- 每次 LLM 调用之前都让路 ----
            # Q9 只在任务开始时查一次零压力; 试玩里一个任务会连打
            # 2N 次调用, 持续更久, 所以必须每步重查。
            if not self._safe_continue():
                tr.status = INTERRUPTED
                return tr

            move = self._ask_player(puzzle, tr.transcript)
            if move is None:
                tr.status = UNAVAILABLE
                tr.error = "Player 调用失败"
                return tr
            kind, text = move
            tr.turns = turn
            tr.final_text = text
            tr.transcript.append({"role": "player", "text": text,
                                  "kind": kind})

            if kind == MOVE_GIVE_UP:
                tr.status = UNSOLVED
                tr.reason = "give_up"
                return tr

            # ---- Host: **走生产 answer()**, 不直接调 judge ----
            if not self._safe_continue():
                tr.status = INTERRUPTED
                return tr
            verdict, comment, err = self._host_answer(spec, puzzle, answer,
                                                      text, turn)
            if err:
                tr.status = UNAVAILABLE
                tr.error = err
                return tr
            tr.transcript.append({"role": "host", "text": comment,
                                  "verdict": verdict})

            if verdict == P.SOLVE:
                tr.status = PASS
                return tr

        tr.status = UNSOLVED
        tr.reason = "max_turns"
        return tr

    # ------------------------------------------------------------------
    def _safe_continue(self) -> bool:
        """让路谓词。异常一律当"不该继续" —— 宁可中断也不影响直播。"""
        try:
            return bool(self._should_continue())
        except Exception:                       # noqa: BLE001
            log.exception("should_continue 异常, 当作中断")
            return False

    def _ask_player(self, puzzle: str,
                    transcript: list) -> Optional[tuple]:
        """一次 Player 调用。返回 `(kind, text)` 或 None(技术失败)。

        Step 10: 实现搬到 `PublicPlayerCore.ask()` —— 净化、prompt 构造、
        工具调用、返回值校验全在公共层。这里只做委托。

        语义**逐字保持**: `temperature=0.0`(试玩结论决定一道已生成好的
        题能不能入池, 必须可复现); "每轮别问同一句"靠 transcript 本身
        在变 + system prompt 要求不复述, 不靠采样随机 —— 若 temperature=0
        下仍反复问同一句, 那**本身就是有价值的结论**(公开信息没能给出
        新推理方向), 不该用随机采样掩盖掉。

        以后要做多样化试玩, 正确做法是离线跑多个固定策略/角色, 而不是
        把一个 Player 调到 0.7 —— 那样多样性不可复现。
        """
        return self.player.ask(puzzle, transcript)

    def _host_answer(self, spec: Any, puzzle: str, answer: str,
                     text: str, turn: int) -> tuple:
        """跑生产 `answer()`。返回 `(verdict, comment, error)`。

        `judge_solve=True` 且带 `spec` —— 与直播逐字相同, 连
        `solution_candidate` 闸门一起测。

        ⚠️ **必须把通关合同一起传下去**。不传的话, 一道 v6 题在这里会
        走 legacy 分支(有合同却没有 `completion_fact_ids` -> `has_contract`
        为假), 于是它可能吐出 `P.SOLVE` —— 试玩就会按**直播里根本不
        存在**的路径判 PASS, 分数与真实语义对不上。
        """
        try:
            results, err = self.host.answer(
                puzzle=puzzle, answer=answer, transcript=[], qid=turn,
                user_name="AI玩家", text=text,
                judge_solve=True, spec=spec,
                completion_fact_ids=list(
                    getattr(spec, "completion_fact_ids", None) or []),
                core_answer=str(getattr(spec, "core_answer", "") or ""))
        except Exception as e:                  # noqa: BLE001
            log.exception("Host answer 异常: %s", e)
            return "", "", str(e)
        if not results:
            return "", "", err or "Host 解析不出裁决"
        r0 = results[0]
        return r0.verdict, (r0.comment or ""), ""


# ======================================================================
# prompt 构造(唯一入口 —— 隔离就靠它)
# ======================================================================
#: Step 10: 实现移到 `public_player.sanitize_transcript`。
#: 保留模块级名字 —— 既有调用点与测试不受影响。
_public_transcript = _sanitize


#: Step 10: 实现移到 `public_player.build_prompt`(唯一的 prompt
#: 构造点在那边的签名上就**收不到**隐藏区字段)。保留模块级名字。
_player_prompt = _public_prompt


#: Step 10: 实现移到 `public_player.unwrap_tool_input`。保留模块级名字。
_unwrap = unwrap_tool_input
