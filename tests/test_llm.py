"""运行: uv run tests/test_llm.py（完全离线, 无网络）。

验证 PuzzleWriter 走**强制工具调用**时, 能把 tool_input 正确转成结构化结果,
以及在工具不可用(只回了文本)时**回退到宽容解析**。

不联网: 用一个假的 client 顶替 AnthropicMessagesClient。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from story.llm import LLMResult, PuzzleWriter  # noqa: E402

FAIL = [0]


def check(name, cond, extra=""):
    if cond:
        print(f"  ok  {name}")
        return
    print(f"  FAIL {name}  {extra}")
    FAIL[0] += 1


class FakeClient:
    """按顺序吐预设的 LLMResult, 并记录收到的 tool 参数。"""

    def __init__(self, results):
        self._results = list(results)
        self.calls = []

    def messages(self, system, user, max_tokens=None, tool=None):
        self.calls.append({"system": system, "user": user, "tool": tool})
        if not self._results:
            return LLMResult(error="no more canned results")
        return self._results.pop(0)


def test_riddle_tool():
    print("[出题: 强制工具]")
    fc = FakeClient([
        LLMResult(tool_input={
            "title": "海龟汤",
            "puzzle": "一个男人点了海龟汤就自杀了。为什么?",
            "answer": "同伴的肉汤骗局。",
            "hints": ["注意汤的味道", "他以前也喝过", "同伴做了什么"],
        }, model="m"),
        LLMResult(tool_input={"ok": True}),          # 自检通过
    ])
    w = PuzzleWriter(client=fc)
    r = w.gen_riddle()
    check("谜面解析", r.puzzle == "一个男人点了海龟汤就自杀了。为什么?", r)
    check("谜底解析", r.answer == "同伴的肉汤骗局。", r)
    check("提示 3 条", len(r.hints) == 3, r.hints)
    check("用了强制工具", fc.calls[0]["tool"] is not None
          and fc.calls[0]["tool"]["name"] == "emit_riddle", fc.calls[0]["tool"])


def test_reviewer_fixes_in_place():
    print("[审稿: 不合格时由审稿人**直接改好**, 不整题重出]")
    # 上一版是"毙掉 -> 重出一稿全新的"。现在改成: 审稿人读懂了问题就
    # **自己动手改**, 只在原稿上动该动的地方 —— 少一道信息损耗,
    # 也不会把一道只差一句话的好题整个丢掉。
    fc = FakeClient([
        # 出题
        LLMResult(tool_input={"puzzle": "他每晚点灯却不让光照到自己。为什么?",
                              "answer": "他是盲人。", "hints": ["a", "b", "c"]}),
        # 审稿: 不合格, 但**直接把改好的稿子给出来**
        LLMResult(tool_input={"ok": False,
                              "note": "谜面没有具体事件, 已补一个",
                              "puzzle": "他每晚把灯点到最亮, 却用黑布把灯泡裹住。为什么?",
                              "answer": "他是盲人, 靠听声音判断屋里有没有人。",
                              "hints": ["和光有关", "他在听", "他看不见"]}),
        # 再审: 通过
        LLMResult(tool_input={"ok": True}),
    ])
    w = PuzzleWriter(client=fc)
    r = w.gen_riddle()
    check("采用了审稿人的改稿(而不是重出一稿)",
          r.puzzle and "黑布" in r.puzzle, r)
    check("改稿的谜底也一并采用", r.answer and "听声音" in r.answer, r.answer)
    check("改稿的提示也一并采用", r.hints and r.hints[0] == "和光有关", r.hints)
    check("没有触发第二次生成(共 3 次调用: 生成/审稿/再审)",
          len(fc.calls) == 3, len(fc.calls))


def test_hard_rule_asks_reviewer_to_fix():
    print("[审稿: 硬规则(第一人称)也交给审稿人改, 不直接丢]")
    # 第一人称是代码能确定判出来的, 但**能改**(换个人称而已)。
    # 所以不是丢掉重出, 而是点名让审稿人改。
    fc = FakeClient([
        LLMResult(tool_input={"puzzle": "我每晚都听见楼上有人走动。为什么?",
                              "answer": "楼上没人, 是他自己的回声。",
                              "hints": ["a", "b", "c"]}),
        # 审稿: 给出第三人称改稿
        LLMResult(tool_input={"ok": False, "note": "改成第三人称",
                              "puzzle": "他每晚都听见楼上有人走动。为什么?",
                              "answer": "楼上没人, 是他自己的回声。",
                              "hints": ["a", "b", "c"]}),
        LLMResult(tool_input={"ok": True}),
    ])
    w = PuzzleWriter(client=fc)
    r = w.gen_riddle()
    check("改成第三人称后被采用",
          r.puzzle and r.puzzle.startswith("他每晚"), r)
    # 审稿请求里应点名"第一人称"这个已知问题
    review_user = fc.calls[1]["user"]
    check("点名了第一人称问题", "第一人称" in review_user, review_user[-150:])


def test_reviewer_no_fix_falls_back_to_regen():
    print("[审稿: 审稿人没给改稿时, 退回重出]")
    fc = FakeClient([
        LLMResult(tool_input={"puzzle": "他每晚点灯却不让光照到自己。为什么?",
                              "answer": "他是盲人。", "hints": ["a", "b", "c"]}),
        # 审稿: 说不行, 但**没给改稿**
        LLMResult(tool_input={"ok": False, "note": "不好"}),
        # 只能重出一稿
        LLMResult(tool_input={"puzzle": "他每天数鸡蛋。为什么?",
                              "answer": "有人进过他家。", "hints": ["a", "b", "c"]}),
        LLMResult(tool_input={"ok": True}),
    ])
    w = PuzzleWriter(client=fc)
    r = w.gen_riddle()
    check("退回重出后采用新稿", r.puzzle and "鸡蛋" in r.puzzle, r)


def test_riddle_check_retries_empty_tool_use():
    print("[出题: 质检拿到空 tool_input 要重出]")
    # 网关抖动的表现: tool_use 块存在但 input 为空。
    # 这**不算通过**, 应该重出一稿 —— 否则质检形同虚设。
    fc = FakeClient([
        # 第一稿
        LLMResult(tool_input={"puzzle": "一稿谜面, 谁做了什么反常的事。为什么?",
                              "answer": "一稿谜底。", "hints": ["a", "b", "c"]}),
        # 质检: 空 tool_input(抖动)
        LLMResult(tool_input={}, text="I'll analyze this puzzle"),
        # 第二稿
        LLMResult(tool_input={"puzzle": "二稿谜面, 另一个具体事件。为什么?",
                              "answer": "二稿谜底。", "hints": ["a", "b", "c"]}),
        # 质检: 通过
        LLMResult(tool_input={"ok": True}),
    ])
    w = PuzzleWriter(client=fc)
    r = w.gen_riddle()
    check("空 tool_input 不算通过, 重出后成功",
          r.puzzle is not None and "二稿" in r.puzzle, r)
    check("共 4 次调用(生成/质检/生成/质检)", len(fc.calls) == 4, len(fc.calls))


def test_first_person_story_rejected():
    print("[出题: 第一人称叙事是故事, 不是谜题]")
    from story.llm import _is_first_person_story
    # 第一人称叙述 -> 判为不合格
    for t in ["深夜我独自在家，座机响了，接起来是我自己的声音。",
              "我住的老楼电梯里，只有我和邻居老太太两个人。",
              "我正在厨房做饭，突然听见门外有人叫我名字。"]:
        check(f"第一人称: {t[:14]}", _is_first_person_story(t) is True, t)
    # 第三人称(含引语里的"我") -> 合格
    for t in ["一个男人走进餐厅，点了一份海龟汤，喝了一口就冲出去自杀了。",
              "男人对酒保说：「请给我一杯水。」酒保却掏出一把枪指着他。",
              "她在葬礼上遇见一个陌生男人，回家后就把亲姐姐杀了。"]:
        check(f"第三人称: {t[:14]}", _is_first_person_story(t) is False, t)
    # 端到端: 第一人称那稿交给审稿人改(而不是直接弃用)
    fc = FakeClient([
        LLMResult(tool_input={"puzzle": "深夜我独自在家，座机响了，是我自己的声音。为什么?",
                              "answer": "梦游。", "hints": ["a", "b", "c"]}),
        # 审稿人: 改成第三人称
        LLMResult(tool_input={"ok": False, "note": "改成第三人称",
                              "puzzle": "一个男人深夜独自在家，座机响了，"
                                        "接起来是他自己的声音。为什么?",
                              "answer": "梦游。", "hints": ["a", "b", "c"]}),
        LLMResult(tool_input={"ok": True}),
    ])
    w = PuzzleWriter(client=fc)
    r = w.gen_riddle()
    check("第一人称那稿被审稿人改成第三人称",
          r.puzzle and r.puzzle.startswith("一个男人"), r)


def test_no_repeat_puzzles():
    print("[出题: 不重复]")
    from story.llm import _too_similar
    # 换个说法重讲同一题 -> 应判为太像
    a = "男人走进一家酒吧，对酒保说：请给我一杯水。酒保却从吧台下掏出一把枪指着他。"
    b = "男人走进酒吧，向酒保要一杯水。酒保却从吧台下面掏出一把枪对着他。"
    check("同题改写应被拦", _too_similar(b, [a]) != "", _too_similar(b, [a]))
    # 完全不同的题 -> 放行
    c = "她独自住在山上, 每天傍晚把一只空玻璃杯倒扣在窗台上。为什么?"
    check("不同题应放行", _too_similar(c, [a]) == "", _too_similar(c, [a]))
    # 端到端: 与已出过的题太像 -> 重出
    fc = FakeClient([
        LLMResult(tool_input={"puzzle": b + "为什么?", "answer": "打嗝。",
                              "hints": ["a", "b", "c"]}),
        LLMResult(tool_input={"ok": True}),           # 质检通过
        # 因为与 a 太像 -> 重出, 给一个全新的
        LLMResult(tool_input={"puzzle": c, "answer": "等一个人。",
                              "hints": ["a", "b", "c"]}),
        LLMResult(tool_input={"ok": True}),
    ])
    w = PuzzleWriter(client=fc)
    r = w.gen_riddle(avoid=[a])
    check("太像的那稿被弃用, 采用新题", r.puzzle == c, r.puzzle)


def test_english_riddle_rejected_on_text_path():
    print("[出题: 文本回退路径也要挡英文]")
    # 实测 bug: 中文检查只加在 tool_input 分支, 模型吐英文时从
    # **文本回退**分支溜上屏了 —— 第 1 题变成了
    # "I'll create a proper, self-contained lateral thinking puzzle…"
    fc = FakeClient([
        LLMResult(text="I'll create a proper, self-contained lateral thinking puzzle."),
        # 重试给个中文的
        LLMResult(tool_input={"puzzle": "一个男人走进餐厅，点了碗海龟汤就自杀了。为什么?",
                              "answer": "同伴的肉。", "hints": ["a", "b", "c"]}),
        LLMResult(tool_input={"ok": True}),
    ])
    w = PuzzleWriter(client=fc)
    r = w.gen_riddle()
    check("英文稿被弃用, 采用中文稿",
          r.puzzle is not None and "海龟汤" in r.puzzle, r)


def test_fallback_riddles_rotate():
    print("[兜底题不总是同一道]")
    from story.parser import FALLBACK_RIDDLES
    check("兜底题 >= 3 道", len(FALLBACK_RIDDLES) >= 3, len(FALLBACK_RIDDLES))
    texts = {p for p, _ in FALLBACK_RIDDLES}
    check("兜底题互不相同", len(texts) == len(FALLBACK_RIDDLES), texts)
    check("不再用'海龟汤'那道烂大街的",
          all("海龟汤" not in p for p, _ in FALLBACK_RIDDLES), texts)


def test_riddle_fallback():
    print("[出题: 工具不可用时回退解析]")
    fc = FakeClient([
        LLMResult(text=(
            "【谜面】\n雨夜有人敲门, 开门却没人。为什么?\n【谜底】\n是他自己的回声。\n"
            "【提示】\n提示一：注意天气。\n")),
        LLMResult(tool_input={"ok": True}),      # 自检通过
    ])
    w = PuzzleWriter(client=fc)
    r = w.gen_riddle()
    check("回退解析出谜面", "敲门" in (r.puzzle or ""), r)
    check("回退解析出谜底", "回声" in (r.answer or ""), r)


def test_answer_tool():
    print("[裁决: 强制工具]")
    fc = FakeClient([LLMResult(tool_input={
        "answers": [{"id": 7, "verdict": "是", "comment": "就差一点"}]})])
    w = PuzzleWriter(client=fc)
    res, err = w.answer("谜面", "谜底", [], 7, "甲", "是同伴的肉吗")
    check("拿到一条裁决", len(res) == 1, res)
    check("qid 用引擎给的", res[0].qid == 7, res)
    check("verdict 正确", res[0].verdict == "是", res)
    check("comment 保留", res[0].comment == "就差一点", res)


def test_answer_rejects_bad_enum():
    print("[裁决: 非法枚举被丢弃]")
    fc = FakeClient([LLMResult(tool_input={
        "answers": [{"id": 1, "verdict": "也许吧", "comment": "x"}]})])
    w = PuzzleWriter(client=fc)
    res, err = w.answer("谜面", "谜底", [], 1, "甲", "问题")
    check("非法裁决被拒(返回空)", res == [], res)
    check("带错误信息", err is not None, err)


def test_answer_enum_forced_by_schema():
    print("[裁决: 揭晓 是合法枚举, 但要裁判复核]")
    fc = FakeClient([
        LLMResult(tool_input={"answers": [{"id": 3, "verdict": "揭晓",
                                           "comment": "答对了！"}]}),
        LLMResult(tool_input={"is_guess": True, "cause_hit": True, "mechanism_hit": True}),      # 裁判确认
    ])
    w = PuzzleWriter(client=fc)
    res, _ = w.answer("谜面", "谜底", [], 3, "甲", "同伴的肉对吧")
    check("揭晓 + 裁判确认 -> 揭晓", res and res[0].verdict == "揭晓", res)


def test_open_question_never_solves():
    print("[开放疑问句不判猜中]")
    # 实测的 bug: "#他为什么跑" 是开放疑问(在问信息, 不在断言),
    # 却被裁判误判成猜中 -> 直接跳揭晓。
    from story.llm import _is_open_question
    for q in ["他为什么跑", "为什么他每天都要洗手", "他怎么做到的",
              "他是什么职业", "他几点出门"]:
        check(f"开放疑问: {q}", _is_open_question(q) is True, q)
    # 是非题必须**不**被误判为开放疑问(否则猜中永远不会触发)
    for q in ["他是不是瞎了", "同伴把肉给他吃了吗", "他是灯塔看守人吗",
              "他知道吗", "地铁上有人吗"]:
        check(f"是非题: {q}", _is_open_question(q) is False, q)
    # 端到端: 开放疑问不调裁判, 所以永远不会变成"揭晓"
    fc = FakeClient([
        LLMResult(tool_input={"answers": [{"id": 1, "verdict": "是"}]}),
        # 故意备一个"裁判说猜中"的返回; 若闸门失效, 它就会被消费掉
        LLMResult(tool_input={"is_guess": True, "cause_hit": True, "mechanism_hit": True}),
    ])
    w = PuzzleWriter(client=fc)
    res, _ = w.answer("谜面", "谜底", [], 1, "甲", "他为什么跑")
    check("开放疑问保持原裁决", res and res[0].verdict == "是", res)
    check("没有调裁判(只用了 1 次调用)", len(fc.calls) == 1,
          [c["tool"]["name"] for c in fc.calls])


def test_verdict_solve_must_pass_judge():
    print("[裁决自称揭晓也要过裁判复核]")
    # 实测 bug: 裁决阶段偶尔自作主张返回"揭晓"(日志里 "#为什么" -> 揭晓),
    # 原来的写法"已是揭晓就不问裁判"让误判一路直通。
    # 现在: 裁决给揭晓 -> 仍要裁判复核; 复核不通过就降级。
    fc = FakeClient([
        LLMResult(tool_input={"answers": [{"id": 1, "verdict": "揭晓"}]}),
        # 裁判否决
        LLMResult(tool_input={"is_guess": True, "cause_hit": False, "mechanism_hit": False}),
    ])
    w = PuzzleWriter(client=fc)
    res, _ = w.answer("谜面", "谜底", [], 1, "甲", "他是不是饿了")
    check("裁决自称揭晓但裁判否决 -> 降级",
          res and res[0].verdict != "揭晓", res)
    # 裁判确认 -> 保留揭晓
    fc2 = FakeClient([
        LLMResult(tool_input={"answers": [{"id": 1, "verdict": "揭晓"}]}),
        LLMResult(tool_input={"is_guess": True, "cause_hit": True, "mechanism_hit": True}),
    ])
    w2 = PuzzleWriter(client=fc2)
    res2, _ = w2.answer("谜面", "谜底", [], 1, "甲", "同伴的肉对吧")
    check("裁决揭晓且裁判确认 -> 保持揭晓",
          res2 and res2[0].verdict == "揭晓", res2)


def test_open_question_downgrades_solve():
    print("[疑问句的揭晓一律降级]")
    # 结构性硬规则: 疑问句在问信息, 不可能同时说出谜底。
    # 即使裁决给了揭晓, 也必须降级, 且**不调裁判**(逻辑上就说不通)。
    fc = FakeClient([
        LLMResult(tool_input={"answers": [{"id": 1, "verdict": "揭晓"}]}),
    ])
    w = PuzzleWriter(client=fc)
    res, _ = w.answer("谜面", "谜底", [], 1, "甲", "为什么")
    check("'为什么' 不给揭晓", res and res[0].verdict != "揭晓", res)
    check("没有多调裁判", len(fc.calls) == 1,
          [c["tool"]["name"] for c in fc.calls])


def test_open_question_with_hypothesis_can_solve():
    print("[带因果假设的疑问句不能一票否决]")
    # 实测 bug: 原来的规则是"含'为什么'就不可能 solved", 于是误伤了
    # 这种**已经给出完整因果假设**的句子 —— 它完全可能是正确答案,
    # 一票否决等于让说中的观众永远猜不中。
    from story.llm import _is_open_question
    # 带假设 -> 不是"纯索取", 必须送裁判
    for t in ["为什么他每天多待十五分钟, 是因为以前灯晚亮十五分钟出过事故吗？",
              "是不是有人死了",
              "他是不是在纪念谁",
              "说明他以前出过事故",
              "同伴把肉给他吃了吗"]:
        check(f"带假设: {t[:16]}", _is_open_question(t) is False, t)
    # 纯索取 -> 仍然挡下
    for t in ["为什么", "他怎么了", "什么"]:
        check(f"纯索取: {t[:10]}", _is_open_question(t) is True, t)
    # 端到端: 带假设的疑问句若裁决为揭晓, **不降级**(会走裁判复核)
    fc = FakeClient([
        LLMResult(tool_input={"answers": [{"id": 1, "verdict": "揭晓"}]}),
        LLMResult(tool_input={"is_guess": True, "cause_hit": True,
                              "mechanism_hit": True}),      # 裁判确认
    ])
    w = PuzzleWriter(client=fc)
    res, _ = w.answer("谜面", "谜底", [], 1, "甲",
                      "是不是因为他以前出过事故才这样的")
    check("带假设的疑问句不被降级",
          res and res[0].verdict == "揭晓", res)
    check("确实走了裁判复核(而不是被关键词一票否决)",
          len(fc.calls) == 2, [c["tool"]["name"] for c in fc.calls])


def test_llm_failure_returns_unavailable_not_irrelevant():
    print("[LLM 故障不能伪装成'无关']")
    # "无关"是断言"你的猜测与谜底无关" —— 那是**错误信息**, 会把观众的
    # 思路带偏。失败时必须给中性的"未判定"。
    from story import parser as P
    check("UNAVAILABLE 常量存在", P.UNAVAILABLE == "未判定", P.UNAVAILABLE)
    # 工具返回不可用 -> 结果里没有裁决 -> answer() 报错(上层转"未判定")
    fc = FakeClient([LLMResult(error="网关抖动")])
    w = PuzzleWriter(client=fc)
    res, err = w.answer("谜面", "谜底", [], 1, "甲", "他是盲人吗")
    check("失败时没有臆造裁决", res == [], res)
    check("带出错误信息", err is not None, err)


def test_judge():
    print("[裁判]")
    fc = FakeClient([LLMResult(tool_input={"is_guess": True, "cause_hit": True, "mechanism_hit": True}),
                     LLMResult(tool_input={"is_guess": True, "cause_hit": False, "mechanism_hit": False})])
    w = PuzzleWriter(client=fc)
    yes = w.judge("谜面", "谜底", "同伴的肉对吧").solved
    no = w.judge("谜面", "谜底", "他饿了吗").solved
    check("判中", yes is True, yes)
    check("判不中", no is False, no)


def test_hint_not_repeated():
    print("[提示: 不与已给过的重复]")
    from story.llm import _hint_repeated
    # 原样重复 / 换个说法重说 -> 都算重复
    for h in ["注意汤的味道", "汤的味道才是关键", "关键在于汤的味道"]:
        check(f"重复: {h}", _hint_repeated(h, ["注意汤的味道"]) is True, h)
    # 真的换个角度 -> 放行
    for h in ["他以前也喝过一次海龟汤", "想想同伴当年做了什么"]:
        check(f"放行: {h}", _hint_repeated(h, ["注意汤的味道"]) is False, h)
    # 端到端: 模型第一次给了重复的 -> 重出, 第二次给新的才采用
    fc = FakeClient([
        LLMResult(tool_input={"hint": "汤的味道才是关键"}),   # 与 given 重复
        LLMResult(tool_input={"hint": "想想他以前经历过什么"}),  # 新的
    ])
    w = PuzzleWriter(client=fc)
    h, _ = w.hint("谜面", "谜底", 2, given=["注意汤的味道"])
    check("重复的被打回, 采用新的", h == "想想他以前经历过什么", h)
    check("确实重出过", len(fc.calls) == 2, len(fc.calls))


def test_hint_and_reveal():
    print("[提示 / 揭晓]")
    fc = FakeClient([LLMResult(tool_input={"hint": "注意汤的味道。不剧透"}),
                     LLMResult(tool_input={"reveal": "谜底是同伴的肉汤骗局。"})])
    w = PuzzleWriter(client=fc)
    h, _ = w.hint("谜面", "谜底", 1, [])
    rv, _ = w.reveal("谜面", "谜底", "solved", "甲")
    check("提示解析", h == "注意汤的味道。不剧透", h)
    check("揭晓解析", rv == "谜底是同伴的肉汤骗局。", rv)


def test_tool_actually_requested():
    print("[每个调用都带强制工具]")
    # answer() 现在会先问裁决、再问裁判(judge), 所以要多备一个 judge 结果
    fc = FakeClient([
        LLMResult(tool_input={"answers": [{"id": 1, "verdict": "是"}]}),
        LLMResult(tool_input={"is_guess": True, "cause_hit": False, "mechanism_hit": False}),
        LLMResult(tool_input={"hint": "h"}),
        LLMResult(tool_input={"reveal": "r"}),
    ])
    w = PuzzleWriter(client=fc)
    w.answer("p", "a", [], 1, "甲", "q")
    w.hint("p", "a", 1, [])
    w.reveal("p", "a", "solved")
    names = [c["tool"]["name"] for c in fc.calls]
    check("工具序列正确",
          names == ["emit_verdict", "emit_judgement", "emit_hint", "emit_reveal"],
          names)


def test_answer_consults_judge():
    print("[裁决后自动问裁判]")
    # 裁决给"是", 但裁判说猜中了 -> 应升级为"揭晓"
    fc = FakeClient([
        LLMResult(tool_input={"answers": [{"id": 5, "verdict": "是"}]}),
        LLMResult(tool_input={"is_guess": True, "cause_hit": True, "mechanism_hit": True}),
    ])
    w = PuzzleWriter(client=fc)
    res, _ = w.answer("谜面", "谜底", [], 5, "甲", "同伴的肉对吧")
    check("裁判命中 -> 升级为揭晓", res and res[0].verdict == "揭晓", res)
    names = [c["tool"]["name"] for c in fc.calls]
    check("确实调了裁判", names == ["emit_verdict", "emit_judgement"], names)


def test_answer_judge_not_consulted_without_answer():
    print("[无谜底时不问裁判]")
    fc = FakeClient([
        LLMResult(tool_input={"answers": [{"id": 1, "verdict": "是"}]}),
    ])
    w = PuzzleWriter(client=fc)
    res, _ = w.answer("谜面", "", [], 1, "甲", "问题")   # answer 为空
    check("仍能裁决", res and res[0].verdict == "是", res)
    check("没多调裁判", len(fc.calls) == 1, [c["tool"]["name"] for c in fc.calls])


def test_reject_reasons_accumulate():
    print("[出题: 审稿人改不动时, 历次原因要累积传给下一稿]")
    # 审稿人给出改稿时走"就地修改"; 但它**没给改稿**时才退回重出 ——
    # 那时历次原因必须累积, 否则第 1 稿的"靠巧合"到第 3 稿就丢了,
    # 模型又踩回同一个坑(实测)。
    fc = FakeClient([
        # 第 1 稿: 审稿说"靠巧合", 但没给改稿 -> 退回重出
        LLMResult(tool_input={"puzzle": "他每天数鸡蛋。为什么?",
                              "answer": "恰好那天多一个。", "hints": ["a", "b", "c"]}),
        LLMResult(tool_input={"ok": False, "note": "谜底靠巧合"}),
        # 第 2 稿: 又不行, 也没给改稿
        LLMResult(tool_input={"puzzle": "她每天擦那扇窗。为什么?",
                              "answer": "她是保洁。", "hints": ["a", "b", "c"]}),
        LLMResult(tool_input={"ok": False, "note": "谜底没解释反常点"}),
        # 第 3 稿: 通过
        LLMResult(tool_input={"puzzle": "他每天擦那扇窗。为什么?",
                              "answer": "窗后是他亡妻的墓。", "hints": ["a", "b", "c"]}),
        LLMResult(tool_input={"ok": True}),
    ])
    w = PuzzleWriter(client=fc)
    r = w.gen_riddle()
    check("最终采用第 3 稿", r.puzzle is not None and "亡妻" in (r.answer or ""), r)
    # 第 3 稿的生成请求(第 5 次调用)里, **两条**原因都该在
    gen3 = fc.calls[4]["user"]
    check("带上了第 1 稿的原因(靠巧合)", "靠巧合" in gen3, gen3[-300:])
    check("带上了第 2 稿的原因(没解释反常点)", "没解释反常点" in gen3, gen3[-300:])


def test_rejected_puzzle_goes_into_avoid():
    print("[出题: 被毙掉的谜面要进 avoid, 避免同题材反复]")
    # 实测 bug: 被毙的稿子没进 avoid, 模型只从驳回理由里看到几个关键词,
    # 就顺着那个题材再写一个 —— 直播间连续 4 稿全是沙漠水壶。
    fc = FakeClient([
        LLMResult(tool_input={"puzzle": "男人在沙漠中醒来, 身边只有一个空水壶。为什么?",
                              "answer": "同伴喝光了。", "hints": ["a", "b", "c"]}),
        LLMResult(tool_input={"ok": False, "note": "不合格"}),   # 没给改稿
        LLMResult(tool_input={"puzzle": "她每天数楼梯的台阶。为什么?",
                              "answer": "她失明了。", "hints": ["a", "b", "c"]}),
        LLMResult(tool_input={"ok": True}),
    ])
    w = PuzzleWriter(client=fc)
    r = w.gen_riddle()
    check("第二稿被采用", r.puzzle and "楼梯" in r.puzzle, r)
    gen2 = fc.calls[2]["user"]
    check("第二稿的 prompt 里带了被毙的沙漠题", "沙漠" in gen2, gen2[-300:])


def test_bad_draft_does_not_consume_attempt():
    print("[出题: 废稿(英文/抖动)不消耗重试次数]")
    # 模型偶发吐英文自言自语 —— 那是废稿, 不该算掉一次机会,
    # 否则后面就没机会改了(实测: 3 稿全废然后退兜底)。
    fc = FakeClient([
        LLMResult(text="I'll create a fresh puzzle in objective third-person, avoiding..."),
        LLMResult(text="Let me think about a good puzzle."),
        LLMResult(tool_input={"puzzle": "他每天擦那扇窗。为什么?",
                              "answer": "窗后是他亡妻的墓。", "hints": ["a", "b", "c"]}),
        LLMResult(tool_input={"ok": True}),
    ])
    w = PuzzleWriter(client=fc)
    r = w.gen_riddle()
    check("两次废稿后仍能出题成功", r.puzzle is not None and "擦那扇窗" in r.puzzle, r)


def test_wrapped_tool_input_unwrapped():
    print("[出题: 网关套壳的 tool_input 要剥掉]")
    # 实测 bug: 网关偶尔返回 {"name":"emit_riddle","parameters":{...}},
    # 这时 d.get("puzzle") 是 None, 而 str(d) 含中文能骗过中文检查,
    # 结果**一整段 JSON 被当成谜面上屏**。
    from story.llm import _unwrap_tool_input
    wrapped = {"name": "emit_riddle",
               "parameters": {"puzzle": "他为什么每天绕路?", "answer": "前妻住那。"}}
    check("剥掉 parameters 壳", _unwrap_tool_input(wrapped).get("puzzle") == "他为什么每天绕路?",
          _unwrap_tool_input(wrapped))
    check("JSON 字符串也能剥",
          _unwrap_tool_input('{"name":"emit_riddle","parameters":{"puzzle":"x"}}')
          == {"puzzle": "x"})
    # 端到端: 套壳的一稿应被正确解析, 而不是当成 JSON 文本
    fc = FakeClient([
        LLMResult(tool_input={"name": "emit_riddle",
                              "parameters": {"puzzle": "他为什么每天绕路?",
                                             "answer": "前妻住那。",
                                             "hints": ["a", "b", "c"]}}),
        LLMResult(tool_input={"ok": True}),
    ])
    w = PuzzleWriter(client=fc)
    r = w.gen_riddle()
    check("套壳的一稿被正确解析",
          r.puzzle == "他为什么每天绕路?", r.puzzle)


def test_hints_not_leaked_into_puzzle():
    print("[出题: 谜面末尾混进的'提示/附注'要被切掉]")
    # 实测 bug: prompt 里写了"提示 3 条", 模型把提示写进了谜面的尾巴 ——
    # "...他为什么再也不敢走？ （提示：他两次都没停车。）" 直接上了直播。
    from story.llm import _strip_puzzle_tail, _looks_meta
    for src, want in [
        ("他为什么再也不敢走？ （提示：他两次都没停车。）", "他为什么再也不敢走？"),
        ("他为什么还要天天去？ 【谜面附注】谜底揭晓前，读者可先看到三条提示：",
         "他为什么还要天天去？"),
        ("他为什么哭?（注：注意他的职业）", "他为什么哭?"),
    ]:
        got = _strip_puzzle_tail(src)
        check(f"切掉尾巴: {want}", got == want, got)
        check("切完不再是 meta", _looks_meta(got) is False, got)
    # 正文里的"提示"二字不能被误伤
    keep = "他收到一条提示短信, 上面只有一个数字。为什么?"
    check("正文里的'提示'不误伤", _strip_puzzle_tail(keep) == keep,
          _strip_puzzle_tail(keep))


def test_reviewer_keeps_solve_atoms():
    print("[审稿: 改稿后 solve_atoms / fair_clues 不能丢]")
    # 实测踩过的最隐蔽的坑: 审稿人改稿后重建 RiddleResult 时只复制了
    # puzzle/answer/hints, atoms 被丢掉 —— 于是只要经过一次审稿, engine
    # 拿到的 _solve_atoms 就是空数组, judge 悄悄退回"凭感觉判"。
    # 新机制**看起来生效, 其实没有**。
    ATOMS = [{"role": "cause", "text": "退潮时礁石露出水面"},
             {"role": "mechanism", "text": "亮灯是标出礁石位置"}]
    CLUES = ["谜面写了'只在退潮亮灯'"]
    fc = FakeClient([
        # 出题: 带 atoms
        LLMResult(tool_input={"puzzle": "守塔人只在退潮亮灯。为什么?",
                              "answer": "礁石露出水面, 亮灯标位置。",
                              "hints": ["a", "b", "c"],
                              "solve_atoms": ATOMS, "fair_clues": CLUES}),
        # 审稿: 改了谜面, **原样带回 atoms**
        LLMResult(tool_input={"ok": False, "note": "补个具体地点",
                              "puzzle": "守塔人在海角灯塔, 只在退潮时亮灯。为什么?",
                              "answer": "礁石露出水面, 亮灯标位置。",
                              "hints": ["a", "b", "c"],
                              "solve_atoms": ATOMS, "fair_clues": CLUES}),
        # 再审: 通过
        LLMResult(tool_input={"ok": True}),
    ])
    w = PuzzleWriter(client=fc)
    r = w.gen_riddle()
    check("改稿被采用", r.puzzle and "海角" in r.puzzle, r)
    check("solve_atoms 经过审稿后仍在", r.solve_atoms == ATOMS, r.solve_atoms)
    check("fair_clues 经过审稿后仍在", r.fair_clues == CLUES, r.fair_clues)
    # 审稿请求里应该把现有 atoms 一并送过去了
    check("审稿请求带上了现有 atoms",
          "退潮时礁石露出水面" in fc.calls[1]["user"], fc.calls[1]["user"][-200:])


def test_reviewer_can_replace_atoms_when_answer_changes():
    print("[审稿: 改了谜底就必须重出 atoms]")
    NEW_ATOMS = [{"role": "cause", "text": "他打嗝"},
                 {"role": "mechanism", "text": "惊吓能止嗝"}]
    fc = FakeClient([
        LLMResult(tool_input={"puzzle": "他要一杯水, 酒保却掏枪。为什么?",
                              "answer": "旧答案(错的)。", "hints": ["a", "b", "c"],
                              "solve_atoms": [{"role": "cause", "text": "旧1"},
                                              {"role": "mechanism", "text": "旧2"}],
                              "fair_clues": ["旧线索"]}),
        # 审稿改了谜底, 同时给出**新的** atoms
        LLMResult(tool_input={"ok": False, "note": "谜底不对, 换成打嗝",
                              "puzzle": "他要一杯水, 酒保却掏枪。为什么?",
                              "answer": "他打嗝, 酒保掏枪吓他止嗝。",
                              "hints": ["a", "b", "c"],
                              "solve_atoms": NEW_ATOMS,
                              "fair_clues": ["他说了声谢谢"]}),
        LLMResult(tool_input={"ok": True}),
    ])
    w = PuzzleWriter(client=fc)
    r = w.gen_riddle()
    check("用了审稿人新给的 atoms", r.solve_atoms == NEW_ATOMS, r.solve_atoms)
    check("用了审稿人新给的 clues", r.fair_clues == ["他说了声谢谢"],
          r.fair_clues)


def test_main_regression_no_atoms_lost():
    print("[回归: reviewer 修 2 轮后 atoms 仍不为空]")
    # 这是 reviewer 特别要求的端到端测试: 生成带 atoms -> 审稿改题(两轮)
    # -> 最终 RiddleResult 仍有 atoms。
    ATOMS = [{"role": "cause", "text": "A1"},
             {"role": "mechanism", "text": "A2"}]
    fc = FakeClient([
        LLMResult(tool_input={"puzzle": "一稿。为什么?", "answer": "一稿底。",
                              "hints": ["a", "b", "c"],
                              "solve_atoms": ATOMS, "fair_clues": ["线索1"]}),
        LLMResult(tool_input={"ok": False, "note": "改1",
                              "puzzle": "二稿。为什么?", "answer": "二稿底。",
                              "hints": ["a", "b", "c"],
                              "solve_atoms": ATOMS, "fair_clues": ["线索1"]}),
        LLMResult(tool_input={"ok": False, "note": "改2",
                              "puzzle": "三稿。为什么?", "answer": "三稿底。",
                              "hints": ["a", "b", "c"],
                              "solve_atoms": ATOMS, "fair_clues": ["线索1"]}),
        LLMResult(tool_input={"ok": True}),
    ])
    w = PuzzleWriter(client=fc)
    r = w.gen_riddle()
    check("两轮修改后仍有谜题", bool(r.puzzle), r)
    check("两轮修改后 atoms 非空", r.solve_atoms == ATOMS, r.solve_atoms)
    check("两轮修改后 clues 非空", r.fair_clues == ["线索1"], r.fair_clues)


def test_atom_role_gate():
    print("[裁判: 说中机制就必须真的命中 mechanism atom]")
    # 只信模型给的 cause_hit/mechanism_hit, 等于把判断权又交回给它。
    # 加上"matched_atoms 必须包含 cause 和 mechanism"才是代码层校验。
    from story.llm import JudgeResult
    ATOMS = [{"role": "cause", "text": "退潮时礁石露出水面"},
             {"role": "mechanism", "text": "亮灯标礁石位置, 涨潮后误导船只"}]
    # ① 模型说都中了, 但 matched_atoms 只命中 support 之外的 0 -> 不通过
    fc = FakeClient([LLMResult(tool_input={
        "is_guess": True, "cause_hit": True, "mechanism_hit": True,
        "matched_atoms": [0]})])
    w = PuzzleWriter(client=fc)
    jr = w.judge("谜面", "谜底", "退潮时礁石露出来", ATOMS)
    check("只命中 cause 不算通关(缺 mechanism)", jr.solved is False,
          f"solved={jr.solved} hit={jr.matched_atoms}")
    # ② 两条都命中 -> 通过
    fc2 = FakeClient([LLMResult(tool_input={
        "is_guess": True, "cause_hit": True, "mechanism_hit": True,
        "matched_atoms": [0, 1]})])
    w2 = PuzzleWriter(client=fc2)
    jr2 = w2.judge("谜面", "谜底", "退潮礁石露出, 亮灯标位置, 涨潮误导", ATOMS)
    check("两条都命中 -> 通关", jr2.solved is True, jr2)
    # ③ 没有 role 的老数据 -> 不做 atom 校验, 不误杀
    fc3 = FakeClient([LLMResult(tool_input={
        "is_guess": True, "cause_hit": True, "mechanism_hit": True,
        "matched_atoms": []})])
    w3 = PuzzleWriter(client=fc3)
    jr3 = w3.judge("谜面", "谜底", "说清了", ["纯字符串1", "纯字符串2"])
    check("老格式(无 role)不误杀", jr3.solved is True, jr3)


def test_judge_technical_failure_not_downgraded_to_irrelevant():
    print("[裁判技术失败: 不能伪装成'无关', 也不能抹掉第一层裁决]")
    from story import parser as P
    # 第一层给了"揭晓", 第二层裁判技术失败 -> 必须是"未判定", 不能结束题
    fc = FakeClient([
        LLMResult(tool_input={"answers": [{"id": 1, "verdict": "揭晓"}]}),
        LLMResult(error="网关抖动"),          # 裁判失败, 无 tool_input 无 text
    ])
    w = PuzzleWriter(client=fc)
    res, _ = w.answer("谜面", "谜底", [], 1, "甲", "同伴的肉对吧", True, None)
    check("技术失败 -> 未判定(不是无关)", res[0].verdict == P.UNAVAILABLE, res)
    check("标记为 unavailable", res[0].status == "unavailable", res)
    # 第一层已经给了正常的"是" -> 复核失败不该把它抹掉
    fc2 = FakeClient([
        LLMResult(tool_input={"answers": [{"id": 2, "verdict": "是"}]}),
        LLMResult(error="网关抖动"),
    ])
    w2 = PuzzleWriter(client=fc2)
    res2, _ = w2.answer("谜面", "谜底", [], 2, "乙", "他是盲人吗", True, None)
    check("复核失败保留第一层的'是'", res2[0].verdict == "是", res2)


def test_check_tool_schema_matches_generator():
    """Q0.1: 审稿工具和出题工具的 solve_atoms **必须是同一种结构**。

    以前 _TOOL_CHECK 是 string[], _TOOL_RIDDLE 是 {role,text} 对象 ——
    审稿人一旦回传 atoms, role 信息就退化了, 而链路上看不出来。
    """
    from story.llm import _TOOL_CHECK, _TOOL_RIDDLE
    a = _TOOL_CHECK["input_schema"]["properties"]["solve_atoms"]
    b = _TOOL_RIDDLE["input_schema"]["properties"]["solve_atoms"]
    check("两边 items 类型一致", a["items"]["type"] == b["items"]["type"],
          (a["items"].get("type"), b["items"].get("type")))
    check("都是 object(带 role/text)", a["items"]["type"] == "object",
          a["items"])
    check("role 枚举一致",
          a["items"]["properties"]["role"]["enum"]
          == b["items"]["properties"]["role"]["enum"], a["items"])
    check("都要求 role+text",
          a["items"]["required"] == ["role", "text"] == b["items"]["required"],
          (a["items"]["required"], b["items"]["required"]))


def test_reviewer_structured_atoms_survive():
    """Q0.2: 审稿人回传结构化 atoms 时, **不能**被 str(dict) 损坏。

    以前 _check_riddle 里是 `str(a).strip()`, 对 {"role":...} 会变成
    "{'role': 'cause', 'text': '...'}" 那种字符串 —— 隐式数据损坏。
    """
    ATOMS = [{"role": "cause", "text": "退潮时礁石露出水面"},
             {"role": "mechanism", "text": "亮灯是标出礁石位置"}]
    fc = FakeClient([
        LLMResult(tool_input={"puzzle": "守塔人只在退潮亮灯。为什么?",
                              "answer": "礁石露出水面, 亮灯标位置。",
                              "hints": ["a", "b", "c"],
                              "solve_atoms": ATOMS, "fair_clues": ["线索"]}),
        # 审稿改了谜面, 原样带回**结构化** atoms
        LLMResult(tool_input={"ok": False, "note": "补地点",
                              "puzzle": "守塔人在海角, 只在退潮亮灯。为什么?",
                              "answer": "礁石露出水面, 亮灯标位置。",
                              "hints": ["a", "b", "c"],
                              "solve_atoms": ATOMS, "fair_clues": ["线索"]}),
        LLMResult(tool_input={"ok": True}),
    ])
    w = PuzzleWriter(client=fc)
    r = w.gen_riddle()
    check("atoms 仍是结构化对象", r.solve_atoms == ATOMS, r.solve_atoms)
    check("没有被 str() 损坏",
          all(isinstance(a, dict) for a in r.solve_atoms), r.solve_atoms)
    # 审稿请求里给它的现有 atoms 也该是 [role] text 形式
    check("审稿请求展示 role",
          "[cause]" in fc.calls[1]["user"], fc.calls[1]["user"][-300:])


def test_fallback_last_draft_keeps_atoms():
    """Q0.3: 所有稿都未通过时的**兜底稿**不能丢 atoms/clues。

    这是"拿最后一稿兜底"的路径 —— 少了 atoms, 新裁判链会悄悄退化成
    "凭一段文学谜底猜感觉", 而日志上完全正常。
    """
    ATOMS = [{"role": "cause", "text": "A1"},
             {"role": "mechanism", "text": "A2"}]
    CLUES = ["线索1"]
    bad = {"puzzle": "一稿。为什么?", "answer": "底。", "hints": ["a", "b", "c"],
           "solve_atoms": ATOMS, "fair_clues": CLUES}
    fc = FakeClient([
        LLMResult(tool_input=dict(bad)),
        LLMResult(tool_input={"ok": False, "note": "不行", "puzzle": "",
                              "answer": "", "hints": []}),
        LLMResult(tool_input=dict(bad, puzzle="二稿。为什么?")),
        LLMResult(tool_input={"ok": False, "note": "还是不行", "puzzle": "",
                              "answer": "", "hints": []}),
    ])
    w = PuzzleWriter(client=fc)
    r = w.gen_riddle(max_attempts=2)
    check("兜底稿仍有谜面", bool(r.puzzle), r)
    check("兜底稿 err 标记不合格", (r.error or "").startswith("不合格"), r.error)
    check("兜底稿**保住了** solve_atoms", r.solve_atoms == ATOMS, r.solve_atoms)
    check("兜底稿**保住了** fair_clues", r.fair_clues == CLUES, r.fair_clues)


def test_no_dead_judge_definition():
    """Q0.5: 不能留着返回 tuple 的旧 judge() 定义(误导性 dead code)。"""
    import inspect

    from story.llm import JudgeResult, PuzzleWriter
    src = inspect.getsource(PuzzleWriter.judge)
    check("judge 只定义一次", src.count("def judge(") == 1)
    check("judge 返回 JudgeResult",
          inspect.signature(PuzzleWriter.judge).return_annotation
          in (JudgeResult, "JudgeResult"),
          inspect.signature(PuzzleWriter.judge).return_annotation)
    # 旧定义会留下这个签名 —— 全局搜一遍确认没有第二份
    import story.llm as M
    whole = inspect.getsource(M)
    check("全文没有第二处 tuple[bool 签名的 judge",
          "-> tuple[bool, Optional[str]]" not in whole)


def main():
    for t in (test_riddle_tool, test_reviewer_fixes_in_place,
              test_hard_rule_asks_reviewer_to_fix,
              test_reviewer_no_fix_falls_back_to_regen,
              test_first_person_story_rejected,
              test_no_repeat_puzzles,
              test_riddle_check_retries_empty_tool_use, test_english_riddle_rejected_on_text_path,
              test_atom_role_gate,
              test_judge_technical_failure_not_downgraded_to_irrelevant,
              test_reviewer_keeps_solve_atoms,
              test_reviewer_can_replace_atoms_when_answer_changes,
              test_main_regression_no_atoms_lost,
              test_reject_reasons_accumulate, test_rejected_puzzle_goes_into_avoid,
              test_bad_draft_does_not_consume_attempt,
              test_wrapped_tool_input_unwrapped, test_hints_not_leaked_into_puzzle,
              test_fallback_riddles_rotate, test_riddle_fallback, test_answer_tool,
              test_answer_rejects_bad_enum, test_answer_enum_forced_by_schema,
              test_answer_consults_judge, test_answer_judge_not_consulted_without_answer,
              test_judge, test_verdict_solve_must_pass_judge,
              test_open_question_downgrades_solve, test_open_question_never_solves,
              test_open_question_with_hypothesis_can_solve,
              test_llm_failure_returns_unavailable_not_irrelevant,
              test_hint_not_repeated,
              test_hint_and_reveal, test_tool_actually_requested,
              # ---- Q0: 链路缺陷回归 ----
              test_check_tool_schema_matches_generator,
              test_reviewer_structured_atoms_survive,
              test_fallback_last_draft_keeps_atoms,
              test_no_dead_judge_definition):
        t()
    print()
    if FAIL[0]:
        print(f"FAILED: {FAIL[0]} 项")
        return 1
    print("PASS: PuzzleWriter(强制工具 + 回退解析) 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
