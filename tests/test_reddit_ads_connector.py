import json

import pytest

from app.connectors.reddit_ads import RedditAdsConnector

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_TOKEN = "reddit-access-token"
_ACCOUNT_ID = "a2_example"
_CAMPAIGN_ID = "t2_camp1"
_ADGROUP_ID = "t2_adg1"


class _FakeClientBase:
    """Base async-context-manager shim for httpx.AsyncClient."""

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


# ---------------------------------------------------------------------------
# list_ad_accounts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_ad_accounts_sends_bearer_token_and_user_agent(monkeypatch):
    """Reddit Ads API requests use OAuth2 bearer auth and a descriptive user agent."""
    captured = {}

    class FakeResponse:
        status_code = 200
        text = '{"data":[]}'

        def json(self):
            return {
                "data": [
                    {
                        "id": "a2_example",
                        "name": "Main Reddit Account",
                        "currency": "USD",
                        "configured_status": "ACTIVE",
                    }
                ]
            }

    class FakeClient(_FakeClientBase):
        async def get(self, url, *, headers=None, params=None):
            captured["url"] = url
            captured["headers"] = headers or {}
            captured["params"] = params or {}
            return FakeResponse()

    monkeypatch.setattr("app.connectors.reddit_ads.httpx.AsyncClient", FakeClient)

    result = await RedditAdsConnector().list_ad_accounts(_TOKEN)

    assert captured["url"] == "https://ads-api.reddit.com/api/v3/ad_accounts"
    assert captured["headers"]["Authorization"] == "Bearer reddit-access-token"
    assert captured["headers"]["User-Agent"].startswith("Fluxito:")
    assert result["accounts"] == [
        {
            "account_id": "a2_example",
            "name": "Main Reddit Account",
            "currency": "USD",
            "status": "ACTIVE",
        }
    ]


# ---------------------------------------------------------------------------
# get_campaign_performance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_campaign_performance_returns_expected_shape(monkeypatch):
    """get_campaign_performance merges campaigns list with per-campaign stats."""
    campaigns_body = {"data": [{"id": _CAMPAIGN_ID, "name": "Test Campaign", "configured_status": "ACTIVE"}]}
    stats_body = {
        "data": {
            "impressions": 1000,
            "clicks": 50,
            "spend_micro_usd": 10_000_000,  # $10.00
            "total_conversions": 5,
        }
    }

    calls: list[str] = []

    class FakeClient(_FakeClientBase):
        async def get(self, url, *, headers=None, params=None):
            calls.append(url)
            if url.endswith("/campaigns"):
                return _resp(200, campaigns_body)
            if "/stats" in url:
                return _resp(200, stats_body)
            return _resp(404, {})

    monkeypatch.setattr("app.connectors.reddit_ads.httpx.AsyncClient", FakeClient)

    result = await RedditAdsConnector().get_campaign_performance(
        _TOKEN, _ACCOUNT_ID, "2025-01-01", "2025-01-31"
    )

    assert result["account_id"] == _ACCOUNT_ID
    assert result["date_range"] == "2025-01-01 to 2025-01-31"
    assert len(result["campaigns"]) == 1

    c = result["campaigns"][0]
    assert c["campaign_id"] == _CAMPAIGN_ID
    assert c["campaign_name"] == "Test Campaign"
    assert c["status"] == "ACTIVE"
    assert c["impressions"] == 1000
    assert c["clicks"] == 50
    assert c["spend"] == pytest.approx(10.0)
    assert c["conversions"] == 5

    # Verify both the campaigns list and stats endpoint were called
    assert any("/campaigns" in u and "/stats" not in u for u in calls)
    assert any("/stats" in u for u in calls)


@pytest.mark.asyncio
async def test_get_campaign_performance_invalid_dates(monkeypatch):
    """get_campaign_performance rejects bad date ranges."""
    result = await RedditAdsConnector().get_campaign_performance(
        _TOKEN, _ACCOUNT_ID, "2025-31-01", "2025-01-01"
    )
    assert result.get("error") is True
    assert "date" in result["message"].lower()


@pytest.mark.asyncio
async def test_get_campaign_performance_stats_failure_still_returns_zeros(monkeypatch):
    """Stats endpoint failure falls back to zero metrics gracefully."""
    campaigns_body = {"data": [{"id": _CAMPAIGN_ID, "name": "Test Campaign", "configured_status": "ACTIVE"}]}

    class FakeClient(_FakeClientBase):
        async def get(self, url, *, headers=None, params=None):
            if url.endswith("/campaigns"):
                return _resp(200, campaigns_body)
            return _resp(500, {"error": "stats unavailable"})

    monkeypatch.setattr("app.connectors.reddit_ads.httpx.AsyncClient", FakeClient)

    result = await RedditAdsConnector().get_campaign_performance(
        _TOKEN, _ACCOUNT_ID, "2025-01-01", "2025-01-31"
    )

    assert len(result["campaigns"]) == 1
    c = result["campaigns"][0]
    assert c["impressions"] == 0
    assert c["clicks"] == 0
    assert c["spend"] == 0
    assert c["conversions"] == 0


# ---------------------------------------------------------------------------
# get_adgroup_performance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_adgroup_performance_returns_expected_shape(monkeypatch):
    """get_adgroup_performance merges ad groups list with per-adgroup stats."""
    adgroups_body = {
        "data": [
            {
                "id": _ADGROUP_ID,
                "name": "Test Ad Group",
                "campaign_id": _CAMPAIGN_ID,
                "configured_status": "ACTIVE",
            }
        ]
    }
    stats_body = {
        "data": {
            "impressions": 500,
            "clicks": 25,
            "spend_micro_usd": 5_000_000,  # $5.00
            "total_conversions": 2,
        }
    }

    class FakeClient(_FakeClientBase):
        async def get(self, url, *, headers=None, params=None):
            if url.endswith("/ad_groups"):
                return _resp(200, adgroups_body)
            if "/stats" in url:
                return _resp(200, stats_body)
            return _resp(404, {})

    monkeypatch.setattr("app.connectors.reddit_ads.httpx.AsyncClient", FakeClient)

    result = await RedditAdsConnector().get_adgroup_performance(
        _TOKEN, _ACCOUNT_ID, "2025-01-01", "2025-01-31", campaign_id=_CAMPAIGN_ID
    )

    assert result["account_id"] == _ACCOUNT_ID
    assert result["date_range"] == "2025-01-01 to 2025-01-31"
    assert len(result["ad_groups"]) == 1

    ag = result["ad_groups"][0]
    assert ag["adgroup_id"] == _ADGROUP_ID
    assert ag["name"] == "Test Ad Group"
    assert ag["campaign_id"] == _CAMPAIGN_ID
    assert ag["status"] == "ACTIVE"
    assert ag["impressions"] == 500
    assert ag["clicks"] == 25
    assert ag["spend"] == pytest.approx(5.0)
    assert ag["conversions"] == 2


@pytest.mark.asyncio
async def test_get_adgroup_performance_invalid_dates(monkeypatch):
    """get_adgroup_performance rejects bad date ranges."""
    result = await RedditAdsConnector().get_adgroup_performance(
        _TOKEN, _ACCOUNT_ID, "2025-13-01", "2025-01-01"
    )
    assert result.get("error") is True


# ---------------------------------------------------------------------------
# audit_tracking_setup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_tracking_setup_active_pixel_scores_high(monkeypatch):
    """audit_tracking_setup returns a high score when an active verified pixel exists."""
    pixels_body = {
        "data": [
            {
                "id": "px_001",
                "name": "Main Pixel",
                "status": "ACTIVE",
                "pixel_js_status": "VERIFIED",
            }
        ]
    }

    class FakeClient(_FakeClientBase):
        async def get(self, url, *, headers=None, params=None):
            return _resp(200, pixels_body)

    monkeypatch.setattr("app.connectors.reddit_ads.httpx.AsyncClient", FakeClient)

    result = await RedditAdsConnector().audit_tracking_setup(_TOKEN, _ACCOUNT_ID)

    assert result["score"] == 100
    assert result["tag_count"] == 1
    assert result["issues"] == []
    assert result["pixels"][0]["id"] == "px_001"


@pytest.mark.asyncio
async def test_audit_tracking_setup_no_pixels_scores_zero(monkeypatch):
    """audit_tracking_setup returns score=0 and a critical issue when no pixels exist."""

    class FakeClient(_FakeClientBase):
        async def get(self, url, *, headers=None, params=None):
            return _resp(200, {"data": []})

    monkeypatch.setattr("app.connectors.reddit_ads.httpx.AsyncClient", FakeClient)

    result = await RedditAdsConnector().audit_tracking_setup(_TOKEN, _ACCOUNT_ID)

    assert result["score"] == 0
    assert result["tag_count"] == 0
    assert len(result["issues"]) >= 1
    assert result["issues"][0]["severity"] == "critical"


@pytest.mark.asyncio
async def test_audit_tracking_setup_404_returns_graceful_fallback(monkeypatch):
    """audit_tracking_setup returns score=50 and a warning on 404 (matching x_ads behaviour)."""

    class FakeClient(_FakeClientBase):
        async def get(self, url, *, headers=None, params=None):
            return _resp(404, {})

    monkeypatch.setattr("app.connectors.reddit_ads.httpx.AsyncClient", FakeClient)

    result = await RedditAdsConnector().audit_tracking_setup(_TOKEN, _ACCOUNT_ID)

    assert result["score"] == 50
    assert result["pixels"] == []
    assert len(result["issues"]) == 1
    assert result["issues"][0]["severity"] == "warning"


# ---------------------------------------------------------------------------
# update_campaign_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_campaign_status_sends_correct_payload(monkeypatch):
    """update_campaign_status PATCHes configured_status and returns confirmation dict."""
    captured = {}

    class FakeClient(_FakeClientBase):
        async def patch(self, url, *, headers=None, json=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers or {}
            return _resp(200, {"data": {"id": _CAMPAIGN_ID, "configured_status": "PAUSED"}})

    monkeypatch.setattr("app.connectors.reddit_ads.httpx.AsyncClient", FakeClient)

    result = await RedditAdsConnector().update_campaign_status(_TOKEN, _ACCOUNT_ID, _CAMPAIGN_ID, "paused")

    assert result == {
        "campaign_id": _CAMPAIGN_ID,
        "account_id": _ACCOUNT_ID,
        "new_status": "PAUSED",
        "updated": True,
    }
    assert captured["json"] == {"configured_status": "PAUSED"}
    expected_url = f"https://ads-api.reddit.com/api/v3/ad_accounts/{_ACCOUNT_ID}/campaigns/{_CAMPAIGN_ID}"
    assert captured["url"] == expected_url
    assert captured["headers"]["Authorization"] == f"Bearer {_TOKEN}"


@pytest.mark.asyncio
async def test_update_campaign_status_rejects_invalid_status(monkeypatch):
    """update_campaign_status returns an error dict for an unsupported status value."""
    result = await RedditAdsConnector().update_campaign_status(_TOKEN, _ACCOUNT_ID, _CAMPAIGN_ID, "DELETED")
    assert result.get("error") is True
    assert "ACTIVE or PAUSED" in result["message"]


@pytest.mark.asyncio
async def test_update_campaign_status_active(monkeypatch):
    """update_campaign_status accepts ACTIVE as well as PAUSED."""

    class FakeClient(_FakeClientBase):
        async def patch(self, url, *, headers=None, json=None):
            return _resp(200, {})

    monkeypatch.setattr("app.connectors.reddit_ads.httpx.AsyncClient", FakeClient)

    result = await RedditAdsConnector().update_campaign_status(_TOKEN, _ACCOUNT_ID, _CAMPAIGN_ID, "ACTIVE")
    assert result["new_status"] == "ACTIVE"
    assert result["updated"] is True


# ---------------------------------------------------------------------------
# update_campaign_budget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_campaign_budget_converts_to_micro_usd(monkeypatch):
    """update_campaign_budget converts USD to micro-USD before sending."""
    captured = {}

    class FakeClient(_FakeClientBase):
        async def patch(self, url, *, headers=None, json=None):
            captured["json"] = json
            return _resp(200, {})

    monkeypatch.setattr("app.connectors.reddit_ads.httpx.AsyncClient", FakeClient)

    result = await RedditAdsConnector().update_campaign_budget(
        _TOKEN, _ACCOUNT_ID, _CAMPAIGN_ID, daily_budget=50.0
    )

    assert result == {
        "campaign_id": _CAMPAIGN_ID,
        "account_id": _ACCOUNT_ID,
        "new_daily_budget": 50.0,
        "updated": True,
    }
    # 50 USD * 1_000_000 = 50_000_000 micro-USD
    assert captured["json"] == {"daily_budget_micro_usd": 50_000_000}


@pytest.mark.asyncio
async def test_update_campaign_budget_rejects_zero_or_negative(monkeypatch):
    """update_campaign_budget rejects non-positive budget values."""
    result = await RedditAdsConnector().update_campaign_budget(
        _TOKEN, _ACCOUNT_ID, _CAMPAIGN_ID, daily_budget=0
    )
    assert result.get("error") is True

    result2 = await RedditAdsConnector().update_campaign_budget(
        _TOKEN, _ACCOUNT_ID, _CAMPAIGN_ID, daily_budget=-10.0
    )
    assert result2.get("error") is True


# ---------------------------------------------------------------------------
# create_campaign
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_campaign_sends_correct_payload(monkeypatch):
    captured = {}

    class FakeClient(_FakeClientBase):
        async def post(self, url, *, headers=None, json=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers or {}
            return _resp(201, {"data": {"id": _CAMPAIGN_ID}})

    monkeypatch.setattr("app.connectors.reddit_ads.httpx.AsyncClient", FakeClient)

    result = await RedditAdsConnector().create_campaign(
        access_token=_TOKEN,
        account_id=_ACCOUNT_ID,
        name="Test Campaign",
        daily_budget=50.0,
        configured_status="PAUSED",
    )

    assert result["campaign_id"] == _CAMPAIGN_ID
    assert result["daily_budget"] == 50.0
    assert result["configured_status"] == "PAUSED"
    assert captured["json"]["name"] == "Test Campaign"
    assert captured["json"]["daily_budget_micro_usd"] == 50_000_000
    assert captured["json"]["configured_status"] == "PAUSED"
    expected_url = f"https://ads-api.reddit.com/api/v3/ad_accounts/{_ACCOUNT_ID}/campaigns"
    assert captured["url"] == expected_url
    assert captured["headers"]["Authorization"] == f"Bearer {_TOKEN}"


@pytest.mark.asyncio
async def test_create_campaign_rejects_invalid_status(monkeypatch):
    result = await RedditAdsConnector().create_campaign(
        _TOKEN, _ACCOUNT_ID, "Test", daily_budget=50.0, configured_status="DELETED"
    )
    assert result.get("error") is True
    assert "ACTIVE or PAUSED" in result["message"]


@pytest.mark.asyncio
async def test_create_campaign_rejects_zero_budget(monkeypatch):
    result = await RedditAdsConnector().create_campaign(
        _TOKEN, _ACCOUNT_ID, "Test", daily_budget=0, configured_status="ACTIVE"
    )
    assert result.get("error") is True
