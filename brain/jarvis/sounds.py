"""Earcons, synthesised rather than shipped as files.

Short non-verbal cues carry a lot: a rising pair means "I'm listening", a
falling pair means "I've stopped". They are generated once at startup so there
are no audio assets to install and no licences to worry about.
"""

from __future__ import annotations

import numpy as np

SR = 24000


def _tone(freq: float, ms: float, amp: float = 0.22, sr: int = SR) -> np.ndarray:
    n = int(sr * ms / 1000.0)
    t = np.arange(n, dtype=np.float32) / sr
    wave = np.sin(2 * np.pi * freq * t, dtype=np.float32)
    wave += 0.25 * np.sin(2 * np.pi * freq * 2 * t, dtype=np.float32)  # a little shine
    # Fast attack, exponential decay: a soft click, never a beep with edges.
    env = np.exp(-t * (5000.0 / ms)).astype(np.float32)
    attack = min(n, int(sr * 0.004))
    if attack:
        env[:attack] *= np.linspace(0.0, 1.0, attack, dtype=np.float32)
    return (wave * env * amp).astype(np.float32)


def _silence(ms: float, sr: int = SR) -> np.ndarray:
    return np.zeros(int(sr * ms / 1000.0), dtype=np.float32)


def _seq(*parts: np.ndarray) -> np.ndarray:
    return np.concatenate(parts).astype(np.float32)


def build() -> dict[str, np.ndarray]:
    return {
        # "I'm listening" - two rising notes
        "wake": _seq(_tone(784, 70), _silence(10), _tone(1175, 90, 0.2)),
        # "I've stopped listening" - the same pair, falling and quieter
        "sleep": _seq(_tone(880, 60, 0.12), _silence(8), _tone(587, 80, 0.1)),
        # something went wrong - a low, short double
        "error": _seq(_tone(320, 90, 0.16), _silence(20), _tone(260, 120, 0.14)),
        # a timer or reminder is up - a small bell, three strikes
        "alarm": _seq(
            _tone(1046, 160, 0.22), _silence(90),
            _tone(1046, 160, 0.22), _silence(90),
            _tone(1318, 260, 0.24),
        ),
        # a background job finished
        "done": _seq(_tone(659, 70, 0.14), _silence(10), _tone(988, 110, 0.16)),
    }


class Earcons:
    def __init__(self, audio_out, enabled: bool = True):
        self.out = audio_out
        self.enabled = enabled
        self._cache = build()

    def play(self, name: str) -> None:
        if not self.enabled or self.out is None:
            return
        clip = self._cache.get(name)
        if clip is not None:
            self.out.play(clip)
