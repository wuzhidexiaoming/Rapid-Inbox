from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import uuid
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Sequence

from app.db.connection import connect_database
from app.db.writer import DatabaseWriter
from app.ingest.storage import utc_now

from .permissions import PermissionContext


VALID_API_KEY_KINDS = {"admin", "public", "service"}
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
        rate_limit_per_min: int = 60,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        if kind not in VALID_API_KEY_KINDS:
            raise ValueError("invalid api key kind")
        if status not in {"active", "revoked", "expired", "disabled"}:
            raise ValueError("invalid api key status")

        scope_values = self._unique_text_values(scopes)
        domain_values = self._unique_int_values(domain_ids)
        mailbox_values = self._unique_text_values(mailbox_patterns)
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
                    expires_at,
                    created_by_admin_id,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                "expires_at": expires_at,
                "created_at": created_at,
            }

        return await self.writer.execute(operation)

    def authenticate_plain_text(self, plain_text: str) -> PermissionContext:
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
            if not bool(row["allow_header"]):
                raise LookupError("header access disabled")
            expires_at = row["expires_at"]
            if expires_at is not None and str(expires_at) <= now:
                raise LookupError("expired api key")
            secret_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
            if not hmac.compare_digest(secret_hash, row["secret_hash"]):
                raise LookupError("invalid api key")

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

    async def record_usage(self, context: PermissionContext, *, ip: str | None = None) -> None:
        if context.api_key_id is None:
            return

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


__all__ = [
    "ApiKeyService",
    "PublicAPIKeyProxy",
    "get_active_permission_context",
    "make_api_key",
    "set_active_permission_context",
]
