#!/usr/bin/env python
# coding: utf-8
"""红汤/黑汤 **汤底** 创意基线实验 (只生成隐藏故事, 不生成汤面)。

## R1: 这一版换掉了什么(以及为什么)

v1 自己造了一个单主题池 `SUBJECTS`, 每道只抽一个 subject, 而 user prompt
写的是"可以围绕「{subject}」, 也可以不围绕"。

**那等于把约束取消了**, 也换掉了产品真正的机制。20 条原文直接证实了后果:
模型迅速掉回它最熟悉的高概率母题 —— 旧恋人 / 病重父亲 / 等亲人回家 /
纪念亡者 / 温情告别。红汤不像红汤, 黑汤大面积塌成伤感文学。

**两随机关键词不是装饰, 也不是为了检查字面命中。** 它的作用是给每次创作
一个**外部随机碰撞**, 压住 AI 的高概率惯性、防止故事越生成越趋同。所以:

    * 删除自造的 `SUBJECTS`;
    * 直接复用**生产同款**的 `KeywordBag`
      (`load_bag(DEFAULT_CORPUS_PATH, seed)` -> `bag.draw()`);
    * 每道抽 **2 个随机关键词**(独立词库随机重组, 不保留原始搭配);
    * 原样记录 `keywords` / corpus version / session seed / draw index;
    * **不**检查两个词有没有字面出现, **不**强迫它们成为机关;
    * **也**不再说"可以不围绕" —— 那会把约束本身取消。

由此, 本实验问的问题也随之改变了。它**不再**是:

    ✗ 无约束时模型会想什么。

而是:

    ✓ 在极短红/黑方向提示 + 生产同款 2-key 随机扰动下,
      模型能不能产生我们认可的汤底。

## 这个实验在问什么

一个**产品审美**问题, 不是工程问题。之前几轮的输入堆了几十条 shape 规则
(长度 / 人称 / 问句 / fair_clues / discovery_beats / completion 合同 ...),
产出的东西越来越像"短篇悬疑案情简介"而不是海龟汤。这一轮把那些规则**全部
拿掉**, 只留"方向提示 + 两随机关键词", 看**原始创意分布**。

所以本实验**故意**不做下面任何一件事:

    * 不写汤面(puzzle) —— 连碰都不碰;
    * 不做结构化(没有 facts / solve_atoms / fair_clues / discovery_beats /
      completion_fact_ids / signature);
    * 不跑 Reviewer, 不跑 truth audit, 不做任何自动评分 / 排名 / 筛选;
    * 不进 Stage B, 不入池, 不写 played / pool_used / archive / pooled 数据。

只有: 一段 system + 一句 user(带两随机关键词) -> 一个**完整隐藏故事** ->
原样存盘。

## 与生产代码的接缝

import 三样, 都只是**取材**与**传输**, 不含任何生成政策:

    story.config.LLMConfig                —— base_url / api_key / model / 超时
    story.llm.AnthropicMessagesClient     —— 传输层(重试 / 超时 / 解析)
    story.keyword_seed.load_bag           —— **生产同款**抽词(2-key 随机碰撞)
    story.keyword_corpus.DEFAULT_CORPUS_PATH
    story.keyword_seed.derive_session_seed / KEYWORD_SEED_VERSION

**不** import `PuzzleWriter`, **不** import `story.quality`, **不**碰
`story.puzzle` 的任何 schema。理由是 `PuzzleWriter.__init__` 会把上游一堆
生成政策常量拉进来; 本实验的要点恰恰是"**没有**那些政策时模型会想什么"。

⚠️ `KeywordBag` 是**刻意**从生产借来的, 与上面那条禁令不冲突:
本实验要测的正是"生产同款随机扰动"能不能把模型从惯性里拉出来。自己再写
一个抽词器就等于把自变量换掉了 —— v1 就是这么跑偏的。

`AnthropicMessagesClient` / `KeywordBag` 都是纯函数式或只读各自配置, 所以
import 它们**不会**改变任何生产行为。这一点由
`tests/test_experiment_red_black_core.py` 静态钉住。

## Prompt 纪律(本实验最重要的一条)

任务要求 prompt **控制在几句话以内**。所以 `RED_SYSTEM` / `BLACK_SYSTEM`
短到看起来"没写够" —— 那是**刻意的**。不要在这里加规则。

历史上每次"补一条规则"都会把输出推得更像规范手册, 而这一轮要看的正是
"方向提示 + 随机词碰撞"下它原生会想什么。
`tests/test_experiment_red_black_core.py` 会断言这两段 system 的长度上界,
防的是后来的模型顺手把它扩写成几十条规范的 prompt(那会让本实验的结论
**不再成立**)。

## 不做内容筛选(很重要)

20 个候选**全部原样保留**: 蠢的 / 普通的 / 明显 AI 味的 / 只有死亡没有
反转的 / 只有悲伤没有黑感的 —— 一个都不删。

只有**技术失败**(网络 / HTTP / schema 不合法)才重试, 且必须记进
`attempts`。**绝不**因为"这条不好看"而重抽 —— 那会把原始分布洗成
"挑出来的 20 条", 实验就白做了。

## 运行

    .venv/Scripts/python.exe tools/experiment_red_black_core.py --run
    .venv/Scripts/python.exe tools/experiment_red_black_core.py --report-only
    .venv/Scripts/python.exe tools/experiment_red_black_core.py --repair-technical

产物(⚠️ `data/**/*.jsonl` 被 .gitignore 挡着, 归档要 `git add -f`):

    data/red_black_core_experiment/raw.jsonl          20 条原始候选
    data/red_black_core_experiment/run_manifest.json  可复现所需的全部元信息
    data/red_black_core_experiment/report.md          客观陈述(不下"哪个好"的结论)
    data/red_black_core_experiment/raw_report.md      上面那份的逐条全文展开
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import os
import sys
import time
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

OUT_DIR = os.path.join(ROOT, "data", "red_black_core_experiment")
RAW_JSONL = os.path.join(OUT_DIR, "raw.jsonl")
MANIFEST_JSON = os.path.join(OUT_DIR, "run_manifest.json")
REPORT_MD = os.path.join(OUT_DIR, "report.md")
RAW_REPORT_MD = os.path.join(OUT_DIR, "raw_report.md")

TAG = "RB-CORE"
log = logging.getLogger(TAG)

#: 每类固定的候选数。**跑之前写死**, 不许跑完再挑。
N_PER_TYPE = 10

#: session seed 的**基** seed。红/黑两条序列各自派生一次, 免得两类的"第 k 个"
#: 被同一串随机数绑在一起(它们之间没有任何需要配对的关系)。
#:
#: ⚠️ 这里用的是**生产同款**的 `derive_session_seed(base)` 派生, 而不是
#: 自己 `random.Random(seed)`: 生产的 session seed 是 64 位混合出来的,
#: 我们照抄那条路径, 才能说"和生产是同一套抽词"。
SEED_RED = 20260921
SEED_BLACK = 20260922

#: 词库路径。走生产默认值(`story.keyword_corpus.DEFAULT_CORPUS_PATH`),
#: 不自己拼路径 —— 否则生产换了词库、实验还指着旧文件。
#: 由 `_corpus_path()` 在运行时取。

#: 温度沿用生产的出题档(`Config.generate_temperature = 0.8`) —— 换了温度
#: 就是在换一个"模型会想什么"的问题, 那会让本实验不再描述生产形态。
TEMPERATURE = 0.8

#: 每个候选的最大生成预算。汤底比汤面长, 所以比 Stage A 的 1500 宽一点,
#: 但仍然**不设**下限 —— 模型想写 80 个字就存 80 个字。
#:
#: ⚠️ 为什么从 1400 提到 2600: 第一次实跑 20 个里有 2 个是**纯技术超限**
#: (`stop=max_tokens`, JSON 还没写完就断了), 而不是内容失败。实测成功的
#: 汤底最长 1982 字, 所以 1400 会稳定砍掉偏长的那一档 —— 那等于给"长故事"
#: 加了一道**隐形的长度筛选**, 而本轮明确不要任何筛选。2600 留出余量。
#: (任务允许技术失败重试, 所以这不算"重抽到满意"。)
MAX_TOKENS = 2600

#: 技术失败最多重试几次(不含首次)。语义不动 —— 只是网络/HTTP。
MAX_TECHNICAL_RETRIES = 2


# ======================================================================
# Prompt —— 刻意极短。**不要加规则。**
# ======================================================================
#: 红汤: 关键在"真相本身值得揭晓", 而不是"有没有血腥"。
RED_SYSTEM = (
    "你在为海龟汤设计隐藏故事。\n"
    "只写事情真正发生了什么, 不要写谜面(writer 之后会从故事里截取)。\n"
    "偏红汤: 真相本身要有分量, 值得最后揭晓 —— 不是普通事故, 也不是单纯的悲剧。"
)

#: 黑汤: 关键在"揭晓后的不适/错位感", 且**不靠血腥**。
#: (任务原话: 黑暗感靠真相、关系、意图、世界规则或认知错位。)
BLACK_SYSTEM = (
    "你在为海龟汤设计隐藏故事。\n"
    "只写事情真正发生了什么, 不要写谜面(writer 之后会从故事里截取)。\n"
    "偏黑汤: 真相揭晓后应让人明显不安或后背发凉 —— 靠关系、意图或世界规则, "
    "不靠血腥描写。"
)

#: user 句 —— 一句话, 带上两个**随机关键词**。
#:
#: ⚠️ 三个"不要", 都是 R1 明确要求的:
#:   1. 不要写"可以围绕 ... 也可以不围绕" —— 那会把约束本身取消(v1 的错);
#:   2. 不要要求两个词字面出现在故事里;
#:   3. 不要说"必须把关键词做成机关" —— 它们是**创意起点**, 不是命题作文。
#: 所以这里只有"用它们作为创意起点"这一句, 不再解释。
USER_PROMPT = "随机关键词：{a}、{b}。用它们作为创意起点，写一个完整的隐藏故事。"

#: 强制工具调用: 只要一个字段。不要 title / puzzle / clues / facts。
_TOOL_CORE = {
    "name": "emit_core_story",
    "description": "交出一个完整的隐藏故事。",
    "input_schema": {
        "type": "object",
        "properties": {
            "story": {
                "type": "string",
                "description": "完整故事全文。",
                "minLength": 1,
            },
        },
        "required": ["story"],
    },
}


def _sha8(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:8]


# ======================================================================
# 抽词 —— **生产同款** KeywordBag(2-key 随机碰撞)
# ======================================================================
def _corpus_path() -> str:
    """生产默认词库路径。不自己拼 —— 生产换库时实验跟着换。"""
    from story.keyword_corpus import DEFAULT_CORPUS_PATH
    return str(DEFAULT_CORPUS_PATH)


def _make_bag(base_seed: int):
    """建一个**生产同款**的词袋。返回 `(bag, meta)`。

    用的是 `load_bag(path, derive_session_seed(base_seed))` —— 与
    `director.py` / `story/prefetch.py` 里那条完全一样的路径。
    """
    from story.keyword_seed import derive_session_seed, load_bag
    ss = derive_session_seed(base_seed)
    bag, meta = load_bag(_corpus_path(), ss)
    return bag, meta, int(ss)


def _draw_two(bag) -> dict:
    """从 bag 抽 2 个随机关键词。**原样返回** bag 的结果。

    ⚠️ 刻意**不**在这里加任何"词好不好"的判断, 也不检查语义相关性 ——
    随机碰撞本身就是创意扰动的来源(生产 §四 的原话)。
    """
    return bag.draw()


def _system_for(kind: str) -> str:
    return RED_SYSTEM if kind == "red" else BLACK_SYSTEM


def _user_for(a: str, b: str) -> str:
    return USER_PROMPT.format(a=a, b=b)


# ======================================================================
# 一次生成
# ======================================================================
def _one_candidate(client, kind: str, idx: int, draw: dict,
                   corpus_meta: dict, session_seed: int) -> dict:
    """生成一个候选。**不做内容判断** —— 拿到什么存什么。

    只有技术失败(HTTP / 网络 / 没交出合法 story)才重试, 且重试次数记进
    `attempts`。技术失败**不是**内容质量信号。

    `draw` 是 `KeywordBag.draw()` 的原始返回, 原样落盘以便复现:
    同一词库 + 同一 session seed + 同一 draw index => 同一对关键词。
    """
    system = _system_for(kind)
    kws = list(draw.get("keywords") or [])
    user = _user_for(kws[0], kws[1])
    rec = {
        "id": f"{kind}-{idx:02d}",
        "type": kind,
        # ---- 抽词溯源(原样记录, 不复述/不改写) ----
        "keywords": kws,
        "keyword_corpus_version": str(corpus_meta.get("corpus_version") or ""),
        "keyword_seed_version": _keyword_seed_version(),
        "keyword_session_seed": session_seed,
        "keyword_draw_index": int(draw.get("index") or 0),
        "keyword_draw_relaxed": int(draw.get("relaxed") or 0),
        "system": system,
        "user": user,
        "attempts": 0,
        "technical_error": "",
        "story": "",
        "model": "",
        "usage": {},
        "latency_ms": 0,
    }
    t0 = time.monotonic()
    last_err = ""
    for attempt in range(MAX_TECHNICAL_RETRIES + 1):
        rec["attempts"] = attempt + 1
        res = client.messages(system, user, max_tokens=MAX_TOKENS,
                              tool=_TOOL_CORE, temperature=TEMPERATURE)
        if res.error:
            last_err = res.error
            log.warning("[%s] 技术失败(第 %d 次): %s",
                        rec["id"], attempt + 1, last_err[:120])
            continue
        d = res.tool_input or {}
        story = str(d.get("story") or "").strip()
        if not story:
            last_err = f"没交出 story (tool_input={str(res.tool_input)[:120]})"
            log.warning("[%s] %s", rec["id"], last_err)
            continue
        rec["story"] = story
        rec["model"] = res.model or ""
        rec["usage"] = res.usage or {}
        last_err = ""
        break
    rec["latency_ms"] = int((time.monotonic() - t0) * 1000)
    rec["technical_error"] = last_err
    return rec


def _keyword_seed_version() -> str:
    from story.keyword_seed import KEYWORD_SEED_VERSION
    return str(KEYWORD_SEED_VERSION)


def _session_seed(base_seed: int) -> int:
    """把 base seed 派生成生产的 session seed(与 director 同一条路径)。"""
    from story.keyword_seed import derive_session_seed
    return int(derive_session_seed(base_seed))


# ======================================================================
# 落盘
# ======================================================================
def _write_raw(records: list) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(RAW_JSONL, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _write_manifest(cfg, records: list, base_sha: str, branch: str,
                    git_head: str, corpus_meta: dict) -> None:
    ok = [r for r in records if r["story"]]
    bad = [r for r in records if not r["story"]]
    manifest = {
        "experiment": "red_black_core_stories",
        "question": (
            "在极短红/黑方向提示 + 生产同款 2-key 随机扰动下, "
            "模型能不能产生我们认可的汤底。"
        ),
        "question_v1_retired": (
            "v1 问的是'无约束时模型会想什么', 那版自己造单主题池且允许"
            "'可以不围绕', 等于取消了约束 —— 20 条迅速塌回温情/怀旧母题。"
            "R1 换回生产 KeywordBag 两随机关键词, 问题也随之改写。"
        ),
        "base_sha": base_sha,
        "branch": branch,
        "git_head_at_run": git_head,
        "model_requested": cfg.model,
        "model_returned": sorted({r["model"] for r in ok if r["model"]}),
        "base_url": cfg.base_url,
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
        "n_per_type": N_PER_TYPE,
        "seed_red": SEED_RED,
        "seed_black": SEED_BLACK,
        # ---- 抽词: 生产同款(2-key 随机碰撞) ----
        "keyword_mechanism": {
            "source": "story.keyword_seed.KeywordBag (生产同款, 直接复用)",
            "corpus_path": "story.keyword_corpus.DEFAULT_CORPUS_PATH",
            "corpus_version": str(corpus_meta.get("corpus_version") or ""),
            "keyword_count": corpus_meta.get("keyword_count"),
            "keyword_seed_version": _keyword_seed_version(),
            "session_seed_red": _session_seed(SEED_RED),
            "session_seed_black": _session_seed(SEED_BLACK),
            "per_candidate": [
                {
                    "id": r["id"],
                    "keywords": r.get("keywords"),
                    "draw_index": r.get("keyword_draw_index"),
                    "relaxed": r.get("keyword_draw_relaxed"),
                }
                for r in records
            ],
            "no_literal_check": (
                "不检查两个关键词有没有字面出现在故事里; 也不要求它们成为"
                "机关。随机碰撞本身就是创意扰动 —— 见生产 §四。"
            ),
        },
        "counts": {
            "total": len(records),
            "ok": len(ok),
            "technical_failed": len(bad),
            "by_type": {
                k: {
                    "ok": len([r for r in ok if r["type"] == k]),
                    "technical_failed": len(
                        [r for r in bad if r["type"] == k]),
                }
                for k in ("red", "black")
            },
        },
        "prompts": {
            "red_system": RED_SYSTEM,
            "black_system": BLACK_SYSTEM,
            "user_template": USER_PROMPT,
            "tool": _TOOL_CORE,
            "red_system_chars": len(RED_SYSTEM),
            "black_system_chars": len(BLACK_SYSTEM),
            "user_template_chars": len(USER_PROMPT),
            "red_system_sha8": _sha8(RED_SYSTEM),
            "black_system_sha8": _sha8(BLACK_SYSTEM),
        },
        "not_generated": [
            "puzzle(汤面)", "title", "fair_clues", "facts", "solve_atoms",
            "discovery_beats", "completion_fact_ids", "hints", "signature",
            "reviewer 分数", "自动分类器结果",
        ],
        "no_filtering": (
            "20 个候选全部原样保留 —— 没有删除 / 没有重抽到满意 / "
            "没有排名。只有技术失败(HTTP/网络/输出超限)会重试, 记在 attempts。"
        ),
        "technical_repair_note": (
            "MAX_TOKENS 从 1400 提到 2600: 1400 会稳定砍掉偏长的那一档"
            "(成功稿最长可达 1982 字), 等于给长故事加了隐形筛选。"
            "若仍有纯技术超限, 用 --repair-technical **只**重跑那个槽位, "
            "且**复用同一个 draw**(不重新抽关键词)。"
        ),
        "outputs": {
            "raw": "data/red_black_core_experiment/raw.jsonl",
            "report": "data/red_black_core_experiment/report.md",
            "raw_report": "data/red_black_core_experiment/raw_report.md",
        },
    }
    with open(MANIFEST_JSON, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _write_reports(records: list, manifest: dict) -> None:
    ok = [r for r in records if r["story"]]
    bad = [r for r in records if not r["story"]]
    lines = []
    A = lines.append

    A("# 红汤 / 黑汤 **汤底** 创意基线 —— 原始结果")
    A("")
    A("## 这个实验**没有**做什么")
    A("")
    A("| 没有做 | 说明 |")
    A("|---|---|")
    A("| 不写汤面 | 本轮完全不生成 puzzle |")
    A("| 不做结构化 | 没有 facts / solve_atoms / fair_clues / "
      "discovery_beats / completion / signature |")
    A("| 不跑 Reviewer | 没有 pass/fix/rewrite, 没有 quality_checks, "
      "没有 truth audit |")
    A("| 不评分不排名 | 没有任何自动分类器给这 20 条打分或排序 |")
    A("| 不筛选 | 20 条全部原样保留, 不删除 / 不重抽 |")
    A("| 不接生产 | 不进 Stage B, 不入池, 不写 played / pool_used / archive |")
    A("")
    A("本文件只陈述**客观事实**。哪几条好、红黑两种哪边更强 —— ")
    A("**由人直接读下面 20 条原始故事来判断**, 报告不替读者下结论。")
    A("")
    A("## 事实")
    A("")
    A(f"- base SHA: `{manifest['base_sha']}`")
    A(f"- branch: `{manifest['branch']}`")
    A(f"- 生成时 HEAD: `{manifest['git_head_at_run']}`")
    A(f"- model(请求): `{manifest['model_requested']}`")
    A(f"- model(返回体): `{manifest['model_returned']}`")
    A(f"- temperature: `{manifest['temperature']}` / "
      f"max_tokens: `{manifest['max_tokens']}`")
    A(f"- seed: red=`{manifest['seed_red']}` black=`{manifest['seed_black']}`")
    A(f"- 红 10 / 黑 10, 共 `{manifest['counts']['total']}` 条")
    A(f"- 成功 `{manifest['counts']['ok']}` / "
      f"技术失败 `{manifest['counts']['technical_failed']}`")
    A(f"- 原始结果: `{manifest['outputs']['raw']}`")
    A(f"- 逐条全文: `{manifest['outputs']['raw_report']}`")
    A("")
    A("## 抽词机制 —— **生产同款 2-key 随机碰撞**")
    A("")
    km = manifest["keyword_mechanism"]
    A(f"- 来源: `{km['source']}`")
    A(f"- 词库: `{km['corpus_path']}` "
      f"(corpus_version=`{km['corpus_version']}`, "
      f"keyword_count=`{km['keyword_count']}`)")
    A(f"- keyword seed version: `{km['keyword_seed_version']}`")
    A(f"- session seed: red=`{km['session_seed_red']}` "
      f"black=`{km['session_seed_black']}`")
    A("- 每个候选: **独立抽 2 个随机关键词**, 不保留原始搭配, 不要求语义相关")
    A(f"- {km['no_literal_check']}")
    A("")
    A("| id | 关键词 | draw index | relaxed |")
    A("|---|---|---|---|")
    for c in km["per_candidate"]:
        A(f"| `{c['id']}` | {'/'.join(c['keywords'] or [])} | "
          f"{c['draw_index']} | {c['relaxed']} |")
    A("")
    A("## 实际使用的 Prompt(原文)")
    A("")
    A(f"### 红汤 system —— {manifest['prompts']['red_system_chars']} 字 "
      f"(`{manifest['prompts']['red_system_sha8']}`)")
    A("")
    A("```text")
    A(manifest["prompts"]["red_system"])
    A("```")
    A("")
    A(f"### 黑汤 system —— {manifest['prompts']['black_system_chars']} 字 "
      f"(`{manifest['prompts']['black_system_sha8']}`)")
    A("")
    A("```text")
    A(manifest["prompts"]["black_system"])
    A("```")
    A("")
    A("### user(两类共用)")
    A("")
    A("```text")
    A(manifest["prompts"]["user_template"])
    A("```")
    A("")
    A("### 输出 schema")
    A("")
    A("```json")
    A(json.dumps(_TOOL_CORE["input_schema"], ensure_ascii=False, indent=2))
    A("```")
    A("")
    A("## 逐条索引")
    A("")
    A("| id | type | 主题 | 技术失败 | 字数 | 试次 |")
    A("|---|---|---|---|---|---|")
    for r in records:
        A(f"| `{r['id']}` | {r['type']} | "
          f"{'/'.join(r.get('keywords') or [])} | "
          f"{'是' if r['technical_error'] else ''} | "
          f"{len(r['story']) if r['story'] else 0} | {r['attempts']} |")
    A("")
    if bad:
        A("### 技术失败明细")
        A("")
        for r in bad:
            A(f"- `{r['id']}` ({r['type']}): {r['technical_error'][:200]}")
        A("")
    # ---- 客观观察(不是评价, 只是"模型自己做了什么") ----
    A("## 模型自发的行为(客观计数, 不含褒贬)")
    A("")
    A("这些是**模型在没有要求的情况下自己做的**, 记下来供人读原文时参照。")
    A("本轮**没有**因此删改任何一条。")
    A("")
    titled = [r["id"] for r in ok
              if r["story"].splitlines()
              and len(r["story"].splitlines()[0].strip()) < 20
              and r["story"].splitlines()[0].strip().startswith("《")]
    A(f"- **自加标题**(首行 `《…》`): {len(titled)}/{len(ok)} 条 —— "
      + (", ".join(f"`{i}`" for i in titled) if titled else "无"))
    meta = [r["id"] for r in ok
            if any(w in r["story"] for w in ("汤面", "汤底", "谜面", "谜底"))]
    A(f"- **提到「谜面/汤底」等元文本**: {len(meta)}/{len(ok)} 条"
      + (f" —— {', '.join('`%s`' % i for i in meta)}" if meta else " —— 无"))
    q = [r["id"] for r in ok if r["story"].strip().endswith(("？", "?"))]
    A(f"- **以问句结尾**: {len(q)}/{len(ok)} 条"
      + (f" —— {', '.join('`%s`' % i for i in q)}" if q else " —— 无"))
    para = [len([p for p in r["story"].split("\n") if p.strip()]) for r in ok]
    if para:
        A(f"- **自然段数**: 最少 {min(para)}, 最多 {max(para)}")
    # ---- 关键词实际落点(只统计, 不作判定) ----
    A("")
    A("### 关键词实际怎么用的(**只统计, 不作判定**)")
    A("")
    A("本轮**不检查**关键词有没有字面出现, 也不要求它们成为机关。")
    A("下面只是把「前 400 字里出现了几个关键词」数出来, 供人参照。")
    A("**命中少不等于坏** —— 词的作用是给创意一个外部起点, 不是命题作文。")
    A("")
    hit_dist = {}
    for r in ok:
        head = r["story"][:400]
        n = len([k for k in (r.get("keywords") or []) if k in head])
        hit_dist[r["id"]] = n
    A(f"- 前 400 字命中 2 个: "
      f"{len([i for i, n in hit_dist.items() if n == 2])}/{len(ok)} 条")
    A(f"- 前 400 字命中 1 个: "
      f"{len([i for i, n in hit_dist.items() if n == 1])}/{len(ok)} 条")
    A(f"- 前 400 字命中 0 个: "
      f"{len([i for i, n in hit_dist.items() if n == 0])}/{len(ok)} 条")
    A("")
    A("| id | 关键词 | 前 400 字命中 |")
    A("|---|---|---|")
    for r in ok:
        A(f"| `{r['id']}` | {'/'.join(r.get('keywords') or [])} | "
          f"{hit_dist[r['id']]}/2 |")
    A("")
    A("## 下一步(本轮**不做**)")
    A("")
    A("只有在人确认这批汤底方向对了之后, 下一轮才做:")
    A("")
    A("> 完整汤底 -> 截取 1~3 句极短汤面")
    A("")
    with open(REPORT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    # ---- 全文展开, 供逐条人工阅读 ----
    raw = []
    B = raw.append
    B("# 红黑汤底 20 条 —— 原始全文")
    B("")
    B("不做筛选、不做排名。这就是模型交回来的原文。")
    B("")
    for r in records:
        B(f"## {r['id']} — {r['type']} — 关键词: "
          f"{'/'.join(r.get('keywords') or [])}")
        B("")
        if r["story"]:
            B(r["story"])
        else:
            B(f"_(技术失败, {r['attempts']} 次尝试)_ {r['technical_error'][:300]}")
        B("")
        B("---")
        B("")
    with open(RAW_REPORT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(raw))


# ======================================================================
# 入口
# ======================================================================
def _setup_log() -> None:
    """显式 utf-8 的 FileHandler + 控制台 —— 见 g4cf_smoke 的 GBK 教训。"""
    os.makedirs(OUT_DIR, exist_ok=True)
    log.setLevel(logging.INFO)
    log.propagate = False
    h = logging.FileHandler(os.path.join(OUT_DIR, "run.log"),
                            encoding="utf-8", errors="replace")
    h.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"))
    log.addHandler(h)
    ch = logging.StreamHandler(
        io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                         errors="replace", line_buffering=True))
    ch.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(ch)


def _git(*args: str) -> str:
    import subprocess
    try:
        r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                           text=True, timeout=10)
        return r.stdout.strip()
    except Exception:                        # noqa: BLE001
        return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true",
                    help="真的打模型(generate 20 个候选)")
    ap.add_argument("--report-only", action="store_true",
                    help="只从已有 raw.jsonl 重出报告, 不打模型")
    ap.add_argument("--repair-technical", action="store_true",
                    help="**只**重跑技术失败的槽位, 已成功的原样保留。"
                         "绝不用来因为'不好看'而重抽 —— 见模块 docstring。")
    args = ap.parse_args()
    _setup_log()

    base_sha = _git("rev-parse", "origin/main") or _git("rev-parse", "HEAD")
    branch = _git("branch", "--show-current")
    git_head = _git("rev-parse", "HEAD")

    if args.report_only:
        if not os.path.exists(RAW_JSONL):
            log.error("没有 %s —— 先跑 --run", RAW_JSONL)
            return 2
        with open(RAW_JSONL, encoding="utf-8") as f:
            records = [json.loads(x) for x in f if x.strip()]
        with open(MANIFEST_JSON, encoding="utf-8") as f:
            manifest = json.load(f)
        _write_reports(records, manifest)
        log.info("报告已重出: %s", REPORT_MD)
        return 0

    if args.repair_technical:
        from story.config import LLMConfig
        from story.llm import AnthropicMessagesClient
        if not os.path.exists(RAW_JSONL):
            log.error("没有 %s —— 先跑 --run", RAW_JSONL)
            return 2
        with open(RAW_JSONL, encoding="utf-8") as f:
            records = [json.loads(x) for x in f if x.strip()]
        cfg = LLMConfig()
        client = AnthropicMessagesClient(cfg)
        # 词库元信息 —— 修复路径也要能写进 manifest, 所以在这里同样建一次。
        _bag_probe, corpus_meta, _ss = _make_bag(SEED_RED)
        todo = [i for i, r in enumerate(records) if not r["story"]]
        if not todo:
            log.info("没有技术失败的槽位, 无需修复")
            return 0
        log.info("技术修复: %d 个槽位 %s",
                 len(todo), [records[i]["id"] for i in todo])
        for i in todo:
            old = records[i]
            kind, idx = old["type"], int(old["id"].split("-")[1])
            # ⚠️ 复用**同一个 draw**: 只换预算, 不重抽关键词。
            # 重抽就成了"重新抽一道题", 那是在偷偷改实验条件。
            draw = {
                "keywords": list(old.get("keywords") or []),
                "index": old.get("keyword_draw_index"),
                "relaxed": old.get("keyword_draw_relaxed"),
            }
            new = _one_candidate(client, kind, idx, draw, corpus_meta,
                                 old.get("keyword_session_seed") or 0)
            if new["story"]:
                records[i] = new
                log.info("[%s] 修复成功 (%d 字, %d 次)",
                         new["id"], len(new["story"]), new["attempts"])
            else:
                log.error("[%s] 修复仍失败: %s",
                          new["id"], new["technical_error"][:160])
            _write_raw(records)
        _write_manifest(cfg, records, base_sha, branch, git_head, corpus_meta)
        _write_reports(records, json.load(open(MANIFEST_JSON,
                                               encoding="utf-8")))
        ok = len([r for r in records if r["story"]])
        log.info("修复后: %d/%d 成功", ok, len(records))
        return 0

    if not args.run:
        log.error("要么 --run, 要么 --report-only, 要么 --repair-technical")
        return 2

    from story.config import LLMConfig
    from story.llm import AnthropicMessagesClient

    cfg = LLMConfig()
    log.info("model=%s temperature=%s max_tokens=%s",
             cfg.model, TEMPERATURE, MAX_TOKENS)
    client = AnthropicMessagesClient(cfg)

    # ---- 两个**生产同款**词袋: 红/黑各一条独立序列 ----
    bag_red, corpus_meta, ss_red = _make_bag(SEED_RED)
    bag_black, meta_black, ss_black = _make_bag(SEED_BLACK)
    log.info("词袋就绪: corpus=%s keyword_count=%s seed_version=%s",
             corpus_meta.get("corpus_version"), corpus_meta.get("keyword_count"),
             _keyword_seed_version())
    log.info("session_seed red=%s black=%s", ss_red, ss_black)

    # ---- 关键词**在第一次模型调用之前**就抽定(不许跑完再挑) ----
    plan = ([("red", i + 1, bag_red.draw(), ss_red) for i in range(N_PER_TYPE)]
            + [("black", i + 1, bag_black.draw(), ss_black)
               for i in range(N_PER_TYPE)])
    log.info("已抽定 %d 组关键词", len(plan))
    for kind, idx, d, _ss in plan:
        log.info("  [%s-%02d] %s", kind, idx, "/".join(d["keywords"]))

    records = []
    for kind, idx, draw, sess in plan:
        # 红/黑各自的 corpus_meta 相同(同一个词库), 传红的那份即可。
        rec = _one_candidate(client, kind, idx, draw, corpus_meta, sess)
        records.append(rec)
        if rec["story"]:
            log.info("[%s/%s] ok (%d 字, %d 次, %dms)",
                     kind, f"{idx:02d}", len(rec["story"]),
                     rec["attempts"], rec["latency_ms"])
        else:
            log.error("[%s/%s] 技术失败: %s",
                      kind, f"{idx:02d}", rec["technical_error"][:160])
        # 边跑边落盘 —— 中途崩了也不会丢掉已完成的候选。
        _write_raw(records)

    _write_raw(records)
    _write_manifest(cfg, records, base_sha, branch, git_head, corpus_meta)
    _write_reports(records, json.load(open(MANIFEST_JSON, encoding="utf-8")))

    ok = len([r for r in records if r["story"]])
    log.info("完成: %d/%d 成功。原始 -> %s", ok, len(records), RAW_JSONL)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
