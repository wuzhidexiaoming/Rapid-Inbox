#include "domain_cache.h"

#include "sqlite_db.h"

#include <sqlite3.h>

#include <cstdint>
#include <string>
#include <utility>
#include <unordered_map>
#include <vector>

namespace rapid_inbox::ingestd {
namespace {

std::string column_text_or_default(sqlite3_stmt* statement,
                                   int column,
                                   const std::string& fallback) {
    if (sqlite3_column_type(statement, column) == SQLITE_NULL) {
        return fallback;
    }
    const unsigned char* text = sqlite3_column_text(statement, column);
    if (text == nullptr) {
        return fallback;
    }
    const int bytes = sqlite3_column_bytes(statement, column);
    return std::string(reinterpret_cast<const char*>(text), static_cast<std::size_t>(bytes));
}

int column_int_or_default(sqlite3_stmt* statement, int column, int fallback) {
    if (sqlite3_column_type(statement, column) == SQLITE_NULL) {
        return fallback;
    }
    return sqlite3_column_int(statement, column);
}

std::int64_t column_int64_or_default(sqlite3_stmt* statement,
                                     int column,
                                     std::int64_t fallback) {
    if (sqlite3_column_type(statement, column) == SQLITE_NULL) {
        return fallback;
    }
    return sqlite3_column_int64(statement, column);
}

}  // namespace

DomainCache::DomainCache(std::filesystem::path database_path, int busy_timeout_ms)
    : database_path_(std::move(database_path)),
      busy_timeout_ms_(busy_timeout_ms),
      matcher_(std::vector<DomainRule>{}),
      domain_policies_() {}

void DomainCache::reload() {
    SqliteDb db(database_path_, busy_timeout_ms_);
    Statement statement = db.prepare(R"SQL(
SELECT id,
       root_domain_ascii,
       root_domain_unicode,
       accept_exact,
       accept_subdomains,
       public_web_enabled,
       public_api_enabled,
       is_active,
       is_hidden,
       plus_addressing_mode,
       local_part_case_sensitive,
       max_message_size_bytes,
       retention_days,
       dns_status
FROM domains
WHERE is_active = 1
)SQL");

    std::vector<DomainRule> rules;
    std::unordered_map<int, DomainPolicySnapshot> domain_policies;
    while (statement.step_row()) {
        sqlite3_stmt* row = statement.get();
        std::string root_domain_ascii = column_text_or_default(row, 1, "");
        if (root_domain_ascii.empty()) {
            continue;
        }

        const int domain_id = sqlite3_column_int(row, 0);
        std::string root_domain_unicode = column_text_or_default(row, 2, root_domain_ascii);
        if (root_domain_unicode.empty()) {
            root_domain_unicode = root_domain_ascii;
        }
        std::string plus_addressing_mode = column_text_or_default(row, 9, "keep");
        if (plus_addressing_mode.empty()) {
            plus_addressing_mode = "keep";
        }
        std::string dns_status = column_text_or_default(row, 13, "unknown");
        if (dns_status.empty()) {
            dns_status = "unknown";
        }

        domain_policies.emplace(domain_id,
                                DomainPolicySnapshot{
                                    .root_domain_unicode = std::move(root_domain_unicode),
                                    .accept_exact = column_int_or_default(row, 3, 1) != 0,
                                    .accept_subdomains = column_int_or_default(row, 4, 1) != 0,
                                    .public_web_enabled = column_int_or_default(row, 5, 1) != 0,
                                    .public_api_enabled = column_int_or_default(row, 6, 1) != 0,
                                    .is_active = column_int_or_default(row, 7, 1) != 0,
                                    .is_hidden = column_int_or_default(row, 8, 0) != 0,
                                    .plus_addressing_mode = plus_addressing_mode,
                                    .local_part_case_sensitive =
                                        column_int_or_default(row, 10, 0) != 0,
                                    .max_message_size_bytes =
                                        column_int64_or_default(row, 11, 52428800),
                                    .retention_days = sqlite3_column_type(row, 12) == SQLITE_NULL
                                                           ? std::optional<int>{}
                                                           : std::optional<int>{
                                                                 sqlite3_column_int(row, 12)},
                                    .dns_status = std::move(dns_status),
                                });

        rules.push_back(DomainRule{
            .domain_id = domain_id,
            .root_domain_ascii = std::move(root_domain_ascii),
            .accept_exact = column_int_or_default(row, 3, 1) != 0,
            .accept_subdomains = column_int_or_default(row, 4, 1) != 0,
            .plus_addressing_mode = std::move(plus_addressing_mode),
            .local_part_case_sensitive = column_int_or_default(row, 10, 0) != 0,
        });
    }

    DomainMatcher next_matcher(std::move(rules));
    const std::lock_guard lock(mutex_);
    matcher_ = std::move(next_matcher);
    domain_policies_ = std::move(domain_policies);
}

std::optional<DomainMatch> DomainCache::match_address(const std::string& address) const {
    const std::lock_guard lock(mutex_);
    return matcher_.match_address(address);
}

DomainRulesSnapshot DomainCache::snapshot_rules() const {
    const std::lock_guard lock(mutex_);
    return DomainRulesSnapshot{matcher_, domain_policies_};
}

DomainMatcher DomainCache::snapshot_matcher() const {
    const std::lock_guard lock(mutex_);
    return matcher_;
}

std::unordered_map<int, DomainPolicySnapshot> DomainCache::snapshot_policies() const {
    const std::lock_guard lock(mutex_);
    return domain_policies_;
}

}  // namespace rapid_inbox::ingestd
