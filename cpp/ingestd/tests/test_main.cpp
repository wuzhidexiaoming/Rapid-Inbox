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
void test_mime_parser_text_only_message();
void test_mime_parser_html_only_message();
void test_mime_parser_multipart_alternative();
void test_mime_parser_attachment_base64();
void test_mime_parser_inline_related_part();
void test_mime_parser_decodes_quoted_printable_text();
void test_mime_parser_decodes_encoded_subject();
void test_mime_parser_reports_malformed_multipart();
void test_mime_parser_allows_boundary_trailing_whitespace();
void test_mime_parser_decodes_latin1_text_body();
void test_mime_parser_keeps_empty_attachment();
void test_mime_parser_decodes_gbk_encoded_subject();
void test_mime_parser_failure_is_std_exception();
void test_verification_code_extracts_plain_six_digit_code();
void test_verification_code_extracts_chinese_code();
void test_verification_code_extracts_grouped_digit_code();
void test_verification_code_extracts_alphanumeric_code();
void test_verification_code_extracts_html_openai_code();
void test_verification_code_ignores_order_number();
void test_verification_code_ignores_ambiguous_two_codes();
void test_verification_code_extracts_numeric_html_entities();
void test_verification_code_ignores_spaced_currency_value();
void test_verification_code_ignores_date_fragments();
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
void test_smtp_server_idle_client_times_out();
void test_batch_writer_writes_raw_and_manifest();
void test_batch_writer_writes_private_storage_permissions();
void test_batch_writer_manifest_includes_domain_policy_snapshot();
void test_batch_writer_missing_domain_policy_rejects_without_creating_database();
void test_batch_writer_uses_job_policy_without_touching_database();
void test_batch_writer_ignores_preexisting_part_symlinks();
void test_batch_writer_writes_sqlite_parsed_records();
void test_batch_writer_marks_parse_failure_without_rejecting_raw();
void test_batch_writer_writes_parsed_attachment_records();

int main() {
    try {
        test_config_defaults();
        test_config_dotenv_and_environment_override();
        test_time_and_path_parts();
        test_ids_have_expected_prefixes();
        test_sha256_known_digest();
        test_storage_paths_match_python_layout();
        test_json_escape();
        test_mime_parser_text_only_message();
        test_mime_parser_html_only_message();
        test_mime_parser_multipart_alternative();
        test_mime_parser_attachment_base64();
        test_mime_parser_inline_related_part();
        test_mime_parser_decodes_quoted_printable_text();
        test_mime_parser_decodes_encoded_subject();
        test_mime_parser_reports_malformed_multipart();
        test_mime_parser_allows_boundary_trailing_whitespace();
        test_mime_parser_decodes_latin1_text_body();
        test_mime_parser_keeps_empty_attachment();
        test_mime_parser_decodes_gbk_encoded_subject();
        test_mime_parser_failure_is_std_exception();
        test_verification_code_extracts_plain_six_digit_code();
        test_verification_code_extracts_chinese_code();
        test_verification_code_extracts_grouped_digit_code();
        test_verification_code_extracts_alphanumeric_code();
        test_verification_code_extracts_html_openai_code();
        test_verification_code_ignores_order_number();
        test_verification_code_ignores_ambiguous_two_codes();
        test_verification_code_extracts_numeric_html_entities();
        test_verification_code_ignores_spaced_currency_value();
        test_verification_code_ignores_date_fragments();
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
        test_smtp_server_idle_client_times_out();
        test_batch_writer_writes_raw_and_manifest();
        test_batch_writer_writes_private_storage_permissions();
        test_batch_writer_manifest_includes_domain_policy_snapshot();
        test_batch_writer_missing_domain_policy_rejects_without_creating_database();
        test_batch_writer_uses_job_policy_without_touching_database();
        test_batch_writer_ignores_preexisting_part_symlinks();
        test_batch_writer_writes_sqlite_parsed_records();
        test_batch_writer_marks_parse_failure_without_rejecting_raw();
        test_batch_writer_writes_parsed_attachment_records();
        std::cout << "ingestd_tests ok\n";
        return EXIT_SUCCESS;
    } catch (const std::exception& exc) {
        std::cerr << "ingestd_tests failed: " << exc.what() << "\n";
        return EXIT_FAILURE;
    }
}
