from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.db.connection import connect_database


@pytest.mark.asyncio
async def test_public_key_without_domain_grants_can_read_current_domains(app_client, runtime) -> None:
    await runtime.create_domain("adb.com")
    key = await runtime.api_keys.create_key(
        name="public-read",
        kind="public",
        scopes=["public.read"],
        domain_ids=[],
        mailbox_patterns=[],
    )

    response = await app_client.get(
        "/api/v1/public/mailboxes/foo@adb.com/messages",
        headers={"X-API-Key": key["plain_text"]},
    )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_public_key_without_domain_grants_can_read_later_domains(app_client, runtime) -> None:
    key = await runtime.api_keys.create_key(
        name="public-read-all-future",
        kind="public",
        scopes=["public.read"],
        domain_ids=[],
        mailbox_patterns=[],
    )
    await runtime.create_domain("later.adb.com")

    response = await app_client.get(
        "/api/v1/public/mailboxes/foo@later.adb.com/messages",
        headers={"X-API-Key": key["plain_text"]},
    )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_seeded_message_public_key_can_read_seeded_mailbox(app_client, seeded_message) -> None:
    response = await app_client.get(
        "/api/v1/public/mailboxes/foo@adb.com/messages",
        headers={"X-API-Key": seeded_message.public_api_key},
    )

    assert response.status_code == 200
    assert response.json()["items"][0]["delivery_id"] == seeded_message.delivery_id


@pytest.mark.asyncio
async def test_mailbox_only_public_key_can_read_mailbox(app_client, runtime, sample_email_bytes) -> None:
    await runtime.create_domain("adb.com")
    await runtime.ensure_smtp_session(
        "smtp_mailbox_only",
        SimpleNamespace(peer=("127.0.0.1", 2525), host_name="pytest", ssl=None),
    )
    key = await runtime.api_keys.create_key(
        name="mailbox-only",
        kind="public",
        scopes=["public.read"],
        domain_ids=[],
        mailbox_patterns=["foo@adb.com"],
    )
    await runtime.accept_message(
        rcpt_tos=["foo@adb.com"],
        envelope_from="sender@example.com",
        content=sample_email_bytes,
        smtp_session_id="smtp_mailbox_only",
    )
    await runtime.drain_parser_queue()
    mailbox = await runtime.get_mailbox_view("foo@adb.com")

    response = await app_client.get(
        "/api/v1/public/mailboxes/foo@adb.com/messages",
        headers={"X-API-Key": key["plain_text"]},
    )

    assert response.status_code == 200
    assert response.json()["items"][0]["delivery_id"] == mailbox["items"][0]["delivery_id"]


@pytest.mark.asyncio
async def test_mailbox_only_public_key_uses_canonical_mailbox_address(app_client, runtime, sample_email_bytes) -> None:
    await runtime.create_domain("adb.com")
    await runtime.ensure_smtp_session(
        "smtp_canonical_mailbox",
        SimpleNamespace(peer=("127.0.0.1", 2525), host_name="pytest", ssl=None),
    )
    key = await runtime.api_keys.create_key(
        name="canonical-mailbox",
        kind="public",
        scopes=["public.read"],
        domain_ids=[],
        mailbox_patterns=["foo@adb.com"],
    )
    await runtime.accept_message(
        rcpt_tos=["foo@adb.com"],
        envelope_from="sender@example.com",
        content=sample_email_bytes,
        smtp_session_id="smtp_canonical_mailbox",
    )
    await runtime.drain_parser_queue()
    mailbox = await runtime.get_mailbox_view("foo@adb.com")

    response = await app_client.get(
        "/api/v1/public/mailboxes/FOO@ADB.COM/messages",
        headers={"X-API-Key": key["plain_text"]},
    )

    assert response.status_code == 200
    assert response.json()["items"][0]["delivery_id"] == mailbox["items"][0]["delivery_id"]


@pytest.mark.asyncio
async def test_public_key_context_is_cleared_after_request(app_client, runtime, sample_email_bytes) -> None:
    await runtime.create_domain("adb.com")
    await runtime.ensure_smtp_session(
        "smtp_context_cleanup",
        SimpleNamespace(peer=("127.0.0.1", 2525), host_name="pytest", ssl=None),
    )
    key = await runtime.api_keys.create_key(
        name="context-cleanup",
        kind="public",
        scopes=["public.read"],
        domain_ids=[],
        mailbox_patterns=["foo@adb.com"],
    )
    await runtime.accept_message(
        rcpt_tos=["foo@adb.com"],
        envelope_from="sender@example.com",
        content=sample_email_bytes,
        smtp_session_id="smtp_context_cleanup",
    )
    await runtime.drain_parser_queue()

    response = await app_client.get(
        "/api/v1/public/mailboxes/foo@adb.com/messages",
        headers={"X-API-Key": key["plain_text"]},
    )

    assert response.status_code == 200

    mailbox = await runtime.get_mailbox_view("bar@adb.com")
    assert mailbox["mailbox"] == "bar@adb.com"
    assert mailbox["message_count"] == 0


@pytest.mark.asyncio
async def test_public_key_records_request_ip(app_client, runtime, sample_email_bytes) -> None:
    await runtime.create_domain("adb.com")
    await runtime.ensure_smtp_session(
        "smtp_record_ip",
        SimpleNamespace(peer=("127.0.0.1", 2525), host_name="pytest", ssl=None),
    )
    key = await runtime.api_keys.create_key(
        name="record-ip",
        kind="public",
        scopes=["public.read"],
        domain_ids=[],
        mailbox_patterns=["foo@adb.com"],
    )
    await runtime.accept_message(
        rcpt_tos=["foo@adb.com"],
        envelope_from="sender@example.com",
        content=sample_email_bytes,
        smtp_session_id="smtp_record_ip",
    )
    await runtime.drain_parser_queue()

    response = await app_client.get(
        "/api/v1/public/mailboxes/foo@adb.com/messages",
        headers={"X-API-Key": key["plain_text"]},
    )

    assert response.status_code == 200

    with connect_database(runtime.settings.database_path) as connection:
        row = connection.execute(
            "SELECT last_used_ip FROM api_keys WHERE id = ?",
            (key["id"],),
        ).fetchone()

    assert row["last_used_ip"] == "127.0.0.1"


@pytest.mark.asyncio
async def test_public_key_ip_restriction_blocks_disallowed_client_ip(app_client, runtime, sample_email_bytes) -> None:
    await runtime.create_domain("adb.com")
    await runtime.ensure_smtp_session(
        "smtp_ip_restriction",
        SimpleNamespace(peer=("127.0.0.1", 2525), host_name="pytest", ssl=None),
    )
    key = await runtime.api_keys.create_key(
        name="ip-restricted",
        kind="public",
        scopes=["public.read"],
        domain_ids=[],
        mailbox_patterns=["foo@adb.com"],
        allowed_ip_cidrs=["203.0.113.0/24"],
    )
    await runtime.accept_message(
        rcpt_tos=["foo@adb.com"],
        envelope_from="sender@example.com",
        content=sample_email_bytes,
        smtp_session_id="smtp_ip_restriction",
    )
    await runtime.drain_parser_queue()

    response = await app_client.get(
        "/api/v1/public/mailboxes/foo@adb.com/messages",
        headers={"X-API-Key": key["plain_text"]},
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_public_key_rate_limit_blocks_repeat_requests(app_client, runtime, sample_email_bytes) -> None:
    await runtime.create_domain("adb.com")
    await runtime.ensure_smtp_session(
        "smtp_rate_limit",
        SimpleNamespace(peer=("127.0.0.1", 2525), host_name="pytest", ssl=None),
    )
    key = await runtime.api_keys.create_key(
        name="rate-limited",
        kind="public",
        scopes=["public.read"],
        domain_ids=[],
        mailbox_patterns=["foo@adb.com"],
        rate_limit_per_min=1,
    )
    await runtime.accept_message(
        rcpt_tos=["foo@adb.com"],
        envelope_from="sender@example.com",
        content=sample_email_bytes,
        smtp_session_id="smtp_rate_limit",
    )
    await runtime.drain_parser_queue()

    first_response = await app_client.get(
        "/api/v1/public/mailboxes/foo@adb.com/messages",
        headers={"X-API-Key": key["plain_text"]},
    )
    second_response = await app_client.get(
        "/api/v1/public/mailboxes/foo@adb.com/messages",
        headers={"X-API-Key": key["plain_text"]},
    )

    assert first_response.status_code == 200
    assert second_response.status_code == 429


@pytest.mark.asyncio
async def test_query_key_auth_respects_allow_query(runtime) -> None:
    disabled_key = await runtime.api_keys.create_key(
        name="query-disabled",
        kind="public",
        scopes=["public.read"],
        domain_ids=[],
        mailbox_patterns=[],
    )
    enabled_key = await runtime.api_keys.create_key(
        name="query-enabled",
        kind="public",
        scopes=["public.read"],
        domain_ids=[],
        mailbox_patterns=[],
        allow_query=True,
    )

    with pytest.raises(LookupError):
        runtime.api_keys.authenticate_query(disabled_key["plain_text"])

    context = runtime.api_keys.authenticate_query(enabled_key["plain_text"])

    assert context.kind == "public"
    assert context.public_id == enabled_key["public_id"]
