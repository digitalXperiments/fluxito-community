import pytest
from httpx import ASGITransport, AsyncClient

from app.api import update_routes
from app.main import app
from app.services import update_service


@pytest.mark.asyncio
async def test_status_endpoint_returns_check_result(monkeypatch):
    async def _fake_check():
        return {
            "current": "1.0.2",
            "latest": "1.0.5",
            "update_available": True,
            "release_notes_url": "https://x",
            "published_at": "2026-05-30T00:00:00Z",
            "checks_enabled": True,
        }

    async def _user(request):
        return {"user_id": "1"}

    monkeypatch.setattr(update_routes, "_resolve_user_ctx", _user)
    monkeypatch.setattr(update_service, "check_for_update", _fake_check)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/updates/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["update_available"] is True
    assert body["latest"] == "1.0.5"


@pytest.mark.asyncio
async def test_status_requires_auth(monkeypatch):
    async def _anon(request):
        return None

    monkeypatch.setattr(update_routes, "_resolve_user_ctx", _anon)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/updates/status")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_apply_requires_superadmin(monkeypatch):
    async def _deny(request):
        from fastapi import HTTPException

        raise HTTPException(403, "Super-admin only")

    monkeypatch.setattr(update_routes, "require_superadmin", _deny)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/updates/apply")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_apply_forwards_to_updater(monkeypatch):
    async def _allow(request):
        return {"id": "1", "email": "a@b.c", "is_superadmin": True}

    async def _check():
        return {
            "current": "1.0.2",
            "latest": "1.0.5",
            "update_available": True,
            "release_notes_url": "https://x",
            "published_at": None,
            "checks_enabled": True,
        }

    calls = {}

    async def _post(version, previous):
        calls["version"] = version
        calls["previous"] = previous
        return {"status": "accepted", "target": version}

    monkeypatch.setattr(update_routes, "require_superadmin", _allow)
    monkeypatch.setattr(update_service, "check_for_update", _check)
    monkeypatch.setattr(update_routes, "_trigger_updater", _post)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/updates/apply")
    assert resp.status_code == 200
    assert calls["version"] == "1.0.5"
    assert calls["previous"] == "1.0.2"


@pytest.mark.asyncio
async def test_apply_rejects_when_no_update(monkeypatch):
    async def _allow(request):
        return {"id": "1", "email": "a@b.c", "is_superadmin": True}

    async def _check():
        return {
            "current": "1.0.5",
            "latest": "1.0.5",
            "update_available": False,
            "release_notes_url": None,
            "published_at": None,
            "checks_enabled": True,
        }

    monkeypatch.setattr(update_routes, "require_superadmin", _allow)
    monkeypatch.setattr(update_service, "check_for_update", _check)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/updates/apply")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_apply_reports_updater_authentication_failure(monkeypatch):
    async def _allow(request):
        return {"id": "1", "email": "a@b.c", "is_superadmin": True}

    async def _check():
        return {
            "current": "1.0.2",
            "latest": "1.0.5",
            "update_available": True,
            "release_notes_url": "https://x",
            "published_at": None,
            "checks_enabled": True,
        }

    async def _post(version, previous):
        request = update_routes.httpx.Request("POST", "http://updater:9000/update")
        response = update_routes.httpx.Response(401, request=request, json={"error": "unauthorized"})
        raise update_routes.httpx.HTTPStatusError("unauthorized", request=request, response=response)

    monkeypatch.setattr(update_routes, "require_superadmin", _allow)
    monkeypatch.setattr(update_service, "check_for_update", _check)
    monkeypatch.setattr(update_routes, "_trigger_updater", _post)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/updates/apply")

    assert resp.status_code == 503
    assert resp.json() == {
        "error": "updater authentication failed",
        "code": "updater_auth_failed",
    }


@pytest.mark.asyncio
async def test_apply_reports_existing_update_job(monkeypatch):
    async def _allow(request):
        return {"id": "1", "email": "a@b.c", "is_superadmin": True}

    async def _check():
        return {
            "current": "1.0.2",
            "latest": "1.0.5",
            "update_available": True,
            "release_notes_url": "https://x",
            "published_at": None,
            "checks_enabled": True,
        }

    async def _post(version, previous):
        request = update_routes.httpx.Request("POST", "http://updater:9000/update")
        response = update_routes.httpx.Response(
            409, request=request, json={"error": "update already in progress"}
        )
        raise update_routes.httpx.HTTPStatusError("conflict", request=request, response=response)

    monkeypatch.setattr(update_routes, "require_superadmin", _allow)
    monkeypatch.setattr(update_service, "check_for_update", _check)
    monkeypatch.setattr(update_routes, "_trigger_updater", _post)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/updates/apply")

    assert resp.status_code == 409
    assert resp.json() == {
        "error": "update already in progress",
        "code": "update_in_progress",
    }


@pytest.mark.asyncio
async def test_apply_reports_unreachable_updater(monkeypatch):
    async def _allow(request):
        return {"id": "1", "email": "a@b.c", "is_superadmin": True}

    async def _check():
        return {
            "current": "1.0.2",
            "latest": "1.0.5",
            "update_available": True,
            "release_notes_url": "https://x",
            "published_at": None,
            "checks_enabled": True,
        }

    async def _post(version, previous):
        request = update_routes.httpx.Request("POST", "http://updater:9000/update")
        raise update_routes.httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(update_routes, "require_superadmin", _allow)
    monkeypatch.setattr(update_service, "check_for_update", _check)
    monkeypatch.setattr(update_routes, "_trigger_updater", _post)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/updates/apply")

    assert resp.status_code == 503
    assert resp.json() == {
        "error": "updater unavailable",
        "code": "updater_unavailable",
    }


@pytest.mark.asyncio
async def test_job_requires_superadmin(monkeypatch):
    async def _deny(request):
        from fastapi import HTTPException

        raise HTTPException(403, "Super-admin only")

    monkeypatch.setattr(update_routes, "require_superadmin", _deny)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/updates/job")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_job_returns_503_when_updater_unreachable(monkeypatch):
    async def _allow(request):
        return {"id": "1", "email": "a@b.c", "is_superadmin": True}

    monkeypatch.setattr(update_routes, "require_superadmin", _allow)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # No updater reachable at UPDATER_URL in the test env -> httpx error -> 503
        resp = await client.get("/api/updates/job")
    assert resp.status_code == 503
    assert resp.json()["status"] == "unknown"


class _FakeRedis:
    def __init__(self, initial=None):
        self.store = dict(initial or {})

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, ttl, value):
        self.store[key] = value


@pytest.mark.asyncio
async def test_check_requires_superadmin(monkeypatch):
    async def _deny(request):
        from fastapi import HTTPException

        raise HTTPException(403, "Super-admin only")

    monkeypatch.setattr(update_routes, "require_superadmin", _deny)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/updates/check")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_check_forces_fresh_status(monkeypatch):
    import app.app_state as app_state

    async def _allow(request):
        return {"id": "1", "email": "a@b.c", "is_superadmin": True}

    forced = {}

    async def _check(force=False):
        forced["force"] = force
        return {
            "current": "1.0.2",
            "latest": "1.0.9",
            "update_available": True,
            "release_notes_url": "https://x",
            "published_at": None,
            "checks_enabled": True,
        }

    monkeypatch.setattr(update_routes, "require_superadmin", _allow)
    monkeypatch.setattr(update_service, "check_for_update", _check)
    monkeypatch.setattr(app_state, "redis_client", _FakeRedis())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/updates/check")
    assert resp.status_code == 200
    assert forced["force"] is True
    assert resp.json()["latest"] == "1.0.9"


@pytest.mark.asyncio
async def test_check_rejected_within_cooldown(monkeypatch):
    import app.app_state as app_state

    async def _allow(request):
        return {"id": "1", "email": "a@b.c", "is_superadmin": True}

    monkeypatch.setattr(update_routes, "require_superadmin", _allow)
    monkeypatch.setattr(app_state, "redis_client", _FakeRedis({update_routes.CHECK_COOLDOWN_KEY: "1"}))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/updates/check")
    assert resp.status_code == 429
    assert resp.json()["code"] == "check_cooldown"


@pytest.mark.asyncio
async def test_check_runs_when_redis_unavailable(monkeypatch):
    """Cooldown guard degrades open: a None Redis must not block the check."""
    import app.app_state as app_state

    async def _allow(request):
        return {"id": "1", "email": "a@b.c", "is_superadmin": True}

    async def _check(force=False):
        return {
            "current": "1.0.2",
            "latest": "1.0.9",
            "update_available": True,
            "release_notes_url": "https://x",
            "published_at": None,
            "checks_enabled": True,
        }

    monkeypatch.setattr(update_routes, "require_superadmin", _allow)
    monkeypatch.setattr(update_service, "check_for_update", _check)
    monkeypatch.setattr(app_state, "redis_client", None)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/updates/check")
    assert resp.status_code == 200
    assert resp.json()["latest"] == "1.0.9"
