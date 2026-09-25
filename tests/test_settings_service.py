"""Tests for DB-backed runtime settings."""

import pytest

from app.settings_service import (
    _cache_clear_all,
    delete_setting,
    get_runtime_setting,
    get_setting,
    list_runtime_settings,
    set_setting,
)


@pytest.mark.asyncio
async def test_setting_round_trip_plain_value(db_session_factory):
    _cache_clear_all()
    async with db_session_factory() as db:
        await set_setting(db, key="smtp_host", value="smtp.example.com")
        await db.commit()

    async with db_session_factory() as db:
        assert await get_setting(db, "smtp_host") == "smtp.example.com"
        assert await get_runtime_setting(db, "smtp_host") == "smtp.example.com"


@pytest.mark.asyncio
async def test_setting_round_trip_secret_is_masked_in_lists(db_session_factory):
    _cache_clear_all()
    async with db_session_factory() as db:
        await set_setting(db, key="smtp_password", value="super-secret", is_secret=True)
        await db.commit()

    async with db_session_factory() as db:
        assert await get_setting(db, "smtp_password") == "super-secret"
        items = await list_runtime_settings(db)

    smtp_password = next(item for item in items if item["key"] == "smtp_password")
    assert smtp_password["source"] == "db"
    assert smtp_password["value"] == "********"


@pytest.mark.asyncio
async def test_runtime_setting_falls_back_to_config_default(db_session_factory):
    _cache_clear_all()
    async with db_session_factory() as db:
        assert await get_runtime_setting(db, "rate_limit_per_min") == 60


@pytest.mark.asyncio
async def test_delete_setting_resets_to_fallback(db_session_factory):
    _cache_clear_all()
    async with db_session_factory() as db:
        await set_setting(db, key="rate_limit_per_min", value=12)
        await db.commit()

    async with db_session_factory() as db:
        assert await get_runtime_setting(db, "rate_limit_per_min") == 12
        assert await delete_setting(db, key="rate_limit_per_min") is True
        await db.commit()

    async with db_session_factory() as db:
        assert await get_runtime_setting(db, "rate_limit_per_min") == 60
