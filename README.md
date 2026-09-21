# 竖屏 AI 海龟汤直播

AI 出一个诡异谜题 → 观众发 `#问题` 追问 → AI 对每条提问**秒回**裁决（是 / 不是 / 无关 / 接近了）
→ 有人猜中核心真相 → 揭晓谜底 → 展示 30 秒 → 自动出下一题 → 循环。
目标：可长时间自动运行，主播基本不干预。

底层是抖音弹幕抓取（只读，不发送、不控制直播间）。

---

## 为什么是海龟汤

之前做过「AI 写小说 + 观众投票决定下一段」，实测四个痛点全中：
看不懂（刷到是一段进行中的文字）、反馈太慢（要等 AI 写 30 秒 + 60 秒投票窗口）、
参与感弱（观众是读者不是玩家）、冷场难看（没观众时像 PPT）。

海龟汤把主角还给观众：**规则一句话讲清、每条弹幕都有即时回应、无人时 AI 自己扛。**

---

## 快速开始

```bat
cd /d D:\whopxxwu\douyin
set PYTHONIOENCODING=utf-8

rem 离线演示(不用开播, 不用联网)
uv run director.py --sim data/demo_script.jsonl

rem 快速测状态机(不调 LLM)
uv run director.py --sim data/demo_script.jsonl --no-llm

rem 接真实直播间
uv run director.py --live 813110862078

rem 手打弹幕
uv run director.py --stdin
```

然后浏览器打开 `http://127.0.0.1:8765/`（`?debug=1` 开调试面板，或页面里按 `D`）。
默认会**自动弹出一个 1080×1920 的直播输出窗口**（Chrome `--app` 模式，无地址栏）。

**接入直播伴侣**：添加**窗口捕获**源 → 选「AI 海龟汤直播」窗口 → 画布 **1080x1920**。
（或添加**浏览器源** → 地址 `http://127.0.0.1:8765/`，`--no-window` 可关掉自动弹窗。）

---

## 玩法流程

```
SETTING   AI 出谜题（3-12 秒）
      ↓
QA        谜面上屏。观众发 #问题 → AI 逐条秒回裁决
      ↓  揭晓条件（谁先满足谁生效）
REVEALING 生成完整谜底
      ↓
REVEALED  谜底上屏，展示 30 秒
      ↓
      回到 SETTING（自动出下一题）
```

### 时间轴（控制一道题的总时长）

**从出题那刻起算，与观众发言完全无关** —— 目的就是限制单题时长，
否则一直有人聊天，题目会无限拖下去。

```
0min   出题
5min   提示 1        ← 每 hint_seconds 一条
10min  提示 2
15min  提示 3        ← 共 max_hints 条
20min  揭晓          ← 最后一条提示后再等 hint_seconds
```

界面上有实时倒计时（顶部中间）：`距第 2 条提示 04:12` / `距揭晓 00:03`。

**提前揭晓**：有人猜中核心真相（裁判判定）→ 立即揭晓。

**不设提问条数上限** —— 单人直播间也能一直玩。

### 猜中怎么判定

不靠模型自觉。**先正常裁决**（是/不是/无关/接近了），若不是「揭晓」，**再单独调一次裁判**问"这条提问是否说中了核心谜底"。

实测模型几乎不会主动吐「揭晓」（它把判猜中当成了泄露答案），但拆开单独问它就肯答。

---

## 界面

```
┌──────────────────────────────┐
│  第 3 题 · 已进行 02:14       │
│                              │
│  他走进餐厅，点了一碗海龟汤，  │   ← 上半：谜面(大字, 居中)
│  喝了一口就冲出去自杀了。      │
│  为什么？                     │
├──────────────────────────────┤
│  观众甲：他瞎了吗    【不是】  │
│  观众乙：汤有毒吗    【接近了】│   ← 下半：问答流(持续上滚)
│  观众丙：是同伴的肉吗【揭晓】  │
│                              │
│  AI 正在思考… (3)             │
├──────────────────────────────┤
│  本题已问 9 · 已答 9 · 观众 27 │
└──────────────────────────────┘
```

---

## 文件结构

```
douyin/
  danmaku.py              # 抖音抓取(只读, 不动)
  director.py             # CLI 入口 + 装配 + 三种 LLM worker + ANSWER 并发池
  story/
    config.py             # CLI/env/默认值
    state.py              # Phase/Snapshot/Action/QAResult
    parser.py             # 宽容解析器(裁决 + 谜题) —— 承重件
    engine.py             # 海龟汤状态机(纯函数, 时钟注入)
    ingest.py             # 弹幕接入(Live/Sim/Stdin)
    llm.py                # Anthropic 兼容客户端 + 出题/裁判/提示/揭晓
    server.py             # stdlib HTTP + 手写 WebSocket
  web/                    # 1080x1920 竖屏页
  tests/                  # 全离线单测
```

---

## 关键实测：用「强制工具调用」拿到结构化输出

这是本项目最重要的一个发现，直接决定了可靠性。

**问题**：`deepseek-v4.1-flash` **拒绝遵守任何文本格式约定**。先后试了 4 种规格
（单行 `【答】`、`1|是`、`编号|裁决|点评`、纯编号列表），每一次它都会：加解释、
加 markdown 粗体、分隔符在 `|` / `→` / 无 之间乱变，甚至**拒绝输出编号**。
更糟的是它会把"判猜中"当成"泄露答案"，宁可不判。

**解法**：走 **`tool_choice: {"type":"tool"}` 强制工具调用**。实测网关支持，
返回一个 **schema 校验过的** `tool_use` 块：

```json
// 请求
{"tools":[{"name":"emit_verdict","input_schema":{...,"verdict":{"enum":["是","不是","无关","接近了","揭晓"]}}}],
 "tool_choice":{"type":"tool","name":"emit_verdict"}}

// 返回 (stop_reason: "tool_use")
{"type":"tool_use","input":{"answers":[{"id":1,"verdict":"揭晓","comment":"答对了！"}]}}
```

效果对比（同一段真实流程）：

| | 文本约定 | 强制工具 |
|---|---|---|
| 解析失败率 | 一轮 7 次 | **0 次** |
| 猜中判定 | 从不触发 | 稳定触发 |

**`story/parser.py` 的宽容解析器仍然保留**，作为工具调用不可用时的回退路径
（老网关 / 别的模型）；单测用真实的模型输出做样本，覆盖了这些畸形格式。

---

## 运行测试（全部离线，不联网）

```bat
uv run tests/test_parser.py    rem 宽容解析器(用实测的真实模型输出做样本)
uv run tests/test_llm.py       rem PuzzleWriter 强制工具 + 回退解析
uv run tests/test_engine.py    rem 状态机(FakeClock 注入时钟)
uv run tests/test_ingest.py    rem 弹幕接入(走真实 protobuf)
uv run tests/test_web.py       rem 无头 Chrome 验证布局, 截图 data/preview.png
```

---

## 配置

### LLM 配置（推荐：只改一个本地文件）

第一次配置时，把示例复制成你的本地配置：

```bat
copy config\llm.example.json config\llm.local.json
```

然后以后只编辑：

`config/llm.local.json`

完整但仍然很简单：

```json
{
  "base_url": "http://127.0.0.1:8080",
  "api_key": "你的 API key",
  "default": "deepseek-v4.1-flash",

  "puzzle.story": "glm-5.3-flash",
  "puzzle.surface": "glm-5.3-flash",
  "puzzle.structure": "glm-5.3-flash",

  "timeout": 60,
  "max_tokens": 700,
  "max_retries": 3
}
```

含义：

- `base_url`：Anthropic-compatible API 地址
- `api_key`：API key
- `default`：所有未单独配置 stage 的默认模型
- `puzzle.story` 等：只覆盖指定环节；**不用把 16 个 stage 全写出来**
- `timeout / max_tokens / max_retries`：全局 LLM 调用默认预算，可省略
- 如果要用内置白名单之外的新模型，可选加：
  `"supported_models_extra": ["你的新模型名"]`

如果你只想换默认模型，甚至可以只写：

```json
{
  "base_url": "http://127.0.0.1:8080",
  "api_key": "你的 API key",
  "default": "deepseek-v4.1-flash"
}
```

常用 stage：

- `puzzle.story` / `puzzle.surface` / `puzzle.structure`：出题创作链
- `puzzle.review`：审题
- `puzzle.truth_audit`：真相一致性检查
- `qa.answer`：直播问答
- `qa.judge`：猜中判定
- `hint`：提示
- `reveal`：揭晓

`config/llm.local.json` 已加入 `.gitignore`，因为它可以包含真实 API key；仓库只提交：

`config/llm.example.json`

作为安全示例。

现有 `config/models.json` 保留为仓库默认模型策略，平时不需要改。最终覆盖优先级：

`CLI > 环境变量 > config/llm.local.json > config/models.json > 代码默认值`

所以正常直播启动仍然是：

```bat
uv run director.py --live 你的直播间ID
```

不需要先 `set AI_BASE_URL` / `set AI_API_KEY` / `set AI_MODEL`。

环境变量：`AI_BASE_URL` / `AI_API_KEY` / `AI_MODEL` / `AI_TIMEOUT` / `AI_MAX_TOKENS` / `AI_MAX_RETRIES`

常调 CLI 参数：

| 参数 | 默认 | 含义 |
|---|---|---|
| `--qa-max-inflight` | 5 | 同时在途的 AI 回答调用上限 |
| `--hint-seconds` | 300 | 提示间隔（也是最后一条提示到揭晓的间隔） |
| `--max-hints` | 3 | 给几条提示（之后再过 `--hint-seconds` 揭晓） |
| `--restate-seconds` | 120 | 多久无人发言就重述谜面（零成本） |
| `--reveal-hold` | 30 | 揭晓展示时长 |
| `--no-window` | – | 不自动开直播输出窗口 |

提问条数**不设上限**。

## 画面

- **上半**：谜面（大字居中，固定不动）
- **下半**：问答流（持续向上滚动），每行是「观众名：问题 + 裁决徽章」
  - 裁决有配色：`是`绿 / `不是`红 / `无关`灰 / `接近了`金 / `揭晓`高亮
  - 连续多条「无关」会自动折叠，只留最近 2 条（防刷屏）
  - 底部常驻一行：`发送 #你的问题 向我提问，猜中谜底我就揭晓`
- **揭晓时**：上半切换到谜底 + 倒计时

---

## 落盘

- `data/danmaku.jsonl` —— 原始弹幕（每条 `chat` 一行）
- `data/puzzle.jsonl` —— 每题的谜面 / 谜底 / 该题问答 / 是否猜中 / 揭晓文案

落盘失败会**停止引擎**（避免静默丢内容），退出码非 0。
