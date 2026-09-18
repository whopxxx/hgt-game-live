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

4. **账本 fail closed, 池子 fail open**。两个文件的容错语义**不同**:
   `pool.jsonl` 是缓存(坏行跳过, 好题照用); `pool_used.jsonl` 是**账本**
   —— 只要有一处读不全, 整个账本就不可信, 本次**一道都不交付**,
   回落现场生成。因为"某道题不在 used 里"和"那行没读出来"从结果上
   无法区分, 而前者意味着把已经播过的题再播一次。详见 `_read_jsonl`。

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

from .puzzle import (MECHANISM_FAMILIES, SOLUTION_SHAPES, PuzzleSpec)
from .quality import (Quotas, cross_puzzle_gate, too_similar, validate_blueprint,
                      validate_spec)

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


def _read_jsonl(path: str, strict: bool = False) -> tuple:
    """容错读 JSONL, 返回 `(records, trustworthy)`。

    ## 为什么要有 `strict`: 池子是缓存, used 是**账本**

    两个文件的容错语义**必须分开**, 混用会把"已播过的题不复活"这条
    硬保证打穿:

      - `pool.jsonl`(缓存): 坏行跳过, 剩下的题照用。池子少几道题
        只是少点便利, 不影响正确性。
      - `pool_used.jsonl`(**账本**): 只要有一处读不全, 整个账本
        就**不可信** —— 因为"某道题不在 `_used` 里"和"那行没读出来"
        从结果上无法区分, 而前者的后果是**把已经播过的题再播一次**。

    所以 `strict=True` 时: 坏行/读不了 -> `trustworthy=False`, 调用方
    据此**本次禁用题池**(fail closed), 回落现场生成。**不尝试"尽量
    恢复"** —— 我们选的是安全优先。

    文件不存在**不算**不可信: 第一次启动本来就是空账本。

    另外: `open()` 遇到非法 UTF-8 会抛 `UnicodeDecodeError`, 那**不是**
    `OSError` 的子类, 早先没被捕获, 会让 `PuzzlePool.open()` 在
    Director 启动时直接崩掉。这里一并接住。
    """
    out: list = []
    if not path:
        return out, True
    try:
        with open(path, "r", encoding="utf-8") as f:
            for ln, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith("//"):
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError as e:
                    log.error("池文件第 %d 行不是合法 JSON: %s", ln, e)
                    if strict:
                        return out, False
    except FileNotFoundError:
        # 空账本/空池都是正常状态。
        log.info("池文件不存在(视为空): %s", path)
        return out, True
    except (OSError, UnicodeDecodeError) as e:
        log.warning("池文件读不了: %s: %s", path, e)
        if strict:
            return out, False
    return out, True


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

        self._items: list[PuzzleSpec] = []
        self._keys: set[str] = set()      # 池内去重
        self._used: set[str] = set()      # 已交付过的(含已播)
        self._aired: set[str] = set()     # 其中已揭晓的
        #: used 账本是否可信。读不全时为 False -> 题池本次**完全禁用**。
        #: 默认 False: 没 load 过之前不该交付任何东西(fail closed)。
        self._used_trustworthy = False
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
        """载入池子与 used 账本。返回载入的池内题数。**不抛**。

        两个文件的容错语义不同(见 `_read_jsonl`): 池子坏行跳过,
        账本坏一处就整体不可信 -> `_used_trustworthy=False` ->
        `pop_next` 本次一律返回 None(fail closed)。
        """
        with self._lock:
            self._items = []
            self._keys = set()
            # 池子是缓存: 坏行跳过, 好题照用。
            pool_recs, _ = _read_jsonl(self.pool_path, strict=False)
            for rec in pool_recs:
                spec = self._spec_from_record(rec)
                if spec is None:
                    continue
                k = spec_key(spec)
                if k in self._keys:
                    continue        # 池内自去重(同一题被 add 两次)
                self._items.append(spec)
                self._keys.add(k)

            # 账本是权威: 读不全就整体不可信。
            used_recs, self._used_trustworthy = _read_jsonl(
                self.used_path, strict=True)
            self._used = set()
            self._aired = set()
            for rec in used_recs:
                if not isinstance(rec, dict):
                    continue
                k = rec.get("key")
                if not k:
                    continue
                # 注意: 记进 _used 的**只**该是题池交付过的题。老日志里
                # 可能混着别的来源(早先的 bug), 这里无法分辨, 所以
                # 一律算 —— 宁可少用一道题, 也不能复活一道。
                self._used.add(k)
                if rec.get("air"):
                    self._aired.add(k)

            if not self._used_trustworthy:
                log.error("used 账本不可信(有损坏行/读不了), "
                          "题池本次**完全禁用**, 回落现场生成: %s",
                          self.used_path)
            log.info("题池载入: %d 道可用, %d 道已用过(其中 %d 已播), "
                     "账本可信=%s",
                     len(self._items), len(self._used), len(self._aired),
                     self._used_trustworthy)
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
        # ---- signature 必须有效(P1) ----
        #
        # `validate_spec` **不**要求 signature 存在, 所以光靠它, 一道
        # signature 全空的题也能入池。弹出时 `cross_puzzle_gate` 会拿
        # blueprint 临时顶替, 看起来还能工作 —— 但上屏时 `_submit_spec`
        # 传的是 `spec.signature.to_dict()`, 也就是那个**空 dict**。
        # engine 见它是非空 dict 就登记进 recent, 于是:
        #     这道题实际播了 hidden_function -> recent 里记成空白
        #     -> 下一题的全局配额看不见它 -> 配额被悄悄放松。
        #
        # 所以"完整 spec"在这里要更严格: signature 的核心维度必须存在
        # 且枚举合法。老 archive 缺 signature 的题仍可被 `from_dict`
        # 读出来, 但**不能直接成为池库存量** —— 要进池得走显式的
        # 迁移/重新审批。
        core = (spec.signature.mechanism_family, spec.signature.solution_shape)
        if not all(core):
            log.info("拒绝入池: signature 缺核心维度(%s)", core)
            return False
        if spec.signature.mechanism_family not in MECHANISM_FAMILIES:
            log.info("拒绝入池: mechanism_family 不在枚举内(%s)",
                     spec.signature.mechanism_family)
            return False
        if spec.signature.solution_shape not in SOLUTION_SHAPES:
            log.info("拒绝入池: solution_shape 不在枚举内(%s)",
                     spec.signature.solution_shape)
            return False
        # blueprint 被显式分配过 -> 顺带验它确实被执行了(与实时路径
        # 的第三道门一致)。没分配过(自由生成)就跳过。
        if getattr(spec, "blueprint_specified", False):
            try:
                vb = validate_blueprint(spec, spec.blueprint)
            except Exception:                   # noqa: BLE001
                log.exception("入池 blueprint 校验异常, 拒绝")
                return False
            if not vb.ok:
                log.info("拒绝入池: blueprint 校验不过(%s)", vb.why()[:120])
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
            # ---- fail closed ----
            # 账本不可信时**一道都不交付**。理由: "某道题不在 _used 里"
            # 和"那一行没读出来"从结果上无法区分, 而前者意味着把已经
            # 播过的题再播一次 —— 那正是我们定死的"宁可不播"要避免的。
            # 池子本身还完好, 但账本坏了就不能信池子里的任何判断。
            if not self._used_trustworthy:
                log.error("used 账本不可信, 本次不交付任何题(回落现场生成)")
                return None
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
