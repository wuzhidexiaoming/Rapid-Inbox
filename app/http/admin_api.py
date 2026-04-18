from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from app.auth.permissions import PermissionContext


router = APIRouter()


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


@router.get("/api/v1/admin/domains")
async def list_domains(
    request: Request,
    admin: PermissionContext = Depends(require_admin_key),
) -> dict:
    require_admin_scope(admin, "domains.write")
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
    await runtime.audit.log(
        "api_key",
        _audit_actor_ref(admin),
        "domains.create",
        "domain",
        str(created["id"]),
        "success",
    )
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
    await request.app.state.runtime.audit.log(
        "api_key",
        _audit_actor_ref(admin),
        "messages.reparse",
        "message",
        message_id,
        "success",
    )
    return {"queued": True, "message_id": message_id}


@router.get("/api/v1/admin/audit-logs")
async def list_audit_logs(
    request: Request,
    admin: PermissionContext = Depends(require_admin_key),
    limit: int = 100,
    offset: int = 0,
) -> dict:
    require_admin_scope(admin, "audit.read")
    await _record_admin_key_usage(request, admin)
    return request.app.state.runtime.audit.list_logs(limit=limit, offset=offset)


@router.get("/api/v1/admin/settings")
async def get_settings(
    request: Request,
    admin: PermissionContext = Depends(require_admin_key),
) -> dict:
    require_admin_scope(admin, "system.write")
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
    await request.app.state.runtime.audit.log(
        "api_key",
        _audit_actor_ref(admin),
        "settings.update",
        "system_settings",
        None,
        "success",
    )
    return updated
