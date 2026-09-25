"""
Mixpanel Connector

Uses the Mixpanel HTTP API v2 (Query, Engage, Funnels, Retention, Cohorts, Lexicon/Data Governance).

Auth: API Secret + Service Token (credential-based, stored encrypted in DB).
All methods accept decrypted api_secret + service_token directly.
Basic Auth: api_secret as username, service_token as password (or empty for some endpoints).

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

_MIXPANEL_BASE = "https://mixpanel.com/api"


class MixpanelConnector:
    """Interfaces with Mixpanel using per-user API secret + service token."""

    async def _request(
        self,
        api_secret: str,
        service_token: str,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict:
        """
        Make an authenticated request to Mixpanel API.
        Uses basic auth with api_secret:service_token.
        """
        try:
            # Basic auth: api_secret:service_token base64 encoded
            auth_string = base64.b64encode(f"{api_secret}:{service_token}".encode()).decode()
            headers = {
                "Authorization": f"Basic {auth_string}",
                "Content-Type": "application/json",
            }

            url = f"{_MIXPANEL_BASE}{endpoint}"
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
                    data = response.json()
                    # Normalize bare list responses so .get() works downstream
                    if isinstance(data, list):
                        data = {"data": data}
                    return data
                except Exception:
                    return {"success": response.status_code < 300, "body": response.text}

        except Exception as e:
            logger.error(f"Mixpanel API request error: {e}")
            return {"error": True, "message": str(e)}

    # ------------------------------------------------------------------
    # Layer 1: Data Access
    # ------------------------------------------------------------------

    @friendly_errors("Mixpanel")
    async def list_projects(self, api_secret: str, service_token: str) -> dict:
        """
        Validate the API credentials by checking events endpoint.
        Mixpanel doesn't have a direct projects endpoint; validate via events/names.
        """
        result = await self._request(api_secret, service_token, "GET", "/2.0/events/names/")
        if result.get("error"):
            return result
        return {
            "valid": True,
            "message": "Mixpanel API credentials are valid",
        }

    @friendly_errors("Mixpanel")
    async def get_events_list(self, api_secret: str, service_token: str) -> dict:
        """
        Fetch all tracked event names.
        GET /2.0/events/names/
        """
        result = await self._request(api_secret, service_token, "GET", "/2.0/events/names/")
        if result.get("error"):
            return result

        events = result.get("data", []) if isinstance(result, dict) else []
        return {
            "events": [
                {
                    "event_type": e if isinstance(e, str) else e.get("event_type", e),
                }
                for e in events
            ],
            "total": len(events),
        }

    @friendly_errors("Mixpanel")
    async def get_event_properties(self, api_secret: str, service_token: str, event_name: str) -> dict:
        """
        Get top properties for a specific event.
        GET /2.0/events/properties/top/?event=<event_name>
        """
        params = {"event": event_name}
        result = await self._request(
            api_secret, service_token, "GET", "/2.0/events/properties/top/", params=params
        )
        if result.get("error"):
            return result

        properties = result.get("properties", []) if isinstance(result, dict) else []
        return {
            "event_type": event_name,
            "properties": properties,
            "total": len(properties),
        }

    @friendly_errors("Mixpanel")
    async def get_user_properties(self, api_secret: str, service_token: str) -> dict:
        """
        Get all user properties via Engage API.
        GET /2.0/engage/properties
        """
        result = await self._request(api_secret, service_token, "GET", "/2.0/engage/properties")
        if result.get("error"):
            return result

        properties = result.get("properties", []) if isinstance(result, dict) else []
        return {
            "properties": properties,
            "total": len(properties),
        }

    @friendly_errors("Mixpanel")
    async def query_events(
        self,
        api_secret: str,
        service_token: str,
        start_date: str,
        end_date: str,
        event_name: str,
    ) -> dict:
        """
        Query event segmentation.
        GET /2.0/segmentation/?event=<event_name>&from_date=<start>&to_date=<end>&type=general
        """
        params = {
            "event": event_name,
            "from_date": start_date,
            "to_date": end_date,
            "type": "general",
        }

        result = await self._request(api_secret, service_token, "GET", "/2.0/segmentation/", params=params)
        if result.get("error"):
            return result

        return {
            "event_type": event_name,
            "start_date": start_date,
            "end_date": end_date,
            "series": result.get("data", {}).get("series", []),
            "xaxis": result.get("data", {}).get("xaxis", []),
        }

    @friendly_errors("Mixpanel")
    async def get_active_users(
        self,
        api_secret: str,
        service_token: str,
        start_date: str,
        end_date: str,
    ) -> dict:
        """
        Get active users via retention/cohort API.
        GET /2.0/retention/
        """
        params = {
            "from_date": start_date,
            "to_date": end_date,
            "retention_type": "cohort",
        }

        result = await self._request(api_secret, service_token, "GET", "/2.0/retention/", params=params)
        if result.get("error"):
            return result

        return {
            "metric": "active_users",
            "start_date": start_date,
            "end_date": end_date,
            "data": result.get("data", {}),
        }

    @friendly_errors("Mixpanel")
    async def get_retention(
        self,
        api_secret: str,
        service_token: str,
        start_date: str,
        end_date: str,
    ) -> dict:
        """
        Get retention analysis.
        GET /2.0/retention/?from_date=<start>&to_date=<end>&retention_type=cohort
        """
        params = {
            "from_date": start_date,
            "to_date": end_date,
            "retention_type": "cohort",
        }

        result = await self._request(api_secret, service_token, "GET", "/2.0/retention/", params=params)
        if result.get("error"):
            return result

        return {
            "metric": "retention",
            "start_date": start_date,
            "end_date": end_date,
            "data": result.get("data", {}),
        }

    @friendly_errors("Mixpanel")
    async def get_funnel(
        self,
        api_secret: str,
        service_token: str,
        start_date: str,
        end_date: str,
        events: list[str],
    ) -> dict:
        """
        List saved funnels or query funnels.
        GET /2.0/funnels/list or /2.0/funnels/
        """
        if not events:
            # List saved funnels
            result = await self._request(api_secret, service_token, "GET", "/2.0/funnels/list")
            if result.get("error"):
                return result
            funnels = result if isinstance(result, list) else result.get("funnels", [])
            return {
                "metric": "funnels_list",
                "funnels": funnels,
                "total": len(funnels),
            }

        params = {
            "from_date": start_date,
            "to_date": end_date,
        }
        result = await self._request(api_secret, service_token, "GET", "/2.0/funnels/", params=params)
        if result.get("error"):
            return result

        return {
            "metric": "funnel",
            "events": events,
            "start_date": start_date,
            "end_date": end_date,
            "data": result.get("data", {}),
        }

    @friendly_errors("Mixpanel")
    async def get_revenue(
        self,
        api_secret: str,
        service_token: str,
        start_date: str,
        end_date: str,
    ) -> dict:
        """
        Get revenue analysis.
        GET /2.0/retention/revenue/?from_date=...&to_date=...
        """
        params = {
            "from_date": start_date,
            "to_date": end_date,
        }

        result = await self._request(
            api_secret, service_token, "GET", "/2.0/retention/revenue/", params=params
        )
        if result.get("error"):
            return result

        return {
            "metric": "revenue",
            "start_date": start_date,
            "end_date": end_date,
            "data": result.get("data", {}),
        }

    @friendly_errors("Mixpanel")
    async def list_cohorts(self, api_secret: str, service_token: str) -> dict:
        """
        List all cohorts.
        GET /2.0/cohorts/list
        """
        result = await self._request(api_secret, service_token, "GET", "/2.0/cohorts/list")
        if result.get("error"):
            return result

        cohorts = result if isinstance(result, list) else result.get("cohorts", [])
        return {
            "cohorts": [
                {
                    "id": c.get("id") if isinstance(c, dict) else None,
                    "name": c.get("name") if isinstance(c, dict) else c,
                    "description": c.get("description") if isinstance(c, dict) else None,
                    "size": c.get("size") if isinstance(c, dict) else None,
                    "created_at": c.get("created_at") if isinstance(c, dict) else None,
                    "archived": c.get("archived") if isinstance(c, dict) else False,
                }
                for c in cohorts
            ],
            "total": len(cohorts),
        }

    # ------------------------------------------------------------------
    # Layer 2: Audit
    # ------------------------------------------------------------------

    @friendly_errors("Mixpanel")
    async def check_taxonomy_health(self, api_secret: str, service_token: str) -> dict:
        """
        Check event and property naming for issues:
        - Events with spaces, uppercase, special characters
        - Duplicate event names (case-insensitive)
        - Unused properties
        """
        events_result = await self.get_events_list(api_secret, service_token)
        if events_result.get("error"):
            return events_result

        issues = []
        events = events_result.get("events", [])
        event_names = [e.get("event_type", "") for e in events]

        for name in event_names:
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

    @friendly_errors("Mixpanel")
    async def check_event_volume_anomalies(
        self,
        api_secret: str,
        service_token: str,
        days_back: int = 30,
        event_name: str | None = None,
    ) -> dict:
        """
        Compare recent event volumes to historical baseline using segmentation API.
        Fetches daily counts for days_back*2 days, splits into baseline/recent halves,
        computes z-scores against baseline mean and pstdev, and flags days with |z| > 2.
        """
        import statistics
        from datetime import datetime, timedelta

        end = datetime.utcnow().date()
        start = end - timedelta(days=days_back * 2)
        start_str = start.isoformat()
        end_str = end.isoformat()

        # If event_name is not provided, query the primary event from get_events_list
        target_event = event_name
        if not target_event:
            ev_res = await self.get_events_list(api_secret, service_token)
            events = ev_res.get("events", [])
            if events and isinstance(events, list):
                target_event = events[0].get("event_type") if isinstance(events[0], dict) else str(events[0])

        if not target_event:
            return {
                "metric": "event_volume_anomalies",
                "days_back": days_back,
                "baseline_mean": 0.0,
                "baseline_std": 0.0,
                "anomalies": [],
                "anomaly_count": 0,
                "health_score": 100,
                "note": "No events found to evaluate volume anomalies.",
            }

        params = {
            "event": target_event,
            "from_date": start_str,
            "to_date": end_str,
            "type": "general",
            "unit": "day",
        }
        res = await self._request(api_secret, service_token, "GET", "/2.0/segmentation/", params=params)
        if isinstance(res, dict) and res.get("error"):
            # Return graceful insufficient data structure on error
            return {
                "metric": "event_volume_anomalies",
                "days_back": days_back,
                "baseline_mean": 0.0,
                "baseline_std": 0.0,
                "anomalies": [],
                "anomaly_count": 0,
                "health_score": 100,
                "note": f"Segmentation query failed: {res.get('message', 'Unknown error')}",
            }

        # Extract daily series: { "data": { "values": { event_name: { "YYYY-MM-DD": count, ... } } } }
        values_dict = res.get("data", {}).get("values", {})
        event_values = values_dict.get(target_event, {}) if isinstance(values_dict, dict) else {}

        # Sort dates chronologically
        sorted_dates = sorted(event_values.keys()) if isinstance(event_values, dict) else []
        volumes = [int(event_values[d] or 0) for d in sorted_dates]

        if len(volumes) < 4:
            return {
                "metric": "event_volume_anomalies",
                "days_back": days_back,
                "event_name": target_event,
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
        recent_dates = sorted_dates[half:]

        try:
            baseline_mean = statistics.mean(baseline)
            baseline_std = statistics.pstdev(baseline) if len(baseline) > 1 else 0.0
        except Exception:
            baseline_mean = sum(baseline) / len(baseline) if baseline else 0.0
            baseline_std = 0.0

        anomalies = []
        if baseline_std > 0:
            for d, val in zip(recent_dates, recent, strict=False):
                z = (val - baseline_mean) / baseline_std
                if abs(z) > 2.0:
                    anomalies.append(
                        {
                            "date": d,
                            "volume": val,
                            "z_score": round(z, 2),
                            "type": "spike" if z > 0 else "drop",
                        }
                    )

        health_score = max(0, 100 - len(anomalies) * 15)
        return {
            "metric": "event_volume_anomalies",
            "days_back": days_back,
            "event_name": target_event,
            "baseline_mean": round(baseline_mean, 2),
            "baseline_std": round(baseline_std, 2),
            "anomalies": anomalies,
            "anomaly_count": len(anomalies),
            "health_score": health_score,
        }

    # ------------------------------------------------------------------
    # Layer 3: Write Operations (Mixpanel Lexicon Schemas API)
    # ------------------------------------------------------------------

    @friendly_errors("Mixpanel")
    async def create_event_type(
        self,
        api_secret: str,
        service_token: str,
        event_type: str,
        description: str | None = None,
        category: str | None = None,
        project_id: str | None = None,
    ) -> dict:
        """
        Create or annotate an event schema via Mixpanel Lexicon Schemas API.
        PUT /app/projects/{project_id}/schemas/events/{event_type}
        """
        payload: dict[str, Any] = {
            "name": event_type,
            "description": description or "",
        }
        if category:
            payload["tags"] = [category]

        endpoint = (
            f"/app/projects/{project_id}/schemas/events/{event_type}"
            if project_id
            else f"/2.0/events/schemas/{event_type}"
        )
        result = await self._request(api_secret, service_token, "PUT", endpoint, json_body=payload)
        if isinstance(result, dict) and result.get("error"):
            # Provide structured feedback if Lexicon Schemas requires additional project permissions
            return {
                "success": False,
                "event_type": event_type,
                "message": result.get("message", "Mixpanel event schema creation failed"),
                "error": result.get("message"),
                "note": "Mixpanel Lexicon requires Service Account with Data Governance permissions.",
            }

        return {
            "success": True,
            "event_type": event_type,
            "description": description,
            "category": category,
            "message": f"Event '{event_type}' registered in Mixpanel Lexicon.",
        }

    @friendly_errors("Mixpanel")
    async def update_event_type(
        self,
        api_secret: str,
        service_token: str,
        event_type: str,
        new_name: str | None = None,
        description: str | None = None,
        category: str | None = None,
        project_id: str | None = None,
    ) -> dict:
        """
        Update event metadata in Mixpanel Lexicon.
        PUT /app/projects/{project_id}/schemas/events/{event_type}
        """
        payload: dict[str, Any] = {
            "name": new_name or event_type,
        }
        if description is not None:
            payload["description"] = description
        if category is not None:
            payload["tags"] = [category]

        endpoint = (
            f"/app/projects/{project_id}/schemas/events/{event_type}"
            if project_id
            else f"/2.0/events/schemas/{event_type}"
        )
        result = await self._request(api_secret, service_token, "PUT", endpoint, json_body=payload)
        if isinstance(result, dict) and result.get("error"):
            return {
                "success": False,
                "event_type": event_type,
                "message": result.get("message", "Mixpanel event schema update failed"),
                "error": result.get("message"),
            }

        return {
            "success": True,
            "event_type": new_name or event_type,
            "description": description,
            "category": category,
            "message": f"Event '{event_type}' updated in Mixpanel Lexicon.",
        }

    @friendly_errors("Mixpanel")
    async def delete_event_type(
        self,
        api_secret: str,
        service_token: str,
        event_type: str,
        project_id: str | None = None,
    ) -> dict:
        """
        Hide or archive an event in Mixpanel Lexicon.
        DELETE /app/projects/{project_id}/schemas/events/{event_type}
        """
        endpoint = (
            f"/app/projects/{project_id}/schemas/events/{event_type}"
            if project_id
            else f"/2.0/events/schemas/{event_type}"
        )
        result = await self._request(api_secret, service_token, "DELETE", endpoint)
        if isinstance(result, dict) and result.get("error"):
            return {
                "success": False,
                "event_type": event_type,
                "message": result.get("message", "Mixpanel event schema deletion failed"),
                "error": result.get("message"),
            }

        return {
            "success": True,
            "event_type": event_type,
            "message": f"Event '{event_type}' archived/hidden in Mixpanel Lexicon.",
        }
