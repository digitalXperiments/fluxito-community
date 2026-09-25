"""
Amplitude Connector

Uses the Amplitude HTTP API v2 (Taxonomy, Event Segmentation, Cohort APIs).

Auth: API Key + Secret Key (credential-based, stored encrypted in DB).
All methods accept decrypted api_key + secret_key directly.

Layer 1 (Read): list_projects, get_events_list, get_event_properties, get_user_properties,
              query_events, get_active_users, get_retention, get_funnel, get_revenue, list_cohorts

Layer 2 (Audit): check_taxonomy_health, check_event_volume_anomalies

Layer 3 (Write): create_event_type, update_event_type, delete_event_type
"""

import base64
import logging
from typing import Any

import httpx

from app.connectors.errors import friendly_errors

logger = logging.getLogger(__name__)

_AMPLITUDE_BASE = "https://amplitude.com/api"


class AmplitudeConnector:
    """Interfaces with Amplitude using per-user API key + secret key."""

    async def _request(
        self,
        api_key: str,
        secret_key: str,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict:
        """
        Make an authenticated request to Amplitude API.
        Uses basic auth with api_key:secret_key.
        """
        try:
            # Basic auth: api_key:secret_key base64 encoded
            auth_string = base64.b64encode(f"{api_key}:{secret_key}".encode()).decode()
            headers = {
                "Authorization": f"Basic {auth_string}",
                "Content-Type": "application/json",
            }

            url = f"{_AMPLITUDE_BASE}{endpoint}"
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
                    return response.json()
                except Exception:
                    return {"success": response.status_code < 300, "body": response.text}

        except Exception as e:
            logger.error(f"Amplitude API request error: {e}")
            return {"error": True, "message": str(e)}

    # ------------------------------------------------------------------
    # Layer 1: Data Access
    # ------------------------------------------------------------------

    @friendly_errors("Amplitude")
    async def list_projects(self, api_key: str, secret_key: str) -> dict:
        """
        Validate the API credentials by checking project info.
        Amplitude doesn't have a direct projects endpoint, so we validate via taxonomy.
        """
        result = await self._request(api_key, secret_key, "GET", "/2/taxonomy/event")
        if result.get("error"):
            return result
        return {
            "valid": True,
            "message": "Amplitude API credentials are valid",
            "events_count": len(result.get("data", {}).get("events", [])),
        }

    @friendly_errors("Amplitude")
    async def get_events_list(self, api_key: str, secret_key: str) -> dict:
        """
        Fetch all tracked events via Taxonomy API.
        GET /api/2/taxonomy/event
        """
        result = await self._request(api_key, secret_key, "GET", "/2/taxonomy/event")
        if result.get("error"):
            return result

        events = result.get("data", {}).get("events", [])
        return {
            "events": [
                {
                    "event_type": e.get("event_type"),
                    "value": e.get("value"),
                    "description": e.get("description"),
                    "last_seen": e.get("last_seen"),
                }
                for e in events
            ],
            "total": len(events),
        }

    @friendly_errors("Amplitude")
    async def get_event_properties(self, api_key: str, secret_key: str, event_type: str) -> dict:
        """
        Get properties for a specific event type.
        GET /api/2/taxonomy/event/{event_type}/properties
        """
        endpoint = f"/2/taxonomy/event/{event_type}/properties"
        result = await self._request(api_key, secret_key, "GET", endpoint)
        if result.get("error"):
            return result

        properties = result.get("data", {}).get("properties", [])
        return {
            "event_type": event_type,
            "properties": [
                {
                    "property_name": p.get("property_name"),
                    "value": p.get("value"),
                    "description": p.get("description"),
                    "last_seen": p.get("last_seen"),
                }
                for p in properties
            ],
            "total": len(properties),
        }

    @friendly_errors("Amplitude")
    async def get_user_properties(self, api_key: str, secret_key: str) -> dict:
        """
        Get all user properties via Taxonomy API.
        GET /api/2/taxonomy/user-property
        """
        result = await self._request(api_key, secret_key, "GET", "/2/taxonomy/user-property")
        if result.get("error"):
            return result

        properties = result.get("data", {}).get("properties", [])
        return {
            "properties": [
                {
                    "property_name": p.get("property_name"),
                    "value": p.get("value"),
                    "description": p.get("description"),
                }
                for p in properties
            ],
            "total": len(properties),
        }

    @friendly_errors("Amplitude")
    async def query_events(
        self,
        api_key: str,
        secret_key: str,
        start_date: str,
        end_date: str,
        event_type: str,
        group_by: str | None = None,
        interval: int = 1,
    ) -> dict:
        """
        Query events via Event Segmentation API.
        POST /api/2/events/segmentation
        """
        json_body = {
            "data_source": {"event_source": "web"},
            "metrics": [{"name": "events"}],
            "group_by": [{"property_name": group_by}] if group_by else [],
            "filter_by": [
                {
                    "property_name": "event_type",
                    "op": "is",
                    "value": event_type,
                }
            ],
            "start": start_date,
            "end": end_date,
            "interval": interval,
        }

        result = await self._request(
            api_key, secret_key, "POST", "/2/events/segmentation", json_body=json_body
        )
        if result.get("error"):
            return result

        data = result.get("data", {})
        return {
            "event_type": event_type,
            "start_date": start_date,
            "end_date": end_date,
            "group_by": group_by,
            "series": data.get("series", []),
            "xaxis": data.get("xaxis", []),
        }

    @friendly_errors("Amplitude")
    async def get_active_users(
        self,
        api_key: str,
        secret_key: str,
        start_date: str,
        end_date: str,
        interval: int = 1,
    ) -> dict:
        """
        Get active/new users over time.
        GET /api/2/users/active?m=active|new|returning
        """
        params = {
            "start": start_date,
            "end": end_date,
            "m": "active",
            "interval": interval,
        }

        result = await self._request(api_key, secret_key, "GET", "/2/users/active", params=params)
        if result.get("error"):
            return result

        data = result.get("data", {})
        return {
            "metric": "active_users",
            "start_date": start_date,
            "end_date": end_date,
            "series": data.get("series", []),
            "xaxis": data.get("xaxis", []),
        }

    @friendly_errors("Amplitude")
    async def get_retention(
        self,
        api_key: str,
        secret_key: str,
        start_date: str,
        end_date: str,
    ) -> dict:
        """
        Get retention analysis.
        GET /api/2/retention
        """
        params = {
            "start": start_date,
            "end": end_date,
        }

        result = await self._request(api_key, secret_key, "GET", "/2/retention", params=params)
        if result.get("error"):
            return result

        return {
            "metric": "retention",
            "start_date": start_date,
            "end_date": end_date,
            "data": result.get("data", {}),
        }

    @friendly_errors("Amplitude")
    async def get_funnel(
        self,
        api_key: str,
        secret_key: str,
        start_date: str,
        end_date: str,
        events: list[str],
    ) -> dict:
        """
        Get funnel analysis for a list of events.
        POST /api/2/funnels
        """
        json_body = {
            "event_keys": events,
            "start": start_date,
            "end": end_date,
        }

        result = await self._request(api_key, secret_key, "POST", "/2/funnels", json_body=json_body)
        if result.get("error"):
            return result

        return {
            "metric": "funnel",
            "events": events,
            "start_date": start_date,
            "end_date": end_date,
            "data": result.get("data", {}),
        }

    @friendly_errors("Amplitude")
    async def get_revenue(
        self,
        api_key: str,
        secret_key: str,
        start_date: str,
        end_date: str,
    ) -> dict:
        """
        Get revenue/LTV analysis.
        GET /api/2/revenue/ltv
        """
        params = {
            "start": start_date,
            "end": end_date,
        }

        result = await self._request(api_key, secret_key, "GET", "/2/revenue/ltv", params=params)
        if result.get("error"):
            return result

        return {
            "metric": "revenue_ltv",
            "start_date": start_date,
            "end_date": end_date,
            "data": result.get("data", {}),
        }

    @friendly_errors("Amplitude")
    async def list_cohorts(self, api_key: str, secret_key: str) -> dict:
        """
        List all cohorts via Cohort API.
        GET /api/3/cohorts
        """
        result = await self._request(api_key, secret_key, "GET", "/3/cohorts")
        if result.get("error"):
            return result

        cohorts = result.get("data", [])
        return {
            "cohorts": [
                {
                    "id": c.get("id"),
                    "name": c.get("name"),
                    "description": c.get("description"),
                    "size": c.get("size"),
                    "created_at": c.get("created_at"),
                    "archived": c.get("archived"),
                }
                for c in cohorts
            ],
            "total": len(cohorts),
        }

    # ------------------------------------------------------------------
    # Layer 2: Audit
    # ------------------------------------------------------------------

    @friendly_errors("Amplitude")
    async def check_taxonomy_health(self, api_key: str, secret_key: str) -> dict:
        """
        Check event and property naming for issues:
        - Events with spaces, uppercase, special characters
        - Duplicate event names (case-insensitive)
        - Unused properties
        """
        events_result = await self.get_events_list(api_key, secret_key)
        if events_result.get("error"):
            return events_result

        issues = []
        events = events_result.get("events", [])
        event_names = [e["event_type"] for e in events]

        for event in events:
            name = event.get("event_type", "")
            # Check for spaces
            if " " in name:
                issues.append(f"Event '{name}' contains spaces (consider snake_case)")
            # Check for uppercase
            if name != name.lower():
                issues.append(f"Event '{name}' contains uppercase (consider lowercase)")
            # Check for special characters
            if not name.replace("_", "").isalnum():
                issues.append(f"Event '{name}' contains special characters")

        # Check for near-duplicates (case-insensitive)
        seen = {}
        for name in event_names:
            lower = name.lower()
            if lower in seen and seen[lower] != name:
                issues.append(f"Potential duplicate: '{seen[lower]}' vs '{name}'")
            seen[lower] = name

        return {
            "event_count": len(events),
            "issues": issues,
            "health_score": max(0, 100 - len(issues) * 5),  # Rough scoring
        }

    @friendly_errors("Amplitude")
    async def check_event_volume_anomalies(self, api_key: str, secret_key: str, days_back: int = 30) -> dict:
        """Compare recent event volumes to a historical baseline using z-scores.

        Fetches daily event counts for the last days_back*2 days, splits into
        a baseline (older half) and recent (newer half), computes per-day z-scores
        against the baseline mean+std, and flags days where |z| > 2.
        """
        from datetime import datetime, timedelta

        end = datetime.utcnow().date()
        start = end - timedelta(days=days_back * 2)
        start_str = start.isoformat()
        end_str = end.isoformat()

        json_body = {
            "metrics": [{"name": "events"}],
            "start": start_str,
            "end": end_str,
            "interval": 1,
        }
        result = await self._request(
            api_key, secret_key, "POST", "/2/events/segmentation", json_body=json_body
        )
        if result.get("error"):
            return {"error": True, "message": result.get("message", "Amplitude error")}

        data = result.get("data", {})
        series = data.get("series", [])
        xaxis = data.get("xaxis", [])

        # Amplitude returns series as [[val1, val2, ...]] or [val1, val2, ...]
        if series and isinstance(series[0], list):
            volumes = [int(v or 0) for v in series[0]]
        else:
            volumes = [int(v or 0) for v in series]

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
        recent_dates = xaxis[half:] if len(xaxis) >= len(volumes) else []

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
                date_str = recent_dates[i] if i < len(recent_dates) else f"day_{half + i}"
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

    @friendly_errors("Amplitude")
    async def create_event_type(
        self,
        api_key: str,
        secret_key: str,
        event_type: str,
        description: str | None = None,
        category: str | None = None,
    ) -> dict:
        """
        Create a new event type.
        POST /api/2/taxonomy/event
        """
        json_body = {
            "event_type": event_type,
        }
        if description:
            json_body["description"] = description
        if category:
            json_body["category"] = category

        result = await self._request(api_key, secret_key, "POST", "/2/taxonomy/event", json_body=json_body)
        if result.get("error"):
            return result

        return {
            "success": True,
            "event_type": event_type,
            "message": "Event type created successfully",
        }

    @friendly_errors("Amplitude")
    async def update_event_type(
        self,
        api_key: str,
        secret_key: str,
        event_type: str,
        new_name: str | None = None,
        description: str | None = None,
        category: str | None = None,
    ) -> dict:
        """
        Update an event type.
        PUT /api/2/taxonomy/event/{event_type}
        """
        json_body = {}
        if new_name:
            json_body["event_type"] = new_name
        if description:
            json_body["description"] = description
        if category:
            json_body["category"] = category

        endpoint = f"/2/taxonomy/event/{event_type}"
        result = await self._request(api_key, secret_key, "PUT", endpoint, json_body=json_body)
        if result.get("error"):
            return result

        return {
            "success": True,
            "event_type": event_type,
            "message": "Event type updated successfully",
        }

    @friendly_errors("Amplitude")
    async def delete_event_type(self, api_key: str, secret_key: str, event_type: str) -> dict:
        """
        Delete an event type.
        DELETE /api/2/taxonomy/event/{event_type}
        """
        endpoint = f"/2/taxonomy/event/{event_type}"
        result = await self._request(api_key, secret_key, "DELETE", endpoint)
        if result.get("error"):
            return result

        return {
            "success": True,
            "event_type": event_type,
            "message": "Event type deleted successfully",
        }
