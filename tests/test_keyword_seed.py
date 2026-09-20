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

import ast
import hashlib
import io
import json
import os
import random
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from story.keyword_seed import (  # noqa: E402
    KEYWORD_BANK, KEYWORD_BANK_VERSION, KEYWORD_SEED_VERSION, _SLOTS, _dedupe,
    derive_session_seed, draw_keyword_groups, draw_two_keywords, keywords_line,
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
    # G3: 生产词源从人工词库换成真实 haiguitang corpus, 所以**种子版本**
    # 前进到 v2。人工词库那个号(retired)单独留在 `KEYWORD_BANK_VERSION`,
    # 因为 G1 实验的历史数据仍按它抽。
    check("KEYWORD_SEED_VERSION == keyword2-seeds-v2",
          KEYWORD_SEED_VERSION == "keyword2-seeds-v2", KEYWORD_SEED_VERSION)
    check("KEYWORD_BANK_VERSION == keyword2-v1(人工词库的历史号)",
          KEYWORD_BANK_VERSION == "keyword2-v1", KEYWORD_BANK_VERSION)
    check("两个号不相等", KEYWORD_SEED_VERSION != KEYWORD_BANK_VERSION)


# ======================================================================
# G3 —— corpus / bag / 降级(§十 的回归)
# ======================================================================
#
# 这一批**不读真实 corpus 文件**(那 3729 行不在版本库里), 用内联的小
# 行集跑同一条解析路径。唯一碰真文件的是 K12, 它只验证**产物形状**,
# 文件不在时跳过(而不是假装通过)。

from story.keyword_corpus import (  # noqa: E402
    CORPUS_VERSION, CorpusError, build_corpus, is_valid_keyword, load_corpus,
    normalize_pair, pairs_from_rows, split_input,
)
from story.keyword_seed import KeywordBag, describe_bag, load_bag  # noqa: E402

#: 假原始行 —— **形状照抄真实 turtle.json**: 每条只有 instruction/input/
#: output/system 四个键, `input` 形如 `关键词：X，Y`。
_FAKE_ROWS = [
    {"instruction": "请根据给定的关键词…", "input": "关键词：山顶，敲门",
     "output": "故事情节：…\n真相：…", "system": "…"},
    {"instruction": "…", "input": "关键词：电话,老师",
     "output": "A", "system": "…"},
    # 全角顿号分隔(原数据里有 348 处)
    {"instruction": "…", "input": "关键词：下雨、棺材", "output": "B",
     "system": "…"},
    # 只有 1 个词 -> 不是 2-key, 必须被丢掉
    {"instruction": "…", "input": "关键词：水", "output": "C", "system": "…"},
    # 3 个词 -> 也要丢(不能"取前两个")
    {"instruction": "…", "input": "关键词：a，b，c", "output": "D",
     "system": "…"},
    # 0 个词
    {"instruction": "…", "input": "关键词：", "output": "E", "system": "…"},
    # 超长句(整段谜面误填) -> 丢
    {"instruction": "…", "input": "关键词：一位女士去鞋店里买了一双红色高跟鞋，"
                                   "这双高跟鞋预示了她今晚的死亡",
     "output": "F", "system": "…"},
    # 夹标点的损坏文本 -> 丢
    {"instruction": "…", "input": "关键词：他为什么死了？，??",
     "output": "G", "system": "…"},
    # 与第 3 条**同一对**, 只是顺序反过来 -> 去重
    {"instruction": "…", "input": "关键词：棺材，下雨", "output": "H",
     "system": "…"},
    # 与第 1 条**同一对**(把全角逗号换成半角 + 加空格)-> 去重
    {"instruction": "…", "input": "关键词：  山顶 , 敲门 ", "output": "I",
     "system": "…"},
    # 直播不适宜的 seed -> 确定性过滤
    {"instruction": "…", "input": "关键词：自杀，药瓶", "output": "J",
     "system": "…"},
]


def _tmp_json(d, name, obj):
    p = os.path.join(d, name)
    with io.open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    return p


def test_g3_corpus_extracts_only_input():
    """§一: 关键词**只**从 `input` 来 —— 换掉 puzzle/answer 不得影响结果。

    这条是任务书里"不读取对应谜题来决定'这个 seed 好不好'"的**可执行**
    版本。做法: 把同一批行的 `output` 全部换成垃圾, 重跑解析, pair 表
    必须**逐位相同**。
    """
    print("\n[K12] corpus 只从 input 提关键词")
    a = pairs_from_rows(_FAKE_ROWS)
    mutated = [dict(r, output="完全不同的谜面与谜底 " + str(i))
               for i, r in enumerate(_FAKE_ROWS)]
    b = pairs_from_rows(mutated)
    check("改掉全部 output 后 pair 表逐位不变", a["pairs"] == b["pairs"],
          (a["pairs"], b["pairs"]))
    # 连 instruction / system 一起换掉也必须不变。
    mutated2 = [dict(r, instruction="别的 prompt", system="别的 system")
                for r in _FAKE_ROWS]
    check("改掉 instruction/system 也不变",
          pairs_from_rows(mutated2)["pairs"] == a["pairs"])
    # `output` 里出现关键词也不得被采到(反向: 若实现偷偷扫 output 就会多)
    poisoned = [dict(r, output="关键词：额外的，词") for r in _FAKE_ROWS]
    check("output 里塞关键词不会被采到",
          pairs_from_rows(poisoned)["pairs"] == a["pairs"])


def test_g3_corpus_only_two_key_rows():
    """§一: **只保留恰好 2 个有效关键词**的记录。"""
    print("\n[K13] 只保留合法 2-key")
    st = pairs_from_rows(_FAKE_ROWS)
    pairs = [tuple(p) for p in st["pairs"]]
    check("每个 pair 都是 2 个词", all(len(p) == 2 for p in pairs), pairs)
    # 1 个词的记录(水)、3 个词的(a,b,c)、0 个词的必须都不在。
    flat = [w for p in pairs for w in p]
    check("1-key 记录被丢掉", "水" not in flat, flat)
    check("**3-key 不被截成前两个**", "a" not in flat and "c" not in flat, flat)
    check("空 input 被丢掉", "" not in flat)
    # 3 个词的记录若被"取前两个"就会产生 (a,b); 那条必须不存在。
    check("没有产生 (a,b) 这种截断 pair",
          ("a", "b") not in pairs and ("b", "a") not in pairs, pairs)


def test_g3_corpus_dedupes_pairs():
    """§二: 相同 pair 去重; **不按原数据出现频率重复存**。"""
    print("\n[K14] pair 去重 + 不带频率")
    st = pairs_from_rows(_FAKE_ROWS)
    pairs = [tuple(p) for p in st["pairs"]]
    check("unique_pairs == len(pairs)",
          st["unique_pairs"] == len(pairs), (st["unique_pairs"], len(pairs)))
    check("没有重复 pair", len(set(pairs)) == len(pairs), pairs)
    # 顺序反过来的同一对(棺材/下雨 vs 下雨/棺材)只留一份。
    n_rain = sum(1 for p in pairs if set(p) == {"下雨", "棺材"})
    check("**顺序反过来的同一对只留一份**", n_rain == 1, n_rain)
    # 全角/半角 + 空白差异不算不同 pair。
    n_hill = sum(1 for p in pairs if set(p) == {"山顶", "敲门"})
    check("**纯格式差异不算不同 pair**", n_hill == 1, n_hill)
    # two_key_rows > unique_pairs 是**信息**(说明真有重复), 两个都记。
    check("two_key_rows 记的是去重前的条数",
          st["two_key_rows"] > st["unique_pairs"],
          (st["two_key_rows"], st["unique_pairs"]))
    # 产物里**不得**有频率字段。
    d = build_corpus(_FAKE_ROWS)
    check("产物没有频率/权重字段",
          not any(k in d for k in ("freq", "frequency", "weights", "count")),
          sorted(d))


def test_g3_corpus_filters_broken_and_unsuitable():
    """确定性清洗: 超长句 / 标点垃圾 / 不适宜 seed 全部丢掉。"""
    print("\n[K15] 确定性清洗(无 LLM)")
    flat = [w for p in pairs_from_rows(_FAKE_ROWS)["pairs"] for w in p]
    check("超长句被丢", not any(len(w) > 12 for w in flat), flat)
    check("夹标点的损坏文本被丢",
          not any("？" in w or "?" in w for w in flat), flat)
    check("不适宜的 seed 被丢",
          not any("自杀" in w for w in flat), flat)
    # 判据本身: 白名单是**字符**级的, 不是黑名单。
    check("纯汉字/字母/数字 -> 合法", is_valid_keyword("山顶"))
    check("带句号 -> 不合法", not is_valid_keyword("他死了。"))
    check("带问号 -> 不合法", not is_valid_keyword("为什么?"))
    check("空 -> 不合法", not is_valid_keyword("   "))
    check("超长 -> 不合法", not is_valid_keyword("一" * 13))
    check("**1 个字也合法**(原数据里有 `110`/`b` 这种)",
          is_valid_keyword("b") and is_valid_keyword("110"))
    # 分隔符: 三种都认。
    check("全角逗号", split_input("关键词：a，b") == ["a", "b"])
    check("半角逗号", split_input("关键词：a,b") == ["a", "b"])
    check("顿号", split_input("关键词：a、b") == ["a", "b"])
    check("全角空格也算分隔", split_input("关键词：a　b") == ["a", "b"])
    check("没有前缀也认", split_input("a，b") == ["a", "b"])
    check("归一: 空白折叠", normalize_pair(" a  b ", "c") == ("a b", "c"))
    check("归一: 顺序无关", normalize_pair("a", "b") == normalize_pair("b", "a"))


def test_g3_bag_reproducible_and_no_repeat():
    """§三 / §四: 同 session seed 可复现; 一个 bag 内不重复; 耗尽重洗。"""
    print("\n[K16] bag: 可复现 + 不放回")
    pairs = [("a", "b"), ("c", "d"), ("e", "f"), ("g", "h")]
    b1 = KeywordBag(pairs, 12345)
    b2 = KeywordBag(pairs, 12345)
    s1 = [b1.draw()["keywords"] for _ in range(4)]
    s2 = [b2.draw()["keywords"] for _ in range(4)]
    check("**同 session seed 顺序完全可复现**", s1 == s2, (s1, s2))
    check("**一个 bag 内不重复**",
          len({tuple(x) for x in s1}) == 4, s1)
    check("四条正好是那四对(只是顺序不同)",
          sorted(tuple(x) for x in s1) == sorted(pairs), s1)
    # 耗尽 -> 重洗下一轮, 不会"抽不出来"
    r5 = b1.draw()
    check("第 5 次仍能抽到(自动重洗)", bool(r5["keywords"]), r5)
    check("round 前进到 2", r5["round"] == 2, r5["round"])
    check("index 连续累加", r5["index"] == 5, r5["index"])
    # 换 seed 会变
    b3 = KeywordBag(pairs, 999)
    check("换 session seed 顺序会变",
          [b3.draw()["keywords"] for _ in range(4)] != s1)
    # 空表必须响亮失败, 不能静默给空
    try:
        KeywordBag([], 1)
        check("空 pair 表抛 ValueError", False, "没有抛")
    except ValueError:
        check("空 pair 表抛 ValueError", True)


def test_g3_bag_does_not_touch_global_random():
    """bag 只能用自己的 Random —— 否则会改 live 出题的序列。"""
    print("\n[K17] bag 不碰全局 random")
    random.seed(4242)
    want = [random.random() for _ in range(6)]
    random.seed(4242)
    b = KeywordBag([("a", "b"), ("c", "d"), ("e", "f")], 7)
    for _ in range(9):
        b.draw()
    got = [random.random() for _ in range(6)]
    check("**抽 9 次后全局 random 序列逐位不变**", got == want, (got, want))


def test_g3_session_seed_derivation():
    """§四: 从 quality_seed 派生; 同输入同输出, 且与输入/其它用途不撞。"""
    print("\n[K18] session seed 派生")
    check("确定性", derive_session_seed(20260920) == derive_session_seed(20260920))
    check("换 quality_seed 会变",
          derive_session_seed(20260920) != derive_session_seed(20260921))
    check("换 session 会变",
          derive_session_seed(20260920, 0) != derive_session_seed(20260920, 1))
    # 与 director.py 给补池 rng 那个派生**不是**同一个数(否则三条链会撞)
    q = 20260920
    check("**不等于 director 的 pf_seed**",
          derive_session_seed(q) != (q ^ 0x9E3779B9), derive_session_seed(q))
    check("**不等于 quality_seed 本身**", derive_session_seed(q) != q)
    check("是 64 位正整数", 0 <= derive_session_seed(q) < 2 ** 64)
    try:
        derive_session_seed(None)
        check("None 抛 ValueError", False, "没有抛")
    except ValueError:
        check("None 抛 ValueError", True)


def test_g3_corpus_missing_is_explicit_not_bank():
    """§五: corpus 缺失/空/损坏 -> 抛错(由调用方显式降级), **不回退词库**。"""
    print("\n[K19] corpus 不可用必须显式")
    with tempfile.TemporaryDirectory() as d:
        for name, obj in (("missing.json", None),
                          ("empty.json", {"pairs": []}),
                          ("null.json", {"pairs": None}),
                          ("bad.json", {"pairs": "不是列表"}),
                          ("nokeys.json", {"foo": 1})):
            p = os.path.join(d, name)
            if obj is not None:
                _tmp_json(d, name, obj)
            try:
                load_corpus(p)
                check(f"{name}: 抛 CorpusError", False, "没有抛")
            except CorpusError:
                check(f"{name}: 抛 CorpusError", True)
        # 路径不存在(不是上面造的)
        try:
            load_corpus(os.path.join(d, "nope-not-here.json"))
            check("路径不存在: 抛 CorpusError", False, "没有抛")
        except CorpusError:
            check("路径不存在: 抛 CorpusError", True)
        # 全是无效 pair 的表
        _tmp_json(d, "allbad.json", {"pairs": [[], ["a"], [None, None], ""]})
        try:
            load_corpus(os.path.join(d, "allbad.json"))
            check("pair 全无效: 抛 CorpusError", False, "没有抛")
        except CorpusError:
            check("pair 全无效: 抛 CorpusError", True)
        # 空字符串路径
        try:
            load_corpus("")
            check("路径为空: 抛 CorpusError", False, "没有抛")
        except CorpusError:
            check("路径为空: 抛 CorpusError", True)
        # ⚠️ 最要紧的一条: 降级路径**不得**碰 KEYWORD_BANK。
        #
        # 用 AST 查**真正的引用**, 不是 grep 文本 —— `keyword_corpus.py`
        # 的 **docstring 里就写着** "G1 / G2 的关键词来自 `KEYWORD_BANK`"
        # (那是在说明"为什么不再用它")。朴素 grep 会把这段散文当成引用,
        # 于是这条断言永远红, 然后被人为"修"掉 —— 那正是 K10 踩过的坑。
        names = set()
        tree = ast.parse(io.open(
            os.path.join(Path(__file__).resolve().parents[1], "story",
                         "keyword_corpus.py"), encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.alias):
                names.add(node.asname or node.name.split(".")[-1])
        check("**keyword_corpus.py 不引用 KEYWORD_BANK(按 AST 查)**",
              "KEYWORD_BANK" not in names, sorted(n for n in names
                                                  if "KEYWORD" in n))


def test_g3_bag_load_from_file():
    """`load_bag` 读产物建 bag, 并给出 §四 要的日志元数据。"""
    print("\n[K20] load_bag + 日志行")
    with tempfile.TemporaryDirectory() as d:
        p = _tmp_json(d, "c.json", build_corpus(_FAKE_ROWS))
        bag, meta = load_bag(p, 20260920)
        check("meta 有 corpus_version",
              meta["corpus_version"] == CORPUS_VERSION, meta)
        check("meta 的 pair_count 与表一致",
              meta["pair_count"] == bag.served + len(bag.pairs) - bag.served
              or meta["pair_count"] == len(bag.pairs), meta)
        check("meta 有 source", meta["source"] == "neurostellar/haiguitang",
              meta)
        line = describe_bag(meta, 20260920)
        for token in ("session_seed=20260920",
                      "corpus_version=" + CORPUS_VERSION,
                      "pair_count="):
            check(f"日志含 {token}", token in line, line)
        # meta **不含**整张 pair 表(300 多条不该进日志)
        check("meta 里没有 pairs", "pairs" not in meta, sorted(meta))


def test_g3_real_corpus_shape():
    """真产物的**形状**(若在)。文件不在时跳过 —— 不假装通过。"""
    print("\n[K21] 真 corpus 产物形状")
    p = os.path.join(Path(__file__).resolve().parents[1],
                     "data", "keyword2_seed_pairs.json")
    if not os.path.exists(p):
        print("  skip 真 corpus 不在(未构建), 跳过形状检查")
        return
    d = load_corpus(p)
    check("corpus_version == " + CORPUS_VERSION,
          d["corpus_version"] == CORPUS_VERSION, d["corpus_version"])
    check("source == neurostellar/haiguitang",
          d["source"] == "neurostellar/haiguitang", d["source"])
    check("有 pairs", len(d["pairs"]) > 0, len(d["pairs"]))
    check("unique_pairs 与表长一致",
          d["unique_pairs"] == len(d["pairs"]),
          (d["unique_pairs"], len(d["pairs"])))
    check("raw_rows >= two_key_rows >= unique_pairs",
          d["raw_rows"] >= d["two_key_rows"] >= d["unique_pairs"],
          (d["raw_rows"], d["two_key_rows"], d["unique_pairs"]))
    # ⚠️ 真产物的关键性质: pair 里**没有**槽位、没有人工词库的痕迹。
    flat = [w for pr in d["pairs"] for w in pr]
    check("每个 pair 都是 2 个非空词", all(len(pr) == 2 and pr[0] and pr[1]
                                        for pr in d["pairs"]))
    check("**没有重复 pair**",
          len({tuple(sorted(pr)) for pr in d["pairs"]}) == len(d["pairs"]))
    # ⚠️ 真产物与人工词库的重叠 —— 这条断言有讲究。
    #
    # 人工词库当初就是照着"普通生活词"挑的(柴米油盐、邻里日常), 所以
    # 真 corpus 里**必然**有一批常见词与它撞上(实测 462 个 unique 词里
    # 有 28 个重合, 约 6%)。撞上不是污染, 反而是"人工词库的语感没跑偏"
    # 的证据。所以**不能**断言"零重叠" —— 那会永远红。
    #
    # 真正要钉的是**来源已经变了**: 绝大多数词**只可能**来自真 corpus,
    # 人工词库里根本没有它们。反过来, 若哪天有人把生产接回人工词库, 这个
    # 比例会立刻倒过来(→ 100%)。
    bank_words = {w for ws in KEYWORD_BANK.values() for w in ws}
    uniq = set(flat)
    only_bank = sum(1 for w in uniq if w in bank_words)
    check("**绝大多数 unique 词不在人工词库里**(证明确实换了来源)",
          only_bank < len(uniq) * 0.15, (only_bank, len(uniq)))
    # ⚠️ 硬证据: 真 corpus 里有**人工词库产不出来**的 pair。
    #
    # 人工词库只有 5 个槽、每槽 20 词, 所以它的 pair 空间是
    # C(5,2) x 20 x 20 = 4000 种, 且**每个词都必须是那 100 个之一**。
    # 真 corpus 的 pair 里只要存在"两个词都**不在**人工词库"的组合
    # (实测大量存在, 如 `广场舞/吵架` 里的 `广场舞`), 就证明词源换了。
    # 这条**不是**在数重叠比例 —— 它查的是一个**只有真 corpus 能满足**
    # 的存在性条件, 所以人工词库若被接回生产, 它必然红。
    pure_corpus = [pr for pr in d["pairs"]
                   if pr[0] not in bank_words and pr[1] not in bank_words]
    check("**存在两个词都不在人工词库里的 pair**",
          len(pure_corpus) > 0, (len(pure_corpus), len(d["pairs"])))


def test_g3_experiment_has_no_own_keyword_logic():
    """§九: 实验脚本不得维护第二份 corpus / 词库。"""
    print("\n[K22] 实验脚本不再有第二份关键词逻辑")
    root = Path(__file__).resolve().parents[1]
    src = io.open(root / "tools" / "experiment_keyword_riddles.py",
                  encoding="utf-8").read()
    check("不定义自己的 KEYWORD_BANK", "KEYWORD_BANK = {" not in src)
    check("不定义自己的 _FAKE / 内联词表",
          not re.search(r"^\s*KEYWORD_BANK\s*[:=]\s*\{", src, re.M))
    check("从生产 import 词库/抽取", "from story.keyword_seed import" in src)
    check("**能从生产 corpus 抽词**(--draw-corpus)",
          "--draw-corpus" in src and "load_bag" in src)
    # 反向: 生产不得 import 实验脚本。
    #
    # ⚠️ 同样用 AST 而不是 grep —— 这几个模块的 **docstring 里都写着**
    # "绝不允许 import tools/experiment_keyword_riddles"(那正是这条规则
    # 的说明)。朴素 grep 会把说明文字当成违规。
    for f in ("story/keyword_seed.py", "story/keyword_corpus.py",
              "story/prefetch.py"):
        tree = ast.parse(io.open(root / f, encoding="utf-8").read())
        mods = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods.extend(al.name for al in node.names)
            elif isinstance(node, ast.ImportFrom):
                mods.append(node.module or "")
        check(f"{f} 不 import 实验脚本(按 AST 查)",
              not any("experiment_keyword_riddles" in m for m in mods), mods)
        check(f"{f} 不 import tools.*(顶层)",
              not any(m == "tools" or m.startswith("tools.") for m in mods),
              mods)


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
        # ---- G3 ----
        test_g3_corpus_extracts_only_input,
        test_g3_corpus_only_two_key_rows,
        test_g3_corpus_dedupes_pairs,
        test_g3_corpus_filters_broken_and_unsuitable,
        test_g3_bag_reproducible_and_no_repeat,
        test_g3_bag_does_not_touch_global_random,
        test_g3_session_seed_derivation,
        test_g3_corpus_missing_is_explicit_not_bank,
        test_g3_bag_load_from_file,
        test_g3_real_corpus_shape,
        test_g3_experiment_has_no_own_keyword_logic,
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
