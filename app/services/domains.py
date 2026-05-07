from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from app.db.connection import connect_database
from app.db.writer import DatabaseWriter
from app.ingest.storage import utc_now
from app.smtp.matcher import DomainMatch, DomainMatcher, DomainRule, normalize_domain


class DomainService:
    def __init__(self, database_path: Path, writer: DatabaseWriter) -> None:
        self._database_path = database_path
        self._writer = writer
        self._matcher = DomainMatcher([])

    def reload(self) -> None:
        with connect_database(self._database_path) as connection:
            rows = connection.execute(
                """
                SELECT
                    id,
                    root_domain_ascii,
                    accept_exact,
                    accept_subdomains,
                    plus_addressing_mode,
                    local_part_case_sensitive
                FROM domains
                WHERE is_active = 1
                """
            ).fetchall()
        self._matcher = DomainMatcher(
            [
                DomainRule(
                    domain_id=row["id"],
                    root_domain_ascii=row["root_domain_ascii"],
                    accept_exact=bool(row["accept_exact"]),
                    accept_subdomains=bool(row["accept_subdomains"]),
                    plus_addressing_mode=row["plus_addressing_mode"],
                    local_part_case_sensitive=bool(row["local_part_case_sensitive"]),
                )
                for row in rows
            ]
        )

    async def create_domain(
        self,
        root_domain: str,
        *,
        accept_exact: bool = True,
        accept_subdomains: bool = True,
        public_web_enabled: bool = True,
        public_api_enabled: bool = True,
        plus_addressing_mode: str = "keep",
        local_part_case_sensitive: bool = False,
        is_active: bool = True,
        max_message_size_bytes: int = 52_428_800,
    ) -> dict[str, Any]:
        now = utc_now()
        root_domain_ascii = self._coerce_root_domain(root_domain)
        accept_exact = self._coerce_bool("accept_exact", accept_exact)
        accept_subdomains = self._coerce_bool("accept_subdomains", accept_subdomains)
        public_web_enabled = self._coerce_bool("public_web_enabled", public_web_enabled)
        public_api_enabled = self._coerce_bool("public_api_enabled", public_api_enabled)
        local_part_case_sensitive = self._coerce_bool("local_part_case_sensitive", local_part_case_sensitive)
        is_active = self._coerce_bool("is_active", is_active)
        max_message_size_bytes = self._coerce_positive_int("max_message_size_bytes", max_message_size_bytes)
        plus_addressing_mode = self._coerce_plus_addressing_mode(plus_addressing_mode)

        def operation(connection: sqlite3.Connection) -> int:
            cursor = connection.execute(
                """
                INSERT INTO domains (
                    root_domain_ascii,
                    root_domain_unicode,
                    accept_exact,
                    accept_subdomains,
                    public_web_enabled,
                    public_api_enabled,
                    is_active,
                    plus_addressing_mode,
                    local_part_case_sensitive,
                    max_message_size_bytes,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    root_domain_ascii,
                    root_domain,
                    int(accept_exact),
                    int(accept_subdomains),
                    int(public_web_enabled),
                    int(public_api_enabled),
                    int(is_active),
                    plus_addressing_mode,
                    int(local_part_case_sensitive),
                    max_message_size_bytes,
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

        domain_id = await self._writer.execute(operation)
        self.reload()
        return self.get_domain(domain_id)

    async def update_domain(self, domain_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("invalid domain payload")

        assignments: list[str] = []
        values: list[Any] = []
        if "root_domain" in payload:
            root_domain = str(payload["root_domain"]).strip()
            root_domain_ascii = self._coerce_root_domain(root_domain)
            assignments.extend(["root_domain_ascii = ?", "root_domain_unicode = ?"])
            values.extend([root_domain_ascii, root_domain])
        for field_name in (
            "accept_exact",
            "accept_subdomains",
            "public_web_enabled",
            "public_api_enabled",
            "local_part_case_sensitive",
            "is_active",
            "is_hidden",
        ):
            if field_name in payload:
                assignments.append(f"{field_name} = ?")
                values.append(int(self._coerce_bool(field_name, payload[field_name])))
        if "plus_addressing_mode" in payload:
            assignments.append("plus_addressing_mode = ?")
            values.append(self._coerce_plus_addressing_mode(payload["plus_addressing_mode"]))
        if "max_message_size_bytes" in payload:
            assignments.append("max_message_size_bytes = ?")
            values.append(self._coerce_positive_int("max_message_size_bytes", payload["max_message_size_bytes"]))
        if "retention_days" in payload:
            assignments.append("retention_days = ?")
            values.append(self._coerce_nullable_positive_int("retention_days", payload["retention_days"]))
        if "notes" in payload:
            assignments.append("notes = ?")
            values.append(self._nullable_text(payload["notes"]))

        if not assignments:
            return self.get_domain(domain_id)

        updated_at = utc_now()
        assignments.append("updated_at = ?")
        values.append(updated_at)

        def operation(connection: sqlite3.Connection) -> None:
            row = connection.execute("SELECT id FROM domains WHERE id = ?", (domain_id,)).fetchone()
            if row is None:
                raise LookupError("domain not found")
            connection.execute(
                f"UPDATE domains SET {', '.join(assignments)} WHERE id = ?",
                (*values, domain_id),
            )

        await self._writer.execute(operation)
        self.reload()
        return self.get_domain(domain_id)

    async def delete_domain(self, domain_id: int) -> dict[str, Any]:
        existing = self.get_domain(domain_id)

        def operation(connection: sqlite3.Connection) -> None:
            cursor = connection.execute("DELETE FROM domains WHERE id = ?", (domain_id,))
            if cursor.rowcount != 1:
                raise LookupError("domain not found")

        await self._writer.execute(operation)
        self.reload()
        return existing

    def list_domains(self) -> list[dict[str, Any]]:
        with connect_database(self._database_path) as connection:
            rows = connection.execute(
                """
                SELECT
                    id,
                    root_domain_ascii,
                    accept_exact,
                    accept_subdomains,
                    public_web_enabled,
                    public_api_enabled,
                    is_active,
                    created_at,
                    updated_at
                FROM domains
                ORDER BY root_domain_ascii ASC
                """
            ).fetchall()
        return [self._normalize_domain_row(row) for row in rows]

    def get_domain(self, domain_id: int) -> dict[str, Any]:
        with connect_database(self._database_path) as connection:
            row = connection.execute(
                """
                SELECT
                    id,
                    root_domain_ascii,
                    root_domain_unicode,
                    accept_exact,
                    accept_subdomains,
                    public_web_enabled,
                    public_api_enabled,
                    is_active,
                    is_hidden,
                    local_part_case_sensitive,
                    plus_addressing_mode,
                    max_message_size_bytes,
                    retention_days,
                    dns_status,
                    dns_last_checked_at,
                    dns_details_json,
                    notes,
                    created_at,
                    updated_at
                FROM domains
                WHERE id = ?
                """,
                (domain_id,),
            ).fetchone()
        if row is None:
            raise LookupError("domain not found")
        payload = self._normalize_domain_row(row)
        payload["dns_recommendations"] = self.dns_recommendations(payload["root_domain_ascii"])
        return payload

    def dns_recommendations(self, root_domain: str) -> list[dict[str, str]]:
        return [
            {
                "name": root_domain,
                "type": "MX",
                "value": f"10 {root_domain}",
                "purpose": "根域邮箱收件路由",
            },
            {
                "name": f"*.{root_domain}",
                "type": "MX",
                "value": f"10 {root_domain}",
                "purpose": "子域邮箱收件路由",
            },
        ]

    def match_address(self, address: str) -> DomainMatch | None:
        return self._matcher.match_address(address)

    def _coerce_root_domain(self, value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("invalid root_domain")
        try:
            return normalize_domain(value)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("invalid root_domain") from exc

    def _coerce_bool(self, field_name: str, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and not isinstance(value, bool) and value in (0, 1):
            return bool(value)
        raise ValueError(f"invalid {field_name}")

    def _coerce_plus_addressing_mode(self, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("invalid plus_addressing_mode")
        if value not in {"keep", "strip"}:
            raise ValueError("invalid plus_addressing_mode")
        return value

    def _coerce_positive_int(self, field_name: str, value: Any) -> int:
        if isinstance(value, bool):
            raise ValueError(f"invalid {field_name}")
        if isinstance(value, float) and not value.is_integer():
            raise ValueError(f"invalid {field_name}")
        try:
            normalized = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid {field_name}") from exc
        if normalized < 1:
            raise ValueError(f"invalid {field_name}")
        if isinstance(value, float) and normalized != value:
            raise ValueError(f"invalid {field_name}")
        return normalized

    def _coerce_nullable_positive_int(self, field_name: str, value: Any) -> int | None:
        if value is None or value == "":
            return None
        return self._coerce_positive_int(field_name, value)

    def _nullable_text(self, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _normalize_domain_row(self, row: sqlite3.Row) -> dict[str, Any]:
        payload = dict(row)
        for key in (
            "accept_exact",
            "accept_subdomains",
            "public_web_enabled",
            "public_api_enabled",
            "is_active",
            "is_hidden",
            "local_part_case_sensitive",
        ):
            if key in payload:
                payload[key] = bool(payload[key])
        return payload
