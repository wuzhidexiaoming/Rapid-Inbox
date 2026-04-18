from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from app.services.attachments import AttachmentService
from app.services.messages import MessageService


router = APIRouter()


def _message_service(request: Request) -> MessageService:
    return MessageService(request.app.state.runtime)


def _attachment_service(request: Request) -> AttachmentService:
    runtime = request.app.state.runtime
    return AttachmentService(runtime, _message_service(request))


@router.get("/mail/{mailbox_address}", response_class=HTMLResponse)
async def mailbox_page(mailbox_address: str, request: Request) -> HTMLResponse:
    runtime = request.app.state.runtime
    try:
        mailbox = await runtime.get_mailbox_view(mailbox_address)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return request.app.state.templates.TemplateResponse(
        request,
        "public/mailbox.html",
        {
            "mailbox_address": mailbox["mailbox"],
            "items": mailbox["items"],
        },
    )


@router.get("/mail/{mailbox_address}/{delivery_id}", response_class=HTMLResponse)
async def message_page(mailbox_address: str, delivery_id: str, request: Request) -> HTMLResponse:
    service = _message_service(request)
    try:
        detail = await service.get_delivery_detail(mailbox_address, delivery_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return request.app.state.templates.TemplateResponse(
        request,
        "public/message.html",
        detail,
    )


@router.get("/mail/{mailbox_address}/{delivery_id}/raw")
async def message_raw(mailbox_address: str, delivery_id: str, request: Request) -> Response:
    service = _message_service(request)
    try:
        raw_bytes = await service.get_raw_message(mailbox_address, delivery_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(raw_bytes, media_type="message/rfc822")


@router.get("/mail/{mailbox_address}/{delivery_id}/html", response_class=HTMLResponse)
async def message_html_frame(mailbox_address: str, delivery_id: str, request: Request) -> HTMLResponse:
    service = _message_service(request)
    try:
        detail = await service.get_delivery_detail(mailbox_address, delivery_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return request.app.state.templates.TemplateResponse(
        request,
        "public/html_frame.html",
        {"html_body": detail["html_body"] or ""},
        headers={
            "Content-Security-Policy": "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'",
        },
    )


@router.get("/mail/{mailbox_address}/{delivery_id}/attachments/{attachment_id}")
async def message_attachment(
    mailbox_address: str,
    delivery_id: str,
    attachment_id: str,
    request: Request,
) -> Response:
    service = _attachment_service(request)
    try:
        attachment = await service.get_delivery_attachment(mailbox_address, delivery_id, attachment_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    disposition = "inline" if attachment.get("is_inline") else "attachment"
    safe_filename = attachment.get("safe_filename") or "attachment.bin"
    return Response(
        attachment["content"],
        media_type=attachment.get("content_type") or "application/octet-stream",
        headers={"Content-Disposition": f'{disposition}; filename="{safe_filename}"'},
    )
