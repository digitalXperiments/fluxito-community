"""Public request-access flow (form submission; GET page added later)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select

import app.app_state as app_state
from app.models.access_request import AccessRequest
from app.models.user import User

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/request-access")
async def request_access_page():
    """Redirect legacy request-access page traffic to signin."""
    return RedirectResponse(url="/signin", status_code=302)


@router.post("/request-access")
async def submit_access_request(request: Request):
    body = await request.json()
    name = (body.get("name") or "").strip()
    email = (body.get("email") or "").strip().lower()
    use_case = (body.get("use_case") or "").strip() or None

    if not name:
        return JSONResponse({"error": "Please enter your name."}, status_code=400)
    if not email or "@" not in email:
        return JSONResponse({"error": "Please enter a valid email address."}, status_code=400)

    async with app_state.db_session_factory() as db:
        existing_user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if existing_user:
            return JSONResponse({"error": "You already have access — please sign in."}, status_code=400)
        pending = (
            await db.execute(
                select(AccessRequest).where(AccessRequest.email == email, AccessRequest.status == "pending")
            )
        ).scalar_one_or_none()
        if pending:
            return JSONResponse({"error": "Your request is already pending review."}, status_code=400)

        db.add(AccessRequest(name=name, email=email, use_case=use_case))
        await db.commit()

    return JSONResponse({"success": True, "message": "Thanks — your request is pending review."})
