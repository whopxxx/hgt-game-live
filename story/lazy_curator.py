#!/usr/bin/env python
# coding: utf-8
"""H3-B: Lazy Curator —— **按需**把外部候选题审进 curated 池。

## 它取代的是什么

H2 的模型是**一次性批量编译**:

    compile_curated.py -> 把 curated_raw 里 322 条**全部**扫一遍

这有三个问题:

  1. **烧钱**: 322 条要 322+ 次 LLM 调用, 而其中能进池的可能只有几十条。
     更要紧的是它**一次性**发生 —— 你无法先看看前 30 条的质量再决定。
  2. **和直播抢网关**: 批量跑起来就是几百次连续调用, 而直播随时可能出题。
  3. **拒题没有记忆**: 见 `tools/curated_ledger.py` 的说明 ——
     每轮重跑都把被拒的题再审一遍。

Lazy Curator 的模型是**库存驱动**的:

    candidate corpus + decision ledger
                |
        看库存够不够(不够才干活)
                |
        取**一条**尚未处理的 candidate
                |
        CuratedCompiler.compile_one()
                |
        accepted -> 进池 + attribution
        rejected -> 记 rejected(终态)
        技术失败 -> 记 technical_defer(可重试)
        直播忙   -> 记 interrupted(可重试)

## 三条铁律

### 一、**绝不在观众等下一题时现场审**

换题路径只能拿**已经 approved** 的题。"池空 -> 现在审一道"是禁止的:
审一道要编译 + 审稿 + 审计, 十几到几十秒, 观众就在那儿干等。

Lazy Curator 是**后台库存生产者**, 不是取题路径的一部分。它慢没关系,
池子空了才是事故 —— 所以它靠 `min_size` 提前启动, 而不是等空了才跑。

### 二、**single-flight**

同一时刻最多一个 worker, 由 `self._busy` 保证。两个 worker 会:
  - 各自选中**同一道**题(都还没写 decision) -> 白烧一次;
  - 同时在网关上, 正好是"不要和直播抢"要避免的。

### 三、**技术失败 ≠ 拒绝**

`rejected` 是终态, 一旦写下去那道题**永远不会**再被审。所以
timeout / 429 / 网关抖动必须走 `technical_defer`。这个区分由
`compile_one` 的 `info` 和调用方的异常捕获**两层**保证 —— 见
`_classify` 的说明。

## 与 PoolPrefetcher 的关系: **不复用**

`PoolPrefetcher` 走的是**发明**链(choose_blueprint -> gen_spec ->
emit_riddle), 它的重试/退避/预算都是围绕"让模型创作一道新题"设计的。
本模块走的是**搬运**链(读已有题 -> 编译)。两条链的第一步完全不同,
质量门之后的复用已经由 `CuratedCompiler` 完成了。

硬塞进 PoolPrefetcher 的后果是把"这批题是造的还是在搬的"变成一个
参数 —— 而那正是最该显式区分的东西。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, Optional

from tools.curated_compiler import CURATED_POLICY_VERSION, CuratedCompiler
from tools.curated_ledger import (
    ACCEPTED, COMPILE_INVALID_STAGES, INTERRUPTED, REJECTED, TECHNICAL_DEFER,
    DecisionLedger, content_hash_of, looks_like_compile_invalid,
)
log = logging.getLogger("hgt.lazycurator")


# ======================================================================
# 候选选择
# ======================================================================
def source_priority(rec: Any) -> tuple:
    """**先审谁**(不是"谁一定能进池")。

    第一版刻意简单、确定性 —— 复杂的排名在数据量只有几百条时收益为负,
    而且会让"为什么这道先审"变得无法解释。

    ## H4-A §十: 顺序按**新**的来源分层

        0  neurostellar/haiguitang   出题时就按海龟汤生成, 先验最合理
        1  TurtleBench                按海龟汤组织, 但只有 32 道独立故事
        2  SE situation/story/mystery SE 上最接近叙事题的标签
        3  其它 SE                    已知混着数学/物理/字谜
        9  未知来源

    ⚠️ haiguitang 排第 0 **只是审题顺序**, 不是准入加分。它一样要过
    curated-v3 全部十三条 —— 任务书 §十/§十五 明确: 它是"高质量候选源",
    不是"白名单直通源"。

    ⚠️ 也**不是** acceptance bonus: 排在前面只意味着"同样没审过时先审它",
    与"它更容易被收"是两件事。混起来会让验收报告里的 yield 变成自证。

    第 2/3 档里也可能有真海龟汤, 第 0/1 档里也可能有垃圾 —— 最终裁定权
    在 curated-v3 那十三门。
    """
    src = str(getattr(rec, "source", "") or "").lower()
    tags = {str(t).lower() for t in (getattr(rec, "tags", None) or [])}
    if "haiguitang" in src:
        return (0, str(getattr(rec, "external_id", "")))
    if "turtlebench" in src:
        return (1, str(getattr(rec, "external_id", "")))
    if tags & {"situation", "story", "mystery"}:
        return (2, str(getattr(rec, "external_id", "")))
    if "stackexchange" in src or "puzzling" in src:
        return (3, str(getattr(rec, "external_id", "")))
    return (9, str(getattr(rec, "external_id", "")))


def select_candidate(recs: list, ledger: DecisionLedger,
                     policy_version: str,
                     skip_ids: Optional[set] = None) -> Optional[Any]:
    """挑**一条**尚未处理的 candidate。没有则 None。

    跳过条件(全部满足才候选):
      - 在 `skip_ids` 里(本次运行已经碰过的 —— 见 `LazyCurator.step`)
      - license 不可用(`license_ok()`)—— 版权上根本不能收
      - 被 safety screen 标记过
      - 已被 near-duplicate 层标记(dup_reason 非空)
      - 账本里已经**终结**(accepted/rejected)

    **不跳** technical_defer / interrupted —— 它们明确是"下次再来"。
    """
    skip = skip_ids or set()
    todo = []
    for r in recs:
        if str(getattr(r, "external_id", "") or "") in skip:
            continue
        if str(getattr(r, "safety_flag", "") or ""):
            continue
        if str(getattr(r, "dup_reason", "") or ""):
            continue
        try:
            if not r.license_ok():
                continue
        except Exception:                       # noqa: BLE001
            continue
        if ledger.is_settled(r, policy_version):
            continue
        todo.append(r)
    if not todo:
        return None
    todo.sort(key=source_priority)
    return todo[0]


def stratified_sample(recs: list, n: int, *, buckets: int = 20) -> list:
    """§十五: **固定 seed 的确定性分层抽样**。

    ## 为什么不能用"按 external_id 字典序取头部"

    那是**最坏**的取样方式, 而且错得很隐蔽:

      - id 空间不是随机的。SE 的问题号递增, 于是字典序头部 = **最早
        的帖子**, 而那正是最老、最可能被编辑过、标签最不规范的一批;
      - haiguitang 的 id 是 `haiguitang:<hash>`, 字典序头部等于取哈希
        前缀最小的一批 —— 与"题目质量"完全无关, 但**看起来**像是
        随机;
      - 更要紧的是**不可复现地偏**: 换一次语料顺序就换一批题, 于是
        "上轮的 12.5%" 与 "这轮的 yield" 不可比。

    ## 做法: 按内容哈希分桶, 每桶取一条

    把 id 空间切成 `buckets` 个桶(默认 20), 每条记录按
    `content_hash` 的前缀落桶, 每桶取第一条 —— 于是:

        可复现    同语料 + 同 N -> 永远同一批(不依赖时间/随机)
        覆盖全体  id 空间被均匀切分, 不是只看哈希前缀最小的那批
        无偏      与来源顺序无关, 与字典序无关

    取不满 N 时就取到多少算多少(语料比 N 小是正常情况)。
    """
    if n <= 0 or not recs:
        return []
    nb = max(1, min(int(buckets), len(recs)))
    # 按**内容哈希**排序 —— 确定性, 且与语料里的顺序无关。
    def _h(r):
        return content_hash_of(r)
    ordered = sorted(recs, key=_h)
    picked: list = []
    seen: set = set()
    # ---- 第一轮: 每桶取一条(轮转, 保证覆盖整个哈希空间) ----
    for i in range(nb):
        if len(picked) >= n:
            break
        # 均匀取桶中心, 避免总是从同一个偏移开始
        idx = (i * len(ordered)) // nb
        while idx < len(ordered):
            r = ordered[idx]
            k = str(getattr(r, "external_id", "") or "")
            if k and k not in seen:
                seen.add(k)
                picked.append(r)
                break
            idx += 1
    # ---- 第二轮: 还不够就按哈希序补齐(仍然确定性) ----
    if len(picked) < n:
        for r in ordered:
            if len(picked) >= n:
                break
            k = str(getattr(r, "external_id", "") or "")
            if k and k not in seen:
                seen.add(k)
                picked.append(r)
    return picked[:n]


# ======================================================================
# LazyCurator
# ======================================================================
class LazyCurator:
    """后台库存生产者。**绝不抛、绝不阻塞直播。**

    它**拥有**一个专用 worker 线程(H3-D 起), 但**不拥有决策权**:
    "现在能不能开始"仍由装配层通过 `on_tick()` 每拍问一次, 因为只有
    装配层能同时看到 engine 与两个池。

    ## 为什么 H3-B 那版"不拥有线程"是错的(实测)

    第一版让调用方在自己的节奏里同步调 `step()`。装配层把这句放进了
    `director._scheduler()` —— 而那**是唯一驱动 `engine.tick()` 的
    线程**。于是:

        step() -> compile_one() -> LLM(十几到几十秒)

    期间 tick 定时、hint deadline、reveal deadline、下一题 deadline、
    scheduler push **全部被拖住**。这不是"这一拍慢一点", 是**心跳停了**。

    ## 现在的形状

        on_tick()   <- 装配层每拍调, **必须立刻返回**
          判断 should_start() -> 非阻塞 submit 一个 job
        worker      <- 专用单线程, 跑真正的 compile_one
          每个 gate 之间继续问 pressure(), 直播忙就让路

    worker 只有一个(`ThreadPoolExecutor(max_workers=1)` 或等价的专用
    daemon)。提交是 `submit()` 不是 `run()`: 队列里已有活时**直接丢弃**
    新提交 —— 这既保证 single-flight, 也保证 tick 不排队。
    """

    def __init__(self, cfg: Any, pool: Any, compiler: CuratedCompiler,
                 candidates: list, ledger: DecisionLedger,
                 pressure: Callable[[], dict],
                 clock: Callable = time.monotonic,
                 budget_seconds: Optional[float] = None,
                 generation_probe: Optional[Callable[[], dict]] = None):
        self.cfg = cfg
        self.pool = pool
        self.compiler = compiler
        self.candidates = list(candidates)
        self.ledger = ledger
        self._pressure = pressure
        self._clock = clock
        # ---- H4-E: 真实 live generation context ----
        #
        # `_stock()` 要问池子"**此刻**能播几道", 而那个答案依赖当前
        # `recent_signatures` / `avoid`。不传的话 dynamic gate 退化成静态门,
        # 于是后台以为"库存健康"而一道都不补 —— 而直播那边 `pop_next` 带窗口
        # 去问, 10 道全被挡住 -> 回落 fallback。这正是昨晚 22 题只出 5 道
        # curated 的成因。
        #
        # 注入的是**只读回调**(`engine.snapshot_generation_inputs`), 每次
        # `_stock()` **现取**当前快照, **绝不缓存** —— 窗口每播一题就变一次。
        # 离线预热没有直播窗口, 传 None(退化为静态语义, 与预热替身一致)。
        self._generation_probe = generation_probe
        #: 最近一次探针是否失败。**可断言**用(见 P0-4 regression):
        #: 失败时 `_stock()` 必须按 playable=0 处理(允许补货), 而不是
        #: 错误地报告"库存健康"。
        self._last_probe_failed = False
        #: 单条的预算上限。超了就算 technical_defer, 让下次重来 ——
        #: 不是 rejected。一道题卡住不该吃掉整个后台窗口。
        self._budget = float(
            budget_seconds if budget_seconds is not None
            else getattr(cfg, "curated_budget_seconds", 45.0) or 45.0)

        # ---- 库存迟滞(H3-B 九) ----
        self._target = max(1, int(getattr(cfg, "curated_target_size", 10) or 10))
        self._min = max(0, int(getattr(cfg, "curated_min_size", 4) or 4))
        self._playable_min = max(
            0, int(getattr(cfg, "curated_playable_min", 2) or 2))
        #: 硬上限: 到这儿就停, 防无限增长。
        self._max = max(self._target,
                        int(getattr(cfg, "curated_max_size", 20) or 20))
        #: 关闭开关(测试/调试用)。
        self._enabled = bool(getattr(cfg, "curated_background_enabled", True))

        #: **本次运行内**已经碰过的 external_id。见 `step` 的说明。
        self._tried: set = set()
        #: 防止 `on_tick` 与 worker 同时改候选选择(选择要读 `_tried`)。
        self._sel_lock = threading.Lock()

        # ---- H3-D: refill cycle(§三) ----
        #
        # 任务语义**不是**"跌破 min -> 补一两道 -> 回到 min 就停", 而是:
        #
        #     跌破 min/playable_min -> 进入 refill cycle
        #     cycle 内: 只要 stock < target 就继续补
        #     stock >= target       -> 退出 cycle
        #
        # 为什么非要这个状态: 没有它时, "补到 4" 与 "补到 10" 看起来
        # 一样 —— 每次补高一点就又回到空闲态, 于是下一题一播掉, 库存
        # 再次跌破 4, 又开始补。结果是**长期钉在低水位**, 而低水位正是
        # 最危险的: 池里只有 4 道题时任何一次审题失败都会让下一题回落
        # 现场生成(观众干等)。
        #
        # 这个标志在**内存里**, 不落盘 —— 它描述的是"本进程正处在一轮
        # 补水的中途"。进程重启后重新评估是**正确**的: 那时库存是新的
        # 事实, 该不该补水应该重新算。
        self._refilling = False

        # ---- H3-D: 专用 worker ----
        # 一个线程, **不是**无界线程池。`_busy` 仍然存在(它是
        # "有没有活正在跑"的权威), 但真正的互斥在 `_exec` 这一层。
        self._busy = False
        self._busy_lock = threading.Lock()
        self._exec = None
        self._last: dict = {}
        self._worker_enabled = True

    # ------------------------------------------------------------------
    # 库存
    # ------------------------------------------------------------------
    def _stock(self) -> tuple:
        """(长期有效库存, 下一题此刻能播)。

        两个数都要 —— 它们回答**不同**的问题(见 `pool.playable_count`
        的说明): `stock` 是"盘上还有没有", `playable` 是"下一题能不能
        立刻交付"。池里堆满但全被当前窗口挡住时, 前者健康而后者是 0,
        那时**仍然要补** —— 若只看 stock, 补池会以为一切正常, 而实际
        下一题只能回落现场生成(观众干等)。

        ## H4-E: playable 必须带**当前** recent/avoid 去问

        早先这里只传 `limit`, 于是 `playable_count` 的 dynamic gate 拿不到
        窗口 -> 退化成静态门 -> 后台看到"能播 10 道"而直播 `pop_next`
        (带窗口)一道都拿不到 -> 补池不补 -> 掉进 fallback 循环。

        探针**每次现取**(绝不缓存): 窗口每播一题就变一次。探针失败时
        **按 playable=0 处理** —— 宁可多补一轮, 也不要错误地认为库存健康
        (fail safe 的方向是"允许补货")。
        """
        try:
            stock = int(self.pool.stock_count(limit=self._max + 1))
        except Exception:                       # noqa: BLE001
            log.exception("读 stock_count 失败, 按 0 处理")
            stock = 0
        recent, avoid = None, None
        self._last_probe_failed = False
        if self._generation_probe is not None:
            try:
                ctx = self._generation_probe() or {}
                recent = ctx.get("recent_signatures")
                avoid = ctx.get("avoid")
            except Exception:                   # noqa: BLE001
                # 拿不到窗口 -> 不能假装库存健康。按 0 处理 = 允许补货。
                log.exception("读 generation context 失败, playable 按 0 处理")
                self._last_probe_failed = True
                return stock, 0
        try:
            playable = int(self.pool.playable_count(
                recent_signatures=recent,
                avoid=avoid,
                limit=self._playable_min + 1))
        except Exception:                       # noqa: BLE001
            log.exception("读 playable_count 失败, 按 0 处理")
            playable = 0
        return stock, playable

    def needs_work(self) -> bool:
        """现在**该不该**补库存(只读, 不干活)。

        ## 两个独立的"启动"条件(§三)

            stock    < min           -> 长期库存不够
            playable < playable_min  -> 下一题此刻没得播

        后者单独列出来, 因为它是**另一个问题**: 池里可能堆着 8 道,
        但全被当前 recent 窗口挡住 -> `playable` 是 0, 而下一题马上
        就要上。只看 stock 会以为一切正常, 补池一道都不补, 直播现场
        回落现场生成(观众干等)。

        ## refill cycle: 一旦开始, 补到 target 才停

        启动时机与**停止时机**是两个不同的水位, 这是迟滞的本质:

            空闲态: stock < min 或 playable < playable_min
                    -> 进入 refill cycle
            cycle 内: stock < target  -> 继续补
            stock >= target           -> 退出 cycle

        为什么不能写成 `stock < target 就开始`: 那会让库存一跌破 10
        就立刻开审, 于是"刚播掉一道"就触发一次 LLM —— 而那时正是
        观众在提问的时候。迟滞让它等到真快空了才动, 然后**一次补满**。

        为什么退出条件是 target 而不是 min: 停在 min 会让库存长期钉在
        低水位, 每一次审题失败都直接变成"下一题现场生成"。补满再停,
        才有冗余去吸收失败。

        `playable` 跌破线同样触发一轮 cycle —— 它描述的是"此刻能不能
        交付", 那比长期库存更急。
        """
        if not self._enabled:
            return False
        stock, playable = self._stock()
        if stock >= self._max:
            # 硬上限: 到顶了就不再进入 cycle。已经在 cycle 里也要退出
            # —— 否则 max 会被 refilling 绕过。
            self._refilling = False
            return False
        if not self._refilling:
            if stock < self._min or playable < self._playable_min:
                self._refilling = True
                log.info("curated 进入 refill cycle: stock=%d(<%d) "
                         "playable=%d(<%d) -> 补到 target=%d",
                         stock, self._min, playable, self._playable_min,
                         self._target)
        if self._refilling and stock >= self._target:
            self._refilling = False
            log.info("curated 退出 refill cycle: stock=%d >= target=%d",
                     stock, self._target)
        return bool(self._refilling)

    def _can_start(self) -> tuple:
        """`should_start` 的**内部**实现 —— **不看** `_busy`。

        为什么要拆开: `step()` 自己会置 `_busy`(它正在跑), 于是如果
        批内每条都问"我忙不忙", 第一条之后必然自我否决。`_busy` 是
        **入口**的判据(见 `should_start` / `on_tick`), 不是批内循环的。
        """
        if not self._enabled:
            return False, "已关闭"
        p = self._safe_pressure()
        # ⚠️ **先评估 refill 意图, 再看能不能开始**(§三)。
        #
        # 顺序反过来的后果很具体: 直播忙的时候 `_phase_ok` 直接返回,
        # 于是 `needs_work()` 从没被调用 -> 永远不进 refill cycle。
        # 而"观众在提问"正是**常态** —— 结果就是补池在真正需要它的
        # 时候从不记住自己该干活。
        #
        # `needs_work()` 是只读的(除了那个内存标志), 所以在这里调用
        # 没有副作用风险。
        want = self.needs_work()
        ok, why = self._phase_ok(p)
        if not ok:
            return False, why
        if not want:
            return False, "库存充足"
        return True, ""

    def should_start(self) -> tuple:
        """能不能**开始**一条。返回 `(ok, why)`。

        直播优先 —— 下列任何一条成立都不启动新的 LLM:
            已有 worker 在途 / 引擎已停 / 不在允许的 phase
            正式出题在途 / 真人 pending / inflight
            AI 玩家 in_flight / hint 在途 / reveal 在途 / 下一题马上开始

        ## phase policy(§二): 白名单, 不是黑名单

        第一版只查压力字段, 于是 `SETTING + 正式 RIDDLE worker 已启动
        + Lazy Curator 同时打 LLM` 是可能的 —— 出题是直播的**主线**,
        后台审题抢它的网关会直接变成"观众等下一题"。现在改成显式白名单:

            允许: REVEALED(距下一题 > guard) / QA(上面压力全为 0)
            禁止: SETTING / REVEALING / STOPPED / IDLE

        为什么 SETTING 必须**无条件**禁止: 出题窗口只有
        `setting_timeout_seconds`, 超时就要走重试/兜底。在它旁边再压
        一次几十秒的编译, 最坏情况是把一次出题挤成超时 —— 而超时的
        代价是观众看到的是兜底文案。
        """
        if self._busy:
            return False, "已有 worker 在途"
        return self._can_start()

    def _phase_ok(self, p: dict) -> tuple:
        """phase 白名单 + 直播压力。返回 `(ok, why)`。

        抽出来是因为**两处**要用同一套判定: `should_start()`(能不能
        开一条新的)与 `_should_continue()`(已经开始的那条要不要在
        下一个 gate 让路)。两处各写一遍必然漂移 —— 而漂移的后果是
        "能开始"与"能继续"两套标准, 与 G4-C 那次同一个教训。
        """
        from story.state import Phase
        if p.get("stopped"):
            return False, "引擎已停"
        ph = p.get("phase")
        # `pressure()` 给的是 Phase 枚举; 测试假件可能给字符串。
        # 两种都要认 —— 否则一个手写的探针会让整条判定静默失效。
        phv = getattr(ph, "value", ph)
        if phv == Phase.REVEALED.value:
            pass                               # 揭晓期间是最空的时候
        elif phv == Phase.QA.value:
            pass
        else:
            return False, f"phase={phv} 不允许后台审题"
        if p.get("riddle_inflight"):
            return False, "正式出题在途"
        if int(p.get("pending") or 0) > 0:
            return False, "真人 pending"
        if int(p.get("inflight") or 0) > 0:
            return False, "真人 inflight"
        if p.get("ai_player_in_flight"):
            return False, "AI 玩家在途"
        if p.get("hint_inflight"):
            return False, "hint 在途"
        if p.get("reveal_inflight"):
            return False, "reveal 在途"
        rem = p.get("reveal_remaining_seconds")
        if rem is not None and float(rem) <= self._guard_s():
            return False, f"下一题只剩 {rem}s"
        return True, ""

    def _guard_s(self) -> float:
        return max(0.0, float(
            getattr(self.cfg, "curated_start_guard_seconds", 15.0) or 15.0))

    def _safe_pressure(self) -> dict:
        try:
            return dict(self._pressure() or {})
        except Exception:                       # noqa: BLE001
            log.exception("压力探针抛异常 —— 视为'直播忙', 不启动")
            return {"pending": 1}               # fail safe: 当成忙

    # ------------------------------------------------------------------
    # 干活
    # ------------------------------------------------------------------
    def step(self, *, max_candidates: int = 1) -> dict:
        """尝试处理**至多** `max_candidates` 条。返回本次的统计。

        `max_candidates` 是**尝试**上限, 不是成功数上限(任务书十六:
        `--limit` 原先"一直审到成功 20 道", 拒绝率高时会调用上百次)。

        ## 为什么本次内不再碰同一条(实测踩到的)

        `technical_defer` / `interrupted` **不写终态**, 所以那条题仍然是
        候选 —— 于是 `select_candidate` 下一轮又把它挑出来, 在**同一次
        运行里**立刻重试。

        实测: `turtlebench:b51c7fba5006` 因为网关回了空 tool_input 被
        defer, 紧接着又被挑出来重审了一遍; 30 条预算里白白吃掉两条。

        这是错的。defer 的语义是"**下次**再试"(换个网络状况 / 换个
        上下文), 不是"立刻重试" —— 网关刚刚才抖过一次, 同一秒再问
        它一次几乎必然还是抖。真正的重试发生在**下一次运行**, 那时
        账本还在、候选还在。

        所以本次已经碰过的 external_id 记进 `_tried`, 同一轮不再回头。

        ## H3-D: `_tried` 从"本次 step 的局部变量"变成实例状态

        原先它是 `step()` 里的一个局部 set, 隐含假设是"一次 step 就是一
        次运行"。改成 worker 之后, 每拍提交一个 job, 每次都是**独立的**
        `step()` 调用 —— 局部 set 会在每次提交时重置, 于是 defer 的题
        在下一拍立刻又被挑出来, 同一次运行里反复重审(实测那两条就是
        这么被吃掉的)。所以它必须是**实例**状态, 跨 tick 累积。
        """
        out = {"processed": 0, "accepted": 0, "rejected": 0,
               "technical_defer": 0, "interrupted": 0, "skipped": 0}
        #: 谁跑整批, 谁负责放掉 `_busy`。
        #:
        #: ⚠️ 这里曾经只在 `_worker_main` 里清, 于是**同步**调用方
        #: (预热 CLI、以及 `step()` 的测试)第一次跑完就把 `_busy`
        #: 永久留在 True —— `should_start` 之后永远说"已有 worker 在途",
        #: 补池静默死掉。由 `step` 自己收尾之后两条路径都对:
        #: worker 那条会再清一次(幂等), 同步那条也不会漏。
        with self._busy_lock:
            self._busy = True
        try:
            for _ in range(max(0, int(max_candidates))):
                ok, why = self._can_start()
                if not ok:
                    out["skipped"] += 1
                    out["stop_reason"] = why
                    break
                # 选择 + 记账必须在同一把锁里 —— `on_tick` 与 worker 可能
                # 在不同的线程上跑, 两边都会读改写 `_tried`。
                with self._sel_lock:
                    rec = select_candidate(self.candidates, self.ledger,
                                           CURATED_POLICY_VERSION,
                                           skip_ids=self._tried)
                    if rec is None:
                        out["stop_reason"] = "没有未处理的 candidate"
                        break
                    self._tried.add(
                        str(getattr(rec, "external_id", "") or ""))
                res = self._curate_one(rec)
                out["processed"] += 1
                key = res.get("decision")
                if key in out:
                    out[key] += 1
        finally:
            with self._busy_lock:
                self._busy = False
        return out

    def _curate_one(self, rec: Any) -> dict:
        """审**一条**。**绝不抛** —— 异常一律转成 technical_defer。

        为什么这里要兜住所有异常: 后台任务抛出去会打断调用方的 tick,
        而 tick 是直播的心跳。宁可少一道题, 不能让直播抖一下。
        """
        eid = str(getattr(rec, "external_id", "") or "")
        t0 = self._clock()
        try:
            log.info("curated candidate start: %s", eid)
            spec, info = self.compiler.compile_one(
                rec, recent=[], blueprint=None,
                should_continue=self._should_continue)
            elapsed = self._clock() - t0

            decision, stage, reasons = self._classify(spec, info, elapsed)
            style_tags = list(info.get("style_tags") or [])
            # ---- §十二: 题型审核证据(审计用, **不进前端**) ----
            #
            # `compile_checks` / `review_checks` 是本次编译/审稿对十三条
            # 判据的逐条回答。它们**不参与** decision identity
            # (键仍是 id+hash+policy), 只让事后复盘能回答"当时判了
            # 什么" —— Reject Audit 正是卡在这里: 字段没落盘, 于是
            # "12.5% 是源差还是门误杀"无法从证据回答。
            #
            # ⚠️ H4-D §十二: v5 起这里落的是**全量十三条**(以前只留四条)
            # —— 题型三问降级成信号之后, 它们**唯一的用途**就是以后排序
            # (库存充足时优先更有反转的题)。只留四条的话, "这题简不简单"
            # 这个信息就永久丢了。
            #
            # ⚠️ 仍然**不进前端**: 观众看不到任何一格信号。
            checks = {}
            for _k, _dst in (("compile_checks", "compile"),
                             ("review_checks", "review")):
                _v = info.get(_k)
                if isinstance(_v, dict):
                    checks[_dst] = _v
            # ---- H4-D §三/§六: 题型信号单独落一格 ----
            #
            # 它**不是**拒绝理由, 只是"这道题偏简单"的记录。与 checks
            # 分开存: checks 是**原始逐条答复**, signal 是**判定后的
            # 结论**(只有"明确不理想"才在里面)。混在一起会让人分不清
            # "模型答了 false"与"我们认为这是个负面信号"。
            for _k in ("story_signal", "review_signal"):
                _v = info.get(_k)
                if _v:
                    checks.setdefault("signals", {})[_k] = list(_v)

            # ---- §四: accepted 是**最终 commit marker**, 必须最后写 ----
            #
            # 旧顺序是"先记 accepted, 再写池/署名"。那在协议上是错的:
            # accepted 的语义是"**已经**进入 active curated stock", 而
            # 写它的那一刻副作用还没发生。于是任何一步失败都会留下
            # "账本说 accepted, 池里没有 / 没有署名" 的半状态 —— 而且
            # accepted 是终态, 那道题**永远不会**被重审。一道题无声地没了。
            #
            # 现在的顺序:
            #     ① 落池(pool)      失败 -> 记 defer, 那道题下次再来
            #     ② 落署名(attr)    失败 -> 记 defer, 并**回滚池行**
            #     ③ 记 accepted     失败 -> 记 defer(池里有孤儿行,
            #                                但孤儿行不可播, 见下)
            #
            # 第 ③ 步失败时盘上确实会留下一行"没有 accepted 决策的池
            # 行"。这不是靠"相信它不会发生"来处理的, 而是靠 live
            # eligibility **主动识别并拒绝**它 —— 见
            # `_validate_pool_spec` 与 `_orphan_guard` 的说明。不变量:
            #
            #     playable curated item => attribution 存在 => accepted 决策存在
            #     accepted 决策         => active pool item 存在 => attribution 存在
            if decision == ACCEPTED and spec is not None:
                ok, fail_where = self._commit(spec)
                if not ok:
                    # 账本里**只**写 defer —— accepted 一行都没有。
                    self.ledger.record(rec, decision=TECHNICAL_DEFER,
                                       policy_version=CURATED_POLICY_VERSION,
                                       stage=fail_where,
                                       reasons=[f"{fail_where}_failed"],
                                       style_tags=style_tags,
                                       checks=checks)
                    log.error("curated deferred: %s reason=%s_failed",
                              eid, fail_where)
                    return {"decision": TECHNICAL_DEFER}
                # 副作用都在盘上了, 现在才允许写终态。
                if not self.ledger.record(rec, decision=ACCEPTED,
                                          policy_version=CURATED_POLICY_VERSION,
                                          stage="accepted", reasons=[],
                                          style_tags=style_tags,
                                          checks=checks):
                    # accepted 写不下去 -> 本次**不算成功**。
                    #
                    # ⚠️ 光靠"没有 accepted 决策 -> 不可播"这条**不够**:
                    # 下次重试会走到这里再写一行池记录, 于是盘上留下
                    # **两份**同样的题(孤儿 + 新的), 而重试那条 accepted
                    # 决策会让**两行都**通过准入门(准入门按内容查账本,
                    # 不看是哪一行写的)-> 同一道题进池两次。
                    #
                    # 所以必须像署名失败那样**把孤儿行作废掉**。墓碑按
                    # `pool_key` + **位置**(见 `_voided_keys`)作废它之前的
                    # 同 key 行 —— 重试写下的新行在墓碑之后, 不受影响。
                    self._void_pool_row(spec, "accepted_ledger_write_failed")
                    self.ledger.record(rec, decision=TECHNICAL_DEFER,
                                       policy_version=CURATED_POLICY_VERSION,
                                       stage="ledger_write",
                                       reasons=["accepted_write_failed"],
                                       style_tags=style_tags,
                                       checks=checks)
                    log.error("accepted 决策写盘失败(该题不可播, 下次重试): %s",
                              eid)
                    return {"decision": TECHNICAL_DEFER}
                # ---- §一-1: 让**本场直播**立刻看见这道题 ----
                #
                # 落盘与写决策都不足以让它可播: `PuzzlePool` 是启动时
                # load 到内存的快照, 不重新 load 就看不见新行 —— 于是
                # "补了一道题, 但下一题还是要现场生成"。
                #
                # 这里**必须**走池的正式激活 API, 不许直接改 `_items`
                # (那会绕过准入门), 也不许再调 `add()`(那会写第二行)。
                #
                # ⚠️ 失败**不算** accepted 失败: 盘上的一切都成立,
                # 下次启动照样读得到。这里只影响"本场立即可播"。
                self._activate(spec)
                stock, playable = self._stock()
                log.info("curated accepted: %s stock=%d playable=%d",
                         eid, stock, playable)
                return {"decision": ACCEPTED, "stock": stock,
                        "playable": playable}

            # 非 accepted 的三种状态**立刻**记 —— 它们不需要等副作用。
            self.ledger.record(rec, decision=decision,
                               policy_version=CURATED_POLICY_VERSION,
                               stage=stage, reasons=reasons,
                               style_tags=style_tags,
                               checks=checks)
            if decision == REJECTED:
                log.info("curated rejected: %s reason=%s", eid,
                         ",".join(reasons[:3]) or stage)
            elif decision == INTERRUPTED:
                log.info("curated interrupted: live pressure (%s)", stage)
            else:
                log.info("curated deferred: %s reason=%s", eid,
                         ",".join(reasons[:2]) or stage)
            return {"decision": decision}
        except Exception as e:                  # noqa: BLE001
            # 未预期的异常 = 技术问题, **不是**内容拒绝。绝不能记 rejected。
            log.exception("curated 处理异常(记 technical_defer): %s", eid)
            self.ledger.record(rec, decision=TECHNICAL_DEFER,
                               policy_version=CURATED_POLICY_VERSION,
                               stage="exception",
                               reasons=[f"exception:{type(e).__name__}"])
            return {"decision": TECHNICAL_DEFER}

    # ------------------------------------------------------------------
    def _should_continue(self) -> bool:
        """交给 `compile_one` 的让路回调。False = 停。

        ⚠️ 这里**不看** `needs_work()`: 一旦开始审了, 就把这一条审完
        (除非直播来抢资源)。中途因为"库存刚好够了"而放弃会浪费掉
        已经花掉的 LLM 调用 —— 而且那道题下次还得从头再来。
        库存判断只在**启动前**做(`should_start`)。

        但 phase 白名单与直播压力**照样要查** —— 而且必须与
        `should_start` 用**同一套**判定(`_phase_ok`)。两处各写一遍的
        漂移是很具体的: "允许在 QA 开始" 但 "QA 一开始就让路" 会让
        新审的题永远走不到写盘那一步, 每次都在第一个 gate 被中断。
        """
        p = self._safe_pressure()
        ok, _why = self._phase_ok(p)
        return ok

    # ------------------------------------------------------------------
    # H3-D: 非阻塞调度
    # ------------------------------------------------------------------
    def on_tick(self, *, max_candidates: int = 1) -> dict:
        """**每拍调一次, 必须立刻返回。** 返回本次是否提交了活。

        ## 契约

            绝不阻塞、绝不抛。
            LLM 在专用 worker 线程里跑, 本函数只做判断 + 提交。

        装配层(`director._scheduler`)就是 tick 线程本身, 所以**任何**
        在这里等待的东西都会直接变成心跳变慢。这正是 H3-B 那版的
        生产 Blocker: 它在这里同步跑了 compile_one。

        ## 为什么用 `submit` 而不是排队

        worker 只有一个。已经有活在跑时**直接丢弃**这次提交(返回
        `submitted: False`) —— 而不是排进队列。原因:

          1. single-flight 是硬要求(两个 worker 会选中同一道题白烧一次,
             而且同时压在网关上 —— 那正是"不要和直播抢"要避免的);
          2. 排队会让"库存不够"这件事被**延迟**发现: 队里堆了 5 个 job,
             每个几十秒, 而这段时间里压力可能早就变了。每一拍重新判断
             一次, 用的是**当下**的压力。

        所以 `max_candidates` 在这里的语义是"这一拍要不要开一条",
        而不是"这一拍要开几条"。与 CLI 的 `--max-candidates`(总共
        处理几条)是两个不同的东西, 名字相同但作用域不同。
        """
        out = {"submitted": False, "reason": ""}
        if not self._worker_enabled:
            out["reason"] = "worker 已关闭"
            return out
        if int(max_candidates) <= 0:
            out["reason"] = "max_candidates=0"
            return out
        ok, why = self.should_start()
        if not ok:
            out["reason"] = why
            return out
        self._ensure_worker()
        if self._exec is None:
            out["reason"] = "worker 不可用"
            return out
        # ---- §一-6: 必须在 submit **之前**原子地占住 `_busy` ----
        #
        # 早先是"先 submit, 再让 worker 去置 `_busy`"。那有一个真实的
        # 竞态窗口:
        #
        #     tick #1: should_start() -> True, submit(job A)
        #              worker 还没被调度起来, _busy 仍是 False
        #     tick #2: should_start() -> True(它看到的是 False!)
        #              submit(job B)
        #     -> 两个 job 在同一个 max_workers=1 的池里**排队**
        #     -> 两个 worker 各自选候选、各自打 LLM
        #
        # 后果正是 single-flight 要防的两件事: 选中同一道题白烧一次, 以及
        # 同时压在网关上抢直播的配额。
        #
        # `ThreadPoolExecutor.submit()` 本身**不阻塞**, 所以这段临界区
        # 极短; 但它必须与 `should_start` 里的那次读取互斥。
        #
        # 用 `_busy_lock` 而不是无锁 CAS: 这个锁在 `step()` 的收尾路径上
        # 也要拿, 两处必须用**同一把**(否则占位与释放会互相看不见)。
        with self._busy_lock:
            if self._busy:
                # 另一拍已经抢先占位 —— 丢弃本次提交(不是排队)。
                out["reason"] = "已有 worker 在途"
                return out
            self._busy = True
        try:
            self._exec.submit(self._worker_main, int(max_candidates))
            out["submitted"] = True
        except Exception:                       # noqa: BLE001
            # 线程池满了 / 已关闭 —— 都不是直播该关心的事。**但占位
            # 必须放掉**, 否则 curator 从此再也提交不了(fail closed
            # 变成了"永久停机")。
            with self._busy_lock:
                self._busy = False
            log.warning("提交 curator job 失败(已忽略)", exc_info=True)
            out["reason"] = "submit 失败"
        return out

    def _ensure_worker(self) -> None:
        """惰性建线程池。**最多一个线程**(§一: 不要建无界线程)。"""
        if self._exec is not None:
            return
        try:
            from concurrent.futures import ThreadPoolExecutor
            self._exec = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="hgt-curator")
        except Exception:                       # noqa: BLE001
            log.exception("建 curator worker 失败 —— 退化成不同步审题")
            self._exec = None

    def _worker_main(self, max_candidates: int) -> None:
        """worker 线程的入口。**绝不抛** —— 后台线程抛出去会静默死掉。

        `_busy` 的收尾由 `step()` 自己负责(见那里的说明), 所以这里
        只需要兜住异常。
        """
        try:
            self.step(max_candidates=max_candidates)
        except Exception:                       # noqa: BLE001
            log.exception("curator worker 异常(已忽略)")
            # 兜底: `step` 若在 `_busy` 置位**之前**就炸了(比如
            # 参数转换), 那它没有机会清。这里再清一次(幂等)。
            with self._busy_lock:
                self._busy = False

    def shutdown(self, *, wait: bool = False) -> None:
        """停 worker。给测试与进程收尾用。**幂等。**"""
        ex = self._exec
        self._exec = None
        self._worker_enabled = False
        if ex is not None:
            try:
                ex.shutdown(wait=wait)
            except Exception:                   # noqa: BLE001
                log.warning("关闭 curator worker 失败(已忽略)", exc_info=True)

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """等当前 job 跑完(测试/收尾用)。返回是否真的空了。"""
        deadline = time.monotonic() + max(0.0, float(timeout))
        while time.monotonic() < deadline:
            if not self._busy:
                return True
            time.sleep(0.01)
        return not self._busy


    # ------------------------------------------------------------------
    #: 这些 stage 的死因是**这次没编成**, 不是"这道 canonical 题不合格"。
    #:
    #: 任务书 §五/§六: `rejected` 是**终态**, 它必须只表示一件事 ——
    #:
    #:     canonical surface + canonical bottom 本身不符合 curated policy
    #:
    #: 而下面这些全是**编译期结构问题**: fair_clue quote 抄错一个字 /
    #: hint 数量不对 / hint 超长 / fact id 引用错 / discovery beat 接线错 /
    #: 生成结构超内部上限。它们的共同点是:
    #:
    #:     原题可能完全没问题 —— 是**这一次搬运**没搬成功。
    #:
    #: 记成 rejected 会让那道题**永久消失**(而它可能是好题), 而且会让
    #: source yield 被系统性低估 —— 审计已经证明同一种"酒吧止嗝"题里
    #: 一条 accepted、另一条仅因为 fair_clue quote 错就被 rejected。
    #: 那是**确定性误分类**, 不是源质量差。
    #:
    #: 所以它们一律 technical_defer(可重试), 报告里单独统计成
    #: `compile_invalid`。清单定义在账本里(报告口径与决策语义必须同源)。
    _COMPILE_INVALID_STAGES = COMPILE_INVALID_STAGES

    def _classify(self, spec: Any, info: dict, elapsed: float) -> tuple:
        """把 `compile_one` 的结果翻成 **四态之一**。返回 `(decision, stage, reasons)`。

        ## 为什么这个映射必须写在一处

        `rejected` 是**终态** —— 写错了那道题永远消失, 而且没有任何地方
        会显示"我们丢了几道"。所以判定必须集中、可读、可测, 不能散落
        在 if/else 里。

        ## v4: `rejected` 的语义被**收窄**了(任务书 §四/§六)

        只有**内容判决**才有资格写 `rejected`:

            无反常点 / 答案不解释谜面 / 纯知识点 / 纯物理技巧 /
            依赖冷知识 / 纯文字游戏 / 完全无唯一解释 / 内容不适合直播

        编译期结构失败(quote 抄错 / hint 数量 / fact id / schema / 接线)
        **不在此列** —— 那是"这一次没编成", 不是"原题永远是垃圾"。

        映射(v4):

            info["interrupted"]        -> interrupted          (可重试)
            spec 非空                  -> accepted             (终态)
            info["technical"]          -> technical_defer      (可重试)
            stage 属于 _COMPILE_INVALID-> technical_defer      (可重试)
            预算超了 / compile_call    -> technical_defer      (可重试)
            内容判决(ai_gate/story_gate/
              story_review)            -> rejected             (终态)
        """
        reasons = [str(r) for r in (info.get("reject_reasons") or []) if r]
        stage = str(info.get("stage") or "")

        if info.get("interrupted"):
            return INTERRUPTED, str(info.get("interrupt_at") or stage), reasons
        if spec is not None:
            return ACCEPTED, "accepted", reasons
        # ---- §一-3: 技术失败一律 technical_defer, 绝不是 rejected ----
        #
        # 这条**必须**排在所有内容判定之前。技术失败的定义很窄:
        #
        #     timeout / 网关错 / 空 tool_input / schema 坏掉 / 预算超了
        #
        # 它们的共同点是"**这次没审成**", 而不是"这道题不行"。记成
        # rejected 会让那道题永久消失 —— 而且没有任何地方会显示丢了。
        if info.get("technical") or stage.endswith("_technical"):
            return TECHNICAL_DEFER, stage or "technical", reasons or ["technical"]
        if elapsed > self._budget:
            # 超预算说明这一条特别慢(网关慢 / 重试多)。把它当成
            # 内容问题是不公平的 —— 换一次网络它可能就过了。
            return TECHNICAL_DEFER, "budget", ["budget_exceeded"]
        if stage == "compile_call":
            return TECHNICAL_DEFER, stage, reasons or ["technical"]
        if stage in ("exception", "pool_write"):
            return TECHNICAL_DEFER, stage, reasons or [stage]
        # ---- §五/§六: 编译期结构失败 -> compile_invalid(可重试) ----
        #
        # ⚠️ 这一支必须排在下面的 `REJECTED` **之前**。审计里 30 条样本
        # 有 6 条死在这里, 而它们**全部**是接线/枚举问题, 没有一条是
        # 内容判决 —— 也就是说旧实现把 6 道可能是好题的题永久吃掉了。
        #
        # stage 保持原值(报告要按原因分类), 但 decision 是 defer。
        if stage in self._COMPILE_INVALID_STAGES:
            return (TECHNICAL_DEFER, stage,
                    reasons or [f"compile_invalid:{stage}"])
        # ---- reason 说是结构问题 -> 同样不是内容判决 ----
        #
        # 实测错配: `_deepest` 记的是**走到过最深**的门, 而
        # `reject_reasons` 取的是**最后一次**尝试的原因 —— 两者来自不同
        # 稿件时, 会出现 stage=truth_audit 而 reason="第 2 条提示超过
        # 30 字"。只看 stage 会把一条 hint 超长算成"审计没过"。
        #
        # ⚠️ 但**硬内容门优先**: `ai_gate` / `hard_gate` 是确定性内容
        # 判决, 它们一旦出现就说明这道题**内容**已经被否了 ——
        # 那时不该因为 reason 里恰好有个"提示"字样就改判成 defer。
        #
        # ⚠️ H4-D §六: 这里**不再包含** `story_gate` / `story_review`。
        # v5 起题型信号不拒题, 所以那两个 stage 不会再产生; 留着它们
        # 会让"旧账本里那条 decision 是什么"与"新代码会怎么判"看起来
        # 不一致 —— 而旧 decision 是**已经落盘的事实**, 不重判。
        if stage not in ("ai_gate", "hard_gate") and \
                looks_like_compile_invalid(stage, reasons):
            return (TECHNICAL_DEFER, stage or "compile_invalid",
                    reasons or [f"compile_invalid:{stage}"])
        # ---- 复核**缺失**同样是技术失败, 不是内容拒绝 ----
        #
        # `story_review_missing` 出现在两处: 编译期拿不到 Reviewer 的
        # 答复, 或答复里没有那四个字段。两种都是"没审成", 换一次网络
        # 可能就好了。记成 rejected 会永久吃掉一道题 —— 那正是 §一-3
        # 要禁止的。
        #
        # ⚠️ H4-D §七: v5 里**Reviewer 缺信号字段不算技术失败** ——
        # 那三个题型字段只是信号, 没答就没答, 不影响这道题能不能播。
        # 所以这条只对**旧 policy 的 decision** 有意义(v5 不会再产生它)。
        if "story_review_missing" in reasons:
            return TECHNICAL_DEFER, stage or "story_review", reasons
        return REJECTED, stage or "unknown", reasons

    # ------------------------------------------------------------------
    def _commit(self, spec: Any) -> tuple:
        """入池 + attribution。返回 `(ok, fail_where)`。

        ## 顺序: **先署名, 再入池**? 不 —— 先入池, 但失败要能回滚

        两个副作用都是 append-only 的追加, 而追加**不能撤销**。所以
        "回滚"在这里的含义不是删除那一行(那会破坏 append-only 的
        崩溃安全), 而是:

            池行写了但署名失败
              -> 追加一条**墓碑**(`void: True` + pool_key),
                 让这一行立刻不可播;
              -> 返回 False, 调用方记 defer, 那道题下次重新审、重新写。

        为什么墓碑而不是删除: 与 used 账本同一个理由 —— 重写整份文件
        的崩溃窗口会丢掉**全部**历史, 追加的最坏情况只丢最后一行。而
        "读一行时看到 `void: True` 就跳过"是一个**读侧**判定, 崩溃在
        追加墓碑之前时, 那一行仍然是"没有 void 的孤儿行" —— 会被下面
        的孤儿判定挡下, 于是两种崩溃点都安全。

        ## 为什么署名失败必须降级(而不是像旧版那样只打个 ERROR)

        旧版 `_commit` 在署名写盘失败时**仍然返回 True**, 理由是"题目
        本身是好的, 版权归属还能从 decision 里追"。那违反了一条产品
        不变量:

            playable curated item => attribution 一定存在

        没有署名的题**不许播**。这不是归档完整性问题 —— 是版权问题:
        那道题的谜面/谜底是别人的 CC BY-SA 作品, 播出时不带署名就是
        违约。所以署名写不下去 -> 这道题本次不能进 active stock。
        """
        pool_key = ""
        try:
            pool_key = _spec_key(spec)
            rec_out = {
                "pool_version": 1,
                "pool_key": pool_key,
                "added_at": time.time(),
                "added_by": "curated-lazy",
                "spec": spec.to_archive(),
            }
            if not _append_jsonl(self._pool_path(), rec_out):
                return False, "pool_write"
            attr = self._attr_record(spec)
            if not _append_jsonl(self._attr_path(), attr):
                # 池里已经有行了 -> 追加墓碑让它不可播。
                self._void_pool_row(spec, "attribution_failed")
                return False, "attribution_write"
            return True, ""
        except Exception:                       # noqa: BLE001
            log.exception("入池异常")
            return False, "pool_exception"

    def _void_pool_row(self, spec: Any, reason: str) -> bool:
        """给刚写下的池行追加一条**墓碑**, 让它立刻不可播。

        ## 两条失败路径共用它

            _commit:    署名写失败        -> 作废池行
            _curate_one: accepted 写失败   -> 作废池行

        后者是 H3-D3 §一-5 补上的: 早先只靠"没有 accepted 决策不可播"
        兜底, 那**不够** —— 下次重试写的新行会让那条 accepted 决策同时
        授权**两行**, 于是同一道题进池两次。墓碑按位置作废它之前的行,
        重试的新行不受影响(见 `PuzzlePool._voided_keys`)。

        ## 墓碑自己写失败时**不额外降级**

        孤儿判定(池准入门里那条 accepted-decision 检查)会在读侧挡住它
        —— 两道防线, 不依赖其中任何一道成功。但仍然要吵一声, 因为盘上
        多了一行需要人工看一眼的东西。
        """
        try:
            pool_key = _spec_key(spec)
            if not _append_jsonl(self._pool_path(),
                                 {"pool_version": 1,
                                  "pool_key": pool_key,
                                  "void": True,
                                  "void_reason": reason,
                                  "added_at": time.time()}):
                log.error("池行无法作废(%s): %s —— 该行没有 accepted 决策时"
                          "不可播; 但**重试会产生第二行**, 值得人工看一眼",
                          reason, pool_key)
                return False
            return True
        except Exception:                       # noqa: BLE001
            log.exception("写墓碑异常(%s)", reason)
            return False

    def _activate(self, spec: Any) -> bool:
        """让**当前内存池**立刻看见这道已提交的题(§一-1)。

        ## 为什么不是直接 `self.pool._items.append(...)`

        那会绕过 `_validate_pool_spec` —— 池内"每一道都过得了准入门"这条
        不变量会当场破掉, 而且**只在**这一条路径上破(重启 load 之后又
        好了)。那种 bug 极难复现: 它只在"本场刚审进来的那道题"上出现。

        所以走池的正式 API。池若没有这个 API(老版本 / 测试替身), 就
        **什么都不做**并如实记账 —— 不能假装挂上了, 也不能因此让
        accepted 失败(盘上的一切都成立, 只是本场看不见)。

        返回是否真的挂进了内存池。
        """
        fn = getattr(self.pool, "activate_committed", None)
        if fn is None:
            # 池替身(离线预热)没有内存池的概念 —— 它每次 `_eligible()`
            # 都重读文件, 所以"本场可见"对它是自动成立的。
            log.debug("池没有 activate_committed(离线替身?), 跳过激活")
            return False
        try:
            ok = bool(fn(spec))
            if not ok:
                log.warning("activate_committed 未生效: 该题本场不可播, "
                            "但盘上已提交(下次启动可见)")
            return ok
        except Exception:                       # noqa: BLE001
            log.exception("activate_committed 抛异常(该题本场不生效)")
            return False

    def _attr_record(self, spec: Any) -> dict:
        """署名的**唯一**构造处。

        抽出来是为了让测试能对着**同一份**记录断言(而不是自己再拼一份
        近似的 dict, 那种测试验的是测试自己)。
        """
        return {
            "external_id": spec.external_id,
            "source": spec.external_source,
            "source_url": spec.source_url,
            "license": spec.license,
            "answer_license": spec.answer_license,
            **(spec.attribution or {}),
        }

    def _pool_path(self) -> str:
        return str(getattr(self.pool, "pool_path", "") or "")

    def _attr_path(self) -> str:
        return str(getattr(self.cfg, "attributions_path", "")
                   or os.path.join("data", "ATTRIBUTIONS.jsonl"))

    # ------------------------------------------------------------------
    def status(self) -> dict:
        """给启动 banner 用的只读快照。**不含题底。**"""
        stock, playable = self._stock()
        decided = self.ledger.stats(CURATED_POLICY_VERSION)
        return {
            "candidates": len(self.candidates),
            "decided": sum(decided.values()),
            "accepted": decided.get(ACCEPTED, 0),
            "rejected": decided.get(REJECTED, 0),
            "technical_defer": decided.get(TECHNICAL_DEFER, 0),
            "interrupted": decided.get(INTERRUPTED, 0),
            "stock": stock,
            "playable": playable,
            "target": self._target,
            "min": self._min,
            #: refill cycle 是否在进行 —— 这是**运维最该看到的一个数**:
            #: 它区分了"库存刚好在 min 以上所以没动"与"正在往 target 补"。
            #: 少了它, "补池怎么没动静"只能靠翻日志。
            "refilling": bool(self._refilling),
            "tried": len(self._tried),
            "enabled": self._enabled,
            "busy": self._busy,
        }


# ======================================================================
# 小工具
# ======================================================================
def _spec_key(spec: Any) -> str:
    from story.pool import spec_key
    return spec_key(spec)


def _append_jsonl(path: str, rec: dict) -> bool:
    """追加一行(flush + fsync)。与 compile_curated 同一实现, 理由见那里。"""
    import json
    if not path:
        return False
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return True
    except OSError as e:
        log.error("写盘失败 %s: %s", path, e)
        return False


# ======================================================================
# 装配
# ======================================================================
def load_candidates(corpus_path: str) -> list:
    """读 curated 语料。不存在 -> 空列表(**不是**异常)。

    "没有语料"是一种正常状态(还没跑 importer), 不该让直播启动失败。
    """
    from tools.curated_common import RawCuratedPuzzle, read_jsonl
    rows = read_jsonl(corpus_path)
    return [RawCuratedPuzzle.from_dict(d) for d in rows]


def build_lazy_curator(cfg: Any, pool: Any, writer: Any,
                       pressure: Callable[[], dict],
                       corpus_path: Optional[str] = None,
                       ledger_path: Optional[str] = None,
                       clock: Callable = time.monotonic,
                       generation_probe: Optional[Callable[[], dict]] = None
                       ) -> Optional[LazyCurator]:
    """按配置装配。**任何一步失败都返回 None**(而不是让直播起不来)。"""
    if not bool(getattr(cfg, "curated_background_enabled", True)):
        return None
    try:
        from tools.curated_common import EXTERNAL_ROOT
        corpus = corpus_path or str(
            getattr(cfg, "curated_corpus_path", "")
            or os.path.join(EXTERNAL_ROOT, "normalized", "curated_raw.jsonl"))
        recs = load_candidates(corpus)
        if not recs:
            log.info("curated 语料为空(%s)—— Lazy Curator 不启动", corpus)
            return None
        led = DecisionLedger(
            ledger_path or str(getattr(cfg, "curated_decisions_path", "")
                               or os.path.join("data",
                                               "curated_decisions.jsonl")))
        comp = CuratedCompiler(writer)
        return LazyCurator(cfg, pool, comp, recs, led, pressure, clock=clock,
                           generation_probe=generation_probe)
    except Exception:                           # noqa: BLE001
        log.exception("装配 Lazy Curator 失败 —— 直播照常, 只是不补 curated")
        return None
