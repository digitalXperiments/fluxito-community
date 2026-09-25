"""Route-level tests for the chat API (minimal app with only the chat router)."""

import json
import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import app.app_state as app_state
import app.models  # noqa: F401
from app import branding
from app.api.ask_routes import _sse_frame, base_url_error, router
from app.ask.keys import get_config, save_config
from app.ask.providers.base import StopReason, StreamEvent
from app.ask.service import ConversationService
from tests.ask._fixtures import seed_user_project


def test_router_exposes_expected_paths():
    paths = {(r.path, m) for r in router.routes for m in getattr(r, "methods", set())}
    for expected in [
        ("/chat", "GET"),
        ("/ask", "GET"),
        ("/settings/ai", "GET"),
        ("/api/chat/stream", "POST"),
        ("/api/chat/conversations", "GET"),
        ("/api/chat/conversations/{conversation_id}", "GET"),
        ("/api/chat/conversations/{conversation_id}", "PATCH"),
        ("/api/chat/conversations/{conversation_id}", "DELETE"),
        ("/api/chat/config", "GET"),
        ("/api/chat/config", "PUT"),
        ("/api/chat/config", "DELETE"),
        ("/api/chat/config/test", "POST"),
    ]:
        assert expected in paths, expected


def test_removed_routes_are_gone():
    paths = {r.path for r in router.routes}
    for gone in (
        "/settings/ai-models",
        "/api/ask/confirm-action",
        "/api/ask/model-options",
        "/api/ask/stream",
    ):
        assert gone not in paths
    assert not any("admin/models" in p or "drafts" in p for p in paths)


def test_sse_frame_format():
    frame = _sse_frame({"type": "text_delta", "text": "hi"})
    assert frame.startswith("data: ") and frame.endswith("\n\n")


@pytest.mark.asyncio
async def test_base_url_validation():
    with patch("app.auth.superadmin_cache.is_superadmin_cached", new=AsyncMock(return_value=False)):
        assert await base_url_error("https://api.openai.com/v1", "u") is None
        assert await base_url_error("https://openrouter.ai/api/v1", "u") is None
        assert await base_url_error("", "u")
        assert await base_url_error("ftp://x/v1", "u")
        assert await base_url_error("https://user:pw@api.openai.com/v1", "u")
        assert "admin" in await base_url_error("http://localhost:1234/v1", "u")
        assert "admin" in await base_url_error("http://10.0.0.5/v1", "u")
    with patch("app.auth.superadmin_cache.is_superadmin_cached", new=AsyncMock(return_value=True)):
        assert await base_url_error("http://localhost:1234/v1", "u") is None
        assert await base_url_error("http://169.254.169.254/latest", "u")
        assert await base_url_error("http://metadata.google.internal/", "u")


# ── HTTP-level tests ─────────────────────────────────────────────────────────


@pytest.fixture
def _patch_db(db_session_factory):
    original = app_state.db_session_factory
    app_state.db_session_factory = db_session_factory
    yield
    app_state.db_session_factory = original


@pytest.fixture(autouse=True)
def _chat_on():
    orig = branding._CHAT_CACHE.get("enabled", True)
    branding._CHAT_CACHE["enabled"] = True
    yield
    branding._CHAT_CACHE["enabled"] = orig


class _Env:
    def __init__(self, uid, pid):
        self.uid, self.pid = uid, pid
        self.client = AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test")


def _app():
    a = FastAPI()
    a.include_router(router)
    return a


@pytest.fixture
async def env(_patch_db, db_session_factory):
    uid, pid = await seed_user_project(db_session_factory)
    e = _Env(uid, pid)
    with ExitStack() as stack:
        stack.enter_context(patch("app.api.ask_routes.get_uid_from_request", return_value=str(uid)))
        stack.enter_context(
            patch("app.api.project_routes.ensure_active_project", new=AsyncMock(return_value=str(pid)))
        )
        stack.enter_context(
            patch("app.auth.connection_gate.member_role", new=AsyncMock(return_value="owner"))
        )
        stack.enter_context(
            patch("app.auth.superadmin_cache.is_superadmin_cached", new=AsyncMock(return_value=False))
        )
        async with e.client:
            yield e


@pytest.mark.asyncio
async def test_config_crud_never_returns_secret(env):
    r = await env.client.get("/api/chat/config")
    assert r.json()["config"] is None
    assert [p["base_url"] for p in r.json()["presets"]] == [
        "https://api.openai.com/v1",
        "https://openrouter.ai/api/v1",
    ]

    r = await env.client.put(
        "/api/chat/config",
        json={
            "base_url": "https://openrouter.ai/api/v1",
            "model": "openai/gpt-4.1-mini",
            "api_key": "sk-or-abcdef12",
        },
    )
    assert r.status_code == 200
    assert "sk-or-abcdef12" not in r.text
    assert r.json()["config"]["key_hint"] == "••••ef12"

    r = await env.client.put(
        "/api/chat/config", json={"base_url": "https://openrouter.ai/api/v1", "model": ""}
    )
    assert r.status_code == 400
    r = await env.client.put("/api/chat/config", json={"base_url": "http://127.0.0.1:1234/v1", "model": "m"})
    assert r.status_code == 400

    r = await env.client.delete("/api/chat/config")
    assert r.status_code == 200
    assert await get_config(env.uid) is None


@pytest.mark.asyncio
async def test_config_test_uses_stored_key_only_for_same_url(env):
    await save_config(user_id=env.uid, base_url="https://api.openai.com/v1", model="m", api_key="sk-stored")
    seen = []

    class FakeProvider:
        def __init__(self, api_key, base_url, timeout=None):
            seen.append((api_key, base_url))

        async def complete(self, model, prompt):
            return "ok"

    with patch("app.api.ask_routes.OpenAIProvider", FakeProvider):
        r = await env.client.post(
            "/api/chat/config/test", json={"base_url": "https://api.openai.com/v1", "model": "m"}
        )
        assert r.json() == {"ok": True}
        await env.client.post(
            "/api/chat/config/test", json={"base_url": "https://other.example.com/v1", "model": "m"}
        )
    assert seen == [("sk-stored", "https://api.openai.com/v1"), (None, "https://other.example.com/v1")]


@pytest.mark.asyncio
async def test_config_test_reports_provider_error(env):
    from app.ask.providers.openai import ProviderError

    class FailingProvider:
        def __init__(self, *a, **k):
            pass

        async def complete(self, model, prompt):
            raise ProviderError(401, '{"error": {"message": "Incorrect API key"}}')

    with patch("app.api.ask_routes.OpenAIProvider", FailingProvider):
        r = await env.client.post(
            "/api/chat/config/test",
            json={"base_url": "https://api.openai.com/v1", "model": "m", "api_key": "x"},
        )
    body = r.json()
    assert body["ok"] is False and "Incorrect API key" in body["error"]


@pytest.mark.asyncio
async def test_stream_without_config_returns_no_key(env):
    r = await env.client.post("/api/chat/stream", json={"message": "hi"})
    assert r.status_code == 400 and r.json()["error"] == "no_key"


class _StreamProvider:
    scripts: list = []

    def __init__(self, api_key, base_url):
        self.api_key, self.base_url = api_key, base_url

    async def stream(self, **kwargs):
        for ev in _StreamProvider.scripts.pop(0):
            yield ev


class _NoToolsBridge:
    def __init__(self, **kwargs):
        pass

    def tool_specs(self):
        return []

    async def dispatch(self, name, params):  # pragma: no cover - never called
        raise AssertionError


def _frames(text):
    return [json.loads(line[5:]) for line in text.split("\n") if line.startswith("data:")]


@pytest.mark.asyncio
async def test_stream_creates_conversation_and_persists(env):
    await save_config(user_id=env.uid, base_url="https://api.openai.com/v1", model="gpt-4.1", api_key="sk-1")
    _StreamProvider.scripts = [
        [
            StreamEvent(type="text_delta", text="Hello there"),
            StreamEvent(type="message_done", stop_reason=StopReason.END),
        ]
    ]
    with (
        patch("app.api.ask_routes.OpenAIProvider", _StreamProvider),
        patch("app.api.ask_routes.ChatToolBridge", _NoToolsBridge),
        patch("app.auth.permissions.resolve_effective_permissions", new=AsyncMock(return_value=None)),
    ):
        r = await env.client.post("/api/chat/stream", json={"message": "How was last week?"})
    frames = _frames(r.text)
    assert frames[0]["type"] == "conversation" and frames[0]["title"] == "How was last week?"
    assert frames[0]["model"] == "gpt-4.1"
    assert any(f["type"] == "text_delta" and f["text"] == "Hello there" for f in frames)
    assert frames[-1]["type"] == "done"

    conv_id = frames[0]["conversation_id"]
    r = await env.client.get(f"/api/chat/conversations/{conv_id}")
    data = r.json()
    assert [m["role"] for m in data["messages"]] == ["user", "assistant"]

    r = await env.client.get("/api/chat/conversations")
    assert [c["id"] for c in r.json()["conversations"]] == [conv_id]
    r = await env.client.get("/api/chat/conversations?q=nothing-matches")
    assert r.json()["conversations"] == []

    r = await env.client.patch(f"/api/chat/conversations/{conv_id}", json={"title": "  Weekly   review "})
    assert r.json()["title"] == "Weekly review"
    r = await env.client.delete(f"/api/chat/conversations/{conv_id}")
    assert r.status_code == 200
    r = await env.client.get(f"/api/chat/conversations/{conv_id}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_other_users_conversation_is_not_found(env, db_session_factory):
    other_uid, other_pid = await seed_user_project(db_session_factory)
    conv = await ConversationService().create(
        project_id=other_pid, user_id=other_uid, model="m", title="secret"
    )
    r = await env.client.get(f"/api/chat/conversations/{conv.id}")
    assert r.status_code == 404
    r = await env.client.delete(f"/api/chat/conversations/{conv.id}")
    assert r.status_code == 404
    r = await env.client.get(f"/api/chat/conversations/{uuid.uuid4()}")
    assert r.status_code == 404
    r = await env.client.get("/api/chat/conversations/not-a-uuid")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_stream_rejects_conversation_from_other_project(env, db_session_factory):
    await save_config(user_id=env.uid, base_url="https://api.openai.com/v1", model="m", api_key="sk")
    _, other_pid = await seed_user_project(db_session_factory)
    conv = await ConversationService().create(project_id=other_pid, user_id=env.uid, model="m")
    r = await env.client.post("/api/chat/stream", json={"message": "hi", "conversation_id": str(conv.id)})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_routes_disabled_by_toggle(env):
    branding._CHAT_CACHE["enabled"] = False
    for method, path in [
        ("POST", "/api/chat/stream"),
        ("GET", "/api/chat/conversations"),
        ("GET", "/api/chat/config"),
        ("POST", "/api/chat/config/test"),
    ]:
        r = await env.client.request(method, path, json={})
        assert r.status_code == 403, path
    r = await env.client.get("/chat", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/home"
    r = await env.client.get("/settings/ai", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/settings"


@pytest.mark.asyncio
async def test_ask_redirects_to_chat(env):
    r = await env.client.get("/ask", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/chat"
