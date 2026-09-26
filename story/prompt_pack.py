#!/usr/bin/env python
# coding: utf-8
"""Prompt Pack loader(Generation: Issue #50 Phase B1 / Judging: Issue #53)。

把生成侧与判题侧的静态 Prompt(system instruction / 角色定义 / 阶段职责
/ 硬语义规则 / 反例判据)从 `story/llm.py` 抽成**版本化**的 Markdown
文件, 由本模块统一加载 —— 单一事实来源是 `haiguitang/prompts/` 下的
文件, **不是** Python 常量。

## 两个 Pack, 两个独立总版本(Issue #53 §3)

    generation -> haiguitang/prompts/generation/   haiguitang-generation-v1
    judging    -> haiguitang/prompts/judging/      haiguitang-judging-v1

"怎么出题"与"怎么理解观众的一句话"是两个独立演进的东西: 出题侧换
prompt 不该迫使判题侧 bump 版本, 反之亦然。所以总版本**必须**分开,
stage 名也**不许**跨 pack 重名(allowlist 全局唯一)。

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
#:
#: v2(5 大类协议): Contract / Audit 的分类语义从 11 类收敛成 5 大类
#: (haiguitang-v2), 属于 generation prompt 的**行为变化** —— 总版本
#: 必须 bump。Judging Pack 没有语义变化, **不**牵动。
HAIGUITANG_GENERATION_PROMPT_VERSION = "haiguitang-generation-v2"

#: 别名 —— 语义同上, 读起来更顺手的场合用。
PROMPT_PACK_VERSION = HAIGUITANG_GENERATION_PROMPT_VERSION

#: Judging Prompt Pack 的**总版本**(Issue #53 §3)。语义: "这一套判题
#: Prompt 的版本", 写进 QA archive 的 `judging_prompt_version`。
#: ⚠️ 与 generation 总版本**互相独立**: 任何一个判题 stage 文件变了
#: 它才 bump; generation 侧的任何变化**不得**牵动它(反之亦然),
#: 否则 #50 的 provenance 语义就漂移了。
HAIGUITANG_JUDGING_PROMPT_VERSION = "haiguitang-judging-v1"

#: 项目根锚点: `story/prompt_pack.py` -> 上两级 = repo root。
#: **绝不**用 cwd —— 服务可能从任意目录启动。
_PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: Prompt Pack 根目录。测试可以用 `monkeypatch` 指到临时目录造
#: 缺文件/空文件/坏文件的反例(生产代码**永远**不该改它)。
PROMPT_ROOT = _PROJECT_ROOT / "haiguitang" / "prompts" / "generation"

#: Judging Pack 根目录(Issue #53 §2)。与 `PROMPT_ROOT` 同级、互相
#: 独立 —— 测试同样可以 monkeypatch 它造反例。
JUDGING_PROMPT_ROOT = _PROJECT_ROOT / "haiguitang" / "prompts" / "judging"

#: stage -> (文件名, stage 版本)。
#:
#: **allowlist** 是加载的唯一入口: 文件名由登记表决定, 不接受调用方
#: 拼路径。新增 stage = 在这里登记 + 新建对应 .md, 二者缺一即 fail
#: closed(测试 §60 会抓)。
STAGES: dict = {
    "truth": ("truth-v1.md", "truth-v1"),
    "surface": ("surface-v1.md", "surface-v1"),
    # ---- 5 大类协议 v2: Contract / Audit 的分类文字换 v2 文件 ----
    # 历史 contract-v1.md / audit-v1.md **保留在盘上**(它们是 v1 时代
    # 的合同文本, 不能覆盖其含义), 但当前生成链已指向 v2。
    "contract": ("contract-v2.md", "contract-v2"),
    "audit": ("audit-v2.md", "audit-v2"),
    "audit_truthfulness": ("audit-truthfulness-v1.md",
                           "audit-truthfulness-v1"),
    "audit_safety": ("audit-safety-v1.md", "audit-safety-v1"),
}

#: Judging Pack 的 stage 登记(Issue #53 §2)。与 generation 的 stage
#: 名**全局不重名** —— allowlist 查找是两级线性表, 重名会让
#: `load_prompt("answer")` 的归属变得含糊, 直接禁止。
JUDGING_STAGES: dict = {
    "answer": ("answer-v1.md", "answer-v1"),
    "candidate_recheck": ("candidate-recheck-v1.md", "candidate-recheck-v1"),
    "completion_verify": ("completion-verify-v1.md", "completion-verify-v1"),
}

#: 共享 fragment 登记(Issue #53 §2): Candidate Recheck 与 Completion
#: Verify 共用同一份"什么叫真正建立 completion fact"的语义合同。
#: fragment **不是**业务 stage —— 业务代码不直接把它当 system prompt
#: 用, 它只通过 stage 文件里的 `{{fragment:name}}` 标记展开。
FRAGMENTS: dict = {}

JUDGING_FRAGMENTS: dict = {
    "completion_specificity": ("completion-specificity-v1.md",
                               "completion-specificity-v1"),
}

#: fragment 展开标记: stage 文件里的一行 `{{fragment:name}}` 会在
#: **加载时**被替换成 fragment 文件的全文。展开发生在占位符检查
#: **之前**(标记本身含 `{}`, 不先展开就会被占位符检查误杀)。
#: name 只允许 `[a-z0-9_]` —— 它必须能撞上登记表, 不存在"拼路径"。
_FRAGMENT_RE = re.compile(r"\{\{fragment:([a-z0-9_]+)\}\}")

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


def _pack_of(stage: str) -> str:
    """stage -> pack 名。两个登记表都查不到 -> 明确错误。"""
    s = str(stage or "")
    if s in STAGES:
        return "generation"
    if s in JUDGING_STAGES:
        return "judging"
    raise PromptPackError(
        f"unknown prompt stage: {stage!r} (allowed: "
        f"{sorted(STAGES) + sorted(JUDGING_STAGES)})")


def _registry(pack: str, kind: str) -> dict:
    """pack + kind(stage|fragment) -> 登记表。"""
    if pack == "generation":
        return STAGES if kind == "stage" else FRAGMENTS
    return JUDGING_STAGES if kind == "stage" else JUDGING_FRAGMENTS


def _root_of(pack: str) -> Path:
    """pack -> 根目录。**读 live 全局**(而不是启动时快照): 测试靠
    monkeypatch 这两个名字注入坏目录, loader 必须每次调用时取。"""
    return PROMPT_ROOT if pack == "generation" else JUDGING_PROMPT_ROOT


def _stage_file(stage: str) -> tuple:
    """查 allowlist。返回 (pack, 文件名, 版本)。"""
    pack = _pack_of(stage)
    filename, version = _registry(pack, "stage")[str(stage or "")]
    return pack, filename, version


def _read_pack_file(pack: str, kind: str, name: str) -> str:
    """从登记表解析 (pack, kind, name) 并读文件。fail closed 全覆盖:
    unknown name / 文件缺失 / 读失败 / 空 / 只有空白。"""
    entry = _registry(pack, kind).get(str(name or ""))
    if entry is None:
        raise PromptPackError(
            f"unknown prompt {kind}: {name!r} in pack {pack!r} "
            f"(allowed: {sorted(_registry(pack, kind))})")
    path = _root_of(pack) / entry[0]
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise PromptPackError(
            f"prompt file missing for {kind} {name!r}: {path}") from e
    except OSError as e:
        raise PromptPackError(
            f"prompt file unreadable for {kind} {name!r}: {path} ({e})") from e
    if not text.strip():
        raise PromptPackError(
            f"prompt file for {kind} {name!r} is empty: {path}")
    return text


def load_fragment(name: str) -> str:
    """加载一个共享 fragment(单一来源的合同文本, Issue #53 §2)。

    fragment 本身也要过占位符检查, 且**禁止**嵌套 include —— 共享
    文本里再套共享文本会让展开顺序变成隐式契约, v1 不需要。
    """
    pack = "judging" if str(name or "") in JUDGING_FRAGMENTS else "generation"
    text = _read_pack_file(pack, "fragment", name)
    if _FRAGMENT_RE.search(text):
        raise PromptPackError(
            f"fragment {name!r} contains a nested fragment include "
            f"(v1 forbids nesting): {name}")
    hole = _PLACEHOLDER_RE.search(text)
    if hole:
        raise PromptPackError(
            f"fragment {name!r} contains an unresolved placeholder "
            f"{hole.group()!r}: static pack files carry no template holes")
    return text


def _expand_fragments(text: str, stage: str) -> str:
    """把 stage 文本里的 `{{fragment:name}}` 展开成 fragment 全文。

    在占位符检查**之前**跑(标记含 `{}`, 后跑会被误杀)。unknown /
    空 fragment 由 `load_fragment` 统一 fail closed。
    """
    def _sub(m: "re.Match") -> str:
        return load_fragment(m.group(1))
    return _FRAGMENT_RE.subn(_sub, text)[0]


def load_prompt(stage: str) -> str:
    """加载一个 stage 的静态 prompt 文本。

    fail closed: unknown stage / 文件不存在 / 读失败 / 内容为空或只有
    空白 / 坏 fragment / 含未渲染占位符 —— 全部抛 `PromptPackError`。

    每次调用都重新读盘(本包调用频率 = 每道题每 stage 一次, 几秒级
    间隔, IO 可忽略) —— 不做缓存就没有"改了文件读到旧的"一类问题。
    """
    pack, _filename, _version = _stage_file(stage)
    text = _read_pack_file(pack, "stage", stage)
    text = _expand_fragments(text, stage)
    hole = _PLACEHOLDER_RE.search(text)
    if hole:
        raise PromptPackError(
            f"prompt file for stage {stage!r} contains an unresolved "
            f"placeholder {hole.group()!r} at offset {hole.start()} "
            f"(pack prompts are static; runtime data "
            f"belongs in the user message)")
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
    """某个 stage 文件的版本(如 `"truth-v1"` / `"answer-v1"`)。

    进 metrics(QA 侧进 archive 的 prompt provenance), 不进 spec。
    """
    return _stage_file(stage)[2]


def fragment_version(name: str) -> str:
    """某个 fragment 文件的版本(如 `"completion-specificity-v1"`)。"""
    pack = "judging" if str(name or "") in JUDGING_FRAGMENTS else "generation"
    return _registry(pack, "fragment")[str(name or "")][1]
