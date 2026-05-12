#include "config.h"

#include <charconv>
#include <cctype>
#include <cstdlib>
#include <fstream>
#include <optional>
#include <stdexcept>
#include <string>
#include <system_error>
#include <unordered_map>

namespace rapid_inbox::ingestd {
namespace {

std::string trim(std::string value) {
    const auto first = value.find_first_not_of(" \t\r\n");
    if (first == std::string::npos) {
        return "";
    }
    const auto last = value.find_last_not_of(" \t\r\n");
    return value.substr(first, last - first + 1);
}

std::string unquote(std::string value) {
    if (value.size() >= 2 && ((value.front() == '"' && value.back() == '"') ||
                              (value.front() == '\'' && value.back() == '\''))) {
        return value.substr(1, value.size() - 2);
    }
    return value;
}

std::optional<std::string> configured_value(
    const std::unordered_map<std::string, std::string>& values,
    const std::string& key) {
    if (const char* env_value = std::getenv(key.c_str())) {
        return std::string(env_value);
    }
    const auto found = values.find(key);
    if (found == values.end()) {
        return std::nullopt;
    }
    return found->second;
}

std::string value_for(const std::unordered_map<std::string, std::string>& values,
                      const std::string& key,
                      const std::string& fallback) {
    const auto value = configured_value(values, key);
    return value.has_value() ? *value : fallback;
}

std::optional<std::string> normalized_configured_value(
    const std::unordered_map<std::string, std::string>& values,
    const std::string& key) {
    const auto value = configured_value(values, key);
    if (!value.has_value()) {
        return std::nullopt;
    }
    std::string normalized = trim(*value);
    if (normalized.empty()) {
        return std::nullopt;
    }
    return normalized;
}

std::runtime_error invalid_integer_error(const std::string& key, const std::string& value) {
    return std::runtime_error("invalid " + key + ": " + value);
}

int int_for(const std::unordered_map<std::string, std::string>& values,
            const std::string& key,
            int fallback) {
    const auto value = normalized_configured_value(values, key);
    if (!value.has_value()) {
        return fallback;
    }
    int parsed = 0;
    const char* first = value->data();
    const char* last = first + value->size();
    const auto [ptr, ec] = std::from_chars(first, last, parsed);
    if (ec != std::errc{} || ptr != last) {
        throw invalid_integer_error(key, *value);
    }
    return parsed;
}

bool bool_for(const std::unordered_map<std::string, std::string>& values,
              const std::string& key,
              bool fallback) {
    std::string value = value_for(values, key, "");
    if (value.empty()) {
        return fallback;
    }
    for (char& ch : value) {
        ch = static_cast<char>(std::tolower(static_cast<unsigned char>(ch)));
    }
    return value == "1" || value == "true" || value == "yes" || value == "on";
}

std::filesystem::path resolve_path(const std::string& value,
                                   const std::filesystem::path& fallback,
                                   const std::filesystem::path& base_dir) {
    const auto normalized = trim(value);
    if (normalized.empty()) {
        return fallback;
    }
    std::filesystem::path path = [&normalized]() {
        if (normalized.front() != '~' || (normalized.size() > 1 && normalized[1] != '/')) {
            return std::filesystem::path(normalized);
        }
        if (const char* home = std::getenv("HOME"); home != nullptr && home[0] != '\0') {
            if (normalized.size() == 1) {
                return std::filesystem::path(home);
            }
            return std::filesystem::path(home) / normalized.substr(2);
        }
        return std::filesystem::path(normalized);
    }();
    if (path.is_relative()) {
        path = base_dir / path;
    }
    return path.lexically_normal();
}

std::unordered_map<std::string, std::string> load_dotenv(const std::filesystem::path& dotenv_path) {
    std::unordered_map<std::string, std::string> values;
    std::ifstream input(dotenv_path);
    std::string line;
    while (std::getline(input, line)) {
        line = trim(line);
        if (line.empty() || line[0] == '#') {
            continue;
        }
        if (line.rfind("export ", 0) == 0) {
            line = trim(line.substr(7));
        }
        const auto equals = line.find('=');
        if (equals == std::string::npos) {
            continue;
        }
        std::string key = trim(line.substr(0, equals));
        std::string value = unquote(trim(line.substr(equals + 1)));
        if (!key.empty()) {
            values[key] = value;
        }
    }
    return values;
}

}

Config Config::load(const std::filesystem::path& base) {
    Config config;
    config.base_dir = std::filesystem::absolute(base).lexically_normal();
    const auto dotenv = load_dotenv(config.base_dir / ".env");

    config.host = value_for(dotenv, "HOST", "127.0.0.1");
    config.port = int_for(dotenv, "PORT", 8000);
    config.smtp_host = value_for(dotenv, "SMTP_HOST", "127.0.0.1");
    config.smtp_port = int_for(dotenv, "SMTP_PORT", 25);
    config.max_message_size_bytes = int_for(dotenv, "MAX_MESSAGE_SIZE_BYTES", 52428800);
    config.max_recipients_per_message = int_for(dotenv, "MAX_RECIPIENTS_PER_MESSAGE", 20);
    config.smtp_idle_timeout_seconds = int_for(dotenv, "SMTP_IDLE_TIMEOUT_SECONDS", 30);
    config.ingest_queue_max_messages = int_for(dotenv, "INGEST_QUEUE_MAX_MESSAGES", 10000);
    config.ingest_batch_max_messages = int_for(dotenv, "INGEST_BATCH_MAX_MESSAGES", 250);
    config.ingest_flush_interval_ms = int_for(dotenv, "INGEST_FLUSH_INTERVAL_MS", 250);
    config.ingest_sqlite_busy_timeout_ms = int_for(dotenv, "INGEST_SQLITE_BUSY_TIMEOUT_MS", 5000);
    config.ingest_storage_fsync = bool_for(dotenv, "INGEST_STORAGE_FSYNC", false);

    config.storage_root = resolve_path(value_for(dotenv, "STORAGE_ROOT", ""),
                                       config.base_dir / "storage",
                                       config.base_dir);
    config.database_path = resolve_path(value_for(dotenv, "DATABASE_PATH", ""),
                                        config.storage_root / "app.db",
                                        config.base_dir);
    return config;
}

}
