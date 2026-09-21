/* 竖屏 AI 海龟汤直播 —— 前端 (两段式布局)
 *
 * 上半部: #top / #puzzle  谜面大字(固定)
 * 下半部: #bottom         问答流; 揭晓时由 #reveal 接替**同一个矩形**
 *                         (两者共用 --workspace-top, 见 C2)
 *
 * 复用旧版的: WS 管道 / fit() 舞台缩放 / 自动滚动兜底 / layout()
 */

(function () {
  "use strict";

  const STAGE_W = 1080, STAGE_H = 1920;

  const $ = (id) => document.getElementById(id);
  // 术语只改**本文件写死的固定 UI 标签**(见 Issue #16 "前端术语统一"):
  // 汤面 / 汤底 / 本场猜汤榜。
  //
  // 刻意**不做**对任意快照文本的 replaceAll —— `next_event_label`、
  // `kind="system"` 行、贡献链原话都可能包含 "谜底" 字样, 而那是**内容**,
  // 不是标签。全局替换会把观众/系统原文改写掉(例如把贡献链里的原话
  // "包含谜底一词" 改成 "包含汤底一词"), 那是改变产品语义, 不是统一术语。
  // 服务端固定文案另有其源(engine._ACK_BY_PHASE / _NUDGES / notice),
  // 本次不动, 已在 PR 中说明。
  const el = {
    stage: $("stage"),
    puzzleIndex: $("puzzle-index"), puzzleElapsed: $("puzzle-elapsed"),
    puzzleTimer: $("puzzle-timer"),
    puzzleViewport: $("puzzle-viewport"), puzzle: $("puzzle"),
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

  // 同一份可取消控制器服务谜面与完整解释。key 不变时什么也不做，
  // 所以 4Hz snapshot 不会把阅读位置反复拉回顶部。
  const SCROLL_TIMING = Object.assign({
    topHoldMs: 3000, bottomHoldMs: 4500, speedPxPerSec: 26,
  }, window.__AUTO_SCROLL_TIMING__ || {});

  class AutoScroller {
    constructor(viewport) {
      this.viewport = viewport;
      this.key = null;
      this.generation = 0;
      this.timer = null;
      this.loop = false;
    }

    update(key, active, loop) {
      if (!active) {
        this.cancel();
        return;
      }
      if (this.key === key) return;
      this.cancel();
      this.key = key;
      this.loop = loop;
      const generation = this.generation;
      // 让本轮 render + layout 完成后再量真实 overflow。
      this.timer = setTimeout(() => this.start(generation), 0);
    }

    cancel() {
      this.generation++;
      if (this.timer != null) {
        clearTimeout(this.timer);
        clearInterval(this.timer);
      }
      this.timer = null;
      this.key = null;
      this.viewport.scrollTop = 0;
      this.viewport.removeAttribute("data-auto-scroll");
      this.viewport.removeAttribute("data-scroll-state");
    }

    start(generation) {
      if (generation !== this.generation) return;
      const max = Math.max(0, this.viewport.scrollHeight
                             - this.viewport.clientHeight);
      if (max <= 1) {
        this.viewport.setAttribute("data-scroll-state", "static");
        return;
      }
      this.viewport.setAttribute("data-auto-scroll", "true");
      this.viewport.setAttribute("data-scroll-state", "top");
      this.timer = setTimeout(() => this.move(generation, max),
                              SCROLL_TIMING.topHoldMs);
    }

    move(generation, max) {
      if (generation !== this.generation) return;
      const started = performance.now();
      const duration = max / Math.max(1, SCROLL_TIMING.speedPxPerSec) * 1000;
      this.viewport.setAttribute("data-scroll-state", "moving");
      const step = () => {
        if (generation !== this.generation) return;
        const now = performance.now();
        const progress = Math.min(1, (now - started) / duration);
        this.viewport.scrollTop = max * progress;
        if (progress < 1) return;
        clearInterval(this.timer);
        this.timer = null;
        this.viewport.scrollTop = max;
        this.viewport.setAttribute("data-scroll-state", "bottom");
        if (!this.loop) return;
        this.timer = setTimeout(() => {
          if (generation !== this.generation) return;
          this.viewport.scrollTop = 0;
          this.viewport.setAttribute("data-scroll-state", "top");
          this.timer = setTimeout(() => this.move(generation, max),
                                  SCROLL_TIMING.topHoldMs);
        }, SCROLL_TIMING.bottomHoldMs);
      };
      this.timer = setInterval(step, 16);
      step();
    }
  }

  const puzzleScroller = new AutoScroller(el.puzzleViewport);
  const revealScroller = new AutoScroller(el.revealBody);

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
      s.solved ? "共同猜汤" : "大家已经推到这里";
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

  // 揭晓工作区(U2: 60 秒三阶段)
  //
  //   阶段           显示内容                              隐藏内容
  //   core           核心答案(超大字号) + "XX 补齐最后线索"  完整解释 / 共同解谜
  //   explanation    核心答案 + 完整解释                     共同解谜
  //   contribution   核心答案 + 共同解谜                     完整解释
  //
  // `reveal_stage` 由**服务端**算(见 engine.snapshot) —— 前端不自己
  // 计时: 刷新/重连后本地计时会从 0 重来, 与服务端不一致; 而且阶段边界
  // 是配置, 前端硬编码一份副本迟早漂移。
  //
  // ⚠️ 为什么必须分三段: 实播里核心答案 + 完整解释 + 共同解谜**同时**
  // 上屏, 下半屏三块互相争空间, fitReveal 只能一路缩字号, 完整解释被压
  // 成一条矮滚动框 —— 而观众没有鼠标去滚直播源。
  // **关键内容不能依赖用户滚动才能看见。** 60 秒本来就是时间资源:
  // 用时间换空间, 而不是把字缩小。
  //
  // 兼容: 老快照没有 `reveal_stage` 时退化成 U1 的两段行为
  // (`reveal_detail_visible` 为真就进 explanation)。
  function revealStageOf(s) {
    const st = s.reveal_stage;
    if (st === "core" || st === "explanation" || st === "contribution") {
      return st;
    }
    return s.reveal_detail_visible ? "explanation" : "core";
  }

  function revealTexts(s) {
    const structured = !!s.revealed_core_answer;
    return {
      core: structured
        ? s.revealed_core_answer
        : (s.revealed_full_answer || s.revealed_answer || ""),
      full: structured
        ? (s.revealed_full_answer || "")
        : (s.revealed_full_answer || s.revealed_answer || ""),
    };
  }

  function renderReveal(s) {
    const on = !!(s.revealed_answer && (s.phase === "revealed" || s.phase === "revealing"));
    el.reveal.classList.toggle("hidden", !on);
    // ⚠️ C2: 揭晓工作区与 #bottom 是**同一个矩形**(共用 --workspace-top),
    // 所以谜面天然不会被盖住 —— 这条不变量由 CSS 保证, 不靠"揭晓层
    // 自己让出上方"。
    //
    // 依然**不碰** #puzzle 的 hidden(谜面整个揭晓过程保持可见: 观众要
    // 对照着看"原来谜面那句话是这个意思")。
    //
    // 下半部的问答工作区要让位(任务书: 揭晓期间下半部整个给答案)。
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
      return;
    }

    const stage = revealStageOf(s);
    // 用 data 属性把阶段暴露给测试与 CSS(几何断言需要知道现在哪一段)。
    el.reveal.setAttribute("data-stage", stage);

    // ---- U3-B: 结构化题与 legacy 题必须**分开**推导 ----
    // 后端 U3-A 起:
    //     revealed_core_answer = raw core_answer
    //     revealed_full_answer = raw answer(结构化题拿不到就空串)
    //     revealed_answer      = legacy/组合揭晓文案
    //
    // 早先的写法是两边各自 `||` 一路兜到底:
    //     core = revealed_core_answer || revealed_full_answer || revealed_answer
    //     full = revealed_full_answer || revealed_answer
    // 对**结构化题**这是错的: 一旦 `revealed_full_answer` 为空(后端某次
    // full 丢失 / 老后端根本没这个字段), `full` 会 fallback 到
    // `revealed_answer` —— 而那是
    //     【核心答案】...
    //     【完整解释】...
    // 整段组合文案, 灌回正文后重复 bug 直接复活(正文里又一次核心答案,
    // 还多出两个本不该出现的标签)。
    //
    // 所以: **有结构化 core 时, full 为空就让它空着。**
    // 只有 legacy(没有 core)才允许往 `revealed_answer` 退。
    const texts = revealTexts(s);
    const core = texts.core, full = texts.full;
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

    // ---- 完整解释: 只在 explanation 阶段显示 ----
    // 若 full === core(legacy 单段题), 显示两块就是同一句话出现两次。
    const showDetail = stage === "explanation" && full && full !== core;
    el.revealBody.classList.toggle("hidden", !showDetail);
    if (showDetail && el.revealBody.textContent !== full) {
      el.revealBody.textContent = full;
    }

    // ---- 共同解谜: 只在 contribution 阶段显示 ----
    // 早显示会跟完整解释抢空间, 而那正是这次要修的故障。
    if (stage === "contribution") {
      renderRevealContributors(s);
    } else {
      renderRevealContributors({reveal_contributors: []});
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
    q.textContent = "…（前 " + n + " 条与汤底无关，已折叠）";
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

  // 提示保留 QA 历史，新提示额外由 announcer 播一次；旧 hintbar 仍隐藏。
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
        "发送 <b>#你的问题</b> 向我提问，猜中汤底我就揭晓";
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
    const ai = s.ai_player || {};
    const parts = [];
    if (s.ai_player) {
      parts.push(ai.in_flight ? "🤖 AI玩家正在推理…" : "👍 点赞可以召唤 AI 玩家");
    }
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

    // 谜面先适配字号；到 46px 仍 overflow 就交给 AutoScroller。
    const maxPuzzleH = TOP_MAX - 260;                // 谜面可用高度
    let fs = 58;
    for (let i = 0; i < 8; i++) {
      if (el.puzzle.style.fontSize === fs + "px"
          && el.puzzle.scrollHeight <= maxPuzzleH) break;
      el.puzzle.style.fontSize = fs + "px";
      if (el.puzzle.scrollHeight <= maxPuzzleH || fs === 46) break;
      fs = Math.max(46, fs - 3);
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
      // C2: `#bottom` 与 `#reveal` **共用**这一个分界值 —— 它们是同一个
      // 工作区矩形的两个占用者(#reveal 揭晓时接替 #bottom)。
      // 只写 CSS 变量而不各自 set style.top: 单一来源, 不可能错位。
      el.content.style.setProperty("--workspace-top", height + "px");
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

  // Session-local presentation: merged AI notice + bounded FIFO of hint notices.
  const announcer = (() => {
    const box = $("announcer"), text = $("announcer-text");
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
    let config = null, phase = "", earned = null, pendingAI = 0;
    let active = "", defaultText = "", lastPresetId = null;
    let nextPreset = Infinity, timer = null, animation = null;
    let currentMessage = "", measuredWidth = 0;
    let hintPuzzle = null, hintCount = null;
    const pendingHints = [];

    function cancel() {
      clearTimeout(timer);
      if (animation) animation.cancel();
      animation = null;
      text.style.transform = "";
      active = "";
    }
    function interval() { return (config ? config.interval_seconds : 90) * 1000; }
    function show(message, kind) {
      cancel();
      active = kind;
      box.dataset.kind = kind;
      text.textContent = message;
      const width = box.clientWidth, length = text.scrollWidth;
      currentMessage = message;
      measuredWidth = width;
      const hold = (config ? config.hold_seconds : 4) * 1000;
      function done() { cancel(); pump(); }
      if (length > width && reduced.matches) {
        // Measure each static page using the same rendered font, not char counts.
        const pages = [];
        let page = "";
        for (const char of Array.from(message)) {
          text.textContent = page + char;
          if (text.scrollWidth > width && page) { pages.push(page); page = char; }
          else page += char;
        }
        pages.push(page);
        let index = 0;
        function next() {
          text.textContent = pages[index++];
          box.dataset.motion = "pages";
          timer = setTimeout(index < pages.length ? next : done, hold);
        }
        next();
      } else if (length > width) {
        const duration = (width + length) / (config ? config.long_text_speed_px_s : 80) * 1000;
        box.dataset.motion = "marquee";
        animation = text.animate([
          {transform: `translateX(${width}px)`},
          {transform: `translateX(${-length}px)`},
        ], {duration, easing: "linear", fill: "forwards"});
        timer = setTimeout(done, duration);
      } else {
        box.dataset.motion = reduced.matches ? "static" : "slide";
        if (!reduced.matches) {
          animation = text.animate([
            {transform: `translateX(${width}px)`, offset: 0},
            {transform: "translateX(0)", offset: 300 / (hold + 600)},
            {transform: "translateX(0)", offset: (hold + 300) / (hold + 600)},
            {transform: `translateX(${-width}px)`, offset: 1},
          ], {duration: hold + 600, fill: "forwards"});
        }
        timer = setTimeout(done, hold + (reduced.matches ? 0 : 600));
      }
    }
    function pump() {
      if (phase !== "qa" || active) return;
      if (pendingAI) {
        const delta = pendingAI;
        pendingAI = 0;
        show(delta > 1 ? "🤖 AI玩家已获得新的出手机会"
          : "🤖 AI玩家已被召唤！正在准备出手…", "ai");
        return;
      }
      if (pendingHints.length) {
        show(pendingHints.shift(), "hint");
        return;
      }
      const items = config && config.enabled ? config.items.filter(x => x.enabled) : [];
      if (items.length && performance.now() >= nextPreset) {
        const index = (items.findIndex(x => x.id === lastPresetId) + 1) % items.length;
        lastPresetId = items[index].id; // Advance even when interrupted by AI.
        nextPreset = performance.now() + interval();
        show("📢 游戏公告 " + items[index].text, "preset");
      } else show(defaultText, "leaderboard");
    }
    function update(s) {
      const value = (s.ai_player || {}).questions_earned;
      if (Number.isSafeInteger(value) && value >= 0) {
        if (earned !== null && value > earned) pendingAI += value - earned;
        earned = value; // First valid snapshot (and a server reset) is a baseline.
      }
      if (hintPuzzle !== s.puzzle_index) {
        hintPuzzle = s.puzzle_index;
        hintCount = null;
        pendingHints.length = 0;
        if (active === "hint") cancel();
      }
      if (Number.isSafeInteger(s.hint_count) && s.hint_count >= 0) {
        if (hintCount !== null && s.hint_count > hintCount) {
          const latest = (s.qa_log || []).filter(r => r.kind === "hint").slice(-1)[0];
          const hint = s.hint_text || (latest && latest.text);
          if (hint) {
            // Bound burst backlog to 16 pending notices; QA keeps the history.
            if (pendingHints.length === 16) pendingHints.shift();
            pendingHints.push("💡 提示：" + hint);
          }
        }
        hintCount = s.hint_count;
      }
      const rows = Array.isArray(s.leaderboard) ? s.leaderboard.slice(0, 3) : [];
      const nextDefault = rows.length
        ? "📢 本场猜汤榜 " + rows.map(r => `${r.rank}. ${r.user_name} ${r.solved_count}题`).join("　")
        : "📢 游戏公告 猜中汤底即可登上本场猜汤榜";
      const changed = nextDefault !== defaultText;
      defaultText = nextDefault;
      if (phase !== s.phase) {
        phase = s.phase;
        cancel();
        nextPreset = performance.now() + interval();
      }
      box.classList.toggle("hidden", phase !== "qa");
      if (phase !== "qa") return;
      if (pendingAI && active !== "ai") {
        // An interrupted hint still gets a full readable turn after the AI notice.
        if (active === "hint") {
          pendingHints.unshift(currentMessage);
          if (pendingHints.length > 16) pendingHints.pop();
        }
        cancel();
      } else if ((pendingHints.length && active !== "ai" && active !== "hint")
                 || (changed && active === "leaderboard")) cancel();
      pump();
    }
    async function reload() {
      try {
        const response = await fetch("/announcements.json", {cache: "no-store"});
        if (!response.ok) throw new Error("announcement HTTP error");
        const c = await response.json();
        if (!c || typeof c.enabled !== "boolean" || !Array.isArray(c.items)
            || !Number.isFinite(c.interval_seconds) || c.interval_seconds <= 0
            || !Number.isFinite(c.hold_seconds) || c.hold_seconds < 3 || c.hold_seconds > 5
            || !Number.isFinite(c.long_text_speed_px_s) || c.long_text_speed_px_s <= 0) {
          throw new Error("invalid announcement config");
        }
        const ids = new Set();
        for (const item of c.items) {
          if (!item || typeof item.id !== "string" || !item.id || ids.has(item.id)
              || typeof item.enabled !== "boolean" || typeof item.text !== "string") {
            throw new Error("invalid announcement item");
          }
          ids.add(item.id);
        }
        c.items = c.items.filter(x => Array.from(x.text).length <= 160 && x.text.trim());
        const first = config === null;
        const anchor = nextPreset - interval();
        config = c;
        if (first && phase === "qa") nextPreset = performance.now() + interval();
        else if (!first) nextPreset = anchor + interval();
      } catch (_) { /* Keep last-known-good; config availability never blocks WS. */ }
    }
    reload();
    setInterval(reload, 15000);
    setInterval(() => {
      if (phase === "qa" && active === "leaderboard" && config && config.enabled
          && config.items.some(x => x.enabled) && performance.now() >= nextPreset) {
        cancel(); pump();
      }
    }, 250);
    reduced.addEventListener("change", () => {
      if (active) show(currentMessage, active);
      else pump();
    });
    new ResizeObserver(() => {
      if (active && box.clientWidth > 0 && box.clientWidth !== measuredWidth) {
        show(currentMessage, active); // Debug width changed: remeasure pages/motion.
      }
    }).observe(box);
    return {update};
  })();

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
    announcer.update(s);
    puzzleScroller.update(
      JSON.stringify([s.puzzle_index, s.puzzle || "", s.phase || ""]),
      s.phase === "qa", true);
    const revealStage = revealStageOf(s);
    const revealText = revealTexts(s).full;
    const revealOn = !!(s.revealed_answer
      && (s.phase === "revealed" || s.phase === "revealing"));
    revealScroller.update(
      JSON.stringify([s.puzzle_index, revealStage, revealText]),
      revealOn && revealStage === "explanation"
        && !el.revealBody.classList.contains("hidden"), false);
  }

  document.addEventListener("keydown", function (e) {
    if (e.key === "d" || e.key === "D") {
      showDebug = !showDebug;
      applyDebug();
    }
  });

  connect();
})();
