from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from app.config import Settings
from app.db.connection import connect_database, initialize_database
from app.db.writer import DatabaseWriter
from app.ingest.parser import MessageParser, ParsedMessage
from app.ingest.recovery import RecoveryScanner
from app.ingest.queue import ParseQueue, ParseTask
from app.ingest.storage import FileStorage, utc_now
from app.services.domains import DomainService
from app.smtp.live_state import LiveState


class RapidInboxRuntime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.storage = FileStorage(settings)
        self.writer = DatabaseWriter(settings.database_path)
        self.domains = DomainService(settings.database_path, self.writer)
        self.parser = MessageParser(self.storage)
        self.parse_queue = ParseQueue(self._parse_message)
        self.live_state = LiveState()
        self.recovery = RecoveryScanner(self)

    async def start(self) -> None:
        self.settings.ensure_directories()
        initialize_database(self.settings.database_path)
        self.domains.reload()
        await self.parse_queue.start()
        await self.recovery.run()

    async def stop(self) -> None:
        await self.parse_queue.stop()

    async def create_domain(self, root_domain: str, **kwargs: Any) -> dict[str, Any]:
        return await self.domains.create_domain(root_domain, **kwargs)

    def list_domains(self) -> list[dict[str, Any]]:
        return self.domains.list_domains()

    async def ensure_smtp_session(self, session_id: str, session: Any, *, last_rcpt_to: str | None = None) -> None:
        now = utc_now()
        peer = getattr(session, "peer", None) or ("unknown", None)
        remote_ip = peer[0] or "unknown"
        remote_port = peer[1]
        helo_name = getattr(session, "host_name", None)
        tls_used = int(bool(getattr(session, "ssl", None)))

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO smtp_sessions (
                    id,
                    remote_ip,
                    remote_port,
                    helo_name,
                    status,
                    tls_used,
                    connect_at,
                    first_command_at,
                    last_command_at,
                    last_rcpt_to_sample
                ) VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    helo_name = excluded.helo_name,
                    tls_used = excluded.tls_used,
                    last_command_at = excluded.last_command_at,
                    last_rcpt_to_sample = COALESCE(excluded.last_rcpt_to_sample, smtp_sessions.last_rcpt_to_sample)
                """,
                (
                    session_id,
                    remote_ip,
                    remote_port,
                    helo_name,
                    tls_used,
                    now,
                    now,
                    now,
                    last_rcpt_to,
                ),
            )

        await self.writer.execute(operation)

    async def accept_message(
        self,
        *,
        rcpt_tos: list[str],
        envelope_from: str | None,
        content: bytes,
        smtp_session_id: str | None = None,
    ) -> str:
        received_at = utc_now()
        message_id = f"msg_{uuid.uuid4().hex}"

        matches = []
        for rcpt_to in rcpt_tos:
            match = self.domains.match_address(rcpt_to)
            if match is None:
                raise ValueError(f"recipient domain not allowed: {rcpt_to}")
            matches.append((rcpt_to, match))

        raw_path, raw_sha256, raw_size_bytes = self.storage.write_raw_message(message_id, received_at, content)
        manifest_payload = {
            "message_id": message_id,
            "smtp_session_id": smtp_session_id,
            "envelope_from": envelope_from,
            "rcpt_tos": list(rcpt_tos),
            "received_at": received_at,
            "raw_path": raw_path,
            "raw_sha256": raw_sha256,
            "raw_size_bytes": raw_size_bytes,
        }
        self.storage.write_manifest(message_id, received_at, manifest_payload)

        def operation(connection: sqlite3.Connection) -> list[str]:
            connection.execute(
                """
                INSERT INTO messages (
                    id,
                    smtp_session_id,
                    raw_path,
                    raw_sha256,
                    raw_size_bytes,
                    envelope_from,
                    from_addr,
                    received_at,
                    parse_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    message_id,
                    smtp_session_id,
                    raw_path,
                    raw_sha256,
                    raw_size_bytes,
                    envelope_from,
                    envelope_from,
                    received_at,
                ),
            )

            delivery_ids: list[str] = []
            for rcpt_to, match in matches:
                mailbox_id = self._upsert_mailbox(connection, match, received_at)
                delivery_id = f"dlv_{uuid.uuid4().hex}"
                connection.execute(
                    """
                    INSERT INTO message_deliveries (
                        id,
                        message_id,
                        mailbox_id,
                        rcpt_to,
                        delivered_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (delivery_id, message_id, mailbox_id, rcpt_to, received_at),
                )
                delivery_ids.append(delivery_id)

            return delivery_ids

        await self.writer.execute(operation)
        await self.parse_queue.enqueue(ParseTask(message_id=message_id))
        return f"250 queued as {message_id}"

    async def drain_parser_queue(self) -> None:
        await self.parse_queue.drain()

    async def recover_from_manifest(self, manifest: dict[str, Any]) -> None:
        await self.writer.execute(lambda connection: self._apply_recovery_manifest(connection, manifest))

    async def find_messages_for_reparse(self) -> list[str]:
        with connect_database(self.settings.database_path) as connection:
            rows = connection.execute(
                """
                SELECT id
                FROM messages
                WHERE parse_status IN ('pending', 'failed')
                ORDER BY received_at ASC, id ASC
                """
            ).fetchall()
        return [str(row["id"]) for row in rows]

    async def get_mailbox_view(self, mailbox_address: str, *, limit: int = 50) -> dict[str, Any]:
        match = self.domains.match_address(mailbox_address)
        if match is None:
            raise LookupError("mailbox domain not managed")

        await self._ensure_mailbox_exists(match)
        with connect_database(self.settings.database_path) as connection:
            mailbox = connection.execute(
                """
                SELECT id, address_canonical, latest_message_at, message_count
                FROM mailboxes
                WHERE address_canonical = ?
                """,
                (match.address_canonical,),
            ).fetchone()
            rows = connection.execute(
                """
                SELECT
                    d.id AS delivery_id,
                    d.delivered_at,
                    m.id AS message_id,
                    m.subject,
                    m.from_addr,
                    m.has_attachments,
                    m.parse_status
                FROM message_deliveries AS d
                JOIN messages AS m ON m.id = d.message_id
                WHERE d.mailbox_id = ? AND d.status = 'active'
                ORDER BY d.delivered_at DESC
                LIMIT ?
                """,
                (mailbox["id"], limit),
            ).fetchall()

        return {
            "mailbox": match.address_canonical,
            "items": [dict(row) for row in rows],
            "message_count": mailbox["message_count"],
        }

    async def get_delivery_detail(self, mailbox_address: str, delivery_id: str) -> dict[str, Any]:
        match = self.domains.match_address(mailbox_address)
        if match is None:
            raise LookupError("mailbox domain not managed")

        await self._ensure_mailbox_exists(match)
        with connect_database(self.settings.database_path) as connection:
            mailbox = connection.execute(
                "SELECT id, address_canonical FROM mailboxes WHERE address_canonical = ?",
                (match.address_canonical,),
            ).fetchone()
            row = connection.execute(
                """
                SELECT
                    d.id AS delivery_id,
                    d.delivered_at,
                    m.id AS message_id,
                    m.subject,
                    m.from_addr,
                    m.text_body_path,
                    m.html_body_path,
                    m.parse_status,
                    m.raw_path,
                    m.headers_json
                FROM message_deliveries AS d
                JOIN messages AS m ON m.id = d.message_id
                WHERE d.id = ? AND d.mailbox_id = ?
                """,
                (delivery_id, mailbox["id"]),
            ).fetchone()
            if row is None:
                raise LookupError("delivery not found")
            attachments = connection.execute(
                """
                SELECT
                    id,
                    filename,
                    safe_filename,
                    content_type,
                    storage_path,
                    size_bytes,
                    is_inline
                FROM attachments
                WHERE message_id = ?
                ORDER BY part_index ASC
                """,
                (row["message_id"],),
            ).fetchall()

        return {
            "delivery_id": row["delivery_id"],
            "message_id": row["message_id"],
            "mailbox": mailbox["address_canonical"],
            "received_at": row["delivered_at"],
            "subject": row["subject"],
            "from_addr": row["from_addr"],
            "parse_status": row["parse_status"],
            "text_body": self.storage.read_text(row["text_body_path"]) or "",
            "html_body": self.storage.read_text(row["html_body_path"]) or "",
            "raw_path": row["raw_path"],
            "headers": json.loads(row["headers_json"] or "[]"),
            "attachments": [dict(attachment) for attachment in attachments],
        }

    async def get_raw_message(self, delivery_id: str) -> bytes:
        with connect_database(self.settings.database_path) as connection:
            row = connection.execute(
                """
                SELECT m.raw_path
                FROM message_deliveries AS d
                JOIN messages AS m ON m.id = d.message_id
                WHERE d.id = ?
                """,
                (delivery_id,),
            ).fetchone()
        if row is None:
            raise LookupError("delivery not found")
        return self.storage.read_bytes(row["raw_path"])

    async def _parse_message(self, task: ParseTask) -> None:
        with connect_database(self.settings.database_path) as connection:
            message_row = connection.execute(
                "SELECT raw_path, received_at FROM messages WHERE id = ?",
                (task.message_id,),
            ).fetchone()
        if message_row is None:
            return

        try:
            parsed = self.parser.parse_message(
                task.message_id,
                self.storage.read_bytes(message_row["raw_path"]),
                message_row["received_at"],
            )
        except Exception as exc:
            await self.writer.execute(
                lambda connection: connection.execute(
                    """
                    UPDATE messages
                    SET parse_status = 'failed',
                        parse_error = ?,
                        indexed_at = ?
                    WHERE id = ?
                    """,
                    (str(exc), utc_now(), task.message_id),
                )
            )
            return

        await self.writer.execute(lambda connection: self._apply_parsed_message(connection, task.message_id, parsed))

    def _apply_parsed_message(self, connection: sqlite3.Connection, message_id: str, parsed: ParsedMessage) -> None:
        connection.execute(
            """
            UPDATE messages
            SET message_id_header = ?,
                subject = ?,
                from_name = ?,
                from_addr = ?,
                reply_to = ?,
                date_header = ?,
                indexed_at = ?,
                parse_status = 'parsed',
                parse_error = NULL,
                has_text = ?,
                has_html = ?,
                has_attachments = ?,
                attachment_count = ?,
                text_preview = ?,
                text_body_path = ?,
                html_body_path = ?,
                headers_json = ?
            WHERE id = ?
            """,
            (
                parsed.message_id_header,
                parsed.subject,
                parsed.from_name,
                parsed.from_addr,
                parsed.reply_to,
                parsed.date_header,
                utc_now(),
                int(parsed.has_text),
                int(parsed.has_html),
                int(parsed.has_attachments),
                parsed.attachment_count,
                parsed.text_preview,
                parsed.text_body_path,
                parsed.html_body_path,
                parsed.headers_json,
                message_id,
            ),
        )

        connection.execute("DELETE FROM attachments WHERE message_id = ?", (message_id,))
        for attachment in parsed.attachments:
            connection.execute(
                """
                INSERT INTO attachments (
                    id,
                    message_id,
                    part_index,
                    filename,
                    safe_filename,
                    content_type,
                    content_disposition,
                    content_id,
                    storage_path,
                    sha256,
                    size_bytes,
                    is_inline,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attachment.attachment_id,
                    message_id,
                    attachment.part_index,
                    attachment.filename,
                    attachment.safe_filename,
                    attachment.content_type,
                    attachment.content_disposition,
                    attachment.content_id,
                    attachment.storage_path,
                    attachment.sha256,
                    attachment.size_bytes,
                    int(attachment.is_inline),
                    utc_now(),
                ),
            )

    def _apply_recovery_manifest(self, connection: sqlite3.Connection, manifest: dict[str, Any]) -> None:
        message_id = str(manifest["message_id"])
        smtp_session_id = manifest.get("smtp_session_id")
        if smtp_session_id is not None:
            session_exists = connection.execute(
                "SELECT 1 FROM smtp_sessions WHERE id = ?",
                (smtp_session_id,),
            ).fetchone()
            if session_exists is None:
                smtp_session_id = None

        received_at = str(manifest["received_at"])
        raw_path = str(manifest["raw_path"])
        raw_sha256 = str(manifest["raw_sha256"])
        raw_size_bytes = int(manifest["raw_size_bytes"])
        envelope_from = manifest.get("envelope_from")
        rcpt_tos = [str(rcpt_to) for rcpt_to in manifest.get("rcpt_tos") or []]

        connection.execute(
            """
            INSERT OR IGNORE INTO messages (
                id,
                smtp_session_id,
                raw_path,
                raw_sha256,
                raw_size_bytes,
                envelope_from,
                from_addr,
                received_at,
                parse_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                message_id,
                smtp_session_id,
                raw_path,
                raw_sha256,
                raw_size_bytes,
                envelope_from,
                envelope_from,
                received_at,
            ),
        )

        mailbox_ids: set[int] = set()
        for rcpt_to in rcpt_tos:
            match = self.domains.match_address(rcpt_to)
            if match is None:
                continue
            mailbox_id = self._ensure_mailbox_record(connection, match, received_at)
            mailbox_ids.add(mailbox_id)
            connection.execute(
                """
                INSERT OR IGNORE INTO message_deliveries (
                    id,
                    message_id,
                    mailbox_id,
                    rcpt_to,
                    delivered_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (f"dlv_{uuid.uuid4().hex}", message_id, mailbox_id, rcpt_to, received_at),
            )

        for mailbox_id in mailbox_ids:
            self._refresh_mailbox_summary(connection, mailbox_id)

    async def _ensure_mailbox_exists(self, match) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            existing = connection.execute(
                "SELECT id FROM mailboxes WHERE address_canonical = ?",
                (match.address_canonical,),
            ).fetchone()
            if existing is not None:
                return
            self._insert_mailbox(connection, match, utc_now(), message_count=0, latest_message_at=None)

        await self.writer.execute(operation)

    def _upsert_mailbox(self, connection: sqlite3.Connection, match, received_at: str) -> int:
        existing = connection.execute(
            "SELECT id FROM mailboxes WHERE address_canonical = ?",
            (match.address_canonical,),
        ).fetchone()
        if existing is not None:
            connection.execute(
                """
                UPDATE mailboxes
                SET last_seen_at = ?,
                    latest_message_at = ?,
                    message_count = message_count + 1
                WHERE id = ?
                """,
                (received_at, received_at, existing["id"]),
            )
            return int(existing["id"])

        return self._insert_mailbox(connection, match, received_at, message_count=1, latest_message_at=received_at)

    def _ensure_mailbox_record(self, connection: sqlite3.Connection, match, received_at: str) -> int:
        existing = connection.execute(
            "SELECT id FROM mailboxes WHERE address_canonical = ?",
            (match.address_canonical,),
        ).fetchone()
        if existing is not None:
            return int(existing["id"])

        return self._insert_mailbox(connection, match, received_at, message_count=0, latest_message_at=None)

    def _refresh_mailbox_summary(self, connection: sqlite3.Connection, mailbox_id: int) -> None:
        summary = connection.execute(
            """
            SELECT
                COUNT(*) AS message_count,
                MIN(delivered_at) AS first_seen_at,
                MAX(delivered_at) AS latest_message_at
            FROM message_deliveries
            WHERE mailbox_id = ? AND status = 'active'
            """,
            (mailbox_id,),
        ).fetchone()
        connection.execute(
            """
            UPDATE mailboxes
            SET first_seen_at = ?,
                last_seen_at = ?,
                latest_message_at = ?,
                message_count = ?
            WHERE id = ?
            """,
            (
                summary["first_seen_at"],
                summary["latest_message_at"],
                summary["latest_message_at"],
                int(summary["message_count"]),
                mailbox_id,
            ),
        )

    def _insert_mailbox(
        self,
        connection: sqlite3.Connection,
        match,
        received_at: str,
        *,
        message_count: int,
        latest_message_at: str | None,
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO mailboxes (
                domain_id,
                local_part_canonical,
                rcpt_domain_ascii,
                address_canonical,
                address_display,
                first_seen_at,
                last_seen_at,
                latest_message_at,
                message_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                match.domain_id,
                match.local_part_canonical,
                match.domain_ascii,
                match.address_canonical,
                match.address_canonical,
                received_at,
                received_at,
                latest_message_at,
                message_count,
            ),
        )
        return int(cursor.lastrowid)
