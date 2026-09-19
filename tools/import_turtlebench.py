#!/usr/bin/env python
# coding: utf-8
"""H1-A: 下载并归一 TurtleBench1.5k(中文海龟汤)。

## 这个数据集的坑(必须先说清楚)

HuggingFace 页面写着 "Chinese: 1.53k rows"。**实际不是 1.53k 道题。**

    1532 行  ->  只有 32 个不同的 (surface, bottom)
    32 个不同的 title

也就是说每个故事被**复制了几十遍**(实测 title "伪装" 出现 100 次,
"井底之蛙" 94 次 …)。每一行是一个不同的 (user_guess, label) 评测样本,
**不是**一道新题。

所以导入必须按 `(surface, bottom)` 去重。任务书原话:
"最终大约只会得到 32 个独立故事，不要把 1.53k 行误算成 1.53k 道题。"

拿 1532 去估算题库规模, 会让我们以为"外题库很大、够播几百场", 而
真相是 32 道 —— 那是运营决策级的方向性错误。

## 网络

`huggingface.co` 在本机**不可达**(实测超时), 但 `hf-mirror.com` 可用
且内容一致(同一个 dataset sha)。所以本脚本:

    默认先试 HF 官方, 失败/超时则回退 hf-mirror
    两者都失败 -> 明确报错退出, **不静默产出空文件**

"静默产出空文件"是最坏的结果: 下游看到 curated_raw.jsonl 存在, 以为
导入成功, 实际 0 条 —— 于是"外题库导完了"这个结论直接是假的。

## 用法

    .venv/Scripts/python.exe tools/import_turtlebench.py
    .venv/Scripts/python.exe tools/import_turtlebench.py --from-file <本地jsonl>

产物(全部在 data_external/ 下, **不进版本库**):

    data_external/turtlebench/raw/zh_data.jsonl        原始下载
    data_external/turtlebench/normalized/turtlebench.jsonl
    data_external/turtlebench/normalized/turtlebench.meta.json
    data_external/turtlebench/rejected_safety.jsonl    命中安全筛查的
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.curated_common import (  # noqa: E402
    EXTERNAL_ROOT, RawCuratedPuzzle, ensure_dir, http_get,
    resolve_proxy, safety_screen, stable_hash, strip_html,
    write_jsonl_deterministic, write_meta,
)

log = logging.getLogger("hgt.turtlebench")

REPO = "Duguce/TurtleBench1.5k"
DATA_FILE = "chinese/zh_data-00000-of-00001.jsonl"
#: 许可证**从 API 读**(实测 cardData.license == "apache-2.0"), 这里只是
#: 兜底常量。**不**因为页面写着 Apache 就无条件写死 —— 万一以后变了,
#: 读 API 才拿得到真值。
LICENSE_FALLBACK = "Apache-2.0"

#: 官方优先, 镜像兜底。
#:
#: ⚠️ 为什么**官方在前**: 走代理时官方是通的(实测 200), 而镜像是个
#: 第三方转发 —— 能用不等于该优先用。同一条数据从官方拿, 出问题时
#: "是不是镜像改过"这个变量就不存在了。镜像只在官方真的抓不到时兜底。
#:
#: 两者的 dataset sha 实测一致, 所以混用不会造成版本漂移; 但实际用了
#: 哪个 endpoint 会记进 meta, 便于日后对账。
ENDPOINTS = (
    "https://huggingface.co",
    "https://hf-mirror.com",
)


def _download(path_in_repo: str, timeout: float = 90.0,
              proxy: str = "") -> tuple:
    """按 ENDPOINTS 顺序试下载。返回 `(bytes, base_url)`。

    为什么要有回退而不是只写死一个: 本机实测 HF 官方**直连**超时、
    走代理可用; 镜像直连可用。写死任何一个都会在某条网络路径上失败。
    两个都试, 并**把实际用的那个记进 meta** —— 以后排查
    "为什么这批数据和上次不一样" 时, 这个字段是唯一线索。
    """
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


def _license(proxy: str = "") -> tuple:
    """从 dataset API 读许可证。返回 `(license, source_note)`。"""
    for base in ENDPOINTS:
        try:
            d = json.loads(http_get(f"{base}/api/datasets/{REPO}",
                                    proxy=proxy, timeout=45,
                                    retries=2).decode("utf-8"))
            lic = (d.get("cardData") or {}).get("license")
            if lic:
                return str(lic), f"api:{base}"
        except Exception as e:                      # noqa: BLE001
            log.warning("读许可证失败(%s): %s", base, str(e)[:100])
    return LICENSE_FALLBACK, "fallback_constant"


def _norm_license(raw: str) -> str:
    """`apache-2.0` -> `Apache-2.0`(与 KNOWN_LICENSES 对齐)。"""
    m = {"apache-2.0": "Apache-2.0", "cc0-1.0": "CC0-1.0"}
    return m.get(str(raw or "").strip().lower(), str(raw or "").strip())


def build_records(rows: list, license_name: str, license_src: str
                  ) -> tuple:
    """去重 + 归一。返回 `(records, rejected, stats)`。

    ## 去重顺序是刻意的

    先按 `(surface, bottom)` 归并, 再在**归并后的代表行**上做安全筛查。
    反过来(先筛查再归并)会把"同一个故事的 100 份拷贝"重复扫 100 次,
    而且 rejected 列表里会出现 100 条一模一样的记录 —— 那份列表是给
    人看的, 重复 100 遍等于没法看。

    ## 代表行怎么选

    同一组里取 `id` 最小的那行 —— 数据集里 id 就是原始顺序, 取最小 =
    取最靠前的那次出现, 重跑结果**稳定**(取最大也稳定, 但"第一次出现"
    更符合直觉)。其余行的 `user_guess`/`label` 归到 `benchmark_guesses`。

    ⚠️ `user_guess`/`label` **绝不进 concept 文本**(任务书: "不进直播题
    文本, 但保留到 benchmark_guesses, 因为以后可以拿来测试 Judge")。
    """
    groups: dict = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        surface = strip_html(r.get("surface"))
        bottom = strip_html(r.get("bottom"))
        if not surface or not bottom:
            continue
        key = (surface, bottom)
        groups.setdefault(key, []).append(r)

    records: list = []
    rejected: list = []
    n_guess = 0
    for (surface, bottom), members in groups.items():
        members = sorted(members, key=lambda r: int(r.get("id") or 0))
        head = members[0]
        title = strip_html(head.get("title"))
        ext = stable_hash("turtlebench", surface, bottom)
        rec = RawCuratedPuzzle(
            external_id=f"turtlebench:{ext}",
            source="TurtleBench1.5k",
            source_url=(f"https://huggingface.co/datasets/{REPO}"),
            source_kind="dataset",
            title=title,
            surface=surface,
            bottom=bottom,
            language="zh",
            original_language="zh",
            translated=False,
            tags=["turtle-soup"],
            question_license=_norm_license(license_name),
            answer_license=_norm_license(license_name),
            # 数据集**没有**逐题作者 —— 如实留空, 不编。
            question_author="", answer_author="",
            # 许可来自 API 而不是逐条 post, 所以 inference 记 "api"
            # (它是 API 明确声明的, 不是我们按日期猜的)。
            question_license_inference="api",
            answer_license_inference="api",
        )
        n_guess += len(members)
        flag = safety_screen(surface + "\n" + bottom)
        if flag:
            rec.safety_flag = flag
            rejected.append(rec)
            continue
        records.append(rec)

    stats = {
        "raw_rows": len(rows),
        "unique_stories": len(groups),
        "kept": len(records),
        "rejected_safety": len(rejected),
        "raw_guess_rows": n_guess,
        "license": _norm_license(license_name),
        "license_source": license_src,
    }
    return records, rejected, stats


def write_benchmark_guesses(path: str, rows: list) -> int:
    """把 (user_guess, label) 单独落一份 —— 以后用来测 Judge。

    **独立于 curated 记录**: 它是评测数据, 不是题库内容。混进
    curated_raw.jsonl 会让那份文件的语义变浑(一个文件两种东西)。
    """
    ensure_dir(path)
    n = 0
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for r in sorted(rows, key=lambda x: int((x or {}).get("id") or 0)):
            if not isinstance(r, dict):
                continue
            f.write(json.dumps({
                "id": r.get("id"),
                "title": strip_html(r.get("title")),
                "surface": strip_html(r.get("surface")),
                "bottom": strip_html(r.get("bottom")),
                "user_guess": r.get("user_guess"),
                "label": r.get("label"),
            }, ensure_ascii=False, sort_keys=True,
                separators=(",", ":")) + "\n")
            n += 1
    log.info("写入 %d 条 benchmark guesses -> %s", n, path)
    return n


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="import_turtlebench",
        description="下载 + 归一 TurtleBench1.5k 中文海龟汤")
    ap.add_argument("--from-file", default="",
                    help="跳过下载, 直接用本地 raw JSONL(离线可复现)")
    ap.add_argument("--root", default=EXTERNAL_ROOT,
                    help=f"输出根目录, 默认 {EXTERNAL_ROOT}")
    ap.add_argument("--proxy", default=None,
                    help="HTTP 代理(默认取 HGT_PROXY/HTTPS_PROXY, "
                         "再退到 127.0.0.1:7897; 传空串 = 直连)")
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--log-level", default="INFO")
    return ap


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(a.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    proxy = resolve_proxy(a.proxy)
    log.info("代理: %s", proxy or "(直连)")

    base = os.path.join(a.root, "turtlebench")
    raw_p = os.path.join(base, "raw", "zh_data.jsonl")
    norm_p = os.path.join(base, "normalized", "turtlebench.jsonl")
    meta_p = os.path.join(base, "normalized", "turtlebench.meta.json")
    rej_p = os.path.join(base, "rejected_safety.jsonl")
    guess_p = os.path.join(base, "benchmark_guesses.jsonl")

    # ---- 1. 拿原始数据 ----
    if a.from_file:
        if not os.path.exists(a.from_file):
            log.error("--from-file 不存在: %s", a.from_file)
            return 1
        with open(a.from_file, "rb") as f:
            blob = f.read()
        src = f"file:{a.from_file}"
        license_name, license_src = LICENSE_FALLBACK, "from_file"
    else:
        try:
            blob, base_url = _download(DATA_FILE, timeout=a.timeout,
                                       proxy=proxy)
        except RuntimeError as e:
            log.error("%s", e)
            return 1
        src = base_url
        license_name, license_src = _license(proxy=proxy)

    ensure_dir(raw_p)
    with open(raw_p, "wb") as f:
        f.write(blob)

    rows = [json.loads(l) for l in blob.decode("utf-8").splitlines()
            if l.strip()]
    log.info("原始 %d 行", len(rows))

    # ---- 2. 去重 + 归一 + 安全筛查 ----
    records, rejected, stats = build_records(rows, license_name, license_src)
    write_jsonl_deterministic(norm_p, records)
    write_jsonl_deterministic(rej_p, rejected)
    write_benchmark_guesses(guess_p, rows)
    write_meta(meta_p, dataset=REPO, endpoint=src, data_file=DATA_FILE,
               **stats)

    # ---- 3. 报告(数字必须能被任务书的验收直接引用) ----
    print()
    print("=" * 62)
    print("TurtleBench 导入")
    print("=" * 62)
    print(f"  原始行数            : {stats['raw_rows']}")
    print(f"  **独立故事数**      : {stats['unique_stories']}")
    print(f"  入 curated_raw      : {stats['kept']}")
    print(f"  安全筛查拦下        : {stats['rejected_safety']}")
    print(f"  许可证              : {stats['license']} "
          f"(来源 {stats['license_source']})")
    print(f"  原始 (guess,label) 行: {stats['raw_guess_rows']}")
    print()
    print("  ⚠️ 独立故事只有 %d 个 —— 不要把 %d 行误当成 %d 道题。"
          % (stats["unique_stories"], stats["raw_rows"],
             stats["raw_rows"]))
    print(f"  -> {norm_p}")
    if rejected:
        print(f"  {stats['rejected_safety']} 条命中安全筛查 -> {rej_p}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
