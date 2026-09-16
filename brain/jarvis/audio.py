"""Microphone capture and speaker playback.

Capture runs at 16 kHz mono because that is what the wake word, the VAD and the
recogniser all expect; playback runs at the TTS rate. Both use sounddevice's
callback API, and the callbacks do almost nothing - anything expensive happens
on the listener thread, because an overrun in the audio callback is an audible
glitch.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Callable

import numpy as np

log = logging.getLogger("jarvis.audio")

SAMPLE_RATE = 16000
FRAME_SAMPLES = 1280  # 80 ms - openWakeWord's native chunk


class MicUnavailable(RuntimeError):
    pass


def mic_authorization() -> str:
    """'authorized' | 'denied' | 'undetermined' | 'unknown'.

    Checked before touching PortAudio: opening an input stream without a grant
    blocks inside CoreAudio waiting for a prompt that only the app bundle can
    raise, which would otherwise hang startup indefinitely.
    """
    try:
        import AVFoundation

        status = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(
            AVFoundation.AVMediaTypeAudio
        )
    except Exception:
        return "unknown"
    return {0: "undetermined", 1: "denied", 2: "denied", 3: "authorized"}.get(int(status), "unknown")


class AudioInput:
    """16 kHz mono capture into a queue of int16 frames."""

    def __init__(self, device: int | str | None = None):
        self.device = device
        self.frames: queue.Queue[np.ndarray] = queue.Queue(maxsize=200)
        self._stream = None
        self._sd = None
        self.dropped = 0
        self.started_at = 0.0

    def start(self) -> None:
        import sounddevice as sd

        self._sd = sd
        try:
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=FRAME_SAMPLES,
                device=self.device,
                callback=self._callback,
            )
            self._stream.start()
        except Exception as exc:  # PortAudioError and friends
            raise MicUnavailable(str(exc)) from exc
        self.started_at = time.time()
        log.info("microphone open (%s)", self._describe())

    def _describe(self) -> str:
        try:
            info = self._sd.query_devices(self.device if self.device is not None else self._sd.default.device[0])
            return str(info.get("name", "default"))
        except Exception:
            return "default"

    def _callback(self, indata, frames, time_info, status) -> None:  # audio thread
        if status:
            log.debug("input status: %s", status)
        try:
            self.frames.put_nowait(indata[:, 0].copy())
        except queue.Full:
            self.dropped += 1

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    @property
    def active(self) -> bool:
        return self._stream is not None and self._stream.active


class AudioOutput:
    """Queued playback with live level reporting and instant interruption."""

    def __init__(self, sample_rate: int = 24000, device: int | str | None = None, volume: float = 0.9):
        self.sample_rate = sample_rate
        self.device = device
        self.volume = volume
        self._q: queue.Queue[np.ndarray | None] = queue.Queue()
        self._stream = None
        self._sd = None
        self._current: np.ndarray | None = None
        self._pos = 0
        self._lock = threading.Lock()
        self._playing = threading.Event()
        self._drained = threading.Event()
        self._drained.set()
        self.on_level: Callable[[float], None] | None = None
        self._level = 0.0

    def start(self) -> None:
        import sounddevice as sd

        self._sd = sd
        self._stream = sd.OutputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="float32",
            blocksize=512,
            device=self.device,
            callback=self._callback,
        )
        self._stream.start()

    def _callback(self, outdata, frames, time_info, status) -> None:  # audio thread
        out = outdata[:, 0]
        filled = 0
        while filled < frames:
            with self._lock:
                if self._current is None or self._pos >= len(self._current):
                    try:
                        nxt = self._q.get_nowait()
                    except queue.Empty:
                        break
                    if nxt is None:
                        self._current = None
                        break
                    self._current = nxt
                    self._pos = 0
                take = min(frames - filled, len(self._current) - self._pos)
                out[filled : filled + take] = self._current[self._pos : self._pos + take]
                self._pos += take
                filled += take
        if filled < frames:
            out[filled:] = 0.0
        if filled:
            self._playing.set()
            self._drained.clear()
            peak = float(np.abs(out[:filled]).max())
            # Perceptual-ish curve: raw RMS makes the orb look lifeless.
            self._level = min(1.0, peak ** 0.6)
        else:
            self._level = 0.0
            if self._playing.is_set() and self._q.empty():
                self._playing.clear()
                self._drained.set()
        if self.on_level is not None:
            try:
                self.on_level(self._level)
            except Exception:
                pass

    def play(self, samples: np.ndarray) -> None:
        """Queue float32 mono audio at self.sample_rate."""
        if samples is None or len(samples) == 0:
            return
        audio = np.asarray(samples, dtype=np.float32) * self.volume
        np.clip(audio, -1.0, 1.0, out=audio)
        self._drained.clear()
        self._q.put(audio)

    def stop(self) -> None:
        """Drop everything queued and go silent immediately (barge-in)."""
        with self._lock:
            while True:
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    break
            self._current = None
            self._pos = 0
        self._playing.clear()
        self._drained.set()
        self._level = 0.0

    @property
    def playing(self) -> bool:
        return self._playing.is_set() or not self._q.empty()

    @property
    def level(self) -> float:
        return self._level

    def wait_drained(self, timeout: float | None = None) -> bool:
        return self._drained.wait(timeout)

    def close(self) -> None:
        self.stop()
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None


def rms_level(frame: np.ndarray) -> float:
    """0..1 loudness for the orb, scaled so normal speech sits around 0.5."""
    if frame is None or len(frame) == 0:
        return 0.0
    x = frame.astype(np.float32) / 32768.0
    rms = float(np.sqrt(np.mean(x * x)))
    return float(min(1.0, (rms * 12.0) ** 0.7))
