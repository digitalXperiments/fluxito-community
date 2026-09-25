"""
Unit tests for the Tag Rule Book identifier.

Tests platform detection via:
  - GTM type codes (tier 1)
  - Regex patterns in Custom HTML tag code (tier 2)
  - Name prefix heuristics (tier 3)
"""

from app.tag_testing.rule_books.identifier import identify_tag
from app.tag_testing.rule_books.manifest import PLATFORM_INDEX

# ── GTM type code (tier 1) ───────────────────────────────────────────────────


def test_identify_ga4_by_gtm_type_code():
    result = identify_tag({"type": "gaawc"})
    assert result is not None
    assert result.matched_platform == "ga4"
    assert result.confidence == "high"
    assert result.match_tier == "type_code"


def test_identify_meta_pixel_by_type_code():
    result = identify_tag(
        {"type": "html", "parameter": [{"key": "html", "value": "fbq('init', '1234567890')"}]}
    )
    assert result is not None
    assert result.matched_platform == "facebook_pixel"


def test_identify_google_ads_conversion_by_type_code():
    result = identify_tag({"type": "awct"})
    assert result is not None
    assert result.matched_platform == "google_ads_conversion"
    assert result.confidence == "high"


def test_identify_floodlight_by_type_code():
    result = identify_tag({"type": "fls"})
    assert result is not None
    assert result.matched_platform == "floodlight"


# ── Custom HTML regex (tier 2) ────────────────────────────────────────────────


def test_identify_tiktok_pixel_by_regex():
    html = "!function(w,d,t){w.TiktokAnalyticsObject=t;"
    result = identify_tag({"type": "html", "parameter": [{"key": "html", "value": html}]})
    assert result is not None
    assert result.matched_platform == "tiktok_pixel"
    assert result.confidence in ("medium", "high")


def test_identify_snap_pixel_by_regex():
    html = "snaptr('init', '12345678-1234-1234-1234-123456789abc');"
    result = identify_tag({"type": "html", "parameter": [{"key": "html", "value": html}]})
    assert result is not None
    assert result.matched_platform == "snap_pixel"


def test_identify_pinterest_by_regex():
    html = "pintrk('load', '12345678901234567');"
    result = identify_tag({"type": "html", "parameter": [{"key": "html", "value": html}]})
    assert result is not None
    assert result.matched_platform == "pinterest_tag"


def test_identify_amplitude_by_regex():
    html = "amplitude.getInstance().init('YOUR_API_KEY');"
    result = identify_tag({"type": "html", "parameter": [{"key": "html", "value": html}]})
    assert result is not None
    assert result.matched_platform == "amplitude"


def test_identify_mixpanel_by_regex():
    html = "mixpanel.init('abc123'); mixpanel.track ('Purchase', {});"  # uses mixpanel.track \s*\(
    result = identify_tag({"type": "html", "parameter": [{"key": "html", "value": html}]})
    assert result is not None
    assert result.matched_platform == "mixpanel"


def test_identify_posthog_by_regex():
    html = "posthog.init('phc_abc123', {api_host:'https://app.posthog.com'})"
    result = identify_tag({"type": "html", "parameter": [{"key": "html", "value": html}]})
    assert result is not None
    assert result.matched_platform == "posthog"


# ── Name prefix heuristic (tier 3) ───────────────────────────────────────────


def test_identify_by_name_prefix_ga4():
    result = identify_tag({"type": "html", "name": "GA4 - Purchase Event"})
    assert result is not None
    assert result.matched_platform == "ga4"
    assert result.confidence == "low"


def test_identify_by_name_prefix_meta():
    result = identify_tag({"type": "html", "name": "FB Pixel - Add to Cart"})
    assert result is not None
    assert result.matched_platform == "facebook_pixel"


def test_identify_by_name_prefix_tiktok():
    result = identify_tag({"type": "html", "name": "TikTok - View Content"})
    assert result is not None
    assert result.matched_platform == "tiktok_pixel"


def test_identify_by_name_prefix_snap():
    result = identify_tag({"type": "html", "name": "Snap Pixel - Purchase"})
    assert result is not None
    assert result.matched_platform == "snap_pixel"


# ── Unknown tags ──────────────────────────────────────────────────────────────


def test_identify_unknown_returns_none():
    result = identify_tag({"type": "html", "name": "Some Generic Tag"})
    assert result.matched_platform is None


def test_identify_empty_tag_returns_none():
    result = identify_tag({})
    assert result is None or (result.matched_platform is None)


# ── All platforms have rule books ─────────────────────────────────────────────


def test_all_platforms_have_non_empty_events():
    """Every Rule Book in the manifest must have at least one event or global rule."""
    from app.tag_testing.rule_books.manifest import RULE_BOOK_MANIFEST

    for rb in RULE_BOOK_MANIFEST:
        assert (
            len(rb.events) + len(rb.global_rules) > 0
        ), f"Platform '{rb.platform}' has neither events nor global rules"


def test_platform_index_covers_all_manifest_entries():
    from app.tag_testing.rule_books.manifest import RULE_BOOK_MANIFEST

    for rb in RULE_BOOK_MANIFEST:
        assert rb.platform in PLATFORM_INDEX, f"'{rb.platform}' missing from PLATFORM_INDEX"
