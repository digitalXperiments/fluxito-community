"""Public legal routes for Fluxito.

Provides:
  - GET /privacy and /legal/privacy: Comprehensive Privacy Policy & Google Limited Use disclosure
  - GET /terms and /legal/terms: Terms of Service for hosted service and MCP platform
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.api.google_oauth_routes import _resolve_user_ctx
from app.templating import render

logger = logging.getLogger(__name__)

router = APIRouter(tags=["legal"])


@router.get("/privacy", response_class=HTMLResponse)
@router.get("/legal/privacy", response_class=HTMLResponse)
async def privacy_policy(request: Request):
    """Privacy Policy page, detailing MCP data flows, zero AI training guarantee,
    and Google API Services User Data Policy / Limited Use compliance."""
    user_ctx = await _resolve_user_ctx(request)
    return render(request, "legal/privacy.html", {"user": user_ctx})


@router.get("/terms", response_class=HTMLResponse)
@router.get("/legal/terms", response_class=HTMLResponse)
async def terms_of_service(request: Request):
    """Terms of Service page governing the hosted platform, API, and MCP tool usage."""
    user_ctx = await _resolve_user_ctx(request)
    return render(request, "legal/terms.html", {"user": user_ctx})
