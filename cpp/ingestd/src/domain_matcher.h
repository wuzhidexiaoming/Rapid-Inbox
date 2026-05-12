#pragma once

#include <optional>
#include <string>
#include <vector>

namespace rapid_inbox::ingestd {

struct DomainRule {
    int domain_id;
    std::string root_domain_ascii;
    bool accept_exact;
    bool accept_subdomains;
    std::string plus_addressing_mode;
    bool local_part_case_sensitive;
};

struct DomainMatch {
    int domain_id;
    std::string domain_ascii;
    std::string root_domain_ascii;
    std::string local_part;
    std::string local_part_canonical;
    std::string address_canonical;
};

std::string normalize_domain(std::string domain);

class DomainMatcher {
public:
    explicit DomainMatcher(std::vector<DomainRule> rules);
    std::optional<DomainMatch> match_address(const std::string& address) const;

private:
    std::vector<DomainRule> rules_;
};

}
