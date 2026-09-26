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
    puzzleIndex: $("puzzle-index"),
    puzzleTimer: $("puzzle-timer"), puzzleMeta: $("puzzle-meta"),
    puzzleViewport: $("puzzle-viewport"), puzzle: $("puzzle"),
    reveal: $("reveal"), revealBody: $("reveal-body"), revealNext: $("reveal-next"),
    revealLabel: $("reveal-label"),
    revealCore: $("reveal-core"), revealWho: $("reveal-who"),
    revealContrib: $("reveal-contrib"),
    revealContribTitle: $("reveal-contrib-title"),
    revealContribList: $("reveal-contrib-list"),
    // ---- Issue #60 §5: REVEALED 互动层 ----
    revealInteraction: $("reveal-interaction"),
    ratingBox: $("rating-box"), ratingTitle: $("rating-title"),
    ratingDist: $("rating-dist"),
    themeBox: $("theme-box"), themeTitle: $("theme-title"),
    themeOptions: $("theme-options"),
    top: $("top"), bottom: $("bottom"), content: $("content"),
    qa: $("qa"), qaBody: $("qa-body"),
    factProgress: $("fact-progress"),
    factProgressTitle: $("fact-progress-title"),
    factProgressDots: $("fact-progress-dots"),
    factProgressList: $("fact-progress-list"),
    announcer: $("announcer"), footer: $("footer-status"),
    thinking: $("thinking"), prompt: $("prompt"),
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

  // 题目元数据(5 大类协议 v2): 只渲染服务端下发的 label。
  //
  // ⚠️ 前端**不**维护任何 enum->中文映射 —— 中文 label 由后端
  // (haiguitang_protocol.public_puzzle_meta)单一拥有, 这里只拼接。
  // 防 stale: 渲染完全依据当前快照的 `puzzle_meta` —— SETTING 阶段
  // 服务端已把它清空, 所以这里**立即**清掉旧题的显示(不能等
  // puzzle_index 变: SETTING 期间下一题还没拿到, 新题号不一定已递增)。
  // 只显示主类与难度，不在直播 header 堆叠 secondary categories。
  function renderPuzzleMeta(s) {
    const meta = (s.puzzle_meta && typeof s.puzzle_meta === "object")
      ? s.puzzle_meta : {};
    const category = meta.primary_category_label || "";
    const difficulty = meta.difficulty_label || "";
    const text = [category, difficulty].filter(Boolean).join(" · ");
    if (el.puzzleMeta.textContent !== text) {
      el.puzzleMeta.replaceChildren();
      if (category) {
        const label = document.createElement("span");
        label.className = "puzzle-category";
        label.textContent = category;
        el.puzzleMeta.append(label);
      }
      if (difficulty) el.puzzleMeta.append((category ? " · " : "") + difficulty);
    }
    el.puzzleMeta.classList.toggle("hidden", !text);
  }

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
    renderPuzzleMeta(s);
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

  // ---- Issue #60 §5: REVEALED 互动层 ----
  //
  // 前端只显示服务端权威状态(reveal_interaction), 不自行推断窗口
  // 是否开放 —— 评分/投票开没开, 只认快照, 绝不从 next_puzzle_ms
  // 自算。0~30s 评分+主题并排; 30s 后 rating_open=false 时评分区
  // 收起，主题投票成为主要 CTA。45~60s contribution 内容
  // 仍可读 —— 本面板按行内紧凑布局渲染, 不挤占正文空间。
  function renderRevealInteraction(s) {
    const ri = s.reveal_interaction || null;
    const on = !!(ri && s.phase === "revealed");
    el.revealInteraction.classList.toggle("hidden", !on);
    if (!on) return;

    // ---- 评分 ----
    el.ratingBox.classList.toggle("hidden", ri.rating_open === false);
    if (ri.rating_open === false && el.ratingTitle.textContent !== "评分已截止") {
      el.ratingTitle.textContent = "评分已截止";
    } else if (ri.rating_open !== false && el.ratingTitle.textContent !== "给本题评分：发送 #1~#5") {
      el.ratingTitle.textContent = "给本题评分：发送 #1~#5";
    }
    const dist = ri.rating_distribution || {};
    let distHtml = "";
    for (const k of ["1", "2", "3", "4", "5"]) {
      const n = dist[k] || 0;
      distHtml += "<span class='rating-pill'>" + k + "★ " + n + "</span>";
    }
    if (el.ratingDist.dataset.sig !== distHtml) {
      el.ratingDist.innerHTML = distHtml;
      el.ratingDist.dataset.sig = distHtml;
    }

    // ---- 主题投票 ----
    const opts = ri.theme_options || [];
    let optsHtml = "";
    for (const o of opts) {
      optsHtml += "<span class='theme-pill'>#" + o.code + " " + o.label
        + " <b>" + (o.votes || 0) + "</b></span>";
    }
    if (el.themeOptions.dataset.sig !== optsHtml) {
      el.themeOptions.innerHTML = optsHtml;
      el.themeOptions.dataset.sig = optsHtml;
    }
    // freeze 后显示选中的主题(§5: 60s 显示最终 selected theme)。
    const sel = ri.selected_category || "";
    if (sel && ri.frozen) {
      const lbl = (opts.find(o => o.category === sel) || {}).label || sel;
      const title = "下一题：" + lbl;
      if (el.themeTitle.textContent !== title) el.themeTitle.textContent = title;
    } else if (!ri.frozen) {
      const defTitle = "下一题主题：发送 #a~#e（可改票）";
      if (el.themeTitle.textContent !== defTitle) el.themeTitle.textContent = defTitle;
    }
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
    // ---- Issue #60: 互动层(评分/主题投票) ----
    renderRevealInteraction(s);
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
  // Issue #53 §31: rephrase(请改问法)**不参与**这里的折叠 —— 它不是
  // 「无关」, 混进去会把"不知道怎么问"和"问偏了"两种信号搅在一起。
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

  let lastFactSig = null, lastFactCount = 0;
  function renderFactProgress(s) {
    const p = s.phase === "qa" ? s.fact_progress : null;
    const visible = p && Number.isInteger(p.total) && p.total > 0;
    el.factProgress.classList.toggle("hidden", !visible);
    if (!visible) {
      lastFactSig = null;
      lastFactCount = 0;
      el.factProgressTitle.textContent = "";
      el.factProgressDots.textContent = "";
      el.factProgressList.replaceChildren();
      return;
    }
    el.factProgressTitle.textContent = "🧩 已确认核心事实 " + p.established + " / " + p.total;
    el.factProgressDots.textContent = "●".repeat(p.established) + "○".repeat(Math.max(0, p.total - p.established));
    el.factProgressList.replaceChildren();
    const facts = p.facts || [];
    const recent = facts.slice(-2);
    if (facts.length > 2) {
      const more = document.createElement("span");
      more.className = "fact-progress-more";
      more.textContent = "另有 " + (facts.length - 2) + " 条已确认";
      el.factProgressList.appendChild(more);
    }
    for (const fact of recent) {
      const row = document.createElement("div");
      row.textContent = "✓ " + fact.text;
      el.factProgressList.appendChild(row);
    }
    const sig = JSON.stringify([s.puzzle_index, p.established, facts]);
    if (lastFactSig && sig !== lastFactSig && p.established > lastFactCount) {
      el.factProgress.classList.remove("new-fact");
      void el.factProgress.offsetWidth;
      el.factProgress.classList.add("new-fact");
    }
    if (sig !== lastFactSig) {
      lastFactSig = sig;
      lastFactCount = p.established;
    }
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
      // Issue #53 §31/§32: rephrase 不是 verdict —— 只读服务端下发的
      // `response_kind` 渲染"请改问法"徽章。JS 不做任何语义判断
      // (不检查文本里有没有"怎么/为什么"), 前端只是渲染。
      if (r.response_kind === "rephrase") {
        const v = document.createElement("span");
        v.className = "verdict v-rephrase";
        v.textContent = "请改问法";
        q.appendChild(v);
      } else if (r.verdict) {
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
    const pending = s.phase === "qa" && s.pending_count > 0;
    el.thinking.classList.toggle("hidden", !pending);
    if (pending) {
      el.thinking.textContent = "AI处理中 ×" + s.pending_count;
    }
  }

  // Footer 操作提示，揭晓阶段随整个 Footer 隐藏。
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
        "发送 <b>#你的问题</b> 向我提问";
    } else if (s.phase === "setting") {
      el.prompt.textContent = "AI 正在准备下一题…";
    } else if (s.phase === "revealing" || s.phase === "revealed") {
      el.prompt.textContent = "本题已结束，稍候将开启新谜题…";
    } else {
      el.prompt.textContent = "直播准备中…";
    }
  }

  function renderStats(s) {
    const ai = s.ai_player || {};
    const on = s.phase === "qa" && !!s.ai_player;
    el.stats.classList.toggle("hidden", !on);
    if (on) {
      const per = Number.isFinite(ai.likes_per_progress) ? ai.likes_per_progress : 100;
      const prog = Number.isFinite(ai.likes_progress) ? ai.likes_progress : 0;
      el.stats.textContent = "❤️ " + prog + "/" + per;
    }
    el.footer.classList.toggle("hidden", s.phase === "revealing" || s.phase === "revealed");
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
    L.push("<span class='k'>已进行</span> " + fmtElapsed(s.puzzle_elapsed_ms));
    const stats = s.stats || {};
    L.push("<span class='k'>本题已问/已答</span> " + (stats.questions || 0) + "/" + (stats.answered || 0));
    L.push("<span class='k'>观众/累计猜中</span> " + (stats.viewers_seen || 0) + "/" + (stats.solved || 0));
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

  // Session-local presentation: merged like/hint notices + bounded FIFO of hint notices.
  const announcer = (() => {
    const box = $("announcer"), text = $("announcer-text");
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
    let config = null, phase = "", hintPuzzle = null, hintCount = null;
    let active = "", lastPresetId = null;
    let nextPreset = Infinity, nextLeaderboard = Infinity;
    let timer = null, animation = null;
    let currentMessage = "", measuredWidth = 0;
    const pendingHints = [];
    // ---- Issue #43: 点赞推进公告(单一待播槽位, 不做 pendingLikes[]) ----
    // likePending   等待播放的最新一条(新 seq 直接覆盖旧的 -> burst 永远
    //               只积压一条, 不会无限 FIFO);
    // lastLikeSeq   已见过的最大 seq: 基线不回放 —— 页面刷新/重连不能把
    //               历史公告再播一遍。
    // ⚠️ 基线的判定: **首帧快照**里就带着的公告属于历史(页面是中途打开
    // 的), 只记 seq 不播; 首帧没有公告的页面, 之后收到的任何 seq 都是
    // 活事件, 照播。否则"本场第一条点赞公告"会被误当历史吞掉。
    let likePending = null, lastLikeSeq = null, sawFirstSnapshot = false;
    let leaderboardRows = [], leaderboardKey = "";

    function cancel() {
      clearTimeout(timer);
      timer = null;
      if (animation) animation.cancel();
      animation = null;
      text.style.transform = "";
      active = "";
      box.classList.add("hidden");
    }
    function interval() { return (config ? config.interval_seconds : 90) * 1000; }
    // ---- Issue #43(review round 2): round 校验的两个具名判定 ----
    // likeRoundOf: 这个快照里"当前 round"是第几题。与 Engine 的记账一致
    //   —— SETTING 期间 pulse 记入**正在准备的**那一题(puzzle_index+1),
    //   其余阶段就是 puzzle_index。
    // likeNoticeStale: 一条待播点赞公告对**这个快照**是否已经失效。
    //   ① round 已经过去(旧 round 的迟到公告) -> 失效;
    //   ② 它诞生于 QA 而视图已离开 QA -> 本题 QA 已结束, 公告失效
    //     (QA 的点赞反馈不拖进揭晓/下一题, §14/§16)。
    //   SETTING 期间入账的公告两条都不命中 —— 它合法地等待自己的 QA。
    function likeRoundOf(s) {
      return s.phase === "setting" ? s.puzzle_index + 1 : s.puzzle_index;
    }
    function likeNoticeStale(lp, s) {
      if (!lp) return false;
      if (Number.isSafeInteger(lp.round_index)
          && lp.round_index < likeRoundOf(s)) return true;
      return lp.phase === "qa" && s.phase !== "qa";
    }
    function show(message, kind, onDone = null) {
      cancel();
      active = kind;
      box.classList.remove("hidden");
      box.dataset.kind = kind;
      text.textContent = message;
      const width = box.clientWidth, length = text.scrollWidth;
      currentMessage = message;
      measuredWidth = width;
      const configuredHold = config ? config.hold_seconds : 4;
      // 自定义公告可以停更久；AI / Hint / 排行榜继续保持短促，
      // 避免把 operator 的 15s 配置扩散到所有临时消息。
      const hold = (kind === "like" ? 3
        : kind === "preset" ? Math.max(5, Math.min(8, configuredHold))
        : 5) * 1000;
      function done() {
        cancel();
        if (onDone) onDone();
        else pump();
      }
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
        const slideMs = 1000;
        if (!reduced.matches) {
          const total = hold + slideMs * 2;
          animation = text.animate([
            {transform: `translateX(${width}px)`, offset: 0},
            {transform: "translateX(0)", offset: slideMs / total},
            {transform: "translateX(0)", offset: (hold + slideMs) / total},
            {transform: `translateX(${-width}px)`, offset: 1},
          ], {duration: total, fill: "forwards"});
        }
        timer = setTimeout(done, hold + (reduced.matches ? 0 : slideMs * 2));
      }
    }

    function leaderboardMessage() {
      if (!leaderboardRows.length) return "";
      return "📢 累计猜汤榜　"
        + leaderboardRows
          .map(r => `${r.rank}. ${r.user_name} ${r.solved_count}题`)
          .join("　");
    }

    function showLeaderboard() {
      if (!leaderboardRows.length) {
        showEmptyLeaderboard();
        return;
      }
      const message = leaderboardMessage();
      nextLeaderboard = performance.now() + 90000;
      show(message, "leaderboard");
    }

    // Empty leaderboard is a quiet baseline state: keep the scheduler armed
    // without rendering placeholder copy or starting a pointless animation.
    function showEmptyLeaderboard() {
      cancel();
      text.textContent = "";
      currentMessage = "";
      measuredWidth = box.clientWidth;
    }

    function pump() {
      if (phase !== "qa" || active) return;
      if (pendingHints.length) {
        show(pendingHints.shift(), "hint");
        return;
      }
      // Issue #43: 点赞公告排在提示之后、preset/榜单之前 ——
      // 提示永远优先, 点赞不会腰斩提示, 但可以打断preset/榜单。
      if (likePending) {
        const lp = likePending;
        likePending = null;
        show(lp.text, "like");
        return;
      }
      const items = config && config.enabled ? config.items.filter(x => x.enabled) : [];
      if (items.length && performance.now() >= nextPreset) {
        const index = (items.findIndex(x => x.id === lastPresetId) + 1) % items.length;
        lastPresetId = items[index].id; // Advance even when interrupted by AI.
        nextPreset = performance.now() + interval();
        show("📢 游戏公告 " + items[index].text, "preset");
      } else if (leaderboardRows.length && performance.now() >= nextLeaderboard) {
        showLeaderboard();
      } else {
        box.classList.add("hidden");
      }
    }
    function update(s) {
      // ---- Issue #43: 显式点赞推进事件(seq 去重 + round 校验) ----
      // 前端不再从 questions_earned 的 delta 推断"AI 被召唤"; 也不盲收
      // seq —— 必须**校验 round**(review round 2): 只有对当前快照仍然
      // 有效的公告才入槽; 旧 round 的迟到公告标记已见后直接丢弃。
      const lp = s.like_progress_notice;
      if (Number.isSafeInteger(lp && lp.seq)) {
        if (!sawFirstSnapshot) {
          sawFirstSnapshot = true;
          lastLikeSeq = lp.seq; // 首帧: 只建基线, 不回放历史公告
        } else if (lp.seq > lastLikeSeq) {
          lastLikeSeq = lp.seq;
          if (lp.text && !likeNoticeStale(lp, s)) likePending = lp;
        }
      } else if (!sawFirstSnapshot) {
        sawFirstSnapshot = true; // 首帧无公告: 之后来的都是活事件
        lastLikeSeq = 0;
      }
      if (hintPuzzle !== s.puzzle_index) {
        const hadPuzzle = hintPuzzle !== null;
        hintPuzzle = s.puzzle_index;
        hintCount = null;
        pendingHints.length = 0;
        // 换题帧的竞态(review round 2): 判定必须在**新快照**上做 ——
        // 属于新 round 的待播公告(SETTING 期间入账那种)必须活着跨过
        // 这帧; 只有已失效的(旧 round)才清。早先"无条件清空"会把
        // "换题前一刻的点赞"先标记已见再清掉, 永久不播。
        if (likeNoticeStale(likePending, s)) likePending = null;
        // 每道新题都重新从第 1 名开始滚完整 Top10。
        // 同一题内 AI / 提示覆盖排行榜后，也会重新从整条榜首开始，
        // 不再维护分页/页码状态。
        if (active === "hint" || (hadPuzzle && active === "leaderboard")) cancel();
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
      const rows = Array.isArray(s.leaderboard) ? s.leaderboard.slice(0, 10) : [];
      const nextKey = JSON.stringify(rows.map(r => [
        r.rank, r.user_name, r.solved_count
      ]));
      const changed = nextKey !== leaderboardKey;
      leaderboardRows = rows;
      leaderboardKey = nextKey;
      if (phase !== s.phase) {
        phase = s.phase;
        cancel();
        // 换阶段同样走失效判定(review round 2): 诞生于 QA 的公告在
        // 视图离开 QA 时失效(不拖进揭晓); SETTING 期间入账的公告
        // 合法跨过 setting→qa, 不能一刀切清掉。
        if (likeNoticeStale(likePending, s)) likePending = null;
        nextPreset = performance.now() + interval();
        nextLeaderboard = performance.now() + 90000;
      }
      if (phase !== "qa") return;
      // ---- 抢占规则(Issue #43 §15) ----
      //   * 提示不被任何东西腰斩: active === "hint" 时谁都不取消,
      //     点赞/其它公告在它播完后由 pump() 依优先级接上;
      //   * 点赞可打断 preset/榜单; 新点赞可替换正在播/待播的旧点赞;
      //   * 提示可打断正在播的点赞(提示更重要)。
      if ((likePending || pendingHints.length) && active !== "hint") cancel();
      else if (changed && active === "leaderboard") cancel();
      pump();
    }
    async function reload() {
      try {
        const response = await fetch("/announcements.json", {cache: "no-store"});
        if (!response.ok) throw new Error("announcement HTTP error");
        const c = await response.json();
        if (!c || typeof c.enabled !== "boolean" || !Array.isArray(c.items)
            || !Number.isFinite(c.interval_seconds) || c.interval_seconds <= 0
            || !Number.isFinite(c.hold_seconds) || c.hold_seconds < 3 || c.hold_seconds > 20
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
        // 公告到点时不要腰斩正在滚动的完整 Top10。
        // 长榜 marquee / reduced-motion 文本分页都有 timer/animation：
        // 让整条榜自然结束，done() -> pump() 再立即播已到期公告。
        // 若整榜短到能静态放下，则没有 timer/animation，公告可到点覆盖。
        if (timer || animation) return;
        cancel(); pump();
      }
      if (phase === "qa" && !active) pump();
    }, 250);
    reduced.addEventListener("change", () => {
      if (active === "leaderboard") showLeaderboard();
      else if (active) show(currentMessage, active);
      else pump();
    });
    new ResizeObserver(() => {
      if (active && box.clientWidth > 0 && box.clientWidth !== measuredWidth) {
        if (active === "leaderboard") showLeaderboard();
        else show(currentMessage, active); // Debug width changed: remeasure pages/motion.
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
    renderFactProgress(s);
    renderReveal(s);
    renderThinking(s);
    renderPrompt(s);
    renderStats(s);
    renderDebug(s);
    layout();
    el.announcer.style.top = el.qa.offsetTop + "px";
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
