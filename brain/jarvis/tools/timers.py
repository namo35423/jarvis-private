"""Countdown timers.

Durations arrive as speech ("an hour and a half", "ninety seconds"), so they are
parsed here rather than by dateparser, which is built for points in time and
happily reads "10 minutes" as a date. Everything is handed to the scheduler,
which persists it, so a timer survives a restart of Jarvis.
"""

from __future__ import annotations

import re
import time

from jarvis.sysutil import ToolError
from jarvis.tools import ToolContext, tool

MAX_SECONDS = 24 * 3600

_UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
}
_UNIT_RX = "|".join(sorted(_UNITS, key=len, reverse=True))

_NUM_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fourty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
    "couple": 2, "few": 3, "dozen": 12,
}
_TENS = {20, 30, 40, 50, 60, 70, 80, 90}

_FRACTIONS = [("andahalf", 0.5), ("andaquarter", 0.25), ("andathird", 1 / 3)]


def _words_to_digits(text: str) -> str:
    toks = text.split()
    out: list[str] = []
    i = 0
    while i < len(toks):
        tok = toks[i]
        if tok in _NUM_WORDS:
            val = _NUM_WORDS[tok]
            # "twenty five" is one number, not two.
            if val in _TENS and i + 1 < len(toks) and _NUM_WORDS.get(toks[i + 1], 99) < 10:
                val += _NUM_WORDS[toks[i + 1]]
                i += 1
            out.append(str(val))
        else:
            out.append(tok)
        i += 1
    return " ".join(out)


def parse_duration(text: str) -> float:
    """Natural-language duration -> seconds. Raises ToolError if unparseable."""
    s = (text or "").lower().replace("-", " ")
    s = s.translate(str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789"))
    digits = ["ศูนย์", "หนึ่ง", "สอง", "สาม", "สี่", "ห้า", "หก", "เจ็ด", "แปด", "เก้า"]
    numbers = {word: str(n) for n, word in enumerate(digits)}
    for n in range(10, 100):
        tens, ones = divmod(n, 10)
        word = ("" if tens == 1 else "ยี่" if tens == 2 else digits[tens]) + "สิบ"
        word += "เอ็ด" if ones == 1 else digits[ones] if ones else ""
        numbers[word] = str(n)
    numbers.update({"หนึ่งร้อย": "100", "ร้อย": "100"})
    for word in sorted(numbers, key=len, reverse=True):
        s = s.replace(word, numbers[word])
    s = s.replace("ครึ่งชั่วโมง", "half hour").replace("ครึ่งนาที", "half minute")
    for th, en in (("ชั่วโมง", "hours"), ("วินาที", "seconds"), ("นาที", "minutes")):
        s = s.replace(th, " " + en + " ")
    s = s.replace("ครึ่ง", " and a half ")
    s = re.sub(r"[^a-z0-9.\s]", " ", s)

    # Fraction idioms run on the spoken words, before any number-word or article
    # rewriting can break them apart.
    s = re.sub(r"\band\s+a\s+half\b", " andahalf", s)
    s = re.sub(r"\band\s+a\s+quarter\b", " andaquarter", s)
    s = re.sub(r"\band\s+a\s+third\b", " andathird", s)
    s = re.sub(r"\bthree\s+quarters\s+(?:of\s+)?an?\s+hour\b", "45 minutes", s)
    s = re.sub(r"\b(?:an?\s+)?quarter\s+(?:of\s+)?(?:an?\s+)?hour\b", "15 minutes", s)
    s = re.sub(r"\bhalf\s+(?:an?\s+)?hour\b", "30 minutes", s)
    s = re.sub(r"\bhalf\s+(?:an?\s+)?minute\b", "30 seconds", s)

    s = re.sub(r"\b(for|please|set|timer|alarm|countdown|the|about|around|just|of)\b", " ", s)
    s = _words_to_digits(s)
    s = re.sub(r"\ban?\b", "1", s)

    for token, frac in _FRACTIONS:
        # "1 hour andahalf" and "2 andahalf hours" mean the same thing.
        s = re.sub(
            rf"(\d+(?:\.\d+)?)\s*({_UNIT_RX})\b\s*{token}",
            lambda m, f=frac: f"{float(m.group(1)) + f} {m.group(2)}",
            s,
        )
        s = re.sub(
            rf"(\d+(?:\.\d+)?)\s*{token}\s*({_UNIT_RX})\b",
            lambda m, f=frac: f"{float(m.group(1)) + f} {m.group(2)}",
            s,
        )
        s = s.replace(token, " ")

    total = 0.0
    matched = False
    bare: float | None = None
    trailing: float | None = None
    last_unit = 0
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*([a-z]+)?", s):
        value = float(m.group(1))
        unit = (m.group(2) or "").strip()
        if unit in _UNITS:
            total += value * _UNITS[unit]
            last_unit = _UNITS[unit]
            matched = True
        elif not unit:
            if matched and trailing is None:
                trailing = value
            elif bare is None:
                bare = value

    # "one minute thirty" - a bare number after a unit means the next unit down.
    if matched and trailing is not None and last_unit in (3600, 60):
        total += trailing * (last_unit / 60)

    if not matched:
        if bare is not None:
            total = bare * 60  # "set a timer for 10" means minutes
        else:
            raise ToolError(
                f"I could not read {text!r} as a length of time. Say something like "
                "'10 minutes' or 'an hour and a half'."
            )

    if total < 1:
        raise ToolError("that is shorter than a second")
    if total > MAX_SECONDS:
        raise ToolError("I only set timers up to 24 hours. Use a reminder for anything longer.")
    return total


def human_duration(seconds: float) -> str:
    total = int(round(seconds))
    if total <= 0:
        return "no time"
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    # Seconds only matter when they are the whole story or the tail of a short timer.
    if secs and not hours:
        parts.append(f"{secs} second{'s' if secs != 1 else ''}")
    return " ".join(parts) if parts else f"{total} seconds"


def _timers(ctx: ToolContext) -> list:
    return [i for i in ctx.scheduler.pending() if i.kind == "timer"]


@tool(
    name="set_timer",
    description=(
        "Start a countdown timer. Jarvis speaks when it finishes. Use for 'set a timer for ten "
        "minutes', 'remind me in an hour', 'three minute egg'. Give the duration exactly as the "
        "user said it. Max 24 hours."
    ),
    params={
        "duration": {
            "type": "string",
            "description": "How long, in plain words: '10 minutes', '90 seconds', 'an hour and a half'",
        },
        "label": {
            "type": "string",
            "description": "What the timer is for, if the user said: 'pasta', 'laundry'",
        },
    },
    required=["duration"],
    ack="Timer set.",
    category="time",
)
async def set_timer(ctx: ToolContext, duration: str, label: str = "") -> str:
    seconds = parse_duration(duration)
    label = " ".join((label or "").split()).strip(".,!?").lower()
    spoken = human_duration(seconds)
    message = f"Your {label} timer is up." if label else f"Your {spoken} timer is up."
    thai_spoken = spoken
    for word, translated in (("hours", "ชั่วโมง"), ("hour", "ชั่วโมง"), ("minutes", "นาที"), ("minute", "นาที"), ("seconds", "วินาที"), ("second", "วินาที")):
        thai_spoken = thai_spoken.replace(word, translated)
    if ctx.cfg.get("language") == "th":
        message = f"ครบเวลา {label or thai_spoken} แล้วครับ"
    ctx.scheduler.add("timer", time.time() + seconds, message, label)
    if ctx.cfg.get("language") == "th":
        return f"ตั้งเวลา {label + ' ' if label else ''}{thai_spoken} แล้วครับ"
    return f"{label.capitalize()} timer set for {spoken}." if label else f"Timer set for {spoken}."


def _describe(items: list, now: float) -> str:
    parts = []
    for item in items:
        left = human_duration(max(0.0, item.fire_at - now))
        parts.append(f"{item.label}, {left} left" if item.label else f"{left} left")
    if len(parts) == 1:
        return f"One timer: {parts[0]}."
    return f"{len(parts)} timers: " + "; ".join(parts) + "."


def _pick(items: list, which: str):
    which = (which or "").strip().lower()
    if not which or which in ("last", "latest", "newest", "it", "that"):
        return max(items, key=lambda i: i.id)
    if which.isdigit():
        by_id = [i for i in items if i.id == int(which)]
        if by_id:
            return by_id[0]
    hits = [i for i in items if which in (i.label or "").lower()]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise ToolError(
            f"{len(hits)} timers match {which!r}: "
            + ", ".join(h.label for h in hits)
            + ". Which one?"
        )
    return None


def _risk(args: dict) -> str | None:
    if (args.get("action") or "").strip().lower() == "cancel_all":
        return "Cancel all running timers?"
    return None


@tool(
    name="timers",
    description=(
        "List or cancel running countdown timers. Use for 'how long left on my timer', "
        "'what timers do I have', 'cancel the pasta timer', 'cancel all timers'. To create one, "
        "use set_timer instead."
    ),
    params={
        "action": {
            "type": "string",
            "enum": ["list", "cancel", "cancel_all"],
            "description": "What to do. Default 'list'.",
        },
        "which": {
            "type": "string",
            "description": "For cancel: the timer's label ('pasta'), its id, or 'last'.",
        },
    },
    required=["action"],
    category="time",
)
async def timers(ctx: ToolContext, action: str = "list", which: str = "") -> str:
    action = (action or "list").strip().lower()
    items = _timers(ctx)
    now = time.time()

    if action == "list":
        return _describe(items, now) if items else "No timers running."

    if action == "cancel_all":
        if not items:
            return "No timers to cancel."
        n = ctx.scheduler.cancel_all("timer")
        return f"Cancelled {n} timer{'s' if n != 1 else ''}."

    if action == "cancel":
        if not items:
            return "No timers running."
        target = _pick(items, which)
        if target is None:
            return f"No timer matching {which!r}. Running: " + _describe(items, now)
        if not ctx.scheduler.cancel(target.id):
            raise ToolError("that timer had already finished")
        name = f"{target.label} timer" if target.label else f"{human_duration(max(0.0, target.fire_at - now))} timer"
        return f"Cancelled the {name}."

    raise ToolError(f"unknown action {action!r}. Use list, cancel or cancel_all.")
