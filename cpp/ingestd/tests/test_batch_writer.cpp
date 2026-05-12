#include "../src/batch_writer.h"
#include "../src/mail_job.h"
#include "../src/sqlite_db.h"
#include "../src/storage_path.h"

#include <sqlite3.h>

#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

namespace test {
void check(bool condition, const std::string& message);
}

namespace {

namespace fs = std::filesystem;

rapid_inbox::ingestd::DomainPolicySnapshot sample_policy() {
    rapid_inbox::ingestd::DomainPolicySnapshot policy;
    policy.root_domain_unicode = "adb.example";
    policy.accept_exact = false;
    policy.accept_subdomains = true;
    policy.public_web_enabled = false;
    policy.public_api_enabled = true;
    policy.is_active = true;
    policy.is_hidden = true;
    policy.plus_addressing_mode = "strip";
    policy.local_part_case_sensitive = true;
    policy.max_message_size_bytes = 12345;
    policy.retention_days = 7;
    policy.dns_status = "warning";
    return policy;
}

rapid_inbox::ingestd::MailJob sample_job() {
    rapid_inbox::ingestd::MailJob job;
    job.smtp_session_id = "smtp_1";
    job.message_id = "msg_1";
    job.envelope_from = "sender@example.com";
    job.received_at = "2026-05-12T03:04:05Z";
    job.raw_content =
        "From: Sender <sender@example.com>\r\n"
        "To: code@adb.com\r\n"
        "Subject: Hello\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        "Your verification code is 123456.\r\n";
    job.raw_sha256 = "digest";
    job.raw_path = rapid_inbox::ingestd::raw_message_path(job.message_id, job.received_at);
    job.manifest_path = rapid_inbox::ingestd::manifest_path(job.message_id, job.received_at);
    rapid_inbox::ingestd::DomainMatch match{1, "adb.com", "adb.com", "code", "code", "code@adb.com"};
    job.recipients.push_back({"dlv_1", "code@adb.com", match, sample_policy()});
    return job;
}

std::string read_text_file(const fs::path& path) {
    std::ifstream input(path);
    return std::string((std::istreambuf_iterator<char>(input)), std::istreambuf_iterator<char>());
}

void write_text_file(const fs::path& path, const std::string& content) {
    std::ofstream output(path, std::ios::binary);
    output << content;
}

fs::path old_style_part_path(const fs::path& final_path) {
    return final_path.parent_path() / ("." + final_path.filename().string() + ".part");
}

void check_private_permissions(const fs::path& path,
                               fs::perms expected,
                               const std::string& message) {
    constexpr fs::perms mask = fs::perms::owner_all | fs::perms::group_all | fs::perms::others_all;
    const fs::perms actual = fs::status(path).permissions() & mask;
    test::check(actual == expected, message);
}

}  // namespace

void test_batch_writer_writes_raw_and_manifest() {
    const fs::path root = fs::temp_directory_path() / "rapid-inbox-writer-storage";
    fs::remove_all(root);
    const fs::path db_path = root / "app.db";
    rapid_inbox::ingestd::BatchWriter writer(root, db_path, 5000, false);
    const rapid_inbox::ingestd::MailJob job = sample_job();
    writer.write_storage_artifacts({job});
    const fs::path raw = root / job.raw_path;
    const fs::path manifest = root / job.manifest_path;
    test::check(fs::exists(raw), "raw file exists");
    test::check(fs::exists(manifest), "manifest file exists");
    const std::string raw_content = read_text_file(raw);
    test::check(raw_content == job.raw_content, "raw content");
    const std::string manifest_content = read_text_file(manifest);
    test::check(manifest_content.find("\"message_id\":\"msg_1\"") != std::string::npos,
                "manifest message id");
    test::check(manifest_content.find("\"rcpt_to\":\"code@adb.com\"") != std::string::npos,
                "manifest recipient");
}

void test_batch_writer_writes_private_storage_permissions() {
    const fs::path root = fs::temp_directory_path() / "rapid-inbox-writer-permissions";
    fs::remove_all(root);
    const fs::path db_path = root / "app.db";
    rapid_inbox::ingestd::BatchWriter writer(root, db_path, 5000, false);
    const rapid_inbox::ingestd::MailJob job = sample_job();

    writer.write_storage_artifacts({job});

    const fs::perms private_dir = fs::perms::owner_all;
    const fs::perms private_file = fs::perms::owner_read | fs::perms::owner_write;
    check_private_permissions(root, private_dir, "storage root is private");
    check_private_permissions(root / "raw", private_dir, "raw dir is private");
    check_private_permissions(root / "raw" / "2026", private_dir, "raw year dir is private");
    check_private_permissions(root / "raw" / "2026" / "05", private_dir,
                              "raw month dir is private");
    check_private_permissions(root / "raw" / "2026" / "05" / "12", private_dir,
                              "raw day dir is private");
    check_private_permissions(root / "manifests", private_dir, "manifest dir is private");
    check_private_permissions(root / "manifests" / "2026", private_dir,
                              "manifest year dir is private");
    check_private_permissions(root / "manifests" / "2026" / "05", private_dir,
                              "manifest month dir is private");
    check_private_permissions(root / "manifests" / "2026" / "05" / "12", private_dir,
                              "manifest day dir is private");
    check_private_permissions(root / job.raw_path, private_file, "raw file is private");
    check_private_permissions(root / job.manifest_path, private_file, "manifest file is private");
}

void test_batch_writer_manifest_includes_domain_policy_snapshot() {
    const fs::path root = fs::temp_directory_path() / "rapid-inbox-writer-domain-policy";
    fs::remove_all(root);
    const fs::path db_path = root / "app.db";
    rapid_inbox::ingestd::BatchWriter writer(root, db_path, 5000, false);
    const rapid_inbox::ingestd::MailJob job = sample_job();

    writer.write_storage_artifacts({job});

    const std::string manifest_content = read_text_file(root / job.manifest_path);
    test::check(manifest_content.find("\"domain_policy\":{") != std::string::npos,
                "manifest includes domain policy object");
    test::check(manifest_content.find("\"root_domain_unicode\":\"adb.example\"") !=
                    std::string::npos,
                "manifest domain policy unicode root");
    test::check(manifest_content.find("\"accept_exact\":false") != std::string::npos,
                "manifest domain policy accept_exact");
    test::check(manifest_content.find("\"accept_subdomains\":true") != std::string::npos,
                "manifest domain policy accept_subdomains");
    test::check(manifest_content.find("\"public_web_enabled\":false") != std::string::npos,
                "manifest domain policy public_web_enabled");
    test::check(manifest_content.find("\"public_api_enabled\":true") != std::string::npos,
                "manifest domain policy public_api_enabled");
    test::check(manifest_content.find("\"is_active\":true") != std::string::npos,
                "manifest domain policy is_active");
    test::check(manifest_content.find("\"is_hidden\":true") != std::string::npos,
                "manifest domain policy is_hidden");
    test::check(manifest_content.find("\"plus_addressing_mode\":\"strip\"") !=
                    std::string::npos,
                "manifest domain policy plus mode");
    test::check(manifest_content.find("\"local_part_case_sensitive\":true") !=
                    std::string::npos,
                "manifest domain policy case sensitivity");
    test::check(manifest_content.find("\"max_message_size_bytes\":12345") !=
                    std::string::npos,
                "manifest domain policy max message size");
    test::check(manifest_content.find("\"retention_days\":7") != std::string::npos,
                "manifest domain policy retention");
    test::check(manifest_content.find("\"dns_status\":\"warning\"") != std::string::npos,
                "manifest domain policy dns status");
}

void test_batch_writer_missing_domain_policy_rejects_without_creating_database() {
    const fs::path root = fs::temp_directory_path() / "rapid-inbox-writer-missing-domain-policy";
    fs::remove_all(root);
    const fs::path db_path = root / "missing.db";
    rapid_inbox::ingestd::BatchWriter writer(root, db_path, 5000, false);
    rapid_inbox::ingestd::MailJob job = sample_job();
    job.recipients[0].domain_policy.reset();

    bool threw = false;
    try {
        writer.write_storage_artifacts({job});
    } catch (const std::runtime_error&) {
        threw = true;
    }

    test::check(threw, "missing domain policy rejects storage write");
    test::check(!fs::exists(db_path), "missing domain policy does not create db");
    test::check(!fs::exists(db_path.string() + "-wal"), "missing domain policy does not create wal");
    test::check(!fs::exists(db_path.string() + "-shm"), "missing domain policy does not create shm");
}

void test_batch_writer_uses_job_policy_without_touching_database() {
    const fs::path root = fs::temp_directory_path() / "rapid-inbox-writer-job-policy";
    fs::remove_all(root);
    const fs::path db_path = root / "missing.db";
    rapid_inbox::ingestd::BatchWriter writer(root, db_path, 5000, false);
    const rapid_inbox::ingestd::MailJob job = sample_job();

    writer.write_storage_artifacts({job});

    test::check(fs::exists(root / job.raw_path), "raw file exists from job policy write");
    test::check(fs::exists(root / job.manifest_path), "manifest exists from job policy write");
    test::check(!fs::exists(db_path), "job policy write does not create db");
    test::check(!fs::exists(db_path.string() + "-wal"), "job policy write does not create wal");
    test::check(!fs::exists(db_path.string() + "-shm"), "job policy write does not create shm");
}

void test_batch_writer_ignores_preexisting_part_symlinks() {
    const fs::path root = fs::temp_directory_path() / "rapid-inbox-writer-part-symlink";
    fs::remove_all(root);
    const fs::path outside_raw = fs::temp_directory_path() / "rapid-inbox-writer-outside-raw.txt";
    const fs::path outside_manifest =
        fs::temp_directory_path() / "rapid-inbox-writer-outside-manifest.txt";
    write_text_file(outside_raw, "outside-safe");
    write_text_file(outside_manifest, "outside-safe");

    const fs::path db_path = root / "app.db";
    rapid_inbox::ingestd::BatchWriter writer(root, db_path, 5000, false);
    const rapid_inbox::ingestd::MailJob job = sample_job();

    const fs::path raw_final = root / job.raw_path;
    const fs::path manifest_final = root / job.manifest_path;
    fs::create_directories(raw_final.parent_path());
    fs::create_directories(manifest_final.parent_path());
    fs::remove(old_style_part_path(raw_final));
    fs::remove(old_style_part_path(manifest_final));
    fs::create_symlink(outside_raw, old_style_part_path(raw_final));
    fs::create_symlink(outside_manifest, old_style_part_path(manifest_final));

    writer.write_storage_artifacts({job});

    test::check(read_text_file(outside_raw) == "outside-safe", "outside raw file unchanged");
    test::check(read_text_file(outside_manifest) == "outside-safe",
                "outside manifest file unchanged");
    test::check(read_text_file(raw_final) == job.raw_content, "raw file written correctly");
    const std::string manifest_content = read_text_file(manifest_final);
    test::check(manifest_content.find("\"message_id\":\"msg_1\"") != std::string::npos,
                "manifest written correctly");
}

void test_batch_writer_writes_sqlite_parsed_records() {
    const fs::path root = fs::temp_directory_path() / "rapid-inbox-writer-sqlite";
    fs::remove_all(root);
    fs::create_directories(root);
    const fs::path db_path = root / "app.db";
    {
        rapid_inbox::ingestd::SqliteDb db(db_path, 5000);
        const fs::path schema_path = fs::path(RAPID_INBOX_REPO_ROOT) / "sqlite_schema.sql";
        std::ifstream schema(schema_path);
        std::string sql((std::istreambuf_iterator<char>(schema)),
                        std::istreambuf_iterator<char>());
        db.exec(sql);
        db.exec("INSERT INTO domains (id, root_domain_ascii, root_domain_unicode, created_at, "
                "updated_at) VALUES (1, 'adb.com', 'adb.com', '2026-05-12T03:04:05Z', "
                "'2026-05-12T03:04:05Z')");
    }

    rapid_inbox::ingestd::BatchWriter writer(root, db_path, 5000, false);
    const rapid_inbox::ingestd::MailJob job = sample_job();
    writer.write_batch({job});

    rapid_inbox::ingestd::SqliteDb db(db_path, 5000);
    auto message = db.prepare(
        "SELECT parse_status, raw_path, envelope_from, subject, text_preview, "
        "text_body_path, html_body_path, verification_code "
        "FROM messages WHERE id = 'msg_1'");
    test::check(message.step_row(), "message row exists");
    test::check(std::string(reinterpret_cast<const char*>(sqlite3_column_text(message.get(), 0))) ==
                    "parsed",
                "message parsed");
    test::check(std::string(reinterpret_cast<const char*>(sqlite3_column_text(message.get(), 1))) ==
                    job.raw_path,
                "message raw path");
    test::check(std::string(reinterpret_cast<const char*>(sqlite3_column_text(message.get(), 2))) ==
                    "sender@example.com",
                "message envelope from");
    test::check(std::string(reinterpret_cast<const char*>(sqlite3_column_text(message.get(), 3))) ==
                    "Hello",
                "message subject");
    test::check(std::string(reinterpret_cast<const char*>(sqlite3_column_text(message.get(), 4)))
                        .find("Your verification code is 123456") == 0,
                "message preview");
    const unsigned char* text_body_path_text = sqlite3_column_text(message.get(), 5);
    test::check(text_body_path_text != nullptr, "message text body path exists");
    const std::string text_body_path_value =
        reinterpret_cast<const char*>(text_body_path_text);
    test::check(sqlite3_column_type(message.get(), 6) == SQLITE_NULL, "message html path null");
    test::check(std::string(reinterpret_cast<const char*>(sqlite3_column_text(message.get(), 7))) ==
                    "123456",
                "message verification code");
    test::check(fs::exists(root / text_body_path_value), "text body file exists");
    test::check(read_text_file(root / text_body_path_value).find("Your verification code is 123456") ==
                    0,
                "text body content");
    const std::string manifest_content = read_text_file(root / job.manifest_path);
    test::check(manifest_content.find("\"parsed\":{\"status\":\"parsed\"") != std::string::npos,
                "manifest parsed status");
    test::check(manifest_content.find("\"text_body_path\":\"" + text_body_path_value + "\"") !=
                    std::string::npos,
                "manifest text body path");
    test::check(manifest_content.find("\"verification_code\":\"123456\"") != std::string::npos,
                "manifest verification code");

    auto mailbox =
        db.prepare("SELECT message_count, address_canonical FROM mailboxes WHERE "
                   "address_canonical = 'code@adb.com'");
    test::check(mailbox.step_row(), "mailbox row exists");
    test::check(sqlite3_column_int(mailbox.get(), 0) == 1, "mailbox count");

    auto delivery =
        db.prepare("SELECT id, rcpt_to FROM message_deliveries WHERE message_id = 'msg_1'");
    test::check(delivery.step_row(), "delivery exists");
    test::check(std::string(reinterpret_cast<const char*>(sqlite3_column_text(delivery.get(), 0))) ==
                    "dlv_1",
                "delivery id");
    test::check(std::string(reinterpret_cast<const char*>(sqlite3_column_text(delivery.get(), 1))) ==
                    "code@adb.com",
                "delivery rcpt");

    auto session = db.prepare("SELECT remote_ip, status, message_count, bytes_received, "
                              "last_command_at FROM smtp_sessions WHERE id = 'smtp_1'");
    test::check(session.step_row(), "smtp session row exists");
    test::check(std::string(reinterpret_cast<const char*>(sqlite3_column_text(session.get(), 0))) ==
                    "unknown",
                "smtp remote ip");
    test::check(std::string(reinterpret_cast<const char*>(sqlite3_column_text(session.get(), 1))) ==
                    "closed",
                "smtp status");
    test::check(sqlite3_column_int(session.get(), 2) == 1, "smtp message count");
    test::check(sqlite3_column_int64(session.get(), 3) ==
                    static_cast<sqlite3_int64>(job.raw_content.size()),
                "smtp bytes received");
    test::check(std::string(reinterpret_cast<const char*>(sqlite3_column_text(session.get(), 4))) ==
                    job.received_at,
                "smtp last command at");

    auto metric = db.prepare("SELECT deliveries FROM mail_metric_buckets WHERE bucket_ts = "
                             "'2026-05-12T03:04:05Z'");
    test::check(metric.step_row(), "metric bucket exists");
    test::check(sqlite3_column_int(metric.get(), 0) == 1, "metric deliveries");
}

void test_batch_writer_marks_parse_failure_without_rejecting_raw() {
    const fs::path root = fs::temp_directory_path() / "rapid-inbox-writer-parse-failure";
    fs::remove_all(root);
    fs::create_directories(root);
    const fs::path db_path = root / "app.db";
    {
        rapid_inbox::ingestd::SqliteDb db(db_path, 5000);
        const fs::path schema_path = fs::path(RAPID_INBOX_REPO_ROOT) / "sqlite_schema.sql";
        std::ifstream schema(schema_path);
        std::string sql((std::istreambuf_iterator<char>(schema)),
                        std::istreambuf_iterator<char>());
        db.exec(sql);
        db.exec("INSERT INTO domains (id, root_domain_ascii, root_domain_unicode, created_at, "
                "updated_at) VALUES (1, 'adb.com', 'adb.com', '2026-05-12T03:04:05Z', "
                "'2026-05-12T03:04:05Z')");
    }

    rapid_inbox::ingestd::BatchWriter writer(root, db_path, 5000, false);
    rapid_inbox::ingestd::MailJob job = sample_job();
    job.raw_content =
        "Subject: Broken\r\n"
        "Content-Type: multipart/mixed; boundary=\"missing\"\r\n"
        "\r\n"
        "body without boundary\r\n";
    writer.write_batch({job});

    rapid_inbox::ingestd::SqliteDb db(db_path, 5000);
    auto message = db.prepare(
        "SELECT parse_status, parse_error, text_body_path, verification_code "
        "FROM messages WHERE id = 'msg_1'");
    test::check(message.step_row(), "failed message row exists");
    test::check(std::string(reinterpret_cast<const char*>(sqlite3_column_text(message.get(), 0))) ==
                    "failed",
                "message failed");
    test::check(sqlite3_column_text(message.get(), 1) != nullptr, "message parse error");
    test::check(sqlite3_column_type(message.get(), 2) == SQLITE_NULL, "failed text path null");
    test::check(sqlite3_column_type(message.get(), 3) == SQLITE_NULL,
                "failed verification code null");
    test::check(fs::exists(root / job.raw_path), "failed raw file exists");
    test::check(fs::exists(root / job.manifest_path), "failed manifest file exists");
    const std::string manifest_content = read_text_file(root / job.manifest_path);
    test::check(manifest_content.find("\"parsed\":{\"status\":\"failed\"") != std::string::npos,
                "failed manifest parsed status");
    test::check(manifest_content.find("\"parse_error\":\"invalid multipart boundary\"") !=
                    std::string::npos,
                "failed manifest parse error");
}

void test_batch_writer_writes_parsed_attachment_records() {
    const fs::path root = fs::temp_directory_path() / "rapid-inbox-writer-attachments";
    fs::remove_all(root);
    fs::create_directories(root);
    const fs::path db_path = root / "app.db";
    {
        rapid_inbox::ingestd::SqliteDb db(db_path, 5000);
        const fs::path schema_path = fs::path(RAPID_INBOX_REPO_ROOT) / "sqlite_schema.sql";
        std::ifstream schema(schema_path);
        std::string sql((std::istreambuf_iterator<char>(schema)),
                        std::istreambuf_iterator<char>());
        db.exec(sql);
        db.exec("INSERT INTO domains (id, root_domain_ascii, root_domain_unicode, created_at, "
                "updated_at) VALUES (1, 'adb.com', 'adb.com', '2026-05-12T03:04:05Z', "
                "'2026-05-12T03:04:05Z')");
    }

    rapid_inbox::ingestd::BatchWriter writer(root, db_path, 5000, false);
    rapid_inbox::ingestd::MailJob job = sample_job();
    job.raw_content =
        "From: Sender <sender@example.com>\r\n"
        "To: code@adb.com\r\n"
        "Subject: Attachment\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: multipart/mixed; boundary=\"mixed-boundary\"\r\n"
        "\r\n"
        "--mixed-boundary\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        "Body.\r\n"
        "--mixed-boundary\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "Content-Disposition: attachment; filename=\"report.txt\"\r\n"
        "Content-Transfer-Encoding: base64\r\n"
        "\r\n"
        "UXVhcnRlcmx5IHJlcG9ydAo=\r\n"
        "--mixed-boundary--\r\n";
    writer.write_batch({job});

    rapid_inbox::ingestd::SqliteDb db(db_path, 5000);
    auto message =
        db.prepare("SELECT has_attachments, attachment_count FROM messages WHERE id = 'msg_1'");
    test::check(message.step_row(), "attachment message row exists");
    test::check(sqlite3_column_int(message.get(), 0) == 1, "message has attachments");
    test::check(sqlite3_column_int(message.get(), 1) == 1, "message attachment count");

    auto attachment = db.prepare(
        "SELECT filename, safe_filename, content_type, storage_path, size_bytes "
        "FROM attachments WHERE message_id = 'msg_1'");
    test::check(attachment.step_row(), "attachment row exists");
    test::check(
        std::string(reinterpret_cast<const char*>(sqlite3_column_text(attachment.get(), 0))) ==
            "report.txt",
        "attachment filename");
    test::check(
        std::string(reinterpret_cast<const char*>(sqlite3_column_text(attachment.get(), 1))) ==
            "report.txt",
        "attachment safe filename");
    test::check(
        std::string(reinterpret_cast<const char*>(sqlite3_column_text(attachment.get(), 2))) ==
            "text/plain",
        "attachment content type");
    const std::string storage_path =
        reinterpret_cast<const char*>(sqlite3_column_text(attachment.get(), 3));
    test::check(sqlite3_column_int(attachment.get(), 4) == 17, "attachment size");
    test::check(fs::exists(root / storage_path), "attachment file exists");
    test::check(read_text_file(root / storage_path) == "Quarterly report\n",
                "attachment file content");
}
