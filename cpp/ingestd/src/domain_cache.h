#pragma once

#include "domain_matcher.h"
#include "mail_job.h"

#include <filesystem>
#include <mutex>
#include <optional>
#include <string>
#include <unordered_map>

namespace rapid_inbox::ingestd {

struct DomainRulesSnapshot {
    DomainMatcher matcher;
    std::unordered_map<int, DomainPolicySnapshot> policies;
};

class DomainCache {
public:
    DomainCache(std::filesystem::path database_path, int busy_timeout_ms);

    DomainCache(const DomainCache&) = delete;
    DomainCache& operator=(const DomainCache&) = delete;

    void reload();
    std::optional<DomainMatch> match_address(const std::string& address) const;
    DomainRulesSnapshot snapshot_rules() const;
    DomainMatcher snapshot_matcher() const;
    std::unordered_map<int, DomainPolicySnapshot> snapshot_policies() const;

private:
    std::filesystem::path database_path_;
    int busy_timeout_ms_;
    mutable std::mutex mutex_;
    DomainMatcher matcher_;
    std::unordered_map<int, DomainPolicySnapshot> domain_policies_;
};

}  // namespace rapid_inbox::ingestd
