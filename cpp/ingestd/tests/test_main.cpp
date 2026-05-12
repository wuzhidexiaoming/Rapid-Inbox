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
void test_domain_cache_snapshots_domain_policies();
void test_domain_cache_snapshots_matcher_and_policies_together();
void test_sqlite_db_applies_pragmas();
void test_sqlite_db_rejects_database_without_wal();
void test_sqlite_db_cleans_up_after_constructor_failure();
void test_sqlite_statement_step_done_rejects_rows();
void test_sqlite_statement_reset_clears_bindings();
void test_sqlite_statement_outliving_db_closes_connection();
void test_mail_queue_capacity_and_close();
void test_mail_queue_try_push_accepts_lvalues_and_rvalues();
void test_mail_queue_close_wakes_waiting_pop_batch();
void test_smtp_session_accepts_valid_message();
void test_smtp_session_attaches_domain_policy_snapshot();
void test_smtp_session_rejects_unknown_domain();
void test_smtp_session_rejects_prefix_collision_commands();
void test_smtp_session_clears_transaction_after_queueing();
void test_smtp_session_rejects_rcpt_before_mail_from();
void test_smtp_session_rejects_empty_mail_from_without_changing_state();
void test_smtp_session_rejects_data_arguments();
void test_smtp_session_discards_oversized_data_until_terminator();
void test_smtp_session_reports_queue_full();
void test_smtp_server_stop_wakes_idle_client();
void test_batch_writer_writes_raw_and_manifest();
void test_batch_writer_writes_private_storage_permissions();
void test_batch_writer_manifest_includes_domain_policy_snapshot();
void test_batch_writer_missing_domain_policy_rejects_without_creating_database();
void test_batch_writer_uses_job_policy_without_touching_database();
void test_batch_writer_ignores_preexisting_part_symlinks();
void test_batch_writer_writes_sqlite_pending_records();

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
        test_domain_cache_snapshots_domain_policies();
        test_domain_cache_snapshots_matcher_and_policies_together();
        test_sqlite_db_applies_pragmas();
        test_sqlite_db_rejects_database_without_wal();
        test_sqlite_db_cleans_up_after_constructor_failure();
        test_sqlite_statement_step_done_rejects_rows();
        test_sqlite_statement_reset_clears_bindings();
        test_sqlite_statement_outliving_db_closes_connection();
        test_mail_queue_capacity_and_close();
        test_mail_queue_try_push_accepts_lvalues_and_rvalues();
        test_mail_queue_close_wakes_waiting_pop_batch();
        test_smtp_session_accepts_valid_message();
        test_smtp_session_attaches_domain_policy_snapshot();
        test_smtp_session_rejects_unknown_domain();
        test_smtp_session_rejects_prefix_collision_commands();
        test_smtp_session_clears_transaction_after_queueing();
        test_smtp_session_rejects_rcpt_before_mail_from();
        test_smtp_session_rejects_empty_mail_from_without_changing_state();
        test_smtp_session_rejects_data_arguments();
        test_smtp_session_discards_oversized_data_until_terminator();
        test_smtp_session_reports_queue_full();
        test_smtp_server_stop_wakes_idle_client();
        test_batch_writer_writes_raw_and_manifest();
        test_batch_writer_writes_private_storage_permissions();
        test_batch_writer_manifest_includes_domain_policy_snapshot();
        test_batch_writer_missing_domain_policy_rejects_without_creating_database();
        test_batch_writer_uses_job_policy_without_touching_database();
        test_batch_writer_ignores_preexisting_part_symlinks();
        test_batch_writer_writes_sqlite_pending_records();
        std::cout << "ingestd_tests ok\n";
        return EXIT_SUCCESS;
    } catch (const std::exception& exc) {
        std::cerr << "ingestd_tests failed: " << exc.what() << "\n";
        return EXIT_FAILURE;
    }
}
