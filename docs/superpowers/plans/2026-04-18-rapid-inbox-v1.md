# Rapid Inbox V1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a complete single-machine Rapid Inbox v1 that can receive inbound SMTP mail, persist and recover mail safely, expose public mailbox access, and provide an authenticated admin UI and API with live operations visibility.

**Architecture:** Keep SMTP hot-path logic small and durable by writing raw mail plus a recovery manifest before returning `250`, then move parsing and secondary indexing into the async ingest layer. Split the app into focused auth, services, ingest, SMTP, and HTTP modules so public views, admin pages, admin APIs, and background recovery all share the same service layer and SQLite write gate.

**Tech Stack:** Python 3.12, FastAPI, Jinja2, aiosmtpd, SQLite, pytest, pytest-asyncio, httpx

---

## File Structure

### Runtime And Configuration

- Modify: `app/config.py`
  - Add admin bootstrap config, cookie/session settings, SMTP safety limits, and storage path helpers for manifests.
- Modify: `app/main.py`
  - Register admin HTML views and SSE routes, initialize shared services on app startup.
- Create: `app/http_runner.py`
  - Start the HTTP server as a standalone process entrypoint.
- Create: `app/smtp_runner.py`
  - Start the SMTP server as a standalone process entrypoint.

### Auth

- Create: `app/auth/__init__.py`
- Create: `app/auth/passwords.py`
  - Password hashing and verification helpers.
- Create: `app/auth/sessions.py`
  - Session cookie issuance, persistence, lookup, revoke.
- Create: `app/auth/api_keys.py`
  - API key creation, hashing, rotate, revoke, last-used updates.
- Create: `app/auth/permissions.py`
  - Scope, domain-grant, mailbox-grant checks for public/admin endpoints.

### Services

- Modify: `app/services/domains.py`
  - Expand from simple create/list to full CRUD plus DNS status integration.
- Create: `app/services/mailboxes.py`
  - Mailbox list/detail/hide toggles and delivery-scoped reads.
- Create: `app/services/messages.py`
  - Message list/detail/raw reads, reparsing, delete operations.
- Create: `app/services/attachments.py`
  - Attachment lookup and content reads with delivery ownership checks.
- Create: `app/services/audit.py`
  - Append and query audit logs.
- Create: `app/services/settings.py`
  - Read/write operational settings and defaults.
- Create: `app/services/dns_check.py`
  - Best-effort DNS guidance and status checks.

### Ingest And Recovery

- Modify: `app/ingest/storage.py`
  - Add manifest storage, HTML render helpers, and read methods.
- Modify: `app/ingest/parser.py`
  - Improve HTML handling and CID attachment indexing.
- Create: `app/ingest/recovery.py`
  - Startup scanner to rebuild missing metadata and requeue parse work.

### SMTP

- Modify: `app/smtp/live_state.py`
  - Track recent events and active sessions for SSE snapshots.
- Modify: `app/smtp/handler.py`
  - Record SMTP events, enforce limits, write manifests, and update session summaries.
- Modify: `app/smtp/server.py`
  - Provide explicit start/stop lifecycle for the SMTP listener.

### HTTP

- Modify: `app/http/public_views.py`
  - Add raw download, attachment download, HTML iframe view, and better domain visibility checks.
- Modify: `app/http/public_api.py`
  - Replace config token auth with DB-backed API key auth and resource grant checks.
- Modify: `app/http/admin_api.py`
  - Expand into domains, mailboxes, messages, SMTP sessions, API keys, audit, settings, and DNS check endpoints.
- Create: `app/http/admin_views.py`
  - Admin login, logout, dashboard, domains, mailboxes, messages, live, API keys, audit, settings.
- Create: `app/http/sse.py`
  - Server-Sent Events route for live SMTP activity.

### Templates

- Modify: `app/templates/public/mailbox.html`
- Modify: `app/templates/public/message.html`
- Create: `app/templates/public/html_frame.html`
- Create: `app/templates/admin/base.html`
- Create: `app/templates/admin/login.html`
- Create: `app/templates/admin/dashboard.html`
- Create: `app/templates/admin/domains.html`
- Create: `app/templates/admin/domain_detail.html`
- Create: `app/templates/admin/mailboxes.html`
- Create: `app/templates/admin/messages.html`
- Create: `app/templates/admin/live.html`
- Create: `app/templates/admin/api_keys.html`
- Create: `app/templates/admin/audit.html`
- Create: `app/templates/admin/settings.html`

### Tests

- Modify: `tests/conftest.py`
  - Add fixtures for bootstrap admin, DB-backed API keys, live event setup, and recovery artifacts.
- Create: `tests/test_fixtures.py`
- Create: `tests/test_auth.py`
- Create: `tests/test_public_downloads.py`
- Create: `tests/test_admin_views.py`
- Create: `tests/test_admin_permissions.py`
- Create: `tests/test_live_sse.py`
- Create: `tests/test_recovery.py`
- Create: `tests/test_dns_check.py`
- Modify: `tests/test_ingest_pipeline.py`
- Modify: `tests/test_public_routes.py`
- Modify: `tests/test_admin_api.py`
- Modify: `tests/test_smtp_handler.py`

### Project Files

- Modify: `.gitignore`
- Modify: `pyproject.toml`
- Create: `.env.example`
- Create: `README.md`

---

### Task 0: Shared Test Harness Fixtures

**Files:**
- Modify: `tests/conftest.py`
- Create: `tests/test_fixtures.py`

- [ ] **Step 1: Write the failing fixture smoke test**

```python
@pytest.mark.asyncio
async def test_shared_runtime_and_app_client_fixtures(app_client, runtime) -> None:
    response = await app_client.get("/does-not-exist")

    assert response.status_code == 404
    assert runtime.settings.storage_root.exists()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_fixtures.py::test_shared_runtime_and_app_client_fixtures -v`
Expected: FAIL because `runtime` and `app_client` fixtures do not exist yet

- [ ] **Step 3: Implement shared fixtures in `tests/conftest.py`**

```python
@dataclass(slots=True)
class SeededMessage:
    message_id: str
    delivery_id: str
    public_api_key: str


@pytest.fixture
async def app_fixture(tmp_path) -> AsyncIterator[tuple[FastAPI, RapidInboxRuntime]]:
    settings = Settings(
        storage_root=tmp_path / "storage",
        database_path=tmp_path / "storage" / "app.db",
    )
    app = create_app(settings=settings)
    async with app.router.lifespan_context(app):
        yield app, app.state.runtime
```

- [ ] **Step 4: Add the shared app client fixture**

```python
@pytest.fixture
async def runtime(app_fixture) -> RapidInboxRuntime:
    _, runtime = app_fixture
    return runtime


@pytest.fixture
async def app_client(app_fixture) -> AsyncIterator[httpx.AsyncClient]:
    app, _ = app_fixture
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_fixtures.py::test_shared_runtime_and_app_client_fixtures -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add tests/conftest.py tests/test_fixtures.py
git commit -m "test: add shared runtime and client fixtures"
```

### Task 1: Durable Raw + Manifest Receive Path

**Files:**
- Modify: `app/config.py`
- Modify: `app/ingest/storage.py`
- Modify: `app/runtime.py`
- Modify: `app/smtp/handler.py`
- Modify: `tests/conftest.py`
- Modify: `tests/test_ingest_pipeline.py`

- [ ] **Step 1: Write the failing manifest test**

```python
@pytest.mark.asyncio
async def test_accept_message_writes_manifest_for_recovery(tmp_path, sample_email_bytes: bytes) -> None:
    settings = Settings(
        storage_root=tmp_path / "storage",
        database_path=tmp_path / "storage" / "app.db",
    )
    runtime = RapidInboxRuntime(settings)

    await runtime.start()
    try:
        await runtime.create_domain("adb.com")
        response = await runtime.accept_message(
            rcpt_tos=["foo@adb.com", "bar@adb.com"],
            envelope_from="sender@example.com",
            content=sample_email_bytes,
            smtp_session_id="smtp_test_1",
        )
        await runtime.drain_parser_queue()

        manifest_paths = list(settings.manifests_dir.rglob("*.json"))
        assert response.startswith("250 queued as ")
        assert len(manifest_paths) == 1

        manifest = json.loads(manifest_paths[0].read_text(encoding="utf-8"))
        assert manifest["smtp_session_id"] == "smtp_test_1"
        assert manifest["envelope_from"] == "sender@example.com"
        assert manifest["rcpt_tos"] == ["foo@adb.com", "bar@adb.com"]
        assert manifest["raw_path"].endswith(".eml")
    finally:
        await runtime.stop()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_ingest_pipeline.py::test_accept_message_writes_manifest_for_recovery -v`
Expected: FAIL because `Settings.manifests_dir` and manifest writes do not exist yet

- [ ] **Step 3: Implement manifest paths and writes**

```python
@property
def manifests_dir(self) -> Path:
    return self.storage_root / "manifests"


def write_manifest(self, message_id: str, received_at: str, payload: dict[str, object]) -> str:
    relative_path = self._dated_path("manifests", message_id, ".json", received_at)
    content = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    self._write_bytes(relative_path, content)
    return relative_path
```

- [ ] **Step 4: Persist the manifest from the receive path**

```python
manifest_payload = {
    "message_id": message_id,
    "smtp_session_id": smtp_session_id,
    "envelope_from": envelope_from,
    "rcpt_tos": list(rcpt_tos),
    "received_at": received_at,
    "raw_path": raw_path,
    "raw_sha256": raw_sha256,
    "raw_size_bytes": raw_size_bytes,
}
self.storage.write_manifest(message_id, received_at, manifest_payload)
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_ingest_pipeline.py::test_accept_message_writes_manifest_for_recovery -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/config.py app/ingest/storage.py app/runtime.py app/smtp/handler.py tests/conftest.py tests/test_ingest_pipeline.py
git commit -m "feat: persist recovery manifests on smtp ingest"
```

### Task 2: Recovery Scanner And Startup Repair

**Files:**
- Create: `app/ingest/recovery.py`
- Modify: `app/runtime.py`
- Modify: `tests/conftest.py`
- Create: `tests/test_recovery.py`

- [ ] **Step 1: Write the failing recovery test**

```python
@pytest.mark.asyncio
async def test_recovery_scanner_rebuilds_missing_message_and_delivery(tmp_path, sample_email_bytes: bytes) -> None:
    settings = Settings(
        storage_root=tmp_path / "storage",
        database_path=tmp_path / "storage" / "app.db",
    )
    runtime = RapidInboxRuntime(settings)

    await runtime.start()
    try:
        await runtime.create_domain("adb.com")
        await runtime.accept_message(
            rcpt_tos=["foo@adb.com"],
            envelope_from="sender@example.com",
            content=sample_email_bytes,
            smtp_session_id="smtp_recover_1",
        )
        await runtime.drain_parser_queue()
    finally:
        await runtime.stop()

    with connect_database(settings.database_path) as connection:
        connection.execute("DELETE FROM message_deliveries")
        connection.execute("DELETE FROM messages")
        connection.commit()

    repaired = RapidInboxRuntime(settings)
    await repaired.start()
    try:
        mailbox = await repaired.get_mailbox_view("foo@adb.com")
        await repaired.drain_parser_queue()
        assert mailbox["message_count"] == 1
        assert mailbox["items"][0]["parse_status"] in {"pending", "parsed"}
    finally:
        await repaired.stop()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_recovery.py::test_recovery_scanner_rebuilds_missing_message_and_delivery -v`
Expected: FAIL because startup recovery does not rebuild metadata

- [ ] **Step 3: Implement the recovery scanner**

```python
class RecoveryScanner:
    def __init__(self, runtime: "RapidInboxRuntime") -> None:
        self.runtime = runtime

    async def run(self) -> None:
        self.runtime.storage.cleanup_stale_parts()
        for manifest_path in self.runtime.settings.manifests_dir.rglob("*.json"):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            await self.runtime.recover_from_manifest(manifest)
        for message_id in await self.runtime.find_messages_for_reparse():
            await self.runtime.parse_queue.enqueue(ParseTask(message_id=message_id))
```

- [ ] **Step 4: Hook recovery into startup**

```python
self.recovery = RecoveryScanner(self)

async def start(self) -> None:
    self.settings.ensure_directories()
    initialize_database(self.settings.database_path)
    self.domains.reload()
    await self.parse_queue.start()
    await self.recovery.run()
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_recovery.py::test_recovery_scanner_rebuilds_missing_message_and_delivery -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/ingest/recovery.py app/runtime.py tests/conftest.py tests/test_recovery.py
git commit -m "feat: recover missing metadata from persisted manifests"
```

### Task 3: Passwords, Sessions, And Bootstrap Admin

**Files:**
- Create: `app/auth/__init__.py`
- Create: `app/auth/passwords.py`
- Create: `app/auth/sessions.py`
- Modify: `app/config.py`
- Modify: `app/runtime.py`
- Create: `tests/test_auth.py`

- [ ] **Step 1: Write the failing auth core test**

```python
@pytest.mark.asyncio
async def test_runtime_bootstraps_admin_and_persists_login_session(tmp_path) -> None:
    settings = Settings(
        storage_root=tmp_path / "storage",
        database_path=tmp_path / "storage" / "app.db",
        bootstrap_admin_username="admin",
        bootstrap_admin_password="change-me-now",
        session_cookie_name="rapid_inbox_session",
    )
    runtime = RapidInboxRuntime(settings)

    await runtime.start()
    try:
        admin = await runtime.auth.authenticate_admin("admin", "change-me-now")
        session = await runtime.auth.create_session(admin_id=admin["id"], ip="127.0.0.1", user_agent="pytest")
        loaded = await runtime.auth.get_session_admin(session["token"])

        assert admin["username"] == "admin"
        assert session["token"]
        assert loaded["admin_id"] == admin["id"]
    finally:
        await runtime.stop()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_auth.py::test_runtime_bootstraps_admin_and_persists_login_session -v`
Expected: FAIL because auth helpers and bootstrap admin logic do not exist

- [ ] **Step 3: Implement password hashing and verification**

```python
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 600_000)
    return f"{salt}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    salt, expected = stored_hash.split("$", 1)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 600_000)
    return hmac.compare_digest(digest.hex(), expected)
```

- [ ] **Step 4: Implement session persistence and bootstrap admin creation**

```python
async def ensure_bootstrap_admin(self) -> None:
    if await self.count_admins() > 0:
        return
    password_hash = hash_password(self.settings.bootstrap_admin_password)
    await self.writer.execute(
        lambda connection: connection.execute(
            """
            INSERT INTO admins (username, password_hash, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                self.settings.bootstrap_admin_username,
                password_hash,
                utc_now(),
                utc_now(),
            ),
        )
    )
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_auth.py::test_runtime_bootstraps_admin_and_persists_login_session -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/auth/__init__.py app/auth/passwords.py app/auth/sessions.py app/config.py app/runtime.py tests/test_auth.py
git commit -m "feat: add admin password auth and session bootstrap"
```

### Task 4: DB-Backed API Keys And Permission Checks

**Files:**
- Create: `app/auth/api_keys.py`
- Create: `app/auth/permissions.py`
- Modify: `app/runtime.py`
- Modify: `tests/conftest.py`
- Create: `tests/test_admin_permissions.py`

- [ ] **Step 1: Write the failing API key permission test**

```python
@pytest.mark.asyncio
async def test_public_key_requires_scope_and_domain_grant(app_client, runtime) -> None:
    await runtime.create_domain("adb.com")
    key = await runtime.api_keys.create_key(
        name="public-read",
        kind="public",
        scopes=["public.read"],
        domain_ids=[],
        mailbox_patterns=[],
    )

    response = await app_client.get(
        "/api/v1/public/mailboxes/foo@adb.com/messages",
        headers={"X-API-Key": key["plain_text"]},
    )

    assert response.status_code == 403
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_admin_permissions.py::test_public_key_requires_scope_and_domain_grant -v`
Expected: FAIL because API keys are still config-token based

- [ ] **Step 3: Implement API key creation and lookup**

```python
def make_api_key(kind: str) -> tuple[str, str, str]:
    prefix = secrets.token_hex(4)
    secret = secrets.token_urlsafe(24)
    plain_text = f"ri_{kind}_{prefix}_{secret}"
    secret_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    return prefix, plain_text, secret_hash
```

- [ ] **Step 4: Implement scope and resource grant checks**

```python
def ensure_mailbox_access(grants: PermissionContext, mailbox_address: str, domain_id: int, required_scope: str) -> None:
    if required_scope not in grants.scopes:
        raise PermissionDenied(required_scope)
    if grants.domain_ids and domain_id not in grants.domain_ids:
        raise PermissionDenied("domain grant missing")
    if grants.mailbox_patterns and mailbox_address not in grants.mailbox_patterns:
        raise PermissionDenied("mailbox grant missing")
```

- [ ] **Step 5: Add `admin_client` and `seeded_message` fixtures**

```python
@pytest.fixture
async def admin_client(app_client: httpx.AsyncClient, runtime: RapidInboxRuntime) -> httpx.AsyncClient:
    key = await runtime.api_keys.create_key(
        name="fixture-admin",
        kind="admin",
        scopes=["domains.write", "messages.write", "audit.read", "system.write", "live.read"],
        domain_ids=[],
        mailbox_patterns=[],
    )
    app_client.headers["X-API-Key"] = key["plain_text"]
    return app_client


@pytest.fixture
async def seeded_message(runtime: RapidInboxRuntime, sample_email_bytes: bytes) -> SeededMessage:
    await runtime.create_domain("adb.com")
    key = await runtime.api_keys.create_key(
        name="fixture-public",
        kind="public",
        scopes=["public.read"],
        domain_ids=[],
        mailbox_patterns=[],
    )
    await runtime.accept_message(
        rcpt_tos=["foo@adb.com"],
        envelope_from="sender@example.com",
        content=sample_email_bytes,
        smtp_session_id="smtp_fixture_1",
    )
    await runtime.drain_parser_queue()
    mailbox = await runtime.get_mailbox_view("foo@adb.com")
    return SeededMessage(
        message_id=mailbox["items"][0]["message_id"],
        delivery_id=mailbox["items"][0]["delivery_id"],
        public_api_key=key["plain_text"],
    )
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_admin_permissions.py::test_public_key_requires_scope_and_domain_grant -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add app/auth/api_keys.py app/auth/permissions.py app/runtime.py tests/conftest.py tests/test_admin_permissions.py
git commit -m "feat: add database-backed api keys and permission checks"
```

### Task 5: Public Raw, Attachment, And Sandboxed HTML Delivery

**Files:**
- Modify: `app/http/public_views.py`
- Modify: `app/http/public_api.py`
- Create: `app/services/attachments.py`
- Create: `app/services/messages.py`
- Create: `app/templates/public/html_frame.html`
- Modify: `app/templates/public/message.html`
- Create: `tests/test_public_downloads.py`

- [ ] **Step 1: Write the failing public download test**

```python
@pytest.mark.asyncio
async def test_public_message_routes_serve_raw_attachment_and_html_frame(app_client, seeded_message) -> None:
    raw_response = await app_client.get(f"/mail/foo@adb.com/{seeded_message.delivery_id}/raw")
    html_response = await app_client.get(f"/mail/foo@adb.com/{seeded_message.delivery_id}/html")
    api_response = await app_client.get(
        f"/api/v1/public/mailboxes/foo@adb.com/messages/{seeded_message.delivery_id}/raw",
        headers={"X-API-Key": seeded_message.public_api_key},
    )

    assert raw_response.status_code == 200
    assert raw_response.headers["content-type"] == "message/rfc822"
    assert "sandbox" in html_response.text
    assert api_response.status_code == 200
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_public_downloads.py::test_public_message_routes_serve_raw_attachment_and_html_frame -v`
Expected: FAIL because raw/html routes and DB-backed public API auth do not exist

- [ ] **Step 3: Implement delivery-scoped raw and attachment reads**

```python
@router.get("/mail/{mailbox_address}/{delivery_id}/raw")
async def message_raw(mailbox_address: str, delivery_id: str, request: Request) -> Response:
    detail = await request.app.state.runtime.get_delivery_detail(mailbox_address, delivery_id)
    raw_bytes = await request.app.state.runtime.get_raw_message(detail["delivery_id"])
    return Response(raw_bytes, media_type="message/rfc822")
```

- [ ] **Step 4: Implement isolated HTML rendering**

```python
@router.get("/mail/{mailbox_address}/{delivery_id}/html", response_class=HTMLResponse)
async def message_html_frame(mailbox_address: str, delivery_id: str, request: Request) -> HTMLResponse:
    detail = await request.app.state.runtime.get_delivery_detail(mailbox_address, delivery_id)
    return request.app.state.templates.TemplateResponse(
        request,
        "public/html_frame.html",
        {"html_body": detail["html_body"]},
        headers={"Content-Security-Policy": "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'"},
    )
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_public_downloads.py::test_public_message_routes_serve_raw_attachment_and_html_frame -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/http/public_views.py app/http/public_api.py app/services/attachments.py app/services/messages.py app/templates/public/html_frame.html app/templates/public/message.html tests/test_public_downloads.py
git commit -m "feat: add public raw attachment and html message routes"
```

### Task 6: Admin APIs For Domains, Mailboxes, Messages, Settings, And Audit

**Files:**
- Modify: `app/http/admin_api.py`
- Modify: `app/services/domains.py`
- Create: `app/services/mailboxes.py`
- Modify: `app/services/messages.py`
- Create: `app/services/audit.py`
- Create: `app/services/settings.py`
- Modify: `app/runtime.py`
- Modify: `tests/test_admin_api.py`

- [ ] **Step 1: Write the failing admin API test**

```python
@pytest.mark.asyncio
async def test_admin_api_supports_message_reparse_and_settings_update(admin_client, runtime, seeded_message) -> None:
    reparse = await admin_client.post(f"/api/v1/admin/messages/{seeded_message.message_id}/reparse")
    settings_response = await admin_client.patch(
        "/api/v1/admin/settings",
        json={"max_recipients_per_message": "25"},
    )
    audit = await admin_client.get("/api/v1/admin/audit-logs")

    assert reparse.status_code == 202
    assert settings_response.status_code == 200
    assert audit.status_code == 200
    assert any(item["action"] == "settings.update" for item in audit.json()["items"])
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_admin_api.py::test_admin_api_supports_message_reparse_and_settings_update -v`
Expected: FAIL because these admin endpoints and services do not exist yet

- [ ] **Step 3: Implement service methods for message and settings operations**

```python
async def reparse_message(self, message_id: str) -> None:
    await self.writer.execute(
        lambda connection: connection.execute(
            "UPDATE messages SET parse_status = 'pending', parse_error = NULL WHERE id = ?",
            (message_id,),
        )
    )
    await self.runtime.parse_queue.enqueue(ParseTask(message_id=message_id))
```

- [ ] **Step 4: Implement the admin API routes and audit writes**

```python
@router.post("/api/v1/admin/messages/{message_id}/reparse", status_code=status.HTTP_202_ACCEPTED)
async def reparse_message(message_id: str, request: Request, admin=Depends(require_admin_key)) -> dict:
    await request.app.state.runtime.messages.reparse_message(message_id)
    await request.app.state.runtime.audit.log("api_key", str(admin["id"]), "messages.reparse", "message", message_id, "success")
    return {"queued": True, "message_id": message_id}
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_admin_api.py::test_admin_api_supports_message_reparse_and_settings_update -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/http/admin_api.py app/services/domains.py app/services/mailboxes.py app/services/messages.py app/services/audit.py app/services/settings.py app/runtime.py tests/test_admin_api.py
git commit -m "feat: add admin operations for messages settings and audit"
```

### Task 7: Admin HTML Login, Dashboard, And Operations Pages

**Files:**
- Create: `app/http/admin_views.py`
- Modify: `app/main.py`
- Create: `app/templates/admin/base.html`
- Create: `app/templates/admin/login.html`
- Create: `app/templates/admin/dashboard.html`
- Create: `app/templates/admin/domains.html`
- Create: `app/templates/admin/domain_detail.html`
- Create: `app/templates/admin/mailboxes.html`
- Create: `app/templates/admin/messages.html`
- Create: `app/templates/admin/api_keys.html`
- Create: `app/templates/admin/audit.html`
- Create: `app/templates/admin/settings.html`
- Create: `tests/test_admin_views.py`

- [ ] **Step 1: Write the failing admin HTML test**

```python
@pytest.mark.asyncio
async def test_admin_login_and_dashboard_page_flow(app_client, runtime) -> None:
    response = await app_client.post(
        "/admin/login",
        data={"username": "admin", "password": runtime.settings.bootstrap_admin_password},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Rapid Inbox Admin" in response.text
    assert "Domains" in response.text
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_admin_views.py::test_admin_login_and_dashboard_page_flow -v`
Expected: FAIL because admin HTML routes and templates do not exist yet

- [ ] **Step 3: Implement login and session-protected admin views**

```python
@router.post("/admin/login")
async def login(request: Request, username: str = Form(), password: str = Form()) -> Response:
    admin = await request.app.state.runtime.auth.authenticate_admin(username, password)
    session = await request.app.state.runtime.auth.create_session(admin["id"], request.client.host, request.headers.get("user-agent", ""))
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(request.app.state.settings.session_cookie_name, session["token"], httponly=True, samesite="lax")
    return response
```

- [ ] **Step 4: Add the dashboard template skeleton**

```html
<h1>Rapid Inbox Admin</h1>
<nav>
  <a href="/admin/domains">Domains</a>
  <a href="/admin/mailboxes">Mailboxes</a>
  <a href="/admin/messages">Messages</a>
  <a href="/admin/live">Live</a>
</nav>
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_admin_views.py::test_admin_login_and_dashboard_page_flow -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/http/admin_views.py app/main.py app/templates/admin/base.html app/templates/admin/login.html app/templates/admin/dashboard.html app/templates/admin/domains.html app/templates/admin/domain_detail.html app/templates/admin/mailboxes.html app/templates/admin/messages.html app/templates/admin/api_keys.html app/templates/admin/audit.html app/templates/admin/settings.html tests/test_admin_views.py
git commit -m "feat: add admin html login and dashboard views"
```

### Task 8: Live SMTP SSE, Session History, And DNS Check

**Files:**
- Modify: `app/smtp/live_state.py`
- Modify: `app/smtp/handler.py`
- Create: `app/http/sse.py`
- Create: `app/services/dns_check.py`
- Modify: `app/http/admin_api.py`
- Modify: `pyproject.toml`
- Create: `app/templates/admin/live.html`
- Create: `tests/test_live_sse.py`
- Create: `tests/test_dns_check.py`

- [ ] **Step 1: Write the failing live stream test**

```python
@pytest.mark.asyncio
async def test_live_sse_stream_emits_recent_rcpt_event(admin_client, runtime, sample_email_bytes: bytes) -> None:
    await runtime.create_domain("adb.com")
    await runtime.accept_message(
        rcpt_tos=["foo@adb.com"],
        envelope_from="sender@example.com",
        content=sample_email_bytes,
        smtp_session_id="smtp_live_1",
    )

    response = await admin_client.get("/api/v1/admin/live/smtp/stream")

    assert response.status_code == 200
    assert "rcpt_accepted" in response.text or "queued" in response.text
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_live_sse.py::test_live_sse_stream_emits_recent_rcpt_event -v`
Expected: FAIL because SSE route and richer live-state event history do not exist

- [ ] **Step 3: Implement live snapshots and SSE formatting**

```python
def encode_sse(event: dict[str, object]) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


@router.get("/api/v1/admin/live/smtp/stream")
async def smtp_stream(request: Request, admin=Depends(require_admin_live_access)) -> StreamingResponse:
    async def event_source() -> AsyncIterator[str]:
        for event in request.app.state.runtime.live_state.snapshot():
            yield encode_sse(event)
    return StreamingResponse(event_source(), media_type="text/event-stream")
```

- [ ] **Step 4: Add DNS check service and endpoint**

```python
async def run_dns_check(self, root_domain: str) -> dict[str, object]:
    try:
        answers = dns.resolver.resolve(root_domain, "MX")
        records = sorted(str(answer.exchange).rstrip(".") for answer in answers)
        return {"status": "ok", "mx_records": records}
    except Exception as exc:
        return {"status": "warning", "error": str(exc), "mx_records": []}
```

```toml
dependencies = [
  "aiosmtpd>=1.4.6",
  "dnspython>=2.7.0",
  "fastapi>=0.115.0",
  "jinja2>=3.1.4",
  "uvicorn>=0.30.0",
]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_live_sse.py::test_live_sse_stream_emits_recent_rcpt_event tests/test_dns_check.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/smtp/live_state.py app/smtp/handler.py app/http/sse.py app/services/dns_check.py app/http/admin_api.py pyproject.toml app/templates/admin/live.html tests/test_live_sse.py tests/test_dns_check.py
git commit -m "feat: add live smtp sse and dns check support"
```

### Task 9: Startup Entrypoints, README, And Final End-To-End Verification

**Files:**
- Modify: `pyproject.toml`
- Modify: `.gitignore`
- Create: `.env.example`
- Create: `README.md`
- Create: `app/http_runner.py`
- Create: `app/smtp_runner.py`
- Modify: `tests/test_public_routes.py`
- Modify: `tests/test_smtp_handler.py`

- [ ] **Step 1: Write the failing end-to-end bootstrap test**

```python
def test_settings_include_bootstrap_and_operational_defaults(tmp_path: Path) -> None:
    settings = Settings(storage_root=tmp_path / "storage", database_path=tmp_path / "storage" / "app.db")

    assert settings.bootstrap_admin_username == "admin"
    assert settings.bootstrap_admin_password
    assert settings.max_recipients_per_message == 20
    assert settings.session_cookie_name == "rapid_inbox_session"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_config.py::test_settings_include_bootstrap_and_operational_defaults -v`
Expected: FAIL because the additional runtime settings are not defined yet

- [ ] **Step 3: Add entrypoints and documented defaults**

```toml
[project.scripts]
rapid-inbox-http = "app.http_runner:main"
rapid-inbox-smtp = "app.smtp_runner:main"
```

```python
def main() -> None:
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)
```

```python
async def main_async() -> None:
    settings = default_settings(Path.cwd())
    runtime = RapidInboxRuntime(settings)
    await runtime.start()
    server = SMTPServer(runtime)
    server.start()
    try:
        await asyncio.Event().wait()
    finally:
        server.stop()
        await runtime.stop()
```

- [ ] **Step 4: Add local developer docs**

```markdown
1. `python3.12 -m venv .venv`
2. `.venv/bin/pip install -e .[dev]`
3. `.venv/bin/uvicorn app.main:app --reload`
4. `.venv/bin/python -m app.smtp_runner`
5. Open `http://127.0.0.1:8000/admin/login`
```

- [ ] **Step 5: Run full verification**

Run: `.venv/bin/pytest -v`
Expected: PASS for all unit, service, and end-to-end tests

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml .gitignore .env.example README.md app/http_runner.py app/smtp_runner.py tests/test_public_routes.py tests/test_smtp_handler.py tests/test_config.py
git commit -m "feat: add runtime entrypoints and local run documentation"
```

## Self-Review

- Spec coverage:
  - Shared runtime and client fixtures are covered in Task 0.
  - SMTP ingress, raw durability, manifest recovery, and async parsing are covered in Tasks 1-2.
  - Admin auth, sessions, API keys, scopes, grants, and seeded data fixtures are covered in Tasks 3-4.
  - Public HTML/API reads, raw/attachment/html delivery, and HTML isolation are covered in Task 5.
  - Admin API and admin HTML surfaces are covered in Tasks 6-7.
  - Live SSE, session visibility, and DNS checks are covered in Task 8.
  - Startup flow, bootstrap defaults, and docs are covered in Task 9.
- Placeholder scan:
  - No `TODO`, `TBD`, or “similar to previous task” placeholders remain.
- Type consistency:
  - Shared names stay consistent across tasks: `Settings`, `RapidInboxRuntime`, `ParseTask`, `delivery_id`, `message_id`, `public.read`, `session_cookie_name`, `bootstrap_admin_username`.
