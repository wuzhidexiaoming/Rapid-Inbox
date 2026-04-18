from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_public_message_routes_serve_raw_attachment_and_html_frame(app_client, seeded_message) -> None:
    raw_response = await app_client.get(f"/mail/foo@adb.com/{seeded_message.delivery_id}/raw")
    html_response = await app_client.get(f"/mail/foo@adb.com/{seeded_message.delivery_id}/html")
    api_response = await app_client.get(
        f"/api/v1/public/mailboxes/foo@adb.com/messages/{seeded_message.delivery_id}/raw",
        headers={"X-API-Key": seeded_message.public_api_key},
    )

    assert raw_response.status_code == 200
    assert raw_response.headers["content-type"] == "message/rfc822"
    assert "sandbox" in html_response.text
    assert api_response.status_code == 200


@pytest.mark.asyncio
async def test_public_message_routes_serve_attachments(app_client, runtime) -> None:
    attachment_email_bytes = (
        b"From: Sender <sender@example.com>\r\n"
        b"To: Foo <foo@adb.com>\r\n"
        b"Subject: Attachment Test\r\n"
        b"Message-ID: <attachment@example.com>\r\n"
        b"Date: Sat, 18 Apr 2026 20:00:00 +0000\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: multipart/mixed; boundary=boundary99\r\n"
        b"\r\n"
        b"--boundary99\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"Body text.\r\n"
        b"\r\n"
        b"--boundary99\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b'Content-Disposition: attachment; filename="report.txt"\r\n'
        b"\r\n"
        b"attachment contents\r\n"
        b"\r\n"
        b"--boundary99--\r\n"
    )

    await runtime.create_domain("adb.com")
    public_key = await runtime.api_keys.create_key(
        name="attachment-public",
        kind="public",
        scopes=["public.read"],
        domain_ids=[],
        mailbox_patterns=["foo@adb.com"],
    )
    await runtime.accept_message(
        rcpt_tos=["foo@adb.com"],
        envelope_from="sender@example.com",
        content=attachment_email_bytes,
    )
    await runtime.drain_parser_queue()

    mailbox = await runtime.get_mailbox_view("foo@adb.com")
    delivery_id = mailbox["items"][0]["delivery_id"]
    detail = await runtime.get_delivery_detail("foo@adb.com", delivery_id)
    attachment_id = detail["attachments"][0]["id"]

    raw_response = await app_client.get(f"/mail/foo@adb.com/{delivery_id}/attachments/{attachment_id}")
    api_response = await app_client.get(
        f"/api/v1/public/mailboxes/foo@adb.com/messages/{delivery_id}/attachments/{attachment_id}",
        headers={"X-API-Key": public_key["plain_text"]},
    )

    assert raw_response.status_code == 200
    assert raw_response.content.startswith(b"attachment contents")
    assert api_response.status_code == 200
    assert api_response.content.startswith(b"attachment contents")
