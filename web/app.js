/* ==========================================================================
 * 木木控制台 app.js — 零依赖纯静态，对接契约 REST(1.11) + WS(1.12)
 *
 * 2026-06-11 语音 HUD 改造：语音为主交互（#voice-orb 点击说话），键盘默认收起；
 * codex reasoning 事件（model_reasoning_summary=detailed）以打字机滚入 .think-stream；
 * 回复文字打字机上屏 + 语音发起的任务完成自动 TTS 播报。
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
 *                             event.item.type=reasoning         → .think-stream 打字机逐字滚入
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

/* ---------- 打字机引擎（思考流/回复上屏） ----------
 * 全局串行队列：同一时刻只有一段在打，保证 思考段1 → 段2 → 回复 的顺序；
 * 用 textContent 追加纯文本（天然防 XSS），富文本由调用方在 Promise 后替换。 */
const _twQueue = [];
let _twBusy = false;

function typewriteQueued(el, text, charsPerTick = 2, scrollEl = null) {
  return new Promise((resolve) => {
    _twQueue.push({ el, text: String(text ?? ""), charsPerTick, scrollEl, resolve });
    _twPump();
  });
}

function _twPump() {
  if (_twBusy) return;
  const job = _twQueue.shift();
  if (!job) return;
  _twBusy = true;
  const cur = document.createElement("span");
  const cursor = document.createElement("span");
  cursor.className = "cursor-blink";
  cursor.textContent = "▍";
  job.el.appendChild(cur);
  job.el.appendChild(cursor);
  let i = 0;
  const t = setInterval(() => {
    // 元素被移除（切会话清屏）或不可见（思考流被折叠 display:none）→ 立即补全收尾，
    // 不让后续队列任务（如回复气泡）被隐形阻塞数十秒
    if (!job.el.isConnected || job.el.offsetParent === null) { finish(); return; }
    i += job.charsPerTick;
    cur.textContent = job.text.slice(0, i);
    if (job.scrollEl) job.scrollEl.scrollTop = job.scrollEl.scrollHeight;
    scrollChat();
    if (i >= job.text.length) finish();
  }, 16);
  function finish() {
    clearInterval(t);
    cur.textContent = job.text;
    cursor.remove();
    _twBusy = false;
    job.resolve();
    _twPump();
  }
}

/* reasoning 摘要是 markdown 风（**标题** 等），思考流以纯文本滚动 */
function stripMd(s) {
  return String(s ?? "").replace(/\*\*([^*]*)\*\*/g, "$1").replace(/`([^`]*)`/g, "$1");
}

/* ---------- 全局状态 ---------- */
const state = {
  token: "",
  authed: false,
  sessionId: null,     // 当前会话；null = 新会话（首条消息后由服务端分配）
  approvals: [],       // 授权请求 FIFO 队列
  editingCronId: null, // 非空 = cron 表单处于编辑模式
  activeTab: "cron",
  speak: false,        // 朗读开关（localStorage jarvis_speak=1，契约 phase2 1.6）
  voiceTasks: new Set(), // 语音发起的 task_id：完成后无视朗读开关自动播报
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

/* ---------- boot 开机动画（"木木系统在线" 逐字打出） ---------- */
const BOOT_LINES = [
  "> 木木内核镜像加载 ...................... OK",
  "> 反应堆输出功率 100% .................... 稳定",
  "> 神经链路 / 全息矩阵同步 ................ 完成",
  "> 安全协议 / 授权网关 .................... 在线",
  "> 接入 codex 推理引擎 .................... 就绪",
  "> 语音链路 / 唤醒词「木木」 .............. 监听",
];
const BOOT_FINAL = "木木系统在线";
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
      case "auth_ok":           setLink(true); state.voiceTasks.clear(); break; // 重连后清残留（断线期间错过的 task_done）
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
    '<div class="chat-hint">// 新会话待命 — 点击下方反应堆开口下令，木木随时效劳</div>';
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

async function sendMessage(textArg, opts = {}) {
  // textArg 省略 = 从键盘输入框取；语音链路直接传转写文本（不经输入框）
  const input = $("#chat-input");
  const text = (textArg !== undefined ? textArg : input.value).trim();
  if (!text) return;
  if (textArg === undefined) input.value = "";
  appendMsg("user", text, new Date().toISOString());
  try {
    const body = { message: text };
    if (state.sessionId) body.session_id = state.sessionId;
    const r = await apiJson("/api/chat", { method: "POST", body });
    if (!state.sessionId) {
      state.sessionId = r.session_id;
      loadSessions();
    }
    if (opts.voice && r.task_id) state.voiceTasks.add(r.task_id);
    ensureTaskBlock({ id: r.task_id, session_id: r.session_id });
    setVoiceStatus("执行中 // WORKING");
  } catch (e) {
    if (e.status === 409) toast("该会话已有任务执行中 // BUSY", true);
    else if (e.status !== 401) toast("发送失败: " + e.message, true);
    setVoiceStatus("待命 // STANDBY");
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
    `<div class="think-stream hidden">
       <div class="think-head">◈ 思考 // REASONING</div>
       <div class="think-body"></div>
     </div>
     <details class="exec-log" open>
       <summary><span class="spin">◌</span> 执行过程 // EXECUTING</summary>
       <div class="exec-body"></div>
     </details>
     <div class="msg jarvis pending"><span class="cursor-blink">▍</span></div>`;
  $("#chat-stream").appendChild(root);
  scrollChat();

  const blk = {
    root,
    thinkWrap: root.querySelector(".think-stream"),
    thinkBody: root.querySelector(".think-body"),
    details: root.querySelector("details"),
    summary: root.querySelector("summary"),
    execBody: root.querySelector(".exec-body"),
    bubble: root.querySelector(".msg"),
    items: new Map(), // codex item.id -> DOM（item.started/completed 去重合并）
    usage: null,
    count: 0,
    thinkCount: 0,
  };
  // 思考区头部点击折叠/展开
  blk.thinkWrap.querySelector(".think-head").addEventListener("click", () =>
    blk.thinkWrap.classList.toggle("collapsed"));
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
      case "reasoning": {
        // codex 思考摘要（整段 item.completed，无增量）→ 思考流打字机逐字滚入
        if (type === "item.completed" && item.text) {
          blk.thinkWrap.classList.remove("hidden");
          blk.thinkCount++;
          const seg = document.createElement("div");
          seg.className = "think-seg";
          blk.thinkBody.appendChild(seg);
          setTaskStatus("思考中 // REASONING");
          typewriteQueued(seg, stripMd(item.text), 3, blk.thinkBody);
        }
        break;
      }
      default: { // 未知 item 类型：原样显示 type
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
  const isVoice = state.voiceTasks.has(msg.task_id);
  state.voiceTasks.delete(msg.task_id);
  if (blk) {
    const status = msg.status || "done";
    const result = msg.result || "";
    blk.bubble.classList.remove("pending");
    if (status === "done") {
      // 回复打字机上屏（纯文本），打完替换为富文本最终态（代码块/行内码渲染）
      blk.bubble.innerHTML = "";
      typewriteQueued(blk.bubble, result || "(无输出)", 2).then(() => {
        blk.bubble.innerHTML = renderRich(result || "(无输出)");
        scrollChat();
      });
    } else {
      blk.bubble.classList.add("failed");
      blk.bubble.innerHTML =
        `<b>${status === "cancelled" ? "⊘ 任务已取消" : "✗ 任务失败"}</b>` +
        (result ? "<br>" + renderRich(result) : "");
    }
    // 折叠执行区 + 思考流收起（头部可点击重新展开）
    blk.details.removeAttribute("open");
    blk.summary.innerHTML = `执行过程 // ${blk.count} 条事件`;
    if (blk.thinkCount > 0) {
      blk.thinkWrap.classList.add("collapsed");
      blk.thinkWrap.querySelector(".think-head").textContent =
        `◈ 思考过程 // ${blk.thinkCount} 段（点击展开）`;
    }
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
    setTaskStatus("待命 // STANDBY"); // 仅本会话任务回写状态字，且不打断录音/播报
    scrollChat();
  }
  // 自动播报：语音发起的任务必播（与 DOM 解耦——切会话清掉 blk 也照播）；
  // 键盘发起的按朗读开关、且仅限当前会话（契约 phase2 1.6）
  if ((msg.status || "done") === "done") {
    if (isVoice) speakText(summarize(msg.result || ""));
    else if (state.speak && blk) speakText(summarize(msg.result || ""));
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

/* ==========================================================================
 * Phase 2 语音（契约 phase2 计划 1.6，2026-06-11 语音 HUD 修订）：
 *   #voice-orb 点击开始 MediaRecorder(audio/webm) 录音（反应堆红脉冲+状态字），
 *   再点结束（30s 上限兜底）→ POST /api/voice/transcribe → 转写文本直接发送
 *   （不经输入框）；语音发起的任务完成自动 TTS 播报（无视朗读开关）。
 *   键盘输入是兜底：#kbd-toggle 展开 #chat-form。
 *   朗读开关（localStorage jarvis_speak=1）：键盘发起的 task_done → summarize()
 *   → POST /api/voice/tts → 播放木木音色 wav。
 * ========================================================================== */

/* 摘要规则（与 voice/daemon 一致，锁定）：去 markdown 符号，取前两句 */
function summarize(text) {
  let t = String(text ?? "");
  t = t.replace(/```[\s\S]*?```/g, " ");          // 代码块整段丢弃（没法朗读）
  t = t.replace(/`([^`\n]*)`/g, "$1");            // 行内代码去反引号
  t = t.replace(/!\[[^\]]*\]\([^)]*\)/g, " ");    // 图片语法丢弃
  t = t.replace(/\[([^\]]*)\]\([^)]*\)/g, "$1");  // 链接只留文字
  t = t.replace(/^\s*\|.*\|\s*$/gm, " ");         // 表格行整行丢弃
  t = t.replace(/^[\s>#*+-]+/gm, "");             // 行首引用/标题/列表符号
  t = t.replace(/[*_~#|]+/g, "");                 // 余下强调/表格符号
  t = t.replace(/\s+/g, " ").trim();
  if (!t) return "";
  const sentences = (t.match(/[^。！？!?；;]+[。！？!?；;]?/g) || [t])
    .map((s) => s.trim()).filter(Boolean);
  return sentences.slice(0, 2).join("").trim();
}

/* ----- 语音坞：点击说话 ----- */
const REC_MAX_MS = 30000; // 忘了点结束的兜底上限
let micRecorder = null, micChunks = [], micStream = null, micTimer = null, micBusy = false;
let micStarting = false; // getUserMedia await 窗口的重入守卫（双击 orb 防双路录音/麦克风泄漏）

function micSupported() {
  // 非安全上下文（http 非 localhost）/老浏览器没有 mediaDevices 或 MediaRecorder
  return !!(window.isSecureContext && navigator.mediaDevices &&
            navigator.mediaDevices.getUserMedia && window.MediaRecorder);
}

function setVoiceStatus(text) {
  const el = $("#voice-status");
  if (el) el.textContent = text;
}

/* 语音链路（录音/转写/播报）活跃时，任务事件不准乱写状态字——
 * 否则录音中任一 cron/他会话任务完成会把"聆听中"覆盖成"待命" */
function voiceLineBusy() {
  return !!(micRecorder || micStarting || micBusy ||
            (speakAudio && !speakAudio.paused && !speakAudio.ended));
}

function setTaskStatus(text) {
  if (!voiceLineBusy()) setVoiceStatus(text);
}

function setRecordingUI(on) {
  $("#voice-orb").classList.toggle("recording", on);
  $("#reactor").classList.toggle("recording", on); // 录音中反应堆变红脉冲（契约 1.6）
  if (on) setVoiceStatus("聆听中 // LISTENING（再点结束）");
}

/* orb 点击：空闲→开始录音；录音中→结束并转写 */
function orbToggle() {
  if (micBusy) return;
  if (micRecorder) { micStop(); return; }
  micStart();
}

async function micStart() {
  if (micBusy || micRecorder || micStarting) return;
  micStarting = true;
  try {
    micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (_) {
    toast("麦克风权限被拒绝，请在浏览器设置中允许", true);
    setVoiceStatus("待命 // STANDBY");
    return;
  } finally {
    micStarting = false;
  }
  micChunks = [];
  const mime = (window.MediaRecorder.isTypeSupported &&
                MediaRecorder.isTypeSupported("audio/webm")) ? "audio/webm" : "";
  micRecorder = mime ? new MediaRecorder(micStream, { mimeType: mime })
                     : new MediaRecorder(micStream); // Safari 给 mp4，服务端 av 都能解
  micRecorder.ondataavailable = (e) => { if (e.data && e.data.size) micChunks.push(e.data); };
  micRecorder.onstop = onMicStop;
  micRecorder.start();
  setRecordingUI(true);
  clearTimeout(micTimer);
  micTimer = setTimeout(micStop, REC_MAX_MS);
}

function micStop() {
  clearTimeout(micTimer);
  if (micRecorder && micRecorder.state !== "inactive") micRecorder.stop();
}

async function onMicStop() {
  setRecordingUI(false);
  if (micStream) { micStream.getTracks().forEach((t) => t.stop()); micStream = null; }
  const type = (micRecorder && micRecorder.mimeType) || "audio/webm";
  micRecorder = null;
  const blob = new Blob(micChunks, { type });
  micChunks = [];
  if (blob.size < 1000) { setVoiceStatus("待命 // STANDBY"); return; } // 误触/过短
  micBusy = true;
  const orb = $("#voice-orb");
  orb.disabled = true;
  setVoiceStatus("识别中 // TRANSCRIBING");
  try {
    const text = await transcribeBlob(blob);
    if (text) {
      await sendMessage(text, { voice: true }); // 转写文本直接发送（语音 HUD 修订）
    } else {
      toast("没听清，请再说一次", true);
      setVoiceStatus("待命 // STANDBY");
    }
  } catch (e) {
    if (e.status !== 401) toast("语音识别失败: " + e.message, true);
    setVoiceStatus("待命 // STANDBY");
  }
  micBusy = false;
  orb.disabled = false;
}

async function transcribeBlob(blob) {
  const fd = new FormData();
  fd.append("file", blob, "speech.webm");
  // 不走 api()：FormData 必须让浏览器自带 multipart boundary，不能手设 Content-Type
  const res = await fetch("/api/voice/transcribe", {
    method: "POST",
    headers: { "Authorization": "Bearer " + state.token },
    body: fd,
  });
  if (res.status === 401) { authFail(); const e = new Error("unauthorized"); e.status = 401; throw e; }
  if (!res.ok) {
    let detail = "";
    try { detail = (await res.json()).detail || ""; } catch (_) {}
    const e = new Error(detail || ("HTTP " + res.status)); e.status = res.status; throw e;
  }
  const data = await res.json();
  return String(data.text || "").trim();
}

/* ----- 朗读开关 + TTS 播放 ----- */
let speakAudio = null;

function setSpeak(on) {
  state.speak = !!on;
  try { localStorage.setItem("jarvis_speak", state.speak ? "1" : "0"); } catch (_) {}
  const btn = $("#btn-speak");
  btn.classList.toggle("on", state.speak);
  btn.textContent = state.speak ? "🔊" : "🔇";
  btn.title = state.speak ? "朗读已开启：任务完成木木音色播报" : "朗读已关闭（语音下令仍自动播报）";
}

async function speakText(text) {
  // 钳到 500 字：与服务端 /api/voice/tts 的 max_length 对齐（超长会 422 整段播不出）
  text = String(text || "").trim().slice(0, 500);
  if (!text) return;
  try {
    const res = await api("/api/voice/tts", { method: "POST", body: { text } });
    if (!res.ok) {
      toast(res.status === 503 ? "TTS worker 离线，无法朗读" : "朗读失败 HTTP " + res.status, true);
      return;
    }
    const url = URL.createObjectURL(await res.blob());
    if (speakAudio) {
      // 打断旧播报：pause 不触发 onended，blob URL 要在这里顺手回收
      try { speakAudio.pause(); } catch (_) {}
      if (speakAudio._url) { try { URL.revokeObjectURL(speakAudio._url); } catch (_) {} }
    }
    speakAudio = new Audio(url);
    speakAudio._url = url;
    setVoiceStatus("播报中 // SPEAKING");
    const cleanup = () => { URL.revokeObjectURL(url); setVoiceStatus("待命 // STANDBY"); };
    speakAudio.onended = cleanup;
    speakAudio.onerror = cleanup;
    await speakAudio.play();
  } catch (e) {
    if (e.status !== 401) toast("朗读失败: " + e.message, true);
    setVoiceStatus("待命 // STANDBY");
  }
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

  // 语音坞：点击说话（契约 phase2 1.6，语音 HUD 修订）
  const orb = $("#voice-orb");
  if (!micSupported()) {
    // 非安全上下文（局域网 http）/老浏览器 → orb 置灰，自动展开键盘兜底
    orb.disabled = true;
    orb.title = "需要 HTTPS（或 localhost）才能用浏览器麦克风";
    setVoiceStatus("麦克风不可用（需 localhost/HTTPS）· 已展开键盘");
    $("#chat-form").classList.remove("hidden");
  } else {
    orb.addEventListener("click", orbToggle);
  }
  $("#kbd-toggle").addEventListener("click", () =>
    $("#chat-form").classList.toggle("hidden"));

  // 语音：朗读开关（localStorage jarvis_speak）
  let speakSaved = "0";
  try { speakSaved = localStorage.getItem("jarvis_speak") || "0"; } catch (_) {}
  setSpeak(speakSaved === "1");
  $("#btn-speak").addEventListener("click", () => setSpeak(!state.speak));
});
