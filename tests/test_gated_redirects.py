"""Tests for gated page redirects.

Verifies that accessing gated pages (/admin, /admin/oauth-apps, etc.)
redirects unauthenticated users to '/' and non-admin users to '/home',
instead of returning raw JSON errors.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport

from app.auth.csrf import _generate_csrf_token
from app.main import app


@pytest.fixture
async def direct_client():
    csrf = _generate_csrf_token()
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        cookies={"csrf_token": csrf},
        headers={"x-csrf-token": csrf},
        follow_redirects=False,
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_admin_page_unauthenticated_redirects_to_homepage(direct_client):
    with patch("app.api.admin_routes._resolve_user_ctx", new=AsyncMock(return_value=None)):
        resp = await direct_client.get("/admin")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/"


@pytest.mark.asyncio
async def test_admin_page_non_superadmin_redirects_to_homepage(direct_client):
    uid = str(uuid.uuid4())
    fake_user = MagicMock()
    fake_user.id = uuid.UUID(uid)
    fake_user.is_superadmin = False

    mock_db = AsyncMock()
    mock_exec_result = MagicMock()
    mock_exec_result.scalar_one_or_none.return_value = fake_user
    mock_db.execute.return_value = mock_exec_result

    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__aenter__.return_value = mock_db

    ctx = type("C", (), {"user_id": uid, "email": "regular@example.com"})()
    with (
        patch("app.api.admin_routes._resolve_user_ctx", new=AsyncMock(return_value=ctx)),
        patch("app.app_state.db_session_factory", mock_session_factory),
    ):
        resp = await direct_client.get("/admin")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/home"


@pytest.mark.asyncio
async def test_admin_page_superadmin_allowed(direct_client):
    uid = str(uuid.uuid4())
    fake_user = MagicMock()
    fake_user.id = uuid.UUID(uid)
    fake_user.is_superadmin = True

    mock_db = AsyncMock()
    mock_exec_result = MagicMock()
    mock_exec_result.scalar_one_or_none.return_value = fake_user
    mock_db.execute.return_value = mock_exec_result

    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__aenter__.return_value = mock_db

    ctx = type("C", (), {"user_id": uid, "email": "super@example.com"})()
    with (
        patch("app.api.admin_routes._resolve_user_ctx", new=AsyncMock(return_value=ctx)),
        patch("app.app_state.db_session_factory", mock_session_factory),
        patch(
            "app.api.google_oauth_routes._load_user_view",
            new=AsyncMock(return_value={"id": uid, "email": "super@example.com"}),
        ),
        patch("app.settings_service.access_approval_required", new=AsyncMock(return_value=False)),
    ):
        resp = await direct_client.get("/admin")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_admin_api_still_returns_401_for_unauthenticated(direct_client):
    """API endpoints must continue returning 401/403 JSON, not redirects."""
    with patch("app.api.admin_routes._resolve_user_ctx", new=AsyncMock(return_value=None)):
        resp = await direct_client.get("/api/admin/users")
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Not authenticated"}


@pytest.mark.asyncio
async def test_integrations_page_unauthenticated_redirects_to_homepage(direct_client):
    with patch("app.api.integrations_routes._resolve_user", new=AsyncMock(return_value=None)):
        resp = await direct_client.get("/admin/oauth-apps")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/"


@pytest.mark.asyncio
async def test_integrations_page_non_admin_redirects_to_homepage(direct_client):
    fake_user = MagicMock()
    fake_user.id = uuid.uuid4()
    fake_user.is_superadmin = False

    mock_db = AsyncMock()
    mock_exec_result = MagicMock()
    mock_exec_result.scalar_one_or_none.return_value = None  # No superadmin, no admin membership
    mock_db.execute.return_value = mock_exec_result

    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__aenter__.return_value = mock_db

    with (
        patch("app.api.integrations_routes._resolve_user", new=AsyncMock(return_value=fake_user)),
        patch("app.app_state.db_session_factory", mock_session_factory),
    ):
        resp = await direct_client.get("/admin/oauth-apps")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/home"


@pytest.mark.asyncio
async def test_project_settings_non_member_redirects_to_homepage(direct_client):
    uid = str(uuid.uuid4())
    fake_user = {"user_id": uid, "email": "someone@example.com"}
    fake_proj = MagicMock()
    fake_proj.id = uuid.uuid4()

    with (
        patch("app.api.project_routes._resolve_user", new=AsyncMock(return_value=fake_user)),
        patch("app.api.project_routes._get_project_by_slug", new=AsyncMock(return_value=fake_proj)),
        patch("app.api.project_routes._get_membership", new=AsyncMock(return_value=None)),
    ):
        resp = await direct_client.get("/project/some-slug/settings")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/home"
