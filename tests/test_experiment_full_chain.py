#!/usr/bin/env python
# coding: utf-8
"""运行: uv run tests/test_experiment_full_chain.py(完全离线, 0 LLM 调用)。

守 `tools/experiment_full_chain.py` 的**四条边界**。这些不是审美判断, 是
这个实验能不能回答它声称的问题的前提。

  1. **不改 production prompt。** 本实验的全部价值在于"看**当前代码**
     会产出什么"。一旦有人顺手在这里改一句 Stage A/B 的提示词, 或者
     monkeypatch 掉生产常量, 结论就不再描述生产 —— 而产物**看上去仍然
     正常**(题照样生成), 这是最危险的失效。所以静态钉住: 不得赋值/
     重定义生产的 SYSTEM 常量, 不得 monkeypatch `_keywords_prompt`。

  2. **lane 注入必须只发生在 Stage A。** 用**真** `KEYWORD_IDEA_SYSTEM`
     字符串构造一次假 client, 断言: Stage A 的调用被贴了一行、Stage B 的
     调用**一个字节都没动**。这条是本工具最容易写错的地方(贴错 system 会
     静默改掉审稿人看到的文本)。

  3. **拒绝不重抽。** Stage B 拒了就是拒了 —— 不得出现"重抽到通过"的
     循环。抽词/生成各自只跑一次。

  4. **不碰生产数据。** 不入池、不写 played / pool_used / archive。

另有一条与本实验定位有关的: **不做自动评分 / 红黑判定器**。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

TOOL = REPO / "tools" / "experiment_full_chain.py"

FAIL = [0]


def check(name, cond, extra=""):
    if cond:
        print(f"  ok  {name}")
        return
    print(f"  FAIL {name}  {extra}")
    FAIL[0] += 1


SRC = TOOL.read_text(encoding="utf-8")
#: ⚠️ **现读现 parse**, 不用 import 时算一次的快照 —— CPython `.pyc` 的
#: 新鲜度只按**秒**比对 mtime, 而"改源码 -> 立刻跑测试"常落在同一秒, 那时
#: 快照/字节码都是旧的, 变异测试会假绿。(R1 实测踩过: 源码里加了字段,
#: 测试仍全绿。) 见 test_experiment_red_black_core.py 里同一段说明。
def _fresh_tree() -> ast.AST:
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
# 一、不改 production prompt
# ======================================================================
def test_does_not_touch_production_prompts():
    """不得改/重定义生产的 prompt 常量, 不得 monkeypatch。"""
    print("\n[FC1] 不改 production prompt")
    code = _code_only_source()
    # 生产的 SYSTEM 常量名: 一旦出现在**赋值左侧**, 就是在改它。
    #
    # ⚠️ 只报告**命中的**那几个, 不对每个赋值都打一条 ok —— 第一版把
    # 脚本里几十个普通赋值(`rec = {...}` / `t0 = ...`)全打印成 "ok",
    # 把一个"有没有改生产常量"的检查淹没在噪音里。
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

    # 不得 monkeypatch 生产函数。
    for bad in ("_keywords_prompt =", "KEYWORD_IDEA_SYSTEM =",
                "monkeypatch", "setattr(story.llm"):
        check(f"无「{bad}」", bad not in code, f"出现: {bad}")

    # ⚠️ `KEYWORD_IDEA_SYSTEM` **允许**出现在代码里 —— `_LaneClient` 必须
    # 按**全等**认出 Stage A 那一次调用。要禁的不是"提到它", 而是"拿它做
    # 替换/拼接之外的用途"。所以只钉一条: 它只能出现在 `_LaneClient` 里。
    tree = _fresh_tree()
    holders = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Name)
                        and sub.id == "KEYWORD_IDEA_SYSTEM"):
                    holders.add(node.name)
        elif isinstance(node, ast.FunctionDef):
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Name)
                        and sub.id == "KEYWORD_IDEA_SYSTEM"):
                    holders.add(node.name)
    allowed = {"_LaneClient", "__init__", "_one_puzzle"}
    extra = holders - allowed
    check("KEYWORD_IDEA_SYSTEM 只出现在 _LaneClient 相关处", not extra,
          f"另外出现在: {sorted(extra)}")


def test_reuses_production_entry_points():
    """必须调用**生产方法**, 不是另写一套。"""
    print("\n[FC2] 复用生产入口")
    imports = set()
    for node in ast.walk(_fresh_tree()):
        if isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
        elif isinstance(node, ast.Import):
            for a in node.names:
                imports.add(a.name)
    check("import story.llm(取 PuzzleWriter/client)",
          "story.llm" in imports)
    code = _code_only_source()
    check("调用 gen_keyword_idea", "gen_keyword_idea" in code)
    check("调用 structure_original_idea", "structure_original_idea" in code)
    check("用 PuzzleWriter", "PuzzleWriter(" in code)
    check("走生产 KeywordBag(load_bag)", "load_bag" in code)
    # 不得自己拼 Stage B 的 user prompt。
    check("不自写 Stage B user prompt",
          "_structure_user_prompt" not in code)


# ======================================================================
# 二、lane 注入只发生在 Stage A
# ======================================================================
def test_lane_injection_only_hits_stage_a():
    """**最要紧的一条**: Stage A 贴一行, 其余调用逐字节透传。

    用真的 `KEYWORD_IDEA_SYSTEM` 构造假 inner client, 断言:
      * 传 Stage A 的 system -> user 前面多一行;
      * 传任何别的 system(Stage B / Reviewer) -> user **一字未动**;
      * 幂等: 已经带 lane 行就不再贴第二行。
    """
    print("\n[FC3] lane 只贴 Stage A")
    from story.llm import KEYWORD_IDEA_SYSTEM
    import tools.experiment_full_chain as E

    class _FakeInner:
        def __init__(self):
            self.calls = []

        def messages(self, system, user, **kw):
            self.calls.append((system, user))
            return "ok"

    lane = "类型：红汤。"
    inner = _FakeInner()
    lc = E._LaneClient(inner, lane)

    # ---- Stage A: 应被贴一行 ----
    lc.messages(KEYWORD_IDEA_SYSTEM, "关键词：A，B\n\n请围绕这几个关键词写…")
    sys_a, user_a = inner.calls[-1]
    check("Stage A user 以 lane 行开头", user_a.startswith(lane + "\n"),
          f"实际: {user_a[:40]}")
    check("Stage A 原文仍在", "关键词：A，B" in user_a)
    check("Stage A 记数 rewritten=1", lc.rewritten == 1, f"{lc.rewritten}")

    # ---- 别的 system: 必须一字未动 ----
    other = "你是结构编辑。把谜题搬进 schema。"
    lc.messages(other, "═══ 谜面(canonical)═══\n某个谜面")
    sys_b, user_b = inner.calls[-1]
    check("Stage B user 完全未改", user_b == "═══ 谜面(canonical)═══\n某个谜面",
          f"实际: {user_b[:40]}")
    check("Stage B system 未被换", sys_b == other)
    check("Stage B 记数 passed_through=1", lc.passed_through == 1,
          f"{lc.passed_through}")
    check("Stage B 未增加 rewritten", lc.rewritten == 1, f"{lc.rewritten}")

    # ---- 幂等: 再喂一遍已带 lane 的文本 ----
    lc.messages(KEYWORD_IDEA_SYSTEM, lane + "\n关键词：A，B")
    _s, user_c = inner.calls[-1]
    check("已带 lane 行时不重复贴",
          user_c.count(lane) == 1, f"出现 {user_c.count(lane)} 次")

    # ---- 判定必须按**全等**, 不是子串 ----
    # 构造一个"包含 KEYWORD_IDEA_SYSTEM 但更长"的 system —— 不该被当成
    # Stage A(否则任何提到它的 system 都会被误改)。
    near = KEYWORD_IDEA_SYSTEM + "\n(额外一行)"
    lc.messages(near, "不该被改")
    _s, user_d = inner.calls[-1]
    check("近似 system 不被当成 Stage A", user_d == "不该被改",
          f"实际: {user_d[:40]}")


def test_lane_line_is_short():
    """lane 行必须**很短** —— 不许借机塞几十条规则。"""
    print("\n[FC4] lane 行要短")
    tpl = _literal("LANE_LINE")
    check("lane 模板 <= 20 字", len(tpl) <= 20, f"实际 {len(tpl)} 字")
    check("lane 模板只有一个占位符", tpl.count("{") == 1, tpl)
    for bad in ("必须", "不要", "至少", "字数", "机关", "线索"):
        check(f"lane 不含「{bad}」", bad not in tpl)


# ======================================================================
# 三、拒绝不重抽 / 四、不碰生产数据
# ======================================================================
def test_no_resample_on_reject():
    """Stage B 拒了不许重抽到通过。"""
    print("\n[FC5] 拒绝不重抽")
    # ⚠️ 查**被调用的名字**, 不查文本 —— manifest 里有一个
    # `no_resample_on_reject` 的**说明字段**(它的作用正是声明"不重抽"),
    # 用子串查会把它当成违规。混淆"描述某个概念"与"使用某个入口", 会
    # 逼着人把说明写得含糊, 那是反效果。
    banned_roots = ("resample", "retry_until", "best_of", "regenerate",
                    "redraw")
    called = set()
    for node in ast.walk(_fresh_tree()):
        if isinstance(node, ast.Call):
            f = node.func
            nm = getattr(f, "id", None) or getattr(f, "attr", None)
            if nm:
                called.add(nm.lower())
    hit = sorted(c for c in called if c.startswith(banned_roots))
    check("无重抽函数调用", not hit, f"命中 {hit}")

    # ⚠️ 还要挡住**循环重试**这种写法: `while ...: <再次生成>`。
    # 第一版只查函数名, 于是 `while not spec_ok: pass` 这种"重抽到通过"
    # 的结构**直接溜过去**(变异测试实测 0 FAIL)。所以这里按**结构**查:
    # 在 `_one_puzzle` 里, 包含 Stage A/B 调用的那个语句**不得**位于任何
    # while 循环内。
    tree = _fresh_tree()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef)
                and node.name == "_one_puzzle"):
            continue
        loop_body_ids = set()
        for sub in ast.walk(node):
            if isinstance(sub, (ast.While, ast.For)):
                for inner in ast.walk(sub):
                    loop_body_ids.add(id(inner))
        in_loop = []
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                nm = getattr(sub.func, "attr", None)
                if nm in ("gen_keyword_idea", "structure_original_idea"):
                    if id(sub) in loop_body_ids:
                        in_loop.append(nm)
        check("Stage A/B 调用不在循环里(拒绝不重抽)", not in_loop,
              f"在循环里: {in_loop}")

    # gen_keyword_idea / structure_original_idea 各只调一次。
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef)
                and node.name == "_one_puzzle"):
            continue
        calls = []
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                nm = getattr(sub.func, "attr", None)
                if nm in ("gen_keyword_idea", "structure_original_idea"):
                    calls.append(nm)
        check("_one_puzzle 里 Stage A/B 各调一次",
              calls.count("gen_keyword_idea") == 1
              and calls.count("structure_original_idea") == 1, f"{calls}")


def test_never_writes_production_paths():
    """不入池、不写生产账本。"""
    print("\n[FC6] 不碰生产数据")
    code = _code_only_source()
    banned = ["pool.jsonl", "played.jsonl", "pool_used.jsonl",
              "curated_pool.jsonl", "curated_used.jsonl", "puzzle.jsonl",
              "submit_riddle", "PuzzlePool", "_archive_reveal"]
    hit = [b for b in banned if b in code]
    check("无生产题库路径/入口", not hit, f"命中 {hit}")
    check("只写 data/full_chain_experiment/",
          "full_chain_experiment" in SRC)


def test_no_auto_scoring():
    """不做自动评分 / 红黑判定器。"""
    print("\n[FC7] 不自动评分")
    code = _code_only_source()
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
    print("\n[FC8] import 无生产副作用")
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
         "import tools.experiment_full_chain as E; print(E.N_PER_TYPE)"
         % str(REPO)],
        cwd=str(REPO), capture_output=True, text=True, timeout=180)
    check("子进程 import 成功", r.returncode == 0, (r.stderr or "")[-400:])
    check("N_PER_TYPE == 5(用户口径)", (r.stdout or "").strip() == "5",
          f"实际 {r.stdout!r}")
    after = snap()
    changed = [k for k in before if before[k] != after[k]]
    check("生产数据文件 0 变化", not changed, f"变化 {changed}")


def main() -> int:
    test_does_not_touch_production_prompts()
    test_reuses_production_entry_points()
    test_lane_injection_only_hits_stage_a()
    test_lane_line_is_short()
    test_no_resample_on_reject()
    test_never_writes_production_paths()
    test_no_auto_scoring()
    test_import_does_not_touch_production()
    print()
    if FAIL[0]:
        print(f"[FC] {FAIL[0]} 条 FAIL")
        return 1
    print("[FC] 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
