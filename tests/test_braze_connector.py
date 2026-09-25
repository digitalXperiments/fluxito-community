"""
Tests for BrazeConnector.

Mirrors the style of test_adobe_marketo_connector.py:
  - monkeypatch httpx.AsyncClient with an async-context-manager shim
  - assert correct URLs, headers, params, body, and return shapes
  - all HTTP is faked (no real Braze API calls)
"""

import json

import pytest

from app.connectors.braze import BrazeConnector

_REST_ENDPOINT = "https://rest.iad-01.braze.com"
_API_KEY = "braze-api-key"


class _FakeClientBase:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, *, headers=None, params=None, json=None):
        raise NotImplementedError("post not stubbed for this test")


def _resp(status: int, body: dict, *, extra_headers: dict | None = None):
    """Build a fake HTTP response.

    *extra_headers* may be passed for tests that need to inspect response
    headers (e.g. 429 rate-limit with X-RateLimit-Reset).
    """
    _text = json.dumps(body)

    class _R:
        status_code = status
        text = _text
        headers = extra_headers or {}

        def json(self):
            return body

    return _R()


def _connector_with_get(monkeypatch, get_handler):
    """Return a BrazeConnector whose GET requests are served by get_handler."""

    class FakeClient(_FakeClientBase):
        async def get(self, url, *, headers=None, params=None):
            return get_handler(url, headers, params)

    monkeypatch.setattr("app.connectors.braze.httpx.AsyncClient", FakeClient)
    return BrazeConnector()


def _connector_with_post(monkeypatch, post_handler):
    """Return a BrazeConnector whose POST requests are served by post_handler."""

    class FakeClient(_FakeClientBase):
        async def post(self, url, *, headers=None, params=None, json=None):
            return post_handler(url, headers, params, json)

    monkeypatch.setattr("app.connectors.braze.httpx.AsyncClient", FakeClient)
    return BrazeConnector()


# ---------------------------------------------------------------------------
# Auth & request shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_campaigns_sends_bearer_auth_and_correct_url(monkeypatch):
    captured = {}

    def handler(url, headers, params):
        captured["url"] = url
        captured["headers"] = headers or {}
        captured["params"] = params or {}
        return _resp(200, {"campaigns": [], "message": "success"})

    conn = _connector_with_get(monkeypatch, handler)
    result = await conn.list_campaigns(_REST_ENDPOINT, _API_KEY)

    assert captured["url"] == f"{_REST_ENDPOINT}/campaigns/list"
    assert captured["headers"]["Authorization"] == f"Bearer {_API_KEY}"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert result["message"] == "success"


@pytest.mark.asyncio
async def test_get_campaign_details_passes_campaign_id_param(monkeypatch):
    captured = {}

    def handler(url, headers, params):
        captured["url"] = url
        captured["headers"] = headers or {}
        captured["params"] = params or {}
        return _resp(200, {"campaign_id": "abc123", "name": "Test Campaign"})

    conn = _connector_with_get(monkeypatch, handler)
    result = await conn.get_campaign_details(_REST_ENDPOINT, _API_KEY, "abc123")

    assert captured["url"] == f"{_REST_ENDPOINT}/campaigns/details"
    assert captured["params"]["campaign_id"] == "abc123"
    assert captured["headers"]["Authorization"] == f"Bearer {_API_KEY}"
    assert result["name"] == "Test Campaign"


@pytest.mark.asyncio
async def test_track_users_posts_body_to_users_track(monkeypatch):
    captured = {}

    def handler(url, headers, params, body):
        captured["url"] = url
        captured["headers"] = headers or {}
        captured["body"] = body
        return _resp(200, {"message": "success"})

    conn = _connector_with_post(monkeypatch, handler)
    result = await conn.track_users(
        _REST_ENDPOINT,
        _API_KEY,
        attributes=[{"external_id": "u1", "first_name": "Alice"}],
        events=[{"external_id": "u1", "name": "purchase"}],
        purchases=[{"external_id": "u1", "product_id": "p1", "price": 9.99}],
    )

    assert captured["url"] == f"{_REST_ENDPOINT}/users/track"
    assert captured["headers"]["Authorization"] == f"Bearer {_API_KEY}"
    assert captured["body"]["attributes"] == [{"external_id": "u1", "first_name": "Alice"}]
    assert captured["body"]["events"] == [{"external_id": "u1", "name": "purchase"}]
    assert captured["body"]["purchases"] == [{"external_id": "u1", "product_id": "p1", "price": 9.99}]
    assert result["message"] == "success"


# ---------------------------------------------------------------------------
# Happy path — GET endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_canvases_happy_path(monkeypatch):
    captured = {}

    def handler(url, headers, params):
        captured["url"] = url
        captured["params"] = params or {}
        return _resp(200, {"canvases": [{"id": "c1", "name": "Onboarding"}], "message": "success"})

    conn = _connector_with_get(monkeypatch, handler)
    result = await conn.list_canvases(_REST_ENDPOINT, _API_KEY, page=1, include_archived=True)

    assert captured["url"] == f"{_REST_ENDPOINT}/canvas/list"
    assert captured["params"]["page"] == 1
    assert captured["params"]["include_archived"] == "true"
    assert result["canvases"][0]["name"] == "Onboarding"


@pytest.mark.asyncio
async def test_get_canvas_details_happy_path(monkeypatch):
    captured = {}

    def handler(url, headers, params):
        captured["url"] = url
        captured["params"] = params or {}
        return _resp(200, {"canvas_id": "cv1", "name": "Welcome Canvas", "steps": []})

    conn = _connector_with_get(monkeypatch, handler)
    result = await conn.get_canvas_details(_REST_ENDPOINT, _API_KEY, "cv1")

    assert captured["url"] == f"{_REST_ENDPOINT}/canvas/details"
    assert captured["params"]["canvas_id"] == "cv1"
    assert result["canvas_id"] == "cv1"


@pytest.mark.asyncio
async def test_list_segments_happy_path(monkeypatch):
    captured = {}

    def handler(url, headers, params):
        captured["url"] = url
        captured["params"] = params or {}
        return _resp(200, {"segments": [{"id": "s1", "name": "Active Users"}], "message": "success"})

    conn = _connector_with_get(monkeypatch, handler)
    result = await conn.list_segments(_REST_ENDPOINT, _API_KEY, page=2, sort_direction="asc")

    assert captured["url"] == f"{_REST_ENDPOINT}/segments/list"
    assert captured["params"]["page"] == 2
    assert captured["params"]["sort_direction"] == "asc"
    assert result["segments"][0]["id"] == "s1"


@pytest.mark.asyncio
async def test_get_segment_details_happy_path(monkeypatch):
    captured = {}

    def handler(url, headers, params):
        captured["url"] = url
        captured["params"] = params or {}
        return _resp(200, {"segment_id": "s1", "name": "VIP Users", "description": "High-value"})

    conn = _connector_with_get(monkeypatch, handler)
    result = await conn.get_segment_details(_REST_ENDPOINT, _API_KEY, "s1")

    assert captured["url"] == f"{_REST_ENDPOINT}/segments/details"
    assert captured["params"]["segment_id"] == "s1"
    assert result["segment_id"] == "s1"


# ---------------------------------------------------------------------------
# Happy path — POST endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_message_posts_to_messages_send(monkeypatch):
    captured = {}

    def handler(url, headers, params, body):
        captured["url"] = url
        captured["body"] = body
        return _resp(200, {"message": "success", "dispatch_id": "d1"})

    conn = _connector_with_post(monkeypatch, handler)
    result = await conn.send_message(
        _REST_ENDPOINT,
        _API_KEY,
        external_user_ids=["u1", "u2"],
        messages={"email": {"subject": "Hello"}},
    )

    assert captured["url"] == f"{_REST_ENDPOINT}/messages/send"
    assert captured["body"]["external_user_ids"] == ["u1", "u2"]
    assert captured["body"]["messages"] == {"email": {"subject": "Hello"}}
    assert captured["body"]["broadcast"] is False
    assert captured["body"]["override_frequency_capping"] is False
    assert captured["body"]["recipient_subscription_state"] == "subscribed"
    assert result["dispatch_id"] == "d1"


@pytest.mark.asyncio
async def test_trigger_campaign_posts_to_campaigns_trigger_send(monkeypatch):
    captured = {}

    def handler(url, headers, params, body):
        captured["url"] = url
        captured["body"] = body
        return _resp(200, {"message": "success"})

    conn = _connector_with_post(monkeypatch, handler)
    result = await conn.trigger_campaign(
        _REST_ENDPOINT,
        _API_KEY,
        "camp-a",
        recipients=[{"external_user_id": "u1"}],
        trigger_properties={"source": "welcome"},
    )

    assert captured["url"] == f"{_REST_ENDPOINT}/campaigns/trigger/send"
    assert captured["body"]["campaign_id"] == "camp-a"
    assert captured["body"]["recipients"] == [{"external_user_id": "u1"}]
    assert captured["body"]["trigger_properties"] == {"source": "welcome"}
    assert captured["body"]["broadcast"] is False
    assert result["message"] == "success"


@pytest.mark.asyncio
async def test_trigger_canvas_posts_to_canvas_trigger_send(monkeypatch):
    captured = {}

    def handler(url, headers, params, body):
        captured["url"] = url
        captured["body"] = body
        return _resp(200, {"message": "success"})

    conn = _connector_with_post(monkeypatch, handler)
    result = await conn.trigger_canvas(
        _REST_ENDPOINT,
        _API_KEY,
        "canvas-1",
        recipients=[{"external_user_id": "u1"}],
        context={"ip": "127.0.0.1"},
    )

    assert captured["url"] == f"{_REST_ENDPOINT}/canvas/trigger/send"
    assert captured["body"]["canvas_id"] == "canvas-1"
    assert captured["body"]["recipients"] == [{"external_user_id": "u1"}]
    assert captured["body"]["context"] == {"ip": "127.0.0.1"}
    assert result["message"] == "success"


@pytest.mark.asyncio
async def test_delete_users_posts_to_users_delete(monkeypatch):
    captured = {}

    def handler(url, headers, params, body):
        captured["url"] = url
        captured["body"] = body
        return _resp(200, {"message": "success"})

    conn = _connector_with_post(monkeypatch, handler)
    result = await conn.delete_users(
        _REST_ENDPOINT,
        _API_KEY,
        external_ids=["u1", "u2"],
        braze_ids=["b1"],
    )

    assert captured["url"] == f"{_REST_ENDPOINT}/users/delete"
    assert captured["body"]["external_ids"] == ["u1", "u2"]
    assert captured["body"]["braze_ids"] == ["b1"]
    assert result["message"] == "success"


@pytest.mark.asyncio
async def test_create_send_id_posts_to_sends_id_create(monkeypatch):
    captured = {}

    def handler(url, headers, params, body):
        captured["url"] = url
        captured["body"] = body
        return _resp(200, {"message": "success", "send_id": "sid1"})

    conn = _connector_with_post(monkeypatch, handler)
    result = await conn.create_send_id(_REST_ENDPOINT, _API_KEY, "camp-a", send_id="my-sid")

    assert captured["url"] == f"{_REST_ENDPOINT}/sends/id/create"
    assert captured["body"]["campaign_id"] == "camp-a"
    assert captured["body"]["send_id"] == "my-sid"
    assert result["send_id"] == "sid1"


@pytest.mark.asyncio
async def test_create_user_alias_posts_body(monkeypatch):
    captured = {}

    def handler(url, headers, params, body):
        captured["url"] = url
        captured["body"] = body
        return _resp(200, {"message": "success"})

    conn = _connector_with_post(monkeypatch, handler)
    result = await conn.create_user_alias(
        _REST_ENDPOINT,
        _API_KEY,
        user_aliases=[{"alias_name": "alice", "alias_label": "email"}],
    )

    assert captured["url"] == f"{_REST_ENDPOINT}/users/alias/new"
    assert captured["body"]["user_aliases"] == [{"alias_name": "alice", "alias_label": "email"}]
    assert result["message"] == "success"


@pytest.mark.asyncio
async def test_identify_users_posts_body(monkeypatch):
    captured = {}

    def handler(url, headers, params, body):
        captured["url"] = url
        captured["body"] = body
        return _resp(200, {"message": "success"})

    conn = _connector_with_post(monkeypatch, handler)
    result = await conn.identify_users(
        _REST_ENDPOINT,
        _API_KEY,
        aliases_to_identify=[{"external_id": "u1", "alias_name": "alice", "alias_label": "email"}],
    )

    assert captured["url"] == f"{_REST_ENDPOINT}/users/identify"
    assert captured["body"]["aliases_to_identify"] == [
        {"external_id": "u1", "alias_name": "alice", "alias_label": "email"}
    ]
    assert result["message"] == "success"


@pytest.mark.asyncio
async def test_merge_users_posts_body(monkeypatch):
    captured = {}

    def handler(url, headers, params, body):
        captured["url"] = url
        captured["body"] = body
        return _resp(200, {"message": "success"})

    conn = _connector_with_post(monkeypatch, handler)
    result = await conn.merge_users(
        _REST_ENDPOINT,
        _API_KEY,
        merge_updates=[
            {"identifier_to_keep": {"external_id": "u1"}, "identifier_to_merge": {"external_id": "u2"}}
        ],
    )

    assert captured["url"] == f"{_REST_ENDPOINT}/users/merge"
    assert len(captured["body"]["merge_updates"]) == 1
    assert captured["body"]["merge_updates"][0]["identifier_to_keep"]["external_id"] == "u1"
    assert result["message"] == "success"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_404_returns_error_dict(monkeypatch):
    def handler(url, headers, params):
        return _resp(404, {"message": "Not found"})

    conn = _connector_with_get(monkeypatch, handler)
    result = await conn.list_campaigns(_REST_ENDPOINT, _API_KEY)

    assert result["error"] is True
    assert result["status_code"] == 404


@pytest.mark.asyncio
async def test_429_returns_rate_limited_error_with_retry_after(monkeypatch):
    class FakeClient(_FakeClientBase):
        async def get(self, url, *, headers=None, params=None):
            return _resp(429, {"message": "Rate limited"}, extra_headers={"X-RateLimit-Reset": "1620000000"})

    monkeypatch.setattr("app.connectors.braze.httpx.AsyncClient", FakeClient)

    conn = BrazeConnector()
    result = await conn.list_campaigns(_REST_ENDPOINT, _API_KEY)

    assert result["error"] is True
    assert result["error_type"] == "rate_limited"
    assert result["status_code"] == 429
    assert result["retry_after"] == "1620000000"


@pytest.mark.asyncio
async def test_429_without_retry_after_header(monkeypatch):
    class FakeClient(_FakeClientBase):
        async def get(self, url, *, headers=None, params=None):
            return _resp(429, {"message": "Rate limited"})

    monkeypatch.setattr("app.connectors.braze.httpx.AsyncClient", FakeClient)

    conn = BrazeConnector()
    result = await conn.list_campaigns(_REST_ENDPOINT, _API_KEY)

    assert result["error"] is True
    assert result["error_type"] == "rate_limited"
    assert result["status_code"] == 429
    assert "retry_after" not in result


@pytest.mark.asyncio
async def test_401_returns_auth_error(monkeypatch):
    def handler(url, headers, params):
        return _resp(401, {"message": "Invalid API key"})

    conn = _connector_with_get(monkeypatch, handler)
    result = await conn.list_campaigns(_REST_ENDPOINT, _API_KEY)

    assert result["error"] is True
    assert result["status_code"] == 401


@pytest.mark.asyncio
async def test_500_returns_error_dict(monkeypatch):
    def handler(url, headers, params):
        return _resp(500, {"message": "Internal server error"})

    conn = _connector_with_get(monkeypatch, handler)
    result = await conn.list_campaigns(_REST_ENDPOINT, _API_KEY)

    assert result["error"] is True
    assert result["status_code"] == 500


@pytest.mark.asyncio
async def test_network_exception_returns_error_dict(monkeypatch):
    class FakeClient(_FakeClientBase):
        async def get(self, url, *, headers=None, params=None):
            msg = "Connection refused by Braze"
            raise ConnectionError(msg)

    monkeypatch.setattr("app.connectors.braze.httpx.AsyncClient", FakeClient)

    conn = BrazeConnector()
    result = await conn.list_campaigns(_REST_ENDPOINT, _API_KEY)

    assert result["error"] is True
    assert "Connection refused" in result["message"]
