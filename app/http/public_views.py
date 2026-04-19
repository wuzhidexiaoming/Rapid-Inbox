from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response

from app.http.pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, build_pagination_context
from app.services.attachments import AttachmentService
from app.services.messages import MessageService


router = APIRouter()


def _message_service(request: Request) -> MessageService:
    return MessageService(request.app.state.runtime)


def _attachment_service(request: Request) -> AttachmentService:
    runtime = request.app.state.runtime
    return AttachmentService(runtime, _message_service(request))


@router.get("/", response_class=HTMLResponse)
async def home_page(request: Request) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request,
        "public/home.html",
        {
            "page_title": "首页",
            "mailbox_example": "收件@示例域名.cn",
            "mail_route_prefix": "/mail/PLACEHOLDER",
        },
    )


@router.get("/mail/{mailbox_address}", response_class=HTMLResponse)
async def mailbox_page(
    mailbox_address: str,
    request: Request,
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
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
    pagination = build_pagination_context(
        path=f"/mail/{mailbox['mailbox']}",
        limit=mailbox["limit"],
        offset=mailbox["offset"],
        total_count=mailbox["message_count"],
        item_count=len(mailbox["items"]),
    )
    return request.app.state.templates.TemplateResponse(
        request,
        "public/mailbox.html",
        {
            "page_title": mailbox["mailbox"],
            "mailbox_address": mailbox["mailbox"],
            "items": mailbox["items"],
            "message_count": mailbox["message_count"],
            **pagination,
        },
    )


@router.get("/mail/{mailbox_address}/{delivery_id}", response_class=HTMLResponse)
async def message_page(mailbox_address: str, delivery_id: str, request: Request) -> HTMLResponse:
    service = _message_service(request)
    try:
        detail = await service.get_public_delivery_detail(mailbox_address, delivery_id, surface="web")
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    detail["page_title"] = detail.get("subject") or "邮件详情"
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
        {"page_title": "网页预览", "srcdoc": srcdoc},
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
