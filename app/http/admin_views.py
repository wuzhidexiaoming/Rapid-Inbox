from __future__ import annotations

import sqlite3
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.auth.sessions import SESSION_DURATION_DAYS
from app.db.connection import connect_database
from app.http.pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, build_pagination_context


router = APIRouter()

API_KEY_SCOPE_OPTIONS = (
    {
        "value": "public.read",
        "label": "公开邮件读取",
        "description": "允许读取公开邮箱的列表、详情、原文与附件。",
    },
    {
        "value": "live.read",
        "label": "实时会话查看",
        "description": "允许查看后台实时 SMTP 会话面板。",
    },
    {
        "value": "domains.read",
        "label": "域名只读",
        "description": "允许查看域名列表、详情与 DNS 检查结果。",
    },
    {
        "value": "domains.write",
        "label": "域名管理",
        "description": "允许新增域名并修改域名相关配置。",
    },
    {
        "value": "mailboxes.write",
        "label": "邮箱管理",
        "description": "允许修改邮箱的公开状态和隐藏状态。",
    },
    {
        "value": "messages.write",
        "label": "邮件重解析",
        "description": "允许触发邮件重新解析与修复处理。",
    },
    {
        "value": "audit.read",
        "label": "审计日志读取",
        "description": "允许查看后台审计日志记录。",
    },
    {
        "value": "system.read",
        "label": "系统设置只读",
        "description": "允许查看当前系统运行配置。",
    },
    {
        "value": "system.write",
        "label": "系统设置修改",
        "description": "允许修改系统级运行参数。",
    },
    {
        "value": "api_keys.write",
        "label": "API 密钥管理",
        "description": "允许创建和吊销 API 密钥。",
    },
)

API_KEY_STATUS_OPTIONS = (
    {"value": "active", "label": "可用"},
    {"value": "disabled", "label": "停用"},
    {"value": "expired", "label": "过期"},
    {"value": "revoked", "label": "吊销"},
)


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client is not None else None


def _render(request: Request, template_name: str, context: dict[str, Any], *, status_code: int = 200) -> Response:
    response = request.app.state.templates.TemplateResponse(request, template_name, context)
    response.status_code = status_code
    return response


def _redirect_to_login() -> RedirectResponse:
    return RedirectResponse("/admin/login", status_code=status.HTTP_303_SEE_OTHER)


def _redirect_to_dashboard() -> RedirectResponse:
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)


def _parse_form_body(body: bytes) -> dict[str, str]:
    if not body:
        return {}
    parsed = parse_qs(body.decode("utf-8", errors="ignore"), keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items() if values}


def _parse_form_body_lists(body: bytes) -> dict[str, list[str]]:
    if not body:
        return {}
    parsed = parse_qs(body.decode("utf-8", errors="ignore"), keep_blank_values=True)
    return {key: values for key, values in parsed.items() if values}


def _form_bool(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_csv_values(value: str | None) -> list[str]:
    if value is None:
        return []
    return [item.strip() for item in value.replace("\n", ",").split(",") if item.strip()]


def _parse_int_values(value: str | None) -> list[int]:
    return [int(item) for item in _parse_csv_values(value)]


def _parse_multi_text_values(values: list[str] | None) -> list[str]:
    if not values:
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = value.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        normalized.append(item)
    return normalized


def _parse_multi_int_values(values: list[str] | None) -> list[int]:
    return [int(item) for item in _parse_multi_text_values(values)]


def _parse_nullable_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _parse_domain_grant_form(
    form: dict[str, list[str]],
    *,
    default_mode: str = "all",
) -> tuple[str, list[int]]:
    raw_mode = (form.get("domain_grant_mode") or [None])[-1]
    if raw_mode is None:
        mode = "selected" if form.get("domain_ids") else default_mode
    else:
        mode = raw_mode.strip() or default_mode
    if mode not in {"all", "selected"}:
        raise ValueError("invalid domain grant mode")
    if mode == "all":
        return mode, []

    domain_ids = _parse_multi_int_values(form.get("domain_ids"))
    if not domain_ids:
        raise ValueError("empty selected domain grants")
    return mode, domain_ids


def _parse_positive_int(value: str | None, *, default: int, field_name: str) -> int:
    if value is None or not value.strip():
        return default
    try:
        normalized = int(value)
    except ValueError as exc:
        raise ValueError(f"invalid {field_name}") from exc
    if normalized < 1:
        raise ValueError(f"invalid {field_name}")
    return normalized


def _parse_non_negative_int(value: str | None, *, default: int, field_name: str) -> int:
    if value is None or not value.strip():
        return default
    try:
        normalized = int(value)
    except ValueError as exc:
        raise ValueError(f"invalid {field_name}") from exc
    if normalized < 0:
        raise ValueError(f"invalid {field_name}")
    return normalized


def _count(connection, query: str, params: tuple[Any, ...] = ()) -> int:
    row = connection.execute(query, params).fetchone()
    if row is None:
        return 0
    return int(row["count"])


async def _current_admin(request: Request) -> dict[str, Any] | None:
    cookie_name = request.app.state.settings.session_cookie_name
    token = request.cookies.get(cookie_name)
    if not token:
        return None

    try:
        return await request.app.state.runtime.auth.get_session_admin(token, ip=_client_ip(request))
    except LookupError:
        return None


async def _require_admin(request: Request) -> dict[str, Any] | Response:
    admin = await _current_admin(request)
    if admin is None:
        return _redirect_to_login()
    return admin


def _dashboard_stats(request: Request) -> dict[str, Any]:
    runtime = request.app.state.runtime
    with connect_database(runtime.settings.database_path) as connection:
        stats = {
            "open_sessions": _count(connection, "SELECT COUNT(*) AS count FROM smtp_sessions WHERE status = 'open'"),
            "domains": _count(connection, "SELECT COUNT(*) AS count FROM domains"),
            "mailboxes": _count(connection, "SELECT COUNT(*) AS count FROM mailboxes"),
            "messages": _count(connection, "SELECT COUNT(*) AS count FROM messages"),
            "pending_messages": _count(connection, "SELECT COUNT(*) AS count FROM messages WHERE parse_status = 'pending'"),
            "failed_messages": _count(connection, "SELECT COUNT(*) AS count FROM messages WHERE parse_status = 'failed'"),
            "api_keys": _count(connection, "SELECT COUNT(*) AS count FROM api_keys"),
            "audit_logs": _count(connection, "SELECT COUNT(*) AS count FROM audit_logs"),
        }
        recent_messages = [
            dict(row)
            for row in connection.execute(
                """
                SELECT
                    m.id,
                    m.subject,
                    m.from_addr,
                    m.received_at,
                    m.parse_status,
                    m.attachment_count
                FROM messages AS m
                ORDER BY m.received_at DESC, m.id DESC
                LIMIT 5
                """
            ).fetchall()
        ]
        recent_domains = [
            dict(row)
            for row in connection.execute(
                """
                SELECT
                    id,
                    root_domain_ascii,
                    is_active,
                    created_at
                FROM domains
                ORDER BY created_at DESC, id DESC
                LIMIT 5
                """
            ).fetchall()
        ]

    return {
        "stats": [
            {
                "label": "当前 SMTP 会话",
                "value": stats["open_sessions"],
                "hint": "仍在监听器上保持连接的 SMTP 会话数。",
            },
            {
                "label": "已接入域名",
                "value": stats["domains"],
                "hint": "正在托管并接收邮件的根域名数量。",
            },
            {
                "label": "已收录邮箱",
                "value": stats["mailboxes"],
                "hint": "系统中已建立索引的公开邮箱数量。",
            },
            {
                "label": "邮件总数",
                "value": stats["messages"],
                "hint": "已被系统接收并建立索引的邮件数量。",
            },
            {
                "label": "待解析邮件",
                "value": stats["pending_messages"],
                "hint": "已经入库、仍在等待 MIME 解析的邮件。",
            },
            {
                "label": "解析失败",
                "value": stats["failed_messages"],
                "hint": "解析出错、需要人工关注的邮件。",
            },
            {
                "label": "API 密钥",
                "value": stats["api_keys"],
                "hint": "用于管理端、服务端与公开访问的密钥总数。",
            },
            {
                "label": "审计日志",
                "value": stats["audit_logs"],
                "hint": "已经记录的管理操作与系统行为。",
            },
        ],
        "recent_domains": recent_domains,
        "recent_messages": recent_messages,
    }


def _list_recent_messages(request: Request, *, limit: int = 100) -> list[dict[str, Any]]:
    with connect_database(request.app.state.runtime.settings.database_path) as connection:
        rows = connection.execute(
            """
            SELECT
                m.id,
                m.subject,
                m.from_addr,
                m.received_at,
                m.parse_status,
                m.parse_error,
                m.has_attachments,
                m.attachment_count,
                COUNT(d.id) AS delivery_count
            FROM messages AS m
            LEFT JOIN message_deliveries AS d ON d.message_id = m.id
            GROUP BY m.id
            ORDER BY m.received_at DESC, m.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def _list_messages_page(request: Request, *, limit: int, offset: int) -> list[dict[str, Any]]:
    with connect_database(request.app.state.runtime.settings.database_path) as connection:
        rows = connection.execute(
            """
            SELECT
                m.id,
                m.subject,
                m.from_addr,
                COALESCE(
                    (
                        SELECT GROUP_CONCAT(rcpt_to, ', ')
                        FROM (
                            SELECT DISTINCT rcpt_to
                            FROM message_deliveries
                            WHERE message_id = m.id
                            ORDER BY rcpt_to ASC
                        )
                    ),
                    ''
                ) AS recipients,
                m.received_at,
                m.parse_status,
                m.parse_error,
                m.has_attachments,
                m.attachment_count,
                COUNT(d.id) AS delivery_count
            FROM messages AS m
            LEFT JOIN message_deliveries AS d ON d.message_id = m.id
            GROUP BY m.id
            ORDER BY m.received_at DESC, m.id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    return [dict(row) for row in rows]


def _list_api_keys(request: Request, *, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    with connect_database(request.app.state.runtime.settings.database_path) as connection:
        rows = connection.execute(
            """
            SELECT
                k.id,
                k.public_id,
                k.name,
                k.description,
                k.kind,
                k.status,
                k.allow_header,
                k.allow_query,
                k.rate_limit_per_min,
                k.expires_at,
                k.last_used_at,
                k.last_used_ip,
                k.created_at,
                COALESCE(
                    (
                        SELECT GROUP_CONCAT(scope, ', ')
                        FROM api_key_scopes
                        WHERE api_key_id = k.id
                    ),
                    ''
                ) AS scopes,
                (
                    SELECT COUNT(*)
                    FROM api_key_domain_grants
                    WHERE api_key_id = k.id
                ) AS domain_count,
                (
                    SELECT COUNT(*)
                    FROM api_key_mailbox_grants
                    WHERE api_key_id = k.id
                ) AS mailbox_count
            FROM api_keys AS k
            ORDER BY k.created_at DESC, k.id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    return [dict(row) for row in rows]


def _count_table_rows(request: Request, table_name: str) -> int:
    with connect_database(request.app.state.runtime.settings.database_path) as connection:
        return _count(connection, f"SELECT COUNT(*) AS count FROM {table_name}")


def _api_keys_page_context(
    request: Request,
    admin: dict[str, Any],
    *,
    limit: int = DEFAULT_PAGE_SIZE,
    offset: int = 0,
    created_api_key: dict[str, Any] | None = None,
    error: str | None = None,
    create_form: dict[str, Any] | None = None,
) -> dict[str, Any]:
    api_keys = _list_api_keys(request, limit=limit, offset=offset)
    total_count = _count_table_rows(request, "api_keys")
    return {
        "page_title": "API 密钥",
        "admin": admin,
        "api_keys": api_keys,
        "available_scopes": API_KEY_SCOPE_OPTIONS,
        "available_domains": request.app.state.runtime.list_domains(),
        "created_api_key": created_api_key,
        "create_form": create_form or _api_key_form_values(),
        "error": error,
        "pagination": build_pagination_context(
            path="/admin/api-keys",
            limit=limit,
            offset=offset,
            total_count=total_count,
            item_count=len(api_keys),
        ),
    }


def _api_key_form_values(form: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = form or {}
    scopes = payload.get("scopes", [])
    domain_ids = payload.get("domain_ids", [])
    domain_grant_mode = str(
        payload.get("domain_grant_mode") or ("selected" if domain_ids else "all")
    )
    return {
        "name": str(payload.get("name", "新的 API 密钥") or "新的 API 密钥"),
        "kind": str(payload.get("kind", "admin") or "admin"),
        "scopes": [str(item) for item in scopes],
        "domain_grant_mode": domain_grant_mode,
        "domain_ids": [str(item) for item in domain_ids],
        "mailbox_patterns": str(payload.get("mailbox_patterns", "") or ""),
    }


def _api_key_edit_form_values(api_key: dict[str, Any], form: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = form or api_key
    scopes = payload.get("scopes", [])
    domain_ids = payload.get("domain_ids", [])
    mailbox_patterns = payload.get("mailbox_patterns", [])
    allowed_ip_cidrs = payload.get("allowed_ip_cidrs", [])
    domain_grant_mode = str(
        payload.get("domain_grant_mode") or ("selected" if domain_ids else "all")
    )
    return {
        "name": str(payload.get("name", "") or ""),
        "description": str(payload.get("description", "") or ""),
        "kind": str(payload.get("kind", "admin") or "admin"),
        "status": str(payload.get("status", "active") or "active"),
        "scopes": [str(item) for item in scopes],
        "domain_grant_mode": domain_grant_mode,
        "domain_ids": [str(item) for item in domain_ids],
        "mailbox_patterns": (
            ", ".join(str(item) for item in mailbox_patterns)
            if isinstance(mailbox_patterns, list)
            else str(mailbox_patterns or "")
        ),
        "allow_header": bool(payload.get("allow_header", True)),
        "allow_query": bool(payload.get("allow_query", False)),
        "rate_limit_per_min": str(payload.get("rate_limit_per_min", "3600") or "0"),
        "allowed_ip_cidrs": (
            ", ".join(str(item) for item in allowed_ip_cidrs)
            if isinstance(allowed_ip_cidrs, list)
            else str(allowed_ip_cidrs or "")
        ),
        "expires_at": str(payload.get("expires_at", "") or ""),
    }


def _api_key_edit_context(
    request: Request,
    admin: dict[str, Any],
    api_key_id: int,
    *,
    error: str | None = None,
    form: dict[str, Any] | None = None,
    updated: bool = False,
) -> dict[str, Any]:
    try:
        api_key = request.app.state.runtime.api_keys.get_key(api_key_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return {
        "page_title": f"编辑 API 密钥：{api_key['name']}",
        "admin": admin,
        "api_key": api_key,
        "available_scopes": API_KEY_SCOPE_OPTIONS,
        "available_statuses": API_KEY_STATUS_OPTIONS,
        "available_domains": request.app.state.runtime.list_domains(),
        "form": form or _api_key_edit_form_values(api_key),
        "error": error,
        "updated": updated,
    }


def _domain_form_values(request: Request, form: dict[str, str] | None = None) -> dict[str, Any]:
    settings = request.app.state.runtime.get_settings()
    payload = form or {}
    return {
        "root_domain": payload.get("root_domain", ""),
        "accept_exact": _form_bool(payload["accept_exact"]) if "accept_exact" in payload else True,
        "accept_subdomains": _form_bool(payload["accept_subdomains"]) if "accept_subdomains" in payload else True,
        "public_web_enabled": _form_bool(payload["public_web_enabled"]) if "public_web_enabled" in payload else True,
        "public_api_enabled": _form_bool(payload["public_api_enabled"]) if "public_api_enabled" in payload else True,
        "local_part_case_sensitive": (
            _form_bool(payload["local_part_case_sensitive"]) if "local_part_case_sensitive" in payload else False
        ),
        "is_active": _form_bool(payload["is_active"]) if "is_active" in payload else True,
        "plus_addressing_mode": payload.get("plus_addressing_mode", "keep") or "keep",
        "max_message_size_bytes": payload.get(
            "max_message_size_bytes",
            str(settings["max_message_size_bytes"]),
        )
        or str(settings["max_message_size_bytes"]),
    }


def _domain_form_error_message(exc: Exception) -> str:
    if isinstance(exc, sqlite3.IntegrityError):
        return "该域名已经存在，不能重复添加。"

    message = str(exc)
    error_map = {
        "invalid root_domain": "请输入有效的根域名，例如 `adb.com`。",
        "invalid accept_exact": "根域接收选项无效。",
        "invalid accept_subdomains": "子域接收选项无效。",
        "invalid public_web_enabled": "公开网页访问选项无效。",
        "invalid public_api_enabled": "公开接口访问选项无效。",
        "invalid plus_addressing_mode": "加号寻址策略无效。",
        "invalid local_part_case_sensitive": "大小写选项无效。",
        "invalid is_active": "启用状态选项无效。",
        "invalid max_message_size_bytes": "最大邮件大小必须是大于 0 的整数。",
    }
    return error_map.get(message, message or "提交的域名信息无效。")


def _render_domains_page(
    request: Request,
    admin: dict[str, Any],
    *,
    status_code: int = 200,
    create_error: str | None = None,
    create_form: dict[str, Any] | None = None,
) -> Response:
    return _render(
        request,
        "admin/domains.html",
        {
            "page_title": "域名",
            "admin": admin,
            "domains": request.app.state.runtime.list_domains(),
            "create_error": create_error,
            "create_form": create_form or _domain_form_values(request),
        },
        status_code=status_code,
    )


def _domain_mailboxes(request: Request, domain_id: int, *, limit: int = 100) -> list[dict[str, Any]]:
    with connect_database(request.app.state.runtime.settings.database_path) as connection:
        rows = connection.execute(
            """
            SELECT
                id,
                address_canonical,
                message_count,
                latest_message_at,
                public_enabled,
                is_hidden,
                notes
            FROM mailboxes
            WHERE domain_id = ?
            ORDER BY latest_message_at DESC, id DESC
            LIMIT ?
            """,
            (domain_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


@router.get("/admin/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Response:
    admin = await _current_admin(request)
    if admin is not None:
        return _redirect_to_dashboard()
    return _render(
        request,
        "admin/login.html",
        {
            "page_title": "管理员登录",
            "error": None,
            "username": "",
        },
    )


@router.post("/admin/login")
async def login(request: Request) -> Response:
    form = _parse_form_body(await request.body())
    username = form.get("username", "").strip()
    password = form.get("password", "")
    if not username or not password:
        return _render(
            request,
            "admin/login.html",
            {
                "page_title": "管理员登录",
                "error": "用户名和密码不能为空。",
                "username": username,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        admin = await request.app.state.runtime.auth.authenticate_admin(username, password, ip=_client_ip(request))
    except LookupError:
        return _render(
            request,
            "admin/login.html",
            {
                "page_title": "管理员登录",
                "error": "用户名或密码不正确。",
                "username": username,
            },
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    session = await request.app.state.runtime.auth.create_session(
        admin_id=admin["id"],
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    response = _redirect_to_dashboard()
    response.set_cookie(
        request.app.state.settings.session_cookie_name,
        session["token"],
        httponly=True,
        samesite="lax",
        max_age=SESSION_DURATION_DAYS * 24 * 60 * 60,
        path="/",
    )
    return response


@router.post("/admin/logout")
async def logout(request: Request) -> Response:
    cookie_name = request.app.state.settings.session_cookie_name
    admin = await _current_admin(request)
    if admin is not None:
        try:
            await request.app.state.runtime.auth.revoke_session(admin["session_id"])
        except Exception:
            pass

    response = _redirect_to_login()
    response.delete_cookie(cookie_name, path="/")
    return response


@router.get("/admin", response_class=HTMLResponse)
async def dashboard_page(request: Request) -> Response:
    admin_or_response = await _require_admin(request)
    if isinstance(admin_or_response, Response):
        return admin_or_response

    summary = _dashboard_stats(request)
    return _render(
        request,
        "admin/dashboard.html",
        {
            "page_title": "仪表盘",
            "admin": admin_or_response,
            **summary,
        },
    )


@router.get("/admin/domains", response_class=HTMLResponse)
async def domains_page(request: Request) -> Response:
    admin_or_response = await _require_admin(request)
    if isinstance(admin_or_response, Response):
        return admin_or_response

    return _render_domains_page(request, admin_or_response)


@router.post("/admin/domains")
async def create_domain_from_form(request: Request) -> Response:
    admin_or_response = await _require_admin(request)
    if isinstance(admin_or_response, Response):
        return admin_or_response

    form = _parse_form_body(await request.body())
    form_values = _domain_form_values(request, form)
    try:
        created = await request.app.state.runtime.create_domain(
            form.get("root_domain", "").strip(),
            accept_exact=_form_bool(form.get("accept_exact")),
            accept_subdomains=_form_bool(form.get("accept_subdomains")),
            public_web_enabled=_form_bool(form.get("public_web_enabled")),
            public_api_enabled=_form_bool(form.get("public_api_enabled")),
            plus_addressing_mode=form.get("plus_addressing_mode", "keep").strip() or "keep",
            local_part_case_sensitive=_form_bool(form.get("local_part_case_sensitive")),
            is_active=_form_bool(form.get("is_active")),
            max_message_size_bytes=_parse_positive_int(
                form.get("max_message_size_bytes"),
                default=int(request.app.state.runtime.get_settings()["max_message_size_bytes"]),
                field_name="max_message_size_bytes",
            ),
        )
    except (ValueError, sqlite3.IntegrityError) as exc:
        return _render_domains_page(
            request,
            admin_or_response,
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            create_error=_domain_form_error_message(exc),
            create_form=form_values,
        )

    return RedirectResponse(f"/admin/domains/{created['id']}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/admin/domains/{domain_id}", response_class=HTMLResponse)
async def domain_detail_page(domain_id: int, request: Request) -> Response:
    admin_or_response = await _require_admin(request)
    if isinstance(admin_or_response, Response):
        return admin_or_response

    try:
        domain = request.app.state.runtime.domains.get_domain(domain_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return _render(
        request,
        "admin/domain_detail.html",
        {
            "page_title": f"{domain['root_domain_ascii']}",
            "admin": admin_or_response,
            "domain": domain,
            "mailboxes": _domain_mailboxes(request, domain_id),
        },
    )


@router.get("/admin/mailboxes", response_class=HTMLResponse)
async def mailboxes_page(
    request: Request,
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0, le=1_000_000),
) -> Response:
    admin_or_response = await _require_admin(request)
    if isinstance(admin_or_response, Response):
        return admin_or_response

    mailboxes = request.app.state.runtime.mailboxes.list_mailboxes(
        limit=limit,
        offset=offset,
    )["items"]
    total_count = _count_table_rows(request, "mailboxes")
    return _render(
        request,
        "admin/mailboxes.html",
        {
            "page_title": "邮箱",
            "admin": admin_or_response,
            "mailboxes": mailboxes,
            "pagination": build_pagination_context(
                path="/admin/mailboxes",
                limit=limit,
                offset=offset,
                total_count=total_count,
                item_count=len(mailboxes),
            ),
        },
    )


@router.post("/admin/mailboxes/{mailbox_id}")
async def update_mailbox_visibility(mailbox_id: int, request: Request) -> Response:
    admin_or_response = await _require_admin(request)
    if isinstance(admin_or_response, Response):
        return admin_or_response

    form = _parse_form_body(await request.body())
    updates: dict[str, Any] = {}
    if "public_enabled" in form:
        updates["public_enabled"] = _form_bool(form.get("public_enabled"))
    if "is_hidden" in form:
        updates["is_hidden"] = _form_bool(form.get("is_hidden"))
    limit = _parse_positive_int(form.get("limit"), default=DEFAULT_PAGE_SIZE, field_name="limit")
    offset = _parse_non_negative_int(form.get("offset"), default=0, field_name="offset")

    try:
        await request.app.state.runtime.mailboxes.update_mailbox(mailbox_id, updates)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    return RedirectResponse(
        f"/admin/mailboxes?limit={limit}&offset={offset}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/admin/messages", response_class=HTMLResponse)
async def messages_page(
    request: Request,
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0, le=1_000_000),
) -> Response:
    admin_or_response = await _require_admin(request)
    if isinstance(admin_or_response, Response):
        return admin_or_response

    messages = _list_messages_page(request, limit=limit, offset=offset)
    with connect_database(request.app.state.runtime.settings.database_path) as connection:
        total_count = _count(connection, "SELECT COUNT(*) AS count FROM messages")
    return _render(
        request,
        "admin/messages.html",
        {
            "page_title": "邮件",
            "admin": admin_or_response,
            "messages": messages,
            "pagination": build_pagination_context(
                path="/admin/messages",
                limit=limit,
                offset=offset,
                total_count=total_count,
                item_count=len(messages),
            ),
        },
    )


@router.get("/admin/api-keys", response_class=HTMLResponse)
async def api_keys_page(
    request: Request,
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0, le=1_000_000),
) -> Response:
    admin_or_response = await _require_admin(request)
    if isinstance(admin_or_response, Response):
        return admin_or_response

    return _render(
        request,
        "admin/api_keys.html",
        _api_keys_page_context(request, admin_or_response, limit=limit, offset=offset),
    )


@router.post("/admin/api-keys", response_class=HTMLResponse)
async def create_api_key(request: Request) -> Response:
    admin_or_response = await _require_admin(request)
    if isinstance(admin_or_response, Response):
        return admin_or_response

    form = _parse_form_body_lists(await request.body())
    name = (form.get("name") or [""])[-1].strip()
    kind = ((form.get("kind") or ["admin"])[-1].strip() or "admin")
    scopes = _parse_multi_text_values(form.get("scopes"))
    mailbox_patterns = _parse_csv_values((form.get("mailbox_patterns") or [""])[-1])
    try:
        domain_grant_mode, domain_ids = _parse_domain_grant_form(form)
    except ValueError as exc:
        domain_ids = []
        domain_grant_mode = (form.get("domain_grant_mode") or ["all"])[-1]
        error_message = (
            "请选择至少一个授权域名，或切换为授权所有可用域名。"
            if str(exc) == "empty selected domain grants"
            else "授权域名选择无效。"
        )
        create_form = _api_key_form_values(
            {
                "name": name,
                "kind": kind,
                "scopes": scopes,
                "domain_grant_mode": domain_grant_mode,
                "domain_ids": [],
                "mailbox_patterns": (form.get("mailbox_patterns") or [""])[-1],
            }
        )
        return _render(
            request,
            "admin/api_keys.html",
            _api_keys_page_context(request, admin_or_response, error=error_message, create_form=create_form),
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    create_form = _api_key_form_values(
        {
            "name": name,
            "kind": kind,
            "scopes": scopes,
            "domain_grant_mode": domain_grant_mode,
            "domain_ids": domain_ids,
            "mailbox_patterns": (form.get("mailbox_patterns") or [""])[-1],
        }
    )

    if not name:
        return _render(
            request,
            "admin/api_keys.html",
            _api_keys_page_context(request, admin_or_response, error="名称不能为空。", create_form=create_form),
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    try:
        created = await request.app.state.runtime.api_keys.create_key(
            name=name,
            kind=kind,
            scopes=scopes,
            domain_ids=domain_ids,
            mailbox_patterns=mailbox_patterns,
        )
    except (ValueError, sqlite3.IntegrityError) as exc:
        return _render(
            request,
            "admin/api_keys.html",
            _api_keys_page_context(request, admin_or_response, error=str(exc), create_form=create_form),
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    return _render(
        request,
        "admin/api_keys.html",
        _api_keys_page_context(request, admin_or_response, created_api_key=created),
        status_code=status.HTTP_200_OK,
    )


@router.get("/admin/api-keys/{api_key_id}", response_class=HTMLResponse)
async def api_key_detail_page(
    api_key_id: int,
    request: Request,
    updated: int = Query(default=0, ge=0, le=1),
) -> Response:
    admin_or_response = await _require_admin(request)
    if isinstance(admin_or_response, Response):
        return admin_or_response

    return _render(
        request,
        "admin/api_key_detail.html",
        _api_key_edit_context(request, admin_or_response, api_key_id, updated=bool(updated)),
    )


@router.post("/admin/api-keys/{api_key_id}", response_class=HTMLResponse)
async def update_api_key_from_form(api_key_id: int, request: Request) -> Response:
    admin_or_response = await _require_admin(request)
    if isinstance(admin_or_response, Response):
        return admin_or_response

    try:
        current_api_key = request.app.state.runtime.api_keys.get_key(api_key_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    form = _parse_form_body_lists(await request.body())
    name = (form.get("name") or [""])[-1].strip()
    kind = ((form.get("kind") or [current_api_key["kind"]])[-1].strip() or current_api_key["kind"])
    status_value = ((form.get("status") or ["active"])[-1].strip() or "active")
    scopes = _parse_multi_text_values(form.get("scopes"))
    mailbox_patterns_raw = (form.get("mailbox_patterns") or [""])[-1]
    allowed_ip_cidrs_raw = (form.get("allowed_ip_cidrs") or [""])[-1]
    expires_at = _parse_nullable_text((form.get("expires_at") or [""])[-1])
    try:
        domain_grant_mode, domain_ids = _parse_domain_grant_form(form)
        rate_limit_per_min = _parse_non_negative_int(
            (form.get("rate_limit_per_min") or ["3600"])[-1],
            default=3600,
            field_name="rate_limit_per_min",
        )
    except ValueError as exc:
        domain_grant_mode = (form.get("domain_grant_mode") or ["all"])[-1]
        domain_ids = []
        error_message = (
            "请选择至少一个授权域名，或切换为授权所有可用域名。"
            if str(exc) == "empty selected domain grants"
            else "提交的密钥配置无效。"
        )
        edit_form = _api_key_edit_form_values(
            current_api_key,
            {
                "name": name,
                "description": (form.get("description") or [""])[-1],
                "kind": kind,
                "status": status_value,
                "scopes": scopes,
                "domain_grant_mode": domain_grant_mode,
                "domain_ids": domain_ids,
                "mailbox_patterns": mailbox_patterns_raw,
                "allow_header": _form_bool((form.get("allow_header") or [None])[-1]),
                "allow_query": _form_bool((form.get("allow_query") or [None])[-1]),
                "rate_limit_per_min": (form.get("rate_limit_per_min") or ["3600"])[-1],
                "allowed_ip_cidrs": allowed_ip_cidrs_raw,
                "expires_at": expires_at or "",
            },
        )
        return _render(
            request,
            "admin/api_key_detail.html",
            _api_key_edit_context(
                request,
                admin_or_response,
                api_key_id,
                error=error_message,
                form=edit_form,
            ),
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    edit_form = {
        "name": name,
        "description": (form.get("description") or [""])[-1],
        "kind": kind,
        "status": status_value,
        "scopes": scopes,
        "domain_grant_mode": domain_grant_mode,
        "domain_ids": domain_ids,
        "mailbox_patterns": mailbox_patterns_raw,
        "allow_header": _form_bool((form.get("allow_header") or [None])[-1]),
        "allow_query": _form_bool((form.get("allow_query") or [None])[-1]),
        "rate_limit_per_min": str(rate_limit_per_min),
        "allowed_ip_cidrs": allowed_ip_cidrs_raw,
        "expires_at": expires_at or "",
    }

    if not name:
        return _render(
            request,
            "admin/api_key_detail.html",
            _api_key_edit_context(
                request,
                admin_or_response,
                api_key_id,
                error="名称不能为空。",
                form=_api_key_edit_form_values(current_api_key, edit_form),
            ),
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    try:
        await request.app.state.runtime.api_keys.update_key(
            api_key_id,
            name=name,
            description=_parse_nullable_text((form.get("description") or [""])[-1]),
            kind=kind,
            status=status_value,
            scopes=scopes,
            domain_ids=domain_ids,
            mailbox_patterns=_parse_csv_values(mailbox_patterns_raw),
            allow_header=edit_form["allow_header"],
            allow_query=edit_form["allow_query"],
            rate_limit_per_min=rate_limit_per_min,
            allowed_ip_cidrs=_parse_csv_values(allowed_ip_cidrs_raw),
            expires_at=expires_at,
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (ValueError, sqlite3.IntegrityError) as exc:
        return _render(
            request,
            "admin/api_key_detail.html",
            _api_key_edit_context(
                request,
                admin_or_response,
                api_key_id,
                error=str(exc),
                form=_api_key_edit_form_values(current_api_key, edit_form),
            ),
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    return RedirectResponse(f"/admin/api-keys/{api_key_id}?updated=1", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/admin/api-keys/{api_key_id}/revoke")
async def revoke_api_key(api_key_id: int, request: Request) -> Response:
    admin_or_response = await _require_admin(request)
    if isinstance(admin_or_response, Response):
        return admin_or_response

    try:
        await request.app.state.runtime.api_keys.revoke_key(api_key_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    form = _parse_form_body(await request.body())
    limit = _parse_positive_int(form.get("limit"), default=DEFAULT_PAGE_SIZE, field_name="limit")
    offset = _parse_non_negative_int(form.get("offset"), default=0, field_name="offset")
    return RedirectResponse(
        f"/admin/api-keys?limit={limit}&offset={offset}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/admin/audit", response_class=HTMLResponse)
async def audit_page(
    request: Request,
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0, le=1_000_000),
) -> Response:
    admin_or_response = await _require_admin(request)
    if isinstance(admin_or_response, Response):
        return admin_or_response

    with connect_database(request.app.state.runtime.settings.database_path) as connection:
        total_count = _count(connection, "SELECT COUNT(*) AS count FROM audit_logs")
    logs = request.app.state.runtime.audit.list_logs(limit=limit, offset=offset)["items"]
    return _render(
        request,
        "admin/audit.html",
        {
            "page_title": "审计日志",
            "admin": admin_or_response,
            "logs": logs,
            "pagination": build_pagination_context(
                path="/admin/audit",
                limit=limit,
                offset=offset,
                total_count=total_count,
                item_count=len(logs),
            ),
        },
    )


def _mail_store_stats(request: Request) -> dict[str, int]:
    with connect_database(request.app.state.runtime.settings.database_path) as connection:
        return {
            "messages": _count(connection, "SELECT COUNT(*) AS count FROM messages"),
            "deliveries": _count(connection, "SELECT COUNT(*) AS count FROM message_deliveries"),
            "mailboxes": _count(connection, "SELECT COUNT(*) AS count FROM mailboxes"),
            "attachments": _count(connection, "SELECT COUNT(*) AS count FROM attachments"),
            "smtp_sessions": _count(connection, "SELECT COUNT(*) AS count FROM smtp_sessions"),
        }


def _settings_items(request: Request) -> list[dict[str, Any]]:
    runtime_settings = request.app.state.runtime.get_settings()
    app_settings = request.app.state.settings
    return [
        {
            "label": "最大邮件大小",
            "value": runtime_settings["max_message_size_bytes"],
            "hint": "系统允许接收的单封邮件大小上限（字节）。",
        },
        {
            "label": "单封邮件最大收件人数",
            "value": runtime_settings["max_recipients_per_message"],
            "hint": "单次 SMTP 事务允许的 RCPT TO 数量上限。",
        },
        {
            "label": "会话 Cookie 名称",
            "value": app_settings.session_cookie_name,
            "hint": "管理后台 HTML 会话使用的 Cookie 名称。",
        },
        {
            "label": "初始管理员账号",
            "value": app_settings.bootstrap_admin_username,
            "hint": "系统启动时自动创建的管理员用户名。",
        },
    ]


def _settings_context(
    request: Request,
    admin: dict[str, Any],
    *,
    mail_clear_result: dict[str, int] | None = None,
    password_changed: bool = False,
    password_error: str | None = None,
) -> dict[str, Any]:
    return {
        "page_title": "系统设置",
        "admin": admin,
        "settings_items": _settings_items(request),
        "mail_store_stats": _mail_store_stats(request),
        "mail_clear_result": mail_clear_result,
        "password_changed": password_changed,
        "password_error": password_error,
    }


@router.get("/admin/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    mail_cleared: int = Query(default=0, ge=0, le=1),
    cleared_messages: int = Query(default=0, ge=0),
    cleared_mailboxes: int = Query(default=0, ge=0),
    cleared_sessions: int = Query(default=0, ge=0),
    database_size_before_bytes: int = Query(default=0, ge=0),
    database_size_after_bytes: int = Query(default=0, ge=0),
    database_vacuumed: int = Query(default=0, ge=0, le=1),
    password_changed: int = Query(default=0, ge=0, le=1),
) -> Response:
    admin_or_response = await _require_admin(request)
    if isinstance(admin_or_response, Response):
        return admin_or_response

    return _render(
        request,
        "admin/settings.html",
        _settings_context(
            request,
            admin_or_response,
            mail_clear_result={
                "messages": cleared_messages,
                "mailboxes": cleared_mailboxes,
                "smtp_sessions": cleared_sessions,
                "database_size_before_bytes": database_size_before_bytes,
                "database_size_after_bytes": database_size_after_bytes,
                "database_vacuumed": database_vacuumed,
            } if mail_cleared else None,
            password_changed=bool(password_changed),
        ),
    )


@router.post("/admin/settings/password")
async def change_admin_password(request: Request) -> Response:
    admin_or_response = await _require_admin(request)
    if isinstance(admin_or_response, Response):
        return admin_or_response

    form = _parse_form_body(await request.body())
    current_password = form.get("current_password", "")
    new_password = form.get("new_password", "")
    confirm_password = form.get("confirm_password", "")

    password_error: str | None = None
    if not current_password or not new_password or not confirm_password:
        password_error = "请填写当前密码和新密码。"
    elif len(new_password) < 8:
        password_error = "新密码至少需要 8 个字符。"
    elif new_password != confirm_password:
        password_error = "两次输入的新密码不一致。"

    if password_error is not None:
        return _render(
            request,
            "admin/settings.html",
            _settings_context(request, admin_or_response, password_error=password_error),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        await request.app.state.runtime.auth.change_admin_password(
            int(admin_or_response["id"]),
            current_password,
            new_password,
        )
    except LookupError:
        return _render(
            request,
            "admin/settings.html",
            _settings_context(request, admin_or_response, password_error="当前密码不正确。"),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    await request.app.state.runtime.audit.log(
        "admin",
        str(admin_or_response.get("username") or admin_or_response.get("id") or "admin"),
        "admin.password.update",
        "admin",
        str(admin_or_response.get("id")),
        "success",
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return RedirectResponse("/admin/settings?password_changed=1", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/admin/settings/clear-mail")
async def clear_mail_store(request: Request) -> Response:
    admin_or_response = await _require_admin(request)
    if isinstance(admin_or_response, Response):
        return admin_or_response

    form = _parse_form_body(await request.body())
    if form.get("confirm") != "clear-all-mail":
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="confirmation required")

    result = await request.app.state.runtime.clear_all_mail()
    await request.app.state.runtime.audit.log(
        "admin",
        str(admin_or_response.get("username") or admin_or_response.get("id") or "admin"),
        "mail.clear_all",
        "mail_store",
        None,
        "success",
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
        details=result,
    )
    return RedirectResponse(
        (
            "/admin/settings"
            f"?mail_cleared=1&cleared_messages={result['messages']}"
            f"&cleared_mailboxes={result['mailboxes']}"
            f"&cleared_sessions={result['smtp_sessions']}"
            f"&database_size_before_bytes={result.get('database_size_before_bytes', 0)}"
            f"&database_size_after_bytes={result.get('database_size_after_bytes', 0)}"
            f"&database_vacuumed={result.get('database_vacuumed', 0)}"
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )
