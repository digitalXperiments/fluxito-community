"""
Unit tests for the PostHogConnector.

Covers:
  1. Instantiation
  2. list_projects
  3. get_events_list
  4. query_events
  5. get_revenue
  6. check_taxonomy_health (clean + issues)
  7. check_event_volume_anomalies (no variance + spike)
  8. Error handling (401 → AuthenticationError via _classify_error)

All HTTP calls are mocked via unittest.mock.patch on httpx.AsyncClient.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.connectors.posthog import PostHogConnector

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HOST = "https://app.posthog.com"
_PROJECT_ID = 12345


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
    mock_client.patch.return_value = response
    mock_client.delete.return_value = response

    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = mock_client
    return patch("httpx.AsyncClient", return_value=mock_cm)


# ---------------------------------------------------------------------------
# 1. Instantiation
# ---------------------------------------------------------------------------


class TestInstantiation:
    def test_class_instantiation(self):
        connector = PostHogConnector()
        assert connector is not None
        assert isinstance(connector, PostHogConnector)


# ---------------------------------------------------------------------------
# 2. list_projects
# ---------------------------------------------------------------------------


class TestListProjects:
    @pytest.mark.anyio
    async def test_list_projects_success(self):
        """GET /api/projects/ returns {valid: True, projects: [...]}."""
        resp = _mock_httpx_response({"results": [{"id": 1, "name": "My Project"}]})
        with _patch_httpx(resp):
            connector = PostHogConnector()
            result = await connector.list_projects("api_key", _HOST)

        assert result["valid"] is True
        assert "PostHog" in result["message"]
        assert len(result["projects"]) == 1

    @pytest.mark.anyio
    async def test_list_projects_invalid_creds(self):
        """A 401 returns an error dict."""
        resp = _mock_httpx_response({"detail": "Invalid API key"}, status_code=401)
        with _patch_httpx(resp):
            connector = PostHogConnector()
            result = await connector.list_projects("bad_key", _HOST)

        assert result.get("error") is True
        assert result.get("status_code") == 401


# ---------------------------------------------------------------------------
# 3. get_events_list
# ---------------------------------------------------------------------------


class TestGetEventsList:
    @pytest.mark.anyio
    async def test_get_events_list_returns_events_and_total(self):
        """GET /api/projects/<id>/events/ normalises the response."""
        resp = _mock_httpx_response(
            {
                "results": [
                    {"name": "$pageview", "last_seen_at": "2024-01-15"},
                    {"name": "purchase", "last_seen_at": "2024-01-14"},
                ]
            }
        )
        with _patch_httpx(resp):
            connector = PostHogConnector()
            result = await connector.get_events_list("api_key", _HOST, _PROJECT_ID)

        assert result["total"] == 2
        assert result["events"][0]["event_type"] == "$pageview"
        assert result["events"][1]["event_type"] == "purchase"

    @pytest.mark.anyio
    async def test_get_events_list_empty(self):
        """Empty results returns zero events."""
        resp = _mock_httpx_response({"results": []})
        with _patch_httpx(resp):
            connector = PostHogConnector()
            result = await connector.get_events_list("api_key", _HOST, _PROJECT_ID)

        assert result["total"] == 0
        assert result["events"] == []


# ---------------------------------------------------------------------------
# 4. query_events
# ---------------------------------------------------------------------------


class TestQueryEvents:
    @pytest.mark.anyio
    async def test_query_events_shape(self):
        """Query endpoint returns {event_type, events, total, start_date, end_date}."""
        resp = _mock_httpx_response(
            {
                "results": [
                    {"event": "purchase", "timestamp": "2024-01-01T10:00:00Z"},
                    {"event": "purchase", "timestamp": "2024-01-02T11:00:00Z"},
                ]
            }
        )
        with _patch_httpx(resp):
            connector = PostHogConnector()
            result = await connector.query_events(
                "api_key", _HOST, _PROJECT_ID, "2024-01-01", "2024-01-07", "purchase"
            )

        assert result["event_type"] == "purchase"
        assert result["start_date"] == "2024-01-01"
        assert result["end_date"] == "2024-01-07"
        assert result["total"] == 2
        assert len(result["events"]) == 2


# ---------------------------------------------------------------------------
# 5. get_revenue
# ---------------------------------------------------------------------------


class TestGetRevenue:
    @pytest.mark.anyio
    async def test_get_revenue_shape(self):
        """Revenue endpoint returns {metric: 'revenue', events, total, ...}."""
        resp = _mock_httpx_response(
            {
                "results": [
                    {"event": "purchase", "properties": {"revenue": 99.99}},
                    {"event": "purchase", "properties": {"revenue": 49.99}},
                ]
            }
        )
        with _patch_httpx(resp):
            connector = PostHogConnector()
            result = await connector.get_revenue("api_key", _HOST, _PROJECT_ID, "2024-01-01", "2024-01-07")

        assert result["metric"] == "revenue"
        assert result["start_date"] == "2024-01-01"
        assert result["end_date"] == "2024-01-07"
        assert result["total"] == 2
        assert len(result["events"]) == 2
        assert "note" in result

    @pytest.mark.anyio
    async def test_get_revenue_error_propagates(self):
        """A 401 returns an error dict."""
        resp = _mock_httpx_response({"detail": "Unauthorized"}, status_code=401)
        with _patch_httpx(resp):
            connector = PostHogConnector()
            result = await connector.get_revenue("bad_key", _HOST, _PROJECT_ID, "2024-01-01", "2024-01-07")

        assert result.get("error") is True


# ---------------------------------------------------------------------------
# 6. check_taxonomy_health
# ---------------------------------------------------------------------------


class TestCheckTaxonomyHealth:
    @pytest.mark.anyio
    async def test_check_taxonomy_health_clean(self):
        """Well-named events produce health_score == 100 and zero issues."""
        connector = PostHogConnector()

        clean_events = {
            "events": [
                {"event_type": "$pageview"},
                {"event_type": "purchase"},
                {"event_type": "add_to_cart"},
            ],
            "total": 3,
        }
        with patch.object(connector, "get_events_list", new_callable=AsyncMock, return_value=clean_events):
            result = await connector.check_taxonomy_health("api_key", _HOST, _PROJECT_ID)

        assert result["event_count"] == 3
        assert result["health_score"] == 100
        assert result["issues"] == []

    @pytest.mark.anyio
    async def test_check_taxonomy_health_issues(self):
        """Events with spaces, uppercase, and duplicates produce issues and score < 100."""
        connector = PostHogConnector()

        messy_events = {
            "events": [
                {"event_type": "Page View"},  # uppercase + space
                {"event_type": "Purchase"},  # uppercase
                {"event_type": "page view"},  # space + duplicate (case-insensitive)
            ],
            "total": 3,
        }
        with patch.object(connector, "get_events_list", new_callable=AsyncMock, return_value=messy_events):
            result = await connector.check_taxonomy_health("api_key", _HOST, _PROJECT_ID)

        assert result["event_count"] == 3
        assert len(result["issues"]) > 0
        assert result["health_score"] < 100
        issue_text = " ".join(result["issues"])
        assert "spaces" in issue_text or "uppercase" in issue_text or "duplicate" in issue_text


# ---------------------------------------------------------------------------
# 7. check_event_volume_anomalies
# ---------------------------------------------------------------------------


class TestCheckEventVolumeAnomalies:
    @pytest.mark.anyio
    async def test_check_event_volume_anomalies_no_variance(self):
        """A flat series (all same values) produces baseline_std == 0, anomaly_count == 0."""
        connector = PostHogConnector()

        # 12 identical values — baseline half = first 6, all same → stdev = 0
        flat_series = [100] * 12
        resp = _mock_httpx_response({"result": [{"data": flat_series}]})

        with _patch_httpx(resp):
            result = await connector.check_event_volume_anomalies("api_key", _HOST, _PROJECT_ID, days_back=6)

        assert result["baseline_std"] == 0.0
        assert result["anomaly_count"] == 0
        assert result["anomalies"] == []
        assert result["health_score"] == 100

    @pytest.mark.anyio
    async def test_check_event_volume_anomalies_with_spike(self):
        """A series with a clear spike in the recent half detects anomalies."""
        connector = PostHogConnector()

        # Baseline: 6 values around 100 (with some variance so stdev > 0)
        # Recent: 6 values where the last 4 are 10x the baseline
        series = [90, 100, 110, 100, 90, 100, 110, 100, 500, 500, 500, 500]
        resp = _mock_httpx_response({"result": [{"data": series}]})

        with _patch_httpx(resp):
            result = await connector.check_event_volume_anomalies("api_key", _HOST, _PROJECT_ID, days_back=6)

        assert result["baseline_std"] > 0
        assert result["anomaly_count"] > 0
        assert len(result["anomalies"]) > 0
        # All anomalies should be spikes (positive z-score)
        for anomaly in result["anomalies"]:
            assert anomaly["direction"] == "spike"
            assert anomaly["z_score"] > 2.0

    @pytest.mark.anyio
    async def test_check_event_volume_anomalies_insufficient_data(self):
        """Fewer than 4 data points returns a note about insufficient data."""
        connector = PostHogConnector()

        resp = _mock_httpx_response({"result": [{"data": [10, 20, 30]}]})
        with _patch_httpx(resp):
            result = await connector.check_event_volume_anomalies("api_key", _HOST, _PROJECT_ID, days_back=6)

        assert result["anomaly_count"] == 0
        assert "Insufficient" in result.get("note", "")


# ---------------------------------------------------------------------------
# 8. Error handling — 401
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_error_handling_401(self):
        """_classify_error maps HTTP 401 to AuthenticationError mentioning 'PostHog'."""
        from app.connectors.errors import AuthenticationError, _classify_error

        exc = Exception("HTTP 401 Unauthorized")
        result = _classify_error(exc, "PostHog")
        assert isinstance(result, AuthenticationError)
        assert "PostHog" in str(result)
        assert "reconnected" in str(result)
