# tests/test_rbac_routes.py
"""Route-level tests for RBAC endpoints (Tasks 12, 13, 14).

Mirrors the harness in test_invite_and_projects.py:
- _patch_db swaps app_state.db_session_factory
- _http_client builds an ASGI test client with a CSRF cookie/header
- _resolve_user is patched with AsyncMock so we don't need real sessions
"""

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
    ) as client:
        yield client


async def _seed_project_with_users(db_session_factory, slug="rbac-proj"):
    """Create an owner, an admin, a regular member and a project. Returns IDs."""
    from app.models.project import Project, ProjectMember
    from app.models.user import User

    async with db_session_factory() as db:
        owner = User(email=f"owner-{slug}@example.com", display_name="Owner")
        admin = User(email=f"admin-{slug}@example.com", display_name="Admin")
        member = User(email=f"member-{slug}@example.com", display_name="Member")
        db.add_all([owner, admin, member])
        await db.flush()

        proj = Project(name="RBAC Proj", slug=slug, owner_id=owner.id)
        db.add(proj)
        await db.flush()

        pm_owner = ProjectMember(project_id=proj.id, user_id=owner.id, role="owner")
        pm_admin = ProjectMember(project_id=proj.id, user_id=admin.id, role="admin")
        pm_member = ProjectMember(project_id=proj.id, user_id=member.id, role="member")
        db.add_all([pm_owner, pm_admin, pm_member])
        await db.commit()

        return {
            "slug": slug,
            "project_id": str(proj.id),
            "owner_id": str(owner.id),
            "owner_email": owner.email,
            "admin_id": str(admin.id),
            "admin_email": admin.email,
            "member_id": str(member.id),
            "member_email": member.email,
            "pm_owner_id": str(pm_owner.id),
            "pm_admin_id": str(pm_admin.id),
            "pm_member_id": str(pm_member.id),
        }


# ---------------------------------------------------------------------------
# Task 12 — Role CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_can_create_role_and_write_implies_read(_http_client, db_session_factory):
    """Admin creates a role; write implies read for tagmanager → ["read","write"] returned."""
    from unittest.mock import AsyncMock, patch

    seed = await _seed_project_with_users(db_session_factory, slug="rc-01")
    admin_ctx = {"user_id": seed["admin_id"], "email": seed["admin_email"]}

    with patch("app.api.project_routes._resolve_user", new=AsyncMock(return_value=admin_ctx)):
        resp = await _http_client.post(
            f"/api/project/{seed['slug']}/roles",
            json={
                "name": "GTM Implementer",
                "description": "Tag work",
                "permissions": {"tools": {"tagmanager": ["write"]}},
            },
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "GTM Implementer"
    # write implies read
    assert sorted(data["permissions"]["tools"]["tagmanager"]) == ["read", "write"]
    assert "id" in data


@pytest.mark.asyncio
async def test_create_role_unknown_domain_returns_400(_http_client, db_session_factory):
    """Creating a role with an unknown domain returns 400."""
    from unittest.mock import AsyncMock, patch

    seed = await _seed_project_with_users(db_session_factory, slug="rc-02")
    admin_ctx = {"user_id": seed["admin_id"], "email": seed["admin_email"]}

    with patch("app.api.project_routes._resolve_user", new=AsyncMock(return_value=admin_ctx)):
        resp = await _http_client.post(
            f"/api/project/{seed['slug']}/roles",
            json={
                "name": "Bad Role",
                "permissions": {"tools": {"nonexistent_domain": ["read"]}},
            },
        )
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_member_cannot_create_role(_http_client, db_session_factory):
    """A regular member gets 403 when attempting to create a role."""
    from unittest.mock import AsyncMock, patch

    seed = await _seed_project_with_users(db_session_factory, slug="rc-03")
    member_ctx = {"user_id": seed["member_id"], "email": seed["member_email"]}

    with patch("app.api.project_routes._resolve_user", new=AsyncMock(return_value=member_ctx)):
        resp = await _http_client.post(
            f"/api/project/{seed['slug']}/roles",
            json={"name": "Sneaky Role", "permissions": {}},
        )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_owner_can_list_roles(_http_client, db_session_factory):
    """Owner can GET /roles and see the roles that exist."""
    from unittest.mock import AsyncMock, patch

    from app.models.role import Role

    seed = await _seed_project_with_users(db_session_factory, slug="rc-04")

    # Pre-seed a role directly in DB
    async with db_session_factory() as db:
        import uuid

        role = Role(
            project_id=uuid.UUID(seed["project_id"]),
            name="Seeded Role",
            permissions={"tools": {"analytics": ["read"]}},
            created_by=uuid.UUID(seed["owner_id"]),
        )
        db.add(role)
        await db.commit()

    owner_ctx = {"user_id": seed["owner_id"], "email": seed["owner_email"]}
    with patch("app.api.project_routes._resolve_user", new=AsyncMock(return_value=owner_ctx)):
        resp = await _http_client.get(f"/api/project/{seed['slug']}/roles")
    assert resp.status_code == 200, resp.text
    roles = resp.json()
    assert isinstance(roles, list)
    names = [r["name"] for r in roles]
    assert "Seeded Role" in names


# ---------------------------------------------------------------------------
# Task 13 — Member role assignment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assign_roles_to_member_persists_and_returns_ids(_http_client, db_session_factory):
    """PUT member roles: assignment persists and response lists role ids."""
    from unittest.mock import AsyncMock, patch

    from app.models.role import Role

    seed = await _seed_project_with_users(db_session_factory, slug="ma-01")

    # Pre-seed two roles
    async with db_session_factory() as db:
        import uuid

        r1 = Role(
            project_id=uuid.UUID(seed["project_id"]),
            name="Role A",
            permissions={},
            created_by=uuid.UUID(seed["owner_id"]),
        )
        r2 = Role(
            project_id=uuid.UUID(seed["project_id"]),
            name="Role B",
            permissions={},
            created_by=uuid.UUID(seed["owner_id"]),
        )
        db.add_all([r1, r2])
        await db.commit()
        r1_id = str(r1.id)
        r2_id = str(r2.id)

    admin_ctx = {"user_id": seed["admin_id"], "email": seed["admin_email"]}
    with patch("app.api.project_routes._resolve_user", new=AsyncMock(return_value=admin_ctx)):
        resp = await _http_client.put(
            f"/api/project/{seed['slug']}/members/{seed['pm_member_id']}/roles",
            json={"role_ids": [r1_id, r2_id]},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert set(data["role_ids"]) == {r1_id, r2_id}

    # Verify persistence
    import uuid as _uuid

    from sqlalchemy import select

    from app.models.role import MemberRole

    async with db_session_factory() as db:
        rows = (
            (
                await db.execute(
                    select(MemberRole).where(MemberRole.project_member_id == _uuid.UUID(seed["pm_member_id"]))
                )
            )
            .scalars()
            .all()
        )
    assigned = {str(r.role_id) for r in rows}
    assert assigned == {r1_id, r2_id}


@pytest.mark.asyncio
async def test_assign_invalid_role_id_returns_400(_http_client, db_session_factory):
    """Assigning a role_id that doesn't belong to the project returns 400."""
    import uuid
    from unittest.mock import AsyncMock, patch

    seed = await _seed_project_with_users(db_session_factory, slug="ma-02")
    admin_ctx = {"user_id": seed["admin_id"], "email": seed["admin_email"]}

    with patch("app.api.project_routes._resolve_user", new=AsyncMock(return_value=admin_ctx)):
        resp = await _http_client.put(
            f"/api/project/{seed['slug']}/members/{seed['pm_member_id']}/roles",
            json={"role_ids": [str(uuid.uuid4())]},  # nonexistent
        )
    assert resp.status_code == 400, resp.text


# ---------------------------------------------------------------------------
# Task 14 — RBAC toggle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_toggle_rbac_returns_new_value(_http_client, db_session_factory):
    """PUT /settings/rbac toggles rbac_enabled and returns it."""
    import uuid as _uuid
    from unittest.mock import AsyncMock, patch

    from sqlalchemy import select

    from app.models.project import Project

    seed = await _seed_project_with_users(db_session_factory, slug="rt-01")
    owner_ctx = {"user_id": seed["owner_id"], "email": seed["owner_email"]}

    with patch("app.api.project_routes._resolve_user", new=AsyncMock(return_value=owner_ctx)):
        resp = await _http_client.put(
            f"/api/project/{seed['slug']}/settings/rbac",
            json={"enabled": True},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["rbac_enabled"] is True

    # Verify DB
    async with db_session_factory() as db:
        proj = (
            await db.execute(select(Project).where(Project.id == _uuid.UUID(seed["project_id"])))
        ).scalar_one()
    assert proj.rbac_enabled is True
