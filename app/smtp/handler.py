from __future__ import annotations

import uuid

from app.ingest.storage import utc_now


class RapidInboxHandler:
    def __init__(self, runtime) -> None:
        self.runtime = runtime

    async def handle_RCPT(self, server, session, envelope, address: str, rcpt_options):
        session_id = self._ensure_session_id(session)
        await self.runtime.ensure_smtp_session(session_id, session, last_rcpt_to=address)
        session_event = self._session_event(session, session_id, {"rcpt_to": address, "state": "rcpt"})
        if self.runtime.domains.match_address(address) is None:
            await self.runtime.live_state.publish(
                {
                    **session_event,
                    "type": "rcpt_rejected",
                    "rcpt_to": address,
                    "state": "rcpt",
                }
            )
            return "550 domain not allowed"

        if address not in envelope.rcpt_tos:
            envelope.rcpt_tos.append(address)
        await self.runtime.live_state.publish(
            {
                **session_event,
                "type": "rcpt_accepted",
                "rcpt_to": address,
                "state": "rcpt",
            }
        )
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):
        session_id = self._ensure_session_id(session)
        await self.runtime.ensure_smtp_session(session_id, session)
        if len(envelope.content) > self.runtime.settings.max_message_size_bytes:
            return "552 message too large"
        if not envelope.rcpt_tos:
            return "554 no valid recipients"

        result = await self.runtime.accept_message(
            rcpt_tos=list(envelope.rcpt_tos),
            envelope_from=getattr(envelope, "mail_from", None),
            content=envelope.content,
            smtp_session_id=session_id,
        )
        message_id = None
        if result.startswith("250 queued as "):
            message_id = result.removeprefix("250 queued as ").strip() or None
        await self.runtime.live_state.publish(
            {
                **self._session_event(session, session_id, {"state": "queued"}),
                "type": "queued",
                "mail_from": getattr(envelope, "mail_from", None),
                "rcpt_count": len(envelope.rcpt_tos),
                "message_id": message_id,
                "state": "queued",
            }
        )
        return result

    def _ensure_session_id(self, session) -> str:
        session_id = getattr(session, "rapid_inbox_session_id", None)
        if session_id is None:
            session_id = f"smtp_{uuid.uuid4().hex}"
            setattr(session, "rapid_inbox_session_id", session_id)
        return session_id

    def _session_event(self, session, session_id: str, extra: dict[str, object] | None = None) -> dict[str, object]:
        peer = getattr(session, "peer", None) or ("unknown", None)
        event: dict[str, object] = {
            "session_id": session_id,
            "remote_ip": peer[0] or "unknown",
            "remote_port": peer[1],
            "helo": getattr(session, "host_name", None),
            "tls_used": bool(getattr(session, "ssl", None)),
            "ts": utc_now(),
        }
        if extra:
            event.update(extra)
        return event
