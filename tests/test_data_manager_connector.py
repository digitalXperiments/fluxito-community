"""Tests for the Google Data Manager REST connector."""

from __future__ import annotations

import json

import pytest

from app.connectors.data_manager import DataManagerConnector


class _FakeClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


def _resp(status: int, body: dict | None, *, empty: bool = False):
    body_text = "" if empty else json.dumps(body or {})

    class _R:
        status_code = status
        content = b"" if empty else body_text.encode()
        text = body_text

        def json(self):
            return body or {}

    return _R()


@pytest.mark.asyncio
async def test_list_audiences_quotes_parent(monkeypatch):
    captured: dict = {}

    class FakeClient(_FakeClient):
        async def request(self, method, url, *, headers=None, params=None, json=None):
            captured.update(method=method, url=url, params=params, json=json, headers=headers)
            return _resp(200, {"userLists": []})

    monkeypatch.setattr("app.connectors.data_manager.httpx.AsyncClient", FakeClient)

    connector = DataManagerConnector(token_manager=None)  # type: ignore[arg-type]
    monkeypatch.setattr(connector, "get_token", lambda _cid: _async_token())
    result = await connector.list_audiences(
        "conn",
        "accountTypes/GOOGLE_ADS/accounts/123",
        page_size=50,
        filter_expression='displayName="Buyers"',
    )
    assert result == {"userLists": []}
    assert captured["method"] == "GET"
    assert captured["url"].endswith(
        "https://datamanager.googleapis.com/v1/accountTypes/GOOGLE_ADS/accounts/123/userLists"
    )
    assert captured["params"]["pageSize"] == 50
    assert captured["params"]["filter"] == 'displayName="Buyers"'


async def _async_token() -> str:
    return "token"


@pytest.mark.asyncio
async def test_ingest_posts_audience_members_path(monkeypatch):
    captured: dict = {}

    class FakeClient(_FakeClient):
        async def request(self, method, url, *, headers=None, params=None, json=None):
            captured.update(method=method, url=url, json=json)
            return _resp(200, {"requestId": "req-1"})

    monkeypatch.setattr("app.connectors.data_manager.httpx.AsyncClient", FakeClient)
    connector = DataManagerConnector(token_manager=None)  # type: ignore[arg-type]
    monkeypatch.setattr(connector, "get_token", lambda _cid: _async_token())
    result = await connector.ingest_audience_members(
        "conn",
        {"audienceMembers": [], "destinations": [{"operatingAccount": {}}]},
    )
    assert result["requestId"] == "req-1"
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/audienceMembers:ingest")


@pytest.mark.asyncio
async def test_delete_empty_body(monkeypatch):
    class FakeClient(_FakeClient):
        async def request(self, method, url, *, headers=None, params=None, json=None):
            return _resp(200, None, empty=True)

    monkeypatch.setattr("app.connectors.data_manager.httpx.AsyncClient", FakeClient)
    connector = DataManagerConnector(token_manager=None)  # type: ignore[arg-type]
    monkeypatch.setattr(connector, "get_token", lambda _cid: _async_token())
    result = await connector.delete_audience("conn", "accountTypes/GOOGLE_ADS/accounts/123/userLists/9")
    assert result == {}
