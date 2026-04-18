from __future__ import annotations

from typing import Any


class MessageService:
    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

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


__all__ = ["MessageService"]
