import json

import pytest

from app.connectors.tiktok_ads import TikTokAdsConnector

_TOKEN = "tiktok-access-token"
_ACCOUNT_ID = "1234567890"
_CAMPAIGN_ID = "camp_001"


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
async def test_create_campaign_succeeds(monkeypatch):
    captured = {}

    class FakeClient(_FakeClientBase):
        async def post(self, url, *, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers or {}
            captured["json"] = json
            return _resp(200, {"code": 0, "message": "OK", "data": {"campaign_id": _CAMPAIGN_ID}})

    monkeypatch.setattr("app.connectors.tiktok_ads.httpx.AsyncClient", FakeClient)

    result = await TikTokAdsConnector().create_campaign(
        access_token=_TOKEN,
        account_id=_ACCOUNT_ID,
        campaign_name="Test Campaign",
        objective_type="TRAFFIC",
        budget=50.0,
    )

    assert result["campaign_id"] == _CAMPAIGN_ID
    assert result["campaign_name"] == "Test Campaign"
    assert result["objective_type"] == "TRAFFIC"
    assert result["budget"] == 50.0
    assert captured["json"]["advertiser_id"] == _ACCOUNT_ID
    assert captured["json"]["campaign_name"] == "Test Campaign"
    assert captured["headers"]["Access-Token"] == _TOKEN
    assert "/campaign/create/" in captured["url"]


@pytest.mark.asyncio
async def test_create_campaign_missing_params_returns_error(monkeypatch):
    """TikTok API error is returned as error dict."""

    class FakeClient(_FakeClientBase):
        async def post(self, url, *, headers=None, json=None):
            return _resp(200, {"code": 40000, "message": "param missing: advertiser_id"})

    monkeypatch.setattr("app.connectors.tiktok_ads.httpx.AsyncClient", FakeClient)

    result = await TikTokAdsConnector().create_campaign(
        access_token=_TOKEN,
        account_id=_ACCOUNT_ID,
        campaign_name="Test",
        objective_type="TRAFFIC",
        budget=50.0,
    )
    assert result.get("error") is True
    assert "40000" in result["message"]
