#pragma once

#include <string>

namespace rapid_inbox::ingestd {

std::string safe_filename(const std::string& filename);
std::string raw_message_path(const std::string& message_id, const std::string& received_at);
std::string manifest_path(const std::string& message_id, const std::string& received_at);

}
