#include "batch_writer.h"

#include "id.h"
#include "json_util.h"
#include "mime_parser.h"
#include "sha256.h"
#include "sqlite_db.h"
#include "storage_path.h"
#include "verification_code.h"

#include <sqlite3.h>

#include <cerrno>
#include <cstdlib>
#include <fcntl.h>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <system_error>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>
#include <utility>
#include <variant>

namespace rapid_inbox::ingestd {
namespace {

class UniqueFd {
public:
    explicit UniqueFd(int fd) : fd_(fd) {}

    ~UniqueFd() {
        if (fd_ >= 0) {
            (void)::close(fd_);
        }
    }

    UniqueFd(const UniqueFd&) = delete;
    UniqueFd& operator=(const UniqueFd&) = delete;

    UniqueFd(UniqueFd&& other) noexcept : fd_(std::exchange(other.fd_, -1)) {}

    UniqueFd& operator=(UniqueFd&& other) noexcept {
        if (this != &other) {
            if (fd_ >= 0) {
                (void)::close(fd_);
            }
            fd_ = std::exchange(other.fd_, -1);
        }
        return *this;
    }

    int get() const {
        return fd_;
    }

    void close_or_throw(const std::string& context) {
        if (fd_ < 0) {
            return;
        }
        const int fd = std::exchange(fd_, -1);
        if (::close(fd) != 0) {
            throw std::system_error(errno, std::generic_category(), context);
        }
    }

private:
    int fd_;
};

UniqueFd open_for_fsync(const std::filesystem::path& path, int extra_flags) {
    const int fd = ::open(path.c_str(), O_RDONLY | extra_flags);
    if (fd < 0) {
        const int error = errno;
        throw std::system_error(error,
                                std::generic_category(),
                                "open failed for fsync: " + path.string());
    }
    return UniqueFd(fd);
}

void fsync_path(const std::filesystem::path& path, int extra_flags) {
    UniqueFd fd = open_for_fsync(path, extra_flags);
    if (::fsync(fd.get()) != 0) {
        const int error = errno;
        throw std::system_error(error, std::generic_category(), "fsync failed: " + path.string());
    }
    fd.close_or_throw("close failed after fsync: " + path.string());
}

void fsync_directory(const std::filesystem::path& path) {
    fsync_path(path, O_DIRECTORY);
}

bool path_is_at_or_inside_root(const std::filesystem::path& root,
                               const std::filesystem::path& target) {
    if (target == root) {
        return true;
    }
    const auto relative = target.lexically_relative(root);
    if (relative.empty()) {
        return false;
    }
    for (const auto& part : relative) {
        if (part == "..") {
            return false;
        }
    }
    return true;
}

void throw_errno(const std::string& context, int error) {
    throw std::system_error(error, std::generic_category(), context);
}

void chmod_private(const std::filesystem::path& path, bool directory) {
    const auto permissions = directory
                                 ? std::filesystem::perms::owner_all
                                 : std::filesystem::perms::owner_read |
                                       std::filesystem::perms::owner_write;
    std::filesystem::permissions(path, permissions, std::filesystem::perm_options::replace);
}

void mkdir_private(const std::filesystem::path& path) {
    if (::mkdir(path.c_str(), 0700) == 0) {
        return;
    }
    const int error = errno;
    if (error == EEXIST) {
        struct stat status {};
        if (::stat(path.c_str(), &status) != 0) {
            const int stat_error = errno;
            throw_errno("stat failed for directory: " + path.string(), stat_error);
        }
        if (!S_ISDIR(status.st_mode)) {
            throw std::runtime_error("storage path component is not a directory: " +
                                     path.string());
        }
        return;
    }
    throw_errno("mkdir failed: " + path.string(), error);
}

void ensure_private_directory_chain(const std::filesystem::path& root,
                                    const std::filesystem::path& directory) {
    const auto canonical_root = std::filesystem::weakly_canonical(root);
    const auto canonical_directory = std::filesystem::weakly_canonical(directory);
    if (!path_is_at_or_inside_root(canonical_root, canonical_directory)) {
        throw std::runtime_error("storage directory path escapes storage root");
    }

    std::filesystem::path current = canonical_directory.root_path();
    for (const auto& part : canonical_directory.relative_path()) {
        current /= part;
        mkdir_private(current);
        if (path_is_at_or_inside_root(canonical_root, current)) {
            chmod_private(current, true);
        }
    }
}

void fsync_directory_chain_to_filesystem_root(const std::filesystem::path& root,
                                              const std::filesystem::path& directory) {
    const auto canonical_root = std::filesystem::weakly_canonical(root);
    auto current = std::filesystem::weakly_canonical(directory);
    if (!path_is_at_or_inside_root(canonical_root, current)) {
        throw std::runtime_error("fsync directory path escapes storage root");
    }

    while (true) {
        fsync_directory(current);
        if (current.parent_path() == current) {
            break;
        }
        current = current.parent_path();
    }
}

const char* json_bool(int value) {
    return value == 0 ? "false" : "true";
}

void write_all(UniqueFd& fd, const std::filesystem::path& path, const std::string& content) {
    const char* cursor = content.data();
    std::size_t remaining = content.size();
    while (remaining > 0) {
        const ssize_t written = ::write(fd.get(), cursor, remaining);
        if (written < 0) {
            if (errno == EINTR) {
                continue;
            }
            const int write_error = errno;
            throw_errno("write failed: " + path.string(), write_error);
        }
        if (written == 0) {
            throw std::runtime_error("write made no progress: " + path.string());
        }
        cursor += written;
        remaining -= static_cast<std::size_t>(written);
    }
}

std::pair<UniqueFd, std::filesystem::path> create_temp_file(const std::filesystem::path& target) {
    const auto temp_path =
        target.parent_path() / ("." + target.filename().string() + ".tmp.XXXXXX");
    std::string temp_template = temp_path.string();
    const int fd = ::mkstemp(temp_template.data());
    if (fd < 0) {
        const int mkstemp_error = errno;
        throw_errno("mkstemp failed: " + temp_path.string(), mkstemp_error);
    }
    return {UniqueFd(fd), std::filesystem::path(temp_template)};
}

std::string build_domain_policy(const DomainPolicySnapshot& policy) {
    std::ostringstream output;
    output << "{";
    output << "\"root_domain_unicode\":\"" << json_escape(policy.root_domain_unicode) << "\",";
    output << "\"accept_exact\":" << json_bool(policy.accept_exact ? 1 : 0) << ",";
    output << "\"accept_subdomains\":" << json_bool(policy.accept_subdomains ? 1 : 0) << ",";
    output << "\"public_web_enabled\":" << json_bool(policy.public_web_enabled ? 1 : 0) << ",";
    output << "\"public_api_enabled\":" << json_bool(policy.public_api_enabled ? 1 : 0) << ",";
    output << "\"is_active\":" << json_bool(policy.is_active ? 1 : 0) << ",";
    output << "\"is_hidden\":" << json_bool(policy.is_hidden ? 1 : 0) << ",";
    output << "\"plus_addressing_mode\":\"" << json_escape(policy.plus_addressing_mode) << "\",";
    output << "\"local_part_case_sensitive\":"
           << json_bool(policy.local_part_case_sensitive ? 1 : 0) << ",";
    output << "\"max_message_size_bytes\":" << policy.max_message_size_bytes << ",";
    output << "\"retention_days\":";
    if (policy.retention_days.has_value()) {
        output << *policy.retention_days;
    } else {
        output << "null";
    }
    output << ",";
    output << "\"dns_status\":\"" << json_escape(policy.dns_status) << "\"";
    output << "}";
    return output.str();
}

std::runtime_error sqlite_bind_error(sqlite3_stmt* statement,
                                     int rc,
                                     const std::string& context) {
    sqlite3* db = sqlite3_db_handle(statement);
    const char* message = db == nullptr ? sqlite3_errstr(rc) : sqlite3_errmsg(db);
    return std::runtime_error(context + ": " + message);
}

void bind_text(Statement& statement,
               int index,
               const std::string& value,
               const std::string& context) {
    const int rc = sqlite3_bind_text(statement.get(), index, value.c_str(), -1, SQLITE_TRANSIENT);
    if (rc != SQLITE_OK) {
        throw sqlite_bind_error(statement.get(), rc, context);
    }
}

void bind_int64(Statement& statement,
                int index,
                sqlite3_int64 value,
                const std::string& context) {
    const int rc = sqlite3_bind_int64(statement.get(), index, value);
    if (rc != SQLITE_OK) {
        throw sqlite_bind_error(statement.get(), rc, context);
    }
}

void bind_optional_text(Statement& statement,
                        int index,
                        const std::optional<std::string>& value,
                        const std::string& context) {
    if (!value.has_value()) {
        const int rc = sqlite3_bind_null(statement.get(), index);
        if (rc != SQLITE_OK) {
            throw sqlite_bind_error(statement.get(), rc, context);
        }
        return;
    }
    bind_text(statement, index, *value, context);
}

void bind_null(Statement& statement, int index, const std::string& context) {
    const int rc = sqlite3_bind_null(statement.get(), index);
    if (rc != SQLITE_OK) {
        throw sqlite_bind_error(statement.get(), rc, context);
    }
}

const ParsedMail* parsed_result(const std::variant<ParsedMail, ParseFailure>& result) {
    return std::holds_alternative<ParsedMail>(result) ? &std::get<ParsedMail>(result) : nullptr;
}

const ParseFailure* failure_result(const std::variant<ParsedMail, ParseFailure>& result) {
    return std::holds_alternative<ParseFailure>(result) ? &std::get<ParseFailure>(result) : nullptr;
}

void prepare_parsed_artifact_metadata(const MailJob& job, ParsedMail& parsed) {
    if (!parsed.text_body.empty()) {
        parsed.has_text = true;
        parsed.text_body_path = text_body_path(job.message_id, job.received_at);
    }
    if (!parsed.html_body.empty()) {
        parsed.has_html = true;
        parsed.html_body_path = html_body_path(job.message_id, job.received_at);
    }
    for (ParsedAttachment& attachment : parsed.attachments) {
        if (attachment.attachment_id.empty()) {
            attachment.attachment_id = make_prefixed_id("att_");
        }
        attachment.safe_filename =
            safe_filename(attachment.filename.value_or("attachment.bin"));
        attachment.storage_path =
            attachment_path(job.message_id, attachment.attachment_id, attachment.safe_filename);
        attachment.sha256 = sha256_hex(attachment.content);
    }
    parsed.attachment_count = static_cast<int>(parsed.attachments.size());
    parsed.has_attachments = parsed.attachment_count > 0;
}

std::string parsed_manifest_json(const ParsedMail& parsed) {
    std::ostringstream output;
    output << "{";
    output << "\"status\":\"parsed\",";
    output << "\"message_id_header\":";
    if (parsed.message_id_header.has_value()) {
        output << "\"" << json_escape(*parsed.message_id_header) << "\"";
    } else {
        output << "null";
    }
    output << ",\"subject\":";
    if (parsed.subject.has_value()) {
        output << "\"" << json_escape(*parsed.subject) << "\"";
    } else {
        output << "null";
    }
    output << ",\"from_name\":";
    if (parsed.from_name.has_value()) {
        output << "\"" << json_escape(*parsed.from_name) << "\"";
    } else {
        output << "null";
    }
    output << ",\"from_addr\":";
    if (parsed.from_addr.has_value()) {
        output << "\"" << json_escape(*parsed.from_addr) << "\"";
    } else {
        output << "null";
    }
    output << ",\"reply_to\":";
    if (parsed.reply_to.has_value()) {
        output << "\"" << json_escape(*parsed.reply_to) << "\"";
    } else {
        output << "null";
    }
    output << ",\"date_header\":";
    if (parsed.date_header.has_value()) {
        output << "\"" << json_escape(*parsed.date_header) << "\"";
    } else {
        output << "null";
    }
    output << ",\"has_text\":" << json_bool(parsed.has_text ? 1 : 0);
    output << ",\"has_html\":" << json_bool(parsed.has_html ? 1 : 0);
    output << ",\"has_attachments\":" << json_bool(parsed.has_attachments ? 1 : 0);
    output << ",\"attachment_count\":" << parsed.attachment_count;
    output << ",\"text_preview\":";
    if (parsed.text_preview.has_value()) {
        output << "\"" << json_escape(*parsed.text_preview) << "\"";
    } else {
        output << "null";
    }
    output << ",\"text_body_path\":";
    if (parsed.text_body_path.has_value()) {
        output << "\"" << json_escape(*parsed.text_body_path) << "\"";
    } else {
        output << "null";
    }
    output << ",\"html_body_path\":";
    if (parsed.html_body_path.has_value()) {
        output << "\"" << json_escape(*parsed.html_body_path) << "\"";
    } else {
        output << "null";
    }
    output << ",\"headers_json\":" << parsed.headers_json;
    output << ",\"verification_code\":";
    if (parsed.verification_code.has_value()) {
        output << "\"" << json_escape(*parsed.verification_code) << "\"";
    } else {
        output << "null";
    }
    output << ",\"attachments\":[";
    for (std::size_t index = 0; index < parsed.attachments.size(); ++index) {
        const ParsedAttachment& attachment = parsed.attachments[index];
        if (index != 0) {
            output << ",";
        }
        output << "{";
        output << "\"id\":\"" << json_escape(attachment.attachment_id) << "\",";
        output << "\"part_index\":" << attachment.part_index << ",";
        output << "\"filename\":";
        if (attachment.filename.has_value()) {
            output << "\"" << json_escape(*attachment.filename) << "\"";
        } else {
            output << "null";
        }
        output << ",\"safe_filename\":\"" << json_escape(attachment.safe_filename) << "\",";
        output << "\"content_type\":\"" << json_escape(attachment.content_type) << "\",";
        output << "\"content_disposition\":";
        if (attachment.content_disposition.has_value()) {
            output << "\"" << json_escape(*attachment.content_disposition) << "\"";
        } else {
            output << "null";
        }
        output << ",\"content_id\":";
        if (attachment.content_id.has_value()) {
            output << "\"" << json_escape(*attachment.content_id) << "\"";
        } else {
            output << "null";
        }
        output << ",\"storage_path\":\"" << json_escape(attachment.storage_path) << "\",";
        output << "\"sha256\":\"" << json_escape(attachment.sha256) << "\",";
        output << "\"size_bytes\":" << attachment.content.size() << ",";
        output << "\"is_inline\":" << json_bool(attachment.is_inline ? 1 : 0);
        output << "}";
    }
    output << "]}";
    return output.str();
}

std::string metric_bucket_ts(const std::string& received_at) {
    return received_at.substr(0, 19) + "Z";
}

}

BatchWriter::BatchWriter(std::filesystem::path storage_root,
                         std::filesystem::path database_path,
                         int busy_timeout_ms,
                         bool fsync_storage)
    : storage_root_(std::move(storage_root)),
      database_path_(std::move(database_path)),
      busy_timeout_ms_(busy_timeout_ms),
      fsync_storage_(fsync_storage) {}

std::filesystem::path BatchWriter::resolve_storage_path(const std::string& relative_path) const {
    std::filesystem::path relative(relative_path);
    if (relative.is_absolute()) {
        throw std::runtime_error("storage path must be relative");
    }
    const auto root = std::filesystem::weakly_canonical(storage_root_);
    const auto target = std::filesystem::weakly_canonical(root / relative);
    if (!path_is_at_or_inside_root(root, target)) {
        throw std::runtime_error("storage path escapes storage root");
    }
    return target;
}

void BatchWriter::write_file_atomic(const std::string& relative_path,
                                    const std::string& content) const {
    const auto target = resolve_storage_path(relative_path);
    ensure_private_directory_chain(storage_root_, target.parent_path());
    auto [part_fd, part] = create_temp_file(target);
    chmod_private(part, false);
    try {
        write_all(part_fd, part, content);
        if (fsync_storage_) {
            if (::fsync(part_fd.get()) != 0) {
                const int fsync_error = errno;
                throw_errno("fsync failed: " + part.string(), fsync_error);
            }
        }
        part_fd.close_or_throw("close failed: " + part.string());
        std::filesystem::rename(part, target);
    } catch (...) {
        std::error_code ec;
        std::filesystem::remove(part, ec);
        throw;
    }
    chmod_private(target, false);
    if (fsync_storage_) {
        fsync_directory_chain_to_filesystem_root(storage_root_, target.parent_path());
    }
}

void BatchWriter::write_parsed_artifacts(const MailJob& job, ParsedMail& parsed) const {
    prepare_parsed_artifact_metadata(job, parsed);
    if (parsed.text_body_path.has_value()) {
        write_file_atomic(*parsed.text_body_path, parsed.text_body);
    }
    if (parsed.html_body_path.has_value()) {
        write_file_atomic(*parsed.html_body_path, parsed.html_body);
    }
    for (ParsedAttachment& attachment : parsed.attachments) {
        write_file_atomic(attachment.storage_path, attachment.content);
    }
}

std::string BatchWriter::build_manifest(const MailJob& job,
                                        const ParsedMail* parsed,
                                        const ParseFailure* failure) const {
    std::ostringstream output;
    output << "{";
    output << "\"message_id\":\"" << json_escape(job.message_id) << "\",";
    output << "\"smtp_session_id\":\"" << json_escape(job.smtp_session_id) << "\",";
    output << "\"envelope_from\":\"" << json_escape(job.envelope_from) << "\",";
    output << "\"received_at\":\"" << json_escape(job.received_at) << "\",";
    output << "\"raw_path\":\"" << json_escape(job.raw_path) << "\",";
    output << "\"raw_sha256\":\"" << json_escape(job.raw_sha256) << "\",";
    output << "\"raw_size_bytes\":" << job.raw_content.size() << ",";
    output << "\"rcpt_tos\":[";
    for (std::size_t i = 0; i < job.recipients.size(); ++i) {
        if (i != 0) {
            output << ",";
        }
        output << "\"" << json_escape(job.recipients[i].rcpt_to) << "\"";
    }
    output << "],\"recipients\":[";
    for (std::size_t i = 0; i < job.recipients.size(); ++i) {
        const auto& recipient = job.recipients[i];
        if (i != 0) {
            output << ",";
        }
        output << "{";
        output << "\"rcpt_to\":\"" << json_escape(recipient.rcpt_to) << "\",";
        output << "\"domain_id\":" << recipient.match.domain_id << ",";
        output << "\"domain_ascii\":\"" << json_escape(recipient.match.domain_ascii) << "\",";
        output << "\"root_domain_ascii\":\"" << json_escape(recipient.match.root_domain_ascii) << "\",";
        output << "\"local_part_canonical\":\""
               << json_escape(recipient.match.local_part_canonical) << "\",";
        output << "\"address_canonical\":\"" << json_escape(recipient.match.address_canonical)
               << "\",";
        if (!recipient.domain_policy.has_value()) {
            throw std::runtime_error("recipient missing domain policy snapshot: " +
                                     recipient.rcpt_to);
        }
        output << "\"domain_policy\":" << build_domain_policy(*recipient.domain_policy);
        output << "}";
    }
    output << "]";
    if (parsed != nullptr) {
        output << ",\"parsed\":" << parsed_manifest_json(*parsed);
    } else if (failure != nullptr) {
        output << ",\"parsed\":{\"status\":\"failed\",\"parse_error\":\""
               << json_escape(failure->message) << "\"}";
    }
    output << "}";
    return output.str();
}

void BatchWriter::write_storage_artifacts(const std::vector<MailJob>& jobs) const {
    for (const MailJob& job : jobs) {
        write_file_atomic(job.manifest_path, build_manifest(job, nullptr, nullptr));
        write_file_atomic(job.raw_path, job.raw_content);
    }
}

void BatchWriter::write_storage_artifacts(
    const std::vector<MailJob>& jobs,
    std::vector<std::variant<ParsedMail, ParseFailure>>& parse_results) const {
    if (jobs.size() != parse_results.size()) {
        throw std::runtime_error("batch writer parse result count mismatch");
    }
    for (std::size_t index = 0; index < jobs.size(); ++index) {
        const MailJob& job = jobs[index];
        auto& result = parse_results[index];
        if (ParsedMail* parsed = std::get_if<ParsedMail>(&result)) {
            prepare_parsed_artifact_metadata(job, *parsed);
        }
        write_file_atomic(job.manifest_path, build_manifest(job, nullptr, nullptr));
        write_file_atomic(job.raw_path, job.raw_content);
        if (ParsedMail* parsed = std::get_if<ParsedMail>(&result)) {
            write_parsed_artifacts(job, *parsed);
        }
        write_file_atomic(job.manifest_path,
                          build_manifest(job, parsed_result(result), failure_result(result)));
    }
}

void BatchWriter::write_sqlite_records(
    const std::vector<MailJob>& jobs,
    const std::vector<std::variant<ParsedMail, ParseFailure>>& parse_results) const {
    if (jobs.empty()) {
        return;
    }
    if (jobs.size() != parse_results.size()) {
        throw std::runtime_error("batch writer parse result count mismatch");
    }

    SqliteDb db(database_path_, busy_timeout_ms_);
    db.exec("BEGIN IMMEDIATE");

    try {
        auto upsert_session = db.prepare(
            "INSERT INTO smtp_sessions (id, remote_ip, status, tls_used, connect_at, "
            "first_command_at, last_command_at, last_mail_from, bytes_received, message_count) "
            "VALUES (?, 'unknown', 'closed', 0, ?, ?, ?, ?, ?, 1) "
            "ON CONFLICT(id) DO UPDATE SET "
            "last_command_at = excluded.last_command_at, "
            "last_mail_from = excluded.last_mail_from, "
            "message_count = smtp_sessions.message_count + 1, "
            "bytes_received = smtp_sessions.bytes_received + excluded.bytes_received");
        auto insert_message = db.prepare(
            "INSERT INTO messages (id, smtp_session_id, raw_path, raw_sha256, raw_size_bytes, "
            "envelope_from, from_addr, received_at, indexed_at, parse_status, parse_error, "
            "message_id_header, subject, from_name, reply_to, date_header, "
            "has_text, has_html, has_attachments, attachment_count, "
            "text_preview, text_body_path, html_body_path, headers_json, verification_code) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)");
        auto upsert_mailbox = db.prepare(
            "INSERT INTO mailboxes (domain_id, local_part_canonical, rcpt_domain_ascii, "
            "address_canonical, address_display, first_seen_at, last_seen_at, latest_message_at, "
            "message_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1) "
            "ON CONFLICT(address_canonical) DO UPDATE SET "
            "last_seen_at = excluded.last_seen_at, "
            "latest_message_at = excluded.latest_message_at, "
            "message_count = mailboxes.message_count + 1");
        auto select_mailbox =
            db.prepare("SELECT id FROM mailboxes WHERE address_canonical = ?");
        auto insert_delivery = db.prepare(
            "INSERT INTO message_deliveries (id, message_id, mailbox_id, rcpt_to, delivered_at) "
            "VALUES (?, ?, ?, ?, ?)");
        auto insert_attachment = db.prepare(
            "INSERT INTO attachments (id, message_id, part_index, filename, safe_filename, "
            "content_type, content_disposition, content_id, storage_path, sha256, size_bytes, "
            "is_inline, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)");
        auto upsert_metric = db.prepare(
            "INSERT INTO mail_metric_buckets (bucket_ts, deliveries, parse_failures) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(bucket_ts) DO UPDATE SET "
            "deliveries = mail_metric_buckets.deliveries + excluded.deliveries, "
            "parse_failures = mail_metric_buckets.parse_failures + excluded.parse_failures");

        for (std::size_t job_index = 0; job_index < jobs.size(); ++job_index) {
            const MailJob& job = jobs[job_index];
            const auto& parse_result = parse_results[job_index];
            const ParsedMail* parsed = parsed_result(parse_result);
            const ParseFailure* failure = failure_result(parse_result);
            const auto raw_size = static_cast<sqlite3_int64>(job.raw_content.size());

            bind_text(upsert_session, 1, job.smtp_session_id, "bind smtp session id");
            bind_text(upsert_session, 2, job.received_at, "bind smtp connect time");
            bind_text(upsert_session, 3, job.received_at, "bind smtp first command time");
            bind_text(upsert_session, 4, job.received_at, "bind smtp last command time");
            bind_text(upsert_session, 5, job.envelope_from, "bind smtp last mail from");
            bind_int64(upsert_session, 6, raw_size, "bind smtp bytes received");
            upsert_session.step_done();
            upsert_session.reset();

            bind_text(insert_message, 1, job.message_id, "bind message id");
            bind_text(insert_message, 2, job.smtp_session_id, "bind message smtp session id");
            bind_text(insert_message, 3, job.raw_path, "bind message raw path");
            bind_text(insert_message, 4, job.raw_sha256, "bind message raw sha256");
            bind_int64(insert_message, 5, raw_size, "bind message raw size");
            bind_text(insert_message, 6, job.envelope_from, "bind message envelope from");
            bind_optional_text(insert_message,
                               7,
                               parsed == nullptr ? std::nullopt : parsed->from_addr,
                               "bind message from addr");
            bind_text(insert_message, 8, job.received_at, "bind message received at");
            bind_text(insert_message, 9, job.received_at, "bind message indexed at");
            bind_text(insert_message,
                      10,
                      parsed == nullptr ? "failed" : "parsed",
                      "bind message parse status");
            if (failure == nullptr) {
                bind_null(insert_message, 11, "bind message parse error");
            } else {
                bind_text(insert_message, 11, failure->message, "bind message parse error");
            }
            bind_optional_text(insert_message,
                               12,
                               parsed == nullptr ? std::nullopt : parsed->message_id_header,
                               "bind message id header");
            bind_optional_text(insert_message,
                               13,
                               parsed == nullptr ? std::nullopt : parsed->subject,
                               "bind message subject");
            bind_optional_text(insert_message,
                               14,
                               parsed == nullptr ? std::nullopt : parsed->from_name,
                               "bind message from name");
            bind_optional_text(insert_message,
                               15,
                               parsed == nullptr ? std::nullopt : parsed->reply_to,
                               "bind message reply to");
            bind_optional_text(insert_message,
                               16,
                               parsed == nullptr ? std::nullopt : parsed->date_header,
                               "bind message date header");
            bind_int64(insert_message,
                       17,
                       parsed != nullptr && parsed->has_text ? 1 : 0,
                       "bind message has text");
            bind_int64(insert_message,
                       18,
                       parsed != nullptr && parsed->has_html ? 1 : 0,
                       "bind message has html");
            bind_int64(insert_message,
                       19,
                       parsed != nullptr && parsed->has_attachments ? 1 : 0,
                       "bind message has attachments");
            bind_int64(insert_message,
                       20,
                       parsed == nullptr ? 0 : parsed->attachment_count,
                       "bind message attachment count");
            bind_optional_text(insert_message,
                               21,
                               parsed == nullptr ? std::nullopt : parsed->text_preview,
                               "bind message text preview");
            bind_optional_text(insert_message,
                               22,
                               parsed == nullptr ? std::nullopt : parsed->text_body_path,
                               "bind message text body path");
            bind_optional_text(insert_message,
                               23,
                               parsed == nullptr ? std::nullopt : parsed->html_body_path,
                               "bind message html body path");
            if (parsed == nullptr) {
                bind_null(insert_message, 24, "bind message headers json");
            } else {
                bind_text(insert_message, 24, parsed->headers_json, "bind message headers json");
            }
            bind_optional_text(insert_message,
                               25,
                               parsed == nullptr ? std::nullopt : parsed->verification_code,
                               "bind message verification code");
            insert_message.step_done();
            insert_message.reset();

            if (parsed != nullptr) {
                for (const ParsedAttachment& attachment : parsed->attachments) {
                    bind_text(insert_attachment,
                              1,
                              attachment.attachment_id,
                              "bind attachment id");
                    bind_text(insert_attachment, 2, job.message_id, "bind attachment message id");
                    bind_int64(insert_attachment,
                               3,
                               attachment.part_index,
                               "bind attachment part index");
                    bind_optional_text(insert_attachment,
                                       4,
                                       attachment.filename,
                                       "bind attachment filename");
                    bind_text(insert_attachment,
                              5,
                              attachment.safe_filename,
                              "bind attachment safe filename");
                    bind_text(insert_attachment,
                              6,
                              attachment.content_type,
                              "bind attachment content type");
                    bind_optional_text(insert_attachment,
                                       7,
                                       attachment.content_disposition,
                                       "bind attachment content disposition");
                    bind_optional_text(insert_attachment,
                                       8,
                                       attachment.content_id,
                                       "bind attachment content id");
                    bind_text(insert_attachment,
                              9,
                              attachment.storage_path,
                              "bind attachment storage path");
                    bind_text(insert_attachment, 10, attachment.sha256, "bind attachment sha256");
                    bind_int64(insert_attachment,
                               11,
                               static_cast<sqlite3_int64>(attachment.content.size()),
                               "bind attachment size");
                    bind_int64(insert_attachment,
                               12,
                               attachment.is_inline ? 1 : 0,
                               "bind attachment inline flag");
                    bind_text(insert_attachment, 13, job.received_at, "bind attachment created at");
                    insert_attachment.step_done();
                    insert_attachment.reset();
                }
            }

            for (const RecipientDelivery& recipient : job.recipients) {
                const DomainMatch& match = recipient.match;
                bind_int64(upsert_mailbox,
                           1,
                           static_cast<sqlite3_int64>(match.domain_id),
                           "bind mailbox domain id");
                bind_text(upsert_mailbox,
                          2,
                          match.local_part_canonical,
                          "bind mailbox local part");
                bind_text(upsert_mailbox, 3, match.domain_ascii, "bind mailbox domain ascii");
                bind_text(upsert_mailbox,
                          4,
                          match.address_canonical,
                          "bind mailbox address canonical");
                bind_text(upsert_mailbox,
                          5,
                          match.address_canonical,
                          "bind mailbox address display");
                bind_text(upsert_mailbox, 6, job.received_at, "bind mailbox first seen at");
                bind_text(upsert_mailbox, 7, job.received_at, "bind mailbox last seen at");
                bind_text(upsert_mailbox, 8, job.received_at, "bind mailbox latest message at");
                upsert_mailbox.step_done();
                upsert_mailbox.reset();

                bind_text(select_mailbox,
                          1,
                          match.address_canonical,
                          "bind mailbox id lookup address");
                if (!select_mailbox.step_row()) {
                    throw std::runtime_error("mailbox upsert did not produce a row: " +
                                             match.address_canonical);
                }
                const sqlite3_int64 mailbox_id = sqlite3_column_int64(select_mailbox.get(), 0);
                select_mailbox.reset();

                bind_text(insert_delivery, 1, recipient.delivery_id, "bind delivery id");
                bind_text(insert_delivery, 2, job.message_id, "bind delivery message id");
                bind_int64(insert_delivery, 3, mailbox_id, "bind delivery mailbox id");
                bind_text(insert_delivery, 4, recipient.rcpt_to, "bind delivery rcpt to");
                bind_text(insert_delivery, 5, job.received_at, "bind delivery delivered at");
                insert_delivery.step_done();
                insert_delivery.reset();
            }

            if (!job.recipients.empty()) {
                bind_text(upsert_metric, 1, metric_bucket_ts(job.received_at), "bind metric bucket");
                bind_int64(upsert_metric,
                           2,
                           static_cast<sqlite3_int64>(job.recipients.size()),
                           "bind metric deliveries");
                bind_int64(upsert_metric,
                           3,
                           failure == nullptr ? 0 : 1,
                           "bind metric parse failures");
                upsert_metric.step_done();
                upsert_metric.reset();
            }
        }

        db.exec("COMMIT");
    } catch (...) {
        try {
            db.exec("ROLLBACK");
        } catch (...) {
        }
        throw;
    }
}

void BatchWriter::write_batch(const std::vector<MailJob>& jobs) const {
    std::vector<std::variant<ParsedMail, ParseFailure>> parse_results;
    parse_results.reserve(jobs.size());
    for (const MailJob& job : jobs) {
        try {
            ParsedMail parsed = MimeParser().parse(job.raw_content);
            parsed.verification_code = extract_verification_code(parsed.subject.value_or(""),
                                                                 parsed.from_addr.value_or(""),
                                                                 parsed.text_body,
                                                                 parsed.html_body,
                                                                 parsed.text_preview.value_or(""));
            parse_results.emplace_back(std::move(parsed));
        } catch (const ParseFailure& failure) {
            parse_results.emplace_back(failure);
        }
    }
    write_storage_artifacts(jobs, parse_results);
    write_sqlite_records(jobs, parse_results);
}

}
