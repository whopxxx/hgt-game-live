#!/usr/bin/env python
# coding: utf-8
"""关键词**词库**(G4)—— 从 **haiguitang 原始 input** 里取独立关键词。

## 这一轮改了什么(G3 -> G4)

G3 把 **pair** 当采样单位: 读 input -> 只留恰好 2-key 的行 -> shuffle
那些 pair。**那不是产品要的随机方式。**

产品要的是:

    从 haiguitang input 提取**独立关键词词库**
    运行时随机抽两个不同关键词**重新组合**
    **原始 pair 关系不保留**

区别是本质的。G3 的做法下, 抽到的两个词永远**在原始数据里一起出现过**
—— 于是 `三兄弟/杀人` 会一直被一起抽到, 而 `三兄弟/高跟鞋` 永远不会。
那等于把外部题库的**搭配先验**继承了下来, 只是打乱了顺序。

G4 起:

    关键词：A，B，C   ->  词库 += {A, B, C}      (三个词各算一个)
    ...3729 行全部展开...
    运行时: word1 = 词库随机抽, word2 = 词库随机抽, word1 != word2
    **两个词的搭配关系是新的** —— 大概率从未在原始数据里出现过

任务书原话: "**随机碰撞就是这个生成器的核心。** Stage A 负责把两个看似
无关的词变成一道自然海龟汤。"

## 这个模块只读 `input`

一旦按 `output`(谜面/谜底)挑词, 就把**外部题库已有的题**当成了"好词"
的判据, 于是 keyword2 变相在抄外部题的选题口味。词的质量判断只允许来自
词自己的形状(长度/字符/是否像句子碎片)。

`build_vocabulary` 因此**只**碰 `row["input"]`, 从不碰 `instruction` /
`system` / `output`。`tests/test_keyword_seed.py` 有一条测试专门守它:
把 `output` 全换成垃圾, 词库必须**逐位不变**。

## 确定性清洗, 不调 LLM

清洗全部是**纯函数**(正则 / 长度 / 字符集 / 句子特征), 零模型调用。
规则**宁可 conservative** —— 任务书 §二: "明显完整句子、带完整事件描述
的长片段必须丢弃。不要只靠 `<=12 字`。"

## 版本

`KEYWORD_SEED_VERSION` 在 `story/keyword_seed.py`(运行时常量)。corpus 自己
的版本在产物文件里(`data/keyword2_vocabulary.json` 的 `corpus_version`)。
两个号**分开**: 换一份词库而 sampler 没变, 与 sampler 改了而词库没换,
是两件不同的事, 复盘时要能分清。
"""

from __future__ import annotations

import json
import os
import re

#: 产物的版本号。**只在词表或清洗规则变化时 bump** —— 它在 checked-in 的
#: JSON 里, 与代码版本解耦(那份 JSON 可能比代码旧)。
CORPUS_VERSION = "keyword2-vocab-v1"

#: 数据源标识。写进产物, 让"这份词从哪来"永远可查。
CORPUS_SOURCE = "neurostellar/haiguitang"

#: 原始 input 里那个前缀。3729/3729 行都带它(已核对), 但解析时仍按
#: 可选处理 —— 将来换数据版本时不该因为少个前缀就整份解析失败。
_KEY_PREFIX = re.compile(r"^\s*关键词\s*[:：]\s*")

#: 所有见过的分隔符: 全角逗号 / 半角逗号 / 顿号 / 分号。原数据里
#: `，` 3625 次、`,` 777 次、`、` 348 次 —— 三种都真实存在, 必须都认。
_SEPARATORS = "，,、；;"

#: 允许的字符: 中日韩汉字 / 拉丁字母 / 数字。
#:
#: 白名单而不是黑名单 —— 黑名单永远漏。大小写折叠**不**做(见 `_norm`)。
_ALLOWED = re.compile(r"^[0-9A-Za-z㐀-䶿一-鿿]+$")

# ======================================================================
# 一、长度与"像不像关键词"的确定性判据(§二)
# ======================================================================
#
# ⚠️ 这是本轮改得最狠的一处。G3 只查 `<= 12 字`, 结果词库里混进了大量
# **句子碎片**:
#
#     一姐妹母亲去世 / 回家后却把姐姐杀了 / 不久后我把大哥也杀了
#     我有两个哥哥 / 一名男子A请另一名男子B签上名字在纸上
#     五人同时到达目的地。加快脚步的四人被淋成了落汤鸡
#
# 它们是"整段谜面误填进 input"再被逗号切开的产物。`<=12` 挡不住它们
# —— 12 个字的**完整句子**有的是。所以 G4 加了**句子性**判据。

#: 词的长度上下限(**汉字当量**, 见 `_cjk_len`)。
#:
#: 上界 6 而不是 12: 普通关键词(人物称谓/地点/物品/动作/状态/日常概念)
#: 极少超过 6 个汉字。任务书 §二 给的示例区间是"2~5 个汉字可以"。
#: 取 6 留一点余量(如"快递员""地下室""图书馆"), 同时把 7 字以上的
#: 描述性片段挡在外面。
#:
#: 下界 1: 原数据里 `110` / `b` 是真实存在的 seed, 不该因为短就丢。
MIN_KEYWORD_LEN = 1
MAX_KEYWORD_LEN = 6

#: 标点 / 空白 / 符号的并集 —— 任何一处出现就说明这是**句子**而不是词。
#:
#: 关键词是名词性片段, 不带句读。`。！？…—～·"《》()` 全在这儿挡掉。
_BAD_CHARS = re.compile(
    r"[。！？!?…—–~～·、，,；;：:;\"'“”‘’《》〈〉()（）\[\]【】{}"
    r"\s\.\-_/\\|+=*&^%$#@`]")

#: **句子性**词素 —— 出现任一个就丢掉。
#:
#: 这些字在正常关键词里几乎不出现, 但在"误填的谜面"里到处都是:
#: 代词(`我/你/他/她`)带出人称叙述, `的/了/却/也/就` 是虚词,
#: `为什么/因为/所以` 是因果连词, `一个/一名/一位` 是量词短语的开头。
#:
#: ⚠️ 这份表**宁可 conservative**: 每一个都经过"这个词库里还有没有
#: 正常词含它"的检查。比如 `了` 会误伤"受不了"(已丢弃, 代价可接受),
#: 但 `的` 如果放进白名单会让"他的朋友"这类碎片全进来 —— 所以照收。
_SENTENCE_MARKERS = (
    # 人称代词 —— 带出叙述视角
    "我", "你", "他", "她", "它", "咱",
    # 虚词 —— 句子黏合剂
    "的", "了", "却", "也", "就", "还", "又", "都", "才", "而", "并",
    "被", "把", "让", "给", "向", "从", "对", "与", "和", "或",
    # 疑问 / 因果 / 转折
    "为什么", "怎么", "怎样", "如何", "因为", "所以", "但是", "可是",
    "于是", "然后", "后来", "结果", "原来", "其实", "竟然", "居然",
    # 量词短语开头(后面往往跟一长串)
    "一个", "一名", "一位", "一只", "一条", "一件", "一场", "一次",
    "一种", "一群", "一堆", "一些", "一样", "一副", "一双", "一张",
    # 时间/顺序连接
    "之后", "之前", "以后", "以前", "当天", "次日", "不久", "最后",
    # 判断 / 存在
    "是", "有", "在", "会", "能", "要", "想", "说", "问", "答",
    "发现", "觉得", "认为", "知道", "看到", "听到",
    # 数字串 + 量(原数据里 "五人同时到达目的地" 这类)
    "同时", "一起", "立刻", "马上", "突然", "终于", "已经", "正在",
    # 程度副词 —— 名词性片段里不出现, 但"描述状态的半句"里到处都是:
    # `屋内光线很暗` / `光线很暗` / `成绩很好`。
    #
    # ⚠️ 只收 `很`。实测 `太` 会误伤 `太空` 与 `四房姨太太`(真词),
    # 而 `很` 在整份词库里**没有**一个假阳性 —— 它命中的三条全是碎片。
    # 这就是"宁可 conservative"的落地方式: 加之前先查碰撞。
    "很",
)

#: 直播明显不适宜的词 —— **确定性**黑名单, 不调 LLM(§三)。
#:
#: 只挡"直接说出口就不合适"的。**刻意保持短**: 长了就变成润色器, 而
#: G1-A 的结论之一是"不要求悲剧" —— 暗色题材本身是 haiguitang 的常态。
#: 任务书 §三: "不要因为 corpus 来源是公开数据就直接全收。"
_UNSUITABLE = (
    "强奸", "轮奸", "做爱", "性交", "卖淫", "嫖娼", "乱伦", "兽交",
    "幼女", "幼童", "恋童", "自杀", "自残", "自慰", "毒品", "冰毒",
    "海洛因", "吸毒", "裸体", "脱衣", "月经", "阴茎", "阴道",
)


def _norm(s) -> str:
    """归一: 只去**纯格式差异**(首尾空白 + 内部连续空白折叠)。

    **不**做繁简转换、**不**做同义合并、**不**做大小写折叠 —— 那些会让
    "是不是同一个词"的判断带上语言知识, 而任务书 §一 要的是"同词只存
    一次"(纯字面去重)。

    ⚠️ **不**排序(那是 G3 的 pair 语义)。G4 里每个词是**独立**的。
    """
    s = str(s if s is not None else "")
    # 全角空格 / 不换行空格 / 制表符都算空白 —— 原数据里三种都有。
    s = s.replace("　", " ").replace("\xa0", " ").replace("\t", " ")
    return re.sub(r"\s+", " ", s).strip()


def _cjk_len(s: str) -> int:
    """长度按**汉字当量**算 —— 每个字符算 1。

    单独抽一个函数是因为 G3 用 `len()`, 而 `len()` 对含拉丁字母的词
    (`A` / `110` / `b`) 与汉字混着算时口径不清楚。这里明确: **按字符数**,
    与 `len()` 同义, 但集中在一处便于将来改成"汉字算 1、拉丁算 0.5"。
    """
    return len(s)


def split_input(raw) -> list:
    """把一条原始 `input` 拆成关键词片段列表。

    只做"拆"这一件事 —— 不做有效性判断(那是 `is_valid_keyword` 的事),
    这样"拆出来几个"与"其中几个能用"是两个可分别测试的量。

    ⚠️ 所有 input 都拆, **不分** 1-key / 2-key / 3-key(§一)。G3 只收
    恰好 2 个的那批, 等于丢掉了一半以上的词。
    """
    s = str(raw if raw is not None else "")
    s = _KEY_PREFIX.sub("", s)
    for ch in _SEPARATORS:
        s = s.replace(ch, "\n")
    # ⚠️ 空白也要能当分隔符。原数据里偶见用空格代替逗号的 input
    # (`关键词：a b`), 只按逗号拆的话它们会粘成一个词, 然后被白名单拒掉。
    s = s.replace("　", " ").replace("\xa0", " ").replace("\t", " ")
    s = re.sub(r"[ ]+", "\n", s)
    return [p for p in (x.strip() for x in s.split("\n")) if p]


def is_valid_keyword(w: str) -> bool:
    """这个片段能不能当**普通关键词**(§二)。

    判据全部是**形状**上的, 与它对应的谜面/谜底**无关**:

      1. 长度(汉字当量)在 `[MIN_KEYWORD_LEN, MAX_KEYWORD_LEN]`
      2. 只含汉字/字母/数字(白名单) —— 挡标点、emoji、混杂符号
      3. 不含**句子性词素**(代词/虚词/连词/量词短语/判断动词…)
      4. 不含不适宜词

    ⚠️ **不**检查"这个词好不好出题" —— 那需要读谜面, 而任务书禁止。

    ⚠️ 第 3 条是本轮新增的, 也是**最要紧**的一条。只靠长度会放进
    `一姐妹母亲去世`(7 字)这类完整事件描述; 加上句子性词素判据之后
    `我`/`的`/`了`/`一个`/`为什么` 一出现就丢, 它们一个都进不来。
    """
    w = _norm(w)
    if not w:
        return False
    # (1) 长度
    if not (MIN_KEYWORD_LEN <= _cjk_len(w) <= MAX_KEYWORD_LEN):
        return False
    # (2) 字符白名单
    if not _ALLOWED.match(w):
        return False
    # (2b) 标点/空白/符号 —— `_ALLOWED` 已经挡住了, 但显式再查一次,
    #      因为 `_ALLOWED` 是 `^...$` 全匹配, 将来若放宽成部分匹配
    #      这条仍然生效。
    if _BAD_CHARS.search(w):
        return False
    # (3) 句子性词素 —— 出现任一个就说明这是句子碎片, 不是词。
    for marker in _SENTENCE_MARKERS:
        if marker in w:
            return False
    # (4) 不适宜词
    low = w.lower()
    for bad in _UNSUITABLE:
        if bad in low:
            return False
    return True


def build_vocabulary(rows) -> dict:
    """从**原始行**抽出独立关键词词库。返回一份可审计的统计 + 词表。

    返回:

        {
          "corpus_version":   str,
          "source":           str,
          "raw_rows":         int,   # 输入行数
          "raw_token_count":  int,   # 拆出来的**全部**片段(未过滤)
          "valid_token_count":int,   # 通过 `is_valid_keyword` 的片段数
          "unique_token_count":int,  # 去重后的词数(len(keywords))
          "keywords":         [str], # 已排序, 顺序确定
        }

    ## 三个计数的含义(不要混)

        raw_token_count    3729 行拆出来的**所有**片段 —— 含句子碎片
        valid_token_count  其中形状合法的 —— 含**重复**
        unique_token_count 去重后的词数 == len(keywords)

    `raw - valid` 是**清洗丢掉的量**, `valid - unique` 是**重复的量**。
    两个差值都是信息, 所以三个都记。

    ## 不保留频率(§一)

    同词只存一次, **不带**它原来的出现次数。任务书原话: "不保留原始
    出现频率作为权重。" 存成有权重的表, 采样就会偏向原数据里被反复
    写到的词 —— 那是外部题库的高频偏置, 我们不继承。
    """
    rows = list(rows or ())
    raw_tokens: list = []
    valid: list = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for tok in split_input(row.get("input")):
            raw_tokens.append(tok)
            if is_valid_keyword(tok):
                valid.append(_norm(tok))
    uniq = sorted(set(valid))
    return {
        "corpus_version": CORPUS_VERSION,
        "source": CORPUS_SOURCE,
        "raw_rows": len(rows),
        "raw_token_count": len(raw_tokens),
        "valid_token_count": len(valid),
        "unique_token_count": len(uniq),
        "keywords": uniq,
    }


#: 旧名保留一个别名, 但语义**已经变了**(现在是词库不是 pair 表)。
#: 任何还在用它构造 pair 表的调用方都应当被视为 bug。
def build_corpus(rows) -> dict:
    return build_vocabulary(rows)


class CorpusError(Exception):
    """词库不可用(缺失 / 空 / 解析失败 / 无有效词)。

    调用方拿到它必须走**显式降级**, 不得静默回退任何内置词表(§八-9)。
    """


def load_vocabulary(path: str) -> dict:
    """读词库产物。**任何**问题都抛 `CorpusError`, 由调用方决定降级。

    ## 为什么抛而不是返回 None

    调用方的降级动作是"整条 keyword2 链让位给 classic Blueprint 链"。
    用一个静默的 None 会让人漏判, 于是生产**看起来**在跑 keyword2, 而
    实际拿到的是空表 —— 那正是要防的形状。
    """
    if not path:
        raise CorpusError("词库路径为空")
    if not os.path.exists(path):
        raise CorpusError("词库文件不存在: %s" % path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception as e:                       # noqa: BLE001
        raise CorpusError("词库解析失败: %s: %s" % (type(e).__name__, e))
    if not isinstance(d, dict):
        raise CorpusError("词库顶层不是对象: %s" % type(d).__name__)
    kws = d.get("keywords")
    if not isinstance(kws, list) or not kws:
        raise CorpusError("词库是空表")
    clean: list = []
    seen = set()
    for w in kws:
        # ⚠️ 类型必须显式查 —— `None` 经 `str()` 会变成字面量 `"None"`,
        # 而 `"None"` 是 4 个拉丁字母, **形状完全合法**。只查形状的话
        # 一份 `[null]` 的损坏产物会被静默接受, 模型拿到的关键词就是
        # `None`(它会照写一个关于"无"的谜题, 而不是报错)。
        if not isinstance(w, str):
            continue
        w = _norm(w)
        if not is_valid_keyword(w) or w in seen:
            continue
        seen.add(w)
        clean.append(w)
    if not clean:
        raise CorpusError("词库的词全部无效")
    d["keywords"] = clean
    d["unique_token_count"] = len(clean)
    return d


#: 默认产物路径(相对仓库根)。`tools/build_keyword_seed_corpus.py` 写它,
#: 生产读它。放在 `data/` 下并**进版本库** —— 它是构建产物但也是输入,
#: 每个部署都要有同一份, 否则"同 seed 同 pair 序列"不成立。
DEFAULT_CORPUS_PATH = os.path.join("data", "keyword2_vocabulary.json")

#: 原始数据的默认路径(`data_external/` 是 .gitignore 的, 只在构建机上有)。
DEFAULT_RAW_PATH = os.path.join(
    "data_external", "haiguitang", "raw", "turtle.json")
