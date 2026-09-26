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
CMD_HINT = "#提示"
HINT_TOKENS = frozenset({"提示", "给点提示", "提示一下", "hint"})
# ---- Issue #43: 旧换题指令是 deterministic tombstone ----
#
# 曾经这些词会直接 `_enter_revealing_locked(..., "skip", ...)`, 任何单个
# 观众都能靠它们结束当前题 —— 已停用。
#
# 但**不能简单地删掉识别**: 不识别的话, `#下一题` 会掉进 Answer LLM 变成
# 一条普通提问(那是另一条必须堵死的路)。所以保留这组词做**墓碑**:
# Engine 识别 -> 消费 -> 不 reveal / 不进 QA / 不调 LLM。
# 名字刻意叫 LEGACY_*: 语义是"旧命令的坟", 不是"另一条换题路径"。
LEGACY_SKIP_TOKENS = frozenset({"下一题", "下一关", "换一题", "跳过", "next"})


class ActionKind(str, Enum):
    """引擎要求外部世界做的事。"""

    RIDDLE = "riddle"         # 调 LLM 出新谜题
    ANSWER = "answer"         # 调 LLM 回答一条提问
    HINT = "hint"             # 调 LLM 生成一条提示
    REVEAL = "reveal"         # 调 LLM 生成揭晓(谜底措辞)
    AI_PLAYER = "ai_player"   # AI 玩家生成公开动作 / Host 或 Judge 裁决
    BROADCAST = "broadcast"   # 只更新状态/提示文案, 不调 LLM
    #: Issue #60: REVEALED 60s freeze 后由 Director 落盘的 closeout
    #: (评分聚合 + 主题票聚合 + selected_category)。Engine 不做 I/O。
    ROUND_CLOSEOUT = "round_closeout"
    THEME_DEMAND = "theme_demand"
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
    #: **已废弃**(Hotfix B): 早先超时会 `tries += 1` 并重派, 现在超时即
    #: fail-fast 判"未判定", 不再重派也不会再读它。保留字段是为了不动
    #: 构造点; 下次清理时连同 `qa_retry_max` 一起删。
    tries: int = 0
    #: 早于此时刻不派发。仍被派发循环读取(过滤尚未到点的条目), 但
    #: 现在没有任何路径会把它设成未来时刻 —— 重派已取消。
    ready_at: float = 0.0

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
    #   v5 起它**只是分析指标**: 有通关合同的题由代码做集合覆盖判定,
    #   不再看它。
    touched_fact_ids: Optional[list] = None
    #: **已建立**的 fact —— 经过"观众这句话 + 主持人 是/不是"之后,
    #: 普通观众已经可以把该 fact 的**完整内容**当作房间共识。
    #:
    #: 与 `touched_fact_ids` 的区别是这一层的全部意义:
    #:     touched     = 问过这个方向      (比如"她与父亲有关系吗")
    #:     established = 这个事实已公开确认为真
    #:                   (比如"门外女人是父亲的亲生女儿")
    #: v5 的通关合同就是靠它做集合覆盖判定。
    #:
    #: ⚠️ **只有真人 QA 能建立它。** 提示 / nudge / 将来 Detective 的
    #: 自动作答**绝不能**碰这个集合 —— 否则系统会自己把题解掉。
    established_fact_ids: Optional[list] = None
    #: v6: 其中**由 completion semantic verifier 复核补入**的那几条。
    #:
    #: 为什么单独记: 复盘时必须能区分
    #:     第一层 Answer 直接 established   vs   复核兜底补回
    #: 两者的比例就是"第一层 prompt 到底是太保守还是刚好"的度量 ——
    #: 没有这个字段只能看到一串没头没尾的"是"。
    #:
    #: 只进 archive(`QARec.to_archive`), **不进前端 JSON**。
    completion_verified_fact_ids: Optional[list] = None
    solution_candidate: Optional[bool] = None
    # ---- Issue #53 §5/§7: 语义类别与裁决分离 ----
    # response_kind = "verdict" -> 这条结果是一次可裁决命题的裁判,
    # verdict ∈ {是, 不是, 无关} 有意义。
    # response_kind = "rephrase" -> 模型理解了这句话, 但它没有给出可
    # 裁决的命题(开放索取/要求解释/闲聊); verdict 为空, 不建立任何
    # fact。旧数据/旧调用方缺字段 -> 默认 "verdict"(archive 向后兼容)。
    # ⚠️ "unavailable"(未判定)不是 response_kind —— 那是 status 上的
    # **技术失败**, 只能由代码产生, 模型永远不能把它当语义类别。
    response_kind: str = "verdict"
    # ---- Issue #53 §35: 判题 Prompt provenance(只进 archive) ----
    # 用于以后对比 answer-v1 vs answer-v2 的 rephrase rate /
    # unavailable rate / rescue rate。旧数据缺字段 -> 空串。
    judging_prompt_version: str = ""
    answer_prompt_version: str = ""
    candidate_recheck_prompt_version: str = ""
    completion_verify_prompt_version: str = ""


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
    #: R2: 本题内的**真实提交顺序**序号(Engine lock 内自增)。
    #:
    #: `ts` 是墙钟, 并发回包可以落在同一刻度上; 这个计数器不会。贡献链
    #: 按它排序才能保证"谁先补上那一块"是结构性正确的。`ts` 仍然保留,
    #: 它回答的是另一个问题("这条发生在直播的第几秒")。
    #:
    #: 提示/重述(qid < 0)不参与, 恒为 0。
    commit_seq: int = 0
    # ---- 裁判覆盖结果(仅落盘/复盘, 不上屏) ----
    status: str = "ok"     # ok | unavailable
    is_guess: Optional[bool] = None
    cause_hit: Optional[bool] = None
    mechanism_hit: Optional[bool] = None
    matched_atoms: Optional[list] = None
    touched_fact_ids: Optional[list] = None
    #: v5: 这条真人问答**公开确认**了哪些 fact。见 `QAResult` 的说明。
    established_fact_ids: Optional[list] = None
    #: v6: 上述集合里, **由 completion 复核补入**的那几条(只进 archive)。
    completion_verified_fact_ids: Optional[list] = None
    #: R1: 这条真人问答在**实际提交到 Engine 的那一刻**, 首次为房间
    #: 新增了哪些 `completion_fact_ids`。
    #:
    #: ## 为什么不能拿 `established_fact_ids` 倒推
    #:
    #: `established_fact_ids` 是"这条问答语义上建立了什么", 描述的是
    #: **语义**。而揭晓贡献链要回答的是"**谁**把拼图放上去的" ——
    #: 那是**时序**问题, 两者不是一回事。
    #:
    #: Answer 并发 5 条, 回包顺序与 qid 顺序无关。可能:
    #:     qid=2 先返回 -> 第一次建立 f1
    #:     qid=1 后返回 -> 又"确认"了一次 f1
    #: 而 `_qa_archive` 会按 qid 重排成 1,2,3。若揭晓时扫 archive 倒推:
    #:     seen = set()
    #:     for rec in archive: ...      # ✗ 会把 f1 的功劳记给 qid=1
    #: 真正推进进度的是 qid=2, 记账却算在 qid=1 头上 —— 公屏会表彰错人。
    #:
    #: 所以归属在 `submit_qa()` 的 Engine lock 内、状态提交那一刻就算定,
    #: 见 engine 里的 before-snapshot 差分。
    #:
    #: 语义: 本字段非空 ⟺ 这条问答**真的把房间进度往前推了一格**。
    #: "语义上又确认了一遍 f1" 若 f1 早已建立, 这里就是空。
    #:
    #: 只进 `to_archive()`, **绝不进 `to_json()`** —— 前端永远不看 fact ID。
    completion_contribution_fact_ids: Optional[list] = None
    solution_candidate: Optional[bool] = None
    # ---- Issue #53 §7/§31/§33/§34/§35 ----
    #: 语义类别(见 `QAResult.response_kind`)。旧 archive 缺字段时按
    #: legacy 语义兼容 -> 默认 "verdict"。
    response_kind: str = "verdict"
    #: 判题 Prompt provenance: 总版本 + 各次实际调用的 stage 版本。
    #: 只进 `to_archive()`(复盘/对比实验用), 不上前端。
    judging_prompt_version: str = ""
    answer_prompt_version: str = ""
    candidate_recheck_prompt_version: str = ""
    completion_verify_prompt_version: str = ""

    def to_json(self) -> dict[str, Any]:
        # 上屏用: 只给前端展示需要的字段(不暴露内部判定细节)
        #
        # ⚠️ `established_fact_ids` **刻意不进这里**: 它是通关状态,
        # 前端不需要、也不该看到内部 fact id。要复盘请用 `to_archive()`。
        # `completion_verified_fact_ids` 同理 —— 它是分析字段, 更不该外露。
        #
        # Issue #53 §31: `response_kind` **要**进这里 —— rephrase 不是
        # 任何一种 verdict, 前端必须能独立渲染"请改问法"徽章, 而不是
        # 把它显示成「无关」。
        d = {
            "qid": self.qid,
            "user_name": self.user_name,
            "text": self.text,
            "verdict": self.verdict,
            "comment": self.comment,
            "kind": self.kind,
        }
        if self.response_kind and self.response_kind != "verdict":
            d["response_kind"] = self.response_kind
        return d

    def to_archive(self) -> dict[str, Any]:
        """落盘用: 展示字段 + 覆盖结果, 供赛后复盘。"""
        d = self.to_json()
        d.update({
            "status": self.status,
            # R2: 真实提交顺序 —— archive 需要它来复盘贡献归属。
            "commit_seq": self.commit_seq,
            "is_guess": self.is_guess,
            "cause_hit": self.cause_hit,
            "mechanism_hit": self.mechanism_hit,
            "matched_atoms": self.matched_atoms,
            "touched_fact_ids": self.touched_fact_ids,
            "established_fact_ids": self.established_fact_ids,
            "completion_verified_fact_ids":
                self.completion_verified_fact_ids,
            # R1: 只有 archive 知道"这条是谁真正推进的"。
            "completion_contribution_fact_ids":
                self.completion_contribution_fact_ids,
            "solution_candidate": self.solution_candidate,
            # ---- Issue #53 §34/§35 ----
            # QA record 扩展, **不是** PuzzleSpec schema 变化 ——
            # 不 bump spec_version。response_kind 缺省即 "verdict",
            # 旧消费者把它当不存在即可。
            "response_kind": self.response_kind,
            "judging_prompt_version": self.judging_prompt_version,
            "answer_prompt_version": self.answer_prompt_version,
            "candidate_recheck_prompt_version":
                self.candidate_recheck_prompt_version,
            "completion_verify_prompt_version":
                self.completion_verify_prompt_version,
        })
        return d

    def to_line(self) -> str:
        """喂回 LLM 的一行。

        Issue #53 §33: rephrase 不是 verdict, 但也**不能**被记录成
        "→ " 后面空空如也(丢语义)。展示层 label 是"请改问法" —— 它是
        transcript 展示 label, **不是** verdict enum 的新成员。
        """
        if self.response_kind == "rephrase":
            c = f" ({self.comment})" if self.comment else ""
            return (f"[{self.qid}] {self.user_name}：{self.text} "
                    f"→ 请改问法{c}")
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
    #: 平台消息唯一 ID(Q12)。空串 = 上游没给(此时引擎走 reconnect
    #: guard 的降级去重)。**放最后** —— 这个 dataclass 是位置构造的
    #: (见 engine 里 `DanmakuItem(wid, ..., is_cmd, now, seq)`),
    #: 插在中间会让所有现有位置参数错位。
    message_id: str = ""

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
    #: **当前题的展示元数据**(难度/主题, 5 大类协议 v2 起下发)。
    #:
    #: 由 `haiguitang_protocol.public_puzzle_meta(spec)` 生成 ——
    #: presentation-safe:
    #:     v2 题      直接给 observed 五类 + 中文 label;
    #:     历史 v1    走确定性 legacy display mapping(不改原始 spec);
    #:     无分类     **空 dict** —— 前端安静隐藏, 绝不显示"未知 · 未分类"。
    #:
    #: ⚠️ 这是正式产品状态, **不进** debug。同样**绝不**携带
    #: `requested_category`(生成意图不是分类事实)/ fact id / hidden
    #: truth。SETTGING 期间必须为空(上一题的元数据已随 `_enter_setting`
    #: 清除), 不能残留"AI 正在出第 13 题, 顶部还写着第 12 题的难度"。
    puzzle_meta: dict[str, Any] = field(default_factory=dict)
    revealed_answer: str = ""               # 谜底; 非空 = 已揭晓
    solved: bool = False
    solved_by: str = ""
    leaderboard: list[dict[str, Any]] = field(default_factory=list)
    # ---- 问答流 ----
    qa_log: list[dict[str, Any]] = field(default_factory=list)
    # 落盘专用的问答流(含裁判覆盖结果)。与 qa_log 分开: 前端不需要
    # 也不该看到内部判定细节, 但复盘**必须**有 —— 否则只能看到"未中",
    # 不知道是 cause 没中还是 mechanism 没中。
    qa_archive: list[dict[str, Any]] = field(default_factory=list)
    #: R2: 揭晓贡献链 —— "这题大家是怎么一起推出来的"。
    #:
    #: ⚠️ **内容全是已经公开过的信息**: 用户名 / 提问原文 / 裁决。
    #: 内部 fact ID **绝不出现**在这里(筛选是服务端做的, 见
    #: `Engine._reveal_contributors_locked`)。
    #:
    #: 只在 REVEALING / REVEALED 阶段非空。QA 阶段刻意不发: 这些文本
    #: 本身早就公开过, 但**"哪几条刚好在通关路径上"**是新的信息 ——
    #: 提前下发等于让前端(以及任何抓包的观众)提前知道题目快解开了。
    reveal_contributors: list[dict[str, Any]] = field(default_factory=list)
    # ---- 揭晓正文(U1) ----
    #: 核心答案 —— 一句话的"原来如此"。**只在 REVEALED 阶段**下发。
    #: 与 `revealed_answer` 分开的理由: 60 秒揭晓的前 15 秒只该显示
    #: 这一句(超大字号), 完整解释随后才铺开。合成一段文本的话前端
    #: 没法把它们分开渲染, 而按标点猜切分是在赌谜底的书写格式。
    #: legacy 题没有 core_answer -> 空串, 前端 fallback 到 full。
    revealed_core_answer: str = ""
    #: 完整解释。**只在 REVEALED 阶段**下发(REVEALING 不提前给)。
    revealed_full_answer: str = ""
    #: 完整解释是否该显示(揭晓 > reveal_core_focus_seconds 后为 true)。
    #: ⚠️ 由**服务端按 phase + 经过时间**算, 不是前端自己计时 ——
    #: 前端刷新/重连后仍要与服务端一致, 而前端本地计时会从 0 重来。
    reveal_detail_visible: bool = False
    #: U2: 揭晓的**当前阶段** —— "core" | "explanation" | "contribution"。
    #:
    #: 为什么由服务端给而不是前端自己按秒数推:
    #:   1. 前端刷新/重连后本地计时会从 0 重来, 与服务端不一致;
    #:   2. 阶段边界是**配置**(reveal_core_focus_seconds /
    #:      reveal_detail_seconds), 前端不该硬编码一份副本
    #:      —— 两份拷贝迟早漂移。
    #:
    #: 60 秒分三段, 是为了让下半屏**同一时刻只有一组长内容**:
    #:   0..focus        只有核心答案(超大字号)
    #:   focus..detail   核心答案 + 完整解释(共同解谜隐藏)
    #:   detail..hold    核心答案 + 共同解谜(完整解释隐藏)
    #:
    #: 实播故障: 三块同时上屏 -> 互相争空间 -> fitReveal 只能一路缩字号
    #: -> 完整解释被压成一条矮滚动框, 而观众没有鼠标去滚直播源。
    #:
    #: ⚠️ 这个字段**只是 UI 状态**, 不含任何 hidden truth。它不携带
    #: fact id / completion id / discovery beats。REVEALED 之外恒为 ""。
    reveal_stage: str = ""
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
    # ---- AI 玩家 ----
    # 只公开计量与在途状态；reservation token / round / spec_key 永不下发。
    ai_player: dict[str, Any] = field(default_factory=dict)
    # ---- Issue #43: 点赞推进的**显式呈现事件** ----
    #
    # 形如 {"seq", "round_index", "pulses", "phase", "text"}; None = 尚无。
    # 为什么由服务端下发而不是前端从 questions_earned 的 delta 推断:
    # round 级语义下, 换题 reset / 旧公告迟到 / 累计与当题混淆都会让
    # delta 推断误判。硬要求(§16):
    #   事件可去重(seq 单调) / 有 round identity / 不跨题迟到
    #   / 一次 burst 只有一个事件(每批 pulse 一个 seq)。
    # 前端只保留**一个**待播点赞槽位, 新 seq 覆盖旧的 —— 永不无限积压。
    like_progress_notice: Optional[dict[str, Any]] = None
    # ---- Issue #60 §5: REVEALED 互动权威状态 ----
    # rating_open / theme_vote_open / 聚合票数 / freeze 后的
    # selected_category。窗口开放与否由 Engine 判, 前端不自行推断。
    reveal_interaction: dict[str, Any] = field(default_factory=dict)
    fact_progress: dict[str, Any] = field(default_factory=dict)
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
            # 5 大类协议 v2: 当前题的展示元数据(可能为空 dict -> 前端隐藏)。
            "puzzle_meta": self.puzzle_meta,
            "revealed_answer": self.revealed_answer,
            # U1: 核心答案 / 完整解释分开下发 + 是否显示细节。
            "revealed_core_answer": self.revealed_core_answer,
            "revealed_full_answer": self.revealed_full_answer,
            "reveal_detail_visible": self.reveal_detail_visible,
            # U2: 揭晓三阶段("core"/"explanation"/"contribution" 或 "")。
            # 纯 UI 状态, 不含 hidden truth。
            "reveal_stage": self.reveal_stage,
            "solved": self.solved,
            "solved_by": self.solved_by,
            "leaderboard": self.leaderboard,
            "qa_log": self.qa_log,
            "qa_total": self.qa_total,
            # R2: 揭晓贡献链(QA 阶段恒为空数组, 见字段说明)。
            "reveal_contributors": self.reveal_contributors,
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
            "ai_player": self.ai_player,
            "like_progress_notice": self.like_progress_notice,
            # Issue #60: 评分/主题投票的权威窗口状态(前端只渲染它)。
            "reveal_interaction": self.reveal_interaction,
            "fact_progress": self.fact_progress,
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
