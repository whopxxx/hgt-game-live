"""用本机无头 Chrome 验证实际布局，截图写入 data/preview.png。零新依赖。

喂的是**海龟汤真实形态**的快照(谜面 + 问答流 + 揭晓)。
"""
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _find_chrome() -> str | None:
    """找一个可用的 Chrome/Chromium。

    早先这里写死 `C:/Program Files/...`(Windows 路径), 于是 CI(Ubuntu)
    上必然找不到 —— 而且报出来的是 subprocess 的 FileNotFoundError,
    看着像测试逻辑坏了, 其实是环境假设错了。

    顺序: 环境变量 -> 各平台常见安装位置 -> PATH。
    """
    env = os.environ.get("HGT_CHROME") or os.environ.get("CHROME_PATH")
    if env and Path(env).exists():
        return env
    cands = [
        # Windows
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        # Linux (CI / 服务器)
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/snap/bin/chromium",
        # macOS
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ]
    for c in cands:
        if c and Path(c).exists():
            return c
    for name in ("google-chrome", "google-chrome-stable", "chromium",
                 "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


CHROME = _find_chrome()

_NEED_MSG = ("没找到 Chrome/Chromium —— 这个套件验证的是真实浏览器布局。\n"
             "  装一个, 或用 HGT_CHROME=/path/to/chrome 指定。")

CHECK = r'''
window.socket = null;
window.WebSocket = class { constructor() { window.socket = this; } };
// 生产值是 3s / 26px/s / 4.5s；浏览器回归只压缩时间，不改状态机。
window.__AUTO_SCROLL_TIMING__ = {
  topHoldMs: 60, bottomHoldMs: 180, speedPxPerSec: 300,
};
window.addEventListener("load", async () => {
  await document.fonts.ready;
  const errors = [];
  const check = (ok, message) => { if (!ok) errors.push(message); };
  const wait = (ms) => new Promise(resolve => setTimeout(resolve, ms));
  const waitFor = async (fn, message, timeout=1800) => {
    const end = performance.now() + timeout;
    while (!fn() && performance.now() < end) await wait(10);
    check(!!fn(), typeof message === "function" ? message() : message);
  };

  const mkQa = (n) => Array.from({length: n}, (_, i) => ({
    qid: i + 1, user_name: "观众" + i, text: "问题编号" + (i + 1) + "：他是不是瞎了",
    // 刻意避开「无关」——它会触发折叠, 干扰基础渲染断言。
    // (折叠行为有单独的测试 ⑪)
    verdict: ["是","不是","是","是"][i % 4], comment: "点评" + i, kind: "qa"
  }));
  const send = (o) => socket.onmessage({data: JSON.stringify(Object.assign({
    phase: "qa", puzzle_index: 1, puzzle: "一个男人走进餐厅，点了一碗海龟汤，喝了一口就冲出去自杀了。为什么？",
    revealed_answer: "", qa_log: [], qa_total: 0, pending_count: 0,
    hint_count: 0, hint_text: "", next_puzzle_ms: null, puzzle_elapsed_ms: 45000,
    story_index: 1, danmaku: [], stats: {questions: 0, answered: 0, solved: 0, dropped: 0, viewers_seen: 3}
  }, o))});

  try {
    // ⓪ 飘屏弹幕已移除(UI cleanup)
    //
    // 飘屏弹幕整个删掉了 —— 它的速度/轨道状态机(P1/P2 那一整套)也一并
    // 删除, 不再保留"display:none 但继续维护"的代码。
    //
    // 这三条保护的是**删除后的契约**, 而不是被删掉的功能:
    //   A. DOM 里不存在 #danmaku
    //   B. 服务端仍然下发 danmaku 字段时, 前端**明确忽略**且不报错
    //      (允许后端暂时保留该字段, 不必和这次前端删除绑在一起)
    //   C. 底部 260px 已被回收 —— #content 底边与 #stage 底边重合
    check(!document.getElementById("danmaku"),
          "A: DOM 中不应再有 #danmaku");
    // B. 服务端**仍然**下发 danmaku 时, 前端必须明确忽略, 且不得报错。
    //
    // 只断言"没有 .dm 节点"是**不够**的: 一个还在尝试渲染、只是找不到
    // 容器的实现也会通过(它 `getElementById("danmaku")` 拿到 null 就
    // 静默放弃)。所以这里改断言**数据路径**:
    //  - 页面不能抛错(errors 里不能多出东西)
    //  - 带 danmaku 的 snapshot 之后, 其它区域必须**照常工作**
    //    (说明 danmaku 字段被安全忽略, 而不是把 onState 打断了)
    // ⚠️ 不能靠"没抛错"来判断 —— `app.js` 里 onState 整个被
    //     `try { onState(...) } catch (e) {}` 包着, 任何渲染异常都被
    //     静默吞掉。所以这里改断言**副作用**: pushDanmaku 在 onState 里
    //     排在 renderStats 之后、renderDebug/layout 之前, 一个还会炸的
    //     实现会把后面的渲染**截断**。
    //     用一个"只有它才会更新"的可见信号来验证 onState 跑到了最后:
    //     layout() 会按谜面长度调 #puzzle 的字号, 而 renderStats 更新统计。
    // 关键: 要验的是 onState **跑完了**, 而不是"stats 有没有更新"。
    // `renderStats` 排在 pushDanmaku **之前**, 所以哪怕 pushDanmaku 炸了,
    // 它照样更新 —— 用 stats 当探针会得到假绿(我第一版就是这么错的)。
    //
    // pushDanmaku 之后只剩 `renderDebug` 和 `layout()`。layout() 会写入
    // 工作区分界值 `--workspace-top`(#top / #bottom / #reveal 三者共用),
    // 这是**唯一**能被外部观察、且必然发生在 pushDanmaku 之后的副作用。
    //
    // ⚠️ C2: 早先这里读 `#bottom.style.top`。C2 把分界值改成 CSS 变量
    // 单一来源后那个 inline style 不再被写, 于是探针会恒等 —— 探针失效
    // 会让 B2 变成"永远绿"的假测试。所以读变量本身。
    const content = document.getElementById("content");
    const topBefore = content.style.getPropertyValue("--workspace-top");
    // 换一道**很长**的谜面, 保证 layout() 会算出不同的 top(否则值不变,
    // 无法区分"没执行"和"执行了但结果一样")
    const longPuzzle = "这是一道很长的谜面。".repeat(12);
    send({puzzle: longPuzzle, puzzle_index: 99,
          stats: {questions: 7, answered: 5, solved: 1, dropped: 0,
                  viewers_seen: 3},
          danmaku: [{seq: 1, user_name: "甲", content: "一", is_command: false},
                    {seq: 2, user_name: "乙", content: "二", is_command: true}]});
    check(document.querySelectorAll(".dm").length === 0,
          "B1: 不应再生成 .dm 节点 (got "
          + document.querySelectorAll(".dm").length + ")");
    const topAfter = content.style.getPropertyValue("--workspace-top");
    check(topAfter && topAfter !== topBefore,
          "B2: 带 danmaku 的 snapshot 之后 onState 必须跑完 —— "
          + "layout() 是最后一步, 它没执行说明中途被截断 "
          + "(--workspace-top " + (topBefore || "(空)") + " -> "
          + (topAfter || "(空)") + ")");
    // C2: 分界值必须**同时**驱动 #bottom 与 #reveal —— 三者同一来源。
    {
      const bot = document.getElementById("bottom");
      const rev = document.getElementById("reveal");
      const bs = getComputedStyle(bot).top;
      check(bs === topAfter,
            "C2: #bottom 的 top 应等于 --workspace-top ("
            + bs + " vs " + topAfter + ")");
      const wasH = rev.classList.contains("hidden");
      rev.classList.remove("hidden");
      const rs = getComputedStyle(rev).top;
      check(rs === topAfter,
            "C2: #reveal 的 top 应等于 --workspace-top ("
            + rs + " vs " + topAfter + ")");
      if (wasH) rev.classList.add("hidden");
    }
    {
      const stage = document.getElementById("stage");
      const cr = content.getBoundingClientRect();
      const sr = stage.getBoundingClientRect();
      // 非 debug 模式下 #content 应铺满舞台到底边(允许 1px 取整误差)
      check(Math.abs(cr.bottom - sr.bottom) <= 1,
            "C: #content 底边应与 #stage 底边重合(260px 已回收) (content="
            + cr.bottom.toFixed(1) + ", stage=" + sr.bottom.toFixed(1) + ")");
    }


    // ① 谜面 + 问答流追加
    send({qa_log: mkQa(3), qa_total: 3});
    check(document.querySelectorAll(".qa-row").length === 3, "应渲染 3 行问答");
    send({qa_log: mkQa(6), qa_total: 6});
    check(document.querySelectorAll(".qa-row").length === 6, "应追加到 6 行");
    // 追加不丢旧内容(现在整窗重画, 所以按内容而非节点身份断言)
    send({qa_log: mkQa(8), qa_total: 8});
    check(document.querySelectorAll(".qa-row").length === 8, "应到 8 行");
    check(document.querySelector(".qa-row").textContent.includes("问题编号1"),
          "最旧一行内容应保留");
    // 裁决徽章
    check(document.querySelector(".verdict"), "缺少裁决徽章");
    check(document.querySelector(".qa-row .q .who").textContent.includes("观众0"),
          "缺少发言者名字");

    // AI 玩家只显示状态，token 数值不进入观众 UI。
    // Issue #43: 常驻文案是"点赞推进"语义 + 服务端快照的 ❤️ 进度。
    send({puzzle_index: 2, story_index: 2, puzzle: "AI 玩家布局测试。",
          ai_player: {questions_available: 3, questions_earned: 7,
                      questions_used: 4, likes_progress: 23,
                      likes_per_progress: 100, in_flight: false},
          qa_log: [{qid: 1, user_name: "AI玩家", text: "地点重要吗？",
                    verdict: "是", comment: "", kind: "ai_player"}],
          qa_total: 1});
    const statsText = document.getElementById("stats").textContent;
    check(statsText.includes("每100点赞，AI玩家助攻并加速本题")
          && statsText.includes("❤️ 23 / 100"),
          "应显示点赞推进文案与进度: " + statsText);
    const aiRow = document.querySelector(".qa-row.kind-ai_player");
    check(aiRow && aiRow.textContent.includes("AI玩家：地点重要吗？")
            && aiRow.textContent.includes("是"),
          "AI ask 行应完整显示: " + (aiRow && aiRow.textContent));
    const puzzleCenter = document.elementFromPoint(540, 300);
    check(puzzleCenter && (puzzleCenter.closest("#top")
          || puzzleCenter.id === "puzzle"),
          "AI 次数/问答不得遮挡谜面");

    const longSolve = "我猜他每天检查门上的位置，是为了确认有没有人进过房间。"
      .repeat(5);
    send({puzzle_index: 3, story_index: 3, puzzle: "AI solve 换行测试。",
          ai_player: {questions_available: 2},
          qa_log: [{qid: 2, user_name: "AI玩家", text: longSolve,
                    verdict: "猜中了", comment: "", kind: "ai_player"}],
          qa_total: 1});
    const solveRow = document.querySelector(".qa-row.kind-ai_player");
    const solveBox = solveRow.getBoundingClientRect();
    const aiQaRect = document.getElementById("qa").getBoundingClientRect();
    check(solveRow.textContent.includes("猜中了"), "AI solve 结果应显示‘猜中了’");
    check(solveBox.left >= aiQaRect.left - 1 && solveBox.right <= aiQaRect.right + 1,
          "AI solve 长文本不得横向溢出 QA 区");

    // ② 提示行: 只在问答流里出现一次, 不再有单独的提示条(避免重复显示)
    send({qa_log: mkQa(2).concat([{qid: -1, user_name: "提示", text: "注意汤的味道",
          verdict: "", comment: "", kind: "hint"}]), qa_total: 3, hint_count: 1,
          hint_text: "注意汤的味道"});
    check(document.querySelector(".qa-row.kind-hint"), "缺少提示行");
    check(document.getElementById("hintbar").classList.contains("hidden"),
          "提示条不应显示(提示只走问答流, 否则重复)");
    // 再推一次**同一条**提示: 不应重复上屏
    const hintRows1 = document.querySelectorAll(".qa-row.kind-hint").length;
    send({qa_log: mkQa(2).concat([{qid: -1, user_name: "提示", text: "注意汤的味道",
          verdict: "", comment: "", kind: "hint"}]), qa_total: 4, hint_count: 2,
          hint_text: "注意汤的味道"});
    check(document.querySelectorAll(".qa-row.kind-hint").length === hintRows1,
          "同一条提示不应重复上屏");
    // 换一条**不同**的提示: 应正常上屏
    send({qa_log: mkQa(2).concat([
          {qid: -1, user_name: "提示", text: "注意汤的味道", verdict: "", comment: "", kind: "hint"},
          {qid: -2, user_name: "提示", text: "他以前也喝过一次", verdict: "", comment: "", kind: "hint"}
        ]), qa_total: 5, hint_count: 2, hint_text: "他以前也喝过一次"});
    check(document.querySelectorAll(".qa-row.kind-hint").length === hintRows1 + 1,
          "不同的新提示应上屏");

    // ③ 思考中
    send({qa_log: mkQa(2), qa_total: 2, pending_count: 4});
    check(!document.getElementById("thinking").classList.contains("hidden"),
          "排队时应显示'思考中'");
    send({qa_log: mkQa(2), qa_total: 2, pending_count: 0});
    check(document.getElementById("thinking").classList.contains("hidden"),
          "无排队时应隐藏'思考中'");

    // ④ 揭晓工作区(U1): 0..core_focus 只显示核心答案
    //
    // ⚠️ 这条 payload **故意带上 reveal_contributors** —— 否则贡献链
    // 无论实现如何都是隐藏的(空列表), 断言"核心阶段不显示贡献链"
    // 就永远不会红(假测试)。
    send({phase: "revealed", qa_log: mkQa(4), qa_total: 4,
          revealed_answer: "多年前他遭遇海难，同伴给他喝的其实是同伴自己的肉。",
          revealed_core_answer: "同伴给他喝的是同伴自己的肉。",
          revealed_full_answer: "多年前他遭遇海难，同伴给他喝的其实是同伴自己的肉，他知情后崩溃。",
          reveal_detail_visible: false,
          reveal_contributors: [
            {qid: 1, user_name: "甲", verdict: "是", is_final: true}],
          solved: true, solved_by: "观众戊", next_puzzle_ms: 25000});
    check(!document.getElementById("reveal").classList.contains("hidden"),
          "揭晓层未显示");
    check(document.getElementById("reveal-core").textContent.includes("同伴自己的肉"),
          "核心答案缺失");
    check(document.getElementById("reveal-next").textContent.includes("25"),
          "下一题倒计时缺失");
    // U1: 0..core_focus 期间完整解释必须**隐藏**(让核心答案独占)。
    check(document.getElementById("reveal-body").classList.contains("hidden"),
          "核心答案阶段完整解释应隐藏");
    check(document.getElementById("reveal-who").textContent.includes("观众戊"),
          "solved 时应显示谁补齐最后线索");

    // ④.2 U1: 揭晓时谜面**保持可见**(它已不在 #top 内, 不再叠字)
    check(!document.getElementById("puzzle").classList.contains("hidden"),
          "U1: 揭晓时谜面应保持可见, 供观众对照");
    // U1: 下半部问答工作区整个让给答案(只视觉隐藏, DOM 保留)。
    check(document.getElementById("bottom").classList.contains("hidden"),
          "U1: 揭晓时下半部问答区应让位");
    // ---- C2: 揭晓工作区**真实几何** ----
    //
    // 上面两条(U1)只证明谜面没有 `.hidden`、底部分区被关了 —— 它们
    // **证明不了谜面真的看得见**。C2 之前 #reveal 是 `inset:0` + 不透明
    // 背景 + z-index:20, 于是它从 y=0 起把整屏刷掉, 谜面虽然"没被隐藏",
    // 但被盖在下面 —— 屏幕上看不见, 测试却全绿。这就是假绿。
    //
    // 现在断言**几何**:
    //   ① 揭晓工作区顶边 >= 谜面区底边(两者不重叠)
    //   ② 揭晓工作区与 #bottom 是**同一个矩形**(共用工作区分界值)
    //   ③ 谜面**中心点**的最上层元素属于谜面区域, 而不是揭晓层
    //       —— 只有这一条能真正抓住"被不透明层盖住"。
    {
      const r = (id) => document.getElementById(id).getBoundingClientRect();
      // ⚠️ `getBoundingClientRect()` 返回的是**缩放后**的 CSS 像素:
      //    fit() 把 #stage transform: scale(min(vw/1080, vh/1920))。
      //    headless 视口是 1064x1825(不是 1080x1920), scale ≈ 0.9505,
      //    所以直接比两个元素的 rect 会混进缩放差异。
      //
      // ⚠️⚠️ 还要**关掉动画**: headless 的 --virtual-time-budget 会把动画
      //    时钟冻在 t=0, `#reveal` 的入场动画(早先是 translateY(16px))
      //    会永久停在 from 帧 —— 量出来的 top 恒偏 16px, 断言随机红。
      //    生产里那段位移也真的会把面板底边推出 #stage(overflow:hidden)
      //    被裁掉, 所以 C2 把动画改成纯 opacity 淡入; 这里再显式 `none`
      //    一次, 让断言与"动画播到哪一帧"彻底无关。
      const revEl = document.getElementById("reveal");
      const savedAnim = revEl.style.animation;
      revEl.style.animation = "none";
      void revEl.getBoundingClientRect();      // 强制回流后再量
      const scale = document.getElementById("stage")
        .getBoundingClientRect().width / 1080;
      check(scale > 0 && scale < 1.01, "C2: scale 应在 (0,1]: " + scale);
      const rl = (id) => {
        const b = r(id);
        return {top: b.top / scale, bottom: b.bottom / scale,
                left: b.left / scale, right: b.right / scale,
                height: b.height / scale};
      };
      const topR = rl("top"), revR = rl("reveal");
      const bot = document.getElementById("bottom");
      check(revR.top >= topR.bottom - 1,
            "C2: 揭晓工作区不得盖住谜面区 (reveal.top="
            + revR.top.toFixed(1) + " top.bottom=" + topR.bottom.toFixed(1)
            + " @scale " + scale.toFixed(4) + ")");
      check(revR.height > 0 && revR.bottom <= 1920 + 1,
            "C2: 揭晓工作区应在舞台内 (top=" + revR.top.toFixed(1)
            + " bottom=" + revR.bottom.toFixed(1) + ")");
      // ② 与 #bottom 同一矩形 —— 先把 #bottom 临时显示出来量(它此刻
      //    是 display:none, 量不到)。量完立刻还原, 不影响后续断言。
      const wasHidden = bot.classList.contains("hidden");
      bot.classList.remove("hidden");
      const botR = rl("bottom");
      check(Math.abs(revR.top - botR.top) <= 1
            && Math.abs(revR.bottom - botR.bottom) <= 1
            && Math.abs(revR.left - botR.left) <= 1
            && Math.abs(revR.right - botR.right) <= 1,
            "C2: 揭晓工作区必须与 #bottom 是同一矩形 (reveal="
            + revR.top.toFixed(1) + ".." + revR.bottom.toFixed(1) + " bottom="
            + botR.top.toFixed(1) + ".." + botR.bottom.toFixed(1) + ")");
      if (wasHidden) bot.classList.add("hidden");
      // ③ 谜面中心点的最上层元素必须属于谜面区域。
      //
      // **这一条才是真正抓住"被不透明层盖住"的断言** —— 前两条只
      // 证明"两个盒子没重叠", 而 `inset:0` 那种实现两个盒子也不会
      // "重叠", 真正的问题是 hit test 命中了谁。
      // `elementFromPoint` 吃视口坐标, 所以用未缩放的 r()。
      //
      // ⚠️ 必须在**揭晓层可见**时测 —— `display:none` 的元素不参与
      //    hit test, 那样"谜面没被盖住"会无条件通过, 又是假绿。
      check(!revEl.classList.contains("hidden"),
            "C2: 揭晓层此刻应可见, 否则这条 hit test 是假绿");
      const px = (r("top").left + r("top").right) / 2;
      const py = (r("top").top + r("top").bottom) / 2;
      const hit = document.elementFromPoint(px, py);
      const topEl = document.getElementById("top");
      const inTop = !!(hit && (hit === topEl || topEl.contains(hit)));
      check(inTop,
            "C2: 谜面中心 (" + px.toFixed(0) + "," + py.toFixed(0)
            + ") 的最上层元素应属于谜面区, 实际 <"
            + (hit ? hit.id || hit.tagName : "null") + ">");
      // 对称地: 揭晓工作区中心点的最上层元素应属于揭晓层。
      const rv = r("reveal");
      const hx = (rv.left + rv.right) / 2;
      const hy = (rv.top + rv.bottom) / 2;
      const hit2 = document.elementFromPoint(hx, hy);
      check(!!(hit2 && (hit2 === revEl || revEl.contains(hit2))),
            "C2: 揭晓工作区中心的最上层元素应属于揭晓层, 实际 <"
            + (hit2 ? hit2.id || hit2.tagName : "null") + ">");
      revEl.style.animation = savedAnim;
    }
    const coreFs = parseFloat(
      getComputedStyle(document.getElementById("reveal-core")).fontSize);
    check(coreFs >= 58, "核心答案字号应 >= 58px, 实际 " + coreFs);
    // U1: 核心答案阶段不得显示共同解谜(它会挤占核心答案的视觉重量)。
    check(document.getElementById("reveal-contrib").classList.contains("hidden"),
          "核心答案阶段不应显示共同解谜");

    // ④.3 U2: explanation 阶段 -> 追加完整解释, **共同解谜仍隐藏**
    //
    // ⚠️ U2 把 U1 的"细节阶段"一分为二: 15-45s 只给完整解释,
    // 45-60s 才轮到共同解谜。它们在时间上错开, 不再同屏争空间 ——
    // 那正是"完整解释被压成一条矮滚动框"的病根。
    send({phase: "revealed", qa_log: mkQa(4), qa_total: 4,
          revealed_answer: "多年前他遭遇海难，同伴给他喝的其实是同伴自己的肉。",
          revealed_core_answer: "同伴给他喝的是同伴自己的肉。",
          revealed_full_answer: "多年前他遭遇海难，同伴给他喝的其实是同伴自己的肉，他知情后崩溃。",
          reveal_detail_visible: true, reveal_stage: "explanation",
          solved: true, solved_by: "观众戊", next_puzzle_ms: 10000,
          reveal_contributors: [{user_name: "甲", verdict: "是", is_final: true}]});
    check(!document.getElementById("reveal-body").classList.contains("hidden"),
          "explanation 阶段完整解释应可见");
    check(document.getElementById("reveal-body").textContent.includes("他知情后崩溃"),
          "完整解释内容缺失");
    check(!document.getElementById("reveal-core").textContent.includes("他知情后崩溃"),
          "核心答案不应混入完整解释");
    check(parseFloat(
      getComputedStyle(document.getElementById("reveal-body")).fontSize) >= 34,
      "完整解释字号应 >= 34px");
    check(document.getElementById("reveal-contrib").classList.contains("hidden"),
          "**explanation 阶段共同解谜必须隐藏**(把空间让给完整解释)");
    // U2: 无重叠 —— 核心答案底 <= 解释顶 <= 倒计时顶
    {
      const g = (id) => document.getElementById(id).getBoundingClientRect();
      const kr = g("reveal-core"), br = g("reveal-body");
      const nr = g("reveal-next");
      check(kr.bottom <= br.top + 1,
            "核心答案不得压到解释上 (" + kr.bottom.toFixed(1)
            + " > " + br.top.toFixed(1) + ")");
      check(br.bottom <= nr.top + 1,
            "解释不得压到倒计时上 (" + br.bottom.toFixed(1)
            + " > " + nr.top.toFixed(1) + ")");
      // **U2 的核心**: 正常长度的完整解释必须不用滚动就完整可见。
      const body = document.getElementById("reveal-body");
      check(body.scrollHeight <= body.clientHeight + 1,
            "U2: explanation 阶段的解释不得依赖滚动 (scrollH="
            + body.scrollHeight + " clientH=" + body.clientHeight + ")");
    }

    // ⑤ 换题: 谜面/问答流被清空
    send({phase: "qa", puzzle_index: 2, story_index: 2,
          puzzle: "一个女人每天给丈夫做同样的汤，丈夫却死了。为什么？",
          revealed_answer: "", qa_log: [], qa_total: 0});
    check(document.getElementById("puzzle").textContent.includes("女人"),
          "新谜面未更新");
    check(document.querySelectorAll(".qa-row").length === 0, "换题后问答流应清空");
    check(document.getElementById("reveal").classList.contains("hidden"),
          "换题后揭晓层应隐藏");
    check(!document.getElementById("puzzle").classList.contains("hidden"),
          "换题后谜面应重新显示");
    check(document.getElementById("puzzle-index").textContent.includes("2"),
          "题号未更新");
    // U1: 换题后下半部问答区必须恢复(否则整场直播只剩谜面)。
    check(!document.getElementById("bottom").classList.contains("hidden"),
          "U1: 换题后问答区应恢复显示");
    // U1: 上一题的贡献人名不得残留。
    check(document.getElementById("reveal-contrib-list").children.length === 0,
          "U1: 换题后不应残留上一题的贡献链");
    check(document.getElementById("reveal-core").textContent === "",
          "U1: 换题后核心答案应清空");

    // ⑥ 谜面完整显示(不截断)
    const long = "他每天都要数一遍楼梯，从一楼数到顶楼。有一天他数到一半就不数了，第二天人们发现他死在了楼梯间。为什么？";
    send({puzzle_index: 3, story_index: 3, puzzle: long, qa_log: [], qa_total: 0});
    check(document.getElementById("puzzle").textContent === long, "谜面被截断");

    // ⑦ 调试面板宽度切换
    for (const debug of [false, true]) {
      document.getElementById("stage").classList.toggle("debug-on", debug);
      document.getElementById("content").style.transition = "none";
      const c = document.getElementById("content").getBoundingClientRect();
      const st = document.getElementById("stage").getBoundingClientRect();
      check(c.left >= st.left - 1 && c.right <= st.right + 1, "内容溢出舞台 debug=" + debug);
    }
    document.getElementById("stage").classList.remove("debug-on");

    // ⑧ 长问答流可滚
    send({puzzle_index: 4, story_index: 4, puzzle: "测试滚动用的谜面。",
          qa_log: mkQa(40), qa_total: 40});
    const qaBox = document.getElementById("qa");
    check(qaBox.scrollHeight > qaBox.clientHeight,
          "40 条问答应溢出可滚 (scrollH=" + qaBox.scrollHeight +
          " clientH=" + qaBox.clientHeight + ")");

    // ⑨ DOM 行数封顶: 远超上限也不卡(每行必须能不断更新)
    //   连续推 300 条, 断言 DOM 行数被压在上限内, 且最后一条确实上屏
    for (let n = 41; n <= 300; n += 20) {
      const win = [];
      for (let i = Math.max(1, n - 39); i <= n; i++) {
        win.push({qid: i, user_name: "观众" + i, text: "问题" + i,
                  verdict: "是", comment: "", kind: "qa"});
      }
      send({puzzle_index: 4, story_index: 4, puzzle: "测试。",
            qa_log: win, qa_total: n});
    }
    // 再推最后一窗口, 保证末尾正好是 300
    const finalWin = [];
    for (let i = 261; i <= 300; i++) {
      finalWin.push({qid: i, user_name: "观众" + i, text: "问题" + i,
                     verdict: "是", comment: "", kind: "qa"});
    }
    send({puzzle_index: 4, story_index: 4, puzzle: "测试。",
          qa_log: finalWin, qa_total: 300});
    const rows = document.querySelectorAll("#qa-body .qa-row");
    check(rows.length <= 60, "DOM 行数应封顶(<=60), 实际 " + rows.length);
    check(rows.length > 0, "封顶后仍应有行");
    const lastRow = document.querySelector("#qa-body .qa-row:last-child");
    check(lastRow && lastRow.textContent.includes("问题300"),
          "最新一条必须上屏: " + (lastRow && lastRow.textContent));

    // ⑩ 常驻互动提示: **任何阶段都显示**, 只是文案不同。
    //    (Q11 之前非 QA 阶段是整个隐藏的 —— 那正是"出题 30-45s 里
    //     观众既没指引、打字也没反馈, 看起来像卡死"的一环)
    send({phase: "qa", puzzle_index: 5, story_index: 5, puzzle: "新谜面。",
          qa_log: [], qa_total: 0, hint_text: "", revealed_answer: ""});
    const prompt = document.getElementById("prompt");
    check(!prompt.classList.contains("hidden"), "QA 阶段应显示互动提示");
    check(prompt.textContent.includes("#你的问题"),
          "互动提示应说明发送格式: " + prompt.textContent);
    // 出题阶段: 仍可见, 文案变成"正在出题"
    send({phase: "setting", puzzle_index: 5, story_index: 5,
          qa_log: [], qa_total: 0});
    check(!prompt.classList.contains("hidden"), "**出题阶段提示条不应隐藏**");
    check(prompt.textContent.includes("出题"),
          "出题阶段应说明正在出题: " + prompt.textContent);
    // 揭晓阶段: 仍可见, 文案换成换题类提示
    send({phase: "revealed", puzzle_index: 5, story_index: 5,
          revealed_answer: "谜底。", qa_log: [], qa_total: 0});
    check(!prompt.classList.contains("hidden"), "**揭晓阶段提示条不应隐藏**");
    check(prompt.textContent.includes("新谜题"),
          "揭晓阶段应说明即将换题: " + prompt.textContent);
    // 系统行(非 QA 阶段 #问题 的反馈)要能上屏。
    // 文案用 engine._ACK_BY_PHASE[SETTING] 的当前取值: 系统固定 copy 用汤面/汤底。
    send({phase: "setting", puzzle_index: 6, story_index: 6, puzzle: "",
          qa_log: [{qid: -1, user_name: "系统", text: "正在准备新题，汤面出现后再发 #问题。",
                    verdict: "", comment: "", kind: "system"}],
          qa_total: 0});
    const sysRows = [...document.querySelectorAll(".qa-row.kind-system")];
    check(sysRows.length === 1, "系统行应渲染 1 条, 实际 " + sysRows.length);
    check(sysRows.length && sysRows[0].textContent.includes("正在准备新题"),
          "系统行内容: " + (sysRows[0] && sysRows[0].textContent));
    check(sysRows.length && sysRows[0].textContent.includes("汤面")
          && !sysRows[0].textContent.includes("谜面"),
          "系统固定文案应用汤面而非谜面: " + (sysRows[0] && sysRows[0].textContent));
    check(sysRows.length && !sysRows[0].textContent.includes("系统："),
          "系统行**不该**带观众名前缀: " + (sysRows[0] && sysRows[0].textContent));

    // ⑩.5 单条「无关」不得把上一条重复画出来
    //     (实测 bug: 折叠起点算错, [是, 无关] 会把"是"那行画两遍)
    send({phase: "qa", puzzle_index: 7, story_index: 7, puzzle: "重复测试。",
          qa_log: [
            {qid: 1, user_name: "甲", text: "唯一问题A", verdict: "是", comment: "", kind: "qa"},
            {qid: 2, user_name: "乙", text: "唯一问题B", verdict: "无关", comment: "", kind: "qa"},
          ], qa_total: 2});
    const dupRows = [...document.querySelectorAll(".qa-row")].map(r => r.textContent);
    check(dupRows.length === 2, "单条无关应显示 2 行, 实际 " + dupRows.length);
    check(dupRows.filter(t => t.includes("唯一问题A")).length === 1,
          "'唯一问题A' 不应重复出现: " + JSON.stringify(dupRows));
    check(dupRows.filter(t => t.includes("唯一问题B")).length === 1,
          "'唯一问题B' 不应重复出现: " + JSON.stringify(dupRows));

    // ⑪ 连续「无关」折叠: 只留最近 2 条 + 一行"已折叠"
    const mkIrr = (from, to) => {
      const a = [];
      for (let i = from; i <= to; i++) {
        a.push({qid: i, user_name: "观众" + i, text: "无关问题" + i,
                verdict: "无关", comment: "", kind: "qa"});
      }
      return a;
    };
    // 6 条连续无关 -> 折叠 4 条, 显示 2 条 + 1 行折叠提示
    send({phase: "qa", puzzle_index: 6, story_index: 6, puzzle: "折叠测试。",
          qa_log: mkIrr(1, 6), qa_total: 6});
    const irrRows = [...document.querySelectorAll(".qa-row")];
    const fold = document.querySelector(".qa-row.kind-fold");
    check(fold, "连续无关应出现折叠行");
    check(fold && fold.textContent.includes("4"),
          "折叠行应标出折叠了几条: " + (fold && fold.textContent));
    check(irrRows.length === 3, "6 条无关应显示为 2 条 + 1 折叠行, 实际 " + irrRows.length);
    // 只留最近两条(无关问题5 / 无关问题6)
    const shownTexts = irrRows.map(r => r.textContent).join("|");
    check(!shownTexts.includes("无关问题1：") && !shownTexts.includes("无关问题2："),
          "被折叠的旧无关不应出现在列表里: " + shownTexts);
    check(shownTexts.includes("无关问题6"), "最近的无关应保留");
    // 无关被打断时, 两段各自折叠
    const mixed = mkIrr(7, 9).concat(
      [{qid: 10, user_name: "甲", text: "关键问题", verdict: "是", comment: "", kind: "qa"}])
      .concat(mkIrr(11, 13));
    send({phase: "qa", puzzle_index: 6, story_index: 6, puzzle: "折叠测试。",
          qa_log: mixed, qa_total: 13});
    check(document.querySelectorAll(".qa-row.kind-fold").length === 2,
          "两段无关应各自折叠成 2 行, 实际 "
          + document.querySelectorAll(".qa-row.kind-fold").length);
    check(document.querySelector(".qa-row:not(.kind-fold) .q")
          || document.body.textContent.includes("关键问题"),
          "关键的'是'问答必须保留");

    // ⑫ 揭晓贡献链: "这题大家是怎么一起推出来的"
    //
    // 服务端只下发 qid/user_name/text/verdict/is_final —— 内部 fact ID
    // 在服务端就被筛掉了。这里验的是**渲染契约**。
    const mkContrib = (final) => ([
      {qid: 1, user_name: "甲", text: "这是测试飞行吗？", verdict: "是", is_final: false},
      {qid: 2, user_name: "乙", text: "复飞就是考试项目？", verdict: "是", is_final: final},
    ]);
    send({phase: "revealed", puzzle_index: 20, story_index: 20,
          revealed_answer: "这是一次预设的测试飞行，复飞本身就是考核项目。",
          revealed_core_answer: "复飞本身就是考核项目。",
          revealed_full_answer: "这是一次预设的测试飞行，复飞本身就是考核项目。",
          reveal_detail_visible: true,
          // U2: 共同解谜只在 contribution 阶段显示(它要跟完整解释
          // 错开时间, 否则两块在同一屏争空间 —— 那正是要修的病)。
          reveal_stage: "contribution",
          solved: true, solved_by: "乙",
          reveal_contributors: mkContrib(true), next_puzzle_ms: 20000});
    const cb = document.getElementById("reveal-contrib");
    check(!cb.classList.contains("hidden"), "有贡献链时应显示该块");
    check(document.getElementById("reveal-contrib-title").textContent
          === "共同猜汤",
          "solved=true 标题应为'共同猜汤', 实际 "
          + document.getElementById("reveal-contrib-title").textContent);
    const cRows = [...document.querySelectorAll(".reveal-contrib-row")];
    check(cRows.length === 2, "应渲染 2 条贡献, 实际 " + cRows.length);
    check(cRows[0].textContent.includes("甲")
          && cRows[0].textContent.includes("这是测试飞行吗"),
          "第一条应含甲与提问: " + (cRows[0] && cRows[0].textContent));
    check(cRows[1].textContent.includes("乙")
          && cRows[1].textContent.includes("复飞就是考试项目"),
          "第二条应含乙与提问: " + (cRows[1] && cRows[1].textContent));
    check(cRows[1].textContent.includes("最后线索"),
          "is_final 那条应带'最后线索': " + cRows[1].textContent);
    check(!cRows[0].textContent.includes("最后线索"),
          "非 final 那条不该带'最后线索': " + cRows[0].textContent);
    check(cRows[0].textContent.includes("是"),
          "应显示裁决");

    // ⑫.2 未解开: 标题变"大家已经推到这里", 且没有 is_final
    send({phase: "revealed", puzzle_index: 20, story_index: 20,
          revealed_answer: "这是一次预设的测试飞行，复飞本身就是考核项目。",
          revealed_core_answer: "复飞本身就是考核项目。",
          revealed_full_answer: "这是一次预设的测试飞行，复飞本身就是考核项目。",
          reveal_detail_visible: true,
          reveal_stage: "contribution",
          solved: false,
          reveal_contributors: mkContrib(false), next_puzzle_ms: 20000});
    check(document.getElementById("reveal-contrib-title").textContent
          === "大家已经推到这里",
          "solved=false 标题应为'大家已经推到这里', 实际 "
          + document.getElementById("reveal-contrib-title").textContent);
    const cRows2 = [...document.querySelectorAll(".reveal-contrib-row")];
    check(cRows2.every(r => !r.textContent.includes("最后线索")),
          "未解开时不该有'最后线索'");

    // ⑫.3 空贡献链 -> 整块隐藏
    send({phase: "revealed", puzzle_index: 20, story_index: 20,
          revealed_answer: "谜底。", revealed_core_answer: "核心。",
          revealed_full_answer: "谜底。", reveal_detail_visible: true,
          reveal_stage: "contribution",
          solved: false,
          reveal_contributors: [], next_puzzle_ms: 20000});
    check(cb.classList.contains("hidden"),
          "无贡献时应隐藏整块");
    check(document.getElementById("reveal-contrib-list").children.length === 0,
          "隐藏时列表应清空");

    // ⑫.4 换题必须擦掉上一题的名字(残留是最难发现的那类 bug)
    send({phase: "revealed", puzzle_index: 20, story_index: 20,
          revealed_answer: "谜底。", revealed_core_answer: "核心。",
          revealed_full_answer: "谜底。", reveal_detail_visible: true,
          reveal_stage: "contribution",
          solved: true,
          reveal_contributors: mkContrib(true), next_puzzle_ms: 20000});
    check(document.getElementById("reveal-contrib-list").children.length === 2,
          "先确保上一题贡献链在");
    send({phase: "qa", puzzle_index: 21, story_index: 21,
          puzzle: "新的一道题。", revealed_answer: "", qa_log: [], qa_total: 0});
    check(document.getElementById("reveal-contrib-list").children.length === 0,
          "换题后贡献链列表必须清空(不能残留上一题名字)");
    check(document.getElementById("reveal-contrib-title").textContent === "",
          "换题后标题也必须清空");

    // ⑬ U2: 长文本在**各自阶段**都不得压到别的块上
    //
    // ⚠️ U2 改写了这条的形态。U1 时"正文 + 贡献链同时可见"是要测的
    // 场景; U2 之后它们**在时间上错开**(explanation 只有正文,
    // contribution 只有贡献链), 所以"正文压到贡献链上"这个组合结构上
    // 不可能出现 —— 再断言它只会得到一条**永远真**的假测试
    // (contrib.top=0 时它当然不重叠)。
    //
    // 真正要守的不变量变成了: 每个阶段里, 该阶段可见的块必须按顺序
    // 排列、互不重叠、且都落在面板内。下面分两个阶段各测一遍。
    const longReveal = "这是一段刻意写得很长的谜底，用来把揭晓层的可用高度"
      + "压到很紧，从而检验字号自适应与其它块的布局是否会互相覆盖。"
      + "再补一些字，确保它必然超过一屏的容量，逼迫 fitReveal 真正去缩字号。"
      + "继续补一些字，继续补一些字，继续补一些字，继续补一些字。"
      + "仍然继续补字，仍然继续补字，仍然继续补字，仍然继续补字。";
    const boxes = () => ({
      core: document.getElementById("reveal-core").getBoundingClientRect(),
      body: document.getElementById("reveal-body").getBoundingClientRect(),
      contrib: document.getElementById("reveal-contrib").getBoundingClientRect(),
      next: document.getElementById("reveal-next").getBoundingClientRect(),
      panel: document.getElementById("reveal").getBoundingClientRect(),
    });

    // ---- explanation 阶段: 核心 + 正文 + 倒计时, 无贡献链 ----
    send({phase: "revealed", puzzle_index: 22, story_index: 22,
          revealed_answer: longReveal,
          revealed_core_answer: "核心答案一句。",
          revealed_full_answer: longReveal, reveal_detail_visible: true,
          reveal_stage: "explanation",
          solved: true, solved_by: "乙",
          reveal_contributors: mkContrib(true), next_puzzle_ms: 18000});
    {
      const b = boxes();
      check(b.contrib.height === 0,
            "U2-⑬: explanation 阶段贡献链应**不可见** (h="
            + b.contrib.height.toFixed(1) + ")");
      check(b.body.height > 0, "U2-⑬: 正文应有高度: " + b.body.height);
      check(b.core.bottom <= b.body.top + 1,
            "U2-⑬: 核心答案不得压到正文上");
      check(b.body.bottom <= b.next.top + 1,
            "U2-⑬: 正文不得压到倒计时上 (body.bottom="
            + b.body.bottom.toFixed(1) + " next.top=" + b.next.top.toFixed(1)
            + ")");
      check(b.core.top >= b.panel.top - 1 && b.next.bottom <= b.panel.bottom + 1,
            "U2-⑬: 核心/正文/倒计时都应落在揭晓面板内");
      const kfs = parseFloat(getComputedStyle(
        document.getElementById("reveal-core")).fontSize);
      check(kfs >= 58, "U2-⑬: 核心答案字号不得低于 58px: " + kfs);
      const fs = parseFloat(getComputedStyle(
        document.getElementById("reveal-body")).fontSize);
      check(fs >= 34, "U2-⑬: 完整解释字号不应缩到 34px 以下: " + fs);
    }

    // ---- contribution 阶段: 核心 + 贡献链 + 倒计时, 无正文 ----
    send({phase: "revealed", puzzle_index: 22, story_index: 22,
          revealed_answer: longReveal,
          revealed_core_answer: "核心答案一句。",
          revealed_full_answer: longReveal, reveal_detail_visible: true,
          reveal_stage: "contribution",
          solved: true, solved_by: "乙",
          reveal_contributors: mkContrib(true), next_puzzle_ms: 18000});
    {
      const b = boxes();
      check(b.body.height === 0,
            "U2-⑬: contribution 阶段正文应**不可见** (h="
            + b.body.height.toFixed(1) + ")");
      check(b.contrib.height > 0,
            "U2-⑬: 贡献链应可见且有高度: " + b.contrib.height);
      check(b.core.bottom <= b.contrib.top + 1,
            "U2-⑬: 核心答案不得压到贡献链上");
      check(b.contrib.bottom <= b.next.top + 1,
            "U2-⑬: 贡献链不得压到倒计时上");
      check(b.core.top >= b.panel.top - 1
            && b.next.bottom <= b.panel.bottom + 1,
            "U2-⑬: 核心/贡献链/倒计时都应落在揭晓面板内");
    }

    // ⑭ 贡献链绝不做 HTML 拼接
    //
    // 用户名与提问都来自观众。贡献链是**新增的**一处把观众字符串放上
    // 大屏的地方, 必须和问答流一样走 textContent —— 否则一个叫
    // `<img onerror=...>` 的观众就能在公屏上执行脚本。
    send({phase: "revealed", puzzle_index: 23, story_index: 23,
          revealed_answer: "谜底。", solved: true,
          revealed_core_answer: "核心。",
          revealed_full_answer: "谜底。", reveal_detail_visible: true,
          reveal_stage: "contribution",
          reveal_contributors: [
            {qid: 1, user_name: "<b>坏名字</b>",
             text: "<img src=x onerror=\"window.__xss=1\">",
             verdict: "是", is_final: true},
          ], next_puzzle_ms: 20000});
    {
      const list = document.getElementById("reveal-contrib-list");
      check(list.querySelector("b") === null,
            "用户名里的 <b> 不该被解析成元素");
      check(list.querySelector("img") === null,
            "提问里的 <img> 不该被解析成元素");
      check(window.__xss === undefined,
            "注入的 onerror 不该被执行");
      check(list.textContent.includes("<b>坏名字</b>"),
            "标签应原样作为文本显示: " + list.textContent);
    }

    // ⑯ U2: 60 秒揭晓三阶段(core / explanation / contribution)
    //
    // 修的病: 实播里核心答案 + 完整解释 + 共同解谜**同时**上屏, 下半屏
    // 三块互相争空间, fitReveal 只能一路缩字号, 完整解释被压成一条矮
    // 滚动框。而**观众没有鼠标去滚直播源** —— 直播画面里的关键内容不能
    // 依赖滚动才能看见。
    //
    // 冻结的不变量(逐阶段):
    //   core          谜面可见 / core 可见 / 解释隐藏 / 贡献隐藏 / core>=58
    //   explanation   谜面可见 / core 可见 / 解释可见 / 贡献隐藏 / 无需滚动
    //   contribution  谜面可见 / core 可见 / 解释隐藏 / 贡献可见 / final 可见
    const coreTxt = "他一直睡在原房门的正前方。";
    // 正常质量政策允许范围内的完整解释(几行, 不该需要滚动)
    const fullTxt = "衣柜是后来封住那扇门的隔板, 他把床摆在了原来门的位置, "
      + "所以每晚其实都睡在门口, 门外走廊的声响让他不敢关灯。";
    const u2Send = (stage, extra) => send(Object.assign({
      phase: "revealed", puzzle_index: 30, story_index: 30,
      puzzle: "他每晚都不敢关灯, 却一直睡在衣柜前面。为什么?",
      revealed_answer: fullTxt, revealed_core_answer: coreTxt,
      revealed_full_answer: fullTxt, reveal_stage: stage,
      reveal_detail_visible: stage !== "core",
      solved: true, solved_by: "乙",
      reveal_contributors: mkContrib(true), next_puzzle_ms: 15000,
    }, extra || {}));
    const vis = (id) => {
      const e = document.getElementById(id);
      if (!e || e.classList.contains("hidden")) return false;
      const r = e.getBoundingClientRect();
      return r.width > 0 && r.height > 0;
    };

    // ---- U2-A: 0-15s 只看核心答案 ----
    u2Send("core");
    check(vis("puzzle"), "U2-A: 谜面应可见");
    check(vis("reveal-core"), "U2-A: 核心答案应可见");
    check(!vis("reveal-body"), "U2-A: 完整解释应隐藏");
    check(!vis("reveal-contrib"), "U2-A: 共同解谜应隐藏");
    {
      const kfs = parseFloat(getComputedStyle(
        document.getElementById("reveal-core")).fontSize);
      check(kfs >= 58, "U2-A: 核心答案字号 >= 58, 实际 " + kfs);
      const kr = document.getElementById("reveal-core").getBoundingClientRect();
      check(kr.top >= -1 && kr.bottom <= 1921,
            "U2-A: 核心答案不得溢出舞台 (" + kr.top.toFixed(1) + ".."
            + kr.bottom.toFixed(1) + ")");
      check(document.getElementById("reveal-next").textContent.includes("秒后"),
            "U2-A: 倒计时应可见");
    }

    // ---- U2-B: 15-45s 核心 + 完整解释, 共同解谜隐藏 ----
    u2Send("explanation");
    check(vis("puzzle"), "U2-B: 谜面应可见");
    check(vis("reveal-core"), "U2-B: 核心答案应可见");
    check(vis("reveal-body"), "U2-B: 完整解释应可见");
    check(!vis("reveal-contrib"), "U2-B: 共同解谜应隐藏(让位给解释)");
    {
      const body = document.getElementById("reveal-body");
      const br = body.getBoundingClientRect();
      const kr = document.getElementById("reveal-core").getBoundingClientRect();
      const panel = document.getElementById("reveal").getBoundingClientRect();
      check(kr.bottom <= br.top + 1,
            "U2-B: 核心答案不得压到解释上");
      check(br.bottom <= panel.bottom + 1,
            "U2-B: 解释底边不得超出揭晓面板 (body=" + br.bottom.toFixed(1)
            + " panel=" + panel.bottom.toFixed(1) + ")");
      // **U2-C 的核心断言**: 正常长度的 answer 必须**不用滚动**就完整可见。
      check(br.height > 0, "U2-B: 解释应有高度");
      check(body.scrollHeight <= body.clientHeight + 1,
            "U2-C: 正常长度解释不得依赖滚动 (scrollH=" + body.scrollHeight
            + " clientH=" + body.clientHeight + ")");
      const fs = parseFloat(getComputedStyle(body).fontSize);
      check(fs >= 34, "U2-C: 解释字号不得低于 34, 实际 " + fs);
    }

    // ---- U2-C2: 质量政策允许的**上限**长度也必须不用滚动 ----
    //
    // 上面 U2-C 用的是"一段正常的解释"。这一条把长度推到 v8 允许的
    // 上限附近(多句、多行), 确认 explanation 阶段仍然不依赖滚动。
    //
    // ⚠️ 为什么必须单独测: `fitReveal` 会缩字号来兜底, 所以"能放下"
    // 可能只是**字号被缩过头**换来的。所以这里同时断言字号仍 >= 34
    // —— 否则一个"把字缩到 20px 塞进去"的实现会假绿。
    {
      const maxAns = "衣柜是后来封住那扇门的隔板, 他把床摆在了原来门的位置, "
        + "所以每晚其实都睡在门口; 门外走廊的声响让他不敢关灯, "
        + "而他自己一直以为那只是习惯。他搬进这间房的时候, "
        + "前一位租客已经把那扇门封死了, 房东只说了句那面墙不要挂重物。"
        + "他每晚听见的其实是楼道里的脚步, 而门就在他背后。";
      u2Send("explanation", {revealed_answer: maxAns,
                             revealed_full_answer: maxAns});
      const body = document.getElementById("reveal-body");
      const fs = parseFloat(getComputedStyle(body).fontSize);
      check(body.scrollHeight <= body.clientHeight + 1,
            "U2-C2: 上限长度解释不得依赖滚动 (scrollH=" + body.scrollHeight
            + " clientH=" + body.clientHeight + ")");
      check(fs >= 34,
            "U2-C2: 不得靠缩字号到 34px 以下来塞进内容, 实际 " + fs);
    }

    // ---- U2-D: 45-60s 核心 + 共同解谜, 完整解释隐藏 ----
    u2Send("contribution");
    check(vis("puzzle"), "U2-D: 谜面应可见");
    check(vis("reveal-core"), "U2-D: 核心答案仍应可见");
    check(!vis("reveal-body"), "U2-D: 完整解释应隐藏(让位给共同解谜)");
    check(vis("reveal-contrib"), "U2-D: 共同解谜应可见");
    {
      const fin = [...document.querySelectorAll(".reveal-contrib-row .final")];
      check(fin.length >= 1, "U2-D: 应出现 ✓ 最后线索 标记");
      const kr = document.getElementById("reveal-core").getBoundingClientRect();
      const cr = document.getElementById("reveal-contrib")
        .getBoundingClientRect();
      check(kr.bottom <= cr.top + 1, "U2-D: 核心答案不得压到贡献链上");
    }

    // ---- U2-E: 谜面中心在三阶段都不被揭晓层盖住 ----
    //
    // ⚠️ 这条必须用 `elementFromPoint`, 不能只断言 `#puzzle` 没有
    // `.hidden` —— 被一层不透明背景盖住的元素**照样**"可见"(U1 之前
    // 就吃过这个假绿)。
    for (const stage of ["core", "explanation", "contribution"]) {
      u2Send(stage);
      const pr = document.getElementById("puzzle").getBoundingClientRect();
      const cx = Math.round(pr.left + pr.width / 2);
      const cy = Math.round(pr.top + pr.height / 2);
      const hit = document.elementFromPoint(cx, cy);
      const inPuzzle = !!(hit && (hit.closest("#top")
                                  || hit.id === "puzzle"
                                  || hit.closest("#puzzle")));
      check(inPuzzle,
            "U2-E: " + stage + " 阶段谜面中心被别的元素盖住 (hit="
            + (hit && (hit.id || hit.className || hit.tagName)) + ")");
    }

    // ---- U2-F: 60 秒结束 -> QA 恢复, 上一题贡献不残留 ----
    send({phase: "qa", puzzle_index: 31, story_index: 31,
          puzzle: "下一道题的谜面。", revealed_answer: "",
          qa_log: [], qa_total: 0, reveal_stage: ""});
    check(!vis("reveal"), "U2-F: 新题揭晓层应隐藏");
    check(vis("bottom"), "U2-F: 问答区应恢复");
    check(document.getElementById("reveal-contrib-list").children.length === 0,
          "U2-F: 上一题贡献链不得残留");
    check(document.getElementById("reveal-core").textContent === "",
          "U2-F: 核心答案应清空");

    // ---- U2-G: 老快照没有 reveal_stage 时退化成两段(U1 行为) ----
    send({phase: "revealed", puzzle_index: 32, story_index: 32,
          puzzle: "兼容测试。", revealed_answer: fullTxt,
          revealed_core_answer: coreTxt, revealed_full_answer: fullTxt,
          reveal_detail_visible: true, solved: false,
          reveal_contributors: [], next_puzzle_ms: 15000});
    check(vis("reveal-body"),
          "U2-G: 无 reveal_stage 但有 detail_visible -> 应显示完整解释");
    send({phase: "revealed", puzzle_index: 33, story_index: 33,
          puzzle: "兼容测试2。", revealed_answer: fullTxt,
          revealed_core_answer: coreTxt, revealed_full_answer: fullTxt,
          reveal_detail_visible: false, solved: false,
          reveal_contributors: [], next_puzzle_ms: 15000});
    check(!vis("reveal-body"),
          "U2-G: 无 reveal_stage 且 detail_visible=false -> 不显示完整解释");

    // ⑭b U3: 结构化题的正文**绝不能**重复核心答案
    //
    // 真实截图 bug: 顶部已经大字显示一次 core_answer, 正文里又出现
    //     【核心答案】
    //     同一段 core_answer
    //     【完整解释】
    //     answer
    // 于是观众看到两次核心答案。
    //
    // 根因在后端(Snapshot 把组合文案当成了 raw full answer, 已由
    // U3-A 修掉); 这一组是**前端**侧的兜底 —— 即使后端某次又下发
    // 组合文案, 前端也不能把整段灌回正文。
    {
      // 现代题的真实形态(U3-A 之后): full 是 raw answer
      const u3Core = "他每天看锅, 是在确认有没有人动过他的东西。";
      const u3Ans = "锅里的状态被他当成一个固定记号; 每天回家后, "
        + "他通过检查这个状态有没有变化, 判断私人物品是否被人动过。";
      // 故意**同时**给一份组合文案的 revealed_answer, 模拟老后端/丢字段
      const u3Composed = "【核心答案】\n" + u3Core + "\n\n【完整解释】\n" + u3Ans;
      const u3Send = (extra) => send(Object.assign({
        phase: "revealed", puzzle_index: 40, story_index: 40,
        puzzle: "他每天回家都要看一眼锅。为什么?",
        revealed_answer: u3Composed, revealed_core_answer: u3Core,
        revealed_full_answer: u3Ans, reveal_stage: "explanation",
        reveal_detail_visible: true, solved: false,
        reveal_contributors: [], next_puzzle_ms: 15000,
      }, extra || {}));

      // ---- U3-C1: core / body 各取 raw, 正文无组合标签 ----
      u3Send({});
      const coreEl = document.getElementById("reveal-core");
      const bodyEl = document.getElementById("reveal-body");
      check(coreEl.textContent.includes(u3Core),
            "U3-C1: 大号 core 应显示 raw core_answer");
      check(bodyEl.textContent.includes(u3Ans),
            "U3-C1: 正文应显示 raw answer");
      for (const tag of ["【核心答案】", "【完整解释】"]) {
        check(bodyEl.textContent.indexOf(tag) === -1,
              "U3-C1: 正文不得出现 " + tag);
      }
      // **最关键的一条**: 查**完整文本出现次数**, 不能只查标签 ——
      // 哪天标签没了但 core 仍重复, 只查标签会假绿。
      {
        const all = document.body.innerText || document.body.textContent || "";
        const occ = all.split(u3Core).length - 1;
        check(occ === 1,
              "U3-C1: core 完整句在可见 DOM 中应只出现 1 次, 实际 " + occ);
        const occAns = all.split("【核心答案】").length - 1;
        check(occAns === 0,
              "U3-C1: 可见 DOM 不该出现任何【核心答案】标签, 实际 " + occAns);
      }

      // ---- U3-C2: `revealed_full_answer` 为空时**绝不**退回组合文案 ----
      //
      // 这是 U3-B 的核心: 结构化题只要有 core, full 为空就让它空着。
      // 若 fallback 到 `revealed_answer`, 上面整段组合文案会灌回正文,
      // 重复 bug 原地复活。
      u3Send({revealed_full_answer: ""});
      check(document.getElementById("reveal-core").textContent.includes(u3Core),
            "U3-C2: full 丢失时大号 core 仍应显示");
      check(!vis("reveal-body"),
            "U3-C2: full 为空 -> 正文不得显示(绝不退回组合文案)");
      {
        const all = document.body.innerText || document.body.textContent || "";
        check(all.indexOf("【核心答案】") === -1,
              "U3-C2: full 丢失后正文不得重新出现组合标签");
        check(all.split(u3Core).length - 1 === 1,
              "U3-C2: core 仍只出现 1 次 (实际 "
              + (all.split(u3Core).length - 1) + ")");
      }

      // ---- U3-D: legacy(无 core)仍能正常揭晓 ----
      //
      // 修 modern path **不能**把老 pool/archive 题的 reveal 弄空。
      send({phase: "revealed", puzzle_index: 41, story_index: 41,
            puzzle: "旧题。", revealed_answer: "旧题只有这一段揭晓。",
            revealed_core_answer: "", revealed_full_answer: "",
            reveal_stage: "explanation", reveal_detail_visible: true,
            solved: false, reveal_contributors: [], next_puzzle_ms: 15000});
      check(document.getElementById("reveal-core").textContent
              .includes("旧题只有这一段揭晓"),
            "U3-D: legacy 题应靠 revealed_answer 兜底显示核心区");
      check(document.body.innerText.indexOf("旧题只有这一段揭晓") !== -1,
            "U3-D: legacy 揭晓文案必须可见");
    }

    // ⑮ U4: 长文本只在真实 overflow 时自动滚动。
    {
      const sizedPuzzle = (n, seed) =>
        (seed.repeat(Math.ceil(n / seed.length) + 1)).slice(0, n - 4)
        + "为什么？";
      const withLineBreaks = (text, every) => {
        const chars = Array.from(text);
        for (let i = every; i < chars.length; i += every) chars[i] = "\n";
        return chars.join("");
      };
      const pv = document.getElementById("puzzle-viewport");
      const puzzle = document.getElementById("puzzle");

      // Case 1: 短谜面保持大字、居中、静态。
      send({phase: "qa", puzzle_index: 50, story_index: 50,
            puzzle: "他每天都把空杯子放在门口。为什么？",
            revealed_answer: "", qa_log: [], qa_total: 0});
      await waitFor(() => pv.dataset.scrollState === "static",
                    "U4-1: 短谜面应完成静态判定 (state="
                    + pv.dataset.scrollState + " scroll=" + pv.scrollHeight
                    + "/" + pv.clientHeight + ")");
      const shortP = puzzle.getBoundingClientRect();
      const shortV = pv.getBoundingClientRect();
      check(!pv.hasAttribute("data-auto-scroll"), "U4-1: 短谜面不得滚动");
      check(parseFloat(getComputedStyle(puzzle).fontSize) >= 55,
            "U4-1: 短谜面应保持大字");
      check(Math.abs((shortP.top + shortP.bottom - shortV.top - shortV.bottom) / 2) < 3,
            "U4-1: 短谜面应垂直居中");

      // Case 2: 中等谜面可以缩字，但完整静态显示。
      const mediumPuzzle = sizedPuzzle(120, "他每晚回家都会检查门缝里的纸片是否移动，");
      send({phase: "qa", puzzle_index: 51, story_index: 51,
            puzzle: mediumPuzzle, revealed_answer: "", qa_log: [], qa_total: 0});
      await waitFor(() => pv.dataset.scrollState === "static",
                    () => "U4-2: 中等谜面应完成静态判定 (state="
                      + pv.dataset.scrollState + " scroll=" + pv.scrollHeight
                      + "/" + pv.clientHeight + " fs="
                      + getComputedStyle(puzzle).fontSize + ")");
      check(!pv.hasAttribute("data-auto-scroll"), "U4-2: 中等谜面不得滚动");
      check(pv.scrollHeight <= pv.clientHeight + 1,
            "U4-2: 中等谜面应完整放下");
      check(parseFloat(getComputedStyle(puzzle).fontSize) >= 46,
            "U4-2: 中等谜面字号不得低于 46px");

      // Case 3/4: 220 字 overflow 后真实移动、到底，且永不侵入 QA。
      // 显式分段，避免 Windows/Linux 中文字体度量不同导致 220 个连续字
      // 在某个平台恰好能塞下，从而把“overflow 行为”误测成“字符数行为”。
      const longPuzzle = withLineBreaks(sizedPuzzle(220,
        "男人每晚都会记录窗边灯光和走廊脚步的先后顺序，"), 10);
      const longState = {phase: "qa", puzzle_index: 52, story_index: 52,
        puzzle: longPuzzle, revealed_answer: "", qa_log: [], qa_total: 0};
      send(longState);
      await waitFor(() => pv.hasAttribute("data-auto-scroll"),
                    () => "U4-3: overflow 谜面应启用自动滚动 (state="
                      + pv.dataset.scrollState + " scroll=" + pv.scrollHeight
                      + "/" + pv.clientHeight + " fs="
                      + getComputedStyle(puzzle).fontSize + ")");
      check(parseFloat(getComputedStyle(puzzle).fontSize) >= 46,
            "U4-3: overflow 谜面字号不得低于 46px");
      {
        const vr = pv.getBoundingClientRect();
        const workspaceTop = parseFloat(document.getElementById("content")
          .style.getPropertyValue("--workspace-top"));
        const br = document.getElementById("bottom").getBoundingClientRect();
        const hr = document.getElementById("puzzle-head").getBoundingClientRect();
        const sr = document.getElementById("stage").getBoundingClientRect();
        const expectedTop = sr.top + workspaceTop * (sr.height / 1920);
        check(hr.bottom <= vr.top + 1,
              "U4-4: puzzle viewport 不得覆盖 puzzle header");
        check(vr.bottom <= expectedTop + 1,
              "U4-4: puzzle viewport 不得侵入 QA");
        check(Math.abs(br.top - expectedTop) <= 1,
              "U4-4: bottom 仍服从 workspace-top (bottom=" + br.top
              + " workspace=" + expectedTop + ")");
      }
      await waitFor(() => pv.dataset.scrollState === "moving" && pv.scrollTop > 0,
                    "U4-3: 顶部停留后谜面应真实向下移动");
      const beforeSnapshot = pv.scrollTop;
      send(Object.assign({}, longState, {stats: {questions: 9, answered: 8}}));
      await wait(35);
      check(pv.scrollTop >= beforeSnapshot,
            "U4-3: 无关 4Hz snapshot 不得重启滚动");
      await waitFor(() => pv.dataset.scrollState === "bottom",
                    "U4-3: 长谜面应到达底部", 5000);
      check(pv.scrollTop + pv.clientHeight >= pv.scrollHeight - 1,
            "U4-3: 长谜面最后一行必须完整进入 viewport");

      // Case 5: 揭晓立刻取消谜面滚动并复位顶部。
      const shortAnswer = "纸片是他留下的记号，用来确认是否有人进过房间。";
      send({phase: "revealed", puzzle_index: 52, story_index: 52,
            puzzle: longPuzzle, revealed_answer: shortAnswer,
            revealed_core_answer: "纸片是防入侵记号。",
            revealed_full_answer: shortAnswer, reveal_stage: "explanation",
            reveal_detail_visible: true, solved: false,
            reveal_contributors: [], next_puzzle_ms: 15000});
      check(pv.scrollTop === 0 && !pv.hasAttribute("data-auto-scroll"),
            "U4-5: 揭晓时谜面应停止并回到顶部");
      {
        const rr = document.getElementById("reveal").getBoundingClientRect();
        const sr = document.getElementById("stage").getBoundingClientRect();
        const workspaceTop = parseFloat(document.getElementById("content")
          .style.getPropertyValue("--workspace-top"));
        const expectedTop = sr.top + workspaceTop * (sr.height / 1920);
        check(Math.abs(rr.top - expectedTop) <= 1
              && Math.abs(rr.bottom - sr.bottom) <= 1,
              "U4-5: reveal 必须继续接替原 bottom 工作区");
      }

      // Case 6/8: 短汤底静态；core 始终 64px 且不滚。
      const rb = document.getElementById("reveal-body");
      const rc = document.getElementById("reveal-core");
      await waitFor(() => rb.dataset.scrollState === "static",
                    "U4-6: 短汤底应完成静态判定 (state="
                    + rb.dataset.scrollState + " scroll=" + rb.scrollHeight
                    + "/" + rb.clientHeight + ")");
      check(!rb.hasAttribute("data-auto-scroll"), "U4-6: 短汤底不得滚动");
      check(parseFloat(getComputedStyle(rc).fontSize) === 64,
            "U4-8: core_answer 应保持 64px");
      check(!rc.hasAttribute("data-auto-scroll") && rc.scrollTop === 0,
            "U4-8: core_answer 永远静态");

      // Case 7: 300 字完整解释自动滚一遍并停在底部。
      // 宽 Latin 词组会在词间自然换行且留下行尾空间；比依赖平台中文字体
      // 的刚好换行稳定，也不依赖 reveal-body 是否保留源文本换行。
      const longAnswer = "WWWWWWWWWWWWWWWWWW ".repeat(20).slice(0, 300);
      send({phase: "revealed", puzzle_index: 52, story_index: 52,
            puzzle: longPuzzle, revealed_answer: longAnswer,
            revealed_core_answer: "纸片是防入侵记号。",
            revealed_full_answer: longAnswer, reveal_stage: "explanation",
            reveal_detail_visible: true, solved: false,
            reveal_contributors: [], next_puzzle_ms: 15000});
      await waitFor(() => rb.dataset.scrollState === "moving" && rb.scrollTop > 0,
                    "U4-7: 长汤底顶部停留后应真实移动");
      await waitFor(() => rb.dataset.scrollState === "bottom",
                    "U4-7: 长汤底应滚到结尾", 5000);
      check(rb.scrollTop + rb.clientHeight >= rb.scrollHeight - 1,
            "U4-7: 长汤底最后一行必须完整可见");

      // Case 9: explanation -> contribution 清理旧滚动；下一题从顶部开始。
      send({phase: "revealed", puzzle_index: 52, story_index: 52,
            puzzle: longPuzzle, revealed_answer: longAnswer,
            revealed_core_answer: "纸片是防入侵记号。",
            revealed_full_answer: longAnswer, reveal_stage: "contribution",
            reveal_detail_visible: true, solved: false,
            reveal_contributors: mkContrib(false), next_puzzle_ms: 10000});
      check(rb.scrollTop === 0 && !rb.hasAttribute("data-auto-scroll"),
            "U4-9: contribution 应取消并复位汤底滚动");
      send({phase: "qa", puzzle_index: 53, story_index: 53,
            puzzle: "下一题从顶部开始。为什么？", revealed_answer: "",
            qa_log: [], qa_total: 0});
      check(pv.scrollTop === 0, "U4-9: 下一题谜面必须从顶部开始");

      // Case 10: A/B/C 快切，旧异步回调不能移动当前题。
      const fastA = sizedPuzzle(220, "题目A记录了很多连续发生但互相矛盾的细节，");
      const fastB = sizedPuzzle(220, "题目B记录了很多连续发生但互相矛盾的细节，");
      const fastC = sizedPuzzle(220, "题目C记录了很多连续发生但互相矛盾的细节，");
      send({phase: "qa", puzzle_index: 54, story_index: 54, puzzle: fastA});
      send({phase: "qa", puzzle_index: 55, story_index: 55, puzzle: fastB});
      send({phase: "qa", puzzle_index: 56, story_index: 56, puzzle: fastC});
      await wait(35); // 小于 C 的 top hold；A/B 的 RAF 已有机会误触发。
      check(puzzle.textContent === fastC && pv.scrollTop === 0,
            "U4-10: 快切后 A/B callback 不得移动 C");
    }


    //
    // 后台补题是运维概念, 不该泄漏给观众 —— 他们只该感受到
    // "看答案 60 秒 -> 下一题直接出现"。
    {
      const txt = document.body.innerText || document.body.textContent || "";
      for (const bad of ["补题", "库存", "已准备", "playable", "playable_min",
                         "prefetch", "题池", "生成中"]) {
        check(txt.indexOf(bad) === -1,
              "U1-F: 观众可见文案不该出现 " + bad);
      }
    }
  } catch (e) { errors.push(e.stack); }
  const result = document.createElement("pre");
  result.id = "test-result"; result.hidden = true;
  result.textContent = JSON.stringify(errors);
  document.body.appendChild(result);
});
'''


ANNOUNCER_CHECK = r'''
window.addEventListener('error', e => {
  const result=document.createElement('pre'); result.id='test-result';
  result.textContent=JSON.stringify([String(e.message)]); document.body.appendChild(result);
});
window.socket = null;
window.WebSocket = class { constructor() { window.socket = this; } };
// Drive real application timers deterministically; layout/font/WAAPI are real Chrome.
let clock = 0, serial = 0;
const tasks = new Map();
performance.now = () => clock;
window.setTimeout = (fn, delay=0) => {
  const id = ++serial; tasks.set(id, {fn, at: clock + delay}); return id;
};
window.clearTimeout = id => tasks.delete(id);
window.setInterval = (fn, delay) => {
  const id = ++serial; tasks.set(id, {fn, at: clock + delay, interval: delay}); return id;
};
window.clearInterval = window.clearTimeout;
const tick = async ms => {
  const end = clock + ms;
  for (;;) {
    const next = [...tasks].filter(([,t]) => t.at <= end).sort((a,b) => a[1].at-b[1].at)[0];
    if (!next) break;
    const [id,t] = next; clock = t.at;
    if (t.interval) t.at += t.interval; else tasks.delete(id);
    t.fn(); await Promise.resolve(); await Promise.resolve();
  }
  clock = end; await Promise.resolve(); await Promise.resolve();
};
let cfg = null, failure = 'missing', fetchCount = 0;
window.fetch = async (url, options) => {
  if (url !== '/announcements.json' || options.cache !== 'no-store') throw Error('bad fetch');
  fetchCount++;
  return {ok: failure !== 'missing', json: async () => {
    if (failure === 'json') throw SyntaxError('synthetic invalid JSON');
    return structuredClone(cfg);
  }};
};
const media = window.matchMedia.bind(window);
let reduced = false;
const listeners = [];
window.matchMedia = query => query === '(prefers-reduced-motion: reduce)' ? {
  get matches() { return reduced; },
  addEventListener: (_, fn) => listeners.push(fn),
} : media(query);
window.addEventListener('load', async () => {
  await document.fonts.ready;
  const errors = [];
  const check = (ok, message) => { if (!ok) errors.push(message); };
  const box = document.getElementById('announcer'), text = document.getElementById('announcer-text');
  const qa = document.getElementById('qa');
  let state = {phase:'qa', puzzle_index:1, puzzle:'合成题面。为什么？', qa_log:[], qa_total:0,
               hint_count:2, hint_text:'历史提示',
               ai_player:{questions_earned:20}, leaderboard:[]};
  const send = extra => { Object.assign(state, extra); socket.onmessage({data:JSON.stringify(state)}); };
  const notice = () => text.textContent;
  const anim = () => text.getAnimations()[0];
  const rect = id => document.getElementById(id).getBoundingClientRect();
  const config = (items, extra={}) => Object.assign({enabled:true, interval_seconds:90,
    hold_seconds:4, long_text_speed_px_s:80, items:items.map((text,i) => ({id:String(i),enabled:true,text}))}, extra);
  const reload = async c => { cfg=c; failure=''; await tick(15000); };
  const finish = async () => {
    const duration = anim() ? anim().effect.getTiming().duration : 4000;
    await tick(duration + 1);
  };
  // 精确走到“当前最早 one-shot timer”的结束点。announcer 的每一轮
  // show() 都只有一个 one-shot done timer；interval/reload/poll 都是 interval。
  // 这个 helper 用于已经播到中途的场景，避免 finish() 再加一整个 duration
  // 而跨过后续 preset / leaderboard turn。
  const finishCurrentTurn = async () => {
    const due = [...tasks.values()]
      .filter(t => !t.interval && t.at >= clock)
      .map(t => t.at)
      .sort((a,b) => a-b)[0];
    if (due === undefined) { await finish(); return; }
    await tick(Math.max(0, due - clock) + 1);
  };
  try {
    send();
    check(box.dataset.kind === 'leaderboard' && notice()==='' && !anim(),
          'A16 empty leaderboard is quiet and must not replay historical summons/hints');
    check(!qa.contains(box) && !document.getElementById('stats').contains(box), 'A16 announcer outside QA/stats');
    const geometry = () => {
      const b=box.getBoundingClientRect(), q=rect('qa'), p=rect('puzzle-viewport');
      const scale=rect('stage').width/1080;
      const workspace=parseFloat(getComputedStyle(document.getElementById('content')).getPropertyValue('--workspace-top'));
      check(b.top >= rect('content').top + workspace*scale - 1 && b.top >= p.bottom-1,
            'A16 announcer below puzzle and workspace top');
      check(b.bottom <= q.top && b.bottom < rect('prompt').top && b.bottom < rect('stats').top,
            'A16 announcer above QA/prompt/stats, not bottom');
      check(b.bottom < rect('stage').top + 1400*scale && b.height > 0,
            'A16 announcer in safe upper workspace');
    };
    geometry();
    const before=box.getBoundingClientRect().top;
    send({qa_log:Array.from({length:70},(_,i)=>({qid:i+1,user_name:'Alice',text:'合成问题'+i,verdict:'是',kind:'qa'})),qa_total:70});
    qa.scrollTop=qa.scrollHeight;
    check(box.getBoundingClientRect().top === before, 'A16 QA append/scroll must not move announcer');
    document.dispatchEvent(new KeyboardEvent('keydown',{key:'d'})); geometry();
    document.dispatchEvent(new KeyboardEvent('keydown',{key:'d'}));
    // ---- Issue #43: 点赞推进公告(显式 like_progress_notice 事件) ----
    // 首帧快照无公告 -> 之后收到的 seq 都是活事件, 照播。
    send({like_progress_notice:{seq:1,round_index:1,pulses:1,phase:'qa',
                                text:'❤️ 点赞助攻！AI玩家加入，游戏进度加快'}});
    check(box.dataset.kind==='like' && notice().includes('点赞助攻'),
          'A16 live like notice plays');
    const first=anim();
    for(let i=0;i<20;i++) send();
    check(anim()===first, 'A16 repeated snapshots do not restart animation');
    send({ai_player:{questions_available:0,questions_used:1,in_flight:true,
                     likes_progress:5,likes_per_progress:100}});
    check(document.getElementById('stats').textContent.includes('AI玩家正在推理…'),
          'A16 AI in-flight shows reasoning state only');
    send({ai_player:{questions_available:0,questions_used:1,in_flight:false,
                     likes_progress:5,likes_per_progress:100}});
    check(document.getElementById('stats').textContent.includes('每100点赞，AI玩家助攻并加速本题')
          && document.getElementById('stats').textContent.includes('❤️ 5 / 100'),
          'A16 resident copy is like-progress semantics with server progress');
    check(anim()===first, 'A16 ai_player field updates do not interrupt like notice');
    socket.onclose(); await tick(801); send();
    check(anim()===first, 'A16 reconnect retains baseline');
    await tick(3000);
    check(box.dataset.kind==='like' && anim()===first, 'A16 like survives field updates for full hold');
    await finish();
    check(box.dataset.kind==='leaderboard', 'A16 same snapshots do not replay old like');
    // 服务端已把一批 pulse 聚合成一条: ×N 一次播完。
    send({like_progress_notice:{seq:2,round_index:1,pulses:3,phase:'qa',
                                text:'❤️ 点赞助攻 ×3！AI玩家获得更多行动机会，游戏加速'}});
    check(notice().includes('×3'), 'A16 burst aggregates into one ×N notice');
    // 连续点赞事件: 单一待播槽位, 新 seq 直接覆盖 -> 只显示最新一条。
    send({like_progress_notice:{seq:3,round_index:1,pulses:1,phase:'qa',
                                text:'❤️ 点赞助攻！AI玩家加入，游戏进度加快'}});
    send({like_progress_notice:{seq:4,round_index:1,pulses:2,phase:'qa',
                                text:'❤️ 点赞助攻 ×2！AI玩家获得更多行动机会，游戏加速'}});
    check(notice().includes('×2') && !notice().includes('×3'),
          'A16 single pending slot keeps only the newest like notice');
    await finish(); check(box.dataset.kind==='leaderboard', 'A16 no unbounded like backlog');
    send({leaderboard:[
      {rank:1,user_name:'Alice',solved_count:5},
      {rank:2,user_name:'Bob',solved_count:4},
      {rank:3,user_name:'Carol',solved_count:3}
    ]});
    check(box.dataset.kind==='leaderboard' && box.dataset.motion==='static'
          && !anim() && notice().includes('Alice 5题'),
          'A16 one-page leaderboard is a persistent static baseline');

    const top10=Array.from({length:10},(_,i)=>({
      rank:i+1,user_name:'P'+(i+1),solved_count:20-i
    }));
    send({leaderboard:top10});
    check(box.dataset.kind==='leaderboard'
          && notice().includes('1. P1 20题')
          && notice().includes('5. P5 16题')
          && notice().includes('10. P10 11题')
          && !notice().includes('1/2') && !notice().includes('2/2'),
          'A16 cumulative Top10 is one complete message, not two logical pages');
    check(box.dataset.motion==='marquee' && !!anim(),
          'A16 long Top10 uses one continuous marquee');

    send({like_progress_notice:{seq:5,round_index:1,pulses:1,phase:'qa',
                                text:'❤️ 点赞助攻！AI玩家加入，游戏进度加快'}});
    check(box.dataset.kind==='like', 'A16 like temporarily overlays Top10 marquee');
    await finish();
    check(box.dataset.kind==='leaderboard'
          && notice().includes('1. P1 20题')
          && notice().includes('10. P10 11题'),
          'A16 after AI, complete Top10 restarts from rank 1');

    // 新题也重新从整条榜首开始；不再维护 1/2、2/2 页码状态。
    send({puzzle_index:2,hint_count:0,hint_text:'',qa_log:[]});
    check(box.dataset.kind==='leaderboard'
          && notice().includes('1. P1 20题')
          && notice().includes('10. P10 11题')
          && !notice().includes('/2'),
          'A16 new puzzle restarts one-line Top10 from rank 1');

    const hintRow=(qid,text)=>({qid,user_name:'提示',kind:'hint',text,verdict:''});
    send({hint_count:3,hint_text:'注意灯的方向',qa_log:[hintRow(-3,'注意灯的方向')],qa_total:71});
    check(box.dataset.kind==='hint' && notice()==='💡 提示：注意灯的方向', 'A16 new hint announces');
    check(qa.querySelector('.kind-hint').textContent.includes('注意灯的方向'), 'A16 hint QA history retained');
    const hintAnimation=anim();
    for(let i=0;i<20;i++) send();
    check(anim()===hintAnimation, 'A16 repeated hint snapshot does not restart');
    send({hint_count:4,hint_text:'',qa_log:[hintRow(-3,'注意灯的方向'),hintRow(-4,'注意门外的人')],qa_total:72});
    await finish();
    check(notice()==='💡 提示：注意门外的人', 'A16 hint uses latest QA hint fallback, FIFO');
    // Issue #43: 点赞不再抢占提示 —— 正在播的提示必须完整播完,
    // 点赞在它之后接上(§15: 提示/重要公告不能被点赞腰斩)。
    send({like_progress_notice:{seq:6,round_index:2,pulses:1,phase:'qa',
                                text:'❤️ 点赞助攻！AI玩家加入，游戏进度加快'}});
    check(box.dataset.kind==='hint' && notice()==='💡 提示：注意门外的人',
          'A16 like never cuts a playing hint short');
    await finish(); check(box.dataset.kind==='like',
          'A16 like notice plays right after the hint finishes');
    await finish(); check(box.dataset.kind==='leaderboard', 'A16 repeated like snapshots enqueue once only');
    send({hint_count:5,hint_text:'旧题尚未播完'});
    send({puzzle_index:3,hint_count:7,hint_text:'新题首帧历史提示'});
    check(box.dataset.kind==='leaderboard', 'A16 new puzzle resets baseline and cancels old hint');
    send({hint_count:8,hint_text:'本题新提示'});
    check(notice()==='💡 提示：本题新提示', 'A16 new puzzle subsequent hint announces');
    // 旧题的待播点赞公告不跨题: hint 播放期间排队的 like,
    // 在换题后必须被丢弃而不是继续播(§16: 不跨题迟到)。
    send({like_progress_notice:{seq:7,round_index:3,pulses:1,phase:'qa',
                                text:'❤️ 旧题的点赞助攻'}});
    check(box.dataset.kind==='hint', 'A16 like waits behind playing hint');
    send({puzzle_index:4,hint_count:0,hint_text:'',qa_log:[]});
    check(!notice().includes('旧题的点赞助攻'),
          'A16 pending like from an old round is dropped on new puzzle');
    await finish();
    check(!notice().includes('旧题的点赞助攻'),
          'A16 dropped like notice never plays later');
    send({hint_count:9,hint_text:'新题的新提示'});
    check(notice()==='💡 提示：新题的新提示', 'A16 hints still work after dropped like');
    await finish(); check(box.dataset.kind==='leaderboard', 'A16 new puzzle hint plays once');

    // ---- review round 2: 跨题竞态反证 ----
    // (a) SETTING 期间入账的点赞公告属于"正在准备的题"(round 5): 它必须
    //     活着跨过换题帧(setting→qa + puzzle 4→5 同帧), 不能被
    //     "先标记已见、再清掉"导致永久不播。
    send({phase:'setting',
          like_progress_notice:{seq:8,round_index:5,pulses:1,phase:'setting',
                                text:'❤️ 点赞助攻已累积，新题开始后生效'}});
    check(!notice().includes('新题开始后生效'),
          'A16 setting-phase like notice waits while box hidden');
    send({phase:'qa',puzzle_index:5,hint_count:0,hint_text:'',qa_log:[]});
    check(notice().includes('新题开始后生效'),
          'A16 setting-phase like notice survives the new-round first frame');
    await finish();
    check(box.dataset.kind==='leaderboard', 'A16 setting-phase like notice drains');
    // (b) 旧 round 的迟到公告: seq 是新的, 但 round 已过去 -> 只标记已见,
    //     永不播放(round 校验, review round 2 的另一半)。
    send({like_progress_notice:{seq:9,round_index:4,pulses:2,phase:'qa',
                                text:'❤️ 上一题的迟到助攻'}});
    check(!notice().includes('迟到助攻'),
          'A16 stale old-round notice is never played');
    await finish();
    check(!notice().includes('迟到助攻'),
          'A16 stale old-round notice stays dead');

    // 术语分两层:
    //   ① 观众内容(原话 / 题目正文 / 贡献 quote / operator 自定义文案)逐字透传;
    //   ② 系统固定文案在**源头**(story/engine.py)就写成汤面/汤底, 前端不做替换。
    // 这里用一条带"谜底"的**观众可控**文本验证 ① —— 系统固定文案的正确性
    // 由 test_engine 的回归覆盖(见 test_fixed_viewer_copy_uses_soup_terms)。
    send({phase:'revealed',revealed_answer:'合成故事正文',reveal_stage:'contribution',solved:true,
          reveal_contributors:[{qid:1,user_name:'Alice',text:'原话包含谜底一词',verdict:'是',is_final:true}]});
    check(document.getElementById('reveal-label').textContent==='汤底揭晓'
          && document.getElementById('reveal-contrib-title').textContent==='共同猜汤', 'A16 reveal labels use soup terminology');
    check(document.getElementById('reveal-contrib-list').textContent.includes('原话包含谜底一词'), 'A16 viewer content not rewritten');
    send({phase:'qa',revealed_answer:'',qa_log:[],qa_total:74});
    check(document.getElementById('prompt').textContent.includes('猜中汤底我就揭晓')
          && notice().includes('累计猜汤榜'), 'A16 prompt and leaderboard terminology');
    // 反证: 若有人把术语做回"对任意文本 replaceAll", 下面几条必然失败 ——
    // 它会把观众提问、计时器标签、贡献链原话一起改写掉。
    send({phase:'qa',qa_log:[{qid:1,user_name:'观众甲',text:'谜底是不是和灯有关？',verdict:'不是',kind:'qa'}],
          next_event_ms:60000,next_event_kind:'hint',next_event_label:'谜底揭晓倒计时',qa_total:75});
    check(qa.textContent.includes('谜底是不是和灯有关？'), 'A16 viewer question wording never rewritten');
    // 计时器文案由真实 rAF 循环写入(setTimeout 被 fake, rAF 没有)。
    await new Promise(requestAnimationFrame);
    check(document.getElementById('puzzle-timer').textContent.startsWith('谜底揭晓倒计时'),
          'A16 timer label passes through verbatim: '
          + document.getElementById('puzzle-timer').textContent);

    send({leaderboard:[]});
    check(box.dataset.kind==='leaderboard' && notice()==='' && !anim(),
          'A16 clearing leaderboard removes placeholder copy without animation');
    await reload(config(['预设甲','禁用','预设乙'], {hold_seconds:15})); cfg.items[1].enabled=false;
    await tick(15000); // reload disabled item
    send({phase:'setting'}); await tick(300000); send({phase:'qa'});
    await tick(89999);
    check(box.dataset.kind==='leaderboard' && notice()==='' && !anim(),
          'A16 empty leaderboard stays quiet before preset due');
    await tick(251); check(notice().includes('预设甲'), 'A16 preset still triggers from empty leaderboard state');
    const short=anim(), timing=short.effect.getTiming(), frames=short.effect.getKeyframes();
    for(let i=0;i<20;i++) send();
    check(anim()===short, 'A16 repeated snapshots do not restart or enqueue preset');
    check(box.dataset.motion==='slide' && timing.duration===17000 && frames.length===4,
          'A16 preset short uses symmetric 1s enter / 15s hold / 1s exit');
    short.currentTime=1000;
    check(text.getBoundingClientRect().left >= box.getBoundingClientRect().left-1
          && text.getBoundingClientRect().right <= box.getBoundingClientRect().right+1,
          'A16 short hold fully visible');
    send({like_progress_notice:{seq:10,round_index:5,pulses:1,phase:'qa',
                                text:'❤️ 点赞助攻！AI玩家加入，游戏进度加快'}});
    check(box.dataset.kind==='like', 'A16 like preempts active preset immediately (<1s)');
    check(anim().effect.getTiming().duration===7000,
          'A16 short like notice uses capped 5s hold plus symmetric 1s slides');
    send({hint_count:12,hint_text:'提示应抢占点赞公告'});
    check(box.dataset.kind==='hint', 'A16 hint preempts active like notice');
    check(anim().effect.getTiming().duration===7000,
          'A16 short hint uses capped 5s hold plus symmetric 1s slides');
    await tick(90000); check(notice().includes('预设乙'), 'A16 interrupted preset advances RR, disabled skipped');
    await tick(90000); check(notice().includes('预设甲'), 'A16 RR returns to first enabled item');

    // 真实用户配置回归：interval=22s / hold=15s。
    // Top10 已改成一整条长 marquee；22s 公告到点时不能把榜截在中间，
    // 必须等第 10 名完整滚出视口后再接公告。
    await finish(); // 让当前 preset 正常结束
    await reload(config(['预设甲','禁用','预设乙'],
                        {interval_seconds:22, hold_seconds:15}));
    cfg.items[1].enabled=false;
    const longTop10=Array.from({length:10},(_,i)=>({
      rank:i+1,
      user_name:'LongPlayerName'.repeat(2)+(i+1),
      solved_count:30-i
    }));
    send({phase:'setting'});
    send({phase:'qa', leaderboard:longTop10});
    check(box.dataset.kind==='leaderboard'
          && box.dataset.motion==='marquee'
          && notice().includes('1. LongPlayerName')
          && notice().includes('10. LongPlayerName')
          && !notice().includes('/2'),
          'A16 22s config starts one complete Top10 marquee');
    check(anim().effect.getTiming().duration > 22250,
          'A16 synthetic Top10 marquee lasts beyond the 22s preset deadline');

    await tick(22250);
    check(box.dataset.kind==='leaderboard'
          && notice().includes('10. LongPlayerName'),
          'A16 due preset must not cut the one-line Top10 at 22s');

    // 精确走到这条 Top10 自己的 done()；done -> pump 会立即接上
    // 已经到期的 preset。
    await finishCurrentTurn();
    check(box.dataset.kind==='preset' && /预设[甲乙]/.test(notice()),
          'A16 queued due preset starts immediately after complete Top10 finishes');
    await finishCurrentTurn();
    check(box.dataset.kind==='leaderboard'
          && notice().includes('1. LongPlayerName')
          && notice().includes('10. LongPlayerName'),
          'A16 after preset, the complete Top10 marquee starts again');

    send({leaderboard:[]});
    check(box.dataset.kind==='leaderboard' && notice()==='' && !anim(),
          'A16 leaderboard can return to quiet empty baseline after queued preset test');

    // 后面的 last-known-good 配置测试仍使用 90s，避免把 22s 场景时间轴扩散出去。
    await reload(config(['预设甲','禁用','预设乙'], {hold_seconds:15}));
    cfg.items[1].enabled=false;
    await tick(15000); // 让 last-known-good 真正载入 disabled 状态
    send({phase:'setting'}); send({phase:'qa'});

    // Bad JSON / bad types / HTTP failure each retain the last-good two items.
    // 每种失败独立做 setting -> qa，重新锚定 nextPreset，避免上一轮 preset
    // 的 17s 生命周期污染下一轮时间轴；这里测的是“坏配置不能覆盖最后一次
    // 好配置”，不是跨轮 timing 偶合。
    for (const bad of ['json','types','missing']) {
      failure=bad; if (bad==='types') { failure=''; cfg={enabled:'bad',items:[]}; }
      send({phase:'setting'}); send({phase:'qa'});
      await tick(92000);
      check(box.dataset.kind==='preset' && /预设[甲乙]/.test(notice()),
            'A16 last-known-good survives '+bad);
      send(); check(box.dataset.kind==='preset', 'A16 config failure does not block WS');
    }
    await reload(config(['更新公告']));
    send({phase:'setting'}); send({phase:'qa'});
    await tick(90250); check(notice().includes('更新公告'), 'A16 hot update replaces items');
    await reload(config(['不该播放'],{enabled:false}));
    await tick(90000); check(box.dataset.kind==='leaderboard', 'A16 global disable');

    const long='长公告含空格 和标点，'.repeat(12)+'末尾可读';
    await reload(config(['😀'.repeat(161),long]));
    send({phase:'setting'}); send({phase:'qa'}); await tick(90250);
    check(notice()==='📢 游戏公告 '+long, 'A16 >160 Unicode item skipped, not truncated; valid item retained');
    check(box.dataset.motion==='marquee', 'A16 long uses marquee');
    const moving=anim(), duration=moving.effect.getTiming().duration;
    check(Math.abs(duration-(box.clientWidth+text.scrollWidth)/80*1000)<1 && duration>4600,
          'A16 long duration is full measured distance / speed (not fixed 4s)');
    check(getComputedStyle(text).textOverflow!=='ellipsis' && getComputedStyle(text).fontSize==='28px'
          && text.offsetHeight===58 && box.clientHeight===58, 'A16 no ellipsis/shrink/wrap');
    moving.currentTime=duration-100;
    const tail=text.getBoundingClientRect().right, left=box.getBoundingClientRect().left;
    check(tail>=left-1 && tail<left+20, 'A16 final characters travel completely across viewport');
    await tick(5000); check(notice().endsWith('末尾可读'), 'A16 long item not removed after 4s');
    send({phase:'revealed',revealed_answer:'合成谜底',revealed_core_answer:'合成核心答案',reveal_stage:'core'});
    check(box.getBoundingClientRect().height===0 && !anim(), 'A16 reveal hides/cancels announcer');
    await tick(300000); send({phase:'qa',revealed_answer:''});
    check(box.dataset.kind==='leaderboard', 'A16 QA returns to default, no accumulated presets');

    reduced=true; listeners.forEach(fn=>fn());
    await tick(90250);
    let pages='', count=0;
    while(box.dataset.kind==='preset' && count++<30) {
      pages+=notice();
      check(!anim() && text.scrollWidth<=box.clientWidth, 'A16 reduced motion pages fully fit without motion');
      await tick(4000);
    }
    check(pages==='📢 游戏公告 '+long, 'A16 reduced motion preserves every character');
    send({leaderboard:[1,2,3,4,5,6,7,8,9,10].map(rank=>({
      rank,user_name:'SyntheticLongName'.repeat(6)+rank,solved_count:21-rank
    }))});
    const boardRows=state.leaderboard;
    const board='📢 累计猜汤榜　'
      + boardRows.map(r=>`${r.rank}. ${r.user_name} ${r.solved_count}题`).join('　');
    let boardPages='';
    for(let i=0;i<120 && boardPages.length<board.length;i++) {
      boardPages+=notice();
      check(text.scrollWidth<=box.clientWidth,
            'A16 reduced-motion slices of one Top10 message fit without animation');
      await tick(4000);
    }
    check(boardPages===board,
          'A16 reduced-motion preserves every character of the one-line Top10 message');
    document.dispatchEvent(new KeyboardEvent('keydown',{key:'d'}));
    await new Promise(requestAnimationFrame);
    await new Promise(requestAnimationFrame);
    check(text.scrollWidth<=box.clientWidth && !anim(), 'A16 debug resize remeasures reduced-motion pages');
    geometry();

    // Hints share exactly the same full-text marquee / static-page renderer.
    const longHint='提示内容不能被永久截断，'.repeat(10)+'最后线索';
    send({hint_count:13,hint_text:longHint});
    let hintPages='', pageCount=0;
    while(box.dataset.kind==='hint' && pageCount++<30) {
      hintPages+=notice();
      check(!anim() && text.scrollWidth<=box.clientWidth, 'A16 reduced-motion hint pages fit');
      await tick(4000);
    }
    check(hintPages==='💡 提示：'+longHint, 'A16 reduced-motion hint preserves complete text');
    reduced=false; listeners.forEach(fn=>fn());
    send({hint_count:14,hint_text:longHint});
    check(box.dataset.motion==='marquee' && notice()==='💡 提示：'+longHint, 'A16 long hint uses untruncated marquee');
    check(Math.abs(anim().effect.getTiming().duration-(box.clientWidth+text.scrollWidth)/80*1000)<1,
          'A16 long hint duration uses full measured distance');
    await finish(); check(box.dataset.kind!=='hint', 'A16 long hint finishes without duplicate');
    check(fetchCount>10, 'A16 periodic hot reload actually runs');
  } catch(e) { errors.push(e.stack); }
  const result=document.createElement('pre'); result.id='test-result'; result.hidden=true;
  result.textContent=JSON.stringify(errors); document.body.appendChild(result);
});
'''


def test_announcement_http():
    import socket
    from http.client import HTTPConnection
    from unittest.mock import patch
    sys.path.insert(0, str(ROOT))
    from story.server import RenderServer, StateHub

    with tempfile.TemporaryDirectory() as tmp, patch("story.server._WEB_DIR", tmp):
        (Path(tmp) / "index.html").write_text("synthetic page", encoding="utf-8")
        config_path = Path(tmp) / "announcements.json"
        config_path.write_bytes((ROOT / "web/announcements.json").read_bytes())
        hub = StateHub()
        hub.publish({"leaderboard": []})
        server = RenderServer(hub, "127.0.0.1", 0)
        server.start()
        port = server._httpd.server_port

        def get(path, method="GET"):
            conn = HTTPConnection("127.0.0.1", port, timeout=3)
            try:
                conn.request(method, path)
                response = conn.getresponse()
                return response.status, dict(response.getheaders()), response.read()
            finally:
                conn.close()

        try:
            status, headers, body = get("/announcements.json")
            assert status == 200 and json.loads(body)["enabled"] is True
            assert headers["Content-Type"] == "application/json; charset=utf-8"
            assert headers["Cache-Control"] == "no-store"
            assert get("/announcements.json", "POST")[0] == 501
            for mode in ("missing", "invalid"):
                if mode == "missing":
                    config_path.unlink()
                    assert get("/announcements.json")[0] == 404
                else:
                    config_path.write_text("{invalid", encoding="utf-8")
                    assert get("/announcements.json")[2] == b"{invalid"
                assert get("/")[0] == 200
                assert json.loads(get("/state")[2]) == {"leaderboard": []}
                with socket.create_connection(("127.0.0.1", port), timeout=3) as client:
                    client.sendall(b"GET /ws HTTP/1.1\r\nHost: localhost\r\n"
                                   b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                                   b"Sec-WebSocket-Key: c3ludGhldGljLWZpeHR1cmU=\r\n\r\n")
                    data = b""
                    while b'"leaderboard": []' not in data:
                        data += client.recv(4096)
                    assert b"101 Switching Protocols" in data
                    client.sendall(b"\x88\x00")
        finally:
            server.stop()
            server._httpd.server_close()
    print("PASS: readonly announcement HTTP / no-store / missing-invalid config preserves page + WS")


def main():
    test_announcement_http()
    if not CHROME:
        # 找不到浏览器时**默认失败**, 不静默跳过 —— 静默跳过会让这个
        # 套件在 CI 上"一直绿", 而它验的正是真实布局, 恰恰是最该跑的。
        # 确实想跳过(比如本地没装)就显式设 HGT_SKIP_WEB=1。
        print("\n" + _NEED_MSG)
        if os.environ.get("HGT_SKIP_WEB") == "1":
            print("HGT_SKIP_WEB=1 -> 跳过")
            return
        sys.exit(1)
    print(f"(使用浏览器: {CHROME})")
    for script in (CHECK, ANNOUNCER_CHECK):
        run_browser(script)


def run_browser(script):
    source = (ROOT / "web/index.html").read_text(encoding="utf-8")
    source = source.replace('href="/style.css"', 'href="' + (ROOT / "web/style.css").as_uri() + '"')
    source = source.replace('<script src="/app.js"></script>',
                            "<script>" + script + "</script><script src=\"" +
                            (ROOT / "web/app.js").as_uri() + '\"></script>')
    # data/ 在干净 checkout 上可能不存在(它只靠两个 .md 撑着)。
    (ROOT / "data").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT / "data") as tmp:
        page = Path(tmp) / "test.html"
        page.write_text(source, encoding="utf-8")
        result = subprocess.run([str(CHROME), "--headless=new", "--disable-gpu", "--enable-logging=stderr",
                                 "--no-first-run", "--hide-scrollbars",
                                 "--user-data-dir=" + str(Path(tmp) / "profile"),
                                 "--window-size=1080,1920", "--virtual-time-budget=15000",
                                 "--screenshot=" + str(ROOT / "data/preview.png"),
                                 "--dump-dom", page.as_uri()], capture_output=True, timeout=45)
        dom = result.stdout.decode("utf-8", errors="replace")
        match = re.search(r'<pre id="test-result"[^>]*>(.*?)</pre>', dom, re.S)
        assert match, dom[-4000:] + result.stderr.decode("utf-8", errors="replace")[-2000:]
        errors = json.loads(html.unescape(match[1]))
        assert not errors, errors
    if script == ANNOUNCER_CHECK:
        print("PASS: A16 baseline/delta/queue/presets/hot reload/geometry/marquee/reduced-motion")
        return
    print("PASS: 问答追加/提示行/思考中/揭晓三阶段/U4 长文本自动滚动/"
          "贡献链/换题清空/调试宽度/长流可滚/无补题文案；data/preview.png")


if __name__ == "__main__":
    main()
