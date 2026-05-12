from __future__ import annotations

import base64
import json

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import Response

from app.auth.api_keys import set_active_permission_context
from app.services.attachments import AttachmentService
from app.services.messages import MessageService


router = APIRouter()


def require_public_api_key(request: Request, api_key: str | None, query_api_key: str | None = None) -> None:
    set_active_permission_context(None)
    credential = api_key or query_api_key
    if not credential:
        raise HTTPException(status_code=401, detail="invalid api key")

    transport = "header" if api_key else "query"
    try:
        context = request.app.state.runtime.api_keys.authenticate_public_credential(
            credential,
            transport=transport,
        )
    except LookupError as exc:
        raise HTTPException(status_code=401, detail="invalid api key") from exc
    set_active_permission_context(context)


def _message_service(request: Request) -> MessageService:
    return MessageService(request.app.state.runtime)


def _attachment_service(request: Request) -> AttachmentService:
    runtime = request.app.state.runtime
    return AttachmentService(runtime, _message_service(request))


def _decode_cursor(cursor: str | None) -> tuple[str, str] | None:
    if cursor is None or not cursor.strip():
        return None
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail="invalid cursor") from exc
    delivered_at = payload.get("delivered_at")
    delivery_id = payload.get("delivery_id")
    if not isinstance(delivered_at, str) or not isinstance(delivery_id, str):
        raise HTTPException(status_code=422, detail="invalid cursor")
    return delivered_at, delivery_id


def _encode_cursor(cursor: dict[str, str] | None) -> str | None:
    if cursor is None:
        return None
    payload = json.dumps(cursor, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


@router.get("/api/v1/public/mailboxes/{mailbox_address}/messages")
async def list_mailbox_messages(
    mailbox_address: str,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    api_key: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0, le=1_000_000),
    cursor: str | None = Query(default=None),
) -> dict:
    require_public_api_key(request, x_api_key, api_key)
    request_ip = request.client.host if request.client is not None else None
    try:
        result = await _message_service(request).get_public_mailbox_view(
            mailbox_address,
            surface="api",
            limit=limit,
            offset=offset,
            cursor=_decode_cursor(cursor),
            request_ip=request_ip,
        )
        result["next_cursor"] = _encode_cursor(result.get("next_cursor"))
        result["pagination"] = {
            "mode": result["pagination_mode"],
            "next_cursor": result["next_cursor"],
            "limit": result["limit"],
            "offset": result["offset"],
        }
        return result
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        set_active_permission_context(None)


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
        return await _message_service(request).get_public_mailbox_verification_codes(
            mailbox_address,
            limit=limit,
            offset=offset,
            request_ip=request_ip,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        set_active_permission_context(None)


@router.get("/api/v1/public/mailboxes/{mailbox_address}/messages/{delivery_id}")
async def get_mailbox_message(
    mailbox_address: str,
    delivery_id: str,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    api_key: str | None = Query(default=None),
) -> dict:
    require_public_api_key(request, x_api_key, api_key)
    request_ip = request.client.host if request.client is not None else None
    try:
        return await _message_service(request).get_public_delivery_detail(
            mailbox_address,
            delivery_id,
            surface="api",
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
        return await _message_service(request).get_public_delivery_verification_code(
            mailbox_address,
            delivery_id,
            request_ip=request_ip,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        set_active_permission_context(None)


@router.get("/api/v1/public/mailboxes/{mailbox_address}/messages/{delivery_id}/raw")
async def get_mailbox_message_raw(
    mailbox_address: str,
    delivery_id: str,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    api_key: str | None = Query(default=None),
) -> Response:
    require_public_api_key(request, x_api_key, api_key)
    request_ip = request.client.host if request.client is not None else None
    try:
        raw_bytes = await _message_service(request).get_public_raw_message(
            mailbox_address,
            delivery_id,
            surface="api",
            request_ip=request_ip,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        set_active_permission_context(None)
    return Response(raw_bytes, media_type="message/rfc822")


@router.get("/api/v1/public/mailboxes/{mailbox_address}/messages/{delivery_id}/attachments/{attachment_id}")
async def get_mailbox_message_attachment(
    mailbox_address: str,
    delivery_id: str,
    attachment_id: str,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    api_key: str | None = Query(default=None),
) -> Response:
    require_public_api_key(request, x_api_key, api_key)
    request_ip = request.client.host if request.client is not None else None
    service = _attachment_service(request)
    try:
        attachment = await service.get_delivery_attachment(
            mailbox_address,
            delivery_id,
            attachment_id,
            surface="api",
            request_ip=request_ip,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        set_active_permission_context(None)
    return Response(
        attachment["content"],
        media_type=attachment.get("content_type") or "application/octet-stream",
        headers=service.build_attachment_response_headers(attachment),
    )
