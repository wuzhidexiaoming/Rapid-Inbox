#include "../src/domain_matcher.h"

#include <stdexcept>
#include <string>
#include <vector>

namespace test {
inline void check(bool condition, const std::string& message) {
    if (!condition) {
        throw std::runtime_error(message);
    }
}
}

namespace {

using rapid_inbox::ingestd::DomainMatch;
using rapid_inbox::ingestd::DomainMatcher;
using rapid_inbox::ingestd::DomainRule;

DomainMatch require_match(const DomainMatcher& matcher, const std::string& address) {
    auto match = matcher.match_address(address);
    test::check(match.has_value(), "expected match for " + address);
    return *match;
}

void require_normalize_rejects(const std::string& domain, const std::string& message) {
    bool rejected = false;
    try {
        (void)rapid_inbox::ingestd::normalize_domain(domain);
    } catch (const std::exception&) {
        rejected = true;
    }
    test::check(rejected, message);
}

}  // namespace

void test_domain_matcher_exact_subdomain_and_longest_suffix() {
    {
        DomainMatcher matcher({
            DomainRule{
                .domain_id = 1,
                .root_domain_ascii = "adb.com",
                .accept_exact = true,
                .accept_subdomains = true,
                .plus_addressing_mode = "keep",
                .local_part_case_sensitive = false,
            },
        });

        const DomainMatch match = require_match(matcher, "Code@adb.com");
        test::check(match.domain_id == 1, "exact root domain id");
        test::check(match.domain_ascii == "adb.com", "exact normalized recipient domain");
        test::check(match.root_domain_ascii == "adb.com", "exact normalized root domain");
        test::check(match.local_part == "Code", "exact original local part");
        test::check(match.local_part_canonical == "code", "exact canonical local part");
        test::check(match.address_canonical == "code@adb.com", "exact canonical address");
    }

    {
        DomainMatcher matcher({
            DomainRule{
                .domain_id = 1,
                .root_domain_ascii = "adb.com",
                .accept_exact = true,
                .accept_subdomains = true,
                .plus_addressing_mode = "keep",
                .local_part_case_sensitive = false,
            },
            DomainRule{
                .domain_id = 2,
                .root_domain_ascii = "x.adb.com",
                .accept_exact = true,
                .accept_subdomains = true,
                .plus_addressing_mode = "keep",
                .local_part_case_sensitive = false,
            },
        });

        const DomainMatch match = require_match(matcher, "User@deep.x.adb.com");
        test::check(match.domain_id == 2, "longest suffix domain id");
        test::check(match.domain_ascii == "deep.x.adb.com", "longest suffix recipient domain");
        test::check(match.address_canonical == "user@deep.x.adb.com",
                    "longest suffix canonical address");
    }

    {
        DomainMatcher matcher({
            DomainRule{
                .domain_id = 3,
                .root_domain_ascii = "exact-only.test",
                .accept_exact = true,
                .accept_subdomains = false,
                .plus_addressing_mode = "keep",
                .local_part_case_sensitive = false,
            },
        });

        test::check(!matcher.match_address("a@sub.exact-only.test").has_value(),
                    "subdomain disabled returns no match");
    }

    {
        DomainMatcher matcher({
            DomainRule{
                .domain_id = 1,
                .root_domain_ascii = "adb.com",
                .accept_exact = true,
                .accept_subdomains = true,
                .plus_addressing_mode = "keep",
                .local_part_case_sensitive = false,
            },
            DomainRule{
                .domain_id = 2,
                .root_domain_ascii = "x.adb.com",
                .accept_exact = true,
                .accept_subdomains = false,
                .plus_addressing_mode = "strip",
                .local_part_case_sensitive = false,
            },
        });

        test::check(!matcher.match_address("Foo+tag@b.x.adb.com").has_value(),
                    "longest disabled subdomain blocks parent fallback");

        const DomainMatch exact_match = require_match(matcher, "Foo+tag@x.adb.com");
        test::check(exact_match.domain_id == 2, "exact longest rule id");
        test::check(exact_match.address_canonical == "foo@x.adb.com",
                    "exact longest rule strips plus tag");

        const DomainMatch parent_match = require_match(matcher, "Foo+tag@z.adb.com");
        test::check(parent_match.domain_id == 1, "parent rule id");
        test::check(parent_match.address_canonical == "foo+tag@z.adb.com",
                    "parent rule keeps plus tag");
    }
}

void test_domain_matcher_plus_and_case_modes() {
    {
        DomainMatcher matcher({
            DomainRule{
                .domain_id = 1,
                .root_domain_ascii = "strip.test",
                .accept_exact = true,
                .accept_subdomains = true,
                .plus_addressing_mode = "strip",
                .local_part_case_sensitive = false,
            },
        });

        const DomainMatch match = require_match(matcher, "User+tag@strip.test");
        test::check(match.local_part == "User+tag", "plus strip original local part");
        test::check(match.local_part_canonical == "user", "plus strip canonical local part");
        test::check(match.address_canonical == "user@strip.test",
                    "plus strip canonical address");
    }

    {
        DomainMatcher matcher({
            DomainRule{
                .domain_id = 2,
                .root_domain_ascii = "case.test",
                .accept_exact = true,
                .accept_subdomains = true,
                .plus_addressing_mode = "keep",
                .local_part_case_sensitive = true,
            },
        });

        const DomainMatch match = require_match(matcher, "User@case.test");
        test::check(match.local_part_canonical == "User", "case-sensitive local part preserved");
        test::check(match.address_canonical == "User@case.test",
                    "case-sensitive canonical address");
    }

    {
        DomainMatcher matcher({
            DomainRule{
                .domain_id = 3,
                .root_domain_ascii = "case.test",
                .accept_exact = true,
                .accept_subdomains = true,
                .plus_addressing_mode = "keep",
                .local_part_case_sensitive = false,
            },
        });

        const DomainMatch match = require_match(matcher, "\xC3\x9C" "ser@case.test");
        test::check(match.local_part_canonical == "\xC3\xBC" "ser",
                    "unicode lowercase local part");
        test::check(match.address_canonical == "\xC3\xBC" "ser@case.test",
                    "unicode lowercase canonical address");
    }

    {
        DomainMatcher matcher({
            DomainRule{
                .domain_id = 4,
                .root_domain_ascii = "case.test",
                .accept_exact = true,
                .accept_subdomains = true,
                .plus_addressing_mode = "keep",
                .local_part_case_sensitive = false,
            },
        });

        const DomainMatch match = require_match(matcher, "\xC4\xB0@case.test");
        test::check(match.local_part_canonical == "i\xCC\x87",
                    "dotted capital i canonical local part");
        test::check(match.address_canonical == "i\xCC\x87@case.test",
                    "dotted capital i canonical address");
    }

    {
        DomainMatcher matcher({
            DomainRule{
                .domain_id = 4,
                .root_domain_ascii = "example.com",
                .accept_exact = true,
                .accept_subdomains = true,
                .plus_addressing_mode = "keep",
                .local_part_case_sensitive = false,
            },
        });

        const std::string invalid_local_part_address("\xFF@example.com",
                                                     sizeof("\xFF@example.com") - 1);
        test::check(!matcher.match_address(invalid_local_part_address).has_value(),
                    "case-insensitive invalid UTF-8 local part returns no match");
    }

    {
        DomainMatcher matcher({
            DomainRule{
                .domain_id = 5,
                .root_domain_ascii = "case.test",
                .accept_exact = true,
                .accept_subdomains = true,
                .plus_addressing_mode = "keep",
                .local_part_case_sensitive = true,
            },
        });

        const DomainMatch match = require_match(matcher, "\xC4\xB0@case.test");
        test::check(match.local_part_canonical == "\xC4\xB0",
                    "unicode case-sensitive local part preserved");
        test::check(match.address_canonical == "\xC4\xB0@case.test",
                    "unicode case-sensitive canonical address");
    }
}

void test_domain_matcher_normalizes_unicode_domain_to_idna() {
    test::check(rapid_inbox::ingestd::normalize_domain(
                    "\xC2\xA0" "example.com" "\xC2\xA0") == "example.com",
                "unicode NBSP at domain edges is stripped");
    test::check(rapid_inbox::ingestd::normalize_domain("example.com\xE3\x80\x80") ==
                    "example.com",
                "unicode ideographic space at domain edge is stripped");
    test::check(rapid_inbox::ingestd::normalize_domain(
                    "\xE2\x80\x83" "example.com" "\xE2\x80\x89") == "example.com",
                "unicode em and thin spaces at domain edges are stripped");
    test::check(rapid_inbox::ingestd::normalize_domain("-bad.com") == "-bad.com",
                "python idna accepts leading hyphen ASCII labels");
    test::check(rapid_inbox::ingestd::normalize_domain("bad-.com") == "bad-.com",
                "python idna accepts trailing hyphen ASCII labels");
    test::check(rapid_inbox::ingestd::normalize_domain("a_b.com") == "a_b.com",
                "python idna accepts underscore ASCII labels");
    test::check(rapid_inbox::ingestd::normalize_domain("bad com.com") == "bad com.com",
                "python idna accepts ASCII space inside labels");
    test::check(rapid_inbox::ingestd::normalize_domain("bad/com.com") == "bad/com.com",
                "python idna accepts ASCII slash inside labels");
    test::check(rapid_inbox::ingestd::normalize_domain(std::string(63, 'a') + ".com") ==
                    std::string(63, 'a') + ".com",
                "63-byte ASCII label is accepted");
    test::check(rapid_inbox::ingestd::normalize_domain("\xEF\xBC\xA1-.com") == "a-.com",
                "python idna maps fullwidth A before ASCII length checks");
    test::check(rapid_inbox::ingestd::normalize_domain("\xE2\x91\xA0-.com") == "1-.com",
                "python idna maps circled digit one before ASCII length checks");
    test::check(rapid_inbox::ingestd::normalize_domain("a\xCC\x81-.com") ==
                    "xn----tfa.com",
                "python idna normalizes combining acute before punycode");
    test::check(rapid_inbox::ingestd::normalize_domain("\xCF\x82-.com") ==
                    "xn----zmb.com",
                "python idna folds greek final sigma before punycode");
    test::check(rapid_inbox::ingestd::normalize_domain("\xC3\x9F-.de") == "ss-.de",
                "python idna maps sharp s to ss before ASCII length checks");
    test::check(rapid_inbox::ingestd::normalize_domain("stra" "\xC3\x9F" "e-.de") ==
                    "strasse-.de",
                "python idna maps sharp s inside labels before ASCII length checks");
    test::check(rapid_inbox::ingestd::normalize_domain(
                    "\xE4\xBE\x8B\xE5\xAD\x90\xE3\x80\x82\xE6\xB5\x8B\xE8\xAF\x95") ==
                    "xn--fsqu00a.xn--0zwm56d",
                "python idna treats ideographic full stop as a dot separator");
    test::check(rapid_inbox::ingestd::normalize_domain("example" "\xEF\xBC\x8E" "com") ==
                    "example.com",
                "python idna treats fullwidth full stop as a dot separator");
    test::check(rapid_inbox::ingestd::normalize_domain("example" "\xEF\xBD\xA1" "com") ==
                    "example.com",
                "python idna treats halfwidth ideographic full stop as a dot separator");
    test::check(rapid_inbox::ingestd::normalize_domain("\xE1\x8E\xA0.com") ==
                    "xn--kz9a.com",
                "python lower then idna normalizes Cherokee capital letter a");
    test::check(rapid_inbox::ingestd::normalize_domain("\xE1\x83\xBC.com") ==
                    "xn--upd.com",
                "python lower then idna normalizes Georgian modifier letter nar");

    require_normalize_rejects("bad..com", "empty interior ASCII label is rejected");
    require_normalize_rejects(".leading.com", "leading empty ASCII label is rejected");
    require_normalize_rejects(std::string(64, 'a') + ".com",
                              "64-byte ASCII label is rejected");
    require_normalize_rejects("\xEE\x80\x80.com",
                              "python idna rejects private-use codepoints");
    require_normalize_rejects("\xE2\x80\xAE.com",
                              "python idna rejects right-to-left override");
    require_normalize_rejects("\xD7\x90" "a.com",
                              "python idna rejects mixed bidi labels");

    {
        DomainMatcher matcher({
            DomainRule{
                .domain_id = 5,
                .root_domain_ascii = "example.com",
                .accept_exact = true,
                .accept_subdomains = true,
                .plus_addressing_mode = "keep",
                .local_part_case_sensitive = false,
            },
        });

        const DomainMatch normal_match = require_match(matcher, "User@example.com");
        test::check(normal_match.domain_id == 5, "normal example.com domain id");
        test::check(normal_match.address_canonical == "user@example.com",
                    "normal example.com canonical address");

        const DomainMatch halfwidth_dot_match =
            require_match(matcher, "User@example" "\xEF\xBD\xA1" "com");
        test::check(halfwidth_dot_match.domain_id == 5, "halfwidth dot domain id");
        test::check(halfwidth_dot_match.address_canonical == "user@example.com",
                    "halfwidth dot canonical address");

        const std::string embedded_nul_domain("example.com\0.evil.com",
                                              sizeof("example.com\0.evil.com") - 1);
        bool rejected_embedded_nul_domain = false;
        try {
            (void)rapid_inbox::ingestd::normalize_domain(embedded_nul_domain);
        } catch (const std::invalid_argument&) {
            rejected_embedded_nul_domain = true;
        }
        test::check(rejected_embedded_nul_domain,
                    "embedded NUL domain is rejected before C ABI normalization");

        const std::string embedded_nul_address("User@example.com\0.evil.com",
                                               sizeof("User@example.com\0.evil.com") - 1);
        test::check(!matcher.match_address(embedded_nul_address).has_value(),
                    "embedded NUL recipient domain returns no match");
        test::check(!matcher.match_address("User@bad..com").has_value(),
                    "invalid empty ASCII recipient label returns no match");
        test::check(!matcher.match_address("User@\xEE\x80\x80.com").has_value(),
                    "private-use recipient domain returns no match");
    }

    {
        DomainMatcher matcher({
            DomainRule{
                .domain_id = 6,
                .root_domain_ascii = "-bad.com",
                .accept_exact = true,
                .accept_subdomains = true,
                .plus_addressing_mode = "keep",
                .local_part_case_sensitive = false,
            },
        });

        const DomainMatch match = require_match(matcher, "User@-bad.com");
        test::check(match.domain_id == 6, "leading hyphen ASCII label domain id");
        test::check(match.address_canonical == "user@-bad.com",
                    "leading hyphen ASCII label canonical address");
    }

    {
        DomainMatcher matcher({
            DomainRule{
                .domain_id = 7,
                .root_domain_ascii = "bad-.com",
                .accept_exact = true,
                .accept_subdomains = true,
                .plus_addressing_mode = "keep",
                .local_part_case_sensitive = false,
            },
        });

        const DomainMatch match = require_match(matcher, "User@bad-.com");
        test::check(match.domain_id == 7, "trailing hyphen ASCII label domain id");
        test::check(match.address_canonical == "user@bad-.com",
                    "trailing hyphen ASCII label canonical address");
    }

    {
        DomainMatcher matcher({
            DomainRule{
                .domain_id = 8,
                .root_domain_ascii = "bad/com.com",
                .accept_exact = true,
                .accept_subdomains = true,
                .plus_addressing_mode = "keep",
                .local_part_case_sensitive = false,
            },
        });

        const DomainMatch match = require_match(matcher, "User@bad/com.com");
        test::check(match.domain_id == 8, "ASCII slash label domain id");
        test::check(match.address_canonical == "user@bad/com.com",
                    "ASCII slash label canonical address");
    }

    {
        DomainMatcher matcher({
            DomainRule{
                .domain_id = 10,
                .root_domain_ascii = "a-.com",
                .accept_exact = true,
                .accept_subdomains = true,
                .plus_addressing_mode = "keep",
                .local_part_case_sensitive = false,
            },
        });

        const DomainMatch match = require_match(matcher, "User@\xEF\xBC\xA1-.com");
        test::check(match.domain_id == 10, "fullwidth normalized domain id");
        test::check(match.domain_ascii == "a-.com", "fullwidth normalized domain");
        test::check(match.address_canonical == "user@a-.com",
                    "fullwidth normalized canonical address");
    }

    {
        DomainMatcher matcher({
            DomainRule{
                .domain_id = 11,
                .root_domain_ascii = "xn--kz9a.com",
                .accept_exact = true,
                .accept_subdomains = true,
                .plus_addressing_mode = "keep",
                .local_part_case_sensitive = false,
            },
        });

        const DomainMatch match = require_match(matcher, "User@\xE1\x8E\xA0.com");
        test::check(match.domain_id == 11, "cherokee normalized domain id");
        test::check(match.domain_ascii == "xn--kz9a.com", "cherokee normalized domain");
        test::check(match.address_canonical == "user@xn--kz9a.com",
                    "cherokee normalized canonical address");
    }

    test::check(rapid_inbox::ingestd::normalize_domain("stra" "\xC3\x9F" "e.de") == "strasse.de",
                "unicode domain normalizes to IDNA transitional form");

    const std::string python_loose_domain = "ma" "\xC3\xB1" "ana-.com";
    test::check(rapid_inbox::ingestd::normalize_domain(python_loose_domain) ==
                    "xn--maana--xwa.com",
                "python idna accepts trailing hyphen after unicode label conversion");

    DomainMatcher matcher({
        DomainRule{
            .domain_id = 3,
            .root_domain_ascii = "strasse.de",
            .accept_exact = true,
            .accept_subdomains = true,
            .plus_addressing_mode = "keep",
            .local_part_case_sensitive = false,
        },
    });

    const std::string unicode_domain = "stra" "\xC3\x9F" "e.de";
    const DomainMatch match = require_match(matcher, "User@" + unicode_domain);

    test::check(match.domain_id == 3, "idna unicode domain id");
    test::check(match.domain_ascii == "strasse.de", "idna normalized domain");
    test::check(match.root_domain_ascii == "strasse.de", "idna normalized root domain");
    test::check(match.address_canonical == "user@strasse.de",
                "idna canonical address");

    DomainMatcher loose_idna_matcher({
        DomainRule{
            .domain_id = 9,
            .root_domain_ascii = "xn--maana--xwa.com",
            .accept_exact = true,
            .accept_subdomains = true,
            .plus_addressing_mode = "keep",
            .local_part_case_sensitive = false,
        },
    });

    const DomainMatch loose_idna_match = require_match(loose_idna_matcher,
                                                       "User@" + python_loose_domain);

    test::check(loose_idna_match.domain_id == 9, "python loose idna domain id");
    test::check(loose_idna_match.domain_ascii == "xn--maana--xwa.com",
                "python loose idna normalized domain");
    test::check(loose_idna_match.address_canonical == "user@xn--maana--xwa.com",
                "python loose idna canonical address");

    DomainMatcher idna_matcher({
        DomainRule{
            .domain_id = 4,
            .root_domain_ascii = "xn--fsqu00a.xn--0zwm56d",
            .accept_exact = true,
            .accept_subdomains = true,
            .plus_addressing_mode = "keep",
            .local_part_case_sensitive = false,
        },
    });

    const std::string chinese_example_domain =
        "\xE4\xBE\x8B\xE5\xAD\x90.\xE6\xB5\x8B\xE8\xAF\x95";
    const DomainMatch idna_match = require_match(idna_matcher, "Inbox@" + chinese_example_domain);

    test::check(idna_match.domain_id == 4, "python idna example domain id");
    test::check(idna_match.domain_ascii == "xn--fsqu00a.xn--0zwm56d",
                "python idna example normalized domain");
    test::check(idna_match.address_canonical == "inbox@xn--fsqu00a.xn--0zwm56d",
                "python idna example canonical address");
}
