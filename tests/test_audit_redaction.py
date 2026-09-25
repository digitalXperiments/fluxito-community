"""
Regression: secrets must not be persisted to the audit trail (FINDINGS S1 #11).

dashboard_rotate_token returns a fresh `query_token` to the caller. The live
response keeps it, but the audit-trail copy (response_preview / arguments) must
have it redacted. _write_audit_row runs every value through _redact_secrets.
"""

from __future__ import annotations

from app.tools.registry import _REDACTED, _redact_secrets


def test_redacts_secret_keyed_values_recursively():
    data = {
        "query_token": "abc123",
        "title": "My Dashboard",
        "nested": {"client_secret": "s3cr3t", "ok": 1},
        "items": [{"api_key": "k"}, {"id": 2}],
        "access_token": "t",
        "service_account_encrypted": "blob",
    }
    red = _redact_secrets(data)
    assert red["query_token"] == _REDACTED
    assert red["access_token"] == _REDACTED
    assert red["service_account_encrypted"] == _REDACTED
    assert red["nested"]["client_secret"] == _REDACTED
    assert red["items"][0]["api_key"] == _REDACTED
    # non-secret values are preserved
    assert red["title"] == "My Dashboard"
    assert red["nested"]["ok"] == 1
    assert red["items"][1]["id"] == 2
    # the original object is not mutated
    assert data["query_token"] == "abc123"


def test_redacts_rotate_token_response_shape():
    # The exact shape dashboard_rotate_token returns.
    result = {
        "rotated": True,
        "dashboard_id": "d-1",
        "query_token": "super-secret-token",
        "hint": "Token rotated.",
    }
    red = _redact_secrets(result)
    assert red["query_token"] == _REDACTED
    assert red["rotated"] is True
    assert red["dashboard_id"] == "d-1"


def test_redact_handles_scalars_and_none():
    assert _redact_secrets(None) is None
    assert _redact_secrets("plain") == "plain"
    assert _redact_secrets(42) == 42
    assert _redact_secrets([1, "a"]) == [1, "a"]
