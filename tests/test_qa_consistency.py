#!/usr/bin/env python
# coding: utf-8
"""运行: uv run tests/test_qa_consistency.py（完全离线）。

Issue #65 §9/§12 + PR #66 review B1/B2/B3: 同题同问一致性 cache(Director 层)。

    * cache 身份 = round_index + spec_key + normalized exact question
      (trim + 折叠空白; **禁止**同义词/编辑距离/关键词归并);
    * 只缓存**被 Engine 回执接受**的成功结果 —— "LLM 返回 ok" ≠ "业务
      提交成功": 迟到的 ok 结果被 Engine 丢弃后绝不能进 cache(B1);
    * 未判定/技术失败不缓存 —— 第二次同问必须重新调 LLM;
    * 换题(round/spec_key 变化)后 cache 自然失效;
    * 并发读写/淘汰收口进专用锁, 无异常且容量恒 <= 上限(B2);
    * cache hit 复用完整 semantic result + provenance 四字段(B3)。
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from director import Director  # noqa: E402
from story.config import Config  # noqa: E402
from story.state import ActionKind, QAResult  # noqa: E402

FAIL = [0]


def check(name, cond, extra=""):
    if cond:
        print(f"  ok  {name}")
        return
    print(f"  FAIL {name}  {extra}")
    FAIL[0] += 1


def mk_dr(**kw):
    cfg = Config(sim_path="x", no_llm=True, pool_enabled=False, **kw)
    return Director(cfg)


def payload(qid=1, rnd=1, sk="specA", text="她死了吗", user="甲"):
    return {"qid": qid, "user_name": user, "text": text,
            "expect_round": rnd, "expect_spec_key": sk,
            "puzzle": "p", "answer": "a", "facts": [],
            "transcript": [], "completion_fact_ids": []}


def _full_result(qid=1, verdict="不是", **kw):
    """带完整 provenance 的成功结果(模拟 writer.answer 的真实产物)。"""
    kw.setdefault("judging_prompt_version", "haiguitang-judging-v2")
    kw.setdefault("answer_prompt_version", "answer-v2")
    return QAResult(qid=qid, verdict=verdict, comment="方向不对",
                    status=kw.pop("status", "ok"),
                    response_kind=kw.pop("response_kind", "verdict"),
                    solution_candidate=kw.pop("solution_candidate", False),
                    touched_fact_ids=kw.pop("touched_fact_ids", ["f1"]),
                    established_fact_ids=kw.pop("established_fact_ids", []),
                    completion_verified_fact_ids=kw.pop(
                        "completion_verified_fact_ids", []),
                    **kw)


class _RecordingEngine:
    """记录 submit_qa 的最小替身。可配置回执行为。"""

    def __init__(self, accept=True):
        self.calls = 0
        self.accept = accept
        self.last_results = None
        self.last_receipt = None

    def submit_qa(self, results, receipt=None, **kw):
        self.calls += 1
        self.last_results = results
        self.last_receipt = receipt
        if receipt is not None:
            for r in (results or []):
                receipt[r.qid] = self.accept
        return []


def test_cache_identity_and_normalization():
    print("\n[I65-C1] cache 身份: trim + 折叠空白; 不做语义归并")
    check("trim", Director._qa_normalize("  她死了吗  ") == "她死了吗")
    check("折叠空白", Director._qa_normalize("她  死了吗") == "她 死了吗")
    check("不做关键词归并(不同文本不同 key)",
          Director._qa_normalize("她死了吗") != Director._qa_normalize("她去世了吗"))


def test_maybe_cache_only_success():
    print("\n[I65-C2] 只缓存成功结果; 未判定/身份不全绝不进 cache")
    with tempfile.TemporaryDirectory() as d:
        dr = mk_dr()
        p = payload()
        dr._maybe_cache_qa(p, QAResult(qid=1, verdict="未判定",
                                       status="unavailable"))
        check("未判定不缓存", len(dr._qa_verdict_cache) == 0)
        dr._maybe_cache_qa(p, _full_result())
        check("成功 verdict 缓存", len(dr._qa_verdict_cache) == 1)
        dr._maybe_cache_qa(payload(text="发生了什么"),
                           _full_result(qid=2, verdict="",
                                        response_kind="rephrase"))
        check("成功 rephrase 缓存", len(dr._qa_verdict_cache) == 2)
        bad = dict(payload(), expect_spec_key="")
        dr._maybe_cache_qa(bad, _full_result(qid=3, verdict="是"))
        check("身份不全不缓存", len(dr._qa_verdict_cache) == 2)


def test_repeat_same_question_no_llm():
    print("\n[I65-C3] 同 round/spec 完全相同问题 -> 复用, 不再调 LLM")
    with tempfile.TemporaryDirectory() as d:
        dr = mk_dr()
        p1 = payload(qid=1, text="她死了吗")
        p2 = payload(qid=2, text="  她死了吗 ", user="乙")   # 规范化后相同
        dr._maybe_cache_qa(p1, _full_result())
        eng = _RecordingEngine()
        orig = dr.engine.submit_qa
        try:
            dr.engine.submit_qa = eng.submit_qa
            dr._answer(p2)
        finally:
            dr.engine.submit_qa = orig
        check("第二次同问 -> 复用缓存, submit_qa 恰好 1 次", eng.calls == 1,
              eng.calls)
        check("复用的 verdict 与首次一致(不是)",
              eng.last_results and eng.last_results[0].verdict == "不是")
        # B3: 复用带完整 provenance
        r = eng.last_results[0]
        check("B3: 复用保留 judging provenance",
              r.judging_prompt_version == "haiguitang-judging-v2",
              r.judging_prompt_version)
        check("B3: 复用保留 answer provenance",
              r.answer_prompt_version == "answer-v2",
              r.answer_prompt_version)
        check("B3: 复用保留 touched/solution_candidate",
              r.touched_fact_ids == ["f1"] and r.solution_candidate is False)
        check("复用后 cache 不变(1 条)", len(dr._qa_verdict_cache) == 1)


def test_unavailable_not_cached_retry_hits_llm():
    print("\n[I65-C4] 第一次未判定 -> 不缓存 -> 第二次同问重新处理")
    with tempfile.TemporaryDirectory() as d:
        dr = mk_dr()
        p1 = payload(qid=1)
        p2 = payload(qid=2, user="乙")
        dr._maybe_cache_qa(p1, QAResult(qid=1, verdict="未判定",
                                        status="unavailable"))
        check("cache 为空", len(dr._qa_verdict_cache) == 0)
        eng = _RecordingEngine()
        orig = dr.engine.submit_qa
        try:
            dr.engine.submit_qa = eng.submit_qa
            dr._answer(p2)
        finally:
            dr.engine.submit_qa = orig
        check("第二次同问被正常处理(未因缓存被拒)", eng.calls == 1, eng.calls)


def test_late_ok_result_not_cached_after_timeout():
    """review B1 核心: qid 超时 -> Engine 已回"未判定"并清 inflight ->
    迟到的 status=ok 结果被 Engine 丢弃 -> **cache 必须仍为空** ->
    第二次同问必须重新走 LLM。"""
    print("\n[I65-B1] 迟到 ok 结果被 Engine 丢弃 -> 不污染 cache")
    with tempfile.TemporaryDirectory() as d:
        dr = mk_dr()
        # Engine 替身: 模拟"迟到" —— 回执 False(该 qid 已不在途)
        eng = _RecordingEngine(accept=False)
        orig = dr.engine.submit_qa
        try:
            dr.engine.submit_qa = eng.submit_qa
            # 走 no_llm 的 _answer 兜底路径(它同样遵守回执纪律)
            dr._answer(payload(qid=9))
        finally:
            dr.engine.submit_qa = orig
        check("迟到结果 cache 仍为空", len(dr._qa_verdict_cache) == 0,
              dict(dr._qa_verdict_cache))
        # 第二次同问: 因为 cache 空, 必须重新提交(没有吃到毒缓存)
        eng2 = _RecordingEngine(accept=True)
        try:
            dr.engine.submit_qa = eng2.submit_qa
            dr._answer(payload(qid=10, user="乙"))
        finally:
            dr.engine.submit_qa = orig
        check("第二次同问重新提交(0 缓存命中)", eng2.calls == 1, eng2.calls)


def test_cached_only_after_engine_accept():
    print("\n[I65-B1b] Engine 接受(回执 True) -> 才进 cache")
    with tempfile.TemporaryDirectory() as d:
        dr = mk_dr()
        eng = _RecordingEngine(accept=True)
        orig = dr.engine.submit_qa
        try:
            dr.engine.submit_qa = eng.submit_qa
            dr._answer(payload(qid=1))
        finally:
            dr.engine.submit_qa = orig
        check("接受的结果进了 cache", len(dr._qa_verdict_cache) == 1)
        # 且 cache 里的是 Engine 接受的那条(不是另一份)
        (k, r), = dr._qa_verdict_cache.items()
        check("cache 内容与提交结果一致", r.verdict == "是", r.verdict)


def test_concurrent_cache_write_evict():
    """review B2: 多 worker 并发写 + 淘汰 —— 无异常, 容量恒 <= 上限。"""
    print("\n[I65-B2] 并发写入/淘汰: 无 KeyError, 容量 <= 64")
    with tempfile.TemporaryDirectory() as d:
        dr = mk_dr()
        errors: list = []

        def worker(wid):
            try:
                for i in range(200):
                    dr._maybe_cache_qa(
                        payload(qid=i, text=f"w{wid}问题{i}"),
                        _full_result(qid=i))
            except Exception as e:              # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(w,))
                   for w in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        check("并发读写无异常", not errors, errors[:3])
        check("容量恒 <= 上限", len(dr._qa_verdict_cache) <= dr._QA_CACHE_MAX,
              len(dr._qa_verdict_cache))


def test_cache_invalidated_on_new_round():
    print("\n[I65-C5] 换题(round/spec_key 变化) -> cache miss")
    with tempfile.TemporaryDirectory() as d:
        dr = mk_dr()
        dr._maybe_cache_qa(payload(rnd=1, sk="specA"), _full_result())
        check("第 1 题已缓存", len(dr._qa_verdict_cache) == 1)
        key_old = dr._qa_cache_key(payload(rnd=1, sk="specA"))
        key_new = dr._qa_cache_key(payload(rnd=2, sk="specB"))
        check("新题 key 与旧题不同", key_old != key_new)
        check("新题查询无缓存命中", dr._qa_verdict_cache.get(key_new) is None)


def test_cache_bounded():
    print("\n[I65-C6] cache 有界(防长直播内存增长)")
    with tempfile.TemporaryDirectory() as d:
        dr = mk_dr()
        for i in range(200):
            dr._maybe_cache_qa(payload(qid=i, text=f"问题{i}"),
                               _full_result(qid=i))
        check("cache 容量封顶", len(dr._qa_verdict_cache) <= dr._QA_CACHE_MAX,
              len(dr._qa_verdict_cache))


def _boot_v5():
    """起一个带通关合同的引擎(复用 test_engine 的夹具)。"""
    from tests.test_engine import boot_v5
    return boot_v5()


def test_engine_receipt_accepted_and_rejected():
    """review B1: `submit_qa(receipt=)` 的接受回执。

        正常在途 + QA 相位 -> receipt[qid]=True(rec 已落账);
        迟到(qid 已被超时清走) -> receipt[qid]=False;
        换题后提交(身份不符) -> 全部 False。
    """
    print("\n[I65-B1-engine] submit_qa 回执: 接受/迟到/换题")
    eng, clk, sp = _boot_v5()
    # 正常提问 -> 在途
    eng.submit_danmaku("u1", "甲", "#问题一")
    clk.advance(20.0)
    acts = [a for a in eng.tick() if a.kind == ActionKind.ANSWER]
    p = acts[0].payload
    receipt: dict = {}
    eng.submit_qa([QAResult(qid=p["qid"], verdict="不是")],
                  expect_round=p["expect_round"],
                  expect_spec_key=p["expect_spec_key"],
                  receipt=receipt)
    check("在途正常回包 -> 回执 True", receipt.get(p["qid"]) is True, receipt)

    # 迟到: 同样的 qid 再交一次(inflight 已被上一次 pop 掉)
    receipt2: dict = {}
    eng.submit_qa([QAResult(qid=p["qid"], verdict="不是")],
                  expect_round=p["expect_round"],
                  expect_spec_key=p["expect_spec_key"],
                  receipt=receipt2)
    check("迟到回包(qid 不在途) -> 回执 False", receipt2.get(p["qid"]) is False,
          receipt2)

    # 超时路径: 观众已经看到"未判定", inflight 被清; 此时迟到的 ok 结果
    # 必须 False —— 这正是"不能进 cache"的权威信号。
    eng2, clk2, sp2 = _boot_v5()
    eng2.submit_danmaku("u1", "甲", "#问题一")
    clk2.advance(20.0)
    acts2 = [a for a in eng2.tick() if a.kind == ActionKind.ANSWER]
    p2 = acts2[0].payload
    # 模拟超时: 从 inflight 清掉(qa_inflight_timeout 到期后的状态)
    with eng2._lock:
        eng2._inflight.pop(p2["qid"], None)
        eng2._inflight_at.pop(p2["qid"], None)
    receipt3: dict = {}
    eng2.submit_qa([QAResult(qid=p2["qid"], verdict="不是", status="ok")],
                   expect_round=p2["expect_round"],
                   expect_spec_key=p2["expect_spec_key"],
                   receipt=receipt3)
    check("超时后迟到的 ok 结果 -> 回执 False(不进 cache 的信号)",
          receipt3.get(p2["qid"]) is False, receipt3)

    # 换题: 身份不符 -> 整包作废 -> False
    receipt4: dict = {}
    eng2.submit_qa([QAResult(qid=999, verdict="是")],
                   expect_round=p2["expect_round"] + 50,
                   expect_spec_key="other-spec",
                   receipt=receipt4)
    check("换题后整包作废 -> 回执 False",
          receipt4.get(999) is False, receipt4)

    # 不传 receipt 时行为完全不变(向后兼容)
    acts5 = [a for a in eng2.submit_danmaku("u2", "乙", "#问题二")
             if a.kind == ActionKind.ANSWER] or \
            [a for a in eng2.tick() if a.kind == ActionKind.ANSWER]
    if acts5:
        p5 = acts5[0].payload
        acts_no_receipt = eng2.submit_qa(
            [QAResult(qid=p5["qid"], verdict="不是")],
            expect_round=p5["expect_round"],
            expect_spec_key=p5["expect_spec_key"])
        check("不传 receipt: 旧行为不变(动作正常返回)", bool(acts_no_receipt))


def test_cache_hit_archive_provenance_consistent():
    """review B3: 首次与 cache hit 两条记录的 prompt provenance 一致,
    语义字段不漂(archive 口径)。"""
    print("\n[I65-B3-archive] 首次 vs cache hit: provenance/语义不漂")
    with tempfile.TemporaryDirectory() as d:
        dr = mk_dr()
        # Engine 替身: 接受回包并落一条 QARec(带 provenance)
        class _ArchEngine(_RecordingEngine):
            def submit_qa(self, results, receipt=None, **kw):
                super().submit_qa(results, receipt=receipt, **kw)
                if receipt is not None:
                    for r in (results or []):
                        receipt[r.qid] = True
                return []

        first = _full_result(qid=1)
        dr._maybe_cache_qa(payload(qid=1), first)
        # 模拟第二次(cache hit 路径)产生的 QAResult —— 直接用 _answer 的
        # 复用分支: 拦截 submit_qa 捕获它实际提交的结果
        eng = _ArchEngine()
        orig = dr.engine.submit_qa
        try:
            dr.engine.submit_qa = eng.submit_qa
            dr._answer(payload(qid=2, text="  她死了吗 ", user="乙"))
        finally:
            dr.engine.submit_qa = orig
        r_first, r_second = first, eng.last_results[0]
        check("provenance: judging 一致",
              r_second.judging_prompt_version
              == r_first.judging_prompt_version
              == "haiguitang-judging-v2")
        check("provenance: answer 一致",
              r_second.answer_prompt_version == r_first.answer_prompt_version)
        check("provenance: candidate_recheck 一致",
              r_second.candidate_recheck_prompt_version
              == r_first.candidate_recheck_prompt_version)
        check("provenance: completion_verify 一致",
              r_second.completion_verify_prompt_version
              == r_first.completion_verify_prompt_version)
        check("语义: verdict/comment 一致",
              r_second.verdict == r_first.verdict
              and r_second.comment == r_first.comment)
        check("语义: response_kind/status/solution_candidate 一致",
              r_second.response_kind == r_first.response_kind
              and r_second.status == r_first.status
              and r_second.solution_candidate == r_first.solution_candidate)
        check("只有身份字段换新(qid)",
              r_second.qid == 2 and r_first.qid == 1)


def main() -> int:
    tests = [
        test_cache_identity_and_normalization,
        test_maybe_cache_only_success,
        test_repeat_same_question_no_llm,
        test_unavailable_not_cached_retry_hits_llm,
        test_late_ok_result_not_cached_after_timeout,
        test_cached_only_after_engine_accept,
        test_concurrent_cache_write_evict,
        test_cache_invalidated_on_new_round,
        test_cache_bounded,
        test_engine_receipt_accepted_and_rejected,
        test_cache_hit_archive_provenance_consistent,
    ]
    for t in tests:
        t()
    print()
    if FAIL[0]:
        print(f"FAILED: {FAIL[0]} 项")
        return 1
    print("PASS: 同题同问一致性 cache(Issue #65 §9/§12 + review B1/B2/B3) 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
