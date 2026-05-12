#include "../src/sqlite_db.h"

#include <sqlite3.h>

#include <filesystem>
#include <stdexcept>
#include <string>
#include <system_error>

namespace test {
inline void check(bool condition, const std::string& message) {
    if (!condition) {
        throw std::runtime_error(message);
    }
}
}

namespace {

namespace fs = std::filesystem;

using rapid_inbox::ingestd::SqliteDb;
using rapid_inbox::ingestd::Statement;

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

int count_open_fds_for_db(const fs::path& db_path) {
    const fs::path fd_dir{"/proc/self/fd"};
    test::check(fs::exists(fd_dir), "/proc/self/fd is available for fd leak regression");

    const std::string db_file = fs::absolute(db_path).lexically_normal().string();
    const std::string wal_file = db_file + "-wal";
    const std::string shm_file = db_file + "-shm";

    int count = 0;
    for (const auto& entry : fs::directory_iterator(fd_dir)) {
        std::error_code ec;
        const fs::path target = fs::read_symlink(entry.path(), ec);
        if (ec) {
            continue;
        }
        const std::string target_string = target.lexically_normal().string();
        if (target_string == db_file || target_string == wal_file || target_string == shm_file) {
            ++count;
        }
    }
    return count;
}

Statement prepare_statement_that_outlives_db(const fs::path& db_path) {
    SqliteDb db(db_path, 5000);
    db.exec("CREATE TABLE outlive_test (value INTEGER NOT NULL)");
    db.exec("INSERT INTO outlive_test VALUES (7)");
    return db.prepare("SELECT value FROM outlive_test");
}

int read_single_int(SqliteDb& db, const std::string& sql) {
    auto statement = db.prepare(sql);
    test::check(statement.step_row(), sql + " returns a row");
    const int value = sqlite3_column_int(statement.get(), 0);
    test::check(!statement.step_row(), sql + " returns exactly one row");
    return value;
}

std::string read_single_text(SqliteDb& db, const std::string& sql) {
    auto statement = db.prepare(sql);
    test::check(statement.step_row(), sql + " returns a row");
    const unsigned char* raw_value = sqlite3_column_text(statement.get(), 0);
    test::check(raw_value != nullptr, sql + " returns text");
    const std::string value = reinterpret_cast<const char*>(raw_value);
    test::check(!statement.step_row(), sql + " returns exactly one row");
    return value;
}

}  // namespace

void test_sqlite_db_applies_pragmas() {
    const fs::path db_path = fresh_db_path("rapid-inbox-sqlite-pragmas.sqlite");
    SqliteDb db(db_path, 4321);

    test::check(read_single_int(db, "PRAGMA foreign_keys") == 1,
                "foreign_keys pragma is enabled");
    test::check(read_single_int(db, "PRAGMA synchronous") == 2,
                "synchronous pragma is FULL");
    test::check(read_single_int(db, "PRAGMA busy_timeout") == 4321,
                "busy_timeout pragma matches constructor");

    std::string journal_mode = read_single_text(db, "PRAGMA journal_mode");
    for (char& ch : journal_mode) {
        if (ch >= 'A' && ch <= 'Z') {
            ch = static_cast<char>(ch - 'A' + 'a');
        }
    }
    test::check(journal_mode == "wal", "journal_mode pragma is WAL");
}

void test_sqlite_db_rejects_database_without_wal() {
    bool threw = false;
    try {
        SqliteDb db(":memory:", 5000);
    } catch (const std::runtime_error& exc) {
        threw = true;
        const std::string message = exc.what();
        test::check(message.find("WAL") != std::string::npos,
                    "WAL rejection error explains failed invariant");
    }
    test::check(threw, "sqlite db rejects handles that cannot enable WAL");
}

void test_sqlite_db_cleans_up_after_constructor_failure() {
    const sqlite3_int64 before = sqlite3_memory_used();
    for (int i = 0; i < 10; ++i) {
        bool threw = false;
        try {
            SqliteDb db(":memory:", 5000);
        } catch (const std::runtime_error&) {
            threw = true;
        }
        test::check(threw, "constructor failure path is exercised");
    }
    const sqlite3_int64 after = sqlite3_memory_used();
    test::check(after <= before + 4096,
                "constructor failure closes opened sqlite handle before rethrow");
}

void test_sqlite_statement_step_done_rejects_rows() {
    const fs::path db_path = fresh_db_path("rapid-inbox-sqlite-step-done.sqlite");
    SqliteDb db(db_path, 5000);
    auto statement = db.prepare("SELECT 1");

    bool threw = false;
    try {
        statement.step_done();
    } catch (const std::runtime_error& exc) {
        threw = true;
        const std::string message = exc.what();
        test::check(message.find("SQLITE_DONE") != std::string::npos,
                    "step_done error names expected DONE state");
    }
    test::check(threw, "step_done throws when sqlite3_step returns SQLITE_ROW");
}

void test_sqlite_statement_reset_clears_bindings() {
    const fs::path db_path = fresh_db_path("rapid-inbox-sqlite-reset.sqlite");
    SqliteDb db(db_path, 5000);
    auto statement = db.prepare("SELECT ?1");

    const char* value = "hello";
    int rc = sqlite3_bind_text(statement.get(), 1, value, -1, SQLITE_STATIC);
    test::check(rc == SQLITE_OK, "bind text succeeds");

    test::check(statement.step_row(), "bound SELECT returns a row");
    const unsigned char* first_value = sqlite3_column_text(statement.get(), 0);
    test::check(first_value != nullptr, "bound SELECT returns text");
    test::check(std::string(reinterpret_cast<const char*>(first_value)) == "hello",
                "bound SELECT returns bound text");

    statement.reset();

    test::check(statement.step_row(), "reset SELECT returns a row");
    test::check(sqlite3_column_type(statement.get(), 0) == SQLITE_NULL,
                "reset clears prior text binding");
    statement.step_done();
}

void test_sqlite_statement_outliving_db_closes_connection() {
    const fs::path db_path = fresh_db_path("rapid-inbox-sqlite-outliving-statement.sqlite");

    {
        auto statement = prepare_statement_that_outlives_db(db_path);
        test::check(statement.step_row(), "statement remains usable after db object is destroyed");
        test::check(sqlite3_column_int(statement.get(), 0) == 7,
                    "outliving statement reads expected row");
        statement.step_done();
    }

    test::check(count_open_fds_for_db(db_path) == 0,
                "statement outliving db does not leak sqlite file descriptors");
}
