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
      ↓ 库存 < 低水位
    REFILL_ACTIVE
      ↓ 满足低压力条件且无在途任务
    GENERATING_ONE
      ↓ add 成功
    REFILL_ACTIVE
      ↓ 库存 >= 高水位
    IDLE

    任何生成/add 失败
      ↓
    BACKOFF
      ↓ 到期
    REFILL_ACTIVE

## 三条设计要点

1. **min/target 是真正的滞回(latch)**。每拍判一次 `stock < min` 是错的:
   1 补成 2 就停了, `pool_target_size` 永远没有意义。

2. **单飞靠 `self._future`, 不靠 `max_workers=1`**。后者只保证"同时执行
   一个", 挡不住 tick 往队列里排 30 个任务。

3. **一次只生成一道**。直播突然忙起来时, 最多只有一道已经发出的生成
   请求无法取消; 不会有 3–4 道连着打完。下一道必须等新的 tick 重新
   确认(QA + 零压力 + 无在途 + latch 仍 active)—— 这才叫低优先级。

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
        self._backoff_s = float(getattr(cfg, "pool_prefetch_backoff_s", 30.0) or 30.0)

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
        self._pending_result = None        # worker 的终局结果, 由 tick 取走
        self._last_fail = ""
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
                return

            # ---- ④ 账本不可信 -> 完全不补池 ----
            # 此时 pop_next 一道都不交付, 灌题只是白烧网关配额。
            if not self._ledger_ok():
                return

            # ---- ⑤ 库存(latch 只需要三种区分, 数到 target 就够) ----
            stock = self._stock()
            if stock is None:
                return

            # ---- ⑥ latch(滞回) ----
            if not self._refill_active and stock < self._min_size:
                self._refill_active = True
                log.info("补池周期启动: 库存 %d < 低水位 %d", stock, self._min_size)
            elif self._refill_active and stock >= self._target_size:
                self._refill_active = False
                log.info("补池周期结束: 库存 %d >= 高水位 %d",
                         stock, self._target_size)
                return
            if not self._refill_active:
                self.skip_count += 1
                return

            # ---- ⑦ 单飞 ----
            if self._future is not None:
                return

            # ---- ⑧ 低压力门 ----
            if not self._low_pressure():
                return

            # ---- ⑨ 取快照 + 占住"在途"标记 ----
            # 快照在**提交时**取, 与"任务是在低压力下起的"是同一时刻。
            # 标记必须先占: 否则出锁到真提交之间若有第二拍进来, 会重复起任务。
            to_submit = self._generation_inputs()
            self._future = _PENDING

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

        # ---- ③ 退避 ----
        # 成功入池清退避; 其余一律按 `OUTCOME_POLICY` 决定。
        # `interrupted` 只让路, 不退避 —— 否则运维数据会把"直播活跃"
        # 误读成"试玩大量失败"。
        if kind == "ok":
            self._retry_at = 0.0
            self._last_fail = ""
            log.info("补池成功: 库存 -> %d", self._stock())
            return
        if (extra or {}).get("interrupted"):
            log.info("补池让路(直播变忙), 不计失败不退避: %s", detail)
            return
        self._retry_at = now + self._backoff_s
        self._last_fail = detail or kind
        log.warning("补池失败(%s), 退避 %.0fs: %s",
                    kind, self._backoff_s, detail)

    # ------------------------------------------------------------------
    def _ledger_ok(self) -> bool:
        try:
            return bool(self.pool.ledger_trustworthy)
        except Exception:                       # noqa: BLE001
            log.exception("读账本可信度异常, 本次不补池")
            return False

    def _stock(self) -> Optional[int]:
        try:
            # limit=target: 数够高水位就早退, 把 tick 线程上的开销封顶。
            return self.pool.stock_count(limit=self._target_size)
        except Exception:                       # noqa: BLE001
            log.exception("读库存异常, 本次不补池")
            return None

    def _low_pressure(self) -> bool:
        """低压力门: **严格零**。

        phase == QA 且 pending/inflight 都为 0 且没有 hint/reveal 在途。
        为什么要求 QA: 出题在途时 phase 是 SETTING, 补池会和它抢;
        REVEALING/REVEALED 同理。为什么要求严格零: 房间忙的时候补池
        本来就没必要(池子里的存量够用), 而判定边界越清晰越好推理。
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
        if p.get("phase") != Phase.QA:
            return False
        if p.get("pending") or p.get("inflight"):
            return False
        if p.get("hint_inflight") or p.get("reveal_inflight"):
            return False
        return True

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
        spec = self.writer.gen_spec(
            avoid=avoid, blueprint=bp, recent=recent,
            # bp is None 时**真的**跳过 blueprint 硬校验, 而不是退回
            # 默认 blueprint(那是已经修过的 bug)。与 live 路径同一写法。
            enforce_blueprint=bp is not None)
        if spec is None or not getattr(spec, "puzzle", "") or spec.error:
            return ("gen_fail",
                    (spec.error if spec is not None else "spec=None")
                    or "空谜面", {})

        # ---- Q10: AI 试玩(默认关闭, 开着才跑) ----
        if self._playtest_enabled():
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

                "added": self.added_count,
                "generation_fail": self.generation_fail_count,
                "add_fail": self.add_fail_count,
                "exception": self.exception_count,
                "skip": self.skip_count,

                "playtest": {
                    "pass": self.playtest_pass_count,
                    "unsolved": self.playtest_unsolved_count,
                    "unavailable": self.playtest_unavailable_count,
                    "interrupted": self.playtest_interrupted_count,
                },

                "last_fail": self._last_fail,
            }
