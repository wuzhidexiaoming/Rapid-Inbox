PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
PRAGMA synchronous = FULL;

BEGIN;

CREATE TABLE IF NOT EXISTS admins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'superadmin' CHECK (role IN ('superadmin', 'operator', 'viewer')),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    must_change_password INTEGER NOT NULL DEFAULT 0 CHECK (must_change_password IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_login_at TEXT,
    last_login_ip TEXT
);

CREATE TABLE IF NOT EXISTS admin_sessions (
    id TEXT PRIMARY KEY,
    admin_id INTEGER NOT NULL,
    session_token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    last_seen_at TEXT,
    last_ip TEXT,
    user_agent TEXT,
    FOREIGN KEY (admin_id) REFERENCES admins(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS domains (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    root_domain_ascii TEXT NOT NULL UNIQUE,
    root_domain_unicode TEXT,
    accept_exact INTEGER NOT NULL DEFAULT 1 CHECK (accept_exact IN (0, 1)),
    accept_subdomains INTEGER NOT NULL DEFAULT 1 CHECK (accept_subdomains IN (0, 1)),
    public_web_enabled INTEGER NOT NULL DEFAULT 1 CHECK (public_web_enabled IN (0, 1)),
    public_api_enabled INTEGER NOT NULL DEFAULT 1 CHECK (public_api_enabled IN (0, 1)),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    is_hidden INTEGER NOT NULL DEFAULT 0 CHECK (is_hidden IN (0, 1)),
    local_part_case_sensitive INTEGER NOT NULL DEFAULT 0 CHECK (local_part_case_sensitive IN (0, 1)),
    plus_addressing_mode TEXT NOT NULL DEFAULT 'keep' CHECK (plus_addressing_mode IN ('keep', 'strip')),
    max_message_size_bytes INTEGER NOT NULL DEFAULT 52428800,
    retention_days INTEGER,
    dns_status TEXT NOT NULL DEFAULT 'unknown' CHECK (dns_status IN ('unknown', 'ok', 'warning', 'error')),
    dns_last_checked_at TEXT,
    dns_details_json TEXT,
    notes TEXT,
    created_by_admin_id INTEGER,
    updated_by_admin_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (created_by_admin_id) REFERENCES admins(id) ON DELETE SET NULL,
    FOREIGN KEY (updated_by_admin_id) REFERENCES admins(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS mailboxes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain_id INTEGER NOT NULL,
    local_part_canonical TEXT NOT NULL,
    rcpt_domain_ascii TEXT NOT NULL,
    address_canonical TEXT NOT NULL UNIQUE,
    address_display TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    latest_message_at TEXT,
    message_count INTEGER NOT NULL DEFAULT 0,
    public_enabled INTEGER NOT NULL DEFAULT 1 CHECK (public_enabled IN (0, 1)),
    is_hidden INTEGER NOT NULL DEFAULT 0 CHECK (is_hidden IN (0, 1)),
    notes TEXT,
    FOREIGN KEY (domain_id) REFERENCES domains(id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_mailboxes_domain ON mailboxes(domain_id);
CREATE INDEX IF NOT EXISTS idx_mailboxes_domain_local ON mailboxes(domain_id, rcpt_domain_ascii, local_part_canonical);
CREATE INDEX IF NOT EXISTS idx_mailboxes_latest_message ON mailboxes(latest_message_at DESC);

CREATE TABLE IF NOT EXISTS smtp_sessions (
    id TEXT PRIMARY KEY,
    remote_ip TEXT NOT NULL,
    remote_port INTEGER,
    local_ip TEXT,
    local_port INTEGER,
    helo_name TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed', 'error')),
    tls_used INTEGER NOT NULL DEFAULT 0 CHECK (tls_used IN (0, 1)),
    tls_cipher TEXT,
    tls_protocol TEXT,
    connect_at TEXT NOT NULL,
    disconnect_at TEXT,
    first_command_at TEXT,
    last_command_at TEXT,
    message_count INTEGER NOT NULL DEFAULT 0,
    rcpt_accepted_count INTEGER NOT NULL DEFAULT 0,
    rcpt_rejected_count INTEGER NOT NULL DEFAULT 0,
    bytes_received INTEGER NOT NULL DEFAULT 0,
    last_mail_from TEXT,
    last_rcpt_to_sample TEXT,
    result_code INTEGER,
    result_message TEXT,
    close_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_smtp_sessions_connect_at ON smtp_sessions(connect_at DESC);
CREATE INDEX IF NOT EXISTS idx_smtp_sessions_remote_ip ON smtp_sessions(remote_ip, connect_at DESC);
CREATE INDEX IF NOT EXISTS idx_smtp_sessions_status ON smtp_sessions(status, connect_at DESC);

CREATE TABLE IF NOT EXISTS smtp_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    ts TEXT NOT NULL,
    payload_json TEXT,
    FOREIGN KEY (session_id) REFERENCES smtp_sessions(id) ON DELETE CASCADE,
    UNIQUE (session_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_smtp_events_session_ts ON smtp_events(session_id, ts ASC);
CREATE INDEX IF NOT EXISTS idx_smtp_events_type_ts ON smtp_events(event_type, ts DESC);

CREATE TABLE IF NOT EXISTS messages (
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
    parse_status TEXT NOT NULL DEFAULT 'pending' CHECK (parse_status IN ('pending', 'parsed', 'failed')),
    parse_error TEXT,
    has_text INTEGER NOT NULL DEFAULT 0 CHECK (has_text IN (0, 1)),
    has_html INTEGER NOT NULL DEFAULT 0 CHECK (has_html IN (0, 1)),
    has_attachments INTEGER NOT NULL DEFAULT 0 CHECK (has_attachments IN (0, 1)),
    attachment_count INTEGER NOT NULL DEFAULT 0,
    text_preview TEXT,
    text_body_path TEXT,
    html_body_path TEXT,
    headers_json TEXT,
    is_deleted_globally INTEGER NOT NULL DEFAULT 0 CHECK (is_deleted_globally IN (0, 1)),
    FOREIGN KEY (smtp_session_id) REFERENCES smtp_sessions(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_received_at ON messages(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_parse_status ON messages(parse_status, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_message_id_header ON messages(message_id_header);
CREATE INDEX IF NOT EXISTS idx_messages_from_addr ON messages(from_addr, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_subject ON messages(subject);
CREATE INDEX IF NOT EXISTS idx_messages_raw_sha256 ON messages(raw_sha256);

CREATE TABLE IF NOT EXISTS message_deliveries (
    id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL,
    mailbox_id INTEGER NOT NULL,
    rcpt_to TEXT NOT NULL,
    delivered_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'deleted', 'hidden')),
    deleted_at TEXT,
    notes TEXT,
    FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE,
    FOREIGN KEY (mailbox_id) REFERENCES mailboxes(id) ON DELETE RESTRICT,
    UNIQUE (message_id, mailbox_id)
);

CREATE INDEX IF NOT EXISTS idx_message_deliveries_mailbox_time ON message_deliveries(mailbox_id, delivered_at DESC);
CREATE INDEX IF NOT EXISTS idx_message_deliveries_message ON message_deliveries(message_id);
CREATE INDEX IF NOT EXISTS idx_message_deliveries_status_time ON message_deliveries(status, delivered_at DESC);
CREATE INDEX IF NOT EXISTS idx_message_deliveries_rcpt_to ON message_deliveries(rcpt_to, delivered_at DESC);

CREATE TABLE IF NOT EXISTS mail_metric_buckets (
    bucket_ts TEXT PRIMARY KEY,
    deliveries INTEGER NOT NULL DEFAULT 0,
    parse_failures INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS attachments (
    id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL,
    part_index INTEGER NOT NULL,
    filename TEXT,
    safe_filename TEXT,
    content_type TEXT,
    content_disposition TEXT,
    content_id TEXT,
    storage_path TEXT NOT NULL,
    sha256 TEXT,
    size_bytes INTEGER NOT NULL,
    is_inline INTEGER NOT NULL DEFAULT 0 CHECK (is_inline IN (0, 1)),
    created_at TEXT NOT NULL,
    FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE,
    UNIQUE (message_id, part_index)
);

CREATE INDEX IF NOT EXISTS idx_attachments_message ON attachments(message_id);
CREATE INDEX IF NOT EXISTS idx_attachments_sha256 ON attachments(sha256);

CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('admin', 'service', 'public')),
    key_prefix TEXT NOT NULL UNIQUE,
    secret_hash TEXT NOT NULL UNIQUE,
    owner_admin_id INTEGER,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked', 'expired', 'disabled')),
    allow_header INTEGER NOT NULL DEFAULT 1 CHECK (allow_header IN (0, 1)),
    allow_query INTEGER NOT NULL DEFAULT 0 CHECK (allow_query IN (0, 1)),
    rate_limit_per_min INTEGER NOT NULL DEFAULT 3600,
    allowed_ip_cidrs TEXT,
    expires_at TEXT,
    last_used_at TEXT,
    last_used_ip TEXT,
    revoked_at TEXT,
    created_by_admin_id INTEGER,
    created_at TEXT NOT NULL,
    FOREIGN KEY (owner_admin_id) REFERENCES admins(id) ON DELETE SET NULL,
    FOREIGN KEY (created_by_admin_id) REFERENCES admins(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_api_keys_status ON api_keys(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_api_keys_kind ON api_keys(kind, status);

CREATE TABLE IF NOT EXISTS api_key_scopes (
    api_key_id INTEGER NOT NULL,
    scope TEXT NOT NULL,
    PRIMARY KEY (api_key_id, scope),
    FOREIGN KEY (api_key_id) REFERENCES api_keys(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS api_key_domain_grants (
    api_key_id INTEGER NOT NULL,
    domain_id INTEGER NOT NULL,
    PRIMARY KEY (api_key_id, domain_id),
    FOREIGN KEY (api_key_id) REFERENCES api_keys(id) ON DELETE CASCADE,
    FOREIGN KEY (domain_id) REFERENCES domains(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS api_key_mailbox_grants (
    api_key_id INTEGER NOT NULL,
    mailbox_pattern TEXT NOT NULL,
    PRIMARY KEY (api_key_id, mailbox_pattern),
    FOREIGN KEY (api_key_id) REFERENCES api_keys(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_type TEXT NOT NULL CHECK (actor_type IN ('admin', 'api_key', 'system', 'anonymous')),
    actor_ref TEXT,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_ref TEXT,
    status TEXT NOT NULL CHECK (status IN ('success', 'failure')),
    ip TEXT,
    user_agent TEXT,
    details_json TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at ON audit_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_logs_actor ON audit_logs(actor_type, actor_ref, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_logs_resource ON audit_logs(resource_type, resource_ref, created_at DESC);

CREATE TABLE IF NOT EXISTS system_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

COMMIT;
