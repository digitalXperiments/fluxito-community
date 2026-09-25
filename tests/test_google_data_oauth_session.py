"""
Regression tests for the Google data-connect OAuth flow and session safety.

Guards against the account-hijack bug where connecting a Google Suite data
source with a *different* Google account (B) while signed in as account A
silently switched the browser session to account B.

Two invariants are enforced:
  1. ``initiate_google_oauth`` records the *logged-in* user in the OAuth state
     (resolved from the signed ``uid`` cookie, not just an MCP bearer token),
     and refuses to start the flow when no one is signed in.
  2. ``google_data_callback`` binds the new connection to the logged-in user
     and NEVER re-issues the ``uid`` session cookie — connecting a data source
     must not change who you are, even when the connected Google account
     differs from the account you signed in with.
"""

import json
from types import SimpleNamespace

import pytest

from app.api import google_oauth_routes

USER_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"  # the signed-in user
EMAIL_B = "someone-else@gmail.com"  # the Google account being connected


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeRedis:
    def __init__(self):
        self.store = {}

    async def setex(self, key, ttl, value):
        self.store[key] = value

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)


class _FakeCredDB:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _fake_cred_factory():
    return _FakeCredDB()


async def _fake_get_creds(db, platform, project_id=None):
    return SimpleNamespace(client_id="cid", client_secret="secret")


# ---------------------------------------------------------------------------
# initiate_google_oauth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initiate_records_cookie_session_user(monkeypatch):
    """A browser session (uid cookie, no MCP token) must be recorded in state."""
    redis = FakeRedis()
    monkeypatch.setattr(google_oauth_routes.app_state, "redis_client", redis)
    monkeypatch.setattr(google_oauth_routes.app_state, "db_session_factory", _fake_cred_factory)
    monkeypatch.setattr("app.auth.oauth_app_credentials.get_oauth_app_credentials", _fake_get_creds)
    monkeypatch.setattr("app.utils.base_url_from_request", lambda request: "http://testserver")

    async def fake_resolve(request):
        return SimpleNamespace(user_id=USER_A, email="me@example.com")

    monkeypatch.setattr(google_oauth_routes, "_resolve_user_ctx", fake_resolve)

    request = SimpleNamespace(query_params={}, cookies={})
    resp = await google_oauth_routes.initiate_google_oauth(request, products="ga4")

    assert resp.status_code in (302, 307)
    assert "accounts.google.com" in resp.headers["location"]

    # Exactly one state entry, and it carries the logged-in user — not None.
    assert len(redis.store) == 1
    state_blob = next(iter(redis.store.values()))
    state = json.loads(state_blob)
    assert state["user_id"] == USER_A, "logged-in user must be recorded in OAuth state"


@pytest.mark.asyncio
async def test_initiate_requires_authentication(monkeypatch):
    """No session → redirect to /signin, never start an anonymous connect flow."""
    redis = FakeRedis()
    monkeypatch.setattr(google_oauth_routes.app_state, "redis_client", redis)

    async def fake_resolve(request):
        return None

    monkeypatch.setattr(google_oauth_routes, "_resolve_user_ctx", fake_resolve)

    request = SimpleNamespace(query_params={}, cookies={})
    resp = await google_oauth_routes.initiate_google_oauth(request, products="ga4")

    assert resp.status_code in (302, 307)
    assert "/signin" in resp.headers["location"]
    assert redis.store == {}, "no OAuth state should be created for an anonymous request"


# ---------------------------------------------------------------------------
# google_data_callback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_data_callback_does_not_switch_session(monkeypatch):
    """
    Connecting account B while signed in as account A must:
      - bind the connection to account A (the logged-in user), and
      - NOT set the uid cookie (session stays as A).
    """
    redis = FakeRedis()
    state_token = "state-token-123"
    await redis.setex(
        f"google_oauth_state:{state_token}",
        600,
        json.dumps(
            {
                "products": "ga4",
                "scopes": ["openid", "email", "https://www.googleapis.com/auth/analytics.readonly"],
                "user_id": USER_A,
                "project_id": "pppppppp-pppp-4ppp-8ppp-pppppppppppp",
                "base_url": "http://testserver",
            }
        ),
    )

    captured = []  # objects added to the DB session

    class FakeResult:
        def __init__(self, val):
            self._val = val

        def scalar_one_or_none(self):
            return self._val

    class FakeSession:
        def __init__(self):
            self._calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, stmt):
            self._calls += 1
            if self._calls == 1:
                # User lookup by id → return account A
                return FakeResult(SimpleNamespace(id=USER_A, email="me@example.com"))
            # Existing-connection lookup → none
            return FakeResult(None)

        def add(self, obj):
            captured.append(obj)

        async def flush(self):
            pass

        async def commit(self):
            pass

    monkeypatch.setattr(google_oauth_routes.app_state, "redis_client", redis)
    monkeypatch.setattr(google_oauth_routes.app_state, "db_session_factory", lambda: FakeSession())
    monkeypatch.setattr("app.auth.oauth_app_credentials.get_oauth_app_credentials", _fake_get_creds)

    # Fake Google HTTP: token exchange, userinfo (account B), and GTM probe.
    class FakeResp:
        def __init__(self, status, payload):
            self.status_code = status
            self._payload = payload
            self.text = json.dumps(payload)

        def json(self):
            return self._payload

    class FakeHttpClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, data=None, **kw):
            return FakeResp(200, {"access_token": "at", "refresh_token": "rt"})

        async def get(self, url, headers=None, **kw):
            if "userinfo" in url:
                return FakeResp(200, {"email": EMAIL_B, "name": "Account B"})
            return FakeResp(404, {})  # GTM discovery skipped

    monkeypatch.setattr(google_oauth_routes.httpx, "AsyncClient", lambda *a, **k: FakeHttpClient())

    # GA4 discovery returns nothing.
    monkeypatch.setattr(
        google_oauth_routes.app_state,
        "ga4_connector",
        SimpleNamespace(list_all_properties_raw=lambda token: _empty()),
    )

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(google_oauth_routes, "invalidate_user_context_cache", _noop)
    monkeypatch.setattr("app.notifications.create_notification", _noop)

    # The callback must be completed by the signed-in user who started the flow.
    from app.auth.uid_cookie import sign_uid

    async def _can_connect(*_a, **_k):
        return True

    monkeypatch.setattr("app.auth.connection_gate.can_connect", _can_connect)
    request = SimpleNamespace(
        headers={"host": "testserver", "x-forwarded-proto": "http"},
        cookies={"uid": sign_uid(USER_A)},
        url=SimpleNamespace(path="/auth/google/data/callback"),
    )
    resp = await google_oauth_routes.google_data_callback(
        request, code="auth-code", state=state_token, error=None
    )

    # Redirected back into the app...
    assert resp.status_code in (302, 307)

    # ...but the session was NOT replaced. No uid cookie may be set here.
    set_cookie_headers = [v for k, v in resp.raw_headers]
    cookie_blob = b" ".join(set_cookie_headers).decode(errors="ignore")
    assert "uid=" not in cookie_blob, "connect callback must not (re)issue the uid session cookie"

    # The connection was bound to the logged-in user A, with B's email recorded.
    conns = [c for c in captured if c.__class__.__name__ == "OAuthConnection"]
    assert len(conns) == 1
    assert str(conns[0].user_id) == USER_A
    assert conns[0].google_email == EMAIL_B


async def _empty():
    return []


@pytest.mark.asyncio
async def test_data_callback_stores_separate_row_per_user(monkeypatch):
    """
    Credentials are per-user. When the same Google account already has a row
    owned by a *different* user (B) in the same project, connecting as user A
    must INSERT a SEPARATE row for A (keyed by user_id, project, provider,
    google_email) — never reuse or clobber B's row. This is what lets two
    users hold their own credentials for the same external account.
    """
    redis = FakeRedis()
    state_token = "state-token-456"
    await redis.setex(
        f"google_oauth_state:{state_token}",
        600,
        json.dumps(
            {
                "products": "ga4",
                "scopes": ["openid", "email", "https://www.googleapis.com/auth/analytics.readonly"],
                "user_id": USER_A,
                "project_id": "pppppppp-pppp-4ppp-8ppp-pppppppppppp",
                "base_url": "http://testserver",
            }
        ),
    )

    added = []

    class FakeResult:
        def __init__(self, val):
            self._val = val

        def scalar_one_or_none(self):
            return self._val

    class FakeSession:
        def __init__(self):
            self._calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, stmt):
            self._calls += 1
            if self._calls == 1:
                return FakeResult(SimpleNamespace(id=USER_A, email="me@example.com"))
            # Existing-connection lookup is scoped to user A — B's row is not
            # visible to it, so the query returns nothing.
            return FakeResult(None)

        def add(self, obj):
            added.append(obj)

        async def flush(self):
            pass

        async def commit(self):
            pass

    monkeypatch.setattr(google_oauth_routes.app_state, "redis_client", redis)
    monkeypatch.setattr(google_oauth_routes.app_state, "db_session_factory", lambda: FakeSession())
    monkeypatch.setattr("app.auth.oauth_app_credentials.get_oauth_app_credentials", _fake_get_creds)

    class FakeResp:
        def __init__(self, status, payload):
            self.status_code = status
            self._payload = payload
            self.text = json.dumps(payload)

        def json(self):
            return self._payload

    class FakeHttpClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, data=None, **kw):
            return FakeResp(200, {"access_token": "at", "refresh_token": "rt"})

        async def get(self, url, headers=None, **kw):
            if "userinfo" in url:
                return FakeResp(200, {"email": EMAIL_B, "name": "Account B"})
            return FakeResp(404, {})

    monkeypatch.setattr(google_oauth_routes.httpx, "AsyncClient", lambda *a, **k: FakeHttpClient())
    monkeypatch.setattr(
        google_oauth_routes.app_state,
        "ga4_connector",
        SimpleNamespace(list_all_properties_raw=lambda token: _empty()),
    )

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(google_oauth_routes, "invalidate_user_context_cache", _noop)
    monkeypatch.setattr("app.notifications.create_notification", _noop)

    # The callback must be completed by the signed-in user who started the flow.
    from app.auth.uid_cookie import sign_uid

    async def _can_connect(*_a, **_k):
        return True

    monkeypatch.setattr("app.auth.connection_gate.can_connect", _can_connect)
    request = SimpleNamespace(
        headers={"host": "testserver", "x-forwarded-proto": "http"},
        cookies={"uid": sign_uid(USER_A)},
        url=SimpleNamespace(path="/auth/google/data/callback"),
    )
    resp = await google_oauth_routes.google_data_callback(
        request, code="auth-code", state=state_token, error=None
    )

    assert resp.status_code in (302, 307)
    # A brand-new row was inserted for user A (B's row is left untouched).
    conns = [c for c in added if c.__class__.__name__ == "OAuthConnection"]
    assert len(conns) == 1
    assert str(conns[0].user_id) == USER_A
    assert conns[0].google_email == EMAIL_B
    assert conns[0].provider == "google"
