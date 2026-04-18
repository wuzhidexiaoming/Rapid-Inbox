from __future__ import annotations

from typing import Any

from app.services.messages import MessageService


SAFE_INLINE_CONTENT_TYPES = {
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
}


class AttachmentService:
    def __init__(self, runtime: Any, messages: MessageService | None = None) -> None:
        self._runtime = runtime
        self._messages = messages or MessageService(runtime)

    async def get_delivery_attachment(
        self,
        mailbox_address: str,
        delivery_id: str,
        attachment_id: str,
        *,
        surface: str = "web",
        request_ip: str | None = None,
    ) -> dict[str, Any]:
        detail = await self._messages.get_public_delivery_detail(
            mailbox_address,
            delivery_id,
            surface=surface,
            request_ip=request_ip,
        )
        for attachment in detail["attachments"]:
            if attachment["id"] != attachment_id:
                continue
            payload = dict(attachment)
            payload["content"] = self._runtime.storage.read_bytes(attachment["storage_path"])
            return payload
        raise LookupError("attachment not found")

    def build_attachment_response_headers(self, attachment: dict[str, Any]) -> dict[str, str]:
        disposition = "inline" if self._should_inline_attachment(attachment) else "attachment"
        safe_filename = attachment.get("safe_filename") or "attachment.bin"
        return {
            "Content-Disposition": f'{disposition}; filename="{safe_filename}"',
            "X-Content-Type-Options": "nosniff",
        }

    def _should_inline_attachment(self, attachment: dict[str, Any]) -> bool:
        if not bool(attachment.get("is_inline")):
            return False
        content_type = str(attachment.get("content_type") or "").split(";", 1)[0].strip().lower()
        return content_type in SAFE_INLINE_CONTENT_TYPES


__all__ = ["AttachmentService"]
