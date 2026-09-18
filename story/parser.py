#!/usr/bin/env python
# coding: utf-8
"""宽容解析器 —— 从大模型的自由文本里抽取结构化结果。

为什么要"宽容": 实测表明 deepseek-v4.1-flash **拒绝遵守任何严格的输出格式**。
我们先后试了 4 种规格(单行【答】、`1|是`、`编号|裁决|点评`、纯编号列表),
每一次它都会:
    - 加上解释性文字
    - 用 markdown 粗体(**是**)
    - 在分隔符之间乱变(`|` / `→` / 无 / `、`)

**唯一在所有变体里都存活的是「数字编号前缀」。** 所以解析器以编号为锚:
先按 `^\\s*\\**(\\d{1,2})\\s*[.．、)）]` 切块, 再在每块内按优先级扫裁决词。

对"话痨"的态度: 模型硬要加的解释不丢弃, 而是当成观众可见的**点评**
(截断到 60 字)。这是把它的缺点变成内容。

谜题解析同理: 模型会输出 `**汤面**` / `<details>` / markdown 标题,
全部剥掉, 并接受多种标记变体。

本模块**不导入** story 包内其他模块(除 state 的数据类), 避免循环依赖。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

from .state import QAResult

# ======================================================================
# 裁决
# ======================================================================

# 五个合法裁决。顺序即"精确匹配"的优先序。
VERDICTS = ("是", "不是", "无关", "揭晓")
SOLVE = "揭晓"

# 按**优先级**排列的裁决关键词 —— 顺序敏感!
# '揭晓' > '无关' > '不是' > '是'
# 若把 '是' 放前面, "不是" 会先命中 '是' 而误判。
_VERDICT_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    (SOLVE, ("揭晓", "真相是", "答案是", "正确答案", "谜底是", "完全正确", "答对了")),
    ("无关", ("无关", "没有关系", "没关系", "不相关", "不相干", "无关紧要")),
    ("不是", ("不是", "没有", "不对", "并非", "错的", "不正确", "否", "非",
              "不在了", "已死", "已经死", "死了")),
    ("是", ("是", "对", "没错", "正确", "是的", "对的", "确实")),
]

# 块锚: 行首的 "1." / "**1.**" / "1、" / "1）" / "1)" / "1|" / "1→"
# 实测模型的编号后的分隔符在 . 、 ) | → 之间乱变, 所以全部接受。
_BLOCK_RE = re.compile(r"(?m)^\s*\**\s*(\d{1,2})\s*(?:[.．、)）|｜→]|\s*[-—]\s*)\s*")


def _clean(text: str) -> str:
    """去掉 markdown 痕迹与零宽字符。"""
    t = text or ""
    t = t.replace("**", "").replace("__", "")
    t = t.replace("`", "")
    # 行首 markdown 标题/列表符
    t = re.sub(r"(?m)^\s*#{1,6}\s*", "", t)
    t = re.sub(r"(?m)^\s*[-*•·]\s+", "", t)
    # 零宽字符
    t = t.replace("​", "").replace("﻿", "")
    return t


def _match_verdict(text: str) -> Optional[str]:
    """在一段文本里找裁决词。

    策略: **优先看句子开头**(模型几乎总是把裁决放在最前面), 找不到再退回
    全文扫描。否则 "是的，味道不一样——但这不是根本原因" 会被结尾的
    "不是" 抢先命中, 判反。
    """
    t = _clean(text)
    if not t:
        return None
    # ① 开头优先(容忍前导 emoji/符号/空白)
    m = _STRICT_HEAD.match(t.strip())
    if m:
        v = _match_verdict_any(m.group(1))
        if v:
            return v
    # ② 第一句内找(句号/换行/破折号之前)
    head = re.split(r"[。！？!?\n—–]", t, maxsplit=1)[0]
    v = _match_verdict_any(head)
    if v:
        return v
    # ③ 全文兜底
    return _match_verdict_any(t)


def _match_verdict_any(text: str) -> Optional[str]:
    """按优先级扫裁决词, 首个命中即返回。"""
    t = _clean(text)
    for verdict, words in _VERDICT_PATTERNS:
        for w in words:
            if w in t:
                return verdict
    return None


# 严格版: 只在"纯散文兜底"时用。
# 松版会把 "这真是一个好天气啊" 里的 '是' 当裁决 —— 必须避免。
_STRICT_HEAD = re.compile(
    r"^\s*[（(【\[]?\s*"
    r"(揭晓|真相是|答案是|正确答案|谜底是|完全正确|答对了"
    r""
    r"|无关|没有关系|不相关|不相干"
    r"|不是|没有|不对|并非|不正确|错的"
    r"|是的|是|对|没错|正确|确实)"
    r"\s*[。！，,、）)】\]]?"
)


def _match_verdict_strict(text: str) -> Optional[str]:
    """严格版裁决匹配: 裁决词必须出现在**开头**(容忍前导括号/空白)。

    仅用于无编号的纯散文兜底 —— 那种情况下宁可判定"没答出来"
    (退回重试), 也不要从闲聊里臆造一个裁决。
    """
    t = _clean(text).strip()
    if not t:
        return None
    m = _STRICT_HEAD.match(t)
    if not m:
        return None
    return _match_verdict_any(m.group(1))


def _comment_after(text: str, verdict: str) -> str:
    """把裁决词之后的文本当作点评。截断到 60 字。

    模型常写成 "**没有。** 他以前喝过的其实是同伴的肉" ——
    '没有。' 后那半句就是很好的观众可见点评。
    """
    t = _clean(text)
    # 去掉开头的编号/问句残留
    # 找到裁决词最后一次出现的位置(用模式里匹配到的那个词)
    pos = -1
    for _, words in _VERDICT_PATTERNS:
        for w in words:
            i = t.find(w)
            if i >= 0 and i + len(w) > pos:
                pos = i + len(w)
    tail = t[pos:] if pos >= 0 else t
    # 去掉紧跟的分隔符与括号内容
    tail = re.sub(r"^[\s|｜→\-—:：,，。.、)）\]】]+", "", tail)
    tail = re.sub(r"^[（(][^）)]*[）)]\s*", "", tail)   # 开头的括号注释
    tail = " ".join(tail.split())
    if len(tail) > 60:
        cut = tail[:60]
        for sep in ("。", "！", "？", "，", "、", ",", " "):
            j = cut.rfind(sep)
            if j >= 20:
                return cut[:j]
        return cut
    return tail


def _strip_question_echo(block: str) -> str:
    """块内头部常回显问题(如 `**#他是盲人吗** → **是**`)。

    若同行有分隔符, 只取分隔符右侧 —— 避免把问句里的字当裁决。
    """
    # 取第一行做判断; 若整块只有一行, 按分隔符切
    for sep in ("→", "->", "|", "｜", "=>", "：", ":"):
        if sep in block:
            # 仅当分隔符左侧像问句(含 # 或 吗/?/？)时才切
            left, _, right = block.partition(sep)
            if ("#" in left or "吗" in left or "?" in left or "？" in left
                    or "是不是" in left or "有没有" in left):
                return right
    return block


def _verdict_span(text: str) -> tuple[int, int]:
    """找裁决词在文本中的 (起, 止) 位置。找不到 (-1,-1)。"""
    t = _clean(text)
    best = (-1, -1)
    for _, words in _VERDICT_PATTERNS:
        for w in words:
            i = t.find(w)
            if i < 0:
                continue
            j = i + len(w)
            # 命中"最靠前"的裁决词(与 _match_verdict 的优先级一起决定结果)
            if best[0] < 0 or i < best[0]:
                best = (i, j)
    return best


def _truncate_after_verdict(block: str) -> str:
    """把块截到裁决句结束 —— 防止块后面的散文/重复列表污染裁决。

    例: `不在了。画家已死。\\n\\n如果你是想让我用"是/否/无关"格式…`
        -> 只保留到第一句。
    """
    t = _clean(block)
    i, j = _verdict_span(t)
    if i < 0:
        return t
    # 从裁决词往后找第一个句末标点(。！？换行), 作为句界
    for k in range(j, min(len(t), j + 40)):
        if t[k] in "。！？!?\n":
            return t[:k + 1]
    return t[:j + 30]


def parse_answers(raw: str, batch: list) -> tuple[list[QAResult], list[int]]:
    """从模型回复里解出裁决。

    batch: [PendingQ, ...] —— 本轮提问, 已按派发顺序排列。
           解析出的块号 i(从 1 起) 对应 batch[i-1]。

    返回 (results, unanswered_qids):
        results       已解出的 QAResult 列表
        unanswered    模型没答到的 qid(需退回队列重试)
    """
    text = _clean(raw or "")
    if not text.strip():
        return [], [q.qid for q in batch]

    blocks: list[tuple[int, str]] = []      # (块号, 块文本)
    marks = list(_BLOCK_RE.finditer(text))
    if marks:
        for i, m in enumerate(marks):
            num = int(m.group(1))
            start = m.end()
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            blocks.append((num, text[start:end].strip()))
    else:
        # 零编号兜底: 模型吐了纯散文。只答第一条, 其余退回。
        # 用**严格**匹配 —— 否则 "这真是一个好天气啊" 里的 '是' 会被当成裁决。
        v = _match_verdict_strict(text)
        if v is None:
            return [], [q.qid for q in batch]
        first = batch[0]
        return [QAResult(qid=first.qid, verdict=v,
                         comment=_comment_after(text, v))], \
               [q.qid for q in batch[1:]]

    by_num: dict[int, QAResult] = {}
    for num, body in blocks:
        if num < 1 or num > len(batch):
            continue                        # 越界块: 忽略
        if num in by_num:
            continue                        # 重复编号(模型会写两遍): 先到先得
        target = batch[num - 1]
        body2 = _strip_question_echo(body)
        body2 = _truncate_after_verdict(body2)      # 防止块尾散文污染裁决
        v = _match_verdict(body2) or _match_verdict(_truncate_after_verdict(body))
        if v is None:
            continue                        # 这块没裁决 -> 留待重试
        comment = _comment_after(body2, v)
        by_num[num] = QAResult(qid=target.qid, verdict=v, comment=comment)

    results = [by_num[i] for i in sorted(by_num)]
    answered = {r.qid for r in results}
    unanswered = [q.qid for q in batch if q.qid not in answered]
    return results, unanswered


# ======================================================================
# 谜题
# ======================================================================

# 标记词本身。检测时不要求冒号/书名号 —— 实测模型会把标记写在
# <summary>🧩 汤底</summary> 里, HTML 剥掉后只剩 "🧩 汤底" 一行,
# 既没有【】也没有冒号。所以按"行内是否含标记词"来定位, 更稳。
_SURFACE_WORDS = ("谜面", "汤面", "题目", "问题")
_ANSWER_WORDS = ("谜底", "汤底", "答案", "真相", "解答")
_HINT_WORDS = ("提示", "线索")
_TITLE_WORDS = ("标题",)
# 谜面区的天然终止语(模型爱在谜面后加一句 "请提问" 之类的引导)。
# 注意: **不能**把 "为什么" 当终止词 —— 那常是谜面本体的一部分
# (例如 "他喝了一口就自杀了。为什么？")。
_END_WORDS = ("请提问", "请开始提问", "开始提问", "请开始猜", "请猜", "请你解释", "请回答")
# 这些词出现时不算标记(避免误伤正文, 如"这个问题的答案")
_WORD_BLOCKLIST = ("这个问题", "那个问题", "没有答案")

# 标记词之后允许的"标签尾": 空白/冒号/括号(括号内可含短文本, 如"（谜面）")
_MARK_TAIL = re.compile(r"^[\s：:）)\]】]*(?:[（(][^）)]{0,8}[）)][\s：:]*)*[\s：:]*$")

_EMOJI_STRIP = re.compile(r"^[\s\W_]*", re.UNICODE)


def _looks_like_marker(line: str) -> bool:
    """判断一行是不是单纯的标记行(如 '汤底'、'提示'), 而非提示内容。"""
    h = _EMOJI_STRIP.sub("", line).strip(" ：:").strip()
    if not h:
        return True
    for words in (_SURFACE_WORDS, _ANSWER_WORDS, _HINT_WORDS, _TITLE_WORDS):
        for w in words:
            if h == w or h == w + "：" or h == w + ":":
                return True
    return False


def _line_of(text: str, pos: int) -> tuple[int, int]:
    """返回 pos 所在行的 (start, end)。"""
    s = text.rfind("\n", 0, pos) + 1
    e = text.find("\n", pos)
    if e < 0:
        e = len(text)
    return s, e


def _find_first(text: str, words: tuple[str, ...]) -> tuple[int, str]:
    """找**作为标记出现**的标记词, 返回 (位置, 标记词)。

    实测模型会写成各种花样, 都必须认出来:
        【谜面】 / 谜面： / **汤面（谜面）：** / 🧩 汤底 / 汤底:

    判定"是标记"的条件(比死字符串匹配宽容得多):
        - 该行以该词开头(容忍行首 emoji/符号), 且该词后面紧跟
          冒号/左括号/【】/行尾 —— 即它是**标签**而非正文里偶然提到;
        - 或该词被【】包裹;
        - 或该词后紧跟冒号。
    """
    best = (-1, "")
    for w in words:
        for m in re.finditer(re.escape(w), text):
            i = m.start()
            if any(b in text[max(0, i - 3):i + len(w) + 2] for b in _WORD_BLOCKLIST):
                continue
            ls, le = _line_of(text, i)
            line = text[ls:le]
            head = _EMOJI_STRIP.sub("", line)          # 去掉行首 emoji/符号
            after = head[len(w):] if head.startswith(w) else None
            is_mark = (
                (i > 0 and text[i - 1] == "【")        # 【谜面】
                or (i + len(w) < len(text) and text[i + len(w)] in "：:")
                # 行首出现, 且后面只跟标签尾(冒号/括号/空白/行尾)
                or (after is not None and _MARK_TAIL.match(after) is not None)
            )
            if is_mark and (best[0] < 0 or i < best[0]):
                best = (i, w)
                break
    return best


def _slice_section(text: str, start_mark: str, stop_marks: tuple[str, ...]) -> str:
    """从 start_mark 之后取到任意 stop_mark(或结尾)之前的文本。"""
    i = text.find(start_mark)
    if i < 0:
        return ""
    start = i + len(start_mark)
    # 吃掉标记后紧跟的标签残留: "（谜面）：" / "：" / "】" / 空白
    while start < len(text):
        ch = text[start]
        if ch in " \t　：:】]":
            start += 1
        elif ch in "（(":
            j = text.find("）", start)
            j2 = text.find(")", start)
            if j < 0 or (j2 >= 0 and j2 < j):
                j = j2
            if j < 0:
                break
            start = j + 1
        else:
            break
    end = len(text)
    for sm in stop_marks:
        # 只找"作为标记出现"的停止词
        pos, word = _find_first(text[start:], (sm,))
        if pos < 0:
            continue
        # 切在**整条标记的最左端**, 而不是词本身的位置 ——
        # 否则 "【提示】" 会把左边的 "【" 留在上一段里
        # (实测: 谜面尾部多出一个孤零零的 "【")。
        cut = start + pos
        if text[cut:cut + 1] not in "【[":
            for b in ("【", "["):
                j = text.rfind(b, start, cut)
                # 只回退到**紧邻的**左括号(中间不能隔着内容)
                if j >= 0 and cut - j <= 2:
                    cut = j
                    break
        end = min(end, cut)
    return text[start:end].strip(" \t\r\n-—|｜")


class Riddle:
    """出题结果。"""

    __slots__ = ("puzzle", "answer", "hints", "title", "error")

    def __init__(self, puzzle: str = "", answer: str = "", hints: Optional[list] = None,
                 title: str = "", error: Optional[str] = None):
        self.puzzle = puzzle
        self.answer = answer
        self.hints = hints or []
        self.title = title
        self.error = error

    def __repr__(self) -> str:      # pragma: no cover - 调试用
        return (f"Riddle(title={self.title!r}, puzzle={self.puzzle[:24]!r}…, "
                f"answer={self.answer[:24]!r}…, hints={len(self.hints)}, "
                f"error={self.error!r})")


def _strip_html(text: str) -> str:
    """剥掉 <details>/<summary> 等标签, 但**保留**标签内的文本。

    模型爱写 <details><summary>提示</summary>实际内容</details>,
    内容是有用的, 标签是有害的。
    """
    t = re.sub(r"</?[a-zA-Z][^>]*>", "", text or "")
    return t


def parse_riddle(raw: str) -> Riddle:
    """解析出题回复 -> Riddle。

    宽容策略:
        - 剥 markdown / HTML 标签
        - 接受 【谜面】/【汤面】/谜面：/汤面： 等多种标记
        - 解析不出谜面 -> **整段当谜面**(永不空屏), 并记 error
    """
    text = _strip_html(raw or "")
    text = _clean(text)
    if not text.strip():
        return Riddle(error="出题回复为空")

    title = ""
    ti, tm = _find_first(text, _TITLE_WORDS)
    if ti >= 0:
        line = text[ti + len(tm):].split("\n", 1)[0].strip(" \t-—|｜#【】")
        title = line[:40]

    # 先定位谜底, 它同时是谜面的终止边界
    ai, am = _find_first(text, _ANSWER_WORDS)
    si, sm = _find_first(text, _SURFACE_WORDS)

    answer = ""
    puzzle = ""

    if ai >= 0:
        # 谜底: 到 提示/线索 标记或结尾
        answer = _slice_section(text, am, _HINT_WORDS)

    if si >= 0:
        # 谜面: 到 谜底/提示/标题 任一标记处
        stop: tuple[str, ...] = ()
        for w in (_ANSWER_WORDS + _HINT_WORDS + _TITLE_WORDS):
            if w not in stop:
                stop = stop + (w,)
        puzzle = _slice_section(text, sm, stop)
        # 再按"请提问"之类的引导语收尾(纯子串, 不是标签)
        for w in _END_WORDS:
            j = puzzle.find(w)
            if j > 0:
                puzzle = puzzle[:j]

    # 兜底: 没有谜面标记
    if not puzzle:
        if ai > 0:
            # 谜底之前的所有内容当谜面
            puzzle = text[:ai].strip(" \t\r\n-—|｜")
        elif ai < 0 and si < 0:
            # 既无谜面也无谜底标记 -> 整段当谜面
            puzzle = text.strip()
        if not puzzle and not answer:
            puzzle = text.strip()

    # 谜面兜底: 去掉残留标题行、分隔线与引导语
    puzzle = re.sub(r"(?m)^\s*#{1,6}\s*.*$", "", puzzle)
    puzzle = re.sub(r"(?m)^\s*[-—=*]{2,}\s*$", "", puzzle)      # --- / === 分隔线
    puzzle = re.sub(r"(?m)^\s*(海龟汤|汤面|谜面|谜题)\s*[:：]?\s*$", "", puzzle)
    for w in _END_WORDS:
        j = puzzle.find(w)
        if j > 0:
            puzzle = puzzle[:j]
    puzzle = puzzle.strip(" \t\r\n-—|｜")

    # 提示: 收集所有提示标记之后的行
    hints: list[str] = []
    hint_i, hint_m = _find_first(text, _HINT_WORDS)
    if hint_i >= 0:
        hint_text = _slice_section(text, hint_m, _ANSWER_WORDS)
        # 逐行, 去掉 "提示一：" / "1." 等前缀
        for line in hint_text.split("\n"):
            ln = re.sub(r"^\s*(提示|线索)[一二三四五1-9]?\s*[:：]?\s*", "", line).strip()
            ln = re.sub(r"^\s*\d{1,2}\s*[.．、)）]\s*", "", ln).strip()
            ln = re.sub(r"^\s*[-*•·]\s*", "", ln).strip()
            ln = ln.strip(" \t-—|｜")
            if ln and len(ln) >= 4 and not _looks_like_marker(ln):
                hints.append(ln[:80])

    err = None
    if not answer:
        err = "未解析出谜底(揭晓时会重新生成)"

    return Riddle(puzzle=puzzle, answer=answer, hints=hints[:3],
                  title=title, error=err)


# ======================================================================
# 兜底谜题(出题连续失败时用, 保证永不开天窗)
#
# 注意: **不要用"海龟汤"那道题** —— 它太出名, 一放出来观众就知道
# 出题挂了。这里准备几道自己写的、结构标准的题, 轮换使用。
# ======================================================================
FALLBACK_RIDDLES = [
    (
        "一个男人每天下班都从公司后门走，哪怕绕远路。有一天他走了前门，"
        "第二天就辞职了。为什么？",
        "后门那条巷子里住着他前妻一家。他每天绕路是为了避开他们，"
        "不想让孩子看见自己过得不好。那天前门封了只能走后门，"
        "他撞见前妻带着孩子，孩子喊了别人一声爸爸。",
    ),
    (
        "她每次坐电梯都要先按一个没人的楼层，再走楼梯回去。"
        "那天她直接按了自己家那层，进门就哭了。为什么？",
        "她有严重的被害妄想，一直靠这个「多余动作」给自己留观察时间。"
        "今天她选择了不再防备——因为医生告诉她，她的妄想症治不好了，"
        "她决定不再让这个病支配自己的生活。",
    ),
    (
        "老人每天都去公园同一张长椅坐一整个下午，从不跟人说话。"
        "有一天他把椅子上的名字牌撕了，之后再没来过。为什么？",
        "那张椅子是他去世老伴捐的，牌子上刻着老伴的名字。"
        "他每天来是陪她。今天管理处要翻修椅子、换掉旧名牌，"
        "他觉得连这点念想都留不住，于是不再来了。",
    ),
    (
        "男人把家里的钟全调快十分钟。家人发现后要调回来，他跪下来求他们别动。为什么？",
        "他妻子生前有严重的迟延症，出门总要磨蹭，两人为此吵了半辈子。"
        "妻子车祸去世那天，正是因为赶时间闯了红灯。"
        "调快十分钟是他给自己留的「缓冲」，也是他赎罪的方式。",
    ),
]
FALLBACK_RIDDLE = FALLBACK_RIDDLES[0]
FALLBACK_HINTS = ["注意他为什么要绕路。", "问题出在他路上遇到的人。",
                  "他不想被谁看见？"]


# ======================================================================
# 小工具: 提问归一化(供引擎去重与注入清洗)
# ======================================================================
def simplify_for_dedupe(raw: str, max_len: int = 40) -> str:
    """把提问压成去重 key: 去标点/空白/emoji, 统一大小写。"""
    s = (raw or "").strip()
    if s.startswith("#"):
        s = s.lstrip("#").strip()
    s = re.sub(r"[\s　!-/:-@\[-`{-~！-＠［-｀｛-～、。〃〈〉《》「」『』【】〔〕・ー…‥‘’“”]+",
               "", s)
    s = "".join(c for c in s if unicodedata.category(c) != "So")
    return s.casefold()[:max_len]


def display_question(raw: str, max_len: int = 60) -> str:
    """给观众看的提问文本: 去掉 '#' 前缀与首尾空白, 保留原始语气。

    '#他是盲人吗！！' -> '他是盲人吗！！'
    """
    if not isinstance(raw, str):
        return ""
    s = raw.strip()
    if s.startswith("#"):
        s = s.lstrip("#").strip()
    s = re.sub(r"[\s　]+", " ", s)
    s = "".join(c for c in s if unicodedata.category(c) != "So")
    return s.strip()[:max_len]
