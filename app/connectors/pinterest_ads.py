"""
Pinterest Ads Connector

Uses the Pinterest Ads API v5 via httpx.
API base: https://api.pinterest.com/v5/

Layer 1: list_accounts, get_campaign_performance, get_adgroup_performance
Layer 2: audit_tracking_setup (basic pixel health)
Layer 3: update_campaign_status, update_campaign_budget
"""

import logging
import re

import httpx

from app.connectors.errors import friendly_errors

logger = logging.getLogger(__name__)

_BASE = "https://api.pinterest.com/v5/"


class PinterestAdsConnector:
    """Interfaces with Pinterest Ads API using per-user OAuth2 access tokens."""

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _headers(access_token: str) -> dict:
        return {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _check(resp: httpx.Response, context: str) -> dict | None:
        """Return error dict if response is not 2xx, else None."""
        if resp.status_code not in (200, 201):
            logger.error(f"Pinterest API error [{context}] status={resp.status_code}: {resp.text[:300]}")
            return {"error": True, "message": f"Pinterest API error ({resp.status_code}): {resp.text[:300]}"}
        return None

    # ------------------------------------------------------------------
    # Layer 1: Data access
    # ------------------------------------------------------------------

    @friendly_errors("Pinterest Ads")
    async def list_accounts(self, access_token: str) -> dict:
        """
        Returns all ad accounts.
        Calls GET /ad_accounts
        """
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"{_BASE}ad_accounts",
                headers=self._headers(access_token),
            )

        err = self._check(resp, "list_accounts")
        if err:
            return err

        accounts_data = resp.json().get("items", [])
        return {
            "accounts": [
                {
                    "account_id": acc.get("id"),
                    "name": acc.get("name"),
                    "currency": acc.get("currency", "USD"),
                    "status": acc.get("account_status", "ACTIVE"),
                }
                for acc in accounts_data
            ]
        }

    @friendly_errors("Pinterest Ads")
    async def get_campaign_performance(
        self,
        access_token: str,
        account_id: str,
        start_date: str,
        end_date: str,
        metrics: list[str] | None = None,
    ) -> dict:
        """
        Returns campaign performance.
        Bulk POST /ad_accounts/{id}/campaigns/analytics_report with campaign_ids.
        """
        if not (
            re.match(r"^\d{4}-\d{2}-\d{2}$", start_date)
            and re.match(r"^\d{4}-\d{2}-\d{2}$", end_date)
            and start_date <= end_date
        ):
            return {"error": True, "message": "Invalid date format or range. Use YYYY-MM-DD, start <= end."}

        DEFAULT_COLUMNS = ["impressions", "clicks", "spend", "total_conversions"]
        columns = metrics or DEFAULT_COLUMNS

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{_BASE}ad_accounts/{account_id}/campaigns?status=ACTIVE",
                headers=self._headers(access_token),
            )

            err = self._check(resp, "get_campaign_performance:list")
            if err:
                return err

            campaigns = resp.json().get("items", [])
            if not campaigns:
                return {
                    "account_id": account_id,
                    "date_range": f"{start_date} to {end_date}",
                    "campaigns": [],
                }

            campaign_ids = [c["id"] for c in campaigns]
            stats_body = {
                "campaign_ids": campaign_ids,
                "columns": columns,
                "start_date": start_date,
                "end_date": end_date,
                "level": "campaign",
                "report_type": "analytics",
            }
            stats_resp = await client.post(
                f"{_BASE}ad_accounts/{account_id}/campaigns/analytics_report",
                headers=self._headers(access_token),
                json=stats_body,
            )
            if stats_resp.status_code != 200:
                logger.warning(f"Pinterest stats failed: {stats_resp.status_code} {stats_resp.text[:200]}")
                stats_map = {}
            else:
                stats_list = stats_resp.json().get("items", [])
                stats_map = {s["campaign_id"]: s for s in stats_list}

        result_campaigns = []
        for c in campaigns:
            stats = stats_map.get(
                c["id"], dict.fromkeys(["impressions", "clicks", "spend", "total_conversions"], 0)
            )
            result_campaigns.append(
                {
                    "campaign_id": c["id"],
                    "campaign_name": c.get("name", "Unnamed"),
                    "status": c.get("status", "ACTIVE"),
                    "impressions": stats.get("impressions", 0),
                    "clicks": stats.get("clicks", 0),
                    "cost": stats.get("spend", 0.0),
                    "conversions": stats.get("total_conversions", 0),
                }
            )

        return {
            "account_id": account_id,
            "date_range": f"{start_date} to {end_date}",
            "campaigns": result_campaigns,
        }

    @friendly_errors("Pinterest Ads")
    async def get_adgroup_performance(
        self,
        access_token: str,
        account_id: str,
        start_date: str,
        end_date: str,
        campaign_id: str | None = None,
    ) -> dict:
        """
        Returns ad group performance.
        """
        if not (
            re.match(r"^\d{4}-\d{2}-\d{2}$", start_date)
            and re.match(r"^\d{4}-\d{2}-\d{2}$", end_date)
            and start_date <= end_date
        ):
            return {"error": True, "message": "Invalid date format or range. Use YYYY-MM-DD, start <= end."}

        DEFAULT_COLUMNS = ["impressions", "clicks", "spend", "total_conversions"]
        params = {"status": "ACTIVE"}
        if campaign_id:
            params["campaign_ids"] = [campaign_id]

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{_BASE}ad_accounts/{account_id}/ad_groups",
                headers=self._headers(access_token),
                params=params,
            )

            err = self._check(resp, "get_adgroup_performance:list")
            if err:
                return err

            adgroups = resp.json().get("items", [])
            if not adgroups:
                return {"account_id": account_id, "date_range": f"{start_date} to {end_date}", "adgroups": []}

            adgroup_ids = [ag["id"] for ag in adgroups]
            campaign_filter = [campaign_id] if campaign_id else None
            stats_body = {
                "campaign_ids": campaign_filter,
                "columns": DEFAULT_COLUMNS,
                "start_date": start_date,
                "end_date": end_date,
                "level": "ad_group",
                "report_type": "analytics",
            }
            stats_resp = await client.post(
                f"{_BASE}ad_accounts/{account_id}/ad_groups/analytics_report",
                headers=self._headers(access_token),
                json=stats_body,
            )
            if stats_resp.status_code != 200:
                logger.warning(
                    f"Pinterest adgroup stats failed: {stats_resp.status_code} {stats_resp.text[:200]}"
                )
                stats_map = {}
            else:
                stats_list = stats_resp.json().get("items", [])
                stats_map = {s["ad_group_id"]: s for s in stats_list}

        result_adgroups = []
        for ag in adgroups:
            stats = stats_map.get(
                ag["id"], dict.fromkeys(["impressions", "clicks", "spend", "total_conversions"], 0)
            )
            result_adgroups.append(
                {
                    "adgroup_id": ag["id"],
                    "name": ag.get("name", "Unnamed"),
                    "status": ag.get("status", "ACTIVE"),
                    "impressions": stats.get("impressions", 0),
                    "clicks": stats.get("clicks", 0),
                    "cost": stats.get("spend", 0.0),
                    "conversions": stats.get("total_conversions", 0),
                }
            )

        return {
            "account_id": account_id,
            "date_range": f"{start_date} to {end_date}",
            "adgroups": result_adgroups,
        }

    @friendly_errors("Pinterest Ads")
    async def audit_tracking_setup(self, access_token: str, account_id: str) -> dict:
        """
        Basic Pinterest pixel audit.
        """
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"{_BASE}ad_accounts/{account_id}/events",
                headers=self._headers(access_token),
            )
        err = self._check(resp, "audit_tracking_setup")
        if err:
            return err
        events_data = resp.json().get("items", [])
        pixels = [
            {
                "id": e.get("id"),
                "name": e.get("name", "Unnamed"),
                "status": e.get("status", "UNKNOWN"),
                "verified": e.get("verification_status") == "VERIFIED",
                "last_fired": e.get("last_fired_time"),
            }
            for e in events_data
        ]
        active_verified = sum(1 for p in pixels if p["status"] == "ACTIVE" and p["verified"])
        total = len(pixels)
        score = 100 if active_verified > 0 else 60 if total > 0 else 20
        issues = []
        if active_verified == 0 and total > 0:
            issues.append("No verified active events.")
        if total == 0:
            issues.append("No events found.")
        message = f"{active_verified}/{total} verified active events."
        return {"score": score, "pixels": pixels, "issues": issues, "message": message}

    # ------------------------------------------------------------------
    # Layer 3: Writes
    # ------------------------------------------------------------------

    @friendly_errors("Pinterest Ads")
    async def update_campaign_status(
        self, access_token: str, account_id: str, campaign_id: str, status: str
    ) -> dict:
        """
        status: ACTIVE | PAUSED
        """
        payload = {"status": status}
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.patch(
                f"{_BASE}campaigns/{campaign_id}",
                headers=self._headers(access_token),
                json=payload,
            )
        err = self._check(resp, "update_campaign_status")
        if err:
            return err
        return {"campaign_id": campaign_id, "new_status": status, "updated": True}

    @friendly_errors("Pinterest Ads")
    async def update_campaign_budget(
        self, access_token: str, account_id: str, campaign_id: str, daily_budget_usd: float
    ) -> dict:
        """
        Updates campaign budget.
        """
        payload = {"daily_budget": daily_budget_usd}
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.patch(
                f"{_BASE}campaigns/{campaign_id}",
                headers=self._headers(access_token),
                json=payload,
            )
        err = self._check(resp, "update_campaign_budget")
        if err:
            return err
        return {"campaign_id": campaign_id, "new_daily_budget_usd": daily_budget_usd, "updated": True}

    @friendly_errors("Pinterest Ads")
    async def create_campaign(
        self,
        access_token: str,
        account_id: str,
        name: str,
        status: str = "ACTIVE",
        daily_budget: float | None = None,
        objective_type: str = "AWARENESS",
    ) -> dict:
        """
        Creates a new Pinterest campaign.
        status: ACTIVE | PAUSED
        daily_budget: in account currency (raw USD)
        objective_type: AWARENESS | TRAFFIC | VIDEO_VIEWS | etc.
        """
        body = {
            "name": name,
            "status": status,
            "objective_type": objective_type,
        }
        if daily_budget is not None:
            body["daily_budget"] = daily_budget

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{_BASE}ad_accounts/{account_id}/campaigns",
                headers=self._headers(access_token),
                json=body,
            )

        err = self._check(resp, "create_campaign")
        if err:
            return err

        created = resp.json()
        return {
            "campaign_id": created.get("id"),
            "campaign_name": name,
            "status": status,
            "account_id": account_id,
            "updated": True,
        }
