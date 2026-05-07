from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ParseTask:
    message_id: str


class ParseQueue:
    def __init__(self, worker: Callable[[ParseTask], Awaitable[None]], *, worker_count: int = 1) -> None:
        self._worker = worker
        self._worker_count = max(1, int(worker_count))
        self._queue: asyncio.Queue[ParseTask | None] = asyncio.Queue()
        self._tasks: list[asyncio.Task[None]] = []
        self._active_message_ids: set[str] = set()
        self._active_changed = asyncio.Event()
        self._active_changed.set()

    async def start(self) -> None:
        if not self._tasks:
            self._tasks = [
                asyncio.create_task(self._run())
                for _ in range(self._worker_count)
            ]

    async def stop(self, *, discard_pending: bool = False, timeout: float | None = None) -> None:
        if not self._tasks:
            return
        tasks = list(self._tasks)
        if discard_pending:
            self.clear_pending()
        for _ in tasks:
            await self._queue.put(None)
        try:
            waiter = asyncio.gather(*tasks)
            if timeout is None:
                await waiter
            else:
                await asyncio.wait_for(waiter, timeout=timeout)
        except TimeoutError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            if all(task.done() for task in tasks):
                self._tasks = []
                self.clear_pending()

    @property
    def is_running(self) -> bool:
        return bool(self._tasks)

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

    async def wait_until_not_active(self, predicate: Callable[[str], bool]) -> None:
        while any(predicate(message_id) for message_id in self._active_message_ids):
            self._active_changed.clear()
            if not any(predicate(message_id) for message_id in self._active_message_ids):
                return
            await self._active_changed.wait()

    async def _run(self) -> None:
        while True:
            task = await self._queue.get()
            try:
                if task is None:
                    return
                self._active_message_ids.add(task.message_id)
                try:
                    await self._worker(task)
                except Exception:
                    continue
            finally:
                if task is not None:
                    self._active_message_ids.discard(task.message_id)
                    self._active_changed.set()
                self._queue.task_done()
