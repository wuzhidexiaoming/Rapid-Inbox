from __future__ import annotations

import asyncio
from collections import deque
from typing import Any


class LiveState:
    def __init__(self, *, max_events: int = 200) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=max_events)
        self._lock = asyncio.Lock()

    async def publish(self, event: dict[str, Any]) -> None:
        payload = dict(event)
        async with self._lock:
            self._events.append(payload)

    def snapshot(self) -> list[dict[str, Any]]:
        return [dict(event) for event in self._events]
