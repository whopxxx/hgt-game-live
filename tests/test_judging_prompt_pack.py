#!/usr/bin/env python
# coding: utf-8
"""Issue #53: Judging Prompt Pack v1 —— 判题热路径的全链路离线验收。

覆盖任务书 §15-§17/§45-§57 的实播事故 fixture、§58 的 loader 负例、
§2-§4 的 Pack 纪律、§27/§55-§57 的 Engine defense-in-depth、§31-§35
的前端/archive 契约。全部 FakeClient + scripted tool_input, 绝不碰
真实模型(§59)。

语义权威原则(§1)是所有用例的隐含判据:

    测试**不**因为字符串里包含"怎么"而期望 rephrase —— 是 scripted
    模型返回 rephrase、代码忠实接受; 反过来"为什么…是因为…吗"返回
    verdict 也必须畅通。这才证明语义权在模型, 测试没有偷偷固定
    关键词规则。
"""
import io
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from story.config import Config                                   # noqa: E402
from story import prompt_pack as PP                               # noqa: E402
from story.engine import Phase, RoundEngine                       # noqa: E402
from story.llm import (LLMResult, PuzzleWriter, _TOOL_ANSWER,      # noqa: E402
    _TOOL_CANDIDATE_RECHECK, _TOOL_COMPLETION_VERIFY)
from story.puzzle import (                                        # noqa: E402
    DiscoveryBeat, FairClue, PuzzleFact, PuzzleSpec, SolveAtom)
from story.quality import QUALITY_POLICY_VERSION                  # noqa: E402
from story.state import ActionKind, QAResult                      # noqa: E402

FAIL = [0]


def check(name, cond, extra=""):
    if cond:
        print(f"  ok  {name}")
        return
    print(f"  FAIL {name}  {extra}")
    FAIL[0] += 1


# ======================================================================
# 基础设施
# ======================================================================
class FakeClient:
    """scripted LLM: 按队列弹出 LLMResult; BaseException 原样抛。"""

    def __init__(self, results):
        self._results = list(results)
        self.calls = []
        self.cfg = type("C", (), {"model": "m", "timeout": 60,
                                  "max_retries": 3, "base_url": "x",
                                  "api_key": "k"})()
        self.runtime_cfg = type("R", (), {
            "answer_temperature": 0.2, "judge_temperature": 0.2,
            "review_temperature": 0.2, "generate_temperature": 0.8,
        })()

    def messages(self, system, user, max_tokens=None, tool=None,
                 temperature=None, timeout=None, max_retries=None,
                 stage=None, model=None):
        self.calls.append({"system": system, "user": user, "tool": tool,
                           "stage": stage})
        if not self._results:
            return LLMResult(error="no more canned results")
        r = self._results.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r


class FakeClock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def mkcfg(**kw):
    kw.setdefault("no_llm", True)
    return Config(sim_path="x", **kw)


_FACTS = [
    PuzzleFact(id="f1", text="门外女人是父亲的亲生女儿",
               kind="core", visibility="hidden"),
    PuzzleFact(id="f2", text="父亲多年前把她藏在门外",
               kind="core", visibility="hidden"),
    PuzzleFact(id="f3", text="家里的钟每周慢十分钟",
               kind="support", visibility="hidden"),
]
_SPEC = PuzzleSpec(
    id="judging-1", title="门外",
    puzzle="她每晚都睡在门外。为什么?",
    answer="她是父亲的亲生女儿, 被父亲藏在门外。",
    core_answer="门外女人是父亲的亲生女儿。",
    completion_fact_ids=["f1", "f2"],
    facts=_FACTS,
    solve_atoms=[
        SolveAtom(id="a1", role="key", text="门外女人是父亲的亲生女儿",
                  fact_ids=["f1"]),
        SolveAtom(id="a2", role="key", text="父亲把她藏在门外",
                  fact_ids=["f2"]),
    ],
    fair_clues=[FairClue(quote="她每晚都睡在门外", supports_atoms=["a2"])],
    discovery_beats=[
        DiscoveryBeat(id="b1", text="父亲在饭桌上多摆了一副碗筷"),
        DiscoveryBeat(id="b2", text="门外女人从不进屋吃饭"),
    ],
    hints=["注意那副碗筷", "想想她为什么从不进屋"],
    prompt_version="riddle-v9", quality_policy_version=QUALITY_POLICY_VERSION)


def _verdict(verdict="是", cand=False, established=None, touched=None,
             kind="verdict", comment="好眼力"):
    """第一层 Answer 的 scripted tool 返回(response_kind 合同齐全)。"""
    return LLMResult(tool_input={"answers": [{
        "id": 1, "response_kind": kind, "verdict": verdict,
        "comment": comment, "solution_candidate": cand,
        "touched_fact_ids": list(touched or []),
        "established_fact_ids": list(established or []),
    }]}, model="m")


def _match(ids):
    """completion 复核的 scripted 返回。"""
    return LLMResult(tool_input={"matched_completion_fact_ids": list(ids)},
                     model="m")


def _recheck(kind="verdict", verdict="是", cand=True, verified=None):
    return LLMResult(tool_input={
        "response_kind": kind, "verdict": verdict,
        "solution_candidate": cand,
        "verified_completion_fact_ids": list(verified or [])}, model="m")


def _writer(client):
    return PuzzleWriter(client=client, runtime_cfg=client.runtime_cfg)


def _run(client, text, **kw):
    kw.setdefault("spec", _SPEC)
    kw.setdefault("completion_fact_ids", list(_SPEC.completion_fact_ids))
    kw.setdefault("core_answer", _SPEC.core_answer)
    kw.setdefault("room_established_fact_ids", [])
    return _writer(client).answer(
        _SPEC.puzzle, _SPEC.answer, [], 1, "甲", text, **kw)


def boot(spec=None):
    spec = spec or _SPEC
    clk = FakeClock()
    eng = RoundEngine(mkcfg(), clock=clk)
    eng.start()
    eng.submit_riddle(spec.puzzle, spec.answer, list(spec.hints), spec=spec)
    assert eng.phase == Phase.QA, eng.phase
    return eng, clk


def ask(eng, clk, uid, name, text, response_kind="verdict", verdict="",
        **kw):
    """真人 #提问 -> ANSWER payload -> 直接提交一个 QAResult。

    绕开 Writer(Engine 层测试要的是**defense-in-depth**: 就算上游
    producer 疯了, Engine 也必须自己拦)。
    """
    eng.submit_danmaku(uid, name, "#" + text)
    clk.advance(20.0)
    acts = [a for a in eng.tick() if a.kind == ActionKind.ANSWER]
    assert acts, "没有派发 ANSWER"
    p = acts[0].payload
    if "completion_verified_fact_ids" not in kw:
        contract = set(eng._completion_fact_ids or ())
        est = [str(x) for x in (kw.get("established_fact_ids") or [])]
        kw["completion_verified_fact_ids"] = [
            x for x in est if x in contract or not contract]
    r = QAResult(qid=p["qid"], response_kind=response_kind,
                 verdict=verdict, status=kw.pop("status", "ok"), **kw)
    return eng.submit_qa([r], expect_round=p.get("expect_round"),
                         expect_spec_key=p.get("expect_spec_key"))


# ======================================================================
# §2/§3/§58: Pack 文件与 loader
# ======================================================================
def test_pack_files_and_versions():
    print("\n[Pack] judging pack 四件套 + 独立总版本")
    for stage in ("answer", "candidate_recheck", "completion_verify"):
        text = PP.load_prompt(stage)
        check(f"{stage} 可加载且非空", bool(text.strip()))
        check(f"{stage} 版本号对齐文件名",
              PP.stage_version(stage).startswith(stage.replace("_", "-")),
              PP.stage_version(stage))
    frag = PP.load_fragment("completion_specificity")
    check("共享 fragment 可加载", "特异性硬规则" in frag)
    check("judging 总版本", PP.HAIGUITANG_JUDGING_PROMPT_VERSION
          == "haiguitang-judging-v1")
    check("generation 总版本未漂移", PP.HAIGUITANG_GENERATION_PROMPT_VERSION
          == "haiguitang-generation-v1" and PP.PROMPT_PACK_VERSION
          == PP.HAIGUITANG_GENERATION_PROMPT_VERSION)
    check("两套总版本互相独立",
          PP.HAIGUITANG_JUDGING_PROMPT_VERSION
          != PP.HAIGUITANG_GENERATION_PROMPT_VERSION)
    # 单一来源: recheck 与 verify 都**引用** fragment, 而不是各抄一份
    root = PP.JUDGING_PROMPT_ROOT
    for fname in ("candidate-recheck-v1.md", "completion-verify-v1.md"):
        raw = (root / fname).read_text(encoding="utf-8")
        check(f"{fname} 用 include 标记引用共享 fragment",
              raw.count("{{fragment:completion_specificity}}") == 1)
        check(f"{fname} 没有把规则抄进正文",
              "特异性硬规则" not in raw)
    for fname in ("answer-v1.md", "candidate-recheck-v1.md",
                  "completion-verify-v1.md", "completion-specificity-v1.md"):
        check(f"{fname} 存在", (root / fname).is_file())
    # 展开后两个 stage 看到的共享文本逐字一致
    cv = PP.load_prompt("completion_verify")
    cr = PP.load_prompt("candidate_recheck")
    seg = frag.strip()
    check("verify 展开后含逐字 fragment", seg in cv)
    check("recheck 展开后含逐字 fragment", seg in cr)
    # generation pack 回归: 六个 stage 原样可加载
    for stage in PP.STAGES:
        check(f"generation/{stage} 不回归", bool(PP.load_prompt(stage).strip()))


def test_loader_negatives():
    print("\n[Pack] loader 负例(fail closed)")
    real_root, real_jroot = PP.PROMPT_ROOT, PP.JUDGING_PROMPT_ROOT
    try:
        for bad in ("nope", "answer-v1", "", "truth_extra"):
            try:
                PP.load_prompt(bad)
                check(f"unknown stage {bad!r} 被拒", False, "没抛")
            except PP.PromptPackError as e:
                check(f"unknown stage {bad!r} 被拒",
                      "unknown prompt stage" in str(e), str(e)[:60])
        with tempfile.TemporaryDirectory() as td:
            PP.JUDGING_PROMPT_ROOT = Path(td)
            try:
                PP.load_prompt("answer")
                check("缺文件被拒", False, "没抛")
            except PP.PromptPackError as e:
                check("缺文件被拒", "missing" in str(e), str(e)[:60])
            (Path(td) / "answer-v1.md").write_text("   \n", encoding="utf-8")
            try:
                PP.load_prompt("answer")
                check("空文件被拒", False, "没抛")
            except PP.PromptPackError as e:
                check("空文件被拒", "empty" in str(e), str(e)[:60])
            (Path(td) / "answer-v1.md").write_text(
                "你好 {name}", encoding="utf-8")
            try:
                PP.load_prompt("answer")
                check("占位符被拒", False, "没抛")
            except PP.PromptPackError as e:
                check("占位符被拒", "placeholder" in str(e), str(e)[:60])
            (Path(td) / "answer-v1.md").write_text(
                "引用 {{fragment:missing_one}}", encoding="utf-8")
            try:
                PP.load_prompt("answer")
                check("未知 fragment 被拒", False, "没抛")
            except PP.PromptPackError as e:
                check("未知 fragment 被拒", True, str(e)[:60])
            try:
                PP.load_fragment("missing_one")
                check("load_fragment 未知被拒", False, "没抛")
            except PP.PromptPackError:
                check("load_fragment 未知被拒", True)
        with tempfile.TemporaryDirectory() as td:
            PP.JUDGING_PROMPT_ROOT = Path(td)
            (Path(td) / "completion-specificity-v1.md").write_text(
                "规则A {{fragment:other}}", encoding="utf-8")
            (Path(td) / "completion-verify-v1.md").write_text(
                "主体 {{fragment:completion_specificity}}", encoding="utf-8")
            try:
                PP.load_prompt("completion_verify")
                check("嵌套 fragment 被拒", False, "没抛")
            except PP.PromptPackError:
                check("嵌套 fragment 被拒", True)
    finally:
        PP.PROMPT_ROOT, PP.JUDGING_PROMPT_ROOT = real_root, real_jroot
    # cwd 无关
    cwd = os.getcwd()
    try:
        os.chdir(tempfile.gettempdir())
        check("任意 cwd 仍可加载", "特异性硬规则"
              in PP.load_prompt("completion_verify"))
    finally:
        os.chdir(cwd)


def test_tool_schemas():
    print("\n[Pack] tool schema: response_kind 合同")
    items = _TOOL_ANSWER["input_schema"]["properties"]["answers"]["items"]
    check("answer 有 response_kind 且 enum 冻结",
          items["properties"]["response_kind"]["enum"]
          == ["verdict", "rephrase"])
    check("response_kind 必填", "response_kind"
          in items["required"], items["required"])
    check("verdict **不再**无条件必填", "verdict" not in items["required"],
          items["required"])
    check("verdict enum 仍是 是/不是/无关",
          items["properties"]["verdict"]["enum"] == ["是", "不是", "无关"])
    check("established/touched/candidate 字段齐",
          {"touched_fact_ids", "established_fact_ids",
           "solution_candidate", "comment"} <= set(items["properties"]))
    rc = _TOOL_CANDIDATE_RECHECK["input_schema"]
    check("recheck 也输出 response_kind",
          rc["properties"]["response_kind"]["enum"]
          == ["verdict", "rephrase"] and "response_kind" in rc["required"])
    check("recheck 没有 solved 字段",
          "solved" not in rc["properties"])
    cv = _TOOL_COMPLETION_VERIFY["input_schema"]
    check("verify 只回传 matched ids",
          list(cv["properties"]) == ["matched_completion_fact_ids"])


# ======================================================================
# §4/§5/§35: verdict/rephrase 路径 + provenance
# ======================================================================
def test_verdict_path_and_provenance():
    print("\n[Answer] verdict 路径 + prompt provenance")
    fc = FakeClient([_verdict("是", established=["f3"], touched=["f3"]),
                     _match([])])
    out, err = _run(fc, "门外女人是父亲的亲生女儿吗")
    r = out[0]
    check("verdict 原样带回", r.verdict == "是" and r.comment == "好眼力")
    check("response_kind=verdict", r.response_kind == "verdict")
    check("system 来自 Pack 文件",
          fc.calls[0]["system"] == PP.load_prompt("answer"))
    check("stage=qa.answer", fc.calls[0]["stage"] == "qa.answer")
    check("provenance: judging 总版本",
          r.judging_prompt_version == "haiguitang-judging-v1")
    check("provenance: answer stage 版本",
          r.answer_prompt_version == "answer-v1")
    check("普通 support 自报不经复核直接建立(非 completion)",
          r.established_fact_ids == ["f3"])


def test_rephrase_fixture_a_soufan():
    print("\n[§15/§45] 实播 fixture A: 怎么做馊饭? -> rephrase")
    fc = FakeClient([_verdict(kind="rephrase", verdict="",
                              comment="请换成能回答是/不是的猜测")])
    out, err = _run(fc, "怎么做馊饭？")
    r = out[0]
    check("response_kind=rephrase 被忠实接受", r.response_kind == "rephrase")
    check("不伪造 verdict", r.verdict == "", r.verdict)
    check("不建立 fact", r.established_fact_ids == []
          and r.touched_fact_ids == [])
    check("不标候选", not r.solution_candidate)
    check("恰好 1 次调用(不调复核/裁判)", len(fc.calls) == 1, len(fc.calls))
    check("没有 completion verifier",
          all(c["tool"]["name"] != "emit_completion_match"
              for c in fc.calls))
    check("没有 Final Judge",
          all(c["tool"]["name"] != "emit_judgement" for c in fc.calls))
    # 引擎侧: rephrase 记录不改变胜利状态, 不进 verdict 统计
    eng, clk = boot()
    before = dict(eng._verdict_counts)
    ask(eng, clk, "u1", "甲", "怎么做馊饭？", response_kind="rephrase",
        verdict="", touched_fact_ids=[], established_fact_ids=[],
        solution_candidate=False)
    check("victory 不变", eng.phase == Phase.QA, eng.phase)
    check("rephrase 不进 verdict_counts",
          eng._verdict_counts == before, eng._verdict_counts)
    check("rephrase_count 独立计数", eng._rephrase_count == 1,
          eng._rephrase_count)


def test_verdict_fixture_b_why_because():
    print("\n[§16/§46] 实播 fixture B: 为什么…是因为…吗 -> verdict")
    text = "为什么他只吃坏饭，是因为想惩罚自己吗？"
    fc = FakeClient([_verdict("是", established=["f3"], touched=["f3"]),
                     _match([])])
    out, err = _run(fc, text)
    r = out[0]
    check("含'为什么'但模型判 verdict -> 畅通", r.verdict == "是")
    check("完整走完 fact 流程", r.established_fact_ids == ["f3"])
    check("只有 1 次调用(support 不触发复核)", len(fc.calls) == 1)
    # 引擎侧: verdict 正常计入统计
    eng, clk = boot()
    ask(eng, clk, "u1", "甲", text, response_kind="verdict", verdict="是",
        touched_fact_ids=["f3"], established_fact_ids=["f3"])
    check("进 verdict_counts[是]", eng._verdict_counts.get("是") == 1,
          eng._verdict_counts)
    check("rephrase_count 不动", eng._rephrase_count == 0)


def test_tool_failure_fixture_c():
    print("\n[§14/§17/§47] 线上事故: tool 空 + 自由文本 -> 未判定")
    fc = FakeClient([LLMResult(text="1|无关|发个 是/不是 的猜测",
                               error="工具调用返回空 input", model="m")])
    out, err = _run(fc, "怎么做馊饭？")
    r = out[0]
    check("verdict = 未判定", r.verdict == "未判定", r.verdict)
    check("status = unavailable", r.status == "unavailable")
    check("established/completion 全空", r.established_fact_ids == []
          and r.completion_verified_fact_ids == [])
    check("不建立 touched", r.touched_fact_ids == [])
    check("**绝不**产出「无关」", r.verdict != "无关")
    check("自由文本点评不再泄漏成裁决",
          "的猜测" not in (r.comment or ""), r.comment)
    check("恰好 1 次调用", len(fc.calls) == 1)
    # 语义权威测试纪律: parser 的关键词表仍然存在, 但生产 answer() 不再调它
    import story.parser as P
    parsed, _ = P.parse_answers("1|无关|发个 是/不是 的猜测",
                                [type("Q", (), {"qid": 1})()])
    check("parse_answers 本体还在(供 legacy/实验), 但 answer() 不再消费",
          bool(parsed))
    src = io.open("story/llm.py", encoding="utf-8").read()
    check("llm.py 不再调用 P.parse_answers",
          "P.parse_answers(" not in src)


def test_malformed_results_fail_closed():
    print("\n[§10/§55] 结构合同无效 -> 未判定(绝不静默修好)")
    # rephrase 却带了 established
    fc = FakeClient([LLMResult(tool_input={"answers": [{
        "id": 1, "response_kind": "rephrase", "verdict": "",
        "solution_candidate": False, "touched_fact_ids": [],
        "established_fact_ids": ["f2"]}]}, model="m")])
    out, _ = _run(fc, "随便聊聊")
    r = out[0]
    check("rephrase + established -> 整条无效",
          r.verdict == "未判定" and r.status == "unavailable")
    check("established 被整体丢弃(不是洗掉再用)",
          r.established_fact_ids == [])
    # verdict 类给了非法 verdict(含已废弃的 揭晓)
    for bad in ("揭晓", "对", "yes"):
        fc = FakeClient([LLMResult(tool_input={"answers": [{
            "id": 1, "response_kind": "verdict", "verdict": bad,
            "solution_candidate": False, "touched_fact_ids": [],
            "established_fact_ids": []}]}, model="m")])
        out, _ = _run(fc, "他瞎了吗")
        check(f"verdict={bad!r} -> 未判定",
              out[0].verdict == "未判定" and out[0].status == "unavailable")
    # response_kind 缺失
    fc = FakeClient([LLMResult(tool_input={"answers": [{
        "id": 1, "verdict": "是", "solution_candidate": False,
        "touched_fact_ids": [], "established_fact_ids": []}]}, model="m")])
    out, _ = _run(fc, "他瞎了吗")
    check("缺 response_kind -> 未判定(不由代码从文本猜)",
          out[0].verdict == "未判定")


def test_candidate_only_from_model():
    print("\n[§12/§48/§49] solution_candidate 只来自模型自报")
    # §48: 没有"所以"也必须能触发复核
    fc = FakeClient([_verdict("是", cand=True), _match(["f1"])])
    out, _ = _run(fc, "门外女人就是他父亲的亲生女儿")
    check("文本不含因果连词", "所以" not in "门外女人就是他父亲的亲生女儿")
    check("candidate=True -> 进入 completion 复核", len(fc.calls) == 2)
    check("复核确认后建立", out[0].completion_verified_fact_ids == ["f1"])
    # §49: 有"所以"但模型判 false -> 代码必须尊重
    fc = FakeClient([_verdict("是", cand=False)])
    out, _ = _run(fc, "是父亲的女儿, 所以睡在门外")
    check("句子里有'所以'", "所以" in "是父亲的女儿, 所以睡在门外")
    check("模型说 candidate=false -> 不触发复核", len(fc.calls) == 1)
    check("没有任何第二层调用",
          all(c["tool"]["name"] == "emit_verdict" for c in fc.calls))
    src = io.open("story/llm.py", encoding="utf-8").read()
    src_code = "\n".join(ln for ln in src.splitlines()
                         if not ln.lstrip().startswith("#"))
    check("_looks_like_solution 不再拥有 candidate 改写权",
          "or _looks_like_solution(text)" not in src_code
          and "r.solution_candidate = _looks_like_solution(text)"
          not in src_code)


# ======================================================================
# §19-§22/§50/§51: Candidate Recheck
# ======================================================================
def _recheck_setup():
    first = LLMResult(tool_input={"answers": [{
        "id": 1, "response_kind": "verdict", "verdict": "无关",
        "comment": "", "solution_candidate": True,
        "touched_fact_ids": [], "established_fact_ids": []}]}, model="m")
    return first


def test_recheck_two_directions():
    print("\n[§20/§21/§50] Candidate Recheck: 两个修复方向")
    # 方向 A: 修成 verdict 是
    fc = FakeClient([_recheck_setup(), _recheck("verdict", "是", True,
                                                verified=["f1"])])
    out, _ = _run(fc, "她就是被父亲藏在门外的亲生女儿")
    r = out[0]
    check("矛盾(候选+无关)触发 Recheck",
          fc.calls[1]["tool"]["name"] == "emit_candidate_recheck")
    check("恰好 2 次调用", len(fc.calls) == 2)
    check("修复为 verdict=是", r.verdict == "是"
          and r.response_kind == "verdict")
    check("Recheck 自己完成 completion 确认",
          r.completion_verified_fact_ids == ["f1"])
    check("不串第三次 verifier",
          all(c["tool"]["name"] != "emit_completion_match"
              for c in fc.calls))
    check("provenance: recheck stage 版本",
          r.candidate_recheck_prompt_version == "candidate-recheck-v1")
    check("recheck system 来自 Pack 文件",
          fc.calls[1]["system"] == PP.load_prompt("candidate_recheck"))
    # 方向 B: 修成 rephrase(第一层把闲聊标成了候选)
    fc = FakeClient([_recheck_setup(), _recheck("rephrase", "", False)])
    out, _ = _run(fc, "哈哈哈主播好搞笑")
    r = out[0]
    check("修复为 rephrase", r.response_kind == "rephrase" and r.verdict == "")
    check("不伪造「无关」", r.verdict != "无关")
    check("candidate 清零", not r.solution_candidate)
    check("无建立", r.established_fact_ids == []
          and r.completion_verified_fact_ids == [])
    check("恰好 2 次调用", len(fc.calls) == 2)


def test_recheck_failure_no_third_call():
    print("\n[§51] Recheck 技术失败 -> 未判定, 绝无第三次调用")
    for label, second in (
            ("timeout", LLMResult(error="timeout", model="m")),
            ("空 tool", LLMResult(text="忙", model="m")),
            ("坏 payload", LLMResult(tool_input={"response_kind": "verdict"},
                                    model="m")),
            ("异常", RuntimeError("boom"))):
        fc = FakeClient([_recheck_setup(), second])
        out, _ = _run(fc, "随便说说")
        r = out[0]
        check(f"{label}: status=unavailable",
              r.verdict == "未判定" and r.status == "unavailable")
        check(f"{label}: 不建立 facts", r.established_fact_ids == []
              and r.completion_verified_fact_ids == [])
        check(f"{label}: 总共最多 2 次调用", len(fc.calls) == 2, len(fc.calls))
        check(f"{label}: 不继续调 verifier/judge",
              all(c["tool"]["name"] == "emit_candidate_recheck"
                  or c["tool"]["name"] == "emit_verdict"
                  for c in fc.calls))


# ======================================================================
# §23-§26/§52/§53: Completion Verify
# ======================================================================
def test_completion_verify_normal_path():
    print("\n[§52] Answer 是 + 复核确认 -> 推进通关")
    fc = FakeClient([_verdict("是", established=["f1"], touched=["f1"]),
                     _match(["f1"])])
    out, _ = _run(fc, "门外女人是父亲的亲生女儿吗")
    r = out[0]
    check("completion 必须先复核", len(fc.calls) == 2
          and fc.calls[1]["tool"]["name"] == "emit_completion_match")
    check("确认后带回 verified", r.completion_verified_fact_ids == ["f1"])
    check("established 只含确认过的", r.established_fact_ids == ["f1"])
    check("verify system 来自 Pack 文件",
          fc.calls[1]["system"] == PP.load_prompt("completion_verify"))
    check("verify 不含答案文本泄漏(它拿不到 verdict 改写权)",
          r.verdict == "是")
    check("provenance: verify stage 版本",
          r.completion_verify_prompt_version == "completion-verify-v1")
    # 引擎覆盖 -> 揭晓(§26: victory 仍只在 Engine)
    eng, clk = boot()
    ask(eng, clk, "u1", "甲", "门外女人是父亲的亲生女儿吗",
        response_kind="verdict", verdict="是", touched_fact_ids=["f1"],
        established_fact_ids=["f1"], completion_verified_fact_ids=["f1"])
    ask(eng, clk, "u2", "乙", "父亲把她藏在门外",
        response_kind="verdict", verdict="是", touched_fact_ids=["f2"],
        established_fact_ids=["f2"], completion_verified_fact_ids=["f2"])
    check("合同覆盖 -> REVEALING", eng.phase == Phase.REVEALING, eng.phase)
    check("胜者是补齐者", eng._solved_by == "乙", eng._solved_by)


def test_completion_verify_tech_failure():
    print("\n[§25/§53] 复核技术失败: fail-open 对话, fail-closed 胜利")
    eng, clk = boot()
    # 第一层可靠裁决是「不是」(普通 support)
    ask(eng, clk, "u1", "甲", "家里的钟慢了吗",
        response_kind="verdict", verdict="不是", touched_fact_ids=["f3"],
        established_fact_ids=["f3"], completion_verified_fact_ids=[])
    check("普通 fact 保留", "f3" in eng._established_fact_ids)
    # 复核失败的 completion 提议: 不推进
    ask(eng, clk, "u2", "乙", "门外女人是父亲的亲生女儿吗",
        response_kind="verdict", verdict="是", touched_fact_ids=["f1"],
        established_fact_ids=["f1"], completion_verified_fact_ids=[])
    check("completion 一条都不推进",
          not ({"f1", "f2"} & eng._established_fact_ids),
          eng._established_fact_ids)
    check("不 solved", eng.phase == Phase.QA, eng.phase)


def test_touched_is_not_established():
    print("\n[§54] touched != established 冻结")
    eng, clk = boot()
    ask(eng, clk, "u1", "甲", "他是不是在朝着门外走",
        response_kind="verdict", verdict="是", touched_fact_ids=["f2"],
        established_fact_ids=[])
    check("touched 不建立", "f2" not in eng._established_fact_ids)
    check("房间共识为空", eng._established_fact_ids == set(),
          eng._established_fact_ids)


def test_engine_defense_in_depth():
    print("\n[§27/§55/§56/§57] Engine defense-in-depth: 恶意 producer")
    eng, clk = boot()
    # rephrase + established completion + verified —— 全都塞给它
    ask(eng, clk, "u1", "甲", "怎么做馊饭？", response_kind="rephrase",
        verdict="", touched_fact_ids=[], established_fact_ids=["f1"],
        completion_verified_fact_ids=["f1"])
    check("rephrase + established -> 一条都不收",
          eng._established_fact_ids == set(), eng._established_fact_ids)
    check("不 solved", eng.phase == Phase.QA, eng.phase)
    # unavailable 同理
    ask(eng, clk, "u2", "乙", "他瞎了吗", response_kind="verdict",
        verdict="未判定", status="unavailable",
        established_fact_ids=["f2"], completion_verified_fact_ids=["f2"])
    check("unavailable + established -> 不收",
          eng._established_fact_ids == set(), eng._established_fact_ids)
    # 直接调写入口, 显式 response_kind=rephrase
    got = eng._record_human_established_locked(
        ["f1"], verdict="是", status="ok", response_kind="rephrase")
    check("写入口拒绝 rephrase", got == [])
    got = eng._record_human_established_locked(
        ["f1"], verdict="是", status="ok", response_kind="verdict")
    check("写入口接受 verdict=是", got == ["f1"], got)
    # 即便 f1 已进共识, 合同没有 f2 依然不揭晓
    ask(eng, clk, "u3", "丙", "父亲把她藏在门外",
        response_kind="rephrase", verdict="", established_fact_ids=["f2"],
        completion_verified_fact_ids=["f2"])
    check("rephrase 不能补齐合同", eng.phase == Phase.QA, eng.phase)


def test_rephrase_stats_and_transcript():
    print("\n[§29/§30/§33] rephrase 统计独立 + transcript 保留语义")
    rec_like = "[7] 甲：怎么做馊饭？ → 请改问法 (请换成是/不是的猜测)"
    from story.state import QARec
    rec = QARec(qid=7, user_name="甲", text="怎么做馊饭？", verdict="",
                kind="qa", response_kind="rephrase",
                comment="请换成是/不是的猜测")
    check("to_line 保留 rephrase 语义", rec.to_line() == rec_like,
          rec.to_line())
    check("transcript 展示 label 不是 verdict enum",
          "请改问法" == rec.to_line().split("→ ")[1].split(" (")[0])
    # verdict 记录的 to_line 不变
    rec2 = QARec(qid=8, user_name="乙", text="他瞎了吗", verdict="不是",
                 kind="qa")
    check("verdict to_line 原样", rec2.to_line()
          == "[8] 乙：他瞎了吗 → 不是", rec2.to_line())
    # 未判定继续独立
    eng, clk = boot()
    ask(eng, clk, "u1", "甲", "他瞎了吗", response_kind="verdict",
        verdict="未判定", status="unavailable")
    check("未判定不进统计", eng._verdict_counts == {}, eng._verdict_counts)
    check("未判定不进 rephrase_count", eng._rephrase_count == 0)


def test_archive_provenance_and_frontend():
    print("\n[§31/§32/§34/§35] archive provenance + 前端契约")
    from story.state import QARec
    rec = QARec(qid=3, user_name="甲", text="怎么做馊饭？", verdict="",
                kind="qa", response_kind="rephrase",
                judging_prompt_version="haiguitang-judging-v1",
                answer_prompt_version="answer-v1",
                candidate_recheck_prompt_version="candidate-recheck-v1")
    d = rec.to_archive()
    check("archive 有 response_kind", d["response_kind"] == "rephrase")
    check("archive 有 judging/answer prompt 版本",
          d["judging_prompt_version"] == "haiguitang-judging-v1"
          and d["answer_prompt_version"] == "answer-v1")
    check("archive 有 recheck 版本(发生过才有值)",
          d["candidate_recheck_prompt_version"] == "candidate-recheck-v1")
    check("archive verify 版本缺省空",
          d["completion_verify_prompt_version"] == "")
    j = rec.to_json()
    check("to_json 带 response_kind(前端渲染用)",
          j.get("response_kind") == "rephrase")
    check("to_json 不带 fact id / prompt 版本",
          "established_fact_ids" not in j
          and "judging_prompt_version" not in j)
    # legacy 记录向后兼容
    old = QARec(qid=4, user_name="乙", text="他瞎了吗", verdict="不是",
                kind="qa")
    check("旧数据缺 response_kind -> 按 verdict 语义兼容",
          old.response_kind == "verdict"
          and "response_kind" not in old.to_json())
    # 前端只读渲染, 不做语义判断
    app = io.open("web/app.js", encoding="utf-8").read()
    check("前端读 response_kind 渲染请改问法徽章",
          'r.response_kind === "rephrase"' in app
          and "请改问法" in app and "v-rephrase" in app)
    check("前端不做关键词语义判断",
          'text.includes("为什么")' not in app
          and 'text.includes("怎么")' not in app)
    check("rephrase 不进连续无关折叠(折叠仍按 verdict==='无关')",
          'r.verdict === "无关"' in app)
    css = io.open("web/style.css", encoding="utf-8").read()
    check("样式表有 .v-rephrase", ".v-rephrase" in css)


def test_protocol_doc_semantic_authority():
    print("\n[§36/§37] protocol/v1.md Semantic Authority")
    doc = io.open("haiguitang/protocol/v1.md", encoding="utf-8").read()
    check("有 Semantic Authority 一节",
          "## 19. Semantic Authority / Judging Contract" in doc)
    for phrase in ("response_kind", "rephrase", "unavailable",
                   "UNAVAILABLE", "defense-in-depth",
                   "downstream lexical reinterpretation"):
        check(f"冻结点: {phrase}", phrase in doc)
    check("victory ownership 引用不变", "RoundEngine.submit_qa" in doc)


def test_heuristics_out_of_qa_business():
    print("\n[§12/§13/§41/§68] heuristic 退出业务路径(源代码级)")
    src = io.open("story/llm.py", encoding="utf-8").read()
    answer_src = "\n".join(
        ln for ln in src.split("def answer(self", 1)[1].split(
            "\n    def _candidate_recheck", 1)[0].splitlines()
        if not ln.lstrip().startswith("#"))
    check("answer() 不再调用 _is_open_question",
          "_is_open_question(" not in answer_src)
    check("answer() 不再直接用 _HYPOTHESIS_RE 分流",
          "_HYPOTHESIS_RE.search" not in answer_src)
    check("helper 仍在(历史测试引用)",
          "def _looks_like_solution" in src and "def _is_open_question" in src)
    check("legacy Final Judge 兼容路径仍在(candidate -> judge)",
          "self.judge(" in answer_src)
    # legacy 无合同: candidate=true -> Judge 仍工作(§40/§42)
    fc = FakeClient([_verdict("是", cand=True),
                     LLMResult(tool_input={"is_guess": True,
                                           "cause_hit": True,
                                           "mechanism_hit": True,
                                           "matched_atoms": ["a1"]},
                               model="m")])
    out, _ = _writer(fc).answer(
        _SPEC.puzzle, _SPEC.answer, [], 1, "甲",
        "为什么她睡在门外, 是因为她是父亲的亲生女儿吗？",
        spec=_SPEC, completion_fact_ids=[], core_answer="", judge_solve=True)
    check("legacy: candidate -> Final Judge(2 次调用)", len(fc.calls) == 2)
    check("legacy: judge 判中 -> 揭晓(仅无合同题)",
          out[0].verdict == "揭晓")
    # 有合同的题绝不调 Judge
    fc = FakeClient([_verdict("是", cand=True), _match([])])
    out, _ = _run(fc, "她就是父亲的亲生女儿")
    check("有合同: 绝不 emit_judgement",
          all(c["tool"]["name"] != "emit_judgement" for c in fc.calls))


# ======================================================================
def main():
    print("=== tests/test_judging_prompt_pack.py ===")
    test_pack_files_and_versions()
    test_loader_negatives()
    test_tool_schemas()
    test_verdict_path_and_provenance()
    test_rephrase_fixture_a_soufan()
    test_verdict_fixture_b_why_because()
    test_tool_failure_fixture_c()
    test_malformed_results_fail_closed()
    test_candidate_only_from_model()
    test_recheck_two_directions()
    test_recheck_failure_no_third_call()
    test_completion_verify_normal_path()
    test_completion_verify_tech_failure()
    test_touched_is_not_established()
    test_engine_defense_in_depth()
    test_rephrase_stats_and_transcript()
    test_archive_provenance_and_frontend()
    test_protocol_doc_semantic_authority()
    test_heuristics_out_of_qa_business()
    print()
    if FAIL[0]:
        print(f"FAILED: {FAIL[0]} check(s)")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
