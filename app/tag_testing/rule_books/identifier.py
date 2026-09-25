"""
Rule Books — Tag Type Identifier
==================================

3-tier identification of a GTM tag's platform:

  Tier 1 — GTM native type code  (deterministic)
  Tier 2 — Custom HTML regex pattern matching
  Tier 3 — Tag name prefix heuristics (last-resort fallback)

Public API
----------
  identify_tag(tag_dict)  → IdentificationResult
  extract_event_from_html(html, platform)  → str | None
  extract_params_from_tag(tag_dict, platform)  → dict

``tag_dict`` is the raw dict returned by GTM's list_tags / get_tag_detail.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

# ---------------------------------------------------------------------------
# Tier 1 — GTM native type codes
# https://developers.google.com/tag-manager/api/v2/reference/accounts/containers/workspaces/tags
# ---------------------------------------------------------------------------

_GTM_TYPE_TO_PLATFORM: dict[str, str] = {
    # GA4 — Google Analytics: GA4 Configuration / Event tag types
    "gaawc": "ga4",  # GA4 Configuration tag
    "gaawe": "ga4",  # GA4 Event tag
    "googtag": "ga4",  # Google tag (newer unified tag)
    # Google Ads
    "awct": "google_ads_conversion",  # Google Ads Conversion Tracking
    "awrd": "google_ads_remarketing",  # Google Ads Remarketing
    "gclidw": "google_ads_conversion",  # Enhanced conversions (web)
    # Floodlight
    "fls": "floodlight",  # Floodlight Activity
    "flsactivity": "floodlight",  # Floodlight Activity (alternate)
    "flsa": "floodlight",  # Floodlight Activity (CM360)
    # Other native types
    "ua": "google_analytics_ua",  # Universal Analytics (legacy)
    "html": None,  # Custom HTML — needs tier-2 detection
    "img": None,  # Custom image — needs tier-2 detection
}

# ---------------------------------------------------------------------------
# Tier 2 — Custom HTML / JS regex patterns
# Ordered by specificity (more specific patterns first).
# ---------------------------------------------------------------------------

_HTML_DETECTION_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Meta Pixel
    (re.compile(r"connect\.facebook\.net|fbevents\.js|fbq\s*\(", re.I), "facebook_pixel"),
    # TikTok Pixel
    (
        re.compile(r"analytics\.tiktok\.com|ttq\.track\(|ttq\.load\(|TiktokAnalyticsObject", re.I),
        "tiktok_pixel",
    ),
    # Snap Pixel
    (re.compile(r"sc-static\.net/scevent|tr\.snapchat\.com|snaptr\s*\(", re.I), "snap_pixel"),
    # Pinterest Tag
    (re.compile(r"pintrk\s*\(|ct\.pinterest\.com|s\.pinimg\.com/ct", re.I), "pinterest_tag"),
    # Twitter/X Pixel
    (re.compile(r"static\.ads-twitter\.com|twq\s*\(|twitter.*pixel", re.I), "twitter_pixel"),
    # LinkedIn Insight
    (
        re.compile(r"snap\.licdn\.com|_linkedin_partner_id|lintrk\s*\(|linkedin.*insight", re.I),
        "linkedin_insight",
    ),
    # Microsoft UET
    (re.compile(r"bat\.bing\.com/action|uetq\b|bat\.bing\.com/bat", re.I), "microsoft_uet"),
    # Criteo
    (re.compile(r"static\.criteo\.net|Criteo\.events\.|criteo_q\b", re.I), "criteo"),
    # Hotjar
    (re.compile(r"static\.hotjar\.com|hj\s*\(|_hjSettings\b", re.I), "hotjar"),
    # Segment
    (re.compile(r"cdn\.segment\.com|analytics\.track\s*\(|window\.analytics\b", re.I), "segment"),
    # Mixpanel
    (re.compile(r"cdn\.mxpnl\.com|mixpanel\.track\s*\(|mixpanel\.init\s*\(", re.I), "mixpanel"),
    # PostHog
    (
        re.compile(
            r"posthog\.init|posthog\.capture|posthog\.identify|app\.posthog\.com|t\.posthog\.com", re.I
        ),
        "posthog",
    ),
    # Amplitude
    (re.compile(r"cdn\.amplitude\.com|amplitude\.getInstance\s*\(|amplitudeJS", re.I), "amplitude"),
    # Adobe Analytics — check before GA4 to avoid false positives on "analytics"
    (re.compile(r"adobe.*analytics|AppMeasurement|s\.t\s*\(|sc\.omtrdc\.net", re.I), "adobe_analytics"),
    # GA4 event via gtag (Custom HTML fallback)
    (re.compile(r"gtag\s*\(\s*['\"]event['\"]", re.I), "ga4"),
    # Google Ads conversion via gtag
    (re.compile(r"gtag\s*\(\s*['\"]event['\"].*AW-", re.I), "google_ads_conversion"),
    # Floodlight via gtag
    (re.compile(r"gtag\s*\(\s*['\"]event['\"].*DC-", re.I), "floodlight"),
]

# ---------------------------------------------------------------------------
# Tier 3 — Tag name prefix heuristics (case-insensitive)
# ---------------------------------------------------------------------------

_NAME_PREFIX_TO_PLATFORM: list[tuple[str, str]] = [
    # Meta
    ("meta ", "facebook_pixel"),
    ("meta-", "facebook_pixel"),
    ("fb ", "facebook_pixel"),
    ("fb-", "facebook_pixel"),
    ("facebook ", "facebook_pixel"),
    # TikTok
    ("tiktok ", "tiktok_pixel"),
    ("ttk ", "tiktok_pixel"),
    # Snap
    ("snap ", "snap_pixel"),
    ("snapchat ", "snap_pixel"),
    # Pinterest
    ("pinterest ", "pinterest_tag"),
    ("pintrk ", "pinterest_tag"),
    # Twitter / X
    ("twitter ", "twitter_pixel"),
    ("x ", "twitter_pixel"),
    ("twq ", "twitter_pixel"),
    # LinkedIn
    ("linkedin ", "linkedin_insight"),
    ("li ", "linkedin_insight"),
    # Microsoft / Bing
    ("bing ", "microsoft_uet"),
    ("microsoft ", "microsoft_uet"),
    ("uet ", "microsoft_uet"),
    # Criteo
    ("criteo ", "criteo"),
    # Hotjar
    ("hotjar ", "hotjar"),
    ("hj ", "hotjar"),
    # Segment
    ("segment ", "segment"),
    # Mixpanel
    ("mixpanel ", "mixpanel"),
    # PostHog
    ("posthog ", "posthog"),
    # Amplitude
    ("amplitude ", "amplitude"),
    # Adobe
    ("adobe ", "adobe_analytics"),
    ("aaa ", "adobe_analytics"),
    # GA4
    ("ga4 ", "ga4"),
    ("ga ", "ga4"),
    # Google Ads
    ("google ads ", "google_ads_conversion"),
    ("gads ", "google_ads_conversion"),
    # Floodlight
    ("floodlight ", "floodlight"),
    ("fls ", "floodlight"),
]

# ---------------------------------------------------------------------------
# Event extraction patterns (per platform)
# ---------------------------------------------------------------------------

# Patterns to extract event name from Custom HTML source
_EVENT_EXTRACTION: dict[str, list[re.Pattern]] = {
    "ga4": [
        re.compile(r"""gtag\s*\(\s*['"]event['"]\s*,\s*['"]([^'"]+)['"]""", re.I),
        re.compile(r"""event_name\s*[=:]\s*['"]([^'"]+)['"]""", re.I),
    ],
    "facebook_pixel": [
        re.compile(r"""fbq\s*\(\s*['"]track['"]\s*,\s*['"]([^'"]+)['"]""", re.I),
        re.compile(r"""fbq\s*\(\s*['"]trackCustom['"]\s*,\s*['"]([^'"]+)['"]""", re.I),
    ],
    "tiktok_pixel": [
        re.compile(r"""ttq\.track\s*\(\s*['"]([^'"]+)['"]""", re.I),
        re.compile(r"""ttq\.page\s*\(""", re.I),  # special "Page" event
    ],
    "snap_pixel": [
        re.compile(r"""snaptr\s*\(\s*['"]track['"]\s*,\s*['"]([^'"]+)['"]""", re.I),
    ],
    "pinterest_tag": [
        re.compile(r"""pintrk\s*\(\s*['"]track['"]\s*,\s*['"]([^'"]+)['"]""", re.I),
    ],
    "twitter_pixel": [
        re.compile(r"""twq\s*\(\s*['"]event['"]\s*,\s*['"]([^'"]+)['"]""", re.I),
        re.compile(r"""twq\s*\(\s*['"]track['"]\s*,\s*['"]([^'"]+)['"]""", re.I),
    ],
}

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class IdentificationResult:
    matched_platform: str | None
    confidence: Literal["high", "medium", "low", "none"]
    match_tier: Literal["type_code", "html_pattern", "name_hint", "none"]
    match_reason: str
    event_name: str | None = None  # extracted from tag HTML if determinable
    gtm_tag_id: str | None = None
    gtm_tag_name: str | None = None

    def as_dict(self) -> dict:
        return {
            "matched_platform": self.matched_platform,
            "confidence": self.confidence,
            "match_tier": self.match_tier,
            "match_reason": self.match_reason,
            "event_name": self.event_name,
            "gtm_tag_id": self.gtm_tag_id,
            "gtm_tag_name": self.gtm_tag_name,
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def identify_tag(tag_dict: dict) -> IdentificationResult:
    """
    Identify which tracking platform a GTM tag belongs to.

    ``tag_dict`` is a raw GTM API tag object (or any dict with ``type``,
    ``name``, and optional ``templateUserCode`` / ``html`` fields).

    Returns an IdentificationResult with the matched platform slug (or None
    if no match was found), confidence level, and match reason.
    """
    tag_id: str | None = tag_dict.get("tagId") or tag_dict.get("tag_id")
    tag_name: str = (tag_dict.get("name") or "").strip()
    tag_type: str = (tag_dict.get("type") or "").strip().lower()

    # Tier 1 — GTM native type code
    if tag_type and tag_type in _GTM_TYPE_TO_PLATFORM:
        platform = _GTM_TYPE_TO_PLATFORM[tag_type]
        if platform is not None:
            return IdentificationResult(
                matched_platform=platform,
                confidence="high",
                match_tier="type_code",
                match_reason=f"GTM native type code '{tag_type}'",
                gtm_tag_id=tag_id,
                gtm_tag_name=tag_name,
            )
        # tag_type == "html" or "img" → fall through to tier 2

    # Extract Custom HTML source for tier 2
    html_source: str = _extract_html_source(tag_dict)

    # Tier 2 — Custom HTML regex patterns
    if html_source:
        for pattern, platform in _HTML_DETECTION_PATTERNS:
            if pattern.search(html_source):
                event_name = extract_event_from_html(html_source, platform)
                return IdentificationResult(
                    matched_platform=platform,
                    confidence="medium",
                    match_tier="html_pattern",
                    match_reason=f"HTML pattern match: {pattern.pattern[:60]}",
                    event_name=event_name,
                    gtm_tag_id=tag_id,
                    gtm_tag_name=tag_name,
                )

    # Tier 3 — Tag name prefix heuristics
    if tag_name:
        lower_name = tag_name.lower()
        for prefix, platform in _NAME_PREFIX_TO_PLATFORM:
            if lower_name.startswith(prefix):
                return IdentificationResult(
                    matched_platform=platform,
                    confidence="low",
                    match_tier="name_hint",
                    match_reason=f"Tag name starts with '{prefix.strip()}'",
                    gtm_tag_id=tag_id,
                    gtm_tag_name=tag_name,
                )

    return IdentificationResult(
        matched_platform=None,
        confidence="none",
        match_tier="none",
        match_reason="No identification match found (unknown or unsupported platform)",
        gtm_tag_id=tag_id,
        gtm_tag_name=tag_name,
    )


def extract_event_from_html(html: str, platform: str) -> str | None:
    """
    Given Custom HTML source and an already-identified platform slug,
    attempt to extract the event name that will be fired.

    Returns the event name string or None if it cannot be determined
    (e.g. the event name is dynamic / data-layer-driven).
    """
    patterns = _EVENT_EXTRACTION.get(platform, [])
    for pattern in patterns:
        m = pattern.search(html)
        if m:
            try:
                return m.group(1).strip()
            except IndexError:
                # Pattern matched but no capture group (e.g. ttq.page())
                return "page"
    return None


def extract_params_from_tag(tag_dict: dict, platform: str) -> dict:
    """
    Extract a best-effort parameter dict from a GTM tag's ``parameter``
    list (for native tags) or from Custom HTML source (limited).

    For native GA4 / Google Ads tags this gives structured key→value pairs.
    For Custom HTML tags this is limited — only literal string params
    embedded in the JS source can be extracted.

    Returns a flat dict suitable for passing to validator.validate_payload().
    """
    params: dict = {}

    # --- Native tag parameters (most reliable) ---------------------------
    raw_params = tag_dict.get("parameter") or []
    for p in raw_params:
        key = p.get("key") or ""
        value = p.get("value")
        ptype = (p.get("type") or "").lower()
        if not key:
            continue
        if ptype == "list":
            # Nested list parameters (e.g. GA4 event_settings, items)
            items = p.get("list") or []
            parsed_list = []
            for item in items:
                sub = {sp.get("key"): sp.get("value") for sp in (item.get("map") or []) if sp.get("key")}
                if sub:
                    parsed_list.append(sub)
            params[key] = parsed_list if parsed_list else value
        elif ptype == "map":
            sub = {mp.get("key"): mp.get("value") for mp in (p.get("map") or []) if mp.get("key")}
            params[key] = sub or value
        elif ptype == "template":
            # {{datalayer_var}} — mark as dynamic so validator knows it was set
            params[key] = value  # e.g. "{{DL - ecommerce.value}}"
        else:
            params[key] = value

    # --- Custom HTML tag — limited literal extraction ---------------------
    html_source = _extract_html_source(tag_dict)
    if html_source and not raw_params:
        # Only try if there are no native params (avoid double-counting)
        for kv_pattern in (
            re.compile(r"""['"]([\w.]+)['"]\s*:\s*['"]([^'"]{0,200})['"]"""),
            re.compile(r"""(\w+)\s*=\s*['"]([^'"]{0,200})['"]"""),
        ):
            for m in kv_pattern.finditer(html_source):
                k, v = m.group(1), m.group(2)
                if k and k not in params:
                    params[k] = v

    return params


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_html_source(tag_dict: dict) -> str:
    """
    Pull the Custom HTML source string from a GTM tag dict.
    GTM stores this in ``parameter`` list under key ``"html"``.
    """
    # Direct field (some serialization formats)
    direct = tag_dict.get("html") or ""
    if direct:
        return str(direct)

    # Standard GTM API parameter list
    for p in tag_dict.get("parameter") or []:
        if (p.get("key") or "").lower() == "html":
            return str(p.get("value") or "")

    # templateUserCode (Custom HTML custom template)
    return str(tag_dict.get("templateUserCode") or "")
