#!/usr/bin/env python
# coding: utf-8
"""运行: uv run tests/test_summon.py（完全离线, 无网络）。

Step 13A: SummonLedger + Like high-water。

这个套件钉住三件事:
  1. Like 用 **total 的 high-water** 换算, 且对重放/倒退幂等;
  2. 通用 earn/reserve/commit/release 的账不出错(不泄漏、不双花);
  3. **礼物在 13A 绝不换算** —— 收到 raw event 只计数, 返回 0。

第 3 条是重点: Step 12A 已确认「GiftMessage 数量 != 真实礼物单位数」,
在 12B 真实样本到手前, 任何把 combo/repeat/total 映射成额度的写法都是
重复计费。这里从 API 上就没有那条路。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from story.summon import LIKES_PER_SUMMON, SummonLedger  # noqa: E402

FAIL = [0]


def check(name, cond, extra=""):
    if cond:
        print(f"  ok  {name}")
        return
    print(f"  FAIL {name}  {extra}")
    FAIL[0] += 1


# ======================================================================
# Like high-water
# ======================================================================
def test_like_first_total_only_initializes():
    """首次 total 只设基线, **不补历史档位**。"""
    print("\n[S13-1] 首次 total 只初始化")
    led = SummonLedger()
    got = led.on_like_total(487)
    check("首次返回 0(不补历史)", got == 0, got)
    check("earned 仍为 0", led.summon_earned_total == 0,
          led.summon_earned_total)
    check("high_water 已设为 487", led.likes_total_high_water == 487,
          led.likes_total_high_water)
    check("桶数按 487 初始化(4)", led.likes_bucket_consumed == 4,
          led.likes_bucket_consumed)
    check("progress = 87", led.likes_progress == 87, led.likes_progress)


def test_like_487_to_523_gives_one():
    print("\n[S13-2] 487 -> 523 = +1")
    led = SummonLedger()
    led.on_like_total(487)
    got = led.on_like_total(523)
    check("+1", got == 1, got)
    check("earned == 1", led.summon_earned_total == 1, led.summon_earned_total)
    check("high_water == 523", led.likes_total_high_water == 523,
          led.likes_total_high_water)
    check("progress == 23", led.likes_progress == 23, led.likes_progress)


def test_like_90_to_520_gives_five():
    """90 已初始化 -> 520 = +5(跨过 4 个新档位)。"""
    print("\n[S13-3] 90 -> 520 = +5")
    led = SummonLedger()
    led.on_like_total(90)
    check("90 只初始化", led.summon_earned_total == 0,
          led.summon_earned_total)
    got = led.on_like_total(520)
    check("+5", got == 5, got)
    check("earned == 5", led.summon_earned_total == 5,
          led.summon_earned_total)
    check("progress == 20", led.likes_progress == 20, led.likes_progress)


def test_like_regression_is_not_recounted():
    """523 -> 320 -> 523 不重算(也不 rebase)。"""
    print("\n[S13-4] 523 -> 320 -> 523 = +0")
    led = SummonLedger()
    led.on_like_total(487)
    led.on_like_total(523)          # +1
    before = led.summon_earned_total
    check("倒退不产生新增", led.on_like_total(320) == 0)
    check("倒退后 high_water 不回退", led.likes_total_high_water == 523,
          led.likes_total_high_water)
    check("回到 523 也不产生新增", led.on_like_total(523) == 0)
    check("earned 未变", led.summon_earned_total == before,
          led.summon_earned_total)


def test_like_repeated_total_is_idempotent():
    """重连重放(同一条 total 再来一遍)-> 不增。"""
    print("\n[S13-5] 重复 total 不增")
    led = SummonLedger()
    led.on_like_total(487)
    led.on_like_total(523)
    n = led.summon_earned_total
    for _ in range(10):
        check_ = led.on_like_total(523)
        if check_ != 0:
            check("重复 total 恒返回 0", False, check_)
            break
    else:
        check("重复 total 恒返回 0", True)
    check("earned 不变", led.summon_earned_total == n, led.summon_earned_total)


def test_like_crossing_multiple_hundreds():
    print("\n[S13-6] 523 -> 601 = +1")
    led = SummonLedger()
    led.on_like_total(487)
    led.on_like_total(523)          # +1
    got = led.on_like_total(601)
    check("+1", got == 1, got)
    check("earned == 2", led.summon_earned_total == 2,
          led.summon_earned_total)
    check("progress == 1", led.likes_progress == 1, led.likes_progress)


def test_like_big_jump():
    """一次跨很多档(比如 0 -> 1000)。"""
    print("\n[S13-7] 大跨度")
    led = SummonLedger()
    led.on_like_total(0)            # 初始化在 0
    check("0 初始化不补", led.summon_earned_total == 0)
    got = led.on_like_total(1000)
    check("1000 -> +10", got == 10, got)


def test_like_never_uses_count():
    """`on_like_total` 只接 total —— API 上没有 count 累加的路。"""
    print("\n[S13-8] 没有 count 累加入口")
    import inspect
    params = set(inspect.signature(SummonLedger.on_like_total).parameters)
    check("形参只有 total", params == {"self", "total", "now"}, params)
    check("没有 count 形参", "count" not in params, params)


# ======================================================================
# 通用账本
# ======================================================================
def test_earn_and_available():
    print("\n[S13-9] earn / available")
    led = SummonLedger()
    check("初始 available=0", led.available == 0)
    led.earn(3)
    check("earn(3) 后 earned=3", led.summon_earned_total == 3,
          led.summon_earned_total)
    check("available=3", led.available == 3)
    check("earn(0) 不加", led.earn(0) == 0)
    check("earn(-5) 不加", led.earn(-5) == 0)
    check("earned 仍为 3", led.summon_earned_total == 3)


def test_reservation_holds_capacity():
    """预约占用 available, 但不消耗 earned。"""
    print("\n[S13-10] 预约占用额度")
    led = SummonLedger()
    led.earn(2)
    check("预约成功",
          led.reserve("t1", round_index=1, spec_key="k1") is True)
    check("available 扣到 1", led.available == 1, led.available)
    check("unconsumed 不变(还没结算)", led.unconsumed == 2, led.unconsumed)
    check("consumed 仍 0", led.summon_consumed_total == 0)
    r = led.detective_reservation
    check("三个身份字段都记下了",
          r.token == "t1" and r.round_index == 1 and r.spec_key == "k1", r)
    check("Reservation 没有 amount 字段",
          not hasattr(r, "amount"), dir(r))


def test_reservation_requires_all_three_identity_fields():
    """**Batch C closeout**: token / round_index / spec_key **全部必填**。

    早先 `round_index=0, spec_key=""` 有默认值, 值空也放行 —— 于是
    Step 14 的迟到回调只要 token 撞上就能兑现别人的预约, 而空 spec_key
    让"哪一稿"这一维整条失效。冻结的契约是三者一致, 不是"愿意传就查"。
    """
    print("\n[S13-10b] 三要素必填")
    led = SummonLedger()
    led.earn(1)
    # 位置参数必填 -> 少传是 TypeError
    try:
        led.reserve("t1")                      # type: ignore[call-arg]
        check("少传身份字段应 TypeError", False, "没有报错")
    except TypeError:
        check("少传身份字段 -> TypeError", True)
    # 传了但为空 -> 拒绝(必填但传空等于没填)
    check("空 spec_key 被拒",
          led.reserve("t1", round_index=1, spec_key="") is False)
    check("空 token 被拒",
          led.reserve("", round_index=1, spec_key="k") is False)
    check("空 round 被拒",
          led.reserve("t", round_index=None, spec_key="k") is False)
    check("没有产生预约", led.detective_reservation is None)
    # 三个都给了才成功
    check("三要素齐全 -> 成功",
          led.reserve("t1", round_index=1, spec_key="k1") is True)


def test_commit_and_release_require_all_three():
    """commit/release 也必须**三者全给**且全部匹配。"""
    print("\n[S13-10c] commit/release 三要素严格匹配")
    led = SummonLedger()
    led.earn(1)
    led.reserve("t1", round_index=7, spec_key="K7")
    for fn_name in ("commit", "release"):
        fn = getattr(led, fn_name)
        try:
            fn("t1")                            # type: ignore[call-arg]
            check(f"{fn_name} 少传应 TypeError", False, "没有报错")
        except TypeError:
            check(f"{fn_name} 少传 -> TypeError", True)


def test_commit_consumes():
    print("\n[S13-11] commit 消耗")
    led = SummonLedger()
    led.earn(2)
    led.reserve("t1", round_index=1, spec_key="k1")
    check("commit 成功",
          led.commit("t1", round_index=1, spec_key="k1") is True)
    check("consumed == 1", led.summon_consumed_total == 1,
          led.summon_consumed_total)
    check("预约已清", led.detective_reservation is None)
    check("available == 1", led.available == 1, led.available)


def test_release_refunds():
    """release 退回额度(不消耗)。"""
    print("\n[S13-12] release 退回")
    led = SummonLedger()
    led.earn(1)
    led.reserve("t1", round_index=1, spec_key="k1")
    check("release 成功",
          led.release("t1", round_index=1, spec_key="k1") is True)
    check("consumed 仍 0", led.summon_consumed_total == 0,
          led.summon_consumed_total)
    check("available 回到 1", led.available == 1, led.available)


def test_reservation_cannot_be_stolen_by_stale_callback():
    """跨题/跨稿的迟到回调不能兑现或释放别人的预约。"""
    print("\n[S13-13] 迟到回调挡得住")
    led = SummonLedger()
    led.earn(1)
    led.reserve("t1", round_index=7, spec_key="K7")
    check("错 token 的 commit 失败",
          led.commit("tX", round_index=7, spec_key="K7") is False)
    check("错 round 的 commit 失败",
          led.commit("t1", round_index=8, spec_key="K7") is False)
    check("错 spec_key 的 commit 失败",
          led.commit("t1", round_index=7, spec_key="K9") is False)
    check("预约仍在", led.detective_reservation is not None)
    check("错 round 的 release 也失败",
          led.release("t1", round_index=8, spec_key="K7") is False)
    check("预约仍在", led.detective_reservation is not None)
    check("正确的仍能兑现",
          led.commit("t1", round_index=7, spec_key="K7") is True)


def test_one_reservation_consumes_exactly_one():
    """一次召唤恒占 1 —— 不存在"一次预约消费多个"的路。"""
    print("\n[S13-13b] 一次召唤恒占 1")
    led = SummonLedger()
    led.earn(5)
    led.reserve("t1", round_index=1, spec_key="k1")
    check("available 只扣 1", led.available == 4, led.available)
    led.commit("t1", round_index=1, spec_key="k1")
    check("consumed 恰好 +1", led.summon_consumed_total == 1,
          led.summon_consumed_total)
    check("consequently available == 4", led.available == 4, led.available)
    # API 上没有 amount 旋钮
    import inspect
    for fn in (SummonLedger.reserve, SummonLedger.commit, SummonLedger.release):
        check(f"{fn.__name__} 没有 amount 形参",
              "amount" not in inspect.signature(fn).parameters,
              sorted(inspect.signature(fn).parameters))


def test_cannot_double_reserve():
    """已有占用时不能再占(否则前一 token 永久泄漏)。"""
    print("\n[S13-14] 不能重复预约")
    led = SummonLedger()
    led.earn(5)
    check("第一次成功", led.reserve("t1", round_index=1, spec_key="k1") is True)
    check("第二次被拒",
          led.reserve("t2", round_index=1, spec_key="k1") is False)
    check("原预约没被覆盖", led.detective_reservation.token == "t1",
          led.detective_reservation.token)
    check("available 只扣 1", led.available == 4, led.available)


def test_cannot_reserve_without_capacity():
    print("\n[S13-15] 没额度不能预约")
    led = SummonLedger()
    check("0 额度 -> 失败",
          led.reserve("t1", round_index=1, spec_key="k1") is False)
    check("没有产生预约", led.detective_reservation is None)
    led.earn(1)
    check("有额度 -> 成功",
          led.reserve("t1", round_index=1, spec_key="k1") is True)


# ======================================================================
# 礼物: 13A 绝不换算
# ======================================================================
def test_gift_event_never_earns():
    """**核心**: 收到礼物事件不产生任何额度。"""
    print("\n[S13-16] 礼物事件不产生额度")
    led = SummonLedger()
    for _ in range(50):
        got = led.on_gift_event(object())
        if got != 0:
            check("on_gift_event 恒返回 0", False, got)
            break
    else:
        check("on_gift_event 恒返回 0", True)
    check("earned 仍为 0", led.summon_earned_total == 0,
          led.summon_earned_total)
    check("只计数", led.gift_events_seen == 50, led.gift_events_seen)


def test_gift_combo_counts_never_reach_earned():
    """带 combo/repeat/total 字段的事件也**不得**换算。

    这些字段现在只是原始数据 —— Step 12B 之前没人知道哪个是增量。
    """
    print("\n[S13-17] combo/repeat/total 不换算")
    led = SummonLedger()

    class _Ev:
        combo_count = 3
        repeat_count = 3
        total_count = 99
        gift_id = "1"

    for _ in range(20):
        led.on_gift_event(_Ev())
    check("earned 仍为 0", led.summon_earned_total == 0,
          led.summon_earned_total)
    check("seen == 20", led.gift_events_seen == 20, led.gift_events_seen)


def test_ledger_has_no_gift_earn_api():
    """账本上不能存在"礼物 -> earn"的入口。"""
    print("\n[S13-18] 没有礼物换算的 API")
    names = [n for n in dir(SummonLedger) if not n.startswith("__")]
    for bad in ("on_gift", "earn_gift", "add_gift", "gift_summon"):
        check(f"没有 {bad}", bad not in names, names)


# ======================================================================
# 跨题 / 即时反馈
# ======================================================================
def test_ledger_survives_puzzle_change_and_reveal():
    """跨题不清零、reveal 不清零(账本是全局的, 不属于某一题)。"""
    print("\n[S13-19] 跨题/揭晓不清零")
    led = SummonLedger()
    led.on_like_total(0)
    led.on_like_total(250)          # +2
    check("earned == 2", led.summon_earned_total == 2)
    # 模拟"换题 / 揭晓"——账本没有这些方法, 也不该有
    for bad in ("reset", "on_puzzle_start", "on_reveal", "clear"):
        check(f"没有 {bad}(不会因换题/揭晓重置)",
              not hasattr(led, bad), bad)
    check("earned 未变", led.summon_earned_total == 2)


def test_notice_seq_increments():
    print("\n[S13-20] 即时反馈序号")
    led = SummonLedger()
    check("初始 seq=0", led.interaction_notice_seq == 0)
    check("set_notice 返回 1", led.set_notice("有人召唤了搭子") == 1)
    check("文本已存", led.interaction_notice_text == "有人召唤了搭子",
          led.interaction_notice_text)
    check("再设 -> 2", led.set_notice("x") == 2)


def test_snapshot_shape():
    print("\n[S13-21] 快照字段")
    led = SummonLedger()
    led.on_like_total(0)
    led.on_like_total(523)
    snap = led.snapshot()
    for key in ("earned", "consumed", "unconsumed", "available",
                "reservation", "likes_total_high_water",
                "likes_bucket_consumed", "likes_progress",
                "gift_events_seen", "notice_seq", "notice_text"):
        check(f"有 {key}", key in snap, sorted(snap))
    check("progress == 23", snap["likes_progress"] == 23,
          snap["likes_progress"])
    check("reservation 初始为 None", snap["reservation"] is None)


def test_constants():
    print("\n[S13-22] 常量")
    check("100 赞 = 1", LIKES_PER_SUMMON == 100, LIKES_PER_SUMMON)


def main():
    tests = [
        test_like_first_total_only_initializes,
        test_like_487_to_523_gives_one,
        test_like_90_to_520_gives_five,
        test_like_regression_is_not_recounted,
        test_like_repeated_total_is_idempotent,
        test_like_crossing_multiple_hundreds,
        test_like_big_jump,
        test_like_never_uses_count,
        test_earn_and_available,
        test_reservation_holds_capacity,
        test_commit_consumes,
        test_release_refunds,
        test_reservation_cannot_be_stolen_by_stale_callback,
        test_reservation_requires_all_three_identity_fields,
        test_commit_and_release_require_all_three,
        test_one_reservation_consumes_exactly_one,
        test_cannot_double_reserve,
        test_cannot_reserve_without_capacity,
        test_gift_event_never_earns,
        test_gift_combo_counts_never_reach_earned,
        test_ledger_has_no_gift_earn_api,
        test_ledger_survives_puzzle_change_and_reveal,
        test_notice_seq_increments,
        test_snapshot_shape,
        test_constants,
    ]
    for t in tests:
        t()
    print()
    if FAIL[0]:
        print(f"FAILED: {FAIL[0]} 项")
        return 1
    print("PASS: SummonLedger(13A: Like high-water + 通用账本, 礼物不换算)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
