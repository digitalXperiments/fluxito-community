"""Reddit Ads Connector.

Uses Reddit Ads API v3 with OAuth2 bearer tokens.
API base: https://ads-api.reddit.com/api/v3

Reddit Ads API v3 field name notes:
- Campaigns: id, name, configured_status (ACTIVE | PAUSED | ARCHIVED)
- Ad groups: id, name, campaign_id, configured_status
- Reporting: GET /ad_accounts/{id}/campaigns/{cid}/stats (or /ad_groups/{id}/stats)
  Returns metrics: impressions, clicks, spend_micro_usd, total_conversions.
  spend_micro_usd is in micro-dollars (divide by 1_000_000 for USD).
- Conversion pixels: GET /ad_accounts/{id}/pixels
  Each pixel: id, name, status (ACTIVE | INACTIVE), pixel_js_status
- Budget: daily_budget_micro_usd — micro-USD units (multiply input USD by 1_000_000).

These field names reflect the documented Reddit Ads API v3; flag any discrepancies
against the live API response at integration time.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from app.connectors.errors import friendly_errors

logger = logging.getLogger(__name__)

_BASE = "https://ads-api.reddit.com/api/v3"
_USER_AGENT = "Fluxito:reddit-ads:v1.0"

# Multiplier to convert human-readable USD/currency to Reddit's micro-currency unit.
# Reddit budgets and spend values use micro-USD (1 USD = 1_000_000 micro-USD).
_MICRO = 1_000_000


class RedditAdsConnector:
    """Interfaces with Reddit Ads API using per-user OAuth2 access tokens."""

    @staticmethod
    def _headers(access_token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
        }

    @staticmethod
    def _check(resp: httpx.Response, context: str) -> dict | None:
        if resp.status_code not in (200, 201):
            logger.error(
                "Reddit Ads API error [%s] status=%s: %s",
                context,
                resp.status_code,
                resp.text[:300],
            )
            return {"error": True, "message": f"Reddit Ads API error ({resp.status_code}): {resp.text[:300]}"}
        return None

    @staticmethod
    def _list_payload(payload: Any) -> list[dict]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            return []
        for key in ("data", "items", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []

    # ------------------------------------------------------------------
    # Layer 1: Data access
    # ------------------------------------------------------------------

    @friendly_errors("Reddit Ads")
    async def list_ad_accounts(self, access_token: str) -> dict:
        """Return ad accounts visible to the authenticated Reddit user."""
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"{_BASE}/ad_accounts",
                headers=self._headers(access_token),
            )

        err = self._check(resp, "list_ad_accounts")
        if err:
            return err

        accounts_data = self._list_payload(resp.json())
        return {
            "accounts": [
                {
                    "account_id": acc.get("id") or acc.get("account_id"),
                    "name": acc.get("name") or acc.get("display_name") or acc.get("id"),
                    "currency": acc.get("currency", "USD"),
                    "status": acc.get("configured_status") or acc.get("status"),
                }
                for acc in accounts_data
            ]
        }

    @friendly_errors("Reddit Ads")
    async def get_campaign_performance(
        self,
        access_token: str,
        account_id: str,
        start_date: str,
        end_date: str,
        metrics: list[str] | None = None,
    ) -> dict:
        """Return campaign performance metrics over the given date range.

        Calls:
          GET /ad_accounts/{account_id}/campaigns
          GET /ad_accounts/{account_id}/campaigns/{campaign_id}/stats
            with ?start_time=YYYY-MM-DD&end_time=YYYY-MM-DD&granularity=TOTAL

        spend is normalised from micro-USD to USD (divided by 1_000_000).
        """
        if not (
            re.match(r"^\d{4}-\d{2}-\d{2}$", start_date)
            and re.match(r"^\d{4}-\d{2}-\d{2}$", end_date)
            and start_date <= end_date
        ):
            return {"error": True, "message": "Invalid date format or range. Use YYYY-MM-DD, start <= end."}

        metrics_fields = metrics or ["impressions", "clicks", "spend", "conversions"]

        async with httpx.AsyncClient(timeout=30) as client:
            campaigns_resp = await client.get(
                f"{_BASE}/ad_accounts/{account_id}/campaigns",
                headers=self._headers(access_token),
            )
            err = self._check(campaigns_resp, "get_campaign_performance:list")
            if err:
                return err

            campaigns = self._list_payload(campaigns_resp.json())
            stats_by_id: dict[str, dict] = {}

            for campaign in campaigns:
                cid = campaign.get("id")
                if not cid:
                    continue
                stats_resp = await client.get(
                    f"{_BASE}/ad_accounts/{account_id}/campaigns/{cid}/stats",
                    headers=self._headers(access_token),
                    params={
                        "start_time": start_date,
                        "end_time": end_date,
                        "granularity": "TOTAL",
                    },
                )
                if stats_resp.status_code == 200:
                    # Reddit stats response: {"data": {"clicks": N, "impressions": N,
                    # "spend_micro_usd": N, "total_conversions": N, ...}}
                    stats_raw = stats_resp.json()
                    inner = stats_raw.get("data") or stats_raw.get("stats") or stats_raw or {}
                    if isinstance(inner, list):
                        inner = inner[0] if inner else {}
                    spend_micro = inner.get("spend_micro_usd", 0) or 0
                    stats_by_id[cid] = {
                        "impressions": inner.get("impressions", 0),
                        "clicks": inner.get("clicks", 0),
                        "spend": round(spend_micro / _MICRO, 6),
                        "conversions": inner.get("total_conversions", 0),
                    }
                else:
                    logger.warning(
                        "Reddit Ads campaign stats failed for %s: %s %s",
                        cid,
                        stats_resp.status_code,
                        stats_resp.text[:200],
                    )

        result_campaigns = []
        for campaign in campaigns:
            cid = campaign.get("id")
            stats = stats_by_id.get(cid, dict.fromkeys(metrics_fields, 0))
            result_campaigns.append(
                {
                    "campaign_id": cid,
                    "campaign_name": campaign.get("name", "Unnamed"),
                    "status": campaign.get("configured_status") or campaign.get("status"),
                    **{field: stats.get(field, 0) for field in metrics_fields},
                }
            )

        return {
            "account_id": account_id,
            "date_range": f"{start_date} to {end_date}",
            "campaigns": result_campaigns,
        }

    @friendly_errors("Reddit Ads")
    async def get_adgroup_performance(
        self,
        access_token: str,
        account_id: str,
        start_date: str,
        end_date: str,
        campaign_id: str | None = None,
    ) -> dict:
        """Return ad group performance metrics over the given date range.

        Reddit calls these "ad groups" (X calls them "line items").

        Calls:
          GET /ad_accounts/{account_id}/ad_groups[?campaign_id=…]
          GET /ad_accounts/{account_id}/ad_groups/{adgroup_id}/stats
            with ?start_time=YYYY-MM-DD&end_time=YYYY-MM-DD&granularity=TOTAL

        spend is normalised from micro-USD to USD.
        """
        if not (
            re.match(r"^\d{4}-\d{2}-\d{2}$", start_date)
            and re.match(r"^\d{4}-\d{2}-\d{2}$", end_date)
            and start_date <= end_date
        ):
            return {"error": True, "message": "Invalid date format or range. Use YYYY-MM-DD, start <= end."}

        params: dict[str, str] = {}
        if campaign_id:
            params["campaign_id"] = campaign_id

        async with httpx.AsyncClient(timeout=30) as client:
            adgroups_resp = await client.get(
                f"{_BASE}/ad_accounts/{account_id}/ad_groups",
                headers=self._headers(access_token),
                params=params or None,
            )
            err = self._check(adgroups_resp, "get_adgroup_performance:list")
            if err:
                return err

            adgroups = self._list_payload(adgroups_resp.json())
            stats_by_id: dict[str, dict] = {}

            for ag in adgroups:
                agid = ag.get("id")
                if not agid:
                    continue
                stats_resp = await client.get(
                    f"{_BASE}/ad_accounts/{account_id}/ad_groups/{agid}/stats",
                    headers=self._headers(access_token),
                    params={
                        "start_time": start_date,
                        "end_time": end_date,
                        "granularity": "TOTAL",
                    },
                )
                if stats_resp.status_code == 200:
                    stats_raw = stats_resp.json()
                    inner = stats_raw.get("data") or stats_raw.get("stats") or stats_raw or {}
                    if isinstance(inner, list):
                        inner = inner[0] if inner else {}
                    spend_micro = inner.get("spend_micro_usd", 0) or 0
                    stats_by_id[agid] = {
                        "impressions": inner.get("impressions", 0),
                        "clicks": inner.get("clicks", 0),
                        "spend": round(spend_micro / _MICRO, 6),
                        "conversions": inner.get("total_conversions", 0),
                    }
                else:
                    logger.warning(
                        "Reddit Ads ad group stats failed for %s: %s %s",
                        agid,
                        stats_resp.status_code,
                        stats_resp.text[:200],
                    )

        result_adgroups = []
        for ag in adgroups:
            agid = ag.get("id")
            stats = stats_by_id.get(agid, {"impressions": 0, "clicks": 0, "spend": 0.0, "conversions": 0})
            result_adgroups.append(
                {
                    "adgroup_id": agid,
                    "name": ag.get("name", "Unnamed"),
                    "campaign_id": ag.get("campaign_id"),
                    "status": ag.get("configured_status") or ag.get("status"),
                    **stats,
                }
            )

        return {
            "account_id": account_id,
            "date_range": f"{start_date} to {end_date}",
            "ad_groups": result_adgroups,
        }

    # ------------------------------------------------------------------
    # Layer 2: Audit
    # ------------------------------------------------------------------

    @friendly_errors("Reddit Ads")
    async def audit_tracking_setup(self, access_token: str, account_id: str) -> dict:
        """Check whether Reddit Pixel / conversion tracking is configured.

        Calls GET /ad_accounts/{account_id}/pixels
        Each pixel: {id, name, status (ACTIVE|INACTIVE), pixel_js_status}

        Returns a score + issues list in the same shape as x_ads.audit_tracking_setup.
        Falls back to a 50-score "could not verify" result on 404 (matching x_ads behaviour).
        """
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"{_BASE}/ad_accounts/{account_id}/pixels",
                headers=self._headers(access_token),
            )

        if resp.status_code == 404:
            return {
                "score": 50,
                "pixels": [],
                "issues": [
                    {
                        "severity": "warning",
                        "issue": "Could not find Reddit Pixel / conversion events for this ad account",
                        "recommendation": (
                            "Verify the Reddit Pixel is installed and the token has analytics access."
                        ),
                    }
                ],
            }

        err = self._check(resp, "audit_tracking_setup")
        if err:
            return err

        pixels_data = self._list_payload(resp.json())
        pixels = [
            {
                "id": p.get("id"),
                "name": p.get("name", "Unnamed"),
                # status values: ACTIVE | INACTIVE (Reddit Ads API v3)
                "status": p.get("status", "UNKNOWN"),
                # pixel_js_status indicates whether the pixel JS has fired recently
                "pixel_js_status": p.get("pixel_js_status", "UNKNOWN"),
            }
            for p in pixels_data
        ]

        active_pixels = [p for p in pixels if p["status"] == "ACTIVE"]
        issues: list[dict] = []

        if not pixels:
            issues.append(
                {
                    "severity": "critical",
                    "issue": "No Reddit Pixels found for this ad account",
                    "recommendation": (
                        "Create and install a Reddit Pixel before optimising campaigns for conversions."
                    ),
                }
            )
        elif not active_pixels:
            issues.append(
                {
                    "severity": "high",
                    "issue": "Reddit Pixel exists but none are ACTIVE",
                    "recommendation": "Activate the pixel and verify the pixel JS is firing on your site.",
                }
            )

        unfired = [p for p in active_pixels if p.get("pixel_js_status") not in ("VERIFIED", "ACTIVE", "OK")]
        if active_pixels and unfired:
            issues.append(
                {
                    "severity": "warning",
                    "issue": f"{len(unfired)} active pixel(s) have not been verified as firing",
                    "recommendation": "Check pixel installation with the Reddit Pixel Helper browser extension.",
                }
            )

        score = 100 if active_pixels and not unfired else 75 if active_pixels else 0 if not pixels else 30

        return {
            "score": score,
            "tag_count": len(pixels),
            "pixels": pixels,
            "issues": issues,
        }

    # ------------------------------------------------------------------
    # Layer 3: Writes
    # ------------------------------------------------------------------

    @friendly_errors("Reddit Ads")
    async def update_campaign_status(
        self,
        access_token: str,
        account_id: str,
        campaign_id: str,
        status: str,
    ) -> dict:
        """Set a campaign's configured_status to ACTIVE or PAUSED.

        Reddit uses the field name `configured_status` (not `entity_status`).
        Valid values: ACTIVE | PAUSED.
        Calls PATCH /ad_accounts/{account_id}/campaigns/{campaign_id}
        """
        normalized = status.upper()
        if normalized not in {"ACTIVE", "PAUSED"}:
            return {"error": True, "message": "status must be ACTIVE or PAUSED"}

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.patch(
                f"{_BASE}/ad_accounts/{account_id}/campaigns/{campaign_id}",
                headers=self._headers(access_token),
                json={"configured_status": normalized},
            )

        err = self._check(resp, "update_campaign_status")
        if err:
            return err

        return {
            "campaign_id": campaign_id,
            "account_id": account_id,
            "new_status": normalized,
            "updated": True,
        }

    @friendly_errors("Reddit Ads")
    async def update_campaign_budget(
        self,
        access_token: str,
        account_id: str,
        campaign_id: str,
        daily_budget: float,
    ) -> dict:
        """Set the campaign's daily budget.

        Reddit budgets are expressed in micro-currency (micro-USD by default):
          1 USD = 1_000_000 micro-USD  (_MICRO constant above).
        Pass `daily_budget` as a plain numeric value in the account's native
        currency (e.g. 50.0 for $50/day); this method converts to micro-currency
        before sending.

        Calls PATCH /ad_accounts/{account_id}/campaigns/{campaign_id}
        with body {"daily_budget_micro_usd": <value in micro-USD>}
        """
        if daily_budget <= 0:
            return {"error": True, "message": "daily_budget must be a positive number"}

        daily_budget_micro = int(round(daily_budget * _MICRO))

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.patch(
                f"{_BASE}/ad_accounts/{account_id}/campaigns/{campaign_id}",
                headers=self._headers(access_token),
                json={"daily_budget_micro_usd": daily_budget_micro},
            )

        err = self._check(resp, "update_campaign_budget")
        if err:
            return err

        return {
            "campaign_id": campaign_id,
            "account_id": account_id,
            "new_daily_budget": daily_budget,
            "updated": True,
        }

    @friendly_errors("Reddit Ads")
    async def create_campaign(
        self,
        access_token: str,
        account_id: str,
        name: str,
        daily_budget: float,
        configured_status: str = "PAUSED",
    ) -> dict:
        """
        Creates a new Reddit campaign.
        daily_budget: in account currency (converted to micro-USD)
        configured_status: ACTIVE | PAUSED
        """
        normalized = configured_status.upper()
        if normalized not in {"ACTIVE", "PAUSED"}:
            return {"error": True, "message": "configured_status must be ACTIVE or PAUSED"}
        if daily_budget <= 0:
            return {"error": True, "message": "daily_budget must be a positive number"}

        daily_budget_micro = int(round(daily_budget * _MICRO))

        body = {
            "name": name,
            "configured_status": normalized,
            "daily_budget_micro_usd": daily_budget_micro,
            "ad_account_id": account_id,
        }

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{_BASE}/ad_accounts/{account_id}/campaigns",
                headers=self._headers(access_token),
                json=body,
            )

        err = self._check(resp, "create_campaign")
        if err:
            return err

        created = resp.json().get("data", {})
        return {
            "campaign_id": created.get("id"),
            "campaign_name": name,
            "configured_status": normalized,
            "daily_budget": daily_budget,
            "account_id": account_id,
            "updated": True,
        }
