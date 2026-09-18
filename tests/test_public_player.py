#!/usr/bin/env python
# coding: utf-8
"""运行: uv run tests/test_public_player.py（完全离线, 无网络）。

Step 10: `PublicPlayerCore` 的**结构性**隔离。

这个套件不测"模型会不会偷看", 而是测"它有没有能力偷看":
    ① 不持有 PuzzleWriter
    ② 不持有 PuzzleSpec
    ③ API **不接收** answer / facts / solve_atoms / signature / coverage

用签名反射 + 属性检查钉住 —— 谁往这一层加一个隐藏区参数, 测试立刻红。
这是比"提示词里写了别看"强一档的保证: 前者靠模型自觉, 后者靠**构造**。
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from story import playtest as PT  # noqa: E402
from story.public_player import (  # noqa: E402
    PublicPlayerCore, build_prompt, sanitize_transcript,
)

FAIL = [0]

#: 隐藏区字段名 —— 这些**绝不允许**出现在公共层 API 的形参里。
HIDDEN_NAMES = (
    "answer", "facts", "solve_atoms", "signature", "coverage",
    "blueprint", "spec", "host_writer", "writer",
    "touched_fact_ids", "cause_hit", "mechanism_hit", "matched_atoms",
    "solution_candidate",
)


def check(name, cond, extra=""):
    if cond:
        print(f"  ok  {name}")
        return
    print(f"  FAIL {name}  {extra}")
    FAIL[0] += 1


class _R:
    def __init__(self, tool_input=None, error=None, text=None):
        self.tool_input = tool_input
        self.error = error
        self.text = text


class _Client:
    """最小 player client。记录收到的 prompt。"""

    def __init__(self, tool_input=None):
        self.tool_input = tool_input
        self.calls = []

    def messages(self, system, user, max_tokens=None, tool=None,
                 temperature=None, **kw):
        self.calls.append({"system": system, "user": user, "tool": tool,
                           "temperature": temperature})
        return _R(tool_input=self.tool_input)


# ======================================================================
def test_api_never_accepts_hidden_fields():
    """**核心**: 公共层的公开 API 签名里不能有隐藏区字段。"""
    print("\n[PP-1] API 签名不含隐藏区字段")
    funcs = {
        "build_prompt": build_prompt,
        "sanitize_transcript": sanitize_transcript,
        "PublicPlayerCore.ask": PublicPlayerCore.ask,
        "PublicPlayerCore.__init__": PublicPlayerCore.__init__,
    }
    for name, fn in funcs.items():
        params = set(inspect.signature(fn).parameters)
        bad = params & set(HIDDEN_NAMES)
        check(f"{name} 不含隐藏区参数", not bad, sorted(bad))


def test_core_holds_no_writer_or_spec():
    """① ② : 实例上不能挂 writer / spec 相关属性。"""
    print("\n[PP-2] 不持有 PuzzleWriter / PuzzleSpec")
    c = PublicPlayerCore(_Client())
    attrs = set(vars(c))
    check("没有 writer 属性",
          not any("writer" in a for a in attrs), sorted(attrs))
    check("没有 spec 属性",
          not any("spec" in a for a in attrs), sorted(attrs))
    check("没有 host 属性",
          not any("host" in a for a in attrs), sorted(attrs))
    # 公开层**故意**只持 client / 温度 / prompt / tool
    check("持有的就是那几个", attrs == {"client", "temperature",
                                        "system", "tool"}, sorted(attrs))


def test_core_module_does_not_import_writer():
    """公共层模块不能 import llm(那会把 PuzzleWriter 拖进来)。"""
    print("\n[PP-3] public_player 不依赖 llm / puzzle")
    import ast
    path = (Path(__file__).resolve().parents[1] / "story"
            / "public_player.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    # ⚠️ 不能用"文件里有没有出现 PuzzleWriter 这几个字"来判断 ——
    # 注释里**会**提到它(解释"为什么不持有")。必须只看**真实的 import**。
    imported: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imported.add(a.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            imported.add(mod)
            for a in node.names:
                imported.add(f"{mod}.{a.name}")
    blob = " ".join(sorted(imported))
    check("没有 import llm", "llm" not in blob.replace("public_player", ""),
          sorted(imported))
    check("没有 import puzzle", "puzzle" not in blob, sorted(imported))
    check("没有 import playtest",
          not any("playtest" in x for x in imported), sorted(imported))
    check("没有从 llm/puzzle 引入 PuzzleWriter/PuzzleSpec",
          not any(x.endswith(("PuzzleWriter", "PuzzleSpec"))
                  for x in imported), sorted(imported))


def test_sanitizer_strips_internal_coverage():
    """净化器必须剥掉内部覆盖信息, 只留 verdict + comment。"""
    print("\n[PP-4] 净化器剥掉覆盖信息")
    tr = [
        {"role": "puzzle", "text": "谜面"},
        {"role": "host", "text": "点评", "verdict": "是",
         "touched_fact_ids": ["f1", "f2"], "cause_hit": True,
         "mechanism_hit": True, "matched_atoms": ["a1"],
         "solution_candidate": True},
    ]
    pub = sanitize_transcript(tr)
    blob = repr(pub)
    for leak in ("f1", "f2", "a1", "cause_hit", "mechanism_hit",
                 "matched_atoms", "solution_candidate", "touched_fact_ids"):
        check(f"净化后不含 {leak}", leak not in blob, pub)
    check("保留了判决与点评",
          pub[1]["text"] == "主持人: 是（点评）", pub[1])


def test_build_prompt_ignores_extra_hidden_keys():
    """就算调用方递进带隐藏字段的记录, prompt 里也不能出现。"""
    print("\n[PP-5] prompt 里不出现隐藏字段值")
    tr = [{"role": "puzzle", "text": "谜面"},
          {"role": "host", "text": "", "verdict": "不是",
           "touched_fact_ids": ["SECRET_FACT"],
           "matched_atoms": ["SECRET_ATOM"]}]
    prompt = build_prompt("谜面", sanitize_transcript(tr))
    check("不含 SECRET_FACT", "SECRET_FACT" not in prompt, prompt)
    check("不含 SECRET_ATOM", "SECRET_ATOM" not in prompt, prompt)


def test_ask_only_sends_public_data():
    """端到端: `ask()` 发出去的 prompt 里没有隐藏区内容。"""
    print("\n[PP-6] ask() 只发公开数据")
    cli = _Client(tool_input={"kind": "ask", "text": "他是医生吗"})
    core = PublicPlayerCore(cli)
    tr = [{"role": "puzzle", "text": "谜面"},
          {"role": "host", "text": "点评", "verdict": "是",
           "touched_fact_ids": ["SECRET"]}]
    got = core.ask("谜面", tr)
    check("返回 (kind, text)", got == ("ask", "他是医生吗"), got)
    sent = cli.calls[0]["user"]
    check("prompt 里没有 SECRET", "SECRET" not in sent, sent)
    check("用了强制工具", cli.calls[0]["tool"] is not None)
    check("temperature=0(可复现)", cli.calls[0]["temperature"] == 0.0,
          cli.calls[0]["temperature"])


def test_ask_rejects_invalid_output():
    """输出不合法 -> None(技术失败), 不是崩, 也不是编一个。"""
    print("\n[PP-7] 非法输出 -> None")
    for ti in (None, {}, {"kind": "乱写的", "text": "x"},
               {"kind": "ask", "text": ""}):
        core = PublicPlayerCore(_Client(tool_input=ti))
        check(f"非法 {ti!r} -> None", core.ask("谜面", []) is None)
    # client 报错也一样
    class _Err:
        def messages(self, **kw):
            return _R(error="网关炸了")
    check("client 报错 -> None",
          PublicPlayerCore(_Err()).ask("谜面", []) is None)


def test_playtest_reuses_public_layer():
    """试玩必须**复用**公共层, 而不是自己再实现一套。"""
    print("\n[PP-8] playtest 复用公共层")
    # 模块级别名指向公共层的同一对象
    check("_player_prompt 就是公共层那个",
          PT._player_prompt is build_prompt, PT._player_prompt)
    check("_public_transcript 就是公共层那个",
          PT._public_transcript is sanitize_transcript)
    check("_PLAYER_SYSTEM 来自公共层",
          PT._PLAYER_SYSTEM == "你在玩一个情境推理谜题。你会看到谜面和主持人此前公开回答过的\n"
          "问答记录。你不知道谜底。\n\n规则:\n"
          "- 一次只问一个最有信息量的问题, 用来排除可能性。\n"
          "- 主持人只会回答 是 / 不是 / 无关, 偶尔附一句点评。\n"
          "- 当你觉得已经能解释谜面里那个反常现象时, 用 kind=solve 给出**完整的\n"
          "  因果解释**(把\"发生了什么\"和\"为什么会这样\"连起来), 而不是继续问细节。\n"
          "- 只有在确实推不动时才用 kind=give_up。\n\n"
          "只输出一句下一轮要说的话。不要复述已经问过的内容。",
          PT._PLAYER_SYSTEM[:40])
    # Playtester 实例上有一个 PublicPlayerCore
    pt = PT.Playtester(player_client=_Client(), host_writer=object())
    check("Playtester 持有 PublicPlayerCore",
          isinstance(pt.player, PublicPlayerCore), type(pt.player))


def main():
    tests = [
        test_api_never_accepts_hidden_fields,
        test_core_holds_no_writer_or_spec,
        test_core_module_does_not_import_writer,
        test_sanitizer_strips_internal_coverage,
        test_build_prompt_ignores_extra_hidden_keys,
        test_ask_only_sends_public_data,
        test_ask_rejects_invalid_output,
        test_playtest_reuses_public_layer,
    ]
    for t in tests:
        t()
    print()
    if FAIL[0]:
        print(f"FAILED: {FAIL[0]} 项")
        return 1
    print("PASS: PublicPlayerCore 结构性隔离全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
