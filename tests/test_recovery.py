from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.runtime import RapidInboxRuntime
from conftest import connect_database


@pytest.mark.asyncio
async def test_recovery_scanner_rebuilds_missing_message_and_delivery(tmp_path, sample_email_bytes: bytes) -> None:
    settings = Settings(
        storage_root=tmp_path / "storage",
        database_path=tmp_path / "storage" / "app.db",
    )
    runtime = RapidInboxRuntime(settings)

    await runtime.start()
    try:
        await runtime.create_domain("adb.com")
        await runtime.ensure_smtp_session(
            "smtp_recover_1",
            SimpleNamespace(peer=("127.0.0.1", 2525), host_name="localhost", ssl=None),
        )
        await runtime.accept_message(
            rcpt_tos=["foo@adb.com"],
            envelope_from="sender@example.com",
            content=sample_email_bytes,
            smtp_session_id="smtp_recover_1",
        )
        await runtime.drain_parser_queue()
    finally:
        await runtime.stop()

    with connect_database(settings.database_path) as connection:
        connection.execute("DELETE FROM message_deliveries")
        connection.execute("DELETE FROM messages")
        connection.commit()

    repaired = RapidInboxRuntime(settings)
    await repaired.start()
    try:
        mailbox = await repaired.get_mailbox_view("foo@adb.com")
        await repaired.drain_parser_queue()
        assert mailbox["message_count"] == 1
        assert mailbox["items"][0]["parse_status"] in {"pending", "parsed"}
    finally:
        await repaired.stop()


@pytest.mark.asyncio
async def test_recovery_scanner_rebuilds_mailbox_bounds_from_multiple_deliveries(
    tmp_path,
    sample_email_bytes: bytes,
) -> None:
    settings = Settings(
        storage_root=tmp_path / "storage",
        database_path=tmp_path / "storage" / "app.db",
    )
    runtime = RapidInboxRuntime(settings)

    await runtime.start()
    try:
        await runtime.create_domain("adb.com")
        await runtime.accept_message(
            rcpt_tos=["foo@adb.com"],
            envelope_from="sender1@example.com",
            content=sample_email_bytes,
        )
        await runtime.accept_message(
            rcpt_tos=["foo@adb.com"],
            envelope_from="sender2@example.com",
            content=sample_email_bytes,
        )
        await runtime.drain_parser_queue()
    finally:
        await runtime.stop()

    manifest_paths = sorted(settings.manifests_dir.rglob("*.json"))
    assert len(manifest_paths) == 2

    later_received_at = "2026-04-18T20:05:01Z"
    earlier_received_at = "2026-04-18T20:00:01Z"
    for manifest_path, received_at in zip(manifest_paths, [later_received_at, earlier_received_at], strict=True):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["received_at"] = received_at
        manifest_path.write_text(json.dumps(manifest, sort_keys=True, ensure_ascii=False), encoding="utf-8")

    with connect_database(settings.database_path) as connection:
        connection.execute("DELETE FROM message_deliveries")
        connection.execute("DELETE FROM messages")
        connection.execute("DELETE FROM mailboxes")
        connection.commit()

    repaired = RapidInboxRuntime(settings)
    await repaired.start()
    try:
        await repaired.drain_parser_queue()
        mailbox = await repaired.get_mailbox_view("foo@adb.com")
        assert mailbox["message_count"] == 2
        assert len(mailbox["items"]) == 2

        with connect_database(settings.database_path) as connection:
            row = connection.execute(
                """
                SELECT first_seen_at, last_seen_at, latest_message_at, message_count
                FROM mailboxes
                WHERE address_canonical = ?
                """,
                ("foo@adb.com",),
            ).fetchone()

        assert row["first_seen_at"] == earlier_received_at
        assert row["last_seen_at"] == later_received_at
        assert row["latest_message_at"] == later_received_at
        assert row["message_count"] == 2
    finally:
        await repaired.stop()
