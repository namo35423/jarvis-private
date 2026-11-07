"""Wayu-Paxa voice worker. Inference and phonemization stay on this computer."""
import contextlib
import json
import os
from pathlib import Path
import sys
import socket
import threading
import time
import traceback

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"


def no_network(*args, **kwargs):
    raise RuntimeError("Network access is disabled in the local voice worker")


# This process talks to Jarvis only through stdin/stdout. Missing assets must
# fail clearly instead of being downloaded during a private conversation.
socket.socket.connect = no_network
socket.socket.connect_ex = no_network
socket.create_connection = no_network


def parent_watchdog():
    parent = os.getppid()
    while True:
        time.sleep(2)
        if os.getppid() != parent:
            os._exit(0)


def reply(value):
    sys.stdout.write(json.dumps(value, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    threading.Thread(target=parent_watchdog, daemon=True).start()
    with contextlib.redirect_stdout(sys.stderr):
        import numpy as np
        import torch
        from wayu_tts import ThaiTTS
        from wayu_tts.voices import Voice
        import spacy
        if not spacy.util.is_package("en_core_web_sm"):
            raise RuntimeError("Offline English pronunciation data is missing; install en_core_web_sm")
        torch.set_num_threads(4)
        model_dir = Path(sys.argv[1]).expanduser()
        if not model_dir.is_dir() or not (model_dir / "model.pth").is_file():
            raise FileNotFoundError("Local Wayu voice model is missing")
        model = ThaiTTS.from_pretrained(model_dir, device="cpu")
        young = model.load_voice("m_young_clear")
        warm = model.load_voice("m_mid_warm")
        # Keep the younger speaker's rhythm; add only a little warmth to timbre.
        smooth_style = young.styles.clone()
        smooth_style[..., :128] = 0.85 * young.styles[..., :128] + 0.15 * warm.styles[..., :128]
        smooth = Voice("m_young_clear", smooth_style)  # preserve calibrated rate
        voices = model.voices + ["m_jarvis_smooth"]
        # Prime dictionaries and CPU kernels before reporting ready.
        model("พร้อมครับ", voice="m_mid_warm", seed=42)
        model.phonemize("Jarvis Google Chrome YouTube Music")
    reply({"ready": True, "voices": voices})
    for line in sys.stdin:
        try:
            request = json.loads(line)
            text = request["text"].strip().replace("จาร์วิส", "Jarvis")
            if not text or len(text) > 4000:
                raise ValueError("Speech text must contain 1–4000 characters")
            voice = request.get("voice", "m_mid_warm")
            if voice not in voices:
                raise ValueError(f"Unknown voice: {voice}")
            speed = max(0.7, min(1.5, float(request.get("speed", 1.0))))
            start = time.monotonic()
            with contextlib.redirect_stdout(sys.stderr):
                audio = model(text, voice=smooth if voice == "m_jarvis_smooth" else voice,
                              speed=speed, seed=42)
                if len(audio) < 2400 or not np.isfinite(audio).all():
                    raise RuntimeError("Voice returned empty or invalid audio")
                model.save(request["output"], np.clip(audio, -1, 1))
            reply({"ok": True, "seconds": time.monotonic() - start})
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            reply({"error": str(exc)})


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        reply({"error": str(exc)})
        sys.exit(1)
