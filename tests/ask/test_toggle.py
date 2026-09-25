"""The instance-wide chat on/off runtime setting (chat_enabled)."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

import app.app_state as app_state
from app import branding
from app.settings_service import RUNTIME_SETTING_BY_KEY, get_chat_enabled


@asynccontextmanager
async def _fake_db_factory():
    yield AsyncMock()


@pytest.fixture(autouse=True)
def _restore_state():
    orig = branding._CHAT_CACHE.get("enabled", True)
    orig_db = app_state.db_session_factory
    app_state.db_session_factory = _fake_db_factory
    yield
    branding._CHAT_CACHE["enabled"] = orig
    app_state.db_session_factory = orig_db


def test_chat_runtime_setting_registered():
    spec = RUNTIME_SETTING_BY_KEY.get("chat_enabled")
    assert spec is not None
    assert spec.env_name == "CHAT_ENABLED"
    assert spec.value_type == "bool"
    assert "ask_flux_enabled" not in RUNTIME_SETTING_BY_KEY


def test_config_default_on():
    from app.config import settings

    assert settings.CHAT_ENABLED is True
    assert not hasattr(settings, "ASK_FLUX_ENABLED")


@pytest.mark.asyncio
async def test_cache_and_refresh():
    with patch("app.settings_service.get_runtime_setting", new=AsyncMock(return_value=False)):
        assert await branding.refresh_chat() is False
        assert branding.chat_enabled() is False
    with patch("app.settings_service.get_runtime_setting", new=AsyncMock(return_value=True)):
        assert await branding.refresh_chat() is True
        assert branding.chat_enabled() is True


@pytest.mark.asyncio
async def test_get_chat_enabled_helper():
    with patch("app.settings_service.get_runtime_setting", new=AsyncMock(return_value=False)):
        assert await get_chat_enabled() is False
