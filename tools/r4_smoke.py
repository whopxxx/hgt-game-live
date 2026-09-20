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


class ObservingPuzzleWriter:
    """**只读**包装器: 抄一份 Story / Surface 的产物, 不改任何行为。

    ## 为什么需要它

    `keyword_spec()` 一旦在 Stage B 被拒(truth_reject / review_rewrite /
    validation_reject), 那一段拿到的 Story answer 与 Surface puzzle 就
    **一起被丢掉** —— 报告里只剩一个标签。于是**没法判断**失败到底是:

        * 汤底本身该拒,
        * 短汤面很好但 Stage B 误杀,
        * 还是 Surface 自己已经泄底。

    而那正是复审最需要看的信息。所以这里 override 两个方法: 先调
    `super()` 拿**原样的**返回值, 再抄一份到 `self.last_story` /
    `self.last_surface`。

    ## 为什么用包装器而不是改 `keyword_spec`

    改 `keyword_spec` 就是**为了观测而污染生产** —— 那是本末倒置。
    包装器只在 smoke 里用, 生产链一行不动。

    ## 为什么不用子类

    子类要 import 真实的 `PuzzleWriter` 并继承它, 于是"包装器有没有
    悄悄改行为"这件事变得难以一眼看清(继承会带进父类的一切)。
    `__getattr__` 转发是**结构性**的: 没显式写出来的属性一律转发, 所以
    "只改了这两个方法"是读得出来的。
    """

    def __init__(self, inner):
        self._inner = inner
        #: 最近一次 Story / Surface 的**原样**返回(失败时保留上一次)。
        self.last_story = None
        self.last_surface = None

    def gen_keyword_story(self, keywords, lane, **kw):
        out = self._inner.gen_keyword_story(keywords, lane, **kw)
        if isinstance(out, dict) and out.get("answer"):
            self.last_story = dict(out)
        return out

    def gen_surface(self, answer, **kw):
        out = self._inner.gen_surface(answer, **kw)
        if isinstance(out, dict) and out.get("puzzle"):
            self.last_surface = dict(out)
        return out

    def __getattr__(self, name):
        return getattr(self._inner, name)


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


def _one(writer, bag, session_seed: int, corpus_meta: dict) -> dict:
    """跑**一次生产链**。失败照实记, 但**保留 Story / Surface 原文**。"""
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
    # 清掉上一题的快照 —— 否则这一题失败时会**误报上一题**的 Story/Surface。
    writer.last_story = None
    writer.last_surface = None
    t0 = time.monotonic()
    spec, why = keyword_spec(writer, bag, session_seed,
                             corpus_version=corpus_meta.get("corpus_version", ""))
    rec["latency_ms"] = int((time.monotonic() - t0) * 1000)
    # ---- ⚠️ 无论成没成, 先把观测量抄出来 ----
    #
    # 这是这次改动的**要点**: Stage B 拒了之后, Story answer 与 Surface
    # puzzle 仍然在报告里, 复审才能判断"该拒"还是"误杀"。
    story = writer.last_story or {}
    surface = writer.last_surface or {}
    rec["story_answer"] = str(story.get("answer") or "")
    rec["surface_puzzle"] = str(surface.get("puzzle") or "")
    rec["surface_puzzle_len"] = len(rec["surface_puzzle"])
    if spec is None:
        rec["reason"] = why or "gen_fail"
        rec["stage_reject"] = getattr(writer, "_last_reject", "") or ""
        return rec
    m = dict(getattr(spec, "metrics", None) or {})
    rec.update({
        "ok": bool(getattr(spec, "puzzle", "")),
        "lane": str(m.get("lane") or ""),
        "answer": getattr(spec, "answer", "") or rec["story_answer"],
        "puzzle": getattr(spec, "puzzle", "") or rec["surface_puzzle"],
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
    rec["puzzle_len"] = len(rec["puzzle"] or "")
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
        # ---- ⚠️ 先把 Story / Surface 原文列出来(**失败也要**) ----
        #
        # 这是这一版报告的关键: 只写一个 reject 标签, 复审没法判断
        # "该拒"还是"误杀"。
        P("### Story answer(汤底原文)")
        P("")
        if r.get("story_answer"):
            P("> " + r["story_answer"].replace("\n", "\n> "))
        else:
            P("_(Story 未成题)_")
        P("")
        P("### Surface puzzle(短汤面原文)")
        P("")
        if r.get("surface_puzzle"):
            P("> " + r["surface_puzzle"].replace("\n", "\n> "))
            P("")
            P(f"_(**{r.get('surface_puzzle_len', 0)} 字**)_")
        else:
            P("_(Surface 未成题)_")
        P("")
        sb = r.get("stage_b") or {}
        P("### Stage B")
        P("")
        if not r["ok"]:
            P(f"- **未通过**: reason=`{r.get('reason')}` "
              f"reject=`{r.get('stage_reject') or sb.get('reject') or '(none)'}`")
            if sb.get("review_decision"):
                P(f"- review_decision: `{sb['review_decision']}`")
            for it in (sb.get("review_issues") or []):
                P(f"- issue: {it}")
            P("")
            P("---")
            P("")
            continue
        P(f"- **通过** (prompt_version=`{r.get('prompt_version')}`)")
        P(f"- review_decision: `{sb.get('review_decision')}`")
        P(f"- truth_audit_ok: `{sb.get('truth_audit_ok')}`")
        P("")
        P(f"_(最终汤面 **{r['puzzle_len']} 字**)_")
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

    cfg = Config()
    client = AnthropicMessagesClient(LLMConfig())
    # ⚠️ **只读**包装器 —— Story / Surface 的产物照抄一份, 供 Stage B
    # 拒绝后仍能写进报告。行为一个字节不改(见类说明)。
    writer = ObservingPuzzleWriter(PuzzleWriter(client=client, runtime_cfg=cfg))
    log.info("model=%s", client.cfg.model)

    records = []
    for kind, base in (("red", SEED_RED), ("black", SEED_BLACK)):
        ss = derive_session_seed(base)
        bag, meta = load_bag(str(DEFAULT_CORPUS_PATH), ss)
        log.info("=== %s: session_seed=%s corpus=%s ===",
                 kind, ss, meta.get("corpus_version"))
        for i in range(1, N_PER_TYPE + 1):
            # ⚠️ **不传 lane_rng** —— lane 现在由 keyword_spec 按
            # (session_seed, draw_index) 无状态派生, 与 live/prefetch 的
            # 调用形状**完全一致**。上一版 smoke 自己手工传了一把持久
            # RNG, 反而把"生产上永远同一个 lane"那个 bug 遮住了。
            rec = _one(writer, bag, ss, meta)
            rec["type"] = kind
            records.append(rec)
            if rec["ok"]:
                log.info("[%s%d] lane=%s kw=%s puzzle=%d字",
                         kind, i, rec["lane"], rec["keywords"],
                         rec["puzzle_len"])
            else:
                log.warning("[%s%d] 未成题: %s | stage=%s | story=%d字 "
                            "surface=%d字",
                            kind, i, rec["reason"],
                            rec.get("stage_reject") or "-",
                            len(rec.get("story_answer") or ""),
                            rec.get("surface_puzzle_len") or 0)
            with open(RAW_JSONL, "w", encoding="utf-8") as f:
                for r in records:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
    _write_report(records)
    log.info("完成: %d/%d 成题。报告 -> %s",
             len([r for r in records if r["ok"]]), len(records), REPORT_MD)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
