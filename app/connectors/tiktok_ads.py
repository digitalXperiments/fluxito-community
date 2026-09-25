"""
TikTok Ads Connector

Uses the TikTok Business API v1.3 via httpx (no official Python SDK).
API base: https://business-api.tiktok.com/open_api/v1.3/

Layer 1: list_accounts, get_campaign_performance, get_adgroup_performance
Layer 2: audit_tracking_setup  (pixel health + Events API detection)
Layer 3: update_campaign_status, update_campaign_budget
"""

import logging

import httpx

from app.connectors.errors import friendly_errors

logger = logging.getLogger(__name__)

_BASE = "https://business-api.tiktok.com/open_api/v1.3"


class TikTokAdsConnector:
    """Interfaces with TikTok Business API using per-user OAuth2 access tokens."""

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _headers(access_token: str) -> dict:
        return {
            "Access-Token": access_token,
            "Content-Type": "application/json",
        }

    @staticmethod
    def _check(resp_data: dict, context: str) -> dict | None:
        """Return error dict if TikTok API returned a non-OK code, else None."""
        code = resp_data.get("code", 0)
        if code != 0:
            msg = resp_data.get("message", "Unknown TikTok API error")
            logger.error(f"TikTok API error [{context}] code={code}: {msg}")
            return {"error": True, "message": f"TikTok API error ({code}): {msg}"}
        return None

    # ------------------------------------------------------------------
    # Layer 1: Data access
    # ------------------------------------------------------------------

    @friendly_errors("TikTok Ads")
    async def list_accounts(self, access_token: str) -> dict:
        """
        Returns all advertiser accounts accessible to this user token.
        Calls GET /oauth2/advertiser/get/
        """
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"{_BASE}/oauth2/advertiser/get/",
                headers=self._headers(access_token),
                params={"fields": '["advertiser_id","advertiser_name","currency","timezone","status"]'},
            )

        data = resp.json()
        err = self._check(data, "list_accounts")
        if err:
            return err

        advertisers = data.get("data", {}).get("list", [])
        return {
            "accounts": [
                {
                    "account_id": str(a.get("advertiser_id")),
                    "name": a.get("advertiser_name"),
                    "currency": a.get("currency"),
                    "timezone": a.get("timezone"),
                    "status": a.get("status"),
                }
                for a in advertisers
            ]
        }

    @friendly_errors("TikTok Ads")
    async def get_campaign_performance(
        self,
        access_token: str,
        account_id: str,
        start_date: str,
        end_date: str,
        metrics: list[str] | None = None,
    ) -> dict:
        """
        Returns campaign-level performance report.
        Calls GET /report/integrated/get/ with report_type=BASIC, dimensions=campaign_id.
        start_date / end_date: YYYY-MM-DD
        """
        default_metrics = [
            "campaign_name",
            "campaign_id",
            "objective_type",
            "campaign_budget",
            "campaign_budget_mode",
            "spend",
            "impressions",
            "clicks",
            "ctr",
            "cpc",
            "cpm",
            "conversions",
            "cost_per_conversion",
            "conversion_rate",
            "reach",
            "frequency",
        ]
        report_metrics = metrics or default_metrics

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{_BASE}/report/integrated/get/",
                headers=self._headers(access_token),
                params={
                    "advertiser_id": account_id,
                    "report_type": "BASIC",
                    "dimensions": '["campaign_id"]',
                    "metrics": str(report_metrics).replace("'", '"'),
                    "start_date": start_date,
                    "end_date": end_date,
                    "page_size": 100,
                },
            )

        data = resp.json()
        err = self._check(data, "get_campaign_performance")
        if err:
            return err

        rows = data.get("data", {}).get("list", [])
        campaigns = []
        for row in rows:
            dims = row.get("dimensions", {})
            mets = row.get("metrics", {})
            campaigns.append(
                {
                    "campaign_id": dims.get("campaign_id"),
                    **mets,
                }
            )

        return {
            "account_id": account_id,
            "date_range": f"{start_date} to {end_date}",
            "campaigns": campaigns,
            "total": len(campaigns),
        }

    @friendly_errors("TikTok Ads")
    async def get_adgroup_performance(
        self,
        access_token: str,
        account_id: str,
        start_date: str,
        end_date: str,
        campaign_id: str | None = None,
    ) -> dict:
        """
        Returns ad-group (adgroup) level performance report.
        Optionally filtered by campaign_id via filtering param.
        """
        metrics = [
            "adgroup_name",
            "adgroup_id",
            "campaign_name",
            "campaign_id",
            "spend",
            "impressions",
            "clicks",
            "ctr",
            "cpc",
            "cpm",
            "conversions",
            "cost_per_conversion",
            "conversion_rate",
        ]
        params = {
            "advertiser_id": account_id,
            "report_type": "BASIC",
            "dimensions": '["adgroup_id"]',
            "metrics": str(metrics).replace("'", '"'),
            "start_date": start_date,
            "end_date": end_date,
            "page_size": 100,
        }
        if campaign_id:
            params["filtering"] = (
                f'[{{"field_name":"campaign_id","filter_type":"IN","filter_value":"["{campaign_id}"]"}}]'
            )

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{_BASE}/report/integrated/get/",
                headers=self._headers(access_token),
                params=params,
            )

        data = resp.json()
        err = self._check(data, "get_adgroup_performance")
        if err:
            return err

        rows = data.get("data", {}).get("list", [])
        adgroups = [
            {"adgroup_id": row.get("dimensions", {}).get("adgroup_id"), **row.get("metrics", {})}
            for row in rows
        ]

        return {
            "account_id": account_id,
            "date_range": f"{start_date} to {end_date}",
            "adgroups": adgroups,
            "total": len(adgroups),
        }

    # ------------------------------------------------------------------
    # Layer 2: Audit
    # ------------------------------------------------------------------

    @friendly_errors("TikTok Ads")
    async def audit_tracking_setup(self, access_token: str, account_id: str) -> dict:
        """
        Audits TikTok tracking health for an advertiser account:
        - Lists all pixels (Base Code) linked to the account
        - Checks pixel status and last event activity
        - Flags missing Purchase/AddToCart events
        - Detects Events API (server-side) usage
        Calls GET /pixel/list/ and GET /pixel/track/get/
        """
        issues = []

        # 1. List pixels
        async with httpx.AsyncClient(timeout=20) as client:
            pixel_list_resp = await client.get(
                f"{_BASE}/pixel/list/",
                headers=self._headers(access_token),
                params={"advertiser_id": account_id},
            )

        data = pixel_list_resp.json()
        err = self._check(data, "audit_tracking_setup:pixel_list")
        if err:
            return {
                **err,
                "note": "Could not fetch pixel list. Verify the access token has TikTok Ads permissions.",
            }

        pixels = data.get("data", {}).get("pixels", [])

        if not pixels:
            return {
                "score": 0,
                "pixels": [],
                "issues": [
                    {
                        "severity": "critical",
                        "issue": "No TikTok Pixel found for this account",
                        "recommendation": "Create a TikTok Pixel in Events Manager and install the base code on your website",
                    }
                ],
            }

        pixel_results = []

        async with httpx.AsyncClient(timeout=20) as client:
            for pixel in pixels:
                pixel_id = pixel.get("pixel_id")
                pixel_name = pixel.get("pixel_name", "Unnamed Pixel")
                pixel_status = pixel.get("status", "UNKNOWN")

                # Fetch recent event stats for this pixel
                track_resp = await client.get(
                    f"{_BASE}/pixel/track/get/",
                    headers=self._headers(access_token),
                    params={
                        "advertiser_id": account_id,
                        "pixel_id": pixel_id,
                    },
                )
                track_data = track_resp.json()
                events = {}
                capi_events = {}
                if track_resp.status_code == 200 and track_data.get("code") == 0:
                    for ev in track_data.get("data", {}).get("event_stat_list", []):
                        ev_name = ev.get("event")
                        channel = ev.get("channel", "WEB")
                        count = ev.get("count", 0)
                        if channel == "WEB":
                            events[ev_name] = count
                        elif channel == "SERVER":
                            capi_events[ev_name] = count

                # Flag inactive pixel
                if pixel_status != "NORMAL":
                    issues.append(
                        {
                            "severity": "warning",
                            "pixel": pixel_name,
                            "issue": f"Pixel status is '{pixel_status}' (expected NORMAL)",
                            "recommendation": "Check the pixel installation and Events Manager for errors",
                        }
                    )

                # Flag missing PageView
                if events.get("Pageview", 0) == 0 and events.get("PageView", 0) == 0:
                    issues.append(
                        {
                            "severity": "warning",
                            "pixel": pixel_name,
                            "issue": "No Pageview events detected in recent period",
                            "recommendation": "Verify the TikTok base pixel code is installed on all pages",
                        }
                    )

                # Flag missing conversion events
                for ev in ["Purchase", "AddToCart"]:
                    if events.get(ev, 0) == 0:
                        issues.append(
                            {
                                "severity": "warning",
                                "pixel": pixel_name,
                                "issue": f"No '{ev}' events detected",
                                "recommendation": f"Add the standard '{ev}' event trigger or use Events API for server-side tracking",
                            }
                        )

                # Recommend Events API if not detected
                has_capi = bool(capi_events)
                if not has_capi and events.get("Purchase", 0) > 0:
                    issues.append(
                        {
                            "severity": "info",
                            "pixel": pixel_name,
                            "issue": "TikTok Events API (server-side) not detected",
                            "recommendation": "Implement TikTok Events API to send server-to-server Purchase events — improves signal quality and reduces data loss from iOS restrictions",
                        }
                    )

                pixel_results.append(
                    {
                        "pixel_id": pixel_id,
                        "pixel_name": pixel_name,
                        "status": pixel_status,
                        "browser_events": events,
                        "server_events": capi_events,
                        "capi_detected": has_capi,
                    }
                )

        critical = sum(1 for i in issues if i["severity"] == "critical")
        warning = sum(1 for i in issues if i["severity"] == "warning")
        score = max(0, 100 - critical * 30 - warning * 10)

        return {
            "score": score,
            "pixel_count": len(pixel_results),
            "pixels": pixel_results,
            "issues": issues,
            "summary": {
                "critical": critical,
                "warning": warning,
                "info": sum(1 for i in issues if i["severity"] == "info"),
            },
        }

    # ------------------------------------------------------------------
    # Layer 3: Write operations
    # ------------------------------------------------------------------

    @friendly_errors("TikTok Ads")
    async def update_campaign_status(
        self,
        access_token: str,
        account_id: str,
        campaign_id: str,
        status: str,
    ) -> dict:
        """
        Updates a TikTok campaign's status.
        status: ENABLE | DISABLE | DELETE
        """
        valid = {"ENABLE", "DISABLE", "DELETE"}
        if status.upper() not in valid:
            return {"error": True, "message": f"status must be one of: {valid}"}

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{_BASE}/campaign/status/update/",
                headers=self._headers(access_token),
                json={
                    "advertiser_id": account_id,
                    "campaign_ids": [campaign_id],
                    "operation_status": status.upper(),
                },
            )

        data = resp.json()
        err = self._check(data, "update_campaign_status")
        if err:
            return err

        return {
            "campaign_id": campaign_id,
            "account_id": account_id,
            "new_status": status.upper(),
            "updated": True,
        }

    @friendly_errors("TikTok Ads")
    async def update_campaign_budget(
        self,
        access_token: str,
        account_id: str,
        campaign_id: str,
        budget: float,
        budget_mode: str = "BUDGET_MODE_DAY",
    ) -> dict:
        """
        Updates a TikTok campaign's budget.
        budget_mode: BUDGET_MODE_DAY (daily) | BUDGET_MODE_TOTAL (lifetime)
        budget: in account currency (e.g. 50.0 = $50)
        """
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{_BASE}/campaign/update/",
                headers=self._headers(access_token),
                json={
                    "advertiser_id": account_id,
                    "campaign_id": campaign_id,
                    "budget": budget,
                    "budget_mode": budget_mode,
                },
            )

        data = resp.json()
        err = self._check(data, "update_campaign_budget")
        if err:
            return err

        return {
            "campaign_id": campaign_id,
            "account_id": account_id,
            "new_budget": budget,
            "budget_mode": budget_mode,
            "updated": True,
        }

    @friendly_errors("TikTok Ads")
    async def create_campaign(
        self,
        access_token: str,
        account_id: str,
        campaign_name: str,
        objective_type: str,
        budget: float,
        budget_mode: str = "BUDGET_MODE_DAY",
        operation_system: int | None = None,
    ) -> dict:
        """
        Creates a new TikTok campaign.
        objective_type: TRAFFIC | CONVERSIONS | REACH | VIDEO_VIEWS
        budget: in account currency (e.g. 50.0 = $50)
        budget_mode: BUDGET_MODE_DAY (daily) | BUDGET_MODE_TOTAL (lifetime)
        operation_system: optional OS targeting (0=Android, 1=iOS)
        """
        body = {
            "advertiser_id": account_id,
            "campaign_name": campaign_name,
            "objective_type": objective_type,
            "budget_mode": budget_mode,
            "budget": budget,
        }
        if operation_system is not None:
            body["operation_system"] = operation_system

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{_BASE}/campaign/create/",
                headers=self._headers(access_token),
                json=body,
            )

        data = resp.json()
        err = self._check(data, "create_campaign")
        if err:
            return err

        campaign_data = data.get("data", {})
        return {
            "campaign_id": campaign_data.get("campaign_id"),
            "campaign_name": campaign_name,
            "objective_type": objective_type,
            "budget": budget,
            "budget_mode": budget_mode,
            "account_id": account_id,
            "updated": True,
        }
