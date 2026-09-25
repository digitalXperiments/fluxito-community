"""Role gate for connecting, editing and disconnecting platforms.

Connections belong to a project and power every AI tool call in it, so only
project owners/admins (``CAN_CONNECT_ROLES``) may attach, replace or remove
them. The gate runs in the request middleware, ahead of the ~50 connection
routes, so no individual route can forget it:

* ``POST/PUT/PATCH/DELETE /api/connections/…`` — for a path naming a
  connection id, the role is checked in the project *that connection belongs
  to*; otherwise (create) in the caller's active project.
* OAuth connect starts (``/connect/<platform>/authorize`` etc.) — owner/admin
  of the active project.

OAuth callbacks re-check the user and project captured at start with
``verify_oauth_callback`` (the callback URL itself carries no project).
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy import select

import app.app_state as app_state

# GET routes that start a connect flow for the active project.
_CONNECT_START_PATHS = {
    "/api/connections/google/initiate",
    "/connect/meta/authorize",
    "/connect/tiktok/authorize",
    "/connect/snap/authorize",
    "/connect/x/authorize",
    "/connect/apple/authorize",
    "/connect/pinterest",
    "/connect/linkedin",
    "/connect/bing",
    "/connect/reddit",
}

_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

_FORBIDDEN_MSG = "Only project owners and admins can connect, edit or disconnect platforms."


def _connection_models() -> list[tuple[Any, Any]]:
    """(model, project column) for every table holding a project's connections."""
    from app.models.bq_connection import BQConnection
    from app.models.connection import OAuthConnection
    from app.models.credential_connection import (
        AdjustConnection,
        AdobeConnection,
        AmplitudeConnection,
        AppsFlyerConnection,
        BranchConnection,
        BrazeConnection,
        MarketoConnection,
        MixpanelConnection,
        MoengageConnection,
        PostHogConnection,
        RedshiftConnection,
        SnowflakeConnection,
    )

    models: list[tuple[Any, Any]] = [
        (OAuthConnection, OAuthConnection.project_id),
        (BQConnection, BQConnection.fluxito_project_id),
    ]
    for m in (
        AmplitudeConnection,
        MixpanelConnection,
        AdobeConnection,
        MarketoConnection,
        PostHogConnection,
        RedshiftConnection,
        SnowflakeConnection,
        BranchConnection,
        AppsFlyerConnection,
        AdjustConnection,
        BrazeConnection,
        MoengageConnection,
    ):
        models.append((m, m.project_id))
    return models


async def project_of_connection(conn_id: str) -> tuple[bool, str | None]:
    """Return (found, project_id) for a connection id across every connection table."""
    try:
        cid = uuid.UUID(conn_id)
    except (ValueError, TypeError):
        return False, None
    async with app_state.db_session_factory() as db:
        for model, project_col in _connection_models():
            row = (await db.execute(select(project_col).where(model.id == cid))).first()
            if row is not None:
                return True, (str(row[0]) if row[0] else None)
    return False, None


async def member_role(user_id: str | None, project_id: str | None) -> str | None:
    """The caller's active role in an active project, or None."""
    from app.models.project import Project, ProjectMember

    if not user_id or not project_id:
        return None
    try:
        uid, pid = uuid.UUID(str(user_id)), uuid.UUID(str(project_id))
    except (ValueError, TypeError):
        return None
    async with app_state.db_session_factory() as db:
        row = (
            await db.execute(
                select(ProjectMember.role)
                .join(Project, Project.id == ProjectMember.project_id)
                .where(
                    ProjectMember.project_id == pid,
                    ProjectMember.user_id == uid,
                    ProjectMember.is_active.is_(True),
                    Project.is_active.is_(True),
                )
            )
        ).first()
    return row[0] if row else None


async def can_connect(user_id: str | None, project_id: str | None) -> bool:
    from app.models.project import CAN_CONNECT_ROLES

    return (await member_role(user_id, project_id)) in CAN_CONNECT_ROLES


async def _caller_user_id(request) -> str | None:
    from app.auth.uid_cookie import get_uid_from_request

    uid = get_uid_from_request(request)
    if uid:
        return uid
    if request.headers.get("Authorization", "").startswith("Bearer "):
        try:
            from app.auth.mcp_session_manager import require_valid_mcp_token

            ctx = await require_valid_mcp_token(request)
            return str(ctx.user_id) if ctx else None
        except Exception:
            return None
    return None


def _denied(path: str, *, html: bool):
    if html:
        from fastapi.responses import HTMLResponse

        return HTMLResponse(
            "<h2>You can't connect platforms in this project.</h2>"
            f"<p>{_FORBIDDEN_MSG} Ask one of them to connect it, or switch to a project you manage.</p>"
            "<p><a href='/connect'>Back to connections</a></p>",
            status_code=403,
        )
    from fastapi.responses import JSONResponse

    return JSONResponse({"error": True, "detail": _FORBIDDEN_MSG}, status_code=403)


async def connection_gate(scope) -> Any:
    """Return a 403 response app when the caller may not change connections, else None."""
    method = scope.get("method", "GET").upper()
    path = scope.get("path", "")
    is_mutation = method in ("POST", "PUT", "PATCH", "DELETE") and path.startswith("/api/connections")
    is_start = method == "GET" and path.rstrip("/") in _CONNECT_START_PATHS
    if not (is_mutation or is_start):
        return None

    from starlette.requests import Request

    request = Request(scope)
    user_id = await _caller_user_id(request)
    if not user_id:
        return None  # unauthenticated — the route itself answers 401 / sends to sign-in

    target_pid: str | None
    ids = _UUID_RE.findall(path) if is_mutation else []
    if ids:
        found, target_pid = await project_of_connection(ids[-1])
        if not found:
            return None  # unknown id — let the route answer 404
        if target_pid is None:
            return None  # legacy user-level row: routes already restrict it to its owner
    else:
        target_pid = request.query_params.get("project_id") or request.cookies.get("active_project_id")
        if not target_pid:
            from app.api.project_routes import ensure_active_project

            target_pid = await ensure_active_project(request, user_id)

    if await can_connect(user_id, target_pid):
        return None
    return _denied(path, html=is_start)


async def verify_oauth_callback(request, state_user_id: str | None, state_project_id: str | None):
    """Check an OAuth callback against the flow that started it.

    The browser completing the callback must be signed in as the user who
    started the flow (so a victim can't be tricked into consenting onto an
    attacker's account), and that user must still be an owner/admin of the
    target project. Returns an HTMLResponse to send back on failure, else None.
    """
    from fastapi.responses import HTMLResponse

    from app.auth.uid_cookie import get_uid_from_request

    current = get_uid_from_request(request)
    if not current or not state_user_id or str(current) != str(state_user_id):
        return HTMLResponse(
            "<h2>This connection link was started by a different account.</h2>"
            "<p>Sign in and start the connection again from Fluxito.</p><p><a href='/connect'>Connections</a></p>",
            status_code=403,
        )
    if state_project_id and not await can_connect(current, state_project_id):
        return _denied(request.url.path, html=True)
    return None
