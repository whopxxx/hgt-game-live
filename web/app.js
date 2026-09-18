/* 竖屏 AI 海龟汤直播 —— 前端 (两段式布局)
 *
 * 上半部: #puzzle  谜面大字(固定) + #reveal 揭晓覆盖层
 * 下半部: #qa      问答流, 持续向上滚动
 *
 * 复用旧版的: WS 管道 / fit() 舞台缩放 / 弹幕轨道 / 自动滚动兜底 / layout()
 */

(function () {
  "use strict";

  const STAGE_W = 1080, STAGE_H = 1920;

  const $ = (id) => document.getElementById(id);
  const el = {
    stage: $("stage"),
    puzzleIndex: $("puzzle-index"), puzzleElapsed: $("puzzle-elapsed"),
    puzzleTimer: $("puzzle-timer"),
    puzzle: $("puzzle"),
    reveal: $("reveal"), revealBody: $("reveal-body"), revealNext: $("reveal-next"),
    top: $("top"), bottom: $("bottom"),
    qa: $("qa"), qaBody: $("qa-body"),
    thinking: $("thinking"), hintbar: $("hintbar"), prompt: $("prompt"),
    stats: $("stats"), toast: $("toast"),
    danmaku: $("danmaku"),
    debug: $("debug"), debugBody: $("debug-body"), conn: $("conn"),
  };

  const params = new URLSearchParams(location.search);
  let showDebug = params.get("debug") === "1";
  function applyDebug() {
    el.debug.classList.toggle("hidden", !showDebug);
    el.stage.classList.toggle("debug-on", showDebug);
  }
  applyDebug();

  // ---------------- 舞台缩放 ----------------
  function fit() {
    const s = Math.min(window.innerWidth / STAGE_W, window.innerHeight / STAGE_H);
    el.stage.style.transform = "scale(" + s + ")";
  }
  window.addEventListener("resize", fit);
  fit();

  // ================= 上半部: 谜面 =================
  let lastPuzzleIndex = undefined;

  function fmtElapsed(ms) {
    if (ms == null) return "";
    const s = Math.floor(ms / 1000);
    return "已进行 " + String(Math.floor(s / 60)).padStart(2, "0") + ":"
      + String(s % 60).padStart(2, "0");
  }

  // 时间轴倒计时: 服务端给"距下一个事件还有多少毫秒" + 事件名,
  // 前端本地每秒插值(不然 4Hz 推送下秒数会跳)。
  let timerAnchor = null;   // {end: performance.now()+ms, label: "距第2条提示"}
  function renderTimer(s) {
    if (s.phase !== "qa" || s.next_event_ms == null) {
      timerAnchor = null;
      el.puzzleTimer.classList.add("hidden");
      return;
    }
    el.puzzleTimer.classList.remove("hidden");
    timerAnchor = {
      end: performance.now() + s.next_event_ms,
      label: s.next_event_label || "距下一个提示",
      soon: s.next_event_kind === "reveal",
    };
  }
  (function timerLoop() {
    if (timerAnchor) {
      const left = Math.max(0, timerAnchor.end - performance.now());
      const sec = Math.ceil(left / 1000);
      el.puzzleTimer.textContent = timerAnchor.label + " "
        + String(Math.floor(sec / 60)).padStart(2, "0") + ":"
        + String(sec % 60).padStart(2, "0");
      el.puzzleTimer.classList.toggle("soon", timerAnchor.soon || sec <= 10);
    }
    requestAnimationFrame(timerLoop);
  })();

  function renderPuzzle(s) {
    if (s.puzzle_index !== lastPuzzleIndex) {
      const isFirst = lastPuzzleIndex === undefined;
      lastPuzzleIndex = s.puzzle_index;
      // 新题: 清空问答流, 谜面淡入, 弹一条"第 N 题"提示
      if (!isFirst) {
        el.qaBody.innerHTML = "";
        lastRenderedSig = null;
        el.puzzle.classList.remove("newpuzzle");
        void el.puzzle.offsetWidth;          // 强制重排, 让动画能重放
        el.puzzle.classList.add("newpuzzle");
        showToast("第 " + s.puzzle_index + " 题");
      }
    }
    if (s.puzzle_index) {
      el.puzzleIndex.textContent = "第 " + s.puzzle_index + " 题";
    }
    const txt = s.puzzle || "";
    if (el.puzzle.textContent !== txt) el.puzzle.textContent = txt;
    el.puzzleElapsed.textContent = s.phase === "qa" ? fmtElapsed(s.puzzle_elapsed_ms) : "";
  }

  // 揭晓层字号自适应: 42px 基准, 内容超高就缩, 下限 22px。
  // 量的是**揭晓层自己的可用高度**(它的 padding 与 #top 不同)。
  let lastRevealText = null;
  function fitReveal(text) {
    if (lastRevealText === text) return;
    lastRevealText = text;
    // 可用高度 = 揭晓层高度 - 上下 padding - 标题 - 倒计时行
    const avail = el.reveal.clientHeight - 150 - 90 - 90;
    let fs = 42;
    for (let i = 0; i < 12 && fs > 22; i++) {
      el.revealBody.style.fontSize = fs + "px";
      if (el.revealBody.scrollHeight <= avail) break;
      fs -= 2;
    }
  }

  // 换题过渡提示(短暂显示后淡出)
  let toastTimer = null;
  function showToast(text) {
    el.toast.textContent = text;
    el.toast.classList.remove("hidden");
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.toast.classList.add("hidden"); }, 1800);
  }

  // 揭晓覆盖层
  function renderReveal(s) {
    const on = !!(s.revealed_answer && (s.phase === "revealed" || s.phase === "revealing"));
    el.reveal.classList.toggle("hidden", !on);
    // 揭晓时**隐藏谜面** —— 否则揭晓层是半透明的, 两层文字会叠在一起
    // (就是"揭晓时字被遮挡"的根因)。
    el.puzzle.classList.toggle("hidden", on);
    if (!on) return;
    if (el.revealBody.textContent !== s.revealed_answer) {
      el.revealBody.textContent = s.revealed_answer;
      lastRevealText = null;              // 让 fitReveal 重新算字号
      fitReveal(s.revealed_answer);
    }
    if (s.next_puzzle_ms != null) {
      el.revealNext.textContent = Math.ceil(s.next_puzzle_ms / 1000) + " 秒后开启新谜题";
    } else {
      el.revealNext.textContent = "";
    }
  }

  // ================= 下半部: 问答流 =================
  // 服务端推的是**尾部窗口**(最近 N 条)。这里做增量追加:
  //   - 用每条记录自己的稳定 key(qid) 判断"这条渲染过没有"
  //   - DOM 里最多保留 MAX_ROWS 行, 超了就从头删
  // 早先的写法用 qa_total 当全局游标, 一旦服务端窗口截断, 游标就对不上,
  // 表现为"到一定数量后不再更新"。
  const MAX_ROWS = 60;          // DOM 里最多留多少行(防卡)
  let lastQaPuzzle = undefined; // 用于检测"换题"
  let lastRenderedSig = null;   // 上次渲染内容的指纹, 没变就不动 DOM
  // 连续「无关」只显示最近 KEEP_IRRELEVANT 条, 其余折叠成一行小字。
  // 观众问偏了是常事, 但一屏全是"无关"太难看, 也会把有用的问答顶走。
  const KEEP_IRRELEVANT = 2;

  function rowKey(r) {
    // 问答有唯一 qid; 提示/重述用 kind+文本(会被替换, 所以同一时刻只有一条)
    return r.qid >= 0 ? "q" + r.qid : r.kind + "|" + (r.text || "");
  }

  function renderQa(s) {
    const log = s.qa_log || [];
    // 换题 -> 清空
    const pz = s.story_index;
    if (pz !== undefined && pz !== lastQaPuzzle) {
      lastQaPuzzle = pz;
      el.qaBody.innerHTML = "";
      lastRenderedSig = null;
    }
    if (!log.length) return;

    // 整个尾部窗口重画。窗口 ≤40 行, 重画开销可忽略, 但换来简单与正确
    // ——增量追加做不了"折叠连续无关"(新行要知道前面有多少条同类)。
    const sig = log.map(rowKey).join(",");
    if (sig === lastRenderedSig) return;   // 没变化, 不动 DOM
    lastRenderedSig = sig;

    el.qaBody.innerHTML = "";
    let pendingIrrelevant = 0;             // 待折叠的连续无关计数
    for (let i = 0; i < log.length; i++) {
      const r = log[i];
      const isIrrelevant = r.kind === "qa" && r.verdict === "无关";
      if (isIrrelevant) {
        pendingIrrelevant++;
        // 先攒着; 等到这一串无关结束(或到达窗口末尾)再决定显示几条
        const next = log[i + 1];
        const nextIsIrrelevant = next && next.kind === "qa" && next.verdict === "无关";
        if (nextIsIrrelevant) continue;
        // 这串无关结束了: 折叠前面多余的, 只保留最后 KEEP_IRRELEVANT 条。
        // 注意起点必须是**这一串的开头**(i - pendingIrrelevant + 1),
        // 早先错写成 i - KEEP_IRRELEVANT + 1 —— 当这串只有 1 条时,
        // 起点会退到上一条(非无关的), 把它**重复画一遍**。
        const start = i - pendingIrrelevant + 1;
        const keepFrom = Math.max(start, i - KEEP_IRRELEVANT + 1);
        const drop = keepFrom - start;
        if (drop > 0) el.qaBody.appendChild(buildFoldRow(drop));
        for (let j = keepFrom; j <= i; j++) {
          el.qaBody.appendChild(buildRow(log[j]));
        }
        pendingIrrelevant = 0;
        continue;
      }
      el.qaBody.appendChild(buildRow(r));
    }

    const rows = el.qaBody.children;
    while (rows.length > MAX_ROWS) el.qaBody.removeChild(rows[0]);
    el.qa.scrollTop = el.qa.scrollHeight;
    requestAnimationFrame(function () { el.qa.scrollTop = el.qa.scrollHeight; });
  }

  function buildFoldRow(n) {
    const row = document.createElement("div");
    row.className = "qa-row kind-fold";
    const q = document.createElement("div");
    q.className = "q";
    q.textContent = "…（前 " + n + " 条与谜底无关，已折叠）";
    row.appendChild(q);
    return row;
  }

  function buildRow(r) {
    const row = document.createElement("div");
    row.className = "qa-row" + (r.kind && r.kind !== "qa" ? " kind-" + r.kind : "");
    const q = document.createElement("div");
    q.className = "q";
    if (r.kind === "hint") {
      q.textContent = "💡 " + r.text;
      row.appendChild(q);
    } else if (r.kind === "nudge") {
      q.textContent = r.text;
      row.appendChild(q);
    } else if (r.kind === "system") {
      // 系统行: 非 QA 阶段观众发 #问题 时的确定性反馈(方案 §8)。
      // 不显示观众名前缀 —— 它是对**这个阶段**的说明, 不是对某个人的回答。
      q.textContent = r.text;
      row.appendChild(q);
    } else {
      // 提问与裁决**同一行**: "观众甲：他瞎了吗  → 不是"
      const who = document.createElement("span");
      who.className = "who";
      who.textContent = r.user_name + "：";
      q.appendChild(who);
      q.appendChild(document.createTextNode(r.text));
      if (r.verdict) {
        const v = document.createElement("span");
        v.className = "verdict v-" + r.verdict;
        v.textContent = r.verdict;
        q.appendChild(v);
      }
      row.appendChild(q);
      if (r.comment) {
        const c = document.createElement("div");
        c.className = "comment";
        c.textContent = r.comment;
        row.appendChild(c);
      }
    }
    return row;
  }

  function renderThinking(s) {
    el.thinking.classList.toggle("hidden", !(s.pending_count > 0));
    if (s.pending_count > 0) {
      el.thinking.textContent = "AI 正在思考… (" + s.pending_count + ")";
    }
  }

  // 提示只走问答流(作为 kind-hint 行), 不再单独用 hintbar 重复显示一遍 ——
  // 之前两处都显示同一条, 看起来就像"提示内容重复"。
  function renderHint(s) {
    el.hintbar.classList.add("hidden");
  }

  // 常驻互动提示: **任何阶段都显示**, 只是文案不同。
  //
  // 早先非 QA 阶段整个隐藏(`if (!inQA) return`), 于是观众在这个阶段
  // 既没有操作指引、打字又收不到反馈 -> 看起来像卡死。出题要 30-45s,
  // 那段空窗正是最需要"我在干活"信号的时候。
  //
  // 提示(💡)已经在问答流里作为一行显示了, 这里**不重复**。
  function renderPrompt(s) {
    el.prompt.classList.remove("hidden");
    if (s.phase === "qa") {
      el.prompt.innerHTML =
        "发送 <b>#你的问题</b> 向我提问，猜中谜底我就揭晓";
    } else if (s.phase === "setting") {
      el.prompt.textContent = "AI 正在出题，请稍候…";
    } else if (s.phase === "revealing" || s.phase === "revealed") {
      el.prompt.textContent = "本题已结束，稍候将开启新谜题…";
    } else {
      el.prompt.textContent = "直播准备中…";
    }
  }

  function renderStats(s) {
    const st = s.stats || {};
    const parts = [];
    if (st.questions) parts.push("本题已问 <b>" + st.questions + "</b>");
    if (st.answered) parts.push("已答 <b>" + st.answered + "</b>");
    if (st.viewers_seen) parts.push("观众 <b>" + st.viewers_seen + "</b>");
    if (st.solved) parts.push("累计猜中 <b>" + st.solved + "</b>");
    el.stats.innerHTML = parts.join("　·　");
  }

  // ================= 弹幕 =================
  const LANES = 5, LANE_H = 44;
  let dmSeq = 0;                     // 轨道轮换序号
  // 同一轨道上"还没走远"的弹幕数 -> 每条再往右错开 STAGGER_PX,
  // 否则同一批涌进来的弹幕会叠在一起(重连重放时最明显)。
  const STAGGER_PX = 260;
  // 弹幕的**恒定**移动速度(px/s)。原来用固定时长(9~12s) + 变化的位移,
  // 位移一涨速度就跟着涨 —— 见 renderDanmaku 里的说明。
  // 110 ≈ 原首条弹幕的速度(舞台宽约 1080 / 约 10 秒), 刻意取中值,
  // 保证这次改动不改变正常情况下的观感。
  const DM_SPEED = 110;
  const batchSlots = new Array(LANES).fill(0);
  let lastDmAt = 0;

  // 服务端每次推的是**最近 N 条的窗口**(4Hz)。必须只渲染"没见过的"
  // —— 用服务端给的**单调递增 seq** 判断。
  //
  // 早先按"人+内容、相邻 2 条内算重复"判重: 每次推送整个窗口时序号全在涨,
  // 于是 40 条旧弹幕**全部重新飞一遍** —— 就是"发一条消息后弹幕乱飞"的根因。
  let lastDmSeq = 0;

  function pushDanmaku(list) {
    if (!list || !list.length) return;

    // 先挑出**真正没见过的** seq, 并推进水位。
    // 服务端 seq 由引擎在锁内 `+1` 生成、窗口按序切 `[-40:]`, 所以它是
    // **连续且升序**的 —— `seq > lastDmSeq` 就等价于"没见过", 不会漏。
    let top = lastDmSeq;
    const fresh = [];
    for (let i = 0; i < list.length; i++) {
      const seq = list[i].seq || 0;
      if (seq > top) top = seq;
      if (seq > lastDmSeq) fresh.push(list[i]);
    }
    lastDmSeq = top;

    // 关键: **没有新弹幕就不要碰批次的时钟**。
    //
    // 曾经的写法是无条件 `lastDmAt = now`, 而服务端在房间有人说过话之后,
    // 每个 snapshot 都会带上最近 40 条 —— 即使这 1.2 秒里**根本没人说话**,
    // 这个函数仍以约 4Hz 被调用, `lastDmAt` 一直被刷新, 下面那句
    // `batchSlots.fill(0)` **永远不执行**。
    // 于是 batchSlots 单调上涨 -> 位移越来越长而时长固定 -> **越播越快**。
    if (!fresh.length) return;

    const now = performance.now();
    if (now - lastDmAt > 1200) batchSlots.fill(0);
    lastDmAt = now;

    for (let i = 0; i < fresh.length; i++) renderDanmaku(fresh[i]);
  }

  function renderDanmaku(d) {
    const node = document.createElement("div");
    node.className = "dm" + (d.is_command ? " cmd" : "");
    node.textContent = d.user_name + "：" + d.content;
    const lane = (dmSeq++) % LANES;
    node.style.top = lane * LANE_H + 4 + "px";
    // 同一批进来的弹幕(重连重放时会一次涌进十几条)不能从同一个 x 出发,
    // 否则会**完全叠在一起**, 看起来像"没显示"。
    // 给同一轨道上还没走远的弹幕再错开一段。
    const echo = batchSlots[lane] || 0;
    node.style.left = (STAGE_W + echo) + "px";
    batchSlots[lane] = echo + STAGGER_PX;
    el.danmaku.appendChild(node);
    // ---- 固定**速度**, 不是固定时长 ----
    //
    // 曾经是 `dur = 9 + random*3`(固定时长), 而位移里含 `echo` ——
    // 于是 echo 一涨, 同样的 9~12 秒要跑更远的路, 弹幕越飞越快。
    //
    // 改成按距离算时长, 速度才与 echo 无关。
    // DM_SPEED 取的是原来**首条弹幕**的速度: 舞台宽约 1080、时长 9~12 秒
    // -> 约 90~120 px/s, 取中值 110。**刻意不趁机调快** —— 这次只修 bug,
    // 不混入观感调整。
    const w = node.offsetWidth + echo;
    const distance = STAGE_W + w;
    const dur = distance / DM_SPEED;
    // 测试探针: 把这一条的位移/时长暴露出来, 供离线用例断言
    // "速度与 echo 无关"。**只读导出**, 不影响渲染逻辑。
    node.dataset.dmDist = distance;
    node.dataset.dmDur = dur;
    const t0 = performance.now();
    (function step(t) {
      const p = (t - t0) / (dur * 1000);
      if (p >= 1) { node.remove(); return; }
      node.style.transform = "translateX(" + (-(STAGE_W + w) * p) + "px)";
      requestAnimationFrame(step);
    })(performance.now());
  }

  // ================= 布局 =================
  // 上半(谜面)与下半(问答)的高度分配。谜面越长给越高, 但保底下半部空间。
  const TOP_MIN = 620, TOP_MAX = 1000, BOTTOM_MIN = 620;
  let lastTopH = -1;
  function layout() {
    const stageH = 1920, danmakuH = 240;
    const avail = stageH - danmakuH;                 // 1680

    // 谜面过长时按内容自动缩小字号(而不是溢出压到问答流上)。
    // 58px 是基准; 内容越高缩得越小, 下限 30px。
    const maxPuzzleH = TOP_MAX - 260;                // 谜面可用高度
    let fs = 58;
    for (let i = 0; i < 12 && fs > 30; i++) {
      if (el.puzzle.style.fontSize === fs + "px"
          && el.puzzle.scrollHeight <= maxPuzzleH) break;
      el.puzzle.style.fontSize = fs + "px";
      if (el.puzzle.scrollHeight <= maxPuzzleH) break;
      fs -= 3;
    }

    const want = el.puzzle.scrollHeight + 190;
    let height = Math.min(TOP_MAX, Math.max(TOP_MIN, want));
    height = Math.min(height, avail - BOTTOM_MIN);
    if (height !== lastTopH) {
      lastTopH = height;
      el.top.style.height = height + "px";
      el.bottom.style.top = height + "px";
    }
  }
  new ResizeObserver(layout).observe(el.puzzle);

  // ================= 调试面板 =================
  function renderDebug(s) {
    if (!showDebug) return;
    const d = s.debug || {}, st = s.stats || {};
    const L = [];
    L.push("<span class='k'>阶段</span> " + s.phase);
    L.push("<span class='k'>题号</span> " + s.puzzle_index + " (总 " + (d.puzzles_total || 0) + ")");
    L.push("<span class='k'>排队/在途</span> " + s.pending_count);
    L.push("<span class='k'>提示</span> " + (s.hint_count || 0));
    if (s.next_puzzle_ms != null) L.push("<span class='k'>下一题</span> " + (s.next_puzzle_ms / 1000).toFixed(1) + "s");
    L.push("<span class='k'>发言观众</span> " + (st.viewers_seen || 0));
    L.push("<span class='k'>丢弃</span> " + (st.dropped || 0));
    L.push("<span class='k'>输入源</span> " + (d.source || "-"));
    L.push("<span class='k'>重连</span> " + (d.reconnects || 0));
    L.push("<span class='k'>请求模型</span> " + (d.model_requested || "-"));
    L.push("<span class='k'>实际模型</span> "
      + (d.model && d.model !== d.model_requested
          ? "<span class='err'>" + d.model + "</span>" : (d.model || "-")));
    if (d.last_usage) L.push("<span class='k'>tokens</span> in " + d.last_usage.input_tokens + " / out " + d.last_usage.output_tokens);
    if (d.last_error) L.push("<span class='err'>错误 " + String(d.last_error).slice(0, 130) + "</span>");
    L.push(""); L.push("<span class='k'>问答统计</span>");
    (s.qa_log || []).slice(-12).forEach(function (r) {
      L.push("  [" + r.qid + "] " + r.user_name + " " + (r.verdict || "-"));
    });
    el.debugBody.innerHTML = L.join("\n");
  }

  // ================= WebSocket =================
  let ws = null, retry = 800;
  function connect() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(proto + "//" + location.host + "/ws");
    ws.onopen = function () { retry = 800; el.conn.classList.add("hidden"); };
    ws.onmessage = function (ev) {
      try { onState(JSON.parse(ev.data)); } catch (e) {}
    };
    ws.onclose = function () {
      el.conn.classList.remove("hidden");
      setTimeout(connect, retry);
      retry = Math.min(retry * 1.6, 8000);
    };
    ws.onerror = function () { try { ws.close(); } catch (e) {} };
  }

  function onState(s) {
    renderPuzzle(s);
    renderTimer(s);
    renderQa(s);
    renderReveal(s);
    renderThinking(s);
    renderHint(s);
    renderPrompt(s);
    renderStats(s);
    pushDanmaku(s.danmaku);
    renderDebug(s);
    layout();
  }

  document.addEventListener("keydown", function (e) {
    if (e.key === "d" || e.key === "D") {
      showDebug = !showDebug;
      applyDebug();
    }
  });

  connect();
})();
