"""Per-connector usage computed from the tool-call activity log.

Source of truth is ``tool_call_audit`` (the same table the /activity-log page
reads). Each row is one MCP tool call; this module attributes every row to the
connector it hit and aggregates, so the Connections page and the Settings ->
API limits tab can show real numbers rather than estimates.

Attribution (see :func:`resolve_connector`):

1. An explicit platform: the ``platform`` column, else ``arguments.params.platform``
   / ``arguments.params.engine`` / ``arguments.engine`` / ``arguments.platform``
   (the unified tools carry their target in ``params``).
2. Otherwise the tool family: ``tagmanager_*`` -> GTM (or Adobe Launch for the
   Launch-only actions), ``seo_*`` -> Search Console (or Bing for ``bing_*``
   actions), plus the legacy per-platform tool-name prefixes.

Calls that never reach a platform are excluded: ``describe`` (local spec
lookups) and ``denied`` (blocked by Fluxito before any upstream request).
Calls that can't be attributed (dashboards, scripts, knowledge, ...) are dropped.

Both page queries are a single grouped SQL statement filtered on the indexed
``project_id`` (and ``user_id``) columns plus ``created_at``.

Headroom (:func:`headroom_for`) compares the busiest fixed window in the last
30 days with the connector's published call-count limit for that window
(:data:`BUDGETS`, mirrored from ``app.connectors.rate_limits``). Sub-minute
limits are normalised to a per-minute rate (e.g. GTM's 25 req / 100 s ->
15 / min) because the log is bucketed per minute. Connectors whose binding
quota is not a call count (tokens, bytes, concurrency) get no headroom figure.
"""

from __future__ import annotations

import datetime as dt
import math
import uuid
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import func, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import ToolCallAudit

USAGE_DAYS = 30

# ---------------------------------------------------------------------------
# Attribution: raw platform string / tool name -> rate_limits.CATALOG key
# ---------------------------------------------------------------------------

_PLATFORM_ALIASES: dict[str, str] = {
    "ga4": "ga4",
    "google_analytics": "ga4",
    "gtm": "gtm",
    "google_tag_manager": "gtm",
    "adobe_launch": "adobe_launch",
    "launch": "adobe_launch",
    "adobe_analytics": "adobe_analytics",
    "adobe": "adobe_analytics",
    "google": "google_ads",
    "google_ads": "google_ads",
    "ads": "google_ads",
    "meta": "meta_ads",
    "meta_ads": "meta_ads",
    "facebook": "meta_ads",
    "tiktok": "tiktok_ads",
    "tiktok_ads": "tiktok_ads",
    "snap": "snap_ads",
    "snap_ads": "snap_ads",
    "snapchat": "snap_ads",
    "x": "x_ads",
    "x_ads": "x_ads",
    "twitter": "x_ads",
    "reddit": "reddit_ads",
    "reddit_ads": "reddit_ads",
    "pinterest": "pinterest_ads",
    "pinterest_ads": "pinterest_ads",
    "linkedin": "linkedin_ads",
    "linkedin_ads": "linkedin_ads",
    "apple": "apple_ads",
    "apple_ads": "apple_ads",
    "apple_search_ads": "apple_ads",
    "marketo": "marketo",
    "branch": "branch",
    "appsflyer": "appsflyer",
    "adjust": "adjust",
    "braze": "braze",
    "moengage": "moengage",
    "amplitude": "amplitude",
    "mixpanel": "mixpanel",
    "posthog": "posthog",
    "bigquery": "bigquery",
    "bq": "bigquery",
    "redshift": "redshift",
    "snowflake": "snowflake",
    "gsc": "search_console",
    "search_console": "search_console",
    "bing": "bing_webmaster",
    "bing_webmaster": "bing_webmaster",
    "data_manager": "data_manager",
}

# tagmanager_* actions that only exist on Adobe Launch (mirrors the
# ADOBE_LAUNCH_*_ACTIONS sets in app.tools.tagmanager_tools, which auto-switch
# the platform when the caller leaves it at the gtm default).
_LAUNCH_ONLY_ACTIONS = frozenset(
    {
        "list_companies",
        "list_properties",
        "get_property",
        "list_rules",
        "get_rule",
        "list_rule_components",
        "get_rule_component",
        "list_data_elements",
        "get_data_element",
        "list_extensions",
        "list_environments",
        "list_libraries",
        "list_builds",
        "create_property",
        "create_rule",
        "update_rule",
        "delete_rule",
        "create_rule_component",
        "update_rule_component",
        "delete_rule_component",
        "create_data_element",
        "update_data_element",
        "delete_data_element",
        "create_library",
        "add_resources_to_library",
        "build_library",
        "transition_library",
    }
)

# Legacy per-platform tool-name prefixes (pre-unified tools).
_LEGACY_PREFIXES: tuple[tuple[str, str], ...] = (
    ("gtm_", "gtm"),
    ("adwords_", "google_ads"),
    ("google_ads_", "google_ads"),
    ("bigquery_", "bigquery"),
    ("bq_", "bigquery"),
    ("redshift_", "redshift"),
    ("snowflake_", "snowflake"),
    ("meta_", "meta_ads"),
    ("tiktok_", "tiktok_ads"),
    ("snap_", "snap_ads"),
    ("amplitude_", "amplitude"),
    ("mixpanel_", "mixpanel"),
    ("posthog_", "posthog"),
    ("adobe_", "adobe_analytics"),
    ("search_console_", "search_console"),
    ("bing_webmaster_", "bing_webmaster"),
)

# Connections-page provider -> the catalog connectors one connection covers.
PROVIDER_CONNECTORS: dict[str, tuple[str, ...]] = {
    "google": ("ga4", "gtm", "search_console", "google_ads", "data_manager"),
    "meta": ("meta_ads",),
    "tiktok": ("tiktok_ads",),
    "snap": ("snap_ads",),
    "x": ("x_ads",),
    "reddit": ("reddit_ads",),
    "pinterest": ("pinterest_ads",),
    "linkedin": ("linkedin_ads",),
    "apple": ("apple_ads",),
    "bing": ("bing_webmaster",),
    "bigquery": ("bigquery",),
    "amplitude": ("amplitude",),
    "branch": ("branch",),
    "appsflyer": ("appsflyer",),
    "adjust": ("adjust",),
    "mixpanel": ("mixpanel",),
    "posthog": ("posthog",),
    "braze": ("braze",),
    "moengage": ("moengage",),
    "adobe": ("adobe_analytics", "adobe_launch"),
    "marketo": ("marketo",),
    "redshift": ("redshift",),
    "snowflake": ("snowflake",),
}


def resolve_connector(tool_name: str | None, raw_platform: str | None, action: str | None) -> str | None:
    """Map one logged call to a ``rate_limits.CATALOG`` key, or None."""
    tool = (tool_name or "").lower()
    act = (action or "").lower()
    plat = (raw_platform or "").strip().lower()
    if plat:
        key = _PLATFORM_ALIASES.get(plat)
        if key == "gtm" and tool.startswith("tagmanager_") and act in _LAUNCH_ONLY_ACTIONS:
            return "adobe_launch"
        return key
    if tool.startswith("tagmanager_"):
        return "adobe_launch" if act in _LAUNCH_ONLY_ACTIONS else "gtm"
    if tool.startswith("seo_"):
        return "bing_webmaster" if act.startswith("bing_") else "search_console"
    for prefix, key in _LEGACY_PREFIXES:
        if tool.startswith(prefix):
            return key
    return None


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------


def _raw_platform_expr():
    args = ToolCallAudit.arguments
    return func.coalesce(
        func.nullif(ToolCallAudit.platform, ""),
        args["params"]["platform"].astext,
        args["params"]["engine"].astext,
        args["engine"].astext,
        args["platform"].astext,
    )


def _base_filters(project_id: uuid.UUID, user_id: uuid.UUID | None) -> list:
    action = ToolCallAudit.arguments["action"].astext
    filters = [
        ToolCallAudit.project_id == project_id,
        ToolCallAudit.status != "denied",
        or_(action.is_(None), action != "describe"),
    ]
    if user_id is not None:
        filters.append(ToolCallAudit.user_id == user_id)
    return filters


def _utcnow() -> dt.datetime:
    # tool_call_audit.created_at is a naive UTC timestamp (server_default now()).
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


@dataclass
class ConnectorUsage:
    """Aggregated calls for one connector."""

    calls: int = 0  # calls in the last ``USAGE_DAYS`` days
    last_used: dt.datetime | None = None  # most recent call (any age)
    # minute bucket (epoch seconds, UTC) -> calls; only filled by usage_by_minute
    minutes: dict[int, int] = field(default_factory=dict)


async def usage_summary(
    db: AsyncSession,
    project_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    days: int = USAGE_DAYS,
    now: dt.datetime | None = None,
) -> dict[str, ConnectorUsage]:
    """30-day call count + last-used time per connector (one grouped query)."""
    since = (now or _utcnow()) - dt.timedelta(days=days)
    raw = _raw_platform_expr().label("raw_platform")
    action = ToolCallAudit.arguments["action"].astext.label("action")
    stmt = (
        select(
            ToolCallAudit.tool_name,
            raw,
            action,
            func.count().filter(ToolCallAudit.created_at >= since).label("calls"),
            func.max(ToolCallAudit.created_at).label("last_used"),
        )
        .where(*_base_filters(project_id, user_id))
        .group_by(ToolCallAudit.tool_name, literal_column("raw_platform"), literal_column("action"))
    )
    out: dict[str, ConnectorUsage] = {}
    for tool_name, raw_platform, act, calls, last_used in (await db.execute(stmt)).all():
        key = resolve_connector(tool_name, raw_platform, act)
        if key is None:
            continue
        u = out.setdefault(key, ConnectorUsage())
        u.calls += int(calls or 0)
        if last_used is not None and (u.last_used is None or last_used > u.last_used):
            u.last_used = last_used
    return out


async def usage_by_minute(
    db: AsyncSession,
    project_id: uuid.UUID,
    days: int = USAGE_DAYS,
    now: dt.datetime | None = None,
) -> dict[str, ConnectorUsage]:
    """Per-connector calls bucketed per minute over the last ``days`` (one grouped query)."""
    since = (now or _utcnow()) - dt.timedelta(days=days)
    raw = _raw_platform_expr().label("raw_platform")
    action = ToolCallAudit.arguments["action"].astext.label("action")
    minute = func.floor(func.extract("epoch", ToolCallAudit.created_at) / 60).label("minute")
    stmt = (
        select(
            ToolCallAudit.tool_name,
            raw,
            action,
            minute,
            func.count().label("calls"),
            func.max(ToolCallAudit.created_at).label("last_used"),
        )
        .where(*_base_filters(project_id, None), ToolCallAudit.created_at >= since)
        .group_by(
            ToolCallAudit.tool_name,
            literal_column("raw_platform"),
            literal_column("action"),
            literal_column("minute"),
        )
    )
    out: dict[str, ConnectorUsage] = {}
    for tool_name, raw_platform, act, minute_idx, calls, last_used in (await db.execute(stmt)).all():
        key = resolve_connector(tool_name, raw_platform, act)
        if key is None:
            continue
        u = out.setdefault(key, ConnectorUsage())
        n = int(calls or 0)
        u.calls += n
        m = int(minute_idx) * 60
        u.minutes[m] = u.minutes.get(m, 0) + n
        if last_used is not None and (u.last_used is None or last_used > u.last_used):
            u.last_used = last_used
    return out


def combine(usage: dict[str, ConnectorUsage], keys: tuple[str, ...] | list[str]) -> ConnectorUsage:
    """Sum several connectors (e.g. the Google services behind one sign-in)."""
    total = ConnectorUsage()
    for k in keys:
        u = usage.get(k)
        if not u:
            continue
        total.calls += u.calls
        if u.last_used is not None and (total.last_used is None or u.last_used > total.last_used):
            total.last_used = u.last_used
    return total


# ---------------------------------------------------------------------------
# Headroom against published call-count limits
# ---------------------------------------------------------------------------

MINUTE = 60
HOUR = 3600
DAY = 86400


@dataclass(frozen=True)
class Budget:
    """A published call-count limit expressed over a window we can measure."""

    limit: int
    window_s: int  # a multiple of 60 (the log is bucketed per minute)
    source: str = ""  # the catalog limit it comes from, when normalised


@dataclass(frozen=True)
class Unmetered:
    """Why a connector has no call-count headroom figure."""

    reason: str


# Mirrors app.connectors.rate_limits.CATALOG (tests keep the keys in sync).
# Several budgets -> the one with the highest utilisation is shown.
BUDGETS: dict[str, tuple[Budget, ...] | Unmetered] = {
    "ga4": Unmetered("quota is token-based"),
    "gtm": (Budget(15, MINUTE, "25 req / 100 s"), Budget(10_000, DAY)),
    "bigquery": Unmetered("quota is bytes scanned"),
    "google_ads": (Budget(15_000, DAY),),
    "search_console": (Budget(200, MINUTE),),
    "data_manager": Unmetered("per-project Cloud quota"),
    "meta_ads": Unmetered("limit scales with active ads"),
    "tiktok_ads": (Budget(600, MINUTE, "~10 req / s"),),
    "snap_ads": (Budget(600, MINUTE, "10 req / s"),),
    "linkedin_ads": Unmetered("call limits not published"),
    "pinterest_ads": (Budget(300, MINUTE),),
    "x_ads": (Budget(250, 15 * MINUTE),),
    "reddit_ads": (Budget(100, MINUTE),),
    "apple_ads": (Budget(300, MINUTE),),
    "bing_webmaster": Unmetered("limit covers URL submission only"),
    "adobe_analytics": (Budget(120, MINUTE, "12 req / 6 s"),),
    "adobe_launch": (Budget(120, MINUTE),),
    "marketo": (Budget(300, MINUTE, "100 calls / 20 s"), Budget(50_000, DAY)),
    "braze": (Budget(600, MINUTE, "~10 req / s"), Budget(250_000, HOUR)),
    "moengage": (Budget(1_000, MINUTE), Budget(1_000_000, DAY)),
    "amplitude": Unmetered("quota is query cost"),
    "branch": (Budget(50, MINUTE),),
    "appsflyer": (Budget(100, MINUTE),),
    "adjust": (Budget(170, MINUTE),),
    "mixpanel": (Budget(60, MINUTE),),
    "posthog": (Budget(240, MINUTE),),
    "redshift": Unmetered("limit is concurrency"),
    "snowflake": Unmetered("limit is concurrency"),
}


def window_name(window_s: int) -> str:
    if window_s == MINUTE:
        return "minute"
    if window_s == HOUR:
        return "hour"
    if window_s == DAY:
        return "day"
    if window_s % HOUR == 0:
        return f"{window_s // HOUR} h"
    return f"{window_s // MINUTE} min"


def _window_unit(window_s: int) -> str:
    return {MINUTE: "min", HOUR: "hour", DAY: "day"}.get(window_s, window_name(window_s))


def peak_in_window(minutes: dict[int, int], window_s: int) -> int:
    """Largest call count in any fixed (UTC-aligned) window of ``window_s``."""
    if not minutes:
        return 0
    if window_s <= MINUTE:
        return max(minutes.values())
    buckets: dict[int, int] = defaultdict(int)
    for ts, n in minutes.items():
        buckets[ts // window_s] += n
    return max(buckets.values())


def headroom_for(connector_key: str, usage: ConnectorUsage | None) -> dict:
    """Template-ready usage + headroom view for one catalog connector."""
    calls = usage.calls if usage else 0
    minutes = usage.minutes if usage else {}
    view: dict = {"calls": calls, "days": USAGE_DAYS, "metered": False}
    spec = BUDGETS.get(connector_key)
    if not isinstance(spec, tuple) or not spec:
        view["reason"] = spec.reason if isinstance(spec, Unmetered) else "no call-count limit"
        return view

    best: tuple[float, Budget, int] | None = None
    for b in spec:
        peak = peak_in_window(minutes, b.window_s)
        util = peak / b.limit
        if best is None or util > best[0]:
            best = (util, b, peak)
    assert best is not None
    util, budget, peak = best
    used_pct = min(100.0, util * 100)
    # Floor the headroom so any real usage never reads as a perfect 100%.
    headroom_pct = max(0, math.floor(100 - used_pct)) if peak else 100
    view.update(
        {
            "metered": True,
            "peak": peak,
            "limit": budget.limit,
            "window": window_name(budget.window_s),
            "limit_label": f"{budget.limit:,} / {_window_unit(budget.window_s)}",
            "limit_source": budget.source,
            "headroom_pct": headroom_pct,
            "used_pct": round(used_pct, 1),
            "level": "bad" if headroom_pct < 10 else ("warn" if headroom_pct < 25 else "ok"),
        }
    )
    return view


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def relative_time(ts: dt.datetime | None, now: dt.datetime | None = None) -> str | None:
    """Compact "2h ago" style label for a naive-UTC timestamp."""
    if ts is None:
        return None
    now = now or _utcnow()
    secs = max(0, int((now - ts).total_seconds()))
    if secs < 60:
        return "just now"
    if secs < HOUR:
        return f"{secs // 60}m ago"
    if secs < DAY:
        return f"{secs // HOUR}h ago"
    days = secs // DAY
    if days < 60:
        return f"{days}d ago"
    return ts.strftime("%b %-d, %Y")
