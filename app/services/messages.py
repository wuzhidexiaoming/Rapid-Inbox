from __future__ import annotations

import base64
import html
import json
import re
import sqlite3
from typing import Any

from app.db.connection import connect_database
from app.ingest.storage import utc_now
from app.ingest.queue import ParseTask


SAFE_INLINE_CONTENT_TYPES = {
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
}

_CID_REFERENCE_RE = re.compile(r'cid:([^"\'<>\s]+)', re.IGNORECASE)
_EMAIL_ADDRESS_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
_CODE_CANDIDATE_RE = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)

_VERIFICATION_CONTEXT_HINTS = (
    "验证码",
    "校验码",
    "verification code",
    "verify code",
    "one-time code",
    "one time code",
    "otp",
)

_OPENAI_SENDERS = (
    "noreply@openai.com",
    "no-reply@openai.com",
    "@openai.com",
    ".openai.com",
)

_OPENAI_HINTS = (
    "your openai verification code",
    "your openai code",
    "verify your email",
    "openai verification code",
)


class MessageService:
    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    async def get_mailbox_view(
        self,
        mailbox_address: str,
        *,
        limit: int = 50,
        offset: int = 0,
        cursor: tuple[str, str] | None = None,
        request_ip: str | None = None,
    ) -> dict[str, Any]:
        return await self._runtime.get_mailbox_view(
            mailbox_address,
            limit=limit,
            offset=offset,
            cursor=cursor,
            request_ip=request_ip,
        )

    async def get_delivery_detail(
        self,
        mailbox_address: str,
        delivery_id: str,
        *,
        request_ip: str | None = None,
    ) -> dict[str, Any]:
        return await self._runtime.get_delivery_detail(mailbox_address, delivery_id, request_ip=request_ip)

    async def get_raw_message(
        self,
        mailbox_address: str,
        delivery_id: str,
        *,
        request_ip: str | None = None,
    ) -> bytes:
        await self.get_delivery_detail(mailbox_address, delivery_id, request_ip=request_ip)
        return await self._runtime.get_raw_message(delivery_id)

    async def reparse_message(self, message_id: str) -> None:
        def operation(connection: sqlite3.Connection) -> int:
            cursor = connection.execute(
                """
                UPDATE messages
                SET parse_status = 'pending',
                    parse_error = NULL
                WHERE id = ?
                """,
                (message_id,),
            )
            return int(cursor.rowcount)

        updated_rows = await self._runtime.writer.execute(operation)
        if updated_rows == 0:
            raise LookupError("message not found")
        await self._runtime.parse_queue.enqueue(ParseTask(message_id=message_id))

    def list_messages(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        query: str | None = None,
        parse_status: str | None = None,
        mailbox_id: int | None = None,
    ) -> dict[str, Any]:
        where_sql, params = self._message_filter_sql(
            query=query,
            parse_status=parse_status,
            mailbox_id=mailbox_id,
        )
        with connect_database(self._runtime.settings.database_path) as connection:
            rows = connection.execute(
                f"""
                SELECT
                    m.id,
                    m.subject,
                    m.from_addr,
                    COALESCE(
                        (
                            SELECT GROUP_CONCAT(rcpt_to, ', ')
                            FROM (
                                SELECT DISTINCT rcpt_to
                                FROM message_deliveries
                                WHERE message_id = m.id
                                ORDER BY rcpt_to ASC
                            )
                        ),
                        ''
                    ) AS recipients,
                    m.received_at,
                    m.parse_status,
                    m.parse_error,
                    m.has_attachments,
                    m.attachment_count,
                    COUNT(d.id) AS delivery_count
                FROM messages AS m
                LEFT JOIN message_deliveries AS d ON d.message_id = m.id
                {where_sql}
                GROUP BY m.id
                ORDER BY m.received_at DESC, m.id DESC
                LIMIT ? OFFSET ?
                """,
                (*params, limit, offset),
            ).fetchall()
            total = connection.execute(
                f"""
                SELECT COUNT(DISTINCT m.id) AS count
                FROM messages AS m
                LEFT JOIN message_deliveries AS d ON d.message_id = m.id
                {where_sql}
                """,
                tuple(params),
            ).fetchone()
        return {
            "items": [dict(row) for row in rows],
            "total_count": 0 if total is None else int(total["count"]),
        }

    def get_admin_message_detail(self, message_id: str) -> dict[str, Any]:
        with connect_database(self._runtime.settings.database_path) as connection:
            row = connection.execute(
                """
                SELECT
                    id,
                    smtp_session_id,
                    raw_path,
                    raw_sha256,
                    raw_size_bytes,
                    envelope_from,
                    message_id_header,
                    subject,
                    from_name,
                    from_addr,
                    reply_to,
                    date_header,
                    received_at,
                    indexed_at,
                    parse_status,
                    parse_error,
                    has_text,
                    has_html,
                    has_attachments,
                    attachment_count,
                    text_preview,
                    text_body_path,
                    html_body_path,
                    headers_json
                FROM messages
                WHERE id = ?
                """,
                (message_id,),
            ).fetchone()
            if row is None:
                raise LookupError("message not found")
            deliveries = connection.execute(
                """
                SELECT
                    d.id AS delivery_id,
                    d.mailbox_id,
                    mb.address_canonical AS mailbox,
                    d.rcpt_to,
                    d.delivered_at,
                    d.status,
                    d.deleted_at
                FROM message_deliveries AS d
                JOIN mailboxes AS mb ON mb.id = d.mailbox_id
                WHERE d.message_id = ?
                ORDER BY d.delivered_at DESC, d.id DESC
                """,
                (message_id,),
            ).fetchall()
            attachments = connection.execute(
                """
                SELECT
                    id,
                    filename,
                    safe_filename,
                    content_type,
                    content_disposition,
                    content_id,
                    storage_path,
                    size_bytes,
                    is_inline
                FROM attachments
                WHERE message_id = ?
                ORDER BY part_index ASC
                """,
                (message_id,),
            ).fetchall()

        payload = dict(row)
        for key in ("has_text", "has_html", "has_attachments"):
            payload[key] = bool(payload[key])
        payload["text_body"] = self._runtime.storage.read_text(payload.get("text_body_path")) or ""
        payload["html_body"] = self._runtime.storage.read_text(payload.get("html_body_path")) or ""
        payload["headers"] = json.loads(payload.get("headers_json") or "[]")
        payload.pop("headers_json", None)
        payload["deliveries"] = [dict(delivery) for delivery in deliveries]
        payload["attachments"] = [dict(attachment) for attachment in attachments]
        return payload

    def get_admin_delivery_detail(self, delivery_id: str) -> dict[str, Any]:
        with connect_database(self._runtime.settings.database_path) as connection:
            row = connection.execute(
                """
                SELECT message_id
                FROM message_deliveries
                WHERE id = ?
                """,
                (delivery_id,),
            ).fetchone()
        if row is None:
            raise LookupError("delivery not found")
        detail = self.get_admin_message_detail(str(row["message_id"]))
        detail["selected_delivery_id"] = delivery_id
        return detail

    def get_admin_raw_message(self, message_id: str) -> bytes:
        detail = self.get_admin_message_detail(message_id)
        return self._runtime.storage.read_bytes(detail["raw_path"])

    def get_admin_attachment(self, message_id: str, attachment_id: str) -> dict[str, Any]:
        detail = self.get_admin_message_detail(message_id)
        for attachment in detail["attachments"]:
            if attachment["id"] != attachment_id:
                continue
            payload = dict(attachment)
            payload["content"] = self._runtime.storage.read_bytes(attachment["storage_path"])
            return payload
        raise LookupError("attachment not found")

    async def soft_delete_delivery(self, delivery_id: str) -> dict[str, Any]:
        result = await self.soft_delete_deliveries([delivery_id])
        if result["deleted"] == 0:
            raise LookupError("delivery not found")
        return result

    async def soft_delete_deliveries(self, delivery_ids: list[str]) -> dict[str, Any]:
        deleted_at = utc_now()
        unique_ids = []
        seen: set[str] = set()
        for delivery_id in delivery_ids:
            if delivery_id in seen:
                continue
            seen.add(delivery_id)
            unique_ids.append(delivery_id)
        if not unique_ids:
            return {"deleted": 0, "delivery_ids": []}

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            placeholders = ", ".join("?" for _ in unique_ids)
            rows = connection.execute(
                f"""
                SELECT id, mailbox_id
                FROM message_deliveries
                WHERE id IN ({placeholders}) AND status = 'active'
                """,
                tuple(unique_ids),
            ).fetchall()
            if not rows:
                return {"deleted": 0, "delivery_ids": []}
            connection.execute(
                f"""
                UPDATE message_deliveries
                SET status = 'deleted',
                    deleted_at = COALESCE(deleted_at, ?)
                WHERE id IN ({placeholders}) AND status = 'active'
                """,
                (deleted_at, *unique_ids),
            )
            mailbox_ids = sorted({int(row["mailbox_id"]) for row in rows})
            for mailbox_id in mailbox_ids:
                self._runtime._refresh_mailbox_summary_after_message_delete(connection, mailbox_id)
            return {
                "deleted": len(rows),
                "delivery_ids": [str(row["id"]) for row in rows],
            }

        return await self._runtime.writer.execute(operation)

    async def get_public_mailbox_view(
        self,
        mailbox_address: str,
        *,
        surface: str,
        limit: int = 50,
        offset: int = 0,
        cursor: tuple[str, str] | None = None,
        request_ip: str | None = None,
    ) -> dict[str, Any]:
        canonical_mailbox_address = await self._require_public_surface_enabled(mailbox_address, surface)
        mailbox = await self._runtime.get_mailbox_view(
            canonical_mailbox_address,
            limit=limit,
            offset=offset,
            cursor=cursor,
            request_ip=request_ip,
        )
        items = [self._prepare_public_mailbox_item(item, surface=surface) for item in mailbox["items"]]
        return {**mailbox, "items": items}

    async def get_public_delivery_detail(
        self,
        mailbox_address: str,
        delivery_id: str,
        *,
        surface: str,
        request_ip: str | None = None,
    ) -> dict[str, Any]:
        canonical_mailbox_address = await self._require_public_surface_enabled(mailbox_address, surface)
        return await self._runtime.get_delivery_detail(canonical_mailbox_address, delivery_id, request_ip=request_ip)

    async def get_public_mailbox_item(
        self,
        mailbox_address: str,
        delivery_id: str,
        *,
        surface: str,
        request_ip: str | None = None,
    ) -> dict[str, Any]:
        canonical_mailbox_address = await self._require_public_surface_enabled(mailbox_address, surface)
        item = await self._runtime.get_mailbox_delivery_item(
            canonical_mailbox_address,
            delivery_id,
            request_ip=request_ip,
        )
        return self._prepare_public_mailbox_item(item, surface=surface)

    async def get_public_raw_message(
        self,
        mailbox_address: str,
        delivery_id: str,
        *,
        surface: str,
        request_ip: str | None = None,
    ) -> bytes:
        await self.get_public_delivery_detail(
            mailbox_address,
            delivery_id,
            surface=surface,
            request_ip=request_ip,
        )
        return await self._runtime.get_raw_message(delivery_id)

    async def get_public_html_preview_srcdoc(
        self,
        mailbox_address: str,
        delivery_id: str,
        *,
        surface: str,
        request_ip: str | None = None,
    ) -> str:
        detail = await self.get_public_delivery_detail(
            mailbox_address,
            delivery_id,
            surface=surface,
            request_ip=request_ip,
        )
        attachments = self._load_attachments_with_content_ids(detail["message_id"], detail["attachments"])
        html_body = self.rewrite_cid_references(
            detail["html_body"] or "",
            attachments,
        )
        return self.build_public_html_preview_document(html_body)

    def build_public_html_preview_document(self, html_body: str) -> str:
        return (
            "<!doctype html>"
            '<html lang="zh-CN">'
            "<head>"
            '<meta charset="utf-8" />'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src data:; style-src \'unsafe-inline\'; form-action \'none\'; connect-src \'none\'; object-src \'none\'; frame-src \'none\'; script-src \'none\'" />'
            '<meta name="referrer" content="no-referrer" />'
            '<base href="about:srcdoc" />'
            "</head>"
            f"<body>{html_body}</body>"
            "</html>"
        )

    def rewrite_cid_references(
        self,
        html_body: str,
        attachments: list[dict[str, Any]],
    ) -> str:
        attachment_routes: dict[str, str] = {}
        for attachment in attachments:
            content_id = self._normalize_cid_reference(attachment.get("content_id"))
            if not content_id:
                continue
            data_url = self._build_inline_data_url(attachment)
            if data_url is None:
                continue
            attachment_routes[content_id] = data_url
        if not attachment_routes:
            return html_body

        def replace_reference(match: re.Match[str]) -> str:
            reference = self._normalize_cid_reference(match.group(1))
            return attachment_routes.get(reference, match.group(0))

        return _CID_REFERENCE_RE.sub(replace_reference, html_body)

    async def _require_public_surface_enabled(self, mailbox_address: str, surface: str) -> str:
        match = self._runtime.domains.match_address(mailbox_address)
        if match is None:
            raise LookupError("mailbox domain not managed")
        if surface not in {"web", "api"}:
            raise ValueError("invalid public surface")

        with connect_database(self._runtime.settings.database_path) as connection:
            row = connection.execute(
                """
                SELECT public_web_enabled, public_api_enabled
                FROM domains
                WHERE id = ?
                """,
                (match.domain_id,),
            ).fetchone()
        if row is None:
            raise LookupError("mailbox domain not managed")
        if surface == "web" and not bool(row["public_web_enabled"]):
            raise LookupError("public web disabled")
        if surface == "api" and not bool(row["public_api_enabled"]):
            raise LookupError("public api disabled")
        return match.address_canonical

    def _normalize_cid_reference(self, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip().strip("<>").lower()

    def _build_inline_data_url(self, attachment: dict[str, Any]) -> str | None:
        content_type = self._normalize_content_type(attachment.get("content_type"))
        if content_type not in SAFE_INLINE_CONTENT_TYPES:
            return None

        content = self._runtime.storage.read_bytes(attachment["storage_path"])
        encoded = base64.b64encode(content).decode("ascii")
        return f"data:{content_type};base64,{encoded}"

    def _load_attachments_with_content_ids(
        self,
        message_id: str,
        attachments: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not attachments:
            return attachments

        with connect_database(self._runtime.settings.database_path) as connection:
            rows = connection.execute(
                """
                SELECT id, content_id
                FROM attachments
                WHERE message_id = ?
                ORDER BY part_index ASC
                """,
                (message_id,),
            ).fetchall()

        content_ids = {str(row["id"]): row["content_id"] for row in rows}
        enriched_attachments: list[dict[str, Any]] = []
        for attachment in attachments:
            payload = dict(attachment)
            content_id = content_ids.get(str(payload["id"]))
            if content_id is not None:
                payload["content_id"] = content_id
            enriched_attachments.append(payload)
        return enriched_attachments

    def _normalize_content_type(self, value: Any) -> str:
        return str(value or "").split(";", 1)[0].strip().lower()

    def _message_filter_sql(
        self,
        *,
        query: str | None,
        parse_status: str | None,
        mailbox_id: int | None,
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if query:
            pattern = f"%{query.strip()}%"
            clauses.append(
                "(m.subject LIKE ? OR m.from_addr LIKE ? OR m.envelope_from LIKE ? OR d.rcpt_to LIKE ?)"
            )
            params.extend([pattern, pattern, pattern, pattern])
        if parse_status:
            if parse_status not in {"pending", "parsed", "failed"}:
                raise ValueError("invalid parse_status")
            clauses.append("m.parse_status = ?")
            params.append(parse_status)
        if mailbox_id is not None:
            clauses.append("d.mailbox_id = ?")
            params.append(int(mailbox_id))
        if not clauses:
            return "", params
        return "WHERE " + " AND ".join(clauses), params

    def _prepare_public_mailbox_item(self, item: dict[str, Any], *, surface: str) -> dict[str, Any]:
        payload = dict(item)
        if surface == "web":
            payload["verification_code"] = self._extract_verification_code(payload)
        payload.pop("text_preview", None)
        payload.pop("text_body_path", None)
        payload.pop("html_body_path", None)
        return payload

    def _extract_verification_code(self, item: dict[str, Any]) -> str | None:
        if item.get("parse_status") != "parsed":
            return None
        if not self._looks_like_verification_candidate(item):
            return None

        sender = str(item.get("from_addr") or "")
        subject = str(item.get("subject") or "")
        text = self._build_verification_analysis_text(item)
        if not text:
            return None

        context_candidates = self._find_context_matched_candidates(text)
        if len(context_candidates) == 1:
            return context_candidates[0]
        if len(context_candidates) > 1:
            return None

        if self._is_openai_candidate_message(sender, subject, text):
            all_candidates = self._find_code_candidates(text)
            if len(all_candidates) == 1 and self._contains_openai_hint(text):
                return all_candidates[0]

        return None

    def _looks_like_verification_candidate(self, item: dict[str, Any]) -> bool:
        sender = str(item.get("from_addr") or "")
        subject = str(item.get("subject") or "")
        preview = str(item.get("text_preview") or "")
        combined = "\n".join(part for part in (sender, subject, preview) if part).lower()
        if any(hint in combined for hint in _VERIFICATION_CONTEXT_HINTS):
            return True
        return self._is_openai_candidate_message(sender, subject, preview)

    def _build_verification_analysis_text(self, item: dict[str, Any]) -> str:
        parts = [str(item.get("subject") or ""), str(item.get("text_preview") or "")]

        text_body = self._safe_read_storage_text(item.get("text_body_path"))
        if text_body:
            parts.append(text_body)

        html_body = self._safe_read_storage_text(item.get("html_body_path"))
        if html_body:
            parts.append(self._html_to_text(html_body))

        return self._normalize_analysis_text("\n".join(part for part in parts if part))

    def _safe_read_storage_text(self, storage_path: Any) -> str:
        if not storage_path:
            return ""
        try:
            return self._runtime.storage.read_text(str(storage_path)) or ""
        except OSError:
            return ""

    def _html_to_text(self, html_body: str) -> str:
        without_active_content = _SCRIPT_STYLE_RE.sub(" ", html_body)
        stripped = _TAG_RE.sub(" ", without_active_content)
        return html.unescape(stripped)

    def _normalize_analysis_text(self, text: str) -> str:
        without_addresses = _EMAIL_ADDRESS_RE.sub(" ", text)
        return re.sub(r"\s+", " ", without_addresses).strip()

    def _find_context_matched_candidates(self, text: str) -> list[str]:
        matches: list[str] = []
        for candidate in self._find_code_candidates(text):
            if self._candidate_has_context(text, candidate):
                matches.append(candidate)
        return matches

    def _find_code_candidates(self, text: str) -> list[str]:
        ordered_unique: list[str] = []
        for match in _CODE_CANDIDATE_RE.finditer(text):
            candidate = match.group(1)
            if candidate not in ordered_unique:
                ordered_unique.append(candidate)
        return ordered_unique

    def _candidate_has_context(self, text: str, candidate: str) -> bool:
        candidate_re = re.compile(rf"(?<!\d){re.escape(candidate)}(?!\d)")
        lowered = text.lower()
        for match in candidate_re.finditer(text):
            window = lowered[max(0, match.start() - 80): min(len(lowered), match.end() + 80)]
            if any(hint in window for hint in _VERIFICATION_CONTEXT_HINTS):
                return True
        return False

    def _is_openai_candidate_message(self, sender: str, *content_parts: str) -> bool:
        sender_lower = sender.lower()
        if any(token in sender_lower for token in _OPENAI_SENDERS):
            return True
        return any("openai" in str(part).lower() for part in content_parts if part)

    def _contains_openai_hint(self, text: str) -> bool:
        lowered = text.lower()
        return any(hint in lowered for hint in _OPENAI_HINTS)


__all__ = ["MessageService"]
