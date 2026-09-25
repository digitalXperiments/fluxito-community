"""Tests for the Branch connector."""

import json

import pytest

from app.connectors.branch import BranchConnector

_API_KEY = "branch-api-key"
_SECRET_KEY = "branch-secret-key"
_EXPORT_DATE = "2025-01-15"


class _FakeClientBase:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


def _resp(status: int, body: dict):
    _text = json.dumps(body)

    class _R:
        status_code = status
        text = _text

        def json(self):
            return body

    return _R()


@pytest.mark.asyncio
async def test_get_app_passes_branch_key_in_path_and_secret_as_query_param(monkeypatch):
    captured = {}

    class FakeClient(_FakeClientBase):
        async def get(self, url, *, headers=None, params=None):
            captured["url"] = url
            captured["headers"] = headers or {}
            captured["params"] = params or {}
            return _resp(200, {"app_id": "123", "app_name": "Test App"})

    monkeypatch.setattr("app.connectors.branch.httpx.AsyncClient", FakeClient)

    await BranchConnector().get_app(_API_KEY, _SECRET_KEY)

    assert "https://api2.branch.io/v1/app/" in captured["url"]
    assert _API_KEY in captured["url"]
    assert captured["params"].get("branch_secret") == _SECRET_KEY


@pytest.mark.asyncio
async def test_get_app_normalizes_response(monkeypatch):
    captured = {}

    class FakeClient(_FakeClientBase):
        async def get(self, url, *, headers=None, params=None):
            captured["url"] = url
            return _resp(200, {"app_id": "abc123", "app_name": "My Branch App", "platform": "ios"})

    monkeypatch.setattr("app.connectors.branch.httpx.AsyncClient", FakeClient)

    result = await BranchConnector().get_app(_API_KEY, _SECRET_KEY)

    assert "app" in result
    assert result["app"]["app_id"] == "abc123"
    assert result["app"]["app_name"] == "My Branch App"


@pytest.mark.asyncio
async def test_request_daily_export_posts_json_body_with_credentials_and_date(monkeypatch):
    captured = {}

    class FakeClient(_FakeClientBase):
        async def post(self, url, *, headers=None, params=None, json=None):
            captured["url"] = url
            captured["json"] = json or {}
            captured["headers"] = headers or {}
            return _resp(
                200, {"open": "https://s3.../open.csv.gz", "install": "https://s3.../install.csv.gz"}
            )

    monkeypatch.setattr("app.connectors.branch.httpx.AsyncClient", FakeClient)

    result = await BranchConnector().request_daily_export(_API_KEY, _SECRET_KEY, _EXPORT_DATE)

    assert captured["url"] == "https://api2.branch.io/v3/export"
    assert captured["json"]["branch_key"] == _API_KEY
    assert captured["json"]["branch_secret"] == _SECRET_KEY
    assert captured["json"]["export_date"] == _EXPORT_DATE
    assert result["success"] is True
    assert result["export_date"] == _EXPORT_DATE


@pytest.mark.asyncio
async def test_api_error_returns_error_dict(monkeypatch):
    captured = {}

    class FakeClient(_FakeClientBase):
        async def get(self, url, *, headers=None, params=None):
            captured["url"] = url
            return _resp(401, {"error": "unauthorized"})

    monkeypatch.setattr("app.connectors.branch.httpx.AsyncClient", FakeClient)

    result = await BranchConnector().get_app(_API_KEY, _SECRET_KEY)

    assert result["error"] is True
    assert result["status_code"] == 401


@pytest.mark.asyncio
async def test_query_analytics_posts_json_body_with_filters(monkeypatch):
    captured = {}

    class FakeClient(_FakeClientBase):
        async def post(self, url, *, headers=None, params=None, json=None):
            captured["url"] = url
            captured["json"] = json or {}
            return _resp(200, [{"timestamp": "2025-01-01", "count": 150}])

    monkeypatch.setattr("app.connectors.branch.httpx.AsyncClient", FakeClient)

    result = await BranchConnector().query_analytics(
        _API_KEY,
        _SECRET_KEY,
        start_date="2025-01-01",
        end_date="2025-01-07",
        data_source="eo_click",
    )

    assert captured["url"] == "https://api.branch.io/v1/query/analytics"
    assert captured["json"]["branch_key"] == _API_KEY
    assert captured["json"]["branch_secret"] == _SECRET_KEY
    assert captured["json"]["start_date"] == "2025-01-01"
    assert captured["json"]["end_date"] == "2025-01-07"
    assert result["success"] is True
    assert len(result["results"]) == 1
