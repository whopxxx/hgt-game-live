#!/usr/bin/env python
# coding: utf-8
"""Small, append-only constraint-ablation experiment.

This imports production prompts and validators but never mutates production
configuration or policy.  Every model call is stateless; retries happen only
when the transport/tool payload is unusable and are recorded as technical.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from story.config import Config, LLMConfig
from story.keyword_corpus import DEFAULT_CORPUS_PATH
from story.keyword_seed import load_bag
from story.llm import (
    CHECK_SYSTEM,
    KEYWORD_IDEA_PROMPT_VERSION,
    KEYWORD_IDEA_SYSTEM,
    STRUCTURE_SYSTEM,
    AnthropicMessagesClient,
    PuzzleWriter,
    _TOOL_KEYWORD_IDEA,
    _TOOL_STRUCTURE,
    _keyword_idea_shape_error,
    _keywords_prompt,
    _spec_from_tool,
    _structure_user_prompt,
    _unconstrained_blueprint,
    _unwrap_tool_input,
)
from story.quality import validate_spec


OUT = ROOT / "data" / "soup_constraint_experiment"
SEED = 20260920


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"name": name, "description": description,
            "input_schema": {"type": "object", "properties": properties,
                             "required": required}}


SURFACE_TOOL = _tool(
    "emit_surface_bundle", "同一谜底的格式、叙述形式和公平线索对照汤面",
    {k: {"type": "string"} for k in (
        "format_only", "current_surface", "p1_third_question",
        "p2_natural_voice", "p3_no_question", "fair_clue_on",
        "fair_clue_off")},
    ["format_only", "current_surface", "p1_third_question",
     "p2_natural_voice", "p3_no_question", "fair_clue_on",
     "fair_clue_off"])

EXPOSURE_TOOL = _tool(
    "emit_exposure_surfaces", "同一谜底的低中高三档信息暴露汤面",
    {k: {"type": "string"} for k in ("low", "medium", "high")},
    ["low", "medium", "high"])

DB_TOOL = _tool(
    "emit_discovery_beat_variants", "同一故事在三种 discovery beat 规则下的结构",
    {k: {"type": "array", "items": {"type": "string"}}
     for k in ("db1_one_to_four", "db2_two_to_four", "db3_optional")},
    ["db1_one_to_four", "db2_two_to_four", "db3_optional"])

CREATIVE_TOOL = _tool(
    "emit_candidate", "输出一份原创实验候选",
    {k: {"type": "string"} for k in ("title", "hidden_story", "puzzle", "answer")},
    ["hidden_story", "puzzle", "answer"])

EXPOSURE_REVIEW_TOOL = _tool(
    "emit_exposure_review", "定性比较三份汤面",
    {"reviews": {"type": "array", "items": {"type": "object",
        "properties": {
            "key": {"type": "string", "enum": ["A", "B", "C"]},
            "question_desire": {"type": "string", "enum": ["有", "弱", "无"]},
            "answer_leak": {"type": "string", "enum": ["低", "中", "高"]},
            "directions": {"type": "array", "items": {"type": "string"}},
            "reading_comprehension": {"type": "boolean"},
            "live_outlook": {"type": "string", "enum": [
                "容易3~5问秒掉", "可能8~20问逐步逼近", "很可能只能乱猜"]},
            "reason": {"type": "string"}},
        "required": ["key", "question_desire", "answer_leak", "directions",
                     "reading_comprehension", "live_outlook", "reason"]}}},
    ["reviews"])

BLIND_TOOL = _tool(
    "emit_blind_reviews", "给匿名海龟汤候选做三档盲评",
    {"reviews": {"type": "array", "items": {"type": "object",
        "properties": {
            "id": {"type": "string"},
            "rating": {"type": "string", "enum": ["想玩", "可以玩", "不想玩"]},
            "reason": {"type": "string"}},
        "required": ["id", "rating", "reason"]}}},
    ["reviews"])

STYLE_CLASS_TOOL = _tool(
    "emit_style_classes", "匿名标注候选的解释机制和冲击类型",
    {"classes": {"type": "array", "items": {"type": "object",
        "properties": {
            "id": {"type": "string"},
            "world": {"type": "string", "enum": ["现实", "架空", "超自然"]},
            "mechanism": {"type": "string", "enum": [
                "普通现实解释", "拍摄道具误会", "医学冷知识", "心理关系",
                "异常规则", "时间空间身份", "其他"]},
            "impact": {"type": "string", "enum": ["强", "中", "弱"]},
            "rule_consistent": {"type": "boolean"},
            "reason": {"type": "string"}},
        "required": ["id", "world", "mechanism", "impact",
                     "rule_consistent", "reason"]}}},
    ["classes"])


FORMAT_SYSTEM = """你是海龟汤实验编辑。用户给出固定 hidden story、固定谜底和原始汤面。
只改汤面，不得改变汤底、人物关系、时间、因果、身份或世界规则；不要补新设定。

format_only：只保证中文、纯文字可玩、220字以内、不撒谎、有一个可调查异常。
不强制第三人称，不强制问号，不要求把关键证据提前写出。

current_surface：模拟当前 surface 压力：第三人称、问号结尾、220字以内，
并把至少一条能支持核心答案的具体 fair clue 明写在汤面中。

p1_third_question：第三人称并以“为什么？”收尾。
p2_natural_voice：使用最自然的第一人称、日记或对话形式，不机械改第三人称。
p3_no_question：异常本身清楚，但末尾没有显式问号。

fair_clue_on：至少一段逐字可摘的事实直接支持核心真相。
fair_clue_off：只保留真实、不撒谎的异常锚点；关键支持事实留给问答。

各版本只改变指定变量。不要评价，不要解释，不要重抽故事。"""

EXPOSURE_SYSTEM = """你是海龟汤汤面信息量实验编辑。hidden story 和 answer 完全冻结，
只输出三份汤面；三份只允许改变信息暴露量，不得改变事实、人物、因果或世界规则。
LOW：只给最异常、最有问题欲的一幕，不给解释路线和关键支持事实。
MEDIUM：给核心异常和一个真实锚点；身份、时间、关系、目的、世界规则大多留给问答。
HIGH：多个 observable clues，至少一条显式线索直接支持核心答案，接近案情简介。
三份都必须真实、中文、纯文字可玩且不超过220字。"""

DB_SYSTEM = """你是结构标注实验员。固定谜面和谜底，不得改写。
只列玩家在问答中可能经历的 discovery beats：
DB1 允许1~4层；DB2强制2~4层；DB3完全不要求，只有自然存在才写，没有可为空。
不要为了凑数量换同义词；但 DB2 必须照要求填写，因此如果故事天然单核，要如实暴露
被迫拆分的结果。"""

REALITY_SYSTEM = {
    "R1": "尽量采用普通现实解释。不要超自然或架空规则。",
    "R2": "现实、架空、超自然均可；只要求内部自洽与可用是/否问答逼近。",
    "R3": ("鼓励异常世界规则、真正超自然、都市怪谈、时间空间身份异常；"
           "不强制鬼怪，但不要自动退回拍摄、道具、普通误会。"),
}

ABSTRACT_STYLE = """写一份原创中文红汤或黑汤：诡异、反常、揭晓有余味、
适合是/否问答、不依赖冷知识。红黑来自认知变化或不安目的，不来自伤害细节。
不要复制任何网站题目。"""

FEWSHOT_STYLE = """写一份原创中文红汤或黑汤，适合是/否问答，不依赖冷知识。
下面只是题感结构，不是模板，不得照抄题材：
1. 弱：一个习惯仅由“以前有人死了，所以纪念”解释。较好：揭晓改变行为的真实目的，
   且开头一句普通话因此变味。
2. 弱：异常只是拍视频、演戏或道具。较好：玩家误解身份、时间或世界规则，揭晓重读场景。
3. 弱：罕见疾病、冷门职业规定本身就是答案。较好：常识足够，隐藏的是关系、目的或因果。
4. 弱：普通看错或误会解释全部。较好：误解被纠正后还出现一个具体、令人不安的真实故事。
这些弱结构不是绝对禁止，但若它们就是整个 reveal 通常不够。示例只表达题感，
不要固定反转数量，不要硬套同一结构。不要复制任何网站题目。"""

BLIND_SYSTEM = """你是独立的海龟汤盲评员。你不知道候选来自哪种 prompt 或实验。
只看汤面和汤底，逐题三选一：
想玩＝汤面有提问欲且揭晓有明显满足；
可以玩＝成立且有一点意思；
不想玩＝直白、普通、套路、只能乱猜、冷知识或解释不爽。
每题主要原因只写一句话。不要猜实验标签，不要按黑暗程度加分。"""

STYLE_CLASS_SYSTEM = """你是独立的内容分类员，不知道候选使用了哪种生成提示。
只按汤面汤底实际内容标注世界类型、主要解释机制、心理冲击强弱和规则是否自洽。
“拍摄道具误会”只在它是主要 reveal 时使用；不要因为出现死亡就自动标为冲击强。"""


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def save_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                    encoding="utf-8")


def put(path: Path, row: dict, key: str = "id") -> None:
    rows = read_jsonl(path)
    if any(r.get(key) == row.get(key) for r in rows):
        return
    rows.append(row)
    save_jsonl(path, rows)


def call_tool(client: AnthropicMessagesClient, system: str, user: str,
              tool: dict, *, max_tokens: int = 2400, temperature: float = 0.7,
              technical_retries: int = 1) -> tuple[dict | None, dict]:
    attempts = []
    for _ in range(technical_retries + 1):
        r = client.messages(system, user, max_tokens=max_tokens, tool=tool,
                            temperature=temperature)
        payload = _unwrap_tool_input(r.tool_input) if r.tool_input else None
        attempts.append({"ok": isinstance(payload, dict) and bool(payload),
                         "error": r.error or "", "model": r.model,
                         "usage": r.usage or {}})
        if isinstance(payload, dict) and payload:
            return payload, {"attempts": attempts, "technical_retries": len(attempts) - 1}
    return None, {"attempts": attempts, "technical_retries": len(attempts) - 1}


def prod_structure(writer: PuzzleWriter, client: AnthropicMessagesClient,
                   raw: dict, runtime: Config) -> dict:
    """Run production Stage B, validator, Reviewer and truth audit with trace."""
    bp = _unconstrained_blueprint()
    user = _structure_user_prompt(raw["puzzle"], raw["answer"],
                                  title=raw.get("title", ""))
    payload, call = call_tool(
        client, STRUCTURE_SYSTEM, user, _TOOL_STRUCTURE, max_tokens=4000,
        temperature=runtime.generate_temperature, technical_retries=1)
    out: dict[str, Any] = {"structure_call": call}
    if payload is None:
        out.update(final="REJECT", reject_layer="stage_b_technical",
                   reject_reason="Stage B 没有可用 tool payload")
        return out
    spec = _spec_from_tool(payload, blueprint=bp, title=raw.get("title", ""))
    spec.puzzle = raw["puzzle"].strip()
    spec.answer = raw["answer"].strip()
    spec.prompt_version = KEYWORD_IDEA_PROMPT_VERSION
    vr = validate_spec(spec)
    out["v3_structure"] = spec.to_dict()
    out["v3_validation"] = {"ok": vr.ok, "errors": vr.errors,
                            "fixable": vr.fixable, "warnings": vr.warnings}
    if not vr.ok:
        out.update(final="REJECT", reject_layer="validator_pre_review",
                   reject_reason=vr.why())
        return out
    reviewed, why, rewrite, technical = writer._review_spec_with_retry(
        spec, bp, must_fix=vr.must_fix(), own_fix_focus=list(vr.fixable))
    out["v4_reviewer"] = {
        "decision": writer._last_review_decision,
        "issues": writer._last_review_issues or [],
        "quality_checks": writer._last_review_checks or {},
        "why": why, "rewrite": rewrite, "technical": technical,
        "before_puzzle": spec.puzzle, "before_answer": spec.answer,
        "after_puzzle": reviewed.puzzle if reviewed else "",
        "after_answer": reviewed.answer if reviewed else "",
    }
    if reviewed is None:
        out.update(final="REJECT",
                   reject_layer="reviewer_technical" if technical else "reviewer",
                   reject_reason=why)
        return out
    vr2 = validate_spec(reviewed)
    out["v5_validation"] = {"ok": vr2.ok, "errors": vr2.errors,
                            "fixable": vr2.fixable, "warnings": vr2.warnings}
    if not vr2.ok or vr2.fixable:
        out.update(final="REJECT", reject_layer="validator_post_review",
                   reject_reason="; ".join(vr2.errors + vr2.fixable))
        return out
    audit = writer.audit_truthfulness(reviewed)
    out["v5_truth_audit"] = audit
    if not audit or not (audit.get("narrator_truthful")
                         and audit.get("mechanism_consistent")):
        out.update(final="REJECT", reject_layer="truth_audit",
                   reject_reason=(audit or {}).get("why", "audit unavailable"))
        return out
    out.update(final="PASS", reject_layer="", reject_reason="",
               final_spec=reviewed.to_dict())
    return out


def stage_a(client: AnthropicMessagesClient, runtime: Config) -> list[dict]:
    path = OUT / "raw_results.jsonl"
    rows = read_jsonl(path)
    if rows:
        return rows
    bag, meta = load_bag(str(DEFAULT_CORPUS_PATH), SEED)
    manifest = {"seed": SEED, "keyword_meta": meta, "model_requested": client.cfg.model,
                "production_base": "dd40bacb2f7edf8f48cf34333d67796d5516fa72"}
    (OUT / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    for i in range(1, 11):
        draw = bag.draw()
        keywords = list(draw["keywords"])
        payload, call = call_tool(
            client, KEYWORD_IDEA_SYSTEM, _keywords_prompt(keywords),
            _TOOL_KEYWORD_IDEA, max_tokens=1500,
            temperature=runtime.generate_temperature, technical_retries=1)
        shape_error = _keyword_idea_shape_error(payload or {}) if payload else "no payload"
        row = {"id": f"S{i:02d}", "keywords": keywords, "call": call,
               "shape_error": shape_error or ""}
        if payload and not shape_error:
            row.update({k: payload.get(k) for k in (
                "core_truth", "observed_clues", "event_chain", "title",
                "puzzle", "answer")})
        put(path, row)
    return read_jsonl(path)


def surface_bundles(client: AnthropicMessagesClient, raws: list[dict]) -> dict[str, dict]:
    path = OUT / "surface_bundles.jsonl"
    done = {r["id"]: r for r in read_jsonl(path)}
    for raw in raws:
        if raw["id"] in done or not raw.get("puzzle"):
            continue
        user = (f"【hidden story】\n{raw['core_truth']}\n\n【固定谜底】\n{raw['answer']}"
                f"\n\n【原始汤面】\n{raw['puzzle']}")
        payload, call = call_tool(client, FORMAT_SYSTEM, user, SURFACE_TOOL,
                                  max_tokens=2400, temperature=0.4)
        put(path, {"id": raw["id"], "call": call, "variants": payload or {}})
    return {r["id"]: r for r in read_jsonl(path)}


def ablation(client: AnthropicMessagesClient, writer: PuzzleWriter, runtime: Config,
             raws: list[dict], bundles: dict[str, dict]) -> None:
    path = OUT / "ablation_results.jsonl"
    done = {r["id"] for r in read_jsonl(path)}
    for raw in raws:
        if raw["id"] in done or not raw.get("puzzle"):
            continue
        variants = (bundles.get(raw["id"]) or {}).get("variants", {})
        trace = prod_structure(writer, client, raw, runtime)
        row = {
            "id": raw["id"], "keywords": raw["keywords"],
            "v0_raw": {"hidden_story": raw["core_truth"],
                       "puzzle": raw["puzzle"], "answer": raw["answer"]},
            "v1_format_only": {"puzzle": variants.get("format_only", ""),
                               "answer": raw["answer"]},
            "v2_current_surface_shadow": {"puzzle": variants.get("current_surface", ""),
                                          "answer": raw["answer"]},
            "production_note": ("V3-V5 从 V0 RAW 进入真实 production；Stage B 本身冻结"
                                "谜面谜底。V2 是独立 shadow policy，不伪装成 production。"),
            **trace,
        }
        put(path, row)


def exposure(client: AnthropicMessagesClient, raws: list[dict]) -> None:
    path = OUT / "surface_exposure_results.jsonl"
    done = {r["id"] for r in read_jsonl(path)}
    for raw in [r for r in raws if r.get("puzzle")][:6]:
        if raw["id"] in done:
            continue
        user = (f"【hidden story】\n{raw['core_truth']}\n\n【固定 answer】\n{raw['answer']}"
                f"\n\n【原始汤面，仅供识别异常】\n{raw['puzzle']}")
        variants, call = call_tool(client, EXPOSURE_SYSTEM, user, EXPOSURE_TOOL,
                                   max_tokens=1800, temperature=0.5)
        variants = variants or {}
        order = [("A", "low"), ("B", "medium"), ("C", "high")]
        review_user = "【固定汤底】\n" + raw["answer"] + "\n\n" + "\n\n".join(
            f"【{letter}】\n{variants.get(key, '')}" for letter, key in order)
        reviews, review_call = call_tool(
            client,
            "独立评估三份匿名汤面，不知道 LOW/MEDIUM/HIGH 标签。按工具中的五个问题定性回答。",
            review_user, EXPOSURE_REVIEW_TOOL, max_tokens=1800, temperature=0)
        put(path, {"id": raw["id"], "answer": raw["answer"],
                   "variants": variants, "generation_call": call,
                   "reviews": (reviews or {}).get("reviews", []),
                   "review_call": review_call})


def structure_variants(client: AnthropicMessagesClient, raws: list[dict]) -> None:
    path = OUT / "discovery_beat_results.jsonl"
    done = {r["id"] for r in read_jsonl(path)}
    for raw in [r for r in raws if r.get("puzzle")][:6]:
        if raw["id"] in done:
            continue
        user = f"【谜面】\n{raw['puzzle']}\n\n【谜底】\n{raw['answer']}"
        variants, call = call_tool(client, DB_SYSTEM, user, DB_TOOL,
                                   max_tokens=1500, temperature=0.3)
        put(path, {"id": raw["id"], "variants": variants or {}, "call": call})


def creative_candidate(client: AnthropicMessagesClient, system: str,
                       keywords: list[str], tag: str) -> dict:
    user = ("关键词：" + "，".join(keywords)
            + "\n请直接创作一份候选。谜面不超过220字，谜底不超过300字。")
    payload, call = call_tool(client, system, user, CREATIVE_TOOL,
                              max_tokens=1800, temperature=0.8)
    return {"id": tag, "keywords": keywords, "candidate": payload or {}, "call": call}


def style_experiments(client: AnthropicMessagesClient, raws: list[dict],
                      bundles: dict[str, dict]) -> None:
    path = OUT / "style_results.jsonl"
    done = {r["id"] for r in read_jsonl(path)}
    source = [r for r in raws if r.get("puzzle")]
    pairs = [r["keywords"] for r in source]
    for i, keys in enumerate(pairs[:5], 1):
        for reality, instruction in REALITY_SYSTEM.items():
            tag = f"REALITY-{reality}-{i:02d}"
            if tag not in done:
                system = ("你是中文海龟汤作者。" + instruction
                          + " 红汤或黑汤应来自认知变化、不安目的或自洽规则，"
                            "不是伤害细节；不用冷知识，不复制网站题目。")
                put(path, creative_candidate(client, system, keys, tag))
                done.add(tag)
    for i, keys in enumerate(pairs[5:10], 1):
        for name, prompt in (("F1", ABSTRACT_STYLE), ("F2", FEWSHOT_STYLE)):
            tag = f"STYLE-{name}-{i:02d}"
            if tag not in done:
                put(path, creative_candidate(client, prompt, keys, tag))
                done.add(tag)
    for raw in source[:6]:
        variants = (bundles.get(raw["id"]) or {}).get("variants", {})
        for key in ("p1_third_question", "p2_natural_voice", "p3_no_question",
                    "fair_clue_on", "fair_clue_off"):
            tag = f"SURFACE-{key}-{raw['id']}"
            if tag not in done:
                put(path, {"id": tag, "source_story": raw["id"],
                           "candidate": {"puzzle": variants.get(key, ""),
                                         "answer": raw["answer"]},
                           "call": (bundles.get(raw["id"]) or {}).get("call", {})})
                done.add(tag)


def classify_styles(client: AnthropicMessagesClient) -> None:
    """Classify creative variants without exposing their prompt labels."""
    creative = [r for r in read_jsonl(OUT / "style_results.jsonl")
                if r["id"].startswith(("REALITY-", "STYLE-"))]
    mapping = [{"anon_id": f"C{i:03d}", "source_id": r["id"],
                "candidate": r.get("candidate") or {}}
               for i, r in enumerate(creative, 1)]
    path = OUT / "style_classification.jsonl"
    done = {r["source_id"] for r in read_jsonl(path)}
    for start in range(0, len(mapping), 10):
        batch = [r for r in mapping[start:start + 10] if r["source_id"] not in done]
        if not batch:
            continue
        user = "\n\n".join(
            f"【{r['anon_id']}】\n汤面：{r['candidate'].get('puzzle', '')}"
            f"\n汤底：{r['candidate'].get('answer', '')}" for r in batch)
        payload, call = call_tool(client, STYLE_CLASS_SYSTEM, user, STYLE_CLASS_TOOL,
                                  max_tokens=2200, temperature=0)
        by_id = {r.get("id"): r for r in (payload or {}).get("classes", [])}
        for item in batch:
            put(path, {"source_id": item["source_id"], "anon_id": item["anon_id"],
                       "classification": by_id.get(item["anon_id"], {}),
                       "review_call": call}, key="source_id")


def collect_blind_candidates() -> list[dict]:
    candidates: dict[tuple[str, str], dict] = {}

    def add(puzzle: str, answer: str, source: str) -> None:
        puzzle, answer = (puzzle or "").strip(), (answer or "").strip()
        if not puzzle or not answer:
            return
        key = (puzzle, answer)
        if key not in candidates:
            candidates[key] = {"puzzle": puzzle, "answer": answer, "sources": []}
        candidates[key]["sources"].append(source)

    for r in read_jsonl(OUT / "ablation_results.jsonl"):
        for key in ("v0_raw", "v1_format_only", "v2_current_surface_shadow"):
            c = r.get(key) or {}
            add(c.get("puzzle", ""), c.get("answer", ""), f"ABLATION:{r['id']}:{key}")
        rv = r.get("v4_reviewer") or {}
        add(rv.get("after_puzzle", ""), rv.get("after_answer", ""),
            f"ABLATION:{r['id']}:v4_reviewer")
    for r in read_jsonl(OUT / "surface_exposure_results.jsonl"):
        for key, puzzle in (r.get("variants") or {}).items():
            add(puzzle, r.get("answer", ""), f"EXPOSURE:{r['id']}:{key}")
    for r in read_jsonl(OUT / "style_results.jsonl"):
        c = r.get("candidate") or {}
        add(c.get("puzzle", ""), c.get("answer", ""), r["id"])
    rows = list(candidates.values())
    random.Random(SEED).shuffle(rows)
    for i, row in enumerate(rows, 1):
        row["blind_id"] = f"P{i:03d}"
    return rows


def blind_review(client: AnthropicMessagesClient) -> None:
    manifest_path = OUT / "blind_manifest.jsonl"
    if not manifest_path.exists():
        save_jsonl(manifest_path, collect_blind_candidates())
    manifest = read_jsonl(manifest_path)
    out_path = OUT / "blind_review.jsonl"
    done = {r["id"] for r in read_jsonl(out_path)}
    for start in range(0, len(manifest), 10):
        batch = [r for r in manifest[start:start + 10] if r["blind_id"] not in done]
        if not batch:
            continue
        user = "\n\n".join(
            f"【{r['blind_id']}】\n汤面：{r['puzzle']}\n汤底：{r['answer']}" for r in batch)
        payload, call = call_tool(client, BLIND_SYSTEM, user, BLIND_TOOL,
                                  max_tokens=2200, temperature=0)
        by_id = {r.get("id"): r for r in (payload or {}).get("reviews", [])}
        for item in batch:
            review = by_id.get(item["blind_id"], {})
            put(out_path, {"id": item["blind_id"],
                           "puzzle": item["puzzle"], "answer": item["answer"],
                           "rating": review.get("rating", ""),
                           "reason": review.get("reason", ""),
                           "review_call": call})


def self_test() -> None:
    assert SURFACE_TOOL["input_schema"]["required"]
    assert set(REALITY_SYSTEM) == {"R1", "R2", "R3"}
    assert "网站" in FEWSHOT_STYLE
    assert BLIND_TOOL["input_schema"]["properties"]["reviews"]["type"] == "array"
    assert STYLE_CLASS_TOOL["input_schema"]["properties"]["classes"]["type"] == "array"
    print("PASS: experiment schema/self-test")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        self_test()
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    runtime = Config()
    cfg = LLMConfig()
    client = AnthropicMessagesClient(cfg)
    writer = PuzzleWriter(client, runtime_cfg=runtime)
    raws = stage_a(client, runtime)
    bundles = surface_bundles(client, raws)
    ablation(client, writer, runtime, raws, bundles)
    exposure(client, raws)
    structure_variants(client, raws)
    style_experiments(client, raws, bundles)
    classify_styles(client)
    blind_review(client)
    print(json.dumps({"raw_stories": len([r for r in raws if r.get('puzzle')]),
                      "blind_candidates": len(read_jsonl(OUT / 'blind_review.jsonl')),
                      "model": cfg.model}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
