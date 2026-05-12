#include "smtp_session.h"

#include "id.h"
#include "sha256.h"
#include "storage_path.h"
#include "time_utils.h"

#include <algorithm>
#include <cctype>
#include <utility>

namespace rapid_inbox::ingestd {
namespace {

std::string upper_ascii(std::string value) {
    for (char& ch : value) {
        ch = static_cast<char>(std::toupper(static_cast<unsigned char>(ch)));
    }
    return value;
}

bool starts_with_ci(const std::string& value, const std::string& prefix) {
    return upper_ascii(value.substr(0, prefix.size())) == upper_ascii(prefix);
}

bool matches_command_ci(const std::string& value, const std::string& command) {
    if (!starts_with_ci(value, command)) {
        return false;
    }
    return value.size() == command.size() ||
           std::isspace(static_cast<unsigned char>(value[command.size()]));
}

bool matches_no_arg_command_ci(const std::string& value, const std::string& command) {
    if (!starts_with_ci(value, command)) {
        return false;
    }
    return std::all_of(value.begin() + static_cast<std::string::difference_type>(command.size()),
                       value.end(),
                       [](unsigned char ch) { return std::isspace(ch); });
}

}

SmtpSession::SmtpSession(const DomainMatcher& matcher,
                         MailQueue& queue,
                         int max_recipients,
                         std::size_t max_message_size_bytes)
    : SmtpSession(matcher,
                  queue,
                  max_recipients,
                  max_message_size_bytes,
                  std::unordered_map<int, DomainPolicySnapshot>{}) {}

SmtpSession::SmtpSession(const DomainMatcher& matcher,
                         MailQueue& queue,
                         int max_recipients,
                         std::size_t max_message_size_bytes,
                         std::unordered_map<int, DomainPolicySnapshot> domain_policies)
    : matcher_(matcher),
      queue_(queue),
      max_recipients_(max_recipients),
      max_message_size_bytes_(max_message_size_bytes),
      domain_policies_(std::move(domain_policies)),
      session_id_(make_prefixed_id("smtp_")) {}

std::string SmtpSession::greeting() const {
    return "220 rapid-inbox-ingestd";
}

std::string SmtpSession::handle_line(const std::string& line) {
    if (in_data_) {
        if (line == ".") {
            if (data_too_large_) {
                clear_transaction_state();
                return "";
            }
            return finish_data();
        }
        if (data_too_large_) {
            return "";
        }
        std::string content_line = line;
        if (!content_line.empty() && content_line[0] == '.') {
            content_line.erase(content_line.begin());
        }
        data_ += content_line;
        data_ += "\r\n";
        if (data_.size() > max_message_size_bytes_) {
            data_too_large_ = true;
            data_.clear();
            return "552 message too large";
        }
        return "";
    }
    return handle_command(line);
}

std::string SmtpSession::handle_command(const std::string& line) {
    if (matches_command_ci(line, "EHLO") || matches_command_ci(line, "HELO")) {
        return "250 rapid-inbox-ingestd";
    }
    if (matches_no_arg_command_ci(line, "QUIT")) {
        return "221 2.0.0 Bye";
    }
    if (matches_no_arg_command_ci(line, "RSET")) {
        clear_transaction_state();
        return "250 OK";
    }
    if (starts_with_ci(line, "MAIL FROM:")) {
        auto value = extract_path_argument(line);
        if (!value.has_value()) {
            return "501 invalid sender";
        }
        mail_from_ = *value;
        recipients_.clear();
        data_.clear();
        data_too_large_ = false;
        return "250 OK";
    }
    if (starts_with_ci(line, "RCPT TO:")) {
        if (mail_from_.empty()) {
            return "503 need MAIL FROM first";
        }
        if (static_cast<int>(recipients_.size()) >= max_recipients_) {
            return "552 too many recipients";
        }
        auto value = extract_path_argument(line);
        if (!value.has_value()) {
            return "501 invalid recipient";
        }
        auto match = matcher_.match_address(*value);
        if (!match.has_value()) {
            return "550 domain not allowed";
        }
        std::optional<DomainPolicySnapshot> domain_policy;
        const auto policy = domain_policies_.find(match->domain_id);
        if (policy != domain_policies_.end()) {
            domain_policy = policy->second;
        }
        recipients_.push_back(
            RecipientDelivery{make_prefixed_id("dlv_"), *value, *match, std::move(domain_policy)});
        return "250 OK";
    }
    if (matches_no_arg_command_ci(line, "DATA")) {
        if (recipients_.empty()) {
            return "554 no valid recipients";
        }
        in_data_ = true;
        data_too_large_ = false;
        data_.clear();
        return "354 End data with <CR><LF>.<CR><LF>";
    }
    return "502 command not implemented";
}

std::string SmtpSession::finish_data() {
    in_data_ = false;
    const std::string received_at = utc_now();
    MailJob job;
    job.smtp_session_id = session_id_;
    job.message_id = make_prefixed_id("msg_");
    job.envelope_from = mail_from_;
    job.received_at = received_at;
    job.raw_content = data_;
    job.raw_sha256 = sha256_hex(data_);
    job.raw_path = raw_message_path(job.message_id, received_at);
    job.manifest_path = manifest_path(job.message_id, received_at);
    job.recipients = recipients_;
    data_.clear();
    if (!queue_.try_push(job)) {
        clear_transaction_state();
        return "451 temporary queue full";
    }
    clear_transaction_state();
    return "250 queued as " + job.message_id;
}

void SmtpSession::clear_transaction_state() {
    mail_from_.clear();
    recipients_.clear();
    data_.clear();
    in_data_ = false;
    data_too_large_ = false;
}

std::optional<std::string> SmtpSession::extract_path_argument(const std::string& line) const {
    const auto colon = line.find(':');
    if (colon == std::string::npos) {
        return std::nullopt;
    }
    std::string value = line.substr(colon + 1);
    value.erase(value.begin(), std::find_if(value.begin(), value.end(), [](unsigned char ch) {
        return !std::isspace(ch);
    }));
    value.erase(std::find_if(value.rbegin(), value.rend(), [](unsigned char ch) {
        return !std::isspace(ch);
    }).base(),
                value.end());
    if (value.empty()) {
        return std::nullopt;
    }
    if (value.front() == '<' || value.back() == '>') {
        if (value.size() < 2 || value.front() != '<' || value.back() != '>') {
            return std::nullopt;
        }
        value = value.substr(1, value.size() - 2);
    }
    return value.empty() ? std::nullopt : std::optional<std::string>(value);
}

}
