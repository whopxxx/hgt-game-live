#!/usr/bin/env python
# coding: utf-8
"""后台补池(Q9)—— 独立于直播、按库存驱动地预生成题目并 add() 进题池。

## 最高优先级不变量

    **本模块只往 Q8 已经定义好的 Pool 边界里生产库存。**
    绝不改变 pop_next() / used ledger / fail-closed / 交付事务语义。

具体到代码, 这意味着本文件里**没有**、也**不该有**对
`pop_next` / `mark_used` / `remember_avoid` / `load` / `submit_riddle`
的调用, 更不该写 `pool._used_trustworthy`。复核时 grep 这几项即可。

## 与直播的关系: 两条流水线, 不是优先与让路(#55 / Phase C)

    LLM provider ──┬── Live pipeline      (Director / engine / live writer)
                   └── Prefetch pipeline  (本模块 / pf writer)

两边**允许同时存在**。本模块的调度**不读取任何直播运行状态**:

    ✗ phase (QA / SETTING / REVEALING / REVEALED)
    ✗ pending / inflight / hint_inflight / reveal_inflight
    ✗ reveal_remaining_seconds / deadline
    ✗ puzzle_index / 场景指纹

后台补题是否运行, 只由三件事决定:

    1. **库存**(stock / playable 相对水位);
    2. **后台自己的技术状态**(backoff / fail streak / 单飞);
    3. **生命周期**(是否激活 / 是否请求停止)。

唯一与直播的业务连接是**通过 Pool 的间接耦合**:

    直播消费 Pool -> 库存下降 -> 后台看到缺货 -> 补回来

参考 `docs/` 与 Issue #55。历史上本模块曾按"低优先级工作, 直播一忙就让路"
建模(`_low_pressure` / `_deadline_too_close` / 相位白名单 / 场景指纹重置退避),
那条路线在实播里产生了三类事故, 已整体移除:

    * 生成到一半相位切换 -> 已付成本的一半候选被丢弃;
    * 库存告急但有弹幕在途 -> 后台根本不启动;
    * 两套库存目标(QA / REVEALED), 后者已无产品价值。

**不允许**把这些判定以任何形式重新引入。若将来需要"直播优先", 正确的
做法是给 live 与 prefetch 各自独立的 transport 预算(已经如此, 见
`director.py` 的 pf_client), 而不是让后台看直播的脸色。

## 生命周期: 三态, 不是两态

    created
      ↓
    PREWARMING / background inactive   <- on_tick 直接返回
      ↓ activate_background()
    active(库存驱动补池)
      ↓ request_stop()
    STOPPED                            <- on_tick 直接返回

**为什么需要 PREWARMING 这一态**: `Director.run()` 先起 scheduler 线程,
再跑 `_prewarm()`, 而 `_prewarm()` 直接调 `_generate_one_inner()` ——
**绕过 `_future` 单飞**。没有这道闸门, scheduler 会在预热期间也提交一条
后台生成, 于是两条 prefetch 生成并发, `max_workers=1` 与 `_future`
都挡不住。这不是"直播抢占", 是**启动期生命周期**, 所以用 event 而不是
相位判据来表达。

`request_stop()` 与 `shutdown()` 分开: 前者只置停止信号(可在收尾期间
提前调用), 后者才回收 executor。

## 状态机(契约)

    IDLE
      ↓ 库存 < 低水位 **或** playable < 可用下限
    REFILL_ACTIVE
      ↓ 无 future / 无 backoff / 已 activate / 未 stop
    GENERATING_ONE
      ↓ add 成功
    REFILL_ACTIVE
      ↓ 库存 >= 高水位 **且** playable 达标
    IDLE

    库存 >= 硬上限(pool_max_size)
      ↓ **无条件**停下(即使 playable 仍为 0, 只 warning) —— 见下
    IDLE

    任何生成/add 失败
      ↓
    BACKOFF(递增: 30/60/120/240/300, 成功后归零)
      ↓ 到期
    REFILL_ACTIVE

    shutdown / request_stop —— 生成链唯一的**主动**中止理由
      ↓
    GENERATING_ONE 在下一昂贵阶段前收手
      ↓
    interrupted(**不退避、不计失败**) —— 它不是故障

## Cooperative cancellation: stop-only

生成链(`gen_spec` / keyword2 Stage A/B / 试玩)在**每一次尚未发出的昂贵
调用之前**检查一次谓词, 说停就停。已经发出的 HTTP 请求无法取消 —— 等它
回来即可, 关键是**它回来之后不能再发下一次**。

后台补池注入的谓词是 **stop-only**:

    _background_should_continue() = not self._shutdown_event.is_set()

它**只**回答"整条链还该不该跑", 不回答"直播忙不忙"。

⚠️ 冷启动预热用的是**另一个**谓词 `prewarm_should_continue(deadline,
should_abort)` —— stop + **deadline**。预热跑在正式开播前、只等有限时间,
所以它有截止时间; 而常驻后台补池没有。**这两条绝不能合并。**

## 后台用自己的预算

`gen_spec` 的默认参数(4 稿 / 90s)是**直播现场出题**的预算: 那时观众
在干等, 多试一稿值得。后台补池的收益上限只是"池子里多一道题", 所以
后台是 2 稿 / 25s —— **少尝试, 不是降低题质**(硬门一道不少)。同理
prefetch client 有自己的 timeout / max_retries(见 `director.py`)。

预算独立 ≠ 调度受直播影响: 预算是本模块**自己**的技术参数。

## 四条设计要点

1. **min/target 是真正的滞回(latch)**。每拍判一次 `stock < min` 是错的:
   1 补成 2 就停了, `pool_target_size` 永远没有意义。

2. **滞回看两个量: stock 与 playable**。`stock_count()` 是长期库存,
   `playable_count()` 是"下一题此刻能不能播"。实播踩过的坑: 6 道候选
   全被当前窗口挡住 -> 回落现场生成、观众干等, 而 stock=6 让补池
   认为健康, 一道都不补。所以启动/停止都同时看这两个。

3. **硬上限兜底**。`playable=0` 也可能是"被某个窗口条件整体挡住",
   此时补进来的新题会被同一条件挡住 —— 没有上限就是无限烧配额而
   playable 一动不动。到 `pool_max_size` 就停, 只 warning。

4. **在途护栏是 `_futures`(每 slot 一个), 不靠 `max_workers`**。后者只
   保证"同时执行 N 个", 挡不住 tick 往队列里排 30 个任务。
   (#55 之前是单 `_future` 单飞; Issue #60 §14 起 bounded concurrency
   = `pool_prefetch_concurrency`, live 默认 2。)

5. **任何时刻最多 N 条 generation pipeline(N=并发上限)**。live 默认
   2 条; 开播前 bulk prefill 用 5(独立进程, 见 prefill_pool.py)。
   每个 slot 独立 Future + 独立 writer; 结果进队列不互相覆盖;
   category in-flight reservation 防 overshoot(§14)。

## 为什么用轮询 future.done() 而不是 add_done_callback

回调在 **executor 线程**上触发, 会和 tick 线程并发读写 latch。回调若也
抢锁, 而 on_tick 正持锁, 就是死锁; 不抢锁则是静默数据竞争。

轮询让 **latch/退避/计数器只有一个写者(tick 线程)**, 这正是让滞回可
审计、可单测的性质。worker 唯一的写口是 `_pending_results` 结果队列(Issue #60 §14)。

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
#: G3: 只为一个 provenance 常量 —— 它决定 `metrics` 里 "这对词是哪份
#: 抽词实现发的"。 `keyword_corpus` / `KeywordBag` 仍然是**函数内**
#: import(见 `_init_keyword_bag`), 因为那几个要读盘, 装配期出错要在
#: 那里被 catch 成"显式降级", 而不是冒泡成 import 错误。
from .keyword_seed import KEYWORD_SEED_VERSION

log = logging.getLogger("story.prefetch")

#: `_future` 的占位标记: "已经决定要提交, 但还没拿到 Future"。
#:
#: 为什么需要它: 提交必须在锁外做(见 `_on_tick_locked_ish`), 于是出锁到
#: 真提交之间有一个窗口。没有标记的话, 第二拍进来会看到 `_future is None`
#: 而重复起一个任务。它**不是** Future, 所以任何 `is not None` 的单飞判断
#: 都会正确地把它当成"在途"。
_PENDING = object()

#: `_pick_deficit_category_locked` 的哨兵: 这一拍**没有**该补的 category
#: (所有类都在 target 之上) —— 走全局 free path 补充。
_NO_SLOT_NEEDED = object()


class PoolPrefetcher:
    """题池后台补池。**绝不抛、绝不阻塞 tick。**

    ## Issue #60 §14: bounded concurrency

    Q9/#55 时代是 single-flight(一个 `_future` / 一个 `_pending_result`
    槽)。本 Issue 把后台生成流水线改成 **bounded concurrency =
    `pool_prefetch_concurrency`(live 默认 2)**:

        * tick 只补空闲 slot, 同时最多 N 条 generation pipeline;
        * 每个 slot 有独立的 Future 与独立的 writer(worker 可能有
          **独立 client + PuzzleWriter** —— `_last_reject` / reviewer
          side-channel 是实例状态, 多 worker 绝不能共用一个 writer);
        * 结果不再塞单槽 `_pending_result`, 而是进**结果队列** ——
          2 个 worker 同时完成不互相覆盖(tick 一拍全部收账);
        * 对每个 requested category 维护 **in-flight reservation**,
          调度用 `effective_stock = actual + in_flight` 防同缺口
          无限 overshoot;
        * stop/shutdown 不变量(#55)逐条保留: `_submit_lock` 与
          state lock 分离 / visible stop 后不再 submit / final
          `should_continue` 紧贴最终 pool 写入 / shutdown 不死锁 /
          已开始 worker 协作中止记 interrupted。
    """

    def __init__(self, cfg: Any, pool: Any, writer: Any,
                 probe_inputs: Callable, pick_blueprint: Callable,
                 rng: Optional[random.Random] = None,
                 executor: Any = None, clock: Callable = time.monotonic,
                 writer_factory: Optional[Callable] = None):
        self.cfg = cfg
        self.pool = pool
        # ---- Issue #60 §14: 每 worker 独立 writer ----
        #
        # `writer_factory` 给了 -> 为每个 slot 各建一个(`PuzzleWriter`
        # 的 `_last_reject` / `_last_review_*` 是实例状态, 共享会让
        # 并发审稿的 provenance 互相污染 —— 与 Director 里 live/prefetch
        # 分实例是同一条理由, 现在并发 worker 之间也要分)。
        # 只传 `writer`(旧调用方/测试) -> 所有 slot 共用它: 测试替身
        # 无状态, 共享无害; 生产路径必须走 factory。
        self._writer_factory = writer_factory
        self._base_writer = writer
        self._concurrency = max(
            1, int(getattr(cfg, "pool_prefetch_concurrency", 2) or 1))
        self._probe_inputs = probe_inputs
        self._pick_blueprint = pick_blueprint
        self._clock = clock
        #: AI 试玩(Q10)。None = 不试玩(默认)。由 Director 通过
        #: `set_playtester()` 注入 —— 这里**不自己 new**, 因为
        #: Playtester 需要 host_writer(本模块的 writer)和
        #: should_continue(本模块的生命周期谓词), 装配权在调用方。
        #: 默认 None = 关闭试玩; 由 `set_playtester()` 在启动前注入。
        self._playtester = None

        self._min_size = max(0, int(getattr(cfg, "pool_min_size", 2) or 0))
        self._target_size = max(0, int(getattr(cfg, "pool_target_size", 5) or 0))
        #: "下一题此刻能播"的最低要求。0 = 关掉这个触发条件。
        self._playable_min = max(
            0, int(getattr(cfg, "pool_playable_min", 1) or 0))
        #: 硬上限。到这儿就停, **即使 playable 还是 0** —— 见 `on_tick`。
        #: 兜底取 target: max 没配时不该比 target 更小(那会让滞回失效)。
        self._max_size = max(
            self._target_size, int(getattr(cfg, "pool_max_size", 10) or 0))
        # ---- 后台补池的独立预算 ----
        # 过去 prefetch 直接调 `gen_spec()` 的默认参数(max_attempts=4 /
        # budget_s=90) —— 那是**直播现场出题**的预算。live 出一道题观众
        # 就在干等, 值得多试几稿; 后台补池只是"有空补一道", 多试一稿的
        # 全部收益是池子里多一道题。所以后台**少尝试**, 不是降低题质
        # —— 硬门一道不少。
        #
        # ⚠️ 这是本模块**自己**的技术参数, 与直播运行状态无关。
        self._prefetch_attempts = max(
            1, int(getattr(cfg, "pool_prefetch_max_attempts", 2) or 2))
        self._prefetch_budget = max(
            1.0, float(getattr(cfg, "pool_prefetch_budget_seconds", 25.0)
                       or 25.0))
        # keyword2 Story 单独允许比普通 prefetch stage 更长，但绝不突破
        # live/global transport 的硬上限。这个值只通过 keyword_spec 显式
        # 传给 puzzle.story；其它 stage 继续继承 pf client 的 30s/0。
        self._story_timeout = min(
            max(0.1, float(getattr(cfg.llm, "timeout", 60.0) or 60.0)),
            max(0.1, float(getattr(
                cfg, "pool_prefetch_story_timeout_seconds", 45.0) or 45.0)))
        self._backoff_s = float(getattr(cfg, "pool_prefetch_backoff_s", 30.0) or 30.0)
        # ---- G1: 连续失败的退避序列 ----
        # 固定 30 秒会让同一个上下文(同样的 recent window / 同样的配额
        # 饱和)被反复重试, 每次都烧完整一轮预算。改成递增, 成功后归零。
        _sched = tuple(getattr(cfg, "pool_prefetch_backoff_schedule_s",
                               None) or ())
        self._backoff_schedule = tuple(
            float(x) for x in _sched if float(x) > 0) or (self._backoff_s,)
        # ---- 库存未恢复时的**短**退避序列 ----
        #
        # 只要 refill latch 还开着，就说明 stock / playable 还没回到目标线。
        # 旧实现只在“完全空池”时用短序列；一旦刚补进 1 道，就立刻切回
        # 30/60/120...，实播里于是出现“明明离 8/12 还很远，第二次技术
        # 失败却直接睡 60 秒”。那会让补给追不上消费。
        #
        # 新语义：refill_active 整个期间都用 5/10/15s（可配置）短退避；
        # 达到目标、latch 关闭后才恢复保守长退避。仍然 single-flight，
        # 不增加并发。
        _refill_sched = tuple(
            getattr(cfg, "pool_prefetch_refill_backoff_schedule_s", None) or ())
        self._refill_backoff_schedule = tuple(
            float(x) for x in _refill_sched if float(x) > 0) or (5.0, 10.0,
                                                                 15.0)

        # 补池用**独立**的 rng。共用 Director 的 _rng 会让 live 路径的
        # blueprint 序列随"补池开不开"而变 —— 那既难排查, 也让
        # "同 seed 下补池不影响出题" 这个可测性质消失。
        #
        # ⚠️ G3: keyword2 的抽词**不再**用这把 rng。它走 `self._bag`
        # (见下), 因为"不放回"是一种状态, 而 rng 只是一个数流。
        # 这把 rng 现在只服务 classic 链的 `pick_blueprint`。
        self._rng = rng if rng is not None else random.Random()

        # ---- G3: keyword bag(真实 haiguitang corpus) ----
        #
        # ## 为什么状态住在这里
        #
        # "一个 bag 用完之前同一个 pair 不重复"是**跨调用**的状态。G2 把它
        # 交给调用方(`used_pairs` 参数), 于是"忘了传"就静默退化成有放回 ——
        # 那个 bug 真发生过。收进 prefetcher 就没有失败模式了。
        #
        # ## corpus 不可用 = **显式降级**, 不是回退人工词库(§五)
        #
        # 读失败时: 打一条明确的 ERROR, 把 `self._bag` 留成 None, 然后
        # `_keyword_enabled()` 返回 False —— 整条 keyword2 链让位给 classic
        # Blueprint 链。**绝不**回退 `KEYWORD_BANK`: 那会让生产"看起来在跑
        # keyword2, 其实用人工词", 而这正是任务书点名的形状。
        #
        # 降级发生在**装配时**(一次), 不在每次 `_generate_one_inner` 里
        # 反复读盘 —— 后台补池是热路径。
        self._bag = None
        self._bag_meta: dict = {}
        self._keyword_session_seed: Optional[int] = None
        self._bag_error: str = ""
        self._init_keyword_bag(cfg)

        # 自己起 executor(不借 Director 的): 它是本模块的内部实现细节,
        # 执行线程数与实际可占用 slot 数一致。
        self._owns_executor = executor is None
        self._executor = executor or ThreadPoolExecutor(
            max_workers=self._concurrency, thread_name_prefix="prefetch")

        # ---- 状态(全部由下面这把锁保护) ----
        # 刻意**不**复用 Director 的 _narrating: 那个锁是出题用的, 补池
        # 持它会把 live 出题挤成"推迟到下一拍" —— 优先级完全倒过来。
        self._lock = threading.Lock()
        #: 生命周期/submit **线性化锁**。⚠️ 与 `_lock` 是**两把**锁, 别合并。
        #:
        #: 为什么必须分开: `submit` 必须在 `_lock` 之外(见
        #: `_on_tick_locked_ish` 的论证 —— submit 可能阻塞, 且同步替身会
        #: 在 submit 里就地跑完 worker, worker 结尾要拿 `_lock`, 非重入
        #: 锁上直接死锁)。于是"把 stop 信号与最终 submit 排成先后"这件事
        #: 没有锁可依 —— 必须由**另一把**锁提供。
        #:
        #: 它守护的不变量只有一条: **一旦 `_shutdown_event` 可见地置位,
        #: 就绝不会再有新的 `submit`**。`request_stop()` / `activate_background()`
        #: 与 tick 的提交段都在它下面, 于是三者两两线性化。
        #:
        #: ## 锁序(死锁防护, 不是风格)
        #:
        #:   唯一合法方向: `_submit_lock` → `_lock`(外层 `_submit_lock`,
        #:   内层可再取 `_lock`)。`_lock` 内**绝不**回头取 `_submit_lock`。
        #:
        #: 之所以不是"两把锁永不嵌套": 提交段里"撤销 `_PENDING` / 回填
        #: future"这两个子块必须原子(否则第二拍 tick 会看到半截状态而
        #: 破坏单飞), 所以它们必须在 `_submit_lock` 内再进 `_lock`。
        #: 方向单一 + `_lock` 内从不取 `_submit_lock`, 就不会成环。
        #:
        #: 持有者只有三个: `request_stop()` / `activate_background()` /
        #: `_on_tick_locked_ish` 的提交段。
        #:
        #: ## 为什么是 `RLock` 而不是 `Lock`
        #:
        #: 它必须是**可重入**的。原因很具体: 提交段持着它调
        #: `executor.submit()`; 而同步执行的替身(测试用)会在这把锁内部
        #: 就地跑完整个 worker, worker 又可能调 `request_stop()` /
        #: `shutdown()` —— 那是**同一线程**重入。普通 `Lock` 会自死锁。
        #:
        #: 这不是只为测试让步: "生成链发现致命错误 -> 主动收手"是未来
        #: 完全可能出现的生产形状(worker 自己调 stop)。把它做成不可重入
        #: 等于埋一颗雷。`RLock` 只放行**同一线程**重入, 跨线程的互斥
        #: 与 `Lock` 完全一致 —— 于是"stop 与 submit 的线性化"这条不变量
        #: 一字不变: 另一个线程的 `request_stop()` 仍然必须等提交段走完
        #: (或抢在它前面, 让提交段复查到并放弃)。
        self._submit_lock = threading.RLock()
        # ---- Issue #60 §14: bounded concurrency 状态 ----
        # 每个 slot 一个 Future(None = 空闲)。`_PENDING` 占位语义保留:
        # 已决定提交、还没拿到 Future。旧 single-flight 的
        # `self._future` 语义由 `_futures` 列表取代;
        # `test` 里直接摸 `pf._future` 的少数路径由下面的兼容 property 承接
        # (等价于"任一 slot 在途")。
        self._futures: list = [None] * self._concurrency
        self._refill_active = False        # latch(滞回)
        self._retry_at = 0.0               # 退避到期时刻(monotonic)
        #: 连续失败次数。`interrupted`(主动中止)**不计** —— 见
        #: `_apply_result`。它在 `_fail_streak` 上的效果必须是
        #: "什么都没发生"。
        self._fail_streak = 0
        # ---- §14: 结果**队列**(替代旧单槽 `_pending_result`) ----
        # 2 个 worker 同一拍完成时, 两个终局都必须被收账 —— 单槽会
        # 互相覆盖。`_pending_results` 是 list[tuple], tick 一次全取走。
        self._pending_results: list = []
        # ---- §11: category in-flight reservation ----
        # key = category("" = 无主题); value = 该类当前在飞的 pipeline 数。
        # 调度用 effective_stock = actual + in_flight, 防止 2 个 slot
        # 同时看到同一个缺口无限 overshoot。
        self._in_flight_by_category: dict = {}
        # ---- §11: demand refill(主题投票的高优先级 demand 信号) ----
        # key = category; value = demand 置位时间。demand 的业务任务
        # 不因一次技术失败/observed miss 消失 —— 直到该类库存回到
        # target 或生命周期 stop 前保持 pending。重复置位去重。
        self._demand_categories: dict = {}
        self._last_fail = ""
        #: 硬上限 warning 的节流时刻(monotonic)。tick 4Hz, 不节流会把
        #: "到顶了但还是没得播"刷成日志洪水。
        self._max_warn_at = 0.0
        self._max_warn_s = 60.0
        # ---- 生命周期(见模块 docstring 的"三态") ----
        #: 后台调度是否已激活。**prewarm 结束前不 set** —— 否则
        #: `Director._prewarm()` 直接调 `_generate_one_inner()`(绕过
        #: `_future`) 时会与 scheduler 提交的生成并发, 单飞形同虚设。
        #:
        #: 注意它**不是**相位判据: 激活之后, 直播处于任何相位都不影响
        #: 补池。它只回答"本次运行的后台补池阶段开始了没有"。
        self._background_active = threading.Event()
        #: 停止信号。`request_stop()` 置位, `shutdown()` 也置位。
        #: 这是生成链**唯一**的主动中止理由(见 `_background_should_continue`)。
        self._shutdown_event = threading.Event()
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
        #: gen_spec 协作式收手的次数(主动中止: shutdown / stop)。**与
        #: generation_fail_count 分开** —— 前者是生命周期中止(正常),
        #: 后者是题不合格(故障)。合成一个数字之后, "补池失败率"就
        #: 不再是纯粹的故障率了。
        self.interrupted_count = 0
        self.skip_count = 0
        # ---- G4-R2 §六: 把"未成题"拆成可分辨的几类 ----
        #
        # 实播复盘时只有一个统一的 `keyword2 未成题` 计数, 于是"这轮为什么
        # 通过率低"只能人工逐行数日志。这五项正是**决策树上的五个出口**:
        #
        #     structure_technical_fail  Stage B **结构调用**的技术失败
        #                               (空 tool_input 等, 已重试一次仍失败)
        #     review_technical_fail     **审稿**的技术失败(超时 / 截断 /
        #                               空 tool_input) —— 与上面那个分开:
        #                               排查方向不同(Stage B 网关 vs Reviewer)
        #     truth_technical_fail      **truth audit** 的技术失败
        #     review_rewrite            Reviewer 读懂后要求重出 —— 内容问题
        #     truth_reject              truth audit 判叙事/机制不成立 —— 内容问题
        #     validation_reject         validate_spec 硬门不过 —— 结构问题
        #     success                   真正入池
        #
        # 前三项是**技术形状**(值得重试 / 换网关), 中间三项是**语义判定**
        # (值得改 prompt / 改创作)。全部由 worker 线程的
        # `extra["reject"]` 单写, 与既有的 fail_streak 状态机正交 ——
        # 它们**不**参与退避决策, 只是账本。
        #
        # ⚠️ R7/Task0: `safety_reject` 与 `safety_technical_fail` 是 R7
        # 双门 AND 引入的两个出口。它们**没进**这张表时, 线上会刷
        # "未知的 reject 标签" 警告, 而这两个原因**恰恰是最该看得见的**:
        # 安全门拒了多少、网关抖了多少, 直接决定"要不要放宽判据"。
        # 只补统计, **不动退避逻辑**。
        self.reject_count: dict = {
            "structure_technical_fail": 0,
            "review_technical_fail": 0,
            "truth_technical_fail": 0,
            "review_rewrite": 0,
            "truth_reject": 0,
            "validation_reject": 0,
            "safety_reject": 0,
            "safety_technical_fail": 0,
            "success": 0,
        }
        # ---- Issue #60 §14: 每 slot 独立 writer(状态齐了再建) ----
        self._writers = [self._make_writer(i)
                         for i in range(self._concurrency)]
        if (self._owns_executor and self._concurrency > 1
                and len({id(w) for w in self._writers if w is not None})
                    != len([w for w in self._writers if w is not None])):
            raise ValueError("concurrent prefetch writers must be independent")

    def _make_writer(self, slot: int) -> Any:
        """slot i 的 writer。并发时 factory 必须提供独立实例。"""
        if self._writer_factory is not None:
            w = self._writer_factory(slot)
            if w is None and self._concurrency > 1 and self._base_writer is not None:
                raise ValueError("prefetch writer_factory returned None")
            return w
        if (self._concurrency > 1 and self._owns_executor
                and self._base_writer is not None):
            raise ValueError("concurrent prefetch requires writer_factory")
        return self._base_writer

    @property
    def writer(self) -> Any:
        """兼容入口: 稳态路径用的是 slot 0 的 writer。

        ⚠️ 写它(测试/老代码 `pf.writer = X`)会同时替换 slot 0 ——
        这是历史语义的保留, 生产代码一律走 `_writers[slot]`。
        """
        return self._writers[0] if self._writers else self._base_writer

    @writer.setter
    def writer(self, value) -> None:
        if self._writers:
            self._writers[0] = value
        self._base_writer = value

    # ------------------------------------------------------------------
    # ---- Issue #60 §11: demand refill 接口 ----
    # ------------------------------------------------------------------
    def request_category(self, category: str) -> bool:
        """主题投票的 demand 信号(Engine/Director 在 REVEALED 中
        第一次见到某类有效票时发)。

        幂等: 重复 demand 同一类不会重复 enqueue 业务任务 —— 已在
        pending 就刷新时间戳并返回 False。demand 的优先级高于普通
        maintenance deficit(见 `_on_tick_locked_ish`)。
        """
        cat = str(category or "").strip()
        if not cat:
            return False
        now = self._clock()
        with self._lock:
            first = cat not in self._demand_categories
            self._demand_categories[cat] = now
        if first:
            log.info("demand refill 置位: category=%s(主题投票需求)", cat)
        return first

    def _clear_demand_if_healthy_locked(self, stock_by_cat: dict) -> None:
        """某类库存 >= target 时清除它的 demand(lifecycle stop 也清)。"""
        target = max(0, int(getattr(self.cfg, "pool_category_target", 10)))
        for cat in list(self._demand_categories):
            if stock_by_cat.get(cat, 0) >= target:
                del self._demand_categories[cat]
                log.info("demand refill 清除: category=%s 库存已到 target", cat)

    def _effective_stock_locked(self, stock_by_cat: dict,
                                category: str) -> int:
        """§14: actual eligible stock + 该类 in-flight reservation。"""
        actual = stock_by_cat.get(category, 0)
        in_flight = self._in_flight_by_category.get(category, 0)
        return actual + in_flight

    # ------------------------------------------------------------------
    @property
    def _future(self):
        """兼容: 旧单飞语义 = "任一 slot 在途"。

        生产代码不再读它; 保留给既有测试(`pf._future is not None` /
        占位撤销断言)与排查。赋值只在并发=1 时与旧语义等价。
        """
        return next((f for f in self._futures if f is not None), None)

    @_future.setter
    def _future(self, value) -> None:
        # 并发=1 时旧测试/旧代码可以按原样置 None(撤销占位)。
        if self._concurrency == 1:
            self._futures[0] = value
        # 并发>1 时忽略外部赋值 —— slot 状态只能由 tick/worker 管理。

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
    # 生命周期(见模块 docstring 的"三态")。
    # 这三个方法与下面的 `on_tick` / `_background_should_continue` 一起,
    # 构成后台补池**唯一**的运行条件 —— 它们都不读直播状态。
    # ------------------------------------------------------------------
    def activate_background(self) -> None:
        """启动后台补池调度。**必须在 `Director._prewarm()` 返回之后调用。**

        为什么需要它: `Director.run()` 先起 scheduler 线程, 再跑
        `_prewarm()`; 而 `_prewarm()` 直接调 `_generate_one_inner()` ——
        绕过 `_future` 单飞。若不闸住 scheduler, 预热期间会出现
        **两条 prefetch 生成并发**, `max_workers=1` 与 `_future` 都
        挡不住。这是启动期生命周期, 不是直播抢占。

        幂等: 重复调用无副作用。
        """
        # 与 `request_stop()` 共用 `_submit_lock`: 否则这里是裸的
        # check-then-set, 并发 stop 会让"停止之后又被复活激活" ——
        # 正是上面那句注释要防的事, 但没锁时防不住。
        with self._submit_lock:
            if self._shutdown_event.is_set():
                # 已经决定停止 -> 不再激活(否则 stop 之后 activate 会把它复活)。
                return
            self._background_active.set()

    def request_stop(self) -> None:
        """发出停止信号。**只置位, 不做资源回收。**

        与 `shutdown()` 分开, 是因为收尾可能分两段: Director 检测到
        `Phase.STOPPED` / 收到 Ctrl-C 时, 应当**立刻**停止后台补池
        (否则收尾那几秒还能起新候选), 而 executor 的回收可以留到
        `finally` 里做。

        ⚠️ 这是生成链的唯一主动中止理由 —— `_background_should_continue()`
        直接读它。幂等。

        ⚠️ 在 `_submit_lock` 下置位, 与 tick 的提交段互斥 —— 那是
        "stop 之后不得再有 submit" 的线性化边界(见 `_submit_lock`
        的声明与 `_on_tick_locked_ish` 的 ⑩b)。
        """
        with self._submit_lock:
            self._shutdown_event.set()

    def set_playtester(self, playtester: Any) -> None:
        """注入 AI 试玩器(Q10)。装配权在 Director —— 见 `__init__` 的说明。

        做成方法而不是直接写 `pf._playtester = ...`, 是为了给这层耦合
        一个显式的装配口: Director 不该逐个摸 prefetcher 的私有字段。

        ⚠️ 必须在 `activate_background()` 之前调用 —— 试玩器一旦开始被
        使用, 其 `should_continue` 就应当是 lifecycle 谓词。
        """
        self._playtester = playtester

    def _background_should_continue(self) -> bool:
        """**后台补池专用**的协作式取消谓词 —— stop-only。

            False  <=>  已经请求停止(shutdown / request_stop)

        它**只**回答"整条链还该不该跑", 不回答"直播忙不忙"。这正是
        Phase C 的核心: 一个正在生成的候选项, 只会因为本次运行正式
        结束而被中止, 不会因为直播切了相位而被丢掉。

        ⚠️ 与 `prewarm_should_continue` 是**两个东西**, 绝不能互相替代:
        那个是 stop + **deadline**(冷启动预热只等有限时间), 这个是纯
        stop(常驻补池没有截止时间)。合并会让预热重新变成无限等待,
        或让常驻补池凭空获得一个不存在的截止时间。

        ⚠️ 也**不允许**在这里重新引入相位 / pending / inflight / hint /
        reveal / deadline 判据 —— 那正是本轮要拆除的结构。
        """
        return not self._shutdown_event.is_set()

    # ------------------------------------------------------------------
    def on_tick(self) -> None:
        """tick 线程调用。**绝不阻塞, 绝不抛。**

        放在 Director._scheduler 的 tick 循环之后调用。

        ## 启动条件(Phase C) —— 一个直播状态都不读

            _enabled()
            AND _background_active 已激活   (prewarm 未结束前为 False)
            AND 未 request_stop / shutdown
            AND 账本可信
            AND stock < 硬上限
            AND refill latch active(库存 < 低水位 或 playable 不达标)
            AND 退避已到期
            AND 无在途 future

        满足即提交。此时直播处于 SETTING / REVEALING / REVEALED, 或
        pending / inflight / hint / reveal 非零, **都不影响** —— 后台
        补题与直播是两条独立流水线(见模块 docstring)。

        ## 停止之后仍要收账(§ 9)

        `request_stop()` / `shutdown()` 之后**不再提交**新候选, 但**已经
        在途的那一条**迟早要回一个终局结果(它多半是 `interrupted`)。
        那份记账**不能**被停止闸门一起丢掉 —— 否则 `interrupted_count`
        恰好在最该被看见的时刻(下播收尾)恒为 0, 运维就会把一次正常
        停止读成"什么都没发生"。

        所以这里把「收账」与「起新活」拆开: 收账无条件做, 起新活才看
        生命周期闸门。
        """
        if not self._enabled():
            return
        # ---- 生命周期闸门(不是相位闸门) ----
        # 未激活(prewarm 期间)或已请求停止 -> 不起新候选。
        active = (self._background_active.is_set()
                  and not self._shutdown_event.is_set())
        try:
            self._on_tick_locked_ish(gate=active)
        except Exception:                       # noqa: BLE001
            # 补池绝不能影响直播主循环 —— 任何异常都吞在这里。
            log.exception("补池 tick 决策异常(忽略)")

    def _on_tick_locked_ish(self, gate: bool = True) -> None:
        """决策。**`_lock` 只包住状态读写, 不包住 submit。**

        为什么 submit 必须在 `_lock` 之外:
          - 真实 executor 的 `submit()` 在队列满时可以阻塞, 持锁阻塞会
            把 tick 线程卡住 —— 那正是"补池不能影响直播"要避免的;
          - 同步执行的替身(测试里那种)会在 `submit()` 里**就地**跑完
            worker, 而 worker 结尾要拿同一把 `_lock` 写结果队列
            —— 非重入锁上直接死锁。
        所以这里先把该读的读完、该占的标记占掉, 出了锁再真正提交。

        ## 但"出锁再提交"本身带一条缝, 由 `_submit_lock` 补上

        `_PENDING` 占位(`_lock` 内)到真提交(`_lock` 外)之间, 并发进来的
        `request_stop()` 会让"停止之后仍然提交一条新候选"。所以提交段整段
        挪进 `_submit_lock`, 并在其中**复查**一次 `_shutdown_event` ——
        详见该段自己的注释(⑩b)。`_lock` 与 `_submit_lock` 的锁序见
        `_submit_lock` 的声明。
        """
        now = self._clock()
        to_submit = None
        with self._lock:
            # ---- ① 回收已完成的 future(每个 slot 各自回收) ----
            # 只把句柄置 None; 状态转移在 ② 做, 让 latch 保持单写者。
            # `_PENDING` 是刚占的位、还没有 Future, 跳过。
            for i, fut in enumerate(self._futures):
                if fut is not None and fut is not _PENDING and fut.done():
                    self._futures[i] = None

            # ---- ② 应用 worker 的终局结果(队列, 一次全收) ----
            # **无条件**: 即使在途那条是因为停止才收手(interrupted), 也要
            # 把这次结果记进账。停止闸门只拦"起新活", 不拦"收旧账"。
            results, self._pending_results = self._pending_results, []
            for res in results:
                kind, detail, extra = res
                self._apply_result(kind, detail, extra, now)

            # ---- ②b 生命周期闸门 ----
            # 未激活(prewarm 期间)或已请求停止 -> 到此为止, 不再起新候选。
            # 放在收账**之后**: 停止信号到达时那条在途候选的 interrupted
            # 记账必须留下(见 `on_tick` docstring)。
            if not gate:
                return

            # ---- ③ 退避 ----
            # 退避**完全**由后台自己的状态决定: 连续失败次数 + latch 是否
            # 还开着(见 `_schedule_now`)。这里刻意**不**因为"直播进入了
            # 新一题"而重置 —— 那属于"直播控制后台调度", Phase C 之后
            # 不再允许。上一次失败就是上一次失败, 与直播播到第几题无关。
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

            # ---- ⑥ 库存 / 可播数 + category-aware 库存(§10) ----
            stock = self._stock()
            if stock is None:
                return
            playable = self._playable(inputs)
            stock_by_cat = self._stock_by_category()

            # demand 信号的生命周期: 该类库存到 target 就清(§11)。
            self._clear_demand_if_healthy_locked(stock_by_cat)

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
            # 启动条件: 长期库存见底 **或** 下一题此刻没得播
            # **或** 某类 eligible 库存低于 low_water(§10)。
            # 停止条件: 库存到高水位 **且** 下一题有得播 **且** 每类
            # 都达到 category target。
            need = (stock < self._min_size
                    or (playable < self._playable_min
                        and stock < self._max_size))
            if not need:
                # ---- §10: category-aware 缺货也是 need ----
                low_water = max(
                    0, int(getattr(self.cfg, "pool_category_low_water", 8)))
                for cat, cnt in stock_by_cat.items():
                    if cnt < low_water and \
                            self._effective_stock_locked(stock_by_cat, cat) \
                            < low_water:
                        need = True
                        break
            if not self._refill_active and need:
                self._refill_active = True
                log.info("补池周期启动: 库存 %d < 低水位 %d, 或 可播 %d < %d",
                         stock, self._min_size, playable, self._playable_min)
            elif self._refill_active and (
                    stock >= self._target_size
                    and playable >= self._playable_min):
                self._refill_active = False
                log.info("补池周期结束: 库存 %d >= 高水位 %d 且 可播 %d >= %d",
                         stock, self._target_size, playable, self._playable_min)
                return
            if not self._refill_active:
                self.skip_count += 1
                return

            # ---- ⑨ 空闲 slot ----
            # 单飞(并发=1)时这条退化为旧语义: 任一在途就不再起新活。
            free_slots = [i for i, f in enumerate(self._futures)
                          if f is None]
            if not free_slots:
                return

            # ---- ⑩ 决定这一拍要补的 category(deficit 选择) ----
            # 优先级(§11): 有投票 demand 且库存不足的 category
            #             > 普通 category maintenance deficit
            #             > 全局(无主题, "" —— 走原 free path)。
            # ⚠️ effective_stock 扣掉 in-flight: 2 个 slot 不能同时
            #    看到同一个缺口无限 overshoot —— 第一个 slot 占了
            #    reservation 之后, 第二个 slot 看到的缺口就小一格。
            cat_plan = self._pick_deficit_category_locked(stock_by_cat)
            if cat_plan is _NO_SLOT_NEEDED:
                # 分类信息全部健康(或池子无 v2 题) -> 全局 free path。
                # 全局 latch 既然还开着(全局 stock/playable 缺), 就该补。
                cat_plan = ""
            to_submit = dict(inputs or {})
            to_submit["requested_category"] = cat_plan
            # 占住一个空闲 slot(标记), 其余留给后面的拍。
            slot = free_slots[0]
            self._futures[slot] = _PENDING

        # ---- 锁外真正提交(由 `_submit_lock` 线性化) ----
        #
        # ⚠️ 这里为什么不能只靠上面的 `gate`: `gate` 是在**进 `_lock`
        # 之前**算好的(on_tick 里), 而 `_PENDING` 占位到真提交之间已经
        # 出了 `_lock`。`request_stop()` 恰恰能插进这条缝:
        #
        #     ⑩ `self._futures[slot] = _PENDING` ← 持 `_lock`
        #     ↓  出锁
        #     ↓  request_stop() 在这里跑完 → `_shutdown_event` 置位
        #     ↓  executor.submit(...)       ← 停都停了还提交, 这就是漏
        #
        # 修法: 拿 `_submit_lock`(与 `request_stop()` 同一把), 在它下面
        # **复查**一次, 然后把复查与 submit 排成不可分割的一段。这样
        # "可见的 stop" 与 "新的 submit" 之间就有了明确的先后 —— 要么
        # stop 先拿到锁(submit 段复查到 → 不提交), 要么 submit 段先拿到
        # 锁(stop 会等它做完, 那条任务属于"停止之前就已提交", 合法)。
        with self._submit_lock:
            # ---- ⑩b 线性化复查: 最后一个决策点 ----
            if self._shutdown_event.is_set():
                with self._lock:            # 锁序: _submit_lock → _lock
                    # ⚠️ 必须撤销 ⑩ 占的标记。忘了它 -> 该 slot 永远是
                    # `_PENDING` -> 并发被永久占死, 补池再也不启动。
                    for i, f in enumerate(self._futures):
                        if f is _PENDING:
                            self._futures[i] = None
                # 不提交。**不记失败、不退避**: 这活根本没开始, 不是故障,
                # 也不是 `interrupted`(那是"已开始后被协作式收手")。
                # 语义与 `_apply_result` 的 interrupted 分支一致。
                return
            try:
                fut = self._executor.submit(self._generate_one,
                                            to_submit, slot)
            except Exception:                   # noqa: BLE001
                log.exception("补池提交失败")
                with self._lock:
                    for i, f in enumerate(self._futures):
                        if f is _PENDING:
                            self._futures[i] = None
                    # G4-R2 §五: 提交失败也算一次连续失败, 所以同样按"池空与否"
                    # 选序列 —— 否则空池时的这条兜底会退回 240/300 秒, 而它
                    # 恰恰是"池子空 + 提交路径出问题"这个最该快速重试的组合。
                    self._fail_streak += 1
                    self._retry_at = now + self._backoff_for_streak_now(
                        self._fail_streak)
                return
            with self._lock:
                # 只有还是自己占的那个标记才认(理论上期间不会被改, 但留个护栏)。
                # ⚠️ 它**不是** stop 防护 —— stop 由上面的 ⑩b 复查负责,
                # 别以为有了这个守卫就不用复查。
                for i, f in enumerate(self._futures):
                    if f is _PENDING:
                        self._futures[i] = fut
                        # 该 slot 起飞 -> 记它的 category in-flight
                        # reservation(§14)。
                        cat = str((to_submit or {}).get(
                            "requested_category", "") or "")
                        if cat:
                            self._in_flight_by_category[cat] = \
                                self._in_flight_by_category.get(cat, 0) + 1
                        break

    def _apply_result(self, kind: str, detail: str, extra: dict,
                      now: float) -> None:
        """把 worker 的终局结果记进计数/退避。

        两条**正交**的账, 不要互相吞:
          - **试玩结果**: `playtest_*_count` **无条件**按 status 自增。
            所以 INTERRUPTED 虽然不退避, 仍然计一次 interrupted ——
            否则区分不了"试玩系统经常坏"和"本次运行正常结束时被中止"。
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
            # **不是失败**。生成链因为 stop / shutdown 收手, 与
            # gen_fail(题不好) / add_fail(存不下) / exc(代码 bug) 是
            # 完全不同的第四类。它有自己的计数, 且**绝不**进失败链。
            self.interrupted_count += 1

        # ---- ②b G4-R2 §六: 分类账 ----
        # worker 在 `extra["reject"]` 里写一个**短标签**, 说明这道候选
        # 到底死在哪一步。与上面按 `kind` 分的账**正交**: `kind` 说的是
        # "这一轮的结果是什么"(ok/gen_fail/…), 这里说的是"**为什么**"。
        # 一个 `gen_fail` 可能是 structure_technical_fail 也可能是
        # truth_reject —— 不分开就永远只能靠人读日志。
        #
        # ⚠️ 认不出的标签一律忽略(不记), 而不是归到 other —— 指标里
        # 多一个恒为 0 的桶比"悄悄把未知原因算进某个已知桶"安全。新增
        # 出口时**必须**同时在这里加一个 key(否则它静默不计数)。
        _rej = str((extra or {}).get("reject") or "")
        if _rej in self.reject_count:
            self.reject_count[_rej] += 1
        elif _rej:
            log.warning("补池: 未知的 reject 标签 %r(未计入分类账)", _rej)
        if kind == "ok":
            self.reject_count["success"] += 1

        # ---- ③ 退避 ----
        # 成功入池清退避并且**重置连续失败序列**; 主动中止既不清也不加;
        # 其余一律按连续失败次数取递增档位。
        #
        # 为什么"主动中止"不能退避: 它是本次运行结束(或收到停止信号),
        # 不是故障。给它退避会让运维数据把"正常下播"读成"补池失败了"。
        #
        # 为什么"主动中止"也不能清退避: 它发生在**结果**里, 而不是"新
        # 一轮成功了"。它什么都没证明, 清退避等于让一次真失败白等。
        if kind == "ok":
            self._retry_at = 0.0
            self._last_fail = ""
            self._fail_streak = 0
            log.info("补池成功: 库存 -> %d", self._stock())
            return
        if (extra or {}).get("interrupted") or kind == "interrupted":
            log.info("补池主动中止(stop), 不计失败不退避: %s", detail)
            return

        # ---- refill-to-target: 内容淘汰不是基础设施故障，不值得等待 ----
        #
        # review_rewrite / truth_reject / validation_reject / safety_reject
        # 的含义都是"这一个候选不收"。下一次 draw 是另一组关键词/另一道题，
        # 等 30/60/120 秒不会提高成功率，只会让消费速度超过补给速度。
        # 技术失败仍走原退避，防止网关/存储真的故障时形成热循环。
        semantic_rejects = {
            "review_rewrite", "truth_reject",
            "validation_reject", "safety_reject",
        }
        if kind == "gen_fail" and _rej in semantic_rejects:
            self._retry_at = 0.0
            self._last_fail = detail or _rej
            self._fail_streak = 0
            log.info("补池候选淘汰(%s), 不退避，继续补到目标: %s",
                     _rej, detail)
            return

        self._fail_streak += 1
        wait = self._backoff_for_streak_now(self._fail_streak)
        self._retry_at = now + wait
        self._last_fail = detail or kind
        if self._refill_active:
            log.warning(
                "补池技术失败 %d 次(%s), 库存未恢复，短退避 %.0fs 后继续: %s",
                self._fail_streak, kind, wait, detail)
        elif wait > self._backoff_s:
            log.warning("补池连续技术失败 %d 次(%s), 退避 %.0fs: %s",
                        self._fail_streak, kind, wait, detail)
        else:
            log.warning("补池技术失败(%s), 退避 %.0fs: %s", kind, wait, detail)

    def _backoff_for_streak(self, streak: int) -> float:
        """连续第 `streak` 次失败该退避多久。

        序列递增(默认 30/60/120/240/300...), 最后一档封顶 —— 不要
        无限翻倍: 补池的价值上限就是"池子里多一道题", 等到 10 分钟
        已经和停掉它没区别了, 但保留 300 秒能保证它**最终**会再试。

        `streak` 从 1 开始(第一次失败取第一档)。

        ⚠️ **G4-R2 §五**: 这只是**序列取值**, 不知道池子空不空。该用哪条
        序列由 `_backoff_schedule_now()` 决定 —— 保持这个函数是纯的, 才能
        让"档位序列"与"当前该用哪条序列"分开测。
        """
        idx = max(0, int(streak) - 1)
        if idx >= len(self._backoff_schedule):
            return float(self._backoff_schedule[-1])
        return float(self._backoff_schedule[idx])

    def _is_empty(self) -> bool:
        """池子是不是**真的**没得播了(G4-R2 §五)。

        判据是 `stock == 0 or playable == 0`, 与 `_on_tick_locked_ish` 的
        补池触发条件**同一套**(那边是"少了就补", 这边是"没了就急")。

        fail **closed** 的方向在这里是反的: 读不到就返回 False(当作
        "不空"), 于是退避退回**保守**长序列。理由: 误判成"空"会让后台
        在没有确认缺货时改用激进档; 误判成"不空"最多是慢一点补上 ——
        而池子真的空了的时候, `stock`/`playable` 是读得到的(它们不依赖
        探针, 只读 pool)。
        """
        try:
            if self._stock() == 0:
                return True
            return self._playable(self._generation_inputs()) == 0
        except Exception:                       # noqa: BLE001
            log.exception("读空池状态异常, 按不空(保守退避)处理")
            return False

    def _schedule_now(self) -> tuple:
        """当前该用哪条退避序列。

            refill latch 活跃（库存还没恢复目标） -> 短序列 5/10/15...
            refill latch 已关闭（库存健康）        -> 长序列 30/60/120...

        关键不是“池子是不是刚好等于 0”，而是“补库存这项工作完成没有”。
        这样 stock 从 0 补到 1 后不会突然从 10 秒级冷却跳回 60 秒级。

        这里只换技术失败后的冷却时长；single-flight、库存目标、硬上限
        全部不变。
        """
        if self._refill_active:
            return self._refill_backoff_schedule
        return self._backoff_schedule

    def _backoff_for_streak_now(self, streak: int) -> float:
        """`streak` 次连续失败, **按当前池子状态**该等多久(G4-R2 §五)。

        与 `_backoff_for_streak` 的分工: 那个是"从这条序列里取第几档",
        这个是"该用哪条序列"。分成两个函数是因为它们回答的是两个不同的
        问题, 而且"序列对不对"与"选对了序列没有"必须能分别测。
        """
        sched = self._schedule_now()
        idx = max(0, int(streak) - 1)
        if idx >= len(sched):
            return float(sched[-1])
        return float(sched[idx])

    # ------------------------------------------------------------------
    def _ledger_ok(self) -> bool:
        try:
            return bool(self.pool.ledger_trustworthy)
        except Exception:                       # noqa: BLE001
            log.exception("读账本可信度异常, 本次不补池")
            return False

    def _stock_by_category(self) -> dict:
        """五类 eligible 库存(§10)。

        **分类信息可用**的判定: 池子里存在至少一道 haiguitang-v2 题。
        全部是历史/legacy 题(或空池)时返回空表 —— 此时 category-aware
        调度整条退位, latch 与 deficit 都只看全局 stock/playable。
        理由: legacy 题不冒充五类库存(§8/§9), 同样**不制造**五类缺口
        —— 否则任何装满 legacy 题的池子都会被误判成"五类全部缺货",
        逐类补到 60 道 hard max。
        """
        try:
            fn = getattr(self.pool, "stock_by_category", None)
            if fn is None:
                return {}
            d = fn()
            if not isinstance(d, dict) or not any(d.values()):
                return {}
            return d
        except Exception:                       # noqa: BLE001
            log.exception("读分类库存异常, 按空表处理")
            return {}

    def _pick_deficit_category_locked(self, stock_by_cat: dict):
        """这一拍该补哪个 category(§10/§11 的调度决策)。须持锁。

        返回 category 字符串("" = 全局 free path)或 `_NO_SLOT_NEEDED`
        (所有已知类都在 target 之上, 且没有 demand)。

        优先级:
            1. **demand 且库存不足**(主题投票的高优先级信号)
            2. **maintenance deficit**(eligible < target)
            3. 全局 free path("")
        effective_stock = actual + in-flight reservation —— 已在飞的
        缺口不再重复补(§14 防 overshoot)。

        tie-break: demand 按置位时间(最早优先), deficit 按五类枚举顺序
        (`V2_CATEGORIES`) —— 全部显式确定, 不依赖 dict 迭代顺序。
        """
        from .haiguitang_protocol import V2_CATEGORIES
        target = max(0, int(getattr(self.cfg, "pool_category_target", 10)))
        # ---- 1. demand 且库存不足(按 demand 置位时间升序) ----
        for cat, _ts in sorted(self._demand_categories.items(),
                               key=lambda kv: (kv[1], kv[0])):
            if self._effective_stock_locked(stock_by_cat, cat) < target:
                return cat
        # ---- 2. maintenance deficit(按五类枚举序) ----
        for cat in V2_CATEGORIES:
            cnt = stock_by_cat.get(cat)
            if cnt is None:
                continue        # 池子没给该类的分类读数(老池/空池) -> 跳过
            if self._effective_stock_locked(stock_by_cat, cat) < target:
                return cat
        # ---- 3. 全部健康 -> free path(全局补水) ----
        return _NO_SLOT_NEEDED

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

        ## Phase C: `limit` 固定跟着 `_playable_min`

        `PuzzlePool.playable_count()` 的 `limit` 是**硬早退**
        (`if n >= limit: break`), 不是"上限提示"。

        历史上这里曾需要"跟当前阶段目标走"(C3): REVEALED 的
        `_reveal_playable_target` 是 2, 而固定传 `_playable_min`(1)
        会让 latch 的"可播够了"停止条件永远不成立, 补池一路补到硬上限。

        Phase C 取消了两套库存目标, 阶段目标不复存在, 所以这个顺序
        约束**从根上消失** —— 调用方不必再"先算目标再 probe"。

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

    # ------------------------------------------------------------------
    def prewarm_should_continue(self, deadline=None,
                                should_abort=None) -> Any:
        """G4-R1: **预热专用**的协作式取消谓词。

        Phase C 之后后台补题本身只认生命周期, 但预热是**启动路径**上的
        一次性动作: 它跑在 `engine.start()` 之前, 而冷启动不能无限等 ——
        所以它比稳态后台多一条**总预算**(`deadline`)理由。这个差异是
        真实存在的, 预热谓词因此**不合并**进 `_background_should_continue`。

        两者的分工:

            `_background_should_continue`   稳态后台 —— 只看 停止(shutdown)
            `prewarm_should_continue`       冷启动预热 —— 看 停止 / 总预算

        预热期间本来就没有直播在跑, 所以"让路给直播"这个概念在这里
        **从来不适用**; 它该停的理由只有"停止信号"和"已经等够了"。

        ## 返回的是**谓词**, 不是判定结果

        调用方拿到的是闭包。这样预算的起点(`t0`)由调用方决定, 而这个类
        不需要知道"预热是从哪一刻开始的"。

        ## 时间预算的真实语义 —— cooperative

        `deadline` 之后**不再启动新的昂贵调用**; 已经在途的那一次 HTTP
        请求**允许自然返回**。这不是"硬 kill timeout", 也做不到 ——
        `urllib` 的请求发出去就取消不了, 假装能强杀只会让日志说谎。
        所以 90s 是**协作式预算**: 它的价值是"最坏情况下多发 0 次调用",
        而不是"到点立刻停"。

        ⚠️ `should_abort` 是给 `Director._prewarm` 传 `self._stop.is_set`
        用的 —— Ctrl-C / 停止信号必须能在预热中途生效, 否则"预热不影响
        可用性"就是假的。
        """

        def _go() -> bool:
            try:
                if should_abort is not None and should_abort():
                    return False
                if deadline is not None and time.monotonic() > deadline:
                    return False
            except Exception:                   # noqa: BLE001
                # fail closed: 谓词自己绝不抛。读不到状态时宁可停手 ——
                # 预热少一道题的成本远小于把一个异常冒进启动流程。
                log.exception("预热谓词异常, 停手")
                return False
            return True

        return _go

    def _generation_inputs(self) -> dict:
        try:
            d = self._probe_inputs()
            return d if isinstance(d, dict) else {}
        except Exception:                       # noqa: BLE001
            log.exception("取生成输入异常, 用空输入")
            return {}

    # ------------------------------------------------------------------
    def _generate_one(self, inputs: dict, slot: int = 0) -> None:
        """executor 线程。**唯一职责: 生成一道, (可选)试玩, add 进池。**

        唯一的写口是结尾那个**结果队列**(`_pending_results`) —— 绝不碰
        latch/退避/计数器(那些是 tick 线程的单写者变量)。
        队列(而不是旧的单槽)保证 2 个 worker 同拍完成时两条终局都留下
        (§14: 2 个 worker 同时完成不能互相覆盖)。

        `slot`: 本 worker 占的 slot 号 —— 它决定用哪只独立 writer。
        """
        kind, detail, extra = "exc", "", {}
        try:
            kind, detail, extra = self._generate_one_inner(
                inputs, slot=slot)
        except Exception as e:                  # noqa: BLE001
            log.exception("补池生成异常: %s", e)
            kind, detail, extra = "exc", str(e), {}
        finally:
            cat = str((inputs or {}).get("requested_category", "") or "")
            with self._lock:
                self._pending_results.append((kind, detail, extra))
                # ---- §14: 释放该 category 的 in-flight reservation ----
                # observed miss 一样释放 —— reservation 只代表"在飞",
                # 不代表"命中"。释放后 tick 重新算 deficit, requested
                # 仍未达标就继续请求该类。
                if cat:
                    left = self._in_flight_by_category.get(cat, 0) - 1
                    if left > 0:
                        self._in_flight_by_category[cat] = left
                    else:
                        self._in_flight_by_category.pop(cat, None)

    def _generate_one_inner(self, inputs: dict,
                            should_continue=None,
                            story_timeout=None,
                            slot: int = 0) -> tuple:
        """返回 `(kind, detail, extra)`。

        `extra` 目前只带一个 key: `playtest`(试玩 status)或 `interrupted`。
        tick 线程靠它做**正交**统计 —— 见 `_apply_result`。

        ## G2: 两条候选产生方式

            keyword2(默认)  抽 2 个关键词 -> Stage A -> Stage B -> 现有质量链
            classic(kill-switch)  pick_blueprint -> gen_spec  (逐位不变)

        `pool_keyword_seed_enabled=False` 时**完整**回到下面那条 —— 它是
        kill-switch, 不是"废弃路径", 所以两条都在这里显式并存。

        ## `should_continue` 的**注入点**

        默认 `None` => 用 `self._background_should_continue`(stop-only 的
        生命周期判据)。传别的谓词进去的只有一个调用者: 冷启动预热
        (`Director._prewarm`), 它要多一条总预算。

        ⚠️ 这个默认值 **Phase C 换过**: 原来是读相位的 `_should_continue`,
        现在后台本身不再有相位概念, 默认值就是纯 stop 判据。**不要**把
        相位 / pending / inflight / deadline 判据重新塞回默认路径。

        ## 为什么预热要单独注入

        预热跑在 `engine.start()` **之前**, 且不能无限等 —— 它需要一条
        总预算(见 `prewarm_should_continue`)。这不是"预热看得懂直播相位",
        而是"启动路径上的动作有期限"。稳态后台没有期限, 所以两者是两个
        谓词, 不能合并。

        ⚠️ 注入必须**一路传到底**, 否则 `--no-keyword-seed` 的预热照样死:
        它要穿过 keyword Stage A/B、classic `gen_spec`、Reviewer / audit
        的协作检查、以及试玩开始前那一处。任何一处漏传 == 那条链的预热
        回归原样。
        """
        if should_continue is None:
            should_continue = self._background_should_continue
        # ---- Issue #60 §12: 每个 worker 启动前拿到明确 GenerationBrief ----
        # `inputs["requested_category"]` 由 tick 的 deficit 选择决定(§10)。
        # 仍走生产唯一入口 `keyword_spec()` —— 不复制 Prompt/Truth/Surface
        # /Contract 逻辑。
        requested = str((inputs or {}).get("requested_category", "") or "")
        if self._keyword_enabled():
            return self._generate_keyword_one(
                inputs, should_continue, story_timeout=story_timeout,
                slot=slot, requested_category=requested)
        return self._generate_classic_one(inputs, should_continue,
                                          slot=slot)

    # ------------------------------------------------------------------
    def _init_keyword_bag(self, cfg: Any) -> None:
        """装配时建 keyword bag(§三 / §四 / §五)。**只在配置开启时做**。

        ## session seed 的来源(§四)

            给了 `keyword_session_seed`      -> 直接用
            只给了 `quality_seed`            -> `derive_session_seed()` 派生
            两个都没给                        -> 随机生成一次, 打 INFO 记下来

        第三条是刻意的: 随机 session seed 只在**启动时**取一次, 然后写进
        日志。于是"这一场直播的 pair 序列"事后永远可复现 —— 从日志里抄那个
        数就能重放。若每次抽词都 random, 复盘时就没有锚点。
        """
        # ⚠️ 这里**只能看配置开关**, 不能调 `_keyword_enabled()` ——
        # 后者**还要求 bag 已存在**, 而 bag 正是本方法要建的。
        # 写成 `if not self._keyword_enabled(): return` 会得到鸡生蛋:
        # bag 是 None -> `_keyword_enabled()` 假 -> 直接 return -> bag
        # 永远是 None -> 生产**静默地**退回 classic, 且 `_bag_error` 为空
        # (连"为什么降级"都没有日志)。
        # 这个 bug 是 `test_g3_good_corpus_activates_bag` 抓出来的:
        # 它注入一份**完好**的 corpus, 却发现 bag 没建起来。
        if not bool(getattr(cfg, "pool_keyword_seed_enabled", True)):
            return
        try:
            from .keyword_corpus import DEFAULT_CORPUS_PATH
            from .keyword_seed import derive_session_seed, load_bag
            path = str(getattr(cfg, "keyword_corpus_path", "")
                       or DEFAULT_CORPUS_PATH)
            ss = getattr(cfg, "keyword_session_seed", None)
            if ss is None:
                qseed = getattr(cfg, "quality_seed", None)
                if qseed is not None:
                    ss = derive_session_seed(qseed)
                else:
                    # ⚠️ 这一条**不能**用 self._rng: 那把 rng 服务 classic
                    # 链的 pick_blueprint, 从它取数会改变 live 序列。
                    # 用系统熵, 然后**立刻记进日志**。
                    ss = random.SystemRandom().getrandbits(64)
            self._bag, self._bag_meta = load_bag(path, ss)
            self._keyword_session_seed = int(ss)
            log.info("keyword2 bag 就绪: %s", self._bag_meta_line())
        except Exception as e:                       # noqa: BLE001
            # ---- §五: corpus 缺失 / 空 / 解析失败 => 显式降级 ----
            self._bag = None
            self._bag_meta = {}
            self._keyword_session_seed = None
            self._bag_error = "%s: %s" % (type(e).__name__, e)
            log.error(
                "keyword2 corpus 不可用, **本次配置整条 keyword2 链让位给 "
                "classic Blueprint 链**(不会回退人工词库): %s",
                self._bag_error)

    def _bag_meta_line(self) -> str:
        """§四 要求的那行 INFO 日志。抽成方法是为了让测试直接断言它。"""
        from .keyword_seed import describe_bag
        return describe_bag(self._bag_meta, self._keyword_session_seed)

    def _keyword_enabled(self) -> bool:
        """本条走不走 keyword2。**默认开**(§八), 关掉即 kill-switch。

        ⚠️ 只认配置, 不认"writer 有没有那两个方法"。用 `hasattr` 兜底会让
        测试替身(writer 是假的)静默切到 classic 链 —— 那样测试就测不到
        keyword 路径, 而生产却是另一套行为("Fake 比 production 更完整"
        的反面)。writer 缺方法是**装配错误**, 该让它响亮地失败。

        ⚠️ G3: 这里**还**要求 bag 存在。`_init_keyword_bag` 失败时
        (corpus 缺失/空/损坏)bag 是 None, 于是这里返回 False, 整条链
        安静但**有日志**地退回 classic —— 这是 §五 要的降级路径。
        """
        if not bool(getattr(self.cfg, "pool_keyword_seed_enabled", True)):
            return False
        return self._bag is not None

    def _generate_keyword_one(self, inputs: dict,
                              should_continue=None,
                              story_timeout=None,
                              slot: int = 0,
                              requested_category: str = "") -> tuple:
        """G2: `2-key -> Stage A -> Stage B`, 之后与 classic 路径**完全共用**。

        ## 主动中止检查点(§九)

        这条链比 classic 多一次**独立**的 LLM 阶段(Stage A), 所以中止检查
        必须覆盖它。四个位置:

            ① Stage A 之前          (骨架内)
            ② Stage A 之后 / B 之前  (骨架内)
            ③ Stage B 之后 / 审稿前  (Stage B 内部, 见 structure_original_idea)
            ④ 审稿之后 / audit 之前  (Stage B 内部, 同上)

        ⚠️ **G4-2 §四: ①② 现在住在 `keyword_seed.keyword_spec` 里**,
        因为 live 现场生成走的是**同一条链**(见 `director._riddle` 的
        fallback)。两份实现会在"哪里写 metrics / 哪里判中止"上漂, 而
        漂了以后 live 与 prefetch 出的题就不是同一种东西了。

        ③④ 落在 `structure_original_idea` 里是刻意的: 那里的检查点与
        `gen_spec` 的写法同源(同一个谓词、同一个 fail-closed 语义), 而
        在**这里**再抄一遍只会得到两份会漂的判定。

        ## 为什么 ①② 必须存在

        Stage A 是**新增的昂贵调用**。若不在它前后检查, 就会出现 G1 修掉
        的那个形状: 收尾/停止信号已经到达, 而后台还在往下走 —— 白白多占
        一份网关配额、拖长一次收尾。任务书 §九 原话: "不能因为新增
        Stage A/B 把已经修好的'一次运行结束后仍在烧昂贵调用'问题带回来。"

        任何一处 false -> `interrupted`(不计 gen_fail、不退避)。

        ⚠️ **谓词由参数注入**(默认 `self._background_should_continue`)。预热
        传的是它**自己**的谓词(stop + deadline) —— 见 `_generate_one_inner`。
        注入必须传进 `keyword_spec`(①②)与 `_finish_one`(试玩前 / 提交前)。
        """
        if should_continue is None:
            should_continue = self._background_should_continue
        if story_timeout is None:
            story_timeout = self._story_timeout
        from .keyword_seed import keyword_spec
        from .haiguitang_protocol import GenerationBrief
        recent = inputs.get("recent_signatures") or []
        avoid = inputs.get("avoid")
        # ---- Issue #60 §12: worker 启动前拿到明确 GenerationBrief ----
        # requested_category 为空 = 自由生成(GenerationBrief() 等价)。
        brief = GenerationBrief(
            requested_category=requested_category).require_valid()
        writer = self._writers[slot] if slot < len(self._writers) \
            else self.writer
        spec, reason = keyword_spec(
            writer, self._bag, self._keyword_session_seed,
            avoid=avoid, recent=recent,
            should_continue=should_continue,
            corpus_version=self._bag_meta.get("corpus_version", ""),
            story_timeout=story_timeout,
            brief=brief)
        if spec is None:
            if reason == "interrupted":
                return ("interrupted", "本次运行已结束, keyword2 主动中止",
                        {"interrupted": True})
            # ---- G4-R2 §六: 把"为什么没成"带进分类账 ----
            # `keyword_spec` 只回一个粗粒度的 `gen_fail` —— 那正是实播
            # 复盘时"只有 keyword2 未成题"的来源。细因写在**失败那一次
            # 的 spec.metrics 里**, 所以从 writer 的侧信道取。
            return ("gen_fail", "keyword2 未成题", self._reject_extra(writer))
        # ---- §12: requested vs observed 补货闭环 ----
        # A. requested ∈ observed categories -> 合法入池, deficit 被补上。
        # B. requested ∉ observed -> 题本身合格仍入统一池(贡献别的类),
        #    但 requested deficit 仍在 —— `extra["category_hit"]=False`
        #    让调度侧知道这一发没有补到 requested(不篡改 observed)。
        obs = list(getattr(spec, "categories", None) or [])
        hit = (not requested_category) or (requested_category in obs)
        # ---- 之后与 classic 路径完全一致(试玩 / add) ----
        return self._finish_one(spec, {"category_hit": hit,
                                       "requested_category":
                                           requested_category},
                                should_continue)

    def _reject_extra(self, writer=None) -> dict:
        """G4-R2 §六: 取上一次 keyword2 失败的**原因标签**。

        Stage B 把标签写进自己返回的那个 spec 的 `metrics["reject"]`,
        而那个 spec 在 `keyword_spec` 里被丢掉(它没有 puzzle)。所以
        这里从 writer 的侧信道(`_last_reject`)读 —— 与
        `_last_review_decision` / `_last_review_call_count` 是同一套约定:
        **实例属性是唯一能穿过"失败即丢弃"这条缝的东西**。

        读不到就返回空 dict —— 没有标签好过编一个错的标签。
        """
        w = writer if writer is not None else self.writer
        tag = str(getattr(w, "_last_reject", "") or "")
        return {"reject": tag} if tag else {}

    def _generate_classic_one(self, inputs: dict,
                              should_continue=None,
                              slot: int = 0) -> tuple:
        """旧路径: `pick_blueprint -> gen_spec`。

        保留它是为了 §八 的 kill-switch —— `pool_keyword_seed_enabled=False`
        必须完整回到这里。

        ⚠️ **谓词由参数注入**(默认 `self._background_should_continue`), 所以
        两条链在这一点上**必须对称** —— keyword2 能停在哪, classic 就能停在
        哪。预热走 classic 链时(`--no-keyword-seed`)必须也能被注入它自己的
        谓词, 否则 kill-switch 一开, 预热就重新变成"一道都出不来"。
        """
        if should_continue is None:
            should_continue = self._background_should_continue
        recent = inputs.get("recent_signatures") or []
        avoid = inputs.get("avoid")
        extra: dict = {}
        bp = self._pick_blueprint(recent, rng=self._rng)
        writer = self._writers[slot] if slot < len(self._writers) \
            else self.writer
        # ---- G1: 低优先级生成 ----
        # `should_continue` 在**每一次尚未发出的昂贵调用之前**被检查
        # (出稿 / 审稿 / truth audit / 下一稿, 见 `gen_spec`)。已经发出去
        # 的那次 HTTP 请求无法取消, 等它回来即可 —— 关键是**它一回来就
        # 不能再发下一次**。这正是要消灭的实播事故: 下一题已经开始现场
        # 生成, 而旧 prefetch 还在审稿 / 再出一稿, 两边同时占网关 51 秒。
        #
        # ⚠️ Phase C 之后 `should_continue` 为 False 的**唯一**含义是
        # "本次运行已结束"(stop / shutdown), 不再有"直播忙 -> 让路"这层
        # 意思。后台与直播是两条独立流水线, 各走各的 transport。
        #
        # 预算也换成后台自己的(少尝试, 不是降低题质): live 是 4 稿/90s,
        # 后台是 2 稿/25s。后台多试一稿的收益只是池子里多一道题, 代价
        # 却是多占一份网关配额、拖长一次收尾。
        spec = writer.gen_spec(
            avoid=avoid, blueprint=bp, recent=recent,
            max_attempts=self._prefetch_attempts,
            budget_s=self._prefetch_budget,
            should_continue=should_continue,
            # bp is None 时**真的**跳过 blueprint 硬校验, 而不是退回
            # 默认 blueprint(那是已经修过的 bug)。与 live 路径同一写法。
            enforce_blueprint=bp is not None)
        # ---- G1: 主动中止 —— 单独一类结果, **不能**记成 gen_fail ----
        # 判据看 metrics 里的显式标记(gen_spec 设的), 而不是猜 error
        # 文本; 也顺手兜住"writer 是替身、没有 metrics"的情况。
        if bool((getattr(spec, "metrics", None) or {}).get("interrupted")):
            return ("interrupted", "本次运行已结束, 本轮补池主动中止",
                    {"interrupted": True})
        # 中止时 gen_spec **故意**把 error 留空(它不是失败), 所以这里
        # 必须在判断 error 之前先判 puzzle 空 —— 否则会走进下面
        # "空谜面"那条, 把一次主动中止记成 gen_fail。
        if spec is None or not getattr(spec, "puzzle", ""):
            if bool(getattr(spec, "interrupted", False)):
                return ("interrupted", "本次运行已结束, 本轮补池主动中止",
                        {"interrupted": True})
            return ("gen_fail",
                    (spec.error if spec is not None else "spec=None")
                    or "空谜面", {})
        return self._finish_one(spec, extra, should_continue)

    # ------------------------------------------------------------------
    def _finish_one(self, spec: Any, extra: dict,
                    should_continue=None) -> tuple:
        """**两条链共用的收尾**: 试玩(可选) -> `pool.add`。

        G2 把这一段从 `_generate_one_inner` 里抽出来, 因为 keyword2 链与
        classic 链**必须逐字共用**它 —— 试玩门槛、add 失败即丢弃、
        `extra` 里 playtest 与入池两组账正交, 这些是 Q9/Q10/G4-C 定下的
        契约, 复制一份迟早会漂(然后两条链的入池语义就不一样了)。
        """
        # ---- Q10: AI 试玩(默认关闭, 开着才跑) ----
        #
        # ---- G4-C: 试玩也必须在**开始之前**检查取消 ----
        #
        # 生成链的检查点在 `gen_spec` / `keyword_spec` 内部(每一次尚未发出的
        # 昂贵调用之前), 但 `_playtest` **不在它们里面** —— 它在之后。于是
        # 存在这条缝:
        #
        #     稿子生成成功返回(完整的生成 + 审稿 + audit)
        #     ↓  这一段之间停止信号到达
        #     ↓  prefetch 仍然启动一次 AI 试玩
        #
        # 试玩本身是**若干次 LLM 调用**(模拟提问者反复问), 跑起来很贵。
        #
        # Phase C 的原则是"每一个尚未开始的昂贵阶段都必须先问一次要不要
        # 继续", 试玩没有理由例外 —— 只是因为它在生成链之外, 容易漏掉。
        #
        # ⚠️ G2: keyword2 链同样经过这里。Stage A / Stage B 各自有检查点,
        # 但**试玩这一处**仍然要判 —— 它是独立的一次(或多次)调用。
        #
        # ⚠️ 用注入的谓词, **不是**写死的后台判据 —— 预热走到这里时后台
        # 尚未 activate, 生命周期状态与稳态不同; 而且预热那份谓词带
        # deadline(见 `prewarm_should_continue`), 写死会让预算失效。
        #
        # `_finish_one` 里一共**三**层检查, 各管各的, 不要合并:
        #   ① 这里(试玩之前)   —— 要不要**开始**这次试玩?
        #   ② playtester 内部   —— 试玩**过程中**还继续吗?(每轮重查)
        #   ③ `pool.add` 之前   —— 要不要**写进池子**?(见本函数末尾)
        # ① 能省下 N 次 LLM 调用, ③ 省下一次写入; 少任何一个都有缝。
        if should_continue is None:
            should_continue = self._background_should_continue
        if self._playtest_enabled():
            if not should_continue():
                log.info("补池: 已请求停止, 试玩未开始(已生成的稿子丢弃)")
                return ("interrupted", "本次运行已结束, 试玩未开始",
                        {"interrupted": True})
            pt, why = self._playtest(spec, should_continue)
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

        # ---- ③ 提交前最后一道检查点(与上面的 ① 是**两个**不同的检查点) ----
        #
        # ① 管"要不要**开始**这次试玩"; 这里管"要不要**把它写进池子**"。
        # 两者之间可能隔着整个试玩(最坏 2N 次 LLM 调用), 停止信号随时
        # 可能到达 —— 特别是下播收尾那几秒。已经发出的调用收不回来, 但
        # **最后这次写入**必须被拦下: 停了就该停, 不该再往池子里灌库存。
        #
        # ⚠️ 必须放在**两条路径的汇合处**, 不能只写在
        # `if self._playtest_enabled():` 里 —— 试玩关着(**默认**!)时那
        # 整段被跳过, 从生成链返回到这里同样是零检查点。那正是
        # `prefill_pool.py` 里"候选完成后 pool.add 之前"补过的同一个洞。
        #
        # ⚠️ 用注入的 `should_continue`, **不是**写死的后台判据 —— 预热
        # 走到这里时那份谓词带 deadline(stop + 总预算)。写死会让预热
        # 在预算耗尽后照样写入。
        if not should_continue():
            log.info("补池: 已请求停止, 提交前放弃本稿(pool.add 未调用)")
            # `interrupted` 的语义是"已经开始的链被协作式收手"(与"从未
            # 开始"不同, 见 `_on_tick_locked_ish` 的 ⑩b)。这里候选确实
            # 走完了生成链, 所以记一次 interrupted_count 是对的 —— 而
            # `extra` 里**不带** `playtest` 键, 那次被丢弃的 PASS 不该
            # 进试玩账(否则试玩通过率会被它污染)。
            return ("interrupted", "本次运行已结束, 提交前放弃本稿",
                    {"interrupted": True})

        # ---- Issue #60 §16: 最终 atomic admission(stop gate 之后) ----
        #
        # 并发下多个 worker 都可能拿着同一份旧 stock recent 开始生成,
        # 最后同时产出相似的题。`add_with_final_admission` 在**同一把
        # pool 锁内**重读当前库存 signatures + 复用现有 deterministic
        # diversity primitives + add, check 与写入之间没有 TOCTOU:
        # 两个同时完成的相似 candidate 不可能都穿过去。
        #
        # 被挡的候选记明确 reject(addmission_reject), 不写 used ledger。
        try:
            ok, why = self.pool.add_with_final_admission(
                spec, source="prefetch")
        except AttributeError:
            # 测试替身 pool 没有 final-admission API -> 退回普通 add
            # (替身本来就不模拟并发, 行为等价)。
            ok, why = (bool(self.pool.add(spec, source="prefetch")),
                       "pool.add 返回 False")
        except Exception:                       # noqa: BLE001
            log.exception("final admission 异常")
            ok, why = False, "final admission 异常"
        if not ok:
            # 契约: add 失败 == 这次生成**丢弃**。
            #
            # **不**留内存副本、**不**直接 submit_riddle 上屏。否则池子里
            # 会同时存在两类库存(盘上可恢复 vs 内存一次性), 而内存题
            # 没有 used 行 —— 崩溃后既不在盘上也不在 used 里, Q8 验收点 4
            # ("pop 过的题重启不复活")立刻无法推理。
            #
            # `extra` 里那次 `playtest=PASS` **照样带回 tick 线程计数** ——
            # 试玩统计与入池统计正交, add 失败不能把 PASS 吞掉。
            return ("add_fail", why or "pool.add 返回 False", extra)
        return ("ok", "", extra)

    # ------------------------------------------------------------------
    def _playtest_enabled(self) -> bool:
        if self._playtester is None:
            return False
        return bool(getattr(self.cfg, "playtest_enabled", False))

    def _playtest(self, spec: Any, should_continue=None) -> tuple:
        """跑一次试玩。返回 `(PlaytestResult | None, why)`。

        `should_continue` 是**本次试玩的**谓词 override(见 `_finish_one`):
        稳态传 None(用 playtester 构造时注入的 stop-only 实例谓词);
        预热必须传它自己那份 `sc`(stop + **deadline**) —— 否则预热试玩
        会越过预算继续打 LLM 轮次, 预算形同虚设。

        **绝不让试玩异常冒泡** —— 它跑在 worker 线程里, 抛出去会变成
        `exc`, 那会把"试玩坏"记成"代码 bug", 混淆两类账。
        """
        try:
            r = self._playtester.run(spec, should_continue)
            return r, ""
        except Exception as e:                  # noqa: BLE001
            log.exception("试玩异常: %s", e)
            return None, str(e)

    # ------------------------------------------------------------------
    def shutdown(self) -> None:
        """收尾: **通知停止 + 回收 executor**。

        ## 与 `request_stop()` 的分工(P0)

        Phase C 把"通知停止"和"资源回收"拆开了, 原因是顺序:

            Director 检测到运行结束 -> request_stop()   <- 必须**立刻**
            Director 的 finally     -> shutdown()       <- 资源回收, 可以晚

        主循环结束后到 `finally` 之间还有几秒收尾(`Phase.STOPPED` 上要
        先 sleep)。这几秒里后台理论上还能起新候选, 所以
        "停止"必须在**离开主循环那一刻**就生效, 而 executor 的回收不必
        抢在那之前。`shutdown()` 自己也置位(幂等), 所以单独调用它仍然是
        完整语义 —— 只是**不要只依赖它在 finally 里被调**。

        ## 别把它理解成"下播立刻退出"

        标准 `ThreadPoolExecutor.shutdown(wait=False, cancel_futures=True)`
        的语义是 —— **`shutdown()` 自己**立即返回; 它不会取消**已经在
        执行**的任务, 而且 CPython 在**退出解释器**时会 join 线程池的
        工作线程(`concurrent.futures.thread` 的 atexit 钩子)。所以若
        `Ctrl-C` 的那一刻正好有一道 gen_spec 在跑, 进程仍会等它结束
        ——那是几十秒量级(gen_spec 的 budget_s=90)。

        这个行为是**接受**的, 不是疏忽: 补池同时最多只有一道在途, 所以
        "退出时正好在跑"是小概率; 为它换成可强杀的隔离执行模型会显著
        复杂化线程模型, 不值得。

        生成链感知到的仍然是**协作式**取消: 已发出的 HTTP 允许自然返回,
        下一个昂贵 stage 之前查一次 `_background_should_continue()`。
        """
        self.request_stop()
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)
        except Exception:                       # noqa: BLE001
            log.exception("补池 executor 关闭异常(忽略)")

    # ------------------------------------------------------------------
    def stats(self) -> dict:
        """补池计数。**分阶段 + 试玩拆开**, 不做成一个宽泛的 "失败数"。

        一个总的 `fail_count` 在这里是有害的: 生成失败/入池失败/代码异常/
        试玩没过是四类完全不同的故障, 合并之后"试玩失败率 40%"这句话
        既不知道是题不收敛、是网关抖了, 还是本次运行正常结束时被中止。
        """
        with self._lock:
            f = self._future
            return {
                "refill_active": self._refill_active,
                # `_PENDING` 也算在途(已决定提交、还没拿到 Future)。
                "in_flight": f is not None,
                # ---- Issue #60 §14: bounded concurrency 指纹 ----
                "concurrency": self._concurrency,
                "in_flight_count": sum(
                    1 for x in self._futures if x is not None),
                "in_flight_by_category": dict(self._in_flight_by_category),
                "demand_categories": sorted(self._demand_categories),
                "backoff_until": self._retry_at,
                # 滞回的两个输入。只看 stock 的话, "stock=5 playable=0"
                # (候选全被当前窗口挡住)这种现场指纹在计数里完全看不见。
                "stock": self._stock(),
                # 这里报的 playable 必须与 latch 判据用**同一个 limit**
                # (`self._playable_min`), 否则复盘时会看到"playable=1 但补池
                # 还在跑"这种自相矛盾的指纹。
                "playable": self._playable(self._generation_inputs(),
                                           limit=self._playable_min),
                "playable_min": self._playable_min,
                "max_size": self._max_size,
                # Phase C: 生命周期三态里最要紧的一格 —— "后台补池还开着吗"。
                # 补池在库存健康时**不跑**是正常的(不代表坏了), 所以判断
                # "补池停了"必须区分是"没活干"还是"已被终止"。
                "shutdown": self._shutdown_event.is_set(),
                # G4-R2 §五: 当前用的是哪条退避序列 —— 复盘时"为什么只等
                # 了 15 秒"与"为什么等了 240 秒"必须一眼看得出, 否则
                # 空池紧急档会被误读成"退避坏了"。
                "empty_pool": self._is_empty(),
                "backoff_schedule": list(self._schedule_now()),
                "prefetch_budget_s": self._prefetch_budget,
                "prefetch_story_timeout_s": self._story_timeout,
                "prefetch_max_attempts": self._prefetch_attempts,
                "fail_streak": self._fail_streak,

                "added": self.added_count,
                "generation_fail": self.generation_fail_count,
                "add_fail": self.add_fail_count,
                "exception": self.exception_count,
                # G1: 与 generation_fail **分开** —— 主动中止是生命周期, 不是故障。
                "interrupted": self.interrupted_count,
                "skip": self.skip_count,
                # G4-R2 §六: 决策树上的五个出口, 直接可读。实播复盘
                # (原来是"只有 keyword2 未成题")现在能一眼看到
                # "技术失败 3 / 审稿重出 2 / 审计不过 1 / 结构不过 0 /
                #  成功 1"。**这是 §六 的全部目的**。
                "reject": dict(self.reject_count),

                "playtest": {
                    "pass": self.playtest_pass_count,
                    "unsolved": self.playtest_unsolved_count,
                    "unavailable": self.playtest_unavailable_count,
                    "interrupted": self.playtest_interrupted_count,
                },

                "last_fail": self._last_fail,
            }
