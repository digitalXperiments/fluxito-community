"""
Tools Registry — Always-on, flat registration.

We register ALL tools unconditionally at session-start. Connection gating
happens inside each tool handler at call-time, returning a structured error
response with a connect URL when needed. No billing gating is applied here.

This means:
  - The AI always sees the full tool list (better discovery, better suggestions).
  - No session re-registration needed when the user adds a new connection.
  - A clean SSE reconnect (no sign-in required) picks up the new connection.

Tool groups:
  analytics_read / analytics_audit / analytics_write   ← GA4 + Amplitude + Adobe Analytics
  tagmanager_read / tagmanager_audit / tagmanager_write ← GTM + Adobe Launch
  warehouse_read / warehouse_query / warehouse_audit    ← BigQuery / Redshift / Snowflake
  marketing_read / marketing_write / marketing_audit    ← Google Ads / Meta / TikTok / Snap

The call-interception hook installed by _install_tool_hook handles:
  - Circuit breaker (prevents hot broken tools from saturating workers)
  - Per-tool timeouts (graceful degradation instead of hanging)
  - Structured audit trail (every call logged for traceability)
  - Activity log + in-memory request stats
"""

import json
import logging
import re
import time
import uuid as _uuid
from typing import Any

import app.app_state as app_state

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RBAC helpers — module-level so tests can monkeypatch _resolve_perms_for_call
# ---------------------------------------------------------------------------


async def _resolve_perms_for_call(user_id: str, project_id: str):
    from app.auth.permissions import resolve_effective_permissions

    return await resolve_effective_permissions(user_id, project_id)


async def _tool_permitted_for_call(
    name: str, arguments: dict, user_id: str | None, project_id: str | None
) -> bool:
    """True if resolved permissions allow this call. Missing ctx -> allow (RBAC only
    restricts when we know who/where; auth + project errors are handled elsewhere)."""
    from app.auth.permissions import ALWAYS_ON_TOOLS, is_write_call

    if name in ALWAYS_ON_TOOLS:
        return True
    action = (arguments or {}).get("action") if isinstance(arguments, dict) else None
    if not token_allows_write() and is_write_call(name, action):
        return False  # read-only MCP token
    if not user_id or not project_id:
        return True
    eff = await _resolve_perms_for_call(user_id, project_id)
    return eff.allows_tool(name, action=action)


def token_allows_write() -> bool:
    """False when the current call comes from an MCP token granted read-only access."""
    import app.app_state as _state

    scopes = _state.current_token_scopes_ctx.get()
    return scopes is None or "write" in scopes


def _call_ctx() -> tuple[str | None, str | None]:
    """(user_id, project_id) of the tool call in progress."""
    import app.app_state as _state

    uctx = _state.current_user_ctx.get()
    pctx = _state.current_project_ctx.get()
    uid = getattr(uctx, "user_id", None) if uctx else None
    pid = None
    if pctx is not None:
        pid = str(getattr(pctx, "project_id", None) or getattr(pctx, "id", None) or "") or None
    return (str(uid) if uid else None), pid


async def inner_tool_permitted(name: str, arguments: dict | None) -> bool:
    """Per-tool RBAC for calls made *from inside* another tool.

    ``run_script`` and ``generic_tool_*`` invoke other tools directly, past the
    MCP call hook; without this a member could reach any write tool through
    them. Same rule as a direct call; no project → deny.
    """
    uid, pid = _call_ctx()
    if uid and not pid:
        return False
    import app.app_state as _state
    from app.auth.resource_scope import resource_violation

    if resource_violation(name, arguments or {}, _state.current_project_ctx.get()):
        return False
    return await _tool_permitted_for_call(name, arguments or {}, uid, pid)


async def caller_has_full_access() -> bool:
    """True for owners/admins (or RBAC off) — needed for raw connector calls."""
    uid, pid = _call_ctx()
    if not token_allows_write():
        return False  # raw connector methods may write
    if not uid:
        return True
    if not pid:
        return False
    eff = await _resolve_perms_for_call(uid, pid)
    return bool(getattr(eff, "full", False))


def _filter_tool_names(names, eff) -> list:
    """Return only the tool names the effective permissions allow. None/full -> all."""
    if eff is None or getattr(eff, "full", False):
        return list(names)
    return [n for n in names if eff.allows_tool(n)]


# Populated by unified.rewire_unified_surface — holds a reference to the
# FastMCP ToolManager so the unified dispatcher tools can reach the
# preserved legacy tool objects.
_tool_manager_ref: dict = {"mgr": None}


# Tools we never audit — noisy meta/diagnostic calls that would flood the log
# without giving the user any "where did this number come from" value.
_AUDIT_SKIP_TOOLS = frozenset(
    (
        "get_session_context",
        "get_knowledge",
        "list_my_projects",
        "set_active_project",
    )
)

# Write-style tools (mirror of _WRITE_TOOLS in quota.py). Kept local to avoid
# a circular import.
_AUDIT_WRITE_TOOLS = frozenset(
    (
        "analytics_write",
        "marketing_write",
        "tagmanager_write",
        "seo_write",
        "generic_tool_write",
        "audience_write",
    )
)

# Well-known list-shaped response keys (optimized as tuple for fast lookup)
_RESPONSE_LIST_KEYS = (
    "rows",
    "results",
    "items",
    "data",
    "campaigns",
    "ad_groups",
    "adgroups",
    "ads",
    "keywords",
    "properties",
    "containers",
    "accounts",
    "segments",
    "tags",
    "triggers",
    "variables",
    "workspaces",
    "events",
    "conversions",
    "audiences",
    "placements",
    "pages",
)


def _summarize_response(data: Any) -> str:
    """Build a short human-readable one-liner describing a tool response."""
    try:
        if isinstance(data, dict):
            if data.get("error"):
                return f"Error: {str(data.get('error'))[:160]}"
            # Well-known list-shaped keys — pick the first that's a list
            for key in _RESPONSE_LIST_KEYS:
                val = data.get(key)
                if isinstance(val, list):
                    return f"{len(val)} {key.replace('_', ' ')} returned"
            if "row_count" in data:
                return f"{data['row_count']} rows returned"
            if "count" in data:
                return f"{data['count']} items"
            # Any list-valued top-level key
            for k, v in data.items():
                if isinstance(v, list):
                    return f"{len(v)} {k.replace('_', ' ')} returned"
            # Fallback — list the top-level keys so users can still see shape
            keys = [k for k in data.keys() if not k.startswith("_")][:4]
            if keys:
                return "fields: " + ", ".join(keys)
            return "empty response"
        if isinstance(data, list):
            return f"{len(data)} items returned"
        if isinstance(data, str):
            return (data[:160] + "…") if len(data) > 160 else data
    except Exception:
        pass
    return "response returned"


# Keys whose VALUE is a secret and must never be persisted to the audit trail.
_SECRET_KEY_RE = re.compile(
    r"(token|password|secret|api[_-]?key|private[_-]?key|credential|authorization|service_account)",
    re.IGNORECASE,
)
# Data Manager member payloads may contain hashed or raw customer identifiers.
# Keep them out of the persistent MCP audit trail just like credentials.
_AUDIENCE_PII_KEY_RE = re.compile(
    r"^(audience[_-]?members?|user[_-]?identifiers?|partner[_-]?provided[_-]?ids?|"
    r"mobile[_-]?ids?|google[_-]?user[_-]?ids?|pair[_-]?ids?|ppids?)$",
    re.IGNORECASE,
)
_REDACTED = "***redacted***"


def _redact_secrets(obj: Any, _depth: int = 0) -> Any:
    """Deep-copy ``obj`` masking values whose key looks secret (tokens, keys, …).

    Used only for the audit-trail copy — the live tool response is untouched, so a
    tool that must return a secret to the caller (e.g. a freshly issued API token)
    still works while the secret stays out of the persisted log (FINDINGS S1 #11).
    """
    if _depth > 8:
        return obj
    if isinstance(obj, dict):
        out: dict[Any, Any] = {}
        for k, v in obj.items():
            if isinstance(k, str) and (_SECRET_KEY_RE.search(k) or _AUDIENCE_PII_KEY_RE.search(k)):
                out[k] = _REDACTED
            else:
                out[k] = _redact_secrets(v, _depth + 1)
        return out
    if isinstance(obj, list):
        return [_redact_secrets(v, _depth + 1) for v in obj]
    return obj


async def _write_audit_row(
    tool_name: str,
    arguments: dict[str, Any] | None,
    raw_text: str | None,
    parsed: Any,
    status: str,
    source_client: str | None,
    duration_ms: int,
) -> None:
    """
    Persist a single audit row. Swallows all errors — auditing must never
    break a tool call.
    """
    # Redact secrets before persisting. A tool may legitimately RETURN a secret
    # to the caller (e.g. a freshly issued API token) — the live
    # response keeps it, but it must never be written to the audit trail
    # (FINDINGS S1 #11). Redaction is local to the logged copies only; the
    # persisted `response_preview` is rebuilt from this redacted `parsed`.
    arguments = _redact_secrets(arguments)
    parsed = _redact_secrets(parsed)

    try:
        user_ctx = app_state.current_user_ctx.get()
    except LookupError:
        return
    if not user_ctx:
        return

    user_id = user_ctx.get("user_id") if isinstance(user_ctx, dict) else getattr(user_ctx, "user_id", None)
    if not user_id:
        return
    try:
        user_uuid = _uuid.UUID(str(user_id))
    except (ValueError, TypeError):
        return

    # Get active project_id if available
    project_uuid = None
    try:
        project_ctx = app_state.current_project_ctx.get()
        if project_ctx:
            project_uuid = _uuid.UUID(str(project_ctx.project_id))
    except (LookupError, ValueError, TypeError):
        pass

    from app.models.audit import ToolCallAudit

    # Prefer a pretty-printed JSON dump of the parsed response so the audit
    # UI can show something readable. Fall back to the raw text otherwise.
    preview = ""
    if isinstance(parsed, (dict, list)):
        try:
            preview = json.dumps(parsed, indent=2, default=str)
        except (TypeError, ValueError):
            preview = raw_text or ""
    else:
        preview = raw_text or ""

    truncated = False
    if preview and len(preview) > ToolCallAudit.MAX_RESPONSE_CHARS:
        preview = preview[: ToolCallAudit.MAX_RESPONSE_CHARS]
        truncated = True

    summary = _summarize_response(parsed)
    error_message = None
    if status in ("error", "denied") and isinstance(parsed, dict):
        error_message = str(parsed.get("error") or parsed.get("message") or "")[:1000] or None

    # Detect platform from arguments (best-effort)
    platform = None
    if isinstance(arguments, dict):
        platform = arguments.get("platform") or arguments.get("provider")
        if not isinstance(platform, str):
            platform = None

    try:
        db_factory = app_state.db_session_factory
        async with db_factory() as db:
            row = ToolCallAudit(
                project_id=project_uuid,
                user_id=user_uuid,
                tool_name=tool_name,
                platform=platform,
                source_client=source_client,
                status=status,
                is_write=tool_name in _AUDIT_WRITE_TOOLS,
                duration_ms=duration_ms,
                arguments=arguments if isinstance(arguments, dict) else None,
                response_summary=summary,
                response_preview=preview or None,
                response_truncated=truncated,
                error_message=error_message,
            )
            db.add(row)
            await db.commit()
    except Exception as e:
        logger.warning(f"audit write failed for tool={tool_name}: {e}")


def _install_tool_hook(mcp_server):
    """
    Monkey-patches the FastMCP tool manager's call_tool method to add
    cross-cutting concerns: circuit breaking, per-tool timeouts, structured
    audit trail, and activity logging.

    This approach avoids wrapping individual tool functions (which
    breaks MCP's signature introspection of ForwardRef annotations).
    Instead, it intercepts the call BEFORE and result AFTER the tool runs.
    """
    tool_manager = mcp_server._tool_manager
    _original_call = tool_manager.call_tool

    async def _instrumented_call_raw(name, arguments, *args, **kwargs):
        # --- Reliability layer ─────────────────────────────────────────────
        # Circuit breaker keyed by tool name — prevents a hot broken tool
        # from saturating the worker. Allows one probe after cooldown.
        from app.tools.reliability import (
            breaker,
            log_request,
            timeout_for,
        )

        breaker_key = name
        allowed, reason = breaker.allow(breaker_key)
        if not allowed:
            logger.warning(f"circuit-open rejecting call to {name}: {reason}")
            result = {
                "error": True,
                "error_type": "circuit_open",
                "message": reason,
                "details": {
                    "retry_hint": (
                        "The tool has been failing repeatedly — the circuit "
                        "breaker is holding new calls briefly to let it recover. "
                        "Retry in ~60s or try a different input."
                    ),
                },
            }
            try:
                log_request(name, arguments, result, "denied", 0, error_type="circuit_open")
            except Exception:
                pass
            return result

        # Per-tool timeout — gracefully degrade instead of hanging forever.
        _tool_timeout = timeout_for(name)

        # Call the original tool — forward any additional args/kwargs
        # (e.g. `context`) that newer FastMCP versions pass through.
        #
        # IMPORTANT: tool_manager.call_tool returns the RAW Python value
        # the tool function returned (dict/list/str/etc). It is NOT a
        # CallToolResult with a `.content` attribute — that wrapping happens
        # later in FastMCP's server layer via `_convert_to_content`. So we
        # treat `result` as the raw tool return value here.
        #
        # CRITICAL: Wrap in try/except to prevent unhandled exceptions from
        # propagating up to the SSE transport layer, which would permanently
        # kill the session connection. Any tool crash is caught here and
        # returned as a structured error response instead.
        _audit_start = time.perf_counter()
        import asyncio as _asyncio

        # Resolve the active project for THIS call, in this task's context,
        # from the durable store (Redis) or an explicit project_id/project
        # argument. The ContextVar set by set_active_project in a *sibling*
        # call never reaches here (each tool runs in its own wait_for task),
        # so we re-resolve per call — see ensure_call_project_ctx.
        from app.auth.mcp_session_manager import ensure_call_project_ctx

        try:
            _proj_token = await ensure_call_project_ctx(name, arguments)
        except Exception:
            _proj_token = None

        # ── RBAC backstop: deny tools the caller's role does not grant ──────
        try:
            import app.app_state as _state

            _uctx = _state.current_user_ctx.get()
            _pctx = _state.current_project_ctx.get()
            _uid = getattr(_uctx, "user_id", None) if _uctx else None
            _pid = None
            if _pctx is not None:
                _pid = str(getattr(_pctx, "project_id", None) or getattr(_pctx, "id", None) or "") or None
            if _uid and not _pid:
                from app.auth.permissions import ALWAYS_ON_TOOLS

                if name not in ALWAYS_ON_TOOLS:
                    # No project → no role to check against. Never fall back to
                    # "allow everything" with the user's own cross-project creds.
                    from app.auth.mcp_session_manager import no_active_project_response

                    return no_active_project_response()
            from app.auth.permissions import is_write_call
            from app.auth.resource_scope import denied_response, resource_violation

            _why = resource_violation(name, arguments, _pctx)
            if _why:
                return denied_response(name, _why)
            _action = (arguments or {}).get("action") if isinstance(arguments, dict) else None
            if not token_allows_write() and is_write_call(name, _action):
                return {
                    "error": True,
                    "error_type": "read_only_access",
                    "message": (
                        f"'{name}' makes changes, but this AI client was connected with read-only access. "
                        "Reconnect it and leave 'Read-only access' unticked to allow changes."
                    ),
                    "tool": name,
                }
            if not await _tool_permitted_for_call(name, arguments, _uid, _pid):
                return {
                    "error": True,
                    "error_type": "permission_denied",
                    "message": f"Your role does not grant access to '{name}'.",
                    "tool": name,
                }
        except Exception as _exc:
            # Fail closed: if permissions can't be resolved, don't run the tool.
            logger.warning("RBAC backstop check failed for %s: %s", name, _exc)
            return {
                "error": True,
                "error_type": "permission_check_failed",
                "message": "Couldn't verify your permissions for this tool. Please try again.",
                "tool": name,
            }

        try:
            try:
                result = await _asyncio.wait_for(
                    _original_call(name, arguments, *args, **kwargs),
                    timeout=_tool_timeout,
                )
            finally:
                # Reset before post-processing so the resolved project never
                # leaks into the next tool call sharing this task/context.
                if _proj_token is not None:
                    try:
                        app_state.current_project_ctx.reset(_proj_token)
                    except ValueError:
                        app_state.current_project_ctx.set(None)
        except TimeoutError:
            _audit_duration_ms = int((time.perf_counter() - _audit_start) * 1000)
            logger.warning(f"tool '{name}' timed out after {_tool_timeout}s (args={arguments})")
            breaker.record_failure(breaker_key)
            result = {
                "error": True,
                "error_type": "timeout",
                "message": (
                    f"Operation exceeded {_tool_timeout}s and was cancelled. "
                    "Try a smaller date range, lower limit, or narrower filter."
                ),
                "details": {"timeout_seconds": _tool_timeout},
            }
            try:
                log_request(name, arguments, result, "timeout", _audit_duration_ms, error_type="timeout")
            except Exception:
                pass
            if name not in _AUDIT_SKIP_TOOLS:
                try:
                    await _write_audit_row(
                        tool_name=name,
                        arguments=arguments if isinstance(arguments, dict) else None,
                        raw_text=json.dumps(result, default=str),
                        parsed=result,
                        status="error",
                        source_client=None,
                        duration_ms=_audit_duration_ms,
                    )
                except Exception:
                    pass
            return result
        except Exception as exc:
            _audit_duration_ms = int((time.perf_counter() - _audit_start) * 1000)

            # ConnectorError = user-friendly message; other exceptions = generic
            from app.connectors.errors import ConnectorError

            if isinstance(exc, ConnectorError):
                logger.warning(
                    "Connector error in tool '%s' (%s): %s",
                    name,
                    exc.platform,
                    exc,
                )
                result = {
                    "error": True,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "details": {"platform": exc.platform},
                }
            else:
                logger.error(
                    f"Unhandled exception in tool '{name}' (args={arguments}): {exc}",
                    exc_info=True,
                )
                result = {
                    "error": True,
                    "error_type": "server_error",
                    "message": f"Internal error in {name}: {exc!s}",
                    "details": {},
                }
            # Still audit the failure
            app_state.tool_call_status_ctx.set("error")
            try:
                source_client = app_state.current_client_name_ctx.get()
                app_state.tool_call_source_ctx.set(source_client)
            except LookupError:
                app_state.tool_call_source_ctx.set(None)
            if name not in _AUDIT_SKIP_TOOLS:
                try:
                    await _write_audit_row(
                        tool_name=name,
                        arguments=arguments if isinstance(arguments, dict) else None,
                        raw_text=json.dumps(result, default=str),
                        parsed=result,
                        status="error",
                        source_client=app_state.tool_call_source_ctx.get(None),
                        duration_ms=_audit_duration_ms,
                    )
                except Exception:
                    pass
            return result
        _audit_duration_ms = int((time.perf_counter() - _audit_start) * 1000)

        # Detect result status for activity log + capture a serialized
        # response for the audit trail.
        call_status = "success"
        _audit_parsed = result if isinstance(result, (dict, list)) else None
        _audit_raw_text = None
        try:
            if isinstance(result, (dict, list)):
                _audit_raw_text = json.dumps(result, default=str)
            elif isinstance(result, str):
                _audit_raw_text = result
                try:
                    _audit_parsed = json.loads(result)
                except (json.JSONDecodeError, TypeError):
                    _audit_parsed = None
            elif result is not None:
                _audit_raw_text = str(result)
        except (TypeError, ValueError):
            _audit_raw_text = None

        if isinstance(_audit_parsed, dict) and _audit_parsed.get("error"):
            error_type = _audit_parsed.get("error_type", "")
            call_status = (
                "denied"
                if error_type in ("scope_denied", "quota_exceeded", "connection_missing", "permission_denied")
                else "error"
            )

        # --- Circuit breaker feedback ---
        # "Real" server-side errors increment the failure count. Tool-level
        # denials (quota, scope, missing connection) are expected and do NOT
        # count against the breaker — otherwise a user without a connection
        # would trip the breaker for everyone.
        try:
            if call_status == "success" or call_status == "denied":
                breaker.record_success(breaker_key)
            else:
                breaker.record_failure(breaker_key)
        except Exception:
            pass

        # --- Structured request log + in-memory stats ---
        try:
            _uid = None
            try:
                _u = app_state.current_user_ctx.get()
                _uid = _u.user_id if _u else None
            except LookupError:
                pass
            log_request(
                name,
                arguments,
                result,
                call_status,
                _audit_duration_ms,
                source_client=app_state.tool_call_source_ctx.get(None),
                user_id=_uid,
                error_type=(_audit_parsed.get("error_type") if isinstance(_audit_parsed, dict) else None),
            )
        except Exception as _e:
            logger.debug(f"log_request failed: {_e}")

        # Store call status for the quota logger to pick up
        app_state.tool_call_status_ctx.set(call_status)

        # Detect source client (e.g. Claude, ChatGPT) from the MCP auth middleware
        try:
            source_client = app_state.current_client_name_ctx.get()
            app_state.tool_call_source_ctx.set(source_client)
        except LookupError:
            app_state.tool_call_source_ctx.set(None)

        # ── Answer audit trail ────────────────────────────────────────────
        # Persist a full request/response record so users can click any AI
        # answer and trace it back to the exact tool call + parameters.
        if name not in _AUDIT_SKIP_TOOLS:
            try:
                await _write_audit_row(
                    tool_name=name,
                    arguments=arguments if isinstance(arguments, dict) else None,
                    raw_text=_audit_raw_text,
                    parsed=_audit_parsed,
                    status=call_status,
                    source_client=app_state.tool_call_source_ctx.get(None),
                    duration_ms=_audit_duration_ms,
                )
            except Exception as _e:
                logger.debug(f"audit hook failed: {_e}")

        return result

    async def _instrumented_call(name, arguments, *args, **kwargs):
        # FastMCP's ``call_tool`` invokes the tool manager with
        # ``convert_result=True`` and expects already-converted content
        # (a list of content blocks, or a ``(content, structured)`` tuple).
        #
        # We deliberately run the wrapped tool with ``convert_result=False``
        # so the instrumentation layer — most importantly the audit trail —
        # observes the RAW dict/list/str the tool returned (needed to derive
        # response summaries and the success/error/denied status). Only once
        # auditing is done do we convert exactly as FastMCP would, so the
        # value handed back to the SDK is byte-for-byte what it expects.
        _want_convert = kwargs.pop("convert_result", False)
        raw = await _instrumented_call_raw(name, arguments, *args, **kwargs)
        if not _want_convert:
            return raw
        tool = tool_manager.get_tool(name)
        if tool is None:
            return raw
        try:
            return tool.fn_metadata.convert_result(raw)
        except Exception:
            # Never let result conversion break a call — return the raw value
            # and let the lowlevel server fall back to generic serialization.
            return raw

    # Actually install the hook. Without this assignment the entire
    # instrumentation layer (audit trail, circuit breaker, per-tool timeout,
    # per-call active-project resolution, source-client capture) is dead code
    # and the Activity Log never receives a single tool-call record.
    tool_manager.call_tool = _instrumented_call

    # ── RBAC tool-list filter ─────────────────────────────────────────────────
    # FastMCP.list_tools is async, so we can await resolve_effective_permissions
    # here. Falls back to the unfiltered list on any error — the call-time
    # backstop remains the real security boundary.
    #
    # IMPORTANT: FastMCP._setup_handlers registers a DIRECT REFERENCE to the
    # original ``self.list_tools`` bound method into the low-level protocol
    # server (``self._mcp_server.list_tools()(self.list_tools)``). Reassigning
    # the ``mcp_server.list_tools`` attribute below therefore does NOT change
    # what the wire protocol calls — the over-HTTP tools/list would stay
    # unfiltered. So we ALSO re-register the filtered handler on the low-level
    # server. (The call_tool backstop works without this trick only because
    # FastMCP.call_tool delegates to ``self._tool_manager.call_tool``, which we
    # wrap directly above.)
    _original_list_tools = mcp_server.list_tools

    async def _filtered_list_tools(*args, **kwargs):
        unfiltered = await _original_list_tools(*args, **kwargs)
        try:
            import app.app_state as _state

            _uctx = _state.current_user_ctx.get(None)
            _pctx = _state.current_project_ctx.get(None)
            _uid = getattr(_uctx, "user_id", None) if _uctx else None
            _pid = None
            if _pctx is not None:
                _pid = str(getattr(_pctx, "project_id", None) or getattr(_pctx, "id", None) or "") or None
            if not _uid or not _pid:
                return unfiltered
            eff = await _resolve_perms_for_call(_uid, _pid)
            allowed = set(_filter_tool_names([t.name for t in unfiltered], eff))
            return [t for t in unfiltered if t.name in allowed]
        except Exception as _exc:
            logger.warning("RBAC list-tools filter failed, returning unfiltered: %s", _exc)
            return unfiltered

    mcp_server.list_tools = _filtered_list_tools

    # Re-register on the low-level protocol server so the over-the-wire
    # tools/list is actually filtered (see the note above). Without this the
    # filter is dead code for real MCP clients.
    try:
        mcp_server._mcp_server.list_tools()(_filtered_list_tools)
    except Exception as _exc:  # pragma: no cover - defensive
        logger.warning(
            "Could not re-register filtered list_tools on the low-level MCP "
            "server; tools/list will be unfiltered over the wire: %s",
            _exc,
        )


def register_all_tools(mcp_server):
    from app.config import settings

    # ── Product Analytics (GA4, Amplitude, Mixpanel, PostHog) ───────────────
    from app.tools.analytics_tools import register_analytics_tools

    register_analytics_tools(mcp_server)

    # ── Tag Manager (GTM) ────────────────────────────────────────────────────
    from app.tools.tagmanager_tools import register_tagmanager_tools

    register_tagmanager_tools(mcp_server)

    # ── Data Warehouse (BigQuery, Redshift, Snowflake) ───────────────────────
    from app.tools.warehouse_tools import register_warehouse_tools

    register_warehouse_tools(mcp_server)

    # ── Paid Marketing (Google Ads, Meta Ads, TikTok, Snap) ─────────────────
    from app.tools.marketing_tools import register_marketing_tools

    register_marketing_tools(mcp_server)

    # ── Cross-Platform Blended Reporting ────────────────────────────────────
    from app.tools.cross_platform_tools import register_cross_platform_tools

    register_cross_platform_tools(mcp_server)

    # ── Knowledge Base (KPI Library + Business Context) ─────────────────────
    from app.tools.knowledge_tools import register_knowledge_tools

    register_knowledge_tools(mcp_server)

    # ── Google Data Manager audiences ───────────────────────────────────────
    from app.tools.audience_tools import register_audience_tools

    register_audience_tools(mcp_server)

    # ── Google Search Console (organic search) ──────────────────────────────
    from app.tools.search_console_tools import register_search_console_tools

    register_search_console_tools(mcp_server)

    # ── Bing Webmaster Tools (Bing organic search) ───────────────────────────
    from app.tools.bing_webmaster_tools import register_bing_webmaster_tools

    register_bing_webmaster_tools(mcp_server)

    # ── Tag Rule Book (20-platform connector-independent rule engine) ─────────
    from app.tools.tag_rulebook_tools import register_tag_rulebook_tools

    register_tag_rulebook_tools(mcp_server)

    # ── Live Tag Test (Claude computer-use guided browser testing) ────────────
    from app.tools.live_tag_test_tools import register_live_tag_test_tools

    register_live_tag_test_tools(mcp_server)

    # ── Audit Result Persistence (save findings to Fluxito UI) ───────────────
    from app.tools.save_audit_result_tools import register_save_audit_result_tools

    register_save_audit_result_tools(mcp_server)

    # Install tool hook AFTER all tools are registered (needs _tool_manager)
    _install_tool_hook(mcp_server)

    # ── Project tools: list, switch, get active project ───────────────────────
    @mcp_server.tool("list_my_projects")
    async def list_my_projects() -> dict:
        """
        List all projects the current user belongs to, with their role and plan.

        Returns a list of projects with: name, slug, plan (free/pro/team),
        your role (owner/admin/member), and whether each is the currently
        active project.

        Call this first when starting a session to see available projects,
        then use set_active_project() to pick one.
        """
        import app.app_state as state

        u = state.current_user_ctx.get()
        if not u:
            return {"error": True, "error_type": "unauthenticated", "message": "No active session."}

        active_proj = state.current_project_ctx.get()
        active_id = active_proj.project_id if active_proj else None

        return {
            "projects": [
                {
                    "project_id": p.project_id,
                    "name": p.project_name,
                    "slug": p.project_slug,
                    "your_role": p.role,
                    "is_active": p.project_id == active_id,
                }
                for p in u.projects
            ],
            "active_project": active_id,
            "hint": "Use set_active_project('project name or slug') to select a project."
            if not active_id
            else None,
        }

    @mcp_server.tool("set_active_project")
    async def set_active_project(project: str) -> dict:
        """
        Set the active project for this session. All subsequent tool calls
        (analytics_read, tagmanager_read, etc.) will be scoped to this project's
        connectors and data.

        Args:
            project: Project name, slug, or ID. Partial matches are supported.

        If you have only one project, it is auto-selected — no need to call this.

        The selection persists across turns. Call this in its OWN turn, then use
        the scoped tools afterwards. Do NOT emit set_active_project and a
        dependent tool as parallel tool calls in the same turn expecting the
        dependent call to see the new project — parallel calls run concurrently
        and the selection may not be visible yet. If you must scope a tool in the
        same turn, pass project_id to that tool directly instead.
        """
        import app.app_state as state
        from app.auth.mcp_session_manager import build_project_context

        u = state.current_user_ctx.get()
        if not u:
            return {"error": True, "error_type": "unauthenticated", "message": "No active session."}

        if not u.projects:
            return {
                "error": True,
                "error_type": "no_projects",
                "message": "You don't belong to any projects yet.",
                "action_required": f"Visit {settings.APP_BASE_URL}/projects to create your first project.",
            }

        # Match by ID, slug, or name (case-insensitive partial match)
        query = project.strip().lower()
        matches = []
        for p in u.projects:
            if query == p.project_id.lower() or query == p.project_slug.lower():
                matches = [p]
                break
            if query in p.project_name.lower() or query in p.project_slug.lower():
                matches.append(p)

        if len(matches) == 0:
            return {
                "error": True,
                "error_type": "project_not_found",
                "message": f"No project matching '{project}' found.",
                "available_projects": [{"name": p.project_name, "slug": p.project_slug} for p in u.projects],
            }
        if len(matches) > 1:
            return {
                "error": True,
                "error_type": "ambiguous_project",
                "message": f"Multiple projects match '{project}'. Please be more specific.",
                "matches": [{"name": p.project_name, "slug": p.project_slug} for p in matches],
            }

        target = matches[0]
        try:
            project_ctx = await build_project_context(target.project_id, u.user_id)
            state.current_project_ctx.set(project_ctx)

            # Persist active project in Redis so it survives SSE reconnects
            try:
                if state.redis_client:
                    await state.redis_client.setex(
                        f"mcp:active_project:{u.user_id}",
                        86400,  # 24h TTL
                        target.project_id,
                    )
            except Exception:
                pass  # non-critical — ContextVar is still set for this session

            # Also update the user context's has_* flags for backward compatibility
            u.has_ga4 = project_ctx.has_ga4
            u.has_gtm = project_ctx.has_gtm
            u.has_ads = project_ctx.has_ads
            u.has_bq = project_ctx.has_bq
            u.has_meta = project_ctx.has_meta
            u.has_tiktok = project_ctx.has_tiktok
            u.has_snap = project_ctx.has_snap
            u.has_x = project_ctx.has_x
            u.has_reddit = project_ctx.has_reddit
            u.has_bing = project_ctx.has_bing
            u.has_apple = project_ctx.has_apple
            u.has_amplitude = project_ctx.has_amplitude
            u.has_branch = project_ctx.has_branch
            u.has_appsflyer = project_ctx.has_appsflyer
            u.has_adjust = project_ctx.has_adjust
            u.has_mixpanel = project_ctx.has_mixpanel
            u.has_posthog = project_ctx.has_posthog
            u.has_adobe_analytics = project_ctx.has_adobe_analytics
            u.has_adobe_launch = project_ctx.has_adobe_launch
            u.has_adobe_marketo = project_ctx.has_adobe_marketo
            u.has_braze = project_ctx.has_braze
            u.has_moengage = project_ctx.has_moengage
            u.has_redshift = project_ctx.has_redshift
            u.has_snowflake = project_ctx.has_snowflake
            u.connections = project_ctx.connections
            u.ga4_properties = project_ctx.ga4_properties
            u.gtm_containers = project_ctx.gtm_containers
            u.ads_accounts = project_ctx.ads_accounts
            u.has_data_manager = project_ctx.has_data_manager

            connected = [
                name
                for name, flag in [
                    ("GA4", project_ctx.has_ga4),
                    ("GTM", project_ctx.has_gtm),
                    ("Google Ads", project_ctx.has_ads),
                    ("Google Data Manager", project_ctx.has_data_manager),
                    ("BigQuery", project_ctx.has_bq),
                    ("Meta Ads", project_ctx.has_meta),
                    ("TikTok Ads", project_ctx.has_tiktok),
                    ("Snap Ads", project_ctx.has_snap),
                    ("X Ads", project_ctx.has_x),
                    ("Reddit Ads", project_ctx.has_reddit),
                    ("Apple Ads", project_ctx.has_apple),
                    ("Bing Webmaster Tools", project_ctx.has_bing),
                    ("Amplitude", project_ctx.has_amplitude),
                    ("Branch", project_ctx.has_branch),
                    ("AppsFlyer", project_ctx.has_appsflyer),
                    ("Adjust", project_ctx.has_adjust),
                    ("Mixpanel", project_ctx.has_mixpanel),
                    ("PostHog", project_ctx.has_posthog),
                    ("Adobe Analytics", project_ctx.has_adobe_analytics),
                    ("Adobe Launch", project_ctx.has_adobe_launch),
                    ("Adobe Marketo Engage", project_ctx.has_adobe_marketo),
                    ("Braze", project_ctx.has_braze),
                    ("MoEngage", project_ctx.has_moengage),
                    ("Redshift", project_ctx.has_redshift),
                    ("Snowflake", project_ctx.has_snowflake),
                ]
                if flag
            ]

            return {
                "success": True,
                "active_project": {
                    "name": project_ctx.project_name,
                    "slug": project_ctx.project_slug,
                    "your_role": project_ctx.role,
                },
                "connected_platforms": connected,
                "message": f"Switched to project '{project_ctx.project_name}'. All tools now scoped to this project.",
            }
        except Exception as e:
            return {"error": True, "message": f"Failed to load project: {e!s}"}

    # ── Programmatic tool calling (run_script) ─────────────────────────────
    # Registered BEFORE the unified rewire so it lands in _tools and, since
    # it is not in the legacy_names set, survives the rewire as a first-class
    # public tool alongside the 18 unified dispatchers.
    from app.tools.programmatic import register_programmatic_tool

    register_programmatic_tool(mcp_server)

    # ── Collapse to unified ~18-tool surface ───────────────────────────────
    # Must run LAST, after every sub-module has registered its fine-grained
    # tools. Preserves the originals internally so the unified dispatchers
    # can still invoke them.
    from app.tools.unified import rewire_unified_surface

    rewire_unified_surface(mcp_server)
