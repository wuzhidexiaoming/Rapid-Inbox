from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException


@dataclass(slots=True)
class PermissionContext:
    scopes: tuple[str, ...]
    domain_ids: tuple[int, ...]
    mailbox_patterns: tuple[str, ...]
    api_key_id: int | None = None
    public_id: str = ""
    name: str = ""
    kind: str = "public"
    legacy_credential: bool = False


class PermissionDenied(HTTPException):
    def __init__(self, detail: str) -> None:
        super().__init__(status_code=403, detail=detail)


def ensure_mailbox_access(grants: PermissionContext, mailbox_address: str, domain_id: int, required_scope: str) -> None:
    if required_scope not in grants.scopes:
        raise PermissionDenied(required_scope)
    # The legacy configured token is treated as an unrestricted compatibility path.
    if grants.legacy_credential:
        return
    if domain_id not in grants.domain_ids:
        raise PermissionDenied("domain grant missing")
    if grants.mailbox_patterns and mailbox_address not in grants.mailbox_patterns:
        raise PermissionDenied("mailbox grant missing")
