# Thai local Jarvis

This branch adds multilingual MLX Whisper, native macOS Thai speech, Thai command shortcuts and YouTube Music controls. All speech and model inference run on this Mac. Web searches and music websites require the internet. Claude handoffs default to off. Memory remains in `~/.jarvis/jarvis.db`.

## Everyday use

Open `~/Applications/Jarvis.app`. Say **Hey Jarvis**, wait for the chime, then speak Thai or English. Command-backslash starts a voice turn; if that shortcut was already taken, the app uses Control-Option-backslash. It is a tap-to-talk shortcut, not hold-to-record. After an answer, the seven-second follow-up window lets you continue without the wake word.

Examples:

- เปิด Google Chrome แล้วเข้า YouTube Music ให้หน่อย
- ลดเสียงเหลือ 30 เปอร์เซ็นต์
- ตอนนี้กี่โมงแล้ว
- เปิด VS Code ให้หน่อย
- ตั้งเวลา 5 นาที
- ช่วยค้นหาข่าว NVIDIA ล่าสุด
- พรุ่งนี้ฉันมีอะไรต้องทำบ้าง

The original English wake model is unchanged. Thai wake phrases are not trained or promised; “จาร์วิส” is stripped from transcribed commands only.

The verified setup uses the MacBook's built-in microphone and speakers. Bluetooth headphone output was silent during the live test, so Jarvis was routed separately to the built-in speakers; other apps retain their existing routing. Volume commands control the macOS default output, which may still be your headphones. Set `listening.input_device` / `listening.output_device` to `null` and restart to follow system defaults again.

Wake sensitivity is calibrated to `0.25` on this Mac (a real successful wake scored `0.425`). Raise it in Settings if background audio triggers it. Microphone frames/levels and wake counts are exposed in local status without recording audio files.

## Settings

The panel's speech-language selector offers automatic Thai/English detection, Thai bias (`th`) and English (`en`). Thai bias can improve short Thai commands; auto is the default. Auto detection retries in Thai if Whisper picks a language outside `stt.allowed_languages` (default Thai and English). Set that list to `[]` for unrestricted multilingual detection. Speaking speed and Thai voice are adjustable in the panel. Restart after changing engine/model in `~/.jarvis/config.json`.

Relevant keys:

```json
{
  "language": "th",
  "stt": {
    "engine": "whisper",
    "model": "mlx-community/whisper-large-v3-turbo",
    "language": "auto",
    "initial_prompt": "",
    "min_rms": 0.002
  },
  "voice": {
    "engine": "macos",
    "thai_voice": "Kanya",
    "english_voice": "Samantha",
    "rate": 175,
    "speed": 1.0
  },
  "llm": {"model": "qwen3:4b-instruct", "num_ctx": 8192},
  "listening": {"end_silence_ms": 650, "followup_seconds": 7.0},
  "claude": {"mode": "off"},
  "safety": {"confirm_risky": true}
}
```

Kanya handles sentences containing Thai; all-English sentences use Samantha. Mixed words within one Thai sentence stay with Kanya, so foreign names may have a Thai pronunciation. The original Kokoro backend remains available.

## Permissions and music

Microphone: System Settings → Privacy & Security → Microphone → Jarvis. Only you can grant macOS permissions. The Python brain stays a child of the Swift app to retain that identity.

Opening Chrome/YouTube Music needs no browser scripting grant. Playback controls require System Settings → Privacy & Security → Automation → Jarvis → Google Chrome, and Chrome → View → Developer → Allow JavaScript from Apple Events. Open a music track in Chrome and sign in yourself when needed. Search commands open results and explicitly ask you to pick a track; they do not claim autoplay. Liked Music opens the account's `playlist?list=LM` page. Play/pause inspect the video state; next verifies a changed media source. Site changes can break selectors, in which case Jarvis reports failure. No extension is installed.

Calendar/Reminders need their app's Automation grant when first used. Reminder creation asks for confirmation. Respond `ยืนยันครับ` or `ยกเลิก`. Accessibility is only needed for generic typing/clicking tools; Screen Recording is only needed for screenshot features. Those grants are optional for the example interaction.

Kanya is installed on the tested Mac. If absent on another machine, open System Settings → Accessibility → Read & Speak (Spoken Content on older macOS) → System voice → Manage Voices; select Thai/Kanya and download it. Check installed voices with `say -v '?'`.

## Start, stop, restart

From the repository:

```sh
open ~/Applications/Jarvis.app
bin/jarvis status
bin/jarvis restart
bin/jarvis mute
bin/jarvis unmute
bin/jarvis logs
```

To quit fully, use Jarvis's menu-bar Quit command. This stops its child brain and releases the mic. `bin/jarvis stop` only interrupts speech, it does not quit. Ollama runs as a local Homebrew service: `brew services stop ollama` stops it; `brew services start ollama` starts it. Enable Start at Login from Jarvis's own menu if desired.

## English rollback without deleting data

```sh
python3 scripts/language_mode.py english
bin/jarvis restart
```

This backs up config, restores Parakeet + Kokoro/bm_george and English responses, and keeps memory, timers and the safer confirmation rules. Download the original speech models using `scripts/fetch_models.sh --english-fallback` first if they are absent. Return with `python3 scripts/language_mode.py thai` and `bin/jarvis restart`.

The unmodified upstream code remains on Git branch `main` at `b8a2635e09e90d3ba626e44cd7ca5014f3b048dd`. Full source rollback is possible by switching to that branch, but its Swift build needs the one-line weak-self capture compatibility fix on this Mac's compiler. The language-mode rollback avoids that build issue.

## Uninstall without losing personal data

Quit Jarvis and turn off its Start at Login. Move `~/Applications/Jarvis.app` to Trash. Remove the `~/.local/bin/jarvis` symlink if desired. Keep `~/.jarvis` to preserve conversation memory/configuration. The repository, Python virtual environment, Hugging Face model cache and Ollama models can be removed separately only when no longer wanted. To remove Ollama, first stop its service and then use `brew uninstall ollama`; do not delete shared caches or `~/.ollama` if other software uses them.

## Verification and limits

Run `brain/verify.sh`, `app/build.sh`, and `cd brain && .venv/bin/python -m pytest -q tests`. Opt into controlled local audio tests with `JARVIS_TEST_SPEECH=1 HF_HUB_OFFLINE=1 .venv/bin/python -m pytest -q tests/test_speech_integration.py`. `brain/requirements-thai-lock.txt` records the installed package versions. Tests mock external/destructive actions. Synthetic speech tests verify local audio-to-text but are not evidence of accuracy for your voice or room. Silence, low-level noise, confidence and repetition filters reduce hallucinations; they cannot distinguish all background music with vocals from intended speech. Close the mic with Mute when needed.

The first model load and first transcription are slower than subsequent turns. The MLX allocation cache is capped at 256 MiB (model weights remain resident). Qwen warm-up reuses the same context size and is not repeated for an indefinitely resident model. Only one STT engine is resident; English fallback is an explicit mode switch. Native speech consumes no large neural TTS model. Ollama uses one 4B model with an 8192-token context. macOS speech synthesis stays on the speaker worker; MLX uses one dedicated worker. Tool-backed model responses wait until the model round is parsed before speech so preliminary tool narration is not spoken as a success.

## Source references

- [Upstream Jarvis](https://github.com/BatmanOnTop/jarvis)
- [MLX Whisper Turbo model](https://huggingface.co/mlx-community/whisper-large-v3-turbo)
- [MLX Whisper Small model](https://huggingface.co/mlx-community/whisper-small-mlx)
- [Ollama offline/cloud configuration](https://docs.ollama.com/faq)
