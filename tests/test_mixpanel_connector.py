"""
Unit tests for the MixpanelConnector.

Covers:
  1. Instantiation
  2. list_projects credential validation
  3. get_events_list
  4. get_event_properties
  5. query_events
  6. get_revenue
  7. check_taxonomy_health (clean + issues)
  8. check_event_volume_anomalies (no variance + spike)
  9. Error handling (401 → AuthenticationError via _classify_error)

All HTTP calls are mocked via unittest.mock.patch on httpx.AsyncClient.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.connectors.mixpanel import MixpanelConnector

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_httpx_response(json_data, status_code=200):
    """Build a mock httpx.Response with .status_code, .json(), and .text."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.text = str(json_data)
    return resp


def _patch_httpx(response):
    """Return a context-manager patch for httpx.AsyncClient returning *response*."""
    mock_client = AsyncMock()
    mock_client.get.return_value = response
    mock_client.post.return_value = response

    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = mock_client
    return patch("httpx.AsyncClient", return_value=mock_cm)


# ---------------------------------------------------------------------------
# 1. Instantiation
# ---------------------------------------------------------------------------


class TestInstantiation:
    def test_class_instantiation(self):
        connector = MixpanelConnector()
        assert connector is not None
        assert isinstance(connector, MixpanelConnector)


# ---------------------------------------------------------------------------
# 2. list_projects
# ---------------------------------------------------------------------------


class TestListProjects:
    @pytest.mark.anyio
    async def test_list_projects_valid_creds(self):
        """Successful /2.0/events/names/ returns {valid: True, ...}."""
        resp = _mock_httpx_response(["page_view", "purchase"])
        with _patch_httpx(resp):
            connector = MixpanelConnector()
            result = await connector.list_projects("api_secret", "service_token")

        assert result["valid"] is True
        assert "Mixpanel" in result["message"]

    @pytest.mark.anyio
    async def test_list_projects_invalid_creds(self):
        """A 401 response returns an error dict (not an exception)."""
        resp = _mock_httpx_response({"error": "unauthorized"}, status_code=401)
        with _patch_httpx(resp):
            connector = MixpanelConnector()
            result = await connector.list_projects("bad_key", "bad_token")

        assert result.get("error") is True
        assert result.get("status_code") == 401


# ---------------------------------------------------------------------------
# 3. get_events_list
# ---------------------------------------------------------------------------


class TestGetEventsList:
    @pytest.mark.anyio
    async def test_get_events_list_returns_events_and_total(self):
        """API returns a list of event names; connector normalises them."""
        resp = _mock_httpx_response(["page_view", "purchase", "add_to_cart"])
        with _patch_httpx(resp):
            connector = MixpanelConnector()
            result = await connector.get_events_list("secret", "token")

        assert result["total"] == 3
        assert result["events"][0]["event_type"] == "page_view"
        assert result["events"][1]["event_type"] == "purchase"
        assert result["events"][2]["event_type"] == "add_to_cart"

    @pytest.mark.anyio
    async def test_get_events_list_empty(self):
        """Empty API response returns zero events."""
        resp = _mock_httpx_response([])
        with _patch_httpx(resp):
            connector = MixpanelConnector()
            result = await connector.get_events_list("secret", "token")

        assert result["total"] == 0
        assert result["events"] == []


# ---------------------------------------------------------------------------
# 4. get_event_properties
# ---------------------------------------------------------------------------


class TestGetEventProperties:
    @pytest.mark.anyio
    async def test_get_event_properties_shape(self):
        """Properties endpoint returns {event_type, properties, total}."""
        resp = _mock_httpx_response({"properties": [{"name": "currency"}, {"name": "value"}]})
        with _patch_httpx(resp):
            connector = MixpanelConnector()
            result = await connector.get_event_properties("secret", "token", "purchase")

        assert result["event_type"] == "purchase"
        assert result["total"] == 2
        assert len(result["properties"]) == 2


# ---------------------------------------------------------------------------
# 5. query_events
# ---------------------------------------------------------------------------


class TestQueryEvents:
    @pytest.mark.anyio
    async def test_query_events_shape(self):
        """Segmentation endpoint returns {event_type, series, xaxis, ...}."""
        resp = _mock_httpx_response(
            {
                "data": {
                    "series": [["2024-01-01", 100], ["2024-01-02", 120]],
                    "xaxis": ["2024-01-01", "2024-01-02"],
                }
            }
        )
        with _patch_httpx(resp):
            connector = MixpanelConnector()
            result = await connector.query_events("secret", "token", "2024-01-01", "2024-01-07", "purchase")

        assert result["event_type"] == "purchase"
        assert result["start_date"] == "2024-01-01"
        assert result["end_date"] == "2024-01-07"
        assert "series" in result
        assert "xaxis" in result


# ---------------------------------------------------------------------------
# 6. get_revenue
# ---------------------------------------------------------------------------


class TestGetRevenue:
    @pytest.mark.anyio
    async def test_get_revenue_shape(self):
        """Revenue endpoint returns {metric: 'revenue', start_date, end_date, data}."""
        resp = _mock_httpx_response({"data": {"2024-01-01": 1500.00}})
        with _patch_httpx(resp):
            connector = MixpanelConnector()
            result = await connector.get_revenue("secret", "token", "2024-01-01", "2024-01-07")

        assert result["metric"] == "revenue"
        assert result["start_date"] == "2024-01-01"
        assert result["end_date"] == "2024-01-07"
        assert "data" in result

    @pytest.mark.anyio
    async def test_get_revenue_error_propagates(self):
        """A 401 from the revenue endpoint returns an error dict."""
        resp = _mock_httpx_response({"error": "unauthorized"}, status_code=401)
        with _patch_httpx(resp):
            connector = MixpanelConnector()
            result = await connector.get_revenue("bad_key", "bad_token", "2024-01-01", "2024-01-07")

        assert result.get("error") is True


# ---------------------------------------------------------------------------
# 7. check_taxonomy_health
# ---------------------------------------------------------------------------


class TestCheckTaxonomyHealth:
    @pytest.mark.anyio
    async def test_check_taxonomy_health_clean(self):
        """Well-named events produce health_score == 100 and zero issues."""
        connector = MixpanelConnector()

        # Mock get_events_list to return clean event names
        clean_events = {
            "events": [
                {"event_type": "page_view"},
                {"event_type": "purchase"},
                {"event_type": "add_to_cart"},
            ],
            "total": 3,
        }
        with patch.object(connector, "get_events_list", new_callable=AsyncMock, return_value=clean_events):
            result = await connector.check_taxonomy_health("secret", "token")

        assert result["event_count"] == 3
        assert result["health_score"] == 100
        assert result["issues"] == []

    @pytest.mark.anyio
    async def test_check_taxonomy_health_issues(self):
        """Events with spaces, uppercase, and duplicates produce issues and score < 100."""
        connector = MixpanelConnector()

        messy_events = {
            "events": [
                {"event_type": "Page View"},  # uppercase + space
                {"event_type": "Purchase"},  # uppercase
                {"event_type": "page view"},  # space + duplicate of "Page View"
            ],
            "total": 3,
        }
        with patch.object(connector, "get_events_list", new_callable=AsyncMock, return_value=messy_events):
            result = await connector.check_taxonomy_health("secret", "token")

        assert result["event_count"] == 3
        assert len(result["issues"]) > 0
        assert result["health_score"] < 100
        # Verify specific issue types are detected
        issue_text = " ".join(result["issues"])
        assert "spaces" in issue_text or "uppercase" in issue_text or "duplicate" in issue_text


# ---------------------------------------------------------------------------
# 8. check_event_volume_anomalies
# ---------------------------------------------------------------------------


class TestCheckEventVolumeAnomalies:
    @pytest.mark.anyio
    async def test_check_event_volume_anomalies_no_variance(self):
        """Static placeholder returns baseline_std == 0 and anomaly_count == 0."""
        connector = MixpanelConnector()
        result = await connector.check_event_volume_anomalies("secret", "token")

        assert result["baseline_std"] == 0.0
        assert result["anomaly_count"] == 0
        assert result["anomalies"] == []
        assert result["health_score"] == 100

    @pytest.mark.anyio
    async def test_check_event_volume_anomalies_with_spike(self):
        """The response shape includes all expected keys (placeholder returns no anomalies)."""
        connector = MixpanelConnector()
        result = await connector.check_event_volume_anomalies("secret", "token", days_back=14)

        assert result["metric"] == "event_volume_anomalies"
        assert result["days_back"] == 14
        assert "baseline_mean" in result
        assert "baseline_std" in result
        assert "anomalies" in result
        assert "anomaly_count" in result
        assert "health_score" in result


# ---------------------------------------------------------------------------
# 9. Error handling — 401
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_error_handling_401(self):
        """_classify_error maps HTTP 401 to AuthenticationError mentioning 'Mixpanel'."""
        from app.connectors.errors import AuthenticationError, _classify_error

        exc = Exception("HTTP 401 Unauthorized")
        result = _classify_error(exc, "Mixpanel")
        assert isinstance(result, AuthenticationError)
        assert "Mixpanel" in str(result)
        assert "reconnected" in str(result)
