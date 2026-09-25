import json

import pytest

from app.connectors.apple_ads import AppleAdsConnector

_TOKEN = "apple-access-token"
_ORG_ID = "40669820"
_CAMPAIGN_ID = "570798765"


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
async def test_list_accounts_uses_bearer_token_without_org_context(monkeypatch):
    captured = {}

    class FakeClient(_FakeClientBase):
        async def get(self, url, *, headers=None, params=None):
            captured["url"] = url
            captured["headers"] = headers or {}
            captured["params"] = params or {}
            return _resp(
                200,
                {
                    "data": [
                        {
                            "orgId": 40669820,
                            "orgName": "Trip Trek",
                            "currency": "USD",
                            "timeZone": "America/Los_Angeles",
                            "roleNames": ["Admin"],
                        }
                    ]
                },
            )

    monkeypatch.setattr("app.connectors.apple_ads.httpx.AsyncClient", FakeClient)

    result = await AppleAdsConnector().list_accounts(_TOKEN)

    assert captured["url"] == "https://api.searchads.apple.com/api/v5/acls"
    assert captured["headers"]["Authorization"] == "Bearer apple-access-token"
    assert "X-AP-Context" not in captured["headers"]
    assert result["accounts"] == [
        {
            "account_id": "40669820",
            "name": "Trip Trek",
            "currency": "USD",
            "timezone": "America/Los_Angeles",
            "roles": ["Admin"],
        }
    ]


@pytest.mark.asyncio
async def test_get_campaign_performance_posts_report_with_org_context(monkeypatch):
    captured = []

    class FakeClient(_FakeClientBase):
        async def get(self, url, *, headers=None, params=None):
            captured.append(("GET", url, headers or {}, params or None, None))
            return _resp(
                200,
                {
                    "data": [
                        {
                            "id": 570798765,
                            "name": "Brand Search",
                            "status": "ENABLED",
                        }
                    ]
                },
            )

        async def post(self, url, *, headers=None, params=None, json=None):
            captured.append(("POST", url, headers or {}, params or None, json or {}))
            return _resp(
                200,
                {
                    "data": {
                        "reportingDataResponse": {
                            "row": [
                                {
                                    "metadata": {"campaignId": 570798765, "campaignName": "Brand Search"},
                                    "granularity": [
                                        {
                                            "impressions": 1000,
                                            "taps": 50,
                                            "localSpend": {"amount": "12.34", "currency": "USD"},
                                            "installs": 7,
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                },
            )

    monkeypatch.setattr("app.connectors.apple_ads.httpx.AsyncClient", FakeClient)

    result = await AppleAdsConnector().get_campaign_performance(_TOKEN, _ORG_ID, "2025-01-01", "2025-01-31")

    assert result["account_id"] == _ORG_ID
    assert result["campaigns"] == [
        {
            "campaign_id": "570798765",
            "campaign_name": "Brand Search",
            "status": "ENABLED",
            "impressions": 1000,
            "clicks": 50,
            "spend": 12.34,
            "conversions": 7,
        }
    ]

    list_call = captured[0]
    report_call = captured[1]
    assert list_call[1] == "https://api.searchads.apple.com/api/v5/campaigns"
    assert list_call[2]["X-AP-Context"] == "orgId=40669820"
    assert report_call[1] == "https://api.searchads.apple.com/api/v5/reports/campaigns"
    assert report_call[2]["Authorization"] == "Bearer apple-access-token"
    assert report_call[4]["startTime"] == "2025-01-01"
    assert report_call[4]["endTime"] == "2025-01-31"


@pytest.mark.asyncio
async def test_create_campaign_sends_correct_body(monkeypatch):
    captured = {}

    class FakeClient(_FakeClientBase):
        async def post(self, url, *, headers=None, json=None):
            captured["url"] = url
            captured["json"] = json
            return _resp(201, {"data": {"id": 570798765}})

    monkeypatch.setattr("app.connectors.apple_ads.httpx.AsyncClient", FakeClient)

    result = await AppleAdsConnector().create_campaign(
        access_token=_TOKEN,
        org_id=_ORG_ID,
        name="Test Campaign",
        status="PAUSED",
        daily_budget=100.0,
    )

    assert result["campaign_id"] == "570798765"
    assert result["campaign_name"] == "Test Campaign"
    assert result["status"] == "PAUSED"
    assert "campaigns" in captured["url"] and "api.searchads.apple.com" in captured["url"]
    assert captured["json"]["name"] == "Test Campaign"
    assert captured["json"]["status"] == "PAUSED"
    assert captured["json"]["dailyBudgetAmount"] == {"amount": "100.0", "currency": "USD"}


@pytest.mark.asyncio
async def test_update_campaign_budget_sends_correct_body(monkeypatch):
    captured = {}

    class FakeClient(_FakeClientBase):
        async def put(self, url, *, headers=None, json=None):
            captured["url"] = url
            captured["json"] = json
            return _resp(200, {"data": {"id": _CAMPAIGN_ID}})

    monkeypatch.setattr("app.connectors.apple_ads.httpx.AsyncClient", FakeClient)

    result = await AppleAdsConnector().update_campaign_budget(
        access_token=_TOKEN,
        org_id=_ORG_ID,
        campaign_id=_CAMPAIGN_ID,
        daily_budget=75.0,
    )

    assert result["campaign_id"] == _CAMPAIGN_ID
    assert result["new_daily_budget"] == 75.0
    assert result["updated"] is True
    assert _CAMPAIGN_ID in captured["url"]
    assert captured["json"]["dailyBudgetAmount"] == {"amount": "75.0", "currency": "USD"}
