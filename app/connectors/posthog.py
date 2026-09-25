"""
PostHog Connector

Uses the PostHog REST API (open-source product analytics, self-hostable).

Auth: Personal/Project API Key via Bearer token.
Base URL: configurable per-connection (Cloud: https://app.posthog.com or https://eu.posthog.com;
self-hosted: any https://posthog.example.com). Stored as plain `project_host` (like Marketo instance_url).
API root: <host>/api/
All endpoints require a project_id in the path: /api/projects/<project_id>/...

Because PostHog needs project_host + project_id + api_key, its model and credential resolver
differ from Amplitude. Connector methods take (api_key, project_host, project_id) instead of
(api_key, secret_key).

Layer 1 (Read): list_projects, get_events_list, get_event_properties, get_user_properties,
              query_events, get_active_users, get_retention, get_funnel, get_revenue, list_cohorts

Layer 2 (Audit): check_taxonomy_health, check_event_volume_anomalies

Layer 3 (Write): create_event_type, update_event_type, delete_event_type
"""

import logging
from typing import Any

import httpx

from app.connectors.errors import friendly_errors

logger = logging.getLogger(__name__)


class PostHogConnector:
    """Interfaces with PostHog using per-connection API key + project_host + project_id."""

    async def _request(
        self,
        api_key: str,
        project_host: str,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict:
        """
        Make an authenticated request to PostHog API.
        Uses Bearer token auth with the API key.
        Endpoint is the path after /api/ (e.g. "/projects/1/events/").
        """
        try:
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }

            base = project_host.rstrip("/")
            url = f"{base}/api{endpoint}"
            async with httpx.AsyncClient(timeout=30.0) as client:
                if method == "GET":
                    response = await client.get(url, headers=headers, params=params)
                elif method == "POST":
                    response = await client.post(url, headers=headers, params=params, json=json_body)
                elif method == "PATCH":
                    response = await client.patch(url, headers=headers, params=params, json=json_body)
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
                    return response.json()
                except Exception:
                    return {"success": response.status_code < 300, "body": response.text}

        except Exception as e:
            logger.error(f"PostHog API request error: {e}")
            return {"error": True, "message": str(e)}

    # ------------------------------------------------------------------
    # Layer 1: Data Access
    # ------------------------------------------------------------------

    @friendly_errors("PostHog")
    async def list_projects(self, api_key: str, project_host: str) -> dict:
        """
        List all projects the API key has access to.
        GET /api/projects/
        """
        result = await self._request(api_key, project_host, "GET", "/projects/")
        if result.get("error"):
            return result
        projects = result.get("results", result) if isinstance(result, dict) else result
        return {
            "valid": True,
            "projects": projects,
            "message": "PostHog API credentials are valid",
        }

    @friendly_errors("PostHog")
    async def get_events_list(self, api_key: str, project_host: str, project_id: int | str) -> dict:
        """
        Fetch event definitions for a project.
        GET /api/projects/<project_id>/events/
        """
        endpoint = f"/projects/{project_id}/events/"
        result = await self._request(api_key, project_host, "GET", endpoint)
        if result.get("error"):
            return result

        events = result.get("results", result) if isinstance(result, dict) else result
        return {
            "events": [
                {
                    "event_type": e.get("name") or e.get("event"),
                    "description": e.get("description"),
                    "last_seen": e.get("last_seen_at") or e.get("last_seen"),
                }
                for e in (events if isinstance(events, list) else [])
            ],
            "total": len(events) if isinstance(events, list) else result.get("count", 0),
        }

    @friendly_errors("PostHog")
    async def get_event_properties(
        self, api_key: str, project_host: str, project_id: int | str, event_name: str
    ) -> dict:
        """
        Get properties for a specific event.
        GET /api/projects/<project_id>/properties/?event=<event_name>
        """
        params = {"event": event_name}
        endpoint = f"/projects/{project_id}/properties/"
        result = await self._request(api_key, project_host, "GET", endpoint, params=params)
        if result.get("error"):
            return result

        props = result.get("results", result) if isinstance(result, dict) else result
        return {
            "event_type": event_name,
            "properties": [
                {
                    "property_name": p.get("name") or p.get("property"),
                    "description": p.get("description"),
                }
                for p in (props if isinstance(props, list) else [])
            ],
            "total": len(props) if isinstance(props, list) else result.get("count", 0),
        }

    @friendly_errors("PostHog")
    async def get_user_properties(self, api_key: str, project_host: str, project_id: int | str) -> dict:
        """
        Get person/user properties for a project.
        GET /api/projects/<project_id>/person_properties/
        """
        endpoint = f"/projects/{project_id}/person_properties/"
        result = await self._request(api_key, project_host, "GET", endpoint)
        if result.get("error"):
            return result

        props = result.get("results", result) if isinstance(result, dict) else result
        return {
            "properties": [
                {
                    "property_name": p.get("name") or p.get("property"),
                    "description": p.get("description"),
                }
                for p in (props if isinstance(props, list) else [])
            ],
            "total": len(props) if isinstance(props, list) else result.get("count", 0),
        }

    @friendly_errors("PostHog")
    async def query_events(
        self,
        api_key: str,
        project_host: str,
        project_id: int | str,
        start_date: str,
        end_date: str,
        event_name: str,
    ) -> dict:
        """
        Query raw events for a project and event type.
        GET /api/projects/<project_id>/events/?event=<name>&after=<start>&before=<end>
        """
        params = {
            "event": event_name,
            "after": start_date,
            "before": end_date,
        }
        endpoint = f"/projects/{project_id}/events/"
        result = await self._request(api_key, project_host, "GET", endpoint, params=params)
        if result.get("error"):
            return result

        events = result.get("results", result) if isinstance(result, dict) else result
        return {
            "event_type": event_name,
            "start_date": start_date,
            "end_date": end_date,
            "events": events if isinstance(events, list) else [],
            "total": len(events) if isinstance(events, list) else result.get("count", 0),
        }

    @friendly_errors("PostHog")
    async def get_active_users(
        self,
        api_key: str,
        project_host: str,
        project_id: int | str,
        start_date: str,
        end_date: str,
    ) -> dict:
        """
        Get daily active users via Trends insight.
        GET /api/projects/<project_id>/insights/trend/?events=[{"id":"$pageview"}]&date_from=<start>&date_to=<end>
        """
        import json

        events_param = json.dumps([{"id": "$pageview"}])
        params = {
            "events": events_param,
            "date_from": start_date,
            "date_to": end_date,
        }
        endpoint = f"/projects/{project_id}/insights/trend/"
        result = await self._request(api_key, project_host, "GET", endpoint, params=params)
        if result.get("error"):
            return result

        return {
            "metric": "active_users",
            "start_date": start_date,
            "end_date": end_date,
            "data": result,
        }

    @friendly_errors("PostHog")
    async def get_retention(
        self,
        api_key: str,
        project_host: str,
        project_id: int | str,
        start_date: str,
        end_date: str,
    ) -> dict:
        """
        Get retention analysis via Retention insight.
        GET /api/projects/<project_id>/insights/retention/?date_from=<start>&date_to=<end>
        """
        params = {
            "date_from": start_date,
            "date_to": end_date,
        }
        endpoint = f"/projects/{project_id}/insights/retention/"
        result = await self._request(api_key, project_host, "GET", endpoint, params=params)
        if result.get("error"):
            return result

        return {
            "metric": "retention",
            "start_date": start_date,
            "end_date": end_date,
            "data": result,
        }

    @friendly_errors("PostHog")
    async def get_funnel(
        self,
        api_key: str,
        project_host: str,
        project_id: int | str,
        start_date: str,
        end_date: str,
        events: list[str],
    ) -> dict:
        """
        Get funnel analysis via Funnel insight.
        GET /api/projects/<project_id>/insights/funnel/?events=[...]&date_from=<start>&date_to=<end>
        """
        import json

        events_param = json.dumps([{"id": e} for e in events])
        params = {
            "events": events_param,
            "date_from": start_date,
            "date_to": end_date,
        }
        endpoint = f"/projects/{project_id}/insights/funnel/"
        result = await self._request(api_key, project_host, "GET", endpoint, params=params)
        if result.get("error"):
            return result

        return {
            "metric": "funnel",
            "events": events,
            "start_date": start_date,
            "end_date": end_date,
            "data": result,
        }

    @friendly_errors("PostHog")
    async def get_revenue(
        self,
        api_key: str,
        project_host: str,
        project_id: int | str,
        start_date: str,
        end_date: str,
    ) -> dict:
        """
        Query revenue via custom events.

        PostHog does not have a native revenue/LTV endpoint like Amplitude.
        Revenue is tracked via custom events (e.g. "purchase") with a revenue property.
        This queries the events API for a 'purchase' event and returns raw matches;
        downstream code can sum a configured revenue property.
        """
        params = {
            "event": "purchase",
            "after": start_date,
            "before": end_date,
        }
        endpoint = f"/projects/{project_id}/events/"
        result = await self._request(api_key, project_host, "GET", endpoint, params=params)
        if result.get("error"):
            return result

        events = result.get("results", result) if isinstance(result, dict) else result
        return {
            "metric": "revenue",
            "start_date": start_date,
            "end_date": end_date,
            "note": "PostHog revenue is tracked via custom events; summed from 'purchase' event results.",
            "events": events if isinstance(events, list) else [],
            "total": len(events) if isinstance(events, list) else result.get("count", 0),
        }

    @friendly_errors("PostHog")
    async def list_cohorts(self, api_key: str, project_host: str, project_id: int | str) -> dict:
        """
        List all cohorts for a project.
        GET /api/projects/<project_id>/cohorts/
        """
        endpoint = f"/projects/{project_id}/cohorts/"
        result = await self._request(api_key, project_host, "GET", endpoint)
        if result.get("error"):
            return result

        cohorts = result.get("results", result) if isinstance(result, dict) else result
        return {
            "cohorts": [
                {
                    "id": c.get("id"),
                    "name": c.get("name"),
                    "description": c.get("description"),
                    "size": c.get("count") or c.get("size"),
                    "created_at": c.get("created_at"),
                    "archived": c.get("is_static") is False if "is_static" in c else c.get("archived"),
                }
                for c in (cohorts if isinstance(cohorts, list) else [])
            ],
            "total": len(cohorts) if isinstance(cohorts, list) else result.get("count", 0),
        }

    # ------------------------------------------------------------------
    # Layer 2: Audit
    # ------------------------------------------------------------------

    @friendly_errors("PostHog")
    async def check_taxonomy_health(self, api_key: str, project_host: str, project_id: int | str) -> dict:
        """
        Check event and property naming for issues:
        - Events with spaces, uppercase, special characters
        - Duplicate event names (case-insensitive)
        - Unused properties
        """
        events_result = await self.get_events_list(api_key, project_host, project_id)
        if events_result.get("error"):
            return events_result

        issues = []
        events = events_result.get("events", [])
        event_names = [e.get("event_type", "") for e in events]

        for event in events:
            name = event.get("event_type", "")
            if not name:
                continue
            # Check for spaces
            if " " in name:
                issues.append(f"Event '{name}' contains spaces (consider snake_case)")
            # Check for uppercase
            if name != name.lower():
                issues.append(f"Event '{name}' contains uppercase (consider lowercase)")
            # Check for special characters (allow $ prefix used by PostHog built-ins)
            if not name.lstrip("$").replace("_", "").isalnum():
                issues.append(f"Event '{name}' contains special characters")

        # Check for near-duplicates (case-insensitive)
        seen = {}
        for name in event_names:
            if not name:
                continue
            lower = name.lower()
            if lower in seen and seen[lower] != name:
                issues.append(f"Potential duplicate: '{seen[lower]}' vs '{name}'")
            seen[lower] = name

        return {
            "event_count": len(events),
            "issues": issues,
            "health_score": max(0, 100 - len(issues) * 5),
        }

    @friendly_errors("PostHog")
    async def check_event_volume_anomalies(
        self, api_key: str, project_host: str, project_id: int | str, days_back: int = 30
    ) -> dict:
        """Compare recent event volumes to a historical baseline using z-scores.

        Fetches daily event counts via the trend insight for the last days_back*2 days,
        splits into a baseline (older half) and recent (newer half), computes per-day
        z-scores against the baseline mean+std, and flags days where |z| > 2.
        """
        from datetime import datetime, timedelta

        end = datetime.utcnow().date()
        start = end - timedelta(days=days_back * 2)
        start_str = start.isoformat()
        end_str = end.isoformat()

        import json

        events_param = json.dumps([{"id": "$pageview"}])
        params = {
            "events": events_param,
            "date_from": start_str,
            "date_to": end_str,
            "interval": 1,
        }
        endpoint = f"/projects/{project_id}/insights/trend/"
        result = await self._request(api_key, project_host, "GET", endpoint, params=params)
        if result.get("error"):
            return {"error": True, "message": result.get("message", "PostHog error")}

        # PostHog trend response shape varies; attempt to extract a numeric series
        data = result.get("result", result) if isinstance(result, dict) else result
        # Try common shapes: list of dicts with 'data' arrays, or direct series
        series = []
        if isinstance(data, list) and data and isinstance(data[0], dict):
            # e.g. [{"data": [..], ...}]
            series = data[0].get("data", [])
        elif isinstance(data, dict):
            series = data.get("series", data.get("data", []))

        volumes = [int(v or 0) for v in (series if isinstance(series, list) else [])]

        if len(volumes) < 4:
            return {
                "metric": "event_volume_anomalies",
                "days_back": days_back,
                "baseline_mean": 0.0,
                "baseline_std": 0.0,
                "anomalies": [],
                "anomaly_count": 0,
                "health_score": 100,
                "note": "Insufficient data for anomaly detection.",
            }

        half = len(volumes) // 2
        baseline = volumes[:half]
        recent = volumes[half:]

        import statistics

        try:
            baseline_mean = statistics.mean(baseline)
            baseline_std = statistics.pstdev(baseline) if len(baseline) > 1 else 0.0
        except Exception:
            baseline_mean = sum(baseline) / len(baseline) if baseline else 0.0
            baseline_std = 0.0

        if baseline_std == 0:
            return {
                "metric": "event_volume_anomalies",
                "days_back": days_back,
                "baseline_mean": round(baseline_mean, 2),
                "baseline_std": 0.0,
                "anomalies": [],
                "anomaly_count": 0,
                "health_score": 100,
                "note": "Baseline has zero variance; cannot compute z-scores.",
            }

        anomalies = []
        for i, val in enumerate(recent):
            z = (val - baseline_mean) / baseline_std
            if abs(z) > 2.0:
                direction = "spike" if z > 0 else "drop"
                date_str = f"day_{half + i}"
                anomalies.append(
                    {
                        "date": date_str,
                        "volume": val,
                        "z_score": round(z, 3),
                        "direction": direction,
                    }
                )

        health = max(0, 100 - len(anomalies) * 10)
        return {
            "metric": "event_volume_anomalies",
            "days_back": days_back,
            "baseline_mean": round(baseline_mean, 2),
            "baseline_std": round(baseline_std, 2),
            "anomalies": anomalies,
            "anomaly_count": len(anomalies),
            "health_score": health,
            "note": f"Compared last {len(recent)} days against prior {len(baseline)} days baseline.",
        }

    # ------------------------------------------------------------------
    # Layer 3: Write Operations
    # ------------------------------------------------------------------

    @friendly_errors("PostHog")
    async def create_event_type(
        self,
        api_key: str,
        project_host: str,
        project_id: int | str,
        event_type: str,
        description: str | None = None,
        category: str | None = None,
    ) -> dict:
        """
        Create a new event definition.
        POST /api/projects/<project_id>/event_definitions/
        """
        json_body: dict[str, Any] = {"name": event_type}
        if description:
            json_body["description"] = description
        if category:
            json_body["category"] = category

        endpoint = f"/projects/{project_id}/event_definitions/"
        result = await self._request(api_key, project_host, "POST", endpoint, json_body=json_body)
        if result.get("error"):
            return result

        return {
            "success": True,
            "event_type": event_type,
            "message": "Event type created successfully",
        }

    @friendly_errors("PostHog")
    async def update_event_type(
        self,
        api_key: str,
        project_host: str,
        project_id: int | str,
        event_type: str,
        new_name: str | None = None,
        description: str | None = None,
        category: str | None = None,
    ) -> dict:
        """
        Update an event definition.
        PATCH /api/projects/<project_id>/event_definitions/<event_type>/
        """
        json_body: dict[str, Any] = {}
        if new_name:
            json_body["name"] = new_name
        if description:
            json_body["description"] = description
        if category:
            json_body["category"] = category

        endpoint = f"/projects/{project_id}/event_definitions/{event_type}/"
        result = await self._request(api_key, project_host, "PATCH", endpoint, json_body=json_body)
        if result.get("error"):
            return result

        return {
            "success": True,
            "event_type": event_type,
            "message": "Event type updated successfully",
        }

    @friendly_errors("PostHog")
    async def delete_event_type(
        self, api_key: str, project_host: str, project_id: int | str, event_type: str
    ) -> dict:
        """
        Delete an event definition.
        DELETE /api/projects/<project_id>/event_definitions/<event_type>/
        """
        endpoint = f"/projects/{project_id}/event_definitions/{event_type}/"
        result = await self._request(api_key, project_host, "DELETE", endpoint)
        if result.get("error"):
            return result

        return {
            "success": True,
            "event_type": event_type,
            "message": "Event type deleted successfully",
        }
