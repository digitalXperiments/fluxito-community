"""Tests for the connector rate-limits catalog and its UI wiring.

Pure + file-based (no DB), so these run fast and deterministically. They guard
the catalog's integrity, the connected/available partitioning, and that both
surfaces (Project Settings tab + Home section) stay wired to the catalog.
"""

import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.app_state as app_state
from app.api.google_oauth_routes import GRANULAR_CONNECTOR_CATALOG
from app.connectors import rate_limits as rl
from app.connectors import usage as connector_usage

SETTINGS_TEMPLATE = Path("app/templates/projects/settings.html")
HOME_TEMPLATE = Path("app/templates/dashboard_home.html")
PARTIAL_TEMPLATE = Path("app/templates/partials/rate_limit_cards.html")


# ── Catalog integrity ────────────────────────────────────────────────────


def test_catalog_keys_are_unique():
    keys = [c.key for c in rl.CATALOG]
    assert len(keys) == len(set(keys))


def test_every_connector_is_well_formed():
    for c in rl.CATALOG:
        assert c.name, c.key
        assert c.headline, c.key
        assert c.category in (
            rl.CAT_ANALYTICS,
            rl.CAT_ADVERTISING,
            rl.CAT_TAGGING,
            rl.CAT_SEARCH,
            rl.CAT_WAREHOUSE,
            rl.CAT_MARKETING,
        ), c.key
        assert c.docs_url.startswith("https://"), c.key
        assert c.confidence in (rl.HIGH, rl.MEDIUM, rl.LOW), c.key
        assert c.consumption_note, c.key
        assert c.error_behavior, c.key
        assert c.reviewed, c.key
        assert c.limits, f"{c.key} has no documented limits"
        for limit in c.limits:
            assert limit.name and limit.value and limit.window and limit.scope, (c.key, limit)


def test_every_connector_has_a_short_usage_line():
    # The card face shows headline + usage, so both must be present and concise.
    for c in rl.CATALOG:
        assert c.usage, c.key
        assert len(c.usage) <= 60, (c.key, c.usage)


def test_core_google_and_meta_connectors_are_present():
    keys = {c.key for c in rl.CATALOG}
    for expected in (
        "ga4",
        "gtm",
        "bigquery",
        "google_ads",
        "search_console",
        "data_manager",
        "meta_ads",
    ):
        assert expected in keys


def test_catalog_covers_every_granular_connector():
    """Every connector the connect-counter knows about has a rate-limit entry.

    Adobe is one row in the granular catalog (has_adobe_analytics OR
    has_adobe_launch) but two rate-limit entries; either flag satisfying it is
    enough. Marketo has a rate-limit entry without a granular-catalog row.
    """
    rl_flags = {f for c in rl.CATALOG for f in c.flags}
    for _key, _label, attrs in GRANULAR_CONNECTOR_CATALOG:
        assert any(a in rl_flags for a in attrs), f"no rate-limit entry maps to {attrs}"


def test_no_catalog_flag_is_unknown_to_the_connect_counter():
    """Catalog flags use the same has_* vocabulary as the connect counter.

    has_marketo is the one intentional addition (Marketo isn't in the granular
    counter), so it's allowed through.
    """
    granular_flags = {a for _k, _l, attrs in GRANULAR_CONNECTOR_CATALOG for a in attrs}
    rl_flags = {f for c in rl.CATALOG for f in c.flags}
    assert rl_flags - granular_flags == {"has_marketo"}


# ── Helpers ──────────────────────────────────────────────────────────────


def test_connected_keys_maps_flags_to_connectors():
    flags = SimpleNamespace(has_ga4=True, has_meta=True, has_bq=True)
    assert rl.connected_keys(flags) == {"ga4", "meta_ads", "bigquery"}


def test_connected_keys_tolerates_missing_attributes():
    # An object with none of the has_* attrs resolves to no connectors.
    assert rl.connected_keys(SimpleNamespace()) == set()


def test_adobe_analytics_and_launch_split_into_two_connectors():
    flags = SimpleNamespace(has_adobe_analytics=True, has_adobe_launch=True)
    assert rl.connected_keys(flags) == {"adobe_analytics", "adobe_launch"}


def test_partition_is_exhaustive_and_ordered():
    connected = {"ga4", "meta_ads"}
    conn, avail = rl.partition(connected)
    assert {c.key for c in conn} == connected
    assert len(conn) + len(avail) == len(rl.CATALOG)
    # Catalog order is preserved within each bucket.
    catalog_order = [c.key for c in rl.CATALOG]
    assert [c.key for c in avail] == [k for k in catalog_order if k not in connected]


def test_to_view_returns_json_friendly_dicts():
    view = rl.to_view(list(rl.CATALOG))
    assert len(view) == len(rl.CATALOG)
    first = view[0]
    assert isinstance(first, dict)
    assert isinstance(first["flags"], list)
    assert isinstance(first["limits"], list)
    assert isinstance(first["limits"][0], dict)
    assert {"name", "value", "window", "scope"} <= set(first["limits"][0])


def test_by_key_roundtrips():
    assert rl.by_key("ga4").name == "Google Analytics 4"
    assert rl.by_key("nope") is None


# ── Settings tab wiring ──────────────────────────────────────────────────


def test_settings_has_api_limits_tab_between_connections_and_notifications():
    src = SETTINGS_TEMPLATE.read_text()
    # The revamped Settings page is a single editorial scroll (no tab bar); the
    # API limits section sits between Connections and Notifications by order.
    assert 'id="limits"' in src
    assert src.index('id="connections"') < src.index('id="limits"')
    assert src.index('id="limits"') < src.index('id="notifications"')


def test_settings_limits_panel_renders_via_shared_partial():
    src = SETTINGS_TEMPLATE.read_text()
    assert 'id="limits"' in src
    # Cards come from the shared partial, not an inline macro.
    assert 'import "partials/rate_limit_cards.html" as rlcards' in src
    assert "rlcards.rl_assets()" in src
    assert "rlcards.rl_card(c)" in src
    assert "rlcards.rl_card(c, muted=true)" in src  # catalog cards dimmed
    assert "rate_limits_connected" in src
    assert "rate_limits_available" in src
    assert "rate_limits_reviewed" in src


# ── Home section wiring ──────────────────────────────────────────────────


def test_home_renders_connected_limits_section_with_deep_link():
    # The home page is now a pure MCP command center. The rate-limit catalog
    # lives solely in Project Settings (guarded by the settings tests above).
    # Guard that home does not re-embed the catalog cards or a stale partial import.
    src = HOME_TEMPLATE.read_text()
    assert "home-agent-hub" in src or "mcp-hub-card" in src
    assert "rlcards.rl_card" not in src
    assert "rate_limit_cards.html" not in src


# ── Shared compact-card + modal partial ──────────────────────────────────


def test_card_partial_is_a_minimal_row_with_usage_and_modal():
    src = PARTIAL_TEMPLATE.read_text()
    # Each row shows the limit + the real calls consumed, plus an info trigger.
    assert "macro rl_card" in src
    assert "rl-row" in src
    assert "c.headline" in src and "usage_count" in src
    assert "No calls in" in src  # graceful empty state
    assert "rl-bar" in src and "headroom_pct" in src  # busiest-window headroom bar
    assert "openRlModal(" in src
    # Full detail lives in the per-connector hidden template, opened in one modal.
    assert 'id="rld-{{ c.key }}"' in src
    assert "macro rl_assets" in src and 'id="rlModal"' in src
    # Verbose detail (note, table, error behavior, typical cost, docs) is in the modal.
    for detail in ("consumption_note", "c.limits", "error_behavior", "c.usage", "c.docs_url"):
        assert detail in src, detail


# ── Drift checker (app/connectors/rate_limits_drift.py) ───────────────────


def test_drift_audit_covers_every_connector():
    from app.connectors.rate_limits_drift import audit

    rows = audit(max_age_days=180)
    assert {r["key"] for r in rows} == {c.key for c in rl.CATALOG}
    for r in rows:
        assert {"key", "name", "docs_url", "reviewed", "age_days", "confidence", "stale"} <= set(r)


def test_drift_audit_staleness_is_threshold_and_date_driven():
    import datetime as dt

    from app.connectors.rate_limits_drift import audit

    # Threshold extremes: -1 day flags everything, a huge window flags nothing.
    assert all(r["stale"] for r in audit(max_age_days=-1))
    assert not any(r["stale"] for r in audit(max_age_days=10**6))
    # Date-driven: years after every reviewed date, all entries are overdue.
    assert all(r["stale"] for r in audit(max_age_days=180, today=dt.date(2035, 1, 1)))


def test_drift_prompt_is_self_contained():
    from app.connectors.rate_limits_drift import DRIFT_PROMPT

    assert "rate_limits.py" in DRIFT_PROMPT
    assert "CHANGED" in DRIFT_PROMPT and "STALE" in DRIFT_PROMPT and "UNVERIFIED" in DRIFT_PROMPT


# ── Usage counters (app/connectors/usage.py) ─────────────────────────────


def test_cache_key_maps_to_connector():
    assert connector_usage.connector_for_cache_key("cache:ga4:report:abc") == "ga4"
    assert connector_usage.connector_for_cache_key("cache:ads:campaigns:x") == "google_ads"
    assert connector_usage.connector_for_cache_key("cache:launch:rules:x") == "adobe_launch"
    # Non-connector / non-cache keys don't count.
    assert connector_usage.connector_for_cache_key("cache:dashboard:123") is None
    assert connector_usage.connector_for_cache_key("mcp:active_project:u1") is None


def test_instrumented_connectors_are_a_subset_of_the_catalog():
    keys = {c.key for c in rl.CATALOG}
    assert keys >= connector_usage.INSTRUMENTED_CONNECTORS


class _FakeRedis:
    def __init__(self, data):
        self.data = data

    async def mget(self, keys):
        return [self.data.get(k) for k in keys]


@pytest.mark.asyncio
async def test_usage_for_sums_daily_counters(monkeypatch):
    today = dt.datetime.utcnow().date()
    d0 = today.strftime("%Y%m%d")
    d1 = (today - dt.timedelta(days=1)).strftime("%Y%m%d")
    data = {
        f"usage:proj1:ga4:{d0}": b"10",
        f"usage:proj1:ga4:{d1}": b"5",
        f"usage:proj1:gtm:{d0}": b"3",
    }
    monkeypatch.setattr(app_state, "redis_client", _FakeRedis(data))
    out = await connector_usage.usage_for("proj1", ["ga4", "gtm", "meta_ads"], days=30)
    assert out == {"ga4": 15, "gtm": 3}  # meta_ads has no recorded calls → omitted


@pytest.mark.asyncio
async def test_usage_for_is_empty_without_redis(monkeypatch):
    monkeypatch.setattr(app_state, "redis_client", None)
    assert await connector_usage.usage_for("p", ["ga4"]) == {}
