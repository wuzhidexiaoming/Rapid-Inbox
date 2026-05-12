#include "../src/config.h"

#include <array>
#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <optional>
#include <stdexcept>
#include <string>
#include <system_error>
#include <vector>

namespace test {
inline void check(bool condition, const std::string& message) {
    if (!condition) {
        throw std::runtime_error(message);
    }
}
}

namespace {

constexpr std::array<const char*, 15> kConfigEnvVars = {
    "HOST",
    "PORT",
    "SMTP_HOST",
    "SMTP_PORT",
    "HOME",
    "STORAGE_ROOT",
    "DATABASE_PATH",
    "MAX_MESSAGE_SIZE_BYTES",
    "MAX_RECIPIENTS_PER_MESSAGE",
    "SMTP_IDLE_TIMEOUT_SECONDS",
    "INGEST_QUEUE_MAX_MESSAGES",
    "INGEST_BATCH_MAX_MESSAGES",
    "INGEST_FLUSH_INTERVAL_MS",
    "INGEST_SQLITE_BUSY_TIMEOUT_MS",
    "INGEST_STORAGE_FSYNC",
};

class ScopedEnvGuard {
public:
    ScopedEnvGuard() {
        saved_.reserve(kConfigEnvVars.size());
        for (const char* name : kConfigEnvVars) {
            const char* value = std::getenv(name);
            if (value != nullptr) {
                saved_.push_back({name, std::string(value)});
            } else {
                saved_.push_back({name, std::nullopt});
            }
            unsetenv(name);
        }
    }

    ~ScopedEnvGuard() {
        for (const auto& entry : saved_) {
            if (entry.value.has_value()) {
                setenv(entry.name.c_str(), entry.value->c_str(), 1);
            } else {
                unsetenv(entry.name.c_str());
            }
        }
    }

    void set(const std::string& name, const std::string& value) {
        setenv(name.c_str(), value.c_str(), 1);
    }

private:
    struct Entry {
        std::string name;
        std::optional<std::string> value;
    };

    std::vector<Entry> saved_;
};

class ScopedTempDir {
public:
    ScopedTempDir() {
        const auto base = std::filesystem::temp_directory_path();
        const auto now = std::chrono::steady_clock::now().time_since_epoch().count();
        for (int attempt = 0; attempt < 100; ++attempt) {
            root_ = base / ("rapid-inbox-config-" + std::to_string(now) + "-" +
                            std::to_string(attempt));
            std::error_code ec;
            if (std::filesystem::create_directory(root_, ec)) {
                return;
            }
        }
        throw std::runtime_error("failed to create unique temp directory");
    }

    ~ScopedTempDir() {
        std::error_code ec;
        std::filesystem::remove_all(root_, ec);
    }

    const std::filesystem::path& path() const {
        return root_;
    }

private:
    std::filesystem::path root_;
};

void write_env_file(const std::filesystem::path& dir, const std::string& contents) {
    std::ofstream env(dir / ".env", std::ios::trunc);
    env << contents;
}

template <typename Fn>
void expect_runtime_error_contains(Fn&& fn, const std::string& expected) {
    try {
        fn();
        throw std::runtime_error("expected runtime_error");
    } catch (const std::runtime_error& exc) {
        test::check(std::string(exc.what()).find(expected) != std::string::npos,
                    std::string("unexpected error: ") + exc.what());
    }
}

void expect_invalid_dotenv_integer(const std::string& dotenv, const std::string& expected) {
    ScopedEnvGuard env_guard;
    ScopedTempDir temp_dir;
    write_env_file(temp_dir.path(), dotenv);

    expect_runtime_error_contains(
        [&] { rapid_inbox::ingestd::Config::load(temp_dir.path()); },
        expected);
}

}  // namespace

void test_config_defaults() {
    ScopedEnvGuard env_guard;
    ScopedTempDir temp_dir;

    rapid_inbox::ingestd::Config config = rapid_inbox::ingestd::Config::load(temp_dir.path());
    test::check(config.base_dir == std::filesystem::absolute(temp_dir.path()).lexically_normal(),
                "default base dir");
    test::check(config.host == "127.0.0.1", "default host");
    test::check(config.port == 8000, "default HTTP port mirror");
    test::check(config.smtp_host == "127.0.0.1", "default SMTP host");
    test::check(config.smtp_port == 25, "default SMTP port");
    test::check(config.storage_root == temp_dir.path() / "storage", "default storage root");
    test::check(config.database_path == temp_dir.path() / "storage" / "app.db",
                "default database path");
    test::check(config.ingest_batch_max_messages == 250, "default ingest batch size");
    test::check(config.ingest_flush_interval_ms == 250, "default flush interval");
}

void test_config_dotenv_and_environment_override() {
    {
        ScopedEnvGuard env_guard;
        ScopedTempDir temp_dir;
        write_env_file(temp_dir.path(),
                       R"(# comment

export HOST = 127.0.0.2
SMTP_HOST = "0.0.0.0"
SMTP_PORT = 2525
STORAGE_ROOT = custom-storage
DATABASE_PATH = custom-db/app.db
MAX_MESSAGE_SIZE_BYTES = 4096
MAX_RECIPIENTS_PER_MESSAGE = 33
SMTP_IDLE_TIMEOUT_SECONDS = 11
INGEST_QUEUE_MAX_MESSAGES = 1234
INGEST_BATCH_MAX_MESSAGES = 55
INGEST_FLUSH_INTERVAL_MS = 77
INGEST_SQLITE_BUSY_TIMEOUT_MS = 88
INGEST_STORAGE_FSYNC = true
)");

        env_guard.set("SMTP_PORT", "2526");

        rapid_inbox::ingestd::Config config = rapid_inbox::ingestd::Config::load(temp_dir.path());
        test::check(config.host == "127.0.0.2", "export syntax with trimmed key/value");
        test::check(config.smtp_host == "0.0.0.0", "quoted dotenv value");
        test::check(config.smtp_port == 2526, "environment overrides dotenv");
        test::check(config.storage_root == temp_dir.path() / "custom-storage",
                    "relative storage root resolves from base dir");
        test::check(config.database_path == temp_dir.path() / "custom-db" / "app.db",
                    "relative database path resolves from base dir");
        test::check(config.max_message_size_bytes == 4096, "parsed max message size");
        test::check(config.max_recipients_per_message == 33, "parsed recipient limit");
        test::check(config.smtp_idle_timeout_seconds == 11, "parsed smtp idle timeout");
        test::check(config.ingest_queue_max_messages == 1234, "parsed ingest queue size");
        test::check(config.ingest_batch_max_messages == 55, "parsed ingest batch size");
        test::check(config.ingest_flush_interval_ms == 77, "parsed flush interval");
        test::check(config.ingest_sqlite_busy_timeout_ms == 88, "parsed sqlite busy timeout");
        test::check(config.ingest_storage_fsync, "parsed fsync boolean");
    }

    {
        ScopedEnvGuard env_guard;
        ScopedTempDir temp_dir;
        write_env_file(temp_dir.path(), "PORT=\n");

        rapid_inbox::ingestd::Config config = rapid_inbox::ingestd::Config::load(temp_dir.path());
        test::check(config.port == 8000, "blank PORT falls back to default");
    }

    {
        ScopedEnvGuard env_guard;
        ScopedTempDir temp_dir;
        env_guard.set("PORT", "  9001  ");

        rapid_inbox::ingestd::Config config = rapid_inbox::ingestd::Config::load(temp_dir.path());
        test::check(config.port == 9001, "trimmed PORT parses");
    }

    {
        ScopedEnvGuard env_guard;
        ScopedTempDir temp_dir;
        env_guard.set("STORAGE_ROOT", "   ");
        env_guard.set("DATABASE_PATH", "\t ");

        rapid_inbox::ingestd::Config config = rapid_inbox::ingestd::Config::load(temp_dir.path());
        test::check(config.storage_root == temp_dir.path() / "storage",
                    "blank STORAGE_ROOT falls back to default");
        test::check(config.database_path == temp_dir.path() / "storage" / "app.db",
                    "blank DATABASE_PATH falls back to default");
    }

    {
        ScopedEnvGuard env_guard;
        ScopedTempDir temp_dir;
        write_env_file(temp_dir.path(), "STORAGE_ROOT=custom-storage\n");

        rapid_inbox::ingestd::Config config = rapid_inbox::ingestd::Config::load(temp_dir.path());
        test::check(config.storage_root == temp_dir.path() / "custom-storage",
                    "custom storage root resolves from base dir");
        test::check(config.database_path == config.storage_root / "app.db",
                    "custom storage root keeps default database path");
    }

    {
        ScopedEnvGuard env_guard;
        ScopedTempDir temp_dir;
        env_guard.set("HOME", (temp_dir.path() / "home").string());
        env_guard.set("STORAGE_ROOT", "~/rapid-inbox-storage");

        rapid_inbox::ingestd::Config config = rapid_inbox::ingestd::Config::load(temp_dir.path());
        test::check(config.storage_root == temp_dir.path() / "home" / "rapid-inbox-storage",
                    "tilde storage root expands from HOME");
        test::check(config.database_path == config.storage_root / "app.db",
                    "tilde storage root keeps default database path");
    }

    expect_invalid_dotenv_integer("SMTP_PORT=25abc\n", "invalid SMTP_PORT: 25abc");
    expect_invalid_dotenv_integer("SMTP_IDLE_TIMEOUT_SECONDS=abc\n",
                                  "invalid SMTP_IDLE_TIMEOUT_SECONDS: abc");
    expect_invalid_dotenv_integer("INGEST_QUEUE_MAX_MESSAGES=999999999999999999999999\n",
                                  "invalid INGEST_QUEUE_MAX_MESSAGES: 999999999999999999999999");
}
