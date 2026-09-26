"""运行: uv run tests/test_parser.py（完全离线, 无网络）。

样本全部是**实测的真实模型输出原文** —— 这些格式是 deepseek-v4.1-flash
实际吐出来的, 不是我们设想的。宽容解析器必须能全部吃下。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from story.parser import (Riddle, parse_answers, parse_riddle,  # noqa: E402
                          simplify_for_dedupe)
from story.state import PendingQ  # noqa: E402


def mkq(n: int) -> list[PendingQ]:
    return [PendingQ(qid=i + 1, user_id=f"u{i}", user_name=f"观众{i}",
                     text=f"问题{i + 1}") for i in range(n)]


def check(name, cond, extra=""):
    if cond:
        print(f"  ok  {name}")
        return 0
    print(f"  FAIL {name}  {extra}")
    return 1


def main():
    fail = 0

    # ---- 1. 五种实测的裁决形态 ----
    print("[裁决: 实测五形态]")
    shapes = [
        # (原文, 期望裁决) —— 全部来自真实模型输出
        ("1. **#他以前喝过海龟汤吗**  \n   **没有。** 他以前喝过的“海龟汤”其实是同伴的肉。", "不是"),
        ("1|是|好眼力", "是"),
        ("1. **#他哭了吗** → **是**  \n（他喝了汤之后哭了。）", "是"),
        ("1、不是，因为今天这碗汤的味道完全不对。", "不是"),
        ("1）无关", "不重要"),  # legacy 文本 -> 三态映射(Issue #65)
        ("1. **#汤里有他认识的人吗** → **是**  \n（有类似的元素）", "是"),
    ]
    for raw, want in shapes:
        res, un = parse_answers(raw, mkq(1))
        got = res[0].verdict if res else None
        fail += check(f"{raw[:26]!r} -> {want}", got == want, f"got={got}")

    # ---- 2. 优先级: '不是' 不能被 '是' 吃掉 ----
    print("[裁决: 优先级]")
    res, _ = parse_answers("1. 不是。", mkq(1))
    fail += check("'不是' 不误判为 '是'", res[0].verdict == "不是", res)
    res, _ = parse_answers("1. 没有，他没杀人。", mkq(1))
    fail += check("'没有' -> 不是", res[0].verdict == "不是", res)
    res, _ = parse_answers("1. 是的，确实如此。", mkq(1))
    fail += check("'是的' -> 是", res[0].verdict == "是", res)

    # ---- 3. 实测: 一次答 5 题、编号稳定、箭头分隔 ----
    print("[裁决: 批量 5 题]")
    live5 = """海龟汤游戏继续，我来逐一回答这五个问题：

1. **#他是自己走进餐厅的吗** → **是**
（他是自己走进餐厅的，行动能力正常。）

2. **#他哭了吗** → **是**
（他喝了汤之后哭了。）

3. **#他认识那个厨师吗** → **不是**

4. **#汤是热的吗** → **是**

5. **#他后面死了吗** → **是**
"""
    res, un = parse_answers(live5, mkq(5))
    got = {r.qid: r.verdict for r in res}
    want = {1: "是", 2: "是", 3: "不是", 4: "是", 5: "是"}
    fail += check("5 题编号锚定", got == want, f"got={got}")
    fail += check("无未答", un == [], un)

    # ---- 4. 实测: 长解释体(模型拒绝短答的原始形态) ----
    print("[裁决: 长解释体]")
    verbose = """这四个问题中，有三条是能够直接给出明确回答的，我先逐一解答。

1. **#他以前喝过海龟汤吗**
   **没有。** 他以前喝过的"海龟汤"其实是同伴的肉，并不是真正的海龟汤。

2. **#他是自杀吗**
   **是。** 谜面已经写明"喝一口自杀"。

3. **#今天天气好吗**
   **与谜底无关。** 这条属于无关问题。

4. **#同伴把自己的肉给他吃了对吗**
   **对。** 海难时，同伴骗他喝的是"海龟汤"，实际上那是同伴自己的肉。
"""
    res, _ = parse_answers(verbose, mkq(4))
    got = {r.qid: r.verdict for r in res}
    want = {1: "不是", 2: "是", 3: "不重要", 4: "是"}  # legacy「无关」->「不重要」
    fail += check("长解释体 4 题", got == want, f"got={got}")
    fail += check("点评被抽取", any(r.comment for r in res), [r.comment for r in res])

    # ---- 5. 揭晓判定 ----
    print("[裁决: 揭晓]")
    res, _ = parse_answers("1. **揭晓！** 你猜对了核心真相。", mkq(1))
    fail += check("'揭晓' -> 揭晓", res[0].verdict == "揭晓", res)
    res, _ = parse_answers("1. 答案是：同伴给他吃的是人肉。", mkq(1))
    fail += check("'答案是' -> 揭晓", res[0].verdict == "揭晓", res)

    # ---- 6. 零编号兜底: 只答第一条, 其余退回 ----
    print("[裁决: 零编号兜底]")
    res, un = parse_answers("不是，他不是盲人。", mkq(3))
    fail += check("无编号只答首条", len(res) == 1 and res[0].qid == 1, res)
    fail += check("其余退回", un == [2, 3], un)
    res, un = parse_answers("这真是一个好天气啊。", mkq(2))
    fail += check("无裁决则全退回", res == [] and un == [1, 2], (res, un))

    # ---- 7. 乱序 + 漏项 ----
    print("[裁决: 乱序与漏项]")
    out_of_order = "2. 不是\n3. 不是\n1. 是"       # 顺序颠倒
    res, un = parse_answers(out_of_order, mkq(3))
    got = {r.qid: r.verdict for r in res}
    fail += check("乱序仍按 qid 归位", got == {1: "是", 2: "不是", 3: "不是"}, got)
    missing = "1. 是\n3. 不是"                        # 漏了 2
    res, un = parse_answers(missing, mkq(3))
    got = {r.qid: r.verdict for r in res}
    fail += check("漏项正确识别", got == {1: "是", 3: "不是"} and un == [2], (got, un))

    # ---- 8. 越界块忽略 ----
    print("[裁决: 越界块]")
    res, _ = parse_answers("1. 是\n7. 不是\n9. 无关", mkq(2))
    fail += check("越界编号被忽略", len(res) == 1 and res[0].qid == 1, res)

    # ---- 8.5 实测: 块尾散文 + 重复列表(真实模型会画蛇添足) ----
    print("[裁决: 尾部污染与重复列表]")
    noisy = """根据你提供的谜面，我来回答这三个提问：

1. **#他是不是杀了人**
→ **是。** 谜底明确指出凶手是弟弟。

2. **#汤里是不是有毒**
→ **不是。** 没有下毒情节。

3. **#他还活着吗**
→ **不在了。** 画家已经死亡。

如果你是想让我用"海龟汤"那种是/否/无关的格式来回答，我可以这样回：

1. 他是不是杀了人 → **是**
2. 汤里是不是有毒 → **无关/不是**
3. 他还活着吗 → **不是（已死亡）**
"""
    res, un = parse_answers(noisy, mkq(3))
    got = {r.qid: r.verdict for r in res}
    fail += check("尾部散文不污染裁决",
                  got == {1: "是", 2: "不是", 3: "不是"}, f"got={got}")
    fail += check("重复列表不覆盖首个", un == [], un)
    fail += check("'不在了' 识别为不是", got.get(3) == "不是", got)
    fail += check("点评不含尾部散文",
                  all("格式来回答" not in r.comment for r in res),
                  [r.comment for r in res])

    # ---- 8.6 实测: 开头裁决不能被句中"不是"反转 ----
    print("[裁决: 开头优先]")
    # 真实模型输出: 以"是的"开头, 句尾却出现"不是根本原因"
    tricky = "1. 是的，味道和他记忆里的不一样——但这只是部分线索，不是根本原因。"
    res, _ = parse_answers(tricky, mkq(1))
    fail += check("开头'是的'不被句中'不是'反转",
                  res and res[0].verdict == "是", res)
    tricky2 = "1. 不是。他并不认识老板。"
    res, _ = parse_answers(tricky2, mkq(1))
    fail += check("开头'不是'仍然正确", res and res[0].verdict == "不是", res)
    tricky3 = "1. 无关。这和谜底没有关系。"
    res, _ = parse_answers(tricky3, mkq(1))
    fail += check("legacy'无关'文本映射到'不重要'(Issue #65)",
                  res and res[0].verdict == "不重要", res)

    # ---- 9. 谜题解析: 四种实测变体 ----
    print("[谜题: 实测变体]")
    v1 = """【谜面】
一个男人走进餐厅，点了一碗海龟汤，喝了一口就冲出去自杀了。为什么？
【谜底】
多年前他遭遇海难，同伴给他喝的其实是同伴自己的肉。
【提示】
提示一：注意他点的是什么汤。
提示二：问题在这碗汤的味道。
"""
    r = parse_riddle(v1)
    fail += check("【谜面】/【谜底】", "男人走进餐厅" in r.puzzle and "海难" in r.answer, r)
    fail += check("提示 2 条", len(r.hints) == 2, r.hints)
    fail += check("提示前缀被剥", r.hints[0].startswith("注意"), r.hints)

    v2 = """**汤面：**
她每天给丈夫做饭，丈夫却越来越瘦。

**汤底：**
她在饭里下了药，让丈夫失去味觉。

**提示：**
1. 问题不在饭量。
2. 注意"味道"。
"""
    r = parse_riddle(v2)
    fail += check("**汤面：** 变体", "越来越瘦" in r.puzzle, r)
    fail += check("**汤底：** 变体", "味觉" in r.answer, r)
    fail += check("数字提示前缀被剥", r.hints and r.hints[0].startswith("问题"), r.hints)

    v3 = """## 海龟汤谜题：《最后一条消息》

**汤面：** 男人独自坐在漆黑的房间里，手机上妻子最后一条消息是"我到楼下了"。

<details>
<summary>🔍 提示</summary>

提示一：妻子并没有死。
提示二：男人去厨房不是为了做饭。

</details>

<details>
<summary>🧩 汤底</summary>

妻子在很久以前发完那条消息后遭遇意外，凶手用她的手机继续发消息。

</details>

希望这个谜题让你满意！
"""
    r = parse_riddle(v3)
    fail += check("details/summary 被剥且内容保留",
                  "漆黑的房间" in r.puzzle and "凶手" in r.answer, r)
    fail += check("html 标签已清除", "<details>" not in r.puzzle, r.puzzle[:60])
    fail += check("标题被抽出", "最后一条消息" in r.title or r.title == "", r.title)

    # ---- 10. 谜题兜底: 无标记 ----
    print("[谜题: 兜底]")
    r = parse_riddle("一个人每天都要数楼梯，有一天他不数了就死了。")
    fail += check("无标记 -> 整段当谜面", "数楼梯" in r.puzzle, r)
    fail += check("无谜底 -> 记 error", r.error is not None, r)
    r = parse_riddle("")
    fail += check("空输入 -> error", r.error is not None and r.puzzle == "", r)

    # 只给谜底不给谜面: 谜底之前的内容当谜面
    r = parse_riddle("他在电梯里跳了一下。\n【谜底】他其实在货梯里，跳一下触发了超载警报。")
    fail += check("仅有谜底标记", "电梯" in r.puzzle and "超载" in r.answer, r)

    # ---- 10.5 实测: 括号变体 + details 包裹(真实模型输出) ----
    print("[谜题: 括号变体(实测)]")
    v4 = """## 海龟汤谜题：最后的画作

**汤面（谜面）：**

一位画家在完成最后一幅画后，用画笔蘸满颜料，在画布角落写下了一个词。

**请提问。**

---

<details>
<summary>点此查看汤底</summary>

**汤底（谜底）：**

他画的是一位盲人，而他没有注意到盲人其实看得见。

</details>
"""
    r = parse_riddle(v4)
    fail += check("汤面（谜面）： 变体", "画家" in r.puzzle and "盲人" not in r.puzzle, r)
    fail += check("汤底（谜底）： 变体", "盲人" in r.answer, r)
    fail += check("谜面不含'请提问'", "请提问" not in r.puzzle, r.puzzle[-30:])

    # ---- 11. 去重 key ----
    print("[去重]")
    fail += check("#他是盲人吗 / #他是盲人吗！！ 同 key",
                  simplify_for_dedupe("#他是盲人吗") == simplify_for_dedupe("#他是盲人吗！！"))
    fail += check("大小写与标点无关",
                  simplify_for_dedupe("#He is Blind?") == simplify_for_dedupe("#he is blind"))
    fail += check("不同问题不同 key",
                  simplify_for_dedupe("#他是盲人吗") != simplify_for_dedupe("#他聋了吗"))

    print()
    if fail:
        print(f"FAILED: {fail} 项")
        return 1
    print("PASS: 裁决/谜题/去重 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
