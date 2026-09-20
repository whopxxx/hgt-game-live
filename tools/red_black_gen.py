#!/usr/bin/env python
# coding: utf-8
"""红汤 / 黑汤 生成风格实验 —— 三臂对照 (A current / B style-first / C story-first)。

## 这不是生产改造

任务书(red-black-soup)的第一条硬约束: **只做审计 + 实验, 不改 production**。
本脚本因此:

    * **不** import 任何 production 的 prompt 去改它;
    * A 臂**原样引用** `story.llm.KEYWORD_IDEA_SYSTEM` / `_TOOL_KEYWORD_IDEA`
      / `story.llm._keywords_prompt` —— **不复制**, 复制会立刻产生第二份
      真相(生产改了实验不知道);
    * B / C 臂的 prompt **只住在这个文件里**, 不写回 `story/`。

## 三臂

    A = CURRENT   冻结生产 Stage A。8 组固定关键词, 每组 1 次。
    B = STYLE     一次调用, 但 prompt 的注意力放在"红汤/黑汤 + 完整故事
                  + 截取最想问的切片"。4 red + 4 black。
    C = STORY     两次调用 —— Call 1 只创作隐藏故事(不写谜面),
                  Call 2 只从故事里截取汤面。4 red + 4 black,
                  其中再分 C1(给关键词) / C2(不给关键词) 各 4。

## 冻结纪律(继承 G5 / G-SF / G9 的教训)

    * 关键词在**任何模型调用之前**抽定并写进 `run.json.groups` + md5;
    * 每格**恰好一次** LLM 调用, 不 retry、不 best-of-N、不看结果重抽;
    * `run.json` **先写盘再渲染**(G9-R2 的教训: 渲染抛异常会把已完成的
      调用全丢掉);
    * 日志显式 utf-8(`logging.basicConfig` 不传 encoding = 本机 GBK =
      混合编码, 没有任何单一 decode 能读通 —— G5 的实测教训)。

## 运行

    .venv/Scripts/python.exe tools/red_black_gen.py --draw-only
    .venv/Scripts/python.exe tools/red_black_gen.py --run
    .venv/Scripts/python.exe tools/red_black_gen.py --report-only
    .venv/Scripts/python.exe tools/red_black_gen.py --gate-only
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import random
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

OUT_DIR = os.path.join(ROOT, "data", "red_black_generation_experiment")
RUN_JSON = os.path.join(OUT_DIR, "run.json")
RUN_LOG = os.path.join(OUT_DIR, "run.log")

#: 抽词 seed —— 与 G5(20260920) / G-SF(20260921) / G9(20260922) 都不同,
#: 换一批新词(任务书 §十四: 必须在跑之前冻结, 不能看结果重挑)。
EXPERIMENT_SEED = 20260923

#: 每组恰好 1 次调用。
N_STYLEFIRST = 8           # B: 4 red + 4 black
N_STORYFIRST = 8           # C: 4 red + 4 black
N_CURRENT = 8              # A

TAG = "RB-GEN"
log = logging.getLogger(TAG)


# ======================================================================
# 一、日志(显式 utf-8 —— 见模块 docstring)
# ======================================================================
def _setup_log() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    log.setLevel(logging.INFO)
    log.propagate = False
    h = logging.FileHandler(RUN_LOG, encoding="utf-8", errors="replace")
    h.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"))
    log.addHandler(h)
    ch = logging.StreamHandler(
        io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                         errors="replace", line_buffering=True))
    ch.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(ch)


# ======================================================================
# 二、关键词: 跑之前抽定、冻结、算 md5
# ======================================================================
def draw_groups() -> dict:
    """从**生产**词库抽 A/B/C 三臂要用的关键词组。

    ⚠️ 用生产的 `load_bag` + `KeywordBag.draw()`, 不自己写一份抽词 ——
    否则测到的就不是"生产抽出来的词"。

    `bag.draw()` 每次**在 bag 内部推进 rng**, 所以顺序抽 24 次得到的
    就是一组**确定**的词对(同 seed 同结果)。抽完立刻算 md5 冻结。
    """
    from story.keyword_seed import load_bag
    from story.keyword_corpus import DEFAULT_CORPUS_PATH

    path = os.path.join(ROOT, DEFAULT_CORPUS_PATH)
    bag, meta = load_bag(path, EXPERIMENT_SEED)

    # A(8) + B(8) + C1(4) 都要关键词 = 20 组;C2 的 4 组**明确无关键词**。
    need = N_CURRENT + N_STYLEFIRST + 4
    pairs = []
    for _ in range(need):
        d = bag.draw()
        pairs.append(list(d["keywords"]))

    groups = {
        "corpus_path": DEFAULT_CORPUS_PATH.replace("\\", "/"),
        "corpus_version": meta.get("corpus_version", ""),
        "keyword_count": meta.get("keyword_count", 0),
        "seed": EXPERIMENT_SEED,
        "current": [{"index": i + 1, "keywords": p}
                    for i, p in enumerate(pairs[:N_CURRENT])],
        "stylefirst": [
            {"index": i + 1,
             "tone": "red" if i % 2 == 0 else "black",
             "keywords": p}
            for i, p in enumerate(pairs[N_CURRENT:N_CURRENT + N_STYLEFIRST])
        ],
        "storyfirst": [
            {"index": i + 1,
             "tone": "red" if i % 2 == 0 else "black",
             # 前 4 组有关键词(C1), 后 4 组明确无关键词(C2)
             "keywords": (pairs[N_CURRENT + N_STYLEFIRST + i]
                          if i < 4 else []),
             "arm": "C1" if i < 4 else "C2"}
            for i in range(N_STORYFIRST)
        ],
    }
    return groups


def groups_md5(groups: dict) -> str:
    """关键词组的指纹 —— 只对**词本身**取哈希, 不含抽取元数据。"""
    payload = json.dumps({
        "current": [g["keywords"] for g in groups["current"]],
        "stylefirst": [g["keywords"] for g in groups["stylefirst"]],
        "storyfirst": [g["keywords"] for g in groups["storyfirst"]],
        "seed": groups["seed"],
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


# ======================================================================
# 三、B 臂: Style-first 单调用
# ======================================================================
#: 黑名单 —— 任务书 §十五 / §十六 明令禁止写进 prompt 的东西。
#: **做成代码级断言**, 不是靠自觉: 一条"红汤=一定死人"写进 prompt,
#: 测到的就不再是"红黑调性", 而是"红黑调性 + 一个固定模板"。
_BANNED_IN_EXPERIMENT_PROMPT = (
    "第三人称", "必须问句", "1~3 句", "2~3 句", "单机关",
    "Blueprint", "taxonomy", "quota", "反转 N 次",
    "观察到的线索", "event_chain", "core_truth",
    "solve_atom", "completion_fact", "discovery_beat", "signature",
    "一定死人", "一定有鬼", "必须包含", "最少出现",
)

STYLE_FIRST_SYSTEM = """你是一个中文「海龟汤」(情境推理谜题)作者。
观众会把你写的这一小段当成一个**现场切片**, 然后用是 / 否提问把它
问穿。全程中文。

## 你要写的是红汤或黑汤

本次的调性是 **{tone_label}**。

{tone_guide}

调性是**内容倾向**, 不是故事模板。不要因为"这是红汤"就塞一个死人,
也不要因为"这是黑汤"就塞一个鬼。

## 创作顺序: 先想完整故事, 再截取汤面

**第一步: 先想一个完整、诡异、值得被揭晓的隐藏故事。**
它必须比汤面写出来的东西**多得多** —— 人物是谁、他们什么关系、
过去发生了什么、某个东西真正的用途、世界运行的规则、事情的真实顺序,
这些都可以是观众**完全不知道**的。

**第二步: 从这个完整故事里, 截取最让人想问"到底怎么回事"的一个切片。**
谜面**不是故事的摘要**。它是整个故事里**最怪的那一幕**。

允许(而且鼓励)故意隐藏: 人物身份 / 人物关系 / 过去发生的关键事 /
某个物体的真实用途 / 世界规则 / 时间顺序 / 空间关系 /
人物知道而玩家不知道的信息。

**只要这些隐藏的东西之后都能通过是 / 否提问被确认, 它就是公平的。**

## 汤面要制造"问题欲", 不是交代信息

写完自问: **观众读完会不会立刻想问至少三个方向?**

例如: 他是谁? 那东西真的是那个东西吗? 有人死了吗? 时间重要吗?
这是真实世界吗? 他为什么这么做? 这个行为是在保护谁, 还是在伤害谁?

## 汤底可以比汤面丰富很多

**不要**要求自己"汤底主要只能把汤面已经写出的事实串一遍"。
唯一的要求是: **汤底里的关键事实, 在游戏过程中可以被问出来。**

## 硬底线

* 谜面和谜底不能自相矛盾;
* 谜底的核心逻辑要真的成立(方向 / 时间 / 数量 / 因果, 自己走一遍);
* 不依赖冷门专业知识, 普通观众靠常识能听懂;
* 不依赖外部图片 / 音频 / 软件;
* 适合直播: 能被念出来, 能被弹幕追问;
* **不靠血腥细节或残酷过程本身制造刺激**, 不用自伤 / 自杀细节当卖点;
* 谜底保持简洁 —— 揭晓时会直接念给观众, 建议不超过 260 字。

按工具字段输出。
"""

_TONE_GUIDE = {
    "red": (
        "红汤: 死亡、失踪、严重后果、危险事件、令人不安的过去都可以成为\n"
        "故事的一部分。\n"
        "但 **\"有人死了\"本身不是反转**。以下这些**不足以**单独构成核心:\n"
        "原来那个人已经去世 / 原来是在祭奠 / 原来以前发生过事故 /\n"
        "原来家人一直很悲伤 / 原来他因为愧疚才这样做。\n"
        "如果谜底只是这些, 那还是弱题 —— 死亡必须是**故事里的一个事实**,\n"
        "而不是**全部的谜底**。"
    ),
    "black": (
        "黑汤: 真相更阴暗、心理冲击更强、人物行为或关系揭晓后令人不舒服、\n"
        "世界规则更诡异, 结尾有明显余味。\n"
        "可以出现超自然或现实中不可能的设定, 只要**内部逻辑自洽**。\n"
        "**不要**靠详细的残酷过程制造刺激, 也**不要**使用自伤 / 自杀细节\n"
        "作为卖点。\n"
        "黑汤要的是\"原来是这样……越想越不对劲\", 不是\"描述得越惨越黑\"。"
    ),
}

#: B 臂的 tool schema —— 与生产 A 臂**不同**: 没有 core_truth /
#: observed_clues / event_chain 三个脚手架字段。这正是 B 臂要测的变量:
#: 去掉"先交结构"的形状之后, 模型还会不会自由地想故事。
_TOOL_STYLE = {
    "name": "emit_red_black_idea",
    "description": "先想一个完整故事, 再截取其中最怪的一幕写成海龟汤",
    "input_schema": {
        "type": "object",
        "properties": {
            "hidden_story": {
                "type": "string",
                "description": ("完整隐藏故事 —— 比谜面写出来的多得多。"
                                "人物/关系/过去/物体真实用途/世界规则/"
                                "真实顺序都在这里说清。"),
            },
            "core_reveal": {
                "type": "string",
                "description": "一句话: 揭晓时观众最意外的那个点是什么",
            },
            "puzzle": {
                "type": "string",
                "description": ("汤面: 从完整故事里截取的**最怪的一幕**。"
                                "不是故事摘要, 不提前解释关键背景。"),
            },
            "answer": {
                "type": "string",
                "description": ("汤底: 揭晓时念给观众。可以比汤面丰富很多。"
                                "建议不超过 260 字。"),
            },
        },
        "required": ["hidden_story", "core_reveal", "puzzle", "answer"],
    },
}


def _style_user_prompt(keywords, tone: str) -> str:
    """B 臂的 user message。

    ⚠️ 有词时**形状与生产一致**(`关键词：X，Y`) —— 换掉它等于换掉被测
    变量(生产 A 臂就是这个形状)。无词时明确说"没有关键词"。
    """
    words = [str(k).strip() for k in (keywords or []) if str(k).strip()]
    if words:
        return ("关键词：" + "，".join(words) + "\n\n"
                "请围绕这个调性写一道中文海龟汤。")
    return ("本题**没有**关键词, 你自己选一个最有意思的方向。\n\n"
            "请围绕这个调性写一道中文海龟汤。")


# ======================================================================
# 四、C 臂: Story-first 两次调用
# ======================================================================
STORY_CALL1_SYSTEM = """你是一个中文悬疑故事作者。全程中文。

本次要写的调性是 **{tone_label}**。

{tone_guide}

## 这一步**只**创作隐藏故事

不要写谜面。不要写线索。不要想 schema。不要想审稿。
**只回答一件事: 这个隐藏故事本身值不值得做成一道海龟汤?**

故事要完整、诡异、有可揭晓性。人物是谁、他们什么关系、过去发生了什么、
某个东西真正的用途、世界运行的规则、事情的真实顺序 —— 全部想清楚,
并且**写下来**(后面有人要从它里面截取谜面)。

要求:

* 核心必须有一个**真正让人意外**的揭晓点, 而不是"原来是一场误会";
* 不要用"原来在拍戏 / 原来是道具 / 原来他看错了 / 原来是普通动物 /
  原来只是巧合 / 原来记忆有问题 / 原来有罕见疾病 / 原来有冷门物理现象 /
  原来有特殊职业规定"这一类**弱揭晓**作为全部核心;
* 不依赖冷门专业知识;
* 不靠血腥细节或残酷过程本身制造刺激, 不用自伤 / 自杀细节当卖点。
"""

_TOOL_STORY = {
    "name": "emit_hidden_story",
    "description": "创作一个完整、诡异、值得揭晓的隐藏故事(不写谜面)",
    "input_schema": {
        "type": "object",
        "properties": {
            "hidden_story": {
                "type": "string",
                "description": ("完整故事。人物/关系/过去/物体真实用途/"
                                "世界规则/真实顺序都写清。这是后面截取"
                                "谜面的**唯一**素材。"),
            },
            "core_reveal": {
                "type": "string",
                "description": "一句话: 揭晓时观众最意外的那个点",
            },
            "interesting_unknowns": {
                "type": "array", "minItems": 3, "maxItems": 6,
                "items": {"type": "string"},
                "description": ("观众第一眼**不可能知道**、但之后应该能靠"
                                "是/否提问确认的关键事实(至少 3 条)。"),
            },
        },
        "required": ["hidden_story", "core_reveal", "interesting_unknowns"],
    },
}

SURFACE_CALL2_SYSTEM = """你是一个中文「海龟汤」(情境推理谜题)编辑。全程中文。

给你一个**已经写好的完整隐藏故事**。你的工作**不是**总结它, 而是从
它里面**截取一幕**做成汤面。

## 截取原则

* 汤面是完整故事里**最怪、最让人想问"到底怎么回事"的一个切片**;
* **不要**总结完整故事;
* **不要**提前解释关键背景;
* **不要**为了"公平"把所有推理证据都塞进汤面 ——
  海龟汤的信息**本来就应该**靠观众提问问出来;
* 允许隐藏: 人物身份 / 关系 / 过去 / 物体真实用途 / 世界规则 /
  时间顺序 / 空间关系 / 人物知道而玩家不知道的信息。

## 公平的判据

**不是**"汤面写全了证据", 而是: **关键事实都能通过是 / 否提问确认**。
所以你要**列出**那些"汤面没写、但观众应该能问出来"的关键事实。

## 硬底线

* 汤面与汤底不能自相矛盾;
* 汤底的核心逻辑要真的成立;
* 不依赖冷门专业知识;
* 适合直播: 能被念出来, 能被弹幕追问;
* 汤底揭晓时直接念给观众, 建议不超过 260 字。

按工具字段输出。
"""

_TOOL_SURFACE = {
    "name": "emit_surface",
    "description": "从完整故事里截取最怪的一幕作为汤面",
    "input_schema": {
        "type": "object",
        "properties": {
            "puzzle": {
                "type": "string",
                "description": ("汤面: 完整故事里最怪的一幕。不是故事摘要, "
                                "不提前解释关键背景。"),
            },
            "answer": {
                "type": "string",
                "description": ("汤底: 揭晓时念给观众, 建议不超过 260 字。"),
            },
            "askable_hidden_facts": {
                "type": "array", "minItems": 2, "maxItems": 6,
                "items": {"type": "string"},
                "description": ("汤面**没有写**, 但观众应该能够通过提问"
                                "确认的关键事实(至少 2 条)。用来验证"
                                "『隐藏 ≠ 不公平』。"),
            },
        },
        "required": ["puzzle", "answer", "askable_hidden_facts"],
    },
}


# ======================================================================
# 五、Prompt 卫生断言(模块导入时执行)
# ======================================================================
def _assert_prompt_hygiene() -> None:
    """实验 prompt 里**不许**出现生产那套形状词。

    这一类污染**看不见**: 一条"第三人称"写进创作 prompt, 测到的就不再
    是"红黑调性", 而是"红黑调性 + 一条人称硬约束"。
    """
    blob = (STYLE_FIRST_SYSTEM + "".join(_TONE_GUIDE.values())
            + STORY_CALL1_SYSTEM + SURFACE_CALL2_SYSTEM
            + json.dumps(_TOOL_STYLE, ensure_ascii=False)
            + json.dumps(_TOOL_STORY, ensure_ascii=False)
            + json.dumps(_TOOL_SURFACE, ensure_ascii=False))
    hits = [w for w in _BANNED_IN_EXPERIMENT_PROMPT if w in blob]
    if hits:
        raise AssertionError(
            "实验 prompt 里出现了生产形状词(会污染实验变量): %r" % (hits,))


_assert_prompt_hygiene()


# ======================================================================
# 六、调用
# ======================================================================
def _call(client, system: str, user: str, tool: dict, temperature=None):
    """一次强制工具调用。返回 `(dict|None, meta)`。**不重试**。"""
    t0 = time.monotonic()
    res = client.messages(system, user, max_tokens=4000, tool=tool,
                          temperature=temperature)
    ms = int((time.monotonic() - t0) * 1000)
    from story.llm import _unwrap_tool_input
    d = _unwrap_tool_input(res.tool_input) if res.tool_input else None
    meta = {
        "latency_ms": ms,
        "model": getattr(res, "model", ""),
        "ok": isinstance(d, dict) and bool(d),
        "error": (getattr(res, "error", "") or "")[:200],
        "usage": getattr(res, "usage", None) or {},
    }
    return (d if isinstance(d, dict) else None), meta


def _run_current(writer, g) -> dict:
    """A 臂: **原样** call 生产 `writer.gen_keyword_idea()`。

    刻意不复制 prompt —— 复制会立刻产生第二份真相。
    """
    rec = {"arm": "A", "index": g["index"], "tone": "",
           "keywords": list(g["keywords"]), "calls": []}
    t0 = time.monotonic()
    idea = writer.gen_keyword_idea(list(g["keywords"]))
    rec["latency_ms"] = int((time.monotonic() - t0) * 1000)
    if idea is None:
        rec["ok"] = False
        rec["error"] = "Stage A 未成题(见 run.log)"
        return rec
    if idea.get("interrupted"):
        rec["ok"] = False
        rec["error"] = "interrupted"
        return rec
    rec["ok"] = True
    rec["stage_a"] = {
        "core_truth": idea.get("core_truth", ""),
        "observed_clues": idea.get("observed_clues", []),
        "event_chain": idea.get("event_chain", []),
        "title": idea.get("title", ""),
        "puzzle": idea.get("puzzle", ""),
        "answer": idea.get("answer", ""),
    }
    return rec


def _run_stylefirst(client, g, temperature) -> dict:
    """B 臂: 一次调用, 注意力放在"红黑调性 + 完整故事 + 截取切片"。"""
    tone = g["tone"]
    sys_p = STYLE_FIRST_SYSTEM.format(tone_label=("红汤" if tone == "red"
                                                  else "黑汤"),
                                      tone_guide=_TONE_GUIDE[tone])
    user = _style_user_prompt(g["keywords"], tone)
    d, meta = _call(client, sys_p, user, _TOOL_STYLE, temperature)
    rec = {"arm": "B", "index": g["index"], "tone": tone,
           "keywords": list(g["keywords"]), "calls": [meta],
           "latency_ms": meta["latency_ms"]}
    if not d:
        rec["ok"] = False
        rec["error"] = meta["error"] or "空 tool_input"
        return rec
    rec["ok"] = True
    rec["result"] = {
        "hidden_story": str(d.get("hidden_story") or ""),
        "core_reveal": str(d.get("core_reveal") or ""),
        "puzzle": str(d.get("puzzle") or ""),
        "answer": str(d.get("answer") or ""),
    }
    return rec


def _run_storyfirst(client, g, temperature) -> dict:
    """C 臂: Call 1 只创作隐藏故事 → Call 2 只截取汤面。"""
    tone = g["tone"]
    c1_sys = STORY_CALL1_SYSTEM.format(
        tone_label=("红汤" if tone == "red" else "黑汤"),
        tone_guide=_TONE_GUIDE[tone])
    words = [str(k).strip() for k in (g["keywords"] or []) if str(k).strip()]
    if words:
        c1_user = ("关键词：" + "，".join(words) + "\n\n"
                   "请先写出这个隐藏故事。关键词自然进入故事即可, "
                   "不必两个都成为机关。")
    else:
        c1_user = "本题**没有**关键词, 你自己选一个最有意思的方向。\n\n请先写出这个隐藏故事。"

    rec = {"arm": "C", "sub_arm": g["arm"], "index": g["index"], "tone": tone,
           "keywords": list(g["keywords"]), "calls": []}

    d1, m1 = _call(client, c1_sys, c1_user, _TOOL_STORY, temperature)
    rec["calls"].append(m1)
    if not d1 or not str(d1.get("hidden_story") or "").strip():
        rec["ok"] = False
        rec["error"] = "Call 1 未产出故事: " + (m1["error"] or "空 payload")
        return rec
    rec["call1"] = {
        "hidden_story": str(d1.get("hidden_story") or ""),
        "core_reveal": str(d1.get("core_reveal") or ""),
        "interesting_unknowns": [str(x) for x in
                                 (d1.get("interesting_unknowns") or [])],
    }

    # ---- Call 2: 把 Call 1 的**完整**结果喂回去 ----
    c2_user = ("【完整隐藏故事】\n" + rec["call1"]["hidden_story"]
               + "\n\n【核心揭晓点】\n" + rec["call1"]["core_reveal"]
               + "\n\n【观众第一眼不可能知道、但应该能问出来的关键事实】\n"
               + "\n".join("- " + x for x in
                           rec["call1"]["interesting_unknowns"])
               + "\n\n请从上面这个故事里截取最怪的一幕, 写成汤面 + 汤底。")
    d2, m2 = _call(client, SURFACE_CALL2_SYSTEM, c2_user, _TOOL_SURFACE,
                   temperature)
    rec["calls"].append(m2)
    if not d2 or not str(d2.get("puzzle") or "").strip():
        rec["ok"] = False
        rec["error"] = "Call 2 未产出汤面: " + (m2["error"] or "空 payload")
        return rec
    rec["ok"] = True
    rec["call2"] = {
        "puzzle": str(d2.get("puzzle") or ""),
        "answer": str(d2.get("answer") or ""),
        "askable_hidden_facts": [str(x) for x in
                                 (d2.get("askable_hidden_facts") or [])],
    }
    return rec


# ======================================================================
# 七、主流程
# ======================================================================
def _write(rec: dict) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    with io.open(RUN_JSON, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)


def cmd_draw_only() -> int:
    _setup_log()
    g = draw_groups()
    print(json.dumps(g, ensure_ascii=False, indent=2))
    print("\ngroups_md5 = %s" % groups_md5(g))
    return 0


def cmd_run() -> int:
    _setup_log()
    from story.config import Config, LLMConfig
    from story.llm import (AnthropicMessagesClient, PuzzleWriter,
                           KEYWORD_IDEA_PROMPT_VERSION,
                           KEYWORD_IDEA_SYSTEM)

    cfg = Config()
    llm_cfg = LLMConfig()
    client = AnthropicMessagesClient(llm_cfg)
    writer = PuzzleWriter(client=client, runtime_cfg=cfg)
    from story.llm import _TOOL_KEYWORD_IDEA
    temperature = float(getattr(cfg, "generate_temperature", 0.8))

    # ⚠️ 关键词必须在**任何模型调用之前**冻结并落盘。
    groups = draw_groups()
    md5 = groups_md5(groups)
    run = {
        "meta": {
            "round": "RED-BLACK-GEN",
            "not_production_change": True,
            "not_an_ab_significance_test": True,
            "no_best_of_n": True,
            "no_retry": True,
            "no_prompt_tuning_after_seeing_results": True,
            "calls_per_group": {"A": 1, "B": 1, "C": 2},
            "model": llm_cfg.model,
            "temperature": temperature,
            "current_stage_a_prompt_version": KEYWORD_IDEA_PROMPT_VERSION,
            "current_stage_a_system_md5": hashlib.md5(
                KEYWORD_IDEA_SYSTEM.encode("utf-8")).hexdigest(),
            "current_stage_a_tool_md5": hashlib.md5(
                json.dumps(_TOOL_KEYWORD_IDEA, ensure_ascii=False,
                           sort_keys=True).encode("utf-8")).hexdigest(),
            "experiment_prompt_md5": {
                "B_style_first": hashlib.md5(
                    STYLE_FIRST_SYSTEM.encode("utf-8")).hexdigest(),
                "C_call1_story": hashlib.md5(
                    STORY_CALL1_SYSTEM.encode("utf-8")).hexdigest(),
                "C_call2_surface": hashlib.md5(
                    SURFACE_CALL2_SYSTEM.encode("utf-8")).hexdigest(),
            },
            "frozen_before_first_call": True,
        },
        "groups": groups,
        "groups_md5": md5,
        "rows": [],
    }
    _write(run)
    log.info("关键词已冻结并写盘: groups_md5=%s", md5)
    log.info("model=%s temperature=%s", llm_cfg.model, temperature)

    # ---- A ----
    log.info("=== A = CURRENT (冻结生产 Stage A), %d 组 ===", N_CURRENT)
    for g in groups["current"]:
        r = _run_current(writer, g)
        run["rows"].append(r)
        _write(run)                        # 每组落盘 —— 崩了也不丢已完成的
        log.info("[A%d] %s -> ok=%s", g["index"], g["keywords"], r["ok"])

    # ---- B ----
    log.info("=== B = STYLE-FIRST 单调用, %d 组 ===", N_STYLEFIRST)
    for g in groups["stylefirst"]:
        r = _run_stylefirst(client, g, temperature)
        run["rows"].append(r)
        _write(run)
        log.info("[B%d/%s] %s -> ok=%s", g["index"], g["tone"],
                 g["keywords"], r["ok"])

    # ---- C ----
    log.info("=== C = STORY-FIRST 两调用, %d 组 ===", N_STORYFIRST)
    for g in groups["storyfirst"]:
        r = _run_storyfirst(client, g, temperature)
        run["rows"].append(r)
        _write(run)
        log.info("[C%d/%s/%s] %s -> ok=%s", g["index"], g["arm"],
                 g["tone"], g["keywords"] or "(无词)", r["ok"])

    log.info("完成。写入 %s", RUN_JSON.replace("\\", "/"))
    return 0


def cmd_report_only() -> int:
    with io.open(RUN_JSON, encoding="utf-8") as f:
        run = json.load(f)
    print(render(run))
    return 0


def render(run: dict) -> str:
    """把 run.json 渲染成人可读的原文清单。

    ⚠️ 每一个计数都**从 run.json 算**, 不手写第二份(G5-R1 的教训)。
    """
    L: list = []
    A = L.append
    rows = run["rows"]
    A("# 红汤 / 黑汤 生成实验 —— 原文")
    A("")
    A("**方向实验, 不是统计结论。** 每格恰好 1 次调用(A/B)或 2 次(C), "
      "不 retry、不 best-of-N。")
    A("")
    A("```text")
    A("A = CURRENT   冻结生产 Stage A (Case-first)")
    A("B = STYLE     一次调用, 注意力在\"红黑调性 + 完整故事 + 截取切片\"")
    A("C = STORY     两次调用: Call1 只创作隐藏故事 -> Call2 只截取汤面")
    A("```")
    A("")
    A("| 臂 | 组 | 调性 | 关键词 | 调用 | ok |")
    A("|---|---|---|---|---|---|")
    for r in rows:
        kw = "，".join(r["keywords"]) if r["keywords"] else "(无)"
        A("| %s%s | %d | %s | %s | %d | %s |" % (
            r["arm"], r.get("sub_arm", ""), r["index"],
            r.get("tone") or "—", kw, len(r.get("calls", [])),
            "ok" if r.get("ok") else "failed"))
    A("")
    for r in rows:
        A("")
        A("---")
        A("")
        hdr = "## %s%s-%d" % (r["arm"], r.get("sub_arm", ""), r["index"])
        kw = "，".join(r["keywords"]) if r["keywords"] else "(无关键词)"
        A("%s · 调性 %s · 关键词 %s" % (hdr, r.get("tone") or "—", kw))
        A("")
        if not r.get("ok"):
            A("**未成题**: %s" % r.get("error", ""))
            continue
        if r["arm"] == "A":
            sa = r["stage_a"]
            A("### core_truth")
            A("")
            A("> " + (sa["core_truth"] or "(空)"))
            A("")
            A("### observed_clues")
            A("")
            for c in sa["observed_clues"]:
                A("* " + c)
            A("")
            A("### event_chain")
            A("")
            for i, s in enumerate(sa["event_chain"], 1):
                A("%d. %s" % (i, s))
            A("")
            A("### 汤面")
            A("")
            A("> " + sa["puzzle"])
            A("")
            A("### 汤底")
            A("")
            A("> " + sa["answer"])
        else:
            if r["arm"] == "B":
                res = r["result"]
                A("### hidden_story")
                A("")
                A("> " + res["hidden_story"])
                A("")
                A("### core_reveal")
                A("")
                A("> " + res["core_reveal"])
                A("")
                A("### 汤面")
                A("")
                A("> " + res["puzzle"])
                A("")
                A("### 汤底")
                A("")
                A("> " + res["answer"])
            else:
                c1, c2 = r["call1"], r["call2"]
                A("### Call 1 · hidden_story")
                A("")
                A("> " + c1["hidden_story"])
                A("")
                A("### Call 1 · core_reveal")
                A("")
                A("> " + c1["core_reveal"])
                A("")
                A("### Call 1 · interesting_unknowns")
                A("")
                for x in c1["interesting_unknowns"]:
                    A("* " + x)
                A("")
                A("### Call 2 · 汤面")
                A("")
                A("> " + c2["puzzle"])
                A("")
                A("### Call 2 · 汤底")
                A("")
                A("> " + c2["answer"])
                A("")
                A("### Call 2 · askable_hidden_facts")
                A("")
                for x in c2["askable_hidden_facts"]:
                    A("* " + x)
    return "\n".join(L)


def main() -> int:
    if "--draw-only" in sys.argv:
        return cmd_draw_only()
    if "--run" in sys.argv:
        return cmd_run()
    if "--report-only" in sys.argv:
        return cmd_report_only()
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
