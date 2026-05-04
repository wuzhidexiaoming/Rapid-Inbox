from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ParseTask:
    message_id: str


class ParseQueue:
    def __init__(self, worker: Callable[[ParseTask], Awaitable[None]]) -> None:
        self._worker = worker
        self._queue: asyncio.Queue[ParseTask | None] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        await self._queue.put(None)
        await self._task
        self._task = None

    @property
    def is_running(self) -> bool:
        return self._task is not None

    async def enqueue(self, task: ParseTask) -> None:
        await self._queue.put(task)

    async def drain(self) -> None:
        await self._queue.join()

    def clear_pending(self) -> int:
        return self.remove_pending(lambda _task: True)

    def remove_pending(self, predicate: Callable[[ParseTask], bool]) -> int:
        cleared = 0
        retained: list[ParseTask | None] = []
        while True:
            try:
                task = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                for retained_task in retained:
                    self._queue.put_nowait(retained_task)
                return cleared

            if task is not None and predicate(task):
                cleared += 1
            else:
                retained.append(task)
            self._queue.task_done()

    async def _run(self) -> None:
        while True:
            task = await self._queue.get()
            try:
                if task is None:
                    return
                try:
                    await self._worker(task)
                except Exception:
                    continue
            finally:
                self._queue.task_done()
