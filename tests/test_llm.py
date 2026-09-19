"""运行: uv run tests/test_llm.py（完全离线, 无网络）。

验证 PuzzleWriter 走**强制工具调用**时, 能把 tool_input 正确转成结构化结果,
以及在工具不可用(只回了文本)时**回退到宽容解析**。

不联网: 用一个假的 client 顶替 AnthropicMessagesClient。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from story.llm import (  # noqa: E402
    RIDDLE_PROMPT_VERSION, LLMResult, PuzzleWriter,
)
from story.quality import QUALITY_POLICY_VERSION  # noqa: E402

FAIL = [0]


def check(name, cond, extra=""):
    if cond:
        print(f"  ok  {name}")
        return
    print(f"  FAIL {name}  {extra}")
    FAIL[0] += 1


class FakeClient:
    """按顺序吐预设的 LLMResult, 并记录收到的 tool 参数。"""

    def __init__(self, results, cfg=None):
        self._results = list(results)
        self.calls = []
        self.runtime_cfg = cfg or runtime_cfg()
        # 默认 blueprint 必须与 riddle() 自报的 signature 一致 ——
        # 严格比对生效后, 不一致会被正确拒绝, 那不是这些用例要测的东西。
        self.default_blueprint = bp_for()
        # client.cfg 是**传输层**配置(base_url/key/model)。业务层的
        # temperature/quota 在 runtime_cfg 上, 由 PuzzleWriter 单独持有。
        self.cfg = _FakeLLMCfg()

    def messages(self, system, user, max_tokens=None, tool=None,
                 temperature=None, timeout=None, max_retries=None):
        self.calls.append({"system": system, "user": user, "tool": tool,
                           "temperature": temperature,
                           "timeout": timeout, "max_retries": max_retries})
        if not self._results:
            return LLMResult(error="no more canned results")
        return self._results.pop(0)


class _FakeLLMCfg:
    """LLMConfig 的最小替身。

    **故意不带 temperature/quota** —— 它们属于 runtime Config。
    早先这个替身"恰好什么都有", 于是掩盖了生产环境里参数静默失效的 bug。
    现在 Fake 也照生产的样子来: 传输层就只管传输层的东西。
    """
    model = "fake-model"


def runtime_cfg(**kw):
    """真的 `Config` 实例(带 temperature/quota)。

    用它而不是再造一个替身 —— 替身与真类一旦不同步就会再次掩盖问题。
    """
    from story.config import Config
    kw.setdefault("sim_path", "x")
    kw.setdefault("no_llm", False)
    return Config(**kw)


# ----------------------------------------------------------------------
# Q2: 出题现在要交出**完整 spec**, 硬校验会检查 facts/引用/clue 原文。
# 所以测试里的稿子不能只是 puzzle+answer, 得是一份结构合格的 spec。
# 下面三个帮手生成这样的返回, 各用例按需覆盖字段。
# ----------------------------------------------------------------------
_GOOD_PUZ = "灯塔守塔人只在退潮的那几个小时亮灯, 涨潮后反而熄掉。为什么?"


def clues_for(puzzle: str) -> list:
    """从谜面里**逐字**摘两段当 fair_clues。

    硬校验要求 quote 真的在谜面里, 所以换谜面就必须换 quote ——
    写死谜面文案的 quote 会让"换一道题"的用例全部因为 clue 失败而挂。
    """
    body = puzzle.rstrip("?？")
    marks = [m for m in ("只在退潮的那几个小时亮灯", "涨潮后反而熄掉")
             if m in body]
    if len(marks) >= 2:
        return [{"quote": marks[0], "supports_atoms": ["a1"]},
                {"quote": marks[1], "supports_atoms": ["a2"]}]
    # 通用兜底: 取谜面开头/中段两截
    n = max(4, len(body) // 3)
    q1, q2 = body[:n], body[n:2 * n]
    out = [{"quote": q1, "supports_atoms": ["a1"]}]
    if q2:
        out.append({"quote": q2, "supports_atoms": ["a2"]})
    return out


def riddle(puzzle=None, answer="退潮时礁石露出, 亮灯是标礁石位置。",
           hints=("a", "b", "c"), **kw):
    """造一份**能过硬校验**的出题返回。

    fair_clues 由 `clues_for(puzzle)` 现算, 所以传任意谜面都能过校验 ——
    这一整套用例的前提是"结构合格", 我们在这里测的是**流程**不是校验。
    """
    puzzle = puzzle or _GOOD_PUZ
    d = {
        "title": "灯塔",
        "puzzle": puzzle,
        "answer": answer,
        # ---- v5 通关合同 ----
        # 灯塔题: 房间确认"礁石露出" + "灯是标礁石" 两点就解出了,
        # 这正是 completion 想表达的"最少必须知道什么"。
        "core_answer": "他亮灯是为了标出退潮时露出的礁石, 不是给船引路。",
        "completion_fact_ids": ["f1", "f2"],
        "hints": list(hints),
        "facts": [
            {"id": "f1", "text": "退潮时礁石露出水面", "kind": "core"},
            {"id": "f2", "text": "灯的真正作用是标示礁石位置", "kind": "core"},
            {"id": "f3", "text": "涨潮后亮灯会误导船只", "kind": "support"},
            {"id": "f4", "text": "不是为了纪念死者", "kind": "exclusion"},
        ],
        "solve_atoms": [
            {"id": "a1", "role": "cause", "text": "退潮使礁石需要标出",
             "fact_ids": ["f1"]},
            {"id": "a2", "role": "mechanism", "text": "灯是标礁石不是引路",
             "fact_ids": ["f2", "f3"]},
        ],
        "fair_clues": clues_for(puzzle),
        "signature": {
            "mechanism_family": "hidden_function",
            "solution_shape": "hidden_function_explains_behavior",
            "domain": "maritime", "relation": "stranger",
            "emotion_mode": "neutral", "time_shape": "instant",
            "death": False, "past_trauma": False,
            "long_term_profession": False, "repeated_ritual": False,
            # ---- v4 新增的 observed 字段 ----
            "reveal_mode": "meaning_flip",
            "procedural_rule_dependency": False,
        },
    }
    d.update(kw)
    return d


def bp_for(fam="hidden_function",
           shape="hidden_function_explains_behavior",
           domain="maritime", relation="stranger",
           emotion="neutral", time_shape="instant", **flags):
    """与 `riddle()` 自报 signature 一致的 blueprint。

    严格比对生效后, 测试若用默认 blueprint(information_gap)去配
    自报 hidden_function 的稿子, 会被正确地拒掉 —— 那不是被测行为。
    """
    from story.puzzle import PuzzleBlueprint
    d = {"mechanism_family": fam, "solution_shape": shape, "domain": domain,
         "relation": relation, "emotion_mode": emotion, "time_shape": time_shape,
         "death": False, "past_trauma": False, "long_term_profession": False,
         "repeated_ritual": False,
         # 与 `sig_ok()` 的 observed reveal 一致 —— 否则 Batch A closeout 的
         # reveal adherence 会(正确地)拒掉每一稿。
         "reveal_mode": "meaning_flip"}
    d.update(flags)
    return PuzzleBlueprint(**d)


def sig_ok():
    """与 riddle() 自报一致的 observed_signature。"""
    return {"mechanism_family": "hidden_function",
            "solution_shape": "hidden_function_explains_behavior",
            "domain": "maritime", "relation": "stranger",
            "emotion_mode": "neutral", "time_shape": "instant",
            "death": False, "past_trauma": False,
            "long_term_profession": False, "repeated_ritual": False,
            "reveal_mode": "meaning_flip",
            "procedural_rule_dependency": False}


def qc_ok(**kw):
    """v5 `quality_checks` —— 四项全 true 才是合格稿。

    `_apply_review` 对它 **fail closed**: 缺一项、或任一项不是 True,
    整稿拒收(即使 decision="pass")。所以所有"应该通过"的 fixture
    都必须带上它。
    """
    d = {"narrator_truthful": True, "mechanism_consistent": True,
         "core_answer_direct": True, "completion_contract_minimal": True}
    d.update(kw)
    return d


def review_ok(puzzle=None, **kw):
    """审稿: pass —— **原样回传**整套字段。

    P0-2 之后 `pass` 也要走 `_apply_review`, 而后者要求"改了就必须整套
    同步"。真实审稿人 pass 时会把 puzzle/answer/atoms/clues 原样带回,
    这里照做 —— 否则会被（正确地）判成"改了却没同步"。

    `puzzle` 是**它在审的那道题**。必须与同一条 FakeClient 队列里上一发
    出题用的谜面一致: 代码现在会把 fair_clues 的 quote 逐字对照谜面,
    对不上就是"审稿人引用了不存在的句子", 会被正确拒掉。
    """
    d = riddle(puzzle=puzzle)
    d.update({"decision": "pass", "observed_signature": sig_ok(),
              "quality_checks": qc_ok()})
    d.update(kw)
    if "fair_clues" not in kw:
        d["fair_clues"] = clues_for(d["puzzle"])
    return d


def review_fix(puzzle, **kw):
    """审稿: fix —— 给出改后的谜面 + 整套同步的 facts/atoms/clues。

    默认带回与 `riddle()` 等价的 facts/atoms/clues(quote 按新谜面重算),
    这样"改稿"在结构上是自洽的。
    """
    d = riddle(puzzle=puzzle)
    d.update({"decision": "fix", "note": "已修改",
              "observed_signature": sig_ok(), "quality_checks": qc_ok()})
    d.update(kw)
    # v5: 审稿改了谜面/谜底 -> core_answer / completion_fact_ids 必须
    # 一起给出, 否则 `_apply_review` 整稿拒收(它们与 facts/atoms/clues
    # 同属一套同步 bundle)。
    if "core_answer" not in kw:
        d["core_answer"] = "他亮灯是为了标出退潮时露出的礁石。"
    if "completion_fact_ids" not in kw:
        d["completion_fact_ids"] = ["f1", "f2"]
    # 审稿改谜面时 quote 必须跟着改后的谜面走
    if "fair_clues" not in kw:
        d["fair_clues"] = clues_for(puzzle)
    return d


def review_rewrite(reason="没有公平推理路径", **kw):
    """审稿: rewrite —— 不给 patch, 只给理由。"""
    d = {"decision": "rewrite", "rewrite_reason": reason,
         "observed_signature": sig_ok()}
    d.update(kw)
    return d



# ======================================================================
# UX-2: v5 通关合同 -> Answer 不做最终判定
# ======================================================================
def _verdict_tool(established=None, touched=None, cand=False):
    return LLMResult(tool_input={"answers": [{
        "id": 1, "verdict": "是", "comment": "好眼力",
        "solution_candidate": cand,
        "touched_fact_ids": list(touched or []),
        "established_fact_ids": list(established or []),
    }]}, model="m")


def test_ux_g_v5_skips_final_judge():
    """Case G: 有通关合同 -> 只调**一次** client, 绝不调 emit_judgement。

    若这里仍调 Final Judge, 就又多了一条绕开合同的通关路径: 观众说中
    一条 support 剧情也可能被裁判判"猜中", 于是合同形同虚设。
    """
    print("\n[UX-G] v5 不调 Final Judge")
    # 队列里**只准备一次** Answer 返回。若代码偷偷调裁判, FakeClient
    # 会吐出 "no more canned results" 错误 —— 断言的就是"没调"。
    fc = FakeClient([_verdict_tool(established=["f1"], cand=True)])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    out, err = w.answer("谜面?", "谜底。", [], 1, "甲", "她是姐姐",
                        facts=riddle()["facts"],
                        completion_fact_ids=["f1"])
    check("只调了一次 client", len(fc.calls) == 1, len(fc.calls))
    check("那一次是 emit_verdict",
          fc.calls[0]["tool"]["name"] == "emit_verdict",
          fc.calls[0]["tool"].get("name"))
    check("没有 emit_judgement 调用",
          all(c["tool"] and c["tool"]["name"] != "emit_judgement"
              for c in fc.calls), [c["tool"] for c in fc.calls])
    check("返回了一条结果", len(out) == 1, out)
    check("established 被带回", out and out[0].established_fact_ids == ["f1"],
          out[0].established_fact_ids if out else None)
    check("**没有**被标成 P.SOLVE", out and out[0].verdict != "揭晓",
          out[0].verdict if out else None)


def test_ux_h_legacy_still_judges():
    """Case H: 无合同 -> Final Judge 流程**完全不变**。"""
    print("\n[UX-H] legacy 无合同 -> 仍走 Final Judge")
    fc = FakeClient([
        _verdict_tool(cand=True),
        LLMResult(tool_input={"is_guess": True, "cause_hit": True,
                              "mechanism_hit": True,
                              "matched_atoms": ["a1", "a2"]}, model="m"),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    out, err = w.answer("谜面?", "谜底。", [], 1, "甲",
                        "退潮时礁石露出, 所以灯是在标礁石位置",
                        solve_atoms=riddle()["solve_atoms"],
                        facts=riddle()["facts"],
                        completion_fact_ids=[])
    check("调了两次(verdict + judge)", len(fc.calls) == 2, len(fc.calls))
    check("第二次是 emit_judgement",
          fc.calls[1]["tool"]["name"] == "emit_judgement",
          fc.calls[1]["tool"].get("name"))
    check("legacy 仍能判出通关", out and out[0].verdict == "揭晓",
          out[0].verdict if out else None)


def test_ux_established_filtered_in_answer():
    """模型编造的 fact id 在 worker 侧就被丢掉。"""
    print("\n[UX-filter] answer() 过滤不存在的 established id")
    fc = FakeClient([_verdict_tool(established=["f999", "f1", "f1"])])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    out, err = w.answer("谜面?", "谜底。", [], 1, "甲", "猜",
                        facts=riddle()["facts"])
    check("f999 被丢、去重后只剩 f1",
          out and out[0].established_fact_ids == ["f1"],
          out[0].established_fact_ids if out else None)


def test_riddle_tool():
    print("[出题: 强制工具]")
    fc = FakeClient([
        LLMResult(tool_input=riddle(), model="m"),
        LLMResult(tool_input=review_ok()),           # 自检通过
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    r = w.gen_riddle(blueprint=fc.default_blueprint)
    check("谜面解析", r.puzzle == _GOOD_PUZ, r)
    check("谜底解析", r.answer and "礁石" in r.answer, r.answer)
    check("提示 3 条", len(r.hints) == 3, r.hints)
    check("用了强制工具", fc.calls[0]["tool"] is not None
          and fc.calls[0]["tool"]["name"] == "emit_riddle", fc.calls[0]["tool"])
    check("出题带了 temperature", fc.calls[0]["temperature"] == 0.8,
          fc.calls[0]["temperature"])
    check("审稿带了 temperature", fc.calls[1]["temperature"] == 0.2,
          fc.calls[1]["temperature"])
    check("solve_atoms 带 fact_ids", r.solve_atoms[0].get("fact_ids") == ["f1"],
          r.solve_atoms)
    check("fair_clues 是 {quote,supports_atoms}",
          r.fair_clues[0].get("quote") == "只在退潮的那几个小时亮灯", r.fair_clues)


def test_gen_spec_returns_puzzle_spec():
    print("[Q2: gen_spec 返回结构化 PuzzleSpec]")
    from story.puzzle import PuzzleSpec
    fc = FakeClient([LLMResult(tool_input=riddle(), model="m"),
                     LLMResult(tool_input=review_ok())])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    spec = w.gen_spec(blueprint=fc.default_blueprint)
    check("返回的是 PuzzleSpec", isinstance(spec, PuzzleSpec), type(spec))
    check("facts 4 条", len(spec.facts) == 4, spec.facts)
    check("core facts 2 条", len(spec.core_hidden_facts()) == 2, spec.facts)
    check("atoms 2 条且带 role", [a.role for a in spec.solve_atoms]
          == ["cause", "mechanism"], spec.solve_atoms)
    check("atom 引用 fact", spec.solve_atoms[0].fact_ids == ["f1"],
          spec.solve_atoms[0].fact_ids)
    check("clue quote 逐字在谜面里",
          all(c.quote in spec.puzzle for c in spec.fair_clues),
          [(c.quote, spec.puzzle) for c in spec.fair_clues])
    check("signal 来自模型自报", spec.signature.domain == "maritime",
          spec.signature)
    # 断言的是"写的就是当前常量", 不是某个写死的版本串 —— 否则每次
    # bump prompt/policy 版本都要来改测试(Step 04 就是这么被绊到的)。
    check("prompt_version 写成当前常量",
          spec.prompt_version == RIDDLE_PROMPT_VERSION,
          spec.prompt_version)
    check("quality_policy_version 写成当前常量",
          spec.quality_policy_version == QUALITY_POLICY_VERSION,
          spec.quality_policy_version)
    from story.puzzle import PuzzleSpec as _PS
    back = _PS.from_dict(spec.to_archive())
    check("archive round trip", back.puzzle == spec.puzzle
          and len(back.facts) == 4, back)


def test_hard_validator_rejects_missing_facts():
    print("[Q2: 硬校验在 reviewer **之前**就拦下结构问题]")
    bad = riddle()
    bad["facts"] = []
    fc = FakeClient([LLMResult(tool_input=bad),
                     LLMResult(tool_input=riddle(puzzle="二稿灯塔题目。为什么?")),
                     LLMResult(tool_input=review_ok("二稿灯塔题目。为什么?"))])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    r = w.gen_riddle(blueprint=fc.default_blueprint)
    check("第一稿被硬校验拦下", r.puzzle and "二稿" in r.puzzle, r)
    check("没给第一稿花 reviewer 调用",
          fc.calls[1]["tool"]["name"] == "emit_riddle",
          fc.calls[1]["tool"]["name"])


def test_hard_validator_rejects_fake_fair_clue():
    print("[Q2: fair_clue 的 quote 不在谜面里 -> 硬校验拒]")
    bad = riddle()
    bad["fair_clues"] = [{"quote": "这句话谜面里根本没有",
                          "supports_atoms": ["a1"]}]
    fc = FakeClient([LLMResult(tool_input=bad),
                     LLMResult(tool_input=riddle(puzzle="换个题目的灯塔。为什么?")),
                     LLMResult(tool_input=review_ok("换个题目的灯塔。为什么?"))])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    r = w.gen_riddle(blueprint=fc.default_blueprint)
    check("被拒后重出成功", r.puzzle and "换个题目" in r.puzzle, r)


def test_blueprint_violation_rejected():
    print("[Q2: 题违反 blueprint(death=false 却写死人) -> 拒]")
    from story.puzzle import PuzzleBlueprint
    bad = riddle()
    bad["signature"]["death"] = True
    fc = FakeClient([LLMResult(tool_input=bad),
                     LLMResult(tool_input=riddle(puzzle="另一道灯塔题。为什么?")),
                     LLMResult(tool_input=review_ok("另一道灯塔题。为什么?"))])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    bp = PuzzleBlueprint(mechanism_family="hidden_function",
                         solution_shape="hidden_function_explains_behavior",
                         domain="maritime", death=False)
    r = w.gen_riddle(blueprint=bp)
    check("违反 blueprint 的稿被拒", r.puzzle and "另一道" in r.puzzle, r)


def test_blueprint_injected_into_prompt():
    print("[Q2: blueprint 作为**硬约束**注入出题 prompt]")
    from story.puzzle import PuzzleBlueprint
    fc = FakeClient([LLMResult(tool_input=riddle()),
                     LLMResult(tool_input=review_ok())])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    bp = PuzzleBlueprint(mechanism_family="object_misuse",
                         solution_shape="misunderstood_object",
                         domain="commerce", relation="colleague")
    w.gen_spec(blueprint=bp)
    u = fc.calls[0]["user"]
    check("prompt 里有 Blueprint 段", "Blueprint" in u, u[:200])
    check("点名了 mechanism_family", "object_misuse" in u, u[:400])
    check("点名了 domain", "commerce" in u, u[:400])
    check("说明不能修改", "不能修改" in u, u[:400])
    check("审稿请求也带 blueprint", "Blueprint" in fc.calls[1]["user"],
          fc.calls[1]["user"][-300:])


def test_reviewer_fixes_in_place():
    print("[审稿: 不合格时由审稿人**直接改好**, 不整题重出]")
    # 上一版是"毙掉 -> 重出一稿全新的"。现在改成: 审稿人读懂了问题就
    # **自己动手改**, 只在原稿上动该动的地方 —— 少一道信息损耗,
    # 也不会把一道只差一句话的好题整个丢掉。
    P1 = "海角守塔人只在退潮的那几个小时亮灯, 涨潮后反而熄掉。为什么?"
    fc = FakeClient([
        LLMResult(tool_input=riddle()),
        LLMResult(tool_input=review_fix(P1, note="补了具体地点")),
        LLMResult(tool_input=review_ok()),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    r = w.gen_riddle(blueprint=fc.default_blueprint)
    check("采用了审稿人的改稿(而不是重出一稿)",
          r.puzzle and "海角" in r.puzzle, r)
    check("改稿的谜底也一并采用", r.answer and "礁石" in r.answer, r.answer)
    check("改稿的提示也一并采用", r.hints == ["a", "b", "c"], r.hints)
    # 审稿人就地改好 -> 硬校验过 -> 采用, 共 2 次调用。
    # (不需要"再审" —— 代码的硬校验比再问一次模型更可靠)
    check("共 2 次调用(出题 + 审稿)",
          len(fc.calls) == 2, len(fc.calls))


def test_hard_rule_asks_reviewer_to_fix():
    print("[审稿: 硬规则(第一人称)也交给审稿人改, 不直接丢]")
    # 第一人称是代码能确定判出来的, 但**能改**(换个人称而已)。
    # 所以不是丢掉重出, 而是点名让审稿人改。
    P0 = "我每晚都听见楼上有人走动, 可楼上根本没人住。为什么?"
    P1 = "他每晚都听见楼上有人走动, 可楼上根本没人住。为什么?"
    fc = FakeClient([
        LLMResult(tool_input=riddle(puzzle=P0)),
        LLMResult(tool_input=review_fix(P1, note="改成第三人称")),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    r = w.gen_riddle(blueprint=fc.default_blueprint)
    check("改成第三人称后被采用",
          r.puzzle and r.puzzle.startswith("他每晚"), r)
    # 审稿请求里应点名"第一人称"这个已知问题
    review_user = fc.calls[1]["user"]
    check("点名了第一人称问题", "第一人称" in review_user, review_user[-300:])


def test_reviewer_no_fix_falls_back_to_regen():
    print("[审稿: 审稿人没给改稿时, 退回重出]")
    P2 = "他每天数一遍冰箱里的鸡蛋, 数完就把冰箱锁上。为什么?"
    fc = FakeClient([
        LLMResult(tool_input=riddle()),
        LLMResult(tool_input={"ok": False, "note": "不好"}),
        LLMResult(tool_input=riddle(puzzle=P2)),
        LLMResult(tool_input=review_ok(P2)),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    r = w.gen_riddle(blueprint=fc.default_blueprint)
    check("退回重出后采用新稿", r.puzzle and "鸡蛋" in r.puzzle, r)
    check("共 4 次调用(出题/审稿/重出/审稿)", len(fc.calls) == 4, len(fc.calls))


def test_riddle_check_retries_empty_tool_use():
    print("[出题: 质检拿到空 tool_input 要重出]")
    # 网关抖动的表现: tool_use 块存在但 input 为空。
    # 这**不算通过**, 应该重出一稿 —— 否则质检形同虚设。
    P2 = "二稿谜面, 另一个具体事件。为什么?"
    fc = FakeClient([
        LLMResult(tool_input=riddle()),
        # 审稿: 空 tool_input(抖动) -> 视为未通过
        LLMResult(tool_input={}, text="I'll analyze this puzzle"),
        # 第二稿
        LLMResult(tool_input=riddle(puzzle=P2)),
        LLMResult(tool_input=review_ok(P2)),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    r = w.gen_riddle(blueprint=fc.default_blueprint)
    check("空 tool_input 不算通过, 重出后成功",
          r.puzzle is not None and "二稿" in r.puzzle, r)
    check("共 4 次调用(生成/质检/生成/质检)", len(fc.calls) == 4, len(fc.calls))
    check("第 2 次调用确实是审稿",
          fc.calls[1]["tool"]["name"] == "emit_review",
          fc.calls[1]["tool"]["name"])


def test_first_person_story_rejected():
    print("[出题: 第一人称叙事是故事, 不是谜题]")
    from story.llm import _is_first_person_story
    for t in ["深夜我独自在家，座机响了，接起来是我自己的声音。",
              "我住的老楼电梯里，只有我和邻居老太太两个人。",
              "我正在厨房做饭，突然听见门外有人叫我名字。"]:
        check(f"第一人称: {t[:14]}", _is_first_person_story(t) is True, t)
    for t in ["一个男人走进餐厅，点了一份海龟汤，喝了一口就冲出去自杀了。",
              "男人对酒保说：「请给我一杯水。」酒保却掏出一把枪指着他。",
              "她在葬礼上遇见一个陌生男人，回家后就把亲姐姐杀了。"]:
        check(f"第三人称: {t[:14]}", _is_first_person_story(t) is False, t)
    # 端到端: 第一人称那稿交给审稿人改(而不是直接弃用)
    P0 = "深夜我独自在家, 座机响了, 接起来是我自己的声音。为什么?"
    P1 = "一个男人深夜独自在家, 座机响了, 接起来是他自己的声音。为什么?"
    fc = FakeClient([
        LLMResult(tool_input=riddle(puzzle=P0)),
        LLMResult(tool_input=review_fix(P1, note="改成第三人称")),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    r = w.gen_riddle(blueprint=fc.default_blueprint)
    check("第一人称那稿被审稿人改成第三人称",
          r.puzzle and r.puzzle.startswith("一个男人"), r)


def test_no_repeat_puzzles():
    print("[出题: 不重复]")
    from story.llm import _too_similar
    # 换个说法重讲同一题 -> 应判为太像
    a = "男人走进一家酒吧，对酒保说：请给我一杯水。酒保却从吧台下掏出一把枪指着他。"
    b = "男人走进酒吧，向酒保要一杯水。酒保却从吧台下面掏出一把枪对着他。"
    check("同题改写应被拦", _too_similar(b, [a]) != "", _too_similar(b, [a]))
    # 完全不同的题 -> 放行
    c = "她独自住在山上, 每天傍晚把一只空玻璃杯倒扣在窗台上。为什么?"
    check("不同题应放行", _too_similar(c, [a]) == "", _too_similar(c, [a]))
    # 端到端: 与已出过的题太像 -> 重出
    # avoid 检查在硬校验**之后**; 太像的那稿要能过校验才会走到 avoid。
    fc = FakeClient([
        LLMResult(tool_input=riddle(puzzle=b + "为什么?")),
        LLMResult(tool_input=review_ok(b + "为什么?")),
        # 与 a 太像 -> 重出
        LLMResult(tool_input=riddle(puzzle=c)),
        LLMResult(tool_input=review_ok(c)),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    r = w.gen_riddle(avoid=[a], blueprint=fc.default_blueprint)
    check("太像的那稿被弃用, 采用新题", r.puzzle == c, r.puzzle)
    check("共 4 次调用(出题/审稿/重出/审稿)", len(fc.calls) == 4, len(fc.calls))


def test_english_riddle_rejected_on_text_path():
    print("[出题: 英文稿在文本回退路径上也要被拒]")
    # 实测漏洞: 中文检查原来只加在 tool_input 分支, 结果模型从文本分支
    # 吐英文上了屏(第 1 题变成了 "I'll create a proper, self-contained…")。
    fc = FakeClient([
        LLMResult(text="I'll create a proper, self-contained lateral thinking "
                       "puzzle for you. Here it is: ..."),
        LLMResult(tool_input=riddle()),
        LLMResult(tool_input=review_ok()),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    r = w.gen_riddle(blueprint=fc.default_blueprint)
    check("英文稿被弃用, 采用中文稿",
          r.puzzle is not None and "灯塔" in r.puzzle, r)


def test_fallback_riddles_rotate():
    print("[兜底题不总是同一道]")
    from story.parser import FALLBACK_RIDDLES
    check("兜底题 >= 3 道", len(FALLBACK_RIDDLES) >= 3, len(FALLBACK_RIDDLES))
    texts = {p for p, _ in FALLBACK_RIDDLES}
    check("兜底题互不相同", len(texts) == len(FALLBACK_RIDDLES), texts)
    check("不再用'海龟汤'那道烂大街的",
          all("海龟汤" not in p for p, _ in FALLBACK_RIDDLES), texts)


def test_riddle_fallback():
    print("[出题: 工具不可用时回退解析]")
    # 文本回退路径拿不到 facts/signature, 所以**过不了硬校验** ——
    # 这里只验证宽容解析器能把谜面/谜底抠出来(gen_spec 的解析行为)。
    fc = FakeClient([
        LLMResult(text=(
            "【谜面】\n雨夜有人敲门, 开门却没人。为什么?\n【谜底】\n是他自己的回声。\n"
            "【提示】\n提示一：注意天气。\n")),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    spec = w.gen_spec(check=False)          # 关掉校验, 只看解析
    check("回退解析出谜面", "敲门" in (spec.puzzle or ""), spec)
    check("回退解析出谜底", "回声" in (spec.answer or ""), spec)


def test_answer_tool():
    print("[裁决: 强制工具]")
    fc = FakeClient([LLMResult(tool_input={
        "answers": [{"id": 7, "verdict": "是", "comment": "就差一点",
                     "touched_fact_ids": ["f1"], "solution_candidate": False}]})])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    res, err = w.answer("谜面", "谜底", [], 7, "甲", "是同伴的肉吗",
                        facts=[{"id": "f1", "text": "打嗝", "kind": "core"}])
    check("拿到一条裁决", len(res) == 1, res)
    check("qid 用引擎给的", res[0].qid == 7, res)
    check("verdict 正确", res[0].verdict == "是", res)
    check("comment 保留", res[0].comment == "就差一点", res)
    check("touched_fact_ids 带出来", res[0].touched_fact_ids == ["f1"], res[0])
    check("solution_candidate 带出来",
          res[0].solution_candidate is False, res[0])


def test_answer_rejects_bad_enum():
    print("[裁决: 非法枚举被丢弃]")
    fc = FakeClient([LLMResult(tool_input={
        "answers": [{"id": 1, "verdict": "也许吧", "comment": "x"}]})])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    res, err = w.answer("谜面", "谜底", [], 1, "甲", "问题")
    check("非法裁决被拒(返回空)", res == [], res)
    check("带错误信息", err is not None, err)
    # Q5: 「揭晓」已从枚举里删掉; 模型若仍吐出来, 降级为"是"而**不是**通关。
    fc2 = FakeClient([LLMResult(tool_input={
        "answers": [{"id": 2, "verdict": "揭晓", "comment": "x"}]})])
    w2 = PuzzleWriter(client=fc2, runtime_cfg=fc2.runtime_cfg)
    res2, _ = w2.answer("谜面", "谜底", [], 2, "甲", "同伴的肉对吧")
    check("废弃的揭晓被降级为'是'", res2 and res2[0].verdict == "是", res2)
    check("降级后没有调裁判",
          [c["tool"]["name"] for c in fc2.calls] == ["emit_verdict"],
          [c["tool"]["name"] for c in fc2.calls])


def test_answer_enum_forced_by_schema():
    print("[Q5: candidate=true 才调裁判]")
    fc = FakeClient([
        LLMResult(tool_input={"answers": [
            {"id": 3, "verdict": "是", "comment": "接近了",
             "solution_candidate": True}]}),
        LLMResult(tool_input={"is_guess": True, "cause_hit": True,
                              "mechanism_hit": True}),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    res, _ = w.answer("谜面", "谜底", [], 3, "甲",
                      "退潮礁石露出所以灯是标礁石对吗")
    check("candidate=true + 裁判确认 -> 揭晓",
          res and res[0].verdict == "揭晓", res)
    names = [c["tool"]["name"] for c in fc.calls]
    check("确实调了裁判", names == ["emit_verdict", "emit_judgement"], names)
    # 反例: candidate=false 不调裁判
    fc2 = FakeClient([LLMResult(tool_input={"answers": [
        {"id": 4, "verdict": "是", "solution_candidate": False}]})])
    w2 = PuzzleWriter(client=fc2, runtime_cfg=fc2.runtime_cfg)
    w2.answer("谜面", "谜底", [], 4, "甲", "他是医生吗")
    check("candidate=false 不调裁判",
          [c["tool"]["name"] for c in fc2.calls] == ["emit_verdict"],
          [c["tool"]["name"] for c in fc2.calls])


def test_open_question_never_solves():
    print("[开放疑问句不判猜中]")
    from story.llm import _is_open_question
    for q in ["他为什么跑", "为什么他每天都要洗手", "他怎么做到的",
              "他是什么职业", "他几点出门"]:
        check(f"开放疑问: {q}", _is_open_question(q) is True, q)
    for q in ["他是不是瞎了", "同伴把肉给他吃了吗", "他是灯塔看守人吗",
              "他知道吗", "地铁上有人吗"]:
        check(f"是非题: {q}", _is_open_question(q) is False, q)
    # 端到端: 即便模型把"#为什么"标成候选, 纯信息索取也不送裁判
    fc = FakeClient([
        LLMResult(tool_input={"answers": [
            {"id": 1, "verdict": "是", "solution_candidate": True}]}),
        LLMResult(tool_input={"is_guess": True, "cause_hit": True,
                              "mechanism_hit": True}),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    res, _ = w.answer("谜面", "谜底", [], 1, "甲", "他为什么跑")
    check("开放疑问保持原裁决", res and res[0].verdict == "是", res)
    check("没有调裁判(只用了 1 次调用)", len(fc.calls) == 1,
          [c["tool"]["name"] for c in fc.calls])


def test_verdict_solve_must_pass_judge():
    print("[candidate + 裁判否决 -> 不通关]")
    # 旧机制允许裁决阶段自己给"揭晓", 实测经常误判且绕过所有检查。
    # 现在「揭晓」已从枚举删掉, 通关只能由裁判确认的 candidate 产生。
    fc = FakeClient([
        LLMResult(tool_input={"answers": [
            {"id": 1, "verdict": "是", "solution_candidate": True}]}),
        LLMResult(tool_input={"is_guess": True, "cause_hit": False,
                              "mechanism_hit": False}),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    res, _ = w.answer("谜面", "谜底", [], 1, "甲", "同伴的肉对吧")
    check("裁判否决 -> 保持'是'", res and res[0].verdict == "是", res)
    check("确实问了裁判",
          [c["tool"]["name"] for c in fc.calls]
          == ["emit_verdict", "emit_judgement"],
          [c["tool"]["name"] for c in fc.calls])


def test_open_question_with_hypothesis_can_solve():
    print("[带因果假设的疑问句仍可通关]")
    # 这句话带"为什么", 但它给出了**完整的因果假设** —— 不该被"纯疑问句"
    # 规则一票否决, 否则说中答案的观众永远猜不中。
    fc = FakeClient([
        LLMResult(tool_input={"answers": [
            {"id": 1, "verdict": "是", "solution_candidate": True}]}),
        LLMResult(tool_input={"is_guess": True, "cause_hit": True,
                              "mechanism_hit": True}),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    res, _ = w.answer(
        "谜面", "谜底", [], 1, "甲",
        "为什么他每天多待十五分钟, 是因为以前灯晚亮十五分钟出过事故吗")
    check("带假设的疑问句不被降级", res and res[0].verdict == "揭晓", res)
    check("确实走了裁判复核",
          [c["tool"]["name"] for c in fc.calls]
          == ["emit_verdict", "emit_judgement"],
          [c["tool"]["name"] for c in fc.calls])


def test_llm_failure_returns_unavailable_not_irrelevant():
    print("[LLM 故障不能伪装成'无关']")
    # "无关"是断言"你的猜测与谜底无关" —— 那是**错误信息**, 会把观众的
    # 思路带偏。失败时必须给中性的"未判定"。
    from story import parser as P
    check("UNAVAILABLE 常量存在", P.UNAVAILABLE == "未判定", P.UNAVAILABLE)
    # 工具返回不可用 -> 结果里没有裁决 -> answer() 报错(上层转"未判定")
    fc = FakeClient([LLMResult(error="网关抖动")])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    res, err = w.answer("谜面", "谜底", [], 1, "甲", "他是盲人吗")
    check("失败时没有臆造裁决", res == [], res)
    check("带出错误信息", err is not None, err)


def test_judge():
    print("[裁判]")
    fc = FakeClient([LLMResult(tool_input={"is_guess": True, "cause_hit": True, "mechanism_hit": True}),
                     LLMResult(tool_input={"is_guess": True, "cause_hit": False, "mechanism_hit": False})])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    yes = w.judge("谜面", "谜底", "同伴的肉对吧").solved
    no = w.judge("谜面", "谜底", "他饿了吗").solved
    check("判中", yes is True, yes)
    check("判不中", no is False, no)


def test_hint_not_repeated():
    print("[提示: 不与已给过的重复]")
    from story.llm import _hint_repeated
    # 原样重复 / 换个说法重说 -> 都算重复
    for h in ["注意汤的味道", "汤的味道才是关键", "关键在于汤的味道"]:
        check(f"重复: {h}", _hint_repeated(h, ["注意汤的味道"]) is True, h)
    # 真的换个角度 -> 放行
    for h in ["他以前也喝过一次海龟汤", "想想同伴当年做了什么"]:
        check(f"放行: {h}", _hint_repeated(h, ["注意汤的味道"]) is False, h)
    # 端到端: 模型第一次给了重复的 -> 重出, 第二次给新的才采用
    fc = FakeClient([
        LLMResult(tool_input={"hint": "汤的味道才是关键"}),   # 与 given 重复
        LLMResult(tool_input={"hint": "想想他以前经历过什么"}),  # 新的
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    h, _ = w.hint("谜面", "谜底", 2, given=["注意汤的味道"])
    check("重复的被打回, 采用新的", h == "想想他以前经历过什么", h)
    check("确实重出过", len(fc.calls) == 2, len(fc.calls))


def test_hint_and_reveal():
    print("[提示 / 揭晓]")
    fc = FakeClient([LLMResult(tool_input={"hint": "注意汤的味道。不剧透"}),
                     LLMResult(tool_input={"reveal": "谜底是同伴的肉汤骗局。"})])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    h, _ = w.hint("谜面", "谜底", 1, [])
    rv, _ = w.reveal("谜面", "谜底", "solved", "甲")
    check("提示解析", h == "注意汤的味道。不剧透", h)
    check("揭晓解析", rv == "谜底是同伴的肉汤骗局。", rv)


def test_tool_actually_requested():
    print("[每个调用都带强制工具]")
    # answer() 现在会先问裁决、再问裁判(judge), 所以要多备一个 judge 结果
    fc = FakeClient([
        LLMResult(tool_input={"answers": [
            {"id": 1, "verdict": "是", "solution_candidate": True}]}),
        LLMResult(tool_input={"is_guess": True, "cause_hit": False,
                              "mechanism_hit": False}),
        LLMResult(tool_input={"hint": "h"}),
        LLMResult(tool_input={"reveal": "r"}),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    w.answer("p", "a", [], 1, "甲", "q")
    w.hint("p", "a", 1, [])
    w.reveal("p", "a", "solved")
    names = [c["tool"]["name"] for c in fc.calls]
    check("工具序列正确",
          names == ["emit_verdict", "emit_judgement", "emit_hint", "emit_reveal"],
          names)


def test_answer_consults_judge():
    print("[裁决后按 candidate 决定是否问裁判]")
    fc = FakeClient([
        LLMResult(tool_input={"answers": [
            {"id": 5, "verdict": "是", "solution_candidate": True}]}),
        LLMResult(tool_input={"is_guess": True, "cause_hit": True,
                              "mechanism_hit": True}),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    res, _ = w.answer("谜面", "谜底", [], 5, "甲", "同伴的肉对吧")
    check("裁判命中 -> 升级为揭晓", res and res[0].verdict == "揭晓", res)
    names = [c["tool"]["name"] for c in fc.calls]
    check("确实调了裁判", names == ["emit_verdict", "emit_judgement"], names)


def test_answer_judge_not_consulted_without_answer():
    print("[无谜底时不问裁判]")
    fc = FakeClient([
        LLMResult(tool_input={"answers": [{"id": 1, "verdict": "是"}]}),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    res, _ = w.answer("谜面", "", [], 1, "甲", "问题")   # answer 为空
    check("仍能裁决", res and res[0].verdict == "是", res)
    check("没多调裁判", len(fc.calls) == 1, [c["tool"]["name"] for c in fc.calls])


def test_reject_reasons_accumulate():
    print("[出题: 审稿人改不动时, 历次原因要累积传给下一稿]")
    # 审稿人给出改稿时走"就地修改"; 但它**没给改稿**时才退回重出 ——
    # 那时历次原因必须累积, 否则第 1 稿的"靠巧合"到第 3 稿就丢了,
    # 模型又踩回同一个坑(实测)。
    fc = FakeClient([
        # 第 1 稿: 结构不合格(没有 facts) -> 硬校验拒, 原因记下
        LLMResult(tool_input=dict(riddle(puzzle="他每天数鸡蛋。为什么?"),
                                  facts=[])),
        # 第 2 稿: 结构合格但审稿说不行, 且**没给改稿** -> 退回重出
        LLMResult(tool_input=riddle(puzzle="她每天擦那扇窗。为什么?")),
        LLMResult(tool_input={"ok": False, "note": "谜底没解释反常点"}),
        # 第 3 稿: 通过
        LLMResult(tool_input=riddle(puzzle="他每天擦那扇窗。为什么?")),
        LLMResult(tool_input=review_ok("他每天擦那扇窗。为什么?")),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    r = w.gen_riddle(blueprint=fc.default_blueprint)
    check("最终采用第 3 稿", r.puzzle is not None and "擦那扇窗" in r.puzzle, r)
    # 第 3 稿的生成请求(第 5 次调用)里, **两条**原因都该在。
    # 注意: 前两稿必须**真的不合格**(不带 facts 就过不了硬校验),
    # 否则审稿不会被调用, 原因也就累积不起来。
    # 最后一次出题的请求(第 3 稿)里, 前两稿的原因都该在
    gen_reqs = [c["user"] for c in fc.calls
                if c["tool"] and c["tool"]["name"] == "emit_riddle"]
    last_gen = gen_reqs[-1]
    check("带上了第 1 稿的原因(结构问题)", "结构问题" in last_gen,
          last_gen[-600:])
    check("带上了第 2 稿的原因(没解释反常点)", "没解释反常点" in last_gen,
          last_gen[-600:])


def test_rejected_puzzle_goes_into_avoid():
    print("[出题: 被毙掉的谜面要进 avoid, 避免同题材反复]")
    # 实测 bug: 被毙的稿子没进 avoid, 模型只从驳回理由里看到几个关键词,
    # 就顺着那个题材再写一个 —— 直播间连续 4 稿全是沙漠水壶。
    fc = FakeClient([
        # 第 1 稿结构不合格(没 facts) -> 直接被毙, 谜面进 bad_puzzles
        LLMResult(tool_input=dict(riddle(
            puzzle="男人在沙漠中醒来, 身边只有一个空水壶。为什么?"),
            facts=[])),
        LLMResult(tool_input=riddle(puzzle="她每天数楼梯的台阶。为什么?")),
        LLMResult(tool_input=review_ok("她每天数楼梯的台阶。为什么?")),
    ])
    # 调用次数 = 出题(坏) + 重出 + 审稿 = 3
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    r = w.gen_riddle(blueprint=fc.default_blueprint)
    check("第二稿被采用", r.puzzle and "楼梯" in r.puzzle, r)
    gen2 = fc.calls[1]["user"]
    check("第二稿的 prompt 里带了被毙的沙漠题", "沙漠" in gen2, gen2[-400:])


def test_bad_draft_does_not_consume_attempt():
    print("[出题: 废稿(英文/抖动)不消耗重试次数]")
    # 模型偶发吐英文自言自语 —— 那是废稿, 不该算掉一次机会,
    # 否则后面就没机会改了(实测: 3 稿全废然后退兜底)。
    fc = FakeClient([
        LLMResult(text="I'll create a fresh puzzle in objective third-person, avoiding..."),
        LLMResult(text="Let me think about a good puzzle."),
        LLMResult(tool_input=riddle(puzzle="他每天擦那扇窗。为什么?")),
        LLMResult(tool_input=review_ok("他每天擦那扇窗。为什么?")),
    ])
    # 两次英文独白走文本分支 -> 解析不出谜面 -> 不消耗次数
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    r = w.gen_riddle(blueprint=fc.default_blueprint)
    check("两次废稿后仍能出题成功",
          r.puzzle is not None and "擦那扇窗" in r.puzzle, r)
    check("废稿没有消耗重试次数(共 4 次调用)", len(fc.calls) == 4,
          len(fc.calls))


def test_wrapped_tool_input_unwrapped():
    print("[出题: 网关套壳的 tool_input 要剥掉]")
    # 实测 bug: 网关偶尔返回 {"name":"emit_riddle","parameters":{...}},
    # 这时 d.get("puzzle") 是 None, 而 str(d) 含中文能骗过中文检查,
    # 结果**一整段 JSON 被当成谜面上屏**。
    from story.llm import _unwrap_tool_input
    inner = riddle(puzzle="他每天擦那扇窗。为什么?")
    d = _unwrap_tool_input({"name": "emit_riddle", "parameters": inner})
    check("壳被剥掉", d.get("puzzle") == inner["puzzle"], d)
    # JSON 字符串形态的 input 也要能解析
    import json as _json
    d2 = _unwrap_tool_input(_json.dumps(inner))
    check("JSON 字符串形态也能解析", d2.get("puzzle") == inner["puzzle"], d2)
    # 端到端: 套壳稿要能被正常出题
    fc = FakeClient([
        LLMResult(tool_input={"name": "emit_riddle",
                              "parameters": riddle(
                                  puzzle="他每天擦那扇窗。为什么?")}),
        LLMResult(tool_input=review_ok("他每天擦那扇窗。为什么?")),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    r = w.gen_riddle(blueprint=fc.default_blueprint)
    check("套壳的一稿被正确解析",
          r.puzzle is not None and "擦那扇窗" in r.puzzle, r)


def test_hints_not_leaked_into_puzzle():
    print("[出题: 谜面末尾混进的提示要切掉]")
    from story.llm import _looks_meta, _strip_puzzle_tail
    raw = ("他每天擦那扇窗, 一擦就是三十年。为什么? "
           "【谜面附注】谜底揭晓前, 读者可先看提示。")
    check("meta 能被识别", _looks_meta(raw) is True, raw)
    cut = _strip_puzzle_tail(raw)
    check("附注被切掉", "谜面附注" not in cut and cut.endswith("为什么?"), cut)


def test_reviewer_keeps_solve_atoms():
    print("[审稿: 改稿后 solve_atoms / fair_clues 不能丢]")
    # 实测踩过的最隐蔽的坑: 审稿人改稿后重建结果时只复制了
    # puzzle/answer/hints, atoms 被丢掉 —— 于是只要经过一次审稿, engine
    # 拿到的 _solve_atoms 就是空数组, judge 悄悄退回"凭感觉判"。
    # 新机制**看起来生效, 其实没有**。
    P1 = "海角守塔人只在退潮的那几个小时亮灯, 涨潮后反而熄掉。为什么?"
    fc = FakeClient([
        LLMResult(tool_input=riddle()),
        LLMResult(tool_input=review_fix(P1)),
        LLMResult(tool_input=review_ok()),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    r = w.gen_riddle(blueprint=fc.default_blueprint)
    check("改稿被采用", r.puzzle and "海角" in r.puzzle, r)
    check("solve_atoms 经过审稿后仍在",
          [a["id"] for a in r.solve_atoms] == ["a1", "a2"], r.solve_atoms)
    check("fair_clues 经过审稿后仍在", len(r.fair_clues) == 2, r.fair_clues)
    check("改稿后 quote 仍逐字在谜面里",
          all(c["quote"] in r.puzzle for c in r.fair_clues),
          (r.fair_clues, r.puzzle))
    check("审稿请求带上了现有 atoms",
          "退潮使礁石需要标出" in fc.calls[1]["user"],
          fc.calls[1]["user"][-700:])


def test_reviewer_can_replace_atoms_when_answer_changes():
    print("[审稿: 改了谜底就必须重出 atoms]")
    P1 = "海角守塔人只在退潮的那几个小时亮灯, 涨潮后反而熄掉。为什么?"
    fc = FakeClient([
        LLMResult(tool_input=riddle()),
        # 审稿改了谜底, 同时给出**新的** atoms/clues
        LLMResult(tool_input=review_fix(
            P1, answer="换成另一个解释。",
            solve_atoms=[{"id": "a1", "role": "cause", "text": "新因",
                          "fact_ids": ["f1"]},
                         {"id": "a2", "role": "mechanism", "text": "新机制",
                          "fact_ids": ["f2"]}],
            fair_clues=[{"quote": "只在退潮的那几个小时亮灯",
                         "supports_atoms": ["a1"]}])),
        LLMResult(tool_input=review_ok()),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    r = w.gen_riddle(blueprint=fc.default_blueprint)
    check("用了审稿人新给的 atoms",
          [a["text"] for a in r.solve_atoms] == ["新因", "新机制"], r.solve_atoms)
    check("用了审稿人新给的 clues",
          r.fair_clues == [{"quote": "只在退潮的那几个小时亮灯",
                            "supports_atoms": ["a1"]}], r.fair_clues)


def test_main_regression_no_atoms_lost():
    print("[回归: reviewer 改稿后 atoms 仍不为空]")
    # 端到端: 生成带 atoms -> 审稿改题 -> 最终 RiddleResult 仍有 atoms。
    P1 = "海角守塔人只在退潮的那几个小时亮灯, 涨潮后反而熄掉。为什么?"
    fc = FakeClient([
        LLMResult(tool_input=riddle()),
        LLMResult(tool_input=review_fix(P1)),
        LLMResult(tool_input=review_ok()),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    r = w.gen_riddle(blueprint=fc.default_blueprint)
    check("改稿后仍有谜题", r.puzzle is not None, r)
    check("改稿后 atoms 非空", len(r.solve_atoms) == 2, r.solve_atoms)
    check("改稿后 clues 非空", len(r.fair_clues) == 2, r.fair_clues)
    check("atoms 的 role 没退化",
          {a["role"] for a in r.solve_atoms} == {"cause", "mechanism"},
          r.solve_atoms)


def test_atom_role_gate():
    print("[裁判: 说中机制就必须真的命中 mechanism atom]")
    # 只信模型给的 cause_hit/mechanism_hit, 等于把判断权又交回给它。
    # 加上"matched_atoms 必须包含 cause 和 mechanism"才是代码层校验。
    from story.llm import JudgeResult
    ATOMS = [{"role": "cause", "text": "退潮时礁石露出水面"},
             {"role": "mechanism", "text": "亮灯标礁石位置, 涨潮后误导船只"}]
    # ① 模型说都中了, 但 matched_atoms 只命中 support 之外的 0 -> 不通过
    fc = FakeClient([LLMResult(tool_input={
        "is_guess": True, "cause_hit": True, "mechanism_hit": True,
        "matched_atoms": [0]})])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    jr = w.judge("谜面", "谜底", "退潮时礁石露出来", ATOMS)
    check("只命中 cause 不算通关(缺 mechanism)", jr.solved is False,
          f"solved={jr.solved} hit={jr.matched_atoms}")
    # ② 两条都命中 -> 通过
    fc2 = FakeClient([LLMResult(tool_input={
        "is_guess": True, "cause_hit": True, "mechanism_hit": True,
        "matched_atoms": [0, 1]})])
    w2 = PuzzleWriter(client=fc2, runtime_cfg=fc2.runtime_cfg)
    jr2 = w2.judge("谜面", "谜底", "退潮礁石露出, 亮灯标位置, 涨潮误导", ATOMS)
    check("两条都命中 -> 通关", jr2.solved is True, jr2)
    # ③ 没有 role 的老数据 -> 不做 atom 校验, 不误杀
    fc3 = FakeClient([LLMResult(tool_input={
        "is_guess": True, "cause_hit": True, "mechanism_hit": True,
        "matched_atoms": []})])
    w3 = PuzzleWriter(client=fc3, runtime_cfg=fc3.runtime_cfg)
    jr3 = w3.judge("谜面", "谜底", "说清了", ["纯字符串1", "纯字符串2"])
    check("老格式(无 role)不误杀", jr3.solved is True, jr3)


# ======================================================================
# Step 08 — Stable atom IDs
# ======================================================================
def test_s08_judge_accepts_atom_ids():
    """Step 08: 命中项用 **id** 回传, 而不是序号。"""
    print("\n[S08-1] Judge 接受 atom id")
    ATOMS = [{"id": "a1", "role": "cause", "text": "退潮时礁石露出水面"},
             {"id": "a2", "role": "mechanism", "text": "亮灯标礁石位置"}]
    fc = FakeClient([LLMResult(tool_input={
        "is_guess": True, "cause_hit": True, "mechanism_hit": True,
        "matched_atoms": ["a1", "a2"]})])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    jr = w.judge("谜面", "谜底", "退潮礁石露出, 亮灯标位置", ATOMS)
    check("id 命中 -> 通关", jr.solved is True, jr)
    check("matched_atoms 保留 id 形式",
          "a1" in jr.matched_atoms and "a2" in jr.matched_atoms,
          jr.matched_atoms)


def test_s08_judge_prompt_shows_atom_ids():
    """Judge prompt 里必须能看到 atom id, 否则模型无从回传。"""
    print("\n[S08-2] Judge prompt 展示 atom id")
    ATOMS = [{"id": "a1", "role": "cause", "text": "退潮时礁石露出水面"},
             {"id": "a2", "role": "mechanism", "text": "亮灯标礁石位置"}]
    fc = FakeClient([LLMResult(tool_input={
        "is_guess": True, "cause_hit": False, "mechanism_hit": False,
        "matched_atoms": []})])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    w.judge("谜面", "谜底", "x", ATOMS)
    user = fc.calls[0]["user"]
    check("prompt 里有 a1", "a1." in user or "a1 " in user, user[:300])
    check("prompt 里有 a2", "a2." in user or "a2 " in user, user[:300])
    check("不再说『编号从 0 开始』", "编号从 0 开始" not in user, user[:300])


def test_s08_legacy_index_still_readable():
    """老 archive / 老 fixture 存的是**序号** -> 仍要能读懂。"""
    print("\n[S08-3] 老格式(整数序号)兼容读取")
    ATOMS = [{"id": "a1", "role": "cause", "text": "原因"},
             {"id": "a2", "role": "mechanism", "text": "机制"}]
    fc = FakeClient([LLMResult(tool_input={
        "is_guess": True, "cause_hit": True, "mechanism_hit": True,
        "matched_atoms": [0, 1]})])          # 老格式: 序号
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    jr = w.judge("谜面", "谜底", "x", ATOMS)
    check("老序号仍能通关", jr.solved is True, jr)


def test_s08_unknown_atom_id_is_dropped():
    """回传了不存在的 id -> 丢弃(不猜), 且不因此通关。"""
    print("\n[S08-4] 不存在的 atom id 被丢弃")
    ATOMS = [{"id": "a1", "role": "cause", "text": "原因"},
             {"id": "a2", "role": "mechanism", "text": "机制"}]
    fc = FakeClient([LLMResult(tool_input={
        "is_guess": True, "cause_hit": True, "mechanism_hit": True,
        "matched_atoms": ["a1", "a99"]})])   # a99 不存在
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    jr = w.judge("谜面", "谜底", "x", ATOMS)
    check("a99 被丢弃", "a99" not in jr.matched_atoms, jr.matched_atoms)
    check("缺 mechanism -> 不通关(代码层 gate 仍生效)",
          jr.solved is False, jr)


def test_s08_index_is_not_stable_across_reorder():
    """**为什么必须用 id**: 序号是位置, 重排后就指向别的 atom。

    这条是 Step 08 的动机本身: 同一句观众猜测, 在 atoms 被审稿人重排
    之后, 用序号会解析成**另一条** atom。
    """
    print("\n[S08-5] 序号会漂移, id 不会")
    before = [{"id": "a1", "role": "cause", "text": "原因"},
              {"id": "a2", "role": "mechanism", "text": "机制"}]
    after = [{"id": "a2", "role": "mechanism", "text": "机制"},
             {"id": "a1", "role": "cause", "text": "原因"}]   # 重排
    ti = {"is_guess": True, "cause_hit": True, "mechanism_hit": True,
          "matched_atoms": ["a1", "a2"]}
    fc = FakeClient([LLMResult(tool_input=ti)])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    check("重排前 id 命中 -> 通关", w.judge("谜面", "谜底", "x", before).solved)
    fc2 = FakeClient([LLMResult(tool_input=ti)])
    w2 = PuzzleWriter(client=fc2, runtime_cfg=fc2.runtime_cfg)
    check("**重排后同一条 id 仍通关**(序号做不到这点)",
          w2.judge("谜面", "谜底", "x", after).solved)


def test_bco_legacy_index_normalized_to_id_on_output():
    """**Batch B closeout**: 老序号输入必须**立即归一成 id** 再写出去。

    只做"读得懂"是不够的: 若结果里仍保留整数 0/1, `JudgeResult ->
    QAResult -> archive` 就继续产出老格式, 迁移永远收不了口, 新直播也
    一直在写混合类型数据。
    """
    print("\n[BCO-2] 老序号输出归一成 id")
    ATOMS = [{"id": "a1", "role": "cause", "text": "原因"},
             {"id": "a2", "role": "mechanism", "text": "机制"}]
    fc = FakeClient([LLMResult(tool_input={
        "is_guess": True, "cause_hit": True, "mechanism_hit": True,
        "matched_atoms": [0, 1]})])          # 老格式输入
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    jr = w.judge("谜面", "谜底", "x", ATOMS)
    check("结果里是 id 而不是序号",
          jr.matched_atoms == ["a1", "a2"], jr.matched_atoms)
    check("全部是字符串", all(isinstance(x, str) for x in jr.matched_atoms),
          jr.matched_atoms)


def test_bco_legacy_index_kept_only_when_atoms_lack_ids():
    """只有在 legacy atoms **本身没有 id** 时才保留序号(不得已)。"""
    print("\n[BCO-3] 无 id 的 legacy atoms 才保留序号")
    ATOMS = [{"role": "cause", "text": "原因"},
             {"role": "mechanism", "text": "机制"}]      # 没有 id
    fc = FakeClient([LLMResult(tool_input={
        "is_guess": True, "cause_hit": True, "mechanism_hit": True,
        "matched_atoms": [0, 1]})])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    jr = w.judge("谜面", "谜底", "x", ATOMS)
    check("保留序号(没有 id 可归一)", jr.matched_atoms == [0, 1],
          jr.matched_atoms)
    check("仍能通关", jr.solved is True, jr)


def test_bco_judge_text_fallback_cannot_solve():
    """**Batch B closeout**: 没有结构化 `emit_judgement` -> **不得通关**。

    老兜底: 没有 tool_input 但 `res.text` 非空时, 只要文本里有"是"就
    `solved=True`。那条路绕过了 canonical facts、matched atom id、以及
    cause+mechanism 的代码一致性门 —— 等于把通关权又还给了自由文本。
    """
    print("\n[BCO-4] Judge 自由文本兜底不得通关")
    for txt in ("是", "是的，猜中了", "对"):
        fc = FakeClient([LLMResult(text=txt)])
        w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
        jr = w.judge("谜面", "谜底", "退潮礁石露出",
                     [{"id": "a1", "role": "cause", "text": "退潮礁石露出"},
                      {"id": "a2", "role": "mechanism", "text": "灯标礁石"}])
        check(f"文本 {txt!r} -> 不通关", jr.solved is False,
              f"solved={jr.solved}")
        check(f"文本 {txt!r} -> 标记为技术失败", jr.failed is True, jr)


def test_bco_empty_tool_input_still_fails_closed():
    """回归: 空 tool_input 一直就是技术失败(别被上面的改动带坏)。"""
    print("\n[BCO-5] 空 tool_input 仍是技术失败")
    fc = FakeClient([LLMResult(tool_input={})])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    jr = w.judge("谜面", "谜底", "x", [{"id": "a1", "role": "cause", "text": "c"}])
    check("不通关", jr.solved is False, jr)
    check("标记技术失败", jr.failed is True, jr)


def test_judge_technical_failure_not_downgraded_to_irrelevant():
    print("[Q5: 裁判技术失败不能伪装成'无关', 也不抹掉第一层裁决]")
    # 第一层判"是" + candidate=true, 但裁判调用技术失败。
    # **不能**把它降成"无关"(那是断言"你的猜测无关", 是错误信息),
    # 也不能抹掉第一层的"是" —— 题目继续。
    fc = FakeClient([
        LLMResult(tool_input={"answers": [
            {"id": 1, "verdict": "是", "solution_candidate": True}]}),
        LLMResult(error="网关抖动"),          # 裁判失败: 无 tool_input 无 text
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    res, _ = w.answer("谜面", "谜底", [], 1, "甲", "同伴的肉对吧")
    check("保留第一层的'是'", res and res[0].verdict == "是", res)
    check("没有伪装成'无关'", res and res[0].verdict != "无关", res)
    # 第一层没给出可用裁决(candidate=true 但 verdict 缺失) -> 才降级
    fc2 = FakeClient([
        LLMResult(tool_input={"answers": [
            {"id": 2, "verdict": "", "solution_candidate": True}]}),
        LLMResult(error="网关抖动"),
    ])
    w2 = PuzzleWriter(client=fc2, runtime_cfg=fc2.runtime_cfg)
    res2, _ = w2.answer("谜面", "谜底", [], 2, "乙", "他是盲人吗")
    check("第一层也无裁决时才降级", res2 == [], res2)


def test_check_tool_schema_matches_generator():
    """Q0.1: 审稿工具和出题工具的 solve_atoms **必须是同一种结构**。

    以前 _TOOL_CHECK 是 string[], _TOOL_RIDDLE 是 {role,text} 对象 ——
    审稿人一旦回传 atoms, role 信息就退化了, 而链路上看不出来。
    """
    from story.llm import _TOOL_CHECK, _TOOL_RIDDLE
    a = _TOOL_CHECK["input_schema"]["properties"]["solve_atoms"]["items"]
    b = _TOOL_RIDDLE["input_schema"]["properties"]["solve_atoms"]["items"]
    a = {"items": a, "props": a["properties"], "req": a["required"]}
    b = {"items": b, "props": b["properties"], "req": b["required"]}
    check("两边 items 类型一致", a["items"]["type"] == b["items"]["type"],
          (a["items"].get("type"), b["items"].get("type")))
    check("都是 object(带 role/text)", a["items"]["type"] == "object", a["items"])
    check("role 枚举一致",
          a["props"]["role"]["enum"] == b["props"]["role"]["enum"], a["props"])
    # required 有意**不同**: 出题必须给全(id/fact_ids), 审稿只需回
    # role+text(可选字段它可能不带), 缺的由 _norm_atoms 兜。形状必须一致。
    check("审稿的 required 是出题的子集",
          set(a["req"]) <= set(b["req"]), (a["req"], b["req"]))
    check("role+text 都在两边 required 里",
          {"role", "text"} <= set(a["req"]) and {"role", "text"} <= set(b["req"]),
          (a["req"], b["req"]))
    # 审稿人也必须能带回 id/fact_ids, 否则审一次就丢一次
    check("审稿 schema 有 id", "id" in a["props"], sorted(a["props"]))
    check("审稿 schema 有 fact_ids", "fact_ids" in a["props"], sorted(a["props"]))


def test_reviewer_structured_atoms_survive():
    """Q0.2: 审稿人回传结构化 atoms 时, **不能**被 str(dict) 损坏。

    以前 _check_riddle 里是 `str(a).strip()`, 对 {"role":...} 会变成
    "{'role': 'cause', 'text': '...'}" 那种字符串 —— 隐式数据损坏。
    """
    ATOMS = [{"id": "a1", "role": "cause", "text": "退潮时礁石露出水面",
              "fact_ids": ["f1"]},
             {"id": "a2", "role": "mechanism", "text": "亮灯是标出礁石位置",
              "fact_ids": ["f2"]}]
    P1 = "海角守塔人只在退潮的那几个小时亮灯, 涨潮后反而熄掉。为什么?"
    fc = FakeClient([
        LLMResult(tool_input=riddle()),
        # 审稿改了谜面, 原样带回**结构化** atoms(含 id/fact_ids)
        LLMResult(tool_input=review_fix(P1, solve_atoms=ATOMS)),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    r = w.gen_riddle(blueprint=fc.default_blueprint)
    # _spec_to_riddle 会补一个 required=True —— 只比关键字段
    key = [{k: a.get(k) for k in ("id", "role", "text", "fact_ids")}
           for a in r.solve_atoms]
    check("atoms 仍是结构化对象", key == ATOMS, r.solve_atoms)
    check("没有被 str() 损坏",
          all(isinstance(a, dict) for a in r.solve_atoms), r.solve_atoms)
    check("审稿请求展示 role",
          "[cause]" in fc.calls[1]["user"], fc.calls[1]["user"][-700:])
    check("审稿请求带上 atom id", "id=a1" in fc.calls[1]["user"],
          fc.calls[1]["user"][-700:])


# ======================================================================
# Step 04 — Riddle / Reviewer v4
# ======================================================================
def test_v4_prompt_versions_bumped():
    """Step 04: prompt 版本必须真的升到 v4(否则档案无法区分两代题)。"""
    print("\n[V4-1] riddle/check prompt 版本")
    from story.llm import CHECK_PROMPT_VERSION, RIDDLE_PROMPT_VERSION
    check("RIDDLE_PROMPT_VERSION == riddle-v6",
          RIDDLE_PROMPT_VERSION == "riddle-v6", RIDDLE_PROMPT_VERSION)
    check("CHECK_PROMPT_VERSION == check-v6",
          CHECK_PROMPT_VERSION == "check-v6", CHECK_PROMPT_VERSION)


def test_v4_signature_schema_has_new_dimensions():
    """Step 04: 两个新 observed 维度必须进**两个**工具的 schema。

    只在出题侧加 = Reviewer 回传不了; 只在审稿侧加 = 生成器报不了。
    两边都要有, 且枚举/类型一致。
    """
    print("\n[V4-2] riddle + check 的 signature schema 都带新维度")
    from story.llm import _TOOL_CHECK, _TOOL_RIDDLE
    from story.puzzle import REVEAL_MODES
    rs = _TOOL_RIDDLE["input_schema"]["properties"]["signature"]
    cs = _TOOL_CHECK["input_schema"]["properties"]["observed_signature"]
    for name, sch in (("riddle", rs), ("check", cs)):
        props = sch["properties"]
        check(f"{name}: 有 reveal_mode", "reveal_mode" in props, sorted(props))
        check(f"{name}: reveal_mode 枚举 == REVEAL_MODES",
              props["reveal_mode"]["enum"] == list(REVEAL_MODES))
        check(f"{name}: 有 procedural_rule_dependency",
              "procedural_rule_dependency" in props, sorted(props))
        check(f"{name}: procedural 是 boolean",
              props["procedural_rule_dependency"]["type"] == "boolean")
    check("riddle: 两个新字段都在 required 里",
          {"reveal_mode", "procedural_rule_dependency"}
          <= set(rs["required"]), rs["required"])


def test_v4_check_system_freezes_reviewer_scope():
    """Step 04 冻结的职责边界必须写进 Prompt —— 否则模型会去兼管全局配额。"""
    print("\n[V4-3] CHECK_SYSTEM 冻结 Reviewer 职责")
    from story.llm import CHECK_SYSTEM
    check("明说只看这一道题", "只看这一道题" in CHECK_SYSTEM)
    check("明说看不到别的题", "看不到" in CHECK_SYSTEM)
    check("明说不读最近窗口配额",
          "最近" in CHECK_SYSTEM and ("配额" in CHECK_SYSTEM))
    check("明说全局配额归代码层",
          "代码层" in CHECK_SYSTEM or "代码" in CHECK_SYSTEM)
    check("点明 emotion/reveal 正交", "正交" in CHECK_SYSTEM)
    # 五项 v4 语义检查
    for kw in ("时间线", "身份", "动作", "线索", "隐藏规则"):
        check(f"含 v4 检查项: {kw}", kw in CHECK_SYSTEM)
    check("提到 reveal_mode adherence",
          "adherence" in CHECK_SYSTEM, "adherence")


def test_v4_riddle_system_states_orthogonality():
    """Step 04: 出题端也要知道 emotion/reveal 正交 + 隐藏规则非默认。"""
    print("\n[V4-4] RIDDLE_SYSTEM 说明正交与规则依赖")
    from story.llm import RIDDLE_SYSTEM
    check("点明两条轴正交", "正交" in RIDDLE_SYSTEM)
    check("列出 reveal_mode 取值", "recontextualization" in RIDDLE_SYSTEM)
    check("提醒不要默认 straight_explanation",
          "straight_explanation" in RIDDLE_SYSTEM
          and "默认" in RIDDLE_SYSTEM)
    check("提醒隐藏规则不是默认解法",
          "隐藏规则" in RIDDLE_SYSTEM or "隐藏规章" in RIDDLE_SYSTEM)


def test_v4_observed_fields_flow_to_signature():
    """Step 04: Reviewer 回传的两个新字段必须真的落到 spec.signature。

    这是端到端的: Prompt/schema 只是一半, 数据必须能穿过去。
    """
    print("\n[V4-5] 新字段端到端落到 signature")
    fc = FakeClient([LLMResult(tool_input=riddle()),
                     LLMResult(tool_input=review_ok())])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    spec = w.gen_spec(blueprint=fc.default_blueprint)
    check("reveal_mode 落到 signature",
          spec.signature.reveal_mode == "meaning_flip",
          spec.signature.reveal_mode)
    check("procedural_rule_dependency 落到 signature",
          spec.signature.procedural_rule_dependency is False,
          spec.signature.procedural_rule_dependency)
    # 再经 archive 往返一次, 不许丢
    from story.puzzle import PuzzleSpec as _PS
    back = _PS.from_dict(spec.to_archive())
    check("archive 往返后 reveal_mode 仍在",
          back.signature.reveal_mode == "meaning_flip",
          back.signature.reveal_mode)
    check("archive 往返后 procedural 仍在",
          back.signature.procedural_rule_dependency is False)


def test_v4_reviewer_observed_reveal_wins_over_generator():
    """Step 04: 审稿人的 observed 值优先于生成器自报 —— 配额靠它。

    生成器说 straight_explanation, 审稿人读完说是 identity_flip:
    最终 signature 必须是审稿人的判断(它是读过成品的人)。
    """
    print("\n[V4-6] 审稿人的 reveal_mode 覆盖生成器自报")
    gen_sig = {"mechanism_family": "hidden_function",
               "solution_shape": "hidden_function_explains_behavior",
               "domain": "maritime", "relation": "stranger",
               "emotion_mode": "neutral", "time_shape": "instant",
               "death": False, "past_trauma": False,
               "long_term_profession": False, "repeated_ritual": False,
               "reveal_mode": "straight_explanation",       # 生成器自报
               "procedural_rule_dependency": False}
    obs = dict(gen_sig)
    obs["reveal_mode"] = "identity_flip"                 # 审稿人观察
    obs["procedural_rule_dependency"] = True
    fc = FakeClient([
        LLMResult(tool_input=riddle(signature=gen_sig)),
        LLMResult(tool_input=review_ok(observed_signature=obs)),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    spec = w.gen_spec(blueprint=fc.default_blueprint)
    check("reveal_mode 采用审稿人的 identity_flip",
          spec.signature.reveal_mode == "identity_flip",
          spec.signature.reveal_mode)
    check("procedural_rule_dependency 采用审稿人的 True",
          spec.signature.procedural_rule_dependency is True,
          spec.signature.procedural_rule_dependency)


def test_v4_reviewer_cannot_see_recent_quota():
    """Step 04 硬边界: 审稿请求里**不得**出现最近窗口的配额状态。

    Reviewer 没有完整的最近题窗口, 所以全局配额导演权不能回到 LLM。
    这条从**请求体**上验证 —— 只断言 Prompt 措辞是不够的。
    """
    print("\n[V4-7] 审稿请求不带最近配额")
    recent = [{"mechanism_family": "hidden_function",
               "solution_shape": "hidden_function_explains_behavior"}] * 3
    fc = FakeClient([LLMResult(tool_input=riddle()),
                     LLMResult(tool_input=review_ok())])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    w.gen_spec(blueprint=fc.default_blueprint, recent=recent)
    chk = fc.calls[1]
    check("审稿请求里没有 recent_signatures 块",
          "recent_signatures" not in chk["user"], chk["user"][-400:])
    check("审稿请求里没有『最近 N 题』配额计数",
          "已出现" not in chk["user"] and "配额" not in chk["user"],
          chk["user"][-400:])


def test_v4_apply_review_keeps_new_fields_on_fix():
    """Step 04: fix 路径也要保住两个新字段(不能只在 pass 路径生效)。"""
    print("\n[V4-8] fix 路径保住新字段")
    P2 = "守塔人只在退潮时亮灯, 涨潮后熄灭。为什么?"
    obs = sig_ok()
    obs["reveal_mode"] = "goal_flip"
    obs["procedural_rule_dependency"] = True
    fc = FakeClient([
        LLMResult(tool_input=riddle()),
        LLMResult(tool_input=review_fix(P2, observed_signature=obs)),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    spec = w.gen_spec(blueprint=fc.default_blueprint)
    check("fix 后谜面已改", spec.puzzle == P2, spec.puzzle)
    check("fix 后 reveal_mode 是审稿人的 goal_flip",
          spec.signature.reveal_mode == "goal_flip", spec.signature.reveal_mode)
    check("fix 后 procedural 是审稿人的 True",
          spec.signature.procedural_rule_dependency is True,
          spec.signature.procedural_rule_dependency)


def test_v4_policy_version_is_v4():
    """Step 04: 内容政策必须 bump —— 否则 Step 03 的隔离不会发生。"""
    print("\n[V4-9] QUALITY_POLICY_VERSION bump 到 v4")
    from story.quality import QUALITY_POLICY_VERSION
    check("当前政策是 quality-v6",
          QUALITY_POLICY_VERSION == "quality-v6", QUALITY_POLICY_VERSION)


# ======================================================================
# Batch A closeout — Reviewer 职责 / observed_signature fail closed
# ======================================================================
def test_closeout_check_tool_has_no_recent_window_rule():
    """Blocker 1: **tool schema 也是给模型的指令**。

    `CHECK_SYSTEM` 里冻结了"不因最近题分布 rewrite", 但 `_TOOL_CHECK`
    的 decision description 里若还留着"机制与最近题高度重复"这种**条件**,
    模型收到的是两条互相矛盾的指令 —— 全局调度权从后门回到了 LLM。

    只查 user prompt 抓不到这条: 它藏在 tool schema 里。

    ⚠️ 断言的是"没有任何**要求**跨题判断的措辞", **不是**"不许出现'最近'
    这两个字" —— 明确**禁止**跨题判断的句子(如"不要因为最近几题都是这种
    而 rewrite")是**好的**, 必须允许存在, 否则这条测试会逼着人删掉正确的
    护栏。所以这里查的是那几个具体的**条件短语**。
    """
    print("\n[CO-1] _TOOL_CHECK 不含『跨题判断』条件")
    import json as _json
    from story.llm import _TOOL_CHECK
    props = _TOOL_CHECK["input_schema"]["properties"]
    dec = props["decision"]["description"]
    # 这些是"要求模型去看最近题"的条件措辞 —— 必须消失
    for bad in ("机制与最近题高度重复", "与最近题高度重复",
                "最近题已经太多", "最近窗口内重复"):
        check(f"decision 不含条件 {bad!r}", bad not in dec, bad)
    # 整个 schema 里不该出现任何"配额/窗口计数"式的判断条件
    blob = _json.dumps(_TOOL_CHECK, ensure_ascii=False)
    for bad in ("已出现", "配额已满", "超过上限"):
        check(f"tool schema 不含计数条件 {bad!r}", bad not in blob, bad)
    # 必须**显式**把跨题判断推给代码层
    check("decision 显式把跨题判断推给代码层",
          "代码层" in dec or "cross_puzzle_gate" in dec, dec[-160:])


def test_closeout_observed_signature_schema_is_complete():
    """Blocker 2 第一层: nested required 必须覆盖**全部** observed 字段。

    顶层 required 只保证 key 存在; 缺 nested required 时模型能只回
    mechanism_family + solution_shape, 于是 reveal/procedural 被
    `from_dict` 静默补默认值, v4 配额被绕过。
    """
    print("\n[CO-2] observed_signature 的 nested required 完整")
    from story.llm import _OBSERVED_SIGNATURE_FIELDS, _TOOL_CHECK
    sch = _TOOL_CHECK["input_schema"]["properties"]["observed_signature"]
    req = set(sch.get("required") or [])
    check("observed_signature 有 nested required", bool(req), req)
    check("nested required == 契约字段表",
          req == set(_OBSERVED_SIGNATURE_FIELDS),
          (sorted(req), sorted(_OBSERVED_SIGNATURE_FIELDS)))
    check("nested required 全是已声明的 properties",
          req <= set(sch["properties"]), sorted(req - set(sch["properties"])))
    for must in ("reveal_mode", "procedural_rule_dependency"):
        check(f"{must} 在 nested required 里", must in req, sorted(req))


def _obs_without(drop: str) -> dict:
    """`sig_ok()` 去掉某一个字段 —— 模拟 Reviewer 只回了半套。"""
    d = sig_ok()
    d.pop(drop, None)
    return d


def test_closeout_incomplete_observed_signature_is_rejected():
    """Blocker 2 第二层(代码): 不完整的 observed_signature **整稿拒**。

    不能只靠 JSON schema —— 正确性不能押在模型遵守 schema 上。
    Reviewer pass 但只回一半字段时, 若采用, `from_dict` 会把
    `reveal_mode` 补成 `""`(不进任何 reveal 桶)、`procedural_rule_dependency`
    补成 `False`(自动算不依赖规则) —— 两条 v4 配额同时被静默绕过。

    验法: 让第 1 稿的审稿回**半套**, 然后看第 2 次出题的请求里有没有
    把"observed_signature 缺字段"作为原因带回去(说明第 1 稿被拒了)。
    """
    print("\n[CO-3] 不完整的 observed_signature 不能采用")
    for drop in ("reveal_mode", "procedural_rule_dependency",
                 "emotion_mode", "domain"):
        P2 = "守塔人只在退潮时亮灯。为什么?"
        fc = FakeClient([
            LLMResult(tool_input=riddle()),                       # 第 1 稿
            LLMResult(tool_input=review_ok(                        # 半套观察
                observed_signature=_obs_without(drop))),
            LLMResult(tool_input=riddle(puzzle=P2)),              # 第 2 稿
            LLMResult(tool_input=review_ok(P2)),                  # 完整
        ])
        w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
        w.gen_spec(blueprint=fc.default_blueprint)
        gen_reqs = [c["user"] for c in fc.calls
                    if c["tool"] and c["tool"]["name"] == "emit_riddle"]
        check(f"缺 {drop}: 第 1 稿被拒并把原因带回下一稿",
              len(gen_reqs) >= 2 and "observed_signature 缺字段" in gen_reqs[-1],
              gen_reqs[-1][-200:] if gen_reqs else "(无第 2 稿)")


def test_closeout_incomplete_obs_never_lands_in_signature():
    """半套 observed_signature **绝不能**成为最终 signature。

    这是上一条的**结论断言**(不只看拒绝原因): 缺 reveal_mode 时,
    最终 spec 的 reveal_mode **不能**是 `from_dict` 补出来的 `""`
    当成"观察过" —— 它要么来自完整观察, 要么这稿根本没被采用。
    """
    print("\n[CO-3b] 半套观察值不会变成最终 signature")
    fc = FakeClient([
        LLMResult(tool_input=riddle()),
        LLMResult(tool_input=review_ok(
            observed_signature=_obs_without("reveal_mode"))),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    spec = w.gen_spec(blueprint=fc.default_blueprint)
    # 稿被拒 -> 走到兜底题; 兜底题不该带 meaning_flip(那是审稿人的观察)
    check("没有采用那半套观察值",
          spec.signature.reveal_mode != "meaning_flip",
          spec.signature.reveal_mode)


def test_closeout_absent_observed_signature_on_pass_is_rejected():
    """**Batch B closeout**: 整个 `observed_signature` 缺失 + pass -> 拒。

    这是第二层防线上的一个洞: 判据曾经是
    `obs_missing and (changed or has_obs)`, 于是"整个 key 都没回 + 谜面
    谜底也没改"会落到 `else`, 直接沿用 `spec.signature` —— 又变回了
    **相信生成器自报值**(P0-2 修掉的那个"自己验自己")。

    危害: v4 两条 observed 配额(reveal / procedural)完全依赖这个值。
    Reviewer 只要不提 observed_signature, 代码就会拿生成器自报的指纹
    登记进最近窗口, 配额统计被污染且**看不出来**。
    """
    print("\n[BCO-1] 整个 observed_signature 缺失 + pass -> 拒")
    r = riddle()
    r.pop("observed_signature", None)     # 整个 key 都没回
    r["decision"] = "pass"
    # 谜面谜底**不改**(这正是老判据漏掉的那种组合)
    fc = FakeClient([LLMResult(tool_input=riddle()),
                     LLMResult(tool_input=r),
                     LLMResult(tool_input=riddle(puzzle="补出的一稿。为什么?")),
                     LLMResult(tool_input=review_ok("补出的一稿。为什么?"))])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    w.gen_spec(blueprint=fc.default_blueprint)
    gen_reqs = [c["user"] for c in fc.calls
                if c["tool"] and c["tool"]["name"] == "emit_riddle"]
    check("第 1 稿被拒并把原因带回下一稿",
          len(gen_reqs) >= 2 and "observed_signature 缺字段" in gen_reqs[-1],
          gen_reqs[-1][-200:] if gen_reqs else "(无第 2 稿)")


def test_closeout_complete_observed_signature_is_accepted():
    """回归: 完整的 observed_signature 照常采用(别把 fail closed 做过头)。"""
    print("\n[CO-4] 完整的 observed_signature 正常采用")
    fc = FakeClient([LLMResult(tool_input=riddle()),
                     LLMResult(tool_input=review_ok())])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    spec = w.gen_spec(blueprint=fc.default_blueprint)
    check("采用了审稿人的 reveal_mode",
          spec.signature.reveal_mode == "meaning_flip", spec.signature.reveal_mode)
    check("采用了审稿人的 procedural",
          spec.signature.procedural_rule_dependency is False,
          spec.signature.procedural_rule_dependency)


def test_rejected_spec_never_returned():
    """P0-1: 所有稿都被拒时, **不能**返回带 puzzle 的兜底稿。

    这是最危险的一条路径: 早先 `return last`, 而 last 带着 puzzle/answer
    和一个 error 字符串; director 只看 `spec.puzzle` 非空就上直播 ——
    于是"连续几稿都因跨题重复被拒"的最后一稿会照常播出,
    跨题去重等于形同虚设。

    质量系统明确拒绝的题, 绝不能反过来变成兜底。
    """
    bad = dict(riddle(), facts=[])          # 结构不合格 -> 每稿都被拒
    fc = FakeClient([
        LLMResult(tool_input=dict(bad)),
        LLMResult(tool_input=dict(bad, puzzle="二稿。为什么?")),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    spec = w.gen_spec(blueprint=fc.default_blueprint, max_attempts=2)
    check("失败时不返回 puzzle", not spec.puzzle, spec.puzzle)
    check("失败时 error 非空", bool(spec.error), spec.error)
    check("失败时也不返回 answer", not spec.answer, spec.answer)


def test_rejected_by_cross_puzzle_gate_not_returned():
    """P0-1: 被**跨题门**拒掉的稿子同样不能兜底播出。

    这条最容易被忽略 —— 题目本身完全合格, 只是"和最近题结构重复"。
    早先这种稿会作为 last 被返回, 于是配额形同虚设。
    """
    from story.puzzle import PuzzleSignature
    dup = PuzzleSignature(mechanism_family="hidden_function",
                          solution_shape="hidden_function_explains_behavior",
                          domain="maritime", relation="stranger",
                          emotion_mode="neutral", time_shape="instant")
    fc = FakeClient([
        LLMResult(tool_input=riddle()),
        LLMResult(tool_input=review_ok()),
        LLMResult(tool_input=riddle(puzzle="二稿灯塔。为什么?")),
        LLMResult(tool_input=review_ok("二稿灯塔。为什么?")),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    # 最近两道都是同一 (mechanism, shape) -> 配额已满
    spec = w.gen_spec(blueprint=fc.default_blueprint, recent=[dup, dup],
                      max_attempts=2)
    check("跨题重复的稿不返回", not spec.puzzle, spec.puzzle)
    check("带出跨题原因", "重复" in (spec.error or ""), spec.error)


def test_director_only_airs_clean_spec():
    """P0-1: director 必须用 error 判断, 不能只看 puzzle 是否为空。"""
    import inspect
    import director as D
    src = inspect.getsource(D.Director._riddle)
    check("director 检查了 spec.error", "spec.error" in src, src)
    check("不再只判断 puzzle", "if spec.puzzle else None" not in src, src)


def test_fixable_format_goes_to_reviewer_not_rejected():
    """第一人称/没问句是**审稿人能改的**, 不能直接毙掉整题。

    实测踩过: 硬校验把"第一人称"当成结构性错误直接拒, 于是审稿人
    根本没机会改它 —— 一道只差一个人称的好题被丢掉, 而且循环会一直
    重出直到次数耗尽。
    """
    from story.quality import validate_spec
    from story.llm import _spec_from_tool
    # validate_spec: 人称/问句/meta 归 fixable, 不 ok=False
    spec = _spec_from_tool(riddle(puzzle="我每晚都在数楼上的脚步声。"))
    r = validate_spec(spec)
    check("没问句不算结构失败", r.ok, r.errors)
    check("但记进了 fixable", any("问句" in f for f in r.fixable), r.fixable)
    check("must_fix() 能转成文本", bool(r.must_fix()), r.must_fix())
    # 端到端: 第一人称那稿交给审稿人 -> 2 次调用就成功
    P0 = "我每晚都听见楼上有人走动, 可楼上根本没人住。为什么?"
    P1 = "他每晚都听见楼上有人走动, 可楼上根本没人住。为什么?"
    fc = FakeClient([
        LLMResult(tool_input=riddle(puzzle=P0)),
        LLMResult(tool_input=review_fix(P1, note="改成第三人称")),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    r2 = w.gen_riddle(blueprint=fc.default_blueprint)
    check("第一人称稿被审稿人改好并采用",
          r2.puzzle and r2.puzzle.startswith("他每晚"), r2)
    check("只用了 2 次调用(出题 + 审稿)", len(fc.calls) == 2, len(fc.calls))
    check("审稿请求点名了人称问题",
          "第一人称" in fc.calls[1]["user"], fc.calls[1]["user"][-400:])


def test_unfixed_format_still_rejected():
    """审稿人**没改掉**人称时不能放行 —— 否则第一人称会溜到直播上。"""
    P0 = "我每晚都听见楼上有人走动, 可楼上根本没人住。为什么?"
    fc = FakeClient([
        LLMResult(tool_input=riddle(puzzle=P0)),
        # 审稿"通过"了, 但谜面还是第一人称(没真改)
        LLMResult(tool_input=review_ok(P0)),
        LLMResult(tool_input=riddle(puzzle="他每天数楼梯台阶。为什么?")),
        LLMResult(tool_input=review_ok("他每天数楼梯台阶。为什么?")),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    r = w.gen_riddle(blueprint=fc.default_blueprint)
    check("没改人称的稿被拒, 最终采用第三人称的",
          r.puzzle and not r.puzzle.startswith("我"), r)


def test_writer_reads_temperature_from_runtime_cfg():
    """P0-8: temperature 必须从 **runtime Config** 读, 不是 client.cfg。

    这类 bug 的特征是"测试全绿但生产失效": `client.cfg` 是 LLMConfig,
    没有 temperature 字段, `getattr(..., None)` 恒为 None, 参数静默不生效。
    早先测试里的 Fake 替身恰好什么字段都有, 于是完全掩盖了它。
    """
    fc = FakeClient([LLMResult(tool_input=riddle()),
                     LLMResult(tool_input=review_ok())])
    w = PuzzleWriter(client=fc, runtime_cfg=runtime_cfg(generate_temperature=0.9,
                                                       review_temperature=0.1))
    w.gen_riddle(blueprint=fc.default_blueprint)
    check("出题用 runtime_cfg 的 generate_temperature",
          fc.calls[0]["temperature"] == 0.9, fc.calls[0]["temperature"])
    check("审稿用 runtime_cfg 的 review_temperature",
          fc.calls[1]["temperature"] == 0.1, fc.calls[1]["temperature"])
    # **没配 runtime_cfg** 时必须返回 None 并告警, 绝不静默用 0
    fc2 = FakeClient([LLMResult(tool_input=riddle()),
                      LLMResult(tool_input=review_ok())])
    w2 = PuzzleWriter(client=fc2)          # 不给 runtime_cfg
    w2.gen_riddle(blueprint=fc2.default_blueprint)
    check("缺 runtime_cfg 时不发 temperature",
          fc2.calls[0]["temperature"] is None, fc2.calls[0]["temperature"])


def test_client_cfg_is_not_used_for_temperature():
    """P0-8 反例: 即便 client.cfg 上碰巧有同名字段, 也不能用它。"""
    fc = FakeClient([LLMResult(tool_input=riddle()),
                     LLMResult(tool_input=review_ok())])
    # 给传输层配置硬塞一个 generate_temperature —— 它**不该**被读到
    fc.cfg.generate_temperature = 0.123
    w = PuzzleWriter(client=fc, runtime_cfg=runtime_cfg(generate_temperature=0.7))
    w.gen_riddle(blueprint=fc.default_blueprint)
    check("忽略 client.cfg 上的同名字段",
          fc.calls[0]["temperature"] == 0.7, fc.calls[0]["temperature"])


def test_quotas_read_from_runtime_cfg():
    """P0-9: cross_puzzle_gate 的 quota 也必须来自 runtime Config。

    否则 director 选 blueprint 用真 quota、generator 的 gate 用默认 quota,
    同一题上跑着两套 policy。
    """
    from story.quality import Quotas
    cfg = runtime_cfg(quota_same_mechanism=1, quality_recent_window=4)
    q = Quotas.from_config(cfg)
    check("window 读对", q.window == 4, q.window)
    check("same_mechanism 读对", q.same_mechanism == 1, q.same_mechanism)
    # 经 PuzzleWriter 读到的必须是同一个
    w = PuzzleWriter(client=FakeClient([]), runtime_cfg=cfg)
    q2 = Quotas.from_config(w._cfg())
    check("PuzzleWriter._cfg() 给出同一个 Config",
          q2.window == 4 and q2.same_mechanism == 1, q2)


def test_judge_passes_temperature():
    """P0-8: judge 也必须真的把 judge_temperature 传下去。"""
    fc = FakeClient([LLMResult(tool_input={
        "is_guess": True, "cause_hit": True, "mechanism_hit": True})])
    w = PuzzleWriter(client=fc, runtime_cfg=runtime_cfg(judge_temperature=0.0))
    w.judge("谜面", "谜底", "说中了")
    check("judge 传了 temperature", fc.calls[0]["temperature"] == 0.0,
          fc.calls[0]["temperature"])


def test_no_dead_judge_definition():
    """Q0.5: 不能留着返回 tuple 的旧 judge() 定义(误导性 dead code)。"""
    import inspect

    from story.llm import JudgeResult, PuzzleWriter
    src = inspect.getsource(PuzzleWriter.judge)
    check("judge 只定义一次", src.count("def judge(") == 1)
    check("judge 返回 JudgeResult",
          inspect.signature(PuzzleWriter.judge).return_annotation
          in (JudgeResult, "JudgeResult"),
          inspect.signature(PuzzleWriter.judge).return_annotation)
    # 旧定义会留下这个签名 —— 全局搜一遍确认没有第二份
    import story.llm as M
    whole = inspect.getsource(M)
    check("全文没有第二处 tuple[bool 签名的 judge",
          "-> tuple[bool, Optional[str]]" not in whole)


def test_judge_tech_failure_preserves_layer1_verdict():
    print("[Q5: 裁判技术失败不抹掉第一层裁决]")
    fc = FakeClient([
        LLMResult(tool_input={"answers": [
            {"id": 1, "verdict": "不是", "comment": "方向不对",
             "solution_candidate": True}]}),
        LLMResult(error="网关抖动"),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    res, _ = w.answer("谜面", "谜底", [], 1, "甲", "是同伴的肉吗")
    check("保留第一层的'不是'", res and res[0].verdict == "不是", res)
    check("comment 也保留", res and res[0].comment == "方向不对", res)


def test_judge_gate_cuts_calls():
    print("[Q5: candidate 闸门真的把 judge 调用率降下来]")
    canned = []
    for i in range(10):
        canned.append(LLMResult(tool_input={"answers": [
            {"id": i + 1, "verdict": "是", "solution_candidate": False}]}))
    canned.append(LLMResult(tool_input={"answers": [
        {"id": 11, "verdict": "是", "solution_candidate": True}]}))
    canned.append(LLMResult(tool_input={
        "is_guess": True, "cause_hit": True, "mechanism_hit": True}))
    fc = FakeClient(canned)
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    for i in range(10):
        w.answer("谜面", "谜底", [], i + 1, "甲", f"他是医生吗{i}")
    w.answer("谜面", "谜底", [], 11, "甲", "礁石露出所以灯标礁石")
    judges = [c for c in fc.calls if c["tool"]["name"] == "emit_judgement"]
    verdicts = [c for c in fc.calls if c["tool"]["name"] == "emit_verdict"]
    check("11 次裁决调用", len(verdicts) == 11, len(verdicts))
    check("只有 1 次裁判调用", len(judges) == 1, len(judges))
    check("judge/answer < 20%", len(judges) / len(verdicts) < 0.20,
          len(judges) / len(verdicts))


def test_answer_uses_facts_block():
    print("[Q5: 裁决 prompt 里带事实表]")
    facts = [{"id": "f1", "text": "退潮时礁石露出水面", "kind": "core"},
             {"id": "f2", "text": "灯是标礁石位置", "kind": "core"}]
    fc = FakeClient([LLMResult(tool_input={"answers": [
        {"id": 1, "verdict": "是", "solution_candidate": False}]})])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    w.answer("谜面", "谜底", [], 1, "甲", "礁石吗", facts=facts)
    u = fc.calls[0]["user"]
    check("prompt 有事实表段", "【事实表(判定依据)】" in u, u[:300])
    check("带 f1", "f1" in u, u[:400])
    check("带 fact 文本", "退潮时礁石露出水面" in u, u[:400])
    check("用 answer_temperature", fc.calls[0]["temperature"] == 0.0,
          fc.calls[0]["temperature"])


def test_answer_forbids_inventing_facts():
    print("[Q5: ANSWER_SYSTEM 声明 facts 是唯一依据]")
    from story.llm import ANSWER_SYSTEM
    check("有'唯一依据'", "唯一依据" in ANSWER_SYSTEM, ANSWER_SYSTEM[:200])
    check("有'不得自行新增'", "不得自行新增" in ANSWER_SYSTEM, ANSWER_SYSTEM[:300])
    check("明确没有揭晓", "没有「揭晓」" in ANSWER_SYSTEM, ANSWER_SYSTEM[:300])


def test_solution_candidate_definition():
    print("[Q5: solution_candidate 的判据写进了 schema]")
    from story.llm import _TOOL_ANSWER
    props = _TOOL_ANSWER["input_schema"]["properties"]["answers"]["items"]["properties"]
    sc = props["solution_candidate"]
    check("类型是 boolean", sc["type"] == "boolean", sc)
    check("给了具体例子", "退潮时礁石露出来" in sc["description"], sc)
    # 措辞更新: 旧描述写"只有 true 才会触发系统的最终判定", v6 起
    # 有合同的题 true 只触发 **completion 复核**(它不能判 solved),
    # 所以这里断言的是"说清了 true 会触发什么"。
    check("说清了 true 触发的是候选复核",
          "completion semantic" in sc["description"]
          and "不能直接判 solved" in sc["description"], sc)
    v = props["verdict"]
    check("枚举里没有揭晓", "揭晓" not in v["enum"], v["enum"])
    check("枚举就是三种", v["enum"] == ["是", "不是", "无关"], v["enum"])
    check("candidate 是必填", "solution_candidate"
          in _TOOL_ANSWER["input_schema"]["properties"]["answers"]["items"]["required"],
          _TOOL_ANSWER["input_schema"]["properties"]["answers"]["items"]["required"])


def test_text_fallback_solution_heuristic():
    print("[Q5: 文本回退路径的 candidate 启发式]")
    from story.llm import _looks_like_solution
    check("完整因果句 -> True",
          _looks_like_solution("退潮时礁石露出来所以灯是在标礁石位置") is True)
    check("短事实提问 -> False", _looks_like_solution("他是医生吗") is False)
    check("带因为的长句 -> True",
          _looks_like_solution("他每天多待十五分钟是因为以前出过事故") is True)
    check("空 -> False", _looks_like_solution("") is False)



def test_review_decision_pass():
    """P0-4: decision=pass -> 原样采用, 不改任何字段。"""
    fc = FakeClient([LLMResult(tool_input=riddle()),
                     LLMResult(tool_input=review_ok())])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    spec = w.gen_spec(blueprint=fc.default_blueprint)
    check("pass 后谜面不变", spec.puzzle == _GOOD_PUZ, spec.puzzle)
    check("pass 后 facts 不变", len(spec.facts) == 4, spec.facts)
    check("pass 后 signature 不变",
          spec.signature.mechanism_family == "hidden_function", spec.signature)


def test_review_decision_rewrite_regenerates():
    """P0-4: decision=rewrite -> **不修补**, 交回生成器换骨架。

    这是"结构性烂题"的唯一出口。早先只有 ok=true/false, 于是
    "核心就是不成立的题"会被 reviewer 围着旧骨架反复修。
    """
    P2 = "他每天数冰箱里的鸡蛋。为什么?"
    fc = FakeClient([
        LLMResult(tool_input=riddle()),
        LLMResult(tool_input=review_rewrite("谜底依赖题面外的私人往事")),
        # 应该**重新出题**(而不是修补上一稿)
        LLMResult(tool_input=riddle(puzzle=P2)),
        LLMResult(tool_input=review_ok(P2)),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    spec = w.gen_spec(blueprint=fc.default_blueprint)
    check("采用了重出的新题", spec.puzzle == P2, spec.puzzle)
    names = [c["tool"]["name"] for c in fc.calls]
    check("rewrite 后走的是**重新出题**而不是修补",
          names == ["emit_riddle", "emit_review", "emit_riddle", "emit_review"],
          names)
    # 重出请求里应带上 rewrite 的理由
    gen2 = fc.calls[2]["user"]
    check("重出请求带上 rewrite 理由", "私人往事" in gen2, gen2[-400:])


def test_review_decision_fix_syncs_facts():
    """P0-5: 审稿改了 answer -> facts **必须**跟着变。

    不然正式 Q&A 会依据**过期事实表**回答观众 —— 比以前"只看文学谜底"
    更危险, 因为现在系统会非常自信。
    """
    P1 = "海角守塔人只在退潮的那几个小时亮灯。为什么?"
    NEW_FACTS = [
        {"id": "f1", "text": "退潮时礁石露出", "kind": "core"},
        {"id": "f2", "text": "灯是标礁石位置", "kind": "core"},
        {"id": "f3", "text": "涨潮后亮灯误导船只", "kind": "support"},
        {"id": "f4", "text": "不是为了纪念", "kind": "exclusion"},
    ]
    fc = FakeClient([
        LLMResult(tool_input=riddle()),
        LLMResult(tool_input=review_fix(
            P1, answer="换了新解释。", facts=NEW_FACTS,
            solve_atoms=[{"id": "a1", "role": "cause", "text": "新因",
                          "fact_ids": ["f1"]},
                         {"id": "a2", "role": "mechanism", "text": "新机制",
                          "fact_ids": ["f2"]}],
            fair_clues=[{"quote": "只在退潮的那几个小时亮灯",
                         "supports_atoms": ["a1"]}])),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    spec = w.gen_spec(blueprint=fc.default_blueprint)
    check("facts 用了审稿人新给的",
          [f.text for f in spec.facts] == [f["text"] for f in NEW_FACTS],
          spec.facts)
    check("answer 也换了", "新解释" in (spec.answer or ""), spec.answer)
    check("atoms 指向**新** fact id",
          spec.solve_atoms[0].fact_ids == ["f1"], spec.solve_atoms[0].fact_ids)


def test_review_decision_fix_syncs_signature():
    """P0-6: 审稿改了核心 -> signature 必须用**重新判断**的。

    否则跨题配额登记的假指纹会把分布算错: 调度器以为刚播的是
    hidden_function, 实际观众看的是创伤题材。
    """
    P1 = "他三十年如一日擦那扇窗。为什么?"
    OBS = dict(sig_ok())
    OBS.update({"mechanism_family": "time_reinterpretation",
                "solution_shape": "past_trauma_explains_current_ritual",
                "emotion_mode": "grief", "time_shape": "years_long",
                "past_trauma": True, "repeated_ritual": True})
    # 生成器这一稿**自报**的就是 trauma 形状(否则会被 blueprint 严格比对
    # 在 reviewer 之前就拒掉 —— 那是另一条测试的事)。审稿人改完核心之后,
    # 用 observed_signature 把指纹**修正**成真实形状。
    gen = dict(riddle(puzzle=P1))
    gen["signature"] = dict(OBS)
    fc = FakeClient([
        LLMResult(tool_input=gen),
        LLMResult(tool_input=review_fix(P1, observed_signature=OBS,
                                        answer="他妻子在那扇窗后。")),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    bp = bp_for(fam="time_reinterpretation",
                shape="past_trauma_explains_current_ritual",
                emotion="grief", time_shape="years_long",
                past_trauma=True, repeated_ritual=True)
    spec = w.gen_spec(blueprint=bp)
    check("signature 用了审稿人的 observed_signature",
          spec.signature.mechanism_family == "time_reinterpretation",
          spec.signature)
    check("trauma 标记也更新了", spec.signature.past_trauma is True,
          spec.signature)
    check("time_shape 也跟着变了",
          spec.signature.time_shape == "years_long", spec.signature)


def test_review_rewrite_has_no_patch_requirement():
    """rewrite 时**不该**要求审稿人给 patch —— 它只需要给理由。"""
    from story.llm import _TOOL_CHECK
    req = _TOOL_CHECK["input_schema"]["required"]
    check("只要求 decision + observed_signature + quality_checks",
          set(req) == {"decision", "observed_signature", "quality_checks"},
          req)
    props = _TOOL_CHECK["input_schema"]["properties"]
    check("有 rewrite_reason 字段", "rewrite_reason" in props, sorted(props))
    check("有 facts 字段", "facts" in props, sorted(props))
    check("有 observed_signature 字段", "observed_signature" in props,
          sorted(props))
    d = props["decision"]
    check("decision 是三选一", d["enum"] == ["pass", "fix", "rewrite"], d)


def test_check_system_teaches_rewrite():
    """CHECK_SYSTEM 必须真的教 rewrite, 而不是只改 schema。"""
    from story.llm import CHECK_SYSTEM
    for k in ("pass", "fix", "rewrite", "rewrite_reason",
              "observed_signature", "私人往事"):
        check(f"CHECK_SYSTEM 含 {k}", k in CHECK_SYSTEM, k)
    check("不再说'任务是改不是退'", "你的任务是改" not in CHECK_SYSTEM,
          CHECK_SYSTEM[:200])
    check("明确不要照抄 signature", "不要照抄" in CHECK_SYSTEM,
          CHECK_SYSTEM[-600:])
    # 第二轮: 必须明说"改了就得整套给齐, 否则整稿被拒"
    check("说明了少给一样会被整稿拒掉", "整稿拒掉" in CHECK_SYSTEM,
          CHECK_SYSTEM[-700:])
    check("说明了 pass 时也要填 observed_signature",
          "pass 时也一样" in CHECK_SYSTEM, CHECK_SYSTEM[-500:])



def test_candidate_safety_net():
    """P1: 模型漏标 candidate 时, 句式兜底要把它救回来。

    只信模型自报的话, 一旦它把**完整答案**判成 false, Final Judge
    永远看不到 —— 观众明明说全了, 系统只回"是"。宁可多调一次裁判。
    """
    fc = FakeClient([
        # 模型说"不是候选", 但这句明显是完整因果
        LLMResult(tool_input={"answers": [
            {"id": 1, "verdict": "是", "solution_candidate": False}]}),
        LLMResult(tool_input={"is_guess": True, "cause_hit": True,
                              "mechanism_hit": True}),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    res, _ = w.answer("谜面", "谜底", [], 1, "甲",
                      "退潮时礁石露出来所以灯是在标礁石位置")
    check("兜底后仍调了裁判",
          [c["tool"]["name"] for c in fc.calls]
          == ["emit_verdict", "emit_judgement"],
          [c["tool"]["name"] for c in fc.calls])
    check("救回了通关", res and res[0].verdict == "揭晓", res)
    # 普通事实提问不该被兜底误伤
    fc2 = FakeClient([LLMResult(tool_input={"answers": [
        {"id": 2, "verdict": "是", "solution_candidate": False}]})])
    w2 = PuzzleWriter(client=fc2, runtime_cfg=fc2.runtime_cfg)
    w2.answer("谜面", "谜底", [], 2, "甲", "他是医生吗")
    check("短事实提问不触发兜底", len(fc2.calls) == 1,
          [c["tool"]["name"] for c in fc2.calls])


def test_touched_fact_ids_filtered():
    """P1: touched_fact_ids 必须过滤到**真实存在**的 fact id。

    Q6 的提示系统会靠这个集合选"还没探索过的方向", 混进假 id
    (模型爱编 f999) 会让它挑错。
    """
    facts = [{"id": "f1", "text": "退潮礁石露出", "kind": "core"},
             {"id": "f2", "text": "灯标礁石", "kind": "core"}]
    fc = FakeClient([LLMResult(tool_input={"answers": [
        {"id": 1, "verdict": "是", "solution_candidate": False,
         "touched_fact_ids": ["f1", "f999", "f2", "f1"]}]})])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    res, _ = w.answer("谜面", "谜底", [], 1, "甲", "礁石吗", facts=facts)
    check("只留合法 id 且去重",
          res[0].touched_fact_ids == ["f1", "f2"], res[0].touched_fact_ids)
    # 全非法 -> 清空
    fc2 = FakeClient([LLMResult(tool_input={"answers": [
        {"id": 2, "verdict": "是", "solution_candidate": False,
         "touched_fact_ids": ["f999", "f888"]}]})])
    w2 = PuzzleWriter(client=fc2, runtime_cfg=fc2.runtime_cfg)
    res2, _ = w2.answer("谜面", "谜底", [], 2, "甲", "礁石吗", facts=facts)
    check("全非法时清空", res2[0].touched_fact_ids == [],
          res2[0].touched_fact_ids)


def test_candidate_definition_documented():
    """P1: candidate 的判据与例子要写进 schema, 别让模型猜。"""
    from story.llm import _TOOL_ANSWER
    sc = _TOOL_ANSWER["input_schema"]["properties"]["answers"]["items"][
        "properties"]["solution_candidate"]
    for k in ("和灯塔有关吗", "退潮后礁石露出来", "他是医生吗"):
        check(f"schema 有例子 {k}", k in sc["description"], sc["description"][:200])



def test_fix_answer_without_facts_is_rejected():
    """P0-1(第二轮): 审稿改了 answer 却不给 facts -> **整稿拒绝**。

    这是上一版残留的漏洞: `_apply_review` 里写的是
        facts = [PuzzleFact.from_dict(f) for f in (ti.get("facts") or [])]
        if not facts:
            facts = list(spec.facts)
    ——**无条件**沿用旧值。注释说"谜底没变才沿用", 代码根本没做那个判断。

    于是"新谜底 + 旧事实表"照样进正式 Q&A: 主持人会依据**过期事实**
    非常自信地回答观众, 比"只看文学谜底"更危险。
    """
    P1 = "海角守塔人只在退潮的那几个小时亮灯。为什么?"
    for missing in ("facts", "solve_atoms", "fair_clues",
                    "observed_signature"):
        gen = riddle()
        fix = {"decision": "fix", "note": "改了核心",
               "puzzle": P1, "answer": "换了一个完全不同的解释。",
               "facts": gen["facts"], "solve_atoms": gen["solve_atoms"],
               "fair_clues": clues_for(P1),
               "observed_signature": sig_ok()}
        del fix[missing]
        fc = FakeClient([LLMResult(tool_input=gen),
                         LLMResult(tool_input=fix),
                         # 应该**重新出题**, 所以后面还得有料
                         LLMResult(tool_input=riddle(puzzle="二稿。为什么?")),
                         LLMResult(tool_input=review_ok("二稿。为什么?"))])
        w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
        spec = w.gen_spec(blueprint=fc.default_blueprint, max_attempts=3)
        check(f"缺 {missing} -> 该稿被拒(没有上屏)",
              spec.puzzle != P1, spec.puzzle)
        names = [c["tool"]["name"] for c in fc.calls]
        check(f"缺 {missing} -> 走的是重出而不是采用",
              names[:2] == ["emit_riddle", "emit_review"]
              and len(names) > 2 and names[2] == "emit_riddle", names)


def test_fix_puzzle_change_also_requires_full_sync():
    """P0-1: 只改**谜面**(没动 answer)也算"改了", 同样要整套同步。

    判据是"谜面或谜底任一变化", 不是只看 answer —— 否则删掉一句泄底
    的话、换了措辞, 也能带着陈旧的 facts 过关。
    """
    P1 = "海角守塔人只在退潮的那几个小时亮灯。为什么?"
    gen = riddle()
    fix = {"decision": "fix", "note": "只改了措辞",
           "puzzle": P1, "answer": gen["answer"],
           "facts": gen["facts"]}          # 没给 atoms/clues/signature
    fc = FakeClient([LLMResult(tool_input=gen),
                     LLMResult(tool_input=fix),
                     LLMResult(tool_input=riddle(puzzle="二稿。为什么?")),
                     LLMResult(tool_input=review_ok("二稿。为什么?"))])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    spec = w.gen_spec(blueprint=fc.default_blueprint, max_attempts=3)
    check("只改谜面也要整套同步", spec.puzzle != P1, spec.puzzle)


def test_pass_uses_reviewer_observed_signature():
    """P0-2(第二轮): decision=pass 也要吸收审稿人的 observed_signature。

    早先 pass 直接 `return spec`, 于是 blueprint 校验比的是**生成器自报**
    的指纹 —— 模型把 emotional_motive 报成 hidden_function, 审稿人看出来了
    并写了 observed_signature, 代码却照旧用自报的, validate_blueprint
    于是"验过了"。那是自己验自己。

    这条用例: 生成器报 hidden_function, 审稿人 pass 但观察为
    emotional_motive -> 最终必须按**审稿人**的, 并被 blueprint 硬拒。
    """
    gen = riddle()
    obs = dict(sig_ok())
    obs.update({"mechanism_family": "emotional_motive",
                "emotion_mode": "grief"})
    fc = FakeClient([
        LLMResult(tool_input=gen),
        LLMResult(tool_input=review_ok(observed_signature=obs)),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    bp = bp_for()                     # 要 hidden_function
    spec = w.gen_spec(blueprint=bp, max_attempts=1)
    check("审稿人的观察覆盖了生成器自报",
          spec.signature.mechanism_family == "emotional_motive"
          or not spec.puzzle,
          (spec.signature.mechanism_family, spec.puzzle))
    check("与 blueprint 冲突 -> 该稿没上屏", not spec.puzzle, spec.puzzle)


def test_pass_signature_accepted_when_matching():
    """P0-2: 审稿人 pass 且观察与 blueprint 一致时, 正常放行。"""
    gen = riddle()
    fc = FakeClient([LLMResult(tool_input=gen),
                     LLMResult(tool_input=review_ok())])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    spec = w.gen_spec(blueprint=bp_for())
    check("正常 pass 仍能出题", bool(spec.puzzle), spec.error)
    check("signature 被登记", spec.signature.mechanism_family
          == "hidden_function", spec.signature)


def test_enforce_blueprint_false_truly_skips():
    """P1(第二轮): `blueprint=None` 要**真的跳过**, 不是退回默认 blueprint。

    早先 `bp = blueprint or PuzzleBlueprint()` -> 关掉调度反而固定到
    information_gap/information_advantage/daily/neutral/instant ——
    所有题长一个样, 与日志里说的"不限形状"正好相反。

    这条用例: 生成器交回一个与默认 blueprint **完全不符**的题,
    enforce_blueprint=False 时必须放行; 默认(True)时必须拒。
    """
    other = riddle()
    other["signature"].update({"mechanism_family": "emotional_motive",
                               "solution_shape": "psychological_necessity",
                               "domain": "family", "emotion_mode": "grief"})

    # 默认 -> 拒(因为默认 blueprint 是 information_gap)
    fc1 = FakeClient([LLMResult(tool_input=dict(other))])
    w1 = PuzzleWriter(client=fc1, runtime_cfg=fc1.runtime_cfg)
    check("不传 blueprint 时默认按默认 blueprint 严格校验",
          not w1.gen_spec(blueprint=fc1.default_blueprint,
                          max_attempts=1).puzzle)

    # 显式关掉 -> 放行(审稿仍然要走, 所以给两发)
    fc2 = FakeClient([LLMResult(tool_input=dict(other)),
                      LLMResult(tool_input=review_ok())])
    w2 = PuzzleWriter(client=fc2, runtime_cfg=fc2.runtime_cfg)
    spec = w2.gen_spec(blueprint=None, enforce_blueprint=False,
                       max_attempts=1)
    check("enforce_blueprint=False 时真的不施加约束",
          bool(spec.puzzle), spec.error)
    # prompt 里不该出现"Blueprint"硬约束段
    check("prompt 里没有 Blueprint 硬约束段",
          "Blueprint" not in fc2.calls[0]["user"],
          fc2.calls[0]["user"][:300])
    check("prompt 明确说了不限形状",
          "不限形状" in fc2.calls[0]["user"], fc2.calls[0]["user"][:200])



def test_hint_is_fact_aware():
    """Q6(方案 §31): 提示必须拿到 facts/atoms/touched, 而不是只看谜面。"""
    from story.puzzle import PuzzleSpec
    sp = PuzzleSpec.from_dict(riddle())
    fc = FakeClient([LLMResult(tool_input={"hint": "想想他为什么挑那个时间。"})])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    h, err = w.hint("谜面", "谜底", 1, [], spec=sp, touched_fact_ids={"f1"})
    check("给出了提示", bool(h), (h, err))
    user = fc.calls[0]["user"]
    check("prompt 有'要点拨的方向'", "要点拨的方向" in user, user[:400])
    check("prompt 有禁止说出段", "禁止说出" in user, user[:600])
    check("prompt 带了 touched 信息", "已经问过的方向" in user, user[:800])
    check("用了 hint_temperature",
          fc.calls[0]["temperature"] == 0.5, fc.calls[0]["temperature"])


def test_hint_without_spec_still_works():
    """Q6: 没有 spec 的兜底题也必须有提示 —— 升级不能把这条路堵死。"""
    fc = FakeClient([LLMResult(tool_input={"hint": "注意顺序。"})])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    h, err = w.hint("谜面", "谜底", 1, [])
    check("无 spec 仍给出提示", bool(h), (h, err))
    check("无 spec 时 prompt 不含方向段",
          "要点拨的方向" not in fc.calls[0]["user"], fc.calls[0]["user"][:300])


def test_hint_rejects_leaked_fact():
    """Q6: 提示直接说出 core hidden fact -> 重出, 不让它上屏。"""
    from story.puzzle import PuzzleSpec
    sp = PuzzleSpec.from_dict(riddle())
    leak = "灯的真正作用是标示礁石位置的位置。"
    fc = FakeClient([
        LLMResult(tool_input={"hint": leak}),
        LLMResult(tool_input={"hint": "想想他为什么挑退潮那会儿。"}),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    h, err = w.hint("谜面", "谜底", 1, [], spec=sp)
    check("泄漏的那条被换掉", h != leak, h)
    check("重出过一次", len(fc.calls) == 2, len(fc.calls))
    check("最终给的是干净的那条", "退潮" in (h or ""), h)


def test_hint_leak_checker_does_not_overblock():
    """Q6: 泄漏检查要**保守** —— 共享几个汉字不该拦。

    过度拦截会让提示被迫说得极含糊(观众更懵)。直播里一条稍微具体
    的提示, 远比一条没用的提示好。
    """
    from story.llm import _hint_leaks
    focus = {"forbidden_core_terms": ["退潮时危险礁石会露出或接近水面"],
             "focus_fact_texts": ["灯的真正作用是标示危险礁石的位置"]}
    check("正常引导语不拦",
          _hint_leaks("想想他为什么挑那个时间开灯。", focus) == "",
          _hint_leaks("想想他为什么挑那个时间开灯。", focus))
    check("用了同一个词但不泄底 -> 不拦",
          _hint_leaks("注意'灯'这个字出现了几次。", focus) == "",
          _hint_leaks("注意'灯'这个字出现了几次。", focus))
    check("照搬 fact 原话 -> 拦",
          _hint_leaks("因为退潮时危险礁石会露出或接近水面。", focus) != "",
          "应拦住")


def test_gen_spec_records_metrics():
    """Q7(方案 §35): gen_spec 要把出题/审稿过程指标挂在 spec 上。"""
    fc = FakeClient([LLMResult(tool_input=riddle()),
                     LLMResult(tool_input=review_rewrite("骨架不行")),
                     LLMResult(tool_input=riddle(puzzle="二稿。为什么?")),
                     LLMResult(tool_input=review_ok("二稿。为什么?"))])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    spec = w.gen_spec(blueprint=fc.default_blueprint)
    m = spec.metrics
    check("有 generation_attempts", m.get("generation_attempts") == 2, m)
    # 注意: FakeClient 是瞬时的, 所以这里是 0 —— 断言的是**字段存在且是
    # 非负整数**, 不是"大于 0"。真实调用时它才是正数。
    check("有 generation_latency_ms",
          isinstance(m.get("generation_latency_ms"), int)
          and m["generation_latency_ms"] >= 0, m)
    check("有 review_calls", m.get("review_calls") == 2, m)
    check("有 review_decision", m.get("review_decision") == "pass", m)
    check("记了 rewrite 次数", m.get("rewrite_count") == 1, m)
    check("标了 ok", m.get("ok") is True, m)


def test_gen_spec_failure_also_records_metrics():
    """Q7: 失败路径也要有指标 —— 否则"出了几稿才放弃"查不出来。"""
    bad = dict(riddle(), facts=[])
    fc = FakeClient([LLMResult(tool_input=dict(bad)),
                     LLMResult(tool_input=dict(bad, puzzle="二稿。为什么?"))])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    spec = w.gen_spec(blueprint=fc.default_blueprint, max_attempts=2)
    check("失败时也有 metrics", bool(spec.metrics), spec.metrics)
    check("失败标了 ok=False", spec.metrics.get("ok") is False, spec.metrics)
    check("失败也记了耗时",
          spec.metrics.get("generation_latency_ms", 0) >= 0, spec.metrics)


def test_review_issues_recorded():
    """Q7: 审稿提了什么问题也要落盘 —— 复盘时这是最有价值的一栏。"""
    fc = FakeClient([
        LLMResult(tool_input=riddle()),
        LLMResult(tool_input=review_fix(
            "海角守塔人只在退潮的那几个小时亮灯。为什么?",
            issues=["谜面是第一人称", "结尾缺问句"])),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    spec = w.gen_spec(blueprint=fc.default_blueprint)
    check("review_issues 被记录",
          spec.metrics.get("review_issues") == ["谜面是第一人称", "结尾缺问句"],
          spec.metrics)
    check("review_decision 是 fix",
          spec.metrics.get("review_decision") == "fix", spec.metrics)



def test_hint_never_returns_leaking_hint_when_exhausted():
    """Q6 blocker(第三轮 review): 三次全泄底时**绝不返回**泄底提示。

    早先的收尾是 `return h, None` —— 它分不清"三次只是重复"和
    "三次全部泄底"。于是模型只要坚持三次把答案说出来, 第三条照上屏,
    泄漏检测形同虚设。

    重复可以认(挑一条最不重复的, 总比没提示强); **泄底不能认**。
    """
    from story.puzzle import PuzzleSpec
    sp = PuzzleSpec.from_dict(riddle())
    leak = "灯的真正作用是标示礁石位置。"
    # 三次全部泄底
    fc = FakeClient([LLMResult(tool_input={"hint": leak}),
                     LLMResult(tool_input={"hint": leak + "。"}),
                     LLMResult(tool_input={"hint": leak + "！"})])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    h, err = w.hint("谜面", "谜底", 1, [], spec=sp)
    check("三次全泄底 -> 不给提示", h is None, h)
    check("给出原因", bool(err) and "泄底" in err, err)
    check("三次都试过了", len(fc.calls) == 3, len(fc.calls))


def test_hint_uses_last_safe_when_only_repeated():
    """Q6 final: 只是**重复**(没泄底)时, 三次耗尽可以认一条干净的。

    这是刻意的区分: 重复的代价只是"观众觉得提示没用", 而泄底的代价
    是"整道题废掉"。两者不能用同一条兜底策略。
    """
    from story.puzzle import PuzzleSpec
    sp = PuzzleSpec.from_dict(riddle())
    same = "再想想灯是用来做什么的。"
    fc = FakeClient([LLMResult(tool_input={"hint": same}),
                     LLMResult(tool_input={"hint": same}),
                     LLMResult(tool_input={"hint": same})])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    # 把它当成"已经给过"的 -> 每次都被判重复
    h, err = w.hint("谜面", "谜底", 2, [same], spec=sp)
    check("重复三次仍给出一条(干净的)", h == same, (h, err))
    check("没有报泄底", not err or "泄底" not in err, err)


def test_hint_leak_check_includes_focus_atom():
    """Q6 final: `focus_atom` 也要查 —— prompt 里把整条 atom 给了模型。

    atom 本身往往就等于答案("灯是在标礁石, 而不是给船引路")。
    只查 fact 文本的话, 模型照搬 atom 而用词与 fact 前 6 字不同就会漏。
    """
    from story.llm import _hint_leaks
    focus = {"focus_atom": "灯是在标礁石, 而不是给船引路",
             "forbidden_core_terms": [],
             "focus_fact_texts": []}
    check("照搬 atom -> 拦",
          _hint_leaks("灯是在标礁石, 而不是给船引路。", focus) != "", "应拦住")
    check("正常引导仍不拦",
          _hint_leaks("想想他为什么挑那个时间开灯。", focus) == "",
          _hint_leaks("想想他为什么挑那个时间开灯。", focus))


def test_gen_spec_records_review_latency_total():
    """Q7(第三轮 review): 审稿耗时是累计值, 不是最后一次。"""
    fc = FakeClient([LLMResult(tool_input=riddle()),
                     LLMResult(tool_input=review_rewrite("不行")),
                     LLMResult(tool_input=riddle(puzzle="二稿。为什么?")),
                     LLMResult(tool_input=review_ok("二稿。为什么?"))])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    spec = w.gen_spec(blueprint=fc.default_blueprint)
    m = spec.metrics
    check("有 review_latency_ms_total",
          isinstance(m.get("review_latency_ms_total"), int), m)
    check("两次审稿都计入了(值 >= 0)",
          m.get("review_latency_ms_total") >= 0, m)
    check("review_calls=2 可算均值", m.get("review_calls") == 2, m)


def test_blueprint_specified_is_explicit_not_inferred():
    """Q7(第三轮 review): provenance 必须显式, 不能从 blueprint 值推断。

    调度器**完全可能合法地**选中 information_gap + information_advantage
    —— 那种题是有 blueprint 的。靠"值等于默认值就判 False"会把它误标,
    统计污染只是换了个方向。
    """
    from story.puzzle import PuzzleBlueprint, PuzzleSpec
    # 一个 domain/relation 都是真实调度结果、但 family/shape 恰好是默认值的题
    bp = PuzzleBlueprint(mechanism_family="information_gap",
                         solution_shape="information_advantage",
                         domain="aviation", relation="colleague")
    gen = riddle()
    gen["signature"].update({"mechanism_family": "information_gap",
                             "solution_shape": "information_advantage",
                             "domain": "aviation", "relation": "colleague"})
    gen["blueprint"] = bp.to_dict()
    # 审稿人的观察必须与 blueprint 一致, 否则会被正确拒掉(那是另一条测试)
    obs = dict(gen["signature"])
    fc = FakeClient([LLMResult(tool_input=gen),
                     LLMResult(tool_input=review_ok(observed_signature=obs))])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    spec = w.gen_spec(blueprint=bp)
    check("恰好是默认值但确实是调度出来的 -> True",
          spec.blueprint_specified is True, spec.blueprint_specified)
    check("archive 如实落盘",
          spec.to_archive().get("blueprint_specified") is True,
          spec.to_archive().get("blueprint_specified"))

    # 自由生成(不施加 blueprint) -> False
    fc2 = FakeClient([LLMResult(tool_input=riddle()),
                      LLMResult(tool_input=review_ok())])
    w2 = PuzzleWriter(client=fc2, runtime_cfg=fc2.runtime_cfg)
    spec2 = w2.gen_spec(blueprint=None, enforce_blueprint=False)
    check("自由生成 -> False", spec2.blueprint_specified is False,
          spec2.blueprint_specified)

    # round-trip 不能丢
    sp = PuzzleSpec.from_dict(spec.to_archive())
    check("round-trip 保住 provenance",
          sp.blueprint_specified is True, sp.blueprint_specified)



def test_messages_timeout_override():
    """`messages()` 的 timeout / max_retries 覆盖(Hotfix B)。

    默认不传 = 沿用全局(`AI_TIMEOUT` / `AI_MAX_RETRIES`), 行为与改动前完全
    一致 —— 出题/审稿/试玩都依赖这一点。传了才生效。

    用打桩的 urlopen 观察**真实**发出的请求, 而不是看函数签名。
    """
    import urllib.request
    import urllib.error
    from story.llm import AnthropicMessagesClient
    from story.config import LLMConfig

    cfg = LLMConfig(api_key="x", base_url="http://x", model="m",
                    timeout=60.0, max_retries=3)
    c = AnthropicMessagesClient(cfg)

    seen = {"timeouts": [], "calls": 0}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            return ('{"content":[{"type":"text","text":"hi"}],'
                    '"usage":{"input_tokens":1,"output_tokens":1}}'
                    ).encode("utf-8")

    def fake_urlopen(req, timeout=None):
        seen["calls"] += 1
        seen["timeouts"].append(timeout)
        return _Resp()

    orig = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        # ① 不传 -> 用全局 60
        c.messages("s", "u")
        check("**默认沿用全局 timeout=60**", seen["timeouts"][-1] == 60.0,
              seen["timeouts"][-1])

        # ② 传 timeout -> 用它
        c.messages("s", "u", timeout=8.0)
        check("**传了就用传的值**", seen["timeouts"][-1] == 8.0,
              seen["timeouts"][-1])

        # ③ max_retries=0 -> 失败时只打一次, 不重试
        def boom(req, timeout=None):
            seen["calls"] += 1
            raise TimeoutError("timed out")
        urllib.request.urlopen = boom
        before = seen["calls"]
        r = c.messages("s", "u", timeout=8.0, max_retries=0)
        check("**max_retries=0 只请求一次**", seen["calls"] - before == 1,
              seen["calls"] - before)
        check("返回的是错误结果(不是抛异常)", r.error is not None, r.error)
    finally:
        urllib.request.urlopen = orig


def test_answer_passes_qa_budget_to_client():
    """`PuzzleWriter.answer()` 要把预算透传给 client.messages()。

    这条防的是"加了参数但没接上" —— 那样 QA 仍会吃全局 60s。
    """
    from story.llm import PuzzleWriter

    cli = FakeClient([LLMResult(tool_input={"answers": [
        {"id": 1, "verdict": "是", "comment": "对"}]})])
    w = PuzzleWriter(client=cli, runtime_cfg=runtime_cfg())
    w.answer("谜面", "谜底", [], 1, "甲", "他是盲人吗",
             timeout=8.0, max_retries=0)
    got = cli.calls[-1]
    check("**answer() 把 timeout 透传给 client**",
          got.get("timeout") == 8.0, got.get("timeout"))
    check("**answer() 把 max_retries 透传给 client**",
          got.get("max_retries") == 0, got.get("max_retries"))

    # 不传 -> None(None 表示"沿用全局", 不能悄悄变成某个默认值)
    cli2 = FakeClient([LLMResult(tool_input={"answers": [
        {"id": 1, "verdict": "是", "comment": "对"}]})])
    w2 = PuzzleWriter(client=cli2, runtime_cfg=runtime_cfg())
    w2.answer("谜面", "谜底", [], 1, "甲", "他是盲人吗")
    got2 = cli2.calls[-1]
    check("不传时为 None(沿用全局)", got2.get("timeout") is None
          and got2.get("max_retries") is None, got2)


def test_qa_budget_reaches_final_judge():
    """candidate 的**第二层**(Final Judge)也必须吃 QA 预算(Hotfix B2)。

    漏掉这条会留下一个真实缺口: 第一层 verdict 用了 8s, 但 candidate=True
    时 `answer()` 会再调 `judge()`, 而 `judge()` 原先不接预算 -> 退回全局
    60s/3 次重试。引擎 25s 后已 fail-fast 判"未判定"(不会再派第二个
    worker), 但旧 worker 会一直卡在裁判上占着 answer pool 的槽位。

    钉住的是**两次调用都拿到 8.0 / 0** —— 只断言其中一次会漏掉另一层。
    """
    from story.llm import PuzzleWriter

    # 第一层: 声明 solution_candidate=True, 于是会进 Final Judge
    cli = FakeClient([
        LLMResult(tool_input={"answers": [
            {"id": 1, "verdict": "是", "comment": "像是说中了",
             "solution_candidate": True}]}),
        # 第二层: 裁判结果
        LLMResult(tool_input={"is_guess": True, "cause_hit": True,
                              "mechanism_hit": True}),
    ])
    w = PuzzleWriter(client=cli, runtime_cfg=runtime_cfg())
    w.answer("谜面", "谜底", [], 1, "甲", "他是盲人所以每晚点灯",
             timeout=8.0, max_retries=0)

    check("**确实调了两次**(verdict + judge)", len(cli.calls) == 2,
          len(cli.calls))
    for i, c in enumerate(cli.calls):
        layer = "verdict" if i == 0 else "judge"
        check(f"**{layer} 拿到 timeout=8.0**", c.get("timeout") == 8.0,
              c.get("timeout"))
        check(f"**{layer} 拿到 max_retries=0**", c.get("max_retries") == 0,
              c.get("max_retries"))

    # 直接调 judge()(golden / 离线裁判 / Q10 的路)不传 -> 仍沿用全局
    cli3 = FakeClient([LLMResult(tool_input={"is_guess": True,
                                             "cause_hit": True,
                                             "mechanism_hit": True})])
    w3 = PuzzleWriter(client=cli3, runtime_cfg=runtime_cfg())
    w3.judge("谜面", "谜底", "他是盲人", facts=None)
    got3 = cli3.calls[-1]
    check("直接调 judge() 时沿用全局(None)",
          got3.get("timeout") is None and got3.get("max_retries") is None,
          got3)


def main():
    for t in (test_riddle_tool,
              # ---- UX-2: v5 通关合同 ----
              test_ux_g_v5_skips_final_judge,
              test_ux_h_legacy_still_judges,
              test_ux_established_filtered_in_answer,
              test_reviewer_fixes_in_place,
              test_messages_timeout_override,
              test_answer_passes_qa_budget_to_client,
              test_qa_budget_reaches_final_judge,
              test_hard_rule_asks_reviewer_to_fix,
              test_reviewer_no_fix_falls_back_to_regen,
              test_first_person_story_rejected,
              test_no_repeat_puzzles,
              test_riddle_check_retries_empty_tool_use, test_english_riddle_rejected_on_text_path,
              test_atom_role_gate,
              # ---- Step 08: stable atom IDs ----
              test_s08_judge_accepts_atom_ids,
              test_s08_judge_prompt_shows_atom_ids,
              test_s08_legacy_index_still_readable,
              test_s08_unknown_atom_id_is_dropped,
              test_s08_index_is_not_stable_across_reorder,
              # ---- Batch B closeout ----
              test_bco_legacy_index_normalized_to_id_on_output,
              test_bco_legacy_index_kept_only_when_atoms_lack_ids,
              test_bco_judge_text_fallback_cannot_solve,
              test_bco_empty_tool_input_still_fails_closed,
              test_judge_technical_failure_not_downgraded_to_irrelevant,
              test_reviewer_keeps_solve_atoms,
              test_reviewer_can_replace_atoms_when_answer_changes,
              test_main_regression_no_atoms_lost,
              test_reject_reasons_accumulate, test_rejected_puzzle_goes_into_avoid,
              test_bad_draft_does_not_consume_attempt,
              test_wrapped_tool_input_unwrapped, test_hints_not_leaked_into_puzzle,
              test_fallback_riddles_rotate, test_riddle_fallback, test_answer_tool,
              test_answer_rejects_bad_enum, test_answer_enum_forced_by_schema,
              test_answer_consults_judge, test_answer_judge_not_consulted_without_answer,
              test_judge, test_verdict_solve_must_pass_judge,
              test_open_question_never_solves,
              test_open_question_with_hypothesis_can_solve,
              test_llm_failure_returns_unavailable_not_irrelevant,
              test_hint_not_repeated,
              test_hint_and_reveal, test_tool_actually_requested,
              # ---- Q0: 链路缺陷回归 ----
              test_check_tool_schema_matches_generator,
              test_reviewer_structured_atoms_survive,
              test_fixable_format_goes_to_reviewer_not_rejected,
              test_unfixed_format_still_rejected,
              test_writer_reads_temperature_from_runtime_cfg,
              test_client_cfg_is_not_used_for_temperature,
              test_quotas_read_from_runtime_cfg,
              test_judge_passes_temperature,
              # ---- P0-4/5/6 ----
              test_rejected_spec_never_returned,
              test_rejected_by_cross_puzzle_gate_not_returned,
              test_director_only_airs_clean_spec,
              test_candidate_safety_net,
              test_touched_fact_ids_filtered,
              test_candidate_definition_documented,
              # ---- Q6 / Q7 ----
              test_hint_never_returns_leaking_hint_when_exhausted,
              test_hint_uses_last_safe_when_only_repeated,
              test_hint_leak_check_includes_focus_atom,
              test_gen_spec_records_review_latency_total,
              test_blueprint_specified_is_explicit_not_inferred,
              test_hint_is_fact_aware,
              test_hint_without_spec_still_works,
              test_hint_rejects_leaked_fact,
              test_hint_leak_checker_does_not_overblock,
              test_gen_spec_records_metrics,
              test_gen_spec_failure_also_records_metrics,
              test_review_issues_recorded,
              # ---- 第二轮 review ----
              test_fix_answer_without_facts_is_rejected,
              test_fix_puzzle_change_also_requires_full_sync,
              test_pass_uses_reviewer_observed_signature,
              test_pass_signature_accepted_when_matching,
              test_enforce_blueprint_false_truly_skips,
              test_review_decision_pass,
              test_review_decision_rewrite_regenerates,
              test_review_decision_fix_syncs_facts,
              test_review_decision_fix_syncs_signature,
              test_review_rewrite_has_no_patch_requirement,
              test_check_system_teaches_rewrite,
              test_no_dead_judge_definition,
              # ---- Q5 ----
              test_judge_tech_failure_preserves_layer1_verdict,
              test_judge_gate_cuts_calls,
              test_answer_uses_facts_block,
              test_answer_forbids_inventing_facts,
              test_solution_candidate_definition,
              test_text_fallback_solution_heuristic,
              # ---- Step 04: Riddle / Reviewer v4 ----
              test_v4_prompt_versions_bumped,
              test_v4_signature_schema_has_new_dimensions,
              test_v4_check_system_freezes_reviewer_scope,
              test_v4_riddle_system_states_orthogonality,
              test_v4_observed_fields_flow_to_signature,
              test_v4_reviewer_observed_reveal_wins_over_generator,
              test_v4_reviewer_cannot_see_recent_quota,
              test_v4_apply_review_keeps_new_fields_on_fix,
              test_v4_policy_version_is_v4,
              # ---- Batch A closeout ----
              test_closeout_check_tool_has_no_recent_window_rule,
              test_closeout_observed_signature_schema_is_complete,
              test_closeout_incomplete_observed_signature_is_rejected,
              test_closeout_incomplete_obs_never_lands_in_signature,
              test_closeout_complete_observed_signature_is_accepted,
              test_closeout_absent_observed_signature_on_pass_is_rejected):
        t()
    print()
    if FAIL[0]:
        print(f"FAILED: {FAIL[0]} 项")
        return 1
    print("PASS: PuzzleWriter(强制工具 + 回退解析) 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
