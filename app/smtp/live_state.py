from __future__ import annotations

from collections import deque
from threading import RLock
from typing import Any
from uuid import uuid4


class LiveState:
    def __init__(self, *, max_events: int = 200) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=max_events)
        self._lock = RLock()
        self._generation = uuid4().hex
        self._next_seq = 1

    async def publish(self, event: dict[str, Any]) -> None:
        payload = dict(event)
        with self._lock:
            payload["seq"] = self._next_seq
            self._next_seq += 1
            self._events.append(payload)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(event) for event in self._events]

    def snapshot_state(self) -> tuple[list[dict[str, Any]], str]:
        with self._lock:
            events = [dict(event) for event in self._events]
            last_seq = int(events[-1].get("seq", 0)) if events else 0
            return events, self._format_cursor(last_seq)

    def snapshot_since(self, seq: int) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(event) for event in self._events if int(event.get("seq", 0)) > seq]

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
            self._generation = uuid4().hex
            self._next_seq = 1

    @property
    def generation(self) -> str:
        return self._generation

    def _format_cursor(self, seq: int) -> str:
        return f"{self._generation}:{seq}"
