from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request
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
) -> dict:
    require_public_api_key(request, x_api_key)
    runtime = request.app.state.runtime
    request_ip = request.client.host if request.client is not None else None
    try:
        return await runtime.get_mailbox_view(mailbox_address, request_ip=request_ip)
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
    runtime = request.app.state.runtime
    request_ip = request.client.host if request.client is not None else None
    try:
        return await runtime.get_delivery_detail(mailbox_address, delivery_id, request_ip=request_ip)
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
        raw_bytes = await _message_service(request).get_raw_message(
            mailbox_address,
            delivery_id,
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
    try:
        attachment = await _attachment_service(request).get_delivery_attachment(
            mailbox_address,
            delivery_id,
            attachment_id,
            request_ip=request_ip,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        set_active_permission_context(None)
    disposition = "inline" if attachment.get("is_inline") else "attachment"
    safe_filename = attachment.get("safe_filename") or "attachment.bin"
    return Response(
        attachment["content"],
        media_type=attachment.get("content_type") or "application/octet-stream",
        headers={"Content-Disposition": f'{disposition}; filename="{safe_filename}"'},
    )
