"""
Branch Connector

Uses the Branch Dashboard REST API (api2.branch.io).

Auth:
- v1 APIs (e.g. GET /v1/app/{branch_key}): branch_key in path, branch_secret as query param
- v3 APIs (POST /v3/export): branch_key + branch_secret in JSON body

Credentials stored as:
    api_key = branch_key
    secret_key = branch_secret

Documented endpoints only:
    get_app(api_key, secret_key) -> {"app": {...}}
    request_daily_export(api_key, secret_key, export_date) -> {"export": {...}} or paths

Removed (not real endpoints): list_apps, list_exports, get_export, request_export,
audit_tracking_setup, list_webhooks.
"""

import logging
from typing import Any

import httpx

from app.connectors.errors import friendly_errors

logger = logging.getLogger(__name__)

_BRANCH_BASE = "https://api2.branch.io"


class BranchConnector:
    """Interfaces with Branch using branch_key + branch_secret."""

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict:
        """
        Low-level HTTP request. Returns raw JSON or error dict.
        Does NOT apply @friendly_errors — callers wrap.
        """
        try:
            hdrs = {"Content-Type": "application/json"}
            if headers:
                hdrs.update(headers)

            async with httpx.AsyncClient(timeout=30.0) as client:
                if method == "GET":
                    resp = await client.get(url, headers=hdrs, params=params)
                elif method == "POST":
                    resp = await client.post(url, headers=hdrs, params=params, json=json_body)
                elif method == "PUT":
                    resp = await client.put(url, headers=hdrs, params=params, json=json_body)
                elif method == "DELETE":
                    resp = await client.delete(url, headers=hdrs, params=params)
                else:
                    return {"error": True, "message": f"Unsupported HTTP method: {method}"}

                if resp.status_code >= 400:
                    return {
                        "error": True,
                        "status_code": resp.status_code,
                        "message": resp.text,
                    }

                try:
                    return resp.json()  # type: ignore[no-any-return]
                except Exception:
                    return {"success": resp.status_code < 300, "body": resp.text}

        except Exception as e:
            logger.error(f"Branch API request error: {e}")
            return {"error": True, "message": str(e)}

    # ------------------------------------------------------------------
    # Layer 1: Data Access (documented endpoints only)
    # ------------------------------------------------------------------

    @friendly_errors("Branch")
    async def get_app(self, api_key: str, secret_key: str) -> dict:
        """
        GET /v1/app/{branch_key}?branch_secret={secret}
        Returns single app config.
        Normalize as: {"app": {...}}
        """
        branch_key = api_key
        url = f"{_BRANCH_BASE}/v1/app/{branch_key}"
        params = {"branch_secret": secret_key}

        result = await self._request("GET", url, params=params)
        if isinstance(result, dict) and result.get("error"):
            return result

        # Branch returns the app object directly or under 'data'
        app_obj = (
            result
            if isinstance(result, dict) and ("app_id" in result or "id" in result)
            else result.get("data", result)
        )
        if not isinstance(app_obj, dict):
            app_obj = {}

        return {
            "app": {
                "app_id": app_obj.get("app_id") or app_obj.get("id") or branch_key,
                "app_name": app_obj.get("app_name") or app_obj.get("name"),
                "platform": app_obj.get("platform"),
                "bundle_id": app_obj.get("bundle_id"),
                "package_name": app_obj.get("package_name"),
                "created_at": app_obj.get("created_at"),
            }
        }

    @friendly_errors("Branch")
    async def request_daily_export(self, api_key: str, secret_key: str, export_date: str) -> dict:
        """
        POST /v3/export
        JSON body: {"branch_key": ..., "branch_secret": ..., "export_date": ...}
        Returns dict with export S3 file paths keyed by event type.

        NOTE: This requests an async export job. There is no companion
        fetch/poll action — the response contains S3 paths to the export
        files when the job completes. If the export is not yet ready the
        response may indicate a pending job.
        """
        url = f"{_BRANCH_BASE}/v3/export"
        body = {
            "branch_key": api_key,
            "branch_secret": secret_key,
            "export_date": export_date,
        }

        result = await self._request("POST", url, json_body=body)
        if isinstance(result, dict) and result.get("error"):
            return result

        # Response typically contains links like {"open": "...s3...", "install": "...", ...}
        # Normalize to a consistent shape for callers
        return {
            "success": True,
            "export_date": export_date,
            "files": result if isinstance(result, dict) else {},
            "raw": result,
        }

    @friendly_errors("Branch")
    async def query_analytics(
        self,
        api_key: str,
        secret_key: str,
        start_date: str,
        end_date: str,
        data_source: str = "eo_click",
        dimensions: list[str] | None = None,
        aggregation: str = "total_count",
    ) -> dict:
        """
        POST /v1/query/analytics
        Query pre-aggregated analytics data (installs, clicks, opens, conversions).
        """
        url = "https://api.branch.io/v1/query/analytics"
        body: dict[str, Any] = {
            "branch_key": api_key,
            "branch_secret": secret_key,
            "start_date": start_date,
            "end_date": end_date,
            "data_source": data_source,
            "aggregation": aggregation,
        }
        if dimensions:
            body["dimensions"] = dimensions

        result = await self._request("POST", url, json_body=body)
        if isinstance(result, dict) and result.get("error"):
            return result

        return {
            "success": True,
            "start_date": start_date,
            "end_date": end_date,
            "data_source": data_source,
            "results": result if isinstance(result, list) else result.get("results", result),
        }
