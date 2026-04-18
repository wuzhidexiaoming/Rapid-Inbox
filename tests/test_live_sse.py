from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_live_sse_stream_emits_recent_rcpt_event(admin_client, runtime, sample_email_bytes: bytes) -> None:
    await runtime.create_domain("adb.com")
    await runtime.accept_message(
        rcpt_tos=["foo@adb.com"],
        envelope_from="sender@example.com",
        content=sample_email_bytes,
        smtp_session_id="smtp_live_1",
    )

    response = await admin_client.get("/api/v1/admin/live/smtp/stream")

    assert response.status_code == 200
    assert "rcpt_accepted" in response.text or "queued" in response.text
