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


@pytest.mark.asyncio
async def test_recovery_scanner_skips_bad_manifests_and_recovers_legacy_manifest_for_inactive_domain(
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
        created_domain = await runtime.create_domain("adb.com")
        response = await runtime.accept_message(
            rcpt_tos=["foo@adb.com"],
            envelope_from="sender@example.com",
            content=sample_email_bytes,
        )
        await runtime.drain_parser_queue()
    finally:
        await runtime.stop()

    message_id = response.removeprefix("250 queued as ")
    manifest_path = next(settings.manifests_dir.rglob("*.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["recipients"][0]["address_canonical"] == "foo@adb.com"
    assert manifest["recipients"][0]["domain_id"] == created_domain["id"]
    assert manifest["recipients"][0]["root_domain_ascii"] == "adb.com"
    assert manifest["rcpt_tos"] == ["foo@adb.com"]
    manifest.pop("recipients")
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    (settings.manifests_dir / "broken.json").write_text("{not valid json", encoding="utf-8")

    with connect_database(settings.database_path) as connection:
        connection.execute("UPDATE domains SET is_active = 0 WHERE root_domain_ascii = ?", ("adb.com",))
        connection.execute("DELETE FROM message_deliveries")
        connection.execute("DELETE FROM messages")
        connection.execute("DELETE FROM mailboxes")
        connection.commit()

    repaired = RapidInboxRuntime(settings)
    await repaired.start()
    try:
        await repaired.drain_parser_queue()
    finally:
        await repaired.stop()

    with connect_database(settings.database_path) as connection:
        mailbox = connection.execute(
            """
            SELECT first_seen_at, last_seen_at, latest_message_at, message_count
            FROM mailboxes
            WHERE address_canonical = ?
            """,
            ("foo@adb.com",),
        ).fetchone()
        delivery = connection.execute(
            """
            SELECT d.message_id, d.rcpt_to, d.delivered_at
            FROM message_deliveries AS d
            WHERE d.message_id = ?
            """,
            (message_id,),
        ).fetchone()
        message = connection.execute(
            """
            SELECT parse_status, parse_error
            FROM messages
            WHERE id = ?
            """,
            (message_id,),
        ).fetchone()

    assert mailbox["message_count"] == 1
    assert mailbox["first_seen_at"] == manifest["received_at"]
    assert mailbox["last_seen_at"] == manifest["received_at"]
    assert mailbox["latest_message_at"] == manifest["received_at"]
    assert delivery["rcpt_to"] == "foo@adb.com"
    assert delivery["delivered_at"] == manifest["received_at"]
    assert message["parse_status"] == "parsed"
    assert message["parse_error"] is None


@pytest.mark.asyncio
async def test_recovery_scanner_skips_legacy_manifest_when_domain_row_missing(
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
            envelope_from="sender@example.com",
            content=sample_email_bytes,
        )
        await runtime.drain_parser_queue()
    finally:
        await runtime.stop()

    manifest_path = next(settings.manifests_dir.rglob("*.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("recipients")
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, ensure_ascii=False), encoding="utf-8")

    with connect_database(settings.database_path) as connection:
        connection.execute("DELETE FROM message_deliveries")
        connection.execute("DELETE FROM messages")
        connection.execute("DELETE FROM mailboxes")
        connection.execute("DELETE FROM domains WHERE root_domain_ascii = ?", ("adb.com",))
        connection.commit()

    repaired = RapidInboxRuntime(settings)
    await repaired.start()
    try:
        assert repaired.list_domains() == []
        with pytest.raises(LookupError):
            await repaired.get_mailbox_view("foo@adb.com")
    finally:
        await repaired.stop()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("plus_addressing_mode", "invalid-mode"),
        ("dns_status", "broken"),
    ],
)
@pytest.mark.asyncio
async def test_recovery_scanner_skips_invalid_domain_policy_values(
    tmp_path,
    sample_email_bytes: bytes,
    field: str,
    value: object,
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
            envelope_from="sender@example.com",
            content=sample_email_bytes,
        )
        await runtime.drain_parser_queue()
    finally:
        await runtime.stop()

    manifest_path = next(settings.manifests_dir.rglob("*.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["recipients"][0]["domain_policy"][field] = value
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, ensure_ascii=False), encoding="utf-8")

    with connect_database(settings.database_path) as connection:
        connection.execute("DELETE FROM message_deliveries")
        connection.execute("DELETE FROM messages")
        connection.execute("DELETE FROM mailboxes")
        connection.execute("DELETE FROM domains WHERE root_domain_ascii = ?", ("adb.com",))
        connection.commit()

    repaired = RapidInboxRuntime(settings)
    await repaired.start()
    try:
        assert repaired.list_domains() == []
        with pytest.raises(LookupError):
            await repaired.get_mailbox_view("foo@adb.com")
    finally:
        await repaired.stop()


@pytest.mark.asyncio
async def test_recovery_scanner_requeues_failed_message_on_startup(
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
        response = await runtime.accept_message(
            rcpt_tos=["foo@adb.com"],
            envelope_from="sender@example.com",
            content=sample_email_bytes,
        )
        await runtime.drain_parser_queue()
    finally:
        await runtime.stop()

    message_id = response.removeprefix("250 queued as ")
    with connect_database(settings.database_path) as connection:
        connection.execute(
            """
            UPDATE messages
            SET parse_status = 'failed',
                parse_error = ?
            WHERE id = ?
            """,
            ("forced failure", message_id),
        )
        connection.commit()

    repaired = RapidInboxRuntime(settings)
    await repaired.start()
    try:
        await repaired.drain_parser_queue()
    finally:
        await repaired.stop()

    with connect_database(settings.database_path) as connection:
        row = connection.execute(
            """
            SELECT parse_status, parse_error
            FROM messages
            WHERE id = ?
            """,
            (message_id,),
        ).fetchone()

    assert row["parse_status"] == "parsed"
    assert row["parse_error"] is None


@pytest.mark.asyncio
async def test_recovery_scanner_restores_older_legacy_manifest_after_newer_manifest_recreates_domain(
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
        await runtime.create_domain(
            "adb.com",
            accept_exact=True,
            accept_subdomains=False,
            plus_addressing_mode="strip",
            local_part_case_sensitive=True,
        )
        await runtime.accept_message(
            rcpt_tos=["Foo+Tag@adb.com"],
            envelope_from="sender@example.com",
            content=sample_email_bytes,
        )
        await runtime.accept_message(
            rcpt_tos=["Foo+Tag@adb.com"],
            envelope_from="sender@example.com",
            content=sample_email_bytes,
        )
        await runtime.drain_parser_queue()
    finally:
        await runtime.stop()

    manifest_paths = sorted(settings.manifests_dir.rglob("*.json"))
    assert len(manifest_paths) == 2

    legacy_manifest_path = manifest_paths[0]
    legacy_manifest = json.loads(legacy_manifest_path.read_text(encoding="utf-8"))
    legacy_manifest.pop("recipients")
    legacy_manifest["received_at"] = "2026-04-17T20:00:00Z"
    legacy_target_path = settings.manifests_dir / "2026" / "04" / "17" / legacy_manifest_path.name
    legacy_target_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_manifest_path.rename(legacy_target_path)
    legacy_target_path.write_text(json.dumps(legacy_manifest, sort_keys=True, ensure_ascii=False), encoding="utf-8")

    with connect_database(settings.database_path) as connection:
        connection.execute("DELETE FROM message_deliveries")
        connection.execute("DELETE FROM messages")
        connection.execute("DELETE FROM mailboxes")
        connection.execute("DELETE FROM domains WHERE root_domain_ascii = ?", ("adb.com",))
        connection.commit()

    repaired = RapidInboxRuntime(settings)
    await repaired.start()
    try:
        await repaired.drain_parser_queue()
        mailbox = await repaired.get_mailbox_view("Foo+Tag@adb.com")

        assert mailbox["message_count"] == 2
        assert len(mailbox["items"]) == 2
    finally:
        await repaired.stop()


@pytest.mark.asyncio
async def test_recovery_scanner_recreates_deleted_domain_from_manifest_metadata(
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
        await runtime.create_domain(
            "adb.com",
            accept_exact=True,
            accept_subdomains=False,
            public_web_enabled=False,
            public_api_enabled=False,
            plus_addressing_mode="strip",
            local_part_case_sensitive=True,
        )
        with connect_database(settings.database_path) as connection:
            connection.execute("UPDATE domains SET is_hidden = 1 WHERE root_domain_ascii = ?", ("adb.com",))
            connection.commit()
        response = await runtime.accept_message(
            rcpt_tos=["Foo+Tag@adb.com"],
            envelope_from="sender@example.com",
            content=sample_email_bytes,
        )
        await runtime.drain_parser_queue()
    finally:
        await runtime.stop()

    message_id = response.removeprefix("250 queued as ")
    manifest_path = next(settings.manifests_dir.rglob("*.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    policy = manifest["recipients"][0]["domain_policy"]
    assert policy["accept_exact"] is True
    assert policy["accept_subdomains"] is False
    assert policy["plus_addressing_mode"] == "strip"
    assert policy["local_part_case_sensitive"] is True
    assert policy["public_web_enabled"] is False
    assert policy["public_api_enabled"] is False
    assert policy["is_hidden"] is True
    with connect_database(settings.database_path) as connection:
        connection.execute("DELETE FROM message_deliveries")
        connection.execute("DELETE FROM messages")
        connection.execute("DELETE FROM mailboxes")
        connection.execute("DELETE FROM domains WHERE root_domain_ascii = ?", ("adb.com",))
        connection.commit()

    repaired = RapidInboxRuntime(settings)
    await repaired.start()
    try:
        mailbox = await repaired.get_mailbox_view("Foo+Tag@adb.com")
        await repaired.drain_parser_queue()
        mailbox_after_parse = await repaired.get_mailbox_view("Foo+Tag@adb.com")

        assert mailbox["message_count"] == 1
        assert mailbox["items"][0]["message_id"] == message_id
        assert mailbox_after_parse["items"][0]["parse_status"] == "parsed"
    finally:
        await repaired.stop()

    with connect_database(settings.database_path) as connection:
        domain = connection.execute(
            """
            SELECT id, root_domain_ascii, is_active
                 , plus_addressing_mode, local_part_case_sensitive, public_web_enabled, public_api_enabled, is_hidden
            FROM domains
            WHERE root_domain_ascii = ?
            """,
            ("adb.com",),
        ).fetchone()
        delivery = connection.execute(
            """
            SELECT d.message_id, d.rcpt_to
            FROM message_deliveries AS d
            WHERE d.message_id = ?
            """,
            (message_id,),
        ).fetchone()

    assert domain["root_domain_ascii"] == "adb.com"
    assert domain["is_active"] == 1
    assert domain["plus_addressing_mode"] == "strip"
    assert domain["local_part_case_sensitive"] == 1
    assert domain["public_web_enabled"] == 0
    assert domain["public_api_enabled"] == 0
    assert domain["is_hidden"] == 1
    assert delivery["rcpt_to"] == "Foo+Tag@adb.com"


@pytest.mark.asyncio
async def test_recovery_scanner_uses_latest_policy_snapshot_for_deleted_domain(
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
        await runtime.create_domain(
            "adb.com",
            accept_exact=True,
            accept_subdomains=False,
            plus_addressing_mode="keep",
            local_part_case_sensitive=False,
        )
        await runtime.accept_message(
            rcpt_tos=["Foo+Tag@adb.com"],
            envelope_from="sender@example.com",
            content=sample_email_bytes,
        )

        with connect_database(settings.database_path) as connection:
            connection.execute(
                """
                UPDATE domains
                SET plus_addressing_mode = 'strip',
                    local_part_case_sensitive = 1
                WHERE root_domain_ascii = ?
                """,
                ("adb.com",),
            )
            connection.commit()
        runtime.domains.reload()

        await runtime.accept_message(
            rcpt_tos=["Foo+Tag@adb.com"],
            envelope_from="sender@example.com",
            content=sample_email_bytes,
        )
        await runtime.drain_parser_queue()
    finally:
        await runtime.stop()

    manifest_paths = sorted(settings.manifests_dir.rglob("*.json"))
    assert len(manifest_paths) == 2

    old_manifest_path = None
    new_manifest_path = None
    for manifest_path in manifest_paths:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        policy = manifest["recipients"][0]["domain_policy"]
        if policy["plus_addressing_mode"] == "keep":
            old_manifest_path = manifest_path
            old_manifest = manifest
        else:
            new_manifest_path = manifest_path
            new_manifest = manifest

    assert old_manifest_path is not None
    assert new_manifest_path is not None

    old_manifest["received_at"] = "2026-04-17T20:00:00Z"
    new_manifest["received_at"] = "2026-04-18T20:00:00Z"
    old_target_path = settings.manifests_dir / "2026" / "04" / "17" / old_manifest_path.name
    new_target_path = settings.manifests_dir / "2026" / "04" / "18" / new_manifest_path.name
    old_target_path.parent.mkdir(parents=True, exist_ok=True)
    new_target_path.parent.mkdir(parents=True, exist_ok=True)
    old_manifest_path.rename(old_target_path)
    if new_manifest_path != new_target_path:
        new_manifest_path.rename(new_target_path)
    old_target_path.write_text(json.dumps(old_manifest, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    new_target_path.write_text(json.dumps(new_manifest, sort_keys=True, ensure_ascii=False), encoding="utf-8")

    with connect_database(settings.database_path) as connection:
        connection.execute("DELETE FROM message_deliveries")
        connection.execute("DELETE FROM messages")
        connection.execute("DELETE FROM mailboxes")
        connection.execute("DELETE FROM domains WHERE root_domain_ascii = ?", ("adb.com",))
        connection.commit()

    repaired = RapidInboxRuntime(settings)
    await repaired.start()
    try:
        with connect_database(settings.database_path) as connection:
            domain = connection.execute(
                """
                SELECT plus_addressing_mode, local_part_case_sensitive
                FROM domains
                WHERE root_domain_ascii = ?
                """,
                ("adb.com",),
            ).fetchone()
            message_count = connection.execute("SELECT COUNT(*) AS count FROM messages").fetchone()
            delivery_count = connection.execute("SELECT COUNT(*) AS count FROM message_deliveries").fetchone()

        assert domain["plus_addressing_mode"] == "strip"
        assert domain["local_part_case_sensitive"] == 1
        assert message_count["count"] == 2
        assert delivery_count["count"] == 2
    finally:
        await repaired.stop()
