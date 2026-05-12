#include "json_util.h"

#include <string>

namespace rapid_inbox::ingestd {
namespace {

char hex_digit(unsigned char value) {
    constexpr char kDigits[] = "0123456789abcdef";
    return kDigits[value & 0x0f];
}

}

std::string json_escape(const std::string& value) {
    std::string escaped;
    escaped.reserve(value.size());

    for (unsigned char ch : value) {
        switch (ch) {
            case '"':
                escaped += "\\\"";
                break;
            case '\\':
                escaped += "\\\\";
                break;
            case '\b':
                escaped += "\\b";
                break;
            case '\f':
                escaped += "\\f";
                break;
            case '\n':
                escaped += "\\n";
                break;
            case '\r':
                escaped += "\\r";
                break;
            case '\t':
                escaped += "\\t";
                break;
            default:
                if (ch < 0x20) {
                    escaped += "\\u00";
                    escaped.push_back(hex_digit(static_cast<unsigned char>(ch >> 4)));
                    escaped.push_back(hex_digit(ch));
                } else {
                    escaped.push_back(static_cast<char>(ch));
                }
                break;
        }
    }

    return escaped;
}

}
