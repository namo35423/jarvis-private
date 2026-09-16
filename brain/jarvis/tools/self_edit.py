"""Jarvis changing its own code, safely.

Flow for "make yourself X":
  1. lock + checkpoint   one edit at a time (persisted, so a crash can't leak it);
                         commit the tree so there is a known-good state
  2. edit                Claude Code edits files; the only shell commands it may run
                         are the verify script and the Swift build
  3. verify              Jarvis checks the result itself: loads, tools register,
                         Swift builds, and the safety net was not touched
  4. apply               a guard - run from the CHECKPOINT's copy, in its own session -
                         restarts Jarvis and waits for a new process to stay healthy
  5. rollback            restores only the files the edit touched, as a new commit;
                         nothing else in the repo is ever reset or cleaned

A voice assistant that can edit itself must never be able to brick itself, or
take unrelated work down with a bad edit.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from jarvis.config import JARVIS_DIR, LOGS_DIR, PROJECT_DIR
from jarvis.sysutil import ToolError, run
from jarvis.tools import ToolContext, tool

ROOT = PROJECT_DIR
PENDING = JARVIS_DIR / "self_edit_pending.json"
MARKER = JARVIS_DIR / "self_edit.json"
ROLLBACK_MARKER = JARVIS_DIR / "self_edit_rollback.json"
PATHS_FILE = JARVIS_DIR / "self_edit_paths.txt"
GUARD = ROOT / "scripts" / "self_edit_guard.sh"
VERIFY = ROOT / "brain" / "verify.sh"

# The machinery that protects against a bad edit may not be changed by an edit.
PROTECTED = {"brain/jarvis/tools/self_edit.py", "scripts/self_edit_guard.sh", "brain/verify.sh"}

GITIGNORE = """.venv/
__pycache__/
*.pyc
app/build/
app/Resources/AppIcon.icns
.DS_Store
*.log
"""

BRIEF = """You are editing the source code of Jarvis, the user's local voice assistant, in {root}.
The user asked Jarvis, out loud, to change itself:

    "{request}"

Rules:
- Read {root}/CLAUDE.md first. It explains the architecture and the traps.
- Make the smallest change that fully does what was asked, in the style of the surrounding code.
- Behaviour defaults live in brain/jarvis/config.py; spoken style in brain/jarvis/prompts.py;
  instant commands in brain/jarvis/core.py (FAST_TOOLS); tools in brain/jarvis/tools/; the
  panel's look in ui/style.css; the menu bar app in app/Sources/.
- Never modify brain/jarvis/tools/self_edit.py, scripts/self_edit_guard.sh or brain/verify.sh -
  a change to them is rejected automatically.
- When done, check your work by running exactly: {root}/brain/verify.sh
  If you changed Swift under app/, also run exactly: {root}/app/build.sh
- Do not restart Jarvis or use git. Jarvis verifies, applies and can roll back the change itself.
- End with one or two plain sentences saying what you changed, to be read aloud."""


# ------------------------------------------------------------------- git
def _git(*args: str, check: bool = True, timeout: float = 30) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["/usr/bin/git", "-C", str(ROOT), "-c", "user.name=Jarvis", "-c", "user.email=jarvis@localhost", *args],
        capture_output=True, text=True, timeout=timeout,
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or ["unknown error"]
        raise ToolError(f"git {args[0]} failed: {detail[0]}")
    return proc


def ensure_repo() -> None:
    if (ROOT / ".git").exists():
        return
    (ROOT / ".gitignore").write_text(GITIGNORE)
    _git("init", "-q")
    _git("add", "-A")
    _git("commit", "-q", "-m", "baseline before Jarvis could edit itself")


def head() -> str:
    return _git("rev-parse", "HEAD").stdout.strip()


def checkpoint(label: str) -> str:
    """Commit whatever is there now; that commit is what a rollback returns to."""
    ensure_repo()
    _git("add", "-A")
    _git("commit", "-q", "--allow-empty", "-m", f"checkpoint before self-edit: {label[:120]}")
    return head()


def changed_since(commit: str) -> list[str]:
    tracked = _git("diff", "--name-only", commit).stdout.split("\n")
    untracked = _git("ls-files", "--others", "--exclude-standard").stdout.split("\n")
    return sorted({p.strip() for p in tracked + untracked if p.strip()})


def restore(commit: str, paths: list[str]) -> None:
    """Put exactly these paths back as they were at `commit`. Nothing else is touched."""
    for path in paths:
        if _git("cat-file", "-e", f"{commit}:{path}", check=False).returncode == 0:
            _git("checkout", commit, "--", path)
        else:
            (ROOT / path).unlink(missing_ok=True)


def commit_paths(message: str, paths: list[str]) -> str:
    _git("add", "-A", "--", *paths)
    _git("commit", "-q", "-m", message[:200], "--", *paths)
    return head()


# ------------------------------------------------------------------ lock
def busy() -> bool:
    return PENDING.exists()


def begin(request: str) -> str:
    if busy():
        raise ToolError("I'm already in the middle of changing myself. Let that finish first.")
    sha = checkpoint(request)
    PENDING.write_text(json.dumps({"checkpoint": sha, "request": request, "started": time.time()}))
    return sha


def end() -> None:
    PENDING.unlink(missing_ok=True)


def take_marker(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    path.unlink(missing_ok=True)
    return data


# ---------------------------------------------------------------- verify
async def verify(changed: list[str]) -> tuple[bool, str]:
    """Would the new code start? Checked before anything is restarted."""
    touched = PROTECTED.intersection(changed)
    if touched:
        return False, f"it tried to change my safety net ({', '.join(sorted(touched))})"
    proc = await run(["/bin/bash", str(VERIFY)], cwd=str(ROOT / "brain"), timeout=120)
    if not proc.ok:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-4:]
        return False, "\n".join(tail) or "the new code does not load"
    if any(f.startswith("app/") for f in changed):
        build = await run([str(ROOT / "app" / "build.sh")], cwd=str(ROOT / "app"), timeout=400)
        if not build.ok or "error:" in (build.stdout + build.stderr):
            return False, "the menu bar app no longer compiles"
    return True, ""


def launch_guard(checkpoint_sha: str, paths: list[str], swift: bool, port: int) -> None:
    """Start the restart-and-rollback guard in its own session.

    It runs the guard script as it was at the checkpoint, so no edit can change
    the code that decides whether to roll that edit back.
    """
    PATHS_FILE.write_text("\n".join(paths) + "\n")
    script = _git("show", f"{checkpoint_sha}:scripts/self_edit_guard.sh", check=False).stdout
    runner = JARVIS_DIR / "self_edit_guard.run.sh"
    runner.write_text(script or GUARD.read_text())
    log = open(LOGS_DIR / "self_edit.log", "a")
    subprocess.Popen(
        ["/bin/bash", str(runner), checkpoint_sha, "app" if swift else "brain",
         str(os.getpid()), str(port), str(ROOT), str(JARVIS_DIR)],
        stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True,
    )


# ------------------------------------------------------------------ tool
@tool(
    name="improve_self",
    description=(
        "Change Jarvis's OWN code: its behaviour, features, voice, commands or look. Use when the user "
        "asks you to change, fix, upgrade or add something to yourself, or to undo your last change."
    ),
    params={
        "request": {"type": "string", "description": "Exactly what the user wants changed about you, with every detail they gave"},
        "undo": {"type": "boolean", "description": "True to roll back your most recent self-change instead"},
    },
    required=[],
    category="self",
)
async def improve_self(ctx: ToolContext, request: str = "", undo: bool = False) -> str:
    if busy():
        raise ToolError("I'm already in the middle of changing myself. Let that finish first.")

    if undo:
        last = ctx.memory.get_kv("self_edit_last")
        if not last:
            raise ToolError("I haven't changed my own code yet, so there is nothing to undo.")
        if head() != last["commit"] or _git("status", "--porcelain").stdout.strip():
            raise ToolError(
                "My code has changed since that edit, so undoing it blindly could break something. "
                "Ask Claude Code to revert it by hand."
            )
        _git("revert", "--no-edit", last["commit"])
        ctx.memory.set_kv("self_edit_last", None)
        MARKER.write_text(json.dumps({"request": f"undo: {last['request']}", "summary": "I've undone my last change."}))
        paths = last.get("changed", [])
        ctx.assistant.restart_after_reply(last["commit"], paths, swift=any(p.startswith("app/") for p in paths))
        return "Undoing my last change and restarting. Back in a moment."

    if not request.strip():
        raise ToolError("say what you want me to change about myself")
    await ctx.jobs.require_claude()

    sha = begin(request.strip())
    root = str(ROOT)
    try:
        await ctx.jobs.start_claude(
            BRIEF.format(root=root, request=request.strip()),
            title=f"Changing myself: {request.strip()[:50]}",
            model=ctx.cfg.get("claude.model") or "sonnet",
            effort="medium",
            kind="self_edit",
            cwd=root,
            permission_mode="acceptEdits",
            extra_args=[
                "--add-dir", root,
                "--allowedTools", "Read", "Edit", "Write", "Glob", "Grep",
                f"Bash({root}/brain/verify.sh)", f"Bash({root}/app/build.sh)",
            ],
            meta={"checkpoint": sha, "request": request.strip()},
        )
    except Exception:
        end()
        raise
    return "On it. I'll make that change to myself, check it, and tell you when I'm back."
