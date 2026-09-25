"""
Snapchat Ads Connector

Uses the Snapchat Marketing API v1 via httpx.
API base: https://adsapi.snapchat.com/v1/

Layer 1: list_accounts, get_campaign_performance, get_adsquad_performance
Layer 2: audit_tracking_setup  (Snap Pixel health + Conversions API detection)
Layer 3: update_campaign_status, update_campaign_budget
"""

import logging

import httpx

from app.connectors.errors import friendly_errors

logger = logging.getLogger(__name__)

_BASE = "https://adsapi.snapchat.com/v1"


class SnapAdsConnector:
    """Interfaces with Snapchat Marketing API using per-user OAuth2 access tokens."""

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
            logger.error(f"Snap API error [{context}] status={resp.status_code}: {resp.text[:300]}")
            return {"error": True, "message": f"Snapchat API error ({resp.status_code}): {resp.text[:300]}"}
        return None

    # ------------------------------------------------------------------
    # Layer 1: Data access
    # ------------------------------------------------------------------

    @friendly_errors("Snap Ads")
    async def list_accounts(self, access_token: str) -> dict:
        """
        Returns all ad accounts across all organisations accessible to the user.
        Calls GET /me/organizations → GET /organizations/{id}/adaccounts
        """
        async with httpx.AsyncClient(timeout=20) as client:
            orgs_resp = await client.get(
                f"{_BASE}/me/organizations",
                headers=self._headers(access_token),
            )

        err = self._check(orgs_resp, "list_accounts:orgs")
        if err:
            return err

        organizations = orgs_resp.json().get("organizations", [])
        accounts = []

        async with httpx.AsyncClient(timeout=20) as client:
            for org_wrapper in organizations:
                org = org_wrapper.get("organization", {})
                org_id = org.get("id")
                accts_resp = await client.get(
                    f"{_BASE}/organizations/{org_id}/adaccounts",
                    headers=self._headers(access_token),
                )
                if accts_resp.status_code == 200:
                    for acct_wrapper in accts_resp.json().get("adaccounts", []):
                        acct = acct_wrapper.get("adaccount", {})
                        accounts.append(
                            {
                                "account_id": acct.get("id"),
                                "name": acct.get("name"),
                                "currency": acct.get("currency"),
                                "timezone": acct.get("timezone"),
                                "status": acct.get("status"),
                                "organization_id": org_id,
                                "organization_name": org.get("name"),
                            }
                        )

        return {"accounts": accounts, "total": len(accounts)}

    @friendly_errors("Snap Ads")
    async def get_campaign_performance(
        self,
        access_token: str,
        account_id: str,
        start_date: str,
        end_date: str,
        metrics: list[str] | None = None,
    ) -> dict:
        """
        Returns campaign-level stats for the given date range.
        Calls GET /adaccounts/{id}/campaigns then GET /campaigns/{id}/stats
        start_date / end_date: YYYY-MM-DD
        """
        async with httpx.AsyncClient(timeout=20) as client:
            camp_resp = await client.get(
                f"{_BASE}/adaccounts/{account_id}/campaigns",
                headers=self._headers(access_token),
            )

        err = self._check(camp_resp, "get_campaign_performance:list")
        if err:
            return err

        campaigns_raw = [c.get("campaign", {}) for c in camp_resp.json().get("campaigns", [])]

        # Fetch stats for all campaigns in one call using the batch stats endpoint
        campaign_ids = [c["id"] for c in campaigns_raw if c.get("id")]
        stats_by_id = {}

        if campaign_ids:
            # Snap supports comma-separated IDs on the stats endpoint
            ids_param = ",".join(campaign_ids[:50])  # API max ~50
            async with httpx.AsyncClient(timeout=30) as client:
                stats_resp = await client.get(
                    f"{_BASE}/campaigns/{ids_param}/stats",
                    headers=self._headers(access_token),
                    params={
                        "granularity": "TOTAL",
                        "fields": "impressions,swipes,spend,video_views,swipe_up_percent,ecpc,ecpm,ecpms",
                        "start_time": f"{start_date}T00:00:00.000-07:00",
                        "end_time": f"{end_date}T23:59:59.000-07:00",
                    },
                )
            if stats_resp.status_code == 200:
                for item in stats_resp.json().get("timeseries_stats", []):
                    ts = item.get("timeseries_stat", {})
                    cid = ts.get("id")
                    total_stats = ts.get("total_stat", {}).get("stats", {})
                    stats_by_id[cid] = total_stats

        campaigns = []
        for c in campaigns_raw:
            cid = c.get("id")
            s = stats_by_id.get(cid, {})
            campaigns.append(
                {
                    "campaign_id": cid,
                    "campaign_name": c.get("name"),
                    "status": c.get("status"),
                    "objective": c.get("objective"),
                    "daily_budget_micro": c.get("daily_budget_micro"),
                    "impressions": s.get("impressions"),
                    "swipes": s.get("swipes"),
                    "spend": s.get("spend"),
                    "video_views": s.get("video_views"),
                    "swipe_up_percent": s.get("swipe_up_percent"),
                    "ecpc": s.get("ecpc"),
                    "ecpm": s.get("ecpm"),
                }
            )

        return {
            "account_id": account_id,
            "date_range": f"{start_date} to {end_date}",
            "campaigns": campaigns,
            "total": len(campaigns),
        }

    @friendly_errors("Snap Ads")
    async def get_adsquad_performance(
        self,
        access_token: str,
        account_id: str,
        start_date: str,
        end_date: str,
        campaign_id: str | None = None,
    ) -> dict:
        """
        Returns ad squad (ad set equivalent) stats.
        Optionally filtered to a single campaign.
        """
        async with httpx.AsyncClient(timeout=20) as client:
            squads_resp = await client.get(
                f"{_BASE}/adaccounts/{account_id}/adsquads",
                headers=self._headers(access_token),
            )

        err = self._check(squads_resp, "get_adsquad_performance:list")
        if err:
            return err

        squads_raw = [s.get("adsquad", {}) for s in squads_resp.json().get("adsquads", [])]
        if campaign_id:
            squads_raw = [s for s in squads_raw if s.get("campaign_id") == campaign_id]

        squad_ids = [s["id"] for s in squads_raw if s.get("id")]
        stats_by_id = {}

        if squad_ids:
            ids_param = ",".join(squad_ids[:50])
            async with httpx.AsyncClient(timeout=30) as client:
                stats_resp = await client.get(
                    f"{_BASE}/adsquads/{ids_param}/stats",
                    headers=self._headers(access_token),
                    params={
                        "granularity": "TOTAL",
                        "fields": "impressions,swipes,spend,video_views,ecpc,ecpm",
                        "start_time": f"{start_date}T00:00:00.000-07:00",
                        "end_time": f"{end_date}T23:59:59.000-07:00",
                    },
                )
            if stats_resp.status_code == 200:
                for item in stats_resp.json().get("timeseries_stats", []):
                    ts = item.get("timeseries_stat", {})
                    sid = ts.get("id")
                    total_stats = ts.get("total_stat", {}).get("stats", {})
                    stats_by_id[sid] = total_stats

        squads = []
        for s in squads_raw:
            sid = s.get("id")
            st = stats_by_id.get(sid, {})
            squads.append(
                {
                    "adsquad_id": sid,
                    "adsquad_name": s.get("name"),
                    "campaign_id": s.get("campaign_id"),
                    "status": s.get("status"),
                    "placement": s.get("placement"),
                    "impressions": st.get("impressions"),
                    "swipes": st.get("swipes"),
                    "spend": st.get("spend"),
                    "ecpc": st.get("ecpc"),
                    "ecpm": st.get("ecpm"),
                }
            )

        return {
            "account_id": account_id,
            "date_range": f"{start_date} to {end_date}",
            "adsquads": squads,
            "total": len(squads),
        }

    # ------------------------------------------------------------------
    # Layer 2: Audit
    # ------------------------------------------------------------------

    @friendly_errors("Snap Ads")
    async def audit_tracking_setup(self, access_token: str, account_id: str) -> dict:
        """
        Audits Snap tracking health for an ad account:
        - Lists all Snap Pixels linked to the account
        - Checks pixel effective_status
        - Fetches pixel event stats to verify event firing
        - Detects Conversions API (CAPI) server-side events
        - Flags missing Purchase/ADD_CART events and low-signal configurations
        """
        issues = []

        async with httpx.AsyncClient(timeout=20) as client:
            pixels_resp = await client.get(
                f"{_BASE}/adaccounts/{account_id}/pixels",
                headers=self._headers(access_token),
            )

        err = self._check(pixels_resp, "audit_tracking_setup:pixels")
        if err:
            return {
                **err,
                "note": "Could not fetch Snap Pixels. Verify the access token has Snap Ads permissions.",
            }

        pixels = [p.get("pixel", {}) for p in pixels_resp.json().get("pixels", [])]

        if not pixels:
            return {
                "score": 0,
                "pixels": [],
                "issues": [
                    {
                        "severity": "critical",
                        "issue": "No Snap Pixel found for this ad account",
                        "recommendation": "Create a Snap Pixel in Events Manager and install the base code on your website",
                    }
                ],
            }

        pixel_results = []

        async with httpx.AsyncClient(timeout=20) as client:
            for pixel in pixels:
                pixel_id = pixel.get("id")
                pixel_name = pixel.get("name", "Unnamed Pixel")
                effective_status = pixel.get("effective_status", "UNKNOWN")

                # Fetch pixel event stats
                stats_resp = await client.get(
                    f"{_BASE}/pixels/{pixel_id}/events/stats",
                    headers=self._headers(access_token),
                )

                browser_events = {}
                server_events = {}

                if stats_resp.status_code == 200:
                    for stat_wrapper in stats_resp.json().get("pixel_event_stats", []):
                        stat = stat_wrapper.get("pixel_event_stat", {})
                        event_type = stat.get("event_type")
                        channel = stat.get("channel_type", "PIXEL")  # PIXEL or SERVER
                        count = stat.get("count", 0)
                        if channel == "PIXEL":
                            browser_events[event_type] = count
                        elif channel == "SERVER":
                            server_events[event_type] = count

                # Flag inactive pixel
                if effective_status != "ACTIVE":
                    issues.append(
                        {
                            "severity": "warning",
                            "pixel": pixel_name,
                            "issue": f"Pixel effective status is '{effective_status}' (expected ACTIVE)",
                            "recommendation": "Check the pixel installation in Snap Events Manager",
                        }
                    )

                # Flag missing PAGE_VIEW
                if browser_events.get("PAGE_VIEW", 0) == 0:
                    issues.append(
                        {
                            "severity": "warning",
                            "pixel": pixel_name,
                            "issue": "No PAGE_VIEW events detected",
                            "recommendation": "Verify the Snap Pixel base code is installed on all pages",
                        }
                    )

                # Flag missing conversion events
                for ev in ["PURCHASE", "ADD_CART"]:
                    if browser_events.get(ev, 0) == 0 and server_events.get(ev, 0) == 0:
                        issues.append(
                            {
                                "severity": "warning",
                                "pixel": pixel_name,
                                "issue": f"No '{ev}' events detected (browser or server)",
                                "recommendation": f"Add '{ev}' event to your checkout flow, or implement Snap CAPI for server-side tracking",
                            }
                        )

                # Recommend CAPI
                has_capi = bool(server_events)
                if not has_capi and browser_events.get("PURCHASE", 0) > 0:
                    issues.append(
                        {
                            "severity": "info",
                            "pixel": pixel_name,
                            "issue": "Snap Conversions API (server-side) not detected",
                            "recommendation": "Implement Snap CAPI to send PURCHASE events server-to-server — improves signal quality and supports iOS 14+ measurement",
                        }
                    )

                pixel_results.append(
                    {
                        "pixel_id": pixel_id,
                        "pixel_name": pixel_name,
                        "effective_status": effective_status,
                        "browser_events": browser_events,
                        "server_events": server_events,
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

    @friendly_errors("Snap Ads")
    async def update_campaign_status(
        self,
        access_token: str,
        account_id: str,
        campaign_id: str,
        status: str,
    ) -> dict:
        """
        Updates a Snap campaign's status.
        status: ACTIVE | PAUSED
        Uses PUT /campaigns/{id}
        """
        valid = {"ACTIVE", "PAUSED"}
        if status.upper() not in valid:
            return {"error": True, "message": f"status must be one of: {valid}"}

        async with httpx.AsyncClient(timeout=20) as client:
            # First fetch current campaign to get required fields for PUT
            get_resp = await client.get(
                f"{_BASE}/campaigns/{campaign_id}",
                headers=self._headers(access_token),
            )

        err = self._check(get_resp, "update_campaign_status:get")
        if err:
            return err

        campaign_data = get_resp.json().get("campaigns", [{}])[0].get("campaign", {})
        campaign_data["status"] = status.upper()

        async with httpx.AsyncClient(timeout=20) as client:
            put_resp = await client.put(
                f"{_BASE}/adaccounts/{account_id}/campaigns",
                headers=self._headers(access_token),
                json={"campaigns": [{"campaign": campaign_data}]},
            )

        err = self._check(put_resp, "update_campaign_status:put")
        if err:
            return err

        return {
            "campaign_id": campaign_id,
            "account_id": account_id,
            "new_status": status.upper(),
            "updated": True,
        }

    @friendly_errors("Snap Ads")
    async def update_campaign_budget(
        self,
        access_token: str,
        account_id: str,
        campaign_id: str,
        daily_budget_micro: int,
    ) -> dict:
        """
        Updates a Snap campaign's daily budget.
        daily_budget_micro: budget in micro-currency (e.g. 50_000_000 = $50.00)
        Uses PUT /campaigns/{id}
        """
        async with httpx.AsyncClient(timeout=20) as client:
            get_resp = await client.get(
                f"{_BASE}/campaigns/{campaign_id}",
                headers=self._headers(access_token),
            )

        err = self._check(get_resp, "update_campaign_budget:get")
        if err:
            return err

        campaign_data = get_resp.json().get("campaigns", [{}])[0].get("campaign", {})
        campaign_data["daily_budget_micro"] = daily_budget_micro

        async with httpx.AsyncClient(timeout=20) as client:
            put_resp = await client.put(
                f"{_BASE}/adaccounts/{account_id}/campaigns",
                headers=self._headers(access_token),
                json={"campaigns": [{"campaign": campaign_data}]},
            )

        err = self._check(put_resp, "update_campaign_budget:put")
        if err:
            return err

        return {
            "campaign_id": campaign_id,
            "account_id": account_id,
            "new_daily_budget_micro": daily_budget_micro,
            "new_daily_budget_usd": round(daily_budget_micro / 1_000_000, 2),
            "updated": True,
        }

    @friendly_errors("Snap Ads")
    async def create_campaign(
        self,
        access_token: str,
        account_id: str,
        name: str,
        status: str = "PAUSED",
        daily_budget: float | None = None,
        start_date: str | None = None,
        objective: str = "APP_INSTALL",
    ) -> dict:
        """
        Creates a new Snap campaign.
        status: ACTIVE | PAUSED
        daily_budget: in account currency (converted to micro-USD)
        start_date: YYYY-MM-DD
        objective: APP_INSTALL | AWARENESS | etc.
        """
        campaign_body = {
            "name": name,
            "status": status.upper(),
            "objective": objective,
        }
        if daily_budget is not None:
            campaign_body["daily_budget_micro"] = int(daily_budget * 1_000_000)
        if start_date:
            campaign_body["start_date"] = start_date

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{_BASE}/adaccounts/{account_id}/campaigns",
                headers=self._headers(access_token),
                json={"campaigns": [{"campaign": campaign_body}]},
            )

        err = self._check(resp, "create_campaign")
        if err:
            return err

        created = resp.json().get("campaigns", [{}])[0].get("campaign", {})
        return {
            "campaign_id": created.get("id"),
            "campaign_name": name,
            "status": status.upper(),
            "account_id": account_id,
            "updated": True,
        }
