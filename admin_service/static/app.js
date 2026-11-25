/* 值班调试台前端逻辑：原生 JS，无构建依赖。 */
"use strict";

const LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"];
const state = {
  workerIds: [],
  overview: null,
  serverOffsetMs: 0,
  logs: {}, // wid -> { items: [], lastId: 0, auto: true, filter: "ALL", error: null }
};

const $cards = document.getElementById("cards");
const $globalMsg = document.getElementById("globalMsg");

// ------------------------------------------------------------ 工具
async function api(path, options = {}) {
  const opts = { headers: {}, ...options };
  if (opts.body && typeof opts.body !== "string") {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.body);
  }
  const resp = await fetch(path, opts);
  let data = null;
  try {
    data = await resp.json();
  } catch (_) {
    /* 非 JSON */
  }
  if (!resp.ok) {
    throw new Error((data && data.detail) || `HTTP ${resp.status}`);
  }
  return data;
}

function showGlobal(message, isErr = false, timeout = 3500) {
  $globalMsg.hidden = false;
  $globalMsg.className = "global-msg" + (isErr ? " err" : "");
  $globalMsg.textContent = message;
  if (timeout) {
    clearTimeout(showGlobal._t);
    showGlobal._t = setTimeout(() => ($globalMsg.hidden = true), timeout);
  }
}

function shortTime(iso) {
  if (!iso) return "--:--:--";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toISOString().slice(11, 19) + "Z";
}

function fullTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toISOString().replace("T", " ").slice(0, 19) + " UTC";
}

function fmtCountdown(sec) {
  if (sec == null || sec <= 0) return "已到期";
  if (sec < 60) return sec.toFixed(1) + " 秒";
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  return `${m} 分 ${String(s).padStart(2, "0")} 秒`;
}

function esc(s) {
  return String(s == null ? "" : s);
}

// ------------------------------------------------------------ 卡片骨架
function cardShell(wid) {
  return `
  <article class="card" id="card-${wid}" data-worker="${wid}">
    <div class="card-head">
      <div class="worker-name"><span class="dot" id="dot-${wid}"></span>${wid}</div>
      <div class="badges">
        <span class="badge" id="channel-${wid}">检测中…</span>
        <span class="badge" id="adminPaused-${wid}" hidden>管理端已暂停下发</span>
      </div>
    </div>

    <div class="level-hero">
      <div class="level-badge lv-UNKNOWN" id="actual-${wid}">--</div>
      <div class="countdown" id="countdown-${wid}">
        <div>临时 DEBUG 剩余</div>
        <div class="big">—</div>
      </div>
    </div>

    <div class="state-grid">
      <div class="row-full"><span class="k">已应用常规级别</span>
        <span class="v" id="regular-${wid}">—</span></div>
      <div class="row-full"><span class="k">调试期内待生效级别</span>
        <span class="v" id="pending-${wid}">—</span></div>
      <div><span class="k">worker 本地截止</span></div>
      <div class="v" id="wDeadline-${wid}" title="">—</div>
      <div><span class="k">管理端记录截止</span></div>
      <div class="v" id="aDeadline-${wid}" title="">—</div>
      <div><span class="k">最近成功拉取</span></div>
      <div class="v" id="lastOk-${wid}">—</div>
      <div><span class="k">最近轮询尝试</span></div>
      <div class="v" id="lastTry-${wid}">—</div>
      <div class="row-full"><span class="k">通道状态 / 错误</span>
        <span class="v" id="error-${wid}">—</span></div>
    </div>

    <div class="controls">
      <div class="ctrl-row">
        <label>常规级别</label>
        <select id="levelSelect-${wid}">
          ${LEVELS.map((l) => `<option value="${l}">${l}</option>`).join("")}
        </select>
        <button class="btn btn-ghost" data-act="set-level">下发并更新常规级别</button>
      </div>
      <div class="ctrl-row">
        <label>DEBUG 时长</label>
        <input type="number" id="seconds-${wid}" value="30" min="1" step="1" /> 秒
        <button class="chip" data-sec="10">10s</button>
        <button class="chip" data-sec="30">30s</button>
        <button class="chip" data-sec="60">60s</button>
        <button class="chip" data-sec="300">5m</button>
        <button class="chip" data-sec="1800">30m</button>
      </div>
      <div class="ctrl-row">
        <button class="btn btn-debug" data-act="debug-start">开启 / 重设 DEBUG</button>
        <button class="btn btn-renew" data-act="debug-renew">续开此时长</button>
        <button class="btn btn-danger-ghost" data-act="debug-cancel">提前结束 DEBUG</button>
        <button class="btn btn-warn" data-act="toggle-pause">暂停接收配置</button>
      </div>
      <div class="ctrl-row">
        <button class="btn btn-primary" data-act="write-sample">📝 写样例日志</button>
        <span class="action-msg" id="actionMsg-${wid}"></span>
      </div>
    </div>

    <div class="logs-wrap">
      <div class="logs-toolbar">
        <strong>样例模块日志</strong>
        <label><input type="checkbox" id="autoLogs-${wid}" checked /> 自动刷新</label>
        <label>级别过滤
          <select id="logFilter-${wid}">
            <option value="ALL">全部</option>
            ${LEVELS.map((l) => `<option value="${l}">${l}</option>`).join("")}
          </select>
        </label>
        <button class="btn btn-ghost" data-act="clear-logs" style="padding:3px 10px;font-size:12px">清空显示</button>
        <span id="logMeta-${wid}"></span>
      </div>
      <div class="log-list" id="logList-${wid}"><div class="log-empty">等待日志…</div></div>
    </div>
  </article>`;
}

function ensureCard(wid) {
  if (document.getElementById(`card-${wid}`)) return;
  $cards.querySelector(".loading")?.remove();
  const wrap = document.createElement("div");
  wrap.innerHTML = cardShell(wid).trim();
  const card = wrap.firstElementChild;
  $cards.appendChild(card);

  state.logs[wid] = {
    items: [],
    lastId: 0,
    auto: true,
    filter: "ALL",
    error: null,
    initialized: false,
    bootId: null, // 当前面板所跟随进程的 boot_id（重启后换新值）
    knownBoots: [], // 本页面已见过的 boot_id，用于识别迟到的旧进程响应
    reqSeq: 0, // 已发出的日志请求序号（单调递增，不排队、不互斥）
    appliedReq: 0, // 已应用的最大请求序号（同进程内乱序响应只认最新的）
    restartNotice: null // 最近一次切换到新进程的提示
  };

  card.addEventListener("click", (ev) => onCardClick(ev, wid));
  card.querySelector(`#logFilter-${wid}`).addEventListener("change", (ev) => {
    state.logs[wid].filter = ev.target.value;
    renderLogs(wid);
  });
  card.querySelector(`#autoLogs-${wid}`).addEventListener("change", (ev) => {
    state.logs[wid].auto = ev.target.checked;
  });
  card.querySelectorAll(".chip").forEach((chip) =>
    chip.addEventListener("click", () => {
      card.querySelector(`#seconds-${wid}`).value = chip.dataset.sec;
    })
  );
}

// ------------------------------------------------------------ 操作
function setActionMsg(wid, text, isErr = false) {
  const el = document.getElementById(`actionMsg-${wid}`);
  if (!el) return;
  el.className = "action-msg " + (isErr ? "err" : "ok");
  el.textContent = text;
}

async function onCardClick(ev, wid) {
  const btn = ev.target.closest("button[data-act]");
  if (!btn) return;
  const act = btn.dataset.act;

  if (act === "clear-logs") {
    state.logs[wid].items = [];
    renderLogs(wid);
    return;
  }

  btn.disabled = true;
  try {
    if (act === "set-level") {
      const level = document.getElementById(`levelSelect-${wid}`).value;
      await api(`/api/workers/${wid}/level`, { method: "POST", body: { level } });
      setActionMsg(wid, `已下发常规级别 ${level}（DEBUG 进行中则到期后生效）`);
    } else if (act === "debug-start" || act === "debug-renew") {
      const seconds = Number(document.getElementById(`seconds-${wid}`).value);
      if (!seconds || seconds <= 0) throw new Error("时长必须是正数秒");
      const path = act === "debug-start" ? "debug/start" : "debug/renew";
      const r = await api(`/api/workers/${wid}/${path}`, { method: "POST", body: { seconds } });
      setActionMsg(
        wid,
        (act === "debug-renew" ? "已续开，" : "已开启，") +
          "管理端截止 " + fullTime(r.config.debug_deadline)
      );
    } else if (act === "debug-cancel") {
      await api(`/api/workers/${wid}/debug/cancel`, { method: "POST" });
      setActionMsg(wid, "已请求提前结束 DEBUG");
    } else if (act === "toggle-pause") {
      const paused = btn.dataset.paused === "1";
      await api(`/api/workers/${wid}/${paused ? "resume" : "pause"}`, { method: "POST" });
      setActionMsg(wid, paused ? "已恢复配置下发" : "已暂停配置下发（模拟断连）");
    } else if (act === "write-sample") {
      const r = await api(`/api/workers/${wid}/sample-logs`, { method: "POST" });
      const res = r.result || {};
      setActionMsg(wid, "");
      const msg = document.getElementById(`actionMsg-${wid}`);
      msg.innerHTML =
        `样例 #${res.sequence} 已写入；实际级别 <b>${res.actual_level}</b>；` +
        `输出 [${(res.emitted || []).join(", ")}]` +
        ((res.suppressed || []).length
          ? `；<span class="suppressed">被过滤 [${res.suppressed.join(", ")}]</span>`
          : "");
      await loadLogs(wid, true);
    }
    await refreshOverview();
  } catch (err) {
    setActionMsg(wid, "操作失败：" + err.message, true);
  } finally {
    btn.disabled = false;
  }
}

document.getElementById("writeAllBtn").addEventListener("click", async (ev) => {
  const btn = ev.currentTarget;
  btn.disabled = true;
  try {
    const r = await api("/api/sample-logs/write-all", { method: "POST" });
    const failed = Object.entries(r.workers).filter(([, v]) => v.error);
    if (failed.length) {
      showGlobal("部分 worker 写入失败：" + failed.map(([k, v]) => `${k}(${v.error})`).join("; "), true, 6000);
    } else {
      const seqs = Object.entries(r.workers)
        .map(([k, v]) => `${k}#${v.result.sequence}(${v.result.actual_level})`)
        .join("，");
      showGlobal("两边均已写样例日志：" + seqs);
    }
    state.workerIds.forEach((id) => loadLogs(id, true));
  } catch (err) {
    showGlobal("写入失败：" + err.message, true);
  } finally {
    btn.disabled = false;
  }
});

// ------------------------------------------------------------ 概览渲染
async function refreshOverview() {
  let data;
  try {
    data = await api("/api/overview");
  } catch (err) {
    showGlobal("无法连接管理服务：" + err.message, true, 5000);
    return;
  }
  state.overview = data;
  const serverMs = new Date(data.server_time).getTime();
  state.serverOffsetMs = Number.isNaN(serverMs) ? 0 : serverMs - Date.now();

  const ids = data.workers.map((w) => w.id);
  const first = state.workerIds.length === 0;
  state.workerIds = ids;
  ids.forEach((wid) => {
    ensureCard(wid);
    updateCard(data.workers.find((w) => w.id === wid));
    if (first) loadLogs(wid, true);
  });
}

function updateCard(w) {
  const wid = w.id;
  const cfg = w.config || {};
  const live = w.live;
  const card = document.getElementById(`card-${wid}`);
  card.classList.toggle("is-paused", !!cfg.paused);
  card.classList.toggle("is-down", !live);

  // 状态接口同样带 boot_id：每秒一次的概览比 2 秒一次的日志轮询更早发现进程重启。
  // 检测到重启时 handleBootChange 会重置游标并立即发起新进程缓冲补读。
  if (live && live.boot_id) {
    handleBootChange(wid, { boot_id: live.boot_id, booted_at: live.booted_at });
  }

  // 头部通道状态
  const dot = document.getElementById(`dot-${wid}`);
  const channel = document.getElementById(`channel-${wid}`);
  const adminPaused = document.getElementById(`adminPaused-${wid}`);
  adminPaused.hidden = !cfg.paused;

  if (!live) {
    dot.className = "dot down";
    channel.className = "badge err";
    channel.textContent = "worker 不可达";
  } else if (cfg.paused || live.delivery_paused) {
    dot.className = "dot paused";
    channel.className = "badge warn";
    channel.textContent = "配置接收已暂停 / 503";
  } else if (live.last_error) {
    dot.className = "dot paused";
    channel.className = "badge warn";
    channel.textContent = "拉取失败，本地自治中";
  } else {
    dot.className = "dot";
    channel.className = "badge ok";
    channel.textContent = "配置通道正常";
  }

  // 实际级别
  const actual = document.getElementById(`actual-${wid}`);
  const level = live ? live.actual_level : "UNKNOWN";
  actual.textContent = level;
  actual.className = `level-badge lv-${LEVELS.includes(level) ? level : "UNKNOWN"}`;

  // 倒计时
  const cd = document.getElementById(`countdown-${wid}`);
  if (live && live.debug_active) {
    cd.classList.remove("dead");
    cd.querySelector(".big").textContent = fmtCountdown(live.seconds_remaining);
  } else {
    cd.classList.add("dead");
    cd.querySelector(".big").textContent = "未开启 / 已到期";
  }

  // 状态字段
  document.getElementById(`regular-${wid}`).textContent = live
    ? `${live.regular_level}（已应用 rev=${live.applied_regular_rev}）`
    : "—";
  const pending = document.getElementById(`pending-${wid}`);
  if (live && live.pending_regular_level) {
    pending.textContent = `${live.pending_regular_level}（rev=${live.pending_regular_rev}，DEBUG 到期后应用）`;
    pending.className = "v pending-text";
  } else {
    pending.textContent = "无";
    pending.className = "v";
  }

  const wd = document.getElementById(`wDeadline-${wid}`);
  wd.textContent = live && live.debug_deadline ? shortTime(live.debug_deadline) + " UTC" : "无";
  wd.title = live ? fullTime(live.debug_deadline) : "";

  const ad = document.getElementById(`aDeadline-${wid}`);
  ad.textContent = cfg.debug_deadline
    ? `${shortTime(cfg.debug_deadline)} UTC（续开 ${cfg.debug_renewals || 0} 次）`
    : "无";
  ad.title = fullTime(cfg.debug_deadline);

  document.getElementById(`lastOk-${wid}`).textContent = live ? shortTime(live.last_poll_success_at) : "—";
  document.getElementById(`lastTry-${wid}`).textContent = live ? shortTime(live.last_poll_attempt_at) : "—";

  const errEl = document.getElementById(`error-${wid}`);
  if (!live) {
    errEl.textContent = w.live_error || "无响应";
    errEl.className = "v error-text";
  } else if (live.last_error) {
    errEl.textContent = live.last_error + `（连续失败 ${live.consecutive_failures} 次，本地到期逻辑不受影响）`;
    errEl.className = "v error-text";
  } else {
    errEl.textContent = "无";
    errEl.className = "v";
  }

  // 控制按钮状态
  const pauseBtn = card.querySelector('button[data-act="toggle-pause"]');
  pauseBtn.dataset.paused = cfg.paused ? "1" : "0";
  pauseBtn.textContent = cfg.paused ? "▶ 恢复接收配置" : "⏸ 暂停接收配置";
  pauseBtn.classList.toggle("btn-warn", !cfg.paused);
  pauseBtn.classList.toggle("btn-primary", !!cfg.paused);

  // 常规级别下拉跟随实际常规值（不打断用户未提交选择：仅在未聚焦时同步）
  const sel = document.getElementById(`levelSelect-${wid}`);
  if (document.activeElement !== sel && live) sel.value = live.regular_level;
}

// ------------------------------------------------------------ 日志
// 进程重启续读的整体思路：
//   1) 每个 worker 进程启动时生成唯一 boot_id，日志/状态接口都带回来；
//   2) 面板状态里的 bootId 记录“当前正在跟随的进程”。收到陌生 boot_id 即判定进程重启：
//      新进程缓冲的日志 id 从 1 重新计数，旧游标（可能已经很大）必须作废，
//      直接用 limit=200 补读新缓冲里仍可取到的日志，再以新进程自己的 last_id 续读；
//   3) 已经被换掉的旧进程迟到响应（boot_id 对不上）一律丢弃，不动列表也不动游标；
//   4) 同一进程内每个请求带单调 reqSeq，乱序返回时只应用序号更大的响应；
//      合并再按 id 去重 + 游标只增不减，双保险保证每条日志只显示一次、续读位置不倒退。
function shortBoot(bootId) {
  return bootId ? String(bootId).slice(0, 8) : "--";
}

/**
 * 识别并处理进程切换。
 * 返回 "adopted"（首次见到该进程，本次响应继续应用）、
 *      "switched"（检测到重启，已重置并发起补读，本次响应应丢弃）、
 *      "stale"（旧进程的迟到响应，必须丢弃）、
 *      "unknown"（响应不含 boot_id，例如 worker 不可达时的代理错误，调用方按错误处理）。
 */
function handleBootChange(wid, data) {
  const ls = state.logs[wid];
  const bootId = data && data.boot_id ? String(data.boot_id) : null;
  if (!bootId) return "unknown";
  if (ls.bootId === bootId) return "same";
  if (!ls.bootId) {
    ls.bootId = bootId;
    if (!ls.knownBoots.includes(bootId)) ls.knownBoots.push(bootId);
    return "adopted";
  }
  if (ls.knownBoots.includes(bootId)) {
    // 已被换掉的旧进程迟到响应：不能改动列表或游标
    return "stale";
  }
  // 陌生 boot_id → worker 已重启，自动切到新进程
  const oldBoot = shortBoot(ls.bootId);
  ls.knownBoots.push(bootId);
  ls.bootId = bootId;
  ls.items = [];
  ls.lastId = 0;
  ls.initialized = false;
  ls.error = null;
  ls.appliedReq = 0; // 旧进程所有在途请求全部作废
  ls.restartNotice = {
    at: new Date().toISOString(),
    from: oldBoot,
    to: shortBoot(bootId),
    bootedAt: data.booted_at || null
  };
  renderLogs(wid);
  showGlobal(`${wid} 检测到进程重启（${oldBoot} → ${shortBoot(bootId)}），已切到新进程并补读缓冲日志`);
  // 用新进程自己的缓冲重新首读（limit 沿用既有 200 条查询方式），不 await，不阻塞调用方
  loadLogs(wid, true);
  return "switched";
}

/** 应用一条同进程日志响应：按 id 去重合并，游标只增不减。 */
function applyLogResponse(wid, data, reqId) {
  const ls = state.logs[wid];
  if (reqId <= ls.appliedReq) return; // 乱序的旧响应，丢弃
  ls.appliedReq = reqId;
  ls.error = data.error || null;
  const incoming = Array.isArray(data.logs) ? data.logs : [];
  if (incoming.length) {
    const seen = new Set(ls.items.map((r) => r.id));
    for (const r of incoming) {
      if (!seen.has(r.id)) {
        ls.items.push(r);
        seen.add(r.id);
      }
    }
    // 同进程内按 id 排序，配合 reverse() 保持“最新在上”的面板排序
    ls.items.sort((a, b) => a.id - b.id);
    if (ls.items.length > 400) ls.items = ls.items.slice(-400); // 显示条数上限维持 400
  }
  // 续读游标只允许前进，乱序/迟到响应都不能让它倒退
  const newLast = Number(data.last_id) || 0;
  if (newLast > ls.lastId) ls.lastId = newLast;
  renderLogs(wid);
}

async function loadLogs(wid, force = false) {
  const ls = state.logs[wid];
  if (!ls) return;
  if (!force && ls.auto === false) return;

  // 每个请求独立序号，不排队、不加全局锁：写样例后的刷新无需等待慢轮询
  const reqId = ++ls.reqSeq;
  const firstRead = !ls.initialized;
  // 沿用日志接口现有查询方式：首读 limit=200；增量用 after_id 游标 + limit=1000
  const url = firstRead
    ? `/api/workers/${wid}/logs?limit=200`
    : `/api/workers/${wid}/logs?after_id=${ls.lastId}&limit=1000`;

  let data;
  try {
    data = await api(url);
  } catch (err) {
    // 仅当没有更新的请求已处理过时展示错误；不清空列表、不动游标
    if (reqId > ls.appliedReq) {
      ls.appliedReq = reqId;
      ls.error = err.message;
      renderLogs(wid);
    }
    return;
  }

  const bootAction = handleBootChange(wid, data);
  if (bootAction === "stale" || bootAction === "switched") return;
  if (bootAction === "unknown" && data.error) {
    // worker 不可达时管理端代理返回的错误信封（无 boot_id）
    if (reqId > ls.appliedReq) {
      ls.appliedReq = reqId;
      ls.error = data.error;
      renderLogs(wid);
    }
    return;
  }

  // 首读与增量都走去重合并（应对首读与增量请求并发的边界），游标始终只增不减
  ls.initialized = true;
  applyLogResponse(wid, data, reqId);
}

function renderLogs(wid) {
  const ls = state.logs[wid];
  const listEl = document.getElementById(`logList-${wid}`);
  const meta = document.getElementById(`logMeta-${wid}`);
  if (!listEl || !ls) return;

  const notice = ls.restartNotice
    ? `进程已切换 ${ls.restartNotice.from}→${ls.restartNotice.to}（缓冲重新计数） · `
    : "";
  meta.textContent = ls.error
    ? "日志接口错误：" + ls.error
    : `${notice}进程 ${shortBoot(ls.bootId)} · 缓冲显示 ${ls.items.length} 条`;

  let items = ls.items.slice().reverse();
  if (ls.filter !== "ALL") items = items.filter((r) => r.level === ls.filter);

  listEl.innerHTML = "";
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "log-empty";
    empty.textContent = ls.initialized ? "当前过滤下暂无日志（DEBUG 级别下 DEBUG 行才会产生）" : "等待日志…";
    listEl.appendChild(empty);
    return;
  }

  const frag = document.createDocumentFragment();
  for (const r of items.slice(0, 200)) {
    const line = document.createElement("div");
    line.className = "log-line " + r.level + ((r.message || "").includes("[level-control]") ? " control" : "");
    const t = document.createElement("span");
    t.className = "t";
    t.textContent = shortTime(r.logger_ts || r.ts);
    const lvl = document.createElement("span");
    lvl.className = "lvl";
    lvl.textContent = r.level;
    const msg = document.createElement("span");
    msg.className = "msg";
    msg.textContent = r.message;
    line.append(t, lvl, msg);
    frag.appendChild(line);
  }
  listEl.appendChild(frag);
}

// ------------------------------------------------------------ 时钟 & 轮询
setInterval(() => {
  const clock = document.getElementById("serverClock");
  const d = new Date(Date.now() + state.serverOffsetMs);
  clock.textContent = "服务器时间 " + d.toISOString().slice(11, 19) + " UTC";
}, 500);

refreshOverview();
setInterval(refreshOverview, 1000);
setInterval(() => state.workerIds.forEach((id) => loadLogs(id)), 2000);
