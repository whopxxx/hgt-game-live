#!/usr/bin/env python
# coding: utf-8
"""category_generation_probe —— Generation v3 五类创作真实 LLM 基线
(Issue #58 §12~§16)。

## 原则: 只组织样本与输出报告, 不复制生成逻辑

本工具是**薄 harness**:

    生产 KeywordBag(load_bag, 生产同款词库/种子派生)
      +
    生产 keyword_spec()(抽词 -> Truth -> Surface -> blind Contract
    -> blind Audit -> validate -> Reviewer, 整条链与 live/prefetch
    完全同一实现)
      +
    GenerationBrief(requested_category=<当前类>, difficulty="")

**不允许**自己拼生成 prompt / 复制创作 Brief / 维护第二份关键词表 /
绕过 Audit / 用 FakeClient 冒充真实样本 —— 这些在结构上就不成立:
本文件 import 的只有生产件。

## 记录与报告

每个 attempt 记录成败与原因; accepted 样本(每类目标 3 道)记录
requested/observed、完整汤面/汤底、provenance。报告只做**事实统计**
(attempts / accepted / requested->observed 命中 / 失败分类 / usage),
**不自动评分** —— 题目好不好由人看 report.md 里的汤面/汤底判断。

运行: uv run tools/category_generation_probe.py --out data/audit/category_generation_v3
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from story.config import Config  # noqa: E402
from story.haiguitang_protocol import (  # noqa: E402
    CATEGORY_LABELS, V2_CATEGORIES,
)

#: 固定类目顺序 —— 报告与 samples.jsonl 的分组顺序必须稳定。
CATEGORY_ORDER: tuple = tuple(V2_CATEGORIES)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Generation v3 五类创作真实 LLM 基线(只组织与报告, "
                    "不复制生成逻辑)")
    ap.add_argument("--out", default="data/audit/category_generation_v3",
                    help="输出目录(run.json / samples.jsonl / report.md)")
    ap.add_argument("--accepted-per-category", type=int, default=3,
                    help="每类目标 accepted 数(默认 3)")
    ap.add_argument("--max-attempts-per-category", type=int, default=6,
                    help="每类 attempt 上限(默认 6; 不足如实报 shortfall)")
    ap.add_argument("--difficulty", default="",
                    help="GenerationBrief.difficulty(第一轮默认不指定)")
    ap.add_argument("--session-seed", type=int, default=None,
                    help="keyword session seed(缺省=SystemRandom 64bit)")
    ap.add_argument("--corpus", default="",
                    help="词库路径(缺省=生产 DEFAULT_CORPUS_PATH)")
    return ap


def run_probe(args) -> dict:
    """跑 5 类 x accepted/attempt 循环。返回 run 汇总 dict。"""
    from story.haiguitang_protocol import GenerationBrief
    from story.keyword_seed import load_bag
    from story.llm import AnthropicMessagesClient, PuzzleWriter

    cfg = Config()
    client = AnthropicMessagesClient(cfg.llm)
    # 调用计数: 包一层 messages(生产 client 原样透传, 只 +1 计数)。
    # **必须线程安全**(与 G1 工具同一约定)。
    import threading
    calls = {"n": 0}
    _lock = threading.Lock()
    _orig = client.messages

    def _counting(*a, **kw):
        with _lock:
            calls["n"] += 1
        return _orig(*a, **kw)

    client.messages = _counting  # type: ignore[assignment]
    writer = PuzzleWriter(client=client, runtime_cfg=cfg)

    # ---- 生产 bag: 与 live/prefetch 同一条 load_bag 路径 ----
    from story.keyword_corpus import DEFAULT_CORPUS_PATH
    import random as _random
    from story.keyword_seed import derive_session_seed
    ss = args.session_seed
    if ss is None:
        qseed = getattr(cfg, "quality_seed", None)
        ss = (derive_session_seed(qseed) if qseed is not None
              else _random.SystemRandom().getrandbits(64))
    corpus_path = str(args.corpus
                      or getattr(cfg, "keyword_corpus_path", "")
                      or DEFAULT_CORPUS_PATH)
    bag, bag_meta = load_bag(corpus_path, int(ss))

    brief_difficulty = str(args.difficulty or "").strip()
    # difficulty 交给 GenerationBrief 校验(空=不指定; 非法早炸)
    if brief_difficulty:
        GenerationBrief(requested_category="logic",
                        difficulty=brief_difficulty).require_valid()

    per_cat_attempts = {}
    per_cat_accepted = {}
    per_cat_fail = {}
    samples = []
    t0 = time.monotonic()

    for cat in CATEGORY_ORDER:                      # 固定类目顺序
        accepted = 0
        attempts = 0
        fails = {"gen_fail": 0, "interrupted": 0, "other": 0}
        # attempt cap: 到 cap 或拿满 accepted 即停, 不私下加轮。
        while (accepted < int(args.accepted_per_category)
               and attempts < int(args.max_attempts_per_category)):
            attempts += 1
            # 每次 attempt 从生产 KeywordBag 抽**新的** 2 个关键词
            # (keyword_spec 内部是唯一 draw 点, 这里只递 bag)。
            brief = GenerationBrief(requested_category=cat,
                                    difficulty=brief_difficulty)
            try:
                spec, why = _run_one(writer, bag, int(ss), cfg,
                                     corpus_version=bag_meta.get(
                                         "corpus_version", ""),
                                     brief=brief)
            except Exception as e:                  # noqa: BLE001
                fails["other"] += 1
                samples.append(_fail_record(cat, attempts, brief, f"exc:{e}"))
                continue
            if spec is None:
                fails.setdefault(why or "gen_fail", 0)
                fails[why or "gen_fail"] += 1
                samples.append(_fail_record(cat, attempts, brief,
                                            why or "gen_fail"))
                continue
            accepted += 1
            samples.append(_ok_record(cat, brief, spec))
        per_cat_attempts[cat] = attempts
        per_cat_accepted[cat] = accepted
        per_cat_fail[cat] = fails

    elapsed = time.monotonic() - t0
    usage = _sum_usage(samples)
    from story.keyword_seed import KEYWORD_SEED_VERSION
    return {
        "protocol_version": "haiguitang-v2",
        "prompt_version": _prompt_version(),
        "session_seed": int(ss),
        "corpus_version": bag_meta.get("corpus_version", ""),
        "keyword_seed_version": KEYWORD_SEED_VERSION,
        "difficulty": brief_difficulty,
        "accepted_per_category_target": int(args.accepted_per_category),
        "max_attempts_per_category": int(args.max_attempts_per_category),
        "attempts": per_cat_attempts,
        "accepted": per_cat_accepted,
        "fails": per_cat_fail,
        "requested_to_observed": _hit_stats(samples),
        "total_llm_calls": calls["n"],
        "usage": usage,
        "elapsed_s": round(elapsed, 1),
        "model": getattr(getattr(client, "cfg", None), "model", ""),
        "shortfall": {c: max(0, int(args.accepted_per_category)
                             - per_cat_accepted[c]) for c in CATEGORY_ORDER},
        "samples": samples,
    }


def _run_one(writer, bag, session_seed, cfg, *, corpus_version, brief):
    """**恰好一次**生产 keyword_spec —— 本工具与生产之间唯一的调用面。"""
    from story.keyword_seed import keyword_spec
    return keyword_spec(writer, bag, session_seed,
                        corpus_version=corpus_version, brief=brief)


def _prompt_version() -> str:
    from story.prompt_pack import HAIGUITANG_GENERATION_PROMPT_VERSION
    return HAIGUITANG_GENERATION_PROMPT_VERSION


def _ok_record(cat: str, brief, spec) -> dict:
    m = dict(getattr(spec, "metrics", None) or {})
    return {
        "category": cat,
        "attempt": m.get("keyword_draw_index"),
        "ok": True,
        "requested_category": brief.requested_category,
        "requested_difficulty": brief.difficulty,
        "primary_category": getattr(spec, "primary_category", ""),
        "categories": list(getattr(spec, "categories", None) or []),
        "difficulty": getattr(spec, "difficulty", ""),
        "keywords": list(m.get("keywords") or []),
        "puzzle": getattr(spec, "puzzle", ""),
        "answer": getattr(spec, "answer", ""),
        "core_answer": getattr(spec, "core_answer", ""),
        "protocol_version": getattr(spec, "protocol_version", ""),
        "prompt_version": getattr(spec, "prompt_version", ""),
        "model": getattr(spec, "model", ""),
        "usage": dict(getattr(spec, "usage", None) or {}),
        "review_decision": m.get("review_decision", ""),
        "reject": m.get("reject", ""),
        "review_issues": list(m.get("review_issues") or []),
        "keyword_draw_index": m.get("keyword_draw_index"),
        "keyword_session_seed": m.get("keyword_session_seed"),
    }


def _fail_record(cat: str, attempt_no: int, brief, why: str) -> dict:
    return {"category": cat, "attempt": attempt_no, "ok": False,
            "requested_category": brief.requested_category,
            "requested_difficulty": brief.difficulty,
            "reason": why}


def _hit_stats(samples) -> dict:
    hits = {c: 0 for c in CATEGORY_ORDER}
    totals = {c: 0 for c in CATEGORY_ORDER}
    observed_dist = {c: {} for c in CATEGORY_ORDER}
    for s in samples:
        if not s.get("ok"):
            continue
        c = s["category"]
        totals[c] += 1
        if s.get("primary_category") == c:
            hits[c] += 1
        obs = s.get("primary_category") or "(空)"
        observed_dist[c][obs] = observed_dist[c].get(obs, 0) + 1
    return {"hits": hits, "accepted_totals": totals,
            "observed_distribution": observed_dist}


def _sum_usage(samples) -> dict:
    tot: dict = {}
    for s in samples:
        if not s.get("ok"):
            continue
        for k, v in (s.get("usage") or {}).items():
            if isinstance(v, (int, float)):
                tot[k] = tot.get(k, 0) + v
    return tot


def write_outputs(run: dict, out_dir: str) -> None:
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    samples = run.pop("samples")
    (d / "run.json").write_text(
        json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    with (d / "samples.jsonl").open("w", encoding="utf-8") as f:
        for s in samples:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    (d / "report.md").write_text(
        render_report(run, samples), encoding="utf-8")


def render_report(run: dict, samples: list) -> str:
    A: list = []
    P = A.append
    P("# Generation v3 —— 五类创作真实 LLM 基线")
    P("")
    P(f"- protocol: `{run['protocol_version']}` / prompt: "
      f"`{run['prompt_version']}` / model: `{run.get('model') or '(未知)'}`")
    P(f"- session_seed: `{run['session_seed']}` / corpus: "
      f"`{run['corpus_version']}` / requested difficulty: "
      f"`{run['difficulty'] or '(不指定)'}`")
    P(f"- 总耗时: {run['elapsed_s']}s / token usage: {run['usage']}")
    P("")
    P("## 汇总(只做事实统计, 不评分)")
    P("")
    P("| 类别 | attempts | accepted | requested=observed 命中 | "
      "observed 分布 | 失败分类 |")
    P("|---|---|---|---|---|---|")
    for c in CATEGORY_ORDER:
        hit = run["requested_to_observed"]["hits"][c]
        dist = run["requested_to_observed"]["observed_distribution"].get(c, {})
        dist_s = ", ".join(f"{k}:{v}" for k, v in dist.items()) or "-"
        fail = run["fails"].get(c, {})
        fail_s = ", ".join(f"{k}:{v}" for k, v in fail.items() if v) or "-"
        P(f"| {c}({CATEGORY_LABELS.get(c, '')}) "
          f"| {run['attempts'][c]} | {run['accepted'][c]} | {hit} "
          f"| {dist_s} | {fail_s} |")
    shortfall = {c: n for c, n in run["shortfall"].items() if n > 0}
    if shortfall:
        P("")
        P(f"**shortfall(如实暴露, 不补样本)**: {shortfall}")
    P("")
    n = 0
    for c in CATEGORY_ORDER:
        P(f"## {c} / {CATEGORY_LABELS.get(c, '')}")
        P("")
        for s in samples:
            if s.get("category") != c or not s.get("ok"):
                continue
            n += 1
            P(f"### {c}-{s['attempt']:02d}")
            P("")
            P(f"请求类型：{s['requested_category']} / "
              f"{CATEGORY_LABELS.get(s['requested_category'], '')}")
            P(f"随机关键词：{' / '.join(s['keywords'])}")
            P("")
            P("【汤面】")
            P(s["puzzle"])
            P("")
            P("【汤底】")
            P(s["answer"])
            P("")
            P("【核心答案】")
            P(s["core_answer"])
            P("")
            P("【最终观察】")
            P(f"primary_category: {s['primary_category']}")
            P(f"categories: {s['categories']}")
            P(f"difficulty: {s['difficulty']}")
            P("")
            P("【生产结果】")
            P(f"protocol_version: {s['protocol_version']}")
            P(f"prompt_version: {s['prompt_version']}")
            P(f"review_decision: {s['review_decision']}")
            P(f"model: {s['model']}")
            P(f"usage: {s['usage']}")
            P("")
        if not run["requested_to_observed"]["accepted_totals"].get(c):
            P("(该类无 accepted 样本)")
            P("")
    P("---")
    P("")
    P(f"共 {n} 道 accepted。requested != observed 是**合法状态**, "
      "报告原样呈现, 不改分类、不挑题。")
    return "\n".join(A)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    run = run_probe(args)
    write_outputs(run, args.out)
    accepted_total = sum(run["accepted"].values())
    print(f"done: accepted {accepted_total}/"
          f"{len(CATEGORY_ORDER) * run['accepted_per_category_target']}, "
          f"out -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
