"""
Bing Webmaster Tools MCP Tool

Provides read access to Bing Webmaster Tools data via the Microsoft OAuth2
Bearer token stored as provider "bing" in OAuthConnection rows.

Five actions mirror the equivalent Google Search Console actions:
  - list_sites
  - get_query_stats   (analogous to search_analytics)
  - get_crawl_stats
  - get_index_coverage
  - get_link_counts
"""

from typing import Annotated, Literal

from pydantic import BeforeValidator

import app.app_state as state
from app.config import settings
from app.tools.shared_helpers import get_provider_token

CoercedStr = Annotated[str, BeforeValidator(str)]


def _user():
    return state.current_user_ctx.get()


def _no_bing() -> dict:
    base = settings.APP_BASE_URL
    return {
        "error": True,
        "error_type": "connection_missing",
        "message": "No Bing Webmaster Tools connection found. Connect your Microsoft account to get Bing search data.",
        "connect_url": f"{base}/connect/bing",
        "action_required": f"Visit {base}/connect/bing to connect Bing Webmaster Tools.",
    }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_bing_webmaster_tools(mcp_server):
    # ====================================================================
    # bing_webmaster_read  — Layer 1 (data access)
    # ====================================================================
    @mcp_server.tool("bing_webmaster_read")
    async def bing_webmaster_read(
        action: Literal[
            "list_sites",
            "get_query_stats",
            "get_crawl_stats",
            "get_index_coverage",
            "get_link_counts",
        ]
        | None = None,
        site_url: CoercedStr | None = None,
        start_date: CoercedStr | None = None,
        end_date: CoercedStr | None = None,
        search_type: CoercedStr | None = None,
        page: int = 0,
        page_size: int = 100,
    ) -> dict:
        """
        Bing Webmaster Tools — read-only data access.

        Actions:
          - list_sites: Enumerate verified Bing Webmaster sites.
          - get_query_stats: Keyword/query performance data.
              Requires: site_url.
              Optional: start_date (YYYY-MM-DD), end_date (YYYY-MM-DD),
                        search_type, page, page_size.
          - get_crawl_stats: Crawl statistics for a site.
              Requires: site_url.
          - get_index_coverage: Index coverage data for a site.
              Requires: site_url.
          - get_link_counts: Inbound link counts for a site.
              Requires: site_url.
        """
        u = _user()
        if not u or not getattr(u, "has_bing", False):
            return _no_bing()

        if not action:
            return {
                "error": True,
                "error_type": "missing_required_param",
                "message": "action is required. Pass action='list_sites', 'get_query_stats', 'get_crawl_stats', 'get_index_coverage', or 'get_link_counts' in params.",
            }

        access_token = get_provider_token("bing")
        if not access_token:
            return _no_bing()

        connector = state.bing_connector

        if action == "list_sites":
            sites = await connector.list_sites(access_token)
            return {"sites": sites}

        # All remaining actions require site_url
        if not site_url:
            return {
                "error": True,
                "error_type": "bad_request",
                "message": f"{action} requires site_url.",
            }

        if action == "get_query_stats":
            return await connector.get_query_stats(
                access_token=access_token,
                site_url=site_url,
                date_from=start_date,
                date_to=end_date,
                search_type=search_type,
                page=page,
                page_size=page_size,
            )

        if action == "get_crawl_stats":
            return await connector.get_crawl_stats(access_token=access_token, site_url=site_url)

        if action == "get_index_coverage":
            return await connector.get_index_coverage(access_token=access_token, site_url=site_url)

        if action == "get_link_counts":
            return await connector.get_link_counts(access_token=access_token, site_url=site_url)

        return {"error": True, "error_type": "bad_request", "message": f"Unknown action: {action}"}
