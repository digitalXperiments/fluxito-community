"""Project trust: client-supplied project ids must name the caller's own projects.

``active_project_id`` (cookie) and ``?project_id=`` are plain client input.
The request middleware drops either one unless the signed-in user is an active
member of that project, and the project switcher only ever redirects to a
same-site path in the newly selected project.
"""

from __future__ import annotations

import uuid

import pytest

import app.app_state as app_state


@pytest.fixture
def _patch_db(db_session_factory):
    original = app_state.db_session_factory
    app_state.db_session_factory = db_session_factory
    yield
    app_state.db_session_factory = original


async def _user_with_project(db_session_factory):
    from app.models.project import Project, ProjectMember
    from app.models.user import User

    uid, pid = uuid.uuid4(), uuid.uuid4()
    async with db_session_factory() as db:
        db.add(User(id=uid, email=f"u-{uid.hex[:8]}@example.com", auth_provider="email"))
        await db.flush()
        db.add(Project(id=pid, name="P", slug=f"p-{pid.hex[:10]}", owner_id=uid))
        await db.flush()
        db.add(ProjectMember(project_id=pid, user_id=uid, role="owner"))
        await db.commit()
    return str(uid), str(pid)


def _scope(cookies: dict[str, str], query: str = "") -> dict:
    cookie = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return {
        "type": "http",
        "path": "/tracking-plan",
        "headers": [(b"host", b"testserver"), (b"cookie", cookie.encode())],
        "query_string": query.encode(),
    }


def _cookies_of(scope) -> dict[str, str]:
    from starlette.requests import Request

    return dict(Request(scope).cookies)


@pytest.mark.asyncio
async def test_foreign_project_cookie_and_query_are_dropped(_patch_db, db_session_factory):
    from app.auth.uid_cookie import sign_uid
    from app.main import _enforce_project_membership

    uid, _own = await _user_with_project(db_session_factory)
    _other_uid, foreign = await _user_with_project(db_session_factory)

    scope = _scope(
        {"uid": sign_uid(uid), "active_project_id": foreign, "csrf_token": "t"}, f"project_id={foreign}&x=1"
    )
    await _enforce_project_membership(scope)

    cookies = _cookies_of(scope)
    assert "active_project_id" not in cookies
    assert cookies["uid"] == sign_uid(uid) and cookies["csrf_token"] == "t"  # other cookies untouched
    assert scope["query_string"] == b"x=1"


@pytest.mark.asyncio
async def test_own_project_cookie_and_query_are_kept(_patch_db, db_session_factory):
    from app.auth.uid_cookie import sign_uid
    from app.main import _enforce_project_membership

    uid, own = await _user_with_project(db_session_factory)
    scope = _scope({"uid": sign_uid(uid), "active_project_id": own}, f"project_id={own}")
    await _enforce_project_membership(scope)

    assert _cookies_of(scope)["active_project_id"] == own
    assert scope["query_string"] == f"project_id={own}".encode()


@pytest.mark.asyncio
async def test_removed_member_loses_the_project(_patch_db, db_session_factory):
    from sqlalchemy import update

    from app.auth.uid_cookie import sign_uid
    from app.main import _enforce_project_membership
    from app.models.project import ProjectMember

    uid, own = await _user_with_project(db_session_factory)
    async with db_session_factory() as db:
        await db.execute(
            update(ProjectMember).where(ProjectMember.user_id == uuid.UUID(uid)).values(is_active=False)
        )
        await db.commit()

    scope = _scope({"uid": sign_uid(uid), "active_project_id": own})
    await _enforce_project_membership(scope)
    assert "active_project_id" not in _cookies_of(scope)


@pytest.mark.asyncio
async def test_unsigned_or_garbage_values_are_dropped(_patch_db, db_session_factory):
    from app.main import _enforce_project_membership

    uid, own = await _user_with_project(db_session_factory)
    forged = _scope({"uid": f"{uid}.forged-signature", "active_project_id": own}, f"project_id={own}")
    await _enforce_project_membership(forged)
    assert "active_project_id" not in _cookies_of(forged)
    assert forged["query_string"] == b""

    garbage = _scope({"active_project_id": "not-a-uuid"})
    await _enforce_project_membership(garbage)
    assert "active_project_id" not in _cookies_of(garbage)


@pytest.mark.parametrize(
    ("referer", "expected"),
    [
        ("https://fluxito.app/project/old-slug/settings", "/project/new-slug/settings"),
        ("https://fluxito.app/tracking-plan", "/tracking-plan"),
        ("https://fluxito.app/audits/0b8f1f62-2f8a-4b5c-9f8e-1a2b3c4d5e6f", "/audits"),
        ("https://fluxito.app//evil.example", "/home"),
        ("https://fluxito.app/\\evil.example", "/home"),
        ("https://fluxito.app/api/implement/coverage", "/home"),
        ("", "/home"),
    ],
)
def test_switch_redirect_stays_on_site_and_follows_the_new_project(referer, expected):
    from app.api.project_routes import _switch_redirect_target

    assert _switch_redirect_target(referer, "new-slug") == expected
