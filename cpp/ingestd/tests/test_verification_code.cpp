#include "../src/verification_code.h"

#include <optional>
#include <string>

namespace test {
void check(bool condition, const std::string& message);
}

namespace {

std::optional<std::string> extract(const std::string& subject,
                                   const std::string& sender,
                                   const std::string& text_body,
                                   const std::string& html_body = "",
                                   const std::string& preview = "") {
    return rapid_inbox::ingestd::extract_verification_code(subject, sender, text_body, html_body,
                                                           preview);
}

}  // namespace

void test_verification_code_extracts_plain_six_digit_code() {
    const auto code = extract("Your verification code", "noreply@example.com",
                              "Your verification code is 482913. It expires in 10 minutes.");
    test::check(code.has_value() && *code == "482913", "plain six digit code");

    const auto year_shaped_code =
        extract("Your PIN", "security@example.com", "Your security code is 2024.");
    test::check(year_shaped_code.has_value() && *year_shaped_code == "2024",
                "year-shaped four digit code");
}

void test_verification_code_extracts_chinese_code() {
    const auto code = extract("注册验证码", "service@example.cn",
                              "您的验证码是 736219，5 分钟内有效。请勿泄露给他人。");
    test::check(code.has_value() && *code == "736219", "chinese verification code");
}

void test_verification_code_extracts_grouped_digit_code() {
    const auto code = extract("Sign in to Example", "no-reply@example.com",
                              "Your one-time code: 123-456. It expires in 10 minutes.");
    test::check(code.has_value() && *code == "123456", "grouped digit code");
}

void test_verification_code_extracts_alphanumeric_code() {
    const auto code = extract("Your confirmation code", "no-reply@example.com",
                              "Please enter confirmation code A3F9B2 to continue.");
    test::check(code.has_value() && *code == "A3F9B2", "alphanumeric code");
}

void test_verification_code_extracts_html_openai_code() {
    const auto code = extract(
        "Verify your email", "noreply@openai.com", "",
        "<html><head><style>.x{color:#000;}</style></head><body>"
        "<h1>Verify your email</h1>"
        "<p>Your OpenAI verification code</p>"
        "<table><tr><td><strong>482951</strong></td></tr></table>"
        "</body></html>");
    test::check(code.has_value() && *code == "482951", "html openai code");
}

void test_verification_code_ignores_order_number() {
    const auto code =
        extract("Order update", "orders@shop.example.com",
                "Order 123456 has shipped and will arrive tomorrow.");
    test::check(!code.has_value(), "order number ignored");
}

void test_verification_code_ignores_ambiguous_two_codes() {
    const auto code = extract(
        "Verification code candidates", "sender@example.com",
        "Your verification code could be 123456 or 654321 depending on region.");
    test::check(!code.has_value(), "ambiguous code abstains");
}

void test_verification_code_extracts_numeric_html_entities() {
    const auto code = extract("Verify your email", "noreply@example.com", "",
                              "<p>Your verification code is "
                              "&#99999999999999999999999999999999999999999999999999999999999; "
                              "<strong>&#52;&#56;&#50;&#57;&#53;&#49;</strong></p>");
    test::check(code.has_value() && *code == "482951", "numeric html entities decoded");
}

void test_verification_code_ignores_spaced_currency_value() {
    const auto code = extract(
        "Confirm payment", "billing@example.com",
        "Use the confirmation code to approve the payment of $ 1234 today.");
    test::check(!code.has_value(), "spaced currency value ignored");
}

void test_verification_code_ignores_date_fragments() {
    const auto code = extract(
        "Verification reminder", "security@example.com",
        "Your verification request from 2026-04-18 is still pending.");
    test::check(!code.has_value(), "date fragments ignored");
}
