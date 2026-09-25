"""
Tests for MCP OAuth authorization supporting email/password and existing browser sessions.

Covers the core of "Approach B" from the implementation plan:
- Detecting a valid browser (uid cookie) session during /oauth/authorize
- Showing a consent screen instead of forcing Google
- Creating real MCPAuthCodes from browser-authenticated users
- Falling back gracefully when the feature flag is disabled
"""

import json
from types import SimpleNamespace

import pytest
from starlette.responses import HTMLResponse

from app.auth import mcp_oauth_server


@pytest.fixture
def fake_redis():
    class FakeRedis:
        def __init__(self):
            self.store = {}

        async def setex(self, key, ttl, value):
            self.store[key] = value

        async def get(self, key):
            return self.store.get(key)

        async def delete(self, key):
            self.store.pop(key, None)

    return FakeRedis()


@pytest.fixture
def fake_db_session_factory():
    class FakeResult:
        def scalar_one_or_none(self):
            return None

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, stmt):
            return FakeResult()

        def add(self, obj):
            pass

        async def commit(self):
            pass

    return lambda: FakeSession()


@pytest.mark.asyncio
async def test_mcp_oauth_with_valid_browser_session_shows_consent(
    monkeypatch,
    fake_redis,
    fake_db_session_factory,
):
    """
    When a user has a valid browser session (signed uid cookie) and calls
    /oauth/authorize with proper PKCE params, they should see the consent
    screen instead of being redirected to Google.
    """
    fake_user_id = "11111111-1111-1111-1111-111111111111"

    monkeypatch.setattr(mcp_oauth_server.app_state, "redis_client", fake_redis)
    monkeypatch.setattr(mcp_oauth_server.app_state, "db_session_factory", fake_db_session_factory)

    async def fake_resolve_browser_session(request):
        return {
            "user_id": fake_user_id,
            "project_id": None,
            "email": "owner@example.com",
        }

    monkeypatch.setattr(
        mcp_oauth_server,
        "_resolve_browser_session_for_mcp_auth",
        fake_resolve_browser_session,
    )
    monkeypatch.setattr(
        "app.templating.render",
        lambda request, template, context, **kwargs: HTMLResponse("Allow access"),
    )

    request = SimpleNamespace(headers={"host": "testserver", "x-forwarded-proto": "http"})

    response = await mcp_oauth_server.authorize(
        request=request,
        response_type="code",
        client_id="test_mcp_client_123",
        redirect_uri="https://claude.ai/api/mcp/auth_callback",
        scope="read",
        state="test-state-abc",
        code_challenge="test-challenge",
        code_challenge_method="S256",
        resource=None,
    )

    assert response.status_code == 200
    assert b"Allow access" in response.body


@pytest.mark.asyncio
async def test_mcp_oauth_flag_disabled_preserves_old_behavior(
    monkeypatch,
    fake_redis,
    fake_db_session_factory,
):
    """
    When MCP_OAUTH_ALLOW_BROWSER_SESSION=False, the authorize handler must
    still force the Google identity path (original behavior).
    """
    monkeypatch.setattr(mcp_oauth_server.app_state, "redis_client", fake_redis)
    monkeypatch.setattr(mcp_oauth_server.app_state, "db_session_factory", fake_db_session_factory)
    monkeypatch.setattr(mcp_oauth_server.settings, "MCP_OAUTH_ALLOW_BROWSER_SESSION", False)

    async def fake_get_oauth_app_credentials(db, platform):
        return SimpleNamespace(client_id="google-client-id")

    monkeypatch.setattr(
        "app.auth.oauth_app_credentials.get_oauth_app_credentials",
        fake_get_oauth_app_credentials,
    )

    request = SimpleNamespace(headers={"host": "testserver", "x-forwarded-proto": "http"})

    response = await mcp_oauth_server.authorize(
        request=request,
        response_type="code",
        client_id="test_mcp_client_456",
        redirect_uri="https://claude.ai/api/mcp/auth_callback",
        scope="read",
        state="test-state-def",
        code_challenge="test-challenge-2",
        code_challenge_method="S256",
        resource=None,
    )

    assert response.status_code == 302
    assert "accounts.google.com" in response.headers["location"]


@pytest.mark.asyncio
async def test_mcp_oauth_consent_redirects_to_client_callback_with_get_semantics(
    monkeypatch,
    fake_redis,
):
    """
    Consent is submitted as POST, but OAuth client callbacks expect a GET.
    Starlette defaults RedirectResponse to 307, which preserves POST and
    causes Claude's callback endpoint to return Method Not Allowed.
    """
    monkeypatch.setattr(mcp_oauth_server.app_state, "redis_client", fake_redis)

    state = "test-state-ghi"
    await fake_redis.setex(
        f"mcp_oauth_state:{state}",
        600,
        """
        {
          "client_id": "test_mcp_client_789",
          "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
          "scope": "read write",
          "state": "test-state-ghi",
          "code_challenge": "test-challenge-3",
          "code_challenge_method": "S256",
          "user_id": "11111111-1111-1111-1111-111111111111",
          "consent_nonce": "nonce-123"
        }
        """,
    )

    async def fake_issue_mcp_auth_code(**kwargs):
        return "issued-code"

    monkeypatch.setattr(mcp_oauth_server, "_issue_mcp_auth_code", fake_issue_mcp_auth_code)

    from app.auth.uid_cookie import sign_uid

    # Same browser that saw the consent page: signed in as the user, with its nonce.
    response = await mcp_oauth_server.authorize_decision(
        request=SimpleNamespace(cookies={"uid": sign_uid("11111111-1111-1111-1111-111111111111")}),
        state=state,
        consent="allow",
        consent_nonce="nonce-123",
    )

    assert response.status_code == 303
    assert response.headers["location"] == (
        "https://claude.ai/api/mcp/auth_callback" "?code=issued-code&state=test-state-ghi&scope=read+write"
    )


@pytest.mark.asyncio
async def test_mcp_oauth_consent_rejects_a_post_from_another_browser(monkeypatch, fake_redis):
    """Holding the state isn't enough: the decision must carry the consent page's
    nonce and come from the same signed-in user, or anyone could approve it."""
    from app.auth.uid_cookie import sign_uid

    monkeypatch.setattr(mcp_oauth_server.app_state, "redis_client", fake_redis)
    victim = "11111111-1111-1111-1111-111111111111"
    for state, cookies, nonce in (
        ("s-no-cookie", {}, "nonce-1"),
        ("s-other-user", {"uid": sign_uid("22222222-2222-2222-2222-222222222222")}, "nonce-1"),
        ("s-bad-nonce", {"uid": sign_uid(victim)}, "wrong"),
    ):
        await fake_redis.setex(
            f"mcp_oauth_state:{state}",
            600,
            json.dumps(
                {
                    "client_id": "c",
                    "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                    "scope": "read",
                    "state": state,
                    "code_challenge": "x",
                    "code_challenge_method": "S256",
                    "user_id": victim,
                    "consent_nonce": "nonce-1",
                }
            ),
        )
        response = await mcp_oauth_server.authorize_decision(
            request=SimpleNamespace(cookies=cookies), state=state, consent="allow", consent_nonce=nonce
        )
        assert response.status_code == 403, state
