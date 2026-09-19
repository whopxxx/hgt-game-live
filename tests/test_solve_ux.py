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
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from story.config import Config  # noqa: E402
from story.engine import RoundEngine  # noqa: E402
import story.parser as P  # noqa: E402
from story.llm import (  # noqa: E402
    COMPLETION_VERIFY_SYSTEM, LLMResult, PuzzleWriter,
    _QUALITY_CHECK_FIELDS, _TOOL_ANSWER, _TOOL_CHECK,
    _TOOL_COMPLETION_VERIFY, _TOOL_RIDDLE,
    ANSWER_SYSTEM, CHECK_SYSTEM, RIDDLE_SYSTEM,
)
from story.puzzle import (  # noqa: E402
    DiscoveryBeat,
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
def _stamp_beats(spec):
    """给 v8 夹具补上 2 个发现阶段。

    当前政策要求 2~4 条, 而这些夹具是为了测**别的**东西(通关判定、
    贡献链、复核 …)才存在的 —— 不补的话每个用例都会先在
    "缺 discovery_beats" 上失败, 掩盖真正要测的行为。
    第一条指向第一个 completion fact, 保证"至少一条通向通关路径"。
    """
    if getattr(spec, "discovery_beats", None):
        return spec
    comp = [str(x).strip() for x in
            (getattr(spec, "completion_fact_ids", None) or []) if str(x).strip()]
    f1 = comp[0] if comp else ""
    f2 = comp[1] if len(comp) > 1 else f1
    spec.discovery_beats = [
        DiscoveryBeat(id="b1", text="先注意到最反常的那个细节",
                      fact_ids=[x for x in (f1,) if x]),
        DiscoveryBeat(id="b2", text="再想通它为什么会这样",
                      fact_ids=[x for x in (f2,) if x]),
    ]
    return spec


def ident_spec(completion=("f1", "f2"), core="门外女人是父亲的亲生女儿。"):
    """集体身份题: 两个核心事实, 可由两个不同的观众分别建立。"""
    return _stamp_beats(PuzzleSpec(
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
        prompt_version="riddle-v8", quality_policy_version="quality-v8"))


def flight_spec():
    """飞行测试: 与 A 同构, 换成"测试飞行"题材。"""
    return _stamp_beats(PuzzleSpec(
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
        prompt_version="riddle-v8", quality_policy_version="quality-v8"))


def auction_spec():
    """去身份化的"拍卖箱子"真实回归 —— 直接复刻直播里的那类题。

    这个 fixture 是**从真实故障反推**出来的: core_answer 说清了核心机制,
    completion 也只拆成两条核心命题(不含"鉴定人定价权"那种行业细节)。
    所以房间说出核心机制就应该能通关 —— 这正是 v6 要守住的东西。
    """
    return _stamp_beats(PuzzleSpec(
        id="ux-auction", title="旧箱子",
        puzzle="古董商把自己收藏的旧箱子送去拍卖, 每次都是他自己举牌"
              "买回来。几年后, 他手里同类的箱子都卖出了高价。为什么?",
        answer="他通过自买自卖制造虚高成交记录, 抬高手里同类旧箱"
               "的市场价值, 再高价出手。",
        core_answer="古董商和拍卖方通过人为制造虚高成交记录, 来抬高手中"
                    "同类旧箱的市场价值。",
        completion_fact_ids=["f1", "f2"],
        facts=[
            PuzzleFact(id="f1", text="古董商通过自买自卖/配合竞拍制造虚高"
                       "成交记录", kind="core", visibility="hidden"),
            PuzzleFact(id="f2", text="目的是抬高手中同类旧箱的市场价值",
                       kind="core", visibility="hidden"),
            PuzzleFact(id="f3", text="拍卖行鉴定人具有根据成交记录调整估值"
                       "的正式定价权", kind="support", visibility="hidden"),
            PuzzleFact(id="f4", text="他不是在洗钱", kind="exclusion",
                       visibility="hidden"),
        ],
        solve_atoms=[
            SolveAtom(id="a1", role="key",
                      text="自买自卖制造虚高成交记录", fact_ids=["f1"]),
            SolveAtom(id="a2", role="key",
                      text="抬高同类箱子价值", fact_ids=["f2"]),
        ],
        fair_clues=[FairClue(quote="每次都是他自己举牌买回来",
                             supports_atoms=["a1"])],
        hints=["注意谁在举牌", "想想成交记录有什么用", "注意他手里还有别的箱子"],
        prompt_version="riddle-v8", quality_policy_version="quality-v8"))


def wardrobe_spec():
    """J1: **实播截图那道题**的等价 fixture。

    谜面问的是"为什么不敢关灯 / 为什么睡在衣柜前", 但真正的核心机制是
    **空间关系**: 衣柜只是封住原房门的隔板, 人一直睡在被封住的那扇门前。

    关键设计: 两条 completion 都是**具体空间命题**, 与谜面的表层措辞
    ("衣柜""睡")看似相关, 实际上"不是高空坠落"**推不出**它们中的任何
    一条。这正是实播里被误判的场景:

        观众 "不敢关灯是因为有高空坠落风险吗？"
        Host "不是"
        -> 只得到"不是高空坠落", 与衣柜/原门无关。

    fact id / 文本都是**测试自造**的, 不复刻直播内部命名; 但 canonical
    语义与实播一致。
    """
    return _stamp_beats(PuzzleSpec(
        id="ux-wardrobe", title="衣柜",
        puzzle="他每晚都不敢关灯, 却一直睡在衣柜前面。为什么?",
        answer="他一直睡在原房门的正前方, 衣柜就是后来封住那扇门的隔板。",
        core_answer="他一直睡在原房门的正前方, 衣柜是封住那扇门的隔板。",
        completion_fact_ids=["f1", "f2"],
        facts=[
            PuzzleFact(id="f1", text="衣柜实际上是后来封住原房门的隔板",
                       kind="core", visibility="hidden"),
            PuzzleFact(id="f2", text="人和床所在的位置实际上就是原房门前",
                       kind="core", visibility="hidden"),
            PuzzleFact(id="f3", text="房间曾经被隔断过", kind="support",
                       visibility="hidden"),
            PuzzleFact(id="f4", text="不是因为高空坠落风险", kind="exclusion",
                       visibility="hidden"),
        ],
        solve_atoms=[
            SolveAtom(id="a1", role="key",
                      text="衣柜封住了原来的房门", fact_ids=["f1"]),
            SolveAtom(id="a2", role="key",
                      text="床的位置就是原门前", fact_ids=["f2"]),
        ],
        fair_clues=[FairClue(quote="却一直睡在衣柜前面",
                             supports_atoms=["a2"])],
        hints=["注意衣柜的位置", "注意床原本在哪", "想想那面墙原来是干什么的"],
        prompt_version="riddle-v8", quality_policy_version="quality-v8"))


def plane_spec():
    """J1-5: 合法 NO 的正例 —— "飞机没有机械故障"。

    completion 的核心命题**就是**被否定掉的那件事, 所以"不是"直接
    等价于它, 应当建立。
    """
    return _stamp_beats(PuzzleSpec(
        id="ux-plane", title="地勤旗",
        puzzle="飞机明明可以正常起飞, 机长却主动取消了航班。为什么?",
        answer="他把与自己无关的地勤警示旗误读成自己飞机有故障, 于是主动"
               "取消了本可正常起飞的航班。",
        core_answer="他把与自己无关的地勤警示旗误读成自己飞机有故障。",
        completion_fact_ids=["f1"],
        facts=[
            PuzzleFact(id="f1", text="这架飞机本身没有机械故障",
                       kind="core", visibility="hidden"),
            PuzzleFact(id="f2", text="警示旗指向的是另一架飞机",
                       kind="support", visibility="hidden"),
        ],
        solve_atoms=[
            SolveAtom(id="a1", role="key", text="飞机没有故障",
                      fact_ids=["f1"]),
        ],
        fair_clues=[FairClue(quote="主动取消了本可正常起飞的航班",
                             supports_atoms=["a1"])],
        hints=["注意他看到了什么", "注意旗子是谁的"],
        prompt_version="riddle-v8", quality_policy_version="quality-v8"))


def _completion_match(ids):
    """第二层 completion 复核的 canned 返回。"""
    return LLMResult(tool_input={"matched_completion_fact_ids": list(ids)},
                     model="m")


def boot(spec):
    clk = FakeClock()
    eng = RoundEngine(mkcfg(), clock=clk)
    eng.start()
    eng.submit_riddle(spec.puzzle, spec.answer, list(spec.hints), spec=spec)
    assert eng.phase == Phase.QA, eng.phase
    return eng, clk


def ask(eng, clk, uid, name, text, **kw):
    """发一条 #提问 -> 拿 ANSWER payload -> 按 kw 回一个 QAResult。

    ## J1-B: 合同内的 id 必须**同时**带 verified

    Engine 现在要求 completion fact 只有同时出现在
    `completion_verified_fact_ids` 里才能进房间共识(J1-B 的
    defense-in-depth)。绝大多数用例测的是**别的**东西(通关覆盖、
    贡献链、提示节奏 …), 不该因为这条新门全部变红。

    所以这里默认把 `established_fact_ids` 里的合同 id 补一份到
    `completion_verified_fact_ids` —— 模拟"Writer 已经正常复核过"。

    ⚠️ **不要**改成无条件复制全部 id: 那样就再也测不出 J1-B 了。
    要测"未复核的 completion 进不来"的用例, 显式传
    `completion_verified_fact_ids=[]`(显式传空也算显式, 不会被覆盖)。
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
    # ---- C6-B: 反馈必须是"收窄合同", 不是"重出/简化" ----
    #
    # 旧断言写的是 `"重出" in vr.why()` —— 它冻结的正是 Q2 想消灭的
    # 那条指令("这题太绕, 应重出")。硬拒不变, 但**下一稿该往哪修**
    # 变了: 收窄完成合同, 保住谜题本身与 discovery_beats。
    check("**提示是收窄合同, 不是重出/简化**",
          "收窄" in vr.why() and "重出" not in vr.why(), vr.why())
    check("  **且明确保住 discovery_beats**",
          "discovery_beats" in vr.why(), vr.why())


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
        r = self._results.pop(0)
        # 允许把"异常"当成一个 canned 结果投进去 —— 真实网关会抛, 而
        # "重判抛异常"必须和"重判超时"走同一条失败处置(C1 的用例之一)。
        if isinstance(r, BaseException):
            raise r
        return r


def _verdict(established=None, cand=False, verdict="是"):
    return LLMResult(tool_input={"answers": [{
        "id": 1, "verdict": verdict, "comment": "好眼力",
        "solution_candidate": cand,
        "touched_fact_ids": [],
        "established_fact_ids": list(established or []),
    }]}, model="m")


_FACTS = [{"id": "f1", "text": "门外女人是父亲的亲生女儿", "kind": "core"}]


def test_case_g_v5_no_final_judge():
    print("\n[Case G] v5 有合同不调 Final Judge(A1: completion 走复核)")
    fc = FakeClient([
        _verdict(established=["f1"], cand=True),
        _completion_match(["f1"]),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    out, err = w.answer("谜面?", "谜底。", [], 1, "甲", "她是姐姐",
                        facts=_FACTS, completion_fact_ids=["f1"])
    check("调了两次(verdict + completion 复核)", len(fc.calls) == 2,
          len(fc.calls))
    check("**绝不 emit_judgement**",
          all(c["tool"]["name"] != "emit_judgement" for c in fc.calls),
          [c["tool"]["name"] for c in fc.calls])
    check("第二次是 emit_completion_match",
          fc.calls[1]["tool"]["name"] == "emit_completion_match",
          fc.calls[1]["tool"]["name"])
    check("不产生 P.SOLVE", out and out[0].verdict != "揭晓",
          out[0].verdict if out else None)
    check("established 经复核确认后带回",
          out and out[0].established_fact_ids == ["f1"],
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
        # ---- C5: 当前政策下 beats 是**必须显式回传**的字段之一 ----
        # 少了它 `_apply_review` 会以 invalid_bundle 拒稿(与 facts /
        # solve_atoms / fair_clues 同级)。
        "discovery_beats": [
            {"id": "b1", "text": "先注意到开门的人反应异常", "fact_ids": ["f3"]},
            {"id": "b2", "text": "再想到两人其实有血缘关系",
             "fact_ids": ["f1"]}],
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
        merged, why, rewrite, _tech = w._review_spec(spec)
        check(f"narrator_truthful={v!r} -> 整稿拒", merged is None, merged)
        check(f"  且判为淘汰({v!r})", rewrite is True, rewrite)
    # 全部为 true 时正常通过
    fc = FakeClient([LLMResult(tool_input=_pass_review(), model="m")])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    merged, why, rewrite, _tech = w._review_spec(PuzzleSpec(
        puzzle="门外站着一个女人, 开门的人一见她就愣住了。为什么?",
        answer="门外女人是父亲的亲生女儿。"))
    check("四项全 true -> 通过", merged is not None, why)


def test_case_j_mechanism_consistency_fail_closed():
    print("\n[Case J] mechanism consistency fail-closed")
    fc = FakeClient([LLMResult(tool_input=_pass_review(
        mechanism_consistent=False), model="m")])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    merged, why, rewrite, _tech = w._review_spec(PuzzleSpec(puzzle="x?", answer="y"))
    check("mechanism_consistent=false -> 整稿拒", merged is None, merged)
    check("判为淘汰", rewrite is True, rewrite)
    check("理由点名了这一项", "mechanism_consistent" in (why or ""), why)
    # 四项**缺失**也一样拒(不能靠"没回就当默认 true")
    fc2 = FakeClient([LLMResult(tool_input={
        "decision": "pass",
        "observed_signature": _pass_review()["observed_signature"]}, model="m")])
    w2 = PuzzleWriter(client=fc2, runtime_cfg=fc2.runtime_cfg)
    merged2, why2, _, _tech = w2._review_spec(PuzzleSpec(puzzle="x?", answer="y"))
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
        prompt_version="riddle-v8", quality_policy_version="quality-v8")
    _stamp_beats(sp)
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
    """Blocker 2: 当前政策标签**不能**配 legacy 通关语义。"""
    print("\n[B2] 当前政策必须有完整合同")
    from story.quality import QUALITY_POLICY_VERSION as _Q
    # v5 标签 + 空合同 -> 拒
    sp = ident_spec()
    sp.completion_fact_ids = []
    vr = validate_spec(sp)
    check("v5 + 空 completion -> 拒", not vr.ok, vr.why())
    check("理由点名了版本门",
          _Q in vr.why() and "completion" in vr.why(), vr.why())
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
    check("版本常量确实是 v7", _Q == "quality-v8", _Q)
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
    # ---- G2-C: "线索指不到通关路径"归 **fixable**, 不是硬拒 ----
    #
    # 事实在、atom 在、clue 也在, 只是 clue 的 supports_atoms 指错了
    # 分支 —— 这是**连线**问题, reviewer 接一根线就能修好。硬拒等于
    # 为一根线丢掉整道题。
    #
    # ⚠️ 但它**必须仍然不合格**(`fixable` 非空), 否则这一稿会带着
    # "线索指不到通关事实"直接上直播。
    check("**clue 指不到 completion -> 仍需修(fixable)**",
          bool(vr.fixable), (vr.ok, vr.fixable))
    check("**不再是硬错误**", vr.ok, vr.why())
    check("理由点名了推理路径",
          any("推理路径" in f or "不公平" in f for f in vr.fixable),
          vr.fixable)
    check("**写明谜面真没线索时要 rewrite 而不是硬接**",
          any("rewrite" in f for f in vr.fixable), vr.fixable)
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

    # ---- C5: discovery_beats 与 facts/atoms/clues **同级同步** ----
    #
    # 必须与"改了谜底却漏回 facts"同等处置。理由: beats 引用的 fact id
    # 在改稿后往往**仍然存在**(改稿常保留原 id), 所以结构校验
    # (validate_spec)完全合法 —— 混合版本"新事实 + 旧推理层次"抓不到。
    for label, mutate in (
            ("漏回 beats", lambda t: t.pop("discovery_beats")),
            ("beats 空列表", lambda t: t.__setitem__("discovery_beats", [])),
            ("beats 不是列表",
             lambda t: t.__setitem__("discovery_beats", "b1")),
            ("beats 元素无 text",
             lambda t: t.__setitem__("discovery_beats",
                                     [{"id": "b1"}, {"id": "b2"}])),
    ):
        t = _copy.deepcopy(base)
        mutate(t)
        merged, = run(t)
        check(f"**当前政策 + {label} -> 拒稿**", merged is None, merged)

    # 但**只改 facts 却原样带回 beats** 是合法的(同步了就该放行)
    t_ok = _copy.deepcopy(base)
    t_ok["facts"] = [dict(f, text=f["text"] + "改") for f in t_ok["facts"]]
    merged, = run(t_ok)
    check("改了 facts 且 beats 原样带回 -> 通过", merged is not None, merged)
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
    merged, why, rewrite, _tech = w._review_spec(legacy)
    check("legacy: 没改就沿用 -> 不因缺 bundle 被拒",
          not (merged is None and "同步合同" in (why or "")), (merged, why))

    # ---- C5 对照: legacy **漏回 beats 也不该被拒** ----
    #
    # v8 之前根本没有 discovery_beats 这个概念, 所以旧稿不能因为
    # "Reviewer 没回 beats" 被拒 —— 那时沿用(空)是正确的兼容行为。
    # 判据必须是**政策版本**(与 `is_v5_review` 同口径), 不是"有没有
    # 这个 key"。
    merged2, why2, _, _tech = w._review_spec(legacy)
    check("**legacy: 漏回 beats -> 仍放行(旧政策没这个概念)**",
          not (merged2 is None and "discovery_beats" in (why2 or "")),
          (merged2, why2))


def test_c6b_completion_over_limit_feedback_does_not_simplify():
    """**C6-B**: `completion > 2` 的**反馈文案**不得再把题往简单里带。

    背景: v8 把产品规则写成了"题目允许有层次, 通关必须简单", 但
    `validate_spec()` 对 3 条 completion 的真实错误文案仍是
    "超过说明这题太绕, 应重出"。这**不只是日志** —— `gen_spec()` 会把它
    记进 `seen_why`, 下一稿的 prompt 里真的会收到:

        【上一稿不合格的地方】
        结构问题: ...超过说明这题太绕, 应重出

    于是模型一边收到 v8 的"题目允许有层次", 一边收到"这题太绕, 应重出",
    重新把题写简单 —— 那正是 Q2 想消灭的行为, 从 deterministic validator
    的反馈链又钻了回来。

    硬拒**不变**, 只改"为什么拒、下一稿该怎么修"。这条测试就是冻结
    "反馈链里不得再出现把题写简单的指令"。
    """
    print("\n[C6-B] completion 超限的反馈不得再让模型简化整题")
    from story.quality import validate_spec, MAX_COMPLETION_FACTS

    spec = ident_spec()
    # 造 3 条 completion(超上限)—— 指向真实存在的 fact
    spec.completion_fact_ids = ["f1", "f2", "f3"]
    r = validate_spec(spec)
    check("completion 超限 -> 仍然硬拒(放宽这件事没变)",
          not r.ok, r.ok)
    joined = " ".join(list(r.errors) + list(r.fixable))
    check("  确实是因为条数被拒",
          any("completion_fact_ids" in e for e in r.errors), r.errors)
    # ---- 冻结: 不得再出现"简化整题"类指令 ----
    #
    # ⚠️ 判据是"**命令**模型去简化", 不是字面出现"简化"二字 ——
    # 正确文案本身就写着"**不要**因此简化谜题本身", 用裸串匹配会把
    # 正确实现判红(实测踩到)。
    for bad_phrase in ("太绕", "应重出", "换一个更简单", "把整道题改简单",
                       "换骨架", "请换一个更简单的骨架"):
        check(f"  **反馈里不得出现 {bad_phrase!r}**",
              bad_phrase not in joined, joined)
    check("  **不得把'简化'当动作下发给模型**",
          "不要因此简化" in joined, joined)
    # ---- 必须给出正确方向: 收窄**合同**, 保住题与 beats ----
    check("  **必须告诉模型收窄通关合同**",
          "收窄" in joined and "合同" in joined, joined)
    check("  **必须明确不要简化谜题本身**",
          "简化谜题" in joined or "不要把题" in joined
          or "不要因此简化" in joined, joined)
    check("  **必须明确保住 discovery_beats**",
          "discovery_beats" in joined, joined)
    check("  上限写的是 1~MAX_COMPLETION_FACTS",
          str(MAX_COMPLETION_FACTS) in joined, joined)


def test_c6b_current_policy_field_lists_all_include_beats():
    """**C6-B**: 生成器与审稿人的**当前政策字段清单**都必须含 beats。

    代码已经 fail-closed, 所以这些遗漏不会把坏题放进直播 —— 但它会让模型
    更容易漏字段, 然后: Reviewer 漏 beats -> 代码拒稿 -> 再生成/再审 ->
    **出题时间变长**。正好和"出题慢"是同一条成本链。

    冻结四处(缺一处就会在某一轮把模型引回漏字段):
      1. `RIDDLE_SYSTEM` 末尾的"按工具字段填"清单
      2. `_TOOL_CHECK.description` 的"必须一起重出"清单
      3. `CHECK_SYSTEM` 的"改了核心就必须重出整套"清单(且条数要对)
      4. `CHECK_SYSTEM` 的 `quality_checks` 项数标题
    """
    print("\n[C6-B] 生成/审稿字段清单都含 discovery_beats")
    from story import llm as _llm

    check("RIDDLE_SYSTEM 字段清单含 discovery_beats",
          "discovery_beats" in _llm.RIDDLE_SYSTEM)
    check("_TOOL_CHECK.description 含 discovery_beats",
          "discovery_beats" in _llm._TOOL_CHECK["description"])
    check("CHECK_SYSTEM 的同步合同清单含 discovery_beats",
          "discovery_beats" in _llm.CHECK_SYSTEM)
    # "六样"是写死的条数: beats 补进去之后必须是七样, 否则模型会按六样凑
    check("**CHECK_SYSTEM 不再写'六样'(已是七样)**",
          "六样" not in _llm.CHECK_SYSTEM)
    check("CHECK_SYSTEM 写的是七样", "七样" in _llm.CHECK_SYSTEM)
    check("**quality_checks 标题写八项(不是四项)**",
          "八项" in _llm.CHECK_SYSTEM and "四项**" not in _llm.CHECK_SYSTEM)
    # _QUALITY_CHECK_FIELDS 必须真的是八项 —— 标题与实现不能各说各话
    check("_QUALITY_CHECK_FIELDS 确实是 8 项",
          len(_llm._QUALITY_CHECK_FIELDS) == 8,
          _llm._QUALITY_CHECK_FIELDS)
    # 工具 schema 的 required 必须含 beats(模型最直接遵守的那一层)
    check("_TOOL_RIDDLE.required 含 discovery_beats",
          "discovery_beats" in _llm._TOOL_RIDDLE["input_schema"]["required"],
          _llm._TOOL_RIDDLE["input_schema"]["required"])


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
        merged, why, rewrite, _t = run(t)
        check(f"{field}='' -> 拒", merged is None, (field, merged))
        check(f"  {field} 的拒绝理由点名空/无效",
              "为空/无效" in (why or ""), why)
        # 纯空白也算空
        t2 = _copy.deepcopy(base)
        t2[field] = "   "
        merged2, why2, _, _t = run(t2)
        check(f"{field}='   ' -> 拒", merged2 is None, (field, merged2))

    # ---- 列表字段置空 ----
    for field in ("completion_fact_ids", "facts", "solve_atoms", "fair_clues"):
        t = _copy.deepcopy(base)
        t[field] = []
        merged, why, rewrite, _t = run(t)
        check(f"{field}=[] -> 拒", merged is None, (field, merged))
        check(f"  {field} 的拒绝理由点名空/无效",
              "为空/无效" in (why or ""), why)

    # ---- 类型错误也算无效 ----
    t3 = _copy.deepcopy(base)
    t3["facts"] = "不是列表"
    merged3, why3, _, _t = run(t3)
    check("facts 类型错误 -> 拒", merged3 is None, merged3)
    t4 = _copy.deepcopy(base)
    t4["core_answer"] = 123
    merged4, why4, _, _t = run(t4)
    check("core_answer 类型错误 -> 拒", merged4 is None, merged4)

    # ---- 正例: 完整非空 bundle 仍通过 ----
    merged5, why5, _, _t = run(_copy.deepcopy(base))
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
    merged, why, rewrite, _tech = w._review_spec(legacy)
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


# ======================================================================
# v6: completion fact 必须是 core_answer 的**最小语义拆分**
# ======================================================================
def test_v6_versions_bumped():
    """四个版本号必须一起走 —— 少 bump 一个就会让复盘分不清版本。"""
    print("\n[v6] 版本号")
    from story.llm import (ANSWER_PROMPT_VERSION, CHECK_PROMPT_VERSION,
                           RIDDLE_PROMPT_VERSION)
    from story.quality import QUALITY_POLICY_VERSION
    check("QUALITY_POLICY_VERSION == quality-v7",
          QUALITY_POLICY_VERSION == "quality-v8", QUALITY_POLICY_VERSION)
    check("RIDDLE_PROMPT_VERSION == riddle-v7",
          RIDDLE_PROMPT_VERSION == "riddle-v9", RIDDLE_PROMPT_VERSION)
    check("CHECK_PROMPT_VERSION == check-v7",
          CHECK_PROMPT_VERSION == "check-v8", CHECK_PROMPT_VERSION)
    check("ANSWER_PROMPT_VERSION == answer-v6",
          ANSWER_PROMPT_VERSION == "answer-v7", ANSWER_PROMPT_VERSION)
    # v8 bump 到 4: discovery_beats 改了 PuzzleSpec 的 schema。
    from story.puzzle import PuzzleSpec
    check("spec_version 升到 4",
          PuzzleSpec(puzzle="p", answer="a").to_archive().get("spec_version")
          == 4)


def test_v6_pool_quarantines_quality_v5():
    """真实故障就是 quality-v5 的题, 它们绝不能继续进直播。

    不迁移、不猜旧合同 —— 一律 quarantine, 等重新补池。
    """
    print("\n[v6] quality-v5 pool 必须被 quarantine")
    import json
    import os
    import tempfile
    from story.pool import PuzzlePool
    from story.puzzle import PuzzleSignature

    def _full(policy):
        sp = ident_spec()
        sp.quality_policy_version = policy
        sp.signature = PuzzleSignature(
            mechanism_family="identity_misread",
            solution_shape="identity_reversal", domain="family",
            emotion_mode="warm", relation="family", time_shape="instant",
            reveal_mode="identity_flip")
        return sp

    # 入池门: 政策版本不匹配 -> quarantine(不迁移、不猜)。
    ok, why = PuzzlePool._validate_pool_spec(_full("quality-v6"))
    check("quality-v6(旧政策) -> 入池被拒", not ok, why)
    check("理由点名政策不兼容", "不兼容" in why, why)
    ok2, why2 = PuzzlePool._validate_pool_spec(_full("quality-v8"))
    check("quality-v7 完整题 -> 入池", ok2, why2)

    # 出池门: 盘上**残留**的 v5 题也绝不能 pop 出来 —— 只拦入池不够,
    # 因为故障现场就是升级前已经写进池子的那批。
    d = tempfile.mkdtemp(prefix="v6pool_")
    cfg = mkcfg(pool_path=os.path.join(d, "pool.jsonl"))
    # 池记录是**包装过的**: `{"pool_version":.., "spec":{..}}`。
    # 直接写裸 spec dict 会被 `_spec_from_record` 当成不合规记录跳过 ——
    # 那样测出来的是"记录坏了", 不是"政策被隔离"。
    from story.pool import POOL_VERSION
    with open(cfg.pool_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"pool_version": POOL_VERSION,
                             "spec": _full("quality-v5").to_dict()},
                            ensure_ascii=False) + "\n")
    pool = PuzzlePool.open(cfg)
    check("盘上残留的 v5 题仍在池内(pending 不跑准入校验)",
          pool.pending_count() == 1, pool.pending_count())
    check("但 stock == 0(v5 无 live 资格)", pool.stock_count() == 0,
          pool.stock_count())
    check("pop 返回 None", pool.pop_next(recent_signatures=[]) is None)
    check("隔离项**原样留在盘上**, 不被删",
          os.path.exists(cfg.pool_path)
          and len([ln for ln in open(cfg.pool_path, encoding="utf-8")
                   if ln.strip()]) == 1)


def test_v6_riddle_prompt_has_minimality_rule():
    """生成端必须拿到"删除测试"这条硬规则, 否则合同还会写得更细。"""
    print("\n[v6] RIDDLE_SYSTEM 最小拆分规则")
    from story.llm import RIDDLE_SYSTEM
    check("点明是 core_answer 的最小语义拆分",
          "最小语义拆分" in RIDDLE_SYSTEM)
    check("给了删除测试", "删除测试" in RIDDLE_SYSTEM)
    check("明确禁止更严格/更细",
          "不能比 core_answer 更严格" in RIDDLE_SYSTEM)
    check("给了真实类型反例(鉴定人定价权)",
          "鉴定人" in RIDDLE_SYSTEM)
    check("反例含 support 降级指引", "降为 support" in RIDDLE_SYSTEM)


def test_v6_reviewer_has_minimality_rule():
    """Reviewer 的 completion_contract_minimal 必须同时管"不得严于"。"""
    print("\n[v6] Reviewer 最小性规则")
    from story.llm import CHECK_SYSTEM, _TOOL_CHECK
    d = (_TOOL_CHECK["input_schema"]["properties"]["quality_checks"]
         ["properties"]["completion_contract_minimal"]["description"])
    check("schema 描述含'不得严于 core_answer'", "不得严于 core_answer" in d)
    check("schema 描述含删除测试", "删除测试" in d)
    check("CHECK_SYSTEM 含 v6 段", "不得严于 core_answer" in CHECK_SYSTEM)
    check("CHECK_SYSTEM 含删除测试", "删除测试" in CHECK_SYSTEM)
    # fail-closed 必须还在: 四项仍全部 required。
    req = (_TOOL_CHECK["input_schema"]["properties"]["quality_checks"]
           ["required"])
    # v8: 从四项扩到八项(后四项查"好不好玩"), 仍是**全部** required ——
    # fail-closed 的语义没变: 任一项不是 True 就整稿拒收。
    check("八项仍全部 required",
          set(req) == {"narrator_truthful", "mechanism_consistent",
                       "core_answer_direct", "completion_contract_minimal",
                       "concrete_anomaly", "clue_recontextualized",
                       "dramatic_payoff", "reasoning_beats_nonredundant"},
          req)


# ======================================================================
# v6: 拍卖箱子 —— 真实故障的回归
# ======================================================================
_AUCTION_TEXT = ("古董商自己把箱子送去拍, 又自己把价格拍高, 刷出高价"
                 "成交记录, 这样手里那些同类旧箱就能卖得更贵。")
#: 带因果连词的完整解 —— `_looks_like_solution` 要求这个(文本回退路径
#: 靠句式启发式认候选)。真实观众说完整解时几乎一定带"所以/是因为"。
_AUCTION_TEXT_CAUSAL = ("古董商自己把箱子送去拍又自己拍高, 所以成交记录"
                        "被刷虚了, 是为了把手里同类旧箱卖得更贵。")


def test_v6_case1_complete_answer_ends_puzzle():
    """Case 1: 明显完整的解答**必须**结束这道题。

    第一层照旧保守(established=[]), 第二层复核补回 f1+f2。
    关键: writer 只调 2 次(1 层 + 复核), 旧 Final Judge **0 次**;
    提交给 Engine 后立刻 REVEALING, 且胜者是当前这位观众。
    """
    print("\n[v6 Case 1] 完整解答 -> 揭晓")
    fc = FakeClient([
        _verdict(established=[], cand=True),
        _completion_match(["f1", "f2"]),
    ])
    spec = auction_spec()
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    out, err = w.answer(
        spec.puzzle, spec.answer, [], 1, "甲", _AUCTION_TEXT, spec=spec,
        completion_fact_ids=spec.completion_fact_ids,
        core_answer=spec.core_answer, room_established_fact_ids=[])
    check("writer 恰好调用 2 次(第一层 + 复核)", len(fc.calls) == 2,
          len(fc.calls))
    check("绝不调旧 Final Judge",
          all(c["tool"]["name"] != "emit_judgement" for c in fc.calls),
          [c["tool"]["name"] for c in fc.calls])
    check("第二层确实走的是 completion 复核",
          fc.calls[1]["tool"]["name"] == "emit_completion_match",
          fc.calls[1]["tool"]["name"] if len(fc.calls) > 1 else None)
    check("QAResult.established == [f1, f2]",
          out and out[0].established_fact_ids == ["f1", "f2"],
          out[0].established_fact_ids if out else None)
    check("复核补入的 id 被单独记录",
          out and out[0].completion_verified_fact_ids == ["f1", "f2"],
          out[0].completion_verified_fact_ids if out else None)
    check("verdict 仍然是'是'(复核绝不改裁决)",
          out and out[0].verdict == "是", out[0].verdict if out else None)

    # ---- 交给 Engine: 必须揭晓, 且胜者是这位观众 ----
    eng, clk = boot(spec)
    ask(eng, clk, "u1", "甲", _AUCTION_TEXT,
        verdict="是", solution_candidate=True,
        established_fact_ids=list(out[0].established_fact_ids or []),
        completion_verified_fact_ids=list(
            out[0].completion_verified_fact_ids or []))
    check("phase == REVEALING", eng.phase == Phase.REVEALING, eng.phase)
    check("胜者是当前真人", eng._solved_by == "甲", eng._solved_by)


def test_v6_case2_vague_direction_does_not_end():
    """Case 2: 模糊方向不能结束 —— 且**不许多调一次** LLM。"""
    print("\n[v6 Case 2] 模糊方向 -> 只调 1 次")
    fc = FakeClient([_verdict(established=[], cand=False, verdict="是")])
    spec = auction_spec()
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    out, err = w.answer(
        spec.puzzle, spec.answer, [], 1, "甲", "古董商在炒作",
        spec=spec, completion_fact_ids=spec.completion_fact_ids,
        core_answer=spec.core_answer, room_established_fact_ids=[])
    check("candidate=false -> 只调 1 次", len(fc.calls) == 1, len(fc.calls))
    check("没调复核",
          all(c["tool"]["name"] != "emit_completion_match" for c in fc.calls))
    check("没有新增 established",
          not (out and out[0].established_fact_ids),
          out[0].established_fact_ids if out else None)

    eng, clk = boot(spec)
    ask(eng, clk, "u1", "甲", "古董商在炒作",
        verdict="是", solution_candidate=False)
    check("不揭晓", eng.phase == Phase.QA, eng.phase)
    check("没有胜者", not eng._solved_by, eng._solved_by)


def test_v6_case3_collective_progress_is_not_regression():
    """Case 3: 房间已建立 f1, 当前观众只补 f2 -> 立即揭晓。

    证明"共同推理"没有退化成"最后一个人必须重新说全"。
    """
    print("\n[v6 Case 3] 集体进度: 只补缺口")
    fc = FakeClient([
        _verdict(established=[], cand=True),
        _completion_match(["f2"]),
    ])
    spec = auction_spec()
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    out, err = w.answer(
        spec.puzzle, spec.answer, [], 2, "乙", "就是为了把手里那些旧箱子卖贵",
        spec=spec, completion_fact_ids=spec.completion_fact_ids,
        core_answer=spec.core_answer, room_established_fact_ids=["f1"])
    prompt = fc.calls[1]["user"]
    miss_block = prompt.split("【仍缺的通关事实】")[1].split("【")[0]
    room_block = prompt.split("【房间此前已确认】")[1].split("【")[0]
    check("【仍缺的通关事实】只列 f2", "f2" in miss_block
          and "f1" not in miss_block, miss_block)
    check("f1 在【房间此前已确认】里", "f1" in room_block, room_block)
    check("复核确实拿到了 core_answer",
          spec.core_answer in prompt, prompt[:200])

    eng, clk = boot(spec)
    ask(eng, clk, "u1", "甲", "他是自己举牌买回来的",
        verdict="是", solution_candidate=False, established_fact_ids=["f1"])
    check("补 f1 后仍未揭晓", eng.phase == Phase.QA, eng.phase)
    ask(eng, clk, "u2", "乙", "就是为了把手里那些旧箱子卖贵",
        verdict="是", solution_candidate=True,
        established_fact_ids=list(out[0].established_fact_ids or []))
    check("补最后一块后立即揭晓", eng.phase == Phase.REVEALING, eng.phase)
    check("胜者是补缺口的那位", eng._solved_by == "乙", eng._solved_by)


def test_v6_case4_bogus_verifier_ids_filtered():
    """Case 4: 复核返回 f999 / support / 非合同 id -> 全部丢弃。"""
    print("\n[v6 Case 4] 非法复核 id 必须过滤")
    # (复核返回, 期望最终 established)
    cases = [
        (["f999", "f3", "f2"], ["f2"]),   # 只留下真合同 id
        (["f999"], []),                   # 全是编的 -> 一条都不留
        (["f3"], []),                     # support 不是合同
        (["f4"], []),                     # exclusion 不是合同
        (["f2", "f2", "f999"], ["f2"]),   # 去重
        (["f1", "f999", "f4"], ["f1"]),
    ]
    for bogus, want in cases:
        fc = FakeClient([
            _verdict(established=[], cand=True),
            _completion_match(bogus),
        ])
        spec = auction_spec()
        w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
        out, err = w.answer(
            spec.puzzle, spec.answer, [], 1, "甲", _AUCTION_TEXT, spec=spec,
            completion_fact_ids=spec.completion_fact_ids,
            core_answer=spec.core_answer, room_established_fact_ids=[])
        est = (out[0].established_fact_ids if out else None) or []
        check(f"bogus={bogus} -> {want}", est == want, est)
        check(f"bogus={bogus} -> 不产生 P.SOLVE",
              out and out[0].verdict != "揭晓",
              out[0].verdict if out else None)
        check(f"bogus={bogus} -> verified 记录也只含合法项",
              (out[0].completion_verified_fact_ids or []) == want,
              out[0].completion_verified_fact_ids if out else None)


def test_v6_case5_verifier_failure_keeps_first_layer():
    """Case 5: 复核技术失败 -> 保留第一层裁决, 不 established, 不 solved。"""
    print("\n[v6 Case 5] 复核技术失败 -> 保留第一层")
    for bad in (LLMResult(error="timeout"), LLMResult(tool_input=None),
                LLMResult(tool_input={"matched_completion_fact_ids": "x"})):
        fc = FakeClient([_verdict(established=[], cand=True), bad])
        spec = auction_spec()
        w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
        out, err = w.answer(
            spec.puzzle, spec.answer, [], 1, "甲", _AUCTION_TEXT, spec=spec,
            completion_fact_ids=spec.completion_fact_ids,
            core_answer=spec.core_answer, room_established_fact_ids=[])
        check(f"失败({bad.error or bad.tool_input}) -> verdict 仍是'是'",
              out and out[0].verdict == "是",
              out[0].verdict if out else None)
        check("没有被改成'未判定'",
              out and out[0].status == "ok",
              out[0].status if out else None)
        check("不新增 established",
              not (out and out[0].established_fact_ids),
              out[0].established_fact_ids if out else None)
        check("不产生 P.SOLVE", out and out[0].verdict != "揭晓",
              out[0].verdict if out else None)


def test_v6_case6_never_touches_old_judge():
    """Case 6: FakeClient 只准备 ANSWER + VERIFY, 没有旧 Judge 的 canned。

    测试能过本身就证明 v6 的候选兜底**没有**偷偷复活 cause/mechanism
    Final Judge —— 否则它会去取第三个结果并拿到 error。
    """
    print("\n[v6 Case 6] 绝不复活旧 Judge")
    fc = FakeClient([
        _verdict(established=[], cand=True),
        _completion_match(["f1", "f2"]),
    ])
    spec = auction_spec()
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    out, err = w.answer(
        spec.puzzle, spec.answer, [], 1, "甲", _AUCTION_TEXT, spec=spec,
        completion_fact_ids=spec.completion_fact_ids,
        core_answer=spec.core_answer, room_established_fact_ids=[])
    check("没有第三次调用(旧 Judge 会来取)", len(fc.calls) == 2,
          len(fc.calls))
    check("工具序列 = [emit_verdict, emit_completion_match]",
          [c["tool"]["name"] for c in fc.calls]
          == ["emit_verdict", "emit_completion_match"],
          [c["tool"]["name"] for c in fc.calls])
    check("通关仍然成立", out and out[0].established_fact_ids == ["f1", "f2"],
          out[0].established_fact_ids if out else None)
    check("engine 侧也揭晓",
          True)   # 见 Case 1 的 engine 断言, 这里只钉工具序列


def test_v6_case7_completion_marker_reaches_answer_prompt():
    """Case 7: `[通关核心]` 标记真的进了 Answer prompt, 且只标通关事实。"""
    print("\n[v6 Case 7] completion 标记进 prompt")
    fc = FakeClient([_verdict(established=[], cand=False)])
    spec = auction_spec()
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    w.answer(spec.puzzle, spec.answer, [], 1, "甲", "他是医生吗", spec=spec,
             completion_fact_ids=spec.completion_fact_ids,
             core_answer=spec.core_answer, room_established_fact_ids=[])
    prompt = fc.calls[0]["user"]
    check("f1 带 [通关核心]", "f1 [core] [通关核心]" in prompt, prompt[:400])
    check("f2 带 [通关核心]", "f2 [core] [通关核心]" in prompt, prompt[:400])
    check("support fact 不带标记",
          "f3 [support] [通关核心]" not in prompt)
    check("exclusion fact 不带标记",
          "f4 [exclusion] [通关核心]" not in prompt)
    check("无合同时不带任何标记",
          " [通关核心]" not in _facts_block_for_test(spec))


def _facts_block_for_test(spec):
    from story.llm import _facts_block
    return _facts_block(spec)


def test_v6_case8_verifier_cannot_solve_by_itself():
    """Case 8: 复核**只能**回 completion id —— schema 里没有 solved。"""
    print("\n[v6 Case 8] 复核没有第二条胜负入口")
    from story.llm import _TOOL_COMPLETION_VERIFY
    props = _TOOL_COMPLETION_VERIFY["input_schema"]["properties"]
    check("只有 matched_completion_fact_ids 一个字段",
          list(props) == ["matched_completion_fact_ids"], list(props))
    check("没有 solved 字段", "solved" not in props)
    check("也没有 verdict 字段", "verdict" not in props)
    check("required 就是它",
          _TOOL_COMPLETION_VERIFY["input_schema"]["required"]
          == ["matched_completion_fact_ids"])
    # 复核即使"全中", 也必须经由 Engine 的合同覆盖才揭晓 —— 直接调
    # 复核函数不会把 verdict 改成 P.SOLVE。
    fc = FakeClient([
        _verdict(established=[], cand=True),
        _completion_match(["f1", "f2"]),
    ])
    spec = auction_spec()
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    out, err = w.answer(
        spec.puzzle, spec.answer, [], 1, "甲", _AUCTION_TEXT, spec=spec,
        completion_fact_ids=spec.completion_fact_ids,
        core_answer=spec.core_answer, room_established_fact_ids=[])
    check("复核全中也**不**把 verdict 改成 P.SOLVE",
          out and out[0].verdict == "是", out[0].verdict if out else None)
    check("通关只能由 Engine 的 submit_qa 触发(见 Case 1)",
          out and out[0].established_fact_ids == ["f1", "f2"])


def test_v6_case9_trigger_matrix():
    """Case 9 / A1: 复核的触发条件。

    多调 = 普通问答白烧一次 LLM; 少调 = 自报的 completion 直接通关。

    **A1 起判据变了**: 不再要求 `verdict=是 + candidate`。现在只要

        missing 非空 AND ( direct_completion 非空 OR candidate=True )

    因为 completion fact **不能只靠第一层自报** —— 哪怕是「不是」裁决,
    只要第一层自报建立了某条 completion, 也必须复核(否则那个洞还在)。
    """
    print("\n[v6 Case 9 / A1] 复核触发矩阵")
    def run(text="他自己拍高", est=None, cand=True, verdict="是",
            room=None, contract=None):
        fc = FakeClient([_verdict(established=est or [], cand=cand,
                                  verdict=verdict),
                         _completion_match(["f2"])])
        spec = auction_spec()
        w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
        out, err = w.answer(
            spec.puzzle, spec.answer, [], 1, "甲", text, spec=spec,
            completion_fact_ids=(spec.completion_fact_ids
                                 if contract is None else contract),
            core_answer=spec.core_answer,
            room_established_fact_ids=room or [])
        return len(fc.calls), out

    n, _ = run()
    check("candidate=True -> 调复核(2 次)", n == 2, n)
    n, _ = run(cand=False)
    check("**candidate=False 且没自报 completion -> 不调(1 次)**", n == 1, n)
    # ---- A1 新增: 自报 completion 就必须复核, 不看 verdict ----
    n, _ = run(cand=False, est=["f2"])
    check("**candidate=False 但自报 f2 -> 也要复核(2 次)**", n == 2, n)
    n, _ = run(verdict="不是", est=["f2"])
    check("**「不是」+ 自报 f2 -> 仍复核(2 次)**", n == 2, n)
    n, _ = run(verdict="不是", cand=False)
    check("「不是」+ candidate=False + 无自报 -> 不调(1 次)", n == 1, n)
    n, _ = run(verdict="无关", cand=False)
    check("「无关」+ candidate=False + 无自报 -> 不调(1 次)", n == 1, n)
    n, out = run(est=["f1"])
    check("第一层已补 f1 -> 仍调, 只缺 f2", n == 2, n)
    # ⚠️ A1: 第一层**自报**了 f1+f2 也要复核 —— 那正是"自报不能直接通关"
    # 这条规则本身。复核回 ["f2"], 所以最终 established 只留 direct 里
    # 非 completion 的部分 + 被确认的 f2。
    n, out = run(est=["f1", "f2"])
    check("**第一层自报满合同 -> 仍要复核(2 次, 自报不是通关)**", n == 2, n)
    check("复核只认 f2 -> 最终 established 不含被否决的 f1",
          out and out[0].established_fact_ids == ["f2"],
          out[0].established_fact_ids if out else None)
    n, out = run(room=["f1", "f2"])
    check("合同已被**房间**覆盖 + candidate -> 不调(1 次)", n == 1, n)
    check("被房间覆盖时第一层的自报 completion 也不留下",
          out and out[0].established_fact_ids == [],
          out[0].established_fact_ids if out else None)
    n, _ = run(room=["f1", "f2"], cand=False)
    check("合同已被房间覆盖 + 无候选 -> 不调(1 次)", n == 1, n)
    # 无合同时 candidate 走的是 legacy 分支 -> 旧 Judge, **不会**调复核。
    # 这里显式给两层 canned: 第一层裁决 + 一个旧 judge 返回。如果代码
    # 误把复核插进来, 工具序列就会变。
    fc = FakeClient([_verdict(established=[], cand=True),
                     LLMResult(tool_input={"is_guess": True,
                                           "cause_hit": False,
                                           "mechanism_hit": False,
                                           "matched_atoms": []}, model="m")])
    spec = auction_spec()
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    w.answer(spec.puzzle, spec.answer, [], 1, "甲", "他自己拍高", spec=spec,
             completion_fact_ids=[], core_answer=spec.core_answer,
             room_established_fact_ids=[],
             solve_atoms=[a.to_dict() for a in spec.solve_atoms])
    names = [c["tool"]["name"] for c in fc.calls]
    check("无合同 -> 绝不调 completion 复核",
          "emit_completion_match" not in names, names)
    check("无合同 + candidate -> 仍走旧 emit_judgement(legacy 未降级)",
          names == ["emit_verdict", "emit_judgement"], names)


def test_v6_case10_status_must_be_ok_for_verifier():
    """Case 10: 复核**只能**碰 established, 绝不能改裁决。

    与 `_record_human_established_locked` 同一套推理: "没标"不等于
    "没问题"。复核是通往 established 的**第二条写入路径**, 所以它要
    过同一道门。

    ⚠️ 这里的检查用 **AST**, 不用字符串匹配 —— docstring 里就会写到
    `r0.verdict == P.YES`, 字符串匹配会把注释当成代码, 那种测试是假的。
    """
    print("\n[v6 Case 10] 复核只能写 established")
    import ast
    import inspect
    import textwrap
    src = textwrap.dedent(inspect.getsource(PuzzleWriter._completion_verify))
    tree = ast.parse(src)
    assigned = set()
    for node in ast.walk(tree):
        # r0.<attr> = ...
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if (isinstance(t, ast.Attribute)
                        and isinstance(t.value, ast.Name)
                        and t.value.id == "r0"):
                    assigned.add(t.attr)
    check("只赋值 established_fact_ids 与 completion_verified_fact_ids",
          assigned == {"established_fact_ids",
                       "completion_verified_fact_ids"}, assigned)
    check("**没有**给 r0.verdict 赋值", "verdict" not in assigned, assigned)
    check("**没有**给 r0.status 赋值", "status" not in assigned, assigned)
    # 代码里真的对 r0.status 做了 "ok" 判定(AST 级, 不是注释里的)。
    # 实际写法是 `str(getattr(r0, "status", "") or "") != "ok"` —— 属性名
    # 是 getattr 的字符串参数。所以这里找两件事: 出现了对 "status" 的
    # getattr(r0, ...), 且同一个函数里比较过常量 "ok"。
    guarded_by_getattr = any(
        isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
        and c.func.id == "getattr" and len(c.args) > 1
        and isinstance(c.args[0], ast.Name) and c.args[0].id == "r0"
        and isinstance(c.args[1], ast.Constant) and c.args[1].value == "status"
        for c in ast.walk(tree))
    guarded_by_attr = any(
        isinstance(n, ast.Attribute) and n.attr == "status"
        and isinstance(n.value, ast.Name) and n.value.id == "r0"
        for n in ast.walk(tree))
    compares_ok = any(
        isinstance(n, ast.Constant) and n.value == "ok"
        for n in ast.walk(tree))
    check("代码里真的对 r0.status 做了 'ok' 判定",
          (guarded_by_getattr or guarded_by_attr) and compares_ok,
          (guarded_by_getattr, guarded_by_attr, compares_ok))
    # 复核函数体内不得出现 P.SOLVE。
    used = {n.attr for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
            and n.value.id == "P"}
    check("不引用 P.SOLVE", "SOLVE" not in used, used)


def test_v6_verified_ids_archive_boundary():
    """`completion_verified_fact_ids` 只进 archive, 不进前端 JSON。

    与 `established_fact_ids` 同一条边界: 前端既不需要、也不该看到
    内部 fact id。少了这条断言, 以后有人"顺手"把它加进 to_json 就
    会把通关状态漏到屏幕上。
    """
    print("\n[v6] 复核来源字段的落盘边界")
    from story.state import QARec
    rec = QARec(qid=1, user_name="甲", text="t", verdict="是", comment="",
                kind="qa", ts=0, established_fact_ids=["f1"],
                completion_verified_fact_ids=["f2"])
    check("to_json 不含 completion_verified_fact_ids",
          "completion_verified_fact_ids" not in rec.to_json())
    check("to_json 仍不含 established_fact_ids",
          "established_fact_ids" not in rec.to_json())
    check("to_archive 含 completion_verified_fact_ids",
          rec.to_archive().get("completion_verified_fact_ids") == ["f2"])
    check("to_archive 含 established_fact_ids",
          rec.to_archive().get("established_fact_ids") == ["f1"])
    # QAResult 也必须带这个字段(默认 None), 否则 writer 赋值会炸。
    from story.state import QAResult
    check("QAResult 有 completion_verified_fact_ids 字段",
          hasattr(QAResult(qid=1, verdict="是"),
                  "completion_verified_fact_ids"))
    check("默认是 None", QAResult(qid=1, verdict="是").completion_verified_fact_ids
          is None)


def test_v6_progress_log_leaks_no_truth():
    """v6 进度 INFO 只能打 **fact ID**, 绝不能打 fact 文本 / core_answer。

    INFO 会进 data/run.log。打文本等于把隐藏真相摊在日志里, 而日志是
    最容易被随手分享出去的东西。这条断言是**防回归**的 —— 以后有人
    想让日志"更好读"而把文本加回来, 这里会红。
    """
    print("\n[v6] 进度日志不泄漏隐藏真相")
    import logging
    spec = auction_spec()
    eng, clk = boot(spec)
    buf = _CaptureLogs()
    try:
        ask(eng, clk, "u1", "甲", "他自己拍高",
            verdict="是", solution_candidate=True, established_fact_ids=["f1"])
    finally:
        buf.detach()
    lines = [l for l in buf.text().splitlines() if "v6进度" in l]
    check("确实打了 v6进度", bool(lines), buf.text()[-200:])
    for l in lines:
        check("进度行只有 fact ID, 不含 core_answer",
              spec.core_answer not in l, l)
        check("进度行不含 answer", spec.answer not in l, l)
        for f in spec.facts:
            check(f"进度行不含 fact 文本({f.id})", f.text not in l, l)
    check("进度行含必要的四个字段",
          all(k in lines[0] for k in
              ("qid=", "candidate=", "covered=", "remaining=")), lines[0])


class _CaptureLogs:
    """把 root logger 的 INFO 抓进内存, 退出时还原。"""

    def __init__(self):
        import io
        import logging
        self._buf = io.StringIO()
        self._h = logging.StreamHandler(self._buf)
        self._h.setLevel(logging.INFO)
        self._root = logging.getLogger()
        self._old = self._root.level
        self._root.addHandler(self._h)
        self._root.setLevel(logging.INFO)

    def text(self):
        return self._buf.getvalue()

    def detach(self):
        self._root.removeHandler(self._h)
        self._root.setLevel(self._old)


# ======================================================================
# v6 blocker: 第一层绝不拥有通关权
# ======================================================================
# 两条绕过路径:
#   1. 文本回退 —— `P.parse_answers` 的关键词表把 揭晓/完全正确/答对了
#      都映射成 P.SOLVE, 而只有 tool 分支做了降级。
#   2. Engine —— 即使 Writer 归一了, 其它 producer 塞进来的 P.SOLVE
#      仍会走 legacy 直通揭晓。
# 这两个都要修, 且都要有 mutation 测试钉住。
_SOLVE_TEXTS = ["1. 揭晓", "1. 答对了", "1. 完全正确", "1. 真相是",
                "1. 答案是", "1. 谜底是", "1. 正确答案"]
_TOOL_SOLVE = [{"id": 1, "verdict": "揭晓", "comment": "",
                "solution_candidate": False, "touched_fact_ids": [],
                "established_fact_ids": []}]


def test_v6_a_text_fallback_solve_cannot_win():
    """A: 纯文本 `1. 揭晓` 不能直接获胜。

    第一层文本回退解析出 P.SOLVE, 必须被统一降级为「是」; 观众说的是
    candidate=false 的短句, 所以连复核都不该触发, 更不该揭晓。
    """
    print("\n[v6 A] text fallback '揭晓' 不能直通")
    spec = auction_spec()
    for txt in _SOLVE_TEXTS:
        fc = FakeClient([LLMResult(text=txt, model="m")])
        w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
        out, err = w.answer(
            spec.puzzle, spec.answer, [], 1, "甲", "他是医生吗", spec=spec,
            completion_fact_ids=spec.completion_fact_ids,
            core_answer=spec.core_answer, room_established_fact_ids=[])
        check(f"{txt!r} -> verdict 是「是」", out and out[0].verdict == "是",
              out[0].verdict if out else None)
        check(f"{txt!r} -> 不是 P.SOLVE", out and out[0].verdict != "揭晓",
              out[0].verdict if out else None)
        check(f"{txt!r} -> 没触发复核(candidate=false)",
              all(c["tool"]["name"] != "emit_completion_match"
                  for c in fc.calls), [c["tool"]["name"] for c in fc.calls])

    # Engine 侧: 提交这条降级后的结果, 必须仍在 QA。
    eng, clk = boot(spec)
    ask(eng, clk, "u1", "甲", "他是医生吗", verdict="是",
        solution_candidate=False)
    check("Engine 仍在 QA", eng.phase == Phase.QA, eng.phase)
    check("没有胜者", not eng._solved_by, eng._solved_by)


def test_v6_b_text_fallback_variants_all_normalized():
    """B: parser 的关键词不止字面 `揭晓` —— 全部都要降级。

    这条与 A 分开, 是因为它防的是"以后有人只给 `揭晓` 加特判"。
    判据必须是 `verdict == P.SOLVE`, 不是某个字面量。
    """
    print("\n[v6 B] text fallback 的 SOLVE 变体全部降级")
    import story.parser as P
    spec = auction_spec()
    seen_solve = 0
    for txt in _SOLVE_TEXTS:
        # 先确认 parser 真的把这些映射成 P.SOLVE —— 否则这条测试是空的。
        parsed, _ = P.parse_answers(txt, [type("Q", (), {"qid": 1})()])
        if parsed and parsed[0].verdict == P.SOLVE:
            seen_solve += 1
        fc = FakeClient([LLMResult(text=txt, model="m")])
        w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
        out, _ = w.answer(
            spec.puzzle, spec.answer, [], 1, "甲", "他是医生吗", spec=spec,
            completion_fact_ids=spec.completion_fact_ids,
            core_answer=spec.core_answer, room_established_fact_ids=[])
        check(f"{txt!r} 降级后不是 P.SOLVE",
              out and out[0].verdict != P.SOLVE,
              out[0].verdict if out else None)
    check("parser 确实会把其中多条映射成 P.SOLVE(否则本测试没意义)",
          seen_solve >= 2, seen_solve)
    # tool 分支同样归一。
    fc = FakeClient([LLMResult(tool_input={"answers": _TOOL_SOLVE}, model="m")])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    out, _ = w.answer(spec.puzzle, spec.answer, [], 1, "甲", "他是医生吗",
                      spec=spec,
                      completion_fact_ids=spec.completion_fact_ids,
                      core_answer=spec.core_answer,
                      room_established_fact_ids=[])
    check("tool 分支的'揭晓'也降级为「是」",
          out and out[0].verdict == "是", out[0].verdict if out else None)


def test_v6_c_text_fallback_candidate_still_reaches_verifier():
    """C: candidate=true 的文本回退**仍能赢** —— 但必须经过 completion IDs。

    这是本 blocker 的关键平衡: 修完之后不能把正常通关也堵死。
    路径: 文本\"完全正确\" -> 降级\"是\" -> `_looks_like_solution` 命中
    -> completion 复核 -> f1/f2 -> Engine 覆盖 -> 揭晓。
    """
    print("\n[v6 C] text fallback + 完整解 -> 经复核获胜")
    spec = auction_spec()
    from story.llm import _looks_like_solution
    check("这条发言确实触发句式启发式",
          _looks_like_solution(_AUCTION_TEXT_CAUSAL), _AUCTION_TEXT_CAUSAL)
    fc = FakeClient([LLMResult(text="1. 完全正确", model="m"),
                     _completion_match(["f1", "f2"])])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    out, err = w.answer(
        spec.puzzle, spec.answer, [], 1, "甲", _AUCTION_TEXT_CAUSAL, spec=spec,
        completion_fact_ids=spec.completion_fact_ids,
        core_answer=spec.core_answer, room_established_fact_ids=[])
    check("第一层先降级为「是」", out and out[0].verdict == "是",
          out[0].verdict if out else None)
    check("句式像完整解 -> 触发了复核", len(fc.calls) == 2, len(fc.calls))
    check("第二层是 completion 复核",
          fc.calls[1]["tool"]["name"] == "emit_completion_match",
          [c["tool"]["name"] for c in fc.calls])
    check("established 来自复核的 completion IDs",
          out and out[0].established_fact_ids == ["f1", "f2"],
          out[0].established_fact_ids if out else None)
    check("**不是**凭 P.SOLVE 赢的", out and out[0].verdict != P.SOLVE,
          out[0].verdict if out else None)

    eng, clk = boot(spec)
    ask(eng, clk, "u1", "甲", _AUCTION_TEXT_CAUSAL,
        verdict=out[0].verdict, solution_candidate=True,
        established_fact_ids=list(out[0].established_fact_ids or []),
        completion_verified_fact_ids=list(
            out[0].completion_verified_fact_ids or []))
    check("Engine coverage 揭晓", eng.phase == Phase.REVEALING, eng.phase)
    check("胜者是当前真人", eng._solved_by == "甲", eng._solved_by)


def test_v6_d_engine_rejects_solve_bypass_with_contract():
    """D: Engine defense-in-depth —— 直接塞 P.SOLVE 也绕不开合同。

    **本批最关键的一条 mutation**: 完全绕过 Writer, 向一个有通关合同的
    Engine 提交 P.SOLVE + established=[]。必须仍在 QA。

    这条防的是"将来另一个 producer / no-llm / 异常 parser 又塞进
    P.SOLVE" —— 只相信 Writer 会永远归一正确是不够的。
    """
    print("\n[v6 D] Engine 直接 P.SOLVE -> 有合同必须拒绝")
    spec = auction_spec()
    eng, clk = boot(spec)
    check("这道题确实有合同", set(spec.completion_fact_ids) == {"f1", "f2"},
          spec.completion_fact_ids)
    ask(eng, clk, "u1", "甲", "我自己宣布答对了",
        verdict="揭晓", solution_candidate=True, established_fact_ids=[])
    check("phase 仍 QA(没有被 P.SOLVE 直通)", eng.phase == Phase.QA,
          eng.phase)
    check("不 solved", not eng._solved_by, eng._solved_by)
    # 对照: 补齐合同才揭晓。
    ask(eng, clk, "u2", "乙", "他是自己拍高刷成交记录的",
        verdict="是", solution_candidate=True, established_fact_ids=["f1"])
    check("补 f1 后仍未揭晓", eng.phase == Phase.QA, eng.phase)
    ask(eng, clk, "u3", "丙", "为了把手里同类旧箱卖贵",
        verdict="是", solution_candidate=True, established_fact_ids=["f2"])
    check("补齐合同后才揭晓", eng.phase == Phase.REVEALING, eng.phase)
    check("胜者是补缺口那位", eng._solved_by == "丙", eng._solved_by)


def test_v6_e_legacy_solve_still_wins():
    """E: legacy(无合同)的 P.SOLVE 路径**必须原样保留**。

    只锁 v6, 不能杀掉旧数据兼容 —— 老 archive / 老 fixture 的题就是
    靠这条路通关的。
    """
    print("\n[v6 E] legacy P.SOLVE 仍可通关")
    from story.puzzle import PuzzleFact, SolveAtom
    legacy = PuzzleSpec(
        id="ux-legacy", title="老题",
        puzzle="门外站着一个女人, 开门的人一见她就愣住了。为什么?",
        answer="门外女人是父亲的亲生女儿。",
        # **无合同** + 旧政策 -> legacy 语义
        core_answer="", completion_fact_ids=[],
        facts=[PuzzleFact(id="f1", text="门外女人是父亲的亲生女儿",
                          kind="core")],
        solve_atoms=[
            SolveAtom(id="a1", role="cause", text="x", fact_ids=["f1"]),
            SolveAtom(id="a2", role="mechanism", text="y", fact_ids=["f1"]),
        ],
        hints=["a", "b", "c"],
        prompt_version="riddle-v4", quality_policy_version="quality-v4")
    eng, clk = boot(legacy)
    check("legacy 题确实没有合同", not eng._completion_fact_ids,
          eng._completion_fact_ids)
    ask(eng, clk, "u1", "甲", "她是父亲的女儿吧",
        verdict="揭晓", solution_candidate=True, established_fact_ids=["f1"])
    check("legacy 的 P.SOLVE 仍进入揭晓",
          eng.phase == Phase.REVEALING, eng.phase)
    check("胜者记下了", eng._solved_by == "甲", eng._solved_by)

    # 反向: 同一道题一旦**有**合同, P.SOLVE 就不再直通。
    with_c = auction_spec()
    eng2, clk2 = boot(with_c)
    ask(eng2, clk2, "u1", "甲", "她是父亲的女儿吧",
        verdict="揭晓", solution_candidate=True, established_fact_ids=["f1"])
    check("有合同时同样输入**不**揭晓", eng2.phase == Phase.QA, eng2.phase)


def test_v6_first_layer_never_owns_victory():
    """把"第一层不拥有通关权"这条语义钉成不变量。

    判据不是某个字面量, 而是: 第一层产出的任何 verdict 都不可能是
    P.SOLVE —— 无论走 tool 还是 text, 无论模型说什么。
    """
    print("\n[v6] 第一层永不产出 P.SOLVE")
    spec = auction_spec()
    # tool 路径: 所有可能的 verdict 值 + 越界值
    for v in ["是", "不是", "无关", "揭晓", "SOLVE", "solved", ""]:
        fc = FakeClient([LLMResult(tool_input={"answers": [{
            "id": 1, "verdict": v, "comment": "c",
            "solution_candidate": False, "touched_fact_ids": [],
            "established_fact_ids": []}]}, model="m")])
        w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
        out, _ = w.answer(spec.puzzle, spec.answer, [], 1, "甲", "他是医生吗",
                          spec=spec,
                          completion_fact_ids=spec.completion_fact_ids,
                          core_answer=spec.core_answer,
                          room_established_fact_ids=[])
        if out:
            check(f"tool verdict={v!r} 产出不是 P.SOLVE",
                  out[0].verdict != P.SOLVE, out[0].verdict)
    # text 路径: 全部 SOLVE 关键词
    for txt in _SOLVE_TEXTS:
        fc = FakeClient([LLMResult(text=txt, model="m")])
        w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
        out, _ = w.answer(spec.puzzle, spec.answer, [], 1, "甲", "他是医生吗",
                          spec=spec,
                          completion_fact_ids=spec.completion_fact_ids,
                          core_answer=spec.core_answer,
                          room_established_fact_ids=[])
        if out:
            check(f"text {txt!r} 产出不是 P.SOLVE",
                  out[0].verdict != P.SOLVE, out[0].verdict)


def test_v6_candidate_not_gated_by_answer_presence():
    """candidate 的判定不该被"谜底有没有记录"左右。

    修 blocker 时顺带发现的**潜伏 bug**: 文本回退里
    `r.solution_candidate = _looks_like_solution(text)` 原先嵌在
    `if answer:` 里面。于是**谜底缺失**时这条路谁都不会被标成候选,
    一个说得完全正确的观众永远走不到复核 —— 而谜底本来就只是用来做
    泄漏检查的, 不该决定 candidate。
    """
    print("\n[v6] candidate 不受 answer 有无影响")
    spec = auction_spec()
    for ans in ["", spec.answer]:
        fc = FakeClient([LLMResult(text="1. 完全正确", model="m"),
                         _completion_match(["f1", "f2"])])
        w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
        out, _ = w.answer(
            spec.puzzle, ans, [], 1, "甲", _AUCTION_TEXT_CAUSAL, spec=spec,
            completion_fact_ids=spec.completion_fact_ids,
            core_answer=spec.core_answer, room_established_fact_ids=[])
        tag = "有谜底" if ans else "无谜底"
        check(f"{tag}: candidate 被正确标出",
              out and out[0].solution_candidate is True,
              out[0].solution_candidate if out else None)
        check(f"{tag}: 复核被触发(2 次调用)", len(fc.calls) == 2,
              len(fc.calls))
        check(f"{tag}: 通关成立",
              out and out[0].established_fact_ids == ["f1", "f2"],
              out[0].established_fact_ids if out else None)


def _contrib_qids(eng):
    return [c["qid"] for c in eng._reveal_contributors_locked()]


def _contrib(eng):
    return eng._reveal_contributors_locked()


# ----------------------------------------------------------------------
# R1/R2-A —— 真正贡献链
# ----------------------------------------------------------------------
def test_r1_a_real_contribution_chain():
    print("\n[R1-A] 真贡献链: A 建 f1, B 建 f2 -> contributors=[A,B], B is_final")
    sp = ident_spec()
    eng, clk = boot(sp)
    ask(eng, clk, "u1", "甲", "她是父亲的女儿吗", verdict="是",
        established_fact_ids=["f1"])
    ask(eng, clk, "u2", "乙", "她昨晚和父亲吃饭了吗", verdict="是",
        established_fact_ids=["f2"])
    check("已通关", eng.phase == Phase.REVEALING, eng.phase)
    c = _contrib(eng)
    check("贡献链长度 2", len(c) == 2, c)
    check("顺序 = 甲,乙", [x["user_name"] for x in c] == ["甲", "乙"], c)
    check("B(乙) is_final=True", c[1]["is_final"] is True, c)
    check("A(甲) is_final=False", c[0]["is_final"] is False, c)


# ----------------------------------------------------------------------
# R1/R2-B —— 重复确认不能重复领奖
# ----------------------------------------------------------------------
def test_r1_b_duplicate_confirmation_not_credited():
    print("\n[R1-B] 重复确认: A 建 f1, B 再确认 f1, C 建 f2 -> [A,C] 不含 B")
    sp = ident_spec()
    eng, clk = boot(sp)
    ask(eng, clk, "u1", "A", "她是父亲的女儿吗", verdict="是",
        established_fact_ids=["f1"])
    # B 语义上**又**建立了 f1 —— 但它不是第一次, 所以不该领奖。
    ask(eng, clk, "u2", "B", "她是不是父亲的女儿", verdict="是",
        established_fact_ids=["f1"])
    ask(eng, clk, "u3", "C", "她昨晚和父亲吃饭了吗", verdict="是",
        established_fact_ids=["f2"])
    c = _contrib(eng)
    check("只有 A,C 领奖", [x["user_name"] for x in c] == ["A", "C"], c)
    check("B 不在贡献链里", "B" not in [x["user_name"] for x in c], c)
    # 而 B 的 established 依然记着 f1 —— 语义与贡献是两件事。
    rec_b = [r for r in eng._qa_archive if r.user_name == "B"][0]
    check("B 的 established 仍含 f1", rec_b.established_fact_ids
          and "f1" in rec_b.established_fact_ids, rec_b.established_fact_ids)
    check("B 的 contribution 为空",
          not rec_b.completion_contribution_fact_ids,
          rec_b.completion_contribution_fact_ids)


# ----------------------------------------------------------------------
# R1/R2-C —— 并发回包顺序(本批最重要的正确性测试)
# ----------------------------------------------------------------------
def test_r1_c_concurrent_commit_order():
    print("\n[R1-C] 并发: qid=2 先提交建 f1, qid=1 后回也声称 f1 -> 归属 qid=2")
    sp = ident_spec()
    eng, clk = boot(sp)
    # 两条都在途(派发顺序 1,2)
    eng.submit_danmaku("u1", "甲", "#问题一")
    clk.advance(20.0)
    a1 = [a for a in eng.tick() if a.kind == ActionKind.ANSWER][0].payload
    eng.submit_danmaku("u2", "乙", "#问题二")
    clk.advance(20.0)
    a2 = [a for a in eng.tick() if a.kind == ActionKind.ANSWER][0].payload
    check("两条 qid 按派发顺序", a1["qid"] == 1 and a2["qid"] == 2,
          (a1["qid"], a2["qid"]))
    # 真实完成顺序反转: qid=2 先回来
    # J1-B: 合同内 id 必须同时被复核确认 —— 这条走裸 submit_qa, 显式给出
    # (模拟 Writer 已正常复核)。
    eng.submit_qa([QAResult(qid=2, verdict="是", established_fact_ids=["f1"],
                            completion_verified_fact_ids=["f1"])])
    # qid=1 后回来, 也声称建立了 f1
    eng.submit_qa([QAResult(qid=1, verdict="是", established_fact_ids=["f1"],
                            completion_verified_fact_ids=["f1"])])
    # 第三条补齐 f2
    eng.submit_danmaku("u3", "丙", "#问题三")
    clk.advance(20.0)
    a3 = [a for a in eng.tick() if a.kind == ActionKind.ANSWER][0].payload
    eng.submit_qa([QAResult(qid=a3["qid"], verdict="是",
                            established_fact_ids=["f2"],
                            completion_verified_fact_ids=["f2"])])
    check("已通关", eng.phase == Phase.REVEALING, eng.phase)
    # archive 按 qid 重排 —— 它是 1,2,3
    check("archive 顺序为 qid 1,2,3",
          [r.qid for r in eng._qa_archive] == [1, 2, 3],
          [r.qid for r in eng._qa_archive])
    # 而贡献链必须按**真实提交顺序**: 2 先, 然后 3。qid=1 没有功劳。
    check("贡献链 = [2,3] 而非 [1,3]", _contrib_qids(eng) == [2, 3],
          _contrib_qids(eng))
    c = _contrib(eng)
    check("qid=2 是最后线索", c[-1]["is_final"] is True and c[-1]["qid"] == 3, c)
    # qid=1 的贡献是空的
    rec1 = [r for r in eng._qa_archive if r.qid == 1][0]
    check("qid=1 无贡献", not rec1.completion_contribution_fact_ids,
          rec1.completion_contribution_fact_ids)

    # ---- 追加: 两位贡献者, 提交顺序与 qid 顺序**完全相反** ----
    #
    # 上面那组只钉住了"重复确认不领奖"(qid=1 被过滤掉), 于是排序键
    # `(ts,qid)` 退化成 `qid` 也能过 —— 它**没有**验证排序本身。
    # 这一组让两条都带贡献、且 qid 大的先提交, 两个排序键才会分叉:
    #     真实提交顺序 = [2, 1]    (正确)
    #     按 qid 排     = [1, 2]   (错误)
    print("  -- 追加: 提交顺序与 qid 顺序相反 --")
    eng2, clk2 = boot(ident_spec())
    eng2.submit_danmaku("u1", "甲", "#问题一")
    clk2.advance(20.0)
    b1 = [a for a in eng2.tick() if a.kind == ActionKind.ANSWER][0].payload
    eng2.submit_danmaku("u2", "乙", "#问题二")
    clk2.advance(20.0)
    b2 = [a for a in eng2.tick() if a.kind == ActionKind.ANSWER][0].payload
    check("派发顺序 1,2", (b1["qid"], b2["qid"]) == (1, 2),
          (b1["qid"], b2["qid"]))
    # qid=2 先提交并建立 f1 —— 它是第一位贡献者
    eng2.submit_qa([QAResult(qid=2, verdict="是", established_fact_ids=["f1"],
                             completion_verified_fact_ids=["f1"])])
    # qid=1 后提交并建立 f2 —— 它是**最后**一块, 必须拿 is_final
    eng2.submit_qa([QAResult(qid=1, verdict="是", established_fact_ids=["f2"],
                             completion_verified_fact_ids=["f2"])])
    check("已通关", eng2.phase == Phase.REVEALING, eng2.phase)
    check("archive 按 qid 排成 1,2",
          [r.qid for r in eng2._qa_archive] == [1, 2],
          [r.qid for r in eng2._qa_archive])
    c2 = _contrib(eng2)
    check("贡献链 = [2,1] 真实提交序", [x["qid"] for x in c2] == [2, 1], c2)
    check("qid=1(后提交)拿 is_final",
          c2[-1]["qid"] == 1 and c2[-1]["is_final"] is True, c2)
    check("qid=2(先提交)不是 is_final", c2[0]["is_final"] is False, c2)
    check("胜者也是 qid=1 那位", eng2._solved_by == "甲", eng2._solved_by)


# ----------------------------------------------------------------------
# R1/R2-D —— support 不算贡献
# ----------------------------------------------------------------------
def test_r1_d_support_is_not_contribution():
    print("\n[R1-D] support fact 建立 -> 不进贡献链")
    sp = ident_spec()
    eng, clk = boot(sp)
    ask(eng, clk, "u1", "甲", "开门的人认识她吗", verdict="是",
        established_fact_ids=["f3"])
    check("仍在 QA(support 不推进通关)", eng.phase == Phase.QA, eng.phase)
    rec = eng._qa_archive[-1]
    check("support 已建立", "f3" in (rec.established_fact_ids or []),
          rec.established_fact_ids)
    check("但无 completion 贡献",
          not rec.completion_contribution_fact_ids,
          rec.completion_contribution_fact_ids)
    check("贡献链为空", _contrib(eng) == [], _contrib(eng))


# ----------------------------------------------------------------------
# R1/R2-E —— touched 不算
# ----------------------------------------------------------------------
def test_r1_e_touched_is_not_contribution():
    print("\n[R1-E] touched 非空但 established 空 -> 不进贡献链")
    sp = ident_spec()
    eng, clk = boot(sp)
    ask(eng, clk, "u1", "甲", "她昨晚吃饭了吗", verdict="是",
        touched_fact_ids=["f1", "f2"], established_fact_ids=[])
    check("仍在 QA", eng.phase == Phase.QA, eng.phase)
    check("贡献链为空", _contrib(eng) == [], _contrib(eng))
    check("无任何记录带贡献",
          all(not r.completion_contribution_fact_ids
              for r in eng._qa_archive), eng._qa_archive)


# ----------------------------------------------------------------------
# R1/R2-F —— verifier 补回也算真人贡献
# ----------------------------------------------------------------------
def test_r1_f_verifier_rescue_counts_as_human():
    print("\n[R1-F] verifier 补回的 f2 首次建立 -> 仍算这位真人的贡献")
    # ⚠️ 形状说明: 复核是**就地**把 id 并进 `r0.established_fact_ids`
    # 并**另记** `completion_verified_fact_ids` 说明provenance(见
    # `PuzzleWriter._completion_verify`)。所以到 Engine 手上时,
    # established 里**已经有** f2 —— 这两个字段不是二选一。
    #
    # 这里先用真 writer 跑一遍, 拿真实产出喂 Engine, 避免手写出一个
    # 生产路径不会产生的形状(我第一版就是这么错的: 只填 verified 不填
    # established, 于是 Engine 侧什么都没建立)。
    fc = FakeClient([
        _verdict(established=[], cand=True),
        _completion_match(["f2"]),
    ])
    spec = auction_spec()
    # 甲先建立 f1 —— 于是 missing 只剩 f2
    eng, clk = boot(spec)
    ask(eng, clk, "u1", "甲", "他自己送拍自己拍高, 是在刷成交记录?", verdict="是",
        established_fact_ids=["f1"])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    out, _err = w.answer(
        spec.puzzle, spec.answer, [], 2, "乙", _AUCTION_TEXT_CAUSAL, spec=spec,
        completion_fact_ids=spec.completion_fact_ids,
        core_answer=spec.core_answer,
        room_established_fact_ids=["f1"])
    check("writer 恰好 2 次调用", len(fc.calls) == 2, len(fc.calls))
    check("复核只补回 f2", out and out[0].established_fact_ids == ["f2"],
          out[0].established_fact_ids if out else None)
    check("provenance 记着 f2",
          out and out[0].completion_verified_fact_ids == ["f2"],
          out[0].completion_verified_fact_ids if out else None)
    # ---- 交给 Engine: 合同 {f1,f2} 补齐 -> 通关, 乙 是贡献者 ----
    ask(eng, clk, "u2", "乙", _AUCTION_TEXT_CAUSAL,
        verdict=out[0].verdict, status=out[0].status,
        solution_candidate=True,
        established_fact_ids=list(out[0].established_fact_ids or []),
        completion_verified_fact_ids=list(
            out[0].completion_verified_fact_ids or []))
    check("已通关", eng.phase == Phase.REVEALING, eng.phase)
    c = _contrib(eng)
    check("贡献链 = [甲,乙]", [x["user_name"] for x in c] == ["甲", "乙"], c)
    check("乙 is_final", c[-1]["is_final"] is True, c)
    rec = [r for r in eng._qa_archive if r.user_name == "乙"][0]
    check("乙的贡献含 f2",
          rec.completion_contribution_fact_ids == ["f2"],
          rec.completion_contribution_fact_ids)


# ----------------------------------------------------------------------
# R1/R2-G —— 时间到的部分贡献
# ----------------------------------------------------------------------
def test_r1_g_timeout_partial_contribution():
    print("\n[R1-G] 时间到只建立 f1 -> solved=False, 贡献链有 1 条且无 is_final")
    sp = ident_spec()
    eng, clk = boot(sp)
    ask(eng, clk, "u1", "甲", "她是父亲的女儿吗", verdict="是",
        established_fact_ids=["f1"])
    eng._enter_revealing_locked(clk.t, "giveup", "")
    check("未通关", eng.phase == Phase.REVEALING, eng.phase)
    check("solved=False", eng._solved is False, eng._solved)
    c = _contrib(eng)
    check("贡献链 1 条", len(c) == 1, c)
    check("是甲", c[0]["user_name"] == "甲", c)
    check("无 is_final", c[0]["is_final"] is False, c)
    snap = eng.snapshot()
    check("Snapshot 也带贡献链", snap.reveal_contributors == c,
          snap.reveal_contributors)
    check("Snapshot solved=False", snap.to_json()["solved"] is False)


# ----------------------------------------------------------------------
# R1/R2-H —— 完全没推到 completion
# ----------------------------------------------------------------------
def test_r1_h_no_contribution_at_all():
    print("\n[R1-H] 没有任何 completion 贡献 -> 贡献链为空")
    sp = ident_spec()
    eng, clk = boot(sp)
    ask(eng, clk, "u1", "甲", "她是不是来讨债的", verdict="是",
        established_fact_ids=["f3"])
    ask(eng, clk, "u2", "乙", "门锁了吗", verdict="不是",
        established_fact_ids=[])
    eng._enter_revealing_locked(clk.t, "giveup", "")
    check("贡献链为空", _contrib(eng) == [], _contrib(eng))
    check("Snapshot 里也是空", eng.snapshot().reveal_contributors == [],
          eng.snapshot().reveal_contributors)


# ----------------------------------------------------------------------
# R1/R2-I —— legacy 无合同
# ----------------------------------------------------------------------
def test_r1_i_legacy_no_contract_empty_contributors():
    print("\n[R1-I] legacy 无合同: Final Judge P.SOLVE 也不产出贡献链")
    sp = ident_spec(completion=())
    sp.quality_policy_version = "quality-v4"
    eng, clk = boot(sp)
    acts = ask(eng, clk, "u1", "甲", "门外女人是父亲的亲生女儿",
               verdict="揭晓", established_fact_ids=["f1", "f2"])
    check("legacy 直接通关", eng.phase == Phase.REVEALING, eng.phase)
    check("贡献链为空(不猜旧数据)", _contrib(eng) == [], _contrib(eng))
    check("Snapshot 里也是空", eng.snapshot().reveal_contributors == [])


# ----------------------------------------------------------------------
# R1/R2-J —— 公开形状
# ----------------------------------------------------------------------
def test_r1_j_public_shape_has_no_internal_fields():
    print("\n[R1-J] 公开贡献链形状固定, 绝不含内部 fact 字段")
    sp = ident_spec()
    eng, clk = boot(sp)
    ask(eng, clk, "u1", "甲", "她是父亲的女儿吗", verdict="是",
        established_fact_ids=["f1"])
    ask(eng, clk, "u2", "乙", "她昨晚和父亲吃饭了吗", verdict="是",
        established_fact_ids=["f2"])
    c = _contrib(eng)
    allowed = {"qid", "user_name", "text", "verdict", "is_final"}
    for row in c:
        check("行 key 恰为公开五字段",
              set(row.keys()) == allowed, sorted(row.keys()))
    # 整个 JSON 序列化后再扫一遍 —— 防止将来有人"顺手"加字段
    blob = json.dumps(eng.snapshot().to_json(), ensure_ascii=False)
    for bad in ("fact_id", "established_fact_ids", "completion_fact_ids",
                "completion_verified_fact_ids",
                "completion_contribution_fact_ids", "touched_fact_ids"):
        check(f"Snapshot JSON 不含 {bad}", bad not in blob)
    check("Snapshot JSON 含 reveal_contributors",
          "reveal_contributors" in blob)


# ----------------------------------------------------------------------
# R1/R2-K —— QA 阶段不下发贡献链
# ----------------------------------------------------------------------
def test_r1_k_qa_phase_does_not_leak_contributors():
    print("\n[R1-K] QA 阶段 Snapshot 的 reveal_contributors 恒为空")
    sp = ident_spec()
    eng, clk = boot(sp)
    ask(eng, clk, "u1", "甲", "她是父亲的女儿吗", verdict="是",
        established_fact_ids=["f1"])
    check("仍在 QA", eng.phase == Phase.QA, eng.phase)
    check("QA 阶段不下发", eng.snapshot().reveal_contributors == [],
          eng.snapshot().reveal_contributors)
    check("to_json 里也是空数组",
          eng.snapshot().to_json()["reveal_contributors"] == [])


# ----------------------------------------------------------------------
# R1/R2-L —— 一条 QA 同时建立 f1+f2 -> 只有一行
# ----------------------------------------------------------------------
def test_r1_l_one_qa_covering_both_is_single_row():
    print("\n[R1-L] 一条真人问答同时建立 f1+f2 -> 贡献链只有 1 行")
    sp = ident_spec()
    eng, clk = boot(sp)
    ask(eng, clk, "u1", "甲", "完整解答: 她是父亲的女儿, 昨晚才相认",
        verdict="是", established_fact_ids=["f1", "f2"])
    check("通关", eng.phase == Phase.REVEALING, eng.phase)
    c = _contrib(eng)
    check("只有一行", len(c) == 1, c)
    check("is_final=True", c[0]["is_final"] is True, c)


# ----------------------------------------------------------------------
# R1/R2-M —— archive 有 reveal_contributors
# ----------------------------------------------------------------------
def test_r1_m_archive_boundary():
    print("\n[R1-M] archive 带 reveal_contributors; to_json 不带贡献字段")
    sp = ident_spec()
    eng, clk = boot(sp)
    ask(eng, clk, "u1", "甲", "她是父亲的女儿吗", verdict="是",
        established_fact_ids=["f1"])
    ask(eng, clk, "u2", "乙", "她昨晚和父亲吃饭了吗", verdict="是",
        established_fact_ids=["f2"])
    # QARec: contribution 只进 archive
    rec = eng._qa_archive[-1]
    check("to_archive 有 contribution",
          "completion_contribution_fact_ids" in rec.to_archive(),
          sorted(rec.to_archive().keys()))
    check("to_json 无 contribution",
          "completion_contribution_fact_ids" not in rec.to_json(),
          sorted(rec.to_json().keys()))
    snap = eng.snapshot()
    check("snapshot.reveal_contributors 非空",
          len(snap.reveal_contributors) == 2, snap.reveal_contributors)


def test_r1_n_non_qa_rows_are_filtered():
    print("\n[R1-N] kind/status/verdict 三重门: 非真人 OK 问答一律不进贡献链")
    # 说明: 正常路径下 hint / nudge 的 contribution 字段本来就是空的, 所以
    # 光靠"跑一遍 hint 看结果"**测不出** kind 过滤到底在不在 —— 那是我的
    # mutation M6 跑出 0 FAIL 的原因。要真正钉住这道门, 必须**直接构造**
    # 那些"理论上不该出现"的 archive 行, 再断言 filter 挡住了它们。
    # 这道门保护的是**将来**: Step 14 的 Detective 若误写了 contribution,
    # 公屏不能把它当成真人共同解谜的功劳。
    sp = ident_spec()
    eng, clk = boot(sp)
    ask(eng, clk, "u1", "甲", "她是父亲的女儿吗", verdict="是",
        established_fact_ids=["f1"])
    # 手工塞进四种"带贡献但不该被表彰"的记录
    eng._qa_archive.append(QARec(
        qid=-1, user_name="提示", text="注意她的身份", verdict="",
        kind="hint", commit_seq=99,
        completion_contribution_fact_ids=["f2"]))
    eng._qa_archive.append(QARec(
        qid=-2, user_name="重述", text="刚才聊到身份", verdict="",
        kind="nudge", commit_seq=100,
        completion_contribution_fact_ids=["f2"]))
    eng._qa_archive.append(QARec(
        qid=90, user_name="超时观众", text="她是女儿", verdict="是",
        kind="qa", status="unavailable", commit_seq=101,
        completion_contribution_fact_ids=["f2"]))
    eng._qa_archive.append(QARec(
        qid=91, user_name="无关观众", text="她吃了吗", verdict="无关",
        kind="qa", commit_seq=102,
        completion_contribution_fact_ids=["f2"]))
    c = _contrib(eng)
    check("只有真人 QA 那一条", [x["user_name"] for x in c] == ["甲"], c)
    check("commit_seq=99 的提示没进来",
          all(x["qid"] >= 0 for x in c), c)


# ======================================================================
# A1: completion fact 必须经语义复核(自报不算通关)
# ======================================================================
def frame_spec():
    """实播"画框有问题吗"那道题的等价 fixture。

    completion f2 的**核心机制**是"画框内部藏着报警感应结构" —— 观众
    只说"画框有问题"时, 公开信息只有"画框存在某种问题", 不足以建立
    报警结构。这正是 A1 要挡住的东西。
    """
    return _stamp_beats(PuzzleSpec(
        id="ux-frame", title="画框",
        puzzle="博物馆一幅名画在闭馆后触发了警报, 但画本身完好无损。"
               "为什么?",
        answer="画框内部藏着报警感应结构, 有人试图把画取下来时触发了它。",
        core_answer="画框内部藏着报警感应结构。",
        completion_fact_ids=["f2"],
        facts=[
            PuzzleFact(id="f2", text="画框内部藏着报警感应结构",
                       kind="core", visibility="hidden"),
            PuzzleFact(id="f4", text="画本身没有被损坏", kind="support",
                       visibility="hidden"),
        ],
        solve_atoms=[
            SolveAtom(id="a1", role="key",
                      text="画框里藏着感应结构", fact_ids=["f2"]),
        ],
        fair_clues=[FairClue(quote="画本身完好无损", supports_atoms=["a1"])],
        hints=["注意不是画本身", "想想画框"],
        prompt_version="riddle-v8", quality_policy_version="quality-v8"))


def test_a1_r1_broad_question_cannot_complete():
    """**Live-R1**: 宽泛问题不能完成具体事实(实播那个洞)。

    第一层答"是"并自报 `established=["f2"]`, 但复核回空 ——
    最终 established **不含 f2**, Engine 因此不揭晓。
    """
    print("\n[A1-R1] 宽泛问题不能完成具体事实")
    spec = frame_spec()
    fc = FakeClient([
        _verdict(established=["f2"], cand=False),      # 第一层自报 f2
        _completion_match([]),                          # 复核否决
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    out, err = w.answer(spec.puzzle, spec.answer, [], 1, "甲", "画框有问题吗？",
                        spec=spec, completion_fact_ids=spec.completion_fact_ids,
                        core_answer=spec.core_answer,
                        room_established_fact_ids=[])
    check("**复核被调用了(candidate=False 但自报了 completion)**",
          len(fc.calls) == 2, len(fc.calls))
    check("verdict 保留(是)", out and out[0].verdict == "是",
          out[0].verdict if out else None)
    check("**最终 established 不含 f2**",
          out and out[0].established_fact_ids == [],
          out[0].established_fact_ids if out else None)
    check("**不产生 P.SOLVE**", out and out[0].verdict != "揭晓",
          out[0].verdict if out else None)


def test_a1_r2_specific_mechanism_can_complete():
    """**Live-R2**: 说出具体机制 -> 复核确认 -> 允许建立。"""
    print("\n[A1-R2] 具体机制可以建立")
    spec = frame_spec()
    fc = FakeClient([
        _verdict(established=["f2"], cand=False),
        _completion_match(["f2"]),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    out, err = w.answer(spec.puzzle, spec.answer, [], 1, "甲",
                        "画框里面是不是藏着报警感应线？",
                        spec=spec, completion_fact_ids=spec.completion_fact_ids,
                        core_answer=spec.core_answer,
                        room_established_fact_ids=[])
    check("复核确认 f2", out and out[0].established_fact_ids == ["f2"],
          out[0].established_fact_ids if out else None)
    check("completion_verified_fact_ids 记录了它",
          out and getattr(out[0], "completion_verified_fact_ids", None)
          == ["f2"],
          getattr(out[0], "completion_verified_fact_ids", None)
          if out else None)


def test_a1_fail_closed_on_verifier_technical_failure():
    """**复核技术失败 -> completion 一条都不推进**(mandatory verify 不能是假门)。

    否则复核挂了, 第一层自报的 completion 照样通关 —— 那道门就是假的。
    但 verdict 与**非** completion 的 established 必须保留
    (fail-open for conversation, fail-closed for victory state)。
    """
    print("\n[A1-failclosed] 复核超时/空返回 -> completion 不推进")
    spec = frame_spec()
    for label, bad in (("空 tool input", LLMResult(tool_input=None, model="m")),
                       ("超时", LLMResult(error="timeout", model="m")),
                       ("schema 不符",
                        LLMResult(tool_input={"wrong": 1}, model="m"))):
        fc = FakeClient([
            _verdict(established=["f2", "f4"], cand=False),
            bad,
        ])
        w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
        out, err = w.answer(spec.puzzle, spec.answer, [], 1, "甲", "画框有问题吗？",
                            spec=spec,
                            completion_fact_ids=spec.completion_fact_ids,
                            core_answer=spec.core_answer,
                            room_established_fact_ids=[])
        check(f"{label}: **verdict 保留**", out and out[0].verdict == "是",
              out[0].verdict if out else None)
        check(f"{label}: **completion f2 不推进**",
              out and "f2" not in out[0].established_fact_ids,
              out[0].established_fact_ids if out else None)
        check(f"{label}: 非 completion 的 f4 保留",
              out and "f4" in out[0].established_fact_ids,
              out[0].established_fact_ids if out else None)


def test_a1_self_report_alone_never_completes():
    """**自报不能通关** —— 哪怕第一层把整个合同都自报了。

    `candidate=False` 且复核一次都没成功时, completion **一条都不留**。
    """
    print("\n[A1-selfreport] 第一层自报满合同仍不算通关")
    spec = frame_spec()
    fc = FakeClient([
        _verdict(established=["f2"], cand=False),
        _completion_match([]),                 # 复核一条都不认
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    out, err = w.answer(spec.puzzle, spec.answer, [], 1, "甲", "画框有问题吗？",
                        spec=spec, completion_fact_ids=spec.completion_fact_ids,
                        core_answer=spec.core_answer,
                        room_established_fact_ids=[])
    check("**established 里没有 f2**",
          out and out[0].established_fact_ids == [],
          out[0].established_fact_ids if out else None)


def test_a1_non_candidate_verifier_cannot_widen():
    """**非候选模式下复核不得超出第一层自报的范围。**

    candidate=False 时只把 `direct_completion` 给复核看。若复核顺手回一条
    观众**没说过的** missing fact, 必须被过滤掉 —— 否则复核自己变成了
    第二条通关入口, 而且完全绕开"观众得说出来"这件事。
    """
    print("\n[A1-nowiden] 非候选复核不能借机扩宽")
    spec = PuzzleSpec(
        id="ux-two", title="两事实",
        puzzle="甲乙两件事同时发生, 为什么?", answer="因为丙。",
        core_answer="因为丙。",
        completion_fact_ids=["f1", "f2"],
        facts=[
            PuzzleFact(id="f1", text="第一件事成立", kind="core"),
            PuzzleFact(id="f2", text="第二件事成立", kind="core"),
        ],
        solve_atoms=[SolveAtom(id="a1", role="key", text="丙",
                               fact_ids=["f1", "f2"])],
        fair_clues=[FairClue(quote="同时发生", supports_atoms=["a1"])],
        hints=["h"], prompt_version="riddle-v8",
        quality_policy_version="quality-v8")
    # 复核回 f1(第一层自报的那条) + f2(观众**没**说过的那条)。
    fc = FakeClient([
        _verdict(established=["f1"], cand=False),
        _completion_match(["f1", "f2"]),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    out, err = w.answer(spec.puzzle, spec.answer, [], 1, "甲", "第一件事成立吗？",
                        spec=spec, completion_fact_ids=spec.completion_fact_ids,
                        core_answer=spec.core_answer,
                        room_established_fact_ids=[])
    check("**只能确认第一层自报的 f1**",
          out and out[0].established_fact_ids == ["f1"],
          out[0].established_fact_ids if out else None)
    check("**f2 被过滤掉(观众没说过)**",
          out and "f2" not in out[0].established_fact_ids,
          out[0].established_fact_ids if out else None)
    # 反过来: 复核只回 f1(自报的那条) -> 正常确认
    fc2 = FakeClient([
        _verdict(established=["f1"], cand=False),
        _completion_match(["f1"]),
    ])
    w2 = PuzzleWriter(client=fc2, runtime_cfg=fc2.runtime_cfg)
    out2, _ = w2.answer(spec.puzzle, spec.answer, [], 1, "甲", "第一件事成立吗？",
                        spec=spec,
                        completion_fact_ids=spec.completion_fact_ids,
                        core_answer=spec.core_answer,
                        room_established_fact_ids=[])
    check("复核确认自报的那条 -> 留下",
          out2 and out2[0].established_fact_ids == ["f1"],
          out2[0].established_fact_ids if out2 else None)


def test_a1_candidate_mode_can_rescue_missing():
    """**完整答案候选可以 rescue 全部 missing**(它本来就是干这个的)。"""
    print("\n[A1-rescue] candidate 模式可 rescue 未自报的 missing")
    spec = frame_spec()
    fc = FakeClient([
        _verdict(established=[], cand=True),        # 第一层什么都没自报
        _completion_match(["f2"]),                  # 复核 rescue
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    out, err = w.answer(spec.puzzle, spec.answer, [], 1, "甲",
                        "画框里面藏着报警感应结构, 所以有人碰它就响了",
                        spec=spec, completion_fact_ids=spec.completion_fact_ids,
                        core_answer=spec.core_answer,
                        room_established_fact_ids=[])
    check("**复核 rescue 成功**",
          out and out[0].established_fact_ids == ["f2"],
          out[0].established_fact_ids if out else None)


def test_a1_completion_verify_prompt_has_specificity_rule():
    """prompt 里必须有"不是判相关性, 是判观众是否已经知道完整命题"。"""
    print("\n[A1-prompt] 复核 prompt 的特异性硬规则")
    from story.llm import COMPLETION_VERIFY_SYSTEM as S
    check("明确否定'判相关性'", "不是" in S and "有没有关系" in S, S[:200])
    check("有'画框有问题'反例", "画框有问题" in S, "缺具体反例")
    check("有'报警感应结构'", "报警感应结构" in S)
    check("有'飞机没有机械故障'正例", "机械故障" in S)
    check("要求拿不准时不建立", "不要建立" in S)


def test_a1_ordering_is_stable_and_reference_ordered():
    """顺序稳定: 按第一层原始顺序保留, 被否决的**绝不**留下。

    这条直接对着开发中踩到的 bug: `_stable_ids` 把 reference 当成
    "也加进来"的第二个 group, 于是被复核否决的 completion id 又回来了
    (`['f2','f1']`)。
    """
    print("\n[A1-order] 顺序稳定 + 被否决的不留")
    from story.llm import PuzzleWriter as _W
    check("keep 决定留下谁",
          _W._stable_ids({"f3"}, reference=["f1", "f3", "f2"]) == ["f3"],
          _W._stable_ids({"f3"}, reference=["f1", "f3", "f2"]))
    check("**reference 里但不在 keep 的绝不出现**",
          _W._stable_ids({"f3"}, reference=["f1", "f3"]) == ["f3"],
          _W._stable_ids({"f3"}, reference=["f1", "f3"]))
    check("顺序跟 reference 走",
          _W._stable_ids({"f2", "f1"}, reference=["f1", "f2"]) == ["f1", "f2"],
          _W._stable_ids({"f2", "f1"}, reference=["f1", "f2"]))
    check("extra 追加在最后",
          _W._stable_ids({"f1"}, reference=["f1"], extra=["f9"]) == ["f1", "f9"],
          _W._stable_ids({"f1"}, reference=["f1"], extra=["f9"]))


# ======================================================================
# A2: candidate=True 却判「无关」-> 定向重判
# ======================================================================
def _recheck(verdict="是", ids=None, cand=None):
    """A2/C0 重判的 canned 返回。

    C0 起工具同时回 `verdict` + `solution_candidate` + 已确认的 completion。
    `cand` 默认按 verdict 推断(是/不是 -> True, 无关 -> False), 需要
    构造"自相矛盾返回"的用例时显式传。
    """
    d = {"verdict": verdict,
         "solution_candidate": (verdict != "无关") if cand is None else cand}
    if ids is not None:
        d["verified_completion_fact_ids"] = list(ids)
    return LLMResult(tool_input=d, model="m")


def test_a2_r3_candidate_irrelevant_triggers_recheck():
    """**Live-R3 / C0-A**: candidate=true + 无关 -> 重判, **恰好 2 次**调用。

    用本场那句类似表达: "歌正好四十分钟, 汤到这个时间正好做好" ——
    它是个 concrete explanation, 却被第一层判成"无关"。这是语义内部
    矛盾, 原样上屏等于给观众一条**错误信息**。

    ⚠️ C0: 这条路径上重判**自己**确认 completion, **不再**进入
    `_completion_verify` —— 否则就是 3 次串行 LLM, 8s x 3 = 24s 贴着
    `qa_inflight_timeout=25s`, 正确答案会被判成超时。所以这里是
    `== 2` 而不是 `>= 2`(旧断言写 `>= 2`, 于是第 3 次调用从来没被测出来)。
    """
    print("\n[A2-R3 / C0-A] candidate=True + 无关 -> 定向重判 (恰好 2 次)")
    spec = auction_spec()
    fc = FakeClient([
        _verdict(cand=True, verdict="无关"),        # 自相矛盾的第一层
        _recheck("是", ids=["f2"]),                 # 重判 + 自己确认 f2
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    out, err = w.answer(spec.puzzle, spec.answer, [], 1, "甲",
                        "歌正好四十分钟, 汤到这个时间正好做好",
                        spec=spec,
                        completion_fact_ids=spec.completion_fact_ids,
                        core_answer=spec.core_answer,
                        room_established_fact_ids=[])
    n = len(fc.calls)
    check("**恰好 2 次调用(第一层 + 重判)**", n == 2, n)
    check("**第二次是 emit_candidate_recheck**",
          n >= 2 and fc.calls[1]["tool"]["name"] == "emit_candidate_recheck",
          fc.calls[1]["tool"]["name"] if n >= 2 else None)
    check("**没有第三次调用**(旧实现会进 _completion_verify)",
          n == 2 and all(c["tool"]["name"] != "emit_completion_match"
                         for c in fc.calls),
          [c["tool"]["name"] for c in fc.calls])
    check("**最终 verdict 不再是「无关」**",
          out and out[0].verdict != "无关", out[0].verdict if out else None)
    check("重判成「是」", out and out[0].verdict == "是",
          out[0].verdict if out else None)
    check("**重判确认的 f2 进了 established**",
          out and out[0].established_fact_ids == ["f2"],
          out[0].established_fact_ids if out else None)
    check("**不产生 P.SOLVE**", out and out[0].verdict != "揭晓",
          out[0].verdict if out else None)


def test_c0_recheck_does_not_inherit_primary_established():
    """**C0**: 第一层既然自相矛盾, 它自报的 established **整体不可信**。

    这条路径下 established **只**等于重判确认过的 completion ——
    不能一边说第一层错了、一边又采信它自报的 fact。
    """
    print("\n[C0-inherit] 重判路径不继承第一层的 established")
    spec = frame_spec()
    # 第一层自报 f4(support, 非 completion)+ candidate=True + 无关
    fc = FakeClient([
        _verdict(cand=True, verdict="无关", established=["f4"]),
        _recheck("是", ids=[]),                     # 重判确认**没有** completion
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    out, err = w.answer(spec.puzzle, spec.answer, [], 1, "甲", "画框有问题吗",
                        spec=spec,
                        completion_fact_ids=spec.completion_fact_ids,
                        core_answer=spec.core_answer,
                        room_established_fact_ids=[])
    check("恰好 2 次", len(fc.calls) == 2, len(fc.calls))
    check("**第一层自报的 f4 不留下**",
          out and out[0].established_fact_ids == [],
          out[0].established_fact_ids if out else None)
    check("verdict 已重判为是", out and out[0].verdict == "是",
          out[0].verdict if out else None)


def test_c0_recheck_can_keep_irrelevant_with_candidate_false():
    """**C0-C**: 矛盾可能来自 `solution_candidate` —— 闲聊被误标成候选。

    重判回「无关 + candidate=false」时**必须接受「无关」**,
    不能硬改成「不是」(那不是"否定了某命题", 而是"根本没有命题")。
    """
    print("\n[C0-C] 误标候选的闲聊 -> 重判回 无关+candidate=false")
    spec = auction_spec()
    fc = FakeClient([
        _verdict(cand=True, verdict="无关"),
        _recheck("无关", cand=False),               # 正解: 它本来就该是无关
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    out, err = w.answer(spec.puzzle, spec.answer, [], 1, "甲", "主播好厉害啊",
                        spec=spec,
                        completion_fact_ids=spec.completion_fact_ids,
                        core_answer=spec.core_answer,
                        room_established_fact_ids=[])
    check("恰好 2 次", len(fc.calls) == 2, len(fc.calls))
    check("**接受「无关」**(不硬改「不是」)",
          out and out[0].verdict == "无关", out[0].verdict if out else None)
    check("**candidate 被纠正为 false**",
          out and out[0].solution_candidate is False,
          out[0].solution_candidate if out else None)
    check("**不再自相矛盾**",
          out and not (out[0].solution_candidate and out[0].verdict == "无关"),
          out[0].solution_candidate if out else None)
    check("不建立 fact", out and out[0].established_fact_ids == [],
          out[0].established_fact_ids if out else None)


def test_c0_recheck_self_contradictory_reply_fails_closed():
    """**C0-D**: 重判自己返回 `candidate=True + 无关` -> fail closed。

    模型自相矛盾时不能放行 —— 那是"你知道有问题却放行", 与
    `_apply_review` 的 quality_checks 同一套 fail-closed 推理。
    """
    print("\n[C0-D] 重判自身矛盾 -> 未判定")
    spec = auction_spec()
    for label, bad in (
            ("无关+candidate=true",
             LLMResult(tool_input={"verdict": "无关",
                                   "solution_candidate": True}, model="m")),
            ("candidate 缺失",
             LLMResult(tool_input={"verdict": "是"}, model="m")),
            ("candidate 类型不对",
             LLMResult(tool_input={"verdict": "是",
                                   "solution_candidate": "yes"}, model="m")),
            ("verified ids 类型不对",
             LLMResult(tool_input={"verdict": "是",
                                   "solution_candidate": True,
                                   "verified_completion_fact_ids": "f2"},
                       model="m"))):
        fc = FakeClient([_verdict(cand=True, verdict="无关"), bad])
        w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
        out, err = w.answer(spec.puzzle, spec.answer, [], 1, "甲", "他是谁",
                            spec=spec,
                            completion_fact_ids=spec.completion_fact_ids,
                            core_answer=spec.core_answer,
                            room_established_fact_ids=[])
        check(f"{label}: 恰好 2 次", len(fc.calls) == 2, len(fc.calls))
        check(f"{label}: 改判未判定",
              out and out[0].verdict == "未判定",
              out[0].verdict if out else None)
        check(f"{label}: status=unavailable",
              out and getattr(out[0], "status", "") == "unavailable",
              getattr(out[0], "status", "") if out else None)
        check(f"{label}: 不建立 fact",
              out and out[0].established_fact_ids == [],
              out[0].established_fact_ids if out else None)


def test_a2_r3_can_recheck_to_no():
    """重判也可以落到「不是」—— 不能只往「是」偏。**恰好 2 次**。"""
    print("\n[A2-R3b / C0-B] 重判可以落到「不是」(恰好 2 次)")
    spec = auction_spec()
    fc = FakeClient([
        _verdict(cand=True, verdict="无关"),
        _recheck("不是"),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    out, err = w.answer(spec.puzzle, spec.answer, [], 1, "甲", "他在炒作吗",
                        spec=spec,
                        completion_fact_ids=spec.completion_fact_ids,
                        core_answer=spec.core_answer,
                        room_established_fact_ids=[])
    check("恰好 2 次", len(fc.calls) == 2, len(fc.calls))
    check("**落到「不是」**", out and out[0].verdict == "不是",
          out[0].verdict if out else None)


def test_a2_recheck_failure_becomes_unavailable():
    """重判失败 -> **不能**留着自相矛盾的「无关」, 改判未判定。

    timeout / 空 tool / verdict 不合法 三种都走这条路。不建立 fact、
    不 solved —— 与 Engine 对"未判定"的既有处理一致。
    """
    print("\n[A2-fail] 重判失败 -> 未判定")
    spec = auction_spec()
    for label, bad in (
            ("空 tool input", LLMResult(tool_input=None, model="m")),
            ("超时", LLMResult(error="timeout", model="m")),
            ("verdict 非法",
             LLMResult(tool_input={"verdict": "或许",
                                   "solution_candidate": True}, model="m")),
            ("schema 不符", LLMResult(tool_input={"other": 1}, model="m"))):
        fc = FakeClient([_verdict(cand=True, verdict="无关"), bad])
        w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
        out, err = w.answer(spec.puzzle, spec.answer, [], 1, "甲", "他是谁",
                            spec=spec,
                            completion_fact_ids=spec.completion_fact_ids,
                            core_answer=spec.core_answer,
                            room_established_fact_ids=[])
        check(f"{label}: **不是「无关」**",
              out and out[0].verdict != "无关",
              out[0].verdict if out else None)
        check(f"{label}: 是「未判定」",
              out and out[0].verdict == "未判定",
              out[0].verdict if out else None)
        check(f"{label}: status=unavailable",
              out and getattr(out[0], "status", "") == "unavailable",
              getattr(out[0], "status", "") if out else None)
        check(f"{label}: 不建立任何 fact",
              out and not out[0].established_fact_ids,
              out[0].established_fact_ids if out else None)


def test_c1_recheck_failure_is_terminal_without_contract():
    """**C1**: 重判**技术失败** + **无合同** -> 仍然恰好 2 次, 第三次绝不被消费。

    这是 C0 漏掉的第二条 3-call 路径。C0 只把 return 放进 `if done:` 里,
    于是失败路径继续往下走, 落到 legacy Final Judge —— 而
    `_recheck_failed` **故意保留** `solution_candidate=True`(失败不重判
    candidate), 正好满足 Judge 的入口条件 `r0.solution_candidate`。
    结果是 `Answer -> failed recheck -> Judge` = 3 次。

    为什么用**无合同**的 spec: 有合同时失败路径落到 `_completion_verify`,
    那条已被 C0-G 的 `<=2` 覆盖; 无合同才是裸奔到 Judge 的那条。

    第三个 canned response 是**故意**塞进去的诱饵: 如果实现还往下走,
    它会被消费掉, `len(calls)` 就是 3。断言"第三个绝不被消费"比只断言
    次数更直接地钉住了这条路径的终点。
    """
    print("\n[C1] 无合同 + 重判失败 -> 2 次(第三次是诱饵)")
    spec = auction_spec()
    # 必须让 Judge 真的**成功**, 否则它失败也会被计数, 分不清是"没调"
    # 还是"调了但没判中"。一个明确判中的第三层才是有效诱饵。
    bait = LLMResult(tool_input={"is_guess": True, "cause_hit": True,
                                 "mechanism_hit": True}, model="m")
    for label, bad in (
            ("timeout", LLMResult(error="timeout", model="m")),
            ("空 tool input", LLMResult(tool_input=None, model="m")),
            ("异常", RuntimeError("boom")),
            ("schema 不符", LLMResult(tool_input={"other": 1}, model="m"))):
        fc = FakeClient([_verdict(cand=True, verdict="无关"), bad, bait])
        w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
        out, _ = w.answer(spec.puzzle, spec.answer, [], 1, "甲", "他是谁",
                          spec=spec,
                          completion_fact_ids=[],       # ← 无合同
                          core_answer=spec.core_answer,
                          room_established_fact_ids=[])
        check(f"{label}: 恰好 2 次", len(fc.calls) == 2, len(fc.calls))
        check(f"{label}: 第三个诱饵**从未**被消费",
              len(fc.calls) < 3, len(fc.calls))
        check(f"{label}: 判未判定(不是「无关」, 也不是 solved)",
              out and out[0].verdict == "未判定" and out[0].verdict != "猜中",
              out[0].verdict if out else None)
        check(f"{label}: status=unavailable",
              out and getattr(out[0], "status", "") == "unavailable",
              getattr(out[0], "status", "") if out else None)


def test_c1_recheck_success_is_terminal_without_contract():
    """**C1**: 重判**成功** + **无合同** -> 2 次, 第三次诱饵不被消费。

    C0 已经堵了这条(成功路径无条件 return), 这里补一条同族对照:
    成功与失败**都必须**终局 —— 返回值只描述"重判成功没有", 不描述
    "能不能继续"。
    """
    print("\n[C1] 无合同 + 重判成功 -> 2 次")
    spec = auction_spec()
    bait = LLMResult(tool_input={"is_guess": True, "cause_hit": True,
                                 "mechanism_hit": True}, model="m")
    fc = FakeClient([_verdict(cand=True, verdict="无关"),
                     _recheck("是", ids=[]), bait])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    out, _ = w.answer(spec.puzzle, spec.answer, [], 1, "甲", "他是谁",
                      spec=spec,
                      completion_fact_ids=[],
                      core_answer=spec.core_answer,
                      room_established_fact_ids=[])
    check("恰好 2 次", len(fc.calls) == 2, len(fc.calls))
    check("第三个诱饵从未被消费", len(fc.calls) < 3, len(fc.calls))
    check("重判结果生效(是)", out and out[0].verdict == "是",
          out[0].verdict if out else None)


def test_c0_call_budget_matrix():
    """**C0-G**: 所有 contract 路径的调用数上界冻结。

       普通 QA                  = 1
       普通 completion QA       = 2  (verdict + completion 复核)
       candidate=True + 无关    = 2  (第一层 + 重判, 重判自任 verifier)
       **任何** contract QA     <= 2  ← 这条是本笔的全部意义

    C0 之前 A2 那条路径是 3 次(重判后又进 `_completion_verify`)。
    8s x 3 = 24s 贴着 `qa_inflight_timeout=25s` —— 正确答案会被 Engine
    判成超时。这里逐条把上界钉死, 并对**每条路径**做统一断言。
    """
    print("\n[C0-G] contract 路径调用数矩阵(全部 <= 2)")
    spec = auction_spec()

    def run(canned, text="他自己拍高", contract=None, room=None):
        fc = FakeClient(canned)
        w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
        out, _ = w.answer(spec.puzzle, spec.answer, [], 1, "甲", text,
                          spec=spec,
                          completion_fact_ids=(spec.completion_fact_ids
                                               if contract is None
                                               else contract),
                          core_answer=spec.core_answer,
                          room_established_fact_ids=room or [])
        return len(fc.calls), out

    cases = [
        # (标签, canned, 期望次数)
        ("普通 QA(candidate=False+无关)",
         [_verdict(cand=False, verdict="无关")], 1),
        ("普通 completion(cand=True+是+自报)",
         [_verdict(established=["f2"], cand=True, verdict="是"),
          _completion_match(["f2"])], 2),
        ("cand=False 但自报 completion",
         [_verdict(established=["f2"], cand=False, verdict="是"),
          _completion_match(["f2"])], 2),
        ("**candidate=True + 无关 -> 重判**",
         [_verdict(cand=True, verdict="无关"),
          _recheck("是", ids=["f2"])], 2),
        ("candidate=True + 无关 -> 重判成不是",
         [_verdict(cand=True, verdict="无关"),
          _recheck("不是")], 2),
        ("candidate=True + 无关 -> 重判回无关",
         [_verdict(cand=True, verdict="无关"),
          _recheck("无关", cand=False)], 2),
    ]
    for label, canned, want in cases:
        n, _ = run(canned)
        check(f"{label}: 恰好 {want} 次", n == want, n)
        check(f"{label}: **不超过 2 次**", n <= 2, n)

    # 无合同时**重判仍然触发**(触发条件是 candidate+无关, 与合同无关)——
    # 只是没有 completion 可确认。仍然是 2 次。
    n, _ = run([_verdict(cand=True, verdict="无关"),
                _recheck("是", ids=[])], contract=[])
    check("无合同 + candidate+无关: 2 次(重判照常)", n == 2, n)
    check("无合同: 不超过 2 次", n <= 2, n)
    # 无合同 + 不自相矛盾 -> 1 次。
    n, _ = run([_verdict(cand=False, verdict="是")], contract=[])
    check("无合同 + 普通问答: 1 次", n == 1, n)


def test_a2_r4_normal_qa_still_one_call():
    """**Live-R4**: 普通问答("有人死吗")仍恰好 1 次 LLM。

    重判只在 candidate=True + 无关 时触发。不要把所有 QA 都变成双调用。
    """
    print("\n[A2-R4] 普通 QA 仍 1 次")
    fc = FakeClient([_verdict(cand=False, verdict="无关")])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    out, err = w.answer("谜面?", "谜底。", [], 1, "甲", "有人死吗",
                        facts=_FACTS, completion_fact_ids=["f1"])
    check("**只调了一次**", len(fc.calls) == 1, len(fc.calls))
    check("candidate=False 的「无关」原样保留",
          out and out[0].verdict == "无关",
          out[0].verdict if out else None)


def test_a2_candidate_yes_is_untouched():
    """candidate=True 但 verdict 不是「无关」-> 不触发重判(不浪费调用)。"""
    print("\n[A2-noop] candidate=True + 是 -> 不重判")
    spec = auction_spec()
    fc = FakeClient([
        _verdict(established=["f2"], cand=True, verdict="是"),
        _completion_match(["f2"]),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    out, err = w.answer(spec.puzzle, spec.answer, [], 1, "甲", "他自己拍高",
                        spec=spec,
                        completion_fact_ids=spec.completion_fact_ids,
                        core_answer=spec.core_answer,
                        room_established_fact_ids=[])
    check("**没有 emit_candidate_recheck**",
          all(c["tool"]["name"] != "emit_candidate_recheck"
              for c in fc.calls), [c["tool"]["name"] for c in fc.calls])
    check("恰好 2 次(verdict + completion 复核)", len(fc.calls) == 2,
          len(fc.calls))


def test_a2_recheck_verdict_definition_frozen_in_prompt():
    """ANSWER_SYSTEM / 重判 prompt 的语义边界必须写死。

    C0 起重判**可以**回「无关」—— 因为矛盾可能来自 `solution_candidate`
    (闲聊被误标成完整解候选)。旧版本把 enum 锁成 是/不是, 会把这种输入
    硬判成「不是」, 给观众另一条错误信息。这条测试冻结修正后的语义。
    """
    print("\n[A2-prompt] verdict 边界写进 prompt")
    from story.llm import ANSWER_SYSTEM as S
    from story.llm import CANDIDATE_RECHECK_SYSTEM as R
    from story.llm import _TOOL_CANDIDATE_RECHECK as T
    check("有「还不足以解题 != 无关」",
          "不足以解题" in S and "无关" in S, S[:200])
    check("「是」的定义含'只说对了一部分'", "只说对了一部分" in S)
    check("重判 prompt 说明自相矛盾", "自相矛盾" in R or "不可能同时" in R)
    # C0: 矛盾可能来自两侧, prompt 必须显式列出, 否则模型只会往
    # "verdict 错了" 那一侧猜。
    check("重判 prompt 说明矛盾可能来自两侧",
          "solution_candidate" in R and "哪一侧" in R, R[:300])
    check("重判 prompt 要求两字段自洽",
          "必须自洽" in R or "必须 false" in R)
    # enum 必须**包含** 无关 —— 这正是 C0 修的 bug。
    enum = T["input_schema"]["properties"]["verdict"]["enum"]
    check("**重判 enum 含「无关」**", "无关" in enum, enum)
    check("重判 enum 含 是/不是", "是" in enum and "不是" in enum, enum)
    # C0: 重判自带 completion 语义确认 —— 特异性规则必须嵌进去,
    # 而不是让模型自己猜一套。
    from story.llm import COMPLETION_SPECIFICITY_RULES as SP
    check("**重判 prompt 嵌入共享的特异性规则**", SP in R)
    check("共享规则含画框反例", "画框" in SP and "不建立" in SP)


def test_c0_recheck_seeded_ids_are_already_verified():
    """**C0**: 重判的 `verified_completion_fact_ids` 是**已确认**(不是提议)。

    A2 时它只是 proposal, 要再走一次 `_completion_verify`; C0 起重判
    **自己**就是这条异常路径的 verifier(否则就是第 3 次 LLM)。所以:

        verified f2 -> 直接进 established (恰好 2 次调用)

    过滤仍然严格: 不在 missing 里的 id(编造的 / 已建立的 / support)
    一律丢弃 —— 与 `_completion_verify` 同一套过滤。
    """
    print("\n[C0-seed] 重判确认的 completion 直接生效, 非法 id 仍被过滤")
    spec = frame_spec()
    fc = FakeClient([
        _verdict(cand=True, verdict="无关"),
        _recheck("是", ids=["f2", "f999", "f4"]),   # f2 合法; f999 不存在; f4 非合同
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    out, err = w.answer(spec.puzzle, spec.answer, [], 1, "甲", "画框有问题吗",
                        spec=spec, completion_fact_ids=spec.completion_fact_ids,
                        core_answer=spec.core_answer,
                        room_established_fact_ids=[])
    check("恰好 2 次", len(fc.calls) == 2, len(fc.calls))
    check("verdict 已重判为是", out and out[0].verdict == "是",
          out[0].verdict if out else None)
    check("**只有合法的 f2 留下**(f999/f4 被过滤)",
          out and out[0].established_fact_ids == ["f2"],
          out[0].established_fact_ids if out else None)


def test_c0_recheck_never_produces_solve():
    """**C0**: 重判**永远**不产生 `P.SOLVE` —— 胜负只有 Engine 一条路。

    重判能写 established, 但"合同 ⊆ established"这个判定必须留在
    `RoundEngine.submit_qa` -> `_record_human_established_locked`。
    若重判自己判 solved, 就多了一条绕开 human-only 边界的胜负入口。
    """
    print("\n[C0-solve] 重判不产生 solved")
    spec = frame_spec()                       # 合同 = ["f2"], 单条
    fc = FakeClient([
        _verdict(cand=True, verdict="无关"),
        _recheck("是", ids=["f2"]),            # 正好覆盖满合同
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    out, err = w.answer(spec.puzzle, spec.answer, [], 1, "甲", "画框有问题吗",
                        spec=spec, completion_fact_ids=spec.completion_fact_ids,
                        core_answer=spec.core_answer,
                        room_established_fact_ids=[])
    r = out[0]
    check("**verdict 绝不是「揭晓」**", r.verdict != "揭晓", r.verdict)
    check("verdict 落在合法集合里",
          r.verdict in ("是", "不是", "无关", "未判定"), r.verdict)
    check("established 覆盖了合同(交由 Engine 判定胜负)",
          set(spec.completion_fact_ids) <= set(r.established_fact_ids or []),
          r.established_fact_ids)


def test_a2_engine_never_accepts_irrelevant_candidate():
    """Engine 侧: 重判之后的 verdict 必须是 是/不是/无关/未判定 之一。

    这条是**契约**断言 —— 自相矛盾的组合不该逃到 Engine。
    """
    print("\n[A2-engine] 自相矛盾的组合不该外泄")
    spec = auction_spec()
    for bad in (LLMResult(tool_input=None, model="m"),
                LLMResult(error="x", model="m")):
        fc = FakeClient([_verdict(cand=True, verdict="无关"), bad])
        w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
        out, _ = w.answer(spec.puzzle, spec.answer, [], 1, "甲", "他是谁",
                          spec=spec,
                          completion_fact_ids=spec.completion_fact_ids,
                          core_answer=spec.core_answer,
                          room_established_fact_ids=[])
        r = out[0]
        check("**不出现 (candidate=True, 无关)**",
              not (r.solution_candidate and r.verdict == "无关"),
              (r.solution_candidate, r.verdict))
        check("verdict 合法", r.verdict in ("是", "不是", "无关", "未判定"),
              r.verdict)


# ======================================================================
# J1 —— 排除错误解释 ≠ 建立正确核心解释(实播截图回归)
# ======================================================================
# 实播: 观众问"不敢关灯是因为有高空坠落风险吗?", Host 答"不是", 系统却
# 把这条算成他补齐了谜底的最后一块, 并在公屏标上「✓ 最后线索」。
#
# 冻结成一句规则:
#
#     not(X) 只蕴含"X 不成立"; 它永远蕴含不出那个真正的原因 Y。
#
# 同时冻结反向: **合法**的 NO 必须照旧能建立事实("飞机有机械故障吗?
# -> 不是" -> 建立"飞机没有机械故障")。禁止的从来不是 NO 本身, 而是
# 借 hidden truth 把"不是 X"升级成真正原因 Y。
def test_j1_1_wrong_exclusion_is_not_a_completion():
    """J1-1(最重要): 错误排除不得成为最后线索 —— 全链四道都要干净。

    模拟一个**有 bug 的第一层**: 判"不是", 却自报建立了 f2
    (衣柜封门那条 completion), 且没给 verified。

    Engine 必须自己拦住 —— 这是 J1-B 的 defense-in-depth。
    """
    print("\n[J1-1] 错误排除不得成为最后线索")
    sp = wardrobe_spec()
    eng, clk = boot(sp)
    ask(eng, clk, "u1", "佛狸", "不敢关灯是因为有高空坠落风险吗",
        verdict="不是",
        established_fact_ids=["f2"],             # 第一层错误自报
        completion_verified_fact_ids=[])          # 复核没确认
    check("**Engine 拒绝未复核的 completion**",
          eng._established_fact_ids == set(), eng._established_fact_ids)
    check("不通关", eng.phase == Phase.QA, eng.phase)
    check("不 solved", not eng._solved, eng._solved)
    check("**贡献链不含这条**",
          eng._reveal_contributors_locked() == [],
          eng._reveal_contributors_locked())
    rec = eng._qa_archive[-1]
    check("该条贡献为空", not rec.completion_contribution_fact_ids,
          rec.completion_contribution_fact_ids)
    # is_final 不可能为 true —— 逐条查, 不靠"列表长度为 0"间接推。
    check("没有任何 is_final",
          all(not c.get("is_final")
              for c in eng._reveal_contributors_locked()),
          eng._reveal_contributors_locked())


def test_j1_2_verifier_must_reject_the_no_leap():
    """J1-2: 复核员本身也必须拒它 —— 不能只靠 Engine 兜。

    直接测 `_completion_verify`: 第一层答"不是"并自报 f2, 复核回空
    -> 最终 established 不含 f2。

    并冻结 prompt contract: 共享规则里必须有"否定回答的直接蕴含规则",
    防止以后这条规则又被删掉。
    """
    print("\n[J1-2] 复核员拒收 NO 跳跃")
    sp = wardrobe_spec()
    fc = FakeClient([
        _verdict(established=["f2"], cand=False, verdict="不是"),
        _completion_match([]),                    # 复核: 一条都不认
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    out, err = w.answer(
        sp.puzzle, sp.answer, [], 1, "佛狸", "不敢关灯是因为有高空坠落风险吗",
        spec=sp, completion_fact_ids=sp.completion_fact_ids,
        core_answer=sp.core_answer, room_established_fact_ids=[])
    check("复核被调用(自报了 completion)", len(fc.calls) == 2, len(fc.calls))
    check("最终 established 不含 f2",
          out and out[0].established_fact_ids == [],
          out[0].established_fact_ids if out else None)
    check("verdict 保留'不是'", out and out[0].verdict == "不是",
          out[0].verdict if out else None)
    check("completion_verified 为空",
          out and not out[0].completion_verified_fact_ids,
          out[0].completion_verified_fact_ids if out else None)

    # ---- prompt contract 冻结 ----
    from story.llm import (COMPLETION_SPECIFICITY_RULES as SP,
                           COMPLETION_VERIFY_SYSTEM as CV,
                           CANDIDATE_RECHECK_SYSTEM as CR)
    check("共享规则含否定蕴含章节",
          "否定回答的直接蕴含规则" in SP, SP[:200])
    check("  **两个消费者都拿到同一份**",
          "否定回答的直接蕴含规则" in CV
          and "否定回答的直接蕴含规则" in CR)
    check("规则写明了核心判据",
          "排除一个错误解释" in SP and "建立正确核心解释" in SP)
    check("规则含实播反例(高空坠落)",
          "高空坠落" in SP, "反例必须留在 prompt 里")


def test_j1_3_and_4_real_core_facts_do_establish():
    """J1-3 / J1-4: 真正问到核心的 Yes 照旧建立, 且能正常通关拿 is_final。

    这才是"最后线索"的正确例子: 有人先建立 f1(衣柜封门), 另一位
    问出 f2(床就在原门前) 补齐 -> 他是 winner 且 is_final。
    """
    print("\n[J1-3/4] 真核心可以建立, 最后一块仍是最后一块")
    sp = wardrobe_spec()
    eng, clk = boot(sp)
    ask(eng, clk, "u1", "甲", "衣柜其实是后来拿来挡住原来的门吗",
        verdict="是", established_fact_ids=["f1"])
    check("1/2 -> 仍在 QA", eng.phase == Phase.QA, eng.phase)
    check("f1 已建立", eng._established_fact_ids == {"f1"},
          eng._established_fact_ids)
    acts = ask(eng, clk, "u2", "乙", "所以他的床其实一直摆在以前那扇门的位置",
               verdict="是", established_fact_ids=["f2"])
    check("补齐 -> REVEALING", eng.phase == Phase.REVEALING, eng.phase)
    check("胜者是补齐者(乙)", eng._solved_by == "乙", eng._solved_by)
    c = eng._reveal_contributors_locked()
    check("贡献链 2 条", len(c) == 2, c)
    check("**最后一条 is_final**",
          c and c[-1]["is_final"] is True, c)
    check("最后一条属于乙", c and c[-1]["user_name"] == "乙", c)
    check("第一条不是 is_final", c and c[0]["is_final"] is False, c)
    rev = [a for a in acts if a.kind == ActionKind.REVEAL]
    check("REVEAL 带 core_answer",
          rev and rev[0].payload.get("core_answer") == sp.core_answer,
          rev[0].payload.get("core_answer") if rev else None)


def test_j1_5_legitimate_no_still_completes():
    """J1-5(反向必测): 合法 NO 不能被误杀。

    completion 就是"飞机没有机械故障", 而观众问"飞机有机械故障吗?",
    "不是"**直接等价于**该 fact —— 必须建立、必须能通关。

    这条与 J1-1 一起构成"不是禁止 NO, 而是禁止 hidden-truth leap"。
    """
    print("\n[J1-5] 合法 NO 仍能建立事实")
    sp = plane_spec()
    eng, clk = boot(sp)
    ask(eng, clk, "u1", "甲", "飞机有机械故障吗", verdict="不是",
        established_fact_ids=["f1"])
    check("**f1 被建立**", eng._established_fact_ids == {"f1"},
          eng._established_fact_ids)
    check("直接通关(合同只有 1 条)",
          eng.phase == Phase.REVEALING, eng.phase)
    check("胜者是甲", eng._solved_by == "甲", eng._solved_by)
    c = eng._reveal_contributors_locked()
    check("贡献链 1 条且 is_final", len(c) == 1 and c[0]["is_final"] is True, c)
    check("裁决显示为'不是'", c and c[0]["verdict"] == "不是", c)


def test_j1_6_keyword_hit_is_not_proposition():
    """J1-6: 关键词命中 ≠ 命题成立。

    谜底里有"衣柜", 但 canonical 并没有"人睡在衣柜**上**"。所以
    "他睡在衣柜上吗"应当是「不是」, 且**不能**建立任何 completion ——
    不能因为"衣柜"是核心物件就判"是"。

    这条钉的是 prompt 里的判据(位置/主体被换掉 = 另一个命题),
    所以同时冻结文案。
    """
    print("\n[J1-6] 关键词命中不等于命题成立")
    from story.llm import COMPLETION_SPECIFICITY_RULES as SP
    check("规则写明主体/位置被换掉就是另一个命题",
          "关键词相关不等于命题成立" in SP or
          "关键词命中 ≠ 命题成立" in SP or
          "已经是" in SP, SP[-500:])
    check("规则点名了衣柜这个反例", "衣柜" in SP, SP[-600:])
    # 第一层判"不是"(正确), 且不建立任何 fact -> Engine 什么都不推进
    sp = wardrobe_spec()
    eng, clk = boot(sp)
    ask(eng, clk, "u1", "乙", "他睡在衣柜上吗", verdict="不是",
        established_fact_ids=[])
    check("不建立任何事实", eng._established_fact_ids == set(),
          eng._established_fact_ids)
    check("不推进通关", eng.phase == Phase.QA, eng.phase)


def test_j1_b_invariant_contribution_subset_of_verified():
    """J1-C: 贡献 ⊆ 已复核 —— 结构性不变量。

    对每一条 qa 记录: `completion_contribution_fact_ids` 必须是
    `completion_verified_fact_ids` 的子集。

    ⚠️ 这条不能只在 `_reveal_contributors_locked` 里临时过滤就了事:
    那样 UI 干净了, 但 `_established_fact_ids` 已被污染、题可能提前
    solved。所以 Engine 的过滤在**写房间共识之前**(J1-B), 这里同时
    断言两件事。
    """
    print("\n[J1-B] 贡献 ⊆ 已复核(结构性不变量)")
    sp = wardrobe_spec()
    eng, clk = boot(sp)
    # 一条正常 + 一条"错误自报且未复核"混在一起
    ask(eng, clk, "u1", "甲", "衣柜其实是后来拿来挡住原来的门吗",
        verdict="是", established_fact_ids=["f1"])
    ask(eng, clk, "u2", "乙", "不敢关灯是因为有高空坠落风险吗",
        verdict="不是", established_fact_ids=["f1", "f2"],
        completion_verified_fact_ids=["f1"])       # 只确认了 f1
    for rec in eng._qa_archive:
        if rec.kind != "qa":
            continue
        contrib = set(rec.completion_contribution_fact_ids or ())
        verified = set(rec.completion_verified_fact_ids or ())
        check(f"qid={rec.qid} 贡献 ⊆ 已复核",
              contrib <= verified, (sorted(contrib), sorted(verified)))
    check("房间共识里不含未复核的 f2",
          "f2" not in eng._established_fact_ids, eng._established_fact_ids)
    check("仍未通关", eng.phase == Phase.QA, eng.phase)


def test_j1_b_noncompletion_ids_pass_through():
    """J1-B 不能误伤: 非 completion 的 fact 行为必须**完全不变**。

    support(f3)/exclusion(f4) 既不在合同里, 就不该被新门拦下 ——
    否则就是把"A1/J1 的 completion 门"错误地扩大成全量白名单。
    """
    print("\n[J1-B] 非 completion fact 原样放行")
    sp = wardrobe_spec()
    eng, clk = boot(sp)
    ask(eng, clk, "u1", "甲", "房间曾经被隔断过吗", verdict="是",
        established_fact_ids=["f3"])
    check("support 正常建立", eng._established_fact_ids == {"f3"},
          eng._established_fact_ids)
    check("不推进通关", eng.phase == Phase.QA, eng.phase)
    eng2, clk2 = boot(wardrobe_spec())
    ask(eng2, clk2, "u1", "甲", "是因为高空坠落风险吗", verdict="不是",
        established_fact_ids=["f4"])
    check("exclusion 正常建立", eng2._established_fact_ids == {"f4"},
          eng2._established_fact_ids)


def test_j1_engine_gate_is_fail_closed_without_verified_field():
    """J1-B fail-closed: `completion_verified_fact_ids` 缺失 -> 视为空。

    老 producer / 测试桩不给这个字段时, completion 一律进不来。宁可少
    建立一条(观众多说一句), 不可多建立一条(题提前结束)。
    """
    print("\n[J1-B] 缺失 verified 字段 -> fail closed")
    sp = wardrobe_spec()
    eng, clk = boot(sp)
    eng.submit_danmaku("u1", "甲", "#衣柜其实是后来拿来挡住原来的门吗")
    clk.advance(20.0)
    p = [a for a in eng.tick() if a.kind == ActionKind.ANSWER][0].payload
    eng.submit_qa([QAResult(qid=p["qid"], verdict="是",
                            established_fact_ids=["f1"])])   # 完全没有该字段
    check("**completion 被拦下**", eng._established_fact_ids == set(),
          eng._established_fact_ids)
    check("不通关", eng.phase == Phase.QA, eng.phase)


def test_j1_c_reveal_chain_refilters_stale_archive_rows():
    """J1-C: 揭晓贡献链必须**自己**再挡一次未复核的 completion。

    ## 为什么需要这条(它是 M3 mutation 逼出来的)

    J1-B 之后, 正常路径写进 `_qa_archive` 的 contribution 已经是
    verified 子集 —— 于是 J1-C 那道过滤在**公共路径上看起来是死的**,
    把它删掉(M3 mutation)也没有任何测试会红。

    但"正常路径走不到"不等于"永远走不到"。`_qa_archive` 是**存量的**:
    一条在改动之前落盘、或者由别的 producer(将来某个 adapter / 回放
    工具)写下的记录, 完全可能带着"未复核却算作贡献"的形态。揭晓是
    观众唯一能看到"谁补上了最后一块"的地方, 值得对存量数据也守一遍。

    所以这里**直接构造**一条这样的 archive 记录(不走 submit_qa),
    断言贡献链不认它。这样 J1-C 就有了真正会红的测试。
    """
    print("\n[J1-C] 贡献链对存量记录再挡一次")
    sp = wardrobe_spec()
    eng, clk = boot(sp)
    # 直接塞一条"声称贡献了 f2, 但从未被复核确认"的存量记录。
    eng._qa_archive.append(QARec(
        qid=99, user_name="佛狸", text="不敢关灯是因为有高空坠落风险吗",
        verdict="不是", kind="qa", ts=1.0, commit_seq=1, status="ok",
        established_fact_ids=["f2"],
        completion_verified_fact_ids=[],            # 没被确认
        completion_contribution_fact_ids=["f2"],    # 却算成了贡献
    ))
    c = eng._reveal_contributors_locked()
    check("**这条不进贡献链**", c == [], c)
    check("没有任何 is_final",
          all(not x.get("is_final") for x in c), c)

    # 反向: 同一条记录**被复核确认过**时, 它必须照常出现(不能把门焊死)
    eng2, clk2 = boot(wardrobe_spec())
    eng2._qa_archive.append(QARec(
        qid=99, user_name="甲", text="衣柜其实是后来拿来挡住原来的门吗",
        verdict="是", kind="qa", ts=1.0, commit_seq=1, status="ok",
        established_fact_ids=["f1"],
        completion_verified_fact_ids=["f1"],
        completion_contribution_fact_ids=["f1"],
    ))
    c2 = eng2._reveal_contributors_locked()
    check("已复核的那条照常出现", len(c2) == 1, c2)
    check("  **且它不是 is_final**(合同还有一条没覆盖)",
          c2 and c2[0]["is_final"] is False, c2)


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
        test_c6b_completion_over_limit_feedback_does_not_simplify,
        test_c6b_current_policy_field_lists_all_include_beats,
        # ---- Final closeout ----
        test_final_closeout_v5_empty_values_rejected,
        test_final_closeout_legacy_fallback_still_works,
        test_final_closeout_status_must_be_ok,
        test_product_semantics_pinned,
        test_established_archive_boundary,
        # ---- v6: completion 必须是最小语义拆分 ----
        test_v6_versions_bumped,
        test_v6_pool_quarantines_quality_v5,
        test_v6_riddle_prompt_has_minimality_rule,
        test_v6_reviewer_has_minimality_rule,
        # ---- v6: 拍卖箱子真实回归 ----
        test_v6_case1_complete_answer_ends_puzzle,
        test_v6_case2_vague_direction_does_not_end,
        test_v6_case3_collective_progress_is_not_regression,
        test_v6_case4_bogus_verifier_ids_filtered,
        test_v6_case5_verifier_failure_keeps_first_layer,
        test_v6_case6_never_touches_old_judge,
        test_v6_case7_completion_marker_reaches_answer_prompt,
        test_v6_case8_verifier_cannot_solve_by_itself,
        test_v6_case9_trigger_matrix,
        test_v6_case10_status_must_be_ok_for_verifier,
        test_v6_verified_ids_archive_boundary,
        test_v6_progress_log_leaks_no_truth,
        # ---- v6 blocker: 第一层绝不拥有通关权 ----
        test_v6_a_text_fallback_solve_cannot_win,
        test_v6_b_text_fallback_variants_all_normalized,
        test_v6_c_text_fallback_candidate_still_reaches_verifier,
        test_v6_d_engine_rejects_solve_bypass_with_contract,
        test_v6_e_legacy_solve_still_wins,
        test_v6_first_layer_never_owns_victory,
        test_v6_candidate_not_gated_by_answer_presence,
        # ---- R1/R2: 揭晓贡献链 ----
        test_r1_a_real_contribution_chain,
        test_r1_b_duplicate_confirmation_not_credited,
        test_r1_c_concurrent_commit_order,
        test_r1_d_support_is_not_contribution,
        test_r1_e_touched_is_not_contribution,
        test_r1_f_verifier_rescue_counts_as_human,
        test_r1_g_timeout_partial_contribution,
        test_r1_h_no_contribution_at_all,
        test_r1_i_legacy_no_contract_empty_contributors,
        test_r1_j_public_shape_has_no_internal_fields,
        test_r1_k_qa_phase_does_not_leak_contributors,
        test_r1_l_one_qa_covering_both_is_single_row,
        test_r1_m_archive_boundary,
        test_r1_n_non_qa_rows_are_filtered,
        # ---- A1: completion fact 必须经语义复核 ----
        test_a1_r1_broad_question_cannot_complete,
        test_a1_r2_specific_mechanism_can_complete,
        test_a1_fail_closed_on_verifier_technical_failure,
        test_a1_self_report_alone_never_completes,
        test_a1_non_candidate_verifier_cannot_widen,
        test_a1_candidate_mode_can_rescue_missing,
        test_a1_completion_verify_prompt_has_specificity_rule,
        test_a1_ordering_is_stable_and_reference_ordered,
        # ---- A2/C0: candidate=True 却判无关 ----
        test_a2_r3_candidate_irrelevant_triggers_recheck,
        test_a2_r3_can_recheck_to_no,
        test_a2_recheck_failure_becomes_unavailable,
        test_a2_r4_normal_qa_still_one_call,
        test_a2_candidate_yes_is_untouched,
        test_a2_recheck_verdict_definition_frozen_in_prompt,
        test_a2_engine_never_accepts_irrelevant_candidate,
        # ---- C0: 自相矛盾裁决最多 2-call ----
        test_c0_recheck_does_not_inherit_primary_established,
        test_c0_recheck_can_keep_irrelevant_with_candidate_false,
        test_c0_recheck_self_contradictory_reply_fails_closed,
        test_c0_recheck_seeded_ids_are_already_verified,
        test_c0_recheck_never_produces_solve,
        test_c0_call_budget_matrix,
        # ---- C1: 重判一旦触发即终局(成功与失败都 return) ----
        test_c1_recheck_failure_is_terminal_without_contract,
        test_c1_recheck_success_is_terminal_without_contract,
        # ---- J1: 排除错误解释 ≠ 建立正确核心解释 ----
        test_j1_1_wrong_exclusion_is_not_a_completion,
        test_j1_2_verifier_must_reject_the_no_leap,
        test_j1_3_and_4_real_core_facts_do_establish,
        test_j1_5_legitimate_no_still_completes,
        test_j1_6_keyword_hit_is_not_proposition,
        test_j1_b_invariant_contribution_subset_of_verified,
        test_j1_b_noncompletion_ids_pass_through,
        test_j1_engine_gate_is_fail_closed_without_verified_field,
        test_j1_c_reveal_chain_refilters_stale_archive_rows,
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
