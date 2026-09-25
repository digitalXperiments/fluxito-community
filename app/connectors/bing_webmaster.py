"""
Bing Webmaster Tools Connector.

Wraps the Bing Webmaster Tools JSON API using Microsoft OAuth 2.0 access tokens.

API base: https://ssl.bing.com/webmaster/api.svc/json/
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.connectors.errors import friendly_errors

logger = logging.getLogger(__name__)

_BING_BASE = "https://ssl.bing.com/webmaster/api.svc/json"


class BingWebmasterConnector:
    """Client for Bing Webmaster Tools API using Microsoft Bearer tokens."""

    def __init__(self):
        pass

    async def _get(
        self,
        path: str,
        access_token: str,
        params: dict[str, Any] | None = None,
        timeout: int = 30,
    ) -> dict[str, Any]:
        url = f"{_BING_BASE}/{path}"
        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, headers=headers, params=params or {})
            resp.raise_for_status()
            return resp.json()

    @friendly_errors("Bing Webmaster Tools")
    async def list_sites(self, access_token: str) -> list[dict[str, Any]]:
        data = await self._get("GetUserSites", access_token)
        sites = data.get("d") or data.get("sites") or []
        return sites if isinstance(sites, list) else []

    @friendly_errors("Bing Webmaster Tools")
    async def get_crawl_stats(self, access_token: str, site_url: str) -> dict[str, Any]:
        return await self._get("GetCrawlStats", access_token, params={"siteUrl": site_url})

    @friendly_errors("Bing Webmaster Tools")
    async def get_query_stats(
        self,
        access_token: str,
        site_url: str,
        date_from: str | None = None,
        date_to: str | None = None,
        search_type: str | None = None,
        page: int = 0,
        page_size: int = 100,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"siteUrl": site_url, "page": page, "pageSize": page_size}
        if date_from:
            params["dateFrom"] = date_from
        if date_to:
            params["dateTo"] = date_to
        if search_type:
            params["searchType"] = search_type
        return await self._get("GetQueryStats", access_token, params=params)

    @friendly_errors("Bing Webmaster Tools")
    async def get_index_coverage(self, access_token: str, site_url: str) -> dict[str, Any]:
        return await self._get("GetIndexCoverage", access_token, params={"siteUrl": site_url})

    @friendly_errors("Bing Webmaster Tools")
    async def get_link_counts(self, access_token: str, site_url: str) -> dict[str, Any]:
        return await self._get("GetLinkCounts", access_token, params={"siteUrl": site_url})
