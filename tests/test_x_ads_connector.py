import re

import pytest

from app.connectors.x_ads import XAdsConnector, XOAuth1Token


@pytest.mark.asyncio
async def test_list_accounts_sends_oauth1_authorization_header(monkeypatch):
    """X Ads API requests are signed with OAuth 1.0a credentials."""
    captured = {}

    class FakeResponse:
        status_code = 200
        text = '{"data":[]}'

        def json(self):
            return {"data": [{"id": "abc123", "name": "Main account", "approval_status": "ACCEPTED"}]}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, *, headers=None, params=None):
            captured["url"] = url
            captured["headers"] = headers or {}
            captured["params"] = params or {}
            return FakeResponse()

    monkeypatch.setattr("app.connectors.x_ads.httpx.AsyncClient", FakeClient)

    connector = XAdsConnector(consumer_key="ck", consumer_secret="cs")
    result = await connector.list_accounts(XOAuth1Token(token="at", token_secret="ats"))

    assert result["accounts"] == [{"account_id": "abc123", "name": "Main account", "status": "ACCEPTED"}]
    auth = captured["headers"]["Authorization"]
    assert auth.startswith("OAuth ")
    assert 'oauth_consumer_key="ck"' in auth
    assert 'oauth_token="at"' in auth
    assert re.search(r'oauth_signature="[^"]+"', auth)


@pytest.mark.asyncio
async def test_create_campaign_sends_signed_post(monkeypatch):
    """create_campaign uses OAuth 1.0a signed POST request."""
    captured = {}

    class FakeResponse:
        status_code = 201
        text = '{"data":{}}'

        def json(self):
            return {"data": {"id": "camp1", "name": "Test Campaign"}}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, headers=None, params=None, json=None):
            captured["url"] = url
            captured["headers"] = headers or {}
            captured["json"] = json
            return FakeResponse()

        async def get(self, *a, **kw):
            raise AssertionError("GET should not be called")

        async def put(self, *a, **kw):
            raise AssertionError("PUT should not be called")

    monkeypatch.setattr("app.connectors.x_ads.httpx.AsyncClient", FakeClient)

    connector = XAdsConnector(consumer_key="ck", consumer_secret="cs")
    result = await connector.create_campaign(
        token=XOAuth1Token(token="at", token_secret="ats"),
        account_id="abc123",
        name="Test Campaign",
        objective="WEBSITE_CLICKS",
        daily_budget=50.0,
        entity_status="PAUSED",
    )

    assert result["campaign_id"] == "camp1"
    assert result["campaign_name"] == "Test Campaign"
    assert "/accounts/abc123/campaigns" in captured["url"]
    assert captured["json"]["name"] == "Test Campaign"
    assert captured["json"]["objective"] == "WEBSITE_CLICKS"
    assert captured["json"]["entity_status"] == "PAUSED"
    # 50 * 1_000_000 = 50_000_000 micro
    assert captured["json"]["daily_budget_local_micro"] == 50_000_000
    auth = captured["headers"]["Authorization"]
    assert auth.startswith("OAuth ")


@pytest.mark.asyncio
async def test_update_campaign_budget_sends_signed_put(monkeypatch):
    """update_campaign_budget uses OAuth 1.0a signed PUT request."""
    captured = {}

    class FakeResponse:
        status_code = 200
        text = '{"data":{}}'

        def json(self):
            return {"data": {}}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def put(self, url, *, headers=None, params=None):
            captured["url"] = url
            captured["headers"] = headers or {}
            captured["params"] = params or {}
            return FakeResponse()

        async def get(self, *a, **kw):
            raise AssertionError("GET not expected")

        async def post(self, *a, **kw):
            raise AssertionError("POST not expected")

    monkeypatch.setattr("app.connectors.x_ads.httpx.AsyncClient", FakeClient)

    connector = XAdsConnector(consumer_key="ck", consumer_secret="cs")
    result = await connector.update_campaign_budget(
        token=XOAuth1Token(token="at", token_secret="ats"),
        account_id="abc123",
        campaign_id="camp1",
        daily_budget=75.0,
    )

    assert result["campaign_id"] == "camp1"
    assert result["new_daily_budget"] == 75.0
    assert "/accounts/abc123/campaigns/camp1" in captured["url"]
    assert captured["params"]["daily_budget_local_micro"] == 75_000_000
