import base64
import json
import urllib.parse
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import InvalidToken
from starlette.requests import Request

import app.app_state as app_state
from app.api.auth_routes import (
    LoginRequest,
    RegisterRequest,
    login,
    register,
    verify_email_page,
)
from app.api.google_oauth_routes import (
    google_start,
    home,
    save_onboarding_preferences,
    signin,
    signin_callback,
    signout,
    tutorial_complete,
    tutorial_page,
)
from app.auth.oauth_app_credentials import OAuthAppNotConfigured


class FakeRedis:
    def __init__(self, data=None):
        self.store = dict(data or {})

    async def setex(self, key, ttl, value):
        self.store[key] = value

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)


def _make_fake_id_token(email="user@example.com", name="Test User"):
    header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').decode().rstrip("=")
    payload = (
        base64.urlsafe_b64encode(json.dumps({"email": email, "email_verified": True, "name": name}).encode())
        .decode()
        .rstrip("=")
    )
    return f"{header}.{payload}.sig"


def _make_request(method="GET", path="/", query_string=b"", cookies=None, json_body=None):
    headers = []
    if cookies:
        cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
        headers.append((b"cookie", cookie_header.encode()))
    if json_body is not None:
        headers.append((b"content-type", b"application/json"))

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": query_string,
        "headers": headers,
        "server": ("testserver", 80),
    }
    req = Request(scope)
    if json_body is not None:
        req._json = json_body
    return req


@asynccontextmanager
async def _fake_db_factory():
    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_result.scalars.return_value.all.return_value = []
    mock_result.scalars.return_value.first.return_value = None
    mock_result.first.return_value = None
    mock_result.scalar.return_value = None
    mock_db.execute = AsyncMock(return_value=mock_result)
    mock_db.commit = AsyncMock()
    yield mock_db


@pytest.fixture(autouse=True)
def _setup_test_env():
    original_db = app_state.db_session_factory
    app_state.db_session_factory = _fake_db_factory
    yield
    app_state.db_session_factory = original_db


# ---------------------------------------------------------------------------
# Sign-in Flow Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signin_invalid_credentials_returns_401():
    req = _make_request("POST", "/auth/login")
    with (
        patch(
            "app.settings_service.get_auth_flags",
            new=AsyncMock(
                return_value={"google_enabled": True, "password_enabled": True, "signup_enabled": True}
            ),
        ),
        patch(
            "app.api.auth_routes.authenticate_user",
            new=AsyncMock(return_value=(None, "Invalid email or password.")),
        ),
    ):
        resp = await login(LoginRequest(email="user@example.com", password="badpassword"), req)
        assert resp.status_code == 401
        data = json.loads(resp.body.decode())
        assert "Invalid" in data["error"]


@pytest.mark.asyncio
async def test_signin_unverified_email_returns_403_with_flag():
    req = _make_request("POST", "/auth/login")
    with (
        patch(
            "app.settings_service.get_auth_flags",
            new=AsyncMock(
                return_value={"google_enabled": True, "password_enabled": True, "signup_enabled": True}
            ),
        ),
        patch(
            "app.api.auth_routes.authenticate_user",
            new=AsyncMock(return_value=(None, "UNVERIFIED")),
        ),
    ):
        resp = await login(LoginRequest(email="unverified@example.com", password="password123"), req)
        assert resp.status_code == 403
        data = json.loads(resp.body.decode())
        assert data["unverified"] is True
        assert data["email"] == "unverified@example.com"


@pytest.mark.asyncio
async def test_signin_new_user_redirects_to_tutorial():
    """A user who has not completed the tutorial (tutorial_completed_at is None)
    must receive redirect_url = '/tutorial' and a signed uid cookie."""
    req = _make_request("POST", "/auth/login")
    mock_user = SimpleNamespace(
        id=uuid.uuid4(),
        email="newuser@example.com",
        display_name="New User",
        tutorial_completed_at=None,
    )
    with (
        patch(
            "app.settings_service.get_auth_flags",
            new=AsyncMock(
                return_value={"google_enabled": True, "password_enabled": True, "signup_enabled": True}
            ),
        ),
        patch(
            "app.api.auth_routes.authenticate_user",
            new=AsyncMock(return_value=(mock_user, None)),
        ),
    ):
        resp = await login(LoginRequest(email="newuser@example.com", password="password123"), req)
        assert resp.status_code == 200
        data = json.loads(resp.body.decode())
        assert data["success"] is True
        assert data["redirect_url"] == "/tutorial"
        # Verify signed uid cookie is set
        set_cookie = resp.headers.get("set-cookie", "")
        assert "uid=" in set_cookie


@pytest.mark.asyncio
async def test_signin_existing_user_redirects_to_home_and_ensures_project():
    """A user who already completed onboarding goes to /home and ensures a default project exists."""
    req = _make_request("POST", "/auth/login")
    mock_user = SimpleNamespace(
        id=uuid.uuid4(),
        email="existing@example.com",
        display_name="Existing User",
        tutorial_completed_at=datetime.utcnow(),
    )
    ensure_mock = AsyncMock(return_value=True)
    with (
        patch(
            "app.settings_service.get_auth_flags",
            new=AsyncMock(
                return_value={"google_enabled": True, "password_enabled": True, "signup_enabled": True}
            ),
        ),
        patch(
            "app.api.auth_routes.authenticate_user",
            new=AsyncMock(return_value=(mock_user, None)),
        ),
        patch("app.api.project_routes.ensure_default_project", new=ensure_mock),
    ):
        resp = await login(LoginRequest(email="existing@example.com", password="password123"), req)
        assert resp.status_code == 200
        data = json.loads(resp.body.decode())
        assert data["success"] is True
        assert data["redirect_url"] == "/home"
        ensure_mock.assert_awaited_once_with(mock_user.id, mock_user.display_name, mock_user.email)


# ---------------------------------------------------------------------------
# Onboarding / Tutorial Flow Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tutorial_unauthenticated_redirects_to_signin():
    req = _make_request("GET", "/tutorial")
    with patch("app.api.google_oauth_routes._resolve_user_ctx", new=AsyncMock(return_value=None)):
        resp = await tutorial_page(req)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/signin?next=/tutorial"


@pytest.mark.asyncio
async def test_tutorial_new_user_renders_onboarding_steps():
    """A user without completed tutorial sees the onboarding wizard steps."""
    user_id = str(uuid.uuid4())
    req = _make_request("GET", "/tutorial")
    mock_ctx = SimpleNamespace(
        user_id=user_id,
        email="fresh@example.com",
        display_name="Fresh Tester",
        connections=[],
    )

    with (
        patch("app.api.google_oauth_routes._resolve_user_ctx", new=AsyncMock(return_value=mock_ctx)),
        patch("app.api.google_oauth_routes.ensure_active_project", new=AsyncMock(return_value=None)),
    ):
        resp = await tutorial_page(req)
        assert resp.status_code == 200
        html = resp.body.decode()
        assert "Step 1 · Welcome" in html
        assert "Step 2 · Connect your AI agent" in html or "Step 2 · Choose AI Engine" in html
        assert "Step 3 · Create Workspace" in html
        assert "Step 4 · Connect your data" in html
        assert "Step 5 · Health check" in html
        assert "Fresh" in html


@pytest.mark.asyncio
async def test_tutorial_already_completed_redirects_to_home():
    """A user who has already completed the tutorial visiting /tutorial gets redirected to /home."""
    user_id = str(uuid.uuid4())
    req = _make_request("GET", "/tutorial")
    mock_ctx = SimpleNamespace(
        user_id=user_id,
        email="done@example.com",
        display_name="Done User",
        connections=[],
    )
    mock_user = SimpleNamespace(
        id=uuid.UUID(user_id),
        tutorial_completed_at=datetime.utcnow(),
        flux_role=None,
        flux_monitors=[],
        preferred_ai_client=None,
    )

    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.first.return_value = None
    mock_result.scalar_one_or_none.return_value = mock_user
    mock_result.scalars.return_value.all.return_value = []
    mock_db.execute = AsyncMock(return_value=mock_result)

    @asynccontextmanager
    async def _user_db():
        yield mock_db

    with (
        patch("app.api.google_oauth_routes._resolve_user_ctx", new=AsyncMock(return_value=mock_ctx)),
        patch("app.app_state.db_session_factory", _user_db),
    ):
        resp = await tutorial_page(req)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/home"


@pytest.mark.asyncio
async def test_tutorial_complete_endpoint_marks_user_and_sets_cookie():
    """POST /api/tutorial/complete ensures a workspace, marks completed_at, and sets cookie."""
    user_id = str(uuid.uuid4())
    proj_id = str(uuid.uuid4())
    req = _make_request("POST", "/api/tutorial/complete")
    mock_ctx = SimpleNamespace(
        user_id=user_id,
        email="done@example.com",
        display_name="Done User",
        connections=[],
    )

    with (
        patch("app.api.google_oauth_routes._resolve_user_ctx", new=AsyncMock(return_value=mock_ctx)),
        patch("app.api.google_oauth_routes.ensure_active_project", new=AsyncMock(return_value=proj_id)),
    ):
        resp = await tutorial_complete(req)
        assert resp.status_code == 200
        data = json.loads(resp.body.decode())
        assert data["ok"] is True
        # Check active project cookie
        set_cookie = resp.headers.get("set-cookie", "")
        assert "active_project_id=" in set_cookie


@pytest.mark.asyncio
async def test_onboarding_preferences_endpoint():
    """POST /api/onboarding/preferences persists choices."""
    user_id = str(uuid.uuid4())
    req = _make_request(
        "POST",
        "/api/onboarding/preferences",
        json_body={"role": "run_marketing", "monitors": ["roas", "health"], "ai_client": "mcp"},
    )
    mock_ctx = SimpleNamespace(
        user_id=user_id,
        email="prefs@example.com",
        display_name="Prefs User",
        connections=[],
    )

    with patch("app.api.google_oauth_routes._resolve_user_ctx", new=AsyncMock(return_value=mock_ctx)):
        resp = await save_onboarding_preferences(req)
        assert resp.status_code == 200
        assert json.loads(resp.body.decode())["ok"] is True


# ---------------------------------------------------------------------------
# Home Dashboard Flow Tests (No Redirect Loops)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_home_unauthenticated_redirects_to_signin():
    req = _make_request("GET", "/home")
    with patch("app.api.google_oauth_routes._resolve_user_ctx", new=AsyncMock(return_value=None)):
        resp = await home(req)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/signin?next=/home"


@pytest.mark.asyncio
async def test_home_user_without_tutorial_redirects_to_tutorial():
    """If tutorial_completed_at is None, /home redirects to /tutorial."""
    user_id = str(uuid.uuid4())
    req = _make_request("GET", "/home")
    mock_ctx = SimpleNamespace(user_id=user_id, email="u@example.com", connections=[])
    mock_user = SimpleNamespace(id=uuid.UUID(user_id), tutorial_completed_at=None)

    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_user
    mock_db.execute = AsyncMock(return_value=mock_result)

    @asynccontextmanager
    async def _user_db():
        yield mock_db

    with (
        patch("app.api.google_oauth_routes._resolve_user_ctx", new=AsyncMock(return_value=mock_ctx)),
        patch("app.app_state.db_session_factory", _user_db),
    ):
        resp = await home(req)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/tutorial"


@pytest.mark.asyncio
async def test_home_completed_tutorial_without_project_creates_default_and_renders():
    """Guards against /home <-> /tutorial infinite redirect loop.
    When an onboarded user has no active project, ensure_default_project creates one."""
    user_id = str(uuid.uuid4())
    proj_id = str(uuid.uuid4())
    req = _make_request("GET", "/home")
    mock_ctx = SimpleNamespace(
        user_id=user_id,
        email="test@example.com",
        display_name="Tester",
        connections=[],
    )
    mock_user = SimpleNamespace(id=uuid.UUID(user_id), tutorial_completed_at=datetime.utcnow())

    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_user
    mock_result.scalars.return_value.all.return_value = []
    mock_db.execute = AsyncMock(return_value=mock_result)

    @asynccontextmanager
    async def _user_db():
        yield mock_db

    ensure_default_mock = AsyncMock(return_value=True)

    call_count = 0

    async def fake_ensure_active(req, uid):
        nonlocal call_count
        call_count += 1
        return None if call_count == 1 else proj_id

    ensure_active_mock = AsyncMock(side_effect=fake_ensure_active)

    with (
        patch("app.api.google_oauth_routes._resolve_user_ctx", new=AsyncMock(return_value=mock_ctx)),
        patch("app.app_state.db_session_factory", _user_db),
        patch("app.api.google_oauth_routes.ensure_active_project", new=ensure_active_mock),
        patch("app.api.project_routes.ensure_default_project", new=ensure_default_mock),
    ):
        resp = await home(req)
        assert resp.status_code == 200
        assert ensure_default_mock.await_count == 1


# ---------------------------------------------------------------------------
# Registration & Email Verification Flow Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_invalid_email():
    req = _make_request("POST", "/auth/register")
    resp = await register(RegisterRequest(email="invalidemail", password="password123"), req)
    assert resp.status_code == 400
    assert "valid email" in json.loads(resp.body.decode())["error"]


@pytest.mark.asyncio
async def test_register_short_password():
    req = _make_request("POST", "/auth/register")
    resp = await register(RegisterRequest(email="valid@example.com", password="short"), req)
    assert resp.status_code == 400
    assert "at least 8 characters" in json.loads(resp.body.decode())["error"]


@pytest.mark.asyncio
async def test_register_success_dispatches_email_and_redirects_to_verify_sent():
    req = _make_request("POST", "/auth/register")
    mock_user = SimpleNamespace(id=uuid.uuid4(), email="new@example.com", display_name="New")
    with (
        patch(
            "app.settings_service.get_auth_flags",
            new=AsyncMock(
                return_value={"google_enabled": True, "password_enabled": True, "signup_enabled": True}
            ),
        ),
        patch("app.settings_service.access_approval_required", new=AsyncMock(return_value=False)),
        patch("app.api.auth_routes.register_user", new=AsyncMock(return_value=(mock_user, None))),
        patch("app.api.auth_routes.send_verification_email", new=AsyncMock()) as send_email_mock,
    ):
        resp = await register(RegisterRequest(email="new@example.com", password="securepassword123"), req)
        assert resp.status_code == 200
        data = json.loads(resp.body.decode())
        assert data["success"] is True
        assert "/auth/verify-email-sent" in data["redirect_url"]
        send_email_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_verify_email_missing_token_renders_error():
    req = _make_request("GET", "/auth/verify-email")
    resp = await verify_email_page(req, token="")
    assert resp.status_code == 200
    assert "Missing verification token" in resp.body.decode()


@pytest.mark.asyncio
async def test_verify_email_valid_token_marks_verified_and_shows_success():
    req = _make_request("GET", "/auth/verify-email")
    with patch("app.api.auth_routes.verify_user_email", new=AsyncMock(return_value=(True, None))):
        resp = await verify_email_page(req, token="valid-tok")
        assert resp.status_code == 200
        html = resp.body.decode()
        assert "Email verified" in html or "Sign in" in html


# ---------------------------------------------------------------------------
# Google Sign-in Page & OAuth Flow Tests
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _not_first_run_db():
    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar.return_value = True
    mock_result.scalar_one_or_none.return_value = None
    mock_db.execute = AsyncMock(return_value=mock_result)
    yield mock_db


@pytest.mark.asyncio
async def test_signin_page_google_only_renders_correctly():
    req = _make_request("GET", "/signin")
    with (
        patch("app.api.google_oauth_routes._resolve_user_ctx", new=AsyncMock(return_value=None)),
        patch("app.app_state.db_session_factory", _not_first_run_db),
        patch(
            "app.auth.oauth_app_credentials.get_oauth_app_credentials",
            new=AsyncMock(return_value=SimpleNamespace(client_id="g-client-id")),
        ),
        patch(
            "app.settings_service.get_auth_flags",
            new=AsyncMock(
                return_value={"google_enabled": True, "password_enabled": False, "signup_enabled": False}
            ),
        ),
    ):
        resp = await signin(req)
        assert resp.status_code == 200
        html = resp.body.decode()
        assert "Continue with Google" in html
        assert "Sign in or get started with your Google account" in html
        # Password inputs and mode toggle should not be rendered
        assert 'id="authToggle"' not in html
        assert 'id="signinForm"' not in html


@pytest.mark.asyncio
async def test_signin_page_displays_gated_banner():
    req = _make_request("GET", "/signin?gated=1")
    with (
        patch("app.api.google_oauth_routes._resolve_user_ctx", new=AsyncMock(return_value=None)),
        patch("app.app_state.db_session_factory", _not_first_run_db),
        patch(
            "app.settings_service.get_auth_flags",
            new=AsyncMock(
                return_value={"google_enabled": True, "password_enabled": False, "signup_enabled": False}
            ),
        ),
    ):
        resp = await signin(req, gated="1")
        assert resp.status_code == 200
        html = resp.body.decode()
        assert "Access approval required" in html


@pytest.mark.asyncio
async def test_signin_page_displays_google_not_configured_error():
    req = _make_request("GET", "/signin?error=google_not_configured")
    with (
        patch("app.api.google_oauth_routes._resolve_user_ctx", new=AsyncMock(return_value=None)),
        patch("app.app_state.db_session_factory", _not_first_run_db),
        patch(
            "app.settings_service.get_auth_flags",
            new=AsyncMock(
                return_value={"google_enabled": True, "password_enabled": True, "signup_enabled": True}
            ),
        ),
    ):
        resp = await signin(req, error="google_not_configured")
        assert resp.status_code == 200
        html = resp.body.decode()
        assert "Google sign-in is not configured yet" in html


@pytest.mark.asyncio
async def test_google_start_unconfigured_redirects_to_signin_error(monkeypatch):
    req = _make_request("GET", "/auth/google/start")
    fake_redis = FakeRedis()
    monkeypatch.setattr(app_state, "redis_client", fake_redis)

    with patch(
        "app.auth.oauth_app_credentials.get_oauth_app_credentials",
        side_effect=OAuthAppNotConfigured("google"),
    ):
        resp = await google_start(req)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/signin?error=google_not_configured"


@pytest.mark.asyncio
async def test_google_start_invalid_encryption_token_redirects_to_signin_error(monkeypatch):
    req = _make_request("GET", "/auth/google/start")
    fake_redis = FakeRedis()
    monkeypatch.setattr(app_state, "redis_client", fake_redis)

    with patch(
        "app.auth.oauth_app_credentials.get_oauth_app_credentials",
        side_effect=InvalidToken(),
    ):
        resp = await google_start(req)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/signin?error=google_not_configured"


@pytest.mark.asyncio
async def test_google_start_success_stores_state_and_redirects(monkeypatch):
    req = _make_request("GET", "/auth/google/start?next=/custom/target")
    fake_redis = FakeRedis()
    monkeypatch.setattr(app_state, "redis_client", fake_redis)

    mock_creds = SimpleNamespace(client_id="google-client-123", client_secret="secret")
    with patch(
        "app.auth.oauth_app_credentials.get_oauth_app_credentials",
        new=AsyncMock(return_value=mock_creds),
    ):
        resp = await google_start(req, next="/custom/target")
        assert resp.status_code == 302
        location = resp.headers["location"]
        assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth?")

        parsed = urllib.parse.urlparse(location)
        params = urllib.parse.parse_qs(parsed.query)

        assert params["client_id"][0] == "google-client-123"
        assert params["response_type"][0] == "code"
        assert "openid" in params["scope"][0]
        assert "email" in params["scope"][0]
        assert "profile" in params["scope"][0]
        state = params["state"][0]

        # Verify state in redis
        stored_raw = fake_redis.store[f"signin_state:{state}"]
        stored = json.loads(stored_raw)
        assert stored["next"] == "/custom/target"


@pytest.mark.asyncio
async def test_google_start_sanitizes_open_redirect_next(monkeypatch):
    req = _make_request("GET", "/auth/google/start?next=https://evil.com")
    fake_redis = FakeRedis()
    monkeypatch.setattr(app_state, "redis_client", fake_redis)

    mock_creds = SimpleNamespace(client_id="google-client-123", client_secret="secret")
    with patch(
        "app.auth.oauth_app_credentials.get_oauth_app_credentials",
        new=AsyncMock(return_value=mock_creds),
    ):
        resp = await google_start(req, next="https://evil.com")
        assert resp.status_code == 302
        parsed = urllib.parse.urlparse(resp.headers["location"])
        params = urllib.parse.parse_qs(parsed.query)
        state = params["state"][0]

        stored = json.loads(fake_redis.store[f"signin_state:{state}"])
        assert stored["next"] == "/home"


@pytest.mark.asyncio
async def test_google_callback_with_error_query_param():
    req = _make_request("GET", "/auth/google/signin/callback?error=access_denied")
    resp = await signin_callback(req, error="access_denied")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/?signin_error=access_denied"


@pytest.mark.asyncio
async def test_google_callback_missing_code_or_state():
    req = _make_request("GET", "/auth/google/signin/callback")
    resp = await signin_callback(req, code=None, state=None)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/"


@pytest.mark.asyncio
async def test_google_callback_missing_or_expired_state(monkeypatch):
    req = _make_request("GET", "/auth/google/signin/callback?code=abc&state=expired")
    fake_redis = FakeRedis()
    monkeypatch.setattr(app_state, "redis_client", fake_redis)
    resp = await signin_callback(req, code="abc", state="expired")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/"


@pytest.mark.asyncio
async def test_google_callback_unconfigured_creds(monkeypatch):
    state = "valid-state-123"
    fake_redis = FakeRedis({f"signin_state:{state}": json.dumps({"next": "/home"})})
    monkeypatch.setattr(app_state, "redis_client", fake_redis)
    req = _make_request("GET", f"/auth/google/signin/callback?code=abc&state={state}")

    with patch(
        "app.auth.oauth_app_credentials.get_oauth_app_credentials",
        side_effect=OAuthAppNotConfigured("google"),
    ):
        resp = await signin_callback(req, code="abc", state=state)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/signin?error=google_not_configured"


@pytest.mark.asyncio
async def test_google_callback_failed_token_exchange(monkeypatch):
    state = "valid-state-123"
    fake_redis = FakeRedis({f"signin_state:{state}": json.dumps({"next": "/home"})})
    monkeypatch.setattr(app_state, "redis_client", fake_redis)
    req = _make_request("GET", f"/auth/google/signin/callback?code=abc&state={state}")

    mock_creds = SimpleNamespace(client_id="cid", client_secret="secret")
    mock_post_resp = MagicMock(status_code=400)
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_post_resp
    mock_ctx = MagicMock()
    mock_ctx.__aenter__.return_value = mock_client
    mock_ctx.__aexit__.return_value = False

    with (
        patch(
            "app.auth.oauth_app_credentials.get_oauth_app_credentials", new=AsyncMock(return_value=mock_creds)
        ),
        patch("httpx.AsyncClient", return_value=mock_ctx),
    ):
        resp = await signin_callback(req, code="abc", state=state)
        assert resp.status_code == 400
        assert "Sign-in failed" in resp.body.decode()


@pytest.mark.asyncio
async def test_google_callback_missing_id_token(monkeypatch):
    state = "valid-state-no-token"
    fake_redis = FakeRedis({f"signin_state:{state}": json.dumps({"next": "/home"})})
    monkeypatch.setattr(app_state, "redis_client", fake_redis)
    req = _make_request("GET", f"/auth/google/signin/callback?code=abc&state={state}")

    mock_creds = SimpleNamespace(client_id="cid", client_secret="secret")
    mock_post_resp = MagicMock(status_code=200)
    mock_post_resp.json.return_value = {}  # Missing id_token
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_post_resp
    mock_ctx = MagicMock()
    mock_ctx.__aenter__.return_value = mock_client
    mock_ctx.__aexit__.return_value = False

    with (
        patch(
            "app.auth.oauth_app_credentials.get_oauth_app_credentials", new=AsyncMock(return_value=mock_creds)
        ),
        patch("httpx.AsyncClient", return_value=mock_ctx),
    ):
        resp = await signin_callback(req, code="abc", state=state)
        assert resp.status_code == 400
        assert "No identity token received" in resp.body.decode()


@pytest.mark.asyncio
async def test_google_callback_new_user_gated_redirects_to_signin_gated(monkeypatch):
    state = "valid-state-gated"
    fake_redis = FakeRedis({f"signin_state:{state}": json.dumps({"next": "/home"})})
    monkeypatch.setattr(app_state, "redis_client", fake_redis)
    req = _make_request("GET", f"/auth/google/signin/callback?code=abc&state={state}")

    mock_creds = SimpleNamespace(client_id="cid", client_secret="secret")
    id_token = _make_fake_id_token("gated_newbie@example.com", "Gated User")
    mock_post_resp = MagicMock(status_code=200)
    mock_post_resp.json.return_value = {"id_token": id_token}
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_post_resp
    mock_ctx = MagicMock()
    mock_ctx.__aenter__.return_value = mock_client
    mock_ctx.__aexit__.return_value = False

    with (
        patch(
            "app.auth.oauth_app_credentials.get_oauth_app_credentials", new=AsyncMock(return_value=mock_creds)
        ),
        patch("httpx.AsyncClient", return_value=mock_ctx),
        patch("app.settings_service.get_runtime_setting", new=AsyncMock(return_value=True)),
        patch("app.api.google_oauth_routes._google_signin_enabled", new=AsyncMock(return_value=True)),
    ):
        resp = await signin_callback(req, code="abc", state=state)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/signin?gated=1"


@pytest.mark.asyncio
async def test_google_callback_new_user_success_redirects_to_tutorial(monkeypatch):
    state = "valid-state-new"
    fake_redis = FakeRedis({f"signin_state:{state}": json.dumps({"next": "/home"})})
    monkeypatch.setattr(app_state, "redis_client", fake_redis)
    req = _make_request("GET", f"/auth/google/signin/callback?code=abc&state={state}")

    mock_creds = SimpleNamespace(client_id="cid", client_secret="secret")
    id_token = _make_fake_id_token("new_google_user@example.com", "New Google User")
    mock_post_resp = MagicMock(status_code=200)
    mock_post_resp.json.return_value = {"id_token": id_token}
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_post_resp
    mock_ctx = MagicMock()
    mock_ctx.__aenter__.return_value = mock_client
    mock_ctx.__aexit__.return_value = False

    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None  # user does not exist
    mock_db.execute = AsyncMock(return_value=mock_result)
    mock_db.commit = AsyncMock()
    mock_db.flush = AsyncMock()

    added_users = []

    def fake_add(u):
        u.id = uuid.uuid4()
        added_users.append(u)

    mock_db.add = fake_add

    @asynccontextmanager
    async def _db_factory():
        yield mock_db

    monkeypatch.setattr(app_state, "db_session_factory", _db_factory)

    with (
        patch(
            "app.auth.oauth_app_credentials.get_oauth_app_credentials", new=AsyncMock(return_value=mock_creds)
        ),
        patch("httpx.AsyncClient", return_value=mock_ctx),
        patch("app.settings_service.get_runtime_setting", new=AsyncMock(return_value=False)),
        patch("app.api.google_oauth_routes._google_signin_enabled", new=AsyncMock(return_value=True)),
    ):
        resp = await signin_callback(req, code="abc", state=state)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/tutorial"
        assert "uid=" in resp.headers.get("set-cookie", "")

        # Verify created user
        assert len(added_users) == 1
        new_u = added_users[0]
        assert new_u.email == "new_google_user@example.com"
        assert new_u.email_verified is True
        assert new_u.auth_provider == "google"


@pytest.mark.asyncio
async def test_google_callback_returning_user_incomplete_tutorial(monkeypatch):
    state = "valid-state-existing-incomplete"
    fake_redis = FakeRedis({f"signin_state:{state}": json.dumps({"next": "/home"})})
    monkeypatch.setattr(app_state, "redis_client", fake_redis)
    req = _make_request("GET", f"/auth/google/signin/callback?code=abc&state={state}")

    mock_creds = SimpleNamespace(client_id="cid", client_secret="secret")
    id_token = _make_fake_id_token("existing@example.com", "Existing User")
    mock_post_resp = MagicMock(status_code=200)
    mock_post_resp.json.return_value = {"id_token": id_token}
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_post_resp
    mock_ctx = MagicMock()
    mock_ctx.__aenter__.return_value = mock_client
    mock_ctx.__aexit__.return_value = False

    existing_user = SimpleNamespace(
        id=uuid.uuid4(),
        email="existing@example.com",
        display_name="Existing User",
        auth_provider="google",
        email_verified=True,
        email_verified_at=datetime.utcnow(),
        tutorial_completed_at=None,  # has NOT completed tutorial
    )

    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = existing_user
    mock_db.execute = AsyncMock(return_value=mock_result)
    mock_db.commit = AsyncMock()

    @asynccontextmanager
    async def _db_factory():
        yield mock_db

    monkeypatch.setattr(app_state, "db_session_factory", _db_factory)

    with (
        patch(
            "app.auth.oauth_app_credentials.get_oauth_app_credentials", new=AsyncMock(return_value=mock_creds)
        ),
        patch("httpx.AsyncClient", return_value=mock_ctx),
    ):
        resp = await signin_callback(req, code="abc", state=state)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/tutorial"
        assert "uid=" in resp.headers.get("set-cookie", "")


@pytest.mark.asyncio
async def test_google_callback_returning_user_completed_tutorial_ensures_project(monkeypatch):
    state = "valid-state-existing-done"
    fake_redis = FakeRedis({f"signin_state:{state}": json.dumps({"next": "/home"})})
    monkeypatch.setattr(app_state, "redis_client", fake_redis)
    req = _make_request("GET", f"/auth/google/signin/callback?code=abc&state={state}")

    mock_creds = SimpleNamespace(client_id="cid", client_secret="secret")
    id_token = _make_fake_id_token("completed@example.com", "Completed User")
    mock_post_resp = MagicMock(status_code=200)
    mock_post_resp.json.return_value = {"id_token": id_token}
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_post_resp
    mock_ctx = MagicMock()
    mock_ctx.__aenter__.return_value = mock_client
    mock_ctx.__aexit__.return_value = False

    existing_user = SimpleNamespace(
        id=uuid.uuid4(),
        email="completed@example.com",
        display_name="Completed User",
        auth_provider="google",
        email_verified=True,
        email_verified_at=datetime.utcnow(),
        tutorial_completed_at=datetime.utcnow(),  # completed tutorial
    )

    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = existing_user
    mock_db.execute = AsyncMock(return_value=mock_result)
    mock_db.commit = AsyncMock()

    @asynccontextmanager
    async def _db_factory():
        yield mock_db

    monkeypatch.setattr(app_state, "db_session_factory", _db_factory)

    with (
        patch(
            "app.auth.oauth_app_credentials.get_oauth_app_credentials", new=AsyncMock(return_value=mock_creds)
        ),
        patch("httpx.AsyncClient", return_value=mock_ctx),
        patch("app.api.project_routes.ensure_default_project", new=AsyncMock()) as ensure_proj_mock,
    ):
        resp = await signin_callback(req, code="abc", state=state)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/home"
        assert "uid=" in resp.headers.get("set-cookie", "")
        ensure_proj_mock.assert_awaited_once_with(
            str(existing_user.id), "Completed User", "completed@example.com"
        )


@pytest.mark.asyncio
async def test_google_callback_collision_unverified_email_account_blocked(monkeypatch):
    state = "valid-state-collision"
    fake_redis = FakeRedis({f"signin_state:{state}": json.dumps({"next": "/home"})})
    monkeypatch.setattr(app_state, "redis_client", fake_redis)
    req = _make_request("GET", f"/auth/google/signin/callback?code=abc&state={state}")

    mock_creds = SimpleNamespace(client_id="cid", client_secret="secret")
    id_token = _make_fake_id_token("unverified@example.com", "Victim")
    mock_post_resp = MagicMock(status_code=200)
    mock_post_resp.json.return_value = {"id_token": id_token}
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_post_resp
    mock_ctx = MagicMock()
    mock_ctx.__aenter__.return_value = mock_client
    mock_ctx.__aexit__.return_value = False

    unverified_email_user = SimpleNamespace(
        id=uuid.uuid4(),
        email="unverified@example.com",
        display_name="Victim",
        auth_provider="email",
        email_verified=False,  # Unverified!
        tutorial_completed_at=None,
    )

    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = unverified_email_user
    mock_db.execute = AsyncMock(return_value=mock_result)

    @asynccontextmanager
    async def _db_factory():
        yield mock_db

    monkeypatch.setattr(app_state, "db_session_factory", _db_factory)

    with (
        patch(
            "app.auth.oauth_app_credentials.get_oauth_app_credentials", new=AsyncMock(return_value=mock_creds)
        ),
        patch("httpx.AsyncClient", return_value=mock_ctx),
    ):
        resp = await signin_callback(req, code="abc", state=state)
        assert resp.status_code == 409
        assert "hasn't been verified yet" in resp.body.decode()


@pytest.mark.asyncio
async def test_google_callback_linking_with_verified_email_account(monkeypatch):
    state = "valid-state-linking"
    fake_redis = FakeRedis({f"signin_state:{state}": json.dumps({"next": "/home"})})
    monkeypatch.setattr(app_state, "redis_client", fake_redis)
    req = _make_request("GET", f"/auth/google/signin/callback?code=abc&state={state}")

    mock_creds = SimpleNamespace(client_id="cid", client_secret="secret")
    id_token = _make_fake_id_token("verified@example.com", "Legit User")
    mock_post_resp = MagicMock(status_code=200)
    mock_post_resp.json.return_value = {"id_token": id_token}
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_post_resp
    mock_ctx = MagicMock()
    mock_ctx.__aenter__.return_value = mock_client
    mock_ctx.__aexit__.return_value = False

    verified_user = SimpleNamespace(
        id=uuid.uuid4(),
        email="verified@example.com",
        display_name=None,
        auth_provider="email",
        email_verified=True,
        email_verified_at=datetime.utcnow(),
        tutorial_completed_at=datetime.utcnow(),
    )

    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = verified_user
    mock_db.execute = AsyncMock(return_value=mock_result)
    mock_db.commit = AsyncMock()

    @asynccontextmanager
    async def _db_factory():
        yield mock_db

    monkeypatch.setattr(app_state, "db_session_factory", _db_factory)

    with (
        patch(
            "app.auth.oauth_app_credentials.get_oauth_app_credentials", new=AsyncMock(return_value=mock_creds)
        ),
        patch("httpx.AsyncClient", return_value=mock_ctx),
        patch("app.api.project_routes.ensure_default_project", new=AsyncMock()),
    ):
        resp = await signin_callback(req, code="abc", state=state)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/home"
        assert verified_user.auth_provider == "both"
        assert verified_user.display_name == "Legit User"


@pytest.mark.asyncio
async def test_signout_clears_uid_cookie_and_redirects():
    req = _make_request("GET", "/signout", cookies={"uid": "some-token"})
    resp = await signout(req)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/"
    cookie_header = resp.headers.get("set-cookie", "")
    assert "uid=" in cookie_header
