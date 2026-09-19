#!/usr/bin/env python
# coding: utf-8
"""后台补池(Q9)—— 只在直播空闲时预生成题目、add() 进题池。

## 最高优先级不变量

    **本模块只往 Q8 已经定义好的 Pool 边界里生产库存。**
    绝不改变 pop_next() / used ledger / fail-closed / 交付事务语义。

具体到代码, 这意味着本文件里**没有**、也**不该有**对
`pop_next` / `mark_used` / `remember_avoid` / `load` / `submit_riddle`
的调用, 更不该写 `pool._used_trustworthy`。复核时 grep 这几项即可。

## 为什么它单独一个模块

补池需要 phase / 压力 / gen_spec / blueprint 调度, 而 `PuzzlePool` 是个
"绝不抛异常、绝不阻塞直播"的纯存储组件 —— 把这些塞进池子会让后者反向
依赖 engine/writer, 也破坏"池子能被脚本独立使用"的性质。

放在 `Director` 里也能跑, 但本模块有**独立的线程模型**(executor 线程 ⇄
tick 线程)和一整套状态机, 值得单独一个文件级 docstring; 而且抽出来之后
它可以只用假协作者单测 —— 不需要 Director, 不需要 monkeypatch Thread。

## 状态机(契约)

    IDLE
      ↓ 库存 < 低水位 **或** 下一题此刻没得播(且未到硬上限)
    REFILL_ACTIVE
      ↓ 满足低压力条件且无在途任务
    GENERATING_ONE
      ↓ add 成功
    REFILL_ACTIVE
      ↓ 库存 >= 高水位 **且** 下一题有得播
    IDLE

    库存 >= 硬上限(pool_max_size)
      ↓ **无条件**停下(即使 playable 仍为 0, 只 warning) —— 见下
    IDLE

    任何生成/add 失败
      ↓
    BACKOFF(递增: 30/60/120/240/300, 成功后归零)
      ↓ 到期
    REFILL_ACTIVE

    **让路(interrupted)** —— 直播变忙, gen_spec 协作式收手
      ↓
    REFILL_ACTIVE(**不退避、不计失败**) —— 它不是故障

## G1: 后台生成必须能在**跑到一半**时让路

上面那张状态机只描述"这一拍要不要启动"。实播事故发生在**两次 tick
之间**:

    18:44:49 prefetch 在 REVEALED 启动(当时确实空闲)
    18:45:07 揭晓结束、下一题开始(SETTING), 池里没题 -> live 现场生成
    18:45:58 旧 prefetch 才跑完第 4 稿失败

两个生成同时占网关 51 秒。之后又反复出现"开 -> 50 秒后败"的循环。

根因不是"判定写错了", 而是**判定只在 tick 里跑**(4Hz 的决策点),
而一次 gen_spec 内部有 4~8 次昂贵调用, 每次之间都可能跨过相位切换。
所以 G1 给 `gen_spec` 加了协作式取消: 每次尚未发出的昂贵调用之前
都问一次 `_should_continue`, 说停就停。已经发出去的 HTTP 请求无法
取消 —— 等它回来即可, 关键是**它回来之后不能再发下一次**。

## G1: 后台用**自己的**预算

`gen_spec` 的默认参数(4 稿 / 90s)是**直播现场出题**的预算: 那时观众
在干等, 多试一稿值得。后台补池的收益上限只是"池子里多一道题", 代价
却是与直播抢网关、以及跨过下一题的 deadline。所以后台是 2 稿 / 25s
—— **少尝试, 不是降低题质**(硬门一道不少)。

## 四条设计要点

1. **min/target 是真正的滞回(latch)**。每拍判一次 `stock < min` 是错的:
   1 补成 2 就停了, `pool_target_size` 永远没有意义。

2. **滞回看两个量: stock 与 playable**。`stock_count()` 是长期库存,
   `playable_count()` 是"下一题此刻能不能播"。实播踩过的坑: 6 道候选
   全被当前窗口挡住 -> 回落现场生成、观众干等, 而 stock=6 让补池
   认为健康, 一道都不补。所以启动/停止都同时看这两个(见 `_on_tick_locked_ish`)。

3. **硬上限兜底**。`playable=0` 也可能是"被某个窗口条件整体挡住",
   此时补进来的新题会被同一条件挡住 —— 没有上限就是无限烧配额而
   playable 一动不动。到 `pool_max_size` 就停, 只 warning。

4. **单飞靠 `self._future`, 不靠 `max_workers=1`**。后者只保证"同时执行
   一个", 挡不住 tick 往队列里排 30 个任务。

5. **一次只生成一道**。直播突然忙起来时, 最多只有一道已经发出的生成
   请求无法取消; 不会有 3–4 道连着打完。下一道必须等新的 tick 重新
   确认(QA + 零压力 + 无在途 + latch 仍 active)—— 这才叫低优先级。

## 允许补池的相位

    QA        严格: pending/inflight 为 0 且无 hint/reveal 在途
    REVEALED  允许: pending/inflight 为 0, 不看 hint_inflight

REVEALED 的 30 秒展示窗是**最好的**生成时机 —— 引擎完全空闲, 而且
有很大概率赶在下一题就位之前完成, 下一题于是直接 pop 池子瞬时切题。
SETTING(直播自己在出题)与 REVEALING(揭晓可能仍在生成)**明确禁止**。

## 为什么用轮询 future.done() 而不是 add_done_callback

回调在 **executor 线程**上触发, 会和 tick 线程并发读写 latch。回调若也
抢锁, 而 on_tick 正持锁, 就是死锁; 不抢锁则是静默数据竞争。

轮询让 **latch/退避/计数器只有一个写者(tick 线程)**, 这正是让滞回可
审计、可单测的性质。worker 唯一的写口是 `_pending_result` 那个槽位。

零新依赖。
"""

from __future__ import annotations

import logging
import random
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Optional

from .playtest import (
    INTERRUPTED as OUTCOME_INTERRUPTED,
    PASS as OUTCOME_PASS,
    UNAVAILABLE as OUTCOME_UNAVAILABLE,
    UNSOLVED as OUTCOME_UNSOLVED,
)

log = logging.getLogger("story.prefetch")

#: `_future` 的占位标记: "已经决定要提交, 但还没拿到 Future"。
#:
#: 为什么需要它: 提交必须在锁外做(见 `_on_tick_locked_ish`), 于是出锁到
#: 真提交之间有一个窗口。没有标记的话, 第二拍进来会看到 `_future is None`
#: 而重复起一个任务。它**不是** Future, 所以任何 `is not None` 的单飞判断
#: 都会正确地把它当成"在途"。
_PENDING = object()


class PoolPrefetcher:
    """题池后台补池。**绝不抛、绝不阻塞 tick。**"""

    def __init__(self, cfg: Any, pool: Any, writer: Any, probe: Callable,
                 probe_inputs: Callable, pick_blueprint: Callable,
                 rng: Optional[random.Random] = None,
                 executor: Any = None, clock: Callable = time.monotonic,
                 playtester: Any = None):
        self.cfg = cfg
        self.pool = pool
        self.writer = writer
        self._probe = probe
        self._probe_inputs = probe_inputs
        self._pick_blueprint = pick_blueprint
        self._clock = clock
        #: AI 试玩(Q10)。None = 不试玩(默认)。由 Director 在
        #: `playtest_enabled` 时注入 —— 这里**不自己 new**, 因为
        #: Playtester 需要 host_writer(本模块的 writer)和
        #: should_continue(本模块的压力探针), 装配权在调用方。
        self._playtester = playtester

        self._min_size = max(0, int(getattr(cfg, "pool_min_size", 2) or 0))
        self._target_size = max(0, int(getattr(cfg, "pool_target_size", 5) or 0))
        #: "下一题此刻能播"的最低要求。0 = 关掉这个触发条件。
        self._playable_min = max(
            0, int(getattr(cfg, "pool_playable_min", 1) or 0))
        #: 硬上限。到这儿就停, **即使 playable 还是 0** —— 见 `on_tick`。
        #: 兜底取 target: max 没配时不该比 target 更小(那会让滞回失效)。
        self._max_size = max(
            self._target_size, int(getattr(cfg, "pool_max_size", 10) or 0))
        # ---- U1: 揭晓窗口专用目标 ----
        # QA 期间补池要和直播抢网关, 目标保守; REVEALED 是引擎**完全空闲**
        # 的 60 秒(观众在看答案, 没有任何在途请求), 这时把目标抬高,
        # 让"看答案 -> 下一题直接出现"真正成立。
        # 兜底取 target: 没配时不该比 QA 期间还低(那会让揭晓窗口白费)。
        self._reveal_target = max(
            self._target_size,
            int(getattr(cfg, "pool_reveal_target_size", 7) or 0))
        self._reveal_playable_target = max(
            self._playable_min,
            int(getattr(cfg, "pool_reveal_playable_target", 2) or 0))
        self._reveal_guard_s = max(
            0.0, float(getattr(cfg, "pool_reveal_start_guard_seconds", 30.0)
                       or 0.0))
        # ---- G1: 后台补池的独立预算 ----
        # 过去 prefetch 直接调 `gen_spec()` 的默认参数(max_attempts=4 /
        # budget_s=90) —— 那是**直播现场出题**的预算。live 出一道题观众
        # 就在干等, 值得多试几稿; 后台补池只是"有空补一道", 多试一稿的
        # 全部收益是池子里多一道题, 代价却是与直播抢网关 + 跨过 deadline
        # 继续跑。所以后台**少尝试**, 不是降低题质 —— 硬门一道不少。
        self._prefetch_attempts = max(
            1, int(getattr(cfg, "pool_prefetch_max_attempts", 2) or 2))
        self._prefetch_budget = max(
            1.0, float(getattr(cfg, "pool_prefetch_budget_seconds", 25.0)
                       or 25.0))
        self._guard_margin = max(
            0.0, float(getattr(cfg, "pool_prefetch_guard_margin_seconds", 5.0)
                       or 0.0))
        # ---- G1: guard 必须至少覆盖一轮预算 ----
        # 只挡"启动"是不够的: 实播里 guard=15s 而一轮 prefetch 可能跑
        # 几十秒, 于是"只剩 18 秒"照样启动一个注定跨过 deadline 的后台
        # 任务, 它跑着跑着下一题已经开始现场生成了 —— 两边同时占网关
        # 51 秒。这里取 max() 兜底, 免得"调大了 budget 却忘了调 guard"
        # 静默退回旧行为。配置侧会为此出一条告警(见 config.validate)。
        self._effective_guard_s = max(
            self._reveal_guard_s, self._prefetch_budget + self._guard_margin)
        self._backoff_s = float(getattr(cfg, "pool_prefetch_backoff_s", 30.0) or 30.0)
        # ---- G1: 连续失败的退避序列 ----
        # 固定 30 秒会让同一个上下文(同样的 recent window / 同样的配额
        # 饱和)被反复重试, 每次都烧完整一轮预算。改成递增, 成功后归零。
        _sched = tuple(getattr(cfg, "pool_prefetch_backoff_schedule_s",
                               None) or ())
        self._backoff_schedule = tuple(
            float(x) for x in _sched if float(x) > 0) or (self._backoff_s,)

        # 补池用**独立**的 rng。共用 Director 的 _rng 会让 live 路径的
        # blueprint 序列随"补池开不开"而变 —— 那既难排查, 也让
        # "同 seed 下补池不影响出题" 这个可测性质消失。
        self._rng = rng if rng is not None else random.Random()

        # 自己起 executor(不借 Director 的): 它是本模块的内部实现细节,
        # 而且 max_workers=1 只是第二道防线, 单飞由 _future 保证。
        self._executor = executor or ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="prefetch")

        # ---- 状态(全部由下面这把锁保护) ----
        # 刻意**不**复用 Director 的 _narrating: 那个锁是出题用的, 补池
        # 持它会把 live 出题挤成"推迟到下一拍" —— 优先级完全倒过来。
        self._lock = threading.Lock()
        self._future: Optional[Future] = None
        self._refill_active = False        # latch(滞回)
        self._retry_at = 0.0               # 退避到期时刻(monotonic)
        #: G1: 连续失败次数。`interrupted`(让路)**不计** —— 见
        #: `_apply_result`。它在 `_fail_streak` 上的效果必须是
        #: "什么都没发生", 否则"直播很忙"会被记成"补池一直在失败"。
        self._fail_streak = 0
        #: G1: 上一次提交时看到的"场景指纹"(puzzle_index)。新一题正式
        #: 开始后, 生成约束环境(recent window)整体变了 —— 此时允许把
        #: 长退避**重置一次**, 而不是机械地等满 300 秒再试同一个上下文。
        self._scene_at_submit = 0
        self._pending_result = None        # worker 的终局结果, 由 tick 取走
        self._last_fail = ""
        #: 硬上限 warning 的节流时刻(monotonic)。tick 4Hz, 不节流会把
        #: "到顶了但还是没得播"刷成日志洪水。
        self._max_warn_at = 0.0
        self._max_warn_s = 60.0
        # ---- 计数(全部由 tick 线程单写) ----
        # 拆得比"一个 fail_count"细, 因为后续排查时**故障发生在哪个
        # 阶段**是最有价值的信息: 生成失败是出题质量问题, add 失败是
        # 存储/校验边界问题, 异常是代码 bug, 试玩没过是收敛性问题。
        # 合并成一个数字就再也拆不开了。
        self.added_count = 0               # 最终成功写进 Pool 的题数
        self.generation_fail_count = 0     # gen_spec 明确失败
        self.add_fail_count = 0            # 生成/试玩都过了, 但 pool.add 失败
        self.exception_count = 0           # worker 非预期异常
        self.playtest_pass_count = 0
        self.playtest_unsolved_count = 0
        self.playtest_unavailable_count = 0
        self.playtest_interrupted_count = 0
        #: G1: gen_spec 因"直播变忙"协作式收手的次数。**与
        #: generation_fail_count 分开** —— 前者是优先级让路(正常),
        #: 后者是题不合格(故障)。合成一个数字之后, "补池失败率"就
        #: 只反映直播活跃度了。
        self.interrupted_count = 0
        self.skip_count = 0

    # ------------------------------------------------------------------
    def _enabled(self) -> bool:
        """补池能不能跑。

        `no_llm` 时 `writer is None` —— 必须**彻底不生成**, 绝不能落进
        假题分支把兜底题灌进池子(那会污染真实题池)。
        """
        if self.pool is None or self.writer is None:
            return False
        if not getattr(self.cfg, "pool_prefetch_enabled", True):
            return False
        return True

    # ------------------------------------------------------------------
    def on_tick(self) -> None:
        """tick 线程调用。**绝不阻塞, 绝不抛。**

        放在 Director._scheduler 的 tick 循环之后: 这一拍的动作(可能
        刚结束一次 REVEAL、也可能刚起了新 RIDDLE)已经生效, 再决定
        补不补。
        """
        if not self._enabled():
            return
        try:
            self._on_tick_locked_ish()
        except Exception:                       # noqa: BLE001
            # 补池绝不能影响直播主循环 —— 任何异常都吞在这里。
            log.exception("补池 tick 决策异常(忽略)")

    def _on_tick_locked_ish(self) -> None:
        """决策。**锁只包住状态读写, 不包住 submit。**

        为什么 submit 必须在锁外:
          - 真实 executor 的 `submit()` 在队列满时可以阻塞, 持锁阻塞会
            把 tick 线程卡住 —— 那正是"补池不能影响直播"要避免的;
          - 同步执行的替身(测试里那种)会在 `submit()` 里**就地**跑完
            worker, 而 worker 结尾要拿同一把锁写 `_pending_result` ——
            非重入锁上直接死锁。
        所以这里先把该读的读完、该占的标记占掉, 出了锁再真正提交。
        """
        now = self._clock()
        to_submit = None
        with self._lock:
            # ---- ① 回收已完成的 future ----
            # 只把句柄置 None; 状态转移在 ② 做, 让 latch 保持单写者。
            # `_PENDING` 是刚占的位、还没有 Future, 跳过。
            fut = self._future
            if fut is not None and fut is not _PENDING and fut.done():
                self._future = None

            # ---- ② 应用 worker 的终局结果 ----
            res, self._pending_result = self._pending_result, None
            if res is not None:
                kind, detail, extra = res
                self._apply_result(kind, detail, extra, now)

            # ---- ③ 退避 ----
            if now < self._retry_at:
                # ---- G1: 场景变了就允许重置一次长退避 ----
                # 退避的初衷是"别在同一个坏上下文里反复烧钱"。但如果
                # 期间**新一题正式开始**了, recent window / 配额饱和状态
                # 已经整体换了一批 —— 机械地等满 300 秒只是白等。
                #
                # 只重置到**第一档**(不是直接清零): 立刻重试一个刚失败
                # 过的环境仍然是错的, 但等 30 秒是合理的。
                #
                # 判据用 puzzle_index(单调递增的场景指纹), 不是"时间到了"
                # —— 后者会让退避序列形同虚设。
                if self._fail_streak > 1:
                    cur = self._scene_of(self._probe_safe())
                    if cur is not None and cur != self._scene_at_submit:
                        self._scene_at_submit = cur
                        self._fail_streak = 1
                        self._retry_at = now + self._backoff_for_streak(1)
                        log.info("补池: 直播已进入新一题(场景指纹 %s), "
                                 "长退避重置为第一档 %.0fs",
                                 cur, self._backoff_for_streak(1))
                if now < self._retry_at:
                    return

            # ---- ④ 账本不可信 -> 完全不补池 ----
            # 此时 pop_next 一道都不交付, 灌题只是白烧网关配额。
            if not self._ledger_ok():
                return

            # ---- ⑤ 取快照(库存与"能不能播"必须看同一刻) ----
            # 只取一次: `playable_count` 与随后的 `_generate_one` 用**同一份**
            # recent/avoid。若 probe 用快照 A 判"还够播"、而生成时又读快照 B,
            # 两拍的窗口差异会让补池照着一个过期判断跑。
            inputs = self._generation_inputs()

            # ---- ⑥ 库存 / 可播数 ----
            # ⚠️ C3: 阶段目标必须在 probe **之前**算出来 —— `_effective_targets()`
            # 在 REVEALED 下要 2 道可播, 而 `playable_count(limit=N)` 是
            # 硬早退(`n >= limit` 就 break)。传 1 就永远数不到 2, latch
            # 的"可播够了"停止条件永不成立 -> 一路补到硬上限。
            #
            # 所以这两行**不能**保持原来的顺序(先 probe 再在 ⑧ 里算目标)。
            target, need_playable = self._effective_targets()
            stock = self._stock()
            if stock is None:
                return
            playable = self._playable(inputs, limit=need_playable)

            # ---- ⑦ 硬上限(先于 latch 判) ----
            # 到顶就彻底停, 即使 playable 仍是 0。理由见 config 里
            # `pool_max_size` 的说明: 若这批题是被某个窗口条件整体挡住,
            # 新补的题会被同一条件挡住 —— 无上限就是无限烧配额而
            # playable 一动不动。这里只 warning, 让运维看得见"补了没用"。
            if stock >= self._max_size:
                if self._refill_active:
                    self._refill_active = False
                if playable < self._playable_min:
                    if now >= self._max_warn_at:
                        # 节流: tick 是 4Hz, 不节流会把这行刷成日志洪水。
                        self._max_warn_at = now + self._max_warn_s
                        # C3: 这里刻意仍用 `_playable_min`(最低生存要求),
                        # 不用 `need_playable`(当前阶段目标)。到硬上限还
                        # 值得报警的是"连一道都播不出来", 而不是"没凑够
                        # 揭晓期的 2 道" —— 后者只是没赚到额外余量。
                        log.warning(
                            "题池达到硬上限(%d)但当前仍无可播题"
                            "(stock=%d playable=%d) —— 停下, 不再生成。"
                            "多半是当前窗口把所有候选都挡住了, "
                            "继续补也补不出去。", self._max_size, stock, playable)
                else:
                    log.info("补池周期结束: 库存 %d >= 硬上限 %d",
                             stock, self._max_size)
                return

            # ---- ⑧ latch(滞回) ----
            # 启动条件: 长期库存见底 **或** 下一题此刻没得播。
            # 停止条件: 库存到高水位 **且** 下一题有得播。
            #
            # 为什么停止要 `and` 而不是 `or`: 只满足一个就停, 会在
            # "库存够但全被当前窗口挡住"时提前收工 —— 那正是实播里
            # "6 道候选全被挡、回落现场生成"的场景。反过来, 只按
            # playable 判启动会让窗口拥挤时狂补(盘上其实堆满了),
            # 所以启动也保留 stock < min 这条腿。
            #
            # ---- U1: REVEALED 用更高的目标 ----
            # 揭晓窗口是引擎完全空闲的 60 秒, 不抢网关, 所以目标抬高到
            # `_reveal_target` / `_reveal_playable_target`。其余阶段沿用
            # QA 的保守目标。"多播一道"的判定也更容易 —— 只要 playable
            # 还没到 reveal 目标就继续补。
            #
            # ⚠️ C3: `target` / `need_playable` 已在 ⑥ 之前算好(并用于
            # 那次 probe 的 limit), 这里只消费, 不要重算 —— 重算本身
            # 没错, 但会让人以为 limit 是别处定的。
            need = (stock < self._min_size
                    or (playable < need_playable
                        and stock < self._max_size))
            if not self._refill_active and need:
                self._refill_active = True
                log.info("补池周期启动: 库存 %d < 低水位 %d, 或 可播 %d < %d",
                         stock, self._min_size, playable, need_playable)
            elif self._refill_active and (
                    stock >= target and playable >= need_playable):
                self._refill_active = False
                log.info("补池周期结束: 库存 %d >= 高水位 %d 且 可播 %d >= %d",
                         stock, target, playable, need_playable)
                return
            if not self._refill_active:
                self.skip_count += 1
                return

            # ---- ⑨ 单飞 ----
            if self._future is not None:
                return

            # ---- ⑩ 低压力门 ----
            if not self._low_pressure():
                return

            # ---- ⑩b U1: 临近下一题就不再**启动**新请求 ----
            # 60 秒到点时下一题**绝不能等待** future: 池里有就直接上,
            # 没有就回落现场生成。留 `pool_reveal_start_guard_seconds`
            # 秒的余量, 免得 deadline 那一刻正好挂着一个跑了一半的任务。
            # **在途的不用强杀** —— 它跑完就进池子, 下一题用不上也无妨。
            if self._deadline_too_close():
                return

            # ---- ⑪ 占住"在途"标记 ----
            # 快照在 ⑤ 已经取好了 —— 与"这道题是不是真的缺"是同一份
            # recent/avoid。这里只占标记: 否则出锁到真提交之间若有第二拍
            # 进来, 会重复起任务。
            to_submit = inputs
            self._future = _PENDING
            # G1: 记下"这次生成是在哪个场景里启动的"。场景指纹变了
            # 才允许重置长退避(见 ③)。
            _sc = self._scene_of(self._probe_safe())
            if _sc is not None:
                self._scene_at_submit = _sc

        # ---- 锁外真正提交 ----
        try:
            fut = self._executor.submit(self._generate_one, to_submit)
        except Exception:                       # noqa: BLE001
            log.exception("补池提交失败")
            with self._lock:
                self._future = None
                self._retry_at = now + self._backoff_s
            return
        with self._lock:
            # 只有还是自己占的那个标记才认(理论上期间不会被改, 但留个护栏)
            if self._future is _PENDING:
                self._future = fut

    def _apply_result(self, kind: str, detail: str, extra: dict,
                      now: float) -> None:
        """把 worker 的终局结果记进计数/退避。

        两条**正交**的账, 不要互相吞:
          - **试玩结果**: `playtest_*_count` **无条件**按 status 自增。
            所以 INTERRUPTED 虽然不退避(让路), 仍然计一次 interrupted ——
            否则区分不了"试玩系统经常坏"和"直播太忙总被打断"。
          - **入池结果**: `added` / `add_fail` 记最终有没有落进池子。
            试玩 PASS 之后 `pool.add()` 失败, 前面那次 `playtest_pass_count`
            **照样保留** —— 否则以后会误以为 Player 没通过。
        """
        pt = (extra or {}).get("playtest")

        # ---- ① 试玩结果(无条件计, 与后面 add 成败无关) ----
        if pt == OUTCOME_PASS:
            self.playtest_pass_count += 1
        elif pt == OUTCOME_UNSOLVED:
            self.playtest_unsolved_count += 1
        elif pt == OUTCOME_UNAVAILABLE:
            self.playtest_unavailable_count += 1
        elif pt == OUTCOME_INTERRUPTED:
            self.playtest_interrupted_count += 1

        # ---- ② 入池结果 ----
        if kind == "ok":
            self.added_count += 1
        elif kind == "gen_fail":
            self.generation_fail_count += 1
        elif kind == "add_fail":
            self.add_fail_count += 1
        elif kind == "exc":
            self.exception_count += 1
        elif kind == "interrupted":
            # G1: **不是失败**。gen_spec 因为"直播变忙"收手, 与
            # gen_fail(题不好) / add_fail(存不下) / exc(代码 bug) 是
            # 完全不同的第四类。它有自己的计数, 且**绝不**进失败链。
            self.interrupted_count += 1

        # ---- ③ 退避 ----
        # 成功入池清退避并且**重置连续失败序列**; 让路既不清也不加;
        # 其余一律按连续失败次数取递增档位。
        #
        # 为什么"让路"不能退避: 直播忙是**正常状态**, 不是故障。给它
        # 退避会让运维数据把"直播很活跃"读成"补池大量失败", 而且退避
        # 结束后直播可能还是忙的, 于是又一轮无效启动。
        #
        # 为什么"让路"也不能清退避: 让路发生在**结果**里, 而不是"新
        # 一轮成功了"。它什么都没证明, 清退避等于让一次真失败白等。
        if kind == "ok":
            self._retry_at = 0.0
            self._last_fail = ""
            self._fail_streak = 0
            log.info("补池成功: 库存 -> %d", self._stock())
            return
        if (extra or {}).get("interrupted") or kind == "interrupted":
            log.info("补池让路(直播变忙), 不计失败不退避: %s", detail)
            return
        self._fail_streak += 1
        wait = self._backoff_for_streak(self._fail_streak)
        self._retry_at = now + wait
        self._last_fail = detail or kind
        if wait > self._backoff_s:
            log.warning("补池连续失败 %d 次(%s), 退避 %.0fs: %s",
                        self._fail_streak, kind, wait, detail)
        else:
            log.warning("补池失败(%s), 退避 %.0fs: %s", kind, wait, detail)

    def _probe_safe(self) -> dict:
        """读压力探针, **绝不抛**。读不到返回空 dict。

        G1 新增的用途是取场景指纹; 判定逻辑仍然各自 fail closed
        (读不到 -> 让路 / 不启动), 不在这里替调用方决定。
        """
        try:
            p = self._probe()
            return p if isinstance(p, dict) else {}
        except Exception:                       # noqa: BLE001
            log.exception("读压力探针异常")
            return {}

    @staticmethod
    def _scene_of(p: dict) -> Optional[int]:
        """从探针结果里取"场景指纹"(当前题号)。取不到返回 None。

        旧探针(没有这个字段的替身)返回 None —— 调用方据此**不做**
        退避重置, 退回 G1 之前的固定递增行为, 而不是把 None 当成
        "场景变了"而疯狂重置。
        """
        v = (p or {}).get("puzzle_index")
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    def _backoff_for_streak(self, streak: int) -> float:
        """连续第 `streak` 次失败该退避多久。

        序列递增(默认 30/60/120/240/300...), 最后一档封顶 —— 不要
        无限翻倍: 补池的价值上限就是"池子里多一道题", 等到 10 分钟
        已经和停掉它没区别了, 但保留 300 秒能保证它**最终**会再试。

        `streak` 从 1 开始(第一次失败取第一档)。
        """
        idx = max(0, int(streak) - 1)
        if idx >= len(self._backoff_schedule):
            return float(self._backoff_schedule[-1])
        return float(self._backoff_schedule[idx])

    # ------------------------------------------------------------------
    def _ledger_ok(self) -> bool:
        try:
            return bool(self.pool.ledger_trustworthy)
        except Exception:                       # noqa: BLE001
            log.exception("读账本可信度异常, 本次不补池")
            return False

    def _stock(self) -> Optional[int]:
        try:
            # limit=max_size(不是 target): 硬上限那条判断需要看得到
            # `stock >= max_size`, 数到 target 就早退会让它永远看不见
            # —— 那正是 L1-C 里"到顶了还在补"的 bug。max_size >= target
            # 恒成立(config 有告警兜底), 所以这个上界比原来宽, 但 latch
            # 需要的三种区分("<min" / ">=target" / 都不是)照旧够用。
            return self.pool.stock_count(limit=self._max_size)
        except Exception:                       # noqa: BLE001
            log.exception("读库存异常, 本次不补池")
            return None

    def _playable(self, inputs: dict, limit: Optional[int] = None) -> int:
        """**下一题此刻能播几道** —— 与 `_stock()` 是两个不同的指标。

        `stock` 是长期库存(与窗口无关), playable 依赖当下的
        recent/avoid。实播踩的坑正是两者的差: stock=6 但 6 道候选
        全被当前窗口挡住, 于是回落现场生成 —— 而补池只看 stock,
        认为健康, 一道都不补。

        用**同一份** `inputs` 去 probe 和生成(见 `_on_tick_locked_ish`
        的 ⑤)。

        ## ⚠️ `limit` 必须跟着**当前阶段的目标**走(C3)

        `PuzzlePool.playable_count()` 的 `limit` 是**硬早退**
        (`if n >= limit: break`), 不是"上限提示"。所以传 1 就只能
        返回 0 或 1 —— 永远数不到 2。

        这正是 C3 之前那个静默失效: `_effective_targets()` 在 REVEALED
        下要求 `playable >= _reveal_playable_target`(默认 2), 但这里
        固定传 `_playable_min`(默认 1), 于是 `playable < need_playable`
        **恒真** -> latch 的"可播数够了"那条停止条件永远无法满足 ->
        补池一路补到硬上限 10 才因 `stock >= _max_size` 停下。

        旧测试没抓到, 是因为它把 `pool_reveal_playable_target` 人为设成
        1 —— 那等于把这条路径的触发条件删掉了(夹具不具备触发条件的
        断言是假的)。

        默认参数保持 `None` -> 退化为 `_playable_min`: 让 QA 阶段的
        调用与 C3 之前**逐位相同**, 不偷偷改变既有的早退行为。

        读不到就当 fail **closed**(返回 0 = 缺货): 此时若误判成"够",
        补池会停, 而实际可能一道都播不出来; 反过来误判成"缺"最多是
        多生成一道, 代价小得多。
        """
        want = self._playable_min if limit is None else int(limit)
        if want <= 0:
            # 关掉了这个触发条件 —— 返回一个"永远够"的哨兵值, 让
            # latch 只由 stock 决定(与 Q9 原行为逐位相同)。
            return want
        try:
            n = self.pool.playable_count(
                inputs.get("recent_signatures"), inputs.get("avoid"),
                limit=want)
            return int(n or 0)
        except Exception:                       # noqa: BLE001
            log.exception("读可播数异常, 按 0(缺货)处理")
            return 0

    def _low_pressure(self) -> bool:
        """低压力门: 允许补池的相位与"零在途"要求。

        ## QA —— 严格零

        `pending == 0 and inflight == 0 and not hint_inflight and
        not reveal_inflight`。房间忙时补池本来就没必要(存量够用),
        而且出题会和观众的提问抢同一个网关配额。

        ## REVEALED —— 允许(这是**最好的**生成窗口)

        REVEALED 是"谜底已公布、等 30 秒展示"的那一段。原来把它排除
        掉是纯粹的浪费: 那 30 秒里没有任何 LLM 工作, 观众在看揭晓,
        引擎完全空闲。补池在这里生成一道, 有很大概率赶在下一题就位
        之前完成 —— 于是下一题直接 pop 池子、瞬时切题, 不必现场等
        10–40 秒。仍要求零在途, 但不看 hint_inflight: REVEALED 期间
        引擎自己可能还在收尾提示, 那不是"抢配额"的对手。

        ## 明确**不**允许 SETTING / REVEALING

        出题在途时 phase 是 SETTING, 补池会和**直播自己的出题**抢网关
        —— 那是最该避让的一刻。REVEALING 期间可能还有揭晓生成的工作
        在飞, 同理不抢。这两个相位保持禁止。

        返回 False 只表示"这一拍不补", 不是错误 —— 下拍再看。
        """
        try:
            p = self._probe()
        except Exception:                       # noqa: BLE001
            log.exception("读压力探针异常, 本次不补池")
            return False
        if not p:
            return False
        if p.get("stopped"):
            return False
        from .state import Phase
        if p.get("pending") or p.get("inflight"):
            return False
        phase = p.get("phase")
        if phase == Phase.QA:
            # QA 期间 hint/reveal 都可能正在生成 —— 让路。
            if p.get("hint_inflight") or p.get("reveal_inflight"):
                return False
            return True
        if phase == Phase.REVEALED:
            if p.get("reveal_inflight"):
                return False
            return True
        return False

    def _effective_targets(self) -> tuple:
        """当前该用哪一组 (stock 高水位, playable 最低要求)。

        QA / 其他阶段 -> 保守的 `_target_size` / `_playable_min`
        (补池要和直播抢网关, 目标太高会互相拖慢);
        REVEALED      -> 抬高的 `_reveal_target` / `_reveal_playable_target`
        (引擎完全空闲的 60 秒, 这是唯一能把池子补厚的窗口)。

        探针读不到 phase 时退回保守组 —— 宁可少补, 不要在没有确认
        窗口空闲的情况下狂打网关。
        """
        try:
            p = self._probe() or {}
        except Exception:                       # noqa: BLE001
            log.exception("读压力探针异常, 用保守目标")
            return self._target_size, self._playable_min
        from .state import Phase
        if p.get("phase") == Phase.REVEALED:
            return self._reveal_target, self._reveal_playable_target
        return self._target_size, self._playable_min

    def _deadline_too_close(self) -> bool:
        """U1 + G1: 距下一题不足 guard 秒 -> 不再**启动**新请求。

        只挡"启动", 不碰在途任务 —— urllib 请求无法取消, 强杀只会让
        那一次生成白烧。已经飞着的跑完就进池子, 下一题用不上也无妨。
        (它**回来了**之后还能不能再发下一次, 由 `_should_continue` 卡。)

        ⚠️ G1: 这里的阈值是 `_effective_guard_s`, **不是**配置里那个
        原始 guard。实播事故就是原始 guard=15s 而一轮 prefetch 能跑
        几十秒 —— "只剩 18 秒"照样启动一个注定跨过 deadline 的后台
        任务, 下一题开始时它还在跑。effective guard = max(配置值,
        一轮预算 + 余量), 保证启动的那次有希望在 deadline 前结束。

        只在 REVEALED 且**确实拿到**剩余秒数时判定。探针给 None
        (不在 REVEALED / 没有 deadline)一律按"不限"处理 —— 少一次
        生成远比误判成"快截稿了"而长期不补池安全。
        """
        if self._effective_guard_s <= 0:
            return False
        try:
            p = self._probe() or {}
        except Exception:                       # noqa: BLE001
            log.exception("读压力探针异常, 本次不启动")
            return True                         # fail closed: 不启动
        left = p.get("reveal_remaining_seconds")
        if left is None:
            return False
        try:
            return float(left) <= self._effective_guard_s
        except (TypeError, ValueError):
            return True                         # 读到脏值 -> 不启动

    def _should_continue(self) -> bool:
        """G1: 后台生成"还该不该继续" —— 传给 `gen_spec` 的协作式取消谓词。

        与 `_low_pressure()` 是**同一个判定边界**(复用同一个压力探针),
        但调用时机完全不同, 所以不能互相替代:

            `_low_pressure()`  决定"这一拍**要不要启动**"      (低频, tick 4Hz)
            `_should_continue` 决定"已经跑起来的**要不要往下走**"(高频, 每次调用前)

        实播事故正是"启动时低压力、跑着跑着房间忙了"这个窗口造成的:
        补池在 REVEALED 启动, 下一题开始后它仍在审稿 / 再出一稿, 与
        直播的现场生成抢了几十秒网关。`_low_pressure` 只在 tick 里跑,
        根本看不见这个窗口; 这个谓词在**每次昂贵调用之前**跑, 看得见。

        ## 允许继续

            QA 且 pending/inflight/hint/reveal 全 0
            REVEALED 且没有 reveal 在途、且距下一题还有足够时间

        ## 必须让路

            SETTING / REVEALING  —— 直播自己在用网关
            pending / inflight > 0 —— 有观众在等回答
            hint / reveal 在途
            stopped
            REVEALED 已进入启动 guard(剩余不足一轮预算)

        ⚠️ 最后那条**必须**用 `_effective_guard_s`(最大 预算+余量), 不能
        用配置里那个原始值: guard 存在的意义就是"留够一轮生成的时间",
        用小的那个等于没留。

        探针读不到时 fail closed(返回 False): 后台少生成一道题的成本
        远小于"在直播最忙的时候抢网关"。谓词自己绝不抛异常。
        """
        try:
            p = self._probe() or {}
        except Exception:                       # noqa: BLE001
            log.exception("读压力探针异常, 后台生成让路")
            return False
        if p.get("stopped"):
            return False
        from .state import Phase
        if p.get("pending") or p.get("inflight"):
            return False
        phase = p.get("phase")
        if phase == Phase.QA:
            # QA 期间 hint/reveal 都可能正在生成 —— 让路。
            if p.get("hint_inflight") or p.get("reveal_inflight"):
                return False
            return True
        if phase == Phase.REVEALED:
            if p.get("reveal_inflight"):
                return False
            left = p.get("reveal_remaining_seconds")
            if left is None:
                # 拿不到剩余秒数就不限制 —— 与 `_deadline_too_close` 同一
                # 取舍: 少一次生成远比"误判成快截稿而长期不补池"安全。
                return True
            try:
                return float(left) > self._effective_guard_s
            except (TypeError, ValueError):
                return False                    # 读到脏值 -> 让路
        # SETTING / REVEALING / IDLE 之外的一切 —— 让路。
        return False

    def _generation_inputs(self) -> dict:
        try:
            d = self._probe_inputs()
            return d if isinstance(d, dict) else {}
        except Exception:                       # noqa: BLE001
            log.exception("取生成输入异常, 用空输入")
            return {}

    # ------------------------------------------------------------------
    def _generate_one(self, inputs: dict) -> None:
        """executor 线程。**唯一职责: 生成一道, (可选)试玩, add 进池。**

        唯一的写口是结尾那个 `_pending_result` —— 绝不碰 latch/退避/
        计数器(那些是 tick 线程的单写者变量)。
        """
        kind, detail, extra = "exc", "", {}
        try:
            kind, detail, extra = self._generate_one_inner(inputs)
        except Exception as e:                  # noqa: BLE001
            log.exception("补池生成异常: %s", e)
            kind, detail, extra = "exc", str(e), {}
        finally:
            with self._lock:
                self._pending_result = (kind, detail, extra)

    def _generate_one_inner(self, inputs: dict) -> tuple:
        """返回 `(kind, detail, extra)`。

        `extra` 目前只带一个 key: `playtest`(试玩 status)或 `interrupted`。
        tick 线程靠它做**正交**统计 —— 见 `_apply_result`。
        """
        recent = inputs.get("recent_signatures") or []
        avoid = inputs.get("avoid")
        extra: dict = {}
        bp = self._pick_blueprint(recent, rng=self._rng)
        # ---- G1: 低优先级生成 ----
        # `should_continue` 在**每一次尚未发出的昂贵调用之前**被检查
        # (出稿 / 审稿 / truth audit / 下一稿, 见 `gen_spec`)。已经发出去
        # 的那次 HTTP 请求无法取消, 等它回来即可 —— 关键是**它一回来就
        # 不能再发下一次**。这正是要消灭的实播事故: 下一题已经开始现场
        # 生成, 而旧 prefetch 还在审稿 / 再出一稿, 两边同时占网关 51 秒。
        #
        # 预算也换成后台自己的(少尝试, 不是降低题质): live 是 4 稿/90s,
        # 后台是 2 稿/25s。后台多试一稿的收益只是池子里多一道题, 代价
        # 却是跨过 deadline 与直播抢网关。
        spec = self.writer.gen_spec(
            avoid=avoid, blueprint=bp, recent=recent,
            max_attempts=self._prefetch_attempts,
            budget_s=self._prefetch_budget,
            should_continue=self._should_continue,
            # bp is None 时**真的**跳过 blueprint 硬校验, 而不是退回
            # 默认 blueprint(那是已经修过的 bug)。与 live 路径同一写法。
            enforce_blueprint=bp is not None)
        # ---- G1: 让路 —— 单独一类结果, **不能**记成 gen_fail ----
        # 判据看 metrics 里的显式标记(gen_spec 设的), 而不是猜 error
        # 文本; 也顺手兜住"writer 是替身、没有 metrics"的情况。
        if bool((getattr(spec, "metrics", None) or {}).get("interrupted")):
            return ("interrupted", "直播变忙, 本轮补池让路", {"interrupted": True})
        # 让路时 gen_spec **故意**把 error 留空(它不是失败), 所以这里
        # 必须在判断 error 之前先判 puzzle 空 —— 否则会走进下面
        # "空谜面"那条, 把一次让路记成 gen_fail。
        if spec is None or not getattr(spec, "puzzle", ""):
            if bool(getattr(spec, "interrupted", False)):
                return ("interrupted", "直播变忙, 本轮补池让路",
                        {"interrupted": True})
            return ("gen_fail",
                    (spec.error if spec is not None else "spec=None")
                    or "空谜面", {})

        # ---- Q10: AI 试玩(默认关闭, 开着才跑) ----
        #
        # ---- G4-C: 试玩也必须在**开始之前**让路 ----
        #
        # G1 让 `gen_spec` 在每一次尚未发出的昂贵调用前检查谓词, 但
        # `_playtest` **不在 `gen_spec` 里面** —— 它在它返回之后。于是
        # 存在这条缝:
        #
        #     gen_spec 成功返回(一次完整的多稿生成 + 审稿 + audit)
        #     ↓  这一段之间直播已经进入 SETTING
        #     ↓  prefetch 仍然启动一次 AI 试玩
        #
        # 试玩本身是**若干次 LLM 调用**(模拟提问者反复问), 会和下一题
        # 的现场生成抢同一个网关。
        #
        # G1 冻结的原则是"后台每一个尚未开始的昂贵 LLM 阶段都必须让 live
        # 优先", 试玩没有理由例外 —— 只是因为它在 gen_spec 之外, 被漏掉了。
        #
        # 两层检查并不冲突: 这里管"要不要**开始**", 而 playtester 自己
        # 内部若也有协作取消, 管的是"试玩**过程中**还继续吗"。
        if self._playtest_enabled():
            if not self._should_continue():
                log.info("补池: 直播变忙, 试玩前让路(已生成的稿子丢弃)")
                return ("interrupted", "直播变忙, 试玩前让路",
                        {"interrupted": True})
            pt, why = self._playtest(spec)
            if pt is not None:
                # 试玩结论落进 metrics —— Q8 的序列化链天然保存它。
                try:
                    spec.metrics["playtest"] = pt.to_metrics()
                except Exception:               # noqa: BLE001
                    log.exception("试玩 metrics 写入失败(忽略)")
                if not pt.passed:
                    # 契约: 不 PASS 就不入池。丢弃 candidate。
                    # **不**留内存副本、**不**直接上屏(同 add 失败)。
                    kind = ("playtest_interrupted"
                            if pt.status == OUTCOME_INTERRUPTED
                            else "playtest_fail")
                    return (kind, f"试玩未通过({pt.status}{'/' + pt.reason if pt.reason else ''})",
                            {"playtest": pt.status,
                             "interrupted": pt.status == OUTCOME_INTERRUPTED})
                extra = {"playtest": pt.status}

        if not self.pool.add(spec, source="prefetch"):
            # 契约: add 失败 == 这次生成**丢弃**。
            #
            # **不**留内存副本、**不**直接 submit_riddle 上屏。否则池子里
            # 会同时存在两类库存(盘上可恢复 vs 内存一次性), 而内存题
            # 没有 used 行 —— 崩溃后既不在盘上也不在 used 里, Q8 验收点 4
            # ("pop 过的题重启不复活")立刻无法推理。
            #
            # `extra` 里那次 `playtest=PASS` **照样带回 tick 线程计数** ——
            # 试玩统计与入池统计正交, add 失败不能把 PASS 吞掉。
            return ("add_fail", "pool.add 返回 False", extra)
        return ("ok", "", extra)

    # ------------------------------------------------------------------
    def _playtest_enabled(self) -> bool:
        if self._playtester is None:
            return False
        return bool(getattr(self.cfg, "playtest_enabled", False))

    def _playtest(self, spec: Any) -> tuple:
        """跑一次试玩。返回 `(PlaytestResult | None, why)`。

        **绝不让试玩异常冒泡** —— 它跑在 worker 线程里, 抛出去会变成
        `exc`, 那会把"试玩坏"记成"代码 bug", 混淆两类账。
        """
        try:
            r = self._playtester.run(spec)
            return r, ""
        except Exception as e:                  # noqa: BLE001
            log.exception("试玩异常: %s", e)
            return None, str(e)

    # ------------------------------------------------------------------
    def shutdown(self) -> None:
        """收尾。**这个方法本身立即返回, 但进程仍会等正在跑的那一道。**

        ⚠️ 别把这里理解成"下播立刻退出":

        标准 `ThreadPoolExecutor.shutdown(wait=False, cancel_futures=True)`
        的语义是 —— **`shutdown()` 自己**立即返回; 它不会取消**已经在
        执行**的任务, 而且 CPython 在**退出解释器**时会 join 线程池的
        工作线程(`concurrent.futures.thread` 的 atexit 钩子)。所以若
        `Ctrl-C` 的那一刻正好有一道 gen_spec 在跑, 进程仍会等它结束
        ——那是几十秒量级(gen_spec 的 budget_s=90)。

        这个行为是**接受**的, 不是疏忽: 补池同时最多只有一道在途, 而且
        只在 QA 空闲时才开始, 所以"退出时正好在跑"是小概率; 为它换成
        可强杀的隔离执行模型会显著复杂化线程模型, 不值得。

        (想真正快速退出, 得让 worker 不依赖普通线程池的退出语义 —— 那是
        另一个量级的改动, 本阶段明确不做。)
        """
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)
        except Exception:                       # noqa: BLE001
            log.exception("补池 executor 关闭异常(忽略)")

    # ------------------------------------------------------------------
    def stats(self) -> dict:
        """补池计数。**分阶段 + 试玩拆开**, 不做成一个宽泛的 "失败数"。

        一个总的 `fail_count` 在这里是有害的: 生成失败/入池失败/代码异常/
        试玩没过是四类完全不同的故障, 合并之后"试玩失败率 40%"这句话
        既不知道是题不收敛、是网关抖了, 还是直播太忙。
        """
        with self._lock:
            f = self._future
            return {
                "refill_active": self._refill_active,
                # `_PENDING` 也算在途(已决定提交、还没拿到 Future)。
                "in_flight": f is not None,
                "backoff_until": self._retry_at,
                # 滞回的两个输入。只看 stock 的话, "stock=5 playable=0"
                # (候选全被当前窗口挡住)这种现场指纹在计数里完全看不见。
                "stock": self._stock(),
                # C3: 这里报的 playable 必须与 latch 判据用**同一个 limit**,
                # 否则复盘时会看到"playable=1 但补池还在跑"这种自相矛盾的
                # 指纹 —— 而那个矛盾恰恰是 C3 之前那个 bug 的样子。
                "playable": self._playable(self._generation_inputs(),
                                           limit=self._effective_targets()[1]),
                "playable_min": self._playable_min,
                "max_size": self._max_size,
                # U1: 揭晓窗口的专用目标(内省/复盘用)。
                "reveal_target": self._reveal_target,
                "reveal_playable_target": self._reveal_playable_target,
                "reveal_guard_s": self._reveal_guard_s,
                # G1: **实际生效**的 guard(>= 一轮预算 + 余量)。报这个
                # 而不是配置里那个原始值 —— 否则复盘时会看到"guard=15
                # 却仍然在剩余 20 秒时启动"这种自相矛盾的指纹, 而那
                # 恰恰是 G1 之前那个 bug 的样子。
                "effective_guard_s": self._effective_guard_s,
                "prefetch_budget_s": self._prefetch_budget,
                "prefetch_max_attempts": self._prefetch_attempts,
                "fail_streak": self._fail_streak,

                "added": self.added_count,
                "generation_fail": self.generation_fail_count,
                "add_fail": self.add_fail_count,
                "exception": self.exception_count,
                # G1: 与 generation_fail **分开** —— 让路是优先级, 不是故障。
                "interrupted": self.interrupted_count,
                "skip": self.skip_count,

                "playtest": {
                    "pass": self.playtest_pass_count,
                    "unsolved": self.playtest_unsolved_count,
                    "unavailable": self.playtest_unavailable_count,
                    "interrupted": self.playtest_interrupted_count,
                },

                "last_fail": self._last_fail,
            }
