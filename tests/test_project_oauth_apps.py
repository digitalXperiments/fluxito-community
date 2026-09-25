"""Per-project OAuth apps: resolution, caching, reconnect flagging, routes.

A project may bring its own developer app per platform. Connect flows and
token refreshes in that project use it; every other project keeps the
install-wide app. Tokens are bound to the app that issued them, so changing
a project's app flags its existing connections for a reconnect.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

import app.app_state as app_state
from app.auth.oauth_app_credentials import (
    OAuthAppNotConfigured,
    _cache_clear_all,
    delete_project_oauth_app_credentials,
    get_oauth_app_credentials,
    get_oauth_app_credentials_cached,
    list_project_oauth_app_status,
    mark_connections_for_reconnect,
    upsert_oauth_app_credentials,
    upsert_project_oauth_app_credentials,
)


@pytest.fixture(autouse=True)
def _clear_helper_cache():
    _cache_clear_all()
    yield
    _cache_clear_all()


@pytest.fixture
def _patch_db(db_session_factory):
    original = app_state.db_session_factory
    app_state.db_session_factory = db_session_factory
    yield
    app_state.db_session_factory = original


async def _make_project(db_session_factory, *, role="owner"):
    """A user + project with the user as `role`. Returns (user_id, project_id, slug)."""
    from app.models.project import Project, ProjectMember
    from app.models.user import User

    uid, pid = uuid.uuid4(), uuid.uuid4()
    slug = f"p-{pid.hex[:10]}"
    async with db_session_factory() as db:
        db.add(User(id=uid, email=f"u-{uid.hex[:8]}@example.com", auth_provider="email"))
        await db.flush()
        db.add(Project(id=pid, name="P", slug=slug, owner_id=uid))
        await db.flush()
        db.add(ProjectMember(project_id=pid, user_id=uid, role=role))
        await db.commit()
    return uid, pid, slug


async def _make_connection(db_session_factory, *, user_id, project_id, provider="meta", status="active"):
    from app.models.connection import OAuthConnection

    cid = uuid.uuid4()
    async with db_session_factory() as db:
        db.add(
            OAuthConnection(
                id=cid,
                user_id=user_id,
                project_id=project_id,
                provider=provider,
                google_email=f"{provider}-{cid.hex[:6]}@example.com",
                access_token_encrypted="at",
                refresh_token_encrypted="rt-plain",
                scopes=[],
                is_active=True,
                connection_status=status,
            )
        )
        await db.commit()
    return cid


async def _status_of(db_session_factory, cid):
    from app.models.connection import OAuthConnection

    async with db_session_factory() as db:
        return (await db.get(OAuthConnection, cid)).connection_status


async def _set_install(db, platform, client_id, extra=None):
    await upsert_oauth_app_credentials(
        db,
        platform=platform,
        client_id=client_id,
        client_secret="install-secret",
        extra=extra,
        configured_by_user_id=None,
    )


async def _set_project(db, project_id, platform, client_id, secret="project-secret", extra=None):
    return await upsert_project_oauth_app_credentials(
        db,
        project_id=project_id,
        platform=platform,
        client_id=client_id,
        client_secret=secret,
        extra=extra,
        configured_by_user_id=None,
    )


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_project_app_wins_and_other_projects_fall_back(db_session_factory):
    _, pid_a, _ = await _make_project(db_session_factory)
    _, pid_b, _ = await _make_project(db_session_factory)
    async with db_session_factory() as db:
        await _set_install(db, "meta", "install-meta-id")
        await _set_project(db, pid_a, "meta", "project-a-meta-id")
        await db.commit()

    async with db_session_factory() as db:
        a = await get_oauth_app_credentials(db, "meta", project_id=pid_a)
        b = await get_oauth_app_credentials(db, "meta", project_id=str(pid_b))
        install = await get_oauth_app_credentials(db, "meta")

    assert (a.source, a.client_id, a.client_secret) == ("project", "project-a-meta-id", "project-secret")
    assert (b.source, b.client_id) == ("db", "install-meta-id")
    assert (install.source, install.client_id) == ("db", "install-meta-id")


@pytest.mark.asyncio
async def test_project_app_works_without_any_install_app(db_session_factory):
    _, pid, _ = await _make_project(db_session_factory)
    async with db_session_factory() as db:
        await _set_project(db, pid, "tiktok", "own-tiktok-id")
        await db.commit()

    async with db_session_factory() as db:
        assert (await get_oauth_app_credentials(db, "tiktok", project_id=pid)).client_id == "own-tiktok-id"
        with pytest.raises(OAuthAppNotConfigured):
            await get_oauth_app_credentials(db, "tiktok")


@pytest.mark.asyncio
async def test_cache_is_keyed_per_project(db_session_factory):
    _, pid_a, _ = await _make_project(db_session_factory)
    _, pid_b, _ = await _make_project(db_session_factory)
    async with db_session_factory() as db:
        await _set_install(db, "snap", "install-snap-id")
        await _set_project(db, pid_a, "snap", "a-snap-id")
        await db.commit()

    async with db_session_factory() as db:
        assert (await get_oauth_app_credentials_cached(db, "snap", project_id=pid_a)).client_id == "a-snap-id"
        assert (
            await get_oauth_app_credentials_cached(db, "snap", project_id=pid_b)
        ).client_id == "install-snap-id"
        assert (await get_oauth_app_credentials_cached(db, "snap")).client_id == "install-snap-id"

    # Removing A's app invalidates only A's cache entry → A falls back.
    async with db_session_factory() as db:
        await delete_project_oauth_app_credentials(db, project_id=pid_a, platform="snap")
        await db.commit()
    async with db_session_factory() as db:
        assert (
            await get_oauth_app_credentials_cached(db, "snap", project_id=pid_a)
        ).client_id == "install-snap-id"


@pytest.mark.asyncio
async def test_upsert_reports_when_the_app_changed(db_session_factory):
    _, pid, _ = await _make_project(db_session_factory)
    async with db_session_factory() as db:
        assert await _set_project(db, pid, "linkedin", "li-1") is True  # new override
        await db.commit()
    async with db_session_factory() as db:
        assert await _set_project(db, pid, "linkedin", "li-1", secret="rotated") is False  # secret rotation
        await db.commit()
    async with db_session_factory() as db:
        assert await _set_project(db, pid, "linkedin", "li-2") is True  # different app
        await db.commit()


@pytest.mark.asyncio
async def test_mark_connections_for_reconnect_is_scoped(db_session_factory):
    uid_a, pid_a, _ = await _make_project(db_session_factory)
    uid_b, pid_b, _ = await _make_project(db_session_factory)
    meta_a = await _make_connection(db_session_factory, user_id=uid_a, project_id=pid_a, provider="meta")
    google_a = await _make_connection(db_session_factory, user_id=uid_a, project_id=pid_a, provider="google")
    meta_b = await _make_connection(db_session_factory, user_id=uid_b, project_id=pid_b, provider="meta")

    async with db_session_factory() as db:
        n = await mark_connections_for_reconnect(db, project_id=pid_a, platform="meta")
        await db.commit()

    assert n == 1
    assert await _status_of(db_session_factory, meta_a) == "broken"
    assert await _status_of(db_session_factory, google_a) == "active"
    assert await _status_of(db_session_factory, meta_b) == "active"


@pytest.mark.asyncio
async def test_status_list_marks_own_fallback_and_unavailable(db_session_factory):
    _, pid, _ = await _make_project(db_session_factory)
    async with db_session_factory() as db:
        await _set_install(db, "google", "install-google-id")
        await _set_project(db, pid, "meta", "own-meta-id-123456")
        await db.commit()
    async with db_session_factory() as db:
        items = {i["platform"]: i for i in await list_project_oauth_app_status(db, pid)}

    assert items["meta"]["source"] == "project"
    assert items["meta"]["client_id_masked"] == "own-me…3456"
    assert items["google"]["source"] == "db"
    assert items["tiktok"]["source"] == "unconfigured"


# ---------------------------------------------------------------------------
# Token refresh path — Google Ads uses the connection's project app
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_google_ads_refresh_uses_the_connection_projects_app(db_session_factory):
    from app.connectors.google_ads import GoogleAdsConnector

    uid, pid, _ = await _make_project(db_session_factory)
    cid = await _make_connection(db_session_factory, user_id=uid, project_id=pid, provider="google")
    async with db_session_factory() as db:
        await _set_install(db, "google", "install-google-id", extra={"developer_token": "install-dev"})
        await _set_project(db, pid, "google", "own-google-id", extra={"developer_token": "own-dev"})
        await db.commit()

    tm = type("TM", (), {"db_session_factory": db_session_factory, "decrypt": lambda self, v: v})()
    refresh, dev, client_id, secret = await GoogleAdsConnector(tm)._get_client_inputs(str(cid))

    assert (refresh, dev, client_id, secret) == ("rt-plain", "own-dev", "own-google-id", "project-secret")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@pytest.fixture
async def _client(_patch_db):
    import httpx
    from httpx import ASGITransport

    from app.auth.csrf import _generate_csrf_token
    from app.main import app

    csrf = _generate_csrf_token()
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        cookies={"csrf_token": csrf},
        headers={"x-csrf-token": csrf},
    ) as client:
        yield client


def _as(uid):
    return patch(
        "app.api.project_oauth_apps_routes._resolve_user",
        new=AsyncMock(return_value={"user_id": str(uid), "email": "x@example.com"}),
    )


@pytest.mark.asyncio
async def test_owner_saves_app_and_existing_connections_are_flagged(_client, db_session_factory):
    uid, pid, slug = await _make_project(db_session_factory)
    conn = await _make_connection(db_session_factory, user_id=uid, project_id=pid, provider="meta")

    with _as(uid):
        resp = await _client.post(
            f"/api/project/{slug}/oauth-apps/meta",
            json={"client_id": "own-meta-app", "client_secret": "s3cret!"},
        )
        listed = await _client.get(f"/api/project/{slug}/oauth-apps")
        # Same app, rotated secret: nothing to reconnect.
        rotated = await _client.post(
            f"/api/project/{slug}/oauth-apps/meta",
            json={"client_id": "own-meta-app", "client_secret": "n3w-secret"},
        )

    assert resp.status_code == 200
    assert resp.json()["reconnect_count"] == 1
    assert await _status_of(db_session_factory, conn) == "broken"
    meta = next(i for i in listed.json()["items"] if i["platform"] == "meta")
    assert meta["source"] == "project"
    assert rotated.json()["reconnect_count"] == 0

    async with db_session_factory() as db:
        creds = await get_oauth_app_credentials(db, "meta", project_id=pid)
    assert (creds.client_id, creds.client_secret) == ("own-meta-app", "n3w-secret")


@pytest.mark.asyncio
async def test_delete_falls_back_and_flags_connections(_client, db_session_factory):
    uid, pid, slug = await _make_project(db_session_factory)
    async with db_session_factory() as db:
        await _set_project(db, pid, "reddit", "own-reddit")
        await db.commit()
    conn = await _make_connection(db_session_factory, user_id=uid, project_id=pid, provider="reddit")

    with _as(uid):
        resp = await _client.delete(f"/api/project/{slug}/oauth-apps/reddit")

    assert resp.json() == {"success": True, "deleted": True, "reconnect_count": 1}
    assert await _status_of(db_session_factory, conn) == "broken"


@pytest.mark.asyncio
async def test_member_cannot_manage_project_apps(_client, db_session_factory):
    uid, _, slug = await _make_project(db_session_factory, role="member")
    with _as(uid):
        listed = await _client.get(f"/api/project/{slug}/oauth-apps")
        saved = await _client.post(
            f"/api/project/{slug}/oauth-apps/meta", json={"client_id": "x" * 8, "client_secret": "y" * 8}
        )
    assert listed.status_code == 403
    assert saved.status_code == 403


@pytest.mark.asyncio
async def test_non_member_cannot_manage_another_projects_apps(_client, db_session_factory):
    _, _, slug = await _make_project(db_session_factory)
    outsider, _, _ = await _make_project(db_session_factory)
    with _as(outsider):
        resp = await _client.post(
            f"/api/project/{slug}/oauth-apps/meta", json={"client_id": "x" * 8, "client_secret": "y" * 8}
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_google_project_app_only_needs_the_data_callback(_client, db_session_factory):
    uid, _, slug = await _make_project(db_session_factory)
    with _as(uid):
        resp = await _client.get(f"/api/project/{slug}/oauth-apps/google")
    uris = resp.json()["redirect_uris"]
    assert len(uris) == 1 and uris[0].endswith("/auth/google/data/callback")
