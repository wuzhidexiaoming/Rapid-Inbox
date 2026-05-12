#include "sha256.h"

#include <openssl/evp.h>

#include <array>
#include <iomanip>
#include <sstream>
#include <stdexcept>

namespace rapid_inbox::ingestd {

std::string sha256_hex(std::string_view content) {
    std::array<unsigned char, EVP_MAX_MD_SIZE> digest{};
    unsigned int digest_size = 0;

    EVP_MD_CTX* context = EVP_MD_CTX_new();
    if (context == nullptr) {
        throw std::runtime_error("failed to create sha256 context");
    }

    const bool ok = EVP_DigestInit_ex(context, EVP_sha256(), nullptr) == 1 &&
                    EVP_DigestUpdate(context, content.data(), content.size()) == 1 &&
                    EVP_DigestFinal_ex(context, digest.data(), &digest_size) == 1;
    EVP_MD_CTX_free(context);

    if (!ok) {
        throw std::runtime_error("failed to compute sha256 digest");
    }

    std::ostringstream output;
    output << std::hex << std::nouppercase << std::setfill('0');
    for (unsigned int index = 0; index < digest_size; ++index) {
        output << std::setw(2) << static_cast<int>(digest[index]);
    }
    return output.str();
}

}
