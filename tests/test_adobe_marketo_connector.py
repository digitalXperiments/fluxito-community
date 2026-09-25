"""
Tests for AdobeMarketoConnector.

Mirrors the style of test_bing_webmaster_connector.py:
  - monkeypatch httpx.AsyncClient with an async-context-manager shim
  - assert correct URLs / params / return shapes
"""

import json

import pytest

from app.connectors.adobe_marketo import AdobeMarketoConnector

_INSTANCE = "https://123-ABC-456.mktorest.com"
_CLIENT_ID = "cid"
_CLIENT_SECRET = "csecret"
_TOKEN = "marketo-access-token"


class _FakeClientBase:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, *, headers=None, params=None, json=None):
        raise NotImplementedError("post not stubbed for this test")


def _resp(status: int, body: dict):
    class _R:
        status_code = status
        text = json.dumps(body)

        def json(self):
            return body

    return _R()


def _connector_with_token(monkeypatch, get_handler):
    """Return a connector whose token endpoint is satisfied and whose REST GETs go to get_handler."""

    class FakeClient(_FakeClientBase):
        async def get(self, url, *, headers=None, params=None):
            if "/identity/oauth/token" in url:
                return _resp(200, {"access_token": _TOKEN, "expires_in": 3600})
            return get_handler(url, headers, params)

    monkeypatch.setattr("app.connectors.adobe_marketo.httpx.AsyncClient", FakeClient)
    return AdobeMarketoConnector()


@pytest.mark.asyncio
async def test_token_is_fetched_with_client_credentials(monkeypatch):
    captured = {}

    class FakeClient(_FakeClientBase):
        async def get(self, url, *, headers=None, params=None):
            captured["url"] = url
            captured["params"] = params or {}
            return _resp(200, {"access_token": _TOKEN, "expires_in": 3600})

    monkeypatch.setattr("app.connectors.adobe_marketo.httpx.AsyncClient", FakeClient)

    conn = AdobeMarketoConnector()
    result = await conn._get_marketo_token(_INSTANCE, _CLIENT_ID, _CLIENT_SECRET)

    assert captured["url"] == f"{_INSTANCE}/identity/oauth/token"
    assert captured["params"]["grant_type"] == "client_credentials"
    assert captured["params"]["client_id"] == _CLIENT_ID
    assert captured["params"]["client_secret"] == _CLIENT_SECRET
    assert result == {"token": _TOKEN}


@pytest.mark.asyncio
async def test_token_is_cached_per_instance(monkeypatch):
    calls = {"n": 0}

    class FakeClient(_FakeClientBase):
        async def get(self, url, *, headers=None, params=None):
            calls["n"] += 1
            return _resp(200, {"access_token": _TOKEN, "expires_in": 3600})

    monkeypatch.setattr("app.connectors.adobe_marketo.httpx.AsyncClient", FakeClient)

    conn = AdobeMarketoConnector()
    await conn._get_marketo_token(_INSTANCE, _CLIENT_ID, _CLIENT_SECRET)
    await conn._get_marketo_token(_INSTANCE, _CLIENT_ID, _CLIENT_SECRET)
    assert calls["n"] == 1  # second call served from cache


@pytest.mark.asyncio
async def test_get_leads_sends_filter_and_bearer(monkeypatch):
    captured = {}

    def handler(url, headers, params):
        captured["url"] = url
        captured["headers"] = headers or {}
        captured["params"] = params or {}
        return _resp(200, {"success": True, "result": [{"id": 1, "email": "a@b.com"}]})

    conn = _connector_with_token(monkeypatch, handler)
    result = await conn.get_leads(
        _INSTANCE,
        _CLIENT_ID,
        _CLIENT_SECRET,
        filter_type="email",
        filter_values=["a@b.com"],
        fields=["id", "email"],
        limit=10,
    )

    assert captured["url"] == f"{_INSTANCE}/rest/v1/leads.json"
    assert captured["headers"]["Authorization"] == f"Bearer {_TOKEN}"
    assert captured["params"]["filterType"] == "email"
    assert captured["params"]["filterValues"] == "a@b.com"
    assert captured["params"]["fields"] == "id,email"
    assert captured["params"]["batchSize"] == 10
    assert result["result"] == [{"id": 1, "email": "a@b.com"}]


@pytest.mark.asyncio
async def test_list_lead_lists_calls_lists_endpoint(monkeypatch):
    captured = {}

    def handler(url, headers, params):
        captured["url"] = url
        return _resp(200, {"success": True, "result": [{"id": 7, "name": "VIPs"}]})

    conn = _connector_with_token(monkeypatch, handler)
    result = await conn.list_lead_lists(_INSTANCE, _CLIENT_ID, _CLIENT_SECRET)

    assert captured["url"] == f"{_INSTANCE}/rest/v1/lists.json"
    assert result["result"][0]["name"] == "VIPs"


@pytest.mark.asyncio
async def test_list_programs_uses_asset_endpoint(monkeypatch):
    captured = {}

    def handler(url, headers, params):
        captured["url"] = url
        captured["params"] = params or {}
        return _resp(200, {"success": True, "result": [{"id": 3, "name": "Q3 Nurture"}]})

    conn = _connector_with_token(monkeypatch, handler)
    result = await conn.list_programs(_INSTANCE, _CLIENT_ID, _CLIENT_SECRET, limit=20)

    assert captured["url"] == f"{_INSTANCE}/rest/asset/v1/programs.json"
    assert captured["params"]["maxReturn"] == 20
    assert result["result"][0]["id"] == 3


@pytest.mark.asyncio
async def test_api_error_returns_structured_error(monkeypatch):
    def handler(url, headers, params):
        return _resp(401, {"success": False, "errors": [{"code": "601", "message": "Access token invalid"}]})

    conn = _connector_with_token(monkeypatch, handler)
    result = await conn.list_lead_lists(_INSTANCE, _CLIENT_ID, _CLIENT_SECRET)
    assert result["error"] is True
    assert result["status_code"] == 401


@pytest.mark.asyncio
async def test_in_body_601_triggers_token_refresh_and_retry(monkeypatch):
    state = {"rest_calls": 0, "token_calls": 0}

    class FakeClient(_FakeClientBase):
        async def get(self, url, *, headers=None, params=None):
            if "/identity/oauth/token" in url:
                state["token_calls"] += 1
                return _resp(200, {"access_token": _TOKEN, "expires_in": 3600})
            state["rest_calls"] += 1
            if state["rest_calls"] == 1:
                return _resp(200, {"success": False, "errors": [{"code": "601", "message": "expired"}]})
            return _resp(200, {"success": True, "result": [{"id": 1}]})

    monkeypatch.setattr("app.connectors.adobe_marketo.httpx.AsyncClient", FakeClient)
    conn = AdobeMarketoConnector()
    result = await conn.list_lead_lists(_INSTANCE, _CLIENT_ID, _CLIENT_SECRET)

    assert state["rest_calls"] == 2  # retried once
    assert state["token_calls"] == 2  # token re-fetched after cache eviction
    assert "error" not in result
    assert result["result"] == [{"id": 1}]


@pytest.mark.asyncio
async def test_unhandled_success_false_code_returns_error(monkeypatch):
    def handler(url, headers, params):
        return _resp(200, {"success": False, "errors": [{"code": "610", "message": "Field not accessible"}]})

    conn = _connector_with_token(monkeypatch, handler)
    result = await conn.list_lead_lists(_INSTANCE, _CLIENT_ID, _CLIENT_SECRET)
    assert result["error"] is True


@pytest.mark.asyncio
async def test_audit_instance_reports_quota(monkeypatch):
    def handler(url, headers, params):
        if url.endswith("/rest/v1/stats/usage.json"):
            return _resp(200, {"success": True, "result": [{"total": 9000}]})
        if url.endswith("/rest/asset/v1/programs.json"):
            return _resp(200, {"success": True, "result": [{"id": 1, "name": "P", "status": "on"}]})
        return _resp(200, {"success": True, "result": []})

    conn = _connector_with_token(monkeypatch, handler)
    result = await conn.audit_instance(_INSTANCE, _CLIENT_ID, _CLIENT_SECRET)

    assert "api_calls_used_today" in result
    assert result["api_calls_used_today"] == 9000
    assert result["program_count"] == 1
    assert result["off_or_unknown_programs"] == []  # status "on" is not flagged


@pytest.mark.asyncio
async def test_check_data_quality_counts_missing_fields(monkeypatch):
    def handler(url, headers, params):
        return _resp(
            200,
            {
                "success": True,
                "result": [
                    {"id": 1, "email": "a@b.com", "company": "Acme"},
                    {"id": 2, "email": None, "company": None},
                ],
            },
        )

    conn = _connector_with_token(monkeypatch, handler)
    result = await conn.check_data_quality(
        _INSTANCE, _CLIENT_ID, _CLIENT_SECRET, sample_emails=["a@b.com", "x@y.com"]
    )
    assert result["leads_checked"] == 2
    assert result["missing_email"] == 1
    assert result["missing_company"] == 1


def _connector_with_post(monkeypatch, post_handler):
    class FakeClient(_FakeClientBase):
        async def get(self, url, *, headers=None, params=None):
            return _resp(200, {"access_token": _TOKEN, "expires_in": 3600})

        async def post(self, url, *, headers=None, params=None, json=None):
            return post_handler(url, headers, params, json)

    monkeypatch.setattr("app.connectors.adobe_marketo.httpx.AsyncClient", FakeClient)
    return AdobeMarketoConnector()


@pytest.mark.asyncio
async def test_create_or_update_leads_posts_payload(monkeypatch):
    captured = {}

    def handler(url, headers, params, body):
        captured["url"] = url
        captured["body"] = body
        return _resp(200, {"success": True, "result": [{"id": 1, "status": "created"}]})

    conn = _connector_with_post(monkeypatch, handler)
    result = await conn.create_or_update_leads(
        _INSTANCE,
        _CLIENT_ID,
        _CLIENT_SECRET,
        leads=[{"email": "a@b.com", "firstName": "A"}],
        lookup_field="email",
        action="createOrUpdate",
    )

    assert captured["url"] == f"{_INSTANCE}/rest/v1/leads.json"
    assert captured["body"]["action"] == "createOrUpdate"
    assert captured["body"]["lookupField"] == "email"
    assert captured["body"]["input"] == [{"email": "a@b.com", "firstName": "A"}]
    assert result["result"][0]["status"] == "created"


@pytest.mark.asyncio
async def test_request_campaign_posts_leads(monkeypatch):
    captured = {}

    def handler(url, headers, params, body):
        captured["url"] = url
        captured["body"] = body
        return _resp(200, {"success": True, "result": [{"id": 55}]})

    conn = _connector_with_post(monkeypatch, handler)
    result = await conn.request_campaign(
        _INSTANCE,
        _CLIENT_ID,
        _CLIENT_SECRET,
        campaign_id="55",
        lead_ids=["1", "2"],
    )

    assert captured["url"] == f"{_INSTANCE}/rest/v1/campaigns/55/trigger.json"
    assert captured["body"]["input"]["leads"] == [{"id": 1}, {"id": 2}]
    assert result["result"][0]["id"] == 55


@pytest.mark.asyncio
async def test_add_leads_to_list_posts_ids(monkeypatch):
    captured = {}

    def handler(url, headers, params, body):
        captured["url"] = url
        captured["body"] = body
        return _resp(200, {"success": True, "result": [{"id": 1, "status": "added"}]})

    conn = _connector_with_post(monkeypatch, handler)
    result = await conn.add_leads_to_list(
        _INSTANCE, _CLIENT_ID, _CLIENT_SECRET, list_id="9", lead_ids=["1", "2"]
    )

    assert captured["url"] == f"{_INSTANCE}/rest/v1/lists/9/leads.json"
    assert captured["body"]["input"] == [{"id": 1}, {"id": 2}]
    assert result["result"][0]["status"] == "added"


@pytest.mark.asyncio
async def test_remove_leads_from_list_uses_method_delete(monkeypatch):
    captured = {}

    def handler(url, headers, params, body):
        captured["url"] = url
        captured["params"] = params or {}
        captured["body"] = body
        return _resp(200, {"success": True, "result": [{"id": 1, "status": "removed"}]})

    conn = _connector_with_post(monkeypatch, handler)
    result = await conn.remove_leads_from_list(
        _INSTANCE, _CLIENT_ID, _CLIENT_SECRET, list_id="9", lead_ids=["1", "2"]
    )

    assert captured["url"] == f"{_INSTANCE}/rest/v1/lists/9/leads.json"
    assert captured["params"]["_method"] == "DELETE"  # DELETE tunneled over POST
    assert captured["body"]["input"] == [{"id": 1}, {"id": 2}]
    assert result["result"][0]["status"] == "removed"


@pytest.mark.asyncio
async def test_schedule_campaign_includes_run_at_when_provided(monkeypatch):
    captured = {}

    def handler(url, headers, params, body):
        captured["url"] = url
        captured["body"] = body
        return _resp(200, {"success": True, "result": [{"id": 55}]})

    conn = _connector_with_post(monkeypatch, handler)
    result = await conn.schedule_campaign(
        _INSTANCE, _CLIENT_ID, _CLIENT_SECRET, campaign_id="55", run_at="2026-07-01T09:00:00Z"
    )

    assert captured["url"] == f"{_INSTANCE}/rest/v1/campaigns/55/schedule.json"
    assert captured["body"]["input"]["runAt"] == "2026-07-01T09:00:00Z"
    assert result["result"][0]["id"] == 55


@pytest.mark.asyncio
async def test_schedule_campaign_omits_run_at_when_none(monkeypatch):
    captured = {}

    def handler(url, headers, params, body):
        captured["body"] = body
        return _resp(200, {"success": True, "result": []})

    conn = _connector_with_post(monkeypatch, handler)
    await conn.schedule_campaign(_INSTANCE, _CLIENT_ID, _CLIENT_SECRET, campaign_id="55")

    assert "runAt" not in captured["body"]["input"]  # omitted when not provided
