"""
Tests for BingWebmasterConnector.

Mirrors the style of test_reddit_ads_connector.py and test_x_ads_connector.py:
  - mock httpx.AsyncClient via monkeypatch
  - assert correct URLs, headers, and return shapes
"""

import json

import pytest

from app.connectors.bing_webmaster import BingWebmasterConnector

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_TOKEN = "ms-access-token"
_SITE_URL = "https://example.com/"

_BING_BASE = "https://ssl.bing.com/webmaster/api.svc/json"


class _FakeClientBase:
    """Async-context-manager shim for httpx.AsyncClient."""

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

        def raise_for_status(self):
            if self.status_code >= 400:
                import httpx

                raise httpx.HTTPStatusError(
                    f"HTTP {self.status_code}",
                    request=None,  # type: ignore[arg-type]
                    response=None,  # type: ignore[arg-type]
                )

        def json(self):
            return body

    return _R()


# ---------------------------------------------------------------------------
# list_sites
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_sites_sends_bearer_token(monkeypatch):
    """list_sites uses Bearer auth and calls GetUserSites."""
    captured = {}

    class FakeClient(_FakeClientBase):
        async def get(self, url, *, headers=None, params=None):
            captured["url"] = url
            captured["headers"] = headers or {}
            return _resp(200, {"d": [{"Url": _SITE_URL, "IsVerified": True}]})

    monkeypatch.setattr("app.connectors.bing_webmaster.httpx.AsyncClient", FakeClient)

    result = await BingWebmasterConnector().list_sites(_TOKEN)

    assert captured["url"] == f"{_BING_BASE}/GetUserSites"
    assert captured["headers"]["Authorization"] == f"Bearer {_TOKEN}"
    assert result == [{"Url": _SITE_URL, "IsVerified": True}]


@pytest.mark.asyncio
async def test_list_sites_returns_empty_list_on_empty_response(monkeypatch):
    """list_sites returns [] when the API returns an empty d key."""

    class FakeClient(_FakeClientBase):
        async def get(self, url, *, headers=None, params=None):
            return _resp(200, {"d": []})

    monkeypatch.setattr("app.connectors.bing_webmaster.httpx.AsyncClient", FakeClient)

    result = await BingWebmasterConnector().list_sites(_TOKEN)
    assert result == []


# ---------------------------------------------------------------------------
# get_query_stats
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_query_stats_sends_correct_params(monkeypatch):
    """get_query_stats passes siteUrl, dateFrom, dateTo, page, pageSize."""
    captured = {}
    fake_body = {"d": [{"Query": "test query", "Impressions": 100, "Clicks": 10}]}

    class FakeClient(_FakeClientBase):
        async def get(self, url, *, headers=None, params=None):
            captured["url"] = url
            captured["params"] = params or {}
            return _resp(200, fake_body)

    monkeypatch.setattr("app.connectors.bing_webmaster.httpx.AsyncClient", FakeClient)

    result = await BingWebmasterConnector().get_query_stats(
        access_token=_TOKEN,
        site_url=_SITE_URL,
        date_from="2025-01-01",
        date_to="2025-01-31",
        page=0,
        page_size=50,
    )

    assert captured["url"] == f"{_BING_BASE}/GetQueryStats"
    assert captured["params"]["siteUrl"] == _SITE_URL
    assert captured["params"]["dateFrom"] == "2025-01-01"
    assert captured["params"]["dateTo"] == "2025-01-31"
    assert captured["params"]["page"] == 0
    assert captured["params"]["pageSize"] == 50
    assert result == fake_body


@pytest.mark.asyncio
async def test_get_query_stats_omits_optional_date_params_when_none(monkeypatch):
    """get_query_stats does not include date keys when dates are None."""
    captured = {}

    class FakeClient(_FakeClientBase):
        async def get(self, url, *, headers=None, params=None):
            captured["params"] = params or {}
            return _resp(200, {})

    monkeypatch.setattr("app.connectors.bing_webmaster.httpx.AsyncClient", FakeClient)

    await BingWebmasterConnector().get_query_stats(access_token=_TOKEN, site_url=_SITE_URL)

    assert "dateFrom" not in captured["params"]
    assert "dateTo" not in captured["params"]
    assert captured["params"]["siteUrl"] == _SITE_URL


# ---------------------------------------------------------------------------
# get_crawl_stats
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_crawl_stats_sends_site_url(monkeypatch):
    """get_crawl_stats passes siteUrl to GetCrawlStats endpoint."""
    captured = {}
    fake_body = {"d": {"CrawledPages": 500}}

    class FakeClient(_FakeClientBase):
        async def get(self, url, *, headers=None, params=None):
            captured["url"] = url
            captured["params"] = params or {}
            return _resp(200, fake_body)

    monkeypatch.setattr("app.connectors.bing_webmaster.httpx.AsyncClient", FakeClient)

    result = await BingWebmasterConnector().get_crawl_stats(access_token=_TOKEN, site_url=_SITE_URL)

    assert captured["url"] == f"{_BING_BASE}/GetCrawlStats"
    assert captured["params"]["siteUrl"] == _SITE_URL
    assert result == fake_body


# ---------------------------------------------------------------------------
# get_index_coverage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_index_coverage_calls_correct_endpoint(monkeypatch):
    """get_index_coverage calls GetIndexCoverage with siteUrl."""
    captured = {}

    class FakeClient(_FakeClientBase):
        async def get(self, url, *, headers=None, params=None):
            captured["url"] = url
            captured["params"] = params or {}
            return _resp(200, {"d": {"IndexedPages": 200}})

    monkeypatch.setattr("app.connectors.bing_webmaster.httpx.AsyncClient", FakeClient)

    result = await BingWebmasterConnector().get_index_coverage(access_token=_TOKEN, site_url=_SITE_URL)

    assert captured["url"] == f"{_BING_BASE}/GetIndexCoverage"
    assert captured["params"]["siteUrl"] == _SITE_URL
    assert result["d"]["IndexedPages"] == 200


# ---------------------------------------------------------------------------
# get_link_counts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_link_counts_calls_correct_endpoint(monkeypatch):
    """get_link_counts calls GetLinkCounts with siteUrl."""
    captured = {}

    class FakeClient(_FakeClientBase):
        async def get(self, url, *, headers=None, params=None):
            captured["url"] = url
            captured["params"] = params or {}
            return _resp(200, {"d": {"InboundLinks": 42}})

    monkeypatch.setattr("app.connectors.bing_webmaster.httpx.AsyncClient", FakeClient)

    result = await BingWebmasterConnector().get_link_counts(access_token=_TOKEN, site_url=_SITE_URL)

    assert captured["url"] == f"{_BING_BASE}/GetLinkCounts"
    assert captured["params"]["siteUrl"] == _SITE_URL
    assert result["d"]["InboundLinks"] == 42
