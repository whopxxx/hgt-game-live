#!/usr/bin/env python
# coding: utf-8
"""R4 production smoke —— 新三段式生成链(Story -> Surface -> Structure)。

## 这是 smoke, 不是实验

任务书 R4 §5 明确: 实现完后只做一个 **5 红 + 5 黑**的 smoke, 走**真实**
production 链, 看它跑不跑得通、产出什么样。所以:

    * 完全走生产入口 `keyword_spec()`(不经任何替身);
    * 真实网关、真实 `KeywordBag` 抽词、真实 lane 掷骰;
    * **不重抽** —— 失败就照实记失败;
    * 不做自动评分 / 排名 / 判定器。

## 与生产的关系

这条链**就是**生产链(见 `story/keyword_seed.py::keyword_spec`), 本工具
只是把它跑 10 次并落盘。它不 import `PuzzleWriter` 以外的任何东西, 也不
改任何常量。

## 运行

    .venv/Scripts/python.exe tools/r4_smoke.py --run
    .venv/Scripts/python.exe tools/r4_smoke.py --report-only

产物(报告入库, 原始 jsonl 不入库 —— 与其它 smoke 同规矩):

    data/r4_smoke/raw.jsonl
    data/r4_smoke/report.md
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

OUT_DIR = os.path.join(ROOT, "data", "r4_smoke")
RAW_JSONL = os.path.join(OUT_DIR, "raw.jsonl")
REPORT_MD = os.path.join(OUT_DIR, "report.md")

TAG = "R4-SMOKE"
log = logging.getLogger(TAG)

#: 每类固定条数(**跑之前写死**)。
N_PER_TYPE = 5

#: 两类的 base seed(生产同款 `derive_session_seed` 派生)。
SEED_RED = 20260925
SEED_BLACK = 20260926


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


def _one(writer, bag, session_seed: int, corpus_meta: dict,
         lane_rng) -> dict:
    """跑**一次生产链**。失败照实记。"""
    from story.keyword_seed import keyword_spec
    keys = bag.draw()
    rec = {
        "keywords": list(keys["keywords"]),
        "draw_index": int(keys.get("index") or 0),
        "session_seed": session_seed,
        "corpus_version": corpus_meta.get("corpus_version", ""),
        "ok": False, "reason": "", "lane": "",
        "answer": "", "puzzle": "", "puzzle_len": 0,
        "prompt_version": "", "stage_b": {},
    }
    t0 = time.monotonic()
    # ⚠️ lane 由 `keyword_spec` 内部掷(它自己派生一把独立 rng) —— smoke
    # 不插手, 免得"smoke 的 lane 序列"与生产不同。
    spec, why = keyword_spec(writer, bag, session_seed,
                             corpus_version=corpus_meta.get("corpus_version", ""),
                             lane_rng=lane_rng)
    rec["latency_ms"] = int((time.monotonic() - t0) * 1000)
    if spec is None:
        rec["reason"] = why or "gen_fail"
        # ⚠️ `keyword_spec` 只回一个**粗粒度**的 "gen_fail" —— 三个完全
        # 不同的成因(Story 没成 / Surface 没成 / Stage B 拒)共用它。
        # 实播复盘时"只有 keyword2 未成题"就是这么来的。所以 smoke 这里
        # 把 writer 的**侧信道**抄出来, 让报告能说清是哪一段断的。
        # (这是 G4-R2 §六 的同一套约定。)
        rec["stage_reject"] = getattr(writer, "_last_reject", "") or ""
        rec["stage_error"] = (getattr(writer, "_last_reject", "")
                              or "(侧信道为空: 失败发生在 Story/Surface, "
                                 "见 run.log)")
        return rec
    m = dict(getattr(spec, "metrics", None) or {})
    rec.update({
        "ok": bool(getattr(spec, "puzzle", "")),
        "lane": str(m.get("lane") or ""),
        "answer": getattr(spec, "answer", ""),
        "puzzle": getattr(spec, "puzzle", ""),
        "puzzle_len": len(getattr(spec, "puzzle", "") or ""),
        "prompt_version": getattr(spec, "prompt_version", ""),
        "story_prompt_version": m.get("story_prompt_version"),
        "surface_prompt_version": m.get("surface_prompt_version"),
        "core_answer": getattr(spec, "core_answer", ""),
        "facts": [{"id": f.id, "text": f.text, "kind": f.kind,
                   "visibility": f.visibility}
                  for f in (getattr(spec, "facts", None) or [])],
        "completion_fact_ids": list(
            getattr(spec, "completion_fact_ids", None) or []),
        "hints": list(getattr(spec, "hints", None) or []),
    })
    rec["stage_b"] = {
        "review_decision": m.get("review_decision"),
        "truth_audit_ok": m.get("truth_audit_ok"),
        "reject": m.get("reject"),
        "review_issues": m.get("review_issues") or [],
    }
    return rec


def _write_report(records: list) -> None:
    A = []
    P = A.append
    ok = [r for r in records if r["ok"]]
    P("# R4 production smoke —— Story -> Surface -> Structure")
    P("")
    P("真实生产链(`keyword_spec`)跑 5 红 + 5 黑。")
    P("**不重抽、不评分、不排名** —— 直接读原文。")
    P("")
    P(f"- 共 `{len(records)}` 道, 成题 `{len(ok)}`")
    P("")
    P("| # | lane | keywords | puzzle 长度 | 结果 |")
    P("|---|---|---|---|---|")
    for i, r in enumerate(records, 1):
        res = "ok" if r["ok"] else f"未成题({r.get('reason')})"
        P(f"| {i} | {r.get('lane') or '-'} | "
          f"{'、'.join(r['keywords'])} | {r['puzzle_len']} | {res} |")
    P("")
    P("---")
    P("")
    for i, r in enumerate(records, 1):
        P(f"## {i}. {r.get('lane') or '(无 lane)'} — "
          f"关键词: {'、'.join(r['keywords'])}")
        P("")
        if not r["ok"]:
            P(f"**未成题**: `{r.get('reason')}`")
            if r.get("stage_error"):
                P("")
                P(f"- Stage B 侧信道: `{r['stage_error']}`")
            P("")
            continue
        P("### answer(完整汤底)")
        P("")
        P("> " + (r["answer"] or "").replace("\n", "\n> "))
        P("")
        P("### puzzle(极短汤面)")
        P("")
        P("> " + (r["puzzle"] or "").replace("\n", "\n> "))
        P("")
        P(f"_(**{r['puzzle_len']} 字**)_")
        P("")
        P("### Stage B")
        P("")
        sb = r.get("stage_b") or {}
        P(f"- review_decision: `{sb.get('review_decision')}`")
        P(f"- truth_audit_ok: `{sb.get('truth_audit_ok')}`")
        if sb.get("reject"):
            P(f"- reject: `{sb['reject']}`")
        for it in (sb.get("review_issues") or []):
            P(f"- issue: {it}")
        P("")
        P("### core_answer")
        P("")
        P(r.get("core_answer") or "_(空)_")
        P("")
        P("### facts")
        P("")
        P("| id | kind | visibility | text |")
        P("|---|---|---|---|")
        for f in (r.get("facts") or []):
            P(f"| `{f['id']}` | {f['kind']} | {f['visibility']} | {f['text']} |")
        P("")
        P("### completion_fact_ids")
        P("")
        P("`" + "`, `".join(r.get("completion_fact_ids") or []) + "`")
        P("")
        P("### hints")
        P("")
        for j, h in enumerate(r.get("hints") or [], 1):
            P(f"{j}. {h}")
        if not r.get("hints"):
            P("_(无)_")
        P("")
        P("---")
        P("")
    with open(REPORT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(A))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--report-only", action="store_true")
    args = ap.parse_args()
    _setup_log()

    if args.report_only:
        with open(RAW_JSONL, encoding="utf-8") as f:
            records = [json.loads(x) for x in f if x.strip()]
        _write_report(records)
        log.info("报告已重出: %s", REPORT_MD)
        return 0
    if not args.run:
        log.error("要么 --run, 要么 --report-only")
        return 2

    from story.config import Config, LLMConfig
    from story.llm import AnthropicMessagesClient, PuzzleWriter
    from story.keyword_seed import derive_session_seed, load_bag
    from story.keyword_corpus import DEFAULT_CORPUS_PATH
    import random as _random

    cfg = Config()
    client = AnthropicMessagesClient(LLMConfig())
    writer = PuzzleWriter(client=client, runtime_cfg=cfg)
    log.info("model=%s", client.cfg.model)

    records = []
    for kind, base in (("red", SEED_RED), ("black", SEED_BLACK)):
        ss = derive_session_seed(base)
        bag, meta = load_bag(str(DEFAULT_CORPUS_PATH), ss)
        # lane rng: 与抽词 rng **隔离**(见 `derive_lane_seed`)。
        from story.keyword_seed import derive_lane_seed
        lane_rng = _random.Random(derive_lane_seed(ss))
        log.info("=== %s: session_seed=%s corpus=%s ===",
                 kind, ss, meta.get("corpus_version"))
        for i in range(1, N_PER_TYPE + 1):
            rec = _one(writer, bag, ss, meta, lane_rng)
            rec["type"] = kind
            records.append(rec)
            if rec["ok"]:
                log.info("[%s%d] lane=%s kw=%s puzzle=%d字",
                         kind, i, rec["lane"], rec["keywords"],
                         rec["puzzle_len"])
            else:
                log.warning("[%s%d] 未成题: %s", kind, i, rec["reason"])
            with open(RAW_JSONL, "w", encoding="utf-8") as f:
                for r in records:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
    _write_report(records)
    log.info("完成: %d/%d 成题。报告 -> %s",
             len([r for r in records if r["ok"]]), len(records), REPORT_MD)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
