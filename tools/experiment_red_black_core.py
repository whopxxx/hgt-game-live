#!/usr/bin/env python
# coding: utf-8
"""红汤/黑汤 **汤底** 创意基线实验 (只生成隐藏故事, 不生成汤面)。

## 这个实验在问什么

一个**产品审美**问题, 不是工程问题:

> 在几乎没有约束的情况下, 模型自己会想出值得玩的红汤 / 黑汤故事吗?

之前几轮的输入堆了几十条 shape 规则(长度 / 人称 / 问句 / fair_clues /
discovery_beats / completion 合同 ...), 产出的东西越来越像"短篇悬疑案情
简介"而不是海龟汤。这一轮把那些规则**全部拿掉**, 只看**原始创意分布**。

所以本实验**故意**不做下面任何一件事:

    * 不写汤面(puzzle) —— 连碰都不碰;
    * 不做结构化(没有 facts / solve_atoms / fair_clues / discovery_beats /
      completion_fact_ids / signature);
    * 不跑 Reviewer, 不跑 truth audit, 不做任何自动评分 / 排名 / 筛选;
    * 不进 Stage B, 不入池, 不写 played / pool_used / archive / pooled 数据。

只有: 一段 system + 一句 user -> 一个**完整隐藏故事** -> 原样存盘。

## 与生产代码的接缝(单点)

只 import 两个东西:

    story.config.LLMConfig        —— 复用 base_url / api_key / model / 超时
    story.llm.AnthropicMessagesClient —— 复用**传输层**(重试 / 超时 / 解析)

**不** import `PuzzleWriter`, **不** import `story.quality`, **不**碰
`story.puzzle` 的任何 schema。理由是 `PuzzleWriter.__init__` 会把上游一堆
生成政策常量拉进来; 本实验的要点恰恰是"**没有**那些政策时模型会想什么"。
用最底层的 client 是唯一能保证"问的真的是裸问题"的接法。

`AnthropicMessagesClient` 是纯传输层(构造时只读 `LLMConfig`), 所以 import
它**不会**改变任何生产行为。这一点由 `tools/test_experiment_red_black_core.py`
静态钉住(见那里的 `test_no_production_generation_imports`)。

## Prompt 纪律(本实验最重要的一条)

任务要求 prompt **控制在几句话以内**。所以 `RED_SYSTEM` / `BLACK_SYSTEM`
短到看起来"没写够" —— 那是**刻意的**。不要在这里加规则。

历史上每次"补一条规则"都会把输出推得更像规范手册, 而这一轮要看的正是
"没有手册时它原生会想什么"。`tools/test_experiment_red_black_core.py`
会断言这两段 system 的长度上界, 防的是后来的模型顺手把它扩写成
几十条规范的 prompt(那会让本实验的结论**不再成立**)。

## 不做内容筛选(很重要)

20 个候选**全部原样保留**: 蠢的 / 普通的 / 明显 AI 味的 / 只有死亡没有
反转的 / 只有悲伤没有黑感的 —— 一个都不删。

只有**技术失败**(网络 / HTTP / schema 不合法)才重试, 且必须记进
`attempts`。**绝不**因为"这条不好看"而重抽 —— 那会把原始分布洗成
"挑出来的 20 条", 实验就白做了。

## 运行

    .venv/Scripts/python.exe tools/experiment_red_black_core.py --run
    .venv/Scripts/python.exe tools/experiment_red_black_core.py --report-only

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
import random
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

#: 抽题 seed。红/黑两条序列各自独立, 免得两类的"第 k 个"被同一串随机数
#: 绑在一起(它们之间没有任何需要配对的关系)。
SEED_RED = 20260921
SEED_BLACK = 20260922

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

#: 两类共用的 user 句。**一句话** —— 它只说明"交什么格式"。
USER_PROMPT = "写一个完整的故事。"

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
# 主题抽样 —— 只用来给两次调用**不同的起点**, 不是内容约束
# ======================================================================
#: 极简主题词池。作用仅仅是"20 个候选不要全挤在同一个母题上"。
#: ⚠️ 它们**不是**必需要素, 也不带 tone —— 只用来打散起点。
#: 用随机抽而不是精心排列, 是为了不在这个环节偷偷注入人类偏好。
SUBJECTS = [
    "一间出租屋", "一次门诊复诊", "一段长途夜车", "一位老邻居",
    "一场公司团建", "一台旧相机", "一个夏令营", "一份体检报告",
    "一次同学聚会", "一间值班室", "一部老手机", "一趟搬家",
    "一个直播间", "一次退租", "一部电梯", "一场婚礼",
    "一间画室", "一次代班", "一个快递驿站", "一场暴雨",
    "一间琴房", "一次过户", "一位护工", "一个旧保险箱",
    "一次采访", "一间地下车库", "一部公交车", "一次体检加项",
    "一间储物间", "一位新同事", "一座小岛", "一次保险理赔",
    "一间病房", "一次家访", "一个二手鱼缸", "一场考试",
    "一间洗衣房", "一次跨年", "一位网友", "一个寄存柜",
]


def _draw_subjects(seed: int, n: int) -> list:
    """按 seed 抽 n 个主题, **不放回**。可复现。"""
    rng = random.Random(seed)
    pool = list(SUBJECTS)
    rng.shuffle(pool)
    return pool[:n]


def _system_for(kind: str) -> str:
    return RED_SYSTEM if kind == "red" else BLACK_SYSTEM


def _user_for(subject: str) -> str:
    return f"{USER_PROMPT}可以围绕「{subject}」, 也可以不围绕。"


# ======================================================================
# 一次生成
# ======================================================================
def _one_candidate(client, kind: str, idx: int, subject: str) -> dict:
    """生成一个候选。**不做内容判断** —— 拿到什么存什么。

    只有技术失败(HTTP / 网络 / 没交出合法 story)才重试, 且重试次数记进
    `attempts`。技术失败**不是**内容质量信号。
    """
    system = _system_for(kind)
    user = _user_for(subject)
    rec = {
        "id": f"{kind}-{idx:02d}",
        "type": kind,
        "subject": subject,
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


# ======================================================================
# 落盘
# ======================================================================
def _write_raw(records: list) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(RAW_JSONL, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _write_manifest(cfg, records: list, base_sha: str, branch: str,
                    git_head: str) -> None:
    ok = [r for r in records if r["story"]]
    bad = [r for r in records if not r["story"]]
    manifest = {
        "experiment": "red_black_core_stories",
        "question": "无约束时模型自己会想出值得玩的红汤/黑汤故事吗",
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
            "user_template": USER_PROMPT + "可以围绕「{subject}」, 也可以不围绕。",
            "tool": _TOOL_CORE,
            "red_system_chars": len(RED_SYSTEM),
            "black_system_chars": len(BLACK_SYSTEM),
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
            "第一轮跑到 18/20: black-02 与 black-08 是纯技术超限"
            "(stop=max_tokens, JSON 未写完), 不是内容失败。因为"
            "MAX_TOKENS=1400 会稳定砍掉偏长的那一档(实测成功稿最长 1982 字, "
            "修复后 black-02 达 3228 字), 那等于给长故事加了隐形筛选。"
            "故把预算提到 2600, 用 --repair-technical **只**重跑这两个槽位, "
            "且**复用同一个 subject**(不重新抽题)。其余 18 条一字未动。"
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
        A(f"| `{r['id']}` | {r['type']} | {r['subject']} | "
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
        B(f"## {r['id']} — {r['type']} — {r['subject']}")
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
        todo = [i for i, r in enumerate(records) if not r["story"]]
        if not todo:
            log.info("没有技术失败的槽位, 无需修复")
            return 0
        log.info("技术修复: %d 个槽位 %s",
                 len(todo), [records[i]["id"] for i in todo])
        for i in todo:
            old = records[i]
            kind, idx = old["type"], int(old["id"].split("-")[1])
            # ⚠️ 复用**同一个 subject**: 只换预算, 不换题目。
            # 换 subject 就成了"重新抽一道", 那是在偷偷改实验条件。
            new = _one_candidate(client, kind, idx, old["subject"])
            if new["story"]:
                records[i] = new
                log.info("[%s] 修复成功 (%d 字, %d 次)",
                         new["id"], len(new["story"]), new["attempts"])
            else:
                log.error("[%s] 修复仍失败: %s",
                          new["id"], new["technical_error"][:160])
            _write_raw(records)
        _write_manifest(cfg, records, base_sha, branch, git_head)
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

    # ---- 主题**在第一次模型调用之前**就抽定(不许跑完再挑) ----
    plan = ([("red", i + 1, s) for i, s in
             enumerate(_draw_subjects(SEED_RED, N_PER_TYPE))] +
            [("black", i + 1, s) for i, s in
             enumerate(_draw_subjects(SEED_BLACK, N_PER_TYPE))])
    log.info("已抽定 %d 个主题(red seed=%s / black seed=%s)",
             len(plan), SEED_RED, SEED_BLACK)

    records = []
    for kind, idx, subject in plan:
        rec = _one_candidate(client, kind, idx, subject)
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
    _write_manifest(cfg, records, base_sha, branch, git_head)
    _write_reports(records, json.load(open(MANIFEST_JSON, encoding="utf-8")))

    ok = len([r for r in records if r["story"]])
    log.info("完成: %d/%d 成功。原始 -> %s", ok, len(records), RAW_JSONL)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
