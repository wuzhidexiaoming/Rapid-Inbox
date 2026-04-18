from __future__ import annotations

import sqlite3
from typing import Any

from app.db.connection import connect_database
from app.ingest.storage import utc_now


class SettingsService:
    DEFAULTS: dict[str, Any] = {
        "max_recipients_per_message": 20,
    }
    SUPPORTED_SETTINGS = {
        "max_message_size_bytes",
        "max_recipients_per_message",
    }
    INTEGER_SETTINGS = SUPPORTED_SETTINGS

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    def get_settings(self) -> dict[str, Any]:
        settings = self._base_settings()
        settings.update(self._load_persisted_settings())
        return settings

    async def load_persisted_settings(self) -> dict[str, Any]:
        settings = self._load_persisted_settings()
        self._runtime.apply_live_settings(settings)
        return settings

    async def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("invalid settings payload")

        self._validate_supported_keys(payload)
        normalized = self._normalize_payload(payload)
        if not normalized:
            return self.get_settings()

        now = utc_now()

        def operation(connection: sqlite3.Connection) -> None:
            for key, value in normalized.items():
                connection.execute(
                    """
                    INSERT INTO system_settings (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    (key, value, now),
                )

        await self._runtime.writer.execute(operation)
        self._runtime.apply_live_settings(
            {key: self._deserialize_value(key, value) for key, value in normalized.items()}
        )
        return self.get_settings()

    def _base_settings(self) -> dict[str, Any]:
        return {
            "max_message_size_bytes": int(self._runtime.settings.max_message_size_bytes),
            **self.DEFAULTS,
        }

    def _load_persisted_settings(self) -> dict[str, Any]:
        with connect_database(self._runtime.settings.database_path) as connection:
            rows = connection.execute(
                """
                SELECT key, value
                FROM system_settings
                ORDER BY key ASC
                """
            ).fetchall()

        settings: dict[str, Any] = {}
        for row in rows:
            key = str(row["key"])
            if key not in self.SUPPORTED_SETTINGS:
                continue
            settings[key] = self._deserialize_value(key, row["value"])
        return settings

    def _normalize_payload(self, payload: dict[str, Any]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for key, value in payload.items():
            key_text = str(key)
            if key_text in self.INTEGER_SETTINGS:
                normalized[key_text] = str(self._coerce_positive_int(key_text, value))
            else:
                normalized[key_text] = self._coerce_text_value(value)
        return normalized

    def _validate_supported_keys(self, payload: dict[str, Any]) -> None:
        unsupported = sorted({str(key) for key in payload if str(key) not in self.SUPPORTED_SETTINGS})
        if unsupported:
            raise ValueError(f"unsupported settings: {', '.join(unsupported)}")

    def _coerce_positive_int(self, key: str, value: Any) -> int:
        if isinstance(value, bool):
            raise ValueError(f"invalid {key}")
        try:
            normalized = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid {key}") from exc
        if normalized < 1:
            raise ValueError(f"invalid {key}")
        return normalized

    def _coerce_text_value(self, value: Any) -> str:
        if value is None:
            raise ValueError("invalid settings value")
        if isinstance(value, bool):
            return "1" if value else "0"
        return str(value)

    def _deserialize_value(self, key: str, value: Any) -> Any:
        if key in self.INTEGER_SETTINGS:
            try:
                return self._coerce_positive_int(key, value)
            except ValueError:
                if key in self.DEFAULTS:
                    return self.DEFAULTS[key]
                return int(self._runtime.settings.max_message_size_bytes)
        return value


__all__ = ["SettingsService"]
