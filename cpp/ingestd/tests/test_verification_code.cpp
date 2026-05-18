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

void test_verification_code_extracts_openai_localized_codes() {
    const auto zh_subject = extract("你的 OpenAI 代码为 752915", "noreply@tm.openai.com", "");
    test::check(zh_subject.has_value() && *zh_subject == "752915", "openai chinese subject code");

    const auto ja_html = extract(
        "OpenAI の一時的な認証コード", "noreply@tm.openai.com", "",
        "<html><body><p>この一時検証コードを入力してください。</p><p><strong>284691</strong></p></body></html>");
    test::check(ja_html.has_value() && *ja_html == "284691", "openai japanese html code");

    const auto ko_html = extract(
        "OpenAI 임시 인증 코드", "otp@tm1.openai.com", "",
        "<html><body><p>이 임시 인증 코드를 입력하세요.</p><p><strong>391742</strong></p></body></html>");
    test::check(ko_html.has_value() && *ko_html == "391742", "openai korean html code");

    const auto pt_html = extract(
        "Seu código de verificação temporário do OpenAI", "noreply@tm.openai.com", "",
        "<html><body><p>Use este código de verificação temporário.</p><p><strong>640218</strong></p></body></html>");
    test::check(pt_html.has_value() && *pt_html == "640218", "openai portuguese html code");

    const auto pt_dirty_preview = extract(
        "Seu código de verificação temporário do OpenAI", "otp@tm1.openai.com", "",
        "<html><body><p>Use este código de verificação temporário.</p><p><strong>050328</strong></p></body></html>",
        "Seu código de verificação temporário do OpenAI @font-face { font-weight: 400; color:#000000; padding: 56px 0 32px 0; }");
    test::check(pt_dirty_preview.has_value() && *pt_dirty_preview == "050328",
                "openai portuguese html code with dirty preview");

    const auto de_html = extract(
        "Dein temporärer Bestätigungscode für OpenAI", "noreply@tm.openai.com", "",
        "<html><body><p>Gib diesen Bestätigungscode ein.</p><p><strong>583104</strong></p></body></html>");
    test::check(de_html.has_value() && *de_html == "583104", "openai german html code");

    const auto fr_subject = extract("Votre code OpenAI : 668266", "noreply@tm.openai.com", "");
    test::check(fr_subject.has_value() && *fr_subject == "668266", "openai french subject code");

    const auto pt_subject = extract("Seu código do OpenAI é 317401", "noreply@tm.openai.com", "");
    test::check(pt_subject.has_value() && *pt_subject == "317401", "openai portuguese subject code");

    const auto de_subject = extract("Dein Code für OpenAI: 639584", "noreply@tm.openai.com", "");
    test::check(de_subject.has_value() && *de_subject == "639584", "openai german subject code");
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
