"""The escape hatches: arbitrary shell commands and arbitrary AppleScript.

These make Jarvis able to do things nobody wrote a tool for. They are also the
two most dangerous things it can do, so a pattern check classifies each call and
anything destructive, privileged or outward-facing has to be confirmed aloud.
"""

from __future__ import annotations

import re

from jarvis.sysutil import ToolError, osascript, run_shell as _run_shell, truncate
from jarvis.tools import ToolContext, tool

# Ordered by how bad the surprise would be.
DANGEROUS = [
    (re.compile(r"\brm\s+(-[a-zA-Z]*\s+)*", re.I), "delete files"),
    (re.compile(r"\b(shred|srm|unlink)\b", re.I), "destroy files"),
    (re.compile(r"\bsudo\b", re.I), "run as administrator"),
    (re.compile(r"\b(dd|mkfs|diskutil\s+(erase|partition|reformat))\b", re.I), "write to a disk"),
    (re.compile(r"\b(shutdown|reboot|halt)\b", re.I), "shut the Mac down"),
    (re.compile(r"\bgit\s+(push\s+.*--force|reset\s+--hard|clean\s+-[a-z]*f)", re.I), "discard git work"),
    (re.compile(r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(bash|sh|zsh)", re.I), "download and run a script"),
    (re.compile(r">\s*/dev/(disk|rdisk)", re.I), "overwrite a disk device"),
    (re.compile(r"\bmv\b.*\s+/(\s|$)", re.I), "move something to the root of the disk"),
    (re.compile(r"\bfind\b.*-delete\b", re.I), "delete files in bulk"),
    (re.compile(r":\(\)\s*\{.*\|\s*:", re.I), "fork-bomb this Mac"),
]


def classify_shell(command: str) -> str | None:
    """Return a confirmation question when the command looks destructive."""
    cmd = (command or "").strip()
    if not cmd:
        return None
    for pattern, what in DANGEROUS:
        if pattern.search(cmd):
            short = cmd if len(cmd) <= 90 else cmd[:87] + "..."
            return f"That will {what}. Run it? The command is: {short}"
    return None


def _confirm_shell(args: dict) -> str | None:
    return classify_shell(args.get("command", "")) or "อนุญาตให้รันคำสั่งนี้ไหมครับ: " + args.get("command", "")


APPLESCRIPT_DANGEROUS = re.compile(
    r"\b(delete|erase|empty (the )?trash|shut down|restart|log out|send|quit application \"(Finder|System)\")\b", re.I
)


def _confirm_applescript(args: dict) -> str | None:
    script = args.get("script", "")
    if APPLESCRIPT_DANGEROUS.search(script):
        return "That script will delete, send or shut something down. Run it?"
    return "อนุญาตให้รัน AppleScript นี้ไหมครับ: " + script


@tool(
    name="run_shell",
    description=(
        "Run a shell command on this Mac and return its output. Use this when no other tool fits: "
        "checking disk space, counting files, running a script, git, homebrew-free CLI work. "
        "Prefer the specific tools (file_action, find_files, system_control) when they apply."
    ),
    params={
        "command": {"type": "string", "description": "The command line, e.g. 'df -h /' or 'ls ~/Downloads | wc -l'"},
        "working_dir": {"type": "string", "description": "Directory to run in, defaults to the home folder"},
    },
    required=["command"],
    risk=_confirm_shell,
    category="system",
)
async def run_shell(ctx: ToolContext, command: str, working_dir: str = "") -> str:
    import os

    cwd = os.path.expanduser(working_dir) if working_dir else os.path.expanduser("~")
    if not os.path.isdir(cwd):
        cwd = os.path.expanduser("~")
    proc = await _run_shell(command, timeout=60, cwd=cwd)
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        detail = err or out or f"exit status {proc.returncode}"
        return f"Command failed ({proc.returncode}): {truncate(detail, 600)}"
    if not out:
        return "Done, with no output."
    return truncate(out, 2500)


@tool(
    name="run_applescript",
    description=(
        "Run AppleScript to control a Mac app that has no dedicated tool here - for example setting "
        "something in Mail, Finder, Keynote, Preview or Safari. Use when a specific app must be scripted."
    ),
    params={
        "script": {"type": "string", "description": "The AppleScript source, e.g. 'tell application \"Finder\" to get name of every window'"},
    },
    required=["script"],
    risk=_confirm_applescript,
    category="system",
)
async def run_applescript(ctx: ToolContext, script: str) -> str:
    if not (script or "").strip():
        raise ToolError("empty script")
    out = await osascript(script, timeout=45)
    return truncate(out or "Done.", 2000)
