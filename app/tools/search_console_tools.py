"""
Google Search Console (GSC) MCP Tools

Three-tool split mirroring the GA4/GTM pattern:
  - search_console_read   (Layer 1 — data access)
  - search_console_audit  (Layer 2 — intelligence / composite analysis)
  - search_console_write  (Layer 3 — sitemap submit/delete — scope-gated)

User identity is never a parameter — it's resolved from the MCP session
via app_state.current_user_ctx.
"""

from typing import Annotated, Literal

from pydantic import BeforeValidator

import app.app_state as state
from app.auth.mcp_session_manager import no_gsc_response
from app.auth.scopes import SCOPE_GSC_WRITE
from app.config import settings

CoercedStr = Annotated[str, BeforeValidator(str)]


def _user():
    return state.current_user_ctx.get()


def _no_gsc():
    return no_gsc_response(settings.APP_BASE_URL)


def _gsc_connection_id() -> str | None:
    """Pick the first Google connection that has a webmasters scope, scoped to active project."""
    # Prefer project-scoped connections
    project = state.current_project_ctx.get()
    connections = None
    if project and project.connections:
        connections = project.connections
    else:
        u = _user()
        if u and u.connections:
            connections = u.connections

    if not connections:
        return None
    for c in connections:
        scopes = getattr(c, "scopes", []) or []
        if any("webmasters" in s for s in scopes):
            return c.id
    # Fallback: first connection (scope check will fail at call time with a
    # clear error if GSC scope isn't actually granted)
    return connections[0].id


def _project_site_urls() -> list:
    """List of site_urls authorized for the active project, if any."""
    u = _user()
    if not u:
        return []
    return [s.get("site_url") for s in getattr(u, "search_console_sites", []) or [] if s.get("site_url")]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_search_console_tools(mcp_server):
    # ====================================================================
    # search_console_read  — Layer 1
    # ====================================================================
    @mcp_server.tool("search_console_read")
    async def search_console_read(
        action: Literal[
            "list_sites",
            "search_analytics",
            "list_sitemaps",
            "get_sitemap",
            "inspect_url",
        ]
        | None = None,
        site_url: CoercedStr | None = None,
        start_date: CoercedStr | None = None,
        end_date: CoercedStr | None = None,
        dimensions: list[CoercedStr] | None = None,
        search_type: CoercedStr | None = None,
        row_limit: int = 1000,
        start_row: int = 0,
        dimension_filter_groups: list | None = None,
        aggregation_type: CoercedStr | None = None,
        data_state: CoercedStr | None = None,
        feedpath: CoercedStr | None = None,
        inspection_url: CoercedStr | None = None,
        language_code: CoercedStr | None = None,
    ) -> dict:
        """
        Google Search Console — read-only data access.

        Actions:
          - list_sites: Enumerate verified GSC properties for this project.
          - search_analytics: Query impressions/clicks/CTR/position.
              Requires: site_url, start_date, end_date.
              Optional: dimensions (subset of query/page/country/device/date/searchAppearance),
                        search_type (web/image/video/news/discover/googleNews),
                        row_limit (max 25000), start_row (pagination),
                        dimension_filter_groups, aggregation_type, data_state ("final"|"all").
          - list_sitemaps: List sitemaps submitted for a site. Requires: site_url.
          - get_sitemap: Details for one sitemap. Requires: site_url, feedpath.
          - inspect_url: URL Inspection API — index+mobile+rich-result status.
              Requires: site_url, inspection_url. Optional: language_code.
        """
        u = _user()
        if not u or not getattr(u, "has_gsc", False):
            return _no_gsc()
        conn_id = _gsc_connection_id()
        if not conn_id:
            return _no_gsc()

        sc = state.search_console_connector

        if not action:
            return {
                "error": True,
                "error_type": "missing_required_param",
                "message": "action is required. Pass action='list_sites', 'search_analytics', 'list_sitemaps', 'get_sitemap', or 'inspect_url' in params.",
            }

        if action == "list_sites":
            return await sc.list_sites(conn_id)

        if action == "search_analytics":
            if not site_url or not start_date or not end_date:
                return {
                    "error": True,
                    "error_type": "bad_request",
                    "message": "search_analytics requires site_url, start_date, end_date.",
                }
            return await sc.search_analytics_query(
                connection_id=conn_id,
                site_url=site_url,
                start_date=start_date,
                end_date=end_date,
                dimensions=dimensions,
                search_type=search_type,
                row_limit=row_limit,
                start_row=start_row,
                dimension_filter_groups=dimension_filter_groups,
                aggregation_type=aggregation_type,
                data_state=data_state,
            )

        if action == "list_sitemaps":
            if not site_url:
                return {
                    "error": True,
                    "error_type": "bad_request",
                    "message": "list_sitemaps requires site_url.",
                }
            return await sc.list_sitemaps(conn_id, site_url)

        if action == "get_sitemap":
            if not site_url or not feedpath:
                return {
                    "error": True,
                    "error_type": "bad_request",
                    "message": "get_sitemap requires site_url and feedpath.",
                }
            return await sc.get_sitemap(conn_id, site_url, feedpath)

        if action == "inspect_url":
            if not site_url or not inspection_url:
                return {
                    "error": True,
                    "error_type": "bad_request",
                    "message": "inspect_url requires site_url and inspection_url.",
                }
            return await sc.inspect_url(conn_id, site_url, inspection_url, language_code)

        return {"error": True, "error_type": "bad_request", "message": f"Unknown action: {action}"}

    # ====================================================================
    # search_console_audit  — Layer 2
    # ====================================================================
    @mcp_server.tool("search_console_audit")
    async def search_console_audit(
        action: Literal[
            "top_movers",
            "striking_distance",
            "ctr_outliers",
            "sitemap_health",
            "gsc_ga4_cross_reference",
        ]
        | None = None,
        site_url: CoercedStr | None = None,
        start_date: CoercedStr | None = None,
        end_date: CoercedStr | None = None,
        compare_start_date: CoercedStr | None = None,
        compare_end_date: CoercedStr | None = None,
        dimension: Literal["query", "page"] | None = "query",
        min_impressions: int = 100,
        limit: int = 50,
        ga4_property_id: CoercedStr | None = None,
    ) -> dict:
        """
        Search Console intelligence audits.

        Actions:
          - top_movers: Biggest click/position gainers + losers vs a comparison window.
              Requires: site_url, start_date, end_date, compare_start_date, compare_end_date.
              Optional: dimension ('query'|'page'), limit.
          - striking_distance: Queries ranking in positions 8-20 (near page 1) with traction.
              Requires: site_url, start_date, end_date.
              Optional: min_impressions (default 100), limit.
          - ctr_outliers: Queries/pages whose CTR is materially below peer-median for their
              average position bucket — prime title/meta-description tuning targets.
              Requires: site_url, start_date, end_date.
              Optional: dimension, min_impressions, limit.
          - sitemap_health: Summary of sitemap status (errors/warnings/pending) for a site.
              Requires: site_url.
          - gsc_ga4_cross_reference: Joins top GSC landing pages with GA4 engagement/conversion
              metrics, highlighting entry pages that attract clicks but underperform post-click.
              Requires: site_url, start_date, end_date, ga4_property_id.
              Optional: limit.
        """
        u = _user()
        if not u or not getattr(u, "has_gsc", False):
            return _no_gsc()
        conn_id = _gsc_connection_id()
        if not conn_id:
            return _no_gsc()

        if not action:
            return {
                "error": True,
                "error_type": "missing_required_param",
                "message": "action is required. Pass action='top_movers', 'striking_distance', 'ctr_outliers', 'sitemap_health', or 'gsc_ga4_cross_reference' in params.",
            }
        if not site_url:
            return {
                "error": True,
                "error_type": "missing_required_param",
                "message": "site_url is required for search_console_audit.",
            }

        sc = state.search_console_connector

        # -------- top_movers ---------------------------------------------
        if action == "top_movers":
            if not (start_date and end_date and compare_start_date and compare_end_date):
                return {
                    "error": True,
                    "error_type": "bad_request",
                    "message": "top_movers requires start/end_date and compare_start/end_date.",
                }
            current = await sc.search_analytics_query(
                conn_id,
                site_url,
                start_date,
                end_date,
                dimensions=[dimension or "query"],
                row_limit=25000,
            )
            prior = await sc.search_analytics_query(
                conn_id,
                site_url,
                compare_start_date,
                compare_end_date,
                dimensions=[dimension or "query"],
                row_limit=25000,
            )
            dim_key = dimension or "query"
            prior_index = {r.get(dim_key): r for r in prior.get("rows", [])}
            movers = []
            for row in current.get("rows", []):
                key = row.get(dim_key)
                p = prior_index.get(key, {})
                delta_clicks = row.get("clicks", 0) - p.get("clicks", 0)
                delta_pos = (row.get("position", 0) or 0) - (p.get("position", 0) or 0)
                movers.append(
                    {
                        dim_key: key,
                        "clicks_current": row.get("clicks", 0),
                        "clicks_prior": p.get("clicks", 0),
                        "delta_clicks": delta_clicks,
                        "position_current": round(row.get("position", 0) or 0, 2),
                        "position_prior": round(p.get("position", 0) or 0, 2),
                        "delta_position": round(delta_pos, 2),
                        "impressions_current": row.get("impressions", 0),
                        "impressions_prior": p.get("impressions", 0),
                    }
                )
            gainers = sorted(movers, key=lambda x: x["delta_clicks"], reverse=True)[:limit]
            losers = sorted(movers, key=lambda x: x["delta_clicks"])[:limit]
            return {
                "site_url": site_url,
                "current_window": {"start": start_date, "end": end_date},
                "prior_window": {"start": compare_start_date, "end": compare_end_date},
                "dimension": dim_key,
                "top_gainers": gainers,
                "top_losers": losers,
            }

        # -------- striking_distance --------------------------------------
        if action == "striking_distance":
            if not (start_date and end_date):
                return {
                    "error": True,
                    "error_type": "bad_request",
                    "message": "striking_distance requires start_date and end_date.",
                }
            resp = await sc.search_analytics_query(
                conn_id,
                site_url,
                start_date,
                end_date,
                dimensions=["query"],
                row_limit=25000,
            )
            rows = resp.get("rows", [])
            striking = [
                r
                for r in rows
                if r.get("impressions", 0) >= min_impressions and 8.0 <= (r.get("position") or 99) <= 20.0
            ]
            striking.sort(key=lambda r: r.get("impressions", 0), reverse=True)
            return {
                "site_url": site_url,
                "start_date": start_date,
                "end_date": end_date,
                "definition": "Queries with avg position between 8 and 20 — close to page-1 with room to grow.",
                "count": len(striking),
                "queries": striking[:limit],
            }

        # -------- ctr_outliers -------------------------------------------
        if action == "ctr_outliers":
            if not (start_date and end_date):
                return {
                    "error": True,
                    "error_type": "bad_request",
                    "message": "ctr_outliers requires start_date and end_date.",
                }
            dim_key = dimension or "query"
            resp = await sc.search_analytics_query(
                conn_id,
                site_url,
                start_date,
                end_date,
                dimensions=[dim_key],
                row_limit=25000,
            )
            rows = [r for r in resp.get("rows", []) if r.get("impressions", 0) >= min_impressions]

            # Bucket by position band, compute median CTR per bucket
            def bucket(pos):
                pos = pos or 99
                if pos < 1.5:
                    return "1"
                if pos < 2.5:
                    return "2"
                if pos < 3.5:
                    return "3"
                if pos < 5.5:
                    return "4-5"
                if pos < 10.5:
                    return "6-10"
                if pos < 20.5:
                    return "11-20"
                return "21+"

            import statistics

            buckets: dict = {}
            for r in rows:
                buckets.setdefault(bucket(r.get("position")), []).append(r.get("ctr", 0.0))
            medians = {k: (statistics.median(v) if v else 0.0) for k, v in buckets.items()}

            outliers = []
            for r in rows:
                b = bucket(r.get("position"))
                med = medians.get(b, 0.0)
                ctr = r.get("ctr", 0.0)
                if med > 0 and ctr < med * 0.5:  # at least 50% below peer-median
                    outliers.append(
                        {
                            dim_key: r.get(dim_key),
                            "impressions": r.get("impressions"),
                            "clicks": r.get("clicks"),
                            "ctr": round(ctr, 4),
                            "position": round(r.get("position") or 0, 2),
                            "position_bucket": b,
                            "peer_median_ctr": round(med, 4),
                            "ctr_gap_vs_median": round(med - ctr, 4),
                        }
                    )
            outliers.sort(key=lambda x: x["impressions"] * (x["peer_median_ctr"] - x["ctr"]), reverse=True)
            return {
                "site_url": site_url,
                "start_date": start_date,
                "end_date": end_date,
                "dimension": dim_key,
                "peer_median_ctr_by_bucket": {k: round(v, 4) for k, v in medians.items()},
                "count": len(outliers),
                "outliers": outliers[:limit],
            }

        # -------- sitemap_health -----------------------------------------
        if action == "sitemap_health":
            data = await sc.list_sitemaps(conn_id, site_url)
            sitemaps = data.get("sitemaps", [])
            total = len(sitemaps)
            with_errors = [s for s in sitemaps if (s.get("errors") or 0) and int(s.get("errors") or 0) > 0]
            with_warnings = [
                s for s in sitemaps if (s.get("warnings") or 0) and int(s.get("warnings") or 0) > 0
            ]
            pending = [s for s in sitemaps if s.get("is_pending")]
            return {
                "site_url": site_url,
                "total_sitemaps": total,
                "pending_count": len(pending),
                "with_errors_count": len(with_errors),
                "with_warnings_count": len(with_warnings),
                "errors": with_errors,
                "warnings": with_warnings,
                "pending": pending,
                "all_sitemaps": sitemaps,
            }

        # -------- gsc_ga4_cross_reference --------------------------------
        if action == "gsc_ga4_cross_reference":
            if not (start_date and end_date and ga4_property_id):
                return {
                    "error": True,
                    "error_type": "bad_request",
                    "message": "gsc_ga4_cross_reference requires start_date, end_date, ga4_property_id.",
                }
            if not getattr(u, "has_ga4", False):
                return {
                    "error": True,
                    "error_type": "connection_missing",
                    "message": "GA4 connection is required for cross-referencing.",
                    "action_required": f"Visit {settings.APP_BASE_URL}/connect to enable GA4.",
                }

            # Pull GSC top landing pages
            gsc_resp = await sc.search_analytics_query(
                conn_id,
                site_url,
                start_date,
                end_date,
                dimensions=["page"],
                row_limit=max(limit * 3, 100),
            )
            gsc_pages = gsc_resp.get("rows", [])

            # Pull GA4 landing page metrics (Organic Search) for the same window
            ga4 = state.ga4_connector
            # Pick Google connection with analytics scope
            ga4_conn = None
            for c in u.connections or []:
                for s in getattr(c, "scopes", []) or []:
                    if "analytics" in s:
                        ga4_conn = c.id
                        break
                if ga4_conn:
                    break
            if not ga4_conn:
                ga4_conn = u.connections[0].id if u.connections else None
            try:
                ga4_rows = await ga4.run_report(
                    connection_id=ga4_conn,
                    property_id=ga4_property_id,
                    start_date=start_date,
                    end_date=end_date,
                    dimensions=["landingPage"],
                    metrics=[
                        "sessions",
                        "engagedSessions",
                        "averageSessionDuration",
                        "conversions",
                        "totalRevenue",
                    ],
                    dimension_filter={
                        "filter": {
                            "fieldName": "sessionDefaultChannelGroup",
                            "stringFilter": {"matchType": "EXACT", "value": "Organic Search"},
                        }
                    },
                    limit=10000,
                )
            except Exception as e:
                return {
                    "error": True,
                    "error_type": "ga4_error",
                    "message": f"GA4 fetch failed: {e}",
                }
            ga4_data = ga4_rows.get("rows", []) if isinstance(ga4_rows, dict) else []

            # Build a GA4 lookup keyed by landingPage path; allow suffix match
            # since GSC returns full URL and GA4 returns paths.
            def _path(u: str) -> str:
                try:
                    from urllib.parse import urlsplit

                    return urlsplit(u).path or u
                except Exception:
                    return u

            ga4_by_path: dict = {}
            for r in ga4_data:
                lp = r.get("landingPage") or r.get("dimensions", {}).get("landingPage")
                if isinstance(lp, str):
                    ga4_by_path[lp] = r

            joined = []
            for g in gsc_pages[: max(limit, 1)]:
                page = g.get("page") or ""
                path = _path(page)
                ga4r = ga4_by_path.get(path) or ga4_by_path.get(path.rstrip("/"))
                sessions = ga4r.get("sessions") if ga4r else None
                engaged = ga4r.get("engagedSessions") if ga4r else None
                conversions = ga4r.get("conversions") if ga4r else None
                revenue = ga4r.get("totalRevenue") if ga4r else None
                engagement_rate = None
                try:
                    if sessions and int(sessions) > 0:
                        engagement_rate = round(float(engaged or 0) / float(sessions), 4)
                except (ValueError, TypeError):
                    pass
                joined.append(
                    {
                        "page": page,
                        "path": path,
                        "gsc_clicks": g.get("clicks", 0),
                        "gsc_impressions": g.get("impressions", 0),
                        "gsc_ctr": round(g.get("ctr", 0) or 0, 4),
                        "gsc_position": round(g.get("position", 0) or 0, 2),
                        "ga4_sessions": sessions,
                        "ga4_engaged_sessions": engaged,
                        "ga4_engagement_rate": engagement_rate,
                        "ga4_conversions": conversions,
                        "ga4_revenue": revenue,
                        "ga4_match": ga4r is not None,
                    }
                )

            joined.sort(key=lambda x: x.get("gsc_clicks") or 0, reverse=True)
            return {
                "site_url": site_url,
                "ga4_property_id": ga4_property_id,
                "start_date": start_date,
                "end_date": end_date,
                "definition": "GSC clicks/position joined with GA4 organic-search landing page engagement and conversions.",
                "pages": joined,
                "matched_count": sum(1 for x in joined if x["ga4_match"]),
                "total_pages": len(joined),
            }

        return {"error": True, "error_type": "bad_request", "message": f"Unknown action: {action}"}

    # ====================================================================
    # search_console_write  — Layer 3 (scope-gated)
    # ====================================================================
    @mcp_server.tool("search_console_write")
    async def search_console_write(
        action: Literal["submit_sitemap", "delete_sitemap"],
        site_url: CoercedStr,
        feedpath: CoercedStr,
    ) -> dict:
        """
        Search Console write operations — requires the full 'webmasters' scope.

        Actions:
          - submit_sitemap: Submit a sitemap for a site. Requires: site_url, feedpath.
          - delete_sitemap: Remove a sitemap. Requires: site_url, feedpath.

        Re-connect Google at /connect with the 'full' tier to grant this scope.
        """
        u = _user()
        if not u or not getattr(u, "has_gsc", False):
            return _no_gsc()
        conn_id = _gsc_connection_id()
        if not conn_id:
            return _no_gsc()

        # Scope check — require full 'webmasters' (not just readonly)
        scopes = u.connections[0].scopes if u.connections else []
        if SCOPE_GSC_WRITE not in scopes:
            return {
                "error": True,
                "error_type": "insufficient_scope",
                "message": "Sitemap submit/delete requires the full Search Console (webmasters) scope.",
                "action_required": f"Reconnect Google at {settings.APP_BASE_URL}/connect with the 'full' tier (Search Console Write enabled).",
            }

        sc = state.search_console_connector
        if action == "submit_sitemap":
            return await sc.submit_sitemap(conn_id, site_url, feedpath)
        if action == "delete_sitemap":
            return await sc.delete_sitemap(conn_id, site_url, feedpath)
        return {"error": True, "error_type": "bad_request", "message": f"Unknown action: {action}"}
