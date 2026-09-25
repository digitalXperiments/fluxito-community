import pytest

from app.connectors.meta_ads import MetaAdsConnector

_TOKEN = "meta-access-token"
_CAMPAIGN_ID = "987654321"


class _FakeClientBase:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


def _resp(status: int, body: dict, text: str = ""):
    _text = text or str(body)

    class _R:
        status_code = status
        text = _text

        def json(self):
            return body

    return _R()


@pytest.mark.asyncio
async def test_update_campaign_budget(monkeypatch):
    captured = {}

    class FakeClient(_FakeClientBase):
        async def post(self, url, *, data=None):
            captured["url"] = url
            captured["data"] = data or {}
            return _resp(200, {"success": True})

    monkeypatch.setattr("app.connectors.meta_ads.httpx.AsyncClient", FakeClient)

    result = await MetaAdsConnector().update_campaign_budget(
        access_token=_TOKEN,
        campaign_id=_CAMPAIGN_ID,
        daily_budget=50.0,
    )

    assert result["campaign_id"] == _CAMPAIGN_ID
    assert result["new_daily_budget"] == 50.0
    assert result["updated"] is True
    assert _CAMPAIGN_ID in captured["url"]
    # Meta stores budget in cents: 50 * 100 = 5000
    assert captured["data"]["daily_budget"] == 5000
    assert captured["data"]["access_token"] == _TOKEN


@pytest.mark.asyncio
async def test_update_campaign_budget_api_error(monkeypatch):
    class FakeClient(_FakeClientBase):
        async def post(self, url, *, data=None):
            return _resp(
                400, {"error": {"message": "Invalid budget"}}, text='{"error":{"message":"Invalid budget"}}'
            )

    monkeypatch.setattr("app.connectors.meta_ads.httpx.AsyncClient", FakeClient)

    result = await MetaAdsConnector().update_campaign_budget(
        access_token=_TOKEN,
        campaign_id=_CAMPAIGN_ID,
        daily_budget=999999.0,
    )
    assert result.get("error") is True
