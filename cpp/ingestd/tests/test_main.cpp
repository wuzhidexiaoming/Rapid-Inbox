#include <cstdlib>
#include <exception>
#include <iostream>
#include <stdexcept>
#include <string>

namespace test {
inline void check(bool condition, const std::string& message) {
    if (!condition) {
        throw std::runtime_error(message);
    }
}
}

void test_config_defaults();
void test_config_dotenv_and_environment_override();
void test_time_and_path_parts();
void test_ids_have_expected_prefixes();
void test_sha256_known_digest();
void test_storage_paths_match_python_layout();
void test_json_escape();
void test_domain_matcher_exact_subdomain_and_longest_suffix();
void test_domain_matcher_plus_and_case_modes();
void test_domain_matcher_normalizes_unicode_domain_to_idna();
void test_domain_cache_loads_active_rules();
void test_domain_cache_respects_matcher_flags();
void test_domain_cache_loads_plus_and_case_modes();
void test_domain_cache_reload_sees_rule_changes();
void test_sqlite_db_applies_pragmas();
void test_sqlite_db_rejects_database_without_wal();
void test_sqlite_db_cleans_up_after_constructor_failure();
void test_sqlite_statement_step_done_rejects_rows();
void test_sqlite_statement_reset_clears_bindings();
void test_sqlite_statement_outliving_db_closes_connection();

int main() {
    try {
        test_config_defaults();
        test_config_dotenv_and_environment_override();
        test_time_and_path_parts();
        test_ids_have_expected_prefixes();
        test_sha256_known_digest();
        test_storage_paths_match_python_layout();
        test_json_escape();
        test_domain_matcher_exact_subdomain_and_longest_suffix();
        test_domain_matcher_plus_and_case_modes();
        test_domain_matcher_normalizes_unicode_domain_to_idna();
        test_domain_cache_loads_active_rules();
        test_domain_cache_respects_matcher_flags();
        test_domain_cache_loads_plus_and_case_modes();
        test_domain_cache_reload_sees_rule_changes();
        test_sqlite_db_applies_pragmas();
        test_sqlite_db_rejects_database_without_wal();
        test_sqlite_db_cleans_up_after_constructor_failure();
        test_sqlite_statement_step_done_rejects_rows();
        test_sqlite_statement_reset_clears_bindings();
        test_sqlite_statement_outliving_db_closes_connection();
        std::cout << "ingestd_tests ok\n";
        return EXIT_SUCCESS;
    } catch (const std::exception& exc) {
        std::cerr << "ingestd_tests failed: " << exc.what() << "\n";
        return EXIT_FAILURE;
    }
}
