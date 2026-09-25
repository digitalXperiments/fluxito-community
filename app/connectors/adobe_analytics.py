"""
Adobe Analytics Connector

Uses the Adobe Analytics 2.0 API via Adobe IMS OAuth (client credentials grant).

Auth: Adobe IMS OAuth — client_id + client_secret + org_id.
Obtains an access token via:
POST https://ims-na1.adobelogin.com/ims/token/v3

Every Analytics 2.0 call is routed as:
  https://analytics.adobe.io/api/{GLOBAL_COMPANY_ID}/...
with header x-proxy-global-company-id: {GLOBAL_COMPANY_ID}

GLOBAL_COMPANY_ID is NOT the IMS org id (xxx@AdobeOrg). It is resolved from:
  1. an explicit company_id argument / stored AdobeConnection.company_id
  2. GET https://analytics.adobe.io/discovery/me
  3. treating a non-IMS org_id as an already-resolved company id (back-compat)

Layer 1 (Read): list_report_suites, get_dimensions, get_metrics, run_report,
                get_segments, get_calculated_metrics
Layer 2 (Audit): audit_report_suite, check_data_quality
Layer 3 (Write): create_segment, update_segment, delete_segment,
                 create_calculated_metric, delete_calculated_metric
Layer 4 (Workspace): list/get/create/update/delete/copy + validate +
                     build_project_definition
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any
from urllib.parse import quote

import httpx

from app.connectors.errors import friendly_errors

logger = logging.getLogger(__name__)

_IMS_BASE = "https://ims-na1.adobelogin.com"
_ANALYTICS_BASE = "https://analytics.adobe.io"

# Analysis Workspace definition version from Adobe's published example.
_WORKSPACE_DEFINITION_VERSION = "31"

_DEFAULT_LIST_EXPANSION = "reportSuiteName,ownerFullName"

# Adobe Workspace project ids in public examples are hex-like tokens
# (e.g. "6091a10005c7706c0acdd751"). Conservative charset + length bound
# rejects wildcards, path traversal, and URL injection before interpolation.
_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# Writable top-level fields accepted on POST /projects (create/copy).
_PROJECT_CREATE_OPTIONAL_FIELDS = frozenset({"tags", "shares"})

# Extra keys allowed on a partial PUT besides the first-class kwargs.
_PROJECT_UPDATE_OPTIONAL_FIELDS = frozenset({"tags", "shares"})

_IMS_ORG_RE = re.compile(r"@adobeorg$", re.IGNORECASE)

_DATE_PRESETS = {
    "thismonth": "thisMonth",
    "this_month": "thisMonth",
    "lastmonth": "lastMonth",
    "last_month": "lastMonth",
    "thisweek": "thisWeek",
    "this_week": "thisWeek",
    "lastweek": "lastWeek",
    "last_week": "lastWeek",
    "thisyear": "thisYear",
    "this_year": "thisYear",
    "yesterday": "yesterday",
    "today": "today",
    "last30days": "last30Days",
    "last_30_days": "last30Days",
    "last7days": "last7Days",
    "last_7_days": "last7Days",
}

_DEFAULT_COLOR_SCHEME = {
    "id": "default",
    "label": "",
    "value": [
        "#00C0C7",
        "#5144D3",
        "#E8871A",
        "#DA3490",
        "#9089FA",
        "#47E26F",
        "#2780EB",
        "#6F38B1",
        "#DFBF03",
        "#CB6F10",
        "#268D6C",
        "#9BEC54",
        "#5EABFA",
        "#BE40CC",
        "#F56BB7",
        "#FEE02D",
    ],
}


class AdobeAnalyticsConnector:
    """Interfaces with Adobe Analytics 2.0 API using client credentials grant."""

    def __init__(self):
        # In-memory token cache: {org_id: {token, expiry}}
        self._token_cache: dict[str, dict[str, Any]] = {}
        # In-memory company-id cache: {org_id or client_id: globalCompanyId}
        self._company_cache: dict[str, str] = {}

    async def _get_adobe_token(self, client_id: str, client_secret: str, org_id: str) -> dict:
        """Get or refresh Adobe IMS access token. Caches tokens with expiry."""
        cache_key = org_id or client_id
        cached = self._token_cache.get(cache_key, {})

        if cached.get("token") and cached.get("expiry", 0) > time.time() + 60:
            return {"token": cached["token"]}

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{_IMS_BASE}/ims/token/v3",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "scope": "openid,AdobeID,read_organizations,additional_info.projectedProductContext",
                    },
                )

                if response.status_code >= 400:
                    return {
                        "error": True,
                        "status_code": response.status_code,
                        "message": response.text,
                    }

                data = response.json()
                token = data.get("access_token")
                expires_in = data.get("expires_in", 3600)

                self._token_cache[cache_key] = {
                    "token": token,
                    "expiry": time.time() + expires_in,
                }

                return {"token": token}

        except Exception as e:
            logger.error("Adobe token request error: %s", e)
            return {"error": True, "message": str(e)}

    async def resolve_company_id(
        self,
        client_id: str,
        client_secret: str,
        org_id: str,
        company_id: str | None = None,
    ) -> dict:
        """
        Resolve the Analytics globalCompanyId used in /api/{id} paths.

        Preference: explicit company_id → cached value → discovery/me →
        non-IMS org_id treated as a company id.
        """
        explicit = (company_id or "").strip()
        if explicit and not _looks_like_ims_org(explicit):
            self._remember_company(org_id, client_id, explicit)
            return {"company_id": explicit, "source": "explicit"}

        cached = self._company_cache.get(org_id) or self._company_cache.get(client_id)
        if cached:
            return {"company_id": cached, "source": "cache"}

        org = (org_id or "").strip()
        if org and not _looks_like_ims_org(org):
            self._remember_company(org_id, client_id, org)
            return {"company_id": org, "source": "org_id"}

        discovered = await self.discover_companies(client_id, client_secret, org_id)
        if discovered.get("error"):
            return discovered

        companies = discovered.get("companies") or []
        picked = _pick_company(companies, org)
        if not picked:
            return {
                "error": True,
                "error_type": "not_connected",
                "message": (
                    "Adobe Analytics discovery returned no globalCompanyId. "
                    "Save the Analytics Company ID on the Adobe connection "
                    "(Developer Console / discovery/me), then retry."
                ),
            }
        resolved = str(picked.get("globalCompanyId") or "").strip()
        if not resolved:
            return {
                "error": True,
                "error_type": "not_connected",
                "message": "Adobe Analytics discovery returned a company without globalCompanyId.",
            }
        self._remember_company(org_id, client_id, resolved)
        return {
            "company_id": resolved,
            "company_name": picked.get("companyName"),
            "source": "discovery",
            "companies": companies,
        }

    async def discover_companies(self, client_id: str, client_secret: str, org_id: str) -> dict:
        """GET /discovery/me — list Analytics companies for this credential."""
        token_result = await self._get_adobe_token(client_id, client_secret, org_id)
        if token_result.get("error"):
            return _map_adobe_http_error(token_result, resource="Adobe Analytics discovery")

        headers = {
            "Authorization": f"Bearer {token_result.get('token')}",
            "Accept": "application/json",
            "x-api-key": client_id,
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(f"{_ANALYTICS_BASE}/discovery/me", headers=headers)
            if response.status_code >= 400:
                return _map_adobe_http_error(
                    {"error": True, "status_code": response.status_code, "message": response.text},
                    resource="Adobe Analytics discovery",
                )
            payload = _parse_json_body(response)
            if payload.get("error"):
                return payload
            companies = _companies_from_discovery(payload, org_id)
            return {
                "ims_user_id": payload.get("imsUserId"),
                "companies": companies,
                "total": len(companies),
            }
        except Exception as e:
            logger.error("Adobe discovery request error: %s", e)
            return {"error": True, "error_type": "upstream_error", "message": str(e)}

    def _remember_company(self, org_id: str, client_id: str, company_id: str) -> None:
        if org_id:
            self._company_cache[org_id] = company_id
        if client_id:
            self._company_cache[client_id] = company_id

    async def _request(
        self,
        client_id: str,
        client_secret: str,
        org_id: str,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | list[Any] | None = None,
        company_id: str | None = None,
        timeout: float = 30.0,
    ) -> dict:
        """
        Authenticated Analytics 2.0 request.

        ``endpoint`` is relative to /api/{GLOBAL_COMPANY_ID}, e.g. ``/projects``.
        JSON arrays are wrapped as ``{"items": [...]}`` so callers can always
        use dict helpers.
        """
        token_result = await self._get_adobe_token(client_id, client_secret, org_id)
        if token_result.get("error"):
            return token_result

        resolved = await self.resolve_company_id(client_id, client_secret, org_id, company_id)
        if resolved.get("error"):
            return resolved
        cid = str(resolved["company_id"])

        token = token_result.get("token")
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-api-key": client_id,
            "x-proxy-global-company-id": cid,
        }

        if not endpoint.startswith("/"):
            endpoint = f"/{endpoint}"
        url = f"{_ANALYTICS_BASE}/api/{cid}{endpoint}"

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
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

                parsed = _parse_json_body(response)
                if isinstance(parsed, dict):
                    parsed.setdefault("_company_id", cid)
                return parsed

        except Exception as e:
            logger.error("Adobe Analytics API request error: %s", e)
            return {"error": True, "message": str(e)}

    # ------------------------------------------------------------------
    # Layer 1: Data Access
    # ------------------------------------------------------------------

    @friendly_errors("Adobe Analytics")
    async def list_report_suites(
        self,
        client_id: str,
        client_secret: str,
        org_id: str,
        company_id: str | None = None,
        limit: int = 100,
        page: int = 0,
    ) -> dict:
        """
        List report suites for the Analytics company.

        GET /api/{company}/reportsuites/collections/suites
        """
        result = await self._request(
            client_id,
            client_secret,
            org_id,
            "GET",
            "/reportsuites/collections/suites",
            params={"limit": limit, "page": page, "expansion": "name"},
            company_id=company_id,
        )
        if result.get("error"):
            # Older tenants still answer /collections/suites (404 only).
            if result.get("status_code") == 404:
                fallback = await self._request(
                    client_id,
                    client_secret,
                    org_id,
                    "GET",
                    "/collections/suites",
                    params={"limit": limit, "page": page},
                    company_id=company_id,
                )
                if not fallback.get("error"):
                    result = fallback
                else:
                    return _map_adobe_http_error(result, resource="report suite")
            else:
                return _map_adobe_http_error(result, resource="report suite")

        suites = _collection_items(result, "suites", "content", "reportSuites", "items")
        return {
            "report_suites": [_summarize_suite(s) for s in suites],
            "total": result.get("totalElements", len(suites)),
            "company_id": result.get("_company_id"),
        }

    @friendly_errors("Adobe Analytics")
    async def get_dimensions(
        self,
        client_id: str,
        client_secret: str,
        org_id: str,
        rsid: str,
        company_id: str | None = None,
    ) -> dict:
        """List dimensions for a report suite. GET /dimensions?rsid="""
        err = _require_nonempty("rsid", rsid)
        if err:
            return err
        result = await self._request(
            client_id,
            client_secret,
            org_id,
            "GET",
            "/dimensions",
            params={"rsid": rsid, "locale": "en_US"},
            company_id=company_id,
        )
        if result.get("error"):
            return _map_adobe_http_error(result, resource="dimension catalog")

        dimensions = _collection_items(result, "dimensions", "content", "items")
        return {
            "rsid": rsid,
            "dimensions": [
                {
                    "id": d.get("id"),
                    "name": d.get("name") or d.get("title"),
                    "type": d.get("type"),
                    "category": d.get("category"),
                }
                for d in dimensions
                if isinstance(d, dict)
            ],
            "total": len(dimensions),
        }

    @friendly_errors("Adobe Analytics")
    async def get_metrics(
        self,
        client_id: str,
        client_secret: str,
        org_id: str,
        rsid: str,
        company_id: str | None = None,
    ) -> dict:
        """List metrics for a report suite. GET /metrics?rsid="""
        err = _require_nonempty("rsid", rsid)
        if err:
            return err
        result = await self._request(
            client_id,
            client_secret,
            org_id,
            "GET",
            "/metrics",
            params={"rsid": rsid, "locale": "en_US"},
            company_id=company_id,
        )
        if result.get("error"):
            return _map_adobe_http_error(result, resource="metric catalog")

        metrics = _collection_items(result, "metrics", "content", "items")
        return {
            "rsid": rsid,
            "metrics": [
                {
                    "id": m.get("id"),
                    "name": m.get("name") or m.get("title"),
                    "type": m.get("type"),
                    "category": m.get("category"),
                }
                for m in metrics
                if isinstance(m, dict)
            ],
            "total": len(metrics),
        }

    @friendly_errors("Adobe Analytics")
    async def run_report(
        self,
        client_id: str,
        client_secret: str,
        org_id: str,
        rsid: str,
        dimensions: list[str],
        metrics: list[str],
        date_range: dict[str, str] | str,
        limit: int = 100,
        company_id: str | None = None,
    ) -> dict:
        """
        Run a ranked report.

        POST /api/{company}/reports

        Date range is sent as a globalFilters dateRange (ISO start/end or
        formula). Metrics use ``{"columnId","id"}``. Dimension ids are
        normalised to ``variables/...``.
        """
        err = _require_nonempty("rsid", rsid)
        if err:
            return err
        metric_ids = [_normalize_metric_id(m) for m in (metrics or []) if str(m).strip()]
        if not metric_ids:
            return {
                "error": True,
                "error_type": "invalid_param",
                "message": "At least one metric is required (e.g. metrics/visits or 'visits').",
            }
        date_filter = _date_range_string(date_range)
        if not date_filter:
            return {
                "error": True,
                "error_type": "invalid_param",
                "message": (
                    "date_range is required. Pass {start, end} as YYYY-MM-DD, "
                    "a 'start/end' string, or a formula such as td-30d/td."
                ),
            }

        dim_ids = [_normalize_dimension_id(d) for d in (dimensions or []) if str(d).strip()]
        json_body: dict[str, Any] = {
            "rsid": rsid,
            "globalFilters": [{"type": "dateRange", "dateRange": date_filter}],
            "metricContainer": {
                "metrics": [{"columnId": str(i), "id": mid} for i, mid in enumerate(metric_ids)],
            },
            "settings": {
                "limit": max(1, int(limit or 100)),
                "page": 0,
                "nonesBehavior": "exclude-nones",
                "countRepeatInstances": True,
            },
        }
        if dim_ids:
            json_body["dimension"] = dim_ids[0]

        result = await self._request(
            client_id,
            client_secret,
            org_id,
            "POST",
            "/reports",
            json_body=json_body,
            company_id=company_id,
            timeout=60.0,
        )
        if result.get("error"):
            return _map_adobe_http_error(result, resource="report")

        rows = result.get("rows") if isinstance(result.get("rows"), list) else []
        return {
            "rsid": rsid,
            "dimensions": dim_ids,
            "metrics": metric_ids,
            "date_range": date_filter,
            "rows": rows,
            "summary": result.get("summaryData") or {},
            "columns": result.get("columns") or {},
            "row_count": len(rows),
        }

    @friendly_errors("Adobe Analytics")
    async def get_segments(
        self,
        client_id: str,
        client_secret: str,
        org_id: str,
        rsid: str | None = None,
        company_id: str | None = None,
    ) -> dict:
        """List segments. GET /segments"""
        params: dict[str, Any] = {"limit": 100, "locale": "en_US"}
        if rsid:
            params["rsid"] = rsid

        result = await self._request(
            client_id,
            client_secret,
            org_id,
            "GET",
            "/segments",
            params=params,
            company_id=company_id,
        )
        if result.get("error"):
            return _map_adobe_http_error(result, resource="segment")

        segments = _collection_items(result, "segments", "content", "items")
        return {
            "rsid": rsid,
            "segments": [
                {
                    "id": s.get("id"),
                    "name": s.get("name"),
                    "description": s.get("description"),
                    "owner": s.get("owner"),
                    "created": s.get("created"),
                }
                for s in segments
                if isinstance(s, dict)
            ],
            "total": result.get("totalElements", len(segments)),
        }

    @friendly_errors("Adobe Analytics")
    async def get_calculated_metrics(
        self,
        client_id: str,
        client_secret: str,
        org_id: str,
        rsid: str | None = None,
        company_id: str | None = None,
    ) -> dict:
        """List calculated metrics. GET /calculatedmetrics"""
        params: dict[str, Any] = {"limit": 100, "locale": "en_US"}
        if rsid:
            params["rsid"] = rsid

        result = await self._request(
            client_id,
            client_secret,
            org_id,
            "GET",
            "/calculatedmetrics",
            params=params,
            company_id=company_id,
        )
        if result.get("error"):
            return _map_adobe_http_error(result, resource="calculated metric")

        metrics = _collection_items(result, "calculatedMetrics", "content", "items", "metrics")
        return {
            "rsid": rsid,
            "calculated_metrics": [
                {
                    "id": m.get("id"),
                    "name": m.get("name"),
                    "description": m.get("description"),
                    "owner": m.get("owner"),
                }
                for m in metrics
                if isinstance(m, dict)
            ],
            "total": result.get("totalElements", len(metrics)),
        }

    # ------------------------------------------------------------------
    # Layer 2: Audit
    # ------------------------------------------------------------------

    @friendly_errors("Adobe Analytics")
    async def audit_report_suite(
        self,
        client_id: str,
        client_secret: str,
        org_id: str,
        rsid: str,
        company_id: str | None = None,
    ) -> dict:
        """Audit a report suite: enabled dimensions, custom events, eVars."""
        dims_result = await self.get_dimensions(client_id, client_secret, org_id, rsid, company_id=company_id)
        if dims_result.get("error"):
            return dims_result

        metrics_result = await self.get_metrics(client_id, client_secret, org_id, rsid, company_id=company_id)
        if metrics_result.get("error"):
            return metrics_result

        dims = dims_result.get("dimensions", [])
        metrics_list = metrics_result.get("metrics", [])

        custom_evars = [d for d in dims if "evar" in str(d.get("id", "")).lower()]
        custom_metrics = [m for m in metrics_list if "event" in str(m.get("id", "")).lower()]

        return {
            "rsid": rsid,
            "total_dimensions": len(dims),
            "total_metrics": len(metrics_list),
            "custom_evars": len(custom_evars),
            "custom_events": len(custom_metrics),
            "health_score": min(100, (len(dims) + len(metrics_list)) * 5),
        }

    @friendly_errors("Adobe Analytics")
    async def check_data_quality(
        self,
        client_id: str,
        client_secret: str,
        org_id: str,
        rsid: str,
        days_back: int = 30,
        company_id: str | None = None,
    ) -> dict:
        """Run a basic traffic report to check recent data quality."""
        lookback = max(1, int(days_back or 30))
        result = await self.run_report(
            client_id,
            client_secret,
            org_id,
            rsid,
            dimensions=["variables/daterangeday"],
            metrics=["metrics/visits"],
            date_range=f"td-{lookback}d/td",
            limit=lookback,
            company_id=company_id,
        )
        if result.get("error"):
            return result

        return {
            "rsid": rsid,
            "days_checked": lookback,
            "rows_returned": result.get("row_count", 0),
            "has_data": result.get("row_count", 0) > 0,
            "status": "healthy" if result.get("row_count", 0) > 0 else "no_data",
        }

    # ------------------------------------------------------------------
    # Layer 3: Write Operations
    # ------------------------------------------------------------------

    @friendly_errors("Adobe Analytics")
    async def create_segment(
        self,
        client_id: str,
        client_secret: str,
        org_id: str,
        name: str,
        rsid: str,
        definition: dict[str, Any],
        description: str | None = None,
        company_id: str | None = None,
    ) -> dict:
        """Create a segment. POST /segments"""
        json_body: dict[str, Any] = {
            "name": name,
            "rsid": rsid,
            "definition": definition,
        }
        if description:
            json_body["description"] = description

        result = await self._request(
            client_id,
            client_secret,
            org_id,
            "POST",
            "/segments",
            json_body=json_body,
            company_id=company_id,
        )
        if result.get("error"):
            return _map_adobe_http_error(result, resource="segment")

        return {
            "success": True,
            "segment_id": result.get("id"),
            "name": name,
            "message": "Segment created successfully",
        }

    @friendly_errors("Adobe Analytics")
    async def update_segment(
        self,
        client_id: str,
        client_secret: str,
        org_id: str,
        segment_id: str,
        name: str | None = None,
        description: str | None = None,
        definition: dict[str, Any] | None = None,
        company_id: str | None = None,
    ) -> dict:
        """Update a segment. PUT /segments/{id}"""
        json_body: dict[str, Any] = {}
        if name:
            json_body["name"] = name
        if description:
            json_body["description"] = description
        if definition:
            json_body["definition"] = definition

        result = await self._request(
            client_id,
            client_secret,
            org_id,
            "PUT",
            f"/segments/{quote(str(segment_id), safe='')}",
            json_body=json_body,
            company_id=company_id,
        )
        if result.get("error"):
            return _map_adobe_http_error(result, resource="segment")

        return {
            "success": True,
            "segment_id": segment_id,
            "message": "Segment updated successfully",
        }

    @friendly_errors("Adobe Analytics")
    async def delete_segment(
        self,
        client_id: str,
        client_secret: str,
        org_id: str,
        segment_id: str,
        company_id: str | None = None,
    ) -> dict:
        """Delete a segment. DELETE /segments/{id}"""
        result = await self._request(
            client_id,
            client_secret,
            org_id,
            "DELETE",
            f"/segments/{quote(str(segment_id), safe='')}",
            company_id=company_id,
        )
        if result.get("error"):
            return _map_adobe_http_error(result, resource="segment")

        return {
            "success": True,
            "segment_id": segment_id,
            "message": "Segment deleted successfully",
        }

    @friendly_errors("Adobe Analytics")
    async def create_calculated_metric(
        self,
        client_id: str,
        client_secret: str,
        org_id: str,
        name: str,
        rsid: str,
        definition: dict[str, Any],
        description: str | None = None,
        company_id: str | None = None,
    ) -> dict:
        """Create a calculated metric. POST /calculatedmetrics"""
        json_body: dict[str, Any] = {
            "name": name,
            "rsid": rsid,
            "definition": definition,
        }
        if description:
            json_body["description"] = description

        result = await self._request(
            client_id,
            client_secret,
            org_id,
            "POST",
            "/calculatedmetrics",
            json_body=json_body,
            company_id=company_id,
        )
        if result.get("error"):
            return _map_adobe_http_error(result, resource="calculated metric")

        return {
            "success": True,
            "metric_id": result.get("id"),
            "name": name,
            "message": "Calculated metric created successfully",
        }

    @friendly_errors("Adobe Analytics")
    async def delete_calculated_metric(
        self,
        client_id: str,
        client_secret: str,
        org_id: str,
        metric_id: str,
        company_id: str | None = None,
    ) -> dict:
        """Delete a calculated metric. DELETE /calculatedmetrics/{id}"""
        result = await self._request(
            client_id,
            client_secret,
            org_id,
            "DELETE",
            f"/calculatedmetrics/{quote(str(metric_id), safe='')}",
            company_id=company_id,
        )
        if result.get("error"):
            return _map_adobe_http_error(result, resource="calculated metric")

        return {
            "success": True,
            "metric_id": metric_id,
            "message": "Calculated metric deleted successfully",
        }

    # ------------------------------------------------------------------
    # Layer 4: Workspace projects (Analysis Workspace reports)
    # ------------------------------------------------------------------

    @friendly_errors("Adobe Analytics")
    async def list_projects(
        self,
        client_id: str,
        client_secret: str,
        org_id: str,
        expansion: list[str] | str | None = None,
        include_type: str | None = None,
        limit: int | None = None,
        page: int | None = None,
        locale: str | None = None,
        company_id: str | None = None,
    ) -> dict:
        """List Analysis Workspace projects. GET /projects"""
        params = _project_list_query_params(
            expansion=expansion,
            include_type=include_type,
            limit=limit,
            page=page,
            locale=locale,
        )
        result = await self._request(
            client_id,
            client_secret,
            org_id,
            "GET",
            "/projects",
            params=params or None,
            company_id=company_id,
        )
        if result.get("error"):
            return _map_adobe_http_error(result, resource="Workspace project")

        raw_items = _project_list_items(result)
        include_definition = _csv_contains(params.get("expansion"), "definition")
        return {
            "projects": [_summarize_project(p, include_definition=include_definition) for p in raw_items],
            "total": result.get("totalElements", len(raw_items)),
            "page": result.get("number", page if page is not None else 0),
            "total_pages": result.get("totalPages"),
            "limit": result.get("size", limit),
            "first_page": result.get("firstPage"),
            "last_page": result.get("lastPage"),
        }

    @friendly_errors("Adobe Analytics")
    async def get_project(
        self,
        client_id: str,
        client_secret: str,
        org_id: str,
        project_id: str,
        expansion: list[str] | str | None = None,
        company_id: str | None = None,
    ) -> dict:
        """Fetch one Workspace project including its full definition."""
        err = validate_project_id(project_id)
        if err:
            return err

        expansion_list = _as_str_list(expansion)
        if "definition" not in expansion_list:
            expansion_list.append("definition")

        result = await self._request(
            client_id,
            client_secret,
            org_id,
            "GET",
            _project_resource_path(project_id),
            params={"expansion": ",".join(expansion_list)},
            company_id=company_id,
        )
        if result.get("error"):
            return _map_adobe_http_error(result, resource="Workspace project")

        project = _unwrap_project(result)
        if not isinstance(project, dict) or not project.get("id"):
            return {
                "error": True,
                "error_type": "upstream_error",
                "message": "Adobe Analytics returned an unexpected project payload.",
            }
        return project

    @friendly_errors("Adobe Analytics")
    async def build_definition(
        self,
        rsid: str,
        tables: list[dict[str, Any]] | None = None,
        date_range: str | None = None,
        definition: dict[str, Any] | None = None,
    ) -> dict:
        """Build a valid Analysis Workspace definition from a compact table spec."""
        err = _require_nonempty("rsid", rsid)
        if err:
            return err
        try:
            built = coerce_project_definition(
                definition,
                rsid=rsid,
                tables=tables,
                date_range=date_range,
            )
        except ValueError as exc:
            return {"error": True, "error_type": "invalid_param", "message": str(exc)}
        return {
            "rsid": rsid,
            "definition": built,
            "message": (
                "Valid Analysis Workspace definition. Pass this as "
                "config.definition to adobe_workspace_create_project, or omit "
                "definition and pass config.tables instead — create builds this "
                "automatically."
            ),
        }

    # Action-name alias used by analytics_read / route drift tests.
    build_project_definition = build_definition

    @friendly_errors("Adobe Analytics")
    async def validate_project(
        self,
        client_id: str,
        client_secret: str,
        org_id: str,
        rsid: str,
        definition: dict[str, Any],
        company_id: str | None = None,
        name: str | None = None,
    ) -> dict:
        """Validate a Workspace definition against a report suite."""
        err = _require_nonempty("rsid", rsid)
        if err:
            return err
        if not isinstance(definition, dict):
            return {
                "error": True,
                "error_type": "invalid_param",
                "message": "definition must be a JSON object (Workspace project definition).",
            }
        try:
            definition = coerce_project_definition(definition, rsid=rsid)
        except ValueError as exc:
            return {"error": True, "error_type": "invalid_param", "message": str(exc)}

        body = {
            "project": {
                "name": name or "Validation",
                "rsid": rsid,
                "type": "project",
                "definition": definition,
            }
        }
        result = await self._request(
            client_id,
            client_secret,
            org_id,
            "POST",
            "/projects/validate",
            params={"rsid": rsid},
            json_body=body,
            company_id=company_id,
        )
        if result.get("error"):
            return _map_adobe_http_error(result, resource="Workspace project validation")

        valid = result.get("valid")
        if valid is None:
            valid = result.get("validator_version") is not None or result.get("validatorVersion") is not None
        return {
            "valid": bool(valid) if valid is not None else True,
            "rsid": rsid,
            "validator_version": result.get("validatorVersion") or result.get("validator_version"),
            "message": result.get("message")
            or ("Definition is valid for this report suite." if valid else "Definition is not valid."),
            "raw": {k: v for k, v in result.items() if k != "_company_id"},
        }

    @friendly_errors("Adobe Analytics")
    async def create_project(
        self,
        client_id: str,
        client_secret: str,
        org_id: str,
        name: str,
        rsid: str,
        definition: dict[str, Any] | None = None,
        description: str | None = None,
        extra: dict[str, Any] | None = None,
        tables: list[dict[str, Any]] | None = None,
        date_range: str | None = None,
        validate: bool = False,
        company_id: str | None = None,
    ) -> dict:
        """
        Create a Workspace project.

        Accepts either a full Workspace ``definition`` or a compact ``tables``
        spec. Incomplete definitions (missing ``workspaces``) are expanded
        into Adobe's published structure before POST.
        """
        err = _require_nonempty("name", name)
        if err:
            return err
        err = _require_nonempty("rsid", rsid)
        if err:
            return err

        try:
            definition = coerce_project_definition(
                definition,
                rsid=rsid,
                tables=tables,
                date_range=date_range,
            )
        except ValueError as exc:
            return {"error": True, "error_type": "invalid_param", "message": str(exc)}

        if validate:
            checked = await self.validate_project(
                client_id,
                client_secret,
                org_id,
                rsid=rsid,
                definition=definition,
                company_id=company_id,
                name=name,
            )
            if checked.get("error"):
                return checked
            if checked.get("valid") is False:
                return {
                    "error": True,
                    "error_type": "invalid_param",
                    "message": (
                        "Adobe rejected the Workspace definition for this report suite. "
                        + str(checked.get("message") or "")
                    ),
                    "validation": checked,
                }

        json_body: dict[str, Any] = {
            "name": name,
            "rsid": rsid,
            "definition": definition,
            "type": "project",
        }
        if description is not None:
            json_body["description"] = description
        if extra:
            for key, value in extra.items():
                if key in _PROJECT_CREATE_OPTIONAL_FIELDS:
                    json_body[key] = value

        result = await self._request(
            client_id,
            client_secret,
            org_id,
            "POST",
            "/projects",
            json_body=json_body,
            company_id=company_id,
        )
        if result.get("error"):
            return _map_adobe_http_error(result, resource="Workspace project")

        created = _unwrap_project(result)
        return {
            "success": True,
            "project_id": created.get("id"),
            "name": created.get("name", name),
            "rsid": created.get("rsid", rsid),
            "message": "Workspace project created successfully",
        }

    @friendly_errors("Adobe Analytics")
    async def update_project(
        self,
        client_id: str,
        client_secret: str,
        org_id: str,
        project_id: str,
        name: str | None = None,
        description: str | None = None,
        rsid: str | None = None,
        definition: dict[str, Any] | None = None,
        owner: dict[str, Any] | None = None,
        updates: dict[str, Any] | None = None,
        merge_definition: bool = False,
        tables: list[dict[str, Any]] | None = None,
        date_range: str | None = None,
        company_id: str | None = None,
    ) -> dict:
        """Update a Workspace project with a minimal partial PUT."""
        err = validate_project_id(project_id)
        if err:
            return err
        if name is not None:
            err = _require_nonempty("name", name)
            if err:
                return err

        if definition is not None and not isinstance(definition, dict):
            return {
                "error": True,
                "error_type": "invalid_param",
                "message": "definition must be a JSON object when provided.",
            }

        # Rebuild from compact tables, or expand a compact spec. A partial
        # definition overlay (e.g. {"version": "32"}) is left alone so Adobe's
        # top-level PUT / merge_definition contract stays intact.
        if tables or _is_compact_workspace_spec(definition):
            current = None
            target_rsid = rsid
            if not target_rsid:
                current = await self.get_project(
                    client_id, client_secret, org_id, project_id, company_id=company_id
                )
                if current.get("error"):
                    return current
                target_rsid = current.get("rsid")
            try:
                definition = coerce_project_definition(
                    definition if isinstance(definition, dict) else None,
                    rsid=str(target_rsid or ""),
                    tables=tables,
                    date_range=date_range,
                )
            except ValueError as exc:
                return {"error": True, "error_type": "invalid_param", "message": str(exc)}

        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if rsid is not None:
            body["rsid"] = rsid
        if owner is not None:
            body["owner"] = owner
        if definition is not None:
            body["definition"] = definition
        if updates:
            for key, value in updates.items():
                if key in _PROJECT_UPDATE_OPTIONAL_FIELDS and key not in body:
                    body[key] = value

        if not body:
            return {
                "error": True,
                "error_type": "invalid_param",
                "message": (
                    "update_project requires at least one writable field "
                    "(name, description, rsid, definition, tables, owner, tags, shares)."
                ),
            }

        merged_definition = False
        if merge_definition and isinstance(body.get("definition"), dict):
            current = await self.get_project(
                client_id, client_secret, org_id, project_id, company_id=company_id
            )
            if current.get("error"):
                return current
            existing_definition = current.get("definition")
            if not isinstance(existing_definition, dict):
                existing_definition = {}
            body["definition"] = _deep_merge(existing_definition, body["definition"])
            merged_definition = True

        result = await self._request(
            client_id,
            client_secret,
            org_id,
            "PUT",
            _project_resource_path(project_id),
            json_body=body,
            company_id=company_id,
        )
        if result.get("error"):
            return _map_adobe_http_error(result, resource="Workspace project")

        updated = _unwrap_project(result)
        message = "Workspace project updated via partial PUT."
        if merged_definition:
            message = (
                "Workspace project updated via partial PUT with opt-in definition merge. "
                "Only the definition subtree was fetched and merged; server-managed "
                "fields were not resent."
            )
        return {
            "success": True,
            "project_id": updated.get("id", project_id),
            "name": updated.get("name", body.get("name")),
            "rsid": updated.get("rsid", body.get("rsid")),
            "updated_fields": sorted(body.keys()),
            "merged_definition": merged_definition,
            "message": message,
        }

    @friendly_errors("Adobe Analytics")
    async def delete_project(
        self,
        client_id: str,
        client_secret: str,
        org_id: str,
        project_id: str,
        company_id: str | None = None,
    ) -> dict:
        """Delete one Workspace project by explicit id."""
        err = validate_project_id(project_id)
        if err:
            return err

        result = await self._request(
            client_id,
            client_secret,
            org_id,
            "DELETE",
            _project_resource_path(project_id),
            company_id=company_id,
        )
        if result.get("error"):
            return _map_adobe_http_error(result, resource="Workspace project")

        return {
            "success": True,
            "project_id": project_id,
            "message": "Workspace project deleted",
        }

    @friendly_errors("Adobe Analytics")
    async def copy_project(
        self,
        client_id: str,
        client_secret: str,
        org_id: str,
        project_id: str,
        name: str,
        company_id: str | None = None,
    ) -> dict:
        """Duplicate a Workspace project (GET source + POST writable fields)."""
        err = validate_project_id(project_id)
        if err:
            return err
        err = _require_nonempty("name", name)
        if err:
            return err

        source = await self.get_project(client_id, client_secret, org_id, project_id, company_id=company_id)
        if source.get("error"):
            return source

        definition = source.get("definition")
        if not isinstance(definition, dict):
            return {
                "error": True,
                "error_type": "invalid_param",
                "message": "Source project has no definition; cannot copy.",
            }
        rsid = source.get("rsid")
        if not rsid:
            return {
                "error": True,
                "error_type": "invalid_param",
                "message": "Source project has no rsid; cannot copy.",
            }

        extra = {key: source[key] for key in _PROJECT_CREATE_OPTIONAL_FIELDS if key in source}
        created = await self.create_project(
            client_id,
            client_secret,
            org_id,
            name=name,
            rsid=str(rsid),
            definition=definition,
            description=source.get("description"),
            extra=extra,
            company_id=company_id,
        )
        if created.get("error"):
            return created
        created["copied_from"] = project_id
        created["message"] = "Workspace project copied successfully"
        return created


# ---------------------------------------------------------------------------
# Workspace project helpers
# ---------------------------------------------------------------------------


def validate_project_id(project_id: Any) -> dict[str, Any] | None:
    """Reject blank, wildcard, or path-like project ids before they are interpolated."""
    if project_id is None or (isinstance(project_id, str) and not project_id.strip()):
        return {
            "error": True,
            "error_type": "invalid_param",
            "message": (
                "project_id is required and must be a non-empty Adobe Workspace "
                "project id (letters, digits, underscore, hyphen only; max 128 chars). "
                "Wildcards, paths, and slashes are not allowed. "
                "Use adobe_workspace_list_projects to obtain a valid id."
            ),
        }
    pid = str(project_id).strip()
    if not _PROJECT_ID_RE.fullmatch(pid):
        return {
            "error": True,
            "error_type": "invalid_param",
            "message": (
                f"Invalid project_id {pid!r}. Adobe Workspace project ids contain "
                "only letters, digits, underscores, and hyphens (1-128 chars). "
                "Wildcards ('*'), path segments ('../'), and slashes are rejected. "
                "Use adobe_workspace_list_projects to obtain a valid id."
            ),
        }
    return None


def _project_resource_path(project_id: str) -> str:
    return f"/projects/{quote(str(project_id).strip(), safe='')}"


def _require_nonempty(field: str, value: Any) -> dict[str, Any] | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return {
            "error": True,
            "error_type": "invalid_param",
            "message": f"{field} is required and must be a non-empty string.",
        }
    return None


def _as_str_list(value: list[str] | str | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return [str(part).strip() for part in value if str(part).strip()]


def _csv_contains(csv: Any, token: str) -> bool:
    if not csv:
        return False
    return token in {part.strip() for part in str(csv).split(",")}


def _project_list_query_params(
    *,
    expansion: list[str] | str | None,
    include_type: str | None,
    limit: int | None,
    page: int | None,
    locale: str | None,
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if expansion is None:
        params["expansion"] = _DEFAULT_LIST_EXPANSION
    else:
        expansion_csv = ",".join(_as_str_list(expansion))
        if expansion_csv:
            params["expansion"] = expansion_csv
    if include_type:
        params["includeType"] = include_type
    if limit is not None:
        params["limit"] = limit
    if page is not None:
        params["page"] = page
    if locale:
        params["locale"] = locale
    return params


def _project_list_items(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for p in _collection_items(result, "content", "projects", "items") if isinstance(p, dict)]


def _unwrap_project(result: dict[str, Any]) -> dict[str, Any]:
    if isinstance(result.get("project"), dict):
        return result["project"]
    return {k: v for k, v in result.items() if k != "_company_id"}


def _summarize_project(project: dict[str, Any], *, include_definition: bool) -> dict[str, Any]:
    owner = project.get("owner")
    owner_out: Any = owner
    if isinstance(owner, dict):
        owner_out = {
            "id": owner.get("id"),
            "name": owner.get("name") or owner.get("fullName") or owner.get("full_name"),
        }
        if project.get("ownerFullName") and not owner_out.get("name"):
            owner_out["name"] = project.get("ownerFullName")
    elif project.get("ownerFullName"):
        owner_out = {"id": owner, "name": project.get("ownerFullName")}

    summary: dict[str, Any] = {
        "id": project.get("id"),
        "name": project.get("name"),
        "description": project.get("description"),
        "rsid": project.get("rsid"),
        "type": project.get("type"),
        "created": project.get("created"),
        "modified": project.get("modified"),
        "owner": owner_out,
    }
    report_suite_name = project.get("reportSuiteName") or project.get("report_suite_name")
    if report_suite_name:
        summary["report_suite_name"] = report_suite_name
    if include_definition and "definition" in project:
        summary["definition"] = project["definition"]
    if "shares" in project:
        summary["shares"] = project["shares"]
    if "tags" in project:
        summary["tags"] = project["tags"]
    access_level = project.get("accessLevel")
    if access_level is not None:
        summary["access_level"] = access_level
    if "externalReferences" in project:
        summary["external_references"] = project["externalReferences"]
    return summary


def _deep_merge(base: Any, overlay: Any) -> Any:
    """Deep-merge ``overlay`` onto ``base``."""
    if isinstance(base, dict) and isinstance(overlay, dict):
        merged: dict[str, Any] = dict(base)
        for key, value in overlay.items():
            if key in merged:
                merged[key] = _deep_merge(merged[key], value)
            else:
                merged[key] = value
        return merged
    return overlay


def _extract_adobe_error(raw: Any) -> tuple[str | None, str | None]:
    """Pull Adobe errorCode / errorDescription out of a raw error body."""
    if raw is None:
        return None, None
    payload: Any = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None, None
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return None, text
    if not isinstance(payload, dict):
        return None, str(raw)
    code = (
        payload.get("errorCode")
        or payload.get("error_code")
        or payload.get("errorId")
        or payload.get("error")
    )
    if isinstance(code, dict):
        code = code.get("code") or code.get("errorCode")
    message = (
        payload.get("errorDescription")
        or payload.get("error_description")
        or payload.get("errorMessage")
        or payload.get("message")
        or payload.get("errorMsg")
    )
    if isinstance(code, str) and code.lower() in {"true", "false"}:
        code = None
    return (str(code) if code else None), (str(message) if message else None)


def _map_adobe_http_error(result: dict[str, Any], *, resource: str) -> dict[str, Any]:
    """Turn an Adobe HTTP error dict into an actionable tool error envelope."""
    status = result.get("status_code")
    adobe_code, adobe_msg = _extract_adobe_error(result.get("message"))
    if status == 404:
        error_type = "invalid_param"
        message = (
            f"Unknown {resource}. Check the id exists and this Analytics company "
            "(globalCompanyId, not the IMS org id) can access it."
        )
    elif status == 403:
        error_type = "insufficient_scope"
        message = (
            f"Adobe Analytics denied access to this {resource}. "
            "The technical account needs Workspace / project permissions on the product profile."
        )
    elif status == 400:
        error_type = "invalid_param"
        message = adobe_msg or f"Adobe Analytics rejected the {resource} payload as invalid."
    elif status == 429:
        error_type = "upstream_error"
        message = "Adobe Analytics is rate-limiting requests. Wait a moment and retry."
    elif status == 401:
        error_type = "not_connected"
        message = "Adobe Analytics authentication failed. Reconnect the Adobe integration."
    else:
        error_type = "upstream_error"
        message = adobe_msg or result.get("message") or f"Adobe Analytics {resource} request failed."

    if adobe_msg and adobe_msg not in str(message):
        message = f"{message} Adobe: {adobe_msg}"

    out: dict[str, Any] = {
        "error": True,
        "error_type": error_type,
        "status_code": status,
        "message": message,
    }
    if adobe_code:
        out["adobe_error_code"] = adobe_code
    if adobe_msg:
        out["adobe_error_message"] = adobe_msg
    return out


# ---------------------------------------------------------------------------
# Request / catalog helpers
# ---------------------------------------------------------------------------


def _looks_like_ims_org(value: str) -> bool:
    return bool(value) and ("@" in value or bool(_IMS_ORG_RE.search(value)))


def _parse_json_body(response: Any) -> dict[str, Any]:
    text = getattr(response, "text", "") or ""
    if not text.strip():
        return {"success": getattr(response, "status_code", 200) < 300}
    try:
        payload = response.json()
    except Exception:
        return {"success": getattr(response, "status_code", 200) < 300, "body": text}
    if isinstance(payload, list):
        return {"items": payload}
    if isinstance(payload, dict):
        return payload
    return {"body": payload}


def _collection_items(result: dict[str, Any], *keys: str) -> list[Any]:
    if not isinstance(result, dict):
        return []
    for key in keys:
        value = result.get(key)
        if isinstance(value, list):
            return value
    items = result.get("items")
    if isinstance(items, list):
        return items
    content = result.get("content")
    if isinstance(content, list):
        return content
    return []


def _companies_from_discovery(payload: dict[str, Any], org_id: str) -> list[dict[str, Any]]:
    companies: list[dict[str, Any]] = []
    for ims_org in payload.get("imsOrgs") or []:
        if not isinstance(ims_org, dict):
            continue
        org = str(ims_org.get("imsOrgId") or "")
        for company in ims_org.get("companies") or []:
            if not isinstance(company, dict):
                continue
            row = dict(company)
            row["imsOrgId"] = org
            companies.append(row)
    if not companies and isinstance(payload.get("companies"), list):
        companies = [c for c in payload["companies"] if isinstance(c, dict)]
    # Prefer companies that belong to the configured IMS org, but keep the rest.
    if org_id:
        matching = [c for c in companies if str(c.get("imsOrgId") or "") == org_id]
        if matching:
            return matching + [c for c in companies if c not in matching]
    return companies


def _pick_company(companies: list[dict[str, Any]], org_id: str) -> dict[str, Any] | None:
    if not companies:
        return None
    if org_id:
        for company in companies:
            if str(company.get("imsOrgId") or "") == org_id and company.get("globalCompanyId"):
                return company
    for company in companies:
        if company.get("globalCompanyId"):
            return company
    return companies[0]


def _summarize_suite(suite: dict[str, Any]) -> dict[str, Any]:
    return {
        "rsid": suite.get("rsid") or suite.get("id"),
        "name": suite.get("name") or suite.get("reportSuiteName"),
        "parent_rsid": suite.get("parentRsid") or suite.get("parent_rsid"),
        "time_zone": suite.get("timezoneZoneinfo") or suite.get("timezoneId") or suite.get("timezone"),
        "created": suite.get("created"),
        "currency": suite.get("currency"),
    }


def _normalize_metric_id(raw: Any) -> str:
    value = str(raw or "").strip()
    if not value:
        return "metrics/visits"
    if value.startswith("metrics/") or value.startswith("cm") or "/" in value:
        return value
    return f"metrics/{value}"


def _normalize_dimension_id(raw: Any) -> str:
    value = str(raw or "").strip()
    if not value:
        return "variables/page"
    if value.startswith("variables/") or "/" in value:
        return value
    return f"variables/{value}"


def _iso_day_start(value: str) -> str:
    text = value.strip()
    if "T" in text:
        return text
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return f"{text}T00:00:00.000"
    return text


def _date_range_string(date_range: dict[str, Any] | str | None) -> str:
    if date_range is None:
        return ""
    if isinstance(date_range, str):
        text = date_range.strip()
        if not text:
            return ""
        preset = _DATE_PRESETS.get(text.lower().replace(" ", ""))
        if preset == "last30Days":
            return "td-30d/td"
        if preset == "last7Days":
            return "td-7d/td"
        if "/" in text:
            start, _, end = text.partition("/")
            return f"{_iso_day_start(start)}/{_iso_day_start(end)}"
        return text
    if isinstance(date_range, dict):
        if date_range.get("dateRange"):
            return _date_range_string(str(date_range["dateRange"]))
        start = date_range.get("start") or date_range.get("startDate") or date_range.get("start_date")
        end = date_range.get("end") or date_range.get("endDate") or date_range.get("end_date")
        if start and end:
            return f"{_iso_day_start(str(start))}/{_iso_day_start(str(end))}"
    return ""


# ---------------------------------------------------------------------------
# Analysis Workspace definition builder
# ---------------------------------------------------------------------------


def _is_full_workspace_definition(definition: dict[str, Any]) -> bool:
    return isinstance(definition.get("workspaces"), list) and bool(definition.get("version"))


def _is_compact_workspace_spec(definition: Any) -> bool:
    """True when ``definition`` is a tables/metrics shortcut, not a PUT overlay."""
    if not isinstance(definition, dict) or _is_full_workspace_definition(definition):
        return False
    return bool(
        definition.get("tables")
        or definition.get("panels")
        or definition.get("metrics")
        or definition.get("dimension")
    )


def _new_id() -> str:
    return str(uuid.uuid4()).upper()


def _short_id() -> str:
    return uuid.uuid4().hex[:8]


def _pretty_component_name(component_id: str) -> str:
    leaf = component_id.split("/")[-1].replace("_", " ").replace("-", " ")
    return leaf[:1].upper() + leaf[1:] if leaf else component_id


def _date_range_entity(date_range: str | None) -> dict[str, Any]:
    raw = (date_range or "thisMonth").strip() or "thisMonth"
    preset_key = raw.lower().replace(" ", "").replace("_", "")
    preset = _DATE_PRESETS.get(preset_key)
    if preset in {"last30Days", "last7Days"} or "/" in raw:
        # Custom / formula ranges use an explicit dateRange string on the entity.
        formula = _date_range_string(raw) or "td-30d/td"
        return {
            "id": "custom",
            "__entity__": True,
            "type": "DateRange",
            "dateRange": formula,
            "__metaData__": {"name": "Custom"},
        }
    entity_id = preset or raw
    return {
        "id": entity_id,
        "__entity__": True,
        "type": "DateRange",
        "__metaData__": {"name": _pretty_component_name(entity_id)},
    }


def _metric_entity(metric_id: str) -> dict[str, Any]:
    mid = _normalize_metric_id(metric_id)
    return {
        "id": mid,
        "__entity__": True,
        "type": "Metric",
        "__metaData__": {"name": _pretty_component_name(mid)},
    }


def _dimension_entity(dimension_id: str) -> dict[str, Any]:
    did = _normalize_dimension_id(dimension_id)
    return {
        "id": did,
        "__entity__": True,
        "type": "Dimension",
        "__metaData__": {"name": _pretty_component_name(did)},
    }


def _table_cell_display() -> dict[str, Any]:
    return {
        "conditionalFormattingOpts": {"autoGenerate": True, "usePercentLimits": False},
        "location": "behindNumber",
        "type": {
            "anomaly": True,
            "background": True,
            "backgroundType": "bar",
            "comparison": "none",
            "interpretZeroAsNoValue": False,
            "number": True,
            "percent": True,
            "showGrandTotal": True,
            "showSparklines": True,
            "showTotals": True,
            "wrapHeaderText": True,
        },
    }


def _data_settings() -> dict[str, Any]:
    return {
        "advancedItemLimit": 5,
        "advancedItemSearch": {"operator": "AND", "rules": []},
    }


def _build_freeform_subpanel(
    *,
    rsid: str,
    metrics: list[str],
    dimension: str | None,
    name: str,
    index: int,
) -> dict[str, Any]:
    metric_ids = [_normalize_metric_id(m) for m in metrics if str(m).strip()] or ["metrics/visits"]
    tree_id = f"{_short_id()}-{index}"
    column_nodes: list[dict[str, Any]] = []
    static_rows: list[dict[str, Any]] = []

    if dimension:
        # Metrics as columns, dimension as the table row component.
        for i, mid in enumerate(metric_ids):
            node_id = f"{tree_id}-{i}"
            column_nodes.append(
                {
                    "_computedValues": [],
                    "component": _metric_entity(mid),
                    "dataSettings": _data_settings(),
                    "id": node_id,
                    "name": _pretty_component_name(mid),
                    "nodes": [],
                    "selectionCoordinates": [],
                    "tableCellDisplay": _table_cell_display(),
                }
            )
        advanced_rows = [_dimension_entity(dimension)]
        sort_column = column_nodes[0]["id"] if column_nodes else f"{tree_id}-0"
    else:
        # Official Adobe example: All Visits as the column, metrics as static rows.
        column_nodes.append(
            {
                "_computedValues": [],
                "component": {
                    "id": "All_Visits",
                    "__entity__": True,
                    "type": "Segment",
                    "__metaData__": {"name": "AllVisits"},
                },
                "dataSettings": _data_settings(),
                "id": f"{tree_id}-col",
                "name": "AllVisits",
                "nodes": [],
                "selectionCoordinates": [],
                "tableCellDisplay": _table_cell_display(),
            }
        )
        for mid in metric_ids:
            static_rows.append(
                {
                    "component": _metric_entity(mid),
                    "dataSettings": _data_settings(),
                    "id": f"{tree_id}-{_short_id()}",
                }
            )
        advanced_rows = []
        sort_column = column_nodes[0]["id"]

    return {
        "collapsed": False,
        "description": "",
        "id": _new_id(),
        "isQuickInsightsSubPanel": False,
        "linkedSourceId": "",
        "position": {"autoHeight": 222, "autoSize": True, "width": 100, "x": 0, "y": 0},
        "reportlet": {
            "advancedMode": False,
            "advancedSettings": {"rows": advanced_rows, "tableState": "builder"},
            "columnTree": {
                "_computedValues": [],
                "dataSettings": _data_settings(),
                "id": f"{tree_id}-root",
                "name": "",
                "nodes": column_nodes,
                "selectionCoordinates": [],
                "tableCellDisplay": _table_cell_display(),
            },
            "freeformTable": {
                "alignDatesForTimeDimension": True,
                "attributionSettings": [],
                "breakdowns": [],
                "collapsed": False,
                "columnWidths": [100, 100],
                "pagination": {"currentPage": 0, "viewBy": 50},
                "search": {"operator": "AND", "rules": []},
                "selectionCoordinates": [],
                "settings": {
                    "breakdownByPosition": False,
                    "rowBasedPercentages": False,
                    "totalsType": "columnSum",
                },
                "sort": {"asc": False, "columnId": sort_column, "labelColumn": False},
                "staticRows": static_rows,
                "statistics": {"functions": [], "ignoreZeros": True},
            },
            "isConfigVisible": True,
            "type": "FreeformReportlet",
        },
        "swatchColor": "#00C0C7",
        "type": "genericSubPanel",
        "visible": True,
        "visualizationIndex": 1,
        "_name": name,
        "_rsid": rsid,
    }


def _build_panel(
    *,
    rsid: str,
    tables: list[dict[str, Any]],
    date_range: str | None,
) -> dict[str, Any]:
    sub_panels = []
    for i, table in enumerate(tables):
        metrics = table.get("metrics") or table.get("metric") or ["metrics/visits"]
        if isinstance(metrics, str):
            metrics = [metrics]
        dimension = table.get("dimension") or table.get("row_dimension")
        name = str(table.get("name") or f"Freeform {i + 1}")
        sub = _build_freeform_subpanel(
            rsid=rsid,
            metrics=list(metrics),
            dimension=str(dimension) if dimension else None,
            name=name,
            index=i,
        )
        sub_panels.append(sub)

    first_name = tables[0].get("name") if tables else "Freeform"
    return {
        "annotations": [],
        "collapsed": False,
        "dateRange": _date_range_entity(date_range or (tables[0].get("date_range") if tables else None)),
        "description": "",
        "id": _new_id(),
        "name": first_name or "Freeform",
        "position": {"autoHeight": 374, "autoSize": True, "width": 100, "x": 0, "y": 0},
        "reportSuite": {
            "id": rsid,
            "__entity__": True,
            "type": "ReportSuite",
            "__metaData__": {"name": rsid, "rsid": rsid},
        },
        "segmentGroups": [],
        "subPanels": sub_panels,
        "type": "panel",
    }


def build_workspace_definition(
    rsid: str,
    tables: list[dict[str, Any]] | None = None,
    date_range: str | None = None,
) -> dict[str, Any]:
    """
    Build a Workspace definition matching Adobe's published example.

    ``tables`` is a compact list of ``{name?, metrics[], dimension?, date_range?}``.
    An empty/omitted tables list yields a single Visits / this-month freeform.
    """
    if not rsid:
        raise ValueError("rsid is required to build a Workspace definition.")
    normalized_tables = list(tables or [])
    if not normalized_tables:
        normalized_tables = [{"name": "Freeform", "metrics": ["metrics/visits"]}]
    panel = _build_panel(rsid=rsid, tables=normalized_tables, date_range=date_range)
    return {
        "additionalCuratedComponents": [],
        "colorScheme": dict(_DEFAULT_COLOR_SCHEME),
        "countRepeatInstances": True,
        "currentWorkspaceIndex": 0,
        "customColorSchemes": [],
        "isCurated": False,
        "version": _WORKSPACE_DEFINITION_VERSION,
        "viewDensity": "expanded",
        "workspaces": [{"id": _new_id(), "name": "", "panels": [panel]}],
    }


def coerce_project_definition(
    definition: dict[str, Any] | None,
    *,
    rsid: str,
    tables: list[dict[str, Any]] | None = None,
    date_range: str | None = None,
) -> dict[str, Any]:
    """
    Return a full Workspace definition.

    * Full definitions (version + workspaces) are returned as-is.
    * Compact specs (``tables`` / ``panels`` / missing workspaces) are expanded.
    * ``None`` builds the default Visits / this-month workspace.
    """
    if tables:
        compact_date = date_range
        if isinstance(definition, dict):
            compact_date = compact_date or definition.get("date_range")
        return build_workspace_definition(rsid, tables=tables, date_range=compact_date)

    if not isinstance(definition, dict) or not definition:
        return build_workspace_definition(rsid, date_range=date_range)

    if _is_full_workspace_definition(definition):
        return definition

    nested_tables = definition.get("tables") or definition.get("panels")
    if isinstance(nested_tables, list):
        return build_workspace_definition(
            rsid,
            tables=nested_tables,
            date_range=date_range or definition.get("date_range"),
        )

    metrics = definition.get("metrics")
    dimension = definition.get("dimension")
    if metrics or dimension:
        table = {
            "name": definition.get("name") or "Freeform",
            "metrics": metrics or ["metrics/visits"],
            "dimension": dimension,
        }
        return build_workspace_definition(
            rsid,
            tables=[table],
            date_range=date_range or definition.get("date_range"),
        )

    # Incomplete object (e.g. {"version": "31"}) — expand to a valid template
    # so create/update succeed instead of Adobe 400'ing.
    return build_workspace_definition(rsid, date_range=date_range or definition.get("date_range"))
