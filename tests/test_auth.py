from __future__ import annotations

import asyncio

import pytest

from app.config import Settings
from app.db.connection import connect_database, initialize_database
from app.runtime import RapidInboxRuntime


@pytest.mark.asyncio
async def test_runtime_bootstraps_admin_and_persists_login_session(tmp_path) -> None:
    settings = Settings(
        storage_root=tmp_path / "storage",
        database_path=tmp_path / "storage" / "app.db",
        bootstrap_admin_username="admin",
        bootstrap_admin_password="change-me-now",
        session_cookie_name="rapid_inbox_session",
    )
    runtime = RapidInboxRuntime(settings)

    await runtime.start()
    try:
        admin = await runtime.auth.authenticate_admin("admin", "change-me-now")
        session = await runtime.auth.create_session(admin_id=admin["id"], ip="127.0.0.1", user_agent="pytest")
        loaded = await runtime.auth.get_session_admin(session["token"])

        assert admin["username"] == "admin"
        assert session["token"]
        assert loaded["admin_id"] == admin["id"]
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_bootstrap_admin_creation_is_conflict_tolerant(tmp_path, monkeypatch) -> None:
    settings = Settings(
        storage_root=tmp_path / "storage",
        database_path=tmp_path / "storage" / "app.db",
        bootstrap_admin_username="admin",
        bootstrap_admin_password="change-me-now",
    )
    runtime = RapidInboxRuntime(settings)
    initialize_database(settings.database_path)

    started = 0
    release = asyncio.Event()

    async def fake_count_admins() -> int:
        nonlocal started
        started += 1
        if started == 2:
            release.set()
        await release.wait()
        return 0

    monkeypatch.setattr(runtime.auth, "count_admins", fake_count_admins)

    results = await asyncio.gather(
        runtime.auth.ensure_bootstrap_admin(),
        runtime.auth.ensure_bootstrap_admin(),
        return_exceptions=True,
    )

    assert results == [None, None]
    with connect_database(settings.database_path) as connection:
        rows = connection.execute("SELECT username FROM admins ORDER BY id ASC").fetchall()
    assert [row["username"] for row in rows] == ["admin"]


def test_verify_password_rejects_malformed_hash() -> None:
    from app.auth.passwords import verify_password

    assert verify_password("change-me-now", "not-a-valid-hash") is False


@pytest.mark.asyncio
async def test_authenticate_admin_preserves_last_login_ip_when_ip_missing(tmp_path) -> None:
    settings = Settings(
        storage_root=tmp_path / "storage",
        database_path=tmp_path / "storage" / "app.db",
        bootstrap_admin_username="admin",
        bootstrap_admin_password="change-me-now",
    )
    runtime = RapidInboxRuntime(settings)

    await runtime.start()
    try:
        await runtime.auth.authenticate_admin("admin", "change-me-now", ip="127.0.0.1")
        admin = await runtime.auth.authenticate_admin("admin", "change-me-now")

        with connect_database(settings.database_path) as connection:
            row = connection.execute(
                "SELECT last_login_ip FROM admins WHERE username = ?",
                ("admin",),
            ).fetchone()

        assert admin["last_login_ip"] == "127.0.0.1"
        assert row["last_login_ip"] == "127.0.0.1"
    finally:
        await runtime.stop()
