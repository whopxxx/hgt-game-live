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
    #: Step 11: Like/Gift 是否进入**业务链**(Director -> Engine)。
    #:
    #: 与 `keep_all` **正交** —— 这是本 Step 的核心:
    #:     keep_all       = 诊断用: 把所有消息类型都落 JSONL
    #:     interaction_enabled = 业务用: 让 Like/Gift 走业务事件通路
    #:
    #: 早先 Like/Gift 的解析被 `keep_all` 顺手挡住(`if not self.keep_all:
    #: return`), 于是"想收礼物"就必须开全量落库。那是**职责耦合**: 一个
    #: 是存储策略, 一个是业务能力。现在两者独立:
    #:     keep_all=False + interaction_enabled=True  -> Like/Gift 进业务链
    #:     keep_all=True                              -> 诊断类型照旧全落
    #:     keep_all=False + interaction_enabled=False -> Like/Gift 不进业务链
    interaction_enabled: bool = True

    # ---- 12B-Auth: WS 登录态(单变量 A/B) ----
    #: 抖音登录态 Cookie 串, 用于 WS handshake。**只从环境变量读**。
    #:
    #: 为什么 env-only, 不给 CLI: 命令行参数会进 shell history、进程
    #: command line(`ps`/任务管理器可见)、截图、以及别人发的复现命令里。
    #: Cookie 是**凭据**, 泄漏一次等于账号被拿走。要走 CLI 的话只能是
    #: `--cookie-file PATH`, 那是另一个 commit 的事。
    #:
    #: `repr=False`: dataclass 的默认 `__repr__` 会打出所有字段, 而我们的
    #: 日志/异常里到处是 f"{cfg}" —— 那等于把凭据写进每一行日志。
    #: 这是**硬要求**, 不是风格偏好, 所以这里显式关掉, 并有测试钉住。
    #:
    #: 空 -> 游客态(与历史行为完全一致)。
    douyin_live_cookie: Optional[str] = field(
        default_factory=lambda: os.environ.get("DOUYIN_LIVE_COOKIE") or None,
        repr=False,
    )

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

    # ---- AI 玩家 ----
    ai_player_enabled: bool = True
    ai_player_min_gap_seconds: float = 45.0
    ai_player_retry_seconds: float = 15.0

    # ---- 收尾与提示(单一时间轴) ----
    #   0min 出题 -> 每 hint_seconds 给一条提示 -> 给满 max_hints 条后
    #   **再等 hint_seconds** 才揭晓。默认就是:
    #       5min 提示1 / 10min 提示2 / 15min 提示3 / 20min 揭晓
    #   另外: 有人猜中 -> 立即揭晓。(提问条数不设上限)
    hint_seconds: float = 300.0           # 每条提示之间的间隔(也是最后等待揭晓的时间)
    max_hints: int = 3                    # 给几条提示(之后再过 hint_seconds 揭晓)
    # ---- H1: 提示的**第二个触发源**(真人成功问答数) ----
    #
    # 时间轴只管"这道题开了多久"; 但房间可能在一分钟内就问出 30 条有信息
    # 量的问答 —— 那时观众早就推到了该给提示的位置, 而时间轴还没到点。
    # 所以提示改成 **时间 OR 问答数** 二者取先到:
    #
    #     每 hint_questions_per_level 条**成功**的真人裁决 -> 进一格提示
    #
    # 默认 20 -> 20 问给 Hint1, 40 问给 Hint2, 60 问给 Hint3。
    # `0` = 关掉这条触发源, 退回纯时间轴。
    #
    # ⚠️ 问答数**绝不能**触发自动揭晓 —— 揭晓仍然只由时间轴与"有人猜中"
    # 决定。否则房间刷得快一点就会把题刷掉, 那是灾难。
    hint_questions_per_level: int = 20
    # 两条提示之间的**最小间隔**(秒)。防止"房间突然从 20 问冲到 45 问"
    # 时 Hint1 与 Hint2 连发 —— 那是信息轰炸, 观众根本来不及想。
    # 手动 `#提示` 成功也走同一条冷却(它们最终都落在 `submit_hint`)。
    hint_min_gap_seconds: float = 45.0
    restate_seconds: float = 120.0        # 长时间无人说话 -> 零成本重述谜面
    # 非 QA 阶段观众发 #问题 时, 多久最多回一条"现在不能问"的提示。
    # 全局节流(不是按观众): 多人同发时不刷屏。取小值 —— 窗口内其他人
    # 仍然静默, 太长就等于又变回"我发的没反应"。
    phase_ack_seconds: float = 5.0
    giveup_seconds: float = 1800.0        # 硬性兜底, 一般轮不到它

    # ---- 谜题循环 ----
    reveal_hold_seconds: float = 60.0     # 揭晓展示时长(用户指定 60s)
    # 揭晓 60 秒分**三段**(U2):
    #   0 .. reveal_core_focus_seconds        只显示核心答案(超大字号)
    #   core_focus .. reveal_detail_seconds   追加完整解释(共同解谜隐藏)
    #   reveal_detail_seconds .. hold         完整解释隐藏, 让位给共同解谜
    #
    # 为什么必须分三段而不是两段: 实播里核心答案 + 完整解释 + 共同解谜
    # **同时**上屏, 于是下半屏三块互相争空间, fitReveal 只能一路缩字号,
    # 完整解释被压成一条矮滚动框 —— 而观众没有鼠标去滚直播源。
    # 60 秒本来就是时间资源: 用时间换空间, 而不是把字缩小。
    reveal_core_focus_seconds: float = 15.0
    # 完整解释展示到什么时候为止(之后让位给贡献链)。
    reveal_detail_seconds: float = 45.0
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
    # stable-refill: 不再等到只剩 1~2 道才救火。低于 8 就开始补，
    # 一次补到 12；即便单候选通过率只有四成，也还有足够缓冲吸收连续失败。
    pool_target_size: int = 12
    pool_min_size: int = 8
    # 下一题**此刻能不能播**的最低要求(与长期库存分开, 见 pool.py)。
    # 实播踩到的坑: 池里 6 道候选全被当前窗口挡住 -> 回落现场生成,
    # 观众干等 10–40 秒; 而 stock=6 让补池认为健康, 一道都不补。
    # 所以补池要同时看 playable —— `stock >= target` 但 `playable <
    # playable_min` 时**仍然**补。
    pool_playable_min: int = 3
    # ---- 揭晓窗口专用目标(60 秒是**最富裕**的补池窗口) ----
    # QA 期间仍然单飞让路，但库存水位已前移(target=12/playable=3)。
    # REVEALED 是引擎**完全空闲**的 60 秒 —— 观众在看答案, 没有任何
    # 在途请求。这时把目标抬高, 让"看答案 -> 下一题直接出现"真正成立
    # (而不是 60 秒后又回到现场生成、观众干等)。
    # 仍然单飞串行 + 受 pool_max_size 约束, 不并行生成多个。
    pool_reveal_target_size: int = 12
    pool_reveal_playable_target: int = 3
    # 距下一题不足这个秒数就不再**启动**新请求(在途的不用强杀)。
    #
    # ⚠️ G1: 它必须**至少覆盖一轮补池的完整预算**(prefetch budget +
    # 安全余量), 否则等于没挡 —— 实播事故:
    #
    #     18:45:07 揭晓结束、下一题开始(SETTING)
    #     18:44:49 启动的那次 prefetch 还在跑
    #     18:45:58 它才跑完第 4 稿失败
    #
    # 也就是 live 现场生成与后台补池**同时**占了 51 秒的网关。当时
    # guard 只有 15s, 而一轮 prefetch 可能跑几十秒 —— "只剩 18 秒"
    # 照样会启动一个最多几十秒的后台任务, 那个任务注定跨过 deadline,
    # 与下一题的现场生成正面相撞。
    #
    # 所以默认值抬到 30s(= `pool_prefetch_budget_seconds` 25 + 余量),
    # 并且 `PoolPrefetcher` 会取 `max(本值, budget + 余量)` 兜底 ——
    # 配置调大 budget 却忘了调 guard 时, 不会静默退回旧行为。
    pool_reveal_start_guard_seconds: float = 30.0
    # 一轮 prefetch 的**安全余量**: guard 至少要比 budget 多这么多,
    # 让"启动的那次生成"有希望在 deadline 之前真的结束。
    pool_prefetch_guard_margin_seconds: float = 5.0
    # 补池的硬上限: 库存到这儿就停, **即使 playable 仍然是 0**。
    # 为什么必须有: 若那批题是被"某个窗口条件"整体挡住的(比如最近
    # 十题全挤在同一 mechanism), 补进来的新题也会被同一条件挡住 ——
    # 没有上限就是无限生成 + 无限烧网关配额, 而 playable 永远不动。
    # 到顶只 warning, 让运维看见"补了但没用", 不是静默空转。
    pool_max_size: int = 16
    # 补池总开关。**与 pool_enabled 解耦**: 关掉它 = 不后台生成, 但
    # 手工/脚本灌进池子的存量题**照常用**。网关故障时就是靠这一条
    # 停掉后台生成、同时继续播已有的题(pool_enabled=False 做不到 ——
    # 它把池子整个关掉)。
    pool_prefetch_enabled: bool = True
    # 补池失败(gen_spec 失败 / pool.add 失败 / 意外异常)后的**首次**
    # 退避秒数。后续连续失败按 `pool_prefetch_backoff_schedule_s` 递增。
    # 为什么必须有: tick 是 4Hz。没有退避时, 网关或磁盘持续故障的
    # 每一次 tick 都会重新提交一个生成任务 —— 那是每秒 4 次的失败
    # 风暴, 比不补池糟得多(它还会和直播出题抢同一个网关配额)。
    pool_prefetch_backoff_s: float = 30.0
    # ---- G1: 连续失败的退避序列(秒) ----
    #
    # 实播指纹: 固定 30 秒退避 + 每轮固定 4 稿, 于是日志长成
    #
    #     18:46:47 开 -> 18:47:37 败
    #     18:48:07 开 -> 18:49:09 败
    #     18:49:39 开 -> 18:50:24 败
    #
    # —— 谷底是**同一个上下文**(同样的 recent window / 同样的配额
    # 饱和状态)在反复重试, 每次都烧完整一轮多稿预算。固定间隔既
    # 不够长(问题不是瞬时的), 也不够短(真想恢复时又白等 30 秒)。
    #
    # 改成递增: 30 -> 60 -> 120 -> 240 -> 300, 之后封顶 300。
    # 成功入池**立刻重置**回第一档; `interrupted`(让路)**不算失败**,
    # 不动这个序列。
    pool_prefetch_backoff_schedule_s: tuple = (30.0, 60.0, 120.0, 240.0,
                                               300.0)
    # ---- G1: 后台补池的**独立**预算 ----
    #
    # 关键: 后台补池过去直接调 `gen_spec()` 的默认参数 (max_attempts=4,
    # budget_s=90) —— 那是**直播现场出题**的预算, 因为 live 出一道题
    # 观众就在干等, 值得多试几稿。而后台补池是"有空就补一道", 多试
    # 一稿的全部收益只是池子里多一道题, 成本却是与直播抢网关 + 跨过
    # deadline 继续跑。
    #
    # 所以后台**少尝试**, 不是降低题质: 同样的硬门一道不少, 只是
    # 两稿都不合格就放弃, 等下一轮, 而不是一路打到第 4 稿。
    pool_prefetch_max_attempts: int = 2
    pool_prefetch_budget_seconds: float = 25.0
    # ---- G2: keyword2 两阶段起题(prefetch 与 live 现场生成共用) ----
    #
    # 打开时, 候选的产生方式换成 keyword2:
    #
    #     程序随机抽 2 个普通生活关键词
    #       -> Stage A: 先想清唯一真相/现场线索/发生顺序, 再写
    #                   title / puzzle / answer(Case-first 顺序)
    #       -> Stage B: 冻结谜面谜底, 只结构化成 PuzzleSpec
    #       -> 现有 Reviewer / truth audit / validate_spec / 跨题门
    #
    # 依据 G1-A / G1-B: 这样出来的题**谜面更短、单机关、没有硬塞的第二
    # 机关**, 更接近外部题库的语感; 而 Blueprint 链最容易丢的就是这个。
    #
    # ⚠️ **prefetch 与 live 现场生成走的是同一条链**(G4-2 §四):
    #     * 两边都经 `story/keyword_seed.py::keyword_spec()`, 所以
    #       "prefetch 是 keyword2 而 live 是 classic"在结构上不可能发生
    #       —— 它们读的是**同一个** config flag;
    #     * kill-switch `--no-keyword-seed` 让两边**一起**回 classic
    #       (`pick_blueprint -> gen_spec`);
    #     * curated 链不受影响 —— 那是"搬运"链, 不是"发明"链。
    #
    # ⚠️ 风格版本号是 `KEYWORD_IDEA_PROMPT_VERSION`(v4 起是 Case-first),
    # 与 `QUALITY_POLICY_VERSION` **分开** —— 接受标准一个字都没改。
    # 旧版本的盘上库存**不会**因为这个 flag 自动消失, 上线用一次性
    # pool rotation 处理(备份 pool.jsonl -> 生成池从空开始 -> 由现有
    # prewarm / prefetch 重新产生), **不要**动 played / used / archive。
    #
    # ⚠️ 质量策略**一条都不放宽**: keyword 题仍然走同一套 Reviewer /
    # truth audit / validate_spec / cross_puzzle_gate HARD / too_similar /
    # recent-10 quota。它仍是 **AI 原创**题(不是 curated)。
    pool_keyword_seed_enabled: bool = True

    # ---- G4: keyword2 的 seed 来源(独立词库) ----
    #
    # 关键词不再来自人工 `KEYWORD_BANK`, 而是 `neurostellar/haiguitang` 的
    # 原始 `input` 字段**展开成的独立词库** —— 由
    # `tools/build_keyword_seed_corpus.py` **离线**构建成这个文件
    # (见 `story/keyword_corpus.py`)。
    #
    # ⚠️ 运行时抽的是**两个独立的词**(随机重新组合), 不是原始 pair。
    # ⚠️ 文件缺失 / 空 / 解析失败时**显式降级**: 打一条 ERROR 日志, 整条
    # keyword2 链让位给 classic Blueprint 链。**不会**回退人工词库 ——
    # "看起来在跑 keyword2, 其实偷偷用人工词"是要防的形状。
    keyword_corpus_path: str = ""      # 空 = data/keyword2_vocabulary.json

    # keyword bag 的 session seed。
    #
    #   None + quality_seed 有值 -> 由 quality_seed 确定性派生(推荐)
    #   None + quality_seed 也空 -> 启动时随机一次, 并**写进 INFO 日志**
    #   给了值                    -> 直接用(复盘时从日志抄回来重放)
    #
    # 它只影响 keyword2 抽词, 与 live 出题的 rng / classic 链的
    # blueprint rng **完全隔离**。
    keyword_session_seed: Optional[int] = None

    # ---- Step 12C: gift_capture_diagnostic 模式 ----
    #
    # ⚠️ **诊断模式, 不是业务模式**。打开它只做一件事: 额外开 1~3 路受控的
    # WS 连接去采集"哪种连接画像能收到 Gift"的原始证据, 每路写进
    # `data/gift_probe/<session>/<profile>/`。
    #
    # ⚠️ 它还是**独占会话**: 打开后 Director **不会启动** —— 本进程只跑
    # 采集, 跑完退出。这不是"游戏照常直播 + 后台旁路探针"那种模式(那需要
    # 单独的生命周期接线, 不在本轮范围内)。见 `director._run_gift_capture_diagnostic`。
    #
    # 它**不**接前端公告、不感谢礼物、不做打赏榜、不改 SummonLedger ——
    # 诊断连接不接任何业务回调(`make_diagnostic_fetcher` 会对传回调的
    # 调用直接报错)。详见 `story/gift_probe/`。
    gift_capture_diagnostic: bool = False
    #: 要开的画像名(逗号分隔)。空 = 用默认三臂(A/B/C)。
    #: 拼错的名字**会报错**而不是被忽略 —— 见 `select_profiles`。
    gift_probe_profiles: str = ""
    #: 最多几路(硬上限 3)。不为了省事, 是为了不无意义地制造大量连接。
    gift_probe_limit: int = 3
    #: 证据目录根。默认 `data/gift_probe`(gitignored)。
    #: **不能**传 `data` 本身 —— 那会让 probe 文件与 production 数据同层。
    gift_probe_dir: str = os.path.join("data", "gift_probe")
    #: 每个 (profile, method) 最多留几个样本。
    gift_probe_max_per_method: int = 20
    #: 摘要间隔(秒)。Issue 建议 20~30。
    gift_probe_summary_seconds: float = 25.0

    # ---- G4-2 §五: 冷启动 prewarm ----
    #
    # keyword2 是**两阶段**的, 一次现场生成几十秒。若第一题让观众等,
    # 开播体验直接垮。所以进第一题正式 SETTING 之前做一次**有限**预热。
    #
    # ⚠️ 目标是"**至少 1 道**", 不是补到 `pool_target_size`。补满要 3~5
    # 轮, 每轮几十秒 —— 那是"开播前先静默三分钟", 对直播不可接受。1 道
    # 够把第一题顶过去, 剩下的交给后台补池在 REVEALED 窗口续。
    #
    # 双上限(轮数 / 秒数), 先到者停。失败**不阻止启动** —— 按 emergency
    # fallback 处理。预热是优化, 不是可用性前提。
    #
    # 设 0 关掉预热(调试 / 离线用)。
    pool_prewarm_max_rounds: int = 2
    pool_prewarm_max_seconds: float = 90.0
    # 池子本体(已过审、待播)与 used 日志(追加式, 记"哪些已经交付过")。
    # 注意**不要**用 data/puzzle_used.jsonl: `data/puzzle.jsonl` 已经是
    # 直播 archive 了, 两个"used"含义不同, 名字太近迟早看错。
    pool_path: str = os.path.join("data", "pool.jsonl")
    pool_used_path: str = os.path.join("data", "pool_used.jsonl")
    # ---- P0: 全局已播账本(任意来源, 跨重启) ----
    #
    # ⚠️ 与上面那个 `pool_used_path` **不是**一回事, 别合并:
    #
    #     pool_used.jsonl   题池交付过哪些题(只覆盖池题)
    #     played.jsonl      **任何来源真的上屏过**哪些题
    #                       (pool / live_generate / fallback / curated)
    #
    # 前者是题池的内部账本(它靠这个决定"池里还剩哪些没用过"); 后者是
    # 播出的**事实**记录, 服务的是"已经播过的题不得再次进入 QA"。
    # 现场生成与兜底题根本不在池子里, 所以只有后者能拦住它们。
    played_path: str = os.path.join("data", "played.jsonl")

    # ---- Batch H2-F/G: curated(外部题库)池 ----
    #
    # curated 题放**单独一个文件**, 不与 `pool.jsonl` 混。任务书 H2-F:
    # "不要直接和普通 pool.jsonl 混成不可区分"。分开的三个好处:
    #   1. 想"只播 curated"只要换一个路径, 不必按来源筛;
    #   2. 可以单独清空/重建 curated 而不动 AI 生成的存量;
    #   3. 出问题时一眼看得出是哪批。
    curated_pool_path: str = os.path.join("data", "curated_pool.jsonl")
    curated_used_path: str = os.path.join("data", "curated_used.jsonl")
    #: 版权溯源(H2-H)。每道 curated 题都要能反查"来自哪 / 作者是谁 /
    #: 什么许可 / 是否翻译"。
    attributions_path: str = os.path.join("data", "ATTRIBUTIONS.jsonl")

    #: **取题顺序(G4-2 起)**: 生成池(keyword2) -> curated -> keyword2 现场生成。
    #:
    #: ## 默认 False —— curated 是 **opt-in**
    #:
    #: 产品决定(任务书 §一/§二):
    #:
    #:     默认直播不再使用 external curated/downloaded 海龟汤作为主题源。
    #:     默认直播的唯一主生成体系是 keyword2。
    #:     下载的完整谜题保留为可选题库 / benchmark / emergency reserve,
    #:     但默认不参与实播调度。
    #:
    #: 于是正常直播里观众**只感受到一套生成风格**。external 题的风格与
    #: AI 生成题差得很远(谜面长度、语感、机关密度), 混在一起播会让
    #: "这一场是什么调性"变得不可控 —— 那正是 H2 之后一直存在的问题。
    #:
    #: `False` 时**完全不碰** curated 池(而不是"最后再试"), 与
    #: `pool_enabled` 同一条原则: 关掉就要是真的关掉 —— 连文件都不读,
    #: Lazy Curator 也不启动(§二: 默认 0 次 LLM 调用)。
    #:
    #: 显式开启用 `--curated`。`--no-curated` 保留为兼容参数, 两者
    #: 都写 `prefer_curated`; 默认值本身就是 False, 所以不写任何 flag
    #: 时就是"关"。
    prefer_curated: bool = False
    #: **现场 AI 生成默认关闭**(H2-G 第一阶段)。
    #:
    #: 理由: 这批的目的正是"看看题源换掉以后风格是不是立刻变好"。
    #: 若现场生成照常开着, 池子一空就回到 AI 造题 —— 那样测出来的
    #: 是混合风格, 分不清改善来自哪一边。
    #:
    #: ⚠️ 关掉**不等于**开天窗: curated + 生成池都没有时, 引擎仍会走
    #: 它自己的结构化兜底(`_riddle_failed_locked`), 直播不会中断。
    allow_live_generation: bool = True

    # ---- Batch H2-G: 补齐外部题库的实时性 ----
    #: curated 池的补池总开关。与 `pool_prefetch_enabled` 分开: curated
    #: 的补池是**离线编译**(compile_curated.py), 不是直播时后台生成 ——
    #: 直播期间不该调它。这个开关只控制"是否把 curated 池纳入取题候选"。
    curated_pool_enabled: bool = True

    # ---- Batch H3-B: Lazy Curator(后台按需审题) ----
    #
    # 库存迟滞: **低于 min** 或"下一题没得播"才开始补, 一路补到 target。
    # 为什么不用"低于 target 就补": 那会让库存一跌破 10 就立刻开审 ——
    # 而那时正是观众在提问的时候。迟滞让它等到真快空了再动, 一次补一批。
    curated_target_size: int = 10
    curated_min_size: int = 4
    curated_playable_min: int = 2
    #: 硬上限, 防无限增长。到这儿就停, 即使 playable 还是 0。
    curated_max_size: int = 20
    #: 后台审题总开关。
    curated_background_enabled: bool = True
    #: 单条预算(秒)。超了记 technical_defer(**不是** rejected)——
    #: 一道题卡住不该吃掉整个后台窗口, 也不该被永久判死。
    curated_budget_seconds: float = 45.0
    #: 临近下一题多少秒内不再启动新的审题。与 `pool_reveal_start_guard_seconds`
    #: 同一个思路: 启动一个注定跨过 deadline 的调用是纯浪费。
    curated_start_guard_seconds: float = 15.0
    #: 候选语料(**只读**)。默认就是 H1-D 的产出。
    curated_corpus_path: str = os.path.join(
        "data_external", "normalized", "curated_raw.jsonl")
    #: 决策账本(H3-A)。"这条路处理过没有"的**唯一**权威。
    curated_decisions_path: str = os.path.join(
        "data", "curated_decisions.jsonl")

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
    # v8 收紧 2 -> 1: 实播里"没有翻转、就是正面解释"和"主要靠制度性
    # 设定成立"这两类最容易让观众觉得无聊。**这是有意的内容 policy
    # 修改**(任务书明确), 不是"调整现有 quota 数值"那条限制的对象。
    quota_straight_explanation: int = 1   # "没有翻转的正面解释"上限
    quota_neutral_emotion: int = 3        # "中性气氛"上限(去掉固定偏置后的护栏)
    quota_procedural_rule: int = 1        # 主要靠制度性设定成立的题上限

    # ---- v8: 最近 10 题的"诡异/紧张"目标带 ----
    # 内容基调要**代码化**, 不能只写在 prompt 里 —— prompt 说了模型也
    # 可能连续出 10 道温馨题。目标: 最近 10 题里约 5~6 道是 eerie/tense
    # (不低于 ~50%, 不长期超过 ~60%)。
    # 区间而不是定值: 定值会让调度器每轮都硬凑, 反而挤压 absurd/warm/
    # neutral/grief 的空间 —— 那些也要留位置。
    quality_dark_tone_min: int = 5
    quality_dark_tone_max: int = 6

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
        # 硬上限低于高水位 -> "补到 target 就停"与"最多到 max"互相矛盾,
        # max 会先触发, target 永远达不到, 滞回退化成"补到 max 为止"。
        # 与 target < min 一样是必然的误配置, 不是风格问题。
        if self.pool_max_size < self.pool_target_size:
            warns.append(
                f"pool_max_size({self.pool_max_size}) < "
                f"pool_target_size({self.pool_target_size}): 硬上限低于高水位, "
                f"补池会卡在 max 上, 永远补不到 target。要让 max >= target。"
            )
        if self.pool_playable_min < 0:
            warns.append(
                f"pool_playable_min({self.pool_playable_min}) 为负, 已按 0 处理"
                f"(0 = 关掉\"下一题缺货\"这个触发条件, 只看长期库存)。"
            )
        # ---- 揭晓窗口 ----
        if not (0 <= self.reveal_core_focus_seconds < self.reveal_hold_seconds):
            warns.append(
                f"reveal_core_focus_seconds({self.reveal_core_focus_seconds}) "
                f"必须落在 [0, reveal_hold_seconds({self.reveal_hold_seconds})) "
                f"内: 核心答案独占时段不能是负的, 也不能长过整个揭晓展示"
                f"(否则完整解释永远不会出现)。"
            )
        # U2: 三段的中间边界必须严格落在两段之间。
        # 注意允许 `reveal_detail_seconds == reveal_hold_seconds`(完整解释
        # 一直显示到下一题)与 `== reveal_core_focus_seconds`(跳过中间段),
        # 但**不允许**越界或倒挂 —— 那会让某一段永远不出现, 且前端拿到
        # 一个自相矛盾的 stage。
        if not (self.reveal_core_focus_seconds
                <= self.reveal_detail_seconds
                <= self.reveal_hold_seconds):
            warns.append(
                f"reveal_detail_seconds({self.reveal_detail_seconds}) 必须落在 "
                f"[reveal_core_focus_seconds"
                f"({self.reveal_core_focus_seconds}), "
                f"reveal_hold_seconds({self.reveal_hold_seconds})] 内: "
                f"完整解释的结束时刻不能早于核心答案独占结束, 也不能晚过"
                f"整个揭晓展示。"
            )
        if self.reveal_hold_seconds <= 0:
            warns.append(
                f"reveal_hold_seconds({self.reveal_hold_seconds}) <= 0: "
                f"揭晓会瞬间跳过, 观众看不到答案。"
            )
        if self.pool_reveal_target_size < self.pool_target_size:
            warns.append(
                f"pool_reveal_target_size({self.pool_reveal_target_size}) < "
                f"pool_target_size({self.pool_target_size}): 揭晓窗口是"
                f"最富裕的补池时机, 目标不该比 QA 期间还低。"
            )
        if self.pool_reveal_target_size > self.pool_max_size:
            warns.append(
                f"pool_reveal_target_size({self.pool_reveal_target_size}) > "
                f"pool_max_size({self.pool_max_size}): 揭晓目标高于硬上限, "
                f"永远补不到。"
            )
        if self.pool_reveal_start_guard_seconds < 0:
            warns.append(
                f"pool_reveal_start_guard_seconds("
                f"{self.pool_reveal_start_guard_seconds}) 为负, 已按 0 处理。"
            )
        if self.pool_prefetch_backoff_s <= 0:
            warns.append(
                f"pool_prefetch_backoff_s({self.pool_prefetch_backoff_s}) <= 0: "
                f"补池失败后不会退避, 4Hz 的 tick 会打成失败风暴。"
            )
        # ---- G1: 补池预算 / guard 自洽 ----
        if self.pool_prefetch_max_attempts < 1:
            warns.append(
                f"pool_prefetch_max_attempts({self.pool_prefetch_max_attempts}) "
                f"< 1: 后台补池连一稿都不会出, 池子永远补不上。"
            )
        # ---- G2: keyword2 开了但补池整个关着 ----
        # 这不是错误(两个开关各自都合法), 但组合起来的结果是"配了个寂寞"
        # —— 唯一会读 keyword2 的消费者是 prefetcher, 而它根本不在跑。
        # 说一句免得运维以为"开了 keyword2 却没生效"是代码 bug。
        if self.pool_keyword_seed_enabled and not self.pool_prefetch_enabled:
            warns.append(
                f"pool_keyword_seed_enabled("
                f"{self.pool_keyword_seed_enabled}) 为真但 "
                f"pool_prefetch_enabled("
                f"{self.pool_prefetch_enabled}) 为假: 后台补池整个没在跑, "
                f"keyword2 不会被用到。要测 keyword2 就两个都开; "
                f"要关后台生成就两个都关。"
            )
        # ---- G3: 显式指了一个不存在的 corpus ----
        # 这条比"配置了个寂寞"更尖锐: 用户**明确**要求用某份 corpus, 而它
        # 不在。运行时会显式降级(打 ERROR 后退回 classic), 所以不是错误;
        # 但配置期就该告诉他, 免得实播时才发现关键词不是他要的那份。
        if (self.pool_keyword_seed_enabled and self.keyword_corpus_path
                and not os.path.exists(self.keyword_corpus_path)):
            warns.append(
                f"keyword_corpus_path({self.keyword_corpus_path}) 不存在: "
                f"运行时 keyword2 会**显式降级**回 classic Blueprint 链"
                f"(不会回退人工词库)。构建: "
                f"`uv run tools/build_keyword_seed_corpus.py`。"
            )
        if self.pool_prefetch_budget_seconds <= 0:
            warns.append(
                f"pool_prefetch_budget_seconds("
                f"{self.pool_prefetch_budget_seconds}) <= 0: 同上的效果 —— "
                f"补池每次都立刻超预算退出。"
            )
        if self.pool_prefetch_guard_margin_seconds < 0:
            warns.append(
                f"pool_prefetch_guard_margin_seconds("
                f"{self.pool_prefetch_guard_margin_seconds}) 为负, 已按 0 处理。"
            )
        # guard 必须至少覆盖一轮补池预算。不满足时 `PoolPrefetcher` 会
        # **取 max() 兜底**(不是静默照旧), 所以这里只提示"你配的这个
        # 数被抬高了", 让运维知道生效值不是他写的那个。
        _min_guard = (self.pool_prefetch_budget_seconds
                      + self.pool_prefetch_guard_margin_seconds)
        if 0 < self.pool_reveal_start_guard_seconds < _min_guard:
            warns.append(
                f"pool_reveal_start_guard_seconds("
                f"{self.pool_reveal_start_guard_seconds}) < 一轮补池预算"
                f"({_min_guard:.0f}s = budget "
                f"{self.pool_prefetch_budget_seconds:.0f} + 余量 "
                f"{self.pool_prefetch_guard_margin_seconds:.0f}): 实际生效值"
                f"会被抬到 {_min_guard:.0f}s。否则'只剩这么多秒'时启动的"
                f"后台生成注定跨过 deadline, 与下一题的现场生成抢网关 —— "
                f"这正是 G1 要消灭的跨场景白烧。"
            )
        _sched = tuple(self.pool_prefetch_backoff_schedule_s or ())
        if not _sched:
            warns.append(
                "pool_prefetch_backoff_schedule_s 为空: 补池连续失败不会"
                "递增退避, 会退回固定间隔反复重试同一个上下文。"
            )
        elif any(float(x) <= 0 for x in _sched):
            warns.append(
                f"pool_prefetch_backoff_schedule_s({_sched}) 含非正数: "
                f"那一档等于不退避。"
            )
        elif list(_sched) != sorted(_sched):
            warns.append(
                f"pool_prefetch_backoff_schedule_s({_sched}) 不是递增的: "
                f"连续失败时退避反而变短, 与'越失败越该等久'的意图相反。"
            )
        if self.phase_ack_seconds <= 0:
            warns.append(
                f"phase_ack_seconds({self.phase_ack_seconds}) <= 0: "
                f"非 QA 阶段的 #问题 提示不会节流, 多人同发时会刷屏。"
            )
        # H1: 问答数触发提示。负值会被 int() 截成 0(= 关掉), 但那多半是
        # 配置写错了, 而不是"我想关掉"——显式写 0 才是关掉。
        if self.hint_questions_per_level < 0:
            warns.append(
                f"hint_questions_per_level({self.hint_questions_per_level}) "
                f"为负, 已按 0 处理(0 = 关掉问答数触发, 只看时间轴)。"
            )
        if self.hint_min_gap_seconds < 0:
            warns.append(
                f"hint_min_gap_seconds({self.hint_min_gap_seconds}) 为负, "
                f"已按 0 处理(等于没有冷却, 提示可能连发)。"
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
        if self.ai_player_min_gap_seconds < 0:
            warns.append("ai_player_min_gap_seconds 为负, 已按 0 处理。")
        if self.ai_player_retry_seconds <= 0:
            warns.append("ai_player_retry_seconds <= 0: 技术失败会高频重试。")
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
    ap.add_argument("--hint-questions-per-level", type=int, default=20,
                    help=("每多少条**成功的真人裁决**进一格提示(0=关掉"
                          "这条触发源, 只看时间轴), 默认 20"))
    ap.add_argument("--hint-min-gap-seconds", type=float, default=45.0,
                    help="两条提示之间的最小间隔(秒), 防止连发, 默认 45")
    ap.add_argument("--restate-seconds", type=float, default=120.0,
                    help="多久无人发言就重述谜面(零成本), 默认 120")
    ap.add_argument("--giveup-seconds", type=float, default=1800.0,
                    help="硬性兜底(一般轮不到), 默认 1800")
    ap.add_argument("--reveal-hold", type=float, default=60.0,
                    help="揭晓展示时长(秒), 默认 60")
    ap.add_argument("--reveal-core-focus", type=float, default=15.0,
                    help="揭晓前多少秒只显示核心答案(超大字号), 默认 15")
    ap.add_argument("--reveal-detail-until", type=float, default=45.0,
                    help="完整解释显示到揭晓的第几秒(之后让位给共同解谜), "
                         "默认 45")
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
    # ---- G2: keyword2 两阶段起题(kill-switch) ----
    ap.add_argument("--no-keyword-seed", dest="pool_keyword_seed_enabled",
                    action="store_false",
                    help="后台补池**与 live 现场生成一起**回到旧的一阶段 "
                         "Blueprint 链(`pick_blueprint -> gen_spec`)。"
                         "默认**开启** keyword2: 随机抽 2 个普通生活关键词 "
                         "-> 先想清真相/现场线索/发生顺序, 再写谜面谜底 "
                         "(Case-first)-> 再结构化。curated 链本来就不走它")
    # ---- G3: keyword2 的 seed 来源 ----
    ap.add_argument("--keyword-corpus", dest="keyword_corpus_path", default="",
                    help="keyword2 的 seed 词库路径(默认 "
                         "data/keyword2_vocabulary.json)。文件不可用时"
                         "**显式降级**到 classic Blueprint 链, 不会回退"
                         "人工词库。构建: tools/build_keyword_seed_corpus.py")
    ap.add_argument("--keyword-session-seed", dest="keyword_session_seed",
                    type=int, default=None,
                    help="keyword bag 的 session seed。默认由 quality_seed "
                         "派生; quality_seed 也没给时启动随机一次并写进 "
                         "INFO 日志(可从日志抄回来重放)")
    # ---- Batch H2-F/G: curated 池(**G4-2 起 opt-in**) ----
    #
    # ⚠️ `--curated` 与 `--no-curated` **写同一个 dest**(`prefer_curated`),
    # 所以两者天然不会打架 —— `argparse` 的后者覆盖前者, 而 dataclass 的
    # 默认值(False)就是"不写任何 flag 时"的口径。
    #
    # 为什么不做成两个独立布尔: 两个开关管同一件事, 迟早会出现
    # `--curated --no-curated` 同时给而代码只读其中一个的情况, 那时
    # "到底开没开"就得靠猜。一个 dest 让"最后写的那个赢"成为唯一规则。
    ap.add_argument("--curated", dest="prefer_curated",
                    action="store_true",
                    help="**显式启用** external curated(下载题库)作为补充题源。"
                         "默认**关闭** —— 默认直播的唯一主生成体系是 "
                         "keyword2, 观众只感受到一套生成风格。开了之后: "
                         "加载 curated 池 + 启动 Lazy Curator + 允许下载题"
                         "进入取题顺序(**仍排在 generated 之后**)。")
    ap.add_argument("--no-curated", dest="prefer_curated",
                    action="store_false",
                    help="显式关闭 curated(与不写 flag 同义, 保留为兼容参数)。")
    ap.set_defaults(prefer_curated=False)
    ap.add_argument("--curated-pool", dest="curated_pool_path", default=None,
                    help="curated 池文件路径(默认 data/curated_pool.jsonl)")
    ap.add_argument("--no-live-generate", dest="allow_live_generation",
                    action="store_false",
                    help="**关闭现场 AI 生成**(H2-G 第一阶段默认建议开这个)。"
                         "两个池都空时不回到 AI 造题, 而是走引擎兜底 —— "
                         "这样测出来的风格改善才不会被 AI 现造的题混掉。"
                         "注意这不等于开天窗: 直播不会中断")
    ap.add_argument("--prefetch-max-attempts", type=int, default=2,
                    help="后台补池每道题最多出几稿(默认 2)。直播现场出题"
                         "是 4 稿 —— 那是观众在干等时的预算; 后台补池只是"
                         "'有空补一道', 多试一稿的收益远小于它和直播抢网关"
                         "的代价")
    ap.add_argument("--prefetch-budget", type=float, default=25.0,
                    help="后台补池每道题的秒预算(默认 25)。与直播现场出题的"
                         "90s 分开, 免得一轮后台生成跨过下一题的 deadline")
    ap.add_argument("--pool-reveal-guard", type=float, default=30.0,
                    help="距下一题不足这个秒数就不再启动新的补池请求"
                         "(默认 30)。必须 >= --prefetch-budget, 否则会取 max()"
                         "抬高")
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

    # ---- Step 12C: gift_capture_diagnostic(诊断模式, 非业务模式) ----
    #
    # 这四个 flag 刻意都带 `gift-probe` 前缀: 它们只影响**诊断**行为,
    # 与直播业务参数混在一起会让"我到底改了业务还是只开了诊断"这个问题
    # 在 `--help` 里看不出来。
    ap.add_argument("--gift-capture-diagnostic", dest="gift_capture_diagnostic",
                    action="store_true",
                    help="**独占诊断会话**(不是直播旁路): 本进程只跑采集, "
                         "额外开 1~3 路受控 WS 连接抓 Gift 原始证据, 每路写"
                         "独立目录, 跑完即退出 —— Director 不会启动。"
                         "不接公告/不感谢礼物/不改 SummonLedger(诊断连接不接"
                         "任何业务回调)。默认关闭。")
    ap.add_argument("--gift-probe-profiles", dest="gift_probe_profiles",
                    default="",
                    help=("要开的画像名, 逗号分隔。可选: "
                          "current-auth-control / random-uid-only / "
                          "reference-2026。留空 = 默认三臂。拼错的名字会"
                          "报错(不静默忽略)。"))
    ap.add_argument("--gift-probe-limit", type=int, default=3,
                    help="最多同时开几路(硬上限 3), 默认 3")
    ap.add_argument("--gift-probe-dir", dest="gift_probe_dir",
                    default=os.path.join("data", "gift_probe"),
                    help="证据目录根, 默认 data/gift_probe(已 gitignored)。"
                         "不能传 data 本身。")
    ap.add_argument("--gift-probe-max-per-method", type=int, default=20,
                    help="每个 (profile, method) 最多留几个原始样本, 默认 20")
    ap.add_argument("--gift-probe-summary-seconds", type=float, default=25.0,
                    help="实时摘要间隔(秒), 默认 25(Issue 建议 20~30)")

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
        reveal_core_focus_seconds=a.reveal_core_focus,
        reveal_detail_seconds=a.reveal_detail_until,
        max_hints=a.max_hints,
        hint_questions_per_level=a.hint_questions_per_level,
        hint_min_gap_seconds=a.hint_min_gap_seconds,
        tick_hz=a.tick_hz,
        stall_seconds=a.stall_seconds,
        max_puzzles=a.max_puzzles,
        # 只加 flag 不在这里接上 = 又一个 dead config(参数形同虚设,
        # 而 --help 里明明写着)。加 flag 和接线必须同一处完成。
        pool_prefetch_enabled=a.pool_prefetch_enabled,
        prefer_curated=a.prefer_curated,
        allow_live_generation=a.allow_live_generation,
        curated_pool_path=(a.curated_pool_path
                           or Config.curated_pool_path),
        pool_prefetch_max_attempts=a.prefetch_max_attempts,
        pool_prefetch_budget_seconds=a.prefetch_budget,
        pool_keyword_seed_enabled=a.pool_keyword_seed_enabled,
        keyword_corpus_path=a.keyword_corpus_path,
        keyword_session_seed=a.keyword_session_seed,
        pool_reveal_start_guard_seconds=a.pool_reveal_guard,
        playtest_enabled=a.playtest_enabled,
        playtest_max_turns=a.playtest_max_turns,
        max_question_len=a.max_question_len,
        # ---- Step 12C: gift_capture_diagnostic ----
        # 加 flag 与接线必须同一处完成, 否则就是 dead config
        # (参数形同虚设, 而 --help 里明明写着)。
        gift_capture_diagnostic=a.gift_capture_diagnostic,
        gift_probe_profiles=a.gift_probe_profiles,
        gift_probe_limit=a.gift_probe_limit,
        gift_probe_dir=a.gift_probe_dir,
        gift_probe_max_per_method=a.gift_probe_max_per_method,
        gift_probe_summary_seconds=a.gift_probe_summary_seconds,
        host=a.host,
        port=a.port,
        open_window=a.open_window,
        no_llm=a.no_llm,
        llm=llm,
        log_level=a.log_level,
        log_file=a.log_file,
    )
    return cfg
