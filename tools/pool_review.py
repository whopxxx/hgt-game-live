#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""只读题池审视 —— 把**当前真正可播**的库存平铺出来给人看。

## 这个工具做什么 / 不做什么

做:

    * 用**真的 `PuzzlePool.load()`**(所以 used 账本、墓碑、池内自去重、
      坏行容错全都与直播一致)载入池子;
    * 从 `_items` 里逐个跑 `_validate_pool_spec()` + used 判定, 得出
      **可播集合**, 并**断言它的数量等于 `stock_count()`** ——
      两个数不一致就直接报错, 不出一份看着像样但不可信的报告;
    * 按**汤面字数从长到短**排, 同长度按 lane 展开 ——
      146 字那种"案情简介化"直接浮到最上面;
    * 每道摊开 `puzzle / answer / lane / keywords / 字数 /
      story_prompt_version / surface_prompt_version /
      quality_policy_version / quality_checks / safety_verified /
      safety_reason`;
    * 一个**极轻**的汇总: 总库存、red/black 计数、字数 min/median/max、
      以及 <40 / 40-60 / 61-80 / >80 四档计数;
    * 另附"被隔离"清单(只讲原因, 不展开正文)。

**不做**:

    * 不给"好玩度"评分 / 不给"红黑纯度"评分
    * 不自动淘汰任何题
    * 不加 Reviewer / 不调 LLM / 不做"反套路检测"

一句话: 它只负责**把要看的摊开**, 好坏的判断留给人。

## ⚠️ 可播集合**只能**来自 `load()`, 不能来自扫 JSONL

第一版是"逐行读 JSONL + 自己跑 `_validate_pool_spec`", 那是**错的**:

    * 它完全绕过了 `load()` 里的 used 账本处理 —— used 恒为空,
      已播过的题会被算成可播;
    * 墓碑(`_voided_keys`)、池内自去重、坏行跳过这些 `load()` 的
      语义它都没有;
    * 更要命的是它可能与 `stock_count()` **不一致**, 而报告读者
      无从察觉 —— 一份"看起来对"的库存快照比没有更危险。

所以现在:

    可播集合  = `load()` 之后的 `_items` ∩ 过门 ∩ 未 used  (权威)
    原始 JSONL = **只用来展示"为什么被隔离"**, 不参与可播判定
    自检       = `len(可播集合) == stock_count()`, 不等就报错退出

## used 账本

generated 池的账本是 `cfg.pool_used_path`(默认 `data/pool_used.jsonl`),
**不是** `puzzle.jsonl`。不传的话 `PuzzlePool` 读不到它, used 恒空 ——
这正是第一版最大的错。本工具显式设好这三个路径再 `open()`。

## 用法

    .venv/Scripts/python.exe tools/pool_review.py
    .venv/Scripts/python.exe tools/pool_review.py --out data/pool_review/REPORT.md
    .venv/Scripts/python.exe tools/pool_review.py --limit 5

**只读**: 不写池子、不动 used 账本、不改配置。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ⚠️ GBK 控制台: 输出里有中文与箭头。任何非 GBK 字符都会让 print() 抛
# UnicodeEncodeError —— 本项目已经踩过 (U+26A0 / U+2286 / U+00A9)。
# 这里显式包一层, 保证工具在任何终端都不会**因为一行日志**而崩。
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8",
                                  errors="replace")

from story.pool import PuzzlePool, spec_key  # noqa: E402
from story.puzzle import PuzzleSpec  # noqa: E402
from story.quality import QUALITY_POLICY_VERSION  # noqa: E402


#: 字数分档 —— **真的** <40 / 40-60 / 61-80 / >80。
#: 注意是左闭右开: 40 落进 "40-60", 60 也落进 "40-60"。
BUCKETS = ((40, "<40"), (61, "40-60"), (81, "61-80"), (10 ** 9, ">80"))


def _bucket(n: int) -> str:
    for hi, name in BUCKETS:
        if n < hi:
            return name
    return ">80"


def _median(xs: list) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    m = len(s) // 2
    if len(s) % 2:
        return float(s[m])
    return (s[m - 1] + s[m]) / 2.0


def _read_jsonl_raw(path: str) -> list:
    """原始行 —— **只**用于展示隔离原因, 不参与可播判定。"""
    rows = []
    if not path or not os.path.exists(path):
        return rows
    with io.open(path, encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append((ln, json.loads(line)))
            except Exception as e:                  # noqa: BLE001
                rows.append((ln, {"__bad__": str(e)}))
    return rows


def _as_cfg(pool_path: str, used_path: str, kind: str):
    """造一个**只读**用的最小 cfg。

    ⚠️ 三个字段都必须给全。少给 `pool_used_path` 会让 used 恒空 ——
    已播过的题会被算成可播, 而那正是这份报告要避免的事。
    """
    from story.config import Config
    cfg = Config()
    cfg.pool_path = pool_path
    cfg.pool_used_path = used_path
    cfg.pool_enabled = True
    cfg.pool_kind = kind
    return cfg


def _item_of(pool: PuzzlePool, spec) -> dict:
    m = dict(getattr(spec, "metrics", None) or {})
    puzzle = spec.puzzle or ""
    return {
        "puzzle": puzzle,
        "answer": spec.answer or "",
        "lane": str(m.get("lane") or ""),
        "keywords": list(m.get("keywords") or []),
        "chars": len(puzzle),
        "story_prompt_version": str(m.get("story_prompt_version") or ""),
        "surface_prompt_version": str(m.get("surface_prompt_version") or ""),
        "quality_policy_version":
            str(getattr(spec, "quality_policy_version", "") or ""),
        "quality_checks": dict(m.get("quality_checks") or {}),
        "safety_verified": m.get("safety_verified"),
        "safety_reason": str(m.get("safety_reason") or ""),
    }


def collect(pool_path: str, used_path: str, kind: str) -> dict:
    """**权威**可播集合来自 `load()`; 隔离清单来自原始 JSONL 对照。"""
    cfg = _as_cfg(pool_path, used_path, kind)
    pool = PuzzlePool.open(cfg)
    if pool is None:
        raise SystemExit("池子被 cfg 关掉了(pool_enabled=False), 无法审视")

    # ---- 权威可播集合 ----
    # 判定与 `stock_count()` **逐字同源**: 过门 + `spec_key() not in _used`。
    # 不用任何自造的等价写法 —— 一旦漂, 自检就会失败(那是好事)。
    used = getattr(pool, "_used", set()) or set()
    eligible = []
    for spec in list(getattr(pool, "_items", []) or []):
        ok, _why = PuzzlePool._validate_pool_spec(spec)
        if not ok:
            continue
        if spec_key(spec) in used:
            continue
        eligible.append(_item_of(pool, spec))

    # ---- 自检: 必须与 stock_count() 一致 ----
    stock = int(pool.stock_count())

    # ---- 隔离清单(只为展示原因) ----
    eligible_texts = {e["puzzle"] for e in eligible}
    quarantined = []
    for ln, d in _read_jsonl_raw(pool_path):
        if "__bad__" in d:
            quarantined.append({"line": ln, "pool_key": "",
                                "why": "JSON 解析失败: " + d["__bad__"]})
            continue
        sp_d = d.get("spec") or {}
        key = d.get("pool_key") or ""
        text = str(sp_d.get("puzzle") or "")
        if text and text in eligible_texts:
            continue                    # 这一条在可播集合里, 不算隔离
        try:
            spec = PuzzleSpec.from_dict(sp_d)
        except Exception as e:                      # noqa: BLE001
            quarantined.append({"line": ln, "pool_key": key,
                                "why": f"from_dict 失败: {e}"})
            continue
        ok, why = PuzzlePool._validate_pool_spec(spec)
        if not ok:
            quarantined.append({"line": ln, "pool_key": key, "why": why})
            continue
        if spec_key(spec) in used:
            quarantined.append({"line": ln, "pool_key": key,
                                "why": "已 used(已取过/播过)"})
            continue
        # 过门、未 used, 但不在可播集合里 —— 说明 load() 没把它收进来
        # (墓碑 / 池内自去重)。这**不该**发生: 报出来别猜。
        quarantined.append({"line": ln, "pool_key": key,
                            "why": "过门且未 used, 但 load() 未收录"
                                   "(墓碑作废? 与池内另一条重复?)"})

    return {"eligible": eligible, "quarantined": quarantined,
            "stock_count": stock, "used_n": len(used),
            "size": len(getattr(pool, "_items", []) or []),
            # ⚠️ 必须报出来。账本一旦有一行读不懂, `load()` 会把
            # `_used` 清空并标 `_used_trustworthy=False`(fail closed)——
            # 那时 used 恒空, **已播过的题会被算成可播**。只报一个
            # "used 0 条"会让读者以为"这个池确实没人播过", 而真相是
            # "账本坏了, 这个数不可信"。
            "used_trustworthy": bool(
                getattr(pool, "_used_trustworthy", False))}


def _fmt_checks(qc: dict) -> str:
    if not qc:
        return "(无)"
    bad = [k for k, v in sorted(qc.items()) if v is not True]
    if not bad:
        return "全过(%d 项)" % len(qc)
    return "未过: " + ", ".join("%s=%r" % (k, qc[k]) for k in bad)


def render(rep: dict, pool_path: str, used_path: str, kind: str,
           limit: int = 0) -> str:
    el = rep["eligible"]
    qz = rep["quarantined"]
    L = []
    A = L.append

    A("# 题池审视报告(只读)")
    A("")
    A("**池文件** `%s`" % pool_path)
    A("**used 账本** `%s`" % used_path)
    A("**pool_kind** `%s` · **当前 policy** `%s`" % (kind, QUALITY_POLICY_VERSION))
    A("")
    A("> 可播集合来自真的 `PuzzlePool.load()`(含 used 账本 / 墓碑 /")
    A("> 池内自去重), 并已断言 `len(可播) == stock_count()`。")
    A(">")
    A("> 这个工具只摊开数据, 不做任何好坏判断 —— 不给好玩度评分、")
    A("> 不给红黑纯度评分、不自动淘汰、不调 LLM。")
    A("")

    A("## 一、汇总")
    A("")
    chars = [e["chars"] for e in el]
    red = sum(1 for e in el if e["lane"] == "red")
    black = sum(1 for e in el if e["lane"] == "black")
    other = len(el) - red - black
    A("| 项 | 值 |")
    A("|---|---|")
    A("| **可播库存(stock)** | **%d** |" % len(el))
    A("| `stock_count()` 自检 | `%d` %s |"
      % (rep["stock_count"], "OK" if rep["stock_count"] == len(el) else "**不一致!**"))
    A("| 池内题数(load 后) | %d |" % rep["size"])
    A("| used 账本条数 | %d |" % rep["used_n"])
    if rep.get("used_trustworthy"):
        A("| used 账本可信 | 是 |")
    else:
        A("| used 账本可信 | **否(账本读不全 -> `_used` 已清空)** |")
    A("| 被隔离(对照原始行) | %d |" % len(qz))
    A("| lane=red | %d |" % red)
    A("| lane=black | %d |" % black)
    if other:
        A("| lane=其他/空 | %d |" % other)
    if chars:
        A("| 汤面字数 min / median / max | %d / %g / %d |"
          % (min(chars), _median(chars), max(chars)))
    else:
        A("| 汤面字数 | (无库存) |")
    A("")

    # ---- ⚠️ 账本不可信 -> 整份报告的"可播"都不可信 ----
    if not rep.get("used_trustworthy"):
        A("> ## ⚠️ used 账本不可信 —— 本报告的**可播库存偏高**")
        A(">")
        A("> `load()` 读 used 账本时遇到读不全的行, 按 fail closed 把")
        A("> `_used` **清空**了。于是**已经播过的题会被算成可播** ——")
        A("> 上面那个 stock 数**偏高**(已播过的题被算回来了), 而高多少")
        A("> 无从得知。")
        A(">")
        A("> 先修账本(或换一份好的), 再看这份报告。")
        A("")

    A("### 汤面字数分布")
    A("")
    A("| 档位 | 数量 |")
    A("|---|---|")
    for _, name in BUCKETS:
        A("| %s | %d |" % (name, sum(1 for c in chars if _bucket(c) == name)))
    A("")
    A("> 关注点: 长档(`>80`)占比。R6 观察到滑回\"讲完半个案情\"的题")
    A("> (146 字) 会落在这里。")
    A("")

    if qz:
        from collections import Counter
        A("### 被隔离的原因分布")
        A("")
        c = Counter(_reason_kind(x["why"]) for x in qz)
        A("| 原因 | 数量 |")
        A("|---|---|")
        for k, v in c.most_common():
            A("| %s | %d |" % (k, v))
        A("")

    A("## 二、可播库存(按汤面字数从长到短, 同长度按 lane)")
    A("")
    if not el:
        A("**当前 0 道可播题。**")
        A("")
        A("若盘上仍有题被隔离, 说明它们不在当前 policy 下 —— "
          "需要一次 prewarm 补池。")
        A("")
    elif limit and limit > 0 and limit < len(el):
        A("> ⚠️ `--limit %d` 只渲染了**前 %d 道**(共 %d 道)。"
          % (limit, limit, len(el)))
        A("> 汇总与隔离区**始终是全体**, 不受 limit 影响。")
        A("")

    ordered = sorted(el, key=lambda e: (-e["chars"], e["lane"] or "~",
                                        e["puzzle"]))
    shown = ordered[:limit] if limit and limit > 0 else ordered
    for i, e in enumerate(shown, 1):
        A("### %d. [%d 字] lane=%s" % (i, e["chars"], e["lane"] or "(空)"))
        A("")
        A("- **keywords**: %s"
          % (" / ".join(e["keywords"]) if e["keywords"] else "(无)"))
        A("- **版本**: policy=`%s` story=`%s` surface=`%s`"
          % (e["quality_policy_version"], e["story_prompt_version"],
             e["surface_prompt_version"]))
        A("- **livestream_safe 复核**: `%s`%s"
          % (e["safety_verified"],
             ("  ·  reason: " + e["safety_reason"]) if e["safety_reason"] else ""))
        A("- **主审 quality_checks**: %s" % _fmt_checks(e["quality_checks"]))
        A("")
        A("**汤面(%d 字)**" % e["chars"])
        A("")
        A("> " + (e["puzzle"] or "(空)").replace("\n", " "))
        A("")
        A("**汤底**")
        A("")
        A("> " + (e["answer"] or "(空)").replace("\n", " "))
        A("")

    if qz:
        A("## 三、被隔离的题(不展开正文)")
        A("")
        A("| 行 | pool_key | 原因 |")
        A("|---|---|---|")
        for x in qz[:60]:
            A("| %d | `%s` | %s |"
              % (x.get("line", 0), x.get("pool_key", "") or "-",
                 x["why"][:90]))
        if len(qz) > 60:
            A("| ... | | 另有 %d 条 |" % (len(qz) - 60))
        A("")
    return "\n".join(L) + "\n"


def _reason_kind(why: str) -> str:
    for k in ("quality policy 不兼容", "quality policy 缺失", "已 used",
              "curated", "JSON 解析失败", "from_dict 失败",
              "load() 未收录"):
        if why.startswith(k) or k in why:
            return k
    return why[:40]


def main() -> int:
    ap = argparse.ArgumentParser(description="只读题池审视(不改任何东西)")
    ap.add_argument("--pool", default="data/pool.jsonl", help="池文件路径")
    ap.add_argument("--used", default="", help="used 账本路径(默认为池同目录的 pool_used.jsonl)")
    ap.add_argument("--pool-kind", default="generated",
                    choices=("generated",),
                    help="池类型。本版**只支持 generated** —— curated 走"
                         "的是 open_curated() 与另一套路径/账本字段,"
                         "没测过就不放出来(宁缺勿错)")
    ap.add_argument("--out", default="", help="报告输出路径(.md); 留空则只打印")
    ap.add_argument("--limit", type=int, default=0,
                    help="只渲染前 N 道(0=全部); 汇总与隔离区始终是全体")
    a = ap.parse_args()

    used = a.used or os.path.join(os.path.dirname(a.pool) or ".",
                                  "pool_used.jsonl")
    rep = collect(a.pool, used, a.pool_kind)

    # ---- 硬自检: 可播集合必须等于 stock_count() ----
    if rep["stock_count"] != len(rep["eligible"]):
        print("[FATAL] 可播集合(%d) != stock_count()(%d) —— "
              "报告不可信, 拒绝输出。这是个 bug, 不是数据问题。"
              % (len(rep["eligible"]), rep["stock_count"]), file=sys.stderr)
        return 2

    text = render(rep, a.pool, used, a.pool_kind, limit=a.limit)
    sys.stdout.write(text)
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with io.open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
        sys.stdout.write("\n[写出] %s\n" % a.out)

    n = len(rep["eligible"])
    print("\n[只读] 可播库存 = %d 道 (与 stock_count() 一致); 被隔离 = %d 道"
          % (n, len(rep["quarantined"])))
    if n == 0:
        print("[提示] 0 道可播 —— 若盘上有题, 说明 policy 已 bump, "
              "需要 prewarm 补池。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
