#!/usr/bin/env python
# coding: utf-8
"""PublicPlayerCore —— **公开信息隔离**的公共层(Step 10 提取)。

## 它是什么

一个只依赖"**观众也能看到的东西**"的模型玩家/推理者。当前唯一的
消费者是 AI 试玩(`story.playtest`), 将来的 Live Detective(Step 14+)
会复用同一层。

提取出来的三样:

    sanitize_transcript()  公开 transcript 净化器(剥掉内部覆盖信息)
    build_prompt()         公开 prompt 构造器(**唯一**入口)
    ask()                  一次纯 client 工具调用

## 三条硬规则(本层存在的理由)

    ① 不持有 PuzzleWriter
    ② 不持有 PuzzleSpec
    ③ API **不接收** answer / facts / solve_atoms / signature / coverage

这三条合起来是**结构性**隔离: 不是"靠提示词请模型别看", 而是**构造上
拿不到**。`build_prompt()` 的签名里根本没有那些参数 —— 想泄漏也传不
进来。这条性质由 `tests/test_public_player.py` 用签名反射钉住。

## 为什么单独一层而不是把 playtest 拆一半

隐藏信息隔离是**直播安全**的性质(AI 玩家/侦探绝不该知道谜底), 而不是
试玩特有的。把它留在 `playtest.py` 里, 将来的 Detective 要么复制一份
(两份纪律迟早漂移), 要么反向依赖试玩模块(把"试玩"这个概念拖进直播
热路径)。抽成独立一层之后, 复用是**依赖方向正确**的。

## 与试玩的关系

`playtest.Playtester` 仍然持有 host writer(它必须跑生产 `answer()`),
但**玩家侧**全部走这一层。行为零变化 —— 提取前后 `_PLAYER_SYSTEM` /
`_PLAYER_TOOL` / prompt 文本逐字相同。

零新依赖。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger("story.public_player")

#: 每轮的动作类型。**只用于日志与策略**, 不决定是否调裁判。
MOVE_ASK = "ask"
MOVE_SOLVE = "solve"
MOVE_GIVE_UP = "give_up"
MOVES = (MOVE_ASK, MOVE_SOLVE, MOVE_GIVE_UP)

#: 强制工具调用。公开玩家没有别的输出通道。
PUBLIC_PLAYER_TOOL = {
    "name": "emit_playtest_move",
    "description": "给出你的下一步。一次只说一句话。",
    "input_schema": {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": list(MOVES),
                "description": (
                    "ask  = 问一个能缩小范围的问题\n"
                    "solve = 给出完整因果解释(认为已经能解释全部反常)\n"
                    "give_up = 确实无法继续"),
            },
            "text": {
                "type": "string",
                "description": "下一句要说的话。问句或完整解释。",
            },
        },
        "required": ["kind", "text"],
    },
}

#: 系统提示。**刻意只描述任务, 不含任何本题信息。**
PUBLIC_PLAYER_SYSTEM = """你在玩一个情境推理谜题。你会看到谜面和主持人此前公开回答过的
问答记录。你不知道谜底。

规则:
- 一次只问一个最有信息量的问题, 用来排除可能性。
- 主持人只会回答 是 / 不是 / 无关, 偶尔附一句点评。
- 当你觉得已经能解释谜面里那个反常现象时, 用 kind=solve 给出**完整的
  因果解释**(把"发生了什么"和"为什么会这样"连起来), 而不是继续问细节。
- 只有在确实推不动时才用 kind=give_up。

只输出一句下一轮要说的话。不要复述已经问过的内容。"""


def sanitize_transcript(transcript: list) -> list:
    """只留公开玩家**该看到**的字段。

    `role=puzzle` 给谜面; `role=player` 给玩家自己说过的话; `role=host`
    **只给 verdict + comment**, 剥掉 touched_fact_ids / cause_hit /
    mechanism_hit / matched_atoms 等内部覆盖信息。

    这是隔离的第一道 —— 后面 `build_prompt` 只会从这里取数据。
    """
    out = []
    for e in (transcript or []):
        if not isinstance(e, dict):
            continue
        role = e.get("role")
        if role == "puzzle":
            out.append({"role": "puzzle", "text": e.get("text", "")})
        elif role == "player":
            out.append({"role": "player", "text": e.get("text", "")})
        elif role == "host":
            line = f"主持人: {e.get('verdict', '')}"
            cm = e.get("text", "")
            if cm:
                line += f"（{cm}）"
            out.append({"role": "host", "text": line})
    return out


def build_prompt(puzzle: str, public: list) -> str:
    """**唯一**的公开 prompt 构造点。

    只接谜面 + 已公开的问答。任何 `answer` / `facts` / `solve_atoms` /
    `signature` / `blueprint` 都**不在签名里** —— 隔离是构造性的。

    `tests/test_public_player.py` 用签名反射钉住这一点: 谁往这里加一个
    隐藏区参数, 测试立刻红。
    """
    lines = [f"【谜面】{puzzle}", ""]
    if len(public or []) > 1:
        lines.append("【已公开的问答】")
        for e in public[1:]:
            if e["role"] == "player":
                lines.append(f"你问: {e['text']}")
            elif e["role"] == "host":
                lines.append(f"  {e['text']}")
        lines.append("")
    lines.append("给出你的下一步。")
    return "\n".join(lines)


def unwrap_tool_input(ti: Any) -> Optional[dict]:
    """把工具返回的 input 归一成字段字典(容忍网关套壳)。

    与 `llm._unwrap_tool_input` 同源但更薄: 公开玩家只读 kind/text 两个
    键, 不需要 llm 那套 puzzle/answer 嵌套处理。
    """
    if not isinstance(ti, dict):
        return None
    for key in ("input", "arguments", "parameters", "data"):
        inner = ti.get(key)
        if isinstance(inner, dict):
            return inner
    return ti


class PublicPlayerCore:
    """公开信息玩家 —— 只拿得到"观众也看得到的东西"。

    ## 它**不**持有什么(这就是全部重点)

        ✗ PuzzleWriter —— 连 host writer 的影子都没有
        ✗ PuzzleSpec   —— 拿不到 facts/atoms/signature
        ✗ 任何隐藏区字段

    它只持有一个 `client`(做一次强制工具调用)。这意味着**即使这个类
    被注入到直播热路径**(Step 14 的 Detective), 它也没有能力泄漏谜底。

    ## 为什么不用 PuzzleWriter

    `PuzzleWriter` 带 `_review_spec` 的实例侧信道(`_last_review_*`),
    而 Q9 已经为 live/prefetch 各留了一个实例。公开玩家侧**不需要**任何
    PuzzleWriter 的能力(只做一次强制工具调用), 所以这里直接持 `client`,
    不 new 第三个 writer —— 少一份要维护的侧信道纪律。
    """

    def __init__(self, client: Any, temperature: float = 0.0,
                 system: str = PUBLIC_PLAYER_SYSTEM,
                 tool: Optional[dict] = None):
        self.client = client
        #: 默认 0.0: 公开玩家的结论**决定一道已生成好的题能不能入池**,
        #: 所以必须可复现。同一份输入今天 pass 明天 unsolved(只因为采样
        #: 抖动)会让质量闸门没法 debug。
        self.temperature = temperature
        self.system = system
        self.tool = tool if tool is not None else PUBLIC_PLAYER_TOOL

    # ------------------------------------------------------------------
    def ask(self, puzzle: str, transcript: list) -> Optional[tuple]:
        """一次公开玩家调用。返回 `(kind, text)` 或 None(技术失败)。

        输入只有谜面与 transcript —— 内部先 `sanitize_transcript` 再
        `build_prompt`, 所以调用方就算递进来带覆盖信息的记录, 也会在
        这一层被剥掉。
        """
        public = sanitize_transcript(transcript)
        res = self.client.messages(
            system=self.system,
            user=build_prompt(puzzle, public),
            tool=self.tool,
            temperature=self.temperature,
        )
        ti = unwrap_tool_input(res.tool_input) if res is not None else None
        if res is None or res.error or not ti:
            log.warning("公开玩家调用失败: %s",
                        getattr(res, "error", "无响应"))
            return None
        kind = str(ti.get("kind") or "").strip()
        text = str(ti.get("text") or "").strip()
        if kind not in MOVES or not text:
            log.warning("公开玩家输出不合法: kind=%r text=%r", kind, text[:40])
            return None
        return kind, text[:200]
