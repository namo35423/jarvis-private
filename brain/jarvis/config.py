"""Configuration: defaults, disk persistence, dotted access.

Everything the user can tune lives here. The file on disk (~/.jarvis/config.json)
only stores overrides, so new defaults added in later versions are picked up
automatically instead of being pinned to whatever was first written.
"""

from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any

HOME = Path.home()
JARVIS_DIR = Path(os.environ.get("JARVIS_HOME", HOME / ".jarvis"))
CONFIG_PATH = JARVIS_DIR / "config.json"
MODELS_DIR = JARVIS_DIR / "models"
LOGS_DIR = JARVIS_DIR / "logs"
SOUNDS_DIR = JARVIS_DIR / "sounds"
DB_PATH = JARVIS_DIR / "jarvis.db"

PKG_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PKG_DIR.parent.parent  # the repository root
UI_DIR = PROJECT_DIR / "ui"

DEFAULTS: dict[str, Any] = {
    "assistant_name": "Jarvis",
    "language": "th",
    "media": {"preferred": "youtube_music"},
    "user_name": "",  # set by setup.sh, or say "call me ..."
    # Spoken honorific. "" = never use one. Set to "sir" for the full film treatment.
    "address_as": "",
    "server": {"host": "127.0.0.1", "port": 8765},
    "llm": {
        "model": "qwen3:4b-instruct",  # pure attention: shared-prefix cache reuse (qwen3.5 is hybrid and re-reads every divergent prompt)
        "fast_model": "",  # empty = reuse the main model, so only one is resident
        "vision_model": "qwen3.5:4b",
        "ollama_url": "http://127.0.0.1:11434",
        "num_ctx": 8192,
        "temperature": 0.7,
        "top_p": 0.8,
        # Keep the model resident so replies stay instant; it costs RAM, not power.
        "keep_alive": "-1m",  # never unload: a cold reload costs ~20s
        "think": False,
        "max_tool_rounds": 8,
        # Hard ceiling on reply length: a voice answer is never 300 tokens.
        "num_predict": 350,
        "request_timeout_s": 180,
        "history_turns": 12,
        "conversation_timeout_min": 120,
    },
    "voice": {
        "engine": "macos",
        "thai_voice": "Kanya (ไทย (ไทย))",
        "english_voice": "Samantha",
        "rate": 175,
        "voice": "bm_george",
        "speed": 1.0,
        "volume": 0.9,
        "sample_rate": 24000,
    },
    "stt": {
        "engine": "whisper",
        "model": "mlx-community/whisper-large-v3-turbo",
        "language": "auto",
        "allowed_languages": ["th", "en"],
        "bias_language": "th",
        "initial_prompt": "",
        "min_rms": 0.002,
        "cache_mib": 256,
        "english_model": "mlx-community/parakeet-tdt-0.6b-v3",
        "fallback_engine": "whisper",
        "fallback_model": "mlx-community/whisper-large-v3-turbo",
    },
    "listening": {
        "enabled": True,
        "wake_word": "hey_jarvis",
        "wake_threshold": 0.5,
        "vad_threshold": 0.5,
        # How long a pause means "they finished talking".
        "end_silence_ms": 650,
        # How long to keep listening after Jarvis finishes speaking.
        "followup_seconds": 7.0,
        "no_speech_timeout_s": 6.0,
        "min_speech_ms": 250,
        "max_utterance_s": 30.0,
        "preroll_ms": 400,
        "input_device": None,
        "output_device": None,
        "chimes": True,
        "barge_in": True,
    },
    "claude": {
        # auto = hand off when the task needs it; explicit = only when asked; off = never
        "mode": "off",
        "binary": "",
        "permission_mode": "acceptEdits",
        "model": "",
        "cwd": str(HOME),
        "timeout_s": 1800,
        "max_budget_usd": 3.0,
        "chrome": False,
    },
    "safety": {"confirm_risky": True, "confirm_timeout_s": 25},
    "location": {
        "city": "",
        "lat": None,
        "lon": None,
        "units": "imperial",
        "timezone": "",
    },
    "ui": {"mini_orb": True, "show_transcript": True},
    "speech": {"max_spoken_chars": 1200},
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    """Dotted-path config with copy-on-write persistence of overrides only."""

    def __init__(self, path: Path = CONFIG_PATH):
        self.path = path
        self._lock = threading.RLock()
        self._overrides: dict[str, Any] = {}
        self._data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self.reload()

    def reload(self) -> None:
        with self._lock:
            try:
                self._overrides = json.loads(self.path.read_text())
            except FileNotFoundError:
                self._overrides = {}
            except (json.JSONDecodeError, OSError):
                # A corrupt config must never stop Jarvis from starting.
                self._overrides = {}
            self._data = _deep_merge(DEFAULTS, self._overrides)

    def get(self, path: str, default: Any = None) -> Any:
        cur: Any = self._data
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def set(self, path: str, value: Any) -> None:
        with self._lock:
            for target in (self._overrides, self._data):
                cur = target
                parts = path.split(".")
                for part in parts[:-1]:
                    cur = cur.setdefault(part, {})
                    if not isinstance(cur, dict):
                        raise ValueError(f"config path {path!r} collides with a value")
                cur[parts[-1]] = value
            self.save()

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._overrides, indent=2))
            tmp.replace(self.path)

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

    def __getitem__(self, path: str) -> Any:
        return self.get(path)


def ensure_dirs() -> None:
    for d in (JARVIS_DIR, MODELS_DIR, LOGS_DIR, SOUNDS_DIR):
        d.mkdir(parents=True, exist_ok=True)
