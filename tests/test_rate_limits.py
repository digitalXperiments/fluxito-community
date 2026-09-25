# tests/test_rate_limits.py
"""Tests for MCP rate limiting: exemption cache, limiter decision, admin config."""

import uuid

import pytest

import app.app_state as app_state


@pytest.fixture
def _patch_db(db_session_factory):
    original = app_state.db_session_factory
    app_state.db_session_factory = db_session_factory
    yield
    app_state.db_session_factory = original


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
async def test_superadmin_cache_true_for_superadmin(_patch_db, db_session_factory):
    from app.auth.superadmin_cache import _clear_superadmin_cache, is_superadmin_cached

    _clear_superadmin_cache()
    sid = await _make_user(db_session_factory, "s@example.com", is_superadmin=True)
    assert await is_superadmin_cached(sid) is True


@pytest.mark.asyncio
async def test_superadmin_cache_false_for_normal_user(_patch_db, db_session_factory):
    from app.auth.superadmin_cache import _clear_superadmin_cache, is_superadmin_cached

    _clear_superadmin_cache()
    uid = await _make_user(db_session_factory, "n@example.com", is_superadmin=False)
    assert await is_superadmin_cached(uid) is False


@pytest.mark.asyncio
async def test_superadmin_cache_caches(_patch_db, db_session_factory):
    """A cached value persists within TTL even if the DB row changes."""
    from sqlalchemy import update

    from app.auth.superadmin_cache import _clear_superadmin_cache, is_superadmin_cached
    from app.models.user import User

    _clear_superadmin_cache()
    sid = await _make_user(db_session_factory, "c@example.com", is_superadmin=True)
    assert await is_superadmin_cached(sid) is True  # caches True

    async with db_session_factory() as db:
        await db.execute(update(User).where(User.id == uuid.UUID(sid)).values(is_superadmin=False))
        await db.commit()

    assert await is_superadmin_cached(sid) is True  # still True from cache (within TTL)


@pytest.mark.asyncio
async def test_superadmin_cache_unknown_user_false(_patch_db, db_session_factory):
    from app.auth.superadmin_cache import _clear_superadmin_cache, is_superadmin_cached

    _clear_superadmin_cache()
    assert await is_superadmin_cached(str(uuid.uuid4())) is False


@pytest.mark.asyncio
async def test_check_rate_limit_blocks_over_limit(_patch_db, db_session_factory, monkeypatch):
    """check_rate_limit returns a rate_limited error dict once per-min is exceeded."""
    import app.auth.rate_limiter as rl

    class _FakeRedis:
        def __init__(self):
            self.counts = {}

        async def incr(self, key):
            self.counts[key] = self.counts.get(key, 0) + 1
            return self.counts[key]

        async def expire(self, key, ttl):
            return True

        async def get(self, key):
            return None

    fake = _FakeRedis()
    monkeypatch.setattr(app_state, "redis_client", fake, raising=False)
    monkeypatch.setattr(rl, "_cached_limits", {"default": {"per_min": 2, "per_hour": 1000}}, raising=False)
    monkeypatch.setattr(rl, "_cached_limits_ts", 9e18, raising=False)  # far future → cache fresh

    uid = "u-test"
    assert await rl.check_rate_limit(uid) is None  # 1st
    assert await rl.check_rate_limit(uid) is None  # 2nd
    blocked = await rl.check_rate_limit(uid)  # 3rd → over
    assert blocked is not None
    assert blocked["error_type"] == "rate_limited"
    assert "retry_after_seconds" in blocked


@pytest.mark.asyncio
async def test_check_rate_limit_blocks_over_daily_limit(_patch_db, db_session_factory, monkeypatch):
    """check_rate_limit enforces the per-day window independently of min/hour."""
    import app.auth.rate_limiter as rl

    class _FakeRedis:
        def __init__(self):
            self.counts = {}

        async def incr(self, key):
            self.counts[key] = self.counts.get(key, 0) + 1
            return self.counts[key]

        async def expire(self, key, ttl):
            return True

        async def get(self, key):
            return None

    fake = _FakeRedis()
    monkeypatch.setattr(app_state, "redis_client", fake, raising=False)
    # Minute/hour high so only the per-day cap can trip.
    monkeypatch.setattr(
        rl, "_cached_limits", {"default": {"per_min": 100, "per_hour": 100, "per_day": 2}}, raising=False
    )
    monkeypatch.setattr(rl, "_cached_limits_ts", 9e18, raising=False)

    uid = "u-day"
    assert await rl.check_rate_limit(uid) is None  # 1st
    assert await rl.check_rate_limit(uid) is None  # 2nd
    blocked = await rl.check_rate_limit(uid)  # 3rd → over daily cap
    assert blocked is not None
    assert blocked["error_type"] == "rate_limited"
    assert "Daily" in blocked["message"]
    assert blocked["limit_per_day"] == 2


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


def _ctx(uid, email):
    return type("C", (), {"user_id": uid, "email": email})()


@pytest.mark.asyncio
async def test_rate_limits_route_requires_superadmin(_http_client, db_session_factory):
    from unittest.mock import AsyncMock, patch

    uid = await _make_user(db_session_factory, "plain-rl@example.com", is_superadmin=False)
    with patch(
        "app.api.admin_routes._resolve_user_ctx",
        new=AsyncMock(return_value=_ctx(uid, "plain-rl@example.com")),
    ):
        resp = await _http_client.patch(
            "/api/admin/settings/rate-limits", json={"per_min": 30, "per_hour": 500}
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_rate_limits_route_writes_settings(_http_client, db_session_factory):
    from unittest.mock import AsyncMock, patch

    from app.settings_service import get_runtime_setting

    sid = await _make_user(db_session_factory, "super-rl@example.com", is_superadmin=True)
    with patch(
        "app.api.admin_routes._resolve_user_ctx",
        new=AsyncMock(return_value=_ctx(sid, "super-rl@example.com")),
    ):
        resp = await _http_client.patch(
            "/api/admin/settings/rate-limits", json={"per_min": 42, "per_hour": 999, "per_day": 5000}
        )
    assert resp.status_code == 200, resp.text
    async with db_session_factory() as db:
        assert int(await get_runtime_setting(db, "rate_limit_per_min", default=0)) == 42
        assert int(await get_runtime_setting(db, "rate_limit_per_hour", default=0)) == 999
        assert int(await get_runtime_setting(db, "rate_limit_per_day", default=0)) == 5000


@pytest.mark.asyncio
async def test_rate_limits_route_rejects_nonpositive(_http_client, db_session_factory):
    from unittest.mock import AsyncMock, patch

    sid = await _make_user(db_session_factory, "super-rl2@example.com", is_superadmin=True)
    with patch(
        "app.api.admin_routes._resolve_user_ctx",
        new=AsyncMock(return_value=_ctx(sid, "super-rl2@example.com")),
    ):
        resp = await _http_client.patch(
            "/api/admin/settings/rate-limits", json={"per_min": 0, "per_hour": 500}
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_rate_limits_get_returns_values(_http_client, db_session_factory):
    from unittest.mock import AsyncMock, patch

    sid = await _make_user(db_session_factory, "super-rl3@example.com", is_superadmin=True)
    with patch(
        "app.api.admin_routes._resolve_user_ctx",
        new=AsyncMock(return_value=_ctx(sid, "super-rl3@example.com")),
    ):
        await _http_client.patch(
            "/api/admin/settings/rate-limits", json={"per_min": 11, "per_hour": 222, "per_day": 3333}
        )
        resp = await _http_client.get("/api/admin/settings/rate-limits")
    assert resp.status_code == 200
    data = resp.json()
    assert data["per_min"] == 11 and data["per_hour"] == 222 and data["per_day"] == 3333
