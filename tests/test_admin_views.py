from __future__ import annotations

import pytest


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
    assert "after_seq=0" in response.text


@pytest.mark.asyncio
async def test_admin_login_rejects_invalid_credentials_with_error(app_client) -> None:
    response = await app_client.post(
        "/admin/login",
        data={"username": "admin", "password": "not-the-password"},
    )

    assert response.status_code == 401
    assert "Invalid username or password." in response.text
