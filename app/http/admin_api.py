from __future__ import annotations

import json
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, Response, StreamingResponse

from app.auth.permissions import PermissionContext
from app.http.sse import LIVE_SSE_EVENT_TYPES, recent_smtp_sessions, smtp_live_snapshot, stream_smtp_live_events
from app.ingest.storage import utc_now
from app.services.dns_check import DnsCheckService


router = APIRouter()


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client is not None else None


async def _current_admin_session(request: Request) -> dict[str, Any] | None:
    cookie_name = request.app.state.settings.session_cookie_name
    token = request.cookies.get(cookie_name)
    if not token:
        return None

    try:
        return await request.app.state.runtime.auth.get_session_admin(token, ip=_client_ip(request))
    except LookupError:
        return None


def _session_permission_context(admin: dict[str, Any]) -> PermissionContext:
    return PermissionContext(
        scopes=("live.read",),
        domain_ids=(),
        mailbox_patterns=(),
        api_key_id=None,
        public_id=str(admin.get("session_id") or admin.get("username") or "admin-session"),
        name=str(admin.get("display_name") or admin.get("username") or "admin-session"),
        kind="admin",
        legacy_credential=True,
    )


def _render_template(request: Request, template_name: str, context: dict[str, Any], *, status_code: int = 200) -> Response:
    response = request.app.state.templates.TemplateResponse(request, template_name, context)
    response.status_code = status_code
    return response


def require_admin_key(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> PermissionContext:
    if x_api_key is None:
        raise HTTPException(status_code=401, detail="invalid admin api key")

    if x_api_key == request.app.state.settings.admin_token:
        return PermissionContext(
            scopes=(),
            domain_ids=(),
            mailbox_patterns=(),
            api_key_id=None,
            public_id="legacy-admin-token",
            name="legacy-admin-token",
            kind="admin",
            legacy_credential=True,
        )

    try:
        context = request.app.state.runtime.api_keys.authenticate_plain_text(x_api_key)
    except LookupError as exc:
        raise HTTPException(status_code=401, detail="invalid admin api key") from exc

    if context.kind not in {"admin", "service"}:
        raise HTTPException(status_code=403, detail="invalid admin api key")

    return context


require_admin_api_key = require_admin_key


def require_admin_scope(admin: PermissionContext, required_scope: str) -> None:
    if admin.legacy_credential:
        return
    if required_scope not in admin.scopes:
        if required_scope.endswith(".read"):
            write_scope = f"{required_scope[:-5]}.write"
            if write_scope in admin.scopes:
                return
        raise HTTPException(status_code=403, detail=required_scope)


async def _record_admin_key_usage(request: Request, admin: PermissionContext) -> None:
    if admin.api_key_id is None:
        return
    request_ip = request.client.host if request.client is not None else None
    await request.app.state.runtime.api_keys.record_usage(admin, ip=request_ip)


def _audit_actor_ref(admin: PermissionContext) -> str:
    if admin.api_key_id is not None:
        return str(admin.api_key_id)
    return admin.public_id or "legacy-admin-token"


async def _write_audit_best_effort(
    request: Request,
    admin: PermissionContext,
    action: str,
    resource_type: str,
    resource_ref: str | None,
    status_value: str,
) -> None:
    try:
        await request.app.state.runtime.audit.log(
            "api_key",
            _audit_actor_ref(admin),
            action,
            resource_type,
            resource_ref,
            status_value,
        )
    except Exception:
        # Mutation already completed; audit writes are best-effort here.
        return


async def require_admin_live_access(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> PermissionContext:
    if x_api_key is not None:
        admin = require_admin_key(request, x_api_key)
        require_admin_scope(admin, "live.read")
        return admin

    admin_session = await _current_admin_session(request)
    if admin_session is None:
        raise HTTPException(status_code=404, detail="live page not found")
    return _session_permission_context(admin_session)


@router.get("/admin/live", response_class=HTMLResponse)
async def live_page(request: Request) -> Response:
    admin = await _current_admin_session(request)
    if admin is None:
        raise HTTPException(status_code=404, detail="live page not found")

    runtime = request.app.state.runtime
    live_events, live_cursor = runtime.live_state.snapshot_state()
    initial_events = live_events or smtp_live_snapshot(runtime)
    return _render_template(
        request,
        "admin/live.html",
        {
            "page_title": "Live",
            "admin": admin,
            "events": initial_events,
            "sessions": recent_smtp_sessions(runtime),
            "stream_url": f"/api/v1/admin/live/smtp/stream?after_cursor={live_cursor}",
            "live_event_types": LIVE_SSE_EVENT_TYPES,
        },
    )


@router.get("/api/v1/admin/live/smtp/stream")
async def smtp_stream(
    request: Request,
    _admin: PermissionContext = Depends(require_admin_live_access),
    after_cursor: str | None = Query(default=None),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    return StreamingResponse(
        stream_smtp_live_events(
            request.app.state.runtime,
            after_cursor=after_cursor,
            last_event_id=last_event_id,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/v1/admin/domains/{domain_id}/dns-check")
async def run_domain_dns_check(
    domain_id: int,
    request: Request,
    admin: PermissionContext = Depends(require_admin_key),
) -> dict[str, Any]:
    require_admin_scope(admin, "domains.write")
    await _record_admin_key_usage(request, admin)
    runtime = request.app.state.runtime

    try:
        domain = runtime.domains.get_domain(domain_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    dns_check = DnsCheckService()
    check_result = await dns_check.run_dns_check(domain["root_domain_ascii"])
    checked_at = utc_now()
    stored_result = {
        "domain_id": domain_id,
        "root_domain_ascii": domain["root_domain_ascii"],
        "checked_at": checked_at,
        **check_result,
    }

    def operation(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            UPDATE domains
            SET dns_status = ?,
                dns_last_checked_at = ?,
                dns_details_json = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                check_result["status"],
                checked_at,
                json.dumps(stored_result, ensure_ascii=False),
                checked_at,
                domain_id,
            ),
        )

    await runtime.writer.execute(operation)
    updated_domain = runtime.domains.get_domain(domain_id)
    updated_domain["dns_check"] = stored_result
    await _write_audit_best_effort(request, admin, "domains.dns_check", "domain", str(domain_id), "success")
    return updated_domain


@router.get("/api/v1/admin/domains")
async def list_domains(
    request: Request,
    admin: PermissionContext = Depends(require_admin_key),
) -> dict:
    require_admin_scope(admin, "domains.read")
    await _record_admin_key_usage(request, admin)
    runtime = request.app.state.runtime
    return {"items": runtime.list_domains()}


@router.post("/api/v1/admin/domains", status_code=status.HTTP_201_CREATED)
async def create_domain(
    payload: dict[str, Any],
    request: Request,
    admin: PermissionContext = Depends(require_admin_key),
) -> dict:
    require_admin_scope(admin, "domains.write")
    await _record_admin_key_usage(request, admin)
    runtime = request.app.state.runtime
    root_domain = payload.get("root_domain")
    if not isinstance(root_domain, str) or not root_domain.strip():
        raise HTTPException(status_code=422, detail="root_domain is required")
    settings = runtime.get_settings()

    try:
        created = await runtime.create_domain(
            root_domain,
            accept_exact=payload.get("accept_exact", True),
            accept_subdomains=payload.get("accept_subdomains", True),
            public_web_enabled=payload.get("public_web_enabled", True),
            public_api_enabled=payload.get("public_api_enabled", True),
            plus_addressing_mode=payload.get("plus_addressing_mode", "keep"),
            local_part_case_sensitive=payload.get("local_part_case_sensitive", False),
            max_message_size_bytes=payload.get("max_message_size_bytes", settings["max_message_size_bytes"]),
        )
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await _write_audit_best_effort(request, admin, "domains.create", "domain", str(created["id"]), "success")
    return created


@router.post("/api/v1/admin/messages/{message_id}/reparse", status_code=status.HTTP_202_ACCEPTED)
async def reparse_message(
    message_id: str,
    request: Request,
    admin: PermissionContext = Depends(require_admin_key),
) -> dict:
    require_admin_scope(admin, "messages.write")
    await _record_admin_key_usage(request, admin)
    try:
        await request.app.state.runtime.messages.reparse_message(message_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await _write_audit_best_effort(request, admin, "messages.reparse", "message", message_id, "success")
    return {"queued": True, "message_id": message_id}


@router.get("/api/v1/admin/audit-logs")
async def list_audit_logs(
    request: Request,
    admin: PermissionContext = Depends(require_admin_key),
    limit: int = Query(default=100, ge=0, le=1000),
    offset: int = Query(default=0, ge=0, le=1_000_000),
) -> dict:
    require_admin_scope(admin, "audit.read")
    await _record_admin_key_usage(request, admin)
    return request.app.state.runtime.audit.list_logs(limit=limit, offset=offset)


@router.get("/api/v1/admin/settings")
async def get_settings(
    request: Request,
    admin: PermissionContext = Depends(require_admin_key),
) -> dict:
    require_admin_scope(admin, "system.read")
    await _record_admin_key_usage(request, admin)
    return request.app.state.runtime.system_settings.get_settings()


@router.patch("/api/v1/admin/settings")
async def update_settings(
    payload: dict[str, Any],
    request: Request,
    admin: PermissionContext = Depends(require_admin_key),
) -> dict:
    require_admin_scope(admin, "system.write")
    await _record_admin_key_usage(request, admin)
    try:
        updated = await request.app.state.runtime.system_settings.update_settings(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await _write_audit_best_effort(request, admin, "settings.update", "system_settings", None, "success")
    return updated
