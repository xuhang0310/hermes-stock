from __future__ import annotations

import json

import httpx

from backend.app.ai.ai_api import DataSourceConfigIn, update_data_source
from backend.app.core.api_keys import (
    TAVILY_POOL_PROVIDER,
    merge_api_keys,
    serialize_api_keys,
    split_api_keys,
)
from backend.app.core.models import DataSourceConfig
from backend.app.market import stock_tools


def test_api_key_pool_parsing_is_ordered_and_deduplicated() -> None:
    assert split_api_keys("primary\nbackup, primary;third") == (
        "primary",
        "backup",
        "third",
    )
    assert merge_api_keys("primary", ("backup", "primary")) == ("primary", "backup")
    assert serialize_api_keys(("primary", "backup")) == "primary\nbackup"


def test_data_source_key_pool_combines_database_and_environment(monkeypatch) -> None:
    rows = {
        "tavily": {"status": "enabled", "api_key": "database-primary"},
        TAVILY_POOL_PROVIDER: {"status": "enabled", "api_key": "database-backup"},
    }
    monkeypatch.setattr(stock_tools, "_data_source_row", rows.get)

    assert stock_tools._data_source_api_keys(
        "tavily",
        ("environment-backup", "database-primary"),
    ) == ("database-primary", "database-backup", "environment-backup")


def test_tavily_switches_key_on_quota_error_and_keeps_working_key(monkeypatch) -> None:
    real_client = httpx.Client
    seen_keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        api_key = json.loads(request.content.decode("utf-8"))["api_key"]
        seen_keys.append(api_key)
        if api_key == "exhausted-key":
            return httpx.Response(429, json={"detail": "quota exhausted"})
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "result",
                        "url": "https://example.com/result",
                        "content": "content",
                        "score": 0.9,
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        stock_tools.httpx,
        "Client",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )
    monkeypatch.setattr(stock_tools, "_TAVILY_ACTIVE_KEY_INDEX", 0)

    first = stock_tools._tavily_search(
        "https://api.tavily.com/search",
        ("exhausted-key", "working-key"),
        "A股",
        3,
    )
    second = stock_tools._tavily_search(
        "https://api.tavily.com/search",
        ("exhausted-key", "working-key"),
        "A股",
        3,
    )

    assert first == second
    assert seen_keys == ["exhausted-key", "working-key", "working-key"]


def test_data_source_update_appends_tavily_key_without_replacing_existing() -> None:
    row = DataSourceConfig(
        provider="tavily",
        display_name="Tavily Search",
        provider_type="search",
        base_url="https://api.tavily.com/search",
        api_key="primary-key",
        status="enabled",
    )
    rows = {"tavily": row}

    class FakeDb:
        def get(self, _model, key):
            return rows.get(key)

        def add(self, added_row) -> None:
            rows[added_row.provider] = added_row

        def commit(self) -> None:
            pass

        def refresh(self, _row) -> None:
            pass

    payload = DataSourceConfigIn(
        display_name="Tavily Search",
        provider_type="search",
        base_url="https://api.tavily.com/search",
        additional_api_key="backup-key",
        status="enabled",
    )

    update_data_source("tavily", payload, FakeDb(), None)  # type: ignore[arg-type]

    assert split_api_keys(row.api_key) == ("primary-key",)
    assert split_api_keys(rows[TAVILY_POOL_PROVIDER].api_key) == ("backup-key",)
