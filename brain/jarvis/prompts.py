"""The system prompt, and the per-turn context header.

Split deliberately: the system prompt must stay byte-identical between turns so
Ollama can reuse the cached prompt prefix (a changed system prompt costs a full
re-prefill of ~2k tokens on every request). Anything that changes - the time,
the frontmost app - rides in the user message instead.
"""

from __future__ import annotations

import time
from datetime import datetime

BASE = """You are {name}, the voice assistant on {user}'s Mac, with real control of it through your tools.

SPEAKING (replies are read aloud)
- Brief by default: answer first, usually in a sentence or two, but give the full answer when the question needs it. No filler, no offers of more help.
- Speak to {user} as "you"; never guess anyone's pronouns.
- Use what you know about {user} and what was said earlier. When they share something worth keeping, call remember.
- No markdown, lists, emoji, URLs or code. Say numbers and times as spoken words.
- Dry and competent. Never narrate what you are doing.

ACTING
- If a request needs an action or live data, call the tool. Never claim an action you did not take, never invent data.
- Chain tools for multi-step jobs, then give one short summary.
- On a tool error, say what failed and the fix. Never repeat a failing call.
- The system handles confirmations; just make the call.
- Ask one short question only if truly ambiguous; otherwise act.
- Pick tools by purpose: Notes app -> notes; facts about the user -> remember; creating a file, presentation, spreadsheet or code -> ask_claude; changing your own behaviour, voice, features or code -> improve_self.

CLAUDE
Do everything you can yourself. ask_claude is a last resort: only for writing code, making documents or presentations, deep multi-source research, or a job you already failed at.

ABOUT {upper_user}
{facts}"""

PERSONALITY_HONORIFIC = """
Address them as "{honorific}" occasionally - not in every reply."""


def build_system_prompt(cfg, memory) -> str:
    name = cfg.get("assistant_name", "Jarvis")
    user = cfg.get("user_name") or "the user"
    facts = memory.list_facts(limit=40) if memory else []
    if facts:
        fact_lines = "\n".join(f"- {f.text}" for f in reversed(facts))
    else:
        fact_lines = "- Nothing yet. When they tell you something worth keeping, call remember."
    prompt = BASE.format(name=name, user=user, upper_user=user.upper(), facts=fact_lines)
    honorific = (cfg.get("address_as") or "").strip()
    if honorific:
        prompt += PERSONALITY_HONORIFIC.format(honorific=honorific)
    return prompt


def context_header(cfg, frontmost: str | None = None, extra: str | None = None) -> str:
    """A compact situational line prepended to the user's message.

    Lives in the user turn, not the system prompt, so the cached prefix survives.
    """
    now = datetime.now()
    bits = [now.strftime("%A %-d %B %Y, %-I:%M %p")]
    if frontmost:
        bits.append(f"frontmost app: {frontmost}")
    if extra:
        bits.append(extra)
    return "[" + "; ".join(bits) + "]"


def with_context(cfg, text: str, frontmost: str | None = None, extra: str | None = None) -> str:
    return f"{context_header(cfg, frontmost, extra)}\n{text}"


CONFIRM_YES = {
    "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "do it", "go ahead", "go for it",
    "confirm", "confirmed", "affirmative", "please do", "yes please", "correct", "right",
    "proceed", "continue", "send it", "definitely", "absolutely", "fine", "alright",
}
CONFIRM_NO = {
    "no", "nope", "nah", "cancel", "stop", "don't", "dont", "do not", "never mind",
    "nevermind", "forget it", "abort", "negative", "wait", "hold on", "leave it", "no thanks",
}


def classify_confirmation(text: str) -> bool | None:
    """Decide whether a spoken reply approved the pending action.

    Returns True/False, or None when the answer is not a clear yes or no (the
    caller then treats it as a new request rather than guessing).
    """
    t = " ".join((text or "").lower().strip().strip(".!?").split())
    if not t:
        return None
    if t in CONFIRM_YES:
        return True
    if t in CONFIRM_NO:
        return False
    first = t.split()[0]
    if first in {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "confirm", "proceed"}:
        return True
    if first in {"no", "nope", "nah", "cancel", "stop", "don't", "dont", "forget", "abort"}:
        return False
    if t.startswith(("go ahead", "do it", "please do", "yes ", "never mind", "no ")):
        return t.startswith(("go ahead", "do it", "please do", "yes "))
    return None
