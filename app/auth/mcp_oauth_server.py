"""
MCP OAuth 2.1 Authorization Server

Implements all endpoints required for Claude.ai to authenticate users:
  GET  /.well-known/oauth-authorization-server  — discovery
  GET  /.well-known/openid-configuration        — OIDC alias
  GET  /oauth/authorize                         — start auth, redirect to Google
  POST /oauth/token                             — token exchange + refresh
  GET  /oauth/userinfo                          — user info
  POST /oauth/revoke                            — revoke token
  GET  /auth/google/identity/callback           — Google identity callback
"""

import base64
import hashlib
import json
import logging
import secrets
import urllib.parse
import uuid
from datetime import datetime, timedelta

import httpx
from fastapi import APIRouter, Form, Query, Request
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

import app.app_state as app_state
from app.config import settings
from app.models.connection import MCPClient
from app.models.mcp_auth_code import MCPAuthCode
from app.models.mcp_session import MCPSession
from app.models.user import User
from app.utils import base_url_from_request

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Approach B helpers (email/password + browser session support for MCP OAuth)
# ---------------------------------------------------------------------------


async def _issue_mcp_auth_code(
    user_id: str,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    code_challenge_method: str,
    scopes: list[str] | None,
) -> str:
    """
    Create and persist an MCPAuthCode for the given user.
    This is the common logic used by both the Google identity path and
    the new browser-session consent path.
    """
    user_uuid = uuid.UUID(str(user_id))
    auth_code_value = secrets.token_urlsafe(32)
    auth_code = MCPAuthCode(
        code=auth_code_value,
        user_id=user_uuid,
        client_id=client_id,
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        scopes=scopes or ["read"],
        expires_at=datetime.utcnow() + timedelta(minutes=10),
    )

    db_session = app_state.db_session_factory()
    async with db_session as db:
        db.add(auth_code)
        await db.commit()

    return auth_code_value


async def _resolve_browser_session_for_mcp_auth(request: Request) -> dict | None:
    """
    Attempt to resolve a usable browser session (uid cookie) for the MCP OAuth flow.

    Returns a dict with at least 'user_id' (and optionally 'project_id') if successful,
    otherwise None.

    This is the key function that enables email/password users and existing
    web sessions to authorize MCP clients without being forced through Google.
    """
    if not settings.MCP_OAUTH_ALLOW_BROWSER_SESSION:
        return None

    try:
        # Late import to avoid circular dependency during early startup
        from app.api.google_oauth_routes import _resolve_user_ctx

        user_ctx = await _resolve_user_ctx(request)
        if user_ctx is None:
            return None

        # Must have at least one active project to be useful for MCP tools
        if not getattr(user_ctx, "projects", None):
            logger.debug("User has no active projects – cannot use for MCP auth")
            return None

        # Choose a reasonable default project (first one for now; can be enhanced later)
        project = user_ctx.projects[0]

        return {
            "user_id": user_ctx.user_id,
            "project_id": getattr(project, "id", None),
            "email": getattr(user_ctx, "email", None),
        }
    except Exception as exc:
        logger.debug(f"Failed to resolve browser session for MCP auth: {exc}")
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _pkce_verify(code_verifier: str, code_challenge: str) -> bool:
    """Verify S256 PKCE code verifier against stored challenge."""
    computed = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).rstrip(b"=").decode()
    return computed == code_challenge


def _build_google_identity_auth_url(state: str, base_url: str, client_id: str) -> str:
    """Build the Google OAuth URL for identity-only login (openid + email + profile)."""
    params = {
        "client_id": client_id,
        "redirect_uri": f"{base_url}/auth/google/identity/callback",
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)


def _url_with_query(base_url: str, params: dict[str, str | None]) -> str:
    """Append query params to a URL while preserving existing query params."""
    parsed = urllib.parse.urlsplit(base_url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query.extend((key, value) for key, value in params.items() if value is not None)
    return urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urllib.parse.urlencode(query),
            parsed.fragment,
        )
    )


def _is_oob_redirect_uri(uri: str | None) -> bool:
    """Detect out-of-band / manual code paste redirect URIs.
    When the client (often a remote/headless process) uses one of these,
    we render a page that displays the code for copy-paste instead of
    redirecting to an unreachable loopback or custom URI.
    """
    if not uri:
        return False
    u = uri.strip().lower()
    if u in {"oob", "urn:ietf:wg:oauth:2.0:oob"}:
        return True
    if u.endswith("/oauth/oob") or u.endswith("/oob"):
        return True
    # Some clients use a localhost oob variant or custom scheme
    return "oob" in u


# ---------------------------------------------------------------------------
# Discovery Endpoints
# ---------------------------------------------------------------------------


@router.get("/.well-known/oauth-authorization-server")
async def oauth_metadata():
    """MCP discovery endpoint — Claude fetches this first."""
    base = settings.APP_BASE_URL
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "userinfo_endpoint": f"{base}/oauth/userinfo",
        "revocation_endpoint": f"{base}/oauth/revoke",
        "scopes_supported": ["read", "write"],
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        # RFC 8707 — signal that we accept the `resource` parameter so
        # Claude's newer MCP client is willing to complete the flow.
        "resource_indicators_supported": True,
    }


@router.get("/.well-known/openid-configuration")
async def openid_configuration():
    """OIDC discovery — some clients check this URL instead."""
    return await oauth_metadata()


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
async def protected_resource_metadata():
    """RFC 9728 — OAuth 2.0 Protected Resource Metadata.

    Newer MCP clients (including current Claude.ai) fetch this to discover
    which authorization server protects the /mcp resource. Without it the
    client gives up before attempting the token exchange.
    """
    base = settings.APP_BASE_URL
    return {
        "resource": f"{base}/mcp",
        "authorization_servers": [base],
        "scopes_supported": ["read", "write"],
        "bearer_methods_supported": ["header"],
    }


# ---------------------------------------------------------------------------
# Dynamic Client Registration
# ---------------------------------------------------------------------------


@router.post("/oauth/register")
async def register_client(request: Request):
    """
    RFC 7591 Dynamic Client Registration endpoint.
    Claude calls this to provision a client_id before starting the OAuth flow.
    """
    data = await request.json()
    redirect_uris = data.get("redirect_uris", [])
    client_name = data.get("client_name", f"Dynamic Client {str(uuid.uuid4())[:8]}")

    if not redirect_uris:
        raise HTTPException(status_code=400, detail="redirect_uris is required")

    client_id = f"mcp_client_{secrets.token_hex(16)}"

    db_session: AsyncSession = app_state.db_session_factory()
    async with db_session as db:
        client = MCPClient(
            client_id=client_id,
            client_name=client_name,
            redirect_uris=redirect_uris,
            allowed_scopes=["read"],
            is_public=True,
        )
        db.add(client)
        await db.commit()

    return {
        "client_id": client_id,
        "client_name": client_name,
        "redirect_uris": redirect_uris,
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }


# ---------------------------------------------------------------------------
# Authorization Endpoint
# ---------------------------------------------------------------------------


def _redirect_host(redirect_uri: str) -> str:
    """Host (or app scheme) the authorization code will be sent to — shown on consent."""
    from urllib.parse import urlsplit

    parts = urlsplit(redirect_uri or "")
    return parts.netloc or (f"{parts.scheme}://" if parts.scheme else "")


@router.get("/oauth/authorize")
async def authorize(
    request: Request,
    response_type: str = Query(...),
    client_id: str = Query(...),
    redirect_uri: str = Query(...),
    scope: str = Query(default="read"),
    state: str = Query(...),
    code_challenge: str = Query(...),
    code_challenge_method: str = Query(default="S256"),
    resource: str | None = Query(default=None),
):
    """
    Step 2 of MCP OAuth flow.
    Stores OAuth params in Redis, then redirects user to Google identity login.
    """
    if response_type != "code":
        raise HTTPException(400, "unsupported_response_type")
    if code_challenge_method != "S256":
        raise HTTPException(400, "unsupported_code_challenge_method")

    # Validate / auto-register client
    # Claude.ai generates a unique client_id per installation, so we
    # auto-register any unknown public client on first use.
    redis = app_state.redis_client
    db_session: AsyncSession = app_state.db_session_factory()

    async with db_session as db:
        result = await db.execute(select(MCPClient).where(MCPClient.client_id == client_id))
        client = result.scalar_one_or_none()

        if not client:
            # Auto-register — allow any PKCE public client (Claude pattern)
            client = MCPClient(
                client_id=client_id,
                client_name=f"Auto-registered: {client_id[:20]}",
                redirect_uris=[redirect_uri],
                allowed_scopes=["read"],
                is_public=True,
            )
            db.add(client)
            await db.commit()
        elif redirect_uri not in (client.redirect_uris or []):
            # Never widen a registered client's redirect URIs from /authorize:
            # anyone could otherwise reuse a real client's id with their own
            # redirect_uri and receive the victim's authorization code.
            raise HTTPException(400, "invalid_redirect_uri")

    # Derive the public base URL from the incoming request so OAuth
    # callbacks redirect to the correct host (not hardcoded localhost).
    from app.utils import base_url_from_request

    req_base_url = base_url_from_request(request)

    # Store OAuth params in Redis for 10 minutes
    state_key = f"mcp_oauth_state:{state}"
    await redis.setex(
        state_key,
        600,
        json.dumps(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "scope": scope,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": code_challenge_method,
                "base_url": req_base_url,
                "resource": resource,
            }
        ),
    )

    if not settings.MCP_OAUTH_ALLOW_BROWSER_SESSION:
        # Legacy behavior: go straight through Google identity auth instead
        # of the web sign-in/consent flow.
        from app.auth.oauth_app_credentials import get_oauth_app_credentials

        async with app_state.db_session_factory() as _cred_db:
            _google_creds = await get_oauth_app_credentials(_cred_db, "google")
        google_url = _build_google_identity_auth_url(
            state=state, base_url=req_base_url, client_id=_google_creds.client_id
        )
        return RedirectResponse(url=google_url, status_code=302)

    # ==================================================================
    # Approach B: Try browser session first (enables email/password users)
    # ==================================================================
    browser_session = await _resolve_browser_session_for_mcp_auth(request)

    if browser_session is not None:
        # We have a valid logged-in browser user with at least one project.
        # Render the consent screen (Approach B).
        user_id = browser_session["user_id"]
        logger.info(
            "MCP OAuth authorize: valid browser session detected for user %s. " "Showing consent screen.",
            user_id,
        )

        # Store the resolved user_id together with the OAuth params.
        # This lets the decision handler create a real MCPAuthCode without
        # re-authenticating via Google.
        # consent_nonce binds the decision POST to this rendered consent page;
        # the decision also re-checks the browser session is this user.
        consent_nonce = secrets.token_urlsafe(24)
        state_data = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scope,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
            "base_url": req_base_url,
            "resource": resource,
            "user_id": user_id,
            "project_id": browser_session.get("project_id"),
            "consent_nonce": consent_nonce,
        }
        await redis.setex(state_key, 600, json.dumps(state_data))

        # Enrich consent screen data
        client_display_name = client_id
        project_display_name = None
        account_display_name = browser_session.get("email") or "Signed in"
        account_email = browser_session.get("email")

        # Try to get a friendly client name from the MCPClient table
        try:
            db = app_state.db_session_factory()
            async with db as session:
                from app.models.connection import MCPClient as MCPClientModel

                client_row = (
                    await session.execute(select(MCPClientModel).where(MCPClientModel.client_id == client_id))
                ).scalar_one_or_none()
                if client_row and client_row.client_name:
                    client_display_name = client_row.client_name
        except Exception:
            pass

        # Try to get the browser user's friendly display name for the consent header
        try:
            db = app_state.db_session_factory()
            async with db as session:
                user_row = (
                    await session.execute(select(User).where(User.id == uuid.UUID(str(user_id))))
                ).scalar_one_or_none()
                if user_row:
                    account_display_name = user_row.display_name or user_row.email
                    account_email = user_row.email
        except Exception:
            pass

        # Try to get a friendly project name if we have the id
        if browser_session.get("project_id"):
            try:
                db = app_state.db_session_factory()
                async with db as session:
                    from app.models.project import Project

                    proj = (
                        await session.execute(
                            select(Project).where(Project.id == browser_session["project_id"])
                        )
                    ).scalar_one_or_none()
                    if proj:
                        project_display_name = proj.name
            except Exception:
                pass

        consent_context = {
            "state": state,
            "client_name": client_display_name,
            "scopes": scope or "read",
            "project_name": project_display_name,
            "account_display_name": account_display_name,
            "account_email": account_email,
            "account_initials": (account_display_name or account_email or "U")[:2].upper(),
            "consent_nonce": consent_nonce,
            "redirect_host": _redirect_host(redirect_uri),
        }

        from app.templating import render

        return render(request, "auth/mcp_consent.html", consent_context)

    # ==================================================================
    # No valid browser session → redirect to sign-in with continuation
    # (this is the key piece that enables email/password users with no
    # prior web session)
    # ==================================================================
    continue_token = secrets.token_urlsafe(24)
    continue_key = f"mcp_oauth_continue:{continue_token}"

    # Store enough information to resume the exact same OAuth request
    await redis.setex(
        continue_key,
        900,  # 15 minutes
        json.dumps(
            {
                "original_state_key": state_key,
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "scope": scope,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": code_challenge_method,
                "base_url": req_base_url,
                "resource": resource,
            }
        ),
    )

    # Send the user to the normal sign-in page.
    # After they log in (email/password or Google), the login handlers will
    # detect the special next URL and resume the OAuth flow.
    signin_url = f"/signin?next=/oauth/authorize/resume/{continue_token}"
    logger.info(
        "MCP OAuth authorize: no browser session. Redirecting to sign-in with continuation (token=%s...)",
        continue_token[:8],
    )
    return RedirectResponse(url=signin_url, status_code=302)


@router.post("/oauth/authorize/decision")
async def authorize_decision(
    request: Request,
    state: str = Form(...),
    consent: str = Form(...),
    consent_nonce: str = Form(default=""),
    read_only: str = Form(default=""),
):
    """
    Handles the user's decision on the MCP consent screen (Approach B).

    The POST must come from the browser that was shown the consent page: it
    has to carry that page's one-time ``consent_nonce`` and be signed in as the
    same user. Without this, anyone holding a ``state`` the victim's browser
    had opened could approve the grant themselves and collect the code.
    """
    import hmac as _hmac

    from app.auth.uid_cookie import get_uid_from_request

    redis = app_state.redis_client
    state_key = f"mcp_oauth_state:{state}"
    params_raw = await redis.get(state_key)

    if not params_raw:
        return JSONResponse({"error": "invalid_or_expired_state"}, status_code=400)

    params = json.loads(params_raw)
    expected_nonce = params.get("consent_nonce") or ""
    browser_uid = get_uid_from_request(request)
    if (
        not expected_nonce
        or not _hmac.compare_digest(str(consent_nonce), str(expected_nonce))
        or not browser_uid
        or str(browser_uid) != str(params.get("user_id"))
    ):
        logger.warning("MCP OAuth decision rejected: consent not bound to this browser session")
        await redis.delete(state_key)
        return JSONResponse({"error": "invalid_consent"}, status_code=403)

    if consent != "allow":
        # User denied
        redirect_uri = params.get("redirect_uri")
        client_state = params.get("state")
        error_url = _url_with_query(
            redirect_uri,
            {
                "error": "access_denied",
                "state": client_state,
            },
        )
        await redis.delete(state_key)
        return RedirectResponse(url=error_url, status_code=303)

    # Real path: create a proper MCPAuthCode for the user who consented via browser session
    user_id = params.get("user_id")
    if not user_id:
        # This can happen if the user had a session when the consent screen was shown
        # but it expired or they cleared cookies before clicking Allow.
        logger.warning("MCP OAuth decision called without user_id in state (stale consent?)")
        await redis.delete(state_key)
        return RedirectResponse(
            url=_url_with_query(
                params.get("redirect_uri"),
                {
                    "error": "login_required",
                    "state": params.get("state"),
                },
            ),
            status_code=303,
        )

    auth_code_value = await _issue_mcp_auth_code(
        user_id=user_id,
        client_id=params["client_id"],
        redirect_uri=params["redirect_uri"],
        code_challenge=params["code_challenge"],
        code_challenge_method=params["code_challenge_method"],
        # What the user chose on the consent screen decides the grant.
        scopes=["read"] if read_only in ("1", "on", "true") else ["read", "write"],
    )

    await redis.delete(state_key)

    redirect_uri = params.get("redirect_uri", "")
    if _is_oob_redirect_uri(redirect_uri):
        logger.info(
            "MCP auth code issued via browser session consent (OOB) for user %s (client=%s)",
            user_id,
            params["client_id"],
        )
        from app.templating import render

        return render(
            request,
            "auth/mcp_oob_success.html",
            {"code": auth_code_value, "state": params.get("state")},
        )

    success_url = _url_with_query(
        redirect_uri,
        {
            "code": auth_code_value,
            "state": params["state"],
            "scope": params.get("scope"),
        },
    )

    logger.info(
        "MCP auth code issued via browser session consent for user %s (client=%s)",
        user_id,
        params["client_id"],
    )
    # This handler receives a browser POST from the consent form. Use 303 so
    # the OAuth client callback is reached with GET; Starlette's default 307
    # preserves POST and makes Claude's callback return Method Not Allowed.
    return RedirectResponse(url=success_url, status_code=303)


@router.get("/oauth/authorize/resume/{continue_token}")
async def resume_mcp_authorize(request: Request, continue_token: str):
    """
    Resumes an MCP OAuth authorization request after the user has
    completed sign-in (email/password or Google web sign-in).

    This closes the loop for users who had no browser session when
    they started the flow from Claude.
    """
    redis = app_state.redis_client
    continue_key = f"mcp_oauth_continue:{continue_token}"

    data_raw = await redis.get(continue_key)
    if not data_raw:
        # Expired or invalid continuation token
        return RedirectResponse(url="/signin?error=oauth_session_expired", status_code=302)

    data = json.loads(data_raw)
    await redis.delete(continue_key)

    # Restore the original OAuth state so the authorize handler can pick it up
    original_state_key = data["original_state_key"]
    await redis.setex(
        original_state_key,
        600,
        json.dumps(
            {
                "client_id": data["client_id"],
                "redirect_uri": data["redirect_uri"],
                "scope": data["scope"],
                "state": data["state"],
                "code_challenge": data["code_challenge"],
                "code_challenge_method": data["code_challenge_method"],
                "base_url": data["base_url"],
                "resource": data.get("resource"),
            }
        ),
    )

    # Re-enter the normal authorize flow.
    # Because the user just logged in, they should now have a valid uid cookie.
    # The authorize handler will detect the browser session and show the consent screen.
    original_params = {
        "response_type": "code",
        "client_id": data["client_id"],
        "redirect_uri": data["redirect_uri"],
        "scope": data.get("scope", "read"),
        "state": data["state"],
        "code_challenge": data["code_challenge"],
        "code_challenge_method": data["code_challenge_method"],
    }
    if data.get("resource"):
        original_params["resource"] = data["resource"]

    from urllib.parse import urlencode

    resume_url = "/oauth/authorize?" + urlencode(original_params)
    return RedirectResponse(url=resume_url, status_code=302)


# ---------------------------------------------------------------------------
# Google Identity Callback (after user logs in with Google)
# ---------------------------------------------------------------------------


@router.get("/auth/google/identity/callback")
async def google_identity_callback(
    request: Request,
    code: str = Query(...),
    state: str = Query(...),
    error: str | None = Query(default=None),
):
    """
    Step 3 of MCP OAuth flow.
    Google redirects here after identity login.
    Creates/finds user, issues MCP auth code, redirects to Claude.
    """
    if error:
        raise HTTPException(400, f"Google auth error: {error}")

    redis = app_state.redis_client

    # Retrieve stored OAuth params
    state_key = f"mcp_oauth_state:{state}"
    params_raw = await redis.get(state_key)
    if not params_raw:
        raise HTTPException(400, "invalid_state")

    params = json.loads(params_raw)
    await redis.delete(state_key)

    # Use the base_url that was stored when the auth flow started so the
    # redirect_uri matches exactly (required by Google's token exchange).
    stored_base_url = params.get("base_url") or base_url_from_request(request)
    identity_redirect_uri = f"{stored_base_url}/auth/google/identity/callback"

    # Exchange code for Google identity tokens
    from app.auth.oauth_app_credentials import get_oauth_app_credentials

    async with app_state.db_session_factory() as _cred_db:
        _google_creds = await get_oauth_app_credentials(_cred_db, "google")

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": _google_creds.client_id,
                "client_secret": _google_creds.client_secret,
                "code": code,
                "redirect_uri": identity_redirect_uri,
                "grant_type": "authorization_code",
            },
        )

    if token_resp.status_code != 200:
        raise HTTPException(400, "Failed to exchange Google identity token")

    token_data = token_resp.json()
    id_token_encoded = token_data.get("id_token")
    if not id_token_encoded:
        raise HTTPException(400, "No id_token in Google response")

    # Decode JWT payload (no signature verification needed for our own flow)
    payload_b64 = id_token_encoded.split(".")[1]
    # Add padding
    padding = 4 - len(payload_b64) % 4
    if padding != 4:
        payload_b64 += "=" * padding

    google_user = json.loads(base64.urlsafe_b64decode(payload_b64))

    email = google_user.get("email")
    display_name = google_user.get("name")

    if not email:
        raise HTTPException(400, "Could not retrieve email from Google")
    # Only a Google-verified address may claim or create a Fluxito account.
    if google_user.get("email_verified") is not True:
        raise HTTPException(400, "Your Google account's email address isn't verified")

    # Create or upsert user
    db_session = app_state.db_session_factory()
    async with db_session as db:
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()

        if not user:
            user = User(email=email, display_name=display_name)
            db.add(user)
            await db.flush()
        elif display_name and not user.display_name:
            user.display_name = display_name
        if getattr(user, "deactivated_by_admin", False) or getattr(user, "is_active", True) is False:
            raise HTTPException(403, "This account is deactivated")

        # Issue MCP auth code
        auth_code_value = secrets.token_urlsafe(32)
        auth_code = MCPAuthCode(
            code=auth_code_value,
            user_id=user.id,
            client_id=params["client_id"],
            redirect_uri=params["redirect_uri"],
            code_challenge=params["code_challenge"],
            code_challenge_method=params["code_challenge_method"],
            scopes=["read", "write"],
            expires_at=datetime.utcnow() + timedelta(minutes=10),
        )
        db.add(auth_code)
        await db.commit()

    # Redirect to client's redirect_uri (or OOB display page for headless/remote flows).
    import logging as _logging

    redirect_uri = params.get("redirect_uri", "")
    if _is_oob_redirect_uri(redirect_uri):
        _logging.getLogger(__name__).info(
            "MCP auth code issued (OOB) → showing code page (client=%s, scope=%s, resource=%s)",
            params["client_id"],
            params.get("scope"),
            params.get("resource"),
        )
        from app.templating import render

        return render(
            request,
            "auth/mcp_oob_success.html",
            {"code": auth_code_value, "state": params.get("state")},
        )

    # Echo back `scope` too — some MCP clients validate it matches request.
    redirect_params = {
        "code": auth_code_value,
        "state": params["state"],
    }
    if params.get("scope"):
        redirect_params["scope"] = params["scope"]

    redirect_url = _url_with_query(redirect_uri, redirect_params)

    _logging.getLogger(__name__).info(
        "MCP auth code issued → redirecting to %s (client=%s, scope=%s, resource=%s)",
        redirect_uri,
        params["client_id"],
        params.get("scope"),
        params.get("resource"),
    )
    return RedirectResponse(url=redirect_url, status_code=302)


# ---------------------------------------------------------------------------
# Token Endpoint
# ---------------------------------------------------------------------------


@router.post("/oauth/token")
async def token(
    request: Request,
    grant_type: str = Form(...),
    code: str | None = Form(default=None),
    redirect_uri: str | None = Form(default=None),
    client_id: str = Form(...),
    code_verifier: str | None = Form(default=None),
    refresh_token: str | None = Form(default=None),
):
    """Token endpoint — exchange auth code or refresh token."""
    if grant_type == "authorization_code":
        return await _handle_auth_code_grant(request, code, redirect_uri, client_id, code_verifier)
    elif grant_type == "refresh_token":
        return await _handle_refresh_token_grant(request, refresh_token, client_id)
    else:
        raise HTTPException(400, "unsupported_grant_type")


async def _handle_auth_code_grant(request, code, redirect_uri, client_id, code_verifier):
    if not all([code, redirect_uri, code_verifier]):
        raise HTTPException(400, "missing required parameters for authorization_code grant")

    db_session = app_state.db_session_factory()
    async with db_session as db:
        result = await db.execute(select(MCPAuthCode).where(MCPAuthCode.code == code))
        auth_code = result.scalar_one_or_none()

        if not auth_code:
            raise HTTPException(400, "invalid_grant")
        if auth_code.used:
            raise HTTPException(400, "invalid_grant")
        if auth_code.expires_at < datetime.utcnow():
            raise HTTPException(400, "invalid_grant")
        if auth_code.redirect_uri != redirect_uri:
            raise HTTPException(400, "invalid_grant")
        if auth_code.client_id != client_id:
            raise HTTPException(400, "invalid_grant")

        if not _pkce_verify(code_verifier, auth_code.code_challenge):
            raise HTTPException(400, "invalid_grant")

        # Mark code as used
        await db.execute(update(MCPAuthCode).where(MCPAuthCode.id == auth_code.id).values(used=True))

        # Issue MCP tokens
        access_token = secrets.token_urlsafe(32)
        new_refresh_token = secrets.token_urlsafe(32)

        session = MCPSession(
            user_id=auth_code.user_id,
            access_token_hash=_sha256(access_token),
            refresh_token_hash=_sha256(new_refresh_token),
            client_id=client_id,
            scopes=auth_code.scopes,
            access_token_expires_at=datetime.utcnow() + timedelta(hours=8),
            refresh_token_expires_at=datetime.utcnow() + timedelta(days=365),
        )
        db.add(session)
        await db.commit()

    return JSONResponse(
        {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": 28800,  # 8 hours
            "refresh_token": new_refresh_token,
            "scope": " ".join(auth_code.scopes or ["read"]),
        }
    )


async def _handle_refresh_token_grant(request, refresh_token, client_id):
    if not refresh_token:
        raise HTTPException(400, "missing refresh_token")

    old_refresh_hash = _sha256(refresh_token)
    db_session = app_state.db_session_factory()

    async with db_session as db:
        result = await db.execute(select(MCPSession).where(MCPSession.refresh_token_hash == old_refresh_hash))
        session = result.scalar_one_or_none()

        if not session:
            raise HTTPException(400, "invalid_grant")
        if session.is_revoked:
            raise HTTPException(400, "invalid_grant")
        if session.refresh_token_expires_at and session.refresh_token_expires_at < datetime.utcnow():
            raise HTTPException(400, "invalid_grant")
        # A refresh token is only good for the client it was issued to.
        if session.client_id and client_id and session.client_id != client_id:
            raise HTTPException(400, "invalid_grant")
        old_access_hash = session.access_token_hash

        # Issue new access token + rotate refresh token (rolling window)
        new_access_token = secrets.token_urlsafe(32)
        new_refresh_token = secrets.token_urlsafe(32)
        new_access_expires = datetime.utcnow() + timedelta(hours=8)
        # Slide the refresh token expiry forward another year on each use
        new_refresh_expires = datetime.utcnow() + timedelta(days=365)

        await db.execute(
            update(MCPSession)
            .where(MCPSession.id == session.id)
            .values(
                access_token_hash=_sha256(new_access_token),
                access_token_expires_at=new_access_expires,
                refresh_token_hash=_sha256(new_refresh_token),
                refresh_token_expires_at=new_refresh_expires,
            )
        )
        await db.commit()

    # The session cache is keyed by the *access* token hash: drop the old one so
    # the superseded access token stops working now, not when its cache expires.
    redis = app_state.redis_client
    await redis.delete(f"mcp_session:{old_access_hash}")

    return JSONResponse(
        {
            "access_token": new_access_token,
            "token_type": "Bearer",
            "expires_in": 28800,  # 8 hours
            "refresh_token": new_refresh_token,
            "scope": " ".join(session.scopes or ["read"]),
        }
    )


# ---------------------------------------------------------------------------
# Userinfo Endpoint
# ---------------------------------------------------------------------------


@router.get("/oauth/userinfo")
async def userinfo(request: Request):
    """Returns basic user info for the authenticated MCP session."""
    from app.auth.mcp_session_manager import require_valid_mcp_token

    user_context = await require_valid_mcp_token(request)
    return {
        "sub": str(user_context.user_id),
        "email": user_context.email,
        "name": user_context.display_name,
    }


# ---------------------------------------------------------------------------
# Revoke Endpoint
# ---------------------------------------------------------------------------


@router.post("/oauth/revoke")
async def revoke(
    request: Request,
    token: str = Form(...),
    token_type_hint: str | None = Form(default=None),
):
    """Revokes an MCP access or refresh token."""
    token_hash = _sha256(token)
    db_session = app_state.db_session_factory()

    async with db_session as db:
        # Try access token hash first
        result = await db.execute(select(MCPSession).where(MCPSession.access_token_hash == token_hash))
        session = result.scalar_one_or_none()

        if not session:
            # Try refresh token hash
            result = await db.execute(select(MCPSession).where(MCPSession.refresh_token_hash == token_hash))
            session = result.scalar_one_or_none()

        if session:
            await db.execute(update(MCPSession).where(MCPSession.id == session.id).values(is_revoked=True))
            await db.commit()
            # Remove from Redis cache
            await app_state.redis_client.delete(f"mcp_session:{session.access_token_hash}")

    # Always return 200 per RFC 7009
    return JSONResponse({"status": "ok"})
