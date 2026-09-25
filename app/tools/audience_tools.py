"""MCP tools for Google Data Manager audiences (UserLists)."""

from __future__ import annotations

import base64
import hashlib
import re
from typing import Any

import app.app_state as state
from app.config import settings
from app.tools.shared_helpers import get_current_project, get_current_user

_DATA_MANAGER_SCOPE = "https://www.googleapis.com/auth/datamanager"
_HEX_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_NAME_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


def _no_connection() -> dict[str, Any]:
    url = f"{settings.APP_BASE_URL}/connect/google"
    return {
        "error": True,
        "error_type": "connection_missing",
        "message": "Connect Google Data Manager before using audience tools.",
        "connect_url": url,
        "action_required": f"Visit {url} and enable Google Data Manager.",
    }


def _iter_connections() -> list[Any]:
    project = get_current_project()
    user = get_current_user()
    connections = getattr(project, "connections", None) if project else None
    if not connections:
        connections = getattr(user, "connections", None) if user else None
    return list(connections or [])


def _is_google_connection(connection: Any) -> bool:
    return getattr(connection, "provider", "google") in ("google", "", None)


def _google_connection_id() -> str | None:
    """Prefer a Google connection that already has the Data Manager scope."""
    google_connections = [c for c in _iter_connections() if _is_google_connection(c)]
    for connection in google_connections:
        scopes = list(getattr(connection, "scopes", None) or [])
        if _DATA_MANAGER_SCOPE in scopes:
            return str(connection.id)
    if google_connections:
        return str(google_connections[0].id)
    return None


def _connection_scopes(connection_id: str | None) -> list[str] | None:
    """Return scopes for the selected project connection, if available."""
    if not connection_id:
        return None
    for connection in _iter_connections():
        if str(getattr(connection, "id", "")) == str(connection_id):
            return list(getattr(connection, "scopes", None) or [])
    return None


def _session() -> tuple[str | None, dict[str, Any] | None]:
    """Resolve the Google connection and enforce the Data Manager scope."""
    connection_id = _google_connection_id()
    if not connection_id:
        return None, _no_connection()

    scopes = _connection_scopes(connection_id)
    # Real MCP contexts always carry OAuth scopes.  Allowing an unknown scope
    # set keeps direct/unit tool wiring useful, while never allowing a known
    # connection to bypass the scope check.
    if scopes is not None and _DATA_MANAGER_SCOPE not in scopes:
        url = f"{settings.APP_BASE_URL}/connect/google"
        return None, {
            "error": True,
            "error_type": "insufficient_scope",
            "message": "Google Data Manager scope is not granted for this connection.",
            "action_required": f"Reconnect Google at {url} with Data Manager enabled.",
            "connect_url": url,
            "required_scope": _DATA_MANAGER_SCOPE,
        }
    return connection_id, None


def _resource_name(value: str | None, field: str) -> tuple[str | None, dict[str, Any] | None]:
    if not value or not value.strip():
        return None, {
            "error": True,
            "error_type": "missing_required_param",
            "message": f"{field} is required.",
        }
    value = value.strip().strip("/")
    if ".." in value or "\\" in value or not value.startswith(("accountTypes/", "userLists/")):
        return None, {
            "error": True,
            "error_type": "invalid_param",
            "message": f"{field} must be a Google Data Manager resource name.",
        }
    return value, None


def _parent(
    account_type: str | None, account_id: str | None, parent: str | None
) -> tuple[str | None, dict[str, Any] | None]:
    if parent:
        return _resource_name(parent, "parent")
    if not account_type or not account_id:
        return None, {
            "error": True,
            "error_type": "missing_required_param",
            "message": "Pass parent='accountTypes/.../accounts/...' or both account_type and account_id.",
        }
    if any(ch in account_type or ch in account_id for ch in ("/", "\\", "..")):
        return None, {
            "error": True,
            "error_type": "invalid_param",
            "message": "account_type and account_id are invalid.",
        }
    return f"accountTypes/{account_type}/accounts/{account_id}", None


_KEY_ALIASES = {
    "display_name": "displayName",
    "membership_status": "membershipStatus",
    "membership_duration": "membershipDuration",
    "integration_code": "integrationCode",
    "ingested_user_list_info": "ingestedUserListInfo",
    "target_network_info": "targetNetworkInfo",
    "account_access_status": "accountAccessStatus",
    "audience_members": "audienceMembers",
    "user_data": "userData",
    "user_identifiers": "userIdentifiers",
    "email_address": "emailAddress",
    "phone_number": "phoneNumber",
    "given_name": "givenName",
    "family_name": "familyName",
    "region_code": "regionCode",
    "postal_code": "postalCode",
    "address_line": "addressLine",
    "administrative_area": "administrativeArea",
    "upload_key_types": "uploadKeyTypes",
    "encryption_info": "encryptionInfo",
    "terms_of_service": "termsOfService",
    "validate_only": "validateOnly",
    "remove_as_of_time": "removeAsOfTime",
    "operating_account": "operatingAccount",
    "product_destination_id": "productDestinationId",
    "login_customer_id": "loginCustomerId",
    "account_type": "accountType",
    "account_id": "accountId",
}


def _snake_to_camel(key: str) -> str:
    if key in _KEY_ALIASES:
        return _KEY_ALIASES[key]
    if "_" not in key or key.startswith("_"):
        return key
    head, *rest = key.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest if part)


def _camelize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            _snake_to_camel(str(key)): _camelize_value(item) for key, item in value.items() if key != "id"
        }
    if isinstance(value, list):
        return [_camelize_value(item) for item in value]
    return value


def _camelize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Convert snake_case keys to Data Manager camelCase, including nested dicts."""
    camelized = _camelize_value(payload)
    return camelized if isinstance(camelized, dict) else {}


def _already_hashed(value: str) -> bool:
    if _HEX_SHA256.fullmatch(value):
        return True
    try:
        decoded = base64.b64decode(value, validate=True)
    except Exception:
        return False
    return len(decoded) == 32


def _sha256(value: str, encoding: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    if encoding.upper() == "BASE64":
        return base64.b64encode(digest).decode("ascii")
    return digest.hex()


def _normalize_email(value: str) -> str:
    email = "".join(value.strip().lower().split())
    local, sep, domain = email.partition("@")
    if not sep:
        return email
    if domain in {"gmail.com", "googlemail.com"}:
        local = local.split("+", 1)[0].replace(".", "")
    return f"{local}@{domain}"


def _normalize_phone(value: str) -> str:
    trimmed = value.strip()
    if trimmed.startswith("00"):
        digits = "".join(ch for ch in trimmed[2:] if ch.isdigit())
        return f"+{digits}"
    digits = "".join(ch for ch in trimmed if ch.isdigit())
    return f"+{digits}"


def _normalize_name(value: str) -> str:
    lowered = value.strip().lower()
    return " ".join(_NAME_PUNCT.sub("", lowered).split())


def _hash_string(value: Any, encoding: str, *, kind: str) -> Any:
    if not isinstance(value, str) or not value.strip():
        return value
    if _already_hashed(value.strip()):
        return value.strip()
    if kind == "email":
        return _sha256(_normalize_email(value), encoding)
    if kind == "phone":
        return _sha256(_normalize_phone(value), encoding)
    if kind == "name":
        return _sha256(_normalize_name(value), encoding)
    return value


def _hash_address(address: dict[str, Any], encoding: str) -> dict[str, Any]:
    hashed = dict(address)
    for field in ("givenName", "familyName"):
        if field in hashed:
            hashed[field] = _hash_string(hashed[field], encoding, kind="name")
    return hashed


def _hash_identifier(identifier: dict[str, Any], encoding: str) -> dict[str, Any]:
    hashed = dict(identifier)
    if "emailAddress" in hashed:
        hashed["emailAddress"] = _hash_string(hashed["emailAddress"], encoding, kind="email")
    if "phoneNumber" in hashed:
        hashed["phoneNumber"] = _hash_string(hashed["phoneNumber"], encoding, kind="phone")
    if isinstance(hashed.get("address"), dict):
        hashed["address"] = _hash_address(hashed["address"], encoding)
    return hashed


def hash_audience_members(members: list[Any] | None, encoding: str | None = None) -> list[Any] | None:
    """Normalize and SHA-256 hash UserData identifiers; skip values already hashed."""
    if not members:
        return members
    enc = (encoding or "HEX").upper()
    hashed_members: list[Any] = []
    for member in members:
        if not isinstance(member, dict):
            hashed_members.append(member)
            continue
        member = dict(member)
        user_data = member.get("userData")
        if isinstance(user_data, dict):
            identifiers = user_data.get("userIdentifiers")
            if isinstance(identifiers, list):
                user_data = dict(user_data)
                user_data["userIdentifiers"] = [
                    _hash_identifier(item, enc) if isinstance(item, dict) else item for item in identifiers
                ]
                member["userData"] = user_data
        hashed_members.append(member)
    return hashed_members


def _payload(config: dict[str, Any] | None, **values: Any) -> dict[str, Any]:
    result = dict(config or {})
    result.update({key: value for key, value in values.items() if value is not None})
    body = _camelize_payload(result)
    encoding = body.get("encoding") if isinstance(body.get("encoding"), str) else "HEX"
    if isinstance(body.get("audienceMembers"), list):
        body["audienceMembers"] = hash_audience_members(body["audienceMembers"], encoding)
    return body


def register_audience_tools(mcp_server) -> None:
    @mcp_server.tool("audience_read")
    async def audience_read(
        action: str = "",
        parent: str | None = None,
        account_type: str | None = None,
        account_id: str | None = None,
        name: str | None = None,
        request_id: str | None = None,
        page_size: int = 100,
        page_token: str | None = None,
        filter: str | None = None,
    ) -> dict[str, Any]:
        """Read Google Data Manager audiences.

        Actions: list_audiences/list_user_lists, get_audience/get_user_list,
        and get_request_status. A parent is accountTypes/.../accounts/...;
        account_type and account_id can be used instead.
        """
        connection_id, error = _session()
        if error:
            return error
        connector = state.data_manager_connector
        if connector is None:
            return {
                "error": True,
                "error_type": "server_error",
                "message": "Data Manager connector is not initialised.",
            }
        if action in {"list_audiences", "list_user_lists"}:
            resolved_parent, error = _parent(account_type, account_id, parent)
            if error:
                return error
            assert connection_id is not None and resolved_parent is not None
            return await connector.list_audiences(
                connection_id,
                resolved_parent,
                page_size=page_size,
                page_token=page_token,
                filter_expression=filter,
            )
        if action in {"get_audience", "get_user_list"}:
            resolved_name, error = _resource_name(name, "name")
            if error:
                return error
            assert connection_id is not None and resolved_name is not None
            return await connector.get_audience(connection_id, resolved_name)
        if action == "get_request_status":
            if not request_id:
                return {
                    "error": True,
                    "error_type": "missing_required_param",
                    "message": "request_id is required.",
                }
            assert connection_id is not None
            return await connector.get_request_status(connection_id, request_id)
        return {
            "error": True,
            "error_type": "unknown_action",
            "message": f"Unknown action '{action}' for audience_read.",
        }

    @mcp_server.tool("audience_write")
    async def audience_write(
        action: str = "",
        parent: str | None = None,
        account_type: str | None = None,
        account_id: str | None = None,
        name: str | None = None,
        config: dict[str, Any] | None = None,
        update_mask: str | None = None,
        audience_members: list[dict[str, Any]] | None = None,
        destinations: list[dict[str, Any]] | None = None,
        encoding: str | None = None,
        encryption_info: dict[str, Any] | None = None,
        terms_of_service: dict[str, Any] | None = None,
        consent: dict[str, Any] | None = None,
        validate_only: bool | None = None,
        remove_as_of_time: str | None = None,
    ) -> dict[str, Any]:
        """Create/manage Data Manager UserLists and ingest or remove members.

        Writes are sent directly to Google Data Manager. Member identifiers
        must follow Google's hashing and consent requirements; use
        validate_only=true to validate a batch without applying it.
        """
        connection_id, error = _session()
        if error:
            return error
        connector = state.data_manager_connector
        if connector is None:
            return {
                "error": True,
                "error_type": "server_error",
                "message": "Data Manager connector is not initialised.",
            }

        if action in {"create_audience", "create_user_list"}:
            resolved_parent, error = _parent(account_type, account_id, parent)
            if error:
                return error
            assert connection_id is not None and resolved_parent is not None
            body = _payload(config)
            if not body.get("displayName"):
                return {
                    "error": True,
                    "error_type": "missing_required_param",
                    "message": "config.display_name is required.",
                }
            return await connector.create_audience(connection_id, resolved_parent, body)

        if action in {"update_audience", "update_user_list", "delete_audience", "delete_user_list"}:
            resolved_name, error = _resource_name(name, "name")
            if error:
                return error
            assert connection_id is not None and resolved_name is not None
            if action in {"delete_audience", "delete_user_list"}:
                return await connector.delete_audience(connection_id, resolved_name)
            return await connector.update_audience(
                connection_id, resolved_name, _payload(config), update_mask
            )

        if action in {"ingest_audience_members", "remove_audience_members"}:
            if not audience_members:
                return {
                    "error": True,
                    "error_type": "missing_required_param",
                    "message": "audience_members is required.",
                }
            if not destinations:
                return {
                    "error": True,
                    "error_type": "missing_required_param",
                    "message": "destinations is required.",
                }
            assert connection_id is not None
            body = _payload(
                config,
                audience_members=audience_members,
                destinations=destinations,
                encoding=encoding,
                encryption_info=encryption_info,
                terms_of_service=terms_of_service,
                consent=consent,
                validate_only=validate_only,
            )
            method = (
                connector.ingest_audience_members
                if action == "ingest_audience_members"
                else connector.remove_audience_members
            )
            return await method(connection_id, body)

        if action == "remove_all_audience_members":
            if not destinations:
                return {
                    "error": True,
                    "error_type": "missing_required_param",
                    "message": "destinations is required.",
                }
            assert connection_id is not None
            body = _payload(
                config,
                destinations=destinations,
                validate_only=validate_only,
                remove_as_of_time=remove_as_of_time,
            )
            return await connector.remove_all_audience_members(connection_id, body)

        return {
            "error": True,
            "error_type": "unknown_action",
            "message": f"Unknown action '{action}' for audience_write.",
        }
