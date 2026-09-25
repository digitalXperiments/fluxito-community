"""HTTP surface for the in-app Chat: page, SSE stream, conversations, model config."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import urllib.parse
import uuid
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse

from app.ask.context import window_history
from app.ask.harness import Harness, HarnessDeps
from app.ask.keys import (
    delete_config,
    get_config,
    get_config_info,
    normalize_base_url,
    resolve_api_key,
    save_config,
)
from app.ask.prompts import build_system_prompt
from app.ask.providers.base import LLMMessage, StreamEvent, TextBlock, ToolSpec, blocks_to_json
from app.ask.providers.openai import OPENAI_BASE_URL, OPENROUTER_BASE_URL, OpenAIProvider, ProviderError
from app.ask.service import ConversationService
from app.ask.tools import ChatToolBridge
from app.auth.uid_cookie import get_uid_from_request
from app.templating import render

router = APIRouter()
_service = ConversationService()

# Base-URL presets offered by the settings form (they only fill the field).
BASE_URL_PRESETS: list[dict[str, str]] = [
    {"label": "OpenAI", "base_url": OPENAI_BASE_URL},
    {"label": "OpenRouter", "base_url": OPENROUTER_BASE_URL},
]

_MAX_MODEL_LEN = 200
_MAX_URL_LEN = 500
_MAX_KEY_LEN = 1000
_MAX_MESSAGE_LEN = 20000
_TEST_TIMEOUT_S = 20.0


def _sse_frame(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _require_chat() -> None:
    from app.branding import chat_enabled

    if not chat_enabled():
        raise HTTPException(status_code=403, detail="The in-app Chat is disabled on this instance.")


def _parse_uuid(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        return {}
    return body if isinstance(body, dict) else {}


async def base_url_error(url: str, uid: str) -> str | None:
    """Validate a user-supplied model endpoint; returns an error message or None.

    Public http(s) hosts are allowed for everyone. Loopback / private-network
    hosts (a local LM Studio or Ollama) are allowed for instance super-admins
    only; link-local addresses (cloud metadata) are never allowed.
    """
    from app.tag_testing.flow_runner.executor import is_safe_http_url

    if not url:
        return "Base URL is required."
    if len(url) > _MAX_URL_LEN:
        return "Base URL is too long."
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "Base URL is not a valid URL."
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "Base URL must start with http:// or https://."
    if parts.username or parts.password:
        return "Base URL must not contain credentials."
    if is_safe_http_url(url):
        return None
    host = parts.hostname.lower()
    if host in ("metadata", "metadata.google.internal"):
        return "That address is not allowed."
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (ip.is_link_local or ip.is_multicast or ip.is_unspecified):
        return "That address is not allowed."
    from app.auth.superadmin_cache import is_superadmin_cached

    if await is_superadmin_cached(uid):
        return None
    return "Local and private-network addresses can only be used by an instance admin."


# ---- pages --------------------------------------------------------------


@router.get("/ask")
async def ask_redirect(request: Request):
    return RedirectResponse("/chat", status_code=302)


@router.get("/chat")
async def chat_page(request: Request):
    from app.branding import chat_enabled

    if not chat_enabled():
        return RedirectResponse("/home", status_code=302)
    from app.api.google_oauth_routes import _load_user_view, _resolve_user_ctx

    user_ctx = await _resolve_user_ctx(request)
    if not user_ctx:
        return RedirectResponse("/signin?next=/chat", status_code=302)
    user_view = await _load_user_view(user_ctx)
    info = await get_config_info(uuid.UUID(user_ctx.user_id))
    return render(
        request,
        "chat.html",
        {"page_title": "Chat", "active": "chat", "user": user_view, "chat_configured": info is not None},
    )


@router.get("/settings/ai")
async def ai_settings(request: Request):
    from app.branding import chat_enabled

    if not chat_enabled():
        return RedirectResponse("/settings", status_code=302)
    uid = get_uid_from_request(request)
    if not uid:
        return RedirectResponse("/signin?next=/settings/ai", status_code=302)
    from app.api.google_oauth_routes import _load_user_view, _resolve_user_ctx

    user_ctx = await _resolve_user_ctx(request)
    user_view = await _load_user_view(user_ctx) if user_ctx else None
    info = await get_config_info(uuid.UUID(uid))
    return render(
        request,
        "settings/ai.html",
        {"user": user_view, "chat_config": asdict(info) if info else None, "presets": BASE_URL_PRESETS},
    )


# ---- chat stream --------------------------------------------------------


@router.post("/api/chat/stream")
async def chat_stream(request: Request):
    _require_chat()
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "auth"}, status_code=401)
    from app.api.project_routes import ensure_active_project

    project_id = await ensure_active_project(request, uid)
    if not project_id:
        return JSONResponse(
            {"error": "no_project", "message": "Create or select a project first."}, status_code=400
        )

    body = await _json_body(request)
    user_text = str(body.get("message") or "").strip()
    if not user_text:
        return JSONResponse({"error": "empty", "message": "Empty message."}, status_code=400)
    if len(user_text) > _MAX_MESSAGE_LEN:
        return JSONResponse({"error": "too_long", "message": "Message is too long."}, status_code=400)

    pid = uuid.UUID(project_id)
    user_uuid = uuid.UUID(uid)

    config = await get_config(user_uuid)
    if config is None:
        return JSONResponse(
            {"error": "no_key", "message": "Set up a model in Settings → AI first."},
            status_code=400,
        )
    url_err = await base_url_error(config.base_url, uid)
    if url_err:
        return JSONResponse({"error": "bad_config", "message": url_err}, status_code=400)

    new_title: str | None = None
    conv_id = body.get("conversation_id")
    if conv_id:
        conv_uuid = _parse_uuid(conv_id)
        conv = await _service.get(conv_uuid) if conv_uuid else None
        # Continue only inside the conversation's own project — never mix
        # another project's history (and tool results) into this context.
        if conv is None or str(conv.user_id) != uid or str(conv.project_id) != str(pid):
            return JSONResponse({"error": "not_found"}, status_code=404)
    else:
        raw = " ".join(user_text.split())
        new_title = raw[:60] + ("…" if len(raw) > 60 else "")
        conv = await _service.create(project_id=pid, user_id=user_uuid, model=config.model, title=new_title)

    history = window_history(await _service.load_history(conv.id))

    from app.auth.connection_gate import member_role
    from app.auth.permissions import resolve_effective_permissions

    eff = await resolve_effective_permissions(uid, project_id)
    bridge = ChatToolBridge(user_id=uid, project_id=project_id, eff=eff)
    specs = bridge.tool_specs()
    project_name = getattr(request.state, "active_project_name", None) or "your project"
    role = await member_role(uid, project_id) or "member"
    prompt_args = {"project_name": project_name, "connected": _tool_families(specs), "role": role}

    harness = Harness(
        HarnessDeps(
            provider=OpenAIProvider(config.api_key, base_url=config.base_url),
            bridge=bridge,
            service=_service,
            conversation_id=conv.id,
            model=config.model,
            system=build_system_prompt(**prompt_args),
            system_without_tools=build_system_prompt(**prompt_args, tools_enabled=False),
            history=history,
        )
    )
    user_message = LLMMessage(role="user", content=[TextBlock(text=user_text)])

    async def event_stream():
        first: dict[str, Any] = {
            "type": "conversation",
            "conversation_id": str(conv.id),
            "model": config.model,
        }
        if new_title:
            first["title"] = new_title
        yield _sse_frame(first)
        try:
            async for ev in harness.run(user_message):
                yield _sse_frame(_event_to_payload(ev))
        except Exception as exc:  # never leak a stack trace into the stream
            yield _sse_frame({"type": "error", "error": f"Chat failed ({type(exc).__name__})."})
        finally:
            yield _sse_frame({"type": "done"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _event_to_payload(ev: StreamEvent) -> dict[str, Any]:
    payload = {k: v for k, v in asdict(ev).items() if v is not None}
    if ev.stop_reason is not None:
        payload["stop_reason"] = ev.stop_reason.value
    return payload


_TOOL_FAMILIES: list[tuple[str, str]] = [
    ("analytics_read", "Analytics"),
    ("tagmanager_read", "Tag Manager"),
    ("marketing_read", "Advertising"),
    ("warehouse_read", "Warehouse"),
    ("seo_read", "Search / SEO"),
    ("audience_read", "Audiences"),
    ("run_audit", "Audits"),
]


def _tool_families(specs: list[ToolSpec]) -> list[str]:
    names = {s.name for s in specs}
    return [label for tool, label in _TOOL_FAMILIES if tool in names]


# ---- conversations ------------------------------------------------------


async def _owned_conversation(request: Request, conversation_id: str):
    """The caller's own conversation in a project they still belong to.
    Returns (conversation, error_response)."""
    uid = get_uid_from_request(request)
    if not uid:
        return None, JSONResponse({"error": "auth"}, status_code=401)
    conv_uuid = _parse_uuid(conversation_id)
    conv = await _service.get(conv_uuid) if conv_uuid else None
    if conv is None or str(conv.user_id) != uid:
        return None, JSONResponse({"error": "not_found"}, status_code=404)
    from app.auth.connection_gate import member_role

    if await member_role(uid, str(conv.project_id)) is None:
        return None, JSONResponse({"error": "not_found"}, status_code=404)
    return conv, None


@router.get("/api/chat/conversations")
async def list_conversations(request: Request):
    _require_chat()
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "auth"}, status_code=401)
    from app.api.project_routes import ensure_active_project

    project_id = await ensure_active_project(request, uid)
    if not project_id:
        return JSONResponse({"conversations": []})
    q = (request.query_params.get("q") or "")[:200]
    convs = await _service.list_for(project_id=uuid.UUID(project_id), user_id=uuid.UUID(uid), query=q)
    return JSONResponse(
        {
            "conversations": [
                {
                    "id": str(c.id),
                    "title": c.title or "New chat",
                    "last_message_at": c.last_message_at.isoformat() if c.last_message_at else None,
                }
                for c in convs
            ]
        }
    )


@router.get("/api/chat/conversations/{conversation_id}")
async def get_conversation(request: Request, conversation_id: str):
    _require_chat()
    conv, err = await _owned_conversation(request, conversation_id)
    if err:
        return err
    rows = await _service.load_messages(conv.id)
    return JSONResponse(
        {
            "id": str(conv.id),
            "title": conv.title,
            "model": conv.model,
            "messages": [
                {
                    "id": str(msg_id),
                    "role": m.role,
                    "content": blocks_to_json(m.content),
                    **({"token_usage": usage} if usage is not None else {}),
                }
                for msg_id, m, usage in rows
            ],
        }
    )


@router.patch("/api/chat/conversations/{conversation_id}")
async def rename_conversation(request: Request, conversation_id: str):
    _require_chat()
    conv, err = await _owned_conversation(request, conversation_id)
    if err:
        return err
    body = await _json_body(request)
    title = " ".join(str(body.get("title") or "").split())
    if not title:
        return JSONResponse({"error": "Title is required."}, status_code=400)
    await _service.set_title(conv.id, title)
    return JSONResponse({"ok": True, "title": title[:200]})


@router.delete("/api/chat/conversations/{conversation_id}")
async def delete_conversation(request: Request, conversation_id: str):
    _require_chat()
    conv, err = await _owned_conversation(request, conversation_id)
    if err:
        return err
    await _service.delete(conv.id)
    return JSONResponse({"ok": True})


# ---- personal model config ---------------------------------------------


@router.get("/api/chat/config")
async def get_chat_config(request: Request):
    _require_chat()
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "auth"}, status_code=401)
    info = await get_config_info(uuid.UUID(uid))
    return JSONResponse({"config": asdict(info) if info else None, "presets": BASE_URL_PRESETS})


def _clean_fields(body: dict[str, Any]) -> tuple[str, str, str | None, str | None]:
    """(base_url, model, api_key, error)."""
    base_url = normalize_base_url(str(body.get("base_url") or ""))
    model = str(body.get("model") or "").strip()
    api_key = str(body.get("api_key") or "").strip() or None
    if not model:
        return base_url, model, api_key, "Model is required."
    if len(model) > _MAX_MODEL_LEN:
        return base_url, model, api_key, "Model name is too long."
    if api_key and len(api_key) > _MAX_KEY_LEN:
        return base_url, model, api_key, "API key is too long."
    return base_url, model, api_key, None


@router.put("/api/chat/config")
async def put_chat_config(request: Request):
    _require_chat()
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "auth"}, status_code=401)
    base_url, model, api_key, err = _clean_fields(await _json_body(request))
    err = err or await base_url_error(base_url, uid)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    await save_config(user_id=uuid.UUID(uid), base_url=base_url, model=model, api_key=api_key)
    info = await get_config_info(uuid.UUID(uid))
    return JSONResponse({"ok": True, "config": asdict(info) if info else None})


@router.delete("/api/chat/config")
async def delete_chat_config(request: Request):
    _require_chat()
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "auth"}, status_code=401)
    await delete_config(uuid.UUID(uid))
    return JSONResponse({"ok": True})


@router.post("/api/chat/config/test")
async def test_chat_config(request: Request):
    """Probe the endpoint with one tiny non-streaming chat call.

    With no API key in the body the stored key is used — but only when the
    base URL matches the saved one, so a saved secret never goes elsewhere.
    """
    _require_chat()
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "auth"}, status_code=401)
    base_url, model, api_key, err = _clean_fields(await _json_body(request))
    err = err or await base_url_error(base_url, uid)
    if err:
        return JSONResponse({"ok": False, "error": err})
    api_key = await resolve_api_key(uuid.UUID(uid), base_url, api_key)
    provider = OpenAIProvider(api_key, base_url=base_url, timeout=_TEST_TIMEOUT_S)
    try:
        await asyncio.wait_for(
            provider.complete(model=model, prompt="Reply with: ok"), timeout=_TEST_TIMEOUT_S
        )
    except TimeoutError:
        return JSONResponse({"ok": False, "error": f"No response within {int(_TEST_TIMEOUT_S)} seconds."})
    except ProviderError as exc:
        return JSONResponse({"ok": False, "error": str(exc)})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"Could not reach the endpoint ({type(exc).__name__})."})
    return JSONResponse({"ok": True})
