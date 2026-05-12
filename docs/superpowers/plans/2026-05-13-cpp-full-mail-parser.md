# C++ Full Mail Parser Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move MIME parsing, body/attachment artifact generation, manifest parse metadata, and verification-code extraction into the C++ ingestd batch writer while keeping Python as the HTTP/admin layer and compatibility fallback.

**Architecture:** SMTP sessions remain lightweight and enqueue `MailJob`. `BatchWriter` parses each job in C++, writes `raw/text/html/attachments/manifest`, then writes SQLite rows as `parsed` or `failed` in one batch transaction. Python keeps recovery, retention, UI/API, and a fallback parser for old pending rows.

**Tech Stack:** C++20, SQLite3, OpenSSL SHA/RAND, existing ICU/unistring link set, Python 3.11+, FastAPI, pytest, CMake/CTest.

---

## File Structure

- Create `cpp/ingestd/src/parsed_mail.h`
  Defines parsed message and attachment value types used by parser and writer.

- Create `cpp/ingestd/src/mime_parser.h`
  Declares `MimeParser`, `ParseFailure`, and small decoding helpers that tests can exercise.

- Create `cpp/ingestd/src/mime_parser.cpp`
  Implements focused RFC822/MIME parsing for common verification emails, text/html bodies, multipart messages, base64, quoted-printable, folded headers, RFC2047 encoded words, and attachments.

- Create `cpp/ingestd/src/verification_code.h`
  Declares persisted-code extraction API.

- Create `cpp/ingestd/src/verification_code.cpp`
  Ports the current high-signal verification-code scoring behavior from Python.

- Modify `cpp/ingestd/src/storage_path.h`
  Add text/html/attachment storage path declarations.

- Modify `cpp/ingestd/src/storage_path.cpp`
  Add text/html/attachment path builders using the current dated layout.

- Modify `cpp/ingestd/src/batch_writer.h`
  Add private helpers for parsed artifact writes and parsed manifest JSON.

- Modify `cpp/ingestd/src/batch_writer.cpp`
  Parse jobs, write parsed artifacts, write parsed/failed manifest metadata, and insert parsed/failed SQLite rows.

- Modify `cpp/ingestd/CMakeLists.txt`
  Add new C++ sources and new test files.

- Modify `cpp/ingestd/tests/test_main.cpp`
  Register new parser, verification, and batch-writer tests.

- Create `cpp/ingestd/tests/test_mime_parser.cpp`
  Unit tests for MIME parser behavior.

- Create `cpp/ingestd/tests/test_verification_code.cpp`
  Unit tests for C++ verification-code extraction.

- Modify `cpp/ingestd/tests/test_batch_writer.cpp`
  Update writer tests from pending-only to parsed/failed persistence.

- Modify `sqlite_schema.sql`
  Add `messages.verification_code`.

- Modify `app/db/connection.py`
  Add lightweight migration for existing databases.

- Modify `app/ingest/parser.py`
  Add fallback-parser `verification_code` to `ParsedMessage`.

- Modify `app/runtime.py`
  Persist fallback `verification_code`, clear it on failure, read it in mailbox queries, and restore optional parsed manifests.

- Modify `app/services/messages.py`
  Return persisted verification codes instead of recomputing from storage.

- Modify `app/http/public_api.py`
  Add public verification-code shortcut endpoints.

- Modify `tests/test_ingest_pipeline.py`
  Cover Python fallback parser populating `verification_code`.

- Modify `tests/test_cpp_ingestd_integration.py`
  Cover C++ parsed rows and code visibility.

- Modify `tests/test_recovery.py`
  Cover parsed manifest recovery, failed parsed manifest recovery, and legacy manifest compatibility.

- Modify `tests/test_public_routes.py`
  Cover persisted-code public list/detail shortcuts.

---

### Task 1: Add `verification_code` Schema and Python Fallback Persistence

**Files:**
- Modify: `sqlite_schema.sql`
- Modify: `app/db/connection.py`
- Modify: `app/ingest/parser.py`
- Modify: `app/runtime.py`
- Modify: `tests/test_ingest_pipeline.py`

- [ ] **Step 1: Write failing schema/fallback tests**

Add this test to `tests/test_ingest_pipeline.py`:

```python
@pytest.mark.asyncio
async def test_python_fallback_parser_persists_verification_code(tmp_path) -> None:
    settings = Settings(
        storage_root=tmp_path / "storage",
        database_path=tmp_path / "storage" / "app.db",
    )
    runtime = RapidInboxRuntime(settings)

    await runtime.start()
    try:
        await runtime.create_domain("adb.com")
        result = await runtime.accept_message(
            rcpt_tos=["foo@adb.com"],
            envelope_from="noreply@openai.com",
            content=(
                b"From: OpenAI <noreply@openai.com>\r\n"
                b"To: foo@adb.com\r\n"
                b"Subject: Your OpenAI verification code\r\n"
                b"Content-Type: text/plain; charset=utf-8\r\n"
                b"\r\n"
                b"Your verification code is 654321.\r\n"
            ),
        )
        await runtime.drain_parser_queue()
    finally:
        await runtime.stop()

    message_id = result.removeprefix("250 queued as ")
    with connect_database(settings.database_path) as connection:
        row = connection.execute(
            "SELECT parse_status, verification_code FROM messages WHERE id = ?",
            (message_id,),
        ).fetchone()

    assert row["parse_status"] == "parsed"
    assert row["verification_code"] == "654321"
```

Add this migration test to the same file:

```python
def test_initialize_database_adds_verification_code_to_existing_messages_table(tmp_path) -> None:
    database_path = tmp_path / "storage" / "app.db"
    database_path.parent.mkdir(parents=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                smtp_session_id TEXT,
                raw_path TEXT NOT NULL UNIQUE,
                raw_sha256 TEXT NOT NULL,
                raw_size_bytes INTEGER NOT NULL,
                envelope_from TEXT,
                message_id_header TEXT,
                subject TEXT,
                from_name TEXT,
                from_addr TEXT,
                reply_to TEXT,
                date_header TEXT,
                received_at TEXT NOT NULL,
                indexed_at TEXT,
                parse_status TEXT NOT NULL DEFAULT 'pending',
                parse_error TEXT,
                has_text INTEGER NOT NULL DEFAULT 0,
                has_html INTEGER NOT NULL DEFAULT 0,
                has_attachments INTEGER NOT NULL DEFAULT 0,
                attachment_count INTEGER NOT NULL DEFAULT 0,
                text_preview TEXT,
                text_body_path TEXT,
                html_body_path TEXT,
                headers_json TEXT,
                is_deleted_globally INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        connection.commit()

    initialize_database(database_path)

    with connect_database(database_path) as connection:
        columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(messages)").fetchall()}
    assert "verification_code" in columns
```

Ensure imports include:

```python
import sqlite3
from app.db.connection import initialize_database
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
.venv/bin/pytest tests/test_ingest_pipeline.py::test_initialize_database_adds_verification_code_to_existing_messages_table tests/test_ingest_pipeline.py::test_python_fallback_parser_persists_verification_code -q
```

Expected: fails because `messages.verification_code` does not exist or is never populated.

- [ ] **Step 3: Add schema column**

In `sqlite_schema.sql`, add the column after `headers_json TEXT`:

```sql
    headers_json TEXT,
    verification_code TEXT,
    is_deleted_globally INTEGER NOT NULL DEFAULT 0 CHECK (is_deleted_globally IN (0, 1)),
```

- [ ] **Step 4: Add lightweight migration**

In `app/db/connection.py`, inside `_apply_lightweight_migrations`, after the admin migration block, add:

```python
    message_columns = _column_names(connection, "messages")
    if "verification_code" not in message_columns:
        connection.execute(
            """
            ALTER TABLE messages
            ADD COLUMN verification_code TEXT
            """
        )
```

- [ ] **Step 5: Extend fallback parser dataclass**

In `app/ingest/parser.py`, import the extractor:

```python
from app.services.verification_code import extract_verification_code
```

Add the field to `ParsedMessage`:

```python
    verification_code: str | None
```

In `MessageParser.parse_message`, compute the code before returning:

```python
        verification_code = extract_verification_code(
            subject=parsed.get("Subject"),
            sender=from_addr or None,
            text_body=text_body,
            html_body=html_body,
            preview=build_preview(text_body, html_body),
        )
```

Set the return field:

```python
            verification_code=verification_code,
```

- [ ] **Step 6: Persist and clear fallback parser code**

In `app/runtime.py`, update `_apply_parsed_message` SQL to include:

```sql
                headers_json = ?,
                verification_code = ?
```

and add `parsed.verification_code` before `message_id` in the parameter tuple.

In `_mark_message_parse_failed`, clear it:

```sql
                headers_json = NULL,
                verification_code = NULL
```

- [ ] **Step 7: Run focused tests**

Run:

```bash
.venv/bin/pytest tests/test_ingest_pipeline.py::test_initialize_database_adds_verification_code_to_existing_messages_table tests/test_ingest_pipeline.py::test_python_fallback_parser_persists_verification_code -q
```

Expected: both pass.

- [ ] **Step 8: Commit**

```bash
git add sqlite_schema.sql app/db/connection.py app/ingest/parser.py app/runtime.py tests/test_ingest_pipeline.py
git commit -m "feat: 持久化邮件验证码字段"
```

---

### Task 2: Add C++ Parsed Mail Types and Storage Paths

**Files:**
- Create: `cpp/ingestd/src/parsed_mail.h`
- Modify: `cpp/ingestd/src/storage_path.h`
- Modify: `cpp/ingestd/src/storage_path.cpp`
- Modify: `cpp/ingestd/tests/test_storage_utils.cpp`
- Modify: `cpp/ingestd/CMakeLists.txt`

- [ ] **Step 1: Extend storage path tests**

In `cpp/ingestd/tests/test_storage_utils.cpp`, add these assertions to `test_storage_paths_match_python_layout`:

```cpp
    test::check(rapid_inbox::ingestd::text_body_path("msg_abc", received_at) ==
                    "text/2026/05/12/msg_abc.txt",
                "text body path");
    test::check(rapid_inbox::ingestd::html_body_path("msg_abc", received_at) ==
                    "html/2026/05/12/msg_abc.html",
                "html body path");
    test::check(rapid_inbox::ingestd::attachment_path("msg_abc", "att_1", "report.txt") ==
                    "attachments/msg_abc/att_1-report.txt",
                "attachment path");
```

- [ ] **Step 2: Run C++ tests to verify failure**

Run:

```bash
cmake --build cpp/ingestd/build && ctest --test-dir cpp/ingestd/build --output-on-failure
```

Expected: compile fails because the new storage path functions do not exist.

- [ ] **Step 3: Create parsed mail value types**

Create `cpp/ingestd/src/parsed_mail.h`:

```cpp
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

}  // namespace rapid_inbox::ingestd
```

- [ ] **Step 4: Add storage path declarations**

In `cpp/ingestd/src/storage_path.h`, add:

```cpp
std::string text_body_path(const std::string& message_id, const std::string& received_at);
std::string html_body_path(const std::string& message_id, const std::string& received_at);
std::string attachment_path(const std::string& message_id,
                            const std::string& attachment_id,
                            const std::string& safe_filename);
```

- [ ] **Step 5: Add storage path implementations**

In `cpp/ingestd/src/storage_path.cpp`, add:

```cpp
std::string text_body_path(const std::string& message_id, const std::string& received_at) {
    return dated_path("text", message_id, received_at, ".txt");
}

std::string html_body_path(const std::string& message_id, const std::string& received_at) {
    return dated_path("html", message_id, received_at, ".html");
}

std::string attachment_path(const std::string& message_id,
                            const std::string& attachment_id,
                            const std::string& safe_name) {
    return "attachments/" + message_id + "/" + attachment_id + "-" + safe_name;
}
```

- [ ] **Step 6: Run C++ tests**

Run:

```bash
cmake --build cpp/ingestd/build && ctest --test-dir cpp/ingestd/build --output-on-failure
```

Expected: all current C++ tests pass.

- [ ] **Step 7: Commit**

```bash
git add cpp/ingestd/src/parsed_mail.h cpp/ingestd/src/storage_path.h cpp/ingestd/src/storage_path.cpp cpp/ingestd/tests/test_storage_utils.cpp cpp/ingestd/CMakeLists.txt
git commit -m "feat: 添加 C++ 解析产物路径"
```

---

### Task 3: Implement Focused C++ MIME Parser

**Files:**
- Create: `cpp/ingestd/src/mime_parser.h`
- Create: `cpp/ingestd/src/mime_parser.cpp`
- Create: `cpp/ingestd/tests/test_mime_parser.cpp`
- Modify: `cpp/ingestd/tests/test_main.cpp`
- Modify: `cpp/ingestd/CMakeLists.txt`

- [ ] **Step 1: Add MIME parser tests**

Create `cpp/ingestd/tests/test_mime_parser.cpp` with tests named:

```cpp
void test_mime_parser_text_only_message();
void test_mime_parser_html_only_message();
void test_mime_parser_multipart_alternative();
void test_mime_parser_attachment_base64();
void test_mime_parser_inline_related_part();
void test_mime_parser_decodes_quoted_printable_text();
void test_mime_parser_decodes_encoded_subject();
void test_mime_parser_reports_malformed_multipart();
```

Use concrete raw messages. The text-only test should assert:

```cpp
const auto parsed = rapid_inbox::ingestd::MimeParser().parse(
    "From: QA Sender <sender@example.com>\r\n"
    "To: foo@adb.com\r\n"
    "Subject: Hello Rapid Inbox\r\n"
    "Message-ID: <hello@example.com>\r\n"
    "Date: Wed, 13 May 2026 10:00:00 +0000\r\n"
    "Content-Type: text/plain; charset=utf-8\r\n"
    "\r\n"
    "Hello from C++ parser.\r\n");
test::check(parsed.subject.value_or("") == "Hello Rapid Inbox", "subject decoded");
test::check(parsed.from_addr.value_or("") == "sender@example.com", "from addr");
test::check(parsed.text_body.find("Hello from C++ parser.") != std::string::npos, "text body");
test::check(parsed.text_preview.value_or("").find("Hello from C++ parser.") == 0, "preview");
test::check(parsed.headers_json.find("[[\"From\"") != std::string::npos, "headers json");
```

The attachment test should include:

```cpp
test::check(parsed.attachments.size() == 1, "one attachment");
test::check(parsed.attachments[0].filename.value_or("") == "report.txt", "attachment filename");
test::check(parsed.attachments[0].content == "Quarterly report\n", "attachment content");
test::check(parsed.attachments[0].content_type == "text/plain", "attachment content type");
```

The malformed multipart test should assert `ParseFailure` is thrown:

```cpp
bool threw = false;
try {
    (void)rapid_inbox::ingestd::MimeParser().parse(
        "Subject: Broken\r\n"
        "Content-Type: multipart/mixed; boundary=\"missing\"\r\n"
        "\r\n"
        "body without boundary\r\n");
} catch (const rapid_inbox::ingestd::ParseFailure&) {
    threw = true;
}
test::check(threw, "malformed multipart throws ParseFailure");
```

Register all test functions in `cpp/ingestd/tests/test_main.cpp`.

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
cmake --build cpp/ingestd/build && ctest --test-dir cpp/ingestd/build --output-on-failure
```

Expected: compile fails because `mime_parser.h` does not exist.

- [ ] **Step 3: Add parser public API**

Create `cpp/ingestd/src/mime_parser.h`:

```cpp
#pragma once

#include "parsed_mail.h"

#include <stdexcept>
#include <string>

namespace rapid_inbox::ingestd {

class MimeParser {
public:
    ParsedMail parse(const std::string& raw_message) const;
};

std::string decode_base64(const std::string& value);
std::string decode_quoted_printable(const std::string& value);
std::string decode_rfc2047_words(const std::string& value);

}  // namespace rapid_inbox::ingestd
```

- [ ] **Step 4: Implement parser internals**

Create `cpp/ingestd/src/mime_parser.cpp` with these concrete responsibilities:

- Normalize line endings to `\n`.
- Split header block and body at the first blank line.
- Unfold headers where a line starts with space or tab.
- Store headers as ordered `(name, value)` pairs and build JSON array with `json_escape`.
- Parse content type parameters case-insensitively.
- Decode body content according to `Content-Transfer-Encoding`.
- For non-multipart `text/plain`, set `text_body`.
- For non-multipart `text/html`, set `html_body`.
- For multipart, split by exact boundary lines `--boundary` and `--boundary--`.
- Recursively parse each part.
- Treat first non-attachment `text/plain` as text body.
- Treat first non-attachment `text/html` as html body.
- Treat `Content-Disposition: attachment`, `inline`, or any part with `Content-ID` and decoded payload as an attachment unless it was selected as the first body.
- Generate `att_` ids with `make_prefixed_id("att_")`.
- Fill attachment SHA-256 with `sha256_hex(content)`.
- Generate `text_preview` by stripping HTML tags when no text body exists and collapsing whitespace.

The parser should throw `ParseFailure{"invalid multipart boundary"}` when a
multipart content type declares a boundary but no boundary segment appears.

- [ ] **Step 5: Add sources to CMake**

In `cpp/ingestd/CMakeLists.txt`, add `src/mime_parser.cpp` to `INGESTD_LIB_SOURCES` and `tests/test_mime_parser.cpp` to `ingestd_tests`.

- [ ] **Step 6: Run C++ parser tests**

Run:

```bash
cmake -S cpp/ingestd -B cpp/ingestd/build
cmake --build cpp/ingestd/build
ctest --test-dir cpp/ingestd/build --output-on-failure
```

Expected: all C++ tests pass.

- [ ] **Step 7: Commit**

```bash
git add cpp/ingestd/CMakeLists.txt cpp/ingestd/src/mime_parser.h cpp/ingestd/src/mime_parser.cpp cpp/ingestd/tests/test_mime_parser.cpp cpp/ingestd/tests/test_main.cpp
git commit -m "feat: 添加 C++ MIME 解析器"
```

---

### Task 4: Implement C++ Verification-Code Extraction

**Files:**
- Create: `cpp/ingestd/src/verification_code.h`
- Create: `cpp/ingestd/src/verification_code.cpp`
- Create: `cpp/ingestd/tests/test_verification_code.cpp`
- Modify: `cpp/ingestd/tests/test_main.cpp`
- Modify: `cpp/ingestd/CMakeLists.txt`

- [ ] **Step 1: Add C++ verification tests**

Create `cpp/ingestd/tests/test_verification_code.cpp` with functions:

```cpp
void test_verification_code_extracts_plain_six_digit_code();
void test_verification_code_extracts_chinese_code();
void test_verification_code_extracts_grouped_digit_code();
void test_verification_code_extracts_alphanumeric_code();
void test_verification_code_extracts_html_openai_code();
void test_verification_code_ignores_order_number();
void test_verification_code_ignores_ambiguous_two_codes();
```

The first test should assert:

```cpp
const auto code = rapid_inbox::ingestd::extract_verification_code(
    "Your verification code",
    "noreply@example.com",
    "Your verification code is 482913. It expires in 10 minutes.",
    "",
    "");
test::check(code.has_value() && *code == "482913", "plain six digit code");
```

The ambiguous test should assert no code:

```cpp
const auto code = rapid_inbox::ingestd::extract_verification_code(
    "Verification code candidates",
    "sender@example.com",
    "Your verification code could be 123456 or 654321 depending on region.",
    "",
    "");
test::check(!code.has_value(), "ambiguous code abstains");
```

Register all functions in `cpp/ingestd/tests/test_main.cpp`.

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
cmake --build cpp/ingestd/build && ctest --test-dir cpp/ingestd/build --output-on-failure
```

Expected: compile fails because `verification_code.h` does not exist.

- [ ] **Step 3: Add extractor API**

Create `cpp/ingestd/src/verification_code.h`:

```cpp
#pragma once

#include <optional>
#include <string>

namespace rapid_inbox::ingestd {

std::optional<std::string> extract_verification_code(const std::string& subject,
                                                     const std::string& sender,
                                                     const std::string& text_body,
                                                     const std::string& html_body,
                                                     const std::string& preview);

}  // namespace rapid_inbox::ingestd
```

- [ ] **Step 4: Port scoring behavior**

Create `cpp/ingestd/src/verification_code.cpp` with these concrete rules:

- Convert HTML to text by removing `<script>`, `<style>`, tags, and common attributes.
- Normalize whitespace.
- Require verification context from subject/body/sender using hints including:
  `验证码`, `确认码`, `登录码`, `verification code`, `security code`,
  `login code`, `sign-in code`, `one-time code`, `otp`, `passcode`,
  `confirmation code`, `temporary code`, `verify your email`.
- Strip URLs, emails, currency values, years, and digit runs of 9 or more.
- Enumerate candidates:
  - pure digits with length 4 to 8,
  - grouped digits like `123-456` and `12 34 56`,
  - alphanumeric tokens length 4 to 10 with at least one digit.
- Score 6-digit candidates highest, then 4/5/7/8 digit, then alphanumeric.
- Add proximity points when a verification hint occurs within 48 characters.
- Add points when the candidate appears in the subject.
- Add points for candidate on its own line or surrounded by strong/simple markup text after HTML conversion.
- Reject when two different candidates tie within a small margin.
- Reject explicit disjunctions like `<code> or <code>` and `<code> 或 <code>`.

Return uppercase alphanumeric codes and grouped digit codes without separators.

- [ ] **Step 5: Add sources to CMake**

In `cpp/ingestd/CMakeLists.txt`, add `src/verification_code.cpp` to `INGESTD_LIB_SOURCES` and `tests/test_verification_code.cpp` to `ingestd_tests`.

- [ ] **Step 6: Run C++ verification tests**

Run:

```bash
cmake -S cpp/ingestd -B cpp/ingestd/build
cmake --build cpp/ingestd/build
ctest --test-dir cpp/ingestd/build --output-on-failure
```

Expected: all C++ tests pass.

- [ ] **Step 7: Commit**

```bash
git add cpp/ingestd/CMakeLists.txt cpp/ingestd/src/verification_code.h cpp/ingestd/src/verification_code.cpp cpp/ingestd/tests/test_verification_code.cpp cpp/ingestd/tests/test_main.cpp
git commit -m "feat: 添加 C++ 验证码提取"
```

---

### Task 5: Make BatchWriter Persist Parsed Artifacts and Parsed Rows

**Files:**
- Modify: `cpp/ingestd/src/batch_writer.h`
- Modify: `cpp/ingestd/src/batch_writer.cpp`
- Modify: `cpp/ingestd/tests/test_batch_writer.cpp`

- [ ] **Step 1: Update batch-writer tests**

In `cpp/ingestd/tests/test_batch_writer.cpp`, rename
`test_batch_writer_writes_sqlite_pending_records` to
`test_batch_writer_writes_sqlite_parsed_records`.

Change its message query to:

```cpp
auto message = db.prepare(
    "SELECT parse_status, raw_path, envelope_from, subject, text_preview, "
    "text_body_path, html_body_path, verification_code "
    "FROM messages WHERE id = 'msg_1'");
```

Assert:

```cpp
test::check(std::string(reinterpret_cast<const char*>(sqlite3_column_text(message.get(), 0))) ==
                "parsed",
            "message parsed");
test::check(std::string(reinterpret_cast<const char*>(sqlite3_column_text(message.get(), 3))) ==
                "Hello",
            "message subject");
test::check(std::string(reinterpret_cast<const char*>(sqlite3_column_text(message.get(), 4)))
                .find("Your verification code is 123456") == 0,
            "message preview");
test::check(std::string(reinterpret_cast<const char*>(sqlite3_column_text(message.get(), 7))) ==
                "123456",
            "message verification code");
```

Update `sample_job().raw_content` to:

```cpp
job.raw_content =
    "From: Sender <sender@example.com>\r\n"
    "To: code@adb.com\r\n"
    "Subject: Hello\r\n"
    "Content-Type: text/plain; charset=utf-8\r\n"
    "\r\n"
    "Your verification code is 123456.\r\n";
```

Add assertions that `root / text_body_path` exists and contains the body.

Add a new test `test_batch_writer_marks_parse_failure_without_rejecting_raw` using a multipart message with a missing boundary. Assert:

```cpp
SELECT parse_status, parse_error, text_body_path, verification_code
FROM messages WHERE id = 'msg_1'
```

has `parse_status='failed'`, non-null `parse_error`, null `text_body_path`, and null `verification_code`, while raw and manifest files exist.

Register the renamed and new tests in `cpp/ingestd/tests/test_main.cpp`.

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
cmake --build cpp/ingestd/build && ctest --test-dir cpp/ingestd/build --output-on-failure
```

Expected: batch writer tests fail because rows are still pending.

- [ ] **Step 3: Extend BatchWriter helpers**

In `cpp/ingestd/src/batch_writer.h`, add private helpers:

```cpp
    void write_parsed_artifacts(const MailJob& job, ParsedMail& parsed) const;
    void write_storage_artifacts(const std::vector<MailJob>& jobs,
                                 std::vector<std::variant<ParsedMail, ParseFailure>>& parse_results) const;
    std::string build_manifest(const MailJob& job, const ParsedMail* parsed, const ParseFailure* failure) const;
    void write_sqlite_records(const std::vector<MailJob>& jobs,
                              const std::vector<std::variant<ParsedMail, ParseFailure>>& parse_results) const;
```

Include:

```cpp
#include "parsed_mail.h"
#include <variant>
```

- [ ] **Step 4: Parse and write artifacts before SQLite transaction**

In `BatchWriter::write_batch`, replace the current two-call flow with:

```cpp
std::vector<std::variant<ParsedMail, ParseFailure>> parse_results;
parse_results.reserve(jobs.size());
for (const MailJob& job : jobs) {
    try {
        ParsedMail parsed = MimeParser().parse(job.raw_content);
        parsed.verification_code = extract_verification_code(
            parsed.subject.value_or(""),
            parsed.from_addr.value_or(""),
            parsed.text_body,
            parsed.html_body,
            parsed.text_preview.value_or(""));
        parse_results.emplace_back(std::move(parsed));
    } catch (const ParseFailure& failure) {
        parse_results.emplace_back(failure);
    }
}
write_storage_artifacts(jobs, parse_results);
write_sqlite_records(jobs, parse_results);
```

Implement `write_storage_artifacts(jobs, parse_results)` so each job writes
manifest first, raw second, and parsed text/html/attachment artifacts third.
That keeps the existing recovery-first storage order while allowing the
manifest JSON to include the optional `parsed` object.

- [ ] **Step 5: Write parsed files**

Implement `write_parsed_artifacts`:

```cpp
void BatchWriter::write_parsed_artifacts(const MailJob& job, ParsedMail& parsed) const {
    if (!parsed.text_body.empty()) {
        parsed.has_text = true;
        parsed.text_body_path = text_body_path(job.message_id, job.received_at);
        write_file_atomic(*parsed.text_body_path, parsed.text_body);
    }
    if (!parsed.html_body.empty()) {
        parsed.has_html = true;
        parsed.html_body_path = html_body_path(job.message_id, job.received_at);
        write_file_atomic(*parsed.html_body_path, parsed.html_body);
    }
    for (ParsedAttachment& attachment : parsed.attachments) {
        if (attachment.attachment_id.empty()) {
            attachment.attachment_id = make_prefixed_id("att_");
        }
        attachment.safe_filename = safe_filename(attachment.filename.value_or("attachment.bin"));
        attachment.storage_path = attachment_path(job.message_id, attachment.attachment_id, attachment.safe_filename);
        attachment.sha256 = sha256_hex(attachment.content);
        write_file_atomic(attachment.storage_path, attachment.content);
    }
    parsed.attachment_count = static_cast<int>(parsed.attachments.size());
    parsed.has_attachments = parsed.attachment_count > 0;
}
```

- [ ] **Step 6: Insert parsed and failed rows**

Update `write_sqlite_records` so the message insert includes all parsed columns.
For parsed results, bind parsed fields and `parse_status='parsed'`. For
failures, bind `parse_status='failed'`, `parse_error`, null body/header/code
fields, and zero attachment counts.

Use prepared insert SQL shaped like:

```sql
INSERT INTO messages (
    id, smtp_session_id, raw_path, raw_sha256, raw_size_bytes,
    envelope_from, from_addr, received_at, indexed_at, parse_status, parse_error,
    message_id_header, subject, from_name, reply_to, date_header,
    has_text, has_html, has_attachments, attachment_count,
    text_preview, text_body_path, html_body_path, headers_json, verification_code
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
```

Insert attachments for parsed results only:

```sql
INSERT INTO attachments (
    id, message_id, part_index, filename, safe_filename, content_type,
    content_disposition, content_id, storage_path, sha256, size_bytes,
    is_inline, created_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
```

- [ ] **Step 7: Include parsed object in manifest**

Extend `build_manifest` so it emits:

```json
"parsed":{"status":"parsed", ...}
```

or:

```json
"parsed":{"status":"failed","parse_error":"..."}
```

Keep the existing top-level fields unchanged.

- [ ] **Step 8: Run C++ tests**

Run:

```bash
cmake -S cpp/ingestd -B cpp/ingestd/build
cmake --build cpp/ingestd/build
ctest --test-dir cpp/ingestd/build --output-on-failure
```

Expected: all C++ tests pass.

- [ ] **Step 9: Commit**

```bash
git add cpp/ingestd/src/batch_writer.h cpp/ingestd/src/batch_writer.cpp cpp/ingestd/tests/test_batch_writer.cpp cpp/ingestd/tests/test_main.cpp
git commit -m "feat: C++ 写入解析后的邮件"
```

---

### Task 6: Recover Parsed and Failed Manifests in Python

**Files:**
- Modify: `app/runtime.py`
- Modify: `tests/test_recovery.py`

- [ ] **Step 1: Add parsed-manifest recovery tests**

In `tests/test_recovery.py`, add:

```python
@pytest.mark.asyncio
async def test_recovery_scanner_restores_parsed_manifest_without_reparse(tmp_path) -> None:
    settings = Settings(
        storage_root=tmp_path / "storage",
        database_path=tmp_path / "storage" / "app.db",
    )
    runtime = RapidInboxRuntime(settings)

    await runtime.start()
    try:
        await runtime.create_domain("adb.com")
        response = await runtime.accept_message(
            rcpt_tos=["foo@adb.com"],
            envelope_from="noreply@openai.com",
            content=(
                b"From: OpenAI <noreply@openai.com>\r\n"
                b"To: foo@adb.com\r\n"
                b"Subject: Your OpenAI verification code\r\n"
                b"Content-Type: text/plain; charset=utf-8\r\n"
                b"\r\n"
                b"Your verification code is 654321.\r\n"
            ),
        )
        await runtime.drain_parser_queue()
    finally:
        await runtime.stop()

    message_id = response.removeprefix("250 queued as ")
    manifest_path = next(settings.manifests_dir.rglob("*.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["parsed"] = {
        "status": "parsed",
        "message_id_header": None,
        "subject": "Recovered parsed subject",
        "from_name": "OpenAI",
        "from_addr": "noreply@openai.com",
        "reply_to": None,
        "date_header": None,
        "has_text": True,
        "has_html": False,
        "has_attachments": False,
        "attachment_count": 0,
        "text_preview": "Your verification code is 654321.",
        "text_body_path": None,
        "html_body_path": None,
        "headers_json": [["Subject", "Recovered parsed subject"]],
        "verification_code": "654321",
        "attachments": [],
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8")

    with connect_database(settings.database_path) as connection:
        connection.execute("DELETE FROM message_deliveries")
        connection.execute("DELETE FROM messages")
        connection.execute("DELETE FROM mailboxes")
        connection.commit()

    recovered = RapidInboxRuntime(settings)
    await recovered.start()
    try:
        await recovered.drain_parser_queue()
    finally:
        await recovered.stop()

    with connect_database(settings.database_path) as connection:
        row = connection.execute(
            "SELECT parse_status, subject, verification_code FROM messages WHERE id = ?",
            (message_id,),
        ).fetchone()

    assert row["parse_status"] == "parsed"
    assert row["subject"] == "Recovered parsed subject"
    assert row["verification_code"] == "654321"
```

Add a second test for `parsed.status='failed'` asserting `parse_status='failed'`
and `parse_error` is restored.

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
.venv/bin/pytest tests/test_recovery.py::test_recovery_scanner_restores_parsed_manifest_without_reparse -q
```

Expected: fails because recovery ignores `parsed`.

- [ ] **Step 3: Validate optional parsed manifest**

In `RapidInboxRuntime.validate_recovery_manifest`, after existing recipient
validation, add validation for optional `parsed`:

```python
        parsed = manifest.get("parsed")
        if parsed is not None:
            self._validate_recovery_parsed_manifest(parsed)
```

Add `_validate_recovery_parsed_manifest`:

```python
    def _validate_recovery_parsed_manifest(self, parsed: Any) -> None:
        if not isinstance(parsed, dict):
            raise ValueError("invalid recovery manifest")
        status = parsed.get("status")
        if status not in {"parsed", "failed"}:
            raise ValueError("invalid recovery manifest")
        if status == "failed":
            if not isinstance(parsed.get("parse_error"), str) or not parsed["parse_error"]:
                raise ValueError("invalid recovery manifest")
            return
        for key in ("has_text", "has_html", "has_attachments"):
            if not isinstance(parsed.get(key), bool):
                raise ValueError("invalid recovery manifest")
        if not isinstance(parsed.get("attachment_count"), int) or isinstance(parsed.get("attachment_count"), bool):
            raise ValueError("invalid recovery manifest")
        if not isinstance(parsed.get("headers_json"), list):
            raise ValueError("invalid recovery manifest")
        attachments = parsed.get("attachments")
        if not isinstance(attachments, list):
            raise ValueError("invalid recovery manifest")
        for attachment in attachments:
            if not isinstance(attachment, dict):
                raise ValueError("invalid recovery manifest")
            for key in ("id", "storage_path", "safe_filename", "content_type"):
                if not isinstance(attachment.get(key), str):
                    raise ValueError("invalid recovery manifest")
            self.storage.resolve(str(attachment["storage_path"]))
```

- [ ] **Step 4: Apply parsed manifest to message rows**

In `_apply_recovery_manifest`, read:

```python
        parsed = manifest.get("parsed")
```

If `parsed.status == "parsed"`, insert message fields as parsed instead of
pending. If `parsed.status == "failed"`, insert message as failed.

For parsed headers, store JSON text:

```python
headers_json = json.dumps(parsed.get("headers_json") or [], ensure_ascii=False)
```

Insert attachments from `parsed["attachments"]` after delivery rows are created.

Keep legacy behavior unchanged when `parsed` is absent.

- [ ] **Step 5: Run recovery tests**

Run:

```bash
.venv/bin/pytest tests/test_recovery.py -q
```

Expected: all recovery tests pass.

- [ ] **Step 6: Commit**

```bash
git add app/runtime.py tests/test_recovery.py
git commit -m "feat: 恢复 C++ 解析清单"
```

---

### Task 7: Return Persisted Codes and Add Public Code APIs

**Files:**
- Modify: `app/runtime.py`
- Modify: `app/services/messages.py`
- Modify: `app/http/public_api.py`
- Modify: `tests/test_public_routes.py`

- [ ] **Step 1: Add public API tests**

In `tests/test_public_routes.py`, add:

```python
@pytest.mark.asyncio
async def test_public_api_lists_mailbox_verification_codes(app_client, runtime) -> None:
    await runtime.create_domain("adb.com")
    public_key = await runtime.api_keys.create_key(
        {
            "name": "public",
            "kind": "public",
            "scopes": ["public.read"],
            "grants": {"all_domains": True},
        }
    )
    await runtime.accept_message(
        rcpt_tos=["foo@adb.com"],
        envelope_from="noreply@openai.com",
        content=_rich_mail_bytes(
            subject="Your OpenAI verification code",
            message_id="code-list@example.com",
            from_addr="OpenAI <noreply@openai.com>",
            body="Your verification code is 654321.",
        ),
    )
    await runtime.drain_parser_queue()

    response = await app_client.get(
        "/api/v1/public/mailboxes/foo@adb.com/verification-codes",
        headers={"X-API-Key": public_key["plain_text"]},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["mailbox"] == "foo@adb.com"
    assert payload["items"][0]["verification_code"] == "654321"
    assert payload["items"][0]["received_at"]
```

Add:

```python
@pytest.mark.asyncio
async def test_public_api_gets_single_message_verification_code(app_client, runtime) -> None:
    await runtime.create_domain("adb.com")
    public_key = await runtime.api_keys.create_key(
        {
            "name": "public",
            "kind": "public",
            "scopes": ["public.read"],
            "grants": {"all_domains": True},
        }
    )
    await runtime.accept_message(
        rcpt_tos=["foo@adb.com"],
        envelope_from="noreply@openai.com",
        content=_rich_mail_bytes(
            subject="Your OpenAI verification code",
            message_id="code-detail@example.com",
            from_addr="OpenAI <noreply@openai.com>",
            body="Your verification code is 482951.",
        ),
    )
    await runtime.drain_parser_queue()
    mailbox = await runtime.get_mailbox_view("foo@adb.com")
    delivery_id = mailbox["items"][0]["delivery_id"]

    response = await app_client.get(
        f"/api/v1/public/mailboxes/foo@adb.com/messages/{delivery_id}/verification-code",
        headers={"X-API-Key": public_key["plain_text"]},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["delivery_id"] == delivery_id
    assert payload["verification_code"] == "482951"
```

- [ ] **Step 2: Run API tests to verify failure**

Run:

```bash
.venv/bin/pytest tests/test_public_routes.py::test_public_api_lists_mailbox_verification_codes tests/test_public_routes.py::test_public_api_gets_single_message_verification_code -q
```

Expected: 404 because the new routes do not exist.

- [ ] **Step 3: Include code in mailbox queries**

In `RapidInboxRuntime.get_mailbox_view` and `get_mailbox_delivery_item`, add
`m.verification_code` to SELECT fields.

In `get_delivery_detail`, include `m.verification_code` and return:

```python
            "verification_code": row["verification_code"],
```

- [ ] **Step 4: Add runtime code query helpers**

Add methods to `RapidInboxRuntime`:

```python
    async def list_mailbox_verification_codes(
        self,
        mailbox_address: str,
        *,
        limit: int = 50,
        offset: int = 0,
        request_ip: str | None = None,
    ) -> dict[str, Any]:
        mailbox = await self.get_mailbox_view(
            mailbox_address,
            limit=limit,
            offset=offset,
            request_ip=request_ip,
        )
        items = [
            {
                "delivery_id": item["delivery_id"],
                "message_id": item["message_id"],
                "received_at": item["delivered_at"],
                "subject": item.get("subject"),
                "from_addr": item.get("from_addr"),
                "parse_status": item.get("parse_status"),
                "verification_code": item.get("verification_code"),
            }
            for item in mailbox["items"]
        ]
        return {**mailbox, "items": items}

    async def get_delivery_verification_code(
        self,
        mailbox_address: str,
        delivery_id: str,
        *,
        request_ip: str | None = None,
    ) -> dict[str, Any]:
        item = await self.get_mailbox_delivery_item(
            mailbox_address,
            delivery_id,
            request_ip=request_ip,
        )
        return {
            "delivery_id": item["delivery_id"],
            "message_id": item["message_id"],
            "received_at": item["delivered_at"],
            "parse_status": item["parse_status"],
            "verification_code": item.get("verification_code"),
        }
```

- [ ] **Step 5: Stop recomputing codes in MessageService**

In `app/services/messages.py`, change `_prepare_public_mailbox_item`:

```python
        if surface == "web":
            payload["verification_code"] = payload.get("verification_code")
```

Remove the call to `_extract_verification_code` from that method. Leave
`_extract_verification_code` in place only if admin/fallback code still imports
it; remove unused imports after running lint or tests.

- [ ] **Step 6: Add public API routes**

In `app/http/public_api.py`, add:

```python
@router.get("/api/v1/public/mailboxes/{mailbox_address}/verification-codes")
async def list_mailbox_verification_codes(
    mailbox_address: str,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    api_key: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0, le=1_000_000),
) -> dict:
    require_public_api_key(request, x_api_key, api_key)
    request_ip = request.client.host if request.client is not None else None
    try:
        return await request.app.state.runtime.list_mailbox_verification_codes(
            mailbox_address,
            limit=limit,
            offset=offset,
            request_ip=request_ip,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        set_active_permission_context(None)


@router.get("/api/v1/public/mailboxes/{mailbox_address}/messages/{delivery_id}/verification-code")
async def get_mailbox_message_verification_code(
    mailbox_address: str,
    delivery_id: str,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    api_key: str | None = Query(default=None),
) -> dict:
    require_public_api_key(request, x_api_key, api_key)
    request_ip = request.client.host if request.client is not None else None
    try:
        return await request.app.state.runtime.get_delivery_verification_code(
            mailbox_address,
            delivery_id,
            request_ip=request_ip,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        set_active_permission_context(None)
```

- [ ] **Step 7: Run API tests**

Run:

```bash
.venv/bin/pytest tests/test_public_routes.py -q
```

Expected: all public route tests pass.

- [ ] **Step 8: Commit**

```bash
git add app/runtime.py app/services/messages.py app/http/public_api.py tests/test_public_routes.py
git commit -m "feat: 添加验证码查询接口"
```

---

### Task 8: C++ Ingestd Integration and Documentation

**Files:**
- Modify: `tests/test_cpp_ingestd_integration.py`
- Modify: `cpp/ingestd/README.md`
- Modify: `README.md`

- [ ] **Step 1: Update C++ integration tests**

In `tests/test_cpp_ingestd_integration.py`, after the mailbox reaches one
message in `test_cpp_ingestd_accepts_mail_and_python_reads_it`, assert:

```python
assert mailbox["items"][0]["parse_status"] == "parsed"
assert mailbox["items"][0]["verification_code"] == "123456"
detail = await runtime.get_delivery_detail("code@adb.com", mailbox["items"][0]["delivery_id"])
assert detail["text_body"].strip() == "Your code is 123456"
assert detail["verification_code"] == "123456"
```

Add an attachment integration test that sends a message with one `report.txt`
attachment through C++ SMTP and asserts Python detail includes the attachment
and can read its content through `AttachmentService`.

- [ ] **Step 2: Run integration tests**

Run:

```bash
cmake -S cpp/ingestd -B cpp/ingestd/build
cmake --build cpp/ingestd/build
.venv/bin/pytest tests/test_cpp_ingestd_integration.py -q
```

Expected: all C++ ingestd integration tests pass.

- [ ] **Step 3: Update C++ README**

In `cpp/ingestd/README.md`, replace the phase note with:

```markdown
The ingest process accepts SMTP mail, queues it in memory, and batch-writes raw
mail, recovery manifests, parsed text/html bodies, attachments, verification
codes, and SQLite message/delivery rows. Python remains the HTTP/admin service
and compatibility parser for old pending rows.
```

- [ ] **Step 4: Update root README operational notes**

In `README.md`, ensure the quickstart/deployment section says:

```markdown
The default quickstart starts the C++ SMTP ingest process on `0.0.0.0:25`.
Parsed message metadata, text/html bodies, attachments, and verification codes
are written by ingestd directly into the existing SQLite database and
`storage/` tree. The Python service serves HTTP/admin/public APIs.
```

- [ ] **Step 5: Run broad validation**

Run:

```bash
cmake -S cpp/ingestd -B cpp/ingestd/build
cmake --build cpp/ingestd/build
ctest --test-dir cpp/ingestd/build --output-on-failure
.venv/bin/pytest tests/test_ingest_pipeline.py tests/test_recovery.py tests/test_public_routes.py tests/test_cpp_ingestd_integration.py -q
git diff --check
```

Expected: all commands pass.

- [ ] **Step 6: Commit**

```bash
git add tests/test_cpp_ingestd_integration.py cpp/ingestd/README.md README.md
git commit -m "docs: 更新 C++ 解析部署说明"
```

---

### Task 9: Final Full Test Pass and Local Smoke

**Files:**
- No planned source edits.

- [ ] **Step 1: Run full Python test suite**

Run:

```bash
.venv/bin/pytest -q
```

Expected: all tests pass.

- [ ] **Step 2: Run full C++ test suite**

Run:

```bash
cmake -S cpp/ingestd -B cpp/ingestd/build
cmake --build cpp/ingestd/build
ctest --test-dir cpp/ingestd/build --output-on-failure
```

Expected: all C++ tests pass.

- [ ] **Step 3: Run quick SMTP smoke against local test port**

Run:

```bash
.venv/bin/pytest tests/test_cpp_ingestd_integration.py::test_cpp_ingestd_accepts_mail_and_python_reads_it -q
```

Expected: smoke passes and verifies parsed/code visibility.

- [ ] **Step 4: Inspect git status**

Run:

```bash
git status --short --branch
```

Expected: branch is ahead by implementation commits with no uncommitted changes.

- [ ] **Step 5: Final commit only if validation fixes changed files**

If Step 1-3 required small fixes, stage changed tracked files and commit them:

```bash
git add -u
git commit -m "test: 验证 C++ 邮件解析链路"
```

If `git add -u` stages nothing, do not create an empty commit. If a validation
fix creates a new file, run `git status --short`, stage the exact new file path
shown there, and then run the same commit command.

---

## Self-Review Notes

- Spec coverage:
  - C++ writer-stage parsing: Tasks 3 and 5.
  - raw/text/html/attachment storage: Tasks 2, 3, and 5.
  - manifest parsed metadata: Tasks 5 and 6.
  - persisted verification code: Tasks 1, 4, 5, and 7.
  - Python compatibility fallback: Tasks 1 and 6.
  - public verification-code API: Task 7.
  - integration and docs: Tasks 8 and 9.

- Type consistency:
  - C++ uses `ParsedMail::verification_code` and SQLite uses `messages.verification_code`.
  - Manifest optional object uses `parsed.status`.
  - Public API response uses `verification_code`.

- Execution mode:
  - Use subagent-driven development where each task is reviewed before the next task starts.
