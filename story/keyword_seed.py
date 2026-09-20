#!/usr/bin/env python
# coding: utf-8
"""关键词种子(G4)—— 从**独立词库**里随机抽 2 个词重新组合, 给起题用。

## 这份代码从哪来

G1-A / G1-B 实验(`tools/experiment_keyword_riddles.py`)已经验证过:
**随机 2 个普通生活关键词 -> AI 自由形成核心海龟汤 -> 再结构化**, 出来的题
比 "Blueprint 命题作文" 更接近外部题库的语感(谜面 median 33 字、单机关、
没有为了显得高级硬加的第二机关)。G1-B 的决策是**默认用 2-key**。

本轮把这件事接进生产, 所以抽词逻辑必须**住在这里**(story/), 而不是反过来
让生产去 import `tools/`。方向是**单向**的:

    story/keyword_seed.py  <-  tools/experiment_keyword_riddles.py

`tools/` 里的实验脚本现在 import 本模块。**绝不能反过来** —— 生产的运行
路径依赖一个实验脚本是荒唐的(那个脚本将来会被删/改/挪)。

## 词库为什么长这样(§二)

    * 至少分 person / place / action / object / state 五个槽
    * 用**普通生活词** —— 柴米油盐、邻里日常
    * **不要**专业术语 / 冷门设备 / 平台机制

任何需要背景知识才能产生联想的词(器材型号 / 行业术语 / 网络平台功能)都不收:
它们会让模型往"知识题"而不是"生活异常"上跑, 而那正是两阶段方案要排除的变量。

词库与槽位顺序**逐字**沿用 G1-A 已验证的那一份 —— 改一个词都会让
"同一 seed 抽到同一组词"这条性质失效, 而 G1-A/G1-B 的全部结论都建立在
那个抽取序列上。

## 两个入口, 别搞混

    draw_keyword_groups(seed, key_count)   实验用(人工词库): 一个 seed 抽 20 组
    load_bag(path, seed) -> KeywordBag     生产用(G4): 从**独立词库**里
                                           随机抽两个词**重新组合**

**生产的 rng 由调用方给**(`PoolPrefetcher` 自己那个独立 rng) —— 本模块
**不持有任何全局随机状态**。这条是硬要求: prefetch 的抽词绝不能让 live
出题序列跟着变, 否则 "同 seed 可复现" 会退化成 "同 seed + 同补池状态可复现",
复盘时说不清。
"""

from __future__ import annotations

import random

#: **关键词来源**的版本号。与 `KEYWORD_IDEA_PROMPT_VERSION` 分开 ——
#: 换 seed 来源与换 prompt 是两件事, 合成一个号会让复盘时分不清
#: "这题风格变了" 是因为换了词, 还是因为换了 prompt。
#:
#: `keyword2-v1`      = G1/G2 的人工 5x20 词库 `KEYWORD_BANK`。
#: `keyword2-seeds-v2` = G3: 真实 haiguitang input, 但以**原始 pair** 为
#:                       采样单位(已废弃 —— 那样会继承外部题库的搭配先验)。
#: `keyword2-vocab-v1` = G4: 真实 haiguitang input 提取**独立词库**, 运行
#:                       时随机抽两个词**重新组合**, 原始 pair 关系不保留。
#: `keyword2-vocab-v2` = G4-D: 上一版之上加了 seed 级 safety 过滤
#:                       (`_SHOCK_MARKERS`) —— 以严重伤害/重口暴力/性暴力/
#:                       自伤/毒品**本身作为冲击点**的词不再进词库。
#:                       ⚠️ sampler 一行没改, bump 的是**词表口径**。
#:                       与 `keyword_corpus.CORPUS_VERSION` 同步 bump:
#:                       两者是"代码侧口径"与"产物侧口径", 复盘时配对看。
KEYWORD_SEED_VERSION = "keyword2-vocab-v2"

#: v1 的人工词库**版本号**(不再是生产默认来源)。保留它是因为
#: `tools/experiment_keyword_riddles.py`(G1 实验)仍然按它抽词 ——
#: 那批历史数据要能对上号。
KEYWORD_BANK_VERSION = "keyword2-v1"

# ======================================================================
# 一、词库(**实验用**, 不再是生产来源)
# ======================================================================
#
# ⚠️ G3 起生产**不再**从这里抽词 —— 生产读 `data/keyword2_seed_pairs.json`
# (真实 haiguitang input, 见 `story/keyword_corpus.py`)。这份人工词库留着
# 只有一个理由: `tools/experiment_keyword_riddles.py` 的 G1-A/G1-B 历史
# 数据是按它抽的, 那批报告要能复现。
#
# **不得**作为生产默认数据源(任务书 §五)。corpus 缺失时生产走显式降级,
# 而不是"偷偷用回人工词"。
#
# 每个槽 20 个词。刻意都是"一眼就是日常场景"的短词。

KEYWORD_BANK: dict[str, list[str]] = {
    "person": [
        "老人", "小孩", "司机", "护士", "老师", "邻居", "新娘", "保安",
        "快递员", "房东", "乘客", "服务员", "父亲", "女儿", "兄弟",
        "同事", "陌生人", "理发师", "售货员", "同学",
    ],
    "place": [
        "出租车", "屋子", "电梯", "图书馆", "医院", "楼道", "阳台",
        "超市", "车站", "厨房", "教室", "地下室", "酒店", "天台",
        "公园", "浴室", "车库", "餐厅", "桥", "车站",
    ],
    "action": [
        "借书", "搬家", "拍照", "敲门", "排队", "结账", "打扫", "等人",
        "打电话", "开车", "回家", "睡觉", "洗澡", "吃饭", "寄信",
        "换衣服", "上楼", "退票", "点菜", "锁门",
    ],
    "object": [
        "钥匙", "雨伞", "行李箱", "信封", "钟表", "镜子", "梯子",
        "账单", "药瓶", "相册", "杯子", "剪刀", "手电筒", "毛巾",
        "绳子", "盒子", "日记本", "校服", "饭盒", "车票",
    ],
    "state": [
        "停电", "下雨", "发烧", "迟到", "失眠", "迷路", "停水",
        "搬家", "离婚", "失业", "怀孕", "喝醉", "打喷嚏", "忘带",
        "掉牙", "烫伤", "吵架", "迷路", "超重", "失眠",
    ],
}

#: 槽位顺序固定 —— 抽到什么槽位不影响"程序抽取"这件事, 但固定顺序
#: 让同一 seed 在任何机器上得到同一组词。
_SLOTS = ("person", "place", "action", "object", "state")


def _dedupe(bank: dict) -> dict:
    """槽位内去重并**保序**(语料里有重复词, 重复会抬高被抽中的概率)。"""
    out = {}
    for slot in _SLOTS:
        seen, keep = set(), []
        for w in bank[slot]:
            if w and w not in seen:
                seen.add(w)
                keep.append(w)
        out[slot] = keep
    return out


# ======================================================================
# 二、实验入口: 一个 seed 抽出 20 组(逐字保留 G1-A 行为)
# ======================================================================
def draw_keyword_groups(seed: int, key_count: int = 0) -> list:
    """按 seed 抽 20 组(10 组 x 2 + 10 组 x 3)。

    返回 `[{index, group, keywords, slots, seed, seed_used}, ...]` ——
    `keywords` 原样保留, 报告直接写它。

    ## 抽取方式

    `random.Random(seed)` 一个实例顺序抽: 先用**不重复**抽样的方式
    为 2 词组各取 2 个不同槽位, 再为 3 词组各取 3 个不同槽位。
    槽位不重复 -> 不会出现"老人 + 小孩"这种同槽位堆叠(那更像人工
    挑词, 不像自然的关键词提示)。

    `seed_used` 逐组记录(基 seed + 组号), 便于复现任何**单组**。

    ## `key_count`

    `0` = 两组都返回(默认, 与 G1-A 行为逐位一致)。
    `2` / `3` = **只**返回那一组。

    ⚠️ 这是**过滤**, 不是重新抽词。20 组的抽取序列**完全不变** ——
    所以 3-key 的第 11~15 组与 G1-A 里"如果跑下去会拿到的"那几组
    一模一样。重新设计抽取方式会让两批数据无法对比, 那正是 G1 实验
    最不该引入的变量。
    """
    if key_count not in (0, 2, 3):
        raise ValueError("key_count 只能是 0 / 2 / 3, 收到 %r" % (key_count,))
    bank = _dedupe(KEYWORD_BANK)
    groups: list = []
    idx = 0
    for n_keys in (2, 3):
        for _ in range(10):
            idx += 1
            seed_used = seed + idx
            rng = random.Random(seed_used)
            slots = rng.sample(_SLOTS, n_keys)
            words = [rng.choice(bank[s]) for s in slots]
            groups.append({
                "index": idx,
                "group": "2key" if n_keys == 2 else "3key",
                "n_keys": n_keys,
                "keywords": words,
                "slots": list(slots),
                "seed": seed,
                "seed_used": seed_used,
            })
    if key_count:
        groups = [g for g in groups if g["n_keys"] == key_count]
    return groups


def keywords_line(g: dict) -> str:
    """`关键词: X，Y`(全角逗号, 与外部题库的观感一致)。"""
    return "关键词：" + "，".join(g["keywords"])


# ======================================================================
# 三、生产入口: shuffled bag(§三 / §四)
# ======================================================================
#
# G2 的生产入口是 `rng.choice()` **有放回**地从人工词库里抽。G3 换成:
#
#     corpus(unique 2-key pairs) -> 独立 keyword RNG shuffle
#       -> 顺序消费
#       -> 一个 bag 用完之前同一个 pair 不重复
#       -> 用完后重新 shuffle 下一轮
#
# 为什么(任务书 §三 原话): "这样比'小人工词库 + choice'有更高的实际
# 组合熵, 也不会短时间连续撞同一 pair。"
#
# 有放回抽样的真实观感是**会重复**: 301 对里抽 20 次, 撞一次的概率约
# 50%(生日问题)。直播里连着两道题拿到同一对词是肉眼可见的尴尬。
#
# ⚠️ **不再要求两个词来自不同槽位**。那是 G1 实验人为加的结构 ——
# 真实 haiguitang 的 input 里 `三兄弟/杀人`、`下雨/棺材` 这种同语义场的
# 组合大量存在。G3 起以**真实 source pair 为准**, 不再叠一层我们的先验。


def derive_session_seed(base_seed, session: int = 0) -> int:
    """从 `quality_seed` 确定性地派生 keyword session seed(§四)。

    ## 为什么要有 session 这一维

    bag 是**有状态**的(消费到哪了)。一场直播从头开始跑, 同一个
    `quality_seed` 会得到同一个 pair 序列 —— 这是可复现性要的。但**同一场
    直播里**重启一次 prefetcher 不该把已经消费过的 pair 从头再放一遍
    (那会在重启点附近立刻重复)。给一个 `session` 号, 重启时 +1, 序列
    就往前走一段, 而"同 session 同序列"仍然成立。

    ⚠️ 与 `quality_seed` **同一把号**但不能共用: `director.py` 给 live 出题
    用 `quality_seed`, 给补池用 `quality_seed ^ 0x9E3779B9`。keyword 采样
    再走一层派生, 于是三者互不干扰 —— 这是"不要让 keyword RNG 改变 live
    generation RNG"的**结构性**保证(§四)。

    派生用的是 SplitMix64 风格的混合(常量取自 mmix / 黄金比), 不是
    `hash()` —— `hash()` 对 str 有 PYTHONHASHSEED 随机化, 跨进程不可复现,
    正好会毁掉这里要的东西。
    """
    if base_seed is None:
        raise ValueError("derive_session_seed 需要 base_seed(quality_seed)")
    x = (int(base_seed) ^ 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
    x = (x + (int(session) & 0xFFFFFFFFFFFFFFFF) * 0xBF58476D1CE4E5B9
         ) & 0xFFFFFFFFFFFFFFFF
    x ^= (x >> 30)
    x = (x * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
    x ^= (x >> 27)
    x = (x * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
    x ^= (x >> 31)
    return x


class KeywordBag:
    """从**独立词库**里随机抽两个词**重新组合**(§四)。

    ## 这一轮改了什么(G3 -> G4)

    G3 是"从 pair 表里不放回地发 pair" —— 抽到的两个词**永远**在原始数据
    里一起出现过。那等于把外部题库的**搭配先验**继承了下来, 只是打乱了
    顺序: `三兄弟/杀人` 会一直被一起抽到, 而 `三兄弟/高跟鞋` 永远抽不到。

    G4 改成:

        word1 = vocabulary 随机抽
        word2 = vocabulary 随机抽
        要求 word1 != word2
        **不保留**它们在原始数据里的搭配关系

    任务书 §四 原话: "不要保留它们在原始数据中的搭配关系。不要要求:
    不同 slot / 原来是同一 pair / 语义相关 / 人工 compatibility。
    **随机碰撞就是这个生成器的核心。** Stage A 负责把两个看似无关的词
    变成一道自然海龟汤。"

    1147 个词 -> 657,231 种 unordered pair, 而原始数据只覆盖其中 301 种。
    绝大多数抽到的是**从未一起出现过**的组合。

    ## 短期重复控制(§五)

    产品要求不是"不放回"(那会退化成另一种确定性), 而是:

      * 同一个 keyword 不要**连续高频**出现
      * 同一个 unordered pair 在**合理窗口**内不重复

    做法: 维护 `recent_keywords` / `recent_pairs` 两个滑动窗口, 抽样时避开。
    **不能陷入无限重抽** —— 达到 `max_tries` 后放宽 keyword cooldown
    (但仍尽量避开 pair), 因为抽词函数不允许有"抽不出来"的失败态。

    ## 可复现(§六)

    同一个词库 + 同一个 session seed => **同一个 pair 序列**。由
    `random.Random(seed)` + 确定的词表顺序保证(词表在构建时已排序)。

    ## 与全局 random 无关

    本类**只**用自己 new 出来的 `random.Random(seed)`, 从不碰 `random`
    模块的全局状态。否则补池的抽词会改变 live 出题的序列。
    """

    #: keyword cooldown 窗口 —— 最近多少个词不重复抽。
    #:
    #: 1147 个词的库里, 窗口 40 意味着"一个词平均每 29 道题才轮到一次";
    #: 撞上的概率很低, 但**偶尔撞上也完全正常**(随机碰撞的核心), 所以
    #: 这里只是个软约束, 不是硬保证。
    KEYWORD_COOLDOWN = 40

    #: pair cooldown 窗口 —— 最近多少道题的对子不重复。
    #:
    #: 657,231 种组合下一个 pair 每 657k 道题才该轮到一次, 所以窗口
    #: 取 200 已经远超"合理"的定义, 且不会造成重抽压力。
    PAIR_COOLDOWN = 200

    #: 单次抽样最多重试几次。达到上限就**放宽 keyword cooldown**
    #: (pair 仍尽量避开) —— 见模块文档"不能陷入无限重抽"。
    MAX_TRIES = 24

    def __init__(self, keywords, session_seed: int,
                 keyword_cooldown: "int | None" = None,
                 pair_cooldown: "int | None" = None):
        if not keywords:
            raise ValueError("KeywordBag 需要非空词表")
        # 去重 + 排序: 词表顺序**必须确定**, 否则同 seed 复现不了。
        self.keywords = sorted({str(w) for w in keywords if str(w).strip()})
        if len(self.keywords) < 2:
            raise ValueError("KeywordBag 至少需要 2 个词才能组合")
        self.session_seed = int(session_seed)
        self._rng = random.Random(self.session_seed)
        self._kc = (self.KEYWORD_COOLDOWN if keyword_cooldown is None
                    else max(0, int(keyword_cooldown)))
        self._pc = (self.PAIR_COOLDOWN if pair_cooldown is None
                    else max(0, int(pair_cooldown)))
        self._recent_kw: list = []
        self._recent_pair: list = []
        self.served = 0
        #: 诊断计数 —— 报告与日志要能看到重试真的发生在什么水平。
        self.retries_total = 0
        self.relaxed_total = 0

    # ---- 内部 ----
    def _pair_key(self, a: str, b: str) -> tuple:
        """unordered pair 的键 —— **排序**, 于是 (A,B) 与 (B,A) 同键。"""
        return (a, b) if a <= b else (b, a)

    # ---- 公开 ----
    def draw(self) -> dict:
        """抽**两个不同**的词并返回。返回 `{keywords, slots, index, relaxed}`。

        ## 重试与放宽的顺序(§五)

            1. 抽 word1 / word2(都避开 `recent_keywords`)
            2. `word1 == word2` -> 重抽
            3. pair 在 `recent_pairs` 里 -> 重抽
            4. 试满 `MAX_TRIES` -> **放宽 keyword cooldown**(只避 pair)
            5. 再试满 `MAX_TRIES` -> 完全放宽(只保证 `word1 != word2`)

        第 4/5 步是**必须有**的: 词库小(比如测试里的 3 个词)或窗口相对
        词库过大时, 严格规则会抽不出来。抽词函数没有"失败"这个返回态,
        所以必须能放宽。

        `slots` 恒为 `[]` —— 独立词库没有槽位概念(§四: "不要要求不同
        slot")。留着这个 key 只为调用方与日志的形状不变。
        """
        n = len(self.keywords)
        kc = min(self._kc, max(0, n - 1))
        pc = min(self._pc, max(0, n * (n - 1) // 2 - 1))
        kset = set(self._recent_kw[-kc:]) if kc else set()
        pset = set(self._recent_pair[-pc:]) if pc else set()

        relaxed = 0
        chosen = None
        for attempt in range(self.MAX_TRIES * 2):
            # 第二段(试满一轮之后)放宽 keyword cooldown。
            if attempt >= self.MAX_TRIES:
                relaxed = 1
                kset = set()
            a = self.keywords[self._rng.randrange(n)]
            if a in kset:
                self.retries_total += 1
                continue
            b = self.keywords[self._rng.randrange(n)]
            if b == a or (relaxed == 0 and b in kset):
                self.retries_total += 1
                continue
            if self._pair_key(a, b) in pset:
                self.retries_total += 1
                continue
            chosen = (a, b)
            break
        if chosen is None:
            # ---- 最后兜底: 只保证两个词不同 ----
            #
            # 走到这里说明窗口相对词库太大(极小词库的构造下会发生)。
            # **绝不能**返回空 —— 调用方没有处理"抽不出来"的地方。
            relaxed = 2
            a = self.keywords[self._rng.randrange(n)]
            b = a
            while b == a and n > 1:
                b = self.keywords[self._rng.randrange(n)]
            chosen = (a, b)

        a, b = chosen
        if relaxed:
            self.relaxed_total += 1
        self.served += 1
        self._recent_kw.append(a)
        self._recent_kw.append(b)
        self._recent_pair.append(self._pair_key(a, b))
        return {
            "keywords": [a, b],
            "slots": [],
            "index": self.served,
            "relaxed": relaxed,
        }



def load_bag(corpus_path: str, session_seed: int,
             keyword_cooldown: "int | None" = None,
             pair_cooldown: "int | None" = None) -> tuple:
    """读**词库**并建 bag。返回 `(bag, corpus_meta)`。

    `corpus_meta` 只带日志/溯源要用的字段, **不含**整张词表 —— 一千多个
    词不该进日志。

    ⚠️ 词库不可用时**抛 `CorpusError`**(从 `keyword_corpus` 透传)。
    调用方必须显式降级到 classic 链, **不得**回退 `KEYWORD_BANK`。
    """
    from .keyword_corpus import load_vocabulary
    d = load_vocabulary(corpus_path)
    words = d["keywords"]
    meta = {
        "corpus_version": str(d.get("corpus_version") or ""),
        "keyword_count": len(words),
        "source": str(d.get("source") or ""),
        "raw_token_count": int(d.get("raw_token_count") or 0),
        "valid_token_count": int(d.get("valid_token_count") or 0),
    }
    return (KeywordBag(words, session_seed, keyword_cooldown, pair_cooldown),
            meta)


def describe_bag(meta: dict, session_seed: int) -> str:
    """§六 要求的那行 INFO 日志的正文。

    形如: `keyword2 session_seed=123 corpus_version=keyword2-vocab-v2
    keyword_count=1147`。单独一个函数是为了让**测试直接断言这行日志**,
    而不是去正则匹配一段拼在别处的字符串。
    """
    return ("keyword2 session_seed=%s corpus_version=%s keyword_count=%s"
            % (session_seed, meta.get("corpus_version", ""),
               meta.get("keyword_count", 0)))


def combos(n: int) -> int:
    """`n` 个词的 unordered 2-key 组合数 = N*(N-1)/2(§七 报告要用)。"""
    n = max(0, int(n))
    return n * (n - 1) // 2


# ======================================================================
# 四、兼容: G2 的 `draw_two_keywords`(人工词库)
# ======================================================================
#
# ⚠️ **生产不再调用它**。保留它是为了:
#   * G1 实验入口(`draw_keyword_groups`)与它共用 `KEYWORD_BANK`;
#   * `tests/test_keyword_seed.py` 里 G2 那批性质测试(槽位不重复、
#     `used_pairs` 生效)仍然描述**历史行为**, 那些测试不该被删 ——
#     删了就看不出"G3 换掉了什么"。
#
# 任何**生产**代码路径引用本函数都应当被视为 bug: 它就是 §五 点名的
# "偷偷用人工词"。

def draw_two_keywords(rng: random.Random,
                      used_pairs: "set | None" = None) -> dict:
    """**G2 遗留 / 仅实验与历史测试用**: 抽 2 个来自不同槽位的人工词。

    ⚠️ 生产走 `KeywordBag`(真实 haiguitang corpus)。本函数的词来自
    `KEYWORD_BANK` —— 那是 G1/G2 的人工先验, G3 起不再是生产来源。

    ## 为什么是 2 个(不是 3 个)

    G1-B 的对照结论: 2-key 与 3-key 的 valid 持平(3/5), 但 3-key 的谜面
    中位长度几乎是两倍(64 vs 33 字), 且 5 道里 2 道出现"为塞第三个词硬造
    一层身份"(为"服务员"把图书馆改成咖啡馆 / 为"相册"加上失散认亲)。
    第 3 个词换来的是**背景复杂度**, 不是**故事自然度**。所以默认 2-key。

    ## 槽位不重复

    与 `draw_keyword_groups` 同一条理由: 同槽位堆叠("老人 + 小孩")看起来
    像人工挑词, 不像自然的关键词提示。

    ## `used_pairs` —— 让连着补的几道题不要总拿同两个槽

    传入已经用过的 `(slot_a, slot_b)` 集合(顺序无关, 内部会归一成排序后的
    元组)。**有解则避开**; 若五个槽的 10 种组合全都用过了(理论上可能,
    实践上不会 —— 一场直播补不了 10 道), 那就**回落到不限**, 而不是死循环。
    这一点是刻意的: 抽词函数绝不能有"抽不出来"的失败态, 调用方没有处理
    它的地方。

    ## rng 归属

    **必须**传调用方自己的 rng(`PoolPrefetcher._rng`)。本函数不 new 任何
    Random, 也不碰 `random` 模块的全局状态 —— 否则 prefetch 的抽词会改变
    live 出题的随机序列。
    """
    bank = _dedupe(KEYWORD_BANK)
    # ⚠️ 两边都必须**归一成有序元组**再比:
    #     pool 里的 pair 是按槽位顺序生成的 (`_SLOTS` 的先后),
    #     而 `used_pairs` 可能是调用方按任意顺序给来的 ("object,person")。
    # 早先这里只归一了 `used`, pool 侧保持槽位顺序, 于是
    # `('person','object') not in {('object','person')}` 恒为真 ——
    # `used_pairs` **静默失效**, 同一对槽会被反复抽到。
    # 这个 bug 是 `test_draw_two_keywords_avoids_used_pairs` 抓出来的。
    pairs = [tuple(sorted((a, b)))
             for i, a in enumerate(_SLOTS) for b in _SLOTS[i + 1:]]
    used = {tuple(sorted(p)) for p in (used_pairs or ()) if p}
    pool = [p for p in pairs if p not in used] or pairs
    slot_a, slot_b = pool[rng.randrange(len(pool))]
    return {
        "keywords": [rng.choice(bank[slot_a]), rng.choice(bank[slot_b])],
        "slots": [slot_a, slot_b],
    }
