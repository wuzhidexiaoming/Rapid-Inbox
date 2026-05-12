#pragma once

#include <string>

namespace rapid_inbox::ingestd {

struct DateParts {
    std::string year;
    std::string month;
    std::string day;
};

std::string utc_now();
DateParts path_date_parts(const std::string& timestamp);

}
