"""
Connector error handling — translates raw API exceptions into
user-friendly messages suitable for MCP tool responses.

Each platform's SDK throws different exception types. This module
normalises them into a consistent ConnectorError hierarchy and provides
a decorator that catches and translates exceptions automatically.

Usage on any connector method:

    @friendly_errors("GA4")
    async def run_report(self, ...):
        ...

The decorator catches exceptions, logs the raw error, and re-raises
a ConnectorError with a clean, user-facing message.
"""

import functools
import inspect
import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class ConnectorError(Exception):
    """Base error for all connector failures — safe to surface to users."""

    def __init__(self, message: str, platform: str, original: Exception | None = None):
        self.platform = platform
        self.original = original
        super().__init__(message)


class PlatformUnavailableError(ConnectorError):
    """The platform API is temporarily unreachable."""


class AuthenticationError(ConnectorError):
    """Token expired, revoked, or insufficient permissions."""


class RateLimitError(ConnectorError):
    """The platform is throttling requests."""


class InvalidRequestError(ConnectorError):
    """The request parameters are wrong (user can fix)."""


class QuotaExceededError(ConnectorError):
    """The platform's own quota is exhausted."""


# ---------------------------------------------------------------------------
# Platform-specific error pattern → friendly message mapping
# ---------------------------------------------------------------------------

# Tuples of (substring_to_match, friendly_message, exception_class)
# Optimized: Use tuple instead of list for immutable collection
_ERROR_PATTERNS: tuple[tuple[str, str, type[ConnectorError]], ...] = (
    # Authentication / permissions
    (
        "401",
        "Authentication failed — your {platform} connection may need to be reconnected.",
        AuthenticationError,
    ),
    (
        "403",
        "Permission denied — your {platform} account may not have access to this resource.",
        AuthenticationError,
    ),
    (
        "invalid_grant",
        "Your {platform} token has expired. Please reconnect your account.",
        AuthenticationError,
    ),
    (
        "token has been expired or revoked",
        "Your {platform} token has expired. Please reconnect your account.",
        AuthenticationError,
    ),
    (
        "insufficient_permissions",
        "Your {platform} account doesn't have the required permissions for this operation.",
        AuthenticationError,
    ),
    ("access_denied", "Access denied by {platform}. Check your account permissions.", AuthenticationError),
    ("unauthenticated", "Your {platform} session has expired. Please reconnect.", AuthenticationError),
    # Rate limiting
    ("429", "{platform} is rate-limiting requests. Please wait a moment and try again.", RateLimitError),
    ("rate limit", "{platform} rate limit reached. Please wait a moment and try again.", RateLimitError),
    (
        "too many requests",
        "{platform} rate limit reached. Please wait a moment and try again.",
        RateLimitError,
    ),
    ("resource_exhausted", "{platform} API quota exhausted. Try again in a few minutes.", RateLimitError),
    ("throttl", "{platform} is throttling requests. Please wait and retry.", RateLimitError),
    (
        "user request limit reached",
        "{platform} rate limit reached. Please wait a moment and try again.",
        RateLimitError,
    ),
    # Quota / billing
    (
        "quota",
        "{platform} API quota exceeded. Check your {platform} billing/quota settings.",
        QuotaExceededError,
    ),
    (
        "billing",
        "{platform} billing issue — the API account may need payment method updates.",
        QuotaExceededError,
    ),
    # Temporarily unavailable
    (
        "503",
        "{platform}'s API is temporarily unavailable. Please try again in a few minutes.",
        PlatformUnavailableError,
    ),
    ("502", "{platform}'s API returned a server error. Please try again shortly.", PlatformUnavailableError),
    (
        "504",
        "{platform}'s API timed out. Try a smaller date range or fewer metrics.",
        PlatformUnavailableError,
    ),
    (
        "unavailable",
        "{platform}'s API is temporarily unavailable. Please try again shortly.",
        PlatformUnavailableError,
    ),
    (
        "deadline exceeded",
        "{platform} request timed out. Try a shorter date range or simpler query.",
        PlatformUnavailableError,
    ),
    (
        "connection reset",
        "Connection to {platform} was interrupted. Please try again.",
        PlatformUnavailableError,
    ),
    (
        "connection refused",
        "{platform}'s API is unreachable. Please try again later.",
        PlatformUnavailableError,
    ),
    (
        "timeout",
        "{platform} request timed out. Try a simpler query or shorter date range.",
        PlatformUnavailableError,
    ),
    (
        "internal",
        "{platform} returned an internal error. This is usually temporary — try again.",
        PlatformUnavailableError,
    ),
    (
        "service_unavailable",
        "{platform}'s API is temporarily unavailable. Please try again.",
        PlatformUnavailableError,
    ),
    # Invalid requests (user-fixable)
    (
        "400",
        "{platform} rejected the request — check your parameters (property ID, date range, etc.).",
        InvalidRequestError,
    ),
    ("404", "The requested {platform} resource was not found. Check the ID or name.", InvalidRequestError),
    (
        "not found",
        "The requested {platform} resource was not found. Verify it exists and you have access.",
        InvalidRequestError,
    ),
    (
        "invalid",
        "{platform} rejected the request as invalid. Double-check your parameters.",
        InvalidRequestError,
    ),
    (
        "unknown metric",
        "One of the metrics isn't supported by {platform}. Check the metric names.",
        InvalidRequestError,
    ),
    (
        "unknown dimension",
        "One of the dimensions isn't supported by {platform}. Check the dimension names.",
        InvalidRequestError,
    ),
    ("field_error", "{platform} reported a field error. Check your request parameters.", InvalidRequestError),
)


def _classify_error(exc: Exception, platform: str) -> ConnectorError:
    """Match an exception against known patterns and return a friendly error."""
    error_str = str(exc).lower()

    for pattern, msg_template, error_cls in _ERROR_PATTERNS:
        if pattern in error_str:
            friendly_msg = msg_template.format(platform=platform)
            return error_cls(friendly_msg, platform=platform, original=exc)

    # Fallback — generic but still user-friendly
    return ConnectorError(
        f"{platform} encountered an unexpected error. Please try again, "
        f"or reconnect your {platform} account if the problem persists.",
        platform=platform,
        original=exc,
    )


# ---------------------------------------------------------------------------
# Decorator
# ---------------------------------------------------------------------------


def friendly_errors(platform: str):
    """
    Decorator that catches raw API exceptions from connector methods and
    re-raises them as user-friendly ConnectorError instances.

    Works on both sync and async methods.

    Usage:
        @friendly_errors("GA4")
        async def run_report(self, ...):
            ...
    """

    def decorator(func):
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except ConnectorError:
                raise  # Already wrapped — don't double-wrap
            except Exception as exc:
                logger.error(
                    "%s connector error in %s: %s",
                    platform,
                    func.__name__,
                    exc,
                    exc_info=True,
                )
                raise _classify_error(exc, platform) from exc

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except ConnectorError:
                raise
            except Exception as exc:
                logger.error(
                    "%s connector error in %s: %s",
                    platform,
                    func.__name__,
                    exc,
                    exc_info=True,
                )
                raise _classify_error(exc, platform) from exc

        if inspect.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator
