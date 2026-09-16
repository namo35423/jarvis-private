"""Long-term facts about the user, and the morning briefing that uses them.

Facts are stored rewritten into the third person. Speech gives us "my sister's
birthday is in June"; six weeks later "my" has no referent, so the rewrite
happens on the way in rather than being guessed at on the way out.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import time
from datetime import datetime

from jarvis.sysutil import ToolError
from jarvis.tools import REGISTRY, ToolContext, tool

_LEAD = re.compile(
    r"^(?:please\s+)?(?:remember|note|store|keep in mind|don'?t forget)(?:\s+(?:that|this))?\s*[:,]?\s+",
    re.I,
)

# Applied in order; the compound forms have to win before the bare pronoun does.
_SUBS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bi'?m\b", re.I), "User is"),
    (re.compile(r"\bi\s+am\b", re.I), "User is"),
    (re.compile(r"\bi'?ve\b", re.I), "User has"),
    (re.compile(r"\bi\s+have\b", re.I), "User has"),
    (re.compile(r"\bi'?ll\b", re.I), "User will"),
    (re.compile(r"\bi'?d\b", re.I), "User would"),
    (re.compile(r"\bmyself\b", re.I), "User"),
    (re.compile(r"\bmine\b", re.I), "User's"),
    (re.compile(r"\bmy\b", re.I), "User's"),
    (re.compile(r"\bme\b", re.I), "User"),
    (re.compile(r"\bi\b"), "User"),
]

_IRREGULAR = {"have": "has", "be": "is", "go": "goes", "do": "does"}

# Only these get conjugated after "User", so an unknown word is never mangled.
_BASE_VERBS = {
    "like", "love", "hate", "prefer", "want", "need", "work", "live", "have", "own",
    "drink", "eat", "use", "play", "go", "do", "study", "take", "run", "ride", "drive",
    "speak", "teach", "watch", "wake", "sleep", "cook", "travel", "keep", "get", "try",
    "fly", "carry", "enjoy", "dislike", "avoid", "train", "lift", "read", "write",
    "build", "manage", "lead", "hold", "miss", "walk", "swim", "bike", "code", "collect",
}


def _conjugate(verb: str) -> str:
    low = verb.lower()
    if low in _IRREGULAR:
        return _IRREGULAR[low]
    if low.endswith(("s", "x", "z", "ch", "sh", "o")):
        return low + "es"
    if low.endswith("y") and len(low) > 1 and low[-2] not in "aeiou":
        return low[:-1] + "ies"
    return low + "s"


def third_person(text: str) -> str:
    out = _LEAD.sub("", " ".join((text or "").split()))
    for pattern, replacement in _SUBS:
        out = pattern.sub(replacement, out)
    out = re.sub(
        r"\bUser\s+([a-z]+)\b",
        lambda m: f"User {_conjugate(m.group(1))}" if m.group(1).lower() in _BASE_VERBS else m.group(0),
        out,
    )
    out = out.strip().rstrip(",;")
    if out and out[0].islower():
        out = out[0].upper() + out[1:]
    if out and out[-1] not in ".!?":
        out += "."
    return out


@tool(
    name="remember",
    description=(
        "Store a durable fact about the user so it is available in later conversations - "
        "preferences, names, birthdays, addresses, habits, how they like things done. Use when "
        "the user says 'remember that...', or states something personal worth keeping. Not for "
        "tasks or timers."
    ),
    params={
        "fact": {
            "type": "string",
            "description": "The fact, as the user said it, e.g. 'my sister's birthday is 3 June'",
        }
    },
    required=["fact"],
    category="memory",
)
async def remember(ctx: ToolContext, fact: str) -> str:
    cleaned = third_person(fact)
    if len(cleaned.strip(".")) < 3:
        raise ToolError("that is too short to store. What should I remember?")
    ctx.memory.add_fact(cleaned, source="user")
    return f"Noted: {cleaned}"


_BROAD = re.compile(r"^(everything|anything|all|my facts|about me|what do you know.*)$", re.I)


@tool(
    name="recall",
    description=(
        "Look up facts previously stored about the user. Use before answering anything personal "
        "you do not already know - preferences, names, dates, addresses - and when the user asks "
        "'what do you know about...' or 'what did I tell you about...'."
    ),
    params={
        "query": {
            "type": "string",
            "description": "What to look for, e.g. 'sister birthday' or 'coffee'",
        }
    },
    required=["query"],
    category="memory",
)
async def recall(ctx: ToolContext, query: str) -> str:
    query = (query or "").strip()
    if not query:
        raise ToolError("what should I look up?")
    if _BROAD.match(query):
        facts = ctx.memory.list_facts(limit=8)
    else:
        facts = ctx.memory.search_facts(query, limit=5)
    if not facts:
        with ctx.memory._lock:
            rows = ctx.memory.db.execute(
                "SELECT role, text FROM turns WHERE text LIKE ? ORDER BY id DESC LIMIT 4", (f"%{query}%",)
            ).fetchall()
        if rows:
            return "From earlier conversations: " + " | ".join(f"{r['role']}: {r['text'][:140]}" for r in rows)
        return f"Nothing stored about {query}."
    joined = "; ".join(f.text.strip().rstrip(".") for f in facts)
    return f"{joined}."


# ------------------------------------------------------------------ briefing


async def _soft_call(ctx: ToolContext, name: str, *variants: dict, timeout: float = 12.0) -> str | None:
    """Call another tool for the briefing, and shrug if it is missing or broken.

    Signatures for the calendar/reminder tools are owned by another module, so
    arguments are filtered against the registered schema and several shapes are
    tried before giving up. One dead tool must not cost the whole briefing.
    """
    spec = REGISTRY.get(name)
    if spec is None:
        return None
    for kwargs in variants or ({},):
        args = {k: v for k, v in kwargs.items() if k in spec.params}
        if [r for r in spec.required if r not in args]:
            continue
        try:
            result = spec.handler(ctx, **args)
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, timeout)
        except Exception:  # noqa: BLE001 - degrade, never fail the briefing
            continue
        text = (result if isinstance(result, str) else str(result or "")).strip()
        if text and not text.lower().startswith("error"):
            return " ".join(text.split())
    return None


def _greeting(now: datetime, name: str, address_as: str) -> str:
    if now.hour < 12:
        part = "Good morning"
    elif now.hour < 17:
        part = "Good afternoon"
    else:
        part = "Good evening"
    who = address_as or name
    return f"{part}, {who}." if who else f"{part}."


def _sentence(text: str) -> str:
    text = text.strip()
    return text if not text or text[-1] in ".!?" else text + "."


@tool(
    name="daily_briefing",
    description=(
        "Speak the morning briefing: greeting, today's weather, today's calendar events, "
        "reminders due today and any running timers. Use for 'good morning', 'brief me', "
        "'what does my day look like', 'catch me up'."
    ),
    params={},
    required=[],
    ack="Here's your day.",
    category="general",
)
async def daily_briefing(ctx: ToolContext) -> str:
    now = datetime.now()
    name = (ctx.cfg.get("user_name") or "").strip()
    address_as = (ctx.cfg.get("address_as") or "").strip()

    lines = [
        f"{_greeting(now, name, address_as)} It's {now.strftime('%A, %B')} {now.day}."
    ]

    weather = await _soft_call(
        ctx, "get_weather", {"when": "today"}, {"when": "now"}, timeout=20.0
    )
    if weather:
        lines.append(_sentence(weather))

    events = await _soft_call(
        ctx,
        "calendar",
        {"action": "list", "day": "today"},
        {"action": "list", "when": "today"},
        {"action": "list", "date": "today"},
        {"action": "list"},
    )
    if events:
        lines.append(_sentence(events))

    reminders = await _soft_call(
        ctx,
        "reminders",
        {"action": "list", "due": "today"},
        {"action": "list", "when": "today"},
        {"action": "list", "day": "today"},
        {"action": "list", "filter": "today"},
        {"action": "list"},
    )
    if reminders:
        lines.append(_sentence(reminders))

    running = [i for i in ctx.scheduler.pending() if i.kind == "timer"]
    if running:
        from jarvis.tools.timers import human_duration

        soonest = min(running, key=lambda i: i.fire_at)
        left = human_duration(max(0.0, soonest.fire_at - time.time()))
        if len(running) == 1:
            label = f" for {soonest.label}" if soonest.label else ""
            lines.append(f"You have a timer{label} with {left} left.")
        else:
            lines.append(f"You have {len(running)} timers running, the next in {left}.")

    if len(lines) == 1:
        lines.append("Nothing else on the books - no weather, calendar or reminders available.")
    return " ".join(lines)
