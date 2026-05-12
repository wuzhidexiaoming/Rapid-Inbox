#pragma once

#include "mail_job.h"

#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <mutex>
#include <queue>
#include <vector>

namespace rapid_inbox::ingestd {

class MailQueue {
public:
    explicit MailQueue(std::size_t capacity);
    bool try_push(MailJob job);
    std::vector<MailJob> pop_batch(std::size_t max_items, std::chrono::milliseconds wait_for);
    void close();
    bool closed() const;
    std::size_t size() const;

private:
    std::size_t capacity_;
    mutable std::mutex mutex_;
    std::condition_variable changed_;
    std::queue<MailJob> queue_;
    bool closed_ = false;
};

}
