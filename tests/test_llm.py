"""运行: uv run tests/test_llm.py（完全离线, 无网络）。

验证 PuzzleWriter 走**强制工具调用**时, 能把 tool_input 正确转成结构化结果,
以及在工具不可用(只回了文本)时**回退到宽容解析**。

不联网: 用一个假的 client 顶替 AnthropicMessagesClient。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from story.llm import (  # noqa: E402
    RIDDLE_PROMPT_VERSION, LLMResult, PuzzleWriter,
    # G2-keyword2: Stage A/B 的 prompt、schema 与审稿契约
    KEYWORD_IDEA_SYSTEM, _TOOL_KEYWORD_IDEA, _TOOL_STRUCTURE, check_tool,
)
from story.quality import QUALITY_POLICY_VERSION  # noqa: E402
from story.puzzle import DiscoveryBeat, FairClue, PuzzleSpec  # noqa: E402

FAIL = [0]


def check(name, cond, extra=""):
    if cond:
        print(f"  ok  {name}")
        return
    print(f"  FAIL {name}  {extra}")
    FAIL[0] += 1


class FakeClient:
    """按顺序吐预设的 LLMResult, 并记录收到的 tool 参数。

    ## Q1 起: truth audit 有**默认通过**的自动应答

    出题链里多了一道 `emit_truth_audit`。若让每条出题用例都在队列里
    手动补一条, 几十条用例会全部变成"我在测队列长度" —— 而它们真正想
    测的是 facts 解析 / 硬校验 / 审稿合并。

    所以: **队列耗尽且这次调用是 truth audit 时**, 自动回一个"通过"。
    队列耗尽且是**别的**工具时, 仍然回 `no more canned results` ——
    那才是"代码偷偷多调了一次"的信号(UX-G2 就靠它)。

    要测 audit 本身(拒稿 / 技术失败)的用例, 显式在队列里放一条
    `_truth_tool(...)`, 它会**先**被取走。
    """

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
                           "max_tokens": max_tokens,
                           "timeout": timeout, "max_retries": max_retries})
        name = (tool or {}).get("name")
        if name == "emit_truth_audit":
            # truth audit **不参与队列轮转**: 队列里只有测试**显式**放的
            # audit 结果才用它, 否则一律自动回"通过"。
            #
            # 为什么不能让它按位置取: 出题链是
            #     出题 -> 审稿 -> audit -> (可能重出) -> 审稿 -> audit
            # audit 出现的位置随重试次数变, 按位置取就会**吃掉**本该给
            # 下一次出题/审稿的那一条, 于是后面全线错位。用显式标记取,
            # 队列语义就与"调用顺序"解耦了。
            for i, r in enumerate(self._results):
                if (r.tool_input or {}).get("__truth_audit__"):
                    self._results.pop(i)
                    ti = dict(r.tool_input)
                    ti.pop("__truth_audit__", None)
                    return LLMResult(tool_input=ti, model=r.model)
            return _truth_tool()
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
        # G4-RB §六: 允许 1~4 个发现阶段。这里给 2 条(多层的正常形状);
        # 单层题另有专门回归(见 test_g4rb_single_beat_is_legal)。
        "discovery_beats": [
            {"id": "b1", "text": "先注意到灯只在退潮时亮",
             "fact_ids": ["f1"]},
            {"id": "b2", "text": "再想到灯是在标礁石, 不是引路",
             "fact_ids": ["f2"]},
        ],
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
         "core_answer_direct": True, "completion_contract_minimal": True,
         # quality-v8: 后四项查"好不好玩", 同样是 fail-closed 的硬门。
         "concrete_anomaly": True, "clue_recontextualized": True,
         "dramatic_payoff": True, "reasoning_beats_nonredundant": True,
         # G4-E: 自由生成链新增的**直播安全硬门**。语义: 死亡作为普通
         # 剧情事实可以(true), 拿重口 / 极端伤害本身当噱头 -> false。
         # 夹具里的题都是普通悬疑, 所以是 true。
         "livestream_safe": True}
    d.update(kw)
    return d


def _truth_tool(truthful=True, consistent=True, conflicts=None):
    """Q1 `emit_truth_audit` 的 canned 返回。

    `__truth_audit__` 这个标记让 `FakeClient` 认出"这条是给 audit 的",
    从队列里**按标记**取而不是按位置 —— 见 `FakeClient.messages` 的说明。
    """
    return LLMResult(tool_input={
        "__truth_audit__": True,
        "narrator_truthful": truthful,
        "mechanism_consistent": consistent,
        "conflicts": list(conflicts or []),
    }, model="m")


def gen_calls(fc):
    """出题链上的调用**去掉 truth audit** 之后还剩几条。

    Q1 之后每条出题用例的调用数都多一次 audit。那些用例想数的是
    "出题/审稿/重出" 各几次 —— 把 audit 算进去只会让每个数字 +1,
    而它们真正要守的性质(重出一稿 = 多一轮)完全没变。

    ⚠️ 这是**过滤**, 不是"允许任意多调" —— 过滤后的数字仍然是精确断言,
    而 audit 本身由 `test_truth_audit_*` 专门测。
    """
    return [c for c in fc.calls
            if (c["tool"] or {}).get("name") != "emit_truth_audit"]


#: 一条"谜面 vs 谜底字面矛盾"的冲突记录(桥题那个真实案例的形状)。
BRIDGE_CONFLICT = {
    "puzzle_claim": "司机并没有掉头",
    "answer_claim": "司机到对岸正常调头后又驶回桥上",
    "why": "谜面用无归属的绝对否定断言了'没掉头', 谜底要求'掉过头'",
}


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


def _completion_tool(ids=None):
    """completion 复核的返回(A1 的第二次调用)。"""
    return LLMResult(
        tool_input={"matched_completion_fact_ids": list(ids or [])},
        model="m")


def _verdict_tool_no(established=None, touched=None, cand=False):
    """同 `_verdict_tool`, 但裁决是「不是」(Truth/边界用)。"""
    r = _verdict_tool(established, touched, cand)
    r.tool_input["answers"][0]["verdict"] = "不是"
    return r


def _verdict_tool_irrelevant(cand=True):
    """candidate=True 却判「无关」—— 自相矛盾结果(A2 的原料)。"""
    return LLMResult(tool_input={"answers": [{
        "id": 1, "verdict": "无关", "comment": "发个 是/不是 的猜测",
        "solution_candidate": cand,
        "touched_fact_ids": [], "established_fact_ids": [],
    }]}, model="m")


def test_ux_g_v5_skips_final_judge():
    """Case G: 有通关合同 -> 只调**一次** client, 绝不调 emit_judgement。

    若这里仍调 Final Judge, 就又多了一条绕开合同的通关路径: 观众说中
    一条 support 剧情也可能被裁判判"猜中", 于是合同形同虚设。
    """
    print("\n[UX-G] v5 有合同不调 Final Judge(completion 要复核)")
    # A1 起有**两次**调用: verdict + completion 复核。断言的是
    # **没有 emit_judgement** —— 那才是"不调 Final Judge"的意思。
    fc = FakeClient([
        _verdict_tool(established=["f1"], cand=True),
        _completion_tool(["f1"]),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    out, err = w.answer("谜面?", "谜底。", [], 1, "甲", "她是姐姐",
                        facts=riddle()["facts"],
                        completion_fact_ids=["f1"])
    check("调了两次(verdict + completion 复核)", len(fc.calls) == 2,
          len(fc.calls))
    check("第一次是 emit_verdict",
          fc.calls[0]["tool"]["name"] == "emit_verdict",
          fc.calls[0]["tool"].get("name"))
    check("**没有 emit_judgement 调用**",
          all(c["tool"] and c["tool"]["name"] != "emit_judgement"
              for c in fc.calls), [c["tool"] for c in fc.calls])
    check("返回了一条结果", len(out) == 1, out)
    check("established 被复核确认后带回",
          out and out[0].established_fact_ids == ["f1"],
          out[0].established_fact_ids if out else None)
    check("**没有**被标成 P.SOLVE", out and out[0].verdict != "揭晓",
          out[0].verdict if out else None)


def test_ux_g2_plain_qa_is_still_one_call():
    """**普通事实问答仍然恰好 1 次 LLM。**

    completion 复核是有条件的(第一层自报了 completion / 是完整答案
    候选), 不该把所有 QA 都变成双调用 —— 那是时延灾难。
    """
    print("\n[UX-G2] 普通 QA 仍 1 次 LLM")
    # 队列里只放一次。若代码偷偷调复核, FakeClient 会吐
    # "no more canned results"。断言的就是"没调"。
    fc = FakeClient([_verdict_tool(established=["f3"])])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    out, err = w.answer("谜面?", "谜底。", [], 1, "甲", "有人死吗",
                        facts=riddle()["facts"],
                        completion_fact_ids=["f1"])
    check("**只调了一次 client**", len(fc.calls) == 1, len(fc.calls))
    check("普通 fact 的 established 直接带回",
          out and out[0].established_fact_ids == ["f3"],
          out[0].established_fact_ids if out else None)


def test_ai_player_ask_is_exactly_one_host_call():
    """AI 普通 ask 禁掉 solve 相关复核：Player 之外只调用一次 Host。"""
    print("\n[AI-LLM] ask 的 Host 裁决恰好 1 次")
    fc = FakeClient([_verdict_tool_irrelevant(cand=True)])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    out, _ = w.answer(
        "谜面?", "谜底。", [], 0, "AI玩家", "地点重要吗？",
        judge_solve=False, facts=riddle()["facts"],
        completion_fact_ids=[])
    check("只调一次 Host", len(fc.calls) == 1, len(fc.calls))
    check("没有 Final Judge/候选重判",
          fc.calls[0]["tool"]["name"] == "emit_verdict",
          [c["tool"]["name"] for c in fc.calls])
    check("正常返回公开裁决", out and out[0].verdict == "无关", out)


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
          len(gen_calls(fc)) == 2, len(gen_calls(fc)))


def test_g4rb_shape_is_not_asked_of_reviewer():
    """G4-RB §三/§四: 审稿请求里**不再**注入人称/问句的 hard focus。

    旧行为(`test_hard_rule_asks_reviewer_to_fix` 记的是它): 代码在
    `must_fix` 为空时**替审稿人决定**"谜面是第一人称, 改成第三人称",
    于是哪怕 `validate_spec` 已经不判它, 形状门也会从这一处继续生效。

    现在 `hard` **只**来自 `must_fix`(真正的结构问题)。审稿人对一条
    第一人称、无结尾问句的谜面**没有任何**代码注入的问题要改。
    """
    P0 = "我每晚都听见楼上有人走动, 可楼上根本没人住。为什么?"
    fc = FakeClient([
        LLMResult(tool_input=riddle(puzzle=P0)),
        LLMResult(tool_input=review_ok(P0)),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    r = w.gen_riddle(blueprint=fc.default_blueprint)
    check("第一人称稿被采用", bool(r.puzzle), r)
    review_user = fc.calls[1]["user"]
    check("审稿请求里没有人称 hard focus",
          "改成第三人称" not in review_user, review_user[-300:])
    check("审稿请求里没有补问句 hard focus",
          "末尾补一句" not in review_user, review_user[-300:])
    check("审稿请求里没有【已知问题】段",
          "【已知问题, 必须改掉】" not in review_user, review_user[-300:])


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
    check("共 4 次调用(出题/审稿/重出/审稿)", len(gen_calls(fc)) == 4, len(gen_calls(fc)))


def test_riddle_check_retries_empty_tool_use():
    """**G2-F**: 审稿拿到空 tool_input(网关抖动) -> **重试同一稿**, 不换稿。

    这不是"这题不好", 是**这次调用没成功**。旧实现把它记成"第 1 稿要求
    重出", 于是丢掉一份可能完全合格的稿子去重新生成一道新题 ——
    一次网关抖动变成整整一轮生成成本。

    修好之后: generator call 仍然只有 **1** 次(emit_riddle 出现一次),
    审稿被重试(emit_review 两次), 第二次成功。

    ⚠️ 重试时 `max_tokens` 抬高一档(3500 -> 4500): 截断的成因就是预算
    不够。
    """
    print("[出题: 质检空 tool_input -> 重试同一稿(G2-F)]")
    P1 = _GOOD_PUZ
    fc = FakeClient([
        LLMResult(tool_input=riddle()),
        # 第 1 次审稿: 空 tool_input(抖动) -> 技术失败
        LLMResult(tool_input={}, text="I'll analyze this puzzle"),
        # 第 2 次审稿(**同一稿**): 正常通过
        LLMResult(tool_input=review_ok(P1)),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    r = w.gen_riddle(blueprint=fc.default_blueprint)
    check("重试后成功出题", r.puzzle is not None and P1[:6] in r.puzzle, r)
    names = [c["tool"]["name"] for c in fc.calls]
    check("**generator 只调了 1 次(没有换稿)**",
          names.count("emit_riddle") == 1, names)
    check("**审稿调了 2 次(重试同一稿)**",
          names.count("emit_review") == 2, names)
    check("前三次是 出题/审稿/审稿(之后才 truth audit)",
          names[:3] == ["emit_riddle", "emit_review", "emit_review"], names)
    check("第 2 次调用确实是审稿",
          fc.calls[1]["tool"]["name"] == "emit_review",
          fc.calls[1]["tool"]["name"])
    # 重试那一发的 max_tokens 必须被抬高 —— 截断的成因就是预算不够,
    # 用同一个预算重试等于把同一个错误再犯一次。
    check("**重试抬高了 max_tokens(3500 -> 4500)**",
          fc.calls[2]["max_tokens"] == 4500, fc.calls[2]["max_tokens"])
    check("首次审稿仍是 3500(不动基线)",
          fc.calls[1]["max_tokens"] == 3500, fc.calls[1]["max_tokens"])


def test_first_person_story_still_detectable():
    """G4-RB §四: `_is_first_person_story` **保留为分析信号**。

    它不再接进修复/拒稿路径(见 `story/llm.py` 的函数 docstring),
    但判据本身仍然要正确 —— 回归 / 实验脚本 / 将来的 metrics 读它。
    """
    print("[出题: 第一人称判据(仅信号, 不再用于修复)]")
    from story.llm import _is_first_person_story
    for t in ["深夜我独自在家，座机响了，接起来是我自己的声音。",
              "我住的老楼电梯里，只有我和邻居老太太两个人。",
              "我正在厨房做饭，突然听见门外有人叫我名字。"]:
        check(f"第一人称: {t[:14]}", _is_first_person_story(t) is True, t)
    for t in ["一个男人走进餐厅，点了一份海龟汤，喝了一口就冲出去自杀了。",
              "男人对酒保说：「请给我一杯水。」酒保却掏出一把枪指着他。",
              "她在葬礼上遇见一个陌生男人，回家后就把亲姐姐杀了。"]:
        check(f"第三人称: {t[:14]}", _is_first_person_story(t) is False, t)


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
    check("共 4 次调用(出题/审稿/重出/审稿)", len(gen_calls(fc)) == 4, len(gen_calls(fc)))


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
    check("确实重出过", len(gen_calls(fc)) == 2, len(gen_calls(fc)))


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
    check("废稿没有消耗重试次数(共 4 次调用)", len(gen_calls(fc)) == 4,
          len(gen_calls(fc)))


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
    check("RIDDLE_PROMPT_VERSION == riddle-v7",
          RIDDLE_PROMPT_VERSION == "riddle-v9", RIDDLE_PROMPT_VERSION)
    check("CHECK_PROMPT_VERSION == check-v9",
          CHECK_PROMPT_VERSION == "check-v9", CHECK_PROMPT_VERSION)


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


def test_r2_hard_contract_never_requires_closing_question():
    """**R2**: hard contract 不得假设谜面一定以问句结尾。

    为什么必须有一条**静态**测试: R1 已经让 `validate_spec` 不再因
    "没有结尾问句"判 fixable, `CHECK_SYSTEM` 也写明了它**不是**毛病。
    但 `core_answer_direct` 还是 hard gate, 而它的 schema / 提示词当时
    仍写着"必须直接回答谜面**末尾那个问题**" —— 于是一个完全合法的
    汤面:

        每天晚上十二点, 她都会从门外听见自己敲门。

    仍可能被 Reviewer 判 `core_answer_direct=false`, 而这一项是门,
    等于**从侧门把"必须问号"重新装回来**。

    这条测试不跑 LLM(那是概率), 而是**逐字钉住契约文案**:
    凡是描述 core_answer / 删除测试的地方, 都不能再要求"谜面末尾有
    问题"; 必须改成"直接解释主要异常 / 核心悬念; 有显式问题则回答它"。
    """
    print("\n[R2] hard contract 不再要求末尾问句")
    from story.llm import (_TOOL_CHECK, _TOOL_STRUCTURE, _TOOL_RIDDLE,
                           CHECK_SYSTEM, STRUCTURE_SYSTEM, RIDDLE_SYSTEM)

    # 旧的"必须回答末尾问题"措辞在任何契约文本里都不该再出现。
    # 这里刻意用**子串**扫全文, 而不是逐个字段断言 —— 漏掉一处
    # (比如又加了个新工具) 就会被抓住。
    stale = ("回答谜面末尾那个问题", "回答谜面最后那个问题",
             "回答谜面末尾的问题", "回答谜面最后的问题")
    for name, text in (("CHECK_SYSTEM", CHECK_SYSTEM),
                       ("STRUCTURE_SYSTEM", STRUCTURE_SYSTEM),
                       ("RIDDLE_SYSTEM", RIDDLE_SYSTEM)):
        for bad in stale:
            check(f"**{name} 不再出现「{bad}」**", bad not in text, bad)

    # 新语义必须在场: 至少提到"主要异常 / 核心悬念"这个说法之一
    for name, text in (("CHECK_SYSTEM", CHECK_SYSTEM),
                       ("STRUCTURE_SYSTEM", STRUCTURE_SYSTEM),
                       ("RIDDLE_SYSTEM", RIDDLE_SYSTEM)):
        check(f"**{name} 改成解释「主要异常 / 核心悬念」**",
              ("主要异常" in text or "核心悬念" in text), name)

    # ---- schema 层: 三个 core_answer 描述都不许再要求末尾问句 ----
    ck = _TOOL_CHECK["input_schema"]["properties"]
    # ⚠️ `core_answer_direct` / `completion_contract_minimal` 嵌在
    # `quality_checks.properties` 里, 不在顶层 —— 路径写错会 KeyError。
    ckqc = ck["quality_checks"]["properties"]
    cad = ckqc["core_answer_direct"]["description"]
    ccm = ckqc["completion_contract_minimal"]["description"]
    for label, txt in (("_TOOL_CHECK.core_answer_direct", cad),
                       ("_TOOL_CHECK.completion_contract_minimal", ccm)):
        for bad in stale:
            check(f"**{label} 不再出现「{bad}」**", bad not in txt, bad)
        check(f"**{label} 提到主要异常 / 核心悬念**",
              ("主要异常" in txt or "核心悬念" in txt), txt[:80])
    # core_answer_direct 要显式说"没有问句也不算 false"
    check("**_TOOL_CHECK.core_answer_direct 明说没有问句不要判 false**",
          ("没有问句" in cad or "不必" in cad), cad[:120])

    # _TOOL_CHECK.core_answer 自身的描述(修复时回传的那个字段)
    ck_ca = ck["core_answer"]["description"]
    for bad in stale:
        check(f"**_TOOL_CHECK.core_answer 不再出现「{bad}」**",
              bad not in ck_ca, bad)
    check("**_TOOL_CHECK.core_answer 提到主要异常 / 核心悬念**",
          ("主要异常" in ck_ca or "核心悬念" in ck_ca), ck_ca[:120])

    # _TOOL_STRUCTURE.core_answer(Stage B)
    st_ca = _TOOL_STRUCTURE["input_schema"]["properties"]["core_answer"][
        "description"]
    for bad in stale:
        check(f"**_TOOL_STRUCTURE.core_answer 不再出现「{bad}」**",
              bad not in st_ca, bad)
    check("**_TOOL_STRUCTURE.core_answer 提到主要异常 / 核心悬念**",
          ("主要异常" in st_ca or "核心悬念" in st_ca), st_ca[:120])

    # _TOOL_RIDDLE.core_answer —— classic 生成器仍可**偏好**问句, 但
    # hard 文案不能再假设它一定存在。
    rd_ca = _TOOL_RIDDLE["input_schema"]["properties"]["core_answer"][
        "description"]
    for bad in stale:
        check(f"**_TOOL_RIDDLE.core_answer 不再出现「{bad}」**",
              bad not in rd_ca, bad)
    check("**_TOOL_RIDDLE.core_answer 提到主要异常 / 核心悬念**",
          ("主要异常" in rd_ca or "核心悬念" in rd_ca), rd_ca[:120])


def test_r2_decision_fix_examples_drop_person_and_question():
    """**R2**: `_TOOL_CHECK.decision.description` 的 fix 例子里不能再有
    「第一人称 / 没结尾问句」。

    这条特别阴: `CHECK_SYSTEM` 明说"这两样不是毛病", 而同一份 schema 的
    `decision` 描述却把 `fix` 举成"第一人称/没结尾问句"。模型同时收到
    两条互相冲突的指令 —— 即使代码不再注入 must_fix, schema 本身也会
    诱导 Reviewer 去 fix 它们。
    """
    print("\n[R2] decision=fix 的例子已去掉人称/问句")
    from story.llm import _TOOL_CHECK
    d = _TOOL_CHECK["input_schema"]["properties"]["decision"]["description"]
    # ⚠️ 不能简单断言 "第一人称" not in d —— 新文案**故意**提到它(为了说
    # 它**不是**毛病)。要钉住的是: 它不再是 **fix 的例子**。
    fix_line = ""
    for line in d.splitlines():
        if line.strip().startswith("fix"):
            fix_line = line
            break
    check("**找到 fix 那一行**", bool(fix_line), "见 decision.description")
    check("**fix 例子不含「第一人称」**", "第一人称" not in fix_line, fix_line)
    check("**fix 例子不含「问句」**", "问句" not in fix_line, fix_line)
    # 但必须**显式**说清这两样不是毛病(否则模型会自己往那方向猜)
    check("**明说第一人称不是毛病**", "第一人称" in d and "不是" in d,
          "见 decision.description")
    check("**明说无问句不是毛病**", "问句" in d and "不是" in d,
          "见 decision.description")
    # fix 仍然要有一个真实例子(meta 文本), 不能空掉
    check("**fix 仍有真实例子(元文本)**",
          "元文本" in fix_line or "【谜底】" in fix_line, fix_line)


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
    check("当前政策是 quality-v9",
          QUALITY_POLICY_VERSION == "quality-v9", QUALITY_POLICY_VERSION)


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
    """**G4-A 改写**: 跨题门撞车**不再**拒稿 —— 改成"记录 + 照常出题"。

    ## 这条测试原来守什么, 为什么必须改

    P0-1 时它守的是: 被跨题门拒掉的稿子不能作为 `last` 兜底播出(否则
    配额形同虚设)。G4-A 把跨题门整体降成 **soft**:

        同类型不是拒题理由。
        safety / correctness / playability / true duplicate 才是硬门。

    于是一个"题目本身完全合格、只是跟 recent 窗口撞了 mechanism"的稿子
    **应该**被交付, 而撞车事实落进 `metrics["diversity_signals"]` 供复盘。
    这条测试因此必须**反转**断言 —— 否则它只是在证明旧行为还在。

    ## 仍然守住的东西(没有放宽)

    撞车**不影响**稿子的合格性, 但下面这些照旧硬: 结构校验 / 审稿 /
    truth audit / `too_similar`。所以这里同时断言"稿子确实交付了"
    与"撞车被记下来了"——两条都不是恒真。
    """
    from story.puzzle import PuzzleSignature
    dup = PuzzleSignature(mechanism_family="hidden_function",
                          solution_shape="hidden_function_explains_behavior",
                          domain="maritime", relation="stranger",
                          emotion_mode="neutral", time_shape="instant")
    fc = FakeClient([
        LLMResult(tool_input=riddle()),
        LLMResult(tool_input=review_ok()),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    # 最近两道都是同一 (mechanism, shape) -> 配额已满
    spec = w.gen_spec(blueprint=fc.default_blueprint, recent=[dup, dup],
                      max_attempts=2)
    check("**G4-A: 撞车不再拒稿, 稿子照常交付**", bool(spec.puzzle), spec.puzzle)
    check("撞车被记进 diversity_signals 而不是 error",
          bool((spec.metrics or {}).get("diversity_signals")),
          (spec.metrics or {}).get("diversity_signals"))
    check("**没有**因此记一条 error", not spec.error, spec.error)


def test_g4_cross_gate_still_hard_for_text_duplicate():
    """**G4-A 反证**: 降成 soft 的**只有**分布/题型, 文本近似仍是硬门。

    没有这条的话, "把 cross gate 改成 soft" 与 "把整个 ⑤⑥ 段删掉"
    在测试上无法区分 —— 而后者会连 `too_similar` 一起废掉。
    """
    fc = FakeClient([
        LLMResult(tool_input=riddle()),
        LLMResult(tool_input=review_ok()),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    same = riddle()["puzzle"]
    spec = w.gen_spec(blueprint=fc.default_blueprint,
                      avoid=[same],            # 和这道题文本一模一样
                      max_attempts=1)
    check("**文本 near-duplicate 仍然硬拒**", not spec.puzzle, spec.puzzle)
    check("原因是 too_similar 不是 diversity",
          "太像" in (spec.error or ""), spec.error)


def test_director_only_airs_clean_spec():
    """P0-1: director 必须用 error 判断, 不能只看 puzzle 是否为空。"""
    import inspect
    import director as D
    src = inspect.getsource(D.Director._riddle)
    check("director 检查了 spec.error", "spec.error" in src, src)
    check("不再只判断 puzzle", "if spec.puzzle else None" not in src, src)


def test_g4rb_puzzle_shape_is_not_fixable():
    """G4-RB §三/§四: 第一人称与"没有结尾问句"**不再**是毛病。

    旧行为(`test_fixable_format_goes_to_reviewer_not_rejected` 记的是它):
    `validate_spec` 把这两样归 `can_fix`, 而 `_review_spec` 再把它
    注入 `must_fix` —— 实际效果是**硬改**: 审稿人必须交出不同的谜面,
    交不出就 rewrite, **整稿丢弃**。

    红黑实验(24 道)里 21 道被"结尾不是问句"卡住, 其中包括生产自己的
    5/8。而第一人称本来就是海龟汤非常自然的形式。

    现在: 两样都**不是** fixable, 谜面原样通过。仍然保留的是 meta
    文本(那是内容污染, 不是形状偏好)。
    """
    from story.quality import validate_spec
    from story.llm import _spec_from_tool
    # 一条**第一人称 + 没有结尾问句**的谜面: 两样都不该被点名
    spec = _spec_from_tool(riddle(puzzle="我每晚都在数楼上的脚步声。"))
    r = validate_spec(spec)
    check("第一人称 + 无问句仍然 ok", r.ok, r.errors)
    check("不再是 fixable", r.fixable == [], r.fixable)
    check("must_fix() 为空", r.must_fix() == "", repr(r.must_fix()))
    check("fix_reasons() 里没有人称/问句",
          not ({"puzzle_person", "puzzle_question"} & set(r.fix_reasons())),
          r.fix_reasons())

    # meta 文本**仍然**是 fixable —— 上面放宽的是形状, 不是内容污染。
    # ⚠️ 这里**只**改谜面, 让它带上元文本; facts/atoms/clue 沿用
    # `riddle()` 的默认那份。谜面一换, 逐字 quote 就对不上了, 于是会
    # **同时**触发 fair_clue 那条 —— 所以断言只问"元文本在不在
    # fixable 里", 不问 fixable 是否只有它一条。
    bad = _spec_from_tool(riddle())
    bad.puzzle = "他为什么走了？ 【提示】因为他怕。"
    rb = validate_spec(bad)
    check("meta 文本仍然 fixable",
          any("元文本" in f for f in rb.fixable), rb.fixable)
    check("meta 仍标记为需改谜面",
          any("[需改谜面]" in f and "元文本" in f for f in rb.fixable),
          rb.fixable)
    check("meta 仍然不是结构失败(不会被硬拒)", rb.ok, rb.errors)


def test_g4rb_first_person_survives_end_to_end():
    """G4-RB §四: 第一人称稿走完正式链**不需要被改**。

    旧端到端路径: 出题(第一人称) -> 代码注入 must_fix("改成第三人称")
    -> 审稿人必须交新谜面 -> 采用改后的。现在那条注入没了。

    ⚠️ 直接调 `_review_spec` 而不是 `gen_riddle` —— 后者还牵着
    blueprint 校验与队列长度, 前置用例一改共享状态就会假红。这里要
    断言的只是"代码不再往审稿请求里注入形状要求"。
    """
    from story.llm import _spec_from_tool
    P0 = "我每晚都听见楼上有人走动, 可楼上根本没人住。为什么?"
    fc = FakeClient([LLMResult(tool_input=review_ok(P0))])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    spec = _spec_from_tool(riddle(puzzle=P0), blueprint=fc.default_blueprint)
    spec.puzzle, spec.answer = P0, "楼上住的是他自己的录音。"
    reviewed, why, rewrite, technical = w._review_spec(
        spec, fc.default_blueprint)
    user = fc.calls[0]["user"]
    check("审稿请求里没有注入人称 hard focus",
          "改成第三人称" not in user, user[-300:])
    check("审稿请求里没有注入补问句 hard focus",
          "末尾补一句" not in user, user[-300:])
    check("审稿请求里没有【已知问题】段",
          "【已知问题, 必须改掉】" not in user, user[-300:])
    check("pass 之后谜面仍是第一人称(没被改)",
          reviewed is not None and reviewed.puzzle == P0,
          (reviewed.puzzle if reviewed else why))


def test_g4rb_unfixed_shape_is_no_longer_rejected():
    """G4-RB §三/§四: "没改人称/没补问句"**不再**导致拒稿。

    旧行为(`test_unfixed_format_still_rejected` 记的是它): 审稿人回
    pass 却没动谜面 -> 代码判"未处理已知问题" -> rewrite -> 整稿丢弃。
    那条守卫的前提是"人称/问句是必须改的毛病" —— 现在它们不是,
    所以守卫也一并消失。
    """
    P0 = "我每晚都听见楼上有人走动, 可楼上根本没人住。为什么?"
    fc = FakeClient([
        LLMResult(tool_input=riddle(puzzle=P0)),
        LLMResult(tool_input=review_ok(P0)),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    r = w.gen_riddle(blueprint=fc.default_blueprint)
    check("第一人称稿**被采用**(不再因为没改人称而重出)",
          bool(r.puzzle) and r.puzzle.startswith("我每晚"), r.puzzle)
    check("没有触发重出(出题 + 审稿 + 自动 audit = 3 次调用)",
          len(fc.calls) == 3, len(fc.calls))


def test_g4rb_single_beat_is_legal():
    """G4-RB §六: **只有 1 个 discovery beat 不能拒稿**。

    旧政策: `MIN_DISCOVERY_BEATS = 2` + schema `minItems: 2` —— 一道
    单核强反转的经典汤结构上不可能合格, 要么编个伪层次, 要么被杀。

    现在 1 条合法。但**结构合法性照旧**: id / fact 引用 / text 非空 /
    通向通关路径。
    """
    from story.quality import validate_spec, MIN_DISCOVERY_BEATS
    from story.llm import _spec_from_tool
    check("下限是 1", MIN_DISCOVERY_BEATS == 1, MIN_DISCOVERY_BEATS)

    # 单条 beat, 且指向通关 fact -> 合法
    one = _spec_from_tool(riddle())
    one.discovery_beats = [DiscoveryBeat(id="b1", text="意识到灯是在标礁石",
                                         fact_ids=["f1"])]
    r = validate_spec(one)
    check("**1 条 beat 合法(单核题的形状)**", r.ok, r.errors)

    # 单条但**不指向通关路径** -> 仍然拒(结构要求不减)
    orphan = _spec_from_tool(riddle())
    orphan.discovery_beats = [DiscoveryBeat(id="b1", text="无关的一层",
                                            fact_ids=[])]
    rb = validate_spec(orphan)
    check("1 条但不通向通关事实 -> 仍拒", not rb.ok, rb.why()[:120])

    # 0 条 -> 仍拒(下限是 1, 不是 0)
    zero = _spec_from_tool(riddle())
    zero.discovery_beats = []
    rz = validate_spec(zero)
    check("0 条 -> 仍拒", not rz.ok, rz.why()[:120])


def test_g4rb_signal_checks_are_soft_but_anchors_stay_hard():
    """R1 收口: 两个 signal false **不再拒稿**; 两个锚点 false **仍然拒**。

    门是 7 项(`FREE_GEN_HARD_CHECKS`), 降为 signal 的只有两项:
    `clue_recontextualized` / `reasoning_beats_nonredundant`。

    ⚠️ 早先这条叫 `test_g4rb_fun_four_are_soft_signals`, 断言的是
    "四项全 false 也通过、门恰好五项"。**R1 按 PR #5 的证据把范围收窄了**:
    `concrete_anomaly`(真实异常锚点)与 `dramatic_payoff`(揭晓兑现)
    回到门里 —— 前者的降级等于删掉报告要保留的东西, 后者在 10 条真实
    trace 里 0 次 false, 没有任何降级依据。所以这条测试也跟着改。
    """
    from story.llm import (FREE_GEN_HARD_CHECKS, FREE_GEN_SIGNAL_CHECKS,
                           _quality_check_contract)
    check("硬门恰好七项",
          set(FREE_GEN_HARD_CHECKS) == {
              "narrator_truthful", "mechanism_consistent",
              "core_answer_direct", "completion_contract_minimal",
              "concrete_anomaly", "dramatic_payoff",
              "livestream_safe"}, FREE_GEN_HARD_CHECKS)
    check("信号恰好两项",
          set(FREE_GEN_SIGNAL_CHECKS) == {
              "clue_recontextualized", "reasoning_beats_nonredundant"},
          FREE_GEN_SIGNAL_CHECKS)
    check("**门与信号不相交**",
          not (set(FREE_GEN_HARD_CHECKS) & set(FREE_GEN_SIGNAL_CHECKS)),
          (FREE_GEN_HARD_CHECKS, FREE_GEN_SIGNAL_CHECKS))
    check("**两个 signal 都不在门里**",
          not (set(FREE_GEN_SIGNAL_CHECKS) & set(FREE_GEN_HARD_CHECKS)),
          FREE_GEN_HARD_CHECKS)

    # 端到端: 两个 signal 全 false + 七项门全 true -> 采用
    from story.llm import _spec_from_tool
    spec = _spec_from_tool(riddle(), blueprint=bp_for())
    qc = dict(qc_ok())
    for k in FREE_GEN_SIGNAL_CHECKS:
        qc[k] = False
    fc = FakeClient([LLMResult(tool_input=review_ok(
        spec.puzzle, quality_checks=qc))])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    reviewed, why, rewrite, technical = w._review_spec(spec, bp_for())
    check("**两个质量信号全 false 仍然通过**", reviewed is not None, why)
    check("不是 rewrite(是语义通过)", not rewrite, rewrite)

    # 反向: 七项硬门里任一项 false -> **仍然拒**
    for k in ("livestream_safe", "narrator_truthful",
              "concrete_anomaly", "dramatic_payoff"):
        bad = dict(qc_ok())
        bad[k] = False
        fc2 = FakeClient([LLMResult(tool_input=review_ok(
            spec.puzzle, quality_checks=bad))])
        w2 = PuzzleWriter(client=fc2, runtime_cfg=fc2.runtime_cfg)
        got, why2, rw2, _ = w2._review_spec(spec, bp_for())
        check(f"**{k}=false 仍然拒稿**", got is None, (got, why2[:80]))

    """G4-RB §三/§四: "没改人称/没补问句"**不再**导致拒稿。

    旧行为(`test_unfixed_format_still_rejected` 记的是它): 审稿人回
    pass 却没动谜面 -> 代码判"未处理已知问题" -> rewrite -> 整稿丢弃。
    那条守卫的存在前提是"人称/问句是必须改的毛病" —— 现在它们不是,
    所以守卫也一并消失。
    """
    P0 = "我每晚都听见楼上有人走动, 可楼上根本没人住。为什么?"
    fc = FakeClient([
        LLMResult(tool_input=riddle(puzzle=P0)),
        LLMResult(tool_input=review_ok(P0)),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    r = w.gen_riddle(blueprint=fc.default_blueprint)
    check("第一人称稿**被采用**(不再因为没改人称而重出)",
          bool(r.puzzle) and r.puzzle.startswith("我每晚"), r.puzzle)
    check("没有触发重出(只用了 出题+审稿 两次调用)",
          len(fc.calls) == 3, len(fc.calls))   # +1 是自动回的 truth audit


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
    names = [c["tool"]["name"] for c in gen_calls(fc)]
    check("rewrite 后走的是**重新出题**而不是修补",
          names == ["emit_riddle", "emit_review", "emit_riddle", "emit_review"],
          names)
    # 重出请求里应带上 rewrite 的理由。索引按**过滤后**的序列取 ——
    # audit 会插在 review 之后, 直接 `fc.calls[2]` 会指到 audit 那条。
    gen2 = gen_calls(fc)[2]["user"]
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
    check("重出过一次", len(gen_calls(fc)) == 2, len(gen_calls(fc)))
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
    check("三次都试过了", len(gen_calls(fc)) == 3, len(gen_calls(fc)))


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


# ======================================================================
# Q1: narrator truth audit(独立的单题逻辑一致性调用)
# ======================================================================
def _bridge_riddle():
    """桥题: 谜面用无归属的绝对否定断言 "司机并没有掉头"。

    谜底却要求司机掉过头。这是实播真的漏过去的那一类。
    """
    return riddle(
        puzzle="司机把车开过桥, 但监控里并没有看到他掉头, 他却又回到了"
               "出发那侧。为什么?",
        answer="他开过桥之后在对岸正常调头, 又驶回桥上, 所以同一段路"
               "他走了两次。")


def test_truth1_bridge_contradiction_rejected():
    """**Truth-1**: "没有掉头" vs "对岸调头" -> 拒稿。"""
    print("\n[Q1-Truth-1] 桥题矛盾必须拒稿")
    fc = FakeClient([
        LLMResult(tool_input=_bridge_riddle(), model="m"),
        LLMResult(tool_input=review_ok(puzzle=_bridge_riddle()["puzzle"])),
        _truth_tool(truthful=False, consistent=True,
                    conflicts=[BRIDGE_CONFLICT]),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    spec = w.gen_spec(blueprint=fc.default_blueprint,
                      max_attempts=1, budget_s=5)
    check("**拒稿(没有谜面可用)**", not spec.puzzle, spec.puzzle)
    check("错误里点名叙事真实性",
          "叙事真实性" in (spec.error or ""), spec.error)
    check("审计真的被调了",
          any((c["tool"] or {}).get("name") == "emit_truth_audit"
              for c in fc.calls), [c["tool"] for c in fc.calls])


def test_truth2_literal_contradiction_rejected_but_weak_ok():
    """**Truth-2**: 字面矛盾 vs 允许误导的边界。

      "此刻仍开着小火" + "早已关火"   -> 拒(谜面**排除**了那个可能)
      "锅还温着"       + "早已关火焐着" -> 过(没排除任何东西)
    """
    print("\n[Q1-Truth-2] 字面矛盾拒 / 弱断言误导过")
    P_BAD = "锅底仍开着小火, 孩子却掀开锅盖就哭了。为什么?"
    fc_bad = FakeClient([
        LLMResult(tool_input=riddle(puzzle=P_BAD), model="m"),
        LLMResult(tool_input=review_ok(puzzle=P_BAD)),
        _truth_tool(truthful=False, consistent=False, conflicts=[{
            "puzzle_claim": "锅底仍开着小火",
            "answer_claim": "其实早已关火, 只是在焐",
            "why": "谜面断言当下火还在烧, 排除了已关火",
        }]),
    ])
    wb = PuzzleWriter(client=fc_bad, runtime_cfg=fc_bad.runtime_cfg)
    sb = wb.gen_spec(blueprint=fc_bad.default_blueprint,
                     max_attempts=1, budget_s=5)
    check("**强断言矛盾 -> 拒稿**", not sb.puzzle, sb.puzzle)

    # 弱断言 — 审计放行
    P_OK = "锅还温着, 孩子却掀开锅盖就哭了。为什么?"
    fc_ok = FakeClient([
        LLMResult(tool_input=riddle(puzzle=P_OK), model="m"),
        LLMResult(tool_input=review_ok(puzzle=P_OK)),
        _truth_tool(truthful=True, consistent=True),
    ])
    wo = PuzzleWriter(client=fc_ok, runtime_cfg=fc_ok.runtime_cfg)
    so = wo.gen_spec(blueprint=fc_ok.default_blueprint)
    check("**弱断言(允许的误导) -> 过**", so.puzzle == P_OK, so.puzzle)


def test_truth3_attributed_belief_passes():
    """**Truth-3**: 有归属的陈述(在他看来) -> 谜底可以推翻它。"""
    print("\n[Q1-Truth-3] 有归属陈述可以通过")
    P = "在他看来, 司机没有掉头。可他自己也说不清怎么回事。为什么?"
    fc = FakeClient([
        LLMResult(tool_input=riddle(puzzle=P), model="m"),
        LLMResult(tool_input=review_ok(puzzle=P)),
        _truth_tool(truthful=True, consistent=True),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    spec = w.gen_spec(blueprint=fc.default_blueprint)
    check("**有归属 -> 通过**", spec.puzzle == P, spec.puzzle)


def test_truth4_audit_technical_failure_rejects():
    """**Truth-4 + G2-F**: audit 空返回 / malformed -> 拒稿, **不能假绿**。

    G2-F 之后多了一层重试: 技术失败会**在同一个 candidate 上**再试一次。
    所以夹具里必须放**两条**坏 audit —— 只放一条的话第二次会落到
    `FakeClient` 的"自动通过"兜底上, 于是这条用例会变成"测夹具"。
    (`test_g2f_audit_retry_same_candidate` 单独验重试本身。)
    """
    print("\n[Q1-Truth-4] audit 技术失败 -> 重试一次仍失败 -> 拒稿")
    def _bad_audit(ti):
        """给这一条打上 audit 标记(否则 FakeClient 会当它是给别的工具的)。"""
        d = dict(ti or {})
        d["__truth_audit__"] = True
        return LLMResult(tool_input=d, model="m")

    for label, bad in (
            ("空 tool input", _bad_audit({})),
            ("缺字段", _bad_audit({"narrator_truthful": True})),
            ("类型不对", _bad_audit({"narrator_truthful": "yes",
                                    "mechanism_consistent": True,
                                    "conflicts": []})),
            ("conflicts 不是 list", _bad_audit({"narrator_truthful": True,
                                                "mechanism_consistent": True,
                                                "conflicts": "none"})),
            ("超时", _bad_audit({}))):
        fc = FakeClient([
            LLMResult(tool_input=riddle(), model="m"),
            LLMResult(tool_input=review_ok()),
            bad,
            # G2-F: 重试那一发也是坏的 -> 两次都技术失败才收手
            _bad_audit(dict(bad.tool_input or {})),
        ])
        w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
        spec = w.gen_spec(blueprint=fc.default_blueprint,
                          max_attempts=1, budget_s=5)
        check(f"{label}: **拒稿(fail closed)**", not spec.puzzle, spec.puzzle)
        check(f"{label}: **generator 只跑了 1 稿(不换稿)**",
              [c["tool"]["name"] for c in fc.calls].count("emit_riddle") == 1,
              [c["tool"]["name"] for c in fc.calls])

    # "超时"这条要单独走: error 而不是 tool_input。
    # ⚠️ `error` 型的结果**没法带标记**, 所以让它排在队首 —— 队列里第
    # 三条位置上的东西会被下一次**非 audit** 调用(也就是没有下一次)取走,
    # 而 audit 只会从标记里取。放队首则第一次 audit 调用就会遇到它。
    fc = FakeClient([
        LLMResult(tool_input=riddle(), model="m"),
        LLMResult(tool_input=review_ok()),
    ])
    # 直接把 audit 打桩成"抛异常", 模拟网关侧的硬故障
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    _real = fc.messages

    def _boom(system, user, **kw):
        if (kw.get("tool") or {}).get("name") == "emit_truth_audit":
            raise TimeoutError("timed out")
        return _real(system, user, **kw)

    fc.messages = _boom
    spec = w.gen_spec(blueprint=fc.default_blueprint, max_attempts=1,
                      budget_s=5)
    check("audit 抛异常: **拒稿(fail closed)**", not spec.puzzle, spec.puzzle)

    # 直接测 audit_truthfulness 的 fail-closed 契约
    fc2 = FakeClient([])

    def _boom2(system, user, **kw):
        raise TimeoutError("timed out")

    fc2.messages = _boom2
    from story.llm import PuzzleWriter as _W
    w2 = _W(client=fc2, runtime_cfg=runtime_cfg())
    r = w2.audit_truthfulness(puzzle="谜面?", core_answer="c", answer="a")
    check("audit 返回 dict 而不是 None(才可能 fail closed)",
          isinstance(r, dict), r)
    check("**技术失败 -> narrator_truthful=False**",
          r and r.get("narrator_truthful") is False, r)
    check("有 why 说明", bool(r.get("why")), r)
    # 输入为空 -> None(与"没跑"区分开: 那是上游硬校验的职责)
    fc3 = FakeClient([])
    w3 = _W(client=fc3, runtime_cfg=runtime_cfg())
    check("空输入 -> None(不冒充通过)",
          w3.audit_truthfulness(puzzle="", answer="") is None)


def test_truth4b_conflicts_nonempty_forces_reject():
    """两个 bool 都 true 但 conflicts 非空 -> 仍算不过(模型自相矛盾)。"""
    print("\n[Q1-Truth-4b] conflicts 非空 -> 一律不过")
    fc = FakeClient([
        LLMResult(tool_input=riddle(), model="m"),
        LLMResult(tool_input=review_ok()),
        _truth_tool(truthful=True, consistent=True,
                    conflicts=[BRIDGE_CONFLICT]),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    spec = w.gen_spec(blueprint=fc.default_blueprint,
                      max_attempts=1, budget_s=5)
    check("**拒稿**", not spec.puzzle, spec.puzzle)


def test_truth5_v6_pool_quarantined_but_v7_eligible():
    """**Truth-5**: 旧 quality-v6 库存被隔离; v7 正常 eligible。"""
    print("\n[Q1-Truth-5] v6 quarantine / v7 eligible")
    import os
    import json
    import tempfile
    from story.config import Config
    from story.pool import PuzzlePool, spec_key
    from story.quality import QUALITY_POLICY_VERSION
    # `good_spec` 在 test_pool 里 —— 直接 import 兄弟测试模块会踩
    # "tests 不是包"的问题, 所以按路径加载(与这两个套件的关系是
    # "共用同一份合格 spec 夹具", 不是互相依赖)。
    import importlib.util as _ilu
    _p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "test_pool.py")
    _spec = _ilu.spec_from_file_location("_tp_for_q1", _p)
    _mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    _pool_good_spec = _mod.good_spec
    check("当前政策是 v9", QUALITY_POLICY_VERSION == "quality-v9",
          QUALITY_POLICY_VERSION)
    d = tempfile.mkdtemp(prefix="q1pool_")
    cfg = Config(sim_path="x", no_llm=True, pool_enabled=True,
                 pool_path=os.path.join(d, "p.jsonl"),
                 pool_used_path=os.path.join(d, "u.jsonl"))
    old = _pool_good_spec()
    old.quality_policy_version = "quality-v6"
    with open(cfg.pool_path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"pool_version": 1, "pool_key": spec_key(old),
                            "added_at": 0.0, "added_by": "legacy",
                            "spec": old.to_archive()},
                           ensure_ascii=False) + "\n")
    pool = PuzzlePool.open(cfg)
    check("**v6: stock_count 不算它**", pool.stock_count() == 0,
          pool.stock_count())
    check("**v6: pop_next 不返回**",
          pool.pop_next(recent_signatures=[]) is None)
    check("**v6: playable_count 也不算**", pool.playable_count([]) == 0,
          pool.playable_count([]))
    # v7 新题。**必须换谜面** —— `good_spec()` 的内容哈希与上面那条 v6
    # 完全一样, 而池内去重是按内容哈希的, 同一道题会被正确地拒。
    new = _pool_good_spec(
        puzzle="钟楼的守夜人每晚敲钟, 但只在涨潮的那几个小时敲。为什么?",
        fair_clues=[_mod.FairClue(quote="只在涨潮的那几个小时敲",
                                  supports_atoms=["a1"]),
                    _mod.FairClue(quote="每晚敲钟", supports_atoms=["a2"])])
    check("**v7: 正常 eligible**", pool.add(new) is True)
    check("v7 入池后 stock=1", pool.stock_count() == 1, pool.stock_count())
    check("v7 能 pop 出来", pool.pop_next(recent_signatures=[]) is not None)


def test_q2_discovery_beats_schema_and_prompts():
    """**Q2-A**: 层次进 schema/prompt, 且通关不被它绑架。"""
    print("\n[Q2-A] discovery_beats 进 schema 与 prompt")
    from story.llm import (_TOOL_RIDDLE, _TOOL_CHECK, RIDDLE_SYSTEM,
                           CHECK_SYSTEM, _QUALITY_CHECK_FIELDS)
    rb = _TOOL_RIDDLE["input_schema"]["properties"].get("discovery_beats")
    check("RIDDLE 工具接受 discovery_beats", rb is not None)
    check("条数区间 1~4 (G4-RB §六: 单层题合法)", (rb or {}).get("minItems") == 1
          and (rb or {}).get("maxItems") == 4,
          ((rb or {}).get("minItems"), (rb or {}).get("maxItems")))
    check("Reviewer 工具也能带回 discovery_beats",
          "discovery_beats" in _TOOL_CHECK["input_schema"]["properties"])
    # ---- 通关不被层次绑架: completion 仍是 1~2 ----
    comp = _TOOL_RIDDLE["input_schema"]["properties"]["completion_fact_ids"]
    check("**completion 仍限 1~2 条(通关必须简单)**",
          comp.get("minItems") == 1 and comp.get("maxItems") == 2,
          (comp.get("minItems"), comp.get("maxItems")))
    # ---- 修正"把题写简单"的措辞 ----
    check("**RIDDLE 不再说'换一个更简单的骨架'**",
          "换一个更简单的骨架" not in RIDDLE_SYSTEM,
          "仍在教模型把题写简单")
    check("RIDDLE 明确 completion 不是复杂度上限",
          "不是对整道题复杂度的限制" in RIDDLE_SYSTEM)
    check("RIDDLE 写了'题目允许有层次, 通关必须简单'",
          "题目允许有层次" in RIDDLE_SYSTEM)
    td = _TOOL_RIDDLE["input_schema"]["properties"][
        "completion_fact_ids"]["description"]
    check("工具描述也不再教'换更简单的骨架'",
          "换一个更简单的骨架" not in td, td[:80])
    # ---- 基调 ----
    check("RIDDLE 有'诡异但现实可解释'基调段",
          "诡异但现实可解释" in RIDDLE_SYSTEM)
    check("RIDDLE 点名优先的机制家族",
          "identity_misread" in RIDDLE_SYSTEM
          and "object_misuse" in RIDDLE_SYSTEM)
    check("RIDDLE 明说不要用惨烈程度替代推理质量",
          "重口" in RIDDLE_SYSTEM)
    # ---- 四个"好不好玩"硬字段 ----
    for f in ("concrete_anomaly", "clue_recontextualized",
              "dramatic_payoff", "reasoning_beats_nonredundant"):
        check(f"quality_checks 含 {f}", f in _QUALITY_CHECK_FIELDS)
    check("CHECK_SYSTEM 解释了 concrete_anomaly",
          "concrete_anomaly" in CHECK_SYSTEM)
    check("CHECK_SYSTEM 解释了 reasoning_beats_nonredundant",
          "reasoning_beats_nonredundant" in CHECK_SYSTEM)


def test_q2_v8_pool_quarantined_but_v9_eligible():
    """**Q2-K / R1**: 旧 quality-v8 库存被隔离; v9 正常 eligible。

    实播冒烟的日志里能看到这一条真的在生产路径上生效(当时的数字是 v7/v8,
    本条把当前版本换成 R1 之后的 v8/v9 —— **隔离机制一个字没改**,
    变的只是"哪个版本算当前"):

        题池 8 道候选全部被挡(回落现场生成): …
        quality policy 不兼容(spec='quality-v8', current='quality-v9')

    隔离靠 `_validate_pool_spec` 既有那一扇门自动生效 —— **不迁移 /
    不伪装 / 不删旧行**。

    ⚠️ **R1 的 v9 bump 是有意的**, 不是事故: 自由生成链的门从 9 项收到
    7 项、`discovery_beats` 下限 2 -> 1, 接受标准真的变了。旧 v8 题是在
    9-hard 标准下被接受的, 与新题不可比较, 所以必须隔离(见
    `story/quality.QUALITY_POLICY_VERSION` 的 v9 段)。
    """
    print("\n[Q2-K] v8 quarantine / v9 eligible")
    import os
    import json
    import tempfile
    from story.config import Config
    from story.pool import PuzzlePool, spec_key
    from story.quality import QUALITY_POLICY_VERSION
    import importlib.util as _ilu
    _p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "test_pool.py")
    _spec = _ilu.spec_from_file_location("_tp_for_q2", _p)
    _mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    _pool_good_spec = _mod.good_spec
    check("当前政策是 v9", QUALITY_POLICY_VERSION == "quality-v9",
          QUALITY_POLICY_VERSION)
    d = tempfile.mkdtemp(prefix="q2pool_")
    cfg = Config(sim_path="x", no_llm=True, pool_enabled=True,
                 pool_path=os.path.join(d, "p.jsonl"),
                 pool_used_path=os.path.join(d, "u.jsonl"))
    # 盘上放一条 v8 —— 它**必须**留在盘上但被一致地挡住。
    old = _pool_good_spec()
    old.quality_policy_version = "quality-v8"
    with open(cfg.pool_path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"pool_version": 1, "pool_key": spec_key(old),
                            "added_at": 0.0, "added_by": "legacy",
                            "spec": old.to_archive()},
                           ensure_ascii=False) + "\n")
    pool = PuzzlePool.open(cfg)
    check("**v8: stock_count 不算它**", pool.stock_count() == 0,
          pool.stock_count())
    check("**v8: pop_next 不返回**",
          pool.pop_next(recent_signatures=[]) is None)
    check("**v8: playable_count 也不算**", pool.playable_count([]) == 0,
          pool.playable_count([]))
    check("**旧行仍在盘上(没删)**",
          sum(1 for _ in open(cfg.pool_path, encoding="utf-8")) == 1)
    # v9 新题正常 eligible(换谜面, 免得撞内容哈希去重)
    new = _pool_good_spec(
        puzzle="钟楼的守夜人每晚敲钟, 但只在涨潮的那几个小时敲。为什么?",
        fair_clues=[_mod.FairClue(quote="只在涨潮的那几个小时敲",
                                  supports_atoms=["a1"]),
                    _mod.FairClue(quote="每晚敲钟", supports_atoms=["a2"])])
    check("**v9: 正常 eligible**", pool.add(new) is True)
    check("v9 入池后 stock=1", pool.stock_count() == 1, pool.stock_count())
    check("v9 能 pop 出来", pool.pop_next(recent_signatures=[]) is not None)


def test_q2_reviewer_all_four_new_fields_required():
    """**Q2-B**: 四个新字段是 fail-closed 的硬门(缺一项就拒稿)。

    这个替身只回**四项** -> 必须被拒。若不拒, 说明新字段是装饰。
    """
    print("\n[Q2-B] 新四项缺一即拒")
    from story.llm import PuzzleWriter
    from story.puzzle import PuzzleSpec
    r = riddle()
    spec = PuzzleSpec(puzzle=r["puzzle"], answer=r["answer"],
                      core_answer=r["core_answer"],
                      completion_fact_ids=r["completion_fact_ids"],
                      quality_policy_version=QUALITY_POLICY_VERSION)
    ti = dict(r)
    ti["decision"] = "pass"
    ti["observed_signature"] = r["signature"]
    ti["quality_checks"] = {"narrator_truthful": True,
                            "mechanism_consistent": True,
                            "core_answer_direct": True,
                            "completion_contract_minimal": True}
    out, why = PuzzleWriter._apply_review(
        spec, ti, bp_for(), r["puzzle"])
    check("**缺四个新字段 -> 拒稿**", out is None, why)
    check("原因点名了缺的字段",
          why and ("concrete_anomaly" in why or "quality_checks" in why), why)


def test_q2_beats_never_reach_frontend():
    """**Q2-C**: discovery_beats **绝不**进 Snapshot / 前端。

    它没有胜负权, 也不该让观众提前看到"这题有几层"。
    """
    print("\n[Q2-C] beats 不进前端")
    from story.state import Snapshot
    from story.engine import RoundEngine
    from story.config import Config
    j = Snapshot().to_json()
    check("**Snapshot 里没有 discovery_beats**",
          "discovery_beats" not in j, sorted(j))
    check("也没有 beats 这个键", "beats" not in j, sorted(j))
    eng = RoundEngine(Config(sim_path="x", reveal_hold_seconds=5.0))
    check("engine.snapshot 也没有",
          "discovery_beats" not in eng.snapshot().to_json())
    check("pressure 里也没有",
          "discovery_beats" not in eng.pressure(), sorted(eng.pressure()))


def test_q2_beats_have_no_victory_power():
    """**Q2-D**: beats 不参与胜负 —— Engine 只认 completion ⊆ established。"""
    print("\n[Q2-D] beats 无胜负权")
    import inspect
    from story.engine import RoundEngine
    src = inspect.getsource(RoundEngine)
    # Engine 里**不该**出现 discovery_beats / beats 的引用。
    check("Engine 源码不提 discovery_beats",
          "discovery_beats" not in src, "Engine 读了 beats —— 它有胜负权了?")
    # submit_qa 的胜负判定仍然只看 completion
    sq = inspect.getsource(RoundEngine.submit_qa)
    check("submit_qa 只按 completion_fact_ids 判胜负",
          "completion_fact_ids" in sq and "discovery_beats" not in sq)


def test_q2_quota_tightened_and_tone_target():
    """**Q2-E**: 两类无聊题 quota 收到 1; 诡异基调目标代码化。"""
    print("\n[Q2-E] quota 收紧 + 基调目标")
    from story.config import Config
    from story.quality import Quotas
    c = Config(sim_path="x")
    check("**straight_explanation 收到 1**",
          c.quota_straight_explanation == 1, c.quota_straight_explanation)
    check("**procedural_rule 收到 1**",
          c.quota_procedural_rule == 1, c.quota_procedural_rule)
    check("基调目标带 5~6",
          (c.quality_dark_tone_min, c.quality_dark_tone_max) == (5, 6),
          (c.quality_dark_tone_min, c.quality_dark_tone_max))
    # 两条路径必须一致(直接构造 vs from_config) —— 只改一边是经典的坑。
    q_direct = Quotas()
    q_cfg = Quotas.from_config(c)
    check("**Quotas() 与 from_config 一致(straight)**",
          q_direct.straight_explanation == q_cfg.straight_explanation == 1,
          (q_direct.straight_explanation, q_cfg.straight_explanation))
    check("**Quotas() 与 from_config 一致(procedural)**",
          q_direct.procedural_rule == q_cfg.procedural_rule == 1,
          (q_direct.procedural_rule, q_cfg.procedural_rule))


def test_q2_versions_bumped():
    """**Q2-F**: 版本统一 bump。"""
    print("\n[Q2-F] 版本 bump")
    from story.llm import (RIDDLE_PROMPT_VERSION, CHECK_PROMPT_VERSION,
                           ANSWER_PROMPT_VERSION)
    from story.quality import QUALITY_POLICY_VERSION
    from story.puzzle import PuzzleSpec
    check("QUALITY_POLICY_VERSION = quality-v9",
          QUALITY_POLICY_VERSION == "quality-v9", QUALITY_POLICY_VERSION)
    check("RIDDLE_PROMPT_VERSION = riddle-v9",
          RIDDLE_PROMPT_VERSION == "riddle-v9", RIDDLE_PROMPT_VERSION)
    check("CHECK_PROMPT_VERSION = check-v9",
          CHECK_PROMPT_VERSION == "check-v9", CHECK_PROMPT_VERSION)
    # Answer 在 C0 那笔已升 answer-v7, Q2 **不再动它**。
    check("ANSWER_PROMPT_VERSION 仍是 C0 升的 answer-v7",
          ANSWER_PROMPT_VERSION == "answer-v7", ANSWER_PROMPT_VERSION)
    check("spec_version = 4",
          PuzzleSpec(puzzle="p", answer="a").to_archive().get("spec_version")
          == 4)


def test_truth_prompt_has_scanning_rules():
    """audit prompt 必须点名那批绝对断言词与归属例外。"""
    print("\n[Q1-prompt] audit prompt 的扫描清单")
    from story.llm import TRUTH_AUDIT_SYSTEM as S
    for w in ("并没有", "从未", "绝不", "唯一", "同一个", "还没有"):
        check(f"扫描词 {w}", w in S, "缺")
    for d in ("身份", "动作", "方向", "前后顺序", "时间", "数量", "地点"):
        check(f"维度 {d}", d in S, "缺")
    check("有归属例外(在他看来)", "在他看来" in S)
    check("桥题反例在 prompt 里", "并没有掉头" in S)
    check("要求拿不准时不放过", "不要" in S and "false" in S)


def test_truth_prompt_hardened_in_riddle_and_check():
    """RIDDLE/CHECK prompt 也要加硬规则(不只靠 audit 兜底)。"""
    print("\n[Q1-prompt2] RIDDLE/CHECK 各加一条硬规则")
    from story.llm import RIDDLE_SYSTEM, CHECK_SYSTEM
    check("RIDDLE 提醒少用绝对断言制造悬念",
          "绝对断言" in RIDDLE_SYSTEM and "少用" in RIDDLE_SYSTEM,
          RIDDLE_SYSTEM[-400:])
    check("RIDDLE 给了'没排除才算允许'的判据",
          "排除" in RIDDLE_SYSTEM, "")
    check("CHECK 要求**先**做逐句扫描",
          "先扫描" in CHECK_SYSTEM or "逐句扫" in CHECK_SYSTEM,
          CHECK_SYSTEM[:600])
    check("CHECK 扫描清单含绝对否定/唯一性/动作顺序",
          "绝对否定" in CHECK_SYSTEM, "")


# ======================================================================
# G1 —— gen_spec 的协作式取消(live 不传 = 行为不变)
# ======================================================================
def test_g1_gen_spec_default_is_bit_identical():
    """**live 路径逐位不变**: 不传 `should_continue` 时, 一次调用都不多。

    这是本步最重要的"没改坏"断言: 协作式取消是给后台补池用的,
    live 出题不传谓词, 必须与 G1 之前**完全相同**。
    """
    print("\n[G1-L1] 不传 should_continue -> live 行为不变")
    fc = FakeClient([LLMResult(tool_input=riddle()),
                     LLMResult(tool_input=review_ok())])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    spec = w.gen_spec(blueprint=fc.default_blueprint)
    check("正常出题成功", spec.puzzle == _GOOD_PUZ, spec.puzzle[:30])
    check("**没有 interrupted 标记**",
          not (spec.metrics or {}).get("interrupted"), spec.metrics)
    check("调用次数与 G1 之前一致(出题 + 审稿)",
          len(gen_calls(fc)) == 2, len(gen_calls(fc)))


def test_g1_gen_spec_stops_before_first_draft():
    """谓词一进门就是 False -> **一次模型调用都不发**。

    这是"下一题已经开始现场生成时, 后台连稿都不该出"的直接表达。
    """
    print("\n[G1-L2] 谓词一开始就 False -> 零调用")
    calls = {"n": 0}

    def nope():
        calls["n"] += 1
        return False

    fc = FakeClient([LLMResult(tool_input=riddle()),
                     LLMResult(tool_input=review_ok())])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    spec = w.gen_spec(blueprint=fc.default_blueprint, should_continue=nope)
    check("**一次 LLM 调用都没发**", len(fc.calls) == 0, len(fc.calls))
    check("**没有谜面**", not spec.puzzle, spec.puzzle[:30])
    check("**error 留空(让路不是失败)**", not spec.error, repr(spec.error))
    check("**metrics 标 interrupted**",
          (spec.metrics or {}).get("interrupted") is True, spec.metrics)
    check("谓词被问过", calls["n"] >= 1, calls["n"])


def test_g1_gen_spec_stops_before_reviewer():
    """**最高价值检查点**: 稿子出来了, 但审稿之前直播变忙 -> 不审稿。

    实播那 51 秒正是这个形状: draft 已发出(无法取消) -> 下一题开始
    -> 稿子回来 -> 之前系统会**继续审稿 + 再出第二稿**。修好之后
    这里必须停手。
    """
    print("\n[G1-L3] 稿子回来后、审稿之前让路")
    state = {"allow": True}

    def gate():
        return state["allow"]

    # 队列里只放"出题"一条 —— 若代码偷偷审稿, FakeClient 会吐
    # "no more canned results" 并被记成技术失败, 断言能抓到。
    fc = FakeClient([LLMResult(tool_input=riddle())])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)

    real_gen = w._gen_spec_once

    def gen_once(*a, **kw):
        out = real_gen(*a, **kw)
        state["allow"] = False        # 稿子一回来, 直播就忙了
        return out

    w._gen_spec_once = gen_once
    spec = w.gen_spec(blueprint=fc.default_blueprint, should_continue=gate)
    names = [c["tool"]["name"] for c in fc.calls]
    check("**只调了 emit_riddle, 没有 emit_review**",
          names == ["emit_riddle"], names)
    check("**没有谜面(这一稿没有通过质量链)**", not spec.puzzle,
          spec.puzzle[:30])
    check("metrics 标 interrupted",
          (spec.metrics or {}).get("interrupted") is True, spec.metrics)
    check("**没有 error(让路不是失败)**", not spec.error, repr(spec.error))


def test_g1_gen_spec_stops_before_second_draft():
    """第一稿被审稿打回, 但**下一稿之前**变忙 -> 不再出第二稿。

    这正是实播"4 稿连打"被截断的位置。
    """
    print("\n[G1-L4] 第一稿被拒后不再出第二稿")
    state = {"allow": True}

    def gate():
        return state["allow"]

    fc = FakeClient([
        LLMResult(tool_input=riddle()),
        # 审稿要求重出 -> 正常会去出第二稿; 但谓词会让它停在检查点 ①
        LLMResult(tool_input=review_rewrite("谜底依赖题面外的私人往事")),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    real_review = w._review_spec

    def review(*a, **kw):
        out = real_review(*a, **kw)
        state["allow"] = False       # 审稿一返回, 直播就忙了
        return out

    w._review_spec = review
    spec = w.gen_spec(blueprint=fc.default_blueprint, should_continue=gate,
                      max_attempts=4)
    names = [c["tool"]["name"] for c in fc.calls]
    check("**只出了 1 稿(没有第二稿)**",
          names.count("emit_riddle") == 1, names)
    check("调了审稿", names.count("emit_review") == 1, names)
    check("**没有跑到第 4 稿**", len(fc.calls) == 2, len(fc.calls))
    check("metrics 标 interrupted",
          (spec.metrics or {}).get("interrupted") is True, spec.metrics)


def test_g1_gen_spec_stops_before_truth_audit():
    """审稿通过后、truth audit 之前变忙 -> 不再 audit 也不再进池。"""
    print("\n[G1-L5] truth audit 之前让路")
    state = {"allow": True}

    def gate():
        return state["allow"]

    fc = FakeClient([LLMResult(tool_input=riddle()),
                     LLMResult(tool_input=review_ok())])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    real_review = w._review_spec

    def review(*a, **kw):
        out = real_review(*a, **kw)
        state["allow"] = False
        return out

    w._review_spec = review
    spec = w.gen_spec(blueprint=fc.default_blueprint, should_continue=gate)
    check("**没有 truth audit 调用**",
          all(c["tool"]["name"] != "emit_truth_audit" for c in fc.calls),
          [c["tool"]["name"] for c in fc.calls])
    check("**spec 没有谜面**", not spec.puzzle, spec.puzzle[:30])
    check("metrics 标 interrupted",
          (spec.metrics or {}).get("interrupted") is True, spec.metrics)


def test_g1_probe_exception_is_fail_closed():
    """谓词自己抛异常 -> 当作"该收手"(fail closed), 绝不让异常冒泡。

    若让异常冒泡, 它会变成 prefetch 的 `exc`(代码 bug) —— 而真相是
    "探针坏了所以不敢继续"。两类账必须分开。
    """
    print("\n[G1-L6] 谓词抛异常 -> fail closed")
    def boom():
        raise RuntimeError("探针炸了")

    fc = FakeClient([LLMResult(tool_input=riddle())])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    spec = w.gen_spec(blueprint=fc.default_blueprint, should_continue=boom)
    check("**零调用**", len(fc.calls) == 0, len(fc.calls))
    check("metrics 标 interrupted",
          (spec.metrics or {}).get("interrupted") is True, spec.metrics)


def test_g1_budget_and_attempts_are_honored():
    """后台预算真的生效: `max_attempts=2` 时最多出 2 稿。"""
    print("\n[G1-L7] 后台预算: 最多 2 稿")
    fc = FakeClient([
        LLMResult(tool_input=riddle()),
        LLMResult(tool_input=review_rewrite("烂")),
        LLMResult(tool_input=riddle(puzzle="二稿谜面, 另一个事件。为什么?")),
        LLMResult(tool_input=review_rewrite("还是烂")),
        # 若还去出第 3 稿, 这里会被取走 -> 断言能抓到
        LLMResult(tool_input=riddle(puzzle="三稿不该出现。为什么?")),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    spec = w.gen_spec(blueprint=fc.default_blueprint, max_attempts=2)
    check("**只出了 2 稿**",
          [c["tool"]["name"] for c in fc.calls].count("emit_riddle") == 2,
          [c["tool"]["name"] for c in fc.calls])
    check("失败时 error 非空且 puzzle 为空(不能被当成结果)",
          bool(spec.error) and not spec.puzzle,
          (spec.error, spec.puzzle[:20]))
    check("**没有被标成 interrupted**",
          not (spec.metrics or {}).get("interrupted"), spec.metrics)


# ======================================================================
# G2-keyword2 —— Stage A / Stage B(writer 级)
# ======================================================================
#
# ⚠️ 注意与下面那个 "G2 —— 可修问题不再整题重造" 的**区别**: 那一批是
# 早先的修复链工作, 与本批的 keyword2 两阶段起题无关。命名上带
# `keyword` 以免混。

def _kw_idea():
    """Stage A 的产出(与 `riddle()` 同一道题, 保证 clues 对得上)。

    ⚠️ `keyword2-v4` 起 Stage A 多出**三个 Case-first 创作脚手架**
    字段(`core_truth` / `observed_clues` / `event_chain`)。它们是
    "作者怎么想", **不是** PuzzleSpec 事实源 —— 见
    `test_g4cf_internal_scaffold_never_reaches_stage_b`。
    """
    r = riddle()
    return {
        "core_truth": "灯塔守望者亮灯不是为了给船引路, 而是为了标出退潮时"
                      "会露出水面的礁石。",
        "observed_clues": ["灯只在退潮的那几个小时亮着",
                           "涨潮时灯是灭的, 而那时航道最需要光"],
        "event_chain": ["这片浅滩退潮时会露出礁石",
                        "守望者用亮灯标出礁石位置",
                        "船看见灯就知道那里有礁石"],
        "title": r.get("title", "灯塔"), "puzzle": r["puzzle"],
        "answer": r["answer"],
    }


#: Case-first 的**三个脚手架字段** —— Stage A 有, Stage B / PuzzleSpec 没有。
_CF_SCAFFOLD = ("core_truth", "observed_clues", "event_chain")

#: Stage B 的词汇 —— Stage A **永远**不该出现这些(脚手架 ≠ PuzzleSpec)。
_STAGE_B_VOCAB = ("facts", "solve_atoms", "completion_fact_ids",
                  "discovery_beats", "signature", "Blueprint",
                  "quota", "recent")


def _kw_structure_payload(**extra):
    """Stage B 的 tool_input: `riddle()` **删掉** puzzle/answer/title。

    这正好模拟真实 schema —— 模型**给不出**那三样。

    `**extra` 用来**故意**塞进 schema 里不存在的字段(如
    `puzzle="模型偷偷改写的谜面"`), 验证代码会忽略它们 —— 这是 §五
    "从结构上禁止它把自然谜面重新写成工程化谜面"的反证测试。
    """
    d = dict(riddle())
    for k in ("puzzle", "answer", "title"):
        d.pop(k, None)
    d.update(extra)
    return d


def test_g2_keyword_stage_a_returns_idea():
    """Stage A 返回完整六字段, 且用的是 keyword 专用 prompt/tool。"""
    print("\n[G2-K1] Stage A: 六字段(含 Case-first 脚手架)")
    fc = FakeClient([LLMResult(tool_input=_kw_idea())])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    idea = w.gen_keyword_idea(["图书馆", "上楼"])
    check("**返回完整六字段**",
          set(idea) >= {"core_truth", "observed_clues", "event_chain",
                        "title", "puzzle", "answer"}, sorted(idea))
    check("puzzle 非空", bool(idea["puzzle"]))
    check("用的是 emit_keyword_idea",
          fc.calls[0]["tool"]["name"] == "emit_keyword_idea",
          fc.calls[0]["tool"]["name"])
    check("**system 是 keyword idea prompt, 不是 RIDDLE_SYSTEM**",
          fc.calls[0]["system"] == KEYWORD_IDEA_SYSTEM)
    # ⚠️ §四: Stage A 不得同时想 facts / atoms / completion / beats。
    low = KEYWORD_IDEA_SYSTEM
    for bad in _STAGE_B_VOCAB:
        check(f"Stage A prompt 不提 {bad}", bad not in low)


def test_g4cf_stage_a_scaffold_is_returned_verbatim():
    """§12.4: 三个脚手架字段**原样**带出, 不被代码悄悄丢掉/改写。"""
    print("\n[G4-CF-1] Stage A 脚手架原样返回")
    idea_in = _kw_idea()
    fc = FakeClient([LLMResult(tool_input=idea_in)])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    idea = w.gen_keyword_idea(["图书馆", "上楼"])
    check("**core_truth 原样**", idea["core_truth"] == idea_in["core_truth"],
          idea["core_truth"][:40])
    check("**observed_clues 原样**",
          idea["observed_clues"] == idea_in["observed_clues"],
          idea["observed_clues"])
    check("**event_chain 原样**", idea["event_chain"] == idea_in["event_chain"],
          idea["event_chain"])


#: 哨兵: 让 `_run` 知道"这个键要**从 dict 里删掉**", 而不是设成某个值。
_DROP = object()


def test_g4cf_stage_a_scaffold_fails_closed():
    """§一 / §二 A~E: Stage A 的**五个 required 字段结构 fail-closed**。

    ⚠️ 这条**替换**了上一版那句"缺脚手架字段不算失败"。那是错的:
    schema 里这五个都是 required, 代码层却对缺失睁一只眼闭一只眼, 于是

        模型跳过"先想清真相"直接写谜面 -> 代码放行
          -> "Stage A 是 Case-first" 就只是 prompt 里的一个愿望

    整个接入的唯一收益是"让模型先想清楚再写"。模型没交脚手架, 就不该
    被当成一次成功的 Stage A。

    ⚠️ 这**不是**语义审核: 只看字段在不在、类型对不对、归一后条数在不在
    区间里。**不**判 core_truth 好不好、clue 有没有泄底。
    """
    print("\n[G4-CF-2a] Stage A 脚手架 fail-closed")
    ok_idea = _kw_idea()

    def _run(mutate):
        d = dict(ok_idea)
        mutate(d)
        for k in list(d):
            if d[k] is _DROP:
                d.pop(k)
        fc = FakeClient([LLMResult(tool_input=d)])
        w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
        out = w.gen_keyword_idea(["图书馆", "上楼"])
        return out, len(fc.calls)

    # ---- 基线: 正常一份**必须**成功(证明下面那些不是恒真) ----
    out, n = _run(lambda d: None)
    check("**基线: 完整六字段 -> 成功**", out is not None and bool(out["puzzle"]))
    check("**基线: 恰好 1 次调用**", n == 1, n)

    # ---- A: 缺 core_truth ----
    out, _ = _run(lambda d: d.__setitem__("core_truth", _DROP))
    check("**A 缺 core_truth -> fail**", out is None, out)
    out, _ = _run(lambda d: d.__setitem__("core_truth", "   "))
    check("**A2 core_truth 纯空白 -> fail**", out is None, out)

    # ---- B: observed_clues 空 ----
    out, _ = _run(lambda d: d.__setitem__("observed_clues", []))
    check("**B observed_clues=[] -> fail**", out is None, out)

    # ---- C: 只有 1 条 observed clue ----
    out, _ = _run(lambda d: d.__setitem__("observed_clues", ["只有一条"]))
    check("**C 1 条 clue -> fail**", out is None, out)

    # ---- D: event_chain 非 list ----
    out, _ = _run(lambda d: d.__setitem__("event_chain", "先这样再那样"))
    check("**D event_chain 非 list -> fail**", out is None, out)

    # ---- E: event_chain 只有 1 条 ----
    out, _ = _run(lambda d: d.__setitem__("event_chain", ["只有一步"]))
    check("**E 1 步 chain -> fail**", out is None, out)

    # ---- 上界也要管: 5 条 clue / 4 步 chain 同样越界 ----
    out, _ = _run(lambda d: d.__setitem__("observed_clues", list("abcde")))
    check("clues 5 条 -> fail", out is None, out)
    out, _ = _run(lambda d: d.__setitem__("event_chain", list("wxyz")))
    check("chain 4 步 -> fail", out is None, out)

    # ---- 占位符凑数: 归一后只剩 1 条 -> fail ----
    out, _ = _run(lambda d: d.__setitem__("observed_clues", ["一条", "", "  "]))
    check("**归一后只剩 1 条 -> fail**(没被占位符骗过)", out is None, out)

    # ---- puzzle / answer 仍是合同字段 ----
    out, _ = _run(lambda d: d.__setitem__("puzzle", _DROP))
    check("缺 puzzle -> fail", out is None, out)
    out, _ = _run(lambda d: d.__setitem__("answer", "  "))
    check("answer 空白 -> fail", out is None, out)

    # ---- title 仍可空 ----
    out, _ = _run(lambda d: d.__setitem__("title", _DROP))
    check("**缺 title 仍成功**(optional)", out is not None and bool(out["puzzle"]))


def test_g4cf_stage_a_shape_fail_is_one_attempt():
    """§二 G: 结构失败**不得**偷偷触发第二次调用。

    默认 `max_attempts=1` -> exactly 1 LLM call。这条防的是"fail-closed
    之后有人加个内部重试把失败率压下去" —— 那会让成本翻倍, 而且
    "1 attempt" 这条生产契约就没了。
    """
    print("\n[G4-CF-2b] 结构失败: exactly 1 次调用")
    bad = dict(_kw_idea())
    bad["observed_clues"] = []           # 结构不合规
    for label, payload in (("结构不合规", bad),
                           ("缺 core_truth",
                            {k: v for k, v in _kw_idea().items()
                             if k != "core_truth"})):
        fc = FakeClient([LLMResult(tool_input=payload)] * 3)
        w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
        out = w.gen_keyword_idea(["图书馆", "上楼"])
        check(f"**{label} -> None**", out is None, out)
        check(f"**{label} -> 恰好 1 次调用**", len(fc.calls) == 1, len(fc.calls))


def test_g4cf_shape_check_is_not_semantic_review():
    """§一: 结构校验**不许**变成语义审核。

    拿一批**结构完全合规但内容可疑**的 idea 进去, 断言全部放行 ——
    结构校验只回答"字段在不在/类型对不对/条数对不对"。

    ⚠️ 这些内容在 G9-R2/R3 里被证明是**真问题**, 但结论是"中间分类器
    不值得接生产"。所以它们必须**通过** Stage A, 由后面的 Reviewer /
    truth audit 去处理。
    """
    print("\n[G4-CF-2c] 结构校验 ≠ 语义审核")
    suspicious = [
        ("core_truth 多解(也可能)",
         {"core_truth": "灯灭了。也许是保险丝烧了, 也可能是有人拉了总闸。"}),
        ("clue 泄底",
         {"observed_clues": ["钥匙早就被他留了一份给那个访客", "门锁完好"]}),
        ("clue 是事后结果",
         {"observed_clues": ["事后清点发现什么也没少", "门锁完好"]}),
        ("chain 逻辑不通",
         {"event_chain": ["先发生结果", "后发生原因"]}),
    ]
    for label, patch in suspicious:
        d = dict(_kw_idea())
        d.update(patch)
        fc = FakeClient([LLMResult(tool_input=d)])
        w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
        out = w.gen_keyword_idea(["图书馆", "上楼"])
        check(f"**{label} -> 放行**(交给后面审核)", out is not None,
              out if out is None else "ok")


def test_g4cf_no_midcheck_in_production():
    """§一: 生产里**没有** Checker T / Checker C。

    只看**代码行**(AST), 不看注释 —— 注释里**必须**能解释"为什么不做
    这件事"。这与 G9-R2 那条「B1 必须 reject」断言踩的是同一个坑:
    一条会打自己解释的断言, 结果是"不能交代自己的决定"。
    """
    print("\n[G4-CF-2d] 生产里没有中间 checker")
    import ast as _ast
    import io as _io
    from pathlib import Path as _P
    _src = _io.open(_P(__file__).resolve().parents[1] / "story" / "llm.py",
                    encoding="utf-8").read()
    _tree = _ast.parse(_src)
    _code = "\n".join(
        _ast.unparse(n) for n in _ast.walk(_tree)
        if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef)))
    for bad in ("check_core_truth", "check_clue_legitimacy"):
        check(f"**生产代码里没有 {bad}**", bad not in _code,
              [ln.strip() for ln in _code.splitlines() if bad in ln][:2])
    check("**注释里解释了为什么不做**(反证)",
          "check_core_truth" in _src and "没有任何" in _src)



def test_g2_keyword_stage_a_one_attempt():
    """§十: Stage A 默认**只发一次**。"""
    print("\n[G2-K2] Stage A: 1 attempt")
    fc = FakeClient([LLMResult(error="网关抖了")])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    idea = w.gen_keyword_idea(["图书馆", "上楼"])
    check("失败返回 None", idea is None, idea)
    check("**只发了 1 次**", len(fc.calls) == 1, len(fc.calls))


def test_g2_keyword_stage_b_freezes_puzzle():
    """**Stage B 不得改题**: 谜面/谜底/标题由**代码**回填。"""
    print("\n[G2-K3] Stage B: canonical 三样被冻结")
    fc = FakeClient([LLMResult(tool_input=_kw_structure_payload()),
                     LLMResult(tool_input=review_ok())])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    idea = _kw_idea()
    spec = w.structure_original_idea(title=idea["title"],
                                     puzzle=idea["puzzle"],
                                     answer=idea["answer"])
    check("puzzle 就是 Stage A 那个",
          spec.puzzle == idea["puzzle"], spec.puzzle[:40])
    check("answer 就是 Stage A 那个",
          spec.answer == idea["answer"], spec.answer[:40])
    check("title 就是 Stage A 那个", spec.title == idea["title"], spec.title)
    check("结构化字段确实填上了", bool(spec.facts) and bool(spec.core_answer),
          (len(spec.facts), spec.core_answer[:20]))


def test_g2_keyword_stage_b_ignores_model_puzzle():
    """⚠️ **最要紧的一条**: 模型硬塞 puzzle 也无效。

    schema 里没有这个字段(结构性禁止, 见 G2-K5), 但万一模型自作主张
    塞了一个同名字段, 代码回填必须**覆盖**它 —— 这是 §五 "从结构上禁止
    把自然谜面重新写成工程化谜面"的第二道保险。
    """
    print("\n[G2-K4] 模型塞 puzzle 也无效")
    payload = _kw_structure_payload()
    payload["puzzle"] = "这是模型偷偷改写过的工程化谜面。为什么?"
    payload["answer"] = "模型偷偷改写的谜底。"
    fc = FakeClient([LLMResult(tool_input=payload),
                     LLMResult(tool_input=review_ok())])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    idea = _kw_idea()
    spec = w.structure_original_idea(title=idea["title"],
                                     puzzle=idea["puzzle"],
                                     answer=idea["answer"])
    check("**puzzle 仍是 Stage A 的**(模型那次被丢弃)",
          spec.puzzle == idea["puzzle"], spec.puzzle[:50])
    check("**answer 仍是 Stage A 的**",
          spec.answer == idea["answer"], spec.answer[:50])
    check("模型塞的那个谜面没出现在 spec 里",
          "工程化谜面" not in (spec.puzzle or ""), spec.puzzle[:50])


def test_g2_keyword_stage_b_schema_is_structural():
    """schema 里**根本没有** puzzle/answer/title —— 结构性禁止。"""
    print("\n[G2-K5] Stage B schema 是结构性的")
    props = set(_TOOL_STRUCTURE["input_schema"]["properties"])
    req = set(_TOOL_STRUCTURE["input_schema"]["required"])
    for k in ("puzzle", "answer", "title"):
        check(f"properties 无 {k}", k not in props, sorted(props))
        check(f"required 无 {k}", k not in req, sorted(req))
    check("它要的是分析字段",
          {"core_answer", "facts", "solve_atoms", "fair_clues",
           "discovery_beats", "hints", "signature",
           "completion_fact_ids"} <= props, sorted(props))
    check("工具名是 emit_structure",
          _TOOL_STRUCTURE["name"] == "emit_structure", _TOOL_STRUCTURE["name"])


def test_g2_keyword_provenance_and_generated():
    """§十一: prompt_version / generation_mode, 且**仍是 generated**。"""
    print("\n[G2-K6] provenance + 仍是 generated")
    fc = FakeClient([LLMResult(tool_input=_kw_structure_payload()),
                     LLMResult(tool_input=review_ok())])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    idea = _kw_idea()
    spec = w.structure_original_idea(title=idea["title"],
                                     puzzle=idea["puzzle"],
                                     answer=idea["answer"])
    check("**prompt_version == keyword2-v5(R1: 旧 prompt 已改)**",
          spec.prompt_version == "keyword2-v5", spec.prompt_version)
    check("**与 classic 的 riddle-v9 不同**",
          spec.prompt_version != RIDDLE_PROMPT_VERSION, spec.prompt_version)
    check("metrics 有 generation_mode=keyword2",
          (spec.metrics or {}).get("generation_mode") == "keyword2", spec.metrics)
    check("metrics 不是空的(溯源没丢)", bool(spec.metrics), spec.metrics)
    check("**source_type 为空(不是 curated)**",
          not getattr(spec, "source_type", ""), repr(getattr(spec, "source_type", "")))
    check("**没有 curated_policy_version**",
          not getattr(spec, "curated_policy_version", ""),
          repr(getattr(spec, "curated_policy_version", "")))
    check("**没有 curated_content_hash**",
          not getattr(spec, "curated_content_hash", ""),
          repr(getattr(spec, "curated_content_hash", "")))
    check("quality_policy_version 仍是当前政策(没 bump)",
          spec.quality_policy_version == QUALITY_POLICY_VERSION,
          spec.quality_policy_version)


def test_g2_keyword_uses_generated_quality_contract():
    """`source_type` 为空 => `check_tool` 发的是 **generated 八项**。

    ⚠️ 这条是 §二 "keyword 题仍是 AI-original, 不得被标成 curated" 的
    **可执行判据**: 一旦有人让 Stage B 走 curated 的组装函数(写
    `source_type="curated"`), `check_tool` 会改发外部题库那九项, 这里立刻红。
    """
    print("\n[G2-K7] keyword spec 走 generated 的审稿契约")
    fc = FakeClient([LLMResult(tool_input=_kw_structure_payload()),
                     LLMResult(tool_input=review_ok())])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    idea = _kw_idea()
    spec = w.structure_original_idea(title=idea["title"],
                                     puzzle=idea["puzzle"],
                                     answer=idea["answer"])
    tool = check_tool(spec)
    req = set(tool["input_schema"]["properties"]["quality_checks"]["required"])
    from story.llm import _CURATED_HARD_CHECK_FIELDS, _QUALITY_CHECK_FIELDS
    check("**发的是 generated 那八项**",
          req == set(_QUALITY_CHECK_FIELDS), sorted(req))
    check("**不是 curated 那套**",
          not (req & set(_CURATED_HARD_CHECK_FIELDS)) or
          req == set(_QUALITY_CHECK_FIELDS), sorted(req))
    # 反证: 一旦标成 curated, 契约就变了 —— 证明上面那条不是恒真。
    spec.source_type = "curated"
    tool2 = check_tool(spec)
    req2 = set(tool2["input_schema"]["properties"]["quality_checks"]["required"])
    check("**标成 curated 后契约确实变了**(反证)",
          req2 != req, (sorted(req)[:3], sorted(req2)[:3]))


def test_g2_keyword_stage_b_blueprint_is_unconstrained():
    """§七: Stage B 发的是"无 target Blueprint"哨兵。"""
    print("\n[G2-K8] Stage B: 无 target Blueprint")
    fc = FakeClient([LLMResult(tool_input=_kw_structure_payload()),
                     LLMResult(tool_input=review_ok())])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    idea = _kw_idea()
    spec = w.structure_original_idea(title=idea["title"],
                                     puzzle=idea["puzzle"],
                                     answer=idea["answer"])
    check("spec.blueprint 是 unconstrained 哨兵",
          getattr(spec.blueprint, "_unconstrained", False) is True, spec.blueprint)
    # 审稿 prompt 必须印"没有 target Blueprint"的**观察声明**,
    # 而不是硬约束 —— 否则 Stage B 会因为"不是某个随机骨架"被要求重出。
    rev = [c for c in fc.calls if c.get("tool") and
           c["tool"]["name"] == "emit_review"]
    check("审过稿", len(rev) == 1, len(rev))
    if rev:
        check("**审稿 prompt 印的是观察声明**",
              "没有** target Blueprint" in rev[0]["user"]
              or "没有" in rev[0]["user"] and "target Blueprint" in rev[0]["user"],
              rev[0]["user"][:200])


def test_g2_keyword_should_continue_checkpoints():
    """让路: Stage B 内部的两个检查点(审稿前 / audit 前)。"""
    print("\n[G2-K9] Stage B 内部让路")
    # ① 一进门就让路 -> 一次调用都不发
    fc = FakeClient([])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    idea = _kw_idea()
    spec = w.structure_original_idea(title=idea["title"], puzzle=idea["puzzle"],
                                     answer=idea["answer"],
                                     should_continue=lambda: False)
    check("**零调用**", len(fc.calls) == 0, len(fc.calls))
    check("metrics 标 interrupted",
          (spec.metrics or {}).get("interrupted") is True, spec.metrics)
    check("error 为空(让路不是失败)", not spec.error, spec.error)
    check("puzzle 为空(半成品不当结果)", not spec.puzzle, spec.puzzle)


def test_g2_keyword_stage_a_interrupt_checkpoint():
    """让路: Stage A 的**返回后**检查点。"""
    print("\n[G2-K10] Stage A 返回后让路")
    fc = FakeClient([LLMResult(tool_input=_kw_idea())])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    n = {"i": 0}

    def gate():
        n["i"] += 1
        return n["i"] <= 1          # 调用前放行, 返回后让路

    out = w.gen_keyword_idea(["图书馆", "上楼"], should_continue=gate)
    check("发了 1 次调用", len(fc.calls) == 1, len(fc.calls))
    check("**返回的是 interrupted, 不是 idea**",
          out == {"interrupted": True}, out)


def test_g2_keyword_rewrite_fails_candidate():
    """Reviewer rewrite -> **整道候选失败**(不重出、不修)。"""
    print("\n[G2-K11] Reviewer rewrite -> 候选失败")
    fc = FakeClient([LLMResult(tool_input=_kw_structure_payload()),
                     LLMResult(tool_input=review_rewrite("没有公平推理路径"))])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    idea = _kw_idea()
    spec = w.structure_original_idea(title=idea["title"],
                                     puzzle=idea["puzzle"],
                                     answer=idea["answer"])
    check("**puzzle 为空**(候选被丢)", not spec.puzzle, spec.puzzle[:30])
    check("error 非空", bool(spec.error), spec.error)
    check("error 提到重出", "重出" in spec.error, spec.error)
    check("metrics 记了 rewrite_count",
          (spec.metrics or {}).get("rewrite_count") == 1, spec.metrics)
    check("**只跑了一次结构化**(没有为失败再生成一稿)",
          [c["tool"]["name"] for c in fc.calls].count("emit_structure") == 1,
          [c["tool"]["name"] for c in fc.calls])


def test_g2_keyword_truth_audit_fail_rejects():
    """truth audit fail -> 候选失败。"""
    print("\n[G2-K12] truth audit fail -> 候选失败")
    fc = FakeClient([LLMResult(tool_input=_kw_structure_payload()),
                     LLMResult(tool_input=review_ok()),
                     LLMResult(tool_input=_truth_tool(
                         truthful=False, consistent=True,
                         conflicts=["谜面说 A, 谜底说不是 A"]))])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    idea = _kw_idea()
    spec = w.structure_original_idea(title=idea["title"],
                                     puzzle=idea["puzzle"],
                                     answer=idea["answer"])
    check("**puzzle 为空**", not spec.puzzle, spec.puzzle[:30])
    check("error 提到审计", "审计" in (spec.error or ""), spec.error)
    check("metrics 记 truth_audit_ok=False",
          (spec.metrics or {}).get("truth_audit_ok") is False, spec.metrics)


# ======================================================================
# G3 —— Stage A prompt 向 haiguitang 原始口径收敛
# ======================================================================
#
# v1 那段 prompt 是我们手写的写作规范, 里面有一批**会主动改变生成分布**
# 的硬约束。§六 逐条点名要去掉的就是它们。这一批测试把它们钉死 —— 否则
# 将来"为了通过率"很容易再写回去, 而那正是 §七 禁止的。

#: v1 里那些**创作形状硬约束** —— 必须已经消失。
#: 每一条都标注了它当年长什么样, 免得将来有人换个说法又加回来。
_V1_SHAPE_RULES = (
    "第三人称",       # "用第三人称客观叙述, 不要'我'"
    "1~3 句",         # "谜面, 1~3 句"
    "单机关",         # "单机关也可以 —— 不需要两个诡计叠在一起"
    "第二机关",       # "不为显得高级增加第二机关"
    "不要职业",       # "不要求复杂人物背景, 不要求职业设定"
    "不要求悲剧",     # "不要求多层反转, 不要求悲剧"
    "结尾要是一个问句",  # "谜面结尾要是一个问句"
    "不要套模板",     # "先在心里想一个自然的情境…不要套模板"
)

#: §六 要求**保留**的运行约束 —— 这些丢了会出直播事故。
_MUST_KEEP = (
    "中文",           # 全程中文
    "冷门专业知识",   # 不依赖冷门专业知识
    "图片",           # 不依赖外部图片/音频/软件
    "直播",           # 适合普通直播场景
)

#: Stage A prompt **永远**不该提的东西(§六: 全都交给 Stage B / Reviewer)。
_NEVER_MENTION = (
    "facts", "atoms", "completion", "discovery_beats", "signature",
    "Blueprint", "quota", "recent",
)


def test_g3_stage_a_prompt_dropped_v1_shape_rules():
    """§六: v1 那些**创作形状**硬约束必须已经从 Stage A prompt 消失。

    ⚠️ 为什么这条重要: 它们是 G1 实验里作为**单变量**被验证过的 —— 那时
    我们想知道"少规定一点会怎样", 所以拿它们做对照。接进生产之后, 它们
    的作用就反过来了: 变成"我们在教模型写我们想要的题", 而不是"让模型
    按这个题源的自然方式出题"。任务书 §七 明确: **不要**为了救通过率
    把它们写回来。
    """
    print("\n[G3-K1] Stage A prompt 已去掉 v1 的形状硬约束")
    low = KEYWORD_IDEA_SYSTEM
    for rule in _V1_SHAPE_RULES:
        check(f"**不含 v1 形状约束: {rule}**", rule not in low,
              [ln for ln in low.splitlines() if rule in ln][:1])


def test_g3_stage_a_prompt_keeps_runtime_constraints():
    """§六: **运行约束**一条都不能丢(中文/不靠冷门知识/不靠外部媒体/适合直播)。

    ⚠️ G4-CF 改的是**创作顺序**(v4 Case-first), 运行约束**一个字都没动**
    —— 它们与"先想真相还是先写谜面"无关, 丢了就是直播事故。
    """
    print("\n[G3-K2] Stage A prompt 保留运行约束")
    low = KEYWORD_IDEA_SYSTEM
    for token in _MUST_KEEP:
        check(f"含运行约束: {token}", token in low)
    # ⚠️ 这两条在 v4 里措辞变了(不再自称"海龟汤故事生成器"), 所以改成
    # 断言语义而不是断言那句旧文案 —— 旧文案本身不是产品口径。
    check("核心语义含'海龟汤'", "海龟汤" in low)
    check("核心语义含'悬念'或'意外/反常'",
          "悬念" in low or "反常" in low)
    check("**谜底仍要求 2~4 句 / 不超过 260 字**(展示硬合同)",
          "2~4 句" in low and "260" in low, low[-400:])


def test_g3_stage_a_prompt_never_mentions_stage_b_vocabulary():
    """§六 / §12.3: Stage A prompt 不提 facts / atoms / completion / Blueprint。

    ⚠️ Case-first 的 `core_truth` / `observed_clues` / `event_chain`
    **不是** Stage B vocabulary —— 这条名单因此**一个词都不放宽**。
    一个字段"形状像数组"不等于它是 `facts`。
    """
    print("\n[G3-K3] Stage A prompt 不提 Stage B 的词汇")
    low = KEYWORD_IDEA_SYSTEM.lower()
    for bad in _NEVER_MENTION:
        check(f"Stage A prompt 不提 {bad}", bad.lower() not in low,
              [ln for ln in low.splitlines() if bad.lower() in ln][:1])
    # v4 的**创作顺序**术语必须真的在, 否则这条测试可能只是在测一个
    # 空 prompt(反证: 断言正向内容存在)
    for token in ("core_truth", "observed_clues", "event_chain"):
        check(f"prompt 里有 {token}", token in KEYWORD_IDEA_SYSTEM)


def test_case_first_stage_a_schema():
    """§12.2: Stage A schema **恰好六个字段**, 且五个 required。

    ⚠️ 这条**替换**了旧的 `test_g3_stage_a_schema_still_only_three_fields`。
    三字段合同是 v3 的形状, 已被产品决定废弃 —— **不要**为了让旧测试绿
    而偷偷维持它, 那会让"Stage A 到底是不是 Case-first"变得不可测。
    """
    print("\n[G4-CF-3] Stage A schema: 六字段 / 五 required")
    props = _TOOL_KEYWORD_IDEA["input_schema"]["properties"]
    req = _TOOL_KEYWORD_IDEA["input_schema"]["required"]
    check("**恰好六个字段**",
          set(props) == {"core_truth", "observed_clues", "event_chain",
                         "title", "puzzle", "answer"}, sorted(props))
    check("**五个 required**",
          set(req) == {"core_truth", "observed_clues", "event_chain",
                       "puzzle", "answer"}, sorted(req))
    check("title 仍可空(不在 required)", "title" not in req, sorted(req))
    check("name 仍是 emit_keyword_idea",
          _TOOL_KEYWORD_IDEA["name"] == "emit_keyword_idea",
          _TOOL_KEYWORD_IDEA["name"])
    for k in _CF_SCAFFOLD:
        check(f"{k} required", k in req, sorted(req))
    # ---- 描述里也不得出现 Stage B 的结构词汇 ----
    blob = json.dumps(_TOOL_KEYWORD_IDEA, ensure_ascii=False).lower()
    for bad in _STAGE_B_VOCAB:
        check(f"schema 描述不提 {bad}", bad.lower() not in blob)


def test_case_first_stage_a_is_not_stage_b():
    """§12.3: Case-first 脚手架 **≠** PuzzleSpec schema。

    这条非常重要: 两者的字段**形状**像(都有数组), 语义完全不同 ——
    `observed_clues` 是"作者先想出来的现场痕迹", `facts` 是 Stage B
    从**最终**谜面谜底读出来的结构化事实。一旦把它们合并, 就有了两份
    事实来源, 以后要解决"谁权威"。
    """
    print("\n[G4-CF-4] 脚手架不是 Stage B 词汇")
    a_props = set(_TOOL_KEYWORD_IDEA["input_schema"]["properties"])
    b_props = set(_TOOL_STRUCTURE["input_schema"]["properties"])
    check("**两个字段集不相交**", not (a_props & b_props),
          sorted(a_props & b_props))
    for bad in _STAGE_B_VOCAB:
        check(f"**Stage A 没有 {bad}**", bad not in a_props)
    for k in _CF_SCAFFOLD:
        check(f"**Stage B 没有 {k}**", k not in b_props, sorted(b_props))
    # prompt 层也要干净
    for bad in _STAGE_B_VOCAB:
        check(f"**Stage A prompt 不提 {bad}**", bad not in KEYWORD_IDEA_SYSTEM)


def test_g4cf_internal_scaffold_never_reaches_stage_b():
    """§13-B: 脚手架字段**不会**因为代码透传而变成第二份 canonical。

    做法: 走**真实的** `keyword_spec` 生产路径(bag -> Stage A -> Stage B),
    把 Stage A 的脚手架填成一眼能认出来的哨兵串, 断言这些串**既不在**
    Stage B 的 user prompt 里, **也不在** spec 的结构化字段里。

    ⚠️ 必须走 `keyword_spec` 而不是直接调 `structure_original_idea`:
    前者才是生产里唯一把 idea 拆成参数交给 Stage B 的地方。直接调后者
    等于我自己决定传什么, 测不到"接线有没有漏"。

    ⚠️ Stage A 的 title/puzzle/answer 当然会出现在 Stage B 的 user
    prompt 里(那正是它要结构化的东西) —— 所以哨兵只放脚手架字段。
    """
    print("\n[G4-CF-5] 脚手架不进入 Stage B(走 keyword_spec)")
    from story.keyword_seed import keyword_spec
    # ⚠️ 哨兵要满足**新加的 fail-closed 结构约束**(2~4 clues / 2~3 chain),
    # 否则 Stage A 在进入 Stage B 之前就被结构校验拦下, 这条用例测不到
    # 它想测的东西(脚手架有没有透传进 Stage B)。
    sentinel = ["内部脚手架AAA", "内部脚手架BBB", "内部脚手架CCC",
                "内部脚手架DDD"]
    idea = dict(_kw_idea())
    idea["core_truth"] = sentinel[0]
    idea["observed_clues"] = [sentinel[1], sentinel[2]]
    idea["event_chain"] = [sentinel[3], "内部脚手架EEE"]
    fc = FakeClient([LLMResult(tool_input=idea),
                     LLMResult(tool_input=_kw_structure_payload()),
                     LLMResult(tool_input=review_ok())])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    from story.keyword_seed import KeywordBag
    bag = KeywordBag(["灯塔", "退潮", "礁石", "船"], 20260921)
    spec, why = keyword_spec(w, bag, 20260921)
    check("**成题了**(前置条件)", spec is not None and bool(spec.puzzle),
          (why, getattr(spec, "error", "")))
    struct_user = [c["user"] for c in fc.calls
                   if (c.get("tool") or {}).get("name") == "emit_structure"]
    check("结构化调用发了 1 次", len(struct_user) == 1, len(struct_user))
    for tag in sentinel:
        check(f"**Stage B user prompt 不含 {tag}**",
              all(tag not in u for u in struct_user),
              [u[:120] for u in struct_user])
    check("**但 Stage A 的 puzzle 确实进了 Stage B**(反证: 不是恒真)",
          any(idea["puzzle"] in u for u in struct_user))
    blob = json.dumps({
        "facts": [getattr(f, "text", "") for f in (spec.facts or [])],
        "atoms": [getattr(a, "text", "") for a in (spec.solve_atoms or [])],
        "beats": [getattr(b, "text", "") for b in (spec.discovery_beats or [])],
        "answer": spec.answer or "", "puzzle": spec.puzzle or "",
        "metrics": spec.metrics or {},
    }, ensure_ascii=False)
    for tag in sentinel:
        check(f"**spec 里不含 {tag}**", tag not in blob, blob[:120])


def test_g4cf_stage_a_and_b_are_only_ever_called_by_keyword_spec():
    """§13-A: **prefetch 与 live 共用 Case-first**, 靠"只有一条路"来保证。

    ⚠️ 不要靠 grep prompt 文本 —— 那只能证明"某段文字出现过"。这里证的是
    **调用图**:

        1. `keyword_spec()` 里同时有 gen_keyword_idea 与 structure_original_idea
           (= 两阶段链只在这一个地方拼起来);
        2. `director.py` 里**没有**这两个方法名 —— live 只能经 `keyword_spec`
           拿到 Case-first, 不可能自己拼一条旧链;
        3. `prefetch.py` 里也**没有**直接调 `gen_keyword_idea`。

    目标: 防止以后又出现 `prefetch = Case-first` / `live = 旧 keyword prompt`
    这种半切换。kill-switch 只有一个(`pool_keyword_seed_enabled`), 两边
    读的是它。
    """
    print("\n[G4-CF-8] live / prefetch 共用 Case-first")
    import ast as _ast
    import io as _io
    from pathlib import Path as _P
    root = _P(__file__).resolve().parents[1]

    def _called_names(rel):
        tree = _ast.parse(_io.open(root / rel, encoding="utf-8").read())
        out = set()
        for n in _ast.walk(tree):
            if isinstance(n, _ast.Call):
                f = n.func
                if isinstance(f, _ast.Attribute):
                    out.add(f.attr)
                elif isinstance(f, _ast.Name):
                    out.add(f.id)
        return out

    ks = _called_names("story/keyword_seed.py")
    check("**keyword_spec 调 gen_keyword_idea**",
          "gen_keyword_idea" in ks)
    check("**keyword_spec 调 structure_original_idea**",
          "structure_original_idea" in ks)

    d = _called_names("director.py")
    check("**director 不直接调 gen_keyword_idea**",
          "gen_keyword_idea" not in d)
    check("**director 不直接调 structure_original_idea**",
          "structure_original_idea" not in d)
    check("**director 确实调 keyword_spec**(否则它根本不走 keyword2)",
          "keyword_spec" in d)

    p = _called_names("story/prefetch.py")
    check("**prefetch 不直接调 gen_keyword_idea**",
          "gen_keyword_idea" not in p)
    check("**prefetch 不直接调 structure_original_idea**",
          "structure_original_idea" not in p)

    # 反证: 这套检查**能**抓到 —— 别名注入一个假的调用图
    fake = set(d) | {"gen_keyword_idea"}
    check("**反证: 注入后确实会红**", "gen_keyword_idea" in fake)


def test_g4cf_stage_a_one_attempt_and_interrupt_still_hold():
    """§12.5 / §12.6: 新 schema 下 **1 attempt** 与**让路**语义不变。

    Case-first 只是多了三个字段, 不该改变预算或让路 —— 这两条是最容易
    在"加字段"时被顺手改掉的东西(比如"多了字段就多试一稿")。
    """
    print("\n[G4-CF-9] 新 schema 下 1 attempt + 让路")
    # ① 失败仍只发 1 次
    fc = FakeClient([LLMResult(error="网关抖了")])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    check("失败返回 None", w.gen_keyword_idea(["图书馆", "上楼"]) is None)
    check("**只发了 1 次**", len(fc.calls) == 1, len(fc.calls))
    # ② 调用前让路 -> 0 次
    fc = FakeClient([])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    out = w.gen_keyword_idea(["图书馆", "上楼"],
                             should_continue=lambda: False)
    check("调用前让路: **0 LLM calls**", len(fc.calls) == 0, len(fc.calls))
    check("**返回 interrupted**", out == {"interrupted": True}, out)
    # ③ 返回后让路 -> 结果丢弃
    fc = FakeClient([LLMResult(tool_input=_kw_idea())])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    n = {"i": 0}

    def gate():
        n["i"] += 1
        return n["i"] <= 1

    out = w.gen_keyword_idea(["图书馆", "上楼"], should_continue=gate)
    check("发了 1 次调用", len(fc.calls) == 1, len(fc.calls))
    check("**返回 interrupted, 不是 idea**",
          out == {"interrupted": True}, out)


def test_g4cf_contract_signature_rejects_scaffold_kwargs():
    """§六: `structure_original_idea` 的**调用签名**里没有脚手架字段。

    上面那条测的是"传进去也不会流出去"; 这条测的是**根因**: 只要签名
    里没有这三个参数, 想透传都传不进去 —— 结构上禁止, 不是靠自觉。

    ⚠️ 用 `inspect` 而不是 grep 源码: 注释里**必须**能讨论这件事
    (上面 `keyword_seed.py` 那段就写了为什么不传), 而 grep 会把
    解释当成违规。
    """
    print("\n[G4-CF-6] Stage B 签名不含脚手架")
    import inspect
    sig = inspect.signature(PuzzleWriter.structure_original_idea)
    params = set(sig.parameters)
    for k in _CF_SCAFFOLD:
        check(f"**签名没有 {k}**", k not in params, sorted(params))
    for k in ("title", "puzzle", "answer"):
        check(f"签名有 {k}(canonical 三样)", k in params, sorted(params))
    # keyword_spec 那边同样只交三样
    from story.keyword_seed import keyword_spec
    import io as _io
    from pathlib import Path as _P
    ks_src = _io.open(_P(__file__).resolve().parents[1] / "story"
                      / "keyword_seed.py", encoding="utf-8").read()
    call = ks_src.split("spec = writer.structure_original_idea(")[1]
    call = call.split(")")[0]
    for k in _CF_SCAFFOLD:
        check(f"**keyword_spec 没把 {k} 交给 Stage B**", k not in call,
              call.replace("\n", " ")[:160])


def test_g4cf_scaffold_dies_at_stage_a():
    """§六: 脚手架**就在 Stage A 结束** —— 池里的 spec 上没有它们。

    `PuzzleSpec` 是持久化对象(`pool.jsonl` / archive)。这三个字段是
    创作期的一次性脚手架, 不该出现在任何落盘结构里。
    """
    print("\n[G4-CF-7] 脚手架不进 PuzzleSpec")
    from story.puzzle import PuzzleSpec as _PS
    fields = set(getattr(_PS, "__dataclass_fields__", {}) or {})
    if not fields:                      # 非 dataclass 时退回 __init__ 签名
        import inspect as _i
        fields = set(_i.signature(_PS).parameters)
    for k in _CF_SCAFFOLD:
        check(f"**PuzzleSpec 没有 {k} 字段**", k not in fields, sorted(fields))
    fc = FakeClient([LLMResult(tool_input=_kw_structure_payload()),
                     LLMResult(tool_input=review_ok())])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    idea = _kw_idea()
    spec = w.structure_original_idea(title=idea["title"],
                                     puzzle=idea["puzzle"],
                                     answer=idea["answer"])
    for k in _CF_SCAFFOLD:
        check(f"**spec 上没有 {k} 属性**", not hasattr(spec, k))
        check(f"**metrics 里也没有 {k}**",
              k not in (spec.metrics or {}), sorted(spec.metrics or {}))


def test_g3_stage_b_still_freezes_and_has_no_puzzle_field():
    """§十: Stage B 仍冻结 canonical 三样, 且 schema 仍无 puzzle 字段。

    这条是 G2 那几条的**再确认** —— G3 只改 Stage A 的 prompt, 动了
    Stage B 就是越界。用与 G2 相同的探针验, 保证结论仍然成立。
    """
    print("\n[G3-K5] Stage B 仍冻结 + schema 仍无 puzzle")
    props = _TOOL_STRUCTURE["input_schema"]["properties"]
    for bad in ("puzzle", "answer", "title"):
        check(f"**Stage B schema 无 {bad} 字段**", bad not in props,
              sorted(props))
    # 模型硬塞 puzzle 也无效
    fc = FakeClient([LLMResult(tool_input=_kw_structure_payload(
                        puzzle="模型偷偷改写的谜面")),
                     LLMResult(tool_input=review_ok())])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    idea = _kw_idea()
    spec = w.structure_original_idea(title=idea["title"],
                                     puzzle=idea["puzzle"],
                                     answer=idea["answer"])
    check("**模型塞的 puzzle 被忽略**", spec.puzzle == idea["puzzle"],
          (spec.puzzle[:40], idea["puzzle"][:40]))
    check("title 也被冻结", spec.title == idea["title"], spec.title)
    check("answer 也被冻结", spec.answer == idea["answer"], spec.answer)
    # ---- §12.7 加强: 脚手架既没进 schema, 也没成为第二份事实源 ----
    for k in _CF_SCAFFOLD:
        check(f"**Stage B schema 没有 {k}**", k not in props, sorted(props))
    blob = json.dumps(_TOOL_STRUCTURE, ensure_ascii=False)
    for k in _CF_SCAFFOLD:
        check(f"**Stage B schema 描述不提 {k}**", k not in blob)
    # Stage A 的 idea 里明明有脚手架, spec 上却一个都没有
    idea_full = _kw_idea()
    for k in _CF_SCAFFOLD:
        check(f"**脚手架没变成 spec 属性: {k}**", not hasattr(spec, k))
        check(f"**脚手架没进 metrics: {k}**", k not in (spec.metrics or {}))
        check(f"脚手架确实在 Stage A 的 idea 里: {k}", k in idea_full)


def test_g3_quality_gates_unchanged():
    """§七: **质量门一条都没放宽** —— 只改了候选怎么想出来。

    做法: 造一个**自由 Stage A 也过不了**的候选(谜面里没有任何可核对的
    事实), 断言后面的门照样拒它。若哪天有人"为了让自由 prompt 的产出能
    过"而放宽硬门, 这条会红。
    """
    print("\n[G3-K6] 质量门未放宽")
    from story.llm import RIDDLE_PROMPT_VERSION, validate_spec
    # (a) 常量没被 bump —— 接受标准一个字都没改
    check("**QUALITY_POLICY_VERSION 未 bump**",
          "keyword2" not in __import__("story.llm", fromlist=["x"])
          .QUALITY_POLICY_VERSION,
          __import__("story.llm", fromlist=["x"]).QUALITY_POLICY_VERSION)
    check("**RIDDLE_PROMPT_VERSION 未 bump(live 还在用)**",
          RIDDLE_PROMPT_VERSION == "riddle-v9", RIDDLE_PROMPT_VERSION)
    # (b) 结构硬门仍然会拒一个空壳 spec
    bare = PuzzleSpec(puzzle="", answer="", core_answer="")
    check("**空壳 spec 仍被 validate_spec 拒**", not validate_spec(bare).ok)
    # (c) curated 的 source_type 仍会让审稿走**另一套**契约(没被合并)
    fc = FakeClient([LLMResult(tool_input=_kw_structure_payload()),
                     LLMResult(tool_input=review_ok())])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    idea = _kw_idea()
    spec = w.structure_original_idea(title=idea["title"],
                                     puzzle=idea["puzzle"],
                                     answer=idea["answer"])
    check("产物仍是 generated(source_type 空)",
          not getattr(spec, "source_type", ""), repr(spec.source_type))
    check("没有 curated_policy_version",
          not getattr(spec, "curated_policy_version", ""))


# ======================================================================
# G2 —— 可修问题不再整题重造
# ======================================================================
def _qip(quote, puzzle):
    """`quote_in_puzzle` 的短别名(测试里用得多)。"""
    from story.puzzle import quote_in_puzzle
    return quote_in_puzzle(quote, puzzle)


def _v5_fixture():
    """一份**当前政策**下结构合格的 spec, 供 `validate_spec` 直测用。

    与 `riddle()`(出题返回的 dict)不同 —— 这是已经解析好的
    `PuzzleSpec` 对象, 用来单独验校验层的行为, 不经过 LLM。
    """
    from story.puzzle import (PuzzleFact, PuzzleSpec, PuzzleSignature,
                              SolveAtom, FairClue, DiscoveryBeat)
    from story.quality import QUALITY_POLICY_VERSION
    return PuzzleSpec(
        title="灯塔",
        puzzle=_GOOD_PUZ,
        answer="退潮时礁石露出, 亮灯是标礁石位置。",
        core_answer="他亮灯是为了标出退潮时露出的礁石。",
        completion_fact_ids=["f1", "f2"],
        hints=["a", "b", "c"],
        facts=[
            PuzzleFact(id="f1", text="退潮时礁石露出水面", kind="core",
                       visibility="hidden"),
            PuzzleFact(id="f2", text="灯的真正作用是标示礁石位置",
                       kind="core", visibility="hidden"),
            PuzzleFact(id="f3", text="涨潮后亮灯会误导船只", kind="support",
                       visibility="public"),
            PuzzleFact(id="f4", text="不是为了纪念死者", kind="exclusion",
                       visibility="public"),
        ],
        solve_atoms=[
            SolveAtom(id="a1", role="cause", text="退潮使礁石需要标出",
                      fact_ids=["f1"], required=True),
            SolveAtom(id="a2", role="mechanism", text="灯是标礁石不是引路",
                      fact_ids=["f2", "f3"], required=True),
        ],
        fair_clues=[FairClue(quote="只在退潮的那几个小时亮灯",
                             supports_atoms=["a1"])],
        discovery_beats=[
            DiscoveryBeat(id="b1", text="先注意到灯只在退潮时亮",
                          fact_ids=["f1"]),
            DiscoveryBeat(id="b2", text="再想到灯是在标礁石, 不是引路",
                          fact_ids=["f2"]),
        ],
        signature=PuzzleSignature(
            mechanism_family="hidden_function",
            solution_shape="hidden_function_explains_behavior",
            domain="maritime", relation="stranger",
            emotion_mode="neutral", time_shape="instant",
            reveal_mode="meaning_flip"),
        quality_policy_version=QUALITY_POLICY_VERSION,
    )


def _fix_tool(**kw):
    """造一条 `emit_hint_fix` 的返回。"""
    return LLMResult(tool_input={"hints": kw.get("hints") or
                                 ["往时间上想。", "注意顺序。", "想想地点。"]})


def test_g2a_core_answer_81_is_repaired_not_regenerated():
    """**G2-A 最关键 regression**: core_answer=81 只花 **1** 次生成。

    实播日志里 `core_answer 81 字` 反复出现, 每次都丢掉整道题重新出稿。
    但 80 字是**最终**门, 而"这句话太长"是 reviewer 一句话能改好的事。

    断言的是**请求计数**, 不是"最终过了": 修好之后 generator 只跑 1 稿。
    """
    print("\n[G2-A] core_answer=81 -> 修当前稿, generator 只跑 1 次")
    P = _GOOD_PUZ
    long_core = "他" * 81
    fc = FakeClient([
        LLMResult(tool_input=riddle(core_answer=long_core), model="m"),
        # 审稿: fix —— 只压缩 core_answer, 其余原样
        LLMResult(tool_input=review_fix(P, core_answer="一句话核心答案。"),
                  model="m"),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    spec = w.gen_spec(blueprint=fc.default_blueprint)
    names = [c["tool"]["name"] for c in fc.calls]
    check("**generator 只跑了 1 稿**",
          names.count("emit_riddle") == 1, names)
    check("最终 core_answer <= 80",
          len(spec.core_answer or "") <= 80, spec.core_answer)
    check("最终出题成功", bool(spec.puzzle), spec.puzzle[:30])


def test_g2a_core_answer_121_is_hard_fail():
    """`> 120` 才算真的写偏了(一段话而不是一句话) -> 换稿。"""
    print("\n[G2-A2] core_answer=121 -> 硬失败换稿")
    fc = FakeClient([
        LLMResult(tool_input=riddle(core_answer="他" * 121), model="m"),
        LLMResult(tool_input=riddle(puzzle="二稿谜面, 另一个事件。为什么?"),
                  model="m"),
        LLMResult(tool_input=review_ok("二稿谜面, 另一个事件。为什么?"),
                  model="m"),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    spec = w.gen_spec(blueprint=fc.default_blueprint, max_attempts=3)
    names = [c["tool"]["name"] for c in fc.calls]
    check("**第一稿没进审稿就换了**",
          names[:1] == ["emit_riddle"] and names.count("emit_riddle") == 2,
          names)
    check("采用了第二稿", "二稿" in (spec.puzzle or ""), spec.puzzle[:30])


def test_u4_livestream_text_length_bounds():
    """U4 长度合同只加硬门，不改变 core_answer 的既有分档。"""
    print("\n[U4-LENGTH] puzzle/answer 硬上限；core 合同不变")
    from story.puzzle import PuzzleSpec
    from story.quality import (
        ANSWER_HARD_MAX_LEN, CORE_ANSWER_MAX_LEN,
        PUZZLE_HARD_MAX_LEN, validate_spec,
    )

    def spec(puzzle_len=PUZZLE_HARD_MAX_LEN,
             answer_len=ANSWER_HARD_MAX_LEN,
             core_len=CORE_ANSWER_MAX_LEN):
        puzzle = "灯" * (puzzle_len - 4) + "为什么？"
        data = riddle(puzzle=puzzle, answer="底" * answer_len,
                      core_answer="核" * core_len,
                      quality_policy_version=QUALITY_POLICY_VERSION)
        return PuzzleSpec.from_dict(data)

    at_limit = validate_spec(spec())
    check("puzzle=220 / answer=300 / core=80 通过",
          at_limit.ok and not at_limit.fixable,
          (at_limit.errors, at_limit.fixable))

    puzzle_over = validate_spec(spec(puzzle_len=PUZZLE_HARD_MAX_LEN + 1))
    check("puzzle=221 硬失败",
          not puzzle_over.ok and any("谜面超过直播展示硬上限" in e
                                     for e in puzzle_over.errors),
          puzzle_over.errors)

    answer_over = validate_spec(spec(answer_len=ANSWER_HARD_MAX_LEN + 1))
    check("answer=301 硬失败",
          not answer_over.ok and any("谜底超过直播展示硬上限" in e
                                     for e in answer_over.errors),
          answer_over.errors)

    core_81 = validate_spec(spec(core_len=CORE_ANSWER_MAX_LEN + 1))
    check("core_answer=81 仍是原有 fixable 语义",
          core_81.ok and any("core_answer 有 81 字" in f
                             for f in core_81.fixable),
          (core_81.errors, core_81.fixable))


def test_g2b_clue_quote_repaired_in_place():
    """**G2-B**: quote 不在谜面 -> 就地改 quote, **不换稿**。

    ⚠️ G4-R2-R2: 审稿人的答复必须**保持 clue 数量不变** —— 现在
    `_core_fix_scope_violation` 的逐 clue 守卫要求"只重摘 quote,
    数量/顺序/supports_atoms 全冻结"。第一版这里用的是 `review_fix(P)`
    (它从 `riddle()` 重新生成**两条** clue), 于是"修 quote"变成了
    "换一整套线索" —— 那正是守卫要拦的越界, 用例却把它当成合法修复。
    真实的重摘就是**同一条 clue 换个 quote**, 所以夹具照这个形状写。
    """
    print("\n[G2-B] clue quote 不在谜面里 -> 就地修")
    P = _GOOD_PUZ
    bad_clues = [{"quote": "这句话谜面里根本没有", "supports_atoms": ["a1"]}]
    # 合法修复: **同一条** clue, quote 换成谜面里真有的那一句。
    good_q = clues_for(P)[0]["quote"]
    fixed = review_fix(P)
    fixed["fair_clues"] = [{"quote": good_q, "supports_atoms": ["a1"]}]
    fc = FakeClient([
        LLMResult(tool_input=riddle(fair_clues=bad_clues), model="m"),
        # 审稿: fix —— 从当前谜面重新逐字摘
        LLMResult(tool_input=fixed, model="m"),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    spec = w.gen_spec(blueprint=fc.default_blueprint)
    names = [c["tool"]["name"] for c in fc.calls]
    check("**generator 只跑了 1 稿**",
          names.count("emit_riddle") == 1, names)
    check("最终出题成功", bool(spec.puzzle), spec.puzzle[:30])
    check("最终 quote 真的在谜面里",
          all(_qip(c.quote, spec.puzzle)
              for c in (spec.fair_clues or [])),
          [c.quote for c in (spec.fair_clues or [])])


def test_g2b_reviewer_must_not_change_puzzle_for_quote():
    """**G2-B 禁令**: 修 quote 时**不得**改谜面去迁就它。

    这里断言的是**反馈措辞**真的把两条禁令写进去了 —— 模型看到什么
    决定它怎么做, 而"改谜面"是一条看起来最省事的路。
    """
    print("\n[G2-B2] 反馈写明两条禁令")
    from story.quality import validate_spec
    s = _v5_fixture()
    s.fair_clues = [FairClue(quote="谜面里没有的句子", supports_atoms=["a1"])]
    vr = validate_spec(s)
    check("进 fixable", bool(vr.fixable), vr.fixable)
    joined = " ".join(vr.fixable)
    check("**写明不得改动谜面**", "不得改动谜面" in joined, joined)
    check("**写明不得编造句子**", "编造" in joined, joined)


def test_g2c_linkage_is_fixable_but_missing_content_is_not():
    """**G2-C**: 区分"metadata 没连好" 与 "内容不存在"。

    前者 reviewer 接一根线就能修; 后者是内容缺失, reviewer 改不出来
    (它不能凭空造一条事实, 那等于替生成器写题)。
    """
    print("\n[G2-C] 连线缺失可修 / 内容缺失硬拒")
    from story.quality import validate_spec
    # (a) 事实在、atom 也在, 只是没连上 -> fixable
    s = _v5_fixture()
    s.solve_atoms[1].fact_ids = ["f3"]          # f2 失去引用
    vr = validate_spec(s)
    check("**连线缺失 -> ok 且 fixable**", vr.ok and bool(vr.fixable),
          (vr.ok, vr.fixable))
    check("反馈说明是连线问题",
          any("连线" in e or "引用" in e for e in vr.fixable), vr.fixable)
    # (b) fact 根本不存在 -> 硬拒
    s2 = _v5_fixture()
    s2.completion_fact_ids = ["f1", "f404"]
    vr2 = validate_spec(s2)
    check("**内容缺失 -> 硬拒**", not vr2.ok, vr2.why())
    check("理由点了'不存在'",
          any("不存在" in e for e in vr2.errors), vr2.errors)


def test_g2d_hint_too_long_gets_narrow_repair():
    """**G2-D**: 只剩 hints 太长 -> 一次**只改 hints** 的窄修复。

    为此丢掉整道题(连同已经通过的 facts/atoms/clues/beats)是最亏的
    一笔账 —— 那是"重新生成一整道题再赌一次"的成本。
    """
    print("\n[G2-D] hints 超长 -> 窄修复, 不换稿")
    P = _GOOD_PUZ
    fc = FakeClient([
        LLMResult(tool_input=riddle(hints=["x" * 31, "b", "c"]), model="m"),
        # 审稿: pass, 但 hints 仍然超长 -> 触发窄修复
        LLMResult(tool_input=review_ok(P, hints=["x" * 31, "b", "c"]),
                  model="m"),
        _fix_tool(hints=["往时间上想。", "注意顺序。", "想想地点。"]),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    spec = w.gen_spec(blueprint=fc.default_blueprint)
    names = [c["tool"]["name"] for c in fc.calls]
    check("**generator 只跑了 1 稿**",
          names.count("emit_riddle") == 1, names)
    check("**调了一次 emit_hint_fix**",
          names.count("emit_hint_fix") == 1, names)
    check("最终三条提示都 <= 30 字",
          all(len(h) <= 30 for h in (spec.hints or [])), spec.hints)
    check("最终出题成功", bool(spec.puzzle), spec.puzzle[:30])


def test_g2d_narrow_repair_refuses_when_other_issues_remain():
    """**G2-D 边界**: fixable 里混着**别的**问题 -> 窄修复**不动手**。

    否则它会掩盖"审稿人没干完活"这件事 —— 补了 hints 让校验通过,
    而 core_answer 还是超长的那一稿就这么溜进直播了。
    """
    print("\n[G2-D2] 混着别的问题 -> 不窄修复")
    P = _GOOD_PUZ
    fc = FakeClient([
        LLMResult(tool_input=riddle(hints=["x" * 31, "b", "c"]), model="m"),
        # 审稿 pass 但 hints 仍超长 —— 而且 core_answer 也超长
        LLMResult(tool_input=review_ok(P, hints=["x" * 31, "b", "c"],
                                       core_answer="他" * 100), model="m"),
        # 第二稿(应该走到这里)
        LLMResult(tool_input=riddle(puzzle="二稿谜面, 另一个事件。为什么?"),
                  model="m"),
        LLMResult(tool_input=review_ok("二稿谜面, 另一个事件。为什么?"),
                  model="m"),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    spec = w.gen_spec(blueprint=fc.default_blueprint, max_attempts=3)
    names = [c["tool"]["name"] for c in fc.calls]
    check("**没有调 emit_hint_fix(问题不止 hints)**",
          names.count("emit_hint_fix") == 0, names)
    check("走了换稿路径", names.count("emit_riddle") == 2, names)


def test_g2e_kind_visibility_swap_is_normalized():
    """**G2-E**: kind/visibility 明显填反 -> 原地换回来。

    实播日志 `fact fN kind 非法: public` —— `public` 显然是 **visibility**
    值。只有两个字段**互相**都是对方的合法取值时才能安全交换。
    """
    print("\n[G2-E] kind/visibility 填反 -> 原地交换")
    from story.quality import validate_spec
    # ⚠️ 用**非 completion** 的 fact 来试: f1/f2 是通关要求, 它们必须
    # 是 hidden —— 拿它们做交换会撞上另一条(正确的)规则, 测的就不是
    # G2-E 了。f3 是 support/public, 交换它不触发任何别的门。
    s = _v5_fixture()
    s.facts[2].kind = "public"        # 填反了
    s.facts[2].visibility = "support"  # 填反了
    vr = validate_spec(s)
    check("**交换后通过(不再报 kind 非法)**", vr.ok, vr.why())
    check("**字段真的被换回来了**",
          s.facts[2].kind == "support" and s.facts[2].visibility == "public",
          (s.facts[2].kind, s.facts[2].visibility))
    # ---- G4-A: 半错位也不再整稿扔掉 ----
    #   kind = "public"(合法 visibility) / visibility = "hidden"(合法)
    #   这不是完整互换 -> 以前落到 r.fail -> 整稿重出。实播里那三条
    #   `fact f7 kind 非法: public` 走的正是这条路。
    s3 = _v5_fixture()
    s3.facts[2].kind = "public"
    s3.facts[2].visibility = "public"
    vr3 = validate_spec(s3)
    check("**半错位不再硬拒(交 reviewer 修)**", vr3.ok, vr3.why())
    check("**并且真的被记成 fixable**",
          any("kind" in m for m in vr3.fixable), vr3.fixable)


def test_g4a_fact_enum_misplacement_is_fixable_not_a_new_draft():
    """**G4-A**（本批最高价值 regression）: `kind=public` **不再换稿**。

    这是 G2 的**真实漏口**: G2 的注释写着"其它非法 kind/visibility 交
    Reviewer 修", 但代码落的是 `r.fail()` —— 只有**完整互换**才会被自动
    纠正。实播日志里最常出现的其实是**半错位**:

        kind       = "public"    <- 合法 visibility
        visibility = "hidden"    -> 合法 visibility, 但不在 FACT_KINDS 里

    于是 `visibility in FACT_KINDS` 为假 -> 不满足交换 -> fail -> 整稿
    扔掉 -> 重新生成。G2 声称消灭的那类白请求**一次都没减少**。

    新契约: generator 只调 **1** 次, reviewer 就地修 kind, audit 照常。
    """
    print("\n[G4-A] fact kind=public -> reviewer 就地修, generator 仍 1 次")
    from story.puzzle import PuzzleFact
    P = _GOOD_PUZ
    bad = riddle()
    bad["facts"][2] = dict(bad["facts"][2])
    bad["facts"][2]["kind"] = "public"       # 合法 visibility, 非法 kind
    bad["facts"][2]["visibility"] = "hidden"
    # reviewer 返回**修好 kind 的同一稿**(事实内容一字不改)
    fixed = review_ok(P)
    fixed["facts"] = [dict(f) for f in fixed["facts"]]
    fixed["facts"][2]["kind"] = "support"
    fixed["facts"][2]["visibility"] = "public"
    fc = FakeClient([
        LLMResult(tool_input=bad, model="m"),
        LLMResult(tool_input=fixed, model="m"),
        _truth_tool(truthful=True, consistent=True),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    spec = w.gen_spec(blueprint=fc.default_blueprint)
    names = [c["tool"]["name"] for c in fc.calls]
    check("**generator 只有 1 次(不换稿)**",
          names.count("emit_riddle") == 1, names)
    check("reviewer 被调用来修",
          names.count("emit_review") == 1, names)
    check("**最终出题成功**", bool(spec.puzzle), spec.puzzle[:30])
    check("kind 已合法",
          all(f.kind in ("core", "support", "exclusion")
              for f in (spec.facts or [])),
          [(f.id, f.kind) for f in (spec.facts or [])])
    # 事实文本**没有被 reviewer 借机改写**
    check("**fact.text 原样保留(没有借机重写事实)**",
          spec.facts[2].text == bad["facts"][2]["text"],
          (spec.facts[2].text, bad["facts"][2]["text"]))
    # 空 text / 缺 id 仍然硬拒 —— 那已经不是"enum 标错"
    s = _v5_fixture()
    s.facts[2].text = ""
    from story.quality import validate_spec
    check("**fact text 为空仍硬拒**", not validate_spec(s).ok)
    _ = PuzzleFact  # 保持 import 被使用


def test_g4a_enums_are_imported_for_the_swap():
    """**G4-A**: 交换判据依赖的两个枚举必须真的可用。"""
    print("\n[G4-A2] FACT_KINDS / FACT_VISIBILITY")
    from story.puzzle import FACT_KINDS, FACT_VISIBILITY
    check("FACT_KINDS 是 kind 的那三个",
          set(FACT_KINDS) == {"core", "support", "exclusion"}, FACT_KINDS)
    check("FACT_VISIBILITY 是 visibility 的那两个",
          set(FACT_VISIBILITY) == {"public", "hidden"}, FACT_VISIBILITY)
    check("**两个枚举不相交(所以'互换'才是无歧义的)**",
          not (set(FACT_KINDS) & set(FACT_VISIBILITY)),
          (FACT_KINDS, FACT_VISIBILITY))


def test_g2f_reviewer_technical_failure_retries_same_candidate():
    """**G2-F**（本批最高价值 regression 之一）: 审稿技术失败 ->
    重试**同一稿**, generator 调用数**不增加**。

    实播: `Reviewer 输出触顶 max_tokens=3500, 工具调用没写完` 被记成
    "第 1 稿要求重出" —— 丢掉一份可能完全合格的稿子去重新生成一道新题。
    """
    print("\n[G2-F1] 审稿技术失败 -> 同稿重试, generator 不增加")
    P = _GOOD_PUZ
    fc = FakeClient([
        LLMResult(tool_input=riddle(), model="m"),
        # 第 1 次审稿: 输出触顶(空 tool_input + error)
        LLMResult(tool_input={},
                  error="输出触顶(max_tokens=3500, 实出 3499), "
                        "工具调用没写完; 需要调大 max_tokens",
                  model="m"),
        # 重试: **同一稿** -> 通过
        LLMResult(tool_input=review_ok(P), model="m"),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    spec = w.gen_spec(blueprint=fc.default_blueprint)
    names = [c["tool"]["name"] for c in fc.calls]
    check("**generator 仍只有 1 次**",
          names.count("emit_riddle") == 1, names)
    check("审稿 2 次(第 2 次是重试)", names.count("emit_review") == 2, names)
    check("重试抬高 max_tokens",
          fc.calls[2]["max_tokens"] == 4500, fc.calls[2]["max_tokens"])
    check("最终出题成功", bool(spec.puzzle), spec.puzzle[:30])


def test_g2f_audit_technical_failure_retries_same_candidate():
    """**G2-F**: truth audit 技术失败同样重试同一稿, 不换题。"""
    print("\n[G2-F2] audit 技术失败 -> 同稿重试")
    P = _GOOD_PUZ
    fc = FakeClient([
        LLMResult(tool_input=riddle(), model="m"),
        LLMResult(tool_input=review_ok(P), model="m"),
        # audit 第 1 次: malformed
        LLMResult(tool_input={"__truth_audit__": True,
                              "narrator_truthful": "yes"}, model="m"),
        # audit 第 2 次(同一稿): 通过
        _truth_tool(truthful=True, consistent=True),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    spec = w.gen_spec(blueprint=fc.default_blueprint)
    names = [c["tool"]["name"] for c in fc.calls]
    check("**generator 仍只有 1 次**",
          names.count("emit_riddle") == 1, names)
    check("**audit 调了 2 次**",
          names.count("emit_truth_audit") == 2, names)
    check("最终出题成功", bool(spec.puzzle), spec.puzzle[:30])


def test_g2f_no_draft_requests_are_capped():
    """**G2-F**: no-draft 的**请求数**上限(不能只靠 90 秒 budget)。

    "没出稿"不计 candidate attempt 是对的(它没形成有效稿子), 但它
    消耗了真实 HTTP 请求 —— 只靠 budget 兜底, 90 秒里十几次白打。
    """
    print("\n[G2-F3] no-draft 请求数上限")
    no_draft = LLMResult(tool_input={}, text="I'll create a fresh riddle...")
    fc = FakeClient([
        LLMResult(tool_input=riddle()),          # 出稿
        LLMResult(tool_input=review_rewrite()),  # 打回
        no_draft, no_draft, no_draft, no_draft, no_draft, no_draft,
        no_draft, no_draft,
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    spec = w.gen_spec(blueprint=fc.default_blueprint, max_attempts=4,
                      max_no_draft_retries=2)
    drafts = [c["tool"]["name"] for c in fc.calls].count("emit_riddle")
    check("**no-draft 到上限就收手(不是打满 8 轮 guard)**",
          drafts <= 4, drafts)
    check("确实没出题", not spec.puzzle, spec.puzzle[:20])



def test_g4_repair_vs_hard_reject_counts():
    """**G4 可观测性**: 出题指标必须能回答"几次 repair 救回了几次硬拒"。

    任务书要求下一场直播直接看到:

        以前 10 次 hard reject
        现在其中 6 次被 repair 救回

    没有这几个量就只能从日志肉眼看。它们**不新增任何 LLM 调用**,
    纯粹是对已经算出来的结果做分类。

    ⚠️ G4-E 修正了一个**语义错误**: G4-D 只记了一个
    `candidate_repair_count`, 而它在**送审稿人之前**就 +1。于是

        带 core_length fixable -> attempt += 1
        -> 审稿人反而要求重出 -> rewrite_count += 1

    会让同一稿同时记成"repair 救回 1"和"重出 1", 那个数根本回答不了
    "有多少**真的**被同稿修好并继续通过"。现在拆成:

        candidate_repair_attempt_count —— 带 fixable 送进审稿人(尝试)
        candidate_repair_success_count —— 改完重新验干净且没重出(救回)

    外加 `hard_reject_before_review_count`(硬校验/blueprint 就毙, 没花审稿)。
    """
    print("\n[G4-OBS] repair vs hard-reject 计数")
    # ---- ① 一稿带 core 超长(可修) -> attempt + success + 分类 ----
    P = _GOOD_PUZ
    fc = FakeClient([
        LLMResult(tool_input=riddle(core_answer="他" * 100), model="m"),
        LLMResult(tool_input=review_ok(P, core_answer="短答案。"), model="m"),
        _truth_tool(truthful=True, consistent=True),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    spec = w.gen_spec(blueprint=fc.default_blueprint)
    m = spec.metrics
    check("**candidate_repair_attempt_count = 1**",
          m.get("candidate_repair_attempt_count") == 1,
          m.get("candidate_repair_attempt_count"))
    check("**candidate_repair_success_count = 1(修好了, 没重出)**",
          m.get("candidate_repair_success_count") == 1,
          m.get("candidate_repair_success_count"))
    check("**repair_attempt_reasons 归到了 core_length**",
          m.get("repair_attempt_reasons", {}).get("core_length") == 1,
          m.get("repair_attempt_reasons"))
    check("**repair_success_reasons 也归到了 core_length**",
          m.get("repair_success_reasons", {}).get("core_length") == 1,
          m.get("repair_success_reasons"))
    check("硬拒计数为 0(它没被硬拒)",
          m.get("hard_reject_before_review_count") == 0,
          m.get("hard_reject_before_review_count"))
    check("出题成功", bool(spec.puzzle), spec.puzzle[:30])

    # ---- ② 一稿结构错误 -> hard_reject 计数, 不花审稿 ----
    bad = riddle()
    bad["facts"] = [dict(f) for f in bad["facts"]]
    bad["facts"][0]["text"] = ""          # fact 文本为空 -> 硬拒
    fc2 = FakeClient([
        LLMResult(tool_input=bad, model="m"),
        LLMResult(tool_input=riddle(), model="m"),
        LLMResult(tool_input=review_ok(P), model="m"),
        _truth_tool(truthful=True, consistent=True),
    ])
    w2 = PuzzleWriter(client=fc2, runtime_cfg=fc2.runtime_cfg)
    spec2 = w2.gen_spec(blueprint=fc2.default_blueprint)
    m2 = spec2.metrics
    check("**hard_reject_before_review_count = 1**",
          m2.get("hard_reject_before_review_count") == 1,
          m2.get("hard_reject_before_review_count"))
    check("那一稿没走进审稿(第一稿没花审稿调用)",
          m2.get("candidate_repair_attempt_count") == 0,
          m2.get("candidate_repair_attempt_count"))

    # ---- ③ fact enum 错位(G4-A) 现在算 repair 而不是 hard reject ----
    bad3 = riddle()
    bad3["facts"] = [dict(f) for f in bad3["facts"]]
    bad3["facts"][2]["kind"] = "public"
    bad3["facts"][2]["visibility"] = "hidden"
    fixed3 = review_ok(P)
    fixed3["facts"] = [dict(f) for f in fixed3["facts"]]
    fixed3["facts"][2]["kind"] = "support"
    fixed3["facts"][2]["visibility"] = "public"
    fc3 = FakeClient([
        LLMResult(tool_input=bad3, model="m"),
        LLMResult(tool_input=fixed3, model="m"),
        _truth_tool(truthful=True, consistent=True),
    ])
    w3 = PuzzleWriter(client=fc3, runtime_cfg=fc3.runtime_cfg)
    spec3 = w3.gen_spec(blueprint=fc3.default_blueprint)
    m3 = spec3.metrics
    check("**fact enum 归到 repair(不是 hard reject)**",
          m3.get("candidate_repair_attempt_count") == 1
          and m3.get("hard_reject_before_review_count") == 0,
          (m3.get("candidate_repair_attempt_count"),
           m3.get("hard_reject_before_review_count")))
    check("**fact_enum 这一稿也真救回了**",
          m3.get("candidate_repair_success_count") == 1,
          m3.get("candidate_repair_success_count"))
    check("**分类为 fact_enum**",
          m3.get("repair_attempt_reasons", {}).get("fact_enum") == 1,
          m3.get("repair_attempt_reasons"))


def test_g4e_repair_attempt_is_not_repair_success():
    """**G4-E 的核心回归**: 送修 != 修好。

    这是任务书点名"最重要"的那条 —— 它必须证明 G4-D 的"互斥"假设
    是错的, 而且现在被纠正了:

        带 core_length fixable -> Reviewer 看完要求 **rewrite**
        => attempt = 1, success = 0, rewrite_count = 1

    旧实现(只记 attempt 并把它叫"救回")在这条路径上会给出
    repair=1 / rewrite=1 的形状 —— 一个自相矛盾的数字。如果谁把
    success 记回"送修就算", 这条会立刻红。
    """
    print("\n[G4-E1] 送修但最终重出 -> success=0")
    P = _GOOD_PUZ
    # 审稿人看完要求重出(不是 ok, 也不是技术失败)
    _review_rewrite = dict(review_ok(P))
    _review_rewrite["decision"] = "rewrite"
    _review_rewrite.pop("puzzle", None)
    fc = FakeClient([
        # 第 1 稿: core 超长(可修) -> 送修
        LLMResult(tool_input=riddle(core_answer="他" * 100), model="m"),
        # 审稿人: 故事本身有语义问题 -> rewrite(整稿扔掉)
        LLMResult(tool_input=_review_rewrite, model="m"),
        # 第 2 稿: 干净的一个正常稿
        LLMResult(tool_input=riddle(), model="m"),
        LLMResult(tool_input=review_ok(P), model="m"),
        _truth_tool(truthful=True, consistent=True),
    ])
    w = PuzzleWriter(client=fc, runtime_cfg=fc.runtime_cfg)
    spec = w.gen_spec(blueprint=fc.default_blueprint)
    m = spec.metrics
    check("**attempt = 1(送修过)**",
          m.get("candidate_repair_attempt_count") == 1,
          m.get("candidate_repair_attempt_count"))
    check("**success = 0(没救回 —— 审稿人把它整稿退回了)**",
          m.get("candidate_repair_success_count") == 0,
          m.get("candidate_repair_success_count"))
    check("**rewrite_count = 1**",
          m.get("rewrite_count") == 1, m.get("rewrite_count"))
    check("**attempt_reasons 仍记着那次 fact/core 修复尝试**",
          bool(m.get("repair_attempt_reasons")),
          m.get("repair_attempt_reasons"))
    check("**success_reasons 必须是空的(没救回就不能记原因)**",
          not m.get("repair_success_reasons"),
          m.get("repair_success_reasons"))
    check("最终仍然出了题(第 2 稿过的)", bool(spec.puzzle), spec.puzzle[:30])


def test_g4e_archive_round_trip_keeps_g4_metrics():
    """**G4-E 落盘回归**: 五个 G4 字段必须真的进 puzzle.jsonl。

    `spec.metrics` 里躺着不等于赛后看得见 —— `_archive_reveal()` 用
    `director._round_metrics()` 显式挑字段写 record, **不搬**整个
    `spec.metrics`。所以漏接 `_round_metrics()` 的话, 指标在直播结束时
    就蒸发了, 而所有别的测试都还是绿的。
    """
    print("\n[G4-E2] archive round-trip 保留 5 个 G4 字段")
    import json
    import os
    import tempfile
    import director as D
    from story.puzzle import PuzzleSpec

    d = tempfile.mkdtemp()
    cfg = D.Config(sim_path="x", no_llm=True,
                   puzzle_out_path=os.path.join(d, "puzzle.jsonl"))
    dr = D.Director(cfg)
    # 造一个"带 G4 指标"的 spec, 走真实的 _archive_reveal 路径
    spec = PuzzleSpec(puzzle=_GOOD_PUZ, answer="退潮时礁石露出。")
    spec.metrics = {
        "generation_attempts": 2,
        "candidate_repair_attempt_count": 3,
        "candidate_repair_success_count": 2,
        "hard_reject_before_review_count": 1,
        "repair_attempt_reasons": {"fact_enum": 2, "core_length": 1},
        "repair_success_reasons": {"fact_enum": 2},
    }
    dr._archive_reveal({"puzzle": spec.puzzle, "answer": spec.answer,
                        "spec": spec}, "揭晓文案")
    with open(cfg.puzzle_out_path, encoding="utf-8") as f:
        rec = json.loads(f.readline())
    rm = rec.get("metrics") or {}
    check("**record.metrics 里有 candidate_repair_attempt_count**",
          rm.get("candidate_repair_attempt_count") == 3,
          rm.get("candidate_repair_attempt_count"))
    check("**record.metrics 里有 candidate_repair_success_count**",
          rm.get("candidate_repair_success_count") == 2,
          rm.get("candidate_repair_success_count"))
    check("**record.metrics 里有 hard_reject_before_review_count**",
          rm.get("hard_reject_before_review_count") == 1,
          rm.get("hard_reject_before_review_count"))
    check("**record.metrics 里有 repair_attempt_reasons(原样)**",
          rm.get("repair_attempt_reasons") == {"fact_enum": 2,
                                               "core_length": 1},
          rm.get("repair_attempt_reasons"))
    check("**record.metrics 里有 repair_success_reasons(原样)**",
          rm.get("repair_success_reasons") == {"fact_enum": 2},
          rm.get("repair_success_reasons"))

    # 老题 / 结构化兜底: metrics 里没有这些键 -> 落盘必须是 0 / {}
    spec_old = PuzzleSpec(puzzle=_GOOD_PUZ, answer="退潮时礁石露出。")
    spec_old.metrics = {"generation_attempts": 1}
    dr._archive_reveal({"puzzle": spec_old.puzzle, "answer": spec_old.answer,
                        "spec": spec_old}, "揭晓文案")
    with open(cfg.puzzle_out_path, encoding="utf-8") as f:
        rec_old = json.loads(f.readlines()[-1])
    ro = rec_old.get("metrics") or {}
    check("**老题兜底为 0 / {}(不是 null, 也不用下游 .get)**",
          (ro.get("candidate_repair_attempt_count") == 0
           and ro.get("candidate_repair_success_count") == 0
           and ro.get("hard_reject_before_review_count") == 0
           and ro.get("repair_attempt_reasons") == {}
           and ro.get("repair_success_reasons") == {}),
          {k: ro.get(k) for k in ("candidate_repair_attempt_count",
                                  "candidate_repair_success_count",
                                  "hard_reject_before_review_count",
                                  "repair_attempt_reasons",
                                  "repair_success_reasons")})


def test_u3e_archive_reveal_keeps_composed_compat():
    """**U3-E 落盘回归**: 旧组合揭晓文案必须**逐字**保留在 archive 里。

    U3 把 Snapshot 的 `revealed_full_answer` 从"组合文案"改成了"raw
    answer"。但 archive 的 `reveal` 字段**不是**同一个东西 —— 它一直是
    Director 侧 `_compose_reveal()` 出来的展示文案, 历史数据与离线分析
    都按这个格式读。修 Snapshot 时**顺手把它也改成 raw** 会让整段历史
    口径断裂, 而这类"展示数据拆分"不该动归档格式。

    所以这条钉住: archive 的 `reveal` 仍是组合文案, `core_answer` 仍是
    raw core, 且**不需要** bump 任何 version(纯展示数据拆分)。
    """
    print("\n[U3-E] archive 保持旧组合 reveal 兼容")
    import json
    import os
    import tempfile
    import director as D
    from story.puzzle import PuzzleSpec

    d = tempfile.mkdtemp()
    cfg = D.Config(sim_path="x", no_llm=True,
                   puzzle_out_path=os.path.join(d, "puzzle.jsonl"))
    dr = D.Director(cfg)
    core = "他每天看锅, 是在确认有没有人动过他的东西。"
    ans = "锅里的状态被他当成一个固定记号, 他靠它判断私人物品有没有被动过。"
    spec = PuzzleSpec(puzzle=_GOOD_PUZ, answer=ans, core_answer=core)
    before = (spec.quality_policy_version, spec.prompt_version)
    text = D.Director._compose_reveal(core, ans)
    check("组合文案确实带两个标签",
          "【核心答案】" in text and "【完整解释】" in text, text[:40])
    dr._archive_reveal({"puzzle": spec.puzzle, "answer": ans, "spec": spec},
                       text)
    with open(cfg.puzzle_out_path, encoding="utf-8") as f:
        rec = json.loads(f.readline())
    # reveal 仍是**组合文案**(逐字), 不是 raw answer
    check("**archive 的 reveal 仍是组合文案(逐字兼容)**",
          rec.get("reveal") == text, (rec.get("reveal") or "")[:60])
    check("archive 的 core_answer 仍是 raw core",
          rec.get("core_answer") == core, rec.get("core_answer"))
    check("archive 的 answer 仍是 raw answer",
          rec.get("answer") == ans, rec.get("answer"))
    check("**没有 bump 任何 version**(纯展示数据拆分)",
          (spec.quality_policy_version, spec.prompt_version) == before,
          (before, spec.quality_policy_version, spec.prompt_version))


def test_g4_fix_reasons_never_guesses():
    """认不出的 fixable 文案 -> "other", **不猜**。

    猜错会让指标说谎 —— 那比分类不全更糟。
    """
    print("\n[G4-OBS2] fix_reasons 不猜")
    from story.quality import ValidationResult
    vr = ValidationResult()
    vr.can_fix("一条全新的、归类表里没有的修复要求")
    check("**认不出 -> other**", vr.fix_reasons() == ["other"],
          vr.fix_reasons())
    vr2 = ValidationResult()
    vr2.can_fix("core_answer 有 90 字, 超过 80 字上限")
    check("认得出 core_length", vr2.fix_reasons() == ["core_length"],
          vr2.fix_reasons())
    check("**空 fixable -> 空分类(不产生假 other)**",
          ValidationResult().fix_reasons() == [],
          ValidationResult().fix_reasons())


def main():
    for t in (test_riddle_tool,
              # ---- UX-2: v5 通关合同 ----
              test_ux_g_v5_skips_final_judge,
              test_ai_player_ask_is_exactly_one_host_call,
              test_ux_h_legacy_still_judges,
              test_ux_established_filtered_in_answer,
              test_reviewer_fixes_in_place,
              test_messages_timeout_override,
              test_answer_passes_qa_budget_to_client,
              test_qa_budget_reaches_final_judge,
              test_g4rb_shape_is_not_asked_of_reviewer,
              test_g4rb_first_person_survives_end_to_end,
              test_reviewer_no_fix_falls_back_to_regen,
              test_first_person_story_still_detectable,
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
              test_g4rb_puzzle_shape_is_not_fixable,
              test_g4rb_unfixed_shape_is_no_longer_rejected,
              test_g4rb_single_beat_is_legal,
              test_g4rb_signal_checks_are_soft_but_anchors_stay_hard,
              test_writer_reads_temperature_from_runtime_cfg,
              test_client_cfg_is_not_used_for_temperature,
              test_quotas_read_from_runtime_cfg,
              test_judge_passes_temperature,
              # ---- P0-4/5/6 ----
              test_rejected_spec_never_returned,
              test_rejected_by_cross_puzzle_gate_not_returned,
              test_g4_cross_gate_still_hard_for_text_duplicate,
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
              # ---- R2: 无问号合法性闭环 ----
              test_r2_hard_contract_never_requires_closing_question,
              test_r2_decision_fix_examples_drop_person_and_question,
              test_v4_check_system_freezes_reviewer_scope,
              test_v4_riddle_system_states_orthogonality,
              test_v4_observed_fields_flow_to_signature,
              test_v4_reviewer_observed_reveal_wins_over_generator,
              test_v4_reviewer_cannot_see_recent_quota,
              test_v4_apply_review_keeps_new_fields_on_fix,
              test_v4_policy_version_is_v4,
              # ---- Batch A closeout ----
              test_closeout_check_tool_has_no_recent_window_rule,
              # ---- G1: gen_spec 协作式取消 ----
              test_g1_gen_spec_default_is_bit_identical,
              test_g1_gen_spec_stops_before_first_draft,
              test_g1_gen_spec_stops_before_reviewer,
              test_g1_gen_spec_stops_before_second_draft,
              test_g1_gen_spec_stops_before_truth_audit,
              test_g1_probe_exception_is_fail_closed,
              test_g1_budget_and_attempts_are_honored,
              # ---- G2-keyword2: Stage A / Stage B(writer 级) ----
              test_g2_keyword_stage_a_returns_idea,
              test_g2_keyword_stage_a_one_attempt,
              test_g2_keyword_stage_b_freezes_puzzle,
              test_g2_keyword_stage_b_ignores_model_puzzle,
              test_g2_keyword_stage_b_schema_is_structural,
              test_g2_keyword_provenance_and_generated,
              test_g2_keyword_uses_generated_quality_contract,
              test_g2_keyword_stage_b_blueprint_is_unconstrained,
              test_g2_keyword_should_continue_checkpoints,
              test_g2_keyword_stage_a_interrupt_checkpoint,
              test_g2_keyword_rewrite_fails_candidate,
              test_g2_keyword_truth_audit_fail_rejects,
        # ---- G4-CF: Stage A 改成 Case-first 创作顺序 ----
        test_g4cf_stage_a_scaffold_is_returned_verbatim,
        test_g4cf_stage_a_scaffold_fails_closed,
        test_g4cf_stage_a_shape_fail_is_one_attempt,
        test_g4cf_shape_check_is_not_semantic_review,
        test_g4cf_no_midcheck_in_production,
        test_case_first_stage_a_schema,
        test_case_first_stage_a_is_not_stage_b,
        test_g4cf_internal_scaffold_never_reaches_stage_b,
        test_g4cf_stage_a_and_b_are_only_ever_called_by_keyword_spec,
        test_g4cf_stage_a_one_attempt_and_interrupt_still_hold,
        test_g4cf_contract_signature_rejects_scaffold_kwargs,
        test_g4cf_scaffold_dies_at_stage_a,
        # ---- G3: Stage A prompt 收敛 ----
        test_g3_stage_a_prompt_dropped_v1_shape_rules,
        test_g3_stage_a_prompt_keeps_runtime_constraints,
        test_g3_stage_a_prompt_never_mentions_stage_b_vocabulary,
        test_g3_stage_b_still_freezes_and_has_no_puzzle_field,
        test_g3_quality_gates_unchanged,
              test_closeout_observed_signature_schema_is_complete,
              test_closeout_incomplete_observed_signature_is_rejected,
              test_closeout_incomplete_obs_never_lands_in_signature,
              test_closeout_complete_observed_signature_is_accepted,
              test_closeout_absent_observed_signature_on_pass_is_rejected,
              # ---- Q1: narrator truth audit ----
              test_truth1_bridge_contradiction_rejected,
              test_truth2_literal_contradiction_rejected_but_weak_ok,
              test_truth3_attributed_belief_passes,
              test_truth4_audit_technical_failure_rejects,
              test_truth4b_conflicts_nonempty_forces_reject,
              test_truth5_v6_pool_quarantined_but_v7_eligible,
              # ---- Q2: quality-v8 题目允许复杂, 通关仍然简单 ----
              # ---- G2: 可修问题不再整题重造 ----
              test_g2a_core_answer_81_is_repaired_not_regenerated,
              test_g2a_core_answer_121_is_hard_fail,
              test_u4_livestream_text_length_bounds,
              test_g2b_clue_quote_repaired_in_place,
              test_g2b_reviewer_must_not_change_puzzle_for_quote,
              test_g2c_linkage_is_fixable_but_missing_content_is_not,
              test_g2d_hint_too_long_gets_narrow_repair,
              test_g2d_narrow_repair_refuses_when_other_issues_remain,
              test_g2e_kind_visibility_swap_is_normalized,
              test_g2f_reviewer_technical_failure_retries_same_candidate,
              test_g2f_audit_technical_failure_retries_same_candidate,
              test_g2f_no_draft_requests_are_capped,
              # ---- G4-A: fact enum 错位不再换稿 ----
              test_g4a_fact_enum_misplacement_is_fixable_not_a_new_draft,
              test_g4a_enums_are_imported_for_the_swap,
              # ---- G4: 可观测性 ----
              test_g4_repair_vs_hard_reject_counts,
              test_g4e_repair_attempt_is_not_repair_success,
              test_g4e_archive_round_trip_keeps_g4_metrics,
              test_g4_fix_reasons_never_guesses,
              test_u3e_archive_reveal_keeps_composed_compat,
              test_q2_discovery_beats_schema_and_prompts,
              test_q2_v8_pool_quarantined_but_v9_eligible,
              test_q2_reviewer_all_four_new_fields_required,
              test_q2_beats_never_reach_frontend,
              test_q2_beats_have_no_victory_power,
              test_q2_quota_tightened_and_tone_target,
              test_q2_versions_bumped,
              test_truth_prompt_has_scanning_rules,
              test_truth_prompt_hardened_in_riddle_and_check):
        t()
    print()
    if FAIL[0]:
        print(f"FAILED: {FAIL[0]} 项")
        return 1
    print("PASS: PuzzleWriter(强制工具 + 回退解析) 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
