"""Speech to text, on-device.

Parakeet (MLX, Apple GPU) is the default: it is fast enough that the pause after
you stop talking is dominated by the end-of-speech timeout rather than by
recognition, and it is markedly better than small Whisper models on short
commands full of app names.
"""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

log = logging.getLogger("jarvis.stt")

# Recognisers emit these for near-silence; speaking them back would be absurd.
JUNK = {
    "", ".", "?", "!", "you", "thank you", "thanks", "thank you.", "bye", "um", "uh",
    "thanks for watching", "thanks for watching!", "subscribe", "[blank_audio]", "silence",
}


class Transcriber:
    """
    All MLX work happens on ONE dedicated thread. MLX binds its streams to the
    thread that created them, so loading the model on one pool thread and
    decoding on another raises "There is no Stream(cpu, 1) in current thread"
    and every spoken command fails. A single-worker executor pins both.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.engine = cfg.get("stt.engine", "parakeet")
        self.model_id = cfg.get("stt.model", "mlx-community/parakeet-tdt-0.6b-v3")
        self._model = None
        self._mx = None
        self._get_logmel = None
        self.ready = False
        self.last_ms = 0.0
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jarvis-stt")

    # ------------------------------------------------------------------ load
    def load_sync(self) -> None:
        t0 = time.time()
        if self.engine == "parakeet":
            import mlx.core as mx
            from parakeet_mlx import from_pretrained
            from parakeet_mlx.audio import get_logmel

            self._mx = mx
            self._get_logmel = get_logmel
            self._model = from_pretrained(self.model_id)
        else:
            import mlx_whisper  # optional fallback engine

            self._model = mlx_whisper
        self.ready = True
        log.info("stt loaded (%s) in %.1fs", self.engine, time.time() - t0)
        # Compile Metal kernels now so the first real utterance is not slow.
        try:
            self.transcribe_sync(np.zeros(16000, dtype=np.float32))
        except Exception as exc:
            log.debug("stt warm-up failed: %s", exc)

    async def load(self) -> None:
        await asyncio.get_running_loop().run_in_executor(self._pool, self.load_sync)

    # ------------------------------------------------------------ transcribe
    def transcribe_sync(self, audio: np.ndarray) -> str:
        """audio: float32 mono at 16 kHz, roughly -1..1."""
        if self._model is None:
            raise RuntimeError("speech recognition is not loaded")
        t0 = time.time()
        audio = np.asarray(audio, dtype=np.float32)
        if audio.size < 1600:  # < 100 ms
            return ""
        peak = float(np.abs(audio).max() or 0.0)
        if peak < 1e-4:
            return ""
        # Normalise quiet recordings; the built-in mic is far from the speaker.
        if peak < 0.3:
            audio = audio * (0.3 / peak)

        if self.engine == "parakeet":
            mel = self._get_logmel(self._mx.array(audio), self._model.preprocessor_config)
            results = self._model.generate(mel)
            text = results[0].text if results else ""
        else:
            out = self._model.transcribe(
                audio, path_or_hf_repo=self.cfg.get("stt.fallback_model"), language="en", fp16=True
            )
            text = out.get("text", "")

        self.last_ms = (time.time() - t0) * 1000
        return self._clean(text)

    async def transcribe(self, audio: np.ndarray) -> str:
        return await asyncio.get_running_loop().run_in_executor(self._pool, self.transcribe_sync, audio)

    def close(self) -> None:
        self._pool.shutdown(wait=False)

    def _clean(self, text: str) -> str:
        t = (text or "").strip()
        if t.lower().strip(" .!?") in JUNK:
            return ""
        # Recognisers like to open with a filler token on clipped audio.
        for prefix in ("um ", "uh ", "hmm "):
            if t.lower().startswith(prefix):
                t = t[len(prefix) :]
        return t.strip()
