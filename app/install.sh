#!/usr/bin/env bash
# Build, then install to ~/Applications and launch.
#
# The install path is fixed on purpose: macOS ties Microphone, Accessibility,
# Screen Recording and Automation grants to the bundle's path and signature, so
# an app that keeps moving gets re-prompted for everything every time.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/Applications/Jarvis.app"

"$ROOT/build.sh"

echo "==> Stopping any running copy"
pkill -f 'Jarvis.app/Contents/MacOS/Jarvis' 2>/dev/null || true
# The brain is a child of the app; make sure it goes too, or it keeps the mic.
pkill -f 'brain/.venv/bin/python -m jarvis' 2>/dev/null || true
sleep 1

echo "==> Installing to $DEST"
rm -rf "$DEST"
mkdir -p "$HOME/Applications"
cp -R "$ROOT/build/Jarvis.app" "$DEST"

echo "==> Launching"
# LaunchServices can still be holding the just-replaced bundle's registration,
# in which case the first open is accepted and quietly does nothing. Verify.
sleep 2
for attempt in 1 2 3; do
    open "$DEST" 2>/dev/null || true
    sleep 3
    if pgrep -f 'Jarvis.app/Contents/MacOS/Jarvis' > /dev/null; then
        break
    fi
    echo "    launch attempt $attempt did not take, retrying"
    /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
        -f "$DEST" 2>/dev/null || true
done

if ! pgrep -f 'Jarvis.app/Contents/MacOS/Jarvis' > /dev/null; then
    echo "!! Jarvis did not start. Open it from ~/Applications in Finder."
    exit 1
fi
echo
echo "Jarvis is starting. First launch will ask for Microphone access - say yes."
echo "Talk to it by saying 'Hey Jarvis', or press Command-Backslash."
