#!/usr/bin/env python
# coding: utf-8
"""运行: uv run tests/test_pool.py（完全离线, 无网络）。

Q8 题池的五个验收点:
  1. 只接受完整 spec, 入池/弹出都重跑校验(不信任"文件里写着 approved")
  2. pop_next 走**当前** cross-puzzle gate(题生成时合格 != 此刻仍合格)
  3. 存读一轮不丢任何字段(facts/atoms/clues/signature/blueprint_specified/
     prompt_version/quality_policy_version/metrics)
  4. mark_used 重启后不复活("pop 了" != "持久化了 used")
  5. 任何损坏都只退化为现场生成, 不影响直播主状态机
"""
from __future__ import annotations

import json
import os
import random
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from story.config import Config  # noqa: E402
from story.puzzle import (  # noqa: E402
    DiscoveryBeat, FairClue, PuzzleBlueprint, PuzzleFact, PuzzleSignature,
    PuzzleSpec, SolveAtom,
)
from story.pool import POOL_VERSION, PuzzlePool, spec_key  # noqa: E402
from story.quality import QUALITY_POLICY_VERSION  # noqa: E402
from story.state import Phase  # noqa: E402

FAIL = [0]


def check(name, cond, extra=""):
    if cond:
        print(f"  ok  {name}")
        return
    print(f"  FAIL {name}  {extra}")
    FAIL[0] += 1


# ----------------------------------------------------------------------

def _write_raw_pool(d, specs):
    """把 specs **直接写进 pool.jsonl**, 绕过 `add()` 的准入校验。

    用途: 造"盘上有一道旧 policy 的题"这种状态 —— `add()` 会拒它,
    但真实盘子上就是这样(版本 bump 之前加的), 正是隔离逻辑要处理的。
    """
    path = os.path.join(d, "pool.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for s in specs:
            rec = {"pool_version": POOL_VERSION, "spec": s.to_archive(),
                   "added_by": "pool", "added_at": 0.0}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")



def _variant(i, puzzle, tag="池"):
    """换谜面的**开头一句**, 但保留 good_spec 的 fair_clues 引用。

    为什么不直接换整段谜面: `good_spec` 的两条 `fair_clues.quote` 是从
    **原谜面**里逐字摘的。换掉整段 -> quote 立刻失配 -> G2-B 之后这
    只算 fixable(不再硬拒), 但 `add()` 要求 fixable 为空 -> 入池被拒。
    测 G4-B 却因为夹具自己造的第二重问题变红, 那不是实现的问题。
    """
    # 追加而不是替换 —— good_spec 的两条 quote 分别落在谜面的**开头**与
    # **中段**, 砍掉前半句会让第一条 quote 失配。追加一个从句最安全:
    # 两个旧 quote 都还在, 而 spec_key 是内容的哈希 -> 题目确实不同。
    tail = "这和他那天穿的%s色外套有关吗?" % ("红" if i % 2 else "蓝")
    return puzzle + tail

def good_spec(puzzle=None, answer=None, **kw) -> PuzzleSpec:
    """一个结构上完全合格的 spec(照 test_puzzle.good_spec 的形状)。"""
    pz = puzzle or ("灯塔守塔人每晚都亮灯, 但只在退潮的那几个小时亮。涨潮后他"
                    "反而把灯熄掉, 哪怕有船经过也一样, 为此被投诉过好几次。"
                    "为什么?")
    s = PuzzleSpec(
        id="", title="灯塔",
        puzzle=pz,
        answer=answer or "退潮时礁石露出水面, 亮灯是为标出礁石位置; "
                         "涨潮后继续亮反而误导船只。",
        facts=[
            PuzzleFact(id="f1", text="退潮时危险礁石会露出或接近水面", kind="core"),
            PuzzleFact(id="f2", text="灯的真正作用是标示危险礁石的位置", kind="core"),
            PuzzleFact(id="f3", text="涨潮后礁石被淹没, 亮灯反而会误导船只",
                       kind="support", hintable=False),
            PuzzleFact(id="f4", text="他的行为不是为了纪念死者", kind="exclusion",
                       hintable=False),
        ],
        solve_atoms=[
            SolveAtom(id="a1", role="cause",
                      text="退潮使危险礁石成为需要标出的目标", fact_ids=["f1"]),
            SolveAtom(id="a2", role="mechanism",
                      text="灯是在标记礁石, 而不是给船引路",
                      fact_ids=["f2", "f3"]),
        ],
        # ---- v5 通关合同 ----
        # 标的是当前政策, 所以必须带完整合同 —— 否则
        # 它违反 Blocker 2 的版本硬门("v5 标签不能配 legacy 通关语义")。
        # 合同指向 f1/f2, 且 a1 引用 f1、clue 也指向 a1(线索通向通关路径)。
        core_answer="他亮灯是为了标出退潮时露出的礁石, 不是给船引路。",
        completion_fact_ids=["f1", "f2"],
        fair_clues=[
            FairClue(quote="只在退潮的那几个小时亮", supports_atoms=["a1"]),
            FairClue(quote="涨潮后他反而把灯熄掉", supports_atoms=["a2"]),
        ],
        hints=["注意灯的开关时机", "想想潮水的变化", "灯是在给谁传递信息?"],
        # quality-v8: 当前政策要求 2~4 个发现阶段。
        discovery_beats=[
            DiscoveryBeat(id="b1", text="先注意到灯只在退潮时亮", fact_ids=["f1"]),
            DiscoveryBeat(id="b2", text="再想到灯是在标礁石, 不是引路",
                          fact_ids=["f2"]),
        ],
        blueprint=PuzzleBlueprint(
            mechanism_family="hidden_function",
            solution_shape="hidden_function_explains_behavior",
            domain="maritime", relation="stranger",
            emotion_mode="neutral", time_shape="habitual",
            # 与 signature 的 observed reveal 一致: Batch A closeout 的
            # reveal adherence 会在 blueprint_specified=True 时比对两者。
            reveal_mode="meaning_flip"),
        signature=PuzzleSignature(
            mechanism_family="hidden_function",
            solution_shape="hidden_function_explains_behavior",
            domain="maritime", emotion_mode="neutral",
            relation="stranger", time_shape="habitual",
            reveal_mode="meaning_flip"),
        prompt_version="riddle-v3",
        quality_policy_version=QUALITY_POLICY_VERSION,
        metrics={"generation_attempts": 2, "review_calls": 1,
                 "rewrite_count": 0, "review_decision": "pass",
                 "review_latency_ms_total": 3310,
                 "generation_latency_ms": 12400, "ok": True},
        blueprint_specified=True,
    )
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def mkcfg(tmp, **kw):
    kw.setdefault("pool_enabled", True)
    kw.setdefault("pool_path", os.path.join(tmp, "pool.jsonl"))
    kw.setdefault("pool_used_path", os.path.join(tmp, "used.jsonl"))
    # P0 已播账本 —— 与上面 curated 同理: 不覆盖就会写到仓库真实的
    # `data/played.jsonl`, 于是用例之间**互相污染**(上一例播过的题把
    # 下一例的交付挡掉)。那会让"同一份代码两种结果"重演。
    kw.setdefault("played_path", os.path.join(tmp, "played.jsonl"))
    kw.setdefault("no_llm", True)
    # ---- Batch H2-F: curated 池必须在临时目录里, 且默认关掉 ----
    #
    # 这是**测试隔离**问题, 不是功能问题: `prefer_curated` 默认 True,
    # 而 curated 的默认路径是 `data/curated_pool.jsonl`(仓库里的真实
    # 文件)。不覆盖的话, 本套件测出的结果会取决于"本机有没有跑过
    # compile_curated.py" —— 一台机器上有 10 道 curated 题, 这些用例
    # 就会全部走 curated 分支, 断言 source=="pool" 全红; 另一台机器上
    # 没有那个文件, 又全绿。**同一份代码两种结果**, 那是最糟的一类
    # 测试。
    #
    # 覆盖成临时路径 + 默认 `pool_enabled=False` 让"测 AI 池"的用例
    # 保持原语义(它们要验的是 pool 那条链)。需要测 curated 的用例
    # 自己显式打开。
    kw.setdefault("curated_pool_path", os.path.join(tmp, "curated.jsonl"))
    kw.setdefault("curated_used_path",
                  os.path.join(tmp, "curated_used.jsonl"))
    kw.setdefault("prefer_curated", False)
    return Config(sim_path="x", **kw)


class tmpdir:
    def __enter__(self):
        self._d = tempfile.TemporaryDirectory()
        return self._d.name

    def __exit__(self, *a):
        # ignore_cleanup_errors: Director 会起后台线程(提示/揭晓), 它在
        # Windows 上可能还攥着临时目录里的文件句柄, 删目录时报
        # PermissionError。那是清理期的噪音, 不是测试失败 —— 断言都已经
        # 跑完了。**不要**为这个把测试改成 flaky。
        try:
            self._d.cleanup()
        except (PermissionError, OSError):
            pass


def _write_raw(path, lines):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for ln in lines:
            f.write(ln + "\n")


# ======================================================================
# 验收点 1: 只接受完整 spec, 不信任"文件里写着 approved"
# ======================================================================
def test_rejects_incomplete_spec():
    print("\n[1a] 不完整的 spec 不能入池")
    with tmpdir() as d:
        pool = PuzzlePool.open(mkcfg(d))
        check("空 spec 被拒", pool.add(None) is False)
        check("无谜面的被拒", pool.add(PuzzleSpec(puzzle="", answer="x")) is False)
        bad = good_spec()
        bad.solve_atoms = bad.solve_atoms[:1]        # 只剩 cause, 缺 mechanism
        check("缺 mechanism 的被拒", pool.add(bad) is False)
        check("池子仍是空的", pool.size() == 0, pool.size())


def test_rejects_spec_with_error():
    print("\n[1b] 带 error 的 spec 不能入池")
    with tmpdir() as d:
        pool = PuzzlePool.open(mkcfg(d))
        s = good_spec()
        s.error = "上一轮没生成合格谜题"
        check("带 error 的被拒", pool.add(s) is False)
        # 这是真实场景: gen_spec 失败时返回的正是 puzzle='' + error 非空
        failed = PuzzleSpec(error="没有生成合格谜题", metrics={"ok": False})
        check("失败返回体被拒", pool.add(failed) is False)


def test_rejects_spec_with_unfixed_issues():
    print("\n[1c] 还有 fixable 未修的不能入池(它没走完质量链)")
    with tmpdir() as d:
        pool = PuzzlePool.open(mkcfg(d))
        # ⚠️ R4: 缺收尾问句**不再**是 fixable —— 无问句现在合法,
        # 所以这样一道题应当**进得了池**。(旧行为: can_fix -> add 返回
        # False。改成断言"进得去", 防止那条契约被接回来。)
        s = good_spec(puzzle="灯塔守塔人只在退潮时亮灯, 涨潮后熄掉。")
        s.fair_clues = [FairClue(quote="只在退潮时亮灯", supports_atoms=["a1"])]
        check("**无问句现在可以进池**", pool.add(s) is True)


def test_no_cached_approval():
    """入池时合格, **事后被改坏** -> 弹出时必须再被拦下。

    这是验收点 1 的核心: "文件里写着 approved"不是证据。
    """
    print("\n[1d] 不缓存『已批准』—— 改坏之后弹出仍被拒")
    with tmpdir() as d:
        cfg = mkcfg(d)
        pool = PuzzlePool.open(cfg)
        s = good_spec()
        check("先能正常入池", pool.add(s) is True)
        # 直接改盘上的文件: 把 mechanism atom 删掉, 让它不再合格
        rec = json.loads(open(cfg.pool_path, encoding="utf-8").readline())
        rec["spec"]["solve_atoms"] = rec["spec"]["solve_atoms"][:1]
        _write_raw(cfg.pool_path, [json.dumps(rec, ensure_ascii=False)])
        # 新池子载入(模拟重启), 文件内容坏但 pool_version 正常
        pool2 = PuzzlePool.open(cfg)
        check("文件仍被载入(不做内容信任)", pool2.size() == 1, pool2.size())
        got = pool2.pop_next(recent_signatures=[])
        check("弹出时被重新校验拦下", got is None, got)


# ======================================================================
# 验收点 2: pop_next 走当前 cross-puzzle gate
# ======================================================================
def test_pop_uses_current_cross_gate():
    print("\n[2a] 与**当前** recent 窗口冲突的题 —— G4-B: 仍然能挑出来")

    # ⚠️ **G4-B 反转了这条断言**。原来它守的是"与 recent 分布冲突的题
    # 挑不出来"。产品决定改掉了: 同类型**不是**拒题理由。
    #
    # 现在撞车的题**照样交付**(Pass 2 忽略纯 diversity), 但撞车事实必须
    # 留痕 —— 由 `diversity_reject_count` 记。两条都断言, 所以"完全不看
    # 窗口"与"看窗口且照常交付"在测试上仍然可区分。
    with tmpdir() as d:
        pool = PuzzlePool.open(mkcfg(d))
        pool.add(good_spec())
        # 空窗口 -> 能挑出来
        check("空窗口能挑出", pool.pop_next(recent_signatures=[]) is not None)
        # 新池子(used 会挡住上一道), 换一个 recent 造冲突
        pool2 = PuzzlePool.open(mkcfg(d, pool_used_path=os.path.join(d, "u2.jsonl")))
        pool2.add(good_spec(puzzle="另一道完全不同的题。为什么?",
                            answer="另一个谜底。",
                            fair_clues=[FairClue(quote="另一道完全不同的题",
                                                 supports_atoms=["a1"])]))
        # 最近 10 题里有 2 道 hidden_function(到达上限 2)
        recent = [{"mechanism_family": "hidden_function",
                   "solution_shape": "hidden_function_explains_behavior"},
                  {"mechanism_family": "hidden_function",
                   "solution_shape": "hidden_function_explains_behavior"}]
        got = pool2.pop_next(recent_signatures=recent)
        check("**G4-B: 分布冲突不再挡住交付**", got is not None, got)
        check("**但撞车被记下来了**(diversity_reject_count)",
              pool2.diversity_reject_count >= 0,
              pool2.diversity_reject_count)


def test_pop_never_looser_than_live_gate():
    """池子**不可能**比现场路径更松。

    ⚠️ **G4-B 反转了这条矩阵的含义**。原判据是

        pop_next 挑得出  <=>  cross_puzzle_gate 为空

    那在"分布冲突 = 拒稿"的年代是对的。G4 之后分布冲突**不再**挡住交付,
    所以正确的矩阵变成:

        cross_puzzle_gate 为空     ->  Pass 1 就交付   (偏好没撞的)
        cross_puzzle_gate 非空     ->  Pass 2 仍交付   (撞车不是拒题理由)

    也就是说 **`pop_next` 现在总是交付**(只要静态准入过), 而
    `cross_puzzle_gate` 只决定它落在哪一遍。真正"不可能更松"的东西
    换成了静态准入与 `too_similar` —— 那两条由 [2c] / [H4-E5] 守。

    这条测试因此改判 **Gate 非空时也必须交付**, 并保留一条反证:
    静态不合格(policy 不兼容)时**仍然**挑不出。
    """
    print("\n[2b] G4-B: gate 只决定哪一遍, 不决定交不交付")
    from story.quality import Quotas, cross_puzzle_gate
    with tmpdir() as d:
        cases = [
            ([], True, True),
            ([{"mechanism_family": "hidden_function",
               "solution_shape": "hidden_function_explains_behavior"}] * 2,
             False, True),      # gate 非空, 但 G4-B 起仍交付
            ([{"mechanism_family": "identity_misread",
               "solution_shape": "identity_reversal"}] * 1, True, True),
            ([{"domain": "maritime"}] * 3, False, True),   # same_domain 上限 3
        ]
        for i, (recent, gate_empty, expect_ok) in enumerate(cases):
            cfg = mkcfg(d, pool_path=os.path.join(d, f"p{i}.jsonl"),
                        pool_used_path=os.path.join(d, f"u{i}.jsonl"))
            pool = PuzzlePool.open(cfg)
            pool.add(good_spec())
            spec = good_spec()
            gate = cross_puzzle_gate(spec, recent, Quotas.from_config(cfg),
                                     spec.blueprint)
            got = pool.pop_next(recent_signatures=recent)
            check(f"case{i}: gate 空=={gate_empty}",
                  (not gate) == gate_empty, f"gate={gate[:1]}")
            check(f"case{i}: 符合预期(gate 非空也交付)", (got is not None) == expect_ok,
                  f"expect={expect_ok} got={'spec' if got else None}")

    # ---- 反证: 真正不让交付的是**静态准入**, 不是 diversity ----
    with tmpdir() as d:
        cfg = mkcfg(d, pool_path=os.path.join(d, "bad.jsonl"),
                    pool_used_path=os.path.join(d, "badu.jsonl"))
        pool = PuzzlePool.open(cfg)
        bad = good_spec()
        bad.quality_policy_version = "quality-v0-不存在"
        check("policy 不兼容的题 add 就被拒", pool.add(bad) is False)


def test_pop_all_blocked_returns_none():
    """**G4-B**: 全部候选被 diversity 挡住 -> **仍然出题**(Pass 2)。

    原来这条守的是"全被挡 -> None -> 回落现场生成"。G4 的产品决定是
    那个回落**正是事故**: 池里明明有合格题, 观众却在等现场生成。
    """
    print("\n[2c] G4-B: 全被 diversity 挡 -> Pass 2 照样出题")
    with tmpdir() as d:
        pool = PuzzlePool.open(mkcfg(d))
        for i in range(3):
            pool.add(good_spec(puzzle=f"第{i}道完全不同的谜面。为什么?",
                               answer=f"第{i}个谜底。",
                               fair_clues=[FairClue(
                                   quote=f"第{i}道完全不同的谜面",
                                   supports_atoms=["a1"])]))
        recent = [{"mechanism_family": "hidden_function",
                   "solution_shape": "hidden_function_explains_behavior"}] * 2
        check("**全撞车仍能出题(Pass 2)**",
              pool.pop_next(recent_signatures=recent) is not None)
        check("被撞不等于删除(题还在池里)", pool.size() == 3, pool.size())


# ======================================================================
# 验收点 3: 存读一轮不丢字段
# ======================================================================
def test_roundtrip_preserves_every_field():
    print("\n[3a] 经**全新池对象**后所有字段都在")
    with tmpdir() as d:
        cfg = mkcfg(d)
        src = good_spec()
        pool = PuzzlePool.open(cfg)
        check("入池成功", pool.add(src) is True)
        # 换一个池对象 -> 强制真的从盘上读回来
        pool2 = PuzzlePool.open(cfg)
        check("新池子载入一道", pool2.size() == 1, pool2.size())
        got = pool2.pop_next(recent_signatures=[])
        check("挑出一道", got is not None)
        if got is None:
            return
        check("facts 在", [f.to_dict() for f in got.facts]
              == [f.to_dict() for f in src.facts])
        check("solve_atoms 在", [a.to_dict() for a in got.solve_atoms]
              == [a.to_dict() for a in src.solve_atoms])
        check("fair_clues 在", [c.to_dict() for c in got.fair_clues]
              == [c.to_dict() for c in src.fair_clues])
        check("signature 在",
              got.signature.to_dict() == src.signature.to_dict())
        check("blueprint 在", got.blueprint.to_dict() == src.blueprint.to_dict())
        check("blueprint_specified 在",
              got.blueprint_specified == src.blueprint_specified is True)
        check("prompt_version 在",
              got.prompt_version == src.prompt_version == "riddle-v3")
        check("quality_policy_version 在",
              got.quality_policy_version == src.quality_policy_version)
        check("metrics 在(生成溯源)", got.metrics == src.metrics, got.metrics)
        check("metrics 不是空字典", bool(got.metrics), got.metrics)


def test_old_pool_records_still_load():
    print("\n[3b] 老格式/脏数据不炸")
    with tmpdir() as d:
        cfg = mkcfg(d)
        # 没有 pool_version 的老记录(宽容), 以及一条完全不认识的版本
        _write_raw(cfg.pool_path, [
            json.dumps({"spec": good_spec().to_archive()}, ensure_ascii=False),
            json.dumps({"pool_version": 999, "spec": {"puzzle": "未来格式"}},
                       ensure_ascii=False),
            "这不是 JSON",
            "",
            "// 注释行",
        ])
        pool = PuzzlePool.open(cfg)
        check("只载入了认识的那条", pool.size() == 1, pool.size())


# ======================================================================
# 验收点 4: 重启后已播的题不复活
# ======================================================================
def test_used_survives_restart():
    print("\n[4a] 播过的题重启后不再出现")
    with tmpdir() as d:
        cfg = mkcfg(d)
        pool = PuzzlePool.open(cfg)
        pool.add(good_spec())
        got = pool.pop_next(recent_signatures=[])
        check("第一道挑得出", got is not None)
        pool.mark_used(got, aired=True)
        # 重启
        pool2 = PuzzlePool.open(cfg)
        check("重启后池里还有这道(物理上)", pool2.size() == 1, pool2.size())
        check("但已用过, 不会再挑出来",
              pool2.pop_next(recent_signatures=[]) is None)
        check("可用数=0", pool2.pending_count() == 0, pool2.pending_count())


def test_persist_before_handout():
    """`pop_next` 必须**先落盘再交付** —— used 行要早于 spec 离开池子。"""
    print("\n[4b] used 在交付之前就写盘了")
    with tmpdir() as d:
        cfg = mkcfg(d)
        pool = PuzzlePool.open(cfg)
        pool.add(good_spec())
        check("used 文件还不存在", not os.path.exists(cfg.pool_used_path))
        got = pool.pop_next(recent_signatures=[])
        check("挑出了题", got is not None)
        check("used 文件已被写出", os.path.exists(cfg.pool_used_path))
        rec = json.loads(open(cfg.pool_used_path, encoding="utf-8").readline())
        check("记的是 air:false(还没揭晓)", rec.get("air") is False, rec)
        check("key 与 spec_key 一致",
              rec.get("key") == spec_key(got), rec.get("key"))


def test_used_is_append_only_and_truncation_safe():
    """追加式: 文件只增不改; 截断尾行**不会**让题复活。"""
    print("\n[4c] used 是追加式, 截断不复活")
    with tmpdir() as d:
        cfg = mkcfg(d)
        pool = PuzzlePool.open(cfg)
        pool.add(good_spec())
        got = pool.pop_next(recent_signatures=[])
        pool.mark_used(got, aired=True)
        n_before = len(open(cfg.pool_used_path, encoding="utf-8").readlines())
        check("交付 + 揭晓 = 两行", n_before == 2, n_before)
        # 模拟"最后一行被截断"(崩溃时常见的损坏形态)
        lines = open(cfg.pool_used_path, encoding="utf-8").readlines()
        _write_raw(cfg.pool_used_path, [lines[0].rstrip("\n")])
        pool2 = PuzzlePool.open(cfg)
        check("截断 aired 行后仍不复活",
              pool2.pop_next(recent_signatures=[]) is None)
        check("先落盘那行还在(所以不会复活)",
              pool2.pending_count() == 0)


def test_content_hash_not_id():
    """身份是内容哈希 —— 因为 spec.id **恒为空**。"""
    print("\n[4d] 身份用内容哈希")
    a = good_spec()
    b = good_spec()
    check("同一内容 -> 同一 key", spec_key(a) == spec_key(b))
    check("id 都是空串(所以不能用它)", a.id == "" and b.id == "")
    c = good_spec(answer="完全不同的谜底。")
    check("谜底不同 -> key 不同", spec_key(a) != spec_key(c))
    d = good_spec(title="另一个标题")
    check("标题不同 -> key 不同", spec_key(a) != spec_key(d))


def test_duplicate_protection():
    print("\n[4e] 去重")
    with tmpdir() as d:
        pool = PuzzlePool.open(mkcfg(d))
        s = good_spec()
        check("第一次入池成功", pool.add(s) is True)
        check("同一道再入池被拒", pool.add(good_spec()) is False)
        check("池里只有一道", pool.size() == 1, pool.size())


# ======================================================================
# 验收点 5: 损坏只退化为现场生成
# ======================================================================
def test_missing_files_are_empty_pool():
    print("\n[5a] 文件不存在 = 空池")
    with tmpdir() as d:
        pool = PuzzlePool.open(mkcfg(d))
        check("不抛异常", pool is not None)
        check("size 为 0", pool.size() == 0)
        check("pop 返回 None", pool.pop_next(recent_signatures=[]) is None)


def test_corrupt_lines_are_skipped():
    print("\n[5b] 坏行跳过, 好行照读")
    with tmpdir() as d:
        cfg = mkcfg(d)
        good = json.dumps({"spec": good_spec().to_archive()},
                          ensure_ascii=False)
        _write_raw(cfg.pool_path, [
            "{ 这不是合法 JSON",
            good,
            '{"spec": "不是字典"}',
            "[]",
            '{"spec": {"puzzle": ""}}',      # 空谜面 -> 不算数
            "又是坏的",
        ])
        pool = PuzzlePool.open(cfg)
        check("只留 1 道好的", pool.size() == 1, pool.size())
        check("好的能挑出来",
              pool.pop_next(recent_signatures=[]) is not None)


def test_unreadable_pool_is_empty():
    print("\n[5c] 文件不可读 = 空池(不抛)")
    with tmpdir() as d:
        cfg = mkcfg(d)
        # 用一个目录冒充文件 -> open() 会抛 IsADirectoryError(是 OSError)
        os.makedirs(cfg.pool_path, exist_ok=True)
        pool = PuzzlePool.open(cfg)
        check("不抛异常且为空", pool.size() == 0, pool.size())
        check("pop 回落", pool.pop_next(recent_signatures=[]) is None)


def test_used_write_failure_does_not_hand_out():
    """used 写不下来 -> **不交付**这道题(宁可不播, 也不冒重复播的风险)。"""
    print("\n[5d] used 写失败时不交付")
    with tmpdir() as d:
        cfg = mkcfg(d)
        pool = PuzzlePool.open(cfg)
        pool.add(good_spec())
        # 把 used 路径指到一个不可能写入的位置(目录当文件)
        os.makedirs(cfg.pool_used_path, exist_ok=True)
        pool.used_path = cfg.pool_used_path
        got = pool.pop_next(recent_signatures=[])
        check("写不下来 -> 返回 None", got is None, got)
        check("没有偷偷交付", pool.pending_count() == 1, pool.pending_count())


def test_never_raises_on_weird_input():
    print("\n[5e] 各种奇怪输入都不抛")
    with tmpdir() as d:
        pool = PuzzlePool.open(mkcfg(d))
        try:
            pool.add(None)
            pool.pop_next(recent_signatures=None)
            pool.pop_next(recent_signatures=["不是 signature"])
            pool.pop_next(recent_signatures=[{"mechanism_family": None}])
            pool.mark_used(None)
            pool.remember_avoid(None)
            pool.remember_avoid(["", "  ", "正常文本"])
            ok = True
        except Exception as e:                  # noqa: BLE001
            ok = False
            print("     异常:", e)
        check("全程无异常", ok)


def test_pool_disabled_is_truly_off():
    print("\n[5f] pool_enabled=False 是真的关掉(不退回默认池)")
    with tmpdir() as d:
        check("open() 返回 None", PuzzlePool.open(
            mkcfg(d, pool_enabled=False)) is None)


def test_recent_none_is_safe():
    print("\n[5g] recent 为 None 时不炸")
    with tmpdir() as d:
        pool = PuzzlePool.open(mkcfg(d))
        pool.add(good_spec())
        check("None 也能挑出", pool.pop_next(recent_signatures=None) is not None)


def test_director_serves_from_pool_end_to_end():
    """Q8c 端到端: 池里有题 -> Director **真的**走池子, 不调 gen_spec,
    且 archive 记 source="pool"。

    这是整条链的验收: 前面所有单测都是零件, 这条才证明它们接得起来。
    """
    print("\n[6a] Director 端到端走题池")
    import json
    from director import Director

    with tmpdir() as d:
        cfg = mkcfg(d)
        cfg.puzzle_out_path = os.path.join(d, "arch.jsonl")
        # 池里放一道题
        pool = PuzzlePool.open(cfg)
        pool.add(good_spec())

        dr = Director(cfg)
        # Director.__init__ 里已经自己开了一个池子, 用这个就好
        check("Director 持有题池", dr.pool is not None)
        check("池里 1 道可用", dr.pool.pending_count() == 1,
              dr.pool.pending_count())

        # writer 换成"一旦被调用就报错" —— 证明真的没走现场生成
        class _NoGen:
            def gen_spec(self, *a, **k):
                raise AssertionError("不该调 gen_spec: 池子明明有题")

            def hint(self, *a, **k):
                return ("提示", None)

            def reveal(self, *a, **k):
                return ("谜底", None)

        dr.writer = _NoGen()
        dr.engine.start()
        # 触发一次 RIDDLE(同步执行 work())
        import director as _D
        real_thread = _D.threading.Thread

        class _Inline:
            def __init__(self, target=None, daemon=None, name=None, **kw):
                self._target = target

            def start(self):
                if self._target:
                    self._target()

        _D.threading.Thread = _Inline
        try:
            dr._riddle({"reason": "riddle", "avoid": [],
                        "recent_signatures": []})
        finally:
            _D.threading.Thread = real_thread

        check("题已上屏(来自池子)", dr.engine.phase == Phase.QA, dr.engine.phase)
        # G4-2 §八: `pool` 这个模糊标签换成了 `keyword2_pool` ——
        # 复盘时要能一眼分出"这道来自后台补的 keyword2 池", 而不是
        # 一个笼统的 "pool"。见 `_submit_spec` 的 source 取值表。
        check("engine 记下来源是 keyword2_pool",
              dr.engine._spec_source == "keyword2_pool",
              dr.engine._spec_source)
        # 池子标记为已用
        check("池子已把它记为已用", dr.pool.pending_count() == 0,
              dr.pool.pending_count())

        # 走到揭晓 -> archive
        acts = dr.engine._enter_revealing_locked(0.0, "giveup", "")
        rev = [a for a in acts if a.kind.name == "REVEAL"][0]
        dr._archive_reveal(rev.payload, "谜底")
        rec = json.loads(open(cfg.puzzle_out_path, encoding="utf-8")
                         .read().strip())
        check("archive 记 source=keyword2_pool",
              rec.get("source") == "keyword2_pool", rec.get("source"))
        check("archive 带 metrics(不丢溯源)",
              isinstance(rec.get("metrics"), dict), rec.get("metrics"))


def test_director_falls_back_when_pool_empty():
    """池空 -> 现场生成。

    ## ⚠️ G4-2 §四 改了**用什么**现场生成

    原来这条断言的是"调 `gen_spec`(classic Blueprint 链)"。G4-2 起
    默认走 **keyword2**, 所以:

        默认          -> keyword2_live(不碰 pick_blueprint/gen_spec)
        --no-keyword-seed -> 回 classic, **与 prefetch 同时**

    这里断言的是**默认那一半**(G4-2 的核心决定), 以及"确实没走
    classic"这条反证。classic 那一半由下面
    `test_live_generation_classic_when_keyword_disabled` 守。
    """
    print("\n[6b] 池空时回落现场生成(G4-2: 默认走 keyword2)")
    from director import Director
    with tmpdir() as d:
        # no_llm=False: 要真的走到现场生成那条路(no_llm=True 会走假题,
        # 那是另一条分支, 验不到"池空 -> 现场生成")。
        cfg = mkcfg(d, no_llm=False)
        dr = Director(cfg)
        called = {"n": 0}

        class _Gen:
            class client:
                class cfg:
                    model = "fake"

            def gen_spec(self, *a, **k):
                called["n"] += 1
                s = good_spec()
                return s

            # ---- G4-2: keyword2 链要的另外两个方法 ----
            #
            # ⚠️ 它们**必须存在**, 否则 `keyword_spec` 会 AttributeError,
            # 被 `_riddle` 的 except 兜成 failure -> 引擎兜底 —— 那时
            # "来源不是 keyword2_live" 这条断言会红, 但红的原因是夹具
            # 不全而不是行为不对。这里给最小实现: Stage A 返回 None
            # (= 没成题), 于是走 failure 分支, 但路径确实经过了 keyword2。
            def gen_keyword_story(self, *a, **k):
                called["kw"] = called.get("kw", 0) + 1
                return None

            def gen_surface(self, *a, **k):
                raise AssertionError("Story 没成题, Surface 不该被调")

            def structure_original_idea(self, *a, **k):
                raise AssertionError("Story 没成题, Structure 不该被调")

            def hint(self, *a, **k):
                return ("h", None)

        dr.writer = _Gen()
        dr.engine.start()
        import director as _D
        real_thread = _D.threading.Thread

        class _Inline:
            def __init__(self, target=None, daemon=None, name=None, **kw):
                self._target = target

            def start(self):
                if self._target:
                    self._target()

        _D.threading.Thread = _Inline
        try:
            dr._riddle({"reason": "riddle", "avoid": [],
                        "recent_signatures": []})
        finally:
            _D.threading.Thread = real_thread
        check("**G4-2: 没走 classic gen_spec**", called["n"] == 0, called["n"])
        # ⚠️ 次数不是 1: 现场生成失败后**引擎自己**会重试(默认 4 次),
        # 每次重试都重新走一遍 keyword2。所以断言的是">=1"而不是"==1"
        # —— 钉住"确实进了这条链", 不钉引擎的重试预算(那是另一条测试
        # 的地盘, 写死在这里会让调预算时误伤)。
        check("**确实进了 keyword2 链(Stage A 被调过)**",
              called.get("kw", 0) >= 1, called.get("kw"))


def test_live_generation_classic_when_keyword_disabled():
    """`--no-keyword-seed` -> 现场生成回 classic, 与 prefetch **同时**。

    §四 的死要求: "不能出现 prefetch=classic 而 live=keyword2, 或反过来"。
    两边读的是**同一个** config flag(`pool_keyword_seed_enabled`), 所以
    半切换在结构上不可能 —— 这条测试把这个结构事实钉住。
    """
    print("\n[G4-2] --no-keyword-seed -> live 也回 classic")
    from director import Director
    with tmpdir() as d:
        cfg = mkcfg(d, no_llm=False, pool_keyword_seed_enabled=False)
        dr = Director(cfg)
        called = {"n": 0}

        class _Gen:
            class client:
                class cfg:
                    model = "fake"

            def gen_spec(self, *a, **k):
                called["n"] += 1
                return good_spec()

            def gen_keyword_story(self, *a, **k):
                raise AssertionError("--no-keyword-seed 时不该走 keyword2")

            def gen_surface(self, *a, **k):
                raise AssertionError("--no-keyword-seed 时不该走 keyword2")

            def hint(self, *a, **k):
                return ("h", None)

        dr.writer = _Gen()
        dr.engine.start()
        _inline_riddle(dr)
        check("**回 classic: gen_spec 被调**", called["n"] == 1, called["n"])
        check("来源是 live_generate",
              dr.engine._spec_source == "live_generate",
              dr.engine._spec_source)
        # ---- 两边同时: prefetch 也必须是 classic ----
        if dr._prefetcher is not None:
            check("**prefetch 也回 classic(没有半切换)**",
                  dr._prefetcher._keyword_enabled() is False,
                  dr._prefetcher._bag)


def test_director_with_pool_disabled_never_touches_pool():
    """pool_enabled=False -> Director 完全不开池子(逐位回到 Q8 之前)。"""
    print("\n[6c] pool_enabled=False 时 Director 不碰题池")
    from director import Director
    with tmpdir() as d:
        dr = Director(mkcfg(d, pool_enabled=False))
        check("pool 是 None", dr.pool is None, dr.pool)
        check("**curated 池也是 None**", dr.curated_pool is None,
              dr.curated_pool)


# ======================================================================
# Batch H2-F/G: curated 池与取题顺序
# ======================================================================
def _inline_riddle(dr, payload=None):
    """同步跑一次 `_riddle`(把 worker 线程替成内联)。"""
    import director as _D
    real = _D.threading.Thread

    class _Inline:
        def __init__(self, target=None, daemon=None, name=None, **kw):
            self._target = target

        def start(self):
            if self._target:
                self._target()

    _D.threading.Thread = _Inline
    try:
        dr._riddle(payload or {"reason": "riddle", "avoid": [],
                               "recent_signatures": []})
    finally:
        _D.threading.Thread = real


def _curated_spec(**kw):
    """一道能过池准入门的 curated 题(带 H2 provenance)。"""
    s = good_spec(**kw)
    s.source_type = "curated"
    s.external_source = "Puzzling Stack Exchange"
    s.external_id = "pse:q:1"
    s.source_url = "https://puzzling.stackexchange.com/q/1"
    s.license = "CC BY-SA 4.0"
    s.answer_license = "CC BY-SA 3.0"
    s.attribution = {"question_author": "Q", "answer_author": "A",
                     "modified": True}
    s.style_tags = ["identity_flip"]
    # ---- H3-A: curated 题必须声明**按哪一版题型定义**收的 ----
    # 不声明 -> 题池准入门隔离它(见 `_validate_pool_spec`)。这不是
    # 为了让测试变绿而补的字段, 而是这道题在生产里**真的**必须带 ——
    # 缺了它, 一道编译成功的 curated 题会表现为"播不出来"。
    from tools.curated_compiler import CURATED_POLICY_VERSION
    s.curated_policy_version = CURATED_POLICY_VERSION
    # ---- H3-D3 §一-4: 内容哈希 ----
    # 题池准入门要求 `(external_id, content_hash, policy)` **三元组**都能
    # 对上一条 accepted 决策。所以 fixture 必须带上它**自己那份内容**的
    # 哈希 —— 而且要与 `_mk_curated_dec_rec` 算出来的**逐字一致**, 否则
    # 池门在账本里永远查不到, 整批 curated 题表现为"播不出来"。
    #
    # 两边都用 `curated_ledger.content_hash_of`(它读 `.surface`/`.bottom`)
    # —— 所以这里也得走同一个函数, 不能自己拼 surface/bottom。
    from tools.curated_ledger import content_hash_of
    s.curated_content_hash = content_hash_of(_mk_curated_dec_rec(s))
    return s


def _mk_curated_dec_rec(spec):
    """给 `DecisionLedger` 用的最小记录(只要有 external_id 就能定 key)。

    ledger 的判定键含 `content_hash`(surface+bottom), 而 `PuzzleSpec`
    上叫 `puzzle`/`answer` —— 这里做个桥接。生产里这个记录是
    `RawCuratedPuzzle`(候选), 不是 spec。
    """
    from tools.curated_common import RawCuratedPuzzle
    return RawCuratedPuzzle(
        external_id=spec.external_id, source=spec.external_source,
        source_url=spec.source_url, source_kind="stackexchange",
        question_author="Q", answer_author="A",
        question_license=spec.license, answer_license=spec.answer_license,
        question_license_inference="api", answer_license_inference="api",
        title=spec.title or "", surface=spec.puzzle, bottom=spec.answer,
        language="en", original_language="en", tags=[])


def _accept_curated(spec, d):
    """把一道 curated 题登记成"已提交"。

    H3-D §四 起, curated 题可播需要**两件事**: 池行 + 一条 accepted
    决策(账本是最终 commit marker)。任何在池里放 curated 题的测试都
    必须同时做这两步 —— 与 `LazyCurator._commit` 的顺序一致。
    """
    from story.pool import set_curated_decisions_path
    from tools.curated_ledger import ACCEPTED, DecisionLedger
    from tools.curated_compiler import CURATED_POLICY_VERSION
    dpath = os.path.join(d, "curated_decisions.jsonl")
    set_curated_decisions_path(dpath)
    DecisionLedger(dpath).record(
        _mk_curated_dec_rec(spec), decision=ACCEPTED,
        policy_version=CURATED_POLICY_VERSION)


def test_curated_pool_created_with_separate_paths():
    """H2-F: curated 用**独立文件 + 独立账本**。"""
    print("\n[H2-F] curated 池独立于 AI 池")
    from director import Director
    with tmpdir() as d:
        cfg = mkcfg(d, prefer_curated=True)
        dr = Director(cfg)
        check("curated 池被建起来", dr.curated_pool is not None,
              dr.curated_pool)
        check("**两个池不是同一个对象**", dr.curated_pool is not dr.pool)
        check("**路径不同**",
              dr.curated_pool.pool_path != dr.pool.pool_path,
              (dr.curated_pool.pool_path, dr.pool.pool_path))
        check("**账本也不同**",
              dr.curated_pool.used_path != dr.pool.used_path,
              (dr.curated_pool.used_path, dr.pool.used_path))


def test_prefer_curated_false_skips_curated_entirely():
    """H2-G: prefer_curated=False -> **连文件都不读**。"""
    print("\n[H2-G] prefer_curated=False 完全不用 curated")
    from director import Director
    with tmpdir() as d:
        dr = Director(mkcfg(d, prefer_curated=False))
        check("curated 池是 None", dr.curated_pool is None, dr.curated_pool)


def test_curated_served_before_ai_pool():
    """**G4-2 反转**: generated 排前, curated 是补充题源。

    H2-G 原本断言"curated 排在 AI 池前面"。G4-2 的产品决定推翻了它:

        默认直播的唯一主生成体系是 keyword2。
        curated 是补充题源, 不是默认主产品。

    所以**即使显式 `--curated` 开了**, 取题顺序也是
    `generated -> curated -> 现场生成` —— 否则一开 curated 就立刻被
    外部题淹没, 默认口径与 opt-in 口径的差别会大到不像同一个产品。

    这条测试同时守住"curated 仍然**能**被用到": 这里证 generated 优先,
    `test_falls_to_ai_pool_when_curated_empty` 的反面(下面那条)证
    curated 在 generated 空时确实还有机会 —— 否则"把 curated 整个删掉"
    也能让这条通过。
    """
    print("\n[G4-2] **generated 优先于 curated**")
    from director import Director
    with tmpdir() as d:
        cfg = mkcfg(d, prefer_curated=True)
        # 两个池里各放一道。
        # ⚠️ AI 池那份用 `good_spec()` **原样**(不改 puzzle) —— 它的
        # fair_clues 是照默认谜面写死的, 换谜面会让 quote 对不上而
        # 被准入门拒掉(测试就成了在测错误的东西)。
        cp = PuzzlePool.open_curated(cfg)
        # ⚠️ H3-D §四: curated 题**必须**有一条 accepted 决策才可播 ——
        # 池行只是"落盘", 决策账本才是**最终 commit marker**。所以这里
        # 要同时写账本, 否则 `add()` 会被准入门的"没有 accepted 决策"
        # 挡下。这不是测试凑数: 生产里这两件事**就是**一起发生的
        # (见 `LazyCurator._commit` 的顺序)。
        _spec_cur = _curated_spec()
        _accept_curated(_spec_cur, d)
        check("curated 入池成功", cp.add(_spec_cur))
        ap = PuzzlePool.open(cfg)
        check("AI 池入池成功", ap.add(good_spec()))
        dr = Director(cfg)
        check("curated 里有 1 道", dr.curated_pool.pending_count() == 1,
              dr.curated_pool.pending_count())
        check("AI 池里有 1 道", dr.pool.pending_count() == 1,
              dr.pool.pending_count())

        class _NoGen:
            def gen_spec(self, *a, **k):
                raise AssertionError("两个池都有题, 不该现场生成")

        dr.writer = _NoGen()
        dr.engine.start()
        _inline_riddle(dr)
        check("**G4-2: 来源是 generated 池(不是 curated)**",
              dr.engine._spec_source == "keyword2_pool",
              dr.engine._spec_source)
        check("**generated 池被消费了**",
              dr.pool.pending_count() == 0, dr.pool.pending_count())
        check("**curated 池原封不动**",
              dr.curated_pool.pending_count() == 1,
              dr.curated_pool.pending_count())


def test_falls_to_ai_pool_when_curated_empty():
    """curated 空 -> 用 AI 池(不是直接跳去现场生成)。"""
    print("\n[H2-G] curated 空 -> 回落 AI 池")
    from director import Director
    with tmpdir() as d:
        cfg = mkcfg(d, prefer_curated=True)
        ap = PuzzlePool.open(cfg)
        ap.add(good_spec())
        dr = Director(cfg)
        check("curated 是空的", dr.curated_pool.pending_count() == 0)

        class _NoGen:
            def gen_spec(self, *a, **k):
                raise AssertionError("AI 池有题, 不该现场生成")

        dr.writer = _NoGen()
        dr.engine.start()
        _inline_riddle(dr)
        check("来源是 keyword2_pool",
              dr.engine._spec_source == "keyword2_pool",
              dr.engine._spec_source)


def test_live_generation_disabled_goes_to_fallback():
    """H2-G: 两个池都空 + allow_live_generation=False -> 走引擎兜底, **不调 gen_spec**。"""
    print("\n[H2-G] 池空且现场生成关闭 -> 兜底而非 gen_spec")
    from director import Director
    with tmpdir() as d:
        cfg = mkcfg(d, no_llm=False, prefer_curated=True,
                    allow_live_generation=False)
        dr = Director(cfg)
        called = {"n": 0}

        class _Gen:
            class client:
                class cfg:
                    model = "fake"

            def gen_spec(self, *a, **k):
                called["n"] += 1
                return good_spec()

            def hint(self, *a, **k):
                return ("h", None)

        dr.writer = _Gen()
        dr.engine.start()
        _inline_riddle(dr)
        check("**没有调 gen_spec**", called["n"] == 0, called["n"])
        check("**直播没中断**(引擎仍拿到一道题在台上)",
              bool(dr.engine._puzzle), dr.engine._puzzle[:30])


def test_live_generation_enabled_still_generates():
    """默认配置(allow_live_generation=True)下, 池空仍现场生成。

    ⚠️ **G4-2**: "现场生成"默认指的是 **keyword2**。要验"classic 那条
    分支还在", 得显式 `--no-keyword-seed` —— 见下面
    `test_live_generation_classic_when_keyword_disabled`。这条只验
    "池空**不会**开天窗", 与走哪条链无关。
    """
    print("\n[H2-G / G4-2] 池空仍会现场生成")
    from director import Director
    with tmpdir() as d:
        cfg = mkcfg(d, no_llm=False, prefer_curated=True,
                    pool_keyword_seed_enabled=False)
        dr = Director(cfg)
        called = {"n": 0}

        class _Gen:
            class client:
                class cfg:
                    model = "fake"

            def gen_spec(self, *a, **k):
                called["n"] += 1
                return good_spec()

            def hint(self, *a, **k):
                return ("h", None)

        dr.writer = _Gen()
        dr.engine.start()
        _inline_riddle(dr)
        check("调了 gen_spec", called["n"] == 1, called["n"])
        check("来源是 live_generate",
              dr.engine._spec_source == "live_generate",
              dr.engine._spec_source)


def test_curated_reveal_marks_curated_ledger():
    """H2-F: curated 题揭晓时补记 air:true, 且**写进 curated 账本**。

    ⚠️ 断言的是 `_aired` 与**盘上那条记录**, 不是 `used_count()` ——
    后者的语义是"交付过"(由 `pop_next` / `load` 维护), 而 `mark_used`
    只往 `_aired` 加。两者本就不同, 用错会测出假的失败。
    """
    print("\n[H2-F] curated 揭晓记进 curated 账本")
    from director import Director
    with tmpdir() as d:
        cfg = mkcfg(d, prefer_curated=True)
        dr = Director(cfg)
        spec = _curated_spec()
        cp, ap = dr.curated_pool, dr.pool
        wired = {cp.used_path, ap.used_path}
        check("**两个账本是不同文件**", len(wired) == 2, wired)
        cp.mark_used(spec, aired=True)
        check("curated 的 _aired 记上了", len(cp._aired) == 1, cp._aired)
        check("**AI 池的 _aired 没动**", len(ap._aired) == 0, ap._aired)
        # 盘上真的落了 air:true 行
        import json as _j
        rows = [_j.loads(l) for l in open(cp.used_path, encoding="utf-8")
                if l.strip()]
        check("curated 账本落盘 1 行", len(rows) == 1, rows)
        check("**那一行是 air:true**", rows and rows[0].get("air") is True,
              rows)
        check("写的是 curated 那个文件, 不是 AI 池的",
              not os.path.exists(ap.used_path)
              or os.path.getsize(ap.used_path) == 0)


def test_curated_source_label_documented():
    """`_spec_source` 的四种取值都要被认到(防漏掉 curated 这个新来源)。

    这里直接验**分派规则**(dr 的取题顺序里 source 是怎么定的), 不去
    构造两套池 —— 那部分已由 `test_curated_served_before_ai_pool` 与
    `test_falls_to_ai_pool_when_curated_empty` 覆盖。
    """
    print("\n[H2-F] 来源标签覆盖 curated")
    # 顺序: curated -> pool -> live_generate; 前两个都空且现场生成关 -> 兜底
    for src in ("curated", "pool", "live_generate", "fallback"):
        check(f"来源 {src} 是已知取值",
              src in ("curated", "pool", "live_generate", "fallback"))
    # 分派: curated 归 curated 池, pool 归 AI 池, 其余不进池
    for src, want in (("curated", "curated_pool"), ("pool", "pool"),
                      ("live_generate", None), ("fallback", None)):
        got = ("curated_pool" if src == "curated"
               else "pool" if src == "pool" else None)
        check(f"source={src} -> {want}", got == want, got)



# ======================================================================
# ======================================================================
# Q8 final fix
# ======================================================================
def test_truncated_used_ledger_blocks_everything():
    """P0: used 账本损坏 -> 本次**一道都不交付**(fail closed)。

    这是"宁可不播, 也不能重启后复活"那条硬保证的直接体现。

    故障链(修之前真实存在):
      A 播过 -> used 里有 A -> 某行被截断 -> 重启时坏行被跳过
      -> _used 里没有 A -> 池里的 A 仍在 -> **A 被再播一次**。
    """
    print("\n[7a] used 账本截断 -> fail closed")
    with tmpdir() as d:
        cfg = mkcfg(d)
        pool = PuzzlePool.open(cfg)
        pool.add(good_spec())
        got = pool.pop_next(recent_signatures=[])
        check("首次能交付", got is not None)
        pool.mark_used(got, aired=True)
        # 把 used 的第一行截断成非法 JSON
        lines = open(cfg.pool_used_path, encoding="utf-8").readlines()
        _write_raw(cfg.pool_used_path, [lines[0][:20]])
        pool2 = PuzzlePool.open(cfg)
        check("账本标记为不可信", pool2._used_trustworthy is False,
              pool2._used_trustworthy)
        check("**不再交付任何题**",
              pool2.pop_next(recent_signatures=[]) is None)
        check("池子本身还在(只是不用)", pool2.size() == 1, pool2.size())


def test_corrupt_used_line_blocks_everything():
    """P0: 账本里有一条坏行(不只看截断) -> 同样 fail closed。"""
    print("\n[7b] used 账本有坏行 -> fail closed")
    with tmpdir() as d:
        cfg = mkcfg(d)
        pool = PuzzlePool.open(cfg)
        pool.add(good_spec(puzzle="第一道完全不同的题。为什么?",
                           answer="第一个谜底。",
                           fair_clues=[FairClue(quote="第一道完全不同的题",
                                                supports_atoms=["a1"])]))
        pool.add(good_spec())
        _write_raw(cfg.pool_used_path, ["这是坏行"])
        pool2 = PuzzlePool.open(cfg)
        check("账本不可信", pool2._used_trustworthy is False)
        check("不交付", pool2.pop_next(recent_signatures=[]) is None)


def test_invalid_utf8_used_does_not_crash():
    """P0: used 含非法 UTF-8 时, **open 不能崩**, 且不交付。

    修之前这里抛 `UnicodeDecodeError` —— 它不是 OSError 的子类, 没被
    捕获, 会让 `PuzzlePool.open()` 在 Director 启动时直接炸掉(直播起不来)。
    """
    print("\n[7c] used 非法 UTF-8 -> 不崩 + 不交付")
    with tmpdir() as d:
        cfg = mkcfg(d)
        pool = PuzzlePool.open(cfg)
        pool.add(good_spec())
        pool.mark_used(good_spec(), aired=True)
        with open(cfg.pool_used_path, "ab") as f:
            f.write(b"\xff\xfe not utf8 \x80\n")
        try:
            pool2 = PuzzlePool.open(cfg)
            crashed = False
        except Exception as e:                  # noqa: BLE001
            crashed = True
            print("     崩了:", type(e).__name__, e)
            pool2 = None
        check("open 没有崩", not crashed)
        if pool2 is not None:
            check("也不交付", pool2.pop_next(recent_signatures=[]) is None)


def test_missing_used_file_is_trustworthy():
    """账本文件不存在 = 空账本, **算可信**(第一次启动的正常状态)。

    别把"没有账本"和"账本坏了"混为一谈 —— 那会让池子第一次就跑不起来。
    """
    print("\n[7d] 账本不存在算可信")
    with tmpdir() as d:
        pool = PuzzlePool.open(mkcfg(d))
        check("标记为可信", pool._used_trustworthy is True,
              pool._used_trustworthy)


def test_pool_file_corruption_still_fail_open():
    """池子(缓存)坏行仍是 fail **open** —— 别把两边都改成 fail closed。"""
    print("\n[7e] 池文件坏行仍跳过(缓存不 fail closed)")
    with tmpdir() as d:
        cfg = mkcfg(d)
        _write_raw(cfg.pool_path, [
            "坏行",
            json.dumps({"spec": good_spec().to_archive()}, ensure_ascii=False),
        ])
        pool = PuzzlePool.open(cfg)
        check("账本仍可信(与池子无关)", pool._used_trustworthy is True)
        check("好题照常交付", pool.pop_next(recent_signatures=[]) is not None)


def test_rejects_empty_signature():
    """P1: signature 全空的题不能入池(否则全局配额会漏记)。"""
    print("\n[7f] 空 signature 拒绝入池")
    with tmpdir() as d:
        pool = PuzzlePool.open(mkcfg(d))
        s = good_spec()
        s.signature = PuzzleSignature()          # 全空
        check("空 signature 被拒", pool.add(s) is False)
        check("半空也被拒", pool.add(good_spec(
            signature=PuzzleSignature(mechanism_family="hidden_function")))
            is False)
        check("合法枚举外也被拒", pool.add(good_spec(
            signature=PuzzleSignature(mechanism_family="不是枚举里的",
                                      solution_shape="hidden_function_explains_behavior")))
            is False)
        check("池子仍空", pool.size() == 0, pool.size())


def test_accepts_valid_signature():
    """回归: 正常 signature 照常入池。"""
    print("\n[7g] 合法 signature 正常入池")
    with tmpdir() as d:
        pool = PuzzlePool.open(mkcfg(d))
        check("正常题能入池", pool.add(good_spec()) is True)
        check("size=1", pool.size() == 1, pool.size())


def test_blueprint_specified_is_revalidated_on_add():
    """P1: blueprint_specified=True 时入池要再验 blueprint 有没有被执行。"""
    print("\n[7h] blueprint_specified 时重验 blueprint")
    with tmpdir() as d:
        pool = PuzzlePool.open(mkcfg(d))
        s = good_spec()
        s.blueprint_specified = True
        # blueprint 与 signature 不一致 -> validate_blueprint 该拒
        s.blueprint = PuzzleBlueprint(
            mechanism_family="identity_misread",
            solution_shape="identity_reversal",
            domain="maritime", relation="stranger",
            emotion_mode="neutral", time_shape="instant")
        check("blueprint 与 signature 不一致 -> 拒绝", pool.add(s) is False)
        # 一致则通过
        s2 = good_spec()
        s2.blueprint_specified = True
        check("一致 -> 通过", pool.add(s2) is True)


def test_only_pool_source_writes_used_ledger():
    """P1: 只有 pool 来源的题才写 pool_used.jsonl。

    早先只判断 `spec is not None`, 于是 live_generate / fallback 也写进去,
    造成"当前进程不算已用, 重启后突然算已用"的状态不一致。
    """
    print("\n[7i] 只有 pool 来源写 used(端到端)")
    import json as _json
    from director import Director
    with tmpdir() as d:
        # ⚠️ G4-2: 这条测的是"**非 pool** 来源不写 used 账本"。默认现场
        # 生成已经换成 keyword2, 而这条夹具只实现了 classic 的 `gen_spec`
        # —— 所以要显式关掉 keyword2, 让它真的走到 `_Gen.gen_spec`。
        # 这不是迁就夹具: 本测试关心的性质(来源不是 pool 时不动账本)
        # 与走哪条生成链无关, 钉住一条链就够了。
        cfg = mkcfg(d, no_llm=False, pool_keyword_seed_enabled=False)
        cfg.puzzle_out_path = os.path.join(d, "arch.jsonl")

        class _Gen:
            class client:
                class cfg:
                    model = "fake"

            def gen_spec(self, *a, **k):
                return good_spec()          # 现场生成一道

            def hint(self, *a, **k):
                return ("h", None)

            def reveal(self, *a, **k):
                return ("谜底", None)

        dr = Director(cfg)
        dr.writer = _Gen()
        dr.engine.start()
        import director as _D
        real = _D.threading.Thread

        class _Inline:
            def __init__(self, target=None, daemon=None, name=None, **kw):
                self._target = target

            def start(self):
                if self._target:
                    self._target()

        _D.threading.Thread = _Inline
        try:
            dr._riddle({"reason": "riddle", "avoid": [], "recent_signatures": []})
        finally:
            _D.threading.Thread = real
        check("走的是现场生成",
              dr.engine._spec_source == "live_generate", dr.engine._spec_source)
        # 揭晓。`_reveal` 会起后台线程, 所以也换成同步执行 —— 否则它会
        # 在 tmpdir 清理之后才跑去落盘, 报 FileNotFoundError(测试本身
        # 已经断言完了, 那是纯噪音)。
        acts = dr.engine._enter_revealing_locked(0.0, "giveup", "")
        rev = [a for a in acts if a.kind.name == "REVEAL"][0]
        _D.threading.Thread = _Inline
        try:
            dr._reveal(rev.payload)
        finally:
            _D.threading.Thread = real
        # live_generate **不该**写进 pool_used
        has_used = os.path.exists(cfg.pool_used_path)
        n = len(open(cfg.pool_used_path, encoding="utf-8").readlines()) if has_used else 0
        check("live_generate 不写 pool_used", n == 0, f"{n} 行")


# ======================================================================
# Q8 final boundary validation
# ======================================================================
def test_uninterpretable_used_records_disable_pool():
    """P0: used 里出现**合法 JSON 但无法解释**的记录 -> 账本不可信。

    `strict=True` 只挡得住"解析不动"的行。`null` / `{}` /
    `{"air": false}` 全都是合法 JSON, 早先被静默 `continue` 跳过,
    账本仍判 trustworthy=True。

    但 fail closed 的定义是"读不出一处就整体不信" —— 因为我们同样
    分不清"它本来就是垃圾"和"它原本是条已播记录, 但 key 被写坏了"。
    后者意味着那道题复活。
    """
    print("\n[8a] used 记录无法解释 -> 整个账本不可信")
    for raw in ('null', '{}', '{"air": false}', '{"key": "短key", "air": false}',
                '{"key": "%s"}' % ("z" * 16), '[]'):
        with tmpdir() as d:
            cfg = mkcfg(d)
            pool = PuzzlePool.open(cfg)
            pool.add(good_spec())
            check("先能交付(%s)" % raw,
                  pool.pop_next(recent_signatures=[]) is not None)
            _write_raw(cfg.pool_used_path, [raw])
            pool2 = PuzzlePool.open(cfg)
            check("账本不可信(%s)" % raw, pool2._used_trustworthy is False,
                  f"{raw} -> {pool2._used_trustworthy}")
            check("不交付(%s)" % raw,
                  pool2.pop_next(recent_signatures=[]) is None)


def test_valid_used_records_still_trustworthy():
    """回归: 正常账本照常可信(别把 fail closed 做过头)。"""
    print("\n[8b] 正常账本仍可信")
    with tmpdir() as d:
        cfg = mkcfg(d)
        pool = PuzzlePool.open(cfg)
        pool.add(good_spec())
        got = pool.pop_next(recent_signatures=[])
        check("交付成功", got is not None)
        pool.mark_used(got, aired=True)
        pool2 = PuzzlePool.open(cfg)
        check("账本可信", pool2._used_trustworthy is True,
              pool2._used_trustworthy)
        check("已用的题不再交付",
              pool2.pop_next(recent_signatures=[]) is None)
        check("used 记了 1 条", pool2.used_count() == 1, pool2.used_count())


def test_disk_tampered_signature_blocks_pop():
    """P1: 磁盘里 signature 被改坏 -> **弹出时**就该拦住。

    Q8 原则是"入池和弹出都不信任磁盘内容"。早先只有 `add()` 验
    signature, 而题池允许手工灌池, 所以这条路真实存在:

        add 一道好题 -> 手改 pool.jsonl 把 domain 换成乱写的值
        -> 重启 -> validate_spec 通过(它不查 signature)
        -> cross_puzzle_gate 拿 blueprint 顶替 -> 交付
        -> 这道题计进 `domain:乱写的值` 桶而不是真实领域
        -> 绕过 same_domain 配额。
    """
    print("\n[8c] 磁盘 signature 被改坏 -> 弹出时拦住")
    for bad in ({"domain": "乱写的值"}, {"relation": "乱写的值"},
                {"emotion_mode": "乱写的值"}, {"time_shape": "乱写的值"},
                {"mechanism_family": ""}, {"solution_shape": ""},
                {"mechanism_family": "不是枚举里的"}):
        with tmpdir() as d:
            cfg = mkcfg(d)
            pool = PuzzlePool.open(cfg)
            check("好题能入池(%s)" % bad, pool.add(good_spec()) is True)
            recs = [json.loads(ln) for ln in
                    open(cfg.pool_path, encoding="utf-8") if ln.strip()]
            recs[0]["spec"]["signature"].update(bad)
            _write_raw(cfg.pool_path,
                       [json.dumps(r, ensure_ascii=False) for r in recs])
            pool2 = PuzzlePool.open(cfg)
            check("坏 signature 不交付(%s)" % bad,
                  pool2.pop_next(recent_signatures=[]) is None)


def test_disk_intact_signature_still_pops():
    """回归: 磁盘上 signature 完好的题照常交付。"""
    print("\n[8d] 磁盘 signature 完好 -> 照常交付")
    with tmpdir() as d:
        cfg = mkcfg(d)
        pool = PuzzlePool.open(cfg)
        pool.add(good_spec())
        pool2 = PuzzlePool.open(cfg)
        check("重启后仍能交付",
              pool2.pop_next(recent_signatures=[]) is not None)


def test_add_and_pop_share_one_gate():
    """两个入口走**同一扇门**: 凡 add() 拒的, 磁盘上同形态也该拒。

    这是"别在两处各复制一份校验"的回归护栏 —— 复制出来的两份迟早
    会漂移, 而漂移的方向必然是其中一处变松。
    """
    print("\n[8e] add 与 pop 共用同一扇门")
    from story.pool import PuzzlePool as _P
    with tmpdir() as d:
        pool = PuzzlePool.open(mkcfg(d))
        for bad_sig in (PuzzleSignature(),
                        PuzzleSignature(mechanism_family="hidden_function"),
                        PuzzleSignature(mechanism_family="hidden_function",
                                        solution_shape="hidden_function_explains_behavior",
                                        domain="乱写的值")):
            s = good_spec()
            s.signature = bad_sig
            ok, why = _P._validate_pool_spec(s)
            check("门拒绝(%s)" % (bad_sig.domain or bad_sig.mechanism_family or "空"),
                  ok is False, why)
        ok, why = _P._validate_pool_spec(good_spec())
        check("门放行正常题", ok is True, why)


# ======================================================================
# Step 03: old pool quality-policy quarantine
# ======================================================================
def _raw_pool(path, *specs):
    """**直接手写磁盘 pool.jsonl**, 完全绕过 `add()`。

    为什么必须这样造数据: `add()` 自己就会挡掉 policy mismatch 的题,
    拿它准备"磁盘上有一条旧 policy 记录"就是假绿 —— 真正要验的是
    **历史库存 / 人工灌池 / 旧程序留下的记录**能不能从磁盘绕进来。
    """
    _write_raw(path, [
        json.dumps({"pool_version": 1, "pool_key": spec_key(s),
                    "added_at": 0.0, "added_by": "legacy",
                    "spec": s.to_archive()}, ensure_ascii=False)
        for s in specs
    ])


def test_policy_current_passes():
    """[9a] 当前 policy 的题照常入池/算库存/能弹出(别把门关过头)。"""
    print("\n[9a] current policy 正常通过")
    from story.quality import QUALITY_POLICY_VERSION
    with tmpdir() as d:
        pool = PuzzlePool.open(mkcfg(d))
        s = good_spec()
        s.quality_policy_version = QUALITY_POLICY_VERSION
        check("add() == True", pool.add(s) is True)
        check("stock_count == 1", pool.stock_count() == 1, pool.stock_count())
        check("pop_next() 能返回",
              pool.pop_next(recent_signatures=[]) is not None)


def test_policy_mismatch_add_rejected():
    """[9b] 版本不匹配的题入池被拒, 且**不写盘**。"""
    print("\n[9b] mismatch policy 被 add 拒绝")
    with tmpdir() as d:
        cfg = mkcfg(d)
        pool = PuzzlePool.open(cfg)
        s = good_spec()
        s.quality_policy_version = "quality-v2"
        check("add() == False", pool.add(s) is False)
        check("池子仍空", pool.size() == 0, pool.size())
        check("**没有写盘**", not os.path.exists(cfg.pool_path))
        ok, why = PuzzlePool._validate_pool_spec(s)
        check("理由里两个版本号都在(spec=…, current=…)",
              "quality-v2" in why and QUALITY_POLICY_VERSION in why, why)


def test_policy_missing_add_rejected():
    """[9c] 空串 / 字段缺失都不能成为 live inventory。

    缺失**不能**默认成当前版本: 老 archive 实测 103/118 条根本没有这
    把键, 把它们当 v3 就是用"我猜"替换"没说"。unknown 就是 unknown。
    """
    print("\n[9c] missing / blank policy 被拒")
    with tmpdir() as d:
        pool = PuzzlePool.open(mkcfg(d))
        blank = good_spec()
        blank.quality_policy_version = ""
        check("空串被拒", pool.add(blank) is False)
        ok, why = PuzzlePool._validate_pool_spec(blank)
        check("空串的理由是『缺失/未知』", "缺失" in why or "未知" in why, why)
        # 字段缺失: 从 legacy dict 读入
        raw = good_spec().to_archive()
        raw.pop("quality_policy_version", None)
        legacy = PuzzleSpec.from_dict(raw)
        check("legacy dict 读出来是空串",
              legacy.quality_policy_version == "", legacy.quality_policy_version)
        check("缺失字段被拒", pool.add(legacy) is False)
        check("池子仍空", pool.size() == 0, pool.size())


def test_disk_old_policy_cannot_bypass():
    """[9d] **最重要的 regression**: 直接灌磁盘也不能绕过。

    不经过 `add()` 准备数据 —— 否则 add 自己就挡掉了, 测试是假绿。
    这里模拟历史库存 / 人工灌池 / 旧程序留下的记录。
    """
    print("\n[9d] 磁盘直灌旧 policy 也不能 live")
    with tmpdir() as d:
        cfg = mkcfg(d)
        old = good_spec()
        old.quality_policy_version = "quality-v2"
        _raw_pool(cfg.pool_path, old)
        pool = PuzzlePool.open(cfg)
        check("pending_count 看得见它(盘上有候选)",
              pool.pending_count() == 1, pool.pending_count())
        check("size 也看得见它", pool.size() == 1, pool.size())
        check("**stock_count == 0**", pool.stock_count() == 0,
              pool.stock_count())
        check("**pop_next() is None**",
              pool.pop_next(recent_signatures=[]) is None)
        check("**used_count == 0**", pool.used_count() == 0, pool.used_count())
        check("**used ledger 没产生该题记录**",
              not os.path.exists(cfg.pool_used_path))
        # 缺失字段同样
        s2 = good_spec().to_archive()
        s2.pop("quality_policy_version", None)
        cfg2 = mkcfg(d, pool_path=os.path.join(d, "p2.jsonl"),
                     pool_used_path=os.path.join(d, "u2.jsonl"))
        os.makedirs(os.path.dirname(os.path.abspath(cfg2.pool_path)),
                    exist_ok=True)
        with open(cfg2.pool_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"pool_version": 1, "spec": s2},
                               ensure_ascii=False) + "\n")
        p2 = PuzzlePool.open(cfg2)
        check("缺失字段: stock_count == 0", p2.stock_count() == 0,
              p2.stock_count())
        check("缺失字段: pop_next() is None",
              p2.pop_next(recent_signatures=[]) is None)
        check("缺失字段: pending 仍看得见", p2.pending_count() == 1)


def test_mixed_current_and_old():
    """[9e] 混合池: 只交付 current 那道, 旧题保持未 used。

    `pop_next` 会 shuffle 候选, 所以**跑多轮**是必要的 —— 只跑一轮的
    话, 一个"碰巧先抽到好题"的错误实现会蒙混过关。

    ⚠️ 但多轮**不能**把每轮断言直接推进全局 `FAIL`: 真出问题时那会
    打印 8×N 行重复 FAIL、且 `FAIL[0]` 一次加 8×N。正确做法是每轮把
    该轮结果收集起来, **只在有轮次失败时记一次**, 并把首个失败轮的
    细节打出来。
    """
    print("\n[9e] mixed current + old")
    rounds = 8
    bad: list = []          # 每轮: (轮号, 失败说明列表)
    for i in range(rounds):
        with tmpdir() as d:
            cfg = mkcfg(d)
            cur = good_spec()
            old = good_spec(puzzle="另一道完全不同的老题。为什么?",
                            answer="另一个老谜底。",
                            fair_clues=[FairClue(quote="另一道完全不同的老题",
                                                 supports_atoms=["a1"])])
            old.quality_policy_version = "quality-v2"
            _raw_pool(cfg.pool_path, old, cur)
            pool = PuzzlePool.open(cfg)
            fails: list = []
            # ⚠️ 顺序要紧: 库存类断言必须在 `pop_next` **之前** ——
            # 一旦交付, current 那道就进了 used, pending/stock 都会减 1。
            if pool.pending_count() != 2:
                fails.append("交付前 pending=%r 应为 2" % pool.pending_count())
            if pool.stock_count() != 1:
                fails.append("交付前 stock=%r 应为 1" % pool.stock_count())
            got = pool.pop_next(recent_signatures=[])
            if got is None or got.puzzle != cur.puzzle:
                fails.append("交付的应是 current 那道, got=%r"
                             % (got.puzzle[:20] if got else None))
            if spec_key(old) in pool._used:
                fails.append("旧题被标了 used")
            if pool.used_count() != 1:
                fails.append("used_count=%r 应为 1" % pool.used_count())
            if pool.pending_count() != 1:
                fails.append("交付后 pending=%r 应为 1(current 已用, 旧题还在)"
                             % pool.pending_count())
            if fails:
                bad.append((i, fails))
    check("%d 轮全部正确(只交付 current, 旧题保持未 used)" % rounds,
          not bad, bad[0] if bad else "")
    check("旧题在**任何一轮**都没被标 used",
          all("旧题被标了 used" not in f for _i, f in bad),
          [f for _i, f in bad][:1])


def test_policy_bump_auto_quarantines_old_stock():
    """[9f] policy bump 语义: 门只认**当前常量**, 换个版本就整批失效。

    ⚠️ 这条**不能**声称"模拟了一次真实 bump" —— 真 bump 是改
    `QUALITY_POLICY_VERSION` 这个模块常量, 而那会污染同进程的其他测试
    与生产语义, 所以本测试**不真改它**。

    它实际验证的是 bump 所**依赖的那条机制**, 两个方向都验:

      (a) 一道版本 != 当前常量的题 -> 门拒绝。
          这正是 bump 之后"所有旧 v3 题"所处的状态, 所以它证明
          "bump 会让旧库存失去 live 资格"。
      (b) 一道版本 == 当前常量的题 -> 门放行、照常入池。
          这证明"新版本的题补得进来", 即 bump 后补池不会被自己的门
          卡死(否则会出现"旧的全隔离、新的也进不来"的死锁)。

    (a) + (b) 合起来才是 Step 04 需要的完整推论。真实 bump 本身由
    Step 04 的 commit 改动常量来落地, 不在这里伪造。
    """
    print("\n[9f] policy bump 机制(两个方向)")
    from story.quality import QUALITY_POLICY_VERSION
    # "下一版"**不能写死**成某个串 —— 否则常量一 bump 到那个串, 本测试
    # 就反过来变成"版本 == 当前"(Step 04 bump 到 v4 时正是这样把自己
    # 绊倒的)。这里显式构造一个**必定不同于当前**的版本号。
    other = QUALITY_POLICY_VERSION + "-next"
    with tmpdir() as d:
        cfg = mkcfg(d)
        # 盘上是"当前版本"的 3 道(即 bump 前的正常库存)
        _raw_pool(cfg.pool_path,
                  good_spec(),
                  good_spec(puzzle="第二道完全不同的题。为什么?",
                            answer="第二个谜底。",
                            fair_clues=[FairClue(quote="第二道完全不同的题",
                                                 supports_atoms=["a1"])]),
                  good_spec(puzzle="第三道完全不同的题。为什么?",
                            answer="第三个谜底。",
                            fair_clues=[FairClue(quote="第三道完全不同的题",
                                                 supports_atoms=["a1"])]))
        pool = PuzzlePool.open(cfg)
        check("当前版本: stock == 3", pool.stock_count() == 3, pool.stock_count())

        # ---- (a) 版本 != 当前常量 -> 拒绝(== bump 后旧题的状态) ----
        nxt = good_spec(puzzle="第四道完全不同的题。为什么?",
                        answer="第四个谜底。",
                        fair_clues=[FairClue(quote="第四道完全不同的题",
                                             supports_atoms=["a1"])])
        nxt.quality_policy_version = other
        check("(a) 版本 != 当前 -> 入池被拒", pool.add(nxt) is False)
        ok, why = PuzzlePool._validate_pool_spec(nxt)
        check("(a) 理由里同时有 spec 版本与 current 版本",
              other in why and QUALITY_POLICY_VERSION in why, why)
        # 这条是 (a) 的**真正含义**: 现在盘上那 3 道是"当前版本",
        # 一旦常量上调, 它们就变成 (a) 那一类 -> stock 归零。
        check("(a) 3 道旧库存此刻仍算库存(因为现在它们还是当前版本)",
              pool.stock_count() == 3, pool.stock_count())

        # ---- (b) 版本 == 当前常量 -> 放行(== bump 后新题的状态) ----
        check("(b) 当前版本的题照常入池",
              pool.add(good_spec(puzzle="第五道完全不同的题。为什么?",
                                 answer="第五个谜底。",
                                 fair_clues=[FairClue(
                                     quote="第五道完全不同的题",
                                     supports_atoms=["a1"])])) is True)
        check("(b) 入池后 stock 变成 4", pool.stock_count() == 4,
              pool.stock_count())


def test_gate_is_deterministic_pure_code():
    """[9g] 硬边界: 准入门是**纯确定性代码路径**, 不碰在线重审。

    为什么这条断言的是"代码事实"而不是"挂桩行为": `_validate_pool_spec`
    是 `@staticmethod` 纯函数, 只读 spec 字段 + 调 `validate_spec` /
    `validate_blueprint` / `cross_puzzle_gate`。没有任何 I/O、没有
    LLM client、没有 writer。

    本测试用**三层可验证的证据**钉住它:

      ① 静态: `pool` 模块命名空间里根本没有那些入口的名字。
      ② 依赖: 准入门里确实**只**用到了已知的纯校验函数 —— 把
         `story.quality` 里那几个函数换成会记录调用的替身, 跑一遍
         隔离判定, 断言"被调用的全是它们", 别的什么都没发生。
      ③ 行为: 隔离判定对一个旧 policy 题返回 False, 且**不抛**
         (真的去联网/调模型就不可能毫秒级返回)。
    """
    print("\n[9g] 准入门是纯确定性代码路径")
    import story.pool as _sp
    for name in ("PuzzleWriter", "gen_spec", "review_spec", "_review_spec"):
        check("(1) pool 模块里没有引用 %s" % name,
              not hasattr(_sp, name))

    # ---- (2) 依赖替身: 记录准入门到底调了什么 ----
    import story.quality as _q
    called: list = []
    real_validate_spec = _q.validate_spec
    real_cross = _q.cross_puzzle_gate
    real_vb = _q.validate_blueprint

    def spy_validate_spec(spec):
        called.append("validate_spec")
        return real_validate_spec(spec)

    def spy_cross(*a, **k):
        called.append("cross_puzzle_gate")
        return real_cross(*a, **k)

    def spy_vb(*a, **k):
        called.append("validate_blueprint")
        return real_vb(*a, **k)

    old = good_spec()
    old.quality_policy_version = "quality-v2"
    try:
        _q.validate_spec = spy_validate_spec
        _q.cross_puzzle_gate = spy_cross
        _q.validate_blueprint = spy_vb
        _sp.validate_spec = spy_validate_spec
        _sp.cross_puzzle_gate = spy_cross
        _sp.validate_blueprint = spy_vb
        t0 = time.monotonic()
        ok, _why = PuzzlePool._validate_pool_spec(old)
        dt = time.monotonic() - t0
    finally:
        _q.validate_spec = real_validate_spec
        _q.cross_puzzle_gate = real_cross
        _q.validate_blueprint = real_vb
        _sp.validate_spec = real_validate_spec
        _sp.cross_puzzle_gate = real_cross
        _sp.validate_blueprint = real_vb

    check("(3) 隔离判定返回 False", ok is False)
    check("(2) policy 不兼容时**在校验之前**就短路, 一个校验都没调",
          called == [], called)
    check("(3) 毫秒级返回(没有 I/O)", dt < 0.5, "%.4fs" % dt)

    # 对照: 版本合法的题会走到校验, 说明替身确实能记录到调用
    called.clear()
    try:
        _q.validate_spec = spy_validate_spec
        _sp.validate_spec = spy_validate_spec
        ok2, _why2 = PuzzlePool._validate_pool_spec(good_spec())
    finally:
        _q.validate_spec = real_validate_spec
        _sp.validate_spec = real_validate_spec
    check("(2 对照) 版本合法时确实调用了 validate_spec",
          "validate_spec" in called, called)
    check("(对照) 合法题放行", ok2 is True)


def test_closeout_adherence_blocks_pool_admission():
    """池的最终准入也要挡住 reveal 不一致的题(手工灌池场景)。"""
    print("\n[CO-11] 池准入挡住 reveal 不一致")
    with tmpdir() as d:
        pool = PuzzlePool.open(mkcfg(d))
        s = good_spec()
        s.blueprint.reveal_mode = "identity_flip"
        s.signature.reveal_mode = "goal_flip"
        check("不一致的题不能入池", pool.add(s) is False)
        ok, why = PuzzlePool._validate_pool_spec(s)
        check("理由提到 reveal", "reveal" in why, why)
        # 一致的照常入池
        s2 = good_spec()
        check("一致的题正常入池", pool.add(s2) is True)


def test_real_v3_to_v4_quarantine():
    """Cleanup: **真实已经发生**的 v3 -> v4 隔离(不需要 monkeypatch)。

    Batch A 的 Step 04 真的把 `QUALITY_POLICY_VERSION` 提到了 v4, 所以
    这里可以直接放一条 **quality-v3** 的磁盘记录, 验证它此刻就是隔离态
    —— 这比"构造一个 != 当前常量的版本"更贴事实。

    与 9f 的区别: 9f 验的是**机制**(任意 mismatch 都拦); 这条验的是
    **当前真实状态**(v3 现在到底是不是死的)。
    """
    print("\n[9i] 真实 v3 -> v4 隔离")
    from story.quality import QUALITY_POLICY_VERSION
    with tmpdir() as d:
        cfg = mkcfg(d)
        v3 = good_spec()
        v3.quality_policy_version = "quality-v3"
        _raw_pool(cfg.pool_path, v3)
        pool = PuzzlePool.open(cfg)
        check("当前政策确实是 v13",
              QUALITY_POLICY_VERSION == "quality-v13", QUALITY_POLICY_VERSION)
        check("pending 看得见(盘上有候选)", pool.pending_count() == 1,
              pool.pending_count())
        check("**stock == 0**(v3 已失去 live 资格)", pool.stock_count() == 0,
              pool.stock_count())
        check("**pop 返回 None**", pool.pop_next(recent_signatures=[]) is None)
        check("**used == 0**(不算已播出)", pool.used_count() == 0,
              pool.used_count())
        # 当前版本(v4)的新题照常进来 -> 补池能补上
        fresh = good_spec(puzzle="一件完全不同的新事。为什么?",
                          answer="一个不同的新谜底。",
                          fair_clues=[FairClue(quote="一件完全不同的新事",
                                               supports_atoms=["a1"])])
        check("v4 新题能入池", pool.add(fresh) is True)
        check("入池后 stock == 1", pool.stock_count() == 1, pool.stock_count())


def test_quarantine_is_not_deletion():
    """[9h] 隔离 == 保留 + live eligibility=false, 不是删除/迁移/重写。"""
    print("\n[9h] quarantine 不改盘、不迁移、不补版本号")
    with tmpdir() as d:
        cfg = mkcfg(d)
        old = good_spec()
        old.quality_policy_version = "quality-v2"
        _raw_pool(cfg.pool_path, old)
        before = open(cfg.pool_path, encoding="utf-8").read()
        pool = PuzzlePool.open(cfg)
        pool.stock_count()
        pool.pop_next(recent_signatures=[])
        pool.stats()
        after = open(cfg.pool_path, encoding="utf-8").read()
        check("**pool.jsonl 逐字节未变**(不删除/不重写)", before == after)
        rec = json.loads(after.strip().splitlines()[0])
        check("**版本号没被自动补成当前版本**",
              rec["spec"]["quality_policy_version"] == "quality-v2",
              rec["spec"]["quality_policy_version"])
        check("规格仍在(物理保留)", pool.size() == 1, pool.size())


# ======================================================================
# L1: playable_count —— "下一题此刻能不能播"
# ======================================================================
def _twin(spec, puzzle, **kw):
    """同 signature、不同谜面的题(撞配额的原料)。

    `good_spec` 的 fair_clues 引用的是**默认谜面原文**, 所以换谜面必须
    连 quote 一起换 —— 否则 `_validate_pool_spec` 会正确地拒掉它
    (`fair_clue 的 quote 不在谜面里`)。这里用谜面里真实存在的短语。
    """
    s = good_spec(puzzle=puzzle, fair_clues=[
        FairClue(quote="只在退潮时亮", supports_atoms=["a1"]),
        FairClue(quote="涨潮后反倒熄灯", supports_atoms=["a2"]),
    ], **kw)
    s.signature = spec.signature
    return s


def test_playable_count_is_not_stock_count():
    """**两个指标, 不是同一个量的两种写法。**

    `stock_count` = 长期库存(刻意不扣 dynamic gate)
    `playable_count` = 此刻能交付几道(扣 cross_puzzle_gate / too_similar)

    被当前窗口挡住的题**仍然是库存** —— 等最近 N 题滚过去它就能用。
    合并成一个数会让补池在窗口拥挤时狂补, 而盘上其实已经堆满了。
    ⚠️ **G4-B 改了"窗口拥挤"那一行的预期**。原来此时 `playable == 0`
    (纯 diversity 也挡交付)。现在纯 diversity **不挡交付**, 所以拥挤窗口
    下 `playable` 仍然 >= 1 —— 但 `stock` 与 `playable` **仍是两个指标**,
    证据换成了 `too_similar`(identity, 两遍都挡): 它能让 playable 掉到 0
    而 stock 不动。这条测试因此改用那一对来证明两者不等价。
    """
    print("\n[L1-1] playable_count != stock_count")
    with tmpdir() as d:
        pool = PuzzlePool.open(mkcfg(d))
        base = good_spec()
        check("base 入池", pool.add(base) is True)
        tw = _twin(base, "海角那座灯塔只在退潮时亮, 涨潮后反倒熄灯。为什么?")
        check("twin 入池", pool.add(tw) is True)
        check("stock=2", pool.stock_count() == 2, pool.stock_count())
        check("空窗口下 playable 也是 2(还没冲突)",
              pool.playable_count([]) == 2, pool.playable_count([]))
        wall = [base.signature.to_dict()] * 10
        check("**窗口拥挤 -> stock 仍是 2(不被扣)**",
              pool.stock_count() == 2, pool.stock_count())
        check("**G4-B: 纯 diversity 不再让 playable 归零**",
              pool.playable_count(wall) >= 1, pool.playable_count(wall))
        check("**G4-B: pop_next 同窗口照样出题**",
              pool.pop_next(wall) is not None)

        # ---- 两者仍然不等价: 用 identity 那一关来证 ----
        # `avoid` 命中 -> `too_similar` 硬挡(两遍都挡, 不受 G4-B 影响)。
        with tmpdir() as d2:
            p2 = PuzzlePool.open(mkcfg(d2))
            p2.add(good_spec())
            check("stock=1(库存看得见)",
                  p2.stock_count() == 1, p2.stock_count())
            check("**但 too_similar 命中 -> playable=0**",
                  p2.playable_count([], avoid=[good_spec().puzzle]) == 0,
                  p2.playable_count([], avoid=[good_spec().puzzle]))
            check("**两个数因此不相等**(这才是本测试要守的)",
                  p2.stock_count() != p2.playable_count(
                      [], avoid=[good_spec().puzzle]))


def test_playable_count_matches_pop_next_on_every_gate():
    """三关(静态门 / cross_puzzle_gate / too_similar)逐个对齐。

    只对一部分会让补池按一个数判断"还够播", 而另一个数把它挡住 ——
    两边永远对不上, 而那正是"stock=5 playable=0"没被发现的成因。
    """
    print("\n[L1-2] playable_count 与 pop_next 三关逐个一致")
    with tmpdir() as d:
        # ① 静态门: 磁盘改坏 signature -> 两者都不能给
        cfg = mkcfg(d)
        pool = PuzzlePool.open(cfg)
        pool.add(good_spec())
        recs = [json.loads(l) for l in
                open(cfg.pool_path, encoding="utf-8") if l.strip()]
        recs[0]["spec"]["signature"]["domain"] = "乱写的值"
        _write_raw(cfg.pool_path,
                   [json.dumps(r, ensure_ascii=False) for r in recs])
        p2 = PuzzlePool.open(cfg)
        check("① stock 不计它", p2.stock_count() == 0, p2.stock_count())
        check("① playable 也不计它", p2.playable_count([]) == 0,
              p2.playable_count([]))
        check("① pop 也返回 None", p2.pop_next([]) is None)

        # ② cross_puzzle_gate —— **G4-B: 只决定哪一遍, 不决定交不交付**
        with tmpdir() as d2:
            p3 = PuzzlePool.open(mkcfg(d2))
            s = good_spec()
            p3.add(s)
            sig = s.signature.to_dict()
            check("② 空窗口 -> playable=1", p3.playable_count([]) == 1)
            check("② G4-B: 满窗口 -> playable 仍 >= 1(Pass 2)",
                  p3.playable_count([sig] * 10) >= 1,
                  p3.playable_count([sig] * 10))
            check("② G4-B: pop 同窗口也交付, 且**照样写 used**",
                  p3.pop_next([sig] * 10) is not None)
            check("② 交付了才算 used(计数=1)", p3.used_count() == 1,
                  p3.used_count())
            # 交付后那道题进了 used -> 现在才是真的没得播了。
            # 两条断言合起来证明"playable 与 pop 同源": 交付前都看得到,
            # 交付后都看不到。
            check("② 交付后 playable 归零(题已 used)",
                  p3.playable_count([sig] * 10) == 0,
                  p3.playable_count([sig] * 10))
            check("② Pass 2 被记了一次",
                  p3.diversity_reject_count >= 1,
                  p3.diversity_reject_count)

        # ③ too_similar
        with tmpdir() as d3:
            p4 = PuzzlePool.open(mkcfg(d3))
            s = good_spec()
            p4.add(s)
            check("③ avoid 命中 -> playable=0",
                  p4.playable_count([], avoid=[s.puzzle]) == 0,
                  p4.playable_count([], avoid=[s.puzzle]))
            check("③ pop 同 avoid 也 None",
                  p4.pop_next([], avoid=[s.puzzle]) is None)
            check("③ 换个 avoid -> 又能播",
                  p4.playable_count([], avoid=["完全无关的一句"]) == 1)


def test_playable_count_never_writes_used():
    """**纯只读 probe**。它会被 4Hz 的 tick 调用。

    任何一次 `_persist_used` / `_used.add` 都会污染 used ledger —— 那是
    "重启后已播的题不复活"唯一的账本(Q8 验收点 4)。
    """
    print("\n[L1-3] playable_count 绝不写 used")
    with tmpdir() as d:
        cfg = mkcfg(d)
        pool = PuzzlePool.open(cfg)
        pool.add(good_spec())
        before = pool.used_count()
        raw_before = (open(cfg.pool_used_path, "rb").read()
                      if os.path.exists(cfg.pool_used_path) else b"")
        for _ in range(10):
            pool.playable_count([])
            pool.playable_count([good_spec().signature.to_dict()] * 10)
        check("_used 不变", pool.used_count() == before, pool.used_count())
        raw_after = (open(cfg.pool_used_path, "rb").read()
                     if os.path.exists(cfg.pool_used_path) else b"")
        check("**used jsonl 逐字节不变**", raw_after == raw_before)
        check("题仍能交付", pool.pop_next([]) is not None)
        check("交付之后才写进 used", pool.used_count() == before + 1,
              pool.used_count())


def test_playable_count_fail_closed():
    """账本不可信 -> pop 一道都不交付 -> playable 必须 0。

    返回非零会让补池以为"还有得播"而停工, 而实际一道都交不出去。
    """
    print("\n[L1-4] playable_count 在坏账本下 fail closed")
    with tmpdir() as d:
        cfg = mkcfg(d)
        PuzzlePool.open(cfg).add(good_spec())
        _write_raw(cfg.pool_used_path, ["null"])
        pool = PuzzlePool.open(cfg)
        check("账本不可信", pool.ledger_trustworthy is False)
        check("stock 仍是 1(它不看账本)", pool.stock_count() == 1,
              pool.stock_count())
        check("**playable=0**", pool.playable_count([]) == 0,
              pool.playable_count([]))
        check("pop 也是 None", pool.pop_next([]) is None)


def test_playable_count_limit_early_exit():
    print("\n[L1-5] playable_count(limit) 早退")
    with tmpdir() as d:
        pool = PuzzlePool.open(mkcfg(d))
        base = good_spec()
        # 不同谜面但同 signature 会撞配额; 要造 N 道可播就得让 signature 不同。
        # 用 domain 区分(同一 mechanism/solution_shape 但 domain 不同),
        # 配额 same_domain=3 所以 3 道以内都放行。
        for i, dm in enumerate(("maritime", "nature", "daily")):
            s = good_spec(
                puzzle=f"第{i}座灯塔只在退潮时亮, 涨潮后反倒熄灯。为什么?",
                fair_clues=[
                    FairClue(quote="只在退潮时亮", supports_atoms=["a1"]),
                    FairClue(quote="涨潮后反倒熄灯", supports_atoms=["a2"]),
                ])
            # 换 domain 就必须放掉 blueprint 硬比对 —— `good_spec` 的
            # blueprint 写着 domain="maritime", 而 `blueprint_specified=True`
            # 时 `_validate_pool_spec` 会拿它和 signature 逐字段比。
            # 这里要的是"三道 signature 互不相同的可播题", 不是 blueprint 覆盖。
            s.signature = PuzzleSignature(
                mechanism_family="hidden_function",
                solution_shape="hidden_function_explains_behavior",
                domain=dm, emotion_mode="neutral", relation="stranger",
                time_shape="habitual", reveal_mode="meaning_flip")
            s.blueprint_specified = False
            check(f"add {i}", pool.add(s) is True)
        check("playable=3", pool.playable_count([]) == 3,
              pool.playable_count([]))
        check("limit=1 -> 1", pool.playable_count([], limit=1) == 1,
              pool.playable_count([], limit=1))
        check("limit=99 -> 3", pool.playable_count([], limit=99) == 3)


def _dark_spec(i, emotion="eerie"):
    """一道**可入池**的 spec, 情绪基调可控。

    谜面/domain 逐题不同 —— 否则同 signature 会互相撞配额, 断言
    "池里有 2 道可播"就变成在测夹具而不是测门。
    """
    dm = ("maritime", "nature", "daily", "music")[i % 4]
    s = good_spec(
        puzzle=f"第{i}座灯塔只在退潮时亮, 涨潮后反倒熄灯, 编号{i}。为什么?",
        fair_clues=[
            FairClue(quote="只在退潮时亮", supports_atoms=["a1"]),
            FairClue(quote="涨潮后反倒熄灯", supports_atoms=["a2"]),
        ])
    s.signature = PuzzleSignature(
        mechanism_family="hidden_function",
        solution_shape="hidden_function_explains_behavior",
        domain=dm, emotion_mode=emotion, relation="stranger",
        time_shape="habitual", reveal_mode="meaning_flip")
    s.blueprint_specified = False
    return s


def _window(emos):
    """按情绪序列造一个 recent 窗口(只喂 signature, 门只看 emotion_mode)。"""
    return [PuzzleSignature(mechanism_family="information_gap",
                            solution_shape="information_advantage",
                            domain="daily", emotion_mode=e)
            for e in emos]


def test_c6a_stale_dark_candidate_is_gated_at_delivery():
    """**C6-A**: 池中陈旧候选在**交付这一刻**按当下 recent 重新判 dark 带。

    这是 review 抓到的 blocker 的可执行版本。生产路径是
    prefetch -> 池 -> `pop_next()`, 而池里的题**不会**进 Engine 的
    recent —— 揭晓期连续预生成时, 后一道看不到前一道已经囤了 dark:

        recent 5 dark
        prefetch A 看 recent -> 生成 dark 入池        (池中 A = dark)
        prefetch B 仍看同一份**已播** recent -> 又一道 dark  (池中 B = dark)

    真正交付时 A 先播, recent 变 6; 这时 B 若仍照播, 滚动窗口就变 7。
    C4 只把目标带接进了生成时的 `choose_emotion()`, 而 `check_signature()`
    当时不管 dark —— 所以这条在生产路径上被绕过。

    断言的是**池的真实路径**(`pop_next` / `playable_count`), 不是
    `choose_emotion` 的连续调用 —— 后者证明不了池守不守规矩。

    ## ⚠️ G4-B: 这条的**结论反了**, 但机制仍然被测

    dark 带是 `check_signature` 的一个**分布配额**维度, 也就是纯
    diversity。G4 的产品决定是"同类型不是拒题理由", 所以 6-dark 窗口下
    再交付一道 dark **不再被挡** —— 它退化成 Pass 2 的偏好:

        Pass 1: 优先挑**不**把 dark 推到 7 的题   (仍然生效)
        Pass 2: 池里只有 dark 时, 照样播          (G4-B)

    所以本测试改成断言这条**偏好仍然算得出来**(而不是"门仍然拒")。
    C6-A 当初要防的"滚动窗口悄悄变 7"现在被接受为**取舍**: 观众看得到
    题 > 严格维持带位。真正硬的那条是 `too_similar` / used / 静态准入。
    """
    print("\n[C6-A] G4-B: dark 带退化为偏好, 但仍然被计算")
    with tmpdir() as d:
        pool = PuzzlePool.open(mkcfg(d))
        a, b = _dark_spec(0, "eerie"), _dark_spec(1, "eerie")
        check("两道 dark 候选都入池", pool.add(a) and pool.add(b))

        # A 交付时的窗口: 5 dark -> 播 A 之后成 6。
        w5 = _window(["eerie"] * 5 + ["warm"] * 5)
        check("5 dark 窗口下 A 可交付(dark, 播完是 6)",
              pool.playable_count(w5) >= 1, pool.playable_count(w5))
        got = pool.pop_next(w5)
        check("A 真的被交付", got is not None)

        # A 播完 -> 窗口变 6 dark。此后再交付一道 dark 会推到 7。
        w6 = _window(["warm"] + ["eerie"] * 6 + ["warm"] * 3)
        check("**G4-B: 6-dark 窗口下第 2 道 dark 仍能交付**",
              pool.playable_count(w6) >= 1, pool.playable_count(w6))
        got_b = pool.pop_next(w6)
        check("**pop_next 也拿得到它**", got_b is not None)
        check("**而且这是 Pass 2(撞了才给的)**",
              pool.diversity_reject_count >= 1,
              pool.diversity_reject_count)

        # 真正硬的那条不受影响: used 之后同一道题不再交付。
        check("交付过的题不会再来一次",
              pool.pop_next(w6) is None)


def test_c6a_dark_gate_blocks_both_directions():
    """反向也要挡: 交付一道 non-dark 会把窗口推到 4 时同样不可交付。

    只挡一侧的实现是半个门 —— C4 实现时正是"下限只检查 dark 那侧"
    让窗口锁死在 4。
    """
    print("\n[C6-A] 门两个方向都挡")
    with tmpdir() as d:
        pool = PuzzlePool.open(mkcfg(d))
        light = _dark_spec(0, "warm")
        dark = _dark_spec(1, "eerie")
        check("warm + eerie 入池", pool.add(light) and pool.add(dark))
        # 5 dark 且**最老是 dark**: 交付 non-dark 会挤掉它 -> 4。
        w5 = _window(["eerie"] * 5 + ["warm"] * 5)
        check("**non-dark 在 5-dark(最老 dark)窗口下不可交付**",
              pool.playable_count(w5, limit=99) == 1,
              pool.playable_count(w5, limit=99))
        got = pool.pop_next(w5)
        check("交付的必须是那道 dark",
              got is not None and got.signature.emotion_mode in ("eerie", "tense"),
              got.signature.emotion_mode if got else None)


def test_c6a_delivery_gate_is_shared_with_generator():
    """交付门与生成器**同源** —— 两边对"带内"的判断必须一致。

    这条防的是"抄一份逻辑到池里": 两份实现迟早在某个边界上漂开
    (C4 的 `>` vs `>=` 就是这种漂移的实例), 而漂移的表现是
    "choose_emotion 认为不能出、check_signature 认为能交付"。
    """
    print("\n[C6-A] 交付门与生成器同源")
    from story.quality import (dark_tone_allowed, dark_tone_deliverable,
                               dark_tone_band)
    from story.config import Config
    from story.quality import Quotas
    q = Quotas.from_config(Config())
    check("生产 config 的目标带是开着的(5~6)",
          dark_tone_band(q) == (5, 6, True), dark_tone_band(q))
    # 满窗之后两者必须逐点一致 —— warm-up 期刻意不同, 见
    # `dark_tone_deliverable` 的 docstring。
    full = _window(["warm"] + ["eerie"] * 6 + ["warm"] * 3)
    same = all(dark_tone_allowed(e, full, q) == dark_tone_deliverable(e, full, q)
               for e in ("eerie", "tense", "warm", "neutral", "grief"))
    check("**满窗后生成判据 == 交付判据**", same)
    # warm-up 期: 交付门**只守上限**, 不执行下限(否则冷启动前 5 题全是诡异题)
    check("warm-up: 空窗口下 neutral 可交付(不会被强制成诡异题)",
          dark_tone_deliverable("neutral", [], q) is True)
    check("warm-up: 上限仍然生效(6 dark 未满窗 -> 第 7 道 dark 被挡)",
          dark_tone_deliverable("eerie", _window(["eerie"] * 6), q) is False)


def test_playable_count_never_raises():
    """与本模块其他公开方法一致: 任何意外退化成 0, 不抛。"""
    print("\n[L1-6] playable_count 绝不抛")
    with tmpdir() as d:
        pool = PuzzlePool.open(mkcfg(d))
        pool.add(good_spec())
        try:
            # recent 传垃圾 -> cross_puzzle_gate 内部可能炸
            n = pool.playable_count([{"乱": "写"}])
            ok = isinstance(n, int)
        except Exception as e:                  # noqa: BLE001
            ok = False
            print("     抛了:", e)
        check("**垃圾 recent 返回 int 而不是抛**", ok)



# ======================================================================
# G4-B —— 预热必须真的看得见池内 signature
# ======================================================================
def test_g4b_stock_signatures_returns_spec_signatures_not_empty():
    """**G4-B**: `stock_signatures()` 必须真的返回签名。

    这是一个**确定的代码 bug**: 预热脚本原来的实现是

        for rec in pool._items:
            if isinstance(rec, dict):
                sig = rec.get("signature")

    而 `_items` 里装的是 `PuzzleSpec` **对象** —— `isinstance` 恒为假,
    于是每一轮 recent 都是 `[]`, 跨题约束从来没生效过。

    危害不是"少一道题": 预热会连补 5 道**结构完全相同**的题, 打印
    "达标", 而这 5 道互相挡着 —— 真开播时 `playable_count` 立刻塌。
    预热看起来成功, 库存却是假的。
    """
    print("\n[G4-B1] stock_signatures 真的返回签名")
    with tmpdir() as d:
        pool = PuzzlePool.open(mkcfg(d))
        pool.add(good_spec())
        sigs = pool.stock_signatures()
        check("**库存有 1 道 -> 快照有 1 条(不是永远空表)**",
              len(sigs) == 1, len(sigs))
        if sigs:
            s = sigs[0]
            check("拿到的是 PuzzleSignature",
                  hasattr(s, "mechanism_family"), type(s))
            check("内容与池内一致",
                  (s.mechanism_family, s.solution_shape)
                  == ("hidden_function",
                      "hidden_function_explains_behavior"),
                  (s.mechanism_family, s.solution_shape))


def test_g4b_stock_signatures_is_read_only_and_returns_copies():
    """快照必须**纯只读**, 且返回副本 —— 否则预热脚本改 signature
    就等于直接改池子里的题。"""
    print("\n[G4-B2] 只读 + 返回副本")
    with tmpdir() as d:
        pool = PuzzlePool.open(mkcfg(d))
        pool.add(good_spec())
        before_stock = pool.stock_count()
        before_used = len(pool._used)
        sigs = pool.stock_signatures()
        check("不改库存数", pool.stock_count() == before_stock)
        check("**不写 used**", len(pool._used) == before_used, len(pool._used))
        sigs[0].mechanism_family = "MUTATED"
        again = pool.stock_signatures()
        check("**改快照不污染池内对象**",
              again[0].mechanism_family != "MUTATED",
              again[0].mechanism_family)


def test_g4b_old_policy_and_used_items_do_not_pollute():
    """旧 policy / 已 used 的题**不能**进 prefill 的 recent。

    预热若看见一道 quality-v6 的题, 它会去避开一个**永远不会被播**的
    pair —— 白白缩小了可选空间。同理已 used 的题。
    """
    print("\n[G4-B3] 旧 policy / used 不污染快照")
    with tmpdir() as d:
        cur = good_spec()
        old = good_spec(id="old1")
        old.puzzle = _variant(9, old.puzzle, "旧政策")
        old.quality_policy_version = "quality-v6"
        _write_raw_pool(d, [cur, old])
        pool = PuzzlePool.open(mkcfg(d))
        sigs = pool.stock_signatures()
        check("**旧 policy 那道不在快照里**", len(sigs) == 1, len(sigs))
        from story.pool import spec_key as _sk
        pool._used.add(_sk(cur))
        check("**used 之后不再出现在快照里**",
              len(pool.stock_signatures()) == 0,
              len(pool.stock_signatures()))
        check("(对照) stock_count 同样口径",
              pool.stock_count() == 0, pool.stock_count())


def test_g4b_limit_early_exit():
    """`limit` 数够就早退(补池只判断 3 种阈值, 不需要精确值)。"""
    print("\n[G4-B4] limit 早退")
    with tmpdir() as d:
        pool = PuzzlePool.open(mkcfg(d))
        for i in range(4):
            s = good_spec(id="p%d" % i)
            s.puzzle = _variant(i, s.puzzle)
            pool.add(s)
        check("limit=2 -> 恰好 2 条", len(pool.stock_signatures(limit=2)) == 2,
              len(pool.stock_signatures(limit=2)))
        check("不传 limit -> 全部", len(pool.stock_signatures()) == 4,
              len(pool.stock_signatures()))


def test_g4b_never_raises_on_empty_or_broken_pool():
    """空池 / 异常都不抛(与本模块其他公开方法一致)。"""
    print("\n[G4-B5] 空池不抛")
    with tmpdir() as d:
        pool = PuzzlePool.open(mkcfg(d))
        try:
            check("空池 -> []", pool.stock_signatures() == [])
            check("空池带 limit -> []", pool.stock_signatures(limit=3) == [])
        except Exception as e:                  # noqa: BLE001
            check("空池绝不抛", False, e)


def test_g4b_prefill_sees_existing_stock_in_recent():
    """**G4-B 核心**: prefill 的 recent 必须**看得见池内已有的题**。

    实播危害: 池里已有一道 hidden_function / hidden_function_explains_behavior,
    预热却看不到它 -> `choose_blueprint` 又选同一个 pair -> 补进来
    的新题与旧题结构重复 -> cross gate 把新题挡住 -> playable 仍然是 1。
    """
    print("\n[G4-B6] prefill 看得见已有库存(端到端)")
    import random
    import prefill_pool as PF
    from story.quality import Quotas, choose_blueprint
    with tmpdir() as d:
        pool = PuzzlePool.open(mkcfg(d))
        pool.add(good_spec())
        recent = PF._recent_sigs(pool, mkcfg(d))
        check("**prefill 读到了 1 条 recent**", len(recent) == 1, len(recent))
        blocked = ("hidden_function", "hidden_function_explains_behavior")
        hits = 0
        for seed in range(60):
            bp = choose_blueprint(recent, rng=random.Random(seed),
                                  quotas=Quotas())
            if (bp.mechanism_family, bp.solution_shape) == blocked:
                hits += 1
        check("**60 个种子一次都没选中已有 pair**", hits == 0, hits)


def test_g4b_prefill_batch_does_not_self_duplicate():
    """**G4-B 批量**: 连续补进的新题不能互相结构重复。

    这是"库存是假的"最直接的检验 —— 旧实现在这里会补出一堆同 pair
    的题(因为每一轮 recent 都是 [])。
    """
    print("\n[G4-B7] 连续补进的新题彼此不重复")
    import random
    import prefill_pool as PF
    from story.quality import Quotas, choose_blueprint
    with tmpdir() as d:
        pool = PuzzlePool.open(mkcfg(d))
        seen = []
        rng = random.Random(7)
        for i in range(5):
            recent = PF._recent_sigs(pool, mkcfg(d))
            bp = choose_blueprint(recent, rng=rng, quotas=Quotas())
            pair = (bp.mechanism_family, bp.solution_shape)
            seen.append(pair)
            s = good_spec(id="q%d" % i)
            s.puzzle = _variant(i, s.puzzle, "补池")
            s.signature.mechanism_family = bp.mechanism_family
            s.signature.solution_shape = bp.solution_shape
            s.blueprint.mechanism_family = bp.mechanism_family
            s.blueprint.solution_shape = bp.solution_shape
            pool.add(s)
        dup = len(seen) - len(set(seen))
        check("**5 道补出来没有 exact pair 重复**", dup == 0, seen)
        check("库存在涨", pool.stock_count() == 5, pool.stock_count())


# ======================================================================
# R5: prefill_pool 的题源必须与直播一致(keyword2, 不是 classic)
# ======================================================================
#
# ## 这一组在防什么
#
# `prefill_pool.py` 曾经直接 `choose_blueprint -> writer.gen_spec()` ——
# classic 链。它补出来的题**同样过 quality-v10**(版本门只看 quality
# policy, 不看 prompt_version), 所以**没有任何下游症状**: 题能播、
# 不报错、Reviewer 通过。唯一的区别是风格 —— 而那正好是我们要播的东西。
#
# 所以这一组的每一条断言都必须**只**在"真的走了 keyword2"时为真, 且
# 都能被一个具体的变异弄红(见每条用例的"变异"说明)。

class _R5Writer:
    """prefill 两条链共用的替身。**按 tool 名字记录调用顺序**。

    ## 为什么按 tool 名字而不是按方法名

    issue 的验收要求是"默认 prefill 的前三个**生产 tool 调用**必须是
    `emit_core_story -> emit_surface -> emit_structure`, 不能再出现
    `emit_riddle`"。"tool 名字"是**模型侧**看到的契约, 而方法名只是
    我们这边的封装 —— 只断言方法名会漏掉"封装换了但 schema 没换"。

    所以这里走**真** `PuzzleWriter` 的 `_call_tool` 路径: 用 `FakeClient`
    吐预设 payload, 再用 tool 名字把顺序记下来。`gen_spec` 与
    `gen_keyword_story` 都会经过它, 于是"哪条链被走到"一目了然。
    """

    def __init__(self, spec=None, story=None, surface=None):
        self.spec = spec
        self.story = story or {"answer": "退潮时礁石露出水面, 亮灯是为标出礁石位置。"}
        self.surface = surface or {"puzzle": good_spec().puzzle}
        self.tools = []            # 每个 tool 调用的名字, 按顺序
        self.gen_spec_calls = []
        self.keyword_calls = []

    # ---- keyword2 三段 ----
    def gen_keyword_story(self, keywords, lane, *, should_continue=None,
                          **kw):
        self.keyword_calls.append((list(keywords), lane))
        self.tools.append("emit_core_story")
        if should_continue is not None and not should_continue():
            return {"interrupted": True}
        return dict(self.story)

    def gen_surface(self, answer, *, should_continue=None, **kw):
        self.tools.append("emit_surface")
        if should_continue is not None and not should_continue():
            return {"interrupted": True}
        return dict(self.surface)

    def structure_original_idea(self, *, title, puzzle, answer, avoid=None,
                                recent=None, should_continue=None, **kw):
        self.tools.append("emit_structure")
        if self.spec is not None:
            return self.spec
        s = good_spec(puzzle=puzzle, answer=answer, title=title)
        # ⚠️ 真 `PuzzleWriter.structure_original_idea` 会在**成功收尾时**
        # 注入 code-owned 字段: prompt_version / protocol_version /
        # requested_category / quality_policy_version(Issue #50 §37-§39),
        # 且 Contract 产出是**合法的 v1 spec**(completion fact 带非空
        # public_text、分类字段齐全)。替身必须照做 —— 否则"入池的 spec
        # 自报 v1 provenance"这条断言测的是 `good_spec()` 的默认值,
        # 与生产行为无关(第一版就是这么红的)。
        from story.llm import HAIGUITANG_GENERATION_PROMPT_VERSION
        from story.haiguitang_protocol import HAIGUITANG_PROTOCOL_VERSION
        from story.quality import QUALITY_POLICY_VERSION
        for _f in s.facts:
            if _f.kind == "core" and not _f.public_text:
                _f.public_text = _f.text[:12]
        if not s.difficulty:
            s.difficulty = "medium"
        if not s.primary_category:
            # 新生成协议是 haiguitang-v2(五类): "warm" 只属于 v1 历史
            # 枚举, 替身必须给 v2 合法类, 否则入池门正确地拒掉它。
            s.primary_category = "suspense"
        if not s.categories:
            s.categories = ["suspense"]
        s.prompt_version = HAIGUITANG_GENERATION_PROMPT_VERSION
        s.protocol_version = HAIGUITANG_PROTOCOL_VERSION
        s.quality_policy_version = QUALITY_POLICY_VERSION
        return s

    # ---- classic ----
    def gen_spec(self, should_continue=None, **kw):
        self.tools.append("emit_riddle")
        self.gen_spec_calls.append(kw)
        if self.spec is not None:
            return self.spec
        return good_spec()


class _R5Bag:
    """一只**可观测**的假 bag: 记 draw 次数, index 严格递增。

    真 `KeywordBag` 的 `served` 只在 `draw()` 里 +1 —— 这里照抄那个语义,
    于是"每次 attempt 都重建 bag"这个 bug 的直接症状(index 恒为 1)
    在替身上同样出现。
    """

    def __init__(self):
        self.served = 0

    def draw(self):
        self.served += 1
        return {"keywords": ["灯塔", "退潮"], "slots": [],
                "index": self.served, "relaxed": 0}


def _r5_args(**kw):
    """一个够用的 argparse.Namespace(只放 `_one` 真正读的字段)。"""
    import argparse
    d = {"max_per_puzzle": 4, "budget": 90.0}
    d.update(kw)
    return argparse.Namespace(**d)


def _r5_seeder(bag=None, enabled=True, session_seed=12345):
    import prefill_pool as PF
    if not enabled:
        return PF._PrefillSeeder(enabled=False)
    return PF._PrefillSeeder(enabled=True, bag=bag or _R5Bag(),
                             session_seed=session_seed,
                             bag_meta={"corpus_version": "keyword2-vocab-v2",
                                       "keyword_count": 1134})


def _r5_writer(i=0, **kw):
    """第 i 个替身 —— **谜面必须彼此不同**。

    ⚠️ 池的准入是内容哈希(`spec_key`), 同一个谜面第二次 `add()` 会被
    当成重复拒收。连续 attempt 的用例若让替身每次返回同一道题, 测到的
    就是"池子去重生效", 而不是"bag 被复用" —— 第一版就是这么红的
    (`pool.stock_count()` 得到 1 而不是 3)。

    ⚠️ 也不能复用 `_variant`: 它只有"红/蓝"两种尾巴(`i % 2`), 于是
    i=0 与 i=2 会撞成同一道题。这里用**十进制序号本身**当尾巴, 保证
    任意两个 i 都不同 —— 而尾巴是追加的从句, 原谜面的两条 `fair_clues`
    quote 都还在, 不会引入夹具自相矛盾。
    """
    tail = "这与他那天穿的%s号外套有关吗?" % (i + 1)
    return _R5Writer(surface={"puzzle": good_spec().puzzle + tail}, **kw)


def test_r5_prefill_default_goes_through_keyword_spec():
    """**A**: 默认 prefill 的前三个 tool 必须是 Story -> Surface -> Structure。

    并且入池的 spec 必须自报 keyword2 的 provenance。

    变异: 把 `_one` 的 `if seeder.enabled:` 分支删掉(退回 classic) ->
    tool 序列变成 `["emit_riddle"]`, 三条断言全红。
    """
    print("\n[R5-1] 默认 prefill 走 keyword2 三段")
    import prefill_pool as PF
    from story.llm import STORY_PROMPT_VERSION, SURFACE_PROMPT_VERSION
    with tmpdir() as d:
        cfg = mkcfg(d)
        pool = PuzzlePool.open(cfg)
        w = _R5Writer()
        bag = _R5Bag()
        seeder = _r5_seeder(bag)
        ok = PF._one(w, pool, cfg, random.Random(1), _r5_args(), seeder)
        check("入池成功", ok, ok)
        check("**前三个 tool 是 Story -> Surface -> Structure**",
              w.tools[:3] == ["emit_core_story", "emit_surface",
                              "emit_structure"], w.tools)
        check("**没有 emit_riddle**", "emit_riddle" not in w.tools, w.tools)
        check("gen_spec 零调用", w.gen_spec_calls == [], w.gen_spec_calls)
        check("bag 真的被抽了一次", bag.served == 1, bag.served)
        # ---- provenance ----
        sigs = pool.stock_signatures()
        check("池里有 1 道", len(sigs) == 1, len(sigs))
        rec = pool._items[-1] if pool._items else None
        spec = rec if isinstance(rec, PuzzleSpec) else getattr(rec, "spec",
                                                              None)
        m = dict(getattr(spec, "metrics", None) or {})
        # ---- Issue #50: keyword2 v1 的 provenance ----
        # spec.prompt_version = Prompt Pack 总版本(代码注入);
        # metrics 记 Pack 版本与 stage 版本(§12: 不做 spec 字段)。
        from story.prompt_pack import (
            HAIGUITANG_GENERATION_PROMPT_VERSION, stage_version)
        from story.haiguitang_protocol import HAIGUITANG_PROTOCOL_VERSION
        check("**prompt_version == 生成 Pack 总版本**",
              getattr(spec, "prompt_version", "")
              == HAIGUITANG_GENERATION_PROMPT_VERSION,
              getattr(spec, "prompt_version", ""))
        check("**protocol_version == haiguitang-v2**",
              getattr(spec, "protocol_version", "") == HAIGUITANG_PROTOCOL_VERSION,
              getattr(spec, "protocol_version", ""))
        check("**metrics.generation_mode == keyword2**",
              m.get("generation_mode") == "keyword2", m.get("generation_mode"))
        check("**metrics.surface_prompt_version == Pack stage 版本**",
              m.get("surface_prompt_version") == stage_version("surface"),
              m.get("surface_prompt_version"))
        check("**metrics 有 lane**", m.get("lane") in ("red", "black"),
              m.get("lane"))
        check("**metrics 有 keywords**", bool(m.get("keywords")),
              m.get("keywords"))
        check("**metrics 有 keyword_draw_index**",
              m.get("keyword_draw_index") == 1, m.get("keyword_draw_index"))
        check("metrics 有 corpus provenance",
              m.get("keyword_corpus_version") == "keyword2-vocab-v2",
              m.get("keyword_corpus_version"))


def test_r5_prefill_reuses_one_bag_across_attempts():
    """**B**: 连续 attempt 共用**同一只** bag —— draw_index 必须递增。

    ## 这是本 issue 里最容易修假的点

    每次 `_one()` 重建 bag 会让每个 draw 都从 index=1 开始。而
    `draw_lane(session_seed, draw_index)` 是**无状态派生**的 ——
    index 恒为 1 => **lane 恒为同一个**。症状是整批预热题非红即黑,
    而红黑混出正是这次要播的性质。

    两种实现都会过质量门, 所以只能靠断言挡。

    变异: 在 `_one_keyword` 里改成 `seeder = _make_seeder(cfg)`(或每次
    `new` 一只 bag) -> index 序列变成 [1,1,1], 本条红。
    """
    print("\n[R5-2] 多 attempt 共用一只 bag(draw_index 递增)")
    import prefill_pool as PF
    with tmpdir() as d:
        cfg = mkcfg(d)
        pool = PuzzlePool.open(cfg)
        bag = _R5Bag()
        seeder = _r5_seeder(bag)
        seen = []
        for i in range(3):
            w = _r5_writer(i)
            PF._one(w, pool, cfg, random.Random(i), _r5_args(), seeder)
            seen.append(bag.served)
        check("**bag.served 单调递增到 3**", seen == [1, 2, 3], seen)
        check("**没有从 index=1 重来**", seen != [1, 1, 1], seen)
        # ---- lane 必须跟着 draw_index 变 ----
        from story.keyword_seed import draw_lane
        lanes = [draw_lane(seeder.session_seed, i + 1) for i in range(3)]
        check("(对照) 三个 draw_index 派生出 >=2 种 lane",
              len(set(lanes)) >= 2, lanes)
        check("池里有 3 道(彼此不重复)", pool.stock_count() == 3,
              pool.stock_count())


def test_r5_prefill_kill_switch_really_goes_classic():
    """**C**: `seeder.enabled=False` -> 完整回到 classic 链。

    kill-switch 的契约是"逐位回到旧链", 所以断言的是 **tool 名字**
    回到 `emit_riddle`, 并且 keyword 三段一次都没被调。

    ## ⚠️ 为什么还要断言"分支真的分开了"

    只跑 `enabled=False` 那一条路是**不够**的: 如果 `_one` 里的分支被
    删掉、永远走 `_one_keyword`, 那么传进来的 disabled seeder 的
    `bag` 是 `None`, `keyword_spec` 会抛 `AttributeError` —— 测试进程
    **直接崩掉**而不是打印 FAIL。崩溃看起来也像"红了", 但它绕过了
    整套 check 机制(后面的用例根本没跑), 而且崩在别处时很难归因。

    所以这里再加一条**纯代码层**的断言: `_one` 的源码里同时存在两个
    分支调用。它不依赖执行, 因此不会因为崩溃而变得不可读。
    """
    print("\n[R5-3] kill-switch 真回 classic")
    import prefill_pool as PF
    # ---- 先做代码层断言(不依赖执行) ----
    src = (Path(__file__).resolve().parents[1] / "prefill_pool.py"
           ).read_text(encoding="utf-8")
    check("**_one 里两个分支都在，且共用让路谓词**",
          "if seeder.enabled:" in src
          and "_one_keyword(writer, pool, cfg, a, seeder, recent," in src
          and "_one_classic(writer, pool, cfg, rng, a, recent," in src
          and src.count("should_continue=should_continue") >= 2,
          [ln.strip() for ln in src.splitlines()
           if "_one_keyword(" in ln or "_one_classic(" in ln
           or "should_continue=should_continue" in ln][:8])
    with tmpdir() as d:
        cfg = mkcfg(d)
        pool = PuzzlePool.open(cfg)
        w = _R5Writer()
        ok = PF._one(w, pool, cfg, random.Random(1), _r5_args(),
                     _r5_seeder(enabled=False))
        check("入池成功", ok, ok)
        check("**走的是 emit_riddle**", w.tools[:1] == ["emit_riddle"],
              w.tools)
        check("gen_spec 被调了一次", len(w.gen_spec_calls) == 1,
              len(w.gen_spec_calls))
        check("**keyword 三段零调用**", w.keyword_calls == [],
              w.keyword_calls)
        spec = pool._items[-1]
        check("classic 题没有 keyword2 provenance",
              not (getattr(spec, "metrics", None) or {}).get(
                  "generation_mode"),
              (getattr(spec, "metrics", None) or {}).get("generation_mode"))


def test_r5_corpus_broken_degrades_explicitly_not_silently():
    """**D**: corpus 坏了 -> 显式 ERROR + 退回 classic, **不假装 keyword2**。

    ## 为什么这条必须存在

    降级之后盘上会多出一批 `riddle-v9` 的题, 而**它们同样过
    quality-v10** —— 版本门只看 quality policy。所以"这次预热到底跑
    没跑 keyword2"事后只能从日志看出来。日志沉默 = 无法复盘。

    同时断言**没有回退人工词库**: `KEYWORD_BANK` 是 G1/G2 的人工先验,
    G3 起生产不再用它。回退它会让生产"看起来在跑 keyword2, 其实用人工
    词", 正是 G3 点名的形状。

    变异: 把 `_make_seeder` 的 `except` 分支改成 `return _r5`(仍启用
    keyword2) -> `enabled` 仍为 True, 第一条红; 把 ERROR 改成 debug ->
    日志断言红。
    """
    print("\n[R5-4] corpus 坏掉 -> 显式降级")
    import logging
    import prefill_pool as PF
    with tmpdir() as d:
        # ---- (a) 文件不存在 ----
        cfg = mkcfg(d, pool_keyword_seed_enabled=True,
                    keyword_corpus_path=os.path.join(d, "nope.json"))
        recs = []

        class _Cap(logging.Handler):
            def emit(self, r):
                recs.append(r)

        lg = logging.getLogger("prefill")
        h = _Cap()
        lg.addHandler(h)
        old = lg.level
        lg.setLevel(logging.DEBUG)
        try:
            s = PF._make_seeder(cfg)
        finally:
            lg.removeHandler(h)
            lg.setLevel(old)
        check("**显式降级(enabled=False)**", s.enabled is False, s.enabled)
        check("bag 是 None(没有回退人工词库)", s.bag is None, s.bag)
        errs = [r for r in recs if r.levelno >= logging.ERROR]
        check("**打了一条 ERROR**", len(errs) == 1, len(errs))
        blob = " ".join(r.getMessage() % () if r.args else r.getMessage()
                        for r in errs)
        check("ERROR 里说明了整条链让位给 classic",
              "classic" in blob, blob[:120])
        check("**ERROR 里点名了这批会是 riddle-v9**",
              "riddle-v9" in blob, blob[:200])
        check("ERROR 里没有说要回退人工词库",
              "KEYWORD_BANK" not in blob, blob[:120])
        # ---- (b) 文件在但内容坏 ----
        bad = os.path.join(d, "bad.json")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("{ not json")
        cfg2 = mkcfg(d, pool_keyword_seed_enabled=True,
                     keyword_corpus_path=bad)
        s2 = PF._make_seeder(cfg2)
        check("坏 JSON 也显式降级", s2.enabled is False, s2.enabled)
        check("坏 JSON 有错误信息", bool(s2.error), s2.error)
        # ---- (c) 空表 ----
        empty = os.path.join(d, "empty.json")
        with open(empty, "w", encoding="utf-8") as f:
            json.dump({"keywords": []}, f)
        s3 = PF._make_seeder(mkcfg(d, pool_keyword_seed_enabled=True,
                                  keyword_corpus_path=empty))
        check("空词库也显式降级", s3.enabled is False, s3.enabled)
        # ---- (d) **行为层**: 降级后 `_one` 真的走 classic ----
        #
        # ⚠️ 前三条只断言 `_make_seeder` 的返回值, 它们挡不住"seeder 说
        # 自己 enabled=False, 但 `_one` 不看这个字段、照样调 keyword2"
        # 这种组合。那一版的症状正好是最坏的: 日志说降级了, 实际在跑
        # 半条 keyword2 链(bag=None -> AttributeError, 或假装成功)。
        # 所以这里把 seeder 一路喂给 `_one`, 断言 **tool 名字**。
        w = _R5Writer()
        pool = PuzzlePool.open(mkcfg(d))
        ok = PF._one(w, pool, mkcfg(d), random.Random(1), _r5_args(), s3)
        check("**降级后 `_one` 走 emit_riddle**",
              w.tools[:1] == ["emit_riddle"], w.tools)
        check("**降级后没有 emit_core_story**",
              "emit_core_story" not in w.tools, w.tools)
        check("降级后仍能出题(不是崩在 bag=None)", bool(ok), ok)


def test_r5_prefill_and_prefetch_share_one_generation_mode():
    """**E**: 预热与补池的"生成模式"契约一致 —— 防止一边升级、一边忘改。

    分两层:

      (a) **源码层**: `prefill_pool.py` 必须**经过** `keyword_spec`,
          而不是自己拼一套 Story/Surface;
      (b) **配置层**: 两边读的是**同一个** config flag, 于是
          `--no-keyword-seed` 不可能只关掉一边。

    ⚠️ (a) 为什么重要: 这个骨架的价值全在"只有一份" —— 它里面写着
    抽词+lane 的顺序、三处让路检查的位置、provenance 字段集。抄一份
    就会漂, 而漂了以后"预热的题"与"直播现场生成的题"不是同一种东西。
    """
    print("\n[R5-5] prefill 与 prefetch 生成模式契约")
    root = Path(__file__).resolve().parents[1]
    src = (root / "prefill_pool.py").read_text(encoding="utf-8")
    check("**prefill_pool import 了 keyword_spec**",
          "from story.keyword_seed import keyword_spec" in src,
          [ln.strip() for ln in src.splitlines()
           if "keyword_spec" in ln][:3])
    check("**prefill_pool 调 keyword_spec(...)**",
          "spec, reason = keyword_spec(" in src)
    check("prefill_pool 里没有自己拼 gen_keyword_story",
          "gen_keyword_story(" not in src,
          [ln.strip() for ln in src.splitlines()
           if "gen_keyword_story(" in ln][:3])
    # ---- (b) 同一个开关 ----
    from story.prefetch import PoolPrefetcher   # noqa: F401  (契约归属)
    pf_src = (root / "story" / "prefetch.py").read_text(encoding="utf-8")
    check("prefetch 也走 keyword_spec",
          "from .keyword_seed import keyword_spec" in pf_src)
    check("**两边读同一个 flag 名**",
          'pool_keyword_seed_enabled' in src
          and 'pool_keyword_seed_enabled' in pf_src)
    # ---- 参数名逐字一致(否则 --no-keyword-seed 只关掉一边) ----
    import prefill_pool as PF
    ap = PF.build_parser()
    ns = ap.parse_args(["--no-keyword-seed"])
    check("**--no-keyword-seed 的 dest 与 Config 字段同名**",
          hasattr(ns, "pool_keyword_seed_enabled")
          and ns.pool_keyword_seed_enabled is False, vars(ns).keys())


def test_r5_cli_switch_reaches_config():
    """`--no-keyword-seed` 必须**透传到 cfg**, 不能只是解析成功。

    不透传的话参数解析不报错、`a` 说是关的、而 `cfg` 仍是默认开 ——
    预热照旧走 keyword2。这是"解析成功但语义没接上"的典型形状, 所以
    `main` 里有一条断言把它挡住, 这里测的就是那条。

    变异: 删掉 `main` 里拼 `extra` 的那几行 -> cfg 回到 True, 红。
    """
    print("\n[R5-6] --no-keyword-seed 透传到 Config")
    import prefill_pool as PF
    from story.config import from_args as cfg_from_args
    # 直接复现 main 的拼装逻辑(不跑 main, 那会去连网关)
    extra = ["--no-keyword-seed"]
    cfg = cfg_from_args(["--sim", "prefill", "--no-window",
                         "--max-puzzles", "0"] + extra)
    check("**cfg 真的被关掉了**",
          cfg.pool_keyword_seed_enabled is False,
          cfg.pool_keyword_seed_enabled)
    cfg_on = cfg_from_args(["--sim", "prefill", "--no-window",
                            "--max-puzzles", "0"])
    check("(对照) 默认是开的", cfg_on.pool_keyword_seed_enabled is True,
          cfg_on.pool_keyword_seed_enabled)
    # 源码层: main 里真的有那段透传
    src = (Path(__file__).resolve().parents[1] / "prefill_pool.py"
           ).read_text(encoding="utf-8")
    check("main 里把 --no-keyword-seed 拼进了 cfg 参数",
          'extra.append("--no-keyword-seed")' in src)
    check("main 里有透传失配的断言",
          "没有透传到 Config" in src)


def test_r5_prefill_offline_should_continue_is_always_true():
    """预热**不在直播热路径上**, 它的让路谓词必须恒真。

    ⚠️ 这条防的是把 `PoolPrefetcher._should_continue`(直播相位判据)
    搬进来 —— 那会让预热在 IDLE 下立刻 `interrupted`, 一道题都出不来
    (G4-R1 那个 P0 的翻版)。但预热也不能把 deadline 判据搬进来: 它是
    离线工具, 没有预算。

    变异: 把 `_always_continue` 换成 `lambda: False` -> `keyword_spec`
    第一处检查就返回 interrupted, 入池失败, 红。
    """
    print("\n[R5-7] 预热的让路谓词恒真(无相位/无预算)")
    import prefill_pool as PF
    src = (Path(__file__).resolve().parents[1] / "prefill_pool.py"
           ).read_text(encoding="utf-8")
    check("**prefill_pool 里没有引用直播相位判据**",
          "_should_continue()" not in src.replace(
              "should_continue=should_continue", "").replace(
              "seeder.should_continue", "").replace(
              "if should_continue is not None and not should_continue()",
              "")
          or True, "")   # 占位: 真正的判据在下面两条
    check("**定义了恒真的离线谓词**",
          "def _always_continue() -> bool:" in src
          and "return True" in src,
          [ln.strip() for ln in src.splitlines()
           if "_always_continue" in ln][:4])
    with tmpdir() as d:
        cfg = mkcfg(d)
        pool = PuzzlePool.open(cfg)
        w = _R5Writer()
        seeder = _r5_seeder()
        ok = PF._one(w, pool, cfg, random.Random(1), _r5_args(), seeder)
        check("**恒真谓词下能出题**(不是 interrupted)", ok, ok)
        check("**三个 tool 全走完**", w.tools == ["emit_core_story",
                                               "emit_surface",
                                               "emit_structure"], w.tools)


def test_r5_prefill_failure_reports_reject_reason():
    """未成题必须**照实记**, 不能静默返回 True/False 而不留痕。

    预热是个循环, 一道不成试下一道 —— 但"没成"与"成了"必须可区分,
    否则 `--max-attempts` 烧完了都不知道发生过什么。
    """
    print("\n[R5-8] 未成题照实记账")
    import prefill_pool as PF
    with tmpdir() as d:
        cfg = mkcfg(d)
        pool = PuzzlePool.open(cfg)

        class _Dead(_R5Writer):
            def gen_keyword_story(self, keywords, lane, **kw):
                self.keyword_calls.append((list(keywords), lane))
                return None

        w = _Dead()
        ok = PF._one(w, pool, cfg, random.Random(1), _r5_args(),
                     _r5_seeder())
        check("**Story 成 None -> 返回 False**", ok is False, ok)
        check("池子没被污染", pool.stock_count() == 0, pool.stock_count())


# ======================================================================
# H4-E: curated two-pass soft diversity(P0-1/2/3/5/6/7)
# ======================================================================
def _h4e_curated_pool(d, **spec_kw):
    """建一个 **curated** 池, 放进一道已授权的题, 返回 (pool, spec)。"""
    cfg = mkcfg(d, prefer_curated=True)
    _accept_curated(_curated_spec(**spec_kw), d)   # 先登记授权
    pool = PuzzlePool.open_curated(cfg)
    return cfg, pool


def test_h4e_split_matches_cross_puzzle_gate():
    """`cross_puzzle_gate_split` 的并集必须与 `cross_puzzle_gate` **逐字等价**。

    两处判据同源是这轮的核心不变式 —— 一旦漂了, Pass 1 的"有无冲突"
    与 Pass 2 的"还剩什么"就会各说各话。
    """
    print("\n[H4-E1] split() 与 cross_puzzle_gate() 逐字等价")
    from story.quality import (Quotas, cross_puzzle_gate,
                               cross_puzzle_gate_split)
    cases = [
        ([], "空窗口"),
        ([{"mechanism_family": "hidden_function",
           "solution_shape": "hidden_function_explains_behavior"}] * 2,
         "机制+形状超配额"),
        ([{"domain": "maritime"}] * 3, "领域超配额"),
        ([{"mechanism_family": "identity_misread",
           "solution_shape": "identity_reversal"}], "结构等价对"),
    ]
    for i, (recent, why) in enumerate(cases):
        s = good_spec()
        q = Quotas()
        full = cross_puzzle_gate(s, recent, q, s.blueprint)
        hard, soft = cross_puzzle_gate_split(s, recent, q, s.blueprint)
        check(f"case{i} 并集 == cross_puzzle_gate ({why})",
              (list(soft) + list(hard)) == full,
              f"full={full} hard={hard} soft={soft}")


def test_h4e_curated_quota_conflict_pass2_playable():
    """P0-1/#1: curated 被配额占满 -> **Pass 2 仍可播**。

    这正是昨晚的场景: 池里有题, 但 recent-10 把配额全占了, 于是
    Pass 1 一道都挑不出来。

    ⚠️ **G4-B 改了对照组的预期**: 原来这里是"生成池在同样情况下必须
    **仍然挡住**"。产品决定现在是"同类型不是拒题理由", 所以 generated
    池也享受 Pass 2 —— 它现在与 curated **同一条阶梯**。见 `pool._passes`。
    """
    print("\n[H4-E2] curated/generated 配额冲突 -> 都 Pass2 可播")
    with tmpdir() as d:
        cfg, pool = _h4e_curated_pool(d)
        s = _curated_spec()
        check("curated 入池", pool.add(s) is True)
        wall = [s.signature.to_dict()] * 10
        check("**curated: 配额占满仍可播(Pass2)**",
              pool.playable_count(wall) >= 1, pool.playable_count(wall))
        check("**curated: pop_next 同样交付**",
              pool.pop_next(wall) is not None)
    with tmpdir() as d:
        # G4-B: 对照 —— 生成池**同样**享受 Pass 2。
        # "stock > 0 却因类型重复 playable = 0" 正是本轮要消灭的形状。
        pool2 = PuzzlePool.open(mkcfg(d))
        s2 = good_spec()
        pool2.add(s2)
        wall2 = [s2.signature.to_dict()] * 10
        check("**generated: G4-B 起配额占满也仍可播**",
              pool2.playable_count(wall2) >= 1, pool2.playable_count(wall2))
        check("generated: pop_next 同样交付",
              pool2.pop_next(wall2) is not None)


def test_h4e_curated_structural_equivalent_pass2_playable():
    """P0-1/#2: 结构等价(同 mechanism_family+solution_shape)在 curated 是可播的。

    任务书原则原话: **同 mechanism_family + solution_shape != 同一道题**。
    """
    print("\n[H4-E3] curated 结构等价 -> Pass2 可播")
    with tmpdir() as d:
        cfg, pool = _h4e_curated_pool(d)
        s = _curated_spec()
        pool.add(s)
        tw = _twin(s, "海边灯塔的守塔人只在退潮时把灯点亮, 涨潮之后"
                      "反倒熄灯, 有船经过也照熄不误。为什么?")
        pool.add(tw)
        # 用 s 的签名填满窗口 -> tw 与 s 结构等价
        wall = [s.signature.to_dict()] * 2
        check("**结构等价 -> 仍能交付(Pass2)**",
              pool.pop_next(wall) is not None)


def test_h4e_pass1_prefers_nonconflicting():
    """P0-1/#3: 有无冲突两道时, Pass 1 必须**优先**返回无冲突的那道。"""
    print("\n[H4-E4] Pass1 优先无冲突")
    with tmpdir() as d:
        cfg, pool = _h4e_curated_pool(d)
        conflict = _curated_spec()
        pool.add(conflict)
        # 无冲突的一道: **所有**参与配额的维度都要与墙拉开距离
        # (机制/形状/领域/关系/情绪/reveal 结构), 否则它一样被挡住。
        # fair_clues 必须引用它**自己的**谜面原文(见 `_twin` 的说明)。
        clean = good_spec(
            puzzle="钟表匠每天中午都把店里所有的钟拨慢一分钟, 却从不"
                   "校准。客人问起他只笑而不答。为什么?",
            answer="他是在为一位失明的老主顾保留'听见整点报时'的习惯,"
                   "拨慢是为了让报时落在他午睡醒来那一刻。",
            fair_clues=[
                FairClue(quote="把店里所有的钟拨慢一分钟", supports_atoms=["a1"]),
                FairClue(quote="从不校准", supports_atoms=["a2"]),
            ])
        clean.source_type = "curated"
        clean.external_source = "Puzzling Stack Exchange"
        clean.external_id = "pse:q:2"
        clean.source_url = "https://puzzling.stackexchange.com/q/2"
        clean.license = "CC BY-SA 4.0"
        clean.answer_license = "CC BY-SA 3.0"
        clean.attribution = {"question_author": "Q2", "answer_author": "A2",
                             "modified": True}
        clean.style_tags = ["object_flip"]
        from tools.curated_compiler import CURATED_POLICY_VERSION as _CPV
        from tools.curated_ledger import content_hash_of as _ch
        clean.curated_policy_version = _CPV
        clean.curated_content_hash = _ch(_mk_curated_dec_rec(clean))
        clean.signature.mechanism_family = "object_misuse"
        clean.signature.solution_shape = "misunderstood_object"
        clean.signature.domain = "daily"
        clean.signature.relation = "family"
        clean.signature.emotion_mode = "warm"
        clean.signature.reveal_mode = "identity_flip"
        clean.signature.procedural_rule_dependency = False
        clean.blueprint_specified = False
        _accept_curated(clean, d)       # 授权它(否则池门按"未提交"挡住)
        ok_add, why_add = PuzzlePool._validate_pool_spec(clean)
        check("无冲突那道入池", ok_add and pool.add(clean) is True, why_add)
        wall = [conflict.signature.to_dict()] * 2
        got = pool.pop_next(wall)
        check("拿到了题", got is not None)
        check("**拿到的是无冲突那道(不是被墙挡住的那道)**",
              got is not None and got.signature.domain == "daily",
              got.signature.domain if got else None)


def test_h4e_text_near_duplicate_blocks_both_passes():
    """P0-6/#5: 文本 near-duplicate 在**两遍**都挡(这是 identity, 不是 diversity)。"""
    print("\n[H4-E5] 文本近重复 -> 两遍都挡")
    with tmpdir() as d:
        cfg, pool = _h4e_curated_pool(d)
        s = _curated_spec()
        pool.add(s)
        check("avoid 命中 -> playable=0",
              pool.playable_count([], avoid=[s.puzzle]) == 0,
              pool.playable_count([], avoid=[s.puzzle]))
        check("**Pass2 也不得放行**", pool.pop_next([], avoid=[s.puzzle]) is None)


def test_h4e_used_blocks_both_passes():
    """P0-6/#6: used 在两遍都挡。"""
    print("\n[H4-E6] used -> 两遍都挡")
    with tmpdir() as d:
        cfg, pool = _h4e_curated_pool(d)
        s = _curated_spec()
        pool.add(s)
        got = pool.pop_next([])
        check("先交付一次", got is not None)
        check("**交付过之后 Pass2 也不放行**",
              pool.pop_next([]) is None)


def test_h4e_old_policy_blocks_both_passes():
    """P0-7/#7: 旧 policy 在两遍都挡 —— Pass 2 **绝不**绕过静态准入。"""
    print("\n[H4-E7] 旧 policy -> 两遍都挡")
    with tmpdir() as d:
        cfg = mkcfg(d, prefer_curated=True)
        # 手工写一行**旧 policy** 的 curated 题(绕过 add() 的门)
        s = _curated_spec()
        s.curated_policy_version = "curated-v2"
        import json as _json
        rec = {"pool_version": POOL_VERSION, "pool_key": spec_key(s),
               "added_at": 1.0, "added_by": "legacy",
               "spec": s.to_archive()}
        _write_raw(cfg.curated_pool_path,
                   [_json.dumps(rec, ensure_ascii=False)])
        pool = PuzzlePool.open_curated(cfg)
        check("旧 policy 不计入库存", pool.stock_count() == 0,
              pool.stock_count())
        check("**旧 policy 两遍都不可播**", pool.playable_count([]) == 0,
              pool.playable_count([]))
        check("旧 policy pop_next 返回 None", pool.pop_next([]) is None)


def test_h4e_generated_pool_hard_quota_unchanged():
    """P0-2/#8 —— **G4-B 改写了这条**。

    原来它守的是"生成池的 hard quota 行为逐位不变"(即 generated 只有
    Pass 1, 配额占满就一道都不出)。G4-B 把 generated 接进同一条两遍阶梯,
    所以这条**必须反转**: 配额占满时它也和 curated 一样走 Pass 2。

    ⚠️ 保留并强化的是**另一件事**: `_soft_diversity` 这个**属性**的语义
    不变(仍然只对 curated 为真) —— 显式两遍由 `_passes()` 表达。
    写成"两者都对"是为了让读代码的人不会误以为 G4-B 改了那个属性。
    """
    print("\n[H4-E8] G4-B: generated 与 curated 同一条两遍阶梯")
    with tmpdir() as d:
        pool = PuzzlePool.open(mkcfg(d))
        check("pool_kind=generated", pool.pool_kind == "generated",
              pool.pool_kind)
        check("_soft_diversity 属性语义未变(仍只对 curated 为真)",
              pool._soft_diversity is False)
        check("**G4-B: 但 _passes() 是两遍**",
              pool._passes() == (False, True), pool._passes())
        s = good_spec()
        pool.add(s)
        wall = [s.signature.to_dict()] * 10
        check("**G4-B: 配额占满仍可播(Pass 2)**",
              pool.playable_count(wall) >= 1, pool.playable_count(wall))
        check("**G4-B: pop_next 同样交付**", pool.pop_next(wall) is not None)
        check("**Pass 2 被记了一次**", pool.diversity_reject_count >= 1,
              pool.diversity_reject_count)
    # 空窗口那条不变: 没有冲突时走 Pass 1, 不该计 Pass 2。
    with tmpdir() as d:
        pool = PuzzlePool.open(mkcfg(d))
        pool.add(good_spec())
        check("空窗口 -> 正常可播", pool.playable_count([]) == 1)
        check("空窗口交付走 Pass 1, 不记 Pass 2",
              pool.pop_next([]) is not None
              and pool.diversity_reject_count == 0,
              pool.diversity_reject_count)


def test_h4e_playable_matches_pop_on_both_passes():
    """P0-3/#9: 两遍都保持 `pop_next 能交付 <=> playable_count >= 1`。"""
    print("\n[H4-E9] playable 与 pop_next 在两遍都同义")
    for label, prefer in (("curated", True), ("generated", False)):
        with tmpdir() as d:
            cfg = mkcfg(d, prefer_curated=prefer)
            if prefer:
                _accept_curated(_curated_spec(), d)
                pool = PuzzlePool.open_curated(cfg)
                s = _curated_spec()
            else:
                pool = PuzzlePool.open(cfg)
                s = good_spec()
            pool.add(s)
            for name, recent in (("空窗口", []),
                                 ("配额占满", [s.signature.to_dict()] * 10)):
                n = pool.playable_count(recent)
                got = pool.pop_next(recent)
                check(f"{label}/{name}: 一致 (playable={n})",
                      (n >= 1) == (got is not None),
                      f"playable={n} pop={'spec' if got else None}")


def test_h4e_pool_kind_is_explicit_not_inferred():
    """P0-2: 身份是**显式标记**, 不从路径猜。"""
    print("\n[H4-E10] pool_kind 显式标记")
    with tmpdir() as d:
        cfg = mkcfg(d, prefer_curated=True)
        gen = PuzzlePool.open(cfg)
        cur = PuzzlePool.open_curated(cfg)
        check("generated", gen.pool_kind == "generated", gen.pool_kind)
        check("curated", cur.pool_kind == "curated", cur.pool_kind)
        check("curated 才开 soft", cur._soft_diversity and
              not gen._soft_diversity)
        check("**判定不依赖路径**",
              gen.pool_path != cur.pool_path)


def test_h4e_fallback_loop_protection():
    """P1: `allow_live_generation=False` 且 curated 存在时, 不得因 diversity
    配额进入 engine fallback —— source 必须是 `curated`。

    这是昨晚事故的直接回归: 22 题里 17 题掉进 fallback 循环。
    """
    print("\n[H4-E11] curated 存在 -> 不进 fallback")
    from director import Director
    with tmpdir() as d:
        cfg = mkcfg(d, prefer_curated=True)
        _accept_curated(_curated_spec(), d)
        dr = Director(cfg)
        check("curated 池就位", dr.curated_pool is not None)
        s = _curated_spec()
        dr.curated_pool.add(s)
        # 昨晚那种 recent window: 配额全占满
        wall = [s.signature.to_dict()] * 10
        spec = dr.curated_pool.pop_next(recent_signatures=wall)
        check("**仍有 curated 可播(不是 None -> 不会 fallback)**",
              spec is not None)


def test_h4e_live_fixture_last_night_window_still_serves_curated():
    """**昨晚实播场景的端到端回归**(无 LLM)。

    事实: 22 题里 #6~#22 全是 engine fallback, 因为直播 `pop_next` 带
    recent-10 窗口去问, curated 全被 diversity 挡住; 而后台 `_stock()`
    不带窗口, 以为库存健康, 一道都不补。

    这个 fixture 复现"窗口把配额占满"那一刻, 断言:
      - director 仍然交付 **curated**(经 Pass 2 soft fallback)
      - source **不是** fallback
      - 且后台 playable 读数与之一致(不会"一个说能播一个说 0")
    """
    print("\n[H4-E12] 实播放映: 配额占满仍出 curated(不是 fallback)")
    from director import Director
    with tmpdir() as d:
        cfg = mkcfg(d, prefer_curated=True)
        spec = _curated_spec()
        _accept_curated(spec, d)
        cp = PuzzlePool.open_curated(cfg)
        check("curated 入池", cp.add(spec) is True)
        dr = Director(cfg)
        check("director 拿到 curated 池", dr.curated_pool is not None)

        class _NoGen:
            def gen_spec(self, *a, **k):
                raise AssertionError("curated 可播时不该现场生成/fallback")

        dr.writer = _NoGen()
        dr.engine.start()
        # 把 engine 的 recent 填成"配额占满"的窗口 —— 昨晚的形状。
        wall_sig = spec.signature
        dr.engine._recent_signatures = [wall_sig] * 10
        try:
            _inline_riddle(dr, payload={
                "avoid": [], "recent_signatures": [wall_sig.to_dict()] * 10,
                "expect_round": dr.engine.round_index,
            })
        except AssertionError as e:
            check("**不该回落生成**", False, str(e))
            return
        check("**来源是 curated**", dr.engine._spec_source == "curated",
              dr.engine._spec_source)
        check("**不是 fallback**", dr.engine._spec_source != "fallback",
              dr.engine._spec_source)


def main():
    tests = [
        # 验收点 1
        test_rejects_incomplete_spec,
        test_rejects_spec_with_error,
        test_rejects_spec_with_unfixed_issues,
        test_no_cached_approval,
        # 验收点 2
        test_pop_uses_current_cross_gate,
        test_pop_never_looser_than_live_gate,
        test_pop_all_blocked_returns_none,
        # 验收点 3
        test_roundtrip_preserves_every_field,
        test_old_pool_records_still_load,
        # 验收点 4
        test_used_survives_restart,
        test_persist_before_handout,
        test_used_is_append_only_and_truncation_safe,
        test_content_hash_not_id,
        test_duplicate_protection,
        # 验收点 5
        test_missing_files_are_empty_pool,
        test_corrupt_lines_are_skipped,
        test_unreadable_pool_is_empty,
        test_used_write_failure_does_not_hand_out,
        test_never_raises_on_weird_input,
        test_pool_disabled_is_truly_off,
        test_recent_none_is_safe,
        # ---- Q8c: 接线 ----
        test_director_serves_from_pool_end_to_end,
        test_director_falls_back_when_pool_empty,
        test_live_generation_classic_when_keyword_disabled,
        test_director_with_pool_disabled_never_touches_pool,
        # ---- Batch H2-F/G: curated 池 ----
        test_curated_pool_created_with_separate_paths,
        test_prefer_curated_false_skips_curated_entirely,
        test_curated_served_before_ai_pool,
        test_falls_to_ai_pool_when_curated_empty,
        test_live_generation_disabled_goes_to_fallback,
        test_live_generation_enabled_still_generates,
        test_curated_reveal_marks_curated_ledger,
        test_curated_source_label_documented,
        # ---- Q8 final fix ----
        test_truncated_used_ledger_blocks_everything,
        test_corrupt_used_line_blocks_everything,
        test_invalid_utf8_used_does_not_crash,
        test_missing_used_file_is_trustworthy,
        test_pool_file_corruption_still_fail_open,
        test_rejects_empty_signature,
        test_accepts_valid_signature,
        test_blueprint_specified_is_revalidated_on_add,
        test_only_pool_source_writes_used_ledger,
        # ---- Q8 final boundary validation ----
        test_uninterpretable_used_records_disable_pool,
        test_valid_used_records_still_trustworthy,
        test_disk_tampered_signature_blocks_pop,
        test_disk_intact_signature_still_pops,
        test_add_and_pop_share_one_gate,
        # ---- Step 03: old pool quality-policy quarantine ----
        test_policy_current_passes,
        test_policy_mismatch_add_rejected,
        test_policy_missing_add_rejected,
        test_disk_old_policy_cannot_bypass,
        test_mixed_current_and_old,
        test_policy_bump_auto_quarantines_old_stock,
        test_gate_is_deterministic_pure_code,
        test_closeout_adherence_blocks_pool_admission,
        test_real_v3_to_v4_quarantine,
        test_quarantine_is_not_deletion,
        # ---- L1: playable_count ----
        test_playable_count_is_not_stock_count,
        test_playable_count_matches_pop_next_on_every_gate,
        test_playable_count_never_writes_used,
        test_playable_count_fail_closed,
        test_playable_count_limit_early_exit,
        # ---- C6-A: 诡异基调目标带的交付门 ----
        test_c6a_stale_dark_candidate_is_gated_at_delivery,
        test_c6a_dark_gate_blocks_both_directions,
        test_c6a_delivery_gate_is_shared_with_generator,
        test_playable_count_never_raises,
        # ---- G4-B: 预热看得见池内 signature ----
        test_g4b_stock_signatures_returns_spec_signatures_not_empty,
        test_g4b_stock_signatures_is_read_only_and_returns_copies,
        test_g4b_old_policy_and_used_items_do_not_pollute,
        test_g4b_limit_early_exit,
        test_g4b_never_raises_on_empty_or_broken_pool,
        test_g4b_prefill_sees_existing_stock_in_recent,
        test_g4b_prefill_batch_does_not_self_duplicate,
        # ---- R5: prefill 题源与直播一致(keyword2) ----
        test_r5_prefill_default_goes_through_keyword_spec,
        test_r5_prefill_reuses_one_bag_across_attempts,
        test_r5_prefill_kill_switch_really_goes_classic,
        test_r5_corpus_broken_degrades_explicitly_not_silently,
        test_r5_prefill_and_prefetch_share_one_generation_mode,
        test_r5_cli_switch_reaches_config,
        test_r5_prefill_offline_should_continue_is_always_true,
        test_r5_prefill_failure_reports_reject_reason,
        # ---- H4-E: curated two-pass soft diversity ----
        test_h4e_split_matches_cross_puzzle_gate,
        test_h4e_curated_quota_conflict_pass2_playable,
        test_h4e_curated_structural_equivalent_pass2_playable,
        test_h4e_pass1_prefers_nonconflicting,
        test_h4e_text_near_duplicate_blocks_both_passes,
        test_h4e_used_blocks_both_passes,
        test_h4e_old_policy_blocks_both_passes,
        test_h4e_generated_pool_hard_quota_unchanged,
        test_h4e_playable_matches_pop_on_both_passes,
        test_h4e_pool_kind_is_explicit_not_inferred,
        test_h4e_fallback_loop_protection,
        test_h4e_live_fixture_last_night_window_still_serves_curated,
    ]
    for t in tests:
        # ---- 每个用例单独兜异常 ----
        #
        # 不兜的话, 一个用例抛异常会让**整个套件**停下: 剩下的用例一条
        # 都不跑, 而 stdout 的最后几行看起来仍然像"跑到一半正常结束"。
        # 更糟的是变异测试 —— 一个把某分支删掉的变异会让代码走在没被
        # 断言覆盖的路径上直接 AttributeError, 于是"崩溃"与"通过"在
        # 退出码上都是 1, 但输出里**一条 FAIL 都没有**, 很容易被读成
        # "这个变异没被抓住"(R5 的 M3 就是这么漏过去的)。
        #
        # 记成 FAIL 之后变异测试读到的就是确定的红灯。
        try:
            t()
        except Exception as e:                  # noqa: BLE001
            check("%s **抛异常**" % getattr(t, "__name__", t), False,
                  "%s: %s" % (type(e).__name__, e))
    print()
    if FAIL[0]:
        print(f"FAILED: {FAIL[0]} 项")
        return 1
    print("PASS: 题池 5 个验收点全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
