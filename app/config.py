from __future__ import annotations

import secrets
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path


def _default_bootstrap_admin_password() -> str:
    return secrets.token_urlsafe(24)


@dataclass(slots=True)
class Settings:
    storage_root: Path
    database_path: Path
    bootstrap_admin_username: str = "admin"
    bootstrap_admin_password: str = field(default_factory=_default_bootstrap_admin_password)
    session_cookie_name: str = "rapid_inbox_session"
    host: str = "127.0.0.1"
    port: int = 8000
    smtp_host: str = "127.0.0.1"
    smtp_port: int = 2525
    max_message_size_bytes: int = 52_428_800
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


def default_settings(base_dir: Path) -> Settings:
    storage_root = base_dir / "storage"
    return Settings(storage_root=storage_root, database_path=storage_root / "app.db")
