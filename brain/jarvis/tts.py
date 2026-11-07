"""Text to speech with Kokoro, sentence by sentence.

A worker thread pulls sentences off a queue, synthesises each one and hands the
audio straight to the speaker. That is what lets Jarvis start talking while the
model is still writing: the first sentence is usually audible well before the
reply is finished.
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
from pathlib import Path
import soundfile as sf
import queue
import threading
import time
from typing import Callable

import numpy as np

from jarvis.audio import AudioOutput
from jarvis.config import MODELS_DIR

log = logging.getLogger("jarvis.tts")

KOKORO_MODEL = MODELS_DIR / "kokoro-v1.0.onnx"
KOKORO_VOICES = MODELS_DIR / "voices-v1.0.bin"

BRITISH_PREFIXES = ("bm_", "bf_")


class Speaker:
    def __init__(self, cfg, audio_out: AudioOutput):
        self.cfg = cfg
        self.out = audio_out
        self._kokoro = None
        self._neural = None
        self.engine = cfg.get("voice.engine", "kokoro")
        self._native_voices = []
        self.last_ms = 0.0
        self.ready = False
        self._q: queue.Queue[tuple[str, str | None, float] | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._generation = 0  # bumped on stop() so stale audio is discarded
        self._lock = threading.Lock()
        self._synthesising = threading.Event()
        self.on_speech_start: Callable[[], None] | None = None
        self.on_speech_end: Callable[[], None] | None = None
        self._was_active = False

    # ------------------------------------------------------------------ load
    def load_sync(self) -> None:
        t0 = time.time()
        if self.engine in ("macos", "wayu"):
            listing = subprocess.run(["/usr/bin/say", "-v", "?"], capture_output=True,
                                     text=True, check=True, timeout=10).stdout
            self._native_voices = [re.split(r"\s+[a-z]{2}_[A-Z]{2}\s+", line)[0].strip()
                                   for line in listing.splitlines()]
            for key in ("voice.thai_voice", "voice.english_voice"):
                voice = self.cfg.get(key)
                if not self._resolve_voice(voice):
                    raise RuntimeError(f"Voice {voice!r} is not installed. Download it in System Settings > Accessibility > Read & Speak > System voice > Manage Voices.")
            if self.engine == "wayu":
                from jarvis.neural_voice import NeuralVoice
                self._neural = NeuralVoice(self.cfg)
                try:
                    self._neural.load()
                except Exception:
                    log.exception("Local neural voice unavailable; using installed macOS voices")
                    self._neural.close()
                    self._neural = None
                    self.engine = "macos"
            self.ready = True
            self._thread = threading.Thread(target=self._worker, name="jarvis-tts", daemon=True)
            self._thread.start()
            return
        from kokoro_onnx import Kokoro

        if not KOKORO_MODEL.exists() or not KOKORO_VOICES.exists():
            raise FileNotFoundError(
                f"Kokoro voice model missing. Run scripts/fetch_models.sh from the Jarvis folder"
            )
        self._kokoro = Kokoro(str(KOKORO_MODEL), str(KOKORO_VOICES))
        self.ready = True
        log.info("tts loaded in %.1fs", time.time() - t0)
        self._thread = threading.Thread(target=self._worker, name="jarvis-tts", daemon=True)
        self._thread.start()

    def _resolve_voice(self, voice: str) -> str | None:
        if voice in self._native_voices:
            return voice
        base = (voice or "").split(" (")[0]
        return next((v for v in self._native_voices if v.split(" (")[0] == base), None)

    def voices(self) -> list[str]:
        if self.engine == "wayu":
            return ["m_jarvis_smooth", "m_young_clear", "m_mid_warm", "m_teen_bright", "m_elderly_deep"]
        if self.engine == "macos":
            return self._native_voices
        if not self._kokoro:
            return []
        try:
            return sorted(self._kokoro.get_voices())
        except Exception:
            return []

    # ------------------------------------------------------------- synthesis
    def _lang_for(self, voice: str) -> str:
        return "en-gb" if voice.startswith(BRITISH_PREFIXES) else "en-us"

    def synth(self, text: str, voice: str | None = None, speed: float | None = None):
        """Blocking synthesis. Returns (float32 samples, sample_rate)."""
        if self.engine == "wayu" and self._neural is not None:
            t0 = time.monotonic()
            try:
                samples, sr = self._neural.synth(
                    text, voice or self.cfg.get("voice.voice", "m_mid_warm"),
                    speed if speed is not None else self.cfg.get("voice.speed", 1.0),
                )
                self.last_ms = (time.monotonic() - t0) * 1000
                return samples, sr
            except Exception:
                log.exception("Neural synthesis failed; speaking this reply with macOS")
                self._neural.close()
                self._neural = None
                # A failed worker is disabled until the next restart, avoiding
                # repeated long timeouts. Preserve the reply using native speech.
                self.engine = "macos"
                voice = None
        if self.engine in ("macos", "wayu"):
            t0 = time.monotonic()
            voice = voice or self.cfg.get("voice.thai_voice" if re.search(r"[฀-๿]", text) else "voice.english_voice")
            voice = self._resolve_voice(voice) or voice
            rate = max(80, min(350, int(self.cfg.get("voice.rate", 175) * (speed if speed is not None else self.cfg.get("voice.speed", 1.0)))))
            with tempfile.TemporaryDirectory(prefix="jarvis-tts-") as tmp:
                source, target = Path(tmp) / "text.txt", Path(tmp) / "speech.aiff"
                source.write_text(text, encoding="utf-8")
                subprocess.run(["/usr/bin/say", "-v", voice, "-r", str(rate), "-f", str(source), "-o", str(target)],
                               capture_output=True, check=True, timeout=45)
                samples, sr = sf.read(target, dtype="float32")
            self.last_ms = (time.monotonic() - t0) * 1000
            if samples.ndim > 1:
                samples = samples.mean(axis=1)
            return samples, sr
        if not self._kokoro:
            raise RuntimeError("speech synthesis is not loaded")
        voice = voice or self.cfg.get("voice.voice", "bm_george")
        speed = float(speed if speed is not None else self.cfg.get("voice.speed", 1.0))
        samples, sr = self._kokoro.create(text, voice=voice, speed=speed, lang=self._lang_for(voice))
        return np.asarray(samples, dtype=np.float32), int(sr)

    # --------------------------------------------------------------- speaking
    def say(self, text: str, voice: str | None = None, speed: float | None = None) -> None:
        """Queue a sentence to speak. Returns immediately."""
        text = (text or "").strip()
        if not text:
            return
        self._q.put((text, voice, speed if speed is not None else -1.0))

    def stop(self) -> None:
        """Abandon everything queued and cut the audio now."""
        with self._lock:
            self._generation += 1
            while True:
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    break
        self.out.stop()

    @property
    def busy(self) -> bool:
        return self._synthesising.is_set() or not self._q.empty() or self.out.playing

    def wait_done(self, timeout: float = 60.0) -> bool:
        deadline = time.monotonic() + timeout
        while self.busy and time.monotonic() < deadline:
            time.sleep(0.03)
        return not self.busy

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=0.2)
            except queue.Empty:
                self._notify_idle()
                continue
            if item is None:
                break
            text, voice, speed = item
            with self._lock:
                generation = self._generation
            try:
                self._synthesising.set()
                samples, sr = self.synth(text, voice, None if speed < 0 else speed)
            except Exception:
                log.exception("synthesis failed for %r", text[:60])
                self._synthesising.clear()
                continue
            self._synthesising.clear()
            with self._lock:
                if generation != self._generation:
                    continue  # interrupted while we were synthesising
            if sr != self.out.sample_rate:
                samples = _resample(samples, sr, self.out.sample_rate)
            if not self._was_active:
                self._was_active = True
                if self.on_speech_start:
                    try:
                        self.on_speech_start()
                    except Exception:
                        log.exception("on_speech_start failed")
            self.out.play(samples)

    def _notify_idle(self) -> None:
        if self._was_active and not self.out.playing and self._q.empty():
            self._was_active = False
            if self.on_speech_end:
                try:
                    self.on_speech_end()
                except Exception:
                    log.exception("on_speech_end failed")

    def close(self) -> None:
        self._stop.set()
        self._q.put(None)
        if self._neural is not None:
            self._neural.close()


def _resample(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst or len(x) == 0:
        return x
    n = int(round(len(x) * dst / src))
    return np.interp(np.linspace(0, len(x) - 1, n, dtype=np.float64), np.arange(len(x)), x).astype(np.float32)
