#!/usr/bin/env python
# coding: utf-8
"""关键词种子语料(G3)—— 从 **haiguitang 原始 input** 里取 2-key 关键词对。

## 为什么不再用人工词库

G1 / G2 的关键词来自 `KEYWORD_BANK` —— 我们**手写**的 5 槽 x 20 词。那批
实验证明了"2-key 自由成题"这条路对, 但词库本身始终是我们的**先验**:

  * 5 个槽(person / place / action / object / state)是我们**人为**分的;
    "两个词必须来自不同槽"这条约束**不是** haiguitang 的 seed 分布。
  * 20 个词 x 5 槽的组合空间比看起来小得多, 而且词的**高频偏置**是我
    们挑词时带进去的。

G3 起关键词改用**真实数据**: `neurostellar/haiguitang` 每一条的 `input`
字段(原样形如 `关键词：山顶，敲门，死者`)。原数据负责提供**自然关键词
空间**, 我们不再自己造。

## 这个模块只读 `input`

任务书 §一: "只使用 haiguitang 的 input 作为 seed source。不读取对应谜题
来决定'这个 seed 好不好'。"

这不是洁癖 —— 一旦按 `output`(谜面/谜底)挑 seed, 我们就把**外部题库
已有的题**当成了"好 seed"的判据, 于是 keyword2 变相在抄外部题的选题
口味。seed 的质量判断只允许来自 seed 自己的形状(长度/字符/是否明显损坏)。

`load_pairs_from_raw` 因此**只**碰 `row["input"]`, 从不碰 `instruction` /
`system` / `output`。`tests/test_keyword_seed.py` 有一条测试专门守这一点:
把 `output` 换掉, 抽出来的 pair 必须**逐位不变**。

## 确定性清洗, 不调 LLM

清洗全部是**纯函数**(正则 / 长度 / 字符集), 没有任何模型调用:

  * 统一中英文逗号与**空白**(全角空格 / 制表符 / 不换行空格)
  * 去掉 `关键词：` 前缀
  * **只保留恰好 2 个有效关键词**的记录(§一)
  * 丢掉空串、明显损坏文本(全是标点/符号)、超长句、不适合当普通关键词的
  * 直播明显不适宜的 seed 做**确定性**过滤(敏感词表), 同样不调 LLM

## 版本

`KEYWORD_SEED_VERSION` 在 `story/keyword_seed.py`(运行时常量)。corpus 自己
的版本在产物文件里(`data/keyword2_seed_pairs.json` 的 `corpus_version`)。
两个号**分开**: corpus 换一份而 sampler 没变, 与 sampler 改了而 corpus
没换, 是两件不同的事, 复盘时要能分清。
"""

from __future__ import annotations

import json
import os
import re

#: corpus 产物的版本号。**只在 pair 集合或清洗规则变化时 bump** ——
#: 它在 `data/keyword2_seed_pairs.json` 里, 与代码版本解耦(那份 JSON 是
#: checked-in 的数据产物, 可能比代码旧)。
CORPUS_VERSION = "keyword2-seeds-v2"

#: 数据源标识。写进产物, 让"这份 pair 从哪来"永远可查。
CORPUS_SOURCE = "neurostellar/haiguitang"

#: 原始 input 里那个前缀。3729/3729 行都带它(已核对), 但解析时仍按
#: 可选处理 —— 将来换数据版本时不该因为少个前缀就整份解析失败。
_KEY_PREFIX = re.compile(r"^\s*关键词\s*[:：]\s*")

#: 所有见过的分隔符: 全角逗号 / 半角逗号 / 顿号 / 分号。原数据里
#: `，` 3625 次、`,` 777 次、`、` 348 次 —— 三种都真实存在, 必须都认。
_SEPARATORS = "，,、；;"

#: 关键词里**允许**的字符: 中日韩汉字 / 拉丁字母 / 数字。
#:
#: 这是"明显损坏文本"的判据: 原数据里有一小撮 input 是整段谜面误填进来的
#: (含句号/问号/换行), 还有夹杂 emoji 的。关键词是**短名词性片段**, 不该
#: 带句读。用**白名单**而不是黑名单 —— 黑名单永远漏。
_ALLOWED = re.compile(r"^[0-9A-Za-z㐀-䶿一-鿿]+$")

#: 单个关键词的长度上下限(按字符数)。
#:
#: 下界 1: `110` / `b` 这类在原数据里是真实存在的 seed。
#: 上界 12: 超过它的 input 基本都是"整句谜面误填"(原数据最长 17 字,
#: 形状是 `一位女士去鞋店里买了一双红色高跟鞋`) —— 那不是关键词。
MIN_KEYWORD_LEN = 1
MAX_KEYWORD_LEN = 12

#: 直播明显不适宜的 seed —— **确定性**黑名单, 不调 LLM。
#:
#: 只挡"直接说出口就不合适"的词。**刻意保持很短**: 长了就变成润色器,
#: 而 G1-A 的结论之一是"不要求悲剧" —— 暗色题材本身不是问题反而是
#: haiguitang 的常态, 我们只挡不适合直播念出来的那几类。
_UNSUITABLE = (
    "强奸", "轮奸", "做爱", "性交", "卖淫", "嫖娼", "乱伦", "兽交",
    "幼女", "幼童", "恋童", "自杀", "自慰", "毒品", "冰毒", "海洛因",
)


def normalize_pair(a: str, b: str) -> tuple:
    """把两个关键词归一成可比较的 pair。

    归一 = **只去掉纯格式差异**(首尾空白 + 内部的连续空白折叠成单个空格)。
    **不**做繁简转换、**不**做同义合并、**不**做大小写折叠 —— 那些会让
    "同一对"的判断带上语言知识, 而我们要的是"原样保留关键词文本, 去掉
    纯格式差异"(§二)。

    顺序无关: 先排序再返回元组, 于是 `(A,B)` 与 `(B,A)` 是同一个 pair。
    这是去重的前提 —— 原数据里两种顺序都出现过(`100块钱/图书馆` 与
    `图书馆/100块钱`)。
    """
    def _n(s: str) -> str:
        s = str(s or "")
        # 全角空格 / 不换行空格 / 制表符都算空白 —— 原数据里三种都有。
        s = s.replace("　", " ").replace("\xa0", " ").replace("\t", " ")
        return re.sub(r"\s+", " ", s).strip()
    return tuple(sorted((_n(a), _n(b))))


def split_input(raw) -> list:
    """把一条原始 `input` 拆成关键词列表。

    只做"拆"这一件事 —— 不做有效性判断(那是 `is_valid_keyword` 的事),
    这样"拆出来几个"与"其中几个能用"是两个可分别测试的量。
    """
    s = str(raw if raw is not None else "")
    s = _KEY_PREFIX.sub("", s)
    for ch in _SEPARATORS:
        s = s.replace(ch, "\n")
    # ⚠️ 空白也要能当分隔符。原数据里偶见用空格代替逗号的 input
    # (`关键词：a b`), 只按逗号拆的话它们会粘成一个词, 然后被
    # `is_valid_keyword` 的白名单拒掉 —— 于是 2-key 记录静默变成 1-key。
    # 全角空格 / 不换行空格 / 制表符一并算。
    s = s.replace("　", " ").replace("\xa0", " ").replace("\t", " ")
    s = re.sub(r"[ ]+", "\n", s)
    return [p for p in (x.strip() for x in s.split("\n")) if p]


def is_valid_keyword(w: str) -> bool:
    """这个片段能不能当**普通关键词**。

    判据全部是**形状**上的, 与它对应的谜面/谜底**无关**(§一):

      * 长度在 `[MIN_KEYWORD_LEN, MAX_KEYWORD_LEN]`
      * 只含汉字/字母/数字(白名单)
      * 不含敏感词

    ⚠️ 这里**不**检查"这个词好不好出题" —— 那需要读谜面, 而任务书
    明确禁止(`不读取对应谜题来决定'这个 seed 好不好'`)。
    """
    w = str(w or "").strip()
    if not w:
        return False
    if not (MIN_KEYWORD_LEN <= len(w) <= MAX_KEYWORD_LEN):
        return False
    if not _ALLOWED.match(w):
        return False
    low = w.lower()
    for bad in _UNSUITABLE:
        if bad in low:
            return False
    return True


def pairs_from_rows(rows) -> dict:
    """从**原始行**里抽出 2-key pair 集合。返回一份可审计的统计 + pair 表。

    返回:

        {
          "raw_rows":        int,   # 输入行数
          "two_key_rows":    int,   # 恰好 2 个有效关键词的记录数
          "unique_pairs":    int,   # 归一后去重的 pair 数
          "pairs":           [[a,b], ...],   # 已排序, 顺序确定
        }

    ## 只有 2 个有效关键词的记录才算(§一)

    原数据的 input 有 0~8 个关键词(实测 1 个 816 行、2 个 1005 行、
    3 个 1368 行)。本轮**只要 2-key** —— 那是 G1-B 选定的形状。3-key 的
    记录**不是**"取前两个", 那会造出原数据里不存在的 pair。

    ## 不按出现频率重复存(§二)

    `pairs` 是 `unique` 的 —— 同一条 pair 在原数据里出现多少次都只留一份。
    原因(任务书原话): "原数据负责提供自然关键词空间, 我们不继承它的高频
    词偏置"。存成有权重的表, 采样时就会偏向原数据里被反复生成的组合。

    `two_key_rows` 与 `unique_pairs` 的差值是**信息**, 所以两个都记。
    """
    rows = list(rows or ())
    two: list = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        kws = [w for w in split_input(row.get("input")) if is_valid_keyword(w)]
        if len(kws) != 2:
            continue
        two.append(normalize_pair(kws[0], kws[1]))
    uniq = sorted(set(two))
    return {
        "raw_rows": len(rows),
        "two_key_rows": len(two),
        "unique_pairs": len(uniq),
        "pairs": [[a, b] for a, b in uniq],
    }


def build_corpus(rows) -> dict:
    """`pairs_from_rows` + 产物头(版本 / 来源 / 统计)。"""
    stats = pairs_from_rows(rows)
    return {
        "corpus_version": CORPUS_VERSION,
        "source": CORPUS_SOURCE,
        "raw_rows": stats["raw_rows"],
        "two_key_rows": stats["two_key_rows"],
        "unique_pairs": stats["unique_pairs"],
        "pairs": stats["pairs"],
    }


def load_corpus(path: str) -> dict:
    """读 corpus 产物。**任何**问题都抛 `CorpusError`, 由调用方决定降级。

    ## 为什么抛而不是返回 None

    调用方(`PoolPrefetcher`)必须**显式**处理"corpus 不可用"这件事, 因为
    它的降级动作是"整条 keyword2 链让位给 classic Blueprint 链"。用一个
    静默的 None 会让人漏判, 于是生产**看起来**在跑 keyword2, 而实际拿到
    的是空表 —— 那正是任务书 §五 点名要防的形状。
    """
    if not path:
        raise CorpusError("corpus 路径为空")
    if not os.path.exists(path):
        raise CorpusError("corpus 文件不存在: %s" % path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception as e:                       # noqa: BLE001
        raise CorpusError("corpus 解析失败: %s: %s" % (type(e).__name__, e))
    if not isinstance(d, dict):
        raise CorpusError("corpus 顶层不是对象: %s" % type(d).__name__)
    pairs = d.get("pairs")
    if not isinstance(pairs, list) or not pairs:
        raise CorpusError("corpus 没有 pair(空表)")
    clean: list = []
    for p in pairs:
        # ⚠️ 三道检查, 缺一不可。
        #
        # (1) 形状: 必须是长度为 2 的序列 —— `[]` / `"abc"` / `["a"]` 挡掉。
        # (2) **类型**: 两个元素都必须是 **str**。
        #     这一步是必须的, 不能靠 `is_valid_keyword`: `None` 经 `str()`
        #     会变成字面量 `"None"`, 而 `"None"` 是 4 个拉丁字母 —— **形状
        #     完全合法**! 只查形状的话, 一份 `[[null, null]]` 的损坏产物会被
        #     静默接受, 而模型拿到的关键词就是 `None` / `None`
        #     (它会照写出一个关于"无"的谜题, 而不是报错)。
        #     (这个坑是 `test_g3_corpus_missing_is_explicit_not_bank`
        #      抓出来的 —— 第一版正是只查了形状。)
        # (3) 内容: 过 `is_valid_keyword`(长度/白名单/敏感词)。
        if not (isinstance(p, (list, tuple)) and len(p) == 2):
            continue
        a, b = p[0], p[1]
        if not (isinstance(a, str) and isinstance(b, str)):
            continue
        a, b = a.strip(), b.strip()
        if not (is_valid_keyword(a) and is_valid_keyword(b)):
            continue
        clean.append((a, b))
    if not clean:
        raise CorpusError("corpus 的 pair 全部无效")
    d["pairs"] = clean
    return d


class CorpusError(Exception):
    """corpus 不可用(缺失 / 空 / 解析失败 / 无有效 pair)。

    调用方拿到它必须走**显式降级**, 不得静默回退任何内置词表(§五)。
    """


#: 默认 corpus 路径(相对仓库根)。`tools/build_keyword_seed_corpus.py` 写它,
#: 生产读它。放在 `data/` 下并**进版本库** —— 它是构建产物但也是输入,
#: 每个部署都要有同一份, 否则"同 seed 同 pair 序列"不成立。
DEFAULT_CORPUS_PATH = os.path.join("data", "keyword2_seed_pairs.json")

#: 原始数据的默认路径(`data_external/` 是 .gitignore 的, 只在构建机上有)。
DEFAULT_RAW_PATH = os.path.join(
    "data_external", "haiguitang", "raw", "turtle.json")
