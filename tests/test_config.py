from pathlib import Path

from app.config import Settings


def test_settings_derive_storage_paths(tmp_path: Path) -> None:
    settings = Settings(storage_root=tmp_path, database_path=tmp_path / "app.db")

    assert settings.raw_dir == tmp_path / "raw"
    assert settings.text_dir == tmp_path / "text"
    assert settings.html_dir == tmp_path / "html"
    assert settings.attachments_dir == tmp_path / "attachments"


def test_settings_include_bootstrap_and_operational_defaults(tmp_path: Path) -> None:
    settings = Settings(storage_root=tmp_path / "storage", database_path=tmp_path / "storage" / "app.db")

    assert settings.bootstrap_admin_username == "admin"
    assert settings.bootstrap_admin_password
    assert settings.max_recipients_per_message == 20
    assert settings.session_cookie_name == "rapid_inbox_session"
