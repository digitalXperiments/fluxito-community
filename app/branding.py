"""Instance branding (whitelabel) — name, logo, accent.

A module-level cache warmed at startup and refreshed on the admin PATCH. `brand()`
is sync and non-blocking (Jinja global); `refresh_brand()` reloads it from settings.
"""

from __future__ import annotations

import logging

import app.app_state as app_state

logger = logging.getLogger(__name__)

_DEFAULTS = {"name": "Fluxito", "logo_url": "", "accent": ""}
_BRAND_CACHE: dict = dict(_DEFAULTS)

# Site-wide announcement banner — same sync-cache pattern as brand().
_ANNOUNCEMENT_CACHE: dict = {"text": ""}

# Site-wide Google Tag Manager (GTM) container ID — same sync-cache pattern.
_GTM_CACHE: dict = {"container_id": ""}

# In-app Chat toggle — same sync-cache pattern.
_CHAT_CACHE: dict = {"enabled": True}


def brand() -> dict:
    """Return the current brand dict (sync, non-blocking). Jinja global."""
    return _BRAND_CACHE


def announcement() -> str:
    """Return the current site-wide announcement banner text (sync). Jinja global."""
    return _ANNOUNCEMENT_CACHE["text"]


def gtm_container_id() -> str:
    """Return the current GTM container ID (sync, non-blocking). Jinja global."""
    return _GTM_CACHE["container_id"]


def chat_enabled() -> bool:
    """Return whether the in-app Chat is enabled on this instance (sync). Jinja global."""
    return bool(_CHAT_CACHE.get("enabled", True))


async def refresh_brand() -> dict:
    """Reload brand settings into the module cache. Safe to call anytime."""
    from app.settings_service import get_runtime_setting

    try:
        async with app_state.db_session_factory() as db:
            name = await get_runtime_setting(db, "brand_name", default="Fluxito")
            logo_url = await get_runtime_setting(db, "brand_logo_url", default="")
            accent = await get_runtime_setting(db, "brand_accent", default="")
        _BRAND_CACHE["name"] = str(name) or "Fluxito"
        _BRAND_CACHE["logo_url"] = str(logo_url or "")
        _BRAND_CACHE["accent"] = str(accent or "")
    except Exception as e:
        logger.warning("refresh_brand failed; keeping last brand cache: %s", e)
    return _BRAND_CACHE


async def refresh_announcement() -> str:
    """Reload the announcement banner into the module cache. Safe to call anytime."""
    from app.settings_service import get_runtime_setting

    try:
        async with app_state.db_session_factory() as db:
            text = await get_runtime_setting(db, "announcement_banner", default="")
        _ANNOUNCEMENT_CACHE["text"] = str(text or "")
    except Exception as e:
        logger.warning("refresh_announcement failed; keeping last banner cache: %s", e)
    return _ANNOUNCEMENT_CACHE["text"]


async def refresh_gtm() -> str:
    """Reload GTM container ID into the module cache. Safe to call anytime."""
    from app.settings_service import get_runtime_setting

    try:
        async with app_state.db_session_factory() as db:
            cid = await get_runtime_setting(db, "gtm_container_id", default="")
        _GTM_CACHE["container_id"] = str(cid or "")
    except Exception as e:
        logger.warning("refresh_gtm failed; keeping last GTM cache: %s", e)
    return _GTM_CACHE["container_id"]


async def refresh_chat() -> bool:
    """Reload chat_enabled from settings into the module cache. Safe to call anytime."""
    from app.settings_service import get_runtime_setting

    try:
        async with app_state.db_session_factory() as db:
            enabled = await get_runtime_setting(db, "chat_enabled", default=True)
        _CHAT_CACHE["enabled"] = bool(enabled)
    except Exception as e:
        logger.warning("refresh_chat failed; keeping last cache: %s", e)
    return _CHAT_CACHE["enabled"]
