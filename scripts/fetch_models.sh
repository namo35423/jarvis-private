#!/usr/bin/env bash
# Fetch trusted model artifacts before running Jarvis offline.
# --english-fallback also installs the original Parakeet and Kokoro models.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/brain/.venv/bin/python"
export PYTHONPATH="$ROOT/brain"
export HF_HUB_DISABLE_TELEMETRY=1
export JARVIS_FETCH_ENGLISH="${1:-}"
unset HF_HUB_OFFLINE
"$PY" - <<'PY'
import os
from huggingface_hub import snapshot_download
from jarvis.config import Config
import openwakeword.utils
openwakeword.utils.download_models(['hey_jarvis'])
cfg=Config()
models=[cfg.get('stt.model')]
if os.environ.get('JARVIS_FETCH_ENGLISH')=='--english-fallback':
 models.append('mlx-community/parakeet-tdt-0.6b-v3')
for model in dict.fromkeys(models):
 print('Fetching',model,flush=True)
 snapshot_download(model)
PY
ENGINE="$("$PY" -c 'from jarvis.config import Config; print(Config().get("voice.engine"))')"
if [ "$ENGINE" = "kokoro" ] || [ "${1:-}" = "--english-fallback" ]; then
    MODELS="$HOME/.jarvis/models"
    mkdir -p "$MODELS"
    for name in kokoro-v1.0.onnx voices-v1.0.bin; do
        if [ ! -s "$MODELS/$name" ]; then
            curl -fL --retry 5 --retry-all-errors -C - -o "$MODELS/$name.partial" \
                "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/$name"
            mv "$MODELS/$name.partial" "$MODELS/$name"
        fi
    done
fi
