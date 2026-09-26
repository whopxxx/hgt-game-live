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
    #
    # G4-D: 词表加了 seed 级 safety 过滤 -> 口径变了 -> 再前进一版。
    check("KEYWORD_SEED_VERSION == keyword2-vocab-v2",
          KEYWORD_SEED_VERSION == "keyword2-vocab-v2", KEYWORD_SEED_VERSION)
    check("KEYWORD_BANK_VERSION == keyword2-v1(人工词库的历史号)",
          KEYWORD_BANK_VERSION == "keyword2-v1", KEYWORD_BANK_VERSION)
    check("两个号不相等", KEYWORD_SEED_VERSION != KEYWORD_BANK_VERSION)


# ======================================================================
# G4 —— 独立词库 / 随机重新组合 / 降级(§八 的回归)
# ======================================================================
#
# 这一批**不读真实词库文件**(那 3729 行不在版本库里), 用内联的小行集跑
# 同一条解析路径。唯一碰真文件的是 K36 / K37。

from story.keyword_corpus import (  # noqa: E402
    CORPUS_VERSION, CorpusError, build_vocabulary, is_valid_keyword,
    load_vocabulary, split_input,
)
from story.keyword_seed import (  # noqa: E402
    KeywordBag, combos, describe_bag, load_bag,
)

#: 假原始行 —— **形状照抄真实 turtle.json**: 每条只有 instruction/input/
#: output/system 四个键, `input` 形如 `关键词：X，Y`。
#:
#: ⚠️ 刻意包含了 **1-key / 2-key / 3-key / 4-key** 四种行 —— §一 要求
#: **所有** input 都展开成词, 不是只读恰好 2-key 的那批。G3 的夹具只有
#: 2-key, 所以挡不住"只读 2-key"这个回归; G4 的夹具把它挡住了。
_FAKE_ROWS = [
    # 1-key 行 -> 贡献 1 个词
    {"instruction": "…", "input": "关键词：山地", "output": "A", "system": "…"},
    # 2-key 行 -> 2 个词(全角逗号)
    {"instruction": "…", "input": "关键词：山顶，敲门", "output": "B",
     "system": "…"},
    # 3-key 行 -> **3 个词**(不能只取前两个!)
    {"instruction": "…", "input": "关键词：电话，老师，火车", "output": "C",
     "system": "…"},
    # 4-key 行 -> 4 个词(半角逗号 + 顿号混用)
    {"instruction": "…", "input": "关键词：水,a、谢谢,桥", "output": "D",
     "system": "…"},
    # 全角顿号
    {"instruction": "…", "input": "关键词：下雨、棺材", "output": "E",
     "system": "…"},
    # 0 个词
    {"instruction": "…", "input": "关键词：", "output": "F", "system": "…"},
    # ---- 必须被丢掉的: 完整句子 / 事件描述 ----
    {"instruction": "…",
     "input": "关键词：一姐妹母亲去世，回家后却把姐姐杀了", "output": "G",
     "system": "…"},
    {"instruction": "…",
     "input": "关键词：不久后我把大哥也杀了，我有两个哥哥", "output": "H",
     "system": "…"},
    {"instruction": "…",
     "input": "关键词：五人同时到达目的地。加快脚步的四人被淋成了落汤鸡",
     "output": "I", "system": "…"},
    # 带标点/疑问
    {"instruction": "…", "input": "关键词：他为什么死了？，??", "output": "J",
     "system": "…"},
    # 直播不适宜
    {"instruction": "…", "input": "关键词：自杀，药瓶", "output": "K",
     "system": "…"},
    # 同词重复(跨行) -> 去重
    {"instruction": "…", "input": "关键词：山顶，桥", "output": "L",
     "system": "…"},
]


def _tmp_json(d, name, obj):
    p = os.path.join(d, name)
    with io.open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    return p


def _vocab_words(rows=None):
    return build_vocabulary(rows if rows is not None else _FAKE_ROWS)["keywords"]


def test_g4_vocab_expands_every_row():
    """§一: **所有** input 都拆成独立词 —— 1-key / 3-key / 4-key 全都要。

    这是 G4 与 G3 最本质的差别。G3 只读恰好 2 个关键词的行, 于是
    `电话/老师/火车` 这一行**整行被丢掉**, 三个词都进不了词库。
    """
    print("\n[K23] 每一行的每个词都进词库")
    words = set(_vocab_words())
    check("**1-key 行贡献了词(山地)**", "山地" in words, sorted(words))
    for w in ("电话", "老师", "火车"):
        check(f"**3-key 行的词进了词库: {w}**", w in words, sorted(words))
    for w in ("水", "a", "谢谢", "桥"):
        check(f"**4-key 行的词进了词库: {w}**", w in words, sorted(words))
    for w in ("山顶", "敲门"):
        check(f"2-key 行的词进了词库: {w}", w in words)


def test_g4_vocab_is_words_not_pairs():
    """§三: **原始 pair 不被当作采样单位**。

    词库里存的是**词**, 不是 `[A,B]`。这条查三件事:
      (a) `keywords` 的每个元素都是**字符串**, 不是 list/tuple;
      (b) 产物里**没有** `pairs` 字段(那是 G3 的形状);
      (c) 原本成对的词现在各自独立可抽。
    """
    print("\n[K24] 词库是词, 不是 pair")
    v = build_vocabulary(_FAKE_ROWS)
    kws = v["keywords"]
    check("**每个元素都是 str, 不是 pair**",
          all(isinstance(w, str) for w in kws),
          [type(w).__name__ for w in kws[:5]])
    check("没有任何元素是 list/tuple",
          not any(isinstance(w, (list, tuple)) for w in kws))
    check("**产物里没有 pairs 字段**", "pairs" not in v, sorted(v))
    for k in ("raw_rows", "raw_token_count", "valid_token_count",
              "unique_token_count", "keywords", "corpus_version", "source"):
        check(f"产物有 {k}", k in v, sorted(v))
    check("原本成对的 山顶 / 桥 都在库里(各自独立)",
          {"山顶", "桥"} <= set(kws))


def test_g4_vocab_counts_and_dedupe():
    """§一: 三个计数各司其职; 同词只存一次; **不带频率**。"""
    print("\n[K25] 计数 / 去重 / 不带频率")
    v = build_vocabulary(_FAKE_ROWS)
    check("raw_token_count >= valid_token_count",
          v["raw_token_count"] >= v["valid_token_count"],
          (v["raw_token_count"], v["valid_token_count"]))
    check("valid_token_count >= unique_token_count",
          v["valid_token_count"] >= v["unique_token_count"],
          (v["valid_token_count"], v["unique_token_count"]))
    check("unique_token_count == len(keywords)",
          v["unique_token_count"] == len(v["keywords"]),
          (v["unique_token_count"], len(v["keywords"])))
    check("raw_rows == 输入行数", v["raw_rows"] == len(_FAKE_ROWS))
    check("没有重复词", len(set(v["keywords"])) == len(v["keywords"]))
    check("keywords 已排序(顺序确定)",
          list(v["keywords"]) == sorted(v["keywords"]))
    # `山顶` 在第 2 行与第 12 行都出现 -> valid 里两次, unique 里一次
    check("**重复词被去重(valid > unique)**",
          v["valid_token_count"] > v["unique_token_count"],
          (v["valid_token_count"], v["unique_token_count"]))
    check("**产物没有频率/权重字段**",
          not any(k in v for k in ("freq", "frequency", "weights",
                                   "counts", "tf")),
          sorted(v))


def test_g4_vocab_rejects_sentence_fragments():
    """§二: 完整句子 / 事件描述**不得**进词库。不要只靠长度。"""
    print("\n[K26] 句子碎片被丢掉")
    flat = set(_vocab_words())
    for frag in ("一姐妹母亲去世", "回家后却把姐姐杀了",
                 "不久后我把大哥也杀了", "我有两个哥哥",
                 "五人同时到达目的地。加快脚步的四人被淋成了落汤鸡"):
        check(f"**整句被丢: {frag[:12]}…**", frag not in flat)
    # 判据本身
    check("普通名词 -> 合法", is_valid_keyword("山顶"))
    check("人物称谓 -> 合法", is_valid_keyword("司机"))
    check("地点 -> 合法", is_valid_keyword("图书馆"))
    check("普通动作 -> 合法", is_valid_keyword("敲门"))
    check("**含'我' -> 不合法(代词)**", not is_valid_keyword("我杀了人"))
    check("**含'了' -> 不合法(虚词)**", not is_valid_keyword("他死了"))
    check("**含'的' -> 不合法(虚词)**", not is_valid_keyword("我的书"))
    check("**含'为什么' -> 不合法**", not is_valid_keyword("为什么哭"))
    check("**含'一个' -> 不合法(量词短语)**", not is_valid_keyword("一个男人"))
    check("**含'很' -> 不合法(程度副词)**", not is_valid_keyword("光线很暗"))
    check("带句号 -> 不合法", not is_valid_keyword("他死了。"))
    check("带问号 -> 不合法", not is_valid_keyword("为什么?"))
    check("带顿号 -> 不合法", not is_valid_keyword("a、b"))
    check("空 -> 不合法", not is_valid_keyword("   "))
    check("**超过 6 字 -> 不合法**", not is_valid_keyword("一" * 7))
    check("**1 个字也合法**(原数据里有 110 / b)", is_valid_keyword("b")
          and is_valid_keyword("110"))
    # 分隔符: 三种都认
    check("全角逗号", split_input("关键词：a，b") == ["a", "b"])
    check("半角逗号", split_input("关键词：a,b") == ["a", "b"])
    check("顿号", split_input("关键词：a、b") == ["a", "b"])
    check("全角空格也算分隔", split_input("关键词：a　b") == ["a", "b"])
    check("没有前缀也认", split_input("a，b") == ["a", "b"])


def test_g4_vocab_extracts_only_input():
    """§一: 词**只**从 `input` 来 —— 换掉 puzzle/answer 不得影响结果。"""
    print("\n[K27] 词库只从 input 提词")
    a = build_vocabulary(_FAKE_ROWS)
    mutated = [dict(r, output="完全不同的谜面与谜底 " + str(i))
               for i, r in enumerate(_FAKE_ROWS)]
    b = build_vocabulary(mutated)
    check("**改掉全部 output 后词表逐位不变**",
          a["keywords"] == b["keywords"])
    mutated2 = [dict(r, instruction="别的 prompt", system="别的 system")
                for r in _FAKE_ROWS]
    check("改掉 instruction/system 也不变",
          build_vocabulary(mutated2)["keywords"] == a["keywords"])
    poisoned = [dict(r, output="关键词：额外的，词") for r in _FAKE_ROWS]
    check("output 里塞关键词不会被采到",
          build_vocabulary(poisoned)["keywords"] == a["keywords"])


def test_g4_vocab_unsuitable_filtered():
    """§三: 不适宜词确定性过滤(无 LLM)。"""
    print("\n[K28] 直播不适宜词被过滤")
    words = set(_vocab_words())
    check("**自杀被丢**", "自杀" not in words, sorted(words))
    check("药瓶**在**(普通物品, 不该被连坐)", "药瓶" in words)
    for bad in ("性交", "强奸", "毒品", "自杀"):
        check(f"{bad} -> 不合法", not is_valid_keyword(bad))


def test_g4_bag_recombines_independently():
    """§四: 两个词由**独立词库**抽出, 是**重新组合**, 不是原始 pair。"""
    print("\n[K29] bag 重新组合两个独立词")
    words = ["甲", "乙", "丙", "丁"]
    bag = KeywordBag(words, 12345, keyword_cooldown=0, pair_cooldown=0)
    seen = set()
    # ⚠️ 抽 4 倍次数而不是"恰好 6 次" —— cooldown=0 时这是**有放回**的
    # 独立抽样, 6 次里拿满 6 种组合的概率不接近 1(那是 coupon collector)。
    # 断言"跑够次数后每种组合都出得来"才是这条测试要证明的性质:
    # **组合是自由生成的, 不受原始 pair 限制**。
    for _ in range(48):
        d = bag.draw()
        check("**word1 != word2**", d["keywords"][0] != d["keywords"][1], d)
        check("两个词都在词库里", set(d["keywords"]) <= set(words), d)
        check("**slots 恒为空(没有槽位概念)**", d["slots"] == [], d)
        seen.add(tuple(sorted(d["keywords"])))
        if len(seen) == 6:
            break
    check("**4 个词的全部 6 种 unordered 组合都出得来**",
          len(seen) == 6, sorted(seen))
    check("未触发放宽", bag.relaxed_total == 0, bag.relaxed_total)


def test_g4_bag_reproducible_and_no_short_repeat():
    """§五 / §六: 固定 seed 可复现; 短期不重复; 不会无限重抽。"""
    print("\n[K30] bag: 可复现 + 短期不重复")
    words = ["w%02d" % i for i in range(60)]
    b1 = KeywordBag(words, 4242)
    b2 = KeywordBag(words, 4242)
    s1 = [tuple(b1.draw()["keywords"]) for _ in range(30)]
    s2 = [tuple(b2.draw()["keywords"]) for _ in range(30)]
    check("**同 session seed 顺序完全可复现**", s1 == s2)
    check("每次都 word1 != word2", all(p[0] != p[1] for p in s1))
    # ⚠️ keyword cooldown 是**软约束**, 不是"40 个词必须全不同"。
    #
    # 实现是"抽到落在窗口里的词就重抽" —— 所以**单次 draw 内部**两个词
    # 一定不在窗口里, 但窗口是**滚动**的。因此"前 20 组 40 个词互不相同"
    # **过强**(实测 38/40), 会假红。
    #
    # 真正要证明的是**短期重复率被压住了**: 无 cooldown 的独立抽样下
    # 40 次抽 60 个词期望约 13 个重复 —— 差一个数量级。
    first40 = [w for p in s1[:20] for w in p]
    dups = len(first40) - len(set(first40))
    check("**前 20 组的词重复数 <= 4(cooldown 生效)**",
          dups <= 4, (dups, len(first40)))
    # ⚠️ **不**断言"零放宽"。60 个词 + 40 词窗口时可选词会变稀, 24 次
    # 重试偶尔真的会失败 -> 放宽是**设计行为**(见 `MAX_TRIES`), 不是回归。
    check("**放宽是少数(<= 1/3 的组)**",
          b1.relaxed_total <= len(s1) // 3,
          (b1.relaxed_total, len(s1)))
    b3 = KeywordBag(words, 999)
    check("换 session seed 顺序会变",
          [tuple(b3.draw()["keywords"]) for _ in range(5)] != s1[:5])

    # ---- pair cooldown: 用一个**小**词库才测得到 ----
    #
    # ⚠️ 60 个词抽 30 次时, pair 撞车的概率本来就接近 0 —— 在那种规模下
    # **删掉 pair cooldown 这条测试也不会红**(变异实测: 0 条失败)。
    # 要真正验证这个机制, 必须把词库压到"随机撞车几乎必然发生"的规模:
    # 10 个词 = 45 种组合, 抽 40 次。
    small = ["s%02d" % i for i in range(10)]
    sb = KeywordBag(small, 31337, keyword_cooldown=0, pair_cooldown=45)
    ps = [tuple(sorted(sb.draw()["keywords"])) for _ in range(40)]
    check("**小词库里 40 组的 pair 仍严格不重复**",
          len(set(ps)) == len(ps),
          (len(set(ps)), len(ps)))
    # 反证: 把 pair 窗口关掉, 同样规模下**必然**出现重复 —— 证明上面那条
    # 不是"规模太小所以碰巧没撞"。
    nb = KeywordBag(small, 31337, keyword_cooldown=0, pair_cooldown=0)
    nps = [tuple(sorted(nb.draw()["keywords"])) for _ in range(40)]
    check("**(反证)关掉 pair 窗口后同样规模下确实会重复**",
          len(set(nps)) < len(nps), (len(set(nps)), len(nps)))


def test_g4_bag_relaxes_instead_of_hanging():
    """§五: 不能陷入无限重抽 —— 窗口相对词库过大时必须**放宽**而不是卡死。"""
    print("\n[K31] 窗口过大时放宽而不是卡死")
    bag = KeywordBag(["甲", "乙", "丙"], 7, keyword_cooldown=999,
                     pair_cooldown=999)
    got = [bag.draw() for _ in range(10)]
    check("**10 次全部返回(没有卡死)**", len(got) == 10, len(got))
    check("每次都 word1 != word2", all(g["keywords"][0] != g["keywords"][1]
                                    for g in got))
    check("**确实触发了放宽**(否则说明窗口没生效)",
          bag.relaxed_total > 0, bag.relaxed_total)
    for bad in ([], ["只有一个"]):
        try:
            KeywordBag(bad, 1)
            check(f"词表 {bad!r} 抛 ValueError", False, "没有抛")
        except ValueError:
            check(f"词表 {bad!r} 抛 ValueError", True)

    # ---- 更强的一条: 窗口真的耗尽, 两层放宽都必须走到 ----
    #
    # ⚠️ 上面"3 个词 / 窗口 999"那条**不够强**, 而且我第一版写的
    # "40 个词 / 窗口 40 / 抽 60 次"也**不够强** —— 在那些规模下第二层
    # (完全放宽)其实到不了, 于是把第二层的兜底改成"返回空"**测试也不红**
    # (变异实测: 0 条失败)。那说明我测的是**死路径**。
    #
    # 真能走到第二层的配置是**小词库 + 中等 cooldown**: 3 个词、窗口 2/5
    # 时, 两层各 24 次重试都可能被窗口挡掉, 兜底必然被触发(实测 200 次
    # 抽取里 `relaxed==2` 出现)。这才是"不能陷入无限重抽"的**真正边界**。
    tiny = KeywordBag(["甲", "乙", "丙"], 3, keyword_cooldown=2,
                      pair_cooldown=5)
    got = [tiny.draw() for _ in range(200)]
    check("**200 次全部返回(没有卡死/抛异常)**",
          len(got) == 200, len(got))
    check("**没有一次返回空 keywords**",
          all(g["keywords"] for g in got),
          [g for g in got if not g["keywords"]][:2])
    check("每次都 word1 != word2",
          all(g["keywords"][0] != g["keywords"][1] for g in got),
          [g for g in got if g["keywords"][0] == g["keywords"][1]][:2])
    check("**两层都走到了(第二层兜底可达)**",
          any(g["relaxed"] == 2 for g in got),
          sorted({g["relaxed"] for g in got}))
    check("**触发过放宽**", tiny.relaxed_total > 0, tiny.relaxed_total)


def test_g4_keyword_order_is_deterministic_bytes():
    """§六: 词表顺序**必须确定** —— 否则同 seed 跨进程复现不了。

    ⚠️ 这条**必须**在**独立进程**里跑。同进程内建两个 bag, `list(set(...))`
    得到的顺序是一样的(PYTHONHASHSEED 相同), 所以**在进程内比较永远绿**
    —— 删掉 `sorted()` 也不会红(变异实测: 0 条失败)。跨进程 `set` 的迭代
    顺序不同, 这才是 `sorted()` 真正在挡的东西。
    """
    print("\n[K40] 词表顺序跨进程确定")
    words = ["山顶", "敲门", "电话", "老师", "下雨", "棺材",
             "高跟鞋", "死亡", "图书馆", "一百元", "三兄弟", "杀人"]
    code = (
        "import sys,json; sys.path.insert(0, %r);"
        "from story.keyword_seed import KeywordBag;"
        "b=KeywordBag(%r, 4242);"
        "print(json.dumps([b.draw()['keywords'] for _ in range(4)],"
        " ensure_ascii=False))" % (str(REPO), words))
    outs = []
    for _ in range(3):
        r = subprocess.run([sys.executable, "-X", "utf8", "-c", code],
                           cwd=str(REPO), capture_output=True)
        outs.append(r.stdout.decode("utf-8").replace("\r\n", "\n").strip())
    check("**3 个独立进程拿到同一序列**",
          len(set(outs)) == 1, outs)
    check("确实抽到了 4 组", outs[0].count("，") >= 3 or len(outs[0]) > 20,
          outs[0][:80])


def test_g4_bag_does_not_touch_global_random():
    """§六: bag 只能用自己的 Random —— 否则会改 live 出题的序列。"""
    print("\n[K32] bag 不碰全局 random")
    random.seed(4242)
    want = [random.random() for _ in range(6)]
    random.seed(4242)
    b = KeywordBag(["a", "b", "c", "d", "e"], 7)
    for _ in range(20):
        b.draw()
    got = [random.random() for _ in range(6)]
    check("**抽 20 次后全局 random 序列逐位不变**", got == want, (got, want))


def test_g4_session_seed_derivation():
    """§六: 从 quality_seed 派生; 同输入同输出, 且与其它用途不撞。"""
    print("\n[K33] session seed 派生")
    check("确定性", derive_session_seed(20260920) == derive_session_seed(20260920))
    check("换 quality_seed 会变",
          derive_session_seed(20260920) != derive_session_seed(20260921))
    check("换 session 会变",
          derive_session_seed(20260920, 0) != derive_session_seed(20260920, 1))
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


def test_g4_vocab_unavailable_is_explicit_not_bank():
    """§八-9: 词库缺失/空/损坏 -> 抛错(由调用方显式降级), **不回退词库**。"""
    print("\n[K34] 词库不可用必须显式")
    with tempfile.TemporaryDirectory() as d:
        for name, obj in (("missing.json", None),
                          ("empty.json", {"keywords": []}),
                          ("null.json", {"keywords": None}),
                          ("bad.json", {"keywords": "不是列表"}),
                          ("nokeys.json", {"foo": 1}),
                          ("allbad.json", {"keywords": [None, 123, "", "   ",
                                                        "我杀了他"]})):
            p = os.path.join(d, name)
            if obj is not None:
                _tmp_json(d, name, obj)
            try:
                load_vocabulary(p)
                check(f"{name}: 抛 CorpusError", False, "没有抛")
            except CorpusError:
                check(f"{name}: 抛 CorpusError", True)
        try:
            load_vocabulary(os.path.join(d, "nope-not-here.json"))
            check("路径不存在: 抛 CorpusError", False, "没有抛")
        except CorpusError:
            check("路径不存在: 抛 CorpusError", True)
        try:
            load_vocabulary("")
            check("路径为空: 抛 CorpusError", False, "没有抛")
        except CorpusError:
            check("路径为空: 抛 CorpusError", True)
        # ⚠️ 最要紧的一条: 降级路径**不得**碰 KEYWORD_BANK。按 AST 查引用。
        names = set()
        for node in ast.walk(ast.parse(io.open(
                os.path.join(Path(__file__).resolve().parents[1], "story",
                             "keyword_corpus.py"), encoding="utf-8").read())):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.alias):
                names.add(node.asname or node.name.split(".")[-1])
        check("**keyword_corpus.py 不引用 KEYWORD_BANK(AST)**",
              "KEYWORD_BANK" not in names,
              sorted(n for n in names if "KEYWORD" in n))


def test_g4_load_bag_from_file():
    """`load_bag` 读产物建 bag, 并给出 §六 要的日志元数据。"""
    print("\n[K35] load_bag + 日志行")
    with tempfile.TemporaryDirectory() as d:
        p = _tmp_json(d, "v.json", build_vocabulary(_FAKE_ROWS))
        bag, meta = load_bag(p, 20260920)
        check("meta 有 corpus_version",
              meta["corpus_version"] == CORPUS_VERSION, meta)
        check("meta 的 keyword_count 与词表一致",
              meta["keyword_count"] == len(bag.keywords), meta)
        check("meta 有 source", meta["source"] == "neurostellar/haiguitang",
              meta)
        check("meta 有 raw/valid 计数",
              "raw_token_count" in meta and "valid_token_count" in meta, meta)
        line = describe_bag(meta, 20260920)
        for token in ("session_seed=20260920",
                      "corpus_version=" + CORPUS_VERSION,
                      "keyword_count="):
            check(f"日志含 {token}", token in line, line)
        check("**meta 里没有 keywords**(上千个词不该进日志)",
              "keywords" not in meta, sorted(meta))


def test_g4_real_vocab_shape():
    """真产物的**形状**(若在)。文件不在时跳过 —— 不假装通过。"""
    print("\n[K36] 真词库产物形状")
    p = os.path.join(Path(__file__).resolve().parents[1],
                     "data", "keyword2_vocabulary.json")
    if not os.path.exists(p):
        print("  skip 真词库不在(未构建), 跳过形状检查")
        return
    d = load_vocabulary(p)
    check("corpus_version == " + CORPUS_VERSION,
          d["corpus_version"] == CORPUS_VERSION, d["corpus_version"])
    check("source == neurostellar/haiguitang",
          d["source"] == "neurostellar/haiguitang", d["source"])
    kws = d["keywords"]
    check("有词", len(kws) > 0, len(kws))
    check("每个都是 str", all(isinstance(w, str) for w in kws))
    check("没有重复词", len(set(kws)) == len(kws))
    check("**unique_token_count 与词表一致**",
          d["unique_token_count"] == len(kws),
          (d["unique_token_count"], len(kws)))
    check("raw >= valid >= unique",
          d["raw_token_count"] >= d["valid_token_count"]
          >= d["unique_token_count"],
          (d["raw_token_count"], d["valid_token_count"],
           d["unique_token_count"]))
    check("**没有任何词超过 6 字**",
          max(len(w) for w in kws) <= 6, max(len(w) for w in kws))
    check("产物里没有 pairs 字段", "pairs" not in d)
    bank_words = {w for ws in KEYWORD_BANK.values() for w in ws}
    only_bank = sum(1 for w in kws if w in bank_words)
    check("**绝大多数词不在人工词库里**",
          only_bank < len(kws) * 0.15, (only_bank, len(kws)))


def test_g4_real_vocab_gives_fresh_combinations():
    """**本轮验收核心**: 固定 seed 下抽的 pair, 绝大多数**从未在原始
    input 里作为同一组出现过**。

    ⚠️ 需要原始文件(`data_external/.../turtle.json`)才算得了比例。它不在时
    **跳过**(而不是假装通过) —— 本地构建机上有, CI 上没有。
    """
    print("\n[K37] 重新组合的比例(验收核心)")
    root = Path(__file__).resolve().parents[1]
    vp = os.path.join(root, "data", "keyword2_vocabulary.json")
    rp = os.path.join(root, "data_external", "haiguitang", "raw",
                      "turtle.json")
    if not (os.path.exists(vp) and os.path.exists(rp)):
        print("  skip 缺真词库或原始件, 跳过比例统计")
        return
    rows = json.load(io.open(rp, encoding="utf-8"))
    orig = set()
    for row in rows:
        toks = [t for t in split_input(row.get("input")) if t]
        for i in range(len(toks)):
            for j in range(i + 1, len(toks)):
                a, b = toks[i], toks[j]
                orig.add((a, b) if a <= b else (b, a))
    ss = derive_session_seed(20260920)
    bag, _ = load_bag(vp, ss)
    drawn = {tuple(sorted(bag.draw()["keywords"])) for _ in range(50)}
    fresh = [p for p in drawn if p not in orig]
    ratio = len(fresh) / len(drawn)
    print("    原始 input 出现过的 unordered pair: %d" % len(orig))
    print("    抽 50 组, 其中从未出现过的: %d (%.1f%%)"
          % (len(fresh), 100 * ratio))
    # 组合空间 65 万, 原始只覆盖 2165 种 —— 期望几乎 100% 是新的。
    # 门槛放 90% 而不是 100%: 偶发撞上一个真实组合是**正常**的(那些词
    # 本来就在同一个自然语义场里), 不构成回归。
    check("**>= 90% 的 pair 从未在原始 input 里出现过**",
          ratio >= 0.90, "%.1f%%" % (100 * ratio))


def test_g4_combos_helper():
    """§七 的组合数公式 N*(N-1)/2。"""
    print("\n[K38] 组合空间公式")
    check("combos(2) == 1", combos(2) == 1)
    check("combos(3) == 3", combos(3) == 3)
    check("combos(4) == 6", combos(4) == 6)
    check("combos(1144) == 653796", combos(1144) == 653796, combos(1144))
    check("combos(0) == 0", combos(0) == 0)


def test_g4_experiment_has_no_own_keyword_logic():
    """§九: 实验脚本不得维护第二份词库。"""
    print("\n[K39] 实验脚本不再有第二份关键词逻辑")
    root = Path(__file__).resolve().parents[1]
    src = io.open(root / "tools" / "experiment_keyword_riddles.py",
                  encoding="utf-8").read()
    check("不定义自己的 KEYWORD_BANK", "KEYWORD_BANK = {" not in src)
    check("从生产 import 词库/抽取", "from story.keyword_seed import" in src)
    check("**能从生产词库抽词**(--draw-corpus)",
          "--draw-corpus" in src and "load_bag" in src)
    for f in ("story/keyword_seed.py", "story/keyword_corpus.py",
              "story/prefetch.py"):
        mods = []
        for node in ast.walk(ast.parse(io.open(root / f,
                                               encoding="utf-8").read())):
            if isinstance(node, ast.Import):
                mods.extend(al.name for al in node.names)
            elif isinstance(node, ast.ImportFrom):
                mods.append(node.module or "")
        check(f"{f} 不 import 实验脚本(AST)",
              not any("experiment_keyword_riddles" in m for m in mods), mods)
        check(f"{f} 不顶层 import tools.*(AST)",
              not any(m == "tools" or m.startswith("tools.") for m in mods),
              mods)


# ======================================================================
# G4-D: seed 级 safety 窄修复(§D)
# ======================================================================
def test_g4d_shock_seeds_excluded():
    """以**冲击点本身**为卖点的词不进词库(§D)。"""
    print("\n[K41] G4-D: 冲击点词被挡")
    from story.keyword_corpus import (is_valid_keyword, reject_reason,
                                      _SHOCK_MARKERS)
    for w in ("碎尸", "分尸", "尸块", "砍手", "砍断", "截肢", "虐待",
              "割腕", "上吊", "性侵", "猥亵"):
        check(f"**{w} 被拒**", not is_valid_keyword(w), w)
        check(f"{w} 的类别是 shock",
              reject_reason(w) == "shock", reject_reason(w))
    check("词表非空(不是把整条判据写死了)", len(_SHOCK_MARKERS) > 0)


def test_g4d_ordinary_death_words_kept():
    """**反证**: 普通死亡/事故/悲剧词**必须保留**(§D 明令)。

    没有这条的话, "把整个词库清空" 也能让上面那条通过。
    这里逐个断言: 它们是海龟汤的**事实材料**, 不是冲击噱头。
    """
    print("\n[K42] G4-D: 普通死亡词不被一刀切")
    from story.keyword_corpus import is_valid_keyword, reject_reason
    for w in ("死亡", "尸体", "棺材", "凶杀", "杀人", "遗书", "祭奠",
              "手枪", "毒药", "埋葬", "精神病", "黑人抬棺", "砷中毒",
              "血迹", "爆炸", "打猎"):
        check(f"**{w} 仍然有效**", is_valid_keyword(w),
              reject_reason(w))


def test_g4d_two_filter_layers_are_separate():
    """两层的**类别**必须分得开 —— 否则报告里拆不出数量。

    `unsafe`(不能直播出现的字面)与 `shock`(以冲击点本身为卖点)是
    两个不同的理由。合成一个数字就再也拆不开了。
    """
    print("\n[K43] G4-D: unsafe 与 shock 是两个类别")
    from story.keyword_corpus import reject_reason
    check("强奸 -> unsafe", reject_reason("强奸") == "unsafe",
          reject_reason("强奸"))
    check("碎尸 -> shock", reject_reason("碎尸") == "shock",
          reject_reason("碎尸"))
    check("**两者不相等**", reject_reason("强奸") != reject_reason("碎尸"))


def test_g4d_reason_matches_validator():
    """`reject_reason` 与 `is_valid_keyword` 对**每个词**结论必须一致。

    两个函数是分开实现的(一个给报告, 一个给生产), 手工保持同步迟早会漂
    —— 漂了之后报告里写的"挡了多少"就是假的。这条测试用一份混合样本
    (含边界词)钉住它们。
    """
    print("\n[K44] G4-D: reason 与 validator 结论一致")
    from story.keyword_corpus import is_valid_keyword, reject_reason
    sample = ["死亡", "碎尸", "我", "图书馆", "很暗", "强奸", "手枪",
              "a", "110", "一姐妹母亲去世", "回家后却把姐姐杀了",
              "截肢", "遗书", "x" * 40, "带 空格", "砷中毒"]
    for w in sample:
        passed = is_valid_keyword(w)
        reason = reject_reason(w)
        check(f"**{w!r} 两边一致**(valid={passed})",
              passed == (reason == ""), (passed, reason))


def test_g4d_product_reports_reasons_not_words():
    """产物里只有**数量与类别**, 没有具体词(§D)。"""
    print("\n[K45] G4-D: 产物不带被挡的词")
    import json as _json
    import os as _os
    from story.keyword_corpus import DEFAULT_CORPUS_PATH, build_vocabulary
    # ① builder 的返回值只有计数
    d = build_vocabulary([{"input": "关键词：碎尸，死亡，我"}])
    check("有 rejected_by_reason", isinstance(
        d.get("rejected_by_reason"), dict), d.get("rejected_by_reason"))
    check("**shock 计了一次**", d["rejected_by_reason"].get("shock") == 1,
          d["rejected_by_reason"])
    check("**sentence 计了一次**",
          d["rejected_by_reason"].get("sentence") == 1,
          d["rejected_by_reason"])
    check("**被挡的词不在 keywords 里**", "碎尸" not in d["keywords"],
          d["keywords"])
    check("正常词在", "死亡" in d["keywords"], d["keywords"])
    check("**rejected 里没有任何词面**(只有类别->计数)",
          all(isinstance(v, int) for v in d["rejected_by_reason"].values())
          and all(k in ("empty", "length", "charset", "sentence",
                        "unsafe", "shock", "unknown")
                  for k in d["rejected_by_reason"]),
          d["rejected_by_reason"])
    # ② 盘上那份 checked-in 的产物同理
    if _os.path.exists(DEFAULT_CORPUS_PATH):
        raw = _json.load(open(DEFAULT_CORPUS_PATH, encoding="utf-8"))
        check("盘上产物有 rejected_by_reason",
              isinstance(raw.get("rejected_by_reason"), dict),
              raw.get("rejected_by_reason"))
        check("**盘上产物没有一个被挡的具体词**(只存了计数)",
              all(isinstance(v, int)
                  for v in raw["rejected_by_reason"].values()),
              raw["rejected_by_reason"])


def test_g4d_real_vocab_has_no_shock_seeds():
    """端到端: checked-in 词库里**一个冲击点词都没有**。"""
    print("\n[K46] G4-D: 真实词库无 shock seed")
    from story.keyword_corpus import (_SHOCK_MARKERS, DEFAULT_CORPUS_PATH,
                                      load_vocabulary)
    d = load_vocabulary(DEFAULT_CORPUS_PATH)
    hits = [w for w in d["keywords"]
            if any(s in w for s in _SHOCK_MARKERS)]
    check("**零命中**", not hits, hits[:10])
    check("但词库仍然够大(>1000)", len(d["keywords"]) > 1000,
          len(d["keywords"]))
    check("corpus_version 是 v2", d["corpus_version"] == "keyword2-vocab-v2",
          d["corpus_version"])


def test_i60_concurrent_draw_atomic():
    """Issue #60 §15: 多线程 draw —— index 单调唯一、1..N 完整、无异常。"""
    print("\n[K60-1] KeywordBag 并发 draw 原子化")
    import threading
    bag = KeywordBag([f"词{i}" for i in range(50)], session_seed=42)
    n_threads, per = 8, 50
    results = []
    errors = []

    def worker():
        try:
            for _ in range(per):
                results.append(bag.draw()["index"])
        except Exception as e:              # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("无异常/无状态损坏", not errors, errors[:3])
    check("draw 总数正确", len(results) == n_threads * per, len(results))
    check("**index 无重复**", len(set(results)) == len(results),
          len(results) - len(set(results)))
    check("**1..N 完整单调**",
          sorted(results) == list(range(1, n_threads * per + 1)), None)


def test_i60_concurrent_draw_sequential_equivalence():
    """§15: 同 seed 下, 并发串行化后的 draw 序列 == 单线程序列
    (锁只做串行化, 不改行为)。"""
    print("\n[K60-2] 并发 draw 与单线程同序")
    import threading
    words = [f"w{i}" for i in range(30)]
    solo = KeywordBag(words, session_seed=7)
    seq = [solo.draw()["keywords"] for _ in range(100)]
    # 并发: 4 线程但用屏障逐次同步 -> 每 draw 的相对顺序确定
    bag = KeywordBag(words, session_seed=7)
    bar = threading.Barrier(4)
    got = []

    def w():
        for _ in range(25):
            bar.wait()
            got.append(bag.draw()["keywords"])

    ts = [threading.Thread(target=w) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    check("barrier 串行化后同序列列一致", got == seq,
          None if got == seq else (got[:3], seq[:3]))


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
        # ---- G4: 独立词库 + 随机重新组合 ----
        test_g4_vocab_expands_every_row,
        test_g4_vocab_is_words_not_pairs,
        test_g4_vocab_counts_and_dedupe,
        test_g4_vocab_rejects_sentence_fragments,
        test_g4_vocab_extracts_only_input,
        test_g4_vocab_unsuitable_filtered,
        test_g4_bag_recombines_independently,
        test_g4_bag_reproducible_and_no_short_repeat,
        test_g4_bag_relaxes_instead_of_hanging,
        test_g4_keyword_order_is_deterministic_bytes,
        test_g4_bag_does_not_touch_global_random,
        test_g4_session_seed_derivation,
        test_g4_vocab_unavailable_is_explicit_not_bank,
        test_g4_load_bag_from_file,
        test_g4_real_vocab_shape,
        test_g4_real_vocab_gives_fresh_combinations,
        test_g4_combos_helper,
        test_g4_experiment_has_no_own_keyword_logic,
        # ---- G4-D: seed 级 safety 窄修复 ----
        test_g4d_shock_seeds_excluded,
        test_g4d_ordinary_death_words_kept,
        test_g4d_two_filter_layers_are_separate,
        test_g4d_reason_matches_validator,
        test_g4d_product_reports_reasons_not_words,
        test_g4d_real_vocab_has_no_shock_seeds,
        # ---- Issue #60 §15: KeywordBag 并发安全 ----
        test_i60_concurrent_draw_atomic,
        test_i60_concurrent_draw_sequential_equivalence,
    ]
    for t in tests:
        t()
    print()
    if FAIL[0]:
        print(f"FAIL: 关键词种子 有 {FAIL[0]} 条不通过")
        return 1
    print("PASS: 关键词种子(独立词库 + 随机组合 + 方向)全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
