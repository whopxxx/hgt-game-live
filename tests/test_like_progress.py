#!/usr/bin/env python
# coding: utf-8
"""运行: uv run tests/test_like_progress.py（完全离线, 无网络）。

Issue #43: 统一点赞推进机制 —— 专用回归套件。

覆盖任务书 §21~§28 的必测清单:
  §21 Like high-water(session 级, 换题不清零)
  §22 round AI opportunity(当前题资源, 不跨题)
  §23 effective elapsed(Hint 加速但真实冷却不加速)
  §24 自动揭晓的真实时间最低保护 + 真人通关不受限
  §25 单 tick 不连发(大 burst 不刷屏)
  §26 旧换题指令墓碑(不 reveal / 不进 QA)
  §16/§17 快照: 显式点赞公告事件 + 同源时间轴
  §28 REVEALED 60 秒不被点赞缩短

产品语义一句话: 每新增 100 赞 = 1 个 pulse = 当前题 AI 机会 +1 **且**
当前题有效时间 +30s(内部调参值, UI 永不显示秒数)。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from story.config import Config  # noqa: E402
from story.engine import RoundEngine  # noqa: E402
from story.state import ActionKind, Phase, QAResult  # noqa: E402
from story.ingest import InteractionEvent  # noqa: E402
from story.puzzle import PuzzleSpec  # noqa: E402

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
        return self.t


def mkcfg(**kw):
    kw.setdefault("no_llm", True)
    return Config(sim_path="x", **kw)


def kinds(acts):
    return [a.kind for a in acts]


def boot(cfg):
    """建引擎 + start() + 交一个谜题, 停在 QA。返回 (eng, clk)。"""
    clk = FakeClock()
    eng = RoundEngine(cfg, clock=clk)
    eng.start()
    eng.submit_riddle("一个男人点了海龟汤，喝一口就自杀了。为什么？",
                      "同伴的肉汤骗局。", ["注意汤的味道"], title="海龟汤")
    assert eng.phase == Phase.QA, eng.phase
    return eng, clk


def like(eng, total):
    """发一条 Like.total, 返回引擎动作。"""
    return eng.submit_interaction(InteractionEvent(kind="like", total=total))


def pulses_of(eng):
    return eng._ai_player_ledger.summon_earned_total


def completion_spec(completion=("f1",), core="他是自杀。"):
    return PuzzleSpec.from_dict({
        "puzzle": "一个男人点了海龟汤，喝一口就自杀了。为什么？",
        "answer": "同伴的肉汤骗局。",
        "facts": [{"id": "f1", "text": "汤里有人肉", "kind": "core"}],
        "solve_atoms": [{"id": "a1", "role": "cause", "text": "人肉",
                         "fact_ids": ["f1"]}],
        "fair_clues": [{"quote": "喝一口", "supports_atoms": ["a1"]}],
        "core_answer": core,
        "completion_fact_ids": list(completion),
        "signature": {"mechanism_family": "hidden_ingredient",
                      "solution_shape": "hidden_ingredient_explains_behavior",
                      "domain": "food"},
    })


# ======================================================================
# §21 Like high-water —— session 级
# ======================================================================
def test_high_water_baseline_and_buckets():
    """§21: 首次 baseline 不补历史; 档位跨越一次算清; 重复/倒退为 0。"""
    print("\n[LP-1] high-water 基线与档位(Engine 接线)")
    eng, clk = boot(mkcfg())
    check("首次 total=90 只建基线", len(like(eng, 90)) == 0
          and pulses_of(eng) == 0, pulses_of(eng))
    check("90 -> 99: 0", len(like(eng, 99)) == 0 and pulses_of(eng) == 0)
    like(eng, 100)
    check("99 -> 100: +1", pulses_of(eng) == 1, pulses_of(eng))
    like(eng, 350)
    check("100 -> 350: +2(跨 2 桶)", pulses_of(eng) == 3, pulses_of(eng))
    led = eng._ai_player_ledger
    check("重复 total: 0", (like(eng, 350), pulses_of(eng) == 3)[1])
    check("倒退 total: 0 且不 rebase",
          len(like(eng, 200)) == 0 and led.likes_total_high_water == 350,
          led.likes_total_high_water)
    # 桶进度来自 session 高水位
    check("likes_progress = 50", eng.snapshot().ai_player["likes_progress"] == 50,
          eng.snapshot().ai_player)


def test_high_water_survives_round_change():
    """§21: 换题后 high-water / bucket consumed 不清零 —— 不重复结算旧赞。"""
    print("\n[LP-2] 换题不清 high-water(1287 -> 1387 只 +1)")
    eng, clk = boot(mkcfg())
    like(eng, 0)                      # 基线 0
    like(eng, 1287)
    got_first_round = pulses_of(eng)
    check("第一题 1287 -> +12", got_first_round == 12, got_first_round)
    # 走完 揭晓 -> 下一题
    eng._enter_revealing_locked(clk.t, "timeout", "")
    eng.submit_reveal("谜底", now=clk.t)
    clk.advance(60)
    eng.tick()
    assert eng.phase == Phase.SETTING, eng.phase
    check("新题 round 额度从 0 开始", pulses_of(eng) == 0, pulses_of(eng))
    led = eng._ai_player_ledger
    check("高水位保留 1287", led.likes_total_high_water == 1287,
          led.likes_total_high_water)
    check("桶数保留 12", led.likes_bucket_consumed == 12,
          led.likes_bucket_consumed)
    like(eng, 1387)
    check("新题 1287 -> 1387 只 +1(不重算 12 个旧桶)",
          pulses_of(eng) == 1, pulses_of(eng))
    eng.submit_riddle("第二题谜面。为什么？", "谜底", now=clk.t)
    check("新题入 QA 后该 1 次可用",
          eng._ai_player_ledger.available == 1, led.snapshot())


# ======================================================================
# §22 round AI opportunity —— 当前题资源
# ======================================================================
def test_round_opportunity_qa_credit_and_burst():
    """§22/§25: QA +N 一次性入账; N 很大也只调度一个 AI 动作。"""
    print("\n[LP-3] QA 批量 pulse 一次性入账, 不连发 AI")
    eng, clk = boot(mkcfg())
    like(eng, 0)                      # 基线 0
    like(eng, 600)                    # +6
    check("一次 +6", pulses_of(eng) == 6, pulses_of(eng))
    acts = eng.tick()
    ai_acts = [a for a in acts if a.kind == ActionKind.AI_PLAYER]
    check("一个 tick 最多一个 AI 动作(绝不瞬间 5 条)",
          len(ai_acts) <= 1, kinds(acts))
    # 预约占用 1, 剩 5
    check("其余额度仍在账上", pulses_of(eng) == 6
          and eng._ai_player_ledger.available == 5, led_snapshot(eng))


def led_snapshot(eng):
    return eng._ai_player_ledger.snapshot()


def test_round_opportunity_reset_on_new_round():
    """§22: 上一题未用的 opportunity 清零; 新题跨新桶只拿新桶。"""
    print("\n[LP-4] 新题清零 round 额度")
    eng, clk = boot(mkcfg())
    like(eng, 0)                      # 基线 0
    like(eng, 950)                    # +9, 全部未用
    eng._enter_revealing_locked(clk.t, "timeout", "")
    eng.submit_reveal("谜底", now=clk.t)
    clk.advance(60)
    eng.tick()
    eng.submit_riddle("第二题谜面。为什么？", "谜底", now=clk.t)
    check("进 QA 后上题 9 次已清零",
          pulses_of(eng) == 0 and eng._ai_player_ledger.available == 0,
          led_snapshot(eng))
    acts = eng.tick()
    check("额度 0 时不派 AI 动作",
          not any(a.kind == ActionKind.AI_PLAYER for a in acts), kinds(acts))


def test_setting_pulses_credit_current_round():
    """§5/§22: SETTING 的 pulse 记入正在准备的题; SETTING 本身不发 AI。"""
    print("\n[LP-5] SETTING 阶段的 pulse 不浪费")
    eng, clk = RoundEngine(mkcfg()), FakeClock()
    eng.start()
    assert eng.phase == Phase.SETTING
    like(eng, 0)                      # 基线 0
    like(eng, 250)                    # +2
    check("SETTING 入账当前(将来的)round", pulses_of(eng) == 2)
    notice = eng.snapshot().like_progress_notice
    check("SETTING 公告中性、round 指向正在准备的题",
          notice and notice["phase"] == "setting"
          and notice["round_index"] == 1
          and "新题开始后生效" in notice["text"],
          notice)
    check("SETTING 不谎称 AI 正在行动",
          notice and "正在" not in notice["text"], notice and notice["text"])
    acts = eng.tick()
    check("SETTING tick 不派 AI 动作",
          not any(a.kind == ActionKind.AI_PLAYER for a in acts), kinds(acts))
    eng.submit_riddle("谜面。为什么？", "谜底", now=clk.t)
    check("进 QA 后 SETTING 期间的额度可用",
          eng._ai_player_ledger.available == 2, led_snapshot(eng))


def test_revealing_revealed_pulses_not_credited():
    """§5/§22/§28: REVEALING/REVEALED 只推进高水位 —— 不入账、不公告、
    不把 pulse 带进下一题, 也不碰 _next_puzzle_deadline。"""
    print("\n[LP-6] 揭晓阶段的 pulse 只记遥测")
    eng, clk = boot(mkcfg())
    like(eng, 150)                    # 基线 100, +0(150 在基线之后? 100 基线桶1, 150 桶1 -> 0)
    # 上面 150 没过桶; 先记 REVEALED 时刻的 deadline
    eng._enter_revealing_locked(clk.t, "timeout", "")
    eng.submit_reveal("谜底", now=clk.t)
    assert eng.phase == Phase.REVEALED
    deadline = eng._next_puzzle_deadline
    like(eng, 400)                    # +3 pulse, 但阶段不消费
    led = led_snapshot(eng)
    check("高水位/桶数照常推进",
          led["likes_total_high_water"] == 400
          and led["likes_bucket_consumed"] == 4, led)
    check("不入账当前题(下一题也拿不到)",
          eng._ai_player_ledger.summon_earned_total == 0, led)
    check("REVEALED 阶段不发点赞公告",
          eng.snapshot().like_progress_notice is None)
    check("点赞绝不修改 _next_puzzle_deadline(§28)",
          eng._next_puzzle_deadline == deadline,
          (eng._next_puzzle_deadline, deadline))
    clk.advance(60)
    eng.tick()
    eng.submit_riddle("第二题谜面。为什么？", "谜底", now=clk.t)
    check("揭晓阶段的 pulse 不带入下一题",
          pulses_of(eng) == 0 and eng._ai_player_ledger.available == 0,
          led_snapshot(eng))
    notice = eng.snapshot().like_progress_notice
    check("新题快照不带旧公告(§16)", notice is None, notice)


def test_stale_reservation_cannot_pollute_new_round():
    """§22: 上一题的在途预约在新题就地失效, 不污染新题。"""
    print("\n[LP-7] stale reservation 不污染新题")
    eng, clk = boot(mkcfg())
    like(eng, 0)                      # 基线 0
    like(eng, 100)                    # +1
    acts = eng.tick()
    token = next(a for a in acts
                 if a.kind == ActionKind.AI_PLAYER).payload["token"]
    eng._enter_revealing_locked(clk.t, "timeout", "")
    eng.submit_reveal("谜底", now=clk.t)
    clk.advance(60)
    eng.tick()
    eng.submit_riddle("第二题谜面。为什么？", "谜底", now=clk.t)
    # 旧 token + 旧 round + 旧 spec_key 的回包必须在身份门被挡下
    acts = eng.submit_ai_player_result(
        token, 1, "old-spec-key", "ask", "旧题问题", verdict="是", now=clk.t)
    check("旧回包不上屏", acts == [], kinds(acts))
    check("新题无残留预约",
          eng._ai_player_ledger.detective_reservation is None, led_snapshot(eng))


# ======================================================================
# §23/§25 effective elapsed 与单 tick 纪律
# ======================================================================
def test_effective_elapsed_advances_hints():
    """§23: real=240s + bonus=90s -> effective=330s >= Hint1。"""
    print("\n[LP-8] 点赞把 Hint 提前(时间轴读 effective)")
    eng, clk = boot(mkcfg(hint_seconds=300.0, restate_seconds=999999))
    clk.advance(240)
    check("真实 240s 还没有提示",
          not any(a.kind == ActionKind.HINT for a in eng.tick()))
    like(eng, 200)                    # 基线 0? 上面 boot 无 like; 首条 200=基线
    # 首条只建基线 -> 用第二条制造 bonus
    like(eng, 500)                    # +3 桶 = 90s bonus
    acts = eng.tick()
    hints = [a for a in acts if a.kind == ActionKind.HINT]
    check("effective=330s 立即满足 Hint1", len(hints) == 1, kinds(acts))
    eng.submit_hint("提示一")
    # §8: 真实冷却不加速 —— Hint1 刚上屏, effective 再高也不连发
    like(eng, 900)                    # +4 桶 = +120s bonus -> effective 远超 Hint2
    acts = eng.tick()
    check("Hint1 后 0 秒(真实): Hint2 不立即上",
          not any(a.kind == ActionKind.HINT for a in acts), kinds(acts))
    check("冷却按真实时间走", eng._hint_cooldown_until > clk.t,
          (eng._hint_cooldown_until, clk.t))


def test_single_tick_burst_no_spam():
    """§25: 大 burst 一次 tick 只推进一个时间轴动作。"""
    print("\n[LP-9] 大 burst 不连发")
    eng, clk = boot(mkcfg(hint_seconds=100.0, restate_seconds=999999,
                          hint_min_gap_seconds=45.0))
    clk.advance(60)
    like(eng, 100)                    # 基线 100(桶 1)
    like(eng, 5100)                   # +50 桶 = 1500s bonus
    acts = eng.tick()
    hint_cnt = len([a for a in acts if a.kind == ActionKind.HINT])
    reveal_cnt = len([a for a in acts if a.kind == ActionKind.REVEAL])
    check("一个 tick 至多一条 HINT", hint_cnt <= 1, kinds(acts))
    check("绝不 HINT + REVEAL 同 tick", not (hint_cnt and reveal_cnt),
          kinds(acts))
    check("真实 60s < 180s: 即使 effective 已爆表也不揭晓",
          reveal_cnt == 0 and eng.phase == Phase.QA,
          (reveal_cnt, eng.phase))
    if hint_cnt:
        eng.submit_hint("提示一")
    # 后续 tick 也受真实冷却约束, 不会每拍一条
    clk.advance(5)
    acts = eng.tick()
    check("5 秒后仍在真实冷却内, 不再派 HINT",
          not any(a.kind == ActionKind.HINT for a in acts), kinds(acts))


def test_effective_elapsed_ui_fields():
    """§7/§17: snapshot 时间轴与 Engine 同源; puzzle_elapsed_ms 仍是真实。"""
    print("\n[LP-10] 快照时间轴同源")
    eng, clk = boot(mkcfg(hint_seconds=300.0, restate_seconds=999999))
    like(eng, 100)                    # 基线 100
    like(eng, 400)                    # +3 桶 = 90s bonus
    clk.advance(10)                   # 真实 10s(_puzzle_started=0.0 时快照省略)
    s = eng.snapshot()
    check("next_event 指向 Hint1", s.next_event_kind == "hint", s.next_event_kind)
    check("倒计时按 effective(约 300-90-10=200s)",
          195 <= s.next_event_ms / 1000 <= 201, s.next_event_ms)
    check("puzzle_elapsed_ms 仍是真实时长(约 10s)",
          s.puzzle_elapsed_ms is not None
          and 9000 <= s.puzzle_elapsed_ms <= 11000,
          s.puzzle_elapsed_ms)
    like(eng, 1200)                   # +8 桶 = +240s -> 累计 bonus 330s
    s2 = eng.snapshot()
    check("加码后倒计时立即反映 effective(已过 Hint1 边界)",
          s2.timeline_slot >= 1, (s2.timeline_slot, s2.next_event_ms))
    check("不会显示负倒计时", s2.next_event_ms >= 0)


# ======================================================================
# §24 自动揭晓的真实时间最低保护
# ======================================================================
def test_auto_reveal_real_time_floor():
    """§24: real=60s + effective 爆表 -> 不揭晓; real 到 180s 才揭晓。"""
    print("\n[LP-11] 自动揭晓的 180s 真实底线")
    eng, clk = boot(mkcfg(hint_seconds=100.0, restate_seconds=999999))
    clk.advance(60)
    like(eng, 100)                    # 基线 100
    like(eng, 2000)                   # +19 桶 = 570s bonus -> effective=630 远超 400
    acts = eng.tick()
    check("真实 60s: 不自动揭晓",
          not any(a.kind == ActionKind.REVEAL for a in acts)
          and eng.phase == Phase.QA, (kinds(acts), eng.phase))
    clk.advance(120)                  # real = 180s
    acts = eng.tick()
    check("真实 180s: 揭晓放行",
          any(a.kind == ActionKind.REVEAL for a in acts)
          and eng.phase == Phase.REVEALING, (kinds(acts), eng.phase))


def test_human_completion_ignores_min_qa_floor():
    """§24: 真人真实通关立即揭晓, 不被 180s 底线扣住。"""
    print("\n[LP-12] 真人通关不受最低保护限制")
    clk = FakeClock()
    eng = RoundEngine(mkcfg(puzzle_min_qa_seconds=180.0,
                            hint_seconds=9999, restate_seconds=999999),
                      clock=clk)
    eng.start()
    spec = completion_spec()
    eng.submit_riddle(spec.puzzle, spec.answer, spec.hints, spec=spec, now=clk.t)
    assert eng.phase == Phase.QA
    clk.advance(20)                   # real 只有 20s
    # 走一遍真实的 提问 -> 派发 -> 裁决 链(submit_qa 只认在途条目)
    eng.submit_danmaku("u1", "甲", "#汤里有人肉吗")
    eng.tick()
    acts = eng.submit_qa([QAResult(
        qid=1, verdict="是", status="ok",
        established_fact_ids=["f1"],
        completion_verified_fact_ids=["f1"])], now=clk.t)
    check("真实 20s 通关合同覆盖 -> 立即揭晓",
          eng.phase == Phase.REVEALING
          and any(a.kind == ActionKind.REVEAL for a in acts),
          (eng.phase, kinds(acts)))


# ======================================================================
# §26 旧换题指令墓碑
# ======================================================================
def test_legacy_skip_tokens_are_tombstones():
    """§26: 五个旧命令 -> 不 reveal / 不进 QA / 不调 LLM; #提示 照常。"""
    print("\n[LP-13] 旧换题指令墓碑")
    eng, clk = boot(mkcfg())
    for tok in ("#下一题", "#下一关", "#换一题", "#跳过", "#next"):
        acts = eng.submit_danmaku("u1", "甲", tok)
        check(f"{tok}: 无动作", acts == [], kinds(acts))
        check(f"{tok}: 不进 QA 队列",
              len(eng._pending) == 0 and len(eng._inflight) == 0,
              (len(eng._pending), len(eng._inflight)))
    check("没有任何 reveal 发生",
          eng.phase == Phase.QA and eng._reveals == 0, eng.phase)
    check("墓碑计数 = 5", eng._legacy_skip_consumed == 5,
          eng._legacy_skip_consumed)
    # #提示 照常
    clk.advance(60)
    acts = eng.submit_danmaku("u1", "甲", "#提示")
    check("#提示 继续正常",
          any(a.kind == ActionKind.HINT for a in acts), kinds(acts))
    # 普通 #问题 继续正常进 QA
    eng.submit_danmaku("u2", "乙", "#他为什么自杀")
    eng.tick()
    check("普通提问继续正常",
          len(eng._pending) + len(eng._inflight) == 1,
          (len(eng._pending), len(eng._inflight)))


# ======================================================================
# §16 显式点赞公告事件
# ======================================================================
def test_like_notice_seq_and_aggregation():
    """§14/§15/§16: 一批一个公告; seq 单调可去重; 带 round identity。"""
    print("\n[LP-14] 公告事件身份与聚合")
    eng, clk = boot(mkcfg())
    like(eng, 100)                    # 基线
    like(eng, 350)                    # +2
    n1 = eng.snapshot().like_progress_notice
    check("×2 聚合成一条", n1 and n1["pulses"] == 2 and "×2" in n1["text"],
          n1)
    check("带 seq/round/phase", n1 and n1["seq"] >= 1
          and n1["round_index"] == 1 and n1["phase"] == "qa", n1)
    like(eng, 351)                    # 不过桶 -> 不产生新公告
    n_same = eng.snapshot().like_progress_notice
    check("不过桶不刷公告", n_same["seq"] == n1["seq"], (n1, n_same))
    like(eng, 950)                    # +6
    n2 = eng.snapshot().like_progress_notice
    check("新批次 seq 严格递增", n2["seq"] > n1["seq"], (n1["seq"], n2["seq"]))
    check("新批次聚合 ×6", n2["pulses"] == 6 and "×6" in n2["text"], n2)
    check("文案绝不含内部秒数",
          all("秒" not in n["text"] and "+30" not in n["text"]
              for n in (n1, n2)), (n1["text"], n2["text"]))


def test_config_roundtrip_and_validation():
    """§18: 两个新配置有默认值、走 CLI、负值有告警。"""
    print("\n[LP-15] 配置与 CLI")
    cfg = mkcfg()
    check("默认 30s", cfg.like_progress_seconds_per_bucket == 30.0,
          cfg.like_progress_seconds_per_bucket)
    check("默认 180s", cfg.puzzle_min_qa_seconds == 180.0,
          cfg.puzzle_min_qa_seconds)
    from story.config import build_parser
    ap = build_parser()
    a = ap.parse_args(["--stdin",
                       "--like-progress-seconds-per-bucket", "12.5",
                       "--puzzle-min-qa-seconds", "90"])
    check("CLI 解析 like-progress", a.like_progress_seconds_per_bucket == 12.5)
    check("CLI 解析 min-qa", a.puzzle_min_qa_seconds == 90)
    from story.config import from_args
    cfg2 = from_args(["--stdin", "--like-progress-seconds-per-bucket", "45",
                      "--puzzle-min-qa-seconds", "120"])
    check("from_args 接线(不是 dead config)",
          cfg2.like_progress_seconds_per_bucket == 45
          and cfg2.puzzle_min_qa_seconds == 120,
          (cfg2.like_progress_seconds_per_bucket, cfg2.puzzle_min_qa_seconds))
    eng, _ = boot(mkcfg(like_progress_seconds_per_bucket=0.0))
    like(eng, 100)
    like(eng, 300)                    # +2 桶但 bonus=0
    check("bonus=0 时 pulse 只给 AI 机会",
          pulses_of(eng) == 2 and eng._round_progress_bonus_seconds == 0.0,
          (pulses_of(eng), eng._round_progress_bonus_seconds))


def main():
    tests = [
        test_high_water_baseline_and_buckets,
        test_high_water_survives_round_change,
        test_round_opportunity_qa_credit_and_burst,
        test_round_opportunity_reset_on_new_round,
        test_setting_pulses_credit_current_round,
        test_revealing_revealed_pulses_not_credited,
        test_stale_reservation_cannot_pollute_new_round,
        test_effective_elapsed_advances_hints,
        test_single_tick_burst_no_spam,
        test_effective_elapsed_ui_fields,
        test_auto_reveal_real_time_floor,
        test_human_completion_ignores_min_qa_floor,
        test_legacy_skip_tokens_are_tombstones,
        test_like_notice_seq_and_aggregation,
        test_config_roundtrip_and_validation,
    ]
    for t in tests:
        t()
    print()
    if FAIL[0]:
        print(f"FAILED: {FAIL[0]} 项")
        return 1
    print("PASS: 点赞推进(Issue #43: pulse / round 资源 / effective elapsed "
          "/ 揭晓保护 / 墓碑 / 公告)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
