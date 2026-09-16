#!/usr/bin/env python
"""Try a single tool without starting the whole assistant.

    .venv/bin/python tools_probe.py                          # list every tool
    .venv/bin/python tools_probe.py open_app '{"name":"Spotify"}'
    .venv/bin/python tools_probe.py --yes move_to_trash '{"path":"/tmp/x"}'

--yes auto-approves confirmation prompts (otherwise they are declined, so a
risky tool is never run by accident from the probe).
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

from jarvis.config import Config, ensure_dirs
from jarvis.memory import Memory, ScheduledItem
from jarvis.scheduler import Scheduler
from jarvis.tools import REGISTRY, ToolContext, execute, load_all


class ProbeAssistant:
    """The smallest object that satisfies ToolContext."""

    def __init__(self, auto_yes: bool = False):
        ensure_dirs()
        self.cfg = Config()
        self.memory = Memory()
        self.scheduler = Scheduler(self.memory, self._fired)
        self.auto_yes = auto_yes
        try:
            from jarvis.jobs import JobManager

            self.jobs = JobManager(self)
        except Exception:  # jobs module not needed by most tools
            self.jobs = None

    async def _fired(self, item: ScheduledItem, late: bool) -> None:
        print(f"[scheduler fired] {item.kind}: {item.text} (late={late})")

    def speak_async(self, text: str) -> None:
        print(f"[say] {text}")

    def broadcast(self, event: dict) -> None:
        print(f"[ui] {json.dumps(event)[:160]}")

    async def request_confirmation(self, prompt: str, detail: str = "") -> bool:
        print(f"[confirm] {prompt} {detail} -> {'YES' if self.auto_yes else 'NO'}")
        return self.auto_yes


async def main() -> int:
    args = [a for a in sys.argv[1:]]
    auto_yes = "--yes" in args
    args = [a for a in args if a != "--yes"]

    load_all()
    if not args:
        for name in sorted(REGISTRY):
            spec = REGISTRY[name]
            risky = " [confirms]" if spec.risk else ""
            print(f"{name:22} {spec.category:12} {spec.description[:70]}{risky}")
        print(f"\n{len(REGISTRY)} tools")
        return 0

    name = args[0]
    payload = json.loads(args[1]) if len(args) > 1 else {}
    assistant = ProbeAssistant(auto_yes)
    ctx = ToolContext(assistant)
    await assistant.scheduler.start()
    t0 = time.time()
    result = await execute(ctx, name, payload)
    print(f"\n--- {name} took {time.time() - t0:.2f}s ---")
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
