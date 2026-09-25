"""
Project Routes — CRUD and member management for projects.

Projects are the data boundary and collaboration space.
All routes are user-authenticated (via uid cookie or MCP Bearer token).

Routes:
  GET  /projects                       — List user's projects
  POST /api/projects                   — Create a new project
  GET  /project/{slug}                 — Project dashboard (redirect to settings for now)
  GET  /project/{slug}/settings        — Project settings (general, members)
  DELETE /api/project/{slug}           — Delete (deactivate) a project
  POST /api/project/{slug}/members     — Invite a member
  DELETE /api/project/{slug}/members/{member_id}  — Remove a member
  PATCH  /api/project/{slug}/members/{member_id}/role  — Change member role
  POST /api/project/{slug}/transfer-ownership  — Transfer ownership
"""

import logging
import re
import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select, update

import app.app_state as app_state
from app.models.project import (
    CAN_CONNECT_ROLES,
    CAN_MANAGE_MEMBERS_ROLES,
    ROLE_ADMIN,
    ROLE_MEMBER,
    ROLE_OWNER,
    Project,
    ProjectMember,
)
from app.models.notification_channel import (
    EMAIL_SENDER_SES,
    EMAIL_SENDER_SMTP,
    VALID_EMAIL_SENDER_TYPES,
    ProjectEmailSender,
    ProjectSlackWebhook,
)
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _resolve_user(request: Request) -> dict | None:
    """Resolve authenticated user from cookie or bearer token.

    Returns a dict with user_id, email, display_name
    so templates can render the full nav (including admin link).
    """
    from app.api.google_oauth_routes import _load_user_view, _resolve_user_ctx

    user_ctx = await _resolve_user_ctx(request)
    if user_ctx:
        full_view = await _load_user_view(user_ctx)
        # Keep user_id for backward compat with routes that expect it
        full_view["user_id"] = user_ctx.user_id
        return full_view
    return None


def dedupe_connections(rows: list[dict]) -> list[dict]:
    """Collapse per-user connection rows into one card per (provider, email).

    Migration 042 made connections per-user, so the same external account can
    appear once per member. For display we show a single card; ``active`` wins
    over ``disconnected`` and we surface how many members connected it.
    """
    by_key: dict[tuple, dict] = {}
    for r in rows:
        key = (r["provider"], r["google_email"])
        card = by_key.get(key)
        if card is None:
            by_key[key] = {**r, "connected_by_count": 1}
        else:
            card["connected_by_count"] += 1
            if r["status"] == "active":
                card["status"] = "active"
    return list(by_key.values())


def get_active_project_id(request: Request) -> str | None:
    """Read the active project ID from the browser cookie.

    Returns the project UUID string or None.
    """
    return request.cookies.get("active_project_id")


def set_active_project_cookie(response, project_id: str):
    """Set the active project cookie on a response.

    ``secure`` matches the ``uid`` cookie (production HTTPS only) so the
    project preference isn't sent in the clear over an HTTP downgrade.
    Lifetime is aligned with ``uid`` (30 days) — keeping a separate
    1-year cookie alive after the session cookie expired meant the
    "active project" outlived the login it was scoped to.
    """
    from app.config import settings as _settings

    response.set_cookie(
        "active_project_id",
        str(project_id),
        max_age=30 * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=_settings.APP_ENV == "production",
        path="/",
    )


async def _get_project_by_slug(slug: str) -> Project | None:
    """Load a project by slug."""
    async with app_state.db_session_factory() as db:
        result = await db.execute(select(Project).where(Project.slug == slug, Project.is_active == True))
        return result.scalar_one_or_none()


async def _get_membership(project_id: uuid.UUID, user_id: uuid.UUID) -> ProjectMember | None:
    """Check if user is a member of the project."""
    async with app_state.db_session_factory() as db:
        result = await db.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user_id,
                ProjectMember.is_active == True,
            )
        )
        return result.scalar_one_or_none()


async def ensure_active_project(request: Request, user_id: str) -> str | None:
    """
    Guarantee an ``active_project_id`` cookie is set.

    Resolution order:
      1. Already set in cookie → validate it still exists & user is a member → return.
      2. Cookie missing or stale → pick the user's first project → return its id
         (caller must set the cookie on the response).
      3. User has zero projects → return None (caller should redirect to /projects).
    """
    uid = uuid.UUID(user_id)
    cookie_pid = get_active_project_id(request)

    async with app_state.db_session_factory() as db:
        # Validate current cookie if present
        if cookie_pid:
            try:
                pid = uuid.UUID(cookie_pid)
                result = await db.execute(
                    select(ProjectMember, Project)
                    .join(Project, ProjectMember.project_id == Project.id)
                    .where(
                        ProjectMember.project_id == pid,
                        ProjectMember.user_id == uid,
                        ProjectMember.is_active == True,
                    )
                )
                row = result.first()
                if row:
                    _, proj = row
                    # Store project name for nav display
                    request.state.active_project_name = proj.name
                    return cookie_pid  # valid
            except (ValueError, Exception):
                pass  # invalid cookie → fall through

        # Auto-select first project the user belongs to
        result = await db.execute(
            select(ProjectMember, Project)
            .join(Project, ProjectMember.project_id == Project.id)
            .where(
                ProjectMember.user_id == uid,
                ProjectMember.is_active == True,
                Project.is_active == True,
            )
            .order_by(Project.created_at.asc())
            .limit(1)
        )
        row = result.first()
        if row:
            _, proj = row
            # Store project name for nav display
            request.state.active_project_name = proj.name
            return str(proj.id)

    return None  # user has no projects


def _slugify(name: str) -> str:
    """Generate a URL-friendly slug from a project name."""
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    # Add a short random suffix to avoid collisions
    suffix = uuid.uuid4().hex[:8]
    return f"{slug}-{suffix}" if slug else suffix


# ---------------------------------------------------------------------------
# Project listing
# ---------------------------------------------------------------------------


@router.get("/projects")
async def list_projects_page(request: Request):
    """List user's projects (HTML page)."""
    user = await _resolve_user(request)
    if not user:
        return RedirectResponse("/signin?next=/projects", status_code=302)

    from app.templating import render

    uid = uuid.UUID(user["user_id"])

    async with app_state.db_session_factory() as db:
        result = await db.execute(
            select(ProjectMember, Project)
            .join(Project, ProjectMember.project_id == Project.id)
            .where(
                ProjectMember.user_id == uid,
                ProjectMember.is_active == True,
                Project.is_active == True,
            )
            .order_by(Project.created_at.desc())
        )
        memberships = [
            {
                "project_id": str(proj.id),
                "name": proj.name,
                "slug": proj.slug,
                "role": pm.role,
                "created_at": proj.created_at.isoformat() if proj.created_at else None,
            }
            for pm, proj in result.all()
        ]

    return render(
        request,
        "projects/list.html",
        {
            "projects": memberships,
            "user": user,
        },
    )


# ---------------------------------------------------------------------------
# Create project
# ---------------------------------------------------------------------------


class _CreateProjectRequest(BaseModel):
    """Validated body for POST /api/projects — caps name/description size
    so a malicious 1MB blob can't slip through the ``request.json()`` path."""

    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)


@router.post("/api/projects")
async def create_project(request: Request):
    """Create a new project."""
    user = await _resolve_user(request)
    if not user:
        raise HTTPException(401, "Not authenticated")

    try:
        payload = _CreateProjectRequest.model_validate(await request.json())
    except ValidationError as exc:
        raise HTTPException(400, f"Invalid request body: {exc.errors()[0]['msg']}")

    name = payload.name.strip()
    description = (payload.description or "").strip() or None

    if not name:
        raise HTTPException(400, "Project name is required")

    uid = uuid.UUID(user["user_id"])

    slug = _slugify(name)

    async with app_state.db_session_factory() as db:
        project = Project(
            name=name,
            slug=slug,
            description=description,
            owner_id=uid,
        )
        db.add(project)
        await db.flush()  # Get the project ID

        # Add owner as first member
        member = ProjectMember(
            project_id=project.id,
            user_id=uid,
            role=ROLE_OWNER,
            joined_at=datetime.utcnow(),
        )
        db.add(member)
        await db.commit()

    # Invalidate user context cache so project list refreshes
    from app.auth.mcp_session_manager import invalidate_user_context_cache

    await invalidate_user_context_cache(str(uid))

    response = JSONResponse(
        {
            "success": True,
            "project": {
                "id": str(project.id),
                "name": project.name,
                "slug": project.slug,
            },
            "redirect_url": f"/project/{project.slug}/settings",
        }
    )
    set_active_project_cookie(response, str(project.id))
    return response


async def ensure_default_project(
    user_id: "uuid.UUID | str",
    display_name: str | None,
    email: str,
) -> bool:
    """Create a personal project for *user_id* iff they belong to none.

    Returns True if a project was created, False if the user already had one.
    Safe to call on every login — it is a no-op for existing members.
    """
    uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))

    async with app_state.db_session_factory() as db:
        count = await db.scalar(
            select(func.count())
            .select_from(ProjectMember)
            .where(ProjectMember.user_id == uid, ProjectMember.is_active == True)
        )
        if count and count > 0:
            return False

        base = (display_name or (email.split("@")[0] if email else "My")).strip()
        name = f"{base}'s Project"
        slug = _slugify(name)

        project = Project(name=name, slug=slug, owner_id=uid)
        db.add(project)
        await db.flush()
        db.add(
            ProjectMember(
                project_id=project.id,
                user_id=uid,
                role=ROLE_OWNER,
                joined_at=datetime.utcnow(),
            )
        )
        await db.commit()

    from app.auth.mcp_session_manager import invalidate_user_context_cache

    await invalidate_user_context_cache(str(uid))
    return True


# ---------------------------------------------------------------------------
# Project settings
# ---------------------------------------------------------------------------


@router.get("/project/{slug}/settings")
async def project_settings_page(request: Request, slug: str):
    """Project settings page (general, members, connections, billing)."""
    user = await _resolve_user(request)
    if not user:
        return RedirectResponse(f"/signin?next=/project/{slug}/settings", status_code=302)

    project = await _get_project_by_slug(slug)
    if not project:
        raise HTTPException(404, "Project not found")

    uid = uuid.UUID(user["user_id"])
    membership = await _get_membership(project.id, uid)
    if not membership:
        return RedirectResponse(url="/home", status_code=302)

    # Load all members
    async with app_state.db_session_factory() as db:
        result = await db.execute(
            select(ProjectMember, User)
            .join(User, ProjectMember.user_id == User.id)
            .where(
                ProjectMember.project_id == project.id,
                ProjectMember.is_active == True,
            )
            .order_by(ProjectMember.role, User.email)
        )
        members = [
            {
                "id": str(pm.id),
                "user_id": str(pm.user_id),
                "email": u.email,
                "display_name": u.display_name,
                "role": pm.role,
                "joined_at": pm.joined_at.isoformat() if pm.joined_at else None,
                "role_ids": [],
            }
            for pm, u in result.all()
        ]

        # Custom RBAC role assignments per member (member_id -> [role_id])
        from app.models.role import MemberRole

        if members:
            mr_rows = await db.execute(
                select(MemberRole.project_member_id, MemberRole.role_id).where(
                    MemberRole.project_member_id.in_([uuid.UUID(m["id"]) for m in members])
                )
            )
            _assign: dict[str, list[str]] = {}
            for pm_id, role_id in mr_rows.all():
                _assign.setdefault(str(pm_id), []).append(str(role_id))
            for m in members:
                m["role_ids"] = _assign.get(m["id"], [])

    # Load project connections (scoped to project)
    from app.models.connection import OAuthConnection

    async with app_state.db_session_factory() as db:
        conn_result = await db.execute(
            select(OAuthConnection).where(
                OAuthConnection.project_id == project.id,
                OAuthConnection.is_active == True,
            )
        )
        raw_connections = [
            {
                "id": str(c.id),
                "provider": c.provider,
                "google_email": c.google_email,
                "status": c.connection_status,
            }
            for c in conn_result.scalars().all()
        ]
        connections = dedupe_connections(raw_connections)

    # Load email senders + Slack webhooks for the Notifications tab.
    # Owner/admin only — for member/viewer we pass empty lists so the
    # tab simply isn't rendered.
    if membership.role in CAN_CONNECT_ROLES:
        email_senders = await _list_email_senders(project.id)
        slack_webhooks = await _list_slack_webhooks(project.id)
        from app.auth.oauth_app_credentials import list_project_oauth_app_status

        async with app_state.db_session_factory() as db:
            oauth_apps = await list_project_oauth_app_status(db, project.id)
        from app.services.invites import list_for_project

        pending_invites = await list_for_project(project.id)
    else:
        email_senders = []
        slack_webhooks = []
        oauth_apps = []
        pending_invites = []

    # Published API rate limits + real per-connector usage, split into this
    # project's connected tools and the rest of the catalog. Uses the shared
    # resolver so the "connected" set matches the Home page exactly. Usage and
    # busiest-window headroom come from the tool-call activity log (one
    # grouped query, per-minute buckets over the last 30 days).
    from app.connectors import call_usage, rate_limits
    from app.connectors.connection_status import resolve_connection_flags

    conn_flags = await resolve_connection_flags(uid, project.id)
    rl_connected, rl_available = rate_limits.partition(rate_limits.connected_keys(conn_flags))
    rl_usage: dict = {}
    try:
        async with app_state.db_session_factory() as db:
            rl_usage = await call_usage.usage_by_minute(db, project.id)
    except Exception:
        logger.warning("rate-limit usage lookup failed", exc_info=True)

    def _rl_view(connectors):
        rows = rate_limits.to_view(connectors)
        for row in rows:
            row["call_usage"] = call_usage.headroom_for(row["key"], rl_usage.get(row["key"]))
            row["usage_count"] = row["call_usage"]["calls"]
        return rows

    from app.templating import render

    response = render(
        request,
        "projects/settings.html",
        {
            "project": {
                "id": str(project.id),
                "name": project.name,
                "slug": project.slug,
                "description": project.description,
                "owner_id": str(project.owner_id),
                "dashboard_style_config": project.dashboard_style_config,
                "rbac_enabled": project.rbac_enabled,
                "created_label": project.created_at.strftime("%b %Y") if project.created_at else None,
            },
            "members": members,
            "connections": connections,
            "email_senders": email_senders,
            "slack_webhooks": slack_webhooks,
            "oauth_apps": oauth_apps,
            "pending_invites": pending_invites,
            "rate_limits_connected": _rl_view(rl_connected),
            "rate_limits_available": _rl_view(rl_available),
            "rate_limits_reviewed": rate_limits.REVIEWED,
            "rate_limits_usage_days": 30,
            "membership": {"role": membership.role},
            "user_project_role": membership.role,
            "user": user,
        },
    )
    # Set active project cookie when viewing project settings
    set_active_project_cookie(response, str(project.id))
    return response


@router.get("/project/{slug}")
async def project_dashboard(request: Request, slug: str):
    """Project home — redirects to settings (does NOT switch active project)."""
    return RedirectResponse(f"/project/{slug}/settings", status_code=302)


_UUID_SEGMENT = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$|^\d+$"
)


def _switch_redirect_target(referer: str, new_slug: str) -> str:
    """Where to land after switching project: the same page, in the new project.

    * ``/project/<old>/…`` → ``/project/<new>/…`` — project-scoped URLs would
      otherwise keep showing the old project under the new one's sidebar.
    * A path ending in a record id (``/audits/<uuid>``) → its list page; the
      record belongs to the previous project.
    * Only same-site relative paths are honoured (never ``//host`` or
      ``/\\host``); anything else lands on ``/home``.
    """
    from urllib.parse import urlparse

    path = urlparse(referer).path if referer else ""
    if not path.startswith("/") or path.startswith(("//", "/\\")) or "\\" in path:
        return "/home"
    if path.startswith("/api/"):
        return "/home"
    parts = path.split("/")  # ["", "project", "<slug>", ...]
    if len(parts) >= 3 and parts[1] == "project" and parts[2]:
        parts[2] = new_slug
        return "/".join(parts)
    segments = [p for p in parts if p]
    while segments and _UUID_SEGMENT.match(segments[-1]):
        segments.pop()
    return "/" + "/".join(segments) if segments else "/home"


async def _revoke_member_caches(user_id: str, project_id: str) -> None:
    """Drop every cached grant for a member after removal or a role change."""
    from app.auth.mcp_session_manager import invalidate_project_context_cache, invalidate_user_context_cache
    from app.auth.permissions import invalidate_permissions_cache

    try:
        await invalidate_permissions_cache(user_id, project_id)
        await invalidate_user_context_cache(user_id)
        await invalidate_project_context_cache(project_id)
        redis = app_state.redis_client
        if redis is not None:
            await redis.delete(f"mcp:active_project:{user_id}")
    except Exception:
        logger.warning("Couldn't clear cached access for %s in %s", user_id, project_id, exc_info=True)


@router.get("/api/project/switch/{slug}")
async def switch_project(request: Request, slug: str):
    """Switch active project via nav dropdown (GET for simple link navigation)."""
    user = await _resolve_user(request)
    if not user:
        return RedirectResponse(url="/signin", status_code=302)

    project = await _get_project_by_slug(slug)
    if not project:
        return RedirectResponse(url="/projects", status_code=302)

    uid = uuid.UUID(user["user_id"])
    membership = await _get_membership(project.id, uid)
    if not membership:
        return RedirectResponse(url="/projects", status_code=302)

    redirect_to = _switch_redirect_target(request.headers.get("referer", ""), project.slug)
    response = RedirectResponse(url=redirect_to, status_code=302)
    set_active_project_cookie(response, str(project.id))
    return response


@router.post("/api/project/{slug}/activate")
async def activate_project(request: Request, slug: str):
    """Set a project as the active project (cookie-based for web UI)."""
    user = await _resolve_user(request)
    if not user:
        raise HTTPException(401, "Not authenticated")

    project = await _get_project_by_slug(slug)
    if not project:
        raise HTTPException(404, "Project not found")

    uid = uuid.UUID(user["user_id"])
    membership = await _get_membership(project.id, uid)
    if not membership:
        raise HTTPException(403, "You are not a member of this project")

    response = JSONResponse({"success": True, "project_id": str(project.id), "slug": slug})
    set_active_project_cookie(response, str(project.id))
    return response


# ---------------------------------------------------------------------------
# Member management
# ---------------------------------------------------------------------------


@router.post("/api/project/{slug}/members")
async def invite_member(request: Request, slug: str):
    """Invite someone to the project by email.

    Creates a *pending* invite — no membership and no account yet. Existing
    users accept it in-app; new people open the one-time link (returned here
    for the inviter to share, and emailed when email is configured) and create
    their account there.
    """
    user = await _resolve_user(request)
    if not user:
        raise HTTPException(401, "Not authenticated")

    project = await _get_project_by_slug(slug)
    if not project:
        raise HTTPException(404, "Project not found")

    uid = uuid.UUID(user["user_id"])
    membership = await _get_membership(project.id, uid)
    if not membership or membership.role not in CAN_MANAGE_MEMBERS_ROLES:
        raise HTTPException(403, "Only owners and admins can invite members")

    body = await request.json()
    invite_email = body.get("email", "").strip().lower()
    invite_role = body.get("role", ROLE_MEMBER).strip()

    if not invite_email or "@" not in invite_email:
        raise HTTPException(400, "Email is required")
    if invite_role not in (ROLE_ADMIN, ROLE_MEMBER):
        raise HTTPException(400, "Invalid role (must be 'admin' or 'member')")

    async with app_state.db_session_factory() as db:
        already = (
            await db.execute(
                select(ProjectMember.id)
                .join(User, User.id == ProjectMember.user_id)
                .where(
                    ProjectMember.project_id == project.id,
                    ProjectMember.is_active == True,
                    User.email == invite_email,
                )
            )
        ).first()
        existing_user = (await db.execute(select(User.id).where(User.email == invite_email))).first()
    if already:
        return JSONResponse(
            {"error": True, "message": f"{invite_email} is already a member of this project."},
            status_code=400,
        )

    from app.services.invites import create_invite, invite_url

    _inv, token = await create_invite(
        project_id=project.id, email=invite_email, role=invite_role, invited_by=uid
    )
    link = invite_url(token)

    import asyncio as _asyncio

    from app.email_service import send_project_invite_email

    _asyncio.create_task(
        send_project_invite_email(
            to_email=invite_email,
            project_name=project.name,
            project_slug=project.slug,
            inviter_email=user["email"],
            inviter_name=user.get("display_name"),
            role=invite_role,
            accept_url=link,
        )
    )

    return JSONResponse(
        {
            "success": True,
            "message": (
                f"Invited {invite_email}. They'll see the invitation when they next sign in."
                if existing_user
                else f"Invited {invite_email}. Share the link below so they can create their account."
            ),
            "email": invite_email,
            "invite_url": link,
            "existing_account": bool(existing_user),
        }
    )


@router.delete("/api/project/{slug}/members/{member_id}")
async def remove_member(request: Request, slug: str, member_id: str):
    """Remove a member from the project."""
    user = await _resolve_user(request)
    if not user:
        raise HTTPException(401, "Not authenticated")

    project = await _get_project_by_slug(slug)
    if not project:
        raise HTTPException(404, "Project not found")

    uid = uuid.UUID(user["user_id"])
    my_membership = await _get_membership(project.id, uid)
    if not my_membership or my_membership.role not in CAN_MANAGE_MEMBERS_ROLES:
        raise HTTPException(403, "Only owners and admins can remove members")

    async with app_state.db_session_factory() as db:
        result = await db.execute(
            select(ProjectMember).where(
                ProjectMember.id == uuid.UUID(member_id),
                ProjectMember.project_id == project.id,
            )
        )
        target = result.scalar_one_or_none()
        if not target:
            raise HTTPException(404, "Member not found")
        if target.role == ROLE_OWNER:
            raise HTTPException(400, "Cannot remove the project owner. Transfer ownership first.")

        # Check if this user owns any connections — if so, mark them disconnected

        # Every connection this member brought into the project stops powering
        # it: their tokens/credentials are theirs, and they've left.
        from app.auth.connection_gate import _connection_models
        from app.models.connection import OAuthConnection as _OAuthConnection

        affected_connections = []
        for model, project_col in _connection_models():
            rows = (
                (
                    await db.execute(
                        select(model).where(
                            project_col == project.id,
                            model.user_id == target.user_id,
                            model.is_active == True,
                        )
                    )
                )
                .scalars()
                .all()
            )
            for cred_conn in rows:
                cred_conn.connection_status = "disconnected"
                cred_conn.is_active = False
                if model is _OAuthConnection:
                    affected_connections.append(cred_conn)

        target.is_active = False
        await db.commit()

    disconnected_count = len(affected_connections)
    msg = "Member removed."
    if disconnected_count > 0:
        msg += f" {disconnected_count} connection(s) owned by this user are now disconnected — an admin needs to reconnect them."

    # Invalidate caches — access ends now, not when a cache expires.
    from app.auth.mcp_session_manager import invalidate_project_context_cache

    await invalidate_project_context_cache(str(project.id))
    await _revoke_member_caches(str(target.user_id), str(project.id))

    # Notify project owner about disconnected connections
    if disconnected_count > 0:
        import asyncio

        try:
            from app.notifications import create_notification

            asyncio.create_task(
                create_notification(
                    user_id=str(project.owner_id),
                    title=f"Connections disconnected in {project.name}",
                    message=f"{disconnected_count} connection(s) were disconnected when a member was removed. An admin needs to reconnect them.",
                    category="project",
                    severity="warning",
                    action_url=f"/project/{slug}/settings",
                )
            )
        except Exception:
            pass

    return JSONResponse({"success": True, "message": msg, "disconnected_connections": disconnected_count})


@router.patch("/api/project/{slug}/members/{member_id}/role")
async def change_member_role(request: Request, slug: str, member_id: str):
    """Change a member's role."""
    user = await _resolve_user(request)
    if not user:
        raise HTTPException(401, "Not authenticated")

    project = await _get_project_by_slug(slug)
    if not project:
        raise HTTPException(404, "Project not found")

    uid = uuid.UUID(user["user_id"])
    my_membership = await _get_membership(project.id, uid)
    if not my_membership or my_membership.role != ROLE_OWNER:
        raise HTTPException(403, "Only the project owner can change roles")

    body = await request.json()
    new_role = body.get("role", "").strip()
    if new_role not in (ROLE_ADMIN, ROLE_MEMBER):
        raise HTTPException(400, "Invalid role (must be 'admin' or 'member')")

    async with app_state.db_session_factory() as db:
        result = await db.execute(
            select(ProjectMember).where(
                ProjectMember.id == uuid.UUID(member_id),
                ProjectMember.project_id == project.id,
            )
        )
        target = result.scalar_one_or_none()
        if not target:
            raise HTTPException(404, "Member not found")
        if target.role == ROLE_OWNER:
            raise HTTPException(400, "Cannot change the owner's role. Use transfer ownership instead.")

        target.role = new_role
        await db.commit()
        target_user_id = str(target.user_id)

    await _revoke_member_caches(target_user_id, str(project.id))
    return JSONResponse({"success": True, "message": f"Role changed to {new_role}."})


@router.post("/api/project/{slug}/members/{member_id}/reset-password")
async def reset_member_password(request: Request, slug: str, member_id: str):
    """Owner/admin re-issues a temporary password for a member (no SMTP era).

    Refuses the project owner, and refuses password-less (Google sign-in) users —
    minting a password onto a Google account would be an impersonation vector.
    """
    user = await _resolve_user(request)
    if not user:
        raise HTTPException(401, "Not authenticated")

    project = await _get_project_by_slug(slug)
    if not project:
        raise HTTPException(404, "Project not found")

    uid = uuid.UUID(user["user_id"])
    my_membership = await _get_membership(project.id, uid)
    if not my_membership or my_membership.role not in CAN_MANAGE_MEMBERS_ROLES:
        raise HTTPException(403, "Only owners and admins can reset passwords")

    from app.auth.email_auth import generate_temp_password, hash_password

    new_password = generate_temp_password()

    async with app_state.db_session_factory() as db:
        result = await db.execute(
            select(ProjectMember).where(
                ProjectMember.id == uuid.UUID(member_id),
                ProjectMember.project_id == project.id,
            )
        )
        target = result.scalar_one_or_none()
        if not target:
            raise HTTPException(404, "Member not found")
        if target.role == ROLE_OWNER:
            raise HTTPException(400, "Cannot reset the project owner's password.")

        target_user = await db.get(User, target.user_id)
        if not target_user:
            raise HTTPException(404, "User not found")
        if not target_user.password_hash:
            raise HTTPException(
                400,
                "This member signs in with Google; password reset doesn't apply.",
            )
        # A password is global to the account, not to this project. Only reset
        # accounts that live entirely inside this project (created by its
        # invite, no other workspace) — otherwise any owner could invite an
        # existing user and take over their account. Never a super admin.
        other_memberships = (
            await db.execute(
                select(func.count())
                .select_from(ProjectMember)
                .where(
                    ProjectMember.user_id == target_user.id,
                    ProjectMember.project_id != project.id,
                    ProjectMember.is_active == True,
                )
            )
        ).scalar() or 0
        if target_user.is_superadmin or other_memberships:
            raise HTTPException(
                403,
                "This person has their own Fluxito account. Ask them to use “Forgot password” on the sign-in page.",
            )

        target_user.password_hash = hash_password(new_password)
        target_user.email_verified = True
        await db.commit()
        target_email = target_user.email

    return JSONResponse({"success": True, "email": target_email, "temp_password": new_password})


# ---------------------------------------------------------------------------
# Ownership transfer
# ---------------------------------------------------------------------------


@router.post("/api/project/{slug}/transfer-ownership")
async def transfer_ownership(request: Request, slug: str):
    """Transfer project ownership to another member."""
    user = await _resolve_user(request)
    if not user:
        raise HTTPException(401, "Not authenticated")

    project = await _get_project_by_slug(slug)
    if not project:
        raise HTTPException(404, "Project not found")

    uid = uuid.UUID(user["user_id"])
    my_membership = await _get_membership(project.id, uid)
    if not my_membership or my_membership.role != ROLE_OWNER:
        raise HTTPException(403, "Only the current owner can transfer ownership")

    body = await request.json()
    new_owner_id = body.get("user_id", "").strip()
    if not new_owner_id:
        raise HTTPException(400, "user_id of the new owner is required")

    new_uid = uuid.UUID(new_owner_id)

    async with app_state.db_session_factory() as db:
        # Verify the new owner is a member
        result = await db.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project.id,
                ProjectMember.user_id == new_uid,
                ProjectMember.is_active == True,
            )
        )
        new_owner_member = result.scalar_one_or_none()
        if not new_owner_member:
            raise HTTPException(400, "The new owner must be an active member of the project")

        # Demote current owner to admin
        my_membership_db = await db.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project.id,
                ProjectMember.user_id == uid,
            )
        )
        current_owner = my_membership_db.scalar_one()
        current_owner.role = ROLE_ADMIN

        # Promote new owner
        new_owner_member.role = ROLE_OWNER

        # Update project.owner_id
        result = await db.execute(select(Project).where(Project.id == project.id))
        proj = result.scalar_one()
        proj.owner_id = new_uid

        await db.commit()

    return JSONResponse({"success": True, "message": "Ownership transferred. You are now an admin."})


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Delete (deactivate) project
# ---------------------------------------------------------------------------


@router.delete("/api/project/{slug}")
async def delete_project(request: Request, slug: str):
    """
    Soft-delete a project — sets is_active=False on the project and its members.

    Only the project owner can delete a project.
    """
    user = await _resolve_user(request)
    if not user:
        raise HTTPException(401, "Not authenticated")

    project = await _get_project_by_slug(slug)
    if not project:
        raise HTTPException(404, "Project not found")

    uid = uuid.UUID(user["user_id"])
    if str(project.owner_id) != str(uid):
        raise HTTPException(403, "Only the project owner can delete this project")

    # Soft-delete: deactivate project and all members
    async with app_state.db_session_factory() as db:
        # Deactivate the project
        result = await db.execute(select(Project).where(Project.id == project.id))
        proj = result.scalar_one()
        proj.is_active = False

        # Deactivate all memberships (remember who, to drop their cached access)
        from sqlalchemy import update as sa_update

        member_ids = [
            str(r[0])
            for r in (
                await db.execute(select(ProjectMember.user_id).where(ProjectMember.project_id == project.id))
            ).all()
        ]
        await db.execute(
            sa_update(ProjectMember).where(ProjectMember.project_id == project.id).values(is_active=False)
        )

        # Deactivate every connection tied to this project (all connection tables)
        from app.auth.connection_gate import _connection_models

        for model, project_col in _connection_models():
            await db.execute(sa_update(model).where(project_col == project.id).values(is_active=False))

        await db.commit()

    # Invalidate caches for every former member, not just the owner.
    for member_id in set(member_ids) | {str(uid)}:
        await _revoke_member_caches(member_id, str(project.id))

    logger.info(f"Project {project.id} ({project.name}) deleted by user {uid}")

    return JSONResponse(
        {
            "success": True,
            "message": f"Project '{project.name}' has been deleted.",
            "redirect_url": "/projects",
        }
    )


_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@router.patch("/api/project/{slug}")
async def update_project(request: Request, slug: str):
    """
    Rename a project and/or change its URL slug. Body: {name, slug?}.

    Owners and admins only. Slugs are lowercase words joined by hyphens,
    3–64 characters, and must be unique across all projects.
    """
    user = await _resolve_user(request)
    if not user:
        raise HTTPException(401, "Not authenticated")

    project = await _get_project_by_slug(slug)
    if not project:
        raise HTTPException(404, "Project not found")

    uid = uuid.UUID(user["user_id"])
    membership = await _get_membership(project.id, uid)
    if not membership or membership.role not in ("owner", "admin"):
        raise HTTPException(403, "Only project owners and admins can edit project details")

    try:
        body = await request.json()
    except Exception:
        body = {}
    name = str(body.get("name") or "").strip()
    new_slug = str(body.get("slug") or "").strip().lower() or project.slug
    if not name or len(name) > 255:
        return JSONResponse({"error": "Project name must be 1–255 characters."}, status_code=400)
    if new_slug != project.slug and (not (3 <= len(new_slug) <= 64) or not _SLUG_RE.match(new_slug)):
        return JSONResponse(
            {"error": "Slug must be 3–64 lowercase letters, numbers and single hyphens."},
            status_code=400,
        )

    async with app_state.db_session_factory() as db:
        if new_slug != project.slug:
            taken = await db.execute(
                select(Project.id).where(Project.slug == new_slug, Project.id != project.id)
            )
            if taken.scalar_one_or_none():
                return JSONResponse({"error": "That slug is already in use."}, status_code=409)
        proj = (await db.execute(select(Project).where(Project.id == project.id))).scalar_one()
        proj.name = name
        proj.slug = new_slug
        member_ids = (
            (
                await db.execute(
                    select(ProjectMember.user_id).where(
                        ProjectMember.project_id == project.id, ProjectMember.is_active == True
                    )
                )
            )
            .scalars()
            .all()
        )
        await db.commit()

    from app.auth.mcp_session_manager import invalidate_user_context_cache

    for member_id in {str(m) for m in member_ids} | {str(uid)}:
        await invalidate_user_context_cache(member_id)

    logger.info(f"Project {project.id} updated by user {uid}: name={name!r} slug={new_slug!r}")
    payload: dict = {"success": True, "project": {"name": name, "slug": new_slug}}
    if new_slug != project.slug:
        payload["redirect_url"] = f"/project/{new_slug}/settings"
    return JSONResponse(payload)


# ---------------------------------------------------------------------------
# Notifications — email senders (bring-your-own SMTP / SES credentials)
# ---------------------------------------------------------------------------
#
# All routes below are project-scoped and require the caller to be the
# project owner or an admin (``CAN_CONNECT_ROLES``). Members cannot see
# or modify credentials. Credentials are stored Fernet-encrypted via
# ``ProjectEmailSender.set_config``; this file never holds decrypted
# credentials longer than a single request.


async def _project_manage_scope(request: Request, slug: str) -> tuple[dict, Project, ProjectMember]:
    """Shared auth+resolution helper for notification routes.

    Returns ``(user, project, membership)``. Raises HTTPException with
    the right status code for auth / visibility / permission failures.
    """
    user = await _resolve_user(request)
    if not user:
        raise HTTPException(401, "Not authenticated")

    project = await _get_project_by_slug(slug)
    if not project:
        raise HTTPException(404, "Project not found")

    uid = uuid.UUID(user["user_id"])
    membership = await _get_membership(project.id, uid)
    if not membership:
        raise HTTPException(403, "You are not a member of this project")
    if membership.role not in CAN_CONNECT_ROLES:
        raise HTTPException(403, "Only owners and admins can manage notification senders")
    return user, project, membership


def _email_sender_config_payload(sender_type: str, body: dict) -> dict:
    """Extract credential fields from a request body into a config dict.

    Validates required fields for each type and raises HTTPException(400)
    with a clear message on missing input. The caller passes the
    resulting dict to ``ProjectEmailSender.set_config`` for encryption.

    For SMTP the port is coerced to int (form submissions may send it
    as a string). The ``tls_mode`` defaults to ``starttls``.
    """
    cfg = body.get("config") or {}
    if not isinstance(cfg, dict):
        raise HTTPException(400, "config must be an object")

    if sender_type == EMAIL_SENDER_SMTP:
        host = (cfg.get("host") or "").strip()
        port_raw = cfg.get("port")
        if not host or not port_raw:
            raise HTTPException(400, "SMTP config requires host and port")
        from app.tag_testing.flow_runner.executor import is_safe_http_url

        # SSRF guard: the server connects to this host (and echoes errors back).
        if not is_safe_http_url(f"http://{host}"):
            raise HTTPException(400, "SMTP host must be a public mail server")
        try:
            port = int(port_raw)
        except (TypeError, ValueError):
            raise HTTPException(400, "SMTP port must be an integer")
        tls_mode = (cfg.get("tls_mode") or "starttls").strip().lower()
        if tls_mode not in {"none", "starttls", "ssl"}:
            raise HTTPException(400, "SMTP tls_mode must be one of: none, starttls, ssl")
        return {
            "host": host,
            "port": port,
            "username": cfg.get("username") or None,
            "password": cfg.get("password") or None,
            "tls_mode": tls_mode,
        }

    if sender_type == EMAIL_SENDER_SES:
        region = (cfg.get("region") or "").strip()
        ak = (cfg.get("access_key_id") or "").strip()
        sk = cfg.get("secret_access_key") or ""
        if not region or not ak or not sk:
            raise HTTPException(
                400,
                "SES config requires region, access_key_id, and secret_access_key",
            )
        out: dict = {
            "region": region,
            "access_key_id": ak,
            "secret_access_key": sk,
        }
        # Optional — only include if set
        cset = (cfg.get("configuration_set") or "").strip()
        if cset:
            out["configuration_set"] = cset
        return out

    raise HTTPException(
        400,
        f"Unsupported email sender type: {sender_type!r}",
    )


async def _list_email_senders(project_id: uuid.UUID) -> list[dict]:
    """Return redacted sender summaries for a project, default-first."""
    from app.notifications.email import sender_display_summary

    async with app_state.db_session_factory() as db:
        result = await db.execute(
            select(ProjectEmailSender)
            .where(ProjectEmailSender.project_id == project_id)
            .order_by(
                ProjectEmailSender.is_default.desc(),
                ProjectEmailSender.created_at.desc(),
            )
        )
        rows = list(result.scalars().all())
    return [sender_display_summary(r) for r in rows]


@router.get("/api/project/{slug}/email-senders")
async def list_email_senders(request: Request, slug: str):
    """List all email senders configured for this project."""
    _, project, _ = await _project_manage_scope(request, slug)
    return JSONResponse({"senders": await _list_email_senders(project.id)})


@router.post("/api/project/{slug}/email-senders")
async def create_email_sender(request: Request, slug: str):
    """Create a new email sender.

    Body shape:
      {
        "type":         "smtp" | "ses",
        "label":        "Gmail",
        "from_address": "reports@example.com",
        "from_name":    "Analytics Reports",   // optional
        "is_default":   false,                  // optional
        "config": {
           // for smtp: host, port, username?, password?, tls_mode
           // for ses:  region, access_key_id, secret_access_key, configuration_set?
        }
      }
    """
    user, project, _ = await _project_manage_scope(request, slug)
    body = await request.json()

    sender_type = (body.get("type") or "").strip().lower()
    if sender_type not in VALID_EMAIL_SENDER_TYPES:
        raise HTTPException(400, f"Invalid sender type (expected one of {sorted(VALID_EMAIL_SENDER_TYPES)})")

    label = (body.get("label") or "").strip()
    from_address = (body.get("from_address") or "").strip()
    if not label:
        raise HTTPException(400, "label is required")
    if not from_address or "@" not in from_address:
        raise HTTPException(400, "from_address must be a valid email address")

    config = _email_sender_config_payload(sender_type, body)
    from_name = (body.get("from_name") or "").strip() or None
    make_default = bool(body.get("is_default", False))

    async with app_state.db_session_factory() as db:
        # If this sender is being marked default, clear any existing default
        # in the same transaction so the partial unique index is never
        # violated mid-commit.
        if make_default:
            await db.execute(
                update(ProjectEmailSender)
                .where(
                    ProjectEmailSender.project_id == project.id,
                    ProjectEmailSender.is_default.is_(True),
                )
                .values(is_default=False)
            )

        # If this is the project's first sender, force-default it — there's
        # no point having a sender that nothing can use.
        existing_count = await db.scalar(
            select(func.count())
            .select_from(ProjectEmailSender)
            .where(
                ProjectEmailSender.project_id == project.id,
            )
        )
        if not existing_count:
            make_default = True

        row = ProjectEmailSender(
            project_id=project.id,
            label=label,
            type=sender_type,
            from_address=from_address,
            from_name=from_name,
            is_default=make_default,
            created_by_user_id=uuid.UUID(user["user_id"]),
        )
        row.set_config(config)
        db.add(row)
        await db.commit()
        await db.refresh(row)

    from app.notifications.email import sender_display_summary

    return JSONResponse(
        {"success": True, "sender": sender_display_summary(row)},
        status_code=201,
    )


@router.patch("/api/project/{slug}/email-senders/{sender_id}")
async def update_email_sender(request: Request, slug: str, sender_id: str):
    """Update an existing email sender.

    Any omitted field is left unchanged. ``config`` is only replaced if
    the body includes a ``config`` key — that lets the settings UI update
    the label without asking the user to re-type credentials.
    """
    _, project, _ = await _project_manage_scope(request, slug)

    try:
        sid = uuid.UUID(sender_id)
    except ValueError:
        raise HTTPException(400, "Invalid sender_id")

    body = await request.json()

    async with app_state.db_session_factory() as db:
        result = await db.execute(
            select(ProjectEmailSender).where(
                ProjectEmailSender.id == sid,
                ProjectEmailSender.project_id == project.id,
            )
        )
        row = result.scalar_one_or_none()
        if not row:
            raise HTTPException(404, "Email sender not found")

        if "label" in body:
            label = (body.get("label") or "").strip()
            if not label:
                raise HTTPException(400, "label cannot be empty")
            row.label = label

        if "from_address" in body:
            from_address = (body.get("from_address") or "").strip()
            if not from_address or "@" not in from_address:
                raise HTTPException(400, "from_address must be a valid email")
            row.from_address = from_address

        if "from_name" in body:
            row.from_name = (body.get("from_name") or "").strip() or None

        if "is_default" in body and bool(body["is_default"]) and not row.is_default:
            # Clear the current default first (same transaction → safe for
            # the partial unique index)
            await db.execute(
                update(ProjectEmailSender)
                .where(
                    ProjectEmailSender.project_id == project.id,
                    ProjectEmailSender.is_default.is_(True),
                )
                .values(is_default=False)
            )
            row.is_default = True
        elif "is_default" in body and not body["is_default"]:
            # Don't allow unsetting the only default — leaves the project
            # with no outbound path and bricks scheduled reports silently.
            if row.is_default:
                remaining = await db.scalar(
                    select(func.count())
                    .select_from(ProjectEmailSender)
                    .where(
                        ProjectEmailSender.project_id == project.id,
                        ProjectEmailSender.id != row.id,
                    )
                )
                if not remaining:
                    raise HTTPException(
                        400,
                        "Cannot unset default: this is the only sender. "
                        "Add another sender first or delete this one.",
                    )
                row.is_default = False

        if "config" in body:
            # Rotate credentials. Type cannot change on update — to switch
            # from SMTP to SES, delete and create a new sender.
            new_cfg = _email_sender_config_payload(row.type, body)
            row.set_config(new_cfg)
            # Credentials changed, so any prior "last tested" result is
            # now meaningless. Reset it.
            row.last_tested_at = None
            row.last_test_status = None
            row.last_test_error = None

        await db.commit()
        await db.refresh(row)

    from app.notifications.email import sender_display_summary

    return JSONResponse({"success": True, "sender": sender_display_summary(row)})


@router.delete("/api/project/{slug}/email-senders/{sender_id}")
async def delete_email_sender(request: Request, slug: str, sender_id: str):
    """Delete an email sender.

    If this was the project's default and other senders exist, the most
    recently-created survivor is promoted to default in the same
    transaction so the project never ends up with zero defaults.
    """
    _, project, _ = await _project_manage_scope(request, slug)

    try:
        sid = uuid.UUID(sender_id)
    except ValueError:
        raise HTTPException(400, "Invalid sender_id")

    async with app_state.db_session_factory() as db:
        result = await db.execute(
            select(ProjectEmailSender).where(
                ProjectEmailSender.id == sid,
                ProjectEmailSender.project_id == project.id,
            )
        )
        row = result.scalar_one_or_none()
        if not row:
            raise HTTPException(404, "Email sender not found")

        was_default = bool(row.is_default)
        await db.delete(row)

        if was_default:
            # Promote the next newest sender to default so the project
            # doesn't end up with zero defaults after the delete.
            await db.flush()  # so ``row`` is actually gone inside this txn
            result = await db.execute(
                select(ProjectEmailSender)
                .where(ProjectEmailSender.project_id == project.id)
                .order_by(ProjectEmailSender.created_at.desc())
                .limit(1)
            )
            successor = result.scalar_one_or_none()
            if successor:
                successor.is_default = True

        await db.commit()

    return JSONResponse({"success": True})


async def _persist_test_result(
    sender_id: uuid.UUID,
    ok: bool,
    error: str | None,
) -> None:
    """Record the outcome of a test on the sender row so the UI can show
    a last-tested indicator. Best-effort — swallowed failures don't
    affect the test result reported to the caller."""
    try:
        async with app_state.db_session_factory() as db:
            await db.execute(
                update(ProjectEmailSender)
                .where(ProjectEmailSender.id == sender_id)
                .values(
                    last_tested_at=datetime.utcnow(),
                    last_test_status="success" if ok else "failed",
                    last_test_error=None if ok else (error or "")[:1000],
                )
            )
            await db.commit()
    except Exception as exc:  # pragma: no cover
        logger.warning("Failed to persist email sender test result: %s", exc)


@router.post("/api/project/{slug}/email-senders/{sender_id}/test")
async def test_email_sender(request: Request, slug: str, sender_id: str):
    """Test a saved email sender.

    Body:
      {
        "to":        "me@example.com",   // optional — if set, sends a real email
        "verify_only": false              // optional — default False
      }

    Behaviour:
      * If ``to`` is omitted or ``verify_only`` is true → just run
        ``sender.verify()`` (connect / credential check, no DATA phase).
      * Otherwise → send a small test email to ``to`` with subject
        "Fluxito test email".

    Either way, ``last_tested_at`` and ``last_test_status`` on the row
    are updated so the settings UI can show a fresh "tested OK" badge.
    """
    _, project, _ = await _project_manage_scope(request, slug)

    try:
        sid = uuid.UUID(sender_id)
    except ValueError:
        raise HTTPException(400, "Invalid sender_id")

    body = await request.json() if await _maybe_has_body(request) else {}
    to_addr = (body.get("to") or "").strip() if isinstance(body, dict) else ""
    verify_only = bool(body.get("verify_only", False)) if isinstance(body, dict) else False

    async with app_state.db_session_factory() as db:
        result = await db.execute(
            select(ProjectEmailSender).where(
                ProjectEmailSender.id == sid,
                ProjectEmailSender.project_id == project.id,
            )
        )
        row = result.scalar_one_or_none()
        if not row:
            raise HTTPException(404, "Email sender not found")

        from app.notifications.email import (
            EmailMessage,
            EmailSendError,
            build_sender_from_row,
        )

        try:
            sender = build_sender_from_row(row)
        except EmailSendError as exc:
            await _persist_test_result(sid, ok=False, error=str(exc))
            return JSONResponse(
                {"success": False, "error": exc.message, "detail": exc.detail},
                status_code=400,
            )

    # Drop the DB session before potentially-slow network calls
    try:
        if to_addr and not verify_only:
            msg = EmailMessage(
                to=[to_addr],
                subject=f"Fluxito · test email from {project.name}",
                text_body=(
                    "This is a test email from Fluxito.\n\n"
                    f"Project: {project.name}\n"
                    f"Sender:  {row.label} ({row.type})\n\n"
                    "If you received this, your sender credentials are working."
                ),
                html_body=(
                    "<p>This is a <strong>test email</strong> from Fluxito.</p>"
                    f"<p>Project: <code>{project.name}</code><br>"
                    f"Sender: <code>{row.label}</code> ({row.type})</p>"
                    "<p>If you received this, your sender credentials are working.</p>"
                ),
            )
            result = await sender.send(msg)
        else:
            result = await sender.verify()
    except EmailSendError as exc:
        await _persist_test_result(sid, ok=False, error=str(exc))
        return JSONResponse(
            {"success": False, "error": exc.message, "detail": exc.detail},
            status_code=400,
        )
    except Exception as exc:
        logger.exception("Email sender test raised unexpected exception")
        await _persist_test_result(sid, ok=False, error=str(exc)[:500])
        return JSONResponse(
            {"success": False, "error": "Unexpected error during test", "detail": str(exc)[:300]},
            status_code=500,
        )

    await _persist_test_result(sid, ok=True, error=None)
    return JSONResponse(
        {
            "success": True,
            "sent": bool(to_addr and not verify_only),
            "message_id": result.message_id,
        }
    )


async def _maybe_has_body(request: Request) -> bool:
    """Return True if the incoming request has a non-empty JSON body.

    Works around the fact that ``request.json()`` raises on empty bodies
    — the test-send endpoint treats a missing body as "verify only".
    """
    try:
        raw = await request.body()
    except Exception:
        return False
    return bool(raw and raw.strip())


# ═══════════════════════════════════════════════════════════════════════════
# Slack webhooks (Project Settings → Notifications tab)
# ═══════════════════════════════════════════════════════════════════════════
#
# Mirrors the email-sender routes but simpler: no "default" concept, and
# the encrypted blob only stores the URL (no display columns to merge).
# Webhooks are independent destinations that schedules point at by id.
# ═══════════════════════════════════════════════════════════════════════════


async def _list_slack_webhooks(project_id: uuid.UUID) -> list[dict]:
    """Return redacted webhook summaries for settings UI rendering."""
    from app.notifications.slack import list_webhook_senders, webhook_display_summary

    async with app_state.db_session_factory() as db:
        rows = await list_webhook_senders(db, project_id)

    return [webhook_display_summary(r) for r in rows]


async def _persist_slack_test_result(webhook_id: uuid.UUID, *, ok: bool, error: str | None) -> None:
    """Best-effort update of a webhook's last_tested_* columns. Swallows errors."""
    try:
        async with app_state.db_session_factory() as db:
            from datetime import datetime

            row = await db.get(ProjectSlackWebhook, webhook_id)
            if not row:
                return
            row.last_tested_at = datetime.utcnow()
            row.last_test_status = "ok" if ok else "error"
            row.last_test_error = (error or "")[:500] if not ok else ""
            await db.commit()
    except Exception:
        logger.exception("Failed to persist Slack webhook test result id=%s", webhook_id)


@router.get("/api/project/{slug}/slack-webhooks")
async def list_slack_webhooks(request: Request, slug: str):
    """List all Slack webhooks for a project (owner/admin only)."""
    _user, project, _membership = await _project_manage_scope(request, slug)
    webhooks = await _list_slack_webhooks(project.id)
    return JSONResponse({"webhooks": webhooks})


@router.post("/api/project/{slug}/slack-webhooks")
async def create_slack_webhook(request: Request, slug: str):
    """Create a new Slack webhook for the project.

    Request body:
      {
        "label":       "#marketing-daily",      # required — human label
        "webhook_url": "https://hooks.slack.com/services/…"  # required
      }

    Validates the URL shape by constructing a ``WebhookSender`` (which
    raises ``SlackSendError`` on bad prefixes) before persisting.
    """
    from app.notifications.slack import WebhookSender
    from app.notifications.slack.base import SlackSendError

    user, project, _membership = await _project_manage_scope(request, slug)

    body = await request.json()
    label = (body.get("label") or "").strip()
    url = (body.get("webhook_url") or "").strip()
    if not label:
        return JSONResponse({"success": False, "error": "label is required"}, status_code=400)
    if not url:
        return JSONResponse({"success": False, "error": "webhook_url is required"}, status_code=400)

    # Validate URL shape early — don't persist garbage.
    try:
        WebhookSender({"webhook_url": url})
    except SlackSendError as exc:
        return JSONResponse(
            {"success": False, "error": exc.message, "detail": exc.detail or ""},
            status_code=400,
        )

    async with app_state.db_session_factory() as db:
        row = ProjectSlackWebhook(
            project_id=project.id,
            label=label[:255],
            created_by_user_id=uuid.UUID(user["user_id"]),
        )
        row.set_webhook_url(url)
        db.add(row)
        await db.commit()
        await db.refresh(row)
        sid = row.id

    from app.notifications.slack import webhook_display_summary

    async with app_state.db_session_factory() as db:
        fresh = await db.get(ProjectSlackWebhook, sid)
        summary = webhook_display_summary(fresh) if fresh else {"id": str(sid)}
    return JSONResponse({"success": True, "id": str(sid), "webhook": summary})


@router.patch("/api/project/{slug}/slack-webhooks/{webhook_id}")
async def update_slack_webhook(request: Request, slug: str, webhook_id: str):
    """Update a Slack webhook's label or URL.

    Request body (all fields optional; at least one required):
      {"label": "#new-channel", "webhook_url": "https://hooks.slack.com/…"}

    Rotating the URL resets ``last_tested_at`` / ``last_test_status`` /
    ``last_test_error`` because the previous test result is no longer
    meaningful.
    """
    from app.notifications.slack import WebhookSender
    from app.notifications.slack.base import SlackSendError

    _user, project, _membership = await _project_manage_scope(request, slug)

    try:
        wid = uuid.UUID(webhook_id)
    except Exception:
        return JSONResponse({"success": False, "error": "invalid webhook id"}, status_code=400)

    body = await request.json()
    new_label = body.get("label")
    new_url = body.get("webhook_url")
    if new_label is None and new_url is None:
        return JSONResponse(
            {"success": False, "error": "no fields to update"},
            status_code=400,
        )

    if new_url is not None:
        # Validate before we commit anything.
        try:
            WebhookSender({"webhook_url": str(new_url).strip()})
        except SlackSendError as exc:
            return JSONResponse(
                {"success": False, "error": exc.message, "detail": exc.detail or ""},
                status_code=400,
            )

    async with app_state.db_session_factory() as db:
        row = await db.get(ProjectSlackWebhook, wid)
        if not row or row.project_id != project.id:
            return JSONResponse(
                {"success": False, "error": "webhook not found"},
                status_code=404,
            )

        if new_label is not None:
            row.label = (str(new_label).strip() or row.label)[:255]
        if new_url is not None:
            row.set_webhook_url(str(new_url).strip())
            # Old test result no longer applies to the new URL
            row.last_tested_at = None
            row.last_test_status = None
            row.last_test_error = None

        await db.commit()

    return JSONResponse({"success": True})


@router.delete("/api/project/{slug}/slack-webhooks/{webhook_id}")
async def delete_slack_webhook(request: Request, slug: str, webhook_id: str):
    """Delete a Slack webhook. Any schedule pointing at it will need to be reassigned.

    We do NOT cascade-disable dependent schedules here — the scheduler
    handles missing-destination cases at send time by marking the run
    failed. Letting the user see the broken schedule surface in the UI
    and fix it explicitly is less surprising than silent auto-disable.
    """
    _user, project, _membership = await _project_manage_scope(request, slug)

    try:
        wid = uuid.UUID(webhook_id)
    except Exception:
        return JSONResponse({"success": False, "error": "invalid webhook id"}, status_code=400)

    async with app_state.db_session_factory() as db:
        row = await db.get(ProjectSlackWebhook, wid)
        if not row or row.project_id != project.id:
            return JSONResponse(
                {"success": False, "error": "webhook not found"},
                status_code=404,
            )
        await db.delete(row)
        await db.commit()

    return JSONResponse({"success": True})


@router.post("/api/project/{slug}/slack-webhooks/{webhook_id}/test")
async def test_slack_webhook(request: Request, slug: str, webhook_id: str):
    """Post a small connection-check message to the webhook.

    Body (all optional):
      {
        "custom_text": "Your analytics are ready — this is a test."  # overrides default blurb
      }

    Unlike email ``verify()``, there is no "no-send" mode — Slack
    webhooks don't have a cheap credential-ping. The test always posts
    a visible message (~2 blocks) that a user can safely ignore.
    """
    from app.notifications.slack import (
        build_webhook_sender_from_row,
        render_simple_blocks,
    )
    from app.notifications.slack.base import SlackMessage, SlackSendError

    _user, project, _membership = await _project_manage_scope(request, slug)

    try:
        wid = uuid.UUID(webhook_id)
    except Exception:
        return JSONResponse({"success": False, "error": "invalid webhook id"}, status_code=400)

    body: dict = {}
    if await _maybe_has_body(request):
        try:
            body = await request.json()
        except Exception:
            body = {}
    custom_text = (body.get("custom_text") or "").strip()

    async with app_state.db_session_factory() as db:
        row = await db.get(ProjectSlackWebhook, wid)
        if not row or row.project_id != project.id:
            return JSONResponse(
                {"success": False, "error": "webhook not found"},
                status_code=404,
            )
        try:
            sender = build_webhook_sender_from_row(row)
        except SlackSendError as exc:
            await _persist_slack_test_result(wid, ok=False, error=exc.message)
            return JSONResponse(
                {"success": False, "error": exc.message, "detail": exc.detail or ""},
                status_code=400,
            )

    blurb = custom_text or (
        f"Fluxito — test message from project *{project.name}*. "
        "If you can read this, your webhook is wired up."
    )
    msg = SlackMessage(
        text=blurb,
        blocks=render_simple_blocks(
            title="Fluxito — connection check",
            body_md=blurb,
            footer="Sent from Project Settings → Notifications.",
        ),
    )

    try:
        await sender.send(msg)
    except SlackSendError as exc:
        await _persist_slack_test_result(wid, ok=False, error=f"{exc.message}: {exc.detail or ''}")
        return JSONResponse(
            {"success": False, "error": exc.message, "detail": exc.detail or ""},
            status_code=400,
        )
    except Exception as exc:
        logger.exception("Slack webhook test raised unexpected exception")
        await _persist_slack_test_result(wid, ok=False, error=str(exc)[:500])
        return JSONResponse(
            {"success": False, "error": "Unexpected error during test", "detail": str(exc)[:300]},
            status_code=500,
        )

    await _persist_slack_test_result(wid, ok=True, error=None)
    return JSONResponse({"success": True, "sent": True})


# ---------------------------------------------------------------------------
# Dashboard settings
# ---------------------------------------------------------------------------

_STYLE_PRESETS = {
    "editorial_warm": {
        "preset": "editorial_warm",
        "primary": "#2d4a1e",
        "accent": "#c4421a",
        "background": "#faf6ee",
        "font_heading": "Playfair Display",
        "font_body": "Inter",
        "chart_palette": ["#2d4a1e", "#c4421a", "#8b7355", "#4a7c59"],
    },
    "dark_analytics": {
        "preset": "dark_analytics",
        "primary": "#6366f1",
        "accent": "#06b6d4",
        "background": "#0f172a",
        "font_heading": "Inter",
        "font_body": "Inter",
        "chart_palette": ["#6366f1", "#06b6d4", "#f59e0b", "#10b981"],
    },
    "clean_minimal": {
        "preset": "clean_minimal",
        "primary": "#18181b",
        "accent": "#2563eb",
        "background": "#ffffff",
        "font_heading": "Inter",
        "font_body": "Inter",
        "chart_palette": ["#18181b", "#2563eb", "#64748b", "#94a3b8"],
    },
    "bold_data": {
        "preset": "bold_data",
        "primary": "#7c3aed",
        "accent": "#db2777",
        "background": "#fafafa",
        "font_heading": "Inter",
        "font_body": "Inter",
        "chart_palette": ["#7c3aed", "#db2777", "#f59e0b", "#059669"],
    },
}


def _validate_style_config(style_config: dict | None) -> dict | None:
    """Validate and normalise a dashboard_style_config payload.

    If preset is a known preset name, expand to full config.
    If preset is 'custom', accept as-is (caller provides all fields).
    If None, returns None (means Claude uses own judgment).
    """
    if style_config is None:
        return None
    preset_name = style_config.get("preset")
    if preset_name == "custom":
        return style_config
    if preset_name not in _STYLE_PRESETS:
        raise ValueError(f"Unknown preset: {preset_name!r}")
    return _STYLE_PRESETS[preset_name]


@router.patch("/api/project/{slug}/dashboard-settings")
async def update_dashboard_settings(request: Request, slug: str):
    """Save the dashboard style config for a project.

    Request body:
      style_config: null | { preset: "editorial_warm"|...|"custom", ...fields }
    """
    user = await _resolve_user(request)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    body = await request.json()

    try:
        style_config = _validate_style_config(body.get("style_config"))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    async with app_state.db_session_factory() as db:
        result = await db.execute(select(Project).where(Project.slug == slug))
        proj = result.scalar_one_or_none()
        if not proj:
            return JSONResponse({"error": "Not found"}, status_code=404)

        # Verify requester is owner or admin
        uid = uuid.UUID(user["user_id"])
        membership = await _get_membership(proj.id, uid)
        if not membership or membership.role not in (ROLE_OWNER, ROLE_ADMIN):
            return JSONResponse({"error": "Forbidden"}, status_code=403)

        proj.dashboard_style_config = style_config
        await db.commit()

    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# RBAC — Role CRUD (Task 12), Member role assignment (Task 13), Toggle (Task 14)
# ---------------------------------------------------------------------------

from app.auth.permissions import (
    PermissionValidationError,
    invalidate_permissions_cache,
    normalize_permissions,
)
from app.models.project import CAN_MANAGE_ROLES
from app.models.role import MemberRole, Role


class _RoleIn(BaseModel):
    name: str
    description: str | None = None
    permissions: dict = {}


class _MemberRolesIn(BaseModel):
    role_ids: list[str] = []


class _RbacToggleIn(BaseModel):
    enabled: bool


def _role_out(role: Role) -> dict:
    return {
        "id": str(role.id),
        "name": role.name,
        "description": role.description,
        "permissions": role.permissions,
        "is_active": role.is_active,
    }


# --- Task 12: GET roles ---


@router.get("/api/project/{slug}/roles")
async def list_roles(request: Request, slug: str):
    """List active custom roles for a project (admin/owner only)."""
    user = await _resolve_user(request)
    if not user:
        raise HTTPException(401, "Not authenticated")

    project = await _get_project_by_slug(slug)
    if not project:
        raise HTTPException(404, "Project not found")

    uid = uuid.UUID(user["user_id"])
    membership = await _get_membership(project.id, uid)
    if not membership or membership.role not in CAN_MANAGE_ROLES:
        raise HTTPException(403, "Only owners and admins can manage roles")

    async with app_state.db_session_factory() as db:
        result = await db.execute(select(Role).where(Role.project_id == project.id, Role.is_active == True))
        roles = result.scalars().all()

    return JSONResponse([_role_out(r) for r in roles])


# --- Task 12: POST create role ---


@router.post("/api/project/{slug}/roles")
async def create_role(request: Request, slug: str, body: _RoleIn):
    """Create a custom role for the project (admin/owner only)."""
    from sqlalchemy.exc import IntegrityError

    user = await _resolve_user(request)
    if not user:
        raise HTTPException(401, "Not authenticated")

    project = await _get_project_by_slug(slug)
    if not project:
        raise HTTPException(404, "Project not found")

    uid = uuid.UUID(user["user_id"])
    membership = await _get_membership(project.id, uid)
    if not membership or membership.role not in CAN_MANAGE_ROLES:
        raise HTTPException(403, "Only owners and admins can manage roles")

    try:
        normalized = normalize_permissions(body.permissions)
    except PermissionValidationError as exc:
        raise HTTPException(400, str(exc))

    async with app_state.db_session_factory() as db:
        role = Role(
            project_id=project.id,
            name=body.name,
            description=body.description,
            permissions=normalized,
            created_by=uid,
        )
        db.add(role)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(409, f"A role named '{body.name}' already exists in this project")

    return JSONResponse(_role_out(role))


# --- Task 12: PATCH update role ---


@router.patch("/api/project/{slug}/roles/{role_id}")
async def update_role(request: Request, slug: str, role_id: str, body: _RoleIn):
    """Update a role's name/description/permissions (admin/owner only)."""
    user = await _resolve_user(request)
    if not user:
        raise HTTPException(401, "Not authenticated")

    project = await _get_project_by_slug(slug)
    if not project:
        raise HTTPException(404, "Project not found")

    uid = uuid.UUID(user["user_id"])
    membership = await _get_membership(project.id, uid)
    if not membership or membership.role not in CAN_MANAGE_ROLES:
        raise HTTPException(403, "Only owners and admins can manage roles")

    try:
        normalized = normalize_permissions(body.permissions)
    except PermissionValidationError as exc:
        raise HTTPException(400, str(exc))

    async with app_state.db_session_factory() as db:
        result = await db.execute(
            select(Role).where(Role.id == uuid.UUID(role_id), Role.project_id == project.id)
        )
        role = result.scalar_one_or_none()
        if not role:
            raise HTTPException(404, "Role not found")

        role.name = body.name
        role.description = body.description
        role.permissions = normalized

        # Collect users holding this role so we can invalidate their caches
        mr_result = await db.execute(
            select(ProjectMember.user_id)
            .join(MemberRole, MemberRole.project_member_id == ProjectMember.id)
            .where(MemberRole.role_id == role.id)
        )
        affected_user_ids = [str(row[0]) for row in mr_result.fetchall()]

        await db.commit()

    for affected_uid in affected_user_ids:
        await invalidate_permissions_cache(affected_uid, str(project.id))

    return JSONResponse(_role_out(role))


# --- Task 12: DELETE (soft) role ---


@router.delete("/api/project/{slug}/roles/{role_id}")
async def delete_role(request: Request, slug: str, role_id: str):
    """Soft-delete a role and remove its MemberRole rows (admin/owner only)."""
    user = await _resolve_user(request)
    if not user:
        raise HTTPException(401, "Not authenticated")

    project = await _get_project_by_slug(slug)
    if not project:
        raise HTTPException(404, "Project not found")

    uid = uuid.UUID(user["user_id"])
    membership = await _get_membership(project.id, uid)
    if not membership or membership.role not in CAN_MANAGE_ROLES:
        raise HTTPException(403, "Only owners and admins can manage roles")

    async with app_state.db_session_factory() as db:
        result = await db.execute(
            select(Role).where(Role.id == uuid.UUID(role_id), Role.project_id == project.id)
        )
        role = result.scalar_one_or_none()
        if not role:
            raise HTTPException(404, "Role not found")

        # Collect affected users before deleting assignments
        mr_result = await db.execute(
            select(ProjectMember.user_id)
            .join(MemberRole, MemberRole.project_member_id == ProjectMember.id)
            .where(MemberRole.role_id == role.id)
        )
        affected_user_ids = [str(row[0]) for row in mr_result.fetchall()]

        # Delete MemberRole rows
        from sqlalchemy import delete as sa_delete

        await db.execute(sa_delete(MemberRole).where(MemberRole.role_id == role.id))

        # Soft-delete the role
        role.is_active = False
        await db.commit()

    for affected_uid in affected_user_ids:
        await invalidate_permissions_cache(affected_uid, str(project.id))

    return JSONResponse({"ok": True, "id": role_id})


# --- Task 13: PUT member role assignment ---


@router.put("/api/project/{slug}/members/{member_id}/roles")
async def assign_member_roles(request: Request, slug: str, member_id: str, body: _MemberRolesIn):
    """Replace a member's role assignments (admin/owner only)."""
    from sqlalchemy import delete as sa_delete

    user = await _resolve_user(request)
    if not user:
        raise HTTPException(401, "Not authenticated")

    project = await _get_project_by_slug(slug)
    if not project:
        raise HTTPException(404, "Project not found")

    uid = uuid.UUID(user["user_id"])
    membership = await _get_membership(project.id, uid)
    if not membership or membership.role not in CAN_MANAGE_ROLES:
        raise HTTPException(403, "Only owners and admins can assign roles")

    role_uuids = [uuid.UUID(rid) for rid in body.role_ids]

    async with app_state.db_session_factory() as db:
        # Load target ProjectMember
        result = await db.execute(
            select(ProjectMember).where(
                ProjectMember.id == uuid.UUID(member_id),
                ProjectMember.project_id == project.id,
            )
        )
        target_member = result.scalar_one_or_none()
        if not target_member:
            raise HTTPException(404, "Member not found")

        # Validate all role_ids belong to this project and are active
        if role_uuids:
            valid_result = await db.execute(
                select(Role.id).where(
                    Role.id.in_(role_uuids),
                    Role.project_id == project.id,
                    Role.is_active == True,
                )
            )
            valid_ids = {row[0] for row in valid_result.fetchall()}
            for rid in role_uuids:
                if rid not in valid_ids:
                    raise HTTPException(400, f"Role {rid} is not a valid active role in this project")

        # Replace assignments
        await db.execute(sa_delete(MemberRole).where(MemberRole.project_member_id == target_member.id))
        for rid in role_uuids:
            db.add(MemberRole(project_member_id=target_member.id, role_id=rid, assigned_by=uid))

        target_user_id = str(target_member.user_id)
        await db.commit()

    await invalidate_permissions_cache(target_user_id, str(project.id))

    return JSONResponse({"ok": True, "role_ids": [str(r) for r in role_uuids]})


# --- Task 14: PUT RBAC toggle ---


@router.put("/api/project/{slug}/settings/rbac")
async def toggle_rbac(request: Request, slug: str, body: _RbacToggleIn):
    """Enable or disable RBAC for the project (admin/owner only)."""
    user = await _resolve_user(request)
    if not user:
        raise HTTPException(401, "Not authenticated")

    project = await _get_project_by_slug(slug)
    if not project:
        raise HTTPException(404, "Project not found")

    uid = uuid.UUID(user["user_id"])
    membership = await _get_membership(project.id, uid)
    if not membership or membership.role not in CAN_MANAGE_ROLES:
        raise HTTPException(403, "Only owners and admins can change RBAC settings")

    async with app_state.db_session_factory() as db:
        result = await db.execute(select(Project).where(Project.id == project.id))
        proj = result.scalar_one()
        proj.rbac_enabled = body.enabled

        # Collect all active member user_ids for cache invalidation
        members_result = await db.execute(
            select(ProjectMember.user_id).where(
                ProjectMember.project_id == project.id,
                ProjectMember.is_active == True,
            )
        )
        all_user_ids = [str(row[0]) for row in members_result.fetchall()]

        await db.commit()

    for affected_uid in all_user_ids:
        await invalidate_permissions_cache(affected_uid, str(project.id))

    return JSONResponse({"ok": True, "rbac_enabled": body.enabled})
