"""
Unit tests for the product-analytics revenue adapters and credential resolvers.

Covers:
  1. get_amplitude_creds — resolve + no-connection
  2. get_mixpanel_creds — resolve
  3. get_posthog_creds — resolve
  4. _adapter_amplitude — success path
  5. _adapter_mixpanel — success path
  6. _adapter_posthog — success path

Mocks are applied at the DB layer (get_encrypted_credential_conn) and
the decrypt_field helper so no real DB or encryption is needed.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import app.app_state as state
import app.tools.shared_helpers as shared_helpers

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

_FAKE_USER_ID = str(uuid.uuid4())
_FAKE_CONN_ID = str(uuid.uuid4())


def _make_mock_conn(**extra_fields):
    """Build a SimpleNamespace that mimics an ORM connection row."""
    base = {
        "id": uuid.UUID(_FAKE_CONN_ID),
        "is_active": True,
        "api_key_encrypted": "enc_api_key",
        "secret_key_encrypted": "enc_secret_key",
    }
    base.update(extra_fields)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# 1. get_amplitude_creds
# ---------------------------------------------------------------------------


class TestGetAmplitudeCreds:
    @pytest.mark.anyio
    async def test_get_amplitude_creds_resolves(self):
        """Resolves (conn_id, api_key, secret_key) from encrypted connection."""
        mock_conn = _make_mock_conn()

        with (
            patch.object(
                shared_helpers,
                "get_encrypted_credential_conn",
                new_callable=AsyncMock,
                return_value=mock_conn,
            ),
            patch.object(
                shared_helpers,
                "decrypt_field",
                side_effect=lambda v: f"decrypted_{v}",
            ),
        ):
            from app.tools.shared_helpers import get_amplitude_creds

            conn_id, api_key, secret_key = await get_amplitude_creds(_FAKE_USER_ID)

        assert conn_id == _FAKE_CONN_ID
        assert api_key == "decrypted_enc_api_key"
        assert secret_key == "decrypted_enc_secret_key"

    @pytest.mark.anyio
    async def test_get_amplitude_creds_no_connection(self):
        """Returns (None, None, None) when no connection exists."""
        with patch.object(
            shared_helpers,
            "get_encrypted_credential_conn",
            new_callable=AsyncMock,
            return_value=None,
        ):
            from app.tools.shared_helpers import get_amplitude_creds

            conn_id, api_key, secret_key = await get_amplitude_creds(_FAKE_USER_ID)

        assert conn_id is None
        assert api_key is None
        assert secret_key is None


# ---------------------------------------------------------------------------
# 2. get_mixpanel_creds
# ---------------------------------------------------------------------------


class TestGetMixpanelCreds:
    @pytest.mark.anyio
    async def test_get_mixpanel_creds_resolves(self):
        """Resolves (conn_id, api_secret, service_token) from encrypted connection."""
        mock_conn = _make_mock_conn()

        with (
            patch.object(
                shared_helpers,
                "get_encrypted_credential_conn",
                new_callable=AsyncMock,
                return_value=mock_conn,
            ),
            patch.object(
                shared_helpers,
                "decrypt_field",
                side_effect=lambda v: f"decrypted_{v}",
            ),
        ):
            from app.tools.shared_helpers import get_mixpanel_creds

            conn_id, api_secret, service_token = await get_mixpanel_creds(_FAKE_USER_ID)

        assert conn_id == _FAKE_CONN_ID
        assert api_secret == "decrypted_enc_api_key"
        assert service_token == "decrypted_enc_secret_key"


# ---------------------------------------------------------------------------
# 3. get_posthog_creds
# ---------------------------------------------------------------------------


class TestGetPosthogCreds:
    @pytest.mark.anyio
    async def test_get_posthog_creds_resolves(self):
        """Resolves (conn_id, api_key, project_host, project_id) — 4-tuple."""
        mock_conn = _make_mock_conn(
            project_host="https://app.posthog.com",
            external_project_id="12345",
        )

        with (
            patch.object(
                shared_helpers,
                "get_encrypted_credential_conn",
                new_callable=AsyncMock,
                return_value=mock_conn,
            ),
            patch.object(
                shared_helpers,
                "decrypt_field",
                side_effect=lambda v: f"decrypted_{v}",
            ),
        ):
            from app.tools.shared_helpers import get_posthog_creds

            conn_id, api_key, project_host, project_id = await get_posthog_creds(_FAKE_USER_ID)

        assert conn_id == _FAKE_CONN_ID
        assert api_key == "decrypted_enc_api_key"
        assert project_host == "https://app.posthog.com"
        assert project_id == "12345"


# ---------------------------------------------------------------------------
# 4. _adapter_amplitude
# ---------------------------------------------------------------------------


class TestAdapterAmplitude:
    @pytest.mark.anyio
    async def test_adapter_amplitude_success(self):
        """Happy path: creds resolve, connector returns revenue data."""
        mock_user = SimpleNamespace(id=_FAKE_USER_ID, has_amplitude=True)
        mock_connector = AsyncMock()
        mock_connector.get_revenue = AsyncMock(return_value={"data": {"series": [[100, 200, 300]]}})

        with (
            patch.object(state, "amplitude_connector", mock_connector),
            patch.object(
                shared_helpers,
                "get_amplitude_creds",
                new_callable=AsyncMock,
                return_value=(_FAKE_CONN_ID, "api_key", "secret_key"),
            ),
        ):
            from app.tools.cross_platform_tools import _adapter_amplitude

            result = await _adapter_amplitude(mock_user, "2024-01-01", "2024-01-31", None)

        assert result["source"] == "amplitude"
        assert result["success"] is True
        assert result["total_revenue_ground_truth"] == 600.0
        assert result["confidence"] == "medium"


# ---------------------------------------------------------------------------
# 5. _adapter_mixpanel
# ---------------------------------------------------------------------------


class TestAdapterMixpanel:
    @pytest.mark.anyio
    async def test_adapter_mixpanel_success(self):
        """Happy path: creds resolve, connector returns revenue data."""
        mock_user = SimpleNamespace(id=_FAKE_USER_ID, has_mixpanel=True)
        mock_connector = AsyncMock()
        mock_connector.get_revenue = AsyncMock(return_value={"data": {"total": 5000}})

        with (
            patch.object(state, "mixpanel_connector", mock_connector),
            patch.object(
                shared_helpers,
                "get_mixpanel_creds",
                new_callable=AsyncMock,
                return_value=(_FAKE_CONN_ID, "api_secret", "service_token"),
            ),
        ):
            from app.tools.cross_platform_tools import _adapter_mixpanel

            result = await _adapter_mixpanel(mock_user, "2024-01-01", "2024-01-31", None)

        assert result["source"] == "mixpanel"
        assert result["success"] is True
        assert result["total_revenue_ground_truth"] == 5000.0
        assert result["confidence"] == "medium"


# ---------------------------------------------------------------------------
# 6. _adapter_posthog
# ---------------------------------------------------------------------------


class TestAdapterPosthog:
    @pytest.mark.anyio
    async def test_adapter_posthog_success(self):
        """Happy path: creds resolve, connector returns revenue data."""
        mock_user = SimpleNamespace(id=_FAKE_USER_ID, has_posthog=True)
        mock_connector = AsyncMock()
        mock_connector.get_revenue = AsyncMock(return_value={"total_revenue": 2500.50, "events": []})

        with (
            patch.object(state, "posthog_connector", mock_connector),
            patch.object(
                shared_helpers,
                "get_posthog_creds",
                new_callable=AsyncMock,
                return_value=(
                    _FAKE_CONN_ID,
                    "api_key",
                    "https://app.posthog.com",
                    "12345",
                ),
            ),
        ):
            from app.tools.cross_platform_tools import _adapter_posthog

            result = await _adapter_posthog(mock_user, "2024-01-01", "2024-01-31", None)

        assert result["source"] == "posthog"
        assert result["success"] is True
        assert result["total_revenue_ground_truth"] == 2500.50
        assert result["confidence"] == "medium"
