"""Shared helpers for the signed ``uid`` browser session cookie.

The cookie value has the format ``<user_id>.<hmac_sha256_of_user_id>``, signed
with ``settings.APP_SECRET_KEY``. These three helpers replaced six near-identical
copies of sign/verify/extract logic that had drifted across the api/ modules.
"""

import hashlib
import hmac as _hmac

from fastapi import Request

from app.config import settings


def sign_uid(user_id: str) -> str:
    """Return a signed cookie value for ``user_id``."""
    sig = _hmac.new(settings.APP_SECRET_KEY.encode(), user_id.encode(), hashlib.sha256).hexdigest()
    return f"{user_id}.{sig}"


def verify_uid(cookie_val: str | None) -> str | None:
    """Return the raw ``user_id`` if the signature matches, else ``None``."""
    if not cookie_val:
        return None
    try:
        uid, sig = cookie_val.rsplit(".", 1)
        expected = _hmac.new(settings.APP_SECRET_KEY.encode(), uid.encode(), hashlib.sha256).hexdigest()
        if _hmac.compare_digest(sig, expected):
            return uid
    except Exception:
        pass
    return None


def get_uid_from_request(request: Request) -> str | None:
    """Extract and verify the ``uid`` cookie from an incoming request."""
    return verify_uid(request.cookies.get("uid"))
