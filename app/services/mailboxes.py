from __future__ import annotations

import sqlite3
from typing import Any

from app.db.connection import connect_database
from app.ingest.storage import utc_now


class MailboxService:
    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    def list_mailboxes(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        query: str | None = None,
        domain_id: int | None = None,
        public_enabled: bool | None = None,
        is_hidden: bool | None = None,
    ) -> dict[str, Any]:
        where_sql, params = self._mailbox_filter_sql(
            query=query,
            domain_id=domain_id,
            public_enabled=public_enabled,
            is_hidden=is_hidden,
        )
        with connect_database(self._runtime.settings.database_path) as connection:
            rows = connection.execute(
                f"""
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
                {where_sql}
                ORDER BY m.latest_message_at DESC, m.id DESC
                LIMIT ? OFFSET ?
                """,
                (*params, limit, offset),
            ).fetchall()
        return {"items": [self._normalize_mailbox_row(row) for row in rows]}

    def count_mailboxes(
        self,
        *,
        query: str | None = None,
        domain_id: int | None = None,
        public_enabled: bool | None = None,
        is_hidden: bool | None = None,
    ) -> int:
        where_sql, params = self._mailbox_filter_sql(
            query=query,
            domain_id=domain_id,
            public_enabled=public_enabled,
            is_hidden=is_hidden,
        )
        with connect_database(self._runtime.settings.database_path) as connection:
            row = connection.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM mailboxes AS m
                JOIN domains AS d ON d.id = m.domain_id
                {where_sql}
                """,
                tuple(params),
            ).fetchone()
        return 0 if row is None else int(row["count"])

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

    def list_mailbox_deliveries(self, mailbox_id: int, *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        self.get_mailbox(mailbox_id)
        with connect_database(self._runtime.settings.database_path) as connection:
            rows = connection.execute(
                """
                SELECT
                    d.id AS delivery_id,
                    d.rcpt_to,
                    d.delivered_at,
                    d.status,
                    d.deleted_at,
                    m.id AS message_id,
                    m.subject,
                    m.from_addr,
                    m.parse_status,
                    m.has_attachments,
                    m.attachment_count
                FROM message_deliveries AS d
                JOIN messages AS m ON m.id = d.message_id
                WHERE d.mailbox_id = ?
                ORDER BY d.delivered_at DESC, d.id DESC
                LIMIT ? OFFSET ?
                """,
                (mailbox_id, limit, offset),
            ).fetchall()
            total = connection.execute(
                "SELECT COUNT(*) AS count FROM message_deliveries WHERE mailbox_id = ?",
                (mailbox_id,),
            ).fetchone()
        return {
            "items": [dict(row) for row in rows],
            "total_count": 0 if total is None else int(total["count"]),
        }

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

    async def soft_delete_mailbox_deliveries(
        self,
        mailbox_id: int,
        *,
        delivery_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        deleted_at = utc_now()

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            existing = connection.execute("SELECT id FROM mailboxes WHERE id = ?", (mailbox_id,)).fetchone()
            if existing is None:
                raise LookupError("mailbox not found")

            params: list[Any] = [deleted_at, mailbox_id]
            delivery_filter = ""
            if delivery_ids is not None:
                if not delivery_ids:
                    return {"deleted": 0, "delivery_ids": []}
                placeholders = ", ".join("?" for _ in delivery_ids)
                delivery_filter = f" AND id IN ({placeholders})"
                params.extend(delivery_ids)

            rows = connection.execute(
                f"""
                SELECT id
                FROM message_deliveries
                WHERE mailbox_id = ? AND status = 'active'{delivery_filter}
                """,
                tuple(params[1:]),
            ).fetchall()
            connection.execute(
                f"""
                UPDATE message_deliveries
                SET status = 'deleted',
                    deleted_at = COALESCE(deleted_at, ?)
                WHERE mailbox_id = ? AND status = 'active'{delivery_filter}
                """,
                tuple(params),
            )
            self._runtime._refresh_mailbox_summary_after_message_delete(connection, mailbox_id)
            return {
                "deleted": len(rows),
                "delivery_ids": [str(row["id"]) for row in rows],
            }

        return await self._runtime.writer.execute(operation)

    def _mailbox_filter_sql(
        self,
        *,
        query: str | None,
        domain_id: int | None,
        public_enabled: bool | None,
        is_hidden: bool | None,
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if query:
            clauses.append("(m.address_canonical LIKE ? OR m.address_display LIKE ? OR m.notes LIKE ?)")
            pattern = f"%{query.strip()}%"
            params.extend([pattern, pattern, pattern])
        if domain_id is not None:
            clauses.append("m.domain_id = ?")
            params.append(int(domain_id))
        if public_enabled is not None:
            clauses.append("m.public_enabled = ?")
            params.append(int(public_enabled))
        if is_hidden is not None:
            clauses.append("m.is_hidden = ?")
            params.append(int(is_hidden))
        if not clauses:
            return "", params
        return "WHERE " + " AND ".join(clauses), params

    def _normalize_mailbox_row(self, row: sqlite3.Row) -> dict[str, Any]:
        payload = dict(row)
        for key in ("public_enabled", "is_hidden"):
            if key in payload:
                payload[key] = bool(payload[key])
        return payload


__all__ = ["MailboxService"]
