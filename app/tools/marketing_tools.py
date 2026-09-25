"""
Unified Marketing MCP Tools
Consolidates Google Ads, Meta Ads, TikTok Ads, and Snapchat Ads into a single interface.
Routes to the appropriate connector based on the 'platform' argument.

Tools:
  marketing_read   — Layer 1 data access (accounts, campaigns, ad groups, keywords, conversions)
  marketing_audit  — Layer 2 intelligence (tracking setup, budget utilisation, quality scores)
  marketing_write  — Layer 3 write ops (create/pause/enable campaigns, update budgets)
                     Google writes are scope-gated via @require_scope.
"""

from typing import Literal

import app.app_state as state
from app.cache import cached_tool_response
from app.tools.shared_helpers import (
    decrypt_field,
    get_braze_creds,
    get_current_user,
    get_encrypted_credential_conn,
    get_google_conn_id,
    get_moengage_creds,
    get_oauth_app_project_id,
    get_provider_oauth1_tokens,
    get_provider_token,
)


def _get_user():
    return get_current_user()


def _get_google_conn_id():
    return get_google_conn_id()


def _get_provider_token(provider_str: str) -> str | None:
    return get_provider_token(provider_str)


def _get_provider_oauth1_tokens(provider_str: str) -> tuple[str, str] | None:
    return get_provider_oauth1_tokens(provider_str)


async def _get_marketo_conn(user_id: str):
    """Fetch the active Marketo connection and decrypt credentials.

    Returns (conn_id, instance_url, client_id, client_secret) or (None, None, None, None).
    """
    from app.models.credential_connection import MarketoConnection
    from app.tools.shared_helpers import decrypt_field, get_encrypted_credential_conn

    conn = await get_encrypted_credential_conn(MarketoConnection, user_id)
    if not conn:
        return None, None, None, None
    client_id = decrypt_field(conn.client_id_encrypted)
    client_secret = decrypt_field(conn.client_secret_encrypted)
    return str(conn.id), conn.instance_url, client_id, client_secret


def _no_marketo() -> dict:
    from app.config import settings

    base = settings.APP_BASE_URL
    return {
        "error": True,
        "error_type": "connection_missing",
        "message": "No Adobe Marketo Engage connection found.",
        "connect_url": f"{base}/connect/marketo",
        "action_required": f"Visit {base}/connect/marketo to connect Marketo.",
    }


def _no_branch() -> dict:
    from app.auth.mcp_session_manager import no_branch_response
    from app.config import settings

    return no_branch_response(settings.APP_BASE_URL)


def _no_appsflyer() -> dict:
    from app.auth.mcp_session_manager import no_appsflyer_response
    from app.config import settings

    return no_appsflyer_response(settings.APP_BASE_URL)


def _no_adjust() -> dict:
    from app.auth.mcp_session_manager import no_adjust_response
    from app.config import settings

    return no_adjust_response(settings.APP_BASE_URL)


_BRAZE_CACHE_TTL = 300
_MOENGAGE_CACHE_TTL = 300


def _no_braze() -> dict:
    from app.auth.mcp_session_manager import no_braze_response
    from app.config import settings

    return no_braze_response(settings.APP_BASE_URL)


def _no_moengage() -> dict:
    from app.auth.mcp_session_manager import no_moengage_response
    from app.config import settings

    return no_moengage_response(settings.APP_BASE_URL)


async def _get_branch_conn(user_id: str):
    """Fetch the active Branch connection and decrypt credentials.

    Returns (conn_id, api_key, secret_key) or (None, None, None).
    """
    from app.models.credential_connection import BranchConnection

    conn = await get_encrypted_credential_conn(BranchConnection, user_id)
    if not conn:
        return None, None, None
    api_key = decrypt_field(conn.api_key_encrypted)
    secret_key = decrypt_field(conn.secret_key_encrypted)
    return str(conn.id), api_key, secret_key


async def _get_appsflyer_conn(user_id: str):
    """Fetch the active AppsFlyer connection and decrypt credentials.

    Returns (conn_id, api_key, secret_key) or (None, None, None).
    secret_key may be empty for AppsFlyer.
    """
    from app.models.credential_connection import AppsFlyerConnection

    conn = await get_encrypted_credential_conn(AppsFlyerConnection, user_id)
    if not conn:
        return None, None, None
    api_key = decrypt_field(conn.api_key_encrypted)
    secret_key = decrypt_field(conn.secret_key_encrypted) if conn.secret_key_encrypted else ""
    return str(conn.id), api_key, secret_key or ""


async def _get_adjust_conn(user_id: str):
    """Fetch the active Adjust connection and decrypt credentials.

    Returns (conn_id, api_key, secret_key) or (None, None, None).
    secret_key may be empty for Adjust.
    """
    from app.models.credential_connection import AdjustConnection

    conn = await get_encrypted_credential_conn(AdjustConnection, user_id)
    if not conn:
        return None, None, None
    api_key = decrypt_field(conn.api_key_encrypted)
    secret_key = decrypt_field(conn.secret_key_encrypted) if conn.secret_key_encrypted else ""
    return str(conn.id), api_key, secret_key or ""


def _unauthorized_response(platform: str):
    from app.auth.mcp_session_manager import no_ads_response
    from app.config import settings

    if platform == "google":
        return no_ads_response(settings.APP_BASE_URL)
    return {
        "error": True,
        "error_type": "connection_missing",
        "message": f"No {platform.title()} Ads account connected.",
        "connect_url": f"{settings.APP_BASE_URL}/connect/{platform.lower()}",
        "action_required": f"Visit {settings.APP_BASE_URL}/connect/{platform.lower()} to authenticate.",
    }


def register_marketing_tools(mcp_server):
    @mcp_server.tool("marketing_read")
    async def marketing_read(
        platform: Literal[
            "google",
            "meta",
            "tiktok",
            "snap",
            "linkedin",
            "pinterest",
            "x",
            "reddit",
            "apple",
            "marketo",
            "branch",
            "appsflyer",
            "adjust",
            "braze",
            "moengage",
        ]
        | None = None,
        action: str = "",
        account_id: str | None = None,
        date_range_start: str | None = None,
        date_range_end: str | None = None,
        metrics: list[str] | None = None,
        campaign_id: str | None = None,
        limit: int = 50,
        resource_id: str | None = None,
        filters: dict | None = None,
        app_id: str | None = None,
        dimensions: list[str] | None = None,
        customer_id: str | None = None,
    ) -> dict:
        """Reads ad platform data. Use marketing_audit for health checks, marketing_write for changes.

        platform: google | meta | tiktok | snap | linkedin | pinterest | x | reddit | apple | marketo | branch | appsflyer | adjust | braze | moengage. Dates (YYYY-MM-DD) via date_range_start / date_range_end.

        All platforms: list_accounts, get_campaign_performance(account_id+dates)
        Google only: get_ad_group_performance(+dates,campaign_id?), get_conversion_actions(account_id), get_keyword_performance(+dates,campaign_id?)
        Meta: get_adset_performance(+dates,campaign_id?)
        TikTok: get_adgroup_performance(+dates,campaign_id?)
        Snap: get_adsquad_performance(+dates,campaign_id?)
        Marketo actions: get_leads, get_lead_by_id, list_lead_lists, get_list_leads, get_lead_activities, list_campaigns, list_programs, get_program, list_emails, list_landing_pages, list_forms
        Branch: get_app
        AppsFlyer: list_apps, get_installs_report(app_id, date_range_start, date_range_end), get_in_app_events_report(app_id, date_range_start, date_range_end), get_partners_report(app_id, date_range_start, date_range_end)
        Adjust: list_apps, get_report(dimensions, metrics, date_period, ...), get_pivot_report(dimensions, metrics, date_period, index, ...), list_events, list_app_automation_apps, get_partner_links(app_token)
        Braze: list_campaigns(pass filters), get_campaign_details(campaign_id), list_canvases, get_canvas_details(canvas_id), list_segments, get_segment_details(segment_id)
        MoEngage: get_user_info(customer_id), list_campaigns(channel, limit), get_campaign_details(campaign_id, campaign_name)
        """
        if not platform:
            return {
                "error": True,
                "error_type": "missing_required_param",
                "message": "platform is required. Pass platform='google', 'meta', 'tiktok', 'snap', 'linkedin', 'pinterest', 'x', 'reddit', 'apple', 'marketo', 'branch', 'appsflyer', 'adjust', 'braze', or 'moengage' in params.",
            }
        user = _get_user()

        # Validate action name upfront per platform
        _VALID_MARKETING_READ_ACTIONS = {
            "google": {
                "list_accounts",
                "get_campaign_performance",
                "get_ad_group_performance",
                "get_conversion_actions",
                "get_keyword_performance",
            },
            "meta": {"list_accounts", "get_campaign_performance", "get_adset_performance"},
            "tiktok": {"list_accounts", "get_campaign_performance", "get_adgroup_performance"},
            "snap": {"list_accounts", "get_campaign_performance", "get_adsquad_performance"},
            "linkedin": {"list_accounts", "get_campaign_performance", "get_adgroup_performance"},
            "pinterest": {"list_accounts", "get_campaign_performance", "get_adgroup_performance"},
            "x": {"list_accounts", "get_campaign_performance", "get_line_item_performance"},
            "reddit": {"list_accounts", "get_campaign_performance", "get_adgroup_performance"},
            "apple": {"list_accounts", "get_campaign_performance", "get_adgroup_performance"},
            "marketo": {
                "get_leads",
                "get_lead_by_id",
                "list_lead_lists",
                "get_list_leads",
                "get_lead_activities",
                "list_campaigns",
                "list_programs",
                "get_program",
                "list_emails",
                "list_landing_pages",
                "list_forms",
            },
            "branch": {
                "get_app",
                "query_analytics",
            },
            "appsflyer": {
                "list_apps",
                "get_installs_report",
                "get_in_app_events_report",
                "get_partners_report",
            },
            "adjust": {
                "list_apps",
                "get_report",
                "get_pivot_report",
                "list_events",
                "list_app_automation_apps",
                "get_partner_links",
            },
            "braze": {
                "list_campaigns",
                "get_campaign_details",
                "list_canvases",
                "get_canvas_details",
                "list_segments",
                "get_segment_details",
            },
            "moengage": {
                "get_user_info",
                "list_campaigns",
                "get_campaign_details",
                "list_events",
            },
        }
        valid_actions = _VALID_MARKETING_READ_ACTIONS.get(platform, set())
        if action not in valid_actions:
            return {
                "error": True,
                "message": f"Unknown action '{action}' for {platform} marketing_read. "
                f"Valid actions: {', '.join(sorted(valid_actions))}",
            }

        if platform == "google":
            if not user or not user.has_ads:
                return _unauthorized_response("google")
            conn_id = _get_google_conn_id()
            ads = state.ads_connector

            if action == "list_accounts":
                return await cached_tool_response(
                    f"cache:ads:list_accounts:{conn_id}",
                    600,
                    ads.list_accounts,
                    conn_id,
                )
            if not account_id:
                return {"error": True, "message": f"account_id is required for '{action}'"}

            if action == "get_campaign_performance":
                mets_key = ",".join(sorted(metrics)) if metrics else "default"
                return await cached_tool_response(
                    f"cache:ads:campaigns:{conn_id}:{account_id}:{date_range_start}:{date_range_end}:{mets_key}",
                    60,
                    ads.get_campaign_performance,
                    conn_id,
                    account_id,
                    date_range_start,
                    date_range_end,
                    metrics,
                )
            elif action == "get_ad_group_performance":
                return await cached_tool_response(
                    f"cache:ads:adgroups:{conn_id}:{account_id}:{date_range_start}:{date_range_end}:{campaign_id}:{limit}",
                    60,
                    ads.get_ad_group_performance,
                    conn_id,
                    account_id,
                    date_range_start,
                    date_range_end,
                    campaign_id,
                    limit,
                )
            elif action == "get_conversion_actions":
                return await cached_tool_response(
                    f"cache:ads:conversions:{conn_id}:{account_id}",
                    120,
                    ads.get_conversion_actions,
                    conn_id,
                    account_id,
                )
            elif action == "get_keyword_performance":
                return await cached_tool_response(
                    f"cache:ads:keywords:{conn_id}:{account_id}:{date_range_start}:{date_range_end}:{campaign_id}:{limit}",
                    60,
                    ads.get_keyword_performance,
                    conn_id,
                    account_id,
                    date_range_start,
                    date_range_end,
                    campaign_id,
                    limit,
                )

            return {"error": True, "message": f"Unknown action '{action}' for Google Ads"}

        elif platform == "meta":
            token = _get_provider_token("meta")
            if not token:
                return _unauthorized_response("meta")

            meta = state.meta_connector
            uid = user.user_id if user else "anon"
            if action == "list_accounts":
                return await cached_tool_response(
                    f"cache:meta:accounts:{uid}",
                    600,
                    meta.list_accounts,
                    token,
                )
            if not account_id:
                return {"error": True, "message": f"account_id is required for '{action}'"}
            if action == "get_campaign_performance":
                mets_key = ",".join(sorted(metrics)) if metrics else "default"
                return await cached_tool_response(
                    f"cache:meta:campaigns:{uid}:{account_id}:{date_range_start}:{date_range_end}:{mets_key}",
                    60,
                    meta.get_campaign_performance,
                    token,
                    account_id,
                    date_range_start,
                    date_range_end,
                    metrics,
                )
            elif action == "get_adset_performance":
                return await cached_tool_response(
                    f"cache:meta:adsets:{uid}:{account_id}:{date_range_start}:{date_range_end}:{campaign_id}",
                    60,
                    meta.get_adset_performance,
                    token,
                    account_id,
                    date_range_start,
                    date_range_end,
                    campaign_id,
                )
            return {
                "error": True,
                "message": f"Unknown action '{action}' for Meta Ads. Supported: list_accounts, get_campaign_performance, get_adset_performance",
            }

        elif platform == "tiktok":
            token = _get_provider_token("tiktok")
            if not token:
                return _unauthorized_response("tiktok")

            tiktok = state.tiktok_connector
            uid = user.user_id if user else "anon"
            if action == "list_accounts":
                return await cached_tool_response(
                    f"cache:tiktok:accounts:{uid}",
                    600,
                    tiktok.list_accounts,
                    token,
                )
            if not account_id:
                return {"error": True, "message": f"account_id is required for '{action}'"}
            if action == "get_campaign_performance":
                mets_key = ",".join(sorted(metrics)) if metrics else "default"
                return await cached_tool_response(
                    f"cache:tiktok:campaigns:{uid}:{account_id}:{date_range_start}:{date_range_end}:{mets_key}",
                    60,
                    tiktok.get_campaign_performance,
                    token,
                    account_id,
                    date_range_start,
                    date_range_end,
                    metrics,
                )
            elif action == "get_adgroup_performance":
                return await cached_tool_response(
                    f"cache:tiktok:adgroups:{uid}:{account_id}:{date_range_start}:{date_range_end}:{campaign_id}",
                    60,
                    tiktok.get_adgroup_performance,
                    token,
                    account_id,
                    date_range_start,
                    date_range_end,
                    campaign_id,
                )
            return {
                "error": True,
                "message": f"Unknown action '{action}' for TikTok Ads. Supported: list_accounts, get_campaign_performance, get_adgroup_performance",
            }

        elif platform == "snap":
            token = _get_provider_token("snap")
            if not token:
                return _unauthorized_response("snap")

            snap = state.snap_connector
            uid = user.user_id if user else "anon"
            if action == "list_accounts":
                return await cached_tool_response(
                    f"cache:snap:accounts:{uid}",
                    600,
                    snap.list_accounts,
                    token,
                )
            if not account_id:
                return {"error": True, "message": f"account_id is required for '{action}'"}
            if action == "get_campaign_performance":
                mets_key = ",".join(sorted(metrics)) if metrics else "default"
                return await cached_tool_response(
                    f"cache:snap:campaigns:{uid}:{account_id}:{date_range_start}:{date_range_end}:{mets_key}",
                    60,
                    snap.get_campaign_performance,
                    token,
                    account_id,
                    date_range_start,
                    date_range_end,
                    metrics,
                )
            elif action == "get_adsquad_performance":
                return await cached_tool_response(
                    f"cache:snap:adsquads:{uid}:{account_id}:{date_range_start}:{date_range_end}:{campaign_id}",
                    60,
                    snap.get_adsquad_performance,
                    token,
                    account_id,
                    date_range_start,
                    date_range_end,
                    campaign_id,
                )
            return {
                "error": True,
                "message": f"Unknown action '{action}' for Snap Ads. Supported: list_accounts, get_campaign_performance, get_adsquad_performance",
            }

        elif platform == "linkedin":
            token = _get_provider_token("linkedin")
            if not token:
                return _unauthorized_response("linkedin")
            linkedin = state.linkedin_connector
            uid = user.user_id if user else "anon"
            if action == "list_accounts":
                return await cached_tool_response(
                    f"cache:linkedin:accounts:{uid}",
                    600,
                    linkedin.list_accounts,
                    token,
                )
            if not account_id:
                return {"error": True, "message": f"account_id is required for '{action}'"}
            if action == "get_campaign_performance":
                mets_key = ",".join(sorted(metrics)) if metrics else "default"
                return await cached_tool_response(
                    f"cache:linkedin:campaigns:{uid}:{account_id}:{date_range_start}:{date_range_end}:{mets_key}",
                    60,
                    linkedin.get_campaign_performance,
                    token,
                    account_id,
                    date_range_start,
                    date_range_end,
                    metrics,
                )
            elif action == "get_adgroup_performance":
                return await cached_tool_response(
                    f"cache:linkedin:adgroups:{uid}:{account_id}:{date_range_start}:{date_range_end}:{campaign_id}:{limit}",
                    60,
                    linkedin.get_adgroup_performance,
                    token,
                    account_id,
                    date_range_start,
                    date_range_end,
                    campaign_id,
                )
            return {
                "error": True,
                "message": f"Unknown action '{action}' for LinkedIn Ads. Supported: list_accounts, get_campaign_performance, get_adgroup_performance",
            }

        elif platform == "pinterest":
            token = _get_provider_token("pinterest")
            if not token:
                return _unauthorized_response("pinterest")
            pinterest = state.pinterest_connector
            uid = user.user_id if user else "anon"
            if action == "list_accounts":
                return await cached_tool_response(
                    f"cache:pinterest:accounts:{uid}",
                    600,
                    pinterest.list_accounts,
                    token,
                )
            if not account_id:
                return {"error": True, "message": f"account_id is required for '{action}'"}
            if action == "get_campaign_performance":
                mets_key = ",".join(sorted(metrics)) if metrics else "default"
                return await cached_tool_response(
                    f"cache:pinterest:campaigns:{uid}:{account_id}:{date_range_start}:{date_range_end}:{mets_key}",
                    60,
                    pinterest.get_campaign_performance,
                    token,
                    account_id,
                    date_range_start,
                    date_range_end,
                    metrics,
                )
            elif action == "get_adgroup_performance":
                return await cached_tool_response(
                    f"cache:pinterest:adgroups:{uid}:{account_id}:{date_range_start}:{date_range_end}:{campaign_id}:{limit}",
                    60,
                    pinterest.get_adgroup_performance,
                    token,
                    account_id,
                    date_range_start,
                    date_range_end,
                    campaign_id,
                )
            return {
                "error": True,
                "message": f"Unknown action '{action}' for Pinterest Ads. Supported: list_accounts, get_campaign_performance, get_adgroup_performance",
            }

        elif platform == "x":
            oauth1_tokens = _get_provider_oauth1_tokens("x")
            if not oauth1_tokens:
                return _unauthorized_response("x")
            from app.auth.oauth_app_credentials import get_oauth_app_credentials_cached
            from app.connectors.x_ads import XAdsConnector, XOAuth1Token

            async with state.db_session_factory() as db:
                creds = await get_oauth_app_credentials_cached(db, "x", project_id=get_oauth_app_project_id())
            x_ads = XAdsConnector(creds.client_id, creds.client_secret)
            x_token = XOAuth1Token(token=oauth1_tokens[0], token_secret=oauth1_tokens[1])
            uid = user.user_id if user else "anon"
            if action == "list_accounts":
                return await cached_tool_response(
                    f"cache:x:accounts:{uid}",
                    600,
                    x_ads.list_accounts,
                    x_token,
                )
            if not account_id:
                return {"error": True, "message": f"account_id is required for '{action}'"}
            if action == "get_campaign_performance":
                mets_key = ",".join(sorted(metrics)) if metrics else "default"
                return await cached_tool_response(
                    f"cache:x:campaigns:{uid}:{account_id}:{date_range_start}:{date_range_end}:{mets_key}",
                    60,
                    x_ads.get_campaign_performance,
                    x_token,
                    account_id,
                    date_range_start,
                    date_range_end,
                    metrics,
                )
            elif action == "get_line_item_performance":
                return await cached_tool_response(
                    f"cache:x:line_items:{uid}:{account_id}:{date_range_start}:{date_range_end}:{campaign_id}",
                    60,
                    x_ads.get_line_item_performance,
                    x_token,
                    account_id,
                    date_range_start,
                    date_range_end,
                    campaign_id,
                )
            return {
                "error": True,
                "message": f"Unknown action '{action}' for X Ads. Supported: list_accounts, get_campaign_performance, get_line_item_performance",
            }

        elif platform == "reddit":
            token = _get_provider_token("reddit")
            if not token:
                return _unauthorized_response("reddit")
            reddit = state.reddit_connector
            uid = user.user_id if user else "anon"
            if action == "list_accounts":
                return await cached_tool_response(
                    f"cache:reddit:accounts:{uid}",
                    600,
                    reddit.list_ad_accounts,
                    token,
                )
            if not account_id:
                return {"error": True, "message": f"account_id is required for '{action}'"}
            if action == "get_campaign_performance":
                mets_key = ",".join(sorted(metrics)) if metrics else "default"
                return await cached_tool_response(
                    f"cache:reddit:campaigns:{uid}:{account_id}:{date_range_start}:{date_range_end}:{mets_key}",
                    60,
                    reddit.get_campaign_performance,
                    token,
                    account_id,
                    date_range_start,
                    date_range_end,
                    metrics,
                )
            elif action == "get_adgroup_performance":
                return await cached_tool_response(
                    f"cache:reddit:adgroups:{uid}:{account_id}:{date_range_start}:{date_range_end}:{campaign_id}:{limit}",
                    60,
                    reddit.get_adgroup_performance,
                    token,
                    account_id,
                    date_range_start,
                    date_range_end,
                    campaign_id,
                )
            return {
                "error": True,
                "message": f"Unknown action '{action}' for Reddit Ads. Supported: list_accounts, get_campaign_performance, get_adgroup_performance",
            }

        elif platform == "apple":
            token = _get_provider_token("apple")
            if not token:
                return _unauthorized_response("apple")
            apple = state.apple_connector
            uid = user.user_id if user else "anon"
            if action == "list_accounts":
                return await cached_tool_response(
                    f"cache:apple:accounts:{uid}",
                    600,
                    apple.list_accounts,
                    token,
                )
            if not account_id:
                return {"error": True, "message": f"account_id is required for '{action}'"}
            if action == "get_campaign_performance":
                mets_key = ",".join(sorted(metrics)) if metrics else "default"
                return await cached_tool_response(
                    f"cache:apple:campaigns:{uid}:{account_id}:{date_range_start}:{date_range_end}:{mets_key}",
                    60,
                    apple.get_campaign_performance,
                    token,
                    account_id,
                    date_range_start,
                    date_range_end,
                    metrics,
                )
            elif action == "get_adgroup_performance":
                return await cached_tool_response(
                    f"cache:apple:adgroups:{uid}:{account_id}:{date_range_start}:{date_range_end}:{campaign_id}:{limit}",
                    60,
                    apple.get_adgroup_performance,
                    token,
                    account_id,
                    date_range_start,
                    date_range_end,
                    campaign_id,
                )
            return {
                "error": True,
                "message": f"Unknown action '{action}' for Apple Ads. Supported: list_accounts, get_campaign_performance, get_adgroup_performance",
            }
        elif platform == "marketo":
            if not user or not getattr(user, "has_adobe_marketo", False):
                return _no_marketo()
            conn_id, instance_url, client_id, client_secret = await _get_marketo_conn(user.user_id)
            if not conn_id:
                return _no_marketo()
            mk = state.adobe_marketo_connector
            f = filters or {}
            creds = (instance_url, client_id, client_secret)

            if action == "get_leads":
                return await mk.get_leads(
                    *creds,
                    filter_type=f.get("filter_type", "email"),
                    filter_values=f.get("filter_values"),
                    fields=f.get("fields"),
                    limit=limit,
                )
            if action == "get_lead_by_id":
                if not resource_id:
                    return {"error": True, "message": "resource_id (lead id) is required for get_lead_by_id"}
                return await mk.get_lead_by_id(*creds, lead_id=resource_id, fields=f.get("fields"))
            if action == "list_lead_lists":
                return await mk.list_lead_lists(*creds, limit=limit)
            if action == "get_list_leads":
                if not resource_id:
                    return {"error": True, "message": "resource_id (list id) is required for get_list_leads"}
                return await mk.get_list_leads(*creds, list_id=resource_id, limit=limit)
            if action == "get_lead_activities":
                return await mk.get_lead_activities(
                    *creds,
                    activity_type_ids=f.get("activity_type_ids"),
                    list_id=f.get("list_id") or resource_id,
                    since_datetime=f.get("since_datetime"),
                    limit=limit,
                )
            if action == "list_campaigns":
                return await mk.list_campaigns(*creds, limit=limit)
            if action == "list_programs":
                return await mk.list_programs(*creds, limit=limit)
            if action == "get_program":
                if not resource_id:
                    return {"error": True, "message": "resource_id (program id) is required for get_program"}
                return await mk.get_program(*creds, program_id=resource_id)
            if action == "list_emails":
                return await mk.list_emails(*creds, limit=limit)
            if action == "list_landing_pages":
                return await mk.list_landing_pages(*creds, limit=limit)
            if action == "list_forms":
                return await mk.list_forms(*creds, limit=limit)

        elif platform == "branch":
            if not user or not getattr(user, "has_branch", False):
                return _no_branch()
            conn_id, api_key, secret_key = await _get_branch_conn(user.user_id)
            if not api_key:
                return _no_branch()
            br = state.branch_connector

            if action in ("get_app", "branch_get_app"):
                return await cached_tool_response(
                    f"cache:branch:app:{conn_id}",
                    300,
                    br.get_app,
                    api_key,
                    secret_key,
                )
            elif action in ("query_analytics", "branch_query_analytics"):
                if not date_range_start or not date_range_end:
                    return {
                        "error": True,
                        "message": "date_range_start and date_range_end are required for Branch query_analytics",
                    }
                dim_key = ",".join(sorted(dimensions)) if dimensions else "none"
                data_source = (filters or {}).get("data_source", "eo_click")
                return await cached_tool_response(
                    f"cache:branch:analytics:{conn_id}:{date_range_start}:{date_range_end}:{dim_key}:{data_source}",
                    60,
                    br.query_analytics,
                    api_key,
                    secret_key,
                    date_range_start,
                    date_range_end,
                    data_source=data_source,
                    dimensions=dimensions,
                )
            # request_daily_export is a write action (see marketing_write)

            return {"error": True, "message": f"Unknown action '{action}' for Branch marketing_read"}

        elif platform == "appsflyer":
            if not user or not getattr(user, "has_appsflyer", False):
                return _no_appsflyer()
            conn_id, api_key, secret_key = await _get_appsflyer_conn(user.user_id)
            if not api_key:
                return _no_appsflyer()
            af = state.appsflyer_connector

            if action == "list_apps":
                return await cached_tool_response(
                    f"cache:appsflyer:apps:{conn_id}",
                    300,
                    af.list_apps,
                    api_key,
                )
            if not app_id:
                return {"error": True, "message": "app_id is required for this AppsFlyer action"}
            elif action == "get_installs_report":
                if not date_range_start or not date_range_end:
                    return {"error": True, "message": "date_range_start and date_range_end are required"}
                return await cached_tool_response(
                    f"cache:appsflyer:installs:{conn_id}:{app_id}:{date_range_start}:{date_range_end}",
                    300,
                    af.get_installs_report,
                    api_key,
                    app_id,
                    date_range_start,
                    date_range_end,
                )
            elif action == "get_in_app_events_report":
                if not date_range_start or not date_range_end:
                    return {"error": True, "message": "date_range_start and date_range_end are required"}
                return await cached_tool_response(
                    f"cache:appsflyer:events:{conn_id}:{app_id}:{date_range_start}:{date_range_end}",
                    300,
                    af.get_in_app_events_report,
                    api_key,
                    app_id,
                    date_range_start,
                    date_range_end,
                )
            elif action == "get_partners_report":
                if not date_range_start or not date_range_end:
                    return {"error": True, "message": "date_range_start and date_range_end are required"}
                return await cached_tool_response(
                    f"cache:appsflyer:partners:{conn_id}:{app_id}:{date_range_start}:{date_range_end}",
                    300,
                    af.get_partners_report,
                    api_key,
                    app_id,
                    date_range_start,
                    date_range_end,
                )

            return {"error": True, "message": f"Unknown action '{action}' for AppsFlyer marketing_read"}

        elif platform == "adjust":
            if not user or not getattr(user, "has_adjust", False):
                return _no_adjust()
            conn_id, api_key, secret_key = await _get_adjust_conn(user.user_id)
            if not api_key:
                return _no_adjust()
            adj = state.adjust_connector

            if action == "list_apps":
                return await cached_tool_response(
                    f"cache:adjust:apps:{conn_id}",
                    300,
                    adj.list_apps,
                    api_key,
                )
            elif action == "get_report":
                if not date_range_start or not date_range_end:
                    return {
                        "error": True,
                        "message": "date_range_start and date_range_end are required for get_report",
                    }
                # dimensions/metrics/date_period are passed as top-level args (not inside payload)
                # filters may contain app_token etc.
                f = filters or {}
                dims = f.get("dimensions") or "app,tracker"
                mets = f.get("metrics") or "installs,clicks"
                period = f.get("date_period") or f"{date_range_start}:{date_range_end}"
                # remove internal keys so only real filter params remain
                extra = {k: v for k, v in f.items() if k not in ("dimensions", "metrics", "date_period")}
                return await adj.get_report(api_key, dims, mets, period, **extra)
            elif action == "get_pivot_report":
                if not date_range_start or not date_range_end:
                    return {
                        "error": True,
                        "message": "date_range_start and date_range_end are required for get_pivot_report",
                    }
                f = filters or {}
                dims = f.get("dimensions") or "app,tracker"
                mets = f.get("metrics") or "installs,clicks"
                period = f.get("date_period") or f"{date_range_start}:{date_range_end}"
                idx = f.get("index") or "tracker"
                extra = {
                    k: v for k, v in f.items() if k not in ("dimensions", "metrics", "date_period", "index")
                }
                return await adj.get_pivot_report(api_key, dims, mets, period, idx, **extra)
            elif action == "list_events":
                return await cached_tool_response(
                    f"cache:adjust:events:{conn_id}",
                    300,
                    adj.list_events,
                    api_key,
                )
            elif action == "list_app_automation_apps":
                return await cached_tool_response(
                    f"cache:adjust:automation_apps:{conn_id}",
                    300,
                    adj.list_app_automation_apps,
                    api_key,
                )
            elif action == "get_partner_links":
                if not resource_id:
                    return {
                        "error": True,
                        "message": "resource_id (app_token) is required for get_partner_links",
                    }
                return await cached_tool_response(
                    f"cache:adjust:partner_links:{conn_id}:{resource_id}",
                    300,
                    adj.get_partner_links,
                    api_key,
                    resource_id,
                )

            return {"error": True, "message": f"Unknown action '{action}' for Adjust marketing_read"}

        elif platform == "braze":
            if not user or not getattr(user, "has_braze", False):
                return _no_braze()
            creds = await get_braze_creds(user.user_id)
            if not creds:
                return _no_braze()
            brz = state.braze_connector
            rest_url = creds["rest_endpoint_url"]
            api_key_braze = creds["api_key"]
            p = filters or {}

            if action == "list_campaigns":
                page = p.get("page", 0)
                include_archived = p.get("include_archived", False)
                sort_direction = p.get("sort_direction")
                modified_after = p.get("modified_after")
                return await cached_tool_response(
                    f"cache:braze:campaigns:{creds['connection_id']}:{page}:{include_archived}:{sort_direction}:{modified_after}",
                    _BRAZE_CACHE_TTL,
                    brz.list_campaigns,
                    rest_url,
                    api_key_braze,
                    page=page,
                    include_archived=include_archived,
                    sort_direction=sort_direction,
                    modified_after=modified_after,
                )
            elif action == "get_campaign_details":
                cid = campaign_id or p.get("campaign_id")
                if not cid:
                    return {"error": True, "message": "campaign_id is required for get_campaign_details"}
                return await cached_tool_response(
                    f"cache:braze:campaign_details:{creds['connection_id']}:{cid}",
                    _BRAZE_CACHE_TTL,
                    brz.get_campaign_details,
                    rest_url,
                    api_key_braze,
                    cid,
                )
            elif action == "list_canvases":
                page = p.get("page", 0)
                include_archived = p.get("include_archived", False)
                sort_direction = p.get("sort_direction")
                modified_after = p.get("modified_after")
                return await cached_tool_response(
                    f"cache:braze:canvases:{creds['connection_id']}:{page}:{include_archived}:{sort_direction}:{modified_after}",
                    _BRAZE_CACHE_TTL,
                    brz.list_canvases,
                    rest_url,
                    api_key_braze,
                    page=page,
                    include_archived=include_archived,
                    sort_direction=sort_direction,
                    modified_after=modified_after,
                )
            elif action == "get_canvas_details":
                cid = p.get("canvas_id") or resource_id
                if not cid:
                    return {"error": True, "message": "canvas_id is required for get_canvas_details"}
                return await cached_tool_response(
                    f"cache:braze:canvas_details:{creds['connection_id']}:{cid}",
                    _BRAZE_CACHE_TTL,
                    brz.get_canvas_details,
                    rest_url,
                    api_key_braze,
                    cid,
                )
            elif action == "list_segments":
                page = p.get("page", 0)
                sort_direction = p.get("sort_direction")
                return await cached_tool_response(
                    f"cache:braze:segments:{creds['connection_id']}:{page}:{sort_direction}",
                    _BRAZE_CACHE_TTL,
                    brz.list_segments,
                    rest_url,
                    api_key_braze,
                    page=page,
                    sort_direction=sort_direction,
                )
            elif action == "get_segment_details":
                sid = p.get("segment_id") or resource_id
                if not sid:
                    return {"error": True, "message": "segment_id is required for get_segment_details"}
                return await cached_tool_response(
                    f"cache:braze:segment_details:{creds['connection_id']}:{sid}",
                    _BRAZE_CACHE_TTL,
                    brz.get_segment_details,
                    rest_url,
                    api_key_braze,
                    sid,
                )

            return {"error": True, "message": f"Unknown action '{action}' for Braze marketing_read"}

        elif platform == "moengage":
            if not user or not getattr(user, "has_moengage", False):
                return _no_moengage()
            creds = await get_moengage_creds(user.user_id)
            if not creds:
                return _no_moengage()
            moe = state.moengage_connector
            dc = creds["data_center"]
            app_id_moe = creds["app_id"]
            api_key_moe = creds["api_key"]
            p = filters or {}

            if action == "get_user_info":
                cust_id = customer_id or p.get("customer_id")
                moengage_id = p.get("moengage_id")
                user_fields = p.get("user_fields_to_export")
                return await cached_tool_response(
                    f"cache:moengage:user_info:{creds['connection_id']}:{cust_id}:{moengage_id}:{user_fields}",
                    _MOENGAGE_CACHE_TTL,
                    moe.get_user_info,
                    dc,
                    app_id_moe,
                    api_key_moe,
                    customer_id=cust_id,
                    moengage_id=moengage_id,
                    user_fields_to_export=user_fields,
                )
            elif action == "list_campaigns":
                channel = p.get("channel")
                lmt = p.get("limit", limit)
                return await cached_tool_response(
                    f"cache:moengage:campaigns:{creds['connection_id']}:{channel}:{lmt}",
                    _MOENGAGE_CACHE_TTL,
                    moe.list_campaigns,
                    dc,
                    app_id_moe,
                    api_key_moe,
                    channel=channel,
                    limit=lmt,
                )
            elif action == "get_campaign_details":
                camp_id = campaign_id or p.get("campaign_id")
                camp_name = p.get("campaign_name")
                return await cached_tool_response(
                    f"cache:moengage:campaign_details:{creds['connection_id']}:{camp_id}:{camp_name}",
                    _MOENGAGE_CACHE_TTL,
                    moe.get_campaign_details,
                    dc,
                    app_id_moe,
                    api_key_moe,
                    campaign_id=camp_id,
                    campaign_name=camp_name,
                )
            elif action == "list_events":
                return await cached_tool_response(
                    f"cache:moengage:events:{creds['connection_id']}:{limit}",
                    _MOENGAGE_CACHE_TTL,
                    moe.list_events,
                    dc,
                    app_id_moe,
                    api_key_moe,
                    limit=limit,
                )

            return {"error": True, "message": f"Unknown action '{action}' for MoEngage marketing_read"}

        return {"error": True, "message": f"Unknown platform: {platform}"}

    @mcp_server.tool("marketing_audit")
    async def marketing_audit(
        platform: Literal[
            "google",
            "meta",
            "tiktok",
            "snap",
            "linkedin",
            "pinterest",
            "x",
            "reddit",
            "apple",
            "marketo",
            "branch",
            "appsflyer",
            "adjust",
            "braze",
            "moengage",
        ]
        | None = None,
        action: str = "",
        account_id: str | None = None,
        date_range_start: str | None = None,
        date_range_end: str | None = None,
        campaign_id: str | None = None,
    ) -> dict:
        """Audits marketing health: tracking, budgets, quality scores.

        platform: google | meta | tiktok | snap | linkedin | pinterest | x | reddit | apple | marketo | branch | appsflyer | adjust. All: audit_tracking_setup(account_id or app_id/source_id).
        Google only: audit_budget_utilization(account_id+dates), audit_quality_scores(account_id,campaign_id?)
        """
        if not platform:
            return {
                "error": True,
                "error_type": "missing_required_param",
                "message": "platform is required. Pass platform='google', 'meta', 'tiktok', 'snap', 'linkedin', 'pinterest', 'x', 'reddit', 'apple', 'marketo', 'branch', 'appsflyer', 'adjust', 'braze', or 'moengage' in params.",
            }
        user = _get_user()
        if (
            platform not in ("marketo", "branch", "appsflyer", "adjust", "braze", "moengage")
            and not account_id
        ):
            return {
                "error": True,
                "error_type": "bad_request",
                "message": f"account_id is required for {platform} {action}.",
            }

        if platform == "google":
            if not user or not user.has_ads:
                return _unauthorized_response("google")
            conn_id = _get_google_conn_id()
            ads = state.ads_connector

            if action == "audit_tracking_setup":
                return await ads.audit_tracking_setup(conn_id, account_id)
            elif action == "audit_budget_utilization":
                if not date_range_start or not date_range_end:
                    return {
                        "error": True,
                        "message": "date_range_start and date_range_end are required for audit_budget_utilization",
                    }
                return await ads.audit_budget_utilization(
                    conn_id, account_id, date_range_start, date_range_end
                )
            elif action == "audit_quality_scores":
                return await ads.audit_quality_scores(conn_id, account_id, campaign_id)
            return {"error": True, "message": f"Unknown action '{action}' for Google Ads audit"}

        elif platform == "meta":
            token = _get_provider_token("meta")
            if not token:
                return _unauthorized_response("meta")
            if action == "audit_tracking_setup":
                return await state.meta_connector.audit_tracking_setup(token, account_id)
            return {
                "error": True,
                "message": f"Unknown action '{action}' for Meta Ads audit. Supported: audit_tracking_setup",
            }

        elif platform == "tiktok":
            token = _get_provider_token("tiktok")
            if not token:
                return _unauthorized_response("tiktok")
            if action == "audit_tracking_setup":
                return await state.tiktok_connector.audit_tracking_setup(token, account_id)
            return {
                "error": True,
                "message": f"Unknown action '{action}' for TikTok Ads audit. Supported: audit_tracking_setup",
            }

        elif platform == "snap":
            token = _get_provider_token("snap")
            if not token:
                return _unauthorized_response("snap")
            if action == "audit_tracking_setup":
                return await state.snap_connector.audit_tracking_setup(token, account_id)
            return {
                "error": True,
                "message": f"Unknown action '{action}' for Snap Ads audit. Supported: audit_tracking_setup",
            }

        elif platform == "linkedin":
            token = _get_provider_token("linkedin")
            if not token:
                return _unauthorized_response("linkedin")
            if action == "audit_tracking_setup":
                return await state.linkedin_connector.audit_tracking_setup(token, account_id)
            return {
                "error": True,
                "message": f"Unknown action '{action}' for LinkedIn Ads audit. Supported: audit_tracking_setup",
            }

        elif platform == "pinterest":
            token = _get_provider_token("pinterest")
            if not token:
                return _unauthorized_response("pinterest")
            if action == "audit_tracking_setup":
                return await state.pinterest_connector.audit_tracking_setup(token, account_id)
            return {
                "error": True,
                "message": f"Unknown action '{action}' for Pinterest Ads audit. Supported: audit_tracking_setup",
            }

        elif platform == "x":
            oauth1_tokens = _get_provider_oauth1_tokens("x")
            if not oauth1_tokens:
                return _unauthorized_response("x")
            if action == "audit_tracking_setup":
                from app.auth.oauth_app_credentials import get_oauth_app_credentials_cached
                from app.connectors.x_ads import XAdsConnector, XOAuth1Token

                async with state.db_session_factory() as db:
                    creds = await get_oauth_app_credentials_cached(
                        db, "x", project_id=get_oauth_app_project_id()
                    )
                return await XAdsConnector(creds.client_id, creds.client_secret).audit_tracking_setup(
                    XOAuth1Token(token=oauth1_tokens[0], token_secret=oauth1_tokens[1]), account_id
                )
            return {
                "error": True,
                "message": f"Unknown action '{action}' for X Ads audit. Supported: audit_tracking_setup",
            }

        elif platform == "reddit":
            token = _get_provider_token("reddit")
            if not token:
                return _unauthorized_response("reddit")
            if action == "audit_tracking_setup":
                return await state.reddit_connector.audit_tracking_setup(token, account_id)
            return {
                "error": True,
                "message": f"Unknown action '{action}' for Reddit Ads audit. Supported: audit_tracking_setup",
            }

        elif platform == "apple":
            token = _get_provider_token("apple")
            if not token:
                return _unauthorized_response("apple")
            if action == "audit_tracking_setup":
                return await state.apple_connector.audit_tracking_setup(token, account_id)
            return {
                "error": True,
                "message": f"Unknown action '{action}' for Apple Ads audit. Supported: audit_tracking_setup",
            }

        elif platform == "gtm":
            return {
                "error": True,
                "message": (
                    "GTM audit actions (suggest_improvements, benchmark_health, "
                    "generate_audit_report, etc.) live in the 'tagmanager_audit' tool. "
                    "Please call tagmanager_audit with platform='gtm' instead of marketing_audit."
                ),
            }

        elif platform == "marketo":
            if not user or not getattr(user, "has_adobe_marketo", False):
                return _no_marketo()
            conn_id, instance_url, client_id, client_secret = await _get_marketo_conn(user.user_id)
            if not conn_id:
                return _no_marketo()
            mk = state.adobe_marketo_connector
            creds = (instance_url, client_id, client_secret)
            if action == "audit_instance":
                return await mk.audit_instance(*creds)
            if action == "check_data_quality":
                return await mk.check_data_quality(*creds)
            return {"error": True, "message": f"Unknown marketo audit action: {action}"}

        elif platform == "branch":
            if not user or not getattr(user, "has_branch", False):
                return _no_branch()
            # Branch does not expose audit_tracking_setup in the real API
            return {
                "error": True,
                "message": "Unknown action for Branch marketing_audit. No audit actions are supported.",
            }

        elif platform == "appsflyer":
            if not user or not getattr(user, "has_appsflyer", False):
                return _no_appsflyer()
            # AppsFlyer does not expose audit_tracking_setup in the real API
            return {
                "error": True,
                "message": "Unknown action for AppsFlyer marketing_audit. No audit actions are supported.",
            }

        elif platform == "adjust":
            if not user or not getattr(user, "has_adjust", False):
                return _no_adjust()
            # Adjust does not expose audit_tracking_setup in the real API
            return {
                "error": True,
                "message": "Unknown action for Adjust marketing_audit. No audit actions are supported.",
            }

        elif platform == "braze":
            if not user or not getattr(user, "has_braze", False):
                return _no_braze()
            # Braze does not expose audit_tracking_setup in the real API
            return {
                "error": True,
                "message": "Unknown action for Braze marketing_audit. No audit actions are supported.",
            }

        elif platform == "moengage":
            if not user or not getattr(user, "has_moengage", False):
                return _no_moengage()
            # MoEngage does not expose audit_tracking_setup in the real API
            return {
                "error": True,
                "message": "Unknown action for MoEngage marketing_audit. No audit actions are supported.",
            }

        return {"error": True, "message": f"Unknown platform: {platform}"}

    @mcp_server.tool("marketing_write")
    async def marketing_write(
        platform: Literal[
            "google",
            "meta",
            "tiktok",
            "snap",
            "linkedin",
            "pinterest",
            "x",
            "reddit",
            "apple",
            "marketo",
            "branch",
            "appsflyer",
            "adjust",
            "braze",
            "moengage",
        ]
        | None = None,
        action: str = "",
        account_id: str | None = None,
        campaign_id: str | None = None,
        campaign_name: str | None = None,
        status: str | None = None,
        daily_budget_usd: float | None = None,
        advertising_channel_type: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        bidding_strategy_type: str | None = None,
        resource_id: str | None = None,
        payload: dict | None = None,
        export_date: str | None = None,
    ) -> dict:
        """Write operations for ad platforms. Google requires 'full' tier.

        platform: google | meta | tiktok | snap | linkedin | pinterest | x | reddit | apple | marketo | branch | appsflyer | adjust | braze | moengage.
        create_campaign: campaign_name, advertising_channel_type(SEARCH|DISPLAY|SHOPPING|VIDEO|PERFORMANCE_MAX), daily_budget_usd, start_date
        update_campaign_status: campaign_id, status(ENABLED|PAUSED)
        update_campaign_budget: campaign_id, daily_budget_usd
        Marketo actions: create_or_update_leads, add_leads_to_list, remove_leads_from_list, request_campaign, schedule_campaign
        Branch: request_daily_export(export_date) — requests a daily data export
        Braze: track_users(payload.attributes, payload.events, payload.purchases), send_message(payload.broadcast, payload.messages), trigger_campaign(payload), trigger_canvas(payload), delete_users(payload)
        MoEngage: create_user(customer_id, payload.attributes), update_user(customer_id, payload.attributes), send_push(payload), send_email(payload), send_sms(payload)
        """
        if not platform:
            return {
                "error": True,
                "error_type": "missing_required_param",
                "message": "platform is required. Pass platform='google', 'meta', 'tiktok', 'snap', 'linkedin', 'pinterest', 'x', 'reddit', 'apple', 'marketo', 'branch', 'appsflyer', 'adjust', 'braze', or 'moengage' in params.",
            }
        user = _get_user()
        if (
            platform not in ("marketo", "branch", "appsflyer", "adjust", "braze", "moengage")
            and not account_id
        ):
            return {
                "error": True,
                "error_type": "bad_request",
                "message": f"account_id is required for {platform} {action}.",
            }

        if platform == "google":
            if not user or not user.has_ads:
                return _unauthorized_response("google")

            # Scope check — Google Ads writes require the full analytics/adwords scope
            from app.auth.scopes import SCOPE_GA4_FULL

            conn = next(
                (c for c in (user.connections or []) if getattr(c, "provider", "google") in ("google", "")),
                None,
            )
            granted_scopes = getattr(conn, "scopes", []) if conn else []
            if SCOPE_GA4_FULL not in granted_scopes:
                from app.config import settings

                return {
                    "error": True,
                    "error_type": "insufficient_scope",
                    "message": "Google Ads write operations require 'Full Access' permission.",
                    "action_required": f"Reconnect Google at {settings.APP_BASE_URL}/connect with 'Full Access' tier.",
                }

            conn_id = _get_google_conn_id()
            ads = state.ads_connector

            if action == "create_campaign":
                missing = [
                    f
                    for f in ["campaign_name", "advertising_channel_type", "daily_budget_usd", "start_date"]
                    if locals()[f] is None
                ]
                if missing:
                    return {
                        "error": True,
                        "message": f"Missing required fields for create_campaign: {missing}",
                    }
                return await ads.create_campaign(
                    connection_id=conn_id,
                    customer_id=account_id,
                    campaign_name=campaign_name,
                    advertising_channel_type=advertising_channel_type,
                    daily_budget_micros=int(daily_budget_usd * 1_000_000),
                    start_date=start_date,
                    end_date=end_date,
                    bidding_strategy_type=bidding_strategy_type or "MAXIMIZE_CLICKS",
                )

            elif action == "update_campaign_status":
                if not campaign_id or not status:
                    return {
                        "error": True,
                        "message": "campaign_id and status are required for update_campaign_status",
                    }
                return await ads.update_campaign_status(conn_id, account_id, campaign_id, status.upper())

            elif action == "update_campaign_budget":
                if not campaign_id or daily_budget_usd is None:
                    return {
                        "error": True,
                        "message": "campaign_id and daily_budget_usd are required for update_campaign_budget",
                    }
                return await ads.update_campaign_budget(
                    conn_id, account_id, campaign_id, int(daily_budget_usd * 1_000_000)
                )

            return {"error": True, "message": f"Unknown action '{action}' for Google Ads write"}

        elif platform == "meta":
            token = _get_provider_token("meta")
            if not token:
                return _unauthorized_response("meta")

            if action == "create_campaign":
                missing = [f for f in ["campaign_name", "advertising_channel_type"] if locals()[f] is None]
                if missing:
                    return {
                        "error": True,
                        "message": f"Missing required fields for Meta create_campaign: {missing}. advertising_channel_type maps to Meta objective (e.g. OUTCOME_SALES, OUTCOME_TRAFFIC).",
                    }
                return await state.meta_connector.create_campaign(
                    access_token=token,
                    account_id=account_id,
                    name=campaign_name,
                    objective=advertising_channel_type,  # reuse field as objective
                    status=(status or "PAUSED").upper(),
                    daily_budget=daily_budget_usd,
                )
            elif action == "update_campaign_status":
                if not campaign_id or not status:
                    return {
                        "error": True,
                        "message": "campaign_id and status are required for update_campaign_status",
                    }
                return await state.meta_connector.update_campaign_status(token, campaign_id, status.upper())
            elif action == "update_campaign_budget":
                if not campaign_id or daily_budget_usd is None:
                    return {
                        "error": True,
                        "message": "campaign_id and daily_budget_usd are required for update_campaign_budget",
                    }
                return await state.meta_connector.update_campaign_budget(
                    access_token=token,
                    campaign_id=campaign_id,
                    daily_budget=daily_budget_usd,
                )
            return {"error": True, "message": f"Unknown action '{action}' for Meta Ads write"}

        elif platform == "tiktok":
            token = _get_provider_token("tiktok")
            if not token:
                return _unauthorized_response("tiktok")

            if action == "create_campaign":
                if campaign_name is None:
                    return {
                        "error": True,
                        "message": "Missing required fields for TikTok create_campaign: ['campaign_name']. Requires campaign_name, advertising_channel_type (as objective_type: TRAFFIC|CONVERSIONS|REACH|VIDEO_VIEWS), daily_budget_usd.",
                    }
                obj_type = advertising_channel_type or (payload or {}).get("objective_type", "TRAFFIC")
                b_mode = (payload or {}).get("budget_mode", "BUDGET_MODE_DAY")
                ops = (payload or {}).get("operation_system")
                call_kwargs = {
                    "access_token": token,
                    "account_id": account_id,
                    "campaign_name": campaign_name,
                    "objective_type": obj_type,
                    "budget": daily_budget_usd or 0,
                    "budget_mode": b_mode,
                }
                if ops is not None:
                    call_kwargs["operation_system"] = ops
                return await state.tiktok_connector.create_campaign(**call_kwargs)
            elif action == "update_campaign_status":
                if not campaign_id or not status:
                    return {
                        "error": True,
                        "message": "campaign_id and status are required for update_campaign_status",
                    }
                return await state.tiktok_connector.update_campaign_status(
                    token, account_id, campaign_id, status.upper()
                )
            elif action == "update_campaign_budget":
                if not campaign_id or daily_budget_usd is None:
                    return {
                        "error": True,
                        "message": "campaign_id and daily_budget_usd are required for update_campaign_budget",
                    }
                return await state.tiktok_connector.update_campaign_budget(
                    token, account_id, campaign_id, daily_budget_usd
                )
            return {
                "error": True,
                "message": f"Unknown action '{action}' for TikTok Ads write. Supported: update_campaign_status, update_campaign_budget",
            }

        elif platform == "snap":
            token = _get_provider_token("snap")
            if not token:
                return _unauthorized_response("snap")

            if action == "create_campaign":
                if campaign_name is None:
                    return {
                        "error": True,
                        "message": "Missing required fields for Snap create_campaign: ['campaign_name']. Requires campaign_name.",
                    }
                return await state.snap_connector.create_campaign(
                    access_token=token,
                    account_id=account_id,
                    name=campaign_name,
                    status=(status or "PAUSED").upper(),
                    daily_budget=daily_budget_usd,
                    start_date=start_date,
                    objective=advertising_channel_type or (payload or {}).get("objective", "APP_INSTALL"),
                )
            elif action == "update_campaign_status":
                if not campaign_id or not status:
                    return {
                        "error": True,
                        "message": "campaign_id and status are required for update_campaign_status",
                    }
                return await state.snap_connector.update_campaign_status(
                    token, account_id, campaign_id, status.upper()
                )
            elif action == "update_campaign_budget":
                if not campaign_id or daily_budget_usd is None:
                    return {
                        "error": True,
                        "message": "campaign_id and daily_budget_usd are required for update_campaign_budget",
                    }
                # Snap uses micro-currency
                return await state.snap_connector.update_campaign_budget(
                    token, account_id, campaign_id, int(daily_budget_usd * 1_000_000)
                )
            return {
                "error": True,
                "message": f"Unknown action '{action}' for Snap Ads write. Supported: update_campaign_status, update_campaign_budget",
            }

        elif platform == "linkedin":
            token = _get_provider_token("linkedin")
            if not token:
                return _unauthorized_response("linkedin")
            linkedin = state.linkedin_connector
            if action == "create_campaign":
                if campaign_name is None:
                    return {
                        "error": True,
                        "message": "Missing required fields for LinkedIn create_campaign: ['campaign_name']. Requires campaign_name.",
                    }
                return await linkedin.create_campaign(
                    access_token=token,
                    account_id=account_id,
                    name=campaign_name,
                    status=(status or "ACTIVE").upper(),
                    daily_budget=daily_budget_usd,
                    objective_type=advertising_channel_type
                    or (payload or {}).get("objective_type", "BRAND_AWARENESS"),
                )
            elif action == "update_campaign_status":
                if not campaign_id or not status:
                    return {
                        "error": True,
                        "message": "campaign_id and status are required for update_campaign_status",
                    }
                return await linkedin.update_campaign_status(token, account_id, campaign_id, status)
            elif action == "update_campaign_budget":
                if not campaign_id or daily_budget_usd is None:
                    return {
                        "error": True,
                        "message": "campaign_id and daily_budget_usd are required for update_campaign_budget",
                    }
                return await linkedin.update_campaign_budget(token, account_id, campaign_id, daily_budget_usd)
            return {
                "error": True,
                "message": f"Unknown action '{action}' for LinkedIn Ads write. Supported: update_campaign_status, update_campaign_budget",
            }

        elif platform == "pinterest":
            token = _get_provider_token("pinterest")
            if not token:
                return _unauthorized_response("pinterest")
            pinterest = state.pinterest_connector
            if action == "create_campaign":
                if campaign_name is None:
                    return {
                        "error": True,
                        "message": "Missing required fields for Pinterest create_campaign: ['campaign_name']. Requires campaign_name.",
                    }
                return await pinterest.create_campaign(
                    access_token=token,
                    account_id=account_id,
                    name=campaign_name,
                    status=(status or "ACTIVE").upper(),
                    daily_budget=daily_budget_usd,
                    objective_type=advertising_channel_type
                    or (payload or {}).get("objective_type", "AWARENESS"),
                )
            elif action == "update_campaign_status":
                if not campaign_id or not status:
                    return {
                        "error": True,
                        "message": "campaign_id and status are required for update_campaign_status",
                    }
                return await pinterest.update_campaign_status(token, account_id, campaign_id, status)
            elif action == "update_campaign_budget":
                if not campaign_id or daily_budget_usd is None:
                    return {
                        "error": True,
                        "message": "campaign_id and daily_budget_usd are required for update_campaign_budget",
                    }
                return await pinterest.update_campaign_budget(
                    token, account_id, campaign_id, daily_budget_usd
                )
            return {
                "error": True,
                "message": f"Unknown action '{action}' for Pinterest Ads write. Supported: update_campaign_status, update_campaign_budget",
            }

        elif platform == "x":
            oauth1_tokens = _get_provider_oauth1_tokens("x")
            if not oauth1_tokens:
                return _unauthorized_response("x")

            from app.auth.oauth_app_credentials import get_oauth_app_credentials_cached
            from app.connectors.x_ads import XAdsConnector, XOAuth1Token

            async with state.db_session_factory() as db:
                creds = await get_oauth_app_credentials_cached(db, "x", project_id=get_oauth_app_project_id())
            x_ads = XAdsConnector(creds.client_id, creds.client_secret)
            x_token = XOAuth1Token(token=oauth1_tokens[0], token_secret=oauth1_tokens[1])

            if action == "create_campaign":
                if campaign_name is None:
                    return {
                        "error": True,
                        "message": "Missing required fields for X create_campaign: ['campaign_name']. Requires campaign_name, daily_budget_usd, advertising_channel_type (as objective).",
                    }
                return await x_ads.create_campaign(
                    token=x_token,
                    account_id=account_id,
                    name=campaign_name,
                    objective=advertising_channel_type or (payload or {}).get("objective", "WEBSITE_CLICKS"),
                    daily_budget=daily_budget_usd or 0,
                    entity_status=(status or "PAUSED").upper(),
                    start_time=start_date,
                )
            elif action == "update_campaign_status":
                if not campaign_id or not status:
                    return {
                        "error": True,
                        "message": "campaign_id and status are required for update_campaign_status",
                    }
                return await x_ads.update_campaign_status(
                    x_token,
                    account_id,
                    campaign_id,
                    status,
                )
            elif action == "update_campaign_budget":
                if not campaign_id or daily_budget_usd is None:
                    return {
                        "error": True,
                        "message": "campaign_id and daily_budget_usd are required for update_campaign_budget",
                    }
                return await x_ads.update_campaign_budget(
                    x_token,
                    account_id,
                    campaign_id,
                    daily_budget_usd,
                )
            return {
                "error": True,
                "message": f"Unknown action '{action}' for X Ads write. Supported: create_campaign, update_campaign_status, update_campaign_budget",
            }

        elif platform == "reddit":
            token = _get_provider_token("reddit")
            if not token:
                return _unauthorized_response("reddit")
            reddit = state.reddit_connector
            if action == "create_campaign":
                if campaign_name is None:
                    return {
                        "error": True,
                        "message": "Missing required fields for Reddit create_campaign: ['campaign_name']. Requires campaign_name, daily_budget_usd.",
                    }
                return await reddit.create_campaign(
                    access_token=token,
                    account_id=account_id,
                    name=campaign_name,
                    daily_budget=daily_budget_usd or 0,
                    configured_status=(status or "PAUSED").upper(),
                )
            elif action == "update_campaign_status":
                if not campaign_id or not status:
                    return {
                        "error": True,
                        "message": "campaign_id and status are required for update_campaign_status",
                    }
                return await reddit.update_campaign_status(token, account_id, campaign_id, status)
            elif action == "update_campaign_budget":
                if not campaign_id or daily_budget_usd is None:
                    return {
                        "error": True,
                        "message": "campaign_id and daily_budget_usd are required for update_campaign_budget",
                    }
                return await reddit.update_campaign_budget(token, account_id, campaign_id, daily_budget_usd)
            return {
                "error": True,
                "message": f"Unknown action '{action}' for Reddit Ads write. Supported: update_campaign_status, update_campaign_budget",
            }

        elif platform == "apple":
            token = _get_provider_token("apple")
            if not token:
                return _unauthorized_response("apple")
            if action == "create_campaign":
                if campaign_name is None:
                    return {
                        "error": True,
                        "message": "Missing required fields for Apple create_campaign: ['campaign_name']. Requires campaign_name.",
                    }
                return await state.apple_connector.create_campaign(
                    access_token=token,
                    org_id=account_id,
                    name=campaign_name,
                    status=(status or "PAUSED").upper(),
                    daily_budget=daily_budget_usd,
                )
            elif action == "update_campaign_status":
                if not campaign_id or not status:
                    return {
                        "error": True,
                        "message": "campaign_id and status are required for update_campaign_status",
                    }
                return await state.apple_connector.update_campaign_status(
                    token, account_id, campaign_id, status
                )
            elif action == "update_campaign_budget":
                if not campaign_id or daily_budget_usd is None:
                    return {
                        "error": True,
                        "message": "campaign_id and daily_budget_usd are required for update_campaign_budget",
                    }
                return await state.apple_connector.update_campaign_budget(
                    token, account_id, campaign_id, daily_budget_usd
                )
            return {
                "error": True,
                "message": f"Unknown action '{action}' for Apple Ads write. Supported: create_campaign, update_campaign_status, update_campaign_budget",
            }
        elif platform == "marketo":
            if not user or not getattr(user, "has_adobe_marketo", False):
                return _no_marketo()
            conn_id, instance_url, client_id, client_secret = await _get_marketo_conn(user.user_id)
            if not conn_id:
                return _no_marketo()
            mk = state.adobe_marketo_connector
            p = payload or {}
            creds = (instance_url, client_id, client_secret)

            if action == "create_or_update_leads":
                if not p.get("leads"):
                    return {"error": True, "message": "payload.leads (list) is required"}
                return await mk.create_or_update_leads(
                    *creds,
                    leads=p["leads"],
                    lookup_field=p.get("lookup_field", "email"),
                    action=p.get("action", "createOrUpdate"),
                )
            if action == "add_leads_to_list":
                if not resource_id or not p.get("lead_ids"):
                    return {
                        "error": True,
                        "message": "resource_id (list id) and payload.lead_ids are required",
                    }
                return await mk.add_leads_to_list(*creds, list_id=resource_id, lead_ids=p["lead_ids"])
            if action == "remove_leads_from_list":
                if not resource_id or not p.get("lead_ids"):
                    return {
                        "error": True,
                        "message": "resource_id (list id) and payload.lead_ids are required",
                    }
                return await mk.remove_leads_from_list(*creds, list_id=resource_id, lead_ids=p["lead_ids"])
            if action == "request_campaign":
                if not resource_id or not p.get("lead_ids"):
                    return {
                        "error": True,
                        "message": "resource_id (campaign id) and payload.lead_ids are required",
                    }
                return await mk.request_campaign(
                    *creds, campaign_id=resource_id, lead_ids=p["lead_ids"], tokens=p.get("tokens")
                )
            if action == "schedule_campaign":
                if not resource_id:
                    return {
                        "error": True,
                        "message": "resource_id (campaign id) is required for schedule_campaign",
                    }
                return await mk.schedule_campaign(
                    *creds, campaign_id=resource_id, run_at=p.get("run_at"), tokens=p.get("tokens")
                )
            return {"error": True, "message": f"Unknown marketo write action: {action}"}

        elif platform == "branch":
            if not user or not getattr(user, "has_branch", False):
                return _no_branch()
            conn_id, api_key, secret_key = await _get_branch_conn(user.user_id)
            if not api_key:
                return _no_branch()
            br = state.branch_connector
            if action == "request_daily_export":
                if not export_date:
                    return {
                        "error": True,
                        "message": "export_date (YYYY-MM-DD) is required for Branch request_daily_export",
                    }
                return await br.request_daily_export(api_key, secret_key, export_date)
            return {
                "error": True,
                "message": "Branch write ops not supported (only request_daily_export via marketing_write is allowed)",
            }

        elif platform == "appsflyer":
            if not user or not getattr(user, "has_appsflyer", False):
                return _no_appsflyer()
            # AppsFlyer has no real write ops exposed here
            return {"error": True, "message": "AppsFlyer write ops not supported"}

        elif platform == "adjust":
            if not user or not getattr(user, "has_adjust", False):
                return _no_adjust()
            # Adjust has no write ops
            return {"error": True, "message": "Adjust write ops not supported"}

        elif platform == "braze":
            if not user or not getattr(user, "has_braze", False):
                return _no_braze()
            creds = await get_braze_creds(user.user_id)
            if not creds:
                return _no_braze()
            brz = state.braze_connector
            rest_url = creds["rest_endpoint_url"]
            api_key_braze = creds["api_key"]
            p = payload or {}

            if action == "track_users":
                return await brz.track_users(
                    rest_url,
                    api_key_braze,
                    attributes=p.get("attributes"),
                    events=p.get("events"),
                    purchases=p.get("purchases"),
                )
            elif action == "send_message":
                return await brz.send_message(
                    rest_url,
                    api_key_braze,
                    broadcast=p.get("broadcast", False),
                    messages=p.get("messages"),
                    external_user_ids=p.get("external_user_ids"),
                    segment_id=p.get("segment_id"),
                )
            elif action == "trigger_campaign":
                campaign_id_braze = p.get("campaign_id")
                if not campaign_id_braze:
                    return {"error": True, "message": "payload.campaign_id is required for trigger_campaign"}
                return await brz.trigger_campaign(
                    rest_url,
                    api_key_braze,
                    campaign_id_braze,
                    broadcast=p.get("broadcast", False),
                    recipients=p.get("recipients"),
                    audience=p.get("audience"),
                    trigger_properties=p.get("trigger_properties"),
                )
            elif action == "trigger_canvas":
                canvas_id_braze = p.get("canvas_id")
                if not canvas_id_braze:
                    return {"error": True, "message": "payload.canvas_id is required for trigger_canvas"}
                return await brz.trigger_canvas(
                    rest_url,
                    api_key_braze,
                    canvas_id_braze,
                    broadcast=p.get("broadcast", False),
                    recipients=p.get("recipients"),
                    audience=p.get("audience"),
                )
            elif action == "delete_users":
                return await brz.delete_users(
                    rest_url,
                    api_key_braze,
                    external_ids=p.get("external_ids"),
                    braze_ids=p.get("braze_ids"),
                )
            elif action == "create_user_alias":
                return await brz.create_user_alias(
                    rest_url,
                    api_key_braze,
                    user_aliases=p.get("user_aliases", []),
                )
            elif action == "identify_users":
                return await brz.identify_users(
                    rest_url,
                    api_key_braze,
                    aliases_to_identify=p.get("aliases_to_identify", []),
                )
            elif action == "merge_users":
                return await brz.merge_users(
                    rest_url,
                    api_key_braze,
                    merge_updates=p.get("merge_updates", []),
                )

            return {"error": True, "message": f"Unknown action '{action}' for Braze marketing_write"}

        elif platform == "moengage":
            if not user or not getattr(user, "has_moengage", False):
                return _no_moengage()
            creds = await get_moengage_creds(user.user_id)
            if not creds:
                return _no_moengage()
            moe = state.moengage_connector
            dc = creds["data_center"]
            app_id_moe = creds["app_id"]
            api_key_moe = creds["api_key"]
            p = payload or {}

            if action == "create_user":
                customer_id = p.get("customer_id")
                if not customer_id:
                    return {"error": True, "message": "payload.customer_id is required for create_user"}
                return await moe.create_user(
                    dc,
                    app_id_moe,
                    api_key_moe,
                    customer_id=customer_id,
                    attributes=p.get("attributes"),
                    platforms=p.get("platforms"),
                    update_existing_only=p.get("update_existing_only", False),
                )
            elif action == "update_user":
                customer_id = p.get("customer_id")
                if not customer_id:
                    return {"error": True, "message": "payload.customer_id is required for update_user"}
                return await moe.update_user(
                    dc,
                    app_id_moe,
                    api_key_moe,
                    customer_id=customer_id,
                    attributes=p.get("attributes"),
                    platforms=p.get("platforms"),
                )
            elif action == "send_push":
                return await moe.send_push(
                    dc,
                    app_id_moe,
                    api_key_moe,
                    campaign_name=p.get("campaign_name", ""),
                    target_platform=p.get("target_platform", []),
                    target_audience=p.get("target_audience", "All Users"),
                    payload=p.get("payload", {}),
                    target_user_attributes=p.get("target_user_attributes"),
                    custom_segment_name=p.get("custom_segment_name"),
                    campaign_delivery=p.get("campaign_delivery"),
                    advanced_settings=p.get("advanced_settings"),
                    campaign_tags=p.get("campaign_tags"),
                )
            elif action == "send_email":
                return await moe.send_email(
                    dc,
                    app_id_moe,
                    api_key_moe,
                    transaction_id=p.get("transaction_id", ""),
                    recipients=p.get("recipients", {}),
                    alert_id=p.get("alert_id"),
                    alert_reference_name=p.get("alert_reference_name"),
                    personalization=p.get("personalization"),
                )
            elif action == "send_sms":
                return await moe.send_sms(
                    dc,
                    app_id_moe,
                    api_key_moe,
                    transaction_id=p.get("transaction_id", ""),
                    recipients=p.get("recipients", {}),
                    alert_id=p.get("alert_id"),
                    alert_reference_name=p.get("alert_reference_name"),
                    personalization=p.get("personalization"),
                )
            elif action == "add_device":
                customer_id = p.get("customer_id")
                if not customer_id:
                    return {"error": True, "message": "payload.customer_id is required for add_device"}
                return await moe.add_device(
                    dc,
                    app_id_moe,
                    api_key_moe,
                    customer_id=customer_id,
                    push_token=p.get("push_token", ""),
                    device_platform=p.get("device_platform", "ANDROID"),
                    device_id=p.get("device_id"),
                )

            return {"error": True, "message": f"Unknown action '{action}' for MoEngage marketing_write"}

        return {"error": True, "message": f"Unknown platform: {platform}"}
