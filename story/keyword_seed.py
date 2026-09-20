#!/usr/bin/env python
# coding: utf-8
"""关键词种子(G2)—— 从普通生活词库里抽 2 个词, 给 AI-original 起题用。

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

    draw_keyword_groups(seed, key_count)   实验用: 一个 seed 抽出 20 组,
                                           序列**完全可复现**(报告要原样列)
    draw_two_keywords(rng, ...)            生产用: 用调用方给的 rng 抽一组
                                           2-key, 可供同一场直播连续调用

**生产的 rng 由调用方给**(`PoolPrefetcher` 自己那个独立 rng) —— 本模块
**不持有任何全局随机状态**。这条是硬要求: prefetch 的抽词绝不能让 live
出题序列跟着变, 否则 "同 seed 可复现" 会退化成 "同 seed + 同补池状态可复现",
复盘时说不清。
"""

from __future__ import annotations

import random

#: 抽词逻辑的版本号。与 prompt 版本(`story.llm.KEYWORD_IDEA_PROMPT_VERSION`)
#: **分开**: 词库变动与 prompt 变动是两件事, 合成一个号会让复盘时分不清
#: "这题风格变了" 是因为换词还是因为换 prompt。
KEYWORD_SEED_VERSION = "keyword2-v1"

# ======================================================================
# 一、词库(§二)
# ======================================================================
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
# 三、生产入口: 用调用方的 rng 抽一组 2-key
# ======================================================================
def draw_two_keywords(rng: random.Random,
                      used_pairs: "set | None" = None) -> dict:
    """抽 **2 个来自不同槽位**的普通生活词。返回 `{keywords, slots}`。

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
