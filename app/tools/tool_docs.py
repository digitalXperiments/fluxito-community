"""
Companion documentation for MCP tools.

Tool docstrings are sent to the LLM on EVERY session as part of the tool
list, so they're a major token cost. We keep the per-tool docstrings short
(~500 chars) and move the detailed reference docs here, exposed via
``get_session_context(tool_name=...)`` that the LLM can call on demand.

Docs are keyed by tool name. Callers that need the detailed reference
(e.g. when assembling a dashboard) request the doc explicitly instead of
paying the token cost on every session.
"""

from __future__ import annotations

WAREHOUSE_QUERY_DOC = """\
warehouse_query — detailed reference
══════════════════════════════════════════════════════════════════════

Executes SQL against BigQuery / Redshift / Snowflake. SELECT only,
capped at max_results=5000.

── Chunked / paginated results ──
For result sets > 100 rows, responses include a _chunk dict:
  {"chunk_index": 0, "chunk_rows": 500, "total_rows": 1342,
   "next_offset": 500, "has_more": true}
Call again with offset=next_offset to fetch the next chunk. This lets
Claude process the first rows while the rest streams.

── Parameter names ──
  engine:         "bigquery" | "redshift" | "snowflake" (required)
  query:          SQL to execute (SELECT only)
  dataset_id:     dataset/schema qualifier (used by preview_table etc.)
  table_id:       table name (used by preview_table)
  connection_id:  usually inferred for projects with a single connection

── Actions per engine ──
All:       run_query, preview_table
BigQuery:  + dry_run (cost estimate)
Redshift:  + explain_query
Snowflake: + explain_query

── Smart defaults ──
- If omitted, start_date/end_date default to last 30 days.
- If the project has a single connected connection, connection_id is
  inferred. Same for dataset_id when a project has a single dataset.
"""

CROSS_PLATFORM_REPORT_DOC = """\
cross_platform_report — detailed reference
══════════════════════════════════════════════════════════════════════

Blended reporting across all connected ad platforms (Google Ads, Meta,
TikTok, Snap). All platform fetches run in parallel via asyncio.gather —
total latency ≈ slowest platform, not sum of platforms.

Actions:
  blended_performance — totals + per-platform breakdown + blended KPIs
  channel_comparison  — side-by-side comparison table with spend_share_pct
  top_campaigns       — top N campaigns sorted by spend|roas|conversions|
                        clicks|cpa|impressions|revenue

Dates default to last 30 days if omitted. Normalized metrics:
impressions, clicks, spend, conversions, revenue. Derived: CTR, CPC,
CPA, ROAS.
"""


RUN_AUDIT_DOC = """\
run_audit — detailed reference
══════════════════════════════════════════════════════════════════════

Single MCP entry point for every audit / health check / anomaly detection
action across every connected platform. Action names are platform-prefixed
so they are globally unambiguous (e.g. ga4_audit_property vs.
warehouse_audit_dataset).

Why separate from the domain read tools:
  • Audits are heavier (multi-call, sometimes multi-minute) than catalog
    reads. A dedicated tool lets us apply longer timeouts and different
    billing weight without penalising cheap reads.
  • Smaller docstrings on analytics_read / tagmanager_read / marketing_read
    / warehouse_read / seo_read → fewer tokens on every session.
  • Clearer selection signal for the model: cheap reads vs. computed audits.

── Invocation ──
  run_audit(action="ga4_audit_data_streams", params={"property_id": "123456"})
  run_audit(action="gtm_audit_consent_mode",
            params={"account_id": "...", "container_id": "..."})
  run_audit(action="warehouse_check_data_quality",
            params={"engine": "bigquery", "dataset_id": "ecom",
                    "table_id": "orders"})
  run_audit(action="seo_top_movers",
            params={"site_url": "sc-domain:example.com",
                    "start_date": "2026-03-01", "end_date": "2026-03-31",
                    "compare_start_date": "2026-02-01",
                    "compare_end_date": "2026-02-28"})

── Actions by domain ──

PRODUCT ANALYTICS (GA4 / Amplitude / Adobe Analytics)
  ga4_audit_data_streams                     params: property_id
  ga4_audit_conversion_events                params: property_id
  ga4_audit_custom_definitions               params: property_id
  ga4_audit_ecommerce                        params: property_id
  ga4_schema_validator                       params: property_id
  ga4_check_data_anomalies                   params: property_id, metric, lookback_days
  amplitude_check_taxonomy_health            params: project_id
  amplitude_check_event_volume_anomalies     params: project_id, lookback_days
  adobe_audit_report_suite                   params: report_suite_id
  adobe_check_data_quality                   params: report_suite_id

TAG MANAGER (GTM / Adobe Launch)
  gtm_audit_container        params: account_id, container_id
  gtm_explain_tag            params: account_id, container_id, tag_id
  gtm_dependency_map         params: account_id, container_id
  gtm_simulate_event         params: account_id, container_id, event_name
  gtm_check_ga4_implementation   params: account_id, container_id
  gtm_find_tracking_regression   params: account_id, container_id, lookback_days
  gtm_diagnose_conversion_discrepancy  GA4 vs. Ads mismatch
  gtm_generate_audit_report  composite roll-up
  gtm_suggest_improvements   composite recommendations
  gtm_benchmark_health       params: account_id, container_id
  adobe_launch_audit_property     params: container_id (Launch property_id)
  adobe_launch_get_publish_history params: container_id
  gtm_audit_consent_mode     Consent Mode v2, cookie banner, GDPR/CCPA
                             signal compliance.
                             params: account_id, container_id,
                             site_url (optional, for cookie-banner probe),
                             region (eea|uk|us|all)
  adobe_audit_consent_mode   Adobe Launch consent / CMP heuristic check.
                             Looks for CMP extension, AEP Web SDK (alloy),
                             consent-named data elements, and consent-
                             gated rules. Heuristic only — Launch API
                             does not expose per-rule consent conditions.
                             params: container_id (Launch property_id)

PAID MARKETING (Google Ads / Meta / TikTok / Snap)
  marketing_audit_budget_utilization  pacing + spend efficiency
  marketing_audit_quality_scores      Google Ads quality-score drill-down
  marketing_connection_health         account-link + data-freshness check

WAREHOUSE (BigQuery / Redshift / Snowflake)
  warehouse_audit_dataset           params: engine, dataset_id
  warehouse_audit_schema            params: engine, database, schema
  warehouse_find_stale_tables       params: engine, dataset_id, days_threshold
  warehouse_check_table_health      params: engine=redshift, dataset_id, table_id
                                    (Redshift only)
  warehouse_check_data_quality      nulls/dupes/cardinality.
                                    params: engine, dataset_id, table_id,
                                    columns[] (optional)
  warehouse_check_empty_tables      params: engine, dataset_id
  warehouse_check_clustering_health BQ partitioning / Redshift sort-dist
                                    params: engine, dataset_id

SEO / SEARCH CONSOLE (Google only — Bing audits not yet implemented)
  seo_top_movers               params: site_url, start_date, end_date,
                                       compare_start_date, compare_end_date,
                                       dimension (query|page), limit
  seo_striking_distance        params: site_url, start_date, end_date,
                                       min_impressions, limit
  seo_ctr_outliers             params: site_url, start_date, end_date,
                                       dimension, min_impressions, limit
  seo_sitemap_health           params: site_url
  seo_gsc_ga4_cross_reference  params: site_url, start_date, end_date,
                                       ga4_property_id, limit

── Return shape ──
  Standard: { status, findings: [{severity, code, message, evidence, ...}],
              recommendations[], ... }
  On error: { error, error_type, message }
"""


RUN_ANALYSIS_DOC = """\
run_analysis — detailed reference
══════════════════════════════════════════════════════════════════════

Single MCP entry point for cross-connector composite analyses — blended
reporting, attribution modeling, incrementality. These are not simple reads;
they join data from multiple platforms, often with identity stitching and
non-trivial math. Routing them through their own tool keeps the domain read
tools lean and lets us apply a longer default timeout here.

── Invocation ──
  run_analysis(action="blended_performance",
               params={"start_date": "2026-03-01",
                       "end_date": "2026-03-31",
                       "platforms": ["google_ads", "meta", "tiktok"]})

  run_analysis(action="revenue_attribution",
               params={"start_date": "2026-03-01",
                       "end_date": "2026-03-31",
                       "model": "time_decay",
                       "attribution_window_days": 30,
                       "conversion_event": "purchase",
                       "order_id_column": "order_id",
                       "ga4_property_id": "123456",
                       "warehouse_platform": "bigquery",
                       "revenue_table": "ecom.orders"})

── Actions ──

  cross_platform_report   Generic blended report. If `action` is set inside
                          `params`, it wins (pass-through to the underlying
                          tool for backwards compatibility).
                          params: start_date, end_date,
                                  platforms[] (ga4|google_ads|meta|tiktok|snap),
                                  dimensions[], metrics[], granularity.

  blended_performance     Totals + per-platform breakdown + blended KPIs
                          (impressions, clicks, spend, conversions, revenue,
                          CTR, CPC, CPA, ROAS).

  channel_comparison      Side-by-side table with spend_share_pct,
                          per-channel KPIs, and relative deltas.

  top_campaigns           Top N campaigns across all connected ad platforms.
                          params: n (default 20),
                                  sort_by (spend|roas|conversions|clicks|cpa|
                                           impressions|revenue).

  revenue_attribution     Multi-touch attribution. Resolves a revenue source
                          adaptively so customers without a warehouse (and
                          even without GA4) still get a useful answer.

                          Revenue-source hierarchy (auto):
                            warehouse → ga4 → adobe_analytics → amplitude
                            → ad_platforms_self_reported → spend_only
                          Override via `revenue_source` (same values, plus
                          "auto"). Warehouse and Amplitude are TOTAL-only
                          sources; when picked, the channel split layers on
                          from GA4 / Adobe / self-reported beneath.

                          params: start_date, end_date,
                                  model (first_touch|last_touch|linear|
                                         time_decay|position_based),
                                  attribution_window_days (default 30),
                                  conversion_event (GA4 event name),
                                  channels[] (optional filter),
                                  revenue_source (optional override),
                                  ga4_property_id (required for ga4 source),
                                  warehouse_platform, revenue_table,
                                  revenue_column, order_id_column,
                                  amplitude_project_id,
                                  adobe_report_suite_id, adobe_org_id.

                          Response includes `revenue_source` (resolved),
                          `revenue_source_confidence` (high|medium|low),
                          `available_revenue_sources[]`, and `warnings[]`
                          when self-reported or spend_only is used.

── Design notes ──
  • Identity stitching: order_id in warehouse ↔ transaction_id in GA4 is
    the default join. Fall back to user_pseudo_id + hashed-email if the
    client has set that up.
  • Ground-truth revenue: warehouse wins whenever both warehouse and a
    channel source are present; warehouse total is shown alongside the
    attributed breakdown.
  • Self-reported (ad platform) revenue DOUBLE-COUNTS conversions across
    platforms; it is a last-resort proxy and surfaced with a warning.
  • spend_only is returned when no revenue source exists; attributed
    revenue / ROAS are null in that case.
  • Mixpanel is not yet supported (no connector).
  • Data-driven attribution (Shapley / Markov) is NOT supported as a sync
    call — for those, enqueue a background job via the scheduler and
    poll the result.

── Return shape ──
  { rows: [...] | breakdown: {...}, totals: {...}, model_metadata: {...} }
  On error: { error, error_type, message }
"""


RUN_SCRIPT_DOC = """\
run_script — detailed reference
══════════════════════════════════════════════════════════════════════

Execute a Python snippet server-side that composes multiple tool calls
in a single round-trip. Use when the user's question would otherwise
force you through 3+ sequential tool calls — fan-out over entities,
cross-connector composition, or filtering/aggregation before returning.

── When to use it ─────────────────────────────────────────────────────
  ✓ "Audit all my GTM containers and show only the broken ones."
  ✓ "Rank campaigns across every ad platform by ROAS, last 30 days."
  ✓ "Find GA4 events that dropped >50% vs last week."
  ✓ "For each audience, get its size and its top conversion event."
  ✓ Any task where Claude would otherwise make N similar tool calls
    and you only need a filtered/aggregated summary back.

── When NOT to use it ────────────────────────────────────────────────
  ✗ A single tool call — call the tool directly, scripts add overhead.
  ✗ Exploring an unknown response shape — call the tool once, inspect
    it, then write a script. Don't guess shapes.
  ✗ When a pre-built composite tool already covers it (e.g.
    `run_audit(action='gtm_audit_container', ...)` for one container).

── Script environment ────────────────────────────────────────────────
Inside the script you have:

  await call(tool_name: str, params: dict) -> dict
      Invoke any tool that appears in get_session_context().
      Tools return dicts — errors come back as {'error': True, ...}
      rather than raising. Check `result.get('error')` if you want to
      skip failed items.

  await gather([awaitable, ...]) -> list
      Run awaitables in parallel. Use this liberally — it's the whole
      point of scripting: parallel fan-out.

  RESULT = <final value>
      Assign here whatever you want returned. Must be JSON-serializable
      (no sets, no custom objects). Unassigned → returns None.

  print(...)
      Captured and returned under `stdout` — useful for debugging when
      a script returns unexpected output.

Safe builtins available: abs, all, any, bool, dict, divmod, enumerate,
filter, float, frozenset, int, len, list, map, max, min, range, reversed,
round, set, slice, sorted, str, sum, tuple, zip, isinstance, issubclass.

── What's forbidden ──────────────────────────────────────────────────
- `import` / `from ... import ...` — no module access
- `open`, `eval`, `exec`, `compile`, `getattr`, `setattr`, `__import__`
- `try`/`except`/`raise` — check return dicts instead of using exceptions
- `class` definitions
- `with` blocks (file handles)
- Any attribute starting with `_` (blocks dunder escape paths)

These limits exist to keep the sandbox tight. If a script needs
something not in the whitelist, break the task into a direct tool
call plus a script, or request a new L2 tool.

── Budgets ───────────────────────────────────────────────────────────
- 30s wall-clock (60s hard cap if you pass timeout_seconds)
- 50 inner `call()` invocations max per script
- 256KB serialized result (filter/aggregate if larger)
- 8KB stdout buffer

── Return shape ──────────────────────────────────────────────────────
Success:
  {
    "result": <whatever RESULT was>,
    "tool_calls_made": 12,
    "duration_ms": 2340,
    "trace": [{"tool": "...", "action": "...", "duration_ms": N}, ...],
    "stdout": "..." | null
  }

Errors (script didn't complete):
  { "error": True, "error_type": "timeout" | "invalid_script" |
                   "output_too_large" | "RuntimeError" | ...,
    "message": "...",
    "tool_calls_made": N, "trace": [...], "stdout": "..." }

── Example 1: audit every GTM container, return only broken ones ────
```
# Discovery + fan-out + filter — all in one hop
containers_resp = await call("tagmanager_read", {"action": "list_containers"})
containers = containers_resp.get("containers", [])

audits = await gather([
    call("run_audit", {
        "action": "gtm_audit_container",
        "params": {"container_id": c["id"]},
    })
    for c in containers
])

broken = []
for c, a in zip(containers, audits):
    if a.get("error"):
        continue
    score = a.get("health_score", 100)
    if score < 80:
        broken.append({
            "container": c["name"],
            "score": score,
            "top_issues": (a.get("issues") or [])[:5],
        })

RESULT = sorted(broken, key=lambda x: x["score"])
```

── Example 2: cross-platform ROAS leaderboard, last 30 days ──────────
```
platforms = ["google_ads", "meta", "tiktok", "snap"]

reports = await gather([
    call("marketing_read", {
        "action": "campaign_performance",
        "params": {
            "platform": p,
            "start_date": "30daysAgo",
            "end_date": "today",
        },
    })
    for p in platforms
])

rows = []
for p, r in zip(platforms, reports):
    if r.get("error"):
        print(f"skipped {p}: {r.get('message')}")
        continue
    for c in r.get("campaigns", []):
        cost = c.get("cost", 0)
        if cost > 100:  # ignore rounding-error campaigns
            rows.append({
                "platform": p,
                "campaign": c.get("name"),
                "cost": cost,
                "roas": round(c.get("revenue", 0) / cost, 2),
            })

RESULT = sorted(rows, key=lambda x: x["roas"], reverse=True)[:20]
```

── Example 3: GA4 event regression detector ──────────────────────────
```
props_resp = await call("analytics_read", {"action": "list_properties"})
prop_id = props_resp["properties"][0]["id"]

this_week, last_week = await gather([
    call("analytics_read", {
        "action": "list_events",
        "params": {
            "platform": "ga4",
            "property_id": prop_id,
            "start_date": "7daysAgo",
            "end_date": "today",
        },
    }),
    call("analytics_read", {
        "action": "list_events",
        "params": {
            "platform": "ga4",
            "property_id": prop_id,
            "start_date": "14daysAgo",
            "end_date": "8daysAgo",
        },
    }),
])

prior = {e["name"]: e["count"] for e in last_week.get("events", [])}
drops = []
for e in this_week.get("events", []):
    base = prior.get(e["name"], 0)
    if base > 100 and e["count"] < base * 0.5:
        drops.append({
            "event": e["name"],
            "previous": base,
            "current": e["count"],
            "drop_pct": round((e["count"] - base) / base * 100, 1),
        })

RESULT = sorted(drops, key=lambda x: x["drop_pct"])
```

── Tips for good scripts ─────────────────────────────────────────────
1. Inspect shapes first. If you've never called a tool, call it once
   the normal way, see the keys, THEN write the script. Scripts that
   guess at response shapes waste the budget on the error path.
2. Filter on the way out, not on the way in. Fetch the data in one
   shot, then filter/sort in Python — don't make N filtered calls.
3. Return summaries, not raw data. If the result is >50KB, the user
   probably wants top-10 or aggregates, not a full dump.
4. Use gather() aggressively for independent calls. Sequential awaits
   serialize — the whole point is parallelism.
5. Check `result.get('error')` before using fields from a tool call.
   Errors are return values here, not exceptions.
"""


AUDIENCE_DOC = """\
audience_read / audience_write — detailed reference
══════════════════════════════════════════════════════════════════════

Google Data Manager exposes audiences as UserLists. Pass either
parent=accountTypes/{account_type}/accounts/{account_id} or account_type and
account_id. Read actions: list_audiences, get_audience, get_request_status.
Write actions: create_audience, update_audience, delete_audience,
ingest_audience_members, remove_audience_members, and
remove_all_audience_members. UserList fields go in config; member payloads
use Google's AudienceMember schema and require the datamanager OAuth scope.
Plain email, phone, and name identifiers are normalized and SHA-256 hashed
before upload; values that are already hashed are left unchanged. Ingest and
remove actions require destinations.
"""


_DOCS = {
    "audience_read": AUDIENCE_DOC,
    "audience_write": AUDIENCE_DOC,
    "warehouse_query": WAREHOUSE_QUERY_DOC,
    "cross_platform_report": CROSS_PLATFORM_REPORT_DOC,
    "run_audit": RUN_AUDIT_DOC,
    "run_analysis": RUN_ANALYSIS_DOC,
    "run_script": RUN_SCRIPT_DOC,
}


def get_doc(tool_name: str) -> str | None:
    """Return the detailed reference doc for a tool, or None."""
    return _DOCS.get(tool_name)


def list_docs() -> list[str]:
    """Return the list of tool names that have detailed reference docs."""
    return sorted(_DOCS.keys())
