from __future__ import annotations

import hashlib
import asyncio
import json
import sqlite3
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any
from time import monotonic, time_ns
from pathlib import Path

from app.auth import AuthService
from app.auth.api_keys import ApiKeyService, get_active_permission_context
from app.auth.permissions import ensure_mailbox_access
from app.config import Settings
from app.db.connection import connect_database, initialize_database
from app.db.writer import DatabaseWriter
from app.ingest.parser import MessageParser, ParsedMessage
from app.ingest.recovery import RecoveryScanner
from app.ingest.queue import ParseQueue, ParseTask
from app.ingest.storage import FileStorage, utc_now
from app.services.audit import AuditService
from app.services.domains import DomainService
from app.services.mailboxes import MailboxService
from app.services.messages import MessageService
from app.services.settings import SettingsService
from app.smtp.live_state import LiveState
from app.smtp.matcher import DomainMatcher, DomainRule


MESSAGE_RETENTION_SECONDS = 20 * 60
MESSAGE_RETENTION_CLEANUP_INTERVAL_SECONDS = 30


class RapidInboxRuntime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._legacy_public_api_key = settings.public_api_key
        self.storage = FileStorage(settings)
        self.writer = DatabaseWriter(settings.database_path)
        self.auth = AuthService(settings, self.writer)
        self.api_keys = ApiKeyService(settings.database_path, self.writer)
        self.domains = DomainService(settings.database_path, self.writer)
        self.mailboxes = MailboxService(self)
        self.messages = MessageService(self)
        self.audit = AuditService(self)
        self.system_settings = SettingsService(self)
        self.parser = MessageParser(self.storage)
        self.parse_queue = ParseQueue(self._parse_message)
        self._mail_store_lock = asyncio.Lock()
        self._smtp_connection_lock = asyncio.Lock()
        self._active_smtp_connections: dict[str, str] = {}
        self._smtp_ip_windows: dict[str, deque[float]] = {}
        self._retention_cleanup_task: asyncio.Task[None] | None = None
        self.live_state = LiveState()
        self.recovery = RecoveryScanner(self)

    async def start(self) -> None:
        self.settings.ensure_directories()
        self.storage.cleanup_abandoned_clear_trash()
        initialize_database(self.settings.database_path)
        await self.auth.ensure_bootstrap_admin()
        await self.system_settings.load_persisted_settings()
        # Swap the plain config token for a string-like proxy that can validate DB-backed keys too.
        self.settings.public_api_key = self.api_keys.configure_legacy_public_api_key(self._legacy_public_api_key)
        await self.parse_queue.start()
        await self.recovery.run()
        self.domains.reload()
        self._retention_cleanup_task = asyncio.create_task(self._message_retention_loop())

    async def stop(self) -> None:
        await self._stop_message_retention_loop()
        await self.parse_queue.stop()
        async with self._smtp_connection_lock:
            self._active_smtp_connections.clear()

    async def create_domain(self, root_domain: str, **kwargs: Any) -> dict[str, Any]:
        return await self.domains.create_domain(root_domain, **kwargs)

    def list_domains(self) -> list[dict[str, Any]]:
        return self.domains.list_domains()

    async def reparse_message(self, message_id: str) -> None:
        await self.messages.reparse_message(message_id)

    def get_settings(self) -> dict[str, Any]:
        return self.system_settings.get_settings()

    async def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.system_settings.update_settings(payload)

    def apply_live_settings(self, updates: dict[str, Any]) -> None:
        for key, value in updates.items():
            if hasattr(self.settings, key):
                setattr(self.settings, key, value)

    async def clear_all_mail(self) -> dict[str, Any]:
        async with self._mail_store_lock:
            dropped_parse_tasks = self.parse_queue.clear_pending()
            await self.parse_queue.stop()
            try:
                result = await self.writer.execute(self._clear_mail_tables)
                result["moved_storage_directories"] = self.storage.clear_mail_data()
                self.live_state.clear()
                try:
                    result.update(await self.writer.execute_maintenance(self._compact_mail_database))
                except sqlite3.Error as exc:
                    result["database_compaction_failed"] = 1
                    result["database_compaction_error"] = str(exc)
                result["dropped_parse_tasks"] = dropped_parse_tasks
                return result
            finally:
                await self.parse_queue.start()

    async def cleanup_expired_messages(self) -> dict[str, int]:
        cutoff = self._message_retention_cutoff()
        expired_message_ids = self._expired_message_ids(cutoff)
        if not expired_message_ids:
            return self._empty_retention_result()

        async with self._mail_store_lock:
            expired_message_ids = self._expired_message_ids(cutoff)
            if not expired_message_ids:
                return self._empty_retention_result()

            expired_message_id_set = set(expired_message_ids)
            dropped_parse_tasks = self.parse_queue.remove_pending(
                lambda task: task.message_id in expired_message_id_set
            )
            queue_was_running = self.parse_queue.is_running
            if queue_was_running:
                await self.parse_queue.stop()

            try:
                result = await self.writer.execute(
                    lambda connection: self._delete_messages_received_at_or_before(connection, cutoff)
                )
                storage_paths = result.pop("storage_paths")
                deleted_files = self._delete_storage_files(storage_paths)
                return {
                    "messages": int(result["messages"]),
                    "deliveries": int(result["deliveries"]),
                    "mailboxes": int(result["mailboxes"]),
                    "attachments": int(result["attachments"]),
                    "raw_size_bytes": int(result["raw_size_bytes"]),
                    "files": deleted_files,
                    "dropped_parse_tasks": dropped_parse_tasks,
                }
            finally:
                if queue_was_running:
                    await self.parse_queue.start()

    async def _message_retention_loop(self) -> None:
        while True:
            await asyncio.sleep(MESSAGE_RETENTION_CLEANUP_INTERVAL_SECONDS)
            try:
                await self.cleanup_expired_messages()
            except asyncio.CancelledError:
                raise
            except Exception:
                continue

    async def _stop_message_retention_loop(self) -> None:
        task = self._retention_cleanup_task
        if task is None:
            return
        self._retention_cleanup_task = None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def _message_retention_cutoff(self) -> str:
        now = datetime.fromisoformat(utc_now().replace("Z", "+00:00"))
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        cutoff = now.astimezone(timezone.utc) - timedelta(seconds=MESSAGE_RETENTION_SECONDS)
        return cutoff.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def _expired_message_ids(self, cutoff: str) -> list[str]:
        with connect_database(self.settings.database_path) as connection:
            rows = connection.execute(
                """
                SELECT id
                FROM messages
                WHERE received_at <= ?
                ORDER BY received_at ASC, id ASC
                """,
                (cutoff,),
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def _empty_retention_result(self) -> dict[str, int]:
        return {
            "messages": 0,
            "deliveries": 0,
            "mailboxes": 0,
            "attachments": 0,
            "raw_size_bytes": 0,
            "files": 0,
            "dropped_parse_tasks": 0,
        }

    def _delete_messages_received_at_or_before(
        self,
        connection: sqlite3.Connection,
        cutoff: str,
    ) -> dict[str, Any]:
        message_rows = connection.execute(
            """
            SELECT
                id,
                raw_path,
                raw_size_bytes,
                received_at,
                text_body_path,
                html_body_path
            FROM messages
            WHERE received_at <= ?
            ORDER BY received_at ASC, id ASC
            """,
            (cutoff,),
        ).fetchall()
        if not message_rows:
            return {**self._empty_retention_result(), "storage_paths": []}

        delivery_count = int(
            connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM message_deliveries AS d
                JOIN messages AS m ON m.id = d.message_id
                WHERE m.received_at <= ?
                """,
                (cutoff,),
            ).fetchone()["count"]
        )
        mailbox_ids = [
            int(row["mailbox_id"])
            for row in connection.execute(
                """
                SELECT DISTINCT d.mailbox_id
                FROM message_deliveries AS d
                JOIN messages AS m ON m.id = d.message_id
                WHERE m.received_at <= ?
                ORDER BY d.mailbox_id ASC
                """,
                (cutoff,),
            ).fetchall()
        ]
        attachment_rows = connection.execute(
            """
            SELECT a.storage_path
            FROM attachments AS a
            JOIN messages AS m ON m.id = a.message_id
            WHERE m.received_at <= ?
            ORDER BY a.message_id ASC, a.part_index ASC
            """,
            (cutoff,),
        ).fetchall()

        storage_paths: list[str] = []
        total_bytes = 0
        for row in message_rows:
            message_id = str(row["id"])
            received_at = str(row["received_at"])
            total_bytes += int(row["raw_size_bytes"])
            for path_value in (
                row["raw_path"],
                row["text_body_path"],
                row["html_body_path"],
                self.storage.manifest_path(message_id, received_at),
            ):
                if path_value:
                    storage_paths.append(str(path_value))
        storage_paths.extend(str(row["storage_path"]) for row in attachment_rows)

        connection.execute("DELETE FROM messages WHERE received_at <= ?", (cutoff,))
        for mailbox_id in mailbox_ids:
            self._refresh_mailbox_summary_after_message_delete(connection, mailbox_id)

        return {
            "messages": len(message_rows),
            "deliveries": delivery_count,
            "mailboxes": len(mailbox_ids),
            "attachments": len(attachment_rows),
            "raw_size_bytes": total_bytes,
            "storage_paths": storage_paths,
        }

    def _refresh_mailbox_summary_after_message_delete(self, connection: sqlite3.Connection, mailbox_id: int) -> None:
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
        message_count = int(summary["message_count"])
        if message_count == 0:
            connection.execute(
                """
                UPDATE mailboxes
                SET latest_message_at = NULL,
                    message_count = 0
                WHERE id = ?
                """,
                (mailbox_id,),
            )
            return

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
                message_count,
                mailbox_id,
            ),
        )

    def _delete_storage_files(self, storage_paths: list[str]) -> int:
        deleted = 0
        seen: set[str] = set()
        for storage_path in storage_paths:
            if storage_path in seen:
                continue
            seen.add(storage_path)
            try:
                path = self.storage.resolve(storage_path)
                if path.is_file():
                    path.unlink()
                    deleted += 1
            except Exception:
                continue
        return deleted

    def list_audit_logs(self, *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        return self.audit.list_logs(limit=limit, offset=offset)

    async def register_smtp_connection(self, session_id: str, remote_ip: str) -> tuple[bool, str | None]:
        async with self._smtp_connection_lock:
            if session_id in self._active_smtp_connections:
                return True, None

            active_limit = int(self.settings.smtp_max_concurrent_connections)
            if active_limit > 0 and len(self._active_smtp_connections) >= active_limit:
                return False, "concurrent connection limit exceeded"

            rate_limit = int(self.settings.smtp_connection_rate_limit_count)
            window_seconds = int(self.settings.smtp_connection_rate_limit_window_seconds)
            if rate_limit > 0 and window_seconds > 0:
                now = monotonic()
                cutoff = now - window_seconds
                window = self._smtp_ip_windows.setdefault(remote_ip, deque())
                while window and window[0] <= cutoff:
                    window.popleft()
                if len(window) >= rate_limit:
                    return False, "per-ip connection rate limit exceeded"
                window.append(now)

            self._active_smtp_connections[session_id] = remote_ip
            return True, None

    async def release_smtp_connection(self, session_id: str) -> None:
        async with self._smtp_connection_lock:
            self._active_smtp_connections.pop(session_id, None)

    def active_smtp_connection_count(self) -> int:
        return len(self._active_smtp_connections)

    def _clear_mail_tables(self, connection: sqlite3.Connection) -> dict[str, int]:
        connection.execute("PRAGMA foreign_keys = OFF")
        messages_count = int(connection.execute("SELECT COUNT(*) AS count FROM messages").fetchone()["count"])
        deliveries_count = int(connection.execute("SELECT COUNT(*) AS count FROM message_deliveries").fetchone()["count"])
        mailboxes_count = int(connection.execute("SELECT COUNT(*) AS count FROM mailboxes").fetchone()["count"])
        attachments_count = int(connection.execute("SELECT COUNT(*) AS count FROM attachments").fetchone()["count"])
        smtp_sessions_count = int(connection.execute("SELECT COUNT(*) AS count FROM smtp_sessions").fetchone()["count"])
        smtp_events_count = int(connection.execute("SELECT COUNT(*) AS count FROM smtp_events").fetchone()["count"])
        total_bytes = int(
            connection.execute("SELECT COALESCE(SUM(raw_size_bytes), 0) AS total FROM messages").fetchone()["total"]
        )

        for table_name in (
            "attachments",
            "message_deliveries",
            "messages",
            "mailboxes",
            "smtp_events",
            "smtp_sessions",
        ):
            connection.execute(f"DELETE FROM {table_name}")
        connection.execute("DELETE FROM sqlite_sequence WHERE name IN ('mailboxes', 'smtp_events')")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("mail store foreign key check failed after clear")
        return {
            "messages": messages_count,
            "deliveries": deliveries_count,
            "mailboxes": mailboxes_count,
            "attachments": attachments_count,
            "smtp_sessions": smtp_sessions_count,
            "smtp_events": smtp_events_count,
            "raw_size_bytes": total_bytes,
        }

    def _compact_mail_database(self, connection: sqlite3.Connection) -> dict[str, int]:
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        freelist_before = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
        size_before = self._database_file_size_bytes()

        checkpoint_before = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        vacuumed = 0
        if freelist_before > 0:
            connection.execute("VACUUM")
            vacuumed = 1
        connection.execute("PRAGMA optimize")
        checkpoint_after = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()

        freelist_after = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
        size_after = self._database_file_size_bytes()
        return {
            "database_size_before_bytes": size_before,
            "database_size_after_bytes": size_after,
            "database_free_bytes_before": freelist_before * page_size,
            "database_free_bytes_after": freelist_after * page_size,
            "database_vacuumed": vacuumed,
            "database_checkpoint_busy_before": int(checkpoint_before[0]) if checkpoint_before is not None else 0,
            "database_checkpoint_busy_after": int(checkpoint_after[0]) if checkpoint_after is not None else 0,
        }

    def _database_file_size_bytes(self) -> int:
        database_path = self.settings.database_path
        return sum(
            path.stat().st_size
            for path in (
                database_path,
                Path(f"{database_path}-wal"),
                Path(f"{database_path}-shm"),
            )
            if path.exists()
        )

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

    async def record_smtp_rcpt(self, session_id: str, *, rcpt_to: str, accepted: bool) -> None:
        now = utc_now()

        def operation(connection: sqlite3.Connection) -> None:
            self._update_smtp_session_summary(
                connection,
                session_id,
                now,
                accepted_delta=1 if accepted else 0,
                rejected_delta=0 if accepted else 1,
                last_rcpt_to_sample=rcpt_to,
            )

        await self.writer.execute(operation)

    async def record_smtp_event(self, session_id: str, event_type: str, payload: dict[str, Any]) -> None:
        now = str(payload.get("ts") or utc_now())
        payload_json = json.dumps(payload, ensure_ascii=False)

        def operation(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                """
                SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq
                FROM smtp_events
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            seq = int(row["next_seq"]) if row is not None else 1
            connection.execute(
                """
                INSERT OR IGNORE INTO smtp_events (session_id, seq, event_type, ts, payload_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, seq, event_type, now, payload_json),
            )

        await self.writer.execute(operation)

    async def close_smtp_session(
        self,
        session_id: str,
        *,
        status: str,
        close_reason: str | None = None,
        result_code: int | None = None,
        result_message: str | None = None,
    ) -> None:
        now = utc_now()

        def operation(connection: sqlite3.Connection) -> None:
            self._update_smtp_session_summary(
                connection,
                session_id,
                now,
                status=status,
                disconnect_at=now,
                close_reason=close_reason,
                result_code=result_code,
                result_message=result_message,
            )

        await self.writer.execute(operation)
        await self.release_smtp_connection(session_id)

    async def accept_message(
        self,
        *,
        rcpt_tos: list[str],
        envelope_from: str | None,
        content: bytes,
        smtp_session_id: str | None = None,
    ) -> str:
        async with self._mail_store_lock:
            return await self._accept_message_without_mail_store_lock(
                rcpt_tos=rcpt_tos,
                envelope_from=envelope_from,
                content=content,
                smtp_session_id=smtp_session_id,
            )

    async def _accept_message_without_mail_store_lock(
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

        raw_path = self.storage.raw_message_path(message_id, received_at)
        raw_sha256 = hashlib.sha256(content).hexdigest()
        raw_size_bytes = len(content)
        domain_policies = self._load_recovery_domain_policies(matches)
        recovery_order_ns = time_ns()
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
            "recovery_order_ns": recovery_order_ns,
            "raw_path": raw_path,
            "raw_sha256": raw_sha256,
            "raw_size_bytes": raw_size_bytes,
        }
        # Persist the recovery manifest first so the message can still be reconstructed
        # if the raw write is interrupted.
        self.storage.write_manifest(message_id, received_at, manifest_payload)
        self.storage.write_raw_message(message_id, received_at, content)

        def operation(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            if smtp_session_id is not None:
                session_exists = connection.execute(
                    "SELECT 1 FROM smtp_sessions WHERE id = ?",
                    (smtp_session_id,),
                ).fetchone()
                if session_exists is None:
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
                        """,
                        (
                            smtp_session_id,
                            "unknown",
                            None,
                            None,
                            0,
                            received_at,
                            received_at,
                            received_at,
                            None,
                        ),
                    )

                self._update_smtp_session_summary(
                    connection,
                    smtp_session_id,
                    received_at,
                    message_delta=1,
                    bytes_received_delta=raw_size_bytes,
                    last_mail_from=envelope_from,
                )

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

            delivery_events: list[dict[str, Any]] = []
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
                delivery_events.append(
                    {
                        "delivery_id": delivery_id,
                        "message_id": message_id,
                        "mailbox": match.address_canonical,
                        "rcpt_to": rcpt_to,
                        "parse_status": "pending",
                        "ts": received_at,
                    }
                )

            return delivery_events

        delivery_events = await self.writer.execute(operation)
        for event in delivery_events:
            await self.live_state.publish({**event, "type": "mailbox_delivery"})
        await self.parse_queue.enqueue(ParseTask(message_id=message_id))
        return f"250 queued as {message_id}"

    async def drain_parser_queue(self) -> None:
        await self.parse_queue.drain()

    async def recover_from_manifest(self, manifest: dict[str, Any]) -> None:
        await self.writer.execute(lambda connection: self._apply_recovery_manifest(connection, manifest))

    async def recover_domain_snapshot(self, snapshot: dict[str, Any]) -> None:
        await self.writer.execute(lambda connection: self._ensure_recovery_domain_record(connection, snapshot, str(snapshot["received_at"])))

    def validate_recovery_manifest(self, manifest: Any) -> None:
        if not isinstance(manifest, dict):
            raise ValueError("invalid recovery manifest")

        for key in ("message_id", "received_at", "raw_path", "raw_sha256", "raw_size_bytes"):
            if key not in manifest:
                raise ValueError("invalid recovery manifest")

        if not all(isinstance(manifest[key], str) for key in ("message_id", "received_at", "raw_path", "raw_sha256")):
            raise ValueError("invalid recovery manifest")
        if "recovery_order_ns" in manifest:
            recovery_order_ns = manifest["recovery_order_ns"]
            if not isinstance(recovery_order_ns, int) or isinstance(recovery_order_ns, bool) or recovery_order_ns < 0:
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

    def _update_smtp_session_summary(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        now: str,
        *,
        accepted_delta: int = 0,
        rejected_delta: int = 0,
        message_delta: int = 0,
        bytes_received_delta: int = 0,
        last_rcpt_to_sample: str | None = None,
        last_mail_from: str | None = None,
        status: str | None = None,
        disconnect_at: str | None = None,
        close_reason: str | None = None,
        result_code: int | None = None,
        result_message: str | None = None,
    ) -> None:
        connection.execute(
            """
            UPDATE smtp_sessions
            SET first_command_at = COALESCE(first_command_at, ?),
                last_command_at = ?,
                message_count = message_count + ?,
                rcpt_accepted_count = rcpt_accepted_count + ?,
                rcpt_rejected_count = rcpt_rejected_count + ?,
                bytes_received = bytes_received + ?,
                last_mail_from = COALESCE(?, last_mail_from),
                last_rcpt_to_sample = COALESCE(?, last_rcpt_to_sample),
                status = COALESCE(?, status),
                disconnect_at = COALESCE(?, disconnect_at),
                close_reason = COALESCE(?, close_reason),
                result_code = COALESCE(?, result_code),
                result_message = COALESCE(?, result_message)
            WHERE id = ?
            """,
            (
                now,
                now,
                message_delta,
                accepted_delta,
                rejected_delta,
                bytes_received_delta,
                last_mail_from,
                last_rcpt_to_sample,
                status,
                disconnect_at,
                close_reason,
                result_code,
                result_message,
                session_id,
            ),
        )

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

    async def get_mailbox_view(
        self,
        mailbox_address: str,
        *,
        limit: int = 50,
        offset: int = 0,
        cursor: tuple[str, str] | None = None,
        request_ip: str | None = None,
    ) -> dict[str, Any]:
        match = self.domains.match_address(mailbox_address)
        if match is None:
            raise LookupError("mailbox domain not managed")

        mailbox = await self._load_public_mailbox(match, request_ip=request_ip)
        cursor_filter = ""
        params: list[Any] = [mailbox["id"]]
        if cursor is not None:
            delivered_at, delivery_id = cursor
            cursor_filter = "AND (d.delivered_at < ? OR (d.delivered_at = ? AND d.id < ?))"
            params.extend([delivered_at, delivered_at, delivery_id])
        page_limit = limit + 1
        params.extend([page_limit, 0 if cursor is not None else offset])
        with connect_database(self.settings.database_path) as connection:
            rows = connection.execute(
                f"""
                SELECT
                    d.id AS delivery_id,
                    d.delivered_at,
                    m.id AS message_id,
                    m.subject,
                    m.from_addr,
                    m.text_preview,
                    m.text_body_path,
                    m.html_body_path,
                    m.has_attachments,
                    m.parse_status
                FROM message_deliveries AS d
                JOIN messages AS m ON m.id = d.message_id
                WHERE d.mailbox_id = ? AND d.status = 'active'
                    {cursor_filter}
                ORDER BY d.delivered_at DESC, d.id DESC
                LIMIT ? OFFSET ?
                """,
                tuple(params),
            ).fetchall()

        items = [dict(row) for row in rows[:limit]]
        message_count = int(mailbox["message_count"])
        has_previous = offset > 0
        has_next = len(rows) > limit if cursor is not None else offset + len(items) < message_count
        next_cursor = None
        if has_next and items:
            last_item = items[-1]
            next_cursor = {
                "delivered_at": last_item["delivered_at"],
                "delivery_id": last_item["delivery_id"],
            }

        return {
            "mailbox": match.address_canonical,
            "items": items,
            "message_count": message_count,
            "limit": limit,
            "offset": offset,
            "pagination_mode": "cursor" if cursor is not None else "offset",
            "next_cursor": next_cursor,
            "has_previous": has_previous,
            "has_next": has_next,
            "previous_offset": max(offset - limit, 0) if has_previous else None,
            "next_offset": offset + limit if has_next else None,
        }

    async def get_mailbox_delivery_item(
        self,
        mailbox_address: str,
        delivery_id: str,
        *,
        request_ip: str | None = None,
    ) -> dict[str, Any]:
        match = self.domains.match_address(mailbox_address)
        if match is None:
            raise LookupError("mailbox domain not managed")

        mailbox = await self._load_public_mailbox(match, request_ip=request_ip)
        with connect_database(self.settings.database_path) as connection:
            row = connection.execute(
                """
                SELECT
                    d.id AS delivery_id,
                    d.delivered_at,
                    m.id AS message_id,
                    m.subject,
                    m.from_addr,
                    m.text_preview,
                    m.text_body_path,
                    m.html_body_path,
                    m.has_attachments,
                    m.parse_status
                FROM message_deliveries AS d
                JOIN messages AS m ON m.id = d.message_id
                WHERE d.id = ? AND d.mailbox_id = ? AND d.status = 'active'
                """,
                (delivery_id, mailbox["id"]),
            ).fetchone()
        if row is None:
            raise LookupError("delivery not found")
        return dict(row)

    async def get_delivery_detail(self, mailbox_address: str, delivery_id: str, *, request_ip: str | None = None) -> dict[str, Any]:
        match = self.domains.match_address(mailbox_address)
        if match is None:
            raise LookupError("mailbox domain not managed")

        mailbox = await self._load_public_mailbox(match, request_ip=request_ip)
        with connect_database(self.settings.database_path) as connection:
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
                WHERE d.id = ? AND d.mailbox_id = ? AND d.status = 'active'
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

    async def _authorize_public_mailbox_access(
        self,
        canonical_mailbox_address: str,
        domain_id: int,
        *,
        request_ip: str | None = None,
    ) -> None:
        context = get_active_permission_context()
        if context is None:
            return
        ensure_mailbox_access(context, canonical_mailbox_address, domain_id, "public.read")
        await self.api_keys.record_usage(context, ip=request_ip)

    async def _load_public_mailbox(self, match, *, request_ip: str | None = None) -> dict[str, Any]:
        with connect_database(self.settings.database_path) as connection:
            mailbox = connection.execute(
                """
                SELECT
                    id,
                    address_canonical,
                    message_count,
                    public_enabled,
                    is_hidden
                FROM mailboxes
                WHERE address_canonical = ?
                """,
                (match.address_canonical,),
            ).fetchone()

        if mailbox is not None and (not bool(mailbox["public_enabled"]) or bool(mailbox["is_hidden"])):
            raise LookupError("mailbox not public")

        await self._authorize_public_mailbox_access(match.address_canonical, match.domain_id, request_ip=request_ip)

        if mailbox is None:
            await self._ensure_mailbox_exists(match)
            with connect_database(self.settings.database_path) as connection:
                mailbox = connection.execute(
                    """
                    SELECT
                        id,
                        address_canonical,
                        message_count,
                        public_enabled,
                        is_hidden
                    FROM mailboxes
                    WHERE address_canonical = ?
                    """,
                    (match.address_canonical,),
                ).fetchone()
            if mailbox is None:
                raise LookupError("mailbox not found")
            if not bool(mailbox["public_enabled"]) or bool(mailbox["is_hidden"]):
                raise LookupError("mailbox not public")

        return dict(mailbox)

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
            raw_bytes = self.storage.read_bytes(message_row["raw_path"])
        except Exception as exc:
            attachment_paths = await self.writer.execute(
                lambda connection: self._mark_message_parse_failed(connection, task.message_id, str(exc))
            )
            self._delete_attachment_files(attachment_paths)
            await self._publish_mailbox_delivery_updates(task.message_id)
            return

        try:
            parsed = self.parser.parse_message(
                task.message_id,
                raw_bytes,
                message_row["received_at"],
            )
        except Exception as exc:
            attachment_paths = await self.writer.execute(
                lambda connection: self._mark_message_parse_failed(connection, task.message_id, str(exc))
            )
            self._delete_attachment_files(attachment_paths)
            await self._publish_mailbox_delivery_updates(task.message_id)
            return

        attachment_paths = await self.writer.execute(
            lambda connection: self._apply_parsed_message(connection, task.message_id, parsed)
        )
        self._delete_attachment_files(attachment_paths)
        await self._publish_mailbox_delivery_updates(task.message_id)

    def _apply_parsed_message(self, connection: sqlite3.Connection, message_id: str, parsed: ParsedMessage) -> list[str]:
        attachment_paths = self._collect_attachment_storage_paths(connection, message_id)
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
        return attachment_paths

    def _mark_message_parse_failed(self, connection: sqlite3.Connection, message_id: str, parse_error: str) -> list[str]:
        attachment_paths = self._collect_attachment_storage_paths(connection, message_id)
        connection.execute("DELETE FROM attachments WHERE message_id = ?", (message_id,))
        connection.execute(
            """
            UPDATE messages
            SET message_id_header = NULL,
                subject = NULL,
                from_name = NULL,
                from_addr = NULL,
                reply_to = NULL,
                date_header = NULL,
                indexed_at = ?,
                parse_status = 'failed',
                parse_error = ?,
                has_text = 0,
                has_html = 0,
                has_attachments = 0,
                attachment_count = 0,
                text_preview = NULL,
                text_body_path = NULL,
                html_body_path = NULL,
                headers_json = NULL
            WHERE id = ?
            """,
            (utc_now(), parse_error, message_id),
        )
        return attachment_paths

    def _collect_attachment_storage_paths(self, connection: sqlite3.Connection, message_id: str) -> list[str]:
        rows = connection.execute(
            """
            SELECT storage_path
            FROM attachments
            WHERE message_id = ?
            """,
            (message_id,),
        ).fetchall()
        return [str(row["storage_path"]) for row in rows]

    def _delete_attachment_files(self, storage_paths: list[str]) -> None:
        for storage_path in storage_paths:
            try:
                self.storage.resolve(storage_path).unlink(missing_ok=True)
            except Exception:
                continue

    async def _publish_mailbox_delivery_updates(self, message_id: str) -> None:
        for event in self._load_mailbox_delivery_update_events(message_id):
            await self.live_state.publish({**event, "type": "mailbox_delivery_updated"})

    def _load_mailbox_delivery_update_events(self, message_id: str) -> list[dict[str, Any]]:
        with connect_database(self.settings.database_path) as connection:
            rows = connection.execute(
                """
                SELECT
                    d.id AS delivery_id,
                    d.rcpt_to,
                    d.delivered_at,
                    m.id AS message_id,
                    m.parse_status,
                    mb.address_canonical AS mailbox
                FROM message_deliveries AS d
                JOIN messages AS m ON m.id = d.message_id
                JOIN mailboxes AS mb ON mb.id = d.mailbox_id
                WHERE d.message_id = ? AND d.status = 'active'
                ORDER BY d.delivered_at ASC, d.id ASC
                """,
                (message_id,),
            ).fetchall()
        return [
            {
                "delivery_id": row["delivery_id"],
                "message_id": row["message_id"],
                "mailbox": row["mailbox"],
                "rcpt_to": row["rcpt_to"],
                "parse_status": row["parse_status"],
                "ts": row["delivered_at"],
            }
            for row in rows
        ]

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
