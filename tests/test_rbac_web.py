# tests/test_rbac_web.py
"""Web-layer RBAC tests: user-view permission injection + page guards."""

import pytest

import app.app_state as app_state


@pytest.fixture
def _patch_db(db_session_factory):
    original = app_state.db_session_factory
    app_state.db_session_factory = db_session_factory
    yield
    app_state.db_session_factory = original


@pytest.mark.asyncio
async def test_user_view_includes_permissions_block(_patch_db, db_session_factory, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import app.auth.permissions as perms
    from app.api.google_oauth_routes import _load_user_view
    from app.models.project import Project, ProjectMember
    from app.models.user import User

    monkeypatch.setattr(perms, "_cache_get", AsyncMock(return_value=None))
    monkeypatch.setattr(perms, "_cache_set", AsyncMock(return_value=None))

    async with db_session_factory() as db:
        u = User(email="member@example.com")
        db.add(u)
        await db.flush()
        p = Project(name="P", slug="wp1", owner_id=u.id, rbac_enabled=True)
        db.add(p)
        await db.flush()
        db.add(ProjectMember(project_id=p.id, user_id=u.id, role="member"))
        await db.flush()
        uid, pid = str(u.id), str(p.id)
        await db.commit()

    view = await _load_user_view(SimpleNamespace(user_id=uid, email="member@example.com"), project_id=pid)
    assert "permissions" in view
    assert view["permissions"]["full"] is False
    assert view["permissions"]["tools"] == {}


@pytest.mark.asyncio
async def test_user_view_permissions_full_when_no_project(_patch_db, db_session_factory):
    from types import SimpleNamespace

    from app.api.google_oauth_routes import _load_user_view
    from app.models.user import User

    async with db_session_factory() as db:
        u = User(email="x@example.com")
        db.add(u)
        await db.flush()
        uid = str(u.id)
        await db.commit()

    view = await _load_user_view(SimpleNamespace(user_id=uid, email="x@example.com"))
    assert view.get("permissions", {"full": True})["full"] is True


@pytest.mark.asyncio
async def test_require_permission_raises_403_when_denied(monkeypatch):
    from fastapi import HTTPException

    import app.auth.permissions as perms
    from app.auth.web_guards import require_domain_permission

    async def fake_resolve(uid, pid):
        return perms.EffectivePermissions(full=False, tools={"tagmanager": {"read"}})

    monkeypatch.setattr(perms, "resolve_effective_permissions", fake_resolve)

    await require_domain_permission("u1", "p1", "tagmanager", "read")  # allowed, no raise
    with pytest.raises(HTTPException) as ei:
        await require_domain_permission("u1", "p1", "analytics", "read")
    assert ei.value.status_code == 403
