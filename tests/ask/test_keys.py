import pytest
from sqlalchemy import select

import app.app_state as app_state
import app.models  # noqa: F401  (loads model metadata)
from app.ask.keys import (
    ChatConfig,
    delete_config,
    get_config,
    get_config_info,
    resolve_api_key,
    save_config,
)
from app.models.conversation import AIProviderKey
from tests.ask._fixtures import seed_user_project


@pytest.fixture
def _patch_db(db_session_factory):
    original = app_state.db_session_factory
    app_state.db_session_factory = db_session_factory
    yield
    app_state.db_session_factory = original


@pytest.mark.asyncio
async def test_save_then_get_round_trips_and_encrypts(_patch_db, db_session_factory):
    uid, _ = await seed_user_project(db_session_factory)
    await save_config(
        user_id=uid, base_url="https://api.openai.com/v1/", model="gpt-4.1", api_key="sk-secret-1234"
    )
    cfg = await get_config(uid)
    assert cfg == ChatConfig(base_url="https://api.openai.com/v1", model="gpt-4.1", api_key="sk-secret-1234")
    async with db_session_factory() as db:
        row = (await db.execute(select(AIProviderKey).where(AIProviderKey.user_id == uid))).scalar_one()
    assert row.api_key_encrypted and "sk-secret" not in row.api_key_encrypted

    info = await get_config_info(uid)
    assert info.has_key and info.key_hint == "••••1234"
    assert "sk-secret" not in repr(info)


@pytest.mark.asyncio
async def test_one_config_per_user_and_blank_key_keeps_secret(_patch_db, db_session_factory):
    uid, _ = await seed_user_project(db_session_factory)
    await save_config(user_id=uid, base_url="https://openrouter.ai/api/v1", model="a", api_key="sk-or-1")
    await save_config(user_id=uid, base_url="https://openrouter.ai/api/v1", model="b", api_key=None)
    cfg = await get_config(uid)
    assert cfg.model == "b" and cfg.api_key == "sk-or-1"
    async with db_session_factory() as db:
        rows = (await db.execute(select(AIProviderKey).where(AIProviderKey.user_id == uid))).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_changing_base_url_without_key_clears_secret(_patch_db, db_session_factory):
    uid, _ = await seed_user_project(db_session_factory)
    await save_config(user_id=uid, base_url="https://api.openai.com/v1", model="a", api_key="sk-1")
    await save_config(user_id=uid, base_url="https://llm.example.com/v1", model="a", api_key=None)
    cfg = await get_config(uid)
    assert cfg.base_url == "https://llm.example.com/v1" and cfg.api_key is None


@pytest.mark.asyncio
async def test_resolve_api_key_never_sends_stored_key_elsewhere(_patch_db, db_session_factory):
    uid, _ = await seed_user_project(db_session_factory)
    await save_config(user_id=uid, base_url="https://api.openai.com/v1", model="a", api_key="sk-1")
    assert await resolve_api_key(uid, "https://api.openai.com/v1/", None) == "sk-1"
    assert await resolve_api_key(uid, "https://evil.example.com/v1", None) is None
    assert await resolve_api_key(uid, "https://evil.example.com/v1", "sk-new") == "sk-new"


@pytest.mark.asyncio
async def test_configs_are_personal_and_deletable(_patch_db, db_session_factory):
    uid_a, _ = await seed_user_project(db_session_factory)
    uid_b, _ = await seed_user_project(db_session_factory)
    await save_config(user_id=uid_a, base_url="https://api.openai.com/v1", model="a", api_key="sk-a")
    assert await get_config(uid_b) is None
    await delete_config(uid_a)
    assert await get_config(uid_a) is None
    assert await get_config_info(uid_a) is None
