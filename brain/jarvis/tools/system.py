"""Apps and the Mac's own state: launching, quitting, volume, brightness, power.

Everything here prefers an API that needs no macOS permission over one that does,
because Jarvis has to work on a machine where the user has not yet clicked through
the Accessibility and Automation prompts. So: NSWorkspace instead of System Events
for "is it running", DisplayServices instead of brightness key codes, pmset and
networksetup instead of UI scripting. AppleScript is used only where there is no
alternative (quitting an app, appearance, restart/shutdown).
"""

from __future__ import annotations

import asyncio
import ctypes
import re
from pathlib import Path

from jarvis.sysutil import (
    ToolError,
    app_path,
    applescript_quote,
    find_app,
    osascript,
    run,
)
from jarvis.tools import tool

# ---------------------------------------------------------------- app state

try:  # pyobjc-framework-Cocoa ships with Quartz, but never let an import kill tool loading
    from AppKit import NSWorkspace as _NSWorkspace
except Exception:  # noqa: BLE001
    _NSWorkspace = None


_APP_EXE = re.compile(r"/([^/]+)\.app/Contents/MacOS/")


def running_apps() -> list[tuple[str, str]]:
    """(app name, "") for every running app, read fresh from the process table.

    Not NSWorkspace: without a Cocoa run loop its runningApplications list is a
    snapshot from the first call and never updates, so anything launched after
    the brain started looked "not running" - which is why "close Spotify" failed
    on a Spotify that was plainly open.
    """
    import subprocess

    try:
        out = subprocess.run(["/bin/ps", "-axo", "comm="], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    seen: dict[str, str] = {}
    for line in out.splitlines():
        m = _APP_EXE.search(line)
        if m:
            seen.setdefault(m.group(1), "")
    return list(seen.items())


def is_running(app_name: str) -> bool:
    """Is this app running? Matches the name Finder shows, case-insensitively."""
    if not app_name:
        return False
    want = app_name.lower()
    for name, bid in running_apps():
        if name.lower() == want or bid.lower().endswith("." + want.replace(" ", "")):
            return True
    return False


def resolve_app(name: str) -> str:
    """Fuzzy-match a spoken app name or raise a ToolError naming near misses."""
    app = find_app(name)
    if app:
        return app
    from jarvis.sysutil import _scan_apps

    installed = sorted(_scan_apps())
    import difflib

    close = difflib.get_close_matches(name, installed, n=3, cutoff=0.4)
    if not close:
        close = [a for a in installed if name.lower()[:3] in a.lower()][:3]
    hint = f" Closest installed apps: {', '.join(close)}." if close else ""
    raise ToolError(f"No installed app matches {name!r}.{hint}")


async def _wait_gone(app: str, seconds: float = 4.0) -> bool:
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        if not is_running(app):
            return True
        await asyncio.sleep(0.25)
    return not is_running(app)


def _automation_hint(app: str, exc: ToolError) -> ToolError:
    msg = str(exc)
    if "-1743" in msg or "not authorized" in msg.lower() or "not allowed" in msg.lower():
        return ToolError(
            f"macOS has not granted Jarvis permission to control {app}. "
            "Ask the user to allow it under System Settings, Privacy & Security, Automation."
        )
    return exc


# -------------------------------------------------------------------- tools


@tool(
    name="open_app",
    description=(
        "Launch a Mac application, or bring it to the front if it is already running. "
        "Use for any 'open X', 'launch X', 'switch to X' request where X is an app."
    ),
    params={
        "name": {
            "type": "string",
            "description": "App name as a person would say it, e.g. Spotify, Chrome, Notes",
        }
    },
    required=["name"],
    ack="Opening {name}.",
    category="system",
)
async def open_app(ctx, name: str) -> str:
    app = resolve_app(name)
    already = is_running(app)
    path = app_path(app) or app
    proc = await run(["/usr/bin/open", "-a", path], timeout=20)
    if not proc.ok:
        raise ToolError(f"Could not open {app}: {(proc.stderr or proc.stdout).strip()[:120]}")
    return f"{app} is already open." if already else f"{app} is open."


@tool(
    name="quit_app",
    description=(
        "Quit a running Mac application. Use for 'close X', 'quit X', 'shut down X' "
        "when X is an app, not the Mac itself."
    ),
    params={"name": {"type": "string", "description": "App name, e.g. Spotify"}},
    required=["name"],
    ack="Closing {name}.",
    category="system",
)
async def quit_app(ctx, name: str) -> str:
    app = resolve_app(name)
    if not is_running(app):
        return f"{app} was not running."
    try:
        await osascript(f"tell application {applescript_quote(app)} to quit", timeout=20)
    except ToolError as exc:
        raise _automation_hint(app, exc) from exc
    if await _wait_gone(app):
        return f"{app} is closed."
    return f"{app} was asked to quit but is still open, probably showing an unsaved-changes dialog."


# ------------------------------------------------------------------ volume

_VOL = re.compile(r"output volume:(\d+).*?output muted:(true|false)", re.S)


async def _volume() -> tuple[int, bool]:
    out = await osascript("get volume settings", timeout=10)
    m = _VOL.search(out)
    if not m:
        raise ToolError(f"could not read the volume (got {out[:60]!r})")
    return int(m.group(1)), m.group(2) == "true"


async def _set_volume(level: int) -> int:
    level = max(0, min(100, int(level)))
    script = f"set volume output volume {level}"
    if level > 0:
        script += "\nset volume without output muted"
    await osascript(script, timeout=10)
    actual, muted = await _volume()
    if abs(actual - level) > 1 or (level > 0 and muted):
        raise ToolError(f"Volume did not change to {level}; device reports {actual}.")
    return actual


# -------------------------------------------------------------- brightness

_DS_PATH = "/System/Library/PrivateFrameworks/DisplayServices.framework/DisplayServices"
_ds_lib: ctypes.CDLL | None = None
_ds_tried = False


def _display_services() -> ctypes.CDLL | None:
    """DisplayServices is the only brightness API that works on Apple silicon.

    The old trick of sending key codes 144/145 through System Events is silently
    dropped on this machine (macOS 26 / M3): no error, no change.
    """
    global _ds_lib, _ds_tried
    if _ds_tried:
        return _ds_lib
    _ds_tried = True
    try:
        lib = ctypes.CDLL(_DS_PATH)
        lib.DisplayServicesGetBrightness.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_float)]
        lib.DisplayServicesGetBrightness.restype = ctypes.c_int
        lib.DisplayServicesSetBrightness.argtypes = [ctypes.c_uint32, ctypes.c_float]
        lib.DisplayServicesSetBrightness.restype = ctypes.c_int
        _ds_lib = lib
    except (OSError, AttributeError):
        _ds_lib = None
    return _ds_lib


def _main_display() -> int:
    from Quartz import CGMainDisplayID

    return int(CGMainDisplayID())


_IOREG_BRIGHT = re.compile(r'"brightness"=\{[^}]*?"max"=(\d+)[^}]*?"value"=(\d+)')


async def _ioreg_brightness() -> float | None:
    """Fallback brightness reading, used only to verify the key-code path."""
    proc = await run(["/usr/sbin/ioreg", "-rc", "AppleARMBacklight", "-d", "1"], timeout=10)
    if not proc.ok:
        return None
    m = _IOREG_BRIGHT.search(proc.stdout)
    if not m:
        # Key order is not guaranteed; try the other arrangement.
        m2 = re.search(r'"brightness"=\{[^}]*?"value"=(\d+)[^}]*?"max"=(\d+)', proc.stdout)
        if not m2:
            return None
        value, maximum = int(m2.group(1)), int(m2.group(2))
    else:
        maximum, value = int(m.group(1)), int(m.group(2))
    return value / maximum if maximum else None


async def _nudge_brightness(delta: float) -> str:
    lib = _display_services()
    if lib is not None:
        did = _main_display()
        cur = ctypes.c_float()
        if lib.DisplayServicesGetBrightness(did, ctypes.byref(cur)) == 0:
            target = max(0.0, min(1.0, cur.value + delta))
            if lib.DisplayServicesSetBrightness(did, ctypes.c_float(target)) == 0:
                check = ctypes.c_float()
                lib.DisplayServicesGetBrightness(did, ctypes.byref(check))
                return f"Brightness {round(check.value * 100)} percent."
    before = await _ioreg_brightness()
    await osascript(
        f"tell application \"System Events\" to key code {144 if delta > 0 else 145}", timeout=10
    )
    await asyncio.sleep(0.4)
    after = await _ioreg_brightness()
    if before is not None and after is not None and abs(after - before) > 0.001:
        return f"Brightness {round(after * 100)} percent."
    raise ToolError(
        "Brightness could not be changed: the DisplayServices API is unavailable and the "
        "brightness key codes do nothing on this Mac. Ask the user to use the keyboard keys."
    )


# ------------------------------------------------------------------- power


def _lock_screen_now() -> bool:
    try:
        lib = ctypes.CDLL("/System/Library/PrivateFrameworks/login.framework/Versions/Current/login")
        return lib.SACLockScreenImmediate() == 0
    except (OSError, AttributeError):
        return False


_BATT = re.compile(r"(\d+)%;\s*([^;]+);\s*([^\s]+)")


def _hhmm(text: str) -> str:
    m = re.match(r"(\d+):(\d\d)", text)
    if not m:
        return ""
    h, mnt = int(m.group(1)), int(m.group(2))
    parts = []
    if h:
        parts.append(f"{h} hour{'s' if h != 1 else ''}")
    if mnt:
        parts.append(f"{mnt} minute{'s' if mnt != 1 else ''}")
    return " ".join(parts)


async def _battery() -> str:
    proc = await run(["/usr/bin/pmset", "-g", "batt"], timeout=10)
    text = proc.check("pmset")
    m = _BATT.search(text)
    if not m:
        return "No battery information available."
    pct, state, remaining = m.group(1), m.group(2).strip(), m.group(3).strip()
    source = "plugged in" if "AC Power" in text else "on battery"
    span = _hhmm(remaining)
    if state == "charging" and span:
        return f"Battery {pct} percent, charging, {span} to full."
    if state == "discharging" and span:
        return f"Battery {pct} percent, {span} remaining."
    if state == "charged":
        return f"Battery {pct} percent, fully charged and {source}."
    return f"Battery {pct} percent, {state}, {source}."


# --------------------------------------------------------------------- wifi

_PORT = re.compile(r"Hardware Port:\s*Wi-Fi\s*\nDevice:\s*(\S+)", re.I)


async def _wifi_device() -> str:
    proc = await run(["/usr/sbin/networksetup", "-listallhardwareports"], timeout=15)
    m = _PORT.search(proc.check("networksetup"))
    if not m:
        raise ToolError("This Mac has no Wi-Fi interface.")
    return m.group(1)


async def _wifi_status() -> str:
    dev = await _wifi_device()
    proc = await run(["/usr/sbin/networksetup", "-getairportpower", dev], timeout=15)
    on = "On" in proc.check("networksetup")
    if not on:
        return "Wi-Fi is off."
    net = await run(["/usr/sbin/networksetup", "-getairportnetwork", dev], timeout=15)
    line = (net.stdout or "").strip()
    if ":" in line and "not associated" not in line.lower():
        return f"Wi-Fi is on, connected to {line.split(':', 1)[1].strip()}."
    return "Wi-Fi is on but not connected to a network."


_ACTIONS = [
    "volume_set",
    "volume_up",
    "volume_down",
    "mute",
    "unmute",
    "volume_status",
    "brightness_up",
    "brightness_down",
    "dark_mode",
    "light_mode",
    "toggle_appearance",
    "lock_screen",
    "sleep_display",
    "battery",
    "wifi_on",
    "wifi_off",
    "wifi_status",
    "sleep",
    "restart",
    "shutdown",
    "logout",
]

_POWER_PROMPTS = {
    "sleep": "Put the Mac to sleep?",
    "restart": "Restart the Mac?",
    "shutdown": "Shut the Mac down?",
    "logout": "Log out of this account?",
}


def _system_risk(args: dict) -> str | None:
    return _POWER_PROMPTS.get(str(args.get("action", "")).strip().lower()) or ("เปลี่ยนการเชื่อมต่อ Wi-Fi ไหมครับ" if args.get("action") in {"wifi_on", "wifi_off"} else None)


@tool(
    name="system_control",
    description=(
        "Control the Mac itself: volume, screen brightness, dark or light appearance, "
        "Wi-Fi, battery level, locking the screen, sleep, restart, shutdown, log out. "
        "Use this for anything about the computer's own state rather than an app or a file."
    ),
    params={
        "action": {
            "type": "string",
            "enum": _ACTIONS,
            "description": "What to do, e.g. volume_set, mute, brightness_up, dark_mode, battery",
        },
        "value": {
            "type": "integer",
            "description": "Only for volume_set: the level from 0 to 100, e.g. 40",
        },
    },
    required=["action"],
    risk=_system_risk,
    category="system",
)
async def system_control(ctx, action: str, value: int | None = None) -> str:
    action = (action or "").strip().lower()

    if action == "volume_set":
        if value is None:
            raise ToolError("volume_set needs a value from 0 to 100.")
        level = await _set_volume(int(value))
        return f"Volume {level} percent."

    if action in ("volume_up", "volume_down"):
        level, muted = await _volume()
        if muted and action == "volume_up":
            level = 0
        level = await _set_volume(level + (10 if action == "volume_up" else -10))
        return f"Volume {level} percent."

    if action in ("mute", "unmute"):
        await osascript(f"set volume {'with' if action == 'mute' else 'without'} output muted", timeout=10)
        return "Muted." if action == "mute" else "Unmuted."

    if action == "volume_status":
        level, muted = await _volume()
        return f"Volume is {level} percent{' but muted' if muted else ''}."

    if action in ("brightness_up", "brightness_down"):
        return await _nudge_brightness(0.1 if action == "brightness_up" else -0.1)

    if action in ("dark_mode", "light_mode", "toggle_appearance"):
        prefix = 'tell application "System Events" to tell appearance preferences'
        if action == "toggle_appearance":
            await osascript(f"{prefix} to set dark mode to not dark mode", timeout=15)
        else:
            want = "true" if action == "dark_mode" else "false"
            await osascript(f"{prefix} to set dark mode to {want}", timeout=15)
        now = await osascript(f"{prefix} to get dark mode", timeout=15)
        return "Dark mode is on." if now.strip() == "true" else "Light mode is on."

    if action == "lock_screen":
        if _lock_screen_now():
            return "Screen locked."
        # Cmd-Ctrl-Q is the system lock shortcut; needs Accessibility.
        try:
            await osascript(
                'tell application "System Events" to keystroke "q" using {command down, control down}',
                timeout=10,
            )
            return "Screen locked."
        except ToolError as exc:
            raise ToolError(
                "Could not lock the screen. macOS may not have granted Jarvis Accessibility "
                f"access: {exc}"
            ) from exc

    if action == "sleep_display":
        (await run(["/usr/bin/pmset", "displaysleepnow"], timeout=10)).check("pmset")
        return "Display asleep."

    if action == "battery":
        return await _battery()

    if action in ("wifi_on", "wifi_off"):
        dev = await _wifi_device()
        want = "on" if action == "wifi_on" else "off"
        proc = await run(["/usr/sbin/networksetup", "-setairportpower", dev, want], timeout=20)
        if not proc.ok:
            raise ToolError(f"Could not turn Wi-Fi {want}: {(proc.stderr or proc.stdout).strip()[:120]}")
        return f"Wi-Fi is {want}."

    if action == "wifi_status":
        return await _wifi_status()

    if action == "sleep":
        (await run(["/usr/bin/pmset", "sleepnow"], timeout=10)).check("pmset")
        return "Going to sleep."

    if action in ("restart", "shutdown", "logout"):
        verb = {"restart": "restart", "shutdown": "shut down", "logout": "log out"}[action]
        try:
            await osascript(f'tell application "System Events" to {verb}', timeout=15)
        except ToolError as exc:
            raise _automation_hint("System Events", exc) from exc
        return {"restart": "Restarting.", "shutdown": "Shutting down.", "logout": "Logging out."}[action]

    raise ToolError(f"Unknown action {action!r}. Valid actions: {', '.join(_ACTIONS)}")


@tool(
    name="notify",
    description=(
        "Show a macOS notification banner. Use when the user asks to be notified or "
        "reminded on screen right now; for a reminder at a future time use the reminders tool."
    ),
    params={
        "title": {"type": "string", "description": "Short heading, e.g. Build finished"},
        "message": {"type": "string", "description": "Body line, e.g. All 42 tests passed"},
    },
    required=["title", "message"],
    category="system",
)
async def notify(ctx, title: str, message: str) -> str:
    await osascript(
        f"display notification {applescript_quote(message)} with title {applescript_quote(title)}",
        timeout=15,
    )
    return "Notification shown."


__all__ = ["is_running", "running_apps", "resolve_app"]


def _unused_path_guard() -> Path:  # pragma: no cover - keeps Path import honest
    return Path.home()
