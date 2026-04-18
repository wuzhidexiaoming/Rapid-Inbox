# Rapid Inbox V1 Design

## Goal

Build a complete single-machine inbound-only mailbox system that can:

- Receive mail over SMTP for configured domains and subdomains
- Persist raw mail plus metadata safely to local disk and SQLite
- Expose anonymous public mailbox/message pages
- Expose public JSON APIs protected by DB-backed API keys
- Provide an admin UI and admin APIs for operations, visibility, and control
- Recover from process crashes when raw mail was persisted before SQLite writes completed

This v1 is intended to be fully runnable on one machine with one local Python environment and local storage. It does not include outbound mail, IMAP, POP3, multi-node deployment, or advanced spam filtering.

## Scope

### In Scope

- SMTP ingress with fast `250` response after durable raw write
- Domain rule management with exact-domain and subdomain support
- Mailbox auto-creation on first delivery and lazy creation on first public access
- Async MIME parsing for headers, text, HTML, and attachments
- Raw, text, HTML, attachment, and manifest storage on local filesystem
- Public HTML pages for mailbox and message views
- Public JSON API with API key authentication and resource scoping
- Admin login with username/password and session cookie
- Admin HTML pages for dashboard, domains, mailboxes, messages, live view, API keys, audit, and settings
- Admin JSON API for the same operational domains
- SMTP live events and history, including SSE streaming
- DNS guidance and DNS check for configured domains
- Audit logging for privileged and destructive actions
- Recovery scanner for persisted raw files with missing or incomplete DB state
- Local development startup flow and README

### Out Of Scope

- IMAP, POP3, or outbound SMTP submission
- Multi-node coordination or shared storage
- Enterprise anti-spam, antivirus, SPF/DKIM/DMARC enforcement
- Private mailbox secrecy model
- Full-text search beyond simple indexed list/detail reads

## Architecture

The application remains a single-process, single-machine Python service boundary with two runtime entrypoints:

1. HTTP server powered by FastAPI
2. SMTP listener powered by `aiosmtpd`

Internally the system is split into focused modules:

- `smtp/`: SMTP handler, live state, session event recording, server bootstrap
- `ingest/`: storage, queue, parser, recovery scanner
- `db/`: SQLite connection and serialized writes
- `services/`: domains, mailboxes, messages, attachments, audit, settings, DNS checks, API keys
- `auth/`: password hashing, admin sessions, API key verification, permission checks
- `http/`: public HTML, public API, admin HTML, admin API, SSE

The critical design rule is preserved: the SMTP hot path only decides whether a recipient is allowed, durably persists the raw bytes and recovery metadata, inserts placeholder records through the single write path, queues parsing, and returns `250`.

## Storage Model

### SQLite Responsibilities

SQLite stores metadata, indexes, permissions, and operational logs:

- admins
- admin_sessions
- domains
- mailboxes
- smtp_sessions
- smtp_events
- messages
- message_deliveries
- attachments
- api_keys and related scope/grant tables
- audit_logs
- system_settings

Large payloads are never stored in SQLite. SQLite is configured with:

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
PRAGMA synchronous = FULL;
```

All writes go through a single async write gate to avoid lock thrash and inconsistent multi-writer behavior.

### Filesystem Responsibilities

Local storage root keeps durable mail artifacts and recovery metadata:

```text
storage/
  app.db
  raw/YYYY/MM/DD/<message_id>.eml
  text/YYYY/MM/DD/<message_id>.txt
  html/YYYY/MM/DD/<message_id>.html
  attachments/<message_id>/<attachment_id>-<safe_name>
  manifests/YYYY/MM/DD/<message_id>.json
  tmp/
```

Each file write uses:

1. a `.part` temporary file
2. `flush()`
3. `fsync()`
4. `os.replace()`

### Recovery Manifest

To make crash recovery complete, each raw mail write is paired with a tiny JSON manifest containing:

- `message_id`
- `smtp_session_id`
- `envelope_from`
- `rcpt_tos`
- `received_at`
- `raw_path`
- `raw_sha256`
- `raw_size_bytes`

Reason: the raw `.eml` file alone does not reliably preserve SMTP envelope recipients. Without a manifest the recovery scanner cannot rebuild `message_deliveries` and mailbox state after SQLite placeholder writes fail.

## Domain And Mailbox Model

### Domain Rules

Each configured domain rule includes:

- `root_domain_ascii`
- `root_domain_unicode`
- `accept_exact`
- `accept_subdomains`
- `public_web_enabled`
- `public_api_enabled`
- `is_active`
- `plus_addressing_mode`
- `local_part_case_sensitive`
- `max_message_size_bytes`

Matching rules:

1. Normalize domain to lowercase IDNA ASCII
2. Find the longest matching configured suffix
3. If exact match, require `accept_exact = true`
4. If child domain match, require `accept_subdomains = true`
5. Use the matched rule for canonicalization decisions

Canonical mailbox address is derived from the matched rule:

- local part lowercased unless the rule is case-sensitive
- plus tag preserved or stripped according to `plus_addressing_mode`
- domain stored in normalized ASCII form

### Virtual Mailboxes

Mailboxes are virtual. The system does not require pre-creation before delivery.

- On first accepted delivery, mailbox metadata is upserted
- On first public access to a managed domain mailbox, an empty mailbox row is created lazily
- Managed domain but no mail: return `200` with an empty mailbox page
- Unmanaged domain: return `404`

## SMTP Ingress Design

### Supported Commands

The SMTP handler supports:

- `EHLO` / `HELO`
- `MAIL FROM`
- `RCPT TO`
- `DATA`
- `RSET`
- `QUIT`

### Hot Path

For each accepted message:

1. Ensure there is an SMTP session id and session summary row
2. Validate each `RCPT TO` against in-memory domain rules
3. Reject if recipient count exceeds configured cap
4. Reject if message size exceeds configured cap
5. On `DATA`, write raw bytes to `.eml`
6. Write recovery manifest next to the raw message
7. Insert placeholder `messages` row
8. Insert one `message_deliveries` row per recipient
9. Upsert mailboxes touched by the delivery
10. Record session counters and SMTP events
11. Queue async parse task
12. Return `250 queued as <message_id>`

### Rejection And Failure Rules

- Domain not allowed: `550`
- Message too large: `552`
- Too many recipients: `552`
- Protocol or session state error: `554`
- Local raw write failure: `451`
- SQLite placeholder failure after raw write: do not delete the raw mail; rely on recovery scanner
- Parse failure: message remains visible with `parse_status = failed`

### Multi-Recipient Behavior

One raw message produces:

- one `messages` row
- multiple `message_deliveries` rows
- one mailbox update per recipient mailbox

No content-based deduplication is performed.

## Async Parsing Design

The parse worker consumes queued `message_id`s and performs slow work off the SMTP path:

1. Read raw `.eml`
2. Parse message headers
3. Extract `text/plain`
4. Extract `text/html`
5. Extract attachments
6. Persist extracted bodies and attachments
7. Build preview text
8. Update the `messages` row
9. Replace placeholder sender/subject data with parsed header data when available

If parsing fails:

- set `parse_status = failed`
- store `parse_error`
- keep the message visible in lists and detail pages
- allow later reparse from the admin UI/API

## Public Access Design

### Public HTML

Routes:

- `GET /mail/{mailbox_address}`
- `GET /mail/{mailbox_address}/{delivery_id}`
- `GET /mail/{mailbox_address}/{delivery_id}/raw`
- `GET /mail/{mailbox_address}/{delivery_id}/attachments/{attachment_id}`
- `GET /mail/{mailbox_address}/{delivery_id}/html`

Behavior:

- Mailbox list is anonymous and only available when the matched domain has `public_web_enabled = true`
- Message detail validates the mailbox-to-delivery relationship before rendering
- Raw and attachment downloads validate the same relationship before returning file content
- The HTML body view is isolated from the main page

### HTML Safety

HTML mail is not injected directly into the main DOM.

The detail page embeds a dedicated render route in a sandboxed iframe. The render route:

- serves only the stored message HTML body
- rewrites local CID references to safe local attachment routes when possible
- does not auto-load remote images
- sets a restrictive CSP and sandbox policy

### Public JSON API

Routes:

- list mailbox messages
- get message detail
- download raw
- download attachment

Rules:

- Requires an API key of kind `public`, `service`, or `admin`
- Requires `public.read` or a broader read scope
- Requires domain and mailbox grant validation
- Domain must have `public_api_enabled = true`

Pagination is cursor-based on `delivered_at` and `delivery_id`.

## Admin Authentication And Authorization

### Admin UI Authentication

The admin HTML UI uses:

- username/password login
- password hash stored in SQLite
- session cookie backed by `admin_sessions`

On first boot, the system ensures a bootstrap admin exists. The bootstrap password is generated or taken from configuration and the UI forces password rotation after first login.

### API Keys

API keys are stored in the database:

- only prefix and `secret_hash` are persisted
- full secret is shown only at create/rotate time
- kinds: `admin`, `service`, `public`
- scopes and resource grants are stored in dedicated tables

Validation order:

1. locate by prefix
2. verify hash
3. verify status and expiry
4. verify source IP if configured
5. verify required scope
6. verify domain grants
7. verify mailbox grants
8. update `last_used_at` and `last_used_ip`

### Permission Rules

- Public API requires `public.read`
- Admin HTML requires authenticated admin session
- Admin API requires admin/service API key with matching scopes
- Live SSE requires admin session or `live.read`
- Destructive message operations require `messages.write`
- API key management requires `apikeys.write`
- Domain CRUD requires `domains.write`
- Settings changes require `system.write`

## Admin UI Design

### Pages

- `/admin/login`
- `/admin`
- `/admin/domains`
- `/admin/domains/{id}`
- `/admin/mailboxes`
- `/admin/messages`
- `/admin/live`
- `/admin/api-keys`
- `/admin/audit`
- `/admin/settings`

### Dashboard

Displays:

- active SMTP session count
- 1-minute and 5-minute receive rate
- parse queue depth
- received in last 24 hours
- failed parses in last 24 hours
- disk usage
- domain count, mailbox count, message count

### Domain Management

Operators can:

- create, view, edit, delete domains
- toggle exact/subdomain acceptance
- toggle public web/API visibility
- view recommended DNS records
- run DNS checks

### Mailbox And Message Operations

Operators can:

- list mailboxes with filtering
- hide or unhide public mailbox access
- browse mailbox deliveries
- inspect message detail
- download raw
- download attachments
- reparse failed or pending messages
- soft-delete deliveries singly or in bulk

### Live View

The live page subscribes to SSE and shows:

- new connections
- EHLO/MAIL FROM/RCPT events
- accepted and rejected recipients
- queued deliveries
- disconnects and errors

The page also links through to recent session history.

### API Key Management

Operators can:

- create keys with kind, scopes, grants, expiry, and transport options
- rotate keys
- revoke keys
- inspect last use metadata

### Audit And Settings

Audit page supports filtering by actor, action, resource, and time range.

Settings page manages operational values such as:

- default max message size
- max recipients per message
- idle timeout
- per-IP rate limit
- disk warning threshold

## Admin API Design

The admin JSON API mirrors the admin UI surface:

- domain CRUD and DNS check
- mailbox list/detail/update/delete
- message list/detail/reparse/delete
- SMTP session list/detail
- live SSE stream
- API key CRUD/rotate/revoke
- audit log listing
- settings read/update

Admin HTML and admin API share the same service layer. The HTTP layer does not embed business rules directly.

## DNS Check Design

The DNS check service is best-effort and advisory, not required on the SMTP hot path.

For each domain rule it reports:

- recommended root MX records
- recommended wildcard MX records
- resolved MX answers for the exact root
- whether wildcard delivery is likely to route correctly
- notes explaining wildcard limitations and zone-cut caveats

The result is stored back into:

- `dns_status`
- `dns_last_checked_at`
- `dns_details_json`

If DNS lookup fails, the domain remains deliverable according to local rules; the check only affects admin visibility.

## Audit Logging

Audit entries are required for:

- admin login/logout/password change
- domain create/update/delete
- API key create/rotate/revoke
- delivery delete and bulk delete
- reparse requests
- settings changes

Public anonymous HTML browsing does not emit full audit records. Internal aggregate counters may still be recorded later, but they are not part of this v1 contract.

## Recovery Scanner

The recovery scanner runs at startup and can also be triggered manually.

Steps:

1. delete stale `.part` files
2. scan raw messages and manifests
3. insert missing `messages` rows
4. insert missing `message_deliveries` rows
5. upsert missing mailboxes
6. enqueue parse tasks for `pending` and `failed` messages
7. record recovery actions in audit logs or system logs

Recovery never drops mail because two raw files have the same hash.

## Data Flow Summary

### Receive Path

`SMTP session -> RCPT validation -> raw + manifest durable write -> placeholder DB write -> parse queue -> 250`

### Read Path

`public/admin request -> permission check -> indexed SQLite read -> filesystem body/raw/attachment read if needed -> response`

### Recovery Path

`startup -> scan persisted artifacts -> repair metadata -> requeue parse tasks`

## Operational Limits

The v1 system includes self-protection controls even though it does not do anti-spam classification:

- max message size
- max recipients per message
- idle timeout
- concurrent connection limit
- short-window per-IP connection rate cap
- disk usage warning threshold

These values live in configuration defaults and `system_settings`, with admin UI exposure where practical.

## Testing Strategy

### Unit Tests

- domain matcher
- mailbox canonicalization
- password hashing and session helpers
- API key validation and permission checks
- file storage and manifest writing
- DNS check result interpretation

### Service Tests

- placeholder insert path
- multi-recipient delivery behavior
- parse success and parse failure transitions
- audit emission
- recovery rebuilds metadata from raw + manifest

### End-To-End Tests

- SMTP handler accept/reject/queue behavior
- public HTML list/detail/raw/attachment behavior
- public API permission enforcement
- admin login/session flow
- admin CRUD flows
- SSE live stream event emission

The SMTP acceptance invariant must be explicitly tested: `250` is returned only after raw and manifest durable writes succeed.

## Startup And Delivery

The repository should ship with:

- a local `.venv`
- `README.md`
- `.env.example`
- HTTP app entrypoint
- SMTP runner entrypoint
- bootstrap admin initialization

Local development must support:

1. creating the venv
2. installing dependencies
3. starting HTTP
4. starting SMTP
5. logging into the admin UI
6. sending a test email locally

## Acceptance Criteria For This V1

This design is complete when the implementation can:

1. start HTTP and SMTP locally
2. create and authenticate an admin user
3. add a domain through the admin UI or admin API
4. accept root-domain and subdomain mail according to rule flags
5. show received mail in public mailbox pages
6. render message text and isolated HTML safely
7. download raw messages and attachments
8. enforce public/admin API permissions with DB-backed API keys
9. show live SMTP activity and historical session data
10. recover delivered raw mail after a crash even if SQLite placeholder writes were missed

## Implementation Notes

- The existing MVP code may be reused where it already matches this design, but auth, admin UI/API, permissions, DNS checks, recovery, and safer HTML rendering need to be expanded to reach the v1 contract.
- The repo currently is not a git repository, so this spec can be written to disk but cannot be committed from the current workspace state.
