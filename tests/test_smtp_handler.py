from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import default_settings
from app.runtime import RapidInboxRuntime
from app.smtp.handler import RapidInboxHandler


@pytest.mark.asyncio
async def test_smtp_handler_accepts_allowed_domain_and_rejects_unknown(tmp_path, sample_email_bytes: bytes) -> None:
    settings = default_settings(tmp_path)
    runtime = RapidInboxRuntime(settings)
    handler = RapidInboxHandler(runtime)

    await runtime.start()
    try:
        await runtime.create_domain("adb.com")

        session = SimpleNamespace(peer=("127.0.0.1", 2525), host_name="mx1.test", ssl=None)
        envelope = SimpleNamespace(rcpt_tos=[], mail_from="sender@example.com", content=sample_email_bytes)

        allowed = await handler.handle_RCPT(None, session, envelope, "foo@adb.com", [])
        rejected = await handler.handle_RCPT(None, session, envelope, "foo@example.com", [])
        queued = await handler.handle_DATA(None, session, envelope)
        await runtime.drain_parser_queue()
        mailbox = await runtime.get_mailbox_view("foo@adb.com")

        assert allowed == "250 OK"
        assert rejected.startswith("550")
        assert queued.startswith("250 queued as ")
        assert mailbox["items"][0]["parse_status"] == "parsed"
    finally:
        await runtime.stop()
