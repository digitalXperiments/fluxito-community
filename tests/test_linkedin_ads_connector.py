import json

import pytest

from app.connectors.linkedin_ads import LinkedInAdsConnector

_TOKEN = "linkedin-access-token"
_ACCOUNT_ID = "123456"
_CAMPAIGN_ID = "789012"


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
async def test_create_campaign(monkeypatch):
    captured = {}

    class FakeClient(_FakeClientBase):
        async def post(self, url, *, headers=None, json=None):
            captured["url"] = url
            captured["json"] = json
            return _resp(201, {"elements": [{"id": _CAMPAIGN_ID}]})

    monkeypatch.setattr("app.connectors.linkedin_ads.httpx.AsyncClient", FakeClient)

    result = await LinkedInAdsConnector().create_campaign(
        access_token=_TOKEN,
        account_id=_ACCOUNT_ID,
        name="Test Campaign",
        objective_type="BRAND_AWARENESS",
    )

    assert result["campaign_id"] == _CAMPAIGN_ID
    assert result["campaign_name"] == "Test Campaign"
    assert captured["json"]["account"] == f"urn:li:sponsoredAccount:{_ACCOUNT_ID}"
    assert captured["json"]["objectiveType"] == "BRAND_AWARENESS"
    assert "/adCampaignsV2" in captured["url"]


@pytest.mark.asyncio
async def test_create_campaign_with_budget(monkeypatch):
    captured = {}

    class FakeClient(_FakeClientBase):
        async def post(self, url, *, headers=None, json=None):
            captured["json"] = json
            return _resp(201, {"elements": [{"id": _CAMPAIGN_ID}]})

    monkeypatch.setattr("app.connectors.linkedin_ads.httpx.AsyncClient", FakeClient)

    result = await LinkedInAdsConnector().create_campaign(
        access_token=_TOKEN,
        account_id=_ACCOUNT_ID,
        name="Test",
        status="PAUSED",
        daily_budget=100.0,
        objective_type="WEBSITE_VISITS",
    )
    assert captured["json"]["dailyBudget"] == {"amount": 100.0}
    assert captured["json"]["status"] == "PAUSED"
    assert result["status"] == "PAUSED"
