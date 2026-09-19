#!/usr/bin/env python
# coding: utf-8
"""H1-B: 从 **Puzzling Stack Exchange** 抓题(本题库的主力来源)。

## 为什么走 API 而不是 Data Dump

任务书写得很清楚, 实测也支持:

    1. Stack Exchange API          <- 当前更省事, 本脚本走这条
    2. 用户手动放入 Data Dump
    3. 老 Internet Archive dump    <- 只作备选

SE 从 2024 年起改了 dump 的获取方式(不再持续往 Internet Archive 传最新
dump, 改成账户页面申请), 所以"下载几十 GB 的 7z 再解 XML"这条路既不
可靠也不必要 —— 我们要的几百道题, API 分页就够, 而且**直接**带回
`question_id / accepted_answer_id / owner / score / creation_date /
content_license`, provenance 一次到位。

**绝不让生产脚本强依赖 Archive.org。**

## 抓哪两个 tag

    situation          —— 官方定义就接近我们的目标: 给出一个异常情境的
                          信息, 要求推出发生了什么 / 为什么会这样。
    lateral-thinking   —— 候选多得多(约 1430), 但**混着**数学/字谜/
                          图形/密码/象棋题。所以必须筛(见 H2-A)。

`/search/advanced` 的 `tagged` 是 **OR** 语义, 所以 `situation;lateral-thinking`
一次就能覆盖两类。

## 版权(本脚本最重要的一段)

每个 post 的 `content_license` 由 API **直接回**(实测 question 与 answer
都有), 那是权威值 —— 比按发布日期猜可靠得多。但两者都可能跨版本
(老问题、新采纳答案), 所以 **question 与 answer 各存各的许可**。

API 没回时回退按发布时间分段(<2011-04-08 2.5 / 2011-04-08~2018-05-01 3.0 /
>=2018-05-02 4.0), 并显式把来源记成 `created_at` —— 见
`curated_common.resolve_license`。

**绝不统一硬编码 CC BY-SA 4.0。**

## 用法

    .venv/Scripts/python.exe tools/import_puzzling_se.py --dry-run
    .venv/Scripts/python.exe tools/import_puzzling_se.py --max-situation 250 --max-lateral 250

产物(全部在 data_external/ 下, **不进版本库**):

    data_external/puzzling_se/raw/questions.jsonl    原始 question 记录
    data_external/puzzling_se/raw/answers.jsonl      原始 answer 记录
    data_external/puzzling_se/normalized/puzzling_se.jsonl
    data_external/puzzling_se/normalized/puzzling_se.meta.json
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.curated_common import (  # noqa: E402
    EXTERNAL_ROOT, RawCuratedPuzzle, ensure_dir, http_get, resolve_proxy,
    resolve_license, safety_screen, stable_hash, strip_html,
    write_jsonl_deterministic, write_meta,
)

log = logging.getLogger("hgt.puzzling")

API = "https://api.stackexchange.com/2.3"
SITE = "puzzling"
PAGE_SIZE = 100

#: 任务书 H2-A 的**确定性**第一层过滤: 这些 tag 一出现就说明它不是
#: 海龟汤(数学/字谜/图形/密码/棋盘/代码)。
#:
#: ⚠️ 这一层在**进 AI 之前**跑。理由不只是省钱 —— 让 AI 去判"这是不是
#: 数学题"是一种浪费, 而 tag 是 SE 自己标的、免费的确定性信号。
NON_STORY_TAGS = frozenset({
    "mathematics", "geometry", "calculation-puzzle", "pattern",
    "word", "cipher", "cryptography", "chess", "programming",
    "code-golf", "number-theory", "probability", "algorithm",
    "rubiks-cube", "board-games", "card-games", "mechanical-puzzle",
    "visual", "rebus", "computer-science",
})

#: 我们**想要**的 tag(至少命中一个才算"像海龟汤")。
STORY_TAGS = frozenset({
    "situation", "story", "lateral-thinking", "logical-deduction",
    "mystery", "riddle",
})

#: `filter=withbody` —— SE 的官方内建 filter, 会把正文一起返回。
#: 没有它就只拿到标题, 而标题对海龟汤基本没用。
WITHBODY = "withbody"


class Throttled(RuntimeError):
    """SE 对我们限流了(HTTP 429 / error 1015)。

    ## 为什么要单独一个异常类型

    这和"某个 answer id 取不到"**完全不是一回事**:

        429          -> 我们被限流了, **一条都没真的查过**, 必须停下/长等
        缺 id        -> 那条内容真的没了(已删除), 可以跳过

    早先 `fetch_answers` 把两者都吞成"返回一个短 dict" —— 于是限流
    表现成 `answer_missing=243`, 而日志里一句错都没有。运营看到这个
    数字会以为"SE 上那些题没有采纳答案", 从而**永久放弃**一批本来
    完全可用的题。这是本批最危险的一类静默失败, 所以专门建一个类型,
    让调用方**不可能**把它和"内容缺失"混淆。
    """


def _is_throttle(e: BaseException) -> bool:
    """这个异常是不是"被限流了"? """
    if isinstance(e, urllib.error.HTTPError):
        return e.code in (429, 503, 502)
    s = str(e)
    return "429" in s or "1015" in s or "Too Many Requests" in s


# ----------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------
def _api(path: str, params: dict, timeout: float = 30.0,
         max_retries: int = 3, proxy: str = "") -> dict:
    """调一次 SE API, 处理 gzip 与退避。

    SE 的 quota 很紧(实测匿名 300/天, 每 100 条一页就吃 1 点), 而且
    还有一层**独立于 quota 的**限流: 请求打太密会直接回 HTTP 429 /
    error 1015, 连 `quota_remaining` 都看不到。实测连续探测几十次就会
    撞上它, 之后连单条查询都 429。

    所以这里的策略是:

        429 / 5xx   -> 长退避后重试(指数), 重试耗尽 -> 抛 `Throttled`
        SE error_id -> 立刻抛(quota 用尽 / 参数错, 重试没用)
        其它异常    -> 短退避重试

    **绝不**把限流降级成"返回空结果" —— 那是 `Throttled` 存在的理由。
    """
    q = dict(params)
    q.setdefault("site", SITE)
    url = f"{API}/{path}?{urllib.parse.urlencode(q)}"
    last: Exception | None = None
    throttled = False
    for attempt in range(max_retries):
        try:
            blob = http_get(url, proxy=proxy, timeout=timeout,
                            accept_gzip=True, retries=1)
            d = json.loads(blob.decode("utf-8"))
            # SE 明确要求: 回了 backoff 就必须等。
            bo = d.get("backoff")
            if bo:
                log.warning("SE 要求 backoff %ss, 等待…", bo)
                time.sleep(float(bo) + 1.0)
            if d.get("error_id"):
                raise RuntimeError(f"SE error {d.get('error_id')}: "
                                   f"{d.get('error_message')}")
            return d
        except (RuntimeError, json.JSONDecodeError, OSError) as e:
            if _is_throttle(e):
                throttled = True
                # 限流要等**久**: 1015 的冷却通常是分钟级, 秒级重试
                # 只会把它续上。指数 20/40/80 秒。
                sleep = 20.0 * (2 ** attempt)
                log.warning("SE 限流(429/1015), 等 %.0fs 后重试(%d/%d)",
                            sleep, attempt + 1, max_retries)
                time.sleep(sleep)
                last = e
                continue
            if "SE error" in str(e):
                raise
            last = e
            sleep = 2.0 * (attempt + 1)
            log.warning("API 失败(第 %d 次): %s —— %.0fs 后重试",
                        attempt + 1, str(e)[:100], sleep)
            time.sleep(sleep)
    if throttled:
        raise Throttled(f"SE 限流, {max_retries} 次重试仍未成功: {last}")
    raise RuntimeError(f"API 连续 {max_retries} 次失败: {last}")


def fetch_questions(tag: str, *, min_score: int, max_items: int,
                    accepted_only: bool, proxy: str = "") -> tuple:
    """分页抓 `/search/advanced`。返回 `(items, quota_left, pages)`。"""
    out: list = []
    page = 1
    quota_left = None
    pages = 0
    while len(out) < max_items:
        params = {
            "tagged": tag,
            "sort": "votes",
            "order": "desc",
            "pagesize": PAGE_SIZE,
            "page": page,
            "filter": WITHBODY,
        }
        if accepted_only:
            # `accepted=True` 只回**有采纳答案**的问题。海龟汤没有
            # 公认谜底就没有"bottom", 对我们毫无用处。
            params["accepted"] = "True"
        d = _api("search/advanced", params, proxy=proxy)
        items = d.get("items") or []
        quota_left = d.get("quota_remaining", quota_left)
        pages += 1
        if not items:
            break
        for it in items:
            if int(it.get("score") or 0) < min_score:
                # sort=votes desc, 所以一旦低于阈值后面都不会更高 ——
                # 但**不能 break 整页**, 因为同分段的顺序不保证。
                continue
            out.append(it)
            if len(out) >= max_items:
                break
        if not d.get("has_more"):
            break
        page += 1
        time.sleep(0.3)          # 温柔一点, 别把 quota 撞爆
    return out, quota_left, pages


def fetch_answers(answer_ids: list, batch: int = 100,
                  proxy: str = "", max_pages: int = 20) -> tuple:
    """按 id 批量取答案。返回 `(by_id, quota_left)`。

    ## 为什么分批

    `/answers/{ids}` 一次最多 100 个 id —— 给 200 个直接回 **HTTP 400**。
    所以必须分批。

    ## ⚠️ 更要紧的: 这个接口**每页只回 30 条**, 且带 `has_more`

    实测(H1-B 的真实故障): 请求 100 个 id, 响应是

        items = 30 条,  has_more = True

    如果没有读 `has_more` 去翻页, 就会**静默只拿到前 30 条**。5 批
    × 30 = 150 —— 也就是之前 `answer_missing: 243` 那个数字的**全部
    来源**。它看起来像"SE 上那些题没有采纳答案", 实际是"我们只翻了
    第一页"。这类错误不会报任何异常, 只会让题库莫名其妙地小一圈。

    所以这里必须:

        每批循环翻页, 直到 has_more 为 False
        每批结束**对账**: 请求数 vs 实收数, 差额如实报出来

    差额 > 0 是**正常的**(已删除的答案不会返回), 但差额大到接近整页
    就说明翻页逻辑又坏了 —— 所以下面加了一条比例告警。

    ## 为什么**不**吞掉整批失败

    早先这里对每一批的异常都 `log.warning` 后继续, 于是"接口 400"
    被记成"答案少了几条"。现在的语义:

        单批重试耗尽 -> 抛 `Throttled` / `RuntimeError`, 整个 import 失败
        抓到 0 条     -> 抛 `Throttled`(不可能真的全没答案)

    宁可这次不产出, 也不产出一份"看起来成功、其实缺了一大半"的文件。
    """
    by_id: dict = {}
    quota_left = None
    ids = [str(i) for i in answer_ids if i]
    if not ids:
        return by_id, quota_left
    batches = [ids[i:i + batch] for i in range(0, len(ids), batch)]
    for bi, chunk in enumerate(batches, 1):
        # ⚠️ **不要**在末尾加 `;`。SE 把 `answers/1;2;3;` 当成一个方法名,
        # 回 **HTTP 400 "no method found with this name"**。
        path = f"answers/{';'.join(chunk)}"
        got = 0
        for page in range(1, max_pages + 1):
            d = _api(path, {"filter": WITHBODY, "page": page}, proxy=proxy)
            quota_left = d.get("quota_remaining", quota_left)
            items = d.get("items") or []
            for a in items:
                by_id[int(a["answer_id"])] = a
            got += len(items)
            if not d.get("has_more"):
                break
            time.sleep(0.5)
        log.info("答案批次 %d/%d: 请求 %d, 实收 %d(%s)",
                 bi, len(batches), len(chunk), got,
                 "已翻完" if got >= len(chunk) else "有缺口")
        # 缺口**必须**可见。已删除的答案会造成小缺口, 但缺口大到接近
        # 一整页(30)就说明翻页又坏了 —— 那时要有人看见, 而不是当成
        # "SE 上没答案"。
        if len(chunk) - got >= 30:
            log.warning("批次 %d 缺口 %d 条(请求 %d 实收 %d)—— 缺口达到"
                        "整页量级, 请确认 has_more 翻页是否正常。",
                        bi, len(chunk) - got, len(chunk), got)
        time.sleep(0.5)
    if not by_id:
        raise Throttled(
            f"请求了 {len(ids)} 个 answer 但一个都没拿到 —— 几乎一定是"
            f"被限流或网关异常, **不是**这些题都没有采纳答案。"
            f"本次不产出文件。")
    return by_id, quota_left


# ----------------------------------------------------------------------
# 归一
# ----------------------------------------------------------------------
def _owner(post: dict) -> tuple:
    """返回 `(display_name, profile_url)`。匿名/已删号如实留空。"""
    o = post.get("owner") or {}
    name = str(o.get("display_name") or "").strip()
    uid = o.get("user_id")
    link = o.get("link") or (f"https://puzzling.stackexchange.com/users/{uid}"
                             if uid else "")
    return name, str(link or "")


def _tags(it: dict) -> list:
    return [str(t).strip().lower() for t in (it.get("tags") or [])
            if str(t).strip()]


def is_story_like(tags: list) -> tuple:
    """H2-A 的确定性过滤。返回 `(ok, reason)`。

    两条规则:
      1. 命中任一 `NON_STORY_TAGS` -> 拒(数学/密码/象棋/代码…)
      2. 一个 `STORY_TAGS` 都不命中 -> 拒(不是叙事推理题)

    第 2 条容易被忽略但同样重要: `lateral-thinking` 里有些题**只**带
    `lateral-thinking` + 一个冷门 tag, 既不是数学也不是故事 —— 那是
    纯知识问答。要求"至少命中一个 story tag"把它们挡在外面。

    ⚠️ `lateral-thinking` 本身**在** STORY_TAGS 里。这是有意的: 它是
    我们的目标 tag 之一。真正干活的排除由第 1 条(NON_STORY)完成 ——
    所以 H2-A 的"不要把 lateral-thinking 当成自动等于海龟汤"是这样
    落地的: 先按 NON_STORY 剔, 再由 AI 审题门细判。
    """
    ts = set(tags)
    bad = ts & NON_STORY_TAGS
    if bad:
        return False, "非叙事 tag: " + "/".join(sorted(bad))
    if not (ts & STORY_TAGS):
        return False, "没有任何叙事推理 tag"
    return True, ""


def build_records(questions: list, answers_by_id: dict
                  ) -> tuple:
    """question + accepted answer -> RawCuratedPuzzle。返回 `(recs, stats)`。

    ## 只要 **accepted** answer

    海龟汤需要"公认谜底"。一个没有采纳答案的问题, bottom 是我们自己
    挑的 —— 那等于我们在替原题作者决定谜底, 正是 H2 明令禁止的
    "AI 不准重新创作故事"。所以没有 accepted_answer_id 的问题**直接跳过**。

    ## `title + body` 怎么变成 surface

    SE 的 body 是 HTML, 里面常有 spoiler 块(`<blockquote class="spoiler">`)
    —— 那**就是**谜底的一部分。我们只粗剥标签, 由 AI 审题门去判断
    "题面是否泄底"。(更精细的 spoiler 剥离需要真 HTML 解析, 会引入
    新依赖; 而这些题后面还要过一道 AI 门, 粗剥够用。)
    """
    recs: list = []
    stats = {"questions": len(questions), "no_accepted": 0,
             "answer_missing": 0, "non_story": 0, "kept": 0,
             "license_unknown": 0}
    for it in questions:
        tags = _tags(it)
        ok, why = is_story_like(tags)
        if not ok:
            stats["non_story"] += 1
            log.info("跳过(%.40s): %s", it.get("title"), why)
            continue
        aid = it.get("accepted_answer_id")
        if not aid:
            stats["no_accepted"] += 1
            continue
        ans = answers_by_id.get(int(aid))
        if not ans:
            stats["answer_missing"] += 1
            continue

        q_surface = strip_html(it.get("body"))
        q_title = strip_html(it.get("title"))
        a_body = strip_html(ans.get("body"))
        if not q_surface or not a_body:
            stats["answer_missing"] += 1
            continue

        q_lic, q_inf = resolve_license(it.get("content_license"),
                                       it.get("creation_date"))
        a_lic, a_inf = resolve_license(ans.get("content_license"),
                                       ans.get("creation_date"))
        if q_inf in ("unknown", "unrecognized"):
            stats["license_unknown"] += 1

        qn, qu = _owner(it)
        an, au = _owner(ans)
        qid = int(it["question_id"])

        rec = RawCuratedPuzzle(
            external_id=f"pse:q:{qid}",
            source="Puzzling Stack Exchange",
            source_url=str(it.get("link") or
                           f"https://puzzling.stackexchange.com/q/{qid}"),
            source_kind="stackexchange",
            question_author=qn, question_author_url=qu,
            answer_author=an, answer_author_url=au,
            question_license=q_lic, answer_license=a_lic,
            question_license_inference=q_inf,
            answer_license_inference=a_inf,
            question_created_at=it.get("creation_date"),
            answer_created_at=ans.get("creation_date"),
            question_id=qid, answer_id=int(ans["answer_id"]),
            title=q_title,
            surface=q_surface,
            bottom=a_body,
            language="en",
            original_language="en",
            # 还没翻译 —— H2-C 那一步才翻, 翻了再置 True。
            translated=False,
            tags=tags,
            question_score=int(it.get("score") or 0),
            answer_score=int(ans.get("score") or 0),
        )
        flag = safety_screen(q_surface + "\n" + a_body)
        rec.safety_flag = flag
        recs.append(rec)
        stats["kept"] += 1

    # ---- 确定性哈希后缀 ----
    # external_id 用 SE 的 question_id(稳定且可反查原帖), **不用**内容
    # 哈希 —— 这里 id 本身就是权威身份, 而且保留它才能生成 attribution
    # 里的原帖链接。任务书 H1-D 的 "<stable-hash>" 是针对 TurtleBench
    # (那个数据集只有自增 id, 换个分片就变)的, 不是普适要求。
    return recs, stats


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="import_puzzling_se",
        description="从 Puzzling Stack Exchange 抓 situation / lateral-thinking")
    ap.add_argument("--max-situation", type=int, default=250,
                    help="situation 最多抓多少题(默认 250)")
    ap.add_argument("--min-score-situation", type=int, default=2,
                    help="situation 的最低分(默认 2)")
    ap.add_argument("--max-lateral", type=int, default=250,
                    help="lateral-thinking 最多抓多少题(默认 250)")
    ap.add_argument("--min-score-lateral", type=int, default=5,
                    help="lateral-thinking 的最低分(默认 5)")
    ap.add_argument("--all-lateral", action="store_true",
                    help="lateral-thinking 不做 score 门槛(仅排查用)")
    ap.add_argument("--root", default=EXTERNAL_ROOT)
    ap.add_argument("--proxy", default=None,
                    help="HTTP 代理(默认取 HGT_PROXY/HTTPS_PROXY, "
                         "再退到 127.0.0.1:7897; 传空串 = 直连)")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--dry-run", action="store_true",
                    help="只抓第一页看看形状, 不落盘")
    ap.add_argument("--log-level", default="INFO")
    return ap


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(a.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    proxy = resolve_proxy(a.proxy)
    log.info("代理: %s", proxy or "(直连)")

    base = os.path.join(a.root, "puzzling_se")
    raw_q = os.path.join(base, "raw", "questions.jsonl")
    raw_a = os.path.join(base, "raw", "answers.jsonl")
    norm_p = os.path.join(base, "normalized", "puzzling_se.jsonl")
    meta_p = os.path.join(base, "normalized", "puzzling_se.meta.json")
    rej_p = os.path.join(base, "rejected_safety.jsonl")

    quota = {}

    # ---- 1. 抓 question ----
    log.info("抓 situation (score>=%d, 最多 %d)…",
             a.min_score_situation, a.max_situation)
    sit, q1, p1 = fetch_questions("situation",
                                  min_score=a.min_score_situation,
                                  max_items=a.max_situation,
                                  accepted_only=True, proxy=proxy)
    quota["after_situation"] = q1
    quota["situation_pages"] = p1
    log.info("situation 抓到 %d 条(%d 页, 剩余 quota %s)", len(sit), p1, q1)

    lat_score = 0 if a.all_lateral else a.min_score_lateral
    log.info("抓 lateral-thinking (score>=%d, 最多 %d)…",
             lat_score, a.max_lateral)
    lat, q2, p2 = fetch_questions("lateral-thinking",
                                  min_score=lat_score,
                                  max_items=a.max_lateral,
                                  accepted_only=True, proxy=proxy)
    quota["after_lateral"] = q2
    quota["lateral_pages"] = p2
    log.info("lateral-thinking 抓到 %d 条(%d 页, 剩余 quota %s)",
             len(lat), p2, q2)

    # ---- 2. 去重(同一个 question 可能同时带两个 tag) ----
    merged: dict = {}
    for it in sit + lat:
        merged[int(it["question_id"])] = it
    questions = sorted(merged.values(),
                       key=lambda x: (-int(x.get("score") or 0),
                                      int(x["question_id"])))
    log.info("合并去重后 %d 个 question", len(questions))

    if a.dry_run:
        print(json.dumps(questions[0], ensure_ascii=False, indent=2)[:1500]
              if questions else "(空)")
        return 0

    if not questions:
        log.error("一个 question 都没抓到 —— 检查网络 / quota / tag 名。"
                  "**不写空文件**(那会让下游以为导入成功)。")
        return 1

    # ---- 3. 抓 accepted answer ----
    aids = [it.get("accepted_answer_id") for it in questions
            if it.get("accepted_answer_id")]
    log.info("取 %d 个 accepted answer…", len(aids))
    try:
        answers, q3 = fetch_answers(aids, proxy=proxy)
    except Throttled as e:
        # ⚠️ 限流**不产出文件**。产出一份"看起来成功、其实缺了大部分
        # 答案"的 curated_raw.jsonl, 比这次失败糟得多 —— 下游会把它当
        # 成完整题库, 而没有任何信号提示它不完整。
        log.error("%s", e)
        log.error("已抓到的 question 原始记录保留在 %s(便于重跑时续用), "
                  "但**不产出归一文件**。稍后重跑。", raw_q)
        ensure_dir(raw_q)
        with open(raw_q, "w", encoding="utf-8", newline="\n") as f:
            for r in sorted(questions,
                            key=lambda x: json.dumps(x, sort_keys=True)):
                f.write(json.dumps(r, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":")) + "\n")
        return 1
    quota["after_answers"] = q3
    quota["answers_requested"] = len(aids)
    quota["answers_received"] = len(answers)
    log.info("拿到 %d/%d 个 answer(剩余 quota %s)",
             len(answers), len(aids), q3)

    # ---- 4. 落原始(便于复现与排查; 不进版本库) ----
    ensure_dir(raw_q)
    for p, rows in ((raw_q, questions), (raw_a, list(answers.values()))):
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            for r in sorted(rows, key=lambda x: json.dumps(x, sort_keys=True)):
                f.write(json.dumps(r, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":")) + "\n")
    log.info("原始记录 -> %s / %s", raw_q, raw_a)

    # ---- 5. 归一 ----
    recs, stats = build_records(questions, answers)
    rejected = [r for r in recs if r.safety_flag]
    kept = [r for r in recs if not r.safety_flag]
    write_jsonl_deterministic(norm_p, kept)
    write_jsonl_deterministic(rej_p, rejected)
    write_meta(meta_p, site=SITE, tags=["situation", "lateral-thinking"],
               min_score_situation=a.min_score_situation,
               min_score_lateral=lat_score,
               quota=quota, **stats)

    # ---- 6. 报告 ----
    print()
    print("=" * 62)
    print("Puzzling Stack Exchange 导入")
    print("=" * 62)
    print(f"  situation 抓到      : {len(sit)}")
    print(f"  lateral-thinking    : {len(lat)}")
    print(f"  合并去重后 question : {len(questions)}")
    print(f"  拿到 accepted answer: {len(answers)}")
    print(f"  非叙事 tag 剔除     : {stats['non_story']}")
    print(f"  **无采纳答案跳过**  : {stats['no_accepted']}")
    print(f"  答案取不到          : {stats['answer_missing']}")
    print(f"  许可证 unknown      : {stats['license_unknown']}")
    print(f"  入 curated_raw      : {len(kept)}")
    print(f"  安全筛查拦下        : {len(rejected)}")
    print(f"  剩余 quota          : {quota.get('after_answers')}")
    print(f"  -> {norm_p}")
    print()
    lic = {}
    for r in kept:
        k = (r.question_license, r.question_license_inference)
        lic[k] = lic.get(k, 0) + 1
    print("  许可证分布(question):")
    for (l, inf), n in sorted(lic.items(), key=lambda x: -x[1]):
        print(f"    {l:16s} ({inf:12s}) x{n}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
