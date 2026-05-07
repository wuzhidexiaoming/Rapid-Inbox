from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from app.db.connection import connect_database


T = TypeVar("T")


class DatabaseWriter:
    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._lock = threading.Lock()

    def _execute_sync(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        with self._lock:
            with connect_database(self._database_path) as connection:
                try:
                    result = operation(connection)
                except Exception:
                    connection.rollback()
                    raise
                connection.commit()
                return result

    def _execute_maintenance_sync(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        with self._lock:
            with connect_database(self._database_path) as connection:
                return operation(connection)

    async def execute(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        return await asyncio.to_thread(self._execute_sync, operation)

    async def execute_maintenance(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        return await asyncio.to_thread(self._execute_maintenance_sync, operation)
