"""Apple Ads Connector.

Uses the Apple Ads Campaign Management API v5 with OAuth2 client credentials.
API base: https://api.searchads.apple.com/api/v5
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from app.connectors.errors import friendly_errors

logger = logging.getLogger(__name__)

_BASE = "https://api.searchads.apple.com/api/v5"
_TOKEN_URL = "https://appleid.apple.com/auth/oauth2/token"


class AppleAdsConnector:
    """Interfaces with Apple Ads using OAuth2 bearer access tokens."""

    @staticmethod
    def _headers(access_token: str, org_id: str | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        if org_id:
            headers["X-AP-Context"] = f"orgId={org_id}"
        return headers

    @staticmethod
    def _check(resp: httpx.Response, context: str) -> dict | None:
        if resp.status_code not in (200, 201):
            logger.error(
                "Apple Ads API error [%s] status=%s: %s",
                context,
                resp.status_code,
                resp.text[:300],
            )
            return {"error": True, "message": f"Apple Ads API error ({resp.status_code}): {resp.text[:300]}"}
        return None

    @staticmethod
    def _list_payload(payload: Any) -> list[dict]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            return []
        data = payload.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            return [data]
        for key in ("items", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []

    @staticmethod
    def _valid_dates(start_date: str, end_date: str) -> bool:
        return (
            bool(re.match(r"^\d{4}-\d{2}-\d{2}$", start_date))
            and bool(re.match(r"^\d{4}-\d{2}-\d{2}$", end_date))
            and start_date <= end_date
        )

    @staticmethod
    def _report_request(start_date: str, end_date: str, limit: int = 1000) -> dict:
        return {
            "startTime": start_date,
            "endTime": end_date,
            "granularity": "SUMMARY",
            "selector": {
                "orderBy": [{"field": "localSpend", "sortOrder": "DESCENDING"}],
                "pagination": {"offset": 0, "limit": limit},
            },
            "returnRecordsWithNoMetrics": True,
            "returnRowTotals": True,
            "returnGrandTotals": True,
        }

    @staticmethod
    def _report_rows(payload: dict) -> list[dict]:
        data = payload.get("data") if isinstance(payload, dict) else {}
        if isinstance(data, dict):
            response = data.get("reportingDataResponse") or data
        else:
            response = payload.get("reportingDataResponse", {}) if isinstance(payload, dict) else {}
        rows = response.get("row", []) if isinstance(response, dict) else []
        return [row for row in rows if isinstance(row, dict)]

    @staticmethod
    def _metric_value(row: dict, key: str, default: int | float = 0) -> Any:
        total = row.get("total")
        if isinstance(total, dict) and key in total:
            return total.get(key, default)
        granularity = row.get("granularity")
        if isinstance(granularity, list) and granularity:
            first = granularity[0]
            if isinstance(first, dict):
                return first.get(key, default)
        return row.get(key, default)

    @classmethod
    def _spend(cls, row: dict) -> float:
        value = cls._metric_value(row, "localSpend", 0)
        if isinstance(value, dict):
            value = value.get("amount", 0)
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    async def request_access_token(self, client_id: str, client_secret: str) -> dict:
        """Exchange Apple Ads client credentials for an access token."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                _TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "scope": "searchadsorg",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if resp.status_code != 200:
            return {
                "error": True,
                "message": f"Apple Ads OAuth token exchange failed ({resp.status_code}): {resp.text[:300]}",
            }
        return resp.json()

    @friendly_errors("Apple Ads")
    async def list_accounts(self, access_token: str) -> dict:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(f"{_BASE}/acls", headers=self._headers(access_token))

        err = self._check(resp, "list_accounts")
        if err:
            return err

        accounts_data = self._list_payload(resp.json())
        return {
            "accounts": [
                {
                    "account_id": str(acc.get("orgId") or acc.get("id") or ""),
                    "name": acc.get("orgName") or acc.get("displayName") or str(acc.get("orgId") or ""),
                    "currency": acc.get("currency"),
                    "timezone": acc.get("timeZone"),
                    "roles": acc.get("roleNames") or [],
                }
                for acc in accounts_data
            ]
        }

    @friendly_errors("Apple Ads")
    async def get_campaign_performance(
        self,
        access_token: str,
        org_id: str,
        start_date: str,
        end_date: str,
        metrics: list[str] | None = None,
    ) -> dict:
        if not self._valid_dates(start_date, end_date):
            return {"error": True, "message": "Invalid date format or range. Use YYYY-MM-DD, start <= end."}

        metrics_fields = metrics or ["impressions", "clicks", "spend", "conversions"]
        async with httpx.AsyncClient(timeout=30) as client:
            campaigns_resp = await client.get(
                f"{_BASE}/campaigns",
                headers=self._headers(access_token, org_id),
            )
            err = self._check(campaigns_resp, "get_campaign_performance:list")
            if err:
                return err

            campaigns = self._list_payload(campaigns_resp.json())
            report_resp = await client.post(
                f"{_BASE}/reports/campaigns",
                headers=self._headers(access_token, org_id),
                json=self._report_request(start_date, end_date),
            )

        stats_by_id: dict[str, dict] = {}
        if report_resp.status_code == 200:
            for row in self._report_rows(report_resp.json()):
                meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
                cid = meta.get("campaignId") or row.get("campaignId")
                if cid is None:
                    continue
                stats_by_id[str(cid)] = {
                    "campaign_name": meta.get("campaignName"),
                    "impressions": self._metric_value(row, "impressions", 0),
                    "clicks": self._metric_value(row, "taps", 0),
                    "spend": self._spend(row),
                    "conversions": self._metric_value(row, "installs", 0),
                }
        else:
            logger.warning(
                "Apple Ads campaign report failed: %s %s", report_resp.status_code, report_resp.text[:200]
            )

        result_campaigns = []
        for campaign in campaigns:
            cid = str(campaign.get("id") or campaign.get("campaignId") or "")
            stats = stats_by_id.get(cid, {})
            result_campaigns.append(
                {
                    "campaign_id": cid,
                    "campaign_name": campaign.get("name") or stats.get("campaign_name") or "Unnamed",
                    "status": campaign.get("status") or campaign.get("servingStatus"),
                    **{field: stats.get(field, 0) for field in metrics_fields},
                }
            )

        return {
            "account_id": str(org_id),
            "date_range": f"{start_date} to {end_date}",
            "campaigns": result_campaigns,
        }

    @friendly_errors("Apple Ads")
    async def get_adgroup_performance(
        self,
        access_token: str,
        org_id: str,
        start_date: str,
        end_date: str,
        campaign_id: str | None = None,
    ) -> dict:
        if not campaign_id:
            return {"error": True, "message": "campaign_id is required for Apple Ads ad group performance"}
        if not self._valid_dates(start_date, end_date):
            return {"error": True, "message": "Invalid date format or range. Use YYYY-MM-DD, start <= end."}

        async with httpx.AsyncClient(timeout=30) as client:
            adgroups_resp = await client.get(
                f"{_BASE}/campaigns/{campaign_id}/adgroups",
                headers=self._headers(access_token, org_id),
            )
            err = self._check(adgroups_resp, "get_adgroup_performance:list")
            if err:
                return err
            adgroups = self._list_payload(adgroups_resp.json())
            report_resp = await client.post(
                f"{_BASE}/reports/campaigns/{campaign_id}/adgroups",
                headers=self._headers(access_token, org_id),
                json=self._report_request(start_date, end_date),
            )

        stats_by_id: dict[str, dict] = {}
        if report_resp.status_code == 200:
            for row in self._report_rows(report_resp.json()):
                meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
                adgroup_id = meta.get("adGroupId") or row.get("adGroupId")
                if adgroup_id is None:
                    continue
                stats_by_id[str(adgroup_id)] = {
                    "impressions": self._metric_value(row, "impressions", 0),
                    "clicks": self._metric_value(row, "taps", 0),
                    "spend": self._spend(row),
                    "conversions": self._metric_value(row, "installs", 0),
                }

        return {
            "account_id": str(org_id),
            "campaign_id": str(campaign_id),
            "date_range": f"{start_date} to {end_date}",
            "ad_groups": [
                {
                    "adgroup_id": str(ag.get("id") or ag.get("adGroupId") or ""),
                    "name": ag.get("name", "Unnamed"),
                    "campaign_id": str(ag.get("campaignId") or campaign_id),
                    "status": ag.get("status") or ag.get("servingStatus"),
                    **stats_by_id.get(
                        str(ag.get("id") or ag.get("adGroupId") or ""),
                        {"impressions": 0, "clicks": 0, "spend": 0.0, "conversions": 0},
                    ),
                }
                for ag in adgroups
            ],
        }

    @friendly_errors("Apple Ads")
    async def audit_tracking_setup(self, access_token: str, org_id: str) -> dict:
        """
        Audit Apple Search Ads tracking & campaign configuration.
        Queries live campaigns, inspects status, budget allocation, and attribution readiness.
        """
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{_BASE}/campaigns",
                headers=self._headers(access_token, org_id),
                params={"limit": 100},
            )
            err = self._check(resp, "audit_tracking_setup:campaigns")
            if err:
                return err

        data = resp.json().get("data", [])
        campaigns = data if isinstance(data, list) else []

        issues = []
        enabled_count = 0
        unbudgeted_count = 0

        for c in campaigns:
            status = str(c.get("status") or c.get("servingStatus") or "").upper()
            if status in {"ENABLED", "RUNNING"}:
                enabled_count += 1
                budget = c.get("dailyBudgetAmount")
                if not budget or not isinstance(budget, dict) or not budget.get("amount"):
                    unbudgeted_count += 1

        if not campaigns:
            issues.append(
                {
                    "severity": "warning",
                    "issue": "No campaigns found in Apple Ads account",
                    "recommendation": "Create and enable at least one search campaign.",
                }
            )
        elif enabled_count == 0:
            issues.append(
                {
                    "severity": "warning",
                    "issue": "All Apple Ads campaigns are paused or inactive",
                    "recommendation": "Enable active campaigns to start receiving attribution data.",
                }
            )

        if unbudgeted_count > 0:
            issues.append(
                {
                    "severity": "warning",
                    "issue": f"{unbudgeted_count} enabled campaign(s) lack explicit daily budgets",
                    "recommendation": "Set dailyBudgetAmount to ensure consistent ad delivery and impression attribution.",
                }
            )

        issues.append(
            {
                "severity": "info",
                "issue": "Apple Search Ads attribution is mediated via App Store Connect / AdServices framework",
                "recommendation": "Ensure your app integrates the Apple AdServices framework (or MMP SDK) to attribute Apple Search Ads installs.",
            }
        )

        score = 100
        for iss in issues:
            if iss["severity"] == "critical":
                score -= 25
            elif iss["severity"] == "warning":
                score -= 15

        return {
            "score": max(0, score),
            "account_id": str(org_id),
            "total_campaigns": len(campaigns),
            "active_campaigns": enabled_count,
            "issues": issues,
        }

    @friendly_errors("Apple Ads")
    async def update_campaign_status(
        self,
        access_token: str,
        org_id: str,
        campaign_id: str,
        status: str,
    ) -> dict:
        normalized = status.upper()
        if normalized not in {"ENABLED", "PAUSED"}:
            return {"error": True, "message": "status must be ENABLED or PAUSED"}

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.put(
                f"{_BASE}/campaigns/{campaign_id}",
                headers=self._headers(access_token, org_id),
                json={"status": normalized},
            )

        err = self._check(resp, "update_campaign_status")
        if err:
            return err
        return {
            "campaign_id": campaign_id,
            "account_id": str(org_id),
            "new_status": normalized,
            "updated": True,
        }

    @friendly_errors("Apple Ads")
    async def create_campaign(
        self,
        access_token: str,
        org_id: str,
        name: str,
        status: str = "PAUSED",
        daily_budget: float | None = None,
        display_name: str | None = None,
    ) -> dict:
        """
        Creates a new Apple Ads campaign.
        status: ENABLED | PAUSED
        daily_budget: in account currency
        """
        normalized = status.upper()
        if normalized not in {"ENABLED", "PAUSED"}:
            return {"error": True, "message": "status must be ENABLED or PAUSED"}

        body = {
            "name": name,
            "status": normalized,
            "displayName": display_name or name,
        }
        if daily_budget is not None:
            body["dailyBudgetAmount"] = {
                "amount": str(daily_budget),
                "currency": "USD",
            }

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{_BASE}/campaigns",
                headers=self._headers(access_token, org_id),
                json=body,
            )

        err = self._check(resp, "create_campaign")
        if err:
            return err

        created = resp.json().get("data", {})
        return {
            "campaign_id": str(created.get("id") or created.get("campaignId") or ""),
            "campaign_name": name,
            "status": normalized,
            "account_id": str(org_id),
            "updated": True,
        }

    @friendly_errors("Apple Ads")
    async def update_campaign_budget(
        self,
        access_token: str,
        org_id: str,
        campaign_id: str,
        daily_budget: float,
    ) -> dict:
        """
        Updates an Apple Ads campaign's daily budget.
        daily_budget: in account currency
        """
        body = {
            "dailyBudgetAmount": {
                "amount": str(daily_budget),
                "currency": "USD",
            }
        }

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.put(
                f"{_BASE}/campaigns/{campaign_id}",
                headers=self._headers(access_token, org_id),
                json=body,
            )

        err = self._check(resp, "update_campaign_budget")
        if err:
            return err

        return {
            "campaign_id": campaign_id,
            "account_id": str(org_id),
            "new_daily_budget": daily_budget,
            "updated": True,
        }
