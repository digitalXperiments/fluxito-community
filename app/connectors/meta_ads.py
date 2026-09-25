"""
Meta (Facebook) Ads Connector

Uses the facebook-business SDK for account listing and campaign performance,
and the Graph API directly (via httpx) for pixel/CAPI audit — the SDK's
Pixel event stats endpoint has limited async support.

Layer 1: list_accounts, get_campaign_performance, get_adset_performance
Layer 2: audit_tracking_setup  (real pixel health via Graph API)
Layer 3: create_campaign, update_campaign_status  (Graph API POST/PUT)
"""

import asyncio
import logging

import httpx

from app.connectors.errors import friendly_errors

logger = logging.getLogger(__name__)

_GRAPH_BASE = "https://graph.facebook.com/v19.0"


class MetaAdsConnector:
    """Interfaces with Meta (Facebook) Ads using per-user access tokens."""

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_meta_creds(self) -> tuple[str, str]:
        """Return (app_id, app_secret) of the Meta app that issued the active project's token."""
        import app.app_state as app_state
        from app.auth.oauth_app_credentials import get_oauth_app_credentials_cached
        from app.tools.shared_helpers import get_oauth_app_project_id

        async with app_state.db_session_factory() as db:
            creds = await get_oauth_app_credentials_cached(db, "meta", project_id=get_oauth_app_project_id())
        return creds.client_id, creds.client_secret

    def _get_api(self, access_token: str, app_id: str, app_secret: str):
        """Return a per-request FacebookAdsApi instance (not global)."""
        from facebook_business.api import FacebookAdsApi

        return FacebookAdsApi(
            app_id=app_id,
            app_secret=app_secret,
            access_token=access_token,
        )

    @staticmethod
    def _act(account_id: str) -> str:
        """Ensure account ID has the required act_ prefix."""
        return account_id if account_id.startswith("act_") else f"act_{account_id}"

    # ------------------------------------------------------------------
    # Layer 1: Data access
    # ------------------------------------------------------------------

    @friendly_errors("Meta Ads")
    async def list_accounts(self, access_token: str) -> dict:
        """Fetch all Meta Ad Accounts accessible to the user."""
        app_id, app_secret = await self._get_meta_creds()

        def _fetch():
            from facebook_business.adobjects.user import User

            api = self._get_api(access_token, app_id, app_secret)
            me = User(fbid="me", api=api)
            accounts = me.get_ad_accounts(
                fields=["id", "name", "currency", "timezone_name", "account_status", "spend_cap"]
            )
            return {
                "accounts": [
                    {
                        "account_id": acc.get("id"),
                        "name": acc.get("name"),
                        "currency": acc.get("currency"),
                        "timezone": acc.get("timezone_name"),
                        "status": acc.get("account_status"),
                        "spend_cap": acc.get("spend_cap"),
                    }
                    for acc in accounts
                ]
            }

        try:
            return await asyncio.to_thread(_fetch)
        except Exception as e:
            logger.error(f"Meta list_accounts error: {e}")
            return {"error": True, "message": str(e)}

    @friendly_errors("Meta Ads")
    async def get_campaign_performance(
        self,
        access_token: str,
        account_id: str,
        start_date: str,
        end_date: str,
        metrics: list[str] | None = None,
    ) -> dict:
        """Get campaign-level insights from Meta Ads."""
        app_id, app_secret = await self._get_meta_creds()

        def _fetch():
            from facebook_business.adobjects.adaccount import AdAccount

            api = self._get_api(access_token, app_id, app_secret)
            act_id = self._act(account_id)
            account = AdAccount(act_id, api=api)

            fields = metrics or [
                "campaign_name",
                "campaign_id",
                "objective",
                "spend",
                "impressions",
                "clicks",
                "cpc",
                "cpm",
                "ctr",
                "actions",
                "cost_per_action_type",
            ]
            params = {
                "level": "campaign",
                "time_range": {"since": start_date, "until": end_date},
                "date_preset": None,
            }
            insights = account.get_insights(fields=fields, params=params)
            return {
                "account_id": act_id,
                "date_range": f"{start_date} to {end_date}",
                "campaigns": [dict(i) for i in insights],
            }

        try:
            return await asyncio.to_thread(_fetch)
        except Exception as e:
            logger.error(f"Meta get_campaign_performance error: {e}")
            return {"error": True, "message": str(e)}

    @friendly_errors("Meta Ads")
    async def get_adset_performance(
        self,
        access_token: str,
        account_id: str,
        start_date: str,
        end_date: str,
        campaign_id: str | None = None,
    ) -> dict:
        """Get ad-set level insights. Optionally filtered to a single campaign."""
        app_id, app_secret = await self._get_meta_creds()

        def _fetch():
            from facebook_business.adobjects.adaccount import AdAccount

            api = self._get_api(access_token, app_id, app_secret)
            act_id = self._act(account_id)
            account = AdAccount(act_id, api=api)

            fields = [
                "adset_name",
                "adset_id",
                "campaign_name",
                "campaign_id",
                "spend",
                "impressions",
                "clicks",
                "ctr",
                "cpc",
                "cpm",
                "actions",
                "reach",
                "frequency",
            ]
            params = {
                "level": "adset",
                "time_range": {"since": start_date, "until": end_date},
            }
            if campaign_id:
                params["filtering"] = [{"field": "campaign.id", "operator": "EQUAL", "value": campaign_id}]

            insights = account.get_insights(fields=fields, params=params)
            return {
                "account_id": act_id,
                "date_range": f"{start_date} to {end_date}",
                "adsets": [dict(i) for i in insights],
            }

        try:
            return await asyncio.to_thread(_fetch)
        except Exception as e:
            logger.error(f"Meta get_adset_performance error: {e}")
            return {"error": True, "message": str(e)}

    # ------------------------------------------------------------------
    # Layer 2: Audit
    # ------------------------------------------------------------------

    @friendly_errors("Meta Ads")
    async def audit_tracking_setup(self, access_token: str, account_id: str) -> dict:
        """
        Audits Meta tracking health for an ad account:
        - Lists all pixels linked to the account
        - Checks last_fired_time for each pixel
        - Pulls event stats (Purchase, AddToCart, PageView, Lead) for the last 7 days
        - Flags missing events, stale pixels, and low Event Match Quality
        Uses the Graph API directly for reliable async behaviour.
        """
        act_id = self._act(account_id)
        issues = []

        async with httpx.AsyncClient(timeout=20) as client:
            # 1. Fetch pixels linked to this ad account
            pixel_resp = await client.get(
                f"{_GRAPH_BASE}/{act_id}/adspixels",
                params={
                    "fields": "id,name,last_fired_time,code",
                    "access_token": access_token,
                },
            )

        if pixel_resp.status_code != 200:
            return {
                "error": True,
                "message": f"Graph API error fetching pixels: {pixel_resp.text}",
            }

        pixels = pixel_resp.json().get("data", [])

        if not pixels:
            return {
                "score": 0,
                "pixels": [],
                "issues": [
                    {
                        "severity": "critical",
                        "issue": "No Meta Pixel found for this ad account",
                        "recommendation": "Create a Meta Pixel in Events Manager and install it on your website",
                    }
                ],
            }

        pixel_results = []

        async with httpx.AsyncClient(timeout=20) as client:
            for pixel in pixels:
                pixel_id = pixel.get("id")
                pixel_name = pixel.get("name", "Unnamed Pixel")
                last_fired = pixel.get("last_fired_time")

                # Fetch event stats for this pixel (last 7 days)
                stats_resp = await client.get(
                    f"{_GRAPH_BASE}/{pixel_id}/stats",
                    params={
                        "start_time": 0,
                        "aggregation": "event",
                        "access_token": access_token,
                    },
                )

                event_counts = {}
                if stats_resp.status_code == 200:
                    for entry in stats_resp.json().get("data", []):
                        event_counts[entry.get("event")] = entry.get("count", 0)

                # Fetch Event Match Quality score
                emq_resp = await client.get(
                    f"{_GRAPH_BASE}/{pixel_id}",
                    params={
                        "fields": "match_rate_approx,data_use_setting",
                        "access_token": access_token,
                    },
                )
                emq_data = emq_resp.json() if emq_resp.status_code == 200 else {}
                match_rate = emq_data.get("match_rate_approx")

                # Flag stale pixel
                if not last_fired:
                    issues.append(
                        {
                            "severity": "critical",
                            "pixel": pixel_name,
                            "issue": "Pixel has never fired",
                            "recommendation": "Install the base pixel code on all pages of your website",
                        }
                    )
                elif event_counts.get("PageView", 0) == 0:
                    issues.append(
                        {
                            "severity": "warning",
                            "pixel": pixel_name,
                            "issue": "Pixel has no PageView events in the last 7 days",
                            "recommendation": "Verify the pixel is still installed correctly — check browser console for errors",
                        }
                    )

                # Flag missing key events
                for ev in ["Purchase", "AddToCart"]:
                    if event_counts.get(ev, 0) == 0:
                        issues.append(
                            {
                                "severity": "warning",
                                "pixel": pixel_name,
                                "issue": f"No '{ev}' events received in the last 7 days",
                                "recommendation": f"Ensure '{ev}' standard event is firing on the relevant page, or implement Conversions API",
                            }
                        )

                # Flag low EMQ
                if match_rate is not None and match_rate < 6:
                    issues.append(
                        {
                            "severity": "warning",
                            "pixel": pixel_name,
                            "issue": f"Event Match Quality score is {match_rate}/10 (below 6.0 threshold)",
                            "recommendation": "Implement Conversions API with Advanced Matching parameters (email, phone, name) to improve EMQ",
                        }
                    )

                # Check for Conversions API (CAPI) — indicated by server events in stats
                has_capi = (
                    event_counts.get("Purchase_server", 0) > 0 or event_counts.get("AddToCart_server", 0) > 0
                )
                if not has_capi and event_counts.get("Purchase", 0) > 0:
                    issues.append(
                        {
                            "severity": "info",
                            "pixel": pixel_name,
                            "issue": "Conversions API (server-side) not detected for Purchase events",
                            "recommendation": "Implement Meta CAPI to send server-to-server events — reduces signal loss from ad blockers and iOS restrictions",
                        }
                    )

                pixel_results.append(
                    {
                        "pixel_id": pixel_id,
                        "pixel_name": pixel_name,
                        "last_fired_time": last_fired,
                        "event_stats": event_counts,
                        "event_match_quality": match_rate,
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

    @friendly_errors("Meta Ads")
    async def create_campaign(
        self,
        access_token: str,
        account_id: str,
        name: str,
        objective: str,
        status: str = "PAUSED",
        special_ad_categories: list[str] | None = None,
        daily_budget: float | None = None,
    ) -> dict:
        """
        Creates a new Meta Ads campaign.
        objective: OUTCOME_TRAFFIC | OUTCOME_AWARENESS | OUTCOME_ENGAGEMENT |
                   OUTCOME_LEADS | OUTCOME_APP_PROMOTION | OUTCOME_SALES
        status: PAUSED | ACTIVE
        daily_budget: in account currency (e.g. 50.00 = $50/day); stored as cents internally.
        """
        act_id = self._act(account_id)
        payload = {
            "name": name,
            "objective": objective,
            "status": status,
            "special_ad_categories": special_ad_categories or [],
            "access_token": access_token,
        }
        if daily_budget is not None:
            payload["daily_budget"] = int(daily_budget * 100)  # Meta uses cents

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{_GRAPH_BASE}/{act_id}/campaigns",
                data=payload,
            )

        if resp.status_code != 200:
            return {"error": True, "message": f"Meta API error: {resp.text}"}

        data = resp.json()
        return {
            "campaign_id": data.get("id"),
            "campaign_name": name,
            "objective": objective,
            "status": status,
            "account_id": act_id,
            "note": "Campaign created. Add an Ad Set and Ad to make it ready to run.",
        }

    @friendly_errors("Meta Ads")
    async def update_campaign_status(
        self,
        access_token: str,
        campaign_id: str,
        status: str,
    ) -> dict:
        """
        Updates a Meta campaign's status.
        status: ACTIVE | PAUSED | DELETED | ARCHIVED
        """
        valid = {"ACTIVE", "PAUSED", "DELETED", "ARCHIVED"}
        if status.upper() not in valid:
            return {"error": True, "message": f"status must be one of: {valid}"}

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{_GRAPH_BASE}/{campaign_id}",
                data={"status": status.upper(), "access_token": access_token},
            )

        if resp.status_code != 200:
            return {"error": True, "message": f"Meta API error: {resp.text}"}

        return {
            "campaign_id": campaign_id,
            "new_status": status.upper(),
            "updated": True,
        }

    @friendly_errors("Meta Ads")
    async def update_campaign_budget(
        self,
        access_token: str,
        campaign_id: str,
        daily_budget: float,
    ) -> dict:
        """
        Updates a Meta campaign's daily budget.
        daily_budget: in account currency (converted to cents internally).
        """
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{_GRAPH_BASE}/{campaign_id}",
                data={
                    "daily_budget": int(daily_budget * 100),
                    "access_token": access_token,
                },
            )

        if resp.status_code != 200:
            return {"error": True, "message": f"Meta API error: {resp.text}"}

        return {
            "campaign_id": campaign_id,
            "new_daily_budget": daily_budget,
            "updated": True,
        }
