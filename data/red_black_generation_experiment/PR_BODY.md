## 这是什么

**纯实验 + 审计。不改 production。** `story/` `director.py` `tests/` `.github/` 零 diff。

## 一、内容约束审计 (`data/red_black_generation_experiment/audit.md`)

逐条列出所有在塑造"直白解释题"的规则（文件:行号 + 副作用 + 建议）。

**根因**：`fair_clues` 契约（`story/quality.py:719-801`）把"公平"实现成
「答案的**逐字证据必须已经写在谜面上**」，再加上 Stage A prompt 第 3 步
「关键步骤没痕迹就回去补一条线索」—— 生成器被**两次**要求把证据塞进谜面。
于是谜面从"一个让人想问的切片"退化成"完整的现场说明"，
谜底只剩"把这些已写出的证据串起来"。

## 二、三臂实验 (`tools/red_black_gen.py`)

| 臂 | 说明 | 每格调用 |
|---|---|---|
| A CURRENT | **原样引用**生产 `KEYWORD_IDEA_SYSTEM` / `_TOOL_KEYWORD_IDEA`（未复制） | 1 |
| B STYLE | 一次调用，注意力在"红黑调性 + 完整故事 + 截取最怪的切片" | 1 |
| C STORY | 两次调用：Call1 只创作隐藏故事 → Call2 只截取汤面 | 2 |

关键词在**任何模型调用之前**冻结（`groups_md5 = e01ffd6fa7b19934b11d2fdae63b98e4`）。
不 retry、不 best-of-N、不变 prompt。实验 prompt 的"生产形状词"黑名单做成
**导入期断言**。

## 三、结果 (`report.md` / `raw.md`)

24 道，32 次调用，`deepseek-v4.1-flash`。

谜面字数中位数：**A 102 / B 68 / C 178**。

**最有价值的产物是 `static_gate.json`**（0 LLM 调用的代码门分析）：

| 臂 | 结尾非问号 | 第一人称 | 谜面>220 fail | 汤底>300 fail | 完全干净 |
|---|---|---|---|---|---|
| A | 5/8 | 0/8 | 0/8 | 0/8 | **3/8** |
| B | 8/8 | 1/8 | 0/8 | 0/8 | **0/8** |
| C | 8/8 | 0/8 | 3/8 | 4/8 | **0/8** |

"结尾非问号"在 `validate_spec` 里是 `can_fix`，但 `story/llm.py:5557` 把它
升级成**硬改**（Reviewer 不改就 rewrite → 整稿丢）。**24 道里 21 道中招，
其中包括生产自己的 A 臂 5/8。**

→ 回答任务书 §十一 那个必须分开的问题：**不是 Prompt 出不来好题，
是 Prompt 一放宽，好题就被框架的形状门修坏/拒掉。**

## 四、建议先放宽的 5 个 hard gate

1. 谜面结尾必须问号（→ "含可调查异常"）
2. `fair_clues` 必须逐字预置（→ 拆分：异常锚点 hard + QA 可获得）
3. "好不好玩"四项作为自由生成硬门（→ 降 soft；curated 侧已有同款先例）
4. `discovery_beats` 强制 2~4（→ 观察指标）
5. 第一人称硬改第三人称

详见 `report.md` §7 / §8（含最小改动方案）。**本轮不实施。**

## 五、验证

* `git status --short story/ director.py tests/ .github/ story/config.py` → **0 行**
* 离线套件全绿（0 fail）：test_llm 785 / test_prefetch 403 / test_pool 338 /
  test_engine 657 / test_keyword_seed 302 / test_g4_source 253 / test_puzzle 414
* 无真实观众数据（未提交 `_export_live*` / `danmaku.jsonl` / 昵称 / session 日志）

🤖 Generated with [Claude Code](https://claude.com/claude-code)
