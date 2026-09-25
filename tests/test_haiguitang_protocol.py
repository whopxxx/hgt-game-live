#!/usr/bin/env python
# coding: utf-8
"""运行: uv run tests/test_haiguitang_protocol.py（**完全离线, 无网络**）。

Issue #48 Phase A: Haiguitang Protocol v1 的确定性回归。

覆盖面(对应任务书 §62~§81):

    版本合同(fail closed) / completion 2~4 分档 / public_text 安全门 /
    safe helper / difficulty + categories 枚举 / requested vs primary /
    GenerationBrief / spec_version 5 与旧 archive 兼容 /
    quality-v13 池不被隔离 / Engine 4-fact 覆盖胜负 / touched 无胜负权 /
    fixtures 全量从盘上读取验证(协议 + 判题语义)。

不调用任何 LLM; 判题语义 fixture 走**真 Engine** 的 judge 输出层
(QAResult), 固定的是代码合同, 不是"某个模型今天判得对不对"。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from story.config import Config  # noqa: E402
from story.engine import RoundEngine  # noqa: E402
from story.haiguitang_protocol import (  # noqa: E402
    CATEGORIES, DIFFICULTIES, HAIGUITANG_PROTOCOL_VERSION as V1,
    LEGACY_MAX_COMPLETION_FACTS, LEGACY_MIN_COMPLETION_FACTS,
    PROTOCOL_V1_MAX_COMPLETION_FACTS, PROTOCOL_V1_MIN_COMPLETION_FACTS,
    SUPPORTED_PROTOCOL_VERSIONS, GenerationBrief, completion_bounds,
    validate_protocol,
)
from story.puzzle import (  # noqa: E402
    PuzzleFact, PuzzleSpec, quote_in_puzzle,
)
from story.pool import PuzzlePool  # noqa: E402
from story.quality import (  # noqa: E402
    QUALITY_POLICY_VERSION, MAX_COMPLETION_FACTS, validate_spec,
)
from story.state import ActionKind, Phase, QAResult  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "haiguitang" / "fixtures"

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
        return self.t


# ======================================================================
# spec 构造 —— 与 fixture 里的两道题同构
# ======================================================================
def _v1_spec(n_completion=2, protocol=V1, with_public_text=True,
             difficulty="medium", primary="warm",
             categories=("warm",), requested="warm",
             facts_extra=None):
    """n_completion 条 core/hidden completion(f1..fn) + 1 条 support。

    n=2/3/4 时 validate_spec **零 error 且零 fixable**(core hidden
    上限已 protocol-aware, Issue #49 review Blocker 1)—— 能过题池门。
    """
    facts = [
        PuzzleFact(id=f"f{i}", text=f"核心事实{i}", kind="core",
                   visibility="hidden",
                   public_text=(f"安全摘要{i}" if with_public_text else ""))
        for i in range(1, n_completion + 1)
    ]
    facts.append(PuzzleFact(id="f9", text="背景事实", kind="support",
                            visibility="hidden", hintable=False))
    if facts_extra:
        facts.extend(facts_extra)
    atoms = [
        SolveAtomLike(id=f"a{i}", role="key" if i == 1 else "mechanism",
                      text=f"推理抓手{i}", fact_ids=[f"f{i}"])
        for i in range(1, n_completion + 1)
    ]
    puzzle = "每天清晨六点，老张都会把门口的灯打开一分钟再关掉，风雨无阻。为什么？"
    from story.puzzle import PuzzleBlueprint, PuzzleSignature
    return PuzzleSpec(
        id="tp1", title="T", puzzle=puzzle,
        answer="灯是给远洋船员儿子的平安信号。",
        core_answer="灯是给跑船儿子的平安信号。",
        completion_fact_ids=[f"f{i}" for i in range(1, n_completion + 1)],
        facts=facts, solve_atoms=atoms,
        fair_clues=[FairClueLike(quote="把门口的灯打开一分钟",
                                 supports_atoms=["a1"])],
        hints=["注意开灯的时长", "想想谁在看这盏灯", "船员家属的约定"],
        discovery_beats=[
            DiscoveryBeatLike(id="b1", text="先注意到开灯时间固定",
                              fact_ids=["f1"]),
            DiscoveryBeatLike(id="b2", text="再想到灯是信号不是照明",
                              fact_ids=["f2" if n_completion > 1 else "f1"]),
        ],
        blueprint=PuzzleBlueprint(mechanism_family="social_rule",
                                  solution_shape="social_constraint",
                                  domain="family", emotion_mode="warm",
                                  time_shape="habitual", relation="family"),
        signature=PuzzleSignature(mechanism_family="social_rule",
                                  solution_shape="social_constraint",
                                  domain="family", emotion_mode="warm",
                                  time_shape="habitual", relation="family"),
        protocol_version=protocol, difficulty=difficulty,
        primary_category=primary, categories=list(categories),
        requested_category=requested,
        quality_policy_version=QUALITY_POLICY_VERSION,
    )


# 延迟导入的真实类型别名(避免文件顶部 import 噪音)
def SolveAtomLike(**kw):
    from story.puzzle import SolveAtom
    return SolveAtom(**kw)


def FairClueLike(**kw):
    from story.puzzle import FairClue
    return FairClue(**kw)


def DiscoveryBeatLike(**kw):
    from story.puzzle import DiscoveryBeat
    return DiscoveryBeat(**kw)


def _legacy_spec(n_completion=1, facts_extra=None):
    """legacy/current 形状: 无 protocol 字段、无 public_text。"""
    return _v1_spec(n_completion=n_completion, protocol="",
                    with_public_text=False, difficulty="",
                    primary="", categories=(), requested="",
                    facts_extra=facts_extra)


def _patch_spec(spec, **kw):
    """构造后的字段级修改(测试专用 —— 故意保留坏值不经过构造器)。"""
    for k, v in kw.items():
        setattr(spec, k, v)
    return spec


# ======================================================================
# P1: 版本合同
# ======================================================================
def test_version_contract():
    print("\n[P1] protocol_version fail closed")
    check("受支持集合 = ('', haiguitang-v1)",
          SUPPORTED_PROTOCOL_VERSIONS == ("", V1), SUPPORTED_PROTOCOL_VERSIONS)
    check("legacy('') 协议层零附加约束",
          validate_protocol(PuzzleSpec()) == [])
    errs = validate_protocol(_patch_spec(PuzzleSpec(),
                                         protocol_version="haiguitang-v999"))
    check("v999 被拒", any("unsupported protocol_version" in e for e in errs),
          errs)
    check("banana 被拒", any("unsupported protocol_version" in e
                             for e in validate_protocol(
                                 _patch_spec(PuzzleSpec(),
                                             protocol_version="banana"))),
          "banana 应拒")
    check("v2 被拒(不是 v1 的别名)",
          any("unsupported protocol_version" in e
              for e in validate_protocol(
                  _patch_spec(PuzzleSpec(), protocol_version="v2"))),
          "v2 应拒")
    # validate_spec 整合: unknown 版本让它 fail(即便内容完全合法)
    r = validate_spec(_patch_spec(_legacy_spec(2),
                                  protocol_version="haiguitang-v999"))
    check("validate_spec 对 unknown 版本失败",
          not r.ok and any("unsupported protocol_version" in e
                           for e in r.errors), r.errors)


# ======================================================================
# P2: PuzzleFact.public_text 序列化
# ======================================================================
def test_fact_public_text_roundtrip():
    print("\n[P2] public_text roundtrip / legacy 读空")
    f = PuzzleFact(id="f1", text="完整隐藏真相", kind="core",
                   visibility="hidden", public_text="安全摘要")
    d = f.to_dict()
    check("to_dict 带 public_text", d.get("public_text") == "安全摘要", d)
    f2 = PuzzleFact.from_dict(d)
    check("from_dict 保持 public_text", f2.public_text == "安全摘要", f2)
    old = PuzzleFact.from_dict({"id": "f1", "text": "老 fact"})
    check("旧 fact 缺键 -> public_text 读成空(不猜)", old.public_text == "",
          old.public_text)
    check("旧 fact 的 text 不受影响", old.text == "老 fact", old.text)
    # 绝不 fallback
    spec = _v1_spec(2)
    spec.facts[0].public_text = ""
    out = spec.public_established_completion_facts({"f1", "f2"})
    check("public_text 为空的 fact 不进 helper 输出",
          [x["id"] for x in out] == ["f2"], out)


# ======================================================================
# P3/P4: completion 条数分档
# ======================================================================
def test_completion_bounds_by_protocol():
    print("\n[P3] v1 completion 2~4; legacy 1~2 不变")
    check("分档常量", (LEGACY_MIN_COMPLETION_FACTS, LEGACY_MAX_COMPLETION_FACTS)
          == (1, 2) and MAX_COMPLETION_FACTS == 2
          and (PROTOCOL_V1_MIN_COMPLETION_FACTS, PROTOCOL_V1_MAX_COMPLETION_FACTS)
          == (2, 4), "常量被改动了")
    check("completion_bounds('') -> 1~2", completion_bounds("") == (1, 2),
          completion_bounds(""))
    check("completion_bounds(v1) -> 2~4",
          completion_bounds(V1) == (2, 4), completion_bounds(V1))
    for n in (2, 3, 4):
        s = _v1_spec(n)
        check(f"v1 {n} 条: validate_protocol 通过", validate_protocol(s) == [],
              validate_protocol(s))
        r = validate_spec(s)
        check(f"v1 {n} 条: validate_spec 通过", r.ok, r.errors)
        # Issue #49 review Blocker 1 反证: 合法的 2/3/4-fact v1 必须
        # **零 fixable** —— 池门拒绝任何带 fixable 的 spec, 留一条
        # "ok=True 但有 can_fix"都会让合法 v1 进不了题池。
        check(f"v1 {n} 条: validate_spec 零 fixable", r.fixable == [],
              r.fixable)
    for n, frag in ((1, "应为 2~4 条"), (5, "应为 2~4 条")):
        s = _v1_spec(n)
        errs = validate_protocol(s)
        check(f"v1 {n} 条: validate_protocol 拒", any(frag in e for e in errs),
              errs)
        r = validate_spec(s)
        check(f"v1 {n} 条: validate_spec 拒", not r.ok, r.errors)
    # legacy 1 条保持既有合法性(§12/§66) —— 这是 Phase A 的兼容核心
    s1 = _legacy_spec(1)
    check("legacy 1 条仍合法", validate_spec(s1).ok, validate_spec(s1).errors)
    check("legacy 1 条协议层无附加约束", validate_protocol(s1) == [])
    # legacy 3 条仍按旧规则拒(合同没有被全局放宽, §50)
    check("legacy 3 条仍被拒(未放宽)",
          not validate_spec(_legacy_spec(3)).ok,
          "legacy 3 条不应因 v1 而变合法")
    # legacy 的 core hidden 上限(3)也**不被** v1 放宽: 4 条 core hidden
    # 在 legacy 下仍带 can_fix 注记
    extra = [PuzzleFact(id=f"f{i}", text=f"额外核心{i}", kind="core",
                        visibility="hidden") for i in (5, 6)]
    rl = validate_spec(_legacy_spec(2, facts_extra=extra))
    check("legacy 4 条 core hidden 仍被标 can_fix(上限未放宽)",
          rl.ok and any("core hidden facts" in f for f in rl.fixable),
          rl.fixable)
    # 而 4 条 core hidden 在 **v1** 下必须 clean(Blocker 1 的另一半):
    check("v1 4 条 core hidden 零 fixable(不再不可修)",
          validate_spec(_v1_spec(4)).fixable == [],
          validate_spec(_v1_spec(4)).fixable)


# ======================================================================
# P5: completion fact 类型硬门
# ======================================================================
def test_completion_fact_kinds():
    print("\n[P5] completion 仍只能 core+hidden, v1 加 public_text 门")

    def with_facts(*facts):
        s = _v1_spec(2)
        s.facts = list(facts)
        return s

    core = PuzzleFact(id="f1", text="c1", kind="core", visibility="hidden",
                      public_text="p1")
    core2 = PuzzleFact(id="f2", text="c2", kind="core", visibility="hidden",
                       public_text="p2")
    check("core+hidden 通过",
          validate_spec(with_facts(core, core2)).ok, "应通过")
    support = PuzzleFact(id="f1", text="s", kind="support",
                         visibility="hidden", public_text="p")
    check("support 作为 completion 被拒",
          not validate_spec(with_facts(support, core2)).ok, "应拒")
    exclu = PuzzleFact(id="f1", text="e", kind="exclusion",
                       visibility="hidden", public_text="p")
    check("exclusion 作为 completion 被拒",
          not validate_spec(with_facts(exclu, core2)).ok, "应拒")
    pub = PuzzleFact(id="f1", text="c1", kind="core", visibility="public",
                     public_text="p1")
    check("public 作为 completion 被拒",
          not validate_spec(with_facts(pub, core2)).ok, "应拒")
    check("不存在的 id 被拒",
          not validate_spec(_patch_spec(_v1_spec(2),
                                        completion_fact_ids=["f1", "f9"])).ok,
          "缺 id 应拒")
    check("重复 id 被拒",
          not validate_spec(_patch_spec(_v1_spec(2),
                                        completion_fact_ids=["f1", "f1"])).ok,
          "重复应拒")
    no_pub = PuzzleFact(id="f1", text="c1", kind="core", visibility="hidden",
                        public_text="")
    errs = validate_protocol(with_facts(no_pub, core2))
    check("v1 缺 public_text 被协议门拒",
          any("缺 public_text" in e for e in errs), errs)


# ======================================================================
# P6: safe helper
# ======================================================================
def test_public_helper():
    print("\n[P6] public_established_completion_facts 安全边界")
    s = _v1_spec(4)
    out = s.public_established_completion_facts({"f1", "f3"})
    check("精确只有已建立的两条", [x["id"] for x in out] == ["f1", "f3"], out)
    check("text 只取 public_text",
          [x["text"] for x in out] == ["安全摘要1", "安全摘要3"], out)
    hidden_texts = {f.text for f in s.facts} - {"安全摘要1", "安全摘要3"}
    check("canonical text 完全不出现在结果里",
          all(x["text"] not in hidden_texts for x in out), out)
    # 顺序 = completion 合同顺序, 不是 set/facts 顺序
    s2 = _v1_spec(2)
    s2.completion_fact_ids = ["f2", "f1"]
    out2 = s2.public_established_completion_facts({"f1", "f2"})
    check("输出按 completion_fact_ids 合同顺序",
          [x["id"] for x in out2] == ["f2", "f1"], out2)
    # 全部缺 public_text -> 空(绝不 fallback, §69)
    s3 = _v1_spec(2, with_public_text=False)
    check("缺 public_text 的 helper 输出为空而不是 canonical text",
          s3.public_established_completion_facts({"f1", "f2"}) == [],
          "fallback 泄漏")
    # 未建立的不返回
    check("未 established 的不返回",
          s.public_established_completion_facts({"f1"}) ==
          [{"id": "f1", "text": "安全摘要1"}], "应只有 f1")
    check("空输入 -> 空输出",
          s.public_established_completion_facts(None) == [], "应空")


# ======================================================================
# P7: difficulty
# ======================================================================
def test_difficulty():
    print("\n[P7] difficulty 固定枚举")
    check("枚举 = easy/medium/hard",
          DIFFICULTIES == ("easy", "medium", "hard"), DIFFICULTIES)
    for d in DIFFICULTIES:
        errs = validate_protocol(_v1_spec(2, difficulty=d))
        check(f"{d} 合法", errs == [], errs)
    for d in ("", "normal", "困难", "简单"):
        errs = validate_protocol(_v1_spec(2, difficulty=d))
        check(f"{d!r} 对 v1 非法", any("difficulty 非法" in e for e in errs),
              errs)
    check("legacy difficulty='' 可读",
          PuzzleSpec.from_dict({"puzzle": "p", "answer": "a"}).difficulty == ""
          and _legacy_spec(1).difficulty == "", "legacy 空难度必须保持未知")
    check("不从 completion 数量推 difficulty: 2 条也可以是 hard",
          validate_protocol(_v1_spec(2, difficulty="hard")) == []
          and validate_protocol(_v1_spec(4, difficulty="easy")) == [],
          "难度与条数必须独立")


# ======================================================================
# P8: categories
# ======================================================================
def test_categories():
    print("\n[P8] 固定 11 类 / 1~3 条 / 含 primary / 无重复")
    check("11 类精确", len(CATEGORIES) == 11 and
          set(CATEGORIES) == {"logic", "suspense", "horror", "twist",
                              "brainstorm", "family", "crime", "tragedy",
                              "warm", "comedy", "sci_fi"}, CATEGORIES)
    ok_cases = [
        ("crime", ["crime"]),
        ("crime", ["crime", "suspense", "twist"]),
        ("warm", ["warm"]),
    ]
    for prim, cats in ok_cases:
        errs = validate_protocol(_v1_spec(2, primary=prim, categories=cats))
        check(f"{prim}/{cats} 合法", errs == [], errs)
    bad_cases = [
        ("crime", [], "categories 有 0 条"),
        ("crime", ["crime", "crime"], "categories 有重复"),
        ("crime", ["suspense"], "必须在 categories 里"),
        ("crime", ["crime", "suspense", "twist", "horror"], "categories 有 4 条"),
        ("crime", ["mystery"], "categories 含非法值"),
        ("mystery", ["logic"], "primary_category 非法"),
    ]
    for prim, cats, frag in bad_cases:
        errs = validate_protocol(_v1_spec(2, primary=prim, categories=cats))
        check(f"{prim}/{cats} 被拒({frag})",
              any(frag in e for e in errs), errs)


# ======================================================================
# P9: requested vs primary
# ======================================================================
def test_requested_mismatch():
    print("\n[P9] requested != primary 合法; roundtrip 分别保存")
    s = _v1_spec(2, requested="sci_fi", primary="suspense",
                 categories=("suspense", "sci_fi"))
    check("mismatch 是合法 spec", validate_protocol(s) == []
          and validate_spec(s).ok, validate_spec(s).errors)
    d = s.to_dict()
    back = PuzzleSpec.from_dict(d)
    check("roundtrip 后 requested 保留",
          back.requested_category == "sci_fi", back.requested_category)
    check("roundtrip 后 primary 保留",
          back.primary_category == "suspense", back.primary_category)
    check("roundtrip 后 categories 保留",
          back.categories == ["suspense", "sci_fi"], back.categories)
    check("两者没有合并成一个字段",
          back.requested_category != back.primary_category, "应为不同值")
    check("requested 为空合法(自由生成)",
          validate_protocol(_v1_spec(2, requested="")) == [], "应合法")
    errs = validate_protocol(_v1_spec(2, requested="mystery"))
    check("requested 非法值被拒",
          any("requested_category 非法" in e for e in errs), errs)


# ======================================================================
# P10: GenerationBrief
# ======================================================================
def test_generation_brief():
    print("\n[P10] GenerationBrief 契约")
    check("全空 = 自由生成, 合法", GenerationBrief().validate() == [], "")
    b = GenerationBrief(requested_category="sci_fi", difficulty="hard")
    check("sci_fi/hard 合法", b.validate() == [], b.validate())
    check("frozen(不可变)", _frozen_ok(b), "应不可变")
    check("非法 category 被确定性拒绝",
          any("requested_category 非法" in e for e in
              GenerationBrief(requested_category="mystery").validate()), "")
    check("非法 difficulty 被确定性拒绝",
          any("difficulty 非法" in e for e in
              GenerationBrief(difficulty="normal").validate()), "")
    d = b.to_dict()
    check("to_dict/from_dict 往返",
          GenerationBrief.from_dict(d) == b, d)
    check("from_dict 宽容(非 dict 输入)",
          GenerationBrief.from_dict(None) == GenerationBrief(), "")
    check("from_dict 保留非法值交给 validator(不偷修)",
          GenerationBrief.from_dict({"requested_category": "mystery"})
          .requested_category == "mystery", "parser 不该修")


def _frozen_ok(b):
    try:
        object.__setattr__  # noqa: B018
        b.requested_category = "x"
        return False
    except Exception:                       # noqa: BLE001
        return True


# ======================================================================
# P11: archive 版本
# ======================================================================
def test_archive_version():
    print("\n[P11] spec_version 5; 旧 archive 照读")
    a = PuzzleSpec(puzzle="p", answer="a").to_archive()
    check("新 archive 写 spec_version=5", a.get("spec_version") == 5,
          a.get("spec_version"))
    old4 = PuzzleSpec.from_dict({"spec_version": 4, "puzzle": "p",
                                 "answer": "a"})
    check("version 4 仍可读", old4.puzzle == "p", old4)
    oldmissing = PuzzleSpec.from_dict({"puzzle": "p", "answer": "a"})
    check("缺 spec_version 仍可读", oldmissing.puzzle == "p", oldmissing)
    # 新字段随 archive 走
    s = _v1_spec(2)
    a2 = s.to_archive()
    check("archive 带新字段",
          a2.get("protocol_version") == V1 and a2.get("difficulty") == "medium"
          and a2.get("primary_category") == "warm"
          and a2.get("categories") == ["warm"]
          and a2.get("requested_category") == "warm", a2.get("protocol_version"))
    back = PuzzleSpec.from_dict(a2)
    check("archive 往返保持新字段",
          back.protocol_version == V1 and back.difficulty == "medium"
          and back.categories == ["warm"], back.protocol_version)
    check("fact.public_text 随 archive 走",
          back.facts[0].public_text == "安全摘要1", back.facts[0])


# ======================================================================
# P12: quality-v13 池不被隔离(Phase A 核心安全门)
# ======================================================================
def test_pool_eligibility_unchanged():
    print("\n[P12] legacy v13 spec 的 live eligibility 不变")
    ok, why = PuzzlePool._validate_pool_spec(_legacy_spec(1))
    check("legacy 1-completion v13 spec 仍可入池", ok, why)
    ok2, why2 = PuzzlePool._validate_pool_spec(_legacy_spec(2))
    check("legacy 2-completion v13 spec 仍可入池", ok2, why2)
    s = PuzzleSpec.from_dict(json.loads(
        (FIXTURES / "protocol_v1" / "valid_legacy_current.json")
        .read_text(encoding="utf-8"))["spec"])
    ok3, why3 = PuzzlePool._validate_pool_spec(s)
    check("fixture 的 legacy spec 仍可入池", ok3, why3)
    bad = _patch_spec(_legacy_spec(2), protocol_version="banana")
    ok4, why4 = PuzzlePool._validate_pool_spec(bad)
    check("unknown protocol_version 在池门 fail closed", not ok4, why4)
    ok5, why5 = PuzzlePool._validate_pool_spec(_v1_spec(2))
    check("合法 v1 spec 也可入池(协议层不打折)", ok5, why5)
    # ---- Issue #49 review Blocker 1 反证 ----
    # 4-fact v1 曾因"core hidden>3 被标 can_fix + 池门拒一切 fixable"
    # 陷入不可修状态, 永远进不了题池。core 上限 protocol-aware 之后,
    # 合法的 4-fact v1 必须 validate_spec clean -> 池门放行。
    four = _v1_spec(4)
    vr4 = validate_spec(four)
    check("4-fact v1: validate_spec clean(ok 且零 fixable)",
          vr4.ok and not vr4.fixable, (vr4.errors, vr4.fixable))
    ok6, why6 = PuzzlePool._validate_pool_spec(four)
    check("4-fact v1: 真正过题池门", ok6, why6)


# ======================================================================
# P13/P14: Engine 覆盖胜负(纯 Engine, 不依赖 LLM)
# ======================================================================
def _boot_with_spec(spec, reveal_hold=5.0):
    clk = FakeClock()
    eng = RoundEngine(Config(sim_path="x", no_llm=True,
                             reveal_hold_seconds=reveal_hold), clock=clk)
    eng.start()
    eng.submit_riddle(spec.puzzle, spec.answer, list(spec.hints), spec=spec)
    assert eng.phase == Phase.QA, eng.phase
    return eng, clk


def _qa(eng, clk, text, verdict, established, touched=None, verified=None):
    eng.submit_danmaku("u1", "观众", "#" + text)
    clk.advance(20.0)
    acts = eng.tick()
    answers = [a for a in acts if a.kind == ActionKind.ANSWER]
    assert answers, f"没有派发 ANSWER: {acts}"
    p = answers[0].payload
    contract = set(eng._completion_fact_ids or ())
    est = [str(x) for x in (established or [])]
    if verified is None:
        verified = [x for x in est if x in contract]
    return eng.submit_qa([QAResult(
        qid=p["qid"], verdict=verdict, comment="",
        established_fact_ids=est,
        touched_fact_ids=[str(x) for x in (touched or [])],
        completion_verified_fact_ids=[str(x) for x in (verified or [])],
    )], expect_round=p.get("expect_round"),
        expect_spec_key=p.get("expect_spec_key"))


def test_engine_four_fact_coverage():
    print("\n[P13] Engine: 4 条合同 3/4 不赢, 4/4 才赢")
    s = _v1_spec(4)
    eng, clk = _boot_with_spec(s)
    for i in (1, 2, 3):
        _qa(eng, clk, f"说法{i}", "是", [f"f{i}"])
        check(f"建立 f{i} 后仍在 QA(未通关)", eng.phase == Phase.QA, eng.phase)
    check("established 已有 3 条合同事实",
          {"f1", "f2", "f3"} <= eng._established_fact_ids,
          eng._established_fact_ids)
    _qa(eng, clk, "说法4", "是", ["f4"])
    check("补齐第 4 条后 REVEALING", eng.phase == Phase.REVEALING, eng.phase)
    check("通关 = 合同覆盖", s.completion_fact_ids and
          set(s.completion_fact_ids) <= eng._established_fact_ids,
          eng._established_fact_ids)


def test_touched_has_no_victory_power():
    print("\n[P14] touched 不具有胜负权")
    s = _v1_spec(4)
    eng, clk = _boot_with_spec(s)
    _qa(eng, clk, "把四条全问一遍", "是", [],
        touched=["f1", "f2", "f3", "f4"], verified=[])
    check("全部 touched 但未 established -> 仍 QA", eng.phase == Phase.QA,
          eng.phase)
    check("touched 只进探索记录",
          {"f1", "f2", "f3", "f4"} <= eng._touched_fact_ids,
          eng._touched_fact_ids)
    check("established 为空", not eng._established_fact_ids,
          eng._established_fact_ids)
    # 负例: verdict=不是 + 全部 touched + 提议建立 -> 依然不建立
    _qa(eng, clk, "全都不对吧", "不是", ["f1"],
        touched=["f1"], verified=["f1"])
    check("「不是」不能建立 completion", "f1" not in eng._established_fact_ids,
          eng._established_fact_ids)


# ======================================================================
# P15: fixtures 全量从盘上读取验证
# ======================================================================
def _run_judging_case(path):
    """把 judging_v1 fixture 走**真 Engine**: viewer message -> ANSWER ->
    按 fixture 里的 judge 输出层字段回 QAResult -> 断言 expect。"""
    d = json.loads(path.read_text(encoding="utf-8"))
    spec = PuzzleSpec.from_dict(d["spec"])
    ok = True
    why = []
    eng, clk = _boot_with_spec(spec)
    for i, step in enumerate(d["steps"], 1):
        _qa(eng, clk, step["viewer_message"], step["verdict"],
            step.get("proposed_established_fact_ids"),
            touched=step.get("touched_fact_ids"),
            verified=step.get("completion_verified_fact_ids"))
        exp = step["expect"]
        for fid in exp.get("established_includes", []):
            if fid not in eng._established_fact_ids:
                ok = False
                why.append(f"step{i}: {fid} 未建立")
        for fid in exp.get("established_excludes", []):
            if fid in eng._established_fact_ids:
                ok = False
                why.append(f"step{i}: {fid} 不该建立")
        for fid in exp.get("touched_includes", []):
            if fid not in eng._touched_fact_ids:
                ok = False
                why.append(f"step{i}: {fid} 未进 touched")
        want_phase = Phase.QA if exp["phase"] == "qa" else Phase.REVEALING
        if eng.phase != want_phase:
            ok = False
            why.append(f"step{i}: phase={eng.phase} 应为 {want_phase}")
        solved = eng.phase == Phase.REVEALING
        if bool(exp.get("solved")) != solved:
            ok = False
            why.append(f"step{i}: solved={solved} 应为 {exp.get('solved')}")
    return ok, "; ".join(why) or "全部成立"


def test_protocol_fixtures():
    print("\n[P15] protocol_v1 fixtures 从盘上全量验证")
    files = sorted(FIXTURES.glob("protocol_v1" + "/*.json"))
    check("fixture 数量足够(>=18)", len(files) >= 18, len(files))
    for path in files:
        d = json.loads(path.read_text(encoding="utf-8"))
        spec = PuzzleSpec.from_dict(d["spec"])
        r = validate_spec(spec)
        want_valid = d["expect"] == "valid"
        check(f"{path.name}: expect={d['expect']}", r.ok == want_valid,
              f"errors={r.errors[:2]} fixable={len(r.fixable)}")
        if want_valid:
            check(f"{path.name}: 协议层也通过", validate_protocol(spec) == [],
                  validate_protocol(spec))
            frags = d.get("expect_fixable_contains", [])
            if isinstance(frags, str):
                frags = [frags]
            for frag in frags:
                check(f"{path.name}: 带预期 fixable 注记({frag})",
                      any(frag in f for f in r.fixable), r.fixable)
        else:
            for frag in d.get("expected_error_contains", []):
                check(f"{path.name}: 错误含「{frag}」",
                      any(frag in e for e in r.errors), r.errors)


def test_judging_fixtures():
    print("\n[P15b] judging_v1 语义反例(真 Engine, 离线)")
    for path in sorted(FIXTURES.glob("judging_v1" + "/*.json")):
        ok, why = _run_judging_case(path)
        check(f"{path.name}", ok, why)


# ======================================================================
# P16: parser 不做 validator 的工作
# ======================================================================
def test_parser_preserves_bad_data():
    print("\n[P16] from_dict 原样保留坏数据, 由 validator 报")
    d = json.loads((FIXTURES / "protocol_v1" / "invalid_category_duplicate.json")
                   .read_text(encoding="utf-8"))["spec"]
    s = PuzzleSpec.from_dict(d)
    check("重复 categories 原样保留(不偷偷去重)",
          s.categories == ["warm", "warm"], s.categories)
    check("validator 在读回的 spec 上拒绝",
          not validate_spec(s).ok, "重复必须被 validator 看到")
    s2 = PuzzleSpec.from_dict({"puzzle": "p", "answer": "a",
                               "difficulty": "banana",
                               "primary_category": "mystery",
                               "categories": ["mystery"]})
    check("difficulty 非法值原样保留", s2.difficulty == "banana", s2.difficulty)
    check("primary 非法值原样保留", s2.primary_category == "mystery",
          s2.primary_category)
    # categories 非法时"含非法值"先报(primary 检查让位, 避免噪声);
    # 单独的非法 primary(合法 categories)必须被拒且不回退 logic
    check("validator 拒非法 primary(不回退 logic)",
          any("primary_category 非法" in e for e in validate_protocol(
              _patch_spec(_v1_spec(2), primary_category="mystery"))),
          validate_protocol(_patch_spec(_v1_spec(2),
                                        primary_category="mystery")))
    # ---- Issue #49 review Blocker 2 反证 ----
    # parser 与 validator **两层**都不许把坏条目洗掉:
    #   from_dict(["crime", ""])  -> 原样 ["crime", ""]   (不是 ["crime"])
    #   from_dict(["crime", null])-> 原样 ["crime", None] (不是 ["crime"])
    #   validate_protocol 也不过滤 —— 空串/null 计入条数并被"含非法值"拒
    s_blank = PuzzleSpec.from_dict({"puzzle": "p", "answer": "a",
                                    "categories": ["crime", ""]})
    check("from_dict 原样保留空串条目", s_blank.categories == ["crime", ""],
          s_blank.categories)
    s_null = PuzzleSpec.from_dict({"puzzle": "p", "answer": "a",
                                   "categories": ["crime", None]})
    check("from_dict 原样保留 null 条目", s_null.categories == ["crime", None],
          s_null.categories)
    for tag, spec_bad in (("空串", s_blank), ("null", s_null)):
        errs_b = validate_protocol(_patch_spec(spec_bad, protocol_version=V1,
                                               completion_fact_ids=[],
                                               difficulty="",
                                               primary_category="",
                                               requested_category=""))
        check(f"validate_protocol 拒 {tag} 条目(不再静默过滤)",
              any("categories 含非法值" in e for e in errs_b), errs_b)
        vr_b = validate_spec(_patch_spec(spec_bad, protocol_version=V1,
                                         completion_fact_ids=[],
                                         difficulty="",
                                         primary_category="",
                                         requested_category=""))
        check(f"validate_spec 拒 {tag} 条目", not vr_b.ok, vr_b.errors)
    # JSON 往返也不洗: null 仍是 null
    check("JSON 往返保持 null 条目",
          PuzzleSpec.from_dict(json.loads(
              json.dumps({"categories": ["crime", None]})))
          .categories == ["crime", None], "null 被洗掉")
    # 直接构造(绕过 parser)的坏条目同样被 validator 拒 —— 钉死"validator
    # 层自己也不过滤"这半边
    direct = _v1_spec(2)
    direct.categories = ["crime", ""]
    check("validator 对直接构造的坏条目同样拒绝",
          any("categories 含非法值" in e for e in validate_protocol(direct)),
          validate_protocol(direct))


# ======================================================================
# P17: content_style/style_tags 没被顶替
# ======================================================================
def test_style_fields_not_repurposed():
    print("\n[P17] content_style 与 categories 是两条独立维度")
    s = _legacy_spec(2)
    s.content_style = ["悬疑"]
    check("legacy content_style=悬疑 不影响合法性", validate_spec(s).ok,
          validate_spec(s).errors)
    check("content_style 仍是 content_style",
          s.content_style == ["悬疑"] and s.categories == [], "字段被复用")
    v1 = _v1_spec(2, primary="suspense", categories=("suspense",))
    v1.content_style = ["悬疑"]
    check("v1 里两者并存合法", validate_spec(v1).ok, validate_spec(v1).errors)
    check("free-tag 悬疑 不是 canonical category",
          "悬疑" not in CATEGORIES, "枚举被污染")


# ======================================================================
def main():
    print("=== tests/test_haiguitang_protocol.py ===")
    test_version_contract()
    test_fact_public_text_roundtrip()
    test_completion_bounds_by_protocol()
    test_completion_fact_kinds()
    test_public_helper()
    test_difficulty()
    test_categories()
    test_requested_mismatch()
    test_generation_brief()
    test_archive_version()
    test_pool_eligibility_unchanged()
    test_engine_four_fact_coverage()
    test_touched_has_no_victory_power()
    test_protocol_fixtures()
    test_judging_fixtures()
    test_parser_preserves_bad_data()
    test_style_fields_not_repurposed()
    print()
    if FAIL[0]:
        print(f"FAILED: {FAIL[0]} check(s)")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
