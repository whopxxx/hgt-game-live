#!/usr/bin/env python
# coding: utf-8
"""Gift-family 原始 protobuf capture(Step 12C)。

## 为什么要把原始字节落盘

现在能看到的只有 method 计数。而"收到 Gift-family method 了, 但 parse
失败 / 解析出的字段不对"这两种情况, **计数上是同一个样子** —— 没有原始
payload 就只能猜。所以诊断模式把疑似 Gift 的 payload 原样存 `.bin`,
留给离线分析(以及下一阶段的语义分析)。

思路借鉴公开实现 `douyin-live-toolkit` 的 ws_capture, 但**没有复制其
代码** —— 这里只需要"按规则挑 payload + 落盘 + 记索引"这三件事。

## 落盘规则

    method 名包含 `Gift`      -> 落盘(Issue §E)
    parse_error 的 payload    -> 落盘(Issue §E)
    其余 method               -> **只计数**, payload 直接丢弃

第三条是刻意保守的: 全量 dump 会让磁盘在一场直播里长出几百 MB, 而且里面
多是排行榜/统计这类纯噪音。本轮要回答的问题用不到它们。

## 限额(两道, 缺一不可)

1. **每 (profile, method) 最多 `max_per_method` 个样本**(默认 20)。
   单有条数上限还不够 —— 一场直播里 `WebcastGiftMessage` 可能来几千条,
   而第 21 条携带的信息量与第 1 条几乎没有差别, 所以超出的直接跳过,
   并把跳过数记进 summary(`capture_skipped_count`)。
2. **总目录字节上限 + 总文件数上限**。这是防"撞爆磁盘"的硬闸: 即使
   攻击者(或平台的一次异常推送)用巨大 payload 打满, 达到上限后本进程
   不再写任何新文件, 而不是把盘写满。

## ⚠️ 隐私与凭据

原始 payload 里**可能带用户资料**(昵称、头像 url、uid)。所以:

- 落盘目录默认 `data/gift_probe/**`, 必须 gitignored;
- 测试**不得**使用真实 payload / 真实用户名(一律造哨兵);
- PR **不得**提交现场 probe 文件。

`index.jsonl` 只记 Issue 列出的那 8 个字段 —— 不记 cookie、不记 payload
内容、不记服务端回来的任何文本。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Optional

#: 默认落盘根目录。`data/gift_probe/**` 已被 .gitignore 覆盖。
DEFAULT_PROBE_ROOT = os.path.join("data", "gift_probe")

#: 每个 (profile, method) 默认保留多少个样本。
DEFAULT_MAX_PER_METHOD = 20

#: 总目录默认上限。20MB 足够放下几百条 Gift 样本, 又远小于"撑爆磁盘"
#: 的量级 —— 一场直播的弹幕 JSONL 本身通常就是这个数量级。
DEFAULT_MAX_TOTAL_BYTES = 20 * 1024 * 1024

#: 总文件数上限。即使每条 payload 都很小, 也不让文件系统里长出十万个
#: 小文件(那会让后续离线分析本身变得很痛苦)。
DEFAULT_MAX_TOTAL_FILES = 2000

#: `parse_status` 的两个取值。
PARSE_STATUS_OK = "ok"
PARSE_STATUS_ERROR = "error"
#: 未尝试解析(非 primary GiftMessage 的 Gift-family method —— 例如
#: `WebcastGiftSortMessage`)。**不要**把它记成 `ok`, 那会把"没解析"
#: 洗成"解析成功"。
PARSE_STATUS_NOT_ATTEMPTED = "not_attempted"


def _safe_component(name: str, fallback: str = "unknown") -> str:
    """把任意字符串变成安全的**单层**路径片段。

    两个理由, 都很实际:

    1. **路径穿越**: `name` 最终来自服务端下发的 method 字符串。若不
       过滤, `../../etc/x` 这类名字会把文件写到 probe 目录**外面** ——
       Issue 明确要求 probe 目录不得写入 production 数据路径。这里把
       非白名单字符全部换成 `_`, 于是 `/` 与 `..` 在结构上不可能出现。
    2. **Windows 保留名 / 大小写文件系统**: 统一小写 + 白名单, 顺带避免
       `CON`、`NUL` 这类在 Windows 上打不开的名字。

    空输入退化为 `fallback`, 而不是产生一个空路径片段(那会静默写到
    父目录)。
    """
    raw = str(name or "").strip().lower()
    out = []
    for ch in raw:
        if ch.isalnum() or ch in "-_.:":
            out.append(ch)
        else:
            out.append("_")
    safe = "".join(out).strip("._")
    # `..` / `.` 会在上面被 strip 掉两端的点; 但仍显式挡一次 —— 这两条
    # 路径如果从别处绕回来, 代价是写到 probe 目录外面。
    if safe in ("", ".", ".."):
        return fallback
    return safe[:80]


#: production 数据文件所在的目录。probe 根目录**不得**与它相同 ——
#: 那样 probe 文件(含原始 payload 与用户资料)会与 pool/played/puzzle
#: 落在同一层, 既污染 production 目录语义, 也让 .gitignore 的
#: `data/gift_probe/**` 规则不再覆盖它们。
PRODUCTION_DATA_DIR = os.path.join("data")

#: production JSONL 文件名。只用于把"probe 目录里不许出现这些名字"这条
#: 要求写成**可执行的**检查(而不是只写在注释里)。
PRODUCTION_DATA_FILES = ("pool.jsonl", "played.jsonl", "puzzle.jsonl",
                         "danmaku.jsonl")


def _session_id_component(session_id: str) -> str:
    """session id 也走同一套过滤 —— 它同样来自命令行/环境, 不是受信输入。"""
    return _safe_component(session_id, fallback="session")


class RawCaptureStore:
    """把一个 profile 的原始 payload 写到它**独占**的目录下。

    ⚠️ 每个 profile 必须持有**自己的** store 实例与目录:
    `data/gift_probe/<session-id>/<profile>/`(Issue §A 推荐目录)。
    两路共用一个目录会让"哪一路收到的"这个问题在文件层就无法回答 ——
    index.jsonl 里虽然有 profile 字段, 但一旦有人按目录做批量分析, 混在
    一起的样本会被当成同一路。

    本类**不做任何解析**, 也不读 payload 内容 —— 它只负责选、写、记索引。
    """

    def __init__(self, root: str, session_id: str, profile_id: str, *,
                 max_per_method: int = DEFAULT_MAX_PER_METHOD,
                 max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
                 max_total_files: int = DEFAULT_MAX_TOTAL_FILES,
                 now_fn=None):
        self.session_id = str(session_id or "")
        self.profile_id = str(profile_id or "")
        self.dir = os.path.join(
            str(root), _session_id_component(self.session_id),
            _safe_component(self.profile_id))
        self.index_path = os.path.join(self.dir, "index.jsonl")
        try:
            self.max_per_method = max(0, int(max_per_method))
        except (TypeError, ValueError):
            self.max_per_method = DEFAULT_MAX_PER_METHOD
        self.max_total_bytes = max(0, int(max_total_bytes))
        self.max_total_files = max(0, int(max_total_files))
        #: `(profile, method) -> 已落盘样本数`。**按 method 分别计数** ——
        #: 用一个全局计数会让"Gift 来了 20 条把 ComboGift 的额度也吃掉",
        #: 于是某个 method 明明有样本却一个都没留下。
        self._per_method: dict = {}
        self._total_bytes = 0
        self._total_files = 0
        self.max_per_method_hits = 0
        self.skipped_for_limits = 0
        self.write_errors = 0
        # 时钟可注入 —— 与项目里其它模块同一纪律, 让测试完全确定。
        self._now = now_fn or (lambda: time.time())
        self._seq = 0
        # 目录**立刻**建出来, 不等第一条 payload。
        #
        # 为什么不等: "三路都连上了、但一条 Gift 都没有"是本轮**最可能**
        # 出现的结论(而且它本身就是有价值的结论)。若目录要等到有 payload
        # 才出现, 那个结论在现场就变成一个空目录 —— 与"程序根本没起来"
        # 长得一模一样。空目录 + 一份 session 摘要才分得清这两种情况。
        self._ensure_dir()

    def _ensure_dir(self) -> bool:
        """建目录。失败不抛(诊断不因磁盘问题炸掉), 但要有痕迹。"""
        try:
            os.makedirs(self.dir, exist_ok=True)
            return True
        except OSError:
            self.write_errors += 1
            return False

    # ------------------------------------------------------------------
    def should_capture(self, method: str, parse_error: bool = False) -> bool:
        """这条 payload 该不该落盘。

        纯判断, 不产生副作用 —— 于是"命中规则"与"真的写下去了"可以分别
        断言(达到上限时前者为 True、后者不发生)。

        ⚠️ 这条函数是 Issue 测试点 6/7/8 的唯一真相来源: 它一旦松掉
        (`return True`), 非 Gift method 会被全量 dump; 一旦紧掉, Gift
        就采不到。
        """
        from .profile import is_gift_family_method
        if parse_error:
            return True
        return is_gift_family_method(method)

    def capture(self, payload, *, method: str, connection_generation: int = 0,
                envelope_msg_id=0, parse_status: str = PARSE_STATUS_OK,
                parse_error: bool = False) -> Optional[str]:
        """按规则落盘一条 payload; 返回文件名, 未落盘返回 None。

        任何失败(磁盘满 / 权限 / 编码)都**吞掉并计数**, 不往上抛:
        这是一个诊断旁路, 它绝不该有能力把直播连接搞挂(Issue §A:
        "不因一条诊断连接失败而杀死主直播")。
        """
        try:
            return self._capture(payload, method=method,
                                 connection_generation=connection_generation,
                                 envelope_msg_id=envelope_msg_id,
                                 parse_status=parse_status,
                                 parse_error=parse_error)
        except Exception:                       # noqa: BLE001
            self.write_errors += 1
            return None

    # ------------------------------------------------------------------
    def _capture(self, payload, *, method, connection_generation,
                 envelope_msg_id, parse_status, parse_error):
        if payload is None:
            return None
        blob = bytes(payload)
        if not self.should_capture(method, parse_error=parse_error):
            return None

        safe_method = _safe_component(method, fallback="unknown_method")
        key = (self.profile_id, safe_method)
        used = self._per_method.get(key, 0)
        if used >= self.max_per_method:
            self.max_per_method_hits += 1
            self.skipped_for_limits += 1
            return None
        if self._total_files >= self.max_total_files:
            self.skipped_for_limits += 1
            return None
        if self._total_bytes + len(blob) > self.max_total_bytes:
            self.skipped_for_limits += 1
            return None

        self._ensure_dir()
        self._seq += 1        # 文件名里带 method 与序号, 便于人工翻目录时一眼看出是哪个 method
        # 的第几条。序号**不用时间戳** —— 同毫秒两条 payload 会撞名, 而
        # 撞名在这里的后果是静默覆盖掉刚采到的样本。
        fname = f"{safe_method}-{self._seq:04d}.bin"
        with open(os.path.join(self.dir, fname), "wb") as f:
            f.write(blob)

        self._per_method[key] = used + 1
        self._total_files += 1
        self._total_bytes += len(blob)
        self._append_index(method=str(method), payload_len=len(blob),
                           filename=fname,
                           connection_generation=connection_generation,
                           envelope_msg_id=envelope_msg_id,
                           parse_status=parse_status)
        return fname

    def _append_index(self, *, method, payload_len, filename,
                      connection_generation, envelope_msg_id, parse_status):
        """写一行 `index.jsonl`。

        ⚠️ 字段表是 Issue §E 的**闭集**, 不要顺手加字段 —— 尤其不要加
        cookie、payload 前若干字节、用户 id 之类。凭据与用户资料都不该
        出现在这个文件里。`payload_sha256` 是唯一"派生的"字段: 它让离线
        分析能识别重复样本, 而 hash 的性质决定了它**无法**被还原成内容。
        """
        rec = {
            "timestamp": self._now(),
            "profile": self.profile_id,
            "connection_generation": int(connection_generation or 0),
            "method": str(method or ""),
            "payload_len": int(payload_len),
            "filename": filename,
            "envelope_msg_id": (str(envelope_msg_id)
                                if envelope_msg_id else ""),
            "parse_status": str(parse_status or ""),
        }
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
        with open(self.index_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()

    # ------------------------------------------------------------------
    def payload_sha256(self, payload) -> str:
        """payload 的 sha256(十六进制)。

        只进离线分析, **不进日志** —— 它是内容的确定性指纹, 对"这条是不是
        重复样本"有用, 但也仅此而已。
        """
        return hashlib.sha256(bytes(payload or b"")).hexdigest()

    def stats(self) -> dict:
        """可安全落盘 / 打日志的用量统计。

        `per_method` 的键是 `(profile, method)` 元组 —— 它在 json 里不是
        合法键, 所以这里转成 `"<profile>/<method>"`。这不只是为了序列化
        好看: 分析侧要按 method 汇总用量, 扁平字符串键比二元组更好用。
        """
        return {
            "profile": self.profile_id,
            "dir": self.dir,
            "files": self._total_files,
            "bytes": self._total_bytes,
            "per_method": {f"{p}/{m}": n
                           for (p, m), n in sorted(self._per_method.items())},
            "max_per_method": self.max_per_method,
            "max_total_bytes": self.max_total_bytes,
            "max_total_files": self.max_total_files,
            "max_per_method_hits": self.max_per_method_hits,
            "skipped_for_limits": self.skipped_for_limits,
            "write_errors": self.write_errors,
        }


def probe_dir_for(root, session_id, profile_id) -> str:
    """`<root>/<session>/<profile>` —— 目录布局的唯一真相来源。

    runner 与测试都从这里取路径, 免得两处各拼一份而其中一处悄悄拼错
    (拼错的后果是两路写进同一个目录, 而日志上完全看不出来)。
    """
    return os.path.join(str(root), _session_id_component(str(session_id)),
                        _safe_component(str(profile_id)))


def assert_outside_production_paths(root: str) -> None:
    """确认 probe 根目录**不在** production 数据路径上。

    Issue 测试点 10: "probe 目录不会写入 production 数据路径"。
    这里把那条要求实现成一个**构造期**检查, 而不是只写在测试里 ——
    测试能挡住 CI, 但挡不住有人手工把 `--gift-probe-dir data` 传进来,
    而那种配置的后果(probe 文件与 production 文件混在同一层)是不可逆的
    混乱, 事后很难区分。

    判据两条, 都是"结构上不可能"而不是"值不等于":

    1. 解析后的绝对路径不得等于 `data/` —— `pool.jsonl` / `played.jsonl`
       / `puzzle.jsonl` / `danmaku.jsonl` 都在那儿, 所以 probe 根目录
       不能就是 `data/`;
    2. probe 目录里的文件名不得是 production 文件名。这条由
       `RawCaptureStore` 生成的名字(`<method>-<seq>.bin` 与
       `index.jsonl`)保证 —— 这里只把该保证写成一个可断言的形式。
    """
    resolved = os.path.abspath(str(root))
    if resolved == os.path.abspath(PRODUCTION_DATA_DIR):
        raise ValueError(
            "gift probe 目录不能是 data/ 本身 —— 那会把 probe 文件"
            "(含原始 payload 与用户资料)和 production 数据放在同一层, "
            "既污染 production 目录语义, 也让 .gitignore 的单条规则"
            "不再能覆盖它们。请用 data/gift_probe 或其它独立目录。")


def probe_paths_are_disjoint(probe_root: str) -> dict:
    """结构性质检查: probe 目录下**不会**出现 production 数据文件名。

    返回一个可断言的字典(而不是抛异常): 它既能在启动时打一条日志, 也能
    被测试直接断言。真正的保证来自 `RawCaptureStore` 的文件名生成规则
    (`index.jsonl` + `<method>-<seq>.bin`), 这里只是把它变成一个显式的、
    可被 mutation 打红的性质 —— "probe 不写 production 数据路径"这条
    要求若只写在注释里, 改坏了没有任何东西会响。

    `would_collide` 必须是**空元组**才算通过: 它的含义是"probe 会生成的
    文件名里, 有几个与 production 数据文件同名"。任何非空值都意味着
    probe 有可能覆盖 production 文件。
    """
    generated = ("index.jsonl", "<method>-<seq>.bin")
    return {
        "probe_root": os.path.abspath(str(probe_root)),
        "production_dir": os.path.abspath(PRODUCTION_DATA_DIR),
        "probe_root_is_production_dir":
            os.path.abspath(str(probe_root))
            == os.path.abspath(PRODUCTION_DATA_DIR),
        "generated_filenames": generated,
        # 逐字同名才算撞。用后缀判 `.bin` 是错的 —— production 里没有
        # `.bin` 文件, 那种写法永远返回空, 于是这条检查永远"绿"。
        "would_collide": tuple(
            n for n in PRODUCTION_DATA_FILES if n in generated),
    }
