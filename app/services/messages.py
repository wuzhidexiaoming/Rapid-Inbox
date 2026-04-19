from __future__ import annotations

import base64
import re
import sqlite3
from typing import Any

from app.db.connection import connect_database
from app.ingest.queue import ParseTask


SAFE_INLINE_CONTENT_TYPES = {
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
}

_CID_REFERENCE_RE = re.compile(r'cid:([^"\'<>\s]+)', re.IGNORECASE)


class MessageService:
    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    async def get_mailbox_view(
        self,
        mailbox_address: str,
        *,
        limit: int = 50,
        offset: int = 0,
        request_ip: str | None = None,
    ) -> dict[str, Any]:
        return await self._runtime.get_mailbox_view(
            mailbox_address,
            limit=limit,
            offset=offset,
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

    async def get_public_mailbox_view(
        self,
        mailbox_address: str,
        *,
        surface: str,
        limit: int = 50,
        offset: int = 0,
        request_ip: str | None = None,
    ) -> dict[str, Any]:
        canonical_mailbox_address = await self._require_public_surface_enabled(mailbox_address, surface)
        return await self._runtime.get_mailbox_view(
            canonical_mailbox_address,
            limit=limit,
            offset=offset,
            request_ip=request_ip,
        )

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
            '<html lang="en">'
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


__all__ = ["MessageService"]
