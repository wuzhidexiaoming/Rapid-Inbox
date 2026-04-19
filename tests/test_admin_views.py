from __future__ import annotations

import re

import pytest

from app.db.connection import connect_database


@pytest.mark.asyncio
async def test_admin_login_and_dashboard_page_flow(app_client, runtime) -> None:
    response = await app_client.post(
        "/admin/login",
        data={"username": "admin", "password": runtime.settings.bootstrap_admin_password},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Rapid Inbox Admin" in response.text
    assert "Domains" in response.text
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
    assert "Live activity" in response.text
    assert "after_cursor=" in response.text


@pytest.mark.asyncio
async def test_admin_login_rejects_invalid_credentials_with_error(app_client) -> None:
    response = await app_client.post(
        "/admin/login",
        data={"username": "admin", "password": "not-the-password"},
    )

    assert response.status_code == 401
    assert "Invalid username or password." in response.text


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
    assert response.headers["location"] == "/admin/mailboxes"
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
