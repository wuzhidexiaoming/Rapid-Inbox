#pragma once

#include "domain_matcher.h"
#include "mail_queue.h"

#include <cstddef>
#include <optional>
#include <string>
#include <unordered_map>
#include <vector>

namespace rapid_inbox::ingestd {

class SmtpSession {
public:
    SmtpSession(const DomainMatcher& matcher,
                MailQueue& queue,
                int max_recipients,
                std::size_t max_message_size_bytes);
    SmtpSession(const DomainMatcher& matcher,
                MailQueue& queue,
                int max_recipients,
                std::size_t max_message_size_bytes,
                std::unordered_map<int, DomainPolicySnapshot> domain_policies);

    std::string greeting() const;
    std::string handle_line(const std::string& line);

private:
    std::string handle_command(const std::string& line);
    std::string finish_data();
    void clear_transaction_state();
    std::optional<std::string> extract_path_argument(const std::string& line) const;

    const DomainMatcher& matcher_;
    MailQueue& queue_;
    int max_recipients_;
    std::size_t max_message_size_bytes_;
    std::unordered_map<int, DomainPolicySnapshot> domain_policies_;
    std::string session_id_;
    std::string mail_from_;
    std::vector<RecipientDelivery> recipients_;
    bool in_data_ = false;
    bool data_too_large_ = false;
    std::string data_;
};

}
