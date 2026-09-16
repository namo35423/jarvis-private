"""Reminders, Calendar, Notes and Messages, through the local apps.

Using the Mac's own apps rather than a cloud API means it works today with no
accounts to connect, and anything created here syncs to the user's phone the
same way it would if they had typed it in themselves.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from jarvis.sysutil import ToolError, applescript_quote, osascript
from jarvis.tools import ToolContext, tool



def parse_when(text: str) -> datetime | None:
    """Natural language time, resolved into the future."""
    if not (text or "").strip():
        return None
    import dateparser

    return dateparser.parse(
        text,
        settings={
            "PREFER_DATES_FROM": "future",
            "RETURN_AS_TIMEZONE_AWARE": False,
            "RELATIVE_BASE": datetime.now(),
        },
    )


def as_applescript_date(when: datetime, var: str = "theDate") -> str:
    """Build a date without relying on the system's date format."""
    return (
        f"set {var} to current date\n"
        f"set year of {var} to {when.year}\n"
        f"set month of {var} to {when.month}\n"
        f"set day of {var} to {when.day}\n"
        f"set hours of {var} to {when.hour}\n"
        f"set minutes of {var} to {when.minute}\n"
        f"set seconds of {var} to 0\n"
    )


def speak_time(when: datetime) -> str:
    today = datetime.now().date()
    day = "today" if when.date() == today else (
        "tomorrow" if when.date() == today + timedelta(days=1) else when.strftime("on %A the %-d")
    )
    return f"{day} at {when.strftime('%-I:%M %p').lower()}"


@tool(
    name="reminders",
    description=(
        "Create or read reminders in the Reminders app, which syncs to the user's iPhone. Use for "
        "'remind me to call mum at six', 'what are my reminders', 'what do I need to do today'."
    ),
    params={
        "action": {"type": "string", "enum": ["create", "list_today", "list_all"]},
        "text": {"type": "string", "description": "What the reminder says, e.g. 'call the dentist'"},
        "when": {"type": "string", "description": "When it is due in plain words, e.g. 'tomorrow at 9am', 'in 2 hours'"},
    },
    required=["action"],
    category="productivity",
)
async def reminders(ctx: ToolContext, action: str, text: str = "", when: str = "") -> str:
    action = action.lower()

    if action == "create":
        if not text.strip():
            raise ToolError("say what to be reminded about")
        due = parse_when(when) if when else None
        props = f"name:{applescript_quote(text.strip())}"
        script = 'tell application "Reminders"\n'
        if due:
            script += as_applescript_date(due)
            props += ", due date:theDate"
        script += f"  make new reminder with properties {{{props}}}\nend tell"
        await osascript(script, timeout=30)
        return f"Reminder set: {text.strip()}" + (f", {speak_time(due)}." if due else ".")

    if action in ("list_today", "list_all"):
        script = (
            'tell application "Reminders"\n'
            '  set out to ""\n'
            '  repeat with r in (every reminder whose completed is false)\n'
            '    set d to due date of r\n'
            '    if d is missing value then\n'
            '      set out to out & (name of r) & "\t" & "none" & "\n"\n'
            '    else\n'
            '      set out to out & (name of r) & "\t" & ((year of d) as text) & "-" & '
            '((month of d as integer) as text) & "-" & ((day of d) as text) & " " & '
            '((hours of d) as text) & ":" & ((minutes of d) as text) & "\n"\n'
            '    end if\n'
            '  end repeat\n'
            '  return out\n'
            'end tell'
        )
        raw = await osascript(script, timeout=25)
        items = []
        today = datetime.now().date()
        for line in raw.splitlines():
            name, _, due = line.partition("\t")
            if not name.strip():
                continue
            when_dt = None
            if due.strip() and due.strip() != "none":
                try:
                    date_part, time_part = due.strip().split(" ")
                    y, m, d = (int(v) for v in date_part.split("-"))
                    hh, mm = (int(v) for v in time_part.split(":"))
                    when_dt = datetime(y, m, d, hh, mm)
                except ValueError:
                    when_dt = None
            if action == "list_today" and (when_dt is None or when_dt.date() != today):
                continue
            items.append(f"{name.strip()}" + (f" at {when_dt.strftime('%-I:%M %p').lower()}" if when_dt else ""))
        if not items:
            return "Nothing due today." if action == "list_today" else "No open reminders."
        head = "Due today: " if action == "list_today" else "Open reminders: "
        return head + "; ".join(items[:12]) + (f" and {len(items) - 12} more." if len(items) > 12 else ".")

    raise ToolError(f"unknown action {action!r}")


@tool(
    name="calendar",
    description=(
        "Read or add events in the Calendar app. Use for 'what's on my calendar', 'am I free at 3', "
        "'what's my day look like', 'put dinner at 7 in my calendar'."
    ),
    params={
        "action": {"type": "string", "enum": ["list", "create"]},
        "day": {"type": "string", "description": "'today', 'tomorrow', or a date like '15 March'. Default today."},
        "title": {"type": "string", "description": "Event title, for create"},
        "start": {"type": "string", "description": "Start time in plain words, e.g. 'tomorrow at 7pm'"},
        "duration_minutes": {"type": "integer", "description": "How long, default 60"},
    },
    required=["action"],
    category="productivity",
)
async def calendar(ctx: ToolContext, action: str, day: str = "today", title: str = "",
                   start: str = "", duration_minutes: int = 60) -> str:
    action = action.lower()

    if action == "create":
        if not title.strip():
            raise ToolError("say what the event is called")
        begin = parse_when(start) if start else None
        if not begin:
            raise ToolError("say when the event starts, e.g. 'tomorrow at 3pm'")
        end = begin + timedelta(minutes=max(5, int(duration_minutes or 60)))
        script = (
            'tell application "Calendar"\n'
            + as_applescript_date(begin, "startDate")
            + as_applescript_date(end, "endDate")
            + '  tell (first calendar whose writable is true)\n'
            f'    make new event with properties {{summary:{applescript_quote(title.strip())}, '
            'start date:startDate, end date:endDate}\n'
            '  end tell\n'
            'end tell'
        )
        await osascript(script, timeout=25)
        return f"Added {title.strip()} {speak_time(begin)}."

    target = parse_when(day) if day and day.lower() not in ("today", "") else datetime.now()
    if day and day.lower() == "tomorrow":
        target = datetime.now() + timedelta(days=1)
    target = target or datetime.now()
    begin = target.replace(hour=0, minute=0, second=0, microsecond=0)
    end = begin + timedelta(days=1)

    script = (
        'tell application "Calendar"\n'
        + as_applescript_date(begin, "startDate")
        + as_applescript_date(end, "endDate")
        + '  set out to ""\n'
        '  repeat with c in calendars\n'
        '    repeat with e in (every event of c whose start date is greater than or equal to startDate '
        'and start date is less than endDate)\n'
        '      set s to start date of e\n'
        '      set out to out & ((hours of s) as text) & ":" & ((minutes of s) as text) & "\t" & (summary of e) & "\n"\n'
        '    end repeat\n'
        '  end repeat\n'
        '  return out\n'
        'end tell'
    )
    raw = await osascript(script, timeout=30)
    events = []
    for line in raw.splitlines():
        time_part, _, name = line.partition("\t")
        if not name.strip():
            continue
        try:
            hh, mm = (int(v) for v in time_part.split(":"))
            label = datetime(2000, 1, 1, hh, mm).strftime("%-I:%M %p").lower().replace(":00", "")
        except ValueError:
            label = time_part
        events.append((time_part, f"{label} {name.strip()}"))
    if not events:
        word = "today" if begin.date() == datetime.now().date() else begin.strftime("on %A")
        return f"Nothing in the calendar {word}."
    events.sort(key=lambda e: [int(p) for p in e[0].split(":")] if ":" in e[0] else [0, 0])
    word = "Today" if begin.date() == datetime.now().date() else begin.strftime("%A")
    return f"{word}: " + ", ".join(e[1] for e in events[:10]) + "."


@tool(
    name="notes",
    description="Create or search notes in the Notes app. Use for 'make a note that...', 'find my note about X'.",
    params={
        "action": {"type": "string", "enum": ["create", "search"]},
        "title": {"type": "string", "description": "Note title for create"},
        "body": {"type": "string", "description": "Note contents for create"},
        "query": {"type": "string", "description": "What to search for"},
    },
    required=["action"],
    category="productivity",
)
async def notes(ctx: ToolContext, action: str, title: str = "", body: str = "", query: str = "") -> str:
    action = action.lower()
    if action == "create":
        if not (title.strip() or body.strip()):
            raise ToolError("say what the note should say")
        name = title.strip() or body.strip()[:40]
        content = f"<div><b>{name}</b></div><div>{(body or '').strip()}</div>"
        await osascript(
            f'tell application "Notes" to make new note at folder "Notes" of default account '
            f'with properties {{name:{applescript_quote(name)}, body:{applescript_quote(content)}}}',
            timeout=30,
        )
        return f"Note saved: {name}."

    if action == "search":
        if not query.strip():
            raise ToolError("say what to search for")
        raw = await osascript(
            f'tell application "Notes"\n'
            f'  set out to ""\n'
            f'  repeat with n in (every note whose name contains {applescript_quote(query)} '
            f'or body contains {applescript_quote(query)})\n'
            f'    set out to out & (name of n) & "\n"\n'
            f'  end repeat\n'
            f'  return out\n'
            f'end tell',
            timeout=45,
        )
        found = [l.strip() for l in raw.splitlines() if l.strip()]
        if not found:
            return f"No notes mention {query}."
        return f"{len(found)} note(s): " + ", ".join(found[:8]) + "."

    raise ToolError(f"unknown action {action!r}")


def _confirm_message(args: dict) -> str:
    return f"Send \"{args.get('text', '')}\" to {args.get('recipient', 'them')}?"


@tool(
    name="messages_send",
    description=(
        "Send an iMessage or text through the Messages app. Use only when the user explicitly asks to "
        "text or message someone. The user is always asked to confirm before it sends."
    ),
    params={
        "recipient": {"type": "string", "description": "Contact name, phone number or email"},
        "text": {"type": "string", "description": "The message to send"},
    },
    required=["recipient", "text"],
    risk=_confirm_message,
    category="productivity",
)
async def messages_send(ctx: ToolContext, recipient: str, text: str) -> str:
    if not recipient.strip() or not text.strip():
        raise ToolError("I need both who to send it to and what to say")
    script = (
        'tell application "Messages"\n'
        '  set svc to 1st service whose service type = iMessage\n'
        f'  set who to participant {applescript_quote(recipient.strip())} of svc\n'
        f'  send {applescript_quote(text.strip())} to who\n'
        'end tell'
    )
    try:
        await osascript(script, timeout=30)
    except ToolError as exc:
        raise ToolError(
            f"Messages would not send that ({exc}). The name may not match a contact - try a phone number."
        ) from exc
    return f"Sent to {recipient.strip()}."
