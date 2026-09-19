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
window.addEventListener("load", async () => {
  await document.fonts.ready;
  const errors = [];
  const check = (ok, message) => { if (!ok) errors.push(message); };

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
    // pushDanmaku 之后只剩 `renderDebug` 和 `layout()`。layout() 会设置
    // `#bottom.style.top`(按谜面高度算), 这是**唯一**能被外部观察、
    // 且必然发生在 pushDanmaku 之后的副作用。
    const bottom = document.getElementById("bottom");
    const topBefore = bottom.style.top;
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
    check(bottom.style.top !== topBefore,
          "B2: 带 danmaku 的 snapshot 之后 onState 必须跑完 —— "
          + "layout() 是最后一步, 它没执行说明中途被截断 "
          + "(#bottom.top " + topBefore + " -> " + bottom.style.top + ")");
    {
      const content = document.getElementById("content");
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

    // ④ 揭晓覆盖层
    send({phase: "revealed", qa_log: mkQa(4), qa_total: 4,
          revealed_answer: "多年前他遭遇海难，同伴给他喝的其实是同伴自己的肉。",
          solved: true, solved_by: "观众戊", next_puzzle_ms: 25000});
    check(!document.getElementById("reveal").classList.contains("hidden"),
          "揭晓层未显示");
    check(document.getElementById("reveal-body").textContent.includes("海难"),
          "揭晓内容缺失");
    check(document.getElementById("reveal-next").textContent.includes("25"),
          "下一题倒计时缺失");

    // ④.5 揭晓时谜面必须隐藏(否则两层文字叠在一起 = "字被遮挡")
    check(document.getElementById("puzzle").classList.contains("hidden"),
          "揭晓时谜面应隐藏, 避免与谜底叠字");
    check(parseFloat(getComputedStyle(document.getElementById("reveal-body")).fontSize) > 0,
          "谜底字号应有效");

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
    // 系统行(非 QA 阶段 #问题 的反馈)要能上屏
    send({phase: "setting", puzzle_index: 6, story_index: 6, puzzle: "",
          qa_log: [{qid: -1, user_name: "系统", text: "正在准备新题，谜面出现后再发 #问题。",
                    verdict: "", comment: "", kind: "system"}],
          qa_total: 0});
    const sysRows = [...document.querySelectorAll(".qa-row.kind-system")];
    check(sysRows.length === 1, "系统行应渲染 1 条, 实际 " + sysRows.length);
    check(sysRows.length && sysRows[0].textContent.includes("正在准备新题"),
          "系统行内容: " + (sysRows[0] && sysRows[0].textContent));
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
          solved: true, solved_by: "乙",
          reveal_contributors: mkContrib(true), next_puzzle_ms: 20000});
    const cb = document.getElementById("reveal-contrib");
    check(!cb.classList.contains("hidden"), "有贡献链时应显示该块");
    check(document.getElementById("reveal-contrib-title").textContent
          === "共同解谜",
          "solved=true 标题应为'共同解谜', 实际 "
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
          revealed_answer: "谜底。", solved: false,
          reveal_contributors: [], next_puzzle_ms: 20000});
    check(cb.classList.contains("hidden"),
          "无贡献时应隐藏整块");
    check(document.getElementById("reveal-contrib-list").children.length === 0,
          "隐藏时列表应清空");

    // ⑫.4 换题必须擦掉上一题的名字(残留是最难发现的那类 bug)
    send({phase: "revealed", puzzle_index: 20, story_index: 20,
          revealed_answer: "谜底。", solved: true,
          reveal_contributors: mkContrib(true), next_puzzle_ms: 20000});
    check(document.getElementById("reveal-contrib-list").children.length === 2,
          "先确保上一题贡献链在");
    send({phase: "qa", puzzle_index: 21, story_index: 21,
          puzzle: "新的一道题。", revealed_answer: "", qa_log: [], qa_total: 0});
    check(document.getElementById("reveal-contrib-list").children.length === 0,
          "换题后贡献链列表必须清空(不能残留上一题名字)");
    check(document.getElementById("reveal-contrib-title").textContent === "",
          "换题后标题也必须清空");

    // ⑬ 长谜底 + 两条贡献链 + 倒计时: 三者不得互相覆盖
    //
    // 这是 fitReveal 从"写死常数"改成"按 DOM 实算"的那条回归。
    // 写死常数时, 贡献链一出现就把可用高度算多了, 正文会压到贡献链上。
    const longReveal = "这是一段刻意写得很长的谜底，用来把揭晓层的可用高度"
      + "压到很紧，从而检验字号自适应与贡献链的布局是否会互相覆盖。"
      + "再补一些字，确保它必然超过一屏的容量，逼迫 fitReveal 真正去缩字号。"
      + "继续补一些字，继续补一些字，继续补一些字，继续补一些字。";
    send({phase: "revealed", puzzle_index: 22, story_index: 22,
          revealed_answer: longReveal, solved: true, solved_by: "乙",
          reveal_contributors: mkContrib(true), next_puzzle_ms: 18000});
    {
      const body = document.getElementById("reveal-body");
      const contrib = document.getElementById("reveal-contrib");
      const next = document.getElementById("reveal-next");
      const panel = document.getElementById("reveal").getBoundingClientRect();
      const br = body.getBoundingClientRect();
      const cr = contrib.getBoundingClientRect();
      const nr = next.getBoundingClientRect();
      check(cr.height > 0, "贡献链应可见且有高度: " + cr.height);
      check(br.height > 0, "正文应有高度: " + br.height);
      // 正文底 <= 贡献链顶(允许 1px 取整)
      check(br.bottom <= cr.top + 1,
            "正文不得压到贡献链上 (body.bottom=" + br.bottom.toFixed(1)
            + " contrib.top=" + cr.top.toFixed(1) + ")");
      // 贡献链底 <= 倒计时顶
      check(cr.bottom <= nr.top + 1,
            "贡献链不得压到倒计时上 (contrib.bottom=" + cr.bottom.toFixed(1)
            + " next.top=" + nr.top.toFixed(1) + ")");
      // 全部落在揭晓面板内
      check(br.top >= panel.top - 1 && nr.bottom <= panel.bottom + 1,
            "正文与倒计时都应落在揭晓面板内 (panel="
            + panel.top.toFixed(1) + ".." + panel.bottom.toFixed(1)
            + " body=" + br.top.toFixed(1) + " next.bottom="
            + nr.bottom.toFixed(1) + ")");
      const fs = parseFloat(getComputedStyle(body).fontSize);
      check(fs >= 22, "字号不应缩到下限以下: " + fs);
    }

    // ⑭ 贡献链绝不做 HTML 拼接
    //
    // 用户名与提问都来自观众。贡献链是**新增的**一处把观众字符串放上
    // 大屏的地方, 必须和问答流一样走 textContent —— 否则一个叫
    // `<img onerror=...>` 的观众就能在公屏上执行脚本。
    send({phase: "revealed", puzzle_index: 23, story_index: 23,
          revealed_answer: "谜底。", solved: true,
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
  } catch (e) { errors.push(e.stack); }
  const result = document.createElement("pre");
  result.id = "test-result"; result.hidden = true;
  result.textContent = JSON.stringify(errors);
  document.body.appendChild(result);
});
'''


def main():
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
    source = (ROOT / "web/index.html").read_text(encoding="utf-8")
    source = source.replace('href="/style.css"', 'href="' + (ROOT / "web/style.css").as_uri() + '"')
    source = source.replace('<script src="/app.js"></script>',
                            "<script>" + CHECK + "</script><script src=\"" +
                            (ROOT / "web/app.js").as_uri() + '\"></script>')
    # data/ 在干净 checkout 上可能不存在(它只靠两个 .md 撑着)。
    (ROOT / "data").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT / "data") as tmp:
        page = Path(tmp) / "test.html"
        page.write_text(source, encoding="utf-8")
        result = subprocess.run([str(CHROME), "--headless=new", "--disable-gpu",
                                 "--no-first-run", "--hide-scrollbars",
                                 "--user-data-dir=" + str(Path(tmp) / "profile"),
                                 "--window-size=1080,1920", "--virtual-time-budget=7000",
                                 "--screenshot=" + str(ROOT / "data/preview.png"),
                                 "--dump-dom", page.as_uri()], capture_output=True, timeout=45)
        dom = result.stdout.decode("utf-8", errors="replace")
        match = re.search(r'<pre id="test-result"[^>]*>(.*?)</pre>', dom, re.S)
        assert match, result.stderr.decode("utf-8", errors="replace")[-2000:]
        errors = json.loads(html.unescape(match[1]))
        assert not errors, errors
    print("PASS: 问答追加/提示行/思考中/揭晓层/贡献链/换题清空/不截断/调试宽度/长流可滚；data/preview.png")


if __name__ == "__main__":
    main()
