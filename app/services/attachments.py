from __future__ import annotations

from typing import Any

from app.services.messages import MessageService


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
        request_ip: str | None = None,
    ) -> dict[str, Any]:
        detail = await self._messages.get_delivery_detail(mailbox_address, delivery_id, request_ip=request_ip)
        for attachment in detail["attachments"]:
            if attachment["id"] != attachment_id:
                continue
            payload = dict(attachment)
            payload["content"] = self._runtime.storage.read_bytes(attachment["storage_path"])
            return payload
        raise LookupError("attachment not found")


__all__ = ["AttachmentService"]
