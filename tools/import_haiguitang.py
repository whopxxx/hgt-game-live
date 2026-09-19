#!/usr/bin/env python
# coding: utf-8
"""H4-A: 导入 `neurostellar/haiguitang` 作为**主** curated 候选源。

## 为什么换源(而不是继续筛 PSE)

H3-D 的实测结论很清楚:

    Lazy Curator 基础设施可工作
    但 PSE 的 source yield 太低

上一轮 30 个 candidate 只收 5 条, 而拒收原因高度集中在
`single_trick` / `no_multi_step_deduction` / `external_knowledge_dependency`
—— 也就是说, PSE 那批语料里**大量是 lateral-thinking / brain teaser**,
本来就不是海龟汤。继续烧 LLM 筛它没有产品价值。

`neurostellar/haiguitang` 是**按海龟汤生成的**数据(dataset card 里
`system` 字段就是出题指令), 先验分布合理得多。所以换源, 但
**不取消 curated-v3 审核** —— 它一样要过全部十三条。

## 这个数据集的形状(实测, 不是猜)

    3729 行, 字段只有四个: instruction / input / system / output
    instruction 与 system **全表各只有 1 个值**(就是出题 prompt)
    input  = "关键词：山顶，敲门，死者"
    output = "故事情节：……\\n真相：……"

真正有用的是 `output`。需要从它拆出 (谜面, 谜底)。实测 3729/3729
都能被同一条正则拆开 —— 但**不能因此假设它永远成立**, 所以
sanitation 仍然把拆不开的算 source-invalid。

## 三条硬要求

### 一、deterministic sanitation 在任何 LLM 调用**之前**

任务书 §四。清洗是**免费**的, 而 Reviewer 是**要花钱**的。顺序反了
就是把钱花在明显残缺的条目上。

### 二、不要在 LLM 前做复杂去重(§九)

只做 **exact normalized hash**。embedding / 语义 / 跨源聚类这一轮
明确不做 —— 用户体验当前不受影响, 而"近重复预处理"会成为大面积
误拒条件(现有 `dup_reason=near_duplicate` 会让 candidate 直接被
`select_candidate` 跳过)。

### 三、不许伪造许可证(§七)

这个源**没有** license 字段(实测 cardData.license == None)。任务书
明确: 该源已由项目负责人批准使用, 所以给 provenance 一个**独立合法
状态** `usage_basis="project_approved_public_dataset"`, 而**不是**
硬填一个 CC BY-SA / MIT / Apache。

## 用法

    .venv/Scripts/python.exe tools/import_haiguitang.py
    .venv/Scripts/python.exe tools/import_haiguitang.py --from-file <本地json>

产物(全部在 `data_external/` 下, **不进版本库**):

    data_external/haiguitang/raw/turtle.json            原始下载
    data_external/haiguitang/normalized/haiguitang.jsonl
    data_external/haiguitang/normalized/haiguitang.meta.json
    data_external/haiguitang/rejected_source.jsonl      确定性清洗拦下的
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.curated_common import (  # noqa: E402
    EXTERNAL_ROOT, RawCuratedPuzzle, ensure_dir, http_get,
    normalize_for_dedup, resolve_proxy, safety_screen, stable_hash,
    strip_html, write_jsonl_deterministic, write_meta,
)

log = logging.getLogger("hgt.haiguitang")

REPO = "neurostellar/haiguitang"
DATA_FILE = "turtle.json"
SOURCE_URL = f"https://huggingface.co/datasets/{REPO}"

#: **项目批准的数据源**(§七)。这不是许可证 —— 它是一个独立状态:
#: "项目负责人明确批准使用/参考这个来源"。
#:
#: ⚠️ 刻意**不**填 CC BY-SA / MIT / Apache。数据集没有声明 license
#: (API 实测 cardData.license == None), 硬填一个不存在的许可证是伪造
#: 版权信息 —— 那比"许可未知"严重得多。
USAGE_BASIS = "project_approved_public_dataset"

#: 官方优先, 镜像兜底(与 import_turtlebench 同一套理由)。
ENDPOINTS = (
    "https://huggingface.co",
    "https://hf-mirror.com",
)

#: 长度规则(§五)。**不是**硬删除原始数据 —— 只是不进 live candidate。
#:
#: 参考: U4 已经支持滚动文本, 所以不再用原来那个 80 字汤面硬门。
SURFACE_MAX = 220
BOTTOM_MAX = 300

#: `output` 的拆分正则。实测 3729/3729 命中。
#:
#: ⚠️ 用 `.*?` + `re.S`: "真相" 前面可能是任意长度的情节, 允许换行。
#: 而**不加** `$` 锚 —— 有些条目真相后面还跟一句补充, 那是内容不是垃圾。
_SPLIT_RE = re.compile(r"故事情节\s*[:：]\s*(.*?)\s*真相\s*[:：]\s*(.*)",
                       re.S)

#: 明显是**生成 prompt 又被续写进 output** 的痕迹(§四)。
#: 命中即以 `source_invalid` 跳过 —— 那不是一道题。
_GEN_LEAK_RE = re.compile(
    r"(请根据|生成一个|关键词\s*[:：]|希望你|以下是.*海龟汤.*生成)")


def _download(path_in_repo: str, timeout: float = 180.0,
              proxy: str = "") -> tuple:
    """按 ENDPOINTS 顺序试下载。返回 `(bytes, base_url)`。"""
    last: Exception | None = None
    for base in ENDPOINTS:
        url = f"{base}/datasets/{REPO}/resolve/main/{path_in_repo}"
        try:
            log.info("尝试下载: %s", url)
            b = http_get(url, proxy=proxy, timeout=timeout, retries=3)
            log.info("下载成功(%d 字节): %s", len(b), base)
            return b, base
        except RuntimeError as e:
            log.warning("下载失败(%s): %s", base, str(e)[:140])
            last = e
    raise RuntimeError(f"所有 endpoint 都下载失败: {last}")


def split_output(output: str) -> tuple:
    """从 `output` 拆出 `(surface, bottom)`。拆不出返回 `("", "")`。

    ## 为什么不用"按行切"

    实测 output 里有换行, 而"故事情节:" / "真相:" 两个标记可能跨行。
    按行切会在那些条目上静默产出半截题。正则 `re.S` 才稳。

    ## 为什么允许标记后面还有内容

    有些条目真相后面还补一句说明。那是**作者的补充**, 属于内容;
    砍掉它会让谜底不完整。所以只取到结尾, 不做二次截断。
    """
    s = strip_html(str(output or ""))
    if not s:
        return "", ""
    m = _SPLIT_RE.search(s)
    if not m:
        return "", ""
    return m.group(1).strip(), m.group(2).strip()


def sanitize(surface: str, bottom: str) -> str:
    """**确定性清洗**(§四)。返回拒收原因, "" = 通过。

    零 LLM 调用。这些判据全是免费的、可复现的, 所以必须在任何
    花钱的调用**之前**跑 —— 顺序反了就是把审核预算花在明显残缺的
    条目上。

    覆盖(任务书 §四逐条):
        缺故事情节 / 缺真相 / output 截断 / 生成 prompt 被续写进 output
        只有题面没有答案 / 只有答案没有题面 / 明显解析失败 / 空白
        超长异常
    """
    s = str(surface or "").strip()
    b = str(bottom or "").strip()
    if not s and not b:
        return "empty_both"
    if not s:
        return "missing_surface"
    if not b:
        return "missing_bottom"
    # 生成 prompt 的痕迹 —— 那是模型的输入被误当成输出, 不是一道题。
    if _GEN_LEAK_RE.search(s) or _GEN_LEAK_RE.search(b):
        return "generation_prompt_leak"
    # 截断: 结尾停在一个明显没写完的地方。
    if s.endswith(("，", ",", "、", "：", ":")) or b.endswith(
            ("，", ",", "、", "：", ":")):
        return "truncated"
    # 太短 == 解析失败或本来就没有内容。
    if len(s) < 8:
        return "surface_too_short"
    if len(b) < 8:
        return "bottom_too_short"
    return ""


def length_ok(surface: str, bottom: str) -> bool:
    """长度规则(§五)。超长**不删原始数据**, 只是不进 live candidate。"""
    return len(surface) <= SURFACE_MAX and len(bottom) <= BOTTOM_MAX


def external_id_for(surface: str, bottom: str) -> str:
    """稳定 id: `haiguitang:<hash>`。

    ## 为什么不用 row index(§八)

    dataset 的行序**可能变**(重新上传、重新 shuffle)。用行号当 id 会
    让"同一道题"在两次导入之间换身份 —— 账本里那条 accepted 决策就
    再也对不上了, 于是同一道题被重审一遍, 或者反过来被当成新题。

    所以哈希**规范化后的** surface + bottom(§八明确要求先规范化)。
    与 `content_hash` 是两件事: 那个用统一的 ledger 规则(不规范化),
    这个用于跨导入的稳定身份。
    """
    h = stable_hash("haiguitang",
                    normalize_for_dedup(surface),
                    normalize_for_dedup(bottom), n=12)
    return f"haiguitang:{h}"


def build_records(rows: list) -> tuple:
    """清洗 + 归一。返回 `(records, rejected, stats)`。

    ## 处理顺序是刻意的

        ① 解析 output -> (surface, bottom)
        ② 确定性 sanitation(免费)  -> 不合格 -> rejected_source
        ③ 长度规则(免费)           -> 超长    -> rejected_source(保留 raw)
        ④ 内容安全粗筛(免费)        -> 命中    -> rejected_safety
        ⑤ exact normalized hash 去重(**只做这一种**)
        ⑥ 建 RawCuratedPuzzle

    前四步全部零 LLM。第 ⑤ 步刻意**只做 exact** —— 任务书 §九 明确
    这一轮不做 embedding / 语义 / 跨源聚类去重, 因为现有
    `dup_reason=near_duplicate` 会让 candidate 被直接跳过, 大面积
    误拒比"偶尔重复"糟得多。
    """
    records: list = []
    rejected_source: list = []
    rejected_safety: list = []
    seen: dict = {}
    n_split_fail = 0
    n_sanitize = 0
    n_too_long = 0
    n_exact_dup = 0

    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            n_split_fail += 1
            continue
        surface, bottom = split_output(row.get("output"))
        if not surface and not bottom:
            n_split_fail += 1
            rejected_source.append({"row": i, "reason": "no_split",
                                    "output": str(row.get("output"))[:300]})
            continue
        why = sanitize(surface, bottom)
        if why:
            n_sanitize += 1
            rejected_source.append({"row": i, "reason": why,
                                    "surface": surface[:200],
                                    "bottom": bottom[:200]})
            continue
        if not length_ok(surface, bottom):
            n_too_long += 1
            # ⚠️ 保留 raw —— 超长不是"烂题", 只是现阶段不适合直播。
            rejected_source.append({"row": i, "reason": "too_long",
                                    "surface": surface[:200],
                                    "bottom": bottom[:200]})
            continue

        key = (normalize_for_dedup(surface), normalize_for_dedup(bottom))
        if key in seen:
            n_exact_dup += 1
            continue
        seen[key] = True

        eid = external_id_for(surface, bottom)
        rec = RawCuratedPuzzle(
            external_id=eid,
            source="neurostellar/haiguitang",
            source_url=SOURCE_URL,
            source_kind="dataset",
            title="",
            surface=surface,
            bottom=bottom,
            language="zh",
            original_language="zh",
            translated=False,
            tags=["turtle-soup", "haiguitang"],
            # ---- §七: 不伪造许可证 ----
            # 数据集**没有**声明 license(API 实测 cardData.license==None)。
            # 硬填一个 CC BY-SA / MIT / Apache 是伪造版权信息 —— 比
            # "许可未知"严重得多。这里留空, 由 usage_basis 表达授权依据。
            question_license="",
            answer_license="",
            question_license_inference="",
            answer_license_inference="",
            # 数据集没有逐题作者 —— 如实留空, 不编。
            question_author="",
            answer_author="",
        )
        #: 授权依据(§七)。**独立合法状态** —— 不是许可证的别名。
        #: `license_ok()` / candidate eligibility 会认它(见
        #: `RawCuratedPuzzle.license_ok`)。
        try:
            rec.usage_basis = USAGE_BASIS          # type: ignore[attr-defined]
        except Exception:                          # noqa: BLE001
            pass

        flag = safety_screen(surface + "\n" + bottom)
        if flag:
            rec.safety_flag = flag
            rejected_safety.append(rec)
            continue
        records.append(rec)

    stats = {
        "raw_rows": len(rows),
        "parse_fail": n_split_fail,
        "sanitize_rejected": n_sanitize,
        "too_long": n_too_long,
        "exact_dup": n_exact_dup,
        "rejected_safety": len(rejected_safety),
        "kept": len(records),
        "usage_basis": USAGE_BASIS,
    }
    return records, rejected_source, rejected_safety, stats


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="import_haiguitang",
        description="下载 + 归一 neurostellar/haiguitang(中文海龟汤)")
    ap.add_argument("--from-file", default="",
                    help="跳过下载, 直接用本地 raw JSON(离线可复现)")
    ap.add_argument("--root", default=EXTERNAL_ROOT,
                    help=f"输出根目录, 默认 {EXTERNAL_ROOT}")
    ap.add_argument("--proxy", default=None,
                    help="HTTP 代理(默认取 HGT_PROXY/HTTPS_PROXY, "
                         "再退到 127.0.0.1:7897; 传空串 = 直连)")
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--log-level", default="INFO")
    return ap


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(a.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    proxy = resolve_proxy(a.proxy)
    log.info("代理: %s", proxy or "(直连)")

    base = os.path.join(a.root, "haiguitang")
    raw_p = os.path.join(base, "raw", DATA_FILE)
    norm_p = os.path.join(base, "normalized", "haiguitang.jsonl")
    meta_p = os.path.join(base, "normalized", "haiguitang.meta.json")
    rej_src = os.path.join(base, "rejected_source.jsonl")
    rej_safe = os.path.join(base, "rejected_safety.jsonl")

    # ---- 1. 拿原始数据 ----
    if a.from_file:
        if not os.path.exists(a.from_file):
            log.error("--from-file 不存在: %s", a.from_file)
            return 1
        with open(a.from_file, "rb") as f:
            blob = f.read()
        src = f"file:{a.from_file}"
    else:
        try:
            blob, base_url = _download(DATA_FILE, timeout=a.timeout,
                                       proxy=proxy)
        except RuntimeError as e:
            log.error("%s", e)
            return 1
        src = base_url

    ensure_dir(raw_p)
    with open(raw_p, "wb") as f:
        f.write(blob)

    try:
        rows = json.loads(blob.decode("utf-8"))
    except ValueError as e:
        # ⚠️ **不静默产出空文件**: 下游看到 haiguitang.jsonl 存在会以为
        # 导入成功, 于是"新源接通了"这个结论直接是假的。
        log.error("原始数据不是合法 JSON: %s", e)
        return 1
    if not isinstance(rows, list):
        log.error("原始数据顶层不是数组(实际 %s)", type(rows).__name__)
        return 1
    log.info("原始 %d 行", len(rows))

    # ---- 2. 确定性清洗 + 归一 ----
    records, rejected_source, rejected_safety, stats = build_records(rows)
    if not records:
        log.error("清洗后一条都不剩 —— **不写空文件**(那会让下游以为"
                  "导入成功)。")
        return 1
    write_jsonl_deterministic(norm_p, records)
    write_jsonl_deterministic(rej_src, rejected_source)
    write_jsonl_deterministic(rej_safe, rejected_safety)
    write_meta(meta_p, dataset=REPO, endpoint=src, data_file=DATA_FILE,
               **stats)

    # ---- 3. 报告(数字必须能被任务书的验收直接引用) ----
    print()
    print("=" * 62)
    print("neurostellar/haiguitang 导入")
    print("=" * 62)
    print(f"  原始行数              : {stats['raw_rows']}")
    print(f"  解析失败(拆不出结构)  : {stats['parse_fail']}")
    print(f"  确定性清洗拦下        : {stats['sanitize_rejected']}")
    print(f"  超长(保留 raw)        : {stats['too_long']}")
    print(f"  exact 重复            : {stats['exact_dup']}")
    print(f"  安全粗筛拦下          : {stats['rejected_safety']}")
    print(f"  **入 candidate**      : {stats['kept']}")
    print(f"  授权依据              : {stats['usage_basis']}")
    print()
    print(f"  -> {norm_p}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
