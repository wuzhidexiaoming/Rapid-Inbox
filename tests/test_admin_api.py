from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.config import Settings
from app.db.connection import connect_database
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
