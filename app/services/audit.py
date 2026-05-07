from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.db.connection import connect_database
from app.ingest.storage import utc_now


class AuditService:
    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    async def log(
        self,
        actor_type: str,
        actor_ref: str | None,
        action: str,
        resource_type: str,
        resource_ref: str | None,
        status: str,
        *,
        ip: str | None = None,
        user_agent: str | None = None,
        details: Any | None = None,
    ) -> dict[str, Any]:
        created_at = utc_now()
        details_json = None if details is None else json.dumps(details, ensure_ascii=False)

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            cursor = connection.execute(
                """
                INSERT INTO audit_logs (
                    actor_type,
                    actor_ref,
                    action,
                    resource_type,
                    resource_ref,
                    status,
                    ip,
                    user_agent,
                    details_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    actor_type,
                    actor_ref,
                    action,
                    resource_type,
                    resource_ref,
                    status,
                    ip,
                    user_agent,
                    details_json,
                    created_at,
                ),
            )
            return {
                "id": int(cursor.lastrowid),
                "actor_type": actor_type,
                "actor_ref": actor_ref,
                "action": action,
                "resource_type": resource_type,
                "resource_ref": resource_ref,
                "status": status,
                "ip": ip,
                "user_agent": user_agent,
                "details": details,
                "created_at": created_at,
            }

        return await self._runtime.writer.execute(operation)

    def list_logs(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        actor: str | None = None,
        action: str | None = None,
        resource: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if actor:
            clauses.append("(actor_ref = ? OR actor_type = ?)")
            params.extend([actor, actor])
        if action:
            clauses.append("action = ?")
            params.append(action)
        if resource:
            clauses.append("(resource_type = ? OR resource_ref = ?)")
            params.extend([resource, resource])
        if start_time:
            clauses.append("created_at >= ?")
            params.append(start_time)
        if end_time:
            clauses.append("created_at <= ?")
            params.append(end_time)
        where_sql = "WHERE " + " AND ".join(clauses) if clauses else ""
        with connect_database(self._runtime.settings.database_path) as connection:
            rows = connection.execute(
                f"""
                SELECT
                    id,
                    actor_type,
                    actor_ref,
                    action,
                    resource_type,
                    resource_ref,
                    status,
                    ip,
                    user_agent,
                    details_json,
                    created_at
                FROM audit_logs
                {where_sql}
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (*params, limit, offset),
            ).fetchall()
            total = connection.execute(
                f"SELECT COUNT(*) AS count FROM audit_logs {where_sql}",
                tuple(params),
            ).fetchone()

        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            details_json = item.get("details_json")
            item["details"] = json.loads(details_json) if details_json else None
            item.pop("details_json", None)
            items.append(item)
        return {"items": items, "total_count": 0 if total is None else int(total["count"])}


__all__ = ["AuditService"]
