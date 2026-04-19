from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import count
import re

import pytest

from app.db.connection import connect_database
import app.runtime as runtime_module


def _patch_sequenced_utc_now(monkeypatch) -> None:
    base = datetime(2026, 4, 18, 20, 0, 0, tzinfo=UTC)
    ticks = count()

    monkeypatch.setattr(
        runtime_module,
        "utc_now",
        lambda: (base + timedelta(seconds=next(ticks))).isoformat().replace("+00:00", "Z"),
    )


@pytest.mark.asyncio
async def test_admin_login_and_dashboard_page_flow(app_client, runtime) -> None:
    response = await app_client.post(
        "/admin/login",
        data={"username": "admin", "password": runtime.settings.bootstrap_admin_password},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "高效、优雅的域名与邮件管控平台" in response.text
    assert "域名管理" in response.text
    assert 'href="/admin/live"' in response.text


@pytest.mark.asyncio
async def test_admin_logout_revokes_session_and_clears_cookie(app_client, runtime) -> None:
    cookie_name = runtime.settings.session_cookie_name

    await app_client.post(
        "/admin/login",
        data={"username": "admin", "password": runtime.settings.bootstrap_admin_password},
        follow_redirects=True,
    )
    assert app_client.cookies.get(cookie_name) is not None

    response = await app_client.post("/admin/logout")

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"
    assert app_client.cookies.get(cookie_name) is None

    with connect_database(runtime.settings.database_path) as connection:
        row = connection.execute(
            """
            SELECT revoked_at
            FROM admin_sessions
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()

    assert row["revoked_at"] is not None

    redirect = await app_client.get("/admin")
    assert redirect.status_code == 303
    assert redirect.headers["location"] == "/admin/login"


@pytest.mark.asyncio
async def test_admin_pages_redirect_unauthenticated_users_to_login(app_client) -> None:
    response = await app_client.get("/admin")

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


@pytest.mark.asyncio
async def test_admin_live_placeholder_route_is_not_exposed(app_client) -> None:
    response = await app_client.get("/admin/live")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_admin_live_page_uses_cursor_based_stream_url(app_client, runtime) -> None:
    await app_client.post(
        "/admin/login",
        data={"username": "admin", "password": runtime.settings.bootstrap_admin_password},
        follow_redirects=True,
    )

    response = await app_client.get("/admin/live")

    assert response.status_code == 200
    assert "实时监控中枢" in response.text
    assert "after_cursor=" in response.text


@pytest.mark.asyncio
async def test_admin_login_rejects_invalid_credentials_with_error(app_client) -> None:
    response = await app_client.post(
        "/admin/login",
        data={"username": "admin", "password": "not-the-password"},
    )

    assert response.status_code == 401
    assert "用户名或密码不正确。" in response.text


@pytest.mark.asyncio
async def test_admin_domains_page_can_create_domain_via_form(app_client, runtime) -> None:
    login = await app_client.post(
        "/admin/login",
        data={"username": "admin", "password": runtime.settings.bootstrap_admin_password},
        follow_redirects=True,
    )
    response = await app_client.post(
        "/admin/domains",
        data={
            "root_domain": "mail.adb.com",
            "accept_exact": "1",
            "accept_subdomains": "1",
            "public_web_enabled": "1",
            "public_api_enabled": "1",
            "plus_addressing_mode": "keep",
            "local_part_case_sensitive": "0",
            "is_active": "1",
            "max_message_size_bytes": "2048",
        },
    )

    created = runtime.domains.list_domains()[0]
    created_detail = runtime.domains.get_domain(created["id"])

    assert login.status_code == 200
    assert response.status_code == 303
    assert response.headers["location"] == f"/admin/domains/{created['id']}"
    assert created["root_domain_ascii"] == "mail.adb.com"
    assert created_detail["accept_subdomains"] is True
    assert created_detail["public_web_enabled"] is True
    assert created_detail["max_message_size_bytes"] == 2048


@pytest.mark.asyncio
async def test_admin_mailboxes_page_can_toggle_visibility_via_form(app_client, runtime, sample_email_bytes: bytes) -> None:
    await runtime.create_domain("adb.com")
    await runtime.accept_message(
        rcpt_tos=["foo@adb.com"],
        envelope_from="sender@example.com",
        content=sample_email_bytes,
    )
    await runtime.drain_parser_queue()

    login = await app_client.post(
        "/admin/login",
        data={"username": "admin", "password": runtime.settings.bootstrap_admin_password},
        follow_redirects=True,
    )
    mailbox = runtime.mailboxes.list_mailboxes()["items"][0]
    response = await app_client.post(
        f"/admin/mailboxes/{mailbox['id']}",
        data={
            "public_enabled": "0",
            "is_hidden": "1",
        },
    )

    updated = runtime.mailboxes.get_mailbox(mailbox["id"])

    assert login.status_code == 200
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/mailboxes?limit=20&offset=0"
    assert updated["public_enabled"] is False
    assert updated["is_hidden"] is True


@pytest.mark.asyncio
async def test_admin_api_keys_page_can_create_and_revoke_via_form(app_client, runtime) -> None:
    login = await app_client.post(
        "/admin/login",
        data={"username": "admin", "password": runtime.settings.bootstrap_admin_password},
        follow_redirects=True,
    )
    created = await app_client.post(
        "/admin/api-keys",
        data={
            "name": "html-admin-key",
            "kind": "admin",
            "scopes": "domains.read",
        },
    )
    match = re.search(r"(ri_admin_[a-f0-9]+_[A-Za-z0-9_-]+)", created.text)

    assert login.status_code == 200
    assert created.status_code in {200, 201}
    assert match is not None

    plain_text = match.group(1)
    with connect_database(runtime.settings.database_path) as connection:
        row = connection.execute(
            """
            SELECT id, status
            FROM api_keys
            WHERE name = ?
            """,
            ("html-admin-key",),
        ).fetchone()

    assert row["status"] == "active"

    revoked = await app_client.post(f"/admin/api-keys/{row['id']}/revoke", follow_redirects=True)

    with connect_database(runtime.settings.database_path) as connection:
        after = connection.execute(
            """
            SELECT status
            FROM api_keys
            WHERE id = ?
            """,
            (row["id"],),
        ).fetchone()

    denied = await app_client.get(
        "/api/v1/admin/domains",
        headers={"X-API-Key": plain_text},
    )

    assert revoked.status_code == 200
    assert after["status"] == "revoked"
    assert denied.status_code == 401


@pytest.mark.asyncio
async def test_admin_mailboxes_page_paginates_results(app_client, runtime, monkeypatch) -> None:
    _patch_sequenced_utc_now(monkeypatch)

    await runtime.create_domain("adb.com")
    for index in range(21):
        await runtime.accept_message(
            rcpt_tos=[f"user{index:02d}@adb.com"],
            envelope_from="sender@example.com",
            content=(
                "From: Sender <sender@example.com>\r\n"
                f"To: User <user{index:02d}@adb.com>\r\n"
                f"Subject: Mailbox {index:02d}\r\n"
                f"Message-ID: <mailbox-{index:02d}@example.com>\r\n"
                "Date: Sat, 18 Apr 2026 20:00:00 +0000\r\n"
                "MIME-Version: 1.0\r\n"
                "Content-Type: text/plain; charset=utf-8\r\n"
                "\r\n"
                "Mailbox pagination test\r\n"
            ).encode("utf-8"),
        )
    await runtime.drain_parser_queue()

    await app_client.post(
        "/admin/login",
        data={"username": "admin", "password": runtime.settings.bootstrap_admin_password},
        follow_redirects=True,
    )
    response = await app_client.get("/admin/mailboxes")

    assert response.status_code == 200
    assert "?limit=20&offset=20" in response.text
    assert "user20@adb.com" in response.text
    assert "user00@adb.com" not in response.text


@pytest.mark.asyncio
async def test_admin_messages_page_paginates_results(app_client, runtime, monkeypatch) -> None:
    _patch_sequenced_utc_now(monkeypatch)

    await runtime.create_domain("adb.com")
    for index in range(21):
        await runtime.accept_message(
            rcpt_tos=["foo@adb.com"],
            envelope_from="sender@example.com",
            content=(
                "From: Sender <sender@example.com>\r\n"
                "To: Foo <foo@adb.com>\r\n"
                f"Subject: Message {index:02d}\r\n"
                f"Message-ID: <message-{index:02d}@example.com>\r\n"
                "Date: Sat, 18 Apr 2026 20:00:00 +0000\r\n"
                "MIME-Version: 1.0\r\n"
                "Content-Type: text/plain; charset=utf-8\r\n"
                "\r\n"
                "Messages pagination test\r\n"
            ).encode("utf-8"),
        )
    await runtime.drain_parser_queue()

    await app_client.post(
        "/admin/login",
        data={"username": "admin", "password": runtime.settings.bootstrap_admin_password},
        follow_redirects=True,
    )
    response = await app_client.get("/admin/messages")

    assert response.status_code == 200
    assert "?limit=20&offset=20" in response.text
    assert "Message 20" in response.text
    assert "Message 00" not in response.text


@pytest.mark.asyncio
async def test_admin_api_keys_and_audit_pages_paginate_results(app_client, runtime) -> None:
    for index in range(21):
        await runtime.api_keys.create_key(
            name=f"paged-key-{index:02d}",
            kind="admin",
            scopes=["domains.read"],
            domain_ids=[],
            mailbox_patterns=[],
        )
        await runtime.audit.log(
            "admin",
            "fixture",
            f"fixture.action.{index:02d}",
            "system_settings",
            None,
            "success",
        )

    await app_client.post(
        "/admin/login",
        data={"username": "admin", "password": runtime.settings.bootstrap_admin_password},
        follow_redirects=True,
    )
    api_keys_response = await app_client.get("/admin/api-keys")
    audit_response = await app_client.get("/admin/audit")

    assert api_keys_response.status_code == 200
    assert "?limit=20&offset=20" in api_keys_response.text
    assert "paged-key-20" in api_keys_response.text
    assert "paged-key-00" not in api_keys_response.text

    assert audit_response.status_code == 200
    assert "?limit=20&offset=20" in audit_response.text
    assert "fixture.action.20" in audit_response.text
    assert "fixture.action.00" not in audit_response.text


@pytest.mark.asyncio
async def test_admin_live_page_limits_stream_items_and_paginates_sessions(app_client, runtime, monkeypatch) -> None:
    _patch_sequenced_utc_now(monkeypatch)

    for index in range(21):
        session_id = f"smtp_session_{index:02d}"
        await runtime.ensure_smtp_session(
            session_id,
            type("Session", (), {"peer": ("127.0.0.1", 2500 + index), "host_name": f"mx{index:02d}", "ssl": None})(),
        )
        await runtime.close_smtp_session(session_id, status="closed", close_reason="fixture")

    for index in range(25):
        await runtime.live_state.publish(
            {
                "type": "queued" if index % 2 else "rcpt_accepted",
                "ts": f"2026-04-18T20:00:{index:02d}Z",
                "session_id": f"smtp_session_{index:02d}",
                "message_id": f"msg_{index:02d}",
            }
        )

    await app_client.post(
        "/admin/login",
        data={"username": "admin", "password": runtime.settings.bootstrap_admin_password},
        follow_redirects=True,
    )
    response = await app_client.get("/admin/live")

    assert response.status_code == 200
    assert response.text.count('data-event-type="') == 20
    assert "?limit=20&offset=20" in response.text
    assert "smtp_session_20" in response.text
    assert "smtp_session_00" not in response.text
