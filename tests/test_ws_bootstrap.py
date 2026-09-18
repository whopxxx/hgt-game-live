#!/usr/bin/env python
# coding: utf-8
"""运行: uv run tests/test_ws_bootstrap.py（完全离线, 无网络）。

Step 12B: 本地生成的 WS bootstrap。

钉住两件事:
  1. **纯函数性质**: 同样的输入产生同样的输出; 时间/RNG 完全可注入
     (测试绝不依赖真实时钟或随机数 —— 那会让测试 flaky 且线上无法复现)。
  2. **格式与外部证据一致**: 字段名、分隔符、`base = sec << 32` 的高位
     关系。抄错一个分隔符就会让服务端收不到数据, 而离线测试是唯一能
     提前发现它的地方。

⚠️ 这些断言验的是"我们忠实实现了**外部当前实现**的做法",
**不是**"抖音官方协议定义"。见 `ws_bootstrap.py` 的边界说明。
"""

from __future__ import annotations

import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vendor" / "douyin_fetcher"))

from ws_bootstrap import generate_ws_bootstrap  # noqa: E402

FAIL = [0]


def check(name, cond, extra=""):
    if cond:
        print(f"  ok  {name}")
        return
    print(f"  FAIL {name}  {extra}")
    FAIL[0] += 1


# ======================================================================
def test_is_deterministic():
    """同样的 (room, uid, now_ms, rng seed) -> 完全相同的输出。"""
    print("\n[WB-1] 确定性")
    a = generate_ws_bootstrap(123, "u1", 1789000000000, random.Random(7))
    b = generate_ws_bootstrap(123, "u1", 1789000000000, random.Random(7))
    check("同输入同输出", a == b, (a, b))
    c = generate_ws_bootstrap(123, "u1", 1789000000001, random.Random(7))
    check("时间变 -> 输出变", a != c)
    d = generate_ws_bootstrap(124, "u1", 1789000000000, random.Random(7))
    check("room 变 -> 输出变", a != d)
    e = generate_ws_bootstrap(123, "u2", 1789000000000, random.Random(7))
    check("uid 变 -> 输出变", a != e)
    f = generate_ws_bootstrap(123, "u1", 1789000000000, random.Random(8))
    check("rng 变 -> 随机部分变", a != f)


def test_no_real_clock_or_random():
    """必须可注入 —— 连续两次调用(不注入)也不该改变函数签名契约。

    这条主要防"实现里偷偷读 time.time()/random 全局"这种回归:
    我们传固定的 rng 与 now_ms, 连调两次必须一致(若函数内部读了全局
    随机或时钟, 第二次就会不同)。
    """
    print("\n[WB-2] 不依赖真实时钟/全局随机")
    a = generate_ws_bootstrap(1, "u", 111, random.Random(3))
    b = generate_ws_bootstrap(1, "u", 111, random.Random(3))
    check("连调两次一致", a == b, (a, b))
    # 只传 seed 相同的 Random 时, 输出完全由 seed+输入决定
    c = generate_ws_bootstrap(1, "u", 111, random.Random(3))
    check("第三次仍一致", a == c)


def test_cursor_format():
    """cursor 的字段名与分隔符必须与外部证据一致。"""
    print("\n[WB-3] cursor 格式")
    now = 1789000000000
    got = generate_ws_bootstrap(1, "u", now, random.Random(1))
    cur = got["cursor"]
    check("以 t- 开头", cur.startswith(f"t-{now}_"), cur[:40])
    # t-{now}_r-{r}_d-1_u-1_h-{h}
    m = re.fullmatch(r"t-(\d+)_r-(\d+)_d-1_u-1_h-(\d+)", cur)
    check("整串匹配 t-{now}_r-{r}_d-1_u-1_h-{h}", m is not None, cur)
    if m:
        check("t 段 == now_ms", int(m.group(1)) == now, m.group(1))
        check("含固定段 d-1_u-1", "_d-1_u-1_" in cur, cur)


def test_internal_ext_format():
    """internal_ext 的字段名与顺序必须与外部证据一致。"""
    print("\n[WB-4] internal_ext 格式")
    now = 1789000000000
    got = generate_ws_bootstrap(4242, "uX", now, random.Random(2))
    ext = got["internal_ext"]
    for must in ("internal_src:dim", "wss_push_room_id:4242",
                 "wss_push_did:uX", f"first_req_ms:{now}",
                 f"fetch_time:{now}", "seq:1",
                 f"wss_info:0-{now}-0-0", "wrds_v:"):
        check(f"含 {must[:28]}", must in ext, ext[:120])
    check("用 | 分隔", "|" in ext, ext[:60])
    check("不含旧 2024 值", "1721106114633" not in ext, ext)


def test_high_bits_are_seconds():
    """`r` / `h` / `wrds_v` 的高位必须是 `sec << 32`(外部实现的核心关系)。"""
    print("\n[WB-5] 高位 = 秒级时间戳 << 32")
    now = 1789000000000
    sec = now // 1000
    base = sec << 32
    got = generate_ws_bootstrap(1, "u", now, random.Random(9))
    m = re.fullmatch(r"t-(\d+)_r-(\d+)_d-1_u-1_h-(\d+)", got["cursor"])
    check("cursor 可解析", m is not None, got["cursor"])
    if m:
        r = int(m.group(2))
        h = int(m.group(3))
        check("r >> 32 == sec", (r >> 32) == sec, (r >> 32, sec))
        check("h >> 32 == sec", (h >> 32) == sec, (h >> 32, sec))
        check("r >= base", r >= base, (r, base))
        check("h >= base", h >= base, (h, base))
        # 低位是随机的(不应等于 0 —— 那说明随机没生效)
        check("r 有低位", (r - base) > 0, r - base)
        check("h 有低位", (h - base) > 0, h - base)
    wr = int(got["internal_ext"].split("wrds_v:")[1])
    check("wrds_v >> 32 == sec", (wr >> 32) == sec, (wr >> 32, sec))


def test_never_raises_and_empty_input_is_safe():
    print("\n[WB-6] 异常输入不抛")
    for args in ((None, None, None), ("", "", 0), (1, "u", "bad"),
                 (1, "u", -1)):
        try:
            got = generate_ws_bootstrap(*args, rng=random.Random(1))
            ok = isinstance(got, dict) and "cursor" in got \
                and "internal_ext" in got
        except Exception as e:                      # noqa: BLE001
            ok = False
            print("     抛了:", e)
        check(f"不抛且返回结构完整 {args!r}", ok)


def test_rng_defaults_to_fresh_random():
    """不传 rng 时用新的 Random, 不共享全局状态。"""
    print("\n[WB-7] 不传 rng 也可用")
    a = generate_ws_bootstrap(1, "u", 5)
    b = generate_ws_bootstrap(1, "u", 5)
    check("两次都返回合法结构",
          all(isinstance(x.get("cursor"), str) for x in (a, b)), (a, b))
    # 不传 rng 时随机低位通常不同(概率极高); 但不强断言相等/不等


def main():
    tests = [
        test_is_deterministic,
        test_no_real_clock_or_random,
        test_cursor_format,
        test_internal_ext_format,
        test_high_bits_are_seconds,
        test_never_raises_and_empty_input_is_safe,
        test_rng_defaults_to_fresh_random,
    ]
    for t in tests:
        t()
    print()
    if FAIL[0]:
        print(f"FAILED: {FAIL[0]} 项")
        return 1
    print("PASS: 本地 WS bootstrap(外部实现用法, 非官方协议定义)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
