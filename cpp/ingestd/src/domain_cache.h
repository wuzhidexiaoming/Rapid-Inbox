#pragma once

#include "domain_matcher.h"

#include <filesystem>
#include <mutex>
#include <optional>
#include <string>

namespace rapid_inbox::ingestd {

class DomainCache {
public:
    DomainCache(std::filesystem::path database_path, int busy_timeout_ms);

    DomainCache(const DomainCache&) = delete;
    DomainCache& operator=(const DomainCache&) = delete;

    void reload();
    std::optional<DomainMatch> match_address(const std::string& address) const;

private:
    std::filesystem::path database_path_;
    int busy_timeout_ms_;
    mutable std::mutex mutex_;
    DomainMatcher matcher_;
};

}  // namespace rapid_inbox::ingestd
