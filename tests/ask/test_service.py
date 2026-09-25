"""Tests for ConversationService (DB-backed)."""

import pytest

import app.app_state as app_state
import app.models  # noqa: F401  (ensures all model metadata loaded)
from app.ask.providers.base import LLMMessage, TextBlock, ToolResultBlock, ToolUseBlock
from app.ask.service import ConversationService
from tests.ask._fixtures import seed_user_project


@pytest.fixture
def _patch_db(db_session_factory):
    original = app_state.db_session_factory
    app_state.db_session_factory = db_session_factory
    yield
    app_state.db_session_factory = original


@pytest.mark.asyncio
async def test_create_append_and_load_round_trip(_patch_db, db_session_factory):
    uid, pid = await seed_user_project(db_session_factory)
    svc = ConversationService()
    conv = await svc.create(project_id=pid, user_id=uid, model="gpt-4.1-mini", title="Hello")
    assert conv.title == "Hello"

    await svc.append(conv.id, LLMMessage(role="user", content=[TextBlock(text="hi")]))
    await svc.append(
        conv.id,
        LLMMessage(role="assistant", content=[ToolUseBlock(id="t1", name="analytics_read", input={"a": 1})]),
        token_usage={"model": "gpt-4.1-mini"},
    )
    await svc.append(
        conv.id, LLMMessage(role="tool", content=[ToolResultBlock(tool_use_id="t1", content="{}")])
    )

    history = await svc.load_history(conv.id)
    assert [m.role for m in history] == ["user", "assistant", "tool"]
    assert history[1].content[0].name == "analytics_read"
    rows = await svc.load_messages(conv.id)
    assert rows[1][2] == {"model": "gpt-4.1-mini"}


@pytest.mark.asyncio
async def test_list_search_rename_delete(_patch_db, db_session_factory):
    uid, pid = await seed_user_project(db_session_factory)
    other_uid, _ = await seed_user_project(db_session_factory)
    svc = ConversationService()
    a = await svc.create(project_id=pid, user_id=uid, model="m", title="Weekly traffic")
    b = await svc.create(project_id=pid, user_id=uid, model="m", title="Ad spend 100%")
    await svc.create(project_id=pid, user_id=other_uid, model="m", title="Weekly traffic (other user)")

    assert {c.id for c in await svc.list_for(project_id=pid, user_id=uid)} == {a.id, b.id}
    assert [c.id for c in await svc.list_for(project_id=pid, user_id=uid, query="weekly")] == [a.id]
    # LIKE wildcards in the query are literal.
    assert [c.id for c in await svc.list_for(project_id=pid, user_id=uid, query="100%")] == [b.id]
    assert await svc.list_for(project_id=pid, user_id=uid, query="_") == []

    await svc.set_title(a.id, "Renamed")
    assert (await svc.get(a.id)).title == "Renamed"

    await svc.append(b.id, LLMMessage(role="user", content=[TextBlock(text="x")]))
    await svc.delete(b.id)
    assert await svc.get(b.id) is None
    assert await svc.load_history(b.id) == []
