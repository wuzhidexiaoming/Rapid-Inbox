from app.http.pagination import build_pagination_context


def test_build_pagination_context_exposes_clickable_page_window() -> None:
    context = build_pagination_context(
        path="/admin/messages",
        limit=10,
        offset=40,
        total_count=100,
        item_count=10,
        extra_params={"kind": "recent"},
    )

    page_labels = [
        item["page"] if item["type"] == "page" else "ellipsis"
        for item in context["page_items"]
    ]
    page_urls = {
        item["page"]: item["url"]
        for item in context["page_items"]
        if item["type"] == "page"
    }

    assert context["current_page"] == 5
    assert context["total_pages"] == 10
    assert context["first_page_url"] == "/admin/messages?kind=recent&limit=10&offset=0"
    assert context["last_page_url"] == "/admin/messages?kind=recent&limit=10&offset=90"
    assert page_labels == [1, "ellipsis", 3, 4, 5, 6, 7, "ellipsis", 10]
    assert page_urls[7] == "/admin/messages?kind=recent&limit=10&offset=60"
    assert next(item for item in context["page_items"] if item.get("page") == 5)["is_current"] is True


def test_build_pagination_context_has_no_page_items_for_empty_results() -> None:
    context = build_pagination_context(
        path="/admin/messages",
        limit=20,
        offset=0,
        total_count=0,
        item_count=0,
    )

    assert context["current_page"] == 0
    assert context["total_pages"] == 0
    assert context["first_page_url"] is None
    assert context["last_page_url"] is None
    assert context["page_items"] == []
