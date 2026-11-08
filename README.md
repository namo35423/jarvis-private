# Jarvis

A voice assistant for your Mac that runs **entirely on your own computer**. It
listens for "Hey Jarvis", understands you, talks back in a natural voice, and
actually operates the Mac: apps, files, browser, music, reminders, the screen.

No accounts, no API keys, no subscription. Your voice never leaves the machine.

```
Say "Hey Jarvis"   ·   or press ⌘\   ·   or click the icon in the menu bar
```

## ฉบับภาษาไทย · Thai-optimized edition

ฉบับนี้ปรับ Jarvis ให้ **ใช้ภาษาไทยได้จริงในชีวิตประจำวัน**: ฟังไทย ตอบเป็นเสียงไทย
เข้าใจคำพูดแบบคนไทย และสั่งงานแอปที่คนไทยใช้จริงอย่าง LINE และ YouTube Music
ทุกอย่างยังรันในเครื่อง พัฒนาต่อจาก [BatmanOnTop/jarvis](https://github.com/BatmanOnTop/jarvis) (MIT)

*This edition tunes Jarvis for Thai speakers: Thai speech in and out, everyday Thai
commands that run without waiting for the model, Thai time and dates, LINE and
YouTube Music, and a full-size Command Center window.*

**ไม่ได้ฝึกโมเดลใหม่ (no fine-tuning).** ใช้โมเดลโอเพนซอร์สที่รันในเครื่องเหมือนเดิม
แต่ปรับทุกชั้นรอบโมเดลให้เข้ากับภาษาไทย ตั้งแต่การฟัง การตีความคำสั่ง ไปจนถึงเสียงพูด

### ปรับอะไรมาบ้าง

| ส่วน | ปรับอย่างไร |
|---|---|
| **ฟังเสียง (STT)** | ใช้ [MLX Whisper Large V3 Turbo](https://huggingface.co/mlx-community/whisper-large-v3-turbo) ฟังไทยปนอังกฤษได้ ถ้าตรวจภาษาได้เป็นภาษาอื่นจะถอดใหม่เป็นไทย แก้คำที่ถอดผิดบ่อย (เช่น "เพียง" → "เพลง") และตัดคำลงท้ายอย่าง ครับ ค่ะ ดิ อ่ะ เว้ย ก่อนตีความ |
| **ตีความคำสั่งไทย** | ตัวแยกคำสั่งภาษาไทยแบบตายตัว (ไม่ต้องรอ LLM) ครอบคลุมเปิด/ปิดแอปและเว็บไซต์ (ไอจี เฟซบุ๊ก พันทิป ฯลฯ), เพลง, เสียง, แบตเตอรี่, พื้นที่ดิสก์, Wi-Fi, ตั้งเวลา, ปลุก, เตือนความจำ, ปฏิทิน, อากาศ, ค่าเงิน และข่าว: **92 จาก 99 คำสั่งทดสอบทำงานทันที** |
| **เวลาแบบไทย** | เข้าใจระบบหกชั่วโมง: ตีสอง · บ่ายสาม · หกโมงเย็น · สองทุ่ม รวมถึง พรุ่งนี้ · มะรืน · ศุกร์หน้า ("เตือนกินยาตอนสองทุ่ม" = 20:00 ไม่ใช่ 14:00) |
| **สมอง (LLM)** | [Qwen3 4B Instruct](https://ollama.com/library/qwen3) ผ่าน Ollama เลือกแทน Qwen3.5 เพราะใช้ KV cache ซ้ำได้: median 1.2 วิ เทียบกับ 6.5 วิต่อคำขอ (วัดบน M3 MacBook Air) และเพิ่มกฎภาษาไทยใน system prompt |
| **กันตอบมั่ว** | คำถามข้อมูลสด (อากาศ ค่าเงิน ข่าว ราคา) เรียกเครื่องมือก่อนตอบเสมอ ถ้าโมเดลอ้างว่า "ทำแล้ว" โดยไม่ได้ทำจริง จะถูกสั่งให้ทำ หรือต้องบอกตรงๆ ว่ายังไม่ได้ทำ และตัวเลขที่ไม่มีในผลค้นหาจะไม่ถูกพูดออกไป |
| **เสียงพูด (TTS)** | เสียงไทยของ macOS (Kanya) เป็นค่าเริ่มต้น หรือเสียงไทย neural แบบออฟไลน์ [Wayu-Paxa-TTS-Edge](https://huggingface.co/wayu-ai/wayu-paxa-tts-edge) มีเสียงผู้ชาย 5 แบบ ([วิธีติดตั้ง](brain/tts_runtime/README.md)) |
| **แอปที่คนไทยใช้** | ส่ง **LINE** ได้ (เช็กชื่อแชทก่อนส่งและถามยืนยันทุกครั้ง) · **YouTube Music** ค้นหา เลือกเพลงที่ชื่อและศิลปินตรง แล้วตรวจว่าเล่นถูกเพลงจริงก่อนตอบ |
| **ข้อมูลท้องถิ่น** | องศาเซลเซียส · ค้นชื่อสถานที่เป็นภาษาไทย · แปลงค่าเงินเป็นบาท · ข่าวไทย · สลับเมืองระหว่างฮ่องกงกับกรุงเทพฯ ได้ใน Settings |
| **Command Center** | หน้าต่างขนาดใหญ่ ปรับขยายเต็มจอได้ วงแหวน AI เคลื่อนไหวตามสถานะจริง (ฟัง / คิด / ใช้เครื่องมือ / พูด) มีหน้าแชต เพลง งาน ปฏิทิน ข้อมูลเครื่อง ประวัติ และ Settings |

### ผลวัดจริง (MacBook Pro M1 Pro, 16 GB)

| ทดสอบ | ผล |
|---|---|
| เลือกเครื่องมือถูก (69 คำสั่งชีวิตประจำวัน, `qwen3:4b-instruct`) | ก่อนปรับ 57 → หลังปรับ 69 ข้อ¹ |
| คำพูดแบบอื่นที่ไม่เคยทดสอบ (30 คำสั่ง) | 30/30 |
| คำสั่งที่ทำงานโดยไม่ต้องรอโมเดล | 92 จาก 99 คำสั่ง ตอบใน 0.1–0.5 วินาที |
| "เปิดเพลง Shape of You ของ Ed Sheeran" | เล่นถูกเพลงใน 4–5 วินาที (รวมตรวจว่าเล่นจริง) |
| ถามพยากรณ์อากาศ | ต่ำกว่า 2 วินาที (เดิม 3–6 วินาที) |
| สร้างเสียงไทย Wayu (หลังโหลดแล้ว) | 0.5–0.7 วินาทีต่อประโยคสั้น |
| Command Center ตอนว่าง | ประมาณ 17% ของ CPU 1 core ที่ 20 fps |

¹ มี 3 ข้อที่ชุดทดสอบนับผิด (คำทักทาย ขอบคุณ และเลขคณิต ซึ่ง Jarvis ตอบทันทีเอง) ตรวจแล้วว่าตอบถูกเมื่อใช้งานจริง

### ลองพูดว่า

- เปิดเพลง Shape of You ของ Ed Sheeran · ข้ามเพลง · เพลงอะไรอยู่
- ส่งไลน์หาสมชายว่าเดี๋ยวโทรกลับ *(ถามยืนยันก่อนส่ง)*
- เตือนกินยาตอนสองทุ่ม · ตั้งปลุกเจ็ดโมงเช้า · ตั้งเวลา 25 นาที
- พรุ่งนี้ฝนจะตกไหม · 100 ดอลลาร์เป็นกี่บาท · ข่าววันนี้
- เปิดพันทิป · ปิดแท็บนี้ · สรุปหน้าเว็บนี้ · แบตเหลือเท่าไหร่

### ความเป็นส่วนตัว

การฟัง การถอดเสียง โมเดลแชต และเสียงพูด ทำงานในเครื่องทั้งหมด จะใช้อินเทอร์เน็ตเฉพาะ
YouTube Music, พยากรณ์อากาศ ([Open-Meteo](https://open-meteo.com)), อัตราแลกเปลี่ยน
([open.er-api.com](https://open.er-api.com)), ข่าว (Google News RSS) และการค้นเว็บ

ฉบับนี้ตั้งเป็นภาษาไทยไว้เป็นค่าเริ่มต้น (`"language": "th"`, ฟังเสียงด้วย Whisper)
รายละเอียดการตั้งค่า สิทธิ์ที่ต้องเปิด และวิธีสลับกลับเป็นภาษาอังกฤษ อยู่ใน [THAI.md](THAI.md)

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
- **Lives in the menu bar.** Starts at login, always listening, no Dock icon until
  you open the full-size **Command Center** window (Spotlight, the Dock, or ⌘0).

## Requirements

- A Mac with **Apple Silicon** (M1 or newer)
- **macOS 14** Sonoma or newer
- **16 GB of memory** recommended (8 GB works, but tightly)
- About **10 GB** of free disk space for the models
- [Ollama](https://ollama.com/download) (free) — runs the language model
- Xcode Command Line Tools (`setup.sh` offers to install them)

## Install

```bash
git clone https://github.com/namo35423/jarvis-private.git jarvis
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
| Speech to text | Thai: [Whisper Large V3 Turbo](https://huggingface.co/mlx-community/whisper-large-v3-turbo) via MLX · English mode: [Parakeet TDT 0.6B v3](https://huggingface.co/mlx-community/parakeet-tdt-0.6b-v3) | Apple GPU |
| Thinking | [Qwen3 4B Instruct](https://ollama.com/library/qwen3) via Ollama, with tool calling | Apple GPU |
| Voice | Thai: macOS Kanya, or [Wayu-Paxa-TTS-Edge](https://huggingface.co/wayu-ai/wayu-paxa-tts-edge) · English mode: [Kokoro 82M](https://github.com/thewh1teagle/kokoro-onnx) (`bm_george`) | on device |
| Big jobs (optional) | Claude Code CLI | your Claude plan |

A small native Swift app sits in the menu bar. It starts a Python "brain" as its
child process, which is what lets macOS attach the Microphone, Accessibility and
Automation permissions to *Jarvis* rather than to Python. If the brain ever
crashes or hangs, the app restarts it.

```
app/      Swift menu bar app (build.sh, install.sh)
brain/    Python assistant: listener, speech, model, tools, memory, learning
ui/       the dropdown panel (WebGL orb) and the Command Center (Canvas 2D core)
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
- Whisper Large V3 Turbo — MIT
- Kokoro 82M — Apache 2.0
- Parakeet TDT 0.6B v3 — CC BY 4.0
- Wayu-Paxa-TTS-Edge (optional Thai voice, installed by hand) — **CC BY-NC 4.0
  (non-commercial use only)**; its inference code is Apache 2.0
- openWakeWord pre-trained models, including "hey jarvis" — **CC BY-NC-SA 4.0
  (non-commercial use only)**
- Silero VAD — MIT

Jarvis is an independent project. It is not affiliated with or endorsed by
Marvel, Apple, Anthropic, Alibaba, or any model author.
