from __future__ import annotations

from dataclasses import dataclass
from email.message import EmailMessage
from email.policy import SMTP
from types import SimpleNamespace

import pytest

from app.config import default_settings
from app.runtime import RapidInboxRuntime
from app.smtp.handler import RapidInboxHandler


@dataclass(frozen=True, slots=True)
class ExpectedAttachment:
    filename: str
    content_type: str
    is_inline: bool = False


@dataclass(frozen=True, slots=True)
class DeliveryCase:
    subject: str
    raw_message: bytes
    expected_text_snippet: str | None
    expected_html_snippet: str | None
    expected_attachments: tuple[ExpectedAttachment, ...]


def _base_message(subject: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = "QA Sender <sender@example.com>"
    message["To"] = "Foo Inbox <foo@adb.com>"
    message["Subject"] = subject
    message["Message-ID"] = f"<{subject.lower().replace(' ', '-')}@example.com>"
    message["Date"] = "Sat, 18 Apr 2026 20:00:00 +0000"
    return message


def _text_only_case() -> DeliveryCase:
    subject = "Plain Text Only"
    message = _base_message(subject)
    message.set_content("This is a plain text SMTP delivery.\nSecond line for preview coverage.\n")
    return DeliveryCase(
        subject=subject,
        raw_message=message.as_bytes(policy=SMTP),
        expected_text_snippet="This is a plain text SMTP delivery.",
        expected_html_snippet=None,
        expected_attachments=(),
    )


def _html_only_case() -> DeliveryCase:
    subject = "HTML Only"
    message = _base_message(subject)
    message.set_content("<html><body><h1>HTML only body</h1><p>Rendered by the parser.</p></body></html>", subtype="html")
    return DeliveryCase(
        subject=subject,
        raw_message=message.as_bytes(policy=SMTP),
        expected_text_snippet=None,
        expected_html_snippet="<h1>HTML only body</h1>",
        expected_attachments=(),
    )


def _alternative_case() -> DeliveryCase:
    subject = "Plain And HTML"
    message = _base_message(subject)
    message.set_content("Fallback plain text body for multipart/alternative delivery.\n")
    message.add_alternative(
        "<html><body><p>Preferred <strong>HTML</strong> body for multipart/alternative delivery.</p></body></html>",
        subtype="html",
    )
    return DeliveryCase(
        subject=subject,
        raw_message=message.as_bytes(policy=SMTP),
        expected_text_snippet="Fallback plain text body for multipart/alternative delivery.",
        expected_html_snippet="Preferred <strong>HTML</strong> body",
        expected_attachments=(),
    )


def _text_with_attachment_case() -> DeliveryCase:
    subject = "Plain Text With Attachment"
    message = _base_message(subject)
    message.set_content("Plain text body with a regular attachment.\n")
    message.add_attachment(
        "Quarterly report attachment body.\n",
        filename="report.txt",
    )
    return DeliveryCase(
        subject=subject,
        raw_message=message.as_bytes(policy=SMTP),
        expected_text_snippet="Plain text body with a regular attachment.",
        expected_html_snippet=None,
        expected_attachments=(ExpectedAttachment(filename="report.txt", content_type="text/plain"),),
    )


def _html_with_attachment_case() -> DeliveryCase:
    subject = "HTML With Attachment"
    message = _base_message(subject)
    message.set_content(
        "<html><body><p>HTML body with an attached PDF-like file.</p></body></html>",
        subtype="html",
    )
    message.add_attachment(
        b"%PDF-1.4\nfake pdf bytes\n",
        maintype="application",
        subtype="pdf",
        filename="invoice.pdf",
    )
    return DeliveryCase(
        subject=subject,
        raw_message=message.as_bytes(policy=SMTP),
        expected_text_snippet=None,
        expected_html_snippet="HTML body with an attached PDF-like file.",
        expected_attachments=(ExpectedAttachment(filename="invoice.pdf", content_type="application/pdf"),),
    )


def _alternative_with_attachment_case() -> DeliveryCase:
    subject = "Plain HTML And Attachment"
    message = _base_message(subject)
    message.set_content("Plain text fallback for mixed delivery with attachment.\n")
    message.add_alternative(
        "<html><body><p>HTML body for mixed delivery with attachment.</p></body></html>",
        subtype="html",
    )
    message.add_attachment(
        b"PK\x03\x04fake zip bytes",
        maintype="application",
        subtype="zip",
        filename="bundle.zip",
    )
    return DeliveryCase(
        subject=subject,
        raw_message=message.as_bytes(policy=SMTP),
        expected_text_snippet="Plain text fallback for mixed delivery with attachment.",
        expected_html_snippet="HTML body for mixed delivery with attachment.",
        expected_attachments=(ExpectedAttachment(filename="bundle.zip", content_type="application/zip"),),
    )


def _html_with_inline_related_case() -> DeliveryCase:
    subject = "HTML With Inline Related"
    message = _base_message(subject)
    message.set_content("Plain text fallback for CID image delivery.\n")
    message.add_alternative(
        '<html><body><p>HTML body with inline image.</p><img src="cid:hero-image"></body></html>',
        subtype="html",
    )
    html_part = message.get_payload()[-1]
    html_part.add_related(
        b"\x89PNG\r\nfake-inline-image",
        maintype="image",
        subtype="png",
        cid="<hero-image>",
        filename="hero.png",
        disposition="inline",
    )
    return DeliveryCase(
        subject=subject,
        raw_message=message.as_bytes(policy=SMTP),
        expected_text_snippet="Plain text fallback for CID image delivery.",
        expected_html_snippet="HTML body with inline image.",
        expected_attachments=(ExpectedAttachment(filename="hero.png", content_type="image/png", is_inline=True),),
    )


DELIVERY_CASES = [
    pytest.param(_text_only_case(), id="text-only"),
    pytest.param(_html_only_case(), id="html-only"),
    pytest.param(_alternative_case(), id="multipart-alternative"),
    pytest.param(_text_with_attachment_case(), id="text-with-attachment"),
    pytest.param(_html_with_attachment_case(), id="html-with-attachment"),
    pytest.param(_alternative_with_attachment_case(), id="alternative-with-attachment"),
    pytest.param(_html_with_inline_related_case(), id="html-with-inline-related"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", DELIVERY_CASES)
async def test_smtp_delivery_parses_realistic_email_formats(tmp_path, case: DeliveryCase) -> None:
    settings = default_settings(tmp_path)
    runtime = RapidInboxRuntime(settings)
    handler = RapidInboxHandler(runtime)

    await runtime.start()
    try:
        await runtime.create_domain("adb.com")

        session = SimpleNamespace(peer=("127.0.0.1", 2525), host_name="mx1.test", ssl=None)
        envelope = SimpleNamespace(rcpt_tos=[], mail_from="sender@example.com", content=case.raw_message)

        rcpt_response = await handler.handle_RCPT(None, session, envelope, "foo@adb.com", [])
        data_response = await handler.handle_DATA(None, session, envelope)
        await runtime.drain_parser_queue()

        mailbox = await runtime.get_mailbox_view("foo@adb.com")
        delivery_id = mailbox["items"][0]["delivery_id"]
        detail = await runtime.get_delivery_detail("foo@adb.com", delivery_id)
        raw_message = await runtime.get_raw_message(delivery_id)

        assert rcpt_response == "250 OK"
        assert data_response.startswith("250 queued as ")
        assert mailbox["message_count"] == 1
        assert mailbox["items"][0]["parse_status"] == "parsed"
        assert detail["subject"] == case.subject
        assert detail["from_addr"] == "sender@example.com"
        assert raw_message == case.raw_message

        if case.expected_text_snippet is None:
            assert detail["text_body"] == ""
        else:
            assert case.expected_text_snippet in detail["text_body"]

        if case.expected_html_snippet is None:
            assert detail["html_body"] == ""
        else:
            assert case.expected_html_snippet in detail["html_body"]

        actual_attachments = [
            (attachment["filename"], attachment["content_type"], bool(attachment["is_inline"]))
            for attachment in detail["attachments"]
        ]
        expected_attachments = [
            (attachment.filename, attachment.content_type, attachment.is_inline)
            for attachment in case.expected_attachments
        ]
        assert actual_attachments == expected_attachments
    finally:
        await runtime.stop()
