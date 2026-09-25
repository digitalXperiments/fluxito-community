import json

import pytest

from app.connectors.pinterest_ads import PinterestAdsConnector

_TOKEN = "pinterest-access-token"
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
            return _resp(201, {"id": _CAMPAIGN_ID, "name": "Test Campaign", "status": "ACTIVE"})

    monkeypatch.setattr("app.connectors.pinterest_ads.httpx.AsyncClient", FakeClient)

    result = await PinterestAdsConnector().create_campaign(
        access_token=_TOKEN,
        account_id=_ACCOUNT_ID,
        name="Test Campaign",
    )

    assert result["campaign_id"] == _CAMPAIGN_ID
    assert result["campaign_name"] == "Test Campaign"
    assert result["status"] == "ACTIVE"
    assert captured["json"]["name"] == "Test Campaign"
    assert _ACCOUNT_ID in captured["url"]


@pytest.mark.asyncio
async def test_create_campaign_with_budget(monkeypatch):
    captured = {}

    class FakeClient(_FakeClientBase):
        async def post(self, url, *, headers=None, json=None):
            captured["json"] = json
            return _resp(201, {"id": _CAMPAIGN_ID})

    monkeypatch.setattr("app.connectors.pinterest_ads.httpx.AsyncClient", FakeClient)

    await PinterestAdsConnector().create_campaign(
        access_token=_TOKEN,
        account_id=_ACCOUNT_ID,
        name="Test",
        daily_budget=50.0,
        objective_type="TRAFFIC",
    )
    assert captured["json"]["daily_budget"] == 50.0
    assert captured["json"]["objective_type"] == "TRAFFIC"
