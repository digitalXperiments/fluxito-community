"""Tests for app.auth.oauth_app_credentials.

DB session fixture decision
---------------------------
conftest.py provides `db_session_factory` (an async_sessionmaker bound to a
fresh test schema that is created at setup and dropped at teardown).  Each
test opens its own `async with db_session_factory() as db:` context, calls
`await db.commit()` after writes, and cleans up via a try/finally that
deletes any rows it inserted.  This matches the pattern used in test_mcp.py
and avoids the risk of cross-test pollution from a shared session.

The helper's in-memory TTL cache is cleared before and after every test by
the `_clear_helper_cache` autouse fixture so cache state never bleeds between
test cases.

TestIntegrationsRoutes strategy
--------------------------------
Route tests use httpx.AsyncClient with ASGITransport so the full FastAPI
request/response cycle runs.  ``app_state.db_session_factory`` is patched to
the test session factory so routes open sessions against the test schema.
``app.api.integrations_routes._resolve_user`` is patched per-test to return
either ``None`` (unauthenticated), a plain-member User, or an admin User —
this avoids the Redis/cookie stack while exercising all route and DB logic.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.auth.oauth_app_credentials import (
    OAuthAppNotConfigured,
    _cache_clear_all,
    delete_oauth_app_credentials,
    get_oauth_app_credentials,
    get_oauth_app_credentials_cached,
    list_oauth_app_status,
    upsert_oauth_app_credentials,
)
from app.models.oauth_app_credential import SUPPORTED_PLATFORMS


@pytest.fixture(autouse=True)
def _clear_helper_cache():
    """Reset the helper's in-memory TTL cache before and after every test."""
    _cache_clear_all()
    yield
    _cache_clear_all()


# ---------------------------------------------------------------------------
# 1. DB row resolves correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_db_row_resolves(db_session_factory):
    """A DB row resolves with source='db'."""
    async with db_session_factory() as db:
        await upsert_oauth_app_credentials(
            db,
            platform="snap",
            client_id="db-snap-id",
            client_secret="db-snap-secret",
            extra=None,
            configured_by_user_id=None,
        )
        await db.commit()

    try:
        async with db_session_factory() as db:
            creds = await get_oauth_app_credentials(db, "snap")

        assert creds.source == "db"
        assert creds.client_id == "db-snap-id"
        assert creds.client_secret == "db-snap-secret"
    finally:
        async with db_session_factory() as db:
            await delete_oauth_app_credentials(db, platform="snap")
            await db.commit()


# ---------------------------------------------------------------------------
# 2. No DB row → OAuthAppNotConfigured
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unconfigured_platform_raises(db_session_factory):
    """No DB row → OAuthAppNotConfigured is raised. (DB-only — no env fallback.)"""
    async with db_session_factory() as db:
        with pytest.raises(OAuthAppNotConfigured, match="linkedin"):
            await get_oauth_app_credentials(db, "linkedin")


# ---------------------------------------------------------------------------
# 4. Unsupported platform → ValueError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unsupported_platform_raises(db_session_factory):
    """Passing an unsupported platform name raises ValueError."""
    async with db_session_factory() as db:
        with pytest.raises(ValueError, match="Unsupported platform"):
            await get_oauth_app_credentials(db, "twitter")


@pytest.mark.asyncio
async def test_unsupported_platform_upsert_raises(db_session_factory):
    """upsert with an unsupported platform raises ValueError before touching DB."""
    async with db_session_factory() as db:
        with pytest.raises(ValueError, match="Unsupported platform"):
            await upsert_oauth_app_credentials(
                db,
                platform="twitter",
                client_id="x",
                client_secret="y",
                extra=None,
                configured_by_user_id=None,
            )


# ---------------------------------------------------------------------------
# 5. Upsert → get → delete → get raises
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_then_delete_round_trip(db_session_factory):
    """Full round-trip: insert, read, delete, then read raises OAuthAppNotConfigured."""
    async with db_session_factory() as db:
        await upsert_oauth_app_credentials(
            db,
            platform="pinterest",
            client_id="pin-id-1",
            client_secret="pin-secret-1",
            extra={"scope": "read_boards"},
            configured_by_user_id=None,
        )
        await db.commit()

    try:
        # Read back
        async with db_session_factory() as db:
            creds = await get_oauth_app_credentials(db, "pinterest")
        assert creds.source == "db"
        assert creds.client_id == "pin-id-1"
        assert creds.extra == {"scope": "read_boards"}

        # Delete
        async with db_session_factory() as db:
            deleted = await delete_oauth_app_credentials(db, platform="pinterest")
            await db.commit()
        assert deleted is True

        # Now should raise — DB-only, no fallback
        async with db_session_factory() as db:
            with pytest.raises(OAuthAppNotConfigured):
                await get_oauth_app_credentials(db, "pinterest")
    except Exception:
        # Cleanup guard in case the delete step itself failed
        try:
            async with db_session_factory() as db:
                await delete_oauth_app_credentials(db, platform="pinterest")
                await db.commit()
        except Exception:
            pass
        raise


# ---------------------------------------------------------------------------
# 6. list_oauth_app_status returns all supported platforms with correct source
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_status_returns_all_platforms(db_session_factory):
    """list_oauth_app_status always returns one entry per supported platform."""
    async with db_session_factory() as db:
        result = await list_oauth_app_status(db)

    platforms_returned = {r["platform"] for r in result}
    assert platforms_returned == set(SUPPORTED_PLATFORMS)
    assert len(result) == len(SUPPORTED_PLATFORMS)

    # All platforms should be 'unconfigured' in a fresh test DB — DB-only,
    # no env fallback. Source is either 'db' (when a row exists) or 'unconfigured'.
    for entry in result:
        assert entry["source"] in ("db", "unconfigured")
        if entry["source"] == "unconfigured":
            assert entry["client_id_masked"] is None
        else:
            assert entry["client_id_masked"] is not None


def test_x_ads_is_supported_oauth_app_platform():
    """X Ads can be configured as an install-wide OAuth app."""
    assert "x" in SUPPORTED_PLATFORMS


def test_reddit_ads_is_supported_oauth_app_platform():
    """Reddit Ads can be configured as an install-wide OAuth app."""
    assert "reddit" in SUPPORTED_PLATFORMS


def test_apple_ads_is_supported_oauth_app_platform():
    """Apple Ads can be configured as an install-wide OAuth app."""
    assert "apple" in SUPPORTED_PLATFORMS


# ---------------------------------------------------------------------------
# 7. Encrypt/decrypt round-trip — secret survives the DB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_encrypt_decrypt_round_trip(db_session_factory):
    """client_secret is encrypted in the DB and decrypted transparently on read."""
    plaintext_secret = "super-secret-tiktok-key-12345"

    async with db_session_factory() as db:
        await upsert_oauth_app_credentials(
            db,
            platform="tiktok",
            client_id="tiktok-app-id",
            client_secret=plaintext_secret,
            extra=None,
            configured_by_user_id=None,
        )
        await db.commit()

    try:
        async with db_session_factory() as db:
            creds = await get_oauth_app_credentials(db, "tiktok")

        assert creds.client_secret == plaintext_secret
        assert creds.source == "db"
    finally:
        async with db_session_factory() as db:
            await delete_oauth_app_credentials(db, platform="tiktok")
            await db.commit()


# ---------------------------------------------------------------------------
# 8. Cached variant returns same result and avoids second DB hit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cached_variant_returns_same_result(db_session_factory):
    """get_oauth_app_credentials_cached returns identical result to uncached call."""
    async with db_session_factory() as db:
        await upsert_oauth_app_credentials(
            db,
            platform="meta",
            client_id="cached-meta-id",
            client_secret="cached-meta-secret",
            extra=None,
            configured_by_user_id=None,
        )
        await db.commit()

    try:
        async with db_session_factory() as db:
            creds_uncached = await get_oauth_app_credentials(db, "meta")

        _cache_clear_all()  # ensure cold cache for cached call

        async with db_session_factory() as db:
            creds_cached = await get_oauth_app_credentials_cached(db, "meta")

        assert creds_cached.client_id == creds_uncached.client_id
        assert creds_cached.client_secret == creds_uncached.client_secret
        assert creds_cached.source == creds_uncached.source

        # Second call hits cache (same session, no DB round-trip)
        async with db_session_factory() as db:
            creds_cached_again = await get_oauth_app_credentials_cached(db, "meta")
        assert creds_cached_again.client_id == creds_cached.client_id
    finally:
        async with db_session_factory() as db:
            await delete_oauth_app_credentials(db, platform="meta")
            await db.commit()


# ---------------------------------------------------------------------------
# 9. Upsert updates an existing row (idempotent overwrite)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_overwrites_existing_row(db_session_factory):
    """Calling upsert twice updates the row rather than creating a duplicate."""
    async with db_session_factory() as db:
        await upsert_oauth_app_credentials(
            db,
            platform="linkedin",
            client_id="li-id-v1",
            client_secret="li-secret-v1",
            extra=None,
            configured_by_user_id=None,
        )
        await db.commit()

    try:
        async with db_session_factory() as db:
            await upsert_oauth_app_credentials(
                db,
                platform="linkedin",
                client_id="li-id-v2",
                client_secret="li-secret-v2",
                extra={"extra_field": "val"},
                configured_by_user_id=None,
            )
            await db.commit()

        async with db_session_factory() as db:
            creds = await get_oauth_app_credentials(db, "linkedin")

        assert creds.client_id == "li-id-v2"
        assert creds.client_secret == "li-secret-v2"
        assert creds.extra == {"extra_field": "val"}
    finally:
        async with db_session_factory() as db:
            await delete_oauth_app_credentials(db, platform="linkedin")
            await db.commit()


# ---------------------------------------------------------------------------
# 10. delete_oauth_app_credentials returns False when no row exists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_nonexistent_row_returns_false(db_session_factory):
    """Deleting a platform that has no row returns False without error."""
    async with db_session_factory() as db:
        result = await delete_oauth_app_credentials(db, platform="google")
        # No commit needed — nothing changed
    assert result is False


# ---------------------------------------------------------------------------
# TestIntegrationsRoutes — HTTP-level tests for /api/integrations/*
# ---------------------------------------------------------------------------


@pytest.fixture
async def _admin_user(db_session_factory):
    """Create a User + ProjectMember(role='owner') and clean up after the test."""
    from app.models.project import Project, ProjectMember
    from app.models.user import User

    uid = uuid.uuid4()
    pid = uuid.uuid4()

    async with db_session_factory() as db:
        user = User(
            id=uid,
            email=f"admin-{uid}@test.example",
            display_name="Admin User",
            is_active=True,
            auth_provider="email",
        )
        db.add(user)
        await db.flush()  # ensure user row exists before project FK references it

        project = Project(
            id=pid,
            name="Test Project",
            slug=f"test-project-{uid.hex[:8]}",
            owner_id=uid,
            is_active=True,
        )
        membership = ProjectMember(
            project_id=pid,
            user_id=uid,
            role="owner",
            is_active=True,
        )
        db.add(project)
        db.add(membership)
        await db.commit()

    try:
        yield user
    finally:
        async with db_session_factory() as db:
            from sqlalchemy import delete as sql_delete

            await db.execute(sql_delete(ProjectMember).where(ProjectMember.user_id == uid))
            await db.execute(sql_delete(Project).where(Project.id == pid))
            from app.models.user import User as UserModel

            await db.execute(sql_delete(UserModel).where(UserModel.id == uid))
            await db.commit()


@pytest.fixture
async def _member_user(db_session_factory):
    """Create a User + ProjectMember(role='member') and clean up after the test."""
    from app.models.project import Project, ProjectMember
    from app.models.user import User

    uid = uuid.uuid4()
    pid = uuid.uuid4()

    async with db_session_factory() as db:
        user = User(
            id=uid,
            email=f"member-{uid}@test.example",
            display_name="Member User",
            is_active=True,
            auth_provider="email",
        )
        db.add(user)
        await db.flush()  # ensure user row exists before project FK references it

        project = Project(
            id=pid,
            name="Member Project",
            slug=f"member-project-{uid.hex[:8]}",
            owner_id=uid,
            is_active=True,
        )
        membership = ProjectMember(
            project_id=pid,
            user_id=uid,
            role="member",
            is_active=True,
        )
        db.add(project)
        db.add(membership)
        await db.commit()

    try:
        yield user
    finally:
        async with db_session_factory() as db:
            from sqlalchemy import delete as sql_delete

            from app.models.user import User as UserModel

            await db.execute(sql_delete(ProjectMember).where(ProjectMember.user_id == uid))
            await db.execute(sql_delete(Project).where(Project.id == pid))
            await db.execute(sql_delete(UserModel).where(UserModel.id == uid))
            await db.commit()


@pytest.fixture
def _patch_db(db_session_factory):
    """Patch app_state.db_session_factory to use the test session factory."""
    import app.app_state as app_state

    original = app_state.db_session_factory
    app_state.db_session_factory = db_session_factory
    yield
    app_state.db_session_factory = original


def _make_csrf_token() -> str:
    """Generate a valid signed CSRF token using the test APP_SECRET_KEY."""
    from app.auth.csrf import _generate_csrf_token

    return _generate_csrf_token()


@pytest.fixture
async def http_client(_patch_db):
    """AsyncClient pointed at the FastAPI app via ASGI transport.

    A pre-generated CSRF token is injected as both the ``csrf_token`` cookie
    and the ``x-csrf-token`` header on all requests so POST/DELETE routes pass
    the CSRF middleware without special per-test handling.
    """
    import httpx
    from httpx import ASGITransport

    from app.main import app

    csrf = _make_csrf_token()

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        cookies={"csrf_token": csrf},
        headers={"x-csrf-token": csrf},
    ) as client:
        yield client


class TestIntegrationsRoutes:
    """HTTP-level tests for /api/integrations/* endpoints."""

    # ------------------------------------------------------------------
    # 1. Unauthenticated → 401
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_list_unauthenticated_returns_401(self, http_client):
        """GET /api/integrations without auth returns 401."""
        with patch(
            "app.api.integrations_routes._resolve_user",
            new=AsyncMock(return_value=None),
        ):
            resp = await http_client.get("/api/integrations")
        assert resp.status_code == 401

    # ------------------------------------------------------------------
    # 2. Project-member (not admin) → 403
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_list_member_returns_403(self, http_client, _member_user):
        """GET /api/integrations as a project-member returns 403."""
        with patch(
            "app.api.integrations_routes._resolve_user",
            new=AsyncMock(return_value=_member_user),
        ):
            resp = await http_client.get("/api/integrations")
        assert resp.status_code == 403

    # ------------------------------------------------------------------
    # 3. Admin → 200, one item per SUPPORTED_PLATFORMS
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_list_admin_returns_200(self, http_client, _admin_user):
        """GET /api/integrations as project-owner returns 200 with one item per platform."""
        with patch(
            "app.api.integrations_routes._resolve_user",
            new=AsyncMock(return_value=_admin_user),
        ):
            resp = await http_client.get("/api/integrations")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert len(data["items"]) == len(SUPPORTED_PLATFORMS)  # one per SUPPORTED_PLATFORMS

    # ------------------------------------------------------------------
    # 4. POST creates a DB row → 200
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_upsert_creates_db_row(self, http_client, _admin_user, db_session_factory):
        """POST /api/integrations/snap saves a row to the DB."""
        try:
            with patch(
                "app.api.integrations_routes._resolve_user",
                new=AsyncMock(return_value=_admin_user),
            ):
                resp = await http_client.post(
                    "/api/integrations/snap",
                    json={"client_id": "snap-app-id-123", "client_secret": "snap-app-secret-xyz"},
                )
            assert resp.status_code == 200
            assert resp.json()["success"] is True

            # Verify the row landed in the DB
            async with db_session_factory() as db:
                creds = await get_oauth_app_credentials(db, "snap")
            assert creds.source == "db"
            assert creds.client_id == "snap-app-id-123"
        finally:
            async with db_session_factory() as db:
                await delete_oauth_app_credentials(db, platform="snap")
                await db.commit()

    # ------------------------------------------------------------------
    # 5. POST then GET shows source=db and configured=true
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_upsert_then_get_shows_db_source(self, http_client, _admin_user, db_session_factory):
        """POST then GET /api/integrations/linkedin shows source='db', configured=true."""
        resolve_mock = AsyncMock(return_value=_admin_user)
        try:
            with patch("app.api.integrations_routes._resolve_user", new=resolve_mock):
                await http_client.post(
                    "/api/integrations/linkedin",
                    json={"client_id": "li-id-route-test", "client_secret": "li-secret-route-test"},
                )
                resp = await http_client.get("/api/integrations/linkedin")

            assert resp.status_code == 200
            data = resp.json()
            assert data["configured"] is True
            assert data["source"] == "db"
            assert data["platform"] == "linkedin"
            assert data["redirect_uris"]  # non-empty list
            assert data["dev_console_url"]
        finally:
            async with db_session_factory() as db:
                await delete_oauth_app_credentials(db, platform="linkedin")
                await db.commit()

    # ------------------------------------------------------------------
    # 6. DELETE removes the row; subsequent GET shows unconfigured
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_delete_removes_row(self, http_client, _admin_user, db_session_factory):
        """DELETE /api/integrations/pinterest removes the row; GET returns unconfigured."""
        resolve_mock = AsyncMock(return_value=_admin_user)
        with patch("app.api.integrations_routes._resolve_user", new=resolve_mock):
            # Create
            await http_client.post(
                "/api/integrations/pinterest",
                json={"client_id": "pin-id-del-test", "client_secret": "pin-secret-del-test"},
            )
            # Delete
            del_resp = await http_client.delete("/api/integrations/pinterest")
            assert del_resp.status_code == 200
            assert del_resp.json()["deleted"] is True

            # Get → unconfigured
            get_resp = await http_client.get("/api/integrations/pinterest")
        assert get_resp.status_code == 200
        assert get_resp.json()["source"] == "unconfigured"
        assert get_resp.json()["configured"] is False

    # ------------------------------------------------------------------
    # 7. Unsupported platform → 400
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_unsupported_platform_returns_400(self, http_client, _admin_user):
        """POST /api/integrations/twitter returns 400 (unsupported platform)."""
        with patch(
            "app.api.integrations_routes._resolve_user",
            new=AsyncMock(return_value=_admin_user),
        ):
            resp = await http_client.post(
                "/api/integrations/twitter",
                json={"client_id": "tw-id", "client_secret": "tw-secret"},
            )
        assert resp.status_code == 400

    # ------------------------------------------------------------------
    # 8. Test endpoint → 404 when unconfigured
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_test_endpoint_returns_404_when_unconfigured(self, http_client, _admin_user):
        """POST /api/integrations/tiktok/test returns 404 when no credentials configured."""
        with patch(
            "app.api.integrations_routes._resolve_user",
            new=AsyncMock(return_value=_admin_user),
        ):
            resp = await http_client.post("/api/integrations/tiktok/test")
        assert resp.status_code == 404

    # ------------------------------------------------------------------
    # 9. Test endpoint → 200 ok=true when configured
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_test_endpoint_returns_ok_when_configured(
        self, http_client, _admin_user, db_session_factory
    ):
        """POST /api/integrations/meta/test returns 200 with ok=true after upsert."""
        resolve_mock = AsyncMock(return_value=_admin_user)
        try:
            with patch("app.api.integrations_routes._resolve_user", new=resolve_mock):
                await http_client.post(
                    "/api/integrations/meta",
                    json={"client_id": "meta-app-id-test-ok", "client_secret": "meta-app-secret-ok"},
                )
                resp = await http_client.post("/api/integrations/meta/test")

            assert resp.status_code == 200
            data = resp.json()
            assert data["ok"] is True
            assert data["platform"] == "meta"
            assert data["source"] == "db"
        finally:
            async with db_session_factory() as db:
                await delete_oauth_app_credentials(db, platform="meta")
                await db.commit()
