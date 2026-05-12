#pragma once

#include "domain_matcher.h"

#include <string>
#include <vector>

namespace rapid_inbox::ingestd {

struct RecipientDelivery {
    std::string delivery_id;
    std::string rcpt_to;
    DomainMatch match;
};

struct MailJob {
    std::string smtp_session_id;
    std::string message_id;
    std::string envelope_from;
    std::string received_at;
    std::string raw_path;
    std::string manifest_path;
    std::string raw_sha256;
    std::string raw_content;
    std::vector<RecipientDelivery> recipients;
};

}
