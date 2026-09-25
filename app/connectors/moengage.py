"""
MoEngage Connector.

Interfaces with the MoEngage Customer Engagement Platform REST APIs for:
- User profiles (create, update, fetch)
- Device tracking
- Campaign search / details (via available search endpoints)
- Transactional push (via Push API)
- Transactional email / SMS (via Inform API)

Data Center handling:
    Base URLs are built dynamically as https://api-{data_center}.moengage.com
    (and https://pushapi-{data_center}.moengage.com for transactional push).

Authentication:
    - Data / Inform / most APIs: HTTP Basic with username=Workspace ID (app_id),
      password=API Key. Header: Authorization: Basic base64(app_id:api_key)
    - Transactional Push: signature inside JSON body (SHA-256 of
      "appId|campaignName|api_key"); no Basic header. Uses pushapi- host.

All credentials are passed per-call (never stored in the connector).

References:
- https://www.moengage.com/docs/api/introduction
- https://www.moengage.com/docs/api/data/data-overview
- https://www.moengage.com/docs/api/push/push-overview
- https://www.moengage.com/docs/api/inform/inform-overview
"""

import base64
import hashlib
import logging
from typing import Any

import httpx

from app.connectors.errors import friendly_errors

logger = logging.getLogger(__name__)


class MoengageConnector:
    """MoEngage REST API connector for user data, devices, campaigns, and transactional messaging."""

    def __init__(self) -> None:
        """No persistent state; credentials are passed to every method."""

    # ------------------------------------------------------------------
    # URL + Auth helpers
    # ------------------------------------------------------------------

    def _build_base_url(self, data_center: str) -> str:
        """Build the standard Data/Inform API base for a given data center (e.g. '01')."""
        dc = str(data_center).strip()
        return f"https://api-{dc}.moengage.com"

    def _build_push_base_url(self, data_center: str) -> str:
        """Build the Push transactional API base (different host)."""
        dc = str(data_center).strip()
        return f"https://pushapi-{dc}.moengage.com"

    def _basic_auth_header(self, app_id: str, api_key: str) -> dict[str, str]:
        """Return headers for Basic auth + JSON content type."""
        token = base64.b64encode(f"{app_id}:{api_key}".encode()).decode()
        return {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
        }

    def _compute_push_signature(self, app_id: str, campaign_name: str, api_key: str) -> str:
        """SHA-256 hex digest of ``{app_id}|{campaign_name}|{api_key}`` for the Push transactional API signature."""
        key = f"{app_id}|{campaign_name}|{api_key}"
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------
    # Core request
    # ------------------------------------------------------------------

    async def _request(
        self,
        *,
        base_url: str,
        app_id: str,
        api_key: str,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        use_basic_auth: bool = True,
    ) -> dict:
        """
        Perform an HTTP request against MoEngage.

        - use_basic_auth=True  → adds Basic header (Data/Inform APIs)
        - use_basic_auth=False → caller is responsible (e.g. Push uses body signature)
        """
        hdrs: dict[str, str] = dict(headers or {})
        if use_basic_auth:
            hdrs.update(self._basic_auth_header(app_id, api_key))
        else:
            hdrs.setdefault("Content-Type", "application/json")

        url = f"{base_url.rstrip('/')}{path}"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                if method.upper() == "GET":
                    resp = await client.get(url, headers=hdrs, params=params)
                elif method.upper() == "POST":
                    resp = await client.post(url, headers=hdrs, params=params, json=json_body)
                else:
                    return {"error": True, "message": f"Unsupported HTTP method: {method}"}

                if resp.status_code == 429:
                    return {
                        "error": True,
                        "error_type": "rate_limited",
                        "status_code": 429,
                        "message": resp.text or "MoEngage rate limit exceeded",
                    }

                if resp.status_code >= 400:
                    return {
                        "error": True,
                        "status_code": resp.status_code,
                        "message": resp.text,
                    }

                try:
                    data = resp.json()
                except Exception:
                    # Non-JSON success body
                    return {"success": True, "status_code": resp.status_code, "body": resp.text}

                # MoEngage Data APIs return {"status": "fail", "error": {...}}
                if isinstance(data, dict) and data.get("status") == "fail":
                    err = data.get("error", {})
                    return {
                        "error": True,
                        "status_code": resp.status_code,
                        "message": err.get("message") or str(data),
                        "error_details": err,
                    }

                return data  # type: ignore[no-any-return]

        except Exception as e:
            logger.error(f"MoEngage API request error: {e}")
            return {"error": True, "message": str(e)}

    # ------------------------------------------------------------------
    # READ methods
    # ------------------------------------------------------------------

    @friendly_errors("Moengage")
    async def get_user_info(
        self,
        data_center: str,
        app_id: str,
        api_key: str,
        *,
        customer_id: str | None = None,
        moengage_id: str | None = None,
        user_fields_to_export: list[str] | None = None,
    ) -> dict:
        """
        Retrieve user profile(s).

        Uses POST /v1/customers/export (supports customer_id or moengage_id).
        At least one identifier must be provided.
        """
        base = self._build_base_url(data_center)

        identifiers: list[dict[str, str]] = []
        if customer_id:
            identifiers.append({"identifier_type": "customer_id", "identifier": customer_id})
        if moengage_id:
            identifiers.append({"identifier_type": "moengage_id", "identifier": moengage_id})

        if not identifiers:
            return {"error": True, "message": "Either customer_id or moengage_id is required"}

        body: dict[str, Any] = {"data": {"identifiers": identifiers}}
        if user_fields_to_export:
            body["data"]["user_fields_to_export"] = user_fields_to_export

        # This endpoint expects app_id as query parameter
        return await self._request(
            base_url=base,
            app_id=app_id,
            api_key=api_key,
            method="POST",
            path="/v1/customers/export",
            params={"app_id": app_id},
            json_body=body,
        )

    @friendly_errors("Moengage")
    async def list_campaigns(
        self,
        data_center: str,
        app_id: str,
        api_key: str,
        *,
        channel: str | None = None,
        limit: int = 50,
    ) -> dict:
        """
        List/search campaigns.

        Uses the available campaigns search endpoint. Results depend on the
        workspace's campaign visibility and API access.
        """
        base = self._build_base_url(data_center)
        body: dict[str, Any] = {"limit": limit}
        if channel:
            body["channel"] = channel

        return await self._request(
            base_url=base,
            app_id=app_id,
            api_key=api_key,
            method="POST",
            path="/campaigns/search",
            json_body=body,
        )

    @friendly_errors("Moengage")
    async def get_campaign_details(
        self,
        data_center: str,
        app_id: str,
        api_key: str,
        *,
        campaign_id: str | None = None,
        campaign_name: str | None = None,
    ) -> dict:
        """
        Fetch details for a specific campaign via search/meta.

        Provide either campaign_id or campaign_name.
        """
        base = self._build_base_url(data_center)
        body: dict[str, Any] = {}
        if campaign_id:
            body["campaign_ids"] = [campaign_id]
        if campaign_name:
            body["campaign_names"] = [campaign_name]

        return await self._request(
            base_url=base,
            app_id=app_id,
            api_key=api_key,
            method="POST",
            path="/campaigns/search",
            json_body=body,
        )

    @friendly_errors("Moengage")
    async def list_events(
        self,
        data_center: str,
        app_id: str,
        api_key: str,
        *,
        limit: int = 50,
    ) -> dict:
        """
        List events.

        MoEngage does not expose a simple public "list events" REST endpoint.
        Events are primarily tracked (ingested) via the Track Event API and
        are visible in the dashboard / exports.

        This method returns a structured informational response.
        """
        return {
            "error": False,
            "note": (
                "MoEngage does not provide a public REST endpoint to enumerate "
                "registered events. Events are tracked via the Track Event API "
                "(POST /v1/event/{app_id}) and appear in the MoEngage dashboard, "
                "analytics, and data warehouse exports."
            ),
            "events": [],
        }

    # ------------------------------------------------------------------
    # WRITE methods
    # ------------------------------------------------------------------

    @friendly_errors("Moengage")
    async def create_user(
        self,
        data_center: str,
        app_id: str,
        api_key: str,
        *,
        customer_id: str,
        attributes: dict[str, Any] | None = None,
        platforms: list[dict[str, Any]] | None = None,
        update_existing_only: bool = False,
    ) -> dict:
        """
        Create or update a user profile.

        Endpoint: POST /v1/customer/{app_id}
        Set update_existing_only=True to only update existing users.
        """
        base = self._build_base_url(data_center)
        body: dict[str, Any] = {
            "type": "customer",
            "customer_id": customer_id,
        }
        if attributes:
            body["attributes"] = attributes
        if platforms:
            body["platforms"] = platforms
        if update_existing_only:
            body["update_existing_only"] = True

        return await self._request(
            base_url=base,
            app_id=app_id,
            api_key=api_key,
            method="POST",
            path=f"/v1/customer/{app_id}",
            json_body=body,
        )

    @friendly_errors("Moengage")
    async def update_user(
        self,
        data_center: str,
        app_id: str,
        api_key: str,
        *,
        customer_id: str,
        attributes: dict[str, Any] | None = None,
        platforms: list[dict[str, Any]] | None = None,
    ) -> dict:
        """
        Update an existing user (does not create new users).

        This is a convenience wrapper around create_user with update_existing_only=True.
        """
        return await self.create_user(
            data_center,
            app_id,
            api_key,
            customer_id=customer_id,
            attributes=attributes,
            platforms=platforms,
            update_existing_only=True,
        )

    @friendly_errors("Moengage")
    async def add_device(
        self,
        data_center: str,
        app_id: str,
        api_key: str,
        *,
        device: dict[str, Any],
    ) -> dict:
        """
        Track / add or update a device for a user.

        Endpoint: POST /v1/device/{app_id}
        The `device` dict should follow MoEngage Track Device schema
        (platform, push_id, etc.).
        """
        base = self._build_base_url(data_center)
        # The device payload typically wraps under a known structure; pass through as-is
        # and let the caller supply the correct shape per docs.
        body = {"type": "device", **device}

        return await self._request(
            base_url=base,
            app_id=app_id,
            api_key=api_key,
            method="POST",
            path=f"/v1/device/{app_id}",
            json_body=body,
        )

    # ------------------------------------------------------------------
    # Transactional messaging
    # ------------------------------------------------------------------

    @friendly_errors("Moengage")
    async def send_push(
        self,
        data_center: str,
        app_id: str,
        api_key: str,
        *,
        campaign_name: str,
        target_platform: list[str],
        target_audience: str = "All Users",
        payload: dict[str, Any],
        target_user_attributes: dict[str, Any] | None = None,
        custom_segment_name: str | None = None,
        campaign_delivery: dict[str, Any] | None = None,
        advanced_settings: dict[str, Any] | None = None,
        campaign_tags: list[str] | None = None,
    ) -> dict:
        """
        Send a transactional or targeted push notification.

        Uses https://pushapi-{dc}.moengage.com/v2/transaction/sendpush
        Requires the Push API key (for signature generation).
        """
        base = self._build_push_base_url(data_center)
        signature = self._compute_push_signature(app_id, campaign_name, api_key)

        body: dict[str, Any] = {
            "appId": app_id,
            "campaignName": campaign_name,
            "signature": signature,
            "requestType": "push",
            "targetPlatform": target_platform,
            "targetAudience": target_audience,
            "payload": payload,
            "campaignDelivery": campaign_delivery or {"type": "soon"},
        }
        if target_user_attributes:
            body["targetUserAttributes"] = target_user_attributes
        if custom_segment_name:
            body["customSegmentName"] = custom_segment_name
        if advanced_settings:
            body["advancedSettings"] = advanced_settings
        if campaign_tags:
            body["campaignTags"] = campaign_tags

        # Push endpoint authenticates via signature in the body, not Basic header.
        return await self._request(
            base_url=base,
            app_id=app_id,
            api_key=api_key,
            method="POST",
            path="/v2/transaction/sendpush",
            json_body=body,
            use_basic_auth=False,
        )

    @friendly_errors("Moengage")
    async def send_email(
        self,
        data_center: str,
        app_id: str,
        api_key: str,
        *,
        transaction_id: str,
        recipients: dict[str, Any],
        alert_id: str | None = None,
        alert_reference_name: str | None = None,
        personalization: dict[str, Any] | None = None,
    ) -> dict:
        """
        Send a transactional email via the Inform API.

        The alert (identified by alert_id or alert_reference_name) must be
        pre-configured in the MoEngage dashboard and must include the Email channel.
        """
        base = self._build_base_url(data_center)
        body: dict[str, Any] = {
            "transaction_id": transaction_id,
            "recipients": recipients,
        }
        if alert_id:
            body["alert_id"] = alert_id
        if alert_reference_name:
            body["alert_reference_name"] = alert_reference_name
        if personalization:
            body["personalization"] = personalization

        return await self._request(
            base_url=base,
            app_id=app_id,
            api_key=api_key,
            method="POST",
            path="/alerts/send",
            json_body=body,
        )

    @friendly_errors("Moengage")
    async def send_sms(
        self,
        data_center: str,
        app_id: str,
        api_key: str,
        *,
        transaction_id: str,
        recipients: dict[str, Any],
        alert_id: str | None = None,
        alert_reference_name: str | None = None,
        personalization: dict[str, Any] | None = None,
    ) -> dict:
        """
        Send a transactional SMS via the Inform API.

        The alert must be pre-configured with the SMS channel.
        """
        # Inform /alerts/send handles SMS when the referenced alert is configured for SMS.
        return await self.send_email(
            data_center,
            app_id,
            api_key,
            transaction_id=transaction_id,
            recipients=recipients,
            alert_id=alert_id,
            alert_reference_name=alert_reference_name,
            personalization=personalization,
        )
