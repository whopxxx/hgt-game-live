#!/usr/bin/env python
# coding: utf-8
"""运行: uv run tests/test_solve_ux.py（完全离线, 无网络）。

Solve UX + Puzzle Truthfulness 回归套件。

这一套钉住的是**产品语义**, 不是实现细节。本批完成后系统必须能用
下面四句话描述, 而代码与测试与它一致:

    1. 谜面可以误导, 但不能撒谎。
    2. 谜底必须能先用一句普通人听懂的话说清楚。
    3. 房间是共同解谜: 已被真人问答公开确认的事实会累计。
    4. 当房间已经建立了这道题真正必要的 1~2 个核心事实, 最后补齐拼图
       的真人立即触发揭晓; 不要求他重新背一遍大家已经推出来的内容。

覆盖(离线、确定性):

    Case A  集体身份题: 补齐缺口即通关, 该句不必含因果
    Case B  飞行测试: 第二条补齐直接揭晓, 不必复述机制
    Case C  touched 不能冒充 established
    Case D  非法 fact id 被丢弃
    Case E  support/exclusion 不能成为 completion
    Case F  completion 最多 2 条
    Case G  v5 不调 Final Judge
    Case H  legacy 不退化
    Case I  narrator truthfulness fail-closed
    Case J  mechanism consistency fail-closed
    Case K  core_answer deterministic reveal(逐字)
    Case L  runtime identity(只改合同也必须换 key)
    Case M  human-only established

数据全部是**去身份化的最小构造**, 不含真实昵称 / session。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from story.config import Config  # noqa: E402
from story.engine import RoundEngine  # noqa: E402
from story.llm import (  # noqa: E402
    LLMResult, PuzzleWriter,
    _QUALITY_CHECK_FIELDS, _TOOL_ANSWER, _TOOL_CHECK, _TOOL_RIDDLE,
    ANSWER_SYSTEM, CHECK_SYSTEM, RIDDLE_SYSTEM,
)
from story.puzzle import (  # noqa: E402
    FairClue, PuzzleFact, PuzzleSpec, SolveAtom, runtime_spec_key,
)
from story.quality import MAX_COMPLETION_FACTS, validate_spec  # noqa: E402
from story.state import ActionKind, Phase, QARec, QAResult  # noqa: E402

FAIL = [0]


def check(name, cond, extra=""):
    if cond:
        print(f"  ok  {name}")
        return
    print(f"  FAIL {name}  {extra}")
    FAIL[0] += 1


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


# ----------------------------------------------------------------------
# 构造器
# ----------------------------------------------------------------------
def ident_spec(completion=("f1", "f2"), core="门外女人是父亲的亲生女儿。"):
    """集体身份题: 两个核心事实, 可由两个不同的观众分别建立。"""
    return PuzzleSpec(
        id="ux-ident", title="门外",
        puzzle="门外站着一个女人, 开门的人一见她就愣住了。为什么?",
        answer="门外女人是父亲的亲生女儿, 她昨晚才与父亲同桌吃饭相认。",
        core_answer=core, completion_fact_ids=list(completion),
        facts=[
            PuzzleFact(id="f1", text="门外女人是父亲的亲生女儿", kind="core",
                       visibility="hidden"),
            PuzzleFact(id="f2", text="门外女人昨晚与父亲同桌吃饭",
                       kind="core", visibility="hidden"),
            PuzzleFact(id="f3", text="开门的人不认识她", kind="support",
                       visibility="hidden"),
            PuzzleFact(id="f4", text="她不是来讨债的", kind="exclusion",
                       visibility="hidden"),
        ],
        solve_atoms=[
            SolveAtom(id="a1", role="key", text="门外女人是父亲的亲生女儿",
                      fact_ids=["f1"]),
            SolveAtom(id="a2", role="key", text="她昨晚与父亲同桌吃饭",
                      fact_ids=["f2"]),
        ],
        fair_clues=[FairClue(quote="开门的人一见她就愣住了",
                             supports_atoms=["a1"])],
        hints=["注意她的身份", "注意昨晚发生了什么", "注意饭桌"],
        prompt_version="riddle-v5", quality_policy_version="quality-v5")


def flight_spec():
    """飞行测试: 与 A 同构, 换成"测试飞行"题材。"""
    return PuzzleSpec(
        id="ux-flight", title="测试飞行",
        puzzle="飞机落地后机长长舒一口气, 乘客却在鼓掌。为什么?",
        answer="这是一次考核飞行, 复飞本身就是测试项目, 落地才意味着通过。",
        core_answer="这是一次预设的测试飞行, 复飞本身就是考核项目。",
        completion_fact_ids=["f1", "f2"],
        facts=[
            PuzzleFact(id="f1", text="这是测试/考核飞行", kind="core"),
            PuzzleFact(id="f2", text="复飞本来就是测试项目", kind="core"),
        ],
        solve_atoms=[
            SolveAtom(id="a1", role="key", text="这是一次测试飞行",
                      fact_ids=["f1"]),
            SolveAtom(id="a2", role="key", text="复飞是考核项目",
                      fact_ids=["f2"]),
        ],
        fair_clues=[FairClue(quote="乘客却在鼓掌", supports_atoms=["a1"])],
        hints=["注意掌声", "注意民航流程", "注意考核"],
        prompt_version="riddle-v5", quality_policy_version="quality-v5")


def boot(spec):
    clk = FakeClock()
    eng = RoundEngine(mkcfg(), clock=clk)
    eng.start()
    eng.submit_riddle(spec.puzzle, spec.answer, list(spec.hints), spec=spec)
    assert eng.phase == Phase.QA, eng.phase
    return eng, clk


def ask(eng, clk, uid, name, text, **kw):
    """发一条 #提问 -> 拿 ANSWER payload -> 按 kw 回一个 QAResult。"""
    eng.submit_danmaku(uid, name, "#" + text)
    clk.advance(20.0)
    acts = [a for a in eng.tick() if a.kind == ActionKind.ANSWER]
    assert acts, "没有派发 ANSWER"
    p = acts[0].payload
    return eng.submit_qa([QAResult(qid=p["qid"], **kw)],
                         expect_round=p.get("expect_round"),
                         expect_spec_key=p.get("expect_spec_key"))


# ----------------------------------------------------------------------
# Case A / B —— 房间共同推理
# ----------------------------------------------------------------------
def test_case_a_collective_identity():
    print("\n[Case A] 集体身份题: 补齐缺口即通关")
    sp = ident_spec()
    eng, clk = boot(sp)
    ask(eng, clk, "u1", "甲", "她昨晚和父亲吃饭了吗",
        verdict="是", established_fact_ids=["f2"])
    check("1/2 -> 仍在 QA", eng.phase == Phase.QA, eng.phase)
    acts = ask(eng, clk, "u2", "乙", "她是姐姐", verdict="是",
               established_fact_ids=["f1"])
    check("2/2 -> REVEALING", eng.phase == Phase.REVEALING, eng.phase)
    check("胜者 = 补齐者", eng._solved_by == "乙", eng._solved_by)
    # 这句**没有**"因为/所以", 也**没有**复述机制 —— 仍然必须通关
    check("该句不含因果连词", "因为" not in "她是姐姐")
    rev = [a for a in acts if a.kind == ActionKind.REVEAL]
    check("REVEAL 带 core_answer 且逐字一致",
          rev and rev[0].payload.get("core_answer") == sp.core_answer,
          rev[0].payload.get("core_answer") if rev else None)


def test_case_b_flight_test():
    print("\n[Case B] 飞行测试: 第二条补齐即揭晓")
    eng, clk = boot(flight_spec())
    ask(eng, clk, "u1", "甲", "这是测试飞行吗", verdict="是",
        established_fact_ids=["f1"])
    check("第一条不够", eng.phase == Phase.QA, eng.phase)
    ask(eng, clk, "u2", "乙", "复飞是考核项目", verdict="是",
        established_fact_ids=["f2"])
    check("第二条补齐 -> 揭晓", eng.phase == Phase.REVEALING, eng.phase)
    check("不要求复述机长/数据/塔台那一串",
          "塔台" not in "复飞是考核项目")


# ----------------------------------------------------------------------
# Case C / D / M —— established 的边界
# ----------------------------------------------------------------------
def test_case_c_touched_is_not_established():
    print("\n[Case C] touched 不能冒充 established")
    eng, clk = boot(ident_spec())
    ask(eng, clk, "u1", "甲", "她和父亲有关系吗", verdict="是",
        touched_fact_ids=["f1"], established_fact_ids=[])
    check("touched 记下", eng._touched_fact_ids == {"f1"}, eng._touched_fact_ids)
    check("established 为空", eng._established_fact_ids == set(),
          eng._established_fact_ids)
    check("未通关", eng.phase == Phase.QA, eng.phase)


def test_case_d_illegal_fact_id():
    print("\n[Case D] 非法 fact id 被丢弃")
    eng, clk = boot(ident_spec())
    ask(eng, clk, "u1", "甲", "随便猜", verdict="是",
        established_fact_ids=["f999", "f1"])
    check("f999 丢弃, f1 保留",
          eng._established_fact_ids == {"f1"}, eng._established_fact_ids)
    check("未通关(1/2)", eng.phase == Phase.QA, eng.phase)


def test_case_m_human_only():
    print("\n[Case M] 只有真人 QA 能建立事实")
    eng, clk = boot(ident_spec())
    ask(eng, clk, "u1", "甲", "第一条", verdict="是",
        established_fact_ids=["f1"])
    before = set(eng._established_fact_ids)
    check("真人建立了 f1", before == {"f1"}, before)
    eng.submit_hint("想想她是谁")
    check("submit_hint 不增加", eng._established_fact_ids == before,
          eng._established_fact_ids)
    # 未判定不建立
    eng.submit_danmaku("u2", "乙", "#再猜")
    clk.advance(20.0)
    acts = [a for a in eng.tick() if a.kind == ActionKind.ANSWER]
    eng.submit_qa([QAResult(qid=acts[0].payload["qid"], verdict="未判定",
                            status="unavailable", established_fact_ids=["f2"])])
    check("技术失败不建立事实", eng._established_fact_ids == before,
          eng._established_fact_ids)
    # 边界必须显式可查(Step 14 Detective 绝不能调它)
    check("存在具名写入口", hasattr(eng, "_record_human_established_locked"))
    doc = eng._record_human_established_locked.__doc__ or ""
    check("docstring 冻结了 Detective 边界", "Detective" in doc, doc[:80])
    check("submit_detective 尚不存在(Step 14)",
          not hasattr(eng, "submit_detective"))


# ----------------------------------------------------------------------
# Case E / F —— 通关合同的硬校验
# ----------------------------------------------------------------------
def test_case_e_support_exclusion_rejected():
    print("\n[Case E] support / exclusion 不能成为 completion")
    vr = validate_spec(ident_spec(completion=("f3",)))
    check("support -> 拒", not vr.ok, vr.why())
    vr2 = validate_spec(ident_spec(completion=("f4",)))
    check("exclusion -> 拒", not vr2.ok, vr2.why())


def test_case_f_completion_capped():
    print(f"\n[Case F] completion 最多 {MAX_COMPLETION_FACTS} 条")
    check("上限常量就是 2(不放宽成 4、5)", MAX_COMPLETION_FACTS == 2,
          MAX_COMPLETION_FACTS)
    sp = ident_spec(completion=("f1", "f2"))
    sp.facts.append(PuzzleFact(id="f5", text="第三条核心", kind="core"))
    sp.solve_atoms[0].fact_ids = ["f1", "f5"]
    sp.completion_fact_ids = ["f1", "f2", "f5"]
    vr = validate_spec(sp)
    check("3 条 -> 拒", not vr.ok, vr.why())
    check("提示应重出而不是放宽", "重出" in vr.why(), vr.why())


# ----------------------------------------------------------------------
# Case G / H —— 胜负归属
# ----------------------------------------------------------------------
class FakeClient:
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
                 temperature=None, timeout=None, max_retries=None):
        self.calls.append({"system": system, "user": user, "tool": tool})
        if not self._results:
            return LLMResult(error="no more canned results")
        return self._results.pop(0)


def _verdict(established=None, cand=False, verdict="是"):
    return LLMResult(tool_input={"answers": [{
        "id": 1, "verdict": verdict, "comment": "好眼力",
        "solution_candidate": cand,
        "touched_fact_ids": [],
        "established_fact_ids": list(established or []),
    }]}, model="m")


_FACTS = [{"id": "f1", "text": "门外女人是父亲的亲生女儿", "kind": "core"}]


def test_case_g_v5_no_final_judge():
    print("\n[Case G] v5 不调 Final Judge")
    fc = FakeClient([_verdict(established=["f1"], cand=True)])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    out, err = w.answer("谜面?", "谜底。", [], 1, "甲", "她是姐姐",
                        facts=_FACTS, completion_fact_ids=["f1"])
    check("只调一次 client", len(fc.calls) == 1, len(fc.calls))
    check("绝不 emit_judgement",
          all(c["tool"]["name"] != "emit_judgement" for c in fc.calls),
          [c["tool"]["name"] for c in fc.calls])
    check("不产生 P.SOLVE", out and out[0].verdict != "揭晓",
          out[0].verdict if out else None)
    check("established 原样带回", out and out[0].established_fact_ids == ["f1"],
          out[0].established_fact_ids if out else None)


def test_case_h_legacy_not_degraded():
    print("\n[Case H] legacy 无合同 -> 仍走 Final Judge")
    fc = FakeClient([
        _verdict(cand=True),
        LLMResult(tool_input={"is_guess": True, "cause_hit": True,
                              "mechanism_hit": True,
                              "matched_atoms": ["a1", "a2"]}, model="m"),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    out, err = w.answer(
        "谜面?", "谜底。", [], 1, "甲", "退潮时礁石露出, 所以灯是标礁石",
        solve_atoms=[{"id": "a1", "role": "cause", "text": "x",
                      "fact_ids": ["f1"]},
                     {"id": "a2", "role": "mechanism", "text": "y",
                      "fact_ids": ["f1"]}],
        facts=_FACTS, completion_fact_ids=[])
    check("调了两次", len(fc.calls) == 2, len(fc.calls))
    check("第二次是 emit_judgement",
          fc.calls[1]["tool"]["name"] == "emit_judgement",
          fc.calls[1]["tool"]["name"])
    check("legacy 仍能通关", out and out[0].verdict == "揭晓",
          out[0].verdict if out else None)


# ----------------------------------------------------------------------
# Case I / J —— Reviewer fail-closed
# ----------------------------------------------------------------------
def _pass_review(**qc):
    """一份 decision=pass 的审稿回包, quality_checks 可按需覆盖。"""
    d = {
        "decision": "pass",
        "puzzle": "门外站着一个女人, 开门的人一见她就愣住了。为什么?",
        "answer": "门外女人是父亲的亲生女儿。",
        "core_answer": "门外女人是父亲的亲生女儿。",
        "hints": ["a", "b", "c"],
        "facts": [{"id": "f1", "text": "门外女人是父亲的亲生女儿",
                   "kind": "core", "visibility": "hidden"},
                  {"id": "f2", "text": "她昨晚与父亲同桌吃饭",
                   "kind": "core", "visibility": "hidden"},
                  {"id": "f3", "text": "开门的人不认识她", "kind": "support"},
                  {"id": "f4", "text": "不是来讨债的", "kind": "exclusion"}],
        "completion_fact_ids": ["f1", "f2"],
        "solve_atoms": [
            {"id": "a1", "role": "key", "text": "门外女人是父亲的亲生女儿",
             "fact_ids": ["f1"]},
            {"id": "a2", "role": "key", "text": "她昨晚与父亲同桌吃饭",
             "fact_ids": ["f2"]}],
        "fair_clues": [{"quote": "开门的人一见她就愣住了",
                        "supports_atoms": ["a1"]}],
        "observed_signature": {
            "mechanism_family": "identity_misread",
            "solution_shape": "identity_reversal", "domain": "family",
            "emotion_mode": "warm", "relation": "family",
            "time_shape": "single_day", "death": False, "past_trauma": False,
            "long_term_profession": False, "repeated_ritual": False,
            "reveal_mode": "identity_flip",
            "procedural_rule_dependency": False},
        "quality_checks": {n: True for n in _QUALITY_CHECK_FIELDS},
    }
    d["quality_checks"].update(qc)
    return d


def test_case_i_narrator_truthfulness_fail_closed():
    print("\n[Case I] narrator truthfulness fail-closed")
    for v in (False, None):
        fc = FakeClient([LLMResult(tool_input=_pass_review(
            narrator_truthful=v), model="m")])
        w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
        spec = PuzzleSpec(puzzle="x?", answer="y")
        merged, why, rewrite = w._review_spec(spec)
        check(f"narrator_truthful={v!r} -> 整稿拒", merged is None, merged)
        check(f"  且判为淘汰({v!r})", rewrite is True, rewrite)
    # 全部为 true 时正常通过
    fc = FakeClient([LLMResult(tool_input=_pass_review(), model="m")])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    merged, why, rewrite = w._review_spec(PuzzleSpec(
        puzzle="门外站着一个女人, 开门的人一见她就愣住了。为什么?",
        answer="门外女人是父亲的亲生女儿。"))
    check("四项全 true -> 通过", merged is not None, why)


def test_case_j_mechanism_consistency_fail_closed():
    print("\n[Case J] mechanism consistency fail-closed")
    fc = FakeClient([LLMResult(tool_input=_pass_review(
        mechanism_consistent=False), model="m")])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    merged, why, rewrite = w._review_spec(PuzzleSpec(puzzle="x?", answer="y"))
    check("mechanism_consistent=false -> 整稿拒", merged is None, merged)
    check("判为淘汰", rewrite is True, rewrite)
    check("理由点名了这一项", "mechanism_consistent" in (why or ""), why)
    # 四项**缺失**也一样拒(不能靠"没回就当默认 true")
    fc2 = FakeClient([LLMResult(tool_input={
        "decision": "pass",
        "observed_signature": _pass_review()["observed_signature"]}, model="m")])
    w2 = PuzzleWriter(client=fc2, runtime_cfg=fc2.runtime_cfg)
    merged2, why2, _ = w2._review_spec(PuzzleSpec(puzzle="x?", answer="y"))
    check("quality_checks 整个缺失 -> 拒", merged2 is None, merged2)


# ----------------------------------------------------------------------
# Case K —— 确定性揭晓
# ----------------------------------------------------------------------
def test_case_k_deterministic_reveal():
    print("\n[Case K] core_answer 确定性揭晓, 逐字不改写")
    from director import Director
    core = "这是一次预设的测试飞行, 复飞本身就是考核项目。"
    answer = "完整解释: 机组在考核中复飞, 落地后机长才松口气, 乘客以为是特技。"
    text = Director._compose_reveal(core, answer)
    lines = text.split("\n")
    check("第一段标题是【核心答案】", lines[0] == "【核心答案】", lines[:2])
    check("第一段正文**逐字**是 core_answer", lines[1] == core, lines[1])
    check("core_answer 原样出现在文本里", core in text)
    check("第二段标题是【完整解释】", "【完整解释】" in text, text)
    check("answer 也带上了", answer in text, text)
    # answer == core 时不重复
    t2 = Director._compose_reveal(core, core)
    check("answer==core 时不重复贴", t2.count(core) == 1, t2)
    check("也不出现【完整解释】", "【完整解释】" not in t2, t2)
    # 不调 LLM: `_compose_reveal` 是 staticmethod, 只做字符串拼接
    check("是纯静态方法(无 client 依赖)",
          isinstance(Director.__dict__["_compose_reveal"], staticmethod))


# ----------------------------------------------------------------------
# Case L —— 运行时身份
# ----------------------------------------------------------------------
def test_case_l_runtime_identity():
    print("\n[Case L] runtime_spec_key 必须包含通关合同")
    f = [{"id": "f1", "text": "事实一"}, {"id": "f2", "text": "事实二"}]
    a = [{"id": "a1", "text": "原子一"}]
    base = runtime_spec_key("谜面", "谜底", f, a, [],
                            core_answer="核心答案 A",
                            completion_fact_ids=["f1"])
    check("只改 core_answer -> 换 key",
          runtime_spec_key("谜面", "谜底", f, a, [],
                           core_answer="核心答案 B",
                           completion_fact_ids=["f1"]) != base)
    check("只改 completion_fact_ids -> 换 key",
          runtime_spec_key("谜面", "谜底", f, a, [],
                           core_answer="核心答案 A",
                           completion_fact_ids=["f1", "f2"]) != base)
    check("completion 是集合语义(顺序无关)",
          runtime_spec_key("谜面", "谜底", f, a, [],
                           completion_fact_ids=["f2", "f1"])
          == runtime_spec_key("谜面", "谜底", f, a, [],
                              completion_fact_ids=["f1", "f2"]))
    check("空合同 != 有合同",
          runtime_spec_key("谜面", "谜底", f, a, []) != base)


# ======================================================================
# Closeout: blocker regressions
# ======================================================================
def test_closeout_b1_atom_floor_reaches_schema():
    """Blocker 1: v5 的 atom 下限必须贯穿 schema 与 prompt。

    代码允许 1 条, 但 schema 写 minItems=2 -> 模型永远不敢只交 1 条,
    于是"身份题只需要一条原子事实"这条改进在**生产里根本不会发生**。
    prompt / schema / code 三套说法必须一致。
    """
    print("\n[B1] atom 下限贯穿 schema / prompt")
    check("RIDDLE solve_atoms.minItems == 1",
          _TOOL_RIDDLE["input_schema"]["properties"]["solve_atoms"]["minItems"]
          == 1, _TOOL_RIDDLE["input_schema"]["properties"]["solve_atoms"])
    check("CHECK solve_atoms.minItems == 1",
          _TOOL_CHECK["input_schema"]["properties"]["solve_atoms"]["minItems"]
          == 1, _TOOL_CHECK["input_schema"]["properties"]["solve_atoms"])
    for name, txt in (("RIDDLE_SYSTEM", RIDDLE_SYSTEM),
                      ("CHECK_SYSTEM", CHECK_SYSTEM)):
        check(f"{name} 不再要求'必须恰好一条 cause 和一条 mechanism'",
              "必须恰好一条" not in txt)
    # 两处 description 都要讲清"不是胜利合同 / 可以只有 1 条 key"
    for label, tool in (("RIDDLE", _TOOL_RIDDLE), ("CHECK", _TOOL_CHECK)):
        d = tool["input_schema"]["properties"]["solve_atoms"]["description"]
        check(f"{label} desc 讲 1~4 条", "1~4" in d, d[:60])
        check(f"{label} desc 讲不是胜利合同", "不是胜利合同" in d
              or "不是玩家逐字通关的模板" in d, d[:60])
        check(f"{label} desc 讲可以只有 1 条 key", "key" in d, d[:60])
    # required 字段不再自称"是否通关必需"
    rd = _TOOL_RIDDLE["input_schema"]["properties"]["solve_atoms"][
        "items"]["properties"]["required"]["description"]
    check("RIDDLE required desc 不再说'通关必需'", "通关必需" not in rd, rd)
    check("RIDDLE required desc 指明不决定通关",
          "不决定" in rd and "completion_fact_ids" in rd, rd)


def test_closeout_b1_minimal_identity_puzzle_valid():
    """Blocker 1: 1 条 completion + 1 条 key atom 全链合法。"""
    print("\n[B1] 最小身份题(1 合同 + 1 key atom)全链合法")
    sp = PuzzleSpec(
        id="mini", title="门外",
        puzzle="门外站着一个女人, 开门的人一见她就愣住了。为什么?",
        answer="门外女人是父亲的亲生女儿。",
        core_answer="门外女人是父亲的亲生女儿。",
        completion_fact_ids=["f1"],
        facts=[PuzzleFact(id="f1", text="门外女人是父亲的亲生女儿",
                          kind="core", visibility="hidden"),
               PuzzleFact(id="f2", text="开门的人不认识她", kind="support")],
        solve_atoms=[SolveAtom(id="a1", role="key",
                               text="门外女人是父亲的亲生女儿",
                               fact_ids=["f1"])],
        fair_clues=[FairClue(quote="开门的人一见她就愣住了",
                             supports_atoms=["a1"])],
        hints=["a", "b", "c"],
        prompt_version="riddle-v5", quality_policy_version="quality-v5")
    vr = validate_spec(sp)
    check("validate_spec 通过", vr.ok, vr.why())
    check("只有 1 条 atom", len(sp.solve_atoms) == 1)
    check("只有 1 条合同", len(sp.completion_fact_ids) == 1)
    # 走一遍引擎: 这一条被确认就该通关
    eng, clk = boot(sp)
    ask(eng, clk, "u1", "甲", "她是父亲的女儿吗", verdict="是",
        established_fact_ids=["f1"])
    check("1 条合同被补齐 -> 揭晓", eng.phase == Phase.REVEALING, eng.phase)


def test_closeout_b2_v5_must_have_contract():
    """Blocker 2: quality-v5 标签**不能**配 legacy 通关语义。"""
    print("\n[B2] quality-v5 必须有完整合同")
    from story.quality import QUALITY_POLICY_VERSION as _Q
    # v5 标签 + 空合同 -> 拒
    sp = ident_spec()
    sp.completion_fact_ids = []
    vr = validate_spec(sp)
    check("v5 + 空 completion -> 拒", not vr.ok, vr.why())
    check("理由点名了版本门",
          "quality-v5" in vr.why() and "completion" in vr.why(), vr.why())
    # v5 标签 + 空 core_answer -> 拒
    sp2 = ident_spec()
    sp2.core_answer = ""
    vr2 = validate_spec(sp2)
    check("v5 + 空 core_answer -> 拒", not vr2.ok, vr2.why())
    # legacy(v4) + 空合同 -> 仍可通过旧 gate
    sp3 = ident_spec()
    sp3.quality_policy_version = "quality-v4"
    sp3.completion_fact_ids = []
    sp3.core_answer = ""
    sp3.solve_atoms = [
        SolveAtom(id="a1", role="cause", text="x", fact_ids=["f1"]),
        SolveAtom(id="a2", role="mechanism", text="y", fact_ids=["f2"]),
    ]
    vr3 = validate_spec(sp3)
    check("legacy 空合同 -> 旧 gate 仍可", vr3.ok, vr3.why())
    check("版本常量确实是 v5", _Q == "quality-v5", _Q)
    # 运行时"有没有合同"仍表示实际状态, 但准入层已保证 v5 必有合同
    check("has_completion_contract 仍是运行时判据",
          ident_spec().has_completion_contract())


def test_closeout_b2_pool_rejects_v5_without_contract():
    """Blocker 2: 题池必须同步 —— v5 无合同不能入池 / 不能出池。"""
    print("\n[B2] 题池: v5 无合同 -> 入池与出池都拒")
    from story.pool import PuzzlePool
    # add() 入口
    sp = ident_spec()
    sp.completion_fact_ids = []
    pool = PuzzlePool.__new__(PuzzlePool)
    ok, why = PuzzlePool._validate_pool_spec(sp)
    check("add/pop 统一门拒绝 v5 无合同", not ok, why)
    # legacy 完整旧题 -> quarantine(政策不匹配)
    sp2 = ident_spec()
    sp2.quality_policy_version = "quality-v4"
    ok2, why2 = PuzzlePool._validate_pool_spec(sp2)
    check("quality-v4 -> quarantine", not ok2, why2)
    check("理由点名政策不兼容", "不兼容" in why2, why2)
    # 空版本 -> 同样隔离, 不伪装成 v5 库存
    sp3 = ident_spec()
    sp3.quality_policy_version = ""
    ok3, why3 = PuzzlePool._validate_pool_spec(sp3)
    check("空版本 -> quarantine", not ok3, why3)
    # 真 v5 完整题 -> 通过。注意池门还会查 signature 完整性, 所以这里
    # 必须带上一份**完整** signature —— ident_spec 是给引擎用的最小构造,
    # 没有 signature, 会被另一条规则拦下(那不是本用例要测的东西)。
    from story.puzzle import PuzzleSignature
    full = ident_spec()
    full.signature = PuzzleSignature(
        mechanism_family="identity_misread",
        solution_shape="identity_reversal", domain="family",
        emotion_mode="warm", relation="family", time_shape="instant",
        reveal_mode="identity_flip")
    ok4, why4 = PuzzlePool._validate_pool_spec(full)
    check("完整 v5 题 -> 入池", ok4, why4)
    del pool


def test_closeout_b3_irrelevant_never_establishes():
    """Blocker 3: 「无关」绝不能建立事实 —— 否则刷无关即可白送通关。"""
    print("\n[B3] 「无关」不能建立事实")
    sp = ident_spec()   # 合同 = f1 + f2
    eng, clk = boot(sp)
    # 先用正常路径建立 f1
    ask(eng, clk, "u1", "甲", "第一条", verdict="是",
        established_fact_ids=["f1"])
    check("f1 已建立", eng._established_fact_ids == {"f1"},
          eng._established_fact_ids)
    # 关键攻击: 用「无关」去建立最后一条 completion fact
    ask(eng, clk, "u2", "乙", "随便问问", verdict="无关",
        established_fact_ids=["f2"])
    check("「无关」不建立事实", eng._established_fact_ids == {"f1"},
          eng._established_fact_ids)
    check("不通关", eng.phase == Phase.QA, eng.phase)
    check("胜者仍为空", not eng._solved_by, eng._solved_by)


def test_closeout_b3_verdict_matrix():
    """Blocker 3: verdict/status 矩阵 —— 只有 是/不是 能建立。"""
    print("\n[B3] established 的 verdict 矩阵")
    from story.parser import NO, UNAVAILABLE, YES
    cases = [
        (YES, "ok", True, "「是」可以建立"),
        (NO, "ok", True, "「不是」也可以建立"),
        ("无关", "ok", False, "「无关」不能建立"),
        (UNAVAILABLE, "unavailable", False, "「未判定」不能建立"),
        ("揭晓", "ok", False, "「揭晓」不能建立"),
    ]
    for verdict, status, should, label in cases:
        sp = ident_spec()
        eng, clk = boot(sp)
        ask(eng, clk, "u1", "甲", "问题", verdict=verdict, status=status,
            established_fact_ids=["f1", "f2"])
        got = eng._established_fact_ids == {"f1", "f2"}
        check(label, got is should, (verdict, status, eng._established_fact_ids))
        if should:
            check(f"  {label} -> 直接通关", eng.phase == Phase.REVEALING,
                  eng.phase)
        elif verdict == "揭晓":
            # 「揭晓」走的是 **legacy** P.SOLVE 路径(那是另一条规则, 不在
            # 本次范围内)。这里只断言它**没有建立 fact** —— 上面那条
            # `got is should` 已经覆盖了。
            pass
        else:
            check(f"  {label} -> 仍在 QA", eng.phase == Phase.QA, eng.phase)


def test_closeout_b4_clue_must_reach_completion():
    """Blocker 4: 至少一条 clue 指向通向 completion 的 atom。"""
    print("\n[B4] clue 必须真的指向通关路径")
    # 反例: 通关走 a1, 但唯一的 clue 指向无关的 a2
    bad = ident_spec()
    bad.solve_atoms = [
        SolveAtom(id="a1", role="key", text="门外女人是父亲的亲生女儿",
                  fact_ids=["f1"]),
        SolveAtom(id="a2", role="support", text="开门的人不认识她",
                  fact_ids=["f3"]),
    ]
    bad.fair_clues = [FairClue(quote="开门的人一见她就愣住了",
                               supports_atoms=["a2"])]   # 指不到 f1
    vr = validate_spec(bad)
    check("clue 指不到 completion -> 拒", not vr.ok, vr.why())
    check("理由点名了推理路径",
          "推理路径" in vr.why() or "不公平" in vr.why(), vr.why())
    # 正例: clue 指向 a1(它引用 f1)。
    # ⚠️ a2 必须仍然引用合同成员 f2 —— 否则"每条 completion fact 都要被
    # atom 引用"那条规则会先开火, 测的就不是 clue 路径了。
    good = ident_spec()
    good.solve_atoms = [
        SolveAtom(id="a1", role="key", text="门外女人是父亲的亲生女儿",
                  fact_ids=["f1"]),
        SolveAtom(id="a2", role="key", text="她昨晚与父亲同桌吃饭",
                  fact_ids=["f2"]),
    ]
    good.fair_clues = [FairClue(quote="开门的人一见她就愣住了",
                                supports_atoms=["a1"])]
    vr2 = validate_spec(good)
    check("clue 指向 completion atom -> 通过", vr2.ok, vr2.why())
    # 只要求"至少一条", 不要求每条 completion fact 都有 clue
    partial = ident_spec()   # 合同 f1+f2, 但只有一条 clue
    check("两条合同一条 clue -> 仍通过", validate_spec(partial).ok,
          validate_spec(partial).why())


def test_closeout_p1_reviewer_sync_fail_closed():
    """P1: reviewer 六样同步必须真的 fail-closed。"""
    print("\n[P1] reviewer 同步合同 fail-closed")
    import copy as _copy

    base = _pass_review()

    def run(ti):
        fc = FakeClient([LLMResult(tool_input=ti, model="m")])
        w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
        # 用一份**v5** spec 作为被审对象, 这样走的是 v5 全量分支
        sub_spec = ident_spec()
        return w._review_spec(sub_spec)[:1]

    # 1) pass + 只改 facts, 省略 solve_atoms -> 拒
    t1 = _copy.deepcopy(base)
    t1["facts"] = [dict(f, text=f["text"] + "改") for f in t1["facts"]]
    t1.pop("solve_atoms")
    merged, = run(t1)
    check("只改 facts 省略 atoms -> 拒", merged is None, merged)

    # 2) pass + 只改 completion_fact_ids, 省略 facts/atoms/clues -> 拒
    t2 = _copy.deepcopy(base)
    t2["completion_fact_ids"] = ["f1"]
    for k in ("facts", "solve_atoms", "fair_clues"):
        t2.pop(k)
    merged, = run(t2)
    check("只改合同省略 facts/atoms/clues -> 拒", merged is None, merged)

    # 3) fix + 改 core_answer, 缺 completion/facts/atoms/clues -> 拒
    t3 = _copy.deepcopy(base)
    t3["decision"] = "fix"
    t3["core_answer"] = "换了核心答案。"
    for k in ("completion_fact_ids", "facts", "solve_atoms", "fair_clues"):
        t3.pop(k)
    merged, = run(t3)
    check("fix 改 core_answer 缺其余 -> 拒", merged is None, merged)

    # 4) 完整 bundle -> 通过
    merged, = run(_copy.deepcopy(base))
    check("完整 bundle -> 通过", merged is not None, merged)
    if merged is not None:
        check("  新 spec 带上合同", merged.completion_fact_ids == ["f1", "f2"],
              merged.completion_fact_ids)
        check("  新 spec 带上 core_answer",
              merged.core_answer == base["core_answer"], merged.core_answer)


def test_closeout_p1_legacy_reviewer_unchanged():
    """P1: legacy(v4)审稿路径保持"改了才要求齐全", 不被 v5 规则误伤。"""
    print("\n[P1] legacy reviewer 不受 v5 全量要求影响")
    fc = FakeClient([LLMResult(tool_input={
        "decision": "pass",
        "observed_signature": _pass_review()["observed_signature"],
        "quality_checks": _pass_review()["quality_checks"],
    }, model="m")])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    legacy = ident_spec()
    legacy.quality_policy_version = "quality-v4"
    legacy.completion_fact_ids = []
    merged, why, rewrite = w._review_spec(legacy)
    check("legacy: 没改就沿用 -> 不因缺 bundle 被拒",
          not (merged is None and "同步合同" in (why or "")), (merged, why))


def test_final_closeout_v5_empty_values_rejected():
    """Blocker: **显式空值**也不能绕过 v5 同步合同。

    `if ti.get(name) is None` 只拦得住"key 缺失"。审稿人完全可以回一个
    **存在但为空**的字段(`core_answer=""` / `facts=[]` / ...), 而下面的
    legacy 兼容逻辑会把它当成"没给", 偷偷沿用旧 spec 内容 —— 混合版本稿
    照样通过。

    这一组逐个把字段置成空值(而不是 pop 掉 key), 全部必须 reject。
    """
    print("\n[final] v5 显式空值 -> 必须 reject")
    import copy as _copy

    base = _pass_review()

    def run(ti):
        fc = FakeClient([LLMResult(tool_input=ti, model="m")])
        w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
        return w._review_spec(ident_spec())

    # ---- 文本字段置空 ----
    for field in ("puzzle", "answer", "core_answer"):
        t = _copy.deepcopy(base)
        t[field] = ""
        merged, why, rewrite = run(t)
        check(f"{field}='' -> 拒", merged is None, (field, merged))
        check(f"  {field} 的拒绝理由点名空/无效",
              "为空/无效" in (why or ""), why)
        # 纯空白也算空
        t2 = _copy.deepcopy(base)
        t2[field] = "   "
        merged2, why2, _ = run(t2)
        check(f"{field}='   ' -> 拒", merged2 is None, (field, merged2))

    # ---- 列表字段置空 ----
    for field in ("completion_fact_ids", "facts", "solve_atoms", "fair_clues"):
        t = _copy.deepcopy(base)
        t[field] = []
        merged, why, rewrite = run(t)
        check(f"{field}=[] -> 拒", merged is None, (field, merged))
        check(f"  {field} 的拒绝理由点名空/无效",
              "为空/无效" in (why or ""), why)

    # ---- 类型错误也算无效 ----
    t3 = _copy.deepcopy(base)
    t3["facts"] = "不是列表"
    merged3, why3, _ = run(t3)
    check("facts 类型错误 -> 拒", merged3 is None, merged3)
    t4 = _copy.deepcopy(base)
    t4["core_answer"] = 123
    merged4, why4, _ = run(t4)
    check("core_answer 类型错误 -> 拒", merged4 is None, merged4)

    # ---- 正例: 完整非空 bundle 仍通过 ----
    merged5, why5, _ = run(_copy.deepcopy(base))
    check("完整非空 bundle -> 通过", merged5 is not None, (merged5, why5))
    if merged5 is not None:
        check("  用的是**新** facts(没被旧值覆盖)",
              [f.text for f in merged5.facts] ==
              [f["text"] for f in base["facts"]], merged5.facts)
        check("  用的是**新** core_answer",
              merged5.core_answer == base["core_answer"], merged5.core_answer)
        check("  用的是**新** completion_fact_ids",
              merged5.completion_fact_ids == base["completion_fact_ids"],
              merged5.completion_fact_ids)


def test_final_closeout_legacy_fallback_still_works():
    """legacy(v4)审稿路径必须保持"空了就沿用旧值", 不被 v5 严格化误伤。"""
    print("\n[final] legacy 仍允许沿用旧值")
    base = _pass_review()
    # 只回 decision + observed_signature + quality_checks, 其余全省略:
    # v4 的合法形态(没改就原样沿用)。
    legacy = ident_spec()
    legacy.quality_policy_version = "quality-v4"
    legacy.completion_fact_ids = []
    # v4 的合法"没改"形态: pass + 原样回传谜面谜底, 其余省略由代码沿用。
    # **必须**把 puzzle/answer 原样带回 —— 若不回,  会为真
    # (空串 != 旧谜面), 于是要求整套同步。那是 v4 的既有语义, 与本批无关。
    legacy_ti = {
        "decision": "pass",
        "puzzle": legacy.puzzle,
        "answer": legacy.answer,
        "observed_signature": base["observed_signature"],
        "quality_checks": base["quality_checks"],
    }
    fc = FakeClient([LLMResult(tool_input=legacy_ti, model="m")])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    merged, why, rewrite = w._review_spec(legacy)
    check("legacy 省略字段 -> 走 fallback, 不因空值被拒",
          not (merged is None and "为空/无效" in (why or "")), (merged, why))
    check("legacy 仍能返回 spec", merged is not None, why)
    if merged is not None:
        check("  沿用了旧 facts", len(merged.facts) == len(legacy.facts),
              merged.facts)


def test_final_closeout_status_must_be_ok():
    """P1: established 的 status 必须**明确是 ok**(fail closed)。"""
    print("\n[final] status 必须明确 ok")
    from story.parser import NO, UNAVAILABLE, YES
    cases = [
        (YES, "ok", True, "是 + ok -> 建立"),
        (NO, "ok", True, "不是 + ok -> 建立"),
        ("无关", "ok", False, "无关 + ok -> 不建立"),
        (UNAVAILABLE, "unavailable", False, "未判定 + unavailable -> 不建立"),
        (YES, "unavailable", False, "是 + unavailable -> 不建立"),
        (YES, "error", False, "是 + error -> 不建立(fail closed)"),
        (YES, "", False, "是 + 空 status -> 不建立(fail closed)"),
        (YES, "future_status", False,
         "是 + 未知状态 -> 不建立(fail closed)"),
        (NO, "error", False, "不是 + error -> 不建立"),
    ]
    for verdict, status, should, label in cases:
        eng, clk = boot(ident_spec())
        ask(eng, clk, "u1", "甲", "问题", verdict=verdict, status=status,
            established_fact_ids=["f1", "f2"])
        got = eng._established_fact_ids == {"f1", "f2"}
        check(label, got is should, (verdict, status, eng._established_fact_ids))
        if should:
            check(f"  {label} -> 通关", eng.phase == Phase.REVEALING, eng.phase)
        else:
            check(f"  {label} -> 未通关", eng.phase != Phase.REVEALING,
                  eng.phase)
    # 默认参数(status 不传, QAResult 默认 "ok")必须仍能建立 ——
    # 否则这条 fail-closed 会把正常路径一起关掉。
    eng2, clk2 = boot(ident_spec())
    ask(eng2, clk2, "u1", "甲", "问题", verdict=YES,
        established_fact_ids=["f1", "f2"])
    check("不传 status(默认 ok)-> 仍能建立并通关",
          eng2.phase == Phase.REVEALING, eng2.phase)



# ----------------------------------------------------------------------
# 四句话产品语义 —— 与 prompt / schema 的一致性
# ----------------------------------------------------------------------
def test_product_semantics_pinned():
    print("\n[语义] 四句话必须与 prompt / schema 一致")
    # 1. 谜面可以误导, 但不能撒谎
    check("RIDDLE_SYSTEM 有'陈述必须为真'",
          "陈述" in RIDDLE_SYSTEM and "为真" in RIDDLE_SYSTEM)
    check("RIDDLE_SYSTEM 给出无归属断言的反例",
          "绝不可能听到那句话" in RIDDLE_SYSTEM)
    check("RIDDLE_SYSTEM 允许有归属的陈述", "在他看来" in RIDDLE_SYSTEM)
    # 2. 谜底必须能先用一句话说清
    check("RIDDLE 工具要求 core_answer",
          "core_answer" in _TOOL_RIDDLE["input_schema"]["properties"])
    check("core_answer 是必填",
          "core_answer" in _TOOL_RIDDLE["input_schema"]["required"])
    check("CHECK 工具也回 core_answer",
          "core_answer" in _TOOL_CHECK["input_schema"]["properties"])
    # 3. 房间共同解谜 —— ANSWER 必须报告 established
    item = _TOOL_ANSWER["input_schema"]["properties"]["answers"]["items"]
    check("ANSWER 工具带 established_fact_ids",
          "established_fact_ids" in item["properties"])
    check("established_fact_ids 是必填",
          "established_fact_ids" in item["required"], item["required"])
    check("ANSWER_SYSTEM 解释 established 语义",
          "established_fact_ids" in ANSWER_SYSTEM)
    check("ANSWER_SYSTEM 有正反两个例子",
          ANSWER_SYSTEM.count("例 1") == 1 and ANSWER_SYSTEM.count("例 2") == 1)
    # 4. 补齐即揭晓 —— 合同字段进工具 schema
    check("RIDDLE 工具带 completion_fact_ids",
          "completion_fact_ids" in _TOOL_RIDDLE["input_schema"]["properties"])
    check("completion_fact_ids 是必填",
          "completion_fact_ids" in _TOOL_RIDDLE["input_schema"]["required"])
    check("CHECK 工具带 completion_fact_ids",
          "completion_fact_ids" in _TOOL_CHECK["input_schema"]["properties"])
    # Reviewer 四项检查进 schema 且必填
    qc = _TOOL_CHECK["input_schema"]["properties"]["quality_checks"]
    check("quality_checks 四项齐全",
          set(qc["properties"]) == set(_QUALITY_CHECK_FIELDS),
          sorted(qc["properties"]))
    check("quality_checks 四项都必填",
          set(qc["required"]) == set(_QUALITY_CHECK_FIELDS), qc["required"])
    check("CHECK_SYSTEM 讲了方向检查", "方向" in CHECK_SYSTEM)
    check("CHECK_SYSTEM 讲了 narrator truthfulness",
          "narrator_truthful" in CHECK_SYSTEM)


# ----------------------------------------------------------------------
# established 的落盘边界
# ----------------------------------------------------------------------
def test_established_archive_boundary():
    print("\n[边界] established 落盘但不外露")
    r = QARec(qid=1, user_name="甲", text="x", verdict="是",
              established_fact_ids=["f1"], touched_fact_ids=["f1"])
    check("to_json 不含 established", "established_fact_ids" not in r.to_json())
    check("to_archive 含 established",
          r.to_archive().get("established_fact_ids") == ["f1"])
    check("to_json 也不含 touched", "touched_fact_ids" not in r.to_json())
    # 端到端: 真人的 established 必须进 qa_archive
    eng, clk = boot(ident_spec())
    ask(eng, clk, "u1", "甲", "第一条", verdict="是",
        established_fact_ids=["f1"])
    recs = [x for x in eng._qa_archive if x.kind == "qa"]
    check("qa_archive 带 established",
          recs and recs[-1].established_fact_ids == ["f1"],
          recs[-1].established_fact_ids if recs else None)


def main():
    tests = [
        test_case_a_collective_identity,
        test_case_b_flight_test,
        test_case_c_touched_is_not_established,
        test_case_d_illegal_fact_id,
        test_case_m_human_only,
        test_case_e_support_exclusion_rejected,
        test_case_f_completion_capped,
        test_case_g_v5_no_final_judge,
        test_case_h_legacy_not_degraded,
        test_case_i_narrator_truthfulness_fail_closed,
        test_case_j_mechanism_consistency_fail_closed,
        test_case_k_deterministic_reveal,
        test_case_l_runtime_identity,
        # ---- Closeout: 4 blockers + P1 ----
        test_closeout_b1_atom_floor_reaches_schema,
        test_closeout_b1_minimal_identity_puzzle_valid,
        test_closeout_b2_v5_must_have_contract,
        test_closeout_b2_pool_rejects_v5_without_contract,
        test_closeout_b3_irrelevant_never_establishes,
        test_closeout_b3_verdict_matrix,
        test_closeout_b4_clue_must_reach_completion,
        test_closeout_p1_reviewer_sync_fail_closed,
        test_closeout_p1_legacy_reviewer_unchanged,
        # ---- Final closeout ----
        test_final_closeout_v5_empty_values_rejected,
        test_final_closeout_legacy_fallback_still_works,
        test_final_closeout_status_must_be_ok,
        test_product_semantics_pinned,
        test_established_archive_boundary,
    ]
    for t in tests:
        t()
    print()
    if FAIL[0]:
        print(f"FAILED: {FAIL[0]} 项")
        return 1
    print("PASS: Solve UX + Puzzle Truthfulness 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
