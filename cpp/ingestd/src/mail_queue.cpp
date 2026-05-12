#include "mail_queue.h"

#include <utility>

namespace rapid_inbox::ingestd {

MailQueue::MailQueue(std::size_t capacity) : capacity_(capacity) {}

bool MailQueue::try_push(MailJob job) {
    {
        std::lock_guard<std::mutex> guard(mutex_);
        if (closed_ || queue_.size() >= capacity_) {
            return false;
        }
        queue_.push(std::move(job));
    }
    changed_.notify_one();
    return true;
}

std::vector<MailJob> MailQueue::pop_batch(std::size_t max_items,
                                          std::chrono::milliseconds wait_for) {
    std::unique_lock<std::mutex> lock(mutex_);
    changed_.wait_for(lock, wait_for, [&] { return closed_ || !queue_.empty(); });
    std::vector<MailJob> batch;
    while (!queue_.empty() && batch.size() < max_items) {
        batch.push_back(std::move(queue_.front()));
        queue_.pop();
    }
    return batch;
}

void MailQueue::close() {
    {
        std::lock_guard<std::mutex> guard(mutex_);
        closed_ = true;
    }
    changed_.notify_all();
}

bool MailQueue::closed() const {
    std::lock_guard<std::mutex> guard(mutex_);
    return closed_;
}

std::size_t MailQueue::size() const {
    std::lock_guard<std::mutex> guard(mutex_);
    return queue_.size();
}

}
