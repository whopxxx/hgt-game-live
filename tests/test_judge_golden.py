#!/usr/bin/env python
# coding: utf-8
"""离线 golden 回放: 裁判准确率回归。

    uv run tests/test_judge_golden.py            # 跑全部
    uv run tests/test_judge_golden.py --report   # 只报数, 不计失败

**需要联网**(要调 LLM)。所以它不进那五个离线套件的门, 单独跑。

为什么需要它: 裁判是单点故障 —— 判早了整题废掉, 判严了观众永远猜不中。
改 JUDGE_SYSTEM / solve_atoms 之前先跑这个, 对比:
    false positive (不该通关却通关)   <- 最要命
    false negative (该通关却没通关)
    judge call rate
"""

from __future__ import annotations

import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from story.config import Config                       # noqa: E402
from story.llm import AnthropicMessagesClient, PuzzleWriter  # noqa: E402

GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "fixtures", "judge_golden.jsonl")


def load_golden() -> list:
    rows = []
    with io.open(GOLDEN, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    report_only = "--report" in argv

    cfg = Config()
    # **必须**把 runtime_cfg 传进去: 否则 judge_temperature 不生效,
    # 这个套件就在用网关默认温度跑。实测同一份代码两次跑会一次误判
    # (false positive)一次漏判(false negative) —— 温度没钉住的金标准
    # 是测不出回归的, 只会让人以为是模型"手气不好"。
    w = PuzzleWriter(client=AnthropicMessagesClient(cfg.llm),
                     runtime_cfg=cfg)
    rows = load_golden()

    tp = tn = fp = fn = 0
    wrong = []
    for r in rows:
        jr = w.judge(r["puzzle"], r["answer"], r["user_text"],
                     r.get("solve_atoms"))
        got = jr.solved
        want = bool(r["expected_solved"])
        if got and want:
            tp += 1
        elif not got and not want:
            tn += 1
        elif got and not want:
            fp += 1
            wrong.append(("误判为猜中", r, got, jr.error))
        else:
            fn += 1
            wrong.append(("漏判(该中未中)", r, got, jr.error))

    total = len(rows)
    print("=" * 60)
    print(f"  golden 回放: {total} 条")
    print("=" * 60)
    print(f"  正确 {tp + tn}/{total}")
    print(f"  false positive (不该通关却通关): {fp}")
    print(f"  false negative (该通关却没通关): {fn}")
    print()
    for kind, r, got, err in wrong:
        print(f"  [{kind}] {r['id']}: {r['user_text']!r}")
        print(f"       期望 solved={r['expected_solved']}, 实得 {got}"
              f"{'  错误: ' + err if err else ''}")
        if r.get("note"):
            print(f"       说明: {r['note']}")
    print()
    if report_only:
        return 0
    # 误判必须为 0 —— 这是第一优先级的指标
    if fp == 0 and fn == 0:
        print("PASS: 裁判 golden 全部正确")
        return 0
    print(f"FAILED: 误判 {fp + fn} 条")
    return 1


if __name__ == "__main__":
    sys.exit(main())
