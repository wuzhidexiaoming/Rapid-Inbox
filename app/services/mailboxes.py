from __future__ import annotations

import sqlite3
from typing import Any

from app.db.connection import connect_database


class MailboxService:
    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    def list_mailboxes(self, *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        with connect_database(self._runtime.settings.database_path) as connection:
            rows = connection.execute(
                """
                SELECT
                    m.id,
                    m.domain_id,
                    d.root_domain_ascii,
                    m.local_part_canonical,
                    m.rcpt_domain_ascii,
                    m.address_canonical,
                    m.address_display,
                    m.first_seen_at,
                    m.last_seen_at,
                    m.latest_message_at,
                    m.message_count,
                    m.public_enabled,
                    m.is_hidden,
                    m.notes
                FROM mailboxes AS m
                JOIN domains AS d ON d.id = m.domain_id
                ORDER BY m.latest_message_at DESC, m.id DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return {"items": [self._normalize_mailbox_row(row) for row in rows]}

    def get_mailbox(self, mailbox_id: int) -> dict[str, Any]:
        with connect_database(self._runtime.settings.database_path) as connection:
            row = connection.execute(
                """
                SELECT
                    m.id,
                    m.domain_id,
                    d.root_domain_ascii,
                    m.local_part_canonical,
                    m.rcpt_domain_ascii,
                    m.address_canonical,
                    m.address_display,
                    m.first_seen_at,
                    m.last_seen_at,
                    m.latest_message_at,
                    m.message_count,
                    m.public_enabled,
                    m.is_hidden,
                    m.notes
                FROM mailboxes AS m
                JOIN domains AS d ON d.id = m.domain_id
                WHERE m.id = ?
                """,
                (mailbox_id,),
            ).fetchone()
        if row is None:
            raise LookupError("mailbox not found")
        return self._normalize_mailbox_row(row)

    async def update_mailbox(self, mailbox_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("invalid mailbox payload")

        assignments: list[str] = []
        values: list[Any] = []
        if "public_enabled" in payload:
            assignments.append("public_enabled = ?")
            values.append(int(bool(payload["public_enabled"])))
        if "is_hidden" in payload:
            assignments.append("is_hidden = ?")
            values.append(int(bool(payload["is_hidden"])))
        if "notes" in payload:
            assignments.append("notes = ?")
            values.append(None if payload["notes"] is None else str(payload["notes"]))

        if not assignments:
            return self.get_mailbox(mailbox_id)

        def operation(connection: sqlite3.Connection) -> None:
            existing = connection.execute(
                "SELECT id FROM mailboxes WHERE id = ?",
                (mailbox_id,),
            ).fetchone()
            if existing is None:
                raise LookupError("mailbox not found")
            connection.execute(
                f"UPDATE mailboxes SET {', '.join(assignments)} WHERE id = ?",
                (*values, mailbox_id),
            )

        await self._runtime.writer.execute(operation)
        return self.get_mailbox(mailbox_id)

    def _normalize_mailbox_row(self, row: sqlite3.Row) -> dict[str, Any]:
        payload = dict(row)
        for key in ("public_enabled", "is_hidden"):
            if key in payload:
                payload[key] = bool(payload[key])
        return payload


__all__ = ["MailboxService"]
