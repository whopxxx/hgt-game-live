#!/usr/bin/env python
# coding: utf-8
"""R3: 最小对照实验 —— 同一批汤底, 只换"汤面怎么截"。

## 这个实验在问什么

R2 已经定位了长汤面的来源: **Stage A 那一次调用同时想故事、想线索、想汤面**。
`structure_original_idea()` 冻结 puzzle/answer, 所以 78~136 字的长汤面在
Stage A 就已经成形了 —— 是 `observed_clues -> puzzle` 这条链把它撑长的。

R3 只动**一个变量**:

    R2:  observed_clues 决定 puzzle   (同一次调用里最后才写)
    R3:  从**已经写好的完整汤底**单独截一个极短场景

样本**完全不动**: 关键词、core_truth、answer 全部复用 R2 那 10 条。
所以两次的差异只可能来自"汤面是怎么截出来的"。

## 新增的调用: 一个隔离的 surface-extractor

极短 prompt(见 `SURFACE_SYSTEM` / `_surface_user`), **只要一个字段**
`puzzle`。任务书 R3 明确禁止在这一步加:

    * fair clue 规则      * 反转数量
    * 问句要求            * 人物关系
    * "必须几条信息"      * 任何别的 shape 约束

## 然后: 原样再跑当前 Stage B

用 `新短汤面 + R2 原 answer` 调**当前完全不改的**
`PuzzleWriter.structure_original_idea()`。

如果它因为"没问句 / fair_clue / completion / hint"拒绝 —— **照实记录,
不修到通过**。这正好回答第二个问题:

> 当前 Stage B 到底能不能承接真正的短汤面?

## 不做的事

    * 不重新抽关键词;  * 不重新想 10 个故事;  * 不重抽;
    * 不改 production; * 不改 KEYWORD_IDEA_SYSTEM; * 不改 observed_clues 规则;
    * 不自动评分; * 不另加判定器; * 不入池。

## 运行

    .venv/Scripts/python.exe tools/experiment_short_surface.py --run
    .venv/Scripts/python.exe tools/experiment_short_surface.py --report-only

产物(⚠️ `data/**/*.jsonl` 被 .gitignore 挡着, 归档要 `git add -f`):

    data/short_surface_experiment/raw.jsonl
    data/short_surface_experiment/run_manifest.json
    data/short_surface_experiment/report.md    <- 并排展示 R2 vs R3 汤面
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

R2_RAW = os.path.join(ROOT, "data", "full_chain_experiment", "raw.jsonl")

OUT_DIR = os.path.join(ROOT, "data", "short_surface_experiment")
RAW_JSONL = os.path.join(OUT_DIR, "raw.jsonl")
MANIFEST_JSON = os.path.join(OUT_DIR, "run_manifest.json")
REPORT_MD = os.path.join(OUT_DIR, "report.md")

TAG = "SHORT-SURFACE"
log = logging.getLogger(TAG)

#: 技术失败最多重试几次(不含首次)。**只针对网络/HTTP/空 tool_input**,
#: 绝不因为"截得不好"重抽。
MAX_TECHNICAL_RETRIES = 2

#: 截取预算。汤面要短, 所以预算不大 —— 给足 JSON 包装的余量即可。
MAX_TOKENS = 900

TEMPERATURE = 0.8


# ======================================================================
# Prompt —— 刻意极短。**不要加规则。**
# ======================================================================
#: 任务书 R3 给的核心意思, 照抄, 不加条款。
SURFACE_SYSTEM = (
    "从下面这个完整汤底中，截取最让人想追问的一小段作为海龟汤汤面。\n"
    "不要概括完整故事，不要解释背景。\n"
    "只写 1～3 句，尽量简短。"
)

#: user 只附 canonical 汤底。**不附** observed_clues —— 那样等于把 R2 的
#: 信息提前暴露路径又搬回来, 变量就不止一个了。
_USER_TEMPLATE = "【完整汤底】\n{answer}\n"

#: 输出 schema **只要 puzzle 一个字段**。
_TOOL_SURFACE = {
    "name": "emit_surface",
    "description": "交出一段极短汤面。",
    "input_schema": {
        "type": "object",
        "properties": {
            "puzzle": {
                "type": "string",
                "description": "从汤底截取的极短汤面(1~3 句)。",
                "minLength": 1,
            },
        },
        "required": ["puzzle"],
    },
}


def _surface_user(answer: str) -> str:
    return _USER_TEMPLATE.format(answer=answer)


def _sha8(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:8]


# ======================================================================
# 一次截取
# ======================================================================
def _extract_surface(client, rec: dict, corpus_meta: dict) -> dict:
    """对**已有**的 R2 汤底截一次短汤面。**不做内容判断。**

    只有技术失败(HTTP / 网络 / 空 tool_input)才重试。
    """
    answer = str(rec["stage_a"].get("answer") or "").strip()
    out = {
        "id": rec["id"],
        "type": rec["type"],
        "lane": rec.get("lane") or "",
        "keywords": list(rec.get("keywords") or []),
        "keyword_corpus_version": rec.get("keyword_corpus_version") or "",
        "keyword_session_seed": rec.get("keyword_session_seed"),
        "keyword_draw_index": rec.get("keyword_draw_index"),
        # ---- R2 的 canonical 汤底(复用, 不重新生成) ----
        "core_truth": rec["stage_a"].get("core_truth", ""),
        "answer": answer,
        "r2_puzzle": rec["stage_a"].get("puzzle", ""),
        "r2_stage_b_ok": bool(rec["stage_b"].get("ok")),
        # ---- R3 新增 ----
        "surface": {"ok": False, "attempts": 0, "technical_error": "",
                    "puzzle": "", "model": "", "usage": {},
                    "latency_ms": 0},
        "stage_b": {"ok": False, "reject": "", "error": "",
                    "latency_ms": 0},
    }

    system = SURFACE_SYSTEM
    user = _surface_user(answer)
    t0 = time.monotonic()
    last_err = ""
    for attempt in range(MAX_TECHNICAL_RETRIES + 1):
        out["surface"]["attempts"] = attempt + 1
        res = client.messages(system, user, max_tokens=MAX_TOKENS,
                              tool=_TOOL_SURFACE, temperature=TEMPERATURE)
        if res.error:
            last_err = res.error
            log.warning("[%s] 截取技术失败(第 %d 次): %s",
                        rec["id"], attempt + 1, last_err[:120])
            continue
        d = res.tool_input or {}
        pz = str(d.get("puzzle") or "").strip()
        if not pz:
            last_err = f"没交出 puzzle (tool_input={str(res.tool_input)[:120]})"
            continue
        out["surface"].update({"ok": True, "puzzle": pz,
                               "model": res.model or "",
                               "usage": res.usage or {}})
        last_err = ""
        break
    out["surface"]["latency_ms"] = int((time.monotonic() - t0) * 1000)
    out["surface"]["technical_error"] = last_err
    return out


def _run_stage_b(writer, rec: dict) -> dict:
    """用 **新短汤面 + R2 原 answer** 跑当前**完全不改**的 Stage B。

    标题沿用 R2 的(如果 raw 里有), 没有就空 —— 不是本轮的变量。
    """
    b = rec["stage_b"]
    if not rec["surface"].get("ok"):
        b["error"] = "surface 未成, 跳过 Stage B"
        return rec
    t0 = time.monotonic()
    spec = writer.structure_original_idea(
        title="", puzzle=rec["surface"]["puzzle"],
        answer=rec["answer"], avoid=None, recent=None)
    b["latency_ms"] = int((time.monotonic() - t0) * 1000)
    b["ok"] = bool(getattr(spec, "puzzle", ""))
    b["prompt_version"] = getattr(spec, "prompt_version", "")
    b["error"] = getattr(spec, "error", "") or ""
    m = dict(getattr(spec, "metrics", None) or {})
    b["review_decision"] = str(m.get("review_decision") or "") or "n/a"
    b["truth_audit_ok"] = m.get("truth_audit_ok")
    b["reject"] = str(m.get("reject") or "")
    b["review_issues"] = list(m.get("review_issues") or [])

    if b["ok"]:
        b.update({
            "core_answer": getattr(spec, "core_answer", ""),
            "completion_contract": getattr(spec, "completion_contract", ""),
            "facts": _facts_dump(spec),
            "completion_fact_ids": list(
                getattr(spec, "completion_fact_ids", None) or []),
            "solve_atoms": _atoms_dump(spec),
            "fair_clues": _clues_dump(spec),
            "discovery_beats": _beats_dump(spec),
            "hints": list(getattr(spec, "hints", None) or []),
            "signature": _sig_dump(spec),
        })
        log.info("[%s] -> Stage B ok (short puzzle %d 字, facts %d)",
                 rec["id"], len(rec["surface"]["puzzle"]),
                 len(b["facts"]))
    else:
        log.warning("[%s] -> Stage B 拒/失败 reject=%s decision=%s error=%s",
                    rec["id"], b["reject"] or "(none)",
                    b["review_decision"], b["error"][:140])
    return rec


# ---- 只读结构导出(与 R2 工具同口径, 便于并排对照) ----
def _facts_dump(spec) -> list:
    return [{"id": getattr(f, "id", ""), "text": getattr(f, "text", ""),
             "hidden": bool(getattr(f, "hidden", False)),
             "kind": str(getattr(f, "kind", "") or "")}
            for f in (getattr(spec, "facts", None) or [])]


def _atoms_dump(spec) -> list:
    return [{"id": getattr(a, "id", ""), "text": getattr(a, "text", ""),
             "fact_ids": list(getattr(a, "fact_ids", None) or [])}
            for a in (getattr(spec, "solve_atoms", None) or [])]


def _clues_dump(spec) -> list:
    return [{"quote": getattr(c, "quote", ""),
             "supports": list(getattr(c, "supports_atoms", None)
                              or getattr(c, "supports", None) or [])}
            for c in (getattr(spec, "fair_clues", None) or [])]


def _beats_dump(spec) -> list:
    return [{"fact_id": getattr(b, "fact_id", ""),
             "text": getattr(b, "text", "")}
            for b in (getattr(spec, "discovery_beats", None) or [])]


def _sig_dump(spec) -> dict:
    s = getattr(spec, "signature", None)
    if s is None:
        return {}
    out = {}
    for k in ("mechanism", "solution_shape", "domain", "relation",
              "reveal_mode", "emotion_mode", "time_shape"):
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


def _write_manifest(records: list, base_sha: str, branch: str,
                    git_head: str, model: str) -> None:
    s_ok = [r for r in records if r["surface"].get("ok")]
    b_ok = [r for r in records if r["stage_b"].get("ok")]
    r2_len = [len(r["r2_puzzle"] or "") for r in records]
    r3_len = [len(r["surface"].get("puzzle") or "") for r in s_ok]
    manifest = {
        "experiment": "short_surface_extraction",
        "question": (
            "把「故事创作」和「汤面截取」拆开后, 汤面是不是立刻正常了; "
            "以及当前 Stage B 到底能不能承接真正的短汤面。"
        ),
        "single_variable": (
            "样本完全相同(R2 那 10 条: type / keywords / core_truth / "
            "answer 全部复用, 不重新抽词、不重新想故事)。唯一变量是"
            "'汤面怎么来': R2 由 observed_clues 在同一次调用里决定, "
            "R3 从已写好的完整汤底单独截取。"
        ),
        "reused_from": "data/full_chain_experiment/raw.jsonl (R2)",
        "base_sha": base_sha,
        "branch": branch,
        "git_head_at_run": git_head,
        "model_requested": model,
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
        "counts": {
            "total": len(records),
            "surface_ok": len(s_ok),
            "surface_technical_failed": len(records) - len(s_ok),
            "stage_b_ok": len(b_ok),
            "stage_b_rejected": len(s_ok) - len(b_ok),
            "by_type": {
                k: {
                    "stage_b_ok": len(
                        [r for r in b_ok if r["type"] == k]),
                    "stage_b_rejected": len(
                        [r for r in s_ok
                         if r["type"] == k and not r["stage_b"].get("ok")]),
                }
                for k in ("red", "black")
            },
        },
        "length_comparison": {
            "r2_puzzle_chars": r2_len,
            "r3_puzzle_chars": r3_len,
            "r2_min": min(r2_len) if r2_len else None,
            "r2_max": max(r2_len) if r2_len else None,
            "r3_min": min(r3_len) if r3_len else None,
            "r3_max": max(r3_len) if r3_len else None,
            "r2_mean": (round(sum(r2_len) / len(r2_len), 1)
                        if r2_len else None),
            "r3_mean": (round(sum(r3_len) / len(r3_len), 1)
                        if r3_len else None),
        },
        "prompts": {
            "surface_system": SURFACE_SYSTEM,
            "surface_system_chars": len(SURFACE_SYSTEM),
            "surface_system_sha8": _sha8(SURFACE_SYSTEM),
            "user_template": _USER_TEMPLATE,
            "tool_schema": _TOOL_SURFACE["input_schema"],
            "answers_are_canonical_from_r2": True,
            "observed_clues_deliberately_not_passed": (
                "user 只附完整汤底, **不附** observed_clues —— 附上就等于把 "
                "R2 那条信息暴露路径搬回来, 变量就不止一个了。"
            ),
        },
        "not_done": [
            "重新抽关键词 / 重新想故事 / 重抽",
            "自动评分 / 额外判定器",
            "改 production / KEYWORD_IDEA_SYSTEM / observed_clues 规则",
            "入池 / played / pool_used / archive",
        ],
        "rejects": [
            {"id": r["id"], "type": r["type"],
             "reject": r["stage_b"].get("reject") or "",
             "review_decision": r["stage_b"].get("review_decision") or "",
             "issues": r["stage_b"].get("review_issues") or []}
            for r in records if not r["stage_b"].get("ok")
        ],
        "outputs": {
            "raw": "data/short_surface_experiment/raw.jsonl",
            "report": "data/short_surface_experiment/report.md",
        },
    }
    with open(MANIFEST_JSON, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _write_report(records: list, manifest: dict) -> None:
    """并排展示 R2 / R3 汤面。**不评分、不排序、不判定。**"""
    L = []
    A = L.append

    A("# R3: 同一批汤底, 只换「汤面怎么截」")
    A("")
    A("样本与 R2 **完全相同**(同关键词 / 同 core_truth / 同 answer)。")
    A("唯一变量: 汤面是 `observed_clues` 在同一次调用里决定的(R2),")
    A("还是从已写好的完整汤底**单独截取**的(R3)。")
    A("")
    A("本报告**不评分、不排序、不加判定器** —— 请直接读两栏汤面。")
    A("")
    A("## 事实")
    A("")
    A(f"- base SHA: `{manifest['base_sha']}`")
    A(f"- branch: `{manifest['branch']}`")
    A(f"- 生成时 HEAD: `{manifest['git_head_at_run']}`")
    A(f"- model: `{manifest['model_requested']}`")
    A(f"- 共 `{manifest['counts']['total']}` 道(复用 R2 那批)")
    A(f"- 截取成功 `{manifest['counts']['surface_ok']}` / "
      f"技术失败 `{manifest['counts']['surface_technical_failed']}`")
    A(f"- Stage B 通过 `{manifest['counts']['stage_b_ok']}` / "
      f"被拒 `{manifest['counts']['stage_b_rejected']}`")
    A(f"- 原始记录: `{manifest['outputs']['raw']}`")
    A("")
    lc = manifest["length_comparison"]
    A("## 汤面长度对照")
    A("")
    A("| | R2 原汤面 | R3 新短汤面 |")
    A("|---|---|---|")
    A(f"| 最短 | {lc['r2_min']} 字 | {lc['r3_min']} 字 |")
    A(f"| 最长 | {lc['r2_max']} 字 | {lc['r3_max']} 字 |")
    A(f"| 平均 | {lc['r2_mean']} 字 | {lc['r3_mean']} 字 |")
    A("")
    A("## 截取用的 Prompt(原文)")
    A("")
    A(f"### system —— {manifest['prompts']['surface_system_chars']} 字 "
      f"(`{manifest['prompts']['surface_system_sha8']}`)")
    A("")
    A("```text")
    A(manifest["prompts"]["surface_system"])
    A("```")
    A("")
    A("### user 模板")
    A("")
    A("```text")
    A(manifest["prompts"]["user_template"])
    A("```")
    A("")
    A("### 输出 schema")
    A("")
    A("```json")
    A(json.dumps(manifest["prompts"]["tool_schema"],
                 ensure_ascii=False, indent=2))
    A("```")
    A("")
    if manifest["rejects"]:
        A("## Stage B 被拒的条目")
        A("")
        A("| id | type | reject | review decision | issues |")
        A("|---|---|---|---|---|")
        for r in manifest["rejects"]:
            iss = "; ".join(str(x) for x in (r.get("issues") or []))
            A(f"| `{r['id']}` | {r['type']} | `{r['reject']}` | "
              f"{r['review_decision']} | {iss[:160]} |")
        A("")
        A("(没有重抽、没有修到通过。)")
        A("")
    A("---")
    A("")

    for r in records:
        s = r["surface"]
        b = r["stage_b"]
        A(f"## {r['id']} — {r.get('lane') or r['type']} — 关键词: "
          f"{'、'.join(r['keywords'])}")
        A("")
        A("### type + keywords")
        A("")
        A(f"- type: `{r['type']}`")
        A(f"- keywords: {'、'.join(r['keywords'])}")
        A("")
        A("### canonical core_truth / answer(来自 R2, 未改)")
        A("")
        A("**core_truth**")
        A("")
        A(r["core_truth"] or "_(空)_")
        A("")
        A("**answer**")
        A("")
        A("> " + (r["answer"] or "_(空)_").replace("\n", "\n> "))
        A("")
        A(f"_(answer 长度: {len(r['answer'] or '')} 字)_")
        A("")
        A("### R2 原汤面 (observed_clues 决定)")
        A("")
        A("> " + (r["r2_puzzle"] or "_(空)_").replace("\n", "\n> "))
        A("")
        A(f"_(**{len(r['r2_puzzle'] or '')} 字**)_")
        A("")
        A("### R3 新短汤面 (从汤底单独截取)")
        A("")
        if s.get("ok"):
            A("> " + s["puzzle"].replace("\n", "\n> "))
            A("")
            A(f"_(**{len(s['puzzle'])} 字**)_")
        else:
            A(f"_(截取技术失败, {s.get('attempts')} 次尝试)_ "
              f"{s.get('technical_error', '')[:200]}")
        A("")
        A("### Stage B (用新短汤面 + R2 原 answer, 当前代码不改)")
        A("")
        if not b.get("ok"):
            A(f"- **未通过**: reject=`{b.get('reject') or '(none)'}` "
              f"decision=`{b.get('review_decision')}`")
            if b.get("error"):
                A(f"- error: {b['error'][:300]}")
            for it in (b.get("review_issues") or []):
                A(f"- issue: {it}")
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
        for f in (b.get("facts") or []):
            A(f"| `{f['id']}` | {'是' if f['hidden'] else ''} | "
              f"{f['kind']} | {f['text']} |")
        A("")
        A("#### completion_fact_ids")
        A("")
        A("`" + "`, `".join(b.get("completion_fact_ids") or []) + "`")
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

    if not os.path.exists(R2_RAW):
        log.error("找不到 R2 原始结果: %s —— 必须先有 R2", R2_RAW)
        return 2
    with open(R2_RAW, encoding="utf-8") as f:
        r2 = [json.loads(x) for x in f if x.strip()]
    r2 = [r for r in r2 if r["stage_a"].get("ok")]
    log.info("复用 R2 的 %d 条汤底(不重新生成故事)", len(r2))

    from story.config import Config, LLMConfig
    from story.llm import AnthropicMessagesClient, PuzzleWriter

    cfg = Config()
    client = AnthropicMessagesClient(LLMConfig())
    writer = PuzzleWriter(client=client, runtime_cfg=cfg)
    log.info("model=%s", client.cfg.model)
    log.info("surface system = %d 字 (%s)", len(SURFACE_SYSTEM),
             _sha8(SURFACE_SYSTEM))

    records = []
    for rec in r2:
        out = _extract_surface(client, rec, {})
        if out["surface"].get("ok"):
            log.info("[%s] 截取 ok: R2 %d 字 -> R3 %d 字",
                     out["id"], len(out["r2_puzzle"]),
                     len(out["surface"]["puzzle"]))
        else:
            log.error("[%s] 截取技术失败: %s", out["id"],
                      out["surface"]["technical_error"][:140])
        out = _run_stage_b(writer, out)
        records.append(out)
        _write_raw(records)

    _write_raw(records)
    _write_manifest(records, base_sha, branch, git_head, client.cfg.model)
    _write_report(records, json.load(open(MANIFEST_JSON, encoding="utf-8")))
    s_ok = len([r for r in records if r["surface"].get("ok")])
    b_ok = len([r for r in records if r["stage_b"].get("ok")])
    log.info("完成: 截取 %d/%d, Stage B %d/%d。原始 -> %s",
             s_ok, len(records), b_ok, len(records), RAW_JSONL)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
