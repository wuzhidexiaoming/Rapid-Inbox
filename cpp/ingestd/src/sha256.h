#pragma once

#include <string>
#include <string_view>

namespace rapid_inbox::ingestd {

std::string sha256_hex(std::string_view content);

}
