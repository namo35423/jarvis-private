"""The assistant itself: state machine, turn loop, and the voice pipeline glue.

Shape of a spoken turn:
    wake word / hotkey -> capture -> transcribe -> model (streaming, with tools)
    -> speak sentence by sentence -> follow-up window -> idle

Two things are load-bearing for how responsive it feels:
  * The model is preloaded the moment the wake word fires, so weights are
    resident by the time transcription finishes.
  * Speech starts on the first complete sentence, while the model is still
    generating the rest.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime
from typing import Any

from jarvis import tools as tools_mod
from jarvis import thai, thai_time
from jarvis.audio import AudioInput, AudioOutput, MicUnavailable
from jarvis.config import Config, ensure_dirs
from jarvis.jobs import Job, JobManager
from jarvis.learning import Learning
from jarvis.listener import Listener
from jarvis.llm import OllamaClient, strip_tool_call_text
from jarvis.memory import Memory, ScheduledItem
from jarvis.prompts import build_system_prompt, classify_confirmation, with_context
from jarvis.scheduler import Scheduler
from jarvis.sounds import Earcons
from jarvis.speech_text import SentenceStream, for_speech
from jarvis.stt import Transcriber
from jarvis.sysutil import osascript
from jarvis.tools import ToolContext, execute, load_all, schemas

log = logging.getLogger("jarvis.core")

IDLE = "idle"
LISTENING = "listening"
THINKING = "thinking"
SPEAKING = "speaking"
WORKING = "working"
CONFIRM = "confirm"
MUTED = "muted"
ERROR = "error"

LEVEL_HZ = 30.0

# Requests that are always the same tool call. Matching them here skips the
# model entirely - about four seconds saved on the things said most often.
FAST_TOOLS: list[tuple[re.Pattern, str, Any]] = [
    (re.compile(r"^(turn (it |the (volume|sound) )?up|volume up|louder|turn up the volume)$"),
     "system_control", lambda m: {"action": "volume_up"}),
    (re.compile(r"^(turn (it |the (volume|sound) )?down|volume down|quieter|turn down the volume)$"),
     "system_control", lambda m: {"action": "volume_down"}),
    (re.compile(r"^(mute|mute the (volume|sound|audio)|silence)$"),
     "system_control", lambda m: {"action": "mute"}),
    (re.compile(r"^(unmute|unmute the (volume|sound|audio))$"),
     "system_control", lambda m: {"action": "unmute"}),
    (re.compile(r"^(set )?(the )?volume (to |at )?(\d{1,3})( percent)?$"),
     "system_control", lambda m: {"action": "volume_set", "value": float(m.group(4))}),
    (re.compile(r"^(how much |whats the |what's the )?battery( level| percentage| left)?$"),
     "system_control", lambda m: {"action": "battery"}),
    (re.compile(r"^(lock (the |my )?(screen|mac|computer)|lock it)$"),
     "system_control", lambda m: {"action": "lock_screen"}),
    (re.compile(r"^(dark mode|turn on dark mode|go dark)$"),
     "system_control", lambda m: {"action": "dark_mode"}),
    (re.compile(r"^(light mode|turn on light mode)$"),
     "system_control", lambda m: {"action": "light_mode"}),
    (re.compile(r"^(pause|pause (the )?(music|song|it))$"),
     "media_control", lambda m: {"action": "pause"}),
    (re.compile(r"^(play|resume|unpause|play (the )?music|keep playing)$"),
     "media_control", lambda m: {"action": "play"}),
    (re.compile(r"^(next|skip|next (song|track)|skip (this|it|the song))$"),
     "media_control", lambda m: {"action": "next"}),
    (re.compile(r"^(previous|go back a (song|track)|previous (song|track)|last song)$"),
     "media_control", lambda m: {"action": "previous"}),
    (re.compile(r"^(what(s| is) (this |playing)?|whats this song|what song is this|whats playing|what is playing)$"),
     "media_control", lambda m: {"action": "now_playing"}),
    (re.compile(r"^(?!.*\b(open|play|remind|reminder|set|send|text|email|search|calendar|timer|make|create|write|"
                r"build|draft|change|update|fix|report|spreadsheet|document|yourself|your)\b)"
                r".*\b(weather|rain|raining|rainy|umbrella|forecast|temperature|snow|snowing|storm|sunny|"
                r"jacket|hoodie|coat|sweater|degrees|hot outside|cold outside|warm outside)\b.*$"),
     "get_weather", lambda m: _weather_args(m.group(0))),
    # Making things and changing itself are routed, not guessed: both small models
    # would otherwise just answer "make me a presentation" in words.
    (re.compile(r"^(undo|revert|roll back) (your (last )?(change|update|edit)|(the |that )?(last )?(change|update|edit) "
                r"(to|you made to) yourself|what you (just )?changed about yourself)$"),
     "improve_self", lambda m: {"undo": True}),
    (re.compile(r"^(?!.*\b(open|play|louder|quieter|softer|answer|mistake|memory)\b)"
                r"(change|modify|update|upgrade|improve|fix|edit|reprogram|redesign|make|give) "
                r"(yourself|your own \w+|your (code|voice|personality|wake word|features?|settings|look|design|ui|"
                r"interface|appearance|speaking style|panel|orb|behaviou?r))\b.*$"),
     "improve_self", lambda m: {"request": m.group(0)}),
    (re.compile(r"^(make|create|build|write|draft|generate|design|put together) (me )?(a |an |some )?"
                r"(new |quick |short |simple |full |detailed )?(\w+ )?(powerpoint|presentation|slide ?deck|slides|"
                r"spreadsheet|website|web app|resume|cover letter|essay|report|document|script|program)\b"
                r"(?!.*\b(reminder|event|note|calendar|folder|text|email)\b).*$"),
     "ask_claude", lambda m: {"task": m.group(0), "complexity": "standard"}),
    # Sites before apps, so "open youtube.com" goes to the browser.
    (re.compile(r"^(?!.*\b(and|then)\b)(open|go to|pull up|load) (?P<site>[\w-]+(\.[\w-]+)+(/\S*)?)$"),
     "open_url", lambda m: {"url": m.group("site")}),
    (re.compile(r"^(?!.*\b(and|then)\b)(search for|search|google) (?P<q>.+)$"),
     "open_url", lambda m: {"url": m.group("q")}),
    (re.compile(r"^(?!.*\b(and|then)\b)(open|launch|switch to|bring up) (the |my )?(?!(a|an|new|some|this|that|it)\b)(?P<app>.+?)( app)?$"),
     "open_app", lambda m: {"name": m.group("app")}),
    (re.compile(r"^(?!.*\b(and|then)\b)(quit|close|kill|exit) (the |my )?(?!(a|an|this|that|it|all|everything|tab|window|the window)\b)(?!.*\b(tab|tabs|window|windows)\b)(?P<app>.+?)( app)?$"),
     "quit_app", lambda m: {"name": m.group("app")}),
]


_WAKE = re.compile(r"^(?:(?:hey|hi|ok|okay|yo)[ ,]+)?(?:jarvis|jervis|jarvi|travis|harvest|j\w{3,6}s|drive a s|there)\b[,.!]*\s*")
_POLITE = re.compile(r"^(?:can you|could you|would you|will you|please|go ahead and|i want you to|i need you to|just)\s+")
_TRAILING = re.compile(r"\s+(?:for me|please|now|right now|real quick|thanks|thank you)$")
_ACTION = re.compile(r"^(close|quit|kill|exit|open|launch|play|pause|skip|next|mute|unmute|turn|set|lock|search|go to)\b")


# A reply that says something was done. Without a tool call in the turn it is false.
_CLAIMS_ACTION = re.compile(
    r"กำลังเล่น|(?:เล่น|เปิด|ปิด|ส่ง|ตั้ง|ปรับ|สร้าง|บันทึก|จด|ลง|เพิ่ม|ลบ|ล็อก|ล็อค|ยกเลิก|ค้นหา|แคป)"
    r"(?:ให้)?.{0,60}?(?:แล้ว|เรียบร้อย|สำเร็จ)|"
    r"\b(?:now playing|is (?:now )?playing|started playing|message (?:was )?sent|sent (?:it|the message|your message)|"
    r"(?:i'?ve|i have) (?:opened|closed|set|created|saved|added|sent|turned)|(?:is|are) now (?:open|closed|on|off))\b",
    re.I)


def _ungrounded_numbers(reply: str, source: str) -> list[str]:
    """Figures of three or more digits in `reply` that appear nowhere in `source` (years excepted)."""
    have = {n.replace(",", "").lstrip("0") for n in re.findall(r"\d[\d,]*(?:\.\d+)?", source)}
    missing = []
    for raw in re.findall(r"\d[\d,]*(?:\.\d+)?", reply):
        n = raw.replace(",", "").rstrip(".")
        digits = n.split(".")[0]
        if len(digits) < 3 or (len(digits) == 4 and (1900 <= int(digits) <= 2100 or 2400 <= int(digits) <= 2700)):
            continue
        if n.lstrip("0") not in have:
            missing.append(raw)
    return missing


# A clarifying question ("ให้ตั้งเวลากี่นาทีครับ?") is not a claim.
_ASKS = re.compile(r"(?:ไหม|มั้ย|หรือ(?:ยัง|เปล่า|ไม่)|อะไร|ยังไง|เท่าไหร่|กี่\S*|\?)\s*(?:ครับ|คะ|ค่ะ)?\s*[?.!]?\s*$")


async def _no_chunks():
    return
    yield  # an empty async generator for forced rounds

# "play shape of you by ed sheeran" -> YouTube Music, when that is the preferred player.
_PLAY_SONG = re.compile(r"^play (?:the song |song )?(?P<title>(?!.*\b(?:and|then)\b).+?)(?: by (?P<artist>.+))?(?: on youtube music)?$")
_NOT_A_TITLE = re.compile(r"^(?:(?:the|my|some|a) )?(?:music|it|that|this|something|song|playlist|liked songs|"
                          r"(?:next|previous|last) (?:song|track)|next|previous|youtube music)$")


def normalise_command(text: str) -> str:
    """Reduce what speech recognition hands over to the bare command.

    Spoken input arrives as "Hey Jarvis, can you open up Spotify for me?" -
    and often with the wake word misheard ("Hey Jervis", "Harvest"). Every
    instant path is anchored on the verb, so all of that has to go first.
    """
    t = (text or "").lower().replace("\u2019", "'").replace("'", "")
    t = re.sub(r"[,!?]", " ", t)
    t = re.sub(r"\.(\s|$)", r" . ", t)           # sentence breaks, but keep youtube.com
    sentences = [x.strip() for x in t.split(" . ") if x.strip()]
    if len(sentences) > 1 and _ACTION.match(_WAKE.sub("", sentences[-1])) and re.search(r"\b(it|that|this)\b", sentences[-1]):
        t = sentences[-1]                          # "I'm on Spotify. Close it." -> "close it"
    elif len(sentences) > 1 and sum(bool(_ACTION.match(_WAKE.sub("", x))) for x in sentences) > 1:
        t = " and then ".join(sentences)  # several commands: no instant path matches "and then"
    else:
        t = " ".join(sentences)
    t = " ".join(t.split())
    for _ in range(3):
        t = _POLITE.sub("", _WAKE.sub("", t)).strip()
    t = _TRAILING.sub("", t).strip(" .")
    t = re.sub(r"^(open|pull|bring) up\b", r"\1", t)
    return thai.normalise(t) if thai.THAI.search(t) else t


PERSONAL = re.compile(r"\b(i|im|i'm|my|mine|me|we|our)\b|ฉัน|ผม|ของเรา", re.I)
_COMMAND = re.compile(r"^(open|close|quit|play|pause|set|remind|text|send|search|find|show|tell|make|create|turn|"
                      r"call|email|add|delete|move|organize|go|look|check|read|take|put|start|stop)\b", re.I)

QUESTION = re.compile(r"^(what|whats|what's|who|whos|who's|when|where|why|how|is|are|am|do|does|did|can|could|would|will|should|tell me|remind me what)\b", re.I)

_PLACE = re.compile(r"\b(?:in|for|at) (?!the\b|my\b|a\b|this\b)([a-z][a-z .-]{2,30}?)(?: (?:today|tomorrow|later|tonight|this week|right now|now))?$")


def _weather_args(text: str) -> dict:
    args: dict[str, str] = {}
    if "tomorrow" in text:
        args["when"] = "tomorrow"
    elif "this week" in text or "week" in text:
        args["when"] = "week"
    m = _PLACE.search(text)
    if m:
        args["location"] = m.group(1).strip()
    return args


class Assistant:
    def __init__(self, cfg: Config | None = None):
        ensure_dirs()
        self.cfg = cfg or Config()
        self.memory = Memory()
        self.llm = OllamaClient(self.cfg)
        self.learning = Learning(self.cfg, self.memory, self.llm)
        self.learning.side_chat = self._side_chat
        self.scheduler = Scheduler(self.memory, self._on_scheduled)
        self.jobs = JobManager(self)
        self.stt = Transcriber(self.cfg)

        self.audio_in = AudioInput(self.cfg.get("listening.input_device"))
        self.audio_out = AudioOutput(
            sample_rate=self.cfg.get("voice.sample_rate", 24000),
            device=self.cfg.get("listening.output_device"),
            volume=self.cfg.get("voice.volume", 0.9),
        )
        from jarvis.tts import Speaker  # imported late: pulls in onnxruntime

        self.speaker = Speaker(self.cfg, self.audio_out)
        self.earcons = Earcons(self.audio_out, enabled=self.cfg.get("listening.chimes", True))
        self.listener = Listener(self.cfg, self.audio_in, self._listener_event, lambda: self.speaker.busy)

        self.state = IDLE
        self.state_detail: str | None = None
        self.clients: set[Any] = set()
        self.loop: asyncio.AbstractEventLoop | None = None

        self.messages: list[dict] = []
        self._system_prompt = ""
        self._last_turn_at = 0.0
        self._turn_task: asyncio.Task | None = None
        self._pending_confirm: dict | None = None
        self._pending_announcements: list[tuple[str, str]] = []
        self._mic_status = "unknown"
        self._level_sent: dict[str, float] = {}
        self._level_silent: dict[str, bool] = {}
        self._followup_task: asyncio.Task | None = None
        self._failed_calls: set[str] = set()
        self._blocked_tools: set[str] = set()
        self._last_tool_result = ""
        self._last_app = ""
        self._last_episode_at = 0.0
        self._current_text = ""
        self._ready = False
        self._ollama_ready = False
        # The server answers before the tools load; commands wait on this instead of
        # failing with "no tool named ..." (set once tools, speech and timers are up).
        self._started = asyncio.Event()

    # ------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        log.info("starting up")

        ok = await self.llm.ensure_up()
        self._ollama_ready = ok and self.llm.model in await self.llm.list_models()
        if not ok:
            log.warning("Ollama is not reachable - replies will fail until it starts")

        # Heavy model loads in parallel; each is slow on its own (1-4 s).
        await asyncio.gather(
            self._load_stt(), self._load_tts(), self._load_listener(), return_exceptions=True
        )

        load_all()
        log.info("%d tools registered", len(tools_mod.REGISTRY))
        self.rebuild_system_prompt()
        self._restore_conversation()

        self.speaker.on_speech_start = lambda: self._from_thread(self._on_speech_start)
        self.speaker.on_speech_end = lambda: self._from_thread(self._on_speech_end)
        self.audio_out.on_level = self._on_output_level

        await self._start_audio()

        await self.scheduler.start()
        self._started.set()
        # Ask for app permissions only when an explicitly requested tool needs them.
        # Finish warming before declaring ready. Nobody is talking to Jarvis in
        # the first few seconds after launch, and doing this lazily instead
        # means the first thing they DO say waits out the whole model load.
        with contextlib.suppress(asyncio.TimeoutError, Exception):
            await asyncio.wait_for(self._warm_model(), timeout=60)
        self._ready = True
        self.set_state(IDLE)
        asyncio.create_task(self._announce_self_edit_result())
        asyncio.create_task(self._reflect_loop())
        log.info("ready")

    async def _start_audio(self) -> None:
        """Open the speaker and microphone without ever blocking the event loop.

        PortAudio's open call sits inside CoreAudio and can block for a long
        time (a pending permission prompt, a device being reconfigured), so it
        runs in a worker with a timeout. Jarvis stays usable by text either way.
        """
        import os

        loop = asyncio.get_running_loop()
        if os.environ.get("JARVIS_NO_AUDIO"):
            self._mic_status = "disabled"
            log.warning("JARVIS_NO_AUDIO set - running without audio")
            return

        try:
            await asyncio.wait_for(loop.run_in_executor(None, self.audio_out.start), timeout=15)
        except Exception as exc:
            log.error("speaker unavailable: %s", exc)

        from jarvis.audio import mic_authorization

        auth = mic_authorization()
        if auth == "denied":
            self._mic_status = "denied"
            log.error("microphone access denied - enable it in System Settings > Privacy & Security > Microphone")
            return

        if auth != "authorized":
            # The prompt is still on screen (or has never been shown). Rather than
            # forcing a restart once the user says yes, watch for the grant.
            self._mic_status = "waiting"
            log.info("waiting for microphone permission")
            asyncio.create_task(self._await_microphone())
            return

        await self._open_microphone()

    async def _open_microphone(self) -> bool:
        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(loop.run_in_executor(None, self.audio_in.start), timeout=15)
        except asyncio.TimeoutError:
            self._mic_status = "blocked"
            log.error("microphone did not open in time")
            return False
        except Exception as exc:
            self._mic_status = "unavailable"
            log.error("microphone unavailable: %s", exc)
            return False

        self._mic_status = "ok"
        if not self.listener.is_alive():
            self.listener.start()
        log.info("microphone live - wake word listening")
        self.broadcast(self.status())
        return True

    async def _await_microphone(self, timeout: float = 900.0) -> None:
        """Poll for the permission grant so voice starts the moment it lands."""
        from jarvis.audio import mic_authorization

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(2.0)
            auth = mic_authorization()
            if auth == "authorized":
                await self._open_microphone()
                return
            if auth == "denied":
                self._mic_status = "denied"
                log.error("microphone access denied")
                self.broadcast(self.status())
                return

    async def _load_stt(self) -> None:
        try:
            await self.stt.load()
        except Exception:
            log.exception("speech recognition failed to load")

    async def _load_tts(self) -> None:
        try:
            await asyncio.get_running_loop().run_in_executor(None, self.speaker.load_sync)
        except Exception:
            log.exception("speech synthesis failed to load")

    async def _load_listener(self) -> None:
        try:
            await asyncio.get_running_loop().run_in_executor(None, self.listener.load_models)
        except Exception:
            log.exception("wake word failed to load")

    async def _preflight_permissions(self) -> None:
        try:
            from jarvis.permissions import preflight

            await preflight()
        except Exception as exc:
            log.debug("permission preflight skipped: %s", exc)

    async def _warm_model(self) -> None:
        """Load the weights AND prime the prompt cache.

        Loading alone leaves the first real request paying ~1.8s to prefill the
        system prompt and tool schemas. Sending that exact prefix once, with a
        single token generated, means the first thing the user says hits a warm
        cache and starts generating immediately.
        """
        await self.llm.warm()
        if not self._system_prompt:
            return
        try:
            await self.llm.chat(
                self._conversation() + [{"role": "user", "content": "ready?"}],
                tools=schemas(),
                options={"num_predict": 1},   # prefill the cache; the reply itself is waste
            )
        except Exception as exc:
            log.debug("prompt priming skipped: %s", exc)

    async def stop(self) -> None:
        self._ready = False
        log.info("shutting down")
        # Stop the work before tearing down what it depends on, otherwise an
        # in-flight turn writes to an already-closed database on the way out.
        for task in (self._turn_task, self._followup_task):
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self.stt.close()
        self.listener.stop()
        self.speaker.stop()
        self.speaker.close()
        self.audio_in.stop()
        self.audio_out.close()
        await self.scheduler.stop()
        with contextlib.suppress(Exception):
            await self.jobs.cancel_all()
        await self.llm.close()
        self.memory.close()

    # ----------------------------------------------------------------- state
    def set_state(self, state: str, detail: str | None = None) -> None:
        if state == self.state and detail == self.state_detail:
            return
        self.state = state
        self.state_detail = detail
        self.broadcast({"type": "state", "state": state, "detail": detail})

    def broadcast(self, event: dict) -> None:
        """Fan an event out to every connected UI. Safe to call from any task."""
        dead = []
        for client in list(self.clients):
            try:
                client.send_event(event)
            except Exception:
                dead.append(client)
        for d in dead:
            self.clients.discard(d)

    def status(self) -> dict:
        return {
            "type": "status",
            "ready": self._ready,
            "pid": os.getpid(),
            "ollama": self._ollama_ready,
            "stt_model": self.stt.model_id,
            "stt_language": self.cfg.get("stt.language"),
            "stt_last_ms": self.stt.last_ms,
            "tts_last_ms": self.speaker.last_ms,
            "tts_engine": self.speaker.engine,
            "model": self.cfg.get("llm.model"),
            "stt": "ready" if self.stt.ready else "off",
            "tts": "ready" if self.speaker.ready else "off",
            "wake": self.listener.wake_ready,
            "wake_peak_30s": round(self.listener.wake_peak, 4),
            "wake_count": self.listener.wake_count,
            "mic_frames": self.audio_in.frames_received,
            "mic_level": round(self.audio_in.last_level, 4),
            "input_device": self.audio_in.device,
            "output_device": self.audio_out.device,
            "mic": self._mic_status,
            "listening_enabled": self.listener.enabled,
            "state": self.state,
            "jobs": [j.as_event() for j in self.jobs.active()],
            "learning": self.learning.stats(),
        }

    def rebuild_system_prompt(self) -> None:
        """Rebuilt between conversations only - a changed prompt costs a re-prefill."""
        base = build_system_prompt(self.cfg, self.memory)
        lessons = self.learning.lessons(limit=int(self.cfg.get("learning.prompt_lessons", 12)))
        if lessons:
            base += "\n\nLEARNED FROM EXPERIENCE (corrections you have already been given - do not repeat these mistakes)\n"
            base += "\n".join(f"- {l}" for l in lessons)
        self._system_prompt = base
        self._prompt_built_at = time.time()

    # ------------------------------------------------- listener thread events
    def _from_thread(self, fn, *args) -> None:
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(fn, *args)

    def _listener_event(self, name: str, payload: dict) -> None:
        self._from_thread(self._handle_listener_event, name, payload)

    def _handle_listener_event(self, name: str, payload: dict) -> None:
        if name == "wake":
            # "Hey Jarvis" always wins: stop talking, drop whatever was still being
            # generated, and cancel a pending follow-up that would restart the recording.
            self._stop_output()
            self.earcons.play("wake")
            self.set_state(LISTENING)
            asyncio.create_task(self.llm.warm())  # weights only - never generate here
        elif name == "capture_start":
            if self.state not in (CONFIRM,):
                self.set_state(LISTENING, "follow-up" if payload.get("followup") else None)
        elif name == "level":
            self._maybe_send_level("mic", payload.get("value", 0.0))
        elif name == "utterance":
            asyncio.create_task(self._on_utterance(payload))
        elif name == "timeout":
            if self._pending_confirm:
                self._resolve_confirmation(False, spoken=True)
            elif self.state in (LISTENING,):
                if not payload.get("followup"):
                    self.earcons.play("sleep")
                self.set_state(IDLE)

    def _maybe_send_level(self, source: str, value: float) -> None:
        # Throttled per source: the speaker reports its level continuously, and with one
        # shared limit its silence crowded out the microphone while listening.
        now = time.monotonic()
        if now - self._level_sent.get(source, 0.0) < 1.0 / LEVEL_HZ:
            return
        silent = float(value) <= 0.001
        if silent and self._level_silent.get(source):
            return  # silence is reported once, not 30 times a second
        self._level_silent[source] = silent
        self._level_sent[source] = now
        self.broadcast({"type": "level", "source": source, "value": round(float(value), 3)})

    def _on_output_level(self, value: float) -> None:
        # Called from the audio thread; throttled and marshalled onto the loop.
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self._maybe_send_level, "tts", value)

    def _on_speech_start(self) -> None:
        if self.state != CONFIRM:
            self.set_state(SPEAKING)

    def _on_speech_end(self) -> None:
        if self.state == SPEAKING:
            self.set_state(IDLE)

    # ------------------------------------------------------------ transcribe
    async def _on_utterance(self, payload: dict) -> None:
        audio = payload.get("audio")
        if audio is None:
            return
        if self.state != CONFIRM:
            self.set_state(THINKING, "transcribing")
        t0 = time.time()
        try:
            text = await self.stt.transcribe(audio)
        except Exception:
            log.exception("transcription failed")
            await self.say("I didn't catch that.")
            self.set_state(IDLE)
            return
        log.info("heard %r in %.0fms (%.1fs audio)", text, (time.time() - t0) * 1000, payload.get("duration", 0))
        if not text.strip():
            if payload.get("followup"):
                self.set_state(IDLE)
            else:
                self.earcons.play("sleep")
                self.set_state(IDLE)
            return
        await self.handle_input(text, source="voice")

    # ----------------------------------------------------------------- input
    async def handle_input(self, text: str, source: str = "voice", speak: bool = True) -> str:
        """Entry point for anything the user says or types."""
        text = (text or "").strip()
        if not text:
            return ""
        if not self._started.is_set():
            log.info("command arrived during startup; waiting for tools: %r", text[:60])
            try:
                await asyncio.wait_for(self._started.wait(), timeout=90)
            except asyncio.TimeoutError:
                return ("ยังเริ่มระบบไม่เสร็จครับ ลองใหม่อีกสักครู่นะครับ" if self.cfg.get("language") == "th"
                        else "Still starting up. Try again in a moment.")

        if self._pending_confirm is not None:
            verdict = classify_confirmation(text)
            if verdict is not None:
                self.broadcast({"type": "transcript", "role": "user", "text": text, "turn": "confirm"})
                self._resolve_confirmation(verdict, spoken=True)
                return ""
            # Not a yes/no: treat it as a new instruction and drop the pending action.
            self._resolve_confirmation(False, spoken=False)

        turn_id = uuid.uuid4().hex[:8]
        self._current_turn = turn_id
        self._current_text = text
        self.broadcast({"type": "transcript", "role": "user", "text": text, "turn": turn_id})
        self.memory.log_turn("user", text)

        self._forced_plan = None
        handled = await self._fast_path(text, speak)
        if handled is not None:
            return handled
        forced, self._forced_plan = self._forced_plan, None

        if self._turn_task and not self._turn_task.done():
            self._turn_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._turn_task

        self._turn_task = asyncio.create_task(self._run_turn(text, turn_id, speak, forced))
        try:
            return await self._turn_task
        except asyncio.CancelledError:
            return ""

    async def _fast_path(self, text: str, speak: bool) -> str | None:
        """Handle the handful of commands that should never wait for a model."""
        t = normalise_command(text)
        line = thai.parse_line(text)  # before lowercasing: the message goes out as spoken
        if line:
            args = {"recipient": line[0], "text": line[1]}
            result = await self._exec("line_send", args)
            self._record_fast("line_send", args, result)
            return await self._quick_reply(thai.localise_result("line_send", args, result), speak)
        song = _PLAY_SONG.match(t) if self.cfg.get("media.preferred") == "youtube_music" else None
        if song and not _NOT_A_TITLE.match(song["title"]):
            args = {"action": "play", "query": song["title"], **({"artist": song["artist"]} if song["artist"] else {})}
            result = await self._exec("youtube_music", args)
            self._record_fast("youtube_music", args, result)
            return await self._quick_reply(thai.localise_result("youtube_music", args, result), speak)
        if thai.THAI.search(t):
            if t in {"หยุดพูด", "เงียบ", "ยกเลิก"}:
                self.interrupt("user asked")
                return ""
            reply = thai.clock_reply(t) or thai.instant_reply(t)
            if reply:
                return await self._quick_reply(reply, speak)
            plan = thai.parse(t, self._last_app, self.cfg.get("media.preferred"))
            if plan and thai.needs_wording(plan):
                # Data questions (weather, calendar, files): run the tool for certain, then let
                # the model word the real result in Thai instead of answering from memory.
                self._forced_plan = plan
                return None
            if plan:
                results = []
                for name, args in plan:
                    result = await self._exec(name, args)
                    self._record_fast(name, args, result)
                    results.append(thai.localise_result(name, args, result))
                    if result.startswith(("Error:", "Cancelled by the user")):
                        break
                    if name in {"open_app", "quit_app"}:
                        self._last_app = args["name"]
                return await self._quick_reply(" ".join(results), speak)
            return None  # Thai compound/ambiguous requests must not hit English shortcuts

        if t in {"stop", "cancel", "never mind", "nevermind", "shut up", "be quiet", "quiet", "stop talking", "forget it"}:
            self.interrupt("user asked")
            return ""

        if t in {"go to sleep", "stop listening", "mute", "mute yourself", "sleep"}:
            self.set_listening(False)
            await self.say("Going quiet. Say nothing more.", force=True)
            return ""

        if t in {"what time is it", "whats the time", "what's the time", "the time", "time"}:
            now = datetime.now().strftime("%-I:%M %p").lower()
            return await self._quick_reply(f"It's {now}.", speak)

        if t in {"what's the date", "whats the date", "what is the date", "what day is it", "what's today"}:
            return await self._quick_reply(datetime.now().strftime("It's %A the %-d of %B."), speak)

        if t in {"new conversation", "start over", "forget this conversation", "clear the conversation"}:
            self.new_conversation()
            return await self._quick_reply("Cleared.", speak)

        # "close it", "open it again": the app from the last command - or, failing
        # that, a running app named in the same breath ("I'm on Spotify. Close it.").
        # Never the frontmost app by guesswork: that could quit the wrong thing.
        if not self._last_app and re.search(r"\b(it|that|this)\b", t):
            from jarvis.tools.system import running_apps

            spoken = text.lower()
            named = [n for n, _ in running_apps()
                     if n != "Jarvis" and re.search(rf"\b{re.escape(n.lower())}\b", spoken)]
            if len(named) == 1:
                self._last_app = named[0]
        if self._last_app:
            if re.match(r"^(close|quit|kill|exit|shut)( down)? (it|that|this|this app|that app)( now| again)?$", t):
                return await self._fast_tool("quit_app", {"name": self._last_app}, text, speak)
            if re.match(r"^(open|launch|reopen|bring back)( up)? (it|that|that app)( again| back up| back)?$", t):
                return await self._fast_tool("open_app", {"name": self._last_app}, text, speak)

        # Common commands run their tool directly. The model adds nothing here
        # and costs a couple of seconds, which is the whole complaint.
        for pattern, name, build in FAST_TOOLS:
            m = pattern.match(t)
            if not m:
                continue
            try:
                result = await self._exec(name, build(m))
            except Exception:
                log.exception("fast path %s failed", name)
                break
            self._record_fast(name, build(m), result)
            if name in ("open_app", "quit_app"):
                self._last_app = build(m).get("name") or self._last_app
            if result.startswith("Error:"):
                # A missing permission is final - the model cannot route around
                # it, and letting it try costs half a minute of dead air.
                if "permission" in result.lower() or "System Settings" in result:
                    return await self._quick_reply(result.removeprefix("Error: "), speak)
                self._last_episode_at = 0.0  # the model's retry is not the user rephrasing
                break  # otherwise let the model try something else
            self.memory.log_turn("user", text)
            return await self._quick_reply(result, speak)

        return None

    async def _exec(self, name: str, args: dict, turn: str | None = None) -> str:
        """Run one tool outside a model turn (fast path, Command Center buttons), visibly:
        the UI gets the same tool events as a model call and shows EXECUTING meanwhile."""
        turn = turn or getattr(self, "_current_turn", "") or "fast"
        call_id = uuid.uuid4().hex[:6]
        prev = self.state
        from_ui = turn == "ui"
        self.broadcast({"type": "tool", "turn": turn, "id": call_id, "name": name,
                        "args": args, "status": "running", "result": None})
        if not from_ui or prev in (IDLE, MUTED):
            self.set_state(WORKING, name)
        log.info("tool %s(%s) [%s]", name, args, "button" if from_ui else "direct")
        started = time.monotonic()
        try:
            result = await execute(ToolContext(self), name, args)
        finally:
            if self.state in (WORKING, THINKING) and (not from_ui or prev in (IDLE, MUTED)):
                self.set_state(prev if from_ui else IDLE)
        failed = result.startswith("Error:")
        log.info("tool %s returned %d chars in %.1fs%s", name, len(result), time.monotonic() - started,
                 " (error)" if failed else "")
        self.broadcast({"type": "tool", "turn": turn, "id": call_id, "name": name, "args": args,
                        "status": "error" if failed else "ok", "result": result[:400]})
        return result

    async def run_ui_tool(self, name: str, args: dict) -> str:
        """A Command Center button: the real tool, with the usual confirmations."""
        result = await self._exec(name, args, turn="ui")
        if self.cfg.get("language") == "th":
            return thai.localise_result(name, args, result)
        if result.startswith("Cancelled by the user"):
            return "Cancelled."  # the model-facing "do not retry" note means nothing on a button
        return result

    async def _fast_tool(self, name: str, args: dict, text: str, speak: bool) -> str | None:
        result = await self._exec(name, args)
        self._record_fast(name, args, result)
        if result.startswith("Error:"):
            self._last_episode_at = 0.0
            return None
        return await self._quick_reply(result, speak)

    def _record_fast(self, name: str, args: dict, result: str) -> None:
        """Instant commands count as 'the last thing I did', so 'that's wrong' right
        after one corrects the right turn."""
        ep = self.learning.start_episode(self._current_text or name)
        ep.plan = [{"name": name, "args": args}]
        ep.results = [str(result)[:300]]
        ep.reply = str(result)[:300]
        ep.errors = int(str(result).startswith("Error:"))
        with contextlib.suppress(Exception):
            self.learning.finish_episode(ep)
        self._last_episode_at = time.time()

    async def _quick_reply(self, text: str, speak: bool) -> str:
        turn = getattr(self, "_current_turn", "") or uuid.uuid4().hex[:8]
        user_text = getattr(self, "_current_text", "")
        if user_text and text != "Cleared.":
            self.messages += [{"role": "user", "content": user_text}, {"role": "assistant", "content": text}]
            self._last_turn_at = time.time()
        self.broadcast({"type": "reply_delta", "turn": turn, "text": text})
        self.broadcast({"type": "reply_done", "turn": turn, "text": text})
        self.memory.log_turn("assistant", text)
        if speak:
            await self.say(text)
            self._schedule_followup()
        return text

    # ------------------------------------------------------------------ turn
    async def _run_turn(self, text: str, turn_id: str, speak: bool, forced: list | None = None) -> str:
        started = time.time()
        self._failed_calls.clear()
        self._blocked_tools.clear()
        self._last_tool_result = ""
        episode = self.learning.start_episode(text)
        self.set_state(THINKING)

        # Is this "that's wrong / it didn't work"? Detection is instant; the lesson
        # itself is distilled after the reply so it never slows the answer down.
        corrected = None
        try:
            if not forced:
                corrected = self.learning.detect_correction(text) or self._rephrase_of_last(text)
            await self.learning.note_preference(text)
        except Exception:
            log.exception("learning pass failed")

        plan = None if (corrected or forced) else self.learning.find_skill(text)
        if plan:
            log.info("skill cache hit for %r", text)
            return await self._replay_skill(plan, text, turn_id, speak, episode)

        self._maybe_reset_conversation()
        if not self.messages:
            self.rebuild_system_prompt()
        frontmost = await asyncio.to_thread(self._frontmost_app)
        # Thai six-hour clock ("สองทุ่ม") is spelled out as 20:00: small models misread it.
        model_text = thai_time.explicit_times(text, alarm="ปลุก" in text) if thai.THAI.search(text) else text
        learn_later = None
        if corrected:
            # Don't just apologise - go back and actually fix it.
            did = "; ".join(
                f"{c.get('name')}({json.dumps(c.get('args', {}))[:80]}) -> {r[:140]}"
                for c, r in zip(corrected.plan, corrected.results)
            ) or (corrected.reply[:200] or "nothing")
            model_text = (
                f"{text}\n\n[CORRECTION - your previous attempt failed]\n"
                f"Previous request: {corrected.request}\nWhat you did: {did}\n"
                "Work out what went wrong and fix it now using a DIFFERENT approach than before. "
                "Confirm it worked with a tool, then say in one sentence what you did."
            )
            learn_later = (corrected, text)
        if forced and any(name in {"chrome_read_page", "screen_look"} for name, _ in forced):
            model_text += ("\n[สรุปเนื้อหาจากผลของเครื่องมือเป็นภาษาไทยสั้นๆ สองถึงสามประโยคสำหรับอ่านออกเสียง "
                           "ไม่ใช้รายการหรือสัญลักษณ์ ใช้เฉพาะสิ่งที่อยู่ในผล]")
        elif forced:
            model_text += ("\n[ตอบจากผลของเครื่องมือเท่านั้น เป็นภาษาไทยสั้นๆ หนึ่งถึงสองประโยคสำหรับอ่านออกเสียง "
                           "ไม่ใช้รายการหรือสัญลักษณ์ ถ้าในผลไม่มีข้อมูลที่ถาม ให้บอกตรงๆ ว่าไม่พบ]")
        user_msg = {"role": "user", "content": with_context(self.cfg, model_text, frontmost, self._fresh_knowledge())}
        convo = self._conversation() + [user_msg]
        turn_start = len(convo)

        stream = SentenceStream()
        spoken_chars = 0
        max_spoken = int(self.cfg.get("speech.max_spoken_chars", 420))
        clipped = False
        final_text = ""
        rounds = int(self.cfg.get("llm.max_tool_rounds", 4)) + (3 if corrected else 0)
        tool_schemas = schemas()
        model = self.llm.model
        ctx = ToolContext(self)
        tools_before = len(episode.plan)
        nudges: list[dict] = []

        try:
            for round_no in range(rounds):
                assistant_text = ""
                calls = []
                error = ""

                if forced and round_no == 0:
                    calls = [_Call(name, args) for name, args in forced]
                    log.info("forced tools for %r: %s", text, [c.name for c in calls])
                    stream_source = None
                else:
                    stream_source = self.llm.chat_stream(convo, tool_schemas, model=model)
                async for chunk in (stream_source or _no_chunks()):
                    if chunk.kind == "text":
                        assistant_text += chunk.text
                    elif chunk.kind == "tool_calls":
                        calls = chunk.tool_calls
                    elif chunk.kind == "done" and chunk.stats:
                        st = chunk.stats
                        log.info(
                            "round %d: prefill %d tok %.0fms | gen %d tok %.0fms | load %.0fms",
                            round_no, st.get("prompt_eval_count", 0), st.get("prompt_eval_duration", 0) / 1e6,
                            st.get("eval_count", 0), st.get("eval_duration", 0) / 1e6, st.get("load_duration", 0) / 1e6,
                        )
                    elif chunk.kind == "error":
                        error = chunk.text

                if error:
                    await self._fail(f"The local model failed: {error}", turn_id, speak)
                    return ""

                assistant_text = strip_tool_call_text(assistant_text)
                if calls:
                    stream._buf = strip_tool_call_text(stream._buf)
                if not calls:
                    final_text = assistant_text.strip()
                    if (len(episode.plan) == tools_before and _CLAIMS_ACTION.search(final_text)
                            and not _ASKS.search(final_text)):
                        # "กำลังเล่น X ครับ" with no tool call: the small model copied an
                        # earlier reply. Nothing happened, so it must not be said.
                        if not nudges and round_no + 1 < rounds:
                            log.info("model claimed an action without calling a tool; asking again")
                            nudges += [{"role": "assistant", "content": final_text},
                                       {"role": "user", "content": "[SYSTEM] You did not call any tool, so nothing "
                                        "happened. Call the right tool now, or say plainly that you cannot."}]
                            convo += nudges
                            continue
                        final_text = ("ผมยังไม่ได้ทำคำสั่งนี้จริงครับ ลองสั่งอีกครั้งโดยบอกชื่อเพลงหรือชื่อคนให้ชัดเจน"
                                      if self.cfg.get("language") == "th" else "I didn't actually do that. Please ask again.")
                    self.broadcast({"type": "reply_delta", "turn": turn_id, "text": final_text})
                    if speak:
                        for sentence in stream.feed(final_text):
                            if spoken_chars + len(sentence) <= max_spoken:
                                self.speaker.say(sentence)
                                spoken_chars += len(sentence)
                            else:
                                clipped = True
                                break
                    break

                # Fast path: one tool, its result is already an answer, and the
                # request asked for one thing. Speak it and stop - skipping a
                # whole generation round is the single biggest latency win.
                if self._can_finish_on_tool(calls, text, round_no):
                    result = await self._run_tool(ctx, calls[0], turn_id, episode)
                    if not result.startswith(("Error:", "Cancelled by the user")) and _speakable(result):
                        final_text = result
                        self.broadcast({"type": "reply_delta", "turn": turn_id, "text": result})
                        if speak:
                            for sentence in stream.feed(result):
                                self.speaker.say(sentence)
                        break
                    # It failed - hand the error back and let the model recover.
                    convo.append({
                        "role": "assistant",
                        "content": assistant_text,
                        "tool_calls": [{"function": {"name": calls[0].name, "arguments": calls[0].arguments}}],
                    })
                    convo.append({"role": "tool", "content": result, "tool_name": calls[0].name})
                    continue

                convo.append({
                    "role": "assistant",
                    "content": assistant_text,
                    "tool_calls": [
                        {"function": {"name": c.name, "arguments": c.arguments}} for c in calls
                    ],
                })

                for call in calls:
                    spec = tools_mod.REGISTRY.get(call.name)
                    if (speak and self.cfg.get("language") != "th" and not assistant_text.strip() and spec and spec.ack
                            and call.name not in tools_mod.SPEAKABLE_RESULT):
                        with contextlib.suppress(Exception):
                            self.speaker.say(spec.ack.format(**{k: str(v) for k, v in call.arguments.items()}))
                    result = await self._run_tool(ctx, call, turn_id, episode)
                    convo.append({"role": "tool", "content": result, "tool_name": call.name})
                self.set_state(THINKING)
            else:
                final_text = "That needed more steps than I'm allowed. Ask me again, more specifically."

            web_results = [m.get("content", "") for m in convo[turn_start:]
                           if m.get("role") == "tool" and m.get("tool_name") in {"web_search", "fetch_url"}]
            if web_results and final_text and _ungrounded_numbers(final_text, " ".join(web_results) + " " + text):
                # A small model reading search snippets invents prices ("29,500 บาท") when the
                # snippets carry none. Only numbers that are in the results may be said.
                log.info("reply had numbers not in the search results: %r", final_text[:160])
                source = re.search(r" - ([a-z0-9.-]+\.[a-z]{2,})(?: - |$)", " ".join(web_results), re.M)
                final_text = ("ผลค้นหาไม่มีตัวเลขที่ชัดเจนครับ" + (f" ข้อมูลล่าสุดดูได้ที่ {source[1]}" if source else "")
                              if self.cfg.get("language") == "th" else
                              "The search results don't give a clear figure." + (f" Check {source[1]}." if source else ""))
                stream = SentenceStream()
                if speak:
                    self.speaker.say(final_text)
            if speak and not clipped:
                rest = stream.flush()
                if rest and spoken_chars + len(rest) <= max_spoken:
                    self.speaker.say(rest)
                elif rest:
                    self.speaker.say("The rest is on screen.")
            if not final_text.strip():
                # The model sometimes runs a tool and then says nothing at all.
                # Silence is the worst possible answer, so fall back to what the
                # tool actually reported.
                last_result = getattr(self, "_last_tool_result", "")
                final_text = last_result.removeprefix("Error: ") if last_result else ("ยังไม่มีผลลัพธ์ยืนยันครับ" if self.cfg.get("language") == "th" else "No result was confirmed.")
                if speak:
                    self.speaker.say(for_speech(final_text))

            self.broadcast({"type": "reply_done", "turn": turn_id, "text": final_text})
            if final_text:
                self.memory.log_turn("assistant", final_text)
            convo = [m for m in convo if not any(m is n for n in nudges)]
            self.messages = convo + ([{"role": "assistant", "content": final_text}] if final_text else [])
            self._last_turn_at = time.time()

            episode.reply = final_text
            episode.seconds = time.time() - started
            self.learning.finish_episode(episode)
            self._last_episode_at = time.time()
            if learn_later:
                asyncio.create_task(self._learn_after_reply(*learn_later))
            if (PERSONAL.search(text) and not QUESTION.match(text.strip()) and not _COMMAND.match(text.strip())
                    and not text.strip().endswith("?")):
                asyncio.create_task(self._capture_facts(text))

            if speak:
                self._schedule_followup()
            else:
                self.set_state(IDLE)
            return final_text

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("turn failed")
            await self._fail(f"Something went wrong: {exc}", turn_id, speak)
            return ""

    # A request with a conjunction may need more than one tool, so the
    # single-tool shortcut must not fire on it and drop half the job.
    _MULTI_INTENT = re.compile(r"\b(and|then|also|after that|as well as|plus)\b|แล้ว|และ", re.I)

    def _can_finish_on_tool(self, calls, request: str, round_no: int) -> bool:
        if self.cfg.get("language") == "th":
            return False  # let the local model render non-localised tool results in Thai
        if round_no != 0 or len(calls) != 1:
            return False
        if calls[0].name not in tools_mod.SPEAKABLE_RESULT:
            return False
        if self._MULTI_INTENT.search(request or ""):
            return False
        return len((request or "").split()) <= 16

    async def _run_tool(self, ctx: ToolContext, call, turn_id: str, episode) -> str:
        # A small model that gets an error likes to try the identical call again.
        # Refuse the repeat: it costs seconds and never works.
        signature = f"{call.name}:{sorted((call.arguments or {}).items())}"
        if call.name in self._blocked_tools:
            return (
                f"Error: {call.name} cannot work until the user grants Jarvis permission. "
                "Tell them what to enable and stop - do not try another tool for this."
            )
        if signature in self._failed_calls:
            return (
                f"Error: {call.name} already failed with these exact arguments this turn. "
                "Do not call it again - tell the user what went wrong, or try a different approach."
            )

        call_id = uuid.uuid4().hex[:6]
        self.broadcast({
            "type": "tool", "turn": turn_id, "id": call_id,
            "name": call.name, "args": call.arguments, "status": "running", "result": None,
        })
        log.info("tool %s(%s)", call.name, call.arguments)
        self.set_state(WORKING, call.name)
        try:
            result = await execute(ctx, call.name, call.arguments)
        finally:
            if self.state == WORKING:
                self.set_state(THINKING)
        if self.cfg.get("language") == "th" and not result.startswith(("Error:", "Cancelled by the user")):
            result = thai.localise_result(call.name, call.arguments, result)
        failed = isinstance(result, str) and result.startswith("Error:")
        log.info("tool %s returned %d chars%s", call.name, len(str(result)), " (error)" if failed else "")
        episode.plan.append({"name": call.name, "args": call.arguments})
        episode.results.append(str(result)[:300])
        if call.name in ("open_app", "quit_app") and (call.arguments or {}).get("name"):
            self._last_app = call.arguments["name"]
        if failed:
            episode.errors += 1
            self._failed_calls.add(signature)
            if "permission" in result.lower() or "System Settings" in result:
                self._blocked_tools.add(call.name)
        self.broadcast({
            "type": "tool", "turn": turn_id, "id": call_id, "name": call.name,
            "args": call.arguments, "status": "error" if failed else "ok",
            "result": result[:400],
        })
        self._last_tool_result = result
        try:
            if self.learning.record_tool(call.name, call.arguments, result):
                asyncio.create_task(self.learning.learn_from_tool_failure(call.name, call.arguments, result))
        except Exception:
            log.exception("tool learning failed")
        return result

    async def _replay_skill(self, plan: list[dict], text: str, turn_id: str, speak: bool, episode) -> str:
        """Run a proven plan with no model call at all."""
        ctx = ToolContext(self)
        results = []
        for step in plan:
            spec = tools_mod.REGISTRY.get(step.get("name", ""))
            if spec is None:
                return await self._run_turn_without_skill(text, turn_id, speak)
            if speak and self.cfg.get("language") != "th" and spec.ack:
                with contextlib.suppress(Exception):
                    self.speaker.say(spec.ack.format(**{k: str(v) for k, v in step.get("args", {}).items()}))
            result = await self._run_tool(ctx, _Call(step["name"], step.get("args", {})), turn_id, episode)
            if result.startswith("Error:"):
                # The shortcut no longer works; fall back to thinking it through.
                self.learning.forget_lesson("")  # no-op guard, keeps signature honest
                return await self._run_turn_without_skill(text, turn_id, speak)
            results.append(result)
        reply = results[-1] if results else "Done."
        self.learning.used_skill(text)
        episode.reply = reply
        with contextlib.suppress(Exception):
            self.learning.finish_episode(episode)
        self._last_episode_at = time.time()
        self.broadcast({"type": "reply_done", "turn": turn_id, "text": reply})
        self.memory.log_turn("assistant", reply)
        if speak:
            self.speaker.say(for_speech(reply))
            self._schedule_followup()
        else:
            self.set_state(IDLE)
        return reply

    async def _run_turn_without_skill(self, text: str, turn_id: str, speak: bool) -> str:
        self.cfg.set("learning.skill_cache", False)
        try:
            return await self._run_turn(text, turn_id, speak)
        finally:
            self.cfg.set("learning.skill_cache", True)

    async def _fail(self, message: str, turn_id: str, speak: bool) -> None:
        log.error(message)
        self.earcons.play("error")
        self.broadcast({"type": "reply_done", "turn": turn_id, "text": message})
        self.broadcast({"type": "notice", "level": "error", "text": message})
        if speak:
            await self.say(message)
        self.set_state(IDLE)

    # --------------------------------------------------------- conversation
    def _conversation(self) -> list[dict]:
        keep = int(self.cfg.get("llm.history_turns", 12)) * 2
        history = [m for m in self.messages if m.get("role") != "system"]
        if len(history) > keep:
            # Trim in one big step, never one message per turn. A window that slides
            # every turn changes the prompt prefix every turn, so Ollama re-reads the
            # whole history instead of serving it from cache - seconds, every time.
            cut = len(history) - keep // 2
            while cut > 0 and history[cut].get("role") != "user":
                cut -= 1  # walk back to a user message: never start on an orphaned tool result
            history = history[cut:]
            self.messages = history
        return [{"role": "system", "content": self._system_prompt}] + history

    def _maybe_reset_conversation(self) -> None:
        timeout_min = float(self.cfg.get("llm.conversation_timeout_min", 15))
        if self._last_turn_at and time.time() - self._last_turn_at > timeout_min * 60:
            log.info("conversation timed out - starting fresh")
            self.new_conversation()

    async def _side_chat(self, prompt: str, num_predict: int = 90) -> str:
        """A background model call that starts with the exact conversation so far.

        Ollama keeps one cached prompt. A side call with a different prefix replaces
        it, and the next real question pays to re-read everything. Starting from the
        same conversation means the next question only reads its own new words."""
        data = await self.llm.chat(
            self._conversation() + [{"role": "user", "content": prompt}],
            tools=schemas(),
            options={"num_predict": num_predict},
        )
        return strip_tool_call_text(((data.get("message") or {}).get("content") or "")).strip()

    def _rephrase_of_last(self, text: str):
        """Asking again, a little differently, soon after an answer means it missed -
        even if they never said "that's wrong"."""
        from difflib import SequenceMatcher

        from jarvis.learning import normalise

        ep = self.learning.last_episode
        if not ep or time.time() - self._last_episode_at > 45:
            return None
        # Only a miss if the last attempt looked like one: it errored, or never acted.
        if ep.errors == 0 and ep.plan:
            return None
        new = normalise(text)
        if re.sub(r"\d+", "#", new) == re.sub(r"\d+", "#", ep.norm) and new != ep.norm:
            return None  # "5 minutes" -> "10 minutes" is a new request, not a complaint
        if SequenceMatcher(None, new, ep.norm).ratio() < 0.72:
            return None
        log.info("asked again (%r) - treating the last attempt as a miss", ep.request)
        return self.learning.detect_correction("that didn't work")

    async def _reflect_loop(self) -> None:
        """While idle, turn recent failures into rules and keep the rule list short."""
        last = time.time()
        while True:
            await asyncio.sleep(60)
            try:
                quiet = time.time() - max(self._last_turn_at, self._last_episode_at) > 600
                if not quiet or self.speaker.busy or self.state not in (IDLE, MUTED) or time.time() - last < 1800:
                    continue
                last = time.time()
                if await self._reflect():
                    self.rebuild_system_prompt()
                    await self._warm_model()
            except Exception:
                log.exception("reflection failed")

    async def _reflect(self) -> bool:
        changed = False
        since = float(self.memory.get_kv("reflected_at", 0) or 0)
        with self.memory._lock:
            rows = [dict(r) for r in self.memory.db.execute(
                "SELECT request, plan, reply FROM episodes WHERE (errors > 0 OR corrected = 1) AND ts > ? "
                "ORDER BY id DESC LIMIT 5", (since,))]
        for r in rows:
            rule = await self._side_chat(
                "An assistant handled this request badly.\n"
                f"Request: {r['request']}\nWhat it did: {r['plan'][:300]}\nIts reply: {(r['reply'] or '')[:200]}\n"
                "Write ONE rule, under 18 words, telling the assistant what to do next time. "
                "Output only the rule. Do not call any tools.", 50)
            rule = rule.splitlines()[0].strip().strip('"') if rule else ""
            if 8 < len(rule) < 200:
                self.learning.add_lesson(rule, kind="reflection")
                changed = True
        self.memory.set_kv("reflected_at", time.time())
        with self.memory._lock:
            lessons = [r["lesson"] for r in self.memory.db.execute("SELECT lesson FROM lessons ORDER BY created ASC")]
        if len(lessons) > 14:
            merged = await self._side_chat(
                "Merge these rules for an assistant into at most 10 short, non-overlapping rules. "
                "Where two conflict, the later one wins. One rule per line, nothing else. Do not call any tools.\n\n"
                + "\n".join(lessons), 320)
            new = [ln.strip(" -*•\t").lstrip("0123456789.) ").strip() for ln in merged.splitlines()]
            new = [ln for ln in new if 8 < len(ln) < 200][:10]
            if len(new) >= 4 and not set(new) <= set(lessons):
                with self.memory._lock:
                    self.memory.db.execute("DELETE FROM lessons")
                    self.memory.db.commit()
                for ln in new:
                    self.learning.add_lesson(ln, kind="consolidated")
                log.info("consolidated %d lessons into %d", len(lessons), len(new))
                changed = True
        return changed

    def _restore_conversation(self) -> None:
        """A restart must not erase the conversation - reload it from the log."""
        keep = int(self.cfg.get("llm.history_turns", 12)) * 2
        turns = self.memory.recent_turns(limit=keep + 10)
        if not turns:
            return
        timeout = float(self.cfg.get("llm.conversation_timeout_min", 120)) * 60
        if time.time() - float(turns[-1]["ts"]) > timeout:
            return
        # Only what came after the last "start over", and after the last long silence.
        for i in range(len(turns) - 1, 0, -1):
            if turns[i]["role"] == "reset" or float(turns[i]["ts"]) - float(turns[i - 1]["ts"]) > timeout:
                turns = turns[i:]
                break
        msgs: list[dict] = []
        for t in turns:
            if t["role"] not in ("user", "assistant") or not t["text"]:
                continue
            if msgs and msgs[-1]["role"] == t["role"] and msgs[-1]["content"] == t["text"]:
                continue  # the fast path logs some user turns twice
            msgs.append({"role": t["role"], "content": t["text"]})
        self.messages = msgs[-keep:]
        self._last_turn_at = float(turns[-1]["ts"])
        log.info("restored %d messages of conversation", len(self.messages))

    def _fresh_knowledge(self) -> str | None:
        """Facts and lessons learned since the system prompt was built. They go in the
        user message so they apply on the very next turn, without a cold re-prefill."""
        since = getattr(self, "_prompt_built_at", time.time())
        with self.memory._lock:
            facts = [r["text"] for r in self.memory.db.execute(
                "SELECT text FROM facts WHERE created > ? ORDER BY id DESC LIMIT 6", (since,))]
            lessons = [r["lesson"] for r in self.memory.db.execute(
                "SELECT lesson FROM lessons WHERE created > ? ORDER BY id DESC LIMIT 6", (since,))]
        bits = []
        if facts:
            bits.append("just learned about the user: " + "; ".join(facts))
        if lessons:
            bits.append("rules from recent corrections: " + "; ".join(lessons))
        return " | ".join(bits) or None

    async def _capture_facts(self, text: str) -> None:
        """Keep personal facts without being told to 'remember'. Runs after the reply."""
        await asyncio.sleep(0.3)
        await self._wait_for_speech_end(timeout=60)
        prompt = (
            "Extract durable facts about the user from this message - preferences, people, "
            "places, projects, habits, details about their life. Write each as a short "
            "third-person sentence on its own line, starting with their name or 'The user'. "
            "Ignore one-off requests and commands. If there is nothing worth keeping, reply NONE.\n\n"
            f"Message: {text}"
        )
        try:
            out = await self._side_chat(prompt + "\nDo not call any tools.", 90)
        except Exception:
            return
        if not out or out.upper().startswith("NONE"):
            return
        known = {f.text.lower().strip(" .") for f in self.memory.list_facts(limit=500)}
        for line in out.splitlines():
            fact = line.strip(" -*•\t").strip()
            if fact[:1] in "<{[" or "tool_call" in fact:
                continue
            if 8 < len(fact) < 200 and "none" not in fact.lower()[:6] and fact.lower().strip(" .") not in known:
                self.memory.add_fact(fact, source="auto")
                log.info("remembered: %s", fact)

    def new_conversation(self) -> None:
        self.memory.log_turn("reset", "conversation cleared")
        self.messages = []
        self._last_turn_at = 0.0
        self.rebuild_system_prompt()

    def _frontmost_app(self) -> str | None:
        """Fresh every call. NSWorkspace goes stale without a Cocoa run loop."""
        import subprocess

        try:
            asn = subprocess.run(["/usr/bin/lsappinfo", "front"], capture_output=True, text=True, timeout=2).stdout.strip()
            out = subprocess.run(["/usr/bin/lsappinfo", "info", "-only", "name", asn],
                                 capture_output=True, text=True, timeout=2).stdout
            m = re.search(r'"(?:LSDisplayName|name)"="([^"]+)"', out)
            return m.group(1) if m else None
        except Exception:
            return None

    async def _learn_after_reply(self, ep, text: str) -> None:
        await asyncio.sleep(0.5)
        await self._wait_for_speech_end(timeout=60)
        with contextlib.suppress(Exception):
            await self.learning.learn_from_correction(ep, text)

    # --------------------------------------------------------------- speaking
    async def say(self, text: str, force: bool = False) -> None:
        """Speak a line that did not come from the model."""
        clean = for_speech(text)
        if not clean:
            return
        self.speaker.say(clean)

    def speak_async(self, text: str) -> None:
        clean = for_speech(text)
        if clean:
            self.speaker.say(clean)

    def _stop_output(self) -> None:
        """Silence Jarvis and abandon the reply in progress, without touching the mic."""
        self.speaker.stop()
        if self._turn_task and not self._turn_task.done():
            self._turn_task.cancel()
        if self._followup_task and not self._followup_task.done():
            self._followup_task.cancel()
        self._pending_announcements.clear()

    def interrupt(self, reason: str = "") -> None:
        log.info("interrupted (%s)", reason)
        self.speaker.stop()
        if self._turn_task and not self._turn_task.done():
            self._turn_task.cancel()
        if self._followup_task and not self._followup_task.done():
            self._followup_task.cancel()
        self.listener.cancel_capture()
        if self._pending_confirm:
            self._resolve_confirmation(False, spoken=False)
        self.set_state(IDLE)

    def _schedule_followup(self) -> None:
        """After Jarvis finishes talking, keep listening briefly for a follow-up."""
        if self._followup_task and not self._followup_task.done():
            self._followup_task.cancel()
        self._followup_task = asyncio.create_task(self._followup())

    async def _followup(self) -> None:
        try:
            await self._wait_for_speech_end()
            window = float(self.cfg.get("listening.followup_seconds", 5.0))
            if window <= 0 or not self.listener.enabled or self._mic_status != "ok":
                self.set_state(IDLE)
                return
            self.set_state(LISTENING, "follow-up")
            self.listener.request_capture(followup=True, settle=0.25)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("follow-up failed")
            self.set_state(IDLE)

    async def _wait_for_speech_end(self, timeout: float = 120.0) -> None:
        deadline = time.monotonic() + timeout
        await asyncio.sleep(0.05)
        while self.speaker.busy and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

    # ----------------------------------------------------------- confirmation
    async def request_confirmation(self, prompt: str, detail: str = "") -> bool:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bool] = loop.create_future()
        confirm_id = uuid.uuid4().hex[:6]
        self._pending_confirm = {"id": confirm_id, "future": fut, "prompt": prompt}
        self.set_state(CONFIRM, prompt)
        self.broadcast({"type": "confirm_request", "id": confirm_id, "prompt": prompt, "detail": detail})
        self.speaker.say(for_speech(prompt))

        listen_task = asyncio.create_task(self._listen_for_answer())
        timeout = float(self.cfg.get("safety.confirm_timeout_s", 25))
        try:
            approved = await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
        except asyncio.TimeoutError:
            approved = False
            self.speaker.say("I'll leave it.")
        finally:
            listen_task.cancel()
            self._pending_confirm = None
        self.broadcast({"type": "confirm_done", "id": confirm_id, "approved": approved})
        self.set_state(THINKING)
        return approved

    async def _listen_for_answer(self) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            await self._wait_for_speech_end(timeout=20)
            if self._pending_confirm and self.listener.enabled and self._mic_status == "ok":
                self.listener.request_capture(followup=True, settle=0.25)

    def _resolve_confirmation(self, approved: bool, spoken: bool) -> None:
        pending = self._pending_confirm
        if not pending:
            return
        self._pending_confirm = None
        fut = pending["future"]
        if not fut.done():
            fut.set_result(approved)
        self.broadcast({"type": "confirm_done", "id": pending["id"], "approved": approved})

    def resolve_confirmation_from_ui(self, confirm_id: str, approved: bool) -> bool:
        if self._pending_confirm and self._pending_confirm["id"] == confirm_id:
            self._resolve_confirmation(approved, spoken=False)
            return True
        return False

    # ------------------------------------------------------------ activation
    def activate(self) -> None:
        """Hotkey or orb click: start listening now, or interrupt if talking."""
        if self._mic_status != "ok":
            self.broadcast({"type": "notice", "level": "warn", "text": "The microphone is not available."})
            return
        self._stop_output()
        if self.state == LISTENING and self.listener.mode == Listener.CAPTURE:
            return  # already listening; let them finish
        if not self.listener.enabled:
            self.set_listening(True)
        self.earcons.play("wake")
        self.set_state(LISTENING)
        asyncio.create_task(self.llm.warm())
        self.listener.request_capture(followup=False, settle=0.15)

    def set_listening(self, enabled: bool) -> None:
        self.listener.set_enabled(enabled)
        self.cfg.set("listening.enabled", enabled)
        self.set_state(IDLE if enabled else MUTED)
        self.broadcast(self.status())

    # ---------------------------------------------------------- announcements
    async def _on_scheduled(self, item: ScheduledItem, late: bool) -> None:
        text = item.text
        if late:
            text = f"{text} - that was due while I was away."
        self.earcons.play("alarm")
        await self.announce(text)
        self.broadcast({"type": "timers", "items": [
            {"id": i.id, "kind": i.kind, "text": i.text, "label": i.label, "fire_at": i.fire_at}
            for i in self.scheduler.pending()
        ]})

    # ------------------------------------------------------------ self-editing
    async def _apply_self_edit(self, job: Job) -> None:
        """Claude finished changing our code: verify it, keep or undo it, then restart."""
        from jarvis.jobs import NOT_LOGGED_IN
        from jarvis.tools import self_edit as se

        cp = job.meta.get("checkpoint", "")
        request = job.meta.get("request", "that")
        loop = asyncio.get_running_loop()
        changed: list[str] = []
        try:
            changed = await loop.run_in_executor(None, se.changed_since, cp)
            logged_out = "not logged in" in (job.result or "").lower()
            if job.status != "done" or logged_out:
                await loop.run_in_executor(None, se.restore, cp, changed)
                se.end()
                if logged_out:
                    self.jobs.forget_auth()
                await self.announce(NOT_LOGGED_IN if logged_out
                                    else "I couldn't finish changing myself, so I left my code exactly as it was.")
                return
            if not changed:
                se.end()
                await self.announce(f"I looked into it but didn't change any code. {(job.result or '')[:240]}")
                return
            ok, detail = await se.verify(changed)
            if not ok:
                log.error("self-edit failed verification: %s", detail)
                await loop.run_in_executor(None, se.restore, cp, changed)
                se.end()
                why = f" {detail[0].upper()}{detail[1:]}." if detail.startswith("it tried") else ""
                await self.announce(f"That change didn't pass my checks, so I undid it.{why}")
                return
            sha = await loop.run_in_executor(None, se.commit_paths, f"self-edit: {request}", changed)
            self.memory.set_kv("self_edit_last", {"checkpoint": cp, "commit": sha, "request": request, "changed": changed})
            se.MARKER.write_text(json.dumps({"request": request, "summary": (job.result or "")[:300]}))
            se.end()
            await self._restart_when_free(cp, changed, swift=any(p.startswith("app/") for p in changed),
                                          say="Done and checked. Restarting to apply it.")
        except Exception:
            log.exception("applying self-edit failed")
            with contextlib.suppress(Exception):
                await loop.run_in_executor(None, se.restore, cp, changed)
            se.end()
            await self.announce("Something went wrong applying that change, so I put my code back.")

    async def _restart_when_free(self, commit: str, paths: list[str], swift: bool, say: str = "") -> None:
        """Restart only when nothing else would be cut off: no other Claude work, no turn."""
        from jarvis.tools import self_edit as se

        deadline = time.monotonic() + 1800
        while time.monotonic() < deadline and (
            any(j.kind != "self_edit" for j in self.jobs.active()) or self.state in (THINKING, CONFIRM, LISTENING)
        ):
            await asyncio.sleep(3)
        if say:
            await self.say(say)
        await asyncio.sleep(0.3)
        await self._wait_for_speech_end(timeout=30)
        se.launch_guard(commit, paths, swift, int(self.cfg.get("server.port", 8765)))

    def restart_after_reply(self, commit: str, paths: list[str], swift: bool) -> None:
        asyncio.create_task(self._restart_when_free(commit, paths, swift))

    async def _announce_self_edit_result(self) -> None:
        from jarvis.tools import self_edit as se

        await asyncio.sleep(1.5)
        pending = se.take_marker(se.PENDING)
        if pending:
            # The brain died while Claude was editing. Stop the orphan and undo its work.
            import subprocess

            subprocess.run(["/usr/bin/pkill", "-f", "You are editing the source code of Jarvis"], check=False)
            cp = pending.get("checkpoint", "")
            try:
                if cp and se.head() == cp:
                    se.restore(cp, se.changed_since(cp))
            except Exception:
                log.exception("recovering an interrupted self-edit failed")
            await self.announce("I was interrupted while changing myself, so I put my code back the way it was.")
            return
        if se.take_marker(se.ROLLBACK_MARKER):
            self.memory.set_kv("self_edit_last", None)
            await self.announce("That change to myself stopped me starting properly, so I rolled it back. I'm on the old version.")
        elif (done := se.take_marker(se.MARKER)):
            summary = (done.get("summary") or "").strip()
            await self.announce(f"I'm back with the change. {summary}" if summary else "I'm back with the change.")

    def on_job_finished(self, job: Job) -> None:
        if job.kind == "self_edit":
            asyncio.create_task(self._apply_self_edit(job))
            return
        if "not logged in" in (job.result or "").lower():
            from jarvis.jobs import NOT_LOGGED_IN

            self.jobs.forget_auth()
            asyncio.create_task(self.announce(NOT_LOGGED_IN))
            return
        if job.status == "cancelled":
            return
        prefix = "Claude finished" if job.status == "done" else "Claude stopped"
        text = f"{prefix}: {job.result}" if job.result else f"{prefix}."
        self.earcons.play("done" if job.status == "done" else "error")
        asyncio.create_task(self.announce(text))

    async def announce(self, text: str) -> None:
        """Speak something unprompted, waiting for a gap if mid-conversation."""
        if self.state in (THINKING, CONFIRM) or self.speaker.busy:
            self._pending_announcements.append((text, "queued"))
            asyncio.create_task(self._drain_announcements())
            return
        self.broadcast({"type": "reply_done", "turn": "announce", "text": text})
        self.memory.log_turn("assistant", text)
        self.speaker.say(for_speech(text))

    async def _drain_announcements(self) -> None:
        await self._wait_for_speech_end(timeout=180)
        while self._pending_announcements and self.state not in (THINKING, CONFIRM):
            text, _ = self._pending_announcements.pop(0)
            self.broadcast({"type": "reply_done", "turn": "announce", "text": text})
            self.speaker.say(for_speech(text))
            await self._wait_for_speech_end(timeout=60)


_LIST_SHAPED = re.compile(r"\n\s*\d+[.)]\s|\n\s*[-*]\s")


def _speakable(result: str) -> bool:
    """Is this tool result a sentence, or a list that needs summarising?

    Reading "28 tabs open: 1. AP Micro Unit 1 Test Study Guide..." aloud is
    worse than spending a round having the model say "28 tabs, mostly study
    material and YouTube".
    """
    if len(result) > 240:
        return False
    return not _LIST_SHAPED.search(result)


class _Call:
    """Duck-types llm.ToolCall for replayed skills."""

    def __init__(self, name: str, arguments: dict):
        self.name = name
        self.arguments = arguments
        self.id = ""
