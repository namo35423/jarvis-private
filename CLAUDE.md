# Jarvis — contributor notes

For people (and coding agents like Claude Code) working on Jarvis. Read
`README.md` first for what it does; this file is about how it is built.

## Shape

```
app/    Swift shell: menu-bar app (LSUIElement), glass dropdown panel (MenuPanel.swift), full-size
        Command Center window (CommandCenter.swift), ⌘\ hotkey, login item, supervises the brain
brain/  Python 3.12 backend - the actual assistant
ui/     what the windows show, no build step: index.html/app.js (panel, WebGL2 orb) and
        command.html/command.js/core.js (Command Center, Canvas 2D core)
bin/    the `jarvis` CLI (also how you drive it from a session)
```

The Swift app launches `brain/.venv/bin/python -m jarvis` as a **child process**.
That is deliberate: macOS attributes the child's Microphone, Accessibility,
Screen Recording and Automation grants to `Jarvis.app`. Never make the brain a
LaunchAgent or run it detached in production — it loses every permission.

## Working on it

```bash
jarvis restart                      # Python changes: the app respawns the brain
app/install.sh                      # Swift changes: rebuild + reinstall + relaunch
jarvis status                       # health, and what it has learned
tail -f ~/.jarvis/logs/brain.log    # everything the brain does
cd brain && ./.venv/bin/python tools_probe.py NAME '{"arg":"value"}'   # one tool, no voice
JARVIS_NO_AUDIO=1 ./.venv/bin/python -m jarvis   # headless, for testing the model/tool path
```

`install.sh` kills the running copy first, and re-verifies the app actually
launched (LaunchServices silently no-ops an `open` on a just-replaced bundle).

## Module map (brain/jarvis/)

| File | Responsibility |
|---|---|
| `core.py` | the state machine and the turn loop. Start here. |
| `listener.py` | wake word + VAD on a worker thread; decides when a turn starts and ends |
| `audio.py` | PortAudio in/out. Opening a stream can block — it is always run off the loop |
| `stt.py` / `tts.py` | Parakeet (MLX) and Kokoro (ONNX) |
| `llm.py` | Ollama streaming chat with tools; recovers tool calls emitted as text |
| `learning.py` | lessons, cached skills, tool stats |
| `jobs.py` | Claude Code hand-offs (stream-json parsing, progress, cancel) |
| `tools/` | everything the model can actually do; `__init__.py` is the contract |
| `server.py` | aiohttp: WebSocket to the UI, HTTP API for the hotkey, CLI and Command Center |
| `live.py` | CPU / memory / battery / Ollama readings for the Command Center (no extra deps) |

## Things that will bite you

- **Never block the event loop.** Audio device opens, AppleScript and model calls
  all go through executors or async. A blocked loop freezes the whole assistant,
  including the server, and it looks like a hang with no error.
- **Keep the system prompt byte-identical between turns.** Ollama reuses the KV
  cache of the previous prompt prefix; anything time-varying (the clock, the
  frontmost app) belongs in the user message. `prompts.py` enforces this split.
- **Tool results are spoken.** Return one or two factual sentences, not a dump.
  4000 chars is the hard cap, but anything near it is already wrong.
- **Small models repeat failing calls.** `core._failed_calls` refuses an identical
  call twice in one turn; keep that when refactoring.
- **AppleScript timeouts usually mean TCC**, not slowness — `sysutil.osascript`
  converts both into an actionable "grant Jarvis permission to control X".
- **Bulk AppleScript is ~5x faster**: `title of tabs of window w`, not a loop over
  tab objects.
- Adding a tool changes the prompt prefix for every request, so keep descriptions
  tight. The ~30 tools cost ~4k tokens of prompt (cached after the first request).

## Testing without a microphone

The mic needs a real grant against `Jarvis.app`, so most work is easier headless:

```bash
curl -s -XPOST localhost:8765/api/command -H 'content-type: application/json' \
  -d '{"text":"how much disk space do i have","speak":false}'
```

`speak:false` keeps it silent.

## Self-editing ("change yourself to…")

`tools/self_edit.py` + `scripts/self_edit_guard.sh` + `brain/verify.sh`. Jarvis
checkpoints to git, Claude Code edits (allowed shell: verify.sh and build.sh only),
Jarvis verifies, then a guard in its own session restarts it and requires a NEW pid
to stay ready; otherwise it restores only the touched paths and force-restarts the
app. Those three files are protected: an edit touching them is rejected.
Both paths are drilled: a good edit is kept, and an edit that crashes on startup is
rolled back unattended. Needs the `claude` CLI logged in (`claude auth status`).

## Model choice

`qwen3:4b-instruct`, not qwen3.5. Qwen 3.5 is a hybrid (recurrent) architecture:
its KV cache is only reused for a strict continuation, so any divergence - a fast
command between questions, a background call, a history trim - re-read the whole
~6k-token prompt (3-7s). Measured on an M3 MacBook Air: median 6.5s vs 1.2s per request. Background
model calls (`_side_chat`) start from the full conversation so they extend the
cache instead of replacing it.

## Before opening a pull request

```bash
brain/verify.sh        # the brain imports and every tool registers
app/build.sh           # the menu bar app compiles
```

## Command Center (the full-size window)

Opened from Spotlight, Finder, a Dock click, ⌘0, the menu (right-click the icon) or the
panel's "center" button. The Dock icon exists only while it is open; closing it frees
the web view entirely and Jarvis is a menu-bar app again. At login it stays hidden.

- Everything on screen comes from brain events or `/api/live`, `/api/music`,
  `/api/weather`, `/api/history`, `/api/action` (fixed button -> tool map; the tool's
  own confirmation still applies). Show "ไม่พร้อมใช้งาน" rather than invent a value.
- Fast-path and button tool calls go through `Assistant._exec`, which broadcasts the
  same `tool` events as a model call and sets the `working` (EXECUTING) state.
- The core caps its frame rate per state (idle 20 fps, active 60) and stops when the
  window cannot be seen. `curl localhost:8765/api/uistats` returns the measured fps of
  every open window, on demand.
- Open `http://127.0.0.1:8765/command.html#chat` in a browser to work on it without
  rebuilding; host-only settings (login item, restart, logs) need the app.
