"""
Google Search Console Connector

Wraps the Search Console API v1 via google-api-python-client.

Layer 1 (read):
  - list_sites                   — enumerate verified properties
  - search_analytics_query       — query performance (queries / pages / countries / devices)
  - list_sitemaps                — sitemaps submitted for a site
  - get_sitemap                  — details for a single sitemap
  - inspect_url                  — URL Inspection API (indexing + mobile + rich results)

Layer 2 (intelligence — implemented on top of read methods in tools):
  - top_losing_queries / top_gaining_queries / ctr_outliers etc.

Layer 3 (write, requires the full 'webmasters' scope):
  - submit_sitemap
  - delete_sitemap

All .execute() calls run in a thread pool via run_sync() because
googleapiclient uses synchronous HTTP.
"""

import logging

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from app.connectors.base import BaseConnector
from app.connectors.errors import friendly_errors

logger = logging.getLogger(__name__)


class SearchConsoleConnector(BaseConnector):
    # Web Search type is the dominant use case; Image / Video / News /
    # Discover / GoogleNews are supported if the caller passes them explicitly.
    DEFAULT_SEARCH_TYPE = "web"

    def _build_service(self, access_token: str):
        creds = Credentials(token=access_token)
        return build("searchconsole", "v1", credentials=creds, cache_discovery=False)

    async def _exec(self, request):
        return await self.run_sync(request.execute)

    # ------------------------------------------------------------------
    # Discovery helper (used during OAuth callback)
    # ------------------------------------------------------------------

    @friendly_errors("Search Console")
    async def list_all_sites_raw(self, access_token: str) -> list:
        """Returns list of site entries — used during data OAuth callback.

        Each entry has ``siteUrl`` and ``permissionLevel``. Sites the user
        can only see but not act on (siteUnverifiedUser) are filtered out
        so discovery doesn't create orphan rows.
        """
        service = self._build_service(access_token)
        sites: list = []
        try:
            resp = await self._exec(service.sites().list())
            for s in resp.get("siteEntry", []):
                perm = s.get("permissionLevel")
                if perm == "siteUnverifiedUser":
                    continue
                sites.append(s)
            logger.info(f"Discovered {len(sites)} Search Console sites")
        except Exception as e:
            logger.error(f"Error discovering Search Console sites: {e}")
        return sites

    # ------------------------------------------------------------------
    # Layer 1 — Data access
    # ------------------------------------------------------------------

    @friendly_errors("Search Console")
    async def list_sites(self, connection_id: str) -> dict:
        token = await self.get_token(connection_id)
        sites = await self.list_all_sites_raw(token)
        return {
            "sites": [
                {
                    "site_url": s.get("siteUrl"),
                    "permission_level": s.get("permissionLevel"),
                    "is_domain_property": str(s.get("siteUrl", "")).startswith("sc-domain:"),
                }
                for s in sites
            ]
        }

    @friendly_errors("Search Console")
    async def search_analytics_query(
        self,
        connection_id: str,
        site_url: str,
        start_date: str,
        end_date: str,
        dimensions: list | None = None,
        search_type: str | None = None,
        row_limit: int = 1000,
        start_row: int = 0,
        dimension_filter_groups: list | None = None,
        aggregation_type: str | None = None,
        data_state: str | None = None,
    ) -> dict:
        """Run a Search Analytics query.

        dimensions: any subset of ["query", "page", "country", "device", "date", "searchAppearance"].
        search_type: one of "web" | "image" | "video" | "news" | "discover" | "googleNews".
        data_state: "final" (default) or "all" (includes last-24h fresh data).
        """
        token = await self.get_token(connection_id)
        service = self._build_service(token)

        body: dict = {
            "startDate": start_date,
            "endDate": end_date,
            "dimensions": dimensions or ["query"],
            "rowLimit": max(1, min(int(row_limit), 25000)),
            "startRow": max(0, int(start_row)),
        }
        if search_type:
            body["type"] = search_type
        if dimension_filter_groups:
            body["dimensionFilterGroups"] = dimension_filter_groups
        if aggregation_type:
            body["aggregationType"] = aggregation_type
        if data_state:
            body["dataState"] = data_state

        resp = await self._exec(service.searchanalytics().query(siteUrl=site_url, body=body))

        rows_out = []
        for r in resp.get("rows", []):
            row = {
                "clicks": r.get("clicks", 0),
                "impressions": r.get("impressions", 0),
                "ctr": r.get("ctr", 0.0),
                "position": r.get("position", 0.0),
            }
            # Flatten dimension keys alongside the metrics
            keys = r.get("keys", []) or []
            for dim, val in zip(body["dimensions"], keys, strict=False):
                row[dim] = val
            rows_out.append(row)

        return {
            "site_url": site_url,
            "start_date": start_date,
            "end_date": end_date,
            "dimensions": body["dimensions"],
            "search_type": body.get("type", self.DEFAULT_SEARCH_TYPE),
            "row_count": len(rows_out),
            "rows": rows_out,
            "response_aggregation_type": resp.get("responseAggregationType"),
        }

    @friendly_errors("Search Console")
    async def list_sitemaps(self, connection_id: str, site_url: str) -> dict:
        token = await self.get_token(connection_id)
        service = self._build_service(token)
        resp = await self._exec(service.sitemaps().list(siteUrl=site_url))
        return {
            "site_url": site_url,
            "sitemaps": [
                {
                    "path": s.get("path"),
                    "last_submitted": s.get("lastSubmitted"),
                    "last_downloaded": s.get("lastDownloaded"),
                    "is_pending": s.get("isPending"),
                    "is_sitemaps_index": s.get("isSitemapsIndex"),
                    "type": s.get("type"),
                    "warnings": s.get("warnings"),
                    "errors": s.get("errors"),
                    "contents": s.get("contents", []),
                }
                for s in resp.get("sitemap", [])
            ],
        }

    @friendly_errors("Search Console")
    async def get_sitemap(self, connection_id: str, site_url: str, feedpath: str) -> dict:
        token = await self.get_token(connection_id)
        service = self._build_service(token)
        return await self._exec(service.sitemaps().get(siteUrl=site_url, feedpath=feedpath))

    @friendly_errors("Search Console")
    async def inspect_url(
        self,
        connection_id: str,
        site_url: str,
        inspection_url: str,
        language_code: str | None = None,
    ) -> dict:
        """URL Inspection API — index status, mobile usability, rich-results for a page."""
        token = await self.get_token(connection_id)
        service = self._build_service(token)
        body = {"siteUrl": site_url, "inspectionUrl": inspection_url}
        if language_code:
            body["languageCode"] = language_code
        resp = await self._exec(service.urlInspection().index().inspect(body=body))
        result = resp.get("inspectionResult", {}) or {}
        index_status = result.get("indexStatusResult", {}) or {}
        mobile = result.get("mobileUsabilityResult", {}) or {}
        rich = result.get("richResultsResult", {}) or {}
        return {
            "site_url": site_url,
            "inspection_url": inspection_url,
            "verdict": index_status.get("verdict"),
            "coverage_state": index_status.get("coverageState"),
            "robots_txt_state": index_status.get("robotsTxtState"),
            "indexing_state": index_status.get("indexingState"),
            "last_crawl_time": index_status.get("lastCrawlTime"),
            "page_fetch_state": index_status.get("pageFetchState"),
            "google_canonical": index_status.get("googleCanonical"),
            "user_canonical": index_status.get("userCanonical"),
            "mobile_verdict": mobile.get("verdict"),
            "mobile_issues": mobile.get("issues", []),
            "rich_results_verdict": rich.get("verdict"),
            "rich_results_items": rich.get("detectedItems", []),
            "inspection_result_link": result.get("inspectionResultLink"),
        }

    # ------------------------------------------------------------------
    # Layer 3 — Write operations
    # ------------------------------------------------------------------

    @friendly_errors("Search Console")
    async def submit_sitemap(self, connection_id: str, site_url: str, feedpath: str) -> dict:
        """Submit a sitemap for a site. Requires the webmasters (write) scope."""
        token = await self.get_token(connection_id)
        service = self._build_service(token)
        await self._exec(service.sitemaps().submit(siteUrl=site_url, feedpath=feedpath))
        return {"submitted": True, "site_url": site_url, "feedpath": feedpath}

    @friendly_errors("Search Console")
    async def delete_sitemap(self, connection_id: str, site_url: str, feedpath: str) -> dict:
        """Remove a sitemap. Requires the webmasters (write) scope."""
        token = await self.get_token(connection_id)
        service = self._build_service(token)
        await self._exec(service.sitemaps().delete(siteUrl=site_url, feedpath=feedpath))
        return {"deleted": True, "site_url": site_url, "feedpath": feedpath}
