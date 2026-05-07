from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA_PATH = Path(__file__).resolve().parents[2] / "sqlite_schema.sql"


def connect_database(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    apply_pragmas(connection)
    return connection


def apply_pragmas(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode = WAL;")
    connection.execute("PRAGMA foreign_keys = ON;")
    connection.execute("PRAGMA busy_timeout = 5000;")
    connection.execute("PRAGMA synchronous = FULL;")


def initialize_database(database_path: Path) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    with connect_database(database_path) as connection:
        connection.executescript(schema)
        _apply_lightweight_migrations(connection)


def _column_names(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()}


def _apply_lightweight_migrations(connection: sqlite3.Connection) -> None:
    admin_columns = _column_names(connection, "admins")
    if "must_change_password" not in admin_columns:
        connection.execute(
            """
            ALTER TABLE admins
            ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0
                CHECK (must_change_password IN (0, 1))
            """
        )
