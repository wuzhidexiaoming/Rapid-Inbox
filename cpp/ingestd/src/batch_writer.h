#pragma once

#include "mail_job.h"

#include <filesystem>
#include <string>
#include <vector>

namespace rapid_inbox::ingestd {

class BatchWriter {
public:
    BatchWriter(std::filesystem::path storage_root,
                std::filesystem::path database_path,
                int busy_timeout_ms,
                bool fsync_storage);

    void write_storage_artifacts(const std::vector<MailJob>& jobs) const;
    void write_sqlite_records(const std::vector<MailJob>& jobs) const;
    void write_batch(const std::vector<MailJob>& jobs) const;

private:
    std::filesystem::path resolve_storage_path(const std::string& relative_path) const;
    void write_file_atomic(const std::string& relative_path, const std::string& content) const;
    std::string build_manifest(const MailJob& job) const;

    std::filesystem::path storage_root_;
    std::filesystem::path database_path_;
    int busy_timeout_ms_;
    bool fsync_storage_;
};

}
