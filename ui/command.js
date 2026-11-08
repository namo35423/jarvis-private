/* JARVIS Command Center.
 *
 * Talks to the brain over the same WebSocket and HTTP API as the menu-bar UI.
 * Nothing on screen is simulated: states, levels, tools and readings all come
 * from brain events or /api endpoints, and anything unavailable says so.
 */
import { Core } from "./core.js";
import { renderMarkdown, escapeHTML } from "./md.js";

const $ = (id) => document.getElementById(id);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const NATIVE = !!window.JARVIS_NATIVE;
const host = (cmd, extra = {}) => window.webkit?.messageHandlers?.jarvisHost?.postMessage({ cmd, ...extra });
if (NATIVE) document.documentElement.classList.add("native");

const ACCENTS = {
  cyan: { acc: [56, 225, 255], acc2: [47, 139, 255], label: "Cyan" },
  blue: { acc: [74, 150, 255], acc2: [56, 225, 255], label: "Electric blue" },
  violet: { acc: [170, 130, 255], acc2: [74, 150, 255], label: "Violet" },
  emerald: { acc: [64, 232, 176], acc2: [56, 190, 255], label: "Emerald" },
};
const VOICE_LABELS = {
  m_jarvis_smooth: "ผู้ชายวัยหนุ่ม · ทุ้มเล็กน้อย (B)",
  m_young_clear: "ผู้ชายวัยหนุ่ม · เสียงโปร่ง (A)",
  m_teen_bright: "ผู้ชาย · สดใส (C)",
  m_mid_warm: "ผู้ชาย · ทุ้มอุ่น",
  m_elderly_deep: "ผู้ชายสูงวัย · เสียงทุ้ม",
};

const S = {
  ws: null, connected: false, retry: 800,
  config: {}, status: {}, voices: [],
  brainState: "offline", detail: "",
  view: "dashboard",
  visible: document.visibilityState === "visible", active: document.hasFocus(),
  live: null, music: null, weather: null,
  flash: null,
  running: new Map(),          // tool call id -> {name, args}
  msgs: new Map(),             // turn id -> chat message record
  history: [],                 // turns loaded from the brain
  heard: [],
  cpuHist: [], memHist: [],
  speak: localStorage.getItem("cc.speak") !== "0",
  lastLevel: 0, lastLevelAt: 0,
  musicBusy: false,
};

const core = new Core($("core"));
const cfg = (path, fallback) => {
  let cur = S.config;
  for (const part of path.split(".")) {
    if (!cur || typeof cur !== "object" || !(part in cur)) return fallback;
    cur = cur[part];
  }
  return cur ?? fallback;
};
const lang = () => (cfg("language", "th") === "en" ? "en" : "th");
const T = (th, en) => (lang() === "th" ? th : en);

/* ================================================================ websocket */
function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  S.ws = ws;
  ws.onopen = () => { S.retry = 800; };
  ws.onmessage = (e) => {
    let msg;
    try { msg = JSON.parse(e.data); } catch { return; }
    handle(msg);
  };
  ws.onclose = () => {
    if (S.ws !== ws) return;
    S.connected = false;
    paintLink();
    paintState();
    setTimeout(connect, S.retry);
    S.retry = Math.min(5000, S.retry * 1.6);
  };
  ws.onerror = () => ws.close();
}
function send(obj) {
  if (S.ws && S.ws.readyState === WebSocket.OPEN) {
    S.ws.send(JSON.stringify(obj));
    return true;
  }
  toast(T("ยังไม่ได้เชื่อมต่อกับ JARVIS Brain", "Not connected to the JARVIS brain"), "warn");
  return false;
}
async function api(path, body) {
  const opts = body === undefined ? {} : { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) };
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok && !data.error) data.error = `HTTP ${res.status}`;
  return data;
}

function handle(msg) {
  switch (msg.type) {
    case "hello":
      S.connected = true;
      S.config = msg.config || {};
      S.voices = msg.voices || [];
      S.status = msg.status || {};
      S.brainState = msg.state || "idle";
      S.history = msg.history || [];
      applyConfig();
      applyStatus();
      renderChatHistory();
      seedDashboardFromHistory();
      paintLink();
      paintState();
      refreshLive();
      refreshWeather();
      refreshMusic();
      break;
    case "state":
      S.brainState = msg.state;
      S.detail = msg.detail || "";
      if (msg.state !== "working") S.running.clear();
      paintState();
      break;
    case "level":
      // Only the source that matches the state drives the visuals: the mic while
      // listening, Jarvis's own voice while speaking.
      if (!((msg.source === "mic" && S.brainState === "listening") || (msg.source === "tts" && S.brainState === "speaking"))) break;
      core.pushLevel(msg.value);
      S.lastLevel = msg.value;
      S.lastLevelAt = performance.now();
      paintLevel();
      break;
    case "transcript":
      onTranscript(msg);
      break;
    case "reply_delta":
      onReply(msg, false);
      break;
    case "reply_done":
      onReply(msg, true);
      break;
    case "tool":
      onTool(msg);
      break;
    case "confirm_request":
      showConfirm(msg);
      break;
    case "confirm_done":
      hideConfirm(msg.id);
      break;
    case "notice":
      if (msg.level === "error") flash(msg.text);
      toast(msg.text, msg.level === "error" ? "error" : msg.level === "warn" ? "warn" : "info");
      break;
    case "status":
      S.status = { ...S.status, ...msg };
      applyStatus();
      break;
    case "config":
      S.config = msg.config || S.config;
      if (msg.voices && msg.voices.length) S.voices = msg.voices;
      applyConfig();
      break;
    case "timers":
      S.timers = msg.items || [];
      renderTimers();
      break;
    case "stats_request":
      // Measured, not estimated: the brain's /api/uistats collects these on demand.
      send({ type: "ui_stats", ...core.stats(), view: S.view, visible: S.visible, active: S.active,
             quality: cfg("ui.animation", "high"), native: NATIVE, w: innerWidth, h: innerHeight, dpr: devicePixelRatio });
      break;
    case "job":
      if (msg.status && ["done", "failed", "cancelled"].includes(msg.status)) toast(`Claude Code: ${msg.status}`, msg.status === "done" ? "ok" : "warn");
      break;
    default:
      break;
  }
}

/* ================================================================ host bridge */
window.jarvisHostEvent = (ev) => {
  if (ev.type === "visibility") {
    S.visible = !!ev.visible;
    S.active = !!ev.active;
    core.setVisibility(S.visible, S.active);
    if (S.visible) { refreshLive(); refreshMusic(); }
  } else if (ev.type === "host") {
    const box = $("setLogin");
    if (box) box.checked = !!ev.loginItem;
    if (ev.error) toast(ev.error, "error");
    if (ev.reduceMotion) document.documentElement.classList.add("reduce");
  }
};
document.addEventListener("visibilitychange", () => {
  S.visible = document.visibilityState === "visible";
  core.setVisibility(S.visible, S.active);
});
window.addEventListener("focus", () => { S.active = true; core.setVisibility(S.visible, true); });
window.addEventListener("blur", () => { S.active = false; core.setVisibility(S.visible, false); });
if (matchMedia("(prefers-reduced-motion: reduce)").matches) document.documentElement.classList.add("reduce");
if (NATIVE) host("hello");

/* ================================================================ state display */
const WORDS = {
  th: { idle: "JARVIS ONLINE", listening: "กำลังฟัง...", thinking: "กำลังประมวลผล...", transcribing: "กำลังถอดเสียง...",
        speaking: "กำลังพูด...", confirm: "รอการยืนยัน", muted: "ไมค์ปิดอยู่", error: "พบปัญหา", offline: "ไม่ได้เชื่อมต่อ", starting: "กำลังเริ่มระบบ..." },
  en: { idle: "JARVIS ONLINE", listening: "Listening...", thinking: "Processing...", transcribing: "Transcribing...",
        speaking: "Speaking...", confirm: "Awaiting confirmation", muted: "Microphone off", error: "Problem", offline: "Disconnected", starting: "Starting up..." },
};
const CHIP = { idle: "IDLE", listening: "LISTENING", thinking: "THINKING", working: "EXECUTING", speaking: "SPEAKING",
               confirm: "CONFIRM", muted: "MUTED", error: "ERROR", offline: "OFFLINE" };

function toolLabel(name, a = {}) {
  a = a || {};
  const th = lang() === "th";
  const t = (x, y) => (th ? x : y);
  switch (name) {
    case "youtube_music": {
      const what = [a.query, a.artist].filter(Boolean).join(t(" ของ ", " by "));
      if (a.action === "play" && what) return t(`กำลังเปิดเพลง ${what}`, `Playing ${what}`);
      return {
        open: t("กำลังเปิด YouTube Music", "Opening YouTube Music"), pause: t("กำลังหยุดเพลง", "Pausing music"),
        play: t("กำลังเล่นเพลงต่อ", "Resuming music"), next: t("กำลังข้ามไปเพลงถัดไป", "Skipping to the next track"),
        previous: t("กำลังย้อนเพลง", "Going back a track"), now_playing: t("กำลังดูว่าเล่นเพลงอะไรอยู่", "Checking what's playing"),
        search: t(`กำลังค้นหาเพลง ${what}`, `Searching for ${what}`), liked: t("กำลังเปิดเพลงที่ชอบ", "Opening liked songs"),
      }[a.action] || t("กำลังสั่ง YouTube Music", "Controlling YouTube Music");
    }
    case "open_app": return t(`กำลังเปิด ${a.name || "แอป"}`, `Opening ${a.name || "an app"}`);
    case "quit_app": return t(`กำลังปิด ${a.name || "แอป"}`, `Quitting ${a.name || "an app"}`);
    case "open_url": return t("กำลังเปิดเว็บไซต์", "Opening the website");
    case "get_weather": return t(`กำลังดูพยากรณ์อากาศ${a.location ? "ที่" + a.location : ""}`, "Checking the weather");
    case "web_search": return t(`กำลังค้นหา “${a.query || ""}”`, `Searching “${a.query || ""}”`);
    case "fetch_url": return t("กำลังอ่านหน้าเว็บ", "Reading the page");
    case "news_headlines": return t("กำลังดึงข่าวล่าสุด", "Fetching the latest news");
    case "convert_currency": return t("กำลังดูอัตราแลกเปลี่ยน", "Checking exchange rates");
    case "line_send": return t(`กำลังส่ง LINE ถึง ${a.recipient || ""}`, `Sending LINE to ${a.recipient || ""}`);
    case "messages_send": return t("กำลังส่งข้อความ", "Sending a message");
    case "set_timer": return t("กำลังตั้งเวลา", "Setting a timer");
    case "timers": return t("กำลังจัดการตัวตั้งเวลา", "Managing timers");
    case "reminders": return a.action === "create" ? t("กำลังเพิ่มรายการเตือน", "Adding a reminder") : t("กำลังอ่านรายการเตือน", "Reading reminders");
    case "calendar": return a.action === "create" ? t("กำลังเพิ่มนัดในปฏิทิน", "Adding an event") : t("กำลังอ่านปฏิทิน", "Reading the calendar");
    case "notes": return t("กำลังจดโน้ต", "Working with Notes");
    case "system_control": {
      const act = String(a.action || "");
      if (act.startsWith("volume") || act === "mute" || act === "unmute") return t("กำลังปรับเสียง", "Adjusting volume");
      const named = {
        battery: t("กำลังดูแบตเตอรี่", "Checking the battery"), disk_space: t("กำลังดูพื้นที่ดิสก์", "Checking disk space"),
        wifi_status: t("กำลังเช็ก Wi-Fi", "Checking Wi-Fi"), wifi_on: t("กำลังเปิด Wi-Fi", "Turning Wi-Fi on"),
        wifi_off: t("กำลังปิด Wi-Fi", "Turning Wi-Fi off"), dark_mode: t("กำลังเปิดโหมดมืด", "Switching to dark mode"),
        light_mode: t("กำลังเปิดโหมดสว่าง", "Switching to light mode"), toggle_appearance: t("กำลังสลับโหมดมืด/สว่าง", "Toggling appearance"),
        lock_screen: t("กำลังล็อกหน้าจอ", "Locking the screen"), sleep_display: t("กำลังปิดหน้าจอ", "Sleeping the display"),
        brightness_up: t("กำลังเพิ่มความสว่าง", "Brightening the screen"), brightness_down: t("กำลังลดความสว่าง", "Dimming the screen"),
        sleep: t("กำลังสั่ง Mac พัก", "Putting the Mac to sleep"), restart: t("กำลังสั่งรีสตาร์ท Mac", "Restarting the Mac"),
        shutdown: t("กำลังสั่งปิดเครื่อง", "Shutting down"), logout: t("กำลังออกจากระบบ", "Logging out"),
      }[act];
      return named || t("กำลังสั่งการระบบ", "Controlling the system");
    }
    case "chrome_read_page": return t("กำลังอ่านหน้าเว็บใน Chrome", "Reading the Chrome page");
    case "chrome_tabs": return t("กำลังจัดการแท็บ Chrome", "Managing Chrome tabs");
    case "chrome_act": return t("กำลังสั่งงานใน Chrome", "Acting in Chrome");
    case "screen_look": return t("กำลังดูหน้าจอ", "Looking at the screen");
    case "find_files": return t("กำลังค้นหาไฟล์", "Finding files");
    case "ask_claude": return t("กำลังส่งงานให้ Claude Code", "Handing off to Claude Code");
    case "remember": return t("กำลังจดจำ", "Remembering");
    default: return t(`กำลังใช้ ${name || "เครื่องมือ"}`, `Using ${name || "a tool"}`);
  }
}

function issue() {
  const st = S.status;
  if (!S.connected) return null;
  if (st.ready === false) return null;
  if (st.ollama === false) return { text: T("Ollama ไม่ทำงาน — JARVIS คิดคำตอบไม่ได้", "Ollama is not running — JARVIS can't think"), fix: T("เปิดแอป Ollama แล้วรอสักครู่", "Open the Ollama app and wait a moment") };
  if (st.mic === "denied") return { text: T("ไม่ได้รับสิทธิ์ไมโครโฟน", "Microphone access denied"), fix: T("System Settings › Privacy & Security › Microphone › เปิด Jarvis", "System Settings › Privacy & Security › Microphone › enable Jarvis") };
  if (st.mic === "blocked" || st.mic === "unavailable") return { text: T("เปิดไมโครโฟนไม่สำเร็จ", "The microphone could not be opened"), fix: T("เสียบไมค์ใหม่ หรือกด Restart AI Service", "Reconnect the mic or restart the AI service") };
  return null;
}

function effectiveState() {
  if (!S.connected) return "offline";
  if (S.flash && S.flash.until > Date.now()) return "error";
  if ((S.brainState === "idle" || S.brainState === "muted") && issue()) return "error";
  return S.brainState || "idle";
}

function flash(text, fix) {
  const clean = String(text || "").replace(/^Error:\s*/, "").replace(/^ทำรายการไม่สำเร็จครับ:\s*/, "");
  S.flash = { text: clean, fix: fix || guessFix(clean), until: Date.now() + 6000 };
  paintState();
  setTimeout(paintState, 6100);
}
function guessFix(text) {
  if (/Allow JavaScript from Apple Events/i.test(text)) return "";
  if (/permission|สิทธิ์|System Settings/i.test(text)) return T("เปิดสิทธิ์ให้ Jarvis ใน System Settings › Privacy & Security", "Allow Jarvis in System Settings › Privacy & Security");
  if (/ollama|model/i.test(text)) return T("ตรวจว่า Ollama เปิดอยู่", "Check that Ollama is running");
  if (/did not respond in time|ไม่ตอบสนอง/i.test(text)) return T("ลองอีกครั้ง หรือตอบหน้าต่างขอสิทธิ์ของ macOS", "Try again, or answer the macOS permission prompt");
  return "";
}

function paintState() {
  const st = effectiveState();
  const W = WORDS[lang()];
  let word = W[st] || W.idle;
  let detail = "";
  const problem = issue();
  if (st === "offline") {
    detail = T("กำลังเชื่อมต่อกับ JARVIS Brain ใหม่… ถ้านานเกิน 30 วินาที กด Restart AI Service ใน Settings", "Reconnecting to the JARVIS brain… if this lasts over 30 s, use Restart AI Service in Settings");
  } else if (st === "error") {
    const f = S.flash && S.flash.until > Date.now() ? S.flash : problem;
    detail = f ? f.text + (f.fix ? `  ·  ${f.fix}` : "") : "";
  } else if (st === "thinking") {
    word = S.detail === "transcribing" ? W.transcribing : W.thinking;
  } else if (st === "working") {
    const last = Array.from(S.running.values()).pop();
    word = last ? toolLabel(last.name, last.args) : toolLabel(S.detail, {});
  } else if (st === "listening") {
    detail = S.detail === "follow-up" ? T("ฟังต่ออยู่ พูดต่อได้เลย", "Still listening") : T("พูดคำสั่งได้เลย", "Go ahead");
  } else if (st === "confirm") {
    detail = S.detail || "";
  } else if (st === "muted") {
    detail = T("กด Mute Microphone อีกครั้ง หรือกดเพื่อพูด เพื่อเปิดไมค์", "Unmute, or press Push to Talk");
  } else if (st === "idle" && S.status.ready === false) {
    word = W.starting;
  } else if (st === "idle" && S.status.listening_enabled === false) {
    detail = T("ไมค์ปิดอยู่ · พิมพ์คำสั่งได้ หรือกด Unmute Microphone / กดเพื่อพูด", "Microphone off · type, or unmute / Push to Talk");
  } else if (st === "idle" && cfg("listening.wake_word_enabled", true) === false) {
    detail = T("Wake word ปิดอยู่ · กดเพื่อพูด หรือ ⌘\\", "Wake word off · Push to Talk or ⌘\\");
  }
  const wordEl = $("stateWord");
  wordEl.textContent = word;
  wordEl.classList.toggle("latin", /^[\x20-\x7E]*$/.test(word));
  $("stateDetail").textContent = detail;
  $("stage").className = `stage s-${st}`;
  core.setState(st);
  const chip = $("chipState");
  chip.className = `chip s-${st}`;
  chip.querySelector("b").textContent = CHIP[st] || st.toUpperCase();
  $("hudState").textContent = CHIP[st] || st.toUpperCase();
  for (const b of $$("[data-ptt]")) b.classList.toggle("live", st === "listening");
}

function paintLevel() {
  const now = performance.now();
  if (now - (S.levelPaintAt || 0) < 120) return;
  S.levelPaintAt = now;
  const pct = Math.round(Math.max(0, Math.min(1, S.lastLevel)) * 100);
  $("hudLevel").textContent = `${pct}%`;
  const m = $("micMeter");
  if (m && S.brainState === "listening") m.style.width = `${pct}%`;
}
setInterval(() => {
  // Levels stop arriving when capture ends; let the readouts fall back honestly.
  if (performance.now() - S.lastLevelAt > 600 && S.lastLevel !== 0) {
    S.lastLevel = 0;
    $("hudLevel").textContent = "0%";
    const m = $("micMeter");
    if (m) m.style.width = "0%";
  }
}, 700);

function paintLink() {
  const dot = $("linkDot");
  const txt = $("linkText");
  if (!S.connected) {
    dot.className = "dot bad";
    txt.textContent = "OFFLINE";
  } else if (S.status.ready === false) {
    dot.className = "dot warn";
    txt.textContent = "STARTING";
  } else {
    dot.className = issue() ? "dot warn" : "dot ok";
    txt.textContent = "BRAIN ONLINE";
  }
}

/* ================================================================ conversation */
function onTranscript(msg) {
  if (msg.role !== "user") return;
  $("dashUser").textContent = msg.text;
  $("dashTools").innerHTML = "";
  S.heard.unshift({ text: msg.text, at: new Date() });
  S.heard = S.heard.slice(0, 12);
  renderHeard();
  if (msg.turn === "confirm") return;
  addMessage("user", msg.text, { turn: msg.turn });
}

function onReply(msg, done) {
  const rec = jarvisMsg(msg.turn);
  if (done) rec.text = msg.text || rec.text;
  else rec.text += msg.text || "";
  rec.streaming = !done;
  scheduleRender(rec);
  if (msg.turn !== "ui") {
    S.dashTurn = msg.turn;
    $("dashReply").innerHTML = renderMarkdown(rec.text) || "—";
  }
}

function onTool(msg) {
  if (msg.status === "running") S.running.set(msg.id, { name: msg.name, args: msg.args });
  else S.running.delete(msg.id);
  if (S.brainState === "working") paintState();
  if (msg.status === "error") flash(msg.result || "");

  // Dashboard chips.
  const row = $("dashTools");
  let chip = row.querySelector(`[data-id="${msg.id}"]`);
  if (!chip) {
    chip = document.createElement("span");
    chip.dataset.id = msg.id;
    row.appendChild(chip);
    while (row.children.length > 6) row.firstElementChild.remove();
  }
  chip.className = `toolChip ${msg.status}`;
  chip.textContent = toolLabel(msg.name, msg.args).replace(/^กำลัง/, "") + (msg.status === "error" ? " ✕" : msg.status === "ok" ? " ✓" : "");
  chip.title = msg.result || "";

  if (msg.turn === "ui") {
    if (msg.status !== "running") refreshMusicSoon(msg.name);
    return;
  }
  const rec = jarvisMsg(msg.turn);
  rec.tools.set(msg.id, msg);
  scheduleRender(rec);
  if (msg.status !== "running") refreshMusicSoon(msg.name);
}

/* ---------------------------------------------------------------- chat log */
function jarvisMsg(turn) {
  let rec = S.msgs.get(turn);
  if (!rec) {
    rec = { role: "jarvis", text: "", tools: new Map(), streaming: false, turn, at: new Date() };
    S.msgs.set(turn, rec);
    mountMessage(rec);
  }
  return rec;
}

function addMessage(role, text, extra = {}) {
  const rec = { role, text, tools: new Map(), streaming: false, at: extra.at || new Date(), ...extra };
  if (role === "user" && extra.turn) S.msgs.set(`u:${extra.turn}`, rec);
  mountMessage(rec);
  return rec;
}

function mountMessage(rec, { history = false } = {}) {
  const log = $("chatLog");
  const day = rec.at.toDateString();
  if (log.dataset.lastDay !== day) {
    const rule = document.createElement("div");
    rule.className = "dayRule";
    rule.textContent = rec.at.toLocaleDateString(lang() === "th" ? "th-TH" : "en-GB", { weekday: "long", day: "numeric", month: "long" });
    log.appendChild(rule);
    log.dataset.lastDay = day;
  }
  const el = document.createElement("div");
  el.className = `msg ${rec.role}`;
  el.innerHTML = `<div class="tools"></div><div class="bubble"></div><div class="meta"><time></time><button type="button" class="copyMsg">คัดลอก</button></div>`;
  el.querySelector("time").textContent = rec.at.toLocaleTimeString(lang() === "th" ? "th-TH" : "en-GB", { hour: "2-digit", minute: "2-digit" });
  el.querySelector(".copyMsg").addEventListener("click", () => copyText(rec.text));
  rec.el = el;
  log.appendChild(el);
  renderMessage(rec);
  if (!history) stickBottom();
}

const pending = new Set();
let renderQueued = false;
function scheduleRender(rec) {
  pending.add(rec);
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => {
    renderQueued = false;
    for (const r of pending) renderMessage(r);
    pending.clear();
    stickBottom();
  });
}

function renderMessage(rec) {
  if (!rec.el) return;
  const bubble = rec.el.querySelector(".bubble");
  if (rec.role === "user") bubble.textContent = rec.text;
  else {
    bubble.classList.add("md");
    bubble.innerHTML = rec.text ? renderMarkdown(rec.text) : (rec.streaming || rec.tools.size ? "" : "…");
    bubble.classList.toggle("streaming", rec.streaming);
    bubble.hidden = !rec.text && !rec.streaming && rec.tools.size > 0;
  }
  const tools = rec.el.querySelector(".tools");
  if (rec.tools.size) {
    tools.innerHTML = "";
    for (const t of rec.tools.values()) {
      const d = document.createElement("details");
      d.className = `toolCard ${t.status}`;
      const st = t.status === "running" ? T("กำลังทำงาน", "RUNNING") : t.status === "ok" ? T("สำเร็จ", "OK") : T("ผิดพลาด", "ERROR");
      d.innerHTML = `<summary><span class="st">${escapeHTML(st)}</span><span>${escapeHTML(toolLabel(t.name, t.args).replace(/^กำลัง/, ""))}</span><span class="mono" style="margin-left:auto;opacity:.6">${escapeHTML(t.name)}</span></summary>` +
        `<pre>${escapeHTML(JSON.stringify(t.args || {}, null, 1))}${t.result ? "\n\n→ " + escapeHTML(t.result) : ""}</pre>`;
      tools.appendChild(d);
    }
  }
}

function stickBottom() {
  const log = $("chatLog");
  if (log.scrollHeight - log.scrollTop - log.clientHeight < 240 || S.view !== "chat") log.scrollTop = log.scrollHeight;
}

function renderChatHistory() {
  const log = $("chatLog");
  const live = Array.from(log.querySelectorAll(".msg")).filter((el) => !el.dataset.history);
  log.innerHTML = "";
  delete log.dataset.lastDay;
  for (const t of S.history) {
    if (t.role === "reset") {
      const rule = document.createElement("div");
      rule.className = "dayRule";
      rule.dataset.history = "1";
      rule.textContent = T("— เริ่มบทสนทนาใหม่ —", "— new conversation —");
      log.appendChild(rule);
      continue;
    }
    const rec = { role: t.role === "user" ? "user" : "jarvis", text: t.text || "", tools: new Map(), streaming: false, at: new Date((t.ts || 0) * 1000) };
    mountMessage(rec, { history: true });
    rec.el.dataset.history = "1";
  }
  for (const el of live) log.appendChild(el);
  log.scrollTop = log.scrollHeight;
}

function seedDashboardFromHistory() {
  const lastUser = [...S.history].reverse().find((t) => t.role === "user");
  const lastJarvis = [...S.history].reverse().find((t) => t.role === "assistant");
  if (lastUser) $("dashUser").textContent = lastUser.text;
  if (lastJarvis) $("dashReply").innerHTML = renderMarkdown(lastJarvis.text);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
  toast(T("คัดลอกแล้ว", "Copied"), "ok", 1400);
}
$("chatLog").addEventListener("click", (e) => {
  const btn = e.target.closest(".codeBlock .copy");
  if (btn) copyText(btn.parentElement.querySelector("code").textContent);
});

/* ---------------------------------------------------------------- composers */
function submitText(text) {
  text = text.trim();
  if (!text) return false;
  return send({ type: "text", text, speak: S.speak });
}
$("dashForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const input = e.target.elements.text;
  if (submitText(input.value)) input.value = "";
});
const chatBox = $("chatForm").elements.text;
$("chatForm").addEventListener("submit", (e) => {
  e.preventDefault();
  if (submitText(chatBox.value)) {
    chatBox.value = "";
    autoGrow();
  }
});
function autoGrow() {
  chatBox.style.height = "auto";
  chatBox.style.height = Math.min(140, chatBox.scrollHeight) + "px";
}
chatBox.addEventListener("input", autoGrow);
chatBox.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    $("chatForm").requestSubmit();
  }
});
for (const box of $$("[data-speak]")) {
  box.checked = S.speak;
  box.addEventListener("change", () => {
    S.speak = box.checked;
    localStorage.setItem("cc.speak", S.speak ? "1" : "0");
    for (const other of $$("[data-speak]")) other.checked = S.speak;
  });
}
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-ptt]");
  if (!b) return;
  if (S.brainState === "listening") {
    send({ type: "stop" });
    return;
  }
  if (S.status.mic && S.status.mic !== "ok") {
    const p = issue();
    toast(p ? `${p.text} · ${p.fix}` : T("ไมโครโฟนยังไม่พร้อม", "The microphone is not ready"), "warn");
    return;
  }
  send({ type: "activate" });
});
$("chatNew").addEventListener("click", () => {
  if (!send({ type: "new_conversation" })) return;
  const rule = document.createElement("div");
  rule.className = "dayRule";
  rule.textContent = T("— เริ่มบทสนทนาใหม่ —", "— new conversation —");
  $("chatLog").appendChild(rule);
  stickBottom();
});
$("chatMore").addEventListener("click", async () => {
  const data = await api("/api/history?limit=400");
  if (data.turns) {
    S.history = data.turns;
    renderChatHistory();
    toast(T(`โหลด ${data.turns.length} ข้อความ`, `Loaded ${data.turns.length} messages`), "ok", 1800);
  }
});

/* ---------------------------------------------------------------- confirm */
let confirmId = null;
function showConfirm(msg) {
  confirmId = msg.id;
  $("confirmTitle").textContent = msg.prompt || T("ยืนยันการทำรายการนี้ไหม", "Confirm this action?");
  $("confirmDetail").textContent = msg.detail || "";
  $("confirmBack").hidden = false;
  $("confirmYes").focus();
}
function hideConfirm(id) {
  if (id && confirmId && id !== confirmId) return;
  confirmId = null;
  $("confirmBack").hidden = true;
}
$("confirmYes").addEventListener("click", () => { if (confirmId) send({ type: "confirm_response", id: confirmId, approved: true }); hideConfirm(); });
$("confirmNo").addEventListener("click", () => { if (confirmId) send({ type: "confirm_response", id: confirmId, approved: false }); hideConfirm(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && confirmId) $("confirmNo").click();
  if (e.metaKey && !e.shiftKey && !e.altKey && /^[1-9]$/.test(e.key)) {
    const btn = $$("#nav button")[+e.key - 1];
    if (btn) { e.preventDefault(); showView(btn.dataset.view); }
  }
});

/* ================================================================ views */
function showView(name) {
  const section = $(`view-${name}`);
  if (!section) return;
  S.view = name;
  for (const b of $$("#nav button")) b.classList.toggle("active", b.dataset.view === name);
  for (const v of $$(".view")) v.classList.toggle("active", v === section);
  $("viewTitle").textContent = section.dataset.title;
  $("viewSub").textContent = section.dataset.sub || "";
  const slot = name === "voice" ? $("voiceSlot") : $("dashSlot");
  if ($("stage").parentElement !== slot) slot.appendChild($("stage"));
  core.resize();
  localStorage.setItem("cc.view", name);
  if (name === "chat") { stickBottom(); loadModels(); setTimeout(() => chatBox.focus(), 50); }
  if (name === "music") { refreshMusic(); loadVolume(); }
  if (name === "tasks") loadTimers();
  if (name === "monitor") { refreshLive(); paintMonitor(); }
  if (name === "history") loadHistory();
  if (name === "settings") { loadModels(); loadDevices(); paintBackend(); }
  if (name === "voice") paintVoice();
}
for (const b of $$("#nav button")) b.addEventListener("click", () => showView(b.dataset.view));
$("togglePanel").addEventListener("click", () => {
  const off = $("shell").classList.toggle("no-live");
  localStorage.setItem("cc.live", off ? "0" : "1");
  core.resize();
});
if (localStorage.getItem("cc.live") === "0") $("shell").classList.add("no-live");

/* ================================================================ config & settings */
function applyConfig() {
  const accent = ACCENTS[cfg("ui.accent", "cyan")] || ACCENTS.cyan;
  document.documentElement.style.setProperty("--acc", accent.acc.join(","));
  document.documentElement.style.setProperty("--acc2", accent.acc2.join(","));
  core.setAccent(accent.acc);
  const quality = cfg("ui.animation", "high");
  core.setQuality(quality);
  document.documentElement.classList.remove("anim-high", "anim-balanced", "anim-minimal");
  document.documentElement.classList.add(`anim-${quality}`);
  document.documentElement.lang = lang();

  for (const el of $$("[data-cfg]")) {
    const val = cfg(el.dataset.cfg, el.dataset.cfg === "listening.wake_word_enabled" ? true : null);
    if (val === null || val === undefined) continue;
    if (el.type === "checkbox") el.checked = !!val;
    else if (el.type === "radio") el.checked = el.value.toLowerCase() === String(val).toLowerCase();
    else el.value = String(val);
    paintOut(el);
  }
  // Voice list (same rule as the menu-bar UI: the macOS engine keeps a separate Thai voice).
  const path = cfg("voice.engine", "macos") === "macos" ? "voice.thai_voice" : "voice.voice";
  const sel = $("setVoice");
  sel.dataset.path = path;
  const current = cfg(path, "");
  const list = S.voices.length ? S.voices.slice() : [];
  if (current && !list.includes(current)) list.unshift(current);
  if (sel.dataset.filled !== list.join("|")) {
    sel.innerHTML = list.map((v) => `<option value="${escapeHTML(v)}">${escapeHTML(VOICE_LABELS[v] || v)}</option>`).join("");
    sel.dataset.filled = list.join("|");
  }
  sel.value = current;
  $("chipModel").textContent = cfg("llm.model", "—");
  $("hudModel").textContent = cfg("llm.model", "—");
  renderSwatches();
  paintState();
  paintMute();
}

function paintOut(el) {
  const out = document.querySelector(`[data-out="${el.dataset.cfg}"]`);
  if (out) out.textContent = Number(el.value).toFixed(2);
}

let cfgTimer = 0;
for (const el of $$("[data-cfg]")) {
  const push = () => {
    const path = el.dataset.cfg;
    let value;
    if (el.type === "checkbox") value = el.checked;
    else if (el.type === "radio") { if (!el.checked) return; value = el.value; }
    else if (el.type === "range") value = Number(el.value);
    else value = el.value;
    paintOut(el);
    clearTimeout(cfgTimer);
    cfgTimer = setTimeout(() => {
      send({ type: "set_config", path, value });
      if (path === "location.city") setTimeout(() => refreshWeather(true), 400);
    }, el.type === "range" ? 220 : 0);
  };
  el.addEventListener("change", push);
  if (el.type === "range") el.addEventListener("input", push);
}
$("setVoice").addEventListener("change", (e) => send({ type: "set_config", path: e.target.dataset.path, value: e.target.value }));
$("previewVoice").addEventListener("click", () => send({ type: "preview_voice", voice: $("setVoice").value }));

function renderSwatches() {
  const box = $("accentSwatches");
  const current = cfg("ui.accent", "cyan");
  box.innerHTML = Object.entries(ACCENTS).map(([k, v]) =>
    `<button type="button" data-accent="${k}" class="${k === current ? "on" : ""}" title="${v.label}" aria-label="${v.label}" style="background:rgb(${v.acc.join(",")});color:rgb(${v.acc.join(",")})"></button>`).join("");
}
$("accentSwatches").addEventListener("click", (e) => {
  const b = e.target.closest("[data-accent]");
  if (b) send({ type: "set_config", path: "ui.accent", value: b.dataset.accent });
});

async function loadModels() {
  const data = await api("/api/models").catch(() => ({}));
  const models = data.models || [];
  const current = data.current || cfg("llm.model", "");
  for (const sel of [$("setModel"), $("chatModel")]) {
    sel.innerHTML = models.length
      ? models.map((m) => `<option value="${escapeHTML(m)}">${escapeHTML(m)}</option>`).join("")
      : `<option>${T("ไม่พร้อมใช้งาน", "Unavailable")}</option>`;
    sel.disabled = !models.length;
    sel.value = current;
  }
  const note = models.length ? "" : T("อ่านรายการโมเดลจาก Ollama ไม่ได้", "Could not list Ollama models");
  $("setModelNote").textContent = note;
  $("chatModelNote").textContent = note;
}
async function changeModel(value, noteEl) {
  noteEl.textContent = T("กำลังเปลี่ยนโมเดล…", "Switching model…");
  const res = await api("/api/config", { path: "llm.model", value }).catch((e) => ({ error: String(e) }));
  if (res.error) {
    noteEl.textContent = res.error;
    toast(res.error, "error");
    loadModels();
    return;
  }
  noteEl.textContent = T(`ใช้ ${value} แล้ว · คำตอบแรกอาจช้าระหว่างโหลดโมเดล`, `Now using ${value}. The first reply may be slow while it loads.`);
  S.config = res.config || S.config;
  applyConfig();
  loadModels();
}
$("setModel").addEventListener("change", (e) => changeModel(e.target.value, $("setModelNote")));
$("chatModel").addEventListener("change", (e) => changeModel(e.target.value, $("chatModelNote")));

async function loadDevices() {
  const data = await api("/api/devices").catch(() => ({}));
  const sel = $("setMic");
  const selected = data.selected ?? cfg("listening.input_device", null);
  const opts = [`<option value="">${T("ค่าเริ่มต้นของระบบ", "System default")}</option>`];
  for (const d of data.inputs || []) opts.push(`<option value="${escapeHTML(d.name)}">${escapeHTML(d.name)}${d.default ? T(" (ค่าเริ่มต้น)", " (default)") : ""}</option>`);
  sel.innerHTML = opts.join("");
  sel.value = selected == null ? "" : String(selected);
  $("micNote").textContent = data.error ? data.error : T("เปลี่ยนไมค์แล้วต้องกด Restart AI Service เพื่อให้มีผล", "Restart the AI service after changing the microphone");
}
$("setMic").addEventListener("change", (e) => {
  send({ type: "set_config", path: "listening.input_device", value: e.target.value || null });
  $("micNote").textContent = T("บันทึกแล้ว · กด Restart AI Service เพื่อใช้ไมค์ใหม่", "Saved · restart the AI service to use it");
});
$("setLogin").addEventListener("change", (e) => host("setLoginItem", { enabled: e.target.checked }));
$("restartBrain").addEventListener("click", () => {
  host("restartBrain");
  toast(T("กำลังรีสตาร์ท AI Service… ใช้เวลาประมาณ 15–25 วินาที", "Restarting the AI service… about 15–25 s"), "info", 5000);
});
$("openLogs").addEventListener("click", () => host("openLogs"));
$("openPerms").addEventListener("click", () => host("permissions"));

function applyStatus() {
  const st = S.status;
  paintLink();
  paintState();
  paintMute();
  paintAi();
  $("hudStt").textContent = st.stt_last_ms ? `${Math.round(st.stt_last_ms)} ms` : "—";
  if (S.view === "settings") paintBackend();
  if (S.view === "voice") paintVoice();
  if (S.view === "monitor") paintMonitor();
}

function paintMute() {
  const on = S.status.listening_enabled === false;
  const qa = $("qaMute");
  qa.classList.toggle("on", on);
  qa.querySelector("span").textContent = on ? T("Unmute Microphone", "Unmute Microphone") : "Mute Microphone";
  $("voiceMute").textContent = on ? T("เปิดไมค์", "Unmute") : T("ปิดไมค์", "Mute");
}
$("qaMute").addEventListener("click", () => send({ type: "set_listening", enabled: S.status.listening_enabled === false }));
$("voiceMute").addEventListener("click", () => send({ type: "set_listening", enabled: S.status.listening_enabled === false }));

const shortModel = (m) => (m ? String(m).replace(/^mlx-community\//, "") : m);
function kv(el, rows) {
  el.innerHTML = rows.map(([k, v, cls]) => `<dt>${escapeHTML(k)}</dt><dd class="${cls || ""}">${escapeHTML(v ?? T("ไม่พร้อมใช้งาน", "Unavailable"))}</dd>`).join("");
}
const yes = (b) => (b ? ["พร้อม", "ok"] : ["ไม่พร้อม", "bad"]);
const MIC = { ok: ["ใช้งานได้", "ok"], waiting: ["รอสิทธิ์", "warn"], denied: ["ไม่ได้รับสิทธิ์", "bad"], blocked: ["เปิดไม่สำเร็จ", "bad"], unavailable: ["ไม่พบไมค์", "bad"], unknown: ["ไม่ทราบ", "warn"] };

function paintBackend() {
  const st = S.status;
  const mic = MIC[st.mic] || [st.mic, ""];
  kv($("setBackend"), [
    ["Brain", st.ready ? `พร้อม · pid ${st.pid}` : "กำลังเริ่ม", st.ready ? "ok" : "warn"],
    ["Ollama", st.ollama ? "ทำงาน" : "ไม่ทำงาน", st.ollama ? "ok" : "bad"],
    ["STT", `${shortModel(st.stt_model) || "—"} · ${st.stt === "ready" ? "พร้อม" : "ปิด"}`, st.stt === "ready" ? "ok" : "warn"],
    ["TTS", `${st.tts_engine || "—"} · ${st.tts === "ready" ? "พร้อม" : "ปิด"}`, st.tts === "ready" ? "ok" : "warn"],
    ["Wake word", st.wake ? (cfg("listening.wake_word_enabled", true) ? "พร้อม" : "ปิดอยู่ (push-to-talk)") : "ไม่พร้อม", st.wake ? "ok" : "warn"],
    ["Microphone", `${mic[0]}${st.listening_enabled === false ? " · ปิดเสียงอยู่" : ""}`, mic[1]],
  ]);
}

function paintVoice() {
  const st = S.status;
  const mic = MIC[st.mic] || [st.mic, ""];
  kv($("voiceKv"), [
    ["ถอดเสียง (STT)", shortModel(st.stt_model)],
    ["ใช้เวลาถอดเสียงล่าสุด", st.stt_last_ms ? `${Math.round(st.stt_last_ms)} ms` : null],
    ["เสียงพูด (TTS)", st.tts_engine],
    ["ใช้เวลาสร้างเสียงล่าสุด", st.tts_last_ms ? `${Math.round(st.tts_last_ms)} ms` : null],
    ["Wake word", st.wake ? `พร้อม · ได้ยิน ${st.wake_count ?? 0} ครั้ง` : "ไม่พร้อม", st.wake ? "ok" : "warn"],
    ["คะแนนสูงสุด 30 วิ", st.wake_peak_30s != null ? String(st.wake_peak_30s) : null],
    ["ไมโครโฟน", `${mic[0]}${st.listening_enabled === false ? " · ปิดเสียงอยู่" : ""}`, mic[1]],
    ["อุปกรณ์รับเสียง", st.input_device != null ? String(st.input_device) : T("ค่าเริ่มต้น", "Default")],
  ]);
}
function renderHeard() {
  $("heard").innerHTML = S.heard.length
    ? S.heard.map((h) => `<li><span>${h.at.toLocaleTimeString("th-TH", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</span>${escapeHTML(h.text)}</li>`).join("")
    : `<li style="list-style:none;margin-left:-18px">${T("ยังไม่มีคำสั่งในรอบนี้", "Nothing yet this session")}</li>`;
}

/* ================================================================ live panel */
function paintClock() {
  if (!S.visible) return;
  const now = new Date();
  const locale = lang() === "th" ? "th-TH" : "en-GB";
  $("clockTime").textContent = now.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  $("clockDate").textContent = now.toLocaleDateString(locale, { weekday: "long", day: "numeric", month: "long", year: "numeric" });
}
setInterval(paintClock, 1000);
paintClock();

let liveTimer = 0;
async function refreshLive() {
  clearTimeout(liveTimer);
  if (S.visible && S.connected) {
    try {
      S.live = await api("/api/live");
      paintLive();
    } catch {
      S.live = null;
      paintLive();
    }
  }
  liveTimer = setTimeout(refreshLive, S.active ? 3000 : 6000);
}

function setBar(el, pct, warnAt = 75, badAt = 90) {
  el.style.width = `${Math.max(0, Math.min(100, pct))}%`;
  el.className = pct >= badAt ? "bad" : pct >= warnAt ? "warn" : "";
}
const gb = (b) => (b / 1073741824).toFixed(1);

function paintLive() {
  const L = S.live || {};
  if (typeof L.cpu === "number") {
    setBar($("cpuBar"), L.cpu);
    $("cpuVal").textContent = `${Math.round(L.cpu)}%`;
    S.cpuHist.push(L.cpu);
  } else $("cpuVal").textContent = "—";
  if (L.memory) {
    setBar($("memBar"), L.memory.percent, 80, 92);
    $("memVal").textContent = `${gb(L.memory.used)}G`;
    S.memHist.push(L.memory.percent);
  } else $("memVal").textContent = "—";
  S.cpuHist = S.cpuHist.slice(-60);
  S.memHist = S.memHist.slice(-60);
  if (L.battery) {
    const b = L.battery;
    setBar($("batBar"), b.percent, 101, 101);
    $("batBar").className = b.percent <= 15 && !b.plugged ? "bad" : b.percent <= 25 && !b.plugged ? "warn" : "";
    $("batVal").textContent = `${b.percent}%`;
    const state = b.charging ? T("กำลังชาร์จ", "Charging") : b.plugged ? T("เสียบสายอยู่ · ไม่ได้ชาร์จ", "Plugged in · not charging") : T("ใช้แบตเตอรี่", "On battery");
    $("batState").textContent = `${b.charging ? "⚡ " : ""}${state}${b.remaining ? ` · ${b.remaining} ${T("ชม.", "h")}` : ""}`;
  } else {
    $("batVal").textContent = "—";
    $("batState").textContent = T("ไม่พร้อมใช้งาน", "Unavailable");
  }
  const ok = typeof L.cpu === "number" && L.memory;
  const sys = $("sysOk");
  sys.textContent = ok ? (L.memory.percent > 92 ? T("หน่วยความจำตึง", "Memory tight") : "NOMINAL") : T("ไม่พร้อมใช้งาน", "Unavailable");
  sys.className = ok ? (L.memory.percent > 92 ? "warn" : "ok") : "warn";
  paintAi();
  if (S.view === "monitor") paintMonitor();
}

function paintAi() {
  const L = S.live || {};
  const st = S.status;
  const ai = L.ollama;
  const head = $("aiOk");
  if (!S.connected) { head.textContent = "OFFLINE"; head.className = "bad"; }
  else if (ai && !ai.up) { head.textContent = "OLLAMA DOWN"; head.className = "bad"; }
  else if (st.ready) { head.textContent = "ONLINE"; head.className = "ok"; }
  else { head.textContent = "STARTING"; head.className = "warn"; }
  kv($("aiKv"), [
    ["Model", cfg("llm.model", st.model || "—")],
    ["Ollama", ai ? (ai.up ? (ai.loaded ? T("โหลดแล้ว", "Loaded") + (ai.vram ? ` · ${gb(ai.vram)}G` : "") : T("ทำงาน · ยังไม่โหลดโมเดล", "Up · model not loaded")) : T("ไม่ทำงาน", "Down")) : null, ai ? (ai.up ? "ok" : "bad") : ""],
    ["STT", st.stt === "ready" ? T("พร้อม", "Ready") : st.stt ? T("ปิด", "Off") : null, st.stt === "ready" ? "ok" : "warn"],
    ["TTS", st.tts === "ready" ? T("พร้อม", "Ready") : st.tts ? T("ปิด", "Off") : null, st.tts === "ready" ? "ok" : "warn"],
    ["Wake", st.wake ? (st.listening_enabled === false ? T("ไมค์ปิด", "Muted") : cfg("listening.wake_word_enabled", true) ? T("พร้อม", "Ready") : T("ปิด", "Off")) : st.wake === false ? T("ไม่พร้อม", "Not ready") : null, st.wake && st.listening_enabled !== false ? "ok" : "warn"],
  ]);
}

/* ---------------------------------------------------------------- weather */
const WX_ICON = {
  sun: '<circle cx="24" cy="24" r="8"/><path d="M24 6v5M24 37v5M6 24h5M37 24h5M11.3 11.3l3.5 3.5M33.2 33.2l3.5 3.5M11.3 36.7l3.5-3.5M33.2 14.8l3.5-3.5"/>',
  moon: '<path d="M31 30a12 12 0 0 1-13-17 12 12 0 1 0 13 17z"/>',
  cloud: '<path d="M14 34h20a7 7 0 0 0 0-14 10 10 0 0 0-19-2 8 8 0 0 0-1 16z"/>',
  partly: '<path d="M18 12v3M9 21H6M11.6 14.6l2 2M26.4 14.6l-2 2"/><path d="M13 22a6 6 0 0 1 11-3"/><path d="M17 36h17a6 6 0 0 0 0-12 8 8 0 0 0-15-1 6.5 6.5 0 0 0-2 13z"/>',
  rain: '<path d="M14 28h20a7 7 0 0 0 0-14 10 10 0 0 0-19-2 8 8 0 0 0-1 16z"/><path d="M17 33l-2 5M25 33l-2 5M33 33l-2 5"/>',
  storm: '<path d="M14 26h20a7 7 0 0 0 0-14 10 10 0 0 0-19-2 8 8 0 0 0-1 16z"/><path d="M25 28l-4 7h6l-4 7"/>',
  fog: '<path d="M10 18h28M6 25h32M12 32h26"/>',
  snow: '<path d="M14 26h20a7 7 0 0 0 0-14 10 10 0 0 0-19-2 8 8 0 0 0-1 16z"/><path d="M18 34v.1M24 38v.1M30 34v.1"/>',
};
function wxKind(code, day) {
  if (code === 0 || code === 1) return day ? "sun" : "moon";
  if (code === 2) return "partly";
  if (code === 3) return "cloud";
  if (code === 45 || code === 48) return "fog";
  if (code >= 95) return "storm";
  if ((code >= 71 && code <= 77) || code === 85 || code === 86) return "snow";
  if (code >= 51) return "rain";
  return "cloud";
}
let wxTimer = 0;
async function refreshWeather(force) {
  clearTimeout(wxTimer);
  if (S.connected) {
    const data = await api(`/api/weather${force ? "?refresh=1" : ""}`).catch(() => ({ ok: false, error: "offline" }));
    S.weather = data;
    paintWeather();
  }
  wxTimer = setTimeout(refreshWeather, 10 * 60 * 1000);
}
function paintWeather() {
  const w = S.weather;
  if (!w || !w.ok) {
    $("wxCity").textContent = "—";
    $("wxTemp").textContent = "—";
    $("wxSky").textContent = T("ไม่พร้อมใช้งาน", "Unavailable");
    $("wxMeta").textContent = w && w.error ? w.error.slice(0, 80) : "";
    $("wxIcon").innerHTML = "";
    return;
  }
  $("wxCity").textContent = w.city;
  $("wxTemp").textContent = `${w.temp}°${w.unit}`;
  $("wxSky").textContent = w.sky;
  const rain = w.rain != null ? `${T("ฝน", "Rain")} ${w.rain}%` : "";
  $("wxMeta").textContent = [`H ${w.hi}° L ${w.lo}°`, rain, w.humidity != null ? `RH ${w.humidity}%` : ""].filter(Boolean).join("  ·  ");
  $("wxIcon").innerHTML = WX_ICON[wxKind(w.code, w.day)];
}

/* ---------------------------------------------------------------- music */
let musicTimer = 0;
function musicInterval() {
  // Each poll is one AppleScript call into Chrome (plus a TCC check), so poll gently;
  // music commands refresh the card immediately anyway.
  if (!S.visible) return 0;
  if (S.view === "music") return S.active ? 3000 : 8000;
  return S.active ? 10000 : 20000;
}
async function refreshMusic() {
  clearTimeout(musicTimer);
  if (S.visible && S.connected && !S.musicBusy) {
    const data = await api("/api/music").catch(() => ({ available: false, reason: "offline" }));
    S.music = { ...data, fetchedAt: performance.now() };
    paintMusic();
  }
  const every = musicInterval();
  if (every) musicTimer = setTimeout(refreshMusic, every);
}
function refreshMusicSoon(tool) {
  if (tool === "youtube_music") setTimeout(refreshMusic, 600);
}
const fmt = (s) => {
  s = Math.max(0, Math.floor(s || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
};
const REASONS = {
  chrome_closed: ["Chrome ปิดอยู่", "Chrome is closed"],
  no_tab: ["ไม่มีแท็บ YouTube Music", "No YouTube Music tab"],
  nothing_loaded: ["ยังไม่ได้เลือกเพลง", "Nothing loaded"],
  js_blocked: ["เปิด Allow JavaScript from Apple Events ใน Chrome", "Enable Allow JavaScript from Apple Events in Chrome"],
  error: ["อ่านสถานะไม่ได้", "Could not read the player"],
  offline: ["ไม่พร้อมใช้งาน", "Unavailable"],
};
function paintMusic() {
  const m = S.music || {};
  const ok = m.available;
  const reason = REASONS[m.reason] || REASONS.offline;
  const status = ok ? (m.ad ? T("กำลังเล่นโฆษณา", "Ad playing") : m.paused ? T("หยุดชั่วคราว", "Paused") : T("กำลังเล่น", "Playing")) : T(reason[0], reason[1]);
  // Live panel tile.
  $("npState").textContent = ok ? (m.paused ? "PAUSED" : m.ad ? "AD" : "PLAYING") : "—";
  $("npState").className = ok && !m.paused ? "ok" : "";
  $("npTitle").textContent = ok ? m.title : T(reason[0], reason[1]);
  $("npArtist").textContent = ok ? m.artist : "";
  $("npArt").style.backgroundImage = ok && m.art ? `url("${m.art}")` : "";
  const playIcon = '<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>';
  const pauseIcon = '<svg viewBox="0 0 24 24"><path d="M8 5v14M16 5v14"/></svg>';
  $("npToggle").innerHTML = ok && !m.paused ? pauseIcon : playIcon;
  $("musicToggle").innerHTML = ok && !m.paused ? pauseIcon : playIcon;
  // Music view.
  $("musicStatus").textContent = status;
  $("musicTitle").textContent = ok ? m.title : T("ยังไม่มีเพลง", "Nothing playing");
  $("musicArtist").textContent = ok ? m.artist : "";
  $("musicAlbum").textContent = ok ? m.album || "" : (m.detail || "");
  const art = $("musicArt");
  if (ok && m.art) {
    if (art.dataset.src !== m.art) {
      art.innerHTML = `<img alt="" src="${escapeHTML(m.art)}">`;
      art.dataset.src = m.art;
      $("musicHero").style.setProperty("--art-bg", `url("${m.art}")`);
    }
  } else if (art.dataset.src) {
    art.innerHTML = '<div class="artEmpty">♪</div>';
    delete art.dataset.src;
    $("musicHero").style.removeProperty("--art-bg");
  }
  paintProgress();
}
function paintProgress() {
  const m = S.music || {};
  if (!m.available || !m.duration) {
    $("musicPos").textContent = "0:00";
    $("musicDur").textContent = m.duration ? fmt(m.duration) : "0:00";
    $("musicBar").style.width = "0%";
    return;
  }
  // The player's clock, advanced locally between polls only while it is really playing.
  const elapsed = !m.paused && !m.ad ? (performance.now() - m.fetchedAt) / 1000 : 0;
  const pos = Math.min(m.duration, m.time + elapsed);
  $("musicPos").textContent = fmt(pos);
  $("musicDur").textContent = fmt(m.duration);
  $("musicBar").style.width = `${(100 * pos) / m.duration}%`;
}
setInterval(() => { if (S.visible && S.view === "music") paintProgress(); }, 500);

document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-music]");
  if (!b) return;
  await musicAction({ action: b.dataset.music });
});
async function musicAction(body) {
  if (S.musicBusy) return;
  S.musicBusy = true;
  for (const btn of $$("[data-music]")) btn.disabled = true;
  try {
    const res = await api("/api/music", body);
    if (res.error) toast(res.error, "error");
    else {
      if (!res.ok) toast(res.result, "warn", 6000);
      else if (body.query) toast(res.result, "ok", 5000);
      if (res.state) { S.music = { ...res.state, fetchedAt: performance.now() }; paintMusic(); }
    }
  } catch (err) {
    toast(String(err), "error");
  } finally {
    S.musicBusy = false;
    for (const btn of $$("[data-music]")) btn.disabled = false;
    refreshMusic();
  }
}
$("musicForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = e.target.elements.q.value.trim();
  if (!q) return;
  musicAction({ action: "play", query: q });
});
async function loadVolume() {
  const v = await api("/api/volume").catch(() => ({}));
  if (v.ok) {
    $("musicVol").value = v.level;
    $("musicVolOut").textContent = v.muted ? T("ปิดเสียง", "Muted") : `${v.level}%`;
  } else $("musicVolOut").textContent = "—";
}
let volTimer = 0;
$("musicVol").addEventListener("input", (e) => {
  $("musicVolOut").textContent = `${e.target.value}%`;
  clearTimeout(volTimer);
  volTimer = setTimeout(async () => {
    const res = await api("/api/volume", { level: Number(e.target.value) }).catch(() => ({}));
    if (!res.ok) toast(res.error || T("ปรับเสียงไม่ได้", "Could not set the volume"), "error");
  }, 160);
});

/* ================================================================ actions, tasks, calendar */
async function runAction(id, extra = {}, out) {
  if (out) { out.textContent = T("กำลังทำงาน…", "Working…"); out.className = "result busy"; }
  const res = await api("/api/action", { id, ...extra }).catch((e) => ({ ok: false, text: String(e) }));
  const text = res.text || res.error || "";
  if (out) { out.textContent = text || T("ไม่มีข้อมูล", "Nothing returned"); out.className = `result${res.ok ? "" : " error"}`; }
  else toast(text, res.ok ? "ok" : "error", 5000);
  return res;
}
for (const b of $$(".quick [data-action]")) {
  b.addEventListener("click", async () => {
    b.classList.add("busy");
    b.disabled = true;
    try { await runAction(b.dataset.action); } finally { b.classList.remove("busy"); b.disabled = false; }
  });
}
for (const b of $$("[data-act]")) {
  b.addEventListener("click", async () => {
    const out = b.closest(".card").querySelector(".result");
    b.disabled = true;
    try {
      await runAction(b.dataset.act, {}, out);
      if (b.dataset.act.startsWith("timers")) loadTimers();
    } finally { b.disabled = false; }
  });
}
function formAction(formId, id, after) {
  $(formId).addEventListener("submit", async (e) => {
    e.preventDefault();
    const data = Object.fromEntries(new FormData(e.target).entries());
    if (data.duration_minutes) data.duration_minutes = Number(data.duration_minutes);
    const out = e.target.closest(".card").querySelector(".result");
    const res = await runAction(id, data, out);
    if (res.ok) e.target.reset();
    if (after) after();
  });
}
formAction("timerForm", "timer_create", () => loadTimers());
formAction("reminderForm", "reminder_create");
formAction("eventForm", "event_create");

async function loadTimers() {
  const data = await api("/api/timers").catch(() => ({ items: [] }));
  S.timers = data.items || [];
  renderTimers();
}
function renderTimers() {
  const ul = $("timerList");
  if (!ul) return;
  const items = (S.timers || []).filter((t) => t.fire_at * 1000 > Date.now() - 1000);
  if (!items.length) {
    ul.innerHTML = `<li class="empty">${T("ไม่มีตั้งเวลาที่กำลังนับ", "No running timers")}</li>`;
    return;
  }
  ul.innerHTML = items.map((t) => {
    const left = Math.max(0, Math.round(t.fire_at - Date.now() / 1000));
    const h = Math.floor(left / 3600);
    const time = h ? `${h}:${String(Math.floor((left % 3600) / 60)).padStart(2, "0")}:${String(left % 60).padStart(2, "0")}` : fmt(left);
    const at = new Date(t.fire_at * 1000).toLocaleTimeString("th-TH", { hour: "2-digit", minute: "2-digit" });
    return `<li><span>${escapeHTML(t.label || t.text || t.kind)}</span><span class="mono">${time} · ${at}</span></li>`;
  }).join("");
}
setInterval(() => { if (S.visible && S.view === "tasks") renderTimers(); }, 1000);

/* ================================================================ monitor & history */
function spark(canvas, values, max = 100) {
  const dpr = Math.min(2, devicePixelRatio || 1);
  const w = canvas.clientWidth * dpr;
  const h = canvas.clientHeight * dpr;
  if (!w || !h) return;
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext("2d");
  const acc = getComputedStyle(document.documentElement).getPropertyValue("--acc").trim();
  ctx.clearRect(0, 0, w, h);
  if (values.length < 2) return;
  ctx.beginPath();
  values.forEach((v, i) => {
    const x = (i / (values.length - 1)) * w;
    const y = h - (Math.min(max, v) / max) * (h - 4) - 2;
    if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y);
  });
  ctx.strokeStyle = `rgba(${acc},.9)`;
  ctx.lineWidth = 1.5 * dpr;
  ctx.stroke();
  ctx.lineTo(w, h);
  ctx.lineTo(0, h);
  ctx.closePath();
  ctx.fillStyle = `rgba(${acc},.12)`;
  ctx.fill();
}
function paintMonitor() {
  const L = S.live || {};
  const st = S.status;
  $("monCpu").textContent = typeof L.cpu === "number" ? `${L.cpu.toFixed(1)}%` : "—";
  $("monMem").textContent = L.memory ? `${L.memory.percent.toFixed(0)}%` : "—";
  $("monMemSub").textContent = L.memory ? `${gb(L.memory.used)} / ${gb(L.memory.total)} GB · free level ${L.memory.pressure_free ?? "—"}%` : T("ไม่พร้อมใช้งาน", "Unavailable");
  $("monBat").textContent = L.battery ? `${L.battery.percent}%` : "—";
  $("monBatSub").textContent = $("batState").textContent;
  const cs = core.stats();
  $("monFps").textContent = `${cs.fps} fps`;
  $("monFpsSub").textContent = `draw ${cs.drawMs} ms · cap ${cs.budget || 0} fps · ${cfg("ui.animation", "high")}`;
  spark($("sparkCpu"), S.cpuHist);
  spark($("sparkMem"), S.memHist);
  const ai = L.ollama || {};
  kv($("monAi"), [
    ["Ollama", ai.up ? T("ทำงาน", "Up") : T("ไม่ทำงาน", "Down"), ai.up ? "ok" : "bad"],
    ["Model", cfg("llm.model", "—")],
    ["โหลดอยู่ในหน่วยความจำ", (ai.running || []).join(", ") || "—"],
    ["VRAM ของโมเดล", ai.vram ? `${gb(ai.vram)} GB` : null],
    ["STT", shortModel(st.stt_model)],
    ["TTS", st.tts_engine],
  ]);
  const learn = st.learning || {};
  kv($("monBrain"), [
    ["Brain pid", st.pid != null ? String(st.pid) : null],
    ["สถานะ", (CHIP[effectiveState()] || "").toString()],
    ["ไมโครโฟน", (MIC[st.mic] || [st.mic])[0], (MIC[st.mic] || [])[1]],
    ["เฟรมเสียงที่รับ", st.mic_frames != null ? Number(st.mic_frames).toLocaleString() : null],
    ["อุปกรณ์ออก", st.output_device != null ? String(st.output_device) : T("ค่าเริ่มต้น", "Default")],
    ["บทเรียน / ทักษะ / งานที่ทำ", `${learn.lessons ?? "—"} / ${learn.skills ?? "—"} / ${learn.episodes ?? "—"}`],
  ]);
}
setInterval(() => {
  if (!S.visible) return;
  const cs = core.stats();
  $("sideFps").textContent = cs.budget ? `${cs.fps}FPS` : "";
  if (S.view === "monitor") {
    $("monFps").textContent = `${cs.fps} fps`;
    $("monFpsSub").textContent = `draw ${cs.drawMs} ms · cap ${cs.budget || 0} fps · ${cfg("ui.animation", "high")}`;
  }
}, 1000);

async function loadHistory() {
  const data = await api("/api/history?limit=150").catch(() => ({}));
  const eps = data.episodes || [];
  $("episodes").innerHTML = eps.length ? eps.map((e) => {
    const when = new Date(e.ts * 1000);
    const bad = e.errors > 0 || e.corrected;
    const res = e.corrected ? T("ถูกแก้", "corrected") : e.errors ? T("ผิดพลาด", "error") : `${(e.seconds || 0).toFixed(1)}s`;
    return `<li><time>${when.toLocaleDateString("th-TH", { day: "numeric", month: "short" })}<br>${when.toLocaleTimeString("th-TH", { hour: "2-digit", minute: "2-digit" })}</time>` +
      `<div><div class="req">${escapeHTML(e.request)}</div>${e.plan.length ? `<div class="plan">${e.plan.map((p) => `<span>${escapeHTML(p)}</span>`).join("")}</div>` : ""}</div>` +
      `<div class="res${bad ? " bad" : ""}">${escapeHTML(res)}</div></li>`;
  }).join("") : `<li class="empty">${T("ยังไม่มีประวัติ", "No history yet")}</li>`;
  const tools = data.tools || [];
  $("toolStats").innerHTML = `<tr><th>TOOL</th><th style="text-align:right">CALLS</th><th style="text-align:right">FAIL</th><th style="text-align:right">OK%</th></tr>` +
    tools.map((t) => `<tr title="${escapeHTML(t.last_error || "")}"><td>${escapeHTML(t.name)}</td><td class="n">${t.calls}</td><td class="n">${t.failures}</td><td class="n">${t.calls ? Math.round(100 * (1 - t.failures / t.calls)) : 0}</td></tr>`).join("");
}

/* ================================================================ toasts */
function toast(text, level = "info", ms = 4200) {
  if (!text) return;
  const el = document.createElement("div");
  el.className = `toast ${level}`;
  el.textContent = text;
  $("toasts").appendChild(el);
  while ($("toasts").children.length > 4) $("toasts").firstElementChild.remove();
  setTimeout(() => el.remove(), ms);
}

/* ================================================================ boot */
renderHeard();
renderTimers();
paintState();
paintLink();
showView(location.hash.slice(1) || localStorage.getItem("cc.view") || "dashboard");
window.addEventListener("hashchange", () => showView(location.hash.slice(1)));
connect();
// Expose the core's measured numbers for profiling from the inspector.
window.jarvisCoreStats = () => core.stats();
