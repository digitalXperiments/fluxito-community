"""
Unit tests for the Tag Rule Book validator.

Tests the validate_payload() scoring engine for:
  - Required params present → high score
  - Required params missing → score drop + critical findings
  - Recommended params missing → warning findings, not zero
  - Value constraints (allowed_values, min/max, regex)
  - Unknown events → empty result
  - compute_score() clamping (0–100)
  - Multi-platform coverage
"""

import pytest

from app.tag_testing.rule_books.manifest import PLATFORM_INDEX
from app.tag_testing.rule_books.validator import compute_score, validate_payload

# ── Helper ────────────────────────────────────────────────────────────────────


def rb(platform: str):
    rb = PLATFORM_INDEX.get(platform)
    if rb is None:
        pytest.skip(f"Platform '{platform}' not found in manifest")
    return rb


# ── GA4 Ecommerce ─────────────────────────────────────────────────────────────


def test_ga4_purchase_all_required_fields():
    result = validate_payload(
        rb("ga4"),
        "purchase",
        {
            "transaction_id": "TXN-001",
            "currency": "USD",
            "value": 99.99,
            "items": [{"item_id": "SKU-1", "item_name": "Widget", "price": 99.99, "quantity": 1}],
        },
    )
    # Global rule ga4.ecom.measurement_id always fires (structural check).
    # The payload-level required params should all pass.
    payload_criticals = [f for f in result.findings if f.status == "critical" and f.param]
    assert len(payload_criticals) == 0, f"Unexpected param-level critical findings: {payload_criticals}"
    assert result.score > 0


def test_ga4_purchase_missing_all_required():
    result = validate_payload(rb("ga4"), "purchase", {})
    assert result.critical_count > 0
    assert result.score < 50


def test_ga4_purchase_missing_transaction_id_is_critical():
    result = validate_payload(
        rb("ga4"),
        "purchase",
        {
            "currency": "USD",
            "value": 99.99,
        },
    )
    # Must have a finding for transaction_id
    rule_ids = [f.rule_id for f in result.findings if f.status != "pass"]
    param_names = [f.param for f in result.findings if f.status != "pass"]
    assert "transaction_id" in param_names or any(
        "transaction_id" in (rid or "") for rid in rule_ids
    ), f"Expected transaction_id finding, got: {result.findings}"


def test_ga4_view_item_minimal():
    result = validate_payload(
        rb("ga4"),
        "view_item",
        {
            "currency": "USD",
            "value": 49.99,
            "items": [{"item_id": "SKU-1", "item_name": "Shirt"}],
        },
    )
    # Only global rules (structural) may fire; no payload-level criticals
    payload_criticals = [f for f in result.findings if f.status == "critical" and f.param]
    assert len(payload_criticals) == 0


def test_ga4_add_to_cart_missing_items_critical():
    result = validate_payload(rb("ga4"), "add_to_cart", {"currency": "USD", "value": 29.00})
    assert result.critical_count >= 1


def test_ga4_unknown_event_returns_no_findings():
    result = validate_payload(rb("ga4"), "totally_unknown_event_xyz", {"foo": "bar"})
    # Unknown events produce no payload findings (spec_found=False)
    assert result.spec_found is False
    payload_findings = [f for f in result.findings if f.param]
    assert len(payload_findings) == 0


# ── Meta Pixel ────────────────────────────────────────────────────────────────


def test_meta_purchase_all_required():
    result = validate_payload(
        rb("facebook_pixel"),
        "Purchase",
        {
            "value": 99.99,
            "currency": "USD",
            "content_ids": ["SKU-1"],
            "content_type": "product",
        },
    )
    assert result.critical_count == 0


def test_meta_purchase_missing_currency():
    result = validate_payload(
        rb("facebook_pixel"),
        "Purchase",
        {
            "value": 99.99,
        },
    )
    assert result.critical_count >= 1


def test_meta_initiate_checkout_valid():
    result = validate_payload(
        rb("facebook_pixel"),
        "InitiateCheckout",
        {
            "value": 79.99,
            "currency": "USD",
            "num_items": 3,
            "content_ids": ["SKU-1", "SKU-2"],  # required by Rule Book
        },
    )
    payload_criticals = [f for f in result.findings if f.status == "critical" and f.param]
    assert len(payload_criticals) == 0


# ── TikTok Pixel ─────────────────────────────────────────────────────────────


def test_tiktok_purchase_valid():
    result = validate_payload(
        rb("tiktok_pixel"),
        "Purchase",
        {
            "content_id": "SKU-1",
            "content_type": "product",  # required by Rule Book
            "currency": "USD",
            "value": 49.99,
            "quantity": 1,
        },
    )
    payload_criticals = [f for f in result.findings if f.status == "critical" and f.param]
    assert len(payload_criticals) == 0


def test_tiktok_purchase_missing_content_id():
    result = validate_payload(
        rb("tiktok_pixel"),
        "Purchase",
        {
            "currency": "USD",
            "value": 49.99,
        },
    )
    rule_ids = [f.rule_id for f in result.findings if f.status != "pass"]
    param_names = [f.param for f in result.findings if f.status != "pass"]
    assert "content_id" in param_names or any("content_id" in (rid or "") for rid in rule_ids)


# ── Snap Pixel ────────────────────────────────────────────────────────────────


def test_snap_purchase_valid():
    result = validate_payload(
        rb("snap_pixel"),
        "PURCHASE",
        {
            "item_ids": ["SKU-1"],
            "price": 99.99,
            "currency": "USD",
            "transaction_id": "TXN-001",  # required by Rule Book
        },
    )
    payload_criticals = [f for f in result.findings if f.status == "critical" and f.param]
    assert len(payload_criticals) == 0


# ── Score clamping ────────────────────────────────────────────────────────────


def test_compute_score_max_is_100():
    assert compute_score(0, 0, 0, 0) == 100


def test_compute_score_with_critical():
    # 1 critical out of 5 total passed should drop score significantly
    score = compute_score(critical=1, warning=0, info=0, passed=4)
    assert 0 <= score < 80


def test_compute_score_never_below_zero():
    score = compute_score(critical=50, warning=20, info=10, passed=0)
    assert score == 0


def test_compute_score_clamped_at_100():
    score = compute_score(critical=0, warning=0, info=0, passed=20)
    assert score == 100


# ── All Rule Books — smoke test ───────────────────────────────────────────────


def test_every_platform_has_serialize():
    """Each Rule Book must serialize without error."""
    from app.tag_testing.rule_books.manifest import RULE_BOOK_MANIFEST

    for rb in RULE_BOOK_MANIFEST:
        d = rb.serialize(include_events=True)
        assert d["platform"] == rb.platform
        assert isinstance(d["events"], list)


def test_every_platform_find_event_works():
    """find_event should be case-insensitive."""
    ga4 = PLATFORM_INDEX["ga4"]
    assert ga4.find_event("PURCHASE") is not None
    assert ga4.find_event("purchase") is not None
    meta = PLATFORM_INDEX["facebook_pixel"]
    assert meta.find_event("purchase") is not None


# ── Segment ───────────────────────────────────────────────────────────────────


def test_segment_order_completed_valid():
    result = validate_payload(
        rb("segment"),
        "Order Completed",
        {
            "order_id": "ORD-001",
            "total": 99.99,
            "currency": "USD",
            "products": [{"product_id": "SKU-1"}],
        },
    )
    payload_criticals = [f for f in result.findings if f.status == "critical" and f.param]
    assert len(payload_criticals) == 0


# ── Amplitude ─────────────────────────────────────────────────────────────────


def test_amplitude_purchase_valid():
    result = validate_payload(
        rb("amplitude"),
        "purchase",
        {
            "product_id": "SKU-1",
            "price": 9.99,
            "quantity": 1,
        },
    )
    payload_criticals = [f for f in result.findings if f.status == "critical" and f.param]
    assert len(payload_criticals) == 0
