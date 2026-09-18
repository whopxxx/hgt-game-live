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
   —— 只要有一处读不全**或读不懂**, 整个账本就不可信, 本次**一道都不
   交付**, 回落现场生成。因为"某道题不在 used 里"和"那行没读出来"从
   结果上无法区分, 而前者意味着把已经播过的题再播一次。读不懂同样
   算坏: `null` / `{}` / `{"air":false}` 都是合法 JSON, 但它们和
   "key 被写坏了"无法区分。详见 `_read_jsonl` 与 `_valid_used_record`。

5. **入池与弹出走同一扇门**。`_validate_pool_spec()` 是唯一的准入
   校验, `add()`(API 入口)与 `pop_next()`(磁盘入口)都调它。题池允许
   手工灌池, 所以"入池时验过了"不能替代"弹出时再验一次"。

6. **quality policy 不兼容 = 隔离, 不是删除(Step 03)**。只有
   `spec.quality_policy_version == quality.QUALITY_POLICY_VERSION` 的题
   才是 live-eligible 库存。空串/缺失/别的版本一律挡在
   `_validate_pool_spec()` 这扇门外 —— 于是它们**自然**地:

       不计入 stock_count      -> 补池看得见"库存=0", 会去补新题
       不能通过 add()          -> 新灌不进旧的
       不会被 pop_next() 返回  -> 播不出来
       不写 used ledger        -> quarantine != 已播出

   但它们**仍然留在 `pool.jsonl` 里**(`load()` 不筛, `size()` /
   `pending_count()` 仍看得见), 因为磁盘上的候选集合与"此刻能不能播"
   是两回事。未来显式的离线 migration/re-review 之后它仍可能重新合法
   —— 所以隔离**绝不**改版本号、**绝不**删行、**绝不**在线重审。

   这一门同时是未来 policy bump 的开关: Step 04 把
   `QUALITY_POLICY_VERSION` 提到 v4 时, 现存 v3 库存会因为这一条
   自动失去 live eligibility, 而新生成的 v4 题照常补得进去 —— 不需要
   任何额外的清理动作。

## 先落盘再交付(验收点 4 的核心不变式)

    pop_next()  -> 先把 `air:false` 行写盘+fsync, **才**把 spec 交出去
    engine      -> 拿到之后才可能上屏
    reveal      -> 追加 `air:true`

为什么是**追加**而不是重写整个 used 文件: 重写是 read-modify-write,
中途崩溃会丢掉**整个** used 集合, 于是所有播过的题集体复活。追加的
最坏情况只是丢最后一行 —— 而丢一行 `air:true` 时, 更早那行 `air:false`
仍在盘上, 所以"已播过"这个事实**不会**因为截断而消失。

⚠️ 注意**不要**从上面这句推出"used 单行损坏就地跳过也可以"。那正是
早先 fail-open 版本的错误推理: 它假定"损坏行 = 丢一行 = 安全"。对
`air:true` 那行成立, 但**对任意一行不成立** —— 我们事前并不知道坏的
是哪一行, 而如果坏的是 `air:false` 行, 那道题就在 `_used` 里消失了,
下次会被再播一次。所以 used 的语义是**整份账本**:
单行损坏 -> 读不懂一处 -> 整个 ledger 不可信 -> 题池本次完全禁用
(回落现场生成)。损坏的粒度是"账本", 不是"行"。

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

from .puzzle import (DOMAINS, EMOTION_MODES, MECHANISM_FAMILIES, RELATIONS,
                     SOLUTION_SHAPES, TIME_SHAPES, PuzzleSpec)
from .quality import (QUALITY_POLICY_VERSION, Quotas, cross_puzzle_gate,
                      too_similar, validate_blueprint, validate_spec)

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


#: key 允许的字符集。`spec_key` 的输出恒为小写 hex。
_HEX = frozenset("0123456789abcdef")


def _valid_used_record(rec: Any) -> bool:
    """这条 JSON 是不是一条**能解释**的 used 账本记录?

    ## 为什么"合法 JSON"还不够

    `strict=True` 只保证**解析得动**, 不保证**看得懂**:

        {}
        null
        {"air": false}

    三个都是合法 JSON。早先的加载循环对它们 `continue` 跳过, 于是账本
    仍被判 `trustworthy=True` —— 可 fail closed 的定义是"读不出一处就
    整体不信"。而这里我们同样分不清:

        它本来就是垃圾      vs      它原本是一条已播记录, 但 key 被写坏了

    后者意味着那道题会复活。所以 schema 不合法 == 账本不可信。

    key 必须恰好是 `KEY_LEN` 位小写 hex: 这同时排除了"key 被截断"和
    "key 被换成别的东西"两种损坏。`air` 必须是真正的 bool —— 缺失也
    算坏, 因为 `_persist_used` 无条件和写它, 读不到说明行是残缺的。
    """
    if not isinstance(rec, dict):
        return False
    k = rec.get("key")
    if not isinstance(k, str) or len(k) != KEY_LEN:
        return False
    if not all(c in _HEX for c in k):
        return False
    return isinstance(rec.get("air"), bool)


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
                # schema 不对 == 读不懂 == 和坏行同等对待(fail closed)。
                # `strict=True` 只挡得住"解析不动"的行, 挡不住 `null`
                # 这种"解析得动但没意义"的行 —— 见 `_valid_used_record`。
                if not _valid_used_record(rec):
                    log.error("used 账本里有无法解释的记录, 整个账本判为"
                              "不可信: %s", str(rec)[:120])
                    self._used_trustworthy = False
                    break
                k = rec["key"]
                # 注意: 记进 _used 的**只**该是题池交付过的题。老日志里
                # 可能混着别的来源(早先的 bug), 这里无法分辨, 所以
                # 一律算 —— 宁可少用一道题, 也不能复活一道。
                self._used.add(k)
                if rec["air"]:
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
        """池内**磁盘上还有多少条没用过的候选**。

        ⚠️ 这**不是**"此刻能播多少道": 它只减 `_used`, **不跑准入
        校验**。所以 quality-policy 不兼容(隔离)的题、signature 被
        磁盘改坏的题, **都**还算在这里面。要看"真能播的库存"用
        `stock_count()`。两个数不等是**正常**的, 不要为了对齐而删题
        —— 隔离项必须原样留在盘上。
        """
        with self._lock:
            return sum(1 for s in self._items
                       if spec_key(s) not in self._used)

    def used_count(self) -> int:
        with self._lock:
            return len(self._used)

    @property
    def ledger_trustworthy(self) -> bool:
        """used 账本是否可信。

        补池(Q9)必须读它: 账本不可信时 `pop_next` 一道都不交付, 此时
        再往池子里灌题就是纯烧网关配额 —— 灌多少都不会被播出来。

        **只读**: `_used_trustworthy` 的写者只有一个, 就是 `load()`。
        补池绝不碰它(否则"账本坏了"这个判断会被补池活动悄悄改掉)。
        """
        with self._lock:
            return self._used_trustworthy

    # ------------------------------------------------------------------
    def stock_count(self, limit: Optional[int] = None) -> int:
        """**补池用**的库存数: 未 used 且**能过 `_validate_pool_spec()`**
        的持久题。数够 `limit` 就早退。

        为什么补池不能直接用 `pending_count()`: 后者只减 `_used`,
        **不跑校验** —— 一道 signature 被磁盘改坏的题照样被算进"库存"。
        补池据此判断"够不够"就会一直少补; 极端情况下库存看着有 3 道、
        实际 0 道能播, 补池却认为池子满了, 一道都不补。

        **quality policy 隔离也走这条**(Step 03): 旧 policy 的题过不了
        准入门, 所以一旦 `QUALITY_POLICY_VERSION` 上调, 盘上那批旧题
        立刻不再计入库存 -> `stock_count` 归零 -> 补池开始补新题。
        这正是"bump 版本号就自动完成隔离"的机制, 不需要额外的清理步骤。

        两个数的**语义不同**, 不是同一个量的两种写法:
            `pending_count` = 盘上有多少条候选
            `stock_count`   = 其中多少条真能进 `pop_next` 的候选集

        **刻意不扣** dynamic gate: `recent_signatures` / `avoid` /
        `cross_puzzle_gate` / `too_similar` 全是"**此刻能不能播**",
        不是"库存有没有"。被当前窗口挡住的题**仍然是库存** —— 等最近
        N 题滚过去它就能用。把它们扣掉会让补池在窗口拥挤时狂补,
        而盘上其实已经堆满了。

        `limit`: 数够就早退。补池的 latch 只需要区分三种情况
        ("< 低水位" / ">= 高水位" / 都不是), 数到 target 就够判断了;
        而校验是 O(池大小) 且这个函数由 4Hz 的 tick 调用, 池子大了
        会和 live 路径的 `pop_next` 抢同一把锁。传 limit 把这个
        开销封顶。

        不抛异常(与本模块其他公开方法一致)。
        """
        with self._lock:
            n = 0
            for s in self._items:
                if limit is not None and n >= limit:
                    break
                if spec_key(s) in self._used:
                    continue
                try:
                    ok, _ = self._validate_pool_spec(s)
                except Exception:                   # noqa: BLE001
                    log.exception("库存校验异常, 该题不计入库存")
                    continue
                if ok:
                    n += 1
            return n

    # ------------------------------------------------------------------
    @staticmethod
    def _validate_pool_spec(spec: PuzzleSpec) -> tuple:
        """池的**统一准入门**。返回 `(ok, why)`。

        `add()`(API 入口)和 `pop_next()`(磁盘入口)都走这一扇门。

        ## 为什么弹出时还要再验一次

        Q8 的原则是"入池和弹出都不信任磁盘内容"。只验 `add()` 等于只
        验了 API 入口 —— 而题池**允许手工灌池**(Q9 之前全靠它), 所以
        `pool.jsonl` 里完全可能有一道 signature 被清空/改坏的题。那时:

            validate_spec 通过(它不查 signature)
            -> cross_puzzle_gate 拿 blueprint 临时顶替(看起来能用)
            -> 交付 -> _submit_spec 传空 signature
            -> engine 登记进 recent 的是空白
            -> 后续全局配额看不见这道题 -> 配额被悄悄放松

        所以同一个 `_validate_pool_spec` 必须在两个入口都跑。Q9 的自动
        补池也复用这扇门。

        ## 为什么六个分类字段都要验, 不只 mechanism/solution

        `domain` / `relation` / `emotion_mode` 同样参与 `cross_puzzle_gate`
        的配额统计(见 `quality._quota_conflicts`)。磁盘里写
        `domain="乱写的值"` 的题目前仍能进池, 之后它计进
        `domain:乱写的值` 这个桶 —— 而它实际会占掉别的领域的播出位,
        等于绕过 `same_domain` 配额。

        `time_shape` 在这里**只要求属于合法枚举**, 不参与配额, 也
        **不**与 blueprint 严格比对(`validate_blueprint` 刻意不比对它,
        见 `quality.py` 的说明: blueprint 里 time_shape 只有默认值
        instant, 严格比对会误杀"长年习惯"这类题)。收进来只是为了
        挡住明显的坏值, 以及让它能被统计。
        """
        if spec is None or not getattr(spec, "puzzle", ""):
            return False, "空 spec / 空谜面"
        if getattr(spec, "error", None):
            return False, f"spec 带 error({str(spec.error)[:60]})"

        # ---- quality policy 兼容门(Step 03) ----
        #
        # 只有**通过当前内容质量政策**的题才是 live-eligible 库存。
        # 三种情况全部隔离, **不猜**:
        #     ""(空串)  /  字段缺失  /  != 当前政策
        #
        # 为什么缺失**不能**默认成当前版本: 老 archive 实测 103/118 条
        # 根本没有这把键(`data/puzzle.jsonl` 观察)。把它们当成 v3 等于
        # 用"我猜它大概是 v3"替换"它没说是哪版" —— 而 `time_shape` 在
        # v2/v3 之间的语义正好相反(v2 是被强制成 instant, v3 是如实
        # 观察, 见 `quality.py` 的 QUALITY_POLICY_VERSION 注释), 猜错
        # 方向就会把两版不可比较的 signature 混进同一个配额窗口。
        # unknown 就是 unknown, 按隔离处理。
        #
        # 隔离**不等于删除**: quarantine 的题留在 `pool.jsonl` 里,
        # 由本函数在每个入口一致地挡住即可 —— 见下面的语义说明。
        spec_policy = str(getattr(spec, "quality_policy_version", "") or "")
        if not spec_policy:
            return False, "quality policy 缺失/未知(spec 没有声明过它属于哪一版政策)"
        if spec_policy != QUALITY_POLICY_VERSION:
            return False, (f"quality policy 不兼容"
                           f"(spec={spec_policy!r}, "
                           f"current={QUALITY_POLICY_VERSION!r})")

        try:
            vr = validate_spec(spec)
        except Exception:                       # noqa: BLE001
            log.exception("池校验异常, 拒绝")
            return False, "校验抛异常"
        if not vr.ok:
            return False, f"硬校验不过({vr.why()[:120]})"
        if vr.fixable:
            # fixable 是"审稿人改一句就能救", 但池子里的题**已经**应该
            # 是审稿后的成品 —— 还留着 fixable 说明它没走完质量链。
            return False, f"还有未修的 fixable({vr.must_fix()[:120]})"

        # ---- signature 必须完整(P1) ----
        #
        # `validate_spec` **不**要求 signature 存在, 所以光靠它, 一道
        # signature 全空的题也能进池。
        sig = getattr(spec, "signature", None)
        if sig is None:
            return False, "缺 signature 对象"
        core = (sig.mechanism_family, sig.solution_shape)
        if not all(core):
            return False, f"signature 缺核心维度({core})"
        for name, val, allowed in (
                ("mechanism_family", sig.mechanism_family, MECHANISM_FAMILIES),
                ("solution_shape", sig.solution_shape, SOLUTION_SHAPES),
                ("domain", sig.domain, DOMAINS),
                ("relation", sig.relation, RELATIONS),
                ("emotion_mode", sig.emotion_mode, EMOTION_MODES),
                ("time_shape", sig.time_shape, TIME_SHAPES)):
            if val not in allowed:
                return False, f"signature.{name} 不在枚举内({val!r})"
        # blueprint 被显式分配过 -> 顺带验它确实被执行了(与实时路径
        # 的第三道门一致)。没分配过(自由生成)就跳过。
        if getattr(spec, "blueprint_specified", False):
            try:
                vb = validate_blueprint(spec, spec.blueprint)
            except Exception:                   # noqa: BLE001
                log.exception("blueprint 校验异常, 拒绝")
                return False, "blueprint 校验抛异常"
            if not vb.ok:
                return False, f"blueprint 校验不过({vb.why()[:120]})"
        return True, ""

    # ------------------------------------------------------------------
    def add(self, spec: PuzzleSpec, source: str = "manual") -> bool:
        """把一道题放进池子。**入池前重跑校验**。

        返回是否真的进去了。校验不过 -> 拒绝且不写盘。

        为什么入池就要验: 将来会有脚本从 archive 里批量导入, 那些
        记录可能是老格式、可能被手改过。"文件里写着 approved"不是
        证据, 重新跑一遍代码判断才是。
        """
        ok, why = self._validate_pool_spec(spec)
        if not ok:
            log.info("拒绝入池: %s", why)
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
                # ① 本体仍合格? **走和 add() 同一扇门** —— 磁盘里的
                #    signature 可能在入池后被改坏, 只验 API 入口不够。
                #    quality policy 不兼容的题也在这里被挡下, 而且
                #    **在 `_persist_used` 之前** continue —— 所以隔离题
                #    不会进 used ledger(quarantine != 已播出; 它将来
                #    离线重审后仍可能重新合法)。
                ok, why = self._validate_pool_spec(spec)
                if not ok:
                    blocked.append(f"{spec.puzzle[:20]}…: {why[:60]}")
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
                # 补池用的真实库存(未 used 且过得了准入门)。与 available
                # 的差就是"盘上有、但其实播不出来"的题数 —— banner 里
                # 两个都打, 免得"库存 5 却一道都取不出来"没法解释。
                "stock": self.stock_count(),
                "used": len(self._used),
                "aired": len(self._aired),
                "trustworthy": self._used_trustworthy,
                "path": self.pool_path,
                "used_path": self.used_path,
            }
