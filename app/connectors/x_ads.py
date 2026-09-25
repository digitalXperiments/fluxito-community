"""X Ads Connector.

Uses the X Ads API with OAuth 1.0a user-context tokens.
API base: https://ads-api.x.com/12
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass
from urllib.parse import quote, urlparse

import httpx

from app.connectors.errors import friendly_errors

logger = logging.getLogger(__name__)

_BASE = "https://ads-api.x.com/12"
_MICRO = 1_000_000


@dataclass(frozen=True)
class XOAuth1Token:
    token: str
    token_secret: str


def _pct(value: object) -> str:
    return quote(str(value), safe="~-._")


class XAdsConnector:
    """Interfaces with X Ads API using OAuth 1.0a signed requests."""

    def __init__(self, consumer_key: str, consumer_secret: str):
        self.consumer_key = consumer_key
        self.consumer_secret = consumer_secret

    def _oauth_header(
        self,
        method: str,
        url: str,
        token: XOAuth1Token | None = None,
        params: dict | None = None,
        extra_oauth: dict | None = None,
    ) -> str:
        oauth = {
            "oauth_consumer_key": self.consumer_key,
            "oauth_nonce": secrets.token_urlsafe(24),
            "oauth_signature_method": "HMAC-SHA1",
            "oauth_timestamp": str(int(time.time())),
            "oauth_version": "1.0",
        }
        if token is not None:
            oauth["oauth_token"] = token.token
        if extra_oauth:
            oauth.update(extra_oauth)

        parsed = urlparse(url)
        base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        signature_params = {**(params or {}), **oauth}
        param_string = "&".join(
            f"{_pct(k)}={_pct(v)}" for k, v in sorted(signature_params.items()) if v is not None
        )
        base_string = "&".join([method.upper(), _pct(base_url), _pct(param_string)])
        signing_key = f"{_pct(self.consumer_secret)}&{_pct(token.token_secret if token else '')}"
        digest = hmac.new(signing_key.encode(), base_string.encode(), hashlib.sha1).digest()
        oauth["oauth_signature"] = base64.b64encode(digest).decode()
        return "OAuth " + ", ".join(f'{_pct(k)}="{_pct(v)}"' for k, v in sorted(oauth.items()))

    async def signed_get(
        self,
        path: str,
        token: XOAuth1Token,
        params: dict | None = None,
        timeout: int = 20,
    ) -> httpx.Response:
        url = f"{_BASE}{path}"
        headers = {"Authorization": self._oauth_header("GET", url, token, params=params)}
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.get(url, headers=headers, params=params)

    async def signed_put(
        self,
        path: str,
        token: XOAuth1Token,
        params: dict | None = None,
        timeout: int = 20,
    ) -> httpx.Response:
        url = f"{_BASE}{path}"
        headers = {"Authorization": self._oauth_header("PUT", url, token, params=params)}
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.put(url, headers=headers, params=params)

    async def signed_post(
        self,
        path: str,
        token: XOAuth1Token,
        params: dict | None = None,
        json_body: dict | None = None,
        timeout: int = 20,
    ) -> httpx.Response:
        url = f"{_BASE}{path}"
        headers = {"Authorization": self._oauth_header("POST", url, token, params=params)}
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.post(url, headers=headers, params=params, json=json_body)

    @staticmethod
    def _check(resp: httpx.Response, context: str) -> dict | None:
        if resp.status_code not in (200, 201):
            logger.error("X Ads API error [%s] status=%s: %s", context, resp.status_code, resp.text[:300])
            return {"error": True, "message": f"X Ads API error ({resp.status_code}): {resp.text[:300]}"}
        return None

    @friendly_errors("X Ads")
    async def list_accounts(self, token: XOAuth1Token) -> dict:
        resp = await self.signed_get("/accounts", token)
        err = self._check(resp, "list_accounts")
        if err:
            return err

        accounts_data = resp.json().get("data", [])
        return {
            "accounts": [
                {
                    "account_id": acc.get("id"),
                    "name": acc.get("name"),
                    "status": acc.get("approval_status") or acc.get("status"),
                }
                for acc in accounts_data
            ]
        }

    @friendly_errors("X Ads")
    async def get_campaign_performance(
        self,
        token: XOAuth1Token,
        account_id: str,
        start_date: str,
        end_date: str,
        metrics: list[str] | None = None,
    ) -> dict:
        campaigns_resp = await self.signed_get(f"/accounts/{account_id}/campaigns", token, timeout=30)
        err = self._check(campaigns_resp, "get_campaign_performance:list")
        if err:
            return err

        campaigns = campaigns_resp.json().get("data", [])
        campaign_ids = [c.get("id") for c in campaigns if c.get("id")]
        metrics_fields = metrics or ["impressions", "clicks", "billed_charge_local_micro", "conversions"]
        stats_by_id: dict[str, dict] = {}

        if campaign_ids:
            stats_resp = await self.signed_get(
                f"/stats/accounts/{account_id}",
                token,
                params={
                    "entity": "CAMPAIGN",
                    "entity_ids": ",".join(campaign_ids[:20]),
                    "start_time": f"{start_date}T00:00:00Z",
                    "end_time": f"{end_date}T00:00:00Z",
                    "granularity": "TOTAL",
                    "metric_groups": "ENGAGEMENT,BILLING,WEB_CONVERSION",
                },
                timeout=30,
            )
            if stats_resp.status_code == 200:
                for row in stats_resp.json().get("data", []):
                    entity_id = row.get("id") or row.get("entity_id")
                    id_data = row.get("id_data", [])
                    metrics_data = id_data[0].get("metrics", {}) if id_data else row.get("metrics", {})
                    stats_by_id[entity_id] = metrics_data or {}
            else:
                logger.warning(
                    "X Ads campaign stats failed: %s %s", stats_resp.status_code, stats_resp.text[:200]
                )

        result_campaigns = []
        for campaign in campaigns:
            cid = campaign.get("id")
            stats = stats_by_id.get(cid, {})
            result_campaigns.append(
                {
                    "campaign_id": cid,
                    "campaign_name": campaign.get("name", "Unnamed"),
                    "status": campaign.get("entity_status") or campaign.get("status"),
                    **{field: stats.get(field, 0) for field in metrics_fields},
                }
            )

        return {
            "account_id": account_id,
            "date_range": f"{start_date} to {end_date}",
            "campaigns": result_campaigns,
        }

    @friendly_errors("X Ads")
    async def get_line_item_performance(
        self,
        token: XOAuth1Token,
        account_id: str,
        start_date: str,
        end_date: str,
        campaign_id: str | None = None,
    ) -> dict:
        params = {"campaign_ids": campaign_id} if campaign_id else None
        line_items_resp = await self.signed_get(f"/accounts/{account_id}/line_items", token, params=params)
        err = self._check(line_items_resp, "get_line_item_performance:list")
        if err:
            return err

        line_items = line_items_resp.json().get("data", [])
        line_item_ids = [li.get("id") for li in line_items if li.get("id")]
        stats_by_id: dict[str, dict] = {}
        if line_item_ids:
            stats_resp = await self.signed_get(
                f"/stats/accounts/{account_id}",
                token,
                params={
                    "entity": "LINE_ITEM",
                    "entity_ids": ",".join(line_item_ids[:20]),
                    "start_time": f"{start_date}T00:00:00Z",
                    "end_time": f"{end_date}T00:00:00Z",
                    "granularity": "TOTAL",
                    "metric_groups": "ENGAGEMENT,BILLING,WEB_CONVERSION",
                },
                timeout=30,
            )
            if stats_resp.status_code == 200:
                for row in stats_resp.json().get("data", []):
                    entity_id = row.get("id") or row.get("entity_id")
                    id_data = row.get("id_data", [])
                    metrics_data = id_data[0].get("metrics", {}) if id_data else row.get("metrics", {})
                    stats_by_id[entity_id] = metrics_data or {}

        return {
            "account_id": account_id,
            "date_range": f"{start_date} to {end_date}",
            "line_items": [
                {
                    "line_item_id": item.get("id"),
                    "name": item.get("name", "Unnamed"),
                    "campaign_id": item.get("campaign_id"),
                    "status": item.get("entity_status") or item.get("status"),
                    **stats_by_id.get(item.get("id"), {}),
                }
                for item in line_items
            ],
        }

    @friendly_errors("X Ads")
    async def audit_tracking_setup(self, token: XOAuth1Token, account_id: str) -> dict:
        resp = await self.signed_get(f"/accounts/{account_id}/web_event_tags", token)
        if resp.status_code == 404:
            return {
                "score": 50,
                "tags": [],
                "issues": [
                    {
                        "severity": "warning",
                        "issue": "Could not find X web event tags for this ad account",
                        "recommendation": "Verify the X Pixel is installed and the token has analytics access.",
                    }
                ],
            }
        err = self._check(resp, "audit_tracking_setup")
        if err:
            return err

        tags = resp.json().get("data", [])
        issues = []
        if not tags:
            issues.append(
                {
                    "severity": "critical",
                    "issue": "No X web event tags found for this ad account",
                    "recommendation": "Create and install an X Pixel / website tag before optimizing campaigns.",
                }
            )
        return {
            "score": 100 if tags else 0,
            "tag_count": len(tags),
            "tags": tags,
            "issues": issues,
        }

    @friendly_errors("X Ads")
    async def update_campaign_status(
        self,
        token: XOAuth1Token,
        account_id: str,
        campaign_id: str,
        status: str,
    ) -> dict:
        normalized = status.upper()
        if normalized not in {"ACTIVE", "PAUSED"}:
            return {"error": True, "message": "status must be ACTIVE or PAUSED"}

        resp = await self.signed_put(
            f"/accounts/{account_id}/campaigns/{campaign_id}",
            token,
            params={"entity_status": normalized},
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

    @friendly_errors("X Ads")
    async def create_campaign(
        self,
        token: XOAuth1Token,
        account_id: str,
        name: str,
        objective: str,
        daily_budget: float,
        entity_status: str = "PAUSED",
        start_time: str | None = None,
    ) -> dict:
        """
        Creates a new X Ads campaign.
        objective: APP_INSTALLS | WEBSITE_CLICKS | ENGAGEMENT | VIDEO_VIEWS | etc.
        daily_budget: in account currency (converted to micro)
        entity_status: ACTIVE | PAUSED
        start_time: ISO 8601 timestamp (default: now)
        """
        import datetime

        body = {
            "name": name,
            "objective": objective,
            "entity_status": entity_status,
            "daily_budget_local_micro": int(daily_budget * _MICRO),
            "start_time": start_time or datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

        resp = await self.signed_post(
            f"/accounts/{account_id}/campaigns",
            token,
            json_body=body,
        )
        err = self._check(resp, "create_campaign")
        if err:
            return err

        data = resp.json().get("data", {})
        return {
            "campaign_id": data.get("id"),
            "campaign_name": name,
            "objective": objective,
            "entity_status": entity_status,
            "account_id": account_id,
            "updated": True,
        }

    @friendly_errors("X Ads")
    async def update_campaign_budget(
        self,
        token: XOAuth1Token,
        account_id: str,
        campaign_id: str,
        daily_budget: float,
    ) -> dict:
        """
        Updates an X Ads campaign's daily budget.
        daily_budget: in account currency (converted to micro)
        """
        resp = await self.signed_put(
            f"/accounts/{account_id}/campaigns/{campaign_id}",
            token,
            params={"daily_budget_local_micro": int(daily_budget * _MICRO)},
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
