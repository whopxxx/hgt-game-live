#!/usr/bin/env python
# coding: utf-8
"""外部题库导入的**公共底座**(Batch H1/H2)。

它解决的问题只有一个:

    网络抓下来的东西是**脏的、非确定的、带版权义务的**,
    而下游(去重 / AI 审题 / curated 编译 / 进池)需要的是
    **确定的、同一份 schema 的、attribution 齐全的**记录。

所以这里放三件东西, 两个 importer(`import_turtlebench.py` /
`import_puzzling_se.py`)共用:

    RawCuratedPuzzle    统一的原始记录 schema(H1-D)
    normalize_*         文本归一(供去重与指纹; H1-E)
    write_jsonl_deterministic
                        确定性落盘(同输入 -> 字节级同输出)

## 为什么"确定性"是一条硬要求

任务书 H1-D: "必须 deterministic。同样输入重新运行: byte-for-byte same。
除了显式的抓取时间字段; 最好抓取时间另放 metadata, 不混进 stable record。"

这不是洁癖。下游 AI 编译是**要花钱**的: 一份 curated_raw.jsonl 若每次
重跑都字节不同, 就没法做"这次只编译新增的那些"这种增量, 也没法用
`diff` 判断"题库真的变了没有" —— 每次都全量重编, 烧的是真金白银。

所以:

    记录本身        -> 只放**来源侧**的稳定事实(id/文本/作者/许可/分数)
    抓取时间 / 配额 -> 另写一份 `*.meta.json`, **不进记录**

排序也必须定死: 按 `(source, external_id)` 排。**不能**按抓取顺序 ——
API 分页顺序会变, 字典序才稳定。

## 版权(这是这个文件里最容易被忽略、后果最重的一段)

Stack Exchange 的公开内容按**发布时间**适用不同的 CC BY-SA 版本:

    < 2011-04-08           CC BY-SA 2.5
    2011-04-08 ~ 2018-05-01  CC BY-SA 3.0
    >= 2018-05-02          CC BY-SA 4.0

所以**绝不允许**给所有题统一硬编码 `CC BY-SA 4.0`。而且一个问题的
question 与它采纳的 answer 可能**跨版本**(老问题、新答案) —— 那种
情况必须**分别保存**。

好消息: SE API 实际会在每个 post 上回一个 `content_license` 字段,
那是**权威值**, 比按日期猜更可靠。所以本模块的策略是:

    优先用 API 回的 content_license
    只有在缺失时才按发布时间回退(并记下是回退来的)
    两者都没有 -> 记 "unknown", 且 **不允许**它进 curated 池

`license_inference` 字段显式记录这个来源, 免得以后有人把"猜的"当成
"API 说的"。
"""

from __future__ import annotations

import datetime
import gzip
import hashlib
import json
import logging
import os
import re
import time
import unicodedata
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

log = logging.getLogger("hgt.curated")

#: 本模块写出的记录格式版本。将来加了字段就 bump, 让下游能判断。
CURATED_RAW_VERSION = 1

#: `data_external/` —— **绝不进版本库**(见 .gitignore)。
#: 任务书: "不要把下载后的原始文件 commit 到 repo。"
EXTERNAL_ROOT = os.path.join("data_external")


# ======================================================================
# 版权
# ======================================================================
#: Stack Exchange 按发布时间的许可分段(官方说明)。
#: 仅在 API **没有**回 `content_license` 时用作回退。
SE_LICENSE_CUTOVERS = (
    # (起始时间戳(含), 许可证名)
    (0, "CC BY-SA 2.5"),
    (1302220800, "CC BY-SA 3.0"),      # 2011-04-08 UTC
    (1525219200, "CC BY-SA 4.0"),      # 2018-05-02 UTC
)

#: 认得的许可证白名单。认不出的一律 **不允许** 进 curated 池 ——
#: "我不认识这个许可" 的正确处置是停下, 不是当成 4.0。
KNOWN_LICENSES = frozenset({
    "CC BY-SA 2.5", "CC BY-SA 3.0", "CC BY-SA 4.0",
    "Apache-2.0", "CC0-1.0", "Public Domain",
})

#: 项目明确批准的来源(H4-A §七)。这些来源**没有**可识别的开放许可证,
#: 但使用依据是"项目负责人明确批准"—— 那是一个**独立合法状态**,
#: 不是许可证的别名。
#:
#: ⚠️ 它**不是**白名单直通: 这些来源的题一样要过 curated-v3 全部十三条。
#: 这里只解决"版权上能不能收", 不解决"这题好不好"。
#:
#: 同样重要的是: 它**不允许**为了通过检查而伪造一个不存在的
#: CC BY-SA / MIT / Apache —— 伪造版权信息比"许可未知"严重得多。
PROJECT_APPROVED_BASIS = frozenset({"project_approved_public_dataset"})


def se_license_for_ts(ts: Optional[int]) -> str:
    """按发布时间回退推断 SE 许可证(仅当 API 没回 content_license 时用)。"""
    if not ts:
        return "unknown"
    out = SE_LICENSE_CUTOVERS[0][1]
    for start, name in SE_LICENSE_CUTOVERS:
        if ts >= start:
            out = name
    return out


def resolve_license(api_license: Any, created_ts: Optional[int]) -> tuple:
    """返回 `(license, inference)`。

    `inference` 是 `"api"` / `"created_at"` / `"unknown"` —— 显式记下
    这个值**从哪来**。下游要靠它区分"SE 明确说了"与"我们按日期猜的":
    前者可以进池, 后者要人工过一眼。
    """
    s = str(api_license or "").strip()
    if s and s in KNOWN_LICENSES:
        return s, "api"
    if s and s.lower() not in ("", "unknown", "none"):
        # API 给了一个我们不认识的值 —— 如实带出, 但标记成不认识,
        # 由下游把它挡在池外。**不**悄悄替换成 4.0。
        return s, "unrecognized"
    guess = se_license_for_ts(created_ts)
    return (guess, "created_at") if guess != "unknown" else ("unknown", "unknown")


def license_is_usable(lic: str, inference: str) -> bool:
    """这份许可能不能支撑"我们把它编译进直播题池"?

    只有**认得的**许可可以。`created_at` 回退**允许**(它就是官方分段
    规则), 但 `unknown` / `unrecognized` 一律不行。
    """
    return (str(lic or "") in KNOWN_LICENSES
            and inference in ("api", "created_at"))


# ======================================================================
# 文本归一(去重用; H1-E)
# ======================================================================
_WS_RE = re.compile(r"\s+")
#: 中文标点 -> 统一形态。只做"同一符号的不同写法"这种**无损**归一,
#: 不做任何语义改写 —— 归一是为了判重, 不是为了改写题目。
_PUNCT_MAP = {
    "，": ",", "。": ".", "、": ",", "；": ";", "：": ":",
    "？": "?", "！": "!", "“": '"', "”": '"', "‘": "'", "’": "'",
    "（": "(", "）": ")", "【": "[", "】": "]", "《": "<", "》": ">",
    "…": "...", "—": "-", "～": "~", "　": " ",
}
_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: Any) -> str:
    """去掉 HTML 标签并解开常见实体。

    SE 的 `body` 是 HTML(`<p>` / `<blockquote class="spoiler">` / `<a>` …),
    直接喂给 AI 会把标签也当成题目内容。这里只做**粗剥**: 去标签 +
    解实体 + 压空白。真正的"这题讲了什么"判断交给 AI 审题那一步。
    """
    s = str(text or "")
    # <br> / </p> 这类是**结构分隔**, 要变成换行, 不能直接抹掉,
    # 否则两段话会粘成一句。
    s = re.sub(r"<\s*(br|/p|/div|/li|/blockquote)\s*/?\s*>", "\n", s,
               flags=re.I)
    s = _TAG_RE.sub("", s)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                 ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'"),
                 ("&hellip;", "…"), ("&mdash;", "—"), ("&ndash;", "-")):
        s = s.replace(a, b)
    s = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), s)
    return s.strip()


def normalize_for_dedup(text: Any) -> str:
    """把文本压成**判重用的规范形**。

    只做无损变换: Unicode NFKC、剥 HTML、标点归一、空白归一、去零宽。
    **不做**任何改写 —— 它只用来比较"这两条是不是同一道题", 不用于
    最终展示。展示永远用原始文本。
    """
    s = strip_html(text)
    s = unicodedata.normalize("NFKC", s)
    s = "".join(ch for ch in s
                if unicodedata.category(ch) != "Cf")      # 零宽字符
    s = "".join(_PUNCT_MAP.get(ch, ch) for ch in s)
    s = _WS_RE.sub(" ", s).strip().lower()
    return s


def stable_hash(*parts: Any, n: int = 12) -> str:
    """稳定内容哈希(前 n 位 hex)。

    用**内容**而不是抓取时拿到的 id: 同一条内容从 API 抓、从 dump 抓、
    重跑一次, 必须得到同一个 external_id —— 否则去重和增量编译全部失效。
    """
    raw = "\x1f".join(str(p or "") for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:n]


# ======================================================================
# 统一记录(H1-D)
# ======================================================================
@dataclass
class RawCuratedPuzzle:
    """一道**外部来源**的原始题(未经 AI 编译)。

    字段严格按任务书 H1-D。刻意保持"来源侧事实"的纯度: 这里**没有**
    "AI 觉得它好不好"的任何字段 —— 那是 H2-B 审题门的产物, 属于
    下一层, 不该混进 stable record(混进来就等于让"重跑一次"依赖
    上一次的模型输出, 确定性当场消失)。
    """

    external_id: str = ""
    source: str = ""
    source_url: str = ""
    source_kind: str = ""              # dataset / stackexchange

    question_author: str = ""
    question_author_url: str = ""
    answer_author: str = ""
    answer_author_url: str = ""

    question_license: str = ""
    answer_license: str = ""

    title: str = ""
    surface: str = ""                  # 谜面(题面/异常情境)
    bottom: str = ""                   # 谜底(解释)

    language: str = ""
    original_language: str = ""
    translated: bool = False

    tags: list = field(default_factory=list)

    question_score: Optional[int] = None
    answer_score: Optional[int] = None

    # ---- 版权溯源(不是"内容", 但必须随记录走) ----
    #: question / answer 的许可各自**从哪来**: api / created_at /
    #: unrecognized / unknown。见 `resolve_license`。
    question_license_inference: str = ""
    answer_license_inference: str = ""
    question_created_at: Optional[int] = None
    answer_created_at: Optional[int] = None
    question_id: Optional[int] = None
    answer_id: Optional[int] = None

    #: 内容安全筛查结果。"" = 没发现问题; 否则是需要人工看的原因。
    safety_flag: str = ""

    #: **授权依据**(H4-A §七)。与 `question_license` 是**两件不同的事**:
    #:
    #:     question_license  "这份内容按哪个开放许可证发布"
    #:     usage_basis       "我们凭什么用它"
    #:
    #: 大多数来源两者一致(SE 的 CC BY-SA、TurtleBench 的 Apache)。
    #: 但 `neurostellar/haiguitang` **没有声明任何 license**
    #: (API 实测 `cardData.license == None`), 而项目负责人明确批准
    #: 使用/参考该来源。那是一个**独立合法状态**, 不是许可证的别名。
    #:
    #: ⚠️ 绝不允许为了通过 `license_ok()` 而**伪造**一个不存在的
    #: CC BY-SA / MIT / Apache —— 伪造版权信息比"许可未知"严重得多。
    #: 所以这里显式建模成第三个字段, 由 `license_ok()` 认它。
    #:
    #: 取值:
    #:     ""                                   没有额外依据(走 license 判定)
    #:     "project_approved_public_dataset"    项目明确批准的数据源
    usage_basis: str = ""

    # ---- H1-E 去重标记(**只标记, 不删除**) ----
    #: "" = 不是重复; "near_duplicate" = 与某条 surface 高度相似。
    #:
    #: 刻意放在**同一个 dataclass** 上而不是另开一份"重复清单": 去重
    #: 结论是这条记录的属性, 分开存就得靠 external_id 两边对账, 而那
    #: 正是"两份判据迟早漂移"的老问题。
    dup_reason: str = ""
    #: 与哪一条重复(对方的 external_id)。
    dup_of: str = ""
    #: 相似度(0~1)。仅 near_duplicate 有意义。
    dup_score: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Any) -> "RawCuratedPuzzle":
        if not isinstance(d, dict):
            return cls()
        known = {f for f in cls.__dataclass_fields__}          # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})

    def dedup_key(self) -> tuple:
        """**exact dedup 的判据**: 归一后的 (surface, bottom)。

        任务书 H1-E: "exact dedup: normalize(surface) / normalize(bottom)
        完全相同只留一份。"
        """
        return (normalize_for_dedup(self.surface),
                normalize_for_dedup(self.bottom))

    def license_ok(self) -> bool:
        """版权/授权上能不能进 curated 池?

        ## 两条独立的合法路径(§七)

            ① 认得的开放许可证(`question_license` 在 KNOWN_LICENSES 里,
               且 inference 是 api / created_at)
            ② 项目明确批准的来源(`usage_basis` 在 PROJECT_APPROVED_BASIS 里)

        第 ② 条**不是**"为了绕过许可检查"的后门: 它是"这个来源的授权
        依据不是某个开放许可证"这件事的**如实建模**。把 haiguitang 硬填
        成 CC BY-SA 会让版权记录变成假的 —— 那比"来源已批准但无 license
        字段"危险得多。

        ⚠️ 注意 `usage_basis` **不**放宽任何内容质量判据。它只回答版权
        问题: 这些来源的题一样要过 curated-v3 全部十三条。
        """
        if str(self.usage_basis or "") in PROJECT_APPROVED_BASIS:
            return True
        return license_is_usable(self.question_license,
                                 self.question_license_inference)




# ======================================================================
# 内容安全(H1-A)
# ======================================================================
#: 明显不适合**公开直播**的题材。任务书: "过于血腥、明确自伤情节、
#: 明显不适合直播的题先进入 `rejected_safety`, 不要自动上线。"
#:
#: ⚠️ 这是**粗筛**, 不是终审。它只挡住"一眼就不该上直播"的, 命中即
#: 送人工看 —— 宁可多送几条, 也不能让一条自伤细节直接上屏。
#: 真正细腻的判断留给 H2-B 的第 9 条("内容是否适合公开直播展示")。
_SAFETY_PATTERNS = (
    ("self_harm", ("自杀", "割腕", "跳楼", "上吊", "自残", "服毒", "安眠药自杀")),
    ("gore", ("碎尸", "肢解", "开膛", "挖出内脏", "脑浆", "血肉模糊", "剥皮")),
    ("minor_harm", ("虐童", "猥亵", "性侵", "强奸")),
)


def safety_screen(text: str) -> str:
    """返回安全标记("" = 没命中)。

    对 surface 与 bottom **都**扫 —— 只扫题面会被"题面干净、谜底血腥"
    绕过, 而那正是海龟汤最常见的形态。
    """
    body = str(text or "")
    if not body:
        return ""
    for slug, needles in _SAFETY_PATTERNS:
        for n in needles:
            if n in body:
                return slug
    return ""


# ======================================================================
# 确定性落盘
# ======================================================================
def ensure_dir(path: str) -> None:
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)


def sort_records(records: list) -> list:
    """确定性排序键: `(source, external_id)`。

    **不能**按抓取顺序 —— API 分页顺序/并发返回顺序都会变, 那会让
    "同样输入"产出不同字节。字典序才是稳的。

    ⚠️ 也接受**普通 dict**: 拒收清单(`rejected_source.jsonl`)是
    `{"row":…, "reason":…}` 这种诊断记录, 不是 `RawCuratedPuzzle`。
    它们同样要确定性落盘, 所以这里按 dict 取 `.get()`。
    """
    def _key(r):
        if isinstance(r, dict):
            return (str(r.get("source") or ""),
                    str(r.get("external_id") or r.get("row") or ""))
        return (str(getattr(r, "source", "") or ""),
                str(getattr(r, "external_id", "") or ""))

    return sorted(records, key=_key)


def write_jsonl_deterministic(path: str, records: list,
                              version_key: str = "curated_raw_version"
                              ) -> int:
    """确定性写 JSONL。返回写入条数。

    两条硬保证:
      1. 记录**排序后**写(见 `sort_records`);
      2. 每行的 JSON 用 `sort_keys=True` + 固定分隔符 —— 否则 dict 的
         插入顺序一变字节就变, "byte-for-byte same" 无从谈起。

    不写抓取时间。要 metadata 请用 `write_meta`。
    """
    ensure_dir(path)
    rows = sort_records(records)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            d = r.to_dict() if hasattr(r, "to_dict") else dict(r)
            f.write(json.dumps(d, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")))
            f.write("\n")
    os.replace(tmp, path)          # 原子替换: 半截文件永远不会被读到
    log.info("写入 %d 条 -> %s", len(rows), path)
    return len(rows)


def write_meta(path: str, **kw) -> None:
    """抓取元信息(时间/配额/命令)。**故意不混进 stable record**。"""
    ensure_dir(path)
    kw.setdefault("fetched_at",
                  datetime.datetime.now().isoformat(timespec="seconds"))
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(kw, f, ensure_ascii=False, sort_keys=True, indent=2)
        f.write("\n")


def read_jsonl(path: str) -> list:
    """读 JSONL。**必须显式 utf-8** —— Windows 上裸 open() 会用 GBK,
    中文题库第一行就炸(本仓已经踩过这个坑)。"""
    out: list = []
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                log.warning("%s 第 %d 行不是合法 JSON, 跳过: %s", path, ln, e)
    return out


#: 两个 importer 共用的 UA。SE 对无 UA 的请求会限流。
USER_AGENT = "hgt-game-live-curated-import/0.1 (+https://github.com/whopxxx/hgt-game-live)"


# ======================================================================
# 网络 / 代理
# ======================================================================
#: 默认代理端口。本机 `huggingface.co` **直连不通**(超时/连接被拒),
#: 必须走本地代理才能拿到官方源。
#:
#: ⚠️ 这是**默认值, 不是硬编码**: 环境变量 `HGT_PROXY` 优先, 传空串
#: 可以显式关掉(直连)。写死一个端口会让脚本换台机器就废掉, 而完全
#: 不支持代理则让它在**本机**直接跑不通。
DEFAULT_PROXY = "http://127.0.0.1:7897"


def resolve_proxy(explicit: Optional[str] = None) -> str:
    """决定用哪个代理。返回 "" = 直连。

    优先级: 显式参数 > `HGT_PROXY`/`HTTPS_PROXY`/`https_proxy` > 默认端口。

    ⚠️ 为什么默认**不**读 `HTTP_PROXY`(大写 HTTP): 那是给**明文** http
    用的, 拿它代理 https 在部分工具链上是错的语义。这里只认明确表示
    "https 也走"的那几个。
    """
    if explicit is not None:
        return str(explicit).strip()
    for k in ("HGT_PROXY", "HTTPS_PROXY", "https_proxy"):
        v = os.environ.get(k)
        if v:
            return str(v).strip()
    return DEFAULT_PROXY


def build_opener(proxy: str, timeout: float = 30.0):
    """建一个带代理的 opener。`proxy` 为空则直连。

    失败重试交给调用方 —— 不同接口的退避语义不同(SE 有 quota/backoff,
    HF 只是网络抖)。
    """
    handlers: list = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler(
            {"http": proxy, "https": proxy}))
    else:
        # 显式**清空**代理: 环境里若设了代理变量, 不显式关掉会走那条路。
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def http_get(url: str, *, proxy: str = "", timeout: float = 30.0,
             accept_gzip: bool = False, retries: int = 3) -> bytes:
    """GET 一个 URL, 带重试与可选 gzip 解压。

    ## 为什么要重试

    实测(本机 + 本地代理): 同一个 host 会**间歇性**失败 ——
    同一条命令连跑三次, 可能一次 `SSL: UNEXPECTED_EOF_WHILE_READING`、
    两次 200。那不是"这个站不可达", 那是抖动。

    如果没有重试, 一次抖动就会被上层解读成"这个来源抓不到", 于是
    静默少抓一大批题 —— 而"少抓了"和"本来就没有"从结果上无法区分。
    这正是本模块最想避免的一类错误。
    """
    last: Exception | None = None
    headers = {"User-Agent": USER_AGENT}
    if accept_gzip:
        headers["Accept-Encoding"] = "gzip"
    op = build_opener(proxy, timeout)
    for attempt in range(max(1, retries)):
        try:
            req = urllib.request.Request(url, headers=headers)
            with op.open(req, timeout=timeout) as r:
                blob = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    blob = gzip.decompress(blob)
            return blob
        except Exception as e:                      # noqa: BLE001
            last = e
            if attempt < retries - 1:
                sleep = 1.5 * (attempt + 1)
                log.warning("GET 失败(第 %d/%d 次): %s: %s —— %.1fs 后重试",
                            attempt + 1, retries, url,
                            type(e).__name__, sleep)
                time.sleep(sleep)
    raise RuntimeError(f"GET 连续 {retries} 次失败: {url}: {last}")
