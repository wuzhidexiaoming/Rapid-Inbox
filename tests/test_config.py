import os
from pathlib import Path

from app.config import Settings, default_settings


def test_settings_derive_storage_paths(tmp_path: Path) -> None:
    settings = Settings(storage_root=tmp_path, database_path=tmp_path / "app.db")

    assert settings.raw_dir == tmp_path / "raw"
    assert settings.text_dir == tmp_path / "text"
    assert settings.html_dir == tmp_path / "html"
    assert settings.attachments_dir == tmp_path / "attachments"


def test_settings_include_bootstrap_and_operational_defaults(tmp_path: Path) -> None:
    settings = Settings(storage_root=tmp_path / "storage", database_path=tmp_path / "storage" / "app.db")

    assert settings.bootstrap_admin_username == "admin"
    assert settings.bootstrap_admin_password == "change-me-now"
    assert settings.smtp_port == 25
    assert settings.max_recipients_per_message == 20
    assert settings.session_cookie_name == "rapid_inbox_session"


def test_default_settings_loads_values_from_dotenv(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "STORAGE_ROOT=./custom-storage",
                "DATABASE_PATH=./custom-storage/custom.db",
                "BOOTSTRAP_ADMIN_USERNAME=rooter",
                "BOOTSTRAP_ADMIN_PASSWORD=super-secret",
                "SESSION_COOKIE_NAME=ri_cookie",
                "HOST=0.0.0.0",
                "PORT=18000",
                "SMTP_HOST=0.0.0.0",
                "SMTP_PORT=2525",
                "MAX_MESSAGE_SIZE_BYTES=1024",
                "MAX_RECIPIENTS_PER_MESSAGE=9",
                "ADMIN_TOKEN=admin-token-1",
                "PUBLIC_API_KEY=public-token-1",
            ]
        ),
        encoding="utf-8",
    )

    settings = default_settings(tmp_path)

    assert settings.storage_root == tmp_path / "custom-storage"
    assert settings.database_path == tmp_path / "custom-storage" / "custom.db"
    assert settings.bootstrap_admin_username == "rooter"
    assert settings.bootstrap_admin_password == "super-secret"
    assert settings.session_cookie_name == "ri_cookie"
    assert settings.host == "0.0.0.0"
    assert settings.port == 18000
    assert settings.smtp_host == "0.0.0.0"
    assert settings.smtp_port == 2525
    assert settings.max_message_size_bytes == 1024
    assert settings.max_recipients_per_message == 9
    assert settings.admin_token == "admin-token-1"
    assert settings.public_api_key == "public-token-1"


def test_environment_variables_override_dotenv(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "HOST=127.0.0.1",
                "SMTP_HOST=127.0.0.1",
                "BOOTSTRAP_ADMIN_PASSWORD=from-dotenv",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.setenv("SMTP_HOST", "0.0.0.0")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "from-env")

    settings = default_settings(tmp_path)

    assert settings.host == "0.0.0.0"
    assert settings.smtp_host == "0.0.0.0"
    assert settings.bootstrap_admin_password == "from-env"


def test_default_settings_resolves_relative_paths_from_base_dir(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "STORAGE_ROOT=./runtime/storage",
                "DATABASE_PATH=./runtime/data/app.db",
            ]
        ),
        encoding="utf-8",
    )

    settings = default_settings(tmp_path)

    assert settings.storage_root == tmp_path / "runtime" / "storage"
    assert settings.database_path == tmp_path / "runtime" / "data" / "app.db"
