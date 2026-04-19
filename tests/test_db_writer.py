from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from pathlib import Path

from app.db.connection import connect_database, initialize_database
from app.db.writer import DatabaseWriter


def _count_probe_rows(database_path: Path) -> int:
    with connect_database(database_path) as connection:
        row = connection.execute("SELECT COUNT(*) AS count FROM writer_probe").fetchone()
    return int(row["count"])


async def _insert_probe_row(writer: DatabaseWriter, value: str) -> None:
    def operation(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS writer_probe (
                value TEXT NOT NULL
            )
            """
        )
        connection.execute("INSERT INTO writer_probe (value) VALUES (?)", (value,))

    await writer.execute(operation)


def test_database_writer_supports_calls_from_different_event_loops(tmp_path: Path) -> None:
    database_path = tmp_path / "storage" / "app.db"
    initialize_database(database_path)
    writer = DatabaseWriter(database_path)

    started = threading.Event()
    thread_error: list[BaseException] = []

    async def hold_writer_lock() -> None:
        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS writer_probe (
                    value TEXT NOT NULL
                )
                """
            )
            connection.execute("INSERT INTO writer_probe (value) VALUES (?)", ("thread-loop",))
            started.set()
            time.sleep(0.2)

        await writer.execute(operation)

    def run_first_loop() -> None:
        try:
            asyncio.run(hold_writer_lock())
        except BaseException as exc:  # noqa: BLE001
            thread_error.append(exc)

    thread = threading.Thread(target=run_first_loop)
    thread.start()
    assert started.wait(timeout=2.0)

    asyncio.run(asyncio.wait_for(_insert_probe_row(writer, "main-loop"), timeout=1.0))
    thread.join()
    assert thread_error == []
    assert _count_probe_rows(database_path) == 2
