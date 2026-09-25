# tests/test_rbac_models.py
"""Persistence tests for RBAC role models."""

import pytest

import app.app_state as app_state
import app.models  # noqa: F401  (ensures model metadata loaded)


@pytest.fixture
def _patch_db(db_session_factory):
    original = app_state.db_session_factory
    app_state.db_session_factory = db_session_factory
    yield
    app_state.db_session_factory = original


@pytest.mark.asyncio
async def test_role_persists_with_permissions(_patch_db, db_session_factory):
    from sqlalchemy import select

    from app.models.project import Project
    from app.models.role import Role
    from app.models.user import User

    async with db_session_factory() as db:
        u = User(email="admin@example.com")
        db.add(u)
        await db.flush()
        p = Project(name="BMK", slug="bmk", owner_id=u.id)
        db.add(p)
        await db.flush()
        role = Role(
            project_id=p.id,
            name="GTM Implementer",
            description="Tag work",
            permissions={"tools": {"tagmanager": ["read", "write"]}, "providers": ["ga4", "gtm"]},
            created_by=u.id,
        )
        db.add(role)
        await db.flush()
        rid = role.id
        await db.commit()

    async with db_session_factory() as db:
        r = (await db.execute(select(Role).where(Role.id == rid))).scalar_one()
        assert r.name == "GTM Implementer"
        assert r.permissions["providers"] == ["ga4", "gtm"]
        assert r.is_active is True


@pytest.mark.asyncio
async def test_member_role_assignment_persists(_patch_db, db_session_factory):
    from sqlalchemy import select

    from app.models.project import Project, ProjectMember
    from app.models.role import MemberRole, Role
    from app.models.user import User

    async with db_session_factory() as db:
        u = User(email="m@example.com")
        db.add(u)
        await db.flush()
        p = Project(name="P", slug="p2", owner_id=u.id)
        db.add(p)
        await db.flush()
        pm = ProjectMember(project_id=p.id, user_id=u.id, role="member")
        db.add(pm)
        await db.flush()
        role = Role(
            project_id=p.id, name="Viewer", permissions={"tools": {"analytics": ["read"]}}, created_by=u.id
        )
        db.add(role)
        await db.flush()
        mr = MemberRole(project_member_id=pm.id, role_id=role.id, assigned_by=u.id)
        db.add(mr)
        await db.flush()
        mrid = mr.id
        role_id_val = role.id
        pm_id_val = pm.id
        await db.commit()

    async with db_session_factory() as db:
        mr = (await db.execute(select(MemberRole).where(MemberRole.id == mrid))).scalar_one()
        assert mr.role_id == role_id_val
        assert mr.project_member_id == pm_id_val


@pytest.mark.asyncio
async def test_project_rbac_enabled_defaults_false(_patch_db, db_session_factory):
    from sqlalchemy import select

    from app.models.project import Project
    from app.models.user import User

    async with db_session_factory() as db:
        u = User(email="o@example.com")
        db.add(u)
        await db.flush()
        p = Project(name="P", slug="p3", owner_id=u.id)
        db.add(p)
        await db.flush()
        pid = p.id
        await db.commit()
    async with db_session_factory() as db:
        p = (await db.execute(select(Project).where(Project.id == pid))).scalar_one()
        assert p.rbac_enabled is False


def test_can_manage_roles_set():
    from app.models.project import CAN_MANAGE_ROLES, ROLE_ADMIN, ROLE_MEMBER, ROLE_OWNER

    assert {ROLE_OWNER, ROLE_ADMIN} == CAN_MANAGE_ROLES
    assert ROLE_MEMBER not in CAN_MANAGE_ROLES
