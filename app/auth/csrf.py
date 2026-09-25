"""
CSRF protection via double-submit cookie + custom header.

Strategy:
  1. A middleware sets a signed CSRF cookie on every response (if missing).
  2. All state-changing requests (POST/PATCH/PUT/DELETE) to non-API/non-MCP
     paths must include an X-CSRF-Token header matching the cookie value.
  3. The admin/billing/etc. JS already uses fetch() with JSON — the
     base template injects a tiny JS snippet that reads the cookie and
     attaches the header to all fetch() calls automatically.

Exempt paths:
  - /mcp/* — MCP uses Bearer tokens; no browser cookies involved
  - /oauth/* — OAuth flows use state params for CSRF
  - /auth/google/* — Google OAuth callbacks use state params
  - Static files and GET requests (safe methods)

This approach requires zero changes to individual route handlers.
"""

import hashlib
import hmac
import logging
import secrets

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from app.config import settings

logger = logging.getLogger(__name__)

# CSRF configuration constants
_CSRF_COOKIE_NAME = "csrf_token"
_CSRF_HEADER_NAME = "x-csrf-token"
_CSRF_TOKEN_SEPARATOR = "."
_CSRF_SIGNATURE_LENGTH = 16
_CSRF_RAW_TOKEN_BYTES = 32
_CSRF_COOKIE_MAX_AGE = 86400  # 24 hours
_CSRF_HASH_ALGO = hashlib.sha256
_SAFE_METHODS = frozenset(("GET", "HEAD", "OPTIONS", "TRACE"))

# Paths exempt from CSRF validation
_EXEMPT_PREFIXES = (
    "/mcp",  # Streamable HTTP endpoint — matches /mcp and any /mcp/*
    "/oauth/",
    "/api/",  # JSON API — CORS + Content-Type:application/json already prevents CSRF
    "/auth/google/",
    "/static/",
    "/.well-known/",
    "/setup",  # First-run setup — no authenticated session exists yet
)


def _generate_csrf_token() -> str:
    """Generate a random CSRF token and sign it with the app secret."""
    raw = secrets.token_hex(_CSRF_RAW_TOKEN_BYTES)
    sig = hmac.new(settings.APP_SECRET_KEY.encode(), raw.encode(), _CSRF_HASH_ALGO).hexdigest()[
        :_CSRF_SIGNATURE_LENGTH
    ]
    return f"{raw}{_CSRF_TOKEN_SEPARATOR}{sig}"


def _verify_csrf_token(token: str) -> bool:
    """Verify the CSRF token signature using constant-time comparison."""
    try:
        raw, sig = token.rsplit(_CSRF_TOKEN_SEPARATOR, 1)
        expected = hmac.new(settings.APP_SECRET_KEY.encode(), raw.encode(), _CSRF_HASH_ALGO).hexdigest()[
            :_CSRF_SIGNATURE_LENGTH
        ]
        # Use constant-time comparison to prevent timing attacks
        return hmac.compare_digest(sig, expected)
    except (ValueError, AttributeError, TypeError):
        return False


def _all_csrf_cookie_values(request) -> list[str]:
    """Return EVERY ``csrf_token`` value from the raw Cookie header.

    A browser can hold multiple cookies with the same name but different
    domain/path scoping (e.g. a stale ``Domain=.example.com`` cookie left over
    from an earlier deploy alongside the current host-only one). Both are sent
    on the request, but ``request.cookies`` (``http.cookies.SimpleCookie``)
    collapses them to a single value — and JS ``document.cookie`` reads a
    *different* one — which breaks double-submit validation with a spurious
    "token mismatch". Parsing the raw header lets us accept the header token if
    it matches ANY cookie the browser actually sent, which preserves
    double-submit security (the attacker still cannot read the victim's cookies)
    while self-healing duplicate/legacy cookies.
    """
    raw = request.headers.get("cookie", "") or ""
    values: list[str] = []
    for part in raw.split(";"):
        name, sep, val = part.strip().partition("=")
        if sep and name.strip() == _CSRF_COOKIE_NAME:
            values.append(val.strip())
    return values


def _validate_double_submit(cookie_values: "list[str]", header_token: "str | None") -> "str | None":
    """Shared double-submit check. Returns an error message, or None if valid."""
    if not cookie_values or not header_token:
        return "CSRF token missing. Please refresh the page and try again."
    header_values = [part.strip() for part in header_token.split(",") if part.strip()]
    if not header_values:
        return "CSRF token missing. Please refresh the page and try again."
    if len(set(header_values)) != 1:
        return "CSRF token mismatch. Please refresh the page and try again."
    submitted_token = header_values[0]
    # The submitted header must equal one of the cookies the browser sent.
    if not any(hmac.compare_digest(cv, submitted_token) for cv in cookie_values):
        return "CSRF token mismatch. Please refresh the page and try again."
    # ...and that token must be one we actually minted (valid signature).
    if not _verify_csrf_token(submitted_token):
        return "Invalid CSRF token. Please refresh the page and try again."
    return None


def csrf_precheck_asgi(scope) -> "JSONResponse | None":
    """Pure-ASGI CSRF pre-check — returns a 403 JSONResponse or None.

    Used by ``_FluxitoRequestMiddleware`` in main.py. Unlike
    ``csrf_middleware`` (BaseHTTPMiddleware-flavored, buffers body),
    this function only validates the INCOMING request and never
    touches the outgoing response, so it's safe to run in front of
    streaming endpoints like /mcp.

    The ``_ensure_csrf_cookie`` response-side set-cookie logic is
    handled separately by a post-response hook (or we accept that
    cookies are only set on the first page load — CSRF still works
    since the token is long-lived in localStorage after that).
    """
    from starlette.requests import Request as _StarletteRequest

    request = _StarletteRequest(scope)
    method = request.method
    path = request.url.path

    if method in _SAFE_METHODS or any(path.startswith(p) for p in _EXEMPT_PREFIXES):
        return None

    cookie_values = _all_csrf_cookie_values(request)
    header_token = request.headers.get(_CSRF_HEADER_NAME)

    error_message = _validate_double_submit(cookie_values, header_token)
    if error_message is not None:
        logger.warning(
            "CSRF validation failed: %s (path=%s, ip=%s, cookies=%d)",
            error_message,
            path,
            _client_ip(request),
            len(cookie_values),
        )
        return JSONResponse(
            {"error": True, "error_type": "csrf_error", "message": error_message},
            status_code=403,
        )
    return None


async def csrf_middleware(request: Request, call_next) -> Response:
    """
    CSRF double-submit cookie middleware.

    Safe methods and exempt paths pass through.
    State-changing requests must have X-CSRF-Token header matching the cookie.
    """
    method = request.method
    path = request.url.path

    # Safe methods and exempt paths — skip validation
    if method in _SAFE_METHODS or any(path.startswith(p) for p in _EXEMPT_PREFIXES):
        response = await call_next(request)
        _ensure_csrf_cookie(request, response)
        return response

    # State-changing request — validate CSRF token (double-submit, multi-cookie tolerant)
    cookie_values = _all_csrf_cookie_values(request)
    header_token = request.headers.get(_CSRF_HEADER_NAME)

    error_message = _validate_double_submit(cookie_values, header_token)
    if error_message is not None:
        logger.warning(
            "CSRF validation failed: %s (path=%s, ip=%s, cookies=%d)",
            error_message,
            path,
            _client_ip(request),
            len(cookie_values),
        )
        return JSONResponse(
            {"error": True, "error_type": "csrf_error", "message": error_message},
            status_code=403,
        )

    response = await call_next(request)
    return response


def _ensure_csrf_cookie(request: Request, response: Response) -> None:
    """Set the CSRF cookie if it's not already present."""
    if _CSRF_COOKIE_NAME not in request.cookies:
        token = _generate_csrf_token()
        response.set_cookie(
            key=_CSRF_COOKIE_NAME,
            value=token,
            httponly=False,  # Must be readable by JS
            samesite="lax",
            secure=settings.APP_ENV == "production",
            max_age=_CSRF_COOKIE_MAX_AGE,
            path="/",
        )


def _client_ip(request: Request) -> str:
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
