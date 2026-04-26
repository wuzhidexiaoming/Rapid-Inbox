from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.policy import SMTP
from functools import partial
from itertools import count

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import default_settings
import app.runtime as runtime_module
from app.main import create_app


def _mail_bytes(subject: str, message_id: str, body: str) -> bytes:
    return (
        "From: Sender <sender@example.com>\r\n"
        "To: Foo <foo@adb.com>\r\n"
        f"Subject: {subject}\r\n"
        f"Message-ID: <{message_id}>\r\n"
        "Date: Sat, 18 Apr 2026 20:00:00 +0000\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        f"{body}\r\n"
    ).encode("utf-8")


def _rich_mail_bytes(
    *,
    subject: str,
    message_id: str,
    from_addr: str,
    body: str,
    subtype: str = "plain",
) -> bytes:
    message = EmailMessage()
    message["From"] = from_addr
    message["To"] = "Foo <foo@adb.com>"
    message["Subject"] = subject
    message["Message-ID"] = f"<{message_id}>"
    message["Date"] = "Sat, 18 Apr 2026 20:00:00 +0000"
    if subtype == "html":
        message.set_content(body, subtype="html")
    else:
        message.set_content(body)
    return message.as_bytes(policy=SMTP)


def _patch_sequenced_utc_now(monkeypatch) -> None:
    base = datetime(2026, 4, 18, 20, 0, 0, tzinfo=timezone.utc)
    ticks = count()

    monkeypatch.setattr(
        runtime_module,
        "utc_now",
        lambda: (base + timedelta(seconds=next(ticks))).isoformat().replace("+00:00", "Z"),
    )


@pytest.mark.asyncio
async def test_public_home_page_exposes_mailbox_entry_point(app_client) -> None:
    response = await app_client.get("/")

    assert response.status_code == 200
    assert "重新定义" in response.text
    assert "公开邮箱" in response.text
    assert "立即进入" in response.text


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


@pytest.mark.asyncio
async def test_public_message_page_displays_shanghai_time(app_client, runtime, monkeypatch, sample_email_bytes: bytes) -> None:
    await runtime.create_domain("adb.com")
    monkeypatch.setattr(runtime_module, "utc_now", lambda: "2026-04-18T20:00:00Z")
    await runtime.accept_message(
        rcpt_tos=["foo@adb.com"],
        envelope_from="sender@example.com",
        content=sample_email_bytes,
    )
    await runtime.drain_parser_queue()
    mailbox = await runtime.get_mailbox_view("foo@adb.com")
    delivery_id = mailbox["items"][0]["delivery_id"]

    response = await app_client.get(f"/mail/foo@adb.com/{delivery_id}")

    assert response.status_code == 200
    assert "2026-04-19 04:00:00" in response.text


@pytest.mark.asyncio
async def test_public_mailbox_page_exposes_pagination_links(app_client, runtime, monkeypatch) -> None:
    _patch_sequenced_utc_now(monkeypatch)

    await runtime.create_domain("adb.com")
    await runtime.accept_message(
        rcpt_tos=["foo@adb.com"],
        envelope_from="sender@example.com",
        content=_mail_bytes("Oldest", "oldest@example.com", "oldest"),
    )
    await runtime.accept_message(
        rcpt_tos=["foo@adb.com"],
        envelope_from="sender@example.com",
        content=_mail_bytes("Middle", "middle@example.com", "middle"),
    )
    await runtime.accept_message(
        rcpt_tos=["foo@adb.com"],
        envelope_from="sender@example.com",
        content=_mail_bytes("Newest", "newest@example.com", "newest"),
    )
    await runtime.drain_parser_queue()

    first_page = await app_client.get("/mail/foo@adb.com?limit=1&offset=0")
    second_page = await app_client.get("/mail/foo@adb.com?limit=1&offset=1")

    assert first_page.status_code == 200
    assert "Newest" in first_page.text
    assert "?limit=1&offset=1" in first_page.text
    assert "?limit=1&offset=2" in first_page.text
    assert 'aria-label="第 3 页"' in first_page.text
    assert second_page.status_code == 200
    assert "Middle" in second_page.text
    assert "?limit=1&offset=0" in second_page.text


@pytest.mark.asyncio
async def test_public_mailbox_page_defaults_to_twenty_results(app_client, runtime, monkeypatch) -> None:
    _patch_sequenced_utc_now(monkeypatch)

    await runtime.create_domain("adb.com")
    for index in range(21):
        await runtime.accept_message(
            rcpt_tos=["foo@adb.com"],
            envelope_from="sender@example.com",
            content=_mail_bytes(f"Subject {index:02d}", f"default-{index:02d}@example.com", f"body-{index:02d}"),
        )
    await runtime.drain_parser_queue()

    response = await app_client.get("/mail/foo@adb.com")

    assert response.status_code == 200
    assert "?limit=20&offset=20" in response.text
    assert "Subject 20" in response.text
    assert "Subject 00" not in response.text


@pytest.mark.asyncio
async def test_public_mailbox_api_returns_pagination_metadata(app_client, runtime, monkeypatch) -> None:
    _patch_sequenced_utc_now(monkeypatch)

    await runtime.create_domain("adb.com")
    await runtime.accept_message(
        rcpt_tos=["foo@adb.com"],
        envelope_from="sender@example.com",
        content=_mail_bytes("Oldest", "oldest-api@example.com", "oldest"),
    )
    await runtime.accept_message(
        rcpt_tos=["foo@adb.com"],
        envelope_from="sender@example.com",
        content=_mail_bytes("Middle", "middle-api@example.com", "middle"),
    )
    await runtime.accept_message(
        rcpt_tos=["foo@adb.com"],
        envelope_from="sender@example.com",
        content=_mail_bytes("Newest", "newest-api@example.com", "newest"),
    )
    await runtime.drain_parser_queue()

    first_page = await app_client.get(
        "/api/v1/public/mailboxes/foo@adb.com/messages?limit=1&offset=0",
        headers={"X-API-Key": str(runtime.settings.public_api_key)},
    )
    second_page = await app_client.get(
        "/api/v1/public/mailboxes/foo@adb.com/messages?limit=1&offset=1",
        headers={"X-API-Key": str(runtime.settings.public_api_key)},
    )

    assert first_page.status_code == 200
    assert first_page.json()["limit"] == 1
    assert first_page.json()["offset"] == 0
    assert first_page.json()["next_offset"] == 1
    assert first_page.json()["previous_offset"] is None
    assert first_page.json()["has_next"] is True
    assert first_page.json()["has_previous"] is False
    assert first_page.json()["items"][0]["subject"] == "Newest"
    assert second_page.status_code == 200
    assert second_page.json()["offset"] == 1
    assert second_page.json()["previous_offset"] == 0
    assert second_page.json()["has_previous"] is True
    assert second_page.json()["items"][0]["subject"] == "Middle"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mailbox_updates",
    [
        {"public_enabled": False},
        {"is_hidden": True},
    ],
)
async def test_public_mailbox_routes_respect_mailbox_visibility_flags(
    app_client,
    runtime,
    sample_email_bytes: bytes,
    mailbox_updates: dict[str, bool],
) -> None:
    await runtime.create_domain("adb.com")
    await runtime.accept_message(
        rcpt_tos=["foo@adb.com"],
        envelope_from="sender@example.com",
        content=sample_email_bytes,
    )
    await runtime.drain_parser_queue()

    mailbox = runtime.mailboxes.list_mailboxes()["items"][0]
    await runtime.mailboxes.update_mailbox(mailbox["id"], mailbox_updates)

    web_response = await app_client.get("/mail/foo@adb.com")
    api_response = await app_client.get(
        "/api/v1/public/mailboxes/foo@adb.com/messages",
        headers={"X-API-Key": str(runtime.settings.public_api_key)},
    )

    assert web_response.status_code == 404
    assert api_response.status_code == 404


@pytest.mark.asyncio
async def test_public_mailbox_page_shows_copy_button_for_openai_verification_code(app_client, runtime) -> None:
    await runtime.create_domain("adb.com")
    await runtime.accept_message(
        rcpt_tos=["foo@adb.com"],
        envelope_from="noreply@openai.com",
        content=_rich_mail_bytes(
            subject="Your OpenAI verification code",
            message_id="openai-otp@example.com",
            from_addr="OpenAI <noreply@openai.com>",
            body="Your OpenAI verification code is 654321.\nUse this code to verify your email.\n",
        ),
    )
    await runtime.drain_parser_queue()

    response = await app_client.get("/mail/foo@adb.com")

    assert response.status_code == 200
    assert "复制验证码" in response.text
    assert "654321" in response.text


@pytest.mark.asyncio
async def test_public_mailbox_page_ignores_numbers_without_verification_keywords(app_client, runtime) -> None:
    await runtime.create_domain("adb.com")
    await runtime.accept_message(
        rcpt_tos=["foo@adb.com"],
        envelope_from="sender@example.com",
        content=_rich_mail_bytes(
            subject="Order update",
            message_id="non-otp@example.com",
            from_addr="Store <sender@example.com>",
            body="Order 123456 has shipped and will arrive tomorrow.\n",
        ),
    )
    await runtime.drain_parser_queue()

    response = await app_client.get("/mail/foo@adb.com")

    assert response.status_code == 200
    assert 'data-code="123456"' not in response.text
    assert "验证码 123456" not in response.text


@pytest.mark.asyncio
async def test_public_mailbox_page_ignores_mail_with_multiple_candidate_codes(app_client, runtime) -> None:
    await runtime.create_domain("adb.com")
    await runtime.accept_message(
        rcpt_tos=["foo@adb.com"],
        envelope_from="sender@example.com",
        content=_rich_mail_bytes(
            subject="Verification code candidates",
            message_id="multi-otp@example.com",
            from_addr="Example <sender@example.com>",
            body="Your verification code could be 123456 or 654321 depending on region.\n",
        ),
    )
    await runtime.drain_parser_queue()

    response = await app_client.get("/mail/foo@adb.com")

    assert response.status_code == 200
    assert 'data-code="123456"' not in response.text
    assert 'data-code="654321"' not in response.text
    assert "验证码 123456" not in response.text
    assert "验证码 654321" not in response.text


@pytest.mark.asyncio
async def test_public_mailbox_page_extracts_html_openai_verification_code(app_client, runtime) -> None:
    await runtime.create_domain("adb.com")
    await runtime.accept_message(
        rcpt_tos=["foo@adb.com"],
        envelope_from="noreply@openai.com",
        content=_rich_mail_bytes(
            subject="Verify your email",
            message_id="html-openai-otp@example.com",
            from_addr="OpenAI <noreply@openai.com>",
            subtype="html",
            body=(
                "<html><body><h1>Verify your email</h1>"
                "<p>Your OpenAI verification code</p>"
                "<table><tr><td>482951</td></tr></table>"
                "</body></html>"
            ),
        ),
    )
    await runtime.drain_parser_queue()

    response = await app_client.get("/mail/foo@adb.com")

    assert response.status_code == 200
    assert "复制验证码" in response.text
    assert "482951" in response.text


@pytest.mark.asyncio
async def test_public_mailbox_page_extracts_chatgpt_login_code_from_css_heavy_openai_html(app_client, runtime) -> None:
    noisy_css = " ".join(
        f".rule-{index} {{ font-family: Sohne; background-image: url(https://cdn.openai.com/font-{index}.woff2); }}"
        for index in range(20)
    )
    await runtime.create_domain("adb.com")
    await runtime.accept_message(
        rcpt_tos=["foo@adb.com"],
        envelope_from="noreply@tm.openai.com",
        content=_rich_mail_bytes(
            subject="Your temporary ChatGPT login code",
            message_id="chatgpt-login-code@example.com",
            from_addr="OpenAI <noreply@tm.openai.com>",
            subtype="html",
            body=(
                "<html><head><style>"
                f"{noisy_css}"
                "</style></head><body>"
                "<p>Enter this temporary verification code to continue:</p>"
                "<p>138349</p>"
                "</body></html>"
            ),
        ),
    )
    await runtime.drain_parser_queue()

    response = await app_client.get("/mail/foo@adb.com")

    assert response.status_code == 200
    assert 'class="btn btn-primary copy-code-btn"' in response.text
    assert "138349" in response.text


@pytest.mark.asyncio
async def test_public_mailbox_page_includes_websocket_bootstrap_on_first_page(app_client, runtime) -> None:
    await runtime.create_domain("adb.com")
    await runtime.accept_message(
        rcpt_tos=["foo@adb.com"],
        envelope_from="sender@example.com",
        content=_mail_bytes("Bootstrap", "bootstrap@example.com", "bootstrap"),
    )
    await runtime.drain_parser_queue()

    response = await app_client.get("/mail/foo@adb.com")

    assert response.status_code == 200
    assert "/mail/foo@adb.com/ws?after_cursor=" in response.text
    assert 'id="mail-list"' in response.text
    assert 'data-live-enabled="true"' in response.text


def test_public_mailbox_websocket_receives_new_delivery_and_parse_update(tmp_path) -> None:
    settings = default_settings(tmp_path)
    app = create_app(settings=settings)

    with TestClient(app) as client:
        runtime = app.state.runtime
        client.portal.call(runtime.create_domain, "adb.com")

        page = client.get("/mail/foo@adb.com")
        assert page.status_code == 200
        marker = "/mail/foo@adb.com/ws?after_cursor="
        start = page.text.index(marker) + len(marker)
        cursor = page.text[start: page.text.index('"', start)]

        with client.websocket_connect(f"/mail/foo@adb.com/ws?after_cursor={cursor}") as websocket:
            client.portal.call(
                partial(
                    runtime.accept_message,
                    rcpt_tos=["foo@adb.com"],
                    envelope_from="sender@example.com",
                    content=_mail_bytes("Live Subject", "live@example.com", "live body"),
                )
            )

            inserted = websocket.receive_json()
            updated = websocket.receive_json()

        assert inserted["type"] == "mailbox_delivery"
        assert inserted["item"]["delivery_id"].startswith("dlv_")
        assert inserted["item"]["parse_status"] == "pending"
        assert inserted["item"]["subject"] is None
        assert inserted["item"]["verification_code"] is None

        assert updated["type"] == "mailbox_delivery_updated"
        assert updated["item"]["delivery_id"] == inserted["item"]["delivery_id"]
        assert updated["item"]["parse_status"] == "parsed"
        assert updated["item"]["subject"] == "Live Subject"
