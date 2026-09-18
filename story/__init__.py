"""竖屏 AI 海龟汤直播系统。

玩法: AI 出一个诡异谜题 -> 观众发 #问题 追问 -> AI 对每条提问**秒回**裁决
(是 / 不是 / 无关) -> 有人猜中核心真相 -> 揭晓谜底
-> 展示 30 秒 -> 自动出下一题。目标是长时间无人值守运行。

模块划分:
    config   配置(CLI/env/默认值)
    state    引擎数据结构(Phase/Snapshot/Action/QAResult)
    parser   宽容解析器 —— 从模型的自由文本里抽取裁决与谜题(承重件)
    engine   海龟汤状态机(纯函数, 时钟注入)
    ingest   弹幕接入(Live 真房间 / Sim 离线 / Stdin)
    llm      Anthropic 兼容客户端 + 出题/裁判/提示/揭晓
    server   stdlib HTTP + WebSocket 渲染服务
"""

__version__ = "0.2.0"
