#pragma once

#include <filesystem>
#include <string>

namespace rapid_inbox::ingestd {

struct Config {
    std::filesystem::path base_dir;
    std::filesystem::path storage_root;
    std::filesystem::path database_path;
    std::string host = "127.0.0.1";
    int port = 8000;
    std::string smtp_host = "127.0.0.1";
    int smtp_port = 25;
    int max_message_size_bytes = 52428800;
    int max_recipients_per_message = 20;
    int smtp_idle_timeout_seconds = 30;
    int ingest_queue_max_messages = 10000;
    int ingest_batch_max_messages = 250;
    int ingest_flush_interval_ms = 250;
    int ingest_sqlite_busy_timeout_ms = 5000;
    bool ingest_storage_fsync = false;

    static Config load(const std::filesystem::path& base_dir);
};

}
