/* 竖屏 AI 海龟汤直播 —— 前端 (两段式布局)
 *
 * 上半部: #puzzle  谜面大字(固定) + #reveal 揭晓覆盖层
 * 下半部: #qa      问答流, 持续向上滚动
 *
 * 复用旧版的: WS 管道 / fit() 舞台缩放 / 自动滚动兜底 / layout()
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
    revealLabel: $("reveal-label"),
    revealCore: $("reveal-core"), revealWho: $("reveal-who"),
    revealContrib: $("reveal-contrib"),
    revealContribTitle: $("reveal-contrib-title"),
    revealContribList: $("reveal-contrib-list"),
    top: $("top"), bottom: $("bottom"), content: $("content"),
    qa: $("qa"), qaBody: $("qa-body"),
    thinking: $("thinking"), hintbar: $("hintbar"), prompt: $("prompt"),
    stats: $("stats"), toast: $("toast"),
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
  //
  // ⚠️ 可用高度必须**按 DOM 实际高度算**, 不能像早先那样写死常数。
  //
  // U1 起**只对 #reveal-body(完整解释)缩字号**:
  //   - #reveal-core 是视觉第一层, 字号由 CSS 定死 64px, **绝不改**。
  //     早先两者共用一个正文块, fitReveal 会一路缩到 22px —— 那正是
  //     "原来如此那一下看不清"的根因。
  //   - 贡献链字号小且最多 1~2 条(completion 合同上限 2 个 fact),
  //     缩它只会让它不可读。
  // 所以这里只留一个可压缩区域, 下限 34px。
  const REVEAL_EXPLAIN_BASE = 34;
  const REVEAL_EXPLAIN_MIN = 34;
  let lastRevealFitKey = null;
  function fitReveal(fullText, coreText) {
    // 只在"会影响布局的东西"变化时重算 —— 每次推送都量 DOM 会很贵,
    // 而这函数在 4Hz 的推送里被调用。
    const key = [fullText || "", coreText || "",
                 el.revealContrib.classList.contains("hidden"),
                 el.revealBody.classList.contains("hidden")].join("");
    if (lastRevealFitKey === key) return;
    lastRevealFitKey = key;
    if (el.revealBody.classList.contains("hidden")) return;
    const rs = getComputedStyle(el.reveal);
    const padding = (parseFloat(rs.paddingTop) || 0)
                  + (parseFloat(rs.paddingBottom) || 0);
    const gap = parseFloat(rs.rowGap || rs.gap || "0") || 0;
    // 固定高度的块: 标题 + 核心答案 + "谁补齐的" + 贡献链(可见时) + 倒计时
    let fixed = el.revealLabel.offsetHeight + el.revealCore.offsetHeight;
    const whoHidden = el.revealWho.classList.contains("hidden");
    if (!whoHidden) fixed += el.revealWho.offsetHeight;
    const contribHidden = el.revealContrib.classList.contains("hidden");
    if (!contribHidden) fixed += el.revealContrib.offsetHeight;
    fixed += el.revealNext.offsetHeight;
    // flex 的 gap 出现在**每个**可见块之间。
    const visibleBlocks = 3 + (whoHidden ? 0 : 1) + (contribHidden ? 0 : 1);
    const avail = el.reveal.clientHeight - padding - fixed
                - gap * (visibleBlocks - 1);
    if (!(avail > 0)) return;          // 布局还没稳, 别把字号缩成 0
    let fs = REVEAL_EXPLAIN_BASE;
    el.revealBody.style.fontSize = fs + "px";
    for (let i = 0; i < 14 && fs > REVEAL_EXPLAIN_MIN; i++) {
      if (el.revealBody.scrollHeight <= avail) break;
      fs -= 2;
      el.revealBody.style.fontSize = fs + "px";
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

  // 揭晓贡献链: "这题大家是怎么一起推出来的"。
  //
  // 服务端下发的每行只有 qid/user_name/text/verdict/is_final —— 内部
  // fact ID 在服务端就已经被筛掉了, 前端拿不到也不该拿到。
  //
  // ⚠️ 全部用 textContent / createElement 构造。用户名和提问都来自
  // 观众, 任何 innerHTML 拼接都是注入口子。
  function renderRevealContributors(s) {
    const rows = Array.isArray(s.reveal_contributors)
               ? s.reveal_contributors : [];
    if (!rows.length) {
      // 没内容 -> 整块隐藏并**清空**。清空很重要: 换题后如果不擦,
      // 上一题的名字会残留到下一题的揭晓里。
      el.revealContrib.classList.add("hidden");
      el.revealContribTitle.textContent = "";
      el.revealContribList.textContent = "";
      return;
    }
    el.revealContribTitle.textContent =
      s.solved ? "共同解谜" : "大家已经推到这里";
    const box = document.createDocumentFragment();
    rows.forEach(function (r) {
      const row = document.createElement("div");
      row.className = "reveal-contrib-row";
      const who = document.createElement("span");
      who.className = "who";
      who.textContent = (r.user_name || "") + "：";
      row.appendChild(who);
      row.appendChild(document.createTextNode(r.text || ""));
      if (r.verdict) {
        const vd = document.createElement("span");
        vd.className = "vd";
        vd.textContent = "→ " + r.verdict;
        row.appendChild(vd);
      }
      if (r.is_final) {
        const fin = document.createElement("span");
        fin.className = "final";
        fin.textContent = "✓ 最后线索";
        row.appendChild(fin);
      }
      box.appendChild(row);
    });
    el.revealContribList.textContent = "";
    el.revealContribList.appendChild(box);
    el.revealContrib.classList.remove("hidden");
  }

  // 揭晓工作区(U1)
  //
  // 分两段显示:
  //   0..reveal_core_focus_seconds  只显示核心答案(超大字号)
  //   之后(reveal_detail_visible)   追加完整解释 + 共同解谜
  //
  // `reveal_detail_visible` 由**服务端**算(见 engine.snapshot) —— 前端
  // 不自己计时, 否则刷新/重连后计时会从 0 重来, 与服务端不一致。
  function renderReveal(s) {
    const on = !!(s.revealed_answer && (s.phase === "revealed" || s.phase === "revealing"));
    el.reveal.classList.toggle("hidden", !on);
    // ⚠️ U1 起**不再**隐藏谜面: 揭晓工作区已从 #top 移出, 从 y=0 起用
    // padding-top 让开谜面区。谜面在上半部保持可见 —— 观众要对照着看
    // "原来谜面那句话是这个意思"。
    //
    // 但**下半部的问答工作区要让位**(任务书: 揭晓期间下半部整个给答案)。
    // 只视觉隐藏, DOM 与 Engine 数据都保留 —— 下一题直接恢复, 不需要
    // 重建任何东西。
    el.bottom.classList.toggle("hidden", on);
    if (!on) {
      // 揭晓层整体关掉时, 贡献链也跟着收起来 —— 否则下一题进入
      // revealing 之前会残留上一题的名字。
      renderRevealContributors({reveal_contributors: []});
      el.revealCore.textContent = "";
      el.revealWho.textContent = "";
      el.revealWho.classList.add("hidden");
      el.revealBody.textContent = "";
      lastRevealFitKey = null;
      return;
    }

    // ---- 核心答案: legacy 题没有 core_answer -> fallback 到完整谜底 ----
    const core = s.revealed_core_answer || s.revealed_full_answer
               || s.revealed_answer || "";
    const full = s.revealed_full_answer || s.revealed_answer || "";
    if (el.revealCore.textContent !== core) el.revealCore.textContent = core;

    // ---- "XX 补齐最后线索" —— 只在该题是 solved 时出现 ----
    const who = s.solved && s.solved_by ? (s.solved_by + " 补齐最后线索") : "";
    if (who) {
      el.revealWho.textContent = who;
      el.revealWho.classList.remove("hidden");
    } else {
      el.revealWho.textContent = "";
      el.revealWho.classList.add("hidden");
    }

    // ---- 完整解释: 只在细节可见、且确实与核心答案不同的时候显示 ----
    // 若 full === core(legacy 单段题), 显示两块就是同一句话出现两次。
    const showDetail = !!s.reveal_detail_visible && full && full !== core;
    el.revealBody.classList.toggle("hidden", !showDetail);
    if (showDetail && el.revealBody.textContent !== full) {
      el.revealBody.textContent = full;
      lastRevealFitKey = null;
    }
    // ---- 共同解谜: **也只在细节阶段**显示 ----
    // 前 core_focus 秒是核心答案独占的, 摆一屏名字会把它挤下去
    // (任务书: 0–15s 不要同时显示完整解释 / 完整贡献链)。
    if (showDetail) {
      renderRevealContributors(s);
    } else {
      renderRevealContributors({reveal_contributors: []});
    }
    // 核心/贡献链的显隐改变了可用高度 -> 重算解释区字号。
    fitReveal(full, core);

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

  // ================= 布局 =================
  // 上半(谜面)与下半(问答)的高度分配。谜面越长给越高, 但保底下半部空间。
  const TOP_MIN = 620, TOP_MAX = 1000, BOTTOM_MIN = 620;
  let lastTopH = -1;
  function layout() {
    // 整个舞台都可用 —— 飘屏弹幕已移除, 不再为它预留底部 240px。
    // 多出来的高度主要回给下半部 QA 区(上半部有 TOP_MAX=1000 封顶,
    // 不会被无限撑大)。
    const stageH = 1920;
    const avail = stageH;

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

    // ⚠️ 这里读 `el.puzzle.scrollHeight` —— 对 `display:none` 的元素它恒为
    // 0, 于是 `want=190` 会被钳到 TOP_MIN。早先 renderReveal 在揭晓时把
    // #puzzle 设为 hidden, 于是**每次进入揭晓上下分割都会跳到 620/1300**。
    // 当时因为揭晓层是 #top 内的绝对定位覆盖层所以看不出来; U1 把揭晓
    // 工作区移出 #top 之后, 那个跳动就会直接可见。
    // 现在 renderReveal **不再隐藏谜面**(谜面本就该在揭晓时保持可见),
    // 所以 scrollHeight 始终有效, 这个坑从源头消失了。
    // 下面这行是防御: 万一将来有人又去隐藏它, 至少不会算出 0。
    const puzzleH = el.puzzle.classList.contains("hidden")
                  ? Math.max(0, lastTopH - 190)      // 沿用上次测得的高度
                  : el.puzzle.scrollHeight;
    const want = puzzleH + 190;
    let height = Math.min(TOP_MAX, Math.max(TOP_MIN, want));
    height = Math.min(height, avail - BOTTOM_MIN);
    if (height !== lastTopH) {
      lastTopH = height;
      el.top.style.height = height + "px";
      el.bottom.style.top = height + "px";
      // U1: 揭晓工作区的 padding-top 跟着谜面区走 —— 它要把正文推到
      // 谜面下方。写死一个常数会在谜面长短变化时压到/远离谜面。
      el.content.style.setProperty("--top-h", height + "px");
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
