from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import Response

from app.auth.api_keys import set_active_permission_context
from app.services.attachments import AttachmentService
from app.services.messages import MessageService


router = APIRouter()


def require_public_api_key(request: Request, api_key: str | None) -> None:
    if api_key != request.app.state.settings.public_api_key:
        raise HTTPException(status_code=401, detail="invalid api key")


def _message_service(request: Request) -> MessageService:
    return MessageService(request.app.state.runtime)


def _attachment_service(request: Request) -> AttachmentService:
    runtime = request.app.state.runtime
    return AttachmentService(runtime, _message_service(request))


@router.get("/api/v1/public/mailboxes/{mailbox_address}/messages")
async def list_mailbox_messages(
    mailbox_address: str,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0, le=1_000_000),
) -> dict:
    require_public_api_key(request, x_api_key)
    request_ip = request.client.host if request.client is not None else None
    try:
        return await _message_service(request).get_public_mailbox_view(
            mailbox_address,
            surface="api",
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
) -> dict:
    require_public_api_key(request, x_api_key)
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


@router.get("/api/v1/public/mailboxes/{mailbox_address}/messages/{delivery_id}/raw")
async def get_mailbox_message_raw(
    mailbox_address: str,
    delivery_id: str,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> Response:
    require_public_api_key(request, x_api_key)
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
) -> Response:
    require_public_api_key(request, x_api_key)
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
