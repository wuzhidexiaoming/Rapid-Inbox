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

        monkeypatch.setattr(runtime_module, "utc_now", lambda: "2026-04-18T20:19:59Z")
        not_yet_expired = await runtime.cleanup_expired_messages()
        assert not_yet_expired["messages"] == 0

        monkeypatch.setattr(runtime_module, "utc_now", lambda: "2026-04-18T20:20:00Z")
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
            }
            mailbox = connection.execute(
                "SELECT message_count, latest_message_at FROM mailboxes WHERE id = ?",
                (mailbox_row["id"],),
            ).fetchone()

        assert counts == {"messages": 0, "deliveries": 0, "attachments": 0}
        assert mailbox["message_count"] == 0
        assert mailbox["latest_message_at"] is None
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

        monkeypatch.setattr(runtime_module, "utc_now", lambda: "2026-04-18T20:20:00Z")
        await runtime.cleanup_expired_messages()
        assert not list(settings.manifests_dir.rglob("*.json"))
    finally:
        await runtime.stop()

    restarted = RapidInboxRuntime(settings)
    monkeypatch.setattr(runtime_module, "utc_now", lambda: "2026-04-18T20:10:00Z")
    await restarted.start()
    try:
        with connect_database(settings.database_path) as connection:
            message_count = connection.execute("SELECT COUNT(*) AS count FROM messages").fetchone()["count"]
    finally:
        await restarted.stop()

    assert message_count == 0
