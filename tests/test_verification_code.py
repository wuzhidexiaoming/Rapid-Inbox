"""Unit tests for :mod:`app.services.verification_code`.

These tests cover the most common verification-code shapes, languages and
formatting patterns that appear in real inbound mails. They exercise the
extractor directly, bypassing storage and database layers.
"""

from __future__ import annotations

import pytest

from app.services.verification_code import extract_verification_code


def _extract(**kwargs) -> str | None:
    return extract_verification_code(
        subject=kwargs.get("subject"),
        sender=kwargs.get("sender"),
        text_body=kwargs.get("text_body"),
        html_body=kwargs.get("html_body"),
        preview=kwargs.get("preview"),
    )


# ---------------------------------------------------------------------------
# Happy path: common patterns we MUST extract
# ---------------------------------------------------------------------------


def test_extract_plain_six_digit_code() -> None:
    assert _extract(
        subject="Your verification code",
        sender="noreply@example.com",
        text_body="Your verification code is 482913. It expires in 10 minutes.",
    ) == "482913"


def test_extract_chinese_verification_code() -> None:
    assert _extract(
        subject="注册验证码",
        sender="service@example.cn",
        text_body="您的验证码是 736219，5 分钟内有效。请勿泄露给他人。",
    ) == "736219"


def test_extract_chinese_code_with_ascii_colon() -> None:
    assert _extract(
        subject="【示例】验证码",
        sender="noreply@示例.cn",
        text_body="验证码: 123456，请在 3 分钟内完成验证。",
    ) == "123456"


def test_extract_code_followed_by_colon_is_your_code() -> None:
    assert _extract(
        subject="Sign in to ExampleApp",
        sender="accounts@example.com",
        text_body="Your code is: 918273\nUse this to finish signing in.",
    ) == "918273"


def test_extract_four_digit_pin() -> None:
    assert _extract(
        subject="Your PIN",
        sender="security@example.com",
        text_body="Please enter the security code 7392 on the next screen.",
    ) == "7392"


def test_extract_eight_digit_code() -> None:
    assert _extract(
        subject="Your sign-in code",
        sender="accounts@example.com",
        text_body="Use the one-time code 12345678 to finish signing in.",
    ) == "12345678"


def test_extract_chinese_eight_digit_confirmation_code() -> None:
    assert _extract(
        subject="账户操作确认",
        sender="verify@example.test",
        text_body="本次操作确认码为 10293847，请在页面中输入完成验证。",
        html_body="<p>本次操作确认码为</p><code style=\"font-size:24px;\">10293847</code>",
    ) == "10293847"


def test_extract_grouped_digit_code() -> None:
    assert _extract(
        subject="Sign in to Example",
        sender="no-reply@example.com",
        text_body="Your one-time code: 123-456. It expires in 10 minutes.",
    ) == "123456"


def test_extract_alphanumeric_code() -> None:
    assert _extract(
        subject="Your confirmation code",
        sender="no-reply@example.com",
        text_body="Please enter confirmation code A3F9B2 to continue.",
    ) == "A3F9B2"


def test_extract_code_on_its_own_line() -> None:
    body = (
        "Hi there,\n\n"
        "Please use the following verification code to continue:\n\n"
        "  482951\n\n"
        "If you did not request this, you can ignore this message."
    )
    assert _extract(
        subject="Verify your email",
        sender="no-reply@openai.com",
        text_body=body,
    ) == "482951"


def test_extract_openai_style_html() -> None:
    html_body = (
        "<html><head><style>.x{color:#000;}</style></head><body>"
        "<h1>Verify your email</h1>"
        "<p>Your OpenAI verification code</p>"
        "<table><tr><td><strong>482951</strong></td></tr></table>"
        "</body></html>"
    )
    assert _extract(
        subject="Verify your email",
        sender="noreply@openai.com",
        html_body=html_body,
    ) == "482951"


def test_extract_chatgpt_login_code_with_heavy_css() -> None:
    noisy_css = " ".join(
        f".rule-{i} {{ font-family: Sohne; background-image: url(https://cdn.openai.com/font-{i}.woff2); }}"
        for i in range(20)
    )
    html_body = (
        "<html><head><style>"
        f"{noisy_css}"
        "</style></head><body>"
        "<p>Enter this temporary verification code to continue:</p>"
        "<p>138349</p>"
        "</body></html>"
    )
    assert _extract(
        subject="Your temporary ChatGPT login code",
        sender="noreply@tm.openai.com",
        html_body=html_body,
    ) == "138349"


def test_extract_google_style_subject_contains_code() -> None:
    # Google presents the code as ``G-283917`` but the actual 6-digit value is
    # what users copy-paste; accept either representation.
    result = _extract(
        subject="G-283917 is your Google verification code",
        sender="no-reply@accounts.google.com",
        text_body="Please do not share this code.\nYour verification code: G-283917",
    )
    assert result in {"283917", "G283917"}


def test_extract_apple_style_code() -> None:
    assert _extract(
        subject="Your Apple ID code",
        sender="no_reply@email.apple.com",
        text_body="Your Apple ID verification code is: 129-458. Do not share it with anyone.",
    ) == "129458"


def test_extract_github_style_code() -> None:
    assert _extract(
        subject="[GitHub] Please verify your device",
        sender="noreply@github.com",
        text_body="Verification code: 815624\n\nOnly enter this code if you are signing in.",
    ) == "815624"


def test_extract_bold_asterisk_code_in_markdown() -> None:
    assert _extract(
        subject="Your code",
        sender="hello@example.com",
        text_body="Please enter this verification code to continue:\n\n**482913**\n\nHave fun.",
    ) == "482913"


# ---------------------------------------------------------------------------
# Non-English variants
# ---------------------------------------------------------------------------


def test_extract_japanese_confirmation_code() -> None:
    assert _extract(
        subject="確認コードのお知らせ",
        sender="noreply@example.jp",
        text_body="お客様の確認コード: 482913 こちらの番号を入力してください。",
    ) == "482913"


def test_extract_korean_verification_code() -> None:
    assert _extract(
        subject="인증 코드 안내",
        sender="noreply@example.kr",
        text_body="귀하의 인증 코드는 482913 입니다. 5분 내에 입력해주세요.",
    ) == "482913"


def test_extract_spanish_verification_code() -> None:
    assert _extract(
        subject="Código de verificación",
        sender="noreply@example.es",
        text_body="Su código de verificación es 482913. No lo comparta con nadie.",
    ) == "482913"


def test_extract_openai_chinese_subject_code() -> None:
    assert _extract(
        subject="你的 OpenAI 代码为 752915",
        sender="noreply@tm.openai.com",
        text_body="",
    ) == "752915"


def test_extract_openai_french_subject_code() -> None:
    assert _extract(
        subject="Votre code OpenAI : 668266",
        sender="noreply@tm.openai.com",
        text_body="",
    ) == "668266"


def test_extract_openai_portuguese_subject_code() -> None:
    assert _extract(
        subject="Seu código do OpenAI é 317401",
        sender="noreply@tm.openai.com",
        text_body="",
    ) == "317401"


def test_extract_openai_german_subject_code() -> None:
    assert _extract(
        subject="Dein Code für OpenAI: 639584",
        sender="noreply@tm.openai.com",
        text_body="",
    ) == "639584"


@pytest.mark.parametrize(
    ("subject", "body_hint", "expected"),
    [
        ("OpenAI の一時的な認証コード", "この一時検証コードを入力してください。", "284691"),
        ("OpenAI 임시 인증 코드", "이 임시 인증 코드를 입력하세요.", "391742"),
        ("Seu código de verificação temporário do OpenAI", "Use este código de verificação temporário.", "640218"),
        ("Code de vérification temporaire pour OpenAI", "Utilisez ce code de vérification temporaire.", "730519"),
        ("Dein temporärer Bestätigungscode für OpenAI", "Gib diesen Bestätigungscode ein.", "583104"),
    ],
)
def test_extract_openai_localized_html_codes(subject: str, body_hint: str, expected: str) -> None:
    assert _extract(
        subject=subject,
        sender="noreply@tm.openai.com",
        html_body=f"<html><body><p>{body_hint}</p><p><strong>{expected}</strong></p></body></html>",
    ) == expected


def test_extract_ignores_dirty_preview_when_html_body_exists() -> None:
    assert _extract(
        subject="Seu código de verificação temporário do OpenAI",
        sender="otp@tm1.openai.com",
        html_body="<html><body><p>Use este código de verificação temporário.</p><p><strong>050328</strong></p></body></html>",
        preview="Seu código de verificação temporário do OpenAI @font-face { font-weight: 400; color:#000000; padding: 56px 0 32px 0; }",
    ) == "050328"


# ---------------------------------------------------------------------------
# False-positive guards: must NOT extract
# ---------------------------------------------------------------------------


def test_does_not_extract_order_number() -> None:
    assert _extract(
        subject="Order update",
        sender="orders@shop.example.com",
        text_body="Order 123456 has shipped and will arrive tomorrow.",
    ) is None


def test_does_not_extract_from_completely_unrelated_mail() -> None:
    assert _extract(
        subject="Weekly newsletter 2026-04-18",
        sender="news@example.com",
        text_body="Here are the 5 articles you missed this week. Total views: 12345.",
    ) is None


def test_does_not_extract_year_as_code() -> None:
    assert _extract(
        subject="Thanks for signing up",
        sender="welcome@example.com",
        text_body="Welcome aboard. Copyright 2024 Example Inc. See you soon.",
    ) is None


def test_does_not_extract_url_paths_with_digits() -> None:
    assert _extract(
        subject="Password reset",
        sender="security@example.com",
        text_body=(
            "Click https://example.com/reset/123456 to continue.\n"
            "This has nothing to do with a verification code."
        ),
    ) is None


def test_does_not_extract_phone_number() -> None:
    assert _extract(
        subject="Contact our support team",
        sender="support@example.com",
        text_body="Please call us at +1 (800) 555-0199 or 18005550199 for help.",
    ) is None


def test_extracts_correct_code_when_phone_number_also_present() -> None:
    assert _extract(
        subject="Your verification code",
        sender="security@example.com",
        text_body=(
            "Your verification code is 482913.\n"
            "If you need help, call +1 (800) 555-0199."
        ),
    ) == "482913"


def test_extracts_correct_code_when_currency_also_present() -> None:
    assert _extract(
        subject="Confirm your payment",
        sender="billing@example.com",
        text_body=(
            "Please enter the confirmation code 482913 to approve the $9,527.32 payment."
        ),
    ) == "482913"


# ---------------------------------------------------------------------------
# Ambiguity
# ---------------------------------------------------------------------------


def test_prefers_candidate_closest_to_verification_keyword() -> None:
    # Two six-digit numbers, only the second follows the context keyword.
    assert _extract(
        subject="Verification code",
        sender="security@example.com",
        text_body=(
            "Reference ID 111111 is for logging purposes only.\n"
            "Your verification code is 987654."
        ),
    ) == "987654"


def test_picks_code_on_isolated_line_when_two_candidates() -> None:
    body = (
        "Your verification code is below. It is case-sensitive.\n\n"
        "482913\n\n"
        "You may ignore the reference number 871623 used internally."
    )
    assert _extract(
        subject="Your verification code",
        sender="noreply@example.com",
        text_body=body,
    ) == "482913"



# ---------------------------------------------------------------------------
# Real-world-ish HTML & formatting
# ---------------------------------------------------------------------------


def test_ignores_numeric_style_values_in_html() -> None:
    html_body = (
        "<html><head>"
        "<style>.a { padding: 482913px; color: #c15f3c; }</style>"
        "</head><body>"
        "<div style=\"width:482913px;color:#c15f3c;\">Welcome</div>"
        "<p>Your verification code is 736219. It expires in 10 minutes.</p>"
        "</body></html>"
    )
    assert _extract(
        subject="Verification code",
        sender="noreply@example.com",
        html_body=html_body,
    ) == "736219"


def test_extract_when_body_contains_urls_with_digits() -> None:
    body = (
        "Hello,\n\n"
        "Use the link https://example.com/login/987654 to continue.\n"
        "Your verification code is 482913.\n"
    )
    assert _extract(
        subject="Your verification code",
        sender="security@example.com",
        text_body=body,
    ) == "482913"


def test_extract_code_when_subject_says_welcome_and_body_asks_to_verify() -> None:
    body = (
        "Welcome aboard! Please use 482913 to verify your email address "
        "and finish creating your account."
    )
    assert _extract(
        subject="Welcome to Example",
        sender="welcome@example.com",
        text_body=body,
    ) == "482913"


def test_abstains_on_pure_marketing_mail() -> None:
    body = (
        "Our Spring sale starts today! Enjoy up to 50% off on 12345 products, "
        "free shipping on orders over $99."
    )
    assert _extract(
        subject="Spring sale now live",
        sender="marketing@example.com",
        text_body=body,
    ) is None


def test_extract_code_inside_html_strong_block() -> None:
    html_body = (
        "<html><body>"
        "<p>Enter this code to confirm your sign-in:</p>"
        "<p><strong style=\"font-size:28px;letter-spacing:6px;\">482913</strong></p>"
        "</body></html>"
    )
    assert _extract(
        subject="Confirm your sign-in",
        sender="security@example.com",
        html_body=html_body,
    ) == "482913"


def test_extract_handles_html_entities() -> None:
    html_body = (
        "<p>Your verification code is <b>482913</b>&mdash;do not share it.</p>"
    )
    assert _extract(
        subject="Verification code",
        sender="security@example.com",
        html_body=html_body,
    ) == "482913"
