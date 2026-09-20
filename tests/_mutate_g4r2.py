#!/usr/bin/env python
# coding: utf-8
"""G4-R2 变异跑手: 每条变异打一个补丁, 跑受影响套件, 断言**必须红**。

为什么要写成一个脚本而不是手工几十次 Edit/undo:
  - 手工漏一次 revert 就会把变异**带进提交**(这个坑 G4-E4 踩过);
  - "哪条变异打红了哪条断言"必须可复现, 否则报告里的表格没有根据。

用法: `.venv/Scripts/python.exe -X utf8 tests/_mutate_g4r2.py`
"""
import io
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
BAK = ROOT / ".mutbak"

#: (编号, 说明, 文件, 原文, 变异后, 期望变红的套件)
MUTATIONS = [
    ("M-R2-1", "Stage B 不重试(+1 去掉, 回到 1 attempt)",
     "story/llm.py",
     "        max_attempts = max(1, int(max_attempts)) + 1",
     "        max_attempts = max(1, int(max_attempts))",
     ["tests/test_g4_source.py"]),

    ("M-R2-2", "Stage B **总是**重试(连技术形状都不判)",
     "story/llm.py",
     "                if attempt < max_attempts:\n"
     "                    m[\"structure_technical_retries\"] = (\n"
     "                        m.get(\"structure_technical_retries\", 0) + 1)\n"
     "                    log.warning(\"Structurize 技术失败(空 tool_input), \"\n"
     "                                \"同一 idea 重试一次\")\n"
     "                continue",
     "                m[\"structure_technical_retries\"] = (\n"
     "                    m.get(\"structure_technical_retries\", 0) + 1)\n"
     "                continue",
     ["tests/test_g4_source.py"]),

    ("M-R2-3", "技术重试前**不**查 should_continue",
     "story/llm.py",
     "            if attempt > 1 and _stop():\n"
     "                return _bail()",
     "            if attempt > 1 and False:\n"
     "                return _bail()",
     ["tests/test_g4_source.py"]),

    ("M-R2-4", "Stage A prompt 去掉 260 约束",
     "story/llm.py",
     "5. **谜底保持简洁** —— 建议 2~4 句, 中文总长度不超过 260 字。\n"
     "   谜底是**揭晓时直接念给观众**的, 写成长篇说明会拖垮直播节奏。\n",
     "",
     ["tests/test_g4_source.py"]),

    ("M-R2-5", "answer 硬上限从 300 放宽",
     "story/quality.py",
     "ANSWER_HARD_MAX_LEN = 300",
     "ANSWER_HARD_MAX_LEN = 600",
     ["tests/test_g4_source.py"]),

    ("M-R2-6", "core>3 退回 hard fail(整道扔掉)",
     "story/quality.py",
     "    if n_core > max_core_hidden:\n"
     "        r.can_fix(\n"
     "            f\"{_CORE_COUNT_MARK} core hidden facts 有 {n_core} 条, \"",
     "    if n_core > max_core_hidden:\n"
     "        r.fail(f\"core hidden facts 有 {n_core} 条\") or (\n"
     "            lambda: None)()\n"
     "    if False:\n"
     "        r.can_fix(\n"
     "            f\"{_CORE_COUNT_MARK} core hidden facts 有 {n_core} 条, \"",
     ["tests/test_g4_source.py", "tests/test_puzzle.py"]),

    ("M-R2-7", "core 修复的越界守卫去掉(改内容也收下)",
     "story/llm.py",
     "        _bad = _core_fix_scope_violation(spec, merged, ti, own_fix_focus)\n"
     "        if _bad:\n"
     "            return None, _bad, True, False\n"
     "        return merged, note or \"审稿已修改\", False, False",
     "        return merged, note or \"审稿已修改\", False, False",
     ["tests/test_g4_source.py"]),

    ("M-R2-8", "空池退避退回长序列(240/300)",
     "story/prefetch.py",
     "        if self._is_empty():\n"
     "            return self._empty_backoff_schedule\n"
     "        return self._backoff_schedule",
     "        return self._backoff_schedule",
     ["tests/test_g4_source.py"]),

    ("M-R2-9", "**所有**情况都用紧急序列(有库存也 60s 封顶)",
     "story/prefetch.py",
     "        if self._is_empty():\n"
     "            return self._empty_backoff_schedule\n"
     "        return self._backoff_schedule",
     "        return self._empty_backoff_schedule",
     ["tests/test_g4_source.py"]),

    ("M-R2-10", "拒绝原因不分类(extra 里不带 reject)",
     "story/prefetch.py",
     "            return (\"gen_fail\", \"keyword2 未成题\", self._reject_extra())",
     "            return (\"gen_fail\", \"keyword2 未成题\", {})",
     ["tests/test_g4_source.py"]),

    ("M-R2-11", "success 不进分类账",
     "story/prefetch.py",
     "        if kind == \"ok\":\n"
     "            self.reject_count[\"success\"] += 1",
     "        if False:\n"
     "            self.reject_count[\"success\"] += 1",
     ["tests/test_g4_source.py"]),

    ("M-R2-12", "语义拒绝被误标成技术失败(指标分不开)",
     "story/llm.py",
     "        vr = validate_spec(spec)\n"
     "        if not vr.ok:\n"
     "            return _bail(\"结构不过: \" + vr.why(), \"validation_reject\")",
     "        vr = validate_spec(spec)\n"
     "        if not vr.ok:\n"
     "            return _bail(\"结构不过: \" + vr.why(),\n"
     "                         \"structure_technical_fail\")",
     ["tests/test_g4_source.py"]),

    # ================= G4-R2-R1 =================
    ("M-R1-1", "core 守卫的字段 diff 整个去掉(改什么都收下)",
     "story/llm.py",
     "    if not any(_CORE_COUNT_MARK in str(f) for f in (own_fix_focus or [])):\n"
     "        return \"\"\n"
     "    dom = fix_domains_for(own_fix_focus)",
     "    if True:\n"
     "        return \"\"\n"
     "    dom = fix_domains_for(own_fix_focus)",
     ["tests/test_g4_source.py"]),

    ("M-R1-2", "守卫退回只看 puzzle/answer/fact.text(R2 第一版)",
     "story/llm.py",
     "    if (list(new.completion_fact_ids or [])\n"
     "            != list(old.completion_fact_ids or [])):",
     "    if False:",
     ["tests/test_g4_source.py"]),

    ("M-R1-3", "允许 support -> core(不查方向)",
     "story/llm.py",
     "            if not (of.kind == \"core\" and nk == \"support\"):",
     "            if False:",
     ["tests/test_g4_source.py"]),

    ("M-R1-4", "守卫**误伤**其它 fixable(不判有没有 core-count)",
     "story/llm.py",
     "    if not any(_CORE_COUNT_MARK in str(f) for f in (own_fix_focus or [])):\n"
     "        return \"\"\n"
     "    dom = fix_domains_for(own_fix_focus)",
     "    dom = {\"facts_kind\"}",
     ["tests/test_g4_source.py", "tests/test_llm.py",
      "tests/test_solve_ux.py"]),

    ("M-R1-5", "审稿技术失败退回冒充结构失败",
     "story/llm.py",
     "                return _bail(\"审稿技术失败: \" + str(why)[:120],\n"
     "                             \"review_technical_fail\")",
     "                return _bail(\"审稿技术失败: \" + str(why)[:120],\n"
     "                             \"structure_technical_fail\")",
     ["tests/test_g4_source.py"]),

    ("M-R1-6", "core-count 与别的 fixable 并存时把域锁死(误伤)",
     "story/quality.py",
     "    ((\"core hidden facts 有\",), (\"facts_kind\",)),",
     "    ((\"core hidden facts 有\",), (\"facts_kind\",)),\n"
     "    ((\"谜面结尾不是问句\",), ()),\n"
     "    ((\"core_answer 有\",), ()),\n"
     "    ((\"的 quote 不在谜面里\",), ()),",
     ["tests/test_g4_source.py"]),
]


def _read(p):
    return io.open(ROOT / p, encoding="utf-8").read()


def _write(p, s):
    io.open(ROOT / p, "w", encoding="utf-8").write(s)


def run_suite(rel):
    r = subprocess.run([PY, "-X", "utf8", str(ROOT / rel)],
                       cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=600)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def main():
    BAK.mkdir(exist_ok=True)
    results = []
    for num, desc, path, old, new, suites in MUTATIONS:
        src = _read(path)
        if old not in src:
            print(f"[{num}] **补丁锚点找不到** —— 变异没打上: {desc}")
            results.append((num, desc, "ANCHOR-MISS", []))
            continue
        if src.count(old) != 1:
            print(f"[{num}] **锚点不唯一**({src.count(old)} 处): {desc}")
            results.append((num, desc, "ANCHOR-AMBIG", []))
            continue
        _write(path, src.replace(old, new, 1))
        try:
            reds = []
            for s in suites:
                rc, out = run_suite(s)
                if rc != 0 or "FAIL" in out or "Traceback" in out:
                    reds.append(os.path.basename(s))
            verdict = "RED" if reds else "**GREEN(变异没被抓住!)**"
            print(f"[{num}] {verdict}  {desc}  红: {reds}")
            results.append((num, desc, verdict, reds))
        finally:
            _write(path, src)
    print("\n" + "=" * 72)
    bad = [r for r in results if not r[2].startswith("RED")]
    for num, desc, verdict, _ in results:
        print(f"  {num:<10} {verdict:<28} {desc}")
    print("=" * 72)
    print(f"共 {len(results)} 条, {len(results) - len(bad)} 条成功变红")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
