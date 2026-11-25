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

  state.logs[wid] = { items: [], lastId: 0, auto: true, filter: "ALL", error: null, initialized: false };

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
async function loadLogs(wid, force = false) {
  const ls = state.logs[wid];
  if (!ls) return;
  if (!force && ls.auto === false) return;
  try {
    let data;
    if (!ls.initialized) {
      data = await api(`/api/workers/${wid}/logs?limit=200`);
      ls.items = data.logs || [];
      ls.initialized = true;
    } else {
      data = await api(`/api/workers/${wid}/logs?after_id=${ls.lastId}&limit=1000`);
      if (data.logs && data.logs.length) ls.items = ls.items.concat(data.logs);
      if (ls.items.length > 400) ls.items = ls.items.slice(-400);
    }
    ls.lastId = data.last_id || ls.lastId;
    ls.error = data.error || null;
    renderLogs(wid);
  } catch (err) {
    ls.error = err.message;
    renderLogs(wid);
  }
}

function renderLogs(wid) {
  const ls = state.logs[wid];
  const listEl = document.getElementById(`logList-${wid}`);
  const meta = document.getElementById(`logMeta-${wid}`);
  if (!listEl || !ls) return;

  meta.textContent = ls.error ? "日志接口错误：" + ls.error : `缓冲显示 ${ls.items.length} 条`;

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
