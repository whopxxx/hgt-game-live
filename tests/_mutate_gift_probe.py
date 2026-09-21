#!/usr/bin/env python
# coding: utf-8
"""Step 12C 变异跑手: 每条变异打一个补丁, 跑受影响的套件, 断言**必须红**。

Issue「Mutation verification」点名要求四条:

  1. 故意让 Gift method 在 handler 前被 continue -> 测试必须失败;
  2. 故意把 authenticated summary 打出原始 cookie -> 安全测试必须失败;
  3. 故意关闭 raw Gift capture -> capture 测试必须失败;
  4. 故意让两个 profile 共用 counter -> 隔离测试必须失败。

为什么写成一个脚本而不是手工几次 Edit/undo:
  - 手工漏一次 revert 就会把变异**带进提交**;
  - "哪条变异打红了哪条断言"必须可复现, 否则 PR 里的表格没有根据。

用法: `uv run tests/_mutate_gift_probe.py`

⚠️ 本脚本只改**本 Step 新增的文件**(`story/gift_probe/*`)。它不去动
`danmaku.py` / `liveMan.py` 的锚点 —— 那两处是生产路径, 一次失败的
revert 代价太大, 而且本 Step 的隔离性质本来就保证"诊断能力不落在那里"。
"""

import io
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
#: 本 Step 的套件 + 会被诊断改动波及的既有套件。
SUITE = "tests/test_gift_probe.py"
NEIGHBOUR_SUITES = ("tests/test_ws_cookie.py", "tests/test_ws_bootstrap.py")

#: (编号, 说明, 文件, 原文, 变异后, 期望变红的套件)
MUTATIONS = [
    # ---- 1. Gift method 在 handler 前被 continue ----
    (
        "M-GP-1",
        "Gift method 在 handler 前被 continue(dispatch 层断掉)",
        "story/gift_probe/hooks.py",
        "                if _m == \"WebcastGiftMessage\":\n"
        "                    self._gift_probe_record_semantics_only()\n"
        "                if fn is None:",
        "                if _m == \"WebcastGiftMessage\":\n"
        "                    self._probe_bump(\"unhandled_method_counts\", _m)\n"
        "                    continue\n"
        "                if fn is None:",
        [SUITE],
    ),
    # ---- 2. authenticated summary 打出原始 cookie ----
    #
    # 做在 `now_text` 的**输出端**: 把 auth 的收敛闸 (`sanitize_auth`) 拆掉,
    # 让它直接转发 counter 上的 auth 字段。这条精确对应"某天有人觉得
    # sanitize_auth 多余, 顺手删掉"这个真实风险 —— 那时只要 counter.auth
    # 曾被赋成凭据(见 M-GP-2b), 每一行摘要都会把凭据打出去。
    (
        "M-GP-2",
        "摘要去掉 auth 收敛闸(直接转发 auth 字段)",
        "story/gift_probe/probe.py",
        "            if k == \"auth\":\n"
        "                head_parts.append(f\"auth={sanitize_auth(s.get('auth'))}\")",
        "            if k == \"auth\":\n"
        "                head_parts.append(f\"auth={s.get('auth')}\")",
        [SUITE],
    ),
    (
        "M-GP-2b",
        "auth 字段被赋成原始 cookie 而不是固定词",
        "story/gift_probe/profile.py",
        "    if login_cookie_is_usable(login_cookie):\n"
        "        return {\"auth\": WS_AUTH_AUTHENTICATED, "
        "\"config_state\": PROFILE_OK}",
        "    if login_cookie_is_usable(login_cookie):\n"
        "        return {\"auth\": \"authenticated \" + str(login_cookie),\n"
        "                \"config_state\": PROFILE_OK}",
        [SUITE],
    ),
    (
        "M-GP-2c",
        "sanitize_auth 放行任意值",
        "story/gift_probe/probe.py",
        "    s = str(value or \"\").strip()\n"
        "    if s in (WS_AUTH_AUTHENTICATED, WS_AUTH_ANONYMOUS):\n"
        "        return s",
        "    s = str(value or \"\").strip()\n"
        "    return s",
        [SUITE],
    ),
    # ---- 3. 关闭 raw Gift capture ----
    (
        "M-GP-3",
        "关闭 Gift-family 的 raw capture(只计数不落盘)",
        "story/gift_probe/capture.py",
        "        from .profile import is_gift_family_method\n"
        "        if parse_error:\n"
        "            return True\n"
        "        return is_gift_family_method(method)",
        "        from .profile import is_gift_family_method\n"
        "        if parse_error:\n"
        "            return True\n"
        "        return False",
        [SUITE],
    ),
    (
        "M-GP-3b",
        "parse_error 的 payload 不再 capture",
        "story/gift_probe/capture.py",
        "        from .profile import is_gift_family_method\n"
        "        if parse_error:\n"
        "            return True\n"
        "        return is_gift_family_method(method)",
        "        from .profile import is_gift_family_method\n"
        "        return is_gift_family_method(method)",
        [SUITE],
    ),
    # ---- 4. 两个 profile 共用 counter ----
    #
    # 做在 runner 里(而不是 profile.py): "共用"是一个**装配**事实 ——
    # 每路各自 new 一个 `ProfileCounters` 就是隔离本身。把那里改成"所有
    # 臂共用一个实例", 就精确复现了"两路数据串在一起"这个故障。
    (
        "M-GP-4",
        "所有 profile 共用一个 counter 实例(串路)",
        "story/gift_probe/runner.py",
        "        self.counters = ProfileCounters(\n"
        "            profile.profile_id,\n"
        "            auth=auth_state[\"auth\"],\n"
        "            config_state=auth_state[\"config_state\"])",
        "        _g = globals().setdefault(\"_SHARED_PC\", ProfileCounters(\n"
        "            profile.profile_id, auth=auth_state[\"auth\"],\n"
        "            config_state=auth_state[\"config_state\"]))\n"
        "        self.counters = _g",
        [SUITE],
    ),
    # ---- 附加: 三层计数被合并 / cap 失效 / 预警 ----
    (
        "M-GP-5",
        "每 method 样本上限失效(无限落盘)",
        "story/gift_probe/capture.py",
        "        used = self._per_method.get(key, 0)\n"
        "        if used >= self.max_per_method:",
        "        used = self._per_method.get(key, 0)\n"
        "        if False:",
        [SUITE],
    ),
    (
        "M-GP-6",
        "probe 目录允许是 data/ 本身(污染 production 目录)",
        "story/gift_probe/capture.py",
        "    if resolved == os.path.abspath(PRODUCTION_DATA_DIR):",
        "    if False:",
        [SUITE],
    ),
    (
        "M-GP-7",
        "method 名不做归一化(日志可被换行注入)",
        "story/gift_probe/probe.py",
        "    s = str(method or \"\")\n"
        "    safe = _METHOD_SAFE_RE.sub(\"?\", s)",
        "    s = str(method or \"\")\n"
        "    safe = s",
        [SUITE],
    ),
    (
        "M-GP-8",
        "随机 uid 臂退化成固定 uid(A/B 不再有差异)",
        "story/gift_probe/profile.py",
        "        if self.random_user_unique_id:\n"
        "            return generate_random_user_unique_id(rng)\n"
        "        return CURRENT_USER_UNIQUE_ID",
        "        return CURRENT_USER_UNIQUE_ID",
        [SUITE],
    ),
    (
        "M-GP-9",
        "诊断 fetcher 接受业务回调(破坏业务隔离)",
        "story/gift_probe/runner.py",
        "    if on_chat is not None or on_control is not None \\\n"
        "            or on_interaction is not None or interaction_enabled:",
        "    if False:",
        [SUITE],
    ),
    (
        "M-GP-10",
        "probe 目录改名会撞 production 数据文件名",
        "story/gift_probe/capture.py",
        "        fname = f\"{safe_method}-{self._seq:04d}.bin\"",
        "        fname = f\"{safe_method}-{self._seq:04d}.jsonl\"",
        [SUITE],
    ),
]


def _read(p):
    return io.open(ROOT / p, encoding="utf-8").read()


def _write(p, s):
    io.open(ROOT / p, "w", encoding="utf-8").write(s)


def run_suite(rel):
    r = subprocess.run([PY, "-X", "utf8", str(ROOT / rel)],
                       cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=600)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def main():
    results = []
    for num, desc, path, old, new, suites in MUTATIONS:
        src = _read(path)
        if old == new:
            print(f"[{num}] **变异是空操作(故意?)**: {desc}")
            results.append((num, desc, "NOOP", []))
            continue
        if old not in src:
            print(f"[{num}] **补丁锚点找不到** —— 变异没打上: {desc}")
            results.append((num, desc, "ANCHOR-MISS", []))
            continue
        if src.count(old) != 1:
            print(f"[{num}] **锚点不唯一**({src.count(old)} 处): {desc}")
            results.append((num, desc, "ANCHOR-AMBIG", []))
            continue
        _write(path, src.replace(old, new, 1))
        try:
            reds = []
            for s in suites:
                rc, out = run_suite(s)
                if rc != 0 or "FAIL" in out or "Traceback" in out:
                    reds.append(os.path.basename(s))
            verdict = "RED" if reds else "**GREEN(变异没被抓住!)**"
            print(f"[{num}] {verdict}  {desc}  红: {reds}")
            results.append((num, desc, verdict, reds))
        finally:
            # ⚠️ 无条件还原。手工 undo 漏一次就会把变异带进提交。
            _write(path, src)
    print("\n" + "=" * 72)
    bad = [r for r in results if not r[2].startswith("RED")]
    for num, desc, verdict, _ in results:
        print(f"  {num:<10} {verdict:<28} {desc}")
    print("=" * 72)
    print(f"共 {len(results)} 条, {len(results) - len(bad)} 条成功变红")
    if bad:
        print("以下变异**没有**被测试抓住(测试或变异有问题):")
        for num, desc, verdict, _ in bad:
            print(f"  {num}: {desc} -> {verdict}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
