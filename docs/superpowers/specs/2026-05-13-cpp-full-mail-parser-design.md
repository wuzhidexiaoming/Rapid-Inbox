# C++ Full Mail Parser Design

## Context

Rapid Inbox already uses the C++ `rapid-inbox-ingestd` process for SMTP intake and
batch persistence. The current hot-path split is still incomplete:

- C++ accepts SMTP mail and writes raw mail, recovery manifest, and pending
  SQLite rows.
- Python then parses pending messages with `app/ingest/parser.py`.
- Python computes `verification_code` on public web reads in
  `app/services/messages.py`.

The next phase moves MIME parsing, body extraction, attachment extraction, body
artifact writes, manifest parse metadata, and verification-code extraction into
C++. Python remains the HTTP/admin layer and a compatibility fallback for old
or failed rows.

The selected approach is Scheme A: parse in the C++ batch writer stage.

## Goals

- Keep SMTP command/session handling lightweight.
- Make returned mail visible in roughly 500 ms to 1 s under normal load.
- Write directly to the existing SQLite database and `storage/` layout.
- Persist `raw`, `text`, `html`, attachments, and manifest artifacts from C++.
- Persist verification codes so read paths do not re-parse mail bodies.
- Preserve normal restart behavior: SIGTERM/SIGINT drains returned-250 mail.
- Allow process-crash loss only for messages still in ingestd memory, matching
  the current durability contract.
- Keep old manifests and existing pending/failed rows recoverable.

## Non-Goals

- Replace the Python HTTP/admin service.
- Build a full mail-server feature set such as SMTP auth, DKIM verification, or
  spam filtering.
- Guarantee perfect parsing of every malformed MIME message in the first C++
  implementation.
- Add a separate parser service process.

## Architecture

SMTP sessions stay simple. `cpp/ingestd/src/smtp_session.cpp` continues to
collect `DATA`, validate recipients through `DomainMatcher`, and enqueue
`MailJob`.

`cpp/ingestd/src/batch_writer.cpp` becomes the parse-and-persist boundary. For
each queued job, the writer:

1. Writes the recovery manifest first.
2. Writes raw `.eml`.
3. Parses MIME in C++.
4. Writes text/html/attachment artifacts.
5. Writes SQLite rows in one batch transaction.

Parsing does not run inside the SMTP session object. If parsing becomes slower
than intake, the queue and batch writer absorb that pressure without blocking
line-level SMTP command processing.

## New C++ Modules

Create focused C++ files under `cpp/ingestd/src/`:

- `parsed_mail.h`
  Defines `ParsedMail`, `ParsedAttachment`, and `ParseError`.

- `mime_parser.h` / `mime_parser.cpp`
  Parses RFC822 headers and common MIME structures:
  `text/plain`, `text/html`, `multipart/alternative`, `multipart/mixed`,
  `multipart/related`, `base64`, `quoted-printable`, folded headers, common
  RFC2047 encoded words, filenames, content IDs, and inline parts.

- `verification_code.h` / `verification_code.cpp`
  Ports the current Python scoring strategy from
  `app/services/verification_code.py` into C++. It should preserve the common
  happy paths and false-positive guards already covered by
  `tests/test_verification_code.py`.

- Extend `storage_path.h` / `storage_path.cpp`
  Add `text_body_path(message_id, received_at)`,
  `html_body_path(message_id, received_at)`, and
  `attachment_path(message_id, attachment_id, safe_filename)`.

The C++ implementation should avoid adding a deployment dependency unless it is
already reliably available. Local inspection found SQLite/OpenSSL/ICU/unistring
available but no installed GMime/libetpan/mimetic package, so the first
implementation should use a focused internal parser.

## Storage Contract

The existing storage layout stays intact:

- Raw mail: `storage/raw/YYYY/MM/DD/<message_id>.eml`
- Text body: `storage/text/YYYY/MM/DD/<message_id>.txt`
- HTML body: `storage/html/YYYY/MM/DD/<message_id>.html`
- Attachments: `storage/attachments/<message_id>/<attachment_id>-<safe_name>`
- Manifest: `storage/manifests/YYYY/MM/DD/<message_id>.json`

C++ must use the same atomic write discipline as the current writer: write a
private temp file inside the target directory, fsync when configured, rename to
the final path, chmod private, and fsync directories when configured.

Attachment filenames use the existing safe filename policy:

- Keep ASCII letters, digits, `.`, `_`, and `-`.
- Replace other runs with `_`.
- Trim leading/trailing `.` and `_`.
- Fall back to `attachment.bin`.

Inline parts with a content ID and no filename get synthesized names similar to
the Python parser, for example `inline-7.png` when the content type maps to a
known extension.

## SQLite Contract

Add one column to `messages`:

```sql
verification_code TEXT
```

Update `sqlite_schema.sql` and `app/db/connection.py` lightweight migrations so
existing databases receive the column on startup.

When C++ parsing succeeds, `messages` is inserted as `parse_status='parsed'`
with these fields populated:

- `message_id_header`
- `subject`
- `from_name`
- `from_addr`
- `reply_to`
- `date_header`
- `indexed_at`
- `has_text`
- `has_html`
- `has_attachments`
- `attachment_count`
- `text_preview`
- `text_body_path`
- `html_body_path`
- `headers_json`
- `verification_code`

The `attachments` table is populated in the same SQLite transaction, ordered by
MIME part index.

When C++ parsing fails, the message is still accepted and retained:

- Manifest and raw file stay written.
- `messages.parse_status='failed'`.
- `messages.parse_error` stores a concise parser error.
- Text/html/attachment fields are null or zero.
- No SMTP rejection is sent for parser errors after queue acceptance.

This is the approved failure policy.

## Manifest Contract

All existing recovery fields remain mandatory:

- `message_id`
- `smtp_session_id`
- `envelope_from`
- `rcpt_tos`
- `recipients`
- `received_at`
- `raw_path`
- `raw_sha256`
- `raw_size_bytes`

Add optional `parsed` metadata:

```json
{
  "status": "parsed",
  "message_id_header": "<...>",
  "subject": "Your code",
  "from_name": "Example",
  "from_addr": "noreply@example.com",
  "reply_to": null,
  "date_header": "Wed, 13 May 2026 10:00:00 +0000",
  "has_text": true,
  "has_html": false,
  "has_attachments": false,
  "attachment_count": 0,
  "text_preview": "Your code is 123456",
  "text_body_path": "text/2026/05/13/msg_x.txt",
  "html_body_path": null,
  "headers_json": [["Subject", "Your code"]],
  "verification_code": "123456",
  "attachments": []
}
```

For parse failures:

```json
{
  "status": "failed",
  "parse_error": "invalid multipart boundary"
}
```

Python recovery keeps accepting old manifests without `parsed`. Old manifests
recover as `pending` and use the Python fallback parser. New manifests with
`parsed.status='parsed'` recover directly into parsed SQLite rows and
attachments. New manifests with `parsed.status='failed'` recover directly into a
failed row.

## Python Runtime Changes

Python remains responsible for:

- HTTP/admin/public routes.
- Authentication, API keys, public-surface access checks.
- Retention cleanup.
- Recovery orchestration.
- Reading raw/body/attachment files for UI and API responses.

Python no longer computes verification codes during public web list rendering.
`MessageService._prepare_public_mailbox_item()` should read `verification_code`
from the row. It should not read `text_body_path` or `html_body_path` just to
extract a code.

Keep `app/ingest/parser.py` and the parse queue for compatibility:

- Existing `pending` rows from Python SMTP path or old manifests can still be
  parsed.
- Admin reparse can still work.
- The fallback parser should also populate `verification_code` after the schema
  column exists.

## Verification Code API

Add public API shortcuts that use the persisted `verification_code` field:

- `GET /api/v1/public/mailboxes/{mailbox_address}/verification-codes`
  Returns recent messages for a mailbox with:
  `delivery_id`, `message_id`, `received_at`, `subject`, `from_addr`,
  `parse_status`, and `verification_code`.

- `GET /api/v1/public/mailboxes/{mailbox_address}/messages/{delivery_id}/verification-code`
  Returns the single delivery code payload:
  `delivery_id`, `message_id`, `received_at`, `parse_status`,
  `verification_code`.

Both endpoints require the existing public API key handling and mailbox access
checks. A missing code returns HTTP 200 with `verification_code: null` when the
message exists. Missing mailbox or delivery remains HTTP 404.

## Data Flow

Successful C++ delivery:

1. SMTP session accepts RCPT and DATA.
2. `MailJob` enters memory queue.
3. Writer batch writes manifest and raw mail.
4. C++ parser extracts headers, bodies, attachments, preview, and code.
5. Writer writes text/html/attachment files.
6. Writer inserts `messages` as parsed, inserts `message_deliveries`,
   upserts mailboxes, inserts attachments, and updates metrics.
7. Python HTTP immediately reads parsed rows.

Parser failure:

1. SMTP session accepts RCPT and DATA.
2. Writer writes manifest and raw mail.
3. C++ parser returns an error.
4. Writer inserts the message as failed with `parse_error`.
5. Python UI/API can still show the raw message and failure state.

Recovery:

1. Python startup scans manifests only when the database needs recovery.
2. Manifest validation still enforces existing raw and recipient fields.
3. Optional `parsed` data is validated if present.
4. Parsed manifests restore parsed rows and attachment rows without re-parsing.
5. Legacy manifests restore pending rows and go through the Python fallback
   parser.

## Testing

C++ tests:

- Add `cpp/ingestd/tests/test_mime_parser.cpp` for text-only, html-only,
  multipart alternative, mixed attachments, related inline images, base64,
  quoted-printable, folded headers, encoded subjects, and malformed boundary
  failure.
- Add `cpp/ingestd/tests/test_verification_code.cpp` mirroring the highest
  value Python verification-code cases.
- Update `cpp/ingestd/tests/test_batch_writer.cpp` to assert parsed message
  fields, text/html files, attachment files, manifest `parsed`, and failed parse
  behavior.

Python/integration tests:

- Update `tests/test_cpp_ingestd_integration.py` to verify a C++ SMTP delivery
  is immediately `parsed`, has a persisted verification code, and exposes the
  body/attachments through Python.
- Update `tests/test_ingest_pipeline.py` so the Python fallback parser fills
  `verification_code`.
- Update `tests/test_recovery.py` for parsed manifest recovery, failed manifest
  recovery, and old manifest compatibility.
- Update `tests/test_public_routes.py` for the two verification-code API
  shortcuts and to ensure mailbox web rendering no longer reads bodies to
  compute codes.
- Keep `tests/test_verification_code.py` as Python fallback coverage until the
  fallback parser is intentionally removed in a future phase.

## Rollout Notes

The first implementation should be conservative:

- Parse only common MIME structures thoroughly.
- Prefer abstaining on ambiguous verification-code candidates.
- Keep Python fallback parser enabled.
- Keep manifest compatibility strict for existing fields and permissive for the
  optional `parsed` extension.
- Do not change the SMTP durability contract in this phase.

Performance validation should use local port 25 or an isolated test port and
measure:

- accepted messages per second,
- parsed rows per second,
- average and p95 visible latency,
- CPU and RSS of `rapid-inbox-ingestd`,
- SQLite busy/retry behavior.

The target remains 1000+ received messages per second under small verification
mail workloads, with parsed/code-visible latency normally within 500 ms to 1 s.
