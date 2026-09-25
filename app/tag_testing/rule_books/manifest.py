"""
Rule Book — Manifest
=====================

Single source of truth for all registered platform Rule Books.

``RULE_BOOK_MANIFEST``  — ordered list of all RuleBook instances.
``PLATFORM_INDEX``      — dict keyed by platform slug for O(1) lookup.

To add a new platform:
  1. Create app/tag_testing/rule_books/platforms/<slug>.py
  2. Import it here and add to RULE_BOOK_MANIFEST.
"""

from __future__ import annotations

from app.tag_testing.rule_books.base import RuleBook
from app.tag_testing.rule_books.platforms.adobe_analytics import RULE_BOOK as _ADOBE
from app.tag_testing.rule_books.platforms.amplitude import RULE_BOOK as _AMPLITUDE
from app.tag_testing.rule_books.platforms.criteo import RULE_BOOK as _CRITEO
from app.tag_testing.rule_books.platforms.floodlight import RULE_BOOK as _FLOODLIGHT
from app.tag_testing.rule_books.platforms.ga4_config import RULE_BOOK as _GA4_CONFIG

# ── Platform imports ──────────────────────────────────────────────────────────
from app.tag_testing.rule_books.platforms.ga4_ecom import RULE_BOOK as _GA4_ECOM
from app.tag_testing.rule_books.platforms.ga4_standard import RULE_BOOK as _GA4_STANDARD
from app.tag_testing.rule_books.platforms.google_ads_conversion import RULE_BOOK as _GADS_CONV
from app.tag_testing.rule_books.platforms.google_ads_remarketing import RULE_BOOK as _GADS_REMARK
from app.tag_testing.rule_books.platforms.gtm_spec import RULE_BOOK as _GTM_SPEC
from app.tag_testing.rule_books.platforms.hotjar import RULE_BOOK as _HOTJAR
from app.tag_testing.rule_books.platforms.linkedin_insight import RULE_BOOK as _LINKEDIN
from app.tag_testing.rule_books.platforms.meta_pixel import RULE_BOOK as _META_PIXEL
from app.tag_testing.rule_books.platforms.microsoft_uet import RULE_BOOK as _MICROSOFT_UET
from app.tag_testing.rule_books.platforms.mixpanel import RULE_BOOK as _MIXPANEL
from app.tag_testing.rule_books.platforms.pinterest_tag import RULE_BOOK as _PINTEREST
from app.tag_testing.rule_books.platforms.posthog import RULE_BOOK as _POSTHOG
from app.tag_testing.rule_books.platforms.segment import RULE_BOOK as _SEGMENT
from app.tag_testing.rule_books.platforms.snap_pixel import RULE_BOOK as _SNAP
from app.tag_testing.rule_books.platforms.tiktok_pixel import RULE_BOOK as _TIKTOK_PIXEL
from app.tag_testing.rule_books.platforms.twitter_pixel import RULE_BOOK as _TWITTER

# ── Manifest — ordered by usage frequency (most common platforms first) ───────
RULE_BOOK_MANIFEST: list[RuleBook] = [
    _GA4_ECOM,
    _GA4_STANDARD,
    _GA4_CONFIG,
    _META_PIXEL,
    _GADS_CONV,
    _GADS_REMARK,
    _GTM_SPEC,
    _TIKTOK_PIXEL,
    _FLOODLIGHT,
    _LINKEDIN,
    _SNAP,
    _PINTEREST,
    _TWITTER,
    _MICROSOFT_UET,
    _CRITEO,
    _ADOBE,
    _AMPLITUDE,
    _SEGMENT,
    _MIXPANEL,
    _POSTHOG,
    _HOTJAR,
]

# ── Platform index for O(1) lookup ────────────────────────────────────────────
PLATFORM_INDEX: dict[str, RuleBook] = {rb.platform: rb for rb in RULE_BOOK_MANIFEST}


def get_rule_book(platform: str) -> RuleBook | None:
    """Return the RuleBook for the given platform slug, or None if not found."""
    return PLATFORM_INDEX.get(platform)


def list_platforms_summary() -> list[dict]:
    """Return a lightweight list of all platforms for tool list responses."""
    return [
        {
            "platform": rb.platform,
            "display_name": rb.display_name,
            "spec_version": rb.spec_version,
            "docs_url": rb.docs_url,
            "event_count": len(rb.events),
            "global_rule_count": len(rb.global_rules),
            "gtm_type_codes": list(rb.gtm_type_codes),
        }
        for rb in RULE_BOOK_MANIFEST
    ]
