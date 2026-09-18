"""运行: uv run tests/test_puzzle.py（完全离线, 无网络）。

覆盖方案 §57:
    PuzzleSpec JSON round trip
    fact id duplicate rejected
    atom references missing fact rejected
    required cause missing rejected
    required mechanism missing rejected
    fair clue quote not in puzzle rejected
    core hidden facts > 3 rejected
    legacy atoms can still migrate
"""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from story.puzzle import (  # noqa: E402
    FairClue, PuzzleBlueprint, PuzzleFact, PuzzleSignature, PuzzleSpec,
    SolveAtom, has_closing_question, is_first_person, quote_in_puzzle,
)
from story.quality import (  # noqa: E402
    Quotas, check_signature, choose_blueprint, cross_puzzle_gate,
    FAMILY_SHAPES, check_tables, is_structurally_duplicate, signature_of,
    validate_blueprint, validate_spec,
)

FAIL = [0]

PUZZLE = ("灯塔守塔人每晚都亮灯, 但只在退潮的那几个小时亮。涨潮后他反而把灯"
          "熄掉, 哪怕有船经过也一样, 为此被投诉过好几次。为什么?")


def check(name, cond, extra=""):
    if cond:
        print(f"  ok  {name}")
        return
    print(f"  FAIL {name}  {extra}")
    FAIL[0] += 1


def good_spec(**kw) -> PuzzleSpec:
    """一个**结构上完全合格**的 spec, 供各用例按需覆盖字段。"""
    facts = [
        PuzzleFact(id="f1", text="退潮时危险礁石会露出或接近水面", kind="core"),
        PuzzleFact(id="f2", text="灯的真正作用是标示危险礁石的位置", kind="core"),
        PuzzleFact(id="f3", text="涨潮后礁石被淹没, 亮灯反而会误导船只",
                   kind="support", hintable=False),
        PuzzleFact(id="f4", text="他的行为不是为了纪念死者", kind="exclusion",
                   hintable=False),
    ]
    atoms = [
        SolveAtom(id="a1", role="cause", text="退潮使危险礁石成为需要标出的目标",
                  fact_ids=["f1"]),
        SolveAtom(id="a2", role="mechanism", text="灯是在标记礁石, 而不是给船引路",
                  fact_ids=["f2", "f3"]),
    ]
    clues = [
        FairClue(quote="只在退潮的那几个小时亮", supports_atoms=["a1"]),
        FairClue(quote="涨潮后他反而把灯熄掉", supports_atoms=["a2"]),
    ]
    s = PuzzleSpec(id="p1", title="灯塔", puzzle=PUZZLE,
                   answer="退潮时礁石露出水面, 亮灯是为标出礁石位置; "
                          "涨潮后继续亮反而误导船只。",
                   facts=facts, solve_atoms=atoms, fair_clues=clues,
                   hints=["注意灯的开关时机", "想想潮水的变化",
                          "灯是在给谁传递信息?"],
                   blueprint=PuzzleBlueprint(
                       mechanism_family="hidden_function",
                       solution_shape="hidden_function_explains_behavior",
                       domain="maritime", relation="stranger",
                       emotion_mode="neutral", time_shape="habitual"),
                   signature=PuzzleSignature(
                       mechanism_family="hidden_function",
                       solution_shape="hidden_function_explains_behavior",
                       domain="maritime", emotion_mode="neutral",
                       relation="stranger"),
                   prompt_version="riddle-v3",
                   quality_policy_version="quality-v2")
    for k, v in kw.items():
        setattr(s, k, v)
    return s


# ======================================================================
def test_spec_roundtrip():
    print("[PuzzleSpec: JSON round trip]")
    s = good_spec()
    d = s.to_dict()
    s2 = PuzzleSpec.from_dict(d)
    check("round trip 后完全相等", s2.to_dict() == d, s2.to_dict())
    check("facts 保持对象", all(isinstance(f, PuzzleFact) for f in s2.facts))
    check("atoms 保持对象", all(isinstance(a, SolveAtom) for a in s2.solve_atoms))
    check("clues 保持对象", all(isinstance(c, FairClue) for c in s2.fair_clues))
    check("blueprint 保持", s2.blueprint.mechanism_family == "hidden_function",
          s2.blueprint)
    check("signature 保持", s2.signature.domain == "maritime", s2.signature)
    # archive 形态
    a = s.to_archive()
    check("archive 带 spec_version=2", a.get("spec_version") == 2, a.get("spec_version"))
    # 老 archive(只有 puzzle/answer)也要能读
    old = PuzzleSpec.from_dict({"puzzle": "老谜面。为什么?", "answer": "老谜底。"})
    check("老 archive 能读", old.puzzle == "老谜面。为什么?" and old.answer == "老谜底。",
          old)
    check("老 archive 的 facts 为空而不是报错", old.facts == [], old.facts)


def test_validate_spec_ok():
    print("[validate_spec: 合格的 spec 通过]")
    r = validate_spec(good_spec())
    check("无 error", r.ok, r.errors)
    check("why() 为空", r.why() == "", r.why())


def test_duplicate_fact_id_rejected():
    print("[validate_spec: fact id 重复 -> 拒]")
    s = good_spec()
    s.facts = list(s.facts) + [PuzzleFact(id="f1", text="重复的")]
    r = validate_spec(s)
    check("被拒", not r.ok, r.errors)
    check("指出重复", any("重复" in e for e in r.errors), r.errors)


def test_atom_missing_fact_rejected():
    print("[validate_spec: atom 引用不存在的 fact -> 拒]")
    s = good_spec()
    s.solve_atoms = [SolveAtom(id="a1", role="cause", text="x", fact_ids=["f1"]),
                     SolveAtom(id="a2", role="mechanism", text="y",
                               fact_ids=["f99"])]
    r = validate_spec(s)
    check("被拒", not r.ok, r.errors)
    check("指出缺失 fact", any("f99" in e for e in r.errors), r.errors)


def test_missing_required_cause_rejected():
    print("[validate_spec: 缺 required cause -> 拒]")
    s = good_spec()
    s.solve_atoms = [SolveAtom(id="a1", role="support", text="x", fact_ids=["f1"]),
                     SolveAtom(id="a2", role="mechanism", text="y", fact_ids=["f2"])]
    r = validate_spec(s)
    check("被拒", not r.ok, r.errors)
    check("指出缺 cause", any("cause" in e for e in r.errors), r.errors)


def test_missing_required_mechanism_rejected():
    print("[validate_spec: 缺 required mechanism -> 拒]")
    s = good_spec()
    s.solve_atoms = [SolveAtom(id="a1", role="cause", text="x", fact_ids=["f1"]),
                     SolveAtom(id="a2", role="support", text="y", fact_ids=["f2"])]
    r = validate_spec(s)
    check("被拒", not r.ok, r.errors)
    check("指出缺 mechanism", any("mechanism" in e for e in r.errors), r.errors)


def test_fair_clue_quote_must_be_in_puzzle():
    print("[validate_spec: fair_clue 的 quote 必须真在谜面里]")
    s = good_spec()
    s.fair_clues = [FairClue(quote="这句话谜面里根本没有出现过",
                             supports_atoms=["a1"])]
    r = validate_spec(s)
    check("被拒", not r.ok, r.errors)
    check("指出 quote 不在谜面", any("不在谜面" in e for e in r.errors), r.errors)
    # 轻度归一: 标点/空白/全角差异不算不同
    s2 = good_spec()
    s2.fair_clues = [FairClue(quote="只在退潮的那几个小时亮", supports_atoms=["a1"])]
    check("正常 quote 通过", validate_spec(s2).ok, validate_spec(s2).errors)
    check("归一后能匹配(全角/加空格)",
          quote_in_puzzle("只在退潮 的 那几个小时亮", PUZZLE), "应匹配")


def test_no_fair_clue_rejected():
    print("[validate_spec: 一条 fair_clue 都没有 -> 拒]")
    s = good_spec()
    s.fair_clues = []
    r = validate_spec(s)
    check("被拒", not r.ok, r.errors)
    check("指出缺 clue", any("fair_clue" in e for e in r.errors), r.errors)


def test_too_many_core_hidden_facts_rejected():
    print("[validate_spec: core hidden facts > 3 -> 拒]")
    s = good_spec()
    s.facts = [PuzzleFact(id=f"f{i}", text=f"核心{i}", kind="core")
               for i in range(1, 5)]        # 4 条 core hidden
    s.solve_atoms = [SolveAtom(id="a1", role="cause", text="x", fact_ids=["f1"]),
                     SolveAtom(id="a2", role="mechanism", text="y", fact_ids=["f2"])]
    r = validate_spec(s)
    check("被拒", not r.ok, r.errors)
    check("指出 core 太多", any("core hidden" in e for e in r.errors), r.errors)


def test_atom_count_bounds():
    print("[validate_spec: solve_atoms 数量必须在 2~4]")
    s = good_spec()
    s.solve_atoms = [SolveAtom(id="a1", role="cause", text="x", fact_ids=["f1"])]
    check("1 条被拒", not validate_spec(s).ok)
    s2 = good_spec()
    s2.solve_atoms = [SolveAtom(id=f"a{i}", role="support", text=f"t{i}",
                                fact_ids=["f1"]) for i in range(5)]
    check("5 条被拒", not validate_spec(s2).ok)


def test_hints_must_be_three_and_short():
    print("[validate_spec: hints 必须 3 条且 <=30 字]")
    s = good_spec()
    s.hints = ["a", "b"]
    check("2 条被拒", not validate_spec(s).ok)
    s2 = good_spec()
    s2.hints = ["a", "b", "x" * 31]
    r = validate_spec(s2)
    check("超长被拒", not r.ok, r.errors)
    check("指出超长", any("30 字" in e for e in r.errors), r.errors)


def test_puzzle_format_checks():
    """谜面格式问题归 `fixable` —— 由审稿人就地改, 不是直接毙。

    这三样(人称/问句/meta)都是"改一句话"的事。早先当成结构性错误直接拒,
    结果是审稿人根本没机会改它, 一道只差一个人称的好题被丢掉 ——
    而且 gen_spec 会一直重出直到次数耗尽。
    """
    print("[validate_spec: 谜面格式问题 -> fixable, 不是 error]")
    def with_puzzle(text, quote):
        """换谜面时同步换 clue —— 否则会先被"quote 不在谜面里"拦下,
        测不到格式那一档。"""
        sp = good_spec()
        sp.puzzle = text
        sp.fair_clues = [FairClue(quote=quote, supports_atoms=["a1"])]
        return sp

    s = with_puzzle("他每天晚上都亮灯, 从不断, 也从不说为什么。",
                    "他每天晚上都亮灯")
    r = validate_spec(s)
    check("没有问句: 结构仍算过", r.ok, r.errors)
    check("没有问句: 记进 fixable", any("问句" in f for f in r.fixable), r.fixable)

    s2 = with_puzzle("我每天晚上都亮灯, 从不间断。为什么?", "我每天晚上都亮灯")
    r2 = validate_spec(s2)
    check("第一人称: 结构仍算过", r2.ok, r2.errors)
    check("第一人称: 记进 fixable",
          any("第一人称" in f for f in r2.fixable), r2.fixable)

    s3 = with_puzzle(PUZZLE + " 【谜底】其实是礁石。", "只在退潮的那几个小时亮")
    r3 = validate_spec(s3)
    check("meta 污染: 结构仍算过", r3.ok, r3.errors)
    check("meta 污染: 记进 fixable",
          any("元文本" in f for f in r3.fixable), r3.fixable)

    # 结构性错误**仍然**是 error(区分必须真的生效)
    s4 = good_spec()
    s4.facts = []
    r4 = validate_spec(s4)
    check("引用缺失的 fact 仍是 error", not r4.ok, r4.errors)
    # 低层 helper
    check("has_closing_question 正例", has_closing_question("他为什么走了?"))
    check("has_closing_question 反例", not has_closing_question("他走了。"))
    check("is_first_person 引语里的我不算",
          not is_first_person('男人对酒保说：「请给我一杯水。」为什么?'))


# ======================================================================
def test_legacy_atoms_migrate():
    print("[迁移: 老的字符串 atoms 能读进来]")
    # 老 RiddleResult 里 atoms 是 ["A1","A2"] 这种纯字符串
    class R:
        puzzle = "老谜面。为什么?"
        answer = "老谜底。"
        hints = ["a", "b", "c"]
        title = "老题"
        error = None
        usage = {"input_tokens": 10}
        model = "m"
        solve_atoms = ["退潮时礁石露出水面", "亮灯是标出礁石位置"]
        fair_clues = ["谜面写了'只在退潮亮灯'"]

    spec = PuzzleSpec.from_legacy_riddle_result(R())
    check("atoms 迁移成对象", len(spec.solve_atoms) == 2, spec.solve_atoms)
    check("第 1 条推成 cause", spec.solve_atoms[0].role == "cause",
          spec.solve_atoms[0])
    check("第 2 条推成 mechanism", spec.solve_atoms[1].role == "mechanism",
          spec.solve_atoms[1])
    check("facts 由 atoms 反推", len(spec.facts) == 2, spec.facts)
    check("atom 引用了反推的 fact",
          spec.solve_atoms[0].fact_ids == ["f1"], spec.solve_atoms[0].fact_ids)
    check("clue 前缀'谜面写了'被剥掉",
          spec.fair_clues[0].quote == "只在退潮亮灯",
          spec.fair_clues[0].quote)
    # 反向: spec -> RiddleResult
    rr = spec.to_riddle_result()
    check("to_riddle_result 谜面一致", rr.puzzle == R.puzzle, rr.puzzle)
    check("to_riddle_result atoms 仍是 {role,text}",
          rr.solve_atoms[0]["role"] == "cause"
          and rr.solve_atoms[0]["text"] == "退潮时礁石露出水面", rr.solve_atoms)
    # 老 dict 形态的 atoms / clues 也要能读
    spec2 = PuzzleSpec.from_dict({
        "puzzle": "x。为什么?", "answer": "y",
        "solve_atoms": [{"role": "cause", "text": "c"},
                        {"role": "mechanism", "text": "m"}],
        "fair_clues": [{"quote": "x", "supports_atoms": ["a1"]}]})
    check("dict 形态 atoms 能读", spec2.solve_atoms[0].role == "cause",
          spec2.solve_atoms)
    check("dict 形态 clues 能读", spec2.fair_clues[0].quote == "x",
          spec2.fair_clues)
    check("缺 id 的 atom 自动补 id", spec2.solve_atoms[1].id == "a2",
          spec2.solve_atoms[1].id)


def test_validate_blueprint_flags():
    print("[validate_blueprint: 模型必须执行 blueprint 的静态标记]")
    s = good_spec()
    bp = PuzzleBlueprint(mechanism_family="hidden_function",
                         solution_shape="hidden_function_explains_behavior",
                         domain="maritime", death=False)
    s.blueprint = bp
    s.signature = PuzzleSignature(
        mechanism_family="hidden_function",
        solution_shape="hidden_function_explains_behavior",
        domain="maritime", death=True)
    r = validate_blueprint(s, bp)
    check("death=false 但自报死了人 -> 拒", not r.ok, r.errors)
    # 关键词兜底
    s2 = good_spec()
    s2.blueprint = bp
    s2.signature = PuzzleSignature(
        mechanism_family="hidden_function",
        solution_shape="hidden_function_explains_behavior", death=False)
    s2.answer = "他多年前自杀未遂, 从此每晚亮灯。"
    r2 = validate_blueprint(s2, bp)
    check("谜底出现'自杀'但 death=false -> 拒", not r2.ok, r2.errors)
    # 正常
    r3 = validate_blueprint(good_spec(),
                            PuzzleBlueprint(mechanism_family="hidden_function",
                                            solution_shape="hidden_function_explains_behavior",
                                            domain="maritime"))
    check("一致时通过", r3.ok, r3.errors)


def test_quota_blocks_after_two_deaths():
    print("[配额: 最近已有 2 道 death -> 下一题不能再死人]")
    recent = [PuzzleSignature(mechanism_family="hidden_function",
                              solution_shape="hidden_function_explains_behavior",
                              domain="maritime", death=True),
              PuzzleSignature(mechanism_family="rule_constraint",
                              solution_shape="rule_constraint",
                              domain="law", death=True)]
    sig = PuzzleSignature(mechanism_family="information_gap",
                          solution_shape="information_advantage",
                          domain="commerce", death=True)
    bad = check_signature(sig, recent, Quotas())
    check("被配额拦住", any("死人" in b for b in bad), bad)
    # death=False 的同一道题则放行
    sig2 = PuzzleSignature(mechanism_family="information_gap",
                           solution_shape="information_advantage",
                           domain="commerce", death=False)
    check("不死人则放行", check_signature(sig2, recent, Quotas()) == [],
          check_signature(sig2, recent, Quotas()))


def test_quota_blocks_trauma_ritual():
    print("[配额: 最近已有 trauma ritual -> 不再选它]")
    recent = [PuzzleSignature(
        mechanism_family="time_reinterpretation",
        solution_shape="past_trauma_explains_current_ritual",
        domain="family", emotion_mode="grief",
        past_trauma=True, repeated_ritual=True)]
    sig = PuzzleSignature(
        mechanism_family="time_reinterpretation",
        solution_shape="past_trauma_explains_current_ritual",
        domain="family", emotion_mode="grief",
        past_trauma=True, repeated_ritual=True)
    bad = check_signature(sig, recent, Quotas())
    check("被 trauma_ritual 配额拦住",
          any("创伤" in b and "怪规矩" in b for b in bad), bad)


def test_quota_blocks_same_mechanism():
    print("[配额: 某 mechanism 已 2 次 -> 下一题不能再选]")
    recent = [PuzzleSignature(mechanism_family="object_misuse",
                              solution_shape="misunderstood_object",
                              domain="food"),
              PuzzleSignature(mechanism_family="object_misuse",
                              solution_shape="hidden_function_explains_behavior",
                              domain="art")]
    sig = PuzzleSignature(mechanism_family="object_misuse",
                          solution_shape="causal_reversal", domain="sport")
    bad = check_signature(sig, recent, Quotas())
    check("被 mechanism 配额拦住", any("object_misuse" in b for b in bad), bad)


def test_scheduler_deterministic_and_respects_quota():
    print("[调度: 固定 seed -> deterministic, 且不选超配额的]")
    import random
    recent = [PuzzleSignature(mechanism_family="object_misuse",
                              solution_shape="misunderstood_object",
                              domain="food"),
              PuzzleSignature(mechanism_family="object_misuse",
                              solution_shape="hidden_function_explains_behavior",
                              domain="art"),
              PuzzleSignature(mechanism_family="hidden_function",
                              solution_shape="hidden_function_explains_behavior",
                              domain="maritime", death=True),
              PuzzleSignature(mechanism_family="rule_constraint",
                              solution_shape="rule_constraint",
                              domain="law", death=True)]
    bp1 = choose_blueprint(recent, random.Random(7))
    bp2 = choose_blueprint(recent, random.Random(7))
    check("同 seed 结果相同", bp1.to_dict() == bp2.to_dict(),
          (bp1.to_dict(), bp2.to_dict()))
    check("没选超配额的 object_misuse",
          bp1.mechanism_family != "object_misuse", bp1.mechanism_family)
    check("没选超配额的 hidden_function",
          bp1.mechanism_family != "hidden_function", bp1.mechanism_family)
    check("没选超配额的 rule_constraint",
          bp1.mechanism_family != "rule_constraint", bp1.mechanism_family)
    check("选出的 blueprint 自身过配额",
          check_signature(signature_of(bp1), recent, Quotas()) == [],
          check_signature(signature_of(bp1), recent, Quotas()))
    # 不同 seed 会给出不同结果(至少不总是同一个)
    picks = {choose_blueprint(recent, random.Random(s)).mechanism_family
             for s in range(12)}
    check("多个 seed 能覆盖多个 family", len(picks) > 1, picks)


def test_scheduler_quota_exhausted_still_returns():
    print("[调度: 配额把组合堵死时仍要返回, 不能卡死]")
    # 用极小的配额把所有 domain/relation 都堵上
    tight = Quotas(window=10, same_domain=0, same_relation=0)
    bp = choose_blueprint([], random.Random(1), tight)
    check("仍然返回了 blueprint", bp is not None, bp)
    check("mechanism_family 非空", bool(bp.mechanism_family), bp)


def test_structural_duplicate():
    print("[去重: 同 (mechanism, shape) 判为结构重复]")
    recent = [PuzzleSignature(mechanism_family="hidden_function",
                              solution_shape="hidden_function_explains_behavior",
                              domain="maritime")]
    # 换个领域(职业)但诡计+形状完全一样 -> 仍算重复
    dup = PuzzleSignature(mechanism_family="hidden_function",
                          solution_shape="hidden_function_explains_behavior",
                          domain="medical")
    check("换职业仍判重复", is_structurally_duplicate(dup, recent) != "",
          is_structurally_duplicate(dup, recent))
    # 换了 mechanism -> 不算
    fresh = PuzzleSignature(mechanism_family="identity_misread",
                            solution_shape="identity_reversal", domain="medical")
    check("换 mechanism 则放行", is_structurally_duplicate(fresh, recent) == "",
          is_structurally_duplicate(fresh, recent))


def test_cross_puzzle_gate():
    print("[跨题门: 生成后按最近的题判分布]")
    recent = [PuzzleSignature(mechanism_family="object_misuse",
                              solution_shape="misunderstood_object",
                              domain="food"),
              PuzzleSignature(mechanism_family="object_misuse",
                              solution_shape="misunderstood_object",
                              domain="art")]
    s = good_spec()
    s.signature = PuzzleSignature(mechanism_family="object_misuse",
                                  solution_shape="misunderstood_object",
                                  domain="sport")
    bad = cross_puzzle_gate(s, recent, Quotas())
    check("超配额的题被拦", len(bad) > 0, bad)
    check("原因里提到 mechanism", any("object_misuse" in b for b in bad), bad)
    # 干净的题放行
    s2 = good_spec()
    s2.signature = PuzzleSignature(mechanism_family="social_rule",
                                   solution_shape="social_constraint",
                                   domain="law")
    check("干净的题放行", cross_puzzle_gate(s2, recent, Quotas()) == [],
          cross_puzzle_gate(s2, recent, Quotas()))
    # 生成器没回传 signature -> 用 blueprint 兜底
    s3 = good_spec()
    s3.signature = PuzzleSignature()
    s3.blueprint = PuzzleBlueprint(mechanism_family="object_misuse",
                                   solution_shape="misunderstood_object",
                                   domain="sport")
    check("缺 signature 时用 blueprint 兜底",
          len(cross_puzzle_gate(s3, recent, Quotas())) > 0,
          cross_puzzle_gate(s3, recent, Quotas()))


def test_template_tables_have_no_dead_ends():
    """模板表自检: FAMILY_SHAPES 不能有非法枚举, 也不能漏掉任何 family。

    抓的是"不报错但整块选不出来"的死路 —— 实测写错过两次。
    """
    bad = check_tables()
    check("FAMILY_SHAPES 没有死路", bad == [], bad)


def test_scheduler_output_always_valid_blueprint():
    """调度器选出的 blueprint **必须**能过 validate_blueprint。

    这条是为了抓一类死路: FAMILY_SHAPES 里曾经有个 "trauma_ritual" key,
    它不是合法的 mechanism_family —— 于是从它选出来的 blueprint 会被
    validate_blueprint 直接毙掉, 那部分候选**永远出不了题**,
    而调度器看上去还"均匀覆盖"了所有 family。
    """
    from story.puzzle import MECHANISM_FAMILIES, SOLUTION_SHAPES

    for fam in FAMILY_SHAPES:
        check(f"FAMILY_SHAPES 的 key '{fam}' 是合法 family",
              fam in MECHANISM_FAMILIES, fam)
        for shape in FAMILY_SHAPES[fam]:
            check(f"  {fam} 的 shape '{shape}' 合法",
                  shape in SOLUTION_SHAPES, shape)

    # 各种 recent 状态下选出来的 blueprint 都要过校验
    import random as _r
    scen = [
        [],
        [PuzzleSignature(mechanism_family="object_misuse",
                         solution_shape="misunderstood_object", domain="food")],
        [PuzzleSignature(mechanism_family=f,
                         solution_shape=FAMILY_SHAPES[f][0],
                         domain="daily") for f in list(FAMILY_SHAPES)[:4]],
    ]
    for i, recent in enumerate(scen):
        for seed in range(6):
            bp = choose_blueprint(recent, _r.Random(seed))
            s = good_spec()
            s.blueprint = bp
            s.signature = signature_of(bp)
            r = validate_blueprint(s, bp)
            check(f"场景{i} seed{seed} 选出的 blueprint 合法",
                  r.ok, (bp.to_dict(), r.errors))


def test_trauma_ritual_quota_applies_via_shape():
    """'创伤+长年怪规矩'的严格配额要通过 shape 推导生效。"""
    recent = [PuzzleSignature(
        mechanism_family="time_reinterpretation",
        solution_shape="past_trauma_explains_current_ritual",
        domain="family", emotion_mode="grief",
        past_trauma=True, repeated_ritual=True)]
    # 同 shape 的 blueprint 必须被挡住(不管换成哪个 family)
    for fam in ("time_reinterpretation", "emotional_motive"):
        bp = PuzzleBlueprint(
            mechanism_family=fam,
            solution_shape="past_trauma_explains_current_ritual",
            domain="workplace", past_trauma=True, repeated_ritual=True,
            emotion_mode="grief")
        bad = check_signature(signature_of(bp), recent, Quotas())
        check(f"{fam} 的 trauma_ritual 被挡", len(bad) > 0, bad)


def test_signature_helpers():
    print("[signature: grief / trauma_ritual 判定]")
    s = PuzzleSignature(emotion_mode="grief")
    check("grief 命中", s.grief())
    check("neutral 不算 grief", not PuzzleSignature(emotion_mode="neutral").grief())
    t = PuzzleSignature(past_trauma=True, repeated_ritual=True)
    check("trauma_ritual 命中", t.trauma_ritual())
    check("只有 trauma 不算",
          not PuzzleSignature(past_trauma=True, repeated_ritual=False).trauma_ritual())


def test_quotas_from_config():
    print("[配额: 能从 Config 读出(方案 §40)]")
    from story.config import Config

    class C:
        quality_recent_window = 6
        quota_same_mechanism = 1
        quota_death = 0
    q = Quotas.from_config(C())
    check("window 读对", q.window == 6, q.window)
    check("same_mechanism 读对", q.same_mechanism == 1, q.same_mechanism)
    check("death 读对", q.death == 0, q.death)
    # 缺字段的 Config 用默认值
    q2 = Quotas.from_config(Config(sim_path="x"))
    check("缺字段时用默认", q2.window == 10 and q2.death == 2, q2)


def main():
    tests = [
        test_spec_roundtrip,
        test_validate_spec_ok,
        test_duplicate_fact_id_rejected,
        test_atom_missing_fact_rejected,
        test_missing_required_cause_rejected,
        test_missing_required_mechanism_rejected,
        test_fair_clue_quote_must_be_in_puzzle,
        test_no_fair_clue_rejected,
        test_too_many_core_hidden_facts_rejected,
        test_atom_count_bounds,
        test_hints_must_be_three_and_short,
        test_puzzle_format_checks,
        test_legacy_atoms_migrate,
        test_validate_blueprint_flags,
        test_quota_blocks_after_two_deaths,
        test_quota_blocks_trauma_ritual,
        test_quota_blocks_same_mechanism,
        test_scheduler_deterministic_and_respects_quota,
        test_scheduler_quota_exhausted_still_returns,
        test_structural_duplicate,
        test_cross_puzzle_gate,
        test_template_tables_have_no_dead_ends,
        test_scheduler_output_always_valid_blueprint,
        test_trauma_ritual_quota_applies_via_shape,
        test_signature_helpers,
        test_quotas_from_config,
    ]
    for t in tests:
        t()
    print()
    if FAIL[0]:
        print(f"FAILED: {FAIL[0]} 项")
        return 1
    print("PASS: PuzzleSpec / 硬校验 / 配额调度 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
