#!/usr/bin/env python
# coding: utf-8
"""Approved Puzzle Pool —— 已过质量链的题的**存放与挑选**(方案 §40, Q8)。

## 它解决什么

到 Q7 为止每道题都是直播时现场生成(`writer.gen_spec`), 代价是:
  - 出题延迟落在直播命脉上(LLM + 审稿, 实测 10–40s, 期间屏幕上没有新题);
  - 已经过审的题只活在内存与 archive 里, 从不复用。

Q8 把**已通过现有质量链**的 `PuzzleSpec` 存下来、挑出来、优先投入直播;
池子空了再回到现场生成。

## 它**不**解决什么(Q9)

background prefetch / build_pool 自动补池 / playtest。题池里的题靠
**手工或一次性脚本** `add()` 进来; 直播路径**不会**自动往里写。

## 三条硬性质

1. **入池与弹出都重跑校验**。文件里写着 approved 不算数 —— 重新
   `validate_spec()` + `cross_puzzle_gate()`。题在生成时合格, 不代表
   此刻与最近 10 题搭配仍合格。

2. **绝不抛异常、绝不阻塞直播**。所有方法契约上返回空值而不抛。
   任何损坏(文件缺失/不可读/单行 JSON 坏)都退化为"池子空", 于是
   调用方回落现场生成。直播主状态机不受影响。

3. **used 是追加式日志, 不是重写**。这条和"重启后已播的题不复活"
   (验收点 4)直接相关, 见下面 `mark_used` 的说明。

## 先落盘再交付(验收点 4 的核心不变式)

    pop_next()  -> 先把 `air:false` 行写盘+fsync, **才**把 spec 交出去
    engine      -> 拿到之后才可能上屏
    reveal      -> 追加 `air:true`

为什么是**追加**而不是重写整个 used 文件: 重写是 read-modify-write,
中途崩溃会丢掉**整个** used 集合, 于是所有播过的题集体复活。追加的
最坏情况只是丢最后一行 —— 而丢一行 `air:true` 时, 更早那行 `air:false`
仍在盘上, 所以"已播过"这个事实**不会**因为截断而消失。

这也是为什么"单行损坏就地跳过"仍然满足验收点 4: 损坏行等价于
丢一行, 而不是丢全部。

零新依赖。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import time
from typing import Any, Optional

from .puzzle import PuzzleSpec
from .quality import (Quotas, cross_puzzle_gate, too_similar, validate_spec)

log = logging.getLogger("story.pool")

#: 池文件格式版本。将来结构变了靠它判断, 老文件读不出就当空池。
POOL_VERSION = 1

#: 内容哈希取前多少位。
#:
#: 为什么用**内容哈希**而不是 `spec.id`: 实测 `gen_spec` **从不设置 id**
#: (`_spec_from_tool` 不传, `_apply_review` 保留空值, 失败路径也不设),
#: 所以 id 恒为空串, 拿它当身份 = 所有题都是同一道。
KEY_LEN = 16


def spec_key(spec: PuzzleSpec) -> str:
    """一道题的稳定身份(内容哈希)。

    用谜面+谜底+标题+id 一起哈希。只用谜面不够 —— 同一谜面配不同
    谜底是两道不同的题(审稿改谜底时就会这样)。
    """
    raw = "\x1f".join([
        str(getattr(spec, "id", "") or ""),
        str(getattr(spec, "title", "") or ""),
        str(getattr(spec, "puzzle", "") or ""),
        str(getattr(spec, "answer", "") or ""),
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:KEY_LEN]


def _read_jsonl(path: str) -> list:
    """容错读 JSONL —— 坏行跳过 + 记日志, **绝不抛**。

    照 `story/ingest.py` 的 `SimSource._load` 的写法(它是本仓库里唯一
    另一个 JSONL 读取点): 空行与 `//` 注释静默跳过, 其余坏行记 error
    后 continue。

    文件不存在是**正常情况**(第一次跑、或池子被清空), 返回空列表而
    不是报错。
    """
    out: list = []
    if not path:
        return out
    try:
        with open(path, "r", encoding="utf-8") as f:
            for ln, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith("//"):
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError as e:
                    log.error("池文件第 %d 行不是合法 JSON, 已跳过: %s",
                              ln, e)
    except FileNotFoundError:
        log.info("池文件不存在(视为空池): %s", path)
    except OSError as e:
        # 不可读(权限/磁盘)也只当空池 —— 题池是加速器, 不是账本。
        log.warning("池文件读不了(视为空池): %s: %s", path, e)
    return out


def _append_line(path: str, rec: dict, fsync: bool = False) -> bool:
    """追加一行 JSON。成功返回 True。

    `fsync=True` 只用在 **used 日志**上 —— 那条"已交付"的记录必须在
    spec 交出去之前真正落盘, 否则崩溃后它会复活。
    其余写盘(pool 本体)不 fsync: 池子是加速器不是账本, archive 才是
    持久化边界(它才 fsync + 失败停引擎)。

    **不抛异常**: 调用方靠返回值决定要不要继续。
    """
    try:
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            if fsync:
                os.fsync(f.fileno())
        return True
    except (OSError, TypeError, ValueError) as e:
        log.warning("池写盘失败: %s: %s", path, e)
        return False


class PuzzlePool:
    """已过质量链的题的池子。

    线程安全: `pop_next` 会从 worker 线程调用, 也可能被脚本调用。
    所有公开方法都过同一把锁。
    """

    def __init__(self, cfg: Any, rng: Optional[random.Random] = None):
        import threading
        self.cfg = cfg
        self._lock = threading.RLock()
        # 注入 rng, **不要用模块级 random** —— 出题相关逻辑全在 worker
        # 线程里跑, 用全局 random 会互相干扰且无法复现(engine/director
        # 已有同样的教训)。quality_seed 给定则可复现。
        seed = getattr(cfg, "quality_seed", None)
        self._rng = rng if rng is not None else random.Random(seed)

        self.pool_path = str(getattr(cfg, "pool_path", "") or "")
        self.used_path = str(getattr(cfg, "pool_used_path", "") or "")
        self._budget_ms = int(getattr(cfg, "pool_op_budget_ms", 500) or 500)

        self._items: list[PuzzleSpec] = []
        self._keys: set[str] = set()      # 池内去重
        self._used: set[str] = set()      # 已交付过的(含已播)
        self._aired: set[str] = set()     # 其中已揭晓的
        #: 排除了池内已有与已用过的题之后的"该避开"的谜面。
        self._avoid_extra: list[str] = []

    # ------------------------------------------------------------------
    @classmethod
    def open(cls, cfg: Any, rng: Optional[random.Random] = None
             ) -> Optional["PuzzlePool"]:
        """建池并载入。

        `pool_enabled=False` -> 返回 None(调用方据此**完全不碰**题池,
        行为与 Q8 之前逐位相同)。注意这里**不**退回任何默认池 ——
        "关掉"必须是真的关掉, 这一点我们在 `enforce_blueprint` 上
        已经踩过一次。
        """
        if not getattr(cfg, "pool_enabled", True):
            log.info("pool_enabled=False: 本题池完全关闭")
            return None
        p = cls(cfg, rng=rng)
        p.load()
        return p

    # ------------------------------------------------------------------
    def load(self) -> int:
        """载入池子与 used 日志。返回载入的池内题数。**不抛**。"""
        with self._lock:
            self._items = []
            self._keys = set()
            for rec in _read_jsonl(self.pool_path):
                spec = self._spec_from_record(rec)
                if spec is None:
                    continue
                k = spec_key(spec)
                if k in self._keys:
                    continue        # 池内自去重(同一题被 add 两次)
                self._items.append(spec)
                self._keys.add(k)

            self._used = set()
            self._aired = set()
            for rec in _read_jsonl(self.used_path):
                if not isinstance(rec, dict):
                    continue
                k = rec.get("key")
                if not k:
                    continue
                self._used.add(k)
                if rec.get("air"):
                    self._aired.add(k)

            log.info("题池载入: %d 道可用, %d 道已用过(其中 %d 已播)",
                     len(self._items), len(self._used), len(self._aired))
            return len(self._items)

    @staticmethod
    def _spec_from_record(rec: Any) -> Optional[PuzzleSpec]:
        """池记录 -> PuzzleSpec。不合规就返回 None(跳过)。"""
        if not isinstance(rec, dict):
            return None
        if rec.get("pool_version") not in (None, POOL_VERSION):
            # 未来的格式版本: 不认识就跳过, 不要瞎猜字段含义。
            log.warning("池记录的 pool_version=%s 不认识, 已跳过",
                        rec.get("pool_version"))
            return None
        d = rec.get("spec")
        if not isinstance(d, dict):
            return None
        try:
            spec = PuzzleSpec.from_dict(d)
        except Exception:                       # noqa: BLE001
            log.exception("池记录解析失败, 已跳过")
            return None
        return spec if spec.puzzle else None

    # ------------------------------------------------------------------
    def size(self) -> int:
        with self._lock:
            return len(self._items)

    def pending_count(self) -> int:
        """池内**当前可用**(未用过)的题数。"""
        with self._lock:
            return sum(1 for s in self._items
                       if spec_key(s) not in self._used)

    def used_count(self) -> int:
        with self._lock:
            return len(self._used)

    # ------------------------------------------------------------------
    def add(self, spec: PuzzleSpec, source: str = "manual") -> bool:
        """把一道题放进池子。**入池前重跑校验**。

        返回是否真的进去了。校验不过 -> 拒绝且不写盘。

        为什么入池就要验: 将来会有脚本从 archive 里批量导入, 那些
        记录可能是老格式、可能被手改过。"文件里写着 approved"不是
        证据, 重新跑一遍代码判断才是。
        """
        if spec is None or not getattr(spec, "puzzle", ""):
            return False
        if getattr(spec, "error", None):
            log.info("拒绝入池: spec 带 error(%s)", str(spec.error)[:60])
            return False
        try:
            vr = validate_spec(spec)
        except Exception:                       # noqa: BLE001
            log.exception("入池校验异常, 拒绝")
            return False
        if not vr.ok:
            log.info("拒绝入池: 硬校验不过(%s)", vr.why()[:120])
            return False
        if vr.fixable:
            # fixable 是"审稿人改一句就能救", 但池子里的题**已经**应该
            # 是审稿后的成品 —— 还留着 fixable 说明它没走完质量链。
            log.info("拒绝入池: 还有未修的 fixable(%s)", vr.must_fix()[:120])
            return False

        with self._lock:
            k = spec_key(spec)
            if k in self._keys:
                log.info("拒绝入池: 池内已有同一道题")
                return False
            if k in self._used:
                log.info("拒绝入池: 这道题已经用过了")
                return False
            rec = {
                "pool_version": POOL_VERSION,
                "pool_key": k,
                "added_at": time.time(),
                "added_by": source,
                "spec": spec.to_archive(),
            }
            if not _append_line(self.pool_path, rec, fsync=False):
                return False
            self._items.append(spec)
            self._keys.add(k)
            log.info("入池: %s… (%s)", spec.puzzle[:30], source)
            return True

    # ------------------------------------------------------------------
    def pop_next(self, recent_signatures: Optional[list] = None,
                 avoid: Optional[list] = None) -> Optional[PuzzleSpec]:
        """挑一道**此刻**可用的题。挑不到返回 None(调用方回落现场生成)。

        每道候选都要过三关:
          ① `validate_spec`  —— 重新确认它本身仍是合格的;
          ② `cross_puzzle_gate` —— 与**当前** recent 窗口的分布是否冲突;
          ③ `too_similar` —— 谜面是否与最近出过的太像。

        ⚠️ 第 ② 关**必须调用 `cross_puzzle_gate` 本身**, 不能只比
        `(mechanism_family, solution_shape)`。那个 tuple 只是结构去重,
        而 `check_signature` 实际还管着 death / past_trauma /
        trauma_ritual / grief / profession_ritual / domain / relation,
        一共 9 个维度。

        被拒的候选**不**从池里删掉 —— 池子是**集合不是队列**: 它是被
        "当前窗口"挡住的, 等最近 10 题滚过去之后它就能用了。

        **不抛异常**: 任何意外都退化成返回 None。
        """
        try:
            return self._pop_next_locked(recent_signatures, avoid)
        except Exception:                       # noqa: BLE001
            log.exception("pop_next 异常, 本题回落现场生成")
            return None

    def _pop_next_locked(self, recent: Optional[list],
                         avoid: Optional[list]) -> Optional[PuzzleSpec]:
        with self._lock:
            cands = [s for s in self._items
                     if spec_key(s) not in self._used]
            if not cands:
                log.info("题池没有可用题(池内 %d 道), 回落现场生成",
                         len(self._items))
                return None
            # 打散, 免得每次总是同一道被先试(池内顺序会随 add 固定)
            self._rng.shuffle(cands)
            quotas = Quotas.from_config(self.cfg)
            blocked: list[str] = []
            for spec in cands:
                # ① 本体仍合格?
                vr = validate_spec(spec)
                if not vr.ok:
                    blocked.append(f"{spec.puzzle[:20]}…: {vr.why()[:60]}")
                    continue
                # ② 与当前分布冲突?
                bad = cross_puzzle_gate(spec, recent, quotas, spec.blueprint)
                if bad:
                    blocked.append(f"{spec.puzzle[:20]}…: {bad[0][:60]}")
                    continue
                # ③ 谜面与最近出过的太像?
                used_texts = list(avoid or []) + self._avoid_extra
                dup = too_similar(spec.puzzle, used_texts)
                if dup:
                    blocked.append(f"{spec.puzzle[:20]}…: 与已出过的太像")
                    continue
                # ---- 先落盘再交付(见模块 docstring 的不变式) ----
                if not self._persist_used(spec, aired=False):
                    # 记不下来就**不交付** —— 宁可不播, 也不冒"重启后
                    # 同一道题再播一次"的风险。
                    log.warning("used 记不下来, 放弃这道题(回落现场生成)")
                    blocked.append(f"{spec.puzzle[:20]}…: used 写失败")
                    continue
                self._used.add(spec_key(spec))
                log.info("题池出题: %s…", spec.puzzle[:30])
                return spec

            log.info("题池 %d 道候选全部被挡(回落现场生成): %s",
                     len(cands), " | ".join(blocked[:3]))
            return None

    # ------------------------------------------------------------------
    def mark_used(self, spec: PuzzleSpec, aired: bool = True) -> None:
        """记下"这道题播过了"。**追加**一行, 永不重写整个文件。

        `aired=False` 是 `pop_next` 交付时写的(已发出但还没揭晓),
        `aired=True` 是揭晓时补写的。两者都进 `_used`, 所以即使 `aired`
        那行丢了, 题也**不会**复活。

        不抛异常。
        """
        if spec is None:
            return
        k = spec_key(spec)
        if aired:
            with self._lock:
                if k in self._aired:
                    return
                self._aired.add(k)
        self._persist_used(spec, aired=aired)

    def _persist_used(self, spec: PuzzleSpec, aired: bool) -> bool:
        rec = {
            "key": spec_key(spec),
            "at": time.time(),
            "air": bool(aired),
            "puzzle": (spec.puzzle or "")[:60],
        }
        # fsync: 这条必须在 spec 真正上屏之前落到盘上(见模块 docstring)。
        return _append_line(self.used_path, rec, fsync=True)

    # ------------------------------------------------------------------
    def remember_avoid(self, texts: list) -> None:
        """把"已经出过的谜面"记下来, 供 `too_similar` 使用。

        池子会跨进程/跨场次存活, 而引擎的 `_used_titles` 只活在当前
        进程里。不记的话, 重启后从池里挑出来的题可能和上一场刚播过的
        重复。
        """
        with self._lock:
            for t in (texts or []):
                t = str(t or "").strip()
                if t and t not in self._avoid_extra:
                    self._avoid_extra.append(t)
            if len(self._avoid_extra) > 50:
                self._avoid_extra = self._avoid_extra[-50:]

    # ------------------------------------------------------------------
    def stats(self) -> dict:
        """给启动 banner / 排查用。"""
        with self._lock:
            return {
                "size": len(self._items),
                "available": sum(1 for s in self._items
                                 if spec_key(s) not in self._used),
                "used": len(self._used),
                "aired": len(self._aired),
                "path": self.pool_path,
                "used_path": self.used_path,
            }
