"""Super-admin instance panel: users + access requests."""

from __future__ import annotations

import logging
import re as _re
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import desc, func, select

import app.app_state as app_state
from app.api.google_oauth_routes import _resolve_user_ctx
from app.models.audit import ToolCallAudit
from app.models.project import Project
from app.models.user import User
from app.templating import render

logger = logging.getLogger(__name__)
router = APIRouter()

_ACCENT_RE = _re.compile(r"^#?[0-9a-zA-Z]{3,8}$")
_GTM_RE = _re.compile(r"^GTM-[A-Z0-9_-]{4,24}$")


def _utc_now_naive() -> datetime:
    """Return current UTC time as naive datetime for PostgreSQL DateTime columns."""
    return datetime.now(UTC).replace(tzinfo=None)


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    """Super-admin instance panel. Redirects to homepage if unauthenticated or not super-admin."""
    user_ctx = await _resolve_user_ctx(request)
    if not user_ctx:
        return RedirectResponse(url="/", status_code=302)

    async with app_state.db_session_factory() as db:
        u = (
            await db.execute(select(User).where(User.id == uuid.UUID(user_ctx.user_id)))
        ).scalar_one_or_none()
        if not u or not u.is_superadmin:
            return RedirectResponse(url="/home", status_code=302)

    from app.api.google_oauth_routes import _load_user_view
    from app.settings_service import access_approval_required

    user_view = await _load_user_view(user_ctx)
    gate_enabled = await access_approval_required()
    return render(request, "admin.html", {"user": user_view, "active": "admin", "gate_enabled": gate_enabled})


async def require_superadmin(request: Request) -> dict:
    """Resolve the current user and require the instance super-admin flag.

    Returns a small dict {id, email, is_superadmin}. Raises 401 if not
    authenticated, 403 if not a super-admin.
    """
    user_ctx = await _resolve_user_ctx(request)
    if not user_ctx:
        raise HTTPException(401, "Not authenticated")
    async with app_state.db_session_factory() as db:
        u = (
            await db.execute(select(User).where(User.id == uuid.UUID(user_ctx.user_id)))
        ).scalar_one_or_none()
        if not u or not u.is_superadmin or not u.is_active or u.deactivated_by_admin:
            raise HTTPException(403, "Super-admin only")
        return {"id": str(u.id), "email": u.email, "is_superadmin": True}


@router.get("/api/admin/users")
async def admin_list_users(request: Request):
    await require_superadmin(request)
    async with app_state.db_session_factory() as db:
        rows = (await db.execute(select(User).order_by(User.created_at.asc()))).scalars().all()
        users = [
            {
                "id": str(u.id),
                "email": u.email,
                "display_name": u.display_name,
                "is_active": u.is_active,
                "is_superadmin": u.is_superadmin,
                "created_at": u.created_at.isoformat() if u.created_at else None,
            }
            for u in rows
        ]
    return JSONResponse({"users": users})


@router.patch("/api/admin/users/{user_id}/active")
async def admin_set_active(request: Request, user_id: str):
    me = await require_superadmin(request)
    body = await request.json()
    is_active = bool(body.get("is_active"))
    if user_id == me["id"] and not is_active:
        raise HTTPException(400, "You cannot deactivate your own account.")
    async with app_state.db_session_factory() as db:
        u = await db.get(User, uuid.UUID(user_id))
        if not u:
            raise HTTPException(404, "User not found")
        if u.is_superadmin and not is_active:
            count = await db.scalar(
                select(func.count())
                .select_from(User)
                .where(User.is_superadmin == True, User.is_active == True)
            )
            if count is not None and count <= 1:
                raise HTTPException(400, "Cannot deactivate the last active super-admin.")
        u.is_active = is_active
        # Admin deactivation locks every session/token and can't be undone by
        # the user; reactivating here lifts it.
        u.deactivated_by_admin = not is_active
        await db.commit()
    from app.auth import account_status
    from app.auth.mcp_session_manager import invalidate_user_context_cache

    account_status.forget(user_id)
    await invalidate_user_context_cache(user_id)
    return JSONResponse({"success": True})


@router.patch("/api/admin/users/{user_id}/superadmin")
async def admin_set_superadmin(request: Request, user_id: str):
    me = await require_superadmin(request)
    body = await request.json()
    is_superadmin = bool(body.get("is_superadmin"))
    async with app_state.db_session_factory() as db:
        u = await db.get(User, uuid.UUID(user_id))
        if not u:
            raise HTTPException(404, "User not found")
        if u.is_superadmin and not is_superadmin:
            count = await db.scalar(select(func.count()).select_from(User).where(User.is_superadmin == True))
            if count is not None and count <= 1:
                raise HTTPException(400, "Cannot remove the last super-admin.")
        u.is_superadmin = is_superadmin
        await db.commit()
    return JSONResponse({"success": True})


@router.get("/api/admin/access-requests")
async def admin_list_access_requests(request: Request):
    await require_superadmin(request)
    status = request.query_params.get("status", "pending")
    from app.models.access_request import AccessRequest

    async with app_state.db_session_factory() as db:
        rows = (
            (
                await db.execute(
                    select(AccessRequest)
                    .where(AccessRequest.status == status)
                    .order_by(AccessRequest.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        items = [
            {
                "id": str(r.id),
                "name": r.name,
                "email": r.email,
                "use_case": r.use_case,
                "status": r.status,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    return JSONResponse({"requests": items})


@router.post("/api/admin/access-requests/{req_id}/approve")
async def admin_approve_access_request(request: Request, req_id: str):
    me = await require_superadmin(request)
    from app.auth.email_auth import generate_temp_password, hash_password
    from app.models.access_request import AccessRequest

    async with app_state.db_session_factory() as db:
        r = await db.get(AccessRequest, uuid.UUID(req_id))
        if not r:
            raise HTTPException(404, "Request not found")
        if r.status != "pending":
            raise HTTPException(400, f"Request already {r.status}.")
        req_email = r.email
        req_name = r.name

        temp_password = None
        existing = (await db.execute(select(User).where(User.email == req_email))).scalar_one_or_none()
        if existing is None:
            temp_password = generate_temp_password()
            new_user = User(
                email=req_email,
                display_name=req_name,
                password_hash=hash_password(temp_password),
                email_verified=True,
                auth_provider="email",
            )
            db.add(new_user)
            await db.flush()
            new_uid = new_user.id
        else:
            # Account already exists — never reset an existing password (takeover guard).
            new_uid = existing.id

        r.status = "approved"
        r.reviewed_by = uuid.UUID(me["id"])
        r.reviewed_at = datetime.now(UTC)
        await db.commit()

    try:
        from app.api.project_routes import ensure_default_project

        await ensure_default_project(new_uid, req_name, req_email)
    except Exception:
        logger.warning("ensure_default_project failed after approval", exc_info=True)

    return JSONResponse({"success": True, "email": req_email, "temp_password": temp_password})


@router.post("/api/admin/access-requests/{req_id}/reject")
async def admin_reject_access_request(request: Request, req_id: str):
    me = await require_superadmin(request)
    from app.models.access_request import AccessRequest

    async with app_state.db_session_factory() as db:
        r = await db.get(AccessRequest, uuid.UUID(req_id))
        if not r:
            raise HTTPException(404, "Request not found")
        r.status = "rejected"
        r.reviewed_by = uuid.UUID(me["id"])
        r.reviewed_at = datetime.now(UTC)
        await db.commit()
    return JSONResponse({"success": True})


@router.get("/api/admin/settings/rate-limits")
async def admin_get_rate_limits(request: Request):
    await require_superadmin(request)
    from app.settings_service import get_runtime_setting

    async with app_state.db_session_factory() as db:
        per_min = int(await get_runtime_setting(db, "rate_limit_per_min", default=60))
        per_hour = int(await get_runtime_setting(db, "rate_limit_per_hour", default=1000))
        per_day = int(await get_runtime_setting(db, "rate_limit_per_day", default=10000))
    return JSONResponse({"per_min": per_min, "per_hour": per_hour, "per_day": per_day})


@router.patch("/api/admin/settings/rate-limits")
async def admin_set_rate_limits(request: Request):
    me = await require_superadmin(request)
    body = await request.json()
    try:
        per_min = int(body.get("per_min"))
        per_hour = int(body.get("per_hour"))
        per_day = int(body.get("per_day"))
    except (TypeError, ValueError):
        raise HTTPException(400, "per_min, per_hour and per_day must be integers.")
    if per_min <= 0 or per_hour <= 0 or per_day <= 0:
        raise HTTPException(400, "Rate limits must be positive.")
    if not (per_min <= per_hour <= per_day):
        raise HTTPException(400, "Limits must be ordered: per-minute ≤ per-hour ≤ per-day.")

    from app.auth.rate_limiter import set_rate_limits
    from app.settings_service import set_setting

    async with app_state.db_session_factory() as db:
        for key, val in (
            ("rate_limit_per_min", per_min),
            ("rate_limit_per_hour", per_hour),
            ("rate_limit_per_day", per_day),
        ):
            await set_setting(db, key=key, value=val, is_secret=False, updated_by_user_id=uuid.UUID(me["id"]))
        await db.commit()
    # Push to the Redis override + bust the in-memory cache so limits apply now.
    try:
        await set_rate_limits({"default": {"per_min": per_min, "per_hour": per_hour, "per_day": per_day}})
    except Exception:
        logger.warning("rate-limit Redis override failed; DB values will apply within 60s", exc_info=True)
    return JSONResponse({"success": True, "per_min": per_min, "per_hour": per_hour, "per_day": per_day})


@router.patch("/api/admin/settings/require-access-approval")
async def admin_toggle_gate(request: Request):
    me = await require_superadmin(request)
    body = await request.json()
    enabled = bool(body.get("enabled"))
    from app.settings_service import set_setting

    async with app_state.db_session_factory() as db:
        await set_setting(
            db,
            key="require_access_approval",
            value=enabled,
            is_secret=False,
            updated_by_user_id=uuid.UUID(me["id"]),
        )
        await db.commit()
    return JSONResponse({"success": True, "enabled": enabled})


@router.get("/api/admin/settings/branding")
async def admin_get_branding(request: Request):
    await require_superadmin(request)
    from app.settings_service import get_runtime_setting

    async with app_state.db_session_factory() as db:
        name = await get_runtime_setting(db, "brand_name", default="Fluxito")
        logo_url = await get_runtime_setting(db, "brand_logo_url", default="")
        accent = await get_runtime_setting(db, "brand_accent", default="")
    return JSONResponse({"name": str(name), "logo_url": str(logo_url or ""), "accent": str(accent or "")})


@router.patch("/api/admin/settings/branding")
async def admin_set_branding(request: Request):
    me = await require_superadmin(request)
    body = await request.json()
    name = (body.get("name") or "").strip()
    logo_url = (body.get("logo_url") or "").strip()
    accent = (body.get("accent") or "").strip()
    if not name or len(name) > 120:
        raise HTTPException(400, "App name is required (max 120 chars).")
    if len(logo_url) > 500:
        raise HTTPException(400, "Logo URL is too long.")
    if accent and not _ACCENT_RE.match(accent):
        raise HTTPException(400, "Accent must be a simple colour (e.g. #0B0B0E).")

    from app.branding import refresh_brand
    from app.settings_service import set_setting

    async with app_state.db_session_factory() as db:
        for key, val in (("brand_name", name), ("brand_logo_url", logo_url), ("brand_accent", accent)):
            await set_setting(db, key=key, value=val, is_secret=False, updated_by_user_id=uuid.UUID(me["id"]))
        await db.commit()
    await refresh_brand()
    return JSONResponse({"success": True, "name": name, "logo_url": logo_url, "accent": accent})


# ---------------------------------------------------------------------------
# Instance operations — maintenance mode + announcement banner
# ---------------------------------------------------------------------------


@router.get("/api/admin/settings/operations")
async def admin_get_operations(request: Request):
    await require_superadmin(request)
    from app.settings_service import get_runtime_setting

    async with app_state.db_session_factory() as db:
        maintenance = bool(await get_runtime_setting(db, "maintenance_mode", default=False))
        chat = bool(await get_runtime_setting(db, "chat_enabled", default=True))
        banner = await get_runtime_setting(db, "announcement_banner", default="")
        gtm_id = await get_runtime_setting(db, "gtm_container_id", default="")
    return JSONResponse(
        {
            "maintenance_mode": maintenance,
            "chat_enabled": chat,
            "announcement_banner": str(banner or ""),
            "gtm_container_id": str(gtm_id or ""),
        }
    )


@router.patch("/api/admin/settings/operations")
async def admin_set_operations(request: Request):
    me = await require_superadmin(request)
    body = await request.json()
    maintenance = bool(body.get("maintenance_mode"))
    chat_enabled = bool(body.get("chat_enabled", True)) if "chat_enabled" in body else True
    banner = (body.get("announcement_banner") or "").strip()
    gtm_container_id = (body.get("gtm_container_id") or "").strip().upper()
    if len(banner) > 280:
        raise HTTPException(400, "Announcement banner is too long (max 280 chars).")
    if gtm_container_id and not _GTM_RE.match(gtm_container_id):
        raise HTTPException(400, "Invalid GTM container ID format. Expected GTM-XXXXXXX.")
    from app.settings_service import set_setting

    async with app_state.db_session_factory() as db:
        await set_setting(
            db,
            key="maintenance_mode",
            value=maintenance,
            is_secret=False,
            updated_by_user_id=uuid.UUID(me["id"]),
        )
        if "chat_enabled" in body:
            await set_setting(
                db,
                key="chat_enabled",
                value=chat_enabled,
                is_secret=False,
                updated_by_user_id=uuid.UUID(me["id"]),
            )
        await set_setting(
            db,
            key="announcement_banner",
            value=banner,
            is_secret=False,
            updated_by_user_id=uuid.UUID(me["id"]),
        )
        await set_setting(
            db,
            key="gtm_container_id",
            value=gtm_container_id,
            is_secret=False,
            updated_by_user_id=uuid.UUID(me["id"]),
        )
        await db.commit()
    from app.branding import refresh_announcement, refresh_chat, refresh_gtm

    await refresh_announcement()
    await refresh_gtm()
    await refresh_chat()
    return JSONResponse(
        {
            "success": True,
            "maintenance_mode": maintenance,
            "chat_enabled": chat_enabled,
            "announcement_banner": banner,
            "gtm_container_id": gtm_container_id,
        }
    )


@router.patch("/api/admin/settings/chat")
async def admin_toggle_chat(request: Request):
    me = await require_superadmin(request)
    body = await request.json()
    enabled = bool(body.get("enabled", True))
    from app.branding import refresh_chat
    from app.settings_service import set_setting

    async with app_state.db_session_factory() as db:
        await set_setting(
            db,
            key="chat_enabled",
            value=enabled,
            is_secret=False,
            updated_by_user_id=uuid.UUID(me["id"]),
        )
        await db.commit()
    await refresh_chat()
    return JSONResponse({"success": True, "enabled": enabled})


@router.get("/api/admin/settings/gtm")
async def admin_get_gtm(request: Request):
    await require_superadmin(request)
    from app.settings_service import get_runtime_setting

    async with app_state.db_session_factory() as db:
        gtm_id = await get_runtime_setting(db, "gtm_container_id", default="")
    return JSONResponse({"gtm_container_id": str(gtm_id or "")})


@router.patch("/api/admin/settings/gtm")
async def admin_set_gtm(request: Request):
    me = await require_superadmin(request)
    body = await request.json()
    gtm_container_id = (body.get("gtm_container_id") or "").strip().upper()
    if gtm_container_id and not _GTM_RE.match(gtm_container_id):
        raise HTTPException(400, "Invalid GTM container ID format. Expected GTM-XXXXXXX.")

    from app.branding import refresh_gtm
    from app.settings_service import set_setting

    async with app_state.db_session_factory() as db:
        await set_setting(
            db,
            key="gtm_container_id",
            value=gtm_container_id,
            is_secret=False,
            updated_by_user_id=uuid.UUID(me["id"]),
        )
        await db.commit()
    await refresh_gtm()
    return JSONResponse({"success": True, "gtm_container_id": gtm_container_id})


# ---------------------------------------------------------------------------
# Sign-in method toggles
# ---------------------------------------------------------------------------


@router.get("/api/admin/settings/auth-methods")
async def admin_get_auth_methods(request: Request):
    await require_superadmin(request)
    from app.settings_service import get_runtime_setting

    async with app_state.db_session_factory() as db:
        google = bool(await get_runtime_setting(db, "auth_google_enabled", default=True))
        password = bool(await get_runtime_setting(db, "auth_password_enabled", default=False))
    return JSONResponse({"google_enabled": google, "password_enabled": password})


@router.patch("/api/admin/settings/auth-methods")
async def admin_set_auth_methods(request: Request):
    me = await require_superadmin(request)
    body = await request.json()
    google = bool(body.get("google_enabled"))
    password = bool(body.get("password_enabled"))
    if not google and not password:
        raise HTTPException(400, "At least one sign-in method must stay enabled.")
    from app.settings_service import set_setting

    async with app_state.db_session_factory() as db:
        await set_setting(
            db,
            key="auth_google_enabled",
            value=google,
            is_secret=False,
            updated_by_user_id=uuid.UUID(me["id"]),
        )
        await set_setting(
            db,
            key="auth_password_enabled",
            value=password,
            is_secret=False,
            updated_by_user_id=uuid.UUID(me["id"]),
        )
        await db.commit()
    return JSONResponse({"success": True, "google_enabled": google, "password_enabled": password})


# ---------------------------------------------------------------------------
# Direct user invite — create an account + temp password without a request
# ---------------------------------------------------------------------------

_EMAIL_RE = _re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@router.post("/api/admin/users/invite")
async def admin_invite_user(request: Request):
    me = await require_superadmin(request)
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    name = (body.get("name") or "").strip() or email.split("@")[0]
    if not _EMAIL_RE.match(email):
        raise HTTPException(400, "Please enter a valid email address.")

    from app.auth.email_auth import generate_temp_password, hash_password

    temp_password = None
    async with app_state.db_session_factory() as db:
        existing = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(400, "A user with that email already exists.")
        temp_password = generate_temp_password()
        new_user = User(
            email=email,
            display_name=name,
            password_hash=hash_password(temp_password),
            email_verified=True,  # invited by an operator — trusted
            auth_provider="email",
        )
        db.add(new_user)
        await db.flush()
        new_uid = new_user.id
        await db.commit()

    try:
        from app.api.project_routes import ensure_default_project

        await ensure_default_project(new_uid, name, email)
    except Exception:
        logger.warning("ensure_default_project failed after invite", exc_info=True)

    # Best-effort invite email (logs to console if SMTP isn't configured).
    try:
        from app.branding import brand as _brand
        from app.config import settings as _settings
        from app.email_service import send_email

        brand_name = _brand()["name"]
        base_url = _settings.APP_BASE_URL.rstrip("/")
        from html import escape as _esc

        html_body = (
            f"<p>You've been invited to {_esc(brand_name)}.</p>"
            f"<p>Sign in at <a href='{_esc(base_url)}/signin'>{_esc(base_url)}/signin</a> with:</p>"
            f"<p><b>Email:</b> {_esc(email)}<br><b>Temporary password:</b> {_esc(temp_password)}</p>"
            f"<p>Please change your password after your first sign-in.</p>"
        )
        text_body = (
            f"You've been invited to {brand_name}.\n\n"
            f"Sign in at {base_url}/signin\nEmail: {email}\nTemporary password: {temp_password}\n\n"
            "Please change your password after your first sign-in."
        )
        await send_email(email, f"You're invited to {brand_name}", html_body, text_body)
    except Exception:
        logger.warning("invite email failed for %s", email, exc_info=True)

    return JSONResponse({"success": True, "email": email, "temp_password": temp_password})


# ---------------------------------------------------------------------------
# Platform Activity Log (Cross-User & Cross-Project Super Admin Audit)
# ---------------------------------------------------------------------------

PLATFORM_LABELS = {
    "ga4": "Google Analytics 4",
    "gtm": "Google Tag Manager",
    "google_ads": "Google Ads",
    "meta": "Meta Ads",
    "tiktok": "TikTok Ads",
    "snap": "Snap Ads",
    "bigquery": "BigQuery",
    "redshift": "Redshift",
    "snowflake": "Snowflake",
    "amplitude": "Amplitude",
    "mixpanel": "Mixpanel",
    "posthog": "PostHog",
    "adobe": "Adobe Analytics",
}


def humanize_tool(name: str) -> str:
    if not name:
        return "Unknown"
    stripped = name
    for prefix in (
        "analytics_",
        "tagmanager_",
        "marketing_",
        "warehouse_",
        "dashboard_",
        "template_",
        "cross_platform_",
    ):
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix) :]
            break
    return stripped.replace("_", " ").title()


def _infer_platform(tool_name: str | None) -> str | None:
    if not tool_name:
        return None
    t = tool_name.lower()
    if t.startswith("tagmanager_") or t.startswith("gtm_"):
        return "gtm"
    if t.startswith("analytics_"):
        return "ga4"
    if t.startswith(("adwords_", "ads_", "google_ads_")):
        return "google_ads"
    if t.startswith(("warehouse_", "bigquery_", "bq_")):
        return "bigquery"
    if t.startswith("redshift_"):
        return "redshift"
    if t.startswith("snowflake_"):
        return "snowflake"
    if t.startswith("meta_"):
        return "meta"
    if t.startswith("tiktok_"):
        return "tiktok"
    if t.startswith("snap_"):
        return "snap"
    if t.startswith("amplitude_"):
        return "amplitude"
    if t.startswith("mixpanel_"):
        return "mixpanel"
    if t.startswith("posthog_"):
        return "posthog"
    if t.startswith("adobe_"):
        return "adobe"
    return None


@router.get("/admin/activity", response_class=HTMLResponse)
async def admin_activity_page(request: Request):
    """Platform-level activity log gated to super-admins."""
    user_ctx = await _resolve_user_ctx(request)
    if not user_ctx:
        return RedirectResponse(url="/signin?next=/admin/activity", status_code=302)

    async with app_state.db_session_factory() as db:
        u = (
            await db.execute(select(User).where(User.id == uuid.UUID(user_ctx.user_id)))
        ).scalar_one_or_none()
        if not u or not u.is_superadmin:
            return RedirectResponse(url="/home", status_code=302)

    from datetime import date, datetime, timedelta
    from app.api.google_oauth_routes import _load_user_view

    user_view = await _load_user_view(user_ctx)

    start_date_str = (request.query_params.get("start_date") or "").strip()
    end_date_str = (request.query_params.get("end_date") or "").strip()

    now = _utc_now_naive()
    today = now.date()

    start_dt = None
    end_dt = None
    is_custom_range = False

    if start_date_str:
        try:
            start_dt = datetime.strptime(start_date_str, "%Y-%m-%d")
            is_custom_range = True
        except ValueError:
            start_dt = None

    if end_date_str:
        try:
            end_dt = datetime.strptime(end_date_str, "%Y-%m-%d") + timedelta(days=1)
            is_custom_range = True
        except ValueError:
            end_dt = None

    if is_custom_range:
        window_start = start_dt if start_dt else (now - timedelta(days=365))
        window_end = end_dt if end_dt else (now + timedelta(days=1))
        delta_days = max(1, (window_end.date() - window_start.date()).days)
        window_days = delta_days
        window_label = f"{start_date_str or 'Start'} – {end_date_str or 'Today'}"
    else:
        try:
            window_days = int(request.query_params.get("days", "14"))
        except (ValueError, TypeError):
            window_days = 14
        if window_days not in (7, 14, 30, 90):
            window_days = 14
        window_start = now - timedelta(days=window_days)
        window_end = None
        window_label = f"Last {window_days} days"

    user_id_filter = request.query_params.get("user_id")
    project_id_filter = request.query_params.get("project_id")
    tool_filter = request.query_params.get("tool")
    platform_filter = request.query_params.get("platform")
    status_filter = request.query_params.get("status")

    stats = {
        "total_week": 0,
        "writes_week": 0,
        "failures_week": 0,
        "users_week": set(),
        "projects_week": set(),
        "total_duration_ms": 0,
        "duration_count": 0,
    }

    all_users = []
    all_projects = []
    all_tool_names = []
    platform_options = []
    results = []

    try:
        async with app_state.db_session_factory() as db:
            all_users = (await db.execute(select(User).order_by(User.email.asc()))).scalars().all()

            all_projects = (await db.execute(select(Project).order_by(Project.name.asc()))).scalars().all()

            tool_names_q = (
                select(ToolCallAudit.tool_name)
                .where(ToolCallAudit.created_at >= window_start)
                .distinct()
                .order_by(ToolCallAudit.tool_name)
            )
            if window_end:
                tool_names_q = tool_names_q.where(ToolCallAudit.created_at < window_end)
            all_tool_names = [r[0] for r in (await db.execute(tool_names_q)).all()]

            platform_q = (
                select(ToolCallAudit.platform)
                .where(ToolCallAudit.created_at >= window_start)
                .where(ToolCallAudit.platform.isnot(None))
                .distinct()
                .order_by(ToolCallAudit.platform)
            )
            if window_end:
                platform_q = platform_q.where(ToolCallAudit.created_at < window_end)
            all_platforms_raw = [r[0] for r in (await db.execute(platform_q)).all()]
            inferred_from_tools = {_infer_platform(tn) for tn in all_tool_names if _infer_platform(tn)}
            all_platforms = sorted(
                set(all_platforms_raw) | inferred_from_tools,
                key=lambda s: (PLATFORM_LABELS.get(s, s.replace("_", " ").title())),
            )
            platform_options = [
                (p, PLATFORM_LABELS.get(p, p.replace("_", " ").title())) for p in all_platforms
            ]

            stmt = (
                select(
                    ToolCallAudit,
                    User.email.label("user_email"),
                    User.display_name.label("user_display_name"),
                    Project.name.label("project_name"),
                    Project.slug.label("project_slug"),
                )
                .outerjoin(User, ToolCallAudit.user_id == User.id)
                .outerjoin(Project, ToolCallAudit.project_id == Project.id)
                .where(ToolCallAudit.created_at >= window_start)
                .order_by(desc(ToolCallAudit.created_at))
            )
            if window_end:
                stmt = stmt.where(ToolCallAudit.created_at < window_end)

            if user_id_filter:
                try:
                    stmt = stmt.where(ToolCallAudit.user_id == uuid.UUID(user_id_filter))
                except ValueError:
                    pass
            if project_id_filter:
                try:
                    stmt = stmt.where(ToolCallAudit.project_id == uuid.UUID(project_id_filter))
                except ValueError:
                    pass
            if tool_filter:
                stmt = stmt.where(ToolCallAudit.tool_name == tool_filter)
            if status_filter == "write":
                stmt = stmt.where(ToolCallAudit.is_write == True)
            elif status_filter == "error":
                stmt = stmt.where(ToolCallAudit.status != "success")
            elif status_filter in ("success", "denied"):
                stmt = stmt.where(ToolCallAudit.status == status_filter)

            results = (await db.execute(stmt.limit(1000))).all()
    except Exception as exc:
        logger.exception("Failed to query platform tool call audits: %s", exc)

    days: dict = {}
    for row in results:
        r, u_email, u_name, p_name, p_slug = row
        if not r.created_at:
            continue

        inferred_plat = r.platform or _infer_platform(r.tool_name) or "other"
        if platform_filter and inferred_plat != platform_filter:
            continue

        is_write = bool(r.is_write)
        is_issue = (r.status or "success") != "success"
        plat_label = PLATFORM_LABELS.get(inferred_plat, inferred_plat.replace("_", " ").title())

        stats["total_week"] += 1
        if is_write:
            stats["writes_week"] += 1
        if is_issue:
            stats["failures_week"] += 1
        if r.user_id:
            stats["users_week"].add(r.user_id)
        if r.project_id:
            stats["projects_week"].add(r.project_id)
        if r.duration_ms is not None:
            stats["total_duration_ms"] += r.duration_ms
            stats["duration_count"] += 1

        day_key = r.created_at.date().isoformat()
        if day_key not in days:
            days[day_key] = {
                "date": r.created_at.date(),
                "total": 0,
                "writes": 0,
                "issues": 0,
                "users": set(),
                "platforms": {},
            }
        d = days[day_key]
        d["total"] += 1
        if is_write:
            d["writes"] += 1
        if is_issue:
            d["issues"] += 1
        if r.user_id:
            d["users"].add(r.user_id)

        if inferred_plat not in d["platforms"]:
            d["platforms"][inferred_plat] = {
                "slug": inferred_plat,
                "label": plat_label,
                "count": 0,
                "writes": 0,
                "issues": 0,
                "calls": [],
            }
        p = d["platforms"][inferred_plat]
        p["count"] += 1
        if is_write:
            p["writes"] += 1
        if is_issue:
            p["issues"] += 1

        u_display = u_name or (u_email.split("@")[0] if u_email else "User")
        p["calls"].append(
            {
                "id": str(r.id),
                "tool_name": r.tool_name,
                "display_name": humanize_tool(r.tool_name),
                "is_write": is_write,
                "is_issue": is_issue,
                "status": r.status,
                "source": r.source_client,
                "summary": r.response_summary,
                "duration_ms": r.duration_ms,
                "time_str": r.created_at.strftime("%H:%M") if r.created_at else "--:--",
                "user_id": str(r.user_id) if r.user_id else "",
                "user_email": u_email or "",
                "user_name": u_display,
                "project_id": str(r.project_id) if r.project_id else None,
                "project_name": p_name or "Global",
            }
        )

    def day_label(d: date) -> str:
        if d == today:
            return "Today"
        if d == today - timedelta(days=1):
            return "Yesterday"
        return d.strftime("%A, %b ") + str(d.day)

    day_list = []
    for day_key in sorted(days.keys(), reverse=True):
        d = days[day_key]
        d["key"] = day_key
        d["label"] = day_label(d["date"])
        d["unique_users"] = len(d["users"])
        platforms_list = []
        for plat in d["platforms"].values():
            plat["calls"].sort(key=lambda c: c["time_str"], reverse=True)
            platforms_list.append(plat)
        platforms_list.sort(key=lambda pl: -pl["count"])
        d["platforms_list"] = platforms_list
        day_list.append(d)

    stats["users_count"] = len(stats["users_week"])
    stats["projects_count"] = len(stats["projects_week"])
    stats["avg_duration_ms"] = (
        round(stats["total_duration_ms"] / stats["duration_count"]) if stats["duration_count"] > 0 else 0
    )

    return render(
        request,
        "admin_activity.html",
        {
            "user": user_view,
            "active": "admin",
            "stats": stats,
            "day_list": day_list,
            "window_days": window_days,
            "window_label": window_label,
            "start_date": start_date_str,
            "end_date": end_date_str,
            "is_custom_range": is_custom_range,
            "all_users": all_users,
            "all_projects": all_projects,
            "selected_user_id": user_id_filter,
            "selected_project_id": project_id_filter,
            "all_tool_names": all_tool_names,
            "selected_tool": tool_filter,
            "platform_options": platform_options,
            "selected_platform": platform_filter,
            "selected_status": status_filter,
        },
    )


@router.get("/admin/activity/{audit_id}", response_class=HTMLResponse)
async def admin_activity_detail_page(request: Request, audit_id: str):
    """Inspect any tool call execution across the platform."""
    user_ctx = await _resolve_user_ctx(request)
    if not user_ctx:
        from urllib.parse import quote

        return RedirectResponse(
            f"/signin?next={quote(f'/admin/activity/{audit_id}', safe='/')}", status_code=302
        )
    async with app_state.db_session_factory() as db:
        u = (
            await db.execute(select(User).where(User.id == uuid.UUID(user_ctx.user_id)))
        ).scalar_one_or_none()
        if not u or not u.is_superadmin:
            return RedirectResponse(url="/home", status_code=302)

    try:
        rid = uuid.UUID(audit_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Not found")

    from app.api.google_oauth_routes import _load_user_view

    user_view = await _load_user_view(user_ctx)

    async with app_state.db_session_factory() as db:
        stmt = (
            select(
                ToolCallAudit,
                User.email.label("user_email"),
                User.display_name.label("user_display_name"),
                Project.name.label("project_name"),
            )
            .outerjoin(User, ToolCallAudit.user_id == User.id)
            .outerjoin(Project, ToolCallAudit.project_id == Project.id)
            .where(ToolCallAudit.id == rid)
        )
        row_tuple = (await db.execute(stmt)).first()

    if not row_tuple:
        raise HTTPException(status_code=404, detail="Not found")

    r, u_email, u_name, p_name = row_tuple
    row_dict = r.to_dict()
    row_dict["user_email"] = u_email or ""
    row_dict["user_name"] = u_name or (u_email.split("@")[0] if u_email else "User")
    row_dict["project_name"] = p_name or "Global (No project)"
    row_dict["platform_label"] = PLATFORM_LABELS.get(
        row_dict.get("platform") or _infer_platform(row_dict.get("tool_name")) or "",
        (row_dict.get("platform") or "").replace("_", " ").title(),
    )

    return render(
        request,
        "admin_activity_detail.html",
        {
            "user": user_view,
            "active": "admin",
            "row": row_dict,
        },
    )


@router.get("/api/admin/activity")
async def api_admin_activity_list(
    request: Request,
    user_id: str | None = Query(None),
    project_id: str | None = Query(None),
    tool: str | None = Query(None),
    status: str | None = Query(None),
    platform: str | None = Query(None),
    source: str | None = Query(None),
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    days: int | None = Query(None),
    format: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """JSON API or CSV export for platform-wide tool calls."""
    await require_superadmin(request)
    from datetime import datetime, timedelta

    async with app_state.db_session_factory() as db:
        stmt = (
            select(
                ToolCallAudit,
                User.email.label("user_email"),
                User.display_name.label("user_display_name"),
                Project.name.label("project_name"),
            )
            .outerjoin(User, ToolCallAudit.user_id == User.id)
            .outerjoin(Project, ToolCallAudit.project_id == Project.id)
        )
        if user_id:
            try:
                stmt = stmt.where(ToolCallAudit.user_id == uuid.UUID(user_id))
            except ValueError:
                pass
        if project_id:
            try:
                stmt = stmt.where(ToolCallAudit.project_id == uuid.UUID(project_id))
            except ValueError:
                pass
        if tool:
            stmt = stmt.where(ToolCallAudit.tool_name == tool)
        if platform:
            stmt = stmt.where(ToolCallAudit.platform == platform)
        if status == "write":
            stmt = stmt.where(ToolCallAudit.is_write == True)
        elif status == "error":
            stmt = stmt.where(ToolCallAudit.status != "success")
        elif status:
            stmt = stmt.where(ToolCallAudit.status == status)
        if source:
            stmt = stmt.where(ToolCallAudit.source_client == source)

        if start_date:
            try:
                s_dt = datetime.strptime(start_date, "%Y-%m-%d")
                stmt = stmt.where(ToolCallAudit.created_at >= s_dt)
            except ValueError:
                pass
        elif days:
            now = _utc_now_naive()
            stmt = stmt.where(ToolCallAudit.created_at >= now - timedelta(days=days))

        if end_date:
            try:
                e_dt = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
                stmt = stmt.where(ToolCallAudit.created_at < e_dt)
            except ValueError:
                pass

        stmt = stmt.order_by(desc(ToolCallAudit.created_at))

        if format == "csv":
            import csv
            import io

            rows = (await db.execute(stmt.limit(5000))).all()
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(
                [
                    "id",
                    "created_at",
                    "user_email",
                    "user_name",
                    "project_name",
                    "tool_name",
                    "platform",
                    "source_client",
                    "status",
                    "is_write",
                    "duration_ms",
                    "summary",
                ]
            )
            for r, u_email, u_name, p_name in rows:
                plat = r.platform or _infer_platform(r.tool_name) or ""
                if platform and plat != platform:
                    continue
                u_display = u_name or (u_email.split("@")[0] if u_email else "User")
                writer.writerow(
                    [
                        str(r.id),
                        r.created_at.isoformat() if r.created_at else "",
                        u_email or "",
                        u_display,
                        p_name or "Global",
                        r.tool_name,
                        plat,
                        r.source_client or "",
                        r.status,
                        "1" if r.is_write else "0",
                        r.duration_ms or "",
                        r.response_summary or "",
                    ]
                )
            return Response(
                content=output.getvalue(),
                media_type="text/csv",
                headers={"Content-Disposition": 'attachment; filename="platform_activity.csv"'},
            )

        rows = (await db.execute(stmt.limit(limit).offset(offset))).all()
        calls = []
        for r, u_email, u_name, p_name in rows:
            plat = r.platform or _infer_platform(r.tool_name) or ""
            if platform and plat != platform:
                continue
            item = r.to_dict()
            item["user_id"] = str(r.user_id) if r.user_id else None
            item["project_id"] = str(r.project_id) if r.project_id else None
            item["user_email"] = u_email or ""
            item["user_name"] = u_name or (u_email.split("@")[0] if u_email else "User")
            item["project_name"] = p_name or "Global"
            calls.append(item)

    return JSONResponse({"calls": calls, "count": len(calls), "limit": limit, "offset": offset})


@router.get("/api/admin/activity/{audit_id}")
async def api_admin_activity_detail(request: Request, audit_id: str):
    """Retrieve details for a single tool call."""
    await require_superadmin(request)
    try:
        rid = uuid.UUID(audit_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Not found")

    async with app_state.db_session_factory() as db:
        stmt = (
            select(
                ToolCallAudit,
                User.email.label("user_email"),
                User.display_name.label("user_display_name"),
                Project.name.label("project_name"),
            )
            .join(User, ToolCallAudit.user_id == User.id)
            .outerjoin(Project, ToolCallAudit.project_id == Project.id)
            .where(ToolCallAudit.id == rid)
        )
        row_tuple = (await db.execute(stmt)).first()

    if not row_tuple:
        raise HTTPException(status_code=404, detail="Not found")

    r, u_email, u_name, p_name = row_tuple
    row_dict = r.to_dict()
    row_dict["user_email"] = u_email
    row_dict["user_name"] = u_name or u_email.split("@")[0]
    row_dict["project_name"] = p_name or "Global"
    return JSONResponse({"call": row_dict})


# ---------------------------------------------------------------------------
# Instance Projects Directory
# ---------------------------------------------------------------------------


@router.get("/admin/projects", response_class=HTMLResponse)
async def admin_projects_page(request: Request):
    """Super-admin view of all projects on the instance."""
    user_ctx = await _resolve_user_ctx(request)
    if not user_ctx:
        return RedirectResponse(url="/signin?next=/admin/projects", status_code=302)

    async with app_state.db_session_factory() as db:
        u = (
            await db.execute(select(User).where(User.id == uuid.UUID(user_ctx.user_id)))
        ).scalar_one_or_none()
        if not u or not u.is_superadmin:
            return RedirectResponse(url="/home", status_code=302)

    from datetime import timedelta
    from app.api.google_oauth_routes import _load_user_view
    from app.models.connection import OAuthConnection
    from app.models.project import ProjectMember

    user_view = await _load_user_view(user_ctx)
    now = _utc_now_naive()
    week_ago = now - timedelta(days=7)

    projects_data = []
    total_calls_week = 0
    total_members = 0
    active_count = 0

    try:
        async with app_state.db_session_factory() as db:
            stmt = (
                select(
                    Project,
                    User.email.label("owner_email"),
                    User.display_name.label("owner_name"),
                )
                .outerjoin(User, Project.owner_id == User.id)
                .order_by(Project.name.asc())
            )
            project_rows = (await db.execute(stmt)).all()

            member_counts_q = select(ProjectMember.project_id, func.count(ProjectMember.id)).group_by(
                ProjectMember.project_id
            )
            member_counts = dict((await db.execute(member_counts_q)).all())

            conn_counts_q = (
                select(OAuthConnection.project_id, func.count(OAuthConnection.id))
                .where(OAuthConnection.project_id.isnot(None))
                .group_by(OAuthConnection.project_id)
            )
            conn_counts = dict((await db.execute(conn_counts_q)).all())

            tool_counts_q = (
                select(ToolCallAudit.project_id, func.count(ToolCallAudit.id))
                .where(ToolCallAudit.created_at >= week_ago)
                .where(ToolCallAudit.project_id.isnot(None))
                .group_by(ToolCallAudit.project_id)
            )
            tool_counts = dict((await db.execute(tool_counts_q)).all())

            last_active_q = (
                select(ToolCallAudit.project_id, func.max(ToolCallAudit.created_at))
                .where(ToolCallAudit.project_id.isnot(None))
                .group_by(ToolCallAudit.project_id)
            )
            last_active_map = dict((await db.execute(last_active_q)).all())

        for proj, o_email, o_name in project_rows:
            if proj.is_active:
                active_count += 1
            m_cnt = member_counts.get(proj.id, 0)
            total_members += m_cnt
            c_cnt = conn_counts.get(proj.id, 0)
            tc_cnt = tool_counts.get(proj.id, 0)
            total_calls_week += tc_cnt

            last_dt = last_active_map.get(proj.id)
            last_str = "No calls yet"
            if last_dt:
                if last_dt.tzinfo is not None:
                    last_dt = last_dt.replace(tzinfo=None)
                delta = now - last_dt
                if delta.days == 0:
                    hours = delta.seconds // 3600
                    if hours == 0:
                        mins = max(1, delta.seconds // 60)
                        last_str = f"{mins}m ago"
                    else:
                        last_str = f"{hours}h ago"
                elif delta.days == 1:
                    last_str = "Yesterday"
                else:
                    last_str = f"{delta.days}d ago"

            owner_display = o_name or (o_email.split("@")[0] if o_email else "None")
            projects_data.append(
                {
                    "id": str(proj.id),
                    "name": proj.name,
                    "slug": proj.slug,
                    "is_active": proj.is_active,
                    "owner_email": o_email or "",
                    "owner_name": owner_display,
                    "members_count": m_cnt,
                    "connectors_count": c_cnt,
                    "tool_calls_7d": tc_cnt,
                    "last_active_str": last_str,
                }
            )
    except Exception as exc:
        logger.exception("Failed to load admin projects: %s", exc)

    return render(
        request,
        "admin_projects.html",
        {
            "user": user_view,
            "active": "admin",
            "projects": projects_data,
            "active_count": active_count,
            "total_members": total_members,
            "total_calls_week": total_calls_week,
        },
    )


@router.get("/api/admin/projects")
async def api_admin_projects_list(request: Request):
    """JSON API returning all projects with metadata and usage."""
    await require_superadmin(request)
    from datetime import timedelta
    from app.models.connection import OAuthConnection
    from app.models.project import ProjectMember

    now = _utc_now_naive()
    week_ago = now - timedelta(days=7)

    async with app_state.db_session_factory() as db:
        stmt = (
            select(
                Project,
                User.email.label("owner_email"),
                User.display_name.label("owner_name"),
            )
            .outerjoin(User, Project.owner_id == User.id)
            .order_by(Project.name.asc())
        )
        project_rows = (await db.execute(stmt)).all()

        member_counts_q = select(ProjectMember.project_id, func.count(ProjectMember.id)).group_by(
            ProjectMember.project_id
        )
        member_counts = dict((await db.execute(member_counts_q)).all())

        conn_counts_q = (
            select(OAuthConnection.project_id, func.count(OAuthConnection.id))
            .where(OAuthConnection.project_id.isnot(None))
            .group_by(OAuthConnection.project_id)
        )
        conn_counts = dict((await db.execute(conn_counts_q)).all())

        tool_counts_q = (
            select(ToolCallAudit.project_id, func.count(ToolCallAudit.id))
            .where(ToolCallAudit.created_at >= week_ago)
            .where(ToolCallAudit.project_id.isnot(None))
            .group_by(ToolCallAudit.project_id)
        )
        tool_counts = dict((await db.execute(tool_counts_q)).all())

    items = []
    for proj, o_email, o_name in project_rows:
        owner_display = o_name or (o_email.split("@")[0] if o_email else "None")
        items.append(
            {
                "id": str(proj.id),
                "name": proj.name,
                "slug": proj.slug,
                "is_active": proj.is_active,
                "owner_email": o_email or "",
                "owner_name": owner_display,
                "members_count": member_counts.get(proj.id, 0),
                "connectors_count": conn_counts.get(proj.id, 0),
                "tool_calls_7d": tool_counts.get(proj.id, 0),
                "created_at": proj.created_at.isoformat() if proj.created_at else None,
            }
        )
    return JSONResponse({"projects": items})
