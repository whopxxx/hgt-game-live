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
    # 测试默认**关掉重放检测**(replay_burst_n=0): 否则每条弹幕都要等
    # 缓冲窗口才提交, 所有测试都得改成异步写法。重放检测有专门的用例覆盖。
    kw.setdefault("replay_burst_n", 0)
    return Config(sim_path="x", no_llm=True, **kw)


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
    """发一条弹幕并**提交**(引擎现在会先缓冲一小会儿做重放检测)。

    真人节奏: 发一条 -> 过一会儿 -> tick 提交。测试里统一用这个帮手。
    返回那次 tick 产生的动作列表(提交与派发往往发生在同一次 tick)。
    """
    eng.submit_danmaku(uid, name, text)
    clk.advance(gap)
    return eng.tick()


def say_many(eng, clk, items, gap=20.0):
    """连续发多条弹幕, 让它们**全部提交但不派发**。

    每条之间推 20 秒(远超重放窗口), 所以:
      - 不会被误判为重放
      - 每条到达时, 前一条所在的窗口已结束 -> 自动提交
    最后 tick 一次, 让它们进入 pending 队列(还没派发)。
    """
    for uid, name, text in items:
        eng.submit_danmaku(uid, name, text)
        clk.advance(gap)
    if eng._burst:
        eng._flush_burst()


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
    check("发言不推迟提示",
          [a for a in eng.tick() if a.kind == ActionKind.HINT] != [], eng.tick())
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


def test_commands_survive_burst_buffer():
    print("[指令不能被缓冲吞掉]")
    # 实测 bug: _flush_burst 曾把 _accept_danmaku 的返回值丢掉,
    # 导致 #提示 / #下一题 这两个指令**彻底失效**(观众发了没反应)。
    # 这里钉住: 走生产配置(缓冲开启)时, 指令仍要能生效。
    eng, clk = boot(Config(sim_path="x", no_llm=True))
    clk.advance(60)          # 过掉 #提示 的 20 秒节流
    eng.submit_danmaku("u1", "甲", "#提示")
    clk.advance(2)           # 过缓冲窗口
    acts = eng.tick()
    check("#提示 生效(缓冲不吞动作)",
          any(a.kind == ActionKind.HINT for a in acts), kinds(acts))
    # #下一题 同理
    clk.advance(30)
    eng.submit_danmaku("u1", "甲", "#下一题")
    clk.advance(2)
    acts = eng.tick()
    check("#下一题 生效",
          any(a.kind == ActionKind.REVEAL for a in acts), kinds(acts))


def test_replay_detection():
    print("[重连重放: 整批挡掉, 但别误伤真人]")
    # 生产配置(重放检测开启)
    eng, clk = boot(Config(sim_path="x", no_llm=True))
    # ① 真人节奏: 每条隔 20 秒, 不该被误判
    for q in ["#是父母吗", "#是兄弟吗", "#是同学吗"]:
        eng.submit_danmaku("u1", "甲", q)
        clk.advance(20)
        eng.tick()
    check("真人节奏不误判", eng._replays == 0, eng._replays)
    check("真人弹幕正常上屏", len(eng._danmaku) == 3, len(eng._danmaku))
    dm0 = len(eng._danmaku)
    # ② 重连重放: 11 条同一瞬间涌进 -> 整批丢弃
    for q in ["111", "#是爱人的电话吗", "#是人打来的电话吗", "#是亲人打来的电话吗",
              "#是儿女吗", "#是父母吗", "[捂脸]", "?", "#是谁", "?", "#母亲去世了吗"]:
        eng.submit_danmaku("u2", "乙", q)
    clk.advance(3)
    eng.tick()
    check("重放被识别", eng._replays == 1, eng._replays)
    check("重放整批不上屏", len(eng._danmaku) == dm0, len(eng._danmaku))
    check("重放不计票", eng._probe()["pending"] == 0, eng._probe())
    # ③ 压制期过后, 新观众说话应正常
    clk.advance(10)
    dm1 = len(eng._danmaku)
    eng.submit_danmaku("u3", "丙", "#新问题")
    clk.advance(20)
    eng.tick()
    check("压制期后恢复正常", len(eng._danmaku) == dm1 + 1, len(eng._danmaku))


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
    cfg.replay_burst_n = 0
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
             test_hint_order_and_dedup, test_commands_survive_burst_buffer,
             test_replay_detection, test_determinism,
             test_llm_failure_never_drops, test_coverage_reaches_archive]
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
