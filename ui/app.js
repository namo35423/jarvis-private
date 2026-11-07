// Jarvis console shell: websocket -> DOM. No framework, no build, no network.
//
// The brain is the single source of truth. This file never guesses state: it
// renders what arrives, and everything the user does is sent back as a message.

import { Orb } from "./orb.js";

const params = new URLSearchParams(location.search);
const MINI = params.get("mini") === "1";

const $ = (id) => document.getElementById(id);

const el = {
  app: $("app"),
  clock: $("clock"),
  model: $("model"),
  conn: $("conn"),
  connText: $("connText"),
  newBtn: $("newBtn"),
  micBtn: $("micBtn"),
  micText: $("micText"),
  settingsBtn: $("settingsBtn"),
  orb: $("orb"),
  orbHit: $("orbHit"),
  statusWord: $("statusWord"),
  statusDetail: $("statusDetail"),
  timers: $("timers"),
  stream: $("stream"),
  rail: $("rail"),
  form: $("composerForm"),
  input: $("input"),
  drawer: $("drawer"),
  drawerClose: $("drawerClose"),
  scrim: $("scrim"),
  toasts: $("toasts"),
  previewBtn: $("previewBtn"),
  setVoice: $("setVoice"),
  permAccess: $("permAccess"),
  permScreen: $("permScreen"),
};

const STATE_WORD = {
  idle: "ready",
  listening: "listening",
  thinking: "thinking",
  speaking: "speaking",
  working: "working",
  confirm: "confirm",
  muted: "muted",
  error: "error",
};

// The orb has no "confirm" mood; waiting on a spoken yes/no is listening.
const ORB_STATE = {
  idle: "idle",
  listening: "listening",
  thinking: "thinking",
  speaking: "speaking",
  working: "working",
  confirm: "listening",
  muted: "muted",
  error: "error",
};

// Kokoro ships these; the brain may send its own list in config.voice.voices,
// which wins. The current voice is always added so an unknown one still shows.
const FALLBACK_VOICES = [
  "af_heart", "af_bella", "af_nicole", "af_sarah", "af_sky", "af_nova", "af_aoede", "af_kore",
  "am_michael", "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_onyx", "am_puck",
  "bf_emma", "bf_isabella", "bf_alice", "bf_lily",
  "bm_george", "bm_fable", "bm_daniel", "bm_lewis",
];

const VOICE_LABELS = {
  m_jarvis_smooth: "ผู้ชายวัยหนุ่ม · ทุ้มเล็กน้อย (B)",
  m_young_clear: "ผู้ชายวัยหนุ่ม · เสียงโปร่ง (A)",
  m_teen_bright: "ผู้ชาย · สดใส (C)",
  m_mid_warm: "ผู้ชาย · ทุ้มอุ่น",
  m_elderly_deep: "ผู้ชายสูงวัย · เสียงทุ้ม",
};

const state = {
  connected: false,
  brainState: "idle",
  detail: "",
  config: {},
  status: {},
  turns: [],
  byId: new Map(),
  current: null,
  jobs: new Map(),
  confirm: null,
  timers: [],
  listening: true,
  caption: "",
};

/* ------------------------------------------------------------------ socket */

const WS_URL = (() => {
  const q = params.get("ws");
  if (q) return q;
  if (location.protocol === "http:" || location.protocol === "https:") {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${location.host}/ws`;
  }
  return "ws://127.0.0.1:8765/ws"; // file:// (the native shell loads the bundle directly)
})();

const API_BASE = (() => {
  if (location.protocol === "http:" || location.protocol === "https:") return location.origin;
  return "http://127.0.0.1:8765";
})();

let sock = null;
let retry = 0;
let retryTimer = 0;

function connect() {
  clearTimeout(retryTimer);
  try {
    sock = new WebSocket(WS_URL);
  } catch (err) {
    scheduleRetry();
    return;
  }
  sock.onopen = () => {
    retry = 0;
    setConnected(true);
  };
  sock.onclose = () => {
    setConnected(false);
    scheduleRetry();
  };
  sock.onerror = () => { /* onclose always follows; nothing useful to add here */ };
  sock.onmessage = (ev) => {
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch {
      return;
    }
    handle(msg);
  };
}

function scheduleRetry() {
  retry = Math.min(retry + 1, 8);
  const wait = Math.min(8000, 300 * 2 ** (retry - 1)) * (0.75 + Math.random() * 0.5);
  retryTimer = setTimeout(connect, wait);
}

function send(msg) {
  if (sock && sock.readyState === WebSocket.OPEN) {
    sock.send(JSON.stringify(msg));
    return true;
  }
  return false;
}

function setConnected(up) {
  state.connected = up;
  el.conn.classList.toggle("up", up);
  el.conn.classList.toggle("down", !up);
  el.connText.textContent = up ? "linked" : "reconnecting";
  if (!up) {
    Orb.setLevel(0);
    Orb.setState("idle");
  }
  paintStatus();
}

/* --------------------------------------------------------------- dispatch */

function handle(msg) {
  switch (msg.type) {
    case "hello":
      state.config = msg.config || {};
      state.voices = msg.voices || [];
      state.status = msg.status || {};
      applyConfig();
      applyStatus();
      loadHistory(msg.history || []);
      setBrainState(msg.state || "idle", null);
      break;
    case "state":
      setBrainState(msg.state, msg.detail);
      break;
    case "level":
      // Both sources drive the same visual; whichever is active is the one sending.
      Orb.setLevel(msg.value);
      break;
    case "transcript":
      onTranscript(msg);
      break;
    case "reply_delta":
      onDelta(msg);
      break;
    case "reply_done":
      onDone(msg);
      break;
    case "tool":
      onTool(msg);
      break;
    case "confirm_request":
      showConfirm(msg);
      break;
    case "confirm_done":
      dismissConfirm(msg.id);
      break;
    case "job":
      onJob(msg);
      break;
    case "notice":
      toast(msg.text, msg.level || "info");
      break;
    case "status":
      state.status = { ...state.status, ...msg };
      applyStatus();
      break;
    case "config":
      state.config = msg.config || {};
      state.voices = msg.voices || [];
      applyConfig();
      break;
    case "timers":
      state.timers = msg.items || [];
      renderTimers();
      break;
    default:
      break;
  }
}

function setBrainState(name, detail) {
  if (!STATE_WORD[name]) name = "idle";
  state.brainState = name;
  state.detail = detail || "";
  Orb.setState(ORB_STATE[name]);
  if (name !== "speaking" && name !== "listening") Orb.setLevel(0);
  paintStatus();
}

function paintStatus() {
  const word = state.connected ? STATE_WORD[state.brainState] : "reconnecting";
  el.statusWord.textContent = word;
  el.statusWord.classList.toggle("hot", state.connected &&
    ["listening", "speaking", "thinking", "working", "confirm"].includes(state.brainState));
  el.statusWord.classList.toggle("bad", state.brainState === "error" || !state.connected);
  el.statusWord.classList.toggle("off", state.brainState === "muted");

  if (MINI) {
    const cap = state.caption || state.detail;
    const showCaption = Boolean(cap) && ["speaking", "thinking", "working"].includes(state.brainState);
    el.statusDetail.textContent = showCaption ? cap : word;
    el.statusDetail.classList.toggle("caption", showCaption);
  } else {
    el.statusDetail.textContent = state.detail;
  }
}

/* ------------------------------------------------------------- transcript */

function makeTurn(id) {
  const node = document.createElement("article");
  node.className = "turn";
  node.innerHTML =
    '<div class="said user" hidden><span class="who">you</span><div class="body"></div></div>' +
    '<div class="tools" hidden></div>' +
    '<div class="said jarvis" hidden><span class="who">jarvis</span><div class="body"></div></div>';
  const turn = {
    id,
    node,
    userEl: node.querySelector(".said.user"),
    userBody: node.querySelector(".said.user .body"),
    toolsEl: node.querySelector(".tools"),
    replyEl: node.querySelector(".said.jarvis"),
    replyBody: node.querySelector(".said.jarvis .body"),
    text: "",
    streaming: false,
    tools: new Map(),
  };
  state.turns.push(turn);
  state.byId.set(id, turn);
  el.stream.appendChild(node);
  while (state.turns.length > 40) {
    const old = state.turns.shift();
    state.byId.delete(old.id);
    old.node.remove();
  }
  return turn;
}

function turnFor(id) {
  if (id && state.byId.has(id)) return state.byId.get(id);
  // A turn id is optional in practice; without one everything lands in the
  // turn already in flight rather than spawning a block per delta.
  if (!id && state.current) return state.current;
  const turn = makeTurn(id || `t${Date.now()}${Math.random().toString(36).slice(2, 6)}`);
  state.current = turn;
  return turn;
}

function onTranscript(msg) {
  let turn = turnFor(msg.turn);
  if (!msg.turn && (turn.text || !turn.userEl.hidden)) turn = makeTurn(`t${Date.now()}`);
  state.current = turn;
  turn.userEl.hidden = false;
  turn.userBody.textContent = msg.text || "";
  state.caption = msg.text || "";
  stick();
}

let pendingPaint = 0;
function onDelta(msg) {
  const turn = turnFor(msg.turn);
  state.current = turn;
  turn.text += msg.text || "";
  turn.streaming = true;
  state.caption = turn.text.slice(-160);
  if (!pendingPaint) {
    pendingPaint = requestAnimationFrame(() => {
      pendingPaint = 0;
      paintReply(turn);
      if (MINI) paintStatus();
      stick();
    });
  }
}

function onDone(msg) {
  const turn = turnFor(msg.turn);
  if (typeof msg.text === "string" && msg.text.length >= turn.text.length) turn.text = msg.text;
  turn.streaming = false;
  paintReply(turn);
  state.caption = turn.text.slice(-160);
  stick();
}

function paintReply(turn) {
  turn.replyEl.hidden = false;
  turn.replyBody.innerHTML = renderMarkdown(turn.text) + (turn.streaming ? '<span class="cursor"></span>' : "");
}

function loadHistory(history) {
  el.stream.innerHTML = "";
  state.turns = [];
  state.byId.clear();
  state.current = null;
  let turn = null;
  for (const item of history) {
    if (item.role === "user" || !turn) {
      turn = makeTurn(`h${state.turns.length}`);
    }
    if (item.role === "user") {
      turn.userEl.hidden = false;
      turn.userBody.textContent = item.text || "";
    } else {
      turn.text = item.text || "";
      paintReply(turn);
      turn = null;
    }
  }
  state.current = state.turns[state.turns.length - 1] || null;
  stick(true);
}

function stick(force) {
  const near = el.stream.scrollHeight - el.stream.scrollTop - el.stream.clientHeight < 120;
  if (force || near) el.stream.scrollTop = el.stream.scrollHeight;
}

/* ------------------------------------------------------------ tool chips */

const MARKS = { running: "·", ok: "✓", error: "✕" };

function onTool(msg) {
  const turn = turnFor(msg.turn);
  let chip = turn.tools.get(msg.id);
  if (!chip) {
    chip = document.createElement("div");
    chip.innerHTML = '<span class="mark"></span><span class="name"></span><span class="args"></span><span class="res"></span>';
    turn.tools.set(msg.id, chip);
    turn.toolsEl.hidden = false;
    turn.toolsEl.appendChild(chip);
  }
  const status = msg.status || "running";
  chip.className = `chip ${status}`;
  chip.querySelector(".mark").textContent = MARKS[status] || MARKS.running;
  chip.querySelector(".name").textContent = msg.name || "tool";
  chip.querySelector(".args").textContent = summariseArgs(msg.args);
  chip.querySelector(".res").textContent =
    status === "running" ? "…" : firstLine(msg.result || "", 90);
  stick();
}

function summariseArgs(args) {
  if (!args || typeof args !== "object") return "";
  const parts = [];
  for (const [k, v] of Object.entries(args)) {
    let s = typeof v === "string" ? v : JSON.stringify(v);
    if (s === undefined) continue;
    if (s.length > 34) s = s.slice(0, 33) + "…";
    parts.push(`${k}=${s}`);
    if (parts.length === 3) break;
  }
  return parts.join(" ");
}

function firstLine(text, n) {
  const line = String(text).split("\n").find((l) => l.trim()) || "";
  return line.length > n ? line.slice(0, n - 1) + "…" : line;
}

/* ------------------------------------------------------ confirm and jobs */

function showConfirm(msg) {
  dismissConfirm();
  const card = document.createElement("div");
  card.className = "card confirm";
  card.innerHTML =
    '<div class="cardTop"><span class="label">confirm</span><span class="label">yes / no</span></div>' +
    '<p class="q"></p><p class="detail"></p>' +
    '<div class="actions"><button class="btn label yes" type="button">yes</button>' +
    '<button class="btn label no" type="button">no</button></div>';
  card.querySelector(".q").textContent = msg.prompt || "Proceed?";
  const detail = card.querySelector(".detail");
  if (msg.detail) detail.textContent = msg.detail; else detail.remove();
  const answer = (approved) => {
    send({ type: "confirm_response", id: msg.id, approved });
    dismissConfirm(msg.id);
  };
  card.querySelector(".yes").addEventListener("click", () => answer(true));
  card.querySelector(".no").addEventListener("click", () => answer(false));
  state.confirm = { id: msg.id, card };
  el.rail.appendChild(card);
  if (!MINI) card.querySelector(".yes").focus();
}

function dismissConfirm(id) {
  if (!state.confirm) return;
  if (id && state.confirm.id !== id) return;
  state.confirm.card.remove();
  state.confirm = null;
}

function onJob(msg) {
  let job = state.jobs.get(msg.id);
  if (!job) {
    const card = document.createElement("div");
    card.className = "card job";
    card.innerHTML =
      '<div class="cardTop"><span class="label"><span class="spin">·</span> <span class="kind"></span></span>' +
      '<span class="label jstatus"></span></div>' +
      '<p class="q title"></p><p class="prog"></p>' +
      '<div class="actions"><button class="btn label cancel" type="button">cancel</button></div>';
    job = { card, timer: 0 };
    state.jobs.set(msg.id, job);
    el.rail.appendChild(card);
  }
  const card = job.card;
  card.className = `card job ${msg.status || "running"}`;
  card.querySelector(".kind").textContent = msg.kind || "job";
  card.querySelector(".jstatus").textContent = msg.status || "running";
  card.querySelector(".title").textContent = msg.title || "";
  card.querySelector(".prog").textContent = msg.result || msg.progress || "";
  card.querySelector(".spin").style.visibility = msg.status === "running" ? "visible" : "hidden";
  const cancel = card.querySelector(".cancel");
  const running = msg.status === "running";
  cancel.textContent = running ? "cancel" : "dismiss";
  cancel.onclick = running
    ? () => send({ type: "cancel_job", id: msg.id })
    : () => { card.remove(); state.jobs.delete(msg.id); };
  clearTimeout(job.timer);
  if (msg.status === "done") {
    // A finished hand-off has already been spoken; leave it up long enough to read.
    job.timer = setTimeout(() => { card.remove(); state.jobs.delete(msg.id); }, 20000);
  }
}

function toast(text, level) {
  const node = document.createElement("div");
  node.className = `toast ${level}`;
  node.textContent = text;
  el.toasts.appendChild(node);
  setTimeout(() => node.remove(), level === "error" ? 9000 : 5000);
}

/* ---------------------------------------------------------------- timers */

function renderTimers() {
  el.timers.innerHTML = "";
  const now = Date.now() / 1000;
  for (const item of state.timers.slice(0, 4)) {
    const left = Math.max(0, (item.fire_at || 0) - now);
    const node = document.createElement("span");
    node.className = "timer";
    const name = document.createElement("span");
    name.textContent = item.label || item.text || item.kind || "timer";
    const time = document.createElement("b");
    time.textContent = fmtLeft(left);
    node.append(name, time);
    el.timers.appendChild(node);
  }
}

function fmtLeft(s) {
  s = Math.round(s);
  if (s >= 3600) return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/* ------------------------------------------------------------- markdown */

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

// Code spans and links are pulled out before the emphasis pass so that a URL
// containing * or _ cannot be mangled into italics.
function inlineMd(text) {
  const held = [];
  const hold = (html) => `\u0001${held.push(html) - 1}\u0001`;
  let s = text;
  s = s.replace(/`([^`\n]+)`/g, (_, c) => hold(`<code>${c}</code>`));
  s = s.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, (_, t, u) => hold(link(u, t)));
  s = s.replace(/(^|[\s(])(https?:\/\/[^\s<>)]+)/g, (_, pre, u) => pre + hold(link(u, u)));
  s = s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*\w])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  s = s.replace(/\u0001(\d+)\u0001/g, (_, i) => held[Number(i)]);
  return s;
}

function link(url, text) {
  const raw = url.replace(/&amp;/g, "&");
  return `<a href="${escapeHtml(raw)}" data-link="${escapeHtml(raw)}">${text}</a>`;
}

function renderMarkdown(src) {
  const lines = escapeHtml(src || "").split("\n");
  const out = [];
  let para = [];
  let list = [];
  let fence = null;

  const flushPara = () => {
    if (para.length) out.push(`<p>${inlineMd(para.join(" "))}</p>`);
    para = [];
  };
  const flushList = () => {
    if (list.length) out.push(`<ul>${list.map((i) => `<li>${inlineMd(i)}</li>`).join("")}</ul>`);
    list = [];
  };

  for (const line of lines) {
    if (fence !== null) {
      if (/^\s*```/.test(line)) {
        out.push(`<pre><code>${fence.join("\n")}</code></pre>`);
        fence = null;
      } else {
        fence.push(line);
      }
      continue;
    }
    if (/^\s*```/.test(line)) {
      flushPara(); flushList();
      fence = [];
      continue;
    }
    const bullet = line.match(/^\s*(?:[-*•]|\d+[.)])\s+(.*)$/);
    if (bullet) {
      flushPara();
      list.push(bullet[1]);
      continue;
    }
    if (!line.trim()) {
      flushPara(); flushList();
      continue;
    }
    const heading = line.match(/^\s*#{1,6}\s+(.*)$/);
    if (heading) {
      flushPara(); flushList();
      out.push(`<p><strong>${inlineMd(heading[1])}</strong></p>`);
      continue;
    }
    flushList();
    para.push(line.trim());
  }
  if (fence !== null) out.push(`<pre><code>${fence.join("\n")}</code></pre>`);
  flushPara();
  flushList();
  return out.join("");
}

// Links are handed to the brain: this window is the assistant, not a browser.
function openLink(url) {
  if (send({ type: "open_url", url })) return;
  fetch(`${API_BASE}/api/command`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: `open ${url}`, speak: false }),
  }).catch(() => toast("Could not reach the brain to open that link.", "warn"));
}

document.addEventListener("click", (ev) => {
  const a = ev.target.closest("a[data-link]");
  if (!a) return;
  ev.preventDefault();
  openLink(a.dataset.link);
});

/* --------------------------------------------------------------- settings */

function cfgGet(path, fallback) {
  let cur = state.config;
  for (const part of path.split(".")) {
    if (!cur || typeof cur !== "object" || !(part in cur)) return fallback;
    cur = cur[part];
  }
  return cur;
}

const controls = Array.from(document.querySelectorAll("[data-path]"));

function applyConfig() {
  const voices = state.voices || cfgGet("voice.voices", null);
  el.setVoice.dataset.path = cfgGet("voice.engine", "macos") === "macos" ? "voice.thai_voice" : "voice.voice";
  const current = cfgGet(el.setVoice.dataset.path, "");
  const list = Array.isArray(voices) && voices.length ? voices.slice() : FALLBACK_VOICES.slice();
  if (current && !list.includes(current)) list.unshift(current);
  if (el.setVoice.dataset.filled !== list.join(",")) {
    el.setVoice.innerHTML = "";
    for (const v of list) {
      const opt = document.createElement("option");
      opt.value = v;
      opt.textContent = VOICE_LABELS[v] || v;
      el.setVoice.appendChild(opt);
    }
    el.setVoice.dataset.filled = list.join(",");
  }

  for (const c of controls) {
    const val = cfgGet(c.dataset.path, null);
    if (val === null || val === undefined) continue;
    if (c.type === "checkbox") c.checked = Boolean(val);
    else c.value = String(val);
    paintNum(c);
  }
  el.model.textContent = cfgGet("llm.model", state.status.model || "");
}

function paintNum(c) {
  const out = $(c.id + "Out");
  if (!out) return;
  const v = Number(c.value);
  if (c.dataset.path === "listening.end_silence_ms") out.textContent = `${Math.round(v)} ms`;
  else if (c.dataset.path === "listening.followup_seconds") out.textContent = `${v.toFixed(1)} s`;
  else out.textContent = v.toFixed(2);
}

let cfgTimer = 0;
function pushConfig(c) {
  const path = c.dataset.path;
  let value;
  if (c.type === "checkbox") value = c.checked;
  else if (c.type === "range") value = Number(c.value);
  else value = c.value;
  clearTimeout(cfgTimer);
  cfgTimer = setTimeout(() => send({ type: "set_config", path, value }), c.type === "range" ? 220 : 0);
}

for (const c of controls) {
  c.addEventListener("input", () => { paintNum(c); pushConfig(c); });
  c.addEventListener("change", () => { paintNum(c); pushConfig(c); });
}

el.previewBtn.addEventListener("click", () => send({ type: "preview_voice", voice: el.setVoice.value }));

function applyStatus() {
  const s = state.status;
  if (s.model) el.model.textContent = s.model;
  if (typeof s.listening_enabled === "boolean") {
    state.listening = s.listening_enabled;
    el.micBtn.setAttribute("aria-pressed", String(!s.listening_enabled));
    el.micText.textContent = s.listening_enabled ? "mic on" : "mic off";
  }
  const perms = s.permissions || {};
  paintPerm(el.permAccess, perms.accessibility);
  paintPerm(el.permScreen, perms.screen_recording);
  if (s.ollama === false) el.model.textContent = (s.model || "model") + " · offline";
}

function paintPerm(node, ok) {
  const val = node.querySelector(".permVal");
  val.textContent = ok === undefined ? "unknown" : ok ? "granted" : "not granted";
  val.classList.toggle("yes", ok === true);
  val.classList.toggle("no", ok === false);
}

function setDrawer(open) {
  el.drawer.classList.toggle("open", open);
  el.drawer.setAttribute("aria-hidden", String(!open));
  el.settingsBtn.setAttribute("aria-expanded", String(open));
  el.scrim.hidden = !open;
  if (open) el.setVoice.focus();
}

el.settingsBtn.addEventListener("click", () => setDrawer(!el.drawer.classList.contains("open")));
el.drawerClose.addEventListener("click", () => setDrawer(false));
el.scrim.addEventListener("click", () => setDrawer(false));

/* ------------------------------------------------------------- controls */

el.form.addEventListener("submit", (ev) => {
  ev.preventDefault();
  const text = el.input.value.trim();
  if (!text) return;
  if (send({ type: "text", text })) el.input.value = "";
});

el.micBtn.addEventListener("click", () => {
  state.listening = !state.listening;
  el.micBtn.setAttribute("aria-pressed", String(!state.listening));
  el.micText.textContent = state.listening ? "mic on" : "mic off";
  send({ type: "set_listening", enabled: state.listening });
});

el.newBtn.addEventListener("click", () => {
  send({ type: "new_conversation" });
  el.stream.innerHTML = "";
  state.turns = [];
  state.byId.clear();
  state.current = null;
  state.caption = "";
});

el.orbHit.addEventListener("click", () => send({ type: "activate" }));

document.addEventListener("keydown", (ev) => {
  const tag = ev.target instanceof HTMLElement ? ev.target.tagName : "";
  const typing = tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA" ||
    (ev.target instanceof HTMLElement && ev.target.isContentEditable);
  if (ev.key === "Escape") {
    if (el.drawer.classList.contains("open")) { setDrawer(false); return; }
    if (typing) el.input.blur();
    send({ type: "stop" });
    return;
  }
  // Space on a focused button must still press that button.
  if (ev.code === "Space" && !typing && tag !== "BUTTON" && !ev.metaKey && !ev.ctrlKey && !ev.altKey) {
    ev.preventDefault();
    send({ type: "activate" });
  }
});

/* ------------------------------------------------------------------ boot */

function tickClock() {
  el.clock.textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (state.timers.length) renderTimers();
}

if (MINI) {
  document.body.classList.add("mini");
  el.input.disabled = true;
}

Orb.mount(el.orb);
Orb.setState("idle");
tickClock();
setInterval(tickClock, 1000);
setConnected(false);
connect();

// Keeps a wedged socket from sitting there looking connected.
setInterval(() => { if (state.connected) send({ type: "ping" }); }, 25000);

window.addEventListener("pagehide", () => { if (sock) sock.close(); });
