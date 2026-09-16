"""Wake word, voice activity, and the decision that you have finished speaking.

Runs on its own thread so ONNX inference never blocks the audio callback or the
event loop. It owns one question: given a stream of 80 ms frames, when does a
turn start and when does it end?

Turn-taking, without acoustic echo cancellation:
  * While Jarvis is talking, only the wake word runs - the VAD would otherwise
    hear Jarvis and "interrupt" itself. Saying "Jarvis" over the top still works,
    because Jarvis never says its own wake word.
  * After Jarvis stops, a short settle delay avoids catching the speaker tail,
    then the follow-up window opens so you can just keep talking.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from collections import deque
from typing import Callable

import numpy as np

from jarvis.audio import FRAME_SAMPLES, SAMPLE_RATE, AudioInput, rms_level

log = logging.getLogger("jarvis.listener")

VAD_CHUNK = 512  # what this Silero build expects at 16 kHz (32 ms)
SPEAKER_SETTLE_S = 0.25


class SileroVAD:
    """Thin wrapper over the Silero ONNX model bundled with openWakeWord."""

    def __init__(self, model_path: str):
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 4
        self.session = ort.InferenceSession(model_path, sess_options=opts, providers=["CPUExecutionProvider"])
        names = {i.name for i in self.session.get_inputs()}
        # v4 carries LSTM state as h/c; v5 folds them into a single "state".
        self.style = "hc" if {"h", "c"} <= names else "state"
        self.reset()

    def reset(self) -> None:
        if self.style == "hc":
            self._h = np.zeros((2, 1, 64), dtype=np.float32)
            self._c = np.zeros((2, 1, 64), dtype=np.float32)
        else:
            self._state = np.zeros((2, 1, 128), dtype=np.float32)

    def __call__(self, chunk: np.ndarray) -> float:
        x = chunk.astype(np.float32).reshape(1, -1) / 32768.0
        sr = np.array(SAMPLE_RATE, dtype=np.int64)
        if self.style == "hc":
            out, self._h, self._c = self.session.run(
                ["output", "hn", "cn"], {"input": x, "sr": sr, "h": self._h, "c": self._c}
            )
        else:
            out, self._state = self.session.run(None, {"input": x, "sr": sr, "state": self._state})
        return float(np.asarray(out).ravel()[0])


class Listener(threading.Thread):
    WAKE = "wake"
    CAPTURE = "capture"
    SLEEP = "sleep"

    def __init__(
        self,
        cfg,
        audio_in: AudioInput,
        on_event: Callable[[str, dict], None],
        is_speaking: Callable[[], bool],
    ):
        super().__init__(name="jarvis-listener", daemon=True)
        self.cfg = cfg
        self.audio = audio_in
        self.on_event = on_event
        self.is_speaking = is_speaking

        self._stop = threading.Event()
        self._mode = self.WAKE
        self._lock = threading.Lock()
        self._pending_capture: dict | None = None
        self._enabled = bool(cfg.get("listening.enabled", True))

        self.oww = None
        self.vad: SileroVAD | None = None
        self.wake_ready = False

        preroll_frames = max(1, int(cfg.get("listening.preroll_ms", 400) / 80))
        self._preroll: deque[np.ndarray] = deque(maxlen=preroll_frames)
        self._vad_residual = np.zeros(0, dtype=np.int16)

        # capture state
        self._buf: list[np.ndarray] = []
        self._speech_ms = 0.0
        self._silence_ms = 0.0
        self._capture_started = 0.0
        self._capture_deadline = 0.0
        self._is_followup = False
        self._quiet_until = 0.0

    # ------------------------------------------------------------- lifecycle
    def load_models(self) -> None:
        """Load wake word + VAD. Slow (~1 s), so it happens before start()."""
        import openwakeword
        from openwakeword.model import Model

        models_dir = os.path.join(os.path.dirname(openwakeword.__file__), "resources", "models")
        wake = self.cfg.get("listening.wake_word", "hey_jarvis")
        wake_path = os.path.join(models_dir, f"{wake}_v0.1.onnx")
        try:
            self.oww = Model(
                wakeword_models=[wake_path] if os.path.exists(wake_path) else [wake],
                inference_framework="onnx",
                vad_threshold=0,
            )
            self.wake_ready = True
        except Exception as exc:
            log.error("wake word unavailable: %s", exc)
            self.wake_ready = False
        try:
            self.vad = SileroVAD(os.path.join(models_dir, "silero_vad.onnx"))
        except Exception as exc:
            log.error("VAD unavailable, falling back to energy gating: %s", exc)
            self.vad = None

    def stop(self) -> None:
        self._stop.set()

    # ---------------------------------------------------------------- inputs
    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = enabled
            if not enabled:
                self._mode = self.SLEEP
                self._reset_capture()
            elif self._mode == self.SLEEP:
                self._mode = self.WAKE
                if self.oww:
                    self.oww.reset()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def mode(self) -> str:
        return self._mode

    def request_capture(self, followup: bool = False, settle: float = 0.0) -> None:
        """Start listening for a command now (hotkey, orb click, or follow-up)."""
        with self._lock:
            self._pending_capture = {"followup": followup}
            self._quiet_until = time.monotonic() + settle

    def cancel_capture(self) -> None:
        with self._lock:
            self._pending_capture = None
            if self._mode == self.CAPTURE:
                self._reset_capture()
                self._mode = self.WAKE if self._enabled else self.SLEEP

    # ------------------------------------------------------------------ loop
    def run(self) -> None:
        while not self._stop.is_set():
            try:
                frame = self.audio.frames.get(timeout=0.3)
            except queue.Empty:
                continue
            try:
                self._process(frame)
            except Exception:
                log.exception("listener frame failed")

    def _process(self, frame: np.ndarray) -> None:
        now = time.monotonic()
        with self._lock:
            pending = self._pending_capture
            self._pending_capture = None
            enabled = self._enabled

        self._preroll.append(frame)

        if pending is not None and now >= self._quiet_until:
            self._begin_capture(pending["followup"])
        elif pending is not None:
            # asked to listen but the speaker is still settling - try next frame
            with self._lock:
                self._pending_capture = pending

        if not enabled:
            return

        if self._mode == self.CAPTURE:
            self._capture_frame(frame, now)
            return

        speaking = self.is_speaking()
        if speaking and not self.cfg.get("listening.barge_in", True):
            return
        if self.wake_ready and self.oww is not None:
            self._wake_frame(frame, speaking)

    def _wake_frame(self, frame: np.ndarray, speaking: bool) -> None:
        try:
            scores = self.oww.predict(frame)
        except Exception:
            log.exception("wake word inference failed")
            return
        threshold = float(self.cfg.get("listening.wake_threshold", 0.5))
        if speaking:
            # Slightly stricter while the speakers are live, to ignore room echo.
            threshold = min(0.95, threshold + 0.05)
        best_name, best = "", 0.0
        for name, score in scores.items():
            if score > best:
                best_name, best = name, float(score)
        if best >= threshold:
            self.oww.reset()
            self.on_event("wake", {"model": best_name, "score": best, "interrupting": speaking})
            self._begin_capture(followup=False)

    def _begin_capture(self, followup: bool) -> None:
        self._mode = self.CAPTURE
        self._is_followup = followup
        self._buf = list(self._preroll) if not followup else []
        self._speech_ms = 0.0
        self._silence_ms = 0.0
        self._capture_started = time.monotonic()
        window = (
            float(self.cfg.get("listening.followup_seconds", 5.0))
            if followup
            else float(self.cfg.get("listening.no_speech_timeout_s", 6.0))
        )
        self._capture_deadline = self._capture_started + window
        self._vad_residual = np.zeros(0, dtype=np.int16)
        if self.vad:
            self.vad.reset()
        self.on_event("capture_start", {"followup": followup, "window": window})

    def _reset_capture(self) -> None:
        self._buf = []
        self._speech_ms = 0.0
        self._silence_ms = 0.0
        self._vad_residual = np.zeros(0, dtype=np.int16)

    def _capture_frame(self, frame: np.ndarray, now: float) -> None:
        self._buf.append(frame)
        self.on_event("level", {"value": rms_level(frame)})

        speech_in_frame = self._frame_has_speech(frame)
        frame_ms = 1000.0 * len(frame) / SAMPLE_RATE
        if speech_in_frame:
            self._speech_ms += frame_ms
            self._silence_ms = 0.0
        elif self._speech_ms > 0:
            self._silence_ms += frame_ms

        end_silence = float(self.cfg.get("listening.end_silence_ms", 1200))
        min_speech = float(self.cfg.get("listening.min_speech_ms", 250))
        max_utt = float(self.cfg.get("listening.max_utterance_s", 30.0))

        if self._speech_ms >= min_speech and self._silence_ms >= end_silence:
            self._finish_capture("silence")
            return
        if now - self._capture_started >= max_utt:
            self._finish_capture("max_length")
            return
        if self._speech_ms < min_speech and now >= self._capture_deadline:
            self._mode = self.WAKE if self._enabled else self.SLEEP
            self._reset_capture()
            if self.oww:
                self.oww.reset()
            self.on_event("timeout", {"followup": self._is_followup})

    def _frame_has_speech(self, frame: np.ndarray) -> bool:
        if self.vad is None:
            return rms_level(frame) > 0.12
        data = np.concatenate([self._vad_residual, frame]) if len(self._vad_residual) else frame
        threshold = float(self.cfg.get("listening.vad_threshold", 0.5))
        voiced = False
        i = 0
        while i + VAD_CHUNK <= len(data):
            if self.vad(data[i : i + VAD_CHUNK]) >= threshold:
                voiced = True
            i += VAD_CHUNK
        self._vad_residual = data[i:]
        return voiced

    def _finish_capture(self, reason: str) -> None:
        frames = self._buf
        self._reset_capture()
        self._mode = self.WAKE if self._enabled else self.SLEEP
        if self.oww:
            self.oww.reset()
        if not frames:
            self.on_event("timeout", {"followup": self._is_followup})
            return
        audio = np.concatenate(frames).astype(np.float32) / 32768.0
        # Trim the trailing pause so the recogniser is not fed a second of silence.
        keep_tail = int(0.25 * SAMPLE_RATE)
        trim = int((self._silence_ms / 1000.0) * SAMPLE_RATE) - keep_tail
        if trim > 0 and len(audio) - trim > SAMPLE_RATE * 0.2:
            audio = audio[:-trim]
        self.on_event(
            "utterance",
            {
                "audio": audio,
                "duration": len(audio) / SAMPLE_RATE,
                "reason": reason,
                "followup": self._is_followup,
            },
        )
