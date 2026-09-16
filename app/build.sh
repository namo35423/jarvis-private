#!/usr/bin/env bash
# Compiles the Swift sources into build/Jarvis.app.
# Needs only the Xcode Command Line Tools — no Xcode, no package manager, no SPM.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
APP="$ROOT/build/Jarvis.app"
BUNDLE_ID="${JARVIS_BUNDLE_ID:-local.jarvis.assistant}"
REPO="$(cd "$ROOT/.." && pwd)"
ARCH="$(uname -m)"
DEPLOY="14.0"

echo "==> Cleaning"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

echo "==> Writing Info.plist"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>          <string>Jarvis</string>
    <key>CFBundleIdentifier</key>          <string>${BUNDLE_ID}</string>
    <key>CFBundleName</key>                <string>Jarvis</string>
    <key>CFBundleDisplayName</key>         <string>Jarvis</string>
    <key>CFBundlePackageType</key>         <string>APPL</string>
    <key>CFBundleShortVersionString</key>  <string>1.0</string>
    <key>CFBundleVersion</key>             <string>1</string>
    <key>CFBundleIconFile</key>            <string>AppIcon</string>
    <key>LSMinimumSystemVersion</key>      <string>${DEPLOY}</string>
    <key>NSHighResolutionCapable</key>     <true/>
    <key>LSUIElement</key>                 <true/>
    <key>JarvisRoot</key>                  <string>${REPO}</string>

    <!-- The Python brain runs as this app's child, so these usage strings and the
         TCC grants that follow from them apply to the whole assistant. -->
    <key>NSMicrophoneUsageDescription</key>
    <string>Jarvis listens for your voice commands.</string>
    <key>NSAppleEventsUsageDescription</key>
    <string>Jarvis controls apps on your Mac when you ask it to.</string>

    <!-- The brain reaches these through the same TCC identity; a missing purpose
         string is a hard kill for the child process, not a polite refusal. -->
    <key>NSCalendarsUsageDescription</key>
    <string>Jarvis reads and creates events when you ask about your calendar.</string>
    <key>NSCalendarsFullAccessUsageDescription</key>
    <string>Jarvis reads and creates events when you ask about your calendar.</string>
    <key>NSRemindersUsageDescription</key>
    <string>Jarvis reads and creates reminders when you ask for them.</string>
    <key>NSRemindersFullAccessUsageDescription</key>
    <string>Jarvis reads and creates reminders when you ask for them.</string>
    <key>NSContactsUsageDescription</key>
    <string>Jarvis looks up people when you ask it to message or call someone.</string>
    <key>NSDesktopFolderUsageDescription</key>
    <string>Jarvis opens and organises files when you ask it to.</string>
    <key>NSDocumentsFolderUsageDescription</key>
    <string>Jarvis opens and organises files when you ask it to.</string>
    <key>NSDownloadsFolderUsageDescription</key>
    <string>Jarvis opens and organises files when you ask it to.</string>

    <!-- The web view talks to the brain over plain HTTP on the loopback interface. -->
    <key>NSAppTransportSecurity</key>
    <dict>
        <key>NSAllowsLocalNetworking</key> <true/>
    </dict>
</dict>
</plist>
PLIST

echo "==> Icon"
if [ ! -f "$ROOT/Resources/AppIcon.icns" ]; then
    mkdir -p "$ROOT/Resources"
    swift "$ROOT/Tools/makeicon.swift" "$ROOT/Resources/AppIcon.icns"
fi
cp "$ROOT/Resources/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"

echo "==> Compiling (${ARCH}, macOS ${DEPLOY})"
swiftc \
    -swift-version 5 \
    -O \
    -target "${ARCH}-apple-macos${DEPLOY}" \
    -framework AppKit \
    -framework WebKit \
    -framework Carbon \
    -framework AVFoundation \
    -framework ServiceManagement \
    -framework CoreGraphics \
    -framework ApplicationServices \
    -o "$APP/Contents/MacOS/Jarvis" \
    "$ROOT/Sources/Config.swift" \
    "$ROOT/Sources/HotKey.swift" \
    "$ROOT/Sources/BrainClient.swift" \
    "$ROOT/Sources/BrainSupervisor.swift" \
    "$ROOT/Sources/MenuPanel.swift" \
    "$ROOT/Sources/Permissions.swift" \
    "$ROOT/Sources/App.swift"

echo "==> Signing (ad-hoc)"
# A stable --identifier is what keeps the Microphone / Accessibility / Screen
# Recording grants attached to this app across rebuilds, as far as macOS allows.
codesign --force --sign - --identifier "$BUNDLE_ID" "$APP"

echo "==> Built $APP"
