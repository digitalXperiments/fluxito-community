"""
Analytics Mega Tools — 3-tool split pattern
Mirrors the GTM tools pattern: analytics_read / analytics_audit / analytics_write

All tools route through platform → action → GA4 connector method.
User identity is never a parameter — always resolved from MCP session via app_state.

Currently implemented platforms: ga4
Scaffolded (graceful stubs): amplitude, mixpanel, posthog
"""

from typing import Annotated, Any, Literal

from pydantic import BeforeValidator

import app.app_state as state
from app.cache import cached_tool_response
from app.config import settings
from app.connectors.adobe_analytics import validate_project_id
from app.tools.shared_helpers import (
    decrypt_field,
    get_current_user,
    get_encrypted_credential_conn,
    get_google_conn_id,
)

# Coerce numeric IDs sent as JSON integers to strings (same convention as GTM tools)
CoercedStr = Annotated[str, BeforeValidator(str)]


def _coerce_str_list(v: Any) -> Any:
    """Coerce a list whose items may be GA4-style {"name": "x"} dicts to plain strings."""
    if isinstance(v, list):
        result = []
        for item in v:
            if isinstance(item, dict):
                # GA4 API format: {"name": "sessions"} → "sessions"
                result.append(str(item.get("name", item.get("expression", next(iter(item.values()), "")))))
            else:
                result.append(str(item))
        return result
    return v


# list[str] that also accepts items in GA4 API object format: {"name": "..."}
CoercedStrList = Annotated[list[str], BeforeValidator(_coerce_str_list)]


def _user():
    return get_current_user()


def _conn():
    """Return Google OAuth connection_id, scoped to active project."""
    return get_google_conn_id()


def _no_ga4():
    from app.auth.mcp_session_manager import no_ga4_response

    return no_ga4_response(settings.APP_BASE_URL)


def _normalize_property_id(property_id: str | None) -> str | None:
    """Auto-prefix 'properties/' if the caller passes a bare numeric ID."""
    if property_id and not property_id.startswith("properties/"):
        property_id = f"properties/{property_id}"
    return property_id


def _require_property(action: str, property_id: str | None):
    if not property_id:
        return {"error": True, "message": f"property_id is required for action '{action}'"}
    return None


def _require_dates(action: str, start: str | None, end: str | None):
    if not start or not end:
        return {"error": True, "message": f"start_date and end_date are required for action '{action}'"}
    return None


def _no_amplitude():
    from app.auth.mcp_session_manager import no_amplitude_response

    return no_amplitude_response(settings.APP_BASE_URL)


def _no_adobe_analytics():
    from app.auth.mcp_session_manager import no_adobe_analytics_response

    return no_adobe_analytics_response(settings.APP_BASE_URL)


async def _get_amplitude_conn(user_id: str):
    """Fetch user's active Amplitude connection and decrypt credentials."""
    from app.models.credential_connection import AmplitudeConnection

    conn = await get_encrypted_credential_conn(AmplitudeConnection, user_id)
    if not conn:
        return None, None, None
    api_key = decrypt_field(conn.api_key_encrypted)
    secret_key = decrypt_field(conn.secret_key_encrypted)
    return str(conn.id), api_key, secret_key


async def _get_adobe_conn(user_id: str):
    """Fetch user's active Adobe connection and decrypt credentials.

    Returns (conn_id, client_id, client_secret, org_id, company_id).
    """
    from app.models.credential_connection import AdobeConnection

    conn = await get_encrypted_credential_conn(
        AdobeConnection,
        user_id,
        extra_filters=[AdobeConnection.has_analytics == True],
    )
    if not conn:
        return None, None, None, None, None
    client_id = decrypt_field(conn.client_id_encrypted)
    client_secret = decrypt_field(conn.client_secret_encrypted)
    return str(conn.id), client_id, client_secret, conn.org_id, conn.company_id


async def _persist_adobe_company_id(conn_id: str, company_id: str) -> None:
    """Best-effort: store a discovered globalCompanyId on the connection."""
    if not conn_id or not company_id:
        return
    try:
        import uuid as _uuid

        from sqlalchemy import update

        from app.models.credential_connection import AdobeConnection

        factory = getattr(state, "db_session_factory", None)
        if factory is None:
            return
        async with factory() as db:
            await db.execute(
                update(AdobeConnection)
                .where(AdobeConnection.id == _uuid.UUID(str(conn_id)))
                .values(company_id=company_id)
            )
            await db.commit()
    except Exception:
        logger = __import__("logging").getLogger(__name__)
        logger.warning("Could not persist Adobe company_id for conn %s", conn_id, exc_info=True)


async def _adobe_session(user):
    """Resolve connector + creds + globalCompanyId, or an error envelope."""
    if not user or not getattr(user, "has_adobe_analytics", False):
        return None, _no_adobe_analytics()
    conn_id, client_id, client_secret, org_id, company_id = await _get_adobe_conn(user.user_id)
    if not client_id:
        return None, _no_adobe_analytics()
    adobe = state.adobe_analytics_connector
    if adobe is None:
        return None, {
            "error": True,
            "error_type": "server_error",
            "message": "adobe_analytics_connector is not initialised.",
        }
    if not company_id:
        resolved = await adobe.resolve_company_id(client_id, client_secret, org_id)
        if resolved.get("error"):
            return None, resolved
        company_id = resolved.get("company_id")
        if company_id:
            await _persist_adobe_company_id(str(conn_id), str(company_id))
    return {
        "adobe": adobe,
        "conn_id": conn_id,
        "client_id": client_id,
        "client_secret": client_secret,
        "org_id": org_id,
        "company_id": company_id,
    }, None


def _no_mixpanel():
    from app.auth.mcp_session_manager import no_mixpanel_response

    return no_mixpanel_response(settings.APP_BASE_URL)


def _no_posthog():
    from app.auth.mcp_session_manager import no_posthog_response

    return no_posthog_response(settings.APP_BASE_URL)


async def _get_mixpanel_conn(user_id: str):
    """Fetch user's active Mixpanel connection and decrypt credentials."""
    from app.models.credential_connection import MixpanelConnection

    conn = await get_encrypted_credential_conn(MixpanelConnection, user_id)
    if not conn:
        return None, None, None
    api_key = decrypt_field(conn.api_key_encrypted)  # api_secret
    secret_key = decrypt_field(conn.secret_key_encrypted)  # service_token
    return str(conn.id), api_key, secret_key


async def _get_posthog_conn(user_id: str):
    """Fetch user's active PostHog connection and decrypt credentials."""
    from app.models.credential_connection import PostHogConnection

    conn = await get_encrypted_credential_conn(PostHogConnection, user_id)
    if not conn:
        return None, None, None, None
    api_key = decrypt_field(conn.api_key_encrypted)
    return str(conn.id), api_key, conn.project_host, conn.external_project_id


def register_analytics_tools(mcp_server):
    # -------------------------------------------------------------------------
    # analytics_read — Layer 1: Data access
    # -------------------------------------------------------------------------

    @mcp_server.tool("analytics_read")
    async def analytics_read(
        platform: Literal["ga4", "amplitude", "mixpanel", "posthog", "adobe_analytics"] | None = None,
        action: str = "",
        property_id: CoercedStr | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        metrics: CoercedStrList | None = None,
        dimensions: CoercedStrList | None = None,
        limit: int = 100,
        event_name: str | None = None,
        days_back: int = 30,
        previous_start: str | None = None,
        previous_end: str | None = None,
        config: dict[str, Any] | None = None,
        project_id: CoercedStr | None = None,
        expansion: CoercedStrList | None = None,
        include_type: str | None = None,
        page: int | None = None,
    ) -> dict:
        """Reads analytics data. Use analytics_audit for health checks, analytics_write for config changes.

        platform: ga4 | amplitude | mixpanel | posthog | adobe_analytics

        GA4 actions (property_id via list_properties first):
          list_properties, run_report*, list_events*, get_event_detail*(+event_name),
          get_conversion_events*, get_realtime(property_id only),
          compare_date_ranges*(+metrics,previous_start,previous_end),
          list_data_streams, list_custom_dimensions, list_custom_metrics, list_audiences
          *=needs property_id+start_date+end_date; run_report also needs dimensions+metrics

        Amplitude actions:
          list_events, get_event_properties(event_name), get_user_properties,
          query_events(event_name+dates), get_active_users(dates), get_retention(dates),
          get_funnel(dates+metrics=[events]), get_revenue(dates), list_cohorts

        Mixpanel actions (same as Amplitude):
          list_events, get_event_properties(event_name), get_user_properties,
          query_events(event_name+dates), get_active_users(dates), get_retention(dates),
          get_funnel(dates+metrics=[events]), get_revenue(dates), list_cohorts

        PostHog actions (same as Amplitude):
          list_events, get_event_properties(event_name), get_user_properties,
          query_events(event_name+dates), get_active_users(dates), get_retention(dates),
          get_funnel(dates+metrics=[events]), get_revenue(dates), list_cohorts

        Adobe actions (property_id=rsid):
          list_report_suites, get_dimensions(rsid), get_metrics(rsid),
          run_report(rsid+dims+metrics+dates), get_segments, get_calculated_metrics,
          adobe_workspace_list_projects (optional expansion/include_type/page/locale),
          adobe_workspace_get_project(+project_id; always expansion=definition),
          adobe_workspace_build_definition(config.tables or definition),
          adobe_workspace_validate_project(config.rsid + definition or tables)
        """
        if not platform:
            return {
                "error": True,
                "error_type": "missing_required_param",
                "message": "platform is required. Pass platform='ga4', 'amplitude', 'mixpanel', 'posthog', or 'adobe_analytics' in params.",
            }
        u = _user()

        if platform == "ga4":
            if not u or not u.has_ga4:
                return _no_ga4()
            ga4 = state.ga4_connector
            conn_id = _conn()
            if not conn_id:
                return _no_ga4()

            # Validate action name first before checking required params
            _VALID_GA4_READ_ACTIONS = {
                "list_properties",
                "run_report",
                "list_events",
                "get_event_detail",
                "get_conversion_events",
                "get_realtime",
                "compare_date_ranges",
                "list_data_streams",
                "list_custom_dimensions",
                "list_custom_metrics",
                "list_audiences",
            }
            if action not in _VALID_GA4_READ_ACTIONS:
                return {
                    "error": True,
                    "message": f"Unknown action '{action}' for GA4 analytics_read. "
                    f"Valid actions: {', '.join(sorted(_VALID_GA4_READ_ACTIONS))}",
                }

            # No property_id needed
            if action == "list_properties":
                return await cached_tool_response(
                    f"cache:ga4:list_properties:{conn_id}",
                    600,
                    ga4.list_properties,
                    conn_id,
                )

            # property_id required for everything below
            property_id = _normalize_property_id(property_id)
            err = _require_property(action, property_id)
            if err:
                return err

            if action == "run_report":
                err = _require_dates(action, start_date, end_date)
                if err:
                    return err
                if not dimensions or not metrics:
                    return {
                        "error": True,
                        "message": "dimensions and metrics lists are required for run_report",
                    }
                dims_key = ",".join(sorted(dimensions))
                mets_key = ",".join(sorted(metrics))
                return await cached_tool_response(
                    f"cache:ga4:report:{conn_id}:{property_id}:{dims_key}:{mets_key}:{start_date}:{end_date}:{limit}",
                    120,
                    ga4.run_report,
                    conn_id,
                    property_id,
                    dimensions=dimensions,
                    metrics=metrics,
                    date_range_start=start_date,
                    date_range_end=end_date,
                    limit=limit,
                )

            elif action == "list_events":
                err = _require_dates(action, start_date, end_date)
                if err:
                    return err
                return await cached_tool_response(
                    f"cache:ga4:events:{conn_id}:{property_id}:{start_date}:{end_date}",
                    120,
                    ga4.list_events,
                    conn_id,
                    property_id,
                    start_date,
                    end_date,
                )

            elif action == "get_event_detail":
                err = _require_dates(action, start_date, end_date)
                if err:
                    return err
                if not event_name:
                    return {"error": True, "message": "event_name is required for get_event_detail"}
                return await ga4.get_event_detail(conn_id, property_id, event_name, start_date, end_date)

            elif action == "get_conversion_events":
                err = _require_dates(action, start_date, end_date)
                if err:
                    return err
                return await ga4.get_conversion_events(conn_id, property_id, start_date, end_date)

            elif action == "get_realtime":
                return await cached_tool_response(
                    f"cache:ga4:realtime:{conn_id}:{property_id}",
                    30,
                    ga4.get_realtime_data,
                    conn_id,
                    property_id,
                )

            elif action == "compare_date_ranges":
                if not metrics:
                    return {"error": True, "message": "metrics list is required for compare_date_ranges"}
                err = _require_dates(action, start_date, end_date)
                if err:
                    return err
                if not previous_start or not previous_end:
                    return {
                        "error": True,
                        "message": "previous_start and previous_end are required for compare_date_ranges",
                    }
                return await ga4.compare_date_ranges(
                    conn_id,
                    property_id,
                    metrics=metrics,
                    current_start=start_date,
                    current_end=end_date,
                    previous_start=previous_start,
                    previous_end=previous_end,
                )

            elif action == "list_data_streams":
                return await cached_tool_response(
                    f"cache:ga4:streams:{conn_id}:{property_id}",
                    600,
                    ga4.list_data_streams,
                    conn_id,
                    property_id,
                )

            elif action == "list_custom_dimensions":
                return await cached_tool_response(
                    f"cache:ga4:custom_dims:{conn_id}:{property_id}",
                    600,
                    ga4.list_custom_dimensions,
                    conn_id,
                    property_id,
                )

            elif action == "list_custom_metrics":
                return await cached_tool_response(
                    f"cache:ga4:custom_mets:{conn_id}:{property_id}",
                    600,
                    ga4.list_custom_metrics,
                    conn_id,
                    property_id,
                )

            elif action == "list_audiences":
                return await cached_tool_response(
                    f"cache:ga4:audiences:{conn_id}:{property_id}",
                    600,
                    ga4.list_audiences,
                    conn_id,
                    property_id,
                )

            return {"error": True, "message": f"Unknown action '{action}' for GA4 analytics_read"}

        elif platform == "amplitude":
            if not u or not u.has_amplitude:
                return _no_amplitude()
            conn_id, api_key, secret_key = await _get_amplitude_conn(u.user_id)
            if not api_key:
                return _no_amplitude()
            amp = state.amplitude_connector

            if action == "list_events":
                return await cached_tool_response(
                    f"cache:amp:events:{conn_id}",
                    300,
                    amp.get_events_list,
                    api_key,
                    secret_key,
                )
            elif action == "get_event_properties":
                if not event_name:
                    return {"error": True, "message": "event_name is required for get_event_properties"}
                return await amp.get_event_properties(api_key, secret_key, event_name)
            elif action == "get_user_properties":
                return await cached_tool_response(
                    f"cache:amp:user_props:{conn_id}",
                    300,
                    amp.get_user_properties,
                    api_key,
                    secret_key,
                )
            elif action == "query_events":
                err = _require_dates(action, start_date, end_date)
                if err:
                    return err
                if not event_name:
                    return {"error": True, "message": "event_name is required for query_events"}
                return await amp.query_events(api_key, secret_key, start_date, end_date, event_name)
            elif action == "get_active_users":
                err = _require_dates(action, start_date, end_date)
                if err:
                    return err
                return await cached_tool_response(
                    f"cache:amp:active:{conn_id}:{start_date}:{end_date}",
                    120,
                    amp.get_active_users,
                    api_key,
                    secret_key,
                    start_date,
                    end_date,
                )
            elif action == "get_retention":
                err = _require_dates(action, start_date, end_date)
                if err:
                    return err
                return await amp.get_retention(api_key, secret_key, start_date, end_date)
            elif action == "get_funnel":
                err = _require_dates(action, start_date, end_date)
                if err:
                    return err
                return await amp.get_funnel(api_key, secret_key, start_date, end_date, metrics or [])
            elif action == "get_revenue":
                err = _require_dates(action, start_date, end_date)
                if err:
                    return err
                return await amp.get_revenue(api_key, secret_key, start_date, end_date)
            elif action == "list_cohorts":
                return await cached_tool_response(
                    f"cache:amp:cohorts:{conn_id}",
                    300,
                    amp.list_cohorts,
                    api_key,
                    secret_key,
                )
            return {"error": True, "message": f"Unknown action '{action}' for Amplitude analytics_read"}

        elif platform == "mixpanel":
            if not u or not u.has_mixpanel:
                return _no_mixpanel()
            conn_id, api_key, secret_key = await _get_mixpanel_conn(u.user_id)
            if not api_key:
                return _no_mixpanel()
            mp = state.mixpanel_connector

            if action == "list_events":
                return await cached_tool_response(
                    f"cache:mp:events:{conn_id}",
                    300,
                    mp.get_events_list,
                    api_key,
                    secret_key,
                )
            elif action == "get_event_properties":
                if not event_name:
                    return {"error": True, "message": "event_name is required for get_event_properties"}
                return await mp.get_event_properties(api_key, secret_key, event_name)
            elif action == "get_user_properties":
                return await cached_tool_response(
                    f"cache:mp:user_props:{conn_id}",
                    300,
                    mp.get_user_properties,
                    api_key,
                    secret_key,
                )
            elif action == "query_events":
                err = _require_dates(action, start_date, end_date)
                if err:
                    return err
                if not event_name:
                    return {"error": True, "message": "event_name is required for query_events"}
                return await mp.query_events(api_key, secret_key, start_date, end_date, event_name)
            elif action == "get_active_users":
                err = _require_dates(action, start_date, end_date)
                if err:
                    return err
                return await cached_tool_response(
                    f"cache:mp:active:{conn_id}:{start_date}:{end_date}",
                    120,
                    mp.get_active_users,
                    api_key,
                    secret_key,
                    start_date,
                    end_date,
                )
            elif action == "get_retention":
                err = _require_dates(action, start_date, end_date)
                if err:
                    return err
                return await mp.get_retention(api_key, secret_key, start_date, end_date)
            elif action == "get_funnel":
                err = _require_dates(action, start_date, end_date)
                if err:
                    return err
                return await mp.get_funnel(api_key, secret_key, start_date, end_date, metrics or [])
            elif action == "get_revenue":
                err = _require_dates(action, start_date, end_date)
                if err:
                    return err
                return await mp.get_revenue(api_key, secret_key, start_date, end_date)
            elif action == "list_cohorts":
                return await cached_tool_response(
                    f"cache:mp:cohorts:{conn_id}",
                    300,
                    mp.list_cohorts,
                    api_key,
                    secret_key,
                )
            return {"error": True, "message": f"Unknown action '{action}' for Mixpanel analytics_read"}

        elif platform == "posthog":
            if not u or not u.has_posthog:
                return _no_posthog()
            conn_id, api_key, project_host, project_id = await _get_posthog_conn(u.user_id)
            if not api_key:
                return _no_posthog()
            ph = state.posthog_connector

            if action == "list_events":
                return await cached_tool_response(
                    f"cache:ph:events:{conn_id}",
                    300,
                    ph.get_events_list,
                    api_key,
                    project_host,
                    project_id,
                )
            elif action == "get_event_properties":
                if not event_name:
                    return {"error": True, "message": "event_name is required for get_event_properties"}
                return await ph.get_event_properties(api_key, project_host, project_id, event_name)
            elif action == "get_user_properties":
                return await cached_tool_response(
                    f"cache:ph:user_props:{conn_id}",
                    300,
                    ph.get_user_properties,
                    api_key,
                    project_host,
                    project_id,
                )
            elif action == "query_events":
                err = _require_dates(action, start_date, end_date)
                if err:
                    return err
                if not event_name:
                    return {"error": True, "message": "event_name is required for query_events"}
                return await ph.query_events(
                    api_key, project_host, project_id, start_date, end_date, event_name
                )
            elif action == "get_active_users":
                err = _require_dates(action, start_date, end_date)
                if err:
                    return err
                return await cached_tool_response(
                    f"cache:ph:active:{conn_id}:{start_date}:{end_date}",
                    120,
                    ph.get_active_users,
                    api_key,
                    project_host,
                    project_id,
                    start_date,
                    end_date,
                )
            elif action == "get_retention":
                err = _require_dates(action, start_date, end_date)
                if err:
                    return err
                return await ph.get_retention(api_key, project_host, project_id, start_date, end_date)
            elif action == "get_funnel":
                err = _require_dates(action, start_date, end_date)
                if err:
                    return err
                return await ph.get_funnel(
                    api_key, project_host, project_id, start_date, end_date, metrics or []
                )
            elif action == "get_revenue":
                err = _require_dates(action, start_date, end_date)
                if err:
                    return err
                return await ph.get_revenue(api_key, project_host, project_id, start_date, end_date)
            elif action == "list_cohorts":
                return await cached_tool_response(
                    f"cache:ph:cohorts:{conn_id}",
                    300,
                    ph.list_cohorts,
                    api_key,
                    project_host,
                    project_id,
                )
            return {"error": True, "message": f"Unknown action '{action}' for PostHog analytics_read"}

        elif platform == "adobe_analytics":
            session, sess_err = await _adobe_session(u)
            if sess_err:
                return sess_err
            adobe = session["adobe"]
            conn_id = session["conn_id"]
            client_id = session["client_id"]
            client_secret = session["client_secret"]
            org_id = session["org_id"]
            company_id = session["company_id"]

            if action == "list_report_suites":
                return await cached_tool_response(
                    f"cache:adobe:suites:{conn_id}",
                    600,
                    adobe.list_report_suites,
                    client_id,
                    client_secret,
                    org_id,
                    company_id=company_id,
                )
            elif action == "get_dimensions":
                if not property_id:
                    return {"error": True, "message": "property_id (rsid) is required for get_dimensions"}
                return await cached_tool_response(
                    f"cache:adobe:dims:{conn_id}:{property_id}",
                    600,
                    adobe.get_dimensions,
                    client_id,
                    client_secret,
                    org_id,
                    property_id,
                    company_id=company_id,
                )
            elif action == "get_metrics":
                if not property_id:
                    return {"error": True, "message": "property_id (rsid) is required for get_metrics"}
                return await cached_tool_response(
                    f"cache:adobe:metrics:{conn_id}:{property_id}",
                    600,
                    adobe.get_metrics,
                    client_id,
                    client_secret,
                    org_id,
                    property_id,
                    company_id=company_id,
                )
            elif action == "run_report":
                if not property_id:
                    return {"error": True, "message": "property_id (rsid) is required for run_report"}
                err = _require_dates(action, start_date, end_date)
                if err:
                    return err
                if not metrics:
                    return {
                        "error": True,
                        "message": "metrics list is required for run_report (e.g. ['visits'] or ['metrics/visits'])",
                    }
                return await adobe.run_report(
                    client_id,
                    client_secret,
                    org_id,
                    property_id,
                    dimensions=dimensions or [],
                    metrics=metrics,
                    date_range={"start": start_date, "end": end_date},
                    limit=limit,
                    company_id=company_id,
                )
            elif action == "get_segments":
                return await cached_tool_response(
                    f"cache:adobe:segments:{conn_id}",
                    300,
                    adobe.get_segments,
                    client_id,
                    client_secret,
                    org_id,
                    property_id,
                    company_id=company_id,
                )
            elif action == "get_calculated_metrics":
                return await cached_tool_response(
                    f"cache:adobe:calc_metrics:{conn_id}",
                    300,
                    adobe.get_calculated_metrics,
                    client_id,
                    client_secret,
                    org_id,
                    property_id,
                    company_id=company_id,
                )
            elif action == "list_projects":
                cfg = config or {}
                return await adobe.list_projects(
                    client_id,
                    client_secret,
                    org_id,
                    expansion=expansion if expansion is not None else cfg.get("expansion"),
                    include_type=include_type
                    if include_type is not None
                    else (cfg.get("include_type") or cfg.get("includeType")),
                    limit=cfg.get("limit", limit),
                    page=page if page is not None else cfg.get("page"),
                    locale=cfg.get("locale"),
                    company_id=company_id,
                )
            elif action == "get_project":
                cfg = config or {}
                pid = project_id or cfg.get("project_id") or cfg.get("id")
                if not pid:
                    return {
                        "error": True,
                        "error_type": "missing_required_param",
                        "message": "project_id is required for adobe_workspace_get_project",
                    }
                id_err = validate_project_id(pid)
                if id_err:
                    return id_err
                return await adobe.get_project(
                    client_id,
                    client_secret,
                    org_id,
                    str(pid),
                    expansion=expansion if expansion is not None else cfg.get("expansion"),
                    company_id=company_id,
                )
            elif action == "build_definition":
                cfg = config or {}
                rsid = cfg.get("rsid") or property_id
                if not rsid:
                    return {
                        "error": True,
                        "error_type": "missing_required_param",
                        "message": "config.rsid (or property_id) is required for adobe_workspace_build_definition",
                    }
                return await adobe.build_project_definition(
                    rsid=str(rsid),
                    tables=cfg.get("tables") if isinstance(cfg.get("tables"), list) else None,
                    date_range=cfg.get("date_range"),
                    definition=cfg.get("definition") if isinstance(cfg.get("definition"), dict) else None,
                )
            elif action == "validate_project":
                cfg = config or {}
                rsid = cfg.get("rsid") or property_id
                if not rsid:
                    return {
                        "error": True,
                        "error_type": "missing_required_param",
                        "message": "config.rsid (or property_id) is required for adobe_workspace_validate_project",
                    }
                built = await adobe.build_project_definition(
                    rsid=str(rsid),
                    tables=cfg.get("tables") if isinstance(cfg.get("tables"), list) else None,
                    date_range=cfg.get("date_range"),
                    definition=cfg.get("definition") if isinstance(cfg.get("definition"), dict) else None,
                )
                if built.get("error"):
                    return built
                return await adobe.validate_project(
                    client_id,
                    client_secret,
                    org_id,
                    rsid=str(rsid),
                    definition=built["definition"],
                    company_id=company_id,
                    name=cfg.get("name"),
                )
            return {"error": True, "message": f"Unknown action '{action}' for Adobe Analytics analytics_read"}

        return {"error": True, "message": f"Unknown platform '{platform}'"}

    # -------------------------------------------------------------------------
    # analytics_audit — Layer 2: Intelligence / health checks
    # -------------------------------------------------------------------------

    @mcp_server.tool("analytics_audit")
    async def analytics_audit(
        platform: Literal["ga4", "amplitude", "mixpanel", "posthog", "adobe_analytics"] | None = None,
        action: str = "",
        property_id: CoercedStr | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        days_back: int = 30,
        sensitivity: Literal["low", "medium", "high"] = "medium",
    ) -> dict:
        """Audits analytics health. Returns scored findings + recommendations.

        platform: ga4 | amplitude | mixpanel | posthog | adobe_analytics

        GA4 (all need property_id):
          audit_ecommerce(+dates), check_data_anomalies(+days_back?,sensitivity?),
          schema_validator(+dates), audit_data_streams, audit_custom_definitions,
          audit_conversion_events(dates optional)

        Amplitude: check_taxonomy_health, check_event_volume_anomalies(days_back?)
        Mixpanel: check_taxonomy_health, check_event_volume_anomalies(days_back?)
        PostHog: check_taxonomy_health, check_event_volume_anomalies(days_back?)
        Adobe (property_id=rsid): audit_report_suite, check_data_quality(+days_back?)
        """
        if not platform:
            return {
                "error": True,
                "error_type": "missing_required_param",
                "message": "platform is required. Pass platform='ga4', 'amplitude', 'mixpanel', 'posthog', or 'adobe_analytics' in params.",
            }
        u = _user()

        if platform == "ga4":
            if not u or not u.has_ga4:
                return _no_ga4()
            ga4 = state.ga4_connector
            conn_id = _conn()

            # Validate action name first
            _VALID_GA4_AUDIT_ACTIONS = {
                "audit_ecommerce",
                "check_data_anomalies",
                "schema_validator",
                "audit_data_streams",
                "audit_custom_definitions",
                "audit_conversion_events",
            }
            if action not in _VALID_GA4_AUDIT_ACTIONS:
                return {
                    "error": True,
                    "message": f"Unknown action '{action}' for GA4 analytics_audit. "
                    f"Valid actions: {', '.join(sorted(_VALID_GA4_AUDIT_ACTIONS))}",
                }

            # property_id required for all GA4 audit actions
            property_id = _normalize_property_id(property_id)
            err = _require_property(action, property_id)
            if err:
                return err

            if action == "audit_ecommerce":
                err = _require_dates(action, start_date, end_date)
                if err:
                    return err
                return await cached_tool_response(
                    f"cache:ga4:audit_ecom:{conn_id}:{property_id}:{start_date}:{end_date}",
                    300,
                    ga4.audit_ecommerce,
                    conn_id,
                    property_id,
                    start_date,
                    end_date,
                )

            elif action == "check_data_anomalies":
                return await cached_tool_response(
                    f"cache:ga4:anomalies:{conn_id}:{property_id}:{days_back}:{sensitivity}",
                    300,
                    ga4.check_data_anomalies,
                    conn_id,
                    property_id,
                    days_back=days_back,
                    sensitivity=sensitivity,
                )

            elif action == "schema_validator":
                err = _require_dates(action, start_date, end_date)
                if err:
                    return err
                return await cached_tool_response(
                    f"cache:ga4:schema:{conn_id}:{property_id}:{start_date}:{end_date}",
                    300,
                    ga4.schema_validator,
                    conn_id,
                    property_id,
                    start_date,
                    end_date,
                )

            elif action == "audit_data_streams":
                return await cached_tool_response(
                    f"cache:ga4:audit_streams:{conn_id}:{property_id}",
                    300,
                    ga4.audit_data_streams,
                    conn_id,
                    property_id,
                )

            elif action == "audit_custom_definitions":
                return await cached_tool_response(
                    f"cache:ga4:audit_custom:{conn_id}:{property_id}",
                    300,
                    ga4.audit_custom_definitions,
                    conn_id,
                    property_id,
                )

            elif action == "audit_conversion_events":
                # Dates are optional — connector has sensible defaults
                s = start_date or "30daysAgo"
                e = end_date or "today"
                result = await cached_tool_response(
                    f"cache:ga4:audit_conv:{conn_id}:{property_id}:{s}:{e}",
                    300,
                    ga4.audit_conversion_events,
                    conn_id,
                    property_id,
                    s,
                    e,
                )

                return result

            return {"error": True, "message": f"Unknown action '{action}' for GA4 analytics_audit"}

        elif platform == "amplitude":
            if not u or not u.has_amplitude:
                return _no_amplitude()
            conn_id, api_key, secret_key = await _get_amplitude_conn(u.user_id)
            if not api_key:
                return _no_amplitude()
            amp = state.amplitude_connector

            if action == "check_taxonomy_health":
                return await cached_tool_response(
                    f"cache:amp:tax_health:{conn_id}",
                    300,
                    amp.check_taxonomy_health,
                    api_key,
                    secret_key,
                )
            elif action == "check_event_volume_anomalies":
                return await amp.check_event_volume_anomalies(api_key, secret_key, days_back=days_back)
            return {"error": True, "message": f"Unknown action '{action}' for Amplitude analytics_audit"}

        elif platform == "mixpanel":
            if not u or not u.has_mixpanel:
                return _no_mixpanel()
            conn_id, api_key, secret_key = await _get_mixpanel_conn(u.user_id)
            if not api_key:
                return _no_mixpanel()
            mp = state.mixpanel_connector

            if action == "check_taxonomy_health":
                return await cached_tool_response(
                    f"cache:mp:tax_health:{conn_id}",
                    300,
                    mp.check_taxonomy_health,
                    api_key,
                    secret_key,
                )
            elif action == "check_event_volume_anomalies":
                return await mp.check_event_volume_anomalies(api_key, secret_key, days_back=days_back)
            return {"error": True, "message": f"Unknown action '{action}' for Mixpanel analytics_audit"}

        elif platform == "posthog":
            if not u or not u.has_posthog:
                return _no_posthog()
            conn_id, api_key, project_host, project_id = await _get_posthog_conn(u.user_id)
            if not api_key:
                return _no_posthog()
            ph = state.posthog_connector

            if action == "check_taxonomy_health":
                return await cached_tool_response(
                    f"cache:ph:tax_health:{conn_id}",
                    300,
                    ph.check_taxonomy_health,
                    api_key,
                    project_host,
                    project_id,
                )
            elif action == "check_event_volume_anomalies":
                return await ph.check_event_volume_anomalies(
                    api_key, project_host, project_id, days_back=days_back
                )
            return {"error": True, "message": f"Unknown action '{action}' for PostHog analytics_audit"}

        elif platform == "adobe_analytics":
            session, sess_err = await _adobe_session(u)
            if sess_err:
                return sess_err
            adobe = session["adobe"]
            conn_id = session["conn_id"]
            client_id = session["client_id"]
            client_secret = session["client_secret"]
            org_id = session["org_id"]
            company_id = session["company_id"]

            if action == "audit_report_suite":
                if not property_id:
                    return {"error": True, "message": "property_id (rsid) is required for audit_report_suite"}
                return await cached_tool_response(
                    f"cache:adobe:audit:{conn_id}:{property_id}",
                    300,
                    adobe.audit_report_suite,
                    client_id,
                    client_secret,
                    org_id,
                    property_id,
                    company_id=company_id,
                )
            elif action == "check_data_quality":
                if not property_id:
                    return {"error": True, "message": "property_id (rsid) is required for check_data_quality"}
                return await adobe.check_data_quality(
                    client_id,
                    client_secret,
                    org_id,
                    property_id,
                    days_back=days_back,
                    company_id=company_id,
                )
            return {
                "error": True,
                "message": f"Unknown action '{action}' for Adobe Analytics analytics_audit",
            }

        return {"error": True, "message": f"Unknown platform '{platform}'"}

    # -------------------------------------------------------------------------
    # analytics_write — Layer 3: Write / admin operations
    # -------------------------------------------------------------------------

    @mcp_server.tool("analytics_write")
    async def analytics_write(
        platform: Literal["ga4", "amplitude", "mixpanel", "posthog", "adobe_analytics"] | None = None,
        action: str = "",
        property_id: CoercedStr | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict:
        """Writes analytics config. Requires full access tier. Use analytics_read first.

        platform: ga4 | amplitude | mixpanel | posthog | adobe_analytics. config=dict of action-specific keys.

        GA4: create_custom_dimension{display_name,parameter_name,scope},
          create_custom_metric{display_name,parameter_name},
          mark_event_as_conversion{event_name}, create_audience{display_name,membership_duration_days,filter_clauses}
        Amplitude: create_event_type{event_type}, update_event_type{event_type}, delete_event_type{event_type}
        Mixpanel: create_event_type{event_type}, update_event_type{event_type}, delete_event_type{event_type}
        PostHog: create_event_type{event_type}, update_event_type{event_type}, delete_event_type{event_type}
        Adobe: create_segment{name,rsid,definition}, update_segment{segment_id},
          delete_segment{segment_id}, create_calculated_metric{name,rsid,definition},
          delete_calculated_metric{metric_id},
          adobe_workspace_create_project{name,rsid, tables[] OR definition},
          adobe_workspace_update_project{project_id} (partial PUT; tables rebuilds
          the definition; merge_definition=true GET+merges definition),
          adobe_workspace_delete_project{project_id},
          adobe_workspace_copy_project{project_id,name}
        """
        if not platform:
            return {
                "error": True,
                "error_type": "missing_required_param",
                "message": "platform is required. Pass platform='ga4', 'amplitude', 'mixpanel', 'posthog', or 'adobe_analytics' in params.",
            }
        config = config or {}
        u = _user()

        if platform == "ga4":
            if not u or not u.has_ga4:
                return _no_ga4()

            # Scope check — write actions need analytics (full) scope
            scopes = u.connections[0].scopes if u.connections else []
            write_scope = "https://www.googleapis.com/auth/analytics"
            if write_scope not in scopes:
                return {
                    "error": True,
                    "error_type": "insufficient_scope",
                    "message": "GA4 write operations require full analytics scope.",
                    "action_required": f"Reconnect Google at {settings.APP_BASE_URL}/connect with 'Full Access' tier.",
                }

            ga4 = state.ga4_connector
            conn_id = _conn()
            property_id = _normalize_property_id(property_id)

            if action == "create_custom_dimension":
                required = ["display_name", "parameter_name", "scope"]
                missing = [k for k in required if not config.get(k)]
                if missing:
                    return {"error": True, "message": f"config is missing required keys: {missing}"}
                return await ga4.create_custom_dimension(
                    conn_id,
                    property_id,
                    display_name=config["display_name"],
                    parameter_name=config["parameter_name"],
                    scope=config["scope"],
                    description=config.get("description", ""),
                )

            elif action == "create_custom_metric":
                required = ["display_name", "parameter_name"]
                missing = [k for k in required if not config.get(k)]
                if missing:
                    return {"error": True, "message": f"config is missing required keys: {missing}"}
                return await ga4.create_custom_metric(conn_id, property_id, config)

            elif action == "mark_event_as_conversion":
                event_name = config.get("event_name")
                if not event_name:
                    return {"error": True, "message": "config.event_name is required"}
                is_conversion = config.get("is_conversion", True)
                return await ga4.mark_event_as_conversion(conn_id, property_id, event_name, is_conversion)

            elif action == "create_audience":
                required = ["display_name", "membership_duration_days", "filter_clauses"]
                missing = [k for k in required if k not in config]
                if missing:
                    return {"error": True, "message": f"config is missing required keys: {missing}"}
                return await ga4.create_audience(
                    conn_id,
                    property_id,
                    display_name=config["display_name"],
                    description=config.get("description", ""),
                    membership_duration_days=config["membership_duration_days"],
                    filter_clauses=config["filter_clauses"],
                )

            return {"error": True, "message": f"Unknown action '{action}' for GA4 analytics_write"}

        elif platform == "amplitude":
            if not u or not u.has_amplitude:
                return _no_amplitude()
            conn_id, api_key, secret_key = await _get_amplitude_conn(u.user_id)
            if not api_key:
                return _no_amplitude()
            amp = state.amplitude_connector

            if action == "create_event_type":
                if not config.get("event_type"):
                    return {"error": True, "message": "config.event_type is required"}
                return await amp.create_event_type(
                    api_key,
                    secret_key,
                    event_type=config["event_type"],
                    description=config.get("description"),
                    category=config.get("category"),
                )
            elif action == "update_event_type":
                if not config.get("event_type"):
                    return {"error": True, "message": "config.event_type is required"}
                return await amp.update_event_type(
                    api_key,
                    secret_key,
                    event_type=config["event_type"],
                    new_name=config.get("new_name"),
                    description=config.get("description"),
                    category=config.get("category"),
                )
            elif action == "delete_event_type":
                if not config.get("event_type"):
                    return {"error": True, "message": "config.event_type is required"}
                return await amp.delete_event_type(api_key, secret_key, config["event_type"])
            return {"error": True, "message": f"Unknown action '{action}' for Amplitude analytics_write"}

        elif platform == "mixpanel":
            if not u or not u.has_mixpanel:
                return _no_mixpanel()
            conn_id, api_key, secret_key = await _get_mixpanel_conn(u.user_id)
            if not api_key:
                return _no_mixpanel()
            mp = state.mixpanel_connector

            if action == "create_event_type":
                if not config.get("event_type"):
                    return {"error": True, "message": "config.event_type is required"}
                return await mp.create_event_type(
                    api_key,
                    secret_key,
                    event_type=config["event_type"],
                    description=config.get("description"),
                    category=config.get("category"),
                )
            elif action == "update_event_type":
                if not config.get("event_type"):
                    return {"error": True, "message": "config.event_type is required"}
                return await mp.update_event_type(
                    api_key,
                    secret_key,
                    event_type=config["event_type"],
                    new_name=config.get("new_name"),
                    description=config.get("description"),
                    category=config.get("category"),
                )
            elif action == "delete_event_type":
                if not config.get("event_type"):
                    return {"error": True, "message": "config.event_type is required"}
                return await mp.delete_event_type(api_key, secret_key, config["event_type"])
            return {"error": True, "message": f"Unknown action '{action}' for Mixpanel analytics_write"}

        elif platform == "posthog":
            if not u or not u.has_posthog:
                return _no_posthog()
            conn_id, api_key, project_host, project_id = await _get_posthog_conn(u.user_id)
            if not api_key:
                return _no_posthog()
            ph = state.posthog_connector

            if action == "create_event_type":
                if not config.get("event_type"):
                    return {"error": True, "message": "config.event_type is required"}
                return await ph.create_event_type(
                    api_key,
                    project_host,
                    project_id,
                    event_type=config["event_type"],
                    description=config.get("description"),
                    category=config.get("category"),
                )
            elif action == "update_event_type":
                if not config.get("event_type"):
                    return {"error": True, "message": "config.event_type is required"}
                return await ph.update_event_type(
                    api_key,
                    project_host,
                    project_id,
                    event_type=config["event_type"],
                    new_name=config.get("new_name"),
                    description=config.get("description"),
                    category=config.get("category"),
                )
            elif action == "delete_event_type":
                if not config.get("event_type"):
                    return {"error": True, "message": "config.event_type is required"}
                return await ph.delete_event_type(api_key, project_host, project_id, config["event_type"])
            return {"error": True, "message": f"Unknown action '{action}' for PostHog analytics_write"}

        elif platform == "adobe_analytics":
            session, sess_err = await _adobe_session(u)
            if sess_err:
                return sess_err
            adobe = session["adobe"]
            client_id = session["client_id"]
            client_secret = session["client_secret"]
            org_id = session["org_id"]
            company_id = session["company_id"]

            if action == "create_segment":
                required = ["name", "rsid", "definition"]
                missing = [k for k in required if not config.get(k)]
                if missing:
                    return {"error": True, "message": f"config missing required keys: {missing}"}
                return await adobe.create_segment(
                    client_id,
                    client_secret,
                    org_id,
                    name=config["name"],
                    description=config.get("description", ""),
                    rsid=config["rsid"],
                    definition=config["definition"],
                    company_id=company_id,
                )
            elif action == "update_segment":
                if not config.get("segment_id"):
                    return {"error": True, "message": "config.segment_id is required"}
                return await adobe.update_segment(
                    client_id,
                    client_secret,
                    org_id,
                    config["segment_id"],
                    name=config.get("name"),
                    description=config.get("description"),
                    definition=config.get("definition"),
                    company_id=company_id,
                )
            elif action == "delete_segment":
                if not config.get("segment_id"):
                    return {"error": True, "message": "config.segment_id is required"}
                return await adobe.delete_segment(
                    client_id, client_secret, org_id, config["segment_id"], company_id=company_id
                )
            elif action == "create_calculated_metric":
                required = ["name", "rsid", "definition"]
                missing = [k for k in required if not config.get(k)]
                if missing:
                    return {"error": True, "message": f"config missing required keys: {missing}"}
                return await adobe.create_calculated_metric(
                    client_id,
                    client_secret,
                    org_id,
                    name=config["name"],
                    description=config.get("description", ""),
                    rsid=config["rsid"],
                    definition=config["definition"],
                    company_id=company_id,
                )
            elif action == "delete_calculated_metric":
                if not config.get("metric_id"):
                    return {"error": True, "message": "config.metric_id is required"}
                return await adobe.delete_calculated_metric(
                    client_id, client_secret, org_id, config["metric_id"], company_id=company_id
                )
            elif action == "create_project":
                missing = [k for k in ("name", "rsid") if not config.get(k)]
                if missing:
                    return {
                        "error": True,
                        "error_type": "missing_required_param",
                        "message": f"config missing required keys: {missing}",
                    }
                definition = config.get("definition")
                tables = config.get("tables")
                if definition is not None and not isinstance(definition, dict):
                    return {
                        "error": True,
                        "error_type": "invalid_param",
                        "message": "config.definition must be a JSON object when provided.",
                    }
                if tables is not None and not isinstance(tables, list):
                    return {
                        "error": True,
                        "error_type": "invalid_param",
                        "message": "config.tables must be an array of {name?, metrics[], dimension?} objects.",
                    }
                extra = {k: v for k, v in config.items() if k in {"tags", "shares"}}
                return await adobe.create_project(
                    client_id,
                    client_secret,
                    org_id,
                    name=config["name"],
                    rsid=config["rsid"],
                    definition=definition if isinstance(definition, dict) else None,
                    description=config.get("description"),
                    extra=extra or None,
                    tables=tables if isinstance(tables, list) else None,
                    date_range=config.get("date_range"),
                    validate=bool(config.get("validate", True)),
                    company_id=company_id,
                )
            elif action == "update_project":
                pid = config.get("project_id") or config.get("id")
                if not pid:
                    return {
                        "error": True,
                        "error_type": "missing_required_param",
                        "message": (
                            "config.project_id is required (explicit Adobe Workspace id; "
                            "letters/digits/_/- only — no bulk or wildcard update)"
                        ),
                    }
                id_err = validate_project_id(pid)
                if id_err:
                    return id_err
                extra_updates = {k: v for k, v in config.items() if k in {"tags", "shares"}}
                tables = config.get("tables")
                if tables is not None and not isinstance(tables, list):
                    return {
                        "error": True,
                        "error_type": "invalid_param",
                        "message": "config.tables must be an array of {name?, metrics[], dimension?} objects.",
                    }
                return await adobe.update_project(
                    client_id,
                    client_secret,
                    org_id,
                    str(pid),
                    name=config.get("name"),
                    description=config.get("description"),
                    rsid=config.get("rsid"),
                    definition=config.get("definition")
                    if isinstance(config.get("definition"), dict)
                    else None,
                    owner=config.get("owner"),
                    updates=extra_updates or None,
                    merge_definition=bool(config.get("merge_definition")),
                    tables=tables if isinstance(tables, list) else None,
                    date_range=config.get("date_range"),
                    company_id=company_id,
                )
            elif action == "delete_project":
                pid = config.get("project_id") or config.get("id")
                if not pid:
                    return {
                        "error": True,
                        "error_type": "missing_required_param",
                        "message": (
                            "config.project_id is required (explicit Adobe Workspace id; "
                            "letters/digits/_/- only — no bulk or wildcard delete)"
                        ),
                    }
                id_err = validate_project_id(pid)
                if id_err:
                    return id_err
                return await adobe.delete_project(
                    client_id, client_secret, org_id, str(pid), company_id=company_id
                )
            elif action == "copy_project":
                pid = config.get("project_id") or config.get("id")
                new_name = config.get("name")
                if not pid:
                    return {
                        "error": True,
                        "error_type": "missing_required_param",
                        "message": "config.project_id is required for copy_project",
                    }
                id_err = validate_project_id(pid)
                if id_err:
                    return id_err
                if not new_name:
                    return {
                        "error": True,
                        "error_type": "missing_required_param",
                        "message": "config.name is required for copy_project (name of the duplicate)",
                    }
                return await adobe.copy_project(
                    client_id,
                    client_secret,
                    org_id,
                    str(pid),
                    name=str(new_name),
                    company_id=company_id,
                )
            return {
                "error": True,
                "message": f"Unknown action '{action}' for Adobe Analytics analytics_write",
            }

        return {"error": True, "message": f"Unknown platform '{platform}'"}
