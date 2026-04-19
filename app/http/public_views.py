from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
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
async def mailbox_page(
    mailbox_address: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0, le=1_000_000),
) -> HTMLResponse:
    service = _message_service(request)
    try:
        mailbox = await service.get_public_mailbox_view(
            mailbox_address,
            surface="web",
            limit=limit,
            offset=offset,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return request.app.state.templates.TemplateResponse(
        request,
        "public/mailbox.html",
        {
            "mailbox_address": mailbox["mailbox"],
            "items": mailbox["items"],
            "message_count": mailbox["message_count"],
            "limit": mailbox["limit"],
            "offset": mailbox["offset"],
            "start_index": offset + 1 if mailbox["items"] else 0,
            "end_index": offset + len(mailbox["items"]),
            "has_previous": mailbox["has_previous"],
            "has_next": mailbox["has_next"],
            "previous_offset": mailbox["previous_offset"],
            "next_offset": mailbox["next_offset"],
            "previous_page_url": (
                f"/mail/{mailbox['mailbox']}?limit={mailbox['limit']}&offset={mailbox['previous_offset']}"
                if mailbox["has_previous"]
                else None
            ),
            "next_page_url": (
                f"/mail/{mailbox['mailbox']}?limit={mailbox['limit']}&offset={mailbox['next_offset']}"
                if mailbox["has_next"]
                else None
            ),
        },
    )


@router.get("/mail/{mailbox_address}/{delivery_id}", response_class=HTMLResponse)
async def message_page(mailbox_address: str, delivery_id: str, request: Request) -> HTMLResponse:
    service = _message_service(request)
    try:
        detail = await service.get_public_delivery_detail(mailbox_address, delivery_id, surface="web")
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
        raw_bytes = await service.get_public_raw_message(mailbox_address, delivery_id, surface="web")
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(raw_bytes, media_type="message/rfc822")


@router.get("/mail/{mailbox_address}/{delivery_id}/html", response_class=HTMLResponse)
async def message_html_frame(mailbox_address: str, delivery_id: str, request: Request) -> HTMLResponse:
    service = _message_service(request)
    try:
        srcdoc = await service.get_public_html_preview_srcdoc(mailbox_address, delivery_id, surface="web")
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return request.app.state.templates.TemplateResponse(
        request,
        "public/html_frame.html",
        {"srcdoc": srcdoc},
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
        attachment = await service.get_delivery_attachment(
            mailbox_address,
            delivery_id,
            attachment_id,
            surface="web",
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(
        attachment["content"],
        media_type=attachment.get("content_type") or "application/octet-stream",
        headers=service.build_attachment_response_headers(attachment),
    )
