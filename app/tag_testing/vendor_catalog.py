# app/tag_testing/vendor_catalog.py
"""Vendor catalog for audit vendors and source platforms.

TP_VENDOR_CATALOG is built by unioning three read-only registries:
  1. app/connectors/rate_limits.py  — CATALOG (Connector dataclasses)
  2. app/api/google_oauth_routes.py — GRANULAR_CONNECTOR_CATALOG (tuples)
  3. app/tag_testing/rule_books/manifest.py — RULE_BOOK_MANIFEST / list_platforms_summary()

De-duplication is on slug.  Audit platform slugs are normalised to their
connector equivalents before de-duplication so the same platform is never
counted twice.  A small curated tail adds common CDP/marketing destinations
present in none of the three registries.

``source`` on each entry indicates which registry first contributed it:
  'connector'  — came from the rate-limits CATALOG or GRANULAR_CONNECTOR_CATALOG
  'audit'      — came from the audit rule-book manifest (after slug normalisation)
  'curated'    — hand-added destinations not in any registry

``get_vendor_catalog()`` returns the full catalog split into:
  destinations  — list of vendor dicts
  source_platforms — list of source-kind dicts (web/ios/android/server/…)
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Audit-slug → canonical connector slug normalisation map
# ---------------------------------------------------------------------------
# Audit rule-book slugs that differ from the canonical connector slugs used in
# rate_limits.CATALOG and GRANULAR_CONNECTOR_CATALOG.  Any audit slug NOT in
# this map is kept as-is if it is unique (e.g. mixpanel, hotjar, segment …).
_AUDIT_SLUG_NORM: dict[str, str] = {
    "facebook_pixel": "meta",
    # All GA4 rule-book variants collapse into the single GA4 connector slug.
    "ga4_ecom": "ga4",
    "ga4_standard": "ga4",
    "ga4_config": "ga4",
    # Google Ads variants
    "google_ads_conversion": "google_ads",
    "google_ads_remarketing": "google_ads",
    # Social pixel variants
    "snap_pixel": "snap",
    "tiktok_pixel": "tiktok",
    "twitter_pixel": "x",
    "pinterest_tag": "pinterest",
    # LinkedIn
    "linkedin_insight": "linkedin",
    # Bing
    "microsoft_uet": "bing",
    # GTM spec rule book → GTM connector
    "gtm_spec": "gtm",
    # Floodlight is a Google/Campaign Manager tag — map to its own slug
    # (no connector slug exists for it; keep it as an audit-only entry).
}

# Category overrides for audit platforms that are not in the rate-limits
# connector catalog (which carries authoritative category strings).
_AUDIT_CATEGORY_OVERRIDES: dict[str, str] = {
    "segment": "Analytics",
    "mixpanel": "Analytics",
    "amplitude": "Analytics",
    "hotjar": "Analytics",
    "adobe_analytics": "Analytics",
    "criteo": "Advertising",
    "floodlight": "Advertising",
}

# ---------------------------------------------------------------------------
# Curated destinations not in any of the three registries
# ---------------------------------------------------------------------------
_CURATED: list[dict] = [
    {"slug": "moengage", "display_name": "MoEngage", "category": "Marketing automation"},
    {"slug": "braze", "display_name": "Braze", "category": "Marketing automation"},
    {"slug": "klaviyo", "display_name": "Klaviyo", "category": "Marketing automation"},
    {"slug": "customerio", "display_name": "Customer.io", "category": "Marketing automation"},
    {"slug": "rudderstack", "display_name": "RudderStack", "category": "Analytics"},
    {"slug": "posthog", "display_name": "PostHog", "category": "Analytics"},
    {"slug": "heap", "display_name": "Heap", "category": "Analytics"},
    {"slug": "iterable", "display_name": "Iterable", "category": "Marketing automation"},
    {"slug": "onesignal", "display_name": "OneSignal", "category": "Marketing automation"},
]

# ---------------------------------------------------------------------------
# Source platform kinds (for the source axis)
# ---------------------------------------------------------------------------
_SOURCE_PLATFORMS: list[dict] = [
    {"slug": "web", "display_name": "Web (Browser)"},
    {"slug": "ios", "display_name": "iOS"},
    {"slug": "android", "display_name": "Android"},
    {"slug": "server", "display_name": "Server-side"},
    {"slug": "warehouse", "display_name": "Data Warehouse"},
    {"slug": "gtm", "display_name": "Google Tag Manager"},
    {"slug": "custom", "display_name": "Custom / Other"},
]


def _build_catalog() -> list[dict]:
    """Build and return the de-duplicated vendor catalog list."""

    seen: dict[str, dict] = {}  # slug → entry (first-win de-dup)

    # ------------------------------------------------------------------
    # 1. GRANULAR_CONNECTOR_CATALOG (from google_oauth_routes) — connectors
    #    the platform knows about for OAuth / credential purposes.
    #    Shape: list[tuple[str, str, tuple[str, ...]]]  (key, label, flags)
    # ------------------------------------------------------------------
    try:
        from app.api.google_oauth_routes import GRANULAR_CONNECTOR_CATALOG

        for key, label, _flags in GRANULAR_CONNECTOR_CATALOG:
            if key not in seen:
                seen[key] = {
                    "slug": key,
                    "display_name": label,
                    "category": "",  # patched below from rate_limits CATALOG
                    "source": "connector",
                }
    except Exception:
        logger.warning("vendors: could not load GRANULAR_CONNECTOR_CATALOG", exc_info=True)

    # ------------------------------------------------------------------
    # 2. rate_limits.CATALOG — Connector dataclasses with authoritative
    #    category strings.  Patch categories onto entries already seeded
    #    from GRANULAR_CONNECTOR_CATALOG; add any net-new entries too.
    # ------------------------------------------------------------------
    try:
        from app.connectors.rate_limits import CATALOG as RL_CATALOG

        for conn in RL_CATALOG:
            if conn.key in seen:
                # Patch category (rate_limits has the canonical string)
                seen[conn.key]["category"] = conn.category
                # Also prefer the rate_limits display name (more descriptive)
                seen[conn.key]["display_name"] = conn.name
            else:
                seen[conn.key] = {
                    "slug": conn.key,
                    "display_name": conn.name,
                    "category": conn.category,
                    "source": "connector",
                }
    except Exception:
        logger.warning("vendors: could not load rate_limits CATALOG", exc_info=True)

    # ------------------------------------------------------------------
    # 3. Audit rule-book manifest — platforms the tag-testing engine knows.
    #    Shape from list_platforms_summary(): list[dict] with 'platform'
    #    and 'display_name' keys.
    # ------------------------------------------------------------------
    try:
        from app.tag_testing.rule_books.manifest import list_platforms_summary

        for platform_info in list_platforms_summary():
            raw_slug: str = platform_info["platform"]
            canonical_slug: str = _AUDIT_SLUG_NORM.get(raw_slug, raw_slug)

            if canonical_slug in seen:
                # Already present from a connector registry — skip (connector wins)
                continue

            category = _AUDIT_CATEGORY_OVERRIDES.get(canonical_slug, "Analytics")
            seen[canonical_slug] = {
                "slug": canonical_slug,
                "display_name": platform_info["display_name"],
                "category": category,
                "source": "audit",
            }
    except Exception:
        logger.warning("vendors: could not load rule_books manifest", exc_info=True)

    # ------------------------------------------------------------------
    # 4. Curated tail — common destinations in none of the three registries.
    # ------------------------------------------------------------------
    for entry in _CURATED:
        slug = entry["slug"]
        if slug not in seen:
            seen[slug] = {
                "slug": slug,
                "display_name": entry["display_name"],
                "category": entry["category"],
                "source": "curated",
            }

    # Return catalog preserving insertion order (connector → audit → curated)
    return list(seen.values())


# Module-level cache — built once on first import.
TP_VENDOR_CATALOG: list[dict] = _build_catalog()

# Slug set for O(1) membership tests (used by routing.py)
_CATALOG_SLUGS: frozenset[str] = frozenset(e["slug"] for e in TP_VENDOR_CATALOG)


def get_vendor_catalog() -> dict:
    """Return the full vendor catalog as a plain JSON-serialisable dict.

    Shape::

        {
            "destinations": [
                {"slug": "ga4", "display_name": "Google Analytics 4",
                 "category": "Analytics", "source": "connector"},
                ...
            ],
            "source_platforms": [
                {"slug": "web", "display_name": "Web (Browser)"},
                ...
            ],
        }
    """
    return {
        "destinations": TP_VENDOR_CATALOG,
        "source_platforms": _SOURCE_PLATFORMS,
    }


def is_known_slug(slug: str) -> bool:
    """Return True if *slug* is in the catalog (connector, audit, or curated)."""
    return slug in _CATALOG_SLUGS
