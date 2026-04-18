from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_public_key_requires_scope_and_domain_grant(app_client, runtime) -> None:
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

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_seeded_message_public_key_can_read_seeded_mailbox(app_client, seeded_message) -> None:
    response = await app_client.get(
        "/api/v1/public/mailboxes/foo@adb.com/messages",
        headers={"X-API-Key": seeded_message.public_api_key},
    )

    assert response.status_code == 200
    assert response.json()["items"][0]["delivery_id"] == seeded_message.delivery_id
