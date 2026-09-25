"""
Braze Connector.

Interfaces with the Braze REST API for customer engagement (campaigns, Canvases,
segments, user profile tracking, messaging, and API-triggered sends).

Auth:
- Static REST API key passed as `Authorization: Bearer {api_key}`.
- No OAuth token exchange or refresh.

Base URL:
- Cluster-specific `rest_endpoint_url` (e.g. https://rest.iad-01.braze.com).
- Passed as a parameter to every method (never stored in the connector).

All public methods are decorated with @friendly_errors("Braze") and return
either a successful response dict from Braze or an error dict:
    {"error": True, "status_code": ..., "message": ...}
For 429 responses:
    {"error": True, "error_type": "rate_limited", "status_code": 429, ...}

Rate limits:
- Braze returns HTTP 429 with optional X-RateLimit-Reset header.
"""

import logging
from typing import Any

import httpx

from app.connectors.errors import friendly_errors

logger = logging.getLogger(__name__)


class BrazeConnector:
    """Interfaces with the Braze REST API using cluster-specific endpoints and Bearer auth."""

    def __init__(self) -> None:
        # No persistent state; credentials are passed per call.
        pass

    async def _request(
        self,
        rest_endpoint_url: str,
        api_key: str,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict:
        """
        Low-level HTTP request to a Braze REST endpoint.

        - Strips trailing slashes from the endpoint URL.
        - Uses Bearer token auth.
        - Returns parsed JSON on success or a structured error dict.
        - Does NOT apply @friendly_errors; callers (public methods) wrap.
        """
        base = (rest_endpoint_url or "").rstrip("/")
        url = f"{base}{path}"

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                if method == "GET":
                    resp = await client.get(url, headers=headers, params=params)
                elif method == "POST":
                    resp = await client.post(url, headers=headers, params=params, json=json_body)
                else:
                    return {"error": True, "message": f"Unsupported HTTP method: {method}"}

                if resp.status_code == 429:
                    result: dict[str, Any] = {
                        "error": True,
                        "error_type": "rate_limited",
                        "status_code": 429,
                        "message": resp.text,
                    }
                    reset = resp.headers.get("X-RateLimit-Reset")
                    if reset:
                        result["retry_after"] = reset
                    return result

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
            logger.error(f"Braze API request error: {e}")
            return {"error": True, "message": str(e)}

    # ------------------------------------------------------------------
    # Layer 1: Read (Export)
    # ------------------------------------------------------------------

    @friendly_errors("Braze")
    async def list_campaigns(
        self,
        rest_endpoint_url: str,
        api_key: str,
        *,
        page: int = 0,
        include_archived: bool = False,
        sort_direction: str | None = None,
        modified_after: str | None = None,
    ) -> dict:
        """
        GET /campaigns/list

        List campaigns (paged, 100 per page by default).
        """
        params: dict[str, Any] = {
            "page": page,
            "include_archived": str(include_archived).lower(),
        }
        if sort_direction:
            params["sort_direction"] = sort_direction
        if modified_after:
            params["last_edit.time[gt]"] = modified_after

        return await self._request(rest_endpoint_url, api_key, "GET", "/campaigns/list", params=params)

    @friendly_errors("Braze")
    async def get_campaign_details(
        self,
        rest_endpoint_url: str,
        api_key: str,
        campaign_id: str,
        *,
        post_launch_draft_version: bool = False,
        include_has_translatable_content: bool = False,
    ) -> dict:
        """
        GET /campaigns/details?campaign_id=...

        Retrieve metadata and message content for a specific campaign.
        """
        params: dict[str, Any] = {
            "campaign_id": campaign_id,
            "post_launch_draft_version": str(post_launch_draft_version).lower(),
            "include_has_translatable_content": str(include_has_translatable_content).lower(),
        }
        return await self._request(rest_endpoint_url, api_key, "GET", "/campaigns/details", params=params)

    @friendly_errors("Braze")
    async def list_canvases(
        self,
        rest_endpoint_url: str,
        api_key: str,
        *,
        page: int = 0,
        include_archived: bool = False,
        sort_direction: str | None = None,
        modified_after: str | None = None,
    ) -> dict:
        """
        GET /canvas/list

        List Canvases (paged).
        """
        params: dict[str, Any] = {
            "page": page,
            "include_archived": str(include_archived).lower(),
        }
        if sort_direction:
            params["sort_direction"] = sort_direction
        if modified_after:
            params["last_edit.time[gt]"] = modified_after

        return await self._request(rest_endpoint_url, api_key, "GET", "/canvas/list", params=params)

    @friendly_errors("Braze")
    async def get_canvas_details(
        self,
        rest_endpoint_url: str,
        api_key: str,
        canvas_id: str,
        *,
        post_launch_draft_version: bool = False,
        include_has_translatable_content: bool = False,
    ) -> dict:
        """
        GET /canvas/details?canvas_id=...

        Retrieve metadata and step/message content for a specific Canvas.
        """
        params: dict[str, Any] = {
            "canvas_id": canvas_id,
            "post_launch_draft_version": str(post_launch_draft_version).lower(),
            "include_has_translatable_content": str(include_has_translatable_content).lower(),
        }
        return await self._request(rest_endpoint_url, api_key, "GET", "/canvas/details", params=params)

    @friendly_errors("Braze")
    async def list_segments(
        self,
        rest_endpoint_url: str,
        api_key: str,
        *,
        page: int = 0,
        sort_direction: str | None = None,
    ) -> dict:
        """
        GET /segments/list

        List segments (paged).
        """
        params: dict[str, Any] = {"page": page}
        if sort_direction:
            params["sort_direction"] = sort_direction

        return await self._request(rest_endpoint_url, api_key, "GET", "/segments/list", params=params)

    @friendly_errors("Braze")
    async def get_segment_details(self, rest_endpoint_url: str, api_key: str, segment_id: str) -> dict:
        """
        GET /segments/details?segment_id=...

        Retrieve metadata for a specific segment.
        """
        params = {"segment_id": segment_id}
        return await self._request(rest_endpoint_url, api_key, "GET", "/segments/details", params=params)

    # ------------------------------------------------------------------
    # Layer 2: User Data (track, alias, identify, merge, delete)
    # ------------------------------------------------------------------

    @friendly_errors("Braze")
    async def track_users(
        self,
        rest_endpoint_url: str,
        api_key: str,
        *,
        attributes: list[dict] | None = None,
        events: list[dict] | None = None,
        purchases: list[dict] | None = None,
    ) -> dict:
        """
        POST /users/track

        Record attributes, custom events, and purchases.
        Each array may contain up to 75 objects.
        """
        body: dict[str, Any] = {}
        if attributes:
            body["attributes"] = attributes
        if events:
            body["events"] = events
        if purchases:
            body["purchases"] = purchases

        return await self._request(rest_endpoint_url, api_key, "POST", "/users/track", json_body=body)

    @friendly_errors("Braze")
    async def create_user_alias(
        self,
        rest_endpoint_url: str,
        api_key: str,
        *,
        user_aliases: list[dict],
    ) -> dict:
        """
        POST /users/alias/new

        Create new user aliases (for existing users or new alias-only users).
        Up to 50 aliases per request.
        """
        body = {"user_aliases": user_aliases}
        return await self._request(rest_endpoint_url, api_key, "POST", "/users/alias/new", json_body=body)

    @friendly_errors("Braze")
    async def identify_users(
        self,
        rest_endpoint_url: str,
        api_key: str,
        *,
        aliases_to_identify: list[dict],
    ) -> dict:
        """
        POST /users/identify

        Identify alias-only users with an external_id.
        """
        body = {"aliases_to_identify": aliases_to_identify}
        return await self._request(rest_endpoint_url, api_key, "POST", "/users/identify", json_body=body)

    @friendly_errors("Braze")
    async def merge_users(
        self,
        rest_endpoint_url: str,
        api_key: str,
        *,
        merge_updates: list[dict],
    ) -> dict:
        """
        POST /users/merge

        Merge one user profile into another.
        """
        body = {"merge_updates": merge_updates}
        return await self._request(rest_endpoint_url, api_key, "POST", "/users/merge", json_body=body)

    @friendly_errors("Braze")
    async def delete_users(
        self,
        rest_endpoint_url: str,
        api_key: str,
        *,
        braze_ids: list[str] | None = None,
        external_ids: list[str] | None = None,
        user_aliases: list[dict] | None = None,
    ) -> dict:
        """
        POST /users/delete

        Delete users by braze_id, external_id, or user alias.
        """
        body: dict[str, Any] = {}
        if braze_ids:
            body["braze_ids"] = braze_ids
        if external_ids:
            body["external_ids"] = external_ids
        if user_aliases:
            body["user_aliases"] = user_aliases

        return await self._request(rest_endpoint_url, api_key, "POST", "/users/delete", json_body=body)

    # ------------------------------------------------------------------
    # Layer 3: Messaging (immediate + API-triggered)
    # ------------------------------------------------------------------

    @friendly_errors("Braze")
    async def send_message(
        self,
        rest_endpoint_url: str,
        api_key: str,
        *,
        broadcast: bool = False,
        external_user_ids: list[str] | None = None,
        user_aliases: list[dict] | None = None,
        segment_id: str | None = None,
        audience: dict | None = None,
        messages: dict | None = None,
        campaign_id: str | None = None,
        send_id: str | None = None,
        override_frequency_capping: bool = False,
        recipient_subscription_state: str = "subscribed",
    ) -> dict:
        """
        POST /messages/send

        Send an immediate message via the API (no pre-built campaign required).
        """
        body: dict[str, Any] = {
            "broadcast": broadcast,
            "override_frequency_capping": override_frequency_capping,
            "recipient_subscription_state": recipient_subscription_state,
        }
        if external_user_ids:
            body["external_user_ids"] = external_user_ids
        if user_aliases:
            body["user_aliases"] = user_aliases
        if segment_id:
            body["segment_id"] = segment_id
        if audience:
            body["audience"] = audience
        if messages:
            body["messages"] = messages
        if campaign_id:
            body["campaign_id"] = campaign_id
        if send_id:
            body["send_id"] = send_id

        return await self._request(rest_endpoint_url, api_key, "POST", "/messages/send", json_body=body)

    @friendly_errors("Braze")
    async def trigger_campaign(
        self,
        rest_endpoint_url: str,
        api_key: str,
        campaign_id: str,
        *,
        broadcast: bool = False,
        recipients: list[dict] | None = None,
        audience: dict | None = None,
        trigger_properties: dict | None = None,
        send_id: str | None = None,
        attachments: list[dict] | None = None,
    ) -> dict:
        """
        POST /campaigns/trigger/send

        Trigger delivery of an API-triggered campaign.
        """
        body: dict[str, Any] = {
            "campaign_id": campaign_id,
            "broadcast": broadcast,
        }
        if recipients is not None:
            body["recipients"] = recipients
        if audience:
            body["audience"] = audience
        if trigger_properties:
            body["trigger_properties"] = trigger_properties
        if send_id:
            body["send_id"] = send_id
        if attachments:
            body["attachments"] = attachments

        return await self._request(
            rest_endpoint_url, api_key, "POST", "/campaigns/trigger/send", json_body=body
        )

    @friendly_errors("Braze")
    async def trigger_canvas(
        self,
        rest_endpoint_url: str,
        api_key: str,
        canvas_id: str,
        *,
        broadcast: bool = False,
        recipients: list[dict] | None = None,
        audience: dict | None = None,
        context: dict | None = None,
    ) -> dict:
        """
        POST /canvas/trigger/send

        Trigger delivery of an API-triggered Canvas.
        """
        body: dict[str, Any] = {
            "canvas_id": canvas_id,
            "broadcast": broadcast,
        }
        if recipients is not None:
            body["recipients"] = recipients
        if audience:
            body["audience"] = audience
        if context:
            body["context"] = context

        return await self._request(rest_endpoint_url, api_key, "POST", "/canvas/trigger/send", json_body=body)

    @friendly_errors("Braze")
    async def create_send_id(
        self,
        rest_endpoint_url: str,
        api_key: str,
        campaign_id: str,
        *,
        send_id: str | None = None,
    ) -> dict:
        """
        POST /sends/id/create

        Create a send identifier for tracking API-driven sends.
        """
        body: dict[str, Any] = {"campaign_id": campaign_id}
        if send_id:
            body["send_id"] = send_id

        return await self._request(rest_endpoint_url, api_key, "POST", "/sends/id/create", json_body=body)
