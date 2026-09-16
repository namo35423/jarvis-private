"""Keyboard, mouse and clipboard - the last resort when nothing is scriptable.

Everything except the clipboard needs Accessibility permission; without it
macOS silently swallows synthetic events, so the check is explicit and the
error says exactly which switch to flip.
"""

from __future__ import annotations

import asyncio
import re

from jarvis.sysutil import ToolError, run
from jarvis.tools import ToolContext, tool

KEYS = {
    "return": 0x24, "enter": 0x24, "tab": 0x30, "space": 0x31, "delete": 0x33, "backspace": 0x33,
    "escape": 0x35, "esc": 0x35, "left": 0x7B, "right": 0x7C, "down": 0x7D, "up": 0x7E,
    "home": 0x73, "end": 0x77, "pageup": 0x74, "pagedown": 0x79, "forwarddelete": 0x75,
    "f1": 0x7A, "f2": 0x78, "f3": 0x63, "f4": 0x76, "f5": 0x60, "f6": 0x61, "f7": 0x62,
    "f8": 0x64, "f9": 0x65, "f10": 0x6D, "f11": 0x67, "f12": 0x6F,
    "a": 0x00, "b": 0x0B, "c": 0x08, "d": 0x02, "e": 0x0E, "f": 0x03, "g": 0x05, "h": 0x04,
    "i": 0x22, "j": 0x26, "k": 0x28, "l": 0x25, "m": 0x2E, "n": 0x2D, "o": 0x1F, "p": 0x23,
    "q": 0x0C, "r": 0x0F, "s": 0x01, "t": 0x11, "u": 0x20, "v": 0x09, "w": 0x0D, "x": 0x07,
    "y": 0x10, "z": 0x06, "0": 0x1D, "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15, "5": 0x17,
    "6": 0x16, "7": 0x1A, "8": 0x1C, "9": 0x19, ",": 0x2B, ".": 0x2F, "/": 0x2C, ";": 0x29,
    "'": 0x27, "[": 0x21, "]": 0x1E, "\\": 0x2A, "-": 0x1B, "=": 0x18, "`": 0x32,
}

DESTRUCTIVE_COMBOS = re.compile(r"(cmd\+shift\+delete|ctrl\+cmd\+q|cmd\+opt\+esc|cmd\+shift\+q)", re.I)

ACCESSIBILITY_HELP = (
    "Jarvis needs Accessibility permission to control the keyboard and mouse. "
    "Open System Settings, Privacy & Security, Accessibility, and switch Jarvis on."
)


def accessibility_ok() -> bool:
    try:
        from ApplicationServices import AXIsProcessTrusted

        return bool(AXIsProcessTrusted())
    except Exception:
        return False


def require_accessibility() -> None:
    if not accessibility_ok():
        raise ToolError(ACCESSIBILITY_HELP)


def _flags(mods: set[str]):
    import Quartz

    value = 0
    if "cmd" in mods or "command" in mods:
        value |= Quartz.kCGEventFlagMaskCommand
    if "shift" in mods:
        value |= Quartz.kCGEventFlagMaskShift
    if "opt" in mods or "alt" in mods or "option" in mods:
        value |= Quartz.kCGEventFlagMaskAlternate
    if "ctrl" in mods or "control" in mods:
        value |= Quartz.kCGEventFlagMaskControl
    if "fn" in mods:
        value |= Quartz.kCGEventFlagMaskSecondaryFn
    return value


def type_text_sync(text: str) -> None:
    import Quartz

    # Unicode strings go in whole, in small chunks: keycode-by-keycode typing
    # would break on accents, emoji and non-US layouts.
    for i in range(0, len(text), 20):
        chunk = text[i : i + 20]
        for down in (True, False):
            event = Quartz.CGEventCreateKeyboardEvent(None, 0, down)
            Quartz.CGEventKeyboardSetUnicodeString(event, len(chunk), chunk)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def press_keys_sync(combo: str) -> None:
    import Quartz

    parts = [p.strip().lower() for p in combo.replace(" ", "").split("+") if p.strip()]
    if not parts:
        raise ToolError("no keys given")
    key = parts[-1]
    mods = set(parts[:-1])
    code = KEYS.get(key)
    if code is None:
        raise ToolError(f"I don't know the key {key!r}")
    flags = _flags(mods)
    down = Quartz.CGEventCreateKeyboardEvent(None, code, True)
    up = Quartz.CGEventCreateKeyboardEvent(None, code, False)
    if flags:
        Quartz.CGEventSetFlags(down, flags)
        Quartz.CGEventSetFlags(up, flags)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)


def mouse_sync(action: str, x: float | None, y: float | None, amount: int = 3) -> None:
    import Quartz

    if x is None or y is None:
        pos = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        x, y = pos.x, pos.y
    point = Quartz.CGPointMake(float(x), float(y))

    if action == "move":
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, point, 0))
        return
    if action == "scroll":
        event = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 1, int(amount))
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        return

    button = Quartz.kCGMouseButtonRight if action == "right_click" else Quartz.kCGMouseButtonLeft
    down_type = Quartz.kCGEventRightMouseDown if action == "right_click" else Quartz.kCGEventLeftMouseDown
    up_type = Quartz.kCGEventRightMouseUp if action == "right_click" else Quartz.kCGEventLeftMouseUp
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, point, button))
    clicks = 2 if action == "double_click" else 1
    for click in range(1, clicks + 1):
        down = Quartz.CGEventCreateMouseEvent(None, down_type, point, button)
        up = Quartz.CGEventCreateMouseEvent(None, up_type, point, button)
        Quartz.CGEventSetIntegerValueField(down, Quartz.kCGMouseEventClickState, click)
        Quartz.CGEventSetIntegerValueField(up, Quartz.kCGMouseEventClickState, click)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)


async def click_at(x: float, y: float) -> None:
    """Used by screen_look's click_text."""
    require_accessibility()
    await asyncio.get_running_loop().run_in_executor(None, mouse_sync, "click", x, y, 0)


def _confirm_keys(args: dict) -> str | None:
    if (args.get("action") or "") != "key":
        return None
    keys = args.get("keys") or ""
    return f"Press {keys}?" if DESTRUCTIVE_COMBOS.search(keys) else None


@tool(
    name="computer_input",
    description=(
        "Control the keyboard and mouse directly, and read or set the clipboard. Use for typing into an "
        "app that has no other way in, pressing a keyboard shortcut, clicking a point, or copying text. "
        "Prefer chrome_act for web pages and screen_look click_text for clicking something by its label."
    ),
    params={
        "action": {"type": "string", "enum": ["type", "key", "click", "double_click", "right_click", "move", "scroll", "clipboard_read", "clipboard_write"]},
        "text": {"type": "string", "description": "Text to type, or to put on the clipboard"},
        "keys": {"type": "string", "description": "A shortcut like 'cmd+s', 'cmd+shift+4', 'enter', 'escape'"},
        "x": {"type": "number", "description": "Screen x for mouse actions"},
        "y": {"type": "number", "description": "Screen y for mouse actions"},
        "amount": {"type": "integer", "description": "Scroll amount; negative scrolls down"},
    },
    required=["action"],
    risk=_confirm_keys,
    category="input",
)
async def computer_input(
    ctx: ToolContext,
    action: str,
    text: str = "",
    keys: str = "",
    x: float | None = None,
    y: float | None = None,
    amount: int = 3,
) -> str:
    action = action.lower()
    loop = asyncio.get_running_loop()

    if action == "clipboard_read":
        proc = await run(["/usr/bin/pbpaste"], timeout=10)
        content = proc.stdout.strip()
        return f"The clipboard holds: {content[:1500]}" if content else "The clipboard is empty."

    if action == "clipboard_write":
        if not text:
            raise ToolError("nothing to copy")
        proc = await run(["/usr/bin/pbcopy"], timeout=10, stdin=text)
        proc.check("pbcopy")
        return "Copied to the clipboard."

    require_accessibility()

    if action == "type":
        if not text:
            raise ToolError("nothing to type")
        await loop.run_in_executor(None, type_text_sync, text)
        return f"Typed {len(text)} characters."

    if action == "key":
        if not keys:
            raise ToolError("no shortcut given")
        await loop.run_in_executor(None, press_keys_sync, keys)
        return f"Pressed {keys}."

    if action in ("click", "double_click", "right_click", "move", "scroll"):
        await loop.run_in_executor(None, mouse_sync, action, x, y, amount)
        where = f" at {int(x)}, {int(y)}" if x is not None and y is not None else ""
        return f"{action.replace('_', ' ').title()}{where}."

    raise ToolError(f"unknown action {action!r}")
