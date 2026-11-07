"""Regression checks for voice failure recovery without a live microphone."""
from types import SimpleNamespace

import numpy as np

from jarvis.config import Config
from jarvis.tts import Speaker


def test_neural_failure_preserves_reply_using_native_voice(tmp_path, monkeypatch):
    cfg = Config(tmp_path / "config.json")
    cfg.set("voice.engine", "wayu")
    cfg.set("voice.voice", "m_mid_warm")
    speaker = Speaker(cfg, SimpleNamespace())
    closed = []

    class BrokenVoice:
        def synth(self, *args):
            raise TimeoutError("worker failed")

        def close(self):
            closed.append(True)

    speaker._neural = BrokenVoice()
    speaker._native_voices = ["Kanya", "Samantha"]
    native_calls = []

    def native(command, **kwargs):
        native_calls.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("jarvis.tts.subprocess.run", native)
    monkeypatch.setattr("jarvis.tts.sf.read", lambda *a, **k: (np.ones(2400, dtype=np.float32), 24000))
    samples, sr = speaker.synth("พร้อมครับ", voice="m_mid_warm")
    assert len(samples) == 2400 and sr == 24000
    assert native_calls[0][2] == "Kanya"
    assert closed and speaker.engine == "macos"
    # Later replies do not retry a dead worker or incur another timeout.
    speaker.synth("พร้อมครับ")
    assert len(closed) == 1 and len(native_calls) == 2


def test_neural_voice_honors_selected_voice_and_rate(tmp_path):
    cfg = Config(tmp_path / "config.json")
    cfg.set("voice.engine", "wayu")
    cfg.set("voice.voice", "m_mid_warm")
    cfg.set("voice.speed", 0.95)
    speaker = Speaker(cfg, SimpleNamespace())
    calls = []

    class Voice:
        def synth(self, *args):
            calls.append(args)
            return np.zeros(2400, dtype=np.float32), 24000

    speaker._neural = Voice()
    speaker.synth("พร้อมครับ")
    speaker.synth("พร้อมครับ", "m_young_clear", 1.1)
    assert calls == [("พร้อมครับ", "m_mid_warm", 0.95), ("พร้อมครับ", "m_young_clear", 1.1)]
