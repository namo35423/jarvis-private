#!/usr/bin/env bash
# One-command install for Jarvis.
#
#   git clone https://github.com/BatmanOnTop/jarvis.git && cd jarvis && ./setup.sh
#
# Safe to re-run: every step skips work that is already done.
# Flags:
#   --no-install   build everything but don't copy the app to ~/Applications or launch it
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
BRAIN="$ROOT/brain"
MODEL="qwen3:4b-instruct"
INSTALL=1
[ "${1:-}" = "--no-install" ] && INSTALL=0

bold() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[31mError:\033[0m %s\n' "$*" >&2; exit 1; }

bold "Checking this Mac"
[ "$(uname -s)" = "Darwin" ] || fail "Jarvis runs on macOS only."
[ "$(uname -m)" = "arm64" ] || fail "Jarvis needs an Apple Silicon Mac (M1 or newer): speech recognition runs on MLX."
major="$(sw_vers -productVersion | cut -d. -f1)"
[ "$major" -ge 14 ] || fail "Jarvis needs macOS 14 Sonoma or newer (you have $(sw_vers -productVersion))."
if ! xcode-select -p >/dev/null 2>&1; then
    xcode-select --install || true
    fail "Install the Xcode Command Line Tools from the dialog that just opened, then run ./setup.sh again."
fi
echo "macOS $(sw_vers -productVersion), $(sysctl -n hw.memsize | awk '{printf "%d GB", $1/1073741824}') memory"

bold "Ollama (runs the language model)"
if ! command -v ollama >/dev/null 2>&1 && [ ! -d /Applications/Ollama.app ]; then
    open "https://ollama.com/download" || true
    fail "Install Ollama from https://ollama.com/download (it just opened), start it once, then run ./setup.sh again."
fi
OLLAMA="$(command -v ollama || echo /Applications/Ollama.app/Contents/Resources/ollama)"
if ! curl -s --max-time 2 http://127.0.0.1:11434/api/version >/dev/null; then
    open -ga Ollama || true
    for _ in $(seq 1 30); do curl -s --max-time 2 http://127.0.0.1:11434/api/version >/dev/null && break; sleep 1; done
fi
curl -s --max-time 2 http://127.0.0.1:11434/api/version >/dev/null || fail "Ollama is installed but not running. Open the Ollama app, then run ./setup.sh again."
"$OLLAMA" pull "$MODEL"

bold "Python environment"
if ! command -v uv >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/uv" ]; then
    echo "Installing uv (Python package manager, from astral.sh)"
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
[ -x "$BRAIN/.venv/bin/python" ] || "$UV" venv --python 3.12 "$BRAIN/.venv"
"$UV" pip install --python "$BRAIN/.venv/bin/python" -r "$BRAIN/requirements.txt"

bold "Voice and wake-word models"
bash "$ROOT/scripts/fetch_models.sh"

bold "Checking the brain loads"
bash "$BRAIN/verify.sh"

if [ ! -s "$HOME/.jarvis/config.json" ] && [ -t 0 ]; then
    printf '\nWhat should Jarvis call you? (press Enter to skip) '
    read -r NAME || NAME=""
    if [ -n "$NAME" ]; then
        mkdir -p "$HOME/.jarvis"
        NAME="$NAME" /usr/bin/python3 -c 'import json, os; print(json.dumps({"user_name": os.environ["NAME"]}, indent=2))' > "$HOME/.jarvis/config.json"
    fi
fi

bold "Command-line tool"
mkdir -p "$HOME/.local/bin"
ln -sf "$ROOT/bin/jarvis" "$HOME/.local/bin/jarvis"
case ":$PATH:" in
    *":$HOME/.local/bin:"*) echo "jarvis is on your PATH" ;;
    *) echo "Add this to your shell profile to use the jarvis command:  export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

if [ "$INSTALL" = 1 ]; then
    bold "Building and installing Jarvis.app"
    bash "$ROOT/app/install.sh"
    cat <<'DONE'

Jarvis is in your menu bar.

  1. Allow Microphone access when macOS asks.
  2. Say "Hey Jarvis", or press Command-Backslash.
  3. Optional: run `jarvis grant` to answer every app-control permission prompt at once.

DONE
else
    bold "Building Jarvis.app (not installing)"
    bash "$ROOT/app/build.sh"
    echo "Built $ROOT/app/build/Jarvis.app"
fi
