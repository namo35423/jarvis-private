# Jarvis

A voice assistant for your Mac that runs **entirely on your own computer**. It
listens for "Hey Jarvis", understands you, talks back in a natural voice, and
actually operates the Mac: apps, files, browser, music, reminders, the screen.

No accounts, no API keys, no subscription. Your voice never leaves the machine.

```
Say "Hey Jarvis"   ·   or press ⌘\   ·   or click the icon in the menu bar
```

## Highlights

- **Fast.** Common commands skip the language model entirely: the time, volume,
  play/pause, opening or quitting an app answer in under half a second. Most
  questions take 1–2.5 seconds on an M-series MacBook Air.
- **Does things, not just talks.** About 30 tools: open and quit apps, find and
  organise files, control Chrome tabs and pages, Spotify and Music, timers,
  Reminders, Calendar, Notes, iMessage, web search, weather, and "what's on my
  screen".
- **Remembers you.** It picks up facts you mention ("my sister is called Maya"),
  keeps the conversation across restarts, and uses both later.
- **Learns from mistakes.** Say "that's wrong" or "it didn't work" and it retries
  with a different approach, then keeps a one-line rule so it doesn't repeat the
  mistake. Requests you repeat become instant shortcuts.
- **Interruptible.** Say "Hey Jarvis" while it's talking and it stops and listens.
- **Optional heavy lifting.** If you have [Claude Code](https://claude.com/claude-code)
  installed, Jarvis can hand off big jobs (write code, build a presentation, deep
  research) and announce the result when it's done. It can even change its own
  code when you ask, with automatic rollback if the change breaks it.
- **Lives in the menu bar.** No Dock icon, starts at login, always listening.

## Requirements

- A Mac with **Apple Silicon** (M1 or newer)
- **macOS 14** Sonoma or newer
- **16 GB of memory** recommended (8 GB works, but tightly)
- About **10 GB** of free disk space for the models
- [Ollama](https://ollama.com/download) (free) — runs the language model
- Xcode Command Line Tools (`setup.sh` offers to install them)

## Install

```bash
git clone https://github.com/BatmanOnTop/jarvis.git
cd jarvis
./setup.sh
```

`setup.sh` checks your Mac, downloads the models (~5 GB total), creates the Python
environment, builds the app into `~/Applications/Jarvis.app`, and launches it. It
is safe to run again at any time.

Then:

1. Click **Allow** when macOS asks for microphone access.
2. Say **"Hey Jarvis"** and wait for the chime.
3. Optional: run `jarvis grant` once to answer every "Jarvis wants to control…"
   prompt in one go, instead of one at a time as you use each app.

## Things to say

| | |
|---|---|
| "Open Spotify" · "Close it" · "Quit Calculator" | instant |
| "What's the weather?" · "Is it going to rain later?" | instant |
| "Turn the volume down" · "Pause" · "What's playing?" | instant |
| "Set a timer for 12 minutes" · "Remind me to call Mom at 6" | |
| "What's on my calendar tomorrow?" · "Text Maya I'm running late" | |
| "Find my resume PDF" · "Organize my Downloads folder" | |
| "What tabs do I have open?" · "Summarize this page" | |
| "What's on my screen?" · "How much disk space is free?" | |
| "My favorite color is green" · later: "What's my favorite color?" | memory |
| "No, that's wrong" · "It didn't work" | retries differently, then learns |
| "Make me a presentation about black holes" | needs Claude Code |
| "Change your voice to sound calmer" · "Undo your last change" | needs Claude Code |

## How it works

| Part | Model | Runs |
|---|---|---|
| Wake word | [openWakeWord](https://github.com/dscripka/openWakeWord) "hey jarvis" + Silero VAD | on device, ~1% CPU |
| Speech to text | [Parakeet TDT 0.6B v3](https://huggingface.co/mlx-community/parakeet-tdt-0.6b-v3) via MLX | Apple GPU |
| Thinking | [Qwen3 4B Instruct](https://ollama.com/library/qwen3) via Ollama, with tool calling | Apple GPU |
| Voice | [Kokoro 82M](https://github.com/thewh1teagle/kokoro-onnx) (`bm_george`) | on device, faster than real time |
| Big jobs (optional) | Claude Code CLI | your Claude plan |

A small native Swift app sits in the menu bar. It starts a Python "brain" as its
child process, which is what lets macOS attach the Microphone, Accessibility and
Automation permissions to *Jarvis* rather than to Python. If the brain ever
crashes or hangs, the app restarts it.

```
app/      Swift menu bar app (build.sh, install.sh)
brain/    Python assistant: listener, speech, model, tools, memory, learning
ui/       the dropdown panel (HTML/CSS/JS, WebGL orb)
bin/      the `jarvis` command-line tool
scripts/  model downloads, self-edit guard
```

Your data lives in `~/.jarvis/`: settings (`config.json`), memory and learned
rules (`jarvis.db`), downloaded models, and logs.

## Permissions

macOS asks the first time each one is needed. Only the microphone is required.

| Permission | Used for | Without it |
|---|---|---|
| **Microphone** | hearing you | you can still type commands in the panel |
| Automation (per app) | controlling Chrome, Spotify, Calendar, Notes… | that app's commands explain what to enable |
| Accessibility | typing, clicking, keyboard shortcuts | those commands explain what to enable |
| Screen Recording | "what's on my screen" | screen commands explain what to enable |

To let Jarvis read and click *inside* web pages in Chrome: **View → Developer →
Allow JavaScript from Apple Events**.

## Safety

Jarvis has real control of your Mac. It asks out loud before the few things that
can't be taken back: sending a message, moving a file to the Trash, and shell
commands that match a destructive pattern (`rm`, `sudo`, erasing a disk). Anything
else runs without asking. You can turn confirmations off in Settings.

Self-editing ("change yourself…") saves a git checkpoint first, lets Claude Code
edit only the Jarvis folder, checks the result loads, and restarts. If the new
version fails its checks or doesn't come back up, only the files it touched are
restored and Jarvis tells you. The rollback machinery itself can't be edited this
way.

## Settings

Left-click the menu bar icon, then **Settings**: voice, speaking rate, wake-word
sensitivity, how long a pause ends your turn, follow-up window, confirmations, and
Claude hand-off. Right-click the icon for mute, restart, logs and start-at-login.

Everything is stored in `~/.jarvis/config.json`, which only holds what you
changed. A few useful keys:

```jsonc
{
  "user_name": "Alex",
  "voice":     { "voice": "bm_george", "speed": 1.3 },       // bm_fable, bm_lewis, af_heart, ...
  "listening": { "end_silence_ms": 550, "followup_seconds": 5.0, "wake_threshold": 0.5 },
  "llm":       { "model": "qwen3:4b-instruct" },
  "location":  { "city": "London", "units": "metric" },
  "claude":    { "mode": "auto" }                             // auto | explicit | off
}
```

## Command line

```bash
jarvis "open spotify"      # run one instruction and print the reply
jarvis say "dinner's up"   # speak something
jarvis listen              # same as pressing ⌘\
jarvis status              # what's loaded, what it has learned
jarvis grant               # walk through the app permission prompts
jarvis mute / unmute       # turn the wake word off and on
jarvis logs                # follow the log
jarvis restart             # restart the brain
```

Everything is also available over a local HTTP API on `127.0.0.1:8765`
(`POST /api/command {"text": "..."}`), so scripts and other agents can drive it.

## Troubleshooting

| Symptom | Fix |
|---|---|
| The icon never reacts to your voice | `jarvis status` — if microphone says `waiting` or `denied`, enable Jarvis in System Settings → Privacy & Security → Microphone |
| "Jarvis doesn't have permission to control X" | run `jarvis grant`, or System Settings → Privacy & Security → Automation |
| It hears "Hey Jarvis" too often / not often enough | Settings → wake sensitivity |
| Claude hand-off says it isn't signed in | run `claude` in Terminal and type `/login` |
| Anything else | `jarvis logs`, or `~/.jarvis/logs/brain.log` |

## Contributing

Issues and pull requests are welcome. `CLAUDE.md` explains the architecture and
the traps (it doubles as context for coding agents). Before opening a PR:

```bash
brain/verify.sh    # the brain loads and every tool registers
app/build.sh       # the menu bar app compiles
```

To add a capability, add a tool in `brain/jarvis/tools/` — see the contract at the
top of `brain/jarvis/tools/__init__.py`.

## License

Jarvis is released under the [MIT License](LICENSE).

The models it downloads have their own licenses, which you accept when
`setup.sh` fetches them:

- Qwen3 — Apache 2.0
- Kokoro 82M — Apache 2.0
- Parakeet TDT 0.6B v3 — CC BY 4.0
- openWakeWord pre-trained models, including "hey jarvis" — **CC BY-NC-SA 4.0
  (non-commercial use only)**
- Silero VAD — MIT

Jarvis is an independent project. It is not affiliated with or endorsed by
Marvel, Apple, Anthropic, Alibaba, or any model author.
