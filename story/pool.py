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
                      cross_puzzle_gate_split,
                      too_similar, validate_blueprint, validate_reveal_adherence,
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


#: key 允许的字符集。`spec_key` 的输出恒为小写 hex。
_HEX = frozenset("0123456789abcdef")

#: curated 决策账本路径。**只读**用 —— 池子绝不写它(写者是
#: `LazyCurator`/预热 CLI)。测试可以改它来指向临时文件。
_CURATED_DECISIONS_PATH = os.path.join("data", "curated_decisions.jsonl")


def set_curated_decisions_path(path: str) -> None:
    """改决策账本路径(**只给测试/装配用**)。

    为什么需要一个模块级可变路径而不是每次新建一个 ledger: 准入门是
    `@staticmethod`, 拿不到装配层的对象。与其把 ledger 一路透传进
    `pop_next` / `stock_count` 的签名(那几个函数的调用点很多, 而且
    每加一个参数都要改测试), 不如让这一个路径成为**唯一**的注入口。
    """
    global _CURATED_DECISIONS_PATH
    _CURATED_DECISIONS_PATH = str(path or "")


def _curated_decision_ok(external_id: str, policy_version: str,
                         content_hash: str = "") -> tuple:
    """这道 curated 题在账本里有没有**针对这份内容**的 `accepted`?

    返回 `(ok, why)`。**fail closed**: 读不出账本就当作"没有 accepted"
    —— 宁可少播一道题, 不可播一道账本不认的题(那意味着它会重复入池)。

    ## 为什么必须是 `(external_id, content_hash, policy)` **三元组**

    H3-D3 §一-4。早先这里只按 `external_id + policy` 倒扫, 理由写的是
    "池行里没有 surface/bottom 原文, 算不出 content_hash"。那个理由
    **不再成立**: 池行现在自带 `curated_content_hash`(见 `PuzzleSpec`)。

    只按 id 查有一个真实漏洞: SE 帖子可以被编辑而问题号不变。

        q123 内容 A accepted -> 池里放 A(账本里 A 的 accepted 行留着)
        作者把 q123 改成内容 B
        重审 B, 这次被判 rejected
        -> 只按 id 查: 命中 A 的 accepted 行 -> **B 被错放行**

    反过来同样错: A 被 rejected, B 被 accepted, 只按 id 查会先看到
    (倒序的)某一条 —— 结论取决于写入顺序, 那不是一个判据。

    所以必须**三元组全同**才算"这份内容被接受过"。同 external_id 的
    旧内容 accepted, 不得授权新内容。

    ## 空 content_hash: 老 archive -> 不可播

    老池行没有这把键(空串), 而账本里的行**一定**有 content_hash(它是
    `make_decision` 无条件写的)。所以空串**永远匹配不到**任何一行 ->
    自动返回 False -> 按"未提交"处理。这正是我们要的: 老库存自动失去
    eligibility, 不需要人工清理(与 curated_policy_version 缺失同一条)。
    """
    eid = str(external_id or "")
    if not eid:
        return False, "curated 题缺 external_id(无法与决策账本对上)"
    ch = str(content_hash or "")
    if not ch:
        return False, ("curated 题缺 curated_content_hash(老 archive 没有"
                       "内容绑定) —— 按不可播处理")
    try:
        from tools.curated_ledger import ACCEPTED, DecisionLedger
        led = DecisionLedger(_CURATED_DECISIONS_PATH)
        for row in reversed(led.rows):
            if str(row.get("external_id") or "") != eid:
                continue
            if str(row.get("policy_version") or "") != str(policy_version):
                continue
            if str(row.get("content_hash") or "") != ch:
                # 同一个 id 的**别的内容**的决策 —— 与本行无关, 继续往前
                # 找。**不能**在这里下结论: 这个 id 可能有多份内容的记录。
                continue
            # 三元组全同 -> 这一行就是本内容的最后一条决策。
            if row.get("decision") == ACCEPTED:
                return True, ""
            return False, (f"这份内容没有 accepted 决策(最后一条是 "
                           f"{row.get('decision')!r}) —— 属于未提交的半状态")
    except Exception:                           # noqa: BLE001
        log.exception("读 curated 决策账本失败 -> 该题按不可播处理")
        return False, "curated 决策账本读不出(fail closed)"
    return False, "curated 决策账本里查不到这份内容(未提交)"


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
        # ---- H4-E / G4-B: 池的身份(显式标记, **不猜路径**) ----
        # 两种池都允许"第二遍"选择: Pass 1 用完整 diversity 门, 一道都没有
        # 时 Pass 2 忽略分布配额(结构相似 != 同一道题)。
        #
        # H4-E 原本只给 curated 这一遍, 理由是"生成的题可以重出, 外部题只能
        # 判能不能播"。G4-B **推翻**了这条: 产品决定是"同类型不是拒题理由",
        # 而实播症状是 `stock > 0` 却因 recent-10 配额占满导致 `playable == 0`
        # —— 观众在等, 池里明明有合格题。所以 generated 也走两遍。
        # 遍历阶梯见 `_passes()`(curated 与 generated 的 Pass 1 现在都忽略
        # soft 维度, 两条链的 diversity 口径因此**一致**)。
        #
        # 为什么是显式字段而不是"路径 == curated_pool_path": `open_curated`
        # 会把 cfg 浅拷贝的 pool_path 覆盖成 curated 路径, 事后无法回推;
        # 而且两份 cfg 共用路径时推断会直接判错。所以由**构造者**写死。
        self.pool_kind: str = "generated"
        #: G4-B: **Pass 2 真正起作用的次数**。每交付一道"Pass 1 挑不出、
        #: 靠忽略 diversity 才拿到"的题就 +1。
        #:
        #: 为什么必须记: 这个数就是"diversity 到底挡住了多少"的直接读数。
        #: 如果它长期是 0, 说明 Pass 2 从来没被用到 —— 那要么是池子很健康,
        #: 要么是 `_passes()` 根本没生效(本项第一版把 generated 接成两遍时
        #: 第一遍就是 hard-only, 于是 Pass 2 永远轮不到)。有这条计数,
        #: "接了但没生效"就不会静默通过。
        self.diversity_reject_count = 0

    # ------------------------------------------------------------------
    @property
    def _soft_diversity(self) -> bool:
        """本池是否启用两遍 soft diversity。**curated 与 generated 都启用**。

        `getattr` 是刻意的: 测试替身(`_FakePool` 等 duck-typed 对象)与
        老实例可能没有这个属性 —— 缺省按 `"generated"` 处理, 即**行为不变**。
        """
        return str(getattr(self, "pool_kind", "") or "") == "curated"

    def _passes(self) -> tuple:
        """本池的两遍 soft 阶梯 —— 喂给 `_candidate_block_reason_locked` 的
        `soft_ok` 序列。

        ## G4-B: generated 池也要两遍

        原本只有 curated 池享受 Pass 2(H4-E), 理由是"生成的题可以重出,
        外部题只能判能不能播"。G4 把这条**推翻**了: 产品决定是

            同类型不是拒题理由。
            safety / correctness / playability / true duplicate 才是硬门。

        而实播症状是具体的: `stock > 0` 却 `playable == 0`, **只因为**
        最近 10 题把 mechanism / domain / death 之类的配额占满了。补池看到
        playable=0 就狂补, 补进来的新题被**同一个**窗口挡住 —— 一路补到
        硬上限, 观众仍然在等。Pass 2 就是消灭这个形状的: Pass 1 先挑不同
        类型的, 实在没有就忽略纯 diversity 照样播合格题。

        ⚠️ Pass 2 **不是**放宽任何硬门。`_validate_pool_spec`(policy 兼容 /
        curated 授权 / 静态校验 / used)与 `too_similar`(文本 near-duplicate
        = identity)在 `_candidate_block_reason_locked` 里**两遍都执行**。

        ## 为什么是查表而不是 `(False, True) if self._soft_diversity else (False,)`

        `"generated"` 要**新**变成两遍, 但 `_soft_diversity` 的历史语义
        (`== "curated"`)不能就地改 —— 它被 curated 的既有测试直接断言,
        改掉等于让"curated 有 Pass 2"这条无据可依。所以把"两遍"这件事
        写进这张显式的表:

            curated    (False, True)    ← 两遍, 与 H4-E 的遍序一致
            generated  (False, True)    ← G4-B 新增
            未知池     (False,)         ← 行为不变(测试替身 / 老实例)

        ⚠️ **`soft_ok` 的语义**(见 `_candidate_block_reason_locked`):
        `False` = Pass 1 = **完整门**(有冲突就不选它);
        `True`  = Pass 2 = 忽略纯 diversity 维度。
        两个池的**遍序也必须一致**: 先偏好不撞的, 再兜底撞的。早先
        curated 写过 `(False,)` —— 那是把 Pass 2 整条删掉了, 于是
        "配额占满仍可播"立刻退回 0(测试当场红)。**两遍是一个前后关系,
        删掉后一遍不是"更严格", 是让 H4-E 治过的病复发。**
        """
        return (False, True)

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
        p.pool_kind = "generated"        # H4-E: 显式身份(不猜路径)
        p.load()
        return p

    @classmethod
    def open_curated(cls, cfg: Any, rng: Optional[random.Random] = None
                     ) -> Optional["PuzzlePool"]:
        """建 **curated** 池并载入(Batch H2-F)。

        ## 为什么不复用 `open()`

        `PuzzlePool` 的路径是**构造时**从 cfg 读的(`self.pool_path`),
        所以"用另一份文件"必须是一个**不同的实例**。而直接把 cfg 的
        `pool_path` 改掉再 `open()` 是危险的: cfg 是全局共享的,
        改它等于把 AI 生成池也指到 curated 文件上 —— 两条链会互相
        污染, 而且这种错误在测试里很难发现(它们各自都"能工作")。

        所以这里显式拷贝一份**浅**配置对象, 只覆盖三个路径字段。
        浅拷贝足够: 我们只读不写, 且 `PuzzlePool` 只存了 cfg 的引用
        用于读 quota / 路径。

        ## 为什么 curated 用**独立**的 used 账本

        `pool_used.jsonl` 记的是"交付过哪些题"。两个池的题集不相交
        (来源不同), 但**共用一份账本**会让:
          1. 任一文件损坏 -> 两个池一起 fail closed(H2 的 curation 白做);
          2. "curated 播了几道"没法单独统计。
        分开之后任一个坏掉只影响它自己 —— 而 curated 池恰恰是我们
        最不希望被 AI 池的磁盘问题拖垮的那个。

        `curated_pool_enabled=False` -> 返回 None(完全关闭)。
        """
        if not getattr(cfg, "curated_pool_enabled", True):
            log.info("curated_pool_enabled=False: curated 池关闭")
            return None
        import copy as _copy
        sub = _copy.copy(cfg)
        sub.pool_path = str(getattr(cfg, "curated_pool_path", "") or "")
        sub.pool_used_path = str(getattr(cfg, "curated_used_path", "") or "")
        # `pool_enabled=False` 时 curated 也一并关掉 —— 它是**总开关**,
        # 不该被绕过(与 prefer_curated 无关: 那个只管取题顺序)。
        sub.pool_enabled = bool(getattr(cfg, "pool_enabled", True))
        p = cls(sub, rng=rng)
        # H4-E: curated 身份 —— 它启用 Pass 2 soft diversity(见 `_soft_diversity`)。
        # 写在这里而不是 `sub` 上: cfg 可能被 `open()` 按引用共享。
        p.pool_kind = "curated"
        p.load()
        log.info("curated 池载入: %d 道(库存 %d)", p.size(), p.stock_count())
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
            # 墓碑要**先收齐**再读行 —— 否则同一道题的孤儿行会先被读成
            # 合法 spec, 而作废它的墓碑在它后面。见 `_voided_keys`。
            voided = self._voided_keys(pool_recs)
            if voided:
                log.info("池内有 %d 个被墓碑作废的 pool_key", len(voided))
            for i, rec in enumerate(pool_recs):
                spec = self._spec_from_record(rec, voided, i)
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
    def _voided_keys(rows: list) -> set:
        """收集被墓碑作废的 `pool_key` —— **只作废墓碑之前**的那些行。

        ## 墓碑为什么必须指名 key, 又为什么必须看**位置**

        curated 入池是"先落池/署名, 最后写 accepted 决策"。署名写不下
        去时无法删除已经追加的那一行(append-only), 于是追加一条墓碑。

        早先的实现只写 `void: True` **不带**指向, 而读侧见到 void 行就
        整行跳过 —— 那看起来能用, 其实是错的:

            同一道题重试成功后, 池里会有**两行** `pool_key` 相同的记录
            (孤儿 + 新的那行)。孤儿那行**没有** void 标记, 所以它仍然
            会被读成一个合法 spec -> 同一道题进池两次。

        所以墓碑必须**指名** key。但光指名还不够 —— 还必须**只看它之前**
        的行, 否则会把重试写下的新行一起作废:

            第 N 行   孤儿(无 void)      <- 应作废
            第 N+1 行 墓碑(指名该 key)    <- 分界线
            第 N+2 行 重试成功的新行      <- **不该**作废

        注意"新行的 key 与孤儿不同"**不能**作为理由: 重试可能产出**内容
        完全相同**的 spec(`spec_key` 是内容哈希), 那时两行 key 一模一样。
        位置是唯一可靠的区分。

        所以返回的是"每个被墓碑指名的 key 的**最早墓碑行号**" —— 读侧
        据此只作废该行号**之前**的同 key 行。
        """
        first_tombstone: dict = {}
        for i, r in enumerate(rows or []):
            if isinstance(r, dict) and r.get("void") and r.get("pool_key"):
                k = str(r["pool_key"])
                if k not in first_tombstone:
                    first_tombstone[k] = i
        return first_tombstone

    @staticmethod
    def _spec_from_record(rec: Any, voided: Optional[dict] = None,
                          index: int = -1) -> Optional[PuzzleSpec]:
        """池记录 -> PuzzleSpec。不合规就返回 None(跳过)。

        ## `void: True` 的墓碑行(H3-D §四)

        墓碑作废的是**它之前**的同 `pool_key` 行(见 `_voided_keys`)。
        调用方必须把墓碑收齐、并把行的**序号**一起传进来, 否则同一道题
        会既留下孤儿行又留下新行 —— 那就重复了。

        `voided=None` 退化成"只跳过墓碑行本身", 那只对"没有重试"的场景
        成立; 生产路径(load / 预热 CLI)一律传。
        """
        if not isinstance(rec, dict):
            return None
        if rec.get("void"):
            log.info("池记录被作废(墓碑): pool_key=%s reason=%s",
                     rec.get("pool_key"), rec.get("void_reason"))
            return None
        if voided:
            key = str(rec.get("pool_key") or "")
            tomb = voided.get(key)
            # 只作废**墓碑之前**的行 —— 之后的同 key 行是重试的产物。
            if tomb is not None and 0 <= index < tomb:
                log.info("池记录因墓碑作废(第 %d 行, 墓碑在第 %d 行): %s",
                         index, tomb, key)
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

    def stock_signatures(self,
                         limit: Optional[int] = None) -> list:
        """**只读**快照: 当前库存题(能进 `pop_next` 候选集的那些)的
        `PuzzleSignature` 列表。

        ## 为什么需要它(G4-B)

        离线预热(`prefill_pool.py`)要在还没有观众的时候给自己造一个
        "最近看过什么"的窗口 —— 否则它会连补 5 道**结构完全相同**的题,
        而这 5 道互相挡着, `playable_count` 仍然是 1。预热看起来达标,
        真开播时库存立刻塌掉。

        之前的实现是预热脚本**直接摸 `self._items`**, 然后:
            if isinstance(rec, dict): sig = rec.get("signature")
        `_items` 里装的是 `PuzzleSpec` **对象**, 不是 dict —— 于是
        `isinstance` 恒为假, 这个函数**永远返回 []**。预热的跨题约束
        从来没有生效过。

        直接读 `spec.signature` 也是错的: `_items` 里还躺着
        **旧 policy 的隔离题**与**已经 used 的题**, 它们会污染 prefill
        的 recent, 让预热去避一堆根本不会被播的 pair。

        所以新增这个公开方法, 语义与 `stock_count()` **完全对齐**:
            not used  +  过 `_validate_pool_spec()`(含 quality policy 门)

        ## 纯只读

        绝不改 `_used` / 绝不 `_persist_used` / 绝不 `shuffle` / 绝不
        `pop`。返回的是 `PuzzleSignature` **副本**(`from_dict(to_dict())`),
        调用方拿到的对象与池内对象不共享任何可变状态 —— 否则预热脚本
        顺手改一下 signature 就会**直接改掉池子里的题**。

        `limit`: 数够就早退(同 `stock_count`)。

        不抛异常(与本模块其他公开方法一致)。
        """
        from .puzzle import PuzzleSignature
        out: list = []
        try:
            with self._lock:
                for s in self._items:
                    if limit is not None and len(out) >= limit:
                        break
                    if spec_key(s) in self._used:
                        continue
                    try:
                        ok, _ = self._validate_pool_spec(s)
                    except Exception:               # noqa: BLE001
                        log.exception("签名快照校验异常, 该题跳过")
                        continue
                    if not ok:
                        continue
                    sig = getattr(s, "signature", None)
                    if sig is None:
                        continue
                    try:
                        out.append(PuzzleSignature.from_dict(sig.to_dict()))
                    except Exception:               # noqa: BLE001
                        log.exception("签名快照序列化异常, 该题跳过")
                        continue
        except Exception:                           # noqa: BLE001
            log.exception("stock_signatures 异常, 返回已收集的部分")
        return out

    def _candidate_block_reason_locked(self, spec: PuzzleSpec,
                                       recent: Optional[list],
                                       avoid: Optional[list],
                                       quotas: "Quotas",
                                       used_texts: list,
                                       soft_ok: bool = False) -> str:
        """这道候选**此刻**能不能播? 返回阻塞原因, 空串 = 能播。

        ## 为什么必须抽出来

        这是 `pop_next()` 与 `playable_count()` **共用**的那扇动态门。
        两处各写一遍的后果是很具体的: 补池按 `playable_count()` 判断
        "还有一道能播"于是不补, 而 `pop_next()` 多一道门把它挡了 ——
        直播现场直接回落现场生成, 而补池全程以为自己很健康。门一旦
        分成两套就会漂, 所以这里**只有**一份。

        查两件事, 顺序与 `pop_next` 一致:
          ② `cross_puzzle_gate` —— 与**当前** recent 窗口的分布冲突?
          ③ `too_similar`       —— 谜面与最近出过的太像?

        ① (`_validate_pool_spec`) 与 ②③ 的**调用时机不同**: 前者是静态
        准入(与窗口无关, 所以 `stock_count` 也跑它), 后两者依赖当下窗口。
        调用方负责先跑 ①, 因为它要区分"这道题根本不合格"和"这道题只是
        此刻被窗口挡住"—— 两种情况下 `blocked` 的日志/语义不同。

        ⚠️ `used_texts` 由调用方算好传进来(`avoid + self._avoid_extra`),
        不在这里读 `self._avoid_extra` —— `playable_count()` 要在同一把
        锁里对 N 道候选复用同一份, 每次重算就是 O(N·池大小)。

        ## `soft_ok`: 两遍选择(见 `_passes()`)的第二遍

        `False` = 完整门: 分布配额(soft)与结构等价都挡。
        `True`  = 只保留 **hard** 门: 忽略 `check_signature` 的全部
                         配额维度与 `is_structurally_duplicate`。

        依据: **同 mechanism_family + solution_shape != 同一道题**。结构相似
        是多样性偏好; 而"一道都播不了、掉进 fallback 循环"是事故(实播 22 题
        只出了 5 道 curated)。

        ⚠️ G4 起 curated 与 generated **都**用这条阶梯 —— 见 `_passes()`。

        ⚠️ `soft_ok=True` **绝不**放宽 `too_similar` —— 那是文本 near-duplicate,
        是 identity/dedupe, 不是 diversity。同理 `_validate_pool_spec`(policy
        兼容 / curated 授权 / used)也在两遍之外, 由调用方先跑。
        """
        # ② 与当前分布冲突? —— 两种口径都走 `cross_puzzle_gate_split`
        #    (hard/soft 分区), 保证与 `cross_puzzle_gate` 同源不漂。
        hard, soft = cross_puzzle_gate_split(
            spec, recent, quotas, spec.blueprint)
        bad = list(soft) + list(hard) if not soft_ok else list(hard)
        if bad:
            return bad[0]
        # ③ 谜面与最近出过的太像? —— **两遍都挡**(identity, 不是 diversity)
        if too_similar(spec.puzzle, used_texts):
            return "与已出过的太像"
        return ""

    # ------------------------------------------------------------------
    def playable_count(self,
                       recent_signatures: Optional[list] = None,
                       avoid: Optional[list] = None,
                       limit: Optional[int] = None) -> int:
        """**此刻**调用 `pop_next(recent, avoid)` 能实际交付几道题。

        ## 与 `stock_count()` 的分工(两个指标, 不要合并)

            stock_count    = 长期有效库存   —— 与时间窗口无关
            playable_count = 下一题此刻能播 —— 依赖当下 recent/avoid

        实播踩到的正是两者的差: 池里 6 道候选**全部**被当前窗口挡住,
        于是回落现场生成(观众干等 10–40 秒), 而 `stock_count()==6`
        让补池认为库存健康, 一道都不补。补池要看的是这个数。

        `stock_count` 的语义**刻意不变**: 它回答"库存有没有", 而
        "被当前窗口挡住"的题**仍然是库存**(等最近 N 题滚过去就能用)。
        把 dynamic 门扣进 stock 会让补池在窗口拥挤时狂补 —— 那时盘上
        其实已经堆满了。所以是**新增一个指标**, 不是改老的那个。

        ## 纯只读

        绝不 `_persist_used` / 绝不改 `_used` / 绝不 `shuffle` / 绝不
        `mark_used`。它会被 4Hz 的 tick 调用, 任何写都会污染 used ledger
        —— 而 used 是"重启后已播的题不复活"唯一的账本(见模块 docstring)。
        为此它**不**复用 `_pop_next_locked()`(那个交付时会写 used),
        而是与它共享上面那扇 `_candidate_block_reason_locked()`。

        `limit`: 数够就早退(同 `stock_count`), 补池只需要判断
        ">= playable_min", 不需要精确值。

        不抛异常(与本模块其他公开方法一致)。
        """
        try:
            return self._playable_count_locked(
                recent_signatures, avoid, limit)
        except Exception:                       # noqa: BLE001
            log.exception("playable_count 异常, 按 0 处理")
            return 0

    def _playable_count_locked(self, recent: Optional[list],
                               avoid: Optional[list],
                               limit: Optional[int]) -> int:
        with self._lock:
            # fail closed: 账本不可信时 pop_next 一道都不交付, 所以此刻
            # 能播的就是 0。若这里返回非零, 补池会以为"还有得播"而
            # 停止补池 —— 而实际一道都交付不出去。
            if not self._used_trustworthy:
                return 0
            quotas = Quotas.from_config(self.cfg)
            used_texts = list(avoid or []) + self._avoid_extra
            n = 0
            # G4-B: 两遍 —— Pass 1 偏好 diversity, Pass 2 忽略纯 diversity。
            # 必须与 `_pop_next_locked` 的遍序**完全一致**, 否则"一个说能播、
            # 一个交付 None"的 bug 会以新形式复活(P0-3)。
            passes = self._passes()
            for soft_ok in passes:
                for s in self._items:
                    if limit is not None and n >= limit:
                        break
                    if spec_key(s) in self._used:
                        continue
                    # ① 静态准入 —— **与 stock_count 同一扇门**, 所以
                    #    quality policy 隔离的题在这里同样不计入。
                    ok, _ = self._validate_pool_spec(s)
                    if not ok:
                        continue
                    # ②③ 动态门 —— 与 pop_next 同一份实现。
                    if self._candidate_block_reason_locked(
                            s, recent, avoid, quotas, used_texts, soft_ok):
                        continue
                    n += 1
                if n:
                    # Pass 1 有结果就不进 Pass 2 —— 两遍是**先后**关系,
                    # 不是求和(Pass 1 的题已在 n 里, 不能再算一遍)。
                    break
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

        # ---- curated 准入政策门(H3-A) ----
        #
        # curated 题**额外**受一条独立政策约束。它与上面的 quality policy
        # 是两件事:
        #
        #   quality_policy_version  "这道题的内容质量达不达标"
        #   curated_policy_version  "按哪一版**题型定义**收进来的"
        #
        # 为什么非要有第二条: 我们收紧的恰恰是后者。H2 的九条判据把
        # "卡车烧油变轻"这种单点物理脑筋急转弯放了进来 —— 它不是质量
        # 问题(叙事真实 / 通关合同 / 层次全都合格), 是**题型定义太宽**。
        # 质量政策版本没变, 所以光靠上面那条门, 旧题会一直合法。
        #
        # 只有 source_type == "curated" 的题才查这条(**不**去要求自由
        # 生成的题声明 curated 政策 —— 那字段对它们没有意义)。
        #
        # 空串 / 缺失 / 不等于当前版本 -> 隔离。H2 那批(v1, 含 q10000)
        # 落盘时没写这把键, 于是**自动**失去 live eligibility:
        #   不计 stock_count / 不能 add / 不被 pop_next 返回。
        # 不需要删行, 也不需要任何人工清理 —— 与 quality policy 那扇门
        # 完全同构。
        if str(getattr(spec, "source_type", "") or "") == "curated":
            from tools.curated_compiler import CURATED_POLICY_VERSION
            cpv = str(getattr(spec, "curated_policy_version", "") or "")
            if not cpv:
                return False, ("curated 政策版本缺失(spec 没声明按哪一版"
                               "题型定义收的)")
            if cpv != CURATED_POLICY_VERSION:
                return False, (f"curated 政策不兼容(spec={cpv!r}, "
                               f"current={CURATED_POLICY_VERSION!r})")
            # ---- §四: 没有 accepted 决策的 curated 题不可播 ----
            #
            # 决策账本是**最终 commit marker**。生成链先落池/署名, 最后
            # 才写 accepted —— 所以"池里有行但账本没有 accepted"是一个
            # **正常会出现的中间状态**(写 accepted 前进程被杀)。
            #
            # 这一条就是把那个中间状态挡住的地方。没有它的话:
            #
            #     落池 -> 署名 -> [这里被杀] -> 下次启动
            #     -> 池里那行看起来完全合法 -> 播出去
            #     -> 而账本认为它从未被接受, 下次还会重新审、重新写
            #     -> 同一道题进池两次
            #
            # 不变量(§四):
            #     playable curated item => attribution 存在 => accepted 决策存在
            #     accepted 决策         => active pool item 存在 => attribution 存在
            # 前半条由这里保证; 后半条由 `LazyCurator._commit` 的顺序保证。
            ok_dec, why_dec = _curated_decision_ok(
                getattr(spec, "external_id", ""), cpv,
                getattr(spec, "curated_content_hash", ""))
            if not ok_dec:
                return False, why_dec

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
            # reveal adherence(Step 02 / Batch A closeout): 池的最终准入
            # 也要挡住"target 与 observed 不一致"的题 —— 实时路径已经拦过
            # 一次, 但盘上的记录可能在入池后被改坏, 而且手工灌池完全绕开
            # 实时路径。这是**第二处**(也是最后一处)调用点。
            try:
                ra = validate_reveal_adherence(spec, spec.blueprint)
            except Exception:                   # noqa: BLE001
                log.exception("reveal adherence 校验异常, 拒绝")
                return False, "reveal adherence 校验抛异常"
            if ra:
                return False, f"reveal 未执行调度目标({'; '.join(ra)[:120]})"
        return True, ""

    # ------------------------------------------------------------------
    def activate_committed(self, spec: PuzzleSpec) -> bool:
        """把一道**已经落盘并被 accepted** 的 curated 题挂进**当前内存池**。

        ## 为什么必须有这个 API(H3-D3 §一-1)

        Lazy Curator 的提交序列是:

            落池行 -> 落署名 -> 写 accepted 决策

        但 `PuzzlePool` 是**启动时 load 到内存**的(`_items` 是那一刻的
        快照)。于是在写盘成功之后, 当前这场直播的内存池**看不见**那道题:
        `stock_count` 不变, `pop_next` 取不到 —— 一道刚被接受的好题要等
        **下次重启**才生效。离线预热时这个问题被掩盖了(预热跑完就退,
        下次启动自然读得到), 但直播里它是"补了等于没补"。

        ## 它凭什么能直接挂, 而不必再写一次盘

        调用方(`LazyCurator._commit`)已经**写完池行了**, 所以这里**不能**
        再走 `add()` —— 那会写第二行, 同一个 `pool_key` 在文件里出现两次
        (读侧虽然会去重, 但那是靠 `load()` 的 `_keys` 兜的, 属于"错得
        不严重", 不是"对")。

        所以本方法只做**内存侧**的挂载:

            ① 重跑 `_validate_pool_spec`(**不是**信任调用方)
            ② 去重(池内已有 / 已用过 -> 拒绝)
            ③ 追加 `_items` / `_keys`

        第 ① 步不能省: 它是"池内每一道都过得了准入门"这条不变量的**唯一**
        维护点。绕过它直接 append `_items` 会造出一道**播得出来但过不了
        门**的题 —— 那正是 §一-1 明确禁止的写法("不要直接从 LazyCurator
        改 `_items`")。

        ## 返回

        True = 现在就能被 `pop_next` 取到。False = 没挂上(原因已 log)。

        **不抛**: 它是提交路径的一环, 抛出去会打断 curator 的收尾。
        """
        try:
            ok, why = self._validate_pool_spec(spec)
            if not ok:
                log.warning("activate_committed 拒绝(准入门不过): %s", why)
                return False
            with self._lock:
                k = spec_key(spec)
                if k in self._keys:
                    # 已经在内存里 —— 幂等成功(重试路径会走到这里)。
                    log.info("activate_committed: 内存池已有同一道题, 跳过")
                    return True
                if k in self._used:
                    log.warning("activate_committed 拒绝: 这道题已经用过了")
                    return False
                self._items.append(spec)
                self._keys.add(k)
                log.info("activate_committed: 本场立即可播 -> %s…",
                         str(spec.puzzle)[:30])
                return True
        except Exception:                       # noqa: BLE001
            log.exception("activate_committed 异常(该题本场不生效)")
            return False

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
            # "该避开的谜面": 调用方给的 avoid(当前窗口) + 池子自己记的
            # (跨进程/跨场次存活, 见 `remember_avoid`)。**算一次** ——
            # 它的内容在循环里不变, 而 `too_similar` 是 O(池大小 × 历史)。
            used_texts = list(avoid or []) + self._avoid_extra
            blocked: list[str] = []
            # G4-B: 两遍 —— 与 `_playable_count_locked` 同一份阶梯。
            # 遍序必须与它**完全一致**(P0-3)。
            passes = self._passes()
            for soft_ok in passes:
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
                    # ②③ 动态门 —— **与 `playable_count()` 共用一份实现**。
                    #     分成两套的话, 补池会按其中一个数判断"还够播"而
                    #     另一个数把它挡住, 两边永远对不上。
                    bad = self._candidate_block_reason_locked(
                        spec, recent, avoid, quotas, used_texts, soft_ok)
                    if bad:
                        blocked.append(f"{spec.puzzle[:20]}…: {bad[:60]}")
                        continue
                    # ---- 先落盘再交付(见模块 docstring 的不变式) ----
                    if not self._persist_used(spec, aired=False):
                        # 记不下来就**不交付** —— 宁可不播, 也不冒"重启后
                        # 同一道题再播一次"的风险。
                        log.warning("used 记不下来, 放弃这道题(回落现场生成)")
                        blocked.append(f"{spec.puzzle[:20]}…: used 写失败")
                        continue
                    self._used.add(spec_key(spec))
                    if soft_ok:
                        # G4-B: 这一道是 Pass 1 挑不出、靠忽略 diversity
                        # 才拿到的。计一次 —— 见 `diversity_reject_count`。
                        self.diversity_reject_count += 1
                        log.info("题池出题(Pass 2: 忽略纯 diversity): %s…",
                                 spec.puzzle[:30])
                    else:
                        log.info("题池出题: %s…", spec.puzzle[:30])
                    return spec
                # 这一遍一道都没交付 -> 自然进入下一遍(Pass 2 放宽 diversity)。
                # 注意: Pass 2 只是再看一遍同一批候选, 只不过 soft 维度不再
                # 阻塞; **绝不**因此绕过 ①(静态准入)与 used 写入。

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
                "playable": self.playable_count(),
                # 上面那个是**长期库存**; 这个是**此刻能不能交付**。
                # 两个都打: `stock=5 playable=0` 正是那场"候选全被挡、
                # 回落现场生成、观众干等"的现场指纹, 只打 stock 看不出来。
                # 这里**不带** recent/avoid(拿不到当前窗口), 所以它是
                # "池子自身 + 账本"下的可播数, 是 `playable_count` 的
                # 上界; 补池走的是带窗口的那个重载。
                "used": len(self._used),
                "aired": len(self._aired),
                "trustworthy": self._used_trustworthy,
                "path": self.pool_path,
                "used_path": self.used_path,
            }
