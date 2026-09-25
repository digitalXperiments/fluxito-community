"""
Notification & Profile API Routes

Notifications:
  GET  /api/notifications         — List notifications (JSON, paginated)
  GET  /api/notifications/count   — Unread count (JSON)
  POST /api/notifications/{id}/read — Mark single as read
  POST /api/notifications/read-all  — Mark all as read

Profile:
  GET  /profile                   — Profile page (HTML)
  POST /api/profile               — Update profile (JSON)
  GET  /api/profile/usage         — Usage stats (JSON)
  POST /api/profile/tokens        — Create PAT (JSON) — for remote/headless MCP
  GET  /api/profile/tokens        — List PATs (JSON)
  POST /api/profile/tokens/{id}/revoke — Revoke PAT (JSON)
"""

import logging
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import desc, func, select, update

import app.app_state as app_state
from app.auth.uid_cookie import get_uid_from_request
from app.auth.mcp_session_manager import create_pat, list_pats, revoke_pat
from app.models.connection import OAuthConnection
from app.models.notification import Notification
from app.models.token import GA4Property, GoogleAdsAccount, GTMContainer
from app.models.user import User
from app.templating import render
from app.utils import base_url_from_request, safe_uuid

logger = logging.getLogger(__name__)

router = APIRouter()


async def _load_user(uid: str | None) -> dict | None:
    """Load user dict from uid."""
    user_uuid = safe_uuid(uid)
    if user_uuid is None:
        return None
    try:
        async with app_state.db_session_factory() as db:
            result = await db.execute(select(User).where(User.id == user_uuid))
            u = result.scalar_one_or_none()
            if u:
                return {
                    "id": str(u.id),
                    "email": u.email or "",
                    "display_name": u.display_name or "",
                    "is_active": bool(u.is_active),
                    "is_superadmin": bool(u.is_superadmin),
                    "created_at": str(u.created_at) if u.created_at else "",
                    "preferred_ai_client": u.preferred_ai_client,
                    "auth_provider": u.auth_provider,
                }
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Notification API
# ---------------------------------------------------------------------------


def _safe_int(value: str | None, default: int) -> int:
    """Parse an int from a query param; fall back to ``default`` on bad input."""
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


@router.get("/api/notifications")
async def list_notifications(request: Request):
    """List notifications for the authenticated user, newest first."""
    uid = get_uid_from_request(request)
    user_uuid = safe_uuid(uid)
    if user_uuid is None:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    limit = max(1, min(_safe_int(request.query_params.get("limit"), 20), 50))
    offset = max(0, _safe_int(request.query_params.get("offset"), 0))

    try:
        async with app_state.db_session_factory() as db:
            result = await db.execute(
                select(Notification)
                .where(Notification.user_id == user_uuid)
                .order_by(desc(Notification.created_at))
                .limit(limit)
                .offset(offset)
            )
            notifications = result.scalars().all()

            # Also get total unread count
            count_result = await db.execute(
                select(func.count(Notification.id)).where(
                    Notification.user_id == user_uuid,
                    Notification.is_read == False,
                )
            )
            unread_count = count_result.scalar() or 0

            return JSONResponse(
                {
                    "notifications": [
                        {
                            "id": str(n.id),
                            "category": n.category,
                            "severity": n.severity,
                            "title": n.title,
                            "message": n.message,
                            "action_url": n.action_url,
                            "is_read": n.is_read,
                            "created_at": n.created_at.isoformat() if n.created_at else None,
                        }
                        for n in notifications
                    ],
                    "unread_count": unread_count,
                }
            )
    except Exception as e:
        logger.error(f"Error listing notifications: {e}")
        return JSONResponse({"error": "Failed to load notifications"}, status_code=500)


@router.get("/api/notifications/count")
async def notification_count(request: Request):
    """Return the unread notification count for the authenticated user."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        from app.notifications import get_unread_count

        count = await get_unread_count(uid)
        return JSONResponse({"unread_count": count})
    except Exception as e:
        logger.error(f"Error getting notification count: {e}")
        return JSONResponse({"unread_count": 0})


@router.post("/api/notifications/{notification_id}/read")
async def mark_notification_read(notification_id: str, request: Request):
    """Mark a single notification as read."""
    uid = get_uid_from_request(request)
    user_uuid = safe_uuid(uid)
    if user_uuid is None:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    notif_uuid = safe_uuid(notification_id)
    if notif_uuid is None:
        return JSONResponse({"error": "Not found"}, status_code=404)

    try:
        async with app_state.db_session_factory() as db:
            await db.execute(
                update(Notification)
                .where(
                    Notification.id == notif_uuid,
                    Notification.user_id == user_uuid,
                )
                .values(is_read=True)
            )
            await db.commit()
            return JSONResponse({"ok": True})
    except Exception as e:
        logger.error(f"Error marking notification read: {e}")
        return JSONResponse({"error": "Failed"}, status_code=500)


@router.post("/api/notifications/read-all")
async def mark_all_read(request: Request):
    """Mark all notifications as read for the authenticated user."""
    uid = get_uid_from_request(request)
    user_uuid = safe_uuid(uid)
    if user_uuid is None:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        async with app_state.db_session_factory() as db:
            await db.execute(
                update(Notification)
                .where(
                    Notification.user_id == user_uuid,
                    Notification.is_read == False,
                )
                .values(is_read=True)
            )
            await db.commit()
            return JSONResponse({"ok": True})
    except Exception as e:
        logger.error(f"Error marking all read: {e}")
        return JSONResponse({"error": "Failed"}, status_code=500)


# ---------------------------------------------------------------------------
# Profile Page (HTML)
# ---------------------------------------------------------------------------


@router.get("/profile")
async def profile_page(request: Request):
    """Render the user's comprehensive profile hub — identity, billing,
    connections, usage stats, and recent notifications all in one place."""
    uid = get_uid_from_request(request)
    if not uid:
        return RedirectResponse("/signin?next=/profile", status_code=302)

    user = await _load_user(uid)
    if not user:
        return RedirectResponse("/signin?next=/profile", status_code=302)

    uid_uuid = uuid.UUID(uid)

    # ── Connections & platforms ──
    connections = []
    ga4_count = 0
    gtm_count = 0
    ads_count = 0
    total_conns = 0
    platforms = []

    try:
        async with app_state.db_session_factory() as db:
            conn_result = await db.execute(
                select(OAuthConnection).where(
                    OAuthConnection.user_id == uid_uuid,
                    OAuthConnection.is_active == True,
                )
            )
            connections = list(conn_result.scalars().all())
            # Discovered resources hang off the connection, not the user.
            conn_ids = [c.id for c in connections]

            ga4_result = await db.execute(
                select(func.count()).select_from(GA4Property).where(GA4Property.connection_id.in_(conn_ids))
            )
            ga4_count = ga4_result.scalar_one() or 0

            gtm_result = await db.execute(
                select(func.count()).select_from(GTMContainer).where(GTMContainer.connection_id.in_(conn_ids))
            )
            gtm_count = gtm_result.scalar_one() or 0

            ads_result = await db.execute(
                select(func.count())
                .select_from(GoogleAdsAccount)
                .where(GoogleAdsAccount.connection_id.in_(conn_ids))
            )
            ads_count = ads_result.scalar_one() or 0
    except Exception:
        pass

    # Detect Google scopes
    google_conns = [c for c in connections if (c.provider or "") in ("google", "", None)]
    google_has_ga4 = any(any("analytics" in s for s in (c.scopes or [])) for c in google_conns)
    google_has_gtm = any(any("tagmanager" in s for s in (c.scopes or [])) for c in google_conns)
    google_has_ads = any("https://www.googleapis.com/auth/adwords" in (c.scopes or []) for c in google_conns)
    meta_count = sum(1 for c in connections if c.provider == "meta")
    tiktok_count = sum(1 for c in connections if c.provider == "tiktok")
    snap_count = sum(1 for c in connections if c.provider == "snap")

    # Credential-based connections
    cred_counts = {"bq": 0, "amp": 0, "adobe": 0, "rs": 0, "sf": 0}
    try:
        from app.models.bq_connection import BQConnection
        from app.models.credential_connection import (
            AdobeConnection,
            AmplitudeConnection,
            MixpanelConnection,
            PostHogConnection,
            RedshiftConnection,
            SnowflakeConnection,
        )

        cred_models = {
            "bq": BQConnection,
            "amp": AmplitudeConnection,
            "mp": MixpanelConnection,
            "ph": PostHogConnection,
            "adobe": AdobeConnection,
            "rs": RedshiftConnection,
            "sf": SnowflakeConnection,
        }
        async with app_state.db_session_factory() as db:
            for key, Model in cred_models.items():
                r = await db.execute(
                    select(func.count())
                    .select_from(Model)
                    .where(Model.user_id == uid_uuid, Model.is_active == True)
                )
                cred_counts[key] = r.scalar_one() or 0
    except Exception:
        pass
    bq_count = cred_counts["bq"]
    amp_count = cred_counts["amp"]
    mp_count = cred_counts["mp"]
    ph_count = cred_counts["ph"]
    adobe_count = cred_counts["adobe"]
    rs_count = cred_counts["rs"]
    sf_count = cred_counts["sf"]

    google_svc_count = sum([google_has_ga4, google_has_gtm, google_has_ads])
    total_conns = (
        google_svc_count
        + bq_count
        + meta_count
        + tiktok_count
        + snap_count
        + amp_count
        + mp_count
        + ph_count
        + adobe_count
        + rs_count
        + sf_count
    )

    platforms = [
        {
            "slug": "ga4",
            "name": "GA4",
            "desc": "Google Analytics 4",
            "count": ga4_count,
            "via_google": bool(not ga4_count and google_has_ga4),
        },
        {
            "slug": "gtm",
            "name": "GTM",
            "desc": "Tag Manager",
            "count": gtm_count,
            "via_google": bool(not gtm_count and google_has_gtm),
        },
        {
            "slug": "google_ads",
            "name": "Google Ads",
            "desc": "Ads accounts",
            "count": ads_count,
            "via_google": bool(not ads_count and google_has_ads),
        },
        {
            "slug": "meta",
            "name": "Meta Ads",
            "desc": "Facebook & Instagram",
            "count": meta_count,
            "via_google": False,
        },
        {
            "slug": "tiktok",
            "name": "TikTok Ads",
            "desc": "Campaigns & insights",
            "count": tiktok_count,
            "via_google": False,
        },
        {
            "slug": "snap",
            "name": "Snapchat",
            "desc": "Campaigns & pixel",
            "count": snap_count,
            "via_google": False,
        },
        {
            "slug": "bigquery",
            "name": "BigQuery",
            "desc": "Data warehouse",
            "count": bq_count,
            "via_google": False,
        },
        {
            "slug": "amplitude",
            "name": "Amplitude",
            "desc": "Product analytics",
            "count": amp_count,
            "via_google": False,
        },
        {
            "slug": "mixpanel",
            "name": "Mixpanel",
            "desc": "Product analytics",
            "count": mp_count,
            "via_google": False,
        },
        {
            "slug": "posthog",
            "name": "PostHog",
            "desc": "Product analytics",
            "count": ph_count,
            "via_google": False,
        },
        {
            "slug": "adobe",
            "name": "Adobe",
            "desc": "Analytics + Launch",
            "count": adobe_count,
            "via_google": False,
        },
        {
            "slug": "redshift",
            "name": "Redshift",
            "desc": "Data warehouse",
            "count": rs_count,
            "via_google": False,
        },
        {
            "slug": "snowflake",
            "name": "Snowflake",
            "desc": "Data warehouse",
            "count": sf_count,
            "via_google": False,
        },
    ]

    # ── Recent notifications ──
    recent_notifs = []
    unread_notifs = 0
    try:
        async with app_state.db_session_factory() as db:
            n_result = await db.execute(
                select(Notification)
                .where(Notification.user_id == uid_uuid)
                .order_by(desc(Notification.created_at))
                .limit(6)
            )
            recent_notifs = [
                {
                    "title": n.title,
                    "message": n.message,
                    "is_read": n.is_read,
                    "action_url": n.action_url,
                    "created_at": n.created_at,
                }
                for n in n_result.scalars().all()
            ]
            cnt_result = await db.execute(
                select(func.count(Notification.id)).where(
                    Notification.user_id == uid_uuid,
                    Notification.is_read == False,
                )
            )
            unread_notifs = cnt_result.scalar() or 0
    except Exception:
        pass

    # ── MCP PATs (Personal Access Tokens for remote/headless MCP clients) ──
    # Loaded here so the profile template can render the "Access Tokens" card.
    # list_pats returns only safe fields (no plaintext/hashes).
    mcp_pats: list[dict] = []
    try:
        mcp_pats = await list_pats(uid)  # defaults to active_only=True (hides revoked)
    except Exception:
        pass

    return render(
        request,
        "profile.html",
        {
            "user": user,
            "active": "profile",
            "org": None,
            "org_member_count": 0,
            "platforms": platforms,
            "total_conns": total_conns,
            "recent_notifs": recent_notifs,
            "unread_notifs": unread_notifs,
            "mcp_pats": mcp_pats,
            "base_url": base_url_from_request(request),
        },
    )


# ---------------------------------------------------------------------------
# Profile API (JSON)
# ---------------------------------------------------------------------------


@router.post("/api/profile")
async def update_profile(request: Request):
    """Update the user's display name or email."""
    uid = get_uid_from_request(request)
    user_uuid = safe_uuid(uid)
    if user_uuid is None:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    display_name = body.get("display_name")
    email = body.get("email")

    updates = {}
    if display_name is not None:
        updates["display_name"] = display_name.strip()[:255] if display_name else None
    if email is not None:
        # The sign-in email can't be changed here: an unverified change would let
        # someone claim an address they don't own (and receive its invites and
        # Google sign-ins). Re-sending the current address is a no-op.
        async with app_state.db_session_factory() as db:
            current = (await db.execute(select(User.email).where(User.id == user_uuid))).scalar_one_or_none()
        if (email or "").strip().lower() != (current or "").lower():
            return JSONResponse(
                {
                    "error": "Your sign-in email can't be changed from the profile. Contact support to move your account."
                },
                status_code=400,
            )

    if not updates:
        return JSONResponse({"error": "No fields to update"}, status_code=400)

    try:
        async with app_state.db_session_factory() as db:
            await db.execute(update(User).where(User.id == user_uuid).values(**updates))
            await db.commit()

            # Create notification for profile update
            from app.notifications import create_notification

            await create_notification(
                user_id=uid,
                title="Profile Updated",
                message="Your profile information has been updated successfully.",
                category="system",
                severity="success",
                action_url="/profile",
            )

            return JSONResponse({"ok": True, "updated": updates})
    except Exception as e:
        logger.error(f"Error updating profile: {e}")
        return JSONResponse({"error": "Failed to update profile"}, status_code=500)


# ---------------------------------------------------------------------------
# Usage Stats API — detailed per-tool / per-platform breakdown
# ---------------------------------------------------------------------------


@router.get("/api/profile/usage")
async def usage_stats(request: Request):
    """Return detailed usage breakdown by tool and platform for current month."""
    uid = get_uid_from_request(request)
    uid_uuid = safe_uuid(uid)
    if uid_uuid is None:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    # Per-tool / per-platform / per-day breakdowns come from the
    # ``tool_call_audit`` table (one row per call). ``usage_ledger``
    # is a monthly rollup and no longer carries that detail.
    from datetime import datetime

    from sqlalchemy import literal_column

    from app.models.audit import ToolCallAudit

    month_key = datetime.utcnow().strftime("%Y-%m")
    year, month = map(int, month_key.split("-"))
    month_start = datetime(year, month, 1)
    month_end = datetime(year + (1 if month == 12 else 0), 1 if month == 12 else month + 1, 1)

    try:
        async with app_state.db_session_factory() as db:
            # Per-tool breakdown
            tool_result = await db.execute(
                select(
                    ToolCallAudit.tool_name,
                    func.count().label("count"),
                )
                .where(
                    ToolCallAudit.user_id == uid_uuid,
                    ToolCallAudit.created_at >= month_start,
                    ToolCallAudit.created_at < month_end,
                )
                .group_by(ToolCallAudit.tool_name)
                .order_by(func.count().desc())
            )
            by_tool = [{"tool": r[0], "count": r[1]} for r in tool_result.all()]

            # Per-platform breakdown
            platform_result = await db.execute(
                select(
                    func.coalesce(ToolCallAudit.platform, "unknown").label("platform"),
                    func.count().label("count"),
                )
                .where(
                    ToolCallAudit.user_id == uid_uuid,
                    ToolCallAudit.created_at >= month_start,
                    ToolCallAudit.created_at < month_end,
                )
                .group_by(literal_column("platform"))
                .order_by(func.count().desc())
            )
            by_platform = [{"platform": r[0], "count": r[1]} for r in platform_result.all()]

            # Daily breakdown for the current month
            daily_result = await db.execute(
                select(
                    func.date(ToolCallAudit.created_at).label("day"),
                    func.count().label("count"),
                )
                .where(
                    ToolCallAudit.user_id == uid_uuid,
                    ToolCallAudit.created_at >= month_start,
                    ToolCallAudit.created_at < month_end,
                )
                .group_by(literal_column("day"))
                .order_by(literal_column("day"))
            )
            by_day = [{"date": str(r[0]), "count": r[1]} for r in daily_result.all()]

            total = sum(t["count"] for t in by_tool)

            return JSONResponse(
                {
                    "month": month_key,
                    "total": total,
                    "by_tool": by_tool,
                    "by_platform": by_platform,
                    "by_day": by_day,
                }
            )
    except Exception as e:
        logger.error(f"Error fetching usage stats: {e}")
        return JSONResponse({"error": "Failed to load usage stats"}, status_code=500)


# ---------------------------------------------------------------------------
# MCP Personal Access Tokens (for remote/headless clients)
# These are user-wide bearer tokens (stored as MCPSession rows with kind='pat').
# The client (on remote) just sends Authorization: Bearer fxt_pat_... on its /mcp calls.
# No OAuth dance or browser required on the remote machine.
# ---------------------------------------------------------------------------


@router.post("/api/profile/tokens")
async def create_mcp_token(request: Request):
    """Create a new PAT. Body: {name: str, expiry_days: int|null }.
    Returns the token plaintext exactly once + metadata.
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        body = {}

    name = (body.get("name") or "").strip()
    expiry_days = body.get("expiry_days")

    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)

    try:
        data = await create_pat(uid, name, expiry_days)
        # Best-effort activity note (non-fatal)
        try:
            from app.notifications import create_notification

            await create_notification(
                user_id=uid,
                title="MCP Access Token created",
                message=f"Token '{name}' created for remote / headless use.",
                category="system",
                action_url="/profile",
            )
        except Exception:
            pass
        return JSONResponse(data)
    except Exception as e:
        logger.error(f"Error creating MCP PAT: {e}")
        return JSONResponse({"error": "Failed to create token"}, status_code=500)


@router.get("/api/profile/tokens")
async def list_mcp_tokens(request: Request):
    """List the caller's PATs (safe fields only; no plaintext)."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        tokens = await list_pats(uid)  # active_only=True by default — revoked tokens are hidden
        return JSONResponse({"tokens": tokens})
    except Exception as e:
        logger.error(f"Error listing MCP PATs: {e}")
        return JSONResponse({"error": "Failed to list tokens"}, status_code=500)


@router.post("/api/profile/tokens/{token_id}/revoke")
async def revoke_mcp_token(request: Request, token_id: str):
    """Revoke one of the caller's PATs. Immediate (DB + Redis)."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        ok = await revoke_pat(uid, token_id)
        if not ok:
            return JSONResponse({"error": "Token not found or not owned by you"}, status_code=404)
        # Best-effort activity note
        try:
            from app.notifications import create_notification

            await create_notification(
                user_id=uid,
                title="MCP Access Token revoked",
                message="A token was revoked from your profile.",
                category="system",
                severity="warning",
                action_url="/profile",
            )
        except Exception:
            pass
        return JSONResponse({"ok": True})
    except Exception as e:
        logger.error(f"Error revoking MCP PAT: {e}")
        return JSONResponse({"error": "Failed to revoke"}, status_code=500)


# ---------------------------------------------------------------------------
# Account Deactivation & Deletion (two-step safety)
# ---------------------------------------------------------------------------


@router.post("/api/account/deactivate")
async def deactivate_account(request: Request):
    """Step 1: Deactivate the account (reversible). User can reactivate later."""
    uid = get_uid_from_request(request)
    user_uuid = safe_uuid(uid)
    if user_uuid is None:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        async with app_state.db_session_factory() as db:
            await db.execute(update(User).where(User.id == user_uuid).values(is_active=False))
            await db.commit()

            from app.notifications import create_notification

            await create_notification(
                user_id=uid,
                title="Account Deactivated",
                message="Your account has been deactivated. You can reactivate it by signing in again, or permanently delete it from your profile.",
                category="system",
                severity="warning",
                action_url="/profile",
            )

            return JSONResponse({"ok": True, "status": "deactivated"})
    except Exception as e:
        logger.error(f"Error deactivating account: {e}")
        return JSONResponse({"error": "Failed to deactivate"}, status_code=500)


@router.post("/api/account/reactivate")
async def reactivate_account(request: Request):
    """Re-enable a deactivated account."""
    uid = get_uid_from_request(request)
    user_uuid = safe_uuid(uid)
    if user_uuid is None:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        async with app_state.db_session_factory() as db:
            user = await db.get(User, user_uuid)
            if user is None:
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            if user.deactivated_by_admin:
                return JSONResponse(
                    {"error": "This account was deactivated by an administrator."}, status_code=403
                )
            user.is_active = True
            await db.commit()
        from app.auth import account_status

        account_status.forget(str(user_uuid))
        return JSONResponse({"ok": True, "status": "active"})
    except Exception as e:
        logger.error(f"Error reactivating account: {e}")
        return JSONResponse({"error": "Failed to reactivate"}, status_code=500)


@router.post("/api/account/delete")
async def delete_account(request: Request):
    """Step 2: Permanently delete account. Only allowed if account is already deactivated."""
    uid = get_uid_from_request(request)
    user_uuid = safe_uuid(uid)
    if user_uuid is None:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        body = {}

    confirmation = body.get("confirm")
    if confirmation != "DELETE":
        return JSONResponse(
            {"error": "Please type DELETE to confirm permanent deletion."},
            status_code=400,
        )

    try:
        async with app_state.db_session_factory() as db:
            # Check account is deactivated first
            result = await db.execute(select(User).where(User.id == user_uuid))
            user = result.scalar_one_or_none()
            if not user:
                return JSONResponse({"error": "User not found"}, status_code=404)

            if user.is_active:
                return JSONResponse(
                    {"error": "You must deactivate your account before deleting it."},
                    status_code=400,
                )

            # Delete the user (cascades to all related data)
            from sqlalchemy import delete as sa_delete

            await db.execute(sa_delete(User).where(User.id == user_uuid))
            await db.commit()

            return JSONResponse({"ok": True, "status": "deleted"})
    except Exception as e:
        logger.error(f"Error deleting account: {e}")
        return JSONResponse({"error": "Failed to delete account"}, status_code=500)


# ---------------------------------------------------------------------------
# Activity Log
# ---------------------------------------------------------------------------


@router.get("/profile/activity")
async def activity_page(request: Request):
    """Legacy route — the activity log now lives at /activity-log as a unified
    day-by-day view backed by tool_call_audit. Permanently redirect."""
    return RedirectResponse("/activity-log", status_code=301)
