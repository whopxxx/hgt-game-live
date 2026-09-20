#!/usr/bin/env python
# coding: utf-8
"""离线构建 keyword2 的 seed **词库**(§一 / §七)。

    uv run tools/build_keyword_seed_corpus.py

读 `data_external/haiguitang/raw/turtle.json`(原始 neurostellar/haiguitang
下载件), **只取 `input` 字段**, 产出 `data/keyword2_vocabulary.json`。

## 为什么这一步是"展开成词"而不是"抽 pair"

    关键词：A，B，C   ->  词库 += {A, B, C}      (三个词各算一个)

**不是** `[A,B]` 也不是只取前两个。所有 3729 行都拆, 1-key 行贡献 1 个词,
3-key 行贡献 3 个 —— 任务书 §一 明确要求。

原因是运行时要在**独立词库**上抽两个词重新组合(见
`story/keyword_seed.KeywordBag`)。若这里存的是 pair 表, 运行时抽到的两个
词就永远"一起出现过", 于是继承了外部题库的搭配先验。

## 为什么要有这一步(而不是运行时直接读原始件)

  * **原始件不在版本库里** —— `data_external/` 是 .gitignore 的(5MB, 且是
    外部数据)。生产环境只有 `data/` 下的产物。
  * **构建可以联网, 运行不可以** —— 任务书 §一: "不得为了构建 corpus 再
    访问网络。" 本脚本**不联网**, 只读已下载的本地文件。
  * **清洗规则变了要能重放** —— 产物进版本库, 于是"这份词表是哪条规则
    跑出来的"由 `corpus_version` 回答。

## 用法

    uv run tools/build_keyword_seed_corpus.py            # 用默认路径
    uv run tools/build_keyword_seed_corpus.py --check    # 只校验产物是否最新
    uv run tools/build_keyword_seed_corpus.py --report   # 附 §七 的组合空间报告
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
    DEFAULT_CORPUS_PATH, DEFAULT_RAW_PATH, build_vocabulary, split_input,
)
from story.keyword_seed import combos, derive_session_seed, load_bag  # noqa: E402


def _read_raw(path: str) -> list:
    with io.open(path, "r", encoding="utf-8") as f:
        rows = json.load(f)
    if not isinstance(rows, list):
        raise SystemExit("原始件顶层不是数组: %r" % type(rows).__name__)
    return rows


def _dumps(d: dict) -> str:
    """产物的规范序列化 —— **确定性**(排序键固定, 缩进固定)。

    不确定的序列化会让每次构建的产物字节都不同, 于是 `--check` 永远失败,
    而这种失败最容易被当成 flake 忽略。`keywords` 已在 `build_vocabulary`
    里排序, 这里只保证 key 顺序与缩进稳定。
    """
    return json.dumps(d, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def _original_pairs(rows) -> set:
    """原始 input 里**作为同一组出现过**的 unordered 2-key pair 集合。

    只用于 §七 的"多少 pair 是全新的"统计 —— **不**参与构建。
    """
    out = set()
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        toks = [t for t in split_input(row.get("input")) if t]
        for i in range(len(toks)):
            for j in range(i + 1, len(toks)):
                a, b = toks[i], toks[j]
                out.add((a, b) if a <= b else (b, a))
    return out


def _report(vocab: dict, rows, seed: int, n_show: int = 20,
            path: str = DEFAULT_CORPUS_PATH) -> None:
    """§七: 组合空间报告 + 固定 seed 下前 N 组重新组合后的 pair。"""
    kws = vocab["keywords"]
    n = len(kws)
    total = combos(n)
    print()
    print("=== 组合空间(§七) ===")
    print("raw rows            :", vocab["raw_rows"])
    print("raw tokens          :", vocab["raw_token_count"])
    print("valid tokens        :", vocab["valid_token_count"])
    print("unique keywords     :", n)
    print("理论 unordered 组合 :", total, "= %d*(%d-1)/2" % (n, n))

    ss = derive_session_seed(seed)
    bag, meta = load_bag(path, ss)
    drawn = [bag.draw() for _ in range(n_show)]
    print()
    print("=== seed=%s -> session_seed=%s, 前 %d 组**重新组合**的 pair ==="
          % (seed, ss, n_show))
    for d in drawn:
        print("  [%02d] %s" % (d["index"], "，".join(d["keywords"])))
    if bag.relaxed_total:
        print("  (其中 %d 组触发了 cooldown 放宽, 重试 %d 次)"
              % (bag.relaxed_total, bag.retries_total))
    else:
        print("  (0 组触发放宽, 共重试 %d 次)" % bag.retries_total)

    # ---- 多少 pair 是**从未在原始 input 里作为同一组出现过**的 ----
    orig = _original_pairs(rows)
    drawn_keys = {tuple(d["keywords"]) for d in drawn}
    fresh = [p for p in drawn_keys
             if ((p[0], p[1]) if p[0] <= p[1] else (p[1], p[0])) not in orig]
    print()
    print("=== 与原始 input 的关系(本轮验收核心) ===")
    print("原始 input 里出现过的 unordered pair :", len(orig))
    print("抽到的 %d 组里, **从未出现过的**       : %d / %d"
          % (len(drawn_keys), len(fresh), len(drawn_keys)))
    if drawn_keys:
        print("比例                                  : %.1f%%"
              % (100.0 * len(fresh) / len(drawn_keys)))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="构建 keyword2 seed 词库")
    ap.add_argument("--raw", default=DEFAULT_RAW_PATH,
                    help=f"原始 turtle.json 路径, 默认 {DEFAULT_RAW_PATH}")
    ap.add_argument("--out", default=DEFAULT_CORPUS_PATH,
                    help=f"产物路径, 默认 {DEFAULT_CORPUS_PATH}")
    ap.add_argument("--check", action="store_true",
                    help="只校验产物是否与重跑结果一致(不写盘)")
    ap.add_argument("--report", action="store_true",
                    help="附 §七 的组合空间报告(会建 bag 抽 20 组)")
    ap.add_argument("--seed", type=int, default=20260920,
                    help="报告用的固定 quality_seed, 默认 20260920")
    ap.add_argument("--show", type=int, default=20,
                    help="报告里展示多少组 pair, 默认 20")
    a = ap.parse_args(argv)

    if not os.path.exists(a.raw):
        print(f"原始件不存在: {a.raw}", file=sys.stderr)
        print("(它来自 tools/import_haiguitang.py 的下载; "
              "data_external/ 不进版本库, 所以只在构建机上有)", file=sys.stderr)
        return 2

    rows = _read_raw(a.raw)
    vocab = build_vocabulary(rows)
    text = _dumps(vocab)

    print("raw rows            :", vocab["raw_rows"])
    print("raw tokens          :", vocab["raw_token_count"])
    print("valid tokens        :", vocab["valid_token_count"])
    print("unique keywords     :", vocab["unique_token_count"])
    print("corpus_version      :", vocab["corpus_version"])
    print("source              :", vocab["source"])

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
        if a.report:
            _report(vocab, rows, a.seed, a.show, a.out)
        return 0

    d = os.path.dirname(os.path.abspath(a.out))
    if d:
        os.makedirs(d, exist_ok=True)
    with io.open(a.out, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print("wrote               :", a.out)

    if a.report:
        _report(vocab, rows, a.seed, a.show, a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
