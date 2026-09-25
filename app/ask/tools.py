"""Read-only bridge between the chat harness and the in-process MCP tools.

The chat may only *read*: a fixed allowlist of tools is exposed, and every
call is rejected before execution when ``is_write_call`` says it would change
something (e.g. ``run_audit`` action ``tag_save_custom_rule``). The caller's
RBAC permissions are enforced on top, and dispatch goes through the
instrumented ``tool_manager.call_tool`` path (RBAC backstop, circuit breaker,
audit trail).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from app import app_state
from app.ask.providers.base import ToolSpec
from app.auth.mcp_session_manager import build_project_context, build_user_context

# The only tools the chat may call.
READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "analytics_read",
        "tagmanager_read",
        "marketing_read",
        "warehouse_read",
        "seo_read",
        "audience_read",
        "get_knowledge",
        "get_session_context",
        "list_my_projects",
        "run_audit",
        "run_analysis",
    }
)

# Source tag written to the tool-call audit trail for chat-initiated calls.
TOOL_CALL_SOURCE = "chat"

_TOOL_TIMEOUT_S = 60.0

WRITE_REJECTED_MESSAGE = (
    "This action would change something, and the in-app chat is read-only. "
    "Tell the user they can make this change from an MCP client (Claude, ChatGPT, "
    "Cursor, ...) connected to this Fluxito instance."
)


def rejection_reason(name: str, params: dict[str, Any], eff: Any = None) -> str | None:
    """Why a tool call is not allowed from the chat, or None when it is.

    Three gates, all server-side:
    1. the tool is on the read-only allowlist;
    2. the call is not a write (``app.auth.permissions.is_write_call``);
    3. the caller's RBAC permissions allow it.
    """
    from app.auth.permissions import ALWAYS_ON_TOOLS, is_write_call

    if name not in READ_ONLY_TOOLS:
        return f"Tool '{name}' is not available in the in-app chat."
    action = params.get("action") if isinstance(params, dict) else None
    if not isinstance(action, str):
        action = None
    if is_write_call(name, action):
        return WRITE_REJECTED_MESSAGE
    if eff is not None and name not in ALWAYS_ON_TOOLS and not eff.allows_tool(name, action=action):
        return f"You don't have permission to use '{name}' in this project."
    return None


class _ChatToolContext:
    """Set the MCP ContextVars for an in-process tool call made by the chat."""

    __slots__ = ("_project_id", "_tokens", "_user_id")

    def __init__(self, user_id: str, project_id: str) -> None:
        self._user_id = user_id
        self._project_id = project_id
        self._tokens: list[tuple[str, Any]] = []

    async def __aenter__(self) -> _ChatToolContext:
        user_ctx = await build_user_context(self._user_id)
        project_ctx = await build_project_context(self._project_id, self._user_id)
        self._tokens = [
            ("user", app_state.current_user_ctx.set(user_ctx)),
            ("project", app_state.current_project_ctx.set(project_ctx)),
            ("source", app_state.tool_call_source_ctx.set(TOOL_CALL_SOURCE)),
            ("client", app_state.current_client_name_ctx.set(TOOL_CALL_SOURCE)),
        ]
        return self

    async def __aexit__(self, *exc: Any) -> None:
        vars = {
            "user": app_state.current_user_ctx,
            "project": app_state.current_project_ctx,
            "source": app_state.tool_call_source_ctx,
            "client": app_state.current_client_name_ctx,
        }
        for kind, token in reversed(self._tokens):
            try:
                vars[kind].reset(token)
            except ValueError:
                vars[kind].set(None)
        self._tokens = []


async def run_mcp_tool(
    *, user_id: str, project_id: str, name: str, params: dict[str, Any]
) -> tuple[str, bool]:
    """Run a registered MCP tool in-process under the chat's user/project context.

    Dispatches through ``tool_manager.call_tool`` — the instrumented entry
    point where the per-project RBAC backstop, circuit breaker and audit trail
    live (calling ``tool.run()`` directly would bypass all of it).

    Returns ``(content_json_str, is_error)``.
    """
    from app.main import mcp_server

    tm = mcp_server._tool_manager
    if tm.get_tool(name) is None:
        return json.dumps({"error": f"Tool '{name}' is not registered."}), True

    t0 = time.monotonic()
    try:
        async with _ChatToolContext(user_id, project_id):
            try:
                raw = await asyncio.wait_for(tm.call_tool(name, dict(params)), timeout=_TOOL_TIMEOUT_S)
                content = json.dumps(raw, default=str)
                await _audit(name, params, content, raw, "success", t0)
                return content, False
            except TimeoutError:
                err_text = json.dumps({"error": f"Tool '{name}' timed out."})
                await _audit(name, params, err_text, None, "error", t0)
                return err_text, True
            except Exception as exc:  # surface tool errors to the model, don't crash the loop
                err_text = json.dumps({"error": f"{type(exc).__name__}: {exc}"})
                await _audit(name, params, err_text, None, "error", t0)
                return err_text, True
    except Exception as ctx_exc:  # context setup failed
        return json.dumps({"error": f"{type(ctx_exc).__name__}: {ctx_exc}"}), True


async def _audit(
    name: str, params: dict[str, Any], raw_text: str, parsed: Any, status: str, t0: float
) -> None:
    try:
        from app.tools.registry import _write_audit_row

        await _write_audit_row(
            tool_name=name,
            arguments=params,
            raw_text=raw_text,
            parsed=parsed,
            status=status,
            source_client=TOOL_CALL_SOURCE,
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
    except Exception:
        pass


class ChatToolBridge:
    """Exposes the read-only tool surface to the harness."""

    def __init__(self, user_id: str, project_id: str, eff: Any = None) -> None:
        self.user_id = user_id
        self.project_id = project_id
        # Caller's resolved RBAC permissions; gates both the exposed tool
        # surface and every dispatch.
        self._eff = eff

    def tool_specs(self) -> list[ToolSpec]:
        """ToolSpecs for every allowlisted, registered tool the caller may use."""
        from app.main import mcp_server

        tm = mcp_server._tool_manager
        specs: list[ToolSpec] = []
        for name in sorted(READ_ONLY_TOOLS):
            tool = tm.get_tool(name)
            if tool is None:
                continue
            if rejection_reason(name, {}, self._eff) is not None:
                continue
            specs.append(
                ToolSpec(
                    name=tool.name,
                    description=(tool.description or "").strip(),
                    input_schema=tool.parameters or {"type": "object", "properties": {}},
                )
            )
        return specs

    async def dispatch(self, name: str, params: dict[str, Any]) -> tuple[str, bool]:
        """Run a tool in-process. Returns (content_json_str, is_error)."""
        reason = rejection_reason(name, params, self._eff)
        if reason is not None:
            return json.dumps({"error": reason}), True
        return await run_mcp_tool(user_id=self.user_id, project_id=self.project_id, name=name, params=params)
