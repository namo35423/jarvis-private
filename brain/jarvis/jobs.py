"""Background jobs, currently meaning "hand this to Claude Code".

The local model is quick and free but shallow; Claude Code can actually do the
work - read files, run commands, iterate. Jobs run detached so Jarvis stays
available while Claude grinds, and the result is announced aloud when it lands.

The stream-json protocol is parsed for progress ("reading files", "searching
the web") so the UI can show something honest rather than a spinner.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import time
import uuid
from dataclasses import dataclass, field

from jarvis.sysutil import ToolError, run, which

log = logging.getLogger("jarvis.jobs")

NOT_LOGGED_IN = (
    "Claude Code isn't signed in on this Mac, so I can't hand that off. Open Terminal, type claude, "
    "then type slash login. You only need to do it once."
)

DELEGATION_PROMPT = """You are executing a task on behalf of Jarvis, a voice assistant running on the user's Mac. The user asked for this out loud and is waiting.

- Work autonomously: do not ask clarifying questions, make the reasonable choice and note it.
- Be efficient. Prefer the direct route over an exhaustive one.
- Your FINAL message is read aloud by a text-to-speech voice. Make it at most three sentences of plain prose: what you did and the outcome. No markdown, no lists, no code blocks, no file paths unless the user needs one.
- If you could not finish, say what blocked you in one sentence.
- Sanity-check results before reporting them. macOS may hand back an empty or partial view of ~/Downloads, ~/Desktop and ~/Documents when permission is missing, so a suspiciously small count or listing is a permissions problem, not a fact. Say so instead of reporting the number."""


@dataclass
class Job:
    id: str
    kind: str
    title: str
    task: str
    status: str = "running"  # running | done | error | cancelled
    progress: str = "Starting…"
    result: str = ""
    started: float = field(default_factory=time.time)
    finished: float = 0.0
    cost_usd: float = 0.0
    turns: int = 0
    model: str = ""
    effort: str = ""
    cwd: str = ""
    permission_mode: str = ""
    extra_args: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def as_event(self) -> dict:
        return {
            "type": "job",
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "status": self.status,
            "progress": self.progress,
            "result": self.result or None,
            "cost_usd": round(self.cost_usd, 4),
            "elapsed": round((self.finished or time.time()) - self.started, 1),
        }


class JobManager:
    def __init__(self, assistant):
        self.assistant = assistant
        self.cfg = assistant.cfg
        self.jobs: dict[str, Job] = {}
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._auth_checked = 0.0
        self._auth_ok = False

    # ------------------------------------------------------------------ util
    def binary(self) -> str | None:
        configured = (self.cfg.get("claude.binary") or "").strip()
        if configured:
            path = os.path.expanduser(configured)
            return path if os.path.exists(path) else None
        found = which("claude")
        if found:
            return found
        fallback = os.path.expanduser("~/.local/bin/claude")
        return fallback if os.path.exists(fallback) else None

    async def require_claude(self) -> None:
        """Fail in a second with a useful message, rather than start a job that can't run."""
        if not self.available():
            raise ToolError("Claude Code isn't installed on this Mac, or hand-off is switched off.")
        if self._auth_ok and time.time() - self._auth_checked < 300:
            return
        proc = await run([self.binary(), "auth", "status"], timeout=15)
        try:
            ok = bool(json.loads(proc.stdout).get("loggedIn"))
        except ValueError:
            ok = proc.ok  # unknown output: don't block on a format change
        self._auth_ok, self._auth_checked = ok, time.time()
        if not ok:
            raise ToolError(NOT_LOGGED_IN)

    def forget_auth(self) -> None:
        self._auth_ok = False

    def available(self) -> bool:
        return self.binary() is not None and self.cfg.get("claude.mode", "auto") != "off"

    def active(self) -> list[Job]:
        return [j for j in self.jobs.values() if j.status == "running"]

    def recent(self, limit: int = 10) -> list[Job]:
        return sorted(self.jobs.values(), key=lambda j: -j.started)[:limit]

    def get(self, job_id: str) -> Job | None:
        if job_id in self.jobs:
            return self.jobs[job_id]
        if job_id in ("last", "latest"):
            jobs = self.recent(1)
            return jobs[0] if jobs else None
        return None

    # ----------------------------------------------------------------- start
    async def start_claude(
        self,
        task: str,
        title: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        kind: str = "claude",
        cwd: str = "",
        permission_mode: str = "",
        extra_args: list | None = None,
        meta: dict | None = None,
    ) -> Job:
        binary = self.binary()
        if not binary:
            raise RuntimeError("The Claude Code CLI is not installed on this Mac.")
        job = Job(
            id=uuid.uuid4().hex[:8],
            kind=kind,
            cwd=cwd,
            permission_mode=permission_mode,
            extra_args=list(extra_args or []),
            meta=dict(meta or {}),
            title=title or _title_from(task),
            task=task,
            model=model or "",
            effort=effort or "",
        )
        self.jobs[job.id] = job
        self.assistant.broadcast(job.as_event())
        self._tasks[job.id] = asyncio.create_task(self._run(job, binary))
        return job

    async def _run(self, job: Job, binary: str) -> None:
        cwd = os.path.expanduser(job.cwd or self.cfg.get("claude.cwd") or "~")
        cmd = [
            binary, "-p", job.task,
            "--output-format", "stream-json",
            "--verbose",
            "--permission-mode", job.permission_mode or self.cfg.get("claude.permission_mode", "auto"),
            "--append-system-prompt", DELEGATION_PROMPT,
            "--no-session-persistence",
        ]
        model = job.model or self.cfg.get("claude.model")
        if model:
            cmd += ["--model", model]
        # Lower effort means fewer thinking tokens, which is the main credit cost.
        if job.effort:
            cmd += ["--effort", job.effort]
        if self.cfg.get("claude.max_budget_usd"):
            cmd += ["--max-budget-usd", str(self.cfg.get("claude.max_budget_usd"))]
        if self.cfg.get("claude.chrome"):
            cmd += ["--chrome"]
        cmd += job.extra_args

        log.info("job %s: %s", job.id, " ".join(shlex.quote(c) for c in cmd[:6]))
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            self._finish(job, "error", f"Could not start Claude: {exc}")
            return
        self._procs[job.id] = proc

        timeout = float(self.cfg.get("claude.timeout_s", 1800))
        try:
            await asyncio.wait_for(self._consume(job, proc), timeout=timeout)
        except asyncio.TimeoutError:
            await self._kill(job.id)
            self._finish(job, "error", "Claude took too long and was stopped.")
            return
        except asyncio.CancelledError:
            await self._kill(job.id)
            self._finish(job, "cancelled", "Cancelled.")
            raise
        finally:
            self._procs.pop(job.id, None)

    async def _consume(self, job: Job, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        final_text = ""
        is_error = False
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            try:
                event = json.loads(line.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype == "assistant":
                job.turns += 1
                progress = _progress_from_assistant(event)
                if progress:
                    job.progress = progress
                    self.assistant.broadcast(job.as_event())
            elif etype == "result":
                final_text = (event.get("result") or "").strip()
                is_error = bool(event.get("is_error"))
                job.cost_usd = float(event.get("total_cost_usd") or 0.0)

        stderr = b""
        if proc.stderr is not None:
            stderr = await proc.stderr.read()
        code = await proc.wait()

        if job.status == "cancelled":
            return
        if code != 0 and not final_text:
            detail = stderr.decode("utf-8", "replace").strip().splitlines()
            msg = detail[-1] if detail else f"Claude exited with status {code}."
            self._finish(job, "error", _humanise_error(msg))
            return
        if is_error and not final_text:
            self._finish(job, "error", "Claude could not finish that.")
            return
        self._finish(job, "done", final_text or "Claude finished, but said nothing.")

    def _finish(self, job: Job, status: str, result: str) -> None:
        job.status = status
        job.result = result
        job.finished = time.time()
        job.progress = "Finished" if status == "done" else status.title()
        self.assistant.broadcast(job.as_event())
        self.assistant.on_job_finished(job)

    # ---------------------------------------------------------------- cancel
    async def _kill(self, job_id: str) -> None:
        proc = self._procs.get(job_id)
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()

    async def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job or job.status != "running":
            return False
        job.status = "cancelled"
        task = self._tasks.get(job.id)
        await self._kill(job.id)
        if task and not task.done():
            task.cancel()
        self._finish(job, "cancelled", "Cancelled.")
        return True

    async def cancel_all(self) -> int:
        return sum([await self.cancel(j.id) for j in self.active()])


def _title_from(task: str) -> str:
    words = (task or "").strip().split()
    return " ".join(words[:7]) + ("…" if len(words) > 7 else "")


_TOOL_PHRASES = {
    "Bash": "Running commands",
    "Read": "Reading files",
    "Edit": "Editing files",
    "Write": "Writing files",
    "Glob": "Looking through files",
    "Grep": "Searching the code",
    "WebSearch": "Searching the web",
    "WebFetch": "Reading a web page",
    "Task": "Working through it",
    "TodoWrite": "Planning",
}


def _progress_from_assistant(event: dict) -> str:
    content = ((event.get("message") or {}).get("content")) or []
    texts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use":
            name = block.get("name", "")
            return _TOOL_PHRASES.get(name, f"Using {name}")
        if block.get("type") == "text" and block.get("text"):
            texts.append(block["text"].strip())
    if texts:
        first = texts[0].split("\n")[0]
        return first[:90]
    return ""


def _humanise_error(msg: str) -> str:
    low = msg.lower()
    if "usage limit" in low or "rate limit" in low:
        return "Claude has hit its usage limit for now."
    if "not logged in" in low or "authentication" in low or "unauthorized" in low:
        return "Claude Code is not signed in on this Mac."
    if "permission" in low:
        return "Claude needed a permission it does not have in background mode."
    return msg[:200]
