/* ==========================================================================
 * J.A.R.V.I.S. 控制台 app.js — 零依赖纯静态，对接契约 REST(1.11) + WS(1.12)
 *
 * ── 事件 → DOM 映射表（README，先于代码锁定） ────────────────────────────────
 *
 * [WS 服务端 → 客户端]
 *  type=system            → #sys-cpu/#sys-mem/#sys-disk 百分比文本；
 *                           #sys-codex 文本+类(ok|unknown|error)；#sys-tasks 数字；
 *                           data.active_tasks>0 → #reactor.active（圆环加速旋转）
 *  type=task_started      → 若 task.session_id == 当前会话：在 #chat-stream 建
 *                           .task-block（<details.exec-log open> 执行区 + .msg.jarvis 占位气泡）；
 *                           刷新右栏「运行中」(#list-running)
 *  type=task_event        → 写入对应 .task-block 的 .exec-body（块未建则先入内存缓冲）：
 *                             event.item.type=command_execution → .ev-cmd（"$ command"）
 *                                 item.completed 时追加 .ev-out（输出尾部）+ .ev-status（exit code）
 *                             event.item.type=agent_message     → .ev-say（中间消息文本）
 *                             event.item.type=mcp_tool_call     → .ev-mcp（"⚙ MCP 工具名"）
 *                             其余 item 类型 / 其余 event 类型    → .ev-misc（仅显示 type 名）
 *                             event.type=turn.completed         → 暂存 usage 备显
 *  type=task_done         → .msg.jarvis 占位填入 result（换行+```代码块渲染）；
 *                           status=failed/cancelled → 气泡加 .failed；
 *                           .exec-log 折叠（去 open）+ summary 改"执行过程 // N 条事件"；
 *                           气泡下追加 .usage-line（tokens in/cached/out）；
 *                           刷新 #list-running/#list-history/#session-list
 *  type=approval_request  → 入授权队列 → #approval-overlay 显示红色警示卡：
 *                           #approval-action / #approval-detail / #approval-risk(.critical)
 *  type=approval_resolved → 从队列移除该 approval；队列空则隐藏 #approval-overlay
 *  type=cron_changed      → 重新 GET /api/cron 渲染 #list-cron
 *  type=pong              → 心跳确认（仅记录 lastPong，无 DOM）
 *
 * [客户端 → 服务端]  每 20s 发 {"type":"ping"}；其余操作全走 REST
 *
 * [REST → DOM]
 *  GET  /api/system                    → 同 type=system
 *  GET  /api/sessions                  → #session-list（.session-item，点击切换）
 *  GET  /api/sessions/{id}/messages    → #chat-stream 历史气泡（user 右蓝 / jarvis 左青）
 *  POST /api/chat                      → 乐观追加用户气泡；409 → toast "BUSY"
 *  GET  /api/tasks?limit=50            → running → #list-running（带取消按钮）；
 *                                        其余 → #list-history（点击展开 result/error）
 *  POST /api/tasks/{id}/cancel         → 刷新运行中列表
 *  GET  /api/approvals?status=pending  → 启动时补漏入授权队列
 *  POST /api/approvals/{id}/decide     → 队列出列，显示下一条
 *  GET/POST/PATCH/DELETE /api/cron(+/run) → #list-cron + #cron-form（422 → #cron-msg）
 *
 * [认证] localStorage("jarvis_token")；任意 fetch 401 或 WS close code=4401
 *        → 清 token → 显示 #token-overlay 重新输入
 * ========================================================================== */

"use strict";

/* ---------- 工具函数 ---------- */
const $ = (sel) => document.querySelector(sel);

function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

/* 简易富文本：```代码块``` + `行内代码` + 换行（无 marked，按契约简单处理） */
function renderRich(text) {
  const parts = String(text ?? "").split("```");
  let html = "";
  for (let i = 0; i < parts.length; i++) {
    if (i % 2 === 1) { // 代码块，首行可能是语言名
      let code = parts[i];
      const nl = code.indexOf("\n");
      if (nl > -1 && nl < 24 && /^[\w+#.-]*$/.test(code.slice(0, nl).trim())) {
        code = code.slice(nl + 1);
      }
      html += `<pre class="codeblock">${esc(code)}</pre>`;
    } else {
      html += esc(parts[i])
        .replace(/`([^`\n]+)`/g, "<code>$1</code>")
        .replace(/\n/g, "<br>");
    }
  }
  return html;
}

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function snippet(s, n = 64) {
  s = String(s ?? "").replace(/\s+/g, " ").trim();
  return s.length > n ? s.slice(0, n) + "…" : s;
}

function toast(text, warn = false) {
  const el = document.createElement("div");
  el.className = "toast" + (warn ? " warn" : "");
  el.textContent = text;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

/* ---------- 全局状态 ---------- */
const state = {
  token: "",
  authed: false,
  sessionId: null,     // 当前会话；null = 新会话（首条消息后由服务端分配）
  approvals: [],       // 授权请求 FIFO 队列
  editingCronId: null, // 非空 = cron 表单处于编辑模式
  activeTab: "cron",
};
let ws = null;
let pingTimer = null;
let reconnectTimer = null;
let lastPong = 0;
const taskBlocks = new Map();   // task_id -> {root, execBody, bubble, details, summary, items:Map, usage}
const pendingEvents = new Map(); // task_id -> [event,...]（块建立前的事件缓冲）

/* ---------- 认证与 REST 封装 ---------- */
async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (state.token) headers["Authorization"] = "Bearer " + state.token;
  let body = opts.body;
  if (body !== undefined && typeof body !== "string") {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(body);
  }
  const res = await fetch(path, Object.assign({}, opts, { headers, body }));
  if (res.status === 401) { authFail(); const e = new Error("unauthorized"); e.status = 401; throw e; }
  return res;
}

async function apiJson(path, opts = {}) {
  const res = await api(path, opts);
  if (!res.ok) {
    let detail = "";
    try { detail = (await res.json()).detail || ""; } catch (_) { /* 非 JSON 错误体 */ }
    const e = new Error(detail || ("HTTP " + res.status));
    e.status = res.status;
    throw e;
  }
  try { return await res.json(); } catch (_) { return null; }
}

function authFail() {
  state.authed = false;
  state.token = "";
  try { localStorage.removeItem("jarvis_token"); } catch (_) {}
  if (ws) { try { ws.onclose = null; ws.close(); } catch (_) {} ws = null; }
  if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
  setLink(false);
  showTokenOverlay("令牌失效，请重新输入");
}

function showTokenOverlay(err = "") {
  $("#token-overlay").classList.remove("hidden");
  $("#token-error").textContent = err;
  $("#token-input").value = "";
  $("#token-input").focus();
}

/* ---------- boot 开机动画（"J.A.R.V.I.S. 系统在线" 逐字打出） ---------- */
const BOOT_LINES = [
  "> J.A.R.V.I.S. 内核镜像加载 .............. OK",
  "> 反应堆输出功率 100% .................... 稳定",
  "> 神经链路 / 全息矩阵同步 ................ 完成",
  "> 安全协议 / 授权网关 .................... 在线",
  "> 接入 codex 推理引擎 .................... 就绪",
];
const BOOT_FINAL = "J.A.R.V.I.S. 系统在线";
let bootDone = false;

function runBoot() {
  const box = $("#boot-text");
  const overlay = $("#boot-overlay");
  let li = 0, ci = 0, lineEl = null;
  let finished = false;

  function finish() {
    if (finished) return;
    finished = true;
    bootDone = true;
    overlay.classList.add("fade");
    setTimeout(() => overlay.remove(), 650);
    init();
  }
  overlay.addEventListener("click", finish);

  function typeFinal() {
    const el = document.createElement("div");
    el.className = "boot-line boot-final";
    box.appendChild(el);
    let i = 0;
    const t = setInterval(() => {
      if (finished) { clearInterval(t); return; }
      el.textContent = BOOT_FINAL.slice(0, ++i);
      if (i >= BOOT_FINAL.length) {
        clearInterval(t);
        el.insertAdjacentHTML("beforeend", '<span class="boot-cursor">▍</span>');
        setTimeout(finish, 700);
      }
    }, 55);
  }

  const t = setInterval(() => {
    if (finished) { clearInterval(t); return; }
    if (li >= BOOT_LINES.length) { clearInterval(t); typeFinal(); return; }
    if (!lineEl) {
      lineEl = document.createElement("div");
      lineEl.className = "boot-line";
      box.appendChild(lineEl);
    }
    const line = BOOT_LINES[li];
    ci += 3; // 每帧 3 字符，干脆利落
    lineEl.textContent = line.slice(0, ci);
    if (ci >= line.length) { li++; ci = 0; lineEl = null; }
  }, 16);
}

/* ---------- 初始化 ---------- */
async function init() {
  let saved = "";
  try { saved = localStorage.getItem("jarvis_token") || ""; } catch (_) {}
  if (!saved) { showTokenOverlay(); return; }
  state.token = saved;
  try {
    const sys = await apiJson("/api/system");
    enterApp(sys);
  } catch (e) {
    if (e.status !== 401) showTokenOverlay("无法连接服务端: " + e.message);
    // 401 时 authFail() 已弹 token 层
  }
}

function enterApp(sys) {
  state.authed = true;
  $("#token-overlay").classList.add("hidden");
  $("#app").classList.remove("hidden");
  if (sys) renderSystem(sys);
  wsConnect();
  loadSessions();
  loadTasks();
  loadCron();
  loadPendingApprovals();
  if (!state.sessionId) showChatHint();
}

/* ---------- WebSocket ---------- */
function wsConnect() {
  if (ws) { try { ws.onclose = null; ws.close(); } catch (_) {} }
  const proto = location.protocol === "https:" ? "wss" : "ws";
  // 契约 1.12（安全修订）：token 不走 URL query（防进访问日志），连接后首条消息认证
  ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onopen = () => {
    ws.send(JSON.stringify({ type: "auth", token: state.token }));
    if (pingTimer) clearInterval(pingTimer);
    pingTimer = setInterval(() => {
      if (ws && ws.readyState === WebSocket.OPEN) ws.send('{"type":"ping"}');
    }, 20000);
  };

  ws.onmessage = (e) => {
    let msg;
    try { msg = JSON.parse(e.data); } catch (_) { return; }
    switch (msg.type) {
      case "auth_ok":           setLink(true); break;
      case "system":            renderSystem(msg.data); break;
      case "task_started":      onTaskStarted(msg.task); break;
      case "task_event":        onTaskEvent(msg.task_id, msg.event); break;
      case "task_done":         onTaskDone(msg); break;
      case "approval_request":  pushApproval(msg.approval); break;
      case "approval_resolved": resolveApproval(msg.approval); break;
      case "cron_changed":      loadCron(); break;
      case "pong":              lastPong = Date.now(); break;
      default: break; // 未知消息类型容忍
    }
  };

  ws.onclose = (ev) => {
    setLink(false);
    if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
    if (ev.code === 4401) { authFail(); return; }
    if (state.authed && state.token) {
      clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(wsConnect, 2500);
    }
  };
  ws.onerror = () => { try { ws.close(); } catch (_) {} };
}

function setLink(on) {
  const el = $("#sys-link");
  el.className = on ? "link-on" : "link-off";
  el.title = on ? "WS 已连接" : "WS 断开";
}

/* ---------- 系统状态条 + 反应堆 ---------- */
function renderSystem(d) {
  if (!d) return;
  const pct = (v) => (v === null || v === undefined ? "--%" : Math.round(v) + "%");
  $("#sys-cpu").textContent = pct(d.cpu_percent);
  $("#sys-mem").textContent = pct(d.mem_percent);
  $("#sys-disk").textContent = pct(d.disk_percent);
  const codex = $("#sys-codex");
  codex.textContent = (d.codex_auth || "--").toUpperCase();
  codex.className = d.codex_auth || "";
  $("#sys-tasks").textContent = d.active_tasks ?? 0;
  setReactor((d.active_tasks ?? 0) > 0);
}

function setReactor(active) {
  $("#reactor").classList.toggle("active", !!active);
}

/* ---------- 会话 ---------- */
async function loadSessions() {
  let list;
  try { list = await apiJson("/api/sessions"); } catch (_) { return; }
  const box = $("#session-list");
  box.innerHTML = "";
  if (!list.length) {
    box.innerHTML = '<div class="empty-hint">// 暂无会话</div>';
    return;
  }
  for (const s of list) {
    const el = document.createElement("div");
    el.className = "session-item" + (s.id === state.sessionId ? " active" : "");
    el.innerHTML = `<div class="s-title">${esc(s.title || "(未命名会话)")}</div>
      <div class="s-time">${esc(fmtTime(s.updated_at || s.created_at))}</div>`;
    el.addEventListener("click", () => selectSession(s.id));
    box.appendChild(el);
  }
}

async function selectSession(id) {
  state.sessionId = id;
  taskBlocks.clear();
  const stream = $("#chat-stream");
  stream.innerHTML = "";
  closeDrawers();
  loadSessions(); // 刷新高亮
  let msgs;
  try { msgs = await apiJson(`/api/sessions/${id}/messages`); } catch (_) { return; }
  for (const m of msgs) appendMsg(m.role, m.content, m.created_at);
  scrollChat(true);
}

function newSession() {
  state.sessionId = null;
  taskBlocks.clear();
  $("#chat-stream").innerHTML = "";
  showChatHint();
  loadSessions();
  closeDrawers();
  $("#chat-input").focus();
}

function showChatHint() {
  $("#chat-stream").innerHTML =
    '<div class="chat-hint">// 新会话待命 — 输入指令，贾维斯随时效劳</div>';
}

/* ---------- 对话渲染 ---------- */
function appendMsg(role, content, ts) {
  const hint = $("#chat-stream .chat-hint");
  if (hint) hint.remove();
  const el = document.createElement("div");
  el.className = "msg " + (role === "user" ? "user" : "jarvis");
  el.innerHTML = renderRich(content) +
    (ts ? `<div class="msg-time">${esc(fmtTime(ts))}</div>` : "");
  $("#chat-stream").appendChild(el);
  scrollChat();
  return el;
}

function scrollChat(force = false) {
  const s = $("#chat-stream");
  const nearBottom = s.scrollHeight - s.scrollTop - s.clientHeight < 180;
  if (force || nearBottom) s.scrollTop = s.scrollHeight;
}

async function sendMessage() {
  const input = $("#chat-input");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  appendMsg("user", text, new Date().toISOString());
  try {
    const body = { message: text };
    if (state.sessionId) body.session_id = state.sessionId;
    const r = await apiJson("/api/chat", { method: "POST", body });
    if (!state.sessionId) {
      state.sessionId = r.session_id;
      loadSessions();
    }
    ensureTaskBlock({ id: r.task_id, session_id: r.session_id });
  } catch (e) {
    if (e.status === 409) toast("该会话已有任务执行中 // BUSY", true);
    else if (e.status !== 401) toast("发送失败: " + e.message, true);
  }
}

/* ---------- 任务执行块（执行过程折叠区 + 回复气泡） ---------- */
function ensureTaskBlock(task) {
  if (!task || !task.id) return null;
  if (taskBlocks.has(task.id)) return taskBlocks.get(task.id);
  if (!task.session_id || task.session_id !== state.sessionId) return null; // cron/他会话任务不进对话流

  const hint = $("#chat-stream .chat-hint");
  if (hint) hint.remove();

  const root = document.createElement("div");
  root.className = "task-block";
  root.innerHTML =
    `<details class="exec-log" open>
       <summary><span class="spin">◌</span> 执行过程 // EXECUTING</summary>
       <div class="exec-body"></div>
     </details>
     <div class="msg jarvis pending"><span class="cursor-blink">▍</span></div>`;
  $("#chat-stream").appendChild(root);
  scrollChat();

  const blk = {
    root,
    details: root.querySelector("details"),
    summary: root.querySelector("summary"),
    execBody: root.querySelector(".exec-body"),
    bubble: root.querySelector(".msg"),
    items: new Map(), // codex item.id -> DOM（item.started/completed 去重合并）
    usage: null,
    count: 0,
  };
  taskBlocks.set(task.id, blk);

  const buf = pendingEvents.get(task.id);
  if (buf) {
    for (const ev of buf) appendExecEvent(blk, ev);
    pendingEvents.delete(task.id);
  }
  return blk;
}

function onTaskStarted(task) {
  ensureTaskBlock(task);
  loadTasks();
  setReactor(true);
}

function onTaskEvent(taskId, ev) {
  if (!ev) return;
  const blk = taskBlocks.get(taskId);
  if (blk) { appendExecEvent(blk, ev); return; }
  // 块尚未建立（如 WS 比 POST /api/chat 响应先到）→ 缓冲
  const buf = pendingEvents.get(taskId) || [];
  buf.push(ev);
  if (buf.length > 800) buf.shift();
  pendingEvents.set(taskId, buf);
}

/* codex 原始事件 → 执行区 DOM（契约 1.14 渲染规则） */
function appendExecEvent(blk, ev) {
  blk.count++;
  const type = ev.type || "";

  if (type === "item.started" || type === "item.completed" || type === "item.updated") {
    const item = ev.item || {};
    switch (item.type) {
      case "command_execution": {
        let el = item.id !== undefined ? blk.items.get(item.id) : null;
        if (!el) {
          el = document.createElement("div");
          el.className = "ev ev-cmd";
          el.innerHTML =
            `<span class="prompt">$</span> ${esc(item.command || "")}` +
            `<span class="ev-status running">⟳</span><pre class="ev-out hidden"></pre>`;
          if (item.id !== undefined) blk.items.set(item.id, el);
          blk.execBody.appendChild(el);
        }
        if (type === "item.completed") {
          const st = el.querySelector(".ev-status");
          const ok = item.exit_code === 0;
          st.className = "ev-status " + (ok ? "ok" : "bad");
          st.textContent = ok ? "✓ exit 0" : "✗ exit " + (item.exit_code ?? "?");
          const out = String(item.aggregated_output || "").trimEnd();
          if (out) {
            const pre = el.querySelector(".ev-out");
            pre.textContent = out.length > 4000 ? "…" + out.slice(-4000) : out;
            pre.classList.remove("hidden");
          }
        }
        break;
      }
      case "agent_message": {
        if (type === "item.completed" && item.text) {
          const el = document.createElement("div");
          el.className = "ev ev-say";
          el.textContent = item.text;
          blk.execBody.appendChild(el);
        }
        break;
      }
      case "mcp_tool_call": {
        let el = item.id !== undefined ? blk.items.get(item.id) : null;
        if (!el) {
          const name = item.tool || item.tool_name || item.name || "(unknown)";
          el = document.createElement("div");
          el.className = "ev ev-mcp";
          el.textContent = `⚙ MCP 工具调用: ${name}`;
          if (item.id !== undefined) blk.items.set(item.id, el);
          blk.execBody.appendChild(el);
        }
        if (type === "item.completed") el.textContent += "  ✓";
        break;
      }
      default: { // 未知 item 类型：原样显示 type（reasoning 等）
        if (type !== "item.completed" || !blk.items.has("misc_" + item.id)) {
          const el = document.createElement("div");
          el.className = "ev ev-misc";
          el.textContent = `[${item.type || "unknown"}]`;
          if (item.id !== undefined) blk.items.set("misc_" + item.id, el);
          blk.execBody.appendChild(el);
        }
        break;
      }
    }
  } else if (type === "turn.completed") {
    blk.usage = ev.usage || null;
  } else if (type === "thread.started" || type === "turn.started") {
    const el = document.createElement("div");
    el.className = "ev ev-misc";
    el.textContent = `// ${type}`;
    blk.execBody.appendChild(el);
  } else { // 未知事件类型：显示 type
    const el = document.createElement("div");
    el.className = "ev ev-misc";
    el.textContent = `[${type || "unknown"}]`;
    blk.execBody.appendChild(el);
  }

  blk.execBody.scrollTop = blk.execBody.scrollHeight;
  scrollChat();
}

function onTaskDone(msg) {
  const blk = taskBlocks.get(msg.task_id);
  pendingEvents.delete(msg.task_id);
  if (blk) {
    const status = msg.status || "done";
    const result = msg.result || "";
    blk.bubble.classList.remove("pending");
    if (status === "done") {
      blk.bubble.innerHTML = renderRich(result || "(无输出)");
    } else {
      blk.bubble.classList.add("failed");
      blk.bubble.innerHTML =
        `<b>${status === "cancelled" ? "⊘ 任务已取消" : "✗ 任务失败"}</b>` +
        (result ? "<br>" + renderRich(result) : "");
    }
    // 折叠执行区
    blk.details.removeAttribute("open");
    blk.summary.innerHTML = `执行过程 // ${blk.count} 条事件`;
    // usage tokens
    const usage = (msg.usage && Object.keys(msg.usage).length ? msg.usage : blk.usage) || {};
    if (Object.keys(usage).length) {
      const u = document.createElement("div");
      u.className = "usage-line";
      u.textContent =
        `⏚ tokens — in:${usage.input_tokens ?? 0} (cached:${usage.cached_input_tokens ?? 0}) ` +
        `out:${usage.output_tokens ?? 0}`;
      blk.root.appendChild(u);
    }
    scrollChat();
  }
  loadTasks();
  loadSessions(); // 会话 title/updated_at 可能已更新
}

/* ---------- 右栏：运行中 / 历史 ---------- */
async function loadTasks() {
  let tasks;
  try { tasks = await apiJson("/api/tasks?limit=50"); } catch (_) { return; }
  const running = tasks.filter((t) => t.status === "running");
  renderRunning(running);
  renderHistory(tasks.filter((t) => t.status !== "running"));
  $("#sys-tasks").textContent = running.length;
  setReactor(running.length > 0);
}

function renderRunning(list) {
  const box = $("#list-running");
  box.innerHTML = "";
  if (!list.length) {
    box.innerHTML = '<div class="empty-hint">// 当前没有运行中的任务</div>';
    return;
  }
  for (const t of list) {
    const el = document.createElement("div");
    el.className = "pitem";
    el.innerHTML =
      `<div class="p-title"><span class="p-name">${esc(snippet(t.prompt, 46))}</span>
         <span class="badge running">RUN</span></div>
       <div class="p-sub">${esc(t.source || "")} · ${esc(fmtTime(t.started_at))}</div>
       <div class="p-actions"><button class="btn btn-deny">取消</button></div>`;
    el.querySelector("button").addEventListener("click", async () => {
      try {
        await apiJson(`/api/tasks/${t.id}/cancel`, { method: "POST" });
        toast("已发送取消指令");
      } catch (e) {
        if (e.status === 404) toast("任务已不在运行", true);
        else if (e.status !== 401) toast("取消失败: " + e.message, true);
      }
      loadTasks();
    });
    box.appendChild(el);
  }
}

function renderHistory(list) {
  const box = $("#list-history");
  box.innerHTML = "";
  if (!list.length) {
    box.innerHTML = '<div class="empty-hint">// 暂无历史任务</div>';
    return;
  }
  for (const t of list) {
    const el = document.createElement("div");
    el.className = "pitem";
    el.innerHTML =
      `<div class="p-title"><span class="p-name">${esc(snippet(t.prompt, 46))}</span>
         <span class="badge ${esc(t.status)}">${esc((t.status || "").toUpperCase())}</span></div>
       <div class="p-sub">${esc(t.source || "")} · ${esc(fmtTime(t.finished_at || t.started_at))}</div>`;
    el.style.cursor = "pointer";
    el.addEventListener("click", () => {
      const exist = el.querySelector(".p-expand");
      if (exist) { exist.remove(); return; }
      const d = document.createElement("div");
      d.className = "p-expand";
      d.textContent = (t.status === "failed" ? (t.error || t.result) : t.result) || "(无记录)";
      el.appendChild(d);
    });
    box.appendChild(el);
  }
}

/* ---------- 右栏：定时任务 CRUD ---------- */
async function loadCron() {
  let jobs;
  try { jobs = await apiJson("/api/cron"); } catch (_) { return; }
  const box = $("#list-cron");
  box.innerHTML = "";
  if (!jobs.length) {
    box.innerHTML = '<div class="empty-hint">// 暂无定时任务，下方可新建</div>';
    return;
  }
  for (const j of jobs) {
    const on = !!j.enabled;
    const el = document.createElement("div");
    el.className = "pitem";
    el.innerHTML =
      `<div class="p-title"><span class="p-name">${esc(j.name)}</span>
         <span class="badge ${on ? "on" : "off"}">${on ? "启用" : "停用"}</span></div>
       <div class="p-sub"><span class="cron-expr">${esc(j.cron)}</span> ${esc(snippet(j.prompt, 50))}</div>
       <div class="p-sub">上次: ${esc(fmtTime(j.last_run_at) || "—")} ${esc(j.last_status || "")}</div>
       <div class="p-actions">
         <button class="btn" data-act="run">▶ 运行</button>
         <button class="btn" data-act="toggle">${on ? "停用" : "启用"}</button>
         <button class="btn" data-act="edit">✎ 编辑</button>
         <button class="btn btn-deny" data-act="del">✕ 删除</button>
       </div>`;
    el.querySelector('[data-act="run"]').addEventListener("click", async () => {
      try { await apiJson(`/api/cron/${j.id}/run`, { method: "POST" }); toast(`已触发「${j.name}」`); }
      catch (e) { if (e.status !== 401) toast("触发失败: " + e.message, true); }
    });
    el.querySelector('[data-act="toggle"]').addEventListener("click", async () => {
      try { await apiJson(`/api/cron/${j.id}`, { method: "PATCH", body: { enabled: on ? 0 : 1 } }); loadCron(); }
      catch (e) { if (e.status !== 401) toast("更新失败: " + e.message, true); }
    });
    el.querySelector('[data-act="edit"]').addEventListener("click", () => {
      state.editingCronId = j.id;
      $("#cron-name").value = j.name || "";
      $("#cron-expr").value = j.cron || "";
      $("#cron-prompt").value = j.prompt || "";
      $("#cron-form-title").textContent = `✎ 编辑：${j.name}`;
      $("#cron-submit").textContent = "保存";
      $("#cron-cancel-edit").classList.remove("hidden");
    });
    el.querySelector('[data-act="del"]').addEventListener("click", async () => {
      if (!confirm(`确认删除定时任务「${j.name}」？`)) return;
      try { await apiJson(`/api/cron/${j.id}`, { method: "DELETE" }); loadCron(); }
      catch (e) { if (e.status !== 401) toast("删除失败: " + e.message, true); }
    });
    box.appendChild(el);
  }
}

function resetCronForm() {
  state.editingCronId = null;
  $("#cron-name").value = "";
  $("#cron-expr").value = "";
  $("#cron-prompt").value = "";
  $("#cron-form-title").textContent = "＋ 新建定时任务";
  $("#cron-submit").textContent = "添加";
  $("#cron-cancel-edit").classList.add("hidden");
  $("#cron-msg").textContent = "";
}

async function submitCronForm() {
  const name = $("#cron-name").value.trim();
  const cron = $("#cron-expr").value.trim();
  const prompt = $("#cron-prompt").value.trim();
  const msgEl = $("#cron-msg");
  if (!name || !cron || !prompt) { msgEl.textContent = "名称 / cron / 指令 三项必填"; return; }
  try {
    if (state.editingCronId) {
      await apiJson(`/api/cron/${state.editingCronId}`, { method: "PATCH", body: { name, cron, prompt } });
    } else {
      await apiJson("/api/cron", { method: "POST", body: { name, cron, prompt } });
    }
    resetCronForm();
    loadCron();
    toast("定时任务已保存");
  } catch (e) {
    if (e.status === 422) msgEl.textContent = "cron 表达式非法（需 5 段：分 时 日 月 周）";
    else if (e.status !== 401) msgEl.textContent = "保存失败: " + e.message;
  }
}

/* ---------- 授权红卡队列 ---------- */
async function loadPendingApprovals() {
  let list;
  try { list = await apiJson("/api/approvals?status=pending"); } catch (_) { return; }
  for (const a of list.slice().reverse()) pushApproval(a); // 旧的先处理
}

function pushApproval(a) {
  if (!a || state.approvals.some((x) => x.id === a.id)) return;
  state.approvals.push(a);
  renderApproval();
}

function resolveApproval(a) {
  if (!a) return;
  state.approvals = state.approvals.filter((x) => x.id !== a.id);
  renderApproval();
}

function renderApproval() {
  const overlay = $("#approval-overlay");
  const cur = state.approvals[0];
  if (!cur) { overlay.classList.add("hidden"); return; }
  overlay.classList.remove("hidden");
  $("#approval-action").textContent = cur.action || "(未注明动作)";
  $("#approval-detail").textContent = cur.detail || "(无明细)";
  const risk = $("#approval-risk");
  risk.textContent = (cur.risk_level || "high").toUpperCase();
  risk.className = "risk-badge" + (cur.risk_level === "critical" ? " critical" : "");
  $("#approval-queue-hint").textContent =
    state.approvals.length > 1 ? `队列中还有 ${state.approvals.length - 1} 条待审批` : "";
}

async function decideApproval(decision) {
  const cur = state.approvals[0];
  if (!cur) return;
  const btns = [$("#approval-approve"), $("#approval-deny")];
  btns.forEach((b) => (b.disabled = true));
  try {
    await apiJson(`/api/approvals/${cur.id}/decide`, { method: "POST", body: { decision } });
    toast(decision === "approved" ? "已批准 // APPROVED" : "已拒绝 // DENIED");
  } catch (e) {
    if (e.status === 409) toast("该请求已被处理", true);
    else if (e.status !== 401) toast("操作失败: " + e.message, true);
  }
  // 按 id 幂等删除：WS approval_resolved 可能先于 REST 响应到达并已删除当前项，
  // 此时 shift() 会误删下一条待审批（双删竞态）
  state.approvals = state.approvals.filter((x) => x.id !== cur.id);
  renderApproval();
  btns.forEach((b) => (b.disabled = false));
}

/* ---------- Tab / 抽屉 ---------- */
function switchTab(name) {
  state.activeTab = name;
  document.querySelectorAll(".panel-tabs .tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.tab === name));
  ["cron", "running", "history"].forEach((p) =>
    $("#pane-" + p).classList.toggle("hidden", p !== name));
}

function togglePanel(which) {
  const panel = which === "sessions" ? $("#sessions-panel") : $("#tasks-panel");
  const other = which === "sessions" ? $("#tasks-panel") : $("#sessions-panel");
  other.classList.remove("open");
  panel.classList.toggle("open");
  $("#backdrop").classList.toggle("hidden", !panel.classList.contains("open"));
}

function closeDrawers() {
  $("#sessions-panel").classList.remove("open");
  $("#tasks-panel").classList.remove("open");
  $("#backdrop").classList.add("hidden");
}

/* ---------- 事件绑定 ---------- */
document.addEventListener("DOMContentLoaded", () => {
  runBoot();

  // token 层
  $("#token-submit").addEventListener("click", async () => {
    const v = $("#token-input").value.trim();
    if (!v) { $("#token-error").textContent = "令牌不能为空"; return; }
    state.token = v;
    try {
      const sys = await apiJson("/api/system");
      try { localStorage.setItem("jarvis_token", v); } catch (_) {}
      enterApp(sys);
    } catch (e) {
      state.token = "";
      $("#token-error").textContent = e.status === 401 ? "令牌无效 // ACCESS DENIED" : "连接失败: " + e.message;
    }
  });
  $("#token-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); $("#token-submit").click(); }
  });

  // 对话
  $("#chat-form").addEventListener("submit", (e) => { e.preventDefault(); sendMessage(); });
  $("#chat-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });
  $("#btn-new-session").addEventListener("click", newSession);

  // 授权
  $("#approval-approve").addEventListener("click", () => decideApproval("approved"));
  $("#approval-deny").addEventListener("click", () => decideApproval("denied"));

  // 右栏
  document.querySelectorAll(".panel-tabs .tab").forEach((t) =>
    t.addEventListener("click", () => switchTab(t.dataset.tab)));
  $("#cron-form").addEventListener("submit", (e) => { e.preventDefault(); submitCronForm(); });
  $("#cron-cancel-edit").addEventListener("click", resetCronForm);

  // 抽屉
  $("#btn-sessions-toggle").addEventListener("click", () => togglePanel("sessions"));
  $("#btn-tasks-toggle").addEventListener("click", () => togglePanel("tasks"));
  $("#backdrop").addEventListener("click", closeDrawers);
});
