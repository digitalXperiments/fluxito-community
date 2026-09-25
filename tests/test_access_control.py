# tests/test_access_control.py
"""Backend tests for the access-control core (super-admin + admin panel + request-access)."""

import uuid

import pytest

import app.app_state as app_state


@pytest.fixture
def _patch_db(db_session_factory):
    original = app_state.db_session_factory
    app_state.db_session_factory = db_session_factory
    yield
    app_state.db_session_factory = original


@pytest.mark.asyncio
async def test_user_has_is_superadmin_default_false(_patch_db, db_session_factory):
    from sqlalchemy import select

    from app.models.user import User

    async with db_session_factory() as db:
        u = User(email="a@example.com")
        db.add(u)
        await db.flush()
        uid = u.id
        await db.commit()
    async with db_session_factory() as db:
        u = (await db.execute(select(User).where(User.id == uid))).scalar_one()
        assert u.is_superadmin is False


@pytest.mark.asyncio
async def test_access_request_model_persists(_patch_db, db_session_factory):
    from sqlalchemy import select

    from app.models.access_request import AccessRequest

    async with db_session_factory() as db:
        r = AccessRequest(name="Jane", email="jane@example.com", use_case="testing")
        db.add(r)
        await db.flush()
        rid = r.id
        await db.commit()
    async with db_session_factory() as db:
        r = (await db.execute(select(AccessRequest).where(AccessRequest.id == rid))).scalar_one()
        assert r.status == "pending"
        assert r.email == "jane@example.com"


@pytest.mark.asyncio
async def test_load_user_view_includes_is_superadmin(_patch_db, db_session_factory):
    from types import SimpleNamespace

    from app.api.google_oauth_routes import _load_user_view
    from app.models.user import User

    async with db_session_factory() as db:
        u = User(email="boss@example.com", is_superadmin=True)
        db.add(u)
        await db.flush()
        uid = str(u.id)
        await db.commit()

    view = await _load_user_view(SimpleNamespace(user_id=uid, email="boss@example.com"))
    assert view["is_superadmin"] is True


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
    ) as client:
        yield client


async def _make_user(db_session_factory, email, *, is_superadmin=False):
    from app.models.user import User

    async with db_session_factory() as db:
        u = User(email=email, is_superadmin=is_superadmin)
        db.add(u)
        await db.flush()
        uid = str(u.id)
        await db.commit()
        return uid


@pytest.mark.asyncio
async def test_admin_users_requires_superadmin(_http_client, db_session_factory):
    from unittest.mock import AsyncMock, patch

    uid = await _make_user(db_session_factory, "plain@example.com", is_superadmin=False)
    with patch(
        "app.api.admin_routes._resolve_user_ctx",
        new=AsyncMock(return_value=type("C", (), {"user_id": uid, "email": "plain@example.com"})()),
    ):
        resp = await _http_client.get("/api/admin/users")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_users_lists_for_superadmin(_http_client, db_session_factory):
    from unittest.mock import AsyncMock, patch

    sid = await _make_user(db_session_factory, "super@example.com", is_superadmin=True)
    await _make_user(db_session_factory, "member@example.com", is_superadmin=False)
    with patch(
        "app.api.admin_routes._resolve_user_ctx",
        new=AsyncMock(return_value=type("C", (), {"user_id": sid, "email": "super@example.com"})()),
    ):
        resp = await _http_client.get("/api/admin/users")
    assert resp.status_code == 200
    emails = [u["email"] for u in resp.json()["users"]]
    assert "super@example.com" in emails and "member@example.com" in emails


@pytest.mark.asyncio
async def test_admin_users_unauthenticated_401(_http_client, db_session_factory):
    from unittest.mock import AsyncMock, patch

    with patch("app.api.admin_routes._resolve_user_ctx", new=AsyncMock(return_value=None)):
        resp = await _http_client.get("/api/admin/users")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_admin_page_unauthenticated_redirects_to_homepage(_http_client, monkeypatch):
    from unittest.mock import AsyncMock, patch

    # Keep the first-run gate open so the admin route (not the empty-users gate) answers.
    monkeypatch.setattr("app.main._setup_complete", True)
    with patch("app.api.admin_routes._resolve_user_ctx", new=AsyncMock(return_value=None)):
        resp = await _http_client.get("/admin")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/"


@pytest.mark.asyncio
async def test_admin_page_non_superadmin_redirects_to_homepage(_http_client, db_session_factory):
    from unittest.mock import AsyncMock, patch

    uid = await _make_user(db_session_factory, "regular@example.com", is_superadmin=False)
    ctx = type("C", (), {"user_id": uid, "email": "regular@example.com"})()
    with patch("app.api.admin_routes._resolve_user_ctx", new=AsyncMock(return_value=ctx)):
        resp = await _http_client.get("/admin")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/home"


@pytest.mark.asyncio
async def test_admin_page_superadmin_allowed(_http_client, db_session_factory):
    from unittest.mock import AsyncMock, patch

    sid = await _make_user(db_session_factory, "admin-user@example.com", is_superadmin=True)
    ctx = type("C", (), {"user_id": sid, "email": "admin-user@example.com"})()
    with (
        patch("app.api.admin_routes._resolve_user_ctx", new=AsyncMock(return_value=ctx)),
        patch(
            "app.api.google_oauth_routes._load_user_view",
            new=AsyncMock(return_value={"id": sid, "email": "admin-user@example.com"}),
        ),
    ):
        resp = await _http_client.get("/admin")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_integrations_page_unauthenticated_redirects_to_homepage(_http_client):
    from unittest.mock import AsyncMock, patch

    with patch("app.api.integrations_routes._resolve_user", new=AsyncMock(return_value=None)):
        resp = await _http_client.get("/admin/oauth-apps")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/"


@pytest.mark.asyncio
async def test_integrations_page_non_admin_redirects_to_homepage(_http_client, db_session_factory):
    from unittest.mock import AsyncMock, patch

    from app.models.user import User

    uid = await _make_user(db_session_factory, "member-only@example.com")
    async with db_session_factory() as db:
        user_obj = await db.get(User, uuid.UUID(uid))
    with patch("app.api.integrations_routes._resolve_user", new=AsyncMock(return_value=user_obj)):
        resp = await _http_client.get("/admin/oauth-apps")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/home"


@pytest.mark.asyncio
async def test_admin_cannot_revoke_last_superadmin(_http_client, db_session_factory):
    from unittest.mock import AsyncMock, patch

    sid = await _make_user(db_session_factory, "solo-super@example.com", is_superadmin=True)
    ctx = type("C", (), {"user_id": sid, "email": "solo-super@example.com"})()
    with patch("app.api.admin_routes._resolve_user_ctx", new=AsyncMock(return_value=ctx)):
        resp = await _http_client.patch(f"/api/admin/users/{sid}/superadmin", json={"is_superadmin": False})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_admin_cannot_deactivate_self(_http_client, db_session_factory):
    from unittest.mock import AsyncMock, patch

    sid = await _make_user(db_session_factory, "self@example.com", is_superadmin=True)
    ctx = type("C", (), {"user_id": sid, "email": "self@example.com"})()
    with patch("app.api.admin_routes._resolve_user_ctx", new=AsyncMock(return_value=ctx)):
        resp = await _http_client.patch(f"/api/admin/users/{sid}/active", json={"is_active": False})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_admin_can_deactivate_other_user(_http_client, db_session_factory):
    from unittest.mock import AsyncMock, patch

    from sqlalchemy import select

    from app.models.user import User

    sid = await _make_user(db_session_factory, "s2@example.com", is_superadmin=True)
    tid = await _make_user(db_session_factory, "victim@example.com", is_superadmin=False)
    ctx = type("C", (), {"user_id": sid, "email": "s2@example.com"})()
    with patch("app.api.admin_routes._resolve_user_ctx", new=AsyncMock(return_value=ctx)):
        resp = await _http_client.patch(f"/api/admin/users/{tid}/active", json={"is_active": False})
    assert resp.status_code == 200
    async with db_session_factory() as db:
        u = (await db.execute(select(User).where(User.id == uuid.UUID(tid)))).scalar_one()
        assert u.is_active is False


@pytest.mark.asyncio
async def test_admin_can_grant_superadmin(_http_client, db_session_factory):
    from unittest.mock import AsyncMock, patch

    from sqlalchemy import select

    from app.models.user import User

    sid = await _make_user(db_session_factory, "granter@example.com", is_superadmin=True)
    tid = await _make_user(db_session_factory, "promote@example.com", is_superadmin=False)
    ctx = type("C", (), {"user_id": sid, "email": "granter@example.com"})()
    with patch("app.api.admin_routes._resolve_user_ctx", new=AsyncMock(return_value=ctx)):
        resp = await _http_client.patch(f"/api/admin/users/{tid}/superadmin", json={"is_superadmin": True})
    assert resp.status_code == 200
    async with db_session_factory() as db:
        u = (await db.execute(select(User).where(User.id == uuid.UUID(tid)))).scalar_one()
        assert u.is_superadmin is True


@pytest.mark.asyncio
async def test_request_access_creates_pending(_http_client, db_session_factory):
    from sqlalchemy import select

    from app.models.access_request import AccessRequest

    resp = await _http_client.post(
        "/request-access", json={"name": "Jane", "email": "newbie@example.com", "use_case": "kicking tires"}
    )
    assert resp.status_code == 200, resp.text
    async with db_session_factory() as db:
        r = (
            await db.execute(select(AccessRequest).where(AccessRequest.email == "newbie@example.com"))
        ).scalar_one()
        assert r.status == "pending"


@pytest.mark.asyncio
async def test_request_access_dedupes_existing_user(_http_client, db_session_factory):
    await _make_user(db_session_factory, "exists@example.com")
    resp = await _http_client.post("/request-access", json={"name": "X", "email": "exists@example.com"})
    assert resp.status_code == 400
    assert "sign in" in resp.json().get("error", "").lower()


@pytest.mark.asyncio
async def test_request_access_dedupes_pending(_http_client, db_session_factory):
    await _http_client.post("/request-access", json={"name": "A", "email": "dup@example.com"})
    resp = await _http_client.post("/request-access", json={"name": "A", "email": "dup@example.com"})
    assert resp.status_code == 400
    assert "pending" in resp.json().get("error", "").lower()


async def _set_flag(db_session_factory, key: str, value: bool) -> None:
    from app.settings_service import set_setting

    async with db_session_factory() as db:
        await set_setting(db, key=key, value=value, is_secret=False, updated_by_user_id=None)
        await db.commit()


@pytest.mark.asyncio
async def test_register_blocked_when_gate_on(_http_client, db_session_factory):
    # Password sign-up is off by default (Google-only); enable it so the gate is what blocks.
    await _set_flag(db_session_factory, "auth_password_enabled", True)
    await _set_flag(db_session_factory, "require_access_approval", True)
    try:
        resp = await _http_client.post(
            "/auth/register",
            json={"email": "blocked@example.com", "password": "password123", "display_name": "B"},
        )
        assert resp.status_code == 403
        assert "request access" in resp.json().get("error", "").lower()
    finally:
        await _set_flag(db_session_factory, "require_access_approval", False)
        await _set_flag(db_session_factory, "auth_password_enabled", False)


@pytest.mark.asyncio
async def test_register_open_when_gate_off(_http_client, db_session_factory):
    await _set_flag(db_session_factory, "auth_password_enabled", True)
    try:
        resp = await _http_client.post(
            "/auth/register",
            json={"email": "open@example.com", "password": "password123", "display_name": "O"},
        )
        assert resp.status_code in (200, 201)
    finally:
        await _set_flag(db_session_factory, "auth_password_enabled", False)


@pytest.mark.asyncio
async def test_register_refused_when_password_auth_disabled(_http_client, db_session_factory):
    await _set_flag(db_session_factory, "auth_password_enabled", False)
    resp = await _http_client.post(
        "/auth/register",
        json={"email": "nopw@example.com", "password": "password123", "display_name": "N"},
    )
    assert resp.status_code == 403
    assert "google" in resp.json().get("error", "").lower()


@pytest.mark.asyncio
async def test_approve_provisions_user_with_temp_password(_http_client, db_session_factory):
    from unittest.mock import AsyncMock, patch

    from sqlalchemy import select

    from app.auth.email_auth import authenticate_user
    from app.models.access_request import AccessRequest

    sid = await _make_user(db_session_factory, "approver@example.com", is_superadmin=True)
    async with db_session_factory() as db:
        r = AccessRequest(name="Newbie", email="newbie2@example.com", use_case="x")
        db.add(r)
        await db.flush()
        rid = str(r.id)
        await db.commit()

    ctx = type("C", (), {"user_id": sid, "email": "approver@example.com"})()
    with patch("app.api.admin_routes._resolve_user_ctx", new=AsyncMock(return_value=ctx)):
        resp = await _http_client.post(f"/api/admin/access-requests/{rid}/approve")
    assert resp.status_code == 200, resp.text
    pw = resp.json()["temp_password"]
    assert pw
    user, err = await authenticate_user("newbie2@example.com", pw)
    assert err is None and user is not None

    async with db_session_factory() as db:
        r = (
            await db.execute(select(AccessRequest).where(AccessRequest.email == "newbie2@example.com"))
        ).scalar_one()
        assert r.status == "approved"


@pytest.mark.asyncio
async def test_approve_existing_password_account_not_reset(_http_client, db_session_factory):
    """If the email already has a password account, approval must NOT reset it."""
    from unittest.mock import AsyncMock, patch

    from app.auth.email_auth import authenticate_user, hash_password
    from app.models.access_request import AccessRequest
    from app.models.user import User

    sid = await _make_user(db_session_factory, "appr2@example.com", is_superadmin=True)
    async with db_session_factory() as db:
        db.add(
            User(
                email="hasacct@example.com",
                password_hash=hash_password("origpass1!"),
                email_verified=True,
                auth_provider="email",
            )
        )
        r = AccessRequest(name="Has Acct", email="hasacct@example.com")
        db.add(r)
        await db.flush()
        rid = str(r.id)
        await db.commit()

    ctx = type("C", (), {"user_id": sid, "email": "appr2@example.com"})()
    with patch("app.api.admin_routes._resolve_user_ctx", new=AsyncMock(return_value=ctx)):
        resp = await _http_client.post(f"/api/admin/access-requests/{rid}/approve")
    assert resp.status_code == 200, resp.text
    assert resp.json().get("temp_password") is None
    # original password still works (not reset)
    user, err = await authenticate_user("hasacct@example.com", "origpass1!")
    assert err is None and user is not None


@pytest.mark.asyncio
async def test_reject_creates_no_account(_http_client, db_session_factory):
    from unittest.mock import AsyncMock, patch

    from sqlalchemy import select

    from app.models.access_request import AccessRequest
    from app.models.user import User

    sid = await _make_user(db_session_factory, "appr3@example.com", is_superadmin=True)
    async with db_session_factory() as db:
        r = AccessRequest(name="Nope", email="nope@example.com")
        db.add(r)
        await db.flush()
        rid = str(r.id)
        await db.commit()

    ctx = type("C", (), {"user_id": sid, "email": "appr3@example.com"})()
    with patch("app.api.admin_routes._resolve_user_ctx", new=AsyncMock(return_value=ctx)):
        resp = await _http_client.post(f"/api/admin/access-requests/{rid}/reject")
    assert resp.status_code == 200
    async with db_session_factory() as db:
        assert (
            await db.execute(select(User).where(User.email == "nope@example.com"))
        ).scalar_one_or_none() is None
        r = (
            await db.execute(select(AccessRequest).where(AccessRequest.email == "nope@example.com"))
        ).scalar_one()
        assert r.status == "rejected"


@pytest.mark.asyncio
async def test_toggle_gate(_http_client, db_session_factory):
    from unittest.mock import AsyncMock, patch

    from app.settings_service import access_approval_required

    sid = await _make_user(db_session_factory, "toggler@example.com", is_superadmin=True)
    ctx = type("C", (), {"user_id": sid, "email": "toggler@example.com"})()
    with patch("app.api.admin_routes._resolve_user_ctx", new=AsyncMock(return_value=ctx)):
        resp = await _http_client.patch("/api/admin/settings/require-access-approval", json={"enabled": True})
    assert resp.status_code == 200
    try:
        assert await access_approval_required() is True
    finally:
        with patch("app.api.admin_routes._resolve_user_ctx", new=AsyncMock(return_value=ctx)):
            await _http_client.patch("/api/admin/settings/require-access-approval", json={"enabled": False})


async def _make_project(db_session_factory, owner_id, *, name="Proj", slug=None):
    from app.models.project import Project

    async with db_session_factory() as db:
        p = Project(name=name, slug=slug or f"proj-{uuid.uuid4().hex[:8]}", owner_id=uuid.UUID(owner_id))
        db.add(p)
        await db.flush()
        pid = str(p.id)
        await db.commit()
        return pid


async def _add_audit_row(db_session_factory, user_id, *, project_id=None, tool_name="analytics_read"):
    from app.models.audit import ToolCallAudit

    async with db_session_factory() as db:
        row = ToolCallAudit(
            user_id=uuid.UUID(user_id),
            project_id=uuid.UUID(project_id) if project_id else None,
            tool_name=tool_name,
            status="success",
            is_write=False,
            duration_ms=12,
        )
        db.add(row)
        await db.commit()


@pytest.mark.asyncio
async def test_activity_log_not_hidden_by_active_project_cookie(_http_client, db_session_factory):
    """Regression: MCP tool calls are recorded against the AI client's active
    project (or NULL), which may differ from the web active_project_id cookie.
    The activity log must NOT silently filter by that cookie, or the user sees
    nothing despite making many tool calls."""
    from unittest.mock import AsyncMock, patch

    uid = await _make_user(db_session_factory, "operator@example.com")
    other_project = await _make_project(db_session_factory, uid)  # what the AI client logged against
    from app.models.project import ProjectMember

    async with db_session_factory() as db:
        db.add(ProjectMember(project_id=uuid.UUID(other_project), user_id=uuid.UUID(uid), role="owner"))
        await db.commit()
    await _add_audit_row(db_session_factory, uid, project_id=other_project)
    await _add_audit_row(db_session_factory, uid, project_id=None, tool_name="run_audit")

    from app.auth.uid_cookie import sign_uid

    ctx = type("C", (), {"user_id": uid, "email": "operator@example.com"})()
    # Browser has a DIFFERENT project selected — must not hide the rows above.
    cookie_project = str(uuid.uuid4())
    with patch("app.api.audit_routes._resolve_user_ctx", new=AsyncMock(return_value=ctx)):
        resp = await _http_client.get(
            "/api/activity-log",
            headers={"Cookie": f"uid={sign_uid(uid)}; active_project_id={cookie_project}"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["count"] == 2

    # Explicit ?project_id= filter still works for a project the user belongs to
    # (the request middleware only honours project ids of the signed-in user).
    with patch("app.api.audit_routes._resolve_user_ctx", new=AsyncMock(return_value=ctx)):
        resp = await _http_client.get(
            f"/api/activity-log?project_id={other_project}", headers={"Cookie": f"uid={sign_uid(uid)}"}
        )
    assert resp.json()["count"] == 1


@pytest.mark.asyncio
async def test_old_integrations_url_redirects_to_admin(_http_client):
    resp = await _http_client.get("/settings/integrations?welcome=1")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/admin/oauth-apps?welcome=1"


@pytest.mark.asyncio
async def test_project_owner_cannot_edit_oauth_apps_when_superadmin_exists(_http_client, db_session_factory):
    """Every signup owns a workspace — owning a project must not unlock instance OAuth credentials."""
    from unittest.mock import AsyncMock, patch

    from app.models.project import Project, ProjectMember
    from app.models.user import User

    await _make_user(db_session_factory, "the-super@example.com", is_superadmin=True)
    oid = await _make_user(db_session_factory, "workspace-owner@example.com")
    async with db_session_factory() as db:
        proj = Project(name="Owner WS", slug=f"owner-ws-{uuid.uuid4().hex[:6]}", owner_id=uuid.UUID(oid))
        db.add(proj)
        await db.flush()
        db.add(ProjectMember(project_id=proj.id, user_id=uuid.UUID(oid), role="owner"))
        await db.commit()
        owner = await db.get(User, uuid.UUID(oid))

    with patch("app.api.integrations_routes._resolve_user", new=AsyncMock(return_value=owner)):
        page = await _http_client.get("/admin/oauth-apps")
        api = await _http_client.post(
            "/api/integrations/google", json={"client_id": "evil-client-id", "client_secret": "evil-secret"}
        )
    assert page.status_code == 302
    assert page.headers["location"] == "/home"
    assert api.status_code == 403
