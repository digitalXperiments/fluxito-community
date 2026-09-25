"""
Unified MCP Tool Surface

Every sub-module still registers its fine-grained tools for internal
dispatch. This module exposes a curated unified surface to MCP clients
and preserves the legacy tools in ``tool_manager._legacy_tools`` for the
dispatchers to call.

Unified dispatchers (action + params):
    analytics_read          analytics_write
    tagmanager_read         tagmanager_write
    marketing_read          marketing_write
    warehouse_read
    seo_read                seo_write
    get_knowledge
    run_audit               run_analysis

Direct tools that survive the rewire (not absorbed into a dispatcher):
    warehouse_query         get_session_context
    set_active_project      list_my_projects
    run_script
    generic_tool_read       generic_tool_write

Each unified dispatcher accepts (action: str, params: Optional[dict] = None)
and dispatches to one of the pre-registered legacy tools. Action names map
1:1 with the original implementations — no behavioural changes.
"""

from __future__ import annotations

import logging
import time
from typing import Literal

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Routing Tables — action_name -> (legacy_tool_name, legacy_action_value | None)
# legacy_action_value = None means the legacy tool does NOT take `action` kwarg.
# ---------------------------------------------------------------------------

# analytics_read: GA4 + Amplitude + Mixpanel + PostHog + Adobe Analytics
# NOTE: Audit actions live in run_audit; cross-platform blends live in run_analysis.
ANALYTICS_READ_ROUTES: dict[str, tuple[str, str | None]] = {
    # GA4 + Amplitude + Mixpanel + PostHog + Adobe Analytics read actions (from analytics_read)
    "run_report": ("analytics_read", "run_report"),
    "compare_date_ranges": ("analytics_read", "compare_date_ranges"),
    "list_properties": ("analytics_read", "list_properties"),
    # NOTE: get_property / list_conversion_events / list_companies were removed —
    # they were advertised in the enum + docstring but have NO handler in any
    # platform branch (stress-test 2026-06-12, FINDINGS S0 #2), so every call
    # returned "Unknown action". GA4 exposes get_conversion_events, not
    # list_conversion_events.
    "list_audiences": ("analytics_read", "list_audiences"),
    "list_custom_dimensions": ("analytics_read", "list_custom_dimensions"),
    "list_custom_metrics": ("analytics_read", "list_custom_metrics"),
    "get_realtime": ("analytics_read", "get_realtime"),
    "list_data_streams": ("analytics_read", "list_data_streams"),
    "get_conversion_events": ("analytics_read", "get_conversion_events"),
    # Amplitude-specific
    "query_events": ("analytics_read", "query_events"),
    "get_active_users": ("analytics_read", "get_active_users"),
    "get_event_properties": ("analytics_read", "get_event_properties"),
    "get_user_properties": ("analytics_read", "get_user_properties"),
    "get_retention": ("analytics_read", "get_retention"),
    "get_funnel": ("analytics_read", "get_funnel"),
    "get_revenue": ("analytics_read", "get_revenue"),
    "list_cohorts": ("analytics_read", "list_cohorts"),
    "list_events": ("analytics_read", "list_events"),
    "get_event_detail": ("analytics_read", "get_event_detail"),
    # Adobe Analytics
    "list_report_suites": ("analytics_read", "list_report_suites"),
    "get_dimensions": ("analytics_read", "get_dimensions"),
    "get_metrics": ("analytics_read", "get_metrics"),
    "get_segments": ("analytics_read", "get_segments"),
    "get_calculated_metrics": ("analytics_read", "get_calculated_metrics"),
    "adobe_workspace_list_projects": ("analytics_read", "list_projects"),
    "adobe_workspace_get_project": ("analytics_read", "get_project"),
    "adobe_workspace_build_definition": ("analytics_read", "build_definition"),
    "adobe_workspace_validate_project": ("analytics_read", "validate_project"),
}

# Google Data Manager audiences (UserLists)
AUDIENCE_READ_ROUTES: dict[str, tuple[str, str | None]] = {
    "list_audiences": ("audience_read", "list_audiences"),
    "list_user_lists": ("audience_read", "list_user_lists"),
    "get_audience": ("audience_read", "get_audience"),
    "get_user_list": ("audience_read", "get_user_list"),
    "get_request_status": ("audience_read", "get_request_status"),
}

AUDIENCE_WRITE_ROUTES: dict[str, tuple[str, str | None]] = {
    "create_audience": ("audience_write", "create_audience"),
    "create_user_list": ("audience_write", "create_user_list"),
    "update_audience": ("audience_write", "update_audience"),
    "update_user_list": ("audience_write", "update_user_list"),
    "delete_audience": ("audience_write", "delete_audience"),
    "delete_user_list": ("audience_write", "delete_user_list"),
    "ingest_audience_members": ("audience_write", "ingest_audience_members"),
    "remove_audience_members": ("audience_write", "remove_audience_members"),
    "remove_all_audience_members": ("audience_write", "remove_all_audience_members"),
}

# analytics_write
ANALYTICS_WRITE_ROUTES: dict[str, tuple[str, str | None]] = {
    "create_audience": ("analytics_write", "create_audience"),
    "create_custom_dimension": ("analytics_write", "create_custom_dimension"),
    "create_custom_metric": ("analytics_write", "create_custom_metric"),
    "mark_event_as_conversion": ("analytics_write", "mark_event_as_conversion"),
    "create_calculated_metric": ("analytics_write", "create_calculated_metric"),
    "update_segment": ("analytics_write", "update_segment"),
    "create_segment": ("analytics_write", "create_segment"),
    "delete_segment": ("analytics_write", "delete_segment"),
    "delete_calculated_metric": ("analytics_write", "delete_calculated_metric"),
    # Adobe Analysis Workspace projects
    "adobe_workspace_create_project": ("analytics_write", "create_project"),
    "adobe_workspace_update_project": ("analytics_write", "update_project"),
    "adobe_workspace_delete_project": ("analytics_write", "delete_project"),
    "adobe_workspace_copy_project": ("analytics_write", "copy_project"),
    # Amplitude / Mixpanel / PostHog event-type CRUD
    "create_event_type": ("analytics_write", "create_event_type"),
    "update_event_type": ("analytics_write", "update_event_type"),
    "delete_event_type": ("analytics_write", "delete_event_type"),
}

# Public action names were made Adobe-specific so models do not confuse Analysis
# Workspace projects with Fluxito, Amplitude, Mixpanel, or PostHog projects. Keep
# the original generic names callable (but absent from tools/list) while existing
# clients and saved automations migrate.
DEPRECATED_ACTION_ALIASES: dict[str, dict[str, str]] = {
    "analytics_read": {
        "list_projects": "adobe_workspace_list_projects",
        "get_project": "adobe_workspace_get_project",
    },
    "analytics_write": {
        "create_project": "adobe_workspace_create_project",
        "update_project": "adobe_workspace_update_project",
        "delete_project": "adobe_workspace_delete_project",
        "copy_project": "adobe_workspace_copy_project",
    },
}

# tagmanager_read (GTM + Adobe Launch) — catalog only.
# Audits (audit_container, consent_mode, etc.) moved to run_audit.
TAGMANAGER_READ_ROUTES: dict[str, tuple[str, str | None]] = {
    # GTM reads
    "list_accounts": ("tagmanager_read", "list_accounts"),
    "list_containers": ("tagmanager_read", "list_containers"),
    "list_workspaces": ("tagmanager_read", "list_workspaces"),
    "list_tags": ("tagmanager_read", "list_tags"),
    "list_triggers": ("tagmanager_read", "list_triggers"),
    "list_variables": ("tagmanager_read", "list_variables"),
    "get_tag_detail": ("tagmanager_read", "get_tag_detail"),
    "get_container_summary": ("tagmanager_read", "get_container_summary"),
    # Adobe Launch reads
    "list_companies": ("tagmanager_read", "list_companies", {"platform": "adobe_launch"}),
    "list_properties": ("tagmanager_read", "list_properties", {"platform": "adobe_launch"}),
    "get_property": ("tagmanager_read", "get_property", {"platform": "adobe_launch"}),
    "list_rules": ("tagmanager_read", "list_rules", {"platform": "adobe_launch"}),
    "get_rule": ("tagmanager_read", "get_rule", {"platform": "adobe_launch"}),
    "list_rule_components": ("tagmanager_read", "list_rule_components", {"platform": "adobe_launch"}),
    "get_rule_component": ("tagmanager_read", "get_rule_component", {"platform": "adobe_launch"}),
    "list_data_elements": ("tagmanager_read", "list_data_elements", {"platform": "adobe_launch"}),
    "get_data_element": ("tagmanager_read", "get_data_element", {"platform": "adobe_launch"}),
    "list_extensions": ("tagmanager_read", "list_extensions", {"platform": "adobe_launch"}),
    "list_environments": ("tagmanager_read", "list_environments", {"platform": "adobe_launch"}),
    "list_libraries": ("tagmanager_read", "list_libraries", {"platform": "adobe_launch"}),
    "list_builds": ("tagmanager_read", "list_builds", {"platform": "adobe_launch"}),
}

# tagmanager_write (GTM + Adobe Launch)
TAGMANAGER_WRITE_ROUTES: dict[str, tuple[str, str | None]] = {
    # GTM
    "propose_change": ("tagmanager_write", "propose_change"),
    "create_workspace": ("tagmanager_write", "create_workspace"),
    "create_tag": ("tagmanager_write", "create_tag"),
    "update_tag": ("tagmanager_write", "update_tag"),
    "delete_tag": ("tagmanager_write", "delete_tag"),
    "create_trigger": ("tagmanager_write", "create_trigger"),
    "create_variable": ("tagmanager_write", "create_variable"),
    "publish_container": ("tagmanager_write", "publish_container"),
    # Adobe Launch
    "create_property": ("tagmanager_write", "create_property", {"platform": "adobe_launch"}),
    "create_rule": ("tagmanager_write", "create_rule", {"platform": "adobe_launch"}),
    "update_rule": ("tagmanager_write", "update_rule", {"platform": "adobe_launch"}),
    "delete_rule": ("tagmanager_write", "delete_rule", {"platform": "adobe_launch"}),
    "create_rule_component": ("tagmanager_write", "create_rule_component", {"platform": "adobe_launch"}),
    "update_rule_component": ("tagmanager_write", "update_rule_component", {"platform": "adobe_launch"}),
    "delete_rule_component": ("tagmanager_write", "delete_rule_component", {"platform": "adobe_launch"}),
    "create_data_element": ("tagmanager_write", "create_data_element", {"platform": "adobe_launch"}),
    "update_data_element": ("tagmanager_write", "update_data_element", {"platform": "adobe_launch"}),
    "delete_data_element": ("tagmanager_write", "delete_data_element", {"platform": "adobe_launch"}),
    "create_library": ("tagmanager_write", "create_library", {"platform": "adobe_launch"}),
    "add_resources_to_library": (
        "tagmanager_write",
        "add_resources_to_library",
        {"platform": "adobe_launch"},
    ),
    "build_library": ("tagmanager_write", "build_library", {"platform": "adobe_launch"}),
    "transition_library": ("tagmanager_write", "transition_library", {"platform": "adobe_launch"}),
}

# marketing_read (Google Ads + Meta Ads + TikTok Ads + Snap Ads) — performance queries only.
# Audits (budget_utilization, quality_scores, connection_health) moved to run_audit.
MARKETING_READ_ROUTES: dict[str, tuple[str, str | None]] = {
    "list_accounts": ("marketing_read", "list_accounts"),
    "get_campaign_performance": ("marketing_read", "get_campaign_performance"),
    "get_ad_group_performance": ("marketing_read", "get_ad_group_performance"),
    "get_adgroup_performance": ("marketing_read", "get_adgroup_performance"),
    "get_adset_performance": ("marketing_read", "get_adset_performance"),
    "get_adsquad_performance": ("marketing_read", "get_adsquad_performance"),
    "get_keyword_performance": ("marketing_read", "get_keyword_performance"),
    "get_conversion_actions": ("marketing_read", "get_conversion_actions"),
    # Adobe Marketo Engage (marketing automation) — read
    "marketo_get_leads": ("marketing_read", "get_leads", {"platform": "marketo"}),
    "marketo_get_lead": ("marketing_read", "get_lead_by_id", {"platform": "marketo"}),
    "marketo_list_lists": ("marketing_read", "list_lead_lists", {"platform": "marketo"}),
    "marketo_get_list_leads": ("marketing_read", "get_list_leads", {"platform": "marketo"}),
    "marketo_get_lead_activities": ("marketing_read", "get_lead_activities", {"platform": "marketo"}),
    "marketo_list_campaigns": ("marketing_read", "list_campaigns", {"platform": "marketo"}),
    "marketo_list_programs": ("marketing_read", "list_programs", {"platform": "marketo"}),
    "marketo_get_program": ("marketing_read", "get_program", {"platform": "marketo"}),
    "marketo_list_emails": ("marketing_read", "list_emails", {"platform": "marketo"}),
    "marketo_list_landing_pages": ("marketing_read", "list_landing_pages", {"platform": "marketo"}),
    "marketo_list_forms": ("marketing_read", "list_forms", {"platform": "marketo"}),
    # AppsFlyer (MMP) — read
    "appsflyer_list_apps": ("marketing_read", "list_apps", {"platform": "appsflyer"}),
    "appsflyer_get_installs_report": ("marketing_read", "get_installs_report", {"platform": "appsflyer"}),
    "appsflyer_get_in_app_events_report": (
        "marketing_read",
        "get_in_app_events_report",
        {"platform": "appsflyer"},
    ),
    "appsflyer_get_partners_report": ("marketing_read", "get_partners_report", {"platform": "appsflyer"}),
    # Adjust (MMP) — read
    "adjust_list_apps": ("marketing_read", "list_apps", {"platform": "adjust"}),
    "adjust_get_report": ("marketing_read", "get_report", {"platform": "adjust"}),
    "adjust_get_pivot_report": ("marketing_read", "get_pivot_report", {"platform": "adjust"}),
    "adjust_list_events": ("marketing_read", "list_events", {"platform": "adjust"}),
    "adjust_list_app_automation_apps": ("marketing_read", "list_app_automation_apps", {"platform": "adjust"}),
    "adjust_get_partner_links": ("marketing_read", "get_partner_links", {"platform": "adjust"}),
    # Branch (Deep linking & Attribution) — read
    "branch_get_app": ("marketing_read", "get_app", {"platform": "branch"}),
    "branch_query_analytics": ("marketing_read", "query_analytics", {"platform": "branch"}),
    # Braze (Customer engagement) — read
    "braze_list_campaigns": ("marketing_read", "list_campaigns", {"platform": "braze"}),
    "braze_get_campaign_details": ("marketing_read", "get_campaign_details", {"platform": "braze"}),
    "braze_list_canvases": ("marketing_read", "list_canvases", {"platform": "braze"}),
    "braze_get_canvas_details": ("marketing_read", "get_canvas_details", {"platform": "braze"}),
    "braze_list_segments": ("marketing_read", "list_segments", {"platform": "braze"}),
    "braze_get_segment_details": ("marketing_read", "get_segment_details", {"platform": "braze"}),
    # MoEngage (Customer engagement) — read
    "moengage_list_campaigns": ("marketing_read", "list_campaigns", {"platform": "moengage"}),
    "moengage_get_campaign_details": ("marketing_read", "get_campaign_details", {"platform": "moengage"}),
    "moengage_get_user_info": ("marketing_read", "get_user_info", {"platform": "moengage"}),
    "moengage_list_events": ("marketing_read", "list_events", {"platform": "moengage"}),
}

# marketing_write
MARKETING_WRITE_ROUTES: dict[str, tuple[str, str | None]] = {
    "update_campaign_budget": ("marketing_write", "update_campaign_budget"),
    "update_campaign_status": ("marketing_write", "update_campaign_status"),
    "create_campaign": ("marketing_write", "create_campaign"),
    # Adobe Marketo Engage — write
    "marketo_upsert_leads": ("marketing_write", "create_or_update_leads", {"platform": "marketo"}),
    "marketo_add_to_list": ("marketing_write", "add_leads_to_list", {"platform": "marketo"}),
    "marketo_remove_from_list": ("marketing_write", "remove_leads_from_list", {"platform": "marketo"}),
    "marketo_request_campaign": ("marketing_write", "request_campaign", {"platform": "marketo"}),
    "marketo_schedule_campaign": ("marketing_write", "schedule_campaign", {"platform": "marketo"}),
    # Branch (Deep linking & Attribution) — write
    "branch_request_daily_export": ("marketing_write", "request_daily_export", {"platform": "branch"}),
    # Braze (Customer engagement) — write
    "braze_track_users": ("marketing_write", "track_users", {"platform": "braze"}),
    "braze_create_user_alias": ("marketing_write", "create_user_alias", {"platform": "braze"}),
    "braze_identify_users": ("marketing_write", "identify_users", {"platform": "braze"}),
    "braze_merge_users": ("marketing_write", "merge_users", {"platform": "braze"}),
    "braze_delete_users": ("marketing_write", "delete_users", {"platform": "braze"}),
    "braze_send_message": ("marketing_write", "send_message", {"platform": "braze"}),
    "braze_trigger_campaign": ("marketing_write", "trigger_campaign", {"platform": "braze"}),
    "braze_trigger_canvas": ("marketing_write", "trigger_canvas", {"platform": "braze"}),
    # MoEngage (Customer engagement) — write
    "moengage_create_user": ("marketing_write", "create_user", {"platform": "moengage"}),
    "moengage_update_user": ("marketing_write", "update_user", {"platform": "moengage"}),
    "moengage_add_device": ("marketing_write", "add_device", {"platform": "moengage"}),
    "moengage_send_push": ("marketing_write", "send_push", {"platform": "moengage"}),
    "moengage_send_email": ("marketing_write", "send_email", {"platform": "moengage"}),
    "moengage_send_sms": ("marketing_write", "send_sms", {"platform": "moengage"}),
}

# warehouse_read (BigQuery + Redshift + Snowflake) — schema + metadata only.
# Audits (audit_dataset, check_data_quality, etc.) moved to run_audit.
WAREHOUSE_READ_ROUTES: dict[str, tuple[str, str | None]] = {
    "list_datasets": ("warehouse_read", "list_datasets"),
    "list_databases": ("warehouse_read", "list_databases"),
    "list_schemas": ("warehouse_read", "list_schemas"),
    "list_warehouses": ("warehouse_read", "list_warehouses"),
    "list_tables": ("warehouse_read", "list_tables"),
    "get_table_schema": ("warehouse_read", "get_table_schema"),
    # preview_table / get_warehouse_usage were routed to legacy `warehouse_read`,
    # which has no handler for them — every call returned "Unknown action"
    # (stress-test 2026-06-12, FINDINGS S0 #3). The real handlers live in
    # warehouse_query (preview_table) and warehouse_audit (get_warehouse_usage).
    "preview_table": ("warehouse_query", "preview_table"),
    "list_connections": ("warehouse_read", "list_connections"),
    "get_warehouse_usage": ("warehouse_audit", "get_warehouse_usage"),
}

# seo_read (Google Search Console + Bing Webmaster Tools) — queries only.
# Audits (top_movers, striking_distance, etc.) moved to run_audit.
# Route key format: <platform>_<action> for Bing; bare action for GSC (backwards-compat).
SEO_READ_ROUTES: dict[str, tuple[str, str | None]] = {
    # Google Search Console actions (bare names preserved for backwards-compatibility)
    "list_sites": ("search_console_read", "list_sites"),
    "search_analytics": ("search_console_read", "search_analytics"),
    "list_sitemaps": ("search_console_read", "list_sitemaps"),
    "get_sitemap": ("search_console_read", "get_sitemap"),
    "inspect_url": ("search_console_read", "inspect_url"),
    # Google Search Console actions (explicit platform prefix)
    "gsc_list_sites": ("search_console_read", "list_sites"),
    "gsc_search_analytics": ("search_console_read", "search_analytics"),
    "gsc_list_sitemaps": ("search_console_read", "list_sitemaps"),
    "gsc_get_sitemap": ("search_console_read", "get_sitemap"),
    "gsc_inspect_url": ("search_console_read", "inspect_url"),
    # Bing Webmaster Tools actions
    "bing_list_sites": ("bing_webmaster_read", "list_sites"),
    "bing_get_query_stats": ("bing_webmaster_read", "get_query_stats"),
    "bing_get_crawl_stats": ("bing_webmaster_read", "get_crawl_stats"),
    "bing_get_index_coverage": ("bing_webmaster_read", "get_index_coverage"),
    "bing_get_link_counts": ("bing_webmaster_read", "get_link_counts"),
}

# seo_write
SEO_WRITE_ROUTES: dict[str, tuple[str, str | None]] = {
    "submit_sitemap": ("search_console_write", "submit_sitemap"),
    "delete_sitemap": ("search_console_write", "delete_sitemap"),
}

# get_knowledge — merges business context and the three KPI
# operations (list / detail / compute). The legacy full-catalog dump
# (get_kpi_definitions / old "kpis" action) has been removed; Claude should
# call list_kpis for discovery and get_kpi for single-KPI details.
KNOWLEDGE_ROUTES: dict[str, tuple[str, str | None]] = {
    "context": ("get_business_context", None),
    "list_kpis": ("list_kpis", None),
    "get_kpi": ("get_kpi", None),
    "compute_kpi": ("compute_kpi", None),
}

# ---------------------------------------------------------------------------
# NEW — Heavy / Composite Surface
#
# run_audit     — every audit_*/check_* action from every domain, prefixed by
#                 platform so action names are globally unambiguous.
# run_analysis  — cross-connector computed insights (blended reports,
#                 attribution models, incrementality, etc.).
#
# Keeping these OUT of the domain read tools means:
#   • simple reads stay fast and have short, focused docstrings;
#   • heavy ops can carry different timeouts, caching, and billing weight;
#   • Claude can tell at tool-selection time whether an op is cheap or heavy.
# ---------------------------------------------------------------------------

# run_audit: prefixed actions, one table across every domain.
#
# NOTE: Only routes with a live handler in the target legacy tool are listed
# here. Historically this table had several "aspirational" routes pointing at
# handlers that were never implemented (ga4_audit_property,
# ga4_audit_tracking_setup, analytics_* composites, gtm_schema_validator,
# gtm_check_taxonomy_health). Those returned "Unknown action" at runtime and
# have been removed — add them back here ONLY when the corresponding handler
# lands in analytics_audit / tagmanager_audit.
AUDIT_ROUTES: dict[str, tuple] = {
    # ── Product analytics (GA4, Amplitude, Mixpanel, PostHog, Adobe Analytics) ────
    # Routes below all map to (legacy_tool, legacy_action, extra_kwargs?) and
    # were verified against the action handlers in analytics_audit.
    "ga4_audit_data_streams": ("analytics_audit", "audit_data_streams", {"platform": "ga4"}),
    "ga4_audit_conversion_events": ("analytics_audit", "audit_conversion_events", {"platform": "ga4"}),
    "ga4_audit_custom_definitions": ("analytics_audit", "audit_custom_definitions", {"platform": "ga4"}),
    "ga4_audit_ecommerce": ("analytics_audit", "audit_ecommerce", {"platform": "ga4"}),
    "ga4_schema_validator": ("analytics_audit", "schema_validator", {"platform": "ga4"}),
    "ga4_check_data_anomalies": ("analytics_audit", "check_data_anomalies", {"platform": "ga4"}),
    "amplitude_check_taxonomy_health": (
        "analytics_audit",
        "check_taxonomy_health",
        {"platform": "amplitude"},
    ),
    "amplitude_check_event_volume_anomalies": (
        "analytics_audit",
        "check_event_volume_anomalies",
        {"platform": "amplitude"},
    ),
    "mixpanel_check_taxonomy_health": (
        "analytics_audit",
        "check_taxonomy_health",
        {"platform": "mixpanel"},
    ),
    "mixpanel_check_event_volume_anomalies": (
        "analytics_audit",
        "check_event_volume_anomalies",
        {"platform": "mixpanel"},
    ),
    "posthog_check_taxonomy_health": (
        "analytics_audit",
        "check_taxonomy_health",
        {"platform": "posthog"},
    ),
    "posthog_check_event_volume_anomalies": (
        "analytics_audit",
        "check_event_volume_anomalies",
        {"platform": "posthog"},
    ),
    "adobe_audit_report_suite": ("analytics_audit", "audit_report_suite", {"platform": "adobe_analytics"}),
    "adobe_check_data_quality": ("analytics_audit", "check_data_quality", {"platform": "adobe_analytics"}),
    # ── Tag Manager (GTM + Adobe Launch) ───────────────────────────────
    # Routes below all map to handlers that exist in tagmanager_audit.
    "gtm_audit_container": ("tagmanager_audit", "audit_container"),
    "gtm_explain_tag": ("tagmanager_audit", "explain_tag"),
    "gtm_simulate_event": ("tagmanager_audit", "simulate_event"),
    "gtm_dependency_map": ("tagmanager_audit", "dependency_map"),
    "gtm_check_ga4_implementation": ("tagmanager_audit", "check_ga4_implementation"),
    "gtm_find_tracking_regression": ("tagmanager_audit", "find_tracking_regression"),
    "gtm_diagnose_conversion_discrepancy": ("tagmanager_audit", "diagnose_conversion_discrepancy"),
    "gtm_generate_audit_report": ("tagmanager_audit", "generate_audit_report"),
    "gtm_suggest_improvements": ("tagmanager_audit", "suggest_improvements"),
    "gtm_benchmark_health": ("tagmanager_audit", "benchmark_health"),
    "adobe_launch_audit_property": (
        "tagmanager_audit",
        "audit_property",
        {"platform": "adobe_launch"},
    ),
    "adobe_launch_get_publish_history": (
        "tagmanager_audit",
        "get_publish_history",
        {"platform": "adobe_launch"},
    ),
    # NEW — Consent Mode v2 / GDPR / CCPA compliance audit.
    # Both GTM and Adobe Launch are supported via the same `audit_consent_mode`
    # action inside `tagmanager_audit`, routed by platform.
    "gtm_audit_consent_mode": (
        "tagmanager_audit",
        "audit_consent_mode",
        {"platform": "gtm"},
    ),
    "adobe_audit_consent_mode": (
        "tagmanager_audit",
        "audit_consent_mode",
        {"platform": "adobe_launch"},
    ),
    # ── Paid marketing ─────────────────────────────────────────────────
    "marketing_audit_budget_utilization": ("marketing_audit", "audit_budget_utilization"),
    "marketing_audit_quality_scores": ("marketing_audit", "audit_quality_scores"),
    # Adobe Marketo Engage — audit
    "marketo_audit_instance": ("marketing_audit", "audit_instance", {"platform": "marketo"}),
    "marketo_check_data_quality": ("marketing_audit", "check_data_quality", {"platform": "marketo"}),
    # ── Warehouse ──────────────────────────────────────────────────────
    "warehouse_audit_dataset": ("warehouse_audit", "audit_dataset"),
    "warehouse_audit_schema": ("warehouse_audit", "audit_schema"),
    "warehouse_find_stale_tables": ("warehouse_audit", "find_stale_tables"),
    "warehouse_check_table_health": ("warehouse_audit", "check_table_health"),
    "warehouse_check_empty_tables": ("warehouse_audit", "check_empty_tables"),
    "warehouse_check_clustering_health": ("warehouse_audit", "check_clustering_health"),
    # ── SEO / Search Console ───────────────────────────────────────────
    "seo_top_movers": ("search_console_audit", "top_movers"),
    "seo_striking_distance": ("search_console_audit", "striking_distance"),
    "seo_ctr_outliers": ("search_console_audit", "ctr_outliers"),
    "seo_sitemap_health": ("search_console_audit", "sitemap_health"),
    "seo_gsc_ga4_cross_reference": ("search_console_audit", "gsc_ga4_cross_reference"),
    # ── Tag Rule Book (connector-independent, 20 platforms) ────────────
    # Works without a GTM connection — validates static config + live captures.
    "tag_list_platforms": ("tag_rulebook", "list_platforms"),
    "tag_get_platform_spec": ("tag_rulebook", "get_platform_spec"),
    "tag_get_event_spec": ("tag_rulebook", "get_event_spec"),
    "tag_identify_type": ("tag_rulebook", "identify_tag_type"),
    "tag_validate_payload": ("tag_rulebook", "validate_payload"),
    "tag_audit_rulebooks": ("tag_rulebook", "audit_against_rulebooks"),
    "tag_list_custom_rules": ("tag_rulebook", "list_custom_rules"),
    "tag_save_custom_rule": ("tag_rulebook", "save_custom_rule"),
    "tag_delete_custom_rule": ("tag_rulebook", "delete_custom_rule"),
    # ── Live Tag Test (Claude computer-use guided testing) ──────────────
    "live_tag_get_plan": ("live_tag_test", "get_test_plan"),
    "live_tag_get_sdr_context": ("live_tag_test", "get_sdr_context"),
    "live_tag_analyze_captures": ("live_tag_test", "analyze_captures"),
    "live_tag_start_session": ("live_tag_test", "start_session"),
    "live_tag_finish_session": ("live_tag_test", "finish_session"),
    "live_tag_list_plans": ("live_tag_test", "list_test_plans"),
    "live_tag_save_plan": ("live_tag_test", "save_test_plan"),
    # ── Audit Persistence (save results to Fluxito UI) ──────────────────
    "save_audit_result": ("save_audit_result", "save"),
    "get_audit_run": ("save_audit_result", "get_run"),
    "list_audit_runs": ("save_audit_result", "list_runs"),
    "audit_score_summary": ("save_audit_result", "get_score_summary"),
    "audit_score_history": ("save_audit_result", "get_score_history"),
}

# run_analysis: cross-connector computed insights.
#   • cross_platform_report — the existing blended reporter
#   • blended_performance / channel_comparison / top_campaigns —
#     sub-actions of cross_platform_report exposed as top-level actions for
#     discoverability.
#   • revenue_attribution — NEW; handler lives inside cross_platform_report.
#     If the handler isn't registered yet the legacy tool returns a clean
#     "unknown action" error so the failure mode is obvious.
ANALYSIS_ROUTES: dict[str, tuple[str, str | None]] = {
    # Legacy pass-through — user sets `action` inside params if desired.
    "cross_platform_report": ("cross_platform_report", None),
    # Pre-wired sub-actions for nicer discoverability.
    "blended_performance": ("cross_platform_report", "blended_performance"),
    "channel_comparison": ("cross_platform_report", "channel_comparison"),
    "top_campaigns": ("cross_platform_report", "top_campaigns"),
    # Revenue attribution (spend + GA4 touches + warehouse revenue).
    "revenue_attribution": ("cross_platform_report", "revenue_attribution"),
}


# ---------------------------------------------------------------------------
# Rich docstrings
# ---------------------------------------------------------------------------

AUDIENCE_READ_DOC = """
Read Google Data Manager audiences (UserLists).

Actions:
  list_audiences / list_user_lists — params: parent='accountTypes/.../accounts/...'
                                     or account_type + account_id; optional page_size, page_token, filter.
  get_audience / get_user_list    — params: name='accountTypes/.../accounts/.../userLists/...'
  get_request_status              — params: request_id from a member ingestion/removal response.

Requires a Google connection with the https://www.googleapis.com/auth/datamanager scope.
"""

AUDIENCE_WRITE_DOC = """
Manage Google Data Manager audiences and audience members.

Actions:
  create_audience / create_user_list — params: parent (or account_type + account_id),
                                      config={display_name, description?, membership_duration?,
                                      membership_status?, ingested_user_list_info?, target_network_info?}
  update_audience / update_user_list — params: name, config, optional update_mask.
  delete_audience / delete_user_list — params: name.
  ingest_audience_members           — params: audience_members[], destinations[], encoding?,
                                      consent?, terms_of_service?, validate_only?.
  remove_audience_members           — same member payload as ingest (destinations required).
  remove_all_audience_members       — params: destinations[], remove_as_of_time?, validate_only?.

Member identifiers must follow Google's hashing and consent requirements. Use validate_only=true
before applying a new batch. Requires the Data Manager OAuth scope.
"""

ANALYTICS_READ_DOC = """
Read product / web analytics data across GA4, Amplitude, Mixpanel, PostHog, Adobe Analytics.

REQUIRED: always pass `platform` ('ga4', 'amplitude', 'mixpanel', 'posthog', or 'adobe_analytics') inside `params`.
Also pass `metrics` and `dimensions` as plain strings (e.g. ["sessions", "users"]),
NOT as GA4 API objects like [{"name": "sessions"}].

For audits / anomaly checks / tracking regressions → use `run_audit`.
For cross-platform blends / attribution → use `run_analysis`.

Actions (pass via `action`, required params inside `params`):

  REPORTING
    run_report        — Run a report. params: platform (required), property_id (ga4) or
                        project_id (amplitude), start_date, end_date,
                        dimensions (list of strings), metrics (list of strings),
                        dimension_filter, limit.
    compare_date_ranges — Run a date-range comparison report (GA4 only).
    get_realtime      — Real-time event stream.
    query_events      — Event query (Amplitude, Mixpanel, PostHog).
    get_active_users  — DAU/WAU/MAU.
    get_retention     — Retention analysis.
    get_funnel        — Funnel conversion.
    get_revenue       — Revenue metrics.

  CATALOG / METADATA
    list_properties, get_property, list_audiences, list_custom_dimensions,
    list_custom_metrics, list_data_streams, list_conversion_events,
    get_conversion_events, list_cohorts, list_events, get_event_detail,
    get_event_properties, get_user_properties, list_report_suites,
    list_companies, get_dimensions, get_metrics, get_segments,
    get_calculated_metrics, adobe_workspace_list_projects,
    adobe_workspace_get_project, adobe_workspace_build_definition,
    adobe_workspace_validate_project

Return shape: {rows/data/items: [...], ...} or {error, error_type, message}.
"""

ANALYTICS_WRITE_DOC = """
Mutate product/web analytics configuration. Requires analytics_write scope.

REQUIRED params for every action: platform ('ga4', 'amplitude', 'mixpanel', 'posthog', or 'adobe_analytics'),
property_id, and config (dict of action-specific keys).

Actions:
  create_audience            — params: platform, property_id, config={name, filter_clauses}
  create_custom_dimension    — params: platform, property_id, config={parameter_name, display_name, scope}
  create_custom_metric       — params: platform, property_id, config={parameter_name, display_name, unit, scope}
  mark_event_as_conversion   — params: platform, property_id, config={event_name}
  create_calculated_metric   — params: platform, property_id, config={report_suite_id, name, formula}
  create_segment             — params: platform, property_id, config={report_suite_id, name, definition}
  update_segment             — params: platform, property_id, config={segment_id, updates}
  delete_segment             — params: platform, property_id, config={segment_id}
  delete_calculated_metric   — params: platform, property_id, config={metric_id}
  adobe_workspace_create_project — Adobe Workspace. Prefer config={name, rsid, tables:[{metrics, dimension?}]}.
                               Fluxito builds the Workspace JSON. Do NOT invent a raw definition.
  adobe_workspace_update_project — Adobe Workspace. Partial PUT of supplied fields only (no pre-GET).
                               Rename: config={project_id, name}. Rebuild tables: config={project_id, tables:[...]}.
                               Set merge_definition=true to GET+local-deep-merge definition.
  adobe_workspace_delete_project — Adobe Workspace. Destructive; explicit project_id only.
                               params: platform, config={project_id}
  adobe_workspace_copy_project — Adobe Workspace. GET source + POST under a new name.
                               params: platform, config={project_id, name}
  create_event_type          — params: platform (amplitude|mixpanel|posthog), config={event_type, description?, category?}
  update_event_type          — params: platform (amplitude|mixpanel|posthog), config={event_type, new_name?, description?, category?}
  delete_event_type          — params: platform (amplitude|mixpanel|posthog), config={event_type}
"""

TAGMANAGER_READ_DOC = """
Read tag manager catalog (GTM, Adobe Launch).

Pass platform (gtm | adobe_launch) in params when both are connected.
For audits (container health, consent mode, taxonomy, publish history, etc.)
→ use `run_audit` (e.g. gtm_audit_container, adobe_launch_get_publish_history).

Actions:
  GTM
    list_accounts        — Enumerate GTM accounts (no args required)
    list_containers      — params: account_id
    list_workspaces      — params: account_id, container_id
    list_tags            — params: account_id, container_id, workspace_id
    list_triggers        — params: account_id, container_id, workspace_id
    list_variables       — params: account_id, container_id, workspace_id
    get_tag_detail       — params: account_id, container_id, workspace_id, tag_id
    get_container_summary— params: account_id, container_id
  ADOBE LAUNCH
    list_companies, list_properties, get_property, list_rules, get_rule,
    list_rule_components, get_rule_component, list_data_elements, get_data_element,
    list_extensions, list_environments, list_libraries, list_builds
"""

TAGMANAGER_WRITE_DOC = """
Mutate tag manager (GTM, Adobe Launch). Requires tagmanager_write / publish scope.

Actions:
  GTM
    propose_change     — Dry-run a proposed change (safe, no mutation). params: spec dict
    create_workspace   — params: account_id, container_id, name
    create_tag         — params: account_id, container_id, workspace_id, name, type,
                         firing_trigger_ids[], parameters[]
    update_tag         — params: account_id, container_id, workspace_id, tag_id, updates
    delete_tag         — params: account_id, container_id, workspace_id, tag_id
    create_trigger     — params: account_id, container_id, workspace_id, name, type, filters[]
    create_variable    — params: account_id, container_id, workspace_id, name, type, parameters[]
    publish_container  — params: account_id, container_id, workspace_id, name (requires publish scope)

  ADOBE LAUNCH
    create_property           — config: {name, company_id, platform?, domains?}
    create_rule               — config: {property_id, name, components?[]}
    update_rule               — config: {rule_id, name?, enabled?}
    delete_rule               — config: {rule_id}
    create_rule_component     — config: {property_id, rule_id, name, delegate_descriptor_id, settings?, extension_id?, rule_order?, order?, negate?, timeout?, delay_next?}
    update_rule_component     — config: {rule_component_id, name?, settings?, delegate_descriptor_id?, rule_order?, order?, negate?, timeout?, delay_next?}
    delete_rule_component     — config: {rule_component_id}
    create_data_element       — config: {property_id, name, delegate_descriptor_id, settings?, extension_id?, clean_text?, force_lower_case?, default_value?, storage_duration?, enabled?}
    update_data_element       — config: {data_element_id, name?, settings?, delegate_descriptor_id?, enabled?, clean_text?, force_lower_case?, default_value?, storage_duration?}
    delete_data_element       — config: {data_element_id}
    create_library            — config: {property_id, name, environment_id}
    add_resources_to_library  — config: {library_id, resources[]}
    build_library             — config: {library_id}
    transition_library        — config: {library_id, action}  (action: submit|approve|reject|develop)
"""

MARKETING_READ_DOC = """
Read paid-marketing performance across Google Ads, Meta, TikTok, Snap.

REQUIRED: always pass `platform` in params. Google Ads uses "google" (not "google_ads").
For audits (budget utilization, quality scores, connection health) → use
`run_audit`. For blended cross-channel reports / attribution → use `run_analysis`.

Actions:
  list_accounts              — Enumerate ad accounts
  get_campaign_performance   — params: platform, account_id, start_date, end_date, metrics[]
  get_ad_group_performance / get_adgroup_performance  — Google Ads ad-group view
  get_adset_performance      — Meta adset-level
  get_adsquad_performance    — Snap adsquad-level
  get_keyword_performance    — Google Ads keyword-level
  get_conversion_actions     — List conversion actions

Adobe Marketo Engage actions (prefix `marketo_`, marketing automation; no account_id):
  marketo_get_leads           — filters={filter_type,filter_values[],fields[]}, limit
  marketo_get_lead            — resource_id (lead id), filters={fields[]}
  marketo_list_lists          — static/smart lists
  marketo_get_list_leads      — resource_id (list id), limit
  marketo_get_lead_activities — filters={activity_type_ids[],list_id,since_datetime}, limit
  marketo_list_campaigns / marketo_list_programs / marketo_get_program (resource_id=program id)
  marketo_list_emails / marketo_list_landing_pages / marketo_list_forms
"""

MARKETING_WRITE_DOC = """
Mutate paid marketing campaigns. Requires marketing_write scope.

REQUIRED: always pass `platform` in params.

Actions:
  update_campaign_budget   — params: platform (required), account_id, campaign_id, new_budget
  update_campaign_status   — params: platform (required), account_id, campaign_id, status (PAUSED|ENABLED)
  create_campaign          — params: platform (required), account_id, spec dict

Adobe Marketo Engage actions (prefix `marketo_`; resource_id = list/campaign id, payload carries the body):
  marketo_upsert_leads     — payload={leads[], lookup_field?, action?}
  marketo_add_to_list      — resource_id (list id), payload={lead_ids[]}
  marketo_remove_from_list — resource_id (list id), payload={lead_ids[]}
  marketo_request_campaign — resource_id (campaign id), payload={lead_ids[], tokens?}
  marketo_schedule_campaign— resource_id (campaign id), payload={run_at?, tokens?}
"""

WAREHOUSE_READ_DOC = """
Read warehouse schema + metadata across BigQuery, Redshift, Snowflake.

REQUIRED: always pass `engine` ('bigquery', 'redshift', or 'snowflake') in params.
For data-quality / stale-table / clustering audits → use `run_audit`.
To actually execute SQL → use `warehouse_query`.

Actions:
  list_connections    — Which warehouses are connected
  list_datasets       — BigQuery datasets
  list_databases      — Redshift/Snowflake databases
  list_schemas        — Redshift/Snowflake schemas
  list_warehouses     — Snowflake compute warehouses
  list_tables         — Tables in a dataset_id/schema
  get_table_schema    — Column list + types for a table_id
  preview_table       — First N rows (cheap sample). params: engine, dataset_id, table_id
  get_warehouse_usage — Compute spend / storage stats
"""

WAREHOUSE_QUERY_DOC = """
Execute SQL against a connected warehouse (BigQuery, Redshift, Snowflake).
Separate from warehouse_read because SQL execution is billable / expensive
and the AI should reason about cost distinctly from catalog reads.

REQUIRED: always pass `engine` and `action` in params.

Params:
  engine        — bigquery | redshift | snowflake (REQUIRED)
  query         — SQL string (required for run_query / dry_run / explain_query)
  dataset_id    — used by preview_table
  table_id      — used by preview_table
  max_results   — Row cap (default 1000, max 5000)
  connection_id — Specific warehouse connection if user has multiple

Actions: run_query (default), preview_table, dry_run (BigQuery),
         explain_query (Redshift/Snowflake).
"""

SEO_READ_DOC = """
Read organic-search data across Google Search Console and Bing Webmaster Tools.
Use action names prefixed with `gsc_` or `bing_` to target a specific platform.
Bare action names (list_sites, search_analytics, etc.) default to Google Search Console
for backwards-compatibility.

For audits (top_movers, striking_distance, CTR outliers, sitemap health,
GSC↔GA4 cross-reference) → use `run_audit` with the `seo_*` action prefix.

GOOGLE SEARCH CONSOLE actions (prefix `gsc_` or use bare names):
  list_sites / gsc_list_sites
                   — Enumerate verified GSC properties
  search_analytics / gsc_search_analytics
                   — Impressions/clicks/CTR/position. params: site_url,
                     start_date, end_date, dimensions[] (query|page|country|
                     device|date|searchAppearance), search_type, row_limit,
                     start_row, dimension_filter_groups, aggregation_type,
                     data_state
  list_sitemaps / gsc_list_sitemaps
                   — params: site_url
  get_sitemap / gsc_get_sitemap
                   — params: site_url, feedpath
  inspect_url / gsc_inspect_url
                   — URL Inspection API. params: site_url, inspection_url,
                     language_code

BING WEBMASTER TOOLS actions (prefix `bing_`):
  bing_list_sites  — Enumerate verified Bing Webmaster sites
  bing_get_query_stats
                   — Keyword/query performance. params: site_url,
                     start_date, end_date (YYYY-MM-DD), search_type,
                     page (default 0), page_size (default 100)
  bing_get_crawl_stats
                   — Crawl statistics. params: site_url
  bing_get_index_coverage
                   — Index coverage data. params: site_url
  bing_get_link_counts
                   — Inbound link counts. params: site_url
"""

SEO_WRITE_DOC = """
Mutate organic-search configuration. Requires the full Search Console
(webmasters) scope.

Actions:
  submit_sitemap — params: site_url, feedpath
  delete_sitemap — params: site_url, feedpath
"""

KNOWLEDGE_DOC = """
Read and execute the project's knowledge base — KPI definitions and business
context. Always call this early in a
session so your answers match the client's terminology and metrics.

Actions:
  list_kpis   — Concise KPI catalog (slug, name, aliases, status,
                short description). Use this for discovery.
                params: status (approved | draft | deprecated | all,
                        default "approved")
  get_kpi     — Full spec for one KPI (definition, expression, bound
                inputs, unit, direction, expected range).
                params: slug (case-insensitive)
  compute_kpi — Execute the KPI's formula against its bound sources and
                return the scalar value. Prefer this over composing
                formulas from raw analytics calls.
                params: slug, date_range_start ("30daysAgo" or
                        "YYYY-MM-DD"), date_range_end ("today" or
                        "YYYY-MM-DD")
  context     — Business context markdown doc. No params.

Typical flow: list_kpis → get_kpi(slug) for definition → compute_kpi(slug)
for the current value.
"""

SESSION_CONTEXT_DOC = """
One-stop context read: connected platforms, active project, and (optionally)
a detailed doc for a specific tool. Call this first in any new session — it
replaces the old get_connection_status + get_active_project + tool_help tools.

Params:
  tool_name (optional) — If provided, returns the detailed reference doc for
                         that tool INSTEAD of the session summary. Use this
                         when you want deeper docs for a specific tool.

No params = full session context:
  • user_email, active_project (name, slug, plan, role)
  • connected_platforms[] and disconnected_platforms[] (with connect_urls)
  • list of available tools (with one-line descriptions)

With tool_name:
  • tool_name, detailed doc (actions + params + examples)
"""

GENERIC_READ_DOC = """
Generic escape-hatch READ tool for capabilities that don't fit the core
buckets (analytics/tagmanager/marketing/warehouse/seo). Use only
when no other read tool applies. Today this is a no-op stub that returns
a capability-not-available error; future features will wire actions here.

Params:
  capability — e.g. 'notifications', 'audit_log', 'webhooks' (future)
  action     — capability-specific action name
  args       — action-specific parameters
"""

GENERIC_WRITE_DOC = """
Generic escape-hatch WRITE tool (see generic_tool_read). Stub today.

Params:
  capability, action, args
"""

RUN_AUDIT_DOC = """
Run a canonical audit / health check across any connected platform. All audit
and anomaly-detection work lives here — keeping it out of the domain read
tools so simple reads stay lean. Action names are platform-prefixed so they
are globally unambiguous.

Call `get_session_context(tool_name='run_audit')` for the full action
reference including required params.

Audit results that return findings or a score are saved to the Fluxito Audit
page automatically; the response carries `saved_to_fluxito.view_url`. Share
that link — do not call save_audit_result again for the same run.

Actions (grouped):

  PRODUCT ANALYTICS (GA4 + Amplitude + Mixpanel + PostHog + Adobe Analytics)
    ga4_audit_data_streams, ga4_audit_conversion_events,
    ga4_audit_custom_definitions, ga4_audit_ecommerce,
    ga4_schema_validator, ga4_check_data_anomalies,
    amplitude_check_taxonomy_health, amplitude_check_event_volume_anomalies,
    mixpanel_check_taxonomy_health, mixpanel_check_event_volume_anomalies,
    posthog_check_taxonomy_health, posthog_check_event_volume_anomalies,
    adobe_audit_report_suite, adobe_check_data_quality

  TAG MANAGER (GTM + Adobe Launch)
    gtm_audit_container, gtm_explain_tag, gtm_simulate_event,
    gtm_dependency_map, gtm_check_ga4_implementation,
    gtm_find_tracking_regression, gtm_diagnose_conversion_discrepancy,
    gtm_generate_audit_report, gtm_suggest_improvements, gtm_benchmark_health,
    gtm_audit_consent_mode    — GTM Consent Mode v2 / GDPR / CCPA audit
    adobe_launch_audit_property, adobe_launch_get_publish_history,
    adobe_audit_consent_mode  — Adobe Launch consent / CMP heuristic audit

  PAID MARKETING (Google Ads + Meta + TikTok + Snap)
    marketing_audit_budget_utilization, marketing_audit_quality_scores,
    marketing_connection_health

  MARKETING AUTOMATION (Adobe Marketo Engage)
    marketo_audit_instance     — API usage vs quota + program inventory
    marketo_check_data_quality — null-field rates on core lead fields

  WAREHOUSE (BigQuery + Redshift + Snowflake)
    Note: most warehouse audits are engine-specific. Pass `engine` in params.
    warehouse_audit_dataset           (bigquery)
    warehouse_audit_schema            (redshift | snowflake)
    warehouse_find_stale_tables       (all engines)
    warehouse_check_table_health      (redshift)
    warehouse_check_data_quality      (all engines)
    warehouse_check_empty_tables      (bigquery)
    warehouse_check_clustering_health (snowflake)

  SEO (Google Search Console)
    seo_top_movers, seo_striking_distance, seo_ctr_outliers,
    seo_sitemap_health, seo_gsc_ga4_cross_reference

Return shape: findings[] with severity + recommendation, or
{error, error_type, message}.
"""

RUN_ANALYSIS_DOC = """
Run a cross-connector composite analysis — blended reporting, attribution
modeling, incrementality, etc. These are computed insights that join data
from more than one platform, so they get their own tool (distinct from the
domain reads) with a longer default timeout.

Actions:
  cross_platform_report   — Generic blended report. params: start_date,
                            end_date, platforms[] (ga4|google_ads|meta|tiktok|snap),
                            dimensions[], metrics[], granularity.
                            If `action` is set inside params, it wins.
  blended_performance     — Totals + per-platform breakdown + blended KPIs
                            (CTR, CPC, CPA, ROAS).
  channel_comparison      — Side-by-side comparison table with spend_share_pct.
  top_campaigns           — Top N campaigns sorted by spend|roas|conversions|
                            clicks|cpa|impressions|revenue.
  revenue_attribution     — Multi-touch attribution model. Joins ad spend
                            (Ads/Meta/TikTok/Snap) + GA4 touch sequence +
                            warehouse revenue. params: start_date, end_date,
                            model (first_touch|last_touch|linear|time_decay|
                            position_based), attribution_window_days,
                            conversion_event, channels[] (optional),
                            order_id_column (warehouse join key).

For single-platform reports → use `analytics_read` or `marketing_read`.
For audits → use `run_audit`.
"""


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def _make_dispatcher(routes: dict, surface_name: str):
    """Build an async dispatcher closure for the given routing table."""

    async def dispatcher(action: str, params: dict | None = None) -> dict:
        from app.tools import specs as _specs
        from app.tools.registry import _tool_manager_ref

        tm = _tool_manager_ref["mgr"]
        params = params or {}
        _has_specs = _specs.has_tool(surface_name)

        # ── Discovery: action="describe" (or params={"describe": true}) returns
        #    the machine-readable spec for one action (or all) so any client can
        #    self-serve params without guessing. ────────────────────────────────
        if _has_specs:
            from app.tools.spec_engine import (
                DESCRIBE_ACTION,
                describe_payload,
                resolve_platform,
            )

            if action == DESCRIBE_ACTION or params.get("describe") is True:
                sp = _specs.specs_for(surface_name)
                target = params.get("action") if action == DESCRIBE_ACTION else action
                target = DEPRECATED_ACTION_ALIASES.get(surface_name, {}).get(target, target)
                return describe_payload(surface_name, sp, target, resolve_platform(params))

        # Deprecated generic Adobe Workspace names remain runtime-compatible,
        # but only their explicit replacements are advertised in tools/list.
        requested_action = action
        action = DEPRECATED_ACTION_ALIASES.get(surface_name, {}).get(action, action)
        route = routes.get(action)
        if route is None:
            err = {
                "error": True,
                "error_type": "unknown_action",
                "message": f"Unknown action '{requested_action}' for {surface_name}.",
                "available_actions": sorted(routes.keys()),
            }
            if _has_specs:
                err["hint"] = "Call action='describe' for each action's params."
            return err
        # Routes may be 2-tuple (tool, action) or 3-tuple
        # (tool, action, extra_kwargs: dict) — the 3rd slot lets us pre-
        # inject kwargs like platform="adobe_launch".
        if len(route) == 3:
            legacy_tool_name, legacy_action, extra = route
        else:
            legacy_tool_name, legacy_action = route
            extra = None
        legacy_tool = tm._legacy_tools.get(legacy_tool_name)  # type: ignore[attr-defined]
        if legacy_tool is None:
            return {
                "error": True,
                "error_type": "server_error",
                "message": f"Internal: legacy tool '{legacy_tool_name}' not found.",
            }
        # ── Self-describing validation: when the action has a spec, a missing
        #    required param returns the full required/optional list + an example
        #    in ONE round-trip (no whack-a-mole). Runtime stays permissive for
        #    actions without a spec yet. ──────────────────────────────────────
        if _has_specs:
            from app.tools.spec_engine import (
                missing_param_error,
                resolve_platform,
                validate_required,
            )

            spec = next((s for s in _specs.specs_for(surface_name) if s.action == action), None)
            if spec is not None:
                effective = dict(params)
                if extra:
                    for k, v in extra.items():
                        effective.setdefault(k, v)
                missing = validate_required(spec, effective)
                if missing:
                    return missing_param_error(spec, missing, resolve_platform(effective))
        # Build args payload the legacy tool expects
        call_args: dict = dict(params)
        if legacy_action is not None:
            call_args["action"] = legacy_action
        if extra:
            # Caller-supplied params win over route defaults
            for k, v in extra.items():
                call_args.setdefault(k, v)
        try:
            if surface_name != "run_audit":
                return await legacy_tool.run(call_args)
            # Audits persist themselves so they show up on the Audit page even
            # when the client never calls save_audit_result.
            from app.tools.audit_autosave import autosave_audit_result

            started = time.monotonic()
            result = await legacy_tool.run(call_args)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            return await autosave_audit_result(action, call_args, result, elapsed_ms)
        except Exception as exc:
            logger.exception(f"{surface_name} dispatch failed for action={action}")
            return {
                "error": True,
                "error_type": "server_error",
                "message": f"{surface_name}({action}) failed: {exc}",
            }

    dispatcher.__name__ = surface_name
    # Expose the valid actions as a JSON-Schema ``enum`` and give ``params`` a
    # clean object type (no nullable union). Strict MCP clients (Grok, OpenAI
    # strict mode) form calls from the served schema, not the prose docstring,
    # so a bare ``string`` action + ``anyOf: [object, null]`` params left them
    # guessing or timing out. Runtime stays permissive — the dispatcher body
    # still tolerates a missing/None ``params`` and unknown actions.
    _action_set = set(routes.keys()) | set(DEPRECATED_ACTION_ALIASES.get(surface_name, {}))
    # Tools backed by the spec engine also accept the reserved "describe" action.
    # It must be in the runtime Literal or pydantic arg-validation rejects the
    # call before the dispatcher body can answer it.
    from app.tools import specs as _specs

    if _specs.has_tool(surface_name):
        from app.tools.spec_engine import DESCRIBE_ACTION

        _action_set.add(DESCRIBE_ACTION)
    _actions = tuple(sorted(_action_set))
    if _actions:
        dispatcher.__annotations__ = {
            "action": Literal[_actions],  # type: ignore[valid-type]
            "params": dict,
            "return": dict,
        }
    return dispatcher


def _strictify_schema(schema) -> None:
    """Rewrite a JSON-Schema node in place to be friendly to strict MCP clients.

    The MCP/pydantic default renders every optional parameter as
    ``{"anyOf": [<type>, {"type": "null"}], "default": null}``. Claude tolerates
    this, but stricter clients (Grok, OpenAI strict mode) reject or mis-handle
    nullable unions — which is why Grok failed to form correct calls and timed
    out. We express optionality the spec-clean way instead: drop the ``null``
    branch and let ``required`` (which pydantic already omits these from) carry
    the optionality.

    Only the *served* schema (``Tool.parameters``) is touched. Runtime call
    validation uses the pydantic ``arg_model``, which is left permissive — so
    existing clients that still send ``null`` keep working.
    """
    if not isinstance(schema, dict):
        return

    for union_key in ("anyOf", "oneOf"):
        branches = schema.get(union_key)
        if isinstance(branches, list):
            non_null = [b for b in branches if not (isinstance(b, dict) and b.get("type") == "null")]
            if len(non_null) != len(branches):
                if len(non_null) == 1:
                    # Collapse the single survivor up into this node.
                    survivor = non_null[0]
                    del schema[union_key]
                    for k, v in survivor.items():
                        schema.setdefault(k, v)
                else:
                    schema[union_key] = non_null
    # A ``null`` default never matches a concrete (non-null) type and trips
    # strict validators. Optionality is carried by ``required`` omission, so a
    # null default on a typed field is pure noise — drop it.
    if "default" in schema and schema["default"] is None and schema.get("type") not in (None, "null"):
        schema.pop("default", None)

    # Recurse into every place a sub-schema can hide.
    for container_key in ("properties", "$defs", "definitions", "patternProperties"):
        container = schema.get(container_key)
        if isinstance(container, dict):
            for sub in container.values():
                _strictify_schema(sub)
    for nested_key in ("items", "additionalProperties", "not"):
        nested = schema.get(nested_key)
        if isinstance(nested, dict):
            _strictify_schema(nested)
    for union_key in ("anyOf", "oneOf", "allOf"):
        branches = schema.get(union_key)
        if isinstance(branches, list):
            for sub in branches:
                _strictify_schema(sub)


def _strictify_tool_schemas(tm) -> None:
    """Sanitize every registered tool's input schema for cross-client safety."""
    cleaned = 0
    for tool in tm._tools.values():
        params = getattr(tool, "parameters", None)
        if isinstance(params, dict):
            _strictify_schema(params)
            cleaned += 1
    logger.info("Strict-client schema sanitize applied to %d tool schemas", cleaned)


def apply_specs(tm) -> None:
    """Generate description + input schema from the spec registry.

    For every tool covered by ``app.tools.specs``, replace the served
    ``description`` and ``parameters`` with output generated from the single
    source of truth (the ActionSpec registry). Tools not in the registry are left
    untouched, so rollout is incremental and safe. The runtime arg-model is not
    touched — only what the client *sees*.
    """
    from app.tools import specs as reg
    from app.tools.spec_engine import build_input_schema, render_description

    applied = 0
    for tool_name, sp in reg.SPECS.items():
        tool = tm._tools.get(tool_name)
        if tool is None:
            continue
        actions = sorted({s.action for s in sp})
        desc = render_description(tool_name, sp, reg.header_for(tool_name), reg.footer_for(tool_name))
        schema = build_input_schema(tool_name, sp, actions, reg.encoding_for(tool_name))
        tool.description = desc
        tool.parameters = schema
        try:
            tool.fn.__doc__ = desc
        except (AttributeError, TypeError):
            pass
        applied += 1
    logger.info("Spec engine generated description+schema for %d tools", applied)


def rewire_unified_surface(mcp_server) -> None:
    """
    Rewire the MCP tool manager to expose only the unified tool surface.

    Must run AFTER every sub-module has registered its fine-grained tools.
    Legacy tool objects are preserved in ``tool_manager._legacy_tools`` so
    the unified dispatchers can still call them.
    """
    tm = mcp_server._tool_manager

    # Import config/state bits we reuse inside get_session_context
    import app.app_state as state
    from app.config import settings
    from app.tools.tool_docs import get_doc, list_docs

    # ── Preserve legacy tool objects for the dispatcher ────────────────────
    legacy_names = {
        # analytics
        "analytics_read",
        # Google Data Manager audiences
        "audience_read",
        "audience_write",
        "analytics_audit",
        "analytics_write",
        "cross_platform_report",
        # tagmanager
        "tagmanager_read",
        "tagmanager_audit",
        "tagmanager_write",
        # marketing
        "marketing_read",
        "marketing_audit",
        "marketing_write",
        # warehouse
        "warehouse_read",
        "warehouse_audit",
        "warehouse_query",
        # search console + bing webmaster
        "search_console_read",
        "search_console_audit",
        "search_console_write",
        "bing_webmaster_read",
        # knowledge
        "get_business_context",
        "list_kpis",
        "get_kpi",
        "compute_kpi",
    }
    # Tag rule book / live tag test / audit persistence are exposed as DIRECT
    # tools (they stay in the public surface), but run_audit also routes to them
    # (tag_*, live_tag_*, and the audit-persistence actions). They must be present
    # in _legacy_tools so the dispatcher can reach them — yet must NOT be removed
    # from tm._tools. Omitting them made all 21 of those run_audit routes return
    # "Internal: legacy tool '…' not found" (stress-test 2026-06-12, FINDINGS S0 #1).
    dual_exposed = {"tag_rulebook", "live_tag_test", "save_audit_result"}
    tm._legacy_tools = {  # type: ignore[attr-defined]
        n: t for n, t in tm._tools.items() if n in (legacy_names | dual_exposed)
    }
    # Also stash a reference to tm for the dispatcher closures
    from app.tools import registry as _reg

    _reg._tool_manager_ref["mgr"] = tm

    # ── Build unified tools ────────────────────────────────────────────────
    def _add(name: str, doc: str, routes: dict):
        fn = _make_dispatcher(routes, name)
        fn.__doc__ = doc
        tm.add_tool(fn, name=name)

    # Drop legacy names from _tools; we'll re-add only the unified ones
    for n in legacy_names:
        tm._tools.pop(n, None)

    # Core capability pairs
    _add("analytics_read", ANALYTICS_READ_DOC, ANALYTICS_READ_ROUTES)
    _add("analytics_write", ANALYTICS_WRITE_DOC, ANALYTICS_WRITE_ROUTES)
    _add("audience_read", AUDIENCE_READ_DOC, AUDIENCE_READ_ROUTES)
    _add("audience_write", AUDIENCE_WRITE_DOC, AUDIENCE_WRITE_ROUTES)
    _add("tagmanager_read", TAGMANAGER_READ_DOC, TAGMANAGER_READ_ROUTES)
    _add("tagmanager_write", TAGMANAGER_WRITE_DOC, TAGMANAGER_WRITE_ROUTES)
    _add("marketing_read", MARKETING_READ_DOC, MARKETING_READ_ROUTES)
    _add("marketing_write", MARKETING_WRITE_DOC, MARKETING_WRITE_ROUTES)
    _add("warehouse_read", WAREHOUSE_READ_DOC, WAREHOUSE_READ_ROUTES)
    _add("seo_read", SEO_READ_DOC, SEO_READ_ROUTES)
    _add("seo_write", SEO_WRITE_DOC, SEO_WRITE_ROUTES)
    _add("get_knowledge", KNOWLEDGE_DOC, KNOWLEDGE_ROUTES)

    # Heavy / composite surface — audits + cross-connector analyses.
    # Kept separate from the domain reads so simple reads stay lean and
    # Claude can reason about cheap vs. expensive ops at selection time.
    _add("run_audit", RUN_AUDIT_DOC, AUDIT_ROUTES)
    _add("run_analysis", RUN_ANALYSIS_DOC, ANALYSIS_ROUTES)

    # warehouse_query is a direct rename of the legacy warehouse_query (SQL exec)
    _wq_legacy = tm._legacy_tools.get("warehouse_query")  # type: ignore[attr-defined]
    if _wq_legacy is not None:

        async def warehouse_query(action: str = "run_query", params: dict | None = None) -> dict:
            call_args = dict(params or {})
            call_args["action"] = action
            return await _wq_legacy.run(call_args)

        warehouse_query.__doc__ = WAREHOUSE_QUERY_DOC
        tm.add_tool(warehouse_query, name="warehouse_query")

    # ── get_session_context (replaces get_connection_status + tool_help + get_active_project) ─
    async def get_session_context(tool_name: str | None = None) -> dict:
        # Tool-specific doc mode
        if tool_name:
            doc = get_doc(tool_name)
            available = sorted(tm._tools.keys())
            if doc is None:
                return {
                    "error": True,
                    "error_type": "not_found",
                    "message": f"No detailed doc for '{tool_name}'.",
                    "available_tools": available,
                    "available_docs": list_docs(),
                }
            return {"tool_name": tool_name, "doc": doc}

        # Full session context mode
        u = state.current_user_ctx.get()
        base = settings.APP_BASE_URL
        if not u:
            return {
                "error": True,
                "error_type": "unauthenticated",
                "message": "No active MCP session found.",
                "action_required": f"Visit {base} to sign in.",
            }

        p = state.current_project_ctx.get()
        if not p:
            if len(u.projects) == 0:
                return {
                    "error": True,
                    "error_type": "no_projects",
                    "message": "You don't belong to any projects yet.",
                    "action_required": f"Visit {base}/projects to create one.",
                }
            return {
                "error": True,
                "error_type": "no_active_project",
                "message": "No project is active. Use set_active_project to select one.",
                "available_projects": [
                    {"name": pr.project_name, "slug": pr.project_slug} for pr in u.projects
                ],
            }

        connected = []
        not_connected = []
        platforms = [
            ("GA4 / Product Analytics", p.has_ga4, f"{base}/connect"),
            ("GTM / Tag Manager", p.has_gtm, f"{base}/connect"),
            ("Google Ads", p.has_ads, f"{base}/connect"),
            ("Google Search Console", getattr(p, "has_gsc", False), f"{base}/connect"),
            ("Google Data Manager", getattr(p, "has_data_manager", False), f"{base}/connect"),
            ("BigQuery", p.has_bq, f"{base}/connect/bigquery"),
            ("Meta Ads", p.has_meta, f"{base}/connect/meta"),
            ("TikTok Ads", p.has_tiktok, f"{base}/connect/tiktok"),
            ("Snapchat Ads", p.has_snap, f"{base}/connect/snap"),
            ("LinkedIn Ads", getattr(p, "has_linkedin", False), f"{base}/connect/linkedin"),
            ("Pinterest Ads", getattr(p, "has_pinterest", False), f"{base}/connect/pinterest"),
            ("X Ads", getattr(p, "has_x", False), f"{base}/connect/x"),
            ("Reddit Ads", getattr(p, "has_reddit", False), f"{base}/connect/reddit"),
            ("Apple Ads", getattr(p, "has_apple", False), f"{base}/connect/apple"),
            ("Bing Webmaster Tools", getattr(p, "has_bing", False), f"{base}/connect/bing"),
            ("Amplitude", p.has_amplitude, f"{base}/connect/amplitude"),
            ("Mixpanel", p.has_mixpanel, f"{base}/connect/mixpanel"),
            ("PostHog", p.has_posthog, f"{base}/connect/posthog"),
            ("Adobe Analytics", p.has_adobe_analytics, f"{base}/connect/adobe"),
            ("Adobe Launch", p.has_adobe_launch, f"{base}/connect/adobe"),
            ("Redshift", p.has_redshift, f"{base}/connect/redshift"),
            ("Snowflake", p.has_snowflake, f"{base}/connect/snowflake"),
        ]
        for name, is_c, url in platforms:
            (connected if is_c else not_connected).append(
                name if is_c else {"platform": name, "connect_url": url}
            )

        # Brief tool map
        tool_summary = sorted(tm._tools.keys())
        return {
            "user_email": u.email,
            "active_project": {
                "project_id": p.project_id,
                "name": p.project_name,
                "slug": p.project_slug,
                "your_role": p.role,
            },
            "connected_platforms": connected,
            "disconnected_platforms": not_connected,
            "available_tools": tool_summary,
            "hint": "Pass tool_name=<name> to get detailed action docs for any tool.",
        }

    get_session_context.__doc__ = SESSION_CONTEXT_DOC
    tm.add_tool(get_session_context, name="get_session_context")

    # ── generic_tool_read / generic_tool_write escape hatches ──────────────
    async def generic_tool_read(capability: str, action: str | None = None, args: dict | None = None) -> dict:
        """Generic escape hatch: dispatches to any legacy tool or state connector."""
        call_args = dict(args or {})
        from app.tools.registry import caller_has_full_access, inner_tool_permitted

        if capability in tm._legacy_tools:
            tool = tm._legacy_tools[capability]
            if action is not None:
                call_args["action"] = action
            if not await inner_tool_permitted(capability, call_args):
                return {
                    "error": True,
                    "error_type": "permission_denied",
                    "message": f"Your role does not grant access to '{capability}'.",
                }
            return await tool.run(call_args)

        # Raw connector methods have no domain mapping — owners/admins only.
        if not await caller_has_full_access():
            return {
                "error": True,
                "error_type": "permission_denied",
                "message": "Direct connector calls need owner or admin access in this project.",
            }
        try:
            from app.context import get_state

            state = get_state()
            conn = getattr(state, f"{capability}_connector", None) or getattr(state, capability, None)
            if conn and action and hasattr(conn, action):
                fn = getattr(conn, action)
                import inspect

                if inspect.iscoroutinefunction(fn):
                    return await fn(**call_args)
                return fn(**call_args)
        except Exception as ex:
            return {"error": True, "error_type": "connector_error", "message": str(ex)}

        return {
            "error": True,
            "error_type": "unknown_capability",
            "message": f"No handler registered for capability='{capability}', action='{action}'.",
        }

    generic_tool_read.__doc__ = GENERIC_READ_DOC
    tm.add_tool(generic_tool_read, name="generic_tool_read")

    async def generic_tool_write(
        capability: str, action: str | None = None, args: dict | None = None
    ) -> dict:
        """Generic escape hatch for write operations: dispatches to legacy tools or state connectors."""
        call_args = dict(args or {})
        from app.tools.registry import caller_has_full_access, inner_tool_permitted

        if capability in tm._legacy_tools:
            tool = tm._legacy_tools[capability]
            if action is not None:
                call_args["action"] = action
            if not await inner_tool_permitted(capability, call_args):
                return {
                    "error": True,
                    "error_type": "permission_denied",
                    "message": f"Your role does not grant access to '{capability}'.",
                }
            return await tool.run(call_args)

        # Raw connector methods have no domain mapping — owners/admins only.
        if not await caller_has_full_access():
            return {
                "error": True,
                "error_type": "permission_denied",
                "message": "Direct connector calls need owner or admin access in this project.",
            }
        try:
            from app.context import get_state

            state = get_state()
            conn = getattr(state, f"{capability}_connector", None) or getattr(state, capability, None)
            if conn and action and hasattr(conn, action):
                fn = getattr(conn, action)
                import inspect

                if inspect.iscoroutinefunction(fn):
                    return await fn(**call_args)
                return fn(**call_args)
        except Exception as ex:
            return {"error": True, "error_type": "connector_error", "message": str(ex)}

        return {
            "error": True,
            "error_type": "unknown_capability",
            "message": f"No write handler registered for capability='{capability}', action='{action}'.",
        }

    generic_tool_write.__doc__ = GENERIC_WRITE_DOC
    tm.add_tool(generic_tool_write, name="generic_tool_write")

    # ── Spec engine: generate description + input schema from the single
    #    source of truth for every tool the registry covers. Runs before the
    #    strict pass so generated schemas are also sanitized. ────────────────
    apply_specs(tm)

    # ── Final pass: make every served schema strict-client friendly ───────
    # Runs LAST so it covers the unified dispatchers, the direct-survivor
    # tools, and any tool registered before the rewire.
    _strictify_tool_schemas(tm)

    logger.info(f"Unified tool surface active — {len(tm._tools)} tools: {sorted(tm._tools.keys())}")
