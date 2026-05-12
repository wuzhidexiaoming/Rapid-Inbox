#include "../src/domain_cache.h"
#include "../src/sqlite_db.h"

#include <filesystem>
#include <stdexcept>
#include <string>

namespace test {
inline void check(bool condition, const std::string& message) {
    if (!condition) {
        throw std::runtime_error(message);
    }
}
}

namespace {

namespace fs = std::filesystem;

using rapid_inbox::ingestd::DomainCache;
using rapid_inbox::ingestd::DomainMatch;
using rapid_inbox::ingestd::SqliteDb;

void remove_db_files(const fs::path& db_path) {
    fs::remove(db_path);
    fs::remove(db_path.string() + "-wal");
    fs::remove(db_path.string() + "-shm");
}

fs::path fresh_db_path(const std::string& filename) {
    const fs::path db_path = fs::temp_directory_path() / filename;
    remove_db_files(db_path);
    return db_path;
}

void create_domains_table(SqliteDb& db) {
    db.exec("CREATE TABLE domains (id INTEGER PRIMARY KEY, root_domain_ascii TEXT, "
            "accept_exact INTEGER, accept_subdomains INTEGER, plus_addressing_mode TEXT, "
            "local_part_case_sensitive INTEGER, is_active INTEGER)");
}

DomainMatch require_match(DomainCache& cache,
                          const std::string& address,
                          const std::string& message) {
    auto match = cache.match_address(address);
    test::check(match.has_value(), message);
    return *match;
}

}  // namespace

void test_domain_cache_loads_active_rules() {
    const fs::path db_path = fresh_db_path("rapid-inbox-domain-cache.sqlite");
    SqliteDb db(db_path, 5000);
    create_domains_table(db);
    db.exec("INSERT INTO domains VALUES (1, 'adb.com', 1, 1, 'keep', 0, 1)");
    db.exec("INSERT INTO domains VALUES (2, 'disabled.com', 1, 1, 'keep', 0, 0)");

    DomainCache cache(db_path, 5000);
    cache.reload();

    auto allowed = cache.match_address("A@adb.com");
    test::check(allowed.has_value(), "active domain matches");
    test::check(allowed->address_canonical == "a@adb.com", "active domain canonical");
    auto disabled = cache.match_address("a@disabled.com");
    test::check(!disabled.has_value(), "inactive domain skipped");
}

void test_domain_cache_respects_matcher_flags() {
    const fs::path db_path = fresh_db_path("rapid-inbox-domain-cache-flags.sqlite");
    SqliteDb db(db_path, 5000);
    create_domains_table(db);
    db.exec("INSERT INTO domains VALUES (1, 'exact-off.test', 0, 1, 'keep', 0, 1)");
    db.exec("INSERT INTO domains VALUES (2, 'sub-off.test', 1, 0, 'keep', 0, 1)");

    DomainCache cache(db_path, 5000);
    cache.reload();

    test::check(!cache.match_address("a@exact-off.test").has_value(),
                "accept_exact false rejects exact domain");
    test::check(cache.match_address("a@sub.exact-off.test").has_value(),
                "accept_subdomains true still accepts subdomain");
    test::check(cache.match_address("a@sub-off.test").has_value(),
                "accept_exact true accepts exact domain");
    test::check(!cache.match_address("a@sub.sub-off.test").has_value(),
                "accept_subdomains false rejects subdomain");
}

void test_domain_cache_loads_plus_and_case_modes() {
    const fs::path db_path = fresh_db_path("rapid-inbox-domain-cache-plus-case.sqlite");
    SqliteDb db(db_path, 5000);
    create_domains_table(db);
    db.exec("INSERT INTO domains VALUES (1, 'strip-case.test', 1, 1, 'strip', 1, 1)");

    DomainCache cache(db_path, 5000);
    cache.reload();

    const DomainMatch match =
        require_match(cache, "User+Tag@strip-case.test", "strip/case domain matches");
    test::check(match.local_part == "User+Tag", "original local part preserved");
    test::check(match.local_part_canonical == "User", "plus tag stripped with case preserved");
    test::check(match.address_canonical == "User@strip-case.test",
                "canonical address strips plus and preserves case");
}

void test_domain_cache_reload_sees_rule_changes() {
    const fs::path db_path = fresh_db_path("rapid-inbox-domain-cache-reload.sqlite");
    SqliteDb db(db_path, 5000);
    create_domains_table(db);
    db.exec("INSERT INTO domains VALUES (1, 'reload-active.test', 1, 1, 'keep', 0, 1)");
    db.exec("INSERT INTO domains VALUES (2, 'reload-toggle.test', 1, 1, 'strip', 1, 0)");

    DomainCache cache(db_path, 5000);
    cache.reload();
    const DomainMatch initial_match =
        require_match(cache, "User+Tag@reload-active.test", "initial active rule matches");
    test::check(initial_match.address_canonical == "user+tag@reload-active.test",
                "initial active rule uses first loaded matcher");
    test::check(!cache.match_address("User+Tag@reload-toggle.test").has_value(),
                "initial inactive rule is absent");

    db.exec("UPDATE domains SET is_active = 1 WHERE id = 2");
    cache.reload();

    const DomainMatch match =
        require_match(cache, "User+Tag@reload-toggle.test", "reload loads newly active rule");
    test::check(match.address_canonical == "User@reload-toggle.test",
                "reload sees updated rule flags");
}
