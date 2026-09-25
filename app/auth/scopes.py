"""
Permission Scope Tiers

Three tiers control which Google OAuth scopes are requested and
which MCP tools are available per the PRD Section 6.
"""

import logging
from functools import wraps

from app.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Scope tier constants
# ---------------------------------------------------------------------------

TIER_READONLY = "readonly"
TIER_GTM_WRITE = "gtm_write"
TIER_FULL = "full"

GOOGLE_IDENTITY_SCOPES: list[str] = [
    "openid",
    "email",
    "profile",
]

GOOGLE_DATA_SCOPES_BY_TIER = {
    TIER_READONLY: [
        "https://www.googleapis.com/auth/analytics.readonly",
        "https://www.googleapis.com/auth/tagmanager.readonly",
        "https://www.googleapis.com/auth/adwords",
        "https://www.googleapis.com/auth/webmasters.readonly",
    ],
    TIER_GTM_WRITE: [
        "https://www.googleapis.com/auth/analytics.readonly",
        "https://www.googleapis.com/auth/tagmanager.edit.containers",
        "https://www.googleapis.com/auth/tagmanager.publish",
        "https://www.googleapis.com/auth/adwords",
        "https://www.googleapis.com/auth/webmasters.readonly",
    ],
    TIER_FULL: [
        "https://www.googleapis.com/auth/analytics",
        "https://www.googleapis.com/auth/tagmanager.edit.containers",
        "https://www.googleapis.com/auth/tagmanager.publish",
        "https://www.googleapis.com/auth/tagmanager.manage.accounts",
        "https://www.googleapis.com/auth/adwords",
        "https://www.googleapis.com/auth/webmasters",
    ],
}

# Minimum scopes required for each write capability
SCOPE_GTM_EDIT = "https://www.googleapis.com/auth/tagmanager.edit.containers"
SCOPE_GTM_PUBLISH = "https://www.googleapis.com/auth/tagmanager.publish"
SCOPE_GA4_FULL = "https://www.googleapis.com/auth/analytics"
SCOPE_GSC_READONLY = "https://www.googleapis.com/auth/webmasters.readonly"
SCOPE_GSC_WRITE = "https://www.googleapis.com/auth/webmasters"


def _insufficient_scope_response(required_scope: str, app_base_url: str) -> dict:
    return {
        "error": True,
        "error_type": "insufficient_scope",
        "message": f"This action requires '{required_scope}' permission.",
        "action_required": f"Reconnect Google at {app_base_url}/connect with a higher permission tier.",
    }


def require_scope(required_scope: str):
    """
    Decorator for Layer 3 write tools.
    Checks that the user's connection has the required Google OAuth scope.

    Usage:
        @require_scope(SCOPE_GTM_EDIT)
        async def my_write_tool(user_context, ...):
            ...
    """

    def decorator(fn):
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            # user_context is typically passed as first positional arg or as kwarg
            user_context = None
            for arg in args:
                if hasattr(arg, "connections"):
                    user_context = arg
                    break
            if user_context is None:
                user_context = kwargs.get("user_context")

            if user_context is None or not user_context.has_google:
                return {
                    "error": True,
                    "error_type": "connection_missing",
                    "message": "No Google account connected.",
                    "action_required": f"Visit {settings.APP_BASE_URL}/connect to connect your Google account.",
                }

            granted_scopes = user_context.connections[0].scopes if user_context.connections else []
            if required_scope not in granted_scopes:
                return _insufficient_scope_response(required_scope, settings.APP_BASE_URL)

            return await fn(*args, **kwargs)

        return wrapper

    return decorator
