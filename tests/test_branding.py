# tests/test_branding.py
"""Whitelabel branding: provider, emails, admin route."""

import pytest

import app.app_state as app_state


@pytest.fixture
def _patch_db(db_session_factory):
    original = app_state.db_session_factory
    app_state.db_session_factory = db_session_factory
    yield
    app_state.db_session_factory = original
    # Reset the module-global brand cache so branding tests never leak
    # a non-default value into other tests/files.
    import app.branding as _b

    _b._BRAND_CACHE.update({"name": "Fluxito", "logo_url": "", "accent": ""})


@pytest.mark.asyncio
async def test_brand_defaults(_patch_db, db_session_factory):
    from app.branding import brand, refresh_brand

    await refresh_brand()
    b = brand()
    assert b["name"] == "Fluxito"
    assert b["logo_url"] == ""
    assert b["accent"] == ""


@pytest.mark.asyncio
async def test_brand_reflects_settings(_patch_db, db_session_factory):
    from app.branding import brand, refresh_brand
    from app.settings_service import set_setting

    async with db_session_factory() as db:
        await set_setting(
            db, key="brand_name", value="Acme Analytics", is_secret=False, updated_by_user_id=None
        )
        await set_setting(
            db, key="brand_logo_url", value="https://x/logo.png", is_secret=False, updated_by_user_id=None
        )
        await set_setting(db, key="brand_accent", value="#ff0000", is_secret=False, updated_by_user_id=None)
        await db.commit()

    await refresh_brand()
    b = brand()
    assert b["name"] == "Acme Analytics"
    assert b["logo_url"] == "https://x/logo.png"
    assert b["accent"] == "#ff0000"


@pytest.mark.asyncio
async def test_invite_email_uses_brand_name(_patch_db, db_session_factory, monkeypatch):
    import app.email_service as es
    from app.branding import refresh_brand
    from app.settings_service import set_setting

    async with db_session_factory() as db:
        await set_setting(
            db, key="brand_name", value="Acme Analytics", is_secret=False, updated_by_user_id=None
        )
        await db.commit()
    await refresh_brand()

    captured = {}

    async def _fake_send_email(to_email, subject, html_body, text_body=None):
        captured["subject"] = subject
        captured["html"] = html_body
        captured["text"] = text_body

    monkeypatch.setattr(es, "send_email", _fake_send_email)
    await es.send_project_invite_email(
        to_email="x@example.com",
        project_name="Proj",
        project_slug="proj",
        inviter_email="boss@example.com",
        role="member",
    )
    # The subject is deliberately personal now ("boss invited you to Proj") and
    # no longer carries the brand name — the brand identity lives in the email
    # body (header wordmark, From line, footer). The rebrand must still fully
    # replace the default "Fluxito" wordmark in the rendered body.
    assert "invited you to Proj" in captured["subject"]
    assert "Acme Analytics" in captured["html"]
    assert "Fluxito" not in captured["html"]


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


def _ctx(uid, email):
    return type("C", (), {"user_id": uid, "email": email})()


@pytest.mark.asyncio
async def test_branding_route_requires_superadmin(_http_client, db_session_factory):
    from unittest.mock import AsyncMock, patch

    uid = await _make_user(db_session_factory, "plain-b@example.com", is_superadmin=False)
    with patch(
        "app.api.admin_routes._resolve_user_ctx", new=AsyncMock(return_value=_ctx(uid, "plain-b@example.com"))
    ):
        resp = await _http_client.patch(
            "/api/admin/settings/branding", json={"name": "Acme", "logo_url": "", "accent": ""}
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_branding_route_writes_and_refreshes(_http_client, db_session_factory):
    from unittest.mock import AsyncMock, patch

    from app.branding import brand

    sid = await _make_user(db_session_factory, "super-b@example.com", is_superadmin=True)
    with patch(
        "app.api.admin_routes._resolve_user_ctx", new=AsyncMock(return_value=_ctx(sid, "super-b@example.com"))
    ):
        resp = await _http_client.patch(
            "/api/admin/settings/branding",
            json={"name": "Acme Co", "logo_url": "https://x/l.png", "accent": "#123456"},
        )
    assert resp.status_code == 200, resp.text
    assert brand()["name"] == "Acme Co"
    assert brand()["accent"] == "#123456"


@pytest.mark.asyncio
async def test_branding_route_rejects_empty_name(_http_client, db_session_factory):
    from unittest.mock import AsyncMock, patch

    sid = await _make_user(db_session_factory, "super-b2@example.com", is_superadmin=True)
    with patch(
        "app.api.admin_routes._resolve_user_ctx",
        new=AsyncMock(return_value=_ctx(sid, "super-b2@example.com")),
    ):
        resp = await _http_client.patch(
            "/api/admin/settings/branding", json={"name": "  ", "logo_url": "", "accent": ""}
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_branding_route_rejects_bad_accent(_http_client, db_session_factory):
    from unittest.mock import AsyncMock, patch

    sid = await _make_user(db_session_factory, "super-b4@example.com", is_superadmin=True)
    with patch(
        "app.api.admin_routes._resolve_user_ctx",
        new=AsyncMock(return_value=_ctx(sid, "super-b4@example.com")),
    ):
        resp = await _http_client.patch(
            "/api/admin/settings/branding", json={"name": "Acme", "logo_url": "", "accent": "#fff; } body{}"}
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_branding_get_returns_values(_http_client, db_session_factory):
    from unittest.mock import AsyncMock, patch

    sid = await _make_user(db_session_factory, "super-b3@example.com", is_superadmin=True)
    with patch(
        "app.api.admin_routes._resolve_user_ctx",
        new=AsyncMock(return_value=_ctx(sid, "super-b3@example.com")),
    ):
        await _http_client.patch(
            "/api/admin/settings/branding", json={"name": "Zeta", "logo_url": "", "accent": ""}
        )
        resp = await _http_client.get("/api/admin/settings/branding")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Zeta"
