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

### 累计猜汤榜（跨直播持久化）

真实直播 `--live` 下，真人最终解出一题会永久追加到：

```text
data/leaderboard.jsonl
```

下次开播会自动重放这个账本，所以排行榜不会因为关程序、重启电脑或下一场直播而清零。

规则：
- 按抖音 `user_id` 累计，同名不同 UID 仍是两个人；
- 用户改昵称后，下一次获胜会更新榜上显示的昵称；
- 只记真人最终解题，AI 玩家 / 超时揭晓 / 跳题不加分；
- 前端只展示累计 Top3；
- 文件是 append-only JSONL，已被 `.gitignore` 排除，不会提交观众身份数据。

如果以后确实想**人工清空总榜**，停播后备份或删除
`data/leaderboard.jsonl`，下次启动就会从空榜开始。

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

### LLM 配置（推荐：一个本地文件管理多个接口 / 多个模型）

第一次复制安全示例：

```bat
copy config\llm.example.json config\llm.local.json
```

以后只编辑：

`config/llm.local.json`

推荐格式是“provider + model alias”两级路由。比如接口 A 有两个模型、接口 B 有两个模型：

```json
{
  "providers": {
    "A": {
      "base_url": "https://api-a.example.com",
      "api_key": "接口 A 的 key"
    },
    "B": {
      "base_url": "https://api-b.example.com",
      "api_key": "接口 B 的 key"
    }
  },

  "models": {
    "a1": {"provider": "A", "model": "model-a-1"},
    "a2": {"provider": "A", "model": "model-a-2"},
    "b1": {"provider": "B", "model": "model-b-1"},
    "b2": {"provider": "B", "model": "model-b-2"}
  },

  "default": "a1",

  "puzzle.story": "b2",
  "puzzle.surface": "b2",
  "puzzle.structure": "b2",
  "puzzle.review": "a2",
  "qa.answer": "a1",
  "qa.judge": "b1",

  "timeout": 60,
  "max_tokens": 700,
  "max_retries": 3
}
```

这里：

- `providers` 只管接口地址和 API key
- `models` 给“接口 + 真实模型名”起一个短别名，例如 `a1 / a2 / b1 / b2`
- `default` 是没单独配置 stage 时使用的 model alias
- 每个 stage 只写 alias；程序会同时切换到正确的 URL、Key 和真实模型
- **不用把 16 个 stage 全写出来**，未写的自动继承 `default`

例如上面的配置表示：

```text
puzzle.story     -> b2 -> 接口 B / model-b-2
puzzle.review    -> a2 -> 接口 A / model-a-2
qa.answer        -> a1 -> 接口 A / model-a-1
qa.judge         -> b1 -> 接口 B / model-b-1
其他 stage       -> a1 -> 接口 A / model-a-1
```

多接口模式下，`default` 和 stage 必须引用已经定义过的 alias。alias 或 provider 拼错会启动失败，不会静默回退到别的接口。

常用 stage：

- `puzzle.story` / `puzzle.surface` / `puzzle.structure`：出题创作链
- `puzzle.review`：审题
- `puzzle.truth_audit`：真相一致性检查
- `qa.answer`：直播问答
- `qa.judge`：猜中判定
- `hint`：提示
- `reveal`：揭晓

旧的单接口格式继续兼容：

```json
{
  "base_url": "http://127.0.0.1:8080",
  "api_key": "你的 API key",
  "default": "deepseek-v4.1-flash"
}
```

`config/llm.local.json` 已加入 `.gitignore`，真实 API key 不会提交 Git；仓库只提交安全示例：

`config/llm.example.json`

正常启动命令不变：

```bat
uv run director.py --live 你的直播间ID
```

启动 banner 会显示每个 stage 的最终路由，例如：

```text
puzzle.story             = b2 -> B/model-b-2
qa.answer                = a1 -> A/model-a-1
```

不会打印完整 API key。

旧环境变量 / CLI 仍保留作为高级覆盖。直接写真实模型名时走 legacy 单接口 endpoint；写 alias 时走 alias 对应 provider。

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
