# tests/test_invite_and_projects.py
"""DB + endpoint tests for invite temp-password, register-claim, default project, reset."""

import pytest

import app.app_state as app_state


@pytest.fixture
def _patch_db(db_session_factory):
    original = app_state.db_session_factory
    app_state.db_session_factory = db_session_factory
    yield
    app_state.db_session_factory = original


@pytest.mark.asyncio
async def test_register_does_not_claim_passwordless_account(_patch_db, db_session_factory):
    """A password-less existing account (Google user or invited stub) must NOT be
    silently claimed by registration — that would be an account-takeover vector."""
    from app.auth.email_auth import authenticate_user, register_user
    from app.models.user import User

    # Seed a password-less row (auth_provider defaults to "google" via server_default).
    async with db_session_factory() as db:
        placeholder = User(email="invitee@example.com")
        db.add(placeholder)
        await db.commit()

    user, error = await register_user("invitee@example.com", "Sup3rSecret!", "Invitee")
    assert user is None
    assert error is not None  # rejected, not claimed

    # The password was NOT set on the existing account.
    authed, auth_err = await authenticate_user("invitee@example.com", "Sup3rSecret!")
    assert authed is None


@pytest.mark.asyncio
async def test_register_rejects_existing_password_account(_patch_db, db_session_factory):
    """If a real (password-set) account exists, registration still errors."""
    from app.auth.email_auth import register_user

    await register_user("taken@example.com", "FirstPass1!", "First")
    user, error = await register_user("taken@example.com", "SecondPass1!", "Second")
    assert user is None
    assert error and "already exists" in error.lower()


@pytest.mark.asyncio
async def test_ensure_default_project_creates_when_none(_patch_db, db_session_factory):
    from sqlalchemy import select

    from app.api.project_routes import ensure_default_project
    from app.models.project import ProjectMember
    from app.models.user import User

    async with db_session_factory() as db:
        u = User(email="solo@example.com", display_name="Solo")
        db.add(u)
        await db.flush()
        uid = u.id
        await db.commit()

    created = await ensure_default_project(uid, "Solo", "solo@example.com")
    assert created is True

    async with db_session_factory() as db:
        memberships = (
            (await db.execute(select(ProjectMember).where(ProjectMember.user_id == uid))).scalars().all()
        )
    assert len(memberships) == 1
    assert memberships[0].role == "owner"


@pytest.mark.asyncio
async def test_ensure_default_project_noop_when_member(_patch_db, db_session_factory):
    from app.api.project_routes import ensure_default_project
    from app.models.project import Project, ProjectMember
    from app.models.user import User

    async with db_session_factory() as db:
        u = User(email="hasproj@example.com")
        db.add(u)
        await db.flush()
        p = Project(name="X", slug="x-existing", owner_id=u.id)
        db.add(p)
        await db.flush()
        db.add(ProjectMember(project_id=p.id, user_id=u.id, role="owner"))
        uid = u.id
        await db.commit()

    created = await ensure_default_project(uid, None, "hasproj@example.com")
    assert created is False


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


async def _seed_owner_and_project(db_session_factory, slug="acme"):
    from app.models.project import Project, ProjectMember
    from app.models.user import User

    async with db_session_factory() as db:
        owner = User(email="owner@example.com", display_name="Owner")
        db.add(owner)
        await db.flush()
        proj = Project(name="Acme", slug=slug, owner_id=owner.id)
        db.add(proj)
        await db.flush()
        db.add(ProjectMember(project_id=proj.id, user_id=owner.id, role="owner"))
        await db.commit()
        return str(owner.id), str(owner.email), slug


@pytest.mark.asyncio
async def test_invite_for_new_email_creates_no_account_and_returns_a_link(_http_client, db_session_factory):
    """Invites are pending: no account (and no temporary password) until the person accepts."""
    from unittest.mock import AsyncMock, patch

    from sqlalchemy import select

    from app.models.user import User

    owner_id, owner_email, slug = await _seed_owner_and_project(db_session_factory)
    owner_ctx = {"user_id": owner_id, "email": owner_email}

    with patch(
        "app.api.project_routes._resolve_user",
        new=AsyncMock(return_value=owner_ctx),
    ):
        resp = await _http_client.post(
            f"/api/project/{slug}/members",
            json={"email": "newhire@example.com", "role": "member"},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "temp_password" not in data
    assert "/invite/" in data["invite_url"]

    async with db_session_factory() as db:
        assert (await db.execute(select(User).where(User.email == "newhire@example.com"))).first() is None


@pytest.mark.asyncio
async def test_invite_existing_password_account_not_reset(_http_client, db_session_factory):
    """Inviting an existing real account must NOT reset its password or return one."""
    from unittest.mock import AsyncMock, patch

    from app.auth.email_auth import authenticate_user, hash_password
    from app.models.user import User

    owner_id, owner_email, slug = await _seed_owner_and_project(db_session_factory, slug="acme2")
    async with db_session_factory() as db:
        db.add(
            User(
                email="real@example.com",
                password_hash=hash_password("orig-pass-1!"),
                email_verified=True,
                auth_provider="email",
            )
        )
        await db.commit()

    owner_ctx = {"user_id": owner_id, "email": owner_email}
    with patch("app.api.project_routes._resolve_user", new=AsyncMock(return_value=owner_ctx)):
        resp = await _http_client.post(
            f"/api/project/{slug}/members", json={"email": "real@example.com", "role": "member"}
        )
    assert resp.status_code == 200, resp.text
    assert resp.json().get("temp_password") is None
    # Original password still works (not reset).
    user, err = await authenticate_user("real@example.com", "orig-pass-1!")
    assert err is None and user is not None


@pytest.mark.asyncio
async def test_invite_existing_google_user_not_hijacked(_http_client, db_session_factory):
    """Inviting an existing Google-only user must NOT set a password or return one."""
    from unittest.mock import AsyncMock, patch

    from sqlalchemy import select

    from app.models.user import User

    owner_id, owner_email, slug = await _seed_owner_and_project(db_session_factory, slug="acme3")
    async with db_session_factory() as db:
        g = User(email="googler@example.com", auth_provider="google", email_verified=True)
        db.add(g)
        await db.flush()
        gid = g.id
        await db.commit()

    owner_ctx = {"user_id": owner_id, "email": owner_email}
    with patch("app.api.project_routes._resolve_user", new=AsyncMock(return_value=owner_ctx)):
        resp = await _http_client.post(
            f"/api/project/{slug}/members", json={"email": "googler@example.com", "role": "member"}
        )
    assert resp.status_code == 200, resp.text
    assert resp.json().get("temp_password") is None
    # Credentials untouched: still no password, still google.
    async with db_session_factory() as db:
        row = (await db.execute(select(User).where(User.id == gid))).scalar_one()
        assert row.password_hash is None
        assert row.auth_provider == "google"


@pytest.mark.asyncio
async def test_reset_password_owner_can_reissue_member_credentials(_http_client, db_session_factory):
    from unittest.mock import AsyncMock, patch

    from app.auth.email_auth import authenticate_user, hash_password
    from app.models.project import Project, ProjectMember
    from app.models.user import User

    async with db_session_factory() as db:
        owner = User(email="o2@example.com", display_name="O2")
        db.add(owner)
        await db.flush()
        proj = Project(name="Beta", slug="beta", owner_id=owner.id)
        db.add(proj)
        await db.flush()
        db.add(ProjectMember(project_id=proj.id, user_id=owner.id, role="owner"))
        member = User(
            email="m2@example.com",
            password_hash=hash_password("oldpass1!"),
            email_verified=True,
            auth_provider="email",
        )
        db.add(member)
        await db.flush()
        member_pm = ProjectMember(project_id=proj.id, user_id=member.id, role="member")
        db.add(member_pm)
        await db.commit()
        owner_ctx = {"user_id": str(owner.id), "email": owner.email}
        member_pm_id = str(member_pm.id)

    with patch(
        "app.api.project_routes._resolve_user",
        new=AsyncMock(return_value=owner_ctx),
    ):
        resp = await _http_client.post(f"/api/project/beta/members/{member_pm_id}/reset-password")
    assert resp.status_code == 200, resp.text
    new_pw = resp.json()["temp_password"]
    assert new_pw

    user, err = await authenticate_user("m2@example.com", new_pw)
    assert err is None and user is not None


@pytest.mark.asyncio
async def test_reset_password_refuses_owner_target(_http_client, db_session_factory):
    from unittest.mock import AsyncMock, patch

    from app.models.project import Project, ProjectMember
    from app.models.user import User

    async with db_session_factory() as db:
        owner = User(email="o3@example.com")
        db.add(owner)
        await db.flush()
        proj = Project(name="Gamma", slug="gamma", owner_id=owner.id)
        db.add(proj)
        await db.flush()
        owner_pm = ProjectMember(project_id=proj.id, user_id=owner.id, role="owner")
        db.add(owner_pm)
        await db.commit()
        owner_ctx = {"user_id": str(owner.id), "email": owner.email}
        owner_pm_id = str(owner_pm.id)

    with patch(
        "app.api.project_routes._resolve_user",
        new=AsyncMock(return_value=owner_ctx),
    ):
        resp = await _http_client.post(f"/api/project/gamma/members/{owner_pm_id}/reset-password")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_reset_password_refuses_google_only_member(_http_client, db_session_factory):
    """A password-less (Google) member cannot be given a password by an admin."""
    from unittest.mock import AsyncMock, patch

    from sqlalchemy import select

    from app.models.project import Project, ProjectMember
    from app.models.user import User

    async with db_session_factory() as db:
        owner = User(email="o4@example.com")
        db.add(owner)
        await db.flush()
        proj = Project(name="Delta", slug="delta", owner_id=owner.id)
        db.add(proj)
        await db.flush()
        db.add(ProjectMember(project_id=proj.id, user_id=owner.id, role="owner"))
        g = User(email="g4@example.com", auth_provider="google", email_verified=True)
        db.add(g)
        await db.flush()
        gid = g.id
        g_pm = ProjectMember(project_id=proj.id, user_id=g.id, role="member")
        db.add(g_pm)
        await db.commit()
        owner_ctx = {"user_id": str(owner.id), "email": owner.email}
        g_pm_id = str(g_pm.id)

    with patch(
        "app.api.project_routes._resolve_user",
        new=AsyncMock(return_value=owner_ctx),
    ):
        resp = await _http_client.post(f"/api/project/delta/members/{g_pm_id}/reset-password")
    assert resp.status_code == 400
    # Still has no password.
    async with db_session_factory() as db:
        row = (await db.execute(select(User).where(User.id == gid))).scalar_one()
        assert row.password_hash is None
