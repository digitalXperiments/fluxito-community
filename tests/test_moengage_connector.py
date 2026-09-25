"""
Tests for MoengageConnector.

Mirrors the style of test_adobe_marketo_connector.py:
  - monkeypatch httpx.AsyncClient with an async-context-manager shim
  - assert correct URLs, headers, params, body, and return shapes
  - all HTTP is faked (no real MoEngage API calls)
  - verifies Basic auth for Data/Inform APIs and signature auth for Push API
"""

import base64
import json

import pytest

from app.connectors.moengage import MoengageConnector

_DATA_CENTER = "01"
_APP_ID = "moengage-app-id"
_API_KEY = "moengage-api-key"


class _FakeClientBase:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url, *, headers=None, params=None):
        raise NotImplementedError("get not stubbed for this test")


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


def _connector_with_post(monkeypatch, post_handler):
    """Return a MoengageConnector whose POST requests are served by post_handler."""

    class FakeClient(_FakeClientBase):
        async def post(self, url, *, headers=None, params=None, json=None):
            return post_handler(url, headers, params, json)

    monkeypatch.setattr("app.connectors.moengage.httpx.AsyncClient", FakeClient)
    return MoengageConnector()


# ---------------------------------------------------------------------------
# Auth & request shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_user_info_sends_basic_auth_header(monkeypatch):
    captured = {}

    def handler(url, headers, params, body):
        captured["url"] = url
        captured["headers"] = headers or {}
        captured["body"] = body
        called_handler["called"] = True
        return _resp(200, {"status": "success", "customers": [{"customer_id": "u1"}]})

    called_handler = {"called": False}
    conn = _connector_with_post(monkeypatch, handler)
    result = await conn.get_user_info(
        _DATA_CENTER,
        _APP_ID,
        _API_KEY,
        customer_id="u1",
    )

    assert called_handler["called"]
    auth = captured["headers"].get("Authorization", "")
    assert auth.startswith("Basic ")
    decoded = base64.b64decode(auth.removeprefix("Basic ")).decode()
    assert decoded == f"{_APP_ID}:{_API_KEY}"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert result["customers"][0]["customer_id"] == "u1"


@pytest.mark.asyncio
async def test_base_url_uses_data_center(monkeypatch):
    captured = {}

    def handler(url, headers, params, body):
        captured["url"] = url
        return _resp(200, {"status": "success"})

    conn = _connector_with_post(monkeypatch, handler)
    await conn.list_campaigns(_DATA_CENTER, _APP_ID, _API_KEY)

    assert f"api-{_DATA_CENTER}.moengage.com" in captured["url"]


@pytest.mark.asyncio
async def test_list_campaigns_passes_channel_param(monkeypatch):
    captured = {}

    def handler(url, headers, params, body):
        captured["url"] = url
        captured["body"] = body
        return _resp(200, {"status": "success", "campaigns": []})

    conn = _connector_with_post(monkeypatch, handler)
    await conn.list_campaigns(_DATA_CENTER, _APP_ID, _API_KEY, channel="email")

    assert "/campaigns/search" in captured["url"]
    assert captured["body"]["channel"] == "email"
    assert captured["body"]["limit"] == 50


# ---------------------------------------------------------------------------
# Push API (separate host + signature auth)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_push_uses_pushapi_host(monkeypatch):
    captured = {}

    class FakeClient(_FakeClientBase):
        async def post(self, url, *, headers=None, params=None, json=None):
            captured["url"] = url
            captured["headers"] = headers or {}
            captured["body"] = json or {}
            return _resp(200, {"status": "success"})

    monkeypatch.setattr("app.connectors.moengage.httpx.AsyncClient", FakeClient)

    conn = MoengageConnector()
    await conn.send_push(
        _DATA_CENTER,
        _APP_ID,
        _API_KEY,
        campaign_name="Test Push",
        target_platform=["android"],
        payload={"message": "Hello"},
    )

    assert f"pushapi-{_DATA_CENTER}.moengage.com" in captured["url"]
    assert "/v2/transaction/sendpush" in captured["url"]


@pytest.mark.asyncio
async def test_send_push_omits_basic_auth_header(monkeypatch):
    captured = {}

    class FakeClient(_FakeClientBase):
        async def post(self, url, *, headers=None, params=None, json=None):
            captured["headers"] = headers or {}
            captured["body"] = json or {}
            captured["url"] = url
            return _resp(200, {"status": "success"})

    monkeypatch.setattr("app.connectors.moengage.httpx.AsyncClient", FakeClient)

    conn = MoengageConnector()
    await conn.send_push(
        _DATA_CENTER,
        _APP_ID,
        _API_KEY,
        campaign_name="Test Push",
        target_platform=["ios"],
        payload={"alert": "Hello iOS"},
    )

    # Push API authenticates via signature in body, not Basic header
    auth = captured["headers"].get("Authorization")
    assert auth is None or not auth.startswith("Basic")


@pytest.mark.asyncio
async def test_send_push_includes_signature_in_body(monkeypatch):
    captured = {}

    class FakeClient(_FakeClientBase):
        async def post(self, url, *, headers=None, params=None, json=None):
            captured["body"] = json or {}
            captured["url"] = url
            return _resp(200, {"status": "success"})

    monkeypatch.setattr("app.connectors.moengage.httpx.AsyncClient", FakeClient)

    conn = MoengageConnector()
    await conn.send_push(
        _DATA_CENTER,
        _APP_ID,
        _API_KEY,
        campaign_name="Test Campaign",
        target_platform=["android", "ios"],
        payload={"title": "Offer", "body": "Check this out"},
    )

    assert "signature" in captured["body"]
    # Verify signature value matches the connector's computation
    expected_sig = conn._compute_push_signature(_APP_ID, "Test Campaign", _API_KEY)
    assert captured["body"]["signature"] == expected_sig
    assert captured["body"]["appId"] == _APP_ID
    assert captured["body"]["campaignName"] == "Test Campaign"
    assert captured["body"]["targetPlatform"] == ["android", "ios"]


# ---------------------------------------------------------------------------
# Happy path — per method
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_campaign_details_happy_path(monkeypatch):
    captured = {}

    def handler(url, headers, params, body):
        captured["url"] = url
        captured["body"] = body
        return _resp(200, {"status": "success", "campaigns": [{"id": "c1", "name": "Welcome"}]})

    conn = _connector_with_post(monkeypatch, handler)
    result = await conn.get_campaign_details(
        _DATA_CENTER,
        _APP_ID,
        _API_KEY,
        campaign_id="c1",
    )

    assert "/campaigns/search" in captured["url"]
    assert captured["body"]["campaign_ids"] == ["c1"]
    assert result["campaigns"][0]["id"] == "c1"


@pytest.mark.asyncio
async def test_create_user_posts_to_customer_endpoint(monkeypatch):
    captured = {}

    def handler(url, headers, params, body):
        captured["url"] = url
        captured["body"] = body
        return _resp(200, {"status": "success", "customer_id": "u1"})

    conn = _connector_with_post(monkeypatch, handler)
    result = await conn.create_user(
        _DATA_CENTER,
        _APP_ID,
        _API_KEY,
        customer_id="u1",
        attributes={"name": "Alice", "email": "alice@example.com"},
    )

    assert f"/v1/customer/{_APP_ID}" in captured["url"]
    assert captured["body"]["type"] == "customer"
    assert captured["body"]["customer_id"] == "u1"
    assert captured["body"]["attributes"]["name"] == "Alice"
    assert result["customer_id"] == "u1"


@pytest.mark.asyncio
async def test_update_user_posts_to_customer_endpoint(monkeypatch):
    captured = {}

    def handler(url, headers, params, body):
        captured["url"] = url
        captured["body"] = body
        return _resp(200, {"status": "success"})

    conn = _connector_with_post(monkeypatch, handler)
    result = await conn.update_user(
        _DATA_CENTER,
        _APP_ID,
        _API_KEY,
        customer_id="u1",
        attributes={"name": "Alice Updated"},
    )

    # update_user delegates to create_user with update_existing_only=True
    assert f"/v1/customer/{_APP_ID}" in captured["url"]
    assert captured["body"]["customer_id"] == "u1"
    assert captured["body"]["update_existing_only"] is True
    assert captured["body"]["attributes"]["name"] == "Alice Updated"
    assert result["status"] == "success"


@pytest.mark.asyncio
async def test_add_device_posts_to_device_endpoint(monkeypatch):
    captured = {}

    def handler(url, headers, params, body):
        captured["url"] = url
        captured["body"] = body
        return _resp(200, {"status": "success"})

    conn = _connector_with_post(monkeypatch, handler)
    result = await conn.add_device(
        _DATA_CENTER,
        _APP_ID,
        _API_KEY,
        device={"platform": "android", "push_id": "fcm-token-123", "customer_id": "u1"},
    )

    assert f"/v1/device/{_APP_ID}" in captured["url"]
    assert captured["body"]["type"] == "device"
    assert captured["body"]["platform"] == "android"
    assert captured["body"]["push_id"] == "fcm-token-123"
    assert result["status"] == "success"


@pytest.mark.asyncio
async def test_send_email_happy_path(monkeypatch):
    captured = {}

    def handler(url, headers, params, body):
        captured["url"] = url
        captured["body"] = body
        return _resp(200, {"status": "success"})

    conn = _connector_with_post(monkeypatch, handler)
    result = await conn.send_email(
        _DATA_CENTER,
        _APP_ID,
        _API_KEY,
        transaction_id="tx-1",
        recipients={"email": "alice@example.com"},
        alert_id="alert-1",
        personalization={"name": "Alice"},
    )

    assert "/alerts/send" in captured["url"]
    assert captured["body"]["transaction_id"] == "tx-1"
    assert captured["body"]["recipients"] == {"email": "alice@example.com"}
    assert captured["body"]["alert_id"] == "alert-1"
    assert captured["body"]["personalization"] == {"name": "Alice"}
    assert result["status"] == "success"


@pytest.mark.asyncio
async def test_send_sms_happy_path(monkeypatch):
    captured = {}

    def handler(url, headers, params, body):
        captured["url"] = url
        captured["body"] = body
        return _resp(200, {"status": "success"})

    conn = _connector_with_post(monkeypatch, handler)
    result = await conn.send_sms(
        _DATA_CENTER,
        _APP_ID,
        _API_KEY,
        transaction_id="tx-2",
        recipients={"phone": "+1234567890"},
        alert_id="alert-sms-1",
        personalization={"code": "1234"},
    )

    # send_sms delegates to send_email
    assert "/alerts/send" in captured["url"]
    assert captured["body"]["transaction_id"] == "tx-2"
    assert captured["body"]["recipients"] == {"phone": "+1234567890"}
    assert result["status"] == "success"


@pytest.mark.asyncio
async def test_list_events_returns_empty_list(monkeypatch):
    """list_events does not make any HTTP call — it returns a static response."""
    called = {"get": False, "post": False}

    class FakeClient(_FakeClientBase):
        async def get(self, url, *, headers=None, params=None):
            called["get"] = True
            return _resp(200, {})

        async def post(self, url, *, headers=None, params=None, json=None):
            called["post"] = True
            return _resp(200, {})

    monkeypatch.setattr("app.connectors.moengage.httpx.AsyncClient", FakeClient)

    conn = MoengageConnector()
    result = await conn.list_events(_DATA_CENTER, _APP_ID, _API_KEY)

    assert called["get"] is False
    assert called["post"] is False
    assert result["error"] is False
    assert result["events"] == []
    assert "note" in result


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_429_returns_rate_limited_error(monkeypatch):
    def handler(url, headers, params, body):
        return _resp(429, {"message": "Rate limited"})

    conn = _connector_with_post(monkeypatch, handler)
    result = await conn.list_campaigns(_DATA_CENTER, _APP_ID, _API_KEY)

    assert result["error"] is True
    assert result["error_type"] == "rate_limited"
    assert result["status_code"] == 429


@pytest.mark.asyncio
async def test_401_returns_auth_error(monkeypatch):
    def handler(url, headers, params, body):
        return _resp(401, {"message": "Unauthorized"})

    conn = _connector_with_post(monkeypatch, handler)
    result = await conn.list_campaigns(_DATA_CENTER, _APP_ID, _API_KEY)

    assert result["error"] is True
    assert result["status_code"] == 401


@pytest.mark.asyncio
async def test_404_returns_error_dict(monkeypatch):
    def handler(url, headers, params, body):
        return _resp(404, {"message": "Not found"})

    conn = _connector_with_post(monkeypatch, handler)
    result = await conn.list_campaigns(_DATA_CENTER, _APP_ID, _API_KEY)

    assert result["error"] is True
    assert result["status_code"] == 404


@pytest.mark.asyncio
async def test_500_returns_error_dict(monkeypatch):
    def handler(url, headers, params, body):
        return _resp(500, {"message": "Internal server error"})

    conn = _connector_with_post(monkeypatch, handler)
    result = await conn.list_campaigns(_DATA_CENTER, _APP_ID, _API_KEY)

    assert result["error"] is True
    assert result["status_code"] == 500


@pytest.mark.asyncio
async def test_network_exception_returns_error_dict(monkeypatch):
    class FakeClient(_FakeClientBase):
        async def post(self, url, *, headers=None, params=None, json=None):
            msg = "Connection refused by MoEngage"
            raise ConnectionError(msg)

    monkeypatch.setattr("app.connectors.moengage.httpx.AsyncClient", FakeClient)

    conn = MoengageConnector()
    result = await conn.list_campaigns(_DATA_CENTER, _APP_ID, _API_KEY)

    assert result["error"] is True
    assert "Connection refused" in result["message"]


@pytest.mark.asyncio
async def test_api_fail_status_returns_error_dict(monkeypatch):
    """MoEngage Data APIs return HTTP 200 with {"status": "fail", "error": {...}}."""

    def handler(url, headers, params, body):
        return _resp(
            200,
            {
                "status": "fail",
                "error": {"message": "Campaign not found", "code": 404},
            },
        )

    conn = _connector_with_post(monkeypatch, handler)
    result = await conn.list_campaigns(_DATA_CENTER, _APP_ID, _API_KEY)

    assert result["error"] is True
    assert "Campaign not found" in result["message"]
    assert result["error_details"]["code"] == 404
