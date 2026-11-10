"""One owned exact inventory read, reconnectable without evidence caching or retries."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from threading import Event
from typing import Any
from uuid import uuid4

from .async_work import run_thread_owned


class InventoryCheck:
    def __init__(self) -> None:
        self.task: asyncio.Task | None = None
        self.key: tuple | None = None
        self.check_id: str | None = None
        self.cancel = Event()
        self.progress = "Checking inventory"
        self.lock = asyncio.Lock()

    async def poll(
        self, key: tuple, check_id: str | None,
        read: Callable[[Event, Callable[[str], None]], dict[str, Any]],
    ) -> dict[str, Any]:
        async with self.lock:
            # A supplied ID requests that particular snapshot, not a time-based cache.
            reuse = self.task is not None and self.key == key and (
                not self.task.done() or check_id == self.check_id
            )
            if not reuse:
                await self._drain()
                self.key, self.check_id = key, uuid4().hex
                self.cancel = Event()
                self.progress = "Checking inventory"
                loop = asyncio.get_running_loop()
                identity = self.check_id

                def publish(message: str) -> None:
                    if identity == self.check_id:
                        self.progress = message

                self.task = asyncio.create_task(run_thread_owned(
                    read, self.cancel,
                    lambda message: loop.call_soon_threadsafe(publish, message),
                ))
            payload = {"inventory_check_id": self.check_id,
                       "inventory_progress": self.progress, "inventory_pending": True}
            if self.task.done():
                try:
                    result = self.task.result()
                except (Exception, asyncio.CancelledError):
                    payload["inventory_error"] = True
                else:
                    payload.update(result, inventory_pending=False)
            return payload

    async def shutdown(self) -> None:
        async with self.lock:
            await self._drain()

    async def _drain(self) -> None:
        if self.task is not None:
            self.cancel.set()
            with suppress(Exception):
                await asyncio.shield(self.task)
            self.task = None
