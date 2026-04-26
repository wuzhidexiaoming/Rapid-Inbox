from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlencode


DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
PAGE_WINDOW_RADIUS = 2


def _build_page_items(
    *,
    current_page: int,
    total_pages: int,
    limit: int,
    build_url: Callable[[int], str | None],
) -> list[dict[str, Any]]:
    if total_pages <= 0:
        return []

    window_start = max(current_page - PAGE_WINDOW_RADIUS, 1)
    window_end = min(current_page + PAGE_WINDOW_RADIUS, total_pages)
    visible_pages = {1, total_pages, *range(window_start, window_end + 1)}

    items: list[dict[str, Any]] = []
    previous_page: int | None = None
    for page in sorted(visible_pages):
        if previous_page is not None and page - previous_page > 1:
            items.append({"type": "ellipsis"})
        target_offset = (page - 1) * limit
        items.append(
            {
                "type": "page",
                "page": page,
                "offset": target_offset,
                "url": build_url(target_offset),
                "is_current": page == current_page,
            }
        )
        previous_page = page
    return items


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

    first_offset = 0 if total_pages else None
    last_offset = (total_pages - 1) * limit if total_pages else None

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
        "first_offset": first_offset,
        "last_offset": last_offset,
        "previous_page_url": build_url(previous_offset),
        "next_page_url": build_url(next_offset),
        "first_page_url": build_url(first_offset),
        "last_page_url": build_url(last_offset),
        "current_page": current_page,
        "total_pages": total_pages,
        "page_items": _build_page_items(
            current_page=current_page,
            total_pages=total_pages,
            limit=limit,
            build_url=build_url,
        ),
    }
