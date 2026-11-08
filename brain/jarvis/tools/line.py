"""Send a LINE message through the LINE for Mac app.

LINE has no AppleScript dictionary, so this drives its window through the
Accessibility API (the same grant computer_input uses): find the chat in the
chat list by its exact name, open it, check the chat header shows that name,
put the text in the message box, press Return, and check the box emptied.
Anything it cannot verify stops before sending and says why.
"""
from __future__ import annotations

import asyncio
import logging
import time

from jarvis.sysutil import ToolError, run
from jarvis.tools import ToolContext, tool

log = logging.getLogger("jarvis.tools.line")
BUNDLE = "jp.naver.line.mac"
RETURN_KEY = 0x24


class _UI:
    """Synchronous Accessibility helpers. Always called off the event loop."""

    def __init__(self) -> None:
        import ApplicationServices as AS
        from AppKit import NSRunningApplication

        self.AS = AS
        apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(BUNDLE)
        if not apps:
            raise ToolError("LINE ไม่ได้เปิดอยู่ครับ")
        self.running = apps[0]
        self.pid = self.running.processIdentifier()
        self.app = AS.AXUIElementCreateApplication(self.pid)
        AS.AXUIElementSetMessagingTimeout(self.app, 3.0)

    def get(self, el, attr):
        err, val = self.AS.AXUIElementCopyAttributeValue(el, attr, None)
        return val if err == 0 else None

    def kids(self, el) -> list:
        return list(self.get(el, "AXChildren") or [])

    def role(self, el) -> str:
        return str(self.get(el, "AXRole") or "")

    def title(self, el) -> str:
        return str(self.get(el, "AXTitle") or self.get(el, "AXDescription") or "").strip()

    def main_parts(self):
        """(chat list, top split group) of the main LINE window, or (None, None)."""
        for window in self.get(self.app, "AXWindows") or []:
            for group in self.kids(window):
                if self.role(group) != "AXSplitGroup":
                    continue
                lists = [k for k in self.kids(group) if self.role(k) == "AXList"]
                if lists:
                    return lists[0], group
        return None, None

    def rows(self, chat_list) -> list[tuple[str, object]]:
        """(row title, pressable element) for every chat in the list."""
        out = []
        for row in self.kids(chat_list):
            target = next(iter(self.kids(row)), None) or row
            label = self.title(target) or self.title(row)
            if label:
                out.append((label, target))
        return out

    def header(self, group, name: str) -> bool:
        """Is the chat open on the right titled `name`? Header buttons sit directly in the split group."""
        want = name.casefold()
        for el in self.kids(group):
            shown = self.title(el).casefold()
            if self.role(el) == "AXButton" and (shown == want or shown.startswith(want + " (")):
                return True
        return False

    def message_box(self, group):
        """The message text area: the first AXTextArea outside the lists."""
        stack = list(reversed(self.kids(group)))
        while stack:
            el = stack.pop()
            role = self.role(el)
            if role == "AXTextArea":
                return el
            if role != "AXList":
                stack.extend(reversed(self.kids(el)))
        return None

    def value(self, el) -> str:
        return str(self.get(el, "AXValue") or "")

    def press(self, el) -> bool:
        return self.AS.AXUIElementPerformAction(el, "AXPress") == 0

    def set(self, el, attr, value) -> bool:
        return self.AS.AXUIElementSetAttributeValue(el, attr, value) == 0


def match_chats(labels: list[str], name: str) -> tuple[list[str], list[str]]:
    """(exact, partial) chat-list labels for `name`. A label is 'Chat name, preview, date'."""
    want = " ".join(name.split()).casefold()
    exact, partial = [], []
    for label in labels:
        chat = label.split(", ")[0].strip()
        if chat.casefold() == want or label.casefold() == want:
            exact.append(label)
        elif want and want in chat.casefold():
            partial.append(chat)
    return exact, partial


def _wait(predicate, timeout: float, step: float = 0.15):
    end = time.monotonic() + timeout
    while True:
        value = predicate()
        if value or time.monotonic() >= end:
            return value
        time.sleep(step)


def _press_return(pid: int) -> None:
    import Quartz

    for down in (True, False):
        event = Quartz.CGEventCreateKeyboardEvent(None, RETURN_KEY, down)
        Quartz.CGEventPostToPid(pid, event)
        time.sleep(0.03)


def _paste(text: str) -> None:
    """Fallback when the text area refuses AXValue: paste, then put the clipboard back."""
    import Quartz
    from AppKit import NSPasteboard, NSPasteboardTypeString

    board = NSPasteboard.generalPasteboard()
    saved = board.stringForType_(NSPasteboardTypeString)
    board.clearContents()
    board.setString_forType_(text, NSPasteboardTypeString)
    for down in (True, False):
        event = Quartz.CGEventCreateKeyboardEvent(None, 0x09, down)  # V
        Quartz.CGEventSetFlags(event, Quartz.kCGEventFlagMaskCommand)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(0.03)
    time.sleep(0.3)
    board.clearContents()
    if saved is not None:
        board.setString_forType_(saved, NSPasteboardTypeString)


def _settled_rows(ui, timeout: float = 5.0):
    """(chat list, split group, rows) once LINE has finished building the list.

    LINE builds its accessibility tree lazily: right after it is activated only a
    few rows have names. Wait until the named-row count stops growing.
    """
    end = time.monotonic() + timeout
    last, stable = -1, 0
    chat_list = group = None
    rows: list = []
    while time.monotonic() < end:
        chat_list, group = ui.main_parts()
        if chat_list is not None:
            rows = ui.rows(chat_list)
            stable = stable + 1 if len(rows) == last and rows else 0
            last = len(rows)
            if stable >= 2:
                break
        time.sleep(0.3)
    return chat_list, group, rows


def _search_field(ui, group):
    return next((k for k in ui.kids(group) if ui.role(k) == "AXTextField"), None)


def find_chat(ui, recipient: str):
    """(row element, chat name, search field we filled or None). Never presses anything.

    Rows far down the list are never built, so a chat not visible is looked up
    through LINE's own search box (ค้นหาข้อความและห้องแชท).
    """
    chat_list, group, rows = _settled_rows(ui)
    if chat_list is None:
        raise ToolError("เปิด LINE แล้วแต่ไม่พบหน้ารายการแชทครับ ตรวจว่าเข้าสู่ระบบ LINE แล้วและหน้าต่างหลักเปิดอยู่")
    exact, partial = match_chats([label for label, _ in rows], recipient)
    searched = None
    if not exact:
        field = _search_field(ui, group)
        if field is not None and not ui.value(field).strip() and ui.set(field, "AXValue", recipient):
            searched = field

            def hit():
                found_list, _ = ui.main_parts()
                if found_list is None:
                    return None
                found = ui.rows(found_list)
                ex, pa = match_chats([label for label, _ in found], recipient)
                partial.extend(pa)
                return (found, ex) if ex else None

            got = _wait(hit, 3.0, 0.25)
            if got:
                rows, exact = got
    try:
        if not exact:
            hint = (" มีชื่อใกล้เคียง: " + ", ".join(list(dict.fromkeys(partial))[:3])) if partial else ""
            raise ToolError(f"ไม่พบแชทชื่อ {recipient} ใน LINE ครับ ยังไม่ได้ส่ง{hint}")
        if len(set(exact)) > 1:
            raise ToolError(f"มีแชทชื่อ {recipient} ใน LINE มากกว่าหนึ่งห้องครับ ยังไม่ได้ส่ง เพื่อไม่ให้ส่งผิดคน")
    except ToolError:
        if searched is not None:
            ui.set(searched, "AXValue", "")
        raise
    target = next(el for label, el in rows if label == exact[0])
    return target, exact[0].split(", ")[0].strip(), searched


def _send_sync(recipient: str, text: str) -> str:
    from jarvis.tools.input import ACCESSIBILITY_HELP, accessibility_ok

    if not accessibility_ok():
        raise ToolError(ACCESSIBILITY_HELP)
    ui = _UI()
    target, chat_name, searched = find_chat(ui, recipient)
    try:
        return _deliver(ui, target, chat_name, text)
    finally:
        if searched is not None:  # put LINE's chat list back the way it was
            ui.set(searched, "AXValue", "")


def _deliver(ui, target, chat_name: str, text: str) -> str:
    _, group = ui.main_parts()
    if not ui.header(group, chat_name):
        if not ui.press(target):
            ui.set(target, "AXSelected", True)
        if not _wait(lambda: ui.header(group, chat_name), 3.0):
            raise ToolError(f"เปิดแชท {chat_name} ใน LINE ไม่สำเร็จครับ ยังไม่ได้ส่ง")

    box = _wait(lambda: ui.message_box(group), 2.0)
    if box is None:
        raise ToolError(f"ไม่พบช่องพิมพ์ข้อความในแชท {chat_name} ครับ ยังไม่ได้ส่ง")
    if ui.value(box).strip():
        raise ToolError(f"มีข้อความพิมพ์ค้างอยู่ในแชท {chat_name} ครับ ยังไม่ได้ส่ง เพื่อไม่ให้ทับข้อความเดิม")

    ui.running.activateWithOptions_(1 << 1)
    ui.set(ui.app, "AXFrontmost", True)
    ui.set(box, "AXFocused", True)
    time.sleep(0.2)
    if not (ui.set(box, "AXValue", text) and _wait(lambda: ui.value(box) == text, 1.0)):
        if not _wait(lambda: ui.running.isActive(), 2.0):
            raise ToolError("เรียกหน้าต่าง LINE ขึ้นมาไม่ได้ครับ ยังไม่ได้ส่ง")
        _paste(text)
        if not _wait(lambda: ui.value(box) == text, 1.5):
            got = ui.value(box)
            if got and got != text:
                ui.set(box, "AXValue", "")
            raise ToolError(f"ใส่ข้อความในแชท {chat_name} ไม่สำเร็จครับ ยังไม่ได้ส่ง")

    if not ui.header(group, chat_name):  # the open chat changed under us: do not send
        ui.set(box, "AXValue", "")
        raise ToolError(f"แชทที่เปิดอยู่ไม่ใช่ {chat_name} แล้วครับ ยกเลิกการส่ง")
    ui.set(box, "AXFocused", True)
    _press_return(ui.pid)
    if not _wait(lambda: not ui.value(box).strip(), 2.5):
        raise ToolError(f"พิมพ์ข้อความไว้ในแชท {chat_name} แล้ว แต่ LINE ยังไม่ส่งครับ กด Enter ในหน้าต่าง LINE เพื่อส่งเอง")
    log.info("LINE message sent to %r (%d chars)", chat_name, len(text))
    return f"ส่งไลน์หา {chat_name} แล้วครับ"


def _confirm(args: dict) -> str:
    return f"ส่งไลน์หา {args.get('recipient', '')} ว่า \"{args.get('text', '')}\" ใช่ไหมครับ"


@tool(
    name="line_send",
    description="Send a LINE message to a chat in the LINE app (exact chat name). Use whenever the user says LINE/ไลน์. "
                "The user confirms before it sends.",
    params={
        "recipient": {"type": "string", "description": "LINE chat or friend name as shown in LINE"},
        "text": {"type": "string", "description": "The message, exactly as the user said it"},
    },
    required=["recipient", "text"],
    risk=_confirm,
    category="productivity",
)
async def line_send(ctx: ToolContext, recipient: str, text: str) -> str:
    from jarvis.thai import line_chat_name

    recipient, text = line_chat_name(recipient), (text or "").strip()
    if not recipient or not text:
        raise ToolError("ต้องมีทั้งชื่อคนรับและข้อความครับ")
    proc = await run(["/usr/bin/open", "-b", BUNDLE], timeout=15)
    if not proc.ok:
        raise ToolError("เปิดแอป LINE ไม่ได้ครับ")
    return await asyncio.to_thread(_send_sync, recipient, text)
