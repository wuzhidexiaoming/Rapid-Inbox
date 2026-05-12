#pragma once

#include <optional>
#include <string>
#include <vector>

namespace rapid_inbox::ingestd {

struct ParsedAttachment {
    std::string attachment_id;
    int part_index = 0;
    std::optional<std::string> filename;
    std::string safe_filename;
    std::string content_type = "application/octet-stream";
    std::optional<std::string> content_disposition;
    std::optional<std::string> content_id;
    std::string storage_path;
    std::string sha256;
    std::string content;
    bool is_inline = false;
};

struct ParsedMail {
    std::optional<std::string> message_id_header;
    std::optional<std::string> subject;
    std::optional<std::string> from_name;
    std::optional<std::string> from_addr;
    std::optional<std::string> reply_to;
    std::optional<std::string> date_header;
    bool has_text = false;
    bool has_html = false;
    bool has_attachments = false;
    int attachment_count = 0;
    std::optional<std::string> text_preview;
    std::optional<std::string> text_body_path;
    std::optional<std::string> html_body_path;
    std::string text_body;
    std::string html_body;
    std::string headers_json = "[]";
    std::optional<std::string> verification_code;
    std::vector<ParsedAttachment> attachments;
};

struct ParseFailure {
    std::string message;
};

}
