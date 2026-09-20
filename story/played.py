#!/usr/bin/env python
# coding: utf-8
"""P0: **全局已播账本** —— 任何来源播过的题, 都不得再次进入 QA。

## 为什么需要它(而不是复用 `pool_used.jsonl`)

`pool_used.jsonl` 记的是"**题池交付过**哪些题"。它**不**覆盖另外两条
来源:

    live_generate   现场生成(池空时回落) —— 不进池账本
    fallback        引擎兜底(出题全挂时) —— 不进池账本

而这三条来源都会**真的上屏**。于是"已经播过的题不得再次进入 QA"这条
要求在 live / fallback 两条路径上是**空的** —— 尤其是兜底: 固定 4 道题
按 `index % 4` 轮换, 第 5 次必然重播第 1 道。

`director.py` 里那段注释其实早就点出了这个缺口, 只是当时把它**接受**
成了已知限制:

    "真要做'全局所有播过题的 ledger', 该单独定义, 不要暗中借这个
      文件承担第二个职责。"

这份模块就是那个"单独定义"。

## 契约(与 `pool_used` 同源, 但覆盖面不同)

    追加式         永不重写整个文件(重写中途崩溃 = 全部复活)
    先落盘再上屏    `remember()` 必须在 spec 交出去**之前** fsync 成功
    fail closed     账本读不全 -> `trustworthy=False` -> 调用方一律
                    **不交付任何题**(宁可不播, 不复播)
    跨重启          启动时 load 回内存, 与 `pool_used` 同一套语义

## 为什么按 `spec_key` 而不是 spec.id

与题池完全同一个理由(见 `story.pool.spec_key` 的说明): `gen_spec` 从不
设置 id, 拿它当身份等于所有题都是同一道。所以这里直接复用
`story.pool.spec_key` —— **同一个哈希, 两个账本**, 于是"同一道题在池
账本与全局账本里的身份一致", 不需要第三套身份定义。
"""
from __future__ import annotations

import logging
import os
import time

log = logging.getLogger("story.played")

#: 全局已播账本的默认路径。
DEFAULT_PATH = os.path.join("data", "played.jsonl")


def _append_line(path: str, rec: dict) -> bool:
    """追加一行 JSON 并 fsync。**不抛异常**。"""
    try:
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)
        line = __import__("json").dumps(rec, ensure_ascii=False) + "\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        return True
    except Exception:                       # noqa: BLE001
        log.exception("已播账本写入失败: %s", path)
        return False


class PlayedLedger:
    """"这一道题**真的上屏过**"的全局账本。

    **绝不抛异常** —— 它挂在直播主循环上, 一次磁盘抖动不该让下播。
    读失败时 `trustworthy` 为 False, 调用方据此**拒绝交付**。
    """

    def __init__(self, path: str = "", enabled: bool = False):
        """⚠️ `enabled` 默认 **False**。

        这不是保守, 是**测试隔离**要求: 几百个既有用例直接
        `Config(sim_path="x")` 起引擎(根本没有 tmpdir), 默认开启会让它们
        全部写到仓库真实的 `data/played.jsonl` —— 于是上一例播过的题会把
        下一例的交付挡掉, "同一份代码两种结果"。(同一类问题在 curated
        池上已经踩过一次, 见 `test_pool.mkcfg` 的注释。)

        生产由 `Director` 显式传 `enabled=True` —— 那才是**唯一**需要
        这条纪律的地方。
        """
        self.path = str(path or DEFAULT_PATH)
        self.enabled = bool(enabled)
        self._keys: set = set()
        #: 账本是否可信。读不全时为 False -> 调用方一道都不交付。
        self.trustworthy = True
        self.loaded = 0

    # ------------------------------------------------------------------
    def load(self) -> int:
        """把账本读回内存。返回读到的条数。**不抛**。

        ⚠️ **坏一处就整体不可信**, 与 `pool_used` 完全同一个理由:
        "某道题不在账本里"与"那一行没读出来"从结果上无法区分, 而前者
        意味着**把播过的题再播一次**。所以宁可整份不认。
        """
        self._keys = set()
        self.trustworthy = True
        self.loaded = 0
        if not self.enabled:
            return 0
        if not os.path.exists(self.path):
            # 文件不存在 = 还没播过任何题。这是**正常**的首次启动, 不是
            # 损坏 —— 与"文件在但读不动"必须分开。
            return 0
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for ln, raw in enumerate(f, 1):
                    raw = raw.strip()
                    if not raw:
                        continue            # 空行是写盘中断的产物, 跳过
                    try:
                        rec = __import__("json").loads(raw)
                    except Exception:       # noqa: BLE001
                        log.error("已播账本第 %d 行解析失败, 整份判为不可信",
                                  ln)
                        self.trustworthy = False
                        break
                    k = rec.get("key") if isinstance(rec, dict) else None
                    if not k or not isinstance(k, str):
                        log.error("已播账本第 %d 行没有可用 key, 整份判为"
                                  "不可信: %s", ln, raw[:80])
                        self.trustworthy = False
                        break
                    self._keys.add(k)
                    self.loaded += 1
        except Exception:                   # noqa: BLE001
            log.exception("已播账本读取失败, 判为不可信: %s", self.path)
            self.trustworthy = False
            return 0
        if not self.trustworthy:
            log.error("已播账本不可信(有损坏行/读不了), 本次**不交付任何"
                      "题**(宁可不播, 不复播): %s", self.path)
        else:
            log.info("已播账本载入: %d 条(%s)", self.loaded, self.path)
        return self.loaded

    # ------------------------------------------------------------------
    def has_played(self, spec) -> bool:
        """这道题**真的上屏过**吗?

        ⚠️ 账本不可信时返回 **True** —— 调用方据此拒播。这是 fail
        closed 的落点: "不知道有没有播过"必须当成"播过", 否则一次
        磁盘故障就等于"全部题都可以重播"。
        """
        if not self.enabled or spec is None:
            return False
        if not self.trustworthy:
            return True
        from .pool import spec_key
        try:
            return spec_key(spec) in self._keys
        except Exception:                   # noqa: BLE001
            log.exception("已播账本查 key 异常, 按已播处理")
            return True

    # ------------------------------------------------------------------
    def remember(self, spec) -> bool:
        """记下"这道题要上屏了"。**必须在真正交付之前调用**。

        返回 False 表示**记不下来** —— 调用方**不得**交付这道题(与
        `pop_next` 里 used 写失败就不交付完全同一条纪律)。

        已经记过的直接返回 True(幂等, 不重复写盘)。
        """
        if not self.enabled or spec is None:
            return True
        from .pool import spec_key
        try:
            k = spec_key(spec)
        except Exception:                   # noqa: BLE001
            log.exception("已播账本算 key 异常, 拒绝交付")
            return False
        if k in self._keys:
            return True
        ok = _append_line(self.path, {
            "key": k, "at": time.time(),
            "puzzle": (getattr(spec, "puzzle", "") or "")[:60],
        })
        if ok:
            self._keys.add(k)
        else:
            # 与 pool 的 used 写失败同一条纪律: 记不下来就**不交付**。
            log.error("已播账本写失败, 本题不交付(宁可不播, 不复播)")
        return ok

    # ------------------------------------------------------------------
    def stats(self) -> dict:
        return {"played": len(self._keys), "trustworthy": self.trustworthy,
                "played_path": self.path, "enabled": self.enabled}
