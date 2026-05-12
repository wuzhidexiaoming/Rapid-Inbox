from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import Settings, default_settings
from app.http.admin_views import router as admin_views_router
from app.http.admin_api import router as admin_api_router
from app.http.public_api import router as public_api_router
from app.http.template_helpers import register_template_helpers
from app.http.public_views import router as public_views_router
from app.runtime import RapidInboxRuntime
from app.smtp.server import SMTPServer


UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _default_port_for_scheme(scheme: str) -> int | None:
    return {"http": 80, "https": 443}.get(scheme.lower())


def _parse_host_port(host_header: str | None) -> tuple[str, int | None] | None:
    if not host_header:
        return None
    parsed = urlparse(f"//{host_header.strip()}")
    if not parsed.hostname:
        return None
    return parsed.hostname.lower(), parsed.port


def _request_scheme(request: Request) -> str:
    forwarded_proto = request.headers.get("x-forwarded-proto")
    if forwarded_proto:
        return forwarded_proto.split(",", 1)[0].strip().lower()
    return request.url.scheme.lower()


def _origin_matches_request_host(request: Request, value: str | None) -> bool:
    if not value or value == "null":
        return False
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.hostname:
        return False
    request_scheme = _request_scheme(request)
    origin_scheme = parsed.scheme.lower()
    if origin_scheme != request_scheme:
        return False
    request_host = _parse_host_port(request.headers.get("host"))
    if request_host is None:
        return False
    origin_host = parsed.hostname.lower()
    origin_port = parsed.port or _default_port_for_scheme(origin_scheme)
    expected_host, expected_port = request_host
    expected_port = expected_port or _default_port_for_scheme(request_scheme)
    if origin_host != expected_host:
        return False
    return origin_port == expected_port


def _is_same_origin_admin_request(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin:
        return _origin_matches_request_host(request, origin)
    referer = request.headers.get("referer")
    if referer:
        return _origin_matches_request_host(request, referer)
    return False


def _apply_security_headers(request: Request, response) -> None:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if _request_scheme(request) == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")


def _admin_form_csrf_required(request: Request) -> bool:
    return request.method.upper() in UNSAFE_METHODS and request.url.path.startswith("/admin")


def create_app(*, settings: Settings | None = None, embed_smtp: bool = False) -> FastAPI:
    resolved_settings = settings or default_settings(Path.cwd())
    runtime = RapidInboxRuntime(resolved_settings)
    app_dir = Path(__file__).resolve().parent
    templates = Jinja2Templates(directory=str(app_dir / "templates"))
    register_template_helpers(templates)

    async def _shutdown(runtime: RapidInboxRuntime, smtp_server: SMTPServer | None) -> None:
        if smtp_server is not None:
            await asyncio.to_thread(smtp_server.stop)
        await runtime.stop()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = resolved_settings
        app.state.runtime = runtime
        app.state.templates = templates
        smtp_server: SMTPServer | None = None
        try:
            await runtime.start()
            if embed_smtp:
                smtp_server = SMTPServer(runtime)
                smtp_server.start()
            yield
        finally:
            shutdown_task = asyncio.create_task(_shutdown(runtime, smtp_server))
            try:
                await asyncio.shield(shutdown_task)
            except asyncio.CancelledError:
                shutdown_task.cancel()
                with suppress(asyncio.CancelledError):
                    await shutdown_task

    app = FastAPI(title="Rapid Inbox", lifespan=lifespan)

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        if _admin_form_csrf_required(request) and not _is_same_origin_admin_request(request):
            response = JSONResponse({"detail": "invalid origin"}, status_code=403)
            _apply_security_headers(request, response)
            return response
        response = await call_next(request)
        _apply_security_headers(request, response)
        return response

    app.mount("/static", StaticFiles(directory=str(app_dir / "static")), name="static")
    app.include_router(public_views_router)
    app.include_router(public_api_router)
    app.include_router(admin_views_router)
    app.include_router(admin_api_router)
    return app


app = create_app()
