from __future__ import annotations

import pytest

from app.config import Settings
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
