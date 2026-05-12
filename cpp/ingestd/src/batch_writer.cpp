#include "batch_writer.h"

#include "json_util.h"
#include "sqlite_db.h"

#include <sqlite3.h>

#include <cerrno>
#include <cstdlib>
#include <fcntl.h>
#include <sstream>
#include <stdexcept>
#include <system_error>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>
#include <utility>

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

std::string BatchWriter::build_manifest(const MailJob& job) const {
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
    output << "]}";
    return output.str();
}

void BatchWriter::write_storage_artifacts(const std::vector<MailJob>& jobs) const {
    for (const MailJob& job : jobs) {
        write_file_atomic(job.manifest_path, build_manifest(job));
        write_file_atomic(job.raw_path, job.raw_content);
    }
}

void BatchWriter::write_sqlite_records(const std::vector<MailJob>& jobs) const {
    if (jobs.empty()) {
        return;
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
            "envelope_from, from_addr, received_at, parse_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')");
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
        auto upsert_metric = db.prepare(
            "INSERT INTO mail_metric_buckets (bucket_ts, deliveries, parse_failures) "
            "VALUES (?, ?, 0) "
            "ON CONFLICT(bucket_ts) DO UPDATE SET "
            "deliveries = mail_metric_buckets.deliveries + excluded.deliveries");

        for (const MailJob& job : jobs) {
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
            bind_text(insert_message, 7, job.envelope_from, "bind message from addr");
            bind_text(insert_message, 8, job.received_at, "bind message received at");
            insert_message.step_done();
            insert_message.reset();

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
    write_storage_artifacts(jobs);
    write_sqlite_records(jobs);
}

}
