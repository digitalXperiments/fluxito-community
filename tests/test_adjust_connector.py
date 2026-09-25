"""Tests for the Adjust connector."""

import json

import pytest

from app.connectors.adjust import AdjustConnector

_API_KEY = "adjust-api-token"
_APP_TOKEN = "abc123xyz"
_DIMENSIONS = "app,tracker"
_METRICS = "installs,clicks"
_DATE_PERIOD = "2025-01-01:2025-01-31"


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
async def test_list_apps_uses_filters_data_endpoint(monkeypatch):
    captured = {}

    class FakeClient(_FakeClientBase):
        async def get(self, url, *, headers=None, params=None):
            captured["url"] = url
            captured["headers"] = headers or {}
            captured["params"] = params or {}
            return _resp(200, {"apps": [{"id": "1", "name": "Test App"}]})

    monkeypatch.setattr("app.connectors.adjust.httpx.AsyncClient", FakeClient)

    await AdjustConnector().list_apps(_API_KEY)

    assert captured["url"] == "https://automate.adjust.com/reports-service/filters_data"
    assert captured["params"].get("required_filters") == "apps"
    assert captured["headers"]["Authorization"] == f"Bearer {_API_KEY}"


@pytest.mark.asyncio
async def test_get_report_passes_dimensions_metrics_as_query_params(monkeypatch):
    captured = {}

    class FakeClient(_FakeClientBase):
        async def get(self, url, *, headers=None, params=None):
            captured["url"] = url
            captured["params"] = params or {}
            return _resp(200, {"rows": [], "totals": {}})

    monkeypatch.setattr("app.connectors.adjust.httpx.AsyncClient", FakeClient)

    await AdjustConnector().get_report(_API_KEY, _DIMENSIONS, _METRICS, _DATE_PERIOD)

    assert captured["url"] == "https://automate.adjust.com/reports-service/report"
    assert captured["params"].get("dimensions") == _DIMENSIONS
    assert captured["params"].get("metrics") == _METRICS
    assert captured["params"].get("date_period") == _DATE_PERIOD


@pytest.mark.asyncio
async def test_get_pivot_report_uses_pivot_endpoint(monkeypatch):
    captured = {}

    class FakeClient(_FakeClientBase):
        async def get(self, url, *, headers=None, params=None):
            captured["url"] = url
            captured["params"] = params or {}
            return _resp(200, {"rows": [], "totals": {}})

    monkeypatch.setattr("app.connectors.adjust.httpx.AsyncClient", FakeClient)

    await AdjustConnector().get_pivot_report(_API_KEY, _DIMENSIONS, _METRICS, _DATE_PERIOD, "tracker")

    assert captured["url"] == "https://automate.adjust.com/reports-service/pivot_report"
    assert captured["params"].get("index") == "tracker"


@pytest.mark.asyncio
async def test_list_events_uses_events_endpoint(monkeypatch):
    captured = {}

    class FakeClient(_FakeClientBase):
        async def get(self, url, *, headers=None, params=None):
            captured["url"] = url
            return _resp(200, {"events": [{"name": "purchase"}]})

    monkeypatch.setattr("app.connectors.adjust.httpx.AsyncClient", FakeClient)

    result = await AdjustConnector().list_events(_API_KEY)

    assert captured["url"] == "https://automate.adjust.com/reports-service/events"
    assert result["total"] == 1


@pytest.mark.asyncio
async def test_get_partner_links_uses_campaign_api_with_token_auth(monkeypatch):
    captured = {}

    class FakeClient(_FakeClientBase):
        async def get(self, url, *, headers=None, params=None):
            captured["url"] = url
            captured["headers"] = headers or {}
            return _resp(200, {"trackers": [{"name": "organic"}]})

    monkeypatch.setattr("app.connectors.adjust.httpx.AsyncClient", FakeClient)

    result = await AdjustConnector().get_partner_links(_API_KEY, _APP_TOKEN)

    # Must use api.adjust.com base and Token token= auth
    assert captured["url"].startswith("https://api.adjust.com/public/v2/apps/")
    assert _APP_TOKEN in captured["url"]
    assert "trackers" in captured["url"]
    assert captured["headers"]["Authorization"] == f"Token token={_API_KEY}"
    assert result["total"] == 1


@pytest.mark.asyncio
async def test_api_error_returns_error_dict(monkeypatch):
    captured = {}

    class FakeClient(_FakeClientBase):
        async def get(self, url, *, headers=None, params=None):
            captured["url"] = url
            return _resp(500, {})

    monkeypatch.setattr("app.connectors.adjust.httpx.AsyncClient", FakeClient)

    result = await AdjustConnector().list_apps(_API_KEY)

    assert result["error"] is True
    assert result["status_code"] == 500
