# Workflow — Audit & diagnose tracking

Use when the user wants to audit tracking/tags/data quality, or asks **"why aren't my
conversions firing?"**. The judgment here is what a tool description can't give you:
*which* audits to run, how to read the findings, and how to turn them into fixes.

## 1. Orient

1. `get_session_context` → which platforms are connected. Only audit what's connected.
2. Decide scope from the user's words: a single platform ("is my GA4 ok?"), a funnel
   ("conversions"), or a full health check.

## 2. Run the right `run_audit` actions

All audits live in `run_audit`. Action names are platform-prefixed. Pick by intent (call
`run_audit(action="describe")` for the exact params of any action):

| Intent | Actions |
|---|---|
| GA4 health | `ga4_audit_data_streams`, `ga4_audit_conversion_events`, `ga4_audit_custom_definitions`, `ga4_audit_ecommerce`, `ga4_schema_validator` |
| GA4 anomalies | `ga4_check_data_anomalies` |
| GTM container | `gtm_audit_container`, `gtm_check_ga4_implementation`, `gtm_dependency_map` |
| **Conversions not firing** | `gtm_diagnose_conversion_discrepancy`, `gtm_find_tracking_regression`, `ga4_audit_conversion_events` |
| Consent / privacy | `gtm_audit_consent_mode`, `adobe_audit_consent_mode` |
| Paid media | `marketing_audit_budget_utilization`, `marketing_audit_quality_scores` (pass `platform`) |
| Warehouse | `warehouse_audit_dataset` / `_schema`, `warehouse_find_stale_tables`, `warehouse_check_empty_tables` (pass `engine`) |
| SEO | `seo_top_movers`, `seo_striking_distance`, `seo_ctr_outliers`, `seo_sitemap_health`, `seo_gsc_ga4_cross_reference` |
| Tag payload validation (no GTM needed) | `tag_list_platforms`, `tag_get_event_spec`, `tag_validate_payload` |

To run a suite efficiently, batch with `run_script`.

## 3. "Why aren't conversions firing?" — the decision path

1. `gtm_diagnose_conversion_discrepancy` — compares GA4 key events vs Ads conversions.
2. `ga4_audit_conversion_events` — is the event marked as a key event, and is it receiving
   volume? Watch for the pattern **tag configured but no data** and **event recently
   stopped** on the *primary* conversion — call those out first.
3. `gtm_find_tracking_regression` (needs `date_range_start` + `date_range_end`) — did a
   container change break it? 
4. If GTM isn't connected, fall back to `tag_validate_payload` against a captured payload.

## 4. Read findings, then conclude

- Every audit returns `findings: [{severity, code, message, evidence, recommendation}]`.
  **Read them before concluding anything.** Lead with `critical`/`high`.
- **Never declare healthy while a `critical` finding stands.**
- Be honest about coverage: an audit only covers connected, scannable platforms. Say what
  you did *not* check.

## 5. Persist + remediate

- Save a structured result to the Fluxito UI with `save_audit_result(action="save", ...)`
  (call `describe` for the findings payload shape). `audit_score_summary` / `…_history`
  track the score over time.
- Turn `recommendation`s into concrete fixes. Mutations go through `*_write` /
  `tagmanager_write` — and `tagmanager_write(action="propose_change")` lets you dry-run a
  GTM change before applying it. Confirm before `publish_container`.
