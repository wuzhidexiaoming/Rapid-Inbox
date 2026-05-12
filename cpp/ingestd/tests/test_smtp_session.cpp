#include "../src/domain_matcher.h"
#include "../src/mail_queue.h"
#include "../src/smtp_session.h"

#include <chrono>
#include <string>
#include <unordered_map>

namespace test {
void check(bool condition, const std::string& message);
}

void test_smtp_session_accepts_valid_message() {
    rapid_inbox::ingestd::DomainMatcher matcher({{1, "adb.com", true, true, "keep", false}});
    rapid_inbox::ingestd::MailQueue queue(10);
    rapid_inbox::ingestd::SmtpSession session(matcher, queue, 20, 1024 * 1024);
    test::check(session.handle_line("EHLO client") == "250 rapid-inbox-ingestd", "ehlo");
    test::check(session.handle_line("MAIL FROM:<sender@example.com>") == "250 OK", "mail from");
    test::check(session.handle_line("RCPT TO:<Code@adb.com>") == "250 OK", "rcpt");
    test::check(session.handle_line("DATA") == "354 End data with <CR><LF>.<CR><LF>", "data");
    test::check(session.handle_line("Subject: Hi") == "", "data line no response");
    test::check(session.handle_line("") == "", "blank data line");
    const std::string queued = session.handle_line(".");
    test::check(queued.rfind("250 queued as msg_", 0) == 0, "queued response");
    auto batch = queue.pop_batch(10, std::chrono::milliseconds(1));
    test::check(batch.size() == 1, "queued one job");
    test::check(batch[0].recipients[0].match.address_canonical == "code@adb.com", "canonical recipient");
}

void test_smtp_session_attaches_domain_policy_snapshot() {
    rapid_inbox::ingestd::DomainMatcher matcher({{1, "adb.com", true, true, "strip", true}});
    rapid_inbox::ingestd::DomainPolicySnapshot policy;
    policy.root_domain_unicode = "ADB.COM";
    policy.accept_exact = true;
    policy.accept_subdomains = false;
    policy.public_web_enabled = false;
    policy.public_api_enabled = true;
    policy.is_active = true;
    policy.is_hidden = true;
    policy.plus_addressing_mode = "strip";
    policy.local_part_case_sensitive = true;
    policy.max_message_size_bytes = 12345;
    policy.retention_days = 7;
    policy.dns_status = "warning";

    rapid_inbox::ingestd::MailQueue queue(10);
    rapid_inbox::ingestd::SmtpSession session(
        matcher,
        queue,
        20,
        1024 * 1024,
        std::unordered_map<int, rapid_inbox::ingestd::DomainPolicySnapshot>{{1, policy}});

    test::check(session.handle_line("MAIL FROM:<sender@example.com>") == "250 OK", "mail from");
    test::check(session.handle_line("RCPT TO:<User+Tag@adb.com>") == "250 OK", "rcpt");
    test::check(session.handle_line("DATA") == "354 End data with <CR><LF>.<CR><LF>", "data");
    test::check(session.handle_line("Subject: Policy") == "", "body");
    test::check(session.handle_line(".").rfind("250 queued as msg_", 0) == 0, "queued");

    auto batch = queue.pop_batch(10, std::chrono::milliseconds(1));
    test::check(batch.size() == 1, "queued one job");
    test::check(batch[0].recipients.size() == 1, "queued one recipient");
    const auto& snapshot = batch[0].recipients[0].domain_policy;
    test::check(snapshot.has_value(), "recipient has domain policy");
    test::check(snapshot->root_domain_unicode == "ADB.COM", "domain policy root unicode");
    test::check(snapshot->accept_subdomains == false, "domain policy accept subdomains");
    test::check(snapshot->public_web_enabled == false, "domain policy public web");
    test::check(snapshot->is_hidden == true, "domain policy hidden");
    test::check(snapshot->plus_addressing_mode == "strip", "domain policy plus mode");
    test::check(snapshot->local_part_case_sensitive == true, "domain policy case mode");
    test::check(snapshot->max_message_size_bytes == 12345, "domain policy max size");
    test::check(snapshot->retention_days.has_value() && *snapshot->retention_days == 7,
                "domain policy retention");
    test::check(snapshot->dns_status == "warning", "domain policy dns");
}

void test_smtp_session_rejects_unknown_domain() {
    rapid_inbox::ingestd::DomainMatcher matcher({{1, "adb.com", true, true, "keep", false}});
    rapid_inbox::ingestd::MailQueue queue(10);
    rapid_inbox::ingestd::SmtpSession session(matcher, queue, 20, 1024 * 1024);
    test::check(session.handle_line("MAIL FROM:<sender@example.com>") == "250 OK", "mail from");
    test::check(session.handle_line("RCPT TO:<Code@unknown.com>") == "550 domain not allowed", "unknown rejected");
}

void test_smtp_session_rejects_prefix_collision_commands() {
    rapid_inbox::ingestd::DomainMatcher matcher({{1, "adb.com", true, true, "keep", false}});
    rapid_inbox::ingestd::MailQueue queue(10);
    rapid_inbox::ingestd::SmtpSession session(matcher, queue, 20, 1024 * 1024);
    test::check(session.handle_line("EHLOX client") == "502 command not implemented", "ehlox rejected");
    test::check(session.handle_line("MAIL FROM:<sender@example.com>") == "250 OK", "mail from");
    test::check(session.handle_line("RCPT TO:<Code@adb.com>") == "250 OK", "rcpt");
    test::check(session.handle_line("DATAX") == "502 command not implemented", "datax rejected");
    test::check(session.handle_line("DATA") == "354 End data with <CR><LF>.<CR><LF>", "data still accepted");
}

void test_smtp_session_clears_transaction_after_queueing() {
    rapid_inbox::ingestd::DomainMatcher matcher({{1, "adb.com", true, true, "keep", false}});
    rapid_inbox::ingestd::MailQueue queue(10);
    rapid_inbox::ingestd::SmtpSession session(matcher, queue, 20, 1024 * 1024);
    test::check(session.handle_line("MAIL FROM:<sender@example.com>") == "250 OK", "mail from");
    test::check(session.handle_line("RCPT TO:<Code@adb.com>") == "250 OK", "rcpt");
    test::check(session.handle_line("DATA") == "354 End data with <CR><LF>.<CR><LF>", "data");
    test::check(session.handle_line("Subject: First") == "", "body");
    test::check(session.handle_line(".").rfind("250 queued as msg_", 0) == 0, "queued");
    test::check(session.handle_line("DATA") == "554 no valid recipients", "second data rejects stale recipients");
    auto batch = queue.pop_batch(10, std::chrono::milliseconds(1));
    test::check(batch.size() == 1, "only first message queued");
}

void test_smtp_session_rejects_rcpt_before_mail_from() {
    rapid_inbox::ingestd::DomainMatcher matcher({{1, "adb.com", true, true, "keep", false}});
    rapid_inbox::ingestd::MailQueue queue(10);
    rapid_inbox::ingestd::SmtpSession session(matcher, queue, 20, 1024 * 1024);
    test::check(session.handle_line("RCPT TO:<Code@adb.com>") == "503 need MAIL FROM first", "rcpt before mail");
    test::check(session.handle_line("DATA") == "554 no valid recipients", "no recipient after rejected rcpt");
}

void test_smtp_session_rejects_empty_mail_from_without_changing_state() {
    rapid_inbox::ingestd::DomainMatcher matcher({{1, "adb.com", true, true, "keep", false}});
    rapid_inbox::ingestd::MailQueue queue(10);
    rapid_inbox::ingestd::SmtpSession session(matcher, queue, 20, 1024 * 1024);
    test::check(session.handle_line("MAIL FROM:<>") == "501 invalid sender", "empty sender rejected");
    test::check(session.handle_line("RCPT TO:<Code@adb.com>") == "503 need MAIL FROM first", "empty sender not set");
    test::check(session.handle_line("MAIL FROM:<sender@example.com>") == "250 OK", "valid sender");
    test::check(session.handle_line("MAIL FROM:   ") == "501 invalid sender", "blank sender rejected");
    test::check(session.handle_line("MAIL FROM:<broken@example.com") == "501 invalid sender", "malformed sender rejected");
    test::check(session.handle_line("RCPT TO:<Code@adb.com>") == "250 OK", "valid sender retained");
    test::check(session.handle_line("DATA") == "354 End data with <CR><LF>.<CR><LF>", "data");
    test::check(session.handle_line(".").rfind("250 queued as msg_", 0) == 0, "queued");
    auto batch = queue.pop_batch(10, std::chrono::milliseconds(1));
    test::check(batch.size() == 1, "queued one message");
    test::check(batch[0].envelope_from == "sender@example.com", "invalid sender did not replace valid sender");
}

void test_smtp_session_rejects_data_arguments() {
    rapid_inbox::ingestd::DomainMatcher matcher({{1, "adb.com", true, true, "keep", false}});
    rapid_inbox::ingestd::MailQueue queue(10);
    rapid_inbox::ingestd::SmtpSession session(matcher, queue, 20, 1024 * 1024);
    test::check(session.handle_line("MAIL FROM:<sender@example.com>") == "250 OK", "mail from");
    test::check(session.handle_line("RCPT TO:<Code@adb.com>") == "250 OK", "rcpt");
    test::check(session.handle_line("DATA anything") == "502 command not implemented", "data arguments rejected");
    test::check(session.handle_line("DATA") == "354 End data with <CR><LF>.<CR><LF>", "bare data accepted");
}

void test_smtp_session_discards_oversized_data_until_terminator() {
    rapid_inbox::ingestd::DomainMatcher matcher({{1, "adb.com", true, true, "keep", false}});
    rapid_inbox::ingestd::MailQueue queue(10);
    rapid_inbox::ingestd::SmtpSession session(matcher, queue, 20, 5);
    test::check(session.handle_line("MAIL FROM:<sender@example.com>") == "250 OK", "mail from");
    test::check(session.handle_line("RCPT TO:<Code@adb.com>") == "250 OK", "rcpt");
    test::check(session.handle_line("DATA") == "354 End data with <CR><LF>.<CR><LF>", "data");
    test::check(session.handle_line("123456") == "552 message too large", "oversize response");
    test::check(session.handle_line("EHLO body") == "", "oversize body discarded");
    test::check(session.handle_line(".") == "", "oversize terminator discarded");
    test::check(session.handle_line("DATA") == "554 no valid recipients", "oversize transaction cleared");
    auto batch = queue.pop_batch(10, std::chrono::milliseconds(1));
    test::check(batch.empty(), "oversize message not queued");
}

void test_smtp_session_reports_queue_full() {
    rapid_inbox::ingestd::DomainMatcher matcher({{1, "adb.com", true, true, "keep", false}});
    rapid_inbox::ingestd::MailQueue queue(0);
    rapid_inbox::ingestd::SmtpSession session(matcher, queue, 20, 1024 * 1024);
    test::check(session.handle_line("MAIL FROM:<sender@example.com>") == "250 OK", "mail from");
    test::check(session.handle_line("RCPT TO:<Code@adb.com>") == "250 OK", "rcpt");
    test::check(session.handle_line("DATA") == "354 End data with <CR><LF>.<CR><LF>", "data");
    test::check(session.handle_line("Subject: Full") == "", "body");
    test::check(session.handle_line(".") == "451 temporary queue full", "queue full response");
    test::check(queue.size() == 0, "queue remains empty");
}
