from __future__ import annotations

import json
from typing import Any

from app.db.connection import connect_database


LIVE_SSE_EVENT_TYPES: tuple[str, ...] = ("rcpt_accepted", "rcpt_rejected", "queued")


def encode_sse(event: dict[str, object]) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


def smtp_live_snapshot(runtime, *, history_limit: int = 25) -> list[dict[str, Any]]:
    events = runtime.live_state.snapshot()
    if events:
        return events
    return _recent_message_events(runtime, limit=history_limit)


def recent_smtp_sessions(runtime, *, limit: int = 25) -> list[dict[str, Any]]:
    with connect_database(runtime.settings.database_path) as connection:
        rows = connection.execute(
            """
            SELECT
                id,
                remote_ip,
                remote_port,
                helo_name,
                status,
                tls_used,
                connect_at,
                disconnect_at,
                first_command_at,
                last_command_at,
                message_count,
                rcpt_accepted_count,
                rcpt_rejected_count,
                bytes_received,
                last_mail_from,
                last_rcpt_to_sample,
                result_code,
                result_message,
                close_reason
            FROM smtp_sessions
            ORDER BY connect_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    sessions: list[dict[str, Any]] = []
    for row in rows:
        session = dict(row)
        session["tls_used"] = bool(session["tls_used"])
        sessions.append(session)
    return sessions


def _recent_message_events(runtime, *, limit: int = 25) -> list[dict[str, Any]]:
    with connect_database(runtime.settings.database_path) as connection:
        rows = connection.execute(
            """
            SELECT
                d.id AS delivery_id,
                d.rcpt_to,
                d.delivered_at,
                m.id AS message_id,
                m.smtp_session_id,
                m.envelope_from
            FROM message_deliveries AS d
            JOIN messages AS m ON m.id = d.message_id
            ORDER BY d.delivered_at DESC, d.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    events: list[dict[str, Any]] = []
    seen_messages: set[str] = set()
    for row in rows:
        session_id = row["smtp_session_id"] or row["message_id"]
        events.append(
            {
                "type": "rcpt_accepted",
                "session_id": session_id,
                "delivery_id": row["delivery_id"],
                "message_id": row["message_id"],
                "rcpt_to": row["rcpt_to"],
                "mail_from": row["envelope_from"],
                "ts": row["delivered_at"],
                "source": "history",
            }
        )
        if row["message_id"] in seen_messages:
            continue
        seen_messages.add(row["message_id"])
        events.append(
            {
                "type": "queued",
                "session_id": session_id,
                "message_id": row["message_id"],
                "mail_from": row["envelope_from"],
                "ts": row["delivered_at"],
                "source": "history",
            }
        )
    return events
