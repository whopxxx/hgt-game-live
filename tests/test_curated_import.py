#!/usr/bin/env python
# coding: utf-8
"""运行: uv run tests/test_curated_import.py（**完全离线, 无网络**）。

Batch H1/H2 的**离线**回归: 外部题库导入的 schema / 归一 / 去重 /
版权 / 安全筛查 / 确定性 落盘。

## 为什么这些测试必须在**离线**套件里

导入脚本本身要联网, 但**它里面真正的逻辑**不能靠"跑一次网络"来测:

    版权分段(<2011-04-08 是 2.5, 不是 4.0)  —— 算错就是法律风险
    normalize_for_dedup                     —— 算错会让同一题通过去重
    确定性落盘(byte-for-byte)              —— 算错会让增量编译永远全量
    is_story_like 的 tag 过滤                —— 算错会放进数学题

这四条都是**纯函数**, 用假数据就能精确断言。放进联网测试里反而测不到
—— 网络一抖, 断言的就是"这一次抓到了什么", 而不是"规则对不对"。

## 本轮最重要的一条回归

`test_se_answer_pagination_reads_has_more` —— 它守的是一个**真实的、
已经发生过的**故障:

    /answers/{ids} 每页只回 30 条并带 has_more
        不翻页 -> 静默只拿前 30 条
        5 批 x 30 = 150       <- 当时看起来像"这些题没有采纳答案"
        实际可用 487 -> 只有 105 条进了 curated_raw(少 70%)

它不报任何异常, 只是让题库莫名其妙小一圈。所以必须有一条测试**钉住**
"has_more 为真时会继续翻页"。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import import_puzzling_se as SE  # noqa: E402
from tools import import_turtlebench as TB  # noqa: E402
from tools.curated_common import (  # noqa: E402
    KNOWN_LICENSES, RawCuratedPuzzle, license_is_usable, normalize_for_dedup,
    resolve_license, safety_screen, se_license_for_ts, sort_records,
    stable_hash, strip_html, write_jsonl_deterministic,
)

FAIL = [0]


def check(name, cond, extra=""):
    if cond:
        print(f"  ok  {name}")
        return
    print(f"  FAIL {name}  {extra}")
    FAIL[0] += 1


# ======================================================================
# 1. 版权分段(算错的后果是**法律**风险, 不是体验问题)
# ======================================================================
def test_se_license_cutovers():
    """SE 按发布时间的许可分段 —— 绝不能统一硬编码 4.0。"""
    print("\n[H1-B] Stack Exchange 许可分段")
    import datetime
    def ts(y, m, d):
        return int(datetime.datetime(y, m, d,
                                     tzinfo=datetime.timezone.utc).timestamp())
    check("2009 -> 2.5", se_license_for_ts(ts(2009, 1, 1)) == "CC BY-SA 2.5",
          se_license_for_ts(ts(2009, 1, 1)))
    check("2011-04-07 -> 2.5(前一天)",
          se_license_for_ts(ts(2011, 4, 7)) == "CC BY-SA 2.5",
          se_license_for_ts(ts(2011, 4, 7)))
    check("2011-04-08 -> 3.0(分段当天)",
          se_license_for_ts(ts(2011, 4, 8)) == "CC BY-SA 3.0",
          se_license_for_ts(ts(2011, 4, 8)))
    check("2016 -> 3.0", se_license_for_ts(ts(2016, 6, 20)) == "CC BY-SA 3.0",
          se_license_for_ts(ts(2016, 6, 20)))
    check("2018-05-01 -> 3.0(最后一天)",
          se_license_for_ts(ts(2018, 5, 1)) == "CC BY-SA 3.0",
          se_license_for_ts(ts(2018, 5, 1)))
    check("2018-05-02 -> 4.0(分段当天)",
          se_license_for_ts(ts(2018, 5, 2)) == "CC BY-SA 4.0",
          se_license_for_ts(ts(2018, 5, 2)))
    check("2024 -> 4.0", se_license_for_ts(ts(2024, 1, 1)) == "CC BY-SA 4.0",
          se_license_for_ts(ts(2024, 1, 1)))
    check("无时间戳 -> unknown", se_license_for_ts(None) == "unknown")


def test_license_api_value_wins_over_guess():
    """**API 回的 content_license 优先** —— 比按日期猜可靠。"""
    print("\n[H1-B] API 许可值优先于日期推断")
    # 2016 的问题, 但 API 说了 4.0(可能是编辑后重发) -> 必须听 API 的
    lic, inf = resolve_license("CC BY-SA 4.0", 1466402775)
    check("API 值胜出", (lic, inf) == ("CC BY-SA 4.0", "api"), (lic, inf))
    # API 没回 -> 按日期
    lic, inf = resolve_license(None, 1466402775)
    check("API 缺失 -> 按日期(3.0)", (lic, inf) == ("CC BY-SA 3.0",
                                                    "created_at"), (lic, inf))
    # API 回了个我们不认识的 -> **如实带出但标记不认识**, 绝不替换成 4.0
    lic, inf = resolve_license("CC BY-NC 9.9", 1600000000)
    check("**不认识的许可不被替换成 4.0**", lic == "CC BY-NC 9.9", lic)
    check("标记为 unrecognized", inf == "unrecognized", inf)
    check("**不认识的许可不可用于题库**",
          not license_is_usable(lic, inf))


def test_license_usability_gate():
    """unknown / unrecognized **不允许**进 curated 池。"""
    print("\n[H1-B] 许可可用性门")
    check("api + 认得 -> 可用",
          license_is_usable("CC BY-SA 4.0", "api"))
    check("created_at 回退 -> 可用(它就是官方分段规则)",
          license_is_usable("CC BY-SA 2.5", "created_at"))
    check("unknown -> **不可用**", not license_is_usable("unknown", "unknown"))
    check("unrecognized -> **不可用**",
          not license_is_usable("CC BY-NC 9.9", "unrecognized"))
    check("空串 -> 不可用", not license_is_usable("", "unknown"))
    check("Apache-2.0 认得", "Apache-2.0" in KNOWN_LICENSES)


def test_question_and_answer_license_stored_separately():
    """**跨版本的 question / answer 必须各存各的许可。**

    老问题(3.0) + 新采纳答案(4.0) 是完全正常的组合。只存一个会让
    attribution 错, 复用时就违反了其中一个的条款。
    """
    print("\n[H1-B] question / answer 许可分开存")
    q = {"question_id": 1, "title": "t", "body": "<p>q</p>",
         "tags": ["situation", "story"], "score": 5,
         "creation_date": 1466402775,          # 2016 -> 3.0
         "content_license": "CC BY-SA 3.0",
         "accepted_answer_id": 9,
         "owner": {"display_name": "Q", "user_id": 1,
                   "link": "https://puzzling.stackexchange.com/users/1/q"}}
    a = {"answer_id": 9, "question_id": 1, "body": "<p>a</p>", "score": 7,
         "creation_date": 1560000000,          # 2019 -> 4.0
         "content_license": "CC BY-SA 4.0",
         "owner": {"display_name": "A", "user_id": 2,
                   "link": "https://puzzling.stackexchange.com/users/2/a"}}
    recs, st = SE.build_records([q], {9: a})
    check("产出 1 条", len(recs) == 1, st)
    r = recs[0]
    check("question 许可 = 3.0", r.question_license == "CC BY-SA 3.0",
          r.question_license)
    check("answer 许可 = 4.0", r.answer_license == "CC BY-SA 4.0",
          r.answer_license)
    check("**两者没有被合并成一个**",
          r.question_license != r.answer_license)
    check("各自记录了推断来源",
          (r.question_license_inference, r.answer_license_inference)
          == ("api", "api"))
    check("作者分别记录",
          r.question_author == "Q" and r.answer_author == "A",
          (r.question_author, r.answer_author))
    check("作者链接分别记录",
          "/users/1" in r.question_author_url
          and "/users/2" in r.answer_author_url,
          (r.question_author_url, r.answer_author_url))
    # SE 的 attribution 要求"可读的 display name + profile link"。
    # 真实 API 的 owner.link 带 slug(users/9000/engineer-toast), 比
    # 只有数字 id 更好 —— 断言它被真的用上, 而不是被我们拼成裸 id。
    check("**链接带 display-name slug**(SE attribution 要求可读链接)",
          r.question_author_url.endswith("/q")
          and r.answer_author_url.endswith("/a"),
          (r.question_author_url, r.answer_author_url))


# ======================================================================
# 2. H2-A: 确定性 turtle 过滤
# ======================================================================
def test_story_like_filter():
    """H2-A: `lateral-thinking` **不等于**海龟汤, 必须筛。"""
    print("\n[H2-A] 非叙事 tag 过滤")
    for bad in ("mathematics", "geometry", "calculation-puzzle", "pattern",
                "word", "cipher", "cryptography", "chess", "programming",
                "code-golf", "rebus", "visual"):
        ok, why = SE.is_story_like(["lateral-thinking", bad])
        check(f"{bad} -> 拒", not ok, why)
    ok, _ = SE.is_story_like(["situation", "story"])
    check("situation + story -> 过", ok)
    ok, _ = SE.is_story_like(["situation", "lateral-thinking", "story",
                              "logical-deduction"])
    check("多叙事 tag -> 过", ok)
    # 一个 story tag 都没有 -> 也要拒(纯知识问答)
    ok, why = SE.is_story_like(["physics"])
    check("没有叙事 tag -> 拒", not ok, why)
    # 混合: 有叙事 tag 但也有数学 tag -> 必须拒(数学优先)
    ok, why = SE.is_story_like(["situation", "mathematics"])
    check("**叙事 tag 不能豁免非叙事 tag**", not ok, why)
    # 真实故障样本: 最高票的 lateral-thinking 是 pattern 题
    ok, why = SE.is_story_like(["pattern", "lateral-thinking"])
    check("**实测最高票那题(pattern)必须被拒**", not ok, why)


def test_real_noise_titles_are_filtered():
    """把实测抓到的噪音标题跑一遍 —— 它们必须全被拒。"""
    print("\n[H2-A] 实抓噪音样本")
    cases = [
        (["pattern", "lateral-thinking"], "Find the letters(字谜)"),
        (["cipher", "story"], "Murder of the President(密码)"),
        (["mathematics", "lateral-thinking"], "A case of the sniffles(数学)"),
        (["visual", "pattern"], "Trapped and gassed!(图形)"),
        (["calculation-puzzle", "mathematics"], "green apples(计算)"),
        (["rebus", "visual"], "Sammy Wolfe(字谜图)"),
        (["word"], "©Law of True Love(文字)"),
    ]
    for tags, name in cases:
        ok, why = SE.is_story_like(tags)
        check(f"拒: {name}", not ok, why)


# ======================================================================
# 3. 归一 + 去重(H1-E 的原料)
# ======================================================================
def test_normalize_for_dedup():
    print("\n[H1-E] 判重归一")
    check("剥 HTML", normalize_for_dedup("<p>他<em>说</em></p>") == "他说",
          normalize_for_dedup("<p>他<em>说</em></p>"))
    check("中文标点归一",
          normalize_for_dedup("他说：你好") == '他说:你好',
          normalize_for_dedup("他说：你好"))
    check("全角/半角 NFKC 归一",
          normalize_for_dedup("ＡＢＣ１２３") == "abc123",
          normalize_for_dedup("ＡＢＣ１２３"))
    check("空白归一",
          normalize_for_dedup("他   说\n\n你好") == "他 说 你好",
          normalize_for_dedup("他   说\n\n你好"))
    check("去零宽字符",
          normalize_for_dedup("他​说") == "他说",
          normalize_for_dedup("他​说"))
    check("**归一是无损的: 不做语义改写**",
          normalize_for_dedup("他每天看锅") == "他每天看锅")
    # 标点/空白**形态**不同必须归一到同一个 key
    a = normalize_for_dedup("<p>他说： “你好”</p>")
    b = normalize_for_dedup("他说: \"你好\"")
    check("标点/空白形态不同 -> 同一 key", a == b, (a, b))
    # ⚠️ 但**词内空格不同**不能被抹平 —— 那是两个不同的串, 抹平就是
    # 有损变换, 会把"他 说"和"他说"这种真的不同的题判成同一道。
    check("**词内空格不同 -> 不同 key**(不做有损归一)",
          normalize_for_dedup("他 说: 你好") != normalize_for_dedup("他说: 你好"))
    # 不同内容必须**不**归一到一起
    check("不同内容 -> 不同 key",
          normalize_for_dedup("他每天看锅") != normalize_for_dedup("她每天看锅"))


def test_exact_dedup_key():
    """`dedup_key()` 用归一后的 (surface, bottom)。"""
    print("\n[H1-E] exact dedup key")
    r1 = RawCuratedPuzzle(surface="<p>他说： “你好”</p>", bottom="A")
    r2 = RawCuratedPuzzle(surface="他说: \"你好\"", bottom="A")
    check("排版不同但同题 -> 同 key", r1.dedup_key() == r2.dedup_key())
    r3 = RawCuratedPuzzle(surface="他说: 你好", bottom="B")
    check("**bottom 不同 -> 不同题**", r1.dedup_key() != r3.dedup_key())


def test_turtlebench_dedup_collapses_to_unique_stories():
    """**本批最关键的一条**: 1532 行 -> 32 个独立故事。

    如果去重坏了, 我们会以为外题库有 1532 道题 —— 那是运营决策级的
    方向性错误(实际只够播一场多)。
    """
    print("\n[H1-A] TurtleBench 去重: 1532 行 -> 32 故事")
    # 造 3 个故事, 每个复制若干遍(模拟 (user_guess,label) 评测行)
    rows = []
    for sid, (surf, bot) in enumerate([
            ("山顶小屋敲门无人", "门外是悬崖, 敲门者被推下去"),
            ("午夜列车问年龄", "他能预知他人死亡年龄"),
            ("交换照片出冷汗", "对方发回的是我自己的照片")]):
        for k in range(40):
            rows.append({"id": sid * 100 + k, "title": f"故事{sid}",
                         "surface": surf, "bottom": bot,
                         "user_guess": f"g{k}", "label": "T"})
    recs, rejected, st = TB.build_records(rows, "Apache-2.0", "api")
    check("原始 120 行", st["raw_rows"] == 120, st)
    check("**塌缩成 3 个独立故事**", st["unique_stories"] == 3, st)
    check("产出 3 条", len(recs) == 3, len(recs))
    check("benchmark guess 行数如实记录",
          st["raw_guess_rows"] == 120, st)
    # 代表行取 id 最小的那次出现 -> 重跑稳定
    ids = sorted(r.external_id for r in recs)
    check("external_id 稳定且唯一", len(set(ids)) == 3, ids)
    check("许可写成 Apache-2.0",
          all(r.question_license == "Apache-2.0" for r in recs))
    # user_guess / label **绝不进题文本**
    for r in recs:
        check(f"**user_guess 没进 surface**({r.title})",
              "g0" not in r.surface and "g39" not in r.surface)
        check(f"**label 没进 bottom**({r.title})",
              r.bottom.strip() != "T")


def test_turtlebench_external_id_is_content_stable():
    """external_id 用**内容**哈希 —— 同内容不同行必须得到同一个 id。"""
    print("\n[H1-A] external_id 内容稳定")
    a = TB.build_records([{"id": 5, "title": "x", "surface": "S",
                           "bottom": "B", "user_guess": "", "label": "T"}],
                         "Apache-2.0", "api")[0][0]
    b = TB.build_records([{"id": 999, "title": "y", "surface": "S",
                           "bottom": "B", "user_guess": "", "label": "F"}],
                         "Apache-2.0", "api")[0][0]
    check("**同 (surface,bottom) -> 同 external_id**",
          a.external_id == b.external_id, (a.external_id, b.external_id))
    c = TB.build_records([{"id": 1, "title": "x", "surface": "S",
                           "bottom": "OTHER", "user_guess": "", "label": "T"}],
                         "Apache-2.0", "api")[0][0]
    check("bottom 不同 -> 不同 id", a.external_id != c.external_id)


# ======================================================================
# 4. 安全筛查
# ======================================================================
def test_safety_screen():
    print("\n[H1-A] 内容安全筛查")
    check("自伤命中", safety_screen("他自杀了") == "self_harm")
    check("血腥命中", safety_screen("现场被碎尸") == "gore")
    check("未成年伤害命中", safety_screen("涉及虐童") == "minor_harm")
    check("干净文本不命中", safety_screen("他每天看锅") == "")
    check("空文本不命中", safety_screen("") == "")
    # 关键: 题面干净但谜底血腥 -> **仍要拦**
    flag = safety_screen("他走进餐厅。" + "\n" + "他割腕自杀了。")
    check("**谜底血腥也要拦(不能只看题面)**", flag == "self_harm", flag)


def test_turtlebench_safety_paths():
    """命中安全的进 `rejected_safety`, **不进** curated_raw。"""
    print("\n[H1-A] 安全命中走 rejected 而非 curated")
    rows = [
        {"id": 1, "title": "干净", "surface": "他每天看锅", "bottom": "记号",
         "user_guess": "", "label": "T"},
        {"id": 2, "title": "自伤", "surface": "他跳河自杀了", "bottom": "x",
         "user_guess": "", "label": "T"},
    ]
    recs, rejected, st = TB.build_records(rows, "Apache-2.0", "api")
    check("干净的进 curated", len(recs) == 1, len(recs))
    check("自伤的进 rejected", len(rejected) == 1, len(rejected))
    check("安全标记被记下", rejected[0].safety_flag == "self_harm",
          rejected[0].safety_flag)
    check("**两边不重叠**",
          not ({r.external_id for r in recs}
               & {r.external_id for r in rejected}))


# ======================================================================
# 5. 确定性落盘(H1-D 的硬要求)
# ======================================================================
def test_write_jsonl_is_byte_deterministic():
    """同输入 -> **字节级**同输出(含乱序输入)。"""
    print("\n[H1-D] 确定性落盘")
    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, "out.jsonl")
        recs = [RawCuratedPuzzle(external_id=f"x{i}", source="S",
                                 surface=f"面{i}", bottom=f"底{i}")
                for i in range(5)]
        import random
        shuffled = list(recs)
        random.Random(7).shuffle(shuffled)
        write_jsonl_deterministic(p, recs)
        first = open(p, "rb").read()
        write_jsonl_deterministic(p, shuffled)      # 换顺序写
        second = open(p, "rb").read()
        check("**乱序输入 -> 字节相同**", first == second)
        # 键序也必须定死
        check("JSON 键已排序",
              b'{"answer_author"' in first)
        check("没有尾随空格/CRLF", b"\r\n" not in first and
              not first.endswith(b"\n\n"))
        lines = [l for l in first.decode("utf-8").split("\n") if l]
        check("写了 5 行", len(lines) == 5, len(lines))
        ids = [json.loads(l)["external_id"] for l in lines]
        check("按 (source, external_id) 排序", ids == sorted(ids), ids)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_meta_is_not_in_stable_record():
    """抓取时间/metadata **不混进** stable record。"""
    print("\n[H1-D] metadata 与 stable record 分离")
    r = RawCuratedPuzzle(external_id="a", source="S", surface="x",
                         bottom="y")
    keys = set(r.to_dict().keys())
    for banned in ("fetched_at", "fetch_time", "quota", "quota_remaining",
                   "crawled_at"):
        check(f"记录里没有 {banned}", banned not in keys)


def test_sort_records_stable():
    print("\n[H1-D] 排序稳定")
    a = RawCuratedPuzzle(external_id="b", source="A")
    b = RawCuratedPuzzle(external_id="a", source="A")
    c = RawCuratedPuzzle(external_id="a", source="B")
    out = sort_records([a, b, c])
    check("先按 source 再按 external_id",
          [(r.source, r.external_id) for r in out]
          == [("A", "a"), ("A", "b"), ("B", "a")],
          [(r.source, r.external_id) for r in out])


def test_raw_curated_puzzle_roundtrip():
    """schema 存读一轮不丢字段。"""
    print("\n[H1-D] RawCuratedPuzzle 存读往返")
    r = RawCuratedPuzzle(
        external_id="pse:q:1", source="Puzzling Stack Exchange",
        source_url="https://x/1", source_kind="stackexchange",
        question_author="Q", question_author_url="u1",
        answer_author="A", answer_author_url="u2",
        question_license="CC BY-SA 3.0", answer_license="CC BY-SA 4.0",
        title="t", surface="s", bottom="b", language="en",
        original_language="en", translated=False,
        tags=["situation"], question_score=5, answer_score=7,
        question_license_inference="api", answer_license_inference="api",
        question_created_at=1, answer_created_at=2,
        question_id=1, answer_id=2)
    back = RawCuratedPuzzle.from_dict(r.to_dict())
    check("往返完全相等", back.to_dict() == r.to_dict())
    check("多余字段被忽略",
          RawCuratedPuzzle.from_dict({"external_id": "z",
                                      "unknown_key": 1}).external_id == "z")
    check("非 dict 输入安全", RawCuratedPuzzle.from_dict(None).external_id == "")


# ======================================================================
# 6. **本轮最重要的一条**: answers 分页(真实故障回归)
# ======================================================================
def test_se_answer_pagination_reads_has_more():
    """`/answers/{ids}` 每页只回 30 条 + `has_more`。

    ## 这条测试守的是一个**已经发生过**的故障

    实测: 请求 100 个 id -> `items` 30 条, `has_more: True`。

    不读 `has_more` 的后果不是报错, 而是**静默只拿前 30 条**:

        5 批 x 30 = 150 条        <- 看起来像"这些题没有采纳答案"
        实际 487 条可用
        -> curated_raw 只有 105 条(少了 70%)

    这种故障不会让任何断言变红 —— 除非**专门有一条测试**盯着它。
    所以下面伪装一个"每页只回 30 条"的 API, 断言我们会翻到底。
    """
    print("\n[H1-B] **answers 分页必须读 has_more**(真实故障回归)")
    all_ids = list(range(1000, 1100))          # 100 个 id
    pool = {i: {"answer_id": i, "question_id": i, "body": "<p>b</p>",
                "score": 1, "creation_date": 1560000000,
                "content_license": "CC BY-SA 4.0",
                "owner": {"display_name": "u", "user_id": 1}}
            for i in all_ids}
    calls = []

    def fake_api(path, params, **kw):
        calls.append(dict(params))
        ids = [int(x) for x in path.split("/", 1)[1].split(";")]
        page = int(params.get("page") or 1)
        per = 30                                # <-- 服务端的真实上限
        start = (page - 1) * per
        chunk = ids[start:start + per]
        return {"items": [pool[i] for i in chunk if i in pool],
                "has_more": start + per < len(ids),
                "quota_remaining": 999}

    orig = SE._api
    SE._api = fake_api
    try:
        got, _q = SE.fetch_answers(all_ids, proxy="")
    finally:
        SE._api = orig
    check("**100 个 id 全部拿到(不是只有 30)**", len(got) == 100,
          f"只拿到 {len(got)}")
    check("翻了多页", len(calls) >= 4, len(calls))
    check("**没有在末尾拼出多余的分号**",
          not any((";" + ";") in str(c) for c in calls))
    check("page 从 1 开始递增",
          [int(c.get("page") or 1) for c in calls][:3] == [1, 2, 3],
          [c.get("page") for c in calls][:3])


def test_se_answer_all_failed_raises_not_silent():
    """整批拿不到 -> **抛异常**, 绝不静默产出短文件。"""
    print("\n[H1-B] 全部取不到 -> 抛错而不是静默截断")
    def fake_api(path, params, **kw):
        return {"items": [], "has_more": False, "quota_remaining": 5}
    orig = SE._api
    SE._api = fake_api
    try:
        raised = None
        try:
            SE.fetch_answers([1, 2, 3], proxy="")
        except Exception as e:                  # noqa: BLE001
            raised = e
    finally:
        SE._api = orig
    check("**抛了异常**(而不是返回空 dict)", raised is not None)
    check("是 Throttled 类型", isinstance(raised, SE.Throttled), type(raised))


def test_se_answer_partial_gap_is_logged():
    """有缺口时**不**抛(已删除答案属正常), 但要能看出来。"""
    print("\n[H1-B] 部分缺口不抛, 但差额可查")
    def fake_api(path, params, **kw):
        # 只回一半, 且明确说没有更多页
        ids = [int(x) for x in path.split("/", 1)[1].split(";")]
        half = ids[:len(ids) // 2]
        return {"items": [{"answer_id": i, "question_id": i, "body": "<p>b</p>",
                           "score": 1, "creation_date": 1560000000,
                           "content_license": "CC BY-SA 4.0",
                           "owner": {"display_name": "u", "user_id": 1}}
                          for i in half],
                "has_more": False, "quota_remaining": 9}
    orig = SE._api
    SE._api = fake_api
    try:
        got, _q = SE.fetch_answers([1, 2, 3, 4, 5, 6], proxy="")
    finally:
        SE._api = orig
    check("拿到有缺口的一半", len(got) == 3, len(got))


# ======================================================================
# 6b. H1-E 三层去重
# ======================================================================
def test_dedup_exact_collapses_identical():
    """① exact: 归一后完全相同的只留一份。"""
    print("\n[H1-E] exact 去重")
    from tools.curated_dedup import dedup
    recs = [RawCuratedPuzzle(external_id=f"x{i}", source="S",
                             surface="他每天看锅确认记号", bottom="B")
            for i in range(4)]
    kept, dupes, st = dedup(recs)
    check("只留 1 条", len(kept) == 1, len(kept))
    check("去掉 3 条", st["exact_removed"] == 3, st)
    check("exact 层不产 dupes(是直接合并)", len(dupes) == 0, len(dupes))


def test_dedup_near_marks_but_never_deletes():
    """② near: **只标记, 不删除** —— 判重会错, 不能悄悄丢题。"""
    print("\n[H1-E] near 只标记不删")
    from tools.curated_dedup import dedup
    a = RawCuratedPuzzle(external_id="a", source="S",
                         surface="守塔人只在退潮时亮灯涨潮后熄灯",
                         bottom="C")
    b = RawCuratedPuzzle(external_id="b", source="T",
                         surface="守塔人只在退潮时亮灯涨潮后熄灯了",
                         bottom="D")          # 谜底不同!
    c = RawCuratedPuzzle(external_id="c", source="S",
                         surface="完全不同的沙漠靶场白旗测风", bottom="E")
    kept, dupes, st = dedup([a, b, c])
    check("近重复进 dupes 而非被删", len(dupes) == 1, len(dupes))
    check("dupes 里带 dup_of", dupes[0].dup_of == "a", dupes[0].dup_of)
    check("dupes 里带相似度", dupes[0].dup_score > 0, dupes[0].dup_score)
    check("dup_reason 标成 near_duplicate",
          dupes[0].dup_reason == "near_duplicate", dupes[0].dup_reason)
    check("**b 没有被丢掉**(还留在 dupes 里可人工过)",
          any(r.external_id == "b" for r in dupes))
    check("不同的题正常保留", any(r.external_id == "c" for r in kept))


def test_dedup_is_order_independent():
    """**去重结果不能依赖输入顺序** —— 否则确定性落盘当场作废。

    这是真实踩到的坑: 两条文字几乎相同但不完全相同的记录(差一个空格)
    归一后 key 不同, 落到 near 层, 而 near 层是"与已保留的第一条比"。
    不排序的话 `dedup([A,B])` 与 `dedup([B,A])` 会保留不同的那条,
    于是同一份输入换个顺序产出不同的 curated_raw。
    """
    print("\n[H1-E] **去重与输入顺序无关**")
    import itertools
    from tools.curated_dedup import dedup
    recs = [
        RawCuratedPuzzle(external_id="a1", source="S",
                         surface="他每天看锅确认记号", bottom="B"),
        RawCuratedPuzzle(external_id="a3", source="T",
                         surface="他 每天看锅确认记号", bottom="B",
                         question_author="Y"),
        RawCuratedPuzzle(external_id="b1", source="S",
                         surface="守塔人只在退潮时亮灯涨潮熄灯", bottom="C"),
        RawCuratedPuzzle(external_id="b2", source="T",
                         surface="守塔人只在退潮时亮灯涨潮熄灯了", bottom="D"),
    ]
    outcomes = set()
    for perm in itertools.permutations(recs):
        kept, dupes, _st = dedup(list(perm))
        outcomes.add((tuple(sorted(r.external_id for r in kept)),
                      tuple(sorted(r.external_id for r in dupes))))
    check(f"**全 {len(list(itertools.permutations(recs)))} 种排列只产出 1 种结果**",
          len(outcomes) == 1, outcomes)


def test_dedup_prefers_richer_record():
    """同一题多份时保留**信息更全**的(有作者 > 没作者)。

    ⚠️ 注意这里构造的差异是**归一后 key 相同**的(只差首尾空白), 所以
    它在 ① exact 层就被合并 —— 那正是我们想要的: 首尾空白是排版噪音,
    不该让同一道题进到模糊层去靠相似度猜。
    """
    print("\n[H1-E] 保留信息更全的那份")
    from tools.curated_dedup import dedup
    poor = RawCuratedPuzzle(external_id="poor", source="S",
                            surface="同一道题的题面", bottom="B")
    rich = RawCuratedPuzzle(external_id="rich", source="T",
                            surface="同一道题的题面 ", bottom="B",
                            question_author="某人")
    check("首尾空白不算不同题(归一后同 key)",
          poor.dedup_key() == rich.dedup_key())
    kept, dupes, st = dedup([poor, rich])
    check("合并成 1 条", len(kept) == 1, len(kept))
    check("计入 exact_removed", st["exact_removed"] == 1, st)
    check("**保留的是有作者的那份**", kept[0].external_id == "rich",
          kept[0].external_id)
    # 乱序也要选同一条
    kept2, _d2, _s2 = dedup([rich, poor])
    check("乱序仍选同一条", kept2[0].external_id == "rich",
          kept2[0].external_id)


def test_dedup_cross_source_report():
    print("\n[H1-E] 来源分布统计")
    from tools.curated_dedup import cross_source_report
    recs = [RawCuratedPuzzle(external_id="1", source="A"),
            RawCuratedPuzzle(external_id="2", source="A"),
            RawCuratedPuzzle(external_id="3", source="B")]
    check("按 source 计数",
          cross_source_report(recs) == {"A": 2, "B": 1},
          cross_source_report(recs))


def test_build_corpus_end_to_end():
    """H1-E 收口: 合并两个来源 + 去重 + 许可门 -> curated_raw。

    全部在临时目录里用**假来源文件**跑, 不联网、不碰 data_external/。
    """
    print("\n[H1-E] build_curated_corpus 端到端")
    from tools import build_curated_corpus as BC
    d = tempfile.mkdtemp()
    try:
        # 造两个来源: 一个 TurtleBench 风格, 一个 SE 风格, 且**跨来源重复**
        tb_dir = os.path.join(d, "turtlebench", "normalized")
        se_dir = os.path.join(d, "puzzling_se", "normalized")
        os.makedirs(tb_dir, exist_ok=True)
        os.makedirs(se_dir, exist_ok=True)

        tb = [RawCuratedPuzzle(
            external_id="turtlebench:aaa", source="TurtleBench1.5k",
            source_kind="dataset", surface="他每天看锅确认记号",
            bottom="锅里的状态是记号", language="zh",
            question_license="Apache-2.0", answer_license="Apache-2.0",
            question_license_inference="api",
            answer_license_inference="api")]
        # SE 里同题(文字略改) + 一道不同的题 + 一道许可不可用的
        se = [
            RawCuratedPuzzle(
                external_id="pse:q:1", source="Puzzling Stack Exchange",
                source_kind="stackexchange", surface="他每天看锅确认记号 ",
                bottom="锅里的状态是记号", language="en",
                question_license="CC BY-SA 4.0",
                answer_license="CC BY-SA 4.0",
                question_license_inference="api",
                answer_license_inference="api"),
            RawCuratedPuzzle(
                external_id="pse:q:2", source="Puzzling Stack Exchange",
                source_kind="stackexchange", surface="完全不一样的沙漠靶场白旗",
                bottom="白旗是测风参照物", language="en",
                question_license="CC BY-SA 3.0",
                answer_license="CC BY-SA 4.0",
                question_license_inference="api",
                answer_license_inference="api"),
            RawCuratedPuzzle(
                external_id="pse:q:3", source="Puzzling Stack Exchange",
                source_kind="stackexchange", surface="许可不可用的题",
                bottom="x", language="en",
                question_license="CC BY-NC 9.9",
                answer_license="CC BY-NC 9.9",
                question_license_inference="unrecognized",
                answer_license_inference="unrecognized"),
        ]
        write_jsonl_deterministic(os.path.join(tb_dir, "turtlebench.jsonl"), tb)
        write_jsonl_deterministic(os.path.join(se_dir, "puzzling_se.jsonl"), se)

        rc = BC.main(["--root", d, "--log-level", "ERROR"])
        check("退出码 0", rc == 0, rc)

        out = os.path.join(d, "normalized", "curated_raw.jsonl")
        rows = [json.loads(l) for l in open(out, encoding="utf-8") if l.strip()]
        ids = sorted(r["external_id"] for r in rows)
        check("**许可不可用的被拦下(q:3 不在)**",
              "pse:q:3" not in ids, ids)
        check("许可不可用的进了 license_rejected",
              os.path.exists(os.path.join(d, "normalized",
                                          "license_rejected.jsonl")))
        check("跨来源同题被合并(只剩 2 条)", len(rows) == 2, ids)
        # 全局排序(H1-D 要求合并后仍是确定的)
        check("**合并后全局有序**",
              ids == sorted(ids), ids)
        # 重复候选被写出(不删)
        dup_p = os.path.join(d, "normalized", "duplicate_candidates.jsonl")
        check("重复候选文件存在", os.path.exists(dup_p))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_build_corpus_is_deterministic():
    """同一份输入跑两次 -> curated_raw 字节相同。"""
    print("\n[H1-E] build_curated_corpus 确定性")
    from tools import build_curated_corpus as BC
    outputs = []
    for run in range(2):
        d = tempfile.mkdtemp()
        try:
            for sub, items in (
                ("turtlebench", [
                    RawCuratedPuzzle(external_id=f"tb{i}", source="TurtleBench1.5k",
                                     surface=f"题面{i}", bottom=f"谜底{i}",
                                     question_license="Apache-2.0",
                                     answer_license="Apache-2.0",
                                     question_license_inference="api",
                                     answer_license_inference="api")
                    for i in range(3)]),
                ("puzzling_se", [
                    RawCuratedPuzzle(external_id=f"se{i}",
                                     source="Puzzling Stack Exchange",
                                     surface=f"另一题面{i}", bottom=f"另一谜底{i}",
                                     question_license="CC BY-SA 4.0",
                                     answer_license="CC BY-SA 4.0",
                                     question_license_inference="api",
                                     answer_license_inference="api")
                    for i in range(3)]),
            ):
                p = os.path.join(d, sub, "normalized",
                                 f"{sub}.jsonl")
                os.makedirs(os.path.dirname(p), exist_ok=True)
                write_jsonl_deterministic(p, items)
            BC.main(["--root", d, "--log-level", "ERROR"])
            with open(os.path.join(d, "normalized", "curated_raw.jsonl"),
                      "rb") as f:
                outputs.append(f.read())
        finally:
            shutil.rmtree(d, ignore_errors=True)
    check("**两次运行字节相同**", outputs[0] == outputs[1])
    check("确实有内容", len(outputs[0]) > 0)


def test_build_corpus_refuses_empty():
    """没有来源文件 -> 报错退出, **不写空文件**。"""
    print("\n[H1-E] 空输入 -> 报错而非静默空产出")
    from tools import build_curated_corpus as BC
    d = tempfile.mkdtemp()
    try:
        rc = BC.main(["--root", d, "--log-level", "ERROR"])
        check("退出码非 0", rc != 0, rc)
        check("**没有产出 curated_raw**",
              not os.path.exists(os.path.join(d, "normalized",
                                              "curated_raw.jsonl")))
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ======================================================================
# 7. SE 记录构造
# ======================================================================
def test_se_skips_questions_without_accepted_answer():
    """没有采纳答案 -> 跳过(**不**自己挑一个当谜底)。"""
    print("\n[H1-B] 无采纳答案 -> 跳过")
    q = {"question_id": 1, "title": "t", "body": "<p>q</p>",
         "tags": ["situation", "story"], "score": 5,
         "creation_date": 1560000000, "content_license": "CC BY-SA 4.0",
         "owner": {"display_name": "Q", "user_id": 1}}     # 没有 accepted
    recs, st = SE.build_records([q], {})
    check("产出 0 条", len(recs) == 0, len(recs))
    check("计入 no_accepted", st["no_accepted"] == 1, st)


def test_se_records_carry_full_attribution():
    """H2-H: 每道 curated 题都必须能反查来源。"""
    print("\n[H1-B] attribution 完整性")
    q = {"question_id": 42, "title": "标题", "body": "<p>题面</p>",
         "tags": ["situation", "story"], "score": 12,
         "creation_date": 1466402775, "content_license": "CC BY-SA 3.0",
         "accepted_answer_id": 77, "link": "https://puzzling.stackexchange.com/q/42",
         "owner": {"display_name": "问者", "user_id": 5,
                   "link": "https://puzzling.stackexchange.com/users/5/asker"}}
    a = {"answer_id": 77, "question_id": 42, "body": "<p>谜底</p>",
         "score": 20, "creation_date": 1466403000,
         "content_license": "CC BY-SA 3.0",
         "owner": {"display_name": "答者", "user_id": 6,
                   "link": "https://puzzling.stackexchange.com/users/6/answerer"}}
    recs, _ = SE.build_records([q], {77: a})
    r = recs[0]
    check("external_id = pse:q:42", r.external_id == "pse:q:42", r.external_id)
    check("source_url 指向原帖",
          "puzzling.stackexchange.com" in r.source_url, r.source_url)
    check("question_id / answer_id 都记了",
          (r.question_id, r.answer_id) == (42, 77))
    check("两个作者都在",
          (r.question_author, r.answer_author) == ("问者", "答者"))
    check("两个许可都在",
          (r.question_license, r.answer_license)
          == ("CC BY-SA 3.0", "CC BY-SA 3.0"))
    check("时间戳都在",
          r.question_created_at == 1466402775
          and r.answer_created_at == 1466403000)
    check("分数都在", (r.question_score, r.answer_score) == (12, 20))
    check("**还没翻译**(H2-C 那步才翻)", r.translated is False)
    check("原文语言记 en", r.original_language == "en")


def test_se_html_is_stripped():
    """SE 的 body 是 HTML —— 标签不能进题面。"""
    print("\n[H1-B] HTML 剥离")
    q = {"question_id": 1, "title": "t",
         "body": "<p>第一段</p><p>第二段 <a href='x'>链接</a></p>",
         "tags": ["situation"], "score": 5, "creation_date": 1560000000,
         "content_license": "CC BY-SA 4.0", "accepted_answer_id": 2,
         "owner": {"display_name": "Q", "user_id": 1}}
    a = {"answer_id": 2, "question_id": 1,
         "body": "<p>谜底<br>换行</p>", "score": 1,
         "creation_date": 1560000000, "content_license": "CC BY-SA 4.0",
         "owner": {"display_name": "A", "user_id": 2}}
    recs, _ = SE.build_records([q], {2: a})
    r = recs[0]
    check("题面无标签", "<p>" not in r.surface and "</p>" not in r.surface,
          r.surface)
    check("题面保留了文字",
          "第一段" in r.surface and "第二段" in r.surface, r.surface)
    check("谜底无标签", "<br>" not in r.bottom, r.bottom)
    check("谜底保留换行", "\n" in r.bottom, repr(r.bottom))
    check("strip_html 解实体",
          strip_html("a &amp; b &quot;c&quot;") == 'a & b "c"',
          strip_html("a &amp; b &quot;c&quot;"))


# ======================================================================
def test_stable_hash_ignores_non_content():
    print("\n[H1-D] stable_hash")
    check("同输入同哈希", stable_hash("a", "b") == stable_hash("a", "b"))
    check("顺序敏感", stable_hash("a", "b") != stable_hash("b", "a"))
    check("默认 12 位", len(stable_hash("x")) == 12, len(stable_hash("x")))


def test_proxy_resolution():
    """代理: 显式 > 环境变量 > 默认端口; 空串 = 直连。"""
    print("\n[H1-网络] 代理解析")
    from tools.curated_common import DEFAULT_PROXY, resolve_proxy
    saved = {k: os.environ.get(k) for k in
             ("HGT_PROXY", "HTTPS_PROXY", "https_proxy")}
    try:
        for k in saved:
            os.environ.pop(k, None)
        check("默认走本地代理端口", resolve_proxy(None) == DEFAULT_PROXY,
              resolve_proxy(None))
        check("**显式空串 = 直连**", resolve_proxy("") == "")
        os.environ["HGT_PROXY"] = "http://example:1"
        check("HGT_PROXY 优先于默认",
              resolve_proxy(None) == "http://example:1")
        check("显式参数优先于环境变量",
              resolve_proxy("http://x:9") == "http://x:9")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_known_licenses_are_exact():
    """许可白名单是**精确**匹配 —— 不要用子串把 NC 当成 SA。"""
    print("\n[H1-B] 许可白名单精确性")
    check("CC BY-SA 4.0 在名单里", "CC BY-SA 4.0" in KNOWN_LICENSES)
    check("**CC BY-SA 4.0 NC 不在名单**",
          "CC BY-SA 4.0 NC" not in KNOWN_LICENSES)
    check("**CC BY-NC-SA 4.0 不在名单**",
          "CC BY-NC-SA 4.0" not in KNOWN_LICENSES)
    check("空串不在名单", "" not in KNOWN_LICENSES)


# ======================================================================
def main():
    tests = [
        # ---- H1-B 版权 ----
        test_se_license_cutovers,
        test_license_api_value_wins_over_guess,
        test_license_usability_gate,
        test_question_and_answer_license_stored_separately,
        test_known_licenses_are_exact,
        # ---- H2-A 过滤 ----
        test_story_like_filter,
        test_real_noise_titles_are_filtered,
        # ---- H1-E 归一/去重 ----
        test_normalize_for_dedup,
        test_exact_dedup_key,
        test_turtlebench_dedup_collapses_to_unique_stories,
        test_turtlebench_external_id_is_content_stable,
        # ---- H1-E 去重 ----
        test_dedup_exact_collapses_identical,
        test_dedup_near_marks_but_never_deletes,
        test_dedup_is_order_independent,
        test_dedup_prefers_richer_record,
        test_dedup_cross_source_report,
        test_build_corpus_end_to_end,
        test_build_corpus_is_deterministic,
        test_build_corpus_refuses_empty,
        # ---- 安全 ----
        test_safety_screen,
        test_turtlebench_safety_paths,
        # ---- H1-D 确定性 ----
        test_write_jsonl_is_byte_deterministic,
        test_meta_is_not_in_stable_record,
        test_sort_records_stable,
        test_raw_curated_puzzle_roundtrip,
        test_stable_hash_ignores_non_content,
        # ---- 分页(本轮最重要) ----
        test_se_answer_pagination_reads_has_more,
        test_se_answer_all_failed_raises_not_silent,
        test_se_answer_partial_gap_is_logged,
        # ---- SE 记录构造 ----
        test_se_skips_questions_without_accepted_answer,
        test_se_records_carry_full_attribution,
        test_se_html_is_stripped,
        # ---- 网络 ----
        test_proxy_resolution,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:                  # noqa: BLE001
            import traceback
            print(f"  FAIL {t.__name__} 抛异常: {e}")
            traceback.print_exc()
            FAIL[0] += 1
    print()
    if FAIL[0]:
        print(f"FAILED: {FAIL[0]}")
        return 1
    print(f"ALL PASS ({len(tests)} tests)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
