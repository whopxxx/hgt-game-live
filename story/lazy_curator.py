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
import time
from typing import Any, Callable, Optional

from tools.curated_compiler import CURATED_POLICY_VERSION, CuratedCompiler
from tools.curated_ledger import (
    ACCEPTED, INTERRUPTED, REJECTED, TECHNICAL_DEFER, DecisionLedger,
)

log = logging.getLogger("hgt.lazycurator")


# ======================================================================
# 候选选择
# ======================================================================
def source_priority(rec: Any) -> tuple:
    """**先审谁**(不是"谁一定能进池")。

    第一版刻意简单、确定性 —— 复杂的排名在数据量只有几百条时收益为负,
    而且会让"为什么这道先审"变得无法解释。

    顺序按**来源可信度**:
        0  TurtleBench            出题时就按海龟汤组织的, 命中率最高
        1  SE situation/story/mystery   SE 上最接近叙事题的标签
        2  SE 其它(含 lateral-thinking) 已知混着数学/物理/字谜, 最后审
        9  未知来源

    ⚠️ 注意这**只是排序**, 不是准入。第 2 档里也可能有真海龟汤,
    第 0 档里也可能有垃圾 —— 最终裁定权在 curated-v2 那三门。
    """
    src = str(getattr(rec, "source", "") or "").lower()
    tags = {str(t).lower() for t in (getattr(rec, "tags", None) or [])}
    if "turtlebench" in src:
        return (0, str(getattr(rec, "external_id", "")))
    if tags & {"situation", "story", "mystery"}:
        return (1, str(getattr(rec, "external_id", "")))
    if "stackexchange" in src or "puzzling" in src:
        return (2, str(getattr(rec, "external_id", "")))
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


# ======================================================================
# LazyCurator
# ======================================================================
class LazyCurator:
    """后台库存生产者。**绝不抛、绝不阻塞直播。**

    它不拥有线程 —— 与 `PoolPrefetcher` 一样, 由调用方在自己的节奏里
    调 `step()`。这样"什么时候可以干活"的判断权留在装配层(它能同时
    看到 engine 和 pool), 而本类只管"取一条、审一条、记账"。
    """

    def __init__(self, cfg: Any, pool: Any, compiler: CuratedCompiler,
                 candidates: list, ledger: DecisionLedger,
                 pressure: Callable[[], dict],
                 clock: Callable = time.monotonic,
                 budget_seconds: Optional[float] = None):
        self.cfg = cfg
        self.pool = pool
        self.compiler = compiler
        self.candidates = list(candidates)
        self.ledger = ledger
        self._pressure = pressure
        self._clock = clock
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

        self._busy = False
        self._last: dict = {}

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
        """
        try:
            stock = int(self.pool.stock_count(limit=self._max + 1))
        except Exception:                       # noqa: BLE001
            log.exception("读 stock_count 失败, 按 0 处理")
            stock = 0
        try:
            playable = int(self.pool.playable_count(limit=self._playable_min + 1))
        except Exception:                       # noqa: BLE001
            log.exception("读 playable_count 失败, 按 0 处理")
            playable = 0
        return stock, playable

    def needs_work(self) -> bool:
        """现在**该不该**补库存(只读, 不干活)。"""
        if not self._enabled:
            return False
        stock, playable = self._stock()
        if stock >= self._max:
            return False
        # 迟滞: 只有**低于 min**、或"下一题没得播"时才启动。
        #
        # 为什么不是 `stock < target`: 那会让库存一跌破 10 就立刻开审,
        # 于是"刚播掉一道"就触发一次 LLM —— 而那时正是观众在提问的时候。
        # 迟滞让它等到真的快空了再动, 一次补一批。
        if stock < self._min:
            return True
        if playable < self._playable_min:
            return True
        return False

    def should_start(self) -> tuple:
        """能不能**开始**一条。返回 `(ok, why)`。

        直播优先 —— 下列任何一条成立都不启动新的 LLM:
            真人 pending / inflight
            AI 玩家 in_flight
            hint 在途
            reveal 在途
            下一题马上开始
            已有 worker 在途
        """
        if self._busy:
            return False, "已有 worker 在途"
        if not self._enabled:
            return False, "已关闭"
        p = self._safe_pressure()
        if p.get("stopped"):
            return False, "引擎已停"
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
        if not self.needs_work():
            return False, "库存充足"
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
        """
        out = {"processed": 0, "accepted": 0, "rejected": 0,
               "technical_defer": 0, "interrupted": 0, "skipped": 0}
        tried: set = set()
        for _ in range(max(0, int(max_candidates))):
            ok, why = self.should_start()
            if not ok:
                out["skipped"] += 1
                out["stop_reason"] = why
                break
            rec = select_candidate(self.candidates, self.ledger,
                                   CURATED_POLICY_VERSION,
                                   skip_ids=tried)
            if rec is None:
                out["stop_reason"] = "没有未处理的 candidate"
                break
            tried.add(str(getattr(rec, "external_id", "") or ""))
            res = self._curate_one(rec)
            out["processed"] += 1
            key = res.get("decision")
            if key in out:
                out[key] += 1
        return out

    def _curate_one(self, rec: Any) -> dict:
        """审**一条**。**绝不抛** —— 异常一律转成 technical_defer。

        为什么这里要兜住所有异常: 后台任务抛出去会打断调用方的 tick,
        而 tick 是直播的心跳。宁可少一道题, 不能让直播抖一下。
        """
        eid = str(getattr(rec, "external_id", "") or "")
        self._busy = True
        t0 = self._clock()
        try:
            log.info("curated candidate start: %s", eid)
            spec, info = self.compiler.compile_one(
                rec, recent=[], blueprint=None,
                should_continue=self._should_continue)
            elapsed = self._clock() - t0

            decision, stage, reasons = self._classify(spec, info, elapsed)
            style_tags = list(info.get("style_tags") or [])
            self.ledger.record(rec, decision=decision,
                               policy_version=CURATED_POLICY_VERSION,
                               stage=stage, reasons=reasons,
                               style_tags=style_tags)

            if decision == ACCEPTED and spec is not None:
                # ---- 先落盘再算数 ----
                # 写池失败 -> 把 decision 降级成 technical_defer。
                # 否则会留下"账本说 accepted, 池里没有"的半状态:
                # 那道题**永远不会**被重审(accepted 是终态), 而它也
                # 播不出来 —— 一道题就这么无声地没了。
                if not self._commit(spec):
                    self.ledger.record(rec, decision=TECHNICAL_DEFER,
                                       policy_version=CURATED_POLICY_VERSION,
                                       stage="pool_write",
                                       reasons=["pool_write_failed"],
                                       style_tags=style_tags)
                    log.error("curated deferred: %s reason=pool_write_failed",
                              eid)
                    return {"decision": TECHNICAL_DEFER}
                stock, playable = self._stock()
                log.info("curated accepted: %s stock=%d playable=%d",
                         eid, stock, playable)
                return {"decision": ACCEPTED, "stock": stock,
                        "playable": playable}

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
        finally:
            self._busy = False

    # ------------------------------------------------------------------
    def _should_continue(self) -> bool:
        """交给 `compile_one` 的让路回调。False = 停。

        ⚠️ 这里**不看** `needs_work()`: 一旦开始审了, 就把这一条审完
        (除非直播来抢资源)。中途因为"库存刚好够了"而放弃会浪费掉
        已经花掉的 LLM 调用 —— 而且那道题下次还得从头再来。
        库存判断只在**启动前**做(`should_start`)。
        """
        p = self._safe_pressure()
        if p.get("stopped"):
            return False
        if int(p.get("pending") or 0) > 0 or int(p.get("inflight") or 0) > 0:
            return False
        if p.get("ai_player_in_flight"):
            return False
        if p.get("hint_inflight") or p.get("reveal_inflight"):
            return False
        rem = p.get("reveal_remaining_seconds")
        if rem is not None and float(rem) <= self._guard_s():
            return False
        return True

    # ------------------------------------------------------------------
    def _classify(self, spec: Any, info: dict, elapsed: float) -> tuple:
        """把 `compile_one` 的结果翻成 **四态之一**。返回 `(decision, stage, reasons)`。

        ## 为什么这个映射必须写在一处

        `rejected` 是**终态** —— 写错了那道题永远消失, 而且没有任何地方
        会显示"我们丢了几道"。所以判定必须集中、可读、可测, 不能散落
        在 if/else 里。

        映射:
            info["interrupted"]  -> interrupted          (可重试)
            spec 非空            -> accepted             (终态)
            预算是超了            -> technical_defer      (可重试)
            其它没收             -> rejected             (终态)
        """
        reasons = [str(r) for r in (info.get("reject_reasons") or []) if r]
        stage = str(info.get("stage") or "")

        if info.get("interrupted"):
            return INTERRUPTED, str(info.get("interrupt_at") or stage), reasons
        if spec is not None:
            return ACCEPTED, "accepted", reasons
        if elapsed > self._budget:
            # 超预算说明这一条特别慢(网关慢 / 重试多)。把它当成
            # 内容问题是不公平的 —— 换一次网络它可能就过了。
            return TECHNICAL_DEFER, "budget", ["budget_exceeded"]
        # stage 标技术问题的也走 defer
        if stage.endswith("_technical") or stage == "compile_call":
            return TECHNICAL_DEFER, stage, reasons or ["technical"]
        if stage in ("exception", "pool_write"):
            return TECHNICAL_DEFER, stage, reasons or [stage]
        return REJECTED, stage or "unknown", reasons

    # ------------------------------------------------------------------
    def _commit(self, spec: Any) -> bool:
        """入池 + attribution。任一步失败 -> False(调用方降级 defer)。

        顺序: **先入池, 再写 attribution**。
        反过来的话, 池写失败会留下一份"没有对应题目的署名" ——
        那是脏数据。先池后署名的最坏情况是"题在池里但署名没写",
        而那种缺失是**可检测**的(ATTRIBUTIONS 里查不到 external_id),
        也比反过来安全。
        """
        try:
            rec_out = {
                "pool_version": 1,
                "pool_key": _spec_key(spec),
                "added_at": time.time(),
                "added_by": "curated-lazy",
                "spec": spec.to_archive(),
            }
            if not _append_jsonl(self._pool_path(), rec_out):
                return False
            if not _append_jsonl(self._attr_path(), {
                "external_id": spec.external_id,
                "source": spec.external_source,
                "source_url": spec.source_url,
                "license": spec.license,
                "answer_license": spec.answer_license,
                **(spec.attribution or {}),
            }):
                # 署名写失败: 题**已经**在池里了, 不能回滚(归档是追加的)。
                # 记一条明显的 ERROR 让它可以被找出来, 但不降级 decision
                # —— 题目本身是好的, 版权归属在 decision ledger 里仍有
                # external_id + source 可追。
                log.error("attribution 写盘失败(题已入池): %s",
                          getattr(spec, "external_id", ""))
            return True
        except Exception:                       # noqa: BLE001
            log.exception("入池异常")
            return False

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
                       clock: Callable = time.monotonic
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
        return LazyCurator(cfg, pool, comp, recs, led, pressure, clock=clock)
    except Exception:                           # noqa: BLE001
        log.exception("装配 Lazy Curator 失败 —— 直播照常, 只是不补 curated")
        return None
