#!/usr/bin/env python
# coding: utf-8
"""配置: CLI + 环境变量 + 默认值。

优先级: CLI > env > 默认值。
所有可调项集中在这里, 引擎/客户端只读不可变配置。
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, field
from typing import Optional


# 网关已知可用的模型。未列出的名字网关会静默 200 并用自己的默认模型回答,
# 所以必须在启动时校验 —— 见 llm.py。
SUPPORTED_MODELS = frozenset({"deepseek-v4.1-flash", "glm-5.3-flash"})


def _env_str(key: str, default: str) -> str:
    v = os.environ.get(key)
    return v if v else default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ[key])
    except (KeyError, ValueError):
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ[key])
    except (KeyError, ValueError):
        return default


def _first_proxy_env() -> Optional[str]:
    """按优先级取代理地址。都没有则返回 None(直连)。"""
    for k in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy",
              "HTTP_PROXY", "http_proxy"):
        v = os.environ.get(k)
        if v:
            return v
    return None


def parse_proxy(url: Optional[str]):
    """把 'http://host:port' / 'socks5://host:port' 拆成
    (host, port, proxy_type) 给 websocket-client 用。

    websocket-client 的 run_forever 只收 host/port 分开的参数,
    所以要在这里拆。返回 None 表示不走代理。
    """
    if not url:
        return None
    s = url.strip()
    ptype = "http"
    if "://" in s:
        scheme, s = s.split("://", 1)
        scheme = scheme.lower()
        ptype = "socks5" if scheme.startswith("socks") else "http"
    # 去掉可能存在的认证信息 user:pass@host:port
    if "@" in s:
        s = s.rsplit("@", 1)[1]
    s = s.rstrip("/")
    if ":" not in s:
        return None
    host, _, port = s.rpartition(":")
    try:
        return host, int(port), ptype
    except ValueError:
        return None


@dataclass
class LLMConfig:
    """LLM 客户端配置。"""

    base_url: str = field(default_factory=lambda: _env_str("AI_BASE_URL", "http://127.0.0.1:8080"))
    api_key: str = field(default_factory=lambda: _env_str("AI_API_KEY", "11"))
    model: str = field(default_factory=lambda: _env_str("AI_MODEL", "deepseek-v4.1-flash"))
    timeout: float = field(default_factory=lambda: _env_float("AI_TIMEOUT", 60.0))
    max_tokens: int = field(default_factory=lambda: _env_int("AI_MAX_TOKENS", 700))
    max_retries: int = field(default_factory=lambda: _env_int("AI_MAX_RETRIES", 3))

    def masked(self) -> dict[str, str]:
        """用于启动日志 —— key 打码。"""
        key = self.api_key
        shown = (key[:2] + "***") if len(key) > 2 else "***"
        return {
            "base_url": self.base_url,
            "model": self.model,
            "api_key": shown,
            "timeout": str(self.timeout),
            "max_tokens": str(self.max_tokens),
            "max_retries": str(self.max_retries),
        }


@dataclass
class Config:
    """全局配置(海龟汤)。"""

    # ---- 输入源 ----
    live_id: Optional[str] = None
    sim_path: Optional[str] = None
    use_stdin: bool = False
    out_path: str = os.path.join("data", "danmaku.jsonl")
    puzzle_out_path: str = os.path.join("data", "puzzle.jsonl")
    keep_all: bool = False

    # ---- 代理(抓取弹幕用) ----
    # 抖音必须走代理时填这里。留空则自动读环境变量
    # HTTP_PROXY / HTTPS_PROXY / ALL_PROXY。
    # 形式: "http://127.0.0.1:7890" 或 "socks5://127.0.0.1:1080"
    # (socks5 需要额外装 pysocks: uv add pysocks)
    proxy: Optional[str] = field(default_factory=lambda: _first_proxy_env())
    no_proxy: Optional[str] = field(default_factory=lambda: os.environ.get("NO_PROXY")
                                    or os.environ.get("no_proxy") or None)

    # ---- 回答节奏 ----
    # 逐条秒回: 1 条提问 = 1 次 AI 调用。
    # qa_max_inflight 是在途调用硬上限(用户指定 5) —— 人多时提问排队而非雪崩。
    qa_max_inflight: int = 5
    qa_inflight_timeout: float = 25.0     # 在途回答超时 -> 直接判"未判定"(不重派)
    qa_retry_max: int = 2                 # 已废弃(保留字段): fail-fast 后不再重派
    qa_dedupe_seconds: float = 600.0      # 同一人同一问题去重窗口
    # ---- QA 的时延预算(与全局 AI_TIMEOUT 分开) ----
    # 为什么 QA 要单独一套: 全局 AI_TIMEOUT=60 / AI_MAX_RETRIES=3 是给
    # 出题/审稿/试玩那些**低频长任务**定的, 它们确实需要长预算。但直播
    # 问答是**面对面**的 —— 观众等 4 分钟等于这条提问已经废了。
    #
    # 历史基线: 正常裁决 1.3~1.7 秒(2026-09-18 13:27 同机实测)。
    # 8 秒 = 基线的 4~5 倍, 足够吸收抖动, 又不会让观众干等。
    #
    # 重试次数为 0 是**刻意的**: urllib 请求无法取消, 所以"超时后再发一次"
    # 不会消灭旧请求, 只会多叠一个 worker —— 那正是这次要修的放大器。
    # 传输层不重试, 重试的所有权归引擎(超时即 fail-fast 给"未判定")。
    qa_answer_timeout: float = 8.0
    qa_answer_retries: int = 0
    # ---- Q12: 重放识别 ----
    # 实测: 抖音在**重连后会把之前的弹幕重放一遍**(18:09 那批会在 18:11 原样
    # 再来一次)。识别分两条路:
    #   主路径: 平台 msg_id 精确去重(不依赖下面任何参数)。
    #   降级:   上游没给 ID 时, 只在"刚重连"的窗口内、且与最近历史大量
    #           精确重复时才抑制。
    # **旧的"1.5s 内 3 条就整批丢弃 + 压制全场 5 秒"已删除** —— 它假设
    # "真人不可能 1.5 秒连发 3 条", 40 人房间看到关键提示时这假设不成立。
    replay_guard_seconds: float = 20.0    # 重连后开多久的重复检测窗口(方案 §9.2 建议 10~20s)
    replay_guard_min_repeats: int = 3     # guard 内同一指纹出现几次判定为重放
    msg_id_cache_size: int = 2000         # msg_id 去重表上限(防内存无界增长)
    pending_cap: int = 40                 # 待答队列硬上限, 溢出丢最旧
    max_question_len: int = 60            # 单条提问长度上限

    # ---- 收尾与提示(单一时间轴) ----
    #   0min 出题 -> 每 hint_seconds 给一条提示 -> 给满 max_hints 条后
    #   **再等 hint_seconds** 才揭晓。默认就是:
    #       5min 提示1 / 10min 提示2 / 15min 提示3 / 20min 揭晓
    #   另外: 有人猜中 -> 立即揭晓。(提问条数不设上限)
    hint_seconds: float = 300.0           # 每条提示之间的间隔(也是最后等待揭晓的时间)
    max_hints: int = 3                    # 给几条提示(之后再过 hint_seconds 揭晓)
    restate_seconds: float = 120.0        # 长时间无人说话 -> 零成本重述谜面
    # 非 QA 阶段观众发 #问题 时, 多久最多回一条"现在不能问"的提示。
    # 全局节流(不是按观众): 多人同发时不刷屏。取小值 —— 窗口内其他人
    # 仍然静默, 太长就等于又变回"我发的没反应"。
    phase_ack_seconds: float = 5.0
    giveup_seconds: float = 1800.0        # 硬性兜底, 一般轮不到它

    # ---- 谜题循环 ----
    reveal_hold_seconds: float = 30.0     # 揭晓展示时长(用户指定 30s)
    # 出题在途超时。**必须大于 gen_riddle 的最坏耗时**: 它内部最多 3 次
    # 生成 + 每次一次质检, 实测单次 4-27 秒, 最坏能跑到一分多钟。
    # 实测踩过: 原来设 45 秒, 引擎在外面判超时, 而 gen_riddle 内部还在跑,
    # 两层重试打架 -> 连续失败 -> 退化成兜底题。
    setting_timeout_seconds: float = 150.0
    riddle_max_attempts: int = 4          # 出题重试上限(累积反馈, 见 gen_riddle)
    max_reveals_per_puzzle: int = 1       # 一题最多揭晓一次
    max_puzzles: int = 0                  # 0 = 无限

    # ---- 节奏 / 接入 ----
    tick_hz: float = 4.0                  # 调度线程频率
    stall_seconds: float = 120.0          # LiveSource 判定"停摆"重启的阈值
    reconnect_alert_after: int = 3        # 连续重建失败几次后打醒目告警
    disconnect_grace: float = 60.0
    sim_loop_gap: float = 20.0            # SimSource loop 每遍之间的间隔

    # ---- 题池(方案 §40) ----
    # 已通过质量链的题存下来、优先投入直播; 池子空了再现场生成。
    pool_enabled: bool = True
    # 低水位 / 高水位(hysteresis)。Q9 的补池 latch:
    #   库存 < pool_min_size   -> 启动一次"补池周期"
    #   补池周期一直补到 库存 >= pool_target_size 才结束
    # 必须是**真正的滞回**, 不是每个 tick 判一次 `stock < min` ——
    # 否则 1 补成 2 就停了, pool_target_size 永远没有意义。
    pool_target_size: int = 5
    pool_min_size: int = 2
    # 补池总开关。**与 pool_enabled 解耦**: 关掉它 = 不后台生成, 但
    # 手工/脚本灌进池子的存量题**照常用**。网关故障时就是靠这一条
    # 停掉后台生成、同时继续播已有的题(pool_enabled=False 做不到 ——
    # 它把池子整个关掉)。
    pool_prefetch_enabled: bool = True
    # 补池失败(gen_spec 失败 / pool.add 失败 / 意外异常)后的退避秒数。
    # 为什么必须有: tick 是 4Hz。没有退避时, 网关或磁盘持续故障的
    # 每一次 tick 都会重新提交一个生成任务 —— 那是每秒 4 次的失败
    # 风暴, 比不补池糟得多(它还会和直播出题抢同一个网关配额)。
    pool_prefetch_backoff_s: float = 30.0
    # 池子本体(已过审、待播)与 used 日志(追加式, 记"哪些已经交付过")。
    # 注意**不要**用 data/puzzle_used.jsonl: `data/puzzle.jsonl` 已经是
    # 直播 archive 了, 两个"used"含义不同, 名字太近迟早看错。
    pool_path: str = os.path.join("data", "pool.jsonl")
    pool_used_path: str = os.path.join("data", "pool_used.jsonl")
    # 注: 这里**曾**有一个 `pool_op_budget_ms`, 号称给 pop_next 的 I/O 一个
    # 上界。它没有任何代码读它 —— 是个 dead config, 注释却会让人以为
    # "500ms 后一定回落", 那是不存在的保证(单次 open/fsync 真卡住时,
    # 没有任何东西能打断它)。删掉, 免得留下假承诺。
    #
    # 真要给阻塞 I/O 做硬超时, 得放到后台线程里做, 那是 Q9 的设计范围。

    # ---- Blueprint 调度(方案 §7) ----
    # **与 pool_enabled 解耦** —— 关掉题池不该顺便关掉"控制题型分布"。
    # 早先 _pick_blueprint 读的是 pool_enabled, 那是错误的职责耦合:
    # 一旦将来为了别的理由关掉 pool, 题目多样性会跟着一起失效。
    quality_scheduler_enabled: bool = True
    quality_seed: Optional[int] = None    # 固定它可让出题可复现(默认随机)

    # ---- 题目分布配额(方案 §10) ----
    # **全部代码判断**, 不写进 reviewer prompt —— 它没有全局状态。
    quality_recent_window: int = 10       # 看最近多少题
    quota_same_mechanism: int = 2         # 同一诡计类型最多几道
    quota_same_solution_shape: int = 2    # 同一解法形状最多几道
    quota_death: int = 2                  # 死人题上限
    quota_past_trauma: int = 2            # 依赖既往创伤的上限
    quota_trauma_ritual: int = 1          # "创伤 + 长年怪规矩"上限(实测坍缩最重)
    # ---- Step 02: reveal / tone / 规则依赖的 rolling quota ----
    # 数的是 **observed**(Reviewer 读完如实回传的 Signature), 不是调度器
    # 的目标值 —— 目标只是输入, 配额反映的是观众真实看到的分布。
    quota_same_reveal_mode: int = 2       # 同一揭晓结构最多几道
    quota_straight_explanation: int = 2   # "没有翻转的正面解释"上限
    quota_neutral_emotion: int = 3        # "中性气氛"上限(去掉固定偏置后的护栏)
    quota_procedural_rule: int = 2        # 主要靠制度性设定成立的题上限

    # ---- AI 试玩(方案 §40/§42): 直播热路径里**默认关闭** ----
    # 只在后台 prefetch candidate 上跑, `_riddle` 永远不试玩。
    playtest_enabled: bool = False
    playtest_max_turns: int = 10          # 试玩轮数上限(Player<->Host 往返)

    # ---- 采样温度(方案 §30) ----
    # 裁决/裁判必须确定性(temperature=0), 出题才需要发散。
    # 网关若不支持, 会记日志而不是默默假设生效。
    answer_temperature: float = 0.0
    judge_temperature: float = 0.0
    # 提示的温度略高一点点: 同一方向可以说出不同角度的引导语,
    # 完全不抖动会让第 2、3 条提示听起来像同一句(方案 §31)。
    hint_temperature: float = 0.5
    # 提示生成失败后, 隔多久重试**同一格**(第三轮 review P1)。
    # 不能设太小 —— 网关持续故障时会变成每秒一次的失败风暴。
    # 也不能设太大 —— 那一格就赶不上时间轴了(走完就揭晓)。
    hint_retry_seconds: float = 15.0
    review_temperature: float = 0.2
    generate_temperature: float = 0.8

    # ---- 上下文 ----
    qa_max_records: int = 60              # 喂回 LLM 的问答记录条数上限
    qa_max_chars: int = 2200              # 喂回 LLM 的问答记录字符上限
    max_prompt_chars: int = 6000          # prompt 字符硬上限
    fallback_puzzle: str = (
        "一个男人走进餐厅，点了一碗海龟汤，喝了一口就冲出去自杀了。为什么？"
    )
    fallback_answer: str = (
        "多年前他遭遇海难，同伴给他喝的“海龟汤”其实是同伴自己的肉。"
        "今天他喝到真正的海龟汤，发现味道完全不同，才明白当年喝的是什么，"
        "巨大的愧疚与创伤让他崩溃自杀。"
    )

    # ---- Web ----
    host: str = "127.0.0.1"
    port: int = 8765
    open_window: bool = True     # 启动时自动开独立直播输出窗口

    # ---- LLM ----
    no_llm: bool = False
    llm: LLMConfig = field(default_factory=LLMConfig)

    # ---- 日志 ----
    log_level: str = "INFO"
    log_file: Optional[str] = None      # 明细日志落盘路径(None = 只打控制台)

    # ------------------------------------------------------------------
    @property
    def source_label(self) -> str:
        if self.live_id:
            return f"live:{self.live_id}"
        if self.sim_path:
            return f"sim:{os.path.basename(self.sim_path)}"
        if self.use_stdin:
            return "stdin"
        return "none"

    def validate(self) -> list[str]:
        """返回警告列表(不致命)。致命错误交给调用方显式抛。"""
        warns: list[str] = []
        n_sources = sum(bool(x) for x in (self.live_id, self.sim_path, self.use_stdin))
        if n_sources == 0:
            raise ValueError("必须指定输入源: --live <id> / --sim <path> / --stdin")
        if n_sources > 1:
            raise ValueError("--live / --sim / --stdin 三者互斥, 只能选一个")
        if not self.no_llm and self.llm.model not in SUPPORTED_MODELS:
            # 实测: 网关对未知模型静默返回 200, 所以这里必须显式警告
            warns.append(
                f"模型 '{self.llm.model}' 不在已知列表 {sorted(SUPPORTED_MODELS)} 中。"
                f"网关对未知模型会静默用默认模型回答(HTTP 200), 配置可能是错的。"
            )
        # 高水位低于低水位 -> 滞回是反的: 补池周期会在启动的那一拍
        # 立刻被判"已到高水位"而清掉, min/target 双双失效, 表现是
        # 池子永远补不起来。这不是"某种可用的配置", 是必然的误配置。
        if self.pool_target_size < self.pool_min_size:
            warns.append(
                f"pool_target_size({self.pool_target_size}) < "
                f"pool_min_size({self.pool_min_size}): 补池滞回反了, "
                f"池子永远补不起来。要让 target >= min。"
            )
        if self.pool_min_size < 0:
            warns.append(f"pool_min_size({self.pool_min_size}) 为负, 已按 0 处理。")
        if self.pool_prefetch_backoff_s <= 0:
            warns.append(
                f"pool_prefetch_backoff_s({self.pool_prefetch_backoff_s}) <= 0: "
                f"补池失败后不会退避, 4Hz 的 tick 会打成失败风暴。"
            )
        if self.phase_ack_seconds <= 0:
            warns.append(
                f"phase_ack_seconds({self.phase_ack_seconds}) <= 0: "
                f"非 QA 阶段的 #问题 提示不会节流, 多人同发时会刷屏。"
            )
        if self.replay_guard_seconds <= 0:
            warns.append(
                f"replay_guard_seconds({self.replay_guard_seconds}) <= 0: "
                f"重连后的重放不会被识别(只在没有 msg_id 时才走这条路)。"
            )
        if self.replay_guard_min_repeats <= 0:
            warns.append(
                f"replay_guard_min_repeats({self.replay_guard_min_repeats}) <= 0: "
                f"重连后会把**所有**没带 ID 的弹幕都当成重放丢掉, 真人也被误杀。"
            )
        if self.msg_id_cache_size <= 0:
            warns.append(
                f"msg_id_cache_size({self.msg_id_cache_size}) <= 0: "
                f"已按默认 2000 处理(引擎会钳回正数), 但显式设成 0 说明"
                f"本意可能是想关掉去重 —— 那要改代码, 不是改这个值。"
            )
        if self.qa_answer_timeout <= 0:
            warns.append(
                f"qa_answer_timeout({self.qa_answer_timeout}) <= 0: "
                f"QA 请求会立刻超时, 观众每条提问都会收到'未判定'。"
            )
        if self.qa_answer_retries < 0:
            warns.append(
                f"qa_answer_retries({self.qa_answer_retries}) < 0: "
                f"已按 0 处理(不重试)。"
            )
        return warns


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="director.py",
        description="竖屏 AI 海龟汤直播 —— AI 出谜题, 观众发 #问题 追问, 猜中揭晓",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  uv run director.py --sim data/demo_script.jsonl --no-llm\n"
            "  uv run director.py --sim data/demo_script.jsonl --reveal-hold 8\n"
            "  uv run director.py --live 813110862078\n"
        ),
    )
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--live", dest="live_id", metavar="LIVE_ID",
                     help="接真实直播间(直播间号)")
    src.add_argument("--sim", dest="sim_path", metavar="PATH",
                     help="离线回放脚本 JSONL")
    src.add_argument("--stdin", dest="use_stdin", action="store_true",
                     help="从标准输入读弹幕(手打 '#问题')")

    ap.add_argument("--out", default=None,
                    help="原始弹幕 JSONL 输出路径 (默认 data/danmaku.jsonl)")
    ap.add_argument("--puzzle-out", default=os.path.join("data", "puzzle.jsonl"),
                    help="谜题/问答 JSONL 输出路径 (默认 data/puzzle.jsonl)")
    ap.add_argument("--keep-all", action="store_true",
                    help="同时记录礼物/进场/点赞等(透传给抓取层)")
    ap.add_argument("--proxy", default=None,
                    help="抓弹幕走代理, 如 http://127.0.0.1:7897 "
                         "(默认读 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY)")
    ap.add_argument("--no-proxy", default=None,
                    help="不走代理的域名, 逗号分隔(默认读 NO_PROXY)")

    ap.add_argument("--qa-max-inflight", type=int, default=5,
                    help="同时在途的 AI 回答调用上限, 默认 5")
    ap.add_argument("--hint-seconds", type=float, default=300.0,
                    help="提示间隔(秒), 也是最后一条提示到揭晓的间隔, 默认 300(5分钟)")
    ap.add_argument("--max-hints", type=int, default=3,
                    help="给几条提示(之后再过 hint-seconds 揭晓), 默认 3")
    ap.add_argument("--restate-seconds", type=float, default=120.0,
                    help="多久无人发言就重述谜面(零成本), 默认 120")
    ap.add_argument("--giveup-seconds", type=float, default=1800.0,
                    help="硬性兜底(一般轮不到), 默认 1800")
    ap.add_argument("--reveal-hold", type=float, default=30.0,
                    help="揭晓展示时长(秒), 默认 30")
    ap.add_argument("--tick-hz", type=float, default=4.0,
                    help="调度线程频率, 默认 4")
    ap.add_argument("--stall-seconds", type=float, default=120.0,
                    help="真房间无弹幕多久判定为停摆并重连, 默认 120")
    ap.add_argument("--max-puzzles", type=int, default=0,
                    help="跑多少题后停止(0=无限), 默认 0")
    ap.add_argument("--no-prefetch", dest="pool_prefetch_enabled",
                    action="store_false",
                    help="不后台补池(只用已有/手工灌的题; 默认开启)。"
                         "网关故障时用它停掉后台生成, 池子里的存量题照常播")
    ap.add_argument("--playtest", dest="playtest_enabled",
                    action="store_true",
                    help="后台补池时先用 AI 玩家试玩一遍, 只有猜得中才入池"
                         "(默认关闭)。会让补池变慢很多: 每道题多约 2N~3N 次"
                         "调用(N=轮数)。每轮是 Player + answer, 而 answer "
                         "一旦把该句判为 solution_candidate 还会再调一次 "
                         "Final Judge, 所以单轮最坏 3 次")
    ap.add_argument("--playtest-max-turns", type=int, default=10,
                    help="AI 试玩最多几轮, 默认 10")

    ap.add_argument("--max-question-len", type=int, default=60,
                    help="单条提问最大长度, 默认 60")

    ap.add_argument("--host", default="127.0.0.1", help="Web 服务绑定地址")
    ap.add_argument("--port", type=int, default=8765, help="Web 服务端口")
    ap.add_argument("--no-window", dest="open_window", action="store_false",
                    help="不自动开直播输出窗口(只起服务, 自己开浏览器)")
    ap.set_defaults(open_window=True)

    ap.add_argument("--no-llm", action="store_true",
                    help="不调 LLM, 用固定文案(用于快速测状态机)")
    ap.add_argument("--model", default=None, help="覆盖 AI_MODEL")
    ap.add_argument("--log-level", default="INFO",
                    choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                    help="控制台日志级别, 默认 INFO")
    ap.add_argument("--log-file", default=None, metavar="PATH",
                    help="把明细日志写到文件(比控制台详细; 默认 data/run.log)")
    return ap


def from_args(argv: Optional[list[str]] = None) -> Config:
    """解析 CLI + env -> Config。"""
    ap = build_parser()
    a = ap.parse_args(argv)

    llm = LLMConfig()
    if a.model:
        llm.model = a.model

    cfg = Config(
        live_id=a.live_id,
        sim_path=a.sim_path,
        use_stdin=a.use_stdin,
        out_path=a.out or os.path.join("data", "danmaku.jsonl"),
        puzzle_out_path=a.puzzle_out,
        keep_all=a.keep_all,
        proxy=a.proxy or _first_proxy_env(),
        no_proxy=a.no_proxy or os.environ.get("NO_PROXY"),
        qa_max_inflight=a.qa_max_inflight,
        hint_seconds=a.hint_seconds,
        restate_seconds=a.restate_seconds,
        giveup_seconds=a.giveup_seconds,
        reveal_hold_seconds=a.reveal_hold,
        max_hints=a.max_hints,
        tick_hz=a.tick_hz,
        stall_seconds=a.stall_seconds,
        max_puzzles=a.max_puzzles,
        # 只加 flag 不在这里接上 = 又一个 dead config(参数形同虚设,
        # 而 --help 里明明写着)。加 flag 和接线必须同一处完成。
        pool_prefetch_enabled=a.pool_prefetch_enabled,
        playtest_enabled=a.playtest_enabled,
        playtest_max_turns=a.playtest_max_turns,
        max_question_len=a.max_question_len,
        host=a.host,
        port=a.port,
        open_window=a.open_window,
        no_llm=a.no_llm,
        llm=llm,
        log_level=a.log_level,
        log_file=a.log_file,
    )
    return cfg
