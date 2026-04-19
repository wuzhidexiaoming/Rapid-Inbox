from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode


DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


def build_pagination_context(
    *,
    path: str,
    limit: int,
    offset: int,
    total_count: int,
    item_count: int,
    extra_params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    has_previous = offset > 0
    has_next = offset + item_count < total_count
    previous_offset = max(offset - limit, 0) if has_previous else None
    next_offset = offset + limit if has_next else None
    current_page = (offset // limit) + 1 if total_count else 0
    total_pages = (total_count + limit - 1) // limit if total_count else 0
    params = dict(extra_params or {})

    def build_url(target_offset: int | None) -> str | None:
        if target_offset is None:
            return None
        query = {**params, "limit": limit, "offset": target_offset}
        return f"{path}?{urlencode(query)}"

    return {
        "limit": limit,
        "offset": offset,
        "total_count": total_count,
        "item_count": item_count,
        "start_index": offset + 1 if item_count else 0,
        "end_index": offset + item_count,
        "has_previous": has_previous,
        "has_next": has_next,
        "previous_offset": previous_offset,
        "next_offset": next_offset,
        "previous_page_url": build_url(previous_offset),
        "next_page_url": build_url(next_offset),
        "current_page": current_page,
        "total_pages": total_pages,
    }
