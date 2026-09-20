#!/usr/bin/env python
# coding: utf-8
"""离线构建 keyword2 的 seed corpus(§二)。

    uv run tools/build_keyword_seed_corpus.py

读 `data_external/haiguitang/raw/turtle.json`(原始 neurostellar/haiguitang
下载件), **只取 `input` 字段**, 产出 `data/keyword2_seed_pairs.json`。

## 为什么要有这一步(而不是运行时直接读原始件)

  * **原始件不在版本库里** —— `data_external/` 是 .gitignore 的(5MB, 且是
    外部数据)。生产环境只有 `data/` 下的产物。运行时直接读原始件会让
    "corpus 缺失"在生产上变成常态, 而那正是 §五 要显式处理的分支。
  * **构建可以联网, 运行不可以** —— 任务书 §一: "不得为了构建 corpus 再
    访问网络。" 本脚本**不联网**, 只读已下载的本地文件。加 `--fetch` 是
    将来换数据版本的事, 本轮不做。
  * **清洗规则变了要能重放** —— 产物进版本库, 于是"这份 pair 表是哪条
    规则跑出来的"由 `corpus_version` 回答。

## 用法

    uv run tools/build_keyword_seed_corpus.py            # 用默认路径
    uv run tools/build_keyword_seed_corpus.py --check    # 只校验产物是否最新
    uv run tools/build_keyword_seed_corpus.py --raw X --out Y

`--check` 是给 CI / 复盘用的: 重跑一遍构建, 与盘上产物比对; 不一致就
非 0 退出。它证明产物**没有过期**(改了清洗规则却忘了重新构建)。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from story.keyword_corpus import (  # noqa: E402
    DEFAULT_CORPUS_PATH, DEFAULT_RAW_PATH, build_corpus,
)


def _read_raw(path: str) -> list:
    with io.open(path, "r", encoding="utf-8") as f:
        rows = json.load(f)
    if not isinstance(rows, list):
        raise SystemExit("原始件顶层不是数组: %r" % type(rows).__name__)
    return rows


def _dumps(d: dict) -> str:
    """产物的规范序列化 —— **确定性**(排序键固定, 缩进固定)。

    不确定的序列化会让每次构建的产物字节都不同, 于是 `--check` 永远失败,
    而这种失败最容易被当成 flake 忽略。`pairs` 已在 `pairs_from_rows` 里
    排序, 这里只保证 key 顺序与缩进稳定。
    """
    return json.dumps(d, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="构建 keyword2 seed corpus")
    ap.add_argument("--raw", default=DEFAULT_RAW_PATH,
                    help=f"原始 turtle.json 路径, 默认 {DEFAULT_RAW_PATH}")
    ap.add_argument("--out", default=DEFAULT_CORPUS_PATH,
                    help=f"产物路径, 默认 {DEFAULT_CORPUS_PATH}")
    ap.add_argument("--check", action="store_true",
                    help="只校验产物是否与重跑结果一致(不写盘)")
    a = ap.parse_args(argv)

    if not os.path.exists(a.raw):
        print(f"原始件不存在: {a.raw}", file=sys.stderr)
        print("(它来自 tools/import_haiguitang.py 的下载; "
              "data_external/ 不进版本库, 所以只在构建机上有)", file=sys.stderr)
        return 2

    rows = _read_raw(a.raw)
    corpus = build_corpus(rows)
    text = _dumps(corpus)

    print("raw rows        :", corpus["raw_rows"])
    print("2-key rows      :", corpus["two_key_rows"])
    print("unique pairs    :", corpus["unique_pairs"])
    print("corpus_version  :", corpus["corpus_version"])
    print("source          :", corpus["source"])

    if a.check:
        if not os.path.exists(a.out):
            print(f"[check] 产物不存在: {a.out}", file=sys.stderr)
            return 1
        with io.open(a.out, "r", encoding="utf-8") as f:
            on_disk = f.read()
        if on_disk != text:
            print(f"[check] 产物**已过期** —— 重跑结果与盘上的 {a.out} 不一致",
                  file=sys.stderr)
            print("        重新构建: uv run tools/build_keyword_seed_corpus.py",
                  file=sys.stderr)
            return 1
        print(f"[check] OK —— {a.out} 与重跑结果一致")
        return 0

    d = os.path.dirname(os.path.abspath(a.out))
    if d:
        os.makedirs(d, exist_ok=True)
    with io.open(a.out, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print("wrote           :", a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
