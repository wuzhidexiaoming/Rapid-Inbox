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
from app.smtp.matcher import DomainMatcher, DomainRule


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
        await self.parse_queue.start()
        await self.recovery.run()
        self.domains.reload()

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
        domain_policies = self._load_recovery_domain_policies(matches)
        recipient_recovery_payloads = [
            self._recovery_recipient_payload(rcpt_to, match, domain_policies[match.domain_id])
            for rcpt_to, match in matches
        ]
        manifest_payload = {
            "message_id": message_id,
            "smtp_session_id": smtp_session_id,
            "envelope_from": envelope_from,
            "rcpt_tos": list(rcpt_tos),
            "recipients": recipient_recovery_payloads,
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

    def validate_recovery_manifest(self, manifest: Any) -> None:
        if not isinstance(manifest, dict):
            raise ValueError("invalid recovery manifest")

        for key in ("message_id", "received_at", "raw_path", "raw_sha256", "raw_size_bytes"):
            if key not in manifest:
                raise ValueError("invalid recovery manifest")

        if not all(isinstance(manifest[key], str) for key in ("message_id", "received_at", "raw_path", "raw_sha256")):
            raise ValueError("invalid recovery manifest")
        raw_size_bytes = manifest["raw_size_bytes"]
        if not isinstance(raw_size_bytes, int) or isinstance(raw_size_bytes, bool):
            raise ValueError("invalid recovery manifest")

        recipients = manifest.get("recipients")
        if recipients is not None:
            if not isinstance(recipients, list) or not recipients:
                raise ValueError("invalid recovery manifest")
            for recipient in recipients:
                if not isinstance(recipient, dict):
                    raise ValueError("invalid recovery manifest")
                if not isinstance(recipient.get("rcpt_to"), str):
                    raise ValueError("invalid recovery manifest")
                if not isinstance(recipient.get("domain_id"), int) or isinstance(recipient.get("domain_id"), bool):
                    raise ValueError("invalid recovery manifest")
                for key in ("domain_ascii", "root_domain_ascii", "local_part_canonical", "address_canonical"):
                    if not isinstance(recipient.get(key), str):
                        raise ValueError("invalid recovery manifest")
                domain_policy = recipient.get("domain_policy")
                if domain_policy is not None:
                    self._validate_recovery_domain_policy(domain_policy)
        else:
            rcpt_tos = manifest.get("rcpt_tos")
            if not isinstance(rcpt_tos, list) or not rcpt_tos:
                raise ValueError("invalid recovery manifest")
            for rcpt_to in rcpt_tos:
                if not isinstance(rcpt_to, str):
                    raise ValueError("invalid recovery manifest")

    def _validate_recovery_domain_policy(self, domain_policy: Any) -> None:
        if not isinstance(domain_policy, dict):
            raise ValueError("invalid recovery manifest")

        for key in (
            "root_domain_unicode",
            "accept_exact",
            "accept_subdomains",
            "public_web_enabled",
            "public_api_enabled",
            "is_active",
            "is_hidden",
            "plus_addressing_mode",
            "local_part_case_sensitive",
            "max_message_size_bytes",
            "retention_days",
            "dns_status",
        ):
            if key not in domain_policy:
                raise ValueError("invalid recovery manifest")

        if not isinstance(domain_policy["root_domain_unicode"], str):
            raise ValueError("invalid recovery manifest")
        for key in (
            "accept_exact",
            "accept_subdomains",
            "public_web_enabled",
            "public_api_enabled",
            "is_active",
            "is_hidden",
            "local_part_case_sensitive",
        ):
            if not isinstance(domain_policy[key], bool):
                raise ValueError("invalid recovery manifest")
        if not isinstance(domain_policy["plus_addressing_mode"], str):
            raise ValueError("invalid recovery manifest")
        if domain_policy["plus_addressing_mode"] not in {"keep", "strip"}:
            raise ValueError("invalid recovery manifest")
        if not isinstance(domain_policy["max_message_size_bytes"], int) or isinstance(domain_policy["max_message_size_bytes"], bool):
            raise ValueError("invalid recovery manifest")
        retention_days = domain_policy["retention_days"]
        if retention_days is not None and (
            not isinstance(retention_days, int) or isinstance(retention_days, bool)
        ):
            raise ValueError("invalid recovery manifest")
        if not isinstance(domain_policy["dns_status"], str):
            raise ValueError("invalid recovery manifest")
        if domain_policy["dns_status"] not in {"unknown", "ok", "warning", "error"}:
            raise ValueError("invalid recovery manifest")

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
        recipients = self._recovery_recipients_from_manifest(connection, manifest)

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
        for recipient in recipients:
            mailbox_id = self._ensure_recovery_mailbox_record(connection, recipient, received_at)
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
                (
                    f"dlv_{uuid.uuid4().hex}",
                    message_id,
                    mailbox_id,
                    recipient["rcpt_to"],
                    received_at,
                ),
            )

        for mailbox_id in mailbox_ids:
            self._refresh_mailbox_summary(connection, mailbox_id)

    def _recovery_recipient_payload(self, rcpt_to: str, match, domain_policy: dict[str, Any]) -> dict[str, Any]:
        return {
            "rcpt_to": rcpt_to,
            "domain_id": match.domain_id,
            "domain_ascii": match.domain_ascii,
            "root_domain_ascii": match.root_domain_ascii,
            "local_part_canonical": match.local_part_canonical,
            "address_canonical": match.address_canonical,
            "domain_policy": domain_policy,
        }

    def _load_recovery_domain_policies(self, matches: list[tuple[str, Any]]) -> dict[int, dict[str, Any]]:
        domain_policies: dict[int, dict[str, Any]] = {}
        with connect_database(self.settings.database_path) as connection:
            for _, match in matches:
                if match.domain_id not in domain_policies:
                    domain_policies[match.domain_id] = self._load_recovery_domain_policy(connection, match.domain_id)
        return domain_policies

    def _load_recovery_domain_policy(self, connection: sqlite3.Connection, domain_id: int) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT
                root_domain_ascii,
                root_domain_unicode,
                accept_exact,
                accept_subdomains,
                public_web_enabled,
                public_api_enabled,
                is_active,
                is_hidden,
                plus_addressing_mode,
                local_part_case_sensitive,
                max_message_size_bytes,
                retention_days,
                dns_status
            FROM domains
            WHERE id = ?
            """,
            (domain_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"domain not found: {domain_id}")
        return {
            "root_domain_unicode": row["root_domain_unicode"] or row["root_domain_ascii"],
            "accept_exact": bool(row["accept_exact"]),
            "accept_subdomains": bool(row["accept_subdomains"]),
            "public_web_enabled": bool(row["public_web_enabled"]),
            "public_api_enabled": bool(row["public_api_enabled"]),
            "is_active": bool(row["is_active"]),
            "is_hidden": bool(row["is_hidden"]),
            "plus_addressing_mode": row["plus_addressing_mode"],
            "local_part_case_sensitive": bool(row["local_part_case_sensitive"]),
            "max_message_size_bytes": int(row["max_message_size_bytes"]),
            "retention_days": row["retention_days"],
            "dns_status": row["dns_status"],
        }

    def _recovery_recipients_from_manifest(
        self,
        connection: sqlite3.Connection,
        manifest: dict[str, Any],
    ) -> list[dict[str, Any]]:
        recipients = manifest.get("recipients")
        if recipients is not None:
            if not isinstance(recipients, list) or not recipients:
                raise ValueError("invalid recovery manifest recipients")
            return [self._coerce_recovery_recipient(recipient) for recipient in recipients]

        rcpt_tos = manifest.get("rcpt_tos")
        if not isinstance(rcpt_tos, list) or not rcpt_tos:
            raise ValueError("invalid recovery manifest rcpt_tos")
        return [self._resolve_legacy_recovery_recipient(connection, str(rcpt_to)) for rcpt_to in rcpt_tos]

    def _coerce_recovery_recipient(self, recipient: Any) -> dict[str, Any]:
        if not isinstance(recipient, dict):
            raise ValueError("invalid recovery manifest recipient")
        try:
            domain_policy = recipient.get("domain_policy")
            return {
                "rcpt_to": str(recipient["rcpt_to"]),
                "domain_id": int(recipient["domain_id"]),
                "domain_ascii": str(recipient["domain_ascii"]),
                "root_domain_ascii": str(recipient["root_domain_ascii"]),
                "local_part_canonical": str(recipient["local_part_canonical"]),
                "address_canonical": str(recipient["address_canonical"]),
                "domain_policy": self._coerce_recovery_domain_policy(domain_policy) if domain_policy is not None else None,
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid recovery manifest recipient") from exc

    def _coerce_recovery_domain_policy(self, domain_policy: Any) -> dict[str, Any]:
        self._validate_recovery_domain_policy(domain_policy)
        return {
            "root_domain_unicode": str(domain_policy["root_domain_unicode"]),
            "accept_exact": bool(domain_policy["accept_exact"]),
            "accept_subdomains": bool(domain_policy["accept_subdomains"]),
            "public_web_enabled": bool(domain_policy["public_web_enabled"]),
            "public_api_enabled": bool(domain_policy["public_api_enabled"]),
            "is_active": bool(domain_policy["is_active"]),
            "is_hidden": bool(domain_policy["is_hidden"]),
            "plus_addressing_mode": str(domain_policy["plus_addressing_mode"]),
            "local_part_case_sensitive": bool(domain_policy["local_part_case_sensitive"]),
            "max_message_size_bytes": int(domain_policy["max_message_size_bytes"]),
            "retention_days": domain_policy["retention_days"],
            "dns_status": str(domain_policy["dns_status"]),
        }

    def _resolve_legacy_recovery_recipient(
        self,
        connection: sqlite3.Connection,
        rcpt_to: str,
    ) -> dict[str, Any]:
        rows = connection.execute(
            """
            SELECT
                id,
                root_domain_ascii,
                accept_exact,
                accept_subdomains,
                plus_addressing_mode,
                local_part_case_sensitive
            FROM domains
            ORDER BY LENGTH(root_domain_ascii) DESC, id ASC
            """
        ).fetchall()
        match = DomainMatcher(
            [
                DomainRule(
                    domain_id=row["id"],
                    root_domain_ascii=row["root_domain_ascii"],
                    accept_exact=bool(row["accept_exact"]),
                    accept_subdomains=bool(row["accept_subdomains"]),
                    plus_addressing_mode=row["plus_addressing_mode"],
                    local_part_case_sensitive=bool(row["local_part_case_sensitive"]),
                )
                for row in rows
            ]
        ).match_address(rcpt_to)
        if match is None:
            raise ValueError(f"unable to recover recipient: {rcpt_to}")
        try:
            domain_policy = self._load_recovery_domain_policy(connection, match.domain_id)
        except LookupError as exc:
            raise ValueError(f"unable to recover recipient: {rcpt_to}") from exc
        return self._recovery_recipient_payload(rcpt_to, match, domain_policy)

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
        cursor = self._insert_mailbox_from_values(
            connection,
            domain_id=match.domain_id,
            local_part_canonical=match.local_part_canonical,
            rcpt_domain_ascii=match.domain_ascii,
            address_canonical=match.address_canonical,
            address_display=match.address_canonical,
            received_at=received_at,
            message_count=message_count,
            latest_message_at=latest_message_at,
        )
        return int(cursor.lastrowid)

    def _ensure_recovery_mailbox_record(
        self,
        connection: sqlite3.Connection,
        recipient: dict[str, Any],
        received_at: str,
    ) -> int:
        domain_id = self._ensure_recovery_domain_record(connection, recipient, received_at)
        existing = connection.execute(
            "SELECT id FROM mailboxes WHERE address_canonical = ?",
            (recipient["address_canonical"],),
        ).fetchone()
        if existing is not None:
            return int(existing["id"])

        cursor = self._insert_mailbox_from_values(
            connection,
            domain_id=domain_id,
            local_part_canonical=str(recipient["local_part_canonical"]),
            rcpt_domain_ascii=str(recipient["domain_ascii"]),
            address_canonical=str(recipient["address_canonical"]),
            address_display=str(recipient["address_canonical"]),
            received_at=received_at,
            message_count=0,
            latest_message_at=None,
        )
        return int(cursor.lastrowid)

    def _ensure_recovery_domain_record(
        self,
        connection: sqlite3.Connection,
        recipient: dict[str, Any],
        received_at: str,
    ) -> int:
        root_domain_ascii = str(recipient["root_domain_ascii"])
        existing = connection.execute(
            "SELECT id FROM domains WHERE root_domain_ascii = ?",
            (root_domain_ascii,),
        ).fetchone()
        if existing is not None:
            return int(existing["id"])

        domain_id = int(recipient["domain_id"])
        domain_policy = recipient.get("domain_policy")
        if domain_policy is None:
            domain_policy = self._default_recovery_domain_policy(recipient)
        existing = connection.execute(
            "SELECT id FROM domains WHERE id = ?",
            (domain_id,),
        ).fetchone()
        if existing is not None:
            return int(existing["id"])

        connection.execute(
            """
            INSERT INTO domains (
                id,
                root_domain_ascii,
                root_domain_unicode,
                accept_exact,
                accept_subdomains,
                public_web_enabled,
                public_api_enabled,
                is_active,
                is_hidden,
                local_part_case_sensitive,
                plus_addressing_mode,
                max_message_size_bytes,
                retention_days,
                dns_status,
                dns_last_checked_at,
                dns_details_json,
                notes,
                created_by_admin_id,
                updated_by_admin_id,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                domain_id,
                root_domain_ascii,
                domain_policy["root_domain_unicode"] or root_domain_ascii,
                int(domain_policy["accept_exact"]),
                int(domain_policy["accept_subdomains"]),
                int(domain_policy["public_web_enabled"]),
                int(domain_policy["public_api_enabled"]),
                int(domain_policy["is_active"]),
                int(domain_policy["is_hidden"]),
                int(domain_policy["local_part_case_sensitive"]),
                domain_policy["plus_addressing_mode"],
                int(domain_policy["max_message_size_bytes"]),
                domain_policy["retention_days"],
                domain_policy["dns_status"],
                None,
                None,
                None,
                None,
                None,
                received_at,
                received_at,
            ),
        )
        return domain_id

    def _default_recovery_domain_policy(self, recipient: dict[str, Any]) -> dict[str, Any]:
        return {
            "root_domain_unicode": str(recipient["root_domain_ascii"]),
            "accept_exact": True,
            "accept_subdomains": True,
            "public_web_enabled": True,
            "public_api_enabled": True,
            "is_active": True,
            "is_hidden": False,
            "plus_addressing_mode": "keep",
            "local_part_case_sensitive": False,
            "max_message_size_bytes": 52_428_800,
            "retention_days": None,
            "dns_status": "unknown",
        }

    def _insert_mailbox_from_values(
        self,
        connection: sqlite3.Connection,
        *,
        domain_id: int,
        local_part_canonical: str,
        rcpt_domain_ascii: str,
        address_canonical: str,
        address_display: str,
        received_at: str,
        message_count: int,
        latest_message_at: str | None,
    ) -> sqlite3.Cursor:
        return connection.execute(
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
                domain_id,
                local_part_canonical,
                rcpt_domain_ascii,
                address_canonical,
                address_display,
                received_at,
                received_at,
                latest_message_at,
                message_count,
            ),
        )
