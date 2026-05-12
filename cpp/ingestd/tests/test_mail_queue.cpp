#include "../src/mail_queue.h"

#include <chrono>
#include <future>
#include <thread>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>

namespace test {
void check(bool condition, const std::string& message);
}

static_assert(
    std::is_same_v<decltype(&rapid_inbox::ingestd::MailQueue::try_push),
                   bool (rapid_inbox::ingestd::MailQueue::*)(rapid_inbox::ingestd::MailJob)>,
    "MailQueue::try_push should accept MailJob by value so callers can move large payloads");

void test_mail_queue_capacity_and_close() {
    rapid_inbox::ingestd::MailQueue queue(1);
    rapid_inbox::ingestd::MailJob job;
    job.message_id = "msg_1";
    test::check(queue.try_push(job), "first push fits");
    test::check(!queue.try_push(job), "second push rejected by capacity");
    auto popped = queue.pop_batch(10, std::chrono::milliseconds(1));
    test::check(popped.size() == 1, "pop one item");
    test::check(popped[0].message_id == "msg_1", "popped message id");
    queue.close();
    test::check(!queue.try_push(job), "push rejected after close");
    auto empty = queue.pop_batch(10, std::chrono::milliseconds(1));
    test::check(empty.empty(), "closed empty queue returns empty batch");
}

void test_mail_queue_try_push_accepts_lvalues_and_rvalues() {
    rapid_inbox::ingestd::MailQueue queue(2);
    rapid_inbox::ingestd::MailJob first;
    first.message_id = "msg_lvalue";
    rapid_inbox::ingestd::MailJob second;
    second.message_id = "msg_rvalue";

    test::check(queue.try_push(first), "lvalue push works");
    test::check(queue.try_push(std::move(second)), "rvalue push works");

    auto popped = queue.pop_batch(10, std::chrono::milliseconds(1));
    test::check(popped.size() == 2, "lvalue and rvalue pushes pop together");
    test::check(popped[0].message_id == "msg_lvalue", "lvalue message id preserved");
    test::check(popped[1].message_id == "msg_rvalue", "rvalue message id preserved");
}

void test_mail_queue_close_wakes_waiting_pop_batch() {
    rapid_inbox::ingestd::MailQueue queue(1);
    std::promise<void> consumer_started;
    auto started = consumer_started.get_future();
    std::promise<std::vector<rapid_inbox::ingestd::MailJob>> result;
    auto result_future = result.get_future();

    std::thread consumer([&] {
        consumer_started.set_value();
        result.set_value(queue.pop_batch(10, std::chrono::seconds(5)));
    });

    started.wait();
    const auto wait_status = result_future.wait_for(std::chrono::milliseconds(50));
    queue.close();

    auto popped = result_future.get();
    consumer.join();
    test::check(wait_status == std::future_status::timeout, "consumer waits before close");
    test::check(popped.empty(), "close wakes waiting consumer with empty batch");
}
