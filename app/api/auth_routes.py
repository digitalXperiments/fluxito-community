"""
Email/password authentication routes.

Handles:
  POST /auth/register              — Create account with email/password
  POST /auth/login                 — Sign in with email/password
  GET  /auth/verify-email          — Verify email from link
  GET  /auth/verify-email-sent     — "Check your inbox" page
  POST /auth/forgot-password       — Request password reset
  GET  /auth/reset-password        — Reset password form
  POST /auth/reset-password        — Process password reset
  POST /auth/resend-verification   — Resend verification email

The GET /signin route (interstitial page) is in google_oauth_routes.py.
"""

import logging

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select

import app.app_state as app_state
from app.auth.email_auth import (
    authenticate_user,
    register_user,
    reset_user_password,
    send_reset_email,
    send_verification_email,
    verify_user_email,
)
from app.auth.uid_cookie import sign_uid
from app.config import settings
from app.models.user import User
from app.templating import render
from app.utils import safe_next_url, trusted_base_url

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _set_auth_cookie(response, user_id: str):
    """Set the uid auth cookie on a response."""
    response.set_cookie(
        "uid",
        sign_uid(user_id),
        max_age=30 * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.APP_ENV == "production",
    )


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    email: str
    password: str
    display_name: str = ""


@router.post("/auth/register")
async def register(payload: RegisterRequest, request: Request):
    """Create a new account with email/password."""
    email = payload.email.strip().lower()
    password = payload.password.strip()
    display_name = payload.display_name.strip() or None

    if not email or "@" not in email:
        return JSONResponse({"error": "Please enter a valid email address."}, status_code=400)
    if len(password) < 8:
        return JSONResponse({"error": "Password must be at least 8 characters."}, status_code=400)

    from app.settings_service import access_approval_required, get_auth_flags

    auth_flags = await get_auth_flags()
    if not auth_flags["password_enabled"]:
        return JSONResponse(
            {"error": "Email and password registration is disabled. Please sign in with Google."},
            status_code=403,
        )

    if await access_approval_required():
        from sqlalchemy import select as _select

        from app.models.user import User as _User

        async with app_state.db_session_factory() as db:
            exists_user = (await db.execute(_select(_User).where(_User.email == email))).scalar_one_or_none()
        if not exists_user:
            return JSONResponse(
                {"error": "This instance is invite-only. Please request access.", "request_access": True},
                status_code=403,
            )

    user, error = await register_user(email, password, display_name)
    if error:
        return JSONResponse({"error": error}, status_code=400)

    # Send verification email
    base_url = trusted_base_url(request)
    try:
        await send_verification_email(email, str(user.id), base_url)
    except Exception:
        logger.exception(f"Failed to send verification email to {email}")

    return JSONResponse(
        {
            "success": True,
            "message": "Account created! Check your email to verify your address.",
            "redirect_url": f"/auth/verify-email-sent?email={email}",
        }
    )


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    email: str
    password: str


@router.post("/auth/login")
async def login(payload: LoginRequest, request: Request):
    """Sign in with email/password."""
    email = payload.email.strip().lower()
    password = payload.password.strip()

    from app.settings_service import get_auth_flags

    auth_flags = await get_auth_flags()
    if not auth_flags["password_enabled"]:
        return JSONResponse(
            {"error": "Email and password sign-in is disabled. Please sign in with Google."},
            status_code=403,
        )

    user, error = await authenticate_user(email, password)

    if error == "UNVERIFIED":
        return JSONResponse(
            {
                "error": "Please verify your email before signing in.",
                "unverified": True,
                "email": email,
            },
            status_code=403,
        )

    if error:
        return JSONResponse({"error": error}, status_code=401)

    # Check tutorial
    needs_tutorial = user.tutorial_completed_at is None

    # Give existing users (e.g. past tutorial or invited members) a personal project if needed.
    # Brand-new users go through the onboarding journey where they create their own named project.
    if not needs_tutorial:
        try:
            from app.api.project_routes import ensure_default_project

            await ensure_default_project(user.id, user.display_name, user.email)
        except Exception:
            logger.warning("ensure_default_project failed on login", exc_info=True)

    # Determine redirect — sanitize ``next`` to prevent open redirects
    # via crafted ``?next=//evil.com`` or ``?next=javascript:...`` values.
    next_url = safe_next_url(request.query_params.get("next"), "/home")
    if needs_tutorial and next_url in ("/home", "/onboard", "/connect"):
        next_url = "/tutorial"

    # Special case: if the user was sent here as part of an MCP OAuth flow
    # (e.g. from Claude), let them continue the authorization after login.
    if next_url and next_url.startswith("/oauth/authorize/resume/"):
        # Trust the continuation token — it was generated by our own OAuth server
        pass  # next_url is already safe to use

    response = JSONResponse(
        {
            "success": True,
            "redirect_url": next_url,
        }
    )
    _set_auth_cookie(response, str(user.id))
    return response


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------


@router.get("/auth/verify-email")
async def verify_email_page(request: Request, token: str = Query(default="")):
    """Process email verification link and show result."""
    if not token:
        return render(
            request,
            "auth/verify_email.html",
            {
                "success": False,
                "error": "Missing verification token.",
            },
        )

    success, error = await verify_user_email(token)

    return render(
        request,
        "auth/verify_email.html",
        {
            "success": success,
            "error": error,
        },
    )


@router.get("/auth/verify-email-sent")
async def verify_email_sent_page(
    request: Request,
    email: str = Query(default=""),
):
    """'Check your inbox' page shown after registration."""
    return render(
        request,
        "auth/verify_email_sent.html",
        {
            "email": email,
        },
    )


@router.post("/auth/resend-verification")
async def resend_verification(request: Request):
    """Resend the verification email."""
    body = await request.json()
    email = body.get("email", "").strip().lower()

    if not email:
        return JSONResponse({"error": "Email is required."}, status_code=400)

    async with app_state.db_session_factory() as db:
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()

    if not user:
        # Don't reveal whether the email exists
        return JSONResponse(
            {"success": True, "message": "If that email is registered, we've sent a verification link."}
        )

    if user.email_verified:
        return JSONResponse({"success": True, "message": "This email is already verified. You can sign in."})

    base_url = trusted_base_url(request)
    try:
        await send_verification_email(email, str(user.id), base_url)
    except Exception:
        logger.exception(f"Failed to resend verification to {email}")

    return JSONResponse({"success": True, "message": "Verification email sent! Check your inbox."})


# ---------------------------------------------------------------------------
# Forgot / Reset password
# ---------------------------------------------------------------------------


@router.post("/auth/forgot-password")
async def forgot_password(request: Request):
    """Send a password reset email."""
    from app.settings_service import get_auth_flags

    auth_flags = await get_auth_flags()
    if not auth_flags["password_enabled"]:
        return JSONResponse(
            {"error": "Password reset is disabled because email/password sign-in is turned off."},
            status_code=403,
        )

    body = await request.json()
    email = body.get("email", "").strip().lower()

    if not email:
        return JSONResponse({"error": "Email is required."}, status_code=400)

    async with app_state.db_session_factory() as db:
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()

    # Always return success to prevent email enumeration
    if user:
        base_url = trusted_base_url(request)
        try:
            await send_reset_email(email, str(user.id), base_url)
        except Exception:
            logger.exception(f"Failed to send reset email to {email}")

    return JSONResponse(
        {
            "success": True,
            "message": "If that email is registered, we've sent a password reset link.",
        }
    )


@router.get("/auth/reset-password")
async def reset_password_page(
    request: Request,
    token: str = Query(default=""),
):
    """Show the reset password form."""
    return render(request, "auth/reset_password.html", {"token": token})


class ResetPasswordRequest(BaseModel):
    token: str
    password: str


@router.post("/auth/reset-password")
async def reset_password(payload: ResetPasswordRequest):
    """Process password reset."""
    from app.settings_service import get_auth_flags

    auth_flags = await get_auth_flags()
    if not auth_flags["password_enabled"]:
        return JSONResponse(
            {"error": "Password reset is disabled because email/password sign-in is turned off."},
            status_code=403,
        )

    if len(payload.password) < 8:
        return JSONResponse({"error": "Password must be at least 8 characters."}, status_code=400)

    success, error = await reset_user_password(payload.token, payload.password)

    if not success:
        return JSONResponse({"error": error}, status_code=400)

    return JSONResponse(
        {
            "success": True,
            "message": "Password reset successfully! You can now sign in.",
            "redirect_url": "/signin",
        }
    )
