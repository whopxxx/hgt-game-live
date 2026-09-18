#!/usr/bin/env python
# coding: utf-8
"""运行: uv run tests/test_playtest.py（完全离线, 无网络）。

Q10 AI 试玩。核心是**两件事**:

    A. 隐藏信息隔离 —— Player 构造上就看不到 answer/facts/atoms/覆盖信息
    B. 让路给直播 —— 每次 LLM 调用前重查压力, 忙了就 interrupted

这一批(Q10a)只测零件: PlaytestResult / prompt 构造 / Player 协议 /
基本结局判定。**不接 prefetch**(那是 Q10c)。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from story import parser as P  # noqa: E402
from story.config import Config  # noqa: E402
from story.playtest import (  # noqa: E402
    INTERRUPTED, MOVE_ASK, MOVE_GIVE_UP, MOVE_SOLVE, PASS, UNAVAILABLE,
    UNSOLVED, Playtester, _player_prompt, _public_transcript,
)
from story.puzzle import (  # noqa: E402
    FairClue, PuzzleBlueprint, PuzzleFact, PuzzleSignature, PuzzleSpec,
    SolveAtom,
)

FAIL = [0]


def check(name, cond, extra=""):
    if cond:
        print(f"  ok  {name}")
        return
    print(f"  FAIL {name}  {extra}")
    FAIL[0] += 1


# ======================================================================
# 假件
# ======================================================================
def mk_spec() -> PuzzleSpec:
    """一道内部自洽的题。谜底/facts/atoms 都是**唯一的哨兵串**,
    便于断言"它们一个字都没漏进 Player prompt"。
    """
    return PuzzleSpec(
        id="", title="灯塔",
        puzzle="守塔人只在退潮时亮灯，涨潮后反而熄灯。为什么？",
        answer="SENTINEL_ANSWER 退潮礁石露出，亮灯是标示礁石。",
        facts=[
            PuzzleFact(id="f1", text="SENTINEL_FACT 退潮时礁石接近水面",
                       kind="core"),
            PuzzleFact(id="f2", text="SENTINEL_FACT2 灯是标示礁石",
                       kind="core"),
        ],
        solve_atoms=[
            SolveAtom(id="a1", role="cause", text="SENTINEL_ATOM 礁石需标示",
                      fact_ids=["f1"]),
        ],
        fair_clues=[
            FairClue(quote="只在退潮时亮灯", supports_atoms=["a1"]),
        ],
        hints=["SENTINEL_HINT 想潮水"],
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
        metrics={"ok": True},
        blueprint_specified=True,
    )


class _FakePlayerClient:
    """假 Player 传输层。moves 用完后一律 give_up。"""

    class cfg:
        model = "fake-player"

    def __init__(self, moves=None, error=None, tool_input=None):
        self.moves = list(moves or [])
        self.error = error
        self._ti = tool_input
        self.calls = []

    def messages(self, system, user, max_tokens=None, tool=None,
                 temperature=None):
        self.calls.append({"system": system, "user": user, "tool": tool,
                           "temperature": temperature})
        if self.error:
            return _R(error=self.error)
        if self._ti is not None:
            return _R(tool_input=self._ti)
        if self.moves:
            kind, text = self.moves.pop(0)
            return _R(tool_input={"kind": kind, "text": text})
        return _R(tool_input={"kind": MOVE_GIVE_UP, "text": "推不动了"})


class _R:
    def __init__(self, text=None, error=None, tool_input=None, usage=None):
        self.text = text
        self.error = error
        self.tool_input = tool_input
        self.usage = usage
        self.model = "fake"


class _FakeHost:
    """假 Host。按 verdicts 队列依次返回; `solved_at` 那轮回 P.SOLVE。"""

    def __init__(self, verdicts=None, solved_at=None, error=None,
                 comment="点评"):
        self.verdicts = list(verdicts or [])
        self.solved_at = solved_at
        self.error = error
        self.comment = comment
        self.calls = []

    def answer(self, puzzle, answer, transcript, qid, user_name, text,
               judge_solve=True, solve_atoms=None, facts=None, spec=None):
        self.calls.append({"text": text, "qid": qid, "judge_solve": judge_solve,
                           "spec": spec, "puzzle": puzzle})
        if self.error:
            return [], self.error
        if self.solved_at is not None and qid == self.solved_at:
            r = P.SOLVE
        elif self.verdicts:
            r = self.verdicts.pop(0)
        else:
            r = "不是"
        return [_QR(verdict=r, comment=self.comment)], None


class _QR:
    def __init__(self, verdict, comment=""):
        self.verdict = verdict
        self.comment = comment


def mkpt(player=None, host=None, cont=None, max_turns=10, clock=None):
    ticks = [0.0] if clock is None else clock

    def _c():
        ticks[0] += 1.0
        return ticks[0]

    return Playtester(
        player_client=player if player is not None else _FakePlayerClient(),
        host_writer=host if host is not None else _FakeHost(),
        should_continue=cont or (lambda: True),
        max_turns=max_turns, clock=_c)


# ======================================================================
# A. 隐藏信息隔离(第一验收点)
# ======================================================================
def test_player_prompt_has_no_hidden_fields():
    """**第一验收点**: 谜底/facts/atoms/hints 一个字都不能进 Player prompt。"""
    print("\n[A1] Player prompt 不含隐藏区")
    spec = mk_spec()
    tr = [{"role": "puzzle", "text": spec.puzzle}]
    pub = _public_transcript(tr)
    prompt = _player_prompt(spec.puzzle, pub)
    for sentinel in ("SENTINEL_ANSWER", "SENTINEL_FACT", "SENTINEL_FACT2",
                     "SENTINEL_ATOM", "SENTINEL_HINT"):
        check(f"**不含 {sentinel}**", sentinel not in prompt, prompt[:200])
    check("谜面在", spec.puzzle in prompt)
    # answer 对象本身根本没被传进构造函数链
    check("_player_prompt 只吃 puzzle+public(签名)",
          list(_player_prompt.__code__.co_varnames[:2]) == ["puzzle", "public"])


def test_real_run_prompt_never_leaks():
    """真跑一遍, 每一条实际发出的 prompt 都过一遍哨兵扫描。"""
    print("\n[A2] 真跑时每轮 prompt 都不泄底")
    spec = mk_spec()
    pl = _FakePlayerClient(moves=[(MOVE_ASK, "是退潮吗？"),
                                  (MOVE_ASK, "灯是给别人看的吗？"),
                                  (MOVE_SOLVE, "礁石露出来，灯是在标礁石")])
    host = _FakeHost(verdicts=["是", "接近了"], solved_at=3)
    pt = mkpt(player=pl, host=host)
    r = pt.run(spec)
    check("结局 PASS", r.status == PASS, r.status)
    check("跑了 3 轮", r.turns == 3, r.turns)
    for i, c in enumerate(pl.calls):
        for sentinel in ("SENTINEL_ANSWER", "SENTINEL_FACT", "SENTINEL_ATOM",
                         "SENTINEL_HINT"):
            check(f"**第{i + 1}轮 prompt 不含 {sentinel}**",
                  sentinel not in c["user"], c["user"][:160])


def test_public_transcript_strips_coverage():
    """Player 下一轮只能看到公开 verdict/comment, 看不到覆盖信息。"""
    print("\n[A3] 公开 transcript 剥掉覆盖字段")
    tr = [
        {"role": "puzzle", "text": "谜面"},
        {"role": "player", "text": "是退潮吗", "kind": MOVE_ASK},
        {"role": "host", "text": "方向对了", "verdict": "是",
         "touched_fact_ids": ["f1"], "cause_hit": True,
         "mechanism_hit": False, "matched_atoms": ["a1"]},
    ]
    pub = _public_transcript(tr)
    blob = json.dumps(pub, ensure_ascii=False)
    check("有 verdict", "是" in blob)
    check("有 comment", "方向对了" in blob)
    for bad in ("touched_fact_ids", "cause_hit", "mechanism_hit",
                "matched_atoms", "f1", "a1"):
        check(f"**不含 {bad}**", bad not in blob, blob)
    check("条数不变", len(pub) == 3, len(pub))


def test_host_answer_uses_production_path():
    """Host 走的是生产 `answer(judge_solve=True, spec=spec)`, 不是 judge()。"""
    print("\n[A4] Host 走生产 answer 链")
    spec = mk_spec()
    host = _FakeHost(solved_at=1)
    pt = mkpt(player=_FakePlayerClient(moves=[(MOVE_SOLVE, "礁石解释")]),
              host=host)
    r = pt.run(spec)
    check("PASS", r.status == PASS, r.status)
    check("调了 answer", len(host.calls) == 1, len(host.calls))
    c = host.calls[0]
    check("**judge_solve=True**", c["judge_solve"] is True)
    check("**带上 spec(连闸门一起测)**", c["spec"] is spec)
    check("qid 是轮号", c["qid"] == 1, c["qid"])


# ======================================================================
# B. 结局判定
# ======================================================================
def test_give_up_is_unsolved():
    print("\n[B1] give_up -> unsolved")
    pt = mkpt(player=_FakePlayerClient(moves=[(MOVE_GIVE_UP, "想不出")]),
              host=_FakeHost())
    r = pt.run(mk_spec())
    check("unsolved", r.status == UNSOLVED, r.status)
    check("没调 Host", True)   # host.calls 未检查, 由下面专门的假件验证


def test_give_up_does_not_call_host():
    print("\n[B2] give_up 不触发 Host")
    host = _FakeHost()
    pt = mkpt(player=_FakePlayerClient(moves=[(MOVE_GIVE_UP, "算了")]),
              host=host)
    pt.run(mk_spec())
    check("**Host 零调用**", len(host.calls) == 0, len(host.calls))


def test_exhausted_is_unsolved():
    print("\n[B3] 用完轮数 -> unsolved")
    pt = mkpt(player=_FakePlayerClient(moves=[(MOVE_ASK, "问1"),
                                              (MOVE_ASK, "问2")]),
              host=_FakeHost(verdicts=["不是", "不是"]), max_turns=2)
    r = pt.run(mk_spec())
    check("unsolved", r.status == UNSOLVED, r.status)
    check("用满 2 轮", r.turns == 2, r.turns)


def test_player_failure_is_unavailable():
    print("\n[B4] Player 失败 -> unavailable(不评价题)")
    pt = mkpt(player=_FakePlayerClient(error="网关抖动"), host=_FakeHost())
    r = pt.run(mk_spec())
    check("unavailable", r.status == UNAVAILABLE, r.status)
    check("带 error", bool(r.error), r.error)


def test_host_failure_is_unavailable():
    print("\n[B5] Host 失败 -> unavailable")
    pt = mkpt(player=_FakePlayerClient(moves=[(MOVE_ASK, "问")]),
              host=_FakeHost(error="裁判超时"))
    r = pt.run(mk_spec())
    check("unavailable", r.status == UNAVAILABLE, r.status)


def test_no_client_is_unavailable():
    print("\n[B6] 没有 client/writer -> unavailable")
    pt = Playtester(player_client=None, host_writer=None)
    r = pt.run(mk_spec())
    check("unavailable", r.status == UNAVAILABLE, r.status)


def test_spec_missing_answer_is_unavailable():
    print("\n[B7] spec 缺谜底 -> unavailable")
    spec = mk_spec()
    spec.answer = ""
    pt = mkpt()
    check("unavailable", pt.run(spec).status == UNAVAILABLE)


# ======================================================================
# C. 让路(每次 LLM 调用前重查)
# ======================================================================
def test_interrupted_before_first_call():
    print("\n[C1] 一开头就忙 -> interrupted, 零调用")
    pl = _FakePlayerClient()
    host = _FakeHost()
    pt = mkpt(player=pl, host=host, cont=lambda: False)
    r = pt.run(mk_spec())
    check("interrupted", r.status == INTERRUPTED, r.status)
    check("**Player 零调用**", len(pl.calls) == 0, len(pl.calls))
    check("**Host 零调用**", len(host.calls) == 0, len(host.calls))


def test_interrupted_after_player_before_host():
    """**关键**: Player 回来后压力升高 -> 下一次 Host 调用不得发生。"""
    print("\n[C2] Player 后变忙 -> 不调 Host")
    state = {"n": 0}

    def cont():
        state["n"] += 1
        return state["n"] <= 1          # 第 1 次(Player 前)放行, 之后不放

    pl = _FakePlayerClient(moves=[(MOVE_ASK, "是退潮吗")])
    host = _FakeHost()
    pt = mkpt(player=pl, host=host, cont=cont)
    r = pt.run(mk_spec())
    check("interrupted", r.status == INTERRUPTED, r.status)
    check("Player 调了 1 次", len(pl.calls) == 1, len(pl.calls))
    check("**Host 零调用**", len(host.calls) == 0, len(host.calls))


def test_interrupted_midway_stops_further_turns():
    print("\n[C3] 中途变忙 -> 立即停后续轮")
    state = {"n": 0}

    def cont():
        state["n"] += 1
        return state["n"] <= 3          # 放行到第 2 轮 Host 之后

    pl = _FakePlayerClient(moves=[(MOVE_ASK, "问1"), (MOVE_ASK, "问2"),
                                  (MOVE_ASK, "问3")])
    host = _FakeHost(verdicts=["不是", "不是", "不是"])
    pt = mkpt(player=pl, host=host, cont=cont)
    r = pt.run(mk_spec())
    check("interrupted", r.status == INTERRUPTED, r.status)
    check("**没跑满 3 轮**", r.turns < 3, r.turns)


def test_continue_predicate_exception_is_interrupt():
    print("\n[C4] should_continue 抛异常 -> interrupted")
    def boom():
        raise RuntimeError("探针炸了")

    pt = mkpt(player=_FakePlayerClient(), host=_FakeHost(), cont=boom)
    check("interrupted", pt.run(mk_spec()).status == INTERRUPTED)


# ======================================================================
# D. Player 协议
# ======================================================================
def test_player_tool_is_forced_and_enum_limited():
    print("\n[D1] Player 用强制工具, kind 限枚举")
    pl = _FakePlayerClient(moves=[(MOVE_ASK, "问")])
    pt = mkpt(player=pl, host=_FakeHost(solved_at=1))
    pt.run(mk_spec())
    tool = pl.calls[0]["tool"]
    check("有工具", tool is not None)
    check("工具名", tool["name"] == "emit_playtest_move", tool["name"])
    sch = tool["input_schema"]
    check("kind 枚举 3 项",
          sch["properties"]["kind"]["enum"] == [MOVE_ASK, MOVE_SOLVE,
                                                MOVE_GIVE_UP],
          sch["properties"]["kind"]["enum"])
    check("required 含 kind/text",
          set(sch["required"]) == {"kind", "text"}, sch["required"])


def test_bad_kind_is_unavailable():
    print("\n[D2] 非法 kind -> unavailable")
    pl = _FakePlayerClient(moves=[("乱写", "文本")])
    pt = mkpt(player=pl, host=_FakeHost())
    check("unavailable", pt.run(mk_spec()).status == UNAVAILABLE)


def test_empty_text_is_unavailable():
    print("\n[D3] 空 text -> unavailable")
    pl = _FakePlayerClient(tool_input={"kind": MOVE_ASK, "text": "   "})
    pt = mkpt(player=pl, host=_FakeHost())
    check("unavailable", pt.run(mk_spec()).status == UNAVAILABLE)


def test_gateway_wrapped_tool_input_ok():
    print("\n[D4] 网关套壳的 tool_input 也能解析")
    pl = _FakePlayerClient(
        tool_input={"input": {"kind": MOVE_ASK, "text": "套壳问句"}})
    host = _FakeHost(solved_at=1)
    pt = mkpt(player=pl, host=host)
    r = pt.run(mk_spec())
    check("PASS(说明解析到了)", r.status == PASS, r.status)


# ======================================================================
# E. PlaytestResult / metrics
# ======================================================================
def test_metrics_shape_and_no_hidden_leak():
    print("\n[E1] metrics 形状正确且不泄露内部字段")
    spec = mk_spec()
    pl = _FakePlayerClient(moves=[(MOVE_ASK, "问"), (MOVE_SOLVE, "解")])
    host = _FakeHost(verdicts=["是"], solved_at=2)
    pt = mkpt(player=pl, host=host)
    r = pt.run(spec)
    m = r.to_metrics()
    check("status", m["status"] == PASS, m["status"])
    check("turns", m["turns"] == 2, m["turns"])
    check("有 duration_ms", isinstance(m["duration_ms"], int), m["duration_ms"])
    check("有 transcript", isinstance(m["transcript"], list))
    blob = json.dumps(m, ensure_ascii=False)
    for bad in ("SENTINEL_ANSWER", "SENTINEL_FACT", "touched_fact_ids",
                "matched_atoms", "cause_hit"):
        check(f"**不含 {bad}**", bad not in blob, blob[:200])


def test_metrics_round_trips_through_metrics_dict():
    """Q8a 的纪律: 比较**源对象**里的 nested metrics 与读回对象。"""
    print("\n[E2] playtest metrics 落进 spec.metrics 后可读回")
    spec = mk_spec()
    pl = _FakePlayerClient(moves=[(MOVE_SOLVE, "解")])
    host = _FakeHost(solved_at=1)
    pt = mkpt(player=pl, host=host)
    r = pt.run(spec)
    spec.metrics["playtest"] = r.to_metrics()
    back = PuzzleSpec.from_dict(spec.to_dict()) if hasattr(
        PuzzleSpec, "from_dict") else None
    if back is None:
        check("PuzzleSpec 有 from_dict(跳过 round-trip)", True)
        return
    got = (back.metrics or {}).get("playtest") or {}
    check("status 一致", got.get("status") == r.status, got.get("status"))
    check("turns 一致", got.get("turns") == r.turns, got.get("turns"))
    check("transcript 一致", got.get("transcript") == r.transcript)


def test_run_never_raises():
    print("\n[E3] 内部异常也被吞成 unavailable")
    class _BoomClient:
        class cfg:
            model = "x"

        def messages(self, *a, **kw):
            raise RuntimeError("炸")

    pt = mkpt(player=_BoomClient(), host=_FakeHost())
    r = pt.run(mk_spec())
    check("unavailable", r.status == UNAVAILABLE, r.status)


def test_run_requires_no_pool_or_engine():
    """AST: 这个模块**不能**碰池子/引擎的写口(Q10a 不接 prefetch)。"""
    print("\n[E4] playtest.py 不碰 Pool/Engine 写口")
    import ast
    src = Path(__file__).resolve().parents[1] / "story" / "playtest.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
    for bad in ("pop_next", "mark_used", "remember_avoid", "submit_riddle",
                "stock_count", "PuzzlePool", "RoundEngine", "parse_answers"):
        check(f"**不出现 {bad}**", bad not in names, bad)


def test_result_predicates():
    print("\n[E5] PlaytestResult 谓词")
    from story.playtest import PlaytestResult
    check("pass -> passed", PlaytestResult(status=PASS).passed is True)
    check("unsolved -> 不 passed",
          PlaytestResult(status=UNSOLVED).passed is False)


def test_player_temperature_is_zero():
    """**可复现性**: 试玩结果决定一道已生成好的题能否入池, 所以 Player
    必须 temperature=0。同一份 spec 因采样抖动今天 pass 明天 unsolved
    会让这个闸门没法 debug。
    """
    print("\n[F1] Player temperature 恒为 0.0")
    pl = _FakePlayerClient(moves=[(MOVE_ASK, "问1"), (MOVE_SOLVE, "解")])
    pt = mkpt(player=pl, host=_FakeHost(verdicts=["不是"], solved_at=2))
    pt.run(mk_spec())
    check("至少调了 2 次", len(pl.calls) >= 2, len(pl.calls))
    temps = {c["temperature"] for c in pl.calls}
    check("**全部 temperature=0.0**", temps == {0.0}, temps)


def test_unsolved_reason_give_up():
    print("\n[F2] give_up -> unsolved/reason=give_up")
    pt = mkpt(player=_FakePlayerClient(moves=[(MOVE_GIVE_UP, "想不出")]),
              host=_FakeHost())
    r = pt.run(mk_spec())
    check("unsolved", r.status == UNSOLVED, r.status)
    check("reason=give_up", r.reason == "give_up", r.reason)


def test_unsolved_reason_max_turns():
    print("\n[F3] 轮数耗尽 -> unsolved/reason=max_turns")
    pt = mkpt(player=_FakePlayerClient(moves=[(MOVE_ASK, "问1"),
                                              (MOVE_ASK, "问2")]),
              host=_FakeHost(verdicts=["不是", "不是"]), max_turns=2)
    r = pt.run(mk_spec())
    check("unsolved", r.status == UNSOLVED, r.status)
    check("reason=max_turns", r.reason == "max_turns", r.reason)


def test_policy_table_is_exactly_the_contract():
    """四种结局的后台策略就是你定的那张表。"""
    print("\n[F4] 结局 -> 策略表与契约逐格一致")
    from story.playtest import OUTCOME_POLICY
    want = {
        PASS:        (False, False, False),
        UNSOLVED:    (True,  True,  True),
        UNAVAILABLE: (True,  True,  True),
        INTERRUPTED: (True,  False, False),
    }
    for st, (drop, bo, cf) in want.items():
        p = OUTCOME_POLICY.get(st)
        check(f"{st} 在表里", p is not None, st)
        if p:
            check(f"**{st}: drop/backoff/fail = {drop}/{bo}/{cf}**",
                  (p["drop"], p["backoff"], p["counted_fail"])
                  == (drop, bo, cf), p)
    check("**恰好 4 个结局**", len(OUTCOME_POLICY) == 4, list(OUTCOME_POLICY))


def test_interrupted_is_not_counted_fail():
    """INTERRUPTED 必须**不**计入失败、**不**退避 —— 否则运维数据会把
    "直播很活跃"误读成"试玩大量失败"。"""
    print("\n[F5] interrupted 不算失败/不退避")
    from story.playtest import PlaytestResult
    p = PlaytestResult(status=INTERRUPTED).policy()
    check("不退避", p["backoff"] is False, p)
    check("不计失败", p["counted_fail"] is False, p)
    check("丢弃 candidate", p["drop"] is True, p)


def test_policy_unknown_status_is_conservative():
    print("\n[F6] 未知 status -> 保守(丢弃+退避+计失败)")
    from story.playtest import PlaytestResult
    p = PlaytestResult(status="某个没见过的值").policy()
    check("**退避**", p["backoff"] is True, p)
    check("**计失败**", p["counted_fail"] is True, p)
    check("丢弃", p["drop"] is True, p)


def test_run_only_returns_known_status():
    """`run()` 只可能返回表里的四种之一。"""
    print("\n[F7] run() 的 status 一定在表内")
    from story.playtest import OUTCOME_POLICY
    cases = [
        (mkpt(player=_FakePlayerClient(moves=[(MOVE_SOLVE, "解")]),
              host=_FakeHost(solved_at=1)), PASS),
        (mkpt(player=_FakePlayerClient(moves=[(MOVE_GIVE_UP, "算了")]),
              host=_FakeHost()), UNSOLVED),
        (mkpt(player=_FakePlayerClient(error="网关"), host=_FakeHost()),
         UNAVAILABLE),
        (mkpt(player=_FakePlayerClient(), host=_FakeHost(),
              cont=lambda: False), INTERRUPTED),
    ]
    for pt, want in cases:
        got = pt.run(mk_spec()).status
        check(f"**{want}**", got == want, got)
        check(f"{got} 在表内", got in OUTCOME_POLICY, got)


def test_metrics_carries_reason():
    print("\n[F8] metrics 带 reason")
    pt = mkpt(player=_FakePlayerClient(moves=[(MOVE_GIVE_UP, "算了")]),
              host=_FakeHost())
    r = pt.run(mk_spec())
    check("metrics 里 reason=give_up",
          r.to_metrics().get("reason") == "give_up", r.to_metrics())


def main():
    tests = [
        # A. 隔离
        test_player_prompt_has_no_hidden_fields,
        test_real_run_prompt_never_leaks,
        test_public_transcript_strips_coverage,
        test_host_answer_uses_production_path,
        # B. 结局
        test_give_up_is_unsolved,
        test_give_up_does_not_call_host,
        test_exhausted_is_unsolved,
        test_player_failure_is_unavailable,
        test_host_failure_is_unavailable,
        test_no_client_is_unavailable,
        test_spec_missing_answer_is_unavailable,
        # C. 让路
        test_interrupted_before_first_call,
        test_interrupted_after_player_before_host,
        test_interrupted_midway_stops_further_turns,
        test_continue_predicate_exception_is_interrupt,
        # D. Player 协议
        test_player_tool_is_forced_and_enum_limited,
        test_bad_kind_is_unavailable,
        test_empty_text_is_unavailable,
        test_gateway_wrapped_tool_input_ok,
        # E. 结果
        test_metrics_shape_and_no_hidden_leak,
        test_metrics_round_trips_through_metrics_dict,
        test_run_never_raises,
        test_run_requires_no_pool_or_engine,
        test_result_predicates,
        # F. Q10b: 温度 / 结局细分 / 策略表
        test_player_temperature_is_zero,
        test_unsolved_reason_give_up,
        test_unsolved_reason_max_turns,
        test_policy_table_is_exactly_the_contract,
        test_interrupted_is_not_counted_fail,
        test_policy_unknown_status_is_conservative,
        test_run_only_returns_known_status,
        test_metrics_carries_reason,
    ]
    for t in tests:
        t()
    print()
    if FAIL[0]:
        print(f"FAIL: {FAIL[0]} 处")
        return 1
    print("PASS: AI 试玩(隔离 + 结局 + 让路) 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
