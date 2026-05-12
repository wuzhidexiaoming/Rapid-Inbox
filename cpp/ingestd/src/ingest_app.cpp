#include "ingest_app.h"

#include <chrono>
#include <exception>
#include <iostream>
#include <thread>
#include <utility>

namespace rapid_inbox::ingestd {

IngestApp::IngestApp(Config config)
    : config_(std::move(config)),
      queue_(static_cast<std::size_t>(config_.ingest_queue_max_messages)),
      domains_(config_.database_path, config_.ingest_sqlite_busy_timeout_ms),
      writer_(config_.storage_root,
              config_.database_path,
              config_.ingest_sqlite_busy_timeout_ms,
              config_.ingest_storage_fsync) {}

IngestApp::~IngestApp() {
    stop_and_drain();
}

void IngestApp::start_writer() {
    domains_.reload();
    running_ = true;
    writer_thread_ = std::thread([this] { writer_loop(); });
}

void IngestApp::stop_and_drain() {
    queue_.close();
    running_ = false;
    if (writer_thread_.joinable()) {
        writer_thread_.join();
    }
}

void IngestApp::writer_loop() {
    while (running_ || queue_.size() > 0) {
        auto batch = queue_.pop_batch(static_cast<std::size_t>(config_.ingest_batch_max_messages),
                                      std::chrono::milliseconds(config_.ingest_flush_interval_ms));
        if (batch.empty()) {
            continue;
        }
        bool written = false;
        while (!written) {
            try {
                writer_.write_batch(batch);
                written = true;
            } catch (const std::exception& exc) {
                std::cerr << "ingestd writer retry after error: " << exc.what() << "\n";
                std::this_thread::sleep_for(std::chrono::milliseconds(250));
            }
        }
    }
}

}
