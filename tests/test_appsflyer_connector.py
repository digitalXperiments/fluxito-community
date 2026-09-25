"""Tests for the AppsFlyer connector."""

import json

import pytest

from app.connectors.appsflyer import AppsFlyerConnector

_API_KEY = "appsflyer-v2-token"
_APP_ID = "com.example.app"
_START_DATE = "2025-01-01"
_END_DATE = "2025-01-31"


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
async def test_list_apps_uses_correct_endpoint_and_bearer_auth(monkeypatch):
    captured = {}

    class FakeClient(_FakeClientBase):
        async def get(self, url, *, headers=None, params=None):
            captured["url"] = url
            captured["headers"] = headers or {}
            captured["params"] = params or {}
            return _resp(200, {"apps": [{"app_id": "1", "app_name": "Test App"}]})

    monkeypatch.setattr("app.connectors.appsflyer.httpx.AsyncClient", FakeClient)

    await AppsFlyerConnector().list_apps(_API_KEY)

    assert captured["url"] == "https://hq1.appsflyer.com/api/mng/apps"
    assert captured["headers"]["Authorization"] == f"Bearer {_API_KEY}"
    # No capabilities filter — list_apps returns all apps


@pytest.mark.asyncio
async def test_get_installs_report_uses_correct_path_with_raw_data_export(monkeypatch):
    captured = {}

    class FakeClient(_FakeClientBase):
        async def get(self, url, *, headers=None, params=None):
            captured["url"] = url
            captured["params"] = params or {}
            return _resp(200, {"results": []})

    monkeypatch.setattr("app.connectors.appsflyer.httpx.AsyncClient", FakeClient)

    await AppsFlyerConnector().get_installs_report(_API_KEY, _APP_ID, _START_DATE, _END_DATE)

    assert "/raw-data/export/app/" in captured["url"]
    assert "installs_report/v5" in captured["url"]
    assert _APP_ID in captured["url"]
    assert captured["params"].get("from") == _START_DATE
    assert captured["params"].get("to") == _END_DATE


@pytest.mark.asyncio
async def test_get_in_app_events_report_uses_underscore_endpoint(monkeypatch):
    captured = {}

    class FakeClient(_FakeClientBase):
        async def get(self, url, *, headers=None, params=None):
            captured["url"] = url
            captured["params"] = params or {}
            return _resp(200, {"results": []})

    monkeypatch.setattr("app.connectors.appsflyer.httpx.AsyncClient", FakeClient)

    await AppsFlyerConnector().get_in_app_events_report(_API_KEY, _APP_ID, _START_DATE, _END_DATE)

    # Must contain underscore, not hyphen
    assert "in_app_events_report" in captured["url"]
    assert "in-app-events-report" not in captured["url"]


@pytest.mark.asyncio
async def test_get_partners_report_uses_different_base_host(monkeypatch):
    captured = {}

    class FakeClient(_FakeClientBase):
        async def get(self, url, *, headers=None, params=None):
            captured["url"] = url
            captured["params"] = params or {}
            return _resp(200, {"results": []})

    monkeypatch.setattr("app.connectors.appsflyer.httpx.AsyncClient", FakeClient)

    await AppsFlyerConnector().get_partners_report(_API_KEY, _APP_ID, _START_DATE, _END_DATE)

    # Must start with the non-hq1 host per docs
    assert captured["url"].startswith("https://hq.appsflyer.com/export/")
    assert _APP_ID in captured["url"]
    assert "partners_report/v5" in captured["url"]


@pytest.mark.asyncio
async def test_csv_response_is_parsed_as_results(monkeypatch):
    """AppsFlyer raw-data endpoints return CSV, not JSON."""
    captured = {}
    csv_body = "date,installs,media_source,campaign\n2025-01-01,100,facebook,campaign_a\n2025-01-02,200,google,campaign_b\n"

    class FakeClient(_FakeClientBase):
        async def get(self, url, *, headers=None, params=None):
            captured["url"] = url

            class _R:
                status_code = 200
                text = csv_body

                def json(self):
                    raise ValueError("Not JSON")

            return _R()

    monkeypatch.setattr("app.connectors.appsflyer.httpx.AsyncClient", FakeClient)

    result = await AppsFlyerConnector().get_installs_report(_API_KEY, _APP_ID, _START_DATE, _END_DATE)

    assert result["total"] == 2
    assert result["installs"][0]["date"] == "2025-01-01"
    assert result["installs"][0]["installs"] == "100"
    assert result["installs"][0]["media_source"] == "facebook"
    assert result["installs"][1]["campaign"] == "campaign_b"


@pytest.mark.asyncio
async def test_api_error_returns_error_dict(monkeypatch):
    captured = {}

    class FakeClient(_FakeClientBase):
        async def get(self, url, *, headers=None, params=None):
            captured["url"] = url
            return _resp(500, {})

    monkeypatch.setattr("app.connectors.appsflyer.httpx.AsyncClient", FakeClient)

    result = await AppsFlyerConnector().list_apps(_API_KEY)

    assert result["error"] is True
    assert result["status_code"] == 500
