"""Read-only MCP tokens, linked-resource scoping, masked credentials and pending invites."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import app.app_state as app_state


@pytest.fixture
def _patch_db(db_session_factory):
    original = app_state.db_session_factory
    app_state.db_session_factory = db_session_factory
    yield
    app_state.db_session_factory = original


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


async def _user(db_session_factory, email=None):
    from app.models.user import User

    uid = uuid.uuid4()
    async with db_session_factory() as db:
        db.add(User(id=uid, email=email or f"u-{uid.hex[:8]}@example.com", auth_provider="email"))
        await db.commit()
    return str(uid)


async def _project(db_session_factory, owner_id):
    from app.models.project import Project, ProjectMember

    pid = uuid.uuid4()
    slug = f"p-{pid.hex[:10]}"
    async with db_session_factory() as db:
        db.add(Project(id=pid, name="Acme", slug=slug, owner_id=uuid.UUID(owner_id)))
        await db.flush()
        db.add(ProjectMember(project_id=pid, user_id=uuid.UUID(owner_id), role="owner"))
        await db.commit()
    return str(pid), slug


def _sign_in(client, uid):
    from app.auth.uid_cookie import sign_uid

    client.cookies.set("uid", sign_uid(uid))


def _as_user(uid, email):
    from contextlib import ExitStack
    from unittest.mock import AsyncMock, patch

    ctx = SimpleNamespace(user_id=uid, email=email, display_name=None, connections=[])
    stack = ExitStack()
    stack.enter_context(
        patch("app.api.google_oauth_routes._resolve_user_ctx", new=AsyncMock(return_value=ctx))
    )
    stack.enter_context(
        patch("app.api.google_oauth_routes._load_user_view", new=AsyncMock(return_value={"email": email}))
    )
    return stack


# ---------------------------------------------------------------------------
# 1. Read-only MCP tokens
# ---------------------------------------------------------------------------


def test_write_calls_are_classified():
    from app.auth.permissions import is_write_call

    assert is_write_call("tagmanager_write") is True
    assert is_write_call("tagmanager_read") is False
    assert is_write_call("generic_tool_write") is True
    assert is_write_call("run_script") is False  # inner calls are checked one by one


@pytest.mark.asyncio
async def test_read_only_token_cannot_write_even_as_owner():
    from app.tools.registry import _tool_permitted_for_call

    token = app_state.current_token_scopes_ctx.set(["read"])
    try:
        assert await _tool_permitted_for_call("tagmanager_write", {}, None, None) is False
        assert await _tool_permitted_for_call("tagmanager_read", {}, None, None) is True
    finally:
        app_state.current_token_scopes_ctx.reset(token)
    # A read + write token (and non-MCP callers) may write.
    token = app_state.current_token_scopes_ctx.set(["read", "write"])
    try:
        assert await _tool_permitted_for_call("tagmanager_write", {}, None, None) is True
    finally:
        app_state.current_token_scopes_ctx.reset(token)


@pytest.mark.asyncio
async def test_consent_read_only_box_issues_a_read_only_grant(monkeypatch):
    from app.auth import mcp_oauth_server
    from app.auth.uid_cookie import sign_uid

    uid = "11111111-1111-1111-1111-111111111111"
    stored = {
        "mcp_oauth_state:s1": (
            '{"client_id": "c", "redirect_uri": "https://claude.ai/api/mcp/auth_callback", "scope": "read write",'
            ' "state": "s1", "code_challenge": "x", "code_challenge_method": "S256",'
            f' "user_id": "{uid}", "consent_nonce": "n"}}'
        )
    }

    class _Redis:
        async def get(self, k):
            return stored.get(k)

        async def delete(self, k):
            stored.pop(k, None)

    granted = {}

    async def _issue(**kw):
        granted.update(kw)
        return "code"

    monkeypatch.setattr(mcp_oauth_server.app_state, "redis_client", _Redis())
    monkeypatch.setattr(mcp_oauth_server, "_issue_mcp_auth_code", _issue)
    await mcp_oauth_server.authorize_decision(
        request=SimpleNamespace(cookies={"uid": sign_uid(uid)}),
        state="s1",
        consent="allow",
        consent_nonce="n",
        read_only="1",
    )
    assert granted["scopes"] == ["read"]


# ---------------------------------------------------------------------------
# 2. Linked resources
# ---------------------------------------------------------------------------


def _ctx(**lists):
    return SimpleNamespace(
        ga4_properties=lists.get("ga4", []),
        gtm_containers=lists.get("gtm", []),
        ads_accounts=lists.get("ads", []),
    )


def test_unlinked_resources_are_refused():
    from app.auth.resource_scope import resource_violation

    ctx = _ctx(
        ga4=[{"property_id": "properties/111"}],
        gtm=[{"container_id": "222", "public_id": "GTM-ABC"}],
        ads=[{"customer_id": "123-456-7890"}],
    )
    assert resource_violation("analytics_read", {"platform": "ga4", "property_id": "111"}, ctx) is None
    assert resource_violation("analytics_read", {"platform": "ga4", "property_id": "properties/999"}, ctx)
    assert resource_violation("tagmanager_write", {"container_id": "GTM-abc"}, ctx) is None
    assert resource_violation("tagmanager_write", {"container_id": "333"}, ctx)
    assert (
        resource_violation("marketing_read", {"platform": "google_ads", "customer_id": "1234567890"}, ctx)
        is None
    )
    assert resource_violation("marketing_write", {"platform": "google_ads", "customer_id": "999"}, ctx)
    # Nested params are checked too.
    assert resource_violation("analytics_read", {"platform": "ga4", "params": {"property_id": "999"}}, ctx)


def test_no_discovered_resources_means_not_enforced():
    from app.auth.resource_scope import resource_violation

    assert resource_violation("tagmanager_write", {"container_id": "333"}, _ctx()) is None


@pytest.mark.asyncio
async def test_owner_can_unlink_a_resource_and_member_cannot(_client, db_session_factory):
    from app.models.connection import OAuthConnection
    from app.models.project import ProjectMember
    from app.models.token import GTMContainer

    owner = await _user(db_session_factory)
    pid, _ = await _project(db_session_factory, owner)
    async with db_session_factory() as db:
        conn = OAuthConnection(
            user_id=uuid.UUID(owner),
            project_id=uuid.UUID(pid),
            provider="google",
            google_email="g@example.com",
            access_token_encrypted="a",
            refresh_token_encrypted="r",
            scopes=[],
            is_active=True,
            connection_status="active",
        )
        db.add(conn)
        await db.flush()
        cont = GTMContainer(connection_id=conn.id, account_id="1", container_id="2", public_id="GTM-X")
        db.add(cont)
        await db.commit()
        cid, rid = str(conn.id), str(cont.id)

    _sign_in(_client, owner)
    listed = await _client.get(f"/api/connections/google/{cid}/resources")
    assert listed.status_code == 200 and listed.json()["gtm"][0]["linked"] is True
    resp = await _client.put(
        f"/api/connections/google/{cid}/resources", json={"type": "gtm", "id": rid, "linked": False}
    )
    assert resp.status_code == 200 and resp.json()["linked"] is False

    member = await _user(db_session_factory)
    async with db_session_factory() as db:
        db.add(ProjectMember(project_id=uuid.UUID(pid), user_id=uuid.UUID(member), role="member"))
        await db.commit()
    _sign_in(_client, member)
    assert (await _client.get(f"/api/connections/google/{cid}/resources")).status_code == 403


# ---------------------------------------------------------------------------
# 3. Masked credentials
# ---------------------------------------------------------------------------


def test_secret_hint_shows_only_the_last_four():
    from app.api.google_oauth_routes import _secret_hint

    assert _secret_hint("sk-live-abcdef123456") == "••••3456"
    assert _secret_hint("short") == "••••"
    assert _secret_hint(None) == "••••"


# ---------------------------------------------------------------------------
# 4. Pending invites
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inviting_an_existing_user_grants_nothing_until_accepted(_client, db_session_factory):
    from sqlalchemy import select

    from app.models.project import ProjectMember

    owner = await _user(db_session_factory, "owner@example.com")
    pid, slug = await _project(db_session_factory, owner)
    invitee = await _user(db_session_factory, "invitee@example.com")

    _sign_in(_client, owner)
    with _as_user(owner, "owner@example.com"):
        resp = await _client.post(
            f"/api/project/{slug}/members", json={"email": "invitee@example.com", "role": "admin"}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert "temp_password" not in body and body["invite_url"].rsplit("/", 1)[-1]

    async with db_session_factory() as db:
        assert (
            await db.execute(
                select(ProjectMember).where(
                    ProjectMember.project_id == uuid.UUID(pid), ProjectMember.user_id == uuid.UUID(invitee)
                )
            )
        ).first() is None

    _sign_in(_client, invitee)
    mine = (await _client.get("/api/invites/mine")).json()["invites"]
    assert len(mine) == 1 and mine[0]["project_name"] == "Acme"
    assert (await _client.post(f"/api/invites/{mine[0]['id']}/accept")).status_code == 200
    async with db_session_factory() as db:
        m = (
            await db.execute(
                select(ProjectMember).where(
                    ProjectMember.project_id == uuid.UUID(pid), ProjectMember.user_id == uuid.UUID(invitee)
                )
            )
        ).scalar_one()
    assert m.role == "admin" and m.is_active


@pytest.mark.asyncio
async def test_invite_for_someone_else_cannot_be_accepted(_client, db_session_factory):
    from app.services.invites import create_invite

    owner = await _user(db_session_factory)
    pid, _ = await _project(db_session_factory, owner)
    inv, _token = await create_invite(
        project_id=uuid.UUID(pid), email="alice@example.com", role="member", invited_by=uuid.UUID(owner)
    )
    mallory = await _user(db_session_factory, "mallory@example.com")
    _sign_in(_client, mallory)
    assert (await _client.post(f"/api/invites/{inv.id}/accept")).status_code == 400


@pytest.mark.asyncio
async def test_new_person_creates_account_from_the_link(_client, db_session_factory):
    from app.services.invites import create_invite

    owner = await _user(db_session_factory)
    pid, _ = await _project(db_session_factory, owner)
    _inv, token = await create_invite(
        project_id=uuid.UUID(pid), email="new@example.com", role="member", invited_by=uuid.UUID(owner)
    )
    page = await _client.get(f"/invite/{token}")
    assert page.status_code == 200 and "Create account" in page.text
    resp = await _client.post(
        "/api/invites/by-token/register",
        json={"token": token, "password": "correct-horse-1", "display_name": "N"},
    )
    assert resp.status_code == 200 and "uid" in resp.cookies
    # The link is single use.
    again = await _client.post(
        "/api/invites/by-token/register", json={"token": token, "password": "correct-horse-1"}
    )
    assert again.status_code == 404


@pytest.mark.asyncio
async def test_reinvite_retires_the_old_link(_patch_db, db_session_factory):
    from app.services.invites import create_invite, get_by_token

    owner = await _user(db_session_factory)
    pid, _ = await _project(db_session_factory, owner)
    _a, t1 = await create_invite(
        project_id=uuid.UUID(pid), email="x@example.com", role="member", invited_by=uuid.UUID(owner)
    )
    _b, t2 = await create_invite(
        project_id=uuid.UUID(pid), email="x@example.com", role="member", invited_by=uuid.UUID(owner)
    )
    assert (await get_by_token(t1)).status == "revoked"
    assert (await get_by_token(t2)).status == "pending"
