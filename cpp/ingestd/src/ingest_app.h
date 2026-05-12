#pragma once

#include "batch_writer.h"
#include "config.h"
#include "domain_cache.h"
#include "mail_queue.h"

#include <atomic>
#include <thread>

namespace rapid_inbox::ingestd {

class IngestApp {
public:
    explicit IngestApp(Config config);
    ~IngestApp();
    IngestApp(const IngestApp&) = delete;
    IngestApp& operator=(const IngestApp&) = delete;

    void start_writer();
    void stop_and_drain();
    MailQueue& queue() { return queue_; }
    DomainCache& domains() { return domains_; }

private:
    void writer_loop();

    Config config_;
    MailQueue queue_;
    DomainCache domains_;
    BatchWriter writer_;
    std::atomic<bool> running_{false};
    std::thread writer_thread_;
};

}
