"""
Connector Error Handling Tests

Covers:
  1. ConnectorError hierarchy (base class, subclasses, attributes)
  2. Error classification (_classify_error pattern matching)
  3. @friendly_errors decorator (async wrapping, passthrough, sync support)
"""

import pytest

from app.connectors.errors import (
    AuthenticationError,
    ConnectorError,
    InvalidRequestError,
    PlatformUnavailableError,
    QuotaExceededError,
    RateLimitError,
    _classify_error,
    friendly_errors,
)

# ---------------------------------------------------------------------------
# 1. ConnectorError hierarchy
# ---------------------------------------------------------------------------


class TestErrorHierarchy:
    """Verify the exception class structure and attributes."""

    def test_base_error_attributes(self):
        err = ConnectorError("Something went wrong", platform="GA4", original=ValueError("raw"))
        assert str(err) == "Something went wrong"
        assert err.platform == "GA4"
        assert isinstance(err.original, ValueError)

    def test_subclasses_inherit_from_connector_error(self):
        subclasses = [
            PlatformUnavailableError,
            AuthenticationError,
            RateLimitError,
            InvalidRequestError,
            QuotaExceededError,
        ]
        for cls in subclasses:
            err = cls("test", platform="Test")
            assert isinstance(err, ConnectorError)
            assert isinstance(err, Exception)

    def test_except_connector_error_catches_subclasses(self):
        """Ensure try/except ConnectorError catches all subclasses."""
        for cls in [
            AuthenticationError,
            RateLimitError,
            PlatformUnavailableError,
            InvalidRequestError,
            QuotaExceededError,
        ]:
            with pytest.raises(ConnectorError):
                raise cls("test", platform="Test")


# ---------------------------------------------------------------------------
# 2. Error classification
# ---------------------------------------------------------------------------


class TestErrorClassification:
    """Tests for _classify_error() — pattern matching against known errors."""

    def test_401_maps_to_auth_error(self):
        exc = Exception("HTTP 401 Unauthorized")
        result = _classify_error(exc, "GA4")
        assert isinstance(result, AuthenticationError)
        assert "GA4" in str(result)
        assert "reconnected" in str(result)

    def test_403_maps_to_auth_error(self):
        exc = Exception("403 Forbidden: insufficient permissions")
        result = _classify_error(exc, "Meta")
        assert isinstance(result, AuthenticationError)
        assert "Meta" in str(result)

    def test_429_maps_to_rate_limit(self):
        exc = Exception("HTTP 429 Too Many Requests")
        result = _classify_error(exc, "TikTok")
        assert isinstance(result, RateLimitError)
        assert "TikTok" in str(result)

    def test_too_many_requests_maps_to_rate_limit(self):
        exc = Exception("too many requests, please slow down")
        result = _classify_error(exc, "Snap")
        assert isinstance(result, RateLimitError)

    def test_resource_exhausted_maps_to_rate_limit(self):
        exc = Exception("RESOURCE_EXHAUSTED: Quota exceeded")
        result = _classify_error(exc, "GA4")
        assert isinstance(result, RateLimitError)

    def test_503_maps_to_unavailable(self):
        exc = Exception("503 Service Unavailable")
        result = _classify_error(exc, "GTM")
        assert isinstance(result, PlatformUnavailableError)

    def test_502_maps_to_unavailable(self):
        exc = Exception("502 Bad Gateway")
        result = _classify_error(exc, "BigQuery")
        assert isinstance(result, PlatformUnavailableError)

    def test_timeout_maps_to_unavailable(self):
        exc = Exception("Connection timeout after 30s")
        result = _classify_error(exc, "Redshift")
        assert isinstance(result, PlatformUnavailableError)
        assert "timed out" in str(result)

    def test_400_maps_to_invalid_request(self):
        exc = Exception("400 Bad Request: invalid property ID")
        result = _classify_error(exc, "GA4")
        assert isinstance(result, InvalidRequestError)

    def test_404_maps_to_invalid_request(self):
        exc = Exception("404 Not Found")
        result = _classify_error(exc, "GTM")
        assert isinstance(result, InvalidRequestError)

    def test_unknown_metric_maps_to_invalid_request(self):
        exc = Exception("unknown metric 'fooBar' requested")
        result = _classify_error(exc, "GA4")
        assert isinstance(result, InvalidRequestError)

    def test_quota_exceeded_maps_correctly(self):
        exc = Exception("API quota exceeded for project")
        result = _classify_error(exc, "Google Ads")
        assert isinstance(result, QuotaExceededError)

    def test_invalid_grant_maps_to_auth(self):
        exc = Exception("invalid_grant: Token has been expired or revoked")
        result = _classify_error(exc, "GA4")
        assert isinstance(result, AuthenticationError)
        assert "expired" in str(result)

    def test_unknown_error_fallback(self):
        exc = Exception("some completely unknown error nobody expects")
        result = _classify_error(exc, "Amplitude")
        assert isinstance(result, ConnectorError)
        assert not isinstance(result, AuthenticationError)
        assert not isinstance(result, RateLimitError)
        assert "Amplitude" in str(result)
        assert "unexpected error" in str(result)

    def test_platform_name_injected(self):
        """All classified errors should mention the platform name."""
        exc = Exception("HTTP 401")
        result = _classify_error(exc, "MyPlatform")
        assert "MyPlatform" in str(result)
        assert result.platform == "MyPlatform"

    def test_original_exception_preserved(self):
        original = ValueError("raw SDK error")
        result = _classify_error(original, "GA4")
        assert result.original is original


# ---------------------------------------------------------------------------
# 3. @friendly_errors decorator
# ---------------------------------------------------------------------------


class TestFriendlyErrorsDecorator:
    """Tests for the @friendly_errors() decorator on async and sync functions."""

    @pytest.mark.anyio
    async def test_async_success_passthrough(self):
        @friendly_errors("TestPlatform")
        async def good_func():
            return {"data": "ok"}

        result = await good_func()
        assert result == {"data": "ok"}

    @pytest.mark.anyio
    async def test_async_wraps_raw_exception(self):
        @friendly_errors("GA4")
        async def failing_func():
            raise RuntimeError("HTTP 401 Unauthorized from Google")

        with pytest.raises(AuthenticationError) as exc_info:
            await failing_func()
        assert "GA4" in str(exc_info.value)

    @pytest.mark.anyio
    async def test_async_does_not_double_wrap(self):
        """If a ConnectorError is already raised, it should pass through unchanged."""
        original = RateLimitError("Already wrapped", platform="GA4")

        @friendly_errors("GA4")
        async def already_wrapped():
            raise original

        with pytest.raises(RateLimitError) as exc_info:
            await already_wrapped()
        assert exc_info.value is original

    def test_sync_success_passthrough(self):
        @friendly_errors("TestPlatform")
        def good_func():
            return 42

        assert good_func() == 42

    def test_sync_wraps_raw_exception(self):
        @friendly_errors("Redshift")
        def failing_func():
            raise ConnectionError("Connection refused by host")

        with pytest.raises(PlatformUnavailableError) as exc_info:
            failing_func()
        assert "Redshift" in str(exc_info.value)

    def test_sync_does_not_double_wrap(self):
        original = InvalidRequestError("Already classified", platform="Snowflake")

        @friendly_errors("Snowflake")
        def already_classified():
            raise original

        with pytest.raises(InvalidRequestError) as exc_info:
            already_classified()
        assert exc_info.value is original

    @pytest.mark.anyio
    async def test_decorator_preserves_function_name(self):
        @friendly_errors("GA4")
        async def my_special_function():
            pass

        assert my_special_function.__name__ == "my_special_function"

    def test_sync_decorator_preserves_function_name(self):
        @friendly_errors("GTM")
        def another_function():
            pass

        assert another_function.__name__ == "another_function"

    @pytest.mark.anyio
    async def test_unknown_error_becomes_generic_connector_error(self):
        @friendly_errors("Adobe")
        async def mysterious_failure():
            raise Exception("something weird and unknown happened")

        with pytest.raises(ConnectorError) as exc_info:
            await mysterious_failure()
        err = exc_info.value
        assert err.platform == "Adobe"
        assert "unexpected error" in str(err)

    @pytest.mark.anyio
    async def test_chained_exception_preserved(self):
        """The __cause__ of the raised ConnectorError should be the original."""

        @friendly_errors("Meta")
        async def api_call():
            raise ValueError("raw API error 503")

        with pytest.raises(PlatformUnavailableError) as exc_info:
            await api_call()
        assert exc_info.value.__cause__ is not None
        assert isinstance(exc_info.value.__cause__, ValueError)
