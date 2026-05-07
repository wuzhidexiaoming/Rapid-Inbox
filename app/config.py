from __future__ import annotations

import ast
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class Settings:
    storage_root: Path
    database_path: Path
    bootstrap_admin_username: str = "admin"
    bootstrap_admin_password: str = "change-me-now"
    session_cookie_name: str = "rapid_inbox_session"
    host: str = "127.0.0.1"
    port: int = 8000
    smtp_host: str = "127.0.0.1"
    smtp_port: int = 25
    max_message_size_bytes: int = 52_428_800
    max_recipients_per_message: int = 20
    smtp_idle_timeout_seconds: int = 30
    smtp_max_concurrent_connections: int = 0
    smtp_connection_rate_limit_count: int = 0
    smtp_connection_rate_limit_window_seconds: int = 60
    smtp_close_after_data: bool = True
    parse_worker_count: int = 4
    fsync_storage_writes: bool = False
    disk_warning_threshold_percent: int = 85
    admin_token: str = "dev-admin-token"
    public_api_key: str = "public-demo-key"

    @property
    def raw_dir(self) -> Path:
        return self.storage_root / "raw"

    @property
    def text_dir(self) -> Path:
        return self.storage_root / "text"

    @property
    def html_dir(self) -> Path:
        return self.storage_root / "html"

    @property
    def attachments_dir(self) -> Path:
        return self.storage_root / "attachments"

    @property
    def manifests_dir(self) -> Path:
        return self.storage_root / "manifests"

    @property
    def tmp_dir(self) -> Path:
        return self.storage_root / "tmp"

    def ensure_directories(self) -> None:
        for path in (
            self.storage_root,
            self.raw_dir,
            self.text_dir,
            self.html_dir,
            self.attachments_dir,
            self.manifests_dir,
            self.tmp_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


def _load_dotenv(dotenv_path: Path) -> dict[str, str]:
    if not dotenv_path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if value[:1] in {"'", '"'} and value[-1:] == value[:1]:
            value = ast.literal_eval(value)
        values[key] = value
    return values


def _resolve_path(value: str | None, *, default: Path, base_dir: Path) -> Path:
    if value is None or not value.strip():
        return default
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path


def _coerce_str(raw: dict[str, str], key: str, default: str) -> str:
    value = raw.get(key)
    return default if value is None else value


def _coerce_int(raw: dict[str, str], key: str, default: int) -> int:
    value = raw.get(key)
    if value is None or not value.strip():
        return default
    return int(value)


def _coerce_bool(raw: dict[str, str], key: str, default: bool) -> bool:
    value = raw.get(key)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def default_settings(base_dir: Path) -> Settings:
    dotenv_values = _load_dotenv(base_dir / ".env")
    merged = {**dotenv_values, **os.environ}

    storage_root = _resolve_path(
        merged.get("STORAGE_ROOT"),
        default=base_dir / "storage",
        base_dir=base_dir,
    )
    database_path = _resolve_path(
        merged.get("DATABASE_PATH"),
        default=storage_root / "app.db",
        base_dir=base_dir,
    )

    return Settings(
        storage_root=storage_root,
        database_path=database_path,
        bootstrap_admin_username=_coerce_str(merged, "BOOTSTRAP_ADMIN_USERNAME", "admin"),
        bootstrap_admin_password=_coerce_str(merged, "BOOTSTRAP_ADMIN_PASSWORD", "change-me-now"),
        session_cookie_name=_coerce_str(merged, "SESSION_COOKIE_NAME", "rapid_inbox_session"),
        host=_coerce_str(merged, "HOST", "127.0.0.1"),
        port=_coerce_int(merged, "PORT", 8000),
        smtp_host=_coerce_str(merged, "SMTP_HOST", "127.0.0.1"),
        smtp_port=_coerce_int(merged, "SMTP_PORT", 25),
        max_message_size_bytes=_coerce_int(merged, "MAX_MESSAGE_SIZE_BYTES", 52_428_800),
        max_recipients_per_message=_coerce_int(merged, "MAX_RECIPIENTS_PER_MESSAGE", 20),
        smtp_idle_timeout_seconds=_coerce_int(merged, "SMTP_IDLE_TIMEOUT_SECONDS", 30),
        smtp_max_concurrent_connections=_coerce_int(merged, "SMTP_MAX_CONCURRENT_CONNECTIONS", 0),
        smtp_connection_rate_limit_count=_coerce_int(merged, "SMTP_CONNECTION_RATE_LIMIT_COUNT", 0),
        smtp_connection_rate_limit_window_seconds=_coerce_int(
            merged,
            "SMTP_CONNECTION_RATE_LIMIT_WINDOW_SECONDS",
            60,
        ),
        smtp_close_after_data=_coerce_bool(merged, "SMTP_CLOSE_AFTER_DATA", True),
        parse_worker_count=_coerce_int(merged, "PARSE_WORKER_COUNT", 4),
        fsync_storage_writes=_coerce_bool(merged, "FSYNC_STORAGE_WRITES", False),
        disk_warning_threshold_percent=_coerce_int(merged, "DISK_WARNING_THRESHOLD_PERCENT", 85),
        admin_token=_coerce_str(merged, "ADMIN_TOKEN", "dev-admin-token"),
        public_api_key=_coerce_str(merged, "PUBLIC_API_KEY", "public-demo-key"),
    )
