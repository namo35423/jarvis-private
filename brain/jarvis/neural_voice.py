"""Private pipe to the isolated local Thai voice runtime (no network service)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import select
import subprocess
import tempfile
import threading

import soundfile as sf

from jarvis.config import PROJECT_DIR, LOGS_DIR


class NeuralVoice:
    def __init__(self, cfg):
        self.cfg = cfg
        self.process = None
        self.log = None

    def load(self):
        python = PROJECT_DIR / "brain/.tts-venv/bin/python"
        worker = PROJECT_DIR / "brain/tts_runtime/worker.py"
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        self.log = (LOGS_DIR / "voice.log").open("a")
        env = dict(os.environ, HF_HUB_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1",
                   TRANSFORMERS_OFFLINE="1", TOKENIZERS_PARALLELISM="false")
        try:
            self.process = subprocess.Popen(
                [str(python), "-u", str(worker), self.cfg.get("voice.model_dir", "")],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
                text=True, bufsize=1, env=env,
            )
            result = self._read(120)
            if not result.get("ready"):
                raise RuntimeError(f"Voice failed to load: {result}")
        except BaseException:
            self.close()
            raise

    def _read(self, timeout):
        if self.process is None or self.process.stdout is None:
            raise RuntimeError("Voice worker is not running")
        if not select.select([self.process.stdout], [], [], timeout)[0]:
            raise TimeoutError("Local voice worker timed out")
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError("Local voice worker exited; see voice.log")
        result = json.loads(line)
        if "error" in result:
            raise RuntimeError(result["error"])
        return result

    def synth(self, text, voice, speed):
        if self.process is None or self.process.poll() is not None:
            raise RuntimeError("Local voice worker is unavailable")
        with tempfile.TemporaryDirectory(prefix="jarvis-neural-") as tmp:
            target = Path(tmp) / "speech.wav"
            request = {"text": text, "voice": voice, "speed": speed, "output": str(target)}
            self.process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
            self._read(90)
            return sf.read(target, dtype="float32")

    def close(self):
        process, self.process = self.process, None
        if process is not None:
            if process.poll() is None:
                process.terminate()
            # Do not wait here: Speaker.close is also called by the asyncio loop.
            for stream in (process.stdin, process.stdout):
                if stream:
                    stream.close()
            def reap():
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            threading.Thread(target=reap, daemon=True).start()
        if self.log:
            self.log.close()
            self.log = None
