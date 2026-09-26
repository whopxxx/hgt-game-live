#!/usr/bin/env python
# coding: utf-8
"""运行: uv run tests/test_interaction.py（完全离线）。

Issue #60 §21: REVEALED 互动(评分 #1~#5 / 下一题主题投票 #a~#e)
Engine 侧行为契约:

    * #1..#5 评分只在 REVEALED 前 30s 有效, 一人一份可改, 最后一次生效;
    * #a..#e 主题票整个 60s 有效, 一人一票可改;
    * `#A`..`#E` 经 casefold 归一后等价;
    * 精确 token, 不做子串匹配(#abc 不是 #a);
    * token 绝不进普通 QA 路径(不送 Answer LLM);
    * 60s freeze 恰好一次 -> next_category(tie 确定性 / 无票为空);
    * freeze 后 closeout 动作恰好一次;
    * SETTING 后 vote ledger 清空, 但 requested_category 不丢;
    * Snapshot.reveal_interaction 是窗口权威。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from story.config import Config  # noqa: E402
from story.engine import RoundEngine  # noqa: E402
from story.state import ActionKind, Phase  # noqa: E402

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
    kw.setdefault("sim_path", "x")
    kw.setdefault("reveal_hold_seconds", 60.0)
    kw.setdefault("rating_window_seconds", 30.0)
    return Config(**kw)


def boot_revealed(**kw):
    """开一局, 直接把题交掉并揭晓 -> REVEALED 窗口(0s 起)。"""
    from story.state import QAResult
    clk = FakeClock()
    eng = RoundEngine(mkcfg(**kw), clock=clk)
    eng.start()
    eng.submit_riddle("谜面", "谜底", title="T")
    # QA -> REVEALING(猜中) -> REVEALED(交付谜底)
    eng.submit_danmaku("u0", "丁", "#答案")
    eng.tick()
    eng.submit_qa([QAResult(qid=1, verdict="揭晓")])
    eng.submit_reveal("汤底")
    assert eng.phase == Phase.REVEALED, eng.phase
    return eng, clk


def kinds(acts):
    return [a.kind for a in acts]


def test_rating_window_and_change():
    print("\n[R1] 评分窗口(0~30s) + 改分最后一次生效")
    eng, clk = boot_revealed()
    acts = eng.submit_danmaku("u1", "甲", "#3")
    check("窗口内 #3 收下并广播", kinds(acts) == [ActionKind.BROADCAST], kinds(acts))
    eng.submit_danmaku("u1", "甲", "#5")
    st = eng._rating_ledger.stats()
    check("同一人改分 -> 只一份", st["count"] == 1, st)
    check("最后一次生效(5)", st["distribution"]["5"] == 1, st)
    clk.advance(29)
    eng.submit_danmaku("u2", "乙", "#2")
    check("29s 仍可评", eng._rating_ledger.stats()["count"] == 2,
          eng._rating_ledger.stats())
    clk.advance(2)      # 31s
    eng.submit_danmaku("u3", "丙", "#4")
    st2 = eng._rating_ledger.stats()
    check("**30s 边界后不再变化**", st2["count"] == 2, st2)


def test_theme_vote_window_and_change():
    print("\n[R2] 主题票窗口(0~60s) + 改票迁移")
    eng, clk = boot_revealed()
    eng.submit_danmaku("u1", "甲", "#c")
    check("#c 收下", eng._theme_ledger.count() == 1, eng._theme_ledger.totals())
    eng.submit_danmaku("u1", "甲", "#a")
    totals = eng._theme_ledger.totals()
    check("改票: 权重从旧类完整迁走(horror 0 / logic 1)",
          totals["horror"] == 0 and totals["logic"] == 1, totals)
    eng.submit_danmaku("u2", "乙", "#A")     # 大写归一
    check("大写 #A 等价 logic", eng._theme_ledger.totals()["logic"] == 2,
          eng._theme_ledger.totals())
    clk.advance(55)
    eng.submit_danmaku("u3", "丙", "#e")
    check("55s(60s 窗口内)仍可投", eng._theme_ledger.count() == 3,
          eng._theme_ledger.count())
    clk.advance(6)      # 61s -> freeze 在下一次 tick
    eng.tick()
    check("freeze 后不再变化", eng._theme_ledger.count() == 0, "已清空")
    check("next_category = logic(2 票最高)",
          eng._next_category == "logic", eng._next_category)


def test_exact_token_not_substring():
    print("\n[R3] 精确 token; #abc 不算 #a; 普通 #问题不受影响")
    eng, clk = boot_revealed()
    before = eng._interaction_tokens_consumed
    eng.submit_danmaku("u1", "甲", "#abc")
    check("#abc 未被当 #a 消费",
          eng._interaction_tokens_consumed == before,
          eng._interaction_tokens_consumed)
    check("#abc 掉进普通 QA(或被 phase ACK, 但绝不是互动 token)",
          eng._interaction_tokens_consumed == before, None)


def test_reserved_tokens_never_enter_qa():
    print("\n[R4] 互动 token 绝不进 Answer LLM 路径(QA 阶段发也不行)")
    eng, clk = boot_revealed()
    # 1) REVEALED 窗口外(QA 阶段)发 #1
    eng.submit_riddle("新谜面", "新谜底", title="T2")   # -> QA
    acts = eng.submit_danmaku("u1", "甲", "#1")
    check("QA 阶段 #1 被消费(不进 QA 队列)",
          eng._probe()["pending"] == 0, eng._probe())
    check("消费产生广播(确定性反馈)", all(
        a.kind in (ActionKind.BROADCAST,) for a in acts), kinds(acts))
    # 2) 子串边界: `#12` 不是 `#1`
    eng2, clk2 = boot_revealed()
    n0 = eng2._interaction_tokens_consumed
    eng2.submit_danmaku("u9", "丁", "#12")
    check("#12 不算互动 token",
          eng2._interaction_tokens_consumed == n0, None)


def test_freeze_tie_and_novote():
    print("\n[R5] freeze: tie 确定性 / 无票为空")
    # tie: suspense 与 horror 各 1 票 -> 固定顺序 logic>suspense>horror
    # -> suspense 胜(枚举序在前)。
    eng, clk = boot_revealed()
    eng.submit_danmaku("u1", "甲", "#b")
    eng.submit_danmaku("u2", "乙", "#c")
    eng.submit_danmaku("u3", "丙", "#b")
    eng.submit_danmaku("u4", "丁", "#c")
    # 无票用例分开; 这里 tie: logic 2 vs horror 2, 其余 0
    eng, clk = boot_revealed()
    eng.submit_danmaku("u1", "甲", "#a")
    eng.submit_danmaku("u2", "乙", "#c")
    eng.submit_danmaku("u3", "丙", "#a")
    eng.submit_danmaku("u4", "丁", "#c")
    clk.advance(61)
    acts = eng.tick()
    check("tie -> 固定枚举序取先(logic)",
          eng._next_category == "logic", eng._next_category)
    co = [a for a in acts if a.kind == ActionKind.ROUND_CLOSEOUT]
    check("closeout 恰好一次", len(co) == 1, len(co))
    check("closeout 带 tie 标记", co and co[0].payload.get("tie") is True,
          co and co[0].payload)

    # 无票
    eng2, clk2 = boot_revealed()
    clk2.advance(61)
    acts2 = eng2.tick()
    co2 = [a for a in acts2 if a.kind == ActionKind.ROUND_CLOSEOUT]
    check("无票 -> selected_category 为空",
          co2 and co2[0].payload.get("selected_category") == "",
          co2 and co2[0].payload)
    check("无票 -> no_vote=True", co2 and co2[0].payload.get("no_vote") is True)


def test_freeze_exactly_once():
    print("\n[R6] freeze 只发生一次(多拍不重复 closeout)")
    eng, clk = boot_revealed()
    eng.submit_danmaku("u1", "甲", "#d")
    clk.advance(61)
    a1 = eng.tick()
    a2 = eng.tick()
    n1 = sum(1 for a in a1 if a.kind == ActionKind.ROUND_CLOSEOUT)
    n2 = sum(1 for a in a2 if a.kind == ActionKind.ROUND_CLOSEOUT)
    check("第一拍恰好 1 次", n1 == 1, n1)
    check("第二拍 0 次", n2 == 0, n2)


def test_requested_category_survives_setting():
    print("\n[R7] SETTING 后 ledger 清空, requested_category 不丢")
    eng, clk = boot_revealed()
    eng.submit_danmaku("u1", "甲", "#b")      # suspense
    clk.advance(61)
    acts = eng.tick()                          # freeze + SETTING
    check("next_category=suspense", eng._next_category == "suspense",
          eng._next_category)
    riddle_acts = [a for a in acts if a.kind == ActionKind.RIDDLE]
    check("**首轮 RIDDLE 带 requested_category**",
          riddle_acts and riddle_acts[0].payload.get("requested_category")
          == "suspense",
          riddle_acts and riddle_acts[0].payload.get("requested_category"))
    check("vote ledger 已清空(下一轮重新收集)",
          eng._theme_ledger.count() == 0)
    # retry 也带
    r = eng.submit_riddle(None, error="失败")   # -> retry RIDDLE 动作
    retry = [a for a in r if a.kind == ActionKind.RIDDLE]
    check("**retry RIDDLE 同样带 requested_category**",
          retry and retry[0].payload.get("requested_category") == "suspense",
          retry and retry[0].payload.get("requested_category"))
    # deferred(request_riddle_action)也带
    d = eng.request_riddle_action("riddle_deferred")
    check("**deferred RIDDLE 带 requested_category**",
          d.payload.get("requested_category") == "suspense",
          d.payload.get("requested_category"))


def test_snapshot_reveal_interaction():
    print("\n[R8] Snapshot.reveal_interaction 是窗口权威")
    eng, clk = boot_revealed()
    s = eng.snapshot().to_json()
    ri = s["reveal_interaction"]
    check("rating_open=True(0s)", ri["rating_open"] is True, ri)
    check("theme_vote_open=True", ri["theme_vote_open"] is True, ri)
    eng.submit_danmaku("u1", "甲", "#4")
    eng.submit_danmaku("u2", "乙", "#a")
    s = eng.snapshot().to_json()
    ri = s["reveal_interaction"]
    check("评分聚合可见", ri["rating_distribution"]["4"] == 1,
          ri["rating_distribution"])
    check("主题票数可见(#a logic=1)",
          ri["theme_options"][0]["votes"] == 1, ri["theme_options"][0])
    check("frozen=False", ri["frozen"] is False, ri)
    clk.advance(31)
    s = eng.snapshot().to_json()
    ri = s["reveal_interaction"]
    check("31s: rating_open=False(服务端判, 前端不自推)",
          ri["rating_open"] is False, ri["rating_open"])
    check("31s: theme 仍开", ri["theme_vote_open"] is True, ri)
    clk.advance(30)
    eng.tick()
    s = eng.snapshot().to_json()
    ri = s["reveal_interaction"]
    check("freeze 后 selected_category 下发",
          ri["selected_category"] == "logic"
          and ri["frozen"] is True, ri)
    # QA 阶段(换题后)互动层隐藏
    eng.submit_riddle("谜面3", "谜底3", title="T3")
    s = eng.snapshot().to_json()
    check("非 REVEALED 时 rating_open=False",
          s["reveal_interaction"]["rating_open"] is False)


def test_closeout_payload_shape():
    print("\n[R9] closeout payload 完整(§6)")
    eng, clk = boot_revealed()
    eng.submit_danmaku("u1", "甲", "#3")
    eng.submit_danmaku("u2", "乙", "#3")
    eng.submit_danmaku("u3", "丙", "#a")
    eng.submit_danmaku("u4", "丁", "#b")
    clk.advance(61)
    acts = eng.tick()
    co = [a for a in acts if a.kind == ActionKind.ROUND_CLOSEOUT][0]
    p = co.payload
    check("rating 聚合", p["rating_count"] == 2 and p["rating_sum"] == 6
          and p["rating_average"] == 3.0, p)
    check("distribution 完整五档",
          set(p["rating_distribution"]) == {"1", "2", "3", "4", "5"}, p)
    check("theme 聚合五类",
          set(p["theme_vote_totals"]) == {"logic", "suspense", "horror",
                                          "emotion", "brainstorm"}, p)
    check("theme_vote_count=2", p["theme_vote_count"] == 2, p)
    check("selected_category=logic(平票按枚举序)", p["selected_category"]
          == "logic", p)
    check("no_vote=False", p["no_vote"] is False, p)


def main() -> int:
    tests = [
        test_rating_window_and_change,
        test_theme_vote_window_and_change,
        test_exact_token_not_substring,
        test_reserved_tokens_never_enter_qa,
        test_freeze_tie_and_novote,
        test_freeze_exactly_once,
        test_requested_category_survives_setting,
        test_snapshot_reveal_interaction,
        test_closeout_payload_shape,
    ]
    for t in tests:
        t()
    print()
    if FAIL[0]:
        print(f"FAILED: {FAIL[0]} 项")
        return 1
    print("PASS: REVEALED 互动(评分/主题投票/freeze/closeout) 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
