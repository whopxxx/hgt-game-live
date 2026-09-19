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
