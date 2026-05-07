from __future__ import annotations

import ipaddress
import json
import hashlib
import hmac
import secrets
import sqlite3
import threading
import uuid
from contextvars import ContextVar
from collections import deque
from pathlib import Path
from time import monotonic
from typing import Any, Sequence

from fastapi import HTTPException

from app.db.connection import connect_database
from app.db.writer import DatabaseWriter
from app.ingest.storage import utc_now

from .permissions import PermissionContext


VALID_API_KEY_KINDS = {"admin", "public", "service"}
VALID_API_KEY_STATUSES = {"active", "revoked", "expired", "disabled"}
_UNSET = object()
_ACTIVE_PERMISSION_CONTEXT: ContextVar[PermissionContext | None] = ContextVar(
    "active_permission_context",
    default=None,
)


def make_api_key(kind: str) -> tuple[str, str, str]:
    prefix = secrets.token_hex(4)
    secret = secrets.token_urlsafe(24)
    plain_text = f"ri_{kind}_{prefix}_{secret}"
    secret_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    return prefix, plain_text, secret_hash


def get_active_permission_context() -> PermissionContext | None:
    return _ACTIVE_PERMISSION_CONTEXT.get()


def set_active_permission_context(context: PermissionContext | None) -> None:
    _ACTIVE_PERMISSION_CONTEXT.set(context)


class PublicAPIKeyProxy(str):
    def __new__(cls, legacy_value: str, service: "ApiKeyService") -> "PublicAPIKeyProxy":
        proxy = str.__new__(cls, legacy_value)
        proxy._service = service
        return proxy

    def __ne__(self, other: object) -> bool:
        return self._service.compare_public_api_key(other)


class ApiKeyService:
    def __init__(self, database_path: Path, writer: DatabaseWriter) -> None:
        self.database_path = database_path
        self.writer = writer
        self._legacy_public_api_key: str | None = None
        self._legacy_public_context: PermissionContext | None = None
        self._usage_lock = threading.Lock()
        self._usage_windows: dict[int, deque[float]] = {}

    def configure_legacy_public_api_key(self, legacy_token: str) -> PublicAPIKeyProxy:
        self._legacy_public_api_key = legacy_token
        self._legacy_public_context = PermissionContext(
            scopes=("public.read",),
            domain_ids=(),
            mailbox_patterns=(),
            public_id="legacy-public-token",
            name="legacy-public-token",
            kind="public",
            legacy_credential=True,
        )
        return PublicAPIKeyProxy(legacy_token, self)

    async def create_key(
        self,
        *,
        name: str,
        kind: str,
        scopes: Sequence[str],
        domain_ids: Sequence[int],
        mailbox_patterns: Sequence[str],
        description: str | None = None,
        owner_admin_id: int | None = None,
        created_by_admin_id: int | None = None,
        status: str = "active",
        allow_header: bool = True,
        allow_query: bool = False,
        rate_limit_per_min: int = 3600,
        allowed_ip_cidrs: Sequence[str] | None = None,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        if kind not in VALID_API_KEY_KINDS:
            raise ValueError("invalid api key kind")
        if status not in VALID_API_KEY_STATUSES:
            raise ValueError("invalid api key status")

        scope_values = self._unique_text_values(scopes)
        domain_values = self._unique_int_values(domain_ids)
        mailbox_values = self._unique_text_values(mailbox_patterns)
        allowed_ip_values = self._normalize_ip_cidrs(allowed_ip_cidrs or ())
        allowed_ip_cidrs_json = json.dumps(list(allowed_ip_values), ensure_ascii=False) if allowed_ip_values else None
        key_prefix, plain_text, secret_hash = make_api_key(kind)
        public_id = f"ak_{uuid.uuid4().hex}"
        created_at = utc_now()

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            cursor = connection.execute(
                """
                INSERT INTO api_keys (
                    public_id,
                    name,
                    description,
                    kind,
                    key_prefix,
                    secret_hash,
                    owner_admin_id,
                    status,
                    allow_header,
                    allow_query,
                    rate_limit_per_min,
                    allowed_ip_cidrs,
                    expires_at,
                    created_by_admin_id,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    public_id,
                    name,
                    description,
                    kind,
                    key_prefix,
                    secret_hash,
                    owner_admin_id,
                    status,
                    int(allow_header),
                    int(allow_query),
                    rate_limit_per_min,
                    allowed_ip_cidrs_json,
                    expires_at,
                    created_by_admin_id,
                    created_at,
                ),
            )
            api_key_id = int(cursor.lastrowid)

            for scope in scope_values:
                connection.execute(
                    "INSERT INTO api_key_scopes (api_key_id, scope) VALUES (?, ?)",
                    (api_key_id, scope),
                )
            for domain_id in domain_values:
                connection.execute(
                    "INSERT INTO api_key_domain_grants (api_key_id, domain_id) VALUES (?, ?)",
                    (api_key_id, domain_id),
                )
            for mailbox_pattern in mailbox_values:
                connection.execute(
                    "INSERT INTO api_key_mailbox_grants (api_key_id, mailbox_pattern) VALUES (?, ?)",
                    (api_key_id, mailbox_pattern),
                )

            return {
                "id": api_key_id,
                "public_id": public_id,
                "name": name,
                "description": description,
                "kind": kind,
                "status": status,
                "key_prefix": key_prefix,
                "plain_text": plain_text,
                "scopes": list(scope_values),
                "domain_ids": list(domain_values),
                "mailbox_patterns": list(mailbox_values),
                "owner_admin_id": owner_admin_id,
                "created_by_admin_id": created_by_admin_id,
                "allow_header": allow_header,
                "allow_query": allow_query,
                "rate_limit_per_min": rate_limit_per_min,
                "allowed_ip_cidrs": list(allowed_ip_values),
                "expires_at": expires_at,
                "created_at": created_at,
            }

        return await self.writer.execute(operation)

    def get_key(self, api_key_id: int) -> dict[str, Any]:
        with connect_database(self.database_path) as connection:
            api_key = self._load_key(connection, api_key_id)
        if api_key is None:
            raise LookupError("api key not found")
        return api_key

    async def update_key(
        self,
        api_key_id: int,
        *,
        name: object = _UNSET,
        description: object = _UNSET,
        kind: object = _UNSET,
        status: object = _UNSET,
        allow_header: object = _UNSET,
        allow_query: object = _UNSET,
        rate_limit_per_min: object = _UNSET,
        allowed_ip_cidrs: object = _UNSET,
        expires_at: object = _UNSET,
        scopes: object = _UNSET,
        domain_ids: object = _UNSET,
        mailbox_patterns: object = _UNSET,
    ) -> dict[str, Any]:
        field_updates: dict[str, Any] = {}

        if name is not _UNSET:
            normalized_name = str(name).strip()
            if not normalized_name:
                raise ValueError("name is required")
            field_updates["name"] = normalized_name
        if description is not _UNSET:
            field_updates["description"] = self._nullable_text(description)
        expected_kind: str | None = None
        if kind is not _UNSET:
            normalized_kind = str(kind).strip()
            if normalized_kind not in VALID_API_KEY_KINDS:
                raise ValueError("invalid api key kind")
            expected_kind = normalized_kind
        if status is not _UNSET:
            normalized_status = str(status).strip()
            if normalized_status not in VALID_API_KEY_STATUSES:
                raise ValueError("invalid api key status")
            field_updates["status"] = normalized_status
        if allow_header is not _UNSET:
            field_updates["allow_header"] = int(self._coerce_bool("allow_header", allow_header))
        if allow_query is not _UNSET:
            field_updates["allow_query"] = int(self._coerce_bool("allow_query", allow_query))
        if rate_limit_per_min is not _UNSET:
            field_updates["rate_limit_per_min"] = self._coerce_non_negative_int(
                "rate_limit_per_min",
                rate_limit_per_min,
            )
        if allowed_ip_cidrs is not _UNSET:
            allowed_ip_values = self._normalize_ip_cidrs(allowed_ip_cidrs or ())
            field_updates["allowed_ip_cidrs"] = (
                json.dumps(list(allowed_ip_values), ensure_ascii=False) if allowed_ip_values else None
            )
        if expires_at is not _UNSET:
            field_updates["expires_at"] = self._nullable_text(expires_at)

        scope_values = self._unique_text_values(scopes) if scopes is not _UNSET else None
        domain_values = self._unique_int_values(domain_ids) if domain_ids is not _UNSET else None
        mailbox_values = self._unique_text_values(mailbox_patterns) if mailbox_patterns is not _UNSET else None
        now = utc_now()

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute(
                """
                SELECT id, kind
                FROM api_keys
                WHERE id = ?
                """,
                (api_key_id,),
            ).fetchone()
            if row is None:
                raise LookupError("api key not found")
            if expected_kind is not None and row["kind"] != expected_kind:
                raise ValueError("api key kind cannot be changed")

            if field_updates:
                assignments = [f"{column} = ?" for column in field_updates]
                params = list(field_updates.values())
                if field_updates.get("status") == "revoked":
                    assignments.append("revoked_at = COALESCE(revoked_at, ?)")
                    params.append(now)
                elif "status" in field_updates:
                    assignments.append("revoked_at = NULL")
                params.append(api_key_id)
                connection.execute(
                    f"""
                    UPDATE api_keys
                    SET {', '.join(assignments)}
                    WHERE id = ?
                    """,
                    params,
                )

            if scope_values is not None:
                connection.execute("DELETE FROM api_key_scopes WHERE api_key_id = ?", (api_key_id,))
                for scope in scope_values:
                    connection.execute(
                        "INSERT INTO api_key_scopes (api_key_id, scope) VALUES (?, ?)",
                        (api_key_id, scope),
                    )
            if domain_values is not None:
                connection.execute("DELETE FROM api_key_domain_grants WHERE api_key_id = ?", (api_key_id,))
                for domain_id in domain_values:
                    connection.execute(
                        "INSERT INTO api_key_domain_grants (api_key_id, domain_id) VALUES (?, ?)",
                        (api_key_id, domain_id),
                    )
            if mailbox_values is not None:
                connection.execute("DELETE FROM api_key_mailbox_grants WHERE api_key_id = ?", (api_key_id,))
                for mailbox_pattern in mailbox_values:
                    connection.execute(
                        "INSERT INTO api_key_mailbox_grants (api_key_id, mailbox_pattern) VALUES (?, ?)",
                        (api_key_id, mailbox_pattern),
                    )

            api_key = self._load_key(connection, api_key_id)
            if api_key is None:
                raise LookupError("api key not found")
            return api_key

        updated = await self.writer.execute(operation)
        if (
            field_updates.get("status") != "active"
            or "rate_limit_per_min" in field_updates
            or "allowed_ip_cidrs" in field_updates
        ):
            with self._usage_lock:
                self._usage_windows.pop(api_key_id, None)
        return updated

    async def revoke_key(self, api_key_id: int) -> dict[str, Any]:
        revoked_at = utc_now()

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute(
                """
                SELECT id, revoked_at
                FROM api_keys
                WHERE id = ?
                """,
                (api_key_id,),
            ).fetchone()
            if row is None:
                raise LookupError("api key not found")

            connection.execute(
                """
                UPDATE api_keys
                SET status = 'revoked',
                    revoked_at = COALESCE(revoked_at, ?)
                WHERE id = ?
                """,
                (revoked_at, api_key_id),
            )
            return {
                "id": int(row["id"]),
                "status": "revoked",
                "revoked_at": str(row["revoked_at"] or revoked_at),
            }

        revoked = await self.writer.execute(operation)
        with self._usage_lock:
            self._usage_windows.pop(api_key_id, None)
        return revoked

    def authenticate_plain_text(self, plain_text: str, *, request_ip: str | None = None) -> PermissionContext:
        return self._authenticate_plain_text(plain_text, transport="header", request_ip=request_ip)

    def authenticate_query(self, plain_text: str, *, request_ip: str | None = None) -> PermissionContext:
        return self._authenticate_plain_text(plain_text, transport="query", request_ip=request_ip)

    async def record_usage(self, context: PermissionContext, *, ip: str | None = None) -> None:
        if context.api_key_id is None:
            return

        with connect_database(self.database_path) as connection:
            row = connection.execute(
                """
                SELECT rate_limit_per_min, allowed_ip_cidrs
                FROM api_keys
                WHERE id = ?
                """,
                (context.api_key_id,),
            ).fetchone()

        if row is None:
            raise HTTPException(status_code=401, detail="invalid api key")

        if not self._request_ip_allowed(ip, row["allowed_ip_cidrs"]):
            raise HTTPException(status_code=403, detail="api key ip not allowed")

        rate_limit_per_min = int(row["rate_limit_per_min"])
        if rate_limit_per_min > 0:
            now_monotonic = monotonic()
            cutoff = now_monotonic - 60
            with self._usage_lock:
                window = self._usage_windows.setdefault(context.api_key_id, deque())
                while window and window[0] <= cutoff:
                    window.popleft()
                if len(window) >= rate_limit_per_min:
                    raise HTTPException(status_code=429, detail="api key rate limit exceeded")
                window.append(now_monotonic)

        now = utc_now()
        await self.writer.execute(
            lambda connection: connection.execute(
                """
                UPDATE api_keys
                SET last_used_at = ?,
                    last_used_ip = COALESCE(?, last_used_ip)
                WHERE id = ?
                """,
                (now, ip, context.api_key_id),
            )
        )

    def _authenticate_plain_text(self, plain_text: str, *, transport: str, request_ip: str | None = None) -> PermissionContext:
        kind, key_prefix, secret = self._parse_plain_text(plain_text)
        now = utc_now()

        with connect_database(self.database_path) as connection:
            row = connection.execute(
                """
                SELECT
                    id,
                    public_id,
                    name,
                    kind,
                    secret_hash,
                    status,
                    allow_header,
                    allow_query,
                    rate_limit_per_min,
                    allowed_ip_cidrs,
                    expires_at
                FROM api_keys
                WHERE key_prefix = ?
                """,
                (key_prefix,),
            ).fetchone()
            if row is None:
                raise LookupError("invalid api key")
            if row["kind"] != kind:
                raise LookupError("invalid api key")
            if row["status"] != "active":
                raise LookupError("inactive api key")
            if transport == "header":
                if not bool(row["allow_header"]):
                    raise LookupError("header access disabled")
            elif transport == "query":
                if not bool(row["allow_query"]):
                    raise LookupError("query access disabled")
            else:
                raise ValueError("invalid api key transport")
            expires_at = row["expires_at"]
            if expires_at is not None and str(expires_at) <= now:
                raise LookupError("expired api key")
            secret_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
            if not hmac.compare_digest(secret_hash, row["secret_hash"]):
                raise LookupError("invalid api key")
            if request_ip is not None and not self._request_ip_allowed(request_ip, row["allowed_ip_cidrs"]):
                raise LookupError("api key ip not allowed")

            scope_rows = connection.execute(
                """
                SELECT scope
                FROM api_key_scopes
                WHERE api_key_id = ?
                ORDER BY scope ASC
                """,
                (row["id"],),
            ).fetchall()
            domain_rows = connection.execute(
                """
                SELECT domain_id
                FROM api_key_domain_grants
                WHERE api_key_id = ?
                ORDER BY domain_id ASC
                """,
                (row["id"],),
            ).fetchall()
            mailbox_rows = connection.execute(
                """
                SELECT mailbox_pattern
                FROM api_key_mailbox_grants
                WHERE api_key_id = ?
                ORDER BY mailbox_pattern ASC
                """,
                (row["id"],),
            ).fetchall()

        return PermissionContext(
            scopes=tuple(str(row["scope"]) for row in scope_rows),
            domain_ids=tuple(int(row["domain_id"]) for row in domain_rows),
            mailbox_patterns=tuple(str(row["mailbox_pattern"]) for row in mailbox_rows),
            api_key_id=int(row["id"]),
            public_id=str(row["public_id"]),
            name=str(row["name"]),
            kind=str(row["kind"]),
        )

    def compare_public_api_key(self, candidate: object) -> bool:
        set_active_permission_context(None)

        if not isinstance(candidate, str):
            return True

        if self._legacy_public_api_key is not None and candidate == self._legacy_public_api_key:
            if self._legacy_public_context is not None:
                set_active_permission_context(self._legacy_public_context)
            return False

        try:
            context = self.authenticate_plain_text(candidate)
        except LookupError:
            return True

        set_active_permission_context(context)
        return False

    def _parse_plain_text(self, plain_text: str) -> tuple[str, str, str]:
        parts = plain_text.split("_", 3)
        if len(parts) != 4 or parts[0] != "ri":
            raise LookupError("invalid api key")
        _, kind, key_prefix, secret = parts
        if kind not in VALID_API_KEY_KINDS:
            raise LookupError("invalid api key")
        return kind, key_prefix, secret

    def _unique_text_values(self, values: Sequence[str]) -> tuple[str, ...]:
        seen: set[str] = set()
        unique_values: list[str] = []
        for value in values:
            if value in seen:
                continue
            seen.add(value)
            unique_values.append(value)
        return tuple(unique_values)

    def _unique_int_values(self, values: Sequence[int]) -> tuple[int, ...]:
        seen: set[int] = set()
        unique_values: list[int] = []
        for value in values:
            normalized = int(value)
            if normalized in seen:
                continue
            seen.add(normalized)
            unique_values.append(normalized)
        return tuple(unique_values)

    def _nullable_text(self, value: object) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _coerce_bool(self, field_name: str, value: object) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and not isinstance(value, bool) and value in (0, 1):
            return bool(value)
        raise ValueError(f"invalid {field_name}")

    def _coerce_non_negative_int(self, field_name: str, value: object) -> int:
        if isinstance(value, bool):
            raise ValueError(f"invalid {field_name}")
        if isinstance(value, float) and not value.is_integer():
            raise ValueError(f"invalid {field_name}")
        try:
            normalized = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid {field_name}") from exc
        if normalized < 0:
            raise ValueError(f"invalid {field_name}")
        return normalized

    def _normalize_ip_cidrs(self, values: Sequence[str]) -> tuple[str, ...]:
        seen: set[str] = set()
        normalized_values: list[str] = []
        for value in values:
            network = ipaddress.ip_network(str(value), strict=False)
            canonical = network.with_prefixlen
            if canonical in seen:
                continue
            seen.add(canonical)
            normalized_values.append(canonical)
        return tuple(normalized_values)

    def _request_ip_allowed(self, request_ip: str | None, allowed_ip_cidrs_raw: str | None) -> bool:
        if not allowed_ip_cidrs_raw:
            return True
        if request_ip is None:
            return False

        try:
            request_address = ipaddress.ip_address(request_ip)
        except ValueError:
            return False

        try:
            allowed_cidrs = json.loads(allowed_ip_cidrs_raw)
        except json.JSONDecodeError:
            return False

        if isinstance(allowed_cidrs, str):
            allowed_cidrs = [allowed_cidrs]
        if not isinstance(allowed_cidrs, list):
            return False
        if not allowed_cidrs:
            return True

        for allowed_cidr in allowed_cidrs:
            try:
                network = ipaddress.ip_network(str(allowed_cidr), strict=False)
            except ValueError:
                return False
            if request_address in network:
                return True
        return False

    def _load_key(self, connection: sqlite3.Connection, api_key_id: int) -> dict[str, Any] | None:
        row = connection.execute(
            """
            SELECT
                id,
                public_id,
                name,
                description,
                kind,
                key_prefix,
                owner_admin_id,
                status,
                allow_header,
                allow_query,
                rate_limit_per_min,
                allowed_ip_cidrs,
                expires_at,
                last_used_at,
                last_used_ip,
                revoked_at,
                created_by_admin_id,
                created_at
            FROM api_keys
            WHERE id = ?
            """,
            (api_key_id,),
        ).fetchone()
        if row is None:
            return None

        scope_rows = connection.execute(
            """
            SELECT scope
            FROM api_key_scopes
            WHERE api_key_id = ?
            ORDER BY scope ASC
            """,
            (api_key_id,),
        ).fetchall()
        domain_rows = connection.execute(
            """
            SELECT domain_id
            FROM api_key_domain_grants
            WHERE api_key_id = ?
            ORDER BY domain_id ASC
            """,
            (api_key_id,),
        ).fetchall()
        mailbox_rows = connection.execute(
            """
            SELECT mailbox_pattern
            FROM api_key_mailbox_grants
            WHERE api_key_id = ?
            ORDER BY mailbox_pattern ASC
            """,
            (api_key_id,),
        ).fetchall()

        allowed_ip_cidrs = self._decode_allowed_ip_cidrs(row["allowed_ip_cidrs"])
        domain_ids = [int(domain_row["domain_id"]) for domain_row in domain_rows]
        return {
            "id": int(row["id"]),
            "public_id": str(row["public_id"]),
            "name": str(row["name"]),
            "description": row["description"],
            "kind": str(row["kind"]),
            "key_prefix": str(row["key_prefix"]),
            "owner_admin_id": row["owner_admin_id"],
            "status": str(row["status"]),
            "allow_header": bool(row["allow_header"]),
            "allow_query": bool(row["allow_query"]),
            "rate_limit_per_min": int(row["rate_limit_per_min"]),
            "allowed_ip_cidrs": allowed_ip_cidrs,
            "expires_at": row["expires_at"],
            "last_used_at": row["last_used_at"],
            "last_used_ip": row["last_used_ip"],
            "revoked_at": row["revoked_at"],
            "created_by_admin_id": row["created_by_admin_id"],
            "created_at": row["created_at"],
            "scopes": [str(scope_row["scope"]) for scope_row in scope_rows],
            "domain_ids": domain_ids,
            "domain_grant_mode": "selected" if domain_ids else "all",
            "mailbox_patterns": [str(mailbox_row["mailbox_pattern"]) for mailbox_row in mailbox_rows],
        }

    def _decode_allowed_ip_cidrs(self, value: str | None) -> list[str]:
        if not value:
            return []
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return []
        if isinstance(decoded, str):
            return [decoded]
        if isinstance(decoded, list):
            return [str(item) for item in decoded if str(item).strip()]
        return []


__all__ = [
    "ApiKeyService",
    "PublicAPIKeyProxy",
    "get_active_permission_context",
    "make_api_key",
    "set_active_permission_context",
]
