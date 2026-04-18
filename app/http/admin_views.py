from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.auth.sessions import SESSION_DURATION_DAYS
from app.db.connection import connect_database


router = APIRouter()


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
                "label": "Active SMTP sessions",
                "value": stats["open_sessions"],
                "hint": "Sessions currently open on the SMTP listener.",
            },
            {
                "label": "Domains",
                "value": stats["domains"],
                "hint": "Managed root domains.",
            },
            {
                "label": "Mailboxes",
                "value": stats["mailboxes"],
                "hint": "Normalized recipient addresses.",
            },
            {
                "label": "Messages",
                "value": stats["messages"],
                "hint": "Raw messages stored in SQLite.",
            },
            {
                "label": "Pending parses",
                "value": stats["pending_messages"],
                "hint": "Messages waiting for MIME parsing.",
            },
            {
                "label": "Failed parses",
                "value": stats["failed_messages"],
                "hint": "Messages that need attention.",
            },
            {
                "label": "API keys",
                "value": stats["api_keys"],
                "hint": "Admin, service, and public keys.",
            },
            {
                "label": "Audit logs",
                "value": stats["audit_logs"],
                "hint": "Recorded administrative actions.",
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


def _list_api_keys(request: Request, *, limit: int = 100) -> list[dict[str, Any]]:
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
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


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
            "page_title": "Admin Login",
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
                "page_title": "Admin Login",
                "error": "Username and password are required.",
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
                "page_title": "Admin Login",
                "error": "Invalid username or password.",
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
            "page_title": "Dashboard",
            "admin": admin_or_response,
            **summary,
        },
    )


@router.get("/admin/domains", response_class=HTMLResponse)
async def domains_page(request: Request) -> Response:
    admin_or_response = await _require_admin(request)
    if isinstance(admin_or_response, Response):
        return admin_or_response

    return _render(
        request,
        "admin/domains.html",
        {
            "page_title": "Domains",
            "admin": admin_or_response,
            "domains": request.app.state.runtime.list_domains(),
        },
    )


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
async def mailboxes_page(request: Request) -> Response:
    admin_or_response = await _require_admin(request)
    if isinstance(admin_or_response, Response):
        return admin_or_response

    return _render(
        request,
        "admin/mailboxes.html",
        {
            "page_title": "Mailboxes",
            "admin": admin_or_response,
            "mailboxes": request.app.state.runtime.mailboxes.list_mailboxes()["items"],
        },
    )


@router.get("/admin/messages", response_class=HTMLResponse)
async def messages_page(request: Request) -> Response:
    admin_or_response = await _require_admin(request)
    if isinstance(admin_or_response, Response):
        return admin_or_response

    return _render(
        request,
        "admin/messages.html",
        {
            "page_title": "Messages",
            "admin": admin_or_response,
            "messages": _list_recent_messages(request),
        },
    )


@router.get("/admin/api-keys", response_class=HTMLResponse)
async def api_keys_page(request: Request) -> Response:
    admin_or_response = await _require_admin(request)
    if isinstance(admin_or_response, Response):
        return admin_or_response

    return _render(
        request,
        "admin/api_keys.html",
        {
            "page_title": "API Keys",
            "admin": admin_or_response,
            "api_keys": _list_api_keys(request),
        },
    )


@router.get("/admin/audit", response_class=HTMLResponse)
async def audit_page(request: Request) -> Response:
    admin_or_response = await _require_admin(request)
    if isinstance(admin_or_response, Response):
        return admin_or_response

    return _render(
        request,
        "admin/audit.html",
        {
            "page_title": "Audit",
            "admin": admin_or_response,
            "logs": request.app.state.runtime.audit.list_logs(limit=100)["items"],
        },
    )


@router.get("/admin/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> Response:
    admin_or_response = await _require_admin(request)
    if isinstance(admin_or_response, Response):
        return admin_or_response

    runtime_settings = request.app.state.runtime.get_settings()
    app_settings = request.app.state.settings
    settings_items = [
        {
            "label": "max_message_size_bytes",
            "value": runtime_settings["max_message_size_bytes"],
            "hint": "Maximum accepted message size.",
        },
        {
            "label": "max_recipients_per_message",
            "value": runtime_settings["max_recipients_per_message"],
            "hint": "Maximum RCPT TO count per SMTP transaction.",
        },
        {
            "label": "session_cookie_name",
            "value": app_settings.session_cookie_name,
            "hint": "Cookie name used for admin HTML sessions.",
        },
        {
            "label": "bootstrap_admin_username",
            "value": app_settings.bootstrap_admin_username,
            "hint": "Initial admin account created at startup.",
        },
    ]

    return _render(
        request,
        "admin/settings.html",
        {
            "page_title": "Settings",
            "admin": admin_or_response,
            "settings_items": settings_items,
        },
    )
