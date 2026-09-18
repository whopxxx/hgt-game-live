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
    EMOTION_MODES, FairClue, PuzzleBlueprint, PuzzleFact, PuzzleSignature,
    PuzzleSpec, REVEAL_DEFAULT, REVEAL_MODES, SolveAtom,
    has_closing_question, is_first_person, quote_in_puzzle,
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
                       relation="stranger", time_shape="habitual"),
                   prompt_version="riddle-v3",
                   quality_policy_version="quality-v3")
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
def test_reveal_mode_enum_is_reveal_only():
    """Step 01: `reveal_mode` 只描述"揭晓结构", 不混情绪。

    任务书 §15 冻结: `eerie / warm / absurd` 属于 `emotion_mode`, 不是
    揭晓结构。v1.1 里那三个混轴名字(`eerie_recontextualization` /
    `absurd_logic` / `warm_reversal`)必须**不在**枚举里 —— 它们是把两条轴
    焊在一起的产物, 一旦混进来, Scheduler 就没法分别导演"结构"和"气氛"。
    """
    for m in ("recontextualization", "identity_flip", "meaning_flip",
              "causal_flip", "goal_flip", "hidden_stakes",
              "perspective_flip", "straight_explanation"):
        check(f"reveal_mode 含 {m}", m in REVEAL_MODES, REVEAL_MODES)
    for bad in ("eerie_recontextualization", "absurd_logic", "warm_reversal",
                "eerie", "warm", "absurd", "neutral"):
        check(f"reveal_mode 不含混轴名 {bad}", bad not in REVEAL_MODES,
              REVEAL_MODES)
    # 与 emotion_mode 严格正交: 两条枚举不许有交集
    check("reveal_mode 与 emotion_mode 无交集",
          not (set(REVEAL_MODES) & set(EMOTION_MODES)),
          set(REVEAL_MODES) & set(EMOTION_MODES))


def test_blueprint_reveal_mode_default_and_roundtrip():
    """Step 01: `Blueprint.reveal_mode` 默认 `straight_explanation`。

    Blueprint 是**指令** —— 代码在出题前必须先给出一个确定的"往哪个方向
    做", 所以它需要确定的默认值。(与之相对, `Signature.reveal_mode` 是
    **观察结果**, 缺失即未知 —— 见下一条测试。)
    """
    bp = PuzzleBlueprint()
    check("Blueprint 默认 reveal_mode = straight_explanation",
          bp.reveal_mode == REVEAL_DEFAULT, bp.reveal_mode)
    bp.reveal_mode = "identity_flip"
    d = bp.to_dict()
    check("Blueprint to_dict 带 reveal_mode",
          d["reveal_mode"] == "identity_flip", d)
    check("Blueprint round trip 保持",
          PuzzleBlueprint.from_dict(d).reveal_mode == "identity_flip",
          PuzzleBlueprint.from_dict(d).reveal_mode)
    # 非法值 -> 回落(Blueprint 需要确定指令, 不能留空)
    check("Blueprint 非法 reveal_mode 回落到默认",
          PuzzleBlueprint.from_dict(
              {"reveal_mode": "乱写的值"}).reveal_mode == REVEAL_DEFAULT,
          PuzzleBlueprint.from_dict({"reveal_mode": "乱写的值"}).reveal_mode)


def test_blueprint_has_no_procedural_rule_dependency():
    """Step 01 review-fix: `procedural_rule_dependency` **只属于 Signature**。

    v1.3 冻结: 它是 Reviewer 读完成品后回传的 **observed fact**, 第一版
    只进入 `Signature / reviewer observed_signature / archive /
    cross-puzzle quota`, **不要求 Blueprint 预先指定**。

    放进 Blueprint 会制造一个"代码预先指定规则依赖"的来源 —— 而代码在
    出题之前根本无从知道这道题会不会依赖某条内部流程。以后很容易被误用
    成"让模型照抄的硬约束", 那恰恰是这个字段要避免的。
    """
    bp = PuzzleBlueprint()
    check("Blueprint 没有 procedural_rule_dependency 字段",
          not hasattr(bp, "procedural_rule_dependency"), dir(bp))
    check("Blueprint.to_dict 不含 procedural_rule_dependency",
          "procedural_rule_dependency" not in bp.to_dict(), bp.to_dict())
    # 就算磁盘上写了这个键(比如老代码写的), 也不能被读成 blueprint 属性
    loaded = PuzzleBlueprint.from_dict(
        {"mechanism_family": "hidden_function",
         "procedural_rule_dependency": True})
    check("Blueprint.from_dict 忽略 procedural_rule_dependency",
          not hasattr(loaded, "procedural_rule_dependency"), loaded)
    # 但 Signature 上必须有, 而且是 bool
    sig = PuzzleSignature()
    check("Signature 有 procedural_rule_dependency",
          sig.procedural_rule_dependency is False, sig)


def test_legacy_signature_reveal_mode_is_unknown_not_straight():
    """Step 01 review-fix: 老 Signature 的 `reveal_mode` 是 **unknown**, 不是 straight。

    这是本轮 review 最实质的一条修正。`Signature` 是**观察结果**, 它上面
    其余字段(`mechanism_family` / `domain` / `emotion_mode` / …)在缺失时
    全部是 `""`。如果只有新加的 `reveal_mode` 默认成 `straight_explanation`,
    那几百道历史题会被一律读成"普通解释", 离线分析就会得到一个**假的历史
    结论**: "历史题全是 straight"。

    "旧题借 `""` 绕过 reveal 配额"不是靠伪造 observed 数据解决的 ——
    正确位置是 Step 03 的 `quality_policy_version + quarantine`: 旧 policy
    题根本不参与 v4 live inventory。
    """
    old = PuzzleSpec.from_dict({
        "puzzle": "老谜面。为什么?", "answer": "老谜底。",
        "signature": {"mechanism_family": "hidden_function",
                      "solution_shape": "hidden_function_explains_behavior",
                      "domain": "maritime"},
        "blueprint": {"mechanism_family": "hidden_function",
                      "solution_shape": "hidden_function_explains_behavior",
                      "domain": "maritime"}})
    check("老 signature 的 reveal_mode 是空(未知)",
          old.signature.reveal_mode == "", repr(old.signature.reveal_mode))
    check("老 signature 的 procedural_rule_dependency 是 False",
          old.signature.procedural_rule_dependency is False,
          old.signature.procedural_rule_dependency)
    check("老 blueprint 的 reveal_mode 是 straight(指令需要确定值)",
          old.blueprint.reveal_mode == REVEAL_DEFAULT,
          old.blueprint.reveal_mode)
    # 与 signature 其余 observed 字段的语义一致
    check("reveal_mode 与其余 observed 字段同语义(缺失即空)",
          old.signature.mechanism_family != ""
          and old.signature.emotion_mode == ""
          and old.signature.reveal_mode == "",
          old.signature.to_dict())
    # 仍然算 v2 spec(有 signature), 所以会进配额统计
    from story.quality import _is_v2_spec
    check("老 signature 仍被认作 v2", _is_v2_spec(old))


def test_signature_reveal_mode_default_is_unknown():
    """Step 01: `Signature.reveal_mode` 的**字段默认值**也必须是 `""`。

    这条是补的 —— 上面那条测试只走 `PuzzleSpec.from_dict()`, 而
    `from_dict` 对缺失的 key 有**自己的**显式回落(`str(d.get(...) or "")`),
    所以它根本碰不到 dataclass 的字段默认值。于是"把字段默认改回
    `straight_explanation`"这个 mutation **不会让任何测试变红** ——
    实测确认过。

    但字段默认值并不是死代码: 任何直接构造 `PuzzleSignature()` 的地方
    (测试、将来的 Reviewer 合并逻辑、离线分析脚本)拿到的就是它。
    两处默认值必须一致, 否则同一个"未知"在两条路径上会读成两个值。
    """
    check("裸构造的 Signature.reveal_mode 是空",
          PuzzleSignature().reveal_mode == "",
          repr(PuzzleSignature().reveal_mode))
    check("裸构造的 Blueprint.reveal_mode 是 straight(指令需要确定值)",
          PuzzleBlueprint().reveal_mode == REVEAL_DEFAULT,
          repr(PuzzleBlueprint().reveal_mode))
    # 两条路径必须给出同一个答案
    check("字段默认与 from_dict 一致",
          PuzzleSignature().reveal_mode
          == PuzzleSignature.from_dict({}).reveal_mode,
          (PuzzleSignature().reveal_mode,
           PuzzleSignature.from_dict({}).reveal_mode))


def test_signature_reveal_mode_rejects_bad_value():
    """Step 01: 非法 observed 值回 `""`(未知), **不**伪造成 straight。"""
    d = {"puzzle": "x。为什么?", "answer": "y",
         "signature": {"mechanism_family": "hidden_function",
                       "solution_shape": "hidden_function_explains_behavior",
                       "domain": "maritime", "reveal_mode": "乱写的值"}}
    s = PuzzleSpec.from_dict(d)
    check("非法 signature.reveal_mode -> 空",
          s.signature.reveal_mode == "", repr(s.signature.reveal_mode))
    # 合法值原样保留
    d["signature"]["reveal_mode"] = "identity_flip"
    check("合法 signature.reveal_mode 保留",
          PuzzleSpec.from_dict(d).signature.reveal_mode == "identity_flip",
          PuzzleSpec.from_dict(d).signature.reveal_mode)
    # procedural 是 bool, 缺失即 False
    check("signature.procedural_rule_dependency round trip",
          PuzzleSpec.from_dict(
              {"signature": {"procedural_rule_dependency": True}}
          ).signature.procedural_rule_dependency is True)


def test_describe_does_not_leak_reveal_mode():
    """Step 01 review-fix: `describe()` 直接进生产 Prompt, 不能提前暴露新字段。

    `_gen_spec_once()` 把它拼进 RIDDLE 的 user 消息(生成器),
    `_review()` 把它拼进审稿消息。所以往这里加一行 = **改生产行为**。

    Step 01 只做 schema/serialization; 让生成器/审稿人正式理解
    `reveal_mode`(并同步 prompt version / tool schema / regression)
    是 Step 04 的事。
    """
    txt = PuzzleBlueprint(reveal_mode="identity_flip").describe()
    check("describe() 不出现 reveal_mode", "reveal_mode" not in txt, txt)
    check("describe() 不出现 procedural_rule_dependency",
          "procedural_rule_dependency" not in txt, txt)
    # 但原有的硬约束字段必须还在(别为了删一行把整段弄坏)
    for must in ("mechanism_family", "solution_shape", "domain",
                 "relation", "emotion_mode", "time_shape"):
        check(f"describe() 仍含 {must}", must in txt, txt)


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
    bp3 = PuzzleBlueprint(mechanism_family="hidden_function",
                          solution_shape="hidden_function_explains_behavior",
                          domain="maritime", relation="stranger",
                          emotion_mode="neutral", time_shape="habitual")
    r3 = validate_blueprint(good_spec(), bp3)
    check("一致时通过", r3.ok, r3.errors)


def test_blueprint_mismatch_is_rejected_not_warned():
    """P0-7: blueprint 的每一维都必须**严格**比对, 不一致是 error 不是 warn。

    早先 mechanism/solution/relation 只 warn, 于是代码说"必须
    hidden_function/commerce/neutral/stranger", 模型交回
    emotional_motive/family/grief 照样过 —— blueprint 只是"建议",
    跨题配额登记的也是假指纹。
    """
    bp = PuzzleBlueprint(mechanism_family="hidden_function",
                         solution_shape="hidden_function_explains_behavior",
                         domain="commerce", relation="stranger",
                         emotion_mode="neutral", time_shape="instant")
    # 每一维各错一个, 都必须被拒
    cases = {
        "mechanism_family": PuzzleSignature(mechanism_family="emotional_motive",
                                            solution_shape="hidden_function_explains_behavior",
                                            domain="commerce", relation="stranger",
                                            emotion_mode="neutral", time_shape="instant"),
        "solution_shape": PuzzleSignature(mechanism_family="hidden_function",
                                          solution_shape="goal_reversal",
                                          domain="commerce", relation="stranger",
                                          emotion_mode="neutral", time_shape="instant"),
        "domain": PuzzleSignature(mechanism_family="hidden_function",
                                  solution_shape="hidden_function_explains_behavior",
                                  domain="family", relation="stranger",
                                  emotion_mode="neutral", time_shape="instant"),
        "relation": PuzzleSignature(mechanism_family="hidden_function",
                                    solution_shape="hidden_function_explains_behavior",
                                    domain="commerce", relation="family",
                                    emotion_mode="neutral", time_shape="instant"),
        "emotion_mode": PuzzleSignature(mechanism_family="hidden_function",
                                        solution_shape="hidden_function_explains_behavior",
                                        domain="commerce", relation="stranger",
                                        emotion_mode="grief", time_shape="instant"),
    }
    for name, sig in cases.items():
        sp = good_spec()
        sp.blueprint, sp.signature = bp, sig
        r = validate_blueprint(sp, bp)
        check(f"{name} 不一致 -> error", not r.ok, r.errors)
        check(f"  错误信息点到 {name}",
              any(name in e for e in r.errors), r.errors)


def test_time_shape_is_not_a_hard_constraint():
    """P1(第二轮 review): `time_shape` 降为 observed metadata, 不拒稿。

    原因: blueprint 里它只有一个默认值 instant, 而严格比对下"每天做某事 /
    连续几天 / 长年观察 / 固定规矩"这类**完全合理**的题全被判死。实测 3 个
    seed 里就有 1 个因为"长年习惯 vs time_shape=instant"多花一轮重出 ——
    这个字段于是从"增加多样性"变成了"提高废稿率"。

    现在生成器如实回传, 代码只统计(见 signature_counts 的 time: 计数)。
    """
    bp = PuzzleBlueprint(mechanism_family="hidden_function",
                         solution_shape="hidden_function_explains_behavior",
                         domain="maritime", relation="stranger",
                         emotion_mode="neutral", time_shape="instant")
    for ts in ("instant", "habitual", "years_long", "single_day"):
        sp = good_spec()
        sp.blueprint = bp
        sp.signature = PuzzleSignature(
            mechanism_family="hidden_function",
            solution_shape="hidden_function_explains_behavior",
            domain="maritime", relation="stranger",
            emotion_mode="neutral", time_shape=ts)
        r = validate_blueprint(sp, bp)
        check(f"time_shape={ts} 不该被拒", r.ok, r.errors)

    # 但其它维度**仍然**是硬约束 —— 降级只限 time_shape 这一个字段
    sp = good_spec()
    sp.blueprint = bp
    sp.signature = PuzzleSignature(
        mechanism_family="hidden_function",
        solution_shape="hidden_function_explains_behavior",
        domain="maritime", relation="stranger",
        emotion_mode="neutral", time_shape="years_long")
    check("降级没有波及其它维度", validate_blueprint(sp, bp).ok, "误拒")

    # time_shape 仍要被统计到(配额不用它, 但分布要看得见)
    from story.quality import signature_counts
    c = signature_counts([sp.signature])
    check("time_shape 进了统计", c.get("time:years_long") == 1, c)


def test_blueprint_flags_both_directions():
    """P0-7: 4 个静态标记必须**双向**比对。

    早先只查 False->True 一个方向: blueprint.past_trauma=True 而题实际
    False 时不报, 于是这道题被登记成"有创伤", 配额算错。
    """
    for name in ("death", "past_trauma", "long_term_profession",
                 "repeated_ritual"):
        # blueprint 要 True, 题实际 False -> 也该拒
        bp = PuzzleBlueprint(mechanism_family="hidden_function",
                             solution_shape="hidden_function_explains_behavior",
                             domain="maritime", relation="stranger",
                             emotion_mode="neutral", time_shape="habitual",
                             **{name: True})
        sp = good_spec()
        sp.blueprint = bp
        sp.signature = PuzzleSignature(
            mechanism_family="hidden_function",
            solution_shape="hidden_function_explains_behavior",
            domain="maritime", relation="stranger", emotion_mode="neutral",
            time_shape="habitual", **{name: False})
        r = validate_blueprint(sp, bp)
        check(f"{name}: blueprint=True 题=False -> 拒", not r.ok, r.errors)
        # 反向: blueprint 要 False, 题报 True -> 也要拒
        bp2 = PuzzleBlueprint(mechanism_family="hidden_function",
                              solution_shape="hidden_function_explains_behavior",
                              domain="maritime", relation="stranger",
                              emotion_mode="neutral", time_shape="habitual")
        sp2 = good_spec()
        sp2.blueprint = bp2
        sp2.signature = PuzzleSignature(
            mechanism_family="hidden_function",
            solution_shape="hidden_function_explains_behavior",
            domain="maritime", relation="stranger", emotion_mode="neutral",
            time_shape="habitual", **{name: True})
        r2 = validate_blueprint(sp2, bp2)
        check(f"{name}: blueprint=False 题=True -> 拒", not r2.ok, r2.errors)


def test_legacy_spec_skips_blueprint_comparison():
    """P0-7: 老 spec(没有 signature)不该被逐项比对判死。"""
    legacy = PuzzleSpec(puzzle="老谜面。为什么?", answer="老谜底。")
    bp = PuzzleBlueprint(mechanism_family="hidden_function",
                         solution_shape="hidden_function_explains_behavior",
                         domain="commerce")
    r = validate_blueprint(legacy, bp)
    check("老 spec 不做逐项比对", r.ok, r.errors)


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


def test_hint_focus_picks_untouched_required_atom():
    """Q6(方案 §33): 提示方向 = required atom -> 其 facts -> 未 touched -> hintable。"""
    from story.quality import hint_focus
    s = good_spec()
    f = hint_focus(s, set())
    check("挑出了一个方向", bool(f["focus_atom"]), f)
    check("方向来自 required atom",
          f["focus_atom"] in [a.text for a in s.required_atoms()],
          f["focus_atom"])
    check("给出了未 touched 的 fact id", bool(f["focus_facts"]), f)
    check("给出的是 hintable 的 fact",
          all(s.fact_by_id()[fid].hintable for fid in f["focus_facts"]), f)


def test_hint_focus_avoids_touched_and_unhintable():
    """Q6: 已 touched 的、以及 hintable=False 的 fact 都不能当提示方向。

    `touched` 是"玩家问过这个方向"—— 再提示就是浪费一条提示额度。
    `hintable=False` 的是排除项/元信息, 提示它等于把观众往反方向带。
    """
    from story.quality import hint_focus
    s = good_spec()
    # f3 是 hintable=False, f1 是 a1 的唯一依赖
    check("f3 确实不可提示", not s.fact_by_id()["f3"].hintable)
    # a1 只依赖 f1 -> 把 f1 标为 touched 后, 焦点该转向 a2
    f = hint_focus(s, {"f1"})
    check("避开已 touched 的 fact", "f1" not in f["focus_facts"], f)
    check("转向了另一条 atom", "f2" in f["focus_facts"], f)
    f2 = hint_focus(s, {"f1", "f2"})
    check("全 touched 时仍给得出方向", bool(f2["focus_atom"]), f2)


def test_hint_focus_forbids_core_hidden():
    """Q6: core hidden fact 必须进"禁止说出"表 —— 说出来就是泄底。"""
    from story.quality import hint_focus
    s = good_spec()
    f = hint_focus(s, set())
    core = [x.text for x in s.core_hidden_facts()]
    check("core hidden 都在禁止表里",
          all(c in f["forbidden_core_terms"] for c in core),
          f["forbidden_core_terms"])
    check("禁止表没有重复项",
          len(f["forbidden_core_terms"]) == len(set(f["forbidden_core_terms"])),
          f["forbidden_core_terms"])
    check("touched 集合如实带出", f["known_or_touched"] == [], f)



def test_to_archive_roundtrip_is_lossless():
    """Q8a: 生成溯源必须能存下来、读回来。

    早先 `to_dict` 只写内容字段, 把 `usage/model/error/metrics` 全漏了,
    于是任何"存下来再读回来"的路径(题池/复盘/离线分析)都会静默拿到
    空 metrics —— 而 `metrics` 是 generation_attempts / review_calls /
    rewrite_count / review_decision / review_latency_ms_total 唯一的家。

    这个测试**故意赋非默认值**: 如果只比 `to_dict()` 自己, 漏掉的键在
    两边都不出现, 断言照样过(现有 test_spec_roundtrip 就是这样)。
    所以这里比的是**源对象 vs 读回来的对象**。
    """
    src = good_spec(
        usage={"input_tokens": 1234, "output_tokens": 567},
        model="claude-sonnet-4-5",
        metrics={"generation_attempts": 3, "review_calls": 2,
                 "rewrite_count": 1, "review_decision": "fix",
                 "review_issues": ["人称"], "review_latency_ms_total": 4210,
                 "generation_latency_ms": 18750, "ok": True},
    )
    back = PuzzleSpec.from_dict(src.to_archive())

    check("metrics 活下来了", back.metrics == src.metrics,
          f"got {back.metrics!r}")
    check("usage 活下来了", back.usage == src.usage, f"got {back.usage!r}")
    check("model 活下来了", back.model == src.model, f"got {back.model!r}")
    check("error 活下来了", back.error == src.error, f"got {back.error!r}")
    # to_dict 是 to_archive 的基础: 两者都不能漏
    check("to_dict 也带 metrics", src.to_dict().get("metrics") == src.metrics,
          src.to_dict().get("metrics"))
    # 内容字段不能被顺手弄坏
    check("内容字段无损", back.to_dict() == src.to_dict())
    check("provenance 无损",
          all(back.to_dict().get(k) == src.to_dict().get(k)
              for k in ("usage", "model", "error", "metrics")))


def test_old_records_without_provenance_still_load():
    """Q8a: 现存 archive 绝大多数是这四把键出现**之前**写的。

    实测 data/puzzle.jsonl 105 条里 103 条没有 spec_version。
    它们必须照样能读 —— 读不出就退化成默认值, **绝不能抛**。
    老记录 metrics 为空是**正确**语义("那时候还没记"), 不是损坏。
    """
    old = PuzzleSpec.from_dict({"puzzle": "老谜面。为什么?", "answer": "老谜底。",
                                "facts": [], "solve_atoms": [], "fair_clues": []})
    check("老记录能读", old.puzzle == "老谜面。为什么?", old.puzzle)
    check("老记录 metrics 退化为空", old.metrics == {}, old.metrics)
    check("老记录 usage 退化为 None", old.usage is None, old.usage)
    check("老记录 model 退化为 None", old.model is None, old.model)
    # 脏类型不能炸(手改过的文件/半截写入)
    dirty = PuzzleSpec.from_dict({"puzzle": "x", "answer": "y",
                                  "metrics": "not-a-dict",
                                  "usage": ["also", "wrong"]})
    check("脏 metrics 被忽略而不是抛", dirty.metrics == {}, dirty.metrics)
    check("脏 usage 被忽略而不是抛", dirty.usage is None, dirty.usage)


def main():
    tests = [
        test_spec_roundtrip,
        # ---- Q8a ----
        test_to_archive_roundtrip_is_lossless,
        test_old_records_without_provenance_still_load,
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
        # ---- Step 01: reveal_mode / procedural_rule_dependency ----
        test_reveal_mode_enum_is_reveal_only,
        test_blueprint_reveal_mode_default_and_roundtrip,
        test_blueprint_has_no_procedural_rule_dependency,
        test_legacy_signature_reveal_mode_is_unknown_not_straight,
        test_signature_reveal_mode_default_is_unknown,
        test_signature_reveal_mode_rejects_bad_value,
        test_describe_does_not_leak_reveal_mode,
        test_legacy_atoms_migrate,
        test_validate_blueprint_flags,
        test_blueprint_mismatch_is_rejected_not_warned,
        test_time_shape_is_not_a_hard_constraint,
        test_hint_focus_picks_untouched_required_atom,
        test_hint_focus_avoids_touched_and_unhintable,
        test_hint_focus_forbids_core_hidden,
        test_blueprint_flags_both_directions,
        test_legacy_spec_skips_blueprint_comparison,
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
