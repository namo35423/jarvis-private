"""Getting macOS to ask its permission questions, on purpose.

Automation grants are per-target-app and only prompted at the moment Jarvis
first tries to control each one. Discovering that one command at a time is
miserable, so `jarvis grant` walks the list while the user is actually at the
keyboard and can answer.

Apps that are not running are skipped rather than launched - the prompt for
those will come the first time they are genuinely used.
"""

from __future__ import annotations

import asyncio
import logging

from jarvis.sysutil import ToolError, _automation_denied, osascript, run

log = logging.getLogger("jarvis.permissions")

# Probes chosen to be read-only and side-effect free.
PROBES: list[tuple[str, str, str]] = [
    ("Google Chrome", "Google Chrome", 'tell application "Google Chrome" to return count of windows'),
    ("Spotify", "Spotify", 'tell application "Spotify" to return player state as text'),
    ("Music", "Music", 'tell application "Music" to return player state as text'),
    ("Calendar", "Calendar", 'tell application "Calendar" to return name of first calendar'),
    ("Reminders", "Reminders", 'tell application "Reminders" to return name of first list'),
    ("Notes", "Notes", 'tell application "Notes" to return count of notes'),
    ("Messages", "Messages", 'tell application "Messages" to return count of chats'),
    ("Finder", "Finder", 'tell application "Finder" to return name of home'),
    ("System Events", "System Events", 'tell application "System Events" to return count of processes'),
]

# These have no separate process to check for, or are always available.
ALWAYS_PROBE = {"Finder", "System Events"}


async def _running(process_name: str) -> bool:
    if process_name in ALWAYS_PROBE:
        return True
    proc = await run(["/usr/bin/pgrep", "-x", process_name], timeout=5)
    return bool(proc.stdout.strip())


async def probe_automation(force: bool = False) -> dict[str, str]:
    """Touch each app so macOS raises its prompt. Returns app -> outcome."""
    results: dict[str, str] = {}
    for app, process_name, script in PROBES:
        if not force and not await _running(process_name):
            results[app] = "skipped (not running)"
            continue
        _automation_denied.discard(app)  # a fresh chance to be granted
        try:
            # Generous: the user may take a few seconds to read the dialog.
            await osascript(script, timeout=45)
            results[app] = "granted"
        except ToolError as exc:
            results[app] = "blocked" if "permission" in str(exc).lower() else f"error: {exc}"
        await asyncio.sleep(0.4)  # let one dialog finish before raising the next
    return results


def summarise(results: dict[str, str]) -> str:
    granted = [a for a, r in results.items() if r == "granted"]
    blocked = [a for a, r in results.items() if r.startswith("blocked")]
    skipped = [a for a, r in results.items() if r.startswith("skipped")]
    parts = []
    if granted:
        parts.append(f"Allowed: {', '.join(granted)}.")
    if blocked:
        parts.append(
            f"Still blocked: {', '.join(blocked)} - turn them on in System Settings, "
            "Privacy & Security, Automation, under Jarvis."
        )
    if skipped:
        parts.append(f"Not running, so not asked yet: {', '.join(skipped)}.")
    return " ".join(parts) or "Nothing to check."


async def preflight() -> dict[str, str]:
    """Quietly discover which running apps we may control.

    Runs in the background at startup with short timeouts. The point is that a
    blocked app is already known by the time the user asks for it, so they get
    "grant Jarvis permission to control Spotify" immediately rather than after
    a ten second stall.
    """
    targets = [(app, proc, script) for app, proc, script in PROBES if app not in ALWAYS_PROBE]
    running = await asyncio.gather(*(_running(proc) for _, proc, _ in targets))

    async def probe(app: str, script: str) -> tuple[str, str]:
        try:
            await osascript(script, timeout=3.0)
            return app, "granted"
        except ToolError:
            return app, "blocked"

    jobs = [probe(app, script) for (app, _, script), up in zip(targets, running) if up]
    if not jobs:
        return {}
    results = dict(await asyncio.gather(*jobs))
    blocked = [a for a, r in results.items() if r == "blocked"]
    if blocked:
        log.info("automation blocked for: %s (run 'jarvis grant' to fix)", ", ".join(blocked))
    return results
