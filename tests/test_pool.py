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
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from story.config import Config  # noqa: E402
from story.puzzle import (  # noqa: E402
    FairClue, PuzzleBlueprint, PuzzleFact, PuzzleSignature, PuzzleSpec,
    SolveAtom,
)
from story.pool import PuzzlePool, spec_key  # noqa: E402
from story.state import Phase  # noqa: E402

FAIL = [0]


def check(name, cond, extra=""):
    if cond:
        print(f"  ok  {name}")
        return
    print(f"  FAIL {name}  {extra}")
    FAIL[0] += 1


# ----------------------------------------------------------------------
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
        fair_clues=[
            FairClue(quote="只在退潮的那几个小时亮", supports_atoms=["a1"]),
            FairClue(quote="涨潮后他反而把灯熄掉", supports_atoms=["a2"]),
        ],
        hints=["注意灯的开关时机", "想想潮水的变化", "灯是在给谁传递信息?"],
        blueprint=PuzzleBlueprint(
            mechanism_family="hidden_function",
            solution_shape="hidden_function_explains_behavior",
            domain="maritime", relation="stranger",
            emotion_mode="neutral", time_shape="habitual"),
        signature=PuzzleSignature(
            mechanism_family="hidden_function",
            solution_shape="hidden_function_explains_behavior",
            domain="maritime", emotion_mode="neutral",
            relation="stranger", time_shape="habitual"),
        prompt_version="riddle-v3",
        quality_policy_version="quality-v3",
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
    kw.setdefault("no_llm", True)
    return Config(sim_path="x", **kw)


class tmpdir:
    def __enter__(self):
        self._d = tempfile.TemporaryDirectory()
        return self._d.name

    def __exit__(self, *a):
        self._d.cleanup()


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
        # 谜面缺收尾问句 -> validate_spec 给 can_fix(不是 fail)
        s = good_spec(puzzle="灯塔守塔人只在退潮时亮灯, 涨潮后熄掉。")
        s.fair_clues = [FairClue(quote="只在退潮时亮灯", supports_atoms=["a1"])]
        check("缺收尾问句被判 fixable", pool.add(s) is False)


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
    print("\n[2a] 与**当前** recent 窗口冲突的题挑不出来")
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
        # 最近 10 题里有 2 道 hidden_function(到达上限 2) -> 必须被拦
        recent = [{"mechanism_family": "hidden_function",
                   "solution_shape": "hidden_function_explains_behavior"},
                  {"mechanism_family": "hidden_function",
                   "solution_shape": "hidden_function_explains_behavior"}]
        check("与最近分布冲突 -> 挑不出",
              pool2.pop_next(recent_signatures=recent) is None)


def test_pop_never_looser_than_live_gate():
    """池子**不可能**比现场路径更松。

    矩阵: 对同一组 recent, `pop_next` 挑得出  <=>  `cross_puzzle_gate` 为空。
    """
    print("\n[2b] 池子与现场 gate 的判断必须一致")
    from story.quality import Quotas, cross_puzzle_gate
    with tmpdir() as d:
        cases = [
            ([], True),
            ([{"mechanism_family": "hidden_function",
               "solution_shape": "hidden_function_explains_behavior"}] * 2, False),
            ([{"mechanism_family": "identity_misread",
               "solution_shape": "identity_reversal"}] * 1, True),
            ([{"domain": "maritime"}] * 3, False),      # same_domain 上限 3
        ]
        for i, (recent, expect_ok) in enumerate(cases):
            cfg = mkcfg(d, pool_path=os.path.join(d, f"p{i}.jsonl"),
                        pool_used_path=os.path.join(d, f"u{i}.jsonl"))
            pool = PuzzlePool.open(cfg)
            pool.add(good_spec())
            spec = good_spec()
            gate = cross_puzzle_gate(spec, recent, Quotas.from_config(cfg),
                                     spec.blueprint)
            got = pool.pop_next(recent_signatures=recent)
            check(f"case{i}: 池==gate (gate空={not gate})",
                  (got is not None) == (not gate),
                  f"gate={gate[:1]} got={'spec' if got else None}")
            check(f"case{i}: 符合预期", (got is not None) == expect_ok,
                  f"expect={expect_ok}")


def test_pop_all_blocked_returns_none():
    print("\n[2c] 全部候选被挡 -> 返回 None(调用方回落现场生成)")
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
        check("全被挡 -> None", pool.pop_next(recent_signatures=recent) is None)
        check("被挡不等于删除(题还在池里)", pool.size() == 3, pool.size())


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
        check("engine 记下来源是 pool",
              dr.engine._spec_source == "pool", dr.engine._spec_source)
        # 池子标记为已用
        check("池子已把它记为已用", dr.pool.pending_count() == 0,
              dr.pool.pending_count())

        # 走到揭晓 -> archive
        acts = dr.engine._enter_revealing_locked(0.0, "giveup", "")
        rev = [a for a in acts if a.kind.name == "REVEAL"][0]
        dr._archive_reveal(rev.payload, "谜底")
        rec = json.loads(open(cfg.puzzle_out_path, encoding="utf-8")
                         .read().strip())
        check("archive 记 source=pool", rec.get("source") == "pool",
              rec.get("source"))
        check("archive 带 metrics(不丢溯源)",
              isinstance(rec.get("metrics"), dict), rec.get("metrics"))


def test_director_falls_back_when_pool_empty():
    """池空 -> 回落现场生成(不能因为池子空就不出题)。"""
    print("\n[6b] 池空时回落现场生成")
    from director import Director
    with tmpdir() as d:
        # no_llm=False: 要真的走到 gen_spec 那条路(no_llm=True 会走假题,
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
        check("确实调了 gen_spec", called["n"] == 1, called["n"])
        check("来源是 live_generate",
              dr.engine._spec_source == "live_generate", dr.engine._spec_source)


def test_director_with_pool_disabled_never_touches_pool():
    """pool_enabled=False -> Director 完全不开池子(逐位回到 Q8 之前)。"""
    print("\n[6c] pool_enabled=False 时 Director 不碰题池")
    from director import Director
    with tmpdir() as d:
        dr = Director(mkcfg(d, pool_enabled=False))
        check("pool 是 None", dr.pool is None, dr.pool)


# ======================================================================
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
        test_director_with_pool_disabled_never_touches_pool,
    ]
    for t in tests:
        t()
    print()
    if FAIL[0]:
        print(f"FAILED: {FAIL[0]} 项")
        return 1
    print("PASS: 题池 5 个验收点全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
