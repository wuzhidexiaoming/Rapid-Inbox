from __future__ import annotations

import hashlib
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import Settings
from app.db.connection import connect_database
from app.db.writer import DatabaseWriter
from app.ingest.storage import utc_now

from .passwords import hash_password, verify_password


SESSION_DURATION_DAYS = 30


def _hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _utc_now_plus_days(days: int) -> str:
    return (
        datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=days)
    ).isoformat().replace("+00:00", "Z")


class AuthService:
    def __init__(self, settings: Settings, writer: DatabaseWriter) -> None:
        self.settings = settings
        self.writer = writer

    async def count_admins(self) -> int:
        with connect_database(self.settings.database_path) as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM admins").fetchone()
        return int(row["count"])

    async def ensure_bootstrap_admin(self) -> None:
        password_hash = hash_password(self.settings.bootstrap_admin_password)
        now = utc_now()
        await self.writer.execute(
            lambda connection: connection.execute(
                """
                INSERT INTO admins (
                    username,
                    password_hash,
                    must_change_password,
                    created_at,
                    updated_at
                )
                SELECT ?, ?, 1, ?, ?
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM admins
                )
                """,
                (
                    self.settings.bootstrap_admin_username,
                    password_hash,
                    now,
                    now,
                ),
            )
        )

    async def authenticate_admin(self, username: str, password: str, *, ip: str | None = None) -> dict[str, Any]:
        with connect_database(self.settings.database_path) as connection:
            row = connection.execute(
                """
                SELECT
                    id,
                    username,
                    display_name,
                    role,
                    is_active,
                    must_change_password,
                    created_at,
                    updated_at,
                    last_login_at,
                    last_login_ip,
                    password_hash
                FROM admins
                WHERE username = ? AND is_active = 1
                """,
                (username,),
            ).fetchone()

        if row is None or not verify_password(password, row["password_hash"]):
            raise LookupError("invalid admin credentials")

        now = utc_now()
        await self.writer.execute(
            lambda connection: connection.execute(
                """
                UPDATE admins
                SET last_login_at = ?,
                    last_login_ip = COALESCE(?, last_login_ip)
                WHERE id = ?
                """,
                (now, ip, row["id"]),
            )
        )

        admin = self._admin_payload(row)
        admin["last_login_at"] = now
        admin["last_login_ip"] = ip if ip is not None else row["last_login_ip"]
        return admin

    async def change_admin_password(self, admin_id: int, current_password: str, new_password: str) -> None:
        with connect_database(self.settings.database_path) as connection:
            row = connection.execute(
                """
                SELECT id, password_hash
                FROM admins
                WHERE id = ? AND is_active = 1
                """,
                (admin_id,),
            ).fetchone()

        if row is None or not verify_password(current_password, row["password_hash"]):
            raise LookupError("invalid admin credentials")

        password_hash = hash_password(new_password)
        updated_at = utc_now()

        def operation(connection: sqlite3.Connection) -> None:
            cursor = connection.execute(
                """
                UPDATE admins
                SET password_hash = ?,
                    must_change_password = 0,
                    updated_at = ?
                WHERE id = ? AND is_active = 1
                """,
                (password_hash, updated_at, admin_id),
            )
            if cursor.rowcount != 1:
                raise LookupError("admin not found")

        await self.writer.execute(operation)

    async def create_session(self, *, admin_id: int, ip: str | None, user_agent: str | None) -> dict[str, Any]:
        session_id = f"sess_{uuid.uuid4().hex}"
        token = secrets.token_urlsafe(32)
        token_hash = _hash_session_token(token)
        created_at = utc_now()
        expires_at = _utc_now_plus_days(SESSION_DURATION_DAYS)

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            admin_row = connection.execute(
                """
                SELECT id
                FROM admins
                WHERE id = ? AND is_active = 1
                """,
                (admin_id,),
            ).fetchone()
            if admin_row is None:
                raise LookupError("admin not found")

            connection.execute(
                """
                INSERT INTO admin_sessions (
                    id,
                    admin_id,
                    session_token_hash,
                    created_at,
                    expires_at,
                    last_seen_at,
                    last_ip,
                    user_agent
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    admin_id,
                    token_hash,
                    created_at,
                    expires_at,
                    created_at,
                    ip,
                    user_agent,
                ),
            )
            return {
                "id": session_id,
                "admin_id": admin_id,
                "token": token,
                "created_at": created_at,
                "expires_at": expires_at,
                "last_seen_at": created_at,
                "last_ip": ip,
                "user_agent": user_agent,
            }

        return await self.writer.execute(operation)

    async def get_session_admin(self, token: str, *, ip: str | None = None) -> dict[str, Any]:
        token_hash = _hash_session_token(token)
        now = utc_now()

        with connect_database(self.settings.database_path) as connection:
            row = connection.execute(
                """
                SELECT
                    s.id AS session_id,
                    s.admin_id,
                    s.created_at AS session_created_at,
                    s.expires_at,
                    s.last_seen_at AS session_last_seen_at,
                    s.last_ip AS session_last_ip,
                    s.user_agent AS session_user_agent,
                    a.id,
                    a.username,
                    a.display_name,
                    a.role,
                    a.is_active,
                    a.must_change_password,
                    a.created_at,
                    a.updated_at,
                    a.last_login_at,
                    a.last_login_ip
                FROM admin_sessions AS s
                JOIN admins AS a ON a.id = s.admin_id
                WHERE s.session_token_hash = ?
                    AND s.revoked_at IS NULL
                    AND s.expires_at > ?
                    AND a.is_active = 1
                """,
                (token_hash, now),
            ).fetchone()

        if row is None:
            raise LookupError("session not found")

        await self.writer.execute(
            lambda connection: connection.execute(
                """
                UPDATE admin_sessions
                SET last_seen_at = ?,
                    last_ip = COALESCE(?, last_ip)
                WHERE id = ?
                """,
                (now, ip, row["session_id"]),
            )
        )

        payload = self._admin_payload(row)
        payload.update(
            {
                "session_id": row["session_id"],
                "admin_id": int(row["admin_id"]),
                "session_created_at": row["session_created_at"],
                "session_expires_at": row["expires_at"],
                "session_last_seen_at": now,
                "session_last_ip": ip if ip is not None else row["session_last_ip"],
                "session_user_agent": row["session_user_agent"],
            }
        )
        return payload

    async def revoke_session(self, session_id: str) -> None:
        revoked_at = utc_now()
        await self.writer.execute(
            lambda connection: connection.execute(
                """
                UPDATE admin_sessions
                SET revoked_at = COALESCE(revoked_at, ?)
                WHERE id = ?
                """,
                (revoked_at, session_id),
            )
        )

    def _admin_payload(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "username": row["username"],
            "display_name": row["display_name"],
            "role": row["role"],
            "is_active": bool(row["is_active"]),
            "must_change_password": bool(row["must_change_password"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_login_at": row["last_login_at"],
            "last_login_ip": row["last_login_ip"],
        }
