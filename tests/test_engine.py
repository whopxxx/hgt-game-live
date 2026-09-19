"""运行: uv run tests/test_engine.py（完全离线, 无网络）。

海龟汤状态机的确定性单测。用 FakeClock 注入时间, 所以能精确控制
"空闲 45 秒"、"揭晓展示 30 秒" 这类行为, 不用真的等。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from story.config import Config  # noqa: E402
from story.engine import RoundEngine  # noqa: E402
from story.llm import RIDDLE_PROMPT_VERSION  # noqa: E402
from story.quality import QUALITY_POLICY_VERSION  # noqa: E402
from story.state import ActionKind, Phase, QARec, QAResult  # noqa: E402


class FakeClock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt
        return self.t


FAIL = [0]


def check(name, cond, extra=""):
    if cond:
        print(f"  ok  {name}")
        return
    print(f"  FAIL {name}  {extra}")
    FAIL[0] += 1


def mkcfg(**kw):
    # Q12 之后 `submit_danmaku` **默认就是同步的**(没有缓冲窗口了),
    # 不再需要 `replay_burst_n=0` 那个开关 —— 它随旧机制一起删掉了。
    kw.setdefault("no_llm", True)
    return Config(sim_path="x", **kw)


def boot(cfg):
    """建引擎 + start() + 交一个谜题, 停在 QA。返回 (eng, clk)。"""
    clk = FakeClock()
    eng = RoundEngine(cfg, clock=clk)
    acts = eng.start()
    assert eng.phase == Phase.SETTING, eng.phase
    assert any(a.kind == ActionKind.RIDDLE for a in acts), acts
    eng.submit_riddle("一个男人点了海龟汤，喝一口就自杀了。为什么？",
                      "同伴的肉汤骗局。", ["注意汤的味道"], title="海龟汤")
    assert eng.phase == Phase.QA, eng.phase
    return eng, clk



def say(eng, clk, uid, name, text, gap=20.0):
    """发一条弹幕 -> 推进时钟 -> tick, 返回那次 tick 的动作。

    注意 `submit_danmaku` **自己也可能同步返回动作**(如 `#提示`)——
    Q12 之后不再有缓冲, 所以想拿那些动作应直接读它的返回值。
    """
    eng.submit_danmaku(uid, name, text)
    clk.advance(gap)
    return eng.tick()


def say_many(eng, clk, items, gap=20.0):
    """连续发多条弹幕(每条之间推 gap), **不** tick。

    刻意不 tick: 这些测试要的是"消息已入队、但还没派发"的中间状态。
    Q12 之后 `submit_danmaku` 同步入队, 所以不再需要旧版的
    `_flush_burst()` 收尾 —— 但**仍然不能** tick, 否则 QA 阶段会立刻
    派发 ANSWER, 调用方就看不到 pending 队列了。
    """
    for uid, name, text in items:
        eng.submit_danmaku(uid, name, text)
        clk.advance(gap)


def kinds(acts):
    return [a.kind for a in acts]


# ======================================================================
def test_start_and_riddle():
    print("[start -> 出题 -> QA]")
    clk = FakeClock()
    eng = RoundEngine(mkcfg(), clock=clk)
    acts = eng.start()
    check("start 出 BROADCAST+RIDDLE",
          kinds(acts) == [ActionKind.BROADCAST, ActionKind.RIDDLE], kinds(acts))
    check("phase=SETTING", eng.phase == Phase.SETTING, eng.phase)
    check("重复 start 无动作", eng.start() == [], eng.start())
    acts = eng.submit_riddle("谜面在此", "谜底在此", title="T")
    check("submit_riddle -> QA", eng.phase == Phase.QA, eng.phase)
    s = eng.snapshot()
    check("谜面上屏", s.puzzle == "谜面在此", s.puzzle)
    check("谜底未揭晓", s.revealed_answer == "", s.revealed_answer)
    check("puzzle_index=1", s.puzzle_index == 1, s.puzzle_index)
    check("new_puzzle 广播", acts[0].payload.get("new_puzzle") is True, acts)


def test_question_routing():
    print("[提问路由]")
    eng, clk = boot(mkcfg())
    eng.submit_danmaku("u1", "甲", "#他是盲人吗")
    acts = eng.tick()
    check("tick 派发 1 条 ANSWER",
          len([a for a in acts if a.kind == ActionKind.ANSWER]) == 1, kinds(acts))
    s = eng.snapshot()
    # 已派发 -> 计入在途; pending 应为 0
    check("已派发计入在途", eng._probe()["inflight"] == 1, eng._probe())
    check("stat_questions=1", eng.snapshot().stat_questions == 1,
          eng.snapshot().stat_questions)
    eng.submit_danmaku("u2", "乙", "前排围观")
    s = eng.snapshot()
    # 注意: pending_count = 排队 + 在途, 所以这里是 1(那条在途的提问),
    # 不是 0。非指令没有**新增**排队项才是要断言的。
    check("非指令不进队列", eng._probe()["pending"] == 0, eng._probe())
    check("非指令仍上屏", len(s.danmaku) == 2, len(s.danmaku))


def test_concurrency_cap():
    print("[并发上限 5]")
    eng, clk = boot(mkcfg(qa_max_inflight=5))
    say_many(eng, clk, [(f"u{i}", f"观众{i}", f"#问题{i}") for i in range(20)])
    check("20 条全部入队", eng._probe()["pending"] == 20, eng._probe())
    acts = eng.tick()
    ans = [a for a in acts if a.kind == ActionKind.ANSWER]
    check("一次最多派发 5 条", len(ans) == 5, len(ans))
    check("剩余 15 排队", eng._probe()["pending"] == 15, eng._probe())
    check("在途 5", eng._probe()["inflight"] == 5, eng._probe())
    check("在途满时不派发",
          [a for a in eng.tick() if a.kind == ActionKind.ANSWER] == [])
    eng.submit_qa([QAResult(qid=1, verdict="是")])
    acts = eng.tick()
    ans = [a for a in acts if a.kind == ActionKind.ANSWER]
    check("答完继续流动", len(ans) == 1, len(ans))
    check("在途仍 5", eng._probe()["inflight"] == 5, eng._probe())


def test_answer_flow():
    print("[回答落屏]")
    eng, clk = boot(mkcfg())
    eng.submit_danmaku("u1", "甲", "#他是盲人吗")
    eng.tick()
    acts = eng.submit_qa([QAResult(qid=1, verdict="不是", comment="方向不对")])
    s = eng.snapshot()
    check("问答入日志", len(s.qa_log) == 1 and s.qa_log[0]["verdict"] == "不是", s.qa_log)
    check("点评保留", s.qa_log[0]["comment"] == "方向不对", s.qa_log)
    check("pending 清零", s.pending_count == 0, s.pending_count)
    check("stat_answered=1", s.stat_answered == 1, s.stat_answered)
    check("有 BROADCAST", any(a.kind == ActionKind.BROADCAST for a in acts), kinds(acts))


def test_ordering_and_missing():
    print("[乱序与漏项]")
    eng, clk = boot(mkcfg())
    for i in range(3):
        eng.submit_danmaku(f"u{i}", f"甲{i}", f"#问题{i}")
    eng.tick()
    eng.submit_qa([QAResult(qid=3, verdict="是"), QAResult(qid=1, verdict="是")])
    s = eng.snapshot()
    got = [(r["qid"], r["verdict"]) for r in s.qa_log]
    check("按提问顺序落屏", got == [(1, "是"), (3, "是")], got)
    # 逐条秒回下, 未答的 qid=2 仍在途(由超时逻辑回收, 不在这里退回)
    check("未答仍在途", eng._probe()["inflight"] == 1, eng._probe())
    eng.submit_qa([QAResult(qid=2, verdict="不是")])
    check("补答后清零", eng._probe()["pending"] == 0
          and eng._probe()["inflight"] == 0, eng._probe())


def test_inflight_timeout():
    """超时 -> 直接判"未判定", **不重派**(Hotfix B)。

    旧契约是"超时退回队列重试", 但 urllib 请求无法取消, 重派会叠加
    并发 worker(真实故障: 1 个问题 -> 3 个 worker -> 12 个 HTTP)。
    新契约是 fail-fast。
    """
    print("[在途超时 -> 未判定(不重派)]")
    eng, clk = boot(mkcfg(qa_inflight_timeout=10.0))
    eng.submit_danmaku("u1", "甲", "#问题")
    eng.tick()
    check("在途 1", eng._probe()["inflight"] == 1, eng._probe())
    clk.advance(11)
    acts = eng.tick()
    p = eng._probe()
    check("**超时后不回队列**", p["pending"] == 0, p)
    check("**在途清空**(不再留一个假在途)", p["inflight"] == 0, p)
    s = eng.snapshot()
    # 兜底裁决必须是**未判定**, 不是"无关"。
    # "无关"是断言"你的猜测与谜底无关", 而这里其实是系统没答上 ——
    # 说成"无关"会把观众的思路带偏。
    check("给'未判定'(不是'无关')",
          any(r["verdict"] == "未判定" for r in s.qa_log), s.qa_log)
    check("兜底不含'无关'",
          not any(r["verdict"] == "无关" for r in s.qa_log), s.qa_log)
    # '未判定' 不该进 LLM transcript —— 否则模型会以为它是一种合法裁决
    check("'未判定'不进 transcript", eng._probe()["history"] == 0,
          eng._probe()["history"])
    # 关键: 超时后不得再派发任何 ANSWER(否则就是并发 worker 的来源)
    ans = [a for a in acts if a.kind.value == "answer"]
    check("**超时那一 tick 不派发 ANSWER**", not ans, [a.payload for a in ans])
    for _ in range(4):
        clk.advance(11)
        acts = eng.tick()
        ans = [a for a in acts if a.kind.value == "answer"]
        check("后续 tick 也不再派发", not ans, [a.payload for a in ans])


def test_no_duplicate_worker_per_qid():
    """同一 qid 在任何时刻**最多一个** worker 派发(Hotfix B 的核心)。

    现有 `test_inflight_timeout` 的盲区: 它用 `clk.advance()` 快进, 但**没有
    worker 在飞** —— 所以看不见"旧 worker 还活着又派了一个新的"。

    ⚠️ 这里是**模拟** worker 仍在途(不调 `submit_qa()`, 表示结果还没回来),
    **不是**真的起了 Director 的线程池 —— 早先的注释写成"用真实 pool /
    worker 真的在途", 与代码不符, 已改正。判别力不受影响: 重复 worker 的
    唯一入口就是引擎为同一 qid **再发一个 ANSWER**, 本测试正是钉住这一点。
    """
    print("[同一 qid 不并发两个 worker]")
    from story.engine import ActionKind
    eng, clk = boot(mkcfg(qa_inflight_timeout=10.0))
    eng.submit_danmaku("u1", "甲", "#问题")
    first = [a for a in eng.tick() if a.kind == ActionKind.ANSWER]
    check("首次派发 1 个 ANSWER", len(first) == 1, len(first))
    qid = first[0].payload["qid"]
    # 模拟"worker 卡住, 结果迟迟不回来" —— 推进到超时之后
    clk.advance(11)
    acts = eng.tick()
    more = [a for a in acts if a.kind == ActionKind.ANSWER]
    check("**超时后不再派发第二个 ANSWER**", not more, [a.payload for a in more])
    # 迟到结果必须被安全忽略(不是崩, 也不是写两条记录)
    before = len(eng.snapshot().qa_log)
    eng.submit_qa([QAResult(qid=qid, verdict="是")])
    check("迟到结果被忽略, 不重复记录",
          len(eng.snapshot().qa_log) == before, len(eng.snapshot().qa_log))


def test_answer_payload_carries_qa_budget():
    """ANSWER payload 要带上 QA 自己的短预算(Hotfix B)。

    预算放在 payload 里跟着动作走, director 直接透传给 writer.answer()。
    没有它, QA 就会吃全局 AI_TIMEOUT=60 / 重试 3 次。
    """
    print("[ANSWER 带 QA 时延预算]")
    eng, clk = boot(mkcfg(qa_answer_timeout=8.0, qa_answer_retries=0))
    eng.submit_danmaku("u1", "甲", "#问题")
    ans = [a for a in eng.tick() if a.kind.value == "answer"]
    check("派发了 ANSWER", len(ans) == 1, len(ans))
    p = ans[0].payload
    check("**payload 带 timeout**", p.get("timeout") == 8.0, p.get("timeout"))
    check("**payload 带 max_retries**", p.get("max_retries") == 0,
          p.get("max_retries"))


def test_dedupe_and_cap():
    print("[去重与队列封顶]")
    eng, clk = boot(mkcfg(pending_cap=5))
    eng.submit_danmaku("u1", "甲", "#他是盲人吗")
    eng.submit_danmaku("u1", "甲", "#他是盲人吗！！")
    check("同人去重", eng._probe()["pending"] == 1, eng._probe())
    eng.submit_danmaku("u2", "乙", "#他是盲人吗")
    check("他人同问不去重", eng._probe()["pending"] == 2, eng._probe())
    clk.advance(601)          # 超过去重窗口(600s)
    eng.submit_danmaku("u1", "甲", "#他是盲人吗")
    check("过窗口可再问", eng._probe()["pending"] == 3, eng._probe())
    for i in range(10):
        eng.submit_danmaku(f"z{i}", f"z{i}", f"#独特问题{i}")
    check("队列封顶", eng._probe()["pending"] == 5, eng._probe())
    # 入队 3 + 10 = 13, 上限 5 -> 丢弃 8; 但其中 z0 与已有问题不同名,
    # 所以实际丢弃数看 pending_cap 的挤出量
    check("记录丢弃数", eng._probe()["dropped"] >= 5, eng._probe())


def test_solve_and_reveal():
    print("[猜中 -> 揭晓]")
    eng, clk = boot(mkcfg(reveal_hold_seconds=30.0))
    eng.submit_danmaku("u1", "甲", "#同伴把自己的肉给他吃了对吗")
    eng.tick()
    acts = eng.submit_qa([QAResult(qid=1, verdict="揭晓")])
    check("揭晓动作", any(a.kind == ActionKind.REVEAL for a in acts), kinds(acts))
    check("phase=REVEALING", eng.phase == Phase.REVEALING, eng.phase)
    s = eng.snapshot()
    check("solved=True", s.solved is True, s)
    check("solved_by 正确", s.solved_by == "甲", s.solved_by)
    acts = eng.submit_reveal("谜底是：同伴用自己的肉骗他是海龟汤。")
    check("phase=REVEALED", eng.phase == Phase.REVEALED, eng.phase)
    s = eng.snapshot()
    check("谜底上屏", "同伴" in s.revealed_answer, s.revealed_answer)
    check("下一题倒计时",
          s.next_puzzle_ms is not None and s.next_puzzle_ms > 0, s.next_puzzle_ms)


def test_u2_reveal_stage_three_phase():
    """**U2**: 揭晓分三阶段下发(core / explanation / contribution)。

    60 秒分三段, 是为了让下半屏**同一时刻只有一组长内容**:
        0..focus       只有核心答案
        focus..detail  核心答案 + 完整解释(共同解谜隐藏)
        detail..hold   核心答案 + 共同解谜(完整解释隐藏)

    实播故障: 三块同时上屏 -> 互相争空间 -> fitReveal 只能一路缩字号
    -> 完整解释被压成一条矮滚动框, 而观众没有鼠标去滚直播源。

    ⚠️ 阶段由**服务端**算(前端不自己计时): 刷新/重连后本地计时会从 0
    重来, 与服务端不一致; 而且边界是配置, 前端硬编码一份副本迟早漂移。
    """
    print("\n[U2-Engine] 揭晓快照三阶段")
    eng, clk = boot(mkcfg(reveal_hold_seconds=60.0,
                          reveal_core_focus_seconds=15.0,
                          reveal_detail_seconds=45.0))
    # ---- QA / REVEALING: 阶段为空(不提前泄 hidden truth) ----
    check("QA: stage 为空", eng.snapshot().reveal_stage == "",
          eng.snapshot().reveal_stage)
    eng.submit_danmaku("u1", "甲", "#是同伴的肉")
    eng.tick()
    eng.submit_qa([QAResult(qid=1, verdict="揭晓")])
    check("REVEALING: 仍不提前给 stage",
          eng.snapshot().reveal_stage == "", eng.snapshot().reveal_stage)

    # ---- REVEALED: 逐秒核对三段边界 ----
    eng.submit_reveal("完整解释在此。")
    # 用 deadline 反推: 进入 REVEALED 的时刻 = deadline - hold
    base = eng._next_puzzle_deadline - 60.0
    cases = [
        (0.0, "core"),
        (14.9, "core"),
        (15.0, "explanation"),      # 边界**含** focus
        (44.9, "explanation"),
        (45.0, "contribution"),     # 边界**含** detail
        (59.0, "contribution"),
    ]
    for dt, want in cases:
        s = eng.snapshot(now=base + dt)
        check(f"{dt:4.1f}s -> {want}", s.reveal_stage == want,
              (dt, s.reveal_stage, want))
    # detail_visible 与 stage 必须自洽(前端两者都用)
    s = eng.snapshot(now=base + 5.0)
    check("core 阶段 detail_visible=False", s.reveal_detail_visible is False)
    s = eng.snapshot(now=base + 30.0)
    check("explanation 阶段 detail_visible=True",
          s.reveal_detail_visible is True)
    s = eng.snapshot(now=base + 50.0)
    check("contribution 阶段 detail_visible 仍为 True(它只表示'过了 focus')",
          s.reveal_detail_visible is True)


def test_u2_stage_boundaries_come_from_config_not_literals():
    """**U2**: 阶段边界必须**来自配置**, 不是写死的 15/45。

    把 hold/core_focus/detail 都换一组值, 阶段必须跟着变 —— 否则就是
    代码里抄了一份常量, 改了配置却不生效。
    """
    print("\n[U2-Engine] 阶段边界来自配置")
    eng, clk = boot(mkcfg(reveal_hold_seconds=20.0,
                          reveal_core_focus_seconds=4.0,
                          reveal_detail_seconds=12.0))
    eng.submit_danmaku("u1", "甲", "#是同伴的肉")
    eng.tick()
    eng.submit_qa([QAResult(qid=1, verdict="揭晓")])
    eng.submit_reveal("解释。")
    base = eng._next_puzzle_deadline - 20.0
    for dt, want in ((0.0, "core"), (4.0, "explanation"),
                     (11.9, "explanation"), (12.0, "contribution")):
        s = eng.snapshot(now=base + dt)
        check(f"自定义边界 {dt:4.1f}s -> {want}", s.reveal_stage == want,
              (dt, s.reveal_stage, want))


def test_u2_detail_boundary_validation():
    """**U2**: `reveal_detail_seconds` 必须落在 [focus, hold] 内。

    越界会让某一段永远不出现(前端拿到自相矛盾的 stage)。
    注意两端**允许**相等: `== hold` = 解释一直显示到下一题;
    `== focus` = 跳过中间段。倒挂与越界必须报错。
    """
    print("\n[U2-Config] reveal_detail_seconds 边界校验")
    check("正常值 -> 无告警",
          not mkcfg(reveal_hold_seconds=60.0, reveal_core_focus_seconds=15.0,
                    reveal_detail_seconds=45.0).validate())
    check("== hold -> 允许",
          not mkcfg(reveal_hold_seconds=60.0, reveal_core_focus_seconds=15.0,
                    reveal_detail_seconds=60.0).validate())
    check("== focus -> 允许",
          not mkcfg(reveal_hold_seconds=60.0, reveal_core_focus_seconds=15.0,
                    reveal_detail_seconds=15.0).validate())
    bad = mkcfg(reveal_hold_seconds=60.0, reveal_core_focus_seconds=15.0,
                reveal_detail_seconds=10.0).validate()
    check("**早于 focus -> 报错**", bool(bad), bad)
    bad2 = mkcfg(reveal_hold_seconds=40.0, reveal_core_focus_seconds=15.0,
                 reveal_detail_seconds=50.0).validate()
    check("**晚于 hold -> 报错**", bool(bad2), bad2)


def test_u1_reveal_snapshot_two_phase():
    """**U1**: 揭晓正文分两段下发, 且**只在 REVEALED**。

        QA / REVEALING   三个字段都为空/false(不提前泄 hidden truth)
        REVEALED 0..focus  core 有值, full 有值, detail_visible=false
        REVEALED > focus   detail_visible=true
    """
    print("\n[U1-Engine] 揭晓快照两段式")
    eng, clk = boot(mkcfg(reveal_hold_seconds=60.0,
                          reveal_core_focus_seconds=15.0))
    # ---- QA: 三个字段都空 ----
    s = eng.snapshot()
    check("QA: core 为空", s.revealed_core_answer == "", s.revealed_core_answer)
    check("QA: full 为空", s.revealed_full_answer == "", s.revealed_full_answer)
    check("QA: detail_visible=False", s.reveal_detail_visible is False)
    # ---- REVEALING: 仍不下发 ----
    eng.submit_danmaku("u1", "甲", "#是同伴的肉")
    eng.tick()
    eng.submit_qa([QAResult(qid=1, verdict="揭晓")])
    check("确实在 REVEALING", eng.phase == Phase.REVEALING, eng.phase)
    s = eng.snapshot()
    check("**REVEALING 不提前下发 core**", s.revealed_core_answer == "",
          s.revealed_core_answer)
    check("**REVEALING 不提前下发 full**", s.revealed_full_answer == "",
          s.revealed_full_answer)
    # ---- REVEALED, 未到 focus: core+full 有值, detail=false ----
    eng.submit_reveal("完整解释在此。")
    s = eng.snapshot()
    check("REVEALED: full 有值", s.revealed_full_answer == "完整解释在此。",
          s.revealed_full_answer)
    check("REVEALED 刚开始: detail_visible=False",
          s.reveal_detail_visible is False, s.reveal_detail_visible)
    # ---- 推进到 focus 之后 ----
    clk.advance(20.0)
    s = eng.snapshot()
    check("**过了 focus -> detail_visible=True**",
          s.reveal_detail_visible is True, s.reveal_detail_visible)


def test_u1_reveal_core_falls_back_when_absent():
    """**U1**: legacy 题没有 core_answer -> 前端靠 full fallback。

    Engine 侧只需保证: 没有 spec 时 `revealed_core_answer` 是空串
    (而不是 None 或上一题的残留)。
    """
    print("\n[U1-Engine] 无 core_answer 时字段为空串")
    eng, clk = boot(mkcfg(reveal_hold_seconds=60.0))
    eng.submit_danmaku("u1", "甲", "#猜中了")
    eng.tick()
    eng.submit_qa([QAResult(qid=1, verdict="揭晓")])
    eng.submit_reveal("只有完整谜底。")
    s = eng.snapshot()
    check("core 是空串(不是 None)", s.revealed_core_answer == "",
          repr(s.revealed_core_answer))
    check("full 正常", s.revealed_full_answer == "只有完整谜底。",
          s.revealed_full_answer)
    check("snapshot.to_json 也带上这三个字段",
          "revealed_core_answer" in s.to_json()
          and "revealed_full_answer" in s.to_json()
          and "reveal_detail_visible" in s.to_json())


def test_u1_pressure_exposes_reveal_remaining():
    """**U1**: pressure() 暴露 reveal_remaining_seconds(只读)。"""
    print("\n[U1-Engine] pressure 暴露剩余秒数")
    eng, clk = boot(mkcfg(reveal_hold_seconds=60.0))
    check("QA 时是 None",
          eng.pressure().get("reveal_remaining_seconds") is None,
          eng.pressure().get("reveal_remaining_seconds"))
    eng.submit_danmaku("u1", "甲", "#猜中了")
    eng.tick()
    eng.submit_qa([QAResult(qid=1, verdict="揭晓")])
    eng.submit_reveal("谜底。")
    left = eng.pressure().get("reveal_remaining_seconds")
    check("REVEALED 时有值且接近 60", left is not None and 55 <= left <= 60,
          left)
    clk.advance(30.0)
    left2 = eng.pressure().get("reveal_remaining_seconds")
    check("过了 30s 后约剩 30", left2 is not None and 25 <= left2 <= 31, left2)


def test_reveal_once():
    print("[一题只揭晓一次]")
    eng, clk = boot(mkcfg(max_reveals_per_puzzle=1))
    eng.submit_danmaku("u1", "甲", "#猜中了")
    eng.tick()
    eng.submit_qa([QAResult(qid=1, verdict="揭晓")])
    check("进入揭晓", eng.phase == Phase.REVEALING, eng.phase)
    n = eng._probe()["reveals"]
    eng.submit_danmaku("u2", "乙", "#我也猜到了")
    eng.submit_qa([QAResult(qid=99, verdict="揭晓")])
    check("不产生第二次揭晓", eng._probe()["reveals"] == n, eng._probe())


def test_next_puzzle_cycle():
    print("[揭晓->下一题]")
    eng, clk = boot(mkcfg(reveal_hold_seconds=30.0))
    eng.submit_danmaku("u1", "甲", "#猜中")
    eng.tick()
    eng.submit_qa([QAResult(qid=1, verdict="揭晓")])
    eng.submit_reveal("完整谜底")
    clk.advance(29)
    check("不到 30s 不换题", eng.tick() == [], eng.tick())
    clk.advance(2)
    acts = eng.tick()
    check("过 30s 开新题", eng.phase == Phase.SETTING, eng.phase)
    check("BROADCAST 先于 RIDDLE",
          kinds(acts) == [ActionKind.BROADCAST, ActionKind.RIDDLE], kinds(acts))
    check("new_puzzle 标记", acts[0].payload.get("new_puzzle") is True, acts[0].payload)
    eng.submit_riddle("新谜面", "新谜底", title="T2")
    s = eng.snapshot()
    check("puzzle_index=2", s.puzzle_index == 2, s.puzzle_index)
    check("问答日志已清空", s.qa_log == [], s.qa_log)
    check("谜底已清空", s.revealed_answer == "", s.revealed_answer)
    check("solved 重置", s.solved is False, s.solved)


def test_timeline_hints_and_reveal():
    print("[时间轴: 每 N 秒一条提示, 再等 N 秒揭晓]")
    # N=100s, max_hints=3 -> 100/200/300s 各一条提示, 400s 揭晓
    N = 100.0
    eng, clk = boot(mkcfg(hint_seconds=N, max_hints=3, restate_seconds=99999))
    got = []
    for _ in range(40):
        clk.advance(10)
        for a in eng.tick():
            if a.kind == ActionKind.HINT:
                got.append((round(clk.t), a.payload["level"]))
                eng.submit_hint(f"提示{a.payload['level']}")
        if eng.phase != Phase.QA:
            break
    check("三条提示的时机", got == [(100, 1), (200, 2), (300, 3)], got)
    check("第 3 条后再等 N 秒才揭晓",
          eng.phase == Phase.REVEALING and 400 <= clk.t < 420, (eng.phase, clk.t))


def test_timeline_survives_busy_chat():
    print("[时间轴: 人多也照走]")
    # 关键回归: 如果时间轴要求"队列为空"才推进, 那人多时提问永远问不完,
    # 提示与揭晓就永远不来。这里持续刷弹幕, 时间轴仍必须走完。
    N = 60.0
    eng, clk = boot(mkcfg(hint_seconds=N, max_hints=3, restate_seconds=99999))
    hints = 0
    q = 0
    done = False
    for _ in range(200):
        clk.advance(5)
        q += 1
        eng.submit_danmaku(f"u{q % 3}", "甲", f"#问题{q}")
        acts = eng.tick()                       # 可能带 HINT
        eng.submit_qa([QAResult(qid=q, verdict="不是")])
        acts += eng.tick()                      # 也可能带 HINT
        # 一次 tick 里 HINT 可能和 ANSWER 一起返回, 所以两批都要扫
        for a in acts:
            if a.kind == ActionKind.HINT:
                hints += 1
                eng.submit_hint(f"提示{hints}")
            if a.kind == ActionKind.REVEAL:
                done = True
        if eng.phase != Phase.QA:
            break
    check("扫描到揭晓", done or eng.phase == Phase.REVEALING, eng.phase)
    check("人多也照给 3 条提示", hints == 3, hints)
    check("到点仍然揭晓", eng.phase == Phase.REVEALING,
          (eng.phase, clk.t))
    check("揭晓约在 4N 时刻", 4 * N <= clk.t <= 4 * N + 15, clk.t)


def test_timeline_countdown_fields():
    print("[时间轴: 倒计时字段]")
    N = 100.0
    eng, clk = boot(mkcfg(hint_seconds=N, max_hints=3, restate_seconds=99999))
    s = eng.snapshot()
    check("总格数 = max_hints+1", s.timeline_total == 4, s.timeline_total)
    check("起始在第 0 格", s.timeline_slot == 0, s.timeline_slot)
    check("倒计时指向第 1 条提示",
          s.next_event_kind == "hint" and "第 1 条" in s.next_event_label,
          (s.next_event_kind, s.next_event_label))
    check("剩余约 100s", 95 <= s.next_event_ms / 1000 <= 100,
          s.next_event_ms)
    # 走到第 3 格后, 倒计时应指向"揭晓"
    clk.advance(N * 3 + 1)
    for a in eng.tick():
        if a.kind == ActionKind.HINT:
            eng.submit_hint("h")
    s2 = eng.snapshot()
    check("最后一格指向揭晓",
          s2.next_event_kind == "reveal" and "揭晓" in s2.next_event_label,
          (s2.next_event_kind, s2.next_event_label))


def test_no_question_cap():
    print("[提问条数不设上限]")
    eng, clk = boot(mkcfg(hint_seconds=99999, max_hints=3, restate_seconds=99999))
    for i in range(80):
        eng.submit_danmaku(f"u{i}", f"甲{i}", f"#问题{i}")
        eng.tick()
        eng.submit_qa([QAResult(qid=i + 1, verdict="不是")])
    check("80 条提问未触发揭晓", eng.phase == Phase.QA, eng.phase)
    check("统计到 80 条", eng.snapshot().stat_questions == 80,
          eng.snapshot().stat_questions)


def test_restate_on_idle():
    print("[冷场重述]")
    eng, clk = boot(mkcfg(hint_seconds=99999,
                          max_hints=3, restate_seconds=90))
    clk.advance(89)
    check("不到 90s 无动作", eng.tick() == [], eng.tick())
    clk.advance(2)
    acts = eng.tick()
    check("重述是纯 BROADCAST",
          acts and all(a.kind == ActionKind.BROADCAST for a in acts), kinds(acts))
    check("重述带 nudge", acts and acts[0].payload.get("nudge"), acts[0].payload if acts else None)


def test_hints_not_reset_by_chat():
    print("[提示走绝对时间轴, 不受发言影响]")
    # 时间轴从出题那一刻起算。有人聊天**不应**重置它 ——
    # 否则一直有人说话, 提示和揭晓就永远不来。
    eng, clk = boot(mkcfg(hint_seconds=45.0, restate_seconds=999999))
    clk.advance(40)
    eng.submit_danmaku("u1", "甲", "随便聊聊")
    clk.advance(6)                               # 累计 46s -> 到点了
    h1 = [a for a in eng.tick() if a.kind == ActionKind.HINT]
    check("发言不推迟提示", h1 != [], eng.tick())
    # 在途期间不重复派发(第三轮 review: 一次只允许一条提示在途)
    check("在途时不重复派发",
          [a for a in eng.tick() if a.kind == ActionKind.HINT] == [])
    # worker 回调(真实路径总会回调, 成功或失败)
    eng.submit_hint("第一条提示")
    # 再走一格, 第二条提示也应到点
    clk.advance(45)
    check("第二条也按时间轴到点",
          [a for a in eng.tick() if a.kind == ActionKind.HINT] != [], eng.tick())


def test_history_trim():
    print("[上下文双封顶]")
    eng, clk = boot(mkcfg(qa_max_records=20, qa_max_chars=400))
    for i in range(200):
        eng.submit_danmaku(f"u{i}", f"观众{i}", f"#这是第{i}个相当长的问题用来撑满字符预算")
        eng.tick()
        eng.submit_qa([QAResult(qid=i + 1, verdict="是", comment="x" * 20)])
    p = eng._probe()
    check("条数封顶", p["history"] <= 20, p)
    check("字符封顶", p["history_chars"] <= 400, p)
    check("qa_total 持续增长", eng.snapshot().qa_total == 200, eng.snapshot().qa_total)
    tr = eng._transcript_locked()
    check("统计行存在", any("问答统计" in ln for ln in tr), tr[-1:])


# ======================================================================
# Step 09 — 完整 QA archive
# ======================================================================
def test_qa_archive_keeps_everything_beyond_ui_tail():
    """**核心**: >120 条 QA 时, archive 完整, UI 仍只取尾窗。

    修之前 snapshot 的 `qa_archive` 是从 `_qa_log` 现算的, 而后者在
    120 条处被截断 —— 于是"归档"同样只留 120 条, 超出的静默消失。
    一场直播几百条问答时, 后面全部丢失, 而且**看不出来**(字段还在,
    只是短了)。
    """
    print("\n[S09-1] >120 QA: archive 完整 / UI 只取尾窗")
    eng, _clk = boot(mkcfg(qa_max_records=500, qa_max_chars=99999))
    N = 200
    for i in range(N):
        eng.submit_danmaku(f"u{i}", f"观众{i}", f"#第{i}个问题")
        eng.tick()
        eng.submit_qa([QAResult(qid=i + 1, verdict="是", comment="c")])
    snap = eng.snapshot()
    check(f"archive 有全部 {N} 条",
          len(snap.qa_archive) == N, len(snap.qa_archive))
    check("qa_total == N", snap.qa_total == N, snap.qa_total)
    # UI 尾窗仍然是有界的
    check("UI qa_log 仍只取尾窗(<=40)", len(snap.qa_log) <= 40,
          len(snap.qa_log))
    # 内部上屏窗口也仍被截到 120
    check("_qa_log 被截到 120", len(eng._qa_log) == 120, len(eng._qa_log))
    check("_qa_archive **没有**被截",
          len(eng._qa_archive) == N, len(eng._qa_archive))
    # archive 必须真的含**最早的**那几条 —— 这是"完整"的定义
    qids = [r.get("qid") for r in snap.qa_archive]
    check("archive 含第 1 条(qid=1)", 1 in qids, qids[:5])
    check("archive 含最后一条", N in qids, qids[-5:])
    check("archive 的 qid 无缺失",
          set(qids) == set(range(1, N + 1)),
          sorted(set(range(1, N + 1)) - set(qids))[:10])


def test_qa_archive_resets_between_puzzles():
    """archive 是**每道题**的存档 -> 开新题时清空(与 _qa_log 同步)。"""
    print("\n[S09-2] 新题清空 archive")
    eng, clk = boot(mkcfg())
    for i in range(5):
        eng.submit_danmaku(f"u{i}", f"观众{i}", f"#问题{i}")
        eng.tick()
        eng.submit_qa([QAResult(qid=i + 1, verdict="是")])
    check("archive 有 5 条", len(eng._qa_archive) == 5, len(eng._qa_archive))
    # 走完这一题
    eng._enter_revealing_locked(clk.t, "giveup", "")
    clk.advance(30.0)
    eng.tick(clk.t)
    if eng.phase != Phase.SETTING:
        eng.phase = Phase.SETTING
    eng.submit_riddle("下一道题。为什么?", "下一个谜底。", ["h1", "h2", "h3"],
                      title="下一题", expect_round=eng.round_index)
    check("新题开始时 archive 已清空",
          len(eng._qa_archive) == 0, len(eng._qa_archive))
    check("_qa_log 也清空了", len(eng._qa_log) == 0, len(eng._qa_log))


def test_qa_archive_includes_hint_and_restate():
    """archive 记的是"这道题发生过的全部", 提示/重述(qid<0)也在内。"""
    print("\n[S09-3] archive 含提示/重述记录")
    eng, _clk = boot(mkcfg())
    eng.submit_danmaku("u1", "观众1", "#问题")
    eng.tick()
    eng.submit_qa([QAResult(qid=1, verdict="是")])
    n_before = len(eng._qa_archive)
    eng._append_qa_locked(QARec(qid=-1, user_name="", text="",
                                verdict="", comment="", kind="hint"))
    check("提示也进 archive", len(eng._qa_archive) == n_before + 1,
          len(eng._qa_archive))


def test_qa_archive_stays_sorted_past_ui_cap():
    """**Batch B closeout**: 超过 UI 上限后 archive 仍按 qid 有序。

    原来有个"快路径/慢路径"分支: 120 条以内按 qid 插入, 超了就变成
    直接 append。并发回答在 120 条之后会乱序 —— 而且是"跑久了才出现"
    的那种, 最难排查。
    """
    print("\n[S09b] archive 超过 UI 上限后仍有序")
    eng, _clk = boot(mkcfg(qa_max_records=500, qa_max_chars=99999))
    N = 160                      # 明确超过 120 的 UI 截断线
    for i in range(N):
        eng.submit_danmaku(f"u{i}", f"观众{i}", f"#第{i}个问题")
        eng.tick()
        eng.submit_qa([QAResult(qid=i + 1, verdict="是")])
    qids = [r.qid for r in eng._qa_archive]
    check(f"archive 有全部 {N} 条", len(qids) == N, len(qids))
    check("**严格递增**(无乱序)",
          qids == sorted(qids), qids[:14])


def test_qa_archive_out_of_order_arrival_stays_sorted():
    """乱序到达(并发回答)也要插到正确位置。"""
    print("\n[S09c] 乱序到达仍有序")
    eng, _clk = boot(mkcfg())
    for qid in (3, 1, 4, 2):
        eng._append_qa_locked(QARec(qid=qid, user_name="u", text=f"q{qid}",
                                    verdict="是", comment="", kind="qa"))
    qids = [r.qid for r in eng._qa_archive]
    check("按 qid 排序", qids == [1, 2, 3, 4], qids)


def test_transcript_bounded():
    print("[长跑: transcript 有界]")
    eng, clk = boot(mkcfg(qa_max_records=10, qa_max_chars=300))
    for i in range(500):
        eng.submit_danmaku(f"u{i}", f"观众{i}", f"#问题{i}很长的描述文字用来撑爆上下文" * 2)
        eng.tick()
        eng.submit_qa([QAResult(qid=i + 1, verdict="无关")])
    tr = eng._transcript_locked()
    total = sum(len(x) for x in tr)
    check("transcript 有界(<1000)", total < 1000, total)
    check("统计行在", any("问答统计" in x for x in tr), tr[-1:])


def test_stop_and_stream_end():
    print("[停止 / 下播]")
    eng, clk = boot(mkcfg())
    acts = eng.on_stream_ended()
    check("下播 -> STOPPED", eng.phase == Phase.STOPPED, eng.phase)
    check("有 BROADCAST", any(a.kind == ActionKind.BROADCAST for a in acts), acts)
    check("停止后 tick 无动作", eng.tick() == [], eng.tick())
    check("停止后不收提问", eng.submit_danmaku("u", "甲", "#问题") == [])
    check("停止后 submit_qa 无动作", eng.submit_qa([QAResult(1, "是")]) == [])


def test_clock_jump():
    print("[时钟跳变不级联]")
    # 跳 100000 秒: 一次 tick 最多推进到"揭晓"这一步, 不应连跳多题。
    eng, clk = boot(mkcfg(hint_seconds=60.0, restate_seconds=999999,
                          max_hints=3, reveal_hold_seconds=30.0))
    clk.advance(100000)
    acts = eng.tick()
    check("一步走到揭晓", eng.phase == Phase.REVEALING, eng.phase)
    check("只发一个动作", len(acts) == 1, [a.kind for a in acts])
    check("不级联多题", eng._probe()["puzzle_index"] == 1, eng._probe())


def test_snapshot_keys():
    print("[快照字段]")
    eng, clk = boot(mkcfg())
    d = eng.snapshot().to_json()
    for k in ("phase", "puzzle", "puzzle_index", "qa_log", "qa_total",
              "pending_count", "revealed_answer", "solved", "solved_by",
              "next_puzzle_ms", "story_index", "stats", "debug"):
        check(f"含 {k}", k in d, list(d))
    check("story_index 别名", d["story_index"] == d["puzzle_index"], d)
    for k in ("questions", "answered", "solved", "dropped", "viewers_seen"):
        check(f"stats 含 {k}", k in d["stats"], list(d["stats"]))


def test_hint_order_and_dedup():
    print("[提示: 保留历史 + 挡重复]")
    # `hint_min_gap_seconds=0`: 这条测的是**提示历史与去重**, 不是冷却。
    # 它每 6 秒推进一次时间轴, 而 H1 的默认冷却 45 秒会把第 2、3 条挡住
    # —— 那不是这条用例要断言的东西。冷却有 H1-C 专门测。
    eng, clk = boot(mkcfg(hint_seconds=5, restate_seconds=9999, max_hints=3,
                          hint_min_gap_seconds=0))
    eng.submit_danmaku("u1", "甲", "#问题一")
    eng.tick()
    eng.submit_qa([QAResult(qid=1, verdict="是")])
    for i in range(3):
        clk.advance(6)
        for a in eng.tick():
            if a.kind == ActionKind.HINT:
                eng.submit_hint(f"提示{i + 1}")
    log = eng.snapshot().qa_log
    hints = [r for r in log if r["kind"] == "hint"]
    # 提示历史**保留**(观众要能回看之前给过的线索)
    check("三条提示都保留", len(hints) == 3, hints)
    check("按时间正序", [h["text"] for h in hints] == ["提示1", "提示2", "提示3"],
          [h["text"] for h in hints])
    check("问答仍在", any(r["kind"] == "qa" for r in log), log)
    # 同一条提示重复提交 -> 不重复上屏
    n = len([r for r in eng.snapshot().qa_log if r["kind"] == "hint"])
    eng.submit_hint("提示3")
    check("同一条提示不重复",
          len([r for r in eng.snapshot().qa_log if r["kind"] == "hint"]) == n,
          eng.snapshot().qa_log)
    # qa_total 只计问答, 不被提示撑大
    check("qa_total 只计问答", eng.snapshot().qa_total == 1,
          eng.snapshot().qa_total)
    check("hint_count 仍累计", eng.snapshot().hint_count == 3,
          eng.snapshot().hint_count)


def test_commands_are_not_swallowed():
    print("[指令不被吞掉]")
    # 实测 bug: 早先的缓冲路径曾把 _accept_danmaku 的返回值丢掉,
    # 导致 #提示 / #下一题 **彻底失效**(观众发了没反应)。
    # Q12 删掉了缓冲, 指令现在**同步**返回动作 —— 这里钉住它仍生效。
    eng, clk = boot(mkcfg())
    clk.advance(60)          # 过掉 #提示 的 20 秒节流
    acts = eng.submit_danmaku("u1", "甲", "#提示")
    check("#提示 生效(同步返回)",
          any(a.kind == ActionKind.HINT for a in acts), kinds(acts))
    # #下一题 同理
    clk.advance(30)
    acts = eng.submit_danmaku("u1", "甲", "#下一题")
    check("#下一题 生效",
          any(a.kind == ActionKind.REVEAL for a in acts), kinds(acts))


def test_msg_id_dedupe():
    """Q12 主路径: 同一个 msg_id 来两次 -> 第二次丢弃。"""
    print("\n[Q12] msg_id 精确去重")
    eng, clk = boot(mkcfg())
    eng.submit_danmaku("u1", "甲", "#第一次", message_id="m1")
    check("第一条进屏", len(eng._danmaku) == 1, len(eng._danmaku))
    clk.advance(5)          # 越过那条 2s 成对判重(见下), 单独验证 msg_id 去重
    eng.submit_danmaku("u1", "甲", "#第一次", message_id="m1")
    check("**同一 ID 第二条被丢弃**", len(eng._danmaku) == 1, len(eng._danmaku))
    check("计入 dup_ids", eng._dup_ids == 1, eng._dup_ids)
    # 不同 ID + 相同内容 -> 都要保留(ID 方案比内容指纹强的地方)
    clk.advance(5)
    eng.submit_danmaku("u1", "甲", "#第一次", message_id="m2")
    check("**不同 ID 同内容都保留**", len(eng._danmaku) == 2, len(eng._danmaku))


def test_msg_id_beats_pairwise_dedupe():
    """msg_id 去重发生在**那条 2s 成对判重之前**吗?

    不是 —— 2s 成对判重是更早的、独立的一道闸(同一人同一内容 2 秒内
    再来一条, 实测抖音会成对推送)。两条路径**并存**且各管一段:
      - 2s 成对判重: 挡"抖音把同一条推两次", 不依赖 msg_id;
      - msg_id 去重:  挡"重连后整批重放", 不依赖时间/内容。
    这里钉住它们**都**有效, 且不会互相破坏。
    """
    print("\n[Q12] msg_id 与 2s 成对判重并存")
    eng, clk = boot(mkcfg())
    # 同一人同一内容、2 秒内、**不同** msg_id -> 被 2s 规则挡住
    eng.submit_danmaku("u1", "甲", "#同一句", message_id="a1")
    eng.submit_danmaku("u1", "甲", "#同一句", message_id="a2")
    check("**2s 内同一人同一内容被挡**", len(eng._danmaku) == 1,
          len(eng._danmaku))
    # 不同人的相同内容 -> 都要保留(2s 规则只管同一人)
    eng.submit_danmaku("u2", "乙", "#同一句", message_id="b1")
    check("**不同人的相同内容保留**", len(eng._danmaku) == 2,
          len(eng._danmaku))


def test_msg_id_cache_is_bounded():
    print("\n[Q12] msg_id 去重表有界")
    eng, clk = boot(mkcfg(msg_id_cache_size=5))
    for i in range(20):
        eng.submit_danmaku("u1", "甲", f"#问题{i}", message_id=f"m{i}")
    check("表不超过上限", len(eng._seen_msg_ids) <= 5, len(eng._seen_msg_ids))
    # 最旧的已被挤出 -> 再来一次不会被判重(这是有界的代价, 但 2000 的
    # 默认容量远超任何真实重放, 所以实际不会漏)
    eng.submit_danmaku("u1", "甲", "#问题0", message_id="m0")
    check("被挤出的旧 ID 不再判重", eng._dup_ids == 0, eng._dup_ids)


def test_distinct_viewers_burst_is_not_replay():
    """**§9.3 第一条**: 3 个不同观众 500ms 内各发一条不同问题 -> 不判重放。

    这正是旧机制会误杀的场景(1.5s 内 3 条 -> 整批丢 + 压制 5 秒)。
    """
    print("\n[Q12] 多人爆发不误杀(3 人 500ms)")
    eng, clk = boot(mkcfg())
    rows = [("u1", "甲", "#是父母吗"), ("u2", "乙", "#是兄弟吗"),
            ("u3", "丙", "#是同学吗")]
    for uid, name, q in rows:
        eng.submit_danmaku(uid, name, q)      # clock 不动 = 同一瞬间
        clk.advance(0.5)
    check("**零重放判定**", eng._replays == 0, eng._replays)
    check("**三条全部上屏**", len(eng._danmaku) == 3, len(eng._danmaku))


def test_ten_viewers_one_second_is_not_replay():
    """**§9.3 第二条**: 10 个不同观众 1 秒内发言 -> 不判重放。"""
    print("\n[Q12] 十人 1 秒内爆发不误杀")
    eng, clk = boot(mkcfg())
    for i in range(10):
        eng.submit_danmaku(f"u{i}", f"观众{i}", f"#问题{i}")
        clk.advance(0.1)
    check("**零重放判定**", eng._replays == 0, eng._replays)
    check("**十条全部上屏**", len(eng._danmaku) == 10, len(eng._danmaku))


def test_reconnect_guard_only_after_reconnect():
    """没有重连 -> guard 关着 -> 就算内容重复也不判重放。"""
    print("\n[Q12] 没重连时不启用 guard")
    eng, clk = boot(mkcfg())
    for _ in range(5):
        eng.submit_danmaku("u1", "甲", "#同一句")
        clk.advance(3)          # 越过 2s 成对判重, 单独验证 guard 没开
    check("**无重连 => 零重放判定**", eng._replays == 0, eng._replays)
    check("全部上屏", len(eng._danmaku) == 5, len(eng._danmaku))


def _connect_then_reconnect(eng) -> None:
    """模拟一次**真实重连**。

    Q12c 之后 `engine.on_reconnect()` 的语义是"已经确认发生了一次真实
    重连" —— 它总是开 guard。首次/重连的区分在**传输层**
    (`LiveSource`), 不在引擎这层, 所以这里不需要再调两次。
    """
    eng.on_reconnect()


def test_reconnect_replay_suppressed():
    """**§9.3 第三条**: 重连后重复刚才完全相同的 8 条 -> 全部抑制。

    这 8 条**内容各不相同** —— 正是旧实现漏掉的形态(旧版数的是"单个
    fingerprint 在历史里出现 >=3 次", 各不相同的话每条纹丝不动地漏过去)。
    """
    print("\n[Q12] 重连后重放被抑制(8 条各不相同)")
    eng, clk = boot(mkcfg())
    msgs = [(f"u{i}", f"观众{i}", f"#问题{i}") for i in range(8)]
    # 先正常收下这批(建立 baseline)。间隔 3s 避开 2s 成对判重。
    for uid, name, q in msgs:
        eng.submit_danmaku(uid, name, q)
        clk.advance(3.0)
    before = len(eng._danmaku)
    check("基线已建立(8 条各不相同)", before == 8, before)
    _connect_then_reconnect(eng)
    clk.advance(1.0)
    # 同样的 8 条再来一遍
    for uid, name, q in msgs:
        eng.submit_danmaku(uid, name, q)
        clk.advance(0.2)
    check("**重放被识别**", eng._replays > 0, eng._replays)
    check("**重放不再上屏**", len(eng._danmaku) == before, len(eng._danmaku))


def test_reconnect_mixed_old_and_new():
    """**§9.3 第四条**: 重连后 6 条旧 + 2 条新 -> 旧抑制、新保留。"""
    print("\n[Q12] 重连后新旧混合")
    eng, clk = boot(mkcfg())
    # 6 条旧消息**各不相同**(Q12b: 旧版要求单指纹重复 3 次才能识别,
    # 那个前提本身就是错的 —— 真实重放就是一批各不相同的旧消息)。
    old = [(f"u{i}", f"观众{i}", f"#旧{i}") for i in range(6)]
    for uid, name, q in old:
        eng.submit_danmaku(uid, name, q)
        clk.advance(3.0)
    before = len(eng._danmaku)
    check("基线 6 条都上屏", before == 6, before)
    _connect_then_reconnect(eng)
    clk.advance(0.5)
    for uid, name, q in old:                   # 6 条旧的重来
        eng.submit_danmaku(uid, name, q)
        clk.advance(0.2)
    after_old = len(eng._danmaku)
    check("**旧消息被抑制**", after_old == before, (before, after_old))
    # 2 条全新的
    eng.submit_danmaku("u9", "丙", "#全新的问题一")
    clk.advance(0.2)
    eng.submit_danmaku("u8", "丁", "#全新的问题二")
    check("**新消息保留**", len(eng._danmaku) == before + 2,
          (before, len(eng._danmaku)))


def test_reconnect_signal_end_to_end():
    """**Q12c 装配测试**: 真正测 LiveSource -> Director/Engine 整条链。

    早先两层**各自**测了"首次过滤", 却没测它们串起来 —— 于是双重消耗
    被漏掉了:

        连接#1  LiveSource 吞掉            -> 引擎根本没收到
        连接#2  LiveSource 放行 -> 引擎又当首次吞掉
        连接#3  guard 才真正开             <- 第一次真实重连被白吃

    现在只有 `LiveSource` 一层负责区分首次/重连。这条测试走**真实的**
    LiveSource._on_first_frame -> director._on_reconnected -> engine,
    而不是分别调两层的内部方法。
    """
    print("\n[Q12c] 重连信号整条链(首次不开 / 重连立即开)")
    from director import Director
    from story.ingest import LiveSource

    cfg = mkcfg(live_id="123")
    dr = Director(cfg)
    # 造一个真 LiveSource, 接上 director 的回调(与 _build_source 一致)
    src = LiveSource(cfg, dr.inbox,
                     on_stream_end=dr._on_stream_end,
                     on_reconnect=dr._on_reconnect,
                     on_reconnected=dr._on_reconnected)
    eng = dr.engine
    check("初始 guard 关", eng._guard_until == 0.0, eng._guard_until)

    # ---- 连接 #1 首帧: 不该开 guard ----
    src._on_first_frame()
    check("**首次建连: guard 不开**", eng._guard_until == 0.0,
          eng._guard_until)

    # ---- 连接 #2 首帧(真实重连): **立即**开 guard ----
    src._on_first_frame()
    check("**第一次重连: guard 立即开**", eng._guard_until > 0,
          eng._guard_until)
    if dr._prefetcher:
        dr._prefetcher.shutdown()


def test_engine_on_reconnect_always_arms():
    """`engine.on_reconnect()` 语义纯化: 调用它 == 已确认真实重连。

    首次/重连的区分不在这一层(那是 `LiveSource` 的责任)。引擎这层若
    再挡一次, 就会把"第一次真实重连"吃掉。
    """
    print("\n[Q12c] engine.on_reconnect 总是开 guard")
    eng, clk = boot(mkcfg())
    eng.on_reconnect()
    check("**第一次调用就开**", eng._guard_until > 0, eng._guard_until)


def test_guard_expires():
    """guard 过窗口就失效 —— 几分钟后又说一遍是真人, 不是重放。"""
    print("\n[Q12] guard 到期后不再判重放")
    eng, clk = boot(mkcfg(replay_guard_seconds=10.0))
    for _ in range(5):
        eng.submit_danmaku("u1", "甲", "#同一句")
        clk.advance(3)          # 避开 2s 成对判重
    before = len(eng._danmaku)
    _connect_then_reconnect(eng)
    clk.advance(11)                            # 越过 guard 窗口
    eng.submit_danmaku("u1", "甲", "#同一句")
    check("**窗口外不判重放**", eng._replays == 0, eng._replays)
    check("正常上屏", len(eng._danmaku) == before + 1, len(eng._danmaku))


def test_incoming_streak_retroactively_suppressed():
    """**防自我增强**: 确认重放之前那几条"疑似"的消息**不能漏出去**。

    旧实现数的是"单 fingerprint 在历史里出现次数", 于是重放的前几条
    一旦写进 `_danmaku`, 后面的就能匹配到刚写进去的自己 —— 条件自我
    增强。现在改成"incoming 连续命中冻结的 baseline", 且确认前先缓冲,
    所以哪怕只差最后一条才到阈值, 前面几条也会被一起回收。
    """
    print("\n[Q12b] 重放批次整批回收(不自我增强)")
    eng, clk = boot(mkcfg(replay_guard_min_repeats=3, replay_guard_seconds=5.0))
    old = [(f"u{i}", f"观众{i}", f"#老问题{i}") for i in range(3)]
    for uid, name, q in old:
        eng.submit_danmaku(uid, name, q)
        clk.advance(3)
    before = len(eng._danmaku)
    check("baseline 3 条", before == 3, before)
    _connect_then_reconnect(eng)
    clk.advance(0.5)
    # 只重放 2 条 —— **不到阈值**, 应该**放行**(可能真是真人在问同样的事)
    for uid, name, q in old[:2]:
        eng.submit_danmaku(uid, name, q)
        clk.advance(0.2)
    check("**未达阈值 -> 不判重放**", eng._replays == 0, eng._replays)
    # 缓冲里那 2 条要等"确认不是重放"才放行。两种释放路径: 后续新消息,
    # 或 guard 到期。这里走**到期**那条(推到窗口外再 tick)。
    clk.advance(11)
    eng.tick()
    check("**guard 到期后补上屏**", len(eng._danmaku) == before + 2,
          (before, len(eng._danmaku)))


def test_streak_broken_by_new_message():
    """连续命中被一条**新消息**打断 -> 不是重放, 缓冲全部放行。"""
    print("\n[Q12b] 新消息打断 streak")
    eng, clk = boot(mkcfg(replay_guard_min_repeats=4))
    old = [(f"u{i}", f"观众{i}", f"#老{i}") for i in range(3)]
    for uid, name, q in old:
        eng.submit_danmaku(uid, name, q)
        clk.advance(3)
    before = len(eng._danmaku)
    _connect_then_reconnect(eng)
    clk.advance(0.5)
    # 2 条旧的(命中, 进缓冲) + 1 条全新的(打断)
    eng.submit_danmaku("u0", "观众0", "#老0")
    clk.advance(0.2)
    eng.submit_danmaku("u1", "观众1", "#老1")
    clk.advance(0.2)
    eng.submit_danmaku("u7", "新人", "#从没见过的问题")
    check("**判为非重放**", eng._replays == 0, eng._replays)
    check("**缓冲里的 2 条补上屏 + 新的 1 条**",
          len(eng._danmaku) == before + 3, (before, len(eng._danmaku)))


def test_msg_id_bypasses_pairwise_dedupe():
    """Q12b: **有 msg_id 时跳过 2s 成对判重**。

    既然拿到了平台唯一 ID, 它就该是唯一的重复判据。否则"不同 ID =
    不误杀"这个契约不成立: 同一人 2 秒内真的发了两次相同内容(平台给了
    两个不同 ID), 第二条仍会被那条启发式吃掉。
    """
    print("\n[Q12b] 有 ID 时绕过 2s 成对判重")
    eng, clk = boot(mkcfg())
    # 同一人同一内容、2 秒内、**不同** ID -> 两条都要保留
    eng.submit_danmaku("u1", "甲", "#同一句", message_id="x1")
    eng.submit_danmaku("u1", "甲", "#同一句", message_id="x2")
    check("**有 ID 时两条都上屏**", len(eng._danmaku) == 2,
          len(eng._danmaku))
    # 没有 ID 时, 那条启发式仍然生效(留给上游不给 ID 的情况)
    eng.submit_danmaku("u2", "乙", "#另一句")
    eng.submit_danmaku("u2", "乙", "#另一句")
    check("**无 ID 时 2s 成对判重仍生效**", len(eng._danmaku) == 3,
          len(eng._danmaku))


def test_guard_release_keeps_user_name():
    """**Q12c**: 缓冲放行后 **user_name 必须完整保留**。

    早先 pending 只存 `(user_id, content, ts, message_id)`, 放行时传
    `user_name=""` —— 一条"疑似重放、后来证明是真人"的提问会变成匿名,
    问答流/回答日志/猜中者姓名全错。
    """
    print("\n[Q12c] guard 放行保留观众名")
    eng, clk = boot(mkcfg(replay_guard_min_repeats=5))
    # baseline: 两条(各只出现一次 —— 正是"各不相同"的重放形态)
    eng.submit_danmaku("u1", "甲观众", "#旧问题一")
    clk.advance(3)
    eng.submit_danmaku("u2", "乙观众", "#旧问题二")
    clk.advance(3)
    _connect_then_reconnect(eng)
    clk.advance(0.5)
    # 2 条疑似(命中 baseline) -> 进缓冲, 未达阈值 5
    eng.submit_danmaku("u1", "甲观众", "#旧问题一")
    clk.advance(0.2)
    eng.submit_danmaku("u2", "乙观众", "#旧问题二")
    clk.advance(0.2)
    # 一条全新的 -> 打断 streak, 缓冲里的两条应被放行
    eng.submit_danmaku("u3", "丙观众", "#全新的问题")
    check("未判重放", eng._replays == 0, eng._replays)
    names = [r.user_name for r in eng._danmaku if r.user_name]
    check("**放行的消息带回了观众名**",
          "甲观众" in names and "乙观众" in names, names)
    check("**没有匿名(空 user_name)的条目**",
          all(r.user_name for r in eng._danmaku),
          [r.user_name for r in eng._danmaku])


def test_guard_release_does_not_swallow_actions():
    """**Q12c**: 缓冲里若是有即时动作的命令(`#提示`), 放行时动作不能丢。

    早先 `_flush_guard_pending_locked()` 把 `_accept_danmaku` 的返回值
    直接丢了。普通 `#问题` 主要只是入队所以看不出, 但 `#提示`/`#下一题`
    产生的 HINT/REVEAL 会静默失效 —— 与 `_flush_burst` 曾踩的是同一类
    bug("把消息补处理了" != "把动作也交付了")。
    """
    print("\n[Q12c] guard 放行不吞动作")
    # 构造一个**只有放行才会产动作**的场景, 否则测不出"动作被吞":
    #   - 当前这条用普通提问(在 QA 里只入队, 无动作);
    #   - 缓冲里那条用 `#提示`(在 QA 里产 HINT)。
    # 这样断言里出现的 HINT 只可能来自放行路径。
    eng, clk = boot(mkcfg(replay_guard_min_repeats=5,
                          replay_guard_seconds=300.0))
    clk.advance(60)                              # 过 #提示 的 20s 节流
    eng.submit_danmaku("u1", "甲", "#提示")       # baseline: 产 HINT
    eng.submit_hint("提示文本")                   # 清掉在途标志
    clk.advance(3)
    _connect_then_reconnect(eng)
    clk.advance(0.5)
    clk.advance(60)                              # 再过节流(guard 仍开)
    # 疑似命中 baseline -> 进缓冲, 本身不上屏
    eng.submit_danmaku("u1", "甲", "#提示")
    check("疑似已进缓冲", len(eng._guard_pending) == 1,
          len(eng._guard_pending))
    # 当前这条是普通提问(**本身不产动作**), 唯一可能的动作来自放行
    acts = eng.submit_danmaku("u9", "新人", "#全新问题")
    ks = [a.kind for a in acts]
    check("**放行的 HINT 被交付了(没吞)**", ActionKind.HINT in ks, ks)


def test_determinism():
    print("[确定性]")
    def run():
        eng, clk = boot(mkcfg())
        eng.submit_danmaku("u1", "甲", "#问题一")
        eng.submit_danmaku("u2", "乙", "#问题二")
        a1 = [(a.kind.value, tuple(sorted(a.payload.keys()))) for a in eng.tick()]
        a2 = [(a.kind.value, tuple(sorted(a.payload.keys())))
              for a in eng.submit_qa([QAResult(1, "是")])]
        return a1 + a2
    check("同输入同动作序列", run() == run())


def test_llm_failure_never_drops():
    """LLM 不回来时, 提问**最终**必须有裁决 —— 不能静默消失。

    Hotfix B 之后这条路更短了: 第一次在途超时就直接给"未判定",
    不再靠"重试 N 次才兜底"。
    """
    print("[LLM 故障不丢提问]")
    eng, clk = boot(mkcfg(qa_inflight_timeout=10.0))
    eng.submit_danmaku("u1", "甲", "#问题")
    eng.tick()
    for _ in range(6):
        clk.advance(11)
        eng.tick()
    s = eng.snapshot()
    check("提问最终有裁决", len(s.qa_log) >= 1, s.qa_log)
    check("**且不再留下假在途**",
          eng._probe()["inflight"] == 0 and eng._probe()["pending"] == 0,
          eng._probe())


def test_coverage_reaches_archive():
    """裁判覆盖结果要**一路走到落盘** —— 这是复盘的关键数据。

    只有"未中"两个字是没法改 prompt 的: 必须能看到是 cause 没中还是
    mechanism 没中、命中了哪几条 atom。
    """
    print("[覆盖结果: 从出题一路带到落盘]")
    cfg = mkcfg()
    eng = RoundEngine(cfg)
    eng.start(0.0)
    ATOMS = [{"role": "cause", "text": "他是后天失明的"},
             {"role": "mechanism", "text": "灯是给别人照的, 免得撞到他"}]
    eng.submit_riddle("他每晚点灯却不让光照到自己。为什么?", "他是盲人。",
                      ["a", "b", "c"], title="T", solve_atoms=ATOMS,
                      fair_clues=["谜面写了'不让光照到自己'"])
    eng.tick(0.1)
    eng.submit_danmaku(1, "甲", "#他是盲人吗", 0.2)
    acts = eng.tick(0.3)
    ans = [a for a in acts if a.kind.value == "answer"]
    check("派发的 ANSWER 带上了 atoms",
          ans and ans[0].payload.get("solve_atoms") == ATOMS,
          ans[0].payload.get("solve_atoms") if ans else None)
    qid = ans[0].payload["qid"]
    eng.submit_qa([QAResult(qid=qid, verdict="不是", comment="方向不对",
                            is_guess=True, cause_hit=True, mechanism_hit=False,
                            matched_atoms=[0])], model="m")
    eng.tick(0.4)
    s = eng.snapshot()
    arch = s.qa_archive[-1]
    check("落盘记录带 cause_hit", arch.get("cause_hit") is True, arch)
    check("落盘记录带 mechanism_hit", arch.get("mechanism_hit") is False, arch)
    check("落盘记录带 matched_atoms", arch.get("matched_atoms") == [0], arch)
    check("上屏记录**不含**内部字段",
          "cause_hit" not in s.qa_log[-1], s.qa_log[-1])


def test_reveal_payload_carries_atoms():
    """Q0.4 回归: REVEAL payload 必须带上 solve_atoms / fair_clues。

    这条链路曾经**结构性断裂**: director._archive_reveal() 已经读这两个
    字段了, 但 engine 的 REVEAL payload 从来没给, 于是落盘里恒为空数组,
    赛后复盘"为什么这条没判中"时对照不了任何东西 —— 而日志上一切正常。
    """
    atoms = [{"role": "cause", "text": "路上埋了东西"},
             {"role": "mechanism", "text": "他要每天去看"}]
    clues = ["谜面写了\"每天\""]
    clk = FakeClock()
    eng = RoundEngine(mkcfg(), clock=clk)
    eng.start()
    eng.submit_riddle("他每天绕远路回家。为什么？", "那条路上有他埋的东西。",
                      ["想想路"], title="绕路",
                      solve_atoms=atoms, fair_clues=clues)
    # say() 内部已经 tick 过, ANSWER 在它返回的动作里
    ans = [a for a in say(eng, clk, "u1", "甲", "#他是要去看什么东西吗")
           if a.kind == ActionKind.ANSWER]
    check("派发了 ANSWER", len(ans) == 1, ans)
    qid = ans[0].payload["qid"]
    acts = eng.submit_qa([QAResult(qid=qid, verdict="揭晓")])
    rev = [a for a in acts if a.kind == ActionKind.REVEAL]
    check("猜中产生了 REVEAL", len(rev) == 1, acts)
    p = rev[0].payload
    check("REVEAL payload 带 solve_atoms", p.get("solve_atoms") == atoms,
          p.get("solve_atoms"))
    check("REVEAL payload 带 fair_clues", p.get("fair_clues") == clues,
          p.get("fair_clues"))


def test_signature_recorded_and_passed_on():
    """Q4: 出题后记下指纹, 下一题的 RIDDLE 动作里带上最近指纹。"""
    clk = FakeClock()
    # 短 hold: 用 advance(31.0) 跨"揭晓 -> 下一题"(与展示时长无关)。
    eng = RoundEngine(mkcfg(reveal_hold_seconds=5.0), clock=clk)
    eng.start()
    sig = {"mechanism_family": "hidden_function",
           "solution_shape": "hidden_function_explains_behavior",
           "domain": "maritime", "death": False}
    eng.submit_riddle("灯塔只在退潮亮灯。为什么?", "因为礁石。", ["a", "b", "c"],
                      title="灯塔", solve_atoms=[{"role": "cause", "text": "x"},
                                                 {"role": "mechanism", "text": "y"}],
                      fair_clues=["只在退潮亮灯"], signature=sig)
    check("指纹记进了引擎", len(eng._recent_signatures) == 1,
          eng._recent_signatures)
    # 走到下一题 -> RIDDLE 动作里要带上最近指纹
    clk.advance(1)
    acts = eng.submit_qa([])
    clk.advance(1)
    eng.tick(clk.t)
    # 直接构造: 揭晓 -> 展示 30s -> 下一题
    eng._enter_revealing_locked(clk.t, "giveup", "")
    eng.submit_reveal("谜底")
    clk.advance(enc := 31.0)
    acts = eng.tick(clk.t)
    rid = [a for a in acts if a.kind == ActionKind.RIDDLE]
    check("产生了 RIDDLE 动作", len(rid) == 1, acts)
    rs = rid[0].payload.get("recent_signatures")
    check("RIDDLE 带上 recent_signatures", rs and len(rs) == 1, rs)
    check("signature 是**可序列化的 dict**",
          rs and isinstance(rs[0], dict)
          and rs[0].get("mechanism_family") == "hidden_function", rs)


def test_signature_window_bounded():
    """Q4: 指纹列表按 quality_recent_window 截断。"""
    clk = FakeClock()
    eng = RoundEngine(mkcfg(quality_recent_window=3), clock=clk)
    for i in range(6):
        eng.start() if eng.phase == Phase.IDLE else None
        if eng.phase != Phase.SETTING:
            eng.phase = Phase.SETTING
        eng.submit_riddle(f"第{i}题。为什么?", "底", ["a", "b", "c"],
                          signature={"mechanism_family": f"m{i}",
                                     "solution_shape": "s", "domain": "daily"})
        eng.phase = Phase.SETTING      # 强制回到 SETTING 再交一题
    check("指纹只留最近 3 条", len(eng._recent_signatures) == 3,
          [x.mechanism_family for x in eng._recent_signatures])
    check("留的是最新的 3 条",
          [x.mechanism_family for x in eng._recent_signatures] == ["m3", "m4", "m5"],
          [x.mechanism_family for x in eng._recent_signatures])


def test_touched_facts_accumulate_and_reset():
    """Q5: touched_fact_ids 要累加, 每题开始清空。"""
    eng, clk = boot(mkcfg())
    check("开局 touched 为空", eng._touched_fact_ids == set(),
          eng._touched_fact_ids)
    ans = [a for a in say(eng, clk, "u1", "甲", "#礁石吗")
           if a.kind == ActionKind.ANSWER]
    qid = ans[0].payload["qid"]
    eng.submit_qa([QAResult(qid=qid, verdict="是", touched_fact_ids=["f1"],
                            solution_candidate=False)])
    check("f1 记进 touched", eng._touched_fact_ids == {"f1"},
          eng._touched_fact_ids)
    ans2 = [a for a in say(eng, clk, "u2", "乙", "#涨潮呢")
            if a.kind == ActionKind.ANSWER]
    eng.submit_qa([QAResult(qid=ans2[0].payload["qid"], verdict="是",
                            touched_fact_ids=["f2", "f3"])])
    check("touched 累加", eng._touched_fact_ids == {"f1", "f2", "f3"},
          eng._touched_fact_ids)
    # 落盘记录里也要有
    arch = eng.snapshot().qa_archive
    check("archive 带 touched_fact_ids",
          arch[0].get("touched_fact_ids") == ["f1"], arch[0])
    check("archive 带 solution_candidate",
          arch[0].get("solution_candidate") is False, arch[0])
    # 上屏记录**不该**带这些内部字段
    check("上屏不含 touched", "touched_fact_ids" not in eng.snapshot().qa_log[0],
          eng.snapshot().qa_log[0])


def test_candidate_count_and_reset_on_new_puzzle():
    """Q5: candidate 计数, 以及开新题时清空。"""
    eng, clk = boot(mkcfg(reveal_hold_seconds=5.0, ))
    ans = [a for a in say(eng, clk, "u1", "甲", "#完整解释")
           if a.kind == ActionKind.ANSWER]
    eng.submit_qa([QAResult(qid=ans[0].payload["qid"], verdict="是",
                            solution_candidate=True, touched_fact_ids=["f1"])])
    check("candidate 计数 +1", eng._candidate_count == 1, eng._candidate_count)
    # 开新题 -> 清空
    eng._enter_revealing_locked(clk.t, "giveup", "")
    eng.submit_reveal("谜底")
    clk.advance(31.0)
    eng.tick(clk.t)
    eng.submit_riddle("新题。为什么?", "新底", ["a", "b", "c"])
    check("新题清空 touched", eng._touched_fact_ids == set(),
          eng._touched_fact_ids)
    check("新题清空 candidate 计数", eng._candidate_count == 0,
          eng._candidate_count)


def test_answer_action_carries_facts():
    """Q5: ANSWER 动作要把 facts 传给 worker。"""
    clk = FakeClock()
    eng = RoundEngine(mkcfg(), clock=clk)
    eng.start()
    from story.puzzle import PuzzleFact, PuzzleSpec
    spec = PuzzleSpec(puzzle="灯塔只在退潮亮灯。为什么?", answer="因为礁石。",
                      facts=[PuzzleFact(id="f1", text="退潮礁石露出", kind="core")])
    eng.submit_riddle("灯塔只在退潮亮灯。为什么?", "因为礁石。", ["a", "b", "c"],
                      spec=spec)
    ans = [a for a in say(eng, clk, "u1", "甲", "#礁石吗")
           if a.kind == ActionKind.ANSWER]
    check("ANSWER 带 facts", ans[0].payload.get("facts") == [
        {"id": "f1", "text": "退潮礁石露出", "kind": "core",
         "visibility": "hidden", "hintable": True}], ans[0].payload.get("facts"))


def test_retry_riddle_keeps_avoid_and_recent():
    """P0-2: retry 的 RIDDLE 动作必须**带上** avoid 与 recent_signatures。

    早先重试只带 reason+attempt, 于是:
      - director 收到 recent_signatures=[] -> Blueprint Scheduler 以为
        "前面一道题都没播过", 配额失效;
      - avoid=None -> 文本去重也失效。
    也就是说**只要发生一次外层 retry, 就能绕过整个 Q4**。
    """
    clk = FakeClock()
    eng = RoundEngine(mkcfg(riddle_max_attempts=3,
                            reveal_hold_seconds=5.0), clock=clk)
    eng.start()
    # 先播一题, 让它有 used_titles 与 signature
    eng.submit_riddle("第一题。为什么?", "底", ["a", "b", "c"],
                      signature={"mechanism_family": "hidden_function",
                                 "solution_shape": "hidden_function_explains_behavior",
                                 "domain": "maritime"})
    check("指纹已记录", len(eng._recent_signatures) == 1, eng._recent_signatures)
    # 进下一题 -> 出题失败 -> retry
    eng._enter_revealing_locked(clk.t, "giveup", "")
    eng.submit_reveal("谜底")
    clk.advance(31.0)
    eng.tick(clk.t)
    check("进入 SETTING", eng.phase == Phase.SETTING, eng.phase)
    acts = eng.submit_riddle(None, error="网关抖动")
    rid = [a for a in acts if a.kind == ActionKind.RIDDLE]
    check("产生了 retry 的 RIDDLE", len(rid) == 1, acts)
    pl = rid[0].payload
    check("retry 带 avoid", "avoid" in pl, pl)
    check("retry 带 recent_signatures", "recent_signatures" in pl, pl)
    check("recent_signatures 非空(核心断言)",
          len(pl.get("recent_signatures") or []) == 1, pl)
    check("reason 标为 retry", pl.get("reason") == "riddle_retry", pl)


def test_first_and_retry_riddle_actions_match():
    """P0-2: 首轮与 retry 的 payload 键必须一致 —— 防止将来又漏一个。"""
    clk = FakeClock()
    eng = RoundEngine(mkcfg(riddle_max_attempts=3), clock=clk)
    first = [a for a in eng.start() if a.kind == ActionKind.RIDDLE][0]
    retry = eng._riddle_action_locked("riddle_retry", 1)
    a, b = set(first.payload), set(retry.payload)
    check("键集合一致(除 attempt)", a - {"attempt"} == b - {"attempt"},
          (sorted(a), sorted(b)))
    check("两者都带 avoid", "avoid" in a and "avoid" in b, (a, b))
    check("两者都带 recent_signatures",
          "recent_signatures" in a and "recent_signatures" in b, (a, b))


def test_retry_payload_is_a_copy():
    """P0-2: payload 里的列表必须是**拷贝** —— 否则后面改引擎状态会串改历史。"""
    clk = FakeClock()
    eng = RoundEngine(mkcfg(), clock=clk)
    eng.start()
    eng.submit_riddle("第一题。为什么?", "底", ["a", "b", "c"],
                      signature={"mechanism_family": "hidden_function",
                                 "solution_shape": "s", "domain": "maritime"})
    act = eng._riddle_action_locked("riddle")
    eng._recent_signatures.clear()
    check("recent_signatures 是拷贝",
          len(act.payload["recent_signatures"]) == 1,
          act.payload["recent_signatures"])


def test_fallback_riddle_is_structured():
    """P1(第二轮): 兜底谜题也必须是完整 PuzzleSpec。

    早先 `submit_riddle(riddle[0], riddle[1], FALLBACK_HINTS)` 没有
    facts/atoms/clues —— 正式 Q&A 于是 facts 为空 -> 主持人退回"只看
    文学谜底", Final Judge 也没有 atom gate。质量系统在这条路径上
    等于不存在, 而它恰好是"出题连挂 3 次"时**唯一**会上屏的题。
    """
    clk = FakeClock()
    eng = RoundEngine(mkcfg(riddle_max_attempts=2), clock=clk)
    eng.start()
    # 连续失败到耗尽
    eng.submit_riddle(None, error="网关抖动")
    acts = eng.submit_riddle(None, error="还是抖动")
    check("最终进 QA", eng.phase == Phase.QA, eng.phase)
    check("兜底题有谜面", bool(eng._puzzle), eng._puzzle)
    sp = eng._spec
    check("兜底题**有 spec**(核心断言)", sp is not None, sp)
    check("兜底题**有 facts**", sp is not None and len(sp.facts) >= 4, sp)
    check("兜底题有 solve_atoms", len(eng._solve_atoms) >= 2, eng._solve_atoms)
    check("兜底题有 fair_clues", len(eng._fair_clues) >= 1, eng._fair_clues)
    check("兜底题有 signature",
          sp is not None and bool(sp.signature.mechanism_family), sp)


def test_fallback_specs_all_valid():
    """P1: 4 道兜底题都要过**同一套**硬校验(不能是手写的法外之地)。"""
    from story import parser as P
    from story.quality import validate_spec
    for i in range(4):
        sp = P.fallback_spec(i)
        vr = validate_spec(sp)
        check(f"兜底题 {i} 过硬校验", vr.ok, vr.why())
        check(f"兜底题 {i} 没有待修项", not vr.fixable, vr.must_fix())


def test_fallback_specs_use_official_taxonomy():
    """P1(第三轮 review): 兜底题的 taxonomy 必须是**正式枚举**里的值。

    早先写的是 psychological_compulsion / psychological_necessity ——
    两个不存在的类别。没炸只是因为 validate_spec 不校验 signature 的
    enum, 而兜底题又不进配额。Q8 开始持久化 spec 之后, Pool / archive
    里就会出现两套并存的 taxonomy。
    """
    from story import parser as P
    from story.puzzle import MECHANISM_FAMILIES, SOLUTION_SHAPES,         DOMAINS, EMOTION_MODES, RELATIONS, TIME_SHAPES
    for i in range(4):
        sp = P.fallback_spec(i)
        sg = sp.signature
        check(f"兜底题 {i} mechanism_family 合法",
              sg.mechanism_family in MECHANISM_FAMILIES, sg.mechanism_family)
        check(f"兜底题 {i} solution_shape 合法",
              sg.solution_shape in SOLUTION_SHAPES, sg.solution_shape)
        check(f"兜底题 {i} domain 合法", sg.domain in DOMAINS, sg.domain)
        check(f"兜底题 {i} emotion_mode 合法",
              sg.emotion_mode in EMOTION_MODES, sg.emotion_mode)
        check(f"兜底题 {i} relation 合法", sg.relation in RELATIONS, sg.relation)
        check(f"兜底题 {i} time_shape 合法",
              sg.time_shape in TIME_SHAPES, sg.time_shape)


def test_fallback_does_not_pollute_quota():
    """P1: 兜底题**不该**进 recent_signatures。

    出题全挂是**系统故障**, 不是一个内容分布事实。把它登记进去会让
    下一题的 blueprint 选择基于"刚播过一道 emotional_motive"这种假前提,
    而且连挂几次就会把配额顶死。
    """
    clk = FakeClock()
    eng = RoundEngine(mkcfg(riddle_max_attempts=2), clock=clk)
    eng.start()
    eng.submit_riddle(None, error="挂了")
    eng.submit_riddle(None, error="又挂了")
    check("兜底题没有登记指纹", len(eng._recent_signatures) == 0,
          eng._recent_signatures)


def test_fallback_rotates():
    """P1: 兜底题要轮换 —— 总用同一道, 观众一看就知道出题挂了。"""
    clk = FakeClock()
    eng = RoundEngine(mkcfg(reveal_hold_seconds=5.0, riddle_max_attempts=1), clock=clk)
    eng.start()
    eng.submit_riddle(None, error="挂了")
    p1 = eng._puzzle
    # 揭晓 -> 下一题 -> 再挂
    eng._enter_revealing_locked(clk.t, "giveup", "")
    eng.submit_reveal("谜底")
    clk.advance(31.0)
    eng.tick(clk.t)
    eng.submit_riddle(None, error="又挂了")
    check("第二道兜底题与第一道不同", eng._puzzle != p1,
          (p1[:20], eng._puzzle[:20]))


def test_request_riddle_action_is_public_and_matches():
    """小修: director 不该直接调 engine 的 `_xxx_locked()`。

    新增的 `request_riddle_action()` 是公开入口, 结果必须与内部版本一致。
    """
    clk = FakeClock()
    eng = RoundEngine(mkcfg(), clock=clk)
    eng.start()
    eng.submit_riddle("第一题。为什么?", "底", ["a", "b", "c"],
                      signature={"mechanism_family": "hidden_function",
                                 "solution_shape": "s", "domain": "maritime"})
    pub = eng.request_riddle_action("riddle_deferred")
    check("返回 RIDDLE 动作", pub.kind == ActionKind.RIDDLE, pub.kind)
    check("reason 正确", pub.payload.get("reason") == "riddle_deferred",
          pub.payload)
    check("带 avoid", "avoid" in pub.payload, pub.payload)
    check("带 recent_signatures", "recent_signatures" in pub.payload,
          pub.payload)
    inner = eng._riddle_action_locked("riddle_deferred")
    check("公开版与内部版结果一致",
          pub.payload == inner.payload, (pub.payload, inner.payload))


def test_hint_payload_carries_spec_and_touched():
    """Q6(方案 §31/§32): HINT 动作要带上 spec 与 touched_fact_ids。

    没有这两样, worker 就只能"看着谜面随便点拨" —— 而"哪个方向还没被
    探索过"这件事模型看不到(touched 集合在引擎里), 只能由代码告诉它。
    """
    clk = FakeClock()
    eng = RoundEngine(mkcfg(hint_seconds=10.0), clock=clk)
    eng.start()
    from story.puzzle import PuzzleSpec
    sp = PuzzleSpec.from_dict({
        "puzzle": "灯塔守塔人只在退潮时亮灯。为什么?",
        "answer": "退潮礁石露出。",
        "facts": [{"id": "f1", "text": "退潮礁石露出", "kind": "core"},
                  {"id": "f2", "text": "灯是标礁石", "kind": "core"}],
        "solve_atoms": [{"id": "a1", "role": "cause", "text": "退潮",
                         "fact_ids": ["f1"]},
                        {"id": "a2", "role": "mechanism", "text": "标礁石",
                         "fact_ids": ["f2"]}],
        "fair_clues": [{"quote": "只在退潮时亮灯", "supports_atoms": ["a1"]}],
        "signature": {"mechanism_family": "hidden_function",
                      "solution_shape": "hidden_function_explains_behavior",
                      "domain": "maritime"},
    })
    eng.submit_riddle(sp.puzzle, sp.answer, sp.hints, spec=sp)
    # 模拟观众问过 f1 这个方向
    eng._touched_fact_ids.add("f1")
    acts = eng.tick(clk.t + 11.0)
    hints = [a for a in acts if a.kind == ActionKind.HINT]
    check("产出了 HINT", len(hints) == 1, acts)
    pl = hints[0].payload
    check("HINT 带 spec", pl.get("spec") is sp, pl.get("spec"))
    check("HINT 带 touched_fact_ids",
          pl.get("touched_fact_ids") == ["f1"], pl)
    check("仍然是列表不是 set(要能进 payload)",
          isinstance(pl.get("touched_fact_ids"), list), pl)


def test_hint_payload_touched_is_a_copy():
    """Q6: touched 必须是**拷贝** —— 否则 worker 读的时候引擎在改它。"""
    clk = FakeClock()
    eng = RoundEngine(mkcfg(hint_seconds=10.0), clock=clk)
    eng.start()
    eng.submit_riddle("谜面。为什么?", "底", ["a", "b", "c"])
    eng._touched_fact_ids.add("f1")
    acts = eng.tick(clk.t + 11.0)
    pl = [a for a in acts if a.kind == ActionKind.HINT][0].payload
    eng._touched_fact_ids.add("f2")
    check("touched 是拷贝", pl["touched_fact_ids"] == ["f1"],
          pl["touched_fact_ids"])


def test_archive_writes_full_schema():
    """Q7(方案 §34/§35): archive 必须写出完整 schema + 生成指标。

    以前只存 puzzle/answer —— 赛后复盘"这题为什么判错"时,
    facts/atoms/clues/blueprint 全都没有, 只能去翻日志。
    另外 §35 的过程指标(几稿出成 / 审稿打了什么)也必须落盘,
    否则"出题质量在变好还是变坏"根本无从判断。
    """
    import io
    import json
    import os
    import tempfile
    from director import Director
    from story.puzzle import PuzzleSpec

    cfg = mkcfg()
    out = os.path.join(tempfile.gettempdir(), "_hgt_arch_test.jsonl")
    if os.path.exists(out):
        os.remove(out)
    cfg.puzzle_out_path = out

    d = Director(cfg)
    d.engine.start()
    sp = PuzzleSpec.from_dict({
        "puzzle": "灯塔题。为什么?", "answer": "礁石。",
        "facts": [{"id": "f1", "text": "退潮礁石露出", "kind": "core"}],
        "solve_atoms": [{"id": "a1", "role": "cause", "text": "退潮",
                         "fact_ids": ["f1"]},
                        {"id": "a2", "role": "mechanism", "text": "标礁石",
                         "fact_ids": ["f1"]}],
        "fair_clues": [{"quote": "只在退潮时亮灯", "supports_atoms": ["a1"]}],
        "signature": {"mechanism_family": "hidden_function",
                      "solution_shape": "hidden_function_explains_behavior",
                      "domain": "maritime"},
        "blueprint": {"mechanism_family": "hidden_function",
                      "solution_shape": "hidden_function_explains_behavior",
                      "domain": "maritime"},
    })
    # 绑定当前常量, 别写死版本串 —— 否则每次 bump 都要回来改测试。
    sp.prompt_version = RIDDLE_PROMPT_VERSION
    sp.quality_policy_version = QUALITY_POLICY_VERSION
    sp.metrics = {"generation_attempts": 2, "generation_latency_ms": 4123,
                  "review_calls": 1, "review_decision": "fix",
                  "review_issues": ["第一人称"], "rewrite_count": 0, "ok": True}
    sp.blueprint_specified = True
    d.engine.submit_riddle(sp.puzzle, sp.answer, ["h"], spec=sp)
    d._archive_reveal({"puzzle": sp.puzzle, "answer": sp.answer,
                       "reason": "giveup", "winner": "",
                       "solve_atoms": [], "fair_clues": [], "spec": sp}, "揭晓")

    rec = json.loads(io.open(out, encoding="utf-8").read().strip())
    # ---- §34 的字段 ----
    for k in ("spec_version", "prompt_version", "quality_policy_version",
              "blueprint", "signature", "puzzle", "answer", "facts",
              "solve_atoms", "fair_clues", "hints", "qa", "winner",
              "reason", "metrics"):
        check(f"archive 有 {k}", k in rec, sorted(rec))
    check("spec_version=4", rec.get("spec_version") == 4, rec.get("spec_version"))
    check("prompt_version 落盘",
          rec.get("prompt_version") == RIDDLE_PROMPT_VERSION, rec)
    check("policy_version 落盘",
          rec.get("quality_policy_version") == QUALITY_POLICY_VERSION, rec)
    check("facts 落盘", len(rec.get("facts") or []) == 1, rec.get("facts"))
    check("blueprint 落盘",
          rec.get("blueprint", {}).get("mechanism_family") == "hidden_function",
          rec.get("blueprint"))
    check("标出 blueprint 是真分配的", rec.get("blueprint_specified") is True, rec)
    check("标出 signature 存在", rec.get("signature_present") is True, rec)

    # ---- §35 的指标 ----
    m = rec.get("metrics", {})
    check("metrics.generation_attempts", m.get("generation_attempts") == 2, m)
    check("metrics.generation_latency_ms", m.get("generation_latency_ms") == 4123, m)
    check("metrics.review_calls", m.get("review_calls") == 1, m)
    check("metrics.review_decision", m.get("review_decision") == "fix", m)
    check("metrics.review_issues", m.get("review_issues") == ["第一人称"], m)
    check("metrics.generated=True", m.get("generated") is True, m)
    for k in ("hint_calls", "question_count", "answered_count",
              "duration_ms", "judge_calls", "answer_calls",
              "solution_candidate_count", "unavailable_count"):
        check(f"metrics 有 {k}", k in m, sorted(m))


def test_archive_metrics_survive_missing_spec():
    """Q7: 兜底题(没有 spec)落盘时不能炸 —— 指标段整体缺省即可。

    "没有生成指标"本身就是有用信息: 一眼看出这题不是生成出来的。
    """
    import io
    import json
    import os
    import tempfile
    from director import Director

    cfg = mkcfg()
    out = os.path.join(tempfile.gettempdir(), "_hgt_arch_test2.jsonl")
    if os.path.exists(out):
        os.remove(out)
    cfg.puzzle_out_path = out

    d = Director(cfg)
    d.engine.start()
    d.engine.submit_riddle("兜底谜面。为什么?", "兜底谜底。", ["h"])
    # spec=None 是"真的什么 spec 都没有"的极端情况(老 archive / 手工调用),
    # 现在真实兜底路径传的是结构化 spec —— 那条路见下面
    # test_archive_fallback_does_not_inherit_previous_metrics。
    d._archive_reveal({"puzzle": "兜底谜面。为什么?", "answer": "兜底谜底。",
                       "reason": "giveup", "winner": "", "spec": None}, "揭晓")
    rec = json.loads(io.open(out, encoding="utf-8").read().strip())
    m = rec.get("metrics", {})
    check("没有 spec 也能落盘", bool(rec.get("puzzle")), rec.get("puzzle"))
    check("generated=False 标出这是兜底题", m.get("generated") is False, m)
    check("生成指标缺省为 0", m.get("generation_attempts") == 0, m)
    check("问答指标仍然有", "question_count" in m, sorted(m))


def test_archive_fallback_does_not_inherit_previous_metrics():
    """Q7 blocker(第三轮 review): 兜底题不能继承上一题的生成指标。

    真实路径:
        第 10 题生成成功 -> _current_spec(旧实现) = 第10题
        第 11 题连续生成失败 -> Engine 内部走**结构化兜底**,
                              Director 侧的"最近一次生成"没被更新
        揭晓第 11 题 -> payload 是第11题兜底 spec, 但指标若去读
                       Director 侧的状态, 读到的还是第10题的
        于是第 11 题被记成 generated=true / attempts=2 —— 全错。

    现在 archive 只相信 REVEAL payload 里的 spec, 所以这里必须
    落成 generated=false / generation_attempts=0。
    """
    import io
    import json
    import os
    import tempfile
    from director import Director
    from story.puzzle import PuzzleSpec

    cfg = mkcfg()
    out = os.path.join(tempfile.gettempdir(), "_hgt_arch_test3.jsonl")
    if os.path.exists(out):
        os.remove(out)
    cfg.puzzle_out_path = out

    d = Director(cfg)
    d.engine.start()
    # 第 10 题: 生成成功, 指标非空
    good = PuzzleSpec.from_dict({"puzzle": "第十题。为什么?", "answer": "底",
                                 "facts": [{"id": "f1", "text": "事实一",
                                            "kind": "core"}],
                                 "solve_atoms": [
                                     {"id": "a1", "role": "cause", "text": "c",
                                      "fact_ids": ["f1"]},
                                     {"id": "a2", "role": "mechanism",
                                      "text": "m", "fact_ids": ["f1"]}],
                                 "fair_clues": [{"quote": "第十题",
                                                 "supports_atoms": ["a1"]}]})
    good.metrics = {"generation_attempts": 2, "generation_latency_ms": 900,
                    "review_calls": 1, "review_decision": "fix",
                    "review_issues": [], "rewrite_count": 0, "ok": True}
    good.blueprint_specified = True
    d._archive_reveal({"puzzle": good.puzzle, "answer": good.answer,
                       "reason": "giveup", "winner": "", "spec": good}, "揭晓")

    # 第 11 题: 走**结构化兜底**(Engine 内部造的, 没有任何生成指标)
    fallback = PuzzleSpec.from_dict({
        "puzzle": "兜底题。为什么?", "answer": "兜底底",
        "facts": [{"id": "f1", "text": "退潮礁石露出", "kind": "core"}],
        "solve_atoms": [{"id": "a1", "role": "cause", "text": "c",
                         "fact_ids": ["f1"]},
                        {"id": "a2", "role": "mechanism", "text": "m",
                         "fact_ids": ["f1"]}],
        "fair_clues": [{"quote": "兜底题", "supports_atoms": ["a1"]}]})
    d._archive_reveal({"puzzle": fallback.puzzle, "answer": fallback.answer,
                       "reason": "giveup", "winner": "", "spec": fallback},
                      "揭晓")

    rows = [json.loads(x) for x in
            io.open(out, encoding="utf-8").read().strip().splitlines()]
    check("落了两题", len(rows) == 2, len(rows))
    m10, m11 = rows[0]["metrics"], rows[1]["metrics"]
    check("第 10 题有生成指标", m10.get("generation_attempts") == 2, m10)
    check("第 11 题**没有**继承第 10 题的指标(核心断言)",
          m11.get("generation_attempts") == 0, m11)
    check("第 11 题 generated=False", m11.get("generated") is False, m11)
    check("第 11 题没继承 review_decision",
          m11.get("review_decision") == "", m11)
    check("第 11 题没继承 rewrite_count",
          m11.get("rewrite_count") == 0, m11)
    check("第 10 题标了 blueprint_specified",
          rows[0].get("blueprint_specified") is True, rows[0])
    check("第 11 题 blueprint_specified=False",
          rows[1].get("blueprint_specified") is False, rows[1])


def test_full_fallback_chain_reaches_archive():
    """P0 端到端(第三轮 review): 真实兜底链跑到**落盘**都不能炸。

    这条链此前没有任何测试覆盖:
        连续出题失败 -> Engine 自动结构化兜底 -> ANSWER payload
        -> REVEAL payload -> _archive_reveal -> json 落盘

    早先它会在最后一步 `TypeError`, 而落盘失败会**停引擎** ——
    也就是说"出题全挂"这种本来就糟的情况会进一步把直播停掉。
    """
    import io as _io
    import json
    import os
    import tempfile
    from director import Director

    cfg = mkcfg(riddle_max_attempts=2)
    out = os.path.join(tempfile.gettempdir(), "_hgt_fallback_chain.jsonl")
    if os.path.exists(out):
        os.remove(out)
    cfg.puzzle_out_path = out

    d = Director(cfg)
    d.engine.start()
    # 真实路径: 连续失败 -> Engine 内部结构化兜底
    d.engine.submit_riddle(None, error="网关挂了")
    d.engine.submit_riddle(None, error="还是挂了")
    check("兜底题已上屏", d.engine.phase == Phase.QA, d.engine.phase)

    # 观众问一条 -> ANSWER payload 也要能吃下兜底题的 atoms
    d.engine.submit_danmaku("u1", "甲", "#和钟有关吗")
    acts = d.engine.tick()
    ans = [a for a in acts if a.kind == ActionKind.ANSWER]
    check("派发了 ANSWER", len(ans) == 1, kinds(acts))
    if ans:
        sa = ans[0].payload.get("solve_atoms")
        check("ANSWER payload 的 atoms 是 dict 列表",
              sa is None or all(isinstance(a, dict) for a in sa),
              [type(a).__name__ for a in (sa or [])])

    # 走真实揭晓 -> 落盘
    rev = d.engine._enter_revealing_locked(0.0, "giveup", "")
    payload = [a for a in rev if a.kind == ActionKind.REVEAL][0].payload
    d._archive_reveal(payload, "揭晓文案")
    check("落盘没有抛异常(核心断言)", os.path.exists(out), out)
    rec = json.loads(_io.open(out, encoding="utf-8").read().strip())
    check("archive 有 solve_atoms",
          len(rec.get("solve_atoms") or []) >= 2, rec.get("solve_atoms"))
    check("archive 有 fair_clues",
          len(rec.get("fair_clues") or []) >= 1, rec.get("fair_clues"))
    check("atoms 是 dict 不是对象",
          all(isinstance(a, dict) for a in rec["solve_atoms"]),
          [type(a).__name__ for a in rec["solve_atoms"]])
    check("兜底题标为未生成", rec["metrics"].get("generated") is False,
          rec["metrics"])
    check("兜底题 blueprint_specified=False",
          rec.get("blueprint_specified") is False, rec.get("blueprint_specified"))


def test_archive_records_review_latency_total():
    """Q7(第三轮 review): 审稿耗时是**累计**值, 不是"最后一次"。"""
    import io
    import json
    import os
    import tempfile
    from director import Director
    from story.puzzle import PuzzleSpec

    cfg = mkcfg()
    out = os.path.join(tempfile.gettempdir(), "_hgt_arch_test4.jsonl")
    if os.path.exists(out):
        os.remove(out)
    cfg.puzzle_out_path = out

    d = Director(cfg)
    d.engine.start()
    sp = PuzzleSpec.from_dict({
        "puzzle": "题。为什么?", "answer": "底",
        "facts": [{"id": "f1", "text": "事实一事实", "kind": "core"}],
        "solve_atoms": [{"id": "a1", "role": "cause", "text": "c",
                         "fact_ids": ["f1"]},
                        {"id": "a2", "role": "mechanism", "text": "m",
                         "fact_ids": ["f1"]}],
        "fair_clues": [{"quote": "题。为什么?", "supports_atoms": ["a1"]}]})
    sp.metrics = {"review_calls": 3, "review_latency_ms_total": 4200}
    d._archive_reveal({"puzzle": sp.puzzle, "answer": sp.answer,
                       "reason": "giveup", "winner": "", "spec": sp}, "揭晓")
    m = json.loads(io.open(out, encoding="utf-8").read().strip())["metrics"]
    check("有 review_latency_ms_total", m.get("review_latency_ms_total") == 4200, m)
    check("有 review_calls 可算均值", m.get("review_calls") == 3, m)


def test_fallback_atoms_are_dicts_not_dataclasses():
    """P0(第三轮 review): Engine 内部协议必须是 `list[dict]`。

    结构化兜底传进来的是 `SolveAtom/FairClue` **对象**, 而 AI 生成路径
    传的是 dict。两种类型混在一条链上会同时坏两件事:

      ① judge() 的 `isinstance(a, dict)` 判不出 role -> "必须同时命中
         cause + mechanism" 的代码层 gate 被静默跳过, 兜底题又绕回宽判;
      ② REVEAL payload 带 dataclass 对象落盘 -> json.dumps 直接
         TypeError, "出题全挂"时把落盘也一起带崩。

    这个边界必须收口, 见 engine 的 `_atom_dict/_clue_dict`。
    """
    from story import parser as P
    clk = FakeClock()
    eng = RoundEngine(mkcfg(riddle_max_attempts=2), clock=clk)
    eng.start()
    eng.submit_riddle(None, error="挂了")
    eng.submit_riddle(None, error="又挂了")
    check("进了 QA", eng.phase == Phase.QA, eng.phase)
    check("兜底题的 atoms 是 dict",
          eng._solve_atoms and all(isinstance(a, dict)
                                   for a in eng._solve_atoms),
          [type(a).__name__ for a in eng._solve_atoms])
    check("兜底题的 clues 是 dict",
          eng._fair_clues and all(isinstance(c, dict)
                                  for c in eng._fair_clues),
          [type(c).__name__ for c in eng._fair_clues])
    a0 = eng._solve_atoms[0] if eng._solve_atoms else None
    check("dict 里带 role",
          isinstance(a0, dict) and a0.get("role") in ("cause", "mechanism"),
          a0)


def test_fallback_reveal_payload_is_json_serializable():
    """P0: 兜底题走到揭晓时, REVEAL payload 必须能 json.dumps。

    这是真实路径: 出题连挂 -> 结构化兜底 -> 揭晓 -> 落盘。
    早先这里会 `TypeError: Object of type SolveAtom is not JSON
    serializable`, 而落盘失败会**停引擎**。
    """
    import json
    clk = FakeClock()
    eng = RoundEngine(mkcfg(riddle_max_attempts=2), clock=clk)
    eng.start()
    eng.submit_riddle(None, error="挂了")
    eng.submit_riddle(None, error="又挂了")
    acts = eng._enter_revealing_locked(clk.t, "giveup", "")
    rev = [a for a in acts if a.kind == ActionKind.REVEAL]
    check("产出了 REVEAL", len(rev) == 1, kinds(acts))
    if not rev:
        return
    pl = rev[0].payload
    # 模拟 _archive_reveal 那一步
    err = ""
    try:
        json.dumps({k: pl.get(k) for k in
                    ("solve_atoms", "fair_clues", "puzzle", "answer")},
                   ensure_ascii=False)
    except TypeError as e:
        err = str(e)
    check("REVEAL payload 可 JSON 序列化", not err, err)
    check("solve_atoms 落在 payload 里是 dict 列表",
          all(isinstance(a, dict) for a in (pl.get("solve_atoms") or [])),
          pl.get("solve_atoms"))


def test_fallback_judge_gate_actually_works():
    """P0: 兜底题也要走**正式 atom gate** —— 只命中 cause 不算通关。

    这正是"把兜底结构化"的**目的**。早先因为类型没统一, judge 认不出
    role, 这个 gate 在兜底路径上被静默跳过。
    """
    from story.llm import _norm_atoms
    from story import parser as P
    sp = P.fallback_spec(0)
    atoms = [_a for _a in (sp.solve_atoms or [])]
    norm = _norm_atoms(atoms)
    check("归一后带 cause", any(a["role"] == "cause" for a in norm), norm)
    check("归一后带 mechanism",
          any(a["role"] == "mechanism" for a in norm), norm)
    check("都是 dict", all(isinstance(a, dict) for a in norm), norm)


def test_hint_slot_not_consumed_on_failure():
    """P1(第三轮 review): 提示生成失败**不该消耗**槽位。

    早先 `_hints_given` 在**发出请求**时就 +1, 而 worker 失败时什么都不
    提交 -> 那一格永远补不回来, 观众少一条提示。
    (Q6 之后 hint() 在"三次全泄底"时也返回 None, 这条路径因此变成
    真实可达, 不再是理论问题。)
    """
    clk = FakeClock()
    eng = RoundEngine(mkcfg(hint_seconds=45.0, restate_seconds=999999,
                            hint_retry_seconds=15.0), clock=clk)
    eng.start()
    eng.submit_riddle("谜面。为什么?", "底", ["a", "b", "c"])
    clk.advance(46)
    check("到点派出提示", [a for a in eng.tick() if a.kind == ActionKind.HINT],
          kinds(eng.tick()))
    check("槽位还没消耗", eng._hints_given == 0, eng._hints_given)
    # worker 失败
    eng.submit_hint(None, error="连续生成的提示都存在泄底风险")
    check("失败后槽位仍未被消耗", eng._hints_given == 0, eng._hints_given)
    check("在途标志已清", eng._hint_pending is False, eng._hint_pending)
    # 退避期内不重试(防失败风暴)
    clk.advance(5)
    check("退避期内不重试",
          [a for a in eng.tick() if a.kind == ActionKind.HINT] == [])
    # 退避结束 -> 重试同一格
    clk.advance(11)
    check("退避结束后重试同一格",
          [a for a in eng.tick() if a.kind == ActionKind.HINT] != [])
    # 这次成功
    eng.submit_hint("第一条提示")
    check("成功后槽位 +1", eng._hints_given == 1, eng._hints_given)


def test_hint_success_still_advances():
    """P1: 成功的提示照常推进计数与时间轴(不能因为改了语义就不走了)。"""
    clk = FakeClock()
    eng = RoundEngine(mkcfg(hint_seconds=45.0, restate_seconds=999999),
                      clock=clk)
    eng.start()
    eng.submit_riddle("谜面。为什么?", "底", ["a", "b", "c"])
    for lvl in (1, 2, 3):
        clk.advance(45)
        h = [a for a in eng.tick() if a.kind == ActionKind.HINT]
        check(f"第 {lvl} 条派出", len(h) == 1, kinds(h))
        eng.submit_hint(f"提示{lvl}")
        check(f"第 {lvl} 条计数", eng._hints_given == lvl, eng._hints_given)
    check("三条都给过", len(eng._hints_shown) == 3, eng._hints_shown)


def test_hint_repeat_also_backs_off():
    """P1(第四轮 review): "安全但重复"的提示也要退避, 不能立刻重发。

    冷场时 Writer 三次都生成"安全但重复"的提示 -> 返回 `last_safe`,
    它与上一条完全相同。早先 Engine 直接 `return []` —— pending 清了
    但**没设退避**, 下一次 tick 立刻再发 HINT。tick 是 4Hz, 于是变成
    每秒 4 次重复的 LLM 请求(冷场本身就意味着这种情况会持续发生)。
    """
    clk = FakeClock()
    eng = RoundEngine(mkcfg(hint_seconds=45.0, restate_seconds=999999,
                            hint_retry_seconds=15.0), clock=clk)
    eng.start()
    eng.submit_riddle("谜面。为什么?", "底", ["a", "b", "c"])
    clk.advance(46)
    check("第一次派出", [a for a in eng.tick() if a.kind == ActionKind.HINT])
    eng.submit_hint("看他的方向")
    check("第一条上屏", eng._hints_given == 1, eng._hints_given)
    # 下一条时间点到 —— 但 worker 给回一条**与上一条相同**的提示
    clk.advance(45)
    check("第二次派出", [a for a in eng.tick() if a.kind == ActionKind.HINT])
    check("在途", eng._hint_pending is True, eng._hint_pending)
    acts = eng.submit_hint("看他的方向")
    check("重复不上屏", acts == [], acts)
    check("重复不消耗槽位", eng._hints_given == 1, eng._hints_given)
    check("pending 已清", eng._hint_pending is False, eng._hint_pending)
    check("重复也设了退避",
          eng._hint_retry_at > clk.t, eng._hint_retry_at - clk.t)
    # 关键: 退避期内 tick 不再产生 HINT(否则就是重复请求风暴)
    check("退避期内不重发",
          [a for a in eng.tick() if a.kind == ActionKind.HINT] == [])
    clk.advance(13)
    check("仍在退避", [a for a in eng.tick()
                       if a.kind == ActionKind.HINT] == [])
    clk.advance(3)
    check("退避结束后重试",
          [a for a in eng.tick() if a.kind == ActionKind.HINT] != [])


def test_hint_never_stuck_pending_on_failure():
    """P1: 失败回调必须让 pending 归位 —— 否则本题提示永久停摆。

    这是 Engine 侧的契约, Director 侧见 test_director_hint_*。
    """
    clk = FakeClock()
    eng = RoundEngine(mkcfg(hint_seconds=45.0, restate_seconds=999999,
                            hint_retry_seconds=15.0), clock=clk)
    eng.start()
    eng.submit_riddle("谜面。为什么?", "底", ["a", "b", "c"])
    clk.advance(46)
    eng.tick()
    check("在途", eng._hint_pending is True, eng._hint_pending)
    # writer 三次全泄底 -> hint() 返回 (None, err)
    eng.submit_hint(None, error="连续生成的提示都存在泄底风险")
    check("pending 归位", eng._hint_pending is False, eng._hint_pending)
    clk.advance(16)
    check("退避后还能再派发",
          [a for a in eng.tick() if a.kind == ActionKind.HINT] != [],
          kinds(eng.tick()))


class _FlakyHintWriter:
    """假 Writer: hint() 按脚本返回 (text, err), 或抛异常。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def hint(self, puzzle, answer, level, given=None, spec=None,
             touched_fact_ids=None, focus=None):
        self.calls += 1
        item = self.script.pop(0) if self.script else ("兜底提示", None)
        if isinstance(item, Exception):
            raise item
        return item


def _director_hint_round(writer):
    """建一个 Director, 进入 QA 并派发一次 HINT, 返回 (d, eng, clk)。"""
    from director import Director
    clk = FakeClock()
    # no_llm=False: 要走真实 writer.hint 分支(`no_llm=True` 会直接
    # 走 `_fake_hint`, 那正是绕过这个集成断点的原因)。
    cfg = mkcfg(no_llm=False, hint_seconds=45.0, restate_seconds=999999,
                hint_retry_seconds=15.0)
    d = Director(cfg)
    # Director 内部会自建 RoundEngine(真实时钟), 测试要的是 FakeClock,
    # 所以整个换掉 —— 这也正是我们想测的: Director._hint 与 Engine
    # 之间的**回调契约**, 而不是 Engine 内部。
    eng = RoundEngine(cfg, clock=clk)
    d.engine = eng
    d.writer = writer
    eng.start()
    eng.submit_riddle("谜面。为什么?", "底", ["a", "b", "c"])
    clk.advance(46)
    acts = eng.tick()
    hint_acts = [a for a in acts if a.kind == ActionKind.HINT]
    check("派发了 HINT", len(hint_acts) == 1, kinds(acts))
    # `_hint` 会把 work() 丢进后台线程。测试要断言线程**跑完之后**的
    # 状态, 所以这里把 Thread 换成"同步执行"版本 —— 否则断言会跑在
    # 回调之前, 变成随机失败(而且掩盖真实 bug)。
    import director as _D
    real_thread = _D.threading.Thread

    class _InlineThread:
        def __init__(self, target=None, daemon=None, name=None, **kw):
            self._target = target

        def start(self):
            if self._target:
                self._target()

    _D.threading.Thread = _InlineThread
    try:
        d._hint(hint_acts[0].payload)
    finally:
        _D.threading.Thread = real_thread
    return d, eng, clk, hint_acts[0]


def test_director_hint_none_clears_pending():
    """P0(第四轮 review): writer.hint 返回 None/err 时, Director
    **必须**回调 Engine —— 否则 `_hint_pending` 永远挂着 True,
    本题后续提示永久不再派发。

    早先 Director 只有 `if text:` 才回调, 这条路径是真实可达的:
    Q6 之后 hint() 在"三次全泄底"时返回 (None, err)。
    """
    w = _FlakyHintWriter([(None, "连续生成的提示都存在泄底风险")])
    d, eng, clk, _ = _director_hint_round(w)
    check("Writer 被调用了", w.calls == 1, w.calls)
    check("pending 已清(不再永久挂起)",
          eng._hint_pending is False, eng._hint_pending)
    check("退避已设置", eng._hint_retry_at > clk.t, eng._hint_retry_at - clk.t)
    check("槽位未消耗", eng._hints_given == 0, eng._hints_given)
    # 退避期内不再派发
    check("退避期内不重发",
          [a for a in eng.tick() if a.kind == ActionKind.HINT] == [])
    # 退避结束后能再次产生 HINT —— 提示链没死
    clk.advance(16)
    again = [a for a in eng.tick() if a.kind == ActionKind.HINT]
    check("退避后能再次派发 HINT", len(again) == 1, kinds(eng.tick()))


def test_director_hint_exception_clears_pending():
    """P0: writer 抛异常时同样要回调 —— 异常路径也要清 pending。"""
    w = _FlakyHintWriter([RuntimeError("网关 500")])
    d, eng, clk, _ = _director_hint_round(w)
    check("异常后 pending 已清",
          eng._hint_pending is False, eng._hint_pending)
    check("异常后退避已设置",
          eng._hint_retry_at > clk.t, eng._hint_retry_at - clk.t)
    clk.advance(16)
    check("异常后提示链仍活着",
          [a for a in eng.tick() if a.kind == ActionKind.HINT] != [])


def test_director_hint_success_still_works():
    """回归: 成功路径不能被这次改动弄坏。"""
    w = _FlakyHintWriter([("留意他面朝的方向。", None)])
    d, eng, clk, _ = _director_hint_round(w)
    check("成功上屏", eng._hints_given == 1, eng._hints_given)
    check("pending 已清", eng._hint_pending is False, eng._hint_pending)
    check("退避未设置", eng._hint_retry_at == 0.0, eng._hint_retry_at)


def test_spec_source_defaults_to_live_generate():
    """Q8: 不传 source 时默认 live_generate(老调用点不受影响)。"""
    clk = FakeClock()
    eng = RoundEngine(mkcfg(), clock=clk)
    eng.start()
    eng.submit_riddle("谜面。为什么?", "底", ["a"])
    check("默认来源", eng._spec_source == "live_generate", eng._spec_source)


def test_reveal_contributors_reach_archive():
    """R2: 公开贡献链必须一路走到落盘。

    结构在 Engine 里对, 不代表数据会流过去 —— 这个项目里已经踩过好几
    次"结构齐了、字段没传"(solve_atoms / fair_clues 就是这么恒为空
    数组的)。所以这里跑**真实**链路:

        真人 QA -> _qa_archive + contribution
        -> _enter_revealing_locked -> REVEAL payload
        -> Director._archive_reveal -> json 落盘

    并同时钉住两件事:
      - 公开形状只有 qid/user_name/text/verdict/is_final;
      - 内部 fact ID **绝不**出现在 reveal_contributors 里(它们只在
        qa[] 的 completion_contribution_fact_ids 里, 那是复盘用的)。
    """
    import io as _io
    import json
    import os
    import tempfile
    from director import Director
    from story.puzzle import PuzzleFact, PuzzleSpec

    cfg = mkcfg()
    out = os.path.join(tempfile.gettempdir(), "_hgt_contrib.jsonl")
    if os.path.exists(out):
        os.remove(out)
    cfg.puzzle_out_path = out

    d = Director(cfg)
    # ⚠️ Director 自己**没有** clock(它用 time.time)。这里要用 FakeClock
    # 精确推进"什么时候派发 ANSWER", 所以把注入了时钟的引擎交给它 ——
    # 否则 tick() 会因为墙钟没走动而不派发。
    clk = FakeClock()
    d.engine = RoundEngine(cfg, clock=clk)
    d.engine.start()
    sp = PuzzleSpec(
        id="r2-arch", title="贡献链",
        puzzle="门外站着一个女人, 开门的人一见她就愣住了。为什么?",
        answer="门外女人是父亲的亲生女儿, 她昨晚才与父亲同桌吃饭相认。",
        core_answer="门外女人是父亲的亲生女儿。",
        completion_fact_ids=["f1", "f2"],
        prompt_version="riddle-v7", quality_policy_version="quality-v7",
        facts=[
            PuzzleFact(id="f1", text="她是父亲的亲生女儿", kind="core",
                       visibility="hidden"),
            PuzzleFact(id="f2", text="她昨晚与父亲同桌吃饭", kind="core",
                       visibility="hidden"),
        ])
    d.engine.submit_riddle(sp.puzzle, sp.answer, ["a"], spec=sp, source="pool")
    check("停在 QA", d.engine.phase == Phase.QA, d.engine.phase)

    # 两位真人分别补齐 f1 / f2
    for uid, name, ask, fid in (("u1", "甲", "她是父亲的女儿吗", "f1"),
                                ("u2", "乙", "她昨晚和父亲吃饭了吗", "f2")):
        d.engine.submit_danmaku(uid, name, "#" + ask)
        clk.advance(1.0)
        ans = [a for a in d.engine.tick() if a.kind == ActionKind.ANSWER]
        check(f"派发 ANSWER({name})", len(ans) == 1, kinds(ans))
        p = ans[0].payload
        d.engine.submit_qa(
            [QAResult(qid=p["qid"], verdict="是", established_fact_ids=[fid],
                      # J1-B: 合同内的 id 必须同时被复核确认才能进房间共识。
                      # 这条走的是裸 submit_qa(没经过 `_answer_and_submit`
                      # 的自动补全), 所以要显式给出 —— 模拟 Writer 已经
                      # 正常复核过。
                      completion_verified_fact_ids=[fid])],
            expect_round=p.get("expect_round"),
            expect_spec_key=p.get("expect_spec_key"))

    check("合同补齐 -> REVEALING",
          d.engine.phase == Phase.REVEALING, d.engine.phase)
    # 通关本身已经产出过 REVEAL。这里再显式取一次 payload —— 我们要验的
    # 是 payload -> archive 这一段, 不依赖上一步是否恰好被调用。
    rev = [a for a in d.engine._enter_revealing_locked(0.0, "giveup", "")
           if a.kind == ActionKind.REVEAL]
    check("产出 REVEAL", len(rev) == 1, kinds(rev))
    payload = rev[0].payload if rev else {}
    check("payload 带 reveal_contributors",
          "reveal_contributors" in payload, sorted(payload.keys()))
    contrib = payload.get("reveal_contributors") or []
    check("贡献链有 2 条", len(contrib) == 2, contrib)
    check("公开形状只有五个字段",
          all(set(c.keys()) == {"qid", "user_name", "text", "verdict",
                                "is_final"} for c in contrib),
          [sorted(c.keys()) for c in contrib])

    d._archive_reveal(payload, "揭晓文案")
    check("落盘成功", os.path.exists(out), out)
    rec = json.loads(_io.open(out, encoding="utf-8").read().strip())
    check("archive 顶层有 reveal_contributors",
          "reveal_contributors" in rec, sorted(rec.keys()))
    got = rec.get("reveal_contributors") or []
    check("落盘里是 2 条", len(got) == 2, got)
    check("落盘形状也是公开五字段",
          all(set(c.keys()) == {"qid", "user_name", "text", "verdict",
                                "is_final"} for c in got),
          [sorted(c.keys()) for c in got])
    blob = json.dumps(rec, ensure_ascii=False)
    # reveal_contributors 内部绝不能有 fact ID
    check("公开贡献链里没有 fact 痕迹",
          all("f1" not in (c.get("text") or "")
              and "f2" not in (c.get("text") or "") for c in got),
          got)
    # 而 qa[] 里**应该**有内部归属(复盘要看)
    qa = [r for r in (rec.get("qa") or []) if r.get("kind") == "qa"]
    check("qa[] 记录了 completion_contribution_fact_ids",
          any(r.get("completion_contribution_fact_ids") for r in qa),
          [(r.get("qid"), r.get("completion_contribution_fact_ids"))
           for r in qa])


def test_spec_source_is_recorded_and_reaches_reveal():
    """Q8: 来源要一路进 REVEAL payload —— archive 靠它区分 pool/现场。"""
    clk = FakeClock()
    eng = RoundEngine(mkcfg(), clock=clk)
    eng.start()
    eng.submit_riddle("谜面。为什么?", "底", ["a"], source="pool")
    check("存下来了", eng._spec_source == "pool", eng._spec_source)
    acts = eng._enter_revealing_locked(clk.t, "giveup", "")
    rev = [a for a in acts if a.kind == ActionKind.REVEAL]
    check("产出了 REVEAL", len(rev) == 1, kinds(acts))
    if rev:
        check("payload 带 spec_source",
              rev[0].payload.get("spec_source") == "pool",
              rev[0].payload.get("spec_source"))


def test_fallback_marks_source_as_fallback():
    """Q8: 兜底是**引擎自己**标的第三种来源。

    不让 director 猜: 兜底发生在 director 的 worker 已经返回失败之后,
    是引擎内部的决定。让 director 去预测它 = 并行维护一份"当前是哪道题"
    的状态, 而 `Director._current_spec` 正是上一轮专门删掉的东西。
    """
    clk = FakeClock()
    eng = RoundEngine(mkcfg(riddle_max_attempts=2), clock=clk)
    eng.start()
    eng.submit_riddle(None, error="挂了")
    eng.submit_riddle(None, error="又挂了")
    check("进了 QA(兜底题上屏)", eng.phase == Phase.QA, eng.phase)
    check("来源标记为 fallback", eng._spec_source == "fallback",
          eng._spec_source)
    acts = eng._enter_revealing_locked(clk.t, "giveup", "")
    rev = [a for a in acts if a.kind == ActionKind.REVEAL]
    if rev:
        check("REVEAL 带 fallback",
              rev[0].payload.get("spec_source") == "fallback",
              rev[0].payload.get("spec_source"))


def test_spec_source_resets_between_puzzles():
    """Q8: 开新题必须重置来源, 否则会串题(上一题 pool -> 这一题误报 pool)。"""
    clk = FakeClock()
    eng = RoundEngine(mkcfg(reveal_hold_seconds=1.0), clock=clk)
    eng.start()
    eng.submit_riddle("第一题。为什么?", "底", ["a"], source="pool")
    check("第一题是 pool", eng._spec_source == "pool", eng._spec_source)
    eng._enter_revealing_locked(clk.t, "giveup", "")
    eng.submit_reveal("谜底")
    clk.advance(2)
    eng.tick()
    check("已进入下一题", eng.phase == Phase.SETTING, eng.phase)
    check("来源已重置", eng._spec_source == "live_generate", eng._spec_source)


def test_archive_records_source():
    """Q8: archive 里 source 是显式字段(不从值推断)。"""
    import io as _io
    import json
    import os
    import tempfile
    from director import Director
    from story.puzzle import PuzzleSpec

    out = os.path.join(tempfile.gettempdir(), "_hgt_src_arch.jsonl")
    if os.path.exists(out):
        os.remove(out)
    cfg = mkcfg()
    cfg.puzzle_out_path = out
    d = Director(cfg)
    d.pool = None                      # 这条用例只验 archive 本身
    d.engine.start()
    sp = PuzzleSpec.from_dict({
        "puzzle": "灯塔题。为什么?", "answer": "礁石。",
        "facts": [{"id": "f1", "text": "退潮礁石露出", "kind": "core"}],
        "solve_atoms": [{"id": "a1", "role": "cause", "text": "退潮",
                         "fact_ids": ["f1"]},
                        {"id": "a2", "role": "mechanism", "text": "标礁石",
                         "fact_ids": ["f1"]}],
        "fair_clues": [{"quote": "只在退潮时亮灯", "supports_atoms": ["a1"]}],
        "signature": {"mechanism_family": "hidden_function",
                      "solution_shape": "hidden_function_explains_behavior",
                      "domain": "maritime"},
        "blueprint": {"mechanism_family": "hidden_function",
                      "solution_shape": "hidden_function_explains_behavior",
                      "domain": "maritime"},
    })
    # 绑定当前常量, 别写死版本串 —— 否则每次 bump 都要回来改测试。
    sp.prompt_version = RIDDLE_PROMPT_VERSION
    sp.quality_policy_version = QUALITY_POLICY_VERSION
    d.engine.submit_riddle(sp.puzzle, sp.answer, [], solve_atoms=sp.solve_atoms,
                           fair_clues=sp.fair_clues, spec=sp, source="pool")
    acts = d.engine._enter_revealing_locked(0.0, "giveup", "")
    rev = [a for a in acts if a.kind == ActionKind.REVEAL][0]
    d._archive_reveal(rev.payload, "谜底")
    rec = json.loads(_io.open(out, encoding="utf-8").read().strip())
    check("archive 记了 source=pool", rec.get("source") == "pool",
          rec.get("source"))
    # 老记录(没有 spec_source)应退化为 live_generate, 而不是 None
    d._archive_reveal({"puzzle": "x", "answer": "y"}, "z")
    rec2 = json.loads(_io.open(out, encoding="utf-8").read().strip().split("\n")[-1])
    check("缺字段时退化为 live_generate",
          rec2.get("source") == "live_generate", rec2.get("source"))


# ======================================================================
# Q11: 非 QA 阶段 ACK —— 不再静默吞掉 #问题
# ======================================================================
def _sys_rows(eng):
    """快照里的系统行。`snapshot().qa_log` 是 dict(已 to_json), 不是 QARec。"""
    return [r for r in eng.snapshot().qa_log if r.get("kind") == "system"]


def test_setting_question_gets_ack():
    """SETTING(出题中)收到 #问题 -> 一条系统提示, 不再静默。"""
    print("\n[Q11] SETTING 阶段的 #问题 有反馈")
    clk = FakeClock()
    eng = RoundEngine(mkcfg(), clock=clk)
    eng.start()                                   # -> SETTING
    check("确实在 SETTING", eng.phase == Phase.SETTING, eng.phase)
    # Q12 之后 submit_danmaku 同步处理并返回动作。
    acts = eng.submit_danmaku("u1", "甲", "#他瞎了吗")
    check("产出了动作", bool(acts), acts)
    rows = _sys_rows(eng)
    check("**有 1 条系统行**", len(rows) == 1, [r["text"] for r in rows])
    check("文案是 SETTING 那条",
          rows and "正在准备新题" in rows[0]["text"],
          rows[0]["text"] if rows else None)
    check("kind=system", rows and rows[0]["kind"] == "system")


def test_revealing_question_gets_ack():
    print("\n[Q11] REVEALING 阶段的 #问题 有反馈")
    eng, clk = boot(mkcfg())
    eng._enter_revealing_locked(clk.t, "giveup", "")
    check("确实在 REVEALING", eng.phase == Phase.REVEALING, eng.phase)
    eng.submit_danmaku("u1", "甲", "#到底为什么")
    clk.advance(3)
    eng.tick()
    rows = _sys_rows(eng)
    check("**有 1 条系统行**", len(rows) == 1, [r["text"] for r in rows])
    check("文案是 REVEALING 那条",
          rows and "正在揭晓" in rows[0]["text"],
          rows[0]["text"] if rows else None)


def test_revealed_question_gets_ack():
    print("\n[Q11] REVEALED 阶段的 #问题 有反馈")
    eng, clk = boot(mkcfg())
    eng._enter_revealing_locked(clk.t, "giveup", "")
    eng.submit_reveal("谜底在此")
    check("确实在 REVEALED", eng.phase == Phase.REVEALED, eng.phase)
    eng.submit_danmaku("u1", "甲", "#那这样的话")
    clk.advance(3)
    eng.tick()
    rows = _sys_rows(eng)
    check("**有 1 条系统行**", len(rows) == 1, [r["text"] for r in rows])
    check("文案是 REVEALED 那条",
          rows and "已结束" in rows[0]["text"],
          rows[0]["text"] if rows else None)


def test_idle_and_stopped_get_no_ack():
    """IDLE / STOPPED 不 ACK(还没开始 / 已结束, 反馈没意义), 且不能崩。"""
    print("\n[Q11] IDLE/STOPPED 不 ACK")
    clk = FakeClock()
    eng = RoundEngine(mkcfg(), clock=clk)
    check("初始是 IDLE", eng.phase == Phase.IDLE, eng.phase)
    eng._accept_danmaku("u1", "甲", "#问题", clk.t)
    check("IDLE 无系统行", not _sys_rows(eng), [r["text"] for r in _sys_rows(eng)])
    eng.start()
    eng.stop("测试")
    check("已停止", eng.phase == Phase.STOPPED, eng.phase)
    eng._accept_danmaku("u1", "甲", "#问题", clk.t)
    check("STOPPED 无系统行", not _sys_rows(eng))


def test_ack_is_globally_throttled():
    """全局节流: 同一时刻连发只出一条; 过了窗口又能出。"""
    print("\n[Q11] ACK 全局节流")
    clk = FakeClock()
    eng = RoundEngine(mkcfg(phase_ack_seconds=5.0), clock=clk)
    eng.start()                                   # -> SETTING
    for i in range(3):
        eng._accept_danmaku(f"u{i}", f"观众{i}", "#问题", clk.t)
    check("**连发 3 条只出 1 条系统行**", len(_sys_rows(eng)) == 1,
          len(_sys_rows(eng)))
    # 窗口内再加一条: 仍然只有 1 条
    clk.advance(4)
    eng._accept_danmaku("u9", "晚来的", "#问题", clk.t)
    check("窗口内仍只有 1 条", len(_sys_rows(eng)) == 1, len(_sys_rows(eng)))
    # 越过窗口: 第 2 条出现
    clk.advance(2)
    eng._accept_danmaku("u10", "更晚的", "#问题", clk.t)
    check("**过窗口后出第 2 条**", len(_sys_rows(eng)) == 2,
          len(_sys_rows(eng)))


def test_ack_does_not_touch_stats_or_history():
    """文档硬要求: 系统行**不**计 stat_questions / verdict_counts,
    **不**进 _history(否则会被喂回 LLM)。"""
    print("\n[Q11] ACK 不污染统计与 transcript")
    clk = FakeClock()
    eng = RoundEngine(mkcfg(), clock=clk)
    eng.start()
    before_q = eng.snapshot().stat_questions
    hist_before = len(eng._history)
    eng._accept_danmaku("u1", "甲", "#问题", clk.t)
    check("stat_questions 不变", eng.snapshot().stat_questions == before_q,
          eng.snapshot().stat_questions)
    check("**不进 _history**", len(eng._history) == hist_before,
          len(eng._history))
    check("不进 verdict_counts", not eng._verdict_counts, eng._verdict_counts)
    check("_qa_total 不变", eng._qa_total == 0, eng._qa_total)


def test_ack_does_not_fire_in_qa():
    """QA 阶段行为完全不变: 正常入队, 没有系统行。"""
    print("\n[Q11] QA 阶段行为不变")
    eng, clk = boot(mkcfg())
    eng.submit_danmaku("u1", "甲", "#他瞎了吗")
    clk.advance(3)
    eng.tick()
    check("**QA 里没有系统行**", not _sys_rows(eng), [r["text"] for r in _sys_rows(eng)])
    check("提问正常入队", len(eng._pending) + len(eng._inflight) == 1,
          (len(eng._pending), len(eng._inflight)))


def test_ack_does_not_hijack_next_or_hint():
    """#下一题 / #提示 的分支在自己的路径上提前 return, 不被 ACK 截胡。"""
    print("\n[Q11] #下一题/#提示 不被 ACK 截胡")
    eng, clk = boot(mkcfg())
    eng.submit_danmaku("u1", "甲", "#下一题")
    clk.advance(3)
    eng.tick()
    check("**#下一题 仍然生效(进入 REVEALING)**",
          eng.phase == Phase.REVEALING, eng.phase)
    check("没被当成普通 #问题 出系统行", not _sys_rows(eng))


def test_ack_action_is_pure_broadcast():
    """ACK 是纯状态机: 只出 BROADCAST, 绝不触发 LLM 类动作。"""
    print("\n[Q11] ACK 只出 BROADCAST")
    clk = FakeClock()
    eng = RoundEngine(mkcfg(), clock=clk)
    eng.start()
    acts = eng._phase_ack_locked("甲", "#问题", clk.t)
    check("有动作", bool(acts), acts)
    check("**全是 BROADCAST**",
          all(a.kind == ActionKind.BROADCAST for a in acts), kinds(acts))
    for bad in (ActionKind.ANSWER, ActionKind.HINT, ActionKind.REVEAL,
                ActionKind.RIDDLE):
        check(f"不含 {bad.value}", bad not in kinds(acts), kinds(acts))


def _to_next_setting(eng, clk):
    """把引擎从第 N 题的 QA 推到第 N+1 题的 SETTING。"""
    eng._enter_revealing_locked(0.0, "giveup", "")
    clk.advance(30.0)
    eng.tick(clk.t)
    if eng.phase != Phase.SETTING:
        eng.phase = Phase.SETTING
    return eng.round_index


# ======================================================================
# Step 06 — spec identity / stale worker gate
# ======================================================================
def test_riddle_action_carries_expect_round():
    """RIDDLE 动作必须带上"这一发是为第几题要的"。"""
    print("\n[S06-1] RIDDLE 动作带 expect_round")
    eng, clk = boot(mkcfg())
    _to_next_setting(eng, clk)
    act = eng._riddle_action_locked("riddle")
    check("payload 里有 expect_round", "expect_round" in act.payload, act.payload)
    check("expect_round == 当前 round_index",
          act.payload["expect_round"] == eng.round_index,
          (act.payload.get("expect_round"), eng.round_index))


def test_stale_worker_return_is_discarded():
    """**核心**: 上一题的 worker 迟到 -> 不得写进下一题。

    故障链(修之前真实存在):
        第 N 题 worker 发出(慢)
        引擎超时/异常 -> 回到 SETTING 重试
        第 N+1 题 worker 很快返回 -> 上屏, 进入 QA
        第 N 题 worker 终于返回 -> 若此刻 phase 又是 SETTING(下一题
        已在出题), 旧交付会被当成**新题的**, 于是谜面错、题号却递增。
    """
    print("\n[S06-2] 迟到交付被丢弃")
    eng, clk = boot(mkcfg())
    _to_next_setting(eng, clk)
    cur = eng.round_index
    # 一个"上一题"的迟到交付: expect_round 是更早的题号
    stale = cur - 1
    before = eng._puzzle
    acts = eng.submit_riddle("一道迟到的旧谜面。为什么?", "旧谜底。",
                             ["h1", "h2", "h3"], title="旧的",
                             expect_round=stale)
    check("迟到交付被丢弃(无动作)", acts == [], acts)
    check("阶段没变", eng.phase == Phase.SETTING, eng.phase)
    check("题号没被推进", eng.round_index == cur, (eng.round_index, cur))
    check("当前题面没被旧内容覆盖", eng._puzzle == before, eng._puzzle)


def test_matching_round_is_accepted():
    """回归: 题号匹配的正常交付照常接受(别把门关过头)。"""
    print("\n[S06-3] 题号匹配 -> 正常接受")
    eng, clk = boot(mkcfg())
    _to_next_setting(eng, clk)
    cur = eng.round_index
    acts = eng.submit_riddle("一道正常的新谜面。为什么?", "新谜底。",
                             ["h1", "h2", "h3"], title="新的",
                             expect_round=cur)
    check("被接受", bool(acts), acts)
    check("进入了 QA", eng.phase == Phase.QA, eng.phase)
    check("题号推进", eng.round_index == cur + 1, (eng.round_index, cur))


def test_expect_round_none_keeps_legacy_behavior():
    """`expect_round=None`(老调用方/测试)不做身份校验 —— 兼容保留。"""
    print("\n[S06-4] expect_round=None 保持旧行为")
    eng, clk = boot(mkcfg())
    _to_next_setting(eng, clk)
    acts = eng.submit_riddle("一道不带题号的谜面。为什么?", "谜底。",
                             ["h1", "h2", "h3"], title="无题号")
    check("照常被接受", bool(acts), acts)
    check("进入 QA", eng.phase == Phase.QA, eng.phase)


def test_stale_return_cannot_advance_archive_identity():
    """迟到交付被丢弃后, **指纹/来源/题号**都不能被它改掉。

    这是"跨题 stale 写入"的实际危害面: 若旧交付被接受, archive 会记下
    「第 N+1 题」的题号 + 「第 N 题」的谜面/来源。
    """
    print("\n[S06-5] 迟到交付不改指纹/来源")
    eng, clk = boot(mkcfg())
    _to_next_setting(eng, clk)
    cur = eng.round_index
    sig_before = list(eng._recent_signatures)
    src_before = eng._spec_source
    eng.submit_riddle("迟到的旧谜面。为什么?", "旧谜底。", ["h1", "h2", "h3"],
                      title="旧的", source="pool",
                      signature={"mechanism_family": "hidden_function",
                                 "solution_shape": "hidden_function_explains_behavior"},
                      expect_round=cur - 1)
    check("指纹没被写入", list(eng._recent_signatures) == sig_before,
          eng._recent_signatures)
    check("来源没被改", eng._spec_source == src_before,
          (eng._spec_source, src_before))


def test_runtime_spec_key_is_stable_and_distinguishes():
    """Batch B closeout: 运行时身份要稳定, 且能区分不同题目。"""
    print("\n[S06b] runtime_spec_key 基本性质")
    from story.puzzle import runtime_spec_key
    f = [{"id": "f1", "text": "事实一"}]
    a = [{"id": "a1", "text": "原因"}]
    k1 = runtime_spec_key("谜面", "谜底", f, a, [])
    k2 = runtime_spec_key("谜面", "谜底", f, a, [])
    check("同输入 -> 同 key", k1 == k2, (k1, k2))
    check("换谜面 -> 不同 key",
          runtime_spec_key("别的谜面", "谜底", f, a, []) != k1)
    check("换谜底 -> 不同 key",
          runtime_spec_key("谜面", "别的谜底", f, a, []) != k1)
    check("换 facts -> 不同 key",
          runtime_spec_key("谜面", "谜底", [{"id": "f9", "text": "x"}], a, []) != k1)
    check("换 atoms -> 不同 key",
          runtime_spec_key("谜面", "谜底", f, [{"id": "a9", "text": "y"}], []) != k1)
    check("不抛异常(空输入)", isinstance(runtime_spec_key(), str))


# ======================================================================
# UX-2: 房间共同推理(v5 通关合同)
# ======================================================================
def _v5_spec(completion, core_answer="这是核心答案。",
             facts=None, atoms=None, puzzle=None, answer=None):
    """造一份带 v5 通关合同的 spec。

    `facts` 默认给出 f1/f2 两条 core/hidden, `atoms` 分别引用它们。
    测试只关心"合同覆盖"这条逻辑, 所以结构保持最小。
    """
    from story.puzzle import (PuzzleFact, PuzzleSpec, SolveAtom, FairClue)
    puzzle = puzzle or "门外站着一个女人, 屋里的人开了门就愣住。为什么?"
    answer = answer or "门外女人是父亲的亲生女儿, 昨晚才相认。"
    facts = facts if facts is not None else [
        PuzzleFact(id="f1", text="门外女人是父亲的亲生女儿", kind="core",
                   visibility="hidden"),
        PuzzleFact(id="f2", text="门外女人昨晚与父亲同桌吃饭", kind="core",
                   visibility="hidden"),
    ]
    atoms = atoms if atoms is not None else [
        SolveAtom(id="a1", role="key", text="门外女人是父亲的亲生女儿",
                  fact_ids=["f1"]),
        SolveAtom(id="a2", role="key", text="她昨晚与父亲同桌吃饭",
                  fact_ids=["f2"]),
    ]
    return PuzzleSpec(
        id="vp1", title="门外", puzzle=puzzle, answer=answer,
        core_answer=core_answer, completion_fact_ids=list(completion),
        facts=facts, solve_atoms=atoms,
        fair_clues=[FairClue(quote=puzzle[:6], supports_atoms=["a1"])],
        hints=["注意她是谁", "注意昨晚", "注意饭桌"],
        prompt_version="riddle-v7", quality_policy_version="quality-v7")


def boot_v5(cfg=None, completion=("f1", "f2"), **spec_kw):
    """起一个引擎, 交一道**带通关合同**的题, 停在 QA。"""
    clk = FakeClock()
    # 默认给个**短**的揭晓展示时长: 调用方普遍用 advance(31.0) 跨过
    # "揭晓 -> 下一题", 那测的是别的东西。U1 把默认提到 60s 之后,
    # 31s 跨不过去了 —— 但把它改成 61 会让这些用例看起来在测那个数。
    eng = RoundEngine(cfg or mkcfg(reveal_hold_seconds=5.0), clock=clk)
    eng.start()
    sp = _v5_spec(completion, **spec_kw)
    eng.submit_riddle(sp.puzzle, sp.answer, list(sp.hints), spec=sp)
    assert eng.phase == Phase.QA, eng.phase
    return eng, clk, sp


def _answer_and_submit(eng, clk, uid, name, text, **kw):
    """发一条'#提问' -> 拿 ANSWER 动作 -> 按 kw 回一个 QAResult。

    ## J1-B: completion id 必须**同时**带 verified

    Engine 现在要求: 落在合同里的 fact 只有同时出现在
    `completion_verified_fact_ids` 里才能进房间共识(J1-B 的
    defense-in-depth)。这些用例测的是**通关/贡献链/落盘**这些别的
    东西, 不是"未复核的 completion 能不能蒙混过关"(那条有专门的
    J1 regression)。

    所以这里默认把 `established_fact_ids` 里的合同 id 自动补一份到
    `completion_verified_fact_ids` —— 模拟"Writer 已经正常复核过"。
    **不要**在这里无条件复制全部 id: 那样就测不出 J1-B 了。
    需要测"未复核"的调用方显式传 `completion_verified_fact_ids=[...]`
    (传空列表也算显式, 不会被这里覆盖)。
    """
    acts = [a for a in say(eng, clk, uid, name, "#" + text)
            if a.kind == ActionKind.ANSWER]
    assert acts, "没有派发 ANSWER"
    p = acts[0].payload
    if "completion_verified_fact_ids" not in kw:
        contract = set(eng._completion_fact_ids or ())
        est = [str(x) for x in (kw.get("established_fact_ids") or [])]
        kw["completion_verified_fact_ids"] = [
            x for x in est if x in contract or not contract]
    return eng.submit_qa([QAResult(qid=p["qid"], **kw)],
                         expect_round=p.get("expect_round"),
                         expect_spec_key=p.get("expect_spec_key")), p


def test_ux_a_collective_solve():
    """Case A: 集体身份题 —— 房间拼齐即通关, 不要求同一人复述。

    已有真人 QA 建立了 f2(她昨晚与父亲同桌吃饭), 当前真人只说
    "她是姐姐" 建立 f1(她的身份)。这句话**没有**"因为/所以",
    也**没有**复述机制, 仍必须立即揭晓。
    """
    print("\n[UX-A] 集体身份题: 补齐缺口即通关")
    eng, clk, sp = boot_v5()
    # 第一位观众建立 f2
    _, _ = _answer_and_submit(eng, clk, "u1", "甲", "她昨晚和父亲吃饭了吗",
                              verdict="是", established_fact_ids=["f2"])
    check("只覆盖 1/2 -> 仍在 QA", eng.phase == Phase.QA, eng.phase)
    check("已积累 f2", eng._established_fact_ids == {"f2"},
          eng._established_fact_ids)
    # 第二位观众建立 f1 -> 合同覆盖
    acts, _ = _answer_and_submit(eng, clk, "u2", "乙", "她是姐姐",
                                 verdict="是", established_fact_ids=["f1"])
    check("补齐缺口 -> REVEALING", eng.phase == Phase.REVEALING, eng.phase)
    check("胜者是补齐者(乙)", eng._solved_by == "乙", eng._solved_by)
    check("确实判为 solved", eng._solved, eng._solved)
    rev = [a for a in acts if a.kind == ActionKind.REVEAL]
    check("产生了 REVEAL 动作", len(rev) == 1, kinds(acts))
    check("REVEAL 带上 core_answer",
          rev and rev[0].payload.get("core_answer") == sp.core_answer,
          rev[0].payload if rev else None)


def test_ux_b_second_fact_completes():
    """Case B: 飞行测试 —— 第二条补齐时直接揭晓。

    不得要求观众说"机长如何松口气 + 数据合格 + 塔台事先知道"那一串。
    """
    print("\n[UX-B] 飞行测试: 第二条补齐直接揭晓")
    from story.puzzle import PuzzleFact, SolveAtom, FairClue, PuzzleSpec
    sp = PuzzleSpec(
        id="fly", title="测试飞行",
        puzzle="飞机落地后机长长舒一口气, 乘客却在鼓掌。为什么?",
        answer="这是一次考核飞行, 复飞本就是测试项目。",
        core_answer="这是一次预设的测试飞行, 复飞本身就是考核项目。",
        completion_fact_ids=["f1", "f2"],
        facts=[PuzzleFact(id="f1", text="这是测试/考核飞行", kind="core"),
               PuzzleFact(id="f2", text="复飞本来就是测试项目", kind="core")],
        solve_atoms=[SolveAtom(id="a1", role="key", text="这是一次测试飞行",
                               fact_ids=["f1"]),
                     SolveAtom(id="a2", role="key", text="复飞是考核项目",
                               fact_ids=["f2"])],
        fair_clues=[FairClue(quote="乘客却在鼓掌", supports_atoms=["a1"])],
        hints=["注意掌声", "注意民航流程", "注意考核"],
        prompt_version="riddle-v7", quality_policy_version="quality-v7")
    clk = FakeClock()
    eng = RoundEngine(mkcfg(), clock=clk)
    eng.start()
    eng.submit_riddle(sp.puzzle, sp.answer, list(sp.hints), spec=sp)
    _answer_and_submit(eng, clk, "u1", "甲", "这是测试飞行吗",
                       verdict="是", established_fact_ids=["f1"])
    check("第一条不够", eng.phase == Phase.QA, eng.phase)
    _, _ = _answer_and_submit(eng, clk, "u2", "乙", "复飞是考核项目",
                              verdict="是", established_fact_ids=["f2"])
    check("第二条补齐 -> 揭晓", eng.phase == Phase.REVEALING, eng.phase)
    check("胜者=乙", eng._solved_by == "乙", eng._solved_by)


def test_ux_c_touched_is_not_established():
    """Case C: touched 绝不能冒充 established。

    问"她和父亲有关系吗"答"是" -> touched=[f1], established=[]。
    绝不能通关。
    """
    print("\n[UX-C] touched 不能冒充 established")
    eng, clk, sp = boot_v5()
    _answer_and_submit(eng, clk, "u1", "甲", "她和父亲有关系吗",
                       verdict="是", touched_fact_ids=["f1"],
                       established_fact_ids=[])
    check("touched 记下了", eng._touched_fact_ids == {"f1"},
          eng._touched_fact_ids)
    check("established 仍为空", eng._established_fact_ids == set(),
          eng._established_fact_ids)
    check("未通关", eng.phase == Phase.QA, eng.phase)


def test_ux_d_illegal_fact_id_dropped():
    """Case D: 模型回不存在的 id -> Engine 丢掉。"""
    print("\n[UX-D] 非法 fact id 被丢弃")
    eng, clk, sp = boot_v5()
    _answer_and_submit(eng, clk, "u1", "甲", "随便猜",
                       verdict="是", established_fact_ids=["f999", "f1"])
    check("f999 被丢, f1 留下",
          eng._established_fact_ids == {"f1"}, eng._established_fact_ids)
    check("未通关(只覆盖 1/2)", eng.phase == Phase.QA, eng.phase)


def test_ux_m_human_only_established():
    """Case M: 只有真人 submit_qa 能累积; 提示/nudge 不能。

    并在代码里冻结: 将来 Detective 的 submit path 绝不能写这个集合。
    """
    print("\n[UX-M] established 只能由真人 QA 建立")
    eng, clk, sp = boot_v5()
    _answer_and_submit(eng, clk, "u1", "甲", "第一条",
                       verdict="是", established_fact_ids=["f1"])
    before = set(eng._established_fact_ids)
    check("真人 QA 建立了 f1", before == {"f1"}, before)
    # 提示回调不得增加
    eng.submit_hint("想想她是谁")
    check("submit_hint 不增加", eng._established_fact_ids == before,
          eng._established_fact_ids)
    # 未判定不得增加
    acts = [a for a in say(eng, clk, "u2", "乙", "#再猜")
            if a.kind == ActionKind.ANSWER]
    eng.submit_qa([QAResult(qid=acts[0].payload["qid"],
                            verdict="未判定", status="unavailable",
                            established_fact_ids=["f2"])])
    check("技术失败不建立任何事实",
          eng._established_fact_ids == before, eng._established_fact_ids)
    # 直接调用具名方法必须存在(边界显式可查)
    check("存在具名写入口 _record_human_established_locked",
          hasattr(eng, "_record_human_established_locked"))


def test_ux_new_puzzle_clears_established():
    """每道新题必须清零 established, 否则新题可能一上来就白送揭晓。"""
    print("\n[UX-new] 新题清零房间共识")
    eng, clk, sp = boot_v5()
    _answer_and_submit(eng, clk, "u1", "甲", "第一条",
                       verdict="是", established_fact_ids=["f1"])
    check("先有 f1", eng._established_fact_ids == {"f1"},
          eng._established_fact_ids)
    eng._enter_revealing_locked(clk.t, "giveup", "")
    eng.submit_reveal("揭晓")
    clk.advance(31.0)
    eng.tick(clk.t)
    sp2 = _v5_spec(("f1", "f2"))
    eng.submit_riddle(sp2.puzzle, sp2.answer, list(sp2.hints), spec=sp2)
    check("新题 established 清零", eng._established_fact_ids == set(),
          eng._established_fact_ids)
    check("新题合同重新装载",
          eng._completion_fact_ids == {"f1", "f2"}, eng._completion_fact_ids)
    check("新题 core_answer 重新装载", eng._core_answer == sp2.core_answer,
          eng._core_answer)


def test_ux_legacy_spec_has_no_contract():
    """legacy 题(无合同)不得因为 v5 改动失去通关能力。"""
    print("\n[UX-legacy] 无合同 -> 仍走旧 Final Judge 路径")
    eng, clk = boot(mkcfg())
    check("无合同 -> 合同集合为空", eng._completion_fact_ids == set(),
          eng._completion_fact_ids)
    acts = [a for a in say(eng, clk, "u1", "甲", "#完整解释")
            if a.kind == ActionKind.ANSWER]
    payload = acts[0].payload
    check("ANSWER payload 带空合同",
          payload.get("completion_fact_ids") == [],
          payload.get("completion_fact_ids"))
    eng.submit_qa([QAResult(qid=payload["qid"], verdict="揭晓",
                            solution_candidate=True)],
                  expect_round=payload.get("expect_round"),
                  expect_spec_key=payload.get("expect_spec_key"))
    check("legacy P.SOLVE 仍能通关", eng.phase == Phase.REVEALING, eng.phase)


def test_ux_answer_payload_carries_contract():
    """ANSWER payload 必须带合同 —— director 只搬运, 不推断。"""
    print("\n[UX-payload] ANSWER payload 带通关合同")
    eng, clk, sp = boot_v5()
    acts = [a for a in say(eng, clk, "u1", "甲", "#问题")
            if a.kind == ActionKind.ANSWER]
    check("payload 带 completion_fact_ids",
          acts[0].payload.get("completion_fact_ids") == ["f1", "f2"],
          acts[0].payload.get("completion_fact_ids"))


def test_ux_established_not_in_ui_json():
    """established_fact_ids 只进 archive, **不进**上屏 JSON。"""
    print("\n[UX-archive] established 落盘但不外露")
    r = QARec(qid=1, user_name="甲", text="x", verdict="是",
              established_fact_ids=["f1"], touched_fact_ids=["f1"])
    check("to_json 不含 established", "established_fact_ids" not in r.to_json(),
          r.to_json())
    check("to_archive 含 established",
          r.to_archive().get("established_fact_ids") == ["f1"],
          r.to_archive())


def test_ux_established_reaches_archive():
    """端到端: 真人 QA 的 established 必须出现在 qa_archive 里。"""
    print("\n[UX-archive2] established 一路到 qa_archive")
    eng, clk, sp = boot_v5()
    _answer_and_submit(eng, clk, "u1", "甲", "第一条",
                       verdict="是", established_fact_ids=["f1"])
    recs = [r for r in eng._qa_archive if r.kind == "qa"]
    check("archive 里有记录", bool(recs), recs)
    check("记录带 established",
          recs and recs[-1].established_fact_ids == ["f1"],
          recs[-1].established_fact_ids if recs else None)


def test_runtime_spec_key_includes_completion_contract():
    """Case L: v5 通关合同**必须**进运行时身份。

    这条是本批最容易漏、后果最隐蔽的一条:

        同一个谜面 + 同一个谜底, 只改了 completion_fact_ids
        -> 这已经是另一道题了(观众要建立的事实不同, 通关时刻不同)

    若它不进哈希, `expect_spec_key` 复核会认为"还是那一稿" ——
    一道按**旧**合同在飞的 ANSWER 会把 established 写进**新**合同的题里。
    这是 identity bug, 与"新谜底 + 旧 facts"同一类。
    """
    print("\n[UX-L] runtime_spec_key 必须包含通关合同")
    from story.puzzle import runtime_spec_key
    f = [{"id": "f1", "text": "事实一"}, {"id": "f2", "text": "事实二"}]
    a = [{"id": "a1", "text": "原因"}]
    base = runtime_spec_key("谜面", "谜底", f, a, [],
                            core_answer="这是核心答案",
                            completion_fact_ids=["f1"])
    # ---- 只改 core_answer ----
    check("只改 core_answer -> 不同 key",
          runtime_spec_key("谜面", "谜底", f, a, [],
                           core_answer="换了一句话的核心答案",
                           completion_fact_ids=["f1"]) != base)
    # ---- 只改 completion_fact_ids ----
    check("只改 completion_fact_ids -> 不同 key",
          runtime_spec_key("谜面", "谜底", f, a, [],
                           core_answer="这是核心答案",
                           completion_fact_ids=["f1", "f2"]) != base)
    # ---- 顺序不改变身份(它是集合语义) ----
    check("completion 顺序不同 -> **同** key(集合语义)",
          runtime_spec_key("谜面", "谜底", f, a, [],
                           core_answer="这是核心答案",
                           completion_fact_ids=["f2", "f1"])
          == runtime_spec_key("谜面", "谜底", f, a, [],
                              core_answer="这是核心答案",
                              completion_fact_ids=["f1", "f2"]))
    # ---- 空合同 = legacy, 且与"有合同"不同 ----
    check("空合同与有合同不同 key",
          runtime_spec_key("谜面", "谜底", f, a, []) != base)
    check("不抛异常(只给合同)",
          isinstance(runtime_spec_key(completion_fact_ids=["f1"]), str))


def test_runtime_spec_key_covers_structure():
    """RF-6: key 必须覆盖**完整** canonical 结构, 不只是 id+text。

    早先每项只取 `id + text`, 于是 atom 的 role/fact_ids/required、
    fact 的 kind/visibility/hintable 全被漏掉; 更明显的是 `FairClue`
    **根本没有 text 字段**(它只有 quote + supports_atoms), 所以不同的
    线索内容几乎没进哈希 —— 两条 quote 与指向都不同的 clue 会算出同一个
    key, 身份判断直接失效。
    """
    print("\n[S06g] runtime_spec_key 覆盖结构字段")
    import copy
    from story.puzzle import runtime_spec_key as k
    f = [{"id": "f1", "text": "t", "kind": "core",
          "visibility": "hidden", "hintable": True}]
    a = [{"id": "a1", "role": "cause", "text": "x",
          "fact_ids": ["f1"], "required": True}]
    c = [{"quote": "谜面原句", "supports_atoms": ["a1"]}]
    base = k("p", "a", f, a, c)
    check("同输入稳定", base == k("p", "a", f, a, c))

    def mutated(which, **change):
        f2, a2, c2 = copy.deepcopy(f), copy.deepcopy(a), copy.deepcopy(c)
        {"fact": f2, "atom": a2, "clue": c2}[which][0].update(change)
        return k("p", "a", f2, a2, c2)

    # 结构字段逐个验 —— 每一个都必须改变 key
    for label, which, change in (
            ("atom.role", "atom", {"role": "mechanism"}),
            ("atom.fact_ids", "atom", {"fact_ids": ["f9"]}),
            ("atom.required", "atom", {"required": False}),
            ("fact.kind", "fact", {"kind": "support"}),
            ("fact.visibility", "fact", {"visibility": "public"}),
            ("fact.hintable", "fact", {"hintable": False}),
            ("clue.quote", "clue", {"quote": "另一句"}),
            ("clue.supports_atoms", "clue", {"supports_atoms": ["a9"]})):
        check(f"改 {label} -> key 变化",
              mutated(which, **change) != base, label)


def test_engine_tracks_current_spec_key():
    """Engine 接受题后必须记住它的运行时身份。"""
    print("\n[S06c] Engine 记录 current_spec_key")
    eng, _clk = boot(mkcfg())
    check("接受题后有 key", bool(eng._current_spec_key), eng._current_spec_key)
    k1 = eng._current_spec_key
    # 开新题 -> 清空, 直到新题被接受
    eng._enter_setting_locked(0.0, "riddle")
    check("开新题时清空", eng._current_spec_key == "", eng._current_spec_key)
    eng.submit_riddle("另一道完全不同的题。为什么?", "另一个谜底。",
                      ["h1", "h2", "h3"], title="另一题",
                      expect_round=eng.round_index)
    check("新题接受后又有 key", bool(eng._current_spec_key))
    check("两道题的 key 不同", eng._current_spec_key != k1,
          (eng._current_spec_key, k1))


def test_async_payloads_carry_identity():
    """ANSWER / HINT / REVEAL 三种 payload 都要带 round + spec_key。"""
    print("\n[S06d] 异步 payload 带身份")
    eng, _clk = boot(mkcfg())
    # ANSWER
    eng.submit_danmaku("u1", "观众1", "#这是问题吗")
    acts = eng.tick()
    ans = [a for a in acts if a.kind == ActionKind.ANSWER]
    check("有 ANSWER 动作", bool(ans), [a.kind for a in acts])
    if ans:
        p = ans[0].payload
        check("ANSWER 带 expect_round",
              p.get("expect_round") == eng.round_index, p.get("expect_round"))
        check("ANSWER 带 expect_spec_key",
              p.get("expect_spec_key") == eng._current_spec_key,
              p.get("expect_spec_key"))
    # HINT: 走 _hint_action 的路径需要时间推进; 直接查 builder
    acts2 = eng._enter_revealing_locked(0.0, "giveup", "")
    rev = [a for a in acts2 if a.kind == ActionKind.REVEAL]
    check("有 REVEAL 动作", bool(rev), [a.kind for a in acts2])
    if rev:
        p = rev[0].payload
        check("REVEAL 带 expect_round",
              p.get("expect_round") == eng.round_index, p.get("expect_round"))
        check("REVEAL 带 expect_spec_key",
              p.get("expect_spec_key") == eng._current_spec_key,
              p.get("expect_spec_key"))


def test_stale_qa_callback_is_discarded():
    """**核心**: 别的题(或别的稿)回来的 QA 回调 -> 整包丢弃。"""
    print("\n[S06e] 跨题 QA 回调被丢弃")
    eng, _clk = boot(mkcfg())
    eng.submit_danmaku("u1", "观众1", "#问题")
    eng.tick()
    # 用一个**不匹配**的 spec_key 回包
    acts = eng.submit_qa([QAResult(qid=1, verdict="是")],
                         expect_round=eng.round_index,
                         expect_spec_key="deadbeefdeadbeef")
    check("不匹配 -> 无动作", acts == [], acts)
    check("在途没被消费", 1 in eng._inflight, list(eng._inflight))
    # 匹配的照常
    acts2 = eng.submit_qa([QAResult(qid=1, verdict="是")],
                          expect_round=eng.round_index,
                          expect_spec_key=eng._current_spec_key)
    check("匹配 -> 正常处理", bool(acts2), acts2)


def test_stale_reveal_callback_is_discarded():
    """揭晓回调也要挡 —— 否则上一题的揭晓文案会揭到新题上。"""
    print("\n[S06f] 跨题 REVEAL 回调被丢弃")
    eng, _clk = boot(mkcfg())
    eng._enter_revealing_locked(0.0, "giveup", "")
    acts = eng.submit_reveal("这是上一题的揭晓文案", expect_round=99)
    check("round 不符 -> 丢弃", acts == [], acts)
    check("阶段没变", eng.phase == Phase.REVEALING, eng.phase)


# ======================================================================
# H1: 提示 = 时间 OR 真人成功问答数
# ======================================================================
def _answer_n(eng, n, start=1, clk=None, gap=0.5):
    """喂 n 条**成功**的真人裁决。

    ⚠️ 必须走**真实派发链**: `submit_qa` 只认"还在 `_inflight` 里"的
    qid(不在途的回包一律丢弃 —— 那是防跨题/重复的纪律)。所以这里先
    发弹幕 -> tick 派发 -> 再 submit_qa。直接凭空 submit_qa 什么都不
    会发生, 那会测出"提示永远不来"的假故障。
    """
    # ⚠️ 两个坑:
    #   ① 在途数被 `qa_max_inflight` 封顶(默认 5) —— 先全发再全回会卡住,
    #      所以必须边派发边回包。
    #   ② qid 由**引擎**按 `_next_qid` 递增分配, 不是弹幕文本里的数字。
    #      凭空 submit_qa(qid=N) 只会被当成"不在途的过期回包"丢掉, 于是
    #      计数永远是 0 —— 那会测出"提示永远不来"的假故障。
    #      所以 qid 从 tick 派发的 ANSWER 动作里读。
    win = max(1, eng.cfg.qa_max_inflight)
    done = 0
    tag = 0
    while done < n:
        batch = min(win, n - done)
        for _ in range(batch):
            tag += 1
            eng.submit_danmaku(f"u{start}_{tag}", f"观众{start}_{tag}",
                               f"#问题{start}_{tag}")
        if clk is not None:
            clk.advance(gap)
        qids = [a.payload["qid"] for a in eng.tick()
                if a.kind == ActionKind.ANSWER]
        for qid in qids:
            eng.submit_qa([QAResult(qid=qid, verdict="是", comment="")])
        done += batch


def test_h1_a_question_count_triggers_hint():
    """**H1-A**: 19 条成功裁决不发提示; 第 20 条发 Hint 1。

    时间轴设得很远(9999s), 所以这条只可能是问答数触发的。
    """
    print("\n[H1-A] 20 条成功问答 -> Hint 1")
    eng, clk = boot(mkcfg(hint_seconds=9999, max_hints=3,
                          restate_seconds=99999,
                          hint_questions_per_level=20,
                          hint_min_gap_seconds=45.0))
    _answer_n(eng, 19, clk=clk)
    clk.advance(1)
    got = [a for a in eng.tick() if a.kind == ActionKind.HINT]
    check("19 条: **不发提示**", not got, [a.payload for a in got])
    _answer_n(eng, 1, start=20, clk=clk)
    clk.advance(1)
    got = [a for a in eng.tick() if a.kind == ActionKind.HINT]
    check("**第 20 条 -> Hint 1**", len(got) == 1, got)
    check("level=1", got and got[0].payload["level"] == 1,
          got[0].payload if got else None)


def test_h1_b_time_after_question_does_not_repeat():
    """**H1-B**: 20 问已经发过 Hint1; 到 5 分钟**不能**重复 Hint1。"""
    print("\n[H1-B] 问答触发过之后, 时间到不重复同一条")
    eng, clk = boot(mkcfg(hint_seconds=300, max_hints=3,
                          restate_seconds=99999,
                          hint_questions_per_level=20,
                          hint_min_gap_seconds=45.0))
    _answer_n(eng, 20, clk=clk)
    clk.advance(1)
    got = [a for a in eng.tick() if a.kind == ActionKind.HINT]
    check("20 问 -> Hint 1", len(got) == 1, got)
    eng.submit_hint("提示一")
    # 空转到 5 分钟时间格
    clk.advance(301)
    got = [a for a in eng.tick() if a.kind == ActionKind.HINT]
    check("**时间到 5min 但不重复 Hint 1**", not got, [a.payload for a in got])
    check("_hints_given 仍是 1", eng._hints_given == 1, eng._hints_given)


def test_h1_c_cooldown_prevents_back_to_back():
    """**H1-C**: 40 问在 Hint1 后立刻达到 -> 冷却内不发 Hint2; 45s 后发。"""
    print("\n[H1-C] cooldown 防连发")
    eng, clk = boot(mkcfg(hint_seconds=9999, max_hints=3,
                          restate_seconds=99999,
                          hint_questions_per_level=20,
                          hint_min_gap_seconds=45.0))
    _answer_n(eng, 20, clk=clk)
    clk.advance(1)
    got = [a for a in eng.tick() if a.kind == ActionKind.HINT]
    check("20 问 -> Hint 1", len(got) == 1, got)
    eng.submit_hint("提示一")
    # 立刻冲到 40 问
    _answer_n(eng, 20, start=100, clk=clk)
    clk.advance(1)
    got = [a for a in eng.tick() if a.kind == ActionKind.HINT]
    check("**40 问但冷却未过 -> 不发 Hint 2**", not got,
          [a.payload for a in got])
    # 冷却过了
    clk.advance(50)
    got = [a for a in eng.tick() if a.kind == ActionKind.HINT]
    check("**45s 后 -> Hint 2**", len(got) == 1, got)
    check("level=2", got and got[0].payload["level"] == 2,
          got[0].payload if got else None)


def test_h1_d_only_successful_human_verdicts_count():
    """**H1-D**: 只有正常裁决计数 —— unavailable / 未判定不算。"""
    print("\n[H1-D] 只数成功真人裁决")
    eng, clk = boot(mkcfg(hint_seconds=9999, max_hints=3,
                          restate_seconds=99999,
                          hint_questions_per_level=20,
                          hint_min_gap_seconds=0))
    # 19 条正常 + 一堆"未判定"
    _answer_n(eng, 19, clk=clk)
    from story import parser as P
    for i in range(200, 260):
        eng.submit_qa([QAResult(qid=i, verdict=P.UNAVAILABLE,
                                status="unavailable")])
    clk.advance(1)
    got = [a for a in eng.tick() if a.kind == ActionKind.HINT]
    check("**19 正常 + 60 未判定 -> 仍不发提示**", not got,
          [a.payload for a in got])
    check("verdict_counts 里没有未判定",
          P.UNAVAILABLE not in eng._verdict_counts, eng._verdict_counts)
    # 第 20 条正常 -> 发
    _answer_n(eng, 1, start=20, clk=clk)
    clk.advance(1)
    got = [a for a in eng.tick() if a.kind == ActionKind.HINT]
    check("第 20 条正常 -> Hint 1", len(got) == 1, got)


def test_h1_e_question_count_never_reveals():
    """**H1-E**: 问答数**绝不能**改变 20min 自动揭晓时间。

    刷 500 条提问也只会给满 3 条提示, 揭晓仍等时间轴。
    """
    print("\n[H1-E] 问答数不触发揭晓")
    N = 300.0
    eng, clk = boot(mkcfg(hint_seconds=N, max_hints=3, restate_seconds=99999,
                          hint_questions_per_level=20,
                          hint_min_gap_seconds=0))
    # ⚠️ `_answer_n` 内部的 tick 也可能把 HINT 派发出来 —— 那些动作会被
    # 它丢掉, 于是 `_hint_pending` 永远挂着, 后面的循环一条提示都看不到
    # (不是"提示没来", 是"上一条还在途")。所以这里自己驱动, 边 tick
    # 边回收 HINT。
    given = 0
    for _ in range(60):
        clk.advance(1)
        for a in eng.tick():
            if a.kind == ActionKind.HINT:
                given += 1
                eng.submit_hint(f"提示{given}")
    # 疯狂刷提问(远超 60 条 = 3 格)
    win = max(1, eng.cfg.qa_max_inflight)
    tag = 0
    for _ in range((500 // win) + 1):
        for _ in range(win):
            tag += 1
            eng.submit_danmaku(f"u{tag}", f"观众{tag}", f"#问题{tag}")
        clk.advance(1)
        qids = []
        for a in eng.tick():
            if a.kind == ActionKind.ANSWER:
                qids.append(a.payload["qid"])
            elif a.kind == ActionKind.HINT:
                given += 1
                eng.submit_hint(f"提示{given}")
        for qid in qids:
            eng.submit_qa([QAResult(qid=qid, verdict="是", comment="")])
    check("**仍在 QA(没被问答数刷到揭晓)**",
          eng.phase == Phase.QA, (eng.phase, clk.t))
    check("提示给满 3 条就停", eng._hints_given == 3, eng._hints_given)
    # 时间轴走完才揭晓
    clk.advance(N * 4)
    eng.tick()
    check("**时间轴走完 -> 揭晓**", eng.phase == Phase.REVEALING,
          (eng.phase, clk.t))


def test_h1_f_failed_hint_does_not_consume_slot_or_cooldown():
    """**H1-F**: hint worker 失败 -> 不消耗槽位, 也不开冷却。

    原有 retry 纪律必须继续成立(第三轮 review P1)。
    """
    print("\n[H1-F] 提示失败不消耗槽位/不开冷却")
    eng, clk = boot(mkcfg(hint_seconds=9999, max_hints=3,
                          restate_seconds=99999,
                          hint_questions_per_level=20,
                          hint_min_gap_seconds=45.0,
                          hint_retry_seconds=1.0))
    _answer_n(eng, 20, clk=clk)
    clk.advance(1)
    got = [a for a in eng.tick() if a.kind == ActionKind.HINT]
    check("派发 Hint 1", len(got) == 1, got)
    before = eng._hints_given
    eng.submit_hint(error="网关超时")
    check("**槽位不被消耗**", eng._hints_given == before, eng._hints_given)
    check("**冷却没被打开**", eng._hint_cooldown_until == 0.0,
          eng._hint_cooldown_until)
    # 退避过后重试同一格
    clk.advance(2)
    got = [a for a in eng.tick() if a.kind == ActionKind.HINT]
    check("**退避后重试 Hint 1**", len(got) == 1, got)
    check("level 仍是 1", got and got[0].payload["level"] == 1,
          got[0].payload if got else None)
    # 这次成功 -> 消耗 + 开冷却
    eng.submit_hint("提示一")
    check("成功后槽位 +1", eng._hints_given == 1, eng._hints_given)
    check("成功后冷却已开", eng._hint_cooldown_until > 0,
          eng._hint_cooldown_until)


def test_h1_g_desired_level_is_max_of_two():
    """desired = max(时间格, 问答格) —— 两条腿谁先到谁算数。"""
    print("\n[H1-G] 两个触发源取 max")
    # 时间快、问答慢
    eng, clk = boot(mkcfg(hint_seconds=100, max_hints=3, restate_seconds=99999,
                          hint_questions_per_level=20,
                          hint_min_gap_seconds=0))
    clk.advance(101)
    got = [a for a in eng.tick() if a.kind == ActionKind.HINT]
    check("时间先到 -> 时间格给提示", len(got) == 1, got)
    eng.submit_hint("提示一")
    # 问答快、时间慢
    eng2, clk2 = boot(mkcfg(hint_seconds=9999, max_hints=3,
                            restate_seconds=99999,
                            hint_questions_per_level=20,
                            hint_min_gap_seconds=0))
    _answer_n(eng2, 20, clk=clk2)
    clk2.advance(1)
    got = [a for a in eng2.tick() if a.kind == ActionKind.HINT]
    check("问答先到 -> 问答格给提示", len(got) == 1, got)


def test_h1_h_zero_disables_question_trigger():
    """`hint_questions_per_level=0` = 关掉问答触发, 退回纯时间轴。"""
    print("\n[H1-H] 0 = 关掉问答触发")
    eng, clk = boot(mkcfg(hint_seconds=9999, max_hints=3,
                          restate_seconds=99999,
                          hint_questions_per_level=0))
    _answer_n(eng, 100, clk=clk)
    clk.advance(1)
    got = [a for a in eng.tick() if a.kind == ActionKind.HINT]
    check("100 问也不发提示", not got, [a.payload for a in got])


def test_h1_config_warnings():
    print("\n[H1-I] Config 抓异常值")
    w = Config(sim_path="x", hint_questions_per_level=-1).validate()
    check("负值有告警", any("hint_questions_per_level" in x for x in w), w)
    w2 = Config(sim_path="x", hint_min_gap_seconds=-1).validate()
    check("冷却为负有告警", any("hint_min_gap_seconds" in x for x in w2), w2)
    w3 = Config(sim_path="x").validate()
    check("默认无这两条告警",
          not any("hint_questions_per_level" in x
                  or "hint_min_gap_seconds" in x for x in w3), w3)


def test_h1_cli_flags_wired():
    """只加字段不接线 = dead config(而 --help 里写着)。"""
    print("\n[H1-J] CLI flag 真的接上了")
    from story.config import from_args
    cfg = from_args(["--sim", "x", "--hint-questions-per-level", "7",
                     "--hint-min-gap-seconds", "12.5"])
    check("hint_questions_per_level 接上", cfg.hint_questions_per_level == 7,
          cfg.hint_questions_per_level)
    check("hint_min_gap_seconds 接上", cfg.hint_min_gap_seconds == 12.5,
          cfg.hint_min_gap_seconds)
    d = from_args(["--sim", "x"])
    check("默认 20", d.hint_questions_per_level == 20,
          d.hint_questions_per_level)
    check("默认 45.0", d.hint_min_gap_seconds == 45.0,
          d.hint_min_gap_seconds)


def main():
    tests = [test_start_and_riddle,
             # ---- H1: 时间 OR 真人成功问答数 ----
             test_h1_a_question_count_triggers_hint,
             test_h1_b_time_after_question_does_not_repeat,
             test_h1_c_cooldown_prevents_back_to_back,
             test_h1_d_only_successful_human_verdicts_count,
             test_h1_e_question_count_never_reveals,
             test_h1_f_failed_hint_does_not_consume_slot_or_cooldown,
             test_h1_g_desired_level_is_max_of_two,
             test_h1_h_zero_disables_question_trigger,
             test_h1_config_warnings,
             test_h1_cli_flags_wired, test_question_routing, test_concurrency_cap,
             test_answer_flow, test_ordering_and_missing, test_inflight_timeout,
             test_no_duplicate_worker_per_qid, test_answer_payload_carries_qa_budget,
             test_dedupe_and_cap, test_solve_and_reveal,
             test_u1_reveal_snapshot_two_phase,
             test_u2_reveal_stage_three_phase,
             test_u2_stage_boundaries_come_from_config_not_literals,
             test_u2_detail_boundary_validation,
             test_u1_reveal_core_falls_back_when_absent,
             test_u1_pressure_exposes_reveal_remaining,
             test_reveal_once,
             test_next_puzzle_cycle,
             test_timeline_hints_and_reveal, test_timeline_survives_busy_chat,
             test_timeline_countdown_fields,
             test_no_question_cap,
             test_restate_on_idle, test_hints_not_reset_by_chat,
             # ---- Step 09: 完整 QA archive ----
             test_qa_archive_keeps_everything_beyond_ui_tail,
             test_qa_archive_resets_between_puzzles,
             test_qa_archive_includes_hint_and_restate,
             test_qa_archive_stays_sorted_past_ui_cap,
             test_qa_archive_out_of_order_arrival_stays_sorted,
             test_history_trim, test_transcript_bounded,
             test_stop_and_stream_end, test_clock_jump, test_snapshot_keys,
             test_hint_order_and_dedup, test_commands_are_not_swallowed,
             # ---- Q12: 重放识别重做 ----
             test_msg_id_dedupe,
             test_msg_id_cache_is_bounded,
             test_msg_id_bypasses_pairwise_dedupe,
             test_distinct_viewers_burst_is_not_replay,
             test_ten_viewers_one_second_is_not_replay,
             test_reconnect_guard_only_after_reconnect,
             test_reconnect_replay_suppressed,
             test_reconnect_mixed_old_and_new,
             test_reconnect_signal_end_to_end,
             test_engine_on_reconnect_always_arms,
             test_guard_release_keeps_user_name,
             test_guard_release_does_not_swallow_actions,
             test_guard_expires,
             test_incoming_streak_retroactively_suppressed,
             test_streak_broken_by_new_message,
             test_determinism,
             test_llm_failure_never_drops, test_coverage_reaches_archive,
             test_reveal_payload_carries_atoms,
             # ---- Q4 / Q5 ----
             test_signature_recorded_and_passed_on,
             test_signature_window_bounded,
             test_touched_facts_accumulate_and_reset,
             test_candidate_count_and_reset_on_new_puzzle,
             test_answer_action_carries_facts,
             # ---- 第二轮 review ----
             test_fallback_riddle_is_structured,
             test_fallback_specs_all_valid,
             test_fallback_specs_use_official_taxonomy,
             test_fallback_does_not_pollute_quota,
             test_fallback_rotates,
             test_request_riddle_action_is_public_and_matches,
             test_hint_payload_carries_spec_and_touched,
             test_hint_payload_touched_is_a_copy,
             # ---- 第三轮 review ----
             test_fallback_atoms_are_dicts_not_dataclasses,
             test_fallback_reveal_payload_is_json_serializable,
             test_fallback_judge_gate_actually_works,
             test_hint_slot_not_consumed_on_failure,
             test_hint_success_still_advances,
             # ---- Q8c: source provenance ----
             test_spec_source_defaults_to_live_generate,
             test_spec_source_is_recorded_and_reaches_reveal,
             # ---- R2: 揭晓贡献链 ----
             test_reveal_contributors_reach_archive,
             test_fallback_marks_source_as_fallback,
             test_spec_source_resets_between_puzzles,
             test_archive_records_source,
             # ---- 第四轮 review (hint 收尾) ----
             test_hint_repeat_also_backs_off,
             test_hint_never_stuck_pending_on_failure,
             test_director_hint_none_clears_pending,
             test_director_hint_exception_clears_pending,
             test_director_hint_success_still_works,
             # ---- Q7 ----
             # ---- Step 06: spec identity / stale worker gate ----
             test_riddle_action_carries_expect_round,
             test_stale_worker_return_is_discarded,
             test_matching_round_is_accepted,
             test_expect_round_none_keeps_legacy_behavior,
             test_stale_return_cannot_advance_archive_identity,
             test_runtime_spec_key_is_stable_and_distinguishes,
             test_runtime_spec_key_includes_completion_contract,
             # ---- UX-2: 房间共同推理 ----
             test_ux_a_collective_solve,
             test_ux_b_second_fact_completes,
             test_ux_c_touched_is_not_established,
             test_ux_d_illegal_fact_id_dropped,
             test_ux_m_human_only_established,
             test_ux_new_puzzle_clears_established,
             test_ux_legacy_spec_has_no_contract,
             test_ux_answer_payload_carries_contract,
             test_ux_established_not_in_ui_json,
             test_ux_established_reaches_archive,
             test_runtime_spec_key_covers_structure,
             test_engine_tracks_current_spec_key,
             test_async_payloads_carry_identity,
             test_stale_qa_callback_is_discarded,
             test_stale_reveal_callback_is_discarded,
             test_runtime_spec_key_is_stable_and_distinguishes,
             test_engine_tracks_current_spec_key,
             test_async_payloads_carry_identity,
             test_stale_qa_callback_is_discarded,
             test_stale_reveal_callback_is_discarded,
             test_archive_writes_full_schema,
             test_archive_metrics_survive_missing_spec,
             test_archive_fallback_does_not_inherit_previous_metrics,
             test_archive_records_review_latency_total,
             test_full_fallback_chain_reaches_archive,
             # ---- P0-2 / P0-3 ----
             test_retry_riddle_keeps_avoid_and_recent,
             test_first_and_retry_riddle_actions_match,
             test_retry_payload_is_a_copy,
             # ---- Q11: 非 QA 阶段 ACK ----
             test_setting_question_gets_ack,
             test_revealing_question_gets_ack,
             test_revealed_question_gets_ack,
             test_idle_and_stopped_get_no_ack,
             test_ack_is_globally_throttled,
             test_ack_does_not_touch_stats_or_history,
             test_ack_does_not_fire_in_qa,
             test_ack_does_not_hijack_next_or_hint,
             test_ack_action_is_pure_broadcast]
    for t in tests:
        t()
    print()
    if FAIL[0]:
        print(f"FAILED: {FAIL[0]} 项")
        return 1
    print("PASS: 海龟汤状态机全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
