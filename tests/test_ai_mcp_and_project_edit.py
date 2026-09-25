# tests/test_ai_mcp_and_project_edit.py
"""AI & MCP hub (OAuth client list + revoke) and project rename (PATCH /api/project/{slug})."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

import app.app_state as app_state


@pytest.fixture
def _patch_db(db_session_factory):
    original = app_state.db_session_factory
    app_state.db_session_factory = db_session_factory
    yield
    app_state.db_session_factory = original


@pytest.fixture
async def _http_client(_patch_db):
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
        follow_redirects=False,
    ) as client:
        yield client


async def _seed_user(db_session_factory, email="owner@example.com"):
    from app.models.user import User

    async with db_session_factory() as db:
        u = User(email=email, display_name=email.split("@")[0])
        db.add(u)
        await db.commit()
        return u.id


async def _seed_sessions(db_session_factory, user_id):
    """Two live OAuth sessions for Claude, one expired + one revoked for Claude,
    one live session for a second client, and one PAT (never listed as a client)."""
    from app.models.connection import MCPClient
    from app.models.mcp_session import MCPSession

    now = datetime.utcnow()
    async with db_session_factory() as db:
        db.add(
            MCPClient(client_id="claude-test", client_name="Claude", redirect_uris=["https://claude.ai/cb"])
        )
        db.add(MCPClient(client_id="cursor-test", client_name="Cursor", redirect_uris=["cursor://cb"]))
        rows = [
            ("claude-test", "oauth", now + timedelta(hours=1), None, False),
            ("claude-test", "oauth", now - timedelta(hours=1), now + timedelta(days=20), False),
            ("claude-test", "oauth", now - timedelta(days=2), now - timedelta(days=1), False),
            ("claude-test", "oauth", now + timedelta(hours=1), None, True),
            ("cursor-test", "oauth", now + timedelta(hours=1), None, False),
            ("pat", "pat", now + timedelta(days=90), None, False),
        ]
        for i, (client_id, kind, access_exp, refresh_exp, revoked) in enumerate(rows):
            db.add(
                MCPSession(
                    user_id=user_id,
                    access_token_hash=f"hash-{user_id}-{i}",
                    client_id=client_id,
                    kind=kind,
                    access_token_expires_at=access_exp,
                    refresh_token_expires_at=refresh_exp,
                    is_revoked=revoked,
                    last_used_at=now - timedelta(minutes=i),
                )
            )
        await db.commit()


@pytest.mark.asyncio
async def test_list_oauth_clients_groups_live_sessions(_patch_db, db_session_factory):
    from app.auth.mcp_session_manager import list_oauth_clients

    uid = await _seed_user(db_session_factory)
    await _seed_sessions(db_session_factory, uid)

    clients = await list_oauth_clients(str(uid))
    by_id = {c["client_id"]: c for c in clients}
    assert set(by_id) == {"claude-test", "cursor-test"}  # PAT is not an OAuth client
    assert by_id["claude-test"]["client_name"] == "Claude"
    assert by_id["claude-test"]["sessions"] == 2  # expired + revoked sessions excluded
    assert by_id["cursor-test"]["sessions"] == 1
    assert by_id["claude-test"]["authorized_at"] and by_id["claude-test"]["last_used_at"]


@pytest.mark.asyncio
async def test_revoke_oauth_client_only_touches_that_client(_patch_db, db_session_factory):
    from app.auth.mcp_session_manager import list_oauth_clients, list_pats, revoke_oauth_client

    uid = await _seed_user(db_session_factory)
    await _seed_sessions(db_session_factory, uid)

    assert await revoke_oauth_client(str(uid), "claude-test") == 3  # every non-revoked Claude row
    assert await revoke_oauth_client(str(uid), "claude-test") == 0
    remaining = await list_oauth_clients(str(uid))
    assert [c["client_id"] for c in remaining] == ["cursor-test"]
    assert len(await list_pats(str(uid))) == 1  # PATs untouched


@pytest.mark.asyncio
async def test_revoke_oauth_client_is_scoped_to_the_user(_patch_db, db_session_factory):
    from app.auth.mcp_session_manager import list_oauth_clients, revoke_oauth_client

    alice = await _seed_user(db_session_factory, "alice@example.com")
    await _seed_sessions(db_session_factory, alice)
    bob = await _seed_user(db_session_factory, "bob@example.com")

    assert await revoke_oauth_client(str(bob), "claude-test") == 0
    assert {c["client_id"] for c in await list_oauth_clients(str(alice))} == {"claude-test", "cursor-test"}


@pytest.mark.asyncio
async def test_ai_clients_api_requires_auth(_http_client):
    with patch("app.api.ai_mcp_routes._resolve_user_ctx", new=AsyncMock(return_value=None)):
        assert (await _http_client.get("/api/ai/clients")).status_code == 401
        assert (await _http_client.post("/api/ai/clients/claude-test/revoke")).status_code == 401
        page = await _http_client.get("/ai")
    assert page.status_code == 302
    assert page.headers["location"].startswith("/signin")


@pytest.mark.asyncio
async def test_ai_clients_api_lists_and_revokes(_http_client, db_session_factory):
    uid = await _seed_user(db_session_factory)
    await _seed_sessions(db_session_factory, uid)
    ctx = type("C", (), {"user_id": str(uid), "email": "owner@example.com"})()

    with patch("app.api.ai_mcp_routes._resolve_user_ctx", new=AsyncMock(return_value=ctx)):
        listed = await _http_client.get("/api/ai/clients")
        assert listed.status_code == 200
        assert {c["client_id"] for c in listed.json()["clients"]} == {"claude-test", "cursor-test"}

        revoked = await _http_client.post("/api/ai/clients/cursor-test/revoke")
        assert revoked.status_code == 200
        assert revoked.json()["revoked_sessions"] == 1

        again = await _http_client.post("/api/ai/clients/cursor-test/revoke")
        assert again.status_code == 404


# ── PATCH /api/project/{slug} ─────────────────────────────────────────────


async def _seed_project(db_session_factory, owner_id, slug="acme-retail", member_role=None):
    from app.models.project import Project, ProjectMember
    from app.models.user import User

    async with db_session_factory() as db:
        p = Project(name="Acme Retail", slug=slug, owner_id=owner_id)
        db.add(p)
        await db.flush()
        db.add(ProjectMember(project_id=p.id, user_id=owner_id, role="owner"))
        member_id = None
        if member_role:
            m = User(email=f"{member_role}@example.com")
            db.add(m)
            await db.flush()
            db.add(ProjectMember(project_id=p.id, user_id=m.id, role=member_role))
            member_id = m.id
        await db.commit()
        return p.id, member_id


def _as_user(user_id):
    return patch(
        "app.api.project_routes._resolve_user",
        new=AsyncMock(return_value={"user_id": str(user_id), "email": "x@example.com"}),
    )


@pytest.mark.asyncio
async def test_project_rename_and_slug_change(_http_client, db_session_factory):
    from sqlalchemy import select

    from app.models.project import Project

    owner = await _seed_user(db_session_factory)
    pid, _ = await _seed_project(db_session_factory, owner)

    with _as_user(owner):
        r = await _http_client.patch("/api/project/acme-retail", json={"name": "Acme UAE"})
        assert r.status_code == 200
        assert r.json()["project"] == {"name": "Acme UAE", "slug": "acme-retail"}
        assert "redirect_url" not in r.json()

        r = await _http_client.patch(
            "/api/project/acme-retail", json={"name": "Acme UAE", "slug": "acme-uae"}
        )
        assert r.status_code == 200
        assert r.json()["redirect_url"] == "/project/acme-uae/settings"

    async with db_session_factory() as db:
        proj = (await db.execute(select(Project).where(Project.id == pid))).scalar_one()
    assert (proj.name, proj.slug) == ("Acme UAE", "acme-uae")


@pytest.mark.asyncio
async def test_project_rename_validation(_http_client, db_session_factory):
    owner = await _seed_user(db_session_factory)
    await _seed_project(db_session_factory, owner)
    other_owner = await _seed_user(db_session_factory, "other@example.com")
    await _seed_project(db_session_factory, other_owner, slug="taken-slug")

    with _as_user(owner):
        assert (await _http_client.patch("/api/project/acme-retail", json={"name": ""})).status_code == 400
        bad = await _http_client.patch("/api/project/acme-retail", json={"name": "Acme", "slug": "Bad Slug!"})
        assert bad.status_code == 400
        taken = await _http_client.patch(
            "/api/project/acme-retail", json={"name": "Acme", "slug": "taken-slug"}
        )
        assert taken.status_code == 409


@pytest.mark.asyncio
async def test_project_rename_requires_owner_or_admin(_http_client, db_session_factory):
    owner = await _seed_user(db_session_factory)
    _, member = await _seed_project(db_session_factory, owner, member_role="member")
    _, admin = await _seed_project(db_session_factory, owner, slug="with-admin", member_role="admin")

    with _as_user(member):
        denied = await _http_client.patch("/api/project/acme-retail", json={"name": "Nope"})
    assert denied.status_code == 403

    with _as_user(admin):
        allowed = await _http_client.patch("/api/project/with-admin", json={"name": "Renamed by admin"})
    assert allowed.status_code == 200
