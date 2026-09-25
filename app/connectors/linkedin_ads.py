"""
LinkedIn Ads Connector

Uses the LinkedIn Marketing API v2 via httpx.
API base: https://api.linkedin.com/rest

Layer 1: list_accounts, get_campaign_performance, get_adgroup_performance
Layer 2: audit_tracking_setup (basic Insight Tag health)
Layer 3: update_campaign_status, update_campaign_budget
"""

import logging

import httpx

from app.connectors.errors import friendly_errors

logger = logging.getLogger(__name__)

_BASE = "https://api.linkedin.com/rest"


class LinkedInAdsConnector:
    """Interfaces with LinkedIn Marketing API using per-user OAuth2 access tokens."""

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _headers(access_token: str) -> dict:
        return {
            "Authorization": f"Bearer {access_token}",
            "Linkedin-Version": "202408",  # Latest stable
            "Content-Type": "application/json",
        }

    @staticmethod
    def _check(resp: httpx.Response, context: str) -> dict | None:
        """Return error dict if response is not 2xx, else None."""
        if resp.status_code not in (200, 201):
            logger.error(f"LinkedIn API error [{context}] status={resp.status_code}: {resp.text[:300]}")
            return {"error": True, "message": f"LinkedIn API error ({resp.status_code}): {resp.text[:300]}"}
        return None

    # ------------------------------------------------------------------
    # Layer 1: Data access
    # ------------------------------------------------------------------

    @friendly_errors("LinkedIn Ads")
    async def list_accounts(self, access_token: str) -> dict:
        """
        Returns all ad accounts accessible to this user.
        Calls GET /adAccounts?q=roleAssignee
        """
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"{_BASE}/adAccounts?q=roleAssignee",
                headers=self._headers(access_token),
            )

        err = self._check(resp, "list_accounts")
        if err:
            return err

        accounts_data = resp.json().get("elements", [])
        return {
            "accounts": [
                {
                    "account_id": acc.get("id"),
                    "name": acc.get("name"),
                    "currency": acc.get("currency", "USD"),
                    "status": acc.get("accountStatus", "ACTIVE"),
                }
                for acc in accounts_data
            ]
        }

    @friendly_errors("LinkedIn Ads")
    async def get_campaign_performance(
        self,
        access_token: str,
        account_id: str,
        start_date: str,
        end_date: str,
        metrics: list[str] | None = None,
    ) -> dict:
        """
        Returns campaign (CampaignGroup) performance.
        Calls GET /adAccounts/{id}/campaignGroups then POST /adAnalyticsV2 for insights.
        """
        import re

        if not (
            re.match(r"^\d{4}-\d{2}-\d{2}$", start_date)
            and re.match(r"^\d{4}-\d{2}-\d{2}$", end_date)
            and start_date <= end_date
        ):
            return {"error": True, "message": "Invalid date format or range. Use YYYY-MM-DD, start <= end."}

        DEFAULT_METRICS = ["impressions", "clicks", "cost", "conversions"]
        METRIC_MAP = {"cost": "cost_in_local_currency"}
        series = [METRIC_MAP.get(m, m) for m in (metrics or DEFAULT_METRICS)]

        # List campaigns
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f'{_BASE}/adAccounts/{account_id}/campaignGroups?q=search&search={{"campaignGroupStatuses":["ACTIVE"]}}',
                headers=self._headers(access_token),
            )

        err = self._check(resp, "get_campaign_performance:list")
        if err:
            return err

        campaigns = resp.json().get("elements", [])
        campaign_ids = [c["id"] for c in campaigns]

        # Fetch insights (aggregate)
        insights = {}
        if campaign_ids:
            insights_body = {
                "pivots": [{"dimensions": [{"column": "campaignGroup"}]}],
                "dateRange": {"start": start_date, "end": end_date},
                "series": series,
            }
            async with httpx.AsyncClient(timeout=30) as client:
                insights_resp = await client.post(
                    f"{_BASE}/adAnalyticsV2?q=analytics",
                    headers=self._headers(access_token),
                    json=insights_body,
                )
            if insights_resp.status_code == 200:
                insight_data = insights_resp.json().get("elements", [])
                for row in insight_data:
                    pivot = row.get("pivotValues", [{}])[0]
                    cid = pivot.get("value", {}).get("campaignGroup", "unknown")
                    data = row.get("data", [])
                    agg = {}
                    for s in series:
                        orig_metric = (
                            next(k for k, v in METRIC_MAP.items() if v == s)
                            if s in METRIC_MAP.values()
                            else s
                        )
                        agg[orig_metric] = sum(d.get(s, {}).get("value", 0) for d in data)
                    insights[cid] = agg

        result_campaigns = []
        for c in campaigns:
            cid = c["id"]
            agg = insights.get(cid, dict.fromkeys(DEFAULT_METRICS, 0))
            result_campaigns.append(
                {
                    "campaign_id": cid,
                    "campaign_name": c.get("name", "Unnamed"),
                    "status": c.get("status", "ACTIVE"),
                    **{k: round(v, 2) if isinstance(v, float) else v for k, v in agg.items()},
                }
            )

        return {
            "account_id": account_id,
            "date_range": f"{start_date} to {end_date}",
            "campaigns": result_campaigns,
        }

    @friendly_errors("LinkedIn Ads")
    async def get_adgroup_performance(
        self,
        access_token: str,
        account_id: str,
        start_date: str,
        end_date: str,
        campaign_id: str | None = None,
    ) -> dict:
        """
        Returns ad group (adGroup) performance.
        """
        import re

        if not (
            re.match(r"^\d{4}-\d{2}-\d{2}$", start_date)
            and re.match(r"^\d{4}-\d{2}-\d{2}$", end_date)
            and start_date <= end_date
        ):
            return {"error": True, "message": "Invalid date format or range. Use YYYY-MM-DD, start <= end."}

        DEFAULT_METRICS = ["impressions", "clicks", "cost", "conversions"]
        METRIC_MAP = {"cost": "cost_in_local_currency"}
        series = [METRIC_MAP.get(m, m) for m in DEFAULT_METRICS]  # Fixed for now

        params = {"q": "roleAssignee"}
        if campaign_id:
            params["campaignGroups"] = campaign_id

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"{_BASE}/adAccounts/{account_id}/adGroups",
                headers=self._headers(access_token),
                params=params,
            )

        err = self._check(resp, "get_adgroup_performance:list")
        if err:
            return err

        adgroups = resp.json().get("elements", [])
        adgroup_ids = [ag["id"] for ag in adgroups]

        insights = {}
        if adgroup_ids:
            insights_body = {
                "pivots": [{"dimensions": [{"column": "adGroup"}]}],
                "dateRange": {"start": start_date, "end": end_date},
                "series": series,
            }
            async with httpx.AsyncClient(timeout=30) as client:
                insights_resp = await client.post(
                    f"{_BASE}/adAnalyticsV2?q=analytics",
                    headers=self._headers(access_token),
                    json=insights_body,
                )
            if insights_resp.status_code == 200:
                insight_data = insights_resp.json().get("elements", [])
                for row in insight_data:
                    pivot = row.get("pivotValues", [{}])[0]
                    gid = pivot.get("value", {}).get("adGroup", "unknown")
                    data = row.get("data", [])
                    agg = {}
                    for s in series:
                        orig_metric = next((k for k, v in METRIC_MAP.items() if v == s), s)
                        agg[orig_metric] = sum(d.get(s, {}).get("value", 0) for d in data)
                    insights[gid] = agg

        result_adgroups = []
        for ag in adgroups:
            gid = ag["id"]
            agg = insights.get(gid, dict.fromkeys(DEFAULT_METRICS, 0))
            result_adgroups.append(
                {
                    "adgroup_id": gid,
                    "name": ag.get("name", "Unnamed"),
                    "status": ag.get("status", "ACTIVE"),
                    **{k: round(v, 2) if isinstance(v, float) else v for k, v in agg.items()},
                }
            )

        return {
            "account_id": account_id,
            "date_range": f"{start_date} to {end_date}",
            "adgroups": result_adgroups,
        }

    # ------------------------------------------------------------------
    # Layer 2: Audit
    # ------------------------------------------------------------------

    @friendly_errors("LinkedIn Ads")
    async def audit_tracking_setup(self, access_token: str, account_id: str) -> dict:
        """
        Basic LinkedIn Insight Tag audit.
        """
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"{_BASE}/adAccounts/{account_id}/insightTags?q=roleAssignee",
                headers=self._headers(access_token),
            )
        err = self._check(resp, "audit_tracking_setup")
        if err:
            return err
        tags_data = resp.json().get("elements", [])
        tags = [
            {
                "id": t.get("id"),
                "name": t.get("name", "Unnamed"),
                "status": t.get("status", "UNKNOWN"),
            }
            for t in tags_data
        ]
        active_count = sum(1 for t in tags if t["status"] == "ACTIVE")
        total_tags = len(tags)
        score = 100 if active_count > 0 else 60 if total_tags > 0 else 20
        issues = []
        if active_count == 0 and total_tags > 0:
            issues.append("No active Insight Tags.")
        if total_tags == 0:
            issues.append("No Insight Tags found.")
        message = f"{active_count}/{total_tags} active Insight Tags."
        return {"score": score, "pixels": tags, "issues": issues, "message": message}

    # ------------------------------------------------------------------
    # Layer 3: Writes
    # ------------------------------------------------------------------

    @friendly_errors("LinkedIn Ads")
    async def update_campaign_status(
        self, access_token: str, account_id: str, campaign_id: str, status: str
    ) -> dict:
        """
        status: ACTIVE | PAUSED
        """
        payload = {"patch": {"$set": {"status": status}}}
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.put(
                f"{_BASE}/campaignGroups/{campaign_id}",
                headers=self._headers(access_token),
                json=payload,
            )
        err = self._check(resp, "update_campaign_status")
        if err:
            return err
        return {"campaign_id": campaign_id, "new_status": status, "updated": True}

    @friendly_errors("LinkedIn Ads")
    async def update_campaign_budget(
        self, access_token: str, account_id: str, campaign_id: str, daily_budget_usd: float
    ) -> dict:
        """
        Updates campaign budget.
        """
        payload = {"patch": {"$set": {"budget": daily_budget_usd}}}
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.put(
                f"{_BASE}/campaignGroups/{campaign_id}",
                headers=self._headers(access_token),
                json=payload,
            )
        err = self._check(resp, "update_campaign_budget")
        if err:
            return err
        return {"campaign_id": campaign_id, "new_daily_budget_usd": daily_budget_usd, "updated": True}

    @friendly_errors("LinkedIn Ads")
    async def create_campaign(
        self,
        access_token: str,
        account_id: str,
        name: str,
        status: str = "ACTIVE",
        daily_budget: float | None = None,
        objective_type: str = "BRAND_AWARENESS",
    ) -> dict:
        """
        Creates a new LinkedIn campaign.
        status: ACTIVE | PAUSED
        daily_budget: in account currency (raw USD)
        objective_type: BRAND_AWARENESS | WEBSITE_VISITS | LEAD_GENERATION | etc.
        """
        account_urn = f"urn:li:sponsoredAccount:{account_id}"
        body = {
            "account": account_urn,
            "name": name,
            "status": status,
            "objectiveType": objective_type,
        }
        if daily_budget is not None:
            body["dailyBudget"] = {"amount": daily_budget}

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{_BASE}/adCampaignsV2",
                headers=self._headers(access_token),
                json=body,
            )

        err = self._check(resp, "create_campaign")
        if err:
            return err

        data = resp.json()
        created = data.get("elements", [{}])[0] if data.get("elements") else {}
        return {
            "campaign_id": created.get("id"),
            "campaign_name": name,
            "status": status,
            "account_id": account_id,
            "updated": True,
        }
