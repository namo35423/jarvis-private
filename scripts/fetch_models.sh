#!/usr/bin/env bash
# Downloads every local model Jarvis needs. Safe to re-run: each step is skipped
# if the file is already there, and partial downloads resume.
set -uo pipefail

BRAIN="$(cd "$(dirname "$0")/../brain" && pwd)"
PY="$BRAIN/.venv/bin/python"
MODELS="$HOME/.jarvis/models"
KOKORO_REL="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"

mkdir -p "$MODELS"

echo "==> Wake word (openWakeWord: hey_jarvis + melspectrogram + embeddings + silero VAD)"
"$PY" - <<'PY'
import openwakeword.utils as u
try:
    u.download_models(["hey_jarvis"])
    print("wake word models ready")
except Exception as e:
    print("WAKE_WORD_FAILED:", type(e).__name__, e)
PY

echo "==> Kokoro TTS voice model"
for f in kokoro-v1.0.onnx voices-v1.0.bin; do
    if [ -s "$MODELS/$f" ]; then
        echo "    $f already present ($(du -h "$MODELS/$f" | cut -f1))"
    else
        echo "    fetching $f"
        curl -fL --retry 3 --retry-delay 2 -C - -o "$MODELS/$f" "$KOKORO_REL/$f" || echo "KOKORO_FAILED: $f"
    fi
done

echo "==> Speech recognition (Parakeet, from Hugging Face)"
"$PY" - <<'PY'
try:
    from parakeet_mlx import from_pretrained
    from_pretrained("mlx-community/parakeet-tdt-0.6b-v3")
    print("parakeet ready")
except Exception as e:
    print("PARAKEET_FAILED:", type(e).__name__, e)
PY

echo "==> Done"
ls -la "$MODELS" 2>/dev/null
