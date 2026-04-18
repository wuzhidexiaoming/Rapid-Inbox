from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.dns_check import DnsCheckService


@pytest.mark.asyncio
async def test_dns_check_service_returns_sorted_mx_records(monkeypatch) -> None:
    def fake_resolve(root_domain: str, record_type: str):
        assert root_domain == "adb.com"
        assert record_type == "MX"
        return [
            SimpleNamespace(exchange="mx2.example."),
            SimpleNamespace(exchange="mx1.example."),
        ]

    monkeypatch.setattr("app.services.dns_check.dns.resolver.resolve", fake_resolve)

    result = await DnsCheckService().run_dns_check("adb.com")

    assert result["status"] == "ok"
    assert result["mx_records"] == ["mx1.example", "mx2.example"]
