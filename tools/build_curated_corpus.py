#!/usr/bin/env python
# coding: utf-8
"""H1-E / H1-D 收口: 把两个来源合成一份**去重后**的 curated_raw。

## 它在管道里的位置

    import_turtlebench.py   -> data_external/turtlebench/normalized/*.jsonl
    import_puzzling_se.py   -> data_external/puzzling_se/normalized/*.jsonl
                                        |
                            build_curated_corpus.py   <- **本脚本**
                                        |
                    data_external/normalized/curated_raw.jsonl
                    data_external/normalized/duplicate_candidates.jsonl
                    data_external/normalized/curated.meta.json

## 为什么要有这一层(而不是让 H2 直接读两个文件)

三个理由, 每个都会造成真实错误:

  1. **跨来源判重**: TurtleBench 与 SE 里会有同一道经典题的不同版本。
     不合并判重 -> 同一个谜底连着播两次, 观众立刻发现。

  2. **排序必须是全局的**: H1-D 要求 byte-for-byte 可复现。两个来源
     各自的文件是各自排序的, 拼起来**不是**全局有序 —— 而拼接顺序
     又取决于调用方。所以必须在这里统一排一次。

  3. **H2 的输入应当只有一份**: 让 H2 自己去读两个文件、各自去重、
     再合并, 等于把本脚本的逻辑散到编译链里, 而那部分逻辑(判重键、
     相似度阈值)一旦分叉就再也对不上。

## 为什么 duplicate_candidates **不删**

任务书 H1-E: "不要自动删除两个不同来源的版本。"

判重会错: 两个版本可能真的是同一道题(该删), 也可能是两道不同的题
(3-gram 相似但谜底不同, 删了就丢题)。这两种从文本上分不开。
自动删的代价是**悄悄丢题**, 留成候选的代价是多几条待确认记录。
所以只标记, 由后续(H2-B 的 AI 审题门 / 人工)决定。

## 用法

    .venv/Scripts/python.exe tools/build_curated_corpus.py
    .venv/Scripts/python.exe tools/build_curated_corpus.py --no-dedup
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.curated_common import (  # noqa: E402
    EXTERNAL_ROOT, RawCuratedPuzzle, ensure_dir, read_jsonl,
    write_jsonl_deterministic, write_meta,
)
from tools.curated_dedup import (  # noqa: E402
    NEAR_DUP_THRESHOLD, cross_source_report, dedup,
)

log = logging.getLogger("hgt.corpus")

#: 两个来源的子路径(相对 root)。顺序**无关**(最终会全局排序), 列在
#: 这里只是为了让"输入集合"显式可见 —— 加新来源时改这一处。
#:
#: ⚠️ 存**相对**路径而不是绝对: 早先这里是 `os.path.join(EXTERNAL_ROOT,
#: ...)` 算好的绝对路径, 于是 `--root` 参数**完全不起作用** —— 传了
#: 也还是读真实目录。测试因此根本没法用临时目录隔离(会读到生产语料)。
#: 相对 root 之后, `--root` 才真的是"去哪找输入、往哪写输出"。
SOURCES = (
    # H4-A: 主源排在前面(见 `lazy_curator.source_priority`)。
    # 顺序在这里**无关**(最终全局排序), 但列在前面让人一眼看到
    # "现在的主源是哪个"。
    ("haiguitang", os.path.join("haiguitang", "normalized",
                                "haiguitang.jsonl")),
    ("turtlebench", os.path.join("turtlebench", "normalized",
                                 "turtlebench.jsonl")),
    ("puzzling_se", os.path.join("puzzling_se", "normalized",
                                 "puzzling_se.jsonl")),
)


def source_paths(root: str) -> list:
    """把相对子路径接到 root 上。"""
    return [(name, os.path.join(root, rel)) for name, rel in SOURCES]


def load_all(root: str) -> tuple:
    """读所有来源。返回 `(records, per_source_counts, missing)`。"""
    recs: list = []
    counts: dict = {}
    missing: list = []
    for name, path in source_paths(root):
        if not os.path.exists(path):
            missing.append((name, path))
            log.warning("来源文件不存在(跳过): %s", path)
            continue
        rows = read_jsonl(path)
        counts[name] = len(rows)
        for d in rows:
            recs.append(RawCuratedPuzzle.from_dict(d))
        log.info("读入 %s: %d 条", name, len(rows))
    return recs, counts, missing


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="build_curated_corpus",
        description="合并两个来源 + 跨来源去重 -> curated_raw.jsonl")
    ap.add_argument("--root", default=EXTERNAL_ROOT)
    ap.add_argument("--no-dedup", action="store_true",
                    help="只合并不去重(排查用)")
    ap.add_argument("--near-threshold", type=float,
                    default=NEAR_DUP_THRESHOLD,
                    help=f"近重复阈值, 默认 {NEAR_DUP_THRESHOLD}")
    ap.add_argument("--log-level", default="INFO")
    return ap


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(a.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    out_dir = os.path.join(a.root, "normalized")
    kept_p = os.path.join(out_dir, "curated_raw.jsonl")
    dup_p = os.path.join(out_dir, "duplicate_candidates.jsonl")
    meta_p = os.path.join(out_dir, "curated.meta.json")

    recs, counts, missing = load_all(a.root)
    if not recs:
        log.error("一个来源都没读到 —— 先跑 import_turtlebench.py / "
                  "import_puzzling_se.py。**不写空文件。**")
        return 1

    before = len(recs)
    if a.no_dedup:
        kept, dupes, stats = recs, [], {"input": before, "exact_removed": 0,
                                        "near_flagged": 0, "kept": before}
    else:
        kept, dupes, stats = dedup(recs, near_threshold=a.near_threshold)

    # ---- 许可门: 认不出的许可**不允许**进 curated ----
    # 这里只拦"许可不可用"的。它们与"重复"是两回事, 所以进一个单独
    # 的桶, 不混进 duplicate_candidates。
    licensed, unlicensed = [], []
    for r in kept:
        (licensed if r.license_ok() else unlicensed).append(r)
    if unlicensed:
        log.warning("%d 条因许可不可用被拦下(见 license_rejected.jsonl)",
                    len(unlicensed))
        write_jsonl_deterministic(
            os.path.join(out_dir, "license_rejected.jsonl"), unlicensed)

    write_jsonl_deterministic(kept_p, licensed)
    write_jsonl_deterministic(dup_p, dupes)
    write_meta(meta_p, sources={k: v for k, v in counts.items()},
               missing=[m[0] for m in missing],
               near_threshold=a.near_threshold,
               dedup_enabled=not a.no_dedup, **stats)

    # ---- 报告(数字要能直接进任务书的验收) ----
    print()
    print("=" * 62)
    print("Curated 语料合并 + 跨来源去重")
    print("=" * 62)
    for name, n in counts.items():
        print(f"  读入 {name:16s}: {n}")
    if missing:
        print(f"  **缺失来源**: {', '.join(m[0] for m in missing)}")
    print(f"  合并总计            : {before}")
    print(f"  exact 合并掉        : {stats['exact_removed']}")
    print(f"  近重复(标记不删)   : {stats['near_flagged']}")
    print(f"  许可不可用被拦      : {len(unlicensed)}")
    print(f"  **入 curated_raw**  : {len(licensed)}")
    print()
    print("  来源分布(去重后):")
    for k, v in cross_source_report(licensed).items():
        print(f"    {k:26s} {v}")
    print()
    print(f"  -> {kept_p}")
    if dupes:
        print(f"  {len(dupes)} 条重复候选(未删) -> {dup_p}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
