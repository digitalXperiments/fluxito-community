"""
Google Ads Connector

Wraps google-ads Python SDK for:
  Layer 1 — data access (accounts, campaigns, keywords, conversions, ad groups)
  Layer 2 — audit intelligence (tracking setup, budget utilisation, quality scores)
  Layer 3 — write operations (create/pause/enable campaigns, update budgets)
"""

from app.connectors.base import BaseConnector
from app.connectors.errors import friendly_errors


class GoogleAdsConnector(BaseConnector):
    def _build_client(self, refresh_token: str, developer_token: str, client_id: str, client_secret: str):
        """Build GoogleAdsClient from a refresh token and resolved app credentials."""
        from google.ads.googleads.client import GoogleAdsClient

        config = {
            "developer_token": developer_token,
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "use_proto_plus": False,
        }
        return GoogleAdsClient.load_from_dict(config, version="v18")

    async def _get_connection_secrets(self, connection_id: str) -> tuple[str, str | None]:
        """Return (decrypted refresh token, project_id) for a connection.

        The project decides which OAuth app refreshes the token: the
        project's own Google app when it has one, else the install's.
        """
        from sqlalchemy import select

        async with self.token_manager.db_session_factory() as db:
            from app.models.connection import OAuthConnection

            result = await db.execute(select(OAuthConnection).where(OAuthConnection.id == connection_id))
            connection = result.scalar_one_or_none()
            if not connection:
                raise ValueError(f"Connection {connection_id} not found")
            project_id = str(connection.project_id) if connection.project_id else None
            return self.token_manager.decrypt(connection.refresh_token_encrypted), project_id

    async def _get_refresh_token(self, connection_id: str) -> str:
        """Retrieve decrypted refresh token from DB for Ads SDK."""
        refresh_token, _project_id = await self._get_connection_secrets(connection_id)
        return refresh_token

    async def _get_ads_creds(self, project_id: str | None = None) -> tuple[str, str, str]:
        """Return (developer_token, client_id, client_secret) for the project's Google app."""
        from app.auth.oauth_app_credentials import get_oauth_app_credentials_cached

        async with self.token_manager.db_session_factory() as db:
            creds = await get_oauth_app_credentials_cached(db, "google", project_id=project_id)
        developer_token = creds.extra.get("developer_token", "")
        return developer_token, creds.client_id, creds.client_secret

    async def _get_client_inputs(self, connection_id: str) -> tuple[str, str, str, str]:
        """Return (refresh_token, developer_token, client_id, client_secret).

        Loads the connection (refresh token + its project), then the OAuth
        app credentials of that project — all four values needed to build a
        GoogleAdsClient.
        """
        refresh_token, project_id = await self._get_connection_secrets(connection_id)
        developer_token, client_id, client_secret = await self._get_ads_creds(project_id)
        return refresh_token, developer_token, client_id, client_secret

    # ------------------------------------------------------------------
    # Layer 1: Data Access
    # ------------------------------------------------------------------

    @friendly_errors("Google Ads")
    async def list_accounts(self, connection_id: str) -> dict:
        refresh_token, _dev_tok, _cid, _csec = await self._get_client_inputs(connection_id)

        def _run():
            client = self._build_client(refresh_token, _dev_tok, _cid, _csec)
            ga_service = client.get_service("GoogleAdsService")

            # List accessible customers
            customer_service = client.get_service("CustomerService")
            accessible = customer_service.list_accessible_customers()

            accounts = []
            for resource_name in accessible.resource_names:
                customer_id = resource_name.split("/")[-1]
                try:
                    query = f"""
                        SELECT
                            customer.id,
                            customer.descriptive_name,
                            customer.currency_code,
                            customer.time_zone,
                            customer.manager
                        FROM customer
                        WHERE customer.id = {customer_id}
                    """
                    stream = ga_service.search_stream(customer_id=customer_id, query=query)
                    for batch in stream:
                        for row in batch.results:
                            accounts.append(
                                {
                                    "customer_id": str(row.customer.id),
                                    "account_name": row.customer.descriptive_name,
                                    "currency": row.customer.currency_code,
                                    "timezone": row.customer.time_zone,
                                    "is_manager_account": row.customer.manager,
                                }
                            )
                except Exception:
                    continue

            return {"accounts": accounts}

        return await self.run_sync(_run)

    @friendly_errors("Google Ads")
    async def get_campaign_performance(
        self,
        connection_id: str,
        customer_id: str,
        date_range_start: str,
        date_range_end: str,
        metrics: list | None = None,
    ) -> dict:
        if metrics is None:
            metrics = ["clicks", "impressions", "cost_micros", "conversions", "ctr", "average_cpc"]

        refresh_token, _dev_tok, _cid, _csec = await self._get_client_inputs(connection_id)

        def _run():
            client = self._build_client(refresh_token, _dev_tok, _cid, _csec)
            ga_service = client.get_service("GoogleAdsService")

            metric_fields = ", ".join([f"metrics.{m}" for m in metrics])
            query = f"""
                SELECT
                    campaign.id,
                    campaign.name,
                    campaign.advertising_channel_type,
                    campaign.status,
                    {metric_fields}
                FROM campaign
                WHERE segments.date BETWEEN '{date_range_start}' AND '{date_range_end}'
                  AND campaign.status != 'REMOVED'
            """

            campaigns = []
            stream = ga_service.search_stream(customer_id=customer_id.replace("-", ""), query=query)
            for batch in stream:
                for row in batch.results:
                    metric_values = {}
                    for m in metrics:
                        val = getattr(row.metrics, m, None)
                        if val is not None:
                            metric_values[m] = val / 1_000_000 if m == "cost_micros" else val
                    campaigns.append(
                        {
                            "campaign_id": str(row.campaign.id),
                            "campaign_name": row.campaign.name,
                            "campaign_type": str(row.campaign.advertising_channel_type.name),
                            "status": str(row.campaign.status.name),
                            **metric_values,
                        }
                    )

            return {"campaigns": campaigns}

        return await self.run_sync(_run)

    @friendly_errors("Google Ads")
    async def get_conversion_actions(self, connection_id: str, customer_id: str) -> dict:
        refresh_token, _dev_tok, _cid, _csec = await self._get_client_inputs(connection_id)

        def _run():
            client = self._build_client(refresh_token, _dev_tok, _cid, _csec)
            ga_service = client.get_service("GoogleAdsService")

            query = """
                SELECT
                    conversion_action.id,
                    conversion_action.name,
                    conversion_action.category,
                    conversion_action.counting_type,
                    conversion_action.attribution_model_settings.attribution_model,
                    conversion_action.status,
                    conversion_action.include_in_conversions_metric,
                    metrics.conversions
                FROM conversion_action
                WHERE conversion_action.status != 'REMOVED'
            """

            actions = []
            stream = ga_service.search_stream(customer_id=customer_id.replace("-", ""), query=query)
            for batch in stream:
                for row in batch.results:
                    ca = row.conversion_action
                    actions.append(
                        {
                            "conversion_name": ca.name,
                            "category": str(ca.category.name),
                            "counting_type": str(ca.counting_type.name),
                            "attribution_model": str(ca.attribution_model_settings.attribution_model.name),
                            "status": str(ca.status.name),
                            "include_in_conversions": ca.include_in_conversions_metric,
                            "recent_conversion_count": int(row.metrics.conversions),
                        }
                    )

            return {"conversion_actions": actions}

        return await self.run_sync(_run)

    @friendly_errors("Google Ads")
    async def get_keyword_performance(
        self,
        connection_id: str,
        customer_id: str,
        date_range_start: str,
        date_range_end: str,
        campaign_id: str | None = None,
        limit: int = 50,
    ) -> dict:
        refresh_token, _dev_tok, _cid, _csec = await self._get_client_inputs(connection_id)

        def _run():
            client = self._build_client(refresh_token, _dev_tok, _cid, _csec)
            ga_service = client.get_service("GoogleAdsService")

            campaign_filter = f"AND campaign.id = {campaign_id}" if campaign_id else ""
            query = f"""
                SELECT
                    ad_group_criterion.keyword.text,
                    ad_group_criterion.keyword.match_type,
                    campaign.name,
                    ad_group.name,
                    metrics.impressions,
                    metrics.clicks,
                    metrics.cost_micros,
                    metrics.conversions,
                    metrics.average_cpc,
                    ad_group_criterion.quality_info.quality_score
                FROM keyword_view
                WHERE segments.date BETWEEN '{date_range_start}' AND '{date_range_end}'
                  AND campaign.status = 'ENABLED'
                  {campaign_filter}
                ORDER BY metrics.clicks DESC
                LIMIT {limit}
            """

            keywords = []
            stream = ga_service.search_stream(customer_id=customer_id.replace("-", ""), query=query)
            for batch in stream:
                for row in batch.results:
                    keywords.append(
                        {
                            "keyword_text": row.ad_group_criterion.keyword.text,
                            "match_type": str(row.ad_group_criterion.keyword.match_type.name),
                            "campaign_name": row.campaign.name,
                            "ad_group_name": row.ad_group.name,
                            "impressions": int(row.metrics.impressions),
                            "clicks": int(row.metrics.clicks),
                            "cost": row.metrics.cost_micros / 1_000_000,
                            "conversions": int(row.metrics.conversions),
                            "quality_score": row.ad_group_criterion.quality_info.quality_score,
                        }
                    )

            return {"keywords": keywords}

        return await self.run_sync(_run)

    @friendly_errors("Google Ads")
    async def audit_tracking_setup(self, connection_id: str, customer_id: str) -> dict:
        """Audit Google Ads tracking setup: auto-tagging, attribution, conversion health."""
        refresh_token, _dev_tok, _cid, _csec = await self._get_client_inputs(connection_id)

        def _run():
            client = self._build_client(refresh_token, _dev_tok, _cid, _csec)
            ga_service = client.get_service("GoogleAdsService")

            query = """
                SELECT
                    customer.auto_tagging_enabled,
                    customer.attribution_lookup_window_days
                FROM customer
            """
            auto_tagging = False

            stream = ga_service.search_stream(customer_id=customer_id.replace("-", ""), query=query)
            for batch in stream:
                for row in batch.results:
                    auto_tagging = row.customer.auto_tagging_enabled
                    break

            return auto_tagging

        auto_tagging = await self.run_sync(_run)
        issues = []

        if not auto_tagging:
            issues.append(
                {
                    "severity": "critical",
                    "description": "Auto-tagging is disabled — campaign data won't flow to GA4",
                    "recommendation": "Enable auto-tagging in Google Ads Account Settings",
                }
            )

        conversion_data = await self.get_conversion_actions(connection_id, customer_id)
        active_conversions = [ca for ca in conversion_data["conversion_actions"] if ca["status"] == "ENABLED"]

        if not active_conversions:
            issues.append(
                {
                    "severity": "critical",
                    "description": "No active conversion actions configured",
                    "recommendation": "Set up at least one conversion action",
                }
            )

        score = max(0, 100 - len(issues) * 25)
        return {
            "auto_tagging_enabled": auto_tagging,
            "conversion_tracking_status": "active" if active_conversions else "inactive",
            "conversion_actions": active_conversions[:5],
            "issues": issues,
            "score": score,
        }

    # ------------------------------------------------------------------
    # Layer 1 (extended): Ad group performance
    # ------------------------------------------------------------------

    @friendly_errors("Google Ads")
    async def get_ad_group_performance(
        self,
        connection_id: str,
        customer_id: str,
        date_range_start: str,
        date_range_end: str,
        campaign_id: str | None = None,
        limit: int = 50,
    ) -> dict:
        """Returns ad-group level performance metrics, optionally filtered by campaign."""
        refresh_token, _dev_tok, _cid, _csec = await self._get_client_inputs(connection_id)

        def _run():
            client = self._build_client(refresh_token, _dev_tok, _cid, _csec)
            ga_service = client.get_service("GoogleAdsService")

            campaign_filter = f"AND campaign.id = {campaign_id}" if campaign_id else ""
            query = f"""
                SELECT
                    ad_group.id,
                    ad_group.name,
                    ad_group.status,
                    campaign.id,
                    campaign.name,
                    metrics.impressions,
                    metrics.clicks,
                    metrics.cost_micros,
                    metrics.conversions,
                    metrics.ctr,
                    metrics.average_cpc,
                    metrics.conversion_rate
                FROM ad_group
                WHERE segments.date BETWEEN '{date_range_start}' AND '{date_range_end}'
                  AND ad_group.status != 'REMOVED'
                  {campaign_filter}
                ORDER BY metrics.clicks DESC
                LIMIT {limit}
            """

            ad_groups = []
            stream = ga_service.search_stream(customer_id=customer_id.replace("-", ""), query=query)
            for batch in stream:
                for row in batch.results:
                    ad_groups.append(
                        {
                            "ad_group_id": str(row.ad_group.id),
                            "ad_group_name": row.ad_group.name,
                            "status": str(row.ad_group.status.name),
                            "campaign_id": str(row.campaign.id),
                            "campaign_name": row.campaign.name,
                            "impressions": int(row.metrics.impressions),
                            "clicks": int(row.metrics.clicks),
                            "cost": round(row.metrics.cost_micros / 1_000_000, 2),
                            "conversions": round(float(row.metrics.conversions), 2),
                            "ctr": round(float(row.metrics.ctr) * 100, 4),
                            "average_cpc": round(row.metrics.average_cpc / 1_000_000, 2),
                            "conversion_rate": round(float(row.metrics.conversion_rate) * 100, 4),
                        }
                    )

            return {"ad_groups": ad_groups, "total": len(ad_groups)}

        return await self.run_sync(_run)

    # ------------------------------------------------------------------
    # Layer 2: Audit intelligence
    # ------------------------------------------------------------------

    @friendly_errors("Google Ads")
    async def audit_budget_utilization(
        self,
        connection_id: str,
        customer_id: str,
        date_range_start: str,
        date_range_end: str,
    ) -> dict:
        """
        Audits campaign budget utilisation.
        Flags campaigns that are limited by budget, overspending, or have zero spend.
        Returns a scored health report with per-campaign spend vs budget details.
        """
        refresh_token, _dev_tok, _cid, _csec = await self._get_client_inputs(connection_id)

        def _run():
            client = self._build_client(refresh_token, _dev_tok, _cid, _csec)
            ga_service = client.get_service("GoogleAdsService")

            query = f"""
                SELECT
                    campaign.id,
                    campaign.name,
                    campaign.status,
                    campaign.serving_status,
                    campaign_budget.amount_micros,
                    campaign_budget.has_recommended_budget,
                    campaign_budget.recommended_budget_amount_micros,
                    metrics.cost_micros,
                    metrics.impressions
                FROM campaign
                WHERE segments.date BETWEEN '{date_range_start}' AND '{date_range_end}'
                  AND campaign.status = 'ENABLED'
                ORDER BY metrics.cost_micros DESC
            """

            campaigns = []
            issues = []
            stream = ga_service.search_stream(customer_id=customer_id.replace("-", ""), query=query)
            for batch in stream:
                for row in batch.results:
                    budget = row.campaign_budget.amount_micros / 1_000_000
                    spend = row.metrics.cost_micros / 1_000_000
                    utilization_pct = round(spend / budget * 100, 1) if budget > 0 else None
                    recommended = row.campaign_budget.recommended_budget_amount_micros
                    rec_budget = recommended / 1_000_000 if recommended else None

                    campaign_entry = {
                        "campaign_id": str(row.campaign.id),
                        "campaign_name": row.campaign.name,
                        "serving_status": str(row.campaign.serving_status.name),
                        "daily_budget": budget,
                        "spend_in_period": round(spend, 2),
                        "budget_utilization_pct": utilization_pct,
                        "has_recommended_budget": row.campaign_budget.has_recommended_budget,
                        "recommended_budget": rec_budget,
                        "impressions": int(row.metrics.impressions),
                    }
                    campaigns.append(campaign_entry)

                    if str(row.campaign.serving_status.name) == "BUDGET_CONSTRAINED":
                        issues.append(
                            {
                                "severity": "warning",
                                "campaign": row.campaign.name,
                                "issue": "Campaign is budget-constrained — ads are not showing for all eligible queries",
                                "recommendation": f"Increase daily budget{f' to the recommended ${rec_budget:.2f}' if rec_budget else ''}",
                            }
                        )
                    if utilization_pct is not None and utilization_pct > 100:
                        issues.append(
                            {
                                "severity": "warning",
                                "campaign": row.campaign.name,
                                "issue": f"Campaign overspent budget by {utilization_pct - 100:.1f}% in this period",
                                "recommendation": "Review budget settings or add a shared budget cap",
                            }
                        )
                    if spend == 0 and int(row.metrics.impressions) == 0:
                        issues.append(
                            {
                                "severity": "info",
                                "campaign": row.campaign.name,
                                "issue": "Enabled campaign had zero spend and zero impressions in this period",
                                "recommendation": "Check ad approvals, targeting settings, and bid strategy",
                            }
                        )

            constrained = sum(1 for c in campaigns if c["serving_status"] == "BUDGET_CONSTRAINED")
            score = max(0, 100 - len([i for i in issues if i["severity"] == "warning"]) * 15)

            return {
                "score": score,
                "total_campaigns_analysed": len(campaigns),
                "budget_constrained_count": constrained,
                "campaigns": campaigns,
                "issues": issues,
            }

        return await self.run_sync(_run)

    @friendly_errors("Google Ads")
    async def audit_quality_scores(
        self,
        connection_id: str,
        customer_id: str,
        campaign_id: str | None = None,
        limit: int = 200,
    ) -> dict:
        """
        Audits keyword Quality Scores across the account (or a single campaign).
        Returns distribution breakdown, keywords needing attention, and an overall score.
        Quality Score tiers: Poor (1-4), Average (5-6), Good (7-10).
        """
        refresh_token, _dev_tok, _cid, _csec = await self._get_client_inputs(connection_id)

        def _run():
            client = self._build_client(refresh_token, _dev_tok, _cid, _csec)
            ga_service = client.get_service("GoogleAdsService")

            campaign_filter = f"AND campaign.id = {campaign_id}" if campaign_id else ""
            query = f"""
                SELECT
                    ad_group_criterion.keyword.text,
                    ad_group_criterion.keyword.match_type,
                    ad_group_criterion.quality_info.quality_score,
                    ad_group_criterion.quality_info.creative_quality_score,
                    ad_group_criterion.quality_info.post_click_quality_score,
                    ad_group_criterion.quality_info.search_predicted_ctr,
                    campaign.name,
                    ad_group.name,
                    metrics.impressions,
                    metrics.clicks,
                    metrics.cost_micros
                FROM keyword_view
                WHERE campaign.status = 'ENABLED'
                  AND ad_group.status = 'ENABLED'
                  AND ad_group_criterion.status = 'ENABLED'
                  {campaign_filter}
                ORDER BY metrics.impressions DESC
                LIMIT {limit}
            """

            keywords = []
            poor, average, good, unknown = 0, 0, 0, 0

            stream = ga_service.search_stream(customer_id=customer_id.replace("-", ""), query=query)
            for batch in stream:
                for row in batch.results:
                    qs = row.ad_group_criterion.quality_info.quality_score
                    qi = row.ad_group_criterion.quality_info

                    if qs == 0:
                        unknown += 1
                        tier = "unknown"
                    elif qs <= 4:
                        poor += 1
                        tier = "poor"
                    elif qs <= 6:
                        average += 1
                        tier = "average"
                    else:
                        good += 1
                        tier = "good"

                    keywords.append(
                        {
                            "keyword": row.ad_group_criterion.keyword.text,
                            "match_type": str(row.ad_group_criterion.keyword.match_type.name),
                            "campaign_name": row.campaign.name,
                            "ad_group_name": row.ad_group.name,
                            "quality_score": qs,
                            "quality_tier": tier,
                            "creative_quality": str(qi.creative_quality_score.name)
                            if qi.creative_quality_score
                            else None,
                            "landing_page_quality": str(qi.post_click_quality_score.name)
                            if qi.post_click_quality_score
                            else None,
                            "expected_ctr": str(qi.search_predicted_ctr.name)
                            if qi.search_predicted_ctr
                            else None,
                            "impressions": int(row.metrics.impressions),
                            "clicks": int(row.metrics.clicks),
                            "cost": round(row.metrics.cost_micros / 1_000_000, 2),
                        }
                    )

            total_scored = poor + average + good
            avg_qs = (
                round(sum(k["quality_score"] for k in keywords if k["quality_score"] > 0) / total_scored, 1)
                if total_scored > 0
                else None
            )

            issues = []
            if total_scored > 0 and poor / total_scored > 0.3:
                issues.append(
                    {
                        "severity": "warning",
                        "issue": f"{poor} keywords ({round(poor / total_scored * 100)}%) have poor Quality Scores (1-4)",
                        "recommendation": "Improve ad relevance, landing page experience, and expected CTR for low-QS keywords",
                    }
                )
            if avg_qs is not None and avg_qs < 5:
                issues.append(
                    {
                        "severity": "critical",
                        "issue": f"Average Quality Score is {avg_qs} — well below the 7+ target",
                        "recommendation": "Restructure ad groups for tighter keyword-to-ad relevance (SKAG or themed groups)",
                    }
                )

            # Surface the worst 10 keywords with impressions > 0
            poor_keywords = sorted(
                [
                    k
                    for k in keywords
                    if k["quality_score"] > 0 and k["quality_score"] <= 4 and k["impressions"] > 0
                ],
                key=lambda x: x["impressions"],
                reverse=True,
            )[:10]

            score = max(0, min(100, int((avg_qs / 10 * 100) if avg_qs else 50)))

            return {
                "score": score,
                "average_quality_score": avg_qs,
                "total_keywords_analysed": len(keywords),
                "distribution": {"poor": poor, "average": average, "good": good, "unknown": unknown},
                "issues": issues,
                "worst_keywords": poor_keywords,
            }

        return await self.run_sync(_run)

    # ------------------------------------------------------------------
    # Layer 3: Write operations
    # ------------------------------------------------------------------

    @friendly_errors("Google Ads")
    async def create_campaign(
        self,
        connection_id: str,
        customer_id: str,
        campaign_name: str,
        advertising_channel_type: str,
        daily_budget_micros: int,
        start_date: str,
        end_date: str | None = None,
        bidding_strategy_type: str = "MAXIMIZE_CLICKS",
    ) -> dict:
        """
        Creates a new Google Ads campaign with a linked campaign budget.

        advertising_channel_type: SEARCH | DISPLAY | SHOPPING | VIDEO | PERFORMANCE_MAX
        bidding_strategy_type: MAXIMIZE_CLICKS | MAXIMIZE_CONVERSIONS | TARGET_CPA | MANUAL_CPC
        start_date / end_date: YYYY-MM-DD format
        daily_budget_micros: daily budget in micros (e.g. 10_000_000 = $10.00)
        """
        refresh_token, _dev_tok, _cid, _csec = await self._get_client_inputs(connection_id)

        def _run():
            client = self._build_client(refresh_token, _dev_tok, _cid, _csec)

            cid = customer_id.replace("-", "")

            # Step 1: Create the campaign budget
            budget_service = client.get_service("CampaignBudgetService")
            budget_op = client.get_type("CampaignBudgetOperation")
            budget = budget_op.create
            budget.name = f"{campaign_name} Budget"
            budget.amount_micros = daily_budget_micros
            budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD

            budget_response = budget_service.mutate_campaign_budgets(customer_id=cid, operations=[budget_op])
            budget_resource = budget_response.results[0].resource_name

            # Step 2: Create the campaign
            campaign_service = client.get_service("CampaignService")
            campaign_op = client.get_type("CampaignOperation")
            campaign = campaign_op.create
            campaign.name = campaign_name
            campaign.advertising_channel_type = getattr(
                client.enums.AdvertisingChannelTypeEnum, advertising_channel_type
            )
            campaign.status = client.enums.CampaignStatusEnum.PAUSED  # Start paused — safer default
            campaign.campaign_budget = budget_resource
            campaign.start_date = start_date
            if end_date:
                campaign.end_date = end_date

            # Bidding strategy
            if bidding_strategy_type == "MAXIMIZE_CLICKS":
                campaign.maximize_clicks.CopyFrom(client.get_type("MaximizeClicks"))
            elif bidding_strategy_type == "MAXIMIZE_CONVERSIONS":
                campaign.maximize_conversions.CopyFrom(client.get_type("MaximizeConversions"))
            elif bidding_strategy_type == "MANUAL_CPC":
                campaign.manual_cpc.enhanced_cpc_enabled = True
            # TARGET_CPA requires a target_cpa_micros — fall back to MAXIMIZE_CONVERSIONS
            else:
                campaign.maximize_conversions.CopyFrom(client.get_type("MaximizeConversions"))

            campaign_response = campaign_service.mutate_campaigns(customer_id=cid, operations=[campaign_op])
            campaign_resource = campaign_response.results[0].resource_name

            return {
                "campaign_resource_name": campaign_resource,
                "campaign_name": campaign_name,
                "budget_resource_name": budget_resource,
                "daily_budget_usd": round(daily_budget_micros / 1_000_000, 2),
                "status": "PAUSED",
                "start_date": start_date,
                "end_date": end_date,
                "note": "Campaign created in PAUSED status. Enable it when ready via update_campaign_status.",
            }

        return await self.run_sync(_run)

    @friendly_errors("Google Ads")
    async def update_campaign_status(
        self,
        connection_id: str,
        customer_id: str,
        campaign_id: str,
        status: str,
    ) -> dict:
        """
        Updates a campaign's status.
        status: ENABLED | PAUSED
        """
        if status not in ("ENABLED", "PAUSED"):
            return {"error": True, "message": "status must be 'ENABLED' or 'PAUSED'"}

        refresh_token, _dev_tok, _cid, _csec = await self._get_client_inputs(connection_id)

        def _run():
            client = self._build_client(refresh_token, _dev_tok, _cid, _csec)
            cid = customer_id.replace("-", "")

            campaign_service = client.get_service("CampaignService")
            campaign_op = client.get_type("CampaignOperation")
            campaign = campaign_op.update
            campaign.resource_name = campaign_service.campaign_path(cid, campaign_id)
            campaign.status = getattr(client.enums.CampaignStatusEnum, status)
            campaign_op.update_mask.paths.append("status")

            campaign_service.mutate_campaigns(customer_id=cid, operations=[campaign_op])

            return {
                "campaign_id": campaign_id,
                "customer_id": customer_id,
                "new_status": status,
                "updated": True,
            }

        return await self.run_sync(_run)

    @friendly_errors("Google Ads")
    async def update_campaign_budget(
        self,
        connection_id: str,
        customer_id: str,
        campaign_id: str,
        new_daily_budget_micros: int,
    ) -> dict:
        """
        Updates the daily budget for a campaign's linked campaign budget.
        new_daily_budget_micros: new daily budget in micros (e.g. 20_000_000 = $20.00)
        """
        refresh_token, _dev_tok, _cid, _csec = await self._get_client_inputs(connection_id)

        def _run():
            client = self._build_client(refresh_token, _dev_tok, _cid, _csec)
            cid = customer_id.replace("-", "")
            ga_service = client.get_service("GoogleAdsService")

            # First: find the campaign budget resource name for this campaign
            query = f"""
                SELECT campaign.campaign_budget
                FROM campaign
                WHERE campaign.id = {campaign_id}
            """
            budget_resource = None
            stream = ga_service.search_stream(customer_id=cid, query=query)
            for batch in stream:
                for row in batch.results:
                    budget_resource = row.campaign.campaign_budget
                    break

            if not budget_resource:
                return {
                    "error": True,
                    "message": f"Could not find campaign budget for campaign_id={campaign_id}",
                }

            budget_service = client.get_service("CampaignBudgetService")
            budget_op = client.get_type("CampaignBudgetOperation")
            budget = budget_op.update
            budget.resource_name = budget_resource
            budget.amount_micros = new_daily_budget_micros
            budget_op.update_mask.paths.append("amount_micros")

            budget_service.mutate_campaign_budgets(customer_id=cid, operations=[budget_op])

            return {
                "campaign_id": campaign_id,
                "customer_id": customer_id,
                "budget_resource_name": budget_resource,
                "new_daily_budget_usd": round(new_daily_budget_micros / 1_000_000, 2),
                "updated": True,
            }

        return await self.run_sync(_run)
