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
    DiscoveryBeat, EMOTION_MODES, FairClue, PuzzleBlueprint, PuzzleFact,
    PuzzleSignature, PuzzleSpec, REVEAL_DEFAULT, REVEAL_MODES, SolveAtom,
    has_closing_question, is_first_person, quote_in_puzzle,
)
from story.quality import (  # noqa: E402
    QUALITY_POLICY_VERSION, Quotas, check_signature, choose_blueprint,
    cross_puzzle_gate, FAMILY_SHAPES, check_tables, is_structurally_duplicate,
    signature_counts, signature_of, validate_blueprint, validate_spec, _candidates,
    DARK_TONE_MODES, choose_emotion, _projected_dark_count,
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
                   # good_spec 标的是**当前政策**(quality-v5), 所以它必须
                   # 是一份**完整 v5 spec** —— 否则它自己就违反了 Blocker 2
                   # 的版本硬门(那正是要修的: "v5 标签不能配 legacy 通关语义")。
                   core_answer="他亮灯是为了标出退潮时露出的礁石, 不是给船引路。",
                   completion_fact_ids=["f1", "f2"],
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
                   quality_policy_version=QUALITY_POLICY_VERSION,
                   # quality-v8: 当前政策要求 2~4 个发现阶段。夹具必须
                   # 自己就是一份**合格**的 v8 spec, 否则每个用例都会
                   # 先在"缺 discovery_beats"上失败, 掩盖真正要测的东西。
                   discovery_beats=[
                       DiscoveryBeat(id="b1", text="先注意到灯只在退潮时亮",
                                     fact_ids=["f1"]),
                       DiscoveryBeat(id="b2", text="再想到灯是在标礁石, 不是引路",
                                     fact_ids=["f2"]),
                   ])
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def legacy_spec(**kw) -> PuzzleSpec:
    """一份**真正的 legacy**(quality-v4)题 —— 没有 v5 合同。

    与 `good_spec()` 的区别只有政策版本与合同字段: 它刻意保留旧语义,
    用来验证"老 archive / 老 fixture 仍可读、仍走旧 gate, 但**不能**
    伪装成 v5 库存"。
    """
    s = good_spec(**kw)
    s.quality_policy_version = "quality-v4"
    if "core_answer" not in kw:
        s.core_answer = ""
    if "completion_fact_ids" not in kw:
        s.completion_fact_ids = []
    return s


# ======================================================================
def v5_spec(**kw) -> PuzzleSpec:
    """在 `good_spec()` 之上加一份**合法**的 v5 通关合同。

    合同指向 f1/f2 —— 两条 kind=core, visibility 默认 hidden, 且分别被
    a1/a2 引用。这正好满足 validate_spec 的 v5 五条要求。
    """
    kw.setdefault("core_answer", "他亮灯是为了标出退潮时露出的礁石, 不是引路。")
    kw.setdefault("completion_fact_ids", ["f1", "f2"])
    return good_spec(**kw)


# ======================================================================
def test_v5_completion_contract_roundtrip():
    print("\n[v5-1] 通关合同的序列化全链")
    s = v5_spec()
    d = s.to_dict()
    check("to_dict 带 core_answer", d.get("core_answer") == s.core_answer, d)
    check("to_dict 带 completion_fact_ids",
          d.get("completion_fact_ids") == ["f1", "f2"], d)
    s2 = PuzzleSpec.from_dict(d)
    check("round trip 保持 core_answer", s2.core_answer == s.core_answer, s2)
    check("round trip 保持 completion",
          s2.completion_fact_ids == ["f1", "f2"], s2.completion_fact_ids)
    a = s.to_archive()
    check("archive spec_version=4", a.get("spec_version") == 4, a.get("spec_version"))
    check("archive 带合同", a.get("completion_fact_ids") == ["f1", "f2"], a)
    check("has_completion_contract() 为真", s.has_completion_contract())
    check("completion_facts() 返回真对象",
          [f.id for f in s.completion_facts()] == ["f1", "f2"],
          s.completion_facts())


def test_v5_old_archive_reads_as_no_contract():
    print("\n[v5-2] 老 archive 宽容读 -> 无合同(legacy 路径)")
    # 一把键都没有的 v4 archive
    old = {"id": "p", "puzzle": PUZZLE, "answer": "老谜底",
           "facts": [{"id": "f1", "text": "x", "kind": "core"}],
           "solve_atoms": [], "fair_clues": [], "hints": []}
    s = PuzzleSpec.from_dict(old)
    check("core_answer 空", s.core_answer == "", s.core_answer)
    check("completion 空", s.completion_fact_ids == [], s.completion_fact_ids)
    check("判为无合同(走 legacy)", not s.has_completion_contract())
    # legacy(v4)题继续走旧 gate: 有 cause+mechanism 就通过
    vr = validate_spec(legacy_spec())
    check("legacy 无合同 -> 旧 gate 仍通过", vr.ok, vr.why())
    # 但 **v5 标签 + 空合同** 必须被拒(见 v5-8)
    vr2 = validate_spec(good_spec(completion_fact_ids=[], core_answer=""))
    check("v5 标签 + 空合同 -> 拒", not vr2.ok, vr2.why())


def test_v5_support_cannot_be_completion():
    print("\n[v5-3 / Case E] support/exclusion 不能作为通关要求")
    vr = validate_spec(v5_spec(completion_fact_ids=["f3"]))
    check("support 当合同 -> 拒", not vr.ok, vr.why())
    check("理由点了 kind", "kind" in vr.why(), vr.why())
    vr2 = validate_spec(v5_spec(completion_fact_ids=["f4"]))
    check("exclusion 当合同 -> 拒", not vr2.ok, vr2.why())


def test_v5_completion_capped_at_two():
    print("\n[v5-4 / Case F] 通关合同最多 2 条")
    s = v5_spec()
    # 造第三条 core/hidden fact, 让某条 atom 引用它, **并把它加进合同**。
    # (只往 facts 里塞是不够的 —— 合同是 completion_fact_ids 本身。)
    s.facts.append(PuzzleFact(id="f5", text="第三条核心事实", kind="core",
                              visibility="hidden"))
    s.solve_atoms[0].fact_ids = ["f1", "f5"]
    s.completion_fact_ids = ["f1", "f2", "f5"]
    vr = validate_spec(s)
    check("3 条合同 -> 拒", not vr.ok, vr.why())
    check("理由点了条数", "1~2" in vr.why(), vr.why())


def test_v5_completion_must_exist_and_be_referenced():
    print("\n[v5-5] 合同 id 必须存在, 且被 atom 引用")
    vr = validate_spec(v5_spec(completion_fact_ids=["f1", "f999"]))
    check("不存在的 id -> 拒", not vr.ok, vr.why())
    # 没有任何 atom 引用 f2
    s = v5_spec()
    s.solve_atoms[1].fact_ids = ["f3"]
    vr2 = validate_spec(s)
    check("没有推理抓手 -> 拒", not vr2.ok, vr2.why())
    check("理由点了抓手", "抓手" in vr2.why(), vr2.why())


def test_v5_core_answer_bounds():
    print("\n[v5-6] core_answer 非空 / 限长 / 不换行")
    vr = validate_spec(v5_spec(core_answer=""))
    check("空 core_answer -> 拒", not vr.ok, vr.why())
    vr2 = validate_spec(v5_spec(core_answer="长" * 81))
    check("超 80 字 -> 拒", not vr2.ok, vr2.why())
    vr3 = validate_spec(v5_spec(core_answer="第一行\n第二行"))
    check("换行 -> 拒", not vr3.ok, vr3.why())
    vr4 = validate_spec(v5_spec(core_answer="恰好一句话的核心答案。"))
    check("正常 core_answer -> 过", vr4.ok, vr4.why())


def test_v5_key_role_atom_is_enough():
    print("\n[v5-7] 身份题可以用 key, 不必硬造 cause/mechanism")
    s = v5_spec()
    # 把它改成身份题: 一条 required key atom, 没有 cause/mechanism
    s.solve_atoms = [
        SolveAtom(id="a1", role="key", text="门外女人是父亲的亲生女儿",
                  fact_ids=["f1"]),
    ]
    # 一条 atom 就够 —— 这正是 v5 下限降到 1 的意义
    s.completion_fact_ids = ["f1"]
    s.fair_clues = [FairClue(quote="只在退潮的那几个小时亮", supports_atoms=["a1"])]
    vr = validate_spec(s)
    check("key-only 题通过 v5 校验", vr.ok, vr.why())
    # 但同一条题若**没有**合同, 仍走旧 gate -> 必须被拒
    vr2 = validate_spec(legacy_spec(
        solve_atoms=[SolveAtom(id="a1", role="key", text="x", fact_ids=["f1"])]))
    check("无合同时 key 不顶用(legacy gate 不变)", not vr2.ok, vr2.why())


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
    check("archive 带 spec_version=4", a.get("spec_version") == 4, a.get("spec_version"))
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
    # **legacy 专属**: v5 有合同时不再要求 cause/mechanism, 所以这条
    # 规则只在无合同的 legacy 题上成立 —— 用 legacy_spec 才测得到它。
    s = legacy_spec()
    s.solve_atoms = [SolveAtom(id="a1", role="support", text="x", fact_ids=["f1"]),
                     SolveAtom(id="a2", role="mechanism", text="y", fact_ids=["f2"])]
    r = validate_spec(s)
    check("被拒", not r.ok, r.errors)
    check("指出缺 cause", any("cause" in e for e in r.errors), r.errors)


def test_missing_required_mechanism_rejected():
    print("[validate_spec: 缺 required mechanism -> 拒]")
    s = legacy_spec()   # 同上: 这是 legacy-only 规则
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


def test_describe_exposes_reveal_target():
    """Step 02: `describe()` 现在**应该**带上 reveal_mode 目标。

    历史: Step 01 刻意不暴露它(那时它恒为默认, 印出去是伪目标);
    Step 02 有了 `choose_reveal_mode` 调度器, 这一行才携带真实信息。

    `describe()` 直接进生产 Prompt(`_gen_spec_once` 的 RIDDLE user 消息、
    `_review` 的审稿消息), 所以这条同时是"生产行为变更"的回归护栏。

    但 `procedural_rule_dependency` **仍然不该出现** —— 它是**观察值**,
    不是指令。让生成器看见一个"你必须依赖/不依赖规则"的硬约束, 会让它
    按目标编造 observed 值, 那个字段立刻失去统计意义。
    """
    txt = PuzzleBlueprint(reveal_mode="identity_flip").describe()
    check("describe() 出现 reveal_mode 目标", "reveal_mode" in txt, txt)
    check("describe() 印出的是那个目标值",
          "identity_flip" in txt, txt)
    check("describe() **不**出现 procedural_rule_dependency",
          "procedural_rule_dependency" not in txt, txt)
    # 原有的硬约束字段必须还在(别为了加一行把整段弄坏)
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


def test_s1_recent_pairs_is_shared_key():
    """**S1**: `recent_pairs()` 是判重键的**唯一**来源。

    `is_structurally_duplicate` 与 scheduler 的候选过滤都必须从它取 ——
    各写一份判重键迟早漂移, 而漂移的代价是调度器主动选一个后面必被
    拒的组合(烧掉 3~4 稿配额)。
    """
    print("\n[S1-A] recent_pairs 是共享判重键")
    from story.quality import recent_pairs
    recent = [
        PuzzleSignature(mechanism_family="rule_constraint",
                        solution_shape="social_constraint", domain="workplace"),
        PuzzleSignature(mechanism_family="hidden_function",
                        solution_shape="misunderstood_object", domain="food"),
    ]
    got = recent_pairs(recent, 10)
    check("**取到两个 pair**", got == {("rule_constraint", "social_constraint"),
                                       ("hidden_function", "misunderstood_object")},
          got)
    # 刻意**不含** domain: 换职业不算换题
    check("**判重键不含 domain**",
          all(len(k) == 2 for k in got), got)
    # 与 is_structurally_duplicate 一致
    dup = PuzzleSignature(mechanism_family="rule_constraint",
                          solution_shape="social_constraint", domain="medical")
    check("is_structurally_duplicate 与 recent_pairs 一致",
          bool(is_structurally_duplicate(dup, recent)), True)
    check("空窗口 -> 空集合", recent_pairs([], 10) == set(), recent_pairs([], 10))
    check("窗口截断生效",
          recent_pairs(recent, 1)
          == {("hidden_function", "misunderstood_object")},
          recent_pairs(recent, 1))


def test_s1_scheduler_never_picks_live_regression_pair():
    """**S1-B**: 复现实播 —— recent 已有 rule_constraint/social_constraint。

    实播日志里第 1/2/4 稿全被 cross gate 以"结构等价"拒掉。修好之后
    调度器**绝不该再返回那个 pair**。
    """
    print("\n[S1-B] 实播 regression: 不再选必死的 pair")
    import random
    blocked = ("rule_constraint", "social_constraint")
    recent = [PuzzleSignature(mechanism_family=blocked[0],
                              solution_shape=blocked[1], domain="workplace")]
    hit = 0
    for seed in range(300):
        bp = choose_blueprint(recent, rng=random.Random(seed), quotas=Quotas())
        if (bp.mechanism_family, bp.solution_shape) == blocked:
            hit += 1
    check("**300 个 seed 里一次都没返回那个 pair**", hit == 0, hit)


def test_s1_never_returns_duplicate_while_alternative_exists():
    """**S1-C**: 通用不变量 —— 只要还有合法且不重复的 pair, 就绝不返回重复的。

    这是 S1 的核心契约。用随机窗口扫, 而不是只测那个实播 pair。
    """
    print("\n[S1-C] 存在合法替代时永不返回重复 pair")
    import random
    from story.quality import recent_pairs, _legal_shapes_for, _quota_allows
    viol = 0
    checked = 0
    for seed in range(400):
        rng = random.Random(seed)
        q = Quotas()
        n = rng.randrange(0, 10)
        recent = []
        for _ in range(n):
            fam = rng.choice(list(FAMILY_SHAPES))
            sh = rng.choice(list(FAMILY_SHAPES[fam]))
            recent.append(PuzzleSignature(
                mechanism_family=fam, solution_shape=sh, domain="maritime",
                emotion_mode="tense", relation="stranger",
                time_shape="instant", reveal_mode="straight_explanation"))
        bp = choose_blueprint(recent, rng=random.Random(seed ^ 0x5bf0),
                              quotas=q)
        if not bp.mechanism_family:
            continue
        blocked = recent_pairs(recent, q.window)
        # 枚举空间里是否还存在合法且不重复的 pair?
        alt = False
        for fam in FAMILY_SHAPES:
            for sh in FAMILY_SHAPES[fam]:
                if (fam, sh) in blocked:
                    continue
                for emo in EMOTION_MODES:
                    if (sh in _legal_shapes_for(fam, emo)
                            and _quota_allows(fam, sh, emo, q, recent)):
                        alt = True
                        break
                if alt:
                    break
            if alt:
                break
        checked += 1
        if alt and (bp.mechanism_family, bp.solution_shape) in blocked:
            viol += 1
            if viol <= 3:
                print(f"    VIOLATION seed={seed} "
                      f"{bp.mechanism_family}/{bp.solution_shape}")
    check(f"**扫描 {checked} 个窗口, 零违规**", viol == 0, viol)


def test_s1_fallback_also_avoids_recent_pair():
    """**S1-D**: `_least_recently_seen` 兜底也必须避开 recent pair。

    它早先直接取 `FAMILY_SHAPES[fam][0]` —— 完全可能正好撞上最近的
    pair, 于是**兜底反而稳定地产出必被拒的蓝图**。
    """
    print("\n[S1-D] 兜底路径也避开 recent pair")
    from story.quality import _least_recently_seen, recent_pairs
    q = Quotas()
    # 构造: 所有 family 都在 recent 里出现过, 且第一条 family 的
    # 第一个 shape 正是 recent 里的 pair。
    fam = "rule_constraint"
    first_shape = FAMILY_SHAPES[fam][0]
    recent = [PuzzleSignature(mechanism_family=fam,
                              solution_shape=first_shape, domain="workplace")]
    bp = _least_recently_seen(q, recent)
    check("**没返回被占用的那个 pair**",
          (bp.mechanism_family, bp.solution_shape)
          not in recent_pairs(recent, q.window),
          (bp.mechanism_family, bp.solution_shape))
    check("仍返回一个合法 blueprint", bool(bp.mechanism_family),
          bp.mechanism_family)


def test_s1_fallback_degrades_when_all_blocked():
    """整个空间真被堵死时仍要返回(不能卡死出题链)。"""
    print("\n[S1-E] 全堵死仍返回(不卡死)")
    from story.quality import _least_recently_seen
    q = Quotas()
    # 把每个 family 的**所有** shape 都塞进 recent
    recent = []
    for fam, shapes in FAMILY_SHAPES.items():
        for sh in shapes:
            recent.append(PuzzleSignature(mechanism_family=fam,
                                          solution_shape=sh,
                                          domain="maritime"))
    bp = _least_recently_seen(q, recent)
    check("**仍然返回一个 blueprint(不抛异常)**",
          bool(bp.mechanism_family), bp.mechanism_family)
    check("返回的 shape 属于该 family",
          bp.solution_shape in FAMILY_SHAPES.get(bp.mechanism_family, ()),
          (bp.mechanism_family, bp.solution_shape))


def test_q2_discovery_beat_validation():
    """**Q2-G**: discovery_beats 的确定性硬校验。

    只判**结构**: 条数 / 唯一 / 非空 / 引用存在 / 整组重复 / 通向通关。
    "这两个 beat 语义上是不是重复" 交给 Reviewer —— 代码硬判会误伤。
    """
    print("\n[Q2-G] discovery_beats 硬校验")
    from story.quality import MIN_DISCOVERY_BEATS, MAX_DISCOVERY_BEATS

    def mk(beats):
        s = good_spec()
        s.discovery_beats = beats
        return s

    ok = mk([DiscoveryBeat(id="b1", text="先注意到灯的时机",
                           fact_ids=["f1"]),
             DiscoveryBeat(id="b2", text="再理解灯在标礁石",
                           fact_ids=["f2"])])
    check("合法的 2 条 -> 过", validate_spec(ok).ok,
          validate_spec(ok).errors[:1])
    check("常量区间是 2~4",
          (MIN_DISCOVERY_BEATS, MAX_DISCOVERY_BEATS) == (2, 4),
          (MIN_DISCOVERY_BEATS, MAX_DISCOVERY_BEATS))

    vr = validate_spec(mk([DiscoveryBeat(id="b1", text="x", fact_ids=["f1"])]))
    check("**只有 1 条 -> 拒**", not vr.ok, vr.errors[:1])
    vr = validate_spec(mk([DiscoveryBeat(id=f"b{i}", text=f"t{i}",
                                         fact_ids=["f1"])
                           for i in range(5)]))
    check("**5 条 -> 拒(太多观众会跟丢)**", not vr.ok, vr.errors[:1])
    vr = validate_spec(mk([DiscoveryBeat(id="b1", text="a", fact_ids=["f1"]),
                           DiscoveryBeat(id="b1", text="b", fact_ids=["f2"])]))
    check("**id 重复 -> 拒**", not vr.ok, vr.errors[:1])
    vr = validate_spec(mk([DiscoveryBeat(id="b1", text="", fact_ids=["f1"]),
                           DiscoveryBeat(id="b2", text="b", fact_ids=["f2"])]))
    check("**text 为空 -> 拒**", not vr.ok, vr.errors[:1])
    vr = validate_spec(mk([DiscoveryBeat(id="b1", text="a", fact_ids=["f999"]),
                           DiscoveryBeat(id="b2", text="b", fact_ids=["f2"])]))
    check("**引用不存在的 fact -> 拒**", not vr.ok, vr.errors[:1])
    vr = validate_spec(mk([DiscoveryBeat(id="b1", text="同一句",
                                         fact_ids=["f1"]),
                           DiscoveryBeat(id="b2", text="同一句",
                                         fact_ids=["f2"])]))
    check("**整组文本完全相同 -> 拒**", not vr.ok, vr.errors[:1])
    vr = validate_spec(mk([DiscoveryBeat(id="b1", text="a", fact_ids=["f3"]),
                           DiscoveryBeat(id="b2", text="b", fact_ids=["f4"])]))
    check("**没有任何 beat 通向通关 -> 拒**", not vr.ok, vr.errors[:1])


def test_q2_completion_stays_one_or_two():
    """**Q2-H**: 层次变多了, 但**通关仍然只需 1~2 条**。

    这是整笔的核心产品规则: 题目允许有层次, 通关必须简单。
    """
    print("\n[Q2-H] 通关仍限 1~2 条")
    from story.quality import MAX_COMPLETION_FACTS
    check("MAX_COMPLETION_FACTS 仍是 2", MAX_COMPLETION_FACTS == 2,
          MAX_COMPLETION_FACTS)
    # 3 条 -> 拒(即使 beats 齐全)
    s = good_spec(completion_fact_ids=["f1", "f2", "f3"])
    s.discovery_beats = [
        DiscoveryBeat(id="b1", text="a", fact_ids=["f1"]),
        DiscoveryBeat(id="b2", text="b", fact_ids=["f2"]),
    ]
    vr = validate_spec(s)
    check("**3 条 completion -> 拒**", not vr.ok,
          [e for e in vr.errors if "completion_fact_ids" in e][:1])
    # **1 条 completion + 3 个 beat -> 合法**(证明"通关简单 != 题目简单")
    s2 = good_spec(completion_fact_ids=["f1"])
    s2.discovery_beats = [
        DiscoveryBeat(id="b1", text="第一层: 注意潮水", fact_ids=["f1"]),
        DiscoveryBeat(id="b2", text="第二层: 灯的用途不是引路", fact_ids=["f3"]),
        DiscoveryBeat(id="b3", text="第三层: 行为的真实目的", fact_ids=["f3"]),
    ]
    vr2 = validate_spec(s2)
    check("**1 条 completion + 3 个 beat -> 合法**", vr2.ok, vr2.errors[:2])


def test_q2_legacy_archive_without_beats_still_reads():
    """**Q2-I**: 旧 archive 没有 discovery_beats -> 照常读出(空表), 不报错。

    绝不从 facts/atoms 反推 —— 那等于给旧题编一个它从没声明过的层次。
    """
    print("\n[Q2-I] 旧 archive 无 beats 仍可读")
    old = {"puzzle": "旧题?", "answer": "旧谜底。",
           "quality_policy_version": "quality-v4"}
    sp = PuzzleSpec.from_dict(old)
    check("读出空表", sp.discovery_beats == [], sp.discovery_beats)
    check("旧题**不**被 v8 的 beats 要求卡住",
          validate_spec(sp).ok or all(
              "discovery_beats" not in e for e in validate_spec(sp).errors),
          validate_spec(sp).errors[:2])
    # roundtrip
    d = sp.to_dict()
    check("to_dict 带上(空)discovery_beats", "discovery_beats" in d,
          sorted(d))
    check("from_dict(to_dict(x)) 稳定",
          PuzzleSpec.from_dict(d).discovery_beats == [], "roundtrip 漂移")


def test_q2_dark_tone_target_is_code():
    """**Q2-J**: 诡异基调目标是**代码**, 不只是 prompt 里的一句话。"""
    print("\n[Q2-J] 基调目标代码化")
    from story.config import Config
    c = Config(sim_path="x")
    check("目标带在 config 里",
          hasattr(c, "quality_dark_tone_min")
          and hasattr(c, "quality_dark_tone_max"),
          "缺 quality_dark_tone_* 字段")
    check("区间合理(不低于 50%, 不高于 60%)",
          c.quality_dark_tone_min >= 5 and c.quality_dark_tone_max <= 6,
          (c.quality_dark_tone_min, c.quality_dark_tone_max))
    # eerie / tense 都在 EMOTION_MODES 里(调度器要能数它们)
    check("eerie / tense 是合法情绪档",
          "eerie" in EMOTION_MODES and "tense" in EMOTION_MODES,
          EMOTION_MODES)


def test_c4_dark_tone_rolling_simulation():
    """**C4**: `choose_emotion` 真的把"最近 10 题 5~6 道诡异/紧张"落成代码。

    Q2-J 只检查了 Config 里存在 `quality_dark_tone_min/max` 两个数字 ——
    那是**配置字段存在**, 不是**行为被接通**。生产 `choose_emotion`
    当时完全没读它们, 仍只是 `1/(1+count)` 随机加权。所以那条测试是
    假绿: 把字段删了会红, 把逻辑删了不会。

    这里做**顺序模拟**: 连续做 N 次真实选择, 每次都把选中的情绪喂回
    recent, 然后检查**每一个滚动 10 题窗口**里的 dark 数。

    为什么必须顺序模拟而不是"调一次看结果": 目标是**滚动窗口**上的
    性质, 单次调用看不见窗口。而 warm-up(窗口未满)与满窗两段的规则
    不同, 只有跑起来才能同时覆盖。
    """
    print("\n[C4] 滚动 10 题的诡异/紧张目标带")
    import random as _random
    from story.puzzle import PuzzleSignature

    q = Quotas(dark_tone_min=5, dark_tone_max=6)
    check("Quotas 带上了目标带", (q.dark_tone_min, q.dark_tone_max) == (5, 6),
          (q.dark_tone_min, q.dark_tone_max))
    check("DARK_TONE_MODES 就是 eerie/tense",
          set(DARK_TONE_MODES) == {"eerie", "tense"}, DARK_TONE_MODES)

    def sig(emo):
        return PuzzleSignature(mechanism_family="information_gap",
                               solution_shape="information_advantage",
                               domain="daily", emotion_mode=emo)

    # ---- ① projected 计数: "选了 dark / 非 dark 窗口会长成什么样" ----
    # 语义是 剩下的 9 道里的 dark 数 + (选的是不是 dark)。
    #
    # 满窗 10 道、6 道 dark、且最老是 dark:
    #   选 dark   -> 挤掉最老那道 dark -> 剩 5 道 + 1 = 6
    #   选非 dark -> 挤掉最老那道 dark -> 剩 5 道 + 0 = 5
    # 这是"进一出一、两侧都还在带内"的情形。
    full = [sig("eerie") if i < 6 else sig("warm") for i in range(10)]
    check("满窗 6 dark 且最老是 dark -> 选 dark 6, 选非 dark 5",
          (_projected_dark_count(full, q, True),
           _projected_dark_count(full, q, False)) == (6, 5),
          (_projected_dark_count(full, q, True),
           _projected_dark_count(full, q, False)))
    # 最老是 warm: 挤掉 warm -> 剩 9 道里 6 道 dark -> 选 dark 是 7(超上限)
    full2 = [sig("warm")] + [sig("eerie") if i < 6 else sig("warm")
                             for i in range(9)]
    check("满窗 6 dark 但最老是 warm -> 选 dark 会到 7(超上限)",
          _projected_dark_count(full2, q, True) == 7,
          _projected_dark_count(full2, q, True))

    # ---- ② 下限强制: 窗口满且够不到下限 -> 只能出 dark ----
    low = [sig("warm") if i < 6 else sig("absurd") for i in range(10)]
    check("低 dark 窗口: 选 dark 是 1, 选非 dark 是 0",
          (_projected_dark_count(low, q, True),
           _projected_dark_count(low, q, False)) == (1, 0),
          (_projected_dark_count(low, q, True),
           _projected_dark_count(low, q, False)))
    got = {choose_emotion(low, _random.Random(s), q) for s in range(40)}
    check("**够不到下限 -> 强制 eerie/tense(往目标走)**",
          got and got <= set(DARK_TONE_MODES), sorted(got))

    # ---- ③ 上限强制: window 满且 projected >= 6 -> 只能出非 dark ----
    got2 = {choose_emotion(full2, _random.Random(s), q) for s in range(40)}
    check("**projected>=max -> 强制非 dark**",
          got2 and not (got2 & set(DARK_TONE_MODES)), sorted(got2))

    # ---- ④ 带内 -> 不再硬凑, 但**两种选择都必须仍在带内** ----
    #
    # ⚠️ 这里刻意选 6 道 dark 的满窗(最老是 dark):
    #   选 dark   -> 挤掉最老那道 dark -> 剩 5 + 1 = 6   (在带内)
    #   选非 dark -> 剩 5 + 0 = 5                        (也在带内)
    # 只有这种"两侧都安全"的窗口才该放开自由选择。
    #
    # 早先这里用的是 5 道 dark 的窗口, 断言"两侧都可能出现" —— 那是
    # **错的**: 5 道且最老是 dark 时, 选非 dark 会让窗口掉到 4(跌破
    # 下限), 所以必须强制 dark。实测时正是这个"放行"让窗口锁死在 4。
    mid = [sig("eerie") if i < 6 else sig("warm") for i in range(10)]
    check("带内窗口: 选 dark 是 6, 选非 dark 是 5(两侧都安全)",
          (_projected_dark_count(mid, q, True),
           _projected_dark_count(mid, q, False)) == (6, 5),
          (_projected_dark_count(mid, q, True),
           _projected_dark_count(mid, q, False)))
    got3 = {choose_emotion(mid, _random.Random(s), q) for s in range(60)}
    check("两侧都安全时 dark 与非 dark 都可能出现(不硬凑)",
          bool(got3 & set(DARK_TONE_MODES)) and bool(got3 - set(DARK_TONE_MODES)),
          sorted(got3))

    # ---- ④b 5 道 dark 的满窗 -> 只能 dark(否则跌破下限) ----
    tight = [sig("eerie") if i < 5 else sig("warm") for i in range(10)]
    check("5 dark 满窗: 选非 dark 会掉到 4",
          _projected_dark_count(tight, q, False) == 4,
          _projected_dark_count(tight, q, False))
    got3b = {choose_emotion(tight, _random.Random(s), q) for s in range(40)}
    check("**5 dark 满窗 -> 强制 dark(不许跌破 5)**",
          got3b and got3b <= set(DARK_TONE_MODES), sorted(got3b))

    # ---- ④c 死区: 两侧都够不到下限 -> 往目标方向走(dark) ----
    # 10 道里 0 道 dark: 选 dark 只有 1, 选非 dark 是 0, 都 < 5。
    # 此时若退回"自由选", 窗口会随机漂移、永远爬不回带内。
    dead = [sig("warm")] * 10
    got3c = {choose_emotion(dead, _random.Random(s), q) for s in range(40)}
    check("**死区 -> 仍优先 dark(不放弃收敛)**",
          got3c and got3c <= set(DARK_TONE_MODES), sorted(got3c))

    # ---- ⑤ 顺序模拟: warm-up 之后每个滚动窗口都落在 5~6 ----
    for seed in (1, 7, 42, 2026):
        rng = _random.Random(seed)
        recent: list = []
        worst = []
        for _step in range(80):
            emo = choose_emotion(recent, rng, q)
            recent.append(sig(emo))
            if len(recent) >= 10:
                win = recent[-10:]
                n = sum(1 for s in win if s.emotion_mode in DARK_TONE_MODES)
                worst.append(n)
        check(f"seed={seed}: 每个滚动 10 题窗口的 dark 数都是 5 或 6",
              all(n in (5, 6) for n in worst),
              sorted(set(worst)))

    # ---- ⑥ 关掉目标带 -> 与旧行为一致(不会再强制) ----
    off = Quotas()                      # 默认 0/0 = 关闭
    check("默认 Quotas 目标带是关的",
          (off.dark_tone_min, off.dark_tone_max) == (0, 0),
          (off.dark_tone_min, off.dark_tone_max))
    low2 = [sig("warm") if i < 6 else sig("absurd") for i in range(10)]
    got4 = {choose_emotion(low2, _random.Random(s), off) for s in range(40)}
    check("关掉后不再强制 dark(可能出现非 dark)",
          bool(got4 - set(DARK_TONE_MODES)), sorted(got4))


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


# ======================================================================
# Step 02 — reveal / tone scheduler + rolling quota
# ======================================================================
def _sig(**kw):
    """造一个最近窗口里的 observed signature。"""
    d = {"mechanism_family": "hidden_function",
         "solution_shape": "hidden_function_explains_behavior",
         "domain": "maritime", "relation": "stranger",
         "emotion_mode": "neutral", "time_shape": "instant"}
    d.update(kw)
    return PuzzleSignature(**d)


def test_s02_neutral_bias_removed():
    """Step 02 核心: `_candidates` 不再把每一道题的情绪钉死成 neutral。

    修之前 `_shape_flags(shape, "neutral")` 写死了情绪 —— 配额再准也没用,
    因为**别的取值从来没被选过**。这条直接验候选集合里存在非 neutral。
    """
    print("\n[S02-1] 去掉『永远 neutral』的固定偏置")
    from story.quality import _candidates
    import random as _r
    cands = _candidates(Quotas(), [])
    emos = {bp.emotion_mode for bp, _ in cands}
    check("候选里不止 neutral", len(emos) > 1, emos)
    check("候选里确实有非 neutral",
          any(e != "neutral" for e in emos), emos)
    # ---- 真正的**分布**测试(光看候选集合不够) ----
    #
    # ⚠️ 关键陷阱: 有些情绪是**形状钉死**的(`past_trauma_...` 必然是
    # grief, 见 SHAPE_FLAGS)。就算"自由情绪选择"被整体固定回 neutral,
    # 那些被钉死的仍会出现 —— 于是"出现了 >1 种情绪"依然成立, 测试
    # **假绿**。所以这里必须只看**自由选择**那部分的分布。
    from story.quality import SHAPE_FLAGS
    pinned_emos = {v["emotion_mode"] for v in SHAPE_FLAGS.values()
                   if "emotion_mode" in v}
    free = []
    for s in range(120):
        bp = choose_blueprint([], _r.Random(s))
        if bp.solution_shape in SHAPE_FLAGS and                 "emotion_mode" in SHAPE_FLAGS[bp.solution_shape]:
            continue            # 形状钉死, 不算自由选择的功劳
        free.append(bp.emotion_mode)
    uniq = set(free)
    check("自由选择的情绪不止一种(固定偏置会在这里挂)",
          len(uniq) > 1, uniq)
    check("自由选择里 neutral 不占满",
          free.count("neutral") < len(free), free.count("neutral"))
    check("自由选择样本量足够", len(free) > 10, len(free))
    check("被钉死的情绪确实是 grief", "grief" in pinned_emos, pinned_emos)
    # 形状钉死的情绪仍然优先(结构性, 不该被自由选择覆盖)
    pinned = [bp for bp, _ in cands
              if bp.solution_shape == "past_trauma_explains_current_ritual"]
    check("被形状钉死的仍是 grief",
          all(bp.emotion_mode == "grief" for bp in pinned),
          {bp.emotion_mode for bp in pinned})


def test_s02_neutral_quota_limits_distribution():
    """neutral 上限 3/10 —— 大量采样后不能垄断。"""
    print("\n[S02-2] neutral 情绪受 rolling quota 限制")
    q = Quotas()
    e = PuzzleSignature(emotion_mode="neutral")
    recent = [e] * 3
    bad = check_signature(PuzzleSignature(emotion_mode="neutral"), recent, q)
    check("已有 3 道 neutral -> 第 4 道被拒", bad != [], bad)
    check("理由提到中性", any("中性" in x for x in bad), bad)
    # 非 neutral 不受这条限制
    ok = check_signature(PuzzleSignature(emotion_mode="warm"), recent, q)
    check("warm 不受 neutral 配额影响", ok == [], ok)


def test_s02_straight_explanation_quota():
    """straight_explanation <= 2/10(它是"不翻转"的默认态, 单独设限)。"""
    print("\n[S02-3] straight_explanation 上限 2/10")
    q = Quotas()
    recent = [_sig(reveal_mode="straight_explanation")] * 2
    bad = check_signature(_sig(reveal_mode="straight_explanation"), recent, q)
    check("已有 2 道 -> 第 3 道被拒", bad != [], bad)
    check("理由提到正面解释",
          any("正面解释" in x for x in bad), bad)
    # 换个有翻转的结构就不受这条限制。
    # ⚠️ 对照组必须**同时**换掉 mechanism/shape: `_sig()` 的默认是
    # hidden_function, 10 道同 mechanism 会撞上 another 上限, 那样就算
    # straight 配额没问题也会红 —— 是测试自己造出来的假阳性。
    ok = check_signature(
        _sig(reveal_mode="identity_flip", mechanism_family="object_misuse",
             solution_shape="misunderstood_object"), [], q)
    check("有翻转的结构不受 straight 配额影响", ok == [], ok)


def test_s02_same_reveal_mode_quota():
    """同一 reveal_mode <= 2/10。"""
    print("\n[S02-4] 同一 reveal_mode 上限 2/10")
    q = Quotas()
    recent = [_sig(reveal_mode="identity_flip")] * 2
    bad = check_signature(_sig(reveal_mode="identity_flip"), recent, q)
    check("identity_flip 出现 2 次 -> 第 3 次被拒", bad != [], bad)
    check("理由提到该结构", any("identity_flip" in x for x in bad), bad)
    ok = check_signature(
        _sig(reveal_mode="goal_flip", mechanism_family="object_misuse",
             solution_shape="misunderstood_object"), [], q)
    check("另一个结构不受影响", ok == [], ok)


def test_s02_procedural_rule_dependency_quota():
    """Step 02: procedural_rule_dependency <= 2/10(observed 口径)。"""
    print("\n[S02-5] 规则依赖上限 2/10")
    q = Quotas()
    recent = [_sig(procedural_rule_dependency=True)] * 2
    bad = check_signature(_sig(procedural_rule_dependency=True), recent, q)
    check("已有 2 道 -> 第 3 道被拒", bad != [], bad)
    check("理由提到制度性设定",
          any("制度性设定" in x for x in bad), bad)
    ok = check_signature(
        _sig(procedural_rule_dependency=False, mechanism_family="object_misuse",
             solution_shape="misunderstood_object"), [], q)
    check("不依赖规则的题不受限制", ok == [], ok)


def test_s02_unknown_reveal_is_not_counted_as_straight():
    """`reveal_mode == ""` 是"没观察过", **不**计入 straight 桶。

    否则老题(没有这个字段)会被一律读成"普通解释", 历史统计立刻失真,
    而且 straight 配额会被历史数据无端占满。
    """
    print("\n[S02-6] 未知 reveal 不算 straight")
    recent = [_sig(reveal_mode="")] * 5
    c = signature_counts(recent)
    check("straight 桶为 0", c.get("reveal:straight_explanation", 0) == 0, c)
    check("记进 __unknown_reveal__", c.get("__unknown_reveal__") == 5, c)
    q = Quotas()
    # 用空窗口验"未知值不占用 straight 桶" —— 拿上面那 5 条当 recent 会
    # 顺带撞上 mechanism 配额, 那样红的是别的原因, 测不到本意。
    ok = check_signature(_sig(reveal_mode="straight_explanation"), [], q)
    check("因此 straight 仍可用", ok == [], ok)


def test_s02_reveal_preference_puts_straight_last():
    """`straight_explanation` 必须在偏好序**最后** —— 允许但不当默认。"""
    print("\n[S02-7] straight 永远排在偏好序最后")
    from story.quality import REVEAL_PREFERENCE
    check("顺序里含全部 8 种",
          set(REVEAL_PREFERENCE) == set(REVEAL_MODES),
          (set(REVEAL_PREFERENCE), set(REVEAL_MODES)))
    check("straight 在最后",
          REVEAL_PREFERENCE[-1] == "straight_explanation",
          REVEAL_PREFERENCE[-1])


def test_s02_choose_reveal_mode_avoids_exhausted():
    """`choose_reveal_mode` 不选已超配额的; 且有翻转型优先。"""
    print("\n[S02-8] reveal 选择器避开超配额的")
    import random
    from story.quality import choose_reveal_mode
    q = Quotas()
    # 把 7 种翻转型全部填满到超配额, 只留 straight。
    # ⚠️ 必须让**每一种都落在 window 内**: `signature_counts` 只看最近
    # window 条, 7 种 × 2 条 = 14 > window(10), 前面的会被挤出窗口 ——
    # 那样 identity_flip 等会"看起来没出现过", 选择器正确地返回了它,
    # 而测试却以为它该被挡住。用一个小 window 的 Quotas 精确表达意图。
    q2 = Quotas(window=14)
    recent = []
    for m in REVEAL_MODES:
        if m == "straight_explanation":
            continue
        recent += [_sig(reveal_mode=m)] * q2.same_reveal_mode
    check("前置: 7 种翻转型都在窗口内",
          all(signature_counts(recent, q2.window)[f"reveal:{m}"] == 2
              for m in REVEAL_MODES if m != "straight_explanation"))
    got = choose_reveal_mode(recent, random.Random(3), q2)
    check("翻转型全满时只能给 straight", got == "straight_explanation", got)
    # 反过来: straight 满了 -> 必须给某个翻转型
    recent2 = [_sig(reveal_mode="straight_explanation")] * q.straight_explanation
    got2 = choose_reveal_mode(recent2, random.Random(3), q)
    check("straight 满时给翻转型", got2 != "straight_explanation", got2)
    # 空窗口: 多次采样应覆盖多个翻转型(不是永远同一个)
    picks = {choose_reveal_mode([], random.Random(s), q) for s in range(30)}
    check("多 seed 覆盖多个结构", len(picks) > 1, picks)
    check("多 seed 里 straight 不占多数",
          sum(1 for s in range(30)
              if choose_reveal_mode([], random.Random(s), q)
              == "straight_explanation") < 15)


def test_s02_blueprint_carries_reveal_target():
    """调度出来的 blueprint 必须带上 reveal 目标值。"""
    print("\n[S02-9] blueprint 带上 reveal 目标")
    import random
    bp = choose_blueprint([], random.Random(5))
    check("reveal_mode 非空", bool(bp.reveal_mode), bp.reveal_mode)
    check("reveal_mode 在枚举内", bp.reveal_mode in REVEAL_MODES, bp.reveal_mode)
    check("describe() 印出了该目标",
          bp.reveal_mode in bp.describe(), bp.describe())
    # ---- **分布**测试: 目标不能被固定成 straight_explanation ----
    # 只验"非空 + 枚举内"是抓不住"固定成 straight"的 —— straight 本身
    # 合法。要跑一串调度看分布。
    targets = [choose_blueprint([], random.Random(s)).reveal_mode
               for s in range(120)]
    uniq = set(targets)
    check("调度 120 次出现 >1 种 reveal 目标", len(uniq) > 1, uniq)
    check("straight 不占满",
          targets.count("straight_explanation") < len(targets),
          targets.count("straight_explanation"))
    check("有翻转的目标出现过",
          any(t != "straight_explanation" for t in targets), uniq)


def test_closeout_hierarchy_family_probability_not_inflated_by_width():
    """Blocker 3 核心: family 概率**不能**被它的展开宽度放大。

    ## 这条测试怎么才算真的有效

    我先写了"宽窄两个 family 占比接近"的版本, 结果**抓不住**真正的
    宽度偏见 mutation —— 因为 `choose_family_shape` 每个 family 只挑一个
    shape, shape 数量本身不直接乘彩票。真正会放大权重的是**笛卡尔积展开
    宽度**(family 能展开出多少 emotion×domain×relation 组合)。

    所以这里直接测**相关性**: 用 `_candidates()`(它就是那个笛卡尔积)
    量出每个 family 的展开宽度, 再看实际调度占比。分层实现下两者
    **不应正相关** —— 事实上最窄的两个 family(time_reinterpretation /
    emotional_motive, 因为它们含被钉死成 grief 的 shape)占比最高, 正好
    与"按宽度计权"相反。

    修之前(大笛卡尔积 + 每 candidate 计权)它们会被系统性饿死。
    """
    print("\n[CO-5] family 概率不被展开宽度放大")
    import random
    from story.quality import FAMILY_SHAPES, family_headroom
    # 空窗口 -> 所有 family 权重相同
    w = family_headroom(Quotas(), [])
    check("空窗口下各 family 权重相等",
          len(set(round(v, 9) for v in w.values())) == 1, w)

    # 用笛卡尔积宽度作为"老实现会给的权重"的代理
    width = {}
    for bp, _sig in _candidates(Quotas(), []):
        width[bp.mechanism_family] = width.get(bp.mechanism_family, 0) + 1

    N = 600
    picks = {}
    for s_ in range(N):
        fam = choose_blueprint([], random.Random(s_)).mechanism_family
        picks[fam] = picks.get(fam, 0) + 1

    # 最窄 / 最宽的 family
    narrow = min(width, key=lambda f: width[f])
    widest = max(width, key=lambda f: width[f])
    check("存在宽度差异(否则这条测试无意义)",
          width[narrow] < width[widest], (narrow, widest))

    exp = N / len(FAMILY_SHAPES)
    # 最窄的那个**不能**被饿死 —— 老实现下它按宽度只该拿 ~5% 的票,
    # 而均匀应当 ~8.3%。60% 的下限给了充足的采样余量。
    check(f"最窄的 {narrow} 占比不低于均匀的 60%",
          picks.get(narrow, 0) >= exp * 0.6,
          f"{picks.get(narrow, 0)} vs expect~{exp:.0f}")
    # 最宽的不能碾压
    check(f"最宽的 {widest} 占比不超过均匀的 2 倍",
          picks.get(widest, 0) <= exp * 2.0,
          f"{picks.get(widest, 0)} vs expect~{exp:.0f}")
    # 关键: 宽度与占比**不能**正相关。
    # 注意别断言"最窄 >= 最宽" —— 两者都在均匀附近, 比较的是采样噪声,
    # 那种断言会随 seed 抖动(实测 51 vs 59)。有意义的界是**比值**:
    # 按宽度计权时最宽/最窄 ≈ 2592/1458 ≈ 1.78, 分层后应接近 1。
    ratio = picks.get(widest, 0) / max(1, picks.get(narrow, 0))
    check("最宽/最窄 的占比比接近 1(宽度没泄漏成概率)",
          ratio < 1.5, f"ratio={ratio:.2f} "
                       f"(widest={widest}:{picks.get(widest, 0)}, "
                       f"narrow={narrow}:{picks.get(narrow, 0)})")


def test_closeout_hierarchy_emotion_picked_before_shape():
    """Blocker 3: emotion 是**独立一层**, 不是被 shape 顺带的。

    选定 grief 时才允许进入被钉死成 grief 的 shape; 选别的情绪时那些
    shape 就不是合法的。这条验两个方向。
    """
    print("\n[CO-6] emotion 层与 shape 层相容性")
    from story.quality import (choose_family_shape, shape_is_compatible_with_emotion,
                               _legal_shapes_for)
    pinned_shape = "past_trauma_explains_current_ritual"
    check("该 shape 被钉死成 grief",
          shape_is_compatible_with_emotion(pinned_shape, "grief") is True)
    check("warm 与该 shape 不相容",
          shape_is_compatible_with_emotion(pinned_shape, "warm") is False)
    # 选定 warm 时, time_reinterpretation 不该有合法 shape(它只有
    # past_trauma... 这个被钉死的 + time_reinterpretation)
    warm_shapes = _legal_shapes_for("time_reinterpretation", "warm")
    check("warm 下 time_reinterpretation 无合法 shape",
          pinned_shape not in warm_shapes, warm_shapes)
    # 选定 grief 时可以进
    grief_shapes = _legal_shapes_for("time_reinterpretation", "grief")
    check("grief 下该 shape 合法", pinned_shape in grief_shapes, grief_shapes)
    # 调度出的 blueprint 必须自洽
    import random
    for s in range(60):
        bp = choose_blueprint([], random.Random(s))
        check_ok = shape_is_compatible_with_emotion(bp.solution_shape,
                                                    bp.emotion_mode)
        if not check_ok:
            check(f"seed={s} 调度结果 emotion/shape 不自洽",
                  False, (bp.mechanism_family, bp.solution_shape,
                          bp.emotion_mode))
            break
    else:
        check("60 次调度的 emotion/shape 全部相容", True)


def test_closeout_choose_reveal_mode_picks_by_deficit():
    """Blocker 3: reveal 目标要按**缺口**选, 不是"没到上限就等权随机"。

    `identity_flip` 已有 1 次而 `goal_flip` 0 次时, 应显著偏向
    `goal_flip` —— 早先实现只看"是否超上限", 两者都未超时不会优先补缺口。
    """
    print("\n[CO-7] reveal 目标按缺口选")
    import random
    from story.quality import choose_reveal_mode
    recent = [_sig(reveal_mode="identity_flip")]
    picks = [choose_reveal_mode(recent, random.Random(s)) for s in range(200)]
    n_id = picks.count("identity_flip")
    n_goal = picks.count("goal_flip")
    check("goal_flip(缺口 0 次) 比 identity_flip(已有 1 次) 更常被选",
          n_goal > n_id, f"goal={n_goal} identity={n_id}")
    check("两者都仍会出现(不是硬禁)",
          n_goal > 0 and n_id > 0, f"goal={n_goal} identity={n_id}")


def test_closeout_reveal_adherence_gate():
    """Blocker 4: target != observed -> **拒绝**(不是只记录)。

    冻结语义: target 是调度意图, observed 是 Reviewer 的事实观察,
    quota 按 observed 统计。但 target != observed 意味着**这稿没执行
    调度目标** —— 那是调度落空, 必须拒, 否则调度器形同虚设。
    """
    print("\n[CO-9] reveal adherence 闸门")
    from story.quality import validate_reveal_adherence
    s = good_spec()
    s.blueprint_specified = True
    s.blueprint.reveal_mode = "identity_flip"
    s.signature.reveal_mode = "identity_flip"
    check("一致 -> 通过", validate_reveal_adherence(s, s.blueprint) == [])
    s.signature.reveal_mode = "goal_flip"
    bad = validate_reveal_adherence(s, s.blueprint)
    check("不一致 -> 拒绝", bad != [], bad)
    check("理由里两个值都在",
          "identity_flip" in bad[0] and "goal_flip" in bad[0], bad)
    # 没观察值 -> 也拒(不能当成"一致")
    s.signature.reveal_mode = ""
    bad2 = validate_reveal_adherence(s, s.blueprint)
    check("缺 observed -> 拒绝", bad2 != [], bad2)


def test_closeout_adherence_skipped_without_assigned_blueprint():
    """自由生成 / 未分配 blueprint 的题**不**做 adherence 比对。

    为什么必须这样: `PuzzleBlueprint.reveal_mode` 的 dataclass 默认值是
    `straight_explanation`(那是"代码没指定时的默认方向")。若不看
    `blueprint_specified` 就比, **每一道自由生成的题都会被要求写成
    普通解释** —— 这正是 Batch A closeout 里真实踩到的坑。
    """
    print("\n[CO-10] 未分配 blueprint 时不比对")
    from story.quality import validate_reveal_adherence
    s = good_spec()
    s.blueprint_specified = False
    s.blueprint.reveal_mode = "straight_explanation"    # dataclass 默认值
    s.signature.reveal_mode = "identity_flip"           # 实际写成别的
    check("未分配 -> 不判(不拒绝)", validate_reveal_adherence(s, s.blueprint) == [])
    # 一旦标记为已分配, 同样的数据就会被拒
    s.blueprint_specified = True
    check("已分配 -> 同样的数据被拒",
          validate_reveal_adherence(s, s.blueprint) != [])


def test_closeout_same_reveal_mode_covers_straight():
    """Blocker 3 cleanup: `straight_explanation` 也要受 `same_reveal_mode`。

    正确语义: **任何** reveal_mode 都受 same_reveal_mode; straight 额外
    还受 straight_explanation —— 实际上限是两者较严的那个。
    早先 straight 只查 straight 配额, 于是 `same_reveal_mode=1,
    straight_explanation=2` 时 straight 仍能出现两次, 违反"同一结构上限 1"。
    """
    print("\n[CO-8] same_reveal_mode 也覆盖 straight")
    q = Quotas(same_reveal_mode=1, straight_explanation=2)
    recent = [_sig(reveal_mode="straight_explanation")]
    bad = check_signature(_sig(reveal_mode="straight_explanation"), recent, q)
    check("same_reveal_mode=1 时 straight 也不能再来一次", bad != [], bad)
    # straight_explanation 更严时同样生效
    q2 = Quotas(same_reveal_mode=5, straight_explanation=1)
    bad2 = check_signature(_sig(reveal_mode="straight_explanation"), recent, q2)
    check("straight_explanation=1 时也不能再来一次", bad2 != [], bad2)
    # 有翻转的结构不受 straight 专属配额影响
    ok = check_signature(
        _sig(reveal_mode="goal_flip", mechanism_family="object_misuse",
             solution_shape="misunderstood_object"), [], Quotas())
    check("翻转型不被 straight 配额误伤", ok == [], ok)


def test_s02_quota_responsibility_stays_in_code():
    """Step 02 硬边界: 配额判断在**代码层**, 不在 Reviewer。

    Reviewer 看不到完整的最近题窗口, 所以"最近是否超配额"这句话不能由它
    来说。这条从**调用关系**上验: `cross_puzzle_gate` 是唯一决定
    "该题能否进链路"的地方, 且它不调用任何 LLM。
    """
    print("\n[S02-10] 配额职责留在代码层")
    import inspect
    from story.quality import check_signature as _cs
    from story.quality import cross_puzzle_gate as _cg
    for fn, name in ((_cs, "check_signature"), (_cg, "cross_puzzle_gate")):
        src = inspect.getsource(fn)
        check(f"{name} 不调用 LLM/writer",
              "PuzzleWriter" not in src and "client" not in src
              and "messages(" not in src, name)
    # 超配额 -> gate 拒绝, 而不是"让 reviewer 去改"
    recent = [_sig(mechanism_family="hidden_function")] * 2
    s = good_spec()
    bad = cross_puzzle_gate(s, recent, Quotas(), s.blueprint)
    check("超配额时 gate 返回拒绝原因", bad != [], bad)


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
        # ---- v5: 通关合同 ----
        test_v5_completion_contract_roundtrip,
        test_v5_old_archive_reads_as_no_contract,
        test_v5_support_cannot_be_completion,
        test_v5_completion_capped_at_two,
        test_v5_completion_must_exist_and_be_referenced,
        test_v5_core_answer_bounds,
        test_v5_key_role_atom_is_enough,
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
        test_describe_exposes_reveal_target,
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
        # ---- S1: 调度器不选"生成前就必死"的 pair ----
        test_s1_recent_pairs_is_shared_key,
        test_s1_scheduler_never_picks_live_regression_pair,
        test_s1_never_returns_duplicate_while_alternative_exists,
        test_s1_fallback_also_avoids_recent_pair,
        test_s1_fallback_degrades_when_all_blocked,
        # ---- Q2: quality-v8 discovery_beats ----
        test_q2_discovery_beat_validation,
        test_q2_completion_stays_one_or_two,
        test_q2_legacy_archive_without_beats_still_reads,
        test_q2_dark_tone_target_is_code,
        # ---- C4: 诡异基调目标带真的接到调度器上 ----
        test_c4_dark_tone_rolling_simulation,
        test_cross_puzzle_gate,
        test_template_tables_have_no_dead_ends,
        test_scheduler_output_always_valid_blueprint,
        test_trauma_ritual_quota_applies_via_shape,
        test_signature_helpers,
        test_quotas_from_config,
        # ---- Step 02: reveal/tone scheduler + rolling quota ----
        test_s02_neutral_bias_removed,
        test_s02_neutral_quota_limits_distribution,
        test_s02_straight_explanation_quota,
        test_s02_same_reveal_mode_quota,
        test_s02_procedural_rule_dependency_quota,
        test_s02_unknown_reveal_is_not_counted_as_straight,
        test_s02_reveal_preference_puts_straight_last,
        test_s02_choose_reveal_mode_avoids_exhausted,
        test_s02_blueprint_carries_reveal_target,
        test_s02_quota_responsibility_stays_in_code,
        # ---- Batch A closeout: hierarchical scheduler ----
        test_closeout_hierarchy_family_probability_not_inflated_by_width,
        test_closeout_hierarchy_emotion_picked_before_shape,
        test_closeout_choose_reveal_mode_picks_by_deficit,
        test_closeout_same_reveal_mode_covers_straight,
        test_closeout_reveal_adherence_gate,
        test_closeout_adherence_skipped_without_assigned_blueprint,
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
