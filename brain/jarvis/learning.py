"""Getting better with use.

Three mechanisms, deliberately cheap - all of them run on the local model or on
plain SQL, so improving never costs API credits:

  lessons   A mistake, distilled to one line, injected into the system prompt.
            Sources: the user correcting Jarvis, a tool failing the same way
            twice, or the user stating a standing preference ("always ...").
  skills    A request that has been handled successfully more than once is
            cached with the exact tool plan that worked. An identical request
            later replays the plan directly - no model call at all, which is
            both instant and free.
  stats     Per-tool success rates, so a tool that keeps failing can be
            surfaced rather than silently retried forever.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("jarvis.learning")

SCHEMA = """
CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    lesson TEXT NOT NULL,
    trigger TEXT DEFAULT '',
    created REAL NOT NULL,
    hits INTEGER DEFAULT 1,
    last_used REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    request TEXT NOT NULL,
    norm TEXT NOT NULL,
    plan TEXT DEFAULT '[]',
    reply TEXT DEFAULT '',
    errors INTEGER DEFAULT 0,
    corrected INTEGER DEFAULT 0,
    seconds REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    norm TEXT UNIQUE NOT NULL,
    request TEXT NOT NULL,
    plan TEXT NOT NULL,
    uses INTEGER DEFAULT 1,
    successes INTEGER DEFAULT 1,
    last_used REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tool_stats (
    name TEXT PRIMARY KEY,
    calls INTEGER DEFAULT 0,
    failures INTEGER DEFAULT 0,
    last_error TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_episodes_norm ON episodes(norm);
"""

CORRECTION_PATTERNS = re.compile(
    r"^\s*(no[,.\s]|nope|wrong|that's wrong|that is wrong|not (that|what)|i (said|meant|asked)|"
    r"ไม่ใช่|ผิดแล้ว|หมายถึง|จริงๆ|actually[,\s]|i didn't (say|ask)|don't do that|stop doing|that's not)",
    re.I,
)
# "It didn't work", "it's still open" - reports that the last action failed.
FAILURE_REPORTS = re.compile(
    r"\b(did ?n'?t|did not|does ?n'?t|does not|is ?n'?t|not) (work|close|open|quit|stop|play|change|happen|do anything)\b|"
    r"ยังไม่ปิด|ยังไม่เปิด|ไม่ทำงาน|ไม่ได้ผล|ลองใหม่|\bstill (open|there|running|playing|on|here|going)\b|\bthat'?s (wrong|not right|not what)\b|"
    r"\btry (it )?again\b|\byou (did ?n'?t|did not|missed|forgot|got it wrong)\b|\bwrong\b",
    re.I,
)
PREFERENCE_PATTERNS = re.compile(
    r"\b(always|never|from now on|in future|in the future|stop (saying|doing)|"
    r"ต่อไปนี้|ฉันชอบ|อย่าพูด|จำไว้ว่า|i prefer|i'd prefer|i like it when|don't ever|remember to)\b",
    re.I,
)

DISTILL_PROMPT = """An assistant made a mistake. Write the standing rule it should follow from now on.

The user asked: {request}
The assistant did: {action}
The user corrected it: {correction}

Write ONE rule addressed TO THE ASSISTANT, as a direct instruction it can obey without further context. Under 18 words. It must describe what the ASSISTANT should do, never what the user should do.
Good: "Always give temperatures in Celsius."
Good: "When asked to open a file, open it rather than only describing it."
Bad: "Ask the user which unit they prefer."
Output only the rule."""

TOOL_LESSON_PROMPT = """An assistant's tool keeps failing.

Tool: {tool}
Arguments used: {args}
Error returned: {error}

Write ONE imperative sentence, under 20 words, telling the assistant how to use this tool correctly next time. No preamble, just the sentence."""


@dataclass
class Episode:
    request: str
    norm: str
    plan: list[dict] = field(default_factory=list)
    results: list[str] = field(default_factory=list)
    reply: str = ""
    errors: int = 0
    seconds: float = 0.0
    row_id: int = 0


def normalise(text: str) -> str:
    """Canonical form for matching repeat requests."""
    t = (text or "").lower().strip()
    t = re.sub(r"^(hey |ok |okay |jarvis[,\s]+|please\s+)+", "", t)
    t = re.sub(r"[^a-z0-9\s\u0e00-\u0e7f]", "", t)
    return re.sub(r"\s+", " ", t).strip()


class Learning:
    def __init__(self, cfg, memory, llm):
        self.cfg = cfg
        self.memory = memory
        self.llm = llm
        with self.memory._lock:
            self.memory.db.executescript(SCHEMA)
            self.memory.db.commit()
        self.last_episode: Episode | None = None
        self.side_chat = None  # set by the assistant: a model call that keeps the prompt cache warm

    # --------------------------------------------------------------- lessons
    def add_lesson(self, lesson: str, kind: str = "correction", trigger: str = "") -> None:
        lesson = (lesson or "").strip().strip('"')
        if not lesson or len(lesson) > 240:
            return
        with self.memory._lock:
            row = self.memory.db.execute(
                "SELECT id, hits FROM lessons WHERE lesson = ?", (lesson,)
            ).fetchone()
            if row:
                self.memory.db.execute(
                    "UPDATE lessons SET hits = hits + 1, created = ? WHERE id = ?", (time.time(), row["id"])
                )
            else:
                self.memory.db.execute(
                    "INSERT INTO lessons (kind, lesson, trigger, created) VALUES (?,?,?,?)",
                    (kind, lesson, trigger[:200], time.time()),
                )
            self.memory.db.commit()
        log.info("learned: %s", lesson)

    def lessons(self, limit: int = 12) -> list[str]:
        """Most-reinforced, most-recent lessons for the system prompt."""
        with self.memory._lock:
            rows = self.memory.db.execute(
                "SELECT lesson FROM lessons ORDER BY hits DESC, created DESC LIMIT ?", (limit,)
            ).fetchall()
        return [r["lesson"] for r in rows]

    def forget_lesson(self, fragment: str) -> int:
        with self.memory._lock:
            cur = self.memory.db.execute(
                "DELETE FROM lessons WHERE lesson LIKE ?", (f"%{fragment}%",)
            )
            self.memory.db.commit()
            return cur.rowcount

    # -------------------------------------------------------------- episodes
    def start_episode(self, request: str) -> Episode:
        return Episode(request=request, norm=normalise(request))

    def finish_episode(self, ep: Episode) -> None:
        with self.memory._lock:
            cur = self.memory.db.execute(
                "INSERT INTO episodes (ts, request, norm, plan, reply, errors, seconds) VALUES (?,?,?,?,?,?,?)",
                (time.time(), ep.request, ep.norm, json.dumps(ep.plan), ep.reply[:2000], ep.errors, ep.seconds),
            )
            self.memory.db.commit()
            ep.row_id = int(cur.lastrowid)
        self.last_episode = ep
        if ep.plan and ep.errors == 0:
            self._reinforce_skill(ep)

    def detect_correction(self, text: str) -> Episode | None:
        """Is this the user saying the last turn was wrong? SQL only - no model call,
        so it never delays the reply."""
        ep = self.last_episode
        if not ep or not (CORRECTION_PATTERNS.search(text or "") or FAILURE_REPORTS.search(text or "")):
            return None
        with self.memory._lock:
            self.memory.db.execute("UPDATE episodes SET corrected = 1 WHERE id = ?", (ep.row_id,))
            self.memory.db.execute("UPDATE skills SET successes = successes - 2 WHERE norm = ?", (ep.norm,))
            self.memory.db.execute("DELETE FROM skills WHERE successes <= 0")
            self.memory.db.commit()
        return ep

    async def learn_from_correction(self, ep: Episode, text: str) -> None:
        """Distil the standing rule. Run after the reply, when the model is free."""
        action = _describe_plan(ep.plan) or (ep.reply[:160] or "replied without acting")
        lesson = await self._distill(DISTILL_PROMPT.format(request=ep.request, action=action, correction=text))
        if lesson:
            self.add_lesson(lesson, kind="correction", trigger=ep.norm)

    async def note_possible_correction(self, text: str) -> bool:
        """If this utterance corrects the previous turn, learn from it."""
        ep = self.last_episode
        if not ep or not CORRECTION_PATTERNS.search(text or ""):
            return False
        with self.memory._lock:
            self.memory.db.execute("UPDATE episodes SET corrected = 1 WHERE id = ?", (ep.row_id,))
            self.memory.db.execute(
                "UPDATE skills SET successes = successes - 1 WHERE norm = ?", (ep.norm,)
            )
            self.memory.db.execute("DELETE FROM skills WHERE successes <= 0")
            self.memory.db.commit()
        action = _describe_plan(ep.plan) or (ep.reply[:160] or "replied without acting")
        lesson = await self._distill(
            DISTILL_PROMPT.format(request=ep.request, action=action, correction=text)
        )
        if lesson:
            self.add_lesson(lesson, kind="correction", trigger=ep.norm)
        return bool(lesson)

    async def note_preference(self, text: str) -> bool:
        """Standing instructions ('always ...', 'never ...') become lessons."""
        if not PREFERENCE_PATTERNS.search(text or ""):
            return False
        cleaned = re.sub(r"^(jarvis[,\s]+|please\s+)", "", (text or "").strip(), flags=re.I)
        if len(cleaned) > 200:
            return False
        self.add_lesson(cleaned.rstrip(".") + ".", kind="preference", trigger="")
        return True

    # ----------------------------------------------------------------- tools
    def record_tool(self, name: str, args: dict, result: str) -> bool:
        """Track outcomes. Returns True when this failure deserves a lesson."""
        if getattr(self.memory, "closed", False):
            return False
        failed = isinstance(result, str) and result.startswith("Error:")
        with self.memory._lock:
            self.memory.db.execute(
                "INSERT INTO tool_stats (name, calls, failures, last_error) VALUES (?,1,?,?) "
                "ON CONFLICT(name) DO UPDATE SET calls = calls + 1, failures = failures + ?, last_error = ?",
                (name, int(failed), result[:200] if failed else "", int(failed), result[:200] if failed else ""),
            )
            self.memory.db.commit()
            if not failed:
                return False
            row = self.memory.db.execute(
                "SELECT failures FROM tool_stats WHERE name = ?", (name,)
            ).fetchone()
        # One failure is noise; the same tool failing repeatedly is a pattern.
        return bool(row and row["failures"] >= 2)

    async def learn_from_tool_failure(self, name: str, args: dict, error: str) -> None:
        lesson = await self._distill(
            TOOL_LESSON_PROMPT.format(tool=name, args=json.dumps(args)[:200], error=error[:300])
        )
        if lesson:
            self.add_lesson(lesson, kind="tool", trigger=name)

    # ---------------------------------------------------------------- skills
    def _reinforce_skill(self, ep: Episode) -> None:
        """Remember a plan that worked, so an identical request can skip the model."""
        if not ep.plan or len(ep.plan) > 2 or not ep.norm:
            return
        if any(c.get("name") in UNCACHEABLE for c in ep.plan) or not plan_fits_request(ep.request, ep.plan):
            return
        plan_json = json.dumps(ep.plan)
        with self.memory._lock:
            row = self.memory.db.execute("SELECT id, plan FROM skills WHERE norm = ?", (ep.norm,)).fetchone()
            if row and row["plan"] == plan_json:
                self.memory.db.execute(
                    "UPDATE skills SET uses = uses + 1, successes = successes + 1, last_used = ? WHERE id = ?",
                    (time.time(), row["id"]),
                )
            elif row:
                self.memory.db.execute(
                    "UPDATE skills SET plan = ?, uses = 1, successes = 1, last_used = ? WHERE id = ?",
                    (plan_json, time.time(), row["id"]),
                )
            else:
                self.memory.db.execute(
                    "INSERT INTO skills (norm, request, plan, last_used) VALUES (?,?,?,?)",
                    (ep.norm, ep.request, plan_json, time.time()),
                )
            self.memory.db.commit()

    def find_skill(self, request: str) -> list[dict] | None:
        """An identical request proven twice replays directly, with no model call."""
        if not self.cfg.get("learning.skill_cache", True):
            return None
        norm = normalise(request)
        if not norm:
            return None
        with self.memory._lock:
            row = self.memory.db.execute(
                "SELECT plan, successes FROM skills WHERE norm = ? AND successes >= 2", (norm,)
            ).fetchone()
        if not row:
            return None
        try:
            plan = json.loads(row["plan"])
        except json.JSONDecodeError:
            return None
        if not plan or not plan_fits_request(request, plan):
            return None
        return plan

    def used_skill(self, request: str) -> None:
        with self.memory._lock:
            self.memory.db.execute(
                "UPDATE skills SET uses = uses + 1, last_used = ? WHERE norm = ?",
                (time.time(), normalise(request)),
            )
            self.memory.db.commit()

    # ----------------------------------------------------------------- stats
    def stats(self) -> dict[str, Any]:
        with self.memory._lock:
            tools = [dict(r) for r in self.memory.db.execute(
                "SELECT name, calls, failures FROM tool_stats ORDER BY calls DESC LIMIT 12"
            ).fetchall()]
            lessons = self.memory.db.execute("SELECT COUNT(*) c FROM lessons").fetchone()["c"]
            skills = self.memory.db.execute("SELECT COUNT(*) c FROM skills WHERE successes >= 2").fetchone()["c"]
            episodes = self.memory.db.execute("SELECT COUNT(*) c FROM episodes").fetchone()["c"]
        return {"lessons": lessons, "skills": skills, "episodes": episodes, "tools": tools}

    def recent_episodes(self, limit: int = 60) -> dict[str, Any]:
        """What Jarvis did lately, newest first: request, tools used, outcome, time taken."""
        with self.memory._lock:
            rows = self.memory.db.execute(
                "SELECT id, ts, request, plan, reply, errors, corrected, seconds FROM episodes "
                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            tools = [dict(r) for r in self.memory.db.execute(
                "SELECT name, calls, failures, last_error FROM tool_stats ORDER BY calls DESC").fetchall()]
        out = []
        for r in rows:
            item = dict(r)
            try:
                item["plan"] = [step.get("name") for step in json.loads(item["plan"] or "[]")]
            except (ValueError, AttributeError):
                item["plan"] = []
            out.append(item)
        return {"episodes": out, "tools": tools}

    # ------------------------------------------------------------- internals
    async def _distill(self, prompt: str) -> str:
        """One short local-model call. Never Claude: learning must be free."""
        try:
            if self.side_chat:
                text = await self.side_chat(prompt + "\nDo not call any tools.", 60)
            else:
                data = await self.llm.chat([{"role": "user", "content": prompt}], options={"num_predict": 60})
                text = ((data.get("message") or {}).get("content") or "").strip()
        except Exception as exc:
            log.debug("lesson distillation failed: %s", exc)
            return ""
        text = re.sub(r"^(lesson|rule|next time)[:\-\s]+", "", text, flags=re.I)
        first = text.split("\n")[0].strip().strip('"')
        return first if 8 < len(first) <= 240 else ""


# Plans whose arguments are inherently one-off, so caching them would be wrong.
UNCACHEABLE = {"ask_claude", "run_shell", "run_applescript", "file_action", "messages_send", "line_send", "computer_input"}


# Arguments that name something the user must have said: a song, an app, a person.
_SPOKEN_ARGS = {"query", "artist", "name", "recipient", "text", "url", "title", "target", "app"}


def plan_fits_request(request: str, plan: list[dict]) -> bool:
    """A cached plan may only replay when every name/title in it was actually said.

    Stops vague follow-ups ("เปิดดิ", "it still didn't open") from being bound to
    whatever the previous turn happened to call.
    """
    said = normalise(request).replace(" ", "")
    for call in plan:
        for key, value in (call.get("args") or {}).items():
            if key in _SPOKEN_ARGS and isinstance(value, str) and value.strip():
                if normalise(value).replace(" ", "") not in said:
                    return False
    return True


def _describe_plan(plan: list[dict]) -> str:
    if not plan:
        return ""
    return ", ".join(f"{c.get('name')}({json.dumps(c.get('args', {}))[:60]})" for c in plan[:3])
