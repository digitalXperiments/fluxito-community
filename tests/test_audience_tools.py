"""Unit tests for Google Data Manager audience helpers and write guards."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from app.tools import audience_tools as at


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class _FakeMcp:
    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, name: str):
        def deco(fn):
            self.tools[name] = fn
            return fn

        return deco


def test_parent_from_account_parts():
    parent, error = at._parent("GOOGLE_ADS", "123", None)
    assert error is None
    assert parent == "accountTypes/GOOGLE_ADS/accounts/123"


def test_parent_rejects_path_injection():
    parent, error = at._parent("GOOGLE_ADS/../x", "123", None)
    assert parent is None
    assert error["error_type"] == "invalid_param"


def test_resource_name_requires_account_types_prefix():
    name, error = at._resource_name("accounts/123/userLists/1", "name")
    assert name is None
    assert error["error_type"] == "invalid_param"


def test_camelize_nested_user_list_config():
    body = at._camelize_payload(
        {
            "display_name": "Buyers",
            "ingested_user_list_info": {"upload_key_types": ["CONTACT_ID"]},
        }
    )
    assert body["displayName"] == "Buyers"
    assert body["ingestedUserListInfo"]["uploadKeyTypes"] == ["CONTACT_ID"]


def test_hash_email_phone_and_names_and_skip_already_hashed():
    already = _sha("already@example.com")
    members = [
        {
            "user_data": {
                "user_identifiers": [
                    {"email_address": "Jane.Doe+promo@gmail.com"},
                    {"phone_number": "+1 (800) 555-0100"},
                    {
                        "address": {
                            "given_name": "Jane",
                            "family_name": "Doe",
                            "region_code": "US",
                            "postal_code": "10001",
                        }
                    },
                    {"email_address": already},
                ]
            }
        }
    ]
    body = at._payload(None, audience_members=members, encoding="HEX")
    identifiers = body["audienceMembers"][0]["userData"]["userIdentifiers"]
    assert identifiers[0]["emailAddress"] == _sha("janedoe@gmail.com")
    assert identifiers[1]["phoneNumber"] == _sha("+18005550100")
    assert identifiers[2]["address"]["givenName"] == _sha("jane")
    assert identifiers[2]["address"]["familyName"] == _sha("doe")
    assert identifiers[2]["address"]["regionCode"] == "US"
    assert identifiers[2]["address"]["postalCode"] == "10001"
    assert identifiers[3]["emailAddress"] == already


def test_google_connection_prefers_datamanager_scope(monkeypatch):
    ga4_only = SimpleNamespace(
        id="conn-ga4",
        provider="google",
        scopes=["https://www.googleapis.com/auth/analytics.readonly"],
    )
    dm = SimpleNamespace(
        id="conn-dm",
        provider="google",
        scopes=[at._DATA_MANAGER_SCOPE],
    )
    monkeypatch.setattr(
        at,
        "get_current_project",
        lambda: SimpleNamespace(connections=[ga4_only, dm]),
    )
    monkeypatch.setattr(at, "get_current_user", lambda: None)
    assert at._google_connection_id() == "conn-dm"


@pytest.mark.asyncio
async def test_ingest_requires_destinations(monkeypatch):
    mcp = _FakeMcp()
    at.register_audience_tools(mcp)
    monkeypatch.setattr(
        at,
        "_session",
        lambda: ("conn-dm", None),
    )
    monkeypatch.setattr(at.state, "data_manager_connector", object())
    result = await mcp.tools["audience_write"](
        action="ingest_audience_members",
        audience_members=[{"userData": {"userIdentifiers": [{"emailAddress": "a@b.com"}]}}],
    )
    assert result["error"] is True
    assert result["error_type"] == "missing_required_param"
    assert "destinations" in result["message"]


@pytest.mark.asyncio
async def test_remove_all_requires_destinations(monkeypatch):
    mcp = _FakeMcp()
    at.register_audience_tools(mcp)
    monkeypatch.setattr(at, "_session", lambda: ("conn-dm", None))
    monkeypatch.setattr(at.state, "data_manager_connector", object())
    result = await mcp.tools["audience_write"](action="remove_all_audience_members")
    assert result["error"] is True
    assert "destinations" in result["message"]
