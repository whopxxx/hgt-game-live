#!/usr/bin/env python
# coding: utf-8
"""R2: **当前生产链**全链路观测 —— 关键词 -> Stage A -> Stage B -> PuzzleSpec。

## 这个实验要回答什么

R1 只看了汤底(裸故事)。这一轮要看的是**当前代码真实链**在加了 red/black
方向之后, 最终展示出来的题**会不会更像真正的海龟汤**, 还是会被
`observed_clues` / `fair_clues` 这些结构重新写成长案情简介。

它**不是**在优化汤底, 也**不是**再写一套 prompt。

## 铁律: 不改 production prompt

Stage A / Stage B **原样调用**当前生产的:

    PuzzleWriter.gen_keyword_idea(keywords)          <- KEYWORD_IDEA_SYSTEM
                                                        _TOOL_KEYWORD_IDEA
                                                        _keywords_prompt()
    PuzzleWriter.structure_original_idea(...)        <- STRUCTURE_SYSTEM
                                                        _TOOL_STRUCTURE
                                                        Reviewer / truth audit
                                                        validation

**一行 production 代码都不改**, `story/llm.py` 的常量一个都不动。

### 那"一行 lane 信息"怎么加

生产 Stage A 的 user message 由 `_keywords_prompt()` 在 `gen_keyword_idea`
**内部**拼好, 没有注入点。要在**不改生产**的前提下只给 Stage A 加一行
类型方向, 本工具用一个 `_LaneClient` 代理:

    生产文本:  关键词：A，B\n\n请围绕这几个关键词写一道中文海龟汤。...
    本工具:    类型：红汤。\n关键词：A，B\n\n请围绕这几个关键词...

即**只在 Stage A 那一条** system==KEYWORD_IDEA_SYSTEM 的调用上, 给 user
前面贴一行; 其余所有调用(Stage B / Reviewer / truth audit)一律**原样透传**。

为什么用代理而不是 monkeypatch `_keywords_prompt`: 代理是**逐次调用**的,
它不可能意外改到别的调用点; monkeypatch 一个模块级函数会影响进程内所有
使用者, 那就不再是"只给这一条加一行"了。代理还让"到底改了什么"变成一段
可读、可测的代码(见 `tests/test_experiment_full_chain.py`)。

⚠️ 这**不是**改 production —— production 自己不经过这个代理, 只有本工具
构造的 writer 用。

## 样本: 5 红 + 5 黑 = 10 道

按用户口径, 本轮不再跑 20 条。

## 拒绝也是数据

Stage B 里 Reviewer / truth audit / validation 若拒了某条:

    * **不重抽到通过**;
    * 保留 Stage A 原始结果(core_truth / observed_clues / event_chain /
      title / puzzle / answer 全留着);
    * 记录 Stage B 的 reject / error 标签;
    * 下一槽继续。

要看的正是**当前真实链会怎么处理它**, 不是凑 10 个"最终都通过"的漂亮样本。

## 不做的事

    * 不自动评分 / 不"想玩不想玩" / 不红黑判定器;
    * 不加任何新 reviewer / gate;
    * 不另写结构器;
    * 不入池、不写 played / pool_used / archive。

## 运行

    .venv/Scripts/python.exe tools/experiment_full_chain.py --run
    .venv/Scripts/python.exe tools/experiment_full_chain.py --report-only

产物(⚠️ `data/**/*.jsonl` 被 .gitignore 挡着, 归档要 `git add -f`):

    data/full_chain_experiment/raw.jsonl        10 条全链路原始记录
    data/full_chain_experiment/run_manifest.json
    data/full_chain_experiment/report.md        人读报告(逐道展开)
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

OUT_DIR = os.path.join(ROOT, "data", "full_chain_experiment")
RAW_JSONL = os.path.join(OUT_DIR, "raw.jsonl")
MANIFEST_JSON = os.path.join(OUT_DIR, "run_manifest.json")
REPORT_MD = os.path.join(OUT_DIR, "report.md")

TAG = "FULL-CHAIN"
log = logging.getLogger(TAG)

#: 每类固定条数。**跑之前写死**(用户口径: 5 红 + 5 黑)。
N_PER_TYPE = 5

#: 红/黑各自的 base seed。用**生产同款** `derive_session_seed` 派生。
SEED_RED = 20260923
SEED_BLACK = 20260924

#: 任务要求: lane 行必须**很短**, 不给红黑写几十条规则。
#: 这是本实验唯一新增的 prompt 文本。
LANE_LINE = "类型：{lane}。"


def _lane_of(kind: str) -> str:
    return "红汤" if kind == "red" else "黑汤"


# ======================================================================
# 只给 Stage A 加一行的代理 —— 其余调用原样透传
# ======================================================================
class _LaneClient:
    """`AnthropicMessagesClient` 的**只读包装**: 只在 Stage A 那一条上贴一行。

    ## 为什么需要它

    生产 Stage A 的 user message 由 `_keywords_prompt()` 在
    `gen_keyword_idea` 内部拼好, 函数签名**没有**注入点。要只给 Stage A 加
    一行类型方向、又不改 production, 只能在**传输层**做这一次改写。

    ## 判定"这条是不是 Stage A"

    比对 `system is` 生产那个 `KEYWORD_IDEA_SYSTEM` 字符串 —— **按内容全等**,
    不是按 `in` / 前缀。Stage B / Reviewer / truth audit 的 system 都不是
    它, 所以一律透传。

    ⚠️ 用全等而不是子串: 子串匹配会让任何"顺手提到一句话"的 system 也被
    改到, 而那种误伤在产物上**看不出来**(题照样生成)。

    ## 幂等

    已经带 lane 行就不再贴 —— 免得重试路径上叠两行。
    """

    def __init__(self, inner, lane_line: str):
        self._inner = inner
        self._lane_line = lane_line
        #: 统计: 改写了多少次 / 透传了多少次。报告里要能看到。
        self.rewritten = 0
        self.passed_through = 0
        #: 逐次记 (system 前 20 字, 是否改写) —— 供报告核对代理真的只动了
        #: Stage A。
        self.trace: list = []
        try:
            from story.llm import KEYWORD_IDEA_SYSTEM
            self._stage_a_system = KEYWORD_IDEA_SYSTEM
        except Exception:                        # noqa: BLE001
            self._stage_a_system = None

    def messages(self, system, user, **kw):
        is_stage_a = (self._stage_a_system is not None
                      and system == self._stage_a_system)
        if is_stage_a and not str(user).startswith(self._lane_line):
            user = self._lane_line + "\n" + user
            self.rewritten += 1
            self.trace.append({"kind": "stage_a", "rewritten": True,
                               "user_head": str(user)[:60]})
        else:
            self.passed_through += 1
            self.trace.append({"kind": "other", "rewritten": False,
                               "system_head": str(system)[:20]})
        return self._inner.messages(system, user, **kw)

    def __getattr__(self, name):
        # 其余属性(base_url / cfg / probe_temperature ...) 原样转给真 client。
        return getattr(self._inner, name)


# ======================================================================
# 抽词 —— 与 R1 同一套(生产 KeywordBag)
# ======================================================================
def _corpus_path() -> str:
    from story.keyword_corpus import DEFAULT_CORPUS_PATH
    return str(DEFAULT_CORPUS_PATH)


def _make_bag(base_seed: int):
    from story.keyword_seed import derive_session_seed, load_bag
    ss = derive_session_seed(base_seed)
    bag, meta = load_bag(_corpus_path(), ss)
    return bag, meta, int(ss)


def _sha8(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:8]


# ======================================================================
# 一道题: Stage A -> Stage B(全链路)
# ======================================================================
def _one_puzzle(writer, lane_client, kind: str, idx: int, draw: dict,
                corpus_meta: dict, session_seed: int) -> dict:
    """跑一道题的完整链。**任何一步失败都原样记下来, 不重抽。**

    返回的 dict 就是报告要逐条展开的那份。
    """
    kws = list(draw.get("keywords") or [])
    rec = {
        "id": f"{kind}-{idx:02d}",
        "type": kind,
        "lane": _lane_of(kind),
        "lane_line": LANE_LINE.format(lane=_lane_of(kind)),
        "keywords": kws,
        "keyword_corpus_version": str(corpus_meta.get("corpus_version") or ""),
        "keyword_seed_version": _keyword_seed_version(),
        "keyword_session_seed": session_seed,
        "keyword_draw_index": int(draw.get("index") or 0),
        "keyword_draw_relaxed": int(draw.get("relaxed") or 0),
        # ---- Stage A ----
        "stage_a": {"ok": False, "error": "", "latency_ms": 0},
        # ---- Stage B ----
        "stage_b": {"ok": False, "reject": "", "error": "", "latency_ms": 0},
    }

    # ---------------- Stage A: 生产方法, 原样调用 ----------------
    t0 = time.monotonic()
    idea = writer.gen_keyword_idea(kws)
    rec["stage_a"]["latency_ms"] = int((time.monotonic() - t0) * 1000)
    if idea is None:
        rec["stage_a"]["error"] = "Stage A 未成题(见 run.log)"
        log.error("[%s] %s -> Stage A 未成题", rec["id"], kws)
        return rec
    if idea.get("interrupted"):
        rec["stage_a"]["error"] = "interrupted"
        return rec
    rec["stage_a"].update({
        "ok": True,
        # 脚手架三项 —— **尤其 observed_clues**, 这是本轮怀疑的对象。
        "core_truth": idea.get("core_truth", ""),
        "observed_clues": idea.get("observed_clues", []),
        "event_chain": idea.get("event_chain", []),
        "title": idea.get("title", ""),
        "puzzle": idea.get("puzzle", ""),
        "answer": idea.get("answer", ""),
    })
    log.info("[%s] %s -> Stage A ok (puzzle %d 字, clues %d 条)",
             rec["id"], kws, len(idea.get("puzzle") or ""),
             len(idea.get("observed_clues") or []))

    # ---------------- Stage B: 生产方法, 原样调用 ----------------
    t1 = time.monotonic()
    spec = writer.structure_original_idea(
        title=idea.get("title", ""), puzzle=idea["puzzle"],
        answer=idea["answer"], avoid=None, recent=None)
    rec["stage_b"]["latency_ms"] = int((time.monotonic() - t1) * 1000)
    rec["stage_b"]["ok"] = bool(getattr(spec, "puzzle", ""))
    rec["stage_b"]["prompt_version"] = getattr(spec, "prompt_version", "")
    rec["stage_b"]["error"] = getattr(spec, "error", "") or ""

    m = dict(getattr(spec, "metrics", None) or {})
    rec["stage_b"]["review_decision"] = str(
        m.get("review_decision") or "") or "n/a"
    rec["stage_b"]["rewrite_count"] = m.get("rewrite_count")
    rec["stage_b"]["truth_audit_ok"] = m.get("truth_audit_ok")
    # 失败时的结构化原因标签(G4-R2 §六)。
    rec["stage_b"]["reject"] = str(m.get("reject") or "")
    # 审稿人给的质量判定(原样抄, 不做二次解释)。
    rec["stage_b"]["quality_checks"] = dict(
        m.get("quality_checks") or {}) if isinstance(
        m.get("quality_checks"), dict) else {}
    rec["stage_b"]["review_issues"] = list(m.get("review_issues") or [])

    if rec["stage_b"]["ok"]:
        rec["stage_b"].update({
            "core_answer": getattr(spec, "core_answer", ""),
            "completion_contract": getattr(spec, "completion_contract", ""),
            # ---- Stage B 的结构化产物(报告要逐条列) ----
            "facts": _facts_dump(spec),
            "completion_fact_ids": list(
                getattr(spec, "completion_fact_ids", None) or []),
            "solve_atoms": _atoms_dump(spec),
            "fair_clues": _clues_dump(spec),
            "discovery_beats": _beats_dump(spec),
            "hints": list(getattr(spec, "hints", None) or []),
            "signature": _sig_dump(spec),
        })
        log.info("[%s] -> Stage B ok (facts %d, clues %d, atoms %d, beats %d)",
                 rec["id"], len(rec["stage_b"]["facts"]),
                 len(rec["stage_b"]["fair_clues"]),
                 len(rec["stage_b"]["solve_atoms"]),
                 len(rec["stage_b"]["discovery_beats"]))
    else:
        log.warning("[%s] -> Stage B 拒/失败 reject=%s decision=%s error=%s",
                    rec["id"], rec["stage_b"]["reject"] or "(none)",
                    rec["stage_b"]["review_decision"],
                    rec["stage_b"]["error"][:120])
    return rec


def _keyword_seed_version() -> str:
    from story.keyword_seed import KEYWORD_SEED_VERSION
    return str(KEYWORD_SEED_VERSION)


# ---- 只读的结构导出: 把 dataclass 拍成可读的 dict, 做判定 ----
def _facts_dump(spec) -> list:
    out = []
    for f in (getattr(spec, "facts", None) or []):
        out.append({
            "id": getattr(f, "id", ""),
            "text": getattr(f, "text", ""),
            "hidden": bool(getattr(f, "hidden", False)),
            "kind": str(getattr(f, "kind", "") or ""),
        })
    return out


def _atoms_dump(spec) -> list:
    out = []
    for a in (getattr(spec, "solve_atoms", None) or []):
        out.append({
            "id": getattr(a, "id", ""),
            "text": getattr(a, "text", ""),
            "fact_ids": list(getattr(a, "fact_ids", None) or []),
        })
    return out


def _clues_dump(spec) -> list:
    out = []
    for c in (getattr(spec, "fair_clues", None) or []):
        out.append({
            "quote": getattr(c, "quote", ""),
            "supports": list(getattr(c, "supports_atoms", None)
                             or getattr(c, "supports", None) or []),
        })
    return out


def _beats_dump(spec) -> list:
    out = []
    for b in (getattr(spec, "discovery_beats", None) or []):
        out.append({
            "fact_id": getattr(b, "fact_id", ""),
            "text": getattr(b, "text", ""),
        })
    return out


def _sig_dump(spec) -> dict:
    s = getattr(spec, "signature", None)
    if s is None:
        return {}
    keys = ("mechanism", "solution_shape", "domain", "relation",
            "reveal_mode", "emotion_mode", "time_shape")
    out = {}
    for k in keys:
        v = getattr(s, k, None)
        if v is not None:
            out[k] = str(v)
    return out


# ======================================================================
# 落盘
# ======================================================================
def _write_raw(records: list) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(RAW_JSONL, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _write_manifest(cfg, records: list, base_sha: str, branch: str,
                    git_head: str, corpus_meta: dict, lane_client,
                    model: str = "") -> None:
    a_ok = [r for r in records if r["stage_a"].get("ok")]
    b_ok = [r for r in records if r["stage_b"].get("ok")]
    manifest = {
        "experiment": "full_chain_stageA_stageB",
        "question": (
            "当前生产 Case-first Stage A / Stage B, 前面加一行 red/black "
            "方向后, 最终展示出来的题会不会更像真正的海龟汤, 还是会被 "
            "observed_clues / fair_clues 重新写成长案情简介。"
        ),
        "base_sha": base_sha,
        "branch": branch,
        "git_head_at_run": git_head,
        "model_requested": model,
        "model_returned": sorted(
            {r.get("stage_b", {}).get("prompt_version") for r in records if r}
            - {None, ""}) or [],
        "base_url": getattr(cfg, "base_url", ""),
        "n_per_type": N_PER_TYPE,
        "seed_red": SEED_RED,
        "seed_black": SEED_BLACK,
        "keyword_mechanism": {
            "source": "story.keyword_seed.KeywordBag (生产同款)",
            "corpus_version": str(corpus_meta.get("corpus_version") or ""),
            "keyword_count": corpus_meta.get("keyword_count"),
            "keyword_seed_version": _keyword_seed_version(),
            "per_candidate": [
                {"id": r["id"], "keywords": r["keywords"],
                 "draw_index": r["keyword_draw_index"]}
                for r in records
            ],
        },
        "production_prompts_reused": {
            "stage_a": ["KEYWORD_IDEA_SYSTEM", "_TOOL_KEYWORD_IDEA",
                        "_keywords_prompt()"],
            "stage_b": ["structure_original_idea", "STRUCTURE_SYSTEM",
                        "_TOOL_STRUCTURE", "Reviewer", "truth audit",
                        "validate_spec"],
            "note": (
                "全部**原样调用** production 方法; story/llm.py 的常量一个"
                "都没改。唯一的增量是 Stage A user message 前的一行 lane。"
            ),
        },
        "lane_injection": {
            "template": LANE_LINE,
            "how": (
                "_LaneClient 代理: 只在 system == KEYWORD_IDEA_SYSTEM 的"
                "那一次调用上把 lane 行贴到 user 前面; 其余调用(Stage B / "
                "Reviewer / truth audit)原样透传。"
            ),
            "stage_a_rewritten": lane_client.rewritten,
            "other_passed_through": lane_client.passed_through,
        },
        "counts": {
            "total": len(records),
            "stage_a_ok": len(a_ok),
            "stage_b_ok": len(b_ok),
            "stage_b_rejected": len([r for r in records
                                     if r["stage_a"].get("ok")
                                     and not r["stage_b"].get("ok")]),
            "by_type": {
                k: {
                    "stage_a_ok": len(
                        [r for r in a_ok if r["type"] == k]),
                    "stage_b_ok": len(
                        [r for r in b_ok if r["type"] == k]),
                }
                for k in ("red", "black")
            },
        },
        "rejects": [
            {"id": r["id"], "type": r["type"],
             "reject": r["stage_b"].get("reject") or "",
             "review_decision": r["stage_b"].get("review_decision") or "",
             "error": r["stage_b"].get("error") or ""}
            for r in records if not r["stage_b"].get("ok")
        ],
        "no_resample_on_reject": (
            "Stage B 拒绝的条目**保留 Stage A 原始结果**并原样记录, 不重抽"
            "到通过 —— 要看的是当前真实链会怎么处理它。"
        ),
        "not_done": [
            "自动评分 / 想玩不想玩 / 红黑判定器",
            "新增 reviewer 或 gate",
            "另写结构器",
            "入池 / played / pool_used / archive",
        ],
        "outputs": {
            "raw": "data/full_chain_experiment/raw.jsonl",
            "report": "data/full_chain_experiment/report.md",
        },
    }
    with open(MANIFEST_JSON, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _write_report(records: list, manifest: dict) -> None:
    """人读报告: 每道只展开用户点名的那些字段。**不下结论、不评分。**"""
    L = []
    A = L.append

    A("# 全链路观测: 关键词 -> Stage A -> Stage B -> PuzzleSpec")
    A("")
    A("当前生产链**原样**跑, 只在 Stage A 的 user 前加一行类型方向。")
    A("本报告**不评分、不排序、不做红黑判定** —— 请直接肉眼读这 10 道。")
    A("")
    A("## 事实")
    A("")
    A(f"- base SHA: `{manifest['base_sha']}`")
    A(f"- branch: `{manifest['branch']}`")
    A(f"- 生成时 HEAD: `{manifest['git_head_at_run']}`")
    A(f"- model: `{manifest['model_requested']}`")
    A(f"- 词库: `{manifest['keyword_mechanism']['corpus_version']}` "
      f"({manifest['keyword_mechanism']['keyword_count']} 词)")
    A(f"- 红 {manifest['n_per_type']} / 黑 {manifest['n_per_type']}, "
      f"共 `{manifest['counts']['total']}` 道")
    A(f"- Stage A 成功 `{manifest['counts']['stage_a_ok']}` / "
      f"Stage B 成功 `{manifest['counts']['stage_b_ok']}` / "
      f"Stage B 被拒 `{manifest['counts']['stage_b_rejected']}`")
    A(f"- 原始记录: `{manifest['outputs']['raw']}`")
    A("")
    A("## 唯一的 prompt 增量")
    A("")
    A("生产 prompt **一字未改**。Stage A 的 user message 前面贴一行:")
    A("")
    A("```text")
    A(LANE_LINE)
    A("```")
    A("")
    A(f"实现: {manifest['lane_injection']['how']}")
    A(f"本次运行: Stage A 改写 `{manifest['lane_injection']['stage_a_rewritten']}` "
      f"次, 其余透传 `{manifest['lane_injection']['other_passed_through']}` 次。")
    A("")
    if manifest["rejects"]:
        A("## Stage B 被拒的条目")
        A("")
        A("| id | type | reject | review decision |")
        A("|---|---|---|---|")
        for r in manifest["rejects"]:
            A(f"| `{r['id']}` | {r['type']} | `{r['reject']}` | "
              f"{r['review_decision']} |")
        A("")
        A("(被拒条目**没有重抽**; 下面仍完整列出它们的 Stage A 原始结果。)")
        A("")
    A("---")
    A("")

    for r in records:
        a = r["stage_a"]
        b = r["stage_b"]
        A(f"## {r['id']} — {r['lane']} — 关键词: "
          f"{'、'.join(r['keywords'])}")
        A("")
        if not a.get("ok"):
            A(f"**Stage A 未成题**: {a.get('error') or '(无)'}")
            A("")
            A("---")
            A("")
            continue

        A("### 1. type + keywords")
        A("")
        A(f"- type: `{r['type']}` ({r['lane']})")
        A(f"- keywords: {'、'.join(r['keywords'])}")
        A(f"- lane 行: `{r['lane_line']}`")
        A("")
        A("### 2. core_truth")
        A("")
        A(a["core_truth"] or "_(空)_")
        A("")
        A("### 3. observed_clues")
        A("")
        for i, c in enumerate(a["observed_clues"] or [], 1):
            A(f"{i}. {c}")
        if not a["observed_clues"]:
            A("_(空)_")
        A("")
        A("### 4. event_chain")
        A("")
        for i, c in enumerate(a["event_chain"] or [], 1):
            A(f"{i}. {c}")
        if not a["event_chain"]:
            A("_(空)_")
        A("")
        A("### 5. 最终汤面 puzzle")
        A("")
        A("> " + (a["puzzle"] or "_(空)_").replace("\n", "\n> "))
        A("")
        A(f"_(puzzle 长度: {len(a['puzzle'] or '')} 字)_")
        A("")
        A("### 6. 最终汤底 answer")
        A("")
        A("> " + (a["answer"] or "_(空)_").replace("\n", "\n> "))
        A("")
        A(f"_(answer 长度: {len(a['answer'] or '')} 字)_")
        A("")
        A("### 7. Stage B")
        A("")
        if not b.get("ok"):
            A(f"- **未通过**: reject=`{b.get('reject') or '(none)'}` "
              f"decision=`{b.get('review_decision')}`")
            if b.get("error"):
                A(f"- error: {b['error'][:300]}")
            if b.get("review_issues"):
                A("- review_issues:")
                for it in b["review_issues"]:
                    A(f"  - {it}")
            A("")
            A("---")
            A("")
            continue

        A(f"- **通过** (prompt_version=`{b.get('prompt_version')}`, "
          f"decision=`{b.get('review_decision')}`)")
        A(f"- truth_audit_ok: `{b.get('truth_audit_ok')}`")
        A("")
        A("#### core_answer")
        A("")
        A(b.get("core_answer") or "_(空)_")
        A("")
        if b.get("completion_contract"):
            A("#### completion_contract")
            A("")
            A(b["completion_contract"])
            A("")
        A("#### facts")
        A("")
        A("| id | hidden | kind | text |")
        A("|---|---|---|---|")
        for f in b.get("facts") or []:
            A(f"| `{f['id']}` | {'是' if f['hidden'] else ''} | "
              f"{f['kind']} | {f['text']} |")
        A("")
        A("#### completion_fact_ids")
        A("")
        A("`" + "`, `".join(b.get("completion_fact_ids") or []) + "`")
        A("")
        if b.get("solve_atoms"):
            A("#### solve_atoms")
            A("")
            for at in b["solve_atoms"]:
                A(f"- `{at['id']}` {at['text']} "
                  f"(facts: {', '.join(at['fact_ids'])})")
            A("")
        A("#### fair_clues")
        A("")
        if b.get("fair_clues"):
            A("| quote | supports |")
            A("|---|---|")
            for c in b["fair_clues"]:
                A(f"| {c['quote']} | {', '.join(c['supports'])} |")
        else:
            A("_(无)_")
        A("")
        A("#### hints")
        A("")
        for i, h in enumerate(b.get("hints") or [], 1):
            A(f"{i}. {h}")
        if not b.get("hints"):
            A("_(无)_")
        A("")
        if b.get("discovery_beats"):
            A("#### discovery_beats")
            A("")
            for bt in b["discovery_beats"]:
                A(f"- `{bt['fact_id']}` {bt['text']}")
            A("")
        if b.get("signature"):
            A("#### signature")
            A("")
            for k, v in b["signature"].items():
                A(f"- {k}: `{v}`")
            A("")
        A("---")
        A("")

    with open(REPORT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(L))


# ======================================================================
# 入口
# ======================================================================
def _setup_log() -> None:
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
    except Exception:                          # noqa: BLE001
        return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--report-only", action="store_true")
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
        _write_report(records, manifest)
        log.info("报告已重出: %s", REPORT_MD)
        return 0

    if not args.run:
        log.error("要么 --run, 要么 --report-only")
        return 2

    from story.config import Config, LLMConfig
    from story.llm import AnthropicMessagesClient, PuzzleWriter

    cfg = Config()
    real_client = AnthropicMessagesClient(LLMConfig())
    log.info("model=%s", real_client.cfg.model)

    bag_red, corpus_meta, ss_red = _make_bag(SEED_RED)
    bag_black, _meta_b, ss_black = _make_bag(SEED_BLACK)
    log.info("词袋: corpus=%s count=%s", corpus_meta.get("corpus_version"),
             corpus_meta.get("keyword_count"))

    plan = ([("red", i + 1, bag_red.draw(), ss_red) for i in range(N_PER_TYPE)]
            + [("black", i + 1, bag_black.draw(), ss_black)
               for i in range(N_PER_TYPE)])
    log.info("已抽定 %d 组关键词:", len(plan))
    for kind, idx, d, _ in plan:
        log.info("  [%s-%02d] %s", kind, idx, "、".join(d["keywords"]))

    records = []
    #: 每组一个**独立**的 lane 代理与 writer —— 免得 trace 互相污染。
    for kind, idx, draw, sess in plan:
        lane = LANE_LINE.format(lane=_lane_of(kind))
        lc = _LaneClient(real_client, lane)
        writer = PuzzleWriter(client=lc, runtime_cfg=cfg)
        rec = _one_puzzle(writer, lc, kind, idx, draw, corpus_meta, sess)
        rec["lane_client_stats"] = {
            "stage_a_rewritten": lc.rewritten,
            "other_passed_through": lc.passed_through,
        }
        records.append(rec)
        _write_raw(records)

    _write_raw(records)
    _write_manifest(cfg, records, base_sha, branch, git_head, corpus_meta,
                    _AggLane(records), real_client.cfg.model)
    _write_report(records, json.load(open(MANIFEST_JSON, encoding="utf-8")))
    a_ok = len([r for r in records if r["stage_a"].get("ok")])
    b_ok = len([r for r in records if r["stage_b"].get("ok")])
    log.info("完成: Stage A %d/%d, Stage B %d/%d。原始 -> %s",
             a_ok, len(records), b_ok, len(records), RAW_JSONL)
    return 0


class _AggLane:
    """把逐组的 lane 统计合成 manifest 要的两个总数。"""

    def __init__(self, records):
        self.rewritten = sum(r.get("lane_client_stats", {})
                             .get("stage_a_rewritten", 0) for r in records)
        self.passed_through = sum(r.get("lane_client_stats", {})
                                  .get("other_passed_through", 0)
                                  for r in records)


if __name__ == "__main__":
    raise SystemExit(main())
