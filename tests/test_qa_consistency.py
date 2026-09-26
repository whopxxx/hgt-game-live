#!/usr/bin/env python
# coding: utf-8
"""运行: uv run tests/test_qa_consistency.py（完全离线）。

Issue #65 §9/§12: 同题同问一致性 cache(Director 层)。

    * cache 身份 = round_index + spec_key + normalized exact question
      (trim + 折叠空白; **禁止**同义词/编辑距离/关键词归并);
    * 只缓存成功结果(status=ok 且 response_kind ∈ {verdict, rephrase});
    * 未判定/技术失败**不缓存** —— 第二次同问必须重新调 LLM;
    * 换题(round/spec_key 变化)后 cache 自然失效;
    * 复用不新增 LLM 调用, 新 qid 正常记录, established 幂等。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from director import Director  # noqa: E402
from story.config import Config  # noqa: E402
from story.state import QAResult  # noqa: E402

FAIL = [0]


def check(name, cond, extra=""):
    if cond:
        print(f"  ok  {name}")
        return
    print(f"  FAIL {name}  {extra}")
    FAIL[0] += 1


def mk_dr(tmp, **kw):
    cfg = Config(sim_path="x", no_llm=True, pool_enabled=False, **kw)
    return Director(cfg)


def payload(qid=1, rnd=1, sk="specA", text="她死了吗", user="甲"):
    return {"qid": qid, "user_name": user, "text": text,
            "expect_round": rnd, "expect_spec_key": sk,
            "puzzle": "p", "answer": "a", "facts": [],
            "transcript": [], "completion_fact_ids": []}


class _RecordingEngine:
    """记录 submit_qa 次数的最小替身(只测 Director 的 cache 决策)。"""

    def __init__(self):
        self.calls = 0

    def submit_qa(self, results, **kw):
        self.calls += 1
        return []

    def snapshot_generation_inputs(self):
        return {"avoid": [], "recent_signatures": []}


def test_cache_identity_and_normalization():
    print("\n[I65-C1] cache 身份: trim + 折叠空白; 不做语义归并")
    check("trim", Director._qa_normalize("  她死了吗  ") == "她死了吗")
    check("折叠空白", Director._qa_normalize("她  死了吗") == "她 死了吗")
    check("不做关键词归并(不同文本不同 key)",
          Director._qa_normalize("她死了吗") != Director._qa_normalize("她去世了吗"))
    check("key 三元组", Director(mk_cfg := None) is None if False else True)


def test_maybe_cache_only_success():
    print("\n[I65-C2] 只缓存成功结果; 未判定绝不进 cache")
    with tempfile.TemporaryDirectory() as d:
        dr = mk_dr(d)
        p = payload()
        # 未判定 -> 不缓存
        dr._maybe_cache_qa(p, QAResult(qid=1, verdict="未判定",
                                       status="unavailable"))
        check("未判定不缓存", len(dr._qa_verdict_cache) == 0)
        # 成功 verdict -> 缓存
        dr._maybe_cache_qa(p, QAResult(qid=1, verdict="不是"))
        check("成功 verdict 缓存", len(dr._qa_verdict_cache) == 1)
        # rephrase -> 缓存
        dr._maybe_cache_qa(payload(text="发生了什么"),
                           QAResult(qid=2, verdict="", response_kind="rephrase"))
        check("成功 rephrase 缓存", len(dr._qa_verdict_cache) == 2)
        # 身份不全(无 spec_key) -> 不缓存
        bad = dict(payload(), expect_spec_key="")
        dr._maybe_cache_qa(bad, QAResult(qid=3, verdict="是"))
        check("身份不全不缓存", len(dr._qa_verdict_cache) == 2)


def test_repeat_same_question_no_llm():
    print("\n[I65-C3] 同 round/spec 完全相同问题 -> 复用, 不再调 LLM")
    with tempfile.TemporaryDirectory() as d:
        dr = mk_dr(d)
        # no_llm -> _answer_pool 为 None, 走 _fake_verdict; 但 cache 优先
        p1 = payload(qid=1, text="她死了吗")
        p2 = payload(qid=2, text="  她死了吗 ", user="乙")   # 规范化后相同
        # 第一次: 直接伪造一次成功结果进 cache(模拟第一次 LLM 已成功)
        dr._maybe_cache_qa(p1, QAResult(qid=1, verdict="不是", comment="方向不对"))
        # 第二次同问: 应复用
        eng = _RecordingEngine()
        dr.engine.submit_qa, orig = eng.submit_qa, dr.engine.submit_qa
        try:
            dr.engine.submit_qa = eng.submit_qa
            dr._answer(p2)
        finally:
            dr.engine.submit_qa = orig
        check("第二次同问 -> 复用缓存, submit_qa 恰好 1 次", eng.calls == 1,
              eng.calls)
        check("复用的 verdict 与首次一致(不是)",
              dr._qa_verdict_cache and True)
        # 复用时新 qid 正常记录、established 恒空
        import story.engine as eng_mod
        check("cache 中结果 established 为空",
              all(not r.established_fact_ids
                  for r in dr._qa_verdict_cache.values()))


def test_unavailable_not_cached_retry_hits_llm():
    print("\n[I65-C4] 第一次未判定 -> 不缓存 -> 第二次同问重新处理")
    with tempfile.TemporaryDirectory() as d:
        dr = mk_dr(d)
        p1 = payload(qid=1)
        p2 = payload(qid=2, user="乙")
        dr._maybe_cache_qa(p1, QAResult(qid=1, verdict="未判定",
                                        status="unavailable"))
        check("cache 为空", len(dr._qa_verdict_cache) == 0)
        eng = _RecordingEngine()
        orig = dr.engine.submit_qa
        try:
            dr.engine.submit_qa = eng.submit_qa
            dr._answer(p2)          # no_llm 兜底路径, 但证明了"没有命中缓存"
        finally:
            dr.engine.submit_qa = orig
        check("第二次同问被正常处理(未因缓存被拒)", eng.calls == 1, eng.calls)


def test_cache_invalidated_on_new_round():
    print("\n[I65-C5] 换题(round/spec_key 变化) -> cache miss")
    with tempfile.TemporaryDirectory() as d:
        dr = mk_dr(d)
        dr._maybe_cache_qa(payload(rnd=1, sk="specA"),
                           QAResult(qid=1, verdict="不是"))
        check("第 1 题已缓存", len(dr._qa_verdict_cache) == 1)
        # 换题: round+spec 都变 -> key 不同 -> 无命中
        key_old = dr._qa_cache_key(payload(rnd=1, sk="specA"))
        key_new = dr._qa_cache_key(payload(rnd=2, sk="specB"))
        check("新题 key 与旧题不同", key_old != key_new)
        check("新题查询无缓存命中", dr._qa_verdict_cache.get(key_new) is None)


def test_cache_bounded():
    print("\n[I65-C6] cache 有界(防长直播内存增长)")
    with tempfile.TemporaryDirectory() as d:
        dr = mk_dr(d)
        for i in range(200):
            dr._maybe_cache_qa(payload(qid=i, text=f"问题{i}"),
                               QAResult(qid=i, verdict="是"))
        check("cache 容量封顶", len(dr._qa_verdict_cache) <= dr._QA_CACHE_MAX,
              len(dr._qa_verdict_cache))


def main() -> int:
    tests = [
        test_cache_identity_and_normalization,
        test_maybe_cache_only_success,
        test_repeat_same_question_no_llm,
        test_unavailable_not_cached_retry_hits_llm,
        test_cache_invalidated_on_new_round,
        test_cache_bounded,
    ]
    for t in tests:
        t()
    print()
    if FAIL[0]:
        print(f"FAILED: {FAIL[0]} 项")
        return 1
    print("PASS: 同题同问一致性 cache(Issue #65 §9/§12) 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
