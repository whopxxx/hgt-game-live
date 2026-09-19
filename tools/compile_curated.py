#!/usr/bin/env python
# coding: utf-8
"""H2 收口: 把 curated 语料**编译**成可进池的 `PuzzleSpec`。

## 管道位置

    data_external/normalized/curated_raw.jsonl     (H1-D/E 产出)
                    |
          compile_curated.py                       <- **本脚本**
                    |
            AI 审题门(9 条) + translate + compile
                    |
            validate -> Reviewer -> truth audit -> cross gate
                    |
    data/curated_pool.jsonl          (可进直播的 curated 题)
    data/ATTRIBUTIONS.jsonl          (H2-H 版权溯源)
    data_external/compile_report.json(验收数字)
    data_external/compile_rejected.jsonl

## 为什么与 `prefill_pool.py` 是两个脚本

`prefill_pool` 走的是**发明**链(choose_blueprint -> gen_spec -> emit_riddle);
本脚本走的是**搬运**链(读已有题 -> curated 编译)。两者的第一步完全不同,
但后四道质量门是同一套(复用 `PuzzleWriter` 的审稿/审计)。混成一个脚本会
让"这批题到底是造的还是在搬的"变成一个参数, 而那正是最该显式区分的东西。

## 断点续跑

编译一道要花一次 LLM 调用(还可能要重试), 几百道就是几百次。中途断掉
(网关抖动 / 手动 Ctrl-C)后必须能接着跑, 而不是从头再来 —— 那不仅费钱,
还会因为已编译的题**不在** curated_pool 里而重复编译。

所以: 每编译成功一道就**追加**写盘 + 记进 `_done` 集合; 重跑时先读
已有的 `curated_pool.jsonl`, 把那批 external_id 跳过。

## 用法

    .venv/Scripts/python.exe tools/compile_curated.py --limit 20
    .venv/Scripts/python.exe tools/compile_curated.py --report-only
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from story.config import Config, from_args as _cfg_from_args  # noqa: E402
from story.pool import spec_key  # noqa: E402
from story.quality import Quotas, choose_blueprint  # noqa: E402
from tools.curated_common import (  # noqa: E402
    EXTERNAL_ROOT, RawCuratedPuzzle, ensure_dir, read_jsonl, resolve_proxy,
    write_jsonl_deterministic, write_meta,
)
from tools.curated_compiler import CuratedCompiler  # noqa: E402

log = logging.getLogger("hgt.compile")

CORPUS = os.path.join(EXTERNAL_ROOT, "normalized", "curated_raw.jsonl")
#: curated 题的池子文件。**与 `pool.jsonl` 分开** —— 任务书 H2-F:
#: "不要直接和普通 pool.jsonl 混成不可区分"。分开之后:
#:   1. 想"只播 curated"只要换一个池路径, 不必筛;
#:   2. 可以单独清空/重建 curated 而不动 AI 生成的存量;
#:   3. 出问题时一眼看得出是哪批。
CURATED_POOL = os.path.join("data", "curated_pool.jsonl")
ATTRIBUTIONS = os.path.join("data", "ATTRIBUTIONS.jsonl")


def _append_jsonl(path: str, rec: dict) -> bool:
    """追加一行(不留半截文件: 写完 flush + fsync)。

    用**追加**而不是重写整个文件: 编译是长跑, 中途被杀时重写会把已经
    编译好的几百道一起丢掉。追加的最坏情况只丢最后一行。
    """
    try:
        ensure_dir(path)
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return True
    except OSError as e:
        log.error("写盘失败 %s: %s", path, e)
        return False


def load_done(pool_path: str) -> set:
    """已经编译过的 external_id(断点续跑用)。"""
    done: set = set()
    for rec in read_jsonl(pool_path):
        d = rec.get("spec") if isinstance(rec, dict) else None
        if isinstance(d, dict) and d.get("external_id"):
            done.add(str(d["external_id"]))
    return done


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="compile_curated",
        description="把 curated_raw 编译成可进池的 PuzzleSpec(复用现有质量链)")
    ap.add_argument("--corpus", default=CORPUS)
    ap.add_argument("--pool-out", default=CURATED_POOL)
    ap.add_argument("--attributions", default=ATTRIBUTIONS)
    ap.add_argument("--limit", type=int, default=0,
                    help="本次最多编译几道(0 = 不设上限)")
    ap.add_argument("--max-attempts", type=int, default=2,
                    help="单道最多编译几稿(默认 2)")
    ap.add_argument("--seed", type=int, default=None,
                    help="blueprint 调度种子(给了就复现)")
    ap.add_argument("--report-only", action="store_true",
                    help="只打印语料统计, 不调 LLM")
    ap.add_argument("--no-llm", action="store_true",
                    help="不使用 LLM(只能干跑, 编译一定失败)")
    ap.add_argument("--log-level", default="INFO")
    ap.add_argument("--log-file", default="")
    return ap


def _make_writer(cfg: Config):
    """与 prefill / prefetch **同构**的 writer。"""
    from story.llm import AnthropicMessagesClient, PuzzleWriter
    return PuzzleWriter(client=AnthropicMessagesClient(cfg.llm),
                        runtime_cfg=cfg)


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(a.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    rows = read_jsonl(a.corpus)
    recs = [RawCuratedPuzzle.from_dict(d) for d in rows]
    if not recs:
        log.error("语料为空或不存在: %s —— 先跑 build_curated_corpus.py。"
                  "**不产出空池。**", a.corpus)
        return 1

    done = load_done(a.pool_out)
    todo = [r for r in recs if r.external_id not in done]
    log.info("语料 %d 条, 已编译 %d, 本次待编译 %d",
             len(recs), len(done), len(todo))

    if a.report_only:
        _print_corpus_report(recs, done)
        return 0

    cfg: Config = _cfg_from_args(["--sim", "compile", "--no-window",
                                  "--max-puzzles", "0"])
    if a.no_llm or not getattr(cfg, "llm", None):
        log.error("编译需要一个可用的 LLM 配置 —— --no-llm 下无法编译。")
        return 1

    writer = _make_writer(cfg)
    comp = CuratedCompiler(writer)
    rng = random.Random(a.seed)
    quotas = Quotas.from_config(cfg)

    ok_n = 0
    rejects: dict = {}
    per_stage: dict = {}
    t0 = time.monotonic()
    for i, rec in enumerate(todo, 1):
        if a.limit and ok_n >= a.limit:
            log.info("已达 --limit %d, 收工。", a.limit)
            break
        # blueprint: 与生成链一样由**代码**选(导演权在代码), 但这里
        # 它是"建议" —— 外部题的形状是既定的, 不该为了凑 blueprint
        # 去改故事(那是 H2 明令禁止的)。所以只作为方向提示注入,
        # 不做 validate_blueprint 硬比对。
        bp = choose_blueprint([], rng=rng, quotas=quotas)
        log.info("[%d/%d] 编译 %s (%s)", i, len(todo), rec.external_id,
                 rec.source)
        try:
            spec, info = comp.compile_one(rec, recent=[], blueprint=bp,
                                          max_attempts=a.max_attempts)
        except Exception as e:                  # noqa: BLE001
            log.exception("编译异常(继续下一道): %s", e)
            info = {"accepted": False, "stage": "exception",
                    "reject_reasons": [str(e)[:120]]}
            spec = None
        stage = str(info.get("stage") or ("ok" if spec else "unknown"))
        per_stage[stage] = per_stage.get(stage, 0) + 1
        if spec is None:
            for r in (info.get("reject_reasons") or ["unknown"]):
                # 归成短标签, 便于报告分类(与 fix_reasons 同一思路)。
                slug = str(r).split(":")[0].strip()[:40] or "unknown"
                rejects[slug] = rejects.get(slug, 0) + 1
            _append_jsonl(
                os.path.join(os.path.dirname(a.corpus) or ".",
                             "compile_rejected.jsonl"),
                {"external_id": rec.external_id,
                 "source": rec.source,
                 "stage": stage,
                 "reject_reasons": info.get("reject_reasons") or []})
            continue

        # ---- 入池(走与 live 同一扇准入门的**前置**检查) ----
        rec_out = {
            "pool_version": 1,
            "pool_key": spec_key(spec),
            "added_at": time.time(),
            "added_by": "curated",
            "spec": spec.to_archive(),
        }
        if not _append_jsonl(a.pool_out, rec_out):
            log.error("curated 池写盘失败, 停在这里(已编译的不会丢)")
            break
        # ---- H2-H: attribution 单独一份 ----
        _append_jsonl(a.attributions, {
            "external_id": spec.external_id,
            "source": spec.external_source,
            "source_url": spec.source_url,
            "license": spec.license,
            "answer_license": spec.answer_license,
            **(spec.attribution or {}),
        })
        ok_n += 1
        log.info("入 curated 池: %s(累计 %d)", spec.external_id, ok_n)

    elapsed = time.monotonic() - t0
    report = {
        "corpus": len(recs),
        "already_done": len(done),
        "attempted": min(len(todo), ok_n + sum(
            v for k, v in per_stage.items() if k != "ok")),
        "compiled_ok": ok_n,
        "by_stage": per_stage,
        "reject_reasons": dict(sorted(rejects.items(),
                                      key=lambda kv: -kv[1])),
        "elapsed_s": round(elapsed, 1),
        "pool_out": a.pool_out,
    }
    write_meta(os.path.join(os.path.dirname(a.corpus) or ".",
                            "compile_report.json"), **report)
    _print_report(report, recs, done, ok_n)
    return 0


def _print_corpus_report(recs: list, done: set) -> None:
    print()
    print("=" * 62)
    print("Curated 语料统计(未编译)")
    print("=" * 62)
    print(f"  语料总数      : {len(recs)}")
    print(f"  已编译        : {len(done)}")
    print(f"  待编译        : {len(recs) - len(done)}")
    src: dict = {}
    lang: dict = {}
    for r in recs:
        src[r.source] = src.get(r.source, 0) + 1
        lang[r.original_language or "?"] = lang.get(
            r.original_language or "?", 0) + 1
    print("  来源分布:")
    for k, v in sorted(src.items(), key=lambda kv: -kv[1]):
        print(f"    {k:26s} {v}")
    print("  原语言分布:")
    for k, v in sorted(lang.items(), key=lambda kv: -kv[1]):
        print(f"    {k:6s} {v}")
    print()


def _print_report(report: dict, recs: list, done: set, ok_n: int) -> None:
    print()
    print("=" * 62)
    print("Curated 编译报告")
    print("=" * 62)
    print(f"  语料总数            : {report['corpus']}")
    print(f"  本次新编译成功      : {ok_n}")
    print(f"  累计已编译          : {len(done) + ok_n}")
    print(f"  耗时                : {report['elapsed_s']}s")
    print()
    print("  各阶段分布:")
    for k, v in sorted(report["by_stage"].items(), key=lambda kv: -kv[1]):
        print(f"    {k:24s} {v}")
    if report["reject_reasons"]:
        print()
        print("  拒收原因 top:")
        for k, v in list(report["reject_reasons"].items())[:12]:
            print(f"    {k:44s} {v}")
    print()
    print(f"  curated 池 -> {report['pool_out']}")
    print()


if __name__ == "__main__":
    sys.exit(main())
