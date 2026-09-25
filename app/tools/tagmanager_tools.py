"""
Tag Manager Mega Tools — 3-tool split pattern
Mirrors the analytics_tools pattern: tagmanager_read / tagmanager_audit / tagmanager_write

All tools route through action → GTM connector method.
User identity is never a parameter — always resolved from MCP session via app_state.

CoercedStr uses Pydantic BeforeValidator(str) so numeric IDs sent as JSON integers
are silently coerced to strings before validation — no nested 'args' wrapper needed.
"""

import asyncio
from typing import Annotated, Literal

from pydantic import BeforeValidator

import app.app_state as state
from app.auth.mcp_session_manager import no_gtm_response
from app.cache import cached_tool_response
from app.config import settings

# Flat string type that accepts integers and coerces them — used on all ID params.
CoercedStr = Annotated[str, BeforeValidator(str)]


def _user():
    return state.current_user_ctx.get()


def _conn():
    """Return Google OAuth connection_id, scoped to active project."""
    from app.tools.shared_helpers import get_google_conn_id

    return get_google_conn_id()


def _no_gtm():
    return no_gtm_response(settings.APP_BASE_URL)


def _no_adobe_launch():
    from app.auth.mcp_session_manager import no_adobe_launch_response

    return no_adobe_launch_response(settings.APP_BASE_URL)


async def _get_adobe_launch_conn(user_id: str):
    """Fetch active Adobe connection with Launch enabled, scoped to active project."""
    from app.models.credential_connection import AdobeConnection
    from app.tools.shared_helpers import decrypt_field, get_encrypted_credential_conn

    conn = await get_encrypted_credential_conn(
        AdobeConnection,
        user_id,
        extra_filters=[AdobeConnection.has_launch == True],
    )
    if not conn:
        return None, None, None, None
    client_id = decrypt_field(conn.client_id_encrypted)
    client_secret = decrypt_field(conn.client_secret_encrypted)
    return str(conn.id), client_id, client_secret, conn.org_id


async def _fetch_all(account_id, container_id):
    """Fetch tags, triggers, variables in parallel."""
    gtm = state.gtm_connector
    conn = _conn()
    tags_r, triggers_r, variables_r = await asyncio.gather(
        gtm.list_tags(conn, account_id, container_id),
        gtm.list_triggers(conn, account_id, container_id),
        gtm.list_variables(conn, account_id, container_id),
    )
    return tags_r["tags"], triggers_r["triggers"], variables_r["variables"]


# ---------------------------------------------------------------------------
# GTM built-in trigger mapping
# These triggers exist in every GTM container but are not returned by the API.
# ---------------------------------------------------------------------------
_BUILTIN_TRIGGERS = {
    "2147479553": {"name": "All Pages", "type": "pageview", "event": "page_view"},
    "2147479573": {"name": "Initialization - All Pages", "type": "init", "event": "gtm.init"},
    "2147479572": {
        "name": "Consent Initialization - All Pages",
        "type": "consentInit",
        "event": "gtm.init_consent",
    },
    "2147479574": {"name": "DOM Ready", "type": "domReady", "event": "dom_ready"},
    "2147479575": {"name": "Window Loaded", "type": "windowLoaded", "event": "window_loaded"},
}


def _resolve_trigger_name(trigger_id_or_name: str, custom_trigger_map: dict | None = None) -> str:
    """Resolve a trigger ID to its human-readable name.

    Checks built-in triggers first, then custom triggers, then returns the raw ID.
    """
    tid = str(trigger_id_or_name)
    builtin = _BUILTIN_TRIGGERS.get(tid)
    if builtin:
        return builtin["name"]
    if custom_trigger_map and tid in custom_trigger_map:
        return custom_trigger_map[tid]
    return trigger_id_or_name


def register_tagmanager_tools(mcp_server):
    # -------------------------------------------------------------------------
    # tagmanager_read — Layer 1: Discovery / data access
    # -------------------------------------------------------------------------

    @mcp_server.tool("tagmanager_read")
    async def tagmanager_read(
        platform: Literal["gtm", "adobe_launch"] = "gtm",
        action: str | None = None,
        account_id: CoercedStr | None = None,
        container_id: CoercedStr | None = None,
        workspace_id: CoercedStr = "0",
        tag_id: CoercedStr | None = None,
    ) -> dict:
        """Reads tag manager data. Use tagmanager_audit for health, tagmanager_write for changes.

        platform: gtm | adobe_launch

        GTM: list_containers, get_container_summary(acct+ctr), list_workspaces(acct+ctr),
          list_tags(acct+ctr), list_triggers(acct+ctr), list_variables(acct+ctr), get_tag_detail(acct+ctr+tag_id)
        Adobe Launch: list_companies, list_properties(account_id), get_property(container_id),
          list_rules(container_id), get_rule(tag_id), list_data_elements(container_id),
          list_extensions(container_id), list_environments(container_id), list_libraries(container_id),
          list_builds(workspace_id)
        """
        u = _user()

        ADOBE_LAUNCH_READ_ACTIONS = {
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
        }
        if action in ADOBE_LAUNCH_READ_ACTIONS and platform == "gtm":
            platform = "adobe_launch"

        if platform == "gtm":
            if not u or not u.has_gtm:
                return _no_gtm()

            conn_id = _conn()
            if not conn_id:
                return _no_gtm()
            if action == "list_accounts":
                return await cached_tool_response(
                    f"cache:gtm:accounts:{conn_id}",
                    600,
                    state.gtm_connector.list_accounts,
                    conn_id,
                )
            if action == "list_containers":
                return await cached_tool_response(
                    f"cache:gtm:containers:{conn_id}:{account_id or 'all'}",
                    600,
                    state.gtm_connector.list_containers,
                    conn_id,
                    account_id,
                )

            # All below require account_id and container_id
            if not account_id or not container_id:
                return {"error": True, "message": f"account_id and container_id are required for '{action}'"}

            if action == "get_container_summary":
                return await cached_tool_response(
                    f"cache:gtm:summary:{conn_id}:{account_id}:{container_id}",
                    300,
                    state.gtm_connector.get_container_summary,
                    conn_id,
                    account_id,
                    container_id,
                )
            elif action == "list_workspaces":
                return await cached_tool_response(
                    f"cache:gtm:workspaces:{conn_id}:{account_id}:{container_id}",
                    300,
                    state.gtm_connector.list_workspaces,
                    conn_id,
                    account_id,
                    container_id,
                )
            elif action == "list_tags":
                return await cached_tool_response(
                    f"cache:gtm:tags:{conn_id}:{account_id}:{container_id}:{workspace_id}",
                    300,
                    state.gtm_connector.list_tags,
                    conn_id,
                    account_id,
                    container_id,
                    workspace_id,
                )
            elif action == "list_triggers":
                return await cached_tool_response(
                    f"cache:gtm:triggers:{conn_id}:{account_id}:{container_id}:{workspace_id}",
                    300,
                    state.gtm_connector.list_triggers,
                    conn_id,
                    account_id,
                    container_id,
                    workspace_id,
                )
            elif action == "list_variables":
                return await cached_tool_response(
                    f"cache:gtm:variables:{conn_id}:{account_id}:{container_id}:{workspace_id}",
                    300,
                    state.gtm_connector.list_variables,
                    conn_id,
                    account_id,
                    container_id,
                    workspace_id,
                )
            elif action == "get_tag_detail":
                if not tag_id:
                    return {"error": True, "message": "tag_id is required for get_tag_detail"}
                return await cached_tool_response(
                    f"cache:gtm:tag:{conn_id}:{account_id}:{container_id}:{tag_id}:{workspace_id}",
                    300,
                    state.gtm_connector.get_tag_detail,
                    conn_id,
                    account_id,
                    container_id,
                    tag_id,
                    workspace_id,
                )

            return {"error": True, "message": f"Unknown action '{action}' for GTM tagmanager_read"}

        elif platform == "adobe_launch":
            if not u or not u.has_adobe_launch:
                return _no_adobe_launch()
            conn_id, client_id, client_secret, org_id = await _get_adobe_launch_conn(u.user_id)
            if not client_id:
                return _no_adobe_launch()
            launch = state.adobe_launch_connector

            if action == "list_companies":
                return await cached_tool_response(
                    f"cache:launch:companies:{conn_id}",
                    600,
                    launch.list_companies,
                    client_id,
                    client_secret,
                    org_id,
                )
            elif action == "list_properties":
                return await cached_tool_response(
                    f"cache:launch:props:{conn_id}:{account_id or 'auto'}",
                    300,
                    launch.list_properties,
                    client_id,
                    client_secret,
                    org_id,
                    account_id,
                )
            elif action == "get_property":
                prop_id = container_id or account_id or tag_id
                if not prop_id:
                    return {
                        "error": True,
                        "message": "container_id (property_id) is required for get_property",
                    }
                return await launch.get_property(client_id, client_secret, org_id, prop_id)
            elif action == "list_rules":
                prop_id = container_id or account_id
                if not prop_id:
                    return {"error": True, "message": "container_id (property_id) is required for list_rules"}
                return await cached_tool_response(
                    f"cache:launch:rules:{conn_id}:{prop_id}",
                    300,
                    launch.list_rules,
                    client_id,
                    client_secret,
                    org_id,
                    prop_id,
                )
            elif action == "get_rule":
                rule_id = tag_id or container_id
                if not rule_id:
                    return {"error": True, "message": "tag_id (rule_id) is required for get_rule"}
                return await launch.get_rule(client_id, client_secret, org_id, rule_id)
            elif action == "list_rule_components":
                rule_id = tag_id or container_id
                if not rule_id:
                    return {"error": True, "message": "tag_id (rule_id) is required for list_rule_components"}
                return await cached_tool_response(
                    f"cache:launch:rule_components:{conn_id}:{rule_id}",
                    300,
                    launch.list_rule_components,
                    client_id,
                    client_secret,
                    org_id,
                    rule_id,
                )
            elif action == "get_rule_component":
                comp_id = tag_id or container_id
                if not comp_id:
                    return {
                        "error": True,
                        "message": "tag_id (rule_component_id) is required for get_rule_component",
                    }
                return await launch.get_rule_component(client_id, client_secret, org_id, comp_id)
            elif action == "list_data_elements":
                prop_id = container_id or account_id
                if not prop_id:
                    return {
                        "error": True,
                        "message": "container_id (property_id) is required for list_data_elements",
                    }
                return await cached_tool_response(
                    f"cache:launch:data_elements:{conn_id}:{prop_id}",
                    300,
                    launch.list_data_elements,
                    client_id,
                    client_secret,
                    org_id,
                    prop_id,
                )
            elif action == "get_data_element":
                element_id = tag_id or container_id
                if not element_id:
                    return {
                        "error": True,
                        "message": "tag_id (data_element_id) is required for get_data_element",
                    }
                return await launch.get_data_element(client_id, client_secret, org_id, element_id)
            elif action == "list_extensions":
                prop_id = container_id or account_id
                if not prop_id:
                    return {
                        "error": True,
                        "message": "container_id (property_id) is required for list_extensions",
                    }
                return await cached_tool_response(
                    f"cache:launch:extensions:{conn_id}:{prop_id}",
                    300,
                    launch.list_extensions,
                    client_id,
                    client_secret,
                    org_id,
                    prop_id,
                )
            elif action == "list_environments":
                prop_id = container_id or account_id
                if not prop_id:
                    return {
                        "error": True,
                        "message": "container_id (property_id) is required for list_environments",
                    }
                return await launch.list_environments(client_id, client_secret, org_id, prop_id)
            elif action == "list_libraries":
                prop_id = container_id or account_id
                if not prop_id:
                    return {
                        "error": True,
                        "message": "container_id (property_id) is required for list_libraries",
                    }
                return await launch.list_libraries(client_id, client_secret, org_id, prop_id)
            elif action == "list_builds":
                lib_id = workspace_id if workspace_id and workspace_id != "0" else (container_id or tag_id)
                if not lib_id:
                    return {"error": True, "message": "workspace_id (library_id) is required for list_builds"}
                return await launch.list_builds(client_id, client_secret, org_id, lib_id)
            return {"error": True, "message": f"Unknown action '{action}' for Adobe Launch tagmanager_read"}

        return {"error": True, "message": f"Unknown platform '{platform}'"}

    # -------------------------------------------------------------------------
    # tagmanager_audit — Layer 2: Intelligence / health analysis
    # -------------------------------------------------------------------------

    @mcp_server.tool("tagmanager_audit")
    async def tagmanager_audit(
        platform: Literal["gtm", "adobe_launch"] = "gtm",
        action: str | None = None,
        account_id: CoercedStr | None = None,
        container_id: CoercedStr | None = None,
        tag_id: CoercedStr | None = None,
        event_type: str | None = None,
        ga4_property_id: str | None = None,
        date_range_start: str | None = None,
        date_range_end: str | None = None,
        customer_id: str | None = None,
    ) -> dict:
        """
        Audits and analyses tag manager configurations.
        Returns scored findings, dependency maps, and actionable recommendations.

        platform: 'gtm' | 'adobe_launch'

        GTM Actions:
          audit_container              — full health score: tags without triggers, duplicate configs, hygiene issues
                                         (account_id + container_id required)
          explain_tag                  — plain-English explanation of what a tag does and when it fires
                                         (account_id + container_id + tag_id required)
          simulate_event               — show which triggers and tags fire for a given event type
                                         (account_id + container_id + event_type required)
          dependency_map               — tag → trigger edge list showing firing relationships
                                         (account_id + container_id required)
          check_ga4_implementation     — verify GA4 config tag setup; flag duplicates
                                         (account_id + container_id required)
          find_tracking_regression     — detect recently broken tags (requires dates)
          diagnose_conversion_discrepancy — cross-reference GTM and GA4/Ads conversion data
          generate_audit_report        — comprehensive markdown audit report
          suggest_improvements         — AI-powered tag/trigger hygiene recommendations
          benchmark_health             — score container against industry best practices

        Adobe Launch Actions:
          audit_property               — full property health score and configuration review (container_id required)
          get_publish_history          — retrieve publish/deployment history (container_id required)
        """
        u = _user()

        if platform == "gtm":
            if not u or not u.has_gtm:
                return _no_gtm()
            conn = _conn()
            gtm = state.gtm_connector

            if action == "audit_container":
                tags, triggers, variables = await _fetch_all(account_id, container_id)
                summary = await gtm.get_container_summary(conn, account_id, container_id)
                issues = []

                # Check for tags without firing triggers
                for t in tags:
                    if not t.get("firing_triggers"):
                        issues.append(
                            {
                                "severity": "critical",
                                "category": "tags",
                                "issue": f"Tag '{t.get('tag_name')}' has no firing triggers",
                            }
                        )

                # BUG-4: Check for orphaned triggers (not used by any tag)
                for tr in triggers:
                    if not tr.get("tags_using_trigger"):
                        issues.append(
                            {
                                "severity": "warning",
                                "category": "triggers",
                                "issue": f"Trigger '{tr.get('trigger_name')}' has no tags using it — orphaned",
                            }
                        )

                critical = sum(1 for i in issues if i.get("severity") == "critical")
                warning = sum(1 for i in issues if i.get("severity") == "warning")
                score = max(0, 100 - critical * 20 - warning * 5)
                result = {
                    "score": score,
                    "total_tags": len(tags),
                    "total_triggers": len(triggers),
                    "total_variables": len(variables),
                    "issues": issues,
                }
                return result

            elif action == "explain_tag":
                if not tag_id:
                    return {"error": True, "message": "tag_id is required for explain_tag"}
                tag = await gtm.get_tag_detail(conn, account_id, container_id, tag_id)

                # Build a custom trigger map for name resolution
                _, triggers_list, _ = await _fetch_all(account_id, container_id)
                custom_map = {
                    tr["trigger_id"]: tr["trigger_name"] for tr in triggers_list if tr.get("trigger_id")
                }

                # Extract meaningful parameters
                params = {}
                for p in tag.get("parameter", []):
                    key = p.get("key", "")
                    val = p.get("value", p.get("list", ""))
                    if key and val:
                        params[key] = val

                # Resolve firing and blocking trigger names
                firing = [_resolve_trigger_name(tid, custom_map) for tid in tag.get("firingTriggerId", [])]
                blocking = [
                    _resolve_trigger_name(tid, custom_map) for tid in tag.get("blockingTriggerId", [])
                ]

                # Map tag type to human-readable description
                tag_type = tag.get("type", "unknown")
                type_descriptions = {
                    "googtag": "Google Tag (GA4 configuration tag)",
                    "gaawc": "GA4 Event Tag",
                    "ua": "Universal Analytics Tag (deprecated)",
                    "html": "Custom HTML Tag",
                    "img": "Custom Image / Pixel Tag",
                    "awct": "Google Ads Conversion Tracking Tag",
                    "gaawr": "Google Ads Remarketing Tag",
                    "flc": "Floodlight Counter Tag",
                    "fls": "Floodlight Sales Tag",
                    "gclidw": "Google Ads Conversion Linker",
                }
                type_desc = type_descriptions.get(tag_type, f"{tag_type} tag")

                # Build explanation
                measurement_id = params.get("tagId", params.get("measurementId", ""))
                mid_note = f" sending data to measurement ID {measurement_id}" if measurement_id else ""
                fires_on = ", ".join(firing) if firing else "no triggers (will never fire)"
                blocks_on = ", ".join(blocking) if blocking else None

                explanation = f"This is a {type_desc}{mid_note}. It fires on: {fires_on}."
                if blocks_on:
                    explanation += f" It is blocked by: {blocks_on}."
                if tag.get("paused"):
                    explanation += " Note: this tag is currently PAUSED."

                return {
                    "tag_name": tag.get("name"),
                    "tag_type": tag_type,
                    "type_description": type_desc,
                    "explanation": explanation,
                    "firing_triggers": firing,
                    "blocking_triggers": blocking,
                    "is_paused": tag.get("paused", False),
                    "parameters": params,
                }

            elif action == "simulate_event":
                if not event_type:
                    return {"error": True, "message": "event_type is required for simulate_event"}
                tags, triggers, _ = await _fetch_all(account_id, container_id)

                # Match custom triggers by type
                firing_triggers = [
                    tr["trigger_name"]
                    for tr in triggers
                    if tr.get("trigger_type", "").lower() == event_type.lower()
                ]

                # Match built-in triggers by event name
                ev_lower = event_type.lower().replace(" ", "_")
                for tid, info in _BUILTIN_TRIGGERS.items():
                    if (
                        info["event"].lower() == ev_lower
                        or info["type"].lower() == ev_lower
                        or info["name"].lower().replace(" ", "_") == ev_lower
                    ):
                        firing_triggers.append(info["name"])

                # Find tags that fire on matched triggers
                tags_firing = []
                for t in tags:
                    tag_triggers = t.get("firing_triggers", [])
                    for ft in firing_triggers:
                        if ft in tag_triggers:
                            tags_firing.append(t["tag_name"])
                            break
                    # Also check built-in trigger IDs in raw firingTriggerId
                    for tid, info in _BUILTIN_TRIGGERS.items():
                        if info["name"] in firing_triggers:
                            # The list_tags connector already resolves IDs,
                            # but check raw ID match too
                            if tid in [str(x) for x in t.get("firing_triggers", [])]:
                                if t["tag_name"] not in tags_firing:
                                    tags_firing.append(t["tag_name"])

                return {
                    "event_type": event_type,
                    "triggers_firing": firing_triggers,
                    "tags_firing": tags_firing,
                }

            elif action == "dependency_map":
                tags, triggers, variables = await _fetch_all(account_id, container_id)
                # Build custom trigger ID→name map for resolution
                custom_map = {tr["trigger_id"]: tr["trigger_name"] for tr in triggers if tr.get("trigger_id")}
                edges = [
                    {"tag": t["tag_name"], "fires_on": _resolve_trigger_name(tr, custom_map)}
                    for t in tags
                    for tr in t.get("firing_triggers", [])
                ]
                return {"edges": edges, "total_tags": len(tags)}

            elif action == "check_ga4_implementation":
                tags, _, _ = await _fetch_all(account_id, container_id)
                config_tags = [t for t in tags if t.get("tag_type") in ("googtag", "gaawc")]
                return {
                    "config_tag_count": len(config_tags),
                    "has_duplicate_config": len(config_tags) > 1,
                    "config_tags": [
                        {"name": t.get("tag_name"), "type": t.get("tag_type")} for t in config_tags
                    ],
                }

            elif action == "generate_audit_report":
                import asyncio

                async def _audit_data():
                    tags_f = gtm.list_tags(conn, account_id, container_id)
                    triggers_f = gtm.list_triggers(conn, account_id, container_id)
                    variables_f = gtm.list_variables(conn, account_id, container_id)
                    summary_f = gtm.get_container_summary(conn, account_id, container_id)
                    tags_r, triggers_r, variables_r, summary_r = await asyncio.gather(
                        tags_f,
                        triggers_f,
                        variables_f,
                        summary_f,
                    )
                    return (
                        tags_r["tags"],
                        triggers_r["triggers"],
                        variables_r["variables"],
                        summary_r,
                    )

                try:
                    tags, triggers, variables, summary = await asyncio.wait_for(
                        _audit_data(),
                        timeout=25,
                    )
                except TimeoutError:
                    return {
                        "error": True,
                        "message": (
                            "Audit timed out — the container has too many entities "
                            "to audit in one call. Try auditing a single aspect "
                            "instead (e.g. audit_container or check_ga4_implementation)."
                        ),
                    }

                # --- Tag analysis ---
                tag_types: dict = {}
                orphan_tags = []
                paused_tags = []
                ua_tags = []
                html_tags = []
                for t in tags:
                    ttype = t.get("tag_type", "unknown")
                    tag_types[ttype] = tag_types.get(ttype, 0) + 1
                    if not t.get("firing_triggers"):
                        orphan_tags.append(t.get("tag_name"))
                    if t.get("is_paused"):
                        paused_tags.append(t.get("tag_name"))
                    if ttype == "ua":
                        ua_tags.append(t.get("tag_name"))
                    if ttype == "html":
                        html_tags.append(t.get("tag_name"))

                # --- Trigger analysis ---
                trigger_types: dict = {}
                unused_triggers = []
                for tr in triggers:
                    ttype = tr.get("trigger_type", "unknown")
                    trigger_types[ttype] = trigger_types.get(ttype, 0) + 1
                    if not tr.get("tags_using_trigger"):
                        unused_triggers.append(tr.get("trigger_name"))

                # --- Variable analysis ---
                unused_variables = [
                    v.get("variable_name") for v in variables if not v.get("tags_using_variable")
                ]

                # --- GA4 check ---
                config_tags = [t for t in tags if t.get("tag_type") in ("googtag", "gaawc")]

                # --- Scoring ---
                issues = []
                for name in orphan_tags:
                    issues.append(
                        {
                            "severity": "critical",
                            "category": "tags",
                            "issue": f"Tag '{name}' has no firing trigger — it will never execute",
                        }
                    )
                for name in ua_tags:
                    issues.append(
                        {
                            "severity": "warning",
                            "category": "migration",
                            "issue": f"Tag '{name}' uses deprecated Universal Analytics (UA) — migrate to GA4",
                        }
                    )
                if len(config_tags) > 1:
                    issues.append(
                        {
                            "severity": "warning",
                            "category": "ga4",
                            "issue": f"Found {len(config_tags)} GA4 config tags — duplicates may cause double-counting",
                        }
                    )
                for name in unused_triggers:
                    issues.append(
                        {
                            "severity": "info",
                            "category": "triggers",
                            "issue": f"Trigger '{name}' is not used by any tag — consider removing",
                        }
                    )
                for name in unused_variables:
                    issues.append(
                        {
                            "severity": "info",
                            "category": "variables",
                            "issue": f"Variable '{name}' is not referenced by any tag — consider removing",
                        }
                    )
                for name in html_tags:
                    issues.append(
                        {
                            "severity": "info",
                            "category": "security",
                            "issue": f"Custom HTML tag '{name}' — review for XSS or performance impact",
                        }
                    )

                critical = sum(1 for i in issues if i["severity"] == "critical")
                warning = sum(1 for i in issues if i["severity"] == "warning")
                info = sum(1 for i in issues if i["severity"] == "info")
                score = max(0, 100 - critical * 20 - warning * 10 - info * 2)

                return {
                    "score": score,
                    "container_id": container_id,
                    "account_id": account_id,
                    "summary": {
                        "total_tags": len(tags),
                        "total_triggers": len(triggers),
                        "total_variables": len(variables),
                        "workspace_count": summary.get("workspace_count", 0),
                        "last_published_at": summary.get("last_published_at"),
                    },
                    "tag_type_breakdown": tag_types,
                    "trigger_type_breakdown": trigger_types,
                    "findings": {
                        "critical": critical,
                        "warning": warning,
                        "info": info,
                    },
                    "issues": issues,
                    "highlights": {
                        "orphan_tags": orphan_tags,
                        "paused_tags": paused_tags,
                        "ua_legacy_tags": ua_tags,
                        "custom_html_tags": html_tags,
                        "unused_triggers": unused_triggers,
                        "unused_variables": unused_variables,
                        "ga4_config_tags": [t.get("tag_name") for t in config_tags],
                    },
                }

            elif action == "suggest_improvements":
                tags, triggers, variables = await _fetch_all(account_id, container_id)
                recommendations = []

                # 1. UA → GA4 migration
                ua_tags = [t for t in tags if t.get("tag_type") == "ua"]
                if ua_tags:
                    recommendations.append(
                        {
                            "priority": "high",
                            "category": "migration",
                            "title": "Migrate Universal Analytics tags to GA4",
                            "detail": f"{len(ua_tags)} UA tag(s) found: {', '.join(t.get('tag_name', '') for t in ua_tags)}. "
                            "Universal Analytics stopped processing data on July 1 2024. Replace with GA4 event tags.",
                            "effort": "medium",
                        }
                    )

                # 2. Orphan tags
                orphans = [t for t in tags if not t.get("firing_triggers")]
                if orphans:
                    recommendations.append(
                        {
                            "priority": "high",
                            "category": "hygiene",
                            "title": "Remove or fix tags with no firing triggers",
                            "detail": f"{len(orphans)} tag(s) will never fire: {', '.join(t.get('tag_name', '') for t in orphans)}. "
                            "Either attach a trigger or delete them to reduce container weight.",
                            "effort": "low",
                        }
                    )

                # 3. Duplicate GA4 config
                config_tags = [t for t in tags if t.get("tag_type") in ("googtag", "gaawc")]
                if len(config_tags) > 1:
                    recommendations.append(
                        {
                            "priority": "high",
                            "category": "ga4",
                            "title": "Consolidate duplicate GA4 config tags",
                            "detail": f"Found {len(config_tags)} config tags: {', '.join(t.get('tag_name', '') for t in config_tags)}. "
                            "Multiple config tags can cause double pageview counting. Keep one and remove the rest.",
                            "effort": "low",
                        }
                    )

                # 4. Custom HTML review
                html_tags = [t for t in tags if t.get("tag_type") == "html"]
                if html_tags:
                    recommendations.append(
                        {
                            "priority": "medium",
                            "category": "security",
                            "title": "Audit custom HTML tags for security and performance",
                            "detail": f"{len(html_tags)} custom HTML tag(s): {', '.join(t.get('tag_name', '') for t in html_tags)}. "
                            "Custom HTML bypasses GTM's built-in safety. Review for XSS risks, synchronous scripts, and document.write usage.",
                            "effort": "medium",
                        }
                    )

                # 5. Unused triggers
                unused_triggers = [tr for tr in triggers if not tr.get("tags_using_trigger")]
                if unused_triggers:
                    recommendations.append(
                        {
                            "priority": "low",
                            "category": "cleanup",
                            "title": "Remove unused triggers",
                            "detail": f"{len(unused_triggers)} trigger(s) are not attached to any tag: "
                            f"{', '.join(tr.get('trigger_name', '') for tr in unused_triggers)}. "
                            "These add clutter and slow down container load evaluation.",
                            "effort": "low",
                        }
                    )

                # 6. Unused variables
                unused_vars = [v for v in variables if not v.get("tags_using_variable")]
                if unused_vars:
                    recommendations.append(
                        {
                            "priority": "low",
                            "category": "cleanup",
                            "title": "Remove unused variables",
                            "detail": f"{len(unused_vars)} variable(s) not referenced: "
                            f"{', '.join(v.get('variable_name', '') for v in unused_vars)}.",
                            "effort": "low",
                        }
                    )

                # 7. Paused tags
                paused = [t for t in tags if t.get("is_paused")]
                if paused:
                    recommendations.append(
                        {
                            "priority": "low",
                            "category": "cleanup",
                            "title": "Decide on paused tags",
                            "detail": f"{len(paused)} paused tag(s): {', '.join(t.get('tag_name', '') for t in paused)}. "
                            "Paused tags still add to container size. Delete if no longer needed.",
                            "effort": "low",
                        }
                    )

                # 8. Consent mode check
                recommendations.append(
                    {
                        "priority": "medium",
                        "category": "privacy",
                        "title": "Verify Consent Mode v2 implementation",
                        "detail": "Google requires Consent Mode v2 for EU traffic since March 2024. "
                        "Ensure your consent management platform is integrated and tags respect consent signals.",
                        "effort": "medium",
                    }
                )

                return {
                    "container_id": container_id,
                    "total_recommendations": len(recommendations),
                    "recommendations": recommendations,
                }

            elif action == "benchmark_health":
                tags, triggers, variables = await _fetch_all(account_id, container_id)
                summary = await gtm.get_container_summary(conn, account_id, container_id)

                total_tags = len(tags)
                total_triggers = len(triggers)
                total_variables = len(variables)

                # --- Individual dimension scores (0-100) ---
                # 1. Container size — penalize bloat
                size_score = 100
                if total_tags > 50:
                    size_score -= min(40, (total_tags - 50) * 2)
                if total_triggers > 40:
                    size_score -= min(30, (total_triggers - 40) * 2)
                if total_variables > 30:
                    size_score -= min(30, (total_variables - 30) * 2)
                size_score = max(0, size_score)

                # 2. Tag hygiene — orphan, paused, deprecated tags
                orphans = sum(1 for t in tags if not t.get("firing_triggers"))
                paused = sum(1 for t in tags if t.get("is_paused"))
                ua_count = sum(1 for t in tags if t.get("tag_type") == "ua")
                hygiene_deductions = orphans * 15 + paused * 5 + ua_count * 10
                hygiene_score = max(0, 100 - hygiene_deductions)

                # 3. GA4 readiness
                config_tags = [t for t in tags if t.get("tag_type") in ("googtag", "gaawc")]
                ga4_score = 100
                if not config_tags:
                    ga4_score = 0
                elif len(config_tags) > 1:
                    ga4_score = 60  # duplicates
                if ua_count > 0 and not config_tags:
                    ga4_score = 20  # UA only, no GA4

                # 4. Trigger efficiency — unused triggers
                unused_triggers = sum(1 for tr in triggers if not tr.get("tags_using_trigger"))
                trigger_score = max(0, 100 - unused_triggers * 10)

                # 5. Variable efficiency — unused variables
                unused_vars = sum(1 for v in variables if not v.get("tags_using_variable"))
                variable_score = max(0, 100 - unused_vars * 8)

                # 6. Security — custom HTML tag ratio
                html_count = sum(1 for t in tags if t.get("tag_type") == "html")
                html_ratio = html_count / max(total_tags, 1)
                security_score = max(0, int(100 - html_ratio * 150))

                # --- Weighted overall score ---
                overall = int(
                    size_score * 0.15
                    + hygiene_score * 0.25
                    + ga4_score * 0.20
                    + trigger_score * 0.15
                    + variable_score * 0.10
                    + security_score * 0.15
                )

                def grade(s):
                    if s >= 90:
                        return "A"
                    if s >= 80:
                        return "B"
                    if s >= 65:
                        return "C"
                    if s >= 50:
                        return "D"
                    return "F"

                return {
                    "overall_score": overall,
                    "overall_grade": grade(overall),
                    "container_id": container_id,
                    "dimensions": {
                        "container_size": {
                            "score": size_score,
                            "grade": grade(size_score),
                            "detail": f"{total_tags} tags, {total_triggers} triggers, {total_variables} variables",
                        },
                        "tag_hygiene": {
                            "score": hygiene_score,
                            "grade": grade(hygiene_score),
                            "detail": f"{orphans} orphan, {paused} paused, {ua_count} deprecated UA",
                        },
                        "ga4_readiness": {
                            "score": ga4_score,
                            "grade": grade(ga4_score),
                            "detail": f"{len(config_tags)} GA4 config tag(s), {ua_count} legacy UA tag(s)",
                        },
                        "trigger_efficiency": {
                            "score": trigger_score,
                            "grade": grade(trigger_score),
                            "detail": f"{unused_triggers} unused out of {total_triggers}",
                        },
                        "variable_efficiency": {
                            "score": variable_score,
                            "grade": grade(variable_score),
                            "detail": f"{unused_vars} unused out of {total_variables}",
                        },
                        "security_posture": {
                            "score": security_score,
                            "grade": grade(security_score),
                            "detail": f"{html_count} custom HTML tags ({int(html_ratio * 100)}% of total)",
                        },
                    },
                    "quick_wins": [
                        issue
                        for issue in [
                            f"Remove {orphans} orphan tag(s)" if orphans else None,
                            f"Migrate {ua_count} UA tag(s) to GA4" if ua_count else None,
                            f"Remove {unused_triggers} unused trigger(s)" if unused_triggers else None,
                            f"Remove {unused_vars} unused variable(s)" if unused_vars else None,
                            f"Consolidate {len(config_tags)} GA4 config tags to 1"
                            if len(config_tags) > 1
                            else None,
                        ]
                        if issue
                    ],
                }

            elif action == "find_tracking_regression":
                # ---------------------------------------------------------------
                # Compare the live (published) version against the previous one
                # to detect tags removed, paused, or that lost triggers.
                # Optionally cross-references with GA4 event data.
                # Uses versions().live() for the current version (fast) and
                # versions().get() for one previous version only.
                # ---------------------------------------------------------------
                if not date_range_start or not date_range_end:
                    return {
                        "error": True,
                        "message": "date_range_start and date_range_end are required for find_tracking_regression",
                    }

                # Step 1: Get version headers for history + live version in parallel-ish
                version_headers = await gtm.get_publish_history_by_conn(conn, account_id, container_id)
                if len(version_headers) < 2:
                    return {
                        "regressions": [],
                        "message": "Not enough published versions to compare (need at least 2).",
                        "versions_available": len(version_headers),
                    }

                # Step 2: Get live version (current published state) — single fast call
                latest_ver = await gtm.get_live_version(conn, account_id, container_id)
                latest_hdr = version_headers[0]

                # Step 3: Get previous version detail
                previous_hdr = version_headers[1]
                previous_ver = await gtm.get_version_detail(
                    conn, account_id, container_id, previous_hdr["containerVersionId"]
                )

                prev_tags = {t.get("tagId"): t for t in previous_ver.get("tag", [])}
                curr_tags = {t.get("tagId"): t for t in latest_ver.get("tag", [])}

                regressions = []

                # 1. Tags removed in the latest version
                for tag_id, tag in prev_tags.items():
                    if tag_id not in curr_tags:
                        regressions.append(
                            {
                                "type": "tag_removed",
                                "severity": "critical",
                                "tag_name": tag.get("name"),
                                "tag_type": tag.get("type"),
                                "detail": f"Tag '{tag.get('name')}' (type: {tag.get('type')}) was present in v{previous_hdr.get('containerVersionId')} but removed in the live version.",
                            }
                        )

                # 2. Tags that became paused or lost their triggers
                for tag_id, curr_tag in curr_tags.items():
                    prev_tag = prev_tags.get(tag_id)
                    if not prev_tag:
                        continue

                    if curr_tag.get("paused") and not prev_tag.get("paused"):
                        regressions.append(
                            {
                                "type": "tag_paused",
                                "severity": "warning",
                                "tag_name": curr_tag.get("name"),
                                "tag_type": curr_tag.get("type"),
                                "detail": f"Tag '{curr_tag.get('name')}' was paused in the live version (was active in v{previous_hdr.get('containerVersionId')}).",
                            }
                        )

                    prev_firing = set(prev_tag.get("firingTriggerId", []))
                    curr_firing = set(curr_tag.get("firingTriggerId", []))
                    lost_triggers = prev_firing - curr_firing
                    if lost_triggers and not curr_firing:
                        regressions.append(
                            {
                                "type": "triggers_removed",
                                "severity": "critical",
                                "tag_name": curr_tag.get("name"),
                                "tag_type": curr_tag.get("type"),
                                "detail": f"Tag '{curr_tag.get('name')}' lost all firing triggers — it will never fire.",
                                "triggers_removed_count": len(lost_triggers),
                            }
                        )
                    elif lost_triggers:
                        regressions.append(
                            {
                                "type": "triggers_reduced",
                                "severity": "warning",
                                "tag_name": curr_tag.get("name"),
                                "tag_type": curr_tag.get("type"),
                                "detail": f"Tag '{curr_tag.get('name')}' lost {len(lost_triggers)} firing trigger(s) but still has {len(curr_firing)} remaining.",
                                "triggers_removed_count": len(lost_triggers),
                            }
                        )

                # 3. Triggers removed
                prev_triggers = {t.get("triggerId"): t for t in previous_ver.get("trigger", [])}
                curr_triggers = {t.get("triggerId"): t for t in latest_ver.get("trigger", [])}
                for trig_id, trig in prev_triggers.items():
                    if trig_id not in curr_triggers:
                        regressions.append(
                            {
                                "type": "trigger_removed",
                                "severity": "warning",
                                "trigger_name": trig.get("name"),
                                "trigger_type": trig.get("type"),
                                "detail": f"Trigger '{trig.get('name')}' (type: {trig.get('type')}) was removed in the live version.",
                            }
                        )

                # 4. Optional GA4 cross-reference
                ga4_event_gaps = []
                if ga4_property_id:
                    try:
                        ga4 = state.ga4_connector
                        ga4_conn = _conn()
                        prop_id = (
                            ga4_property_id
                            if ga4_property_id.startswith("properties/")
                            else f"properties/{ga4_property_id}"
                        )

                        events_resp = await ga4.list_events(
                            ga4_conn, prop_id, date_range_start, date_range_end
                        )
                        ga4_events = {
                            e["event_name"]: int(e.get("event_count", 0))
                            for e in events_resp.get("events", [])
                        }

                        for tag in latest_ver.get("tag", []):
                            tag_type = tag.get("type", "")
                            if tag_type not in ("gaawe", "gaawc", "googtag"):
                                continue
                            event_name = None
                            for param in tag.get("parameter", []):
                                if param.get("key") == "eventName":
                                    event_name = param.get("value")
                                    break
                            if event_name and event_name not in ga4_events:
                                ga4_event_gaps.append(
                                    {
                                        "tag_name": tag.get("name"),
                                        "expected_event": event_name,
                                        "ga4_count": 0,
                                        "detail": f"GTM tag '{tag.get('name')}' fires event '{event_name}' but GA4 received 0 occurrences in the date range.",
                                    }
                                )
                    except Exception as e:
                        ga4_event_gaps = [{"warning": f"Could not cross-reference GA4: {e!s}"}]

                return {
                    "container_id": container_id,
                    "versions_compared": {
                        "current": {
                            "version_id": latest_ver.get("containerVersionId"),
                            "name": latest_hdr.get("name"),
                            "published": latest_hdr.get("timeStamp"),
                        },
                        "previous": {
                            "version_id": previous_hdr.get("containerVersionId"),
                            "name": previous_hdr.get("name"),
                            "published": previous_hdr.get("timeStamp"),
                        },
                    },
                    "regressions_found": len(regressions),
                    "regressions": regressions,
                    "ga4_event_gaps": ga4_event_gaps
                    if ga4_property_id
                    else "Provide ga4_property_id to cross-reference with GA4 event data",
                    "summary": {
                        "tags_removed": sum(1 for r in regressions if r["type"] == "tag_removed"),
                        "tags_paused": sum(1 for r in regressions if r["type"] == "tag_paused"),
                        "triggers_broken": sum(
                            1 for r in regressions if r["type"] in ("triggers_removed", "triggers_reduced")
                        ),
                        "triggers_deleted": sum(1 for r in regressions if r["type"] == "trigger_removed"),
                    },
                }

            elif action == "diagnose_conversion_discrepancy":
                # ---------------------------------------------------------------
                # Cross-reference GTM conversion-related tags with GA4 conversion
                # event data to find mismatches: tags that should send conversions
                # but GA4 reports zero or very low counts, or GA4 conversions that
                # have no matching GTM tag.
                # ---------------------------------------------------------------
                if not ga4_property_id:
                    return {
                        "error": True,
                        "message": "ga4_property_id is required for diagnose_conversion_discrepancy",
                    }

                prop_id = (
                    ga4_property_id
                    if ga4_property_id.startswith("properties/")
                    else f"properties/{ga4_property_id}"
                )
                start = date_range_start or "30daysAgo"
                end = date_range_end or "today"

                tags, triggers, _ = await _fetch_all(account_id, container_id)

                # --- Identify conversion-related GTM tags ---
                conversion_tags = []
                for t in tags:
                    tag_type = t.get("tag_type", "")
                    tag_name = t.get("tag_name", "")
                    is_conversion_tag = False
                    event_name = None
                    platform = None

                    # GA4 event tags (gaawe = GA4 Event)
                    if tag_type == "gaawe":
                        is_conversion_tag = True
                        platform = "ga4"
                        # The event name might be in the tag name or parameters
                        # We already have normalized data from _fetch_all
                        event_name = tag_name  # fallback

                    # Google Ads conversion tracking
                    elif tag_type in ("awct", "sp"):
                        is_conversion_tag = True
                        platform = "google_ads"

                    # Facebook/Meta pixel
                    elif tag_type == "html" and any(
                        kw in tag_name.lower() for kw in ["facebook", "meta", "fbq", "pixel", "conversion"]
                    ):
                        is_conversion_tag = True
                        platform = "meta"

                    # Any tag with "conversion" in the name
                    elif "conversion" in tag_name.lower():
                        is_conversion_tag = True
                        platform = "other"

                    if is_conversion_tag:
                        conversion_tags.append(
                            {
                                "tag_name": tag_name,
                                "tag_type": tag_type,
                                "platform": platform,
                                "event_name": event_name,
                                "has_triggers": bool(t.get("firing_triggers")),
                                "is_paused": t.get("is_paused", False),
                            }
                        )

                # --- Fetch GA4 conversion events ---
                ga4 = state.ga4_connector
                ga4_conn = _conn()
                discrepancies = []

                try:
                    conv_resp = await ga4.get_conversion_events(ga4_conn, prop_id, start, end)
                    ga4_conversions = {c["event_name"]: c for c in conv_resp.get("conversion_events", [])}
                except Exception as e:
                    return {
                        "error": True,
                        "message": f"Could not fetch GA4 conversion events: {e!s}",
                        "gtm_conversion_tags_found": len(conversion_tags),
                        "conversion_tags": conversion_tags,
                    }

                # --- Also get GA4 event list with counts ---
                try:
                    events_resp = await ga4.list_events(ga4_conn, prop_id, start, end)
                    ga4_events = {
                        e["event_name"]: int(e.get("event_count", 0)) for e in events_resp.get("events", [])
                    }
                except Exception:
                    ga4_events = {}

                # --- Cross-reference: GTM tags vs GA4 data ---
                for ct in conversion_tags:
                    issues = []

                    if ct["is_paused"]:
                        issues.append("Tag is paused — conversions will not fire")

                    if not ct["has_triggers"]:
                        issues.append("Tag has no firing triggers — it will never execute")

                    # For GA4 event tags, check if the event shows up in GA4
                    if ct["platform"] == "ga4" and ct.get("event_name"):
                        ev = ct["event_name"]
                        count = ga4_events.get(ev, 0)
                        if count == 0:
                            issues.append(f"Event '{ev}' has 0 occurrences in GA4 for the date range")
                        elif count < 5:
                            issues.append(
                                f"Event '{ev}' has only {count} occurrences in GA4 — suspiciously low"
                            )

                    if issues:
                        discrepancies.append(
                            {
                                "tag_name": ct["tag_name"],
                                "tag_type": ct["tag_type"],
                                "platform": ct["platform"],
                                "issues": issues,
                                "severity": "critical"
                                if not ct["has_triggers"] or ct["is_paused"]
                                else "warning",
                            }
                        )

                # --- Check reverse: GA4 conversions with no matching GTM tag ---
                gtm_ga4_event_names = {
                    ct.get("event_name") for ct in conversion_tags if ct["platform"] == "ga4"
                }
                orphan_conversions = []
                for ev_name, ev_data in ga4_conversions.items():
                    count = ga4_events.get(ev_name, 0)
                    if ev_name not in gtm_ga4_event_names and count > 0:
                        orphan_conversions.append(
                            {
                                "event_name": ev_name,
                                "ga4_count": count,
                                "detail": f"GA4 conversion event '{ev_name}' ({count} occurrences) has no matching GTM event tag — it may be fired via gtag.js directly or from another source.",
                            }
                        )

                return {
                    "container_id": container_id,
                    "ga4_property": prop_id,
                    "date_range": {"start": start, "end": end},
                    "gtm_conversion_tags": len(conversion_tags),
                    "conversion_tags": conversion_tags,
                    "discrepancies_found": len(discrepancies),
                    "discrepancies": discrepancies,
                    "orphan_ga4_conversions": orphan_conversions,
                    "ga4_conversion_events_total": len(ga4_conversions),
                    "summary": {
                        "gtm_tags_with_issues": len(discrepancies),
                        "ga4_conversions_without_gtm_tag": len(orphan_conversions),
                        "paused_conversion_tags": sum(1 for ct in conversion_tags if ct["is_paused"]),
                        "triggerless_conversion_tags": sum(
                            1 for ct in conversion_tags if not ct["has_triggers"]
                        ),
                    },
                }

            elif action == "audit_consent_mode":
                # ── Consent Mode v2 / GDPR / CCPA compliance audit ────────
                # Top-10 automated checks against the GTM container. Each
                # finding has severity + category + recommendation so the
                # output is directly actionable.
                #
                # Checks (short name ↔ what it verifies):
                #   cmp_present          — a Consent Mode template/tag exists
                #   consent_types_v2     — all 4 v2 types are handled
                #                          (ad_storage, analytics_storage,
                #                           ad_user_data, ad_personalization)
                #   defaults_before_meas — consent-init fires before GA4/Ads
                #                          (priority ordering)
                #   ga4_respects_consent — GA4 tags have wait_for_update /
                #                          consent-aware firing
                #   ads_gated_on_storage — Google Ads conversion tags gated
                #                          on ad_storage=granted
                #   non_google_gated     — Meta/TikTok/Snap pixels gated
                #   cmp_known_vendor     — known CMP (Cookiebot, OneTrust,
                #                          Usercentrics, Iubenda, Didomi…)
                #   region_defaults      — region-specific default consent
                #   sgtm_consent_forward — server-side tagging forwards
                #                          consent (if SGTM present)
                #   data_redaction       — URL passthrough + ads_data_redaction
                if not account_id or not container_id:
                    return {
                        "error": True,
                        "message": ("account_id and container_id are required for audit_consent_mode"),
                    }

                tags, triggers, variables = await _fetch_all(account_id, container_id)

                # ── Helpers to probe tag types / parameter values ──────
                def _tag_type(t):
                    return (t.get("tag_type") or t.get("type") or "").lower()

                def _tag_name(t):
                    return (t.get("tag_name") or t.get("name") or "").lower()

                def _tag_params(t):
                    # GTM tag parameters may live as a list of {key, value}
                    # dicts or a flat dict — normalise to lowercase dict.
                    raw = t.get("parameter") or t.get("parameters") or []
                    out = {}
                    if isinstance(raw, list):
                        for p in raw:
                            k = str(p.get("key", "")).lower()
                            out[k] = p.get("value")
                    elif isinstance(raw, dict):
                        for k, v in raw.items():
                            out[str(k).lower()] = v
                    return out

                def _is_ga4(t):
                    tt = _tag_type(t)
                    return (
                        tt in {"gaawe", "gaawc", "googtag", "ga4", "awct"}
                        or "ga4" in _tag_name(t)
                        or "google tag" in _tag_name(t)
                        or ("analytics" in tt and "ga4" in _tag_name(t))
                    )

                def _is_google_ads_conversion(t):
                    tt = _tag_type(t)
                    nm = _tag_name(t)
                    return (
                        tt in {"awct", "sp"}
                        or "google ads" in nm
                        or "adwords" in nm
                        or ("conversion" in nm and ("ads" in nm or "google" in nm))
                    )

                def _is_non_google_pixel(t):
                    nm = _tag_name(t)
                    tt = _tag_type(t)
                    keywords = (
                        "meta",
                        "facebook",
                        "fbq",
                        "pixel",
                        "tiktok",
                        "ttq",
                        "snap",
                        "pinterest",
                        "linkedin",
                        "twitter",
                        "x ads",
                        "reddit",
                    )
                    return any(k in nm for k in keywords) or any(k in tt for k in keywords)

                # Build haystacks for keyword scans
                all_text_blob = " ".join(
                    [_tag_name(t) + " " + _tag_type(t) for t in tags]
                    + [
                        str(v.get("name", "")).lower() + " " + str(v.get("type", "")).lower()
                        for v in variables
                    ]
                ).lower()

                findings: list[dict] = []

                def _add(check, severity, message, recommendation, evidence=None):
                    findings.append(
                        {
                            "check": check,
                            "severity": severity,  # critical | warning | info
                            "category": "consent",
                            "message": message,
                            "recommendation": recommendation,
                            "evidence": evidence or {},
                        }
                    )

                # 1) CMP / Consent-Init tag present ────────────────────
                consent_init_tags = [
                    t
                    for t in tags
                    if _tag_type(t) in {"cm", "ccd"} or "consent" in _tag_name(t) or "consent" in _tag_type(t)
                ]
                if not consent_init_tags:
                    _add(
                        "cmp_present",
                        "critical",
                        "No Consent Mode / consent-init tag detected in the container.",
                        "Install Google's Consent Mode template or a "
                        "certified CMP (Cookiebot, OneTrust, Usercentrics, "
                        "Iubenda, Didomi) and set default consent states "
                        "before any measurement tags fire.",
                        {"matched_tags": 0},
                    )

                # 2) All 4 v2 consent types handled ────────────────────
                v2_types = [
                    "ad_storage",
                    "analytics_storage",
                    "ad_user_data",
                    "ad_personalization",
                ]
                missing_v2 = [c for c in v2_types if c not in all_text_blob]
                if missing_v2:
                    _add(
                        "consent_types_v2",
                        "critical" if len(missing_v2) >= 2 else "warning",
                        f"Consent Mode v2 types missing from container: {', '.join(missing_v2)}.",
                        "v2 added ad_user_data and ad_personalization — "
                        "update your consent-default/update tags to set all "
                        "four signals. Required for EU traffic since "
                        "March 2024.",
                        {"missing_types": missing_v2},
                    )

                # 3) Defaults fire BEFORE measurement tags ─────────────
                # Heuristic: consent-init tags should have priority higher
                # than GA4/Ads tags, OR use the "Consent Initialization" trigger.
                def _priority(t):
                    try:
                        return int(t.get("priority") or _tag_params(t).get("priority") or 0)
                    except (ValueError, TypeError):
                        return 0

                meas_tags = [
                    t for t in tags if _is_ga4(t) or _is_google_ads_conversion(t) or _is_non_google_pixel(t)
                ]
                if consent_init_tags and meas_tags:
                    max_consent_prio = max(
                        (_priority(t) for t in consent_init_tags),
                        default=0,
                    )
                    lower_meas = [_tag_name(t) for t in meas_tags if _priority(t) >= max_consent_prio]
                    # Also check trigger type — "Consent Initialization - All Pages"
                    using_consent_init_trigger = any(
                        any("consent" in (str(tr).lower()) for tr in (t.get("firing_triggers") or []))
                        for t in consent_init_tags
                    )
                    if not using_consent_init_trigger and lower_meas:
                        _add(
                            "defaults_before_meas",
                            "critical",
                            "Consent-init tag priority is not strictly "
                            "higher than measurement tags and no "
                            "'Consent Initialization' trigger is detected.",
                            "Use the 'Consent Initialization - All Pages' "
                            "trigger for the consent-defaults tag, or raise "
                            "its priority above every GA4/Ads/pixel tag.",
                            {"measurement_tags_at_or_above": lower_meas[:10]},
                        )

                # 4) GA4 tags respect consent (wait_for_update) ────────
                ga4_without_wait = []
                for t in tags:
                    if not _is_ga4(t):
                        continue
                    params = _tag_params(t)
                    has_wait = any("wait_for_update" in str(k) or "consent" in str(k) for k in params)
                    if not has_wait:
                        ga4_without_wait.append(_tag_name(t))
                if ga4_without_wait:
                    _add(
                        "ga4_respects_consent",
                        "warning",
                        f"{len(ga4_without_wait)} GA4 tag(s) have no consent-aware firing parameter.",
                        "Set wait_for_update (500ms is typical) on GA4 "
                        "config tags so they hold until the CMP signals "
                        "a consent choice.",
                        {"tags": ga4_without_wait[:10]},
                    )

                # 5) Google Ads conversion tags gated ──────────────────
                ads_tags = [t for t in tags if _is_google_ads_conversion(t)]
                ads_ungated = []
                for t in ads_tags:
                    # Heuristic: a consent-aware Ads tag either has
                    # `consentSettings` in params OR fires on a trigger
                    # whose name mentions consent/granted.
                    params = _tag_params(t)
                    if any("consent" in str(k) for k in params):
                        continue
                    trig_names = " ".join(str(tr) for tr in (t.get("firing_triggers") or [])).lower()
                    if "consent" in trig_names or "granted" in trig_names:
                        continue
                    ads_ungated.append(_tag_name(t))
                if ads_ungated:
                    _add(
                        "ads_gated_on_storage",
                        "critical",
                        f"{len(ads_ungated)} Google Ads conversion tag(s) "
                        "fire without an ad_storage=granted gate.",
                        "Restrict Ads tags to fire only when "
                        "ad_storage='granted' (Consent Mode will throttle "
                        "cookieless conversions otherwise, under-reporting "
                        "your campaigns).",
                        {"tags": ads_ungated[:10]},
                    )

                # 6) Non-Google pixels gated ───────────────────────────
                pixels = [t for t in tags if _is_non_google_pixel(t)]
                pixel_ungated = []
                for t in pixels:
                    trig_names = " ".join(str(tr) for tr in (t.get("firing_triggers") or [])).lower()
                    if "consent" in trig_names or "granted" in trig_names:
                        continue
                    pixel_ungated.append(_tag_name(t))
                if pixel_ungated:
                    _add(
                        "non_google_gated",
                        "critical",
                        f"{len(pixel_ungated)} non-Google pixel tag(s) "
                        "(Meta, TikTok, Snap, etc.) fire without a "
                        "consent-gated trigger.",
                        "Consent Mode only affects Google tags natively. "
                        "Third-party pixels must be gated by a blocking "
                        "trigger checking your CMP's consent variable.",
                        {"tags": pixel_ungated[:10]},
                    )

                # 7) Known CMP vendor detected ─────────────────────────
                known_vendors = {
                    "cookiebot": "Cookiebot",
                    "onetrust": "OneTrust",
                    "usercentrics": "Usercentrics",
                    "iubenda": "Iubenda",
                    "didomi": "Didomi",
                    "trustarc": "TrustArc",
                    "osano": "Osano",
                    "cookieyes": "CookieYes",
                    "quantcast": "Quantcast Choice",
                }
                detected_vendor = None
                for key, label in known_vendors.items():
                    if key in all_text_blob:
                        detected_vendor = label
                        break
                if not detected_vendor:
                    _add(
                        "cmp_known_vendor",
                        "info",
                        "No signature of a known CMP vendor found in tag / variable names.",
                        "If you're using a custom banner, make sure it "
                        "integrates with GTM's consent API — many home-"
                        "grown banners fail Consent Mode signalling.",
                    )

                # 8) Region-specific defaults ──────────────────────────
                region_keywords = (
                    "region",
                    "eea",
                    "european",
                    "gdpr",
                    "uk",
                    "ccpa",
                    "california",
                )
                has_region_signal = any(kw in all_text_blob for kw in region_keywords)
                if not has_region_signal:
                    _add(
                        "region_defaults",
                        "warning",
                        "No region-specific consent configuration detected.",
                        "Set defaults to 'denied' for EEA/UK (GDPR) and "
                        "'granted' for US (with CCPA opt-out honoring). "
                        "A single global default commonly over/under-"
                        "throttles one of the two jurisdictions.",
                    )

                # 9) SGTM consent forwarding ───────────────────────────
                sgtm_signals = ("server-side", "server container", "sgtm", "stape", "gtm.server")
                has_sgtm = any(s in all_text_blob for s in sgtm_signals)
                if has_sgtm:
                    # Look for forwarded-consent plumbing in tag params
                    forwards_consent = any("consent" in str(k) for t in tags for k in _tag_params(t))
                    if not forwards_consent:
                        _add(
                            "sgtm_consent_forward",
                            "warning",
                            "Server-side tagging detected but consent "
                            "signals don't appear to be forwarded to the "
                            "SGTM container.",
                            "Pass x-gtm-consent headers (or the equivalent "
                            "event data) from web to server so SGTM can "
                            "apply the same gating.",
                        )

                # 10) Data redaction flags ─────────────────────────────
                redaction_hits = [
                    kw
                    for kw in ("url_passthrough", "ads_data_redaction", "urlpassthrough", "adsdataredaction")
                    if kw in all_text_blob
                ]
                if not redaction_hits:
                    _add(
                        "data_redaction",
                        "info",
                        "No url_passthrough / ads_data_redaction signals detected.",
                        "With Consent Mode, enable url_passthrough=true "
                        "(preserves gclid across pages without cookies) "
                        "and ads_data_redaction=true (strips ad-click IDs "
                        "when ad_storage is denied).",
                    )

                critical_n = sum(1 for f in findings if f["severity"] == "critical")
                warning_n = sum(1 for f in findings if f["severity"] == "warning")
                info_n = sum(1 for f in findings if f["severity"] == "info")
                score = max(0, 100 - critical_n * 20 - warning_n * 8 - info_n * 2)

                return {
                    "action": "audit_consent_mode",
                    "container": {
                        "account_id": account_id,
                        "container_id": container_id,
                    },
                    "score": score,
                    "summary": {
                        "critical": critical_n,
                        "warning": warning_n,
                        "info": info_n,
                        "total_findings": len(findings),
                        "detected_cmp_vendor": detected_vendor,
                        "has_server_side_tagging": has_sgtm,
                    },
                    "findings": findings,
                    "scope_note": (
                        "Tag-manager-side checks only. A complete audit "
                        "also inspects the live site (cookie banner UX, "
                        "actual network requests, regional routing) — those "
                        "checks need a site crawl and are not yet "
                        "implemented."
                    ),
                }

            return {"error": True, "message": f"Unknown action '{action}' for GTM tagmanager_audit"}

        elif platform == "adobe_launch":
            if not u or not u.has_adobe_launch:
                return _no_adobe_launch()
            conn_id, client_id, client_secret, org_id = await _get_adobe_launch_conn(u.user_id)
            if not client_id:
                return _no_adobe_launch()
            launch = state.adobe_launch_connector

            if action == "audit_property":
                if not container_id:
                    return {
                        "error": True,
                        "message": "container_id (property_id) is required for audit_property",
                    }
                return await cached_tool_response(
                    f"cache:launch:audit:{conn_id}:{container_id}",
                    300,
                    launch.audit_property,
                    client_id,
                    client_secret,
                    org_id,
                    container_id,
                )
            elif action == "get_publish_history":
                if not container_id:
                    return {
                        "error": True,
                        "message": "container_id (property_id) is required for get_publish_history",
                    }
                return await launch.get_publish_history(client_id, client_secret, org_id, container_id)
            elif action == "audit_consent_mode":
                if not container_id:
                    return {
                        "error": True,
                        "message": ("container_id (Launch property_id) is required for audit_consent_mode"),
                    }
                # ── Adobe Launch consent audit ──────────────────────
                # Heuristic-only: Launch's object model (properties,
                # extensions, rules, data elements) doesn't carry
                # explicit Consent Mode v2 signals the way GTM does, so
                # we look for proxy evidence:
                #   1. A known CMP extension is installed.
                #   2. At least one data element references consent/CMP.
                #   3. At least one rule's name references consent/CMP
                #      (common convention: rules gated on consent state).
                #   4. Adobe Experience Platform Web SDK ("alloy")
                #      extension is installed — it's the recommended
                #      path for server-side consent forwarding.
                findings: list[dict] = []
                detected_cmp = None
                try:
                    ext_resp, de_resp, rules_resp = await asyncio.gather(
                        launch.list_extensions(client_id, client_secret, org_id, container_id),
                        launch.list_data_elements(client_id, client_secret, org_id, container_id),
                        launch.list_rules(client_id, client_secret, org_id, container_id),
                        return_exceptions=True,
                    )
                except Exception as exc:
                    return {
                        "error": True,
                        "message": f"Adobe Launch audit fetch failed: {exc}",
                    }

                def _safe_list(resp, key):
                    if isinstance(resp, dict) and not resp.get("error"):
                        return resp.get(key) or []
                    return []

                extensions = _safe_list(ext_resp, "extensions")
                data_elements = _safe_list(de_resp, "data_elements")
                rules = _safe_list(rules_resp, "rules")

                # ── 1. CMP extension detection ──────────────────────
                KNOWN_CMP_EXTS = {
                    "onetrust",
                    "cookiebot",
                    "usercentrics",
                    "iubenda",
                    "didomi",
                    "trustarc",
                    "osano",
                    "cookieyes",
                    "quantcast",
                    "consentmanager",
                }
                ext_names = [(e.get("name") or "").lower() for e in extensions]
                for name in ext_names:
                    for cmp_v in KNOWN_CMP_EXTS:
                        if cmp_v in name:
                            detected_cmp = cmp_v
                            break
                    if detected_cmp:
                        break

                if detected_cmp:
                    findings.append(
                        {
                            "severity": "info",
                            "code": "cmp_extension_installed",
                            "message": (f"Known CMP extension detected: '{detected_cmp}'"),
                        }
                    )
                else:
                    findings.append(
                        {
                            "severity": "critical",
                            "code": "no_cmp_extension",
                            "message": (
                                "No known CMP extension installed in Adobe "
                                "Launch. Consent signals likely not captured "
                                "centrally."
                            ),
                        }
                    )

                # ── 2. Web SDK / Alloy (server-side consent path) ──
                has_alloy = any(
                    "alloy" in n or "aep web sdk" in n or "experience platform" in n for n in ext_names
                )
                if has_alloy:
                    findings.append(
                        {
                            "severity": "info",
                            "code": "aep_web_sdk_present",
                            "message": (
                                "AEP Web SDK (alloy) is installed — supports server-side consent forwarding."
                            ),
                        }
                    )
                else:
                    findings.append(
                        {
                            "severity": "warning",
                            "code": "no_aep_web_sdk",
                            "message": (
                                "AEP Web SDK (alloy) not detected. Consider "
                                "installing for robust consent forwarding."
                            ),
                        }
                    )

                # ── 3. Consent data elements ────────────────────────
                CONSENT_KEYWORDS = (
                    "consent",
                    "cmp",
                    "cookie",
                    "gdpr",
                    "ccpa",
                    "opt-out",
                    "opt_out",
                    "optout",
                )
                consent_de = [
                    d
                    for d in data_elements
                    if any(k in (d.get("name") or "").lower() for k in CONSENT_KEYWORDS)
                ]
                if consent_de:
                    findings.append(
                        {
                            "severity": "info",
                            "code": "consent_data_elements",
                            "message": (f"Found {len(consent_de)} data element(s) referencing consent."),
                            "evidence": [d.get("name") for d in consent_de[:5]],
                        }
                    )
                else:
                    findings.append(
                        {
                            "severity": "critical",
                            "code": "no_consent_data_elements",
                            "message": (
                                "No data elements reference consent/CMP. "
                                "Rules cannot gate on consent state without "
                                "these."
                            ),
                        }
                    )

                # ── 4. Consent-gated rules ──────────────────────────
                consent_rules = [
                    r for r in rules if any(k in (r.get("name") or "").lower() for k in CONSENT_KEYWORDS)
                ]
                if consent_rules:
                    findings.append(
                        {
                            "severity": "info",
                            "code": "consent_gated_rules",
                            "message": (f"Found {len(consent_rules)} rule(s) with consent-related naming."),
                            "evidence": [r.get("name") for r in consent_rules[:5]],
                        }
                    )
                else:
                    findings.append(
                        {
                            "severity": "warning",
                            "code": "no_consent_gated_rules",
                            "message": (
                                "No rules appear to be gated on consent state "
                                "(by naming convention). Verify that ad/"
                                "analytics rules check consent before firing."
                            ),
                        }
                    )

                severities = [f["severity"] for f in findings]
                score = {
                    "critical": severities.count("critical"),
                    "warning": severities.count("warning"),
                    "info": severities.count("info"),
                    "total_findings": len(findings),
                    "detected_cmp_vendor": detected_cmp,
                    "has_aep_web_sdk": has_alloy,
                }

                return {
                    "action": "audit_consent_mode",
                    "platform": "adobe_launch",
                    "property_id": container_id,
                    "summary": score,
                    "findings": findings,
                    "scope_note": (
                        "Adobe Launch audit is heuristic: Launch's API "
                        "exposes extensions, rules, and data elements, "
                        "but does NOT expose per-rule consent conditions. "
                        "Final compliance verification requires inspecting "
                        "rule logic in the Launch UI and running a browser-"
                        "side CMP check against the published library."
                    ),
                }
            return {"error": True, "message": f"Unknown action '{action}' for Adobe Launch tagmanager_audit"}

        return {"error": True, "message": f"Unknown platform '{platform}'"}

    # -------------------------------------------------------------------------
    # tagmanager_write — Layer 3: Write / publish operations
    # -------------------------------------------------------------------------

    @mcp_server.tool("tagmanager_write")
    async def tagmanager_write(
        platform: Literal["gtm", "adobe_launch"] = "gtm",
        action: str | None = None,
        account_id: CoercedStr | None = None,
        container_id: CoercedStr | None = None,
        name: str | None = None,
        workspace_id: CoercedStr | None = None,
        tag_id: CoercedStr | None = None,
        type: str | None = None,
        parameters: list | None = None,
        firing_trigger_ids: list | None = None,
        blocking_trigger_ids: list | None = None,
        updates: dict | None = None,
        filters: list | None = None,
        spec: dict | None = None,
        config: dict | None = None,
    ) -> dict:
        """
        Modifies tag manager configurations and publishes changes.
        Requires write / publish scopes; read-only users receive a clear error.

        platform: 'gtm' | 'adobe_launch'

        GTM Actions:
          propose_change     — describe a proposed change without applying it (safe for read-only users)
          create_workspace   — create a new workspace for staging changes (name required)
          create_tag         — create a new tag (name + type + firing_trigger_ids required)
          update_tag         — update an existing tag's fields (tag_id + updates dict required)
          delete_tag         — delete a tag by ID (tag_id required)
          create_trigger     — create a new trigger (name + type required; filters optional)
          create_variable    — create a new variable (name + type + parameters required)
          publish_container  — publish the workspace as a new container version (workspace_id + name required)
                               Requires the GTM publish scope.

        Adobe Launch Actions:
          create_property    — config: {name, company_id, platform?, domains?}
          create_rule        — config: {property_id, name}
          create_data_element — config: {property_id, name, delegate_descriptor_id, settings?}
          create_library     — config: {property_id, name, environment_id}
          add_resources_to_library — config: {library_id, resources=[]}
          build_library      — config: {library_id}
          transition_library — config: {library_id, action} (action: submit, approve, reject, develop)
        """
        u = _user()

        if platform == "gtm":
            if not u or not u.has_gtm:
                return _no_gtm()
            scopes = u.connections[0].scopes if u.connections else []

            # Publish is a separate elevated scope check
            if action == "publish_container":
                if "https://www.googleapis.com/auth/tagmanager.publish" not in scopes:
                    return {
                        "error": True,
                        "error_type": "insufficient_scope",
                        "message": "Publishing requires the GTM publish scope.",
                        "action_required": f"Reconnect Google at {settings.APP_BASE_URL}/connect with 'GTM Publish' tier.",
                    }
                return await state.gtm_connector.publish_container(
                    _conn(), account_id, container_id, workspace_id, name, ""
                )

            # propose_change is safe for anyone — generates a concrete proposal
            if action == "propose_change":
                if not spec:
                    return {
                        "error": True,
                        "message": "spec dict is required for propose_change. Include entity_type, name, and changes.",
                    }

                entity_type = spec.get("entity_type", "tag")
                entity_name = spec.get("name") or name or "Unknown"
                change_type = spec.get("change_type", "create")
                changes = spec.get("changes", spec)

                # Build a human-readable proposal
                proposal_lines = [
                    f"Proposed {change_type} of {entity_type} '{entity_name}':",
                ]

                if change_type == "create":
                    if entity_type == "tag":
                        tag_type = changes.get("tag_type", changes.get("type", "unknown"))
                        firing = changes.get("firing_triggers", changes.get("firing_trigger_ids", []))
                        proposal_lines.append(f"  - Type: {tag_type}")
                        if firing:
                            proposal_lines.append(f"  - Fires on: {', '.join(str(f) for f in firing)}")
                        params = changes.get("parameters", [])
                        if params:
                            proposal_lines.append(f"  - Parameters: {len(params)} configured")
                    elif entity_type == "trigger":
                        trigger_type = changes.get("trigger_type", changes.get("type", "unknown"))
                        proposal_lines.append(f"  - Type: {trigger_type}")
                        filters = changes.get("filters", [])
                        if filters:
                            proposal_lines.append(f"  - Filters: {len(filters)} conditions")
                    elif entity_type == "variable":
                        var_type = changes.get("variable_type", changes.get("type", "unknown"))
                        proposal_lines.append(f"  - Type: {var_type}")
                elif change_type == "update":
                    for key, val in changes.items():
                        if key not in ("entity_type", "name", "change_type"):
                            proposal_lines.append(f"  - Set {key} = {val}")
                elif change_type == "delete":
                    proposal_lines.append(f"  - Will remove {entity_type} '{entity_name}' from the workspace")

                proposal_lines.append("")
                proposal_lines.append(
                    "Impact: Changes will be staged in a workspace. They will NOT be live until the workspace is published."
                )
                proposal_lines.append(
                    "Next steps: Use create_workspace → apply changes → review → publish_container to go live."
                )

                return {
                    "operation": "propose_change",
                    "proposal": "\n".join(proposal_lines),
                    "proposed_config": spec,
                    "change_type": change_type,
                    "entity_type": entity_type,
                    "entity_name": entity_name,
                    "is_live": False,
                    "requires_publish": True,
                    # GTM identifiers so a caller (e.g. Ask Fluxito's draft flow)
                    # can later publish the workspace this proposal targets. Any
                    # may be None if the caller didn't supply them.
                    "connection_id": _conn(),
                    "account_id": account_id,
                    "container_id": container_id,
                    "workspace_id": workspace_id,
                }

            # All other write actions need the edit.containers scope
            if "https://www.googleapis.com/auth/tagmanager.edit.containers" not in scopes:
                return {
                    "error": True,
                    "error_type": "insufficient_scope",
                    "message": "This action requires the GTM write scope.",
                    "action_required": f"Reconnect Google at {settings.APP_BASE_URL}/connect with 'GTM Write' tier.",
                }

            gtm = state.gtm_connector
            conn_id = _conn()

            if action == "create_workspace":
                return await gtm.create_workspace(conn_id, account_id, container_id, name, "")
            elif action == "create_tag":
                return await gtm.create_tag(
                    conn_id,
                    account_id,
                    container_id,
                    workspace_id,
                    name,
                    type,
                    parameters or [],
                    firing_trigger_ids or [],
                    blocking_trigger_ids,
                    "",
                )
            elif action == "update_tag":
                return await gtm.update_tag(
                    conn_id, account_id, container_id, workspace_id, tag_id, updates or {}
                )
            elif action == "delete_tag":
                return await gtm.delete_tag(conn_id, account_id, container_id, workspace_id, tag_id)
            elif action == "create_trigger":
                return await gtm.create_trigger(
                    conn_id, account_id, container_id, workspace_id, name, type, filters
                )
            elif action == "create_variable":
                return await gtm.create_variable(
                    conn_id, account_id, container_id, workspace_id, name, type, parameters or []
                )

            ADOBE_LAUNCH_WRITE_ACTIONS = {
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
            if action in ADOBE_LAUNCH_WRITE_ACTIONS:
                platform = "adobe_launch"
            else:
                return {"error": True, "message": f"Unknown action '{action}' for GTM tagmanager_write"}

        if platform == "adobe_launch":
            # Write gating (FINDINGS S1 #8): unlike GTM there is no Google-style
            # per-action OAuth scope to pre-check — Adobe Launch uses a
            # Server-to-Server credential whose Launch permissions are fixed at
            # the IMS/credential level (the `has_launch` capability flag). Writes
            # are therefore gated by (1) the RBAC backstop, which requires the
            # `tagmanager:write` domain for this tool exactly as it does for GTM
            # writes, and (2) the Adobe credential's own API permissions (the API
            # rejects an under-privileged credential). `transition_library`
            # (action='approve') is the production-publish equivalent and is
            # covered by the same `tagmanager:write` gate.
            if not u or not u.has_adobe_launch:
                return _no_adobe_launch()
            conn_id, client_id, client_secret, org_id = await _get_adobe_launch_conn(u.user_id)
            if not client_id:
                return _no_adobe_launch()
            launch = state.adobe_launch_connector

            cfg = dict(config or {})
            if "name" not in cfg and name:
                cfg["name"] = name
            if "property_id" not in cfg:
                if container_id:
                    cfg["property_id"] = container_id
                elif account_id and action != "create_property":
                    cfg["property_id"] = account_id
            if "rule_id" not in cfg and tag_id:
                cfg["rule_id"] = tag_id
            if "data_element_id" not in cfg and tag_id:
                cfg["data_element_id"] = tag_id
            if "rule_component_id" not in cfg and tag_id:
                cfg["rule_component_id"] = tag_id
            if "library_id" not in cfg and workspace_id and workspace_id != "0":
                cfg["library_id"] = workspace_id
            if "settings" not in cfg and spec:
                cfg["settings"] = spec

            if action == "create_property":
                comp_id = cfg.get("company_id") or account_id
                if not cfg.get("name") or not comp_id:
                    return {"error": True, "message": "config.name and config.company_id are required"}
                return await launch.create_property(
                    client_id,
                    client_secret,
                    org_id,
                    comp_id,
                    name=cfg["name"],
                    platform=cfg.get("platform", "web"),
                    domains=cfg.get("domains"),
                )
            elif action == "create_rule":
                if not cfg.get("property_id") or not cfg.get("name"):
                    return {"error": True, "message": "config.property_id and config.name are required"}
                return await launch.create_rule(
                    client_id,
                    client_secret,
                    org_id,
                    cfg["property_id"],
                    cfg["name"],
                    components=cfg.get("components"),
                )
            elif action == "update_rule":
                rule_id = cfg.get("rule_id")
                if not rule_id:
                    return {"error": True, "message": "config.rule_id is required"}
                return await launch.update_rule(
                    client_id,
                    client_secret,
                    org_id,
                    rule_id,
                    name=cfg.get("name"),
                    enabled=cfg.get("enabled"),
                )
            elif action == "delete_rule":
                rule_id = cfg.get("rule_id")
                if not rule_id:
                    return {"error": True, "message": "config.rule_id is required"}
                return await launch.delete_rule(client_id, client_secret, org_id, rule_id)
            elif action == "create_rule_component":
                required = ["property_id", "rule_id", "name", "delegate_descriptor_id"]
                missing = [k for k in required if not cfg.get(k)]
                if missing:
                    return {"error": True, "message": f"config missing required keys: {missing}"}
                return await launch.create_rule_component(
                    client_id,
                    client_secret,
                    org_id,
                    cfg["property_id"],
                    cfg["rule_id"],
                    name=cfg["name"],
                    delegate_descriptor_id=cfg["delegate_descriptor_id"],
                    settings=cfg.get("settings"),
                    extension_id=cfg.get("extension_id"),
                    rule_order=cfg.get("rule_order"),
                    order=cfg.get("order"),
                    negate=cfg.get("negate"),
                    timeout=cfg.get("timeout"),
                    delay_next=cfg.get("delay_next"),
                )
            elif action == "update_rule_component":
                comp_id = cfg.get("rule_component_id")
                if not comp_id:
                    return {"error": True, "message": "config.rule_component_id is required"}
                return await launch.update_rule_component(
                    client_id,
                    client_secret,
                    org_id,
                    comp_id,
                    name=cfg.get("name"),
                    settings=cfg.get("settings"),
                    delegate_descriptor_id=cfg.get("delegate_descriptor_id"),
                    rule_order=cfg.get("rule_order"),
                    order=cfg.get("order"),
                    negate=cfg.get("negate"),
                    timeout=cfg.get("timeout"),
                    delay_next=cfg.get("delay_next"),
                )
            elif action == "delete_rule_component":
                comp_id = cfg.get("rule_component_id")
                if not comp_id:
                    return {"error": True, "message": "config.rule_component_id is required"}
                return await launch.delete_rule_component(client_id, client_secret, org_id, comp_id)
            elif action == "create_data_element":
                required = ["property_id", "name", "delegate_descriptor_id"]
                missing = [k for k in required if not cfg.get(k)]
                if missing:
                    return {"error": True, "message": f"config missing required keys: {missing}"}
                return await launch.create_data_element(
                    client_id,
                    client_secret,
                    org_id,
                    cfg["property_id"],
                    name=cfg["name"],
                    delegate_descriptor_id=cfg["delegate_descriptor_id"],
                    settings=cfg.get("settings"),
                    extension_id=cfg.get("extension_id"),
                    clean_text=cfg.get("clean_text"),
                    force_lower_case=cfg.get("force_lower_case"),
                    default_value=cfg.get("default_value"),
                    storage_duration=cfg.get("storage_duration"),
                    enabled=cfg.get("enabled", True),
                )
            elif action == "update_data_element":
                de_id = cfg.get("data_element_id")
                if not de_id:
                    return {"error": True, "message": "config.data_element_id is required"}
                return await launch.update_data_element(
                    client_id,
                    client_secret,
                    org_id,
                    de_id,
                    name=cfg.get("name"),
                    settings=cfg.get("settings"),
                    delegate_descriptor_id=cfg.get("delegate_descriptor_id"),
                    enabled=cfg.get("enabled"),
                    clean_text=cfg.get("clean_text"),
                    force_lower_case=cfg.get("force_lower_case"),
                    default_value=cfg.get("default_value"),
                    storage_duration=cfg.get("storage_duration"),
                )
            elif action == "delete_data_element":
                de_id = cfg.get("data_element_id")
                if not de_id:
                    return {"error": True, "message": "config.data_element_id is required"}
                return await launch.delete_data_element(client_id, client_secret, org_id, de_id)
            elif action == "create_library":
                required = ["property_id", "name", "environment_id"]
                missing = [k for k in required if not cfg.get(k)]
                if missing:
                    return {"error": True, "message": f"config missing required keys: {missing}"}
                return await launch.create_library(
                    client_id,
                    client_secret,
                    org_id,
                    cfg["property_id"],
                    name=cfg["name"],
                    environment_id=cfg["environment_id"],
                )
            elif action == "add_resources_to_library":
                if not cfg.get("library_id") or not cfg.get("resources"):
                    return {"error": True, "message": "config.library_id and config.resources are required"}
                return await launch.add_resources_to_library(
                    client_id,
                    client_secret,
                    org_id,
                    cfg["library_id"],
                    cfg["resources"],
                )
            elif action == "build_library":
                if not cfg.get("library_id"):
                    return {"error": True, "message": "config.library_id is required"}
                return await launch.build_library(client_id, client_secret, org_id, cfg["library_id"])
            elif action == "transition_library":
                if not cfg.get("library_id") or not cfg.get("action"):
                    return {"error": True, "message": "config.library_id and config.action are required"}
                return await launch.transition_library(
                    client_id,
                    client_secret,
                    org_id,
                    cfg["library_id"],
                    cfg["action"],
                )
            return {"error": True, "message": f"Unknown action '{action}' for Adobe Launch tagmanager_write"}

        return {"error": True, "message": f"Unknown platform '{platform}'"}
