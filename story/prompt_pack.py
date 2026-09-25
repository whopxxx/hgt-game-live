#!/usr/bin/env python
# coding: utf-8
"""Generation Prompt Pack loader(Issue #50 Phase B1)。

把生成侧静态 Prompt(system instruction / 角色定义 / 阶段职责 / 硬语义
规则 / 反例判据)从 `story/llm.py` 抽成**版本化**的 Markdown 文件,
由本模块统一加载 —— 单一事实来源是 `haiguitang/prompts/generation/`
下的文件, **不是** Python 常量。

## 职责边界(任务书 §5)

    .md 文件拥有:  system instruction / 角色 / 阶段职责 / 硬语义规则 / 反例
    Python 拥有:   运行时 puzzle / answer / keywords / transcript / avoid /
                   recent / blueprint / GenerationBrief / 动态约束 /
                   tool schema / timeout / temperature / retry

所以本 loader 刻意**很薄**: 没有 Jinja、没有模板引擎、没有网络加载、
没有热重载、没有编辑后台。Prompt 就是随 commit 一起版本化的源文件。

## 硬纪律

    - **allowlist**: 只能加载 `STAGES` 里登记的 stage, 文件名来自登记表
      而不是调用方输入 —— `load_prompt("../../secret")` 在结构上不可能
      (stage 不在 allowlist 里, 直接报错)。
    - **cwd 无关**: 根目录由 `Path(__file__)` 锚定, repo root / tests/ /
      任意 cwd 启动行为一致。
    - **fail closed**: unknown stage / 文件缺失 / 文件为空 / 只有空白 /
      读取失败 / 残留未渲染占位符 —— 一律抛 `PromptPackError`。
      绝不允许"读失败就回退 llm.py 老常量"(否则单一事实来源是假的)。
      注意: prompt 文件缺失是**代码/部署错误**, 不是 LLM 技术重试的
      理由 —— 调用方不得把它混进 retry 链。

零新依赖, 不 import story 其它模块(llm.py 会 import 本模块)。
"""

from __future__ import annotations

import re
from pathlib import Path

#: Generation Prompt Pack 的**总版本**。语义: "这一套生成 Prompt 的
#: 版本", 写进成功 spec 的 `prompt_version` 与 metrics 的
#: `prompt_pack_version`。它与 stage 文件版本(如 `truth-v1`)是两个
#: 概念: 总版本变**或**任何一个 stage 文件变, 总版本都应 bump。
HAIGUITANG_GENERATION_PROMPT_VERSION = "haiguitang-generation-v1"

#: 别名 —— 语义同上, 读起来更顺手的场合用。
PROMPT_PACK_VERSION = HAIGUITANG_GENERATION_PROMPT_VERSION

#: 项目根锚点: `story/prompt_pack.py` -> 上两级 = repo root。
#: **绝不**用 cwd —— 服务可能从任意目录启动。
_PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: Prompt Pack 根目录。测试可以用 `monkeypatch` 指到临时目录造
#: 缺文件/空文件/坏文件的反例(生产代码**永远**不该改它)。
PROMPT_ROOT = _PROJECT_ROOT / "haiguitang" / "prompts" / "generation"

#: stage -> (文件名, stage 版本)。
#:
#: **allowlist** 是加载的唯一入口: 文件名由登记表决定, 不接受调用方
#: 拼路径。新增 stage = 在这里登记 + 新建对应 .md, 二者缺一即 fail
#: closed(测试 §60 会抓)。
STAGES: dict = {
    "truth": ("truth-v1.md", "truth-v1"),
    "surface": ("surface-v1.md", "surface-v1"),
    "contract": ("contract-v1.md", "contract-v1"),
    "audit": ("audit-v1.md", "audit-v1"),
    "audit_truthfulness": ("audit-truthfulness-v1.md",
                           "audit-truthfulness-v1"),
    "audit_safety": ("audit-safety-v1.md", "audit-safety-v1"),
}

#: 静态 prompt 文件里**不允许**出现的未渲染占位符(§6/§60)。
#: Generation Pack v1 是全静态的 —— lane / requested category /
#: difficulty 等运行时数据一律由 Python 拼 user message, 不进 system。
#: 检测的是 `{name}` 形式的模板洞; `{{`/`}}` 转义对与本包无关,
#: 同样不允许(需要字面花括号的 prompt 应该换措辞, 而不是依赖转义)。
_PLACEHOLDER_RE = re.compile(r"\{[^{}\n]*\}")


class PromptPackError(Exception):
    """Prompt Pack 加载/渲染失败的**唯一**异常类型。

    调用方**不得**把它当成可重试的 LLM 技术失败 —— 文件缺失/为空/
    有模板洞是代码或部署错误, 必须让生成链立刻明确失败(fail closed),
    而不是烧一次重试再得到同一个错误。
    """


def _stage_file(stage: str) -> tuple:
    """查 allowlist。unknown stage -> 明确错误(不做任何路径拼接)。"""
    entry = STAGES.get(str(stage or ""))
    if entry is None:
        raise PromptPackError(
            f"unknown prompt stage: {stage!r} (allowed: {sorted(STAGES)})")
    return entry


def load_prompt(stage: str) -> str:
    """加载一个 stage 的静态 prompt 文本。

    fail closed: unknown stage / 文件不存在 / 读失败 / 内容为空或只有
    空白 / 含未渲染占位符 —— 全部抛 `PromptPackError`。

    每次调用都重新读盘(本包调用频率 = 每道题每 stage 一次, 几秒级
    间隔, IO 可忽略) —— 不做缓存就没有"改了文件读到旧的"一类问题。
    """
    filename, _version = _stage_file(stage)
    # 文件名来自上面的登记表(字面量), 不是调用方输入 —— 没有
    # 路径穿越面。这里仍然 resolve 一次, 让错误信息带绝对路径。
    path = PROMPT_ROOT / filename
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise PromptPackError(
            f"prompt file missing for stage {stage!r}: {path}") from e
    except OSError as e:
        raise PromptPackError(
            f"prompt file unreadable for stage {stage!r}: {path} ({e})") from e
    if not text.strip():
        raise PromptPackError(
            f"prompt file for stage {stage!r} is empty: {path}")
    hole = _PLACEHOLDER_RE.search(text)
    if hole:
        raise PromptPackError(
            f"prompt file for stage {stage!r} contains an unresolved "
            f"placeholder {hole.group()!r} at offset {hole.start()} "
            f"(Generation Pack v1 prompts are static; runtime data "
            f"belongs in the user message): {path}")
    return text


def render_prompt(stage: str, variables: dict) -> str:
    """渲染一个 stage(带**strict** 变量检查)。

    v1 的全部 stage 都是静态的 —— 调用方应使用 `load_prompt`。
    保留本函数是为了把"模板未完整解析必须 fail closed"(§6/§60)钉死
    在代码里: 变量缺失、多余的位置参数、渲染后残留 `{...}` 占位符,
    任何一种都抛 `PromptPackError`, 绝不把 `{foo}` 原样发给模型。
    """
    text = load_prompt(stage)
    if not variables:
        return text
    try:
        rendered = text.format(**dict(variables))
    except (KeyError, IndexError, ValueError) as e:
        raise PromptPackError(
            f"prompt render failed for stage {stage!r}: {e!r}") from e
    hole = _PLACEHOLDER_RE.search(rendered)
    if hole:
        raise PromptPackError(
            f"prompt render left an unresolved placeholder "
            f"{hole.group()!r} for stage {stage!r}")
    return rendered


def stage_version(stage: str) -> str:
    """某个 stage 文件的版本(如 `"truth-v1"`)。进 metrics, 不进 spec。"""
    return _stage_file(stage)[1]
