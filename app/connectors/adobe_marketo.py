"""
Adobe Marketo Engage Connector.

Marketo uses its OWN OAuth 2.0 client-credentials flow against the customer's
instance — NOT Adobe IMS. Credentials are not shared with adobe_analytics /
adobe_launch.

Auth:   GET {instance}/identity/oauth/token?grant_type=client_credentials&client_id=..&client_secret=..
API:    {instance}/rest/v1/...   and   {instance}/rest/asset/v1/...

Layer 1 (Read):  get_leads, get_lead_by_id, list_lead_lists, get_list_leads,
                 get_lead_activities, list_campaigns, list_programs, get_program,
                 list_emails, list_landing_pages, list_forms
Layer 2 (Audit): audit_instance, check_data_quality
Layer 3 (Write): create_or_update_leads, add_leads_to_list, remove_leads_from_list,
                 request_campaign, schedule_campaign
"""

import logging
import time
from typing import Any

import httpx

from app.connectors.errors import friendly_errors

logger = logging.getLogger(__name__)


class AdobeMarketoConnector:
    """Interfaces with the Adobe Marketo Engage REST API (client-credentials grant)."""

    def __init__(self):
        # In-memory token cache: {instance_url: {token, expiry}}
        self._token_cache: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------
    async def _get_marketo_token(self, instance_url: str, client_id: str, client_secret: str) -> dict:
        """Get or refresh a Marketo access token (cached per instance, 60s buffer)."""
        instance_url = instance_url.rstrip("/")
        cached = self._token_cache.get(instance_url, {})
        if cached.get("token") and cached.get("expiry", 0) > time.time() + 60:
            return {"token": cached["token"]}

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    f"{instance_url}/identity/oauth/token",
                    params={
                        "grant_type": "client_credentials",
                        "client_id": client_id,
                        "client_secret": client_secret,
                    },
                )
                if response.status_code >= 400:
                    return {"error": True, "status_code": response.status_code, "message": response.text}
                data = response.json()
                token = data.get("access_token")
                expires_in = data.get("expires_in", 3600)
                self._token_cache[instance_url] = {"token": token, "expiry": time.time() + expires_in}
                return {"token": token}
        except Exception as e:
            logger.error(f"Marketo token request error: {e}")
            return {"error": True, "message": str(e)}

    async def _request(
        self,
        instance_url: str,
        client_id: str,
        client_secret: str,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        _retry: bool = True,
    ) -> dict:
        """Authenticated request to the Marketo REST API. Refreshes token once on 601/602."""
        instance_url = instance_url.rstrip("/")
        token_result = await self._get_marketo_token(instance_url, client_id, client_secret)
        if token_result.get("error"):
            return token_result
        token = token_result.get("token")
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        url = f"{instance_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                if method == "GET":
                    response = await client.get(url, headers=headers, params=params)
                elif method == "POST":
                    response = await client.post(url, headers=headers, params=params, json=json_body)
                else:
                    return {"error": True, "message": f"Unsupported HTTP method: {method}"}

            if response.status_code >= 400:
                return {"error": True, "status_code": response.status_code, "message": response.text}

            data = response.json()
            # Marketo returns 200 with success=false for auth/quota errors.
            if isinstance(data, dict) and data.get("success") is False:
                codes = {str(e.get("code")) for e in data.get("errors", [])}
                # Expired/invalid token -> drop cache and retry once.
                if _retry and codes & {"601", "602"}:
                    self._token_cache.pop(instance_url, None)
                    return await self._request(
                        instance_url, client_id, client_secret, method, path, params, json_body, _retry=False
                    )
                if codes & {"606", "607", "615"}:
                    return {
                        "error": True,
                        "error_type": "rate_limited",
                        "message": "Marketo API rate limit / daily quota exceeded.",
                        "errors": data.get("errors", []),
                    }
                # Any other success:false response — surface it as an error
                # so callers checking result.get("error") don't treat it as success.
                return {
                    "error": True,
                    "message": "Marketo API returned an error.",
                    "errors": data.get("errors", []),
                }
            return data
        except Exception as e:
            logger.error(f"Marketo API request error: {e}")
            return {"error": True, "message": str(e)}

    @staticmethod
    def _csv(values: list[str] | None) -> str | None:
        if not values:
            return None
        return ",".join(str(v) for v in values)

    # ------------------------------------------------------------------
    # Layer 1: Read
    # ------------------------------------------------------------------
    @friendly_errors("Adobe Marketo Engage")
    async def get_leads(
        self,
        instance_url: str,
        client_id: str,
        client_secret: str,
        filter_type: str = "email",
        filter_values: list[str] | None = None,
        fields: list[str] | None = None,
        limit: int = 100,
    ) -> dict:
        """GET /rest/v1/leads.json — leads matching a filter (e.g. filter_type=email)."""
        params: dict[str, Any] = {"filterType": filter_type, "batchSize": limit}
        fv = self._csv(filter_values)
        if fv:
            params["filterValues"] = fv
        fl = self._csv(fields)
        if fl:
            params["fields"] = fl
        return await self._request(
            instance_url, client_id, client_secret, "GET", "/rest/v1/leads.json", params=params
        )

    @friendly_errors("Adobe Marketo Engage")
    async def get_lead_by_id(
        self,
        instance_url: str,
        client_id: str,
        client_secret: str,
        lead_id: str,
        fields: list[str] | None = None,
    ) -> dict:
        """GET /rest/v1/lead/{id}.json — a single lead by Marketo id."""
        params = {}
        fl = self._csv(fields)
        if fl:
            params["fields"] = fl
        return await self._request(
            instance_url,
            client_id,
            client_secret,
            "GET",
            f"/rest/v1/lead/{lead_id}.json",
            params=params or None,
        )

    @friendly_errors("Adobe Marketo Engage")
    async def list_lead_lists(
        self, instance_url: str, client_id: str, client_secret: str, limit: int = 100
    ) -> dict:
        """GET /rest/v1/lists.json — static/smart lists."""
        return await self._request(
            instance_url, client_id, client_secret, "GET", "/rest/v1/lists.json", params={"batchSize": limit}
        )

    @friendly_errors("Adobe Marketo Engage")
    async def get_list_leads(
        self, instance_url: str, client_id: str, client_secret: str, list_id: str, limit: int = 100
    ) -> dict:
        """GET /rest/v1/list/{listId}/leads.json — members of a list."""
        return await self._request(
            instance_url,
            client_id,
            client_secret,
            "GET",
            f"/rest/v1/list/{list_id}/leads.json",
            params={"batchSize": limit},
        )

    @friendly_errors("Adobe Marketo Engage")
    async def get_lead_activities(
        self,
        instance_url: str,
        client_id: str,
        client_secret: str,
        activity_type_ids: list[str] | None = None,
        list_id: str | None = None,
        since_datetime: str | None = None,
        limit: int = 100,
    ) -> dict:
        """Lead activities (opens, clicks, form fills, email engagement).

        Two-step: get a paging token from /rest/v1/activities/pagingtoken.json
        (needs sinceDatetime), then GET /rest/v1/activities.json.
        """
        token_params = {"sinceDatetime": since_datetime or "1970-01-01T00:00:00Z"}
        pt = await self._request(
            instance_url,
            client_id,
            client_secret,
            "GET",
            "/rest/v1/activities/pagingtoken.json",
            params=token_params,
        )
        if pt.get("error"):
            return pt
        next_token = pt.get("nextPageToken")
        params: dict[str, Any] = {"nextPageToken": next_token, "batchSize": limit}
        ids = self._csv(activity_type_ids)
        if ids:
            params["activityTypeIds"] = ids
        if list_id:
            params["listId"] = list_id
        return await self._request(
            instance_url, client_id, client_secret, "GET", "/rest/v1/activities.json", params=params
        )

    @friendly_errors("Adobe Marketo Engage")
    async def list_campaigns(
        self, instance_url: str, client_id: str, client_secret: str, limit: int = 100
    ) -> dict:
        """GET /rest/v1/campaigns.json — smart campaigns."""
        return await self._request(
            instance_url,
            client_id,
            client_secret,
            "GET",
            "/rest/v1/campaigns.json",
            params={"batchSize": limit},
        )

    @friendly_errors("Adobe Marketo Engage")
    async def list_programs(
        self, instance_url: str, client_id: str, client_secret: str, limit: int = 100
    ) -> dict:
        """GET /rest/asset/v1/programs.json — programs."""
        return await self._request(
            instance_url,
            client_id,
            client_secret,
            "GET",
            "/rest/asset/v1/programs.json",
            params={"maxReturn": limit},
        )

    @friendly_errors("Adobe Marketo Engage")
    async def get_program(
        self, instance_url: str, client_id: str, client_secret: str, program_id: str
    ) -> dict:
        """GET /rest/asset/v1/program/{id}.json — a single program."""
        return await self._request(
            instance_url, client_id, client_secret, "GET", f"/rest/asset/v1/program/{program_id}.json"
        )

    @friendly_errors("Adobe Marketo Engage")
    async def list_emails(
        self, instance_url: str, client_id: str, client_secret: str, limit: int = 100
    ) -> dict:
        """GET /rest/asset/v1/emails.json — email assets."""
        return await self._request(
            instance_url,
            client_id,
            client_secret,
            "GET",
            "/rest/asset/v1/emails.json",
            params={"maxReturn": limit},
        )

    @friendly_errors("Adobe Marketo Engage")
    async def list_landing_pages(
        self, instance_url: str, client_id: str, client_secret: str, limit: int = 100
    ) -> dict:
        """GET /rest/asset/v1/landingPages.json — landing page assets."""
        return await self._request(
            instance_url,
            client_id,
            client_secret,
            "GET",
            "/rest/asset/v1/landingPages.json",
            params={"maxReturn": limit},
        )

    @friendly_errors("Adobe Marketo Engage")
    async def list_forms(
        self, instance_url: str, client_id: str, client_secret: str, limit: int = 100
    ) -> dict:
        """GET /rest/asset/v1/forms.json — form assets."""
        return await self._request(
            instance_url,
            client_id,
            client_secret,
            "GET",
            "/rest/asset/v1/forms.json",
            params={"maxReturn": limit},
        )

    # ------------------------------------------------------------------
    # Layer 2: Audit
    # ------------------------------------------------------------------
    @friendly_errors("Adobe Marketo Engage")
    async def audit_instance(self, instance_url: str, client_id: str, client_secret: str) -> dict:
        """Health snapshot: daily API usage, program inventory + stale/off programs."""
        usage = await self._request(
            instance_url, client_id, client_secret, "GET", "/rest/v1/stats/usage.json"
        )
        programs = await self._request(
            instance_url,
            client_id,
            client_secret,
            "GET",
            "/rest/asset/v1/programs.json",
            params={"maxReturn": 200},
        )
        api_calls = 0
        if not usage.get("error"):
            rows = usage.get("result") or []
            if rows and isinstance(rows[0], dict):
                api_calls = rows[0].get("total", 0)
        progs = [] if programs.get("error") else (programs.get("result") or [])
        off_programs = [p.get("name") for p in progs if str(p.get("status", "")).lower() in ("off", "")]
        return {
            "error": False,
            "api_calls_used_today": api_calls,
            "program_count": len(progs),
            "off_or_unknown_programs": off_programs,
            "warnings": (["No programs found"] if not progs else []),
        }

    @friendly_errors("Adobe Marketo Engage")
    async def check_data_quality(
        self, instance_url: str, client_id: str, client_secret: str, sample_emails: list[str] | None = None
    ) -> dict:
        """Null-field rates on core lead fields over a sampled set of leads."""
        leads_resp = await self.get_leads(
            instance_url,
            client_id,
            client_secret,
            filter_type="email",
            filter_values=sample_emails,
            fields=["id", "email", "company"],
            limit=300,
        )
        if leads_resp.get("error"):
            return leads_resp
        leads = leads_resp.get("result") or []
        missing_email = sum(1 for lead in leads if not lead.get("email"))
        missing_company = sum(1 for lead in leads if not lead.get("company"))
        return {
            "error": False,
            "leads_checked": len(leads),
            "missing_email": missing_email,
            "missing_company": missing_company,
        }

    # ------------------------------------------------------------------
    # Layer 3: Write
    # ------------------------------------------------------------------
    @staticmethod
    def _lead_id_objects(lead_ids: list[str]) -> list[dict]:
        out = []
        for lid in lead_ids:
            try:
                out.append({"id": int(lid)})
            except (TypeError, ValueError):
                out.append({"id": lid})
        return out

    @friendly_errors("Adobe Marketo Engage")
    async def create_or_update_leads(
        self,
        instance_url: str,
        client_id: str,
        client_secret: str,
        leads: list[dict],
        lookup_field: str = "email",
        action: str = "createOrUpdate",
    ) -> dict:
        """POST /rest/v1/leads.json — create/update leads (dedupe by lookup_field)."""
        body = {"action": action, "lookupField": lookup_field, "input": leads}
        return await self._request(
            instance_url, client_id, client_secret, "POST", "/rest/v1/leads.json", json_body=body
        )

    @friendly_errors("Adobe Marketo Engage")
    async def add_leads_to_list(
        self, instance_url: str, client_id: str, client_secret: str, list_id: str, lead_ids: list[str]
    ) -> dict:
        """POST /rest/v1/lists/{listId}/leads.json — add leads to a static list."""
        body = {"input": self._lead_id_objects(lead_ids)}
        return await self._request(
            instance_url,
            client_id,
            client_secret,
            "POST",
            f"/rest/v1/lists/{list_id}/leads.json",
            json_body=body,
        )

    @friendly_errors("Adobe Marketo Engage")
    async def remove_leads_from_list(
        self, instance_url: str, client_id: str, client_secret: str, list_id: str, lead_ids: list[str]
    ) -> dict:
        """Remove leads from a static list. Marketo uses DELETE semantics via _method=DELETE on POST."""
        body = {"input": self._lead_id_objects(lead_ids)}
        return await self._request(
            instance_url,
            client_id,
            client_secret,
            "POST",
            f"/rest/v1/lists/{list_id}/leads.json",
            params={"_method": "DELETE"},
            json_body=body,
        )

    @friendly_errors("Adobe Marketo Engage")
    async def request_campaign(
        self,
        instance_url: str,
        client_id: str,
        client_secret: str,
        campaign_id: str,
        lead_ids: list[str],
        tokens: list[dict] | None = None,
    ) -> dict:
        """POST /rest/v1/campaigns/{id}/trigger.json — run a trigger smart campaign for leads."""
        inp: dict[str, Any] = {"leads": self._lead_id_objects(lead_ids)}
        if tokens:
            inp["tokens"] = tokens
        return await self._request(
            instance_url,
            client_id,
            client_secret,
            "POST",
            f"/rest/v1/campaigns/{campaign_id}/trigger.json",
            json_body={"input": inp},
        )

    @friendly_errors("Adobe Marketo Engage")
    async def schedule_campaign(
        self,
        instance_url: str,
        client_id: str,
        client_secret: str,
        campaign_id: str,
        run_at: str | None = None,
        tokens: list[dict] | None = None,
    ) -> dict:
        """POST /rest/v1/campaigns/{id}/schedule.json — schedule a batch campaign run."""
        inp: dict[str, Any] = {}
        if run_at:
            inp["runAt"] = run_at
        if tokens:
            inp["tokens"] = tokens
        return await self._request(
            instance_url,
            client_id,
            client_secret,
            "POST",
            f"/rest/v1/campaigns/{campaign_id}/schedule.json",
            json_body={"input": inp},
        )
