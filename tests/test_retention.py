from __future__ import annotations

from email.message import EmailMessage

import pytest

import app.runtime as runtime_module
from app.config import Settings
from app.runtime import RapidInboxRuntime
from conftest import connect_database


def _attachment_email_bytes() -> bytes:
    message = EmailMessage()
    message["From"] = "Sender <sender@example.com>"
    message["To"] = "Foo <foo@adb.com>"
    message["Subject"] = "Expiring message"
    message["Message-ID"] = "<expiring@example.com>"
    message.set_content("This message should expire.")
    message.add_attachment(
        b"attachment-body",
        maintype="text",
        subtype="plain",
        filename="report.txt",
    )
    return message.as_bytes()


@pytest.mark.asyncio
async def test_message_retention_deletes_expired_mail_rows_and_files(tmp_path, monkeypatch) -> None:
    settings = Settings(
        storage_root=tmp_path / "storage",
        database_path=tmp_path / "storage" / "app.db",
    )
    runtime = RapidInboxRuntime(settings)

    await runtime.start()
    try:
        monkeypatch.setattr(runtime_module, "utc_now", lambda: "2026-04-18T20:00:00Z")
        await runtime.create_domain("adb.com")
        await runtime.accept_message(
            rcpt_tos=["foo@adb.com"],
            envelope_from="sender@example.com",
            content=_attachment_email_bytes(),
        )
        await runtime.drain_parser_queue()

        with connect_database(settings.database_path) as connection:
            message_row = connection.execute(
                """
                SELECT id, raw_path, received_at, text_body_path, html_body_path
                FROM messages
                """
            ).fetchone()
            attachment_row = connection.execute("SELECT storage_path FROM attachments").fetchone()
            mailbox_row = connection.execute("SELECT id, message_count FROM mailboxes").fetchone()

        storage_paths = [
            message_row["raw_path"],
            message_row["text_body_path"],
            message_row["html_body_path"],
            attachment_row["storage_path"],
            runtime.storage.manifest_path(message_row["id"], message_row["received_at"]),
        ]
        existing_paths = [runtime.storage.resolve(path) for path in storage_paths if path]
        assert all(path.is_file() for path in existing_paths)
        assert mailbox_row["message_count"] == 1

        monkeypatch.setattr(runtime_module, "utc_now", lambda: "2026-04-18T20:09:59Z")
        not_yet_expired = await runtime.cleanup_expired_messages()
        assert not_yet_expired["messages"] == 0

        monkeypatch.setattr(runtime_module, "utc_now", lambda: "2026-04-18T20:10:00Z")
        expired = await runtime.cleanup_expired_messages()

        assert expired["messages"] == 1
        assert expired["deliveries"] == 1
        assert expired["attachments"] == 1
        assert expired["mailboxes"] == 1
        assert expired["files"] == len(existing_paths)
        assert not any(path.exists() for path in existing_paths)

        with connect_database(settings.database_path) as connection:
            counts = {
                "messages": connection.execute("SELECT COUNT(*) AS count FROM messages").fetchone()["count"],
                "deliveries": connection.execute("SELECT COUNT(*) AS count FROM message_deliveries").fetchone()["count"],
                "attachments": connection.execute("SELECT COUNT(*) AS count FROM attachments").fetchone()["count"],
                "metrics": connection.execute(
                    "SELECT COALESCE(SUM(deliveries), 0) AS count FROM mail_metric_buckets"
                ).fetchone()["count"],
            }
            mailbox = connection.execute(
                "SELECT message_count, latest_message_at FROM mailboxes WHERE id = ?",
                (mailbox_row["id"],),
            ).fetchone()

        assert counts == {"messages": 0, "deliveries": 0, "attachments": 0, "metrics": 1}
        assert mailbox is None
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_retention_deletes_empty_mailboxes_after_ten_minutes(tmp_path, monkeypatch) -> None:
    settings = Settings(
        storage_root=tmp_path / "storage",
        database_path=tmp_path / "storage" / "app.db",
    )
    runtime = RapidInboxRuntime(settings)

    await runtime.start()
    try:
        await runtime.create_domain("adb.com")
        with connect_database(settings.database_path) as connection:
            domain_id = connection.execute(
                "SELECT id FROM domains WHERE root_domain_ascii = 'adb.com'"
            ).fetchone()["id"]
            connection.executemany(
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
                ) VALUES (?, ?, 'adb.com', ?, ?, ?, ?, NULL, 0)
                """,
                [
                    (
                        domain_id,
                        "old-empty",
                        "old-empty@adb.com",
                        "old-empty@adb.com",
                        "2026-04-18T20:00:00Z",
                        "2026-04-18T20:00:00Z",
                    ),
                    (
                        domain_id,
                        "fresh-empty",
                        "fresh-empty@adb.com",
                        "fresh-empty@adb.com",
                        "2026-04-18T20:00:01Z",
                        "2026-04-18T20:00:01Z",
                    ),
                ],
            )

        monkeypatch.setattr(runtime_module, "utc_now", lambda: "2026-04-18T20:10:00Z")
        expired = await runtime.cleanup_expired_messages()

        assert expired["mailboxes"] == 1

        with connect_database(settings.database_path) as connection:
            remaining = {
                str(row["address_canonical"])
                for row in connection.execute("SELECT address_canonical FROM mailboxes").fetchall()
            }

        assert remaining == {"fresh-empty@adb.com"}
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_retention_deletes_stale_smtp_sessions_without_dropping_active_connection(tmp_path, monkeypatch) -> None:
    settings = Settings(
        storage_root=tmp_path / "storage",
        database_path=tmp_path / "storage" / "app.db",
    )
    runtime = RapidInboxRuntime(settings)

    await runtime.start()
    try:
        allowed, reason = await runtime.register_smtp_connection("smtp_active", "127.0.0.1")
        assert allowed is True
        assert reason is None

        with connect_database(settings.database_path) as connection:
            for session_id, status, ts in (
                ("smtp_closed_old", "closed", "2026-04-18T20:00:00Z"),
                ("smtp_error_old", "error", "2026-04-18T20:00:00Z"),
                ("smtp_inactive_open_old", "open", "2026-04-18T20:00:00Z"),
                ("smtp_active", "open", "2026-04-18T20:00:00Z"),
                ("smtp_closed_fresh", "closed", "2026-04-18T20:00:01Z"),
            ):
                connection.execute(
                    """
                    INSERT INTO smtp_sessions (
                        id,
                        remote_ip,
                        status,
                        connect_at,
                        disconnect_at,
                        last_command_at
                    ) VALUES (?, '127.0.0.1', ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        status,
                        ts,
                        ts if status != "open" else None,
                        ts,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO smtp_events (session_id, seq, event_type, ts, payload_json)
                    VALUES (?, 1, 'connect', ?, '{}')
                    """,
                    (session_id, ts),
                )

        monkeypatch.setattr(runtime_module, "utc_now", lambda: "2026-04-18T20:10:00Z")
        expired = await runtime.cleanup_expired_messages()

        assert expired["smtp_sessions"] == 3

        with connect_database(settings.database_path) as connection:
            remaining_sessions = {
                str(row["id"])
                for row in connection.execute("SELECT id FROM smtp_sessions ORDER BY id").fetchall()
            }
            remaining_events = {
                str(row["session_id"])
                for row in connection.execute("SELECT session_id FROM smtp_events ORDER BY session_id").fetchall()
            }

        assert remaining_sessions == {"smtp_active", "smtp_closed_fresh"}
        assert remaining_events == {"smtp_active", "smtp_closed_fresh"}
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_metric_bucket_retention_runs_without_expired_mail_or_sessions(tmp_path, monkeypatch) -> None:
    settings = Settings(
        storage_root=tmp_path / "storage",
        database_path=tmp_path / "storage" / "app.db",
    )
    runtime = RapidInboxRuntime(settings)

    await runtime.start()
    try:
        with connect_database(settings.database_path) as connection:
            connection.executemany(
                """
                INSERT INTO mail_metric_buckets (bucket_ts, deliveries, parse_failures)
                VALUES (?, ?, ?)
                """,
                [
                    ("2026-04-18T19:59:59Z", 1, 0),
                    ("2026-04-18T20:00:00Z", 2, 0),
                    ("2026-04-20T19:59:00Z", 3, 1),
                ],
            )

        monkeypatch.setattr(runtime_module, "utc_now", lambda: "2026-04-20T20:00:00Z")
        expired = await runtime.cleanup_expired_messages()

        assert expired["messages"] == 0
        assert expired["smtp_sessions"] == 0
        assert expired["metric_buckets"] == 1

        with connect_database(settings.database_path) as connection:
            rows = connection.execute(
                """
                SELECT bucket_ts, deliveries, parse_failures
                FROM mail_metric_buckets
                ORDER BY bucket_ts
                """
            ).fetchall()

        assert [dict(row) for row in rows] == [
            {"bucket_ts": "2026-04-18T20:00:00Z", "deliveries": 2, "parse_failures": 0},
            {"bucket_ts": "2026-04-20T19:59:00Z", "deliveries": 3, "parse_failures": 1},
        ]
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_deleted_expired_manifest_is_not_recovered_on_restart(tmp_path, monkeypatch) -> None:
    settings = Settings(
        storage_root=tmp_path / "storage",
        database_path=tmp_path / "storage" / "app.db",
    )
    runtime = RapidInboxRuntime(settings)

    await runtime.start()
    try:
        monkeypatch.setattr(runtime_module, "utc_now", lambda: "2026-04-18T20:00:00Z")
        await runtime.create_domain("adb.com")
        await runtime.accept_message(
            rcpt_tos=["foo@adb.com"],
            envelope_from="sender@example.com",
            content=_attachment_email_bytes(),
        )
        await runtime.drain_parser_queue()

        monkeypatch.setattr(runtime_module, "utc_now", lambda: "2026-04-18T20:10:00Z")
        await runtime.cleanup_expired_messages()
        assert not list(settings.manifests_dir.rglob("*.json"))
    finally:
        await runtime.stop()

    restarted = RapidInboxRuntime(settings)
    monkeypatch.setattr(runtime_module, "utc_now", lambda: "2026-04-18T20:05:00Z")
    await restarted.start()
    try:
        with connect_database(settings.database_path) as connection:
            message_count = connection.execute("SELECT COUNT(*) AS count FROM messages").fetchone()["count"]
    finally:
        await restarted.stop()

    assert message_count == 0
