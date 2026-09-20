#!/usr/bin/env python
# coding: utf-8
"""运行: uv run tests/test_experiment_short_surface.py(完全离线, 0 LLM 调用)。

守 R3 这个**最小对照实验**的边界。R3 的全部价值在于"**只换一个变量**":
同样那 10 条汤底, 只改"汤面怎么截"。一旦别的变量被顺手动掉, 对照就不成立,
而产物**看上去仍然正常**(照样出 10 道题) —— 这是最危险的失效。

所以守四条:

  1. **不重新生成故事。** 关键词 / core_truth / answer 必须**从 R2
     raw.jsonl 读**, 不得调用 `gen_keyword_idea`, 不得自己抽词。
  2. **截取 prompt 必须极短。** 它是这一轮的自变量。加了 fair clue /
     问句 / 反转数量之类的规则, 实验就不再回答它声称的问题。
  3. **stage B 一个字不改。** 必须调生产的
     `structure_original_idea`, 不得 monkeypatch 生产常量。
  4. **不重抽。** 技术失败之外不得重试; 拒绝照实记录。

另有一条与本实验定位有关: **不做自动评分 / 不另加判定器**。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

TOOL = REPO / "tools" / "experiment_short_surface.py"

FAIL = [0]


def check(name, cond, extra=""):
    if cond:
        print(f"  ok  {name}")
        return
    print(f"  FAIL {name}  {extra}")
    FAIL[0] += 1


SRC = TOOL.read_text(encoding="utf-8")


def _fresh_tree() -> ast.AST:
    """⚠️ **现读现 parse**, 不用 import 时算一次的快照。

    CPython 的 `.pyc` 新鲜度只按**秒**比对 mtime, 而"改源码 -> 立刻跑测试"
    常落在同一秒 —— 那时快照/字节码都是旧的, 变异测试会**假绿**。
    (R1/R2 都实测踩过。) 所以每个检查都重新读盘。
    """
    return ast.parse(TOOL.read_text(encoding="utf-8"))


def _literal(name: str):
    for node in _fresh_tree().body:
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            if getattr(t, "id", None) == name:
                return ast.literal_eval(node.value)
    raise AssertionError(f"源码里找不到模块级常量 {name}")


def _code_only_source() -> str:
    """剔除所有 docstring 后的代码文本 —— 检查只看**会执行的代码**。"""
    tree = _fresh_tree()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = getattr(node, "body", None)
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


# ======================================================================
# 一、不重新生成故事
# ======================================================================
def test_reuses_r2_stories_only():
    """关键词/core_truth/answer 必须来自 R2, 不得重新生成。"""
    print("\n[SS1] 只复用 R2 的汤底")
    code = _code_only_source()
    check("读 R2 raw.jsonl", "full_chain_experiment" in code
          or "R2_RAW" in code)
    # 绝不重新跑 Stage A / 抽词。
    for bad in ("gen_keyword_idea", "load_bag", "KeywordBag", "bag.draw"):
        check(f"不调用 {bad}", bad not in code, f"出现: {bad}")
    # 必须把 R2 的三样搬过来。
    for f in ("core_truth", "answer", "r2_puzzle"):
        check(f"archive 里带 {f}", f in code, f"缺 {f}")


def test_extraction_uses_canonical_answer_not_clues():
    """截取只喂完整汤底, **不喂** observed_clues。

    喂了 clues 就等于把 R2 那条"信息提前暴露"的路径搬回来, 变量不止一个。

    ⚠️ 这里查的是 **user message 是怎么拼出来的** 和 **真正传给模型的东西**,
    不是"全文里有没有出现 observed_clues 这个词" —— 脚本的 docstring /
    manifest 说明 / 报告标题里**必须**能写"不附 observed_clues"这种句子
    (那正是它要声明的事)。用子串查会把这些说明当成违规, 逼着人把文档写
    得更含糊, 那是反效果。(第一版就是这么误报的。)
    """
    print("\n[SS2] 截取不用 observed_clues")
    # 1) user 模板只含 answer 占位。
    tpl = _literal("_USER_TEMPLATE")
    check("user 模板只含 answer 占位",
          tpl.count("{") == 1 and "{answer}" in tpl, tpl)

    # 2) 组装 user 的那个函数里**只许用 answer** —— 不得拼别的任何东西。
    #
    # ⚠️ 只查 "observed_clues" 这个词是不够的: 换一个变量名(比如把线索
    # 存成 `rec_clues` 再拼进去)就绕过了。变异测试实测过这个洞。
    # 所以这里反过来查**白名单**: user 只能由 `_USER_TEMPLATE.format(
    # answer=...)` 构成, 不允许出现对其它字段的引用。
    tree = _fresh_tree()
    found = False
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef)
                and node.name == "_surface_user"):
            continue
        found = True
        # 该函数体里所有被读到的属性名 / 变量名 / 下标常量。
        used = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name):
                used.add(sub.id)
            elif isinstance(sub, ast.Attribute):
                used.add(sub.attr)
            elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                used.add(sub.value)
            elif isinstance(sub, ast.Subscript):
                sl = sub.slice
                if isinstance(sl, ast.Constant):
                    used.add(str(sl.value))
        # 允许: 函数自身、参数、模板常量、answer 关键字。
        allowed = {"_surface_user", "_USER_TEMPLATE", "answer", "format",
                   "str", "return", "rec"}
        extra = sorted(x for x in used if x not in allowed)
        check("_surface_user 只用 answer(不拼别的字段)", not extra,
              f"还引用了: {extra}")
    check("存在 _surface_user 函数", found)

    # 3) 传给截取调用的 user 必须是 _surface_user(...) 的结果。
    code = _code_only_source()
    check("截取调用用 _surface_user 组 user", "_surface_user(" in code)


# ======================================================================
# 二、截取 prompt 必须极短
# ======================================================================
def test_surface_prompt_stays_minimal():
    """截取 prompt 是这轮的自变量 —— 不许扩写成规则手册。"""
    print("\n[SS3] 截取 prompt 极短")
    s = _literal("SURFACE_SYSTEM")
    check("SURFACE_SYSTEM <= 120 字", len(s) <= 120, f"实际 {len(s)} 字")
    # 必须真的说了"截取 / 不要概括 / 简短"这三件事。
    check("说了'截取'", "截取" in s)
    check("说了'不要概括完整故事'", "不要概括" in s)
    check("说了'1～3 句'", "1～3" in s)
    # 不得出现任务书点名禁止的新规则。
    banned = ["fair", "clue", "反转", "问句", "人物关系", "必须几条",
              "字数上限", "机关", "至少", "恰好", "discovery", "atom"]
    hit = [b for b in banned if b in s]
    check("不含被禁止的新规则", not hit, f"命中 {hit}")

    # schema 只要一个字段。
    tool = _literal("_TOOL_SURFACE")
    props = tool["input_schema"]["properties"]
    check("schema 只有 puzzle 一个字段", list(props) == ["puzzle"],
          f"实际 {sorted(props)}")


# ======================================================================
# 三、Stage B 一个字不改
# ======================================================================
def test_stage_b_calls_production_unchanged():
    """必须调生产的 structure_original_idea, 且不得改生产常量。"""
    print("\n[SS4] Stage B 用生产原样")
    code = _code_only_source()
    check("调用 structure_original_idea", "structure_original_idea" in code)
    check("用 PuzzleWriter", "PuzzleWriter(" in code)
    check("不自写结构器", "_structure_user_prompt" not in code)

    prod_names = {"KEYWORD_IDEA_SYSTEM", "STRUCTURE_SYSTEM", "CHECK_SYSTEM",
                  "RIDDLE_SYSTEM", "ANSWER_SYSTEM"}
    offenders = set()
    for node in ast.walk(_fresh_tree()):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        for t in targets:
            nm = getattr(t, "id", None) or getattr(t, "attr", None)
            if nm in prod_names:
                offenders.add(nm)
    check("不重新赋值任何生产 SYSTEM 常量", not offenders,
          f"改了: {sorted(offenders)}")
    for bad in ("_keywords_prompt =", "monkeypatch", "setattr(story.llm"):
        check(f"无「{bad}」", bad not in code)


def test_no_resample():
    """技术失败之外不重试; Stage B 拒了不重抽。"""
    print("\n[SS5] 不重抽")
    called = set()
    for node in ast.walk(_fresh_tree()):
        if isinstance(node, ast.Call):
            f = node.func
            nm = getattr(f, "id", None) or getattr(f, "attr", None)
            if nm:
                called.add(nm.lower())
    banned_roots = ("resample", "retry_until", "best_of", "regenerate",
                    "redraw")
    hit = sorted(c for c in called if c.startswith(banned_roots))
    check("无重抽函数调用", not hit, f"命中 {hit}")

    # structure_original_idea 不得位于任何循环内(拒绝不重抽)。
    tree = _fresh_tree()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef)
                and node.name == "_run_stage_b"):
            continue
        loop_ids = set()
        for sub in ast.walk(node):
            if isinstance(sub, (ast.While, ast.For)):
                for inner in ast.walk(sub):
                    loop_ids.add(id(inner))
        in_loop = [getattr(s.func, "attr", None)
                   for s in ast.walk(node)
                   if isinstance(s, ast.Call)
                   and getattr(s.func, "attr", None)
                   == "structure_original_idea"
                   and id(s) in loop_ids]
        check("stage B 调用不在循环里", not in_loop, f"在循环里: {in_loop}")

    # 技术重试上界是个小常数。
    check("技术重试上界 <= 3", _literal("MAX_TECHNICAL_RETRIES") <= 3,
          f"实际 {_literal('MAX_TECHNICAL_RETRIES')}")


# ======================================================================
# 四、不碰生产数据 / 不评分
# ======================================================================
def test_never_writes_production_paths():
    """不入池、不写生产账本。"""
    print("\n[SS6] 不碰生产数据")
    code = _code_only_source()
    banned = ["pool.jsonl", "played.jsonl", "pool_used.jsonl",
              "curated_pool.jsonl", "curated_used.jsonl", "puzzle.jsonl",
              "submit_riddle", "PuzzlePool", "_archive_reveal"]
    hit = [b for b in banned if b in code]
    check("无生产题库路径/入口", not hit, f"命中 {hit}")
    check("只写 data/short_surface_experiment/",
          "short_surface_experiment" in SRC)


def test_no_auto_scoring():
    """不做自动评分 / 不另加判定器。"""
    print("\n[SS7] 不自动评分")
    banned_roots = ("score", "rank", "classif", "judge", "grade",
                    "rate", "filter", "select", "sort", "best_of",
                    "pick_best", "choose", "winner")
    _STDLIB_OK = {"sorted", "sort"}
    called = set()
    for node in ast.walk(_fresh_tree()):
        if isinstance(node, ast.Call):
            f = node.func
            nm = getattr(f, "id", None) or getattr(f, "attr", None)
            if nm:
                called.add(nm.lower())
    hit = sorted(c for c in called
                 if c not in _STDLIB_OK and c.startswith(banned_roots))
    check("无评分/排名/筛选函数调用", not hit, f"命中 {hit}")


def test_import_does_not_touch_production():
    """import 本工具**不得**产生任何生产副作用。"""
    print("\n[SS8] import 无生产副作用")
    import subprocess
    import hashlib

    def snap():
        out = {}
        for rel in ("data/pool.jsonl", "data/played.jsonl",
                    "data/pool_used.jsonl", "data/curated_pool.jsonl",
                    "data/puzzle.jsonl"):
            p = REPO / rel
            out[rel] = (hashlib.sha1(p.read_bytes()).hexdigest()
                        if p.exists() else "(absent)")
        return out

    before = snap()
    r = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, r'%s'); "
         "import tools.experiment_short_surface as E; "
         "print(len(E.SURFACE_SYSTEM))" % str(REPO)],
        cwd=str(REPO), capture_output=True, text=True, timeout=180)
    check("子进程 import 成功", r.returncode == 0, (r.stderr or "")[-400:])
    after = snap()
    changed = [k for k in before if before[k] != after[k]]
    check("生产数据文件 0 变化", not changed, f"变化 {changed}")


def main() -> int:
    test_reuses_r2_stories_only()
    test_extraction_uses_canonical_answer_not_clues()
    test_surface_prompt_stays_minimal()
    test_stage_b_calls_production_unchanged()
    test_no_resample()
    test_never_writes_production_paths()
    test_no_auto_scoring()
    test_import_does_not_touch_production()
    print()
    if FAIL[0]:
        print(f"[SS] {FAIL[0]} 条 FAIL")
        return 1
    print("[SS] 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
