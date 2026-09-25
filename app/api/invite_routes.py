"""Project invitation routes.

GET    /invite/<token>                         — the one-time invite page
POST   /api/invites/by-token/accept            — signed-in invitee accepts via the link  {token}
POST   /api/invites/by-token/register          — new person creates their account + accepts  {token, password, display_name}
GET    /api/invites/mine                       — open invites for the signed-in user (in-app banner)
POST   /api/invites/<id>/accept | /decline     — respond from the banner
GET    /api/project/<slug>/invites             — pending invites (owner/admin)
DELETE /api/project/<slug>/invites/<id>        — revoke (owner/admin)
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select

import app.app_state as app_state
from app.auth.uid_cookie import get_uid_from_request, sign_uid
from app.config import settings
from app.models.user import User
from app.services import invites as svc

router = APIRouter()


async def _signed_in_user(request: Request) -> User | None:
    uid = get_uid_from_request(request)
    if not uid:
        return None
    try:
        async with app_state.db_session_factory() as db:
            return await db.get(User, uuid.UUID(uid))
    except (ValueError, TypeError):
        return None


def _set_session(response, user_id: str) -> None:
    response.set_cookie(
        "uid",
        sign_uid(user_id),
        max_age=30 * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.APP_ENV == "production",
    )


@router.get("/invite/{token}", response_class=HTMLResponse)
async def invite_page(request: Request, token: str):
    from app.models.project import Project
    from app.templating import render

    inv = await svc.get_by_token(token)
    project_name = None
    if inv is not None:
        async with app_state.db_session_factory() as db:
            proj = await db.get(Project, inv.project_id)
            project_name = proj.name if proj and proj.is_active else None
    valid = inv is not None and project_name is not None and svc._is_open(inv)

    me = await _signed_in_user(request)
    account_exists = False
    if valid:
        async with app_state.db_session_factory() as db:
            account_exists = (
                await db.execute(select(User.id).where(User.email == inv.email))
            ).first() is not None

    if not valid:
        state = "invalid"
    elif me is not None and (me.email or "").lower() == inv.email:
        state = "accept"
    elif me is not None:
        state = "wrong_account"
    elif account_exists:
        state = "sign_in"
    else:
        state = "register"

    return render(
        request,
        "auth/invite.html",
        {
            "state": state,
            "token": token,
            "invite_email": inv.email if inv else None,
            "role": inv.role if inv else None,
            "project_name": project_name,
            "me_email": me.email if me else None,
        },
    )


@router.post("/api/invites/by-token/accept")
async def accept_by_token(request: Request):
    me = await _signed_in_user(request)
    if me is None:
        raise HTTPException(401, "Sign in to accept this invitation")
    body = await request.json()
    inv = await svc.get_by_token(str(body.get("token") or ""))
    if inv is None:
        raise HTTPException(404, "This invitation is no longer valid.")
    ok, detail = await svc.accept(inv.id, user_id=str(me.id), user_email=me.email)
    if not ok:
        raise HTTPException(400, detail)
    resp = JSONResponse({"success": True, "redirect": "/home"})
    from app.api.project_routes import set_active_project_cookie

    set_active_project_cookie(resp, detail)
    return resp


@router.post("/api/invites/by-token/register")
async def register_from_invite(request: Request):
    """A new person creates their account from the invite link and joins the project.

    The one-time token (sent to / handed over for this address) stands in for
    email verification, as the temporary password used to.
    """
    from app.auth.email_auth import hash_password

    body = await request.json()
    token = str(body.get("token") or "")
    password = str(body.get("password") or "")
    display_name = str(body.get("display_name") or "").strip()[:255] or None
    if len(password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters.")
    inv = await svc.get_by_token(token)
    if inv is None or not svc._is_open(inv):
        raise HTTPException(404, "This invitation is no longer valid.")

    from datetime import datetime

    async with app_state.db_session_factory() as db:
        if (await db.execute(select(User.id).where(User.email == inv.email))).first():
            raise HTTPException(409, "An account with this email already exists — sign in to accept.")
        user = User(
            email=inv.email,
            display_name=display_name,
            password_hash=hash_password(password),
            email_verified=True,
            email_verified_at=datetime.utcnow(),
            auth_provider="email",
        )
        db.add(user)
        await db.commit()
        user_id = str(user.id)

    ok, detail = await svc.accept(inv.id, user_id=user_id, user_email=inv.email)
    if not ok:
        raise HTTPException(400, detail)
    resp = JSONResponse({"success": True, "redirect": "/home"})
    _set_session(resp, user_id)
    from app.api.project_routes import set_active_project_cookie

    set_active_project_cookie(resp, detail)
    return resp


@router.get("/api/invites/mine")
async def my_invites(request: Request):
    me = await _signed_in_user(request)
    if me is None:
        return JSONResponse({"invites": []})
    return JSONResponse({"invites": await svc.pending_for_email(me.email)})


@router.post("/api/invites/{invite_id}/accept")
async def accept_invite(request: Request, invite_id: str):
    me = await _signed_in_user(request)
    if me is None:
        raise HTTPException(401, "Not authenticated")
    try:
        iid = uuid.UUID(invite_id)
    except ValueError:
        raise HTTPException(404, "Invitation not found")
    ok, detail = await svc.accept(iid, user_id=str(me.id), user_email=me.email)
    if not ok:
        raise HTTPException(400, detail)
    resp = JSONResponse({"success": True})
    from app.api.project_routes import set_active_project_cookie

    set_active_project_cookie(resp, detail)
    return resp


@router.post("/api/invites/{invite_id}/decline")
async def decline_invite(request: Request, invite_id: str):
    me = await _signed_in_user(request)
    if me is None:
        raise HTTPException(401, "Not authenticated")
    try:
        iid = uuid.UUID(invite_id)
    except ValueError:
        raise HTTPException(404, "Invitation not found")
    if not await svc.decline(iid, user_email=me.email):
        raise HTTPException(404, "Invitation not found")
    return JSONResponse({"success": True})


async def _admin_project(request: Request, slug: str):
    from app.api.project_routes import _get_membership, _get_project_by_slug, _resolve_user
    from app.models.project import CAN_MANAGE_MEMBERS_ROLES

    user = await _resolve_user(request)
    if not user:
        raise HTTPException(401, "Not authenticated")
    project = await _get_project_by_slug(slug)
    if not project:
        raise HTTPException(404, "Project not found")
    m = await _get_membership(project.id, uuid.UUID(user["user_id"]))
    if not m or m.role not in CAN_MANAGE_MEMBERS_ROLES:
        raise HTTPException(403, "Only owners and admins can manage invitations")
    return project


@router.get("/api/project/{slug}/invites")
async def project_invites(request: Request, slug: str):
    project = await _admin_project(request, slug)
    rows = await svc.list_for_project(project.id)
    for r in rows:
        r["created_at"] = r["created_at"].isoformat() if r["created_at"] else None
        r["expires_at"] = r["expires_at"].isoformat() if r["expires_at"] else None
    return JSONResponse({"invites": rows})


@router.delete("/api/project/{slug}/invites/{invite_id}")
async def revoke_invite(request: Request, slug: str, invite_id: str):
    project = await _admin_project(request, slug)
    try:
        iid = uuid.UUID(invite_id)
    except ValueError:
        raise HTTPException(404, "Invitation not found")
    if not await svc.revoke(iid, project_id=project.id):
        raise HTTPException(404, "Invitation not found")
    return JSONResponse({"success": True})
