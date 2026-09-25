"""
Spec registry — the single source of truth for MCP tool action/param specs.

Each ``data/<tool>.json`` file holds the curated specs for one public tool. They
are loaded into :class:`~app.tools.spec_engine.ActionSpec` objects at import and
consumed by ``apply_specs`` (in ``app/tools/unified.py``) to generate the served
description, input schema, ``describe`` payload, and error envelopes.

To cover a new tool: add ``data/<tool>.json`` (seed it from the Phase-1 truth
tables) and, optionally, a HEADER/FOOTER below. The drift-guard test
(``tests/test_tool_specs.py``) then enforces that the registry matches the tool's
routes and that every routed action is reachable.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.tools.spec_engine import ActionSpec, Param

_DATA = Path(__file__).parent / "data"


def _load_param(p: dict) -> Param:
    return Param(
        name=p["name"],
        type=p.get("type", "string"),
        required=bool(p.get("required", False)),
        enum=tuple(p["enum"]) if p.get("enum") else None,
        item_type=p.get("item_type"),
        example=p.get("example"),
        doc=p.get("doc", ""),
        platforms=tuple(p["platforms"]) if p.get("platforms") else None,
    )


def _load_spec(r: dict) -> ActionSpec:
    return ActionSpec(
        tool=r["tool"],
        action=r["action"],
        summary=r.get("summary", ""),
        params=tuple(_load_param(p) for p in r.get("params", [])),
        platforms=tuple(r["platforms"]) if r.get("platforms") else None,
        returns=r.get("returns", ""),
        scope=r.get("scope"),
        mutates=bool(r.get("mutates", False)),
        reversible=r.get("reversible"),
        example=r.get("example"),
        group=r.get("group", ""),
    )


def _load_all() -> dict[str, list[ActionSpec]]:
    out: dict[str, list[ActionSpec]] = {}
    for f in sorted(_DATA.glob("*.json")):
        records = json.loads(f.read_text())
        out[f.stem] = [_load_spec(r) for r in records]
    return out


#: tool -> [ActionSpec]
SPECS: dict[str, list[ActionSpec]] = _load_all()

#: Per-tool description header (the summary + sibling-tool routing rules).
HEADERS: dict[str, str] = {
    "audience_read": (
        "Read Google Data Manager audiences (UserLists). Pass parent or account_type + account_id "
        "for list operations, and name for a single audience. Requires the datamanager OAuth scope."
    ),
    "audience_write": (
        "Create and manage Google Data Manager audiences and audience members. Member identifiers "
        "must follow Google's hashing and consent requirements; use validate_only for new batches."
    ),
    "analytics_read": (
        "Read product / web analytics across GA4, Amplitude, and Adobe Analytics.\n"
        "ALWAYS pass `platform` ('ga4' | 'amplitude' | 'adobe_analytics') in params. "
        "Each action is valid only for the platform(s) shown in [brackets] — calling "
        "an action on the wrong platform returns an 'unknown action' error.\n"
        "For audits / anomaly checks / tracking regressions use `run_audit`; for "
        "cross-platform blends / attribution use `run_analysis`.\n"
        "Adobe Analysis Workspace actions are explicitly prefixed `adobe_workspace_` "
        "and listed under ADOBE WORKSPACE. Create with config.tables "
        "([{metrics, dimension?}]) — Fluxito builds the Workspace JSON. "
        "Do not invent a raw `definition`.\n"
        'Pass metrics/dimensions as plain strings (e.g. ["sessions","country"]); the '
        'GA4 object form [{"name":"sessions"}] is also accepted and coerced.'
    ),
    "run_audit": (
        "Run a canonical audit / health-check / anomaly-detection action across any "
        "connected platform. Action names are platform-prefixed and globally "
        "unambiguous (e.g. ga4_audit_data_streams vs warehouse_audit_dataset).\n"
        "Heavier than the domain *_read tools (longer timeouts, different billing) — "
        "for simple catalog reads use analytics_read / tagmanager_read / etc.\n"
        "Note: warehouse audits need `engine` in params; the two `marketing_audit_*` "
        "actions need `platform` in params."
    ),
    "analytics_write": (
        "Create / update / delete analytics definitions — GA4 audiences, custom "
        "dimensions/metrics, conversion events; Adobe segments, calculated metrics, "
        "and Analysis Workspace projects.\n"
        "Pass `platform` in params. Mutating — requires the analytics write scope. "
        "Adobe Analysis Workspace actions are explicitly prefixed `adobe_workspace_` "
        "and listed under ADOBE WORKSPACE. Prefer adobe_workspace_create_project with "
        "config.tables (not a hand-written definition). "
        "Use analytics_read for reads, run_audit for audits."
    ),
    "tagmanager_read": (
        "Read the tag-manager catalog (GTM + Adobe Launch). Pass `platform` "
        "('gtm' | 'adobe_launch') when both are connected.\n"
        "ADOBE LAUNCH reuses GTM param NAMES with different meanings: `container_id` = "
        "Launch property_id, `tag_id` = rule_id, `workspace_id` = library_id, "
        "`account_id` = company_id. For audits use run_audit (gtm_audit_*, "
        "adobe_launch_*)."
    ),
    "tagmanager_write": (
        "Mutate the tag manager (GTM + Adobe Launch). Pass `platform`. Requires the "
        "tagmanager write scope; publish_container also needs the publish scope.\n"
        "`propose_change` is a SAFE dry-run. A GTM tag created with no "
        "firing_trigger_ids never fires. Adobe Launch uses the same overloaded param "
        "names as tagmanager_read."
    ),
    "marketing_read": (
        "Read paid-ads performance (Google, Meta, TikTok, Snap — also LinkedIn, "
        "Pinterest, X, Reddit, Apple) and Adobe Marketo Engage. ALWAYS pass "
        "`platform`. Marketo actions are the `marketo_*`-prefixed ones.\n"
        "For spend/budget audits use run_audit; for cross-platform blends use "
        "run_analysis."
    ),
    "marketing_write": (
        "Mutate paid-ads campaigns (budget / status / create) and Adobe Marketo "
        "(lead upsert, list membership, campaign requests). Pass `platform`.\n"
        "create_campaign is now available on ALL 9 platforms (Google, Meta, TikTok, "
        "Snap, LinkedIn, Pinterest, X, Reddit, Apple). update_campaign_budget is also "
        "available on all 9. Use advertising_channel_type for platform-specific "
        "objective/channel type.\n"
        "WARNING: budget/status changes affect LIVE ad spend immediately. The budget "
        "field is `daily_budget_usd` (not `new_budget`)."
    ),
    "warehouse_read": (
        "Read warehouse schema / metadata (BigQuery, Redshift, Snowflake). ALWAYS "
        "pass `engine`. `dataset_id` is the dataset (BigQuery) or the schema "
        "(Redshift/Snowflake).\n"
        "To RUN SQL use warehouse_query; for data-quality / health audits use "
        "run_audit (warehouse_*)."
    ),
    "seo_read": (
        "Read organic-search data: Google Search Console (bare or `gsc_`-prefixed "
        "actions — identical handlers) and Bing Webmaster Tools (`bing_`-prefixed).\n"
        "`site_url` uses GSC's form, e.g. `sc-domain:example.com` or a full URL. For "
        "audits (top movers, striking distance, CTR outliers, sitemap health) use "
        "run_audit (seo_*)."
    ),
    "seo_write": (
        "Submit or delete a sitemap in Google Search Console. Requires the Search "
        "Console write scope. `delete_sitemap` is IRREVERSIBLE."
    ),
    "get_knowledge": (
        "Read the project's knowledge base — KPI library and business context. Call "
        "early so your terminology and metrics match the client.\nFlow: list_kpis (discover) → get_kpi (definition) → compute_kpi "
        "(current value)."
    ),
    "run_analysis": (
        "Cross-connector computed insights — blended performance across ad platforms, "
        "channel comparison, top campaigns, and revenue attribution (spend + GA4 "
        "touches + warehouse revenue).\n"
        "Param names are `date_range_start` / `date_range_end` / `limit` (NOT "
        "start_date / end_date / n). For single-platform reads use the domain *_read "
        "tools."
    ),
}

#: Per-tool description footer (return-shape note).
FOOTERS: dict[str, str] = {
    "analytics_read": "Returns: { rows / data / items: [...], ... } or the standard error envelope.",
    "run_audit": (
        "Returns: { status, findings: [{severity, code, message, evidence, "
        "recommendation}], ... } or the standard error envelope."
    ),
}

#: Per-tool served-schema encoding. 'flat' keeps the {action, params} envelope and
#: types `params` as the documented superset of every action's params (strict-safe).
#: 'union' (per-action discriminated) is a Phase-3 candidate, gated by the
#: conformance test.
ENCODING: dict[str, str] = {}


def has_tool(tool: str) -> bool:
    return tool in SPECS


def specs_for(tool: str) -> list[ActionSpec]:
    return SPECS.get(tool, [])


def header_for(tool: str) -> str:
    return HEADERS.get(tool, "")


def footer_for(tool: str) -> str:
    return FOOTERS.get(tool, "")


def encoding_for(tool: str) -> str:
    return ENCODING.get(tool, "flat")


__all__ = [
    "ENCODING",
    "FOOTERS",
    "HEADERS",
    "SPECS",
    "encoding_for",
    "footer_for",
    "has_tool",
    "header_for",
    "specs_for",
]
