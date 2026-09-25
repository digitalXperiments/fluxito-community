"""
Cross-Platform Attribution & Blended Reporting

The killer Pro feature: queries ALL connected platforms in parallel, normalizes
metrics into a common schema, and returns a unified view for Claude to reason across.

Actions:
  blended_performance — aggregated metrics across all connected ad platforms
  channel_comparison  — side-by-side platform comparison with derived metrics
  top_campaigns       — top N campaigns across all platforms by spend/ROAS/conversions
"""

import asyncio
import logging
from typing import Annotated, Literal

from pydantic import BeforeValidator

import app.app_state as state

# Pydantic type that coerces incoming ints/floats to str — some clients send
# numeric IDs as JSON numbers rather than strings.
CoercedStr = Annotated[str, BeforeValidator(str)]
from app.config import settings
from app.tools.shared_helpers import get_current_user, get_google_conn_id, get_provider_token

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers (delegated to shared_helpers)
# ---------------------------------------------------------------------------


def _get_user():
    return get_current_user()


def _get_google_conn_id():
    return get_google_conn_id()


def _get_provider_token(provider_str: str):
    return get_provider_token(provider_str)


# ---------------------------------------------------------------------------
# Metric normalization
# ---------------------------------------------------------------------------


def _safe_float(val, default=0.0):
    """Safely convert a value to float."""
    if val is None:
        return default
    try:
        # Handle currency strings like "$1,234.56"
        if isinstance(val, str):
            val = val.replace("$", "").replace(",", "").strip()
        return float(val)
    except (ValueError, TypeError):
        return default


def _safe_int(val, default=0):
    if val is None:
        return default
    try:
        return int(float(str(val).replace(",", "")))
    except (ValueError, TypeError):
        return default


def _derive_metrics(row: dict) -> dict:
    """Calculate derived metrics (CTR, CPC, CPA, ROAS) from base metrics."""
    impressions = _safe_float(row.get("impressions"))
    clicks = _safe_float(row.get("clicks"))
    spend = _safe_float(row.get("spend"))
    conversions = _safe_float(row.get("conversions"))
    revenue = _safe_float(row.get("revenue"))

    row["ctr"] = round((clicks / impressions * 100), 2) if impressions > 0 else 0.0
    row["cpc"] = round(spend / clicks, 2) if clicks > 0 else 0.0
    row["cpa"] = round(spend / conversions, 2) if conversions > 0 else 0.0
    row["roas"] = round(revenue / spend, 2) if spend > 0 else 0.0
    return row


def _normalize_google_campaigns(data: dict) -> list[dict]:
    """Normalize Google Ads campaign data to common schema."""
    rows = []
    campaigns = data.get("campaigns", data.get("rows", []))
    if isinstance(campaigns, dict):
        campaigns = [campaigns]
    for c in campaigns:
        row = {
            "platform": "google_ads",
            "campaign_id": str(c.get("campaign_id", c.get("id", ""))),
            "campaign_name": c.get("campaign_name", c.get("name", "Unknown")),
            "impressions": _safe_int(c.get("impressions")),
            "clicks": _safe_int(c.get("clicks")),
            "spend": _safe_float(c.get("cost", c.get("spend", c.get("cost_micros", 0)))),
            "conversions": _safe_float(c.get("conversions")),
            "revenue": _safe_float(c.get("conversion_value", c.get("revenue", 0))),
        }
        # Google Ads returns cost in micros sometimes
        if row["spend"] > 100000:  # likely micros
            row["spend"] = row["spend"] / 1_000_000
        rows.append(_derive_metrics(row))
    return rows


def _normalize_meta_campaigns(data: dict) -> list[dict]:
    """Normalize Meta Ads campaign data to common schema."""
    rows = []
    campaigns = data.get("campaigns", data.get("rows", []))
    if isinstance(campaigns, dict):
        campaigns = [campaigns]
    for c in campaigns:
        row = {
            "platform": "meta",
            "campaign_id": str(c.get("campaign_id", c.get("id", ""))),
            "campaign_name": c.get("campaign_name", c.get("name", "Unknown")),
            "impressions": _safe_int(c.get("impressions")),
            "clicks": _safe_int(c.get("clicks", c.get("link_clicks", 0))),
            "spend": _safe_float(c.get("spend")),
            "conversions": _safe_float(c.get("conversions", c.get("actions_count", 0))),
            "revenue": _safe_float(c.get("purchase_value", c.get("revenue", 0))),
        }
        rows.append(_derive_metrics(row))
    return rows


def _normalize_tiktok_campaigns(data: dict) -> list[dict]:
    """Normalize TikTok Ads campaign data to common schema."""
    rows = []
    campaigns = data.get("campaigns", data.get("rows", []))
    if isinstance(campaigns, dict):
        campaigns = [campaigns]
    for c in campaigns:
        row = {
            "platform": "tiktok",
            "campaign_id": str(c.get("campaign_id", c.get("id", ""))),
            "campaign_name": c.get("campaign_name", c.get("name", "Unknown")),
            "impressions": _safe_int(c.get("impressions", c.get("show_cnt", 0))),
            "clicks": _safe_int(c.get("clicks", c.get("click_cnt", 0))),
            "spend": _safe_float(c.get("spend", c.get("total_cost", 0))),
            "conversions": _safe_float(c.get("conversions", c.get("convert_cnt", 0))),
            "revenue": _safe_float(c.get("total_purchase_value", c.get("revenue", 0))),
        }
        rows.append(_derive_metrics(row))
    return rows


def _normalize_snap_campaigns(data: dict) -> list[dict]:
    """Normalize Snap Ads campaign data to common schema."""
    rows = []
    campaigns = data.get("campaigns", data.get("rows", []))
    if isinstance(campaigns, dict):
        campaigns = [campaigns]
    for c in campaigns:
        spend_val = _safe_float(c.get("spend", c.get("total_budget", 0)))
        if spend_val > 100000:
            spend_val = spend_val / 1_000_000  # Snap uses micro-currency
        row = {
            "platform": "snap",
            "campaign_id": str(c.get("campaign_id", c.get("id", ""))),
            "campaign_name": c.get("campaign_name", c.get("name", "Unknown")),
            "impressions": _safe_int(c.get("impressions")),
            "clicks": _safe_int(c.get("clicks", c.get("swipes", 0))),
            "spend": spend_val,
            "conversions": _safe_float(c.get("conversions", c.get("conversion_purchases", 0))),
            "revenue": _safe_float(c.get("conversion_purchases_value", c.get("revenue", 0))),
        }
        rows.append(_derive_metrics(row))
    return rows


# ---------------------------------------------------------------------------
# Data fetchers — each returns (platform_name, normalized_rows) or (name, error)
# ---------------------------------------------------------------------------


async def _fetch_google_ads(user, date_start, date_end, account_id=None):
    """Fetch and normalize Google Ads campaign data."""
    try:
        conn_id = _get_google_conn_id()
        if not conn_id:
            return ("google_ads", {"error": True, "message": "No Google connection"})

        # If no account_id, list accounts and use first
        if not account_id:
            accounts_data = await state.ads_connector.list_accounts(conn_id)
            accounts = accounts_data.get("accounts", [])
            if not accounts:
                return ("google_ads", {"error": True, "message": "No Google Ads accounts found"})
            account_id = accounts[0].get("customer_id")

        data = await state.ads_connector.get_campaign_performance(
            conn_id, account_id, date_start, date_end, None
        )
        if data.get("error"):
            return ("google_ads", data)
        return ("google_ads", _normalize_google_campaigns(data))
    except Exception as e:
        logger.error(f"Cross-platform Google Ads fetch error: {e}")
        return ("google_ads", {"error": True, "message": str(e)})


async def _fetch_meta_ads(user, date_start, date_end, account_id=None):
    """Fetch and normalize Meta Ads campaign data."""
    try:
        token = _get_provider_token("meta")
        if not token:
            return ("meta", {"error": True, "message": "No Meta connection"})

        if not account_id:
            accounts_data = await state.meta_connector.list_accounts(token)
            accounts = accounts_data.get("accounts", [])
            if not accounts:
                return ("meta", {"error": True, "message": "No Meta Ads accounts found"})
            account_id = accounts[0].get("account_id")

        data = await state.meta_connector.get_campaign_performance(
            token, account_id, date_start, date_end, None
        )
        if data.get("error"):
            return ("meta", data)
        return ("meta", _normalize_meta_campaigns(data))
    except Exception as e:
        logger.error(f"Cross-platform Meta fetch error: {e}")
        return ("meta", {"error": True, "message": str(e)})


async def _fetch_tiktok_ads(user, date_start, date_end, account_id=None):
    """Fetch and normalize TikTok Ads campaign data."""
    try:
        token = _get_provider_token("tiktok")
        if not token:
            return ("tiktok", {"error": True, "message": "No TikTok connection"})

        if not account_id:
            accounts_data = await state.tiktok_connector.list_accounts(token)
            accounts = accounts_data.get("accounts", [])
            if not accounts:
                return ("tiktok", {"error": True, "message": "No TikTok Ads accounts found"})
            account_id = accounts[0].get("advertiser_id", accounts[0].get("account_id"))

        data = await state.tiktok_connector.get_campaign_performance(
            token, account_id, date_start, date_end, None
        )
        if data.get("error"):
            return ("tiktok", data)
        return ("tiktok", _normalize_tiktok_campaigns(data))
    except Exception as e:
        logger.error(f"Cross-platform TikTok fetch error: {e}")
        return ("tiktok", {"error": True, "message": str(e)})


async def _fetch_snap_ads(user, date_start, date_end, account_id=None):
    """Fetch and normalize Snap Ads campaign data."""
    try:
        token = _get_provider_token("snap")
        if not token:
            return ("snap", {"error": True, "message": "No Snap connection"})

        if not account_id:
            accounts_data = await state.snap_connector.list_accounts(token)
            accounts = accounts_data.get("accounts", [])
            if not accounts:
                return ("snap", {"error": True, "message": "No Snap Ads accounts found"})
            account_id = accounts[0].get("account_id", accounts[0].get("id"))

        data = await state.snap_connector.get_campaign_performance(
            token, account_id, date_start, date_end, None
        )
        if data.get("error"):
            return ("snap", data)
        return ("snap", _normalize_snap_campaigns(data))
    except Exception as e:
        logger.error(f"Cross-platform Snap fetch error: {e}")
        return ("snap", {"error": True, "message": str(e)})


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------


def _aggregate_by_platform(all_rows: list[dict]) -> list[dict]:
    """Roll up campaign-level rows into per-platform totals."""
    platforms = {}
    for row in all_rows:
        p = row["platform"]
        if p not in platforms:
            platforms[p] = {
                "platform": p,
                "campaigns": 0,
                "impressions": 0,
                "clicks": 0,
                "spend": 0.0,
                "conversions": 0.0,
                "revenue": 0.0,
            }
        agg = platforms[p]
        agg["campaigns"] += 1
        agg["impressions"] += _safe_int(row.get("impressions"))
        agg["clicks"] += _safe_int(row.get("clicks"))
        agg["spend"] += _safe_float(row.get("spend"))
        agg["conversions"] += _safe_float(row.get("conversions"))
        agg["revenue"] += _safe_float(row.get("revenue"))

    result = []
    for agg in platforms.values():
        agg["spend"] = round(agg["spend"], 2)
        agg["conversions"] = round(agg["conversions"], 2)
        agg["revenue"] = round(agg["revenue"], 2)
        result.append(_derive_metrics(agg))
    return sorted(result, key=lambda x: x["spend"], reverse=True)


def _blended_totals(all_rows: list[dict]) -> dict:
    """Calculate blended totals across all platforms."""
    totals = {
        "total_platforms": len(set(r["platform"] for r in all_rows)),
        "total_campaigns": len(all_rows),
        "total_impressions": sum(_safe_int(r.get("impressions")) for r in all_rows),
        "total_clicks": sum(_safe_int(r.get("clicks")) for r in all_rows),
        "total_spend": round(sum(_safe_float(r.get("spend")) for r in all_rows), 2),
        "total_conversions": round(sum(_safe_float(r.get("conversions")) for r in all_rows), 2),
        "total_revenue": round(sum(_safe_float(r.get("revenue")) for r in all_rows), 2),
    }
    totals["blended_ctr"] = (
        round((totals["total_clicks"] / totals["total_impressions"] * 100), 2)
        if totals["total_impressions"] > 0
        else 0.0
    )
    totals["blended_cpc"] = (
        round(totals["total_spend"] / totals["total_clicks"], 2) if totals["total_clicks"] > 0 else 0.0
    )
    totals["blended_cpa"] = (
        round(totals["total_spend"] / totals["total_conversions"], 2)
        if totals["total_conversions"] > 0
        else 0.0
    )
    totals["blended_roas"] = (
        round(totals["total_revenue"] / totals["total_spend"], 2) if totals["total_spend"] > 0 else 0.0
    )
    return totals


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Revenue-source adapters for revenue_attribution
#
# Each adapter returns a dict with this shape:
#   {
#     "source": str,                       # adapter name
#     "success": bool,                     # whether data was retrieved
#     "per_channel": {ch: {sessions, conversions, revenue}},
#     "total_revenue_ground_truth": float | None,
#     "confidence": "high" | "medium" | "low",
#     "notes": str,
#     "error": str | None,
#   }
#
# Adapters are picked by `_resolve_revenue_source()` using the hierarchy:
#   warehouse → ga4 → amplitude → mixpanel → posthog → adobe_analytics
#   → ad_platforms_self_reported → spend_only
# The caller can short-circuit this with the explicit `revenue_source` arg.
# ---------------------------------------------------------------------------

_EMPTY_ADAPTER = {
    "per_channel": {},
    "total_revenue_ground_truth": None,
    "confidence": "low",
    "notes": "",
    "error": None,
}


def _normalize_channel_from_ga4(src: str, med: str) -> str:
    s = (src or "").lower()
    m = (med or "").lower()
    if s in ("google", "adwords") and m in ("cpc", "paid", "ppc"):
        return "google_ads"
    if s in ("facebook", "fb", "meta", "instagram", "ig") and m in ("cpc", "paid", "paid_social"):
        return "meta"
    if s == "tiktok" and m in ("cpc", "paid", "paid_social"):
        return "tiktok"
    if s in ("snapchat", "snap") and m in ("cpc", "paid"):
        return "snap"
    if m == "organic":
        return f"organic/{s}"
    if m == "(none)" or s == "(direct)":
        return "direct"
    if m == "email":
        return "email"
    if m == "referral":
        return f"referral/{s}"
    return f"{s}/{m}"


def _normalize_channel_from_utm(utm_source: str, utm_medium: str = "") -> str:
    """Normalize a UTM source string (Amplitude / Adobe) to our channel keys."""
    return _normalize_channel_from_ga4(utm_source, utm_medium)


async def _adapter_warehouse(
    user,
    date_start: str,
    date_end: str,
    warehouse_platform: str | None,
    revenue_table: str | None,
    revenue_column: str,
    order_id_column: str,
) -> dict:
    """Fetch total revenue from the warehouse. Channel split is NOT returned
    here — warehouse is ground-truth for totals only; channel attribution
    layers on top from GA4/ad platforms."""
    out = {"source": "warehouse", "success": False, **_EMPTY_ADAPTER}
    if not warehouse_platform or not revenue_table:
        out["error"] = "warehouse_platform + revenue_table required"
        return out
    has_wh = any(
        [
            getattr(user, "has_bq", False),
            getattr(user, "has_redshift", False),
            getattr(user, "has_snowflake", False),
        ]
    )
    if not has_wh:
        out["error"] = "no warehouse connected"
        return out
    try:
        from app.sql_safety import (
            InvalidIdentifierError,
            quote_identifier,
            validate_qualified_identifier,
        )

        # Module-level entrypoint (the warehouse_query tool itself is a closure
        # and is NOT importable — the old `import warehouse_query` always raised
        # ImportError, which was swallowed, so this path silently never ran).
        from app.tools.warehouse_tools import warehouse_query_impl as _wq_tool

        # Validate every identifier before interpolation. SQL drivers can't
        # bind column/table names, so these come from a strict allowlist.
        try:
            # revenue_table may be `dataset.table` (BQ) or just `table` (RS/SF)
            validate_qualified_identifier(revenue_table, field_name="revenue_table")
            safe_table_parts = [quote_identifier(p) for p in revenue_table.split(".")]
            safe_table = ".".join(safe_table_parts)
            safe_rev_col = quote_identifier(revenue_column)
            safe_order_col = quote_identifier(order_id_column or "created_at")
        except InvalidIdentifierError as exc:
            out["error"] = f"unsafe identifier in revenue_table/column: {exc}"
            return out

        # Validate dates look like ISO-8601 (YYYY-MM-DD) — we still inline
        # them because the downstream warehouse_query doesn't accept params,
        # but the regex guards against injection via the date field.
        import re as _re

        _date_re = _re.compile(r"^\d{4}-\d{2}-\d{2}$")
        if not (_date_re.match(date_start) and _date_re.match(date_end)):
            out["error"] = "date_start and date_end must be ISO-8601 (YYYY-MM-DD)"
            return out

        sql = (
            f"SELECT SUM({safe_rev_col}) AS total_revenue "
            f"FROM {safe_table} "
            f"WHERE DATE({safe_order_col}) "
            f"BETWEEN '{date_start}' AND '{date_end}'"
        )
        # warehouse_query is the MCP tool — call it directly; it resolves
        # the connection via user context.
        result = (
            await _wq_tool(
                engine=warehouse_platform,
                action="run_query",
                query=sql,
            )
            if callable(_wq_tool)
            else None
        )
        if isinstance(result, dict) and not result.get("error"):
            rows = result.get("rows") or []
            if rows:
                out["total_revenue_ground_truth"] = _safe_float(rows[0].get("total_revenue"))
                out["success"] = True
                out["confidence"] = "high"
                out["notes"] = (
                    "Warehouse revenue is ground truth; channel split is layered on from GA4/ad platforms."
                )
                return out
        out["error"] = (
            result.get("message") if isinstance(result, dict) else "warehouse_query returned no rows"
        )
    except Exception as exc:
        out["error"] = f"warehouse query failed: {exc}"
    return out


async def _adapter_ga4(
    user,
    date_start: str,
    date_end: str,
    ga4_property_id: str | None,
    use_first_touch: bool = False,
) -> dict:
    """Fetch per-channel sessions/conversions/revenue from GA4 Data API."""
    out = {"source": "ga4", "success": False, **_EMPTY_ADAPTER, "per_channel": {}}
    if not getattr(user, "has_ga4", False) or not ga4_property_id:
        out["error"] = "GA4 not connected or ga4_property_id missing"
        return out
    ga4 = state.ga4_connector
    conn_id = _get_google_conn_id()
    if not conn_id:
        out["error"] = "no Google connection"
        return out
    dim_source = "firstUserSource" if use_first_touch else "sessionSource"
    dim_medium = "firstUserMedium" if use_first_touch else "sessionMedium"
    try:
        report = await ga4.run_report(
            conn_id,
            ga4_property_id,
            dimensions=[dim_source, dim_medium],
            metrics=["sessions", "conversions", "totalRevenue", "purchaseRevenue"],
            date_range_start=date_start,
            date_range_end=date_end,
            limit=200,
        )
    except Exception as exc:
        out["error"] = f"GA4 fetch failed: {exc}"
        return out
    rows = (report.get("rows") if isinstance(report, dict) else None) or []
    per_channel: dict = {}
    for r in rows:
        src = r.get(dim_source) or r.get("source") or ""
        med = r.get(dim_medium) or r.get("medium") or ""
        ch = _normalize_channel_from_ga4(str(src), str(med))
        slot = per_channel.setdefault(
            ch,
            {
                "sessions": 0.0,
                "conversions": 0.0,
                "revenue": 0.0,
            },
        )
        slot["sessions"] += _safe_float(r.get("sessions"))
        slot["conversions"] += _safe_float(r.get("conversions"))
        rev = _safe_float(r.get("purchaseRevenue")) or _safe_float(r.get("totalRevenue"))
        slot["revenue"] += rev
    out["per_channel"] = per_channel
    out["success"] = True
    out["confidence"] = "high"
    out["notes"] = f"GA4 Data API, {'first-touch' if use_first_touch else 'last-touch'} session channel."
    return out


async def _adapter_amplitude(
    user,
    date_start: str,
    date_end: str,
    amplitude_project_id: str | None,
) -> dict:
    """Amplitude revenue adapter. The REST `/revenue/ltv` endpoint gives
    total revenue by date but not a channel breakdown. We report it as a
    TOTAL-only source with medium confidence; the channel split must come
    from another layer (self-reported ad platforms or GA4)."""
    out = {"source": "amplitude", "success": False, **_EMPTY_ADAPTER}
    if not getattr(user, "has_amplitude", False):
        out["error"] = "Amplitude not connected"
        return out
    amp = getattr(state, "amplitude_connector", None)
    if amp is None:
        out["error"] = "amplitude_connector not initialised"
        return out
    try:
        from app.tools.shared_helpers import get_amplitude_creds

        _, api_key, secret_key = await get_amplitude_creds(str(getattr(user, "id", "")))
    except Exception as exc:
        out["error"] = f"could not resolve Amplitude creds: {exc}"
        return out
    try:
        result = await amp.get_revenue(api_key, secret_key, date_start, date_end)
    except Exception as exc:
        out["error"] = f"Amplitude get_revenue failed: {exc}"
        return out
    if isinstance(result, dict) and result.get("error"):
        out["error"] = result.get("message") or "amplitude error"
        return out
    # Extract total revenue — Amplitude's LTV shape: data.series lists daily
    # totals. Sum defensively across any numeric leaf.
    total_rev = 0.0
    data = (result.get("data") or {}) if isinstance(result, dict) else {}
    series = data.get("series") or []
    for s in series:
        if isinstance(s, list):
            for v in s:
                total_rev += _safe_float(v)
        else:
            total_rev += _safe_float(s)
    out["total_revenue_ground_truth"] = round(total_rev, 2) or None
    out["success"] = total_rev > 0
    out["confidence"] = "medium"
    out["notes"] = (
        "Amplitude /revenue/ltv gives aggregate revenue only (no channel "
        "split). Use ad-platform self-reported or GA4 for the channel "
        "breakdown."
    )
    return out


async def _adapter_mixpanel(
    user,
    date_start: str,
    date_end: str,
    mixpanel_project_id: str | None,
) -> dict:
    """Mixpanel revenue adapter. The Revenue API (`/2.0/retention/revenue/`)
    gives aggregate revenue only — no channel split. We report it as a
    TOTAL-only source with medium confidence; the channel split must come
    from another layer (self-reported ad platforms or GA4)."""
    out = {"source": "mixpanel", "success": False, **_EMPTY_ADAPTER}
    if not getattr(user, "has_mixpanel", False):
        out["error"] = "Mixpanel not connected"
        return out
    mp = getattr(state, "mixpanel_connector", None)
    if mp is None:
        out["error"] = "mixpanel_connector not initialised"
        return out
    try:
        from app.tools.shared_helpers import get_mixpanel_creds

        _, api_key, secret_key = await get_mixpanel_creds(str(getattr(user, "id", "")))
    except Exception as exc:
        out["error"] = f"could not resolve Mixpanel creds: {exc}"
        return out
    try:
        result = await mp.get_revenue(api_key, secret_key, date_start, date_end)
    except Exception as exc:
        out["error"] = f"Mixpanel get_revenue failed: {exc}"
        return out
    if isinstance(result, dict) and result.get("error"):
        out["error"] = result.get("message") or "mixpanel error"
        return out
    # Extract total revenue — Mixpanel revenue shape varies; sum any
    # numeric leaves defensively.
    total_rev = 0.0
    data = (result.get("data") or result.get("results") or {}) if isinstance(result, dict) else {}
    if isinstance(data, list):
        for item in data:
            total_rev += _safe_float(item.get("revenue") if isinstance(item, dict) else item)
    elif isinstance(data, dict):
        for v in data.values():
            total_rev += _safe_float(v)
    else:
        total_rev += _safe_float(data)
    out["total_revenue_ground_truth"] = round(total_rev, 2) or None
    out["success"] = total_rev > 0
    out["confidence"] = "medium"
    out["notes"] = (
        "Mixpanel Revenue API gives aggregate revenue only (no channel "
        "split). Use ad-platform self-reported or GA4 for the channel "
        "breakdown."
    )
    return out


async def _adapter_posthog(
    user,
    date_start: str,
    date_end: str,
    posthog_project_id: str | None,
) -> dict:
    """PostHog revenue adapter. PostHog does not have a native revenue
    endpoint — revenue is tracked via custom events. The connector queries
    the events API for a revenue event and sums the revenue property.
    Returns aggregate revenue with medium confidence."""
    out = {"source": "posthog", "success": False, **_EMPTY_ADAPTER}
    if not getattr(user, "has_posthog", False):
        out["error"] = "PostHog not connected"
        return out
    ph = getattr(state, "posthog_connector", None)
    if ph is None:
        out["error"] = "posthog_connector not initialised"
        return out
    try:
        from app.tools.shared_helpers import get_posthog_creds

        _, api_key, project_host, project_id = await get_posthog_creds(
            str(getattr(user, "id", "")),
        )
    except Exception as exc:
        out["error"] = f"could not resolve PostHog creds: {exc}"
        return out
    try:
        result = await ph.get_revenue(api_key, project_host, project_id, date_start, date_end)
    except Exception as exc:
        out["error"] = f"PostHog get_revenue failed: {exc}"
        return out
    if isinstance(result, dict) and result.get("error"):
        out["error"] = result.get("message") or "posthog error"
        return out
    total_rev = 0.0
    if isinstance(result, dict):
        total_rev = _safe_float(result.get("total_revenue") or result.get("revenue"))
    out["total_revenue_ground_truth"] = round(total_rev, 2) or None
    out["success"] = total_rev > 0
    out["confidence"] = "medium"
    out["notes"] = (
        "PostHog revenue is derived from custom events (e.g. 'purchase' "
        "event with a revenue property). No native revenue endpoint — "
        "use ad-platform self-reported or GA4 for the channel breakdown."
    )
    return out


async def _adapter_adobe_analytics(
    user,
    date_start: str,
    date_end: str,
    adobe_report_suite_id: str | None,
    adobe_org_id: str | None,
) -> dict:
    """Adobe Analytics revenue adapter. Uses run_report with the
    `lastTouchChannel` dimension and `revenue` metric for a per-channel
    breakdown. Requires an explicit report_suite_id."""
    out = {"source": "adobe_analytics", "success": False, **_EMPTY_ADAPTER, "per_channel": {}}
    if not getattr(user, "has_adobe_analytics", False):
        out["error"] = "Adobe Analytics not connected"
        return out
    if not adobe_report_suite_id:
        out["error"] = "adobe_report_suite_id required"
        return out
    adobe = getattr(state, "adobe_analytics_connector", None)
    if adobe is None:
        out["error"] = "adobe_analytics_connector not initialised"
        return out
    try:
        from app.tools.shared_helpers import get_adobe_analytics_creds

        _, client_id, client_secret, resolved_org, company_id = await get_adobe_analytics_creds(
            str(getattr(user, "id", "")), adobe_org_id
        )
        org_id = resolved_org
    except Exception as exc:
        out["error"] = f"could not resolve Adobe creds: {exc}"
        return out
    try:
        report = await adobe.run_report(
            client_id,
            client_secret,
            org_id,
            rsid=adobe_report_suite_id,
            dimensions=["variables/lasttouchchannel"],
            metrics=["metrics/revenue", "metrics/orders", "metrics/visits"],
            date_range={"start": date_start, "end": date_end},
            limit=200,
            company_id=company_id,
        )
    except Exception as exc:
        out["error"] = f"Adobe run_report failed: {exc}"
        return out
    if isinstance(report, dict) and report.get("error"):
        out["error"] = report.get("message") or "adobe error"
        return out
    per_channel: dict = {}
    rows = report.get("rows") if isinstance(report, dict) else None
    for r in rows or []:
        ch_raw = r.get("name") or r.get("value") or r.get("dimension") or ""
        ch = _normalize_channel_from_utm(str(ch_raw))
        data = r.get("data") or r.get("metrics") or []
        # Adobe row["data"] is a positional list matching the metrics order.
        if isinstance(data, list) and len(data) >= 3:
            rev, orders, visits = (
                _safe_float(data[0]),
                _safe_float(data[1]),
                _safe_float(data[2]),
            )
        else:
            rev = _safe_float(r.get("revenue"))
            orders = _safe_float(r.get("orders"))
            visits = _safe_float(r.get("visits"))
        slot = per_channel.setdefault(
            ch,
            {
                "sessions": 0.0,
                "conversions": 0.0,
                "revenue": 0.0,
            },
        )
        slot["sessions"] += visits
        slot["conversions"] += orders
        slot["revenue"] += rev
    out["per_channel"] = per_channel
    out["success"] = bool(per_channel)
    out["confidence"] = "high" if per_channel else "low"
    out["notes"] = (
        "Adobe Analytics last-touch marketing channel. Switch to variables/marketingchannel for first-touch."
    )
    return out


def _adapter_self_reported(all_rows: list[dict]) -> dict:
    """Aggregate each platform's self-reported conversions + revenue from
    the already-fetched ad-platform rows. This OVER-COUNTS: every platform
    claims the same conversion as its own, so totals will be inflated."""
    per_channel: dict = {}
    for row in all_rows:
        ch = (row.get("platform") or "").lower()
        if ch == "google":
            ch = "google_ads"
        slot = per_channel.setdefault(
            ch,
            {
                "sessions": 0.0,
                "conversions": 0.0,
                "revenue": 0.0,
            },
        )
        # Ad platforms don't report sessions in the same sense.
        slot["sessions"] += _safe_float(row.get("clicks"))
        slot["conversions"] += _safe_float(row.get("conversions"))
        slot["revenue"] += _safe_float(row.get("revenue"))
    return {
        "source": "ad_platforms_self_reported",
        "success": bool(per_channel),
        "per_channel": per_channel,
        "total_revenue_ground_truth": None,
        "confidence": "low",
        "notes": (
            "Self-reported revenue/conversions from each ad platform. "
            "OVER-COUNTS because platforms double-claim the same purchase. "
            "Treat as a directional proxy, not a source of truth."
        ),
        "error": None,
    }


def _adapter_spend_only(all_rows: list[dict]) -> dict:
    """Fallback when no revenue/conversion source is available. Returns
    spend-per-channel only; attribution math degrades to spend share."""
    per_channel: dict = {}
    for row in all_rows:
        ch = (row.get("platform") or "").lower()
        if ch == "google":
            ch = "google_ads"
        slot = per_channel.setdefault(
            ch,
            {
                "sessions": 0.0,
                "conversions": 0.0,
                "revenue": 0.0,
            },
        )
        slot["sessions"] += _safe_float(row.get("clicks"))
    return {
        "source": "spend_only",
        "success": bool(per_channel),
        "per_channel": per_channel,
        "total_revenue_ground_truth": None,
        "confidence": "low",
        "notes": (
            "No revenue source available. Returning spend-per-channel. "
            "ROAS / attributed revenue will be null — connect GA4, a "
            "warehouse, Amplitude, Mixpanel, PostHog, or Adobe Analytics to enable attribution."
        ),
        "error": None,
    }


def _available_revenue_sources(
    user,
    all_rows: list[dict],
    warehouse_platform: str | None,
    revenue_table: str | None,
    ga4_property_id: CoercedStr | None,
    adobe_report_suite_id: str | None,
) -> list[str]:
    """Heuristically list which revenue-source adapters *could* run given
    the current user's connections. Useful for the response payload so
    callers know what alternatives they have."""
    avail: list[str] = []
    has_wh = any(
        [
            getattr(user, "has_bq", False),
            getattr(user, "has_redshift", False),
            getattr(user, "has_snowflake", False),
        ]
    )
    if has_wh and warehouse_platform and revenue_table:
        avail.append("warehouse")
    if getattr(user, "has_ga4", False) and ga4_property_id:
        avail.append("ga4")
    if getattr(user, "has_amplitude", False):
        avail.append("amplitude")
    if getattr(user, "has_mixpanel", False):
        avail.append("mixpanel")
    if getattr(user, "has_posthog", False):
        avail.append("posthog")
    if getattr(user, "has_adobe_analytics", False) and adobe_report_suite_id:
        avail.append("adobe_analytics")
    if all_rows:
        avail.append("ad_platforms_self_reported")
        avail.append("spend_only")
    return avail


def register_cross_platform_tools(mcp_server):
    @mcp_server.tool("cross_platform_report")
    async def cross_platform_report(
        action: Literal[
            "blended_performance",
            "channel_comparison",
            "top_campaigns",
            "revenue_attribution",
        ],
        date_range_start: str | None = None,
        date_range_end: str | None = None,
        platforms: list[str] | None = None,
        sort_by: str | None = None,
        limit: int = 20,
        google_ads_account_id: str | None = None,
        meta_account_id: str | None = None,
        tiktok_account_id: str | None = None,
        snap_account_id: str | None = None,
        # ── revenue_attribution params ────────────────────────────────
        model: str | None = None,
        attribution_window_days: int = 30,
        conversion_event: str | None = "purchase",
        ga4_property_id: CoercedStr | None = None,
        warehouse_platform: str | None = None,
        revenue_table: str | None = None,
        order_id_column: str | None = "order_id",
        revenue_column: str | None = "revenue",
        channels: list[str] | None = None,
        # ── revenue-source selection ─────────────────────────────────
        revenue_source: str | None = None,
        amplitude_project_id: str | None = None,
        adobe_report_suite_id: str | None = None,
        adobe_org_id: str | None = None,
    ) -> dict:
        """Blended reporting across connected ad platforms. Pro/Team only.

        All platform fetches run in parallel (asyncio.gather) — latency ≈ slowest
        platform, not sum of platforms.

        action: blended_performance | channel_comparison | top_campaigns
        Dates: YYYY-MM-DD. If omitted, defaults to last 30 days.
        platforms: optional filter, e.g. ["google","meta"].
        sort_by (top_campaigns): spend|roas|conversions|clicks|cpa
        *_account_id: override auto-detected accounts when multiple exist.

        Call tool_help("cross_platform_report") for the full reference.
        """
        # Smart default: last 30 days if dates omitted
        from app.tools.defaults import default_date_range

        date_range_start, date_range_end = default_date_range(date_range_start, date_range_end, days=30)
        user = _get_user()

        if not user:
            return {
                "error": True,
                "error_type": "unauthenticated",
                "message": "No active session. Please sign in.",
            }

        # Determine which platforms to query
        platform_checks = {
            "google_ads": (user.has_ads, _fetch_google_ads, google_ads_account_id),
            "meta": (user.has_meta, _fetch_meta_ads, meta_account_id),
            "tiktok": (user.has_tiktok, _fetch_tiktok_ads, tiktok_account_id),
            "snap": (user.has_snap, _fetch_snap_ads, snap_account_id),
        }

        # Filter to requested platforms (if specified)
        if platforms:
            # Normalize platform names
            name_map = {
                "google": "google_ads",
                "google_ads": "google_ads",
                "gads": "google_ads",
                "meta": "meta",
                "facebook": "meta",
                "fb": "meta",
                "tiktok": "tiktok",
                "tt": "tiktok",
                "snap": "snap",
                "snapchat": "snap",
            }
            requested = {name_map.get(p.lower(), p.lower()) for p in platforms}
            platform_checks = {k: v for k, v in platform_checks.items() if k in requested}

        # Build list of fetch tasks for connected platforms
        tasks = []
        connected_names = []
        skipped = []

        for name, (is_connected, fetcher, acct_id) in platform_checks.items():
            if is_connected:
                connected_names.append(name)
                tasks.append(fetcher(user, date_range_start, date_range_end, acct_id))
            else:
                skipped.append(name)

        if not tasks:
            base = settings.APP_BASE_URL
            return {
                "error": True,
                "error_type": "no_platforms_connected",
                "message": (
                    "No ad platforms are connected. Connect at least one platform "
                    "to use cross-platform reporting."
                ),
                "connect_url": f"{base}/connect",
                "available_platforms": ["Google Ads", "Meta Ads", "TikTok Ads", "Snap Ads"],
            }

        # Fetch all platforms in parallel via asyncio.gather — total latency
        # ≈ slowest platform, NOT sum of platforms. return_exceptions=True
        # prevents one failing fetch from cancelling the others.
        import time as _t

        _t0 = _t.perf_counter()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        _parallel_ms = int((_t.perf_counter() - _t0) * 1000)
        logger.debug(f"cross_platform_report: fetched {len(tasks)} platforms in parallel in {_parallel_ms}ms")

        # Process results
        all_rows = []
        errors = {}
        platform_data = {}

        for result in results:
            if isinstance(result, Exception):
                errors["unknown"] = str(result)
                continue
            name, data = result
            if isinstance(data, dict) and data.get("error"):
                errors[name] = data.get("message", "Unknown error")
            elif isinstance(data, list):
                all_rows.extend(data)
                platform_data[name] = data

        if not all_rows and errors:
            return {
                "error": True,
                "message": "All platform queries failed.",
                "platform_errors": errors,
                "connected_platforms": connected_names,
                "skipped_platforms": skipped,
                "hint": (
                    "Check individual platform connections. Use get_connection_status "
                    "for details and connect URLs."
                ),
            }

        # Build response based on action
        if action == "blended_performance":
            return {
                "action": "blended_performance",
                "date_range": {"start": date_range_start, "end": date_range_end},
                "platforms_queried": connected_names,
                "platforms_skipped": skipped,
                "blended_totals": _blended_totals(all_rows),
                "per_platform": _aggregate_by_platform(all_rows),
                "errors": errors if errors else None,
            }

        elif action == "channel_comparison":
            comparison = _aggregate_by_platform(all_rows)
            # Add percentage of total spend
            total_spend = sum(r["spend"] for r in comparison) or 1
            for row in comparison:
                row["spend_share_pct"] = round(row["spend"] / total_spend * 100, 1)
            return {
                "action": "channel_comparison",
                "date_range": {"start": date_range_start, "end": date_range_end},
                "platforms_queried": connected_names,
                "comparison": comparison,
                "blended_totals": _blended_totals(all_rows),
                "errors": errors if errors else None,
            }

        elif action == "top_campaigns":
            sort_field = sort_by or "spend"
            valid_sorts = {"spend", "roas", "conversions", "clicks", "cpa", "impressions", "revenue"}
            if sort_field not in valid_sorts:
                sort_field = "spend"

            reverse = sort_field != "cpa"  # lower CPA is better
            sorted_rows = sorted(
                all_rows,
                key=lambda r: _safe_float(r.get(sort_field)),
                reverse=reverse,
            )[:limit]

            return {
                "action": "top_campaigns",
                "date_range": {"start": date_range_start, "end": date_range_end},
                "sort_by": sort_field,
                "limit": limit,
                "platforms_queried": connected_names,
                "campaigns": sorted_rows,
                "total_campaigns_across_platforms": len(all_rows),
                "blended_totals": _blended_totals(all_rows),
                "errors": errors if errors else None,
            }

        elif action == "revenue_attribution":
            # ── Multi-touch revenue attribution ───────────────────────
            # Resolves a revenue source (warehouse > ga4 > amplitude >
            # mixpanel > posthog >
            # adobe_analytics > ad_platforms_self_reported > spend_only),
            # then distributes channel credit per the chosen model.
            #
            # The caller may force a specific source via revenue_source=...
            #
            # Scope caveat: session-level channel data (from any of these
            # sources) is not a true user-level path. linear / time_decay /
            # position_based are session-share approximations and are
            # flagged as such in the response.
            chosen_model = (model or "last_touch").lower()
            SUPPORTED_MODELS = {
                "first_touch",
                "last_touch",
                "linear",
                "time_decay",
                "position_based",
            }
            if chosen_model not in SUPPORTED_MODELS:
                return {
                    "error": True,
                    "error_type": "unsupported_model",
                    "message": (
                        f"Model '{chosen_model}' is not supported. "
                        f"Choose one of: {sorted(SUPPORTED_MODELS)}. "
                        "Data-driven attribution (shapley/markov) requires "
                        "BigQuery Export and is not available as a sync call."
                    ),
                }

            # ── Resolve the revenue source ────────────────────────────
            VALID_SOURCES = {
                "warehouse",
                "ga4",
                "amplitude",
                "mixpanel",
                "posthog",
                "adobe_analytics",
                "ad_platforms_self_reported",
                "spend_only",
                "auto",
            }
            requested_source = (revenue_source or "auto").lower()
            if requested_source not in VALID_SOURCES:
                return {
                    "error": True,
                    "error_type": "unsupported_revenue_source",
                    "message": (
                        f"revenue_source '{requested_source}' is not valid. "
                        f"Choose one of: {sorted(VALID_SOURCES)}."
                    ),
                }

            available = _available_revenue_sources(
                user,
                all_rows,
                warehouse_platform,
                revenue_table,
                ga4_property_id,
                adobe_report_suite_id,
            )

            use_first_touch = chosen_model == "first_touch"

            async def _try_ga4():
                return await _adapter_ga4(
                    user,
                    date_range_start,
                    date_range_end,
                    ga4_property_id,
                    use_first_touch=use_first_touch,
                )

            async def _try_adobe():
                return await _adapter_adobe_analytics(
                    user,
                    date_range_start,
                    date_range_end,
                    adobe_report_suite_id,
                    adobe_org_id,
                )

            async def _try_amplitude():
                return await _adapter_amplitude(
                    user,
                    date_range_start,
                    date_range_end,
                    amplitude_project_id,
                )

            async def _try_mixpanel():
                return await _adapter_mixpanel(
                    user,
                    date_range_start,
                    date_range_end,
                    None,
                )

            async def _try_posthog():
                return await _adapter_posthog(
                    user,
                    date_range_start,
                    date_range_end,
                    None,
                )

            async def _try_warehouse():
                return await _adapter_warehouse(
                    user,
                    date_range_start,
                    date_range_end,
                    warehouse_platform,
                    revenue_table,
                    revenue_column,
                    order_id_column,
                )

            channel_source: dict
            total_source: dict | None = None

            if requested_source == "warehouse":
                total_source = await _try_warehouse()
                # Channel source underneath: GA4 → Adobe → self-reported → spend
                channel_source = None  # type: ignore
                for fetch in (_try_ga4, _try_adobe):
                    cs = await fetch()
                    if cs["success"]:
                        channel_source = cs
                        break
                if channel_source is None:
                    sr = _adapter_self_reported(all_rows)
                    channel_source = sr if sr["success"] else _adapter_spend_only(all_rows)
            elif requested_source == "ga4":
                channel_source = await _try_ga4()
            elif requested_source == "amplitude":
                total_source = await _try_amplitude()
                sr = _adapter_self_reported(all_rows)
                channel_source = sr if sr["success"] else _adapter_spend_only(all_rows)
            elif requested_source == "mixpanel":
                total_source = await _try_mixpanel()
                sr = _adapter_self_reported(all_rows)
                channel_source = sr if sr["success"] else _adapter_spend_only(all_rows)
            elif requested_source == "posthog":
                total_source = await _try_posthog()
                sr = _adapter_self_reported(all_rows)
                channel_source = sr if sr["success"] else _adapter_spend_only(all_rows)
            elif requested_source == "adobe_analytics":
                channel_source = await _try_adobe()
            elif requested_source == "ad_platforms_self_reported":
                channel_source = _adapter_self_reported(all_rows)
            elif requested_source == "spend_only":
                channel_source = _adapter_spend_only(all_rows)
            else:
                # auto
                has_wh = any(
                    [
                        getattr(user, "has_bq", False),
                        getattr(user, "has_redshift", False),
                        getattr(user, "has_snowflake", False),
                    ]
                )
                if has_wh and warehouse_platform and revenue_table:
                    wh = await _try_warehouse()
                    if wh["success"]:
                        total_source = wh
                channel_source = None  # type: ignore
                if getattr(user, "has_ga4", False) and ga4_property_id:
                    cs = await _try_ga4()
                    if cs["success"]:
                        channel_source = cs
                if (
                    channel_source is None
                    and getattr(user, "has_adobe_analytics", False)
                    and adobe_report_suite_id
                ):
                    cs = await _try_adobe()
                    if cs["success"]:
                        channel_source = cs
                if channel_source is None and getattr(user, "has_amplitude", False):
                    amp = await _try_amplitude()
                    if amp["success"] and total_source is None:
                        total_source = amp
                    sr = _adapter_self_reported(all_rows)
                    channel_source = sr if sr["success"] else _adapter_spend_only(all_rows)
                if channel_source is None and getattr(user, "has_mixpanel", False):
                    mp = await _try_mixpanel()
                    if mp["success"] and total_source is None:
                        total_source = mp
                    sr = _adapter_self_reported(all_rows)
                    channel_source = sr if sr["success"] else _adapter_spend_only(all_rows)
                if channel_source is None and getattr(user, "has_posthog", False):
                    ph = await _try_posthog()
                    if ph["success"] and total_source is None:
                        total_source = ph
                    sr = _adapter_self_reported(all_rows)
                    channel_source = sr if sr["success"] else _adapter_spend_only(all_rows)
                if channel_source is None:
                    sr = _adapter_self_reported(all_rows)
                    channel_source = sr if sr["success"] else _adapter_spend_only(all_rows)

            if not channel_source.get("success"):
                return {
                    "error": True,
                    "error_type": "no_revenue_source",
                    "message": (
                        "Could not resolve any revenue source. Tried "
                        f"'{channel_source.get('source')}' — "
                        f"{channel_source.get('error') or 'empty result'}. "
                        "Connect GA4, a warehouse, Amplitude, Mixpanel, PostHog, Adobe "
                        "Analytics, or at least one ad platform."
                    ),
                    "available_revenue_sources": available,
                    "revenue_source_requested": requested_source,
                }

            per_channel_ga4: dict[str, dict] = channel_source["per_channel"]

            # ── Spend per channel (aggregate from all_rows) ───────────
            spend_by_channel: dict[str, float] = {}
            for row in all_rows:
                ch = (row.get("platform") or "").lower()
                if ch == "google":
                    ch = "google_ads"
                spend_by_channel[ch] = spend_by_channel.get(ch, 0.0) + _safe_float(row.get("spend"))

            # ── Apply attribution model ───────────────────────────────
            # For first_touch / last_touch the source query already did the
            # work — we consume the per-channel revenue as-is.
            #
            # For linear / time_decay / position_based (approximation):
            # we weight by session share per channel — a proxy since we
            # don't have user-level paths. Flagged in the response.
            attribution_note = None
            per_channel_attributed: dict[str, float] = {}

            if chosen_model in ("first_touch", "last_touch"):
                for ch, data in per_channel_ga4.items():
                    per_channel_attributed[ch] = data["revenue"]
            else:
                # Approximation: normalize sessions and scale total revenue
                # by session share. This is NOT true multi-touch — it is a
                # best-effort under the Data API.
                total_sessions = sum(d["sessions"] for d in per_channel_ga4.values()) or 1.0
                total_revenue = sum(d["revenue"] for d in per_channel_ga4.values())
                for ch, data in per_channel_ga4.items():
                    weight = data["sessions"] / total_sessions
                    if chosen_model == "time_decay":
                        # Slight bias toward last-touch (revenue) blended
                        # 60/40 with session-share.
                        weight = 0.4 * weight + 0.6 * (data["revenue"] / (total_revenue or 1))
                    elif chosen_model == "position_based":
                        # 40% first + 40% last + 20% middle — without path
                        # data we collapse this to: 70/30 weight toward
                        # channels with both high sessions AND high revenue.
                        rev_share = data["revenue"] / (total_revenue or 1)
                        weight = 0.3 * weight + 0.7 * ((weight * rev_share) ** 0.5)
                    # linear: keep session-share as-is
                    per_channel_attributed[ch] = total_revenue * weight
                attribution_note = (
                    f"'{chosen_model}' is approximated from "
                    f"{channel_source['source']} session shares. "
                    "For true multi-touch paths, enable event-level "
                    "tracking (e.g. GA4 → BigQuery Export)."
                )

            # Suppress attributed revenue if source carries no revenue
            if channel_source["source"] == "spend_only":
                per_channel_attributed = {}

            # Optional filter
            if channels:
                keep = {c.lower() for c in channels}
                per_channel_attributed = {
                    k: v for k, v in per_channel_attributed.items() if k.lower() in keep
                }

            # ── Build breakdown rows (spend, revenue, ROAS, CAC) ──────
            is_spend_only = channel_source["source"] == "spend_only"
            breakdown = []
            for ch in sorted(set(per_channel_attributed) | set(spend_by_channel) | set(per_channel_ga4)):
                attributed_rev = per_channel_attributed.get(ch, 0.0)
                spend = spend_by_channel.get(ch, 0.0)
                conv = per_channel_ga4.get(ch, {}).get("conversions", 0.0)
                roas = (attributed_rev / spend) if spend > 0 and not is_spend_only else None
                cac = (spend / conv) if conv > 0 else None
                breakdown.append(
                    {
                        "channel": ch,
                        "spend": round(spend, 2),
                        "attributed_revenue": (round(attributed_rev, 2) if not is_spend_only else None),
                        "conversions": round(conv, 2),
                        "roas": round(roas, 2) if roas is not None else None,
                        "cac": round(cac, 2) if cac is not None else None,
                        "sessions": round(per_channel_ga4.get(ch, {}).get("sessions", 0.0), 0),
                    }
                )

            breakdown.sort(
                key=lambda r: r["attributed_revenue"] or 0.0,
                reverse=True,
            )

            ground_truth_rev = None
            ground_truth_src_name = None
            if total_source and total_source.get("success"):
                ground_truth_rev = total_source.get("total_revenue_ground_truth")
                ground_truth_src_name = total_source.get("source")

            totals = {
                "total_spend": round(sum(r["spend"] for r in breakdown), 2),
                "total_attributed_revenue": (
                    round(sum((r["attributed_revenue"] or 0.0) for r in breakdown), 2)
                    if not is_spend_only
                    else None
                ),
                "ground_truth_revenue": (
                    round(ground_truth_rev, 2) if ground_truth_rev is not None else None
                ),
                "ground_truth_source": ground_truth_src_name,
                "blended_roas": None,
            }
            if totals["total_spend"] > 0 and totals["total_attributed_revenue"]:
                totals["blended_roas"] = round(totals["total_attributed_revenue"] / totals["total_spend"], 2)

            # Warnings
            warnings: list[str] = []
            if channel_source["source"] == "ad_platforms_self_reported":
                warnings.append(
                    "Using ad-platform self-reported conversions — each "
                    "platform double-counts the same purchase. Totals are "
                    "inflated; treat as directional only."
                )
            if is_spend_only:
                warnings.append(
                    "No revenue source available. Only spend per channel "
                    "is returned; attributed revenue / ROAS are null."
                )
            if channel_source["confidence"] != "high":
                warnings.append(
                    f"Channel source confidence is "
                    f"'{channel_source['confidence']}' — "
                    f"{channel_source['notes']}"
                )

            return {
                "action": "revenue_attribution",
                "date_range": {"start": date_range_start, "end": date_range_end},
                "model": chosen_model,
                "attribution_window_days": attribution_window_days,
                "conversion_event": conversion_event,
                "platforms_queried": connected_names,
                "ga4_property_id": ga4_property_id,
                "revenue_source": channel_source["source"],
                "revenue_source_requested": requested_source,
                "revenue_source_confidence": channel_source["confidence"],
                "available_revenue_sources": available,
                "totals": totals,
                "breakdown": breakdown,
                "warehouse": {
                    "platform": warehouse_platform,
                    "table": revenue_table,
                    "order_id_column": order_id_column,
                    "revenue_column": revenue_column,
                    "error": (
                        total_source.get("error")
                        if total_source and total_source.get("source") == "warehouse"
                        else None
                    ),
                }
                if warehouse_platform
                else None,
                "notes": {
                    "model_note": attribution_note,
                    "channel_source_note": channel_source.get("notes"),
                    "ground_truth_note": (total_source.get("notes") if total_source else None),
                    "scope_note": (
                        "Channel-level attribution derived from "
                        f"{channel_source['source']}. Per-user path "
                        "reconstruction (true linear / time-decay / "
                        "position-based) and data-driven models require "
                        "event-level export."
                    ),
                },
                "warnings": warnings or None,
                "errors": errors if errors else None,
            }

        return {"error": True, "message": f"Unknown action: {action}"}
