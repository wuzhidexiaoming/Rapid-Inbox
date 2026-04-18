from __future__ import annotations

import asyncio
from types import SimpleNamespace
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.db.connection import connect_database
from app.ingest.parser import ParsedMessage
from app.ingest.queue import ParseTask
from app.main import create_app
from app.smtp.handler import RapidInboxHandler


@pytest.mark.asyncio
async def test_admin_domain_api_creates_and_lists_domains(tmp_path) -> None:
    settings = Settings(
        storage_root=tmp_path / "storage",
        database_path=tmp_path / "storage" / "app.db",
        admin_token="admin-secret",
    )
    app = create_app(settings=settings)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            created = await client.post(
                "/api/v1/admin/domains",
                headers={"X-API-Key": settings.admin_token},
                json={"root_domain": "adb.com", "accept_subdomains": True},
            )
            listed = await client.get(
                "/api/v1/admin/domains",
                headers={"X-API-Key": settings.admin_token},
            )

        assert created.status_code == 201
        assert created.json()["root_domain_ascii"] == "adb.com"
        assert listed.status_code == 200
        assert listed.json()["items"][0]["root_domain_ascii"] == "adb.com"


@pytest.mark.asyncio
async def test_admin_api_supports_message_reparse_and_settings_update(admin_client, runtime, seeded_message) -> None:
    reparse = await admin_client.post(f"/api/v1/admin/messages/{seeded_message.message_id}/reparse")
    settings_response = await admin_client.patch(
        "/api/v1/admin/settings",
        json={"max_recipients_per_message": "25"},
    )
    audit = await admin_client.get("/api/v1/admin/audit-logs")

    assert reparse.status_code == 202
    assert settings_response.status_code == 200
    assert audit.status_code == 200
    assert any(item["action"] == "settings.update" for item in audit.json()["items"])
    assert all("details_json" not in item for item in audit.json()["items"])


@pytest.mark.asyncio
async def test_admin_api_mutation_succeeds_when_audit_logging_fails(admin_client, runtime) -> None:
    async def failing_audit_log(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    runtime.audit.log = failing_audit_log

    response = await admin_client.patch(
        "/api/v1/admin/settings",
        json={"max_recipients_per_message": "33"},
    )

    assert response.status_code == 200
    assert response.json()["max_recipients_per_message"] == 33


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query_string",
    [
        "?limit=-1",
        "?limit=1001",
        "?offset=-1",
    ],
)
async def test_admin_api_rejects_invalid_audit_pagination(admin_client, query_string: str) -> None:
    response = await admin_client.get(f"/api/v1/admin/audit-logs{query_string}")

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_admin_api_rejects_invalid_domain_payload(admin_client) -> None:
    response = await admin_client.post(
        "/api/v1/admin/domains",
        json={
            "root_domain": "bad.adb.com",
            "plus_addressing_mode": "bogus",
        },
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_admin_api_read_endpoints_accept_read_scopes(admin_client, runtime) -> None:
    await runtime.create_domain("adb.com")
    read_only_key = await runtime.api_keys.create_key(
        name="fixture-admin-read",
        kind="admin",
        scopes=["domains.read", "system.read"],
        domain_ids=[],
        mailbox_patterns=[],
    )

    domains_response = await admin_client.get(
        "/api/v1/admin/domains",
        headers={"X-API-Key": read_only_key["plain_text"]},
    )
    settings_response = await admin_client.get(
        "/api/v1/admin/settings",
        headers={"X-API-Key": read_only_key["plain_text"]},
    )

    assert domains_response.status_code == 200
    domain = domains_response.json()["items"][0]
    assert domain["root_domain_ascii"] == "adb.com"
    assert isinstance(domain["accept_exact"], bool)
    assert isinstance(domain["public_web_enabled"], bool)
    assert isinstance(domain["is_active"], bool)
    assert settings_response.status_code == 200
    assert settings_response.json()["max_message_size_bytes"] == runtime.settings.max_message_size_bytes


@pytest.mark.asyncio
async def test_admin_mailbox_service_normalizes_boolean_flags(runtime, sample_email_bytes) -> None:
    await runtime.create_domain("adb.com")
    await runtime.ensure_smtp_session(
        "smtp_mailbox_booleans",
        SimpleNamespace(peer=("127.0.0.1", 2525), host_name="pytest", ssl=None),
    )
    await runtime.accept_message(
        rcpt_tos=["foo@adb.com"],
        envelope_from="sender@example.com",
        content=sample_email_bytes,
        smtp_session_id="smtp_mailbox_booleans",
    )
    await runtime.drain_parser_queue()

    mailbox = runtime.mailboxes.list_mailboxes()["items"][0]

    assert mailbox["address_canonical"] == "foo@adb.com"
    assert isinstance(mailbox["public_enabled"], bool)
    assert isinstance(mailbox["is_hidden"], bool)
    assert mailbox["public_enabled"] is True
    assert mailbox["is_hidden"] is False


@pytest.mark.asyncio
async def test_admin_api_reparse_failure_clears_stale_parsed_fields(runtime, monkeypatch) -> None:
    attachment_email_bytes = (
        b"From: Sender <sender@example.com>\r\n"
        b"To: Foo <foo@adb.com>\r\n"
        b"Subject: Attachment Mail\r\n"
        b"Message-ID: <attachment@example.com>\r\n"
        b"Date: Sat, 18 Apr 2026 20:00:00 +0000\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: multipart/mixed; boundary=boundary99\r\n"
        b"\r\n"
        b"--boundary99\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"Attachment body.\r\n"
        b"\r\n"
        b"--boundary99\r\n"
        b"Content-Type: text/plain\r\n"
        b'Content-Disposition: attachment; filename="report.txt"\r\n'
        b"\r\n"
        b"attachment contents\r\n"
        b"\r\n"
        b"--boundary99--\r\n"
    )
    await runtime.create_domain("adb.com")
    await runtime.ensure_smtp_session(
        "smtp_reparse_failure",
        SimpleNamespace(peer=("127.0.0.1", 2525), host_name="pytest", ssl=None),
    )
    await runtime.accept_message(
        rcpt_tos=["foo@adb.com"],
        envelope_from="sender@example.com",
        content=attachment_email_bytes,
        smtp_session_id="smtp_reparse_failure",
    )
    await runtime.drain_parser_queue()
    mailbox = await runtime.get_mailbox_view("foo@adb.com")
    message_id = mailbox["items"][0]["message_id"]

    with connect_database(runtime.settings.database_path) as connection:
        attachment_row = connection.execute(
            """
            SELECT storage_path
            FROM attachments
            WHERE message_id = ?
            """,
            (message_id,),
        ).fetchone()
    attachment_path = runtime.storage.resolve(attachment_row["storage_path"])
    assert attachment_path.exists() is True

    def failing_parse_message(*args, **kwargs):
        raise ValueError("parse failed")

    monkeypatch.setattr(runtime.parser, "parse_message", failing_parse_message)

    await runtime.messages.reparse_message(message_id)
    await runtime.drain_parser_queue()

    with connect_database(runtime.settings.database_path) as connection:
        row = connection.execute(
            """
            SELECT
                parse_status,
                parse_error,
                message_id_header,
                subject,
                from_name,
                from_addr,
                reply_to,
                date_header,
                has_text,
                has_html,
                has_attachments,
                attachment_count,
                text_preview,
                text_body_path,
                html_body_path,
                headers_json
            FROM messages
            WHERE id = ?
            """,
            (message_id,),
        ).fetchone()
        attachment_count = connection.execute(
            "SELECT COUNT(*) AS count FROM attachments WHERE message_id = ?",
            (message_id,),
        ).fetchone()

    assert attachment_path.exists() is False
    assert row["parse_status"] == "failed"
    assert row["parse_error"] == "parse failed"
    assert row["message_id_header"] is None
    assert row["subject"] is None
    assert row["from_name"] is None
    assert row["from_addr"] is None
    assert row["reply_to"] is None
    assert row["date_header"] is None
    assert row["has_text"] == 0
    assert row["has_html"] == 0
    assert row["has_attachments"] == 0
    assert row["attachment_count"] == 0
    assert row["text_preview"] is None
    assert row["text_body_path"] is None
    assert row["html_body_path"] is None
    assert row["headers_json"] is None
    assert attachment_count["count"] == 0


@pytest.mark.asyncio
async def test_admin_api_parse_failure_keeps_attachment_files_when_db_write_fails(runtime, monkeypatch) -> None:
    attachment_email_bytes = (
        b"From: Sender <sender@example.com>\r\n"
        b"To: Foo <foo@adb.com>\r\n"
        b"Subject: Attachment Mail\r\n"
        b"Message-ID: <attachment-dberror@example.com>\r\n"
        b"Date: Sat, 18 Apr 2026 20:00:00 +0000\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: multipart/mixed; boundary=boundary99\r\n"
        b"\r\n"
        b"--boundary99\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"Attachment body.\r\n"
        b"\r\n"
        b"--boundary99\r\n"
        b"Content-Type: text/plain\r\n"
        b'Content-Disposition: attachment; filename="report.txt"\r\n'
        b"\r\n"
        b"attachment contents\r\n"
        b"\r\n"
        b"--boundary99--\r\n"
    )
    await runtime.create_domain("adb.com")
    await runtime.ensure_smtp_session(
        "smtp_reparse_db_failure",
        SimpleNamespace(peer=("127.0.0.1", 2525), host_name="pytest", ssl=None),
    )
    await runtime.accept_message(
        rcpt_tos=["foo@adb.com"],
        envelope_from="sender@example.com",
        content=attachment_email_bytes,
        smtp_session_id="smtp_reparse_db_failure",
    )
    await runtime.drain_parser_queue()
    mailbox = await runtime.get_mailbox_view("foo@adb.com")
    message_id = mailbox["items"][0]["message_id"]

    with connect_database(runtime.settings.database_path) as connection:
        attachment_row = connection.execute(
            """
            SELECT storage_path
            FROM attachments
            WHERE message_id = ?
            """,
            (message_id,),
        ).fetchone()
    attachment_path = runtime.storage.resolve(attachment_row["storage_path"])
    assert attachment_path.exists() is True

    def failing_parse_message(*args, **kwargs):
        raise ValueError("parse failed")

    async def failing_execute(operation):
        with connect_database(runtime.settings.database_path) as connection:
            operation(connection)
            connection.rollback()
        raise RuntimeError("db write failed")

    monkeypatch.setattr(runtime.parser, "parse_message", failing_parse_message)
    monkeypatch.setattr(runtime.writer, "execute", failing_execute)

    with pytest.raises(RuntimeError, match="db write failed"):
        await runtime._parse_message(ParseTask(message_id=message_id))

    assert attachment_path.exists() is True


@pytest.mark.asyncio
async def test_admin_api_successful_reparse_replaces_attachment_files(runtime) -> None:
    attachment_email_bytes = (
        b"From: Sender <sender@example.com>\r\n"
        b"To: Foo <foo@adb.com>\r\n"
        b"Subject: Attachment Mail\r\n"
        b"Message-ID: <attachment-success@example.com>\r\n"
        b"Date: Sat, 18 Apr 2026 20:00:00 +0000\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: multipart/mixed; boundary=boundary99\r\n"
        b"\r\n"
        b"--boundary99\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"Attachment body.\r\n"
        b"\r\n"
        b"--boundary99\r\n"
        b"Content-Type: text/plain\r\n"
        b'Content-Disposition: attachment; filename="report.txt"\r\n'
        b"\r\n"
        b"attachment contents\r\n"
        b"\r\n"
        b"--boundary99--\r\n"
    )
    await runtime.create_domain("adb.com")
    await runtime.ensure_smtp_session(
        "smtp_reparse_success",
        SimpleNamespace(peer=("127.0.0.1", 2525), host_name="pytest", ssl=None),
    )
    await runtime.accept_message(
        rcpt_tos=["foo@adb.com"],
        envelope_from="sender@example.com",
        content=attachment_email_bytes,
        smtp_session_id="smtp_reparse_success",
    )
    await runtime.drain_parser_queue()
    mailbox = await runtime.get_mailbox_view("foo@adb.com")
    message_id = mailbox["items"][0]["message_id"]

    with connect_database(runtime.settings.database_path) as connection:
        attachment_row = connection.execute(
            """
            SELECT storage_path
            FROM attachments
            WHERE message_id = ?
            """,
            (message_id,),
        ).fetchone()
    old_attachment_path = runtime.storage.resolve(attachment_row["storage_path"])
    assert old_attachment_path.exists() is True

    def successful_parse_message(*args, **kwargs):
        return ParsedMessage(
            message_id_header="<reparsed@example.com>",
            subject="Reparsed Attachment Mail",
            from_name="Sender",
            from_addr="sender@example.com",
            reply_to=None,
            date_header="Sat, 18 Apr 2026 20:00:00 +0000",
            has_text=True,
            has_html=False,
            has_attachments=False,
            attachment_count=0,
            text_preview="Reparsed attachment body.",
            text_body_path=None,
            html_body_path=None,
            headers_json="[]",
            attachments=[],
        )

    runtime.parser.parse_message = successful_parse_message

    await runtime.messages.reparse_message(message_id)
    await runtime.drain_parser_queue()

    with connect_database(runtime.settings.database_path) as connection:
        attachment_count = connection.execute(
            "SELECT COUNT(*) AS count FROM attachments WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        row = connection.execute(
            "SELECT parse_status, subject FROM messages WHERE id = ?",
            (message_id,),
        ).fetchone()

    assert old_attachment_path.exists() is False
    assert attachment_count["count"] == 0
    assert row["parse_status"] == "parsed"
    assert row["subject"] == "Reparsed Attachment Mail"


@pytest.mark.asyncio
async def test_admin_api_attachment_cleanup_errors_do_not_stop_later_reparses(runtime, monkeypatch) -> None:
    attachment_email_bytes = (
        b"From: Sender <sender@example.com>\r\n"
        b"To: Foo <foo@adb.com>\r\n"
        b"Subject: Attachment Mail\r\n"
        b"Message-ID: <attachment-cleanup-error@example.com>\r\n"
        b"Date: Sat, 18 Apr 2026 20:00:00 +0000\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: multipart/mixed; boundary=boundary99\r\n"
        b"\r\n"
        b"--boundary99\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"Attachment body.\r\n"
        b"\r\n"
        b"--boundary99\r\n"
        b"Content-Type: text/plain\r\n"
        b'Content-Disposition: attachment; filename="report.txt"\r\n'
        b"\r\n"
        b"attachment contents\r\n"
        b"\r\n"
        b"--boundary99--\r\n"
    )
    second_email_bytes = (
        b"From: Sender <sender@example.com>\r\n"
        b"To: Foo <foo@adb.com>\r\n"
        b"Subject: Plain Mail\r\n"
        b"Message-ID: <plain-cleanup-error@example.com>\r\n"
        b"Date: Sat, 18 Apr 2026 20:05:00 +0000\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"Plain message body.\r\n"
    )
    await runtime.create_domain("adb.com")
    await runtime.ensure_smtp_session(
        "smtp_cleanup_error",
        SimpleNamespace(peer=("127.0.0.1", 2525), host_name="pytest", ssl=None),
    )
    await runtime.accept_message(
        rcpt_tos=["foo@adb.com"],
        envelope_from="sender@example.com",
        content=attachment_email_bytes,
        smtp_session_id="smtp_cleanup_error",
    )
    await runtime.accept_message(
        rcpt_tos=["foo@adb.com"],
        envelope_from="sender@example.com",
        content=second_email_bytes,
        smtp_session_id="smtp_cleanup_error",
    )
    await runtime.drain_parser_queue()

    with connect_database(runtime.settings.database_path) as connection:
        first_message_id = connection.execute(
            "SELECT id FROM messages WHERE subject = ?",
            ("Attachment Mail",),
        ).fetchone()["id"]
        second_message_id = connection.execute(
            "SELECT id FROM messages WHERE subject = ?",
            ("Plain Mail",),
        ).fetchone()["id"]
        first_attachment_row = connection.execute(
            "SELECT storage_path FROM attachments WHERE message_id = ?",
            (first_message_id,),
        ).fetchone()
    first_attachment_path = runtime.storage.resolve(first_attachment_row["storage_path"])

    original_unlink = Path.unlink

    def failing_unlink(self, missing_ok: bool = False):  # type: ignore[override]
        if self == first_attachment_path:
            raise RuntimeError("cleanup failed")
        return original_unlink(self, missing_ok=missing_ok)

    def reparsed_message(message_id: str) -> ParsedMessage:
        return ParsedMessage(
            message_id_header=f"<reparsed-{message_id}@example.com>",
            subject=f"reparsed-{message_id}",
            from_name="Sender",
            from_addr="sender@example.com",
            reply_to=None,
            date_header="Sat, 18 Apr 2026 20:00:00 +0000",
            has_text=True,
            has_html=False,
            has_attachments=False,
            attachment_count=0,
            text_preview=f"reparsed-{message_id}",
            text_body_path=None,
            html_body_path=None,
            headers_json="[]",
            attachments=[],
        )

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    monkeypatch.setattr(runtime.parser, "parse_message", lambda message_id, raw_content, received_at: reparsed_message(message_id))

    await runtime.messages.reparse_message(first_message_id)
    await runtime.messages.reparse_message(second_message_id)
    await asyncio.wait_for(runtime.drain_parser_queue(), timeout=2)

    with connect_database(runtime.settings.database_path) as connection:
        first_row = connection.execute(
            "SELECT subject FROM messages WHERE id = ?",
            (first_message_id,),
        ).fetchone()
        second_row = connection.execute(
            "SELECT subject FROM messages WHERE id = ?",
            (second_message_id,),
        ).fetchone()

    assert first_attachment_path.exists() is True
    assert first_row["subject"] == f"reparsed-{first_message_id}"
    assert second_row["subject"] == f"reparsed-{second_message_id}"


@pytest.mark.asyncio
async def test_admin_api_settings_update_applies_live_message_size_limit(admin_client, runtime) -> None:
    runtime.settings.max_message_size_bytes = 10
    await runtime.create_domain("adb.com")
    handler = RapidInboxHandler(runtime)
    session = SimpleNamespace(peer=("127.0.0.1", 2525), host_name="pytest", ssl=None)
    envelope = SimpleNamespace(
        rcpt_tos=[],
        mail_from="sender@example.com",
        content=(
            b"From: Sender <sender@example.com>\r\n"
            b"To: Foo <foo@adb.com>\r\n"
            b"Subject: Live settings\r\n"
            b"\r\n"
            b"hello world\r\n"
        ),
    )

    await handler.handle_RCPT(None, session, envelope, "foo@adb.com", [])
    rejected = await handler.handle_DATA(None, session, envelope)
    settings_response = await admin_client.patch(
        "/api/v1/admin/settings",
        json={"max_message_size_bytes": "100"},
    )
    accepted = await handler.handle_DATA(None, session, envelope)

    await runtime.drain_parser_queue()

    assert rejected == "552 message too large"
    assert settings_response.status_code == 200
    assert runtime.settings.max_message_size_bytes == 100
    assert accepted.startswith("250 queued as ")


@pytest.mark.asyncio
async def test_admin_api_settings_reload_from_database_on_restart(tmp_path) -> None:
    storage_root = tmp_path / "storage"
    database_path = storage_root / "app.db"
    settings = Settings(
        storage_root=storage_root,
        database_path=database_path,
    )
    app = create_app(settings=settings)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            admin_key = await app.state.runtime.api_keys.create_key(
                name="restart-admin",
                kind="admin",
                scopes=["system.write"],
                domain_ids=[],
                mailbox_patterns=[],
            )
            response = await client.patch(
                "/api/v1/admin/settings",
                headers={"X-API-Key": admin_key["plain_text"]},
                json={"max_message_size_bytes": "10"},
            )

        assert response.status_code == 200

    restarted_settings = Settings(
        storage_root=storage_root,
        database_path=database_path,
    )
    restarted_app = create_app(settings=restarted_settings)

    async with restarted_app.router.lifespan_context(restarted_app):
        assert restarted_app.state.runtime.settings.max_message_size_bytes == 10
        await restarted_app.state.runtime.create_domain("adb.com")
        handler = RapidInboxHandler(restarted_app.state.runtime)
        session = SimpleNamespace(peer=("127.0.0.1", 2525), host_name="pytest", ssl=None)
        envelope = SimpleNamespace(
            rcpt_tos=[],
            mail_from="sender@example.com",
            content=b"01234567890",
        )

        await handler.handle_RCPT(None, session, envelope, "foo@adb.com", [])
        result = await handler.handle_DATA(None, session, envelope)

        assert result == "552 message too large"


@pytest.mark.asyncio
async def test_admin_api_settings_allowlist_rejects_unsupported_keys_and_filters_echo(admin_client, runtime) -> None:
    rejected = await admin_client.patch(
        "/api/v1/admin/settings",
        json={"admin_token": "super-secret"},
    )

    with connect_database(runtime.settings.database_path) as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO system_settings (key, value, updated_at)
            VALUES (?, ?, ?)
            """,
            ("admin_token", "super-secret", "2026-04-18T00:00:00Z"),
        )

    listed = await admin_client.get("/api/v1/admin/settings")

    assert rejected.status_code == 422
    assert "admin_token" not in listed.json()
    assert set(listed.json()) == {"max_message_size_bytes", "max_recipients_per_message"}


@pytest.mark.asyncio
async def test_admin_api_rejects_fractional_settings_values(admin_client) -> None:
    response = await admin_client.patch(
        "/api/v1/admin/settings",
        json={"max_recipients_per_message": 12.5},
    )

    assert response.status_code == 422
