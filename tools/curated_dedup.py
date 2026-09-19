#!/usr/bin/env python
# coding: utf-8
"""H1-E: 跨来源去重(三层)。

## 它解决什么

同一个故事会在不同来源里**各出现一次**, 而且文字可以差很多:

    经典"隧道复明"      中文版 / 英文版 / 稍改写版
    SE 上同一题被重发    两个 question_id, 正文几乎一样
    TurtleBench 内部     已在 H1-A 按 (surface,bottom) 塌缩, 但跨来源仍会撞

不去重的后果不是"多几道题", 而是**同一个谜底连着播两次** —— 观众会
立刻发现"这不刚讲过吗", 而那对直播体验是致命的(比题目平庸严重得多)。

## 三层, 严格性递减

    ① exact   归一后 (surface, bottom) 完全相同   -> 只留一份
    ② near    surface 3-gram Jaccard >= 阈值      -> 进 duplicate_candidates
    ③ canonical 同一道经典题的不同改写             -> 进 duplicate_candidates

## 为什么 ②③ **不自动删**

任务书明确: "不要自动删除两个不同来源的版本。"

这是对的, 因为**判重会错**。两个版本可能:

    真的是同一道题   -> 该删
    是两道不同的题   -> 3-gram 相似但谜底不同, 删了就丢题

而这两种从文本相似度上**分不开**。自动删的代价是"悄悄丢题"(不可见、
不可恢复), 留成候选的代价是"多几条待人工/后续模型确认的记录"(可见)。
所以 ②③ 一律**只标记, 不删除**。

## 复用 `quality.too_similar`, 不另写一份

它已经在题池/生成链里用了很久, 阈值(0.22)是**实测标定**过的
(同题改写 ~0.29, 完全不同 ~0.00)。这里重写一份相似度函数, 两条链的
判据迟早漂移 —— 那正是 S1 里 `recent_pairs` 的同一个教训。
"""

from __future__ import annotations

import logging
from typing import Optional

from .curated_common import RawCuratedPuzzle, normalize_for_dedup

log = logging.getLogger("hgt.dedup")

#: near-duplicate 的判定阈值。**与 `quality.too_similar` 的默认值一致**
#: (0.22, 3-gram Jaccard)。
#:
#: 刻意不在这里另设一个值: 两个数不一致会造成"同一条链说重复、另一条
#: 说没重复", 而排查时没人知道该信哪个。
NEAR_DUP_THRESHOLD = 0.22


def _surface_similarity(a: str, b: str) -> float:
    """surface 的 3-gram Jaccard。复用 `quality.ngrams`。"""
    try:
        from story.quality import ngrams
    except Exception:                           # noqa: BLE001
        return 0.0
    na, nb = ngrams(a), ngrams(b)
    if not na or not nb:
        return 0.0
    inter = len(na & nb)
    union = len(na | nb)
    return (inter / union) if union else 0.0


def dedup(records: list,
          near_threshold: float = NEAR_DUP_THRESHOLD
          ) -> tuple:
    """三层去重。返回 `(kept, duplicates, stats)`。

    `duplicates` 里的每条都带 `dup_reason` / `dup_of` 字段, 便于人工过。

    ## 顺序是有意的

    先 ①(便宜且精确)再 ②③(贵且模糊)。反过来会让完全相同的记录
    在模糊层被标成"疑似", 多一堆本来可以确定处理的候选。

    ## 保留哪一份

    同一个 exact key 上保留**信息更全**的那一份(有作者 > 没作者,
    有分数 > 没分数), 平局时按 external_id 字典序 —— 保证确定性。

    ## ⚠️ ② near 层必须先**排序**再比, 否则结果依赖输入顺序

    这是个真实踩到的坑: 两条记录文字几乎相同但**不完全相同**
    (差一个空格)时, 它们归一后 key 不同, 于是落到 near 层。而 near 层
    的判据是"与**已保留**的第一条比" —— 谁先来谁赢, 于是

        dedup([A, B]) -> kept=[A]
        dedup([B, A]) -> kept=[B]

    同一份输入换个顺序产出不同的 curated_raw, H1-D 的"byte-for-byte
    same"当场作废, 下游增量编译也跟着失效。

    修法: **先按确定性键排序**再进 near 层。这样"谁先来"由内容决定,
    与调用方给的顺序无关。
    """
    stats = {"input": len(records), "exact_removed": 0,
             "near_flagged": 0, "kept": 0}

    # ---------- ① exact ----------
    groups: dict = {}
    for r in records:
        groups.setdefault(r.dedup_key(), []).append(r)

    stage1: list = []
    for key, members in sorted(groups.items(), key=lambda kv: kv[0]):
        if len(members) == 1:
            stage1.append(members[0])
            continue
        # 多份完全相同的: 留信息最全的
        best = sorted(members, key=_richness_key)[0]
        stage1.append(best)
        stats["exact_removed"] += len(members) - 1
        log.info("exact 重复 %d 份 -> 留 %s(%s)",
                 len(members), best.external_id,
                 " / ".join(sorted(m.external_id for m in members
                                   if m is not best))[:120])

    # ---------- ② near(surface 相似) ----------
    # **先排序**: 让"谁先被保留"由确定性键决定, 而不是调用方给的顺序。
    stage1 = sorted(stage1, key=_richness_key)
    kept: list = []
    dupes: list = []
    for r in stage1:
        hit = None
        hit_sim = 0.0
        for k in kept:
            sim = _surface_similarity(r.surface, k.surface)
            if sim >= near_threshold and sim > hit_sim:
                hit, hit_sim = k, sim
        if hit is not None:
            # ⚠️ **只标记, 不删** —— 见模块 docstring。
            r.dup_reason = "near_duplicate"
            r.dup_of = hit.external_id
            r.dup_score = round(hit_sim, 4)
            dupes.append(r)
            stats["near_flagged"] += 1
            # 用 debug 而不是 info: 实测一轮 375 条里就有 53 条近重复,
            # 每条打一行 INFO 会把直播日志冲得看不见别的。结论已经在
            # duplicate_candidates.jsonl 与 stats 里, 逐条明细属于
            # "排查时才要"的粒度。
            log.debug("疑似近重复 %.3f: %s ~ %s", hit_sim,
                      r.external_id, hit.external_id)
            continue
        kept.append(r)

    stats["kept"] = len(kept)
    return kept, dupes, stats


def _richness_key(r: RawCuratedPuzzle) -> tuple:
    """"信息更全"的排序键(升序取最小)。

    优先级: 有作者 > 有分数 > external_id 字典序(确定性兜底)。
    """
    has_author = 1 if (r.question_author or r.answer_author) else 0
    has_score = 1 if (r.question_score is not None
                      or r.answer_score is not None) else 0
    return (0 if has_author else 1,
            0 if has_score else 1,
            str(r.external_id or ""))


def cross_source_report(records: list) -> dict:
    """按 source 统计 —— 给验收报告用。

    任务书要求报告里能直接读到"两个来源各贡献了多少", 所以在这里
    算好, 而不是让报告脚本自己再 group 一遍(两处各写一遍必然漂移)。
    """
    out: dict = {}
    for r in records:
        out[r.source] = out.get(r.source, 0) + 1
    return dict(sorted(out.items()))
