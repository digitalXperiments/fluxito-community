"""
AppsFlyer Connector

Uses the AppsFlyer HQ API (raw-data export) and management endpoints.

Auth: Authorization: Bearer <V2.0 API token>
Base URL: https://hq1.appsflyer.com/api

Documented endpoints only:
    list_apps(api_key) -> {"apps": [...], "total": N}
    get_installs_report(api_key, app_id, start_date, end_date) -> {"app_id": ..., "installs": [...], "total": N}
    get_in_app_events_report(api_key, app_id, start_date, end_date) -> {"app_id": ..., "events": [...], "total": N}
    get_partners_report(api_key, app_id, start_date, end_date) -> {"app_id": ..., "partners": [...], "total": N}
        NOTE: partners_report uses DIFFERENT host: https://hq.appsflyer.com/export/...

Removed (not real endpoints): get_app_overview, audit_tracking_setup, get_pull_api_aurora_token.
"""

import csv
import io
import logging
from typing import Any

import httpx

from app.connectors.errors import friendly_errors

logger = logging.getLogger(__name__)


def _parse_csv(text: str) -> list[dict]:
    """Parse CSV text into list of dicts with normalized column names.

    Converts column names to lowercase, strips whitespace, and replaces
    spaces/hyphens with underscores so they match the normalizer's expected keys.
    """
    reader = csv.DictReader(io.StringIO(text))
    return [
        {k.strip().lower().replace(" ", "_").replace("-", "_"): v for k, v in row.items()} for row in reader
    ]


_APPSFLYER_BASE = "https://hq1.appsflyer.com/api"
# Partners report uses a different host per official docs
_APPSFLYER_PARTNERS_BASE = "https://hq.appsflyer.com"


class AppsFlyerConnector:
    """Interfaces with AppsFlyer using V2.0 API Bearer token."""

    def _headers(self, api_key: str) -> dict[str, str]:
        """Build headers for AppsFlyer API requests."""
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "accept": "application/json",
        }

    async def _request(
        self,
        api_key: str,
        method: str,
        endpoint: str,
        *,
        base_url: str | None = None,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict:
        """
        Make an authenticated request to AppsFlyer API.
        Uses Bearer token auth.

        base_url: optional override (used for partners report which lives on a different host)
        """
        try:
            headers = self._headers(api_key)
            effective_base = base_url or _APPSFLYER_BASE
            url = f"{effective_base}{endpoint}"
            async with httpx.AsyncClient(timeout=30.0) as client:
                if method == "GET":
                    response = await client.get(url, headers=headers, params=params)
                elif method == "POST":
                    response = await client.post(url, headers=headers, params=params, json=json_body)
                elif method == "PUT":
                    response = await client.put(url, headers=headers, params=params, json=json_body)
                elif method == "DELETE":
                    response = await client.delete(url, headers=headers, params=params)
                else:
                    return {"error": True, "message": f"Unsupported HTTP method: {method}"}

                if response.status_code >= 400:
                    return {
                        "error": True,
                        "status_code": response.status_code,
                        "message": response.text,
                    }

                try:
                    return response.json()  # type: ignore[no-any-return]
                except Exception:
                    # AppsFlyer raw-data endpoints return CSV
                    if response.status_code < 300 and response.text.strip():
                        try:
                            rows = _parse_csv(response.text)
                            if rows:
                                return {"results": rows}
                        except Exception:
                            pass
                    return {"success": response.status_code < 300, "body": response.text}

        except Exception as e:
            logger.error(f"AppsFlyer API request error: {e}")
            return {"error": True, "message": str(e)}

    # ------------------------------------------------------------------
    # Layer 1: Data Access (documented endpoints only)
    # ------------------------------------------------------------------

    @friendly_errors("AppsFlyer")
    async def list_apps(self, api_key: str) -> dict:
        """
        GET /mng/apps
        Returns list of apps.
        Normalize as: {"apps": [...], "total": N}
        """
        result = await self._request(api_key, "GET", "/mng/apps")
        if isinstance(result, dict) and result.get("error"):
            return result

        apps = (
            result if isinstance(result, list) else result.get("apps", []) if isinstance(result, dict) else []
        )
        if not isinstance(apps, list):
            apps = []

        return {
            "apps": [
                {
                    "app_id": a.get("app_id") or a.get("id") or a.get("appId"),
                    "app_name": a.get("app_name") or a.get("name") or a.get("appName"),
                    "platform": a.get("platform"),
                    "bundle_id": a.get("bundle_id") or a.get("bundleId"),
                    "package_name": a.get("package_name") or a.get("packageName"),
                }
                for a in apps
                if isinstance(a, dict)
            ],
            "total": len(apps),
        }

    @friendly_errors("AppsFlyer")
    async def get_installs_report(
        self,
        api_key: str,
        app_id: str,
        start_date: str,
        end_date: str,
    ) -> dict:
        """
        GET /raw-data/export/app/{app_id}/installs_report/v5?from={start_date}&to={end_date}
        Normalize as: {"app_id": ..., "installs": [...], "total": N}
        """
        params = {"from": start_date, "to": end_date}
        endpoint = f"/raw-data/export/app/{app_id}/installs_report/v5"
        result = await self._request(api_key, "GET", endpoint, params=params)
        if isinstance(result, dict) and result.get("error"):
            return result

        installs = (
            result
            if isinstance(result, list)
            else result.get("results", [])
            if isinstance(result, dict)
            else []
        )
        if not isinstance(installs, list):
            installs = []

        return {
            "app_id": app_id,
            "start_date": start_date,
            "end_date": end_date,
            "installs": [
                {
                    "date": i.get("date"),
                    "installs": i.get("installs"),
                    "media_source": i.get("media_source"),
                    "campaign": i.get("campaign"),
                }
                for i in installs
                if isinstance(i, dict)
            ],
            "total": len(installs),
        }

    @friendly_errors("AppsFlyer")
    async def get_in_app_events_report(
        self,
        api_key: str,
        app_id: str,
        start_date: str,
        end_date: str,
    ) -> dict:
        """
        GET /raw-data/export/app/{app_id}/in_app_events_report/v5?from={start_date}&to={end_date}
        Note: endpoint path has underscore: in_app_events_report (not in-app-events-report)
        Normalize as: {"app_id": ..., "events": [...], "total": N}
        """
        params = {"from": start_date, "to": end_date}
        endpoint = f"/raw-data/export/app/{app_id}/in_app_events_report/v5"
        result = await self._request(api_key, "GET", endpoint, params=params)
        if isinstance(result, dict) and result.get("error"):
            return result

        events = (
            result
            if isinstance(result, list)
            else result.get("results", [])
            if isinstance(result, dict)
            else []
        )
        if not isinstance(events, list):
            events = []

        return {
            "app_id": app_id,
            "start_date": start_date,
            "end_date": end_date,
            "events": [
                {
                    "date": e.get("date"),
                    "event_name": e.get("event_name"),
                    "event_count": e.get("event_count"),
                    "media_source": e.get("media_source"),
                    "campaign": e.get("campaign"),
                }
                for e in events
                if isinstance(e, dict)
            ],
            "total": len(events),
        }

    @friendly_errors("AppsFlyer")
    async def get_partners_report(
        self,
        api_key: str,
        app_id: str,
        start_date: str,
        end_date: str,
    ) -> dict:
        """
        GET https://hq.appsflyer.com/export/{app_id}/partners_report/v5?from={start_date}&to={end_date}
        NOTE: Different host! hq.appsflyer.com (not hq1.appsflyer.com/api)
        Auth is the same Bearer token.
        Normalize as: {"app_id": ..., "partners": [...], "total": N}
        """
        params = {"from": start_date, "to": end_date}
        # Use full path on the different host; _request will prepend the base we pass
        endpoint = f"/export/{app_id}/partners_report/v5"
        result = await self._request(
            api_key, "GET", endpoint, base_url=_APPSFLYER_PARTNERS_BASE, params=params
        )
        if isinstance(result, dict) and result.get("error"):
            return result

        partners = (
            result
            if isinstance(result, list)
            else result.get("results", [])
            if isinstance(result, dict)
            else []
        )
        if not isinstance(partners, list):
            partners = []

        return {
            "app_id": app_id,
            "start_date": start_date,
            "end_date": end_date,
            "partners": [
                {
                    "media_source": p.get("media_source"),
                    "installs": p.get("installs"),
                    "clicks": p.get("clicks"),
                    "impressions": p.get("impressions"),
                    "cost": p.get("cost"),
                }
                for p in partners
                if isinstance(p, dict)
            ],
            "total": len(partners),
        }
