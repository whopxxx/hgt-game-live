#!/usr/bin/env python
# coding: utf-8
"""运行: uv run tests/test_protocol_evidence.py（完全离线, 无网络）。

Step 12A: 守住「外部证据」这份文件的**性质**。

它唯一容易出的事故是: 后来的模型为了让 Step 13 好写, 往里面**补一条
看起来像实测的时间序列**(例如 `{combo_count: 1 -> 2 -> 3}`) —— 那会把
猜测洗成证据, 然后被下游当成事实消费。这个套件就是防这个。

它同时钉住: 我们文件里**声称的字段编号**真的与本地 proto 一致 ——
免得文档抄错, 而抄错会让 Step 12B 采错列。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAIL = [0]
EVIDENCE = ROOT / "tests" / "fixtures" / \
    "interaction_protocol_external_evidence.json"
DOC = ROOT / "docs" / "interaction_protocol_characterization.md"
PROTO = ROOT / "vendor" / "douyin_fetcher" / "protobuf" / "douyin.proto"


def check(name, cond, extra=""):
    if cond:
        print(f"  ok  {name}")
        return
    print(f"  FAIL {name}  {extra}")
    FAIL[0] += 1


def load() -> dict:
    return json.loads(EVIDENCE.read_text(encoding="utf-8"))


# ======================================================================
def test_evidence_file_is_valid_and_complete():
    print("\n[12A-1] 证据文件结构完整")
    d = load()
    for key in ("_README", "sources", "confirmed",
                "derived_conclusions", "unknown_pending_12b",
                "explicitly_not_recorded"):
        check(f"有 {key}", key in d, sorted(d))
    ids = {s["id"] for s in d["sources"]}
    check("三个来源都在",
          ids == {"our-proto", "external-impl", "external-issue-88"}, ids)
    # 每条 confirmed 都要有来源
    for c in d["confirmed"]:
        if c.get("source") not in ids:
            check(f"confirmed 有合法来源: {c.get('claim', '')[:30]}", False,
                  c.get("source"))
            break
    else:
        check("每条 confirmed 都指向已知来源", True)
    # 每条 derived 都要有依据
    for c in d["derived_conclusions"]:
        if not set(c.get("derives_from") or []) <= ids:
            check(f"derived 有合法依据: {c.get('conclusion', '')[:30]}",
                  False, c.get("derives_from"))
            break
    else:
        check("每条 derived 都指向已知来源", True)


def test_no_fabricated_time_series():
    """**核心**: 不得出现任何"看起来像实测序列"的**断言**。

    ⚠️ 不能简单地"出现 `1 -> 2 -> 3` 就红": 该串**会**出现在
    「禁止记录这种观察」的说明里(`explicitly_not_recorded`)与文档的
    否定句中。那些是**护栏**, 不是证据 —— 把它们判红会逼着人删掉护栏,
    正好反了。
    所以判据是: 序列**只能**出现在否定/禁止语境里, 不能出现在
    `confirmed` / `sources` / `derived_conclusions` 这些**断言区**。
    """
    print("\n[12A-2] 断言区不含伪造的时间序列")
    d = load()
    pat = re.compile(r"\d+\s*(?:->|→|=>)\s*\d+\s*(?:->|→|=>)")
    # 只在**断言区**查序列
    for key in ("confirmed", "sources", "derived_conclusions"):
        blob = json.dumps(d.get(key), ensure_ascii=False)
        hits = pat.findall(blob)
        check(f"{key} 里没有时间序列", not hits, hits)
    # ⚠️ 未知区**允许**出现 `1->2->3` —— 那是 U2 那个**问题本身**
    # ("combo 更新是否 1->2->3"), 是提问, 不是观察。所以这里不判它,
    # 只要求它确实是以问句形式存在。
    u_blob = json.dumps(d.get("unknown_pending_12b"), ensure_ascii=False)
    check("未知区把该序列作为**问题**提出",
          pat.search(u_blob) and "?" in u_blob, u_blob[:160])
    # 不该出现"我们观察到/采样到"这类**声称**
    raw = json.dumps(d, ensure_ascii=False)
    for bad in ("我们观察到", "实测得到", "采样得到", "observed_sequence",
                "sample_data"):
        check(f"没有声称 {bad!r}", bad not in raw, bad)
    # 明确登记了"没记什么"(护栏必须在)
    check("显式登记了未记录项",
          len(d.get("explicitly_not_recorded") or []) >= 2,
          d.get("explicitly_not_recorded"))
    check("未记录项里点名了 combo 序列",
          any("combo_count" in x for x in d["explicitly_not_recorded"]),
          d["explicitly_not_recorded"])


def test_unknown_list_is_preserved():
    """Step 12B 要回答的问题清单不能被悄悄删空(那等于假装已解决)。"""
    print("\n[12A-3] 未知项清单仍在")
    d = load()
    u = d.get("unknown_pending_12b") or []
    check("至少 6 条未知", len(u) >= 6, len(u))
    joined = json.dumps(u, ensure_ascii=False)
    for must in ("combo", "repeat", "total", "group_id", "trace_id", "msg_id"):
        check(f"未知项覆盖 {must}", must in joined, must)


def test_evidence_does_not_claim_gift_semantics():
    """**核心**: **断言区**不得给出 combo/repeat/total 的语义结论。

    同样不能做全文字符串匹配: 文档的否定句里**会**出现"不等于本消息新增"
    之类的措辞, 那是澄清, 不是断言。
    """
    print("\n[12A-4] 断言区未宣称 gift 增量语义")
    d = load()
    # ⚠️ 只查 **claim 正文**, 不查 note/claim 全文 —— 因为**澄清用的否定句**
    # 正是写在 note 里的(例如"这只说明它是显示用数量, **不**说明它等于本
    # 消息新增的礼物单位数")。把那些判红会逼着人删掉澄清, 正好反了。
    claims = " ".join(c.get("claim", "") for c in d.get("confirmed", []))
    for phrase in ("绝对值", "累计绝对", "新增礼物数", "等于本消息",
                   "单位数", "earned", "summon", "+1"):
        check(f"claim 正文里没有 {phrase!r}", phrase not in claims, phrase)
    # 澄清性 note 必须**仍然在**(它是防止误读的护栏)
    notes = " ".join(c.get("note", "") for c in d.get("confirmed", []))
    check("保留了『不足以说明等于新增』的澄清",
          "不" in notes and "新增" in notes, notes[:120])
    # unknown 区必须仍然明确列着那个悬而未决的问题
    u = json.dumps(d.get("unknown_pending_12b"), ensure_ascii=False)
    check("未知区仍列着『哪个是累计绝对量』",
          "绝对量" in u, u[:200])


def test_proto_field_numbers_match_local_proto():
    """文档/证据里写的字段编号必须与**本地 proto** 一致。

    抄错编号会让 Step 12B 采错列 —— 而那种错在离线测试里完全看不出来。
    """
    print("\n[12A-5] 字段编号与本地 proto 一致")
    proto = PROTO.read_text(encoding="utf-8")

    def field_num(message: str, field: str):
        m = re.search(rf"message {message}\s*\{{(.*?)\n\}}", proto, re.S)
        if not m:
            return None
        fm = re.search(rf"\b{field}\s*=\s*(\d+)\s*;", m.group(1))
        return int(fm.group(1)) if fm else None

    expect = [
        ("LikeMessage", "count", 2),
        ("LikeMessage", "total", 3),
        ("GiftMessage", "giftId", 2),
        ("GiftMessage", "groupCount", 4),
        ("GiftMessage", "repeatCount", 5),
        ("GiftMessage", "comboCount", 6),
        ("GiftMessage", "repeatEnd", 9),
        ("GiftMessage", "groupId", 11),
        ("GiftMessage", "logId", 16),
        ("GiftMessage", "totalCount", 29),
        ("GiftMessage", "traceId", 35),
        ("Message", "msgId", 3),
        ("Common", "msgId", 2),
    ]
    for msg, fld, num in expect:
        got = field_num(msg, fld)
        check(f"{msg}.{fld} == {num}", got == num, f"proto 里是 {got}")

    # 证据文件里登记的那些字段串也要与 proto 一致
    d = load()
    blob = json.dumps(d["confirmed"], ensure_ascii=False)
    for msg, fld, num in expect:
        token = f"{msg}.{fld} = {num}"
        if token in blob:
            real = field_num(msg, fld)
            check(f"证据里的 {token} 正确", real == num, real)


def test_doc_exists_and_is_marked_not_live():
    """文档必须存在, 且**开头就**声明不含真实采样。"""
    print("\n[12A-6] 文档存在且标注非实测")
    check("文档存在", DOC.exists(), DOC)
    if not DOC.exists():
        return
    md = DOC.read_text(encoding="utf-8")
    head = md[:600]
    check("开头声明不含真实直播采样",
          "不含任何真实直播采样" in head, head[:120])
    check("提到 Step 12B 仍未发生", "12B" in md)
    for sec in ("已确认", "仍未知"):
        check(f"有『{sec}』小节", sec in md, sec)


def main():
    tests = [
        test_evidence_file_is_valid_and_complete,
        test_no_fabricated_time_series,
        test_unknown_list_is_preserved,
        test_evidence_does_not_claim_gift_semantics,
        test_proto_field_numbers_match_local_proto,
        test_doc_exists_and_is_marked_not_live,
    ]
    for t in tests:
        t()
    print()
    if FAIL[0]:
        print(f"FAILED: {FAIL[0]} 项")
        return 1
    print("PASS: Step 12A 外部证据(非实测)完整性通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
