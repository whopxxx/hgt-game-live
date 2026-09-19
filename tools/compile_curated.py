#!/usr/bin/env python
# coding: utf-8
"""H3-C: curated 预热 CLI —— **同一个 LazyCurator 的薄包装**。

## 它不再是什么

H2 时这个脚本是**另一套实现**: 自己读语料、自己 `load_done(curated_pool)`、
自己循环 `compile_one`、自己写池。于是"离线批量"与"直播后台补"是两条
独立的代码路径 —— 两条路径迟早会漂移(改了一边的准入标准, 另一边不知道)。

任务书十五: **不要维护两套 curator**。现在它只做三件事:

    解析参数 -> 装配 LazyCurator -> 按预算调 step()

所有判定(rejected / technical_defer / interrupted 的区分、库存迟滞、
让路)都走直播那条路径用的**同一个** `LazyCurator`。离线预热与直播后台
补货的行为因此**按构造**一致。

## `--target-stock` 与 `--max-candidates` 是两个独立上限

任务书十六指出的老问题: 原先只有一个 `--limit 20`, 语义是

    "一直审到**成功** 20 道"

于是拒绝率高时会调用 100+ 次 —— 你设 20, 花掉 120 次的预算。两个上限
分开之后:

    库存达到 --target-stock  **或者**  本次处理满 --max-candidates
                            (任一先到就停)

## 断点续跑

由**决策账本**提供(H3-A), 不再是"读 curated_pool 反推"。
后者只知道成功的题, 于是被拒的题每轮都被重审 —— 那是本批要消灭的浪费。

## 用法

    # 预热到库存 10 道, 本次最多处理 30 个 candidate
    .venv/Scripts/python.exe tools/compile_curated.py \\
        --target-stock 10 --max-candidates 30

    # 只看语料与账本统计, 不调 LLM
    .venv/Scripts/python.exe tools/compile_curated.py --report-only
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from story.config import Config, from_args as _cfg_from_args  # noqa: E402
from tools.curated_common import (  # noqa: E402
    EXTERNAL_ROOT, ensure_dir, read_jsonl, write_meta,
)
from tools.curated_compiler import CURATED_POLICY_VERSION  # noqa: E402
from tools.curated_ledger import DecisionLedger  # noqa: E402

log = logging.getLogger("hgt.prewarm")

CORPUS = os.path.join(EXTERNAL_ROOT, "normalized", "curated_raw.jsonl")
CURATED_POOL = os.path.join("data", "curated_pool.jsonl")
ATTRIBUTIONS = os.path.join("data", "ATTRIBUTIONS.jsonl")
DECISIONS = os.path.join("data", "curated_decisions.jsonl")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="compile_curated",
        description="预热 curated 库存(与直播后台补货同一个 LazyCurator)")
    ap.add_argument("--corpus", default=CORPUS)
    ap.add_argument("--pool-out", default=CURATED_POOL)
    ap.add_argument("--attributions", default=ATTRIBUTIONS)
    ap.add_argument("--decisions", default=DECISIONS)
    # ---- 两个独立上限(任务书十六) ----
    ap.add_argument("--target-stock", type=int, default=10,
                    help="库存达到这个数就停(默认 10)")
    ap.add_argument("--max-candidates", type=int, default=30,
                    help=("本次最多**处理**几个 candidate(不是成功数)。"
                          "默认 30 —— 防「想审 20 道却烧掉 120 次」。"))
    ap.add_argument("--min-size", type=int, default=None,
                    help="低于这个库存才开始(默认取 --target-stock)")
    ap.add_argument("--max-attempts", type=int, default=2,
                    help="单道最多编译几稿(默认 2)")
    ap.add_argument("--budget-seconds", type=float, default=None,
                    help="单条预算上限(秒), 超了记 technical_defer")
    ap.add_argument("--report-only", action="store_true",
                    help="只打印语料/账本统计, 不调 LLM")
    ap.add_argument("--show-samples", type=int, default=0,
                    help="打印 N 道 accepted 样本(验收人工看用)")
    # ---- §十五/§十六: 确定性分层抽样 + 逐条报告 ----
    ap.add_argument("--sample", type=int, default=0,
                    help=("从待处理 candidate 里做**固定 seed 的确定性"
                          "分层抽样**, 取 N 条(按内容哈希分桶, 覆盖全体"
                          " id 空间, 不是字典序头部)"))
    ap.add_argument("--report-json", default="",
                    help=("把**逐条**结果写成 JSON(§十六: 全部 candidate "
                          "的 external_id/surface/bottom/最终状态/拒因; "
                          "accepted 另给 core_answer/style/compile_checks/"
                          "review_checks)"))
    ap.add_argument("--no-llm", action="store_true",
                    help="不使用 LLM(只能干跑)")
    ap.add_argument("--log-level", default="INFO")
    ap.add_argument("--log-file", default="")
    return ap


def _make_writer(cfg: Config):
    """与 prefill / prefetch **同构**的 writer。"""
    from story.llm import AnthropicMessagesClient, PuzzleWriter
    return PuzzleWriter(client=AnthropicMessagesClient(cfg.llm),
                        runtime_cfg=cfg)


# ======================================================================
# 报告
# ======================================================================
def _print_corpus_report(recs: list, led: DecisionLedger) -> None:
    print()
    print("=" * 62)
    print("Curated 语料 / 账本统计(未调 LLM)")
    print("=" * 62)
    print(f"  语料总数      : {len(recs)}")
    st = led.stats(CURATED_POLICY_VERSION)
    print(f"  账本(policy={CURATED_POLICY_VERSION}):")
    print(f"    accepted        : {st['accepted']}")
    print(f"    rejected        : {st['rejected']}")
    print(f"    technical_defer : {st['technical_defer']}")
    print(f"    interrupted     : {st['interrupted']}")
    print(f"  待处理        : "
          f"{len(recs) - st['accepted'] - st['rejected'] - st['technical_defer'] - st['interrupted']}")
    src: dict = {}
    lang: dict = {}
    for r in recs:
        src[r.source] = src.get(r.source, 0) + 1
        lang[r.original_language or "?"] = lang.get(
            r.original_language or "?", 0) + 1
    print("  来源分布:")
    for k, v in sorted(src.items(), key=lambda kv: -kv[1]):
        print(f"    {k:26s} {v}")
    print("  原语言分布:")
    for k, v in sorted(lang.items(), key=lambda kv: -kv[1]):
        print(f"    {k:6s} {v}")
    print()


def _print_report(rep: dict, led: DecisionLedger) -> None:
    print()
    print("=" * 62)
    print("Curated 预热报告")
    print("=" * 62)
    print(f"  政策版本             : {CURATED_POLICY_VERSION}")
    print(f"  本次 processed       : {rep['processed']}")
    print(f"    accepted           : {rep['accepted']}")
    print(f"    rejected           : {rep['rejected']}")
    print(f"    technical_defer    : {rep['technical_defer']}")
    print(f"    interrupted        : {rep['interrupted']}")
    print(f"  停止原因             : {rep.get('stop_reason') or '-'}")
    print(f"  耗时                 : {rep['elapsed_s']}s")
    print(f"  库存                 : {rep['stock']} 道"
          f"(可播 {rep['playable']})")
    print(f"  LLM 调用             : {rep['llm_calls']}")
    st = led.stats(CURATED_POLICY_VERSION)
    print()
    print(f"  账本累计(policy={CURATED_POLICY_VERSION}):")
    print(f"    accepted {st['accepted']} / rejected {st['rejected']} / "
          f"defer {st['technical_defer']} / interrupted {st['interrupted']}")
    _print_source_quality(led)
    settle = led.settle_stats(CURATED_POLICY_VERSION)
    if settle["by_stage"]:
        print()
        print("  拒收 stage 分布:")
        for k, v in sorted(settle["by_stage"].items(), key=lambda kv: -kv[1]):
            print(f"    {k:26s} {v}")
    if settle["by_reason"]:
        print()
        print("  拒收原因 top:")
        for k, v in list(sorted(settle["by_reason"].items(),
                                key=lambda kv: -kv[1]))[:12]:
            print(f"    {k:44s} {v}")
    print()


def _print_source_quality(led: DecisionLedger) -> None:
    """§十四: 把 source quality 拆成**几个不同的数字**。

    ⚠️ 这一段的重点不是"多打几行", 而是**不再只报 accepted/processed**。
    把网关抖动 / schema 错误 / hint 生成失败 / fair_clue 接线失败算成
    "源质量差"是**统计错误** —— 一次网络抖动会让 yield 看起来掉一半,
    而源本身没变。

    真正的 source yield:

        accepted / (accepted + content_rejected)

    分母**不含** defer / interrupted。
    """
    q = led.source_quality(CURATED_POLICY_VERSION)
    print()
    print("  源质量拆分(§十四 —— 不要把技术失败算成源差):")
    print(f"    processed        : {q['processed']}")
    print(f"    accepted         : {q['accepted']}")
    print(f"    content_rejected : {q['content_rejected']}"
          f"   <- **真**内容不合格")
    print(f"    technical_defer  : {q['technical_defer']}"
          f"   (含 compile_invalid {q['compile_invalid']})")
    print(f"    interrupted      : {q['interrupted']}")
    y = q["source_yield"]
    print(f"    **source yield** : "
          + ("n/a(还没有内容判决)" if y is None else f"{y:.1%}")
          + "  = accepted / (accepted + content_rejected)")
    # ---- H4-D §十二: 信号分布(不是死因) ----
    #
    # ⚠️ 这一段的措辞很重要。信号**不拒题**, 所以这里回答的是
    # "收进来的题有多少偏简单", 而不是"有多少题因为偏简单被拒"。
    # 后者在 v5 里**恒为 0** —— 混着说会让人以为门还卡在那儿。
    sig = led.signal_stats(CURATED_POLICY_VERSION)
    if sig["accepted_with_signal"] or sig["accepted_clean"]:
        print()
        print("  信号分布(§十二 —— 信号**不拒题**, 只用于以后排序):")
        print(f"    accepted 且无任何信号 : {sig['accepted_clean']}"
              f"   (层次/反转都齐)")
        for k, v in sorted(sig["accepted_with_signal"].items(),
                           key=lambda kv: -kv[1]):
            print(f"    accepted 带信号 {k:26s} {v}")
        if sig["rejected_with_signal"]:
            print("    rejected 也带信号(说明它不是死因):")
            for k, v in sorted(sig["rejected_with_signal"].items(),
                               key=lambda kv: -kv[1]):
                print(f"      {k:28s} {v}")


def _print_samples(pool_path: str, n: int) -> None:
    """打印 N 道 accepted 样本, 供人工验收(任务书廿四)。"""
    import json
    rows = []
    for line in read_jsonl(pool_path):
        d = line.get("spec") if isinstance(line, dict) else None
        if isinstance(d, dict):
            rows.append(d)
    if not rows:
        print("  (池里没有 accepted 样本)")
        return
    # 确定性抽样: 不随机, 取等距的 N 道 —— 重跑同一条命令得到同一批。
    step = max(1, len(rows) // max(1, n))
    picked = rows[::step][:n]
    print()
    print("=" * 62)
    print(f"  accepted 样本 {len(picked)}/{len(rows)} 道(等距抽取)")
    print("=" * 62)
    for i, d in enumerate(picked, 1):
        print(f"\n--- [{i}] {d.get('external_id')} ---")
        print(f"  来源      : {d.get('external_source')}")
        print(f"  原标题    : {d.get('title')}")
        print(f"  内容风格  : {d.get('content_style')}")
        print(f"  结构标签  : {d.get('style_tags')}")
        print(f"  许可      : {d.get('license')} / {d.get('answer_license')}")
        puz = str(d.get("puzzle") or "")
        print(f"  谜面      : {puz[:200]}")
        print(f"  core      : {str(d.get('core_answer') or '')[:120]}")
        print(f"  谜底      : {str(d.get('answer') or '')[:200]}")
    print()


# ======================================================================
def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(a.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from story.lazy_curator import build_lazy_curator, load_candidates

    recs = load_candidates(a.corpus)
    if not recs:
        log.error("语料为空或不存在: %s —— 先跑 build_curated_corpus.py。"
                  "**不产出空池。**", a.corpus)
        return 1

    led = DecisionLedger(a.decisions)
    if a.report_only:
        _print_corpus_report(recs, led)
        if a.show_samples:
            _print_samples(a.pool_out, a.show_samples)
        return 0

    cfg: Config = _cfg_from_args(["--sim", "prewarm", "--no-window",
                                  "--max-puzzles", "0"])
    if a.no_llm or not getattr(cfg, "llm", None):
        log.error("预热需要一个可用的 LLM 配置 —— --no-llm 下无法编译。")
        return 1

    # ---- 把 CLI 的参数**灌进 cfg**, 让 LazyCurator 读同一套配置 ----
    # 这样离线预热与直播后台用的是**同一个**决策逻辑(任务书十五)。
    cfg.curated_target_size = max(1, int(a.target_stock))
    cfg.curated_min_size = (int(a.min_size) if a.min_size is not None
                            else cfg.curated_target_size)
    cfg.curated_max_size = max(cfg.curated_target_size,
                               cfg.curated_max_size)
    cfg.curated_decisions_path = a.decisions
    cfg.attributions_path = a.attributions

    writer = _make_writer(cfg)
    # 给 client 套计数器 —— 报告里的"LLM 调用"必须是真的, 不能永远 0。
    counter = _CountingClient(writer.client)
    writer.client = counter

    # 用**池替身**作为落点: 预热是离线的, 不需要(也不该)去碰直播的
    # used 账本。LazyCurator 只需要 pool_path / stock_count / playable_count。
    pool = _FakePoolForPrewarm(a.pool_out)

    if a.budget_seconds is not None:
        cfg.curated_budget_seconds = float(a.budget_seconds)
    # ---- 预热是**离线批处理**, 没有直播可以让路 ----
    #
    # 探针必须显式声明"我在跑离线任务", 而不是返回一个空 dict。
    #
    # ⚠️ 这个坑是实测踩到的: H3-D 给 `should_start` 加了 phase 白名单
    # 之后, 空探针(没有 `phase` 键)会被判成"不在允许的 phase" →
    # **整个预热一条都不审**, 报告里只留一句
    # `stop_reason: phase=None 不允许后台审题`。而 `--show-samples`
    # 照样打印(它读的是池文件里**上一轮**的旧样本), 所以表面上像是
    # "跑完了", 实际上一次 LLM 都没调。
    #
    # 语义上这是对的: `phase` 是**直播场景**的让路判据, 离线预热里
    # 根本没有直播。所以这里用一个明确的"离线空闲"探针, 而不是让
    # 白名单去猜。
    offline_free = {"phase": "qa", "pending": 0, "inflight": 0,
                    "hint_inflight": False, "reveal_inflight": False,
                    "ai_player_in_flight": False, "riddle_inflight": False,
                    "stopped": False, "reveal_remaining_seconds": None}
    lc = build_lazy_curator(cfg, pool, writer, lambda: dict(offline_free),
                            corpus_path=a.corpus, ledger_path=a.decisions)
    if lc is None:
        log.error("无法装配 LazyCurator(语料为空或已关闭)。")
        return 1
    # `--max-attempts` 走 compiler 层(CLI 只管"审几稿", 不复制编译逻辑)
    _wrap_attempts(lc.compiler, a.max_attempts)
    #: 供报告读取(见 `_count_llm_calls`)
    lc._llm_counter = counter

    t0 = time.monotonic()
    calls0 = _count_llm_calls(lc)
    # ---- §十五: 确定性分层抽样(替换候选集, **不再是字典序头部**) ----
    if a.sample and a.sample > 0:
        from story.lazy_curator import stratified_sample
        before = len(lc.candidates)
        lc.candidates = stratified_sample(lc.candidates, int(a.sample))
        print(f"\n  [§十五] 分层抽样: {before} -> {len(lc.candidates)} 条"
              f"(固定 seed / 按内容哈希分桶, 可复现)")
    rep = lc.step(max_candidates=max(0, int(a.max_candidates)))
    elapsed = time.monotonic() - t0

    # ⚠️ **必须重新读账本**。
    #
    # `led` 是在跑之前打开的, 它持有的是那一刻的索引。`lc.step()` 往
    # **同一个文件**追加决策, 但写的是 LazyCurator 自己持有的那个
    # `DecisionLedger` 实例 —— 两个实例各有一份内存索引, 互不可见。
    # 所以直接 `led.stats()` 会报出**跑之前**的计数(实测: 全 0),
    # 而报告上看起来像"一条都没记下来"。
    #
    # 重新读一次盘即可 —— 账本是 append-only 的, 盘上就是权威。
    led.reload()

    stock, playable = lc._stock()
    out = {
        "corpus": len(recs),
        "processed": rep.get("processed", 0),
        "accepted": rep.get("accepted", 0),
        "rejected": rep.get("rejected", 0),
        "technical_defer": rep.get("technical_defer", 0),
        "interrupted": rep.get("interrupted", 0),
        "stop_reason": rep.get("stop_reason", ""),
        "elapsed_s": round(elapsed, 1),
        "stock": stock,
        "playable": playable,
        "llm_calls": _count_llm_calls(lc, calls0),
        "pool_out": a.pool_out,
        "policy_version": CURATED_POLICY_VERSION,
    }
    write_meta(os.path.join(os.path.dirname(a.corpus) or ".",
                            "compile_report.json"), **out)
    _print_report(out, led)
    # ---- §十六: 逐条报告(全部 candidate 的最终状态 + 拒因) ----
    if a.report_json:
        _write_item_report(a.report_json, getattr(lc, "candidates", []),
                           led, out)
    if a.show_samples:
        _print_samples(a.pool_out, a.show_samples)
    return 0


def _write_item_report(path: str, recs: list, led: DecisionLedger,
                       out: dict) -> None:
    """§十六: 把**逐条**结果写成 JSON。

    每条给: external_id / surface / bottom / 最终状态 / 内容 reject reason。
    accepted 的**再**给 core_answer / style / reasoning_shape /
    compile_checks / review_checks。

    ⚠️ `surface` / `bottom` 是 canonical 原文, 必须**全文**给 —— 只给
    标题的话人工无法判断"门有没有误杀"(Reject Audit 的教训)。
    """
    import json
    items = []
    for r in recs:
        eid = str(getattr(r, "external_id", "") or "")
        d = led.last(r, CURATED_POLICY_VERSION) or {}
        dec = str(d.get("decision") or "unprocessed")
        row = {
            "external_id": eid,
            "source": str(getattr(r, "source", "") or ""),
            "surface": getattr(r, "surface", "") or "",
            "bottom": getattr(r, "bottom", "") or "",
            "decision": dec,
            "stage": str(d.get("stage") or ""),
            "reasons": list(d.get("reasons") or []),
            # §十四: 这一条落在哪个桶里(报告要能按它分组)
            "bucket": _bucket_of(d),
        }
        if dec == "accepted":
            spec = _pool_spec_by_id(out.get("pool_out", ""), eid)
            if spec:
                row.update({
                    "core_answer": spec.get("core_answer"),
                    "answer": spec.get("answer"),
                    "style_tags": spec.get("style_tags"),
                    "content_style": spec.get("content_style"),
                    "reasoning_shape": _reasoning_shape(spec),
                })
        checks = d.get("checks")
        if isinstance(checks, dict) and checks:
            row["compile_checks"] = checks.get("compile") or {}
            row["review_checks"] = checks.get("review") or {}
            # ---- H4-D §十二: 信号单独给一格 ----
            #
            # 它**不是**拒绝理由 —— 只是一道题"偏简单"的记录。审批端要看
            # 的是"被 accepted 的题里有多少是单点脑筋急转弯", 所以它必须
            # 与 reject reason 分开呈现, 否则会被误读成死因。
            sig = checks.get("signals")
            if isinstance(sig, dict) and sig:
                row["signals"] = sig
        items.append(row)
    payload = dict(out)
    payload["items"] = items
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"  -> 逐条报告 {len(items)} 条: {path}")


def _bucket_of(d: dict) -> str:
    """把一条决策归入 §十四 的桶(报告口径)。"""
    dec = str(d.get("decision") or "")
    st = str(d.get("stage") or "")
    if dec == "accepted":
        return "accepted"
    if dec == "rejected":
        return "content_rejected"
    if dec == "interrupted":
        return "interrupted"
    if dec == "technical_defer":
        from tools.curated_ledger import COMPILE_INVALID_STAGES
        return ("compile_invalid" if st in COMPILE_INVALID_STAGES
                else "technical_defer")
    return "unprocessed"


def _pool_spec_by_id(pool_path: str, eid: str) -> dict:
    """从池文件里按 external_id 取 spec(accepted 的补充信息用)。"""
    if not pool_path or not os.path.exists(pool_path):
        return {}
    for row in read_jsonl(pool_path):
        spec = row.get("spec") if isinstance(row, dict) else None
        if isinstance(spec, dict) and str(spec.get("external_id")) == eid:
            return spec
    return {}


def _reasoning_shape(spec: dict) -> dict:
    """§十六 的 `reasoning_shape`: atoms 的 role 分布 + facts 计数。"""
    atoms = spec.get("solve_atoms") or []
    roles: dict = {}
    for a in atoms:
        if isinstance(a, dict):
            k = str(a.get("role") or "?")
            roles[k] = roles.get(k, 0) + 1
    return {"roles": roles,
            "n_facts": len(spec.get("facts") or []),
            "n_beats": len(spec.get("discovery_beats") or []),
            "completion_fact_ids": spec.get("completion_fact_ids") or []}


# ======================================================================
# 小工具
# ======================================================================
class _FakePoolForPrewarm:
    """预热用的池**替身**: 只提供 LazyCurator 需要的接口。

    为什么不直接用 `PuzzlePool.open(cfg)`: 那会把池的**账本**一起打开,
    而预热是离线的(没有直播在跑), 让离线脚本去碰直播的 used 账本是
    不必要的风险。这里只需要 pool_path / stock_count / playable_count。

    ## H3-D §八: 库存必须按 **live 的 policy eligibility** 数

    早先的实现是 `len(self._rows())` —— 池文件里有多少行就算多少。
    policy bump 之后这**立刻是错的**:

        文件里有 10 条 curated-v2 + 2 条 curated-v3, 当前 policy=v3
        live 真正能播的只有 2 条
        而 CLI 报 stock=10 -> 认为库存充足 -> 一道都不预热

    结果是"预热跑完了, 直播一看库存还是空的"。所以这里复用**同一个**
    准入门(`PuzzlePool._validate_pool_spec`)—— 它正是 live 侧决定
    "能不能播"的那一扇。只读, 不碰 used 账本。
    """

    def __init__(self, path):
        self.pool_path = path
        self.used_path = path + ".used"
        self._path = path

    def _eligible(self) -> list:
        """过得了 live 准入门的 spec。**这是唯一的计数口径。**"""
        from story.pool import PuzzlePool
        rows = read_jsonl(self._path) if os.path.exists(self._path) else []
        voided = PuzzlePool._voided_keys(rows)
        out = []
        for i, row in enumerate(rows):
            spec = PuzzlePool._spec_from_record(row, voided, i)
            if spec is None:
                continue
            try:
                ok, _why = PuzzlePool._validate_pool_spec(spec)
            except Exception:                   # noqa: BLE001
                continue
            if ok:
                out.append(spec)
        return out

    def stock_count(self, limit=None):
        n = len(self._eligible())
        return n if limit is None else min(n, limit)

    def playable_count(self, *a, **k):
        # 预热进程里没有 recent 窗口, 所以"可播"== "过准入门"。
        # 这正是直播侧 `stock_count` 的语义, 而不是 `playable_count`
        # 那条更严的(它还要过 cross_puzzle_gate)。预热刻意用宽的
        # 那个: 窗口是**运行时**的, 离线不该假装知道。
        return len(self._eligible())


def _wrap_attempts(compiler, max_attempts: int) -> None:
    """把 CLI 的 `--max-attempts` 灌进 compiler(不改其签名)。"""
    orig = compiler.compile_one

    def patched(rec, **kw):
        kw.setdefault("max_attempts", max_attempts)
        return orig(rec, **kw)

    compiler.compile_one = patched


def _count_llm_calls(lc, baseline: int = 0) -> int:
    """统计 LLM 调用次数。

    ⚠️ 不能读 `client.calls` —— 那是**测试假件**才有的属性, 真实客户端
    没有, 于是报告会永远打印 0(而那看起来像"一次都没调", 与实际
    相反)。这里改成给 client 包一层计数器, 是唯一可靠的口径。
    """
    n = getattr(getattr(lc, "_llm_counter", None), "n", None)
    if n is None:
        return baseline
    return max(0, int(n) - int(baseline))


class _CountingClient:
    """给真实 client 套一层计数。**只加计数, 不改行为。**"""

    def __init__(self, inner):
        self._inner = inner
        self.n = 0

    def messages(self, *a, **kw):
        self.n += 1
        return self._inner.messages(*a, **kw)

    def __getattr__(self, name):
        return getattr(self._inner, name)


if __name__ == "__main__":
    sys.exit(main())
