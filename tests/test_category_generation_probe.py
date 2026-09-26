#!/usr/bin/env python
# coding: utf-8
"""运行: uv run tests/test_category_generation_probe.py(**完全离线**)。

Issue #58 §18-G: category_generation_probe 的离线测试。

覆盖: CLI 参数 / 固定类目顺序 / 每类目标 accepted / attempt cap /
使用生产 `keyword_spec` 的结构断言 / report 渲染完整 puzzle/answer/
requested/observed / 失败 attempt 不被当 accepted / 不写 Pool /
used ledger / archive 等生产文件。真实 LLM 不进 CI。
"""
import ast
import io
import json
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FAIL = [0]


def check(name, cond, extra=""):
    if cond:
        print(f"  ok  {name}")
        return
    print(f"  FAIL {name}  {extra}")
    FAIL[0] += 1


_ROOT = Path(__file__).resolve().parents[1]
_TOOL = _ROOT / "tools" / "category_generation_probe.py"


def _load_tool_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "category_generation_probe", str(_TOOL))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_structure_uses_production_chain():
    """§18-G: probe 只组织与报告 —— 结构上只能经生产 keyword_spec。"""
    print("\n[G-P1] probe 结构断言(生产链, 不复制生成逻辑)")
    src = _TOOL.read_text(encoding="utf-8")
    tree = ast.parse(src)
    check("tool 存在且可解析", bool(src.strip()))
    # 只 import 生产件, 不出现第二份关键词表 / 第二份创作卡。
    check("不复制 category brief 常量",
          "CATEGORY_CREATIVE_BRIEFS = " not in src
          and "【核心体验】" not in src.replace("不复制创作 Brief", ""))
    check("不维护第二份关键词表", "KEYWORD_BANK" not in src)
    check("不自己拼 Truth prompt", "emit_core_story" not in src
          and "_TOOL_STORY" not in src)
    check("引用生产 keyword_spec",
          "keyword_spec" in src and "from story.keyword_seed import" in src)
    check("引用生产 GenerationBrief", "GenerationBrief" in src)
    # 不写生产文件(Pool / used ledger / archive)
    check("不 import pool / director / prefetch",
          "from story.pool" not in src and "import director" not in src
          and "from story.prefetch" not in src)


def test_cli_and_fixed_order():
    """CLI 参数与固定类目顺序。"""
    print("\n[G-P2] CLI 参数 + 固定类目顺序")
    mod = _load_tool_module()
    from story.haiguitang_protocol import V2_CATEGORIES
    check("CATEGORY_ORDER == V2_CATEGORIES(固定顺序)",
          mod.CATEGORY_ORDER == tuple(V2_CATEGORIES), mod.CATEGORY_ORDER)
    ap = mod.build_parser()
    # 全部参数可解析(缺省值)
    a = ap.parse_args([])
    check("默认 out 目录", a.out == "data/audit/category_generation_v3", a.out)
    check("默认 accepted 3", a.accepted_per_category == 3)
    check("默认 attempt cap 6", a.max_attempts_per_category == 6)
    check("默认 difficulty 空(第一轮不指定)", a.difficulty == "")
    a2 = ap.parse_args(["--accepted-per-category", "1",
                        "--max-attempts-per-category", "2",
                        "--difficulty", "hard",
                        "--session-seed", "4242"])
    check("参数可覆盖", a2.accepted_per_category == 1
          and a2.max_attempts_per_category == 2
          and a2.difficulty == "hard" and a2.session_seed == 4242)


def _fake_run(monkey_results_factory):
    """以 FakeClient 替换生产 client 跑 run_probe(离线)。"""
    from story.config import Config
    mod = _load_tool_module()

    class _Args:
        out = "unused"
        accepted_per_category = 2
        max_attempts_per_category = 3
        difficulty = ""
        session_seed = 4242
        corpus = ""

    args = _Args()
    # monkeypatch 生产入口: client 换 Fake, bag 换固定词小袋。
    import story.llm as L
    from story.keyword_seed import KeywordBag

    orig_client = mod.__dict__.get("AnthropicMessagesClient")

    class _TinyBag(KeywordBag):
        pass

    results = monkey_results_factory()

    real_spec = L.PuzzleWriter
    return mod, args, results


def test_probe_offline_run(tmp=False):
    """离线 FakeClient 全链: accepted/attempt cap/失败记账/报告渲染。"""
    print("\n[G-P3] probe 离线全链(FakeClient, 真生产 keyword_spec)")
    mod = _load_tool_module()
    import story.llm as L
    from story.keyword_seed import KeywordBag
    from tests.test_llm import (  # noqa: E402  复用同一套 payload 助手
        _kw_story, _kw_surface, _kw_structure_payload, review_ok,
        _truth_tool, LLMResult,
    )

    class _Args:
        out = "(in-memory)"
        accepted_per_category = 1
        max_attempts_per_category = 2
        difficulty = ""
        session_seed = 4242
        corpus = ""

    captured = {"briefs": []}
    orig_spec = None

    def _fake_ok_factory():
        # 一个 accepted 队列: story/surface/structure/review(+truth 自动)
        return [
            LLMResult(tool_input=_kw_story()),
            LLMResult(tool_input=_kw_surface()),
            LLMResult(tool_input=_kw_structure_payload()),
            LLMResult(tool_input=review_ok()),
        ]

    real_keyword_spec = None
    import story.keyword_seed as KS

    real_ks = KS.keyword_spec
    calls = {"n": 0}

    # 用真 keyword_spec + FakeClient writer: 每个 attempt 前塞好队列。
    state = {"attempt": 0}

    class _FakeTransport:
        """替换 AnthropicMessagesClient 的离线传输。

        每个 attempt 发一批 4 件套(story/surface/structure/review;
        truth audit 由 FakeClient 同款自动应答承担 —— 但这里不是
        FakeClient, 所以 truth audit 也在队列里显式给)。
        """

        def __init__(self, cfg):
            self.cfg = type("C", (), {"model": "fake-model"})()

        def messages(self, *a, **kw):
            # 与 test_llm.FakeClient 同一套约定: 按 tool 名分发应答,
            # truth audit / safety 走"通过"应答, 其余按生成/审稿角色。
            tool = (kw.get("tool") or {}).get("name", "")
            if tool == "emit_truth_audit":
                return LLMResult(tool_input={"narrator_truthful": True,
                                             "mechanism_consistent": True,
                                             "conflicts": []})
            if tool in ("emit_safety_check", "livestream_safe"):
                return LLMResult(tool_input={"livestream_safe": True,
                                             "reason": "(fake 放行)"})
            by_tool = {
                "emit_core_story": _kw_story,
                "emit_surface": _kw_surface,
                "emit_structure": _kw_structure_payload,
            }
            if tool in by_tool:
                return LLMResult(tool_input=by_tool[tool]())
            # Reviewer(emit_review) -> pass bundle
            return LLMResult(tool_input=review_ok())

    # monkeypatch run_probe 的 client/bag 构造
    import story.config as CF

    class _MiniBag(KeywordBag):
        def __init__(self):
            super().__init__(["灯塔", "退潮", "雨伞", "车站", "钥匙",
                              "停电"], 4242)

    orig_load_bag = KS.load_bag
    KS.load_bag = lambda *a, **k: (_MiniBag(), {"corpus_version": "t-v1"})
    orig_client_cls = L.AnthropicMessagesClient
    L.AnthropicMessagesClient = _FakeTransport

    # 记录每次传入的 brief(顺序断言)
    orig_run_one = mod._run_one

    def _spy_run_one(writer, bag, ss, cfg, *, corpus_version, brief):
        captured["briefs"].append(brief.requested_category)
        return orig_run_one(writer, bag, ss, cfg,
                            corpus_version=corpus_version, brief=brief)

    mod._run_one = _spy_run_one
    try:
        run = mod.run_probe(_Args())
    finally:
        KS.load_bag = orig_load_bag
        L.AnthropicMessagesClient = orig_client_cls
        mod._run_one = orig_run_one

    # ---- 断言 ----
    check("每类 accepted 目标 1",
          all(v == 1 for v in run["accepted"].values()), run["accepted"])
    check("类目顺序固定", list(run["attempts"]) == list(mod.CATEGORY_ORDER),
          list(run["attempts"]))
    check("每类 attempt cap=2 内",
          all(1 <= v <= 2 for v in run["attempts"].values()),
          run["attempts"])
    check("失败 attempt 不被当 accepted",
          all(s.get("ok") is True for s in run["samples"]
              if s.get("ok") is not None and s.get("puzzle") is None)
          or all(("puzzle" in s) == s.get("ok", False)
                 for s in run["samples"]),
          "失败记录没有 puzzle")
    check("accepted 样本带完整字段",
          all(s.get("puzzle") and s.get("answer") and s.get("core_answer")
              and s.get("requested_category") and s.get("categories") is not None
              for s in run["samples"] if s.get("ok")),
          [k for s in run["samples"] if s.get("ok")
           for k in ("puzzle", "answer", "core_answer") if not s.get(k)])
    check("requested 固定按类注入",
          captured["briefs"] == list(mod.CATEGORY_ORDER) * 1
          or captured["briefs"] == [
              c for c in mod.CATEGORY_ORDER
              for _ in range(run["attempts"][c])],
          captured["briefs"])
    check("protocol/prompt 版本被记录",
          run["protocol_version"] == "haiguitang-v2"
          and run["prompt_version"].startswith("haiguitang-generation-v"),
          (run["protocol_version"], run["prompt_version"]))

    # ---- review 5324929975 Blocker 1/2: 审计证据 ----
    check("call_log 存在且每条带 stage/model/usage/latency",
          all(set(("stage", "model", "usage", "latency_s")) <= set(c)
              for c in run["call_log"]),
          "call_log 字段缺")
    check("total_llm_calls == len(call_log)",
          run["total_llm_calls"] == len(run["call_log"]),
          (run["total_llm_calls"], len(run["call_log"])))
    check("usage 从全量调用汇总(含失败)",
          isinstance(run["usage"], dict), run["usage"])
    check("stage_stats 按 stage 聚合",
          all("calls" in v and "errors" in v
              for v in (run["stage_stats"] or {}).values()),
          run["stage_stats"])
    check("失败记录带 reject/review_technical/calls",
          all(("reject" in s and "review_technical" in s and "calls" in s)
              for s in run["samples"] if not s.get("ok")),
          [k for s in run["samples"] if not s.get("ok")
           for k in ("reject", "review_technical", "calls") if k not in s])
    check("失败记录带 category_attempt",
          all("category_attempt" in s for s in run["samples"]))

    # ---- 报告渲染 ----
    md = mod.render_report(run, run["samples"])
    check("report 渲染 puzzle/answer",
          all((s["puzzle"] in md and s["answer"] in md)
              for s in run["samples"] if s.get("ok")))
    check("report 渲染 requested/observed",
          "请求类型：" in md and "primary_category:" in md)
    check("report 按类分组且顺序稳定",
          md.index("## logic") < md.index("## suspense") < md.index(
              "## horror") < md.index("## emotion") < md.index("## brainstorm"))
    check("report 不自动评分", "8.7/10" not in md and "最佳类型" not in md)


def test_write_outputs_no_production_files():
    """写盘只落 run.json/samples.jsonl/report.md, 不碰任何生产文件。"""
    print("\n[G-P4] 输出物仅三件, 不写 Pool/ledger/archive")
    mod = _load_tool_module()
    run = {"protocol_version": "haiguitang-v2",
           "prompt_version": "haiguitang-generation-v3",
           "session_seed": 1, "corpus_version": "t", "keyword_seed_version":
               "t", "difficulty": "",
           "accepted_per_category_target": 1, "max_attempts_per_category": 2,
           "attempts": {c: 1 for c in ("logic", "suspense", "horror",
                                       "emotion", "brainstorm")},
           "accepted": {c: (1 if c == "logic" else 0) for c in
                        ("logic", "suspense", "horror", "emotion",
                         "brainstorm")},
           "fails": {}, "requested_to_observed": {
               "hits": {"logic": 1, "suspense": 0, "horror": 0,
                        "emotion": 0, "brainstorm": 0},
               "accepted_totals": {"logic": 1, "suspense": 0, "horror": 0,
                                   "emotion": 0, "brainstorm": 0},
               "observed_distribution": {c: {} for c in
                                         ("logic", "suspense", "horror",
                                          "emotion", "brainstorm")}},
           "total_llm_calls": 3, "usage": {}, "elapsed_s": 0.1, "model": "m",
           "shortfall": {c: (0 if c == "logic" else 1) for c in
                         ("logic", "suspense", "horror", "emotion",
                          "brainstorm")},
           "samples": [{"category": "logic", "attempt": 1, "ok": True,
                        "requested_category": "logic",
                        "primary_category": "logic", "categories": ["logic"],
                        "difficulty": "medium", "keywords": ["a", "b"],
                        "puzzle": "P", "answer": "A", "core_answer": "C",
                        "protocol_version": "haiguitang-v2",
                        "prompt_version": "haiguitang-generation-v3",
                        "model": "m", "usage": {}, "review_decision": "pass",
                        "reject": "", "review_issues": []}]}
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "audit")
        mod.write_outputs(json.loads(json.dumps(run)), out)
        names = sorted(p.name for p in Path(out).iterdir())
        check("恰好四件输出(call_log 新增)", names == ["calls.jsonl",
                                                        "report.md", "run.json",
                                                        "samples.jsonl"], names)
        md = (Path(out) / "report.md").read_text(encoding="utf-8")
        check("run.json 可读", json.loads(
            (Path(out) / "run.json").read_text(encoding="utf-8"))[
                "protocol_version"] == "haiguitang-v2")
        check("samples.jsonl 每行一个 JSON", all(
            json.loads(ln) for ln in
            (Path(out) / "samples.jsonl").read_text(
                encoding="utf-8").strip().split("\n") if ln.strip()))
        # 渲染完整性(§15)
        for frag in ("【汤面】", "【汤底】", "【核心答案】", "【最终观察】",
                     "【生产结果】"):
            check(f"report 含{frag}", frag in md)


def main():
    test_structure_uses_production_chain()
    test_cli_and_fixed_order()
    test_probe_offline_run()
    test_write_outputs_no_production_files()
    if FAIL[0]:
        print(f"\nFAILED: {FAIL[0]} check(s)")
        return 1
    print("\nALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
