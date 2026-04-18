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
    assert "Content-Security-Policy" in html_response.text
    assert "about:srcdoc" in html_response.text
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
    assert raw_response.headers["content-disposition"].startswith("attachment;")
    assert raw_response.headers["x-content-type-options"] == "nosniff"
    assert api_response.status_code == 200
    assert api_response.content.startswith(b"attachment contents")
    assert api_response.headers["content-disposition"].startswith("attachment;")
    assert api_response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
async def test_public_attachment_routes_allow_inline_only_for_safe_raster_images(app_client, runtime) -> None:
    attachment_email_bytes = (
        b"From: Sender <sender@example.com>\r\n"
        b"To: Foo <foo@adb.com>\r\n"
        b"Subject: Inline Allowlist Test\r\n"
        b"Message-ID: <inline-allowlist@example.com>\r\n"
        b"Date: Sat, 18 Apr 2026 20:00:00 +0000\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: multipart/related; boundary=boundaryallow\r\n"
        b"\r\n"
        b"--boundaryallow\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"Body text.\r\n"
        b"\r\n"
        b"--boundaryallow\r\n"
        b"Content-Type: image/png\r\n"
        b"Content-Disposition: inline; filename=\"hero.png\"\r\n"
        b"Content-ID: <hero-png>\r\n"
        b"\r\n"
        b"png-bytes\r\n"
        b"\r\n"
        b"--boundaryallow\r\n"
        b"Content-Type: image/svg+xml\r\n"
        b"Content-Disposition: inline; filename=\"evil.svg\"\r\n"
        b"Content-ID: <hero-svg>\r\n"
        b"\r\n"
        b"<svg xmlns=\"http://www.w3.org/2000/svg\"></svg>\r\n"
        b"\r\n"
        b"--boundaryallow--\r\n"
    )

    await runtime.create_domain("adb.com")
    public_key = await runtime.api_keys.create_key(
        name="inline-allowlist-public",
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
    png_attachment_id = next(attachment["id"] for attachment in detail["attachments"] if attachment["content_type"] == "image/png")
    svg_attachment_id = next(attachment["id"] for attachment in detail["attachments"] if attachment["content_type"] == "image/svg+xml")

    png_response = await app_client.get(f"/mail/foo@adb.com/{delivery_id}/attachments/{png_attachment_id}")
    svg_response = await app_client.get(
        f"/api/v1/public/mailboxes/foo@adb.com/messages/{delivery_id}/attachments/{svg_attachment_id}",
        headers={"X-API-Key": public_key["plain_text"]},
    )

    assert png_response.status_code == 200
    assert png_response.headers["content-disposition"].startswith("inline;")
    assert png_response.headers["x-content-type-options"] == "nosniff"
    assert svg_response.status_code == 200
    assert svg_response.headers["content-disposition"].startswith("attachment;")
    assert svg_response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
async def test_public_web_routes_respect_public_web_enabled_flag(app_client, runtime, sample_email_bytes) -> None:
    await runtime.create_domain("web-disabled.adb.com", public_web_enabled=False, public_api_enabled=True)
    await runtime.accept_message(
        rcpt_tos=["foo@web-disabled.adb.com"],
        envelope_from="sender@example.com",
        content=sample_email_bytes,
    )
    await runtime.drain_parser_queue()

    mailbox = await runtime.get_mailbox_view("foo@web-disabled.adb.com")
    delivery_id = mailbox["items"][0]["delivery_id"]
    public_key = await runtime.api_keys.create_key(
        name="web-disabled-public",
        kind="public",
        scopes=["public.read"],
        domain_ids=[],
        mailbox_patterns=["foo@web-disabled.adb.com"],
    )

    web_response = await app_client.get(f"/mail/foo@web-disabled.adb.com/{delivery_id}/raw")
    api_response = await app_client.get(
        f"/api/v1/public/mailboxes/foo@web-disabled.adb.com/messages/{delivery_id}/raw",
        headers={"X-API-Key": public_key["plain_text"]},
    )

    assert web_response.status_code == 404
    assert api_response.status_code == 200


@pytest.mark.asyncio
async def test_public_api_routes_respect_public_api_enabled_flag(app_client, runtime, sample_email_bytes) -> None:
    await runtime.create_domain("api-disabled.adb.com", public_web_enabled=True, public_api_enabled=False)
    await runtime.accept_message(
        rcpt_tos=["foo@api-disabled.adb.com"],
        envelope_from="sender@example.com",
        content=sample_email_bytes,
    )
    await runtime.drain_parser_queue()

    mailbox = await runtime.get_mailbox_view("foo@api-disabled.adb.com")
    delivery_id = mailbox["items"][0]["delivery_id"]
    public_key = await runtime.api_keys.create_key(
        name="api-disabled-public",
        kind="public",
        scopes=["public.read"],
        domain_ids=[],
        mailbox_patterns=["foo@api-disabled.adb.com"],
    )

    web_response = await app_client.get(f"/mail/foo@api-disabled.adb.com/{delivery_id}/raw")
    api_response = await app_client.get(
        f"/api/v1/public/mailboxes/foo@api-disabled.adb.com/messages/{delivery_id}/raw",
        headers={"X-API-Key": public_key["plain_text"]},
    )

    assert web_response.status_code == 200
    assert api_response.status_code == 404


@pytest.mark.asyncio
async def test_public_html_frame_rewrites_cid_references_to_attachment_routes(app_client, runtime) -> None:
    cid_email_bytes = (
        b"From: Sender <sender@example.com>\r\n"
        b"To: Foo <foo@adb.com>\r\n"
        b"Subject: CID Rewrite\r\n"
        b"Message-ID: <cid@example.com>\r\n"
        b"Date: Sat, 18 Apr 2026 20:00:00 +0000\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: multipart/related; boundary=boundarycid\r\n"
        b"\r\n"
        b"--boundarycid\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n"
        b"\r\n"
        b"<html><body><img src=\"cid:hero-image\" alt=\"Hero\"></body></html>\r\n"
        b"\r\n"
        b"--boundarycid\r\n"
        b"Content-Type: image/png\r\n"
        b"Content-Disposition: inline; filename=\"hero.png\"\r\n"
        b"Content-ID: <hero-image>\r\n"
        b"\r\n"
        b"png-bytes\r\n"
        b"\r\n"
        b"--boundarycid--\r\n"
    )

    await runtime.create_domain("cid.adb.com")
    await runtime.accept_message(
        rcpt_tos=["foo@cid.adb.com"],
        envelope_from="sender@example.com",
        content=cid_email_bytes,
    )
    await runtime.drain_parser_queue()

    mailbox = await runtime.get_mailbox_view("foo@cid.adb.com")
    delivery_id = mailbox["items"][0]["delivery_id"]
    detail = await runtime.get_delivery_detail("foo@cid.adb.com", delivery_id)
    attachment_id = detail["attachments"][0]["id"]

    html_response = await app_client.get(f"/mail/foo@cid.adb.com/{delivery_id}/html")

    assert html_response.status_code == 200
    assert f"/mail/foo@cid.adb.com/{delivery_id}/attachments/{attachment_id}" in html_response.text
    assert "cid:hero-image" not in html_response.text
    assert "Content-Security-Policy" in html_response.text
    assert "about:srcdoc" in html_response.text
