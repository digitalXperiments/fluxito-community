"""Regression tests for the cross-tenant / RBAC hardening pass.

Each test pins one fix: connection role gate, OAuth callback binding, admin
password resets, admin deactivation, knowledge RBAC, template visibility,
SQL literal validation, redirect / email-link hygiene and MCP tool gating.
"""

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


async def _user(db_session_factory, *, password=False, superadmin=False):
    from app.models.user import User

    uid = uuid.uuid4()
    async with db_session_factory() as db:
        db.add(
            User(
                id=uid,
                email=f"u-{uid.hex[:8]}@example.com",
                auth_provider="email",
                password_hash="x" if password else None,
                is_superadmin=superadmin,
            )
        )
        await db.commit()
    return str(uid)


async def _project(db_session_factory, owner_id, *, rbac=False):
    from app.models.project import Project, ProjectMember

    pid = uuid.uuid4()
    async with db_session_factory() as db:
        db.add(
            Project(
                id=pid, name="P", slug=f"p-{pid.hex[:10]}", owner_id=uuid.UUID(owner_id), rbac_enabled=rbac
            )
        )
        await db.flush()
        db.add(ProjectMember(project_id=pid, user_id=uuid.UUID(owner_id), role="owner"))
        await db.commit()
    return str(pid), f"p-{pid.hex[:10]}"


async def _member(db_session_factory, project_id, user_id, role="member"):
    from app.models.project import ProjectMember

    async with db_session_factory() as db:
        m = ProjectMember(project_id=uuid.UUID(project_id), user_id=uuid.UUID(user_id), role=role)
        db.add(m)
        await db.commit()
        return str(m.id)


def _signed_in(client, uid, project_id=None):
    from app.auth.uid_cookie import sign_uid

    client.cookies.set("uid", sign_uid(uid))
    if project_id:
        client.cookies.set("active_project_id", project_id)


def _as_user(uid):
    """Patch the full user-context builder (it needs Redis) for routes that use it."""
    from contextlib import ExitStack
    from unittest.mock import AsyncMock, patch

    ctx = SimpleNamespace(user_id=uid, email=f"{uid}@example.com", display_name=None, connections=[])
    stack = ExitStack()
    for target in (
        "app.api.google_oauth_routes._resolve_user_ctx",
        "app.api.knowledge_routes._resolve_user_ctx",
    ):
        stack.enter_context(patch(target, new=AsyncMock(return_value=ctx)))
    stack.enter_context(
        patch("app.api.google_oauth_routes._load_user_view", new=AsyncMock(return_value={"email": ctx.email}))
    )
    return stack


# ---------------------------------------------------------------------------
# Connection role gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_cannot_add_a_connection_but_admin_can(_client, db_session_factory):
    from app.auth.connection_gate import connection_gate

    owner = await _user(db_session_factory)
    pid, _ = await _project(db_session_factory, owner)
    member = await _user(db_session_factory)
    await _member(db_session_factory, pid, member, "member")

    _signed_in(_client, member, pid)
    resp = await _client.post("/api/connections/branch", json={"display_name": "x", "api_key": "k"})
    assert resp.status_code == 403

    from app.auth.uid_cookie import sign_uid

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/connections/branch",
        "query_string": b"",
        "headers": [(b"cookie", f"uid={sign_uid(owner)}; active_project_id={pid}".encode())],
    }
    assert await connection_gate(scope) is None  # owner passes the gate


@pytest.mark.asyncio
async def test_editing_a_connection_checks_the_connections_own_project(_patch_db, db_session_factory):
    """Admin in project A can't edit a row living in project B where they're only a member."""
    from app.auth.connection_gate import connection_gate
    from app.auth.uid_cookie import sign_uid
    from app.models.credential_connection import BranchConnection

    user = await _user(db_session_factory)
    pid_a, _ = await _project(db_session_factory, user)  # owner of A
    other = await _user(db_session_factory)
    pid_b, _ = await _project(db_session_factory, other)
    await _member(db_session_factory, pid_b, user, "member")  # plain member of B
    async with db_session_factory() as db:
        row = BranchConnection(
            user_id=uuid.UUID(user),
            project_id=uuid.UUID(pid_b),
            display_name="b",
            api_key_encrypted="x",
            secret_key_encrypted="",
            connection_status="active",
            is_active=True,
        )
        db.add(row)
        await db.commit()
        cid = str(row.id)

    scope = {
        "type": "http",
        "method": "PUT",
        "path": f"/api/connections/branch/{cid}",
        "query_string": b"",
        "headers": [(b"cookie", f"uid={sign_uid(user)}; active_project_id={pid_a}".encode())],
    }
    denied = await connection_gate(scope)
    assert denied is not None and denied.status_code == 403


@pytest.mark.asyncio
async def test_member_cannot_start_an_oauth_connect(_client, db_session_factory):
    owner = await _user(db_session_factory)
    pid, _ = await _project(db_session_factory, owner)
    member = await _user(db_session_factory)
    await _member(db_session_factory, pid, member)
    _signed_in(_client, member, pid)
    resp = await _client.get("/connect/meta/authorize")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_oauth_callback_must_come_from_the_user_who_started_it(_patch_db, db_session_factory):
    from app.auth.connection_gate import verify_oauth_callback
    from app.auth.uid_cookie import sign_uid

    owner = await _user(db_session_factory)
    pid, _ = await _project(db_session_factory, owner)
    victim = await _user(db_session_factory)

    def req(uid):
        return SimpleNamespace(
            cookies={"uid": sign_uid(uid)} if uid else {}, url=SimpleNamespace(path="/auth/meta/callback")
        )

    assert await verify_oauth_callback(req(owner), owner, pid) is None
    assert (await verify_oauth_callback(req(victim), owner, pid)).status_code == 403  # victim's browser
    assert (await verify_oauth_callback(req(None), owner, pid)).status_code == 403
    assert (await verify_oauth_callback(req(victim), victim, pid)).status_code == 403  # not an admin of pid


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_cannot_reset_password_of_a_user_with_their_own_workspace(_client, db_session_factory):
    attacker = await _user(db_session_factory)
    pid, slug = await _project(db_session_factory, attacker)
    victim = await _user(db_session_factory, password=True)
    await _project(db_session_factory, victim)  # the victim's own workspace
    member_id = await _member(db_session_factory, pid, victim)

    _signed_in(_client, attacker, pid)
    with _as_user(attacker):
        resp = await _client.post(f"/api/project/{slug}/members/{member_id}/reset-password")
    assert resp.status_code == 403
    assert "temp_password" not in resp.text


@pytest.mark.asyncio
async def test_admin_can_reset_password_of_an_account_created_for_the_project(_client, db_session_factory):
    owner = await _user(db_session_factory)
    pid, slug = await _project(db_session_factory, owner)
    invited = await _user(db_session_factory, password=True)  # only lives in this project
    member_id = await _member(db_session_factory, pid, invited)

    _signed_in(_client, owner, pid)
    with _as_user(owner):
        resp = await _client.post(f"/api/project/{slug}/members/{member_id}/reset-password")
    assert resp.status_code == 200 and resp.json()["temp_password"]


@pytest.mark.asyncio
async def test_admin_deactivation_signs_the_user_out_and_blocks_self_reactivation(
    _patch_db, db_session_factory
):
    from sqlalchemy import update

    from app.auth.uid_cookie import sign_uid
    from app.main import _drop_blocked_session
    from app.models.user import User

    uid = await _user(db_session_factory)
    async with db_session_factory() as db:
        await db.execute(
            update(User).where(User.id == uuid.UUID(uid)).values(is_active=False, deactivated_by_admin=True)
        )
        await db.commit()

    scope = {"type": "http", "headers": [(b"cookie", f"uid={sign_uid(uid)}".encode())]}
    await _drop_blocked_session(scope)
    assert scope["headers"] == []  # uid cookie removed → signed out

    from app.api.notification_routes import reactivate_account

    resp = await reactivate_account(SimpleNamespace(cookies={"uid": sign_uid(uid)}))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_profile_email_cannot_be_changed(_client, db_session_factory):
    uid = await _user(db_session_factory)
    _signed_in(_client, uid)
    resp = await _client.post("/api/profile", json={"email": "someone-else@example.com"})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# RBAC on web APIs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rbac_member_without_roles_cannot_write_knowledge(_client, db_session_factory):
    owner = await _user(db_session_factory)
    pid, _ = await _project(db_session_factory, owner, rbac=True)
    member = await _user(db_session_factory)
    await _member(db_session_factory, pid, member)
    _signed_in(_client, member, pid)
    with _as_user(member):
        resp = await _client.put("/api/business-context", json={"content": "overwrite"})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Input hygiene
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "nxt", ["/\t/evil.com", "/\n/evil.com", "//evil.com", "/\\evil.com", "https://evil.com"]
)
def test_next_url_rejects_off_site_targets(nxt):
    from app.utils import safe_next_url

    assert safe_next_url(nxt) == "/home"


def test_email_links_ignore_a_forged_host_header(monkeypatch):
    from app.config import settings
    from app.utils import trusted_base_url

    monkeypatch.setattr(settings, "APP_BASE_URL", "https://fluxito.app")
    req = SimpleNamespace(headers={"host": "evil.example", "x-forwarded-proto": "https"})
    assert trusted_base_url(req) == "https://fluxito.app"


# ---------------------------------------------------------------------------
# MCP tool gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inner_tool_calls_need_a_project():
    import app.app_state as _state
    from app.tools.registry import caller_has_full_access, inner_tool_permitted

    token = _state.current_user_ctx.set(SimpleNamespace(user_id=str(uuid.uuid4())))
    try:
        assert await inner_tool_permitted("tagmanager_write", {"action": "publish_container"}) is False
        assert await caller_has_full_access() is False
    finally:
        _state.current_user_ctx.reset(token)
