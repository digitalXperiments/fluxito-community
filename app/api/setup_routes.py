"""
First-run setup wizard.

Routes:
  GET  /setup          — Render setup page (403 if any user already exists)
  POST /setup/email    — Create first admin via email/password, then redirect
                         to /admin/oauth-apps?welcome=1

Google sign-in is handled by /auth/google/start?next=/admin/oauth-apps?welcome=1.
The GET /setup page links to that endpoint directly — no separate route needed.

Security:
  - All routes return 403 once any user exists in the DB.
  - We check `users` table directly, not via session, so there's no way to
    bypass the guard by manipulating cookies.
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import exists, select

import app.app_state as app_state
from app.auth.email_auth import hash_password
from app.models.user import User
from app.templating import render

logger = logging.getLogger(__name__)

router = APIRouter()


async def _any_user_exists() -> bool:
    """Return True if at least one row exists in the users table."""
    async with app_state.db_session_factory() as db:
        result = await db.execute(select(exists().where(User.id.is_not(None))))
        return bool(result.scalar())


@router.get("/setup")
async def setup_page(request: Request):
    """Render the first-run setup page. Returns 403 if already set up."""
    if await _any_user_exists():
        return render(
            request,
            "setup.html",
            {"already_complete": True},
            status_code=403,
        )
    return render(request, "setup.html", {"already_complete": False})


@router.post("/setup/email")
async def setup_email(request: Request):
    """Create the first admin account via email/password.

    Accepts JSON body: {email, password, display_name?}.
    Returns JSON {success, redirect_url} or {error}.
    """
    # Guard: refuse if any user already exists
    if await _any_user_exists():
        return JSONResponse(
            {"error": "Setup is already complete. Please sign in normally."},
            status_code=403,
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body."}, status_code=400)

    email = (body.get("email") or "").strip().lower()
    password = (body.get("password") or "").strip()
    display_name = (body.get("display_name") or "").strip() or None
    project_name = (body.get("project_name") or "").strip() or "My Project"

    if not email or "@" not in email:
        return JSONResponse({"error": "Please enter a valid email address."}, status_code=400)
    if len(password) < 8:
        return JSONResponse({"error": "Password must be at least 8 characters."}, status_code=400)

    # Create the first admin user — skip email verification (self-hosted install)
    try:
        async with app_state.db_session_factory() as db:
            # Double-check inside the session (race guard)
            result = await db.execute(select(exists().where(User.id.is_not(None))))
            if result.scalar():
                return JSONResponse(
                    {"error": "Setup is already complete. Please sign in normally."},
                    status_code=403,
                )

            from app.models.project import Project, ProjectMember

            user = User(
                email=email,
                display_name=display_name,
                password_hash=hash_password(password),
                email_verified=True,  # self-hosted: trust the operator
                auth_provider="email",
                is_superadmin=True,
            )
            db.add(user)
            await db.flush()

            # Create a default project and make the first admin its owner
            import re

            slug = re.sub(r"[^a-z0-9]+", "-", project_name.lower()).strip("-") or "my-project"
            project = Project(
                name=project_name,
                slug=slug,
                owner_id=user.id,
            )
            db.add(project)
            await db.flush()

            membership = ProjectMember(
                project_id=project.id,
                user_id=user.id,
                role="owner",
            )
            db.add(membership)
            await db.commit()

            user_id = str(user.id)
    except Exception:
        logger.exception("Failed to create first admin user")
        return JSONResponse({"error": "Failed to create account. Check server logs."}, status_code=500)

    # Sign them in by setting the uid cookie
    from app.auth.uid_cookie import sign_uid
    from app.config import settings

    response = JSONResponse(
        {
            "success": True,
            "redirect_url": "/admin/oauth-apps?welcome=1",
        }
    )
    response.set_cookie(
        "uid",
        sign_uid(user_id),
        max_age=30 * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.APP_ENV == "production",
    )
    return response
