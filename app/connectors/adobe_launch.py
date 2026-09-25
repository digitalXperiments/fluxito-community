"""
Adobe Launch Connector

Uses the Adobe Launch Reactor API v1 (experience-platform Launch).

Auth: Same Adobe IMS as Adobe Analytics (client_id + client_secret + org_id).
Base URL: https://reactor.adobe.io

All resources follow JSON:API format.

Layer 1 (Read): list_companies, list_properties, get_property, list_rules, get_rule, list_data_elements,
              list_extensions, list_environments, list_libraries, list_builds

Layer 2 (Audit): audit_property, get_publish_history

Layer 3 (Write): create_property, create_rule, create_data_element, create_library, add_resources_to_library,
               build_library, transition_library
"""

import json
import logging
import time
from typing import Any

import httpx

from app.connectors.errors import friendly_errors

logger = logging.getLogger(__name__)

_IMS_BASE = "https://ims-na1.adobelogin.com"
_REACTOR_BASE = "https://reactor.adobe.io"


class AdobeLaunchConnector:
    """Interfaces with Adobe Launch Reactor API using client credentials grant."""

    def __init__(self):
        # In-memory token cache: {org_id: {token, expiry}}
        self._token_cache: dict[str, dict[str, Any]] = {}

    async def _get_adobe_token(self, client_id: str, client_secret: str, org_id: str) -> dict:
        """
        Get or refresh Adobe IMS access token.
        Caches tokens with expiry; refreshes if expired.
        Reuses same logic as adobe_analytics.
        """
        cache_key = org_id
        cached = self._token_cache.get(cache_key, {})

        # Check if cached token is still valid (with 60s buffer)
        if cached.get("token") and cached.get("expiry", 0) > time.time() + 60:
            return {"token": cached["token"]}

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{_IMS_BASE}/ims/token/v3",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "scope": "openid,AdobeID,read_organizations,additional_info.projectedProductContext",
                    },
                )

                if response.status_code >= 400:
                    return {
                        "error": True,
                        "status_code": response.status_code,
                        "message": response.text,
                    }

                data = response.json()
                token = data.get("access_token")
                expires_in = data.get("expires_in", 3600)

                # Cache the token
                self._token_cache[cache_key] = {
                    "token": token,
                    "expiry": time.time() + expires_in,
                }

                return {"token": token}

        except Exception as e:
            logger.error(f"Adobe IMS token request error: {e}")
            return {"error": True, "message": str(e)}

    async def _request(
        self,
        client_id: str,
        client_secret: str,
        org_id: str,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict:
        """
        Make an authenticated request to Adobe Launch Reactor API.
        Automatically obtains and injects bearer token.
        """
        token_result = await self._get_adobe_token(client_id, client_secret, org_id)
        if token_result.get("error"):
            return token_result

        token = token_result.get("token")
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/vnd.api+json",
            "Accept": "application/vnd.api+json;revision=1",
            "x-api-key": client_id,
        }
        if org_id:
            headers["x-gw-ims-org-id"] = org_id

        try:
            url = f"{_REACTOR_BASE}{endpoint}"
            async with httpx.AsyncClient(timeout=30.0) as client:
                if method == "GET":
                    response = await client.get(url, headers=headers, params=params)
                elif method == "POST":
                    response = await client.post(url, headers=headers, params=params, json=json_body)
                elif method == "PUT":
                    response = await client.put(url, headers=headers, params=params, json=json_body)
                elif method == "PATCH":
                    response = await client.patch(url, headers=headers, params=params, json=json_body)
                elif method == "DELETE":
                    response = await client.delete(url, headers=headers, params=params)
                else:
                    return {"error": True, "message": f"Unsupported HTTP method: {method}"}

                if response.status_code >= 400:
                    return {
                        "error": True,
                        "status_code": response.status_code,
                        "message": response.text,
                    }

                try:
                    return response.json()
                except Exception:
                    return {"success": response.status_code < 300, "body": response.text}

        except Exception as e:
            logger.error(f"Adobe Launch API request error: {e}")
            return {"error": True, "message": str(e)}

    # ------------------------------------------------------------------
    # Layer 1: Data Access
    # ------------------------------------------------------------------

    @friendly_errors("Adobe Launch")
    async def list_companies(self, client_id: str, client_secret: str, org_id: str) -> dict:
        """
        List all companies accessible to the org.
        GET /companies
        """
        result = await self._request(client_id, client_secret, org_id, "GET", "/companies")
        if result.get("error"):
            return result

        companies = result.get("data", [])
        return {
            "companies": [
                {
                    "id": c.get("id"),
                    "name": c.get("attributes", {}).get("name"),
                    "org_id": c.get("attributes", {}).get("org_id"),
                }
                for c in companies
            ],
            "total": len(companies),
        }

    @friendly_errors("Adobe Launch")
    async def list_properties(
        self, client_id: str, client_secret: str, org_id: str, company_id: str | None = None
    ) -> dict:
        """
        List all properties for a company.
        GET /companies/{company_id}/properties
        """
        target_company = company_id
        if not target_company or not str(target_company).startswith("CO"):
            companies_res = await self.list_companies(client_id, client_secret, org_id)
            if companies_res.get("error"):
                return companies_res
            companies = companies_res.get("companies", [])
            if companies:
                matched = next(
                    (
                        c
                        for c in companies
                        if target_company
                        and (
                            c.get("id") == target_company
                            or c.get("org_id") == target_company
                            or c.get("name") == target_company
                        )
                    ),
                    None,
                )
                target_company = matched.get("id") if matched else companies[0].get("id")

        if not target_company:
            return {"error": True, "message": "No Adobe Launch company found for this organization."}

        result = await self._request(
            client_id,
            client_secret,
            org_id,
            "GET",
            f"/companies/{target_company}/properties",
        )
        if result.get("error"):
            return result

        properties = result.get("data", [])
        return {
            "company_id": target_company,
            "properties": [
                {
                    "id": p.get("id"),
                    "name": p.get("attributes", {}).get("name"),
                    "platform": p.get("attributes", {}).get("platform"),
                    "domains": p.get("attributes", {}).get("domains", []),
                }
                for p in properties
            ],
            "total": len(properties),
        }

    @friendly_errors("Adobe Launch")
    async def get_property(self, client_id: str, client_secret: str, org_id: str, property_id: str) -> dict:
        """
        Get a single property.
        GET /properties/{property_id}
        """
        result = await self._request(client_id, client_secret, org_id, "GET", f"/properties/{property_id}")
        if result.get("error"):
            return result

        prop = result.get("data", {})
        return {
            "property_id": property_id,
            "name": prop.get("attributes", {}).get("name"),
            "platform": prop.get("attributes", {}).get("platform"),
            "domains": prop.get("attributes", {}).get("domains", []),
            "created": prop.get("attributes", {}).get("created_at"),
            "updated": prop.get("attributes", {}).get("updated_at"),
        }

    @friendly_errors("Adobe Launch")
    async def list_rules(self, client_id: str, client_secret: str, org_id: str, property_id: str) -> dict:
        """
        List all rules for a property.
        GET /properties/{property_id}/rules
        """
        result = await self._request(
            client_id, client_secret, org_id, "GET", f"/properties/{property_id}/rules"
        )
        if result.get("error"):
            return result

        rules = result.get("data", [])
        return {
            "property_id": property_id,
            "rules": [
                {
                    "id": r.get("id"),
                    "name": r.get("attributes", {}).get("name"),
                    "enabled": r.get("attributes", {}).get("enabled"),
                }
                for r in rules
            ],
            "total": len(rules),
        }

    @friendly_errors("Adobe Launch")
    async def get_rule(self, client_id: str, client_secret: str, org_id: str, rule_id: str) -> dict:
        """
        Get a single rule with all components.
        GET /rules/{rule_id}?include=rule_components
        """
        params = {"include": "rule_components"}
        result = await self._request(
            client_id, client_secret, org_id, "GET", f"/rules/{rule_id}", params=params
        )
        if result.get("error"):
            return result

        rule = result.get("data", {})
        components = result.get("included", [])

        formatted_components = [
            {
                "id": c.get("id"),
                "name": c.get("attributes", {}).get("name"),
                "delegate_descriptor_id": c.get("attributes", {}).get("delegate_descriptor_id"),
                "settings": c.get("attributes", {}).get("settings"),
                "rule_order": c.get("attributes", {}).get("rule_order"),
                "order": c.get("attributes", {}).get("order"),
                "negate": c.get("attributes", {}).get("negate"),
                "timeout": c.get("attributes", {}).get("timeout"),
                "delay_next": c.get("attributes", {}).get("delay_next"),
            }
            for c in components
        ]

        return {
            "rule_id": rule_id,
            "name": rule.get("attributes", {}).get("name"),
            "enabled": rule.get("attributes", {}).get("enabled"),
            "components_count": len(components),
            "component_types": list(set(c.get("type", "") for c in components)),
            "components": formatted_components,
        }

    @friendly_errors("Adobe Launch")
    async def list_rule_components(
        self, client_id: str, client_secret: str, org_id: str, rule_id: str
    ) -> dict:
        """
        List all rule components for a rule.
        GET /rules/{rule_id}/rule_components
        """
        result = await self._request(
            client_id, client_secret, org_id, "GET", f"/rules/{rule_id}/rule_components"
        )
        if result.get("error"):
            return result

        components = result.get("data", [])
        return {
            "rule_id": rule_id,
            "rule_components": [
                {
                    "id": c.get("id"),
                    "name": c.get("attributes", {}).get("name"),
                    "delegate_descriptor_id": c.get("attributes", {}).get("delegate_descriptor_id"),
                    "settings": c.get("attributes", {}).get("settings"),
                    "rule_order": c.get("attributes", {}).get("rule_order"),
                    "order": c.get("attributes", {}).get("order"),
                    "negate": c.get("attributes", {}).get("negate"),
                }
                for c in components
            ],
            "total": len(components),
        }

    @friendly_errors("Adobe Launch")
    async def get_rule_component(
        self, client_id: str, client_secret: str, org_id: str, rule_component_id: str
    ) -> dict:
        """
        Get a single rule component.
        GET /rule_components/{rule_component_id}
        """
        result = await self._request(
            client_id, client_secret, org_id, "GET", f"/rule_components/{rule_component_id}"
        )
        if result.get("error"):
            return result

        comp = result.get("data", {})
        attrs = comp.get("attributes", {})
        return {
            "rule_component_id": rule_component_id,
            "name": attrs.get("name"),
            "delegate_descriptor_id": attrs.get("delegate_descriptor_id"),
            "settings": attrs.get("settings"),
            "rule_order": attrs.get("rule_order"),
            "order": attrs.get("order"),
            "negate": attrs.get("negate"),
            "timeout": attrs.get("timeout"),
            "delay_next": attrs.get("delay_next"),
            "created": attrs.get("created_at"),
            "updated": attrs.get("updated_at"),
        }

    @friendly_errors("Adobe Launch")
    async def list_data_elements(
        self, client_id: str, client_secret: str, org_id: str, property_id: str
    ) -> dict:
        """
        List all data elements for a property.
        GET /properties/{property_id}/data_elements
        """
        result = await self._request(
            client_id,
            client_secret,
            org_id,
            "GET",
            f"/properties/{property_id}/data_elements",
        )
        if result.get("error"):
            return result

        elements = result.get("data", [])
        return {
            "property_id": property_id,
            "data_elements": [
                {
                    "id": e.get("id"),
                    "name": e.get("attributes", {}).get("name"),
                    "enabled": e.get("attributes", {}).get("enabled"),
                    "delegate_descriptor_id": e.get("attributes", {}).get("delegate_descriptor_id"),
                }
                for e in elements
            ],
            "total": len(elements),
        }

    @friendly_errors("Adobe Launch")
    async def get_data_element(
        self, client_id: str, client_secret: str, org_id: str, data_element_id: str
    ) -> dict:
        """
        Get a single data element.
        GET /data_elements/{data_element_id}
        """
        result = await self._request(
            client_id, client_secret, org_id, "GET", f"/data_elements/{data_element_id}"
        )
        if result.get("error"):
            return result

        element = result.get("data", {})
        attrs = element.get("attributes", {})
        return {
            "data_element_id": data_element_id,
            "name": attrs.get("name"),
            "enabled": attrs.get("enabled"),
            "delegate_descriptor_id": attrs.get("delegate_descriptor_id"),
            "settings": attrs.get("settings"),
            "clean_text": attrs.get("clean_text"),
            "force_lower_case": attrs.get("force_lower_case"),
            "default_value": attrs.get("default_value"),
            "storage_duration": attrs.get("storage_duration"),
            "created": attrs.get("created_at"),
            "updated": attrs.get("updated_at"),
        }

    @friendly_errors("Adobe Launch")
    async def list_extensions(
        self, client_id: str, client_secret: str, org_id: str, property_id: str
    ) -> dict:
        """
        List all extensions installed for a property.
        GET /properties/{property_id}/extensions
        """
        result = await self._request(
            client_id,
            client_secret,
            org_id,
            "GET",
            f"/properties/{property_id}/extensions",
        )
        if result.get("error"):
            return result

        extensions = result.get("data", [])
        return {
            "property_id": property_id,
            "extensions": [
                {
                    "id": e.get("id"),
                    "name": e.get("attributes", {}).get("name"),
                    "version": e.get("attributes", {}).get("version"),
                    "enabled": e.get("attributes", {}).get("enabled"),
                }
                for e in extensions
            ],
            "total": len(extensions),
        }

    @friendly_errors("Adobe Launch")
    async def list_environments(
        self, client_id: str, client_secret: str, org_id: str, property_id: str
    ) -> dict:
        """
        List all environments for a property.
        GET /properties/{property_id}/environments
        """
        result = await self._request(
            client_id,
            client_secret,
            org_id,
            "GET",
            f"/properties/{property_id}/environments",
        )
        if result.get("error"):
            return result

        envs = result.get("data", [])
        return {
            "property_id": property_id,
            "environments": [
                {
                    "id": e.get("id"),
                    "name": e.get("attributes", {}).get("name"),
                    "archive_uri": e.get("attributes", {}).get("archive_uri"),
                }
                for e in envs
            ],
            "total": len(envs),
        }

    @friendly_errors("Adobe Launch")
    async def list_libraries(self, client_id: str, client_secret: str, org_id: str, property_id: str) -> dict:
        """
        List all libraries for a property.
        GET /properties/{property_id}/libraries
        """
        result = await self._request(
            client_id,
            client_secret,
            org_id,
            "GET",
            f"/properties/{property_id}/libraries",
        )
        if result.get("error"):
            return result

        libraries = result.get("data", [])
        return {
            "property_id": property_id,
            "libraries": [
                {
                    "id": lib.get("id"),
                    "name": lib.get("attributes", {}).get("name"),
                    "state": lib.get("attributes", {}).get("state"),
                }
                for lib in libraries
            ],
            "total": len(libraries),
        }

    @friendly_errors("Adobe Launch")
    async def list_builds(self, client_id: str, client_secret: str, org_id: str, library_id: str) -> dict:
        """
        List all builds for a library.
        GET /libraries/{library_id}/builds
        """
        result = await self._request(
            client_id, client_secret, org_id, "GET", f"/libraries/{library_id}/builds"
        )
        if result.get("error"):
            return result

        builds = result.get("data", [])
        return {
            "library_id": library_id,
            "builds": [
                {
                    "id": b.get("id"),
                    "status": b.get("attributes", {}).get("status"),
                    "created": b.get("attributes", {}).get("created_at"),
                }
                for b in builds
            ],
            "total": len(builds),
        }

    # ------------------------------------------------------------------
    # Layer 2: Audit
    # ------------------------------------------------------------------

    @friendly_errors("Adobe Launch")
    async def audit_property(self, client_id: str, client_secret: str, org_id: str, property_id: str) -> dict:
        """
        Audit a property: count rules, data elements, extensions; flag issues.
        """
        rules_result = await self.list_rules(client_id, client_secret, org_id, property_id)
        elements_result = await self.list_data_elements(client_id, client_secret, org_id, property_id)
        extensions_result = await self.list_extensions(client_id, client_secret, org_id, property_id)

        if any(r.get("error") for r in [rules_result, elements_result, extensions_result]):
            return {"error": True, "message": "Failed to audit property"}

        rules = rules_result.get("rules", [])
        elements = elements_result.get("data_elements", [])
        extensions = extensions_result.get("extensions", [])

        issues = []
        # Flag disabled rules
        disabled_rules = [r for r in rules if not r.get("enabled")]
        if disabled_rules:
            issues.append(f"Found {len(disabled_rules)} disabled rules")

        # Flag disabled elements
        disabled_elements = [e for e in elements if not e.get("enabled")]
        if disabled_elements:
            issues.append(f"Found {len(disabled_elements)} disabled data elements")

        return {
            "property_id": property_id,
            "rules_count": len(rules),
            "data_elements_count": len(elements),
            "extensions_count": len(extensions),
            "issues": issues,
            "health_score": max(0, 100 - len(issues) * 20),
        }

    @friendly_errors("Adobe Launch")
    async def get_publish_history(
        self, client_id: str, client_secret: str, org_id: str, property_id: str
    ) -> dict:
        """
        Get recent library builds (publish history) for a property.
        """
        libraries_result = await self.list_libraries(client_id, client_secret, org_id, property_id)
        if libraries_result.get("error"):
            return libraries_result

        libraries = libraries_result.get("libraries", [])

        # Get builds for each library
        all_builds = []
        for lib in libraries:
            builds_result = await self.list_builds(client_id, client_secret, org_id, lib.get("id"))
            if not builds_result.get("error"):
                for build in builds_result.get("builds", []):
                    build["library_name"] = lib.get("name")
                    all_builds.append(build)

        # Sort by created date descending and take top 10
        all_builds.sort(key=lambda b: b.get("created", ""), reverse=True)

        return {
            "property_id": property_id,
            "recent_builds": all_builds[:10],
            "total_builds": len(all_builds),
        }

    # ------------------------------------------------------------------
    # Layer 3: Write Operations
    # ------------------------------------------------------------------

    @friendly_errors("Adobe Launch")
    async def create_property(
        self,
        client_id: str,
        client_secret: str,
        org_id: str,
        company_id: str,
        name: str,
        platform: str = "web",
        domains: list[str] | None = None,
    ) -> dict:
        """
        Create a new property.
        POST /companies/{company_id}/properties
        """
        json_body = {
            "data": {
                "type": "properties",
                "attributes": {
                    "name": name,
                    "platform": platform,
                },
            }
        }
        if domains:
            json_body["data"]["attributes"]["domains"] = domains

        result = await self._request(
            client_id,
            client_secret,
            org_id,
            "POST",
            f"/companies/{company_id}/properties",
            json_body=json_body,
        )
        if result.get("error"):
            return result

        prop = result.get("data", {})
        return {
            "success": True,
            "property_id": prop.get("id"),
            "name": name,
            "message": "Property created successfully",
        }

    async def _resolve_extension_id(
        self,
        client_id: str,
        client_secret: str,
        org_id: str,
        property_id: str,
        delegate_descriptor_id: str | None = None,
        extension_id: str | None = None,
        extension_name: str | None = None,
    ) -> str | None:
        """
        Resolve an extension ID for a property.
        If extension_id is provided, returns it.
        Otherwise fetches installed extensions for the property and matches by name
        (from extension_name or delegate_descriptor_id package prefix, e.g. 'core' or 'adobe-analytics'),
        defaulting to the 'core' extension.
        """
        if extension_id:
            return extension_id

        target_name = extension_name
        if not target_name and delegate_descriptor_id and "::" in delegate_descriptor_id:
            target_name = delegate_descriptor_id.split("::", 1)[0].strip()

        ext_res = await self.list_extensions(client_id, client_secret, org_id, property_id)
        if ext_res.get("error"):
            return None

        extensions = ext_res.get("extensions", [])
        if not extensions:
            return None

        # 1. Match by target_name
        if target_name:
            target_clean = target_name.lower().replace("_", "-")
            for ext in extensions:
                ext_name = (ext.get("name") or "").lower().replace("_", "-")
                if ext_name == target_clean:
                    return ext.get("id")

        # 2. Match "core" extension
        for ext in extensions:
            if (ext.get("name") or "").lower() == "core":
                return ext.get("id")

        # 3. Fallback to first extension
        return extensions[0].get("id")

    @friendly_errors("Adobe Launch")
    async def create_rule(
        self,
        client_id: str,
        client_secret: str,
        org_id: str,
        property_id: str,
        name: str,
        components: list[dict[str, Any]] | None = None,
    ) -> dict:
        """
        Create a new rule, optionally attaching components (events, conditions, actions).
        POST /properties/{property_id}/rules
        """
        json_body = {
            "data": {
                "type": "rules",
                "attributes": {
                    "name": name,
                    "enabled": True,
                },
            }
        }

        result = await self._request(
            client_id,
            client_secret,
            org_id,
            "POST",
            f"/properties/{property_id}/rules",
            json_body=json_body,
        )
        if result.get("error"):
            return result

        rule = result.get("data", {})
        rule_id = rule.get("id")

        created_components = []
        if components and rule_id:
            for comp in components:
                comp_name = comp.get("name") or f"{name} Component"
                comp_desc = comp.get("delegate_descriptor_id")
                if not comp_desc:
                    continue
                comp_res = await self.create_rule_component(
                    client_id,
                    client_secret,
                    org_id,
                    property_id,
                    rule_id,
                    name=comp_name,
                    delegate_descriptor_id=comp_desc,
                    settings=comp.get("settings"),
                    extension_id=comp.get("extension_id"),
                    rule_order=comp.get("rule_order"),
                    order=comp.get("order"),
                    negate=comp.get("negate"),
                    timeout=comp.get("timeout"),
                    delay_next=comp.get("delay_next"),
                )
                if not comp_res.get("error"):
                    created_components.append(comp_res)
                else:
                    logger.warning(
                        f"Failed to create rule component {comp_name} for rule {rule_id}: {comp_res}"
                    )

        return {
            "success": True,
            "rule_id": rule_id,
            "name": name,
            "components_count": len(created_components),
            "components": created_components,
            "message": "Rule created successfully"
            + (f" with {len(created_components)} component(s)" if created_components else ""),
        }

    @friendly_errors("Adobe Launch")
    async def update_rule(
        self,
        client_id: str,
        client_secret: str,
        org_id: str,
        rule_id: str,
        name: str | None = None,
        enabled: bool | None = None,
    ) -> dict:
        """
        Update an existing rule.
        PATCH /rules/{rule_id}
        """
        attributes: dict[str, Any] = {}
        if name is not None:
            attributes["name"] = name
        if enabled is not None:
            attributes["enabled"] = enabled

        json_body = {
            "data": {
                "type": "rules",
                "id": rule_id,
                "attributes": attributes,
            }
        }

        result = await self._request(
            client_id,
            client_secret,
            org_id,
            "PATCH",
            f"/rules/{rule_id}",
            json_body=json_body,
        )
        if result.get("error"):
            return result

        rule = result.get("data", {})
        return {
            "success": True,
            "rule_id": rule_id,
            "name": rule.get("attributes", {}).get("name", name),
            "enabled": rule.get("attributes", {}).get("enabled", enabled),
            "message": "Rule updated successfully",
        }

    @friendly_errors("Adobe Launch")
    async def delete_rule(self, client_id: str, client_secret: str, org_id: str, rule_id: str) -> dict:
        """
        Delete a rule.
        DELETE /rules/{rule_id}
        """
        result = await self._request(client_id, client_secret, org_id, "DELETE", f"/rules/{rule_id}")
        if result.get("error"):
            return result

        return {
            "success": True,
            "rule_id": rule_id,
            "message": "Rule deleted successfully",
        }

    @friendly_errors("Adobe Launch")
    async def create_rule_component(
        self,
        client_id: str,
        client_secret: str,
        org_id: str,
        property_id: str,
        rule_id: str,
        name: str,
        delegate_descriptor_id: str,
        settings: dict[str, Any] | str | None = None,
        extension_id: str | None = None,
        rule_order: int | None = None,
        order: int | None = None,
        negate: bool | None = None,
        timeout: int | None = None,
        delay_next: bool | None = None,
    ) -> dict:
        """
        Create a rule component (event, condition, or action) and attach it to a rule.
        POST /properties/{property_id}/rule_components
        """
        ext_id = await self._resolve_extension_id(
            client_id,
            client_secret,
            org_id,
            property_id,
            delegate_descriptor_id=delegate_descriptor_id,
            extension_id=extension_id,
        )
        if not ext_id:
            return {
                "error": True,
                "message": f"Could not resolve extension for rule component on property '{property_id}'. Please ensure the extension is installed.",
            }

        if isinstance(settings, (dict, list)):
            settings_str = json.dumps(settings)
        elif settings is not None:
            settings_str = str(settings)
        else:
            settings_str = "{}"

        attributes: dict[str, Any] = {
            "name": name,
            "delegate_descriptor_id": delegate_descriptor_id,
            "settings": settings_str,
        }
        if rule_order is not None:
            attributes["rule_order"] = rule_order
        if order is not None:
            attributes["order"] = order
        if negate is not None:
            attributes["negate"] = negate
        if timeout is not None:
            attributes["timeout"] = timeout
        if delay_next is not None:
            attributes["delay_next"] = delay_next

        json_body = {
            "data": {
                "type": "rule_components",
                "attributes": attributes,
                "relationships": {
                    "rules": {
                        "data": [
                            {
                                "type": "rules",
                                "id": rule_id,
                            }
                        ]
                    },
                    "extension": {
                        "data": {
                            "type": "extensions",
                            "id": ext_id,
                        }
                    },
                },
            }
        }

        result = await self._request(
            client_id,
            client_secret,
            org_id,
            "POST",
            f"/properties/{property_id}/rule_components",
            json_body=json_body,
        )
        if result.get("error"):
            return result

        comp = result.get("data", {})
        return {
            "success": True,
            "rule_component_id": comp.get("id"),
            "rule_id": rule_id,
            "name": name,
            "delegate_descriptor_id": delegate_descriptor_id,
            "extension_id": ext_id,
            "message": "Rule component created successfully",
            "data": comp,
        }

    @friendly_errors("Adobe Launch")
    async def update_rule_component(
        self,
        client_id: str,
        client_secret: str,
        org_id: str,
        rule_component_id: str,
        name: str | None = None,
        settings: dict[str, Any] | str | None = None,
        delegate_descriptor_id: str | None = None,
        rule_order: int | None = None,
        order: int | None = None,
        negate: bool | None = None,
        timeout: int | None = None,
        delay_next: bool | None = None,
    ) -> dict:
        """
        Update an existing rule component.
        PATCH /rule_components/{rule_component_id}
        """
        attributes: dict[str, Any] = {}
        if name is not None:
            attributes["name"] = name
        if settings is not None:
            attributes["settings"] = (
                json.dumps(settings) if isinstance(settings, (dict, list)) else str(settings)
            )
        if delegate_descriptor_id is not None:
            attributes["delegate_descriptor_id"] = delegate_descriptor_id
        if rule_order is not None:
            attributes["rule_order"] = rule_order
        if order is not None:
            attributes["order"] = order
        if negate is not None:
            attributes["negate"] = negate
        if timeout is not None:
            attributes["timeout"] = timeout
        if delay_next is not None:
            attributes["delay_next"] = delay_next

        json_body = {
            "data": {
                "type": "rule_components",
                "id": rule_component_id,
                "attributes": attributes,
            }
        }

        result = await self._request(
            client_id,
            client_secret,
            org_id,
            "PATCH",
            f"/rule_components/{rule_component_id}",
            json_body=json_body,
        )
        if result.get("error"):
            return result

        comp = result.get("data", {})
        return {
            "success": True,
            "rule_component_id": rule_component_id,
            "name": comp.get("attributes", {}).get("name", name),
            "message": "Rule component updated successfully",
            "data": comp,
        }

    @friendly_errors("Adobe Launch")
    async def delete_rule_component(
        self, client_id: str, client_secret: str, org_id: str, rule_component_id: str
    ) -> dict:
        """
        Delete a rule component.
        DELETE /rule_components/{rule_component_id}
        """
        result = await self._request(
            client_id, client_secret, org_id, "DELETE", f"/rule_components/{rule_component_id}"
        )
        if result.get("error"):
            return result

        return {
            "success": True,
            "rule_component_id": rule_component_id,
            "message": "Rule component deleted successfully",
        }

    @friendly_errors("Adobe Launch")
    async def create_data_element(
        self,
        client_id: str,
        client_secret: str,
        org_id: str,
        property_id: str,
        name: str,
        delegate_descriptor_id: str,
        settings: dict[str, Any] | str | None = None,
        extension_id: str | None = None,
        clean_text: bool | None = None,
        force_lower_case: bool | None = None,
        default_value: str | None = None,
        storage_duration: str | None = None,
        enabled: bool = True,
    ) -> dict:
        """
        Create a new data element.
        POST /properties/{property_id}/data_elements
        """
        ext_id = await self._resolve_extension_id(
            client_id,
            client_secret,
            org_id,
            property_id,
            delegate_descriptor_id=delegate_descriptor_id,
            extension_id=extension_id,
        )
        if not ext_id:
            return {
                "error": True,
                "message": f"Could not resolve extension for data element on property '{property_id}'. Please ensure the extension is installed.",
            }

        # Reactor API requires settings to be a JSON string
        if isinstance(settings, (dict, list)):
            settings_str = json.dumps(settings)
        elif settings is not None:
            settings_str = str(settings)
        else:
            settings_str = "{}"

        attributes: dict[str, Any] = {
            "name": name,
            "delegate_descriptor_id": delegate_descriptor_id,
            "settings": settings_str,
            "enabled": True if enabled is None else enabled,
        }
        if clean_text is not None:
            attributes["clean_text"] = clean_text
        if force_lower_case is not None:
            attributes["force_lower_case"] = force_lower_case
        if default_value is not None:
            attributes["default_value"] = default_value
        if storage_duration is not None:
            attributes["storage_duration"] = storage_duration

        json_body = {
            "data": {
                "type": "data_elements",
                "attributes": attributes,
                "relationships": {
                    "extension": {
                        "data": {
                            "type": "extensions",
                            "id": ext_id,
                        }
                    }
                },
            }
        }

        result = await self._request(
            client_id,
            client_secret,
            org_id,
            "POST",
            f"/properties/{property_id}/data_elements",
            json_body=json_body,
        )
        if result.get("error"):
            return result

        element = result.get("data", {})
        return {
            "success": True,
            "data_element_id": element.get("id"),
            "name": name,
            "delegate_descriptor_id": delegate_descriptor_id,
            "extension_id": ext_id,
            "message": "Data element created successfully",
            "data": element,
        }

    @friendly_errors("Adobe Launch")
    async def update_data_element(
        self,
        client_id: str,
        client_secret: str,
        org_id: str,
        data_element_id: str,
        name: str | None = None,
        settings: dict[str, Any] | str | None = None,
        delegate_descriptor_id: str | None = None,
        enabled: bool | None = None,
        clean_text: bool | None = None,
        force_lower_case: bool | None = None,
        default_value: str | None = None,
        storage_duration: str | None = None,
    ) -> dict:
        """
        Update an existing data element.
        PATCH /data_elements/{data_element_id}
        """
        attributes: dict[str, Any] = {}
        if name is not None:
            attributes["name"] = name
        if settings is not None:
            attributes["settings"] = (
                json.dumps(settings) if isinstance(settings, (dict, list)) else str(settings)
            )
        if delegate_descriptor_id is not None:
            attributes["delegate_descriptor_id"] = delegate_descriptor_id
        if enabled is not None:
            attributes["enabled"] = enabled
        if clean_text is not None:
            attributes["clean_text"] = clean_text
        if force_lower_case is not None:
            attributes["force_lower_case"] = force_lower_case
        if default_value is not None:
            attributes["default_value"] = default_value
        if storage_duration is not None:
            attributes["storage_duration"] = storage_duration

        json_body = {
            "data": {
                "type": "data_elements",
                "id": data_element_id,
                "attributes": attributes,
            }
        }

        result = await self._request(
            client_id,
            client_secret,
            org_id,
            "PATCH",
            f"/data_elements/{data_element_id}",
            json_body=json_body,
        )
        if result.get("error"):
            return result

        element = result.get("data", {})
        return {
            "success": True,
            "data_element_id": data_element_id,
            "name": element.get("attributes", {}).get("name", name),
            "message": "Data element updated successfully",
            "data": element,
        }

    @friendly_errors("Adobe Launch")
    async def delete_data_element(
        self, client_id: str, client_secret: str, org_id: str, data_element_id: str
    ) -> dict:
        """
        Delete a data element.
        DELETE /data_elements/{data_element_id}
        """
        result = await self._request(
            client_id, client_secret, org_id, "DELETE", f"/data_elements/{data_element_id}"
        )
        if result.get("error"):
            return result

        return {
            "success": True,
            "data_element_id": data_element_id,
            "message": "Data element deleted successfully",
        }

    @friendly_errors("Adobe Launch")
    async def create_library(
        self,
        client_id: str,
        client_secret: str,
        org_id: str,
        property_id: str,
        name: str,
        environment_id: str,
    ) -> dict:
        """
        Create a new library.
        POST /properties/{property_id}/libraries
        """
        json_body = {
            "data": {
                "type": "libraries",
                "attributes": {
                    "name": name,
                },
                "relationships": {
                    "environment": {
                        "data": {
                            "type": "environments",
                            "id": environment_id,
                        }
                    }
                },
            }
        }

        result = await self._request(
            client_id,
            client_secret,
            org_id,
            "POST",
            f"/properties/{property_id}/libraries",
            json_body=json_body,
        )
        if result.get("error"):
            return result

        lib = result.get("data", {})
        return {
            "success": True,
            "library_id": lib.get("id"),
            "name": name,
            "message": "Library created successfully",
        }

    @friendly_errors("Adobe Launch")
    async def add_resources_to_library(
        self,
        client_id: str,
        client_secret: str,
        org_id: str,
        library_id: str,
        resources: list[dict[str, str]],
    ) -> dict:
        """
        Add resources (rules, data elements, extensions) to a library.
        POST /libraries/{library_id}/relationships/resources
        """
        # Published resources are frozen. PATCH the library with meta.action=revise
        # to create working copies (POST .../relationships/resources 404s on origin IDs).
        resource_data = []
        for r in resources:
            item: dict[str, Any] = {
                "type": r.get("type", "rules"),
                "id": r.get("id"),
            }
            meta = r.get("meta") if isinstance(r.get("meta"), dict) else {"action": "revise"}
            item["meta"] = meta
            resource_data.append(item)

        json_body = {
            "data": {
                "id": library_id,
                "type": "libraries",
                "relationships": {
                    "resources": {
                        "data": resource_data,
                    }
                },
            }
        }

        result = await self._request(
            client_id,
            client_secret,
            org_id,
            "PATCH",
            f"/libraries/{library_id}",
            json_body=json_body,
        )
        if result.get("error"):
            return result

        return {
            "success": True,
            "library_id": library_id,
            "resources_added": len(resources),
            "message": "Resources added to library successfully",
        }

    @friendly_errors("Adobe Launch")
    async def build_library(self, client_id: str, client_secret: str, org_id: str, library_id: str) -> dict:
        """
        Build (publish) a library.
        POST /libraries/{library_id}/builds
        """
        json_body = {
            "data": {
                "type": "builds",
            }
        }

        result = await self._request(
            client_id,
            client_secret,
            org_id,
            "POST",
            f"/libraries/{library_id}/builds",
            json_body=json_body,
        )
        if result.get("error"):
            return result

        build = result.get("data", {})
        return {
            "success": True,
            "build_id": build.get("id"),
            "status": build.get("attributes", {}).get("status"),
            "message": "Library build initiated successfully",
        }

    @friendly_errors("Adobe Launch")
    async def transition_library(
        self,
        client_id: str,
        client_secret: str,
        org_id: str,
        library_id: str,
        action: str,
    ) -> dict:
        """
        Transition a library state (submit, approve, reject, develop).
        PATCH /libraries/{library_id}
        """
        valid_actions = ["submit", "approve", "reject", "develop"]
        if action not in valid_actions:
            return {
                "error": True,
                "message": f"Invalid action. Must be one of: {', '.join(valid_actions)}",
            }

        json_body = {
            "data": {
                "type": "libraries",
                "id": library_id,
                "attributes": {
                    "state": action,
                },
            }
        }

        result = await self._request(
            client_id,
            client_secret,
            org_id,
            "PATCH",
            f"/libraries/{library_id}",
            json_body=json_body,
        )
        if result.get("error"):
            return result

        lib = result.get("data", {})
        return {
            "success": True,
            "library_id": library_id,
            "state": lib.get("attributes", {}).get("state"),
            "message": f"Library transitioned to {action}",
        }
