from __future__ import annotations

import httpx
import pytest

from app.config import default_settings
from app.main import create_app


@pytest.mark.asyncio
async def test_mailbox_page_and_public_api_show_received_message(tmp_path, sample_email_bytes: bytes) -> None:
    settings = default_settings(tmp_path)
    app = create_app(settings=settings)

    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        await runtime.create_domain("adb.com")
        await runtime.accept_message(
            rcpt_tos=["foo@adb.com"],
            envelope_from="sender@example.com",
            content=sample_email_bytes,
        )
        await runtime.drain_parser_queue()
        mailbox = await runtime.get_mailbox_view("foo@adb.com")
        delivery_id = mailbox["items"][0]["delivery_id"]

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            page = await client.get("/mail/foo@adb.com")
            detail = await client.get(f"/mail/foo@adb.com/{delivery_id}")
            api = await client.get(
                "/api/v1/public/mailboxes/foo@adb.com/messages",
                headers={"X-API-Key": settings.public_api_key},
            )

        assert page.status_code == 200
        assert "Hello Rapid Inbox" in page.text
        assert detail.status_code == 200
        assert "sender@example.com" in detail.text
        assert api.status_code == 200
        assert api.json()["items"][0]["delivery_id"] == delivery_id
