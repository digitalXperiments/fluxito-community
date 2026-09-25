"""
Adjust Connector

Uses the Adjust Reports Service and App Automation / Campaign APIs.

Auth:
- Most endpoints: Authorization: Bearer <api_token>
- Campaign API (partner trackers): Authorization: Token token=<api_token>

Base URLs:
- Reports / App Automation: https://automate.adjust.com
- Campaign (trackers): https://api.adjust.com

Credentials stored as:
    api_key = Adjust API Token
    secret_key = unused (optional)

Documented endpoints:
    list_apps(api_key) -> {"apps": [...], "total": N}          # via filters_data
    get_report(api_key, dimensions, metrics, date_period, **filters)
    get_pivot_report(api_key, dimensions, metrics, date_period, index, **filters)
    list_events(api_key) -> {"events": [...], "total": N}
    list_app_automation_apps(api_key) -> {"apps": [...], "total": N}
    get_partner_links(api_key, app_token) -> {"trackers": [...], "total": N}

Removed (not real endpoints): list_sources, get_source, list_callbacks, audit_tracking_setup.
"""

import logging
from typing import Any

import httpx

from app.connectors.errors import friendly_errors

logger = logging.getLogger(__name__)

_ADJUST_BASE = "https://automate.adjust.com"
_ADJUST_CAMPAIGN_BASE = "https://api.adjust.com"


class AdjustConnector:
    """Interfaces with Adjust using per-user API token."""

    def _headers(self, api_key: str, auth_type: str = "bearer") -> dict[str, str]:
        """
        Build headers for Adjust API requests.

        auth_type:
            "bearer"  -> Authorization: Bearer <token>
            "token"   -> Authorization: Token token=<token>   (used for Campaign API /public/v2)
        """
        if auth_type == "token":
            return {
                "Authorization": f"Token token={api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        # default bearer
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _request(
        self,
        api_key: str,
        method: str,
        endpoint: str,
        *,
        base_url: str | None = None,
        auth_type: str = "bearer",
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict:
        """
        Make an authenticated request to Adjust API.
        Supports both Bearer and 'Token token=' auth styles.
        """
        try:
            headers = self._headers(api_key, auth_type=auth_type)
            effective_base = base_url or _ADJUST_BASE
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
                    return {"success": response.status_code < 300, "body": response.text}

        except Exception as e:
            logger.error(f"Adjust API request error: {e}")
            return {"error": True, "message": str(e)}

    # ------------------------------------------------------------------
    # Layer 1: Data Access (documented endpoints only)
    # ------------------------------------------------------------------

    @friendly_errors("Adjust")
    async def list_apps(self, api_key: str) -> dict:
        """
        GET /reports-service/filters_data?required_filters=apps
        Normalize as: {"apps": [{"id": ..., "name": ..., "short_name": ...}], "total": N}
        """
        params = {"required_filters": "apps"}
        result = await self._request(api_key, "GET", "/reports-service/filters_data", params=params)
        if isinstance(result, dict) and result.get("error"):
            return result

        # The response shape per docs is typically {"apps": [...] } or nested under data
        apps: list[dict] = []
        if isinstance(result, dict):
            apps = (
                result.get("apps") or result.get("data", {}).get("apps", [])
                if isinstance(result.get("data"), dict)
                else []
            )
            if not apps and "data" in result and isinstance(result["data"], list):
                # sometimes the list itself
                apps = result["data"]
        elif isinstance(result, list):
            apps = result

        if not isinstance(apps, list):
            apps = []

        normalized = []
        for a in apps:
            if not isinstance(a, dict):
                continue
            normalized.append(
                {
                    "id": a.get("id") or a.get("app_token") or a.get("token"),
                    "name": a.get("name") or a.get("app_name"),
                    "short_name": a.get("short_name") or a.get("shortname"),
                    "app_token": a.get("app_token") or a.get("token"),
                    "platform": a.get("platform"),
                    "bundle_id": a.get("bundle_id"),
                    "package_name": a.get("package_name"),
                }
            )

        return {"apps": normalized, "total": len(normalized)}

    @friendly_errors("Adjust")
    async def get_report(
        self,
        api_key: str,
        dimensions: str,
        metrics: str,
        date_period: str,
        **filters: Any,
    ) -> dict:
        """
        GET /reports-service/report
        Query params: dimensions, metrics, date_period, plus optional filter params
        Returns: {"rows": [...], "totals": {...}, "warnings": []}
        """
        params: dict[str, Any] = {
            "dimensions": dimensions,
            "metrics": metrics,
            "date_period": date_period,
        }
        # Merge any additional filters (e.g. app_token, tracker_token, etc.)
        for k, v in filters.items():
            if v is not None:
                params[k] = v

        result = await self._request(api_key, "GET", "/reports-service/report", params=params)
        if isinstance(result, dict) and result.get("error"):
            return result

        # Response is expected to already be the report object with rows/totals/warnings
        rows = result.get("rows", []) if isinstance(result, dict) else []
        totals = result.get("totals", {}) if isinstance(result, dict) else {}
        warnings = result.get("warnings", []) if isinstance(result, dict) else []

        return {
            "rows": rows if isinstance(rows, list) else [],
            "totals": totals if isinstance(totals, dict) else {},
            "warnings": warnings if isinstance(warnings, list) else [],
            "raw": result,
        }

    @friendly_errors("Adjust")
    async def get_pivot_report(
        self,
        api_key: str,
        dimensions: str,
        metrics: str,
        date_period: str,
        index: str,
        **filters: Any,
    ) -> dict:
        """
        GET /reports-service/pivot_report
        Query params: dimensions, metrics, date_period, index, plus optional filter params
        Returns: {"rows": [...], "totals": {...}, "warnings": []}
        """
        params: dict[str, Any] = {
            "dimensions": dimensions,
            "metrics": metrics,
            "date_period": date_period,
            "index": index,
        }
        for k, v in filters.items():
            if v is not None:
                params[k] = v

        result = await self._request(api_key, "GET", "/reports-service/pivot_report", params=params)
        if isinstance(result, dict) and result.get("error"):
            return result

        rows = result.get("rows", []) if isinstance(result, dict) else []
        totals = result.get("totals", {}) if isinstance(result, dict) else {}
        warnings = result.get("warnings", []) if isinstance(result, dict) else []

        return {
            "rows": rows if isinstance(rows, list) else [],
            "totals": totals if isinstance(totals, dict) else {},
            "warnings": warnings if isinstance(warnings, list) else [],
            "raw": result,
        }

    @friendly_errors("Adjust")
    async def list_events(self, api_key: str) -> dict:
        """
        GET /reports-service/events
        Normalize as: {"events": [...], "total": N}
        """
        result = await self._request(api_key, "GET", "/reports-service/events")
        if isinstance(result, dict) and result.get("error"):
            return result

        events = (
            result
            if isinstance(result, list)
            else result.get("events", [])
            if isinstance(result, dict)
            else []
        )
        if not isinstance(events, list):
            events = []

        return {
            "events": events,
            "total": len(events),
        }

    @friendly_errors("Adjust")
    async def list_app_automation_apps(self, api_key: str) -> dict:
        """
        GET /app-automation/apps/list
        NOTE: Only returns apps created/updated via THIS API, not all dashboard apps.
        Normalize as: {"apps": [...], "total": N}
        """
        result = await self._request(api_key, "GET", "/app-automation/apps/list")
        if isinstance(result, dict) and result.get("error"):
            return result

        apps = (
            result if isinstance(result, list) else result.get("apps", []) if isinstance(result, dict) else []
        )
        if not isinstance(apps, list):
            apps = []

        return {
            "apps": apps,
            "total": len(apps),
        }

    @friendly_errors("Adjust")
    async def get_partner_links(self, api_key: str, app_token: str) -> dict:
        """
        GET /public/v2/apps/{app_token}/trackers
        Auth: Authorization: Token token={api_key} (NOT Bearer!)
        Base: https://api.adjust.com
        Normalize as: {"trackers": [...], "total": N}
        """
        endpoint = f"/public/v2/apps/{app_token}/trackers"
        result = await self._request(
            api_key,
            "GET",
            endpoint,
            base_url=_ADJUST_CAMPAIGN_BASE,
            auth_type="token",
        )
        if isinstance(result, dict) and result.get("error"):
            return result

        trackers = (
            result
            if isinstance(result, list)
            else result.get("trackers", [])
            if isinstance(result, dict)
            else []
        )
        if not isinstance(trackers, list):
            trackers = []

        return {
            "trackers": trackers,
            "total": len(trackers),
        }
