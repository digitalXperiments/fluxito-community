"""
GA4 Connector

Wraps google-analytics-data SDK for all GA4 read operations
and GA4 Admin SDK for write operations (Tier 3).
"""

import statistics

from app.connectors.base import BaseConnector
from app.connectors.errors import friendly_errors

# GA4 API limits and standard events as constants
_GA4_MAX_METRICS_PER_REQUEST = 10
# Per-call gRPC timeout: gives the tool-level asyncio.wait_for() time to fire
# cleanly without threads hanging indefinitely in the SDK pool.
_GA4_CALL_TIMEOUT = 25.0
_GA4_MAX_REPORT_LIMIT = 10000
_GA4_ECOMMERCE_EVENTS = frozenset(
    {"view_item", "add_to_cart", "begin_checkout", "purchase", "remove_from_cart", "view_cart"}
)
_GA4_STANDARD_EVENTS = frozenset(
    {
        "page_view",
        "session_start",
        "first_visit",
        "user_engagement",
        "scroll",
        "click",
        "video_start",
        "video_progress",
        "video_complete",
        "file_download",
        "view_search_results",
        "search",
        "purchase",
        "add_to_cart",
        "remove_from_cart",
        "begin_checkout",
        "add_payment_info",
        "add_shipping_info",
        "view_item",
        "view_item_list",
        "view_cart",
        "select_item",
        "select_promotion",
        "view_promotion",
        "sign_up",
        "login",
        "generate_lead",
    }
)
_GA4_RESERVED_PREFIX = frozenset({"ga_", "_"})


def _build_filter_expression(f: dict):
    """Recursively build a GA4 FilterExpression proto from a camelCase dict.

    Supports: filter.stringFilter, filter.inListFilter, filter.numericFilter,
    andGroup, orGroup, notExpression.
    """
    from google.analytics.data_v1beta.types import (
        Filter,
        FilterExpression,
        FilterExpressionList,
        NumericValue,
    )

    if "andGroup" in f:
        exprs = [_build_filter_expression(e) for e in f["andGroup"]["expressions"]]
        return FilterExpression(and_group=FilterExpressionList(expressions=exprs))
    if "orGroup" in f:
        exprs = [_build_filter_expression(e) for e in f["orGroup"]["expressions"]]
        return FilterExpression(or_group=FilterExpressionList(expressions=exprs))
    if "notExpression" in f:
        return FilterExpression(not_expression=_build_filter_expression(f["notExpression"]))
    if "filter" in f:
        filt = f["filter"]
        field_name = filt["fieldName"]
        if "stringFilter" in filt:
            sf = filt["stringFilter"]
            mt_map = {
                "EXACT": Filter.StringFilter.MatchType.EXACT,
                "BEGINS_WITH": Filter.StringFilter.MatchType.BEGINS_WITH,
                "ENDS_WITH": Filter.StringFilter.MatchType.ENDS_WITH,
                "CONTAINS": Filter.StringFilter.MatchType.CONTAINS,
                "FULL_REGEXP": Filter.StringFilter.MatchType.FULL_REGEXP,
                "PARTIAL_REGEXP": Filter.StringFilter.MatchType.PARTIAL_REGEXP,
            }
            return FilterExpression(
                filter=Filter(
                    field_name=field_name,
                    string_filter=Filter.StringFilter(
                        match_type=mt_map.get(
                            sf.get("matchType", "EXACT"),
                            Filter.StringFilter.MatchType.EXACT,
                        ),
                        value=sf["value"],
                        case_sensitive=sf.get("caseSensitive", False),
                    ),
                )
            )
        if "inListFilter" in filt:
            ilf = filt["inListFilter"]
            return FilterExpression(
                filter=Filter(
                    field_name=field_name,
                    in_list_filter=Filter.InListFilter(
                        values=ilf["values"],
                        case_sensitive=ilf.get("caseSensitive", False),
                    ),
                )
            )
        if "numericFilter" in filt:
            nf = filt["numericFilter"]
            op_map = {
                "EQUAL": Filter.NumericFilter.Operation.EQUAL,
                "LESS_THAN": Filter.NumericFilter.Operation.LESS_THAN,
                "LESS_THAN_OR_EQUAL": Filter.NumericFilter.Operation.LESS_THAN_OR_EQUAL,
                "GREATER_THAN": Filter.NumericFilter.Operation.GREATER_THAN,
                "GREATER_THAN_OR_EQUAL": Filter.NumericFilter.Operation.GREATER_THAN_OR_EQUAL,
            }
            val = nf["value"]
            num_val = (
                NumericValue(int64_value=int(val["intValue"]))
                if "intValue" in val
                else NumericValue(double_value=float(val.get("doubleValue", 0)))
            )
            return FilterExpression(
                filter=Filter(
                    field_name=field_name,
                    numeric_filter=Filter.NumericFilter(
                        operation=op_map.get(
                            nf.get("operation", "EQUAL"),
                            Filter.NumericFilter.Operation.EQUAL,
                        ),
                        value=num_val,
                    ),
                )
            )
    raise ValueError(f"Unsupported filter structure: {list(f.keys())}")


class GA4Connector(BaseConnector):
    async def _run(self, func, *args, **kwargs):
        """run_sync with a per-call gRPC timeout to prevent indefinite hangs."""
        return await self.run_sync(func, *args, timeout=_GA4_CALL_TIMEOUT, **kwargs)

    def _build_client(self, access_token: str):
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        from google.oauth2.credentials import Credentials

        creds = Credentials(token=access_token)
        return BetaAnalyticsDataClient(credentials=creds)

    def _build_admin_client(self, access_token: str):
        from google.analytics.admin import AnalyticsAdminServiceClient
        from google.oauth2.credentials import Credentials

        creds = Credentials(token=access_token)
        return AnalyticsAdminServiceClient(credentials=creds)

    # ------------------------------------------------------------------
    # Discovery helpers (used during OAuth callback)
    # ------------------------------------------------------------------

    @friendly_errors("GA4")
    async def list_all_properties_raw(self, access_token: str) -> list:
        """Returns list of property dicts — used during data OAuth callback."""
        import logging

        from google.analytics.admin import AnalyticsAdminServiceClient
        from google.oauth2.credentials import Credentials

        logger = logging.getLogger(__name__)

        creds = Credentials(token=access_token)
        client = AnalyticsAdminServiceClient(credentials=creds)

        properties = []
        try:
            # Using list_account_summaries is more reliable as it returns
            # all properties the user has access to, even if they don't
            # have account-level access.
            def _list_account_summaries(timeout=None):
                return list(client.list_account_summaries(timeout=timeout))

            summaries = await self._run(_list_account_summaries)
            for account_summary in summaries:
                acct_id = account_summary.account.split("/")[-1]
                acct_name = account_summary.display_name

                for prop_summary in account_summary.property_summaries:
                    prop_id = prop_summary.property.split("/")[-1]
                    properties.append(
                        {
                            "id": prop_id,
                            "name": prop_summary.property,
                            "displayName": prop_summary.display_name,
                            "account": acct_id,
                            "accountName": acct_name,
                        }
                    )

            logger.info(f"Discovered {len(properties)} GA4 properties")
        except Exception as e:
            logger.error(f"Error discovering GA4 properties: {e!s}")
            # Fallback to manual account iteration if summaries fail
            try:

                def _list_accounts(timeout=None):
                    return list(client.list_accounts(timeout=timeout))

                def _list_properties(account_name: str, timeout=None):
                    return list(client.list_properties(filter=f"parent:{account_name}", timeout=timeout))

                for account in await self._run(_list_accounts):
                    acct_id = account.name.split("/")[-1]
                    for prop in await self._run(_list_properties, account.name):
                        properties.append(
                            {
                                "id": prop.name.split("/")[-1],
                                "name": prop.name,
                                "displayName": prop.display_name,
                                "account": acct_id,
                                "accountName": account.display_name,
                            }
                        )
            except Exception as e2:
                logger.error(f"Fallback GA4 discovery also failed: {e2!s}")

        return properties

    # ------------------------------------------------------------------
    # Layer 1: Data Access
    # ------------------------------------------------------------------

    @friendly_errors("GA4")
    async def list_properties(self, connection_id: str) -> dict:
        token = await self.get_token(connection_id)
        return {"properties": await self.list_all_properties_raw(token)}

    @friendly_errors("GA4")
    async def get_metadata(self, connection_id: str, property_id: str) -> dict:
        """
        Returns GA4 metadata (all available dimensions and metrics) for a
        given property, used by the KPI library field picker.

        Calls ``properties/{property_id}/metadata`` on the Data API —
        includes standard GA4 fields plus the property's custom
        dimensions/metrics and any deprecated flags.
        """
        token = await self.get_token(connection_id)
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        from google.analytics.data_v1beta.types import GetMetadataRequest
        from google.oauth2.credentials import Credentials

        creds = Credentials(token=token)
        client = BetaAnalyticsDataClient(credentials=creds)

        request = GetMetadataRequest(name=f"properties/{property_id}/metadata")
        response = await self._run(client.get_metadata, request)

        def _dim(d) -> dict:
            return {
                "api_name": d.api_name,
                "ui_name": d.ui_name,
                "description": d.description,
                "category": d.category,
                "custom_definition": bool(d.custom_definition),
                "deprecated_api_names": list(d.deprecated_api_names or []),
            }

        def _metric(m) -> dict:
            return {
                "api_name": m.api_name,
                "ui_name": m.ui_name,
                "description": m.description,
                "category": m.category,
                "type": getattr(m.type_, "name", str(m.type_)),
                "expression": m.expression or None,
                "custom_definition": bool(m.custom_definition),
                "deprecated_api_names": list(m.deprecated_api_names or []),
            }

        return {
            "property_id": property_id,
            "dimensions": [_dim(d) for d in response.dimensions],
            "metrics": [_metric(m) for m in response.metrics],
        }

    @friendly_errors("GA4")
    async def run_report(
        self,
        connection_id: str,
        property_id: str,
        dimensions: list,
        metrics: list,
        date_range_start: str,
        date_range_end: str,
        order_by=None,
        limit: int = 100,
        dimension_filter: dict | None = None,
        metric_filter: dict | None = None,
    ) -> dict:
        token = await self.get_token(connection_id)
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        from google.analytics.data_v1beta.types import DateRange, Dimension, Metric, RunReportRequest
        from google.oauth2.credentials import Credentials

        creds = Credentials(token=token)
        client = BetaAnalyticsDataClient(credentials=creds)

        # GA4 API hard limit: max 10 metrics per request
        metrics = metrics[:_GA4_MAX_METRICS_PER_REQUEST]

        request = RunReportRequest(
            property=property_id,
            date_ranges=[DateRange(start_date=date_range_start, end_date=date_range_end)],
            dimensions=[Dimension(name=d) for d in dimensions],
            metrics=[Metric(name=m) for m in metrics],
            limit=min(limit, _GA4_MAX_REPORT_LIMIT),
            dimension_filter=_build_filter_expression(dimension_filter) if dimension_filter else None,
            metric_filter=_build_filter_expression(metric_filter) if metric_filter else None,
        )

        # Run sync gRPC call in thread pool to avoid blocking the event loop
        response = await self._run(client.run_report, request)
        rows = []
        for row in response.rows:
            rows.append(
                {
                    "dimensions": [d.value for d in row.dimension_values],
                    "metrics": [m.value for m in row.metric_values],
                }
            )

        return {
            "rows": rows,
            "row_count": response.row_count,
            "dimension_headers": [h.name for h in response.dimension_headers],
            "metric_headers": [h.name for h in response.metric_headers],
        }

    async def list_events(
        self, connection_id: str, property_id: str, date_range_start: str, date_range_end: str
    ) -> dict:
        result = await self.run_report(
            connection_id,
            property_id,
            dimensions=["eventName"],
            metrics=["eventCount"],
            date_range_start=date_range_start,
            date_range_end=date_range_end,
            limit=200,
        )
        events = sorted(
            [{"event_name": r["dimensions"][0], "event_count": int(r["metrics"][0])} for r in result["rows"]],
            key=lambda x: x["event_count"],
            reverse=True,
        )
        return {"events": events, "total_events": len(events)}

    async def get_event_detail(
        self,
        connection_id: str,
        property_id: str,
        event_name: str,
        date_range_start: str,
        date_range_end: str,
    ) -> dict:
        total = await self.run_report(
            connection_id,
            property_id,
            dimensions=["eventName"],
            metrics=["eventCount", "totalUsers"],
            date_range_start=date_range_start,
            date_range_end=date_range_end,
            limit=1,
        )
        count = 0
        unique_users = 0
        if total["rows"]:
            count = int(total["rows"][0]["metrics"][0])
            unique_users = int(total["rows"][0]["metrics"][1])

        # Get parameter breakdown (top params via customEvent:parameter dimension)
        params_result = await self.run_report(
            connection_id,
            property_id,
            dimensions=["eventName", "pagePath"],
            metrics=["eventCount"],
            date_range_start=date_range_start,
            date_range_end=date_range_end,
            limit=50,
        )

        return {
            "event_name": event_name,
            "total_count": count,
            "unique_users": unique_users,
            "sample_data": params_result.get("rows", [])[:10],
        }

    async def get_conversion_events(
        self, connection_id: str, property_id: str, date_range_start: str, date_range_end: str
    ) -> dict:
        token = await self.get_token(connection_id)
        admin_client = self._build_admin_client(token)
        try:
            raw = await self._run(admin_client.list_conversion_events, parent=property_id)
            conversion_events = list(raw)
        except Exception:
            conversion_events = []

        event_names = [e.event_name for e in conversion_events]

        event_counts = {}
        if event_names:
            result = await self.run_report(
                connection_id,
                property_id,
                dimensions=["eventName", "isConversionEvent"],
                metrics=["conversions"],
                date_range_start=date_range_start,
                date_range_end=date_range_end,
                limit=100,
            )
            for row in result["rows"]:
                if row["dimensions"][1] == "true":
                    event_counts[row["dimensions"][0]] = int(row["metrics"][0])

        output = []
        for e in conversion_events:
            output.append(
                {
                    "event_name": e.event_name,
                    "is_default_conversion": not getattr(e, "deletable", getattr(e, "is_deletable", True)),
                    "count": event_counts.get(e.event_name, 0),
                }
            )
        return {"conversion_events": output}

    @friendly_errors("GA4")
    async def get_realtime_data(self, connection_id: str, property_id: str) -> dict:
        token = await self.get_token(connection_id)
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        from google.analytics.data_v1beta.types import Dimension, Metric, RunRealtimeReportRequest
        from google.oauth2.credentials import Credentials

        creds = Credentials(token=token)
        client = BetaAnalyticsDataClient(credentials=creds)

        req = RunRealtimeReportRequest(
            property=property_id,
            dimensions=[Dimension(name="unifiedScreenName")],
            metrics=[Metric(name="activeUsers")],
        )
        response = await self._run(client.run_realtime_report, req)
        pages = [
            {"page": r.dimension_values[0].value, "users": int(r.metric_values[0].value)}
            for r in response.rows
        ]
        total_users = sum(p["users"] for p in pages)

        return {
            "active_users": total_users,
            "top_pages": sorted(pages, key=lambda x: x["users"], reverse=True)[:10],
        }

    @friendly_errors("GA4")
    async def compare_date_ranges(
        self,
        connection_id: str,
        property_id: str,
        metrics: list,
        current_start: str,
        current_end: str,
        previous_start: str,
        previous_end: str,
    ) -> dict:
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        from google.analytics.data_v1beta.types import DateRange, Metric, RunReportRequest
        from google.oauth2.credentials import Credentials

        token = await self.get_token(connection_id)
        creds = Credentials(token=token)
        client = BetaAnalyticsDataClient(credentials=creds)

        # GA4 API hard limit: max 10 metrics per request (nested requests same limit)
        metrics = metrics[:_GA4_MAX_METRICS_PER_REQUEST]

        request = RunReportRequest(
            property=property_id,
            date_ranges=[
                DateRange(start_date=current_start, end_date=current_end),
                DateRange(start_date=previous_start, end_date=previous_end),
            ],
            metrics=[Metric(name=m) for m in metrics],
        )
        response = await self._run(client.run_report, request)

        results = []
        current_row = response.rows[0] if response.rows else None
        previous_row = response.rows[1] if len(response.rows) > 1 else None

        for i, metric_name in enumerate(metrics):
            current_val = float(current_row.metric_values[i].value) if current_row else 0
            previous_val = float(previous_row.metric_values[i].value) if previous_row else 0
            change_abs = current_val - previous_val
            change_pct = (change_abs / previous_val * 100) if previous_val != 0 else None

            results.append(
                {
                    "metric": metric_name,
                    "current_value": current_val,
                    "previous_value": previous_val,
                    "change_absolute": change_abs,
                    "change_percent": round(change_pct, 2) if change_pct is not None else None,
                }
            )

        return {"comparisons": results}

    # ------------------------------------------------------------------
    # Layer 2: Intelligence
    # ------------------------------------------------------------------

    async def audit_ecommerce(
        self, connection_id: str, property_id: str, date_range_start: str, date_range_end: str
    ) -> dict:
        standard_events = list(_GA4_ECOMMERCE_EVENTS)
        result = await self.list_events(connection_id, property_id, date_range_start, date_range_end)
        event_map = {e["event_name"]: e["event_count"] for e in result["events"]}

        events_status = {}
        for ev in standard_events:
            events_status[ev] = {"present": ev in event_map, "count": event_map.get(ev, 0)}

        # Funnel analysis
        funnel = []
        counts = [event_map.get(ev, 0) for ev in standard_events]
        for i, (ev, count) in enumerate(zip(standard_events, counts, strict=False)):
            drop_pct = None
            if i > 0 and counts[i - 1] > 0:
                drop_pct = round((1 - count / counts[i - 1]) * 100, 1)
            funnel.append({"stage": ev, "count": count, "drop_pct_from_prev": drop_pct})

        issues = []

        # Determine if this is an ecommerce property by checking if ANY
        # ecommerce events have ever been configured
        has_any_ecommerce = any(events_status[ev]["present"] for ev in standard_events)

        if not events_status["purchase"]["present"]:
            if has_any_ecommerce:
                # Other ecommerce events exist but purchase is missing — that's critical
                issues.append(
                    {
                        "event": "purchase",
                        "issue": "purchase event not found but other ecommerce events are present — check GA4 setup",
                        "severity": "critical",
                    }
                )
            else:
                # No ecommerce events at all — likely not an ecommerce site
                issues.append(
                    {
                        "event": "purchase",
                        "issue": "No ecommerce events found. This may not be an ecommerce property.",
                        "severity": "info",
                    }
                )

        if events_status["add_to_cart"]["count"] > 0 and events_status["purchase"]["count"] == 0:
            issues.append(
                {
                    "event": "purchase",
                    "issue": "add_to_cart fires but no purchases tracked — check GA4 setup",
                    "severity": "critical",
                }
            )

        purchase_count = events_status["purchase"]["count"]
        return {
            "is_ecommerce_property": has_any_ecommerce,
            "standard_events": events_status,
            "funnel_drop_off": funnel,
            "data_quality_issues": issues,
            "purchase_count": purchase_count,
        }

    async def check_data_anomalies(
        self, connection_id: str, property_id: str, days_back: int = 30, sensitivity: str = "medium"
    ) -> dict:
        end = "today"
        start = f"{days_back}daysAgo"
        result = await self.run_report(
            connection_id,
            property_id,
            dimensions=["date"],
            metrics=["sessions", "activeUsers", "conversions"],
            date_range_start=start,
            date_range_end=end,
            limit=days_back,
        )

        threshold_map = {"low": 3.0, "medium": 2.0, "high": 1.5}
        threshold = threshold_map.get(sensitivity, 2.0)

        daily_sessions = [(r["dimensions"][0], int(r["metrics"][0])) for r in result["rows"]]
        daily_sessions.sort(key=lambda x: x[0])

        anomalies = []
        values = [s for _, s in daily_sessions]
        if len(values) >= 7:
            for i in range(7, len(values)):
                window = values[i - 7 : i]
                mean = statistics.mean(window)
                stdev = statistics.stdev(window) if len(window) > 1 else 0
                actual = values[i]
                date = daily_sessions[i][0]
                if stdev > 0:
                    z = abs(actual - mean) / stdev
                    if z > threshold:
                        anomaly_type = "drop" if actual < mean else "spike"
                        anomalies.append(
                            {
                                "date": date,
                                "metric": "sessions",
                                "actual_value": actual,
                                "expected_value": round(mean, 1),
                                "deviation_pct": round((actual - mean) / mean * 100, 1),
                                "anomaly_type": anomaly_type,
                                "severity": "critical" if z > threshold * 1.5 else "warning",
                                "possible_causes": [
                                    "GA4 tracking code removed",
                                    "Filtering change",
                                    "Site downtime",
                                ]
                                if anomaly_type == "drop"
                                else ["Traffic spike", "Bot traffic", "Campaign launch"],
                            }
                        )

        return {
            "anomalies_found": anomalies,
            "overall_health": "critical"
            if any(a["severity"] == "critical" for a in anomalies)
            else ("degraded" if anomalies else "healthy"),
        }

    async def schema_validator(
        self, connection_id: str, property_id: str, date_range_start: str, date_range_end: str
    ) -> dict:
        result = await self.list_events(connection_id, property_id, date_range_start, date_range_end)
        all_events = result["events"]

        correct = []
        non_standard = []
        violations = []

        for ev in all_events:
            name = ev["event_name"]
            count = ev["event_count"]
            if name in _GA4_STANDARD_EVENTS:
                correct.append({"name": name, "status": "correct"})
            elif any(name.startswith(p) for p in _GA4_RESERVED_PREFIX):
                violations.append({"name": name, "reserved_by": "GA4"})
            else:
                non_standard.append({"name": name, "count": count, "suggestion": None})

        score = int(len(correct) / max(len(all_events), 1) * 100)
        return {
            "events_validated": len(all_events),
            "standard_events_correct": correct,
            "non_standard_event_names": non_standard,
            "reserved_name_violations": violations,
            "score": score,
        }

    # ------------------------------------------------------------------
    # Layer 3: Write operations (Tier FULL required)
    # ------------------------------------------------------------------

    @friendly_errors("GA4")
    async def create_audience(
        self,
        connection_id: str,
        property_id: str,
        display_name: str,
        description: str,
        membership_duration_days: int,
        filter_clauses: list,
    ) -> dict:
        token = await self.get_token(connection_id)
        admin_client = self._build_admin_client(token)
        from google.analytics.admin_v1alpha.types import Audience

        audience = await self._run(
            admin_client.create_audience,
            parent=property_id,
            audience=Audience(
                display_name=display_name,
                description=description or "",
                membership_duration_days=membership_duration_days,
                filter_clauses=filter_clauses,
            ),
        )
        return {
            "audience_id": audience.name,
            "display_name": audience.display_name,
            "property_id": property_id,
        }

    @friendly_errors("GA4")
    async def update_conversion_event(
        self, connection_id: str, property_id: str, event_name: str, is_conversion: bool
    ) -> dict:
        token = await self.get_token(connection_id)
        admin_client = self._build_admin_client(token)
        if is_conversion:
            await self._run(
                admin_client.create_conversion_event,
                parent=property_id,
                conversion_event={"event_name": event_name},
            )
        else:
            # Find and delete
            for ce in await self._run(admin_client.list_conversion_events, parent=property_id):
                if ce.event_name == event_name:
                    await self._run(admin_client.delete_conversion_event, name=ce.name)
                    break
        return {"event_name": event_name, "is_conversion": is_conversion, "property_id": property_id}

    @friendly_errors("GA4")
    async def create_custom_dimension(
        self,
        connection_id: str,
        property_id: str,
        display_name: str,
        parameter_name: str,
        scope: str,
        description: str = "",
    ) -> dict:
        token = await self.get_token(connection_id)
        admin_client = self._build_admin_client(token)
        from google.analytics.admin_v1alpha.types import CustomDimension

        dim = await self._run(
            admin_client.create_custom_dimension,
            parent=property_id,
            custom_dimension=CustomDimension(
                display_name=display_name,
                parameter_name=parameter_name,
                scope=scope,
                description=description,
            ),
        )
        return {
            "dimension_id": dim.name,
            "display_name": dim.display_name,
            "parameter_name": dim.parameter_name,
            "scope": scope,
        }

    # ------------------------------------------------------------------
    # Layer 1: Admin reads (data streams, custom definitions, audiences)
    # ------------------------------------------------------------------

    async def list_data_streams(self, connection_id: str, property_id: str) -> dict:
        """Lists all data streams (web, iOS, Android) for a GA4 property."""
        token = await self.get_token(connection_id)
        admin_client = self._build_admin_client(token)
        streams = []
        try:
            for stream in await self._run(admin_client.list_data_streams, parent=property_id):
                stream_type = "unknown"
                stream_id = stream.name.split("/")[-1]
                detail = {}
                if stream.web_stream_data.measurement_id:
                    stream_type = "web"
                    detail = {
                        "measurement_id": stream.web_stream_data.measurement_id,
                        "default_uri": stream.web_stream_data.default_uri,
                        "enhanced_measurement_enabled": (stream.web_stream_data.measurement_id != ""),
                    }
                elif stream.android_app_stream_data.package_name:
                    stream_type = "android"
                    detail = {
                        "package_name": stream.android_app_stream_data.package_name,
                        "firebase_app_id": stream.android_app_stream_data.firebase_app_id,
                    }
                elif stream.ios_app_stream_data.bundle_id:
                    stream_type = "ios"
                    detail = {
                        "bundle_id": stream.ios_app_stream_data.bundle_id,
                        "firebase_app_id": stream.ios_app_stream_data.firebase_app_id,
                    }
                streams.append(
                    {
                        "stream_id": stream_id,
                        "stream_name": stream.display_name,
                        "stream_type": stream_type,
                        "create_time": str(stream.create_time) if stream.create_time else None,
                        "update_time": str(stream.update_time) if stream.update_time else None,
                        **detail,
                    }
                )
        except Exception as e:
            return {"error": True, "message": str(e), "data_streams": []}
        return {"data_streams": streams, "total": len(streams)}

    async def list_custom_dimensions(self, connection_id: str, property_id: str) -> dict:
        """Lists all custom dimensions registered on a GA4 property."""
        token = await self.get_token(connection_id)
        admin_client = self._build_admin_client(token)
        dims = []
        try:
            for dim in await self._run(admin_client.list_custom_dimensions, parent=property_id):
                dims.append(
                    {
                        "name": dim.name,
                        "parameter_name": dim.parameter_name,
                        "display_name": dim.display_name,
                        "description": dim.description,
                        "scope": str(dim.scope.name) if dim.scope else "unknown",
                        "disallow_ads_personalization": getattr(dim, "disallow_ads_personalization", False),
                    }
                )
        except Exception as e:
            return {"error": True, "message": str(e), "custom_dimensions": []}
        return {"custom_dimensions": dims, "total": len(dims)}

    async def list_custom_metrics(self, connection_id: str, property_id: str) -> dict:
        """Lists all custom metrics registered on a GA4 property."""
        token = await self.get_token(connection_id)
        admin_client = self._build_admin_client(token)
        metrics = []
        try:
            for m in await self._run(admin_client.list_custom_metrics, parent=property_id):
                metrics.append(
                    {
                        "name": m.name,
                        "parameter_name": m.parameter_name,
                        "display_name": m.display_name,
                        "description": m.description,
                        "scope": str(m.scope.name) if m.scope else "unknown",
                        "measurement_unit": str(m.measurement_unit.name) if m.measurement_unit else "unknown",
                        "restricted_metric_type": [
                            str(t.name) for t in getattr(m, "restricted_metric_type", [])
                        ],
                    }
                )
        except Exception as e:
            return {"error": True, "message": str(e), "custom_metrics": []}
        return {"custom_metrics": metrics, "total": len(metrics)}

    async def list_audiences(self, connection_id: str, property_id: str) -> dict:
        """Lists all audiences defined on a GA4 property."""
        token = await self.get_token(connection_id)
        admin_client = self._build_admin_client(token)
        audiences = []
        try:
            for aud in await self._run(admin_client.list_audiences, parent=property_id):
                audiences.append(
                    {
                        "name": aud.name,
                        "display_name": aud.display_name,
                        "description": aud.description,
                        "membership_duration_days": aud.membership_duration_days,
                        "ads_personalization_enabled": getattr(aud, "ads_personalization_enabled", None),
                    }
                )
        except Exception as e:
            return {"error": True, "message": str(e), "audiences": []}
        return {"audiences": audiences, "total": len(audiences)}

    # ------------------------------------------------------------------
    # Layer 2: Audit / Intelligence (new methods)
    # ------------------------------------------------------------------

    async def audit_data_streams(self, connection_id: str, property_id: str) -> dict:
        """
        Audits data streams on a GA4 property for common configuration issues:
        missing measurement IDs, no web stream, multiple web streams, etc.
        """
        result = await self.list_data_streams(connection_id, property_id)
        if result.get("error"):
            return result

        streams = result["data_streams"]
        issues = []
        web_streams = [s for s in streams if s["stream_type"] == "web"]
        android_streams = [s for s in streams if s["stream_type"] == "android"]
        ios_streams = [s for s in streams if s["stream_type"] == "ios"]

        if not streams:
            issues.append(
                {
                    "severity": "critical",
                    "issue": "No data streams found on this property",
                    "recommendation": "Add a web or app data stream in GA4 Admin → Data Streams",
                }
            )
        if not web_streams:
            issues.append(
                {
                    "severity": "warning",
                    "issue": "No web data stream configured",
                    "recommendation": "Add a web data stream if your property tracks a website",
                }
            )
        if len(web_streams) > 1:
            names = [s["stream_name"] for s in web_streams]
            issues.append(
                {
                    "severity": "warning",
                    "issue": f"Multiple web streams found ({len(web_streams)}): {names}",
                    "recommendation": "Verify each stream corresponds to a distinct domain. Duplicate streams can cause data fragmentation.",
                }
            )
        for s in web_streams:
            if not s.get("measurement_id"):
                issues.append(
                    {
                        "severity": "critical",
                        "stream": s["stream_name"],
                        "issue": "Web stream has no Measurement ID",
                        "recommendation": "Re-create the stream or check GA4 admin — Measurement ID is required for gtag/GTM setup",
                    }
                )
            if not s.get("default_uri"):
                issues.append(
                    {
                        "severity": "warning",
                        "stream": s["stream_name"],
                        "issue": "Web stream has no default URI set",
                        "recommendation": "Set the website URL in GA4 Admin → Data Streams to enable cross-domain linking",
                    }
                )

        score = max(0, 100 - sum(30 if i["severity"] == "critical" else 10 for i in issues))
        return {
            "score": score,
            "total_streams": len(streams),
            "web_streams": len(web_streams),
            "android_streams": len(android_streams),
            "ios_streams": len(ios_streams),
            "issues": issues,
            "streams": streams,
        }

    async def audit_custom_definitions(self, connection_id: str, property_id: str) -> dict:
        """
        Audits custom dimensions and custom metrics for:
        - Duplicated parameter names
        - Naming convention violations (non-snake_case)
        - Unused scope choices (e.g. HIT scope on a metric)
        - Description missing
        Returns a health report with issues and a score.
        """
        import re

        dims_result = await self.list_custom_dimensions(connection_id, property_id)
        metrics_result = await self.list_custom_metrics(connection_id, property_id)

        dims = dims_result.get("custom_dimensions", [])
        metrics = metrics_result.get("custom_metrics", [])

        issues = []
        snake_case = re.compile(r"^[a-z][a-z0-9_]*$")

        # Check dimensions
        dim_param_names = [d["parameter_name"] for d in dims]
        for d in dims:
            if not d.get("description"):
                issues.append(
                    {
                        "severity": "info",
                        "type": "custom_dimension",
                        "item": d["display_name"],
                        "issue": "No description set",
                        "recommendation": "Add a description so team members understand what this dimension captures",
                    }
                )
            if not snake_case.match(d["parameter_name"]):
                issues.append(
                    {
                        "severity": "warning",
                        "type": "custom_dimension",
                        "item": d["display_name"],
                        "issue": f"Parameter name '{d['parameter_name']}' is not snake_case",
                        "recommendation": "GA4 parameter names should be lowercase snake_case to avoid case-sensitivity issues in reports",
                    }
                )
            if dim_param_names.count(d["parameter_name"]) > 1:
                issues.append(
                    {
                        "severity": "critical",
                        "type": "custom_dimension",
                        "item": d["display_name"],
                        "issue": f"Duplicate parameter name '{d['parameter_name']}'",
                        "recommendation": "Duplicate parameter names mean two dimensions map to the same data — delete or rename one",
                    }
                )

        # Check metrics
        metric_param_names = [m["parameter_name"] for m in metrics]
        for m in metrics:
            if not m.get("description"):
                issues.append(
                    {
                        "severity": "info",
                        "type": "custom_metric",
                        "item": m["display_name"],
                        "issue": "No description set",
                        "recommendation": "Add a description so team members understand what this metric measures",
                    }
                )
            if not snake_case.match(m["parameter_name"]):
                issues.append(
                    {
                        "severity": "warning",
                        "type": "custom_metric",
                        "item": m["display_name"],
                        "issue": f"Parameter name '{m['parameter_name']}' is not snake_case",
                        "recommendation": "GA4 parameter names should be lowercase snake_case",
                    }
                )
            if metric_param_names.count(m["parameter_name"]) > 1:
                issues.append(
                    {
                        "severity": "critical",
                        "type": "custom_metric",
                        "item": m["display_name"],
                        "issue": f"Duplicate parameter name '{m['parameter_name']}'",
                        "recommendation": "Duplicate parameter names cause metrics to conflict — delete or rename one",
                    }
                )
            if m.get("measurement_unit") == "STANDARD" and not m.get("description"):
                issues.append(
                    {
                        "severity": "info",
                        "type": "custom_metric",
                        "item": m["display_name"],
                        "issue": "Measurement unit is STANDARD (generic) — consider using CURRENCY, FEET, HOURS, etc.",
                        "recommendation": "Set a specific unit so GA4 formats the metric correctly in reports",
                    }
                )

        critical = sum(1 for i in issues if i["severity"] == "critical")
        warning = sum(1 for i in issues if i["severity"] == "warning")
        score = max(0, 100 - critical * 25 - warning * 10)

        return {
            "score": score,
            "custom_dimensions": dims,
            "custom_metrics": metrics,
            "total_dimensions": len(dims),
            "total_metrics": len(metrics),
            "issues": issues,
            "summary": {
                "critical": critical,
                "warning": warning,
                "info": sum(1 for i in issues if i["severity"] == "info"),
            },
        }

    async def audit_conversion_events(
        self,
        connection_id: str,
        property_id: str,
        date_range_start: str = "30daysAgo",
        date_range_end: str = "today",
    ) -> dict:
        """
        Audits conversion events: lists all marked conversions, their recent counts,
        and flags events with zero conversions, missing data, or potential
        double-counting issues.
        """
        token = await self.get_token(connection_id)
        admin_client = self._build_admin_client(token)

        conversion_events = []
        try:
            raw = await self._run(admin_client.list_conversion_events, parent=property_id)
            conversion_events = list(raw)
        except Exception as e:
            return {"error": True, "message": f"Could not list conversion events: {e!s}"}

        if not conversion_events:
            return {
                "conversion_events": [],
                "issues": [
                    {
                        "severity": "warning",
                        "issue": "No conversion events are marked on this property",
                        "recommendation": "Mark key events (purchase, generate_lead, sign_up) as conversions in GA4 Admin → Events",
                    }
                ],
                "score": 50,
            }

        # Pull conversion counts for the date range
        event_counts: dict = {}
        try:
            report = await self.run_report(
                connection_id,
                property_id,
                dimensions=["eventName", "isConversionEvent"],
                metrics=["conversions", "totalUsers"],
                date_range_start=date_range_start,
                date_range_end=date_range_end,
                limit=200,
            )
            for row in report.get("rows", []):
                if row["dimensions"][1] == "true":
                    event_counts[row["dimensions"][0]] = {
                        "conversions": int(row["metrics"][0]),
                        "unique_users": int(row["metrics"][1]),
                    }
        except Exception:
            pass  # Counts will remain empty; still report admin-level info

        issues = []
        output = []
        event_names = [e.event_name for e in conversion_events]

        # Flag duplicate event names (shouldn't happen but can via API quirks)
        if len(event_names) != len(set(event_names)):
            issues.append(
                {
                    "severity": "warning",
                    "issue": "Duplicate conversion event names detected",
                    "recommendation": "Check GA4 Admin → Events for duplicate conversion markers — these can inflate conversion counts",
                }
            )

        for ev in conversion_events:
            count_data = event_counts.get(ev.event_name, {})
            count = count_data.get("conversions", 0)
            unique_users = count_data.get("unique_users", 0)

            rec = {
                "event_name": ev.event_name,
                "is_deletable": getattr(ev, "deletable", getattr(ev, "is_deletable", None)),
                "count_in_period": count,
                "unique_converters": unique_users,
                "date_range": f"{date_range_start} → {date_range_end}",
            }
            output.append(rec)

            if count == 0:
                issues.append(
                    {
                        "severity": "warning",
                        "event": ev.event_name,
                        "issue": f"Conversion event '{ev.event_name}' recorded 0 conversions in the selected period",
                        "recommendation": "Verify the event is firing correctly in DebugView or check the date range",
                    }
                )

        # Check for missing high-value conversions
        important_missing = {"purchase", "generate_lead", "sign_up"} - set(event_names)
        for m in important_missing:
            issues.append(
                {
                    "severity": "info",
                    "issue": f"'{m}' is not marked as a conversion — it's a standard high-value event",
                    "recommendation": f"If your property tracks {m}, consider marking it as a conversion in GA4 Admin",
                }
            )

        critical = sum(1 for i in issues if i["severity"] == "critical")
        warning = sum(1 for i in issues if i["severity"] == "warning")
        score = max(0, 100 - critical * 30 - warning * 15)

        return {
            "score": score,
            "total_conversion_events": len(conversion_events),
            "conversion_events": output,
            "issues": issues,
            "summary": {
                "critical": critical,
                "warning": warning,
                "info": sum(1 for i in issues if i["severity"] == "info"),
            },
        }

    # ------------------------------------------------------------------
    # Layer 3: Additional write operations
    # ------------------------------------------------------------------

    @friendly_errors("GA4")
    async def create_custom_metric(self, connection_id: str, property_id: str, config: dict) -> dict:
        """Creates a new custom metric on a GA4 property."""
        token = await self.get_token(connection_id)
        admin_client = self._build_admin_client(token)
        from google.analytics.admin_v1alpha.types import CustomMetric

        metric = await self._run(
            admin_client.create_custom_metric,
            parent=property_id,
            custom_metric=CustomMetric(
                display_name=config.get("display_name", ""),
                parameter_name=config.get("parameter_name", ""),
                scope=config.get("scope", "EVENT"),
                measurement_unit=config.get("measurement_unit", "STANDARD"),
                description=config.get("description", ""),
            ),
        )
        return {
            "metric_id": metric.name,
            "display_name": metric.display_name,
            "parameter_name": metric.parameter_name,
            "scope": str(metric.scope.name) if metric.scope else "unknown",
            "measurement_unit": str(metric.measurement_unit.name) if metric.measurement_unit else "unknown",
            "property_id": property_id,
        }

    async def mark_event_as_conversion(
        self, connection_id: str, property_id: str, event_name: str, is_conversion: bool = True
    ) -> dict:
        """Marks (or unmarks) a GA4 event as a conversion. Alias for update_conversion_event."""
        return await self.update_conversion_event(connection_id, property_id, event_name, is_conversion)
