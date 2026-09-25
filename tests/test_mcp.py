"""
MCP Infrastructure Tests

Covers:
  1. Tool registration — all tools present, docstrings, signatures, routing tables
  2. Dispatcher error handling — unknown actions return structured errors
  3. Auth & tokens — PKCE, OAuth metadata, session helpers, scope tiers, Fernet
  4. Full OAuth 2.1 flow — register → authorize → callback → token → refresh → revoke
"""

import base64
import hashlib
import inspect
import json
import secrets

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _pkce_pair():
    """Generate a PKCE code_verifier + S256 code_challenge."""
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


# ═══════════════════════════════════════════════════════════════════════════
# 1. MCP Tools
# ═══════════════════════════════════════════════════════════════════════════

EXPECTED_TOOLS = {
    # Domain read/write pairs
    "analytics_read",
    "analytics_write",
    "audience_read",
    "audience_write",
    "tagmanager_read",
    "tagmanager_write",
    "marketing_read",
    "marketing_write",
    "warehouse_read",
    "warehouse_query",
    "seo_read",
    "seo_write",
    # Knowledge + composite
    "get_knowledge",
    "get_session_context",
    "run_audit",
    "run_analysis",
    "run_script",
    # Project management
    "set_active_project",
    "list_my_projects",
    # Escape hatches
    "generic_tool_read",
    "generic_tool_write",
    # Tag testing & rulebook tools
    "live_tag_test",
    "save_audit_result",
    "tag_rulebook",
}

DISPATCHERS = {
    "analytics_read",
    "analytics_write",
    "audience_read",
    "audience_write",
    "tagmanager_read",
    "tagmanager_write",
    "marketing_read",
    "marketing_write",
    "warehouse_read",
    "seo_read",
    "seo_write",
    "get_knowledge",
    "run_audit",
    "run_analysis",
}


@pytest.fixture(scope="module")
def mcp_server():
    from mcp.server.fastmcp import FastMCP

    from app.tools.registry import register_all_tools

    mcp = FastMCP(name="test-fluxito")
    register_all_tools(mcp)
    return mcp


@pytest.fixture(scope="module")
def tool_manager(mcp_server):
    return mcp_server._tool_manager


@pytest.fixture(scope="module")
def registered_names(tool_manager):
    return set(tool_manager._tools.keys())


def test_expected_tools_present(registered_names):
    missing = EXPECTED_TOOLS - registered_names
    assert not missing, f"Missing tools: {sorted(missing)}"


def test_no_unexpected_tools(registered_names):
    extra = registered_names - EXPECTED_TOOLS
    assert not extra, f"Unexpected tools (update EXPECTED_TOOLS if intentional): {sorted(extra)}"


def test_instrumentation_hook_is_installed(tool_manager):
    """register_all_tools must actually swap in the instrumented call_tool.

    Regression guard: _install_tool_hook used to build the wrapper but never
    assign it, so the audit trail (and the entire Activity Log) silently got
    no data even though every tool worked. This asserts the real registration
    path installs the hook end-to-end.
    """
    assert tool_manager.call_tool.__name__ == "_instrumented_call", (
        "tool_manager.call_tool is not the instrumented wrapper — "
        "the audit/instrumentation hook is not installed"
    )
    assert "_install_tool_hook" in tool_manager.call_tool.__qualname__


def test_all_tools_have_docstrings(tool_manager):
    missing = [
        name
        for name, tool in tool_manager._tools.items()
        if not (getattr(tool.fn, "__doc__", None) or "").strip()
    ]
    assert not missing, f"Tools missing docstrings: {sorted(missing)}"


def test_docstrings_at_least_20_chars(tool_manager):
    short = [
        name
        for name, tool in tool_manager._tools.items()
        if len((getattr(tool.fn, "__doc__", None) or "").strip()) < 20
    ]
    assert not short, f"Too-short docstrings: {sorted(short)}"


def test_dispatchers_accept_action_and_params(tool_manager):
    issues = []
    for name in DISPATCHERS:
        tool = tool_manager._tools.get(name)
        if tool is None:
            issues.append(f"{name}: not registered")
            continue
        params = set(inspect.signature(tool.fn).parameters.keys())
        if "action" not in params:
            issues.append(f"{name}: missing 'action'")
        if "params" not in params:
            issues.append(f"{name}: missing 'params'")
    assert not issues, "Signature issues:\n" + "\n".join(issues)


def test_route_tuples_well_formed():
    from app.tools.unified import (
        ANALYSIS_ROUTES,
        ANALYTICS_READ_ROUTES,
        ANALYTICS_WRITE_ROUTES,
        AUDIENCE_READ_ROUTES,
        AUDIENCE_WRITE_ROUTES,
        AUDIT_ROUTES,
        KNOWLEDGE_ROUTES,
        MARKETING_READ_ROUTES,
        MARKETING_WRITE_ROUTES,
        SEO_READ_ROUTES,
        SEO_WRITE_ROUTES,
        TAGMANAGER_READ_ROUTES,
        TAGMANAGER_WRITE_ROUTES,
        WAREHOUSE_READ_ROUTES,
    )

    all_routes = {
        "analytics_read": ANALYTICS_READ_ROUTES,
        "analytics_write": ANALYTICS_WRITE_ROUTES,
        "audience_read": AUDIENCE_READ_ROUTES,
        "audience_write": AUDIENCE_WRITE_ROUTES,
        "tagmanager_read": TAGMANAGER_READ_ROUTES,
        "tagmanager_write": TAGMANAGER_WRITE_ROUTES,
        "marketing_read": MARKETING_READ_ROUTES,
        "marketing_write": MARKETING_WRITE_ROUTES,
        "warehouse_read": WAREHOUSE_READ_ROUTES,
        "seo_read": SEO_READ_ROUTES,
        "seo_write": SEO_WRITE_ROUTES,
        "get_knowledge": KNOWLEDGE_ROUTES,
        "run_audit": AUDIT_ROUTES,
        "run_analysis": ANALYSIS_ROUTES,
    }

    bad = []
    for surface, routes in all_routes.items():
        assert routes, f"{surface} routing table is empty"
        for action, route in routes.items():
            if not isinstance(route, tuple) or len(route) < 2 or len(route) > 3:
                bad.append(f"{surface}.{action}: bad shape {route!r}")
            elif not isinstance(route[0], str):
                bad.append(f"{surface}.{action}: tool name not str")
            elif len(route) == 3 and not isinstance(route[2], dict):
                bad.append(f"{surface}.{action}: extra_kwargs not dict")
    assert not bad, "Malformed routes:\n" + "\n".join(bad)


@pytest.mark.asyncio
async def test_unknown_action_returns_structured_error(tool_manager):
    """Dispatchers must return error_type='unknown_action' for bad actions."""
    get_knowledge = tool_manager._tools["get_knowledge"].fn
    result = await get_knowledge(action="bogus", params={})
    assert result.get("error") is True
    assert result.get("error_type") == "unknown_action"
    available = set(result.get("available_actions", []))

    from app.tools.unified import KNOWLEDGE_ROUTES

    assert set(KNOWLEDGE_ROUTES) <= available


# ═══════════════════════════════════════════════════════════════════════════
# 2. Auth & Tokens
# ═══════════════════════════════════════════════════════════════════════════


def test_pkce_valid_verifier():
    from app.auth.mcp_oauth_server import _pkce_verify

    verifier, challenge = _pkce_pair()
    assert _pkce_verify(verifier, challenge) is True


def test_pkce_wrong_verifier():
    from app.auth.mcp_oauth_server import _pkce_verify

    _, challenge = _pkce_pair()
    assert _pkce_verify("wrong-verifier", challenge) is False


@pytest.mark.asyncio
async def test_oauth_metadata_shape():
    from app.auth.mcp_oauth_server import oauth_metadata

    meta = await oauth_metadata()
    assert meta["grant_types_supported"] == ["authorization_code", "refresh_token"]
    assert "S256" in meta["code_challenge_methods_supported"]
    assert meta["token_endpoint_auth_methods_supported"] == ["none"]
    assert "/oauth/authorize" in meta["authorization_endpoint"]
    assert "/oauth/token" in meta["token_endpoint"]
    assert "/oauth/register" in meta["registration_endpoint"]


def test_sha256_deterministic():
    from app.auth.mcp_session_manager import sha256

    token = "test-access-token-abc123"
    assert sha256(token) == sha256(token)
    assert sha256(token) != sha256(token + "x")


def test_user_context_cache_roundtrip():
    from app.auth.mcp_session_manager import ConnectionInfo, ProjectMembership, UserContext

    ctx = UserContext(
        user_id="11111111-1111-1111-1111-111111111111",
        email="test@example.com",
        display_name="Test User",
        projects=[
            ProjectMembership(
                project_id="22222222-2222-2222-2222-222222222222",
                project_name="My Project",
                project_slug="my-project",
                role="owner",
            )
        ],
        has_ga4=True,
        has_gtm=False,
        connections=[
            ConnectionInfo(
                id="33333333-3333-3333-3333-333333333333",
                provider="google",
                google_email="user@gmail.com",
                scopes=["https://www.googleapis.com/auth/analytics.readonly"],
                connection_status="active",
            )
        ],
    )
    restored = UserContext.from_cache_dict(ctx.to_cache_dict())
    assert restored.user_id == ctx.user_id
    assert restored.email == ctx.email
    assert restored.has_ga4 is True
    assert restored.has_gtm is False
    assert len(restored.projects) == 1
    assert restored.projects[0].project_id == "22222222-2222-2222-2222-222222222222"
    assert len(restored.connections) == 1
    assert restored.connections[0].provider == "google"


def test_project_context_cache_roundtrip():
    from app.auth.mcp_session_manager import ConnectionInfo, ProjectContext

    ctx = ProjectContext(
        project_id="22222222-2222-2222-2222-222222222222",
        project_name="Test Project",
        project_slug="test-project",
        role="admin",
        owner_id="11111111-1111-1111-1111-111111111111",
        has_ga4=True,
        has_bq=True,
        connections=[
            ConnectionInfo(
                id="44444444-4444-4444-4444-444444444444",
                provider="google",
                google_email="admin@example.com",
                scopes=["https://www.googleapis.com/auth/analytics"],
                connection_status="active",
            )
        ],
    )
    restored = ProjectContext.from_cache_dict(ctx.to_cache_dict())
    assert restored.project_id == ctx.project_id
    assert restored.has_ga4 is True
    assert restored.has_bq is True


# ═══════════════════════════════════════════════════════════════════════════
# Per-call active-project resolution (ensure_call_project_ctx)
#
# Regression coverage for the stateless-session bug: set_active_project's
# selection must reach sibling tool calls in the same request/batch (and across
# turns) even though each tool runs in its own asyncio.wait_for task. Tools that
# read current_project_ctx (tracking_plan/SDR, get_session_context) used to see
# no_active_project while connection-fallback tools (analytics_read) worked,
# because the ContextVar set by set_active_project never crossed the task
# boundary. The fix re-resolves per call from Redis + explicit project_id.
# ═══════════════════════════════════════════════════════════════════════════

_PID = "22222222-2222-2222-2222-222222222222"
_UID = "11111111-1111-1111-1111-111111111111"


def _user_ctx_with_project():
    from app.auth.mcp_session_manager import ProjectMembership, UserContext

    return UserContext(
        user_id=_UID,
        email="owner@example.com",
        display_name="Owner",
        projects=[
            ProjectMembership(
                project_id=_PID,
                project_name="VAST Data",
                project_slug="vast-data",
                role="owner",
            )
        ],
    )


def _fake_project_ctx():
    from app.auth.mcp_session_manager import ProjectContext

    return ProjectContext(
        project_id=_PID,
        project_name="VAST Data",
        project_slug="vast-data",
        role="owner",
        owner_id=_UID,
        has_ga4=True,
    )


@pytest.mark.asyncio
async def test_ensure_call_project_ctx_restores_from_redis(fake_redis, monkeypatch):
    """A tool that reads current_project_ctx resolves the session's active
    project from Redis even when the ContextVar is unset (the sibling-call /
    cross-turn case that broke tracking_plan)."""
    import app.app_state as state
    from app.auth import mcp_session_manager as mgr

    saved = {"user": state.current_user_ctx.get(), "redis": state.redis_client}
    monkeypatch.setattr(mgr, "build_project_context", lambda pid, uid: _async_return(_fake_project_ctx()))
    state.redis_client = fake_redis
    state.current_user_ctx.set(_user_ctx_with_project())
    state.current_project_ctx.set(None)
    await fake_redis.set(f"mcp:active_project:{_UID}", _PID)

    try:
        token = await mgr.ensure_call_project_ctx("tracking_plan", {"action": "generate", "params": {}})
        assert token is not None
        resolved = state.current_project_ctx.get()
        assert resolved is not None and resolved.project_id == _PID
        # Legacy fallback flags mirrored onto the user context too.
        assert state.current_user_ctx.get().has_ga4 is True
    finally:
        state.current_project_ctx.set(None)
        state.current_user_ctx.set(saved["user"])
        state.redis_client = saved["redis"]


@pytest.mark.asyncio
async def test_ensure_call_project_ctx_honors_explicit_project_id(monkeypatch):
    """An explicit project_id in params resolves with NO Redis state — the
    stateless per-call override that previously couldn't bypass the SDR guard."""
    import app.app_state as state
    from app.auth import mcp_session_manager as mgr

    saved = {"user": state.current_user_ctx.get(), "redis": state.redis_client}
    monkeypatch.setattr(mgr, "build_project_context", lambda pid, uid: _async_return(_fake_project_ctx()))
    state.redis_client = None  # prove it does not depend on session state
    state.current_user_ctx.set(_user_ctx_with_project())
    state.current_project_ctx.set(None)

    try:
        token = await mgr.ensure_call_project_ctx(
            "tracking_plan", {"action": "generate", "params": {"project_id": _PID}}
        )
        assert token is not None
        assert state.current_project_ctx.get().project_id == _PID
    finally:
        state.current_project_ctx.set(None)
        state.current_user_ctx.set(saved["user"])
        state.redis_client = saved["redis"]


@pytest.mark.asyncio
async def test_ensure_call_project_ctx_ignores_foreign_project_id(fake_redis, monkeypatch):
    """A non-membership project_id (e.g. an Amplitude project id) must NOT
    override scope, and must fall through to Redis session restore."""
    import app.app_state as state
    from app.auth import mcp_session_manager as mgr

    saved = {"user": state.current_user_ctx.get(), "redis": state.redis_client}
    monkeypatch.setattr(mgr, "build_project_context", lambda pid, uid: _async_return(_fake_project_ctx()))
    state.redis_client = fake_redis
    state.current_user_ctx.set(_user_ctx_with_project())
    state.current_project_ctx.set(None)
    await fake_redis.set(f"mcp:active_project:{_UID}", _PID)

    try:
        # Amplitude analytics_read passes its own numeric project_id in params.
        token = await mgr.ensure_call_project_ctx(
            "analytics_read", {"action": "run_report", "params": {"project_id": "987654"}}
        )
        assert token is not None
        # Falls through to the session's real project, not the foreign id.
        assert state.current_project_ctx.get().project_id == _PID
    finally:
        state.current_project_ctx.set(None)
        state.current_user_ctx.set(saved["user"])
        state.redis_client = saved["redis"]


@pytest.mark.asyncio
async def test_ensure_call_project_ctx_skips_set_active_project():
    """set_active_project manages its own selection — the resolver must not
    pre-empt it with the stale Redis value."""
    import app.app_state as state
    from app.auth import mcp_session_manager as mgr

    saved = state.current_user_ctx.get()
    state.current_user_ctx.set(_user_ctx_with_project())
    try:
        token = await mgr.ensure_call_project_ctx("set_active_project", {"project": "vast-data"})
        assert token is None
    finally:
        state.current_user_ctx.set(saved)


def _async_return(value):
    async def _coro():
        return value

    return _coro()


def test_google_platform_flags_readonly():
    from app.auth.mcp_session_manager import derive_google_platform_flags

    class FakeConn:
        def __init__(self, scopes):
            self.scopes = scopes

    ga4, gtm, ads, gsc = derive_google_platform_flags(
        [FakeConn(["https://www.googleapis.com/auth/analytics.readonly"])]
    )
    assert ga4 is True
    assert gtm is False
    assert ads is False
    assert gsc is False


def test_google_platform_flags_full():
    from app.auth.mcp_session_manager import derive_google_platform_flags

    class FakeConn:
        def __init__(self, scopes):
            self.scopes = scopes

    ga4, gtm, ads, gsc = derive_google_platform_flags(
        [
            FakeConn(
                [
                    "https://www.googleapis.com/auth/analytics",
                    "https://www.googleapis.com/auth/tagmanager.edit.containers",
                    "https://www.googleapis.com/auth/adwords",
                    "https://www.googleapis.com/auth/webmasters",
                ]
            )
        ]
    )
    assert all([ga4, gtm, ads, gsc])


def test_google_platform_flags_empty():
    from app.auth.mcp_session_manager import derive_google_platform_flags

    ga4, gtm, ads, gsc = derive_google_platform_flags([])
    assert not any([ga4, gtm, ads, gsc])


def test_scope_tiers_defined():
    from app.auth.scopes import GOOGLE_DATA_SCOPES_BY_TIER, TIER_FULL, TIER_GTM_WRITE, TIER_READONLY

    assert TIER_READONLY in GOOGLE_DATA_SCOPES_BY_TIER
    assert TIER_GTM_WRITE in GOOGLE_DATA_SCOPES_BY_TIER
    assert TIER_FULL in GOOGLE_DATA_SCOPES_BY_TIER


def test_readonly_has_analytics_readonly():
    from app.auth.scopes import GOOGLE_DATA_SCOPES_BY_TIER, TIER_READONLY

    assert "https://www.googleapis.com/auth/analytics.readonly" in GOOGLE_DATA_SCOPES_BY_TIER[TIER_READONLY]


def test_full_has_analytics_write():
    from app.auth.scopes import GOOGLE_DATA_SCOPES_BY_TIER, TIER_FULL

    scopes = GOOGLE_DATA_SCOPES_BY_TIER[TIER_FULL]
    assert "https://www.googleapis.com/auth/analytics" in scopes
    assert "https://www.googleapis.com/auth/analytics.readonly" not in scopes


def test_fernet_encrypt_decrypt_roundtrip():
    from cryptography.fernet import Fernet

    from app.auth.google_token_manager import GoogleTokenManager

    key = Fernet.generate_key().decode()
    mgr = GoogleTokenManager.__new__(GoogleTokenManager)
    mgr.fernet = Fernet(key.encode())

    original = "ya29.a0AfH6SMBx_test_access_token_1234567890"
    encrypted = mgr.encrypt(original)
    assert encrypted != original
    assert mgr.decrypt(encrypted) == original


def test_settings_loads():
    from app.config import settings

    assert settings.APP_ENV == "test"
    assert len(settings.APP_SECRET_KEY) >= 32


def test_pydantic_v2_config():
    from app.config import Settings

    assert hasattr(Settings, "model_config")


# ═══════════════════════════════════════════════════════════════════════════
# 3. Full OAuth 2.1 Flow (needs Postgres + Redis)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dynamic_client_registration(db_engine, fake_redis, db_session_factory):
    """POST /oauth/register creates a client and returns client_id."""
    from unittest.mock import AsyncMock

    import app.app_state as state
    from app.auth.mcp_oauth_server import register_client

    saved = {"redis_client": state.redis_client, "db_session_factory": state.db_session_factory}
    state.redis_client = fake_redis
    state.db_session_factory = db_session_factory

    try:
        request = AsyncMock()
        request.json = AsyncMock(
            return_value={
                "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
                "client_name": "Test Claude Client",
            }
        )
        result = await register_client(request)
        assert result["client_id"].startswith("mcp_client_")
        assert result["client_name"] == "Test Claude Client"
        assert "authorization_code" in result["grant_types"]
    finally:
        for k, v in saved.items():
            setattr(state, k, v)


@pytest.mark.asyncio
async def test_full_auth_code_flow(db_engine, fake_redis, db_session_factory):
    """End-to-end: register → authorize → Google callback → token → refresh → revoke."""
    import urllib.parse
    from unittest.mock import AsyncMock, patch

    from sqlalchemy import select

    import app.app_state as state
    from app.auth.mcp_oauth_server import (
        authorize,
        google_identity_callback,
        register_client,
        revoke,
        token,
    )
    from app.auth.oauth_app_credentials import upsert_oauth_app_credentials
    from app.config import settings
    from app.models.mcp_session import MCPSession

    saved = {
        "redis_client": state.redis_client,
        "db_session_factory": state.db_session_factory,
        "mcp_oauth_allow_browser_session": settings.MCP_OAUTH_ALLOW_BROWSER_SESSION,
    }
    state.redis_client = fake_redis
    state.db_session_factory = db_session_factory
    settings.MCP_OAUTH_ALLOW_BROWSER_SESSION = False

    # Seed Google OAuth app credentials — DB-only path; the authorize/callback
    # routes look these up via app.auth.oauth_app_credentials.
    async with db_session_factory() as _seed_db:
        await upsert_oauth_app_credentials(
            _seed_db,
            platform="google",
            client_id="test-google-client-id",
            client_secret="test-google-client-secret",
            extra=None,
            configured_by_user_id=None,
        )
        await _seed_db.commit()

    try:
        # 1. Register client
        reg_request = AsyncMock()
        reg_request.json = AsyncMock(
            return_value={
                "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
            }
        )
        reg_result = await register_client(reg_request)
        client_id = reg_result["client_id"]

        # 2. Authorize → redirects to Google
        verifier, challenge = _pkce_pair()
        auth_state = secrets.token_urlsafe(16)

        auth_request = AsyncMock()
        auth_request.headers = {"host": "testserver", "x-forwarded-proto": "http"}

        response = await authorize(
            request=auth_request,
            response_type="code",
            client_id=client_id,
            redirect_uri="https://claude.ai/api/mcp/auth_callback",
            scope="read",
            state=auth_state,
            code_challenge=challenge,
            code_challenge_method="S256",
            resource=None,
        )
        assert "accounts.google.com" in str(
            response.headers.get("location", response.body if hasattr(response, "body") else "")
        )

        # 3. Simulate Google identity callback
        google_payload = {"email": "testuser@gmail.com", "email_verified": True, "name": "Test User"}
        payload_b64 = base64.urlsafe_b64encode(json.dumps(google_payload).encode()).rstrip(b"=").decode()
        fake_id_token = f"header.{payload_b64}.signature"

        from unittest.mock import MagicMock

        mock_google_response = MagicMock()
        mock_google_response.status_code = 200
        mock_google_response.json.return_value = {
            "access_token": "google-access-token",
            "id_token": fake_id_token,
        }

        with patch("httpx.AsyncClient") as MockClient:
            mock_client_instance = AsyncMock()
            mock_client_instance.post.return_value = mock_google_response
            mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client_instance

            callback_request = AsyncMock()
            callback_request.headers = {"host": "testserver", "x-forwarded-proto": "http"}

            callback_response = await google_identity_callback(
                request=callback_request,
                code="google-auth-code",
                state=auth_state,
                error=None,
            )

        redirect_url = str(callback_response.headers.get("location", ""))
        assert "claude.ai" in redirect_url
        assert "code=" in redirect_url

        parsed = urllib.parse.urlparse(redirect_url)
        qs = urllib.parse.parse_qs(parsed.query)
        auth_code = qs["code"][0]

        # 4. Token exchange
        token_request = AsyncMock()
        token_result = await token(
            request=token_request,
            grant_type="authorization_code",
            code=auth_code,
            redirect_uri="https://claude.ai/api/mcp/auth_callback",
            client_id=client_id,
            code_verifier=verifier,
        )

        token_body = json.loads(token_result.body)
        assert "access_token" in token_body
        assert "refresh_token" in token_body
        assert token_body["token_type"] == "Bearer"
        assert token_body["expires_in"] == 28800

        # Verify session in DB
        access_hash = _sha256(token_body["access_token"])
        async with state.db_session_factory() as db:
            result = await db.execute(select(MCPSession).where(MCPSession.access_token_hash == access_hash))
            session = result.scalar_one_or_none()
            assert session is not None
            assert not session.is_revoked

        # 5. Refresh token
        refresh_result = await token(
            request=token_request,
            grant_type="refresh_token",
            client_id=client_id,
            refresh_token=token_body["refresh_token"],
        )
        refresh_body = json.loads(refresh_result.body)
        assert refresh_body["access_token"] != token_body["access_token"]
        assert refresh_body["refresh_token"] != token_body["refresh_token"]

        # 6. Revoke
        revoke_request = AsyncMock()
        revoke_result = await revoke(
            request=revoke_request,
            token=refresh_body["access_token"],
        )
        revoke_body = json.loads(revoke_result.body)
        assert revoke_body["status"] == "ok"

        # Verify session marked revoked
        new_access_hash = _sha256(refresh_body["access_token"])
        async with state.db_session_factory() as db:
            result = await db.execute(
                select(MCPSession).where(MCPSession.access_token_hash == new_access_hash)
            )
            session = result.scalar_one_or_none()
            assert session is not None
            assert session.is_revoked is True
    finally:
        for k, v in saved.items():
            if k == "mcp_oauth_allow_browser_session":
                settings.MCP_OAUTH_ALLOW_BROWSER_SESSION = v
            else:
                setattr(state, k, v)
