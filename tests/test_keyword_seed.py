#!/usr/bin/env python
# coding: utf-8
"""运行: uv run tests/test_keyword_seed.py（完全离线, 无网络, 0 LLM 调用）。

G2: 关键词种子的**零件**回归。抽词是两阶段起题的第一步, 它错了后面全错。

守住的四件事:

  1. **生产抽词 vs 实验抽词同一份实现** —— G1-A/G1-B 的全部结论都建立在
     "程序按 seed 抽词"上。若生产另写一份, 那些结论就不再描述生产行为。
     这条同时管**方向**: 只允许 `story/ <- tools/`, 反过来会造出生产依赖
     实验脚本的循环。

  2. **抽取序列逐位不变** —— `--draw-only` 的输出必须与 G1 基线逐字节相同
     (md5 钉死)。改词库或改抽取方式都会让两批历史数据无法对比。

  3. **生产抽词的性质** —— 恰好 2 个词、来自不同槽位、同一个 rng 可复现、
     `used_pairs` 真的被避开。

  4. **本模块不持有全局随机状态** —— 生产抽词必须用调用方给的 rng。
     否则 prefetch 的抽词会悄悄改变 live 出题的随机序列, 而"同 seed 可
     复现"会退化成"同 seed + 同补池状态可复现"。
"""
from __future__ import annotations

import hashlib
import io
import os
import random
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from story.keyword_seed import (  # noqa: E402
    KEYWORD_BANK, KEYWORD_SEED_VERSION, _SLOTS, _dedupe,
    draw_keyword_groups, draw_two_keywords, keywords_line,
)

FAIL = [0]

#: G1-A / G1-B 的历史基线: `--draw-only`(默认 20 组)输出的 md5。
#:
#: 这个数字是**契约**, 不是"当前碰巧的值"。它证明"抽取序列没有被 G2 的
#: 搬模块改动碰过"。若它变了, 要么是有人改了词库/抽取方式(那会让 G1 的
#: 结论失效, 需要重新论证), 要么是 index/格式动了(报告里的组号会对不上)。
#:
#: ⚠️ **必须按归一化换行后的字节算**。理由: `--draw-only` 用 `print()`,
#: 而 Python 的 stdout 在 Windows 上把 `\n` 翻成 `\r\n`、在 Linux 上不翻。
#: 直接对 `r.stdout` 取 md5 会得到一个**平台相关**的值 —— 本地绿、CI 红。
#: (第一版就是这么写的: 本地 md5 = 049c7073..., 而 CI 上是另一个值,
#: 于是 `离线套件 keyword_seed` 在 CI 上挂了而本地全绿。)
#: 下面这个常量是 **LF 归一化后**的值, 两个平台一致。
G1_DRAW_MD5 = "9493d2cc11fe03b49d516f5233455dcd"

REPO = Path(__file__).resolve().parents[1]


def check(name, cond, extra=""):
    if cond:
        print(f"  ok  {name}")
        return
    print(f"  FAIL {name}  {extra}")
    FAIL[0] += 1


# ======================================================================
# 一、词库结构(§二)
# ======================================================================
def test_bank_shape():
    """词库必须分五个槽, 每槽 18~20 个互不相同的普通生活词。"""
    print("\n[K1] 词库结构")
    check("五个槽齐备",
          set(KEYWORD_BANK) == {"person", "place", "action", "object", "state"},
          sorted(KEYWORD_BANK))
    check("_SLOTS 与词库一致", tuple(sorted(_SLOTS)) == tuple(sorted(KEYWORD_BANK)),
          _SLOTS)
    bank = _dedupe(KEYWORD_BANK)
    # ⚠️ 去重后是 19 / 18 而不是 20, 这是**继承自 G1-A 的既有语料**:
    # place 里 "车站" 出现两次, state 里 "迷路" 与 "失眠" 各出现两次。
    # 本轮**不动**词库 —— 改一个词就会让 `--draw-only` 的 md5 变, 而那是
    # G1-A/G1-B 全部结论的地基(见 K4)。这里断言的是"去重后仍然饱满",
    # 不是"恰好 20": 把既有重复当成 bug 去"修"会静默毁掉可复现性。
    for slot in _SLOTS:
        n = len(bank[slot])
        check(f"{slot}: 去重后 >= 18 个", n >= 18, n)
        check(f"{slot}: 无空词", all(w.strip() for w in bank[slot]))
        check(f"{slot}: 去重后确实无重复", len(set(bank[slot])) == n)
    check("person/action/object 去重后 20 个",
          all(len(bank[s]) == 20 for s in ("person", "action", "object")),
          {s: len(bank[s]) for s in ("person", "action", "object")})
    # 词库里有重复词(语料遗留) —— `_dedupe` 必须真的在起作用, 否则被抽中
    # 的概率会被抬高, 而"程序均匀抽词"这条前提就不成立了。
    raw_dup = any(len(KEYWORD_BANK[s]) != len(set(KEYWORD_BANK[s]))
                  for s in _SLOTS)
    check("词库里确实有重复(证明 _dedupe 不是空操作)", raw_dup is True)


def test_program_draws_not_handpicked():
    """同一个 seed 必须得到同一组词; 不同 seed 应当得到不同的词。"""
    print("\n[K2] 抽词是程序的, 且可复现")
    a = draw_keyword_groups(20260920)
    b = draw_keyword_groups(20260920)
    check("同 seed 逐位相同", a == b)
    c = draw_keyword_groups(20260921)
    check("换 seed 会变", [g["keywords"] for g in a] != [g["keywords"] for g in c])
    check("20 组", len(a) == 20, len(a))
    check("前 10 组 2-key / 后 10 组 3-key",
          [g["n_keys"] for g in a] == [2] * 10 + [3] * 10)


def test_key_count_is_filter_not_redraw():
    """`key_count` 必须是**过滤**而不是重抽 —— 两组数据的可比性靠它。"""
    print("\n[K3] key_count 是过滤, 不是重抽")
    allg = draw_keyword_groups(20260920)
    two = draw_keyword_groups(20260920, 2)
    three = draw_keyword_groups(20260920, 3)
    check("2-key 取到前 10 组", [g["index"] for g in two] == list(range(1, 11)))
    check("3-key 取到后 10 组",
          [g["index"] for g in three] == list(range(11, 21)))
    # ⚠️ 最要紧的一条: 过滤出来的那些组与"整批里对应位置"**逐字相同**。
    # 若实现改成"按 key_count 重新抽", 这里立刻红 —— 而那正是 G1-B 里
    # "3-key 的第 11~15 组与 G1-A 会拿到的那几组一样"这个前提。
    by_idx = {g["index"]: g for g in allg}
    for g in two + three:
        check(f"[{g['index']:02d}] 与整批同组逐字相同",
              by_idx[g["index"]] == g)
    try:
        draw_keyword_groups(20260920, 5)
        check("非法 key_count 抛 ValueError", False, "没有抛")
    except ValueError:
        check("非法 key_count 抛 ValueError", True)


def test_g1_draw_sequence_pinned():
    """`--draw-only` 的输出必须与 G1 基线**逐字节相同**(md5)。

    ⚠️ 归一化换行再算 md5 —— 见 `G1_DRAW_MD5` 的说明。不对齐这一步会让
    这条测试**平台相关**(Windows 绿 / Linux 红), 那是假绿也是假红。
    """
    print("\n[K4] 抽取序列与 G1 基线逐字节相同")
    tool = REPO / "tools" / "experiment_keyword_riddles.py"
    r = subprocess.run(
        [sys.executable, "-X", "utf8", str(tool), "--draw-only"],
        cwd=str(REPO), capture_output=True)
    check("--draw-only 退出码 0", r.returncode == 0, r.returncode)
    text = r.stdout.decode("utf-8").replace("\r\n", "\n")
    md5 = hashlib.md5(text.encode("utf-8")).hexdigest()
    check(f"--draw-only md5(LF 归一) == {G1_DRAW_MD5}", md5 == G1_DRAW_MD5, md5)
    check("输出里有 20 组", text.count("  [") == 20, text.count("  ["))
    # 顺带钉住"两个词之间是全角逗号"这条格式 —— 报告与外部题库观感都靠它。
    check("组内两个词用全角逗号分隔",
          all("，" in ln for ln in text.splitlines() if ln.strip().startswith("[")),
          [ln for ln in text.splitlines() if ln.strip().startswith("[")][:2])


# ======================================================================
# 二、生产抽词(§三)
# ======================================================================
def test_draw_two_keywords_shape():
    """生产抽词: 恰好 2 个、不同槽位、词来自对应槽。"""
    print("\n[K5] 生产抽词: 2 个 / 不同槽 / 词来自该槽")
    bank = _dedupe(KEYWORD_BANK)
    rng = random.Random(20260920)
    for i in range(200):
        g = draw_two_keywords(rng)
        if len(g["keywords"]) != 2:
            check("恰好 2 个词", False, g)
            return
        if len(set(g["slots"])) != 2:
            check("两个词来自不同槽位", False, g)
            return
        for w, s in zip(g["keywords"], g["slots"]):
            if w not in bank[s]:
                check("词确实来自它的槽", False, (w, s))
                return
    check("200 次抽样: 恰好 2 个词", True)
    check("200 次抽样: 两个词来自不同槽位", True)
    check("200 次抽样: 词确实来自它的槽", True)


def test_draw_two_keywords_reproducible():
    """同一个 rng 状态 -> 同一次抽词(固定 seed 可复现)。"""
    print("\n[K6] 生产抽词可复现")
    a = [draw_two_keywords(random.Random(7)) for _ in range(5)]
    b = [draw_two_keywords(random.Random(7)) for _ in range(5)]
    check("同 seed 同序列", a == b, (a, b))


def test_draw_two_keywords_avoids_used_pairs():
    """`used_pairs` 必须真的被避开 —— 否则连补几道题会总拿同两个槽。"""
    print("\n[K7] used_pairs 被避开")
    rng = random.Random(11)
    seen = []
    for _ in range(10):
        g = draw_two_keywords(rng, used_pairs=seen)
        pair = tuple(sorted(g["slots"]))
        check(f"第 {len(seen)+1} 次拿到未用过的槽对 {pair}", pair not in seen)
        seen.append(pair)
    check("10 次拿满五个槽的全部 10 种组合", len(set(seen)) == 10, seen)
    # ⚠️ 全部用完时必须**回落**而不是死循环/抛异常 —— 抽词函数不允许有
    # "抽不出来"这个失败态, 调用方没有处理它的地方。
    g = draw_two_keywords(rng, used_pairs=seen)
    check("全部用完后回落(不抛、不死循环)", len(g["keywords"]) == 2, g)


def test_no_global_random_state():
    """本模块**不得**使用 `random` 的全局状态。

    判据: 调用前后 `random.random()` 的序列不受影响。若实现里漏写了
    `rng.choice` 而写成 `random.choice`, 全局状态就会被推进 —— 那会污染
    **同进程里 live 出题**的随机序列, 而那种 bug 极难定位。
    """
    print("\n[K8] 不使用全局随机状态")
    random.seed(12345)
    before = [random.random() for _ in range(3)]
    random.seed(12345)
    rng = random.Random(999)
    for _ in range(20):
        draw_two_keywords(rng)
        draw_keyword_groups(5)
    after = [random.random() for _ in range(3)]
    check("全局 random 序列未被推进", before == after, (before, after))


def test_keywords_line_shape():
    """`关键词：X，Y` —— 全角逗号, 与外部题库观感一致。"""
    print("\n[K9] keywords_line 形状")
    line = keywords_line({"keywords": ["图书馆", "上楼"]})
    check("全角逗号分隔", line == "关键词：图书馆，上楼", line)


def test_dependency_direction():
    """方向必须是 `story/ <- tools/`, 绝不能反过来。

    ## 为什么这条值得一条测试

    这不是洁癖: `story/` 是**直播进程**的包, `tools/` 是离线工具层(下载器
    / 编译器)。让 `story/keyword_seed.py` 去 import `tools/` 会把那一整串
    依赖(以及"实验脚本"这个身份)拖进直播的 import 图; 而且**改实验脚本
    会改生产行为** —— 那正是本轮要消灭的耦合。

    判据是**源码文本**: `story/keyword_seed.py` 里不得出现 `tools`。用文本
    而不是"试着 import 一下", 是因为后者会被运行环境里恰好存在的模块骗过。
    """
    print("\n[K10] 依赖方向: story 不依赖 tools")
    src = io.open(REPO / "story" / "keyword_seed.py", encoding="utf-8").read()
    # ⚠️ 只认**真正的 import 语句**。第一版用"行里有 import 又有 tools"
    # 判, 结果把 docstring 里解释这件事的两行散文也判成了违规 —— 一条
    # 会误报的测试比没有测试更糟(它逼人写更含糊的注释)。
    import ast
    tree = ast.parse(src)
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bad += [a.name for a in node.names if a.name.split(".")[0] == "tools"]
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "")
            if mod.split(".")[0] == "tools":
                bad.append(mod)
    check("story/keyword_seed.py 不 import tools/*", not bad, bad)
    # 反向必须有: 实验脚本从生产 import 抽词(否则就是两份实现)。
    exp = io.open(REPO / "tools" / "experiment_keyword_riddles.py",
                  encoding="utf-8").read()
    check("实验脚本 import story.keyword_seed",
          "from story.keyword_seed import" in exp)
    # 且**不再**有本地副本 —— grep 那个函数定义。
    check("实验脚本里没有 draw_keyword_groups 的本地定义",
          "def draw_keyword_groups" not in exp)


def test_version_constant():
    """版本号必须存在且形状与既有 *-v1 约定一致。"""
    print("\n[K11] 版本号")
    check("KEYWORD_SEED_VERSION == keyword2-v1",
          KEYWORD_SEED_VERSION == "keyword2-v1", KEYWORD_SEED_VERSION)


def main():
    tests = [
        test_bank_shape,
        test_program_draws_not_handpicked,
        test_key_count_is_filter_not_redraw,
        test_g1_draw_sequence_pinned,
        test_draw_two_keywords_shape,
        test_draw_two_keywords_reproducible,
        test_draw_two_keywords_avoids_used_pairs,
        test_no_global_random_state,
        test_keywords_line_shape,
        test_dependency_direction,
        test_version_constant,
    ]
    for t in tests:
        t()
    print()
    if FAIL[0]:
        print(f"FAIL: 关键词种子 有 {FAIL[0]} 条不通过")
        return 1
    print("PASS: 关键词种子(词库 + 抽取 + 方向)全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
