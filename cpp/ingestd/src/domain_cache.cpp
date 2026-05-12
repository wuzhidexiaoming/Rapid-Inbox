#include "domain_cache.h"

#include "sqlite_db.h"

#include <sqlite3.h>

#include <string>
#include <utility>
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

}  // namespace

DomainCache::DomainCache(std::filesystem::path database_path, int busy_timeout_ms)
    : database_path_(std::move(database_path)),
      busy_timeout_ms_(busy_timeout_ms),
      matcher_(std::vector<DomainRule>{}) {}

void DomainCache::reload() {
    SqliteDb db(database_path_, busy_timeout_ms_);
    Statement statement = db.prepare(R"SQL(
SELECT id, root_domain_ascii, accept_exact, accept_subdomains, plus_addressing_mode, local_part_case_sensitive
FROM domains
WHERE is_active = 1
)SQL");

    std::vector<DomainRule> rules;
    while (statement.step_row()) {
        sqlite3_stmt* row = statement.get();
        std::string root_domain_ascii = column_text_or_default(row, 1, "");
        if (root_domain_ascii.empty()) {
            continue;
        }

        std::string plus_addressing_mode = column_text_or_default(row, 4, "keep");
        if (plus_addressing_mode.empty()) {
            plus_addressing_mode = "keep";
        }

        rules.push_back(DomainRule{
            .domain_id = sqlite3_column_int(row, 0),
            .root_domain_ascii = std::move(root_domain_ascii),
            .accept_exact = sqlite3_column_int(row, 2) != 0,
            .accept_subdomains = sqlite3_column_int(row, 3) != 0,
            .plus_addressing_mode = std::move(plus_addressing_mode),
            .local_part_case_sensitive = sqlite3_column_int(row, 5) != 0,
        });
    }

    DomainMatcher next_matcher(std::move(rules));
    const std::lock_guard lock(mutex_);
    matcher_ = std::move(next_matcher);
}

std::optional<DomainMatch> DomainCache::match_address(const std::string& address) const {
    const std::lock_guard lock(mutex_);
    return matcher_.match_address(address);
}

}  // namespace rapid_inbox::ingestd
