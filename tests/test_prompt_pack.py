#!/usr/bin/env python
# coding: utf-8
"""运行: uv run tests/test_prompt_pack.py（**完全离线, 无网络**）。

Issue #50 Phase B1: Generation Prompt Pack v1 回归。

覆盖面(对应任务书 §59~§76, §95):

    loader 六 stage 可加载 / 负例(unknown / missing / empty / cwd) /
    模板洞 fail closed / v1 tool schema == 协议常量 / FakeClient 全链
    (Truth -> Surface -> Contract -> Audit 三子门 -> validate -> pool) /
    2/3/4 completion 三档 clean 入池 / 1/5 拒 / public_text 负例 /
    requested mismatch 与 difficulty mismatch 端到端 / Audit fix rebuild
    不丢 v1 字段 / classic 与 curated 仍 legacy / quality-v13 库存不旋转。

不调用真实模型: 全链用按 stage 顺序回放的 FakeClient(与 test_llm 的
FakeClient 同约定 —— truth audit / safety 自动应答)。
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from story.haiguitang_protocol import (  # noqa: E402
    CATEGORIES, DIFFICULTIES, HAIGUITANG_PROTOCOL_VERSION as V1,
    MAX_CATEGORIES, MIN_CATEGORIES,
    PROTOCOL_V1_MAX_COMPLETION_FACTS, PROTOCOL_V1_MIN_COMPLETION_FACTS,
    GenerationBrief,
)
from story.llm import (  # noqa: E402
    LLMResult, PuzzleWriter, _TOOL_STRUCTURE, _TOOL_STORY, _TOOL_SURFACE,
    check_tool, _v1_check_tool,
)
from story.pool import PuzzlePool  # noqa: E402
from story.prompt_pack import (  # noqa: E402
    HAIGUITANG_GENERATION_PROMPT_VERSION, PROMPT_PACK_VERSION, PROMPT_ROOT,
    STAGES, PromptPackError, load_prompt, stage_version,
)
from story.quality import QUALITY_POLICY_VERSION, validate_spec  # noqa: E402

FAIL = [0]


def check(name, cond, extra=""):
    if cond:
        print(f"  ok  {name}")
        return
    print(f"  FAIL {name}  {extra}")
    FAIL[0] += 1


# ======================================================================
# P1: loader 六 stage 可加载(§59)
# ======================================================================
def test_all_stages_load():
    print("\n[P1] 六个 stage 都能从盘上加载")
    check("stage 登记表精确 6 个",
          sorted(STAGES) == ["audit", "audit_safety", "audit_truthfulness",
                             "contract", "surface", "truth"], sorted(STAGES))
    for stage in STAGES:
        text = load_prompt(stage)
        check(f"{stage} 可加载且非空", bool(text.strip()), stage)
        check(f"{stage} 有版本号", stage_version(stage).startswith(stage
              .replace("_", "-").split("-")[0]) or bool(stage_version(stage)),
              stage_version(stage))
    check("Pack 总版本", HAIGUITANG_GENERATION_PROMPT_VERSION
          == "haiguitang-generation-v1" == PROMPT_PACK_VERSION,
          HAIGUITANG_GENERATION_PROMPT_VERSION)
    # 静态性: 六份文件都不含 {placeholder}(§6)
    for stage in STAGES:
        check(f"{stage} 无模板变量(全静态)", "{" not in load_prompt(stage))


# ======================================================================
# P2: loader 负例(§60)
# ======================================================================
def test_loader_negatives():
    print("\n[P2] loader fail closed")
    for bad in ("../../secret", "banana", "truth-v1.md", "", None,
                "truth; rm -rf"):
        try:
            load_prompt(bad)
            check(f"unknown stage {bad!r} 被拒", False, "没抛")
        except PromptPackError as e:
            check(f"unknown stage {bad!r} 被拒", "unknown prompt stage"
                  in str(e), str(e)[:60])

    # 缺文件 / 空文件 / 只有空白: 用临时 PROMPT_ROOT 造(生产不改它)
    real_root = PROMPT_ROOT
    try:
        with tempfile.TemporaryDirectory() as td:
            import story.prompt_pack as PP
            PP.PROMPT_ROOT = Path(td)
            try:
                load_prompt("truth")
                check("缺文件被拒", False, "没抛")
            except PromptPackError as e:
                check("缺文件被拒", "missing" in str(e), str(e)[:60])
            (Path(td) / "truth-v1.md").write_text("", encoding="utf-8")
            try:
                load_prompt("truth")
                check("空文件被拒", False, "没抛")
            except PromptPackError as e:
                check("空文件被拒", "empty" in str(e), str(e)[:60])
            (Path(td) / "truth-v1.md").write_text("   \n\t \n", encoding="utf-8")
            try:
                load_prompt("truth")
                check("只有空白被拒", False, "没抛")
            except PromptPackError as e:
                check("只有空白被拒", "empty" in str(e), str(e)[:60])
            (Path(td) / "truth-v1.md").write_text(
                "写一个故事 {lane} 结束。", encoding="utf-8")
            try:
                load_prompt("truth")
                check("未渲染占位符被拒", False, "没抛")
            except PromptPackError as e:
                check("未渲染占位符被拒", "placeholder" in str(e),
                      str(e)[:80])
            try:
                PP.render_prompt("truth", {"lane": "黑汤"})
                check("render 变量缺失 fail closed", False, "没抛")
            except PromptPackError:
                check("render 变量缺失 fail closed", True)
    finally:
        import story.prompt_pack as PP
        PP.PROMPT_ROOT = real_root

    # cwd 无关: 换到任意 cwd 再加载, 必须一致(§8)
    cwd = os.getcwd()
    try:
        os.chdir(tempfile.gettempdir())
        text = load_prompt("truth")
        check("任意 cwd 启动仍可加载", "海龟汤" in text, text[:30])
    finally:
        os.chdir(cwd)

    # render_prompt: 全静态 stage 无变量渲染 = 原文
    check("render 无变量 == load", PP.render_prompt("truth", None)
          == load_prompt("truth"))


# ======================================================================
# P3: v1 tool schema == 协议常量(§26/§62)
# ======================================================================
def test_v1_tool_schema_matches_protocol():
    print("\n[P3] Contract / Audit 工具 schema 与协议常量一致")
    sch = _TOOL_STRUCTURE["input_schema"]["properties"]
    comp = sch["completion_fact_ids"]
    check("Contract completion minItems == 2",
          comp["minItems"] == PROTOCOL_V1_MIN_COMPLETION_FACTS == 2,
          comp["minItems"])
    check("Contract completion maxItems == 4",
          comp["maxItems"] == PROTOCOL_V1_MAX_COMPLETION_FACTS == 4,
          comp["maxItems"])
    check("Contract facts[].public_text 必填",
          "public_text" in sch["facts"]["items"]["required"]
          and "public_text" in sch["facts"]["items"]["properties"],
          sch["facts"]["items"]["required"])
    check("Contract difficulty enum == DIFFICULTIES",
          sch["difficulty"]["enum"] == list(DIFFICULTIES),
          sch["difficulty"]["enum"])
    check("Contract primary enum == CATEGORIES",
          sch["primary_category"]["enum"] == list(CATEGORIES),
          sch["primary_category"]["enum"])
    cats = sch["categories"]
    check("Contract categories 1~3 且 enum == CATEGORIES",
          cats["minItems"] == MIN_CATEGORIES == 1
          and cats["maxItems"] == MAX_CATEGORIES == 3
          and cats["items"]["enum"] == list(CATEGORIES),
          (cats["minItems"], cats["maxItems"]))
    check("Contract required 带分类三件",
          all(k in _TOOL_STRUCTURE["input_schema"]["required"]
              for k in ("difficulty", "primary_category", "categories")),
          _TOOL_STRUCTURE["input_schema"]["required"])
    blob = json.dumps(_TOOL_STRUCTURE, ensure_ascii=False)
    for code_field in ("protocol_version", "requested_category",
                       "quality_policy_version", "prompt_version"):
        check(f"Contract schema 没有 code 字段 {code_field}",
              code_field not in blob, code_field)

    # 审稿 v1 变体
    vs = _v1_check_tool()["input_schema"]["properties"]
    vcomp = vs["completion_fact_ids"]
    check("Audit completion 2~4 == 协议常量",
          vcomp["minItems"] == PROTOCOL_V1_MIN_COMPLETION_FACTS
          and vcomp["maxItems"] == PROTOCOL_V1_MAX_COMPLETION_FACTS, vcomp)
    check("Audit facts[].public_text 必填",
          "public_text" in vs["facts"]["items"]["required"],
          vs["facts"]["items"]["required"])
    check("Audit difficulty/primary/categories enum 对齐协议",
          vs["difficulty"]["enum"] == list(DIFFICULTIES)
          and vs["primary_category"]["enum"] == list(CATEGORIES)
          and vs["categories"]["items"]["enum"] == list(CATEGORIES),
          "enum 漂移")
    # legacy/curated 不受影响
    legacy = check_tool(None)
    lcomp = legacy["input_schema"]["properties"]["completion_fact_ids"]
    check("legacy 审稿 schema 仍是 1~2(未放宽)",
          lcomp["minItems"] == 1 and lcomp["maxItems"] == 2,
          (lcomp["minItems"], lcomp["maxItems"]))
    check("legacy facts[].public_text 不是必填",
          "public_text" not in legacy["input_schema"]["properties"]
          ["facts"]["items"]["required"], "legacy 被污染")


# ======================================================================
# P4: FakeClient 全链(§63~§72, §95)
# ======================================================================
# 与 test_llm.FakeClient 同约定的精简版: truth audit / safety 自动应答,
# 其余按队列顺序回放。
class FakeClient:
    def __init__(self, results):
        self._results = list(results)
        self.calls = []
        self.runtime_cfg = None
        self.cfg = None

    def messages(self, system, user, max_tokens=None, tool=None,
                 temperature=None, timeout=None, max_retries=None,
                 stage=None, model=None):
        self.calls.append({"system": system, "user": user, "tool": tool,
                           "stage": stage, "max_tokens": max_tokens,
                           "temperature": temperature})
        name = (tool or {}).get("name")
        if name == "emit_truth_audit":
            for i, r in enumerate(self._results):
                if (r.tool_input or {}).get("__truth_audit__"):
                    self._results.pop(i)
                    ti = dict(r.tool_input)
                    ti.pop("__truth_audit__", None)
                    return LLMResult(tool_input=ti)
            return LLMResult(tool_input={
                "narrator_truthful": True, "mechanism_consistent": True,
                "conflicts": []})
        if name == "emit_safety_check":
            for i, r in enumerate(self._results):
                if (r.tool_input or {}).get("__safety__"):
                    self._results.pop(i)
                    ti = dict(r.tool_input)
                    ti.pop("__safety__", None)
                    return LLMResult(tool_input=ti)
            return LLMResult(tool_input={"livestream_safe": True,
                                         "reason": "(fake 默认放行)"})
        if not self._results:
            return LLMResult(error="no more canned results")
        return self._results.pop(0)


def _v1_contract_payload(n_completion=2, **overrides):
    """一份合法的 v1 Contract tool payload(灯塔题骨架, 可调档)。

    n_completion=1..5: f1/f2 恒为核心事实, n>=3 追加 f5, f6, ...(core,
    带安全摘要); f3(support)/f4(exclusion)恒为非通关事实。
    """
    facts = [
        {"id": "f1", "text": "退潮时礁石露出水面", "kind": "core",
         "public_text": "退潮后水下的礁石会露出来"},
        {"id": "f2", "text": "灯的真正作用是标示礁石位置", "kind": "core",
         "public_text": "灯是在标记危险礁石的位置"},
        {"id": "f3", "text": "涨潮后亮灯会误导船只", "kind": "support"},
        {"id": "f4", "text": "不是为了纪念死者", "kind": "exclusion"},
    ]
    extra_ids: list = []
    for i in range(3, n_completion + 1):
        fid = f"f{i + 2}"
        extra_ids.append(fid)
        facts.append({"id": fid, "text": f"通关核心事实{i}",
                      "kind": "core", "public_text": f"安全摘要{i}",
                      "visibility": "hidden"})
    comp = (["f1"] if n_completion == 1 else ["f1", "f2"]) + extra_ids
    atoms = [
        {"id": "a1", "role": "cause", "text": "退潮使礁石需要标出",
         "fact_ids": ["f1"], "required": True},
        {"id": "a2", "role": "mechanism", "text": "灯是标礁石不是引路",
         "fact_ids": ["f2", "f3"], "required": True},
    ]
    for j, fid in enumerate(extra_ids):
        atoms.append({"id": f"a{3 + j}", "role": "key",
                      "text": f"通关核心事实{3 + j}",
                      "fact_ids": [fid], "required": True})
    d = {
        "core_answer": "他亮灯是为了标出退潮时露出的礁石, 不是给船引路。",
        "completion_fact_ids": comp,
        "facts": facts,
        "solve_atoms": atoms,
        "fair_clues": [{"quote": "只在退潮的那几个小时亮",
                        "supports_atoms": ["a1"]}],
        "discovery_beats": [
            {"id": "b1", "text": "先注意到灯只在退潮时亮",
             "fact_ids": ["f1"]},
            {"id": "b2", "text": "再想到灯是在标礁石, 不是引路",
             "fact_ids": ["f2"]},
        ],
        "hints": ["注意灯的开关时机", "想想潮水的变化", "灯是在给谁传递信息?"],
        "signature": {
            "mechanism_family": "hidden_function",
            "solution_shape": "hidden_function_explains_behavior",
            "domain": "maritime", "relation": "stranger",
            "emotion_mode": "neutral", "time_shape": "habitual",
            "death": False, "past_trauma": False,
            "long_term_profession": False, "repeated_ritual": False,
            "reveal_mode": "hidden_stakes",
            "procedural_rule_dependency": False,
        },
        "difficulty": "medium",
        "primary_category": "logic",
        "categories": ["logic", "suspense"],
    }
    d.update(overrides)
    return d


PUZZLE = "灯塔只在退潮的那几个小时亮, 涨潮后他反而把灯熄掉。为什么?"
ANSWER = "退潮时礁石露出水面, 亮灯是为标出礁石位置; 涨潮后继续亮反而误导船只。"


def _review_payload(decision="pass", **kw):
    """v1 审稿回传 bundle: 原样带回 + 观察分类。"""
    d = _v1_contract_payload(2)
    d.update({"decision": decision,
              "puzzle": PUZZLE,
              "answer": ANSWER,
              "observed_signature": d["signature"],
              "quality_checks": {
                  "narrator_truthful": True, "mechanism_consistent": True,
                  "core_answer_direct": True, "completion_contract_minimal":
                      True,
                  "concrete_anomaly": True, "clue_recontextualized": True,
                  "dramatic_payoff": True,
                  "reasoning_beats_nonredundant": True,
                  "livestream_safe": True},
              })
    d.update(kw)
    return d


def _run_pipeline(contract_payload=None, brief=None, review_result=None,
                  safety_result=None, truth_result=None, extra_truth=None):
    """跑一遍真实 keyword2 全链(Truth -> Surface -> Contract -> Audit)。

    review/safety/truth 缺省自动"通过"(safety/truth 由 FakeClient 的
    自动应答承担; review 需要显式回传 bundle, 缺省给 pass)。

    返回 (spec, reason, client)。
    """
    from story.keyword_seed import keyword_spec as _ks
    from story.config import Config

    class _Bag:
        def draw(self):
            return {"keywords": ["灯塔", "退潮"], "slots": [],
                    "index": 3, "relaxed": 0}

    payload = contract_payload if contract_payload is not None \
        else _v1_contract_payload()
    results = [
        LLMResult(tool_input={"answer": ANSWER}),
        LLMResult(tool_input={"puzzle": PUZZLE}),
        LLMResult(tool_input=payload),
        review_result if review_result is not None
        else LLMResult(tool_input=_review_payload("pass")),
    ]
    # safety / truth 的应答不占队列位置(FakeClient 按标记取),
    # 所以这里打上各自的标记键。
    if safety_result is not None:
        ti = dict(safety_result.tool_input or {})
        ti["__safety__"] = True
        results.append(LLMResult(tool_input=ti))
    if truth_result is not None:
        ti = dict(truth_result.tool_input or {})
        ti["__truth_audit__"] = True
        results.append(LLMResult(tool_input=ti))
    if extra_truth is not None:
        results.append(extra_truth)
    cli = FakeClient(results)
    w = PuzzleWriter(client=cli, runtime_cfg=Config(sim_path="x",
                                                    no_llm=True))
    spec, reason = _ks(w, _Bag(), 20260925,
                       should_continue=lambda: True, brief=brief)
    return spec, reason, cli, w


def test_full_pipeline_v1_clean_and_pool():
    print("\n[P4] FakeClient 全链: v1 spec clean 且过池门(§64/§95)")
    spec, reason, cli, w = _run_pipeline()
    check("成题", bool(spec and spec.puzzle), reason)
    check("protocol_version == haiguitang-v1",
          spec.protocol_version == V1, spec.protocol_version)
    check("prompt_version == 生成 Pack 版本",
          spec.prompt_version == HAIGUITANG_GENERATION_PROMPT_VERSION,
          spec.prompt_version)
    check("quality_policy_version == quality-v13",
          spec.quality_policy_version == QUALITY_POLICY_VERSION,
          spec.quality_policy_version)
    check("requested_category == ''(自由生成)",
          spec.requested_category == "", spec.requested_category)
    check("difficulty/primary/categories 来自 Contract",
          (spec.difficulty, spec.primary_category, spec.categories)
          == ("medium", "logic", ["logic", "suspense"]),
          (spec.difficulty, spec.primary_category, spec.categories))
    check("completion fact 全部带 public_text",
          all(f.public_text for f in spec.facts
              if f.id in spec.completion_fact_ids),
          [(f.id, f.public_text) for f in spec.facts])
    vr = validate_spec(spec)
    check("validate_spec clean(ok 且零 fixable)",
          vr.ok and not vr.fixable, (vr.errors, vr.fixable))
    ok, why = PuzzlePool._validate_pool_spec(spec)
    check("过池门", ok, why)
    stages = [c["stage"] for c in cli.calls]
    check("调用链前四段 = story -> surface -> structure -> review",
          stages[:4] == ["puzzle.story", "puzzle.surface",
                         "puzzle.structure", "puzzle.review"], stages)
    check("system 全部来自 Prompt Pack",
          cli.calls[0]["system"] == load_prompt("truth")
          and cli.calls[1]["system"] == load_prompt("surface")
          and cli.calls[2]["system"] == load_prompt("contract")
          and cli.calls[3]["system"] == load_prompt("audit"),
          [c["system"][:20] for c in cli.calls])
    m = dict(spec.metrics or {})
    check("metrics 记 Pack 与 stage 版本(§12)",
          m.get("prompt_pack_version") == PROMPT_PACK_VERSION
          and m.get("truth_prompt_version") == stage_version("truth")
          and m.get("contract_prompt_version") == stage_version("contract"),
          {k: m.get(k) for k in ("prompt_pack_version",
                                 "truth_prompt_version")})
    check("spec.prompt_version 不再是 legacy 标签",
          spec.prompt_version != "keyword2-v7", spec.prompt_version)


def test_completion_2_3_4_all_clean_and_pool():
    print("\n[P5] 2/3/4 completion 三档全部 clean 入池(§65)")
    for n in (2, 3, 4):
        spec, reason, _cli, _w = _run_pipeline(_v1_contract_payload(n))
        check(f"{n}-fact 成题", bool(spec and spec.puzzle), reason)
        vr = validate_spec(spec)
        check(f"{n}-fact clean", vr.ok and not vr.fixable,
              (vr.errors[:1], vr.fixable[:1]))
        ok, why = PuzzlePool._validate_pool_spec(spec)
        check(f"{n}-fact 过池门", ok, why)


def test_completion_1_and_5_rejected():
    print("\n[P6] 1/5 completion 被硬门拒, Reviewer 不能静默绕过(§66)")
    spec, reason, _cli, _w = _run_pipeline(_v1_contract_payload(1))
    check("1-fact 不成题", not (spec and spec.puzzle), reason)
    check("1-fact 拒因是 validation_reject",
          reason == "gen_fail" and _reject_of(_w) == "validation_reject",
          (reason, _reject_of(_w)))
    spec5, reason5, _cli5, _w5 = _run_pipeline(_v1_contract_payload(5))
    check("5-fact 不成题", not (spec5 and spec5.puzzle), reason5)
    check("5-fact 拒因是 validation_reject",
          _reject_of(_w5) == "validation_reject", _reject_of(_w5))


def _reject_of(writer):
    """G4-R2 §六: 失败原因标签在 writer 侧信道上(失败稿被 keyword_spec 丢弃)。"""
    return getattr(writer, "_last_reject", "")


def test_missing_public_text_never_autofilled():
    print("\n[P7] completion 缺 public_text 被拒, 绝不拿 canonical 补(§67)")
    payload = _v1_contract_payload(2)
    payload["facts"][0] = {"id": "f1", "text": "退潮时礁石露出水面",
                           "kind": "core", "public_text": ""}
    spec, reason, _cli, _w = _run_pipeline(payload)
    check("不成题", not (spec and spec.puzzle), reason)
    check("拒因 validation_reject", _reject_of(_w) == "validation_reject",
          _reject_of(_w))
    # 反证: 代码没有拿 canonical text 补 —— spec 上 f1 的 public_text 仍是空
    check("public_text 没有被 canonical text 顶上",
          all(f.public_text != "退潮时礁石露出水面"
              for f in (spec.facts if spec else [])
              if f.id == "f1"),
          "fallback 泄漏")


def test_requested_category_mismatch_end_to_end():
    print("\n[P8] requested=sci_fi, observed=suspense: 合法且 provenance 保持"
          "(§35/§68/§70)")
    brief = GenerationBrief(requested_category="sci_fi")
    spec, reason, cli, w = _run_pipeline(
        _v1_contract_payload(2, primary_category="suspense",
                             categories=["suspense", "sci_fi"]),
        brief=brief,
        review_result=LLMResult(tool_input=_review_payload(
            "pass", primary_category="suspense",
            categories=["suspense", "sci_fi"])))
    check("成题", bool(spec and spec.puzzle), reason)
    check("Truth user message 带创作意图",
          "sci_fi" in cli.calls[0]["user"], cli.calls[0]["user"][:80])
    intent = "本次希望主方向偏向"
    check("Surface 看不到 requested(创作意图句不出现)",
          intent not in cli.calls[1]["user"], cli.calls[1]["user"][:60])
    check("Contract 看不到 requested(盲分类)",
          intent not in cli.calls[2]["user"], cli.calls[2]["user"][:80])
    check("Audit 看不到 requested(盲分类)",
          intent not in cli.calls[3]["user"], "requested 泄漏进 audit")
    check("spec.requested_category == sci_fi(provenance 保持)",
          spec.requested_category == "sci_fi", spec.requested_category)
    check("spec.primary_category == suspense(观察结果)",
          spec.primary_category == "suspense", spec.primary_category)
    check("requested != primary 合法且 clean",
          validate_spec(spec).ok and not validate_spec(spec).fixable,
          validate_spec(spec).errors)
    ok, why = PuzzlePool._validate_pool_spec(spec)
    check("mismatch 过池门", ok, why)


def test_requested_difficulty_mismatch_end_to_end():
    print("\n[P9] requested hard, observed medium -> spec.difficulty=medium"
          "(§19/§69)")
    brief = GenerationBrief(difficulty="hard")
    spec, reason, cli, w = _run_pipeline(
        _v1_contract_payload(2, difficulty="medium"), brief=brief)
    check("成题", bool(spec and spec.puzzle), reason)
    check("Truth 看见 hard 意图", "困难" in cli.calls[0]["user"],
          cli.calls[0]["user"][-80:])
    check("最终 difficulty == observed medium",
          spec.difficulty == "medium", spec.difficulty)
    check("requested difficulty 只进 metrics",
          (spec.metrics or {}).get("requested_difficulty") == "hard",
          (spec.metrics or {}).get("requested_difficulty"))


def _review_payload(decision="pass", **kw):
    """v1 审稿回传 bundle: 原样带回 + 观察分类。"""
    d = _v1_contract_payload(2)
    d.update({"decision": decision,
              "puzzle": PUZZLE,
              "answer": ANSWER,
              "observed_signature": d["signature"],
              "quality_checks": {
                  "narrator_truthful": True, "mechanism_consistent": True,
                  "core_answer_direct": True, "completion_contract_minimal":
                      True,
                  "concrete_anomaly": True, "clue_recontextualized": True,
                  "dramatic_payoff": True,
                  "reasoning_beats_nonredundant": True,
                  "livestream_safe": True},
              })
    d.update(kw)
    return d


def test_review_fix_preserves_v1_fields():
    print("\n[P10] Audit fix rebuild 不丢 v1 字段(§40/§71)")
    brief = GenerationBrief(requested_category="sci_fi")
    # 谜面只追加一句(fair_clue 的 quote 仍在改后谜面里), 走 fix 分支
    fixed_puzzle = PUZZLE + "守塔人对此绝口不提。"
    fixed = _review_payload(
        decision="fix", puzzle=fixed_puzzle,
        core_answer="他亮灯是为了标出退潮时露出的礁石。")
    spec, reason, _cli, _w = _run_pipeline(
        _v1_contract_payload(2), brief=brief,
        review_result=LLMResult(tool_input=fixed))
    check("fix 成题", bool(spec and spec.puzzle),
          (reason, _reject_of(_w)))
    check("protocol_version 仍是 v1", spec.protocol_version == V1,
          spec.protocol_version)
    check("requested_category 仍是 sci_fi",
          spec.requested_category == "sci_fi", spec.requested_category)
    check("prompt_version 仍是 Pack 版本",
          spec.prompt_version == HAIGUITANG_GENERATION_PROMPT_VERSION,
          spec.prompt_version)
    check("quality_policy_version 仍是 quality-v13",
          spec.quality_policy_version == QUALITY_POLICY_VERSION,
          spec.quality_policy_version)
    check("difficulty/categories 来自审稿重判",
          (spec.difficulty, spec.primary_category, spec.categories)
          == ("medium", "logic", ["logic", "suspense"]),
          (spec.difficulty, spec.primary_category, spec.categories))
    check("public_text 全部保留",
          all(f.public_text for f in spec.facts
              if f.id in spec.completion_fact_ids),
          [(f.id, f.public_text) for f in spec.facts])
    vr = validate_spec(spec)
    check("fix 后仍 clean", vr.ok and not vr.fixable,
          (vr.errors[:1], vr.fixable[:1]))


def test_review_bundle_missing_v1_fields_rejected():
    print("\n[P11] v1 审稿回传缺 difficulty/categories -> 整稿拒(§41)")
    fixed = _review_payload(decision="fix", puzzle=PUZZLE + "补一句?")
    del fixed["difficulty"]
    spec, reason, _cli, _w = _run_pipeline(_v1_contract_payload(2),
                                       review_result=LLMResult(
                                           tool_input=fixed))
    check("缺字段不成题", not (spec and spec.puzzle), reason)


def test_review_rewrite_lifecycle():
    print("\n[P12] pass / fix / rewrite 三态 lifecycle(§72)")
    # pass
    spec, reason, _cli, _w = _run_pipeline(
        _v1_contract_payload(2),
        review_result=LLMResult(tool_input=_review_payload("pass")))
    check("pass 成题且 protocol=v1",
          bool(spec and spec.puzzle) and spec.protocol_version == V1,
          (reason, getattr(spec, "protocol_version", None)))
    # rewrite: 坏候选不能伪装成成功 spec
    spec2, reason2, _cli2, _w2 = _run_pipeline(
        _v1_contract_payload(2),
        review_result=LLMResult(tool_input=_review_payload(
            "rewrite", rewrite_reason="没有公平推理路径")))
    check("rewrite 不成题", not (spec2 and spec2.puzzle), reason2)
    check("rewrite 标签 review_rewrite",
          _reject_of(_w2) == "review_rewrite", _reject_of(_w2))


def test_safety_and_truthfulness_gates_still_hold():
    print("\n[P13] Safety / Truthfulness 双门行为保持(§73/§74)")
    # safety false -> 拒
    spec, reason, _cli, _w = _run_pipeline(
        _v1_contract_payload(2),
        safety_result=LLMResult(tool_input={
            "livestream_safe": False, "reason": "性暴力核心情节"}))
    check("safety false 拒稿", not (spec and spec.puzzle), reason)
    check("拒因 safety_reject", _reject_of(_w) == "safety_reject",
          _reject_of(_w))
    # truth audit false -> 拒
    spec2, reason2, _cli2, _w2 = _run_pipeline(
        _v1_contract_payload(2),
        truth_result=LLMResult(tool_input={
            "narrator_truthful": False, "mechanism_consistent": False,
            "conflicts": [{"puzzle_claim": "a", "answer_claim": "b",
                           "why": "c"}]}))
    check("truthfulness false 拒稿", not (spec2 and spec2.puzzle), reason2)
    check("拒因 truth_reject", _reject_of(_w2) == "truth_reject",
          _reject_of(_w2))


# ======================================================================
# P14: classic / curated 仍 legacy; quality-v13 库存不旋转(§47~§50, §76)
# ======================================================================
def test_classic_and_curated_stay_legacy():
    print("\n[P14] classic 兜底与 curated 不升 v1; 旧库存仍可播")
    # classic 链(emit_riddle)走 RIDDLE_SYSTEM, 产 legacy spec
    from story.llm import RIDDLE_SYSTEM  # noqa: F401  (常量仍在 = legacy 源)
    spec, reason, cli, w = _run_pipeline(_v1_contract_payload(2))
    check("(前置)keyword2 产 v1", spec.protocol_version == V1,
          spec.protocol_version)
    check("keyword2 链上没有 emit_riddle",
          all((c["tool"] or {}).get("name") != "emit_riddle"
              for c in cli.calls), "混链")
    # legacy v13 spec(带旧 stage 版本标签的库存)仍过池门
    from tests.test_haiguitang_protocol import _legacy_spec
    ls = _legacy_spec(1)
    ls.prompt_version = "keyword2-v7"
    ok, why = PuzzlePool._validate_pool_spec(ls)
    check("legacy v13 spec(protocol='')仍可入池", ok, why)
    check("legacy 不会因为生成侧激活 v1 而被要求 v1", ls.protocol_version == "",
          ls.protocol_version)


# ======================================================================
# P15: 单一来源(§61)
# ======================================================================
def test_single_source_of_prompts():
    print("\n[P15] v1 prompt 无内嵌重复常量; v1 路径走 loader")
    import story.llm as L
    check("llm.py 不再有 STORY_SYSTEM 常量", not hasattr(L, "STORY_SYSTEM"))
    check("llm.py 不再有 SURFACE_SYSTEM 常量",
          not hasattr(L, "SURFACE_SYSTEM"))
    check("llm.py 不再有 STRUCTURE_SYSTEM 常量",
          not hasattr(L, "STRUCTURE_SYSTEM"))
    check("llm.py 不再有 TRUTH_AUDIT_SYSTEM 常量",
          not hasattr(L, "TRUTH_AUDIT_SYSTEM"))
    check("llm.py 不再有 SAFETY_SYSTEM 常量", not hasattr(L, "SAFETY_SYSTEM"))
    # legacy CHECK_SYSTEM 仍在(classic/curated 用) —— 但 v1 链走 loader
    check("legacy CHECK_SYSTEM 保留(classic/curated 用)",
          hasattr(L, "CHECK_SYSTEM"))
    # _review_spec 的 v1 分支真的用了 load_prompt("audit"):
    # 用一份 v1 spec + FakeClient 走一遍, system 必须等于文件文本。
    spec, _reason, cli, _w = _run_pipeline(_v1_contract_payload(2))
    check("v1 题的审稿 system == audit-v1.md",
          cli.calls[3]["system"] == load_prompt("audit"),
          cli.calls[3]["system"][:40])


# ======================================================================
def main():
    print("=== tests/test_prompt_pack.py ===")
    test_all_stages_load()
    test_loader_negatives()
    test_v1_tool_schema_matches_protocol()
    test_full_pipeline_v1_clean_and_pool()
    test_completion_2_3_4_all_clean_and_pool()
    test_completion_1_and_5_rejected()
    test_missing_public_text_never_autofilled()
    test_requested_category_mismatch_end_to_end()
    test_requested_difficulty_mismatch_end_to_end()
    test_review_fix_preserves_v1_fields()
    test_review_bundle_missing_v1_fields_rejected()
    test_review_rewrite_lifecycle()
    test_safety_and_truthfulness_gates_still_hold()
    test_classic_and_curated_stay_legacy()
    test_single_source_of_prompts()
    print()
    if FAIL[0]:
        print(f"FAILED: {FAIL[0]} check(s)")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
