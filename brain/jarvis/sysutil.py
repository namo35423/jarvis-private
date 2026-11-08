"""Shared helpers for talking to macOS: subprocesses, AppleScript, app lookup."""

from __future__ import annotations

import asyncio
import difflib
import os
import plistlib
import re
import shutil
import unicodedata
from dataclasses import dataclass
from pathlib import Path

APP_DIRS = [
    Path("/Applications"),
    Path("/Applications/Utilities"),
    Path("/System/Applications"),
    Path("/System/Applications/Utilities"),
    Path.home() / "Applications",
]


class ToolError(Exception):
    """Expected failure. The message is handed back to the model verbatim."""


@dataclass
class Proc:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def check(self, what: str = "command") -> str:
        if not self.ok:
            msg = (self.stderr or self.stdout or "").strip().splitlines()
            detail = msg[-1] if msg else f"exit {self.returncode}"
            raise ToolError(f"{what} failed: {detail}")
        return self.stdout.strip()


async def run(
    cmd: list[str],
    *,
    timeout: float = 30.0,
    stdin: str | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> Proc:
    """Run a command without a shell. Never raises on non-zero exit."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env={**os.environ, **(env or {})} if env else None,
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(stdin.encode() if stdin is not None else None), timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ToolError(f"timed out after {timeout:.0f}s: {' '.join(cmd[:3])}")
    return Proc(proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))


async def run_shell(command: str, *, timeout: float = 30.0, cwd: str | None = None) -> Proc:
    """Run a command line through zsh (for pipes, globs, redirection)."""
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
        cwd=cwd,
        executable="/bin/zsh",
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ToolError(f"command timed out after {timeout:.0f}s")
    return Proc(proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))


_AS_ERR = re.compile(r"execution error: (.*?)(?: \(-?\d+\))?$", re.S | re.M)
# -1743 is TCC refusing Apple Events; -600/-609 mean the app is not running.
_NOT_AUTHORISED = re.compile(r"-1743|not authori[sz]ed to send apple events", re.I)

# Apps we have already been refused control of, so we fail fast instead of
# waiting on a prompt that will not appear again this session.
_automation_denied: set[str] = set()


def automation_denied(app: str) -> bool:
    return app in _automation_denied


def automation_help(app: str) -> str:
    return (
        f"macOS has not given Jarvis permission to control {app}. Open System Settings, "
        f"Privacy & Security, Automation, find Jarvis and switch on {app}."
    )


async def osascript(script: str, *, language: str = "AppleScript", timeout: float = 10.0) -> str:
    """Run AppleScript (or JavaScript for Automation) and return stdout.

    Raises ToolError with the AppleScript error text, which is usually specific
    enough for the model to correct itself (e.g. "Application isn't running").
    """
    app = _target_app(script)
    if app and app in _automation_denied:
        raise ToolError(automation_help(app))

    cmd = ["/usr/bin/osascript"]
    if language.lower().startswith("j"):
        cmd += ["-l", "JavaScript"]
    cmd += ["-"]
    try:
        proc = await run(cmd, timeout=timeout, stdin=script)
    except ToolError as exc:
        # A timeout is either a permission prompt still waiting or the app being
        # busy. Say both, but do not remember it as a refusal: one slow reply must
        # not block every later command until restart. Only -1743 is a real "no".
        if app:
            raise ToolError(
                f"{app} did not respond in time. If macOS is asking whether Jarvis may control {app}, "
                f"allow it; otherwise try again."
            ) from exc
        raise
    if not proc.ok:
        err = proc.stderr.strip()
        if _NOT_AUTHORISED.search(err):
            app = _target_app(script) or "that app"
            _automation_denied.add(app)
            raise ToolError(automation_help(app))
        m = _AS_ERR.search(err)
        raise ToolError(m.group(1).strip() if m else (err or "AppleScript failed"))
    return proc.stdout.strip()


_TELL_APP = re.compile(r'tell application "([^"]+)"')


def _target_app(script: str) -> str | None:
    m = _TELL_APP.search(script or "")
    return m.group(1) if m else None


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", s.lower())


_app_cache: dict[str, str] | None = None


def _scan_apps() -> dict[str, str]:
    global _app_cache
    if _app_cache is not None:
        return _app_cache
    apps: dict[str, str] = {}
    for d in APP_DIRS:
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.suffix == ".app":
                apps.setdefault(p.stem, str(p))
            elif p.is_dir():  # one level down, e.g. /Applications/Chrome Apps.localized
                try:
                    for q in p.iterdir():
                        if q.suffix == ".app":
                            apps.setdefault(q.stem, str(q))
                except OSError:
                    pass
    _app_cache = apps
    return apps


def find_app(name: str) -> str | None:
    """Fuzzy-match a spoken app name to an installed .app name.

    Speech recognition gives us "spotify", "google chrome", "vs code" — none of
    which are exact bundle names, so exact match is tried first, then prefix,
    then substring, then difflib.
    """
    apps = _scan_apps()
    if not name:
        return None
    target = _norm(name)
    if not target:
        return None
    byn = {_norm(k): k for k in apps}
    if target in byn:
        return byn[target]
    ALIASES = {
        "chrome": "googlechrome",
        "browser": "googlechrome",
        "vscode": "visualstudiocode",
        "vs": "visualstudiocode",
        "code": "visualstudiocode",
        "settings": "systemsettings",
        "preferences": "systemsettings",
        "systempreferences": "systemsettings",
        "activitymonitor": "activitymonitor",
        "terminal": "terminal",
        "music": "music",
        "applemusic": "music",
        "imessage": "messages",
        "texts": "messages",
        "email": "mail",
        "calender": "calendar",
        "excel": "microsoftexcel",
        "word": "microsoftword",
        "powerpoint": "microsoftpowerpoint",
        "claude": "claude",
        "chatgpt": "chatgpt",
    }
    if target in ALIASES and ALIASES[target] in byn:
        return byn[ALIASES[target]]
    starts = [k for k in byn if k.startswith(target)]
    if starts:
        return byn[min(starts, key=len)]
    subs = [k for k in byn if target in k or k in target]
    if subs:
        return byn[min(subs, key=len)]
    close = difflib.get_close_matches(target, list(byn), n=1, cutoff=0.72)
    return byn[close[0]] if close else None


def app_path(name: str) -> str | None:
    app = find_app(name)
    return _scan_apps().get(app) if app else None


def bundle_id(app_name: str) -> str | None:
    p = app_path(app_name)
    if not p:
        return None
    try:
        with open(Path(p) / "Contents" / "Info.plist", "rb") as fh:
            return plistlib.load(fh).get("CFBundleIdentifier")
    except (OSError, plistlib.InvalidFileException):
        return None


def truncate(text: str, limit: int = 4000, note: str = "\n…[truncated]") -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + note


def which(binary: str) -> str | None:
    return shutil.which(binary, path=os.environ.get("PATH", "") + f":{Path.home()}/.local/bin:/usr/local/bin:/opt/homebrew/bin")


def applescript_quote(s: str) -> str:
    """Quote a Python string for embedding inside an AppleScript string literal."""
    return '"' + (s or "").replace("\\", "\\\\").replace('"', '\\"') + '"'
