import json

import httpx
import pytest

import app.app_state as app_state
from app.services import update_service
from app.services.update_service import is_newer, parse_semver
from app.settings_service import RUNTIME_SETTING_BY_KEY


def test_update_checks_setting_registered():
    assert "update_checks_enabled" in RUNTIME_SETTING_BY_KEY
    spec = RUNTIME_SETTING_BY_KEY["update_checks_enabled"]
    assert spec.value_type == "bool"


def test_parse_semver_strips_prefix_and_suffix():
    assert parse_semver("v1.2.3") == (1, 2, 3)
    assert parse_semver("1.2.3+local") == (1, 2, 3)
    assert parse_semver("1.2.3-rc1") == (1, 2, 3)
    assert parse_semver("1.0") == (1, 0, 0)


def test_is_newer():
    assert is_newer("1.0.3", "1.0.2") is True
    assert is_newer("1.1.0", "1.0.9") is True
    assert is_newer("1.0.2", "1.0.2") is False
    assert is_newer("1.0.1", "1.0.2") is False
    assert is_newer("1.0.2", "1.0.2+local") is False


class _FakeRedis:
    def __init__(self, initial=None):
        self.store = dict(initial or {})
        self.setex_calls = []

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, ttl, value):
        self.setex_calls.append((key, ttl, value))
        self.store[key] = value


def _async_return(value):
    async def _inner():
        return value

    return _inner


@pytest.mark.asyncio
async def test_check_returns_disabled_when_setting_off(monkeypatch):
    monkeypatch.setattr(update_service, "update_checks_enabled", _async_return(False))
    monkeypatch.setattr(app_state, "redis_client", _FakeRedis())
    result = await update_service.check_for_update()
    assert result["checks_enabled"] is False
    assert result["update_available"] is False


@pytest.mark.asyncio
async def test_check_uses_cache_when_present(monkeypatch):
    monkeypatch.setattr(update_service, "update_checks_enabled", _async_return(True))
    monkeypatch.setattr(update_service, "get_version", lambda: "1.0.2")
    cached = json.dumps(
        {
            "tag_name": "v1.0.5",
            "html_url": "https://x/releases/v1.0.5",
            "published_at": "2026-05-30T00:00:00Z",
        }
    )
    monkeypatch.setattr(app_state, "redis_client", _FakeRedis({update_service.CACHE_KEY: cached}))

    async def _boom(*a, **k):
        raise AssertionError("network should not be called on cache hit")

    monkeypatch.setattr(update_service, "_fetch_latest_release", _boom)
    result = await update_service.check_for_update()
    assert result["latest"] == "1.0.5"
    assert result["update_available"] is True


@pytest.mark.asyncio
async def test_force_bypasses_cache_and_rewrites(monkeypatch):
    monkeypatch.setattr(update_service, "update_checks_enabled", _async_return(True))
    monkeypatch.setattr(update_service, "get_version", lambda: "1.0.2")
    stale = json.dumps(
        {"tag_name": "v1.0.3", "html_url": "https://x/v1.0.3", "published_at": "2026-01-01T00:00:00Z"}
    )
    fake = _FakeRedis({update_service.CACHE_KEY: stale})
    monkeypatch.setattr(app_state, "redis_client", fake)

    calls = {"n": 0}

    async def _fresh(*a, **k):
        calls["n"] += 1
        return {"tag_name": "v1.0.9", "html_url": "https://x/v1.0.9", "published_at": "2026-06-01T00:00:00Z"}

    monkeypatch.setattr(update_service, "_fetch_latest_release", _fresh)

    result = await update_service.check_for_update(force=True)

    assert calls["n"] == 1  # network was hit despite warm cache
    assert result["latest"] == "1.0.9"
    assert result["update_available"] is True
    assert any(key == update_service.CACHE_KEY for key, _ttl, _val in fake.setex_calls)  # rewrote cache


@pytest.mark.asyncio
async def test_check_swallows_network_errors(monkeypatch):
    monkeypatch.setattr(update_service, "update_checks_enabled", _async_return(True))
    monkeypatch.setattr(update_service, "get_version", lambda: "1.0.2")
    monkeypatch.setattr(app_state, "redis_client", _FakeRedis())

    async def _raise(*a, **k):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(update_service, "_fetch_latest_release", _raise)
    result = await update_service.check_for_update()
    assert result["update_available"] is False
    assert result["latest"] is None
