"""Catalog, reporting, and discovery tests for the Adobe Analytics connector.

HTTP is stubbed — no real network, Postgres, or Redis.
"""

from __future__ import annotations

import json

import pytest

from app.connectors.adobe_analytics import (
    AdobeAnalyticsConnector,
    build_workspace_definition,
    coerce_project_definition,
)

_CLIENT_ID = "cid"
_CLIENT_SECRET = "csecret"
_ORG = "ABCDE@AdobeOrg"
_COMPANY = "exampleco"
_TOKEN = "adobe-access-token"
_DISCOVERY = {
    "imsUserId": "user@AdobeID",
    "imsOrgs": [
        {
            "imsOrgId": _ORG,
            "companies": [{"globalCompanyId": _COMPANY, "companyName": "Example Co"}],
        }
    ],
}


class _FakeClientBase:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


def _resp(status: int, body):
    class _R:
        status_code = status
        text = body if isinstance(body, str) else json.dumps(body)

        def json(self):
            if isinstance(body, str):
                return json.loads(body)
            return body

    return _R()


def _install_client(monkeypatch, *, get=None, post=None):
    calls: list[dict] = []

    class FakeClient(_FakeClientBase):
        async def post(self, url, **kwargs):
            if "/ims/token" in url:
                return _resp(200, {"access_token": _TOKEN, "expires_in": 3600})
            calls.append({"method": "POST", "url": url, **kwargs})
            if post is None:
                raise AssertionError(f"unexpected POST {url}")
            return post(url, **kwargs)

        async def get(self, url, **kwargs):
            if "/ims/token" in url:
                return _resp(200, {"access_token": _TOKEN, "expires_in": 3600})
            if "/discovery/me" in url:
                return _resp(200, _DISCOVERY)
            calls.append({"method": "GET", "url": url, **kwargs})
            if get is None:
                raise AssertionError(f"unexpected GET {url}")
            return get(url, **kwargs)

    monkeypatch.setattr("app.connectors.adobe_analytics.httpx.AsyncClient", FakeClient)
    return AdobeAnalyticsConnector(), calls


@pytest.mark.asyncio
async def test_resolve_company_id_uses_discovery(monkeypatch):
    conn, _ = _install_client(monkeypatch)
    result = await conn.resolve_company_id(_CLIENT_ID, _CLIENT_SECRET, _ORG)
    assert result["company_id"] == _COMPANY
    assert result["source"] == "discovery"
    cached = await conn.resolve_company_id(_CLIENT_ID, _CLIENT_SECRET, _ORG)
    assert cached["source"] == "cache"


@pytest.mark.asyncio
async def test_list_report_suites_parses_array_payload(monkeypatch):
    def get(url, **kwargs):
        assert url == f"https://analytics.adobe.io/api/{_COMPANY}/reportsuites/collections/suites"
        assert kwargs["headers"]["x-proxy-global-company-id"] == _COMPANY
        return _resp(
            200,
            [
                {"rsid": "examplersid", "name": "Example RS", "timezoneZoneinfo": "US/Pacific"},
                {"id": "othersuite", "name": "Other"},
            ],
        )

    conn, _ = _install_client(monkeypatch, get=get)
    result = await conn.list_report_suites(_CLIENT_ID, _CLIENT_SECRET, _ORG)
    assert result["total"] == 2
    assert result["report_suites"][0]["rsid"] == "examplersid"
    assert result["report_suites"][1]["rsid"] == "othersuite"
    assert result["company_id"] == _COMPANY


@pytest.mark.asyncio
async def test_get_dimensions_parses_array_payload(monkeypatch):
    def get(url, **kwargs):
        assert url == f"https://analytics.adobe.io/api/{_COMPANY}/dimensions"
        assert kwargs["params"]["rsid"] == "examplersid"
        return _resp(
            200,
            [
                {"id": "variables/page", "title": "Page", "type": "string", "category": "Traffic"},
                {"id": "variables/evar1", "name": "Campaign", "type": "string"},
            ],
        )

    conn, _ = _install_client(monkeypatch, get=get)
    result = await conn.get_dimensions(_CLIENT_ID, _CLIENT_SECRET, _ORG, "examplersid")
    assert result["total"] == 2
    assert result["dimensions"][0]["id"] == "variables/page"
    assert result["dimensions"][0]["name"] == "Page"


@pytest.mark.asyncio
async def test_run_report_sends_global_filter_and_metric_ids(monkeypatch):
    captured = {}

    def post(url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return _resp(
            200,
            {
                "rows": [{"value": "Home", "data": [12]}],
                "summaryData": {"totals": [12]},
                "columns": {"columnIds": ["0"]},
            },
        )

    conn, calls = _install_client(monkeypatch, post=post)
    result = await conn.run_report(
        _CLIENT_ID,
        _CLIENT_SECRET,
        _ORG,
        "examplersid",
        dimensions=["page"],
        metrics=["visits"],
        date_range={"start": "2026-01-01", "end": "2026-01-31"},
        limit=10,
    )
    assert captured["url"] == f"https://analytics.adobe.io/api/{_COMPANY}/reports"
    body = captured["json"]
    assert body["rsid"] == "examplersid"
    assert body["dimension"] == "variables/page"
    assert body["metricContainer"]["metrics"] == [{"columnId": "0", "id": "metrics/visits"}]
    assert body["globalFilters"] == [
        {"type": "dateRange", "dateRange": "2026-01-01T00:00:00.000/2026-01-31T00:00:00.000"}
    ]
    assert "dateRange" not in body
    assert result["row_count"] == 1
    assert result["metrics"] == ["metrics/visits"]
    assert [c["method"] for c in calls] == ["POST"]


def test_build_workspace_definition_has_adobe_shape():
    definition = build_workspace_definition(
        "examplersid",
        tables=[{"name": "Traffic", "metrics": ["visits"], "dimension": "page"}],
        date_range="thisMonth",
    )
    assert definition["version"] == "31"
    panel = definition["workspaces"][0]["panels"][0]
    assert panel["reportSuite"]["id"] == "examplersid"
    assert panel["dateRange"]["id"] == "thisMonth"
    reportlet = panel["subPanels"][0]["reportlet"]
    assert reportlet["type"] == "FreeformReportlet"
    assert reportlet["columnTree"]["nodes"][0]["component"]["id"] == "metrics/visits"
    assert reportlet["advancedSettings"]["rows"][0]["id"] == "variables/page"


def test_coerce_incomplete_definition_expands():
    built = coerce_project_definition({"version": "31"}, rsid="examplersid")
    assert built["workspaces"][0]["panels"]
    full = {"version": "31", "workspaces": [{"id": "ws", "panels": []}]}
    assert coerce_project_definition(full, rsid="examplersid") is full
