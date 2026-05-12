#include "id.h"

#include <array>
#include <openssl/rand.h>

#include <stdexcept>

namespace rapid_inbox::ingestd {
namespace {

std::string hex_encode(const std::array<unsigned char, 16>& bytes) {
    constexpr char kDigits[] = "0123456789abcdef";
    std::string encoded;
    encoded.reserve(bytes.size() * 2);
    for (unsigned char byte : bytes) {
        encoded.push_back(kDigits[byte >> 4]);
        encoded.push_back(kDigits[byte & 0x0f]);
    }
    return encoded;
}

}

std::string make_prefixed_id(const std::string& prefix) {
    std::array<unsigned char, 16> bytes{};
    if (RAND_bytes(bytes.data(), static_cast<int>(bytes.size())) != 1) {
        throw std::runtime_error("failed to generate random id");
    }
    return prefix + hex_encode(bytes);
}

}
