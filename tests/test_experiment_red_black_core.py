#!/usr/bin/env python
# coding: utf-8
"""运行: uv run tests/test_experiment_red_black_core.py(完全离线, 0 LLM 调用)。

实验 `tools/experiment_red_black_core.py` 守的是**两条纪律**, 而不是它跑出来的
故事好不好 —— 故事好不好要人读, 任何断言都代替不了那次阅读。

  1. **Prompt 必须短。** 本实验要问的是"没有几十条规则时, 模型原生产出什么"。
     一旦有人后来往 system 里补规则(哪怕出于好意), 那个问题就不再被回答,
     而**输出看上去仍然正常** —— 这是最危险的一种失效: 实验静默跑偏, 结果
     却照样生成, 报告里也不会出现任何异常。所以把长度上界**钉死**。
     长度不是审美, 它是这个实验的**自变量**。

  2. **只从生产借传输层, 不借生成政策。** import `PuzzleWriter` /
     `story.quality` / `story.puzzle` 会把生成政策常量一起拉进来, 那时
     "裸问"就不成立了。同时这些 import 也**不得**改变生产行为 ——
     本文件用 AST 检查实验脚本的顶层 import, 而不是靠"运行一下看看"。

另守两条与本实验定位有关的:

  3. **不碰生产数据。** 脚本里不得出现 pool / played / pool_used / archive
     的写入路径。实验跑一次就往生产题库里塞东西是不可接受的副作用。

  4. **不写汤面字段。** 本轮只生成隐藏故事。schema 里一旦出现 puzzle/title/
     facts, 就说明有人把实验扩成了"顺手也生成汤面" —— 那是下一轮的事。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

TOOL = REPO / "tools" / "experiment_red_black_core.py"

FAIL = [0]


def check(name, cond, extra=""):
    if cond:
        print(f"  ok  {name}")
        return
    print(f"  FAIL {name}  {extra}")
    FAIL[0] += 1


SRC = TOOL.read_text(encoding="utf-8")
#: ⚠️ **故意不在这里 parse 一份模块级 AST**。任何"import 时算一次"的快照
#: 都会在变异测试(先改源码、再跑测试, 同一个进程)里过期, 于是改坏了也不
#: 变红。所有检查一律走 `_fresh_tree()` 现读现 parse —— 见它的 docstring。


def _fresh_tree() -> ast.AST:
    """**每次重新读盘**再 parse。

    ⚠️ 不要用模块级的 `TREE` 去读被测常量。模块级 `TREE` 是本文件被 import
    时 parse 的一份快照; 若测试文件和被测文件在**同一次进程**里被先后改写
    (变异测试正是如此), 那份快照就过期了 —— 于是"改了源码"在测试里看不见。

    我实际踩到过这个: 源码里明明加进了 `puzzle` 字段, `_literal("_TOOL_CORE")`
    仍回 `['story']`, 变异测试假绿。教训与 `.pyc` 那条同源 —— **任何形式的
    缓存都会让"改坏了不报错"**, 所以这里一律现读现 parse。
    """
    return ast.parse(TOOL.read_text(encoding="utf-8"))


def _literal(name: str):
    """从**源码 AST** 里取一个模块级常量字面量(现读盘, 不走缓存)。"""
    for node in _fresh_tree().body:
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            if getattr(t, "id", None) == name:
                return ast.literal_eval(node.value)
    raise AssertionError(f"源码里找不到模块级常量 {name}")


def _code_only_source() -> str:
    """把源码里**所有文档字符串**剔除后重新生成的文本。

    ## 为什么必须有这个

    下面几条检查要找的是"代码里出现了生产入口 / 评分入口"。但 `SRC` 里
    混着**散文** —— 模块 docstring 会写"**不** import `PuzzleWriter`",
    报告模板会写"没有 quality_checks"。那些是**说明本实验不做什么**的
    句子, 却会被朴素的子串检查当成违规(第一版就是这样, 两条误报全在
    注释里)。

    所以检查必须只看**真的会执行的代码**。做法是把每个 `def`/`class`/
    模块的第一条 Expression 语句(docstring)从 AST 里摘掉再 dump ——
    而不是"把注释行删掉", 那样还能被 `# noqa` 之类的形式绕过。
    """
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


def _top_level_imports() -> set:
    """本文件里所有 import 的**模块名**(含函数内 import)。现读盘。"""
    names = set()
    for node in ast.walk(_fresh_tree()):
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
    return names


def _str_constants() -> dict:
    """所有字符串常量字面量(按内容 -> 出现次数)。现读盘。"""
    out = {}
    for node in ast.walk(_fresh_tree()):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out[node.value] = out.get(node.value, 0) + 1
    return out


# ======================================================================
# 一、Prompt 纪律
# ======================================================================
def test_prompts_stay_short():
    """两条 system 必须保持"几句话"量级。

    上界取 220 字: 现有版本各 ~90 字, 留一倍余量给措辞调整, 但**挡不住**
    几十条规则的扩写(那至少 800 字起)。这不是审美判断 —— 长度就是本实验
    的自变量, 变长了实验结论不再成立。
    """
    print("\n[RBC1] Prompt 必须短")
    for name in ("RED_SYSTEM", "BLACK_SYSTEM"):
        s = _literal(name)
        check(f"{name} <= 220 字", len(s) <= 220, f"实际 {len(s)} 字")
        # 必须真的说了"不要写谜面" —— 这是本轮唯一硬性交付约束。
        check(f"{name} 明说不要写谜面",
              "谜面" in s and ("不要写" in s or "别写" in s))
        # 不得出现生产 shape 词汇 —— 出现即说明规则被搬进来了。
        banned = ["fair_clues", "discovery_beats", "solve_atoms", "facts",
                  "signature", "completion", "minItems", "schema",
                  "第三人称", "第一人称", "问句", "字数", "不超过",
                  "必须包含", "至少", "恰好"]
        hit = [b for b in banned if b in s]
        check(f"{name} 不含生产 shape 词汇", not hit, f"命中 {hit}")

    check("user 模板 <= 60 字", len(_literal("USER_PROMPT")) <= 60,
          f"实际 {len(_literal('USER_PROMPT'))} 字")


def test_temperature_matches_production_generation():
    """温度必须是生产的出题档 —— 换温度就是在换问题。"""
    print("\n[RBC2] 温度口径")
    from story.config import Config
    temp = _literal("TEMPERATURE")
    check("temperature == Config.generate_temperature",
          abs(temp - Config().generate_temperature) < 1e-9,
          f"实验 {temp} vs 生产 {Config().generate_temperature}")


# ======================================================================
# 二、import 边界
# ======================================================================
def test_no_production_generation_imports():
    """不得 import 生成政策层 —— 否则"裸问"不成立。

    ⚠️ `story.keyword_seed` 是**例外且必须**: R1 明确要求复用生产同款的
    `KeywordBag` 两随机关键词。自己再写一个抽词器就等于把自变量换掉。
    所以这里禁止的是 `story.quality` / `story.puzzle`(生成政策层), 而
    `keyword_seed` 反过来是**要求存在**的 —— 见
    `test_reuses_production_keyword_bag`。
    """
    print("\n[RBC3] 只借传输层")
    imports = _top_level_imports()
    forbidden = {
        "story.quality", "story.puzzle",
        "story.engine", "story.state", "director",
    }
    hit = sorted(imports & forbidden)
    check("不 import 生成政策层", not hit, f"命中 {hit}")

    # ⚠️ 检查**代码**, 不是散文 —— 模块 docstring 里明说了"**不** import
    # PuzzleWriter", 那种句子正是本检查想鼓励的, 不该被当成违规。
    check("代码里不引用 PuzzleWriter",
          "PuzzleWriter" not in _code_only_source(),
          "可执行代码里出现了 PuzzleWriter")

    # 允许的接缝必须**真的**被用到(否则说明它在别处拿了别的东西)
    check("import 了 story.config", "story.config" in imports)
    check("import 了 story.llm", "story.llm" in imports)

    # 更严的一条: 从 story.llm 只许拿 client。
    got = set()
    for node in ast.walk(_fresh_tree()):
        if isinstance(node, ast.ImportFrom) and node.module == "story.llm":
            got |= {a.name for a in node.names}
    check("story.llm 只拿 AnthropicMessagesClient",
          got == {"AnthropicMessagesClient"}, f"实际 {sorted(got)}")


def test_reuses_production_keyword_bag():
    """必须复用**生产同款**的两随机关键词机制(R1 的核心要求)。

    守三件事:

      1. 不得再自造单主题池 `SUBJECTS` —— v1 就是那样跑偏的(模型迅速
         塌回温情/怀旧母题);
      2. 必须走 `load_bag` + `bag.draw()`, 也就是生产那条抽词路径;
      3. user prompt **不得**出现"也可以不围绕" —— 那句话把约束取消了。
    """
    print("\n[RBC3b] 复用生产两随机关键词")
    code = _code_only_source()
    check("代码里没有自造 SUBJECTS 池",
          "SUBJECTS = " not in code and "_draw_subjects" not in code,
          "仍存在自造主题池")
    check("走 load_bag(生产抽词)", "load_bag" in code)
    check("走 bag.draw()", ".draw()" in code)
    check("用生产词库路径(DEFAULT_CORPUS_PATH)",
          "DEFAULT_CORPUS_PATH" in code)

    # 三句必须**不存在**的话。
    tpl = _literal("USER_PROMPT")
    for bad in ("也可以不围绕", "可以不围绕", "或不围绕"):
        check(f"user 不含「{bad}」", bad not in tpl, f"实际: {tpl}")
    # 但必须**有**关键词占位。
    check("user 含两个关键词占位",
          "{a}" in tpl and "{b}" in tpl, f"实际: {tpl}")

    # 系统提示里也不得再出现"可以不围绕"之类。
    for nm in ("RED_SYSTEM", "BLACK_SYSTEM"):
        s = _literal(nm)
        check(f"{nm} 不含「不围绕」", "不围绕" not in s)


def test_experiment_import_does_not_touch_production():
    """import 实验模块**不得**产生任何生产副作用。

    做法: 在子进程里 import 它, 断言没有新增/改动生产数据文件。
    这比"读一遍源码觉得没问题"更硬 —— 副作用可以藏在 import 期执行的
    任意代码里。
    """
    print("\n[RBC4] import 无生产副作用")
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
         "import tools.experiment_red_black_core as E; "
         "print(E.RED_SYSTEM[:1] + E.BLACK_SYSTEM[:1])" % str(REPO)],
        cwd=str(REPO), capture_output=True, text=True, timeout=120)
    check("子进程 import 成功", r.returncode == 0,
          (r.stderr or "")[-400:])
    after = snap()
    changed = [k for k in before if before[k] != after[k]]
    check("生产数据文件 0 变化", not changed, f"变化 {changed}")


# ======================================================================
# 三、不碰生产数据 / 不写汤面
# ======================================================================
def test_never_writes_production_paths():
    """脚本里不得出现生产题库的写入路径。"""
    print("\n[RBC5] 不碰生产数据")
    banned = ["pool.jsonl", "played.jsonl", "pool_used.jsonl",
              "curated_pool.jsonl", "curated_used.jsonl", "puzzle.jsonl",
              "submit_riddle", "PuzzlePool", "_archive_reveal"]
    hit = [b for b in banned if b in SRC]
    check("无生产题库路径/入口", not hit, f"命中 {hit}")

    # 唯一允许写的目录就是本实验自己的。
    check("只写 data/red_black_core_experiment/",
          "red_black_core_experiment" in SRC)


def test_no_surface_fields_emitted():
    """schema 只许有一个字段: story。出现 puzzle/title/facts 即越界。"""
    print("\n[RBC6] 不生成汤面")
    tool = _literal("_TOOL_CORE")
    props = tool["input_schema"]["properties"]
    check("schema 只有一个字段", list(props) == ["story"],
          f"实际 {sorted(props)}")
    check("required == ['story']",
          tool["input_schema"]["required"] == ["story"])

    # 记录结构里也只许有 story 这一项内容字段。
    banned = ["puzzle", "title", "facts", "solve_atoms", "fair_clues",
              "discovery_beats", "completion_fact_ids", "hints",
              "signature", "reviewer", "score"]
    hit = [b for b in banned if b in props]
    check("schema 无汤面/结构化字段", not hit, f"命中 {hit}")


def test_keyword_draw_is_reproducible_and_real():
    """抽词必须**可复现**, 且抽出来的**确实是两个不同的真实词**。

    这里**真跑**生产的 `KeywordBag`(纯 CPU, 不打模型、不联网): 同一个
    base seed 派生出的 bag, 必须给出同一串 pair 序列 —— 否则报告里的
    `keywords` / `draw_index` 就复现不了, 那两条溯源字段等于装饰。

    另外确认抽出来的是**词库里的词**(不是自造的 subject 字符串): 每个词
    都必须出现在 `data/keyword2_vocabulary.json` 的 `keywords` 里。
    """
    print("\n[RBC7] 抽词可复现")
    import json
    import tools.experiment_red_black_core as E

    seed_red = _literal("SEED_RED")
    seed_black = _literal("SEED_BLACK")
    n = _literal("N_PER_TYPE")
    check("红黑 seed 不同", seed_red != seed_black)

    # ---- 可复现: 同 seed 两次建 bag, 序列一致 ----
    b1, m1, ss1 = E._make_bag(seed_red)
    b2, m2, ss2 = E._make_bag(seed_red)
    check("同 seed 同 session seed", ss1 == ss2, f"{ss1} vs {ss2}")
    seq1 = [tuple(b1.draw()["keywords"]) for _ in range(n)]
    seq2 = [tuple(b2.draw()["keywords"]) for _ in range(n)]
    check("同 seed 同 pair 序列", seq1 == seq2, f"{seq1} vs {seq2}")

    # ---- 每个 draw 都是两个**不同**的词 ----
    check("每道两个词互不相同",
          all(a != b for a, b in seq1),
          f"{[p for p in seq1 if p[0] == p[1]]}")
    check("每道恰好 2 个词", all(len(p) == 2 for p in seq1))

    # ---- 抽出来的必须是**词库里的真实词** ----
    vocab = set()
    with open(E._corpus_path(), encoding="utf-8") as f:
        vocab = set(json.load(f)["keywords"])
    allkw = [w for p in seq1 for w in p]
    notin = [w for w in allkw if w not in vocab]
    check("抽出的词都在真实词库里", not notin, f"不在词库: {notin}")

    # ---- 同一个 bag 内不重复抽同一个 pair(cooldown 生效) ----
    check("同一 bag 内 pair 不重复",
          len(set(seq1)) == len(seq1), f"{seq1}")


def test_no_literal_keyword_check_and_no_forcing():
    """**不检查字面命中, 也不强迫关键词成为机关**(R1 明确要求)。

    这条守的是"不要顺手加一个 checker": 一旦脚本里出现
    `if kw not in story: reject` 之类的逻辑, 实验就从"随机扰动"变成了
    "命题作文 + 命中判定", 产物会立刻变味。
    """
    print("\n[RBC9] 不检查字面命中")
    code = _code_only_source()
    # 不得在故事文本上做关键词包含判断。
    for bad in ("not in story", "not in rec[\"story\"]", "kw in story",
                "keyword in story", "require_keyword", "must_contain"):
        check(f"无「{bad}」", bad not in code, "出现了字面命中检查")
    # 也不得要求"必须成为机关"之类措辞。
    for nm in ("RED_SYSTEM", "BLACK_SYSTEM"):
        s = _literal(nm)
        for bad in ("机关", "字面", "必须出现", "必须包含"):
            check(f"{nm} 不含「{bad}」", bad not in s)


def test_no_auto_filtering_or_scoring():
    """不得有自动评分 / 筛选 / 重抽到满意。"""
    print("\n[RBC8] 不自动筛选")
    # 重抽只允许由 technical_error 触发。
    check("没有按内容重抽的分支",
          "不满意" not in SRC and "retry_on_quality" not in SRC)

    # ---- 不得有评分/排名/分类**入口** ----
    #
    # ⚠️ 这里查的是**被调用的名字**, 不是**文本**。
    # 原因: 本实验的报告模板里**必须**能写"没有 quality_checks / 不排名"
    # 这种句子(那正是它要说明的事), 而任何基于子串的检查都会把它们当成
    # 违规 —— 第一版就误报了两条, 全在说明性文字里。混淆"提到某个概念"
    # 和"使用某个入口", 会让这条检查逼着人把文档写得更含糊, 那是反效果。
    #
    # ⚠️ 匹配取"词根前缀", 但要**排除标准库内建** ——
    #   * 全等会放过 `score_story`(第一版就是这样, 变异测试实测假绿);
    #   * 裸子串会把 `enumerate`(含 `rate`) 判成违规;
    #   * 前缀匹配仍会把 `sorted`(以 `sort` 开头)判成违规, 而它只是本
    #     文件自己排序展示用的标准库函数。
    # 所以: 前缀匹配 + 一张**显式**的标准库白名单。白名单很短且都是内建,
    # 任何被调用的评分/筛选入口都不会撞进来。
    _STDLIB_OK = {"sorted", "sort"}
    banned_roots = ("score", "rank", "classif", "judge", "grade",
                    "rate", "filter", "select", "sort", "best_of",
                    "pick_best", "choose", "winner")
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

    # 也不许 import 任何判定/分类模块。
    imports = _top_level_imports()
    banned_mods = [m for m in imports
                   if any(k in m.lower()
                          for k in ("judge", "classif", "scor", "rank"))]
    check("不 import 判定/分类模块", not banned_mods, f"命中 {banned_mods}")

    # 重试上界必须是小常数(防"重试到满意"被写成一个循环)。
    check("技术重试上界 <= 3", _literal("MAX_TECHNICAL_RETRIES") <= 3,
          f"实际 {_literal('MAX_TECHNICAL_RETRIES')}")


def main() -> int:
    test_prompts_stay_short()
    test_temperature_matches_production_generation()
    test_no_production_generation_imports()
    test_reuses_production_keyword_bag()
    test_experiment_import_does_not_touch_production()
    test_never_writes_production_paths()
    test_no_surface_fields_emitted()
    test_keyword_draw_is_reproducible_and_real()
    test_no_auto_filtering_or_scoring()
    test_no_literal_keyword_check_and_no_forcing()
    print()
    if FAIL[0]:
        print(f"[RBC] {FAIL[0]} 条 FAIL")
        return 1
    print("[RBC] 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
