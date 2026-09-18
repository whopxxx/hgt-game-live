"""运行: uv run tests/test_engine.py（完全离线, 无网络）。

海龟汤状态机的确定性单测。用 FakeClock 注入时间, 所以能精确控制
"空闲 45 秒"、"揭晓展示 30 秒" 这类行为, 不用真的等。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from story.config import Config  # noqa: E402
from story.engine import RoundEngine  # noqa: E402
from story.state import ActionKind, Phase, QAResult  # noqa: E402


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
    print("[在途超时重试]")
    eng, clk = boot(mkcfg(qa_inflight_timeout=10.0, qa_retry_max=2))
    eng.submit_danmaku("u1", "甲", "#问题")
    eng.tick()
    check("在途 1", eng._probe()["inflight"] == 1, eng._probe())
    clk.advance(11)
    eng.tick()
    check("超时后回队列",
          eng._probe()["pending"] == 1 and eng._probe()["inflight"] == 0, eng._probe())
    for _ in range(4):
        eng.tick()
        clk.advance(11)
        eng.tick()
    s = eng.snapshot()
    # 重试用尽 -> 兜底裁决必须是**未判定**, 不是"无关"。
    # "无关"是断言"你的猜测与谜底无关", 而这里其实是系统没答上 ——
    # 说成"无关"会把观众的思路带偏。
    check("重试用尽给'未判定'(不是'无关')",
          any(r["verdict"] == "未判定" for r in s.qa_log), s.qa_log)
    check("兜底不含'无关'",
          not any(r["verdict"] == "无关" for r in s.qa_log), s.qa_log)
    # '未判定' 不该进 LLM transcript —— 否则模型会以为它是一种合法裁决
    check("'未判定'不进 transcript", eng._probe()["history"] == 0,
          eng._probe()["history"])


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
    eng, clk = boot(mkcfg(hint_seconds=5, restate_seconds=9999, max_hints=3))
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


def test_reconnect_replay_suppressed():
    """**§9.3 第三条**: 重连后重复刚才完全相同的 8 条 -> 全部抑制。"""
    print("\n[Q12] 重连后重放被抑制")
    eng, clk = boot(mkcfg())
    msgs = [("u1", "甲", "#是父母吗"), ("u2", "乙", "#是兄弟吗"),
            ("u1", "甲", "#是父母吗"), ("u2", "乙", "#是兄弟吗"),
            ("u1", "甲", "#是父母吗"), ("u2", "乙", "#是兄弟吗"),
            ("u1", "甲", "#是父母吗"), ("u2", "乙", "#是兄弟吗")]
    # 先正常收下这批(建立"最近历史"基线)。间隔 3s 避开 2s 成对判重。
    for uid, name, q in msgs:
        eng.submit_danmaku(uid, name, q)
        clk.advance(3.0)
    before = len(eng._danmaku)
    check("基线已建立", before == len(msgs), before)
    # 重连 -> 开 guard
    eng.on_reconnect()
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
    # 每条内容都不同, 且间隔 3s —— 避免撞上那条 2s 成对判重(那是另一道闸,
    # 这里要单独考察 guard)。每条重复 3 次才够 guard 的 min_repeats。
    old = [("u1", "甲", "#旧一"), ("u1", "甲", "#旧一"), ("u1", "甲", "#旧一"),
           ("u2", "乙", "#旧二"), ("u2", "乙", "#旧二"), ("u2", "乙", "#旧二")]
    for uid, name, q in old:
        eng.submit_danmaku(uid, name, q)
        clk.advance(3.0)
    before = len(eng._danmaku)
    check("基线 6 条都上屏", before == 6, before)
    eng.on_reconnect()
    clk.advance(0.5)
    for uid, name, q in old:                   # 6 条旧的重来
        eng.submit_danmaku(uid, name, q)
        clk.advance(0.2)
    after_old = len(eng._danmaku)
    check("**旧消息被抑制**", after_old == before, (before, after_old))
    # 2 条全新的
    eng.submit_danmaku("u3", "丙", "#全新的问题一")
    clk.advance(0.2)
    eng.submit_danmaku("u4", "丁", "#全新的问题二")
    check("**新消息保留**", len(eng._danmaku) == before + 2,
          (before, len(eng._danmaku)))


def test_guard_expires():
    """guard 过窗口就失效 —— 几分钟后又说一遍是真人, 不是重放。"""
    print("\n[Q12] guard 到期后不再判重放")
    eng, clk = boot(mkcfg(replay_guard_seconds=10.0))
    for _ in range(5):
        eng.submit_danmaku("u1", "甲", "#同一句")
        clk.advance(0.5)
    before = len(eng._danmaku)
    eng.on_reconnect()
    clk.advance(11)                            # 越过 guard 窗口
    eng.submit_danmaku("u1", "甲", "#同一句")
    check("**窗口外不判重放**", eng._replays == 0, eng._replays)
    check("正常上屏", len(eng._danmaku) == before + 1, len(eng._danmaku))


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
    print("[LLM 故障不丢提问]")
    eng, clk = boot(mkcfg(qa_inflight_timeout=10.0, qa_retry_max=2))
    eng.submit_danmaku("u1", "甲", "#问题")
    eng.tick()
    for _ in range(6):
        clk.advance(11)
        eng.tick()
    s = eng.snapshot()
    check("提问最终有裁决", len(s.qa_log) >= 1, s.qa_log)


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
    eng = RoundEngine(mkcfg(), clock=clk)
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
    eng, clk = boot(mkcfg())
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
    eng = RoundEngine(mkcfg(riddle_max_attempts=3), clock=clk)
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
    eng = RoundEngine(mkcfg(riddle_max_attempts=1), clock=clk)
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
    sp.prompt_version = "riddle-v3"
    sp.quality_policy_version = "quality-v3"
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
    check("spec_version=2", rec.get("spec_version") == 2, rec.get("spec_version"))
    check("prompt_version 落盘", rec.get("prompt_version") == "riddle-v3", rec)
    check("policy_version 落盘",
          rec.get("quality_policy_version") == "quality-v3", rec)
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
    sp.prompt_version = "riddle-v3"
    sp.quality_policy_version = "quality-v3"
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


def main():
    tests = [test_start_and_riddle, test_question_routing, test_concurrency_cap,
             test_answer_flow, test_ordering_and_missing, test_inflight_timeout,
             test_dedupe_and_cap, test_solve_and_reveal, test_reveal_once,
             test_next_puzzle_cycle,
             test_timeline_hints_and_reveal, test_timeline_survives_busy_chat,
             test_timeline_countdown_fields,
             test_no_question_cap,
             test_restate_on_idle, test_hints_not_reset_by_chat,
             test_history_trim, test_transcript_bounded,
             test_stop_and_stream_end, test_clock_jump, test_snapshot_keys,
             test_hint_order_and_dedup, test_commands_are_not_swallowed,
             # ---- Q12: 重放识别重做 ----
             test_msg_id_dedupe,
             test_msg_id_cache_is_bounded,
             test_distinct_viewers_burst_is_not_replay,
             test_ten_viewers_one_second_is_not_replay,
             test_reconnect_guard_only_after_reconnect,
             test_reconnect_replay_suppressed,
             test_reconnect_mixed_old_and_new,
             test_guard_expires,
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
