import json

import pytest

from app.connectors.snap_ads import SnapAdsConnector

_TOKEN = "snap-access-token"
_ACCOUNT_ID = "adaccount123"
_CAMPAIGN_ID = "camp456"


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
            return _resp(
                201,
                {
                    "campaigns": [
                        {"campaign": {"id": _CAMPAIGN_ID, "name": "Test Campaign", "status": "PAUSED"}}
                    ]
                },
            )

    monkeypatch.setattr("app.connectors.snap_ads.httpx.AsyncClient", FakeClient)

    result = await SnapAdsConnector().create_campaign(
        access_token=_TOKEN,
        account_id=_ACCOUNT_ID,
        name="Test Campaign",
        status="PAUSED",
        daily_budget=50.0,
        objective="APP_INSTALL",
    )

    assert result["campaign_id"] == _CAMPAIGN_ID
    assert result["campaign_name"] == "Test Campaign"
    assert result["status"] == "PAUSED"
    assert captured["json"]["campaigns"][0]["campaign"]["daily_budget_micro"] == 50_000_000
    assert _ACCOUNT_ID in captured["url"]


@pytest.mark.asyncio
async def test_create_campaign_no_budget(monkeypatch):
    captured = {}

    class FakeClient(_FakeClientBase):
        async def post(self, url, *, headers=None, json=None):
            captured["json"] = json
            return _resp(201, {"campaigns": [{"campaign": {"id": _CAMPAIGN_ID}}]})

    monkeypatch.setattr("app.connectors.snap_ads.httpx.AsyncClient", FakeClient)

    result = await SnapAdsConnector().create_campaign(
        access_token=_TOKEN,
        account_id=_ACCOUNT_ID,
        name="Test",
    )
    assert "daily_budget_micro" not in captured["json"]["campaigns"][0]["campaign"]
    assert result["campaign_id"] == _CAMPAIGN_ID
