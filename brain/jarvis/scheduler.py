"""Timers, alarms and reminders that survive a restart.

Anything scheduled is written to SQLite first and only then given an asyncio
task, so quitting Jarvis mid-timer does not lose it: on the next start, items
already past due fire immediately ("your 10 minute timer finished while I was
away"), and future ones are re-armed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from jarvis.memory import Memory, ScheduledItem

log = logging.getLogger("jarvis.scheduler")


class Scheduler:
    def __init__(self, memory: Memory, on_fire: Callable[[ScheduledItem, bool], Awaitable[None]]):
        self.memory = memory
        self.on_fire = on_fire
        self._tasks: dict[int, asyncio.Task] = {}

    async def start(self) -> None:
        for item in self.memory.pending_scheduled():
            self._arm(item)
        log.info("scheduler armed %d item(s)", len(self._tasks))

    def _arm(self, item: ScheduledItem) -> None:
        if item.id in self._tasks:
            return
        self._tasks[item.id] = asyncio.create_task(self._wait_and_fire(item))

    async def _wait_and_fire(self, item: ScheduledItem) -> None:
        delay = item.fire_at - time.time()
        late = delay < -2
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            self.memory.complete_scheduled(item.id)
            await self.on_fire(item, late)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("scheduled item %s failed to fire", item.id)
        finally:
            self._tasks.pop(item.id, None)

    def add(self, kind: str, fire_at: float, text: str, label: str = "") -> ScheduledItem:
        item_id = self.memory.add_scheduled(kind, fire_at, text, label)
        item = ScheduledItem(item_id, kind, fire_at, text, label)
        self._arm(item)
        return item

    def cancel(self, item_id: int) -> bool:
        task = self._tasks.pop(item_id, None)
        if task:
            task.cancel()
        return self.memory.cancel_scheduled(item_id)

    def cancel_all(self, kind: str | None = None) -> int:
        n = 0
        for item in self.pending():
            if kind is None or item.kind == kind:
                n += int(self.cancel(item.id))
        return n

    def pending(self) -> list[ScheduledItem]:
        return self.memory.pending_scheduled()

    async def stop(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        self._tasks.clear()
