"""
GTM Connector

Wraps google-api-python-client Tag Manager API v2.
Provides all read methods (Layer 1) and write methods (Layer 3).

All .execute() calls run in a thread pool via run_sync() to avoid
blocking the asyncio event loop (googleapiclient uses synchronous HTTP).
"""

import httplib2
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build

from app.connectors.base import BaseConnector
from app.connectors.errors import friendly_errors

_GTM_HTTP_TIMEOUT = 30  # seconds per API call


class GTMConnector(BaseConnector):
    def _build_service(self, access_token: str):
        creds = Credentials(token=access_token)
        authed_http = AuthorizedHttp(creds, http=httplib2.Http(timeout=_GTM_HTTP_TIMEOUT))
        return build("tagmanager", "v2", http=authed_http, cache_discovery=False)

    async def _exec(self, request):
        """Execute a Google API request in a thread pool to avoid blocking."""
        return await self.run_sync(request.execute)

    # ------------------------------------------------------------------
    # Discovery helpers (used during OAuth callback)
    # ------------------------------------------------------------------

    @friendly_errors("GTM")
    async def list_all_containers_raw(self, access_token: str) -> list:
        import asyncio

        service = self._build_service(access_token)
        containers = []
        try:
            accounts_resp = await self._exec(service.accounts().list())
            accounts = accounts_resp.get("account", [])
            if not accounts:
                return containers

            # Fetch containers for all accounts in parallel
            tasks = []
            account_ids = []
            for account in accounts:
                account_id = account["accountId"]
                account_ids.append(account_id)
                req = service.accounts().containers().list(parent=f"accounts/{account_id}")
                tasks.append(self._exec(req))

            results = await asyncio.gather(*tasks, return_exceptions=True)
            for account_id, resp in zip(account_ids, results, strict=False):
                if isinstance(resp, Exception):
                    continue
                for c in resp.get("container", []):
                    c["accountId"] = account_id
                    containers.append(c)
        except Exception:
            pass
        return containers

    # ------------------------------------------------------------------
    # Layer 1: Data Access
    # ------------------------------------------------------------------

    @friendly_errors("GTM")
    async def list_accounts(self, connection_id: str) -> dict:
        """Enumerate all GTM accounts visible to this connection."""
        token = await self.get_token(connection_id)
        service = self._build_service(token)
        try:
            resp = await self._exec(service.accounts().list())
        except Exception as exc:
            return {"error": True, "message": f"Failed to list GTM accounts: {exc}"}
        return {
            "accounts": [
                {
                    "account_id": str(a.get("accountId", "")),
                    "name": a.get("name"),
                    "path": a.get("path"),
                    "share_data": a.get("shareData", False),
                    "fingerprint": a.get("fingerprint"),
                }
                for a in resp.get("account", [])
            ]
        }

    @friendly_errors("GTM")
    async def list_containers(self, connection_id: str, account_id: str | None = None) -> dict:
        token = await self.get_token(connection_id)
        if account_id:
            service = self._build_service(token)
            resp = await self._exec(service.accounts().containers().list(parent=f"accounts/{account_id}"))
            containers = []
            for c in resp.get("container", []):
                c.setdefault("accountId", account_id)
                containers.append(c)
        else:
            containers = await self.list_all_containers_raw(token)
        return {
            "containers": [
                {
                    "account_id": str(c.get("accountId", "")),
                    "container_id": str(c.get("containerId", "")),
                    "container_name": c.get("name"),
                    "public_id": c.get("publicId"),
                    "usage_context": c.get("usageContext", []),
                }
                for c in containers
            ]
        }

    @friendly_errors("GTM")
    async def get_container_summary(self, connection_id: str, account_id: str, container_id: str) -> dict:
        import asyncio

        token = await self.get_token(connection_id)
        service = self._build_service(token)
        parent = f"accounts/{account_id}/containers/{container_id}"

        # Fetch tags, triggers, variables, workspaces, and versions in parallel to prevent gateway timeouts
        tags_task = self._exec(
            service.accounts().containers().workspaces().tags().list(parent=f"{parent}/workspaces/0")
        )
        triggers_task = self._exec(
            service.accounts().containers().workspaces().triggers().list(parent=f"{parent}/workspaces/0")
        )
        variables_task = self._exec(
            service.accounts().containers().workspaces().variables().list(parent=f"{parent}/workspaces/0")
        )
        workspaces_task = self._exec(service.accounts().containers().workspaces().list(parent=parent))
        versions_task = self._exec(service.accounts().containers().version_headers().list(parent=parent))

        tags_resp, triggers_resp, variables_resp, workspaces_resp, versions_resp = await asyncio.gather(
            tags_task, triggers_task, variables_task, workspaces_task, versions_task, return_exceptions=True
        )

        tags = tags_resp.get("tag", []) if not isinstance(tags_resp, Exception) else []
        triggers = triggers_resp.get("trigger", []) if not isinstance(triggers_resp, Exception) else []
        variables = variables_resp.get("variable", []) if not isinstance(variables_resp, Exception) else []
        workspaces = (
            workspaces_resp.get("workspace", []) if not isinstance(workspaces_resp, Exception) else []
        )
        versions = (
            versions_resp.get("containerVersionHeader", [])
            if not isinstance(versions_resp, Exception)
            else []
        )
        latest_version = versions[0] if versions else None

        return {
            "container_id": container_id,
            "account_id": account_id,
            "tag_count": len(tags),
            "trigger_count": len(triggers),
            "variable_count": len(variables),
            "workspace_count": len(workspaces),
            "last_published_at": latest_version.get("timeStamp") if latest_version else None,
            "last_published_by": latest_version.get("path") if latest_version else None,
        }

    @friendly_errors("GTM")
    async def list_tags(
        self, connection_id: str, account_id: str, container_id: str, workspace_id: str = "0"
    ) -> dict:
        token = await self.get_token(connection_id)
        service = self._build_service(token)
        parent = f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}"
        tags = (
            await self._exec(service.accounts().containers().workspaces().tags().list(parent=parent))
        ).get("tag", [])

        # Get trigger names for mapping
        triggers_resp = await self._exec(
            service.accounts().containers().workspaces().triggers().list(parent=parent)
        )
        trigger_map = {t["triggerId"]: t.get("name", "") for t in triggers_resp.get("trigger", [])}

        return {
            "tags": [
                {
                    "tag_id": t.get("tagId"),
                    "tag_name": t.get("name"),
                    "tag_type": t.get("type"),
                    "firing_triggers": [trigger_map.get(tid, tid) for tid in t.get("firingTriggerId", [])],
                    "blocking_triggers": [
                        trigger_map.get(tid, tid) for tid in t.get("blockingTriggerId", [])
                    ],
                    "is_paused": t.get("paused", False),
                    "notes": t.get("notes"),
                }
                for t in tags
            ]
        }

    @friendly_errors("GTM")
    async def list_triggers(
        self, connection_id: str, account_id: str, container_id: str, workspace_id: str = "0"
    ) -> dict:
        token = await self.get_token(connection_id)
        service = self._build_service(token)
        parent = f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}"
        triggers = (
            await self._exec(service.accounts().containers().workspaces().triggers().list(parent=parent))
        ).get("trigger", [])

        # Get tags to map trigger→tags
        tags_resp = await self._exec(service.accounts().containers().workspaces().tags().list(parent=parent))
        trigger_to_tags: dict = {}
        for tag in tags_resp.get("tag", []):
            for tid in tag.get("firingTriggerId", []):
                trigger_to_tags.setdefault(tid, []).append(tag.get("name", ""))

        return {
            "triggers": [
                {
                    "trigger_id": t.get("triggerId"),
                    "trigger_name": t.get("name"),
                    "trigger_type": t.get("type"),
                    "filters": t.get("filter", []),
                    "tags_using_trigger": trigger_to_tags.get(t.get("triggerId", ""), []),
                }
                for t in triggers
            ]
        }

    @friendly_errors("GTM")
    async def list_variables(
        self, connection_id: str, account_id: str, container_id: str, workspace_id: str = "0"
    ) -> dict:
        token = await self.get_token(connection_id)
        service = self._build_service(token)
        parent = f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}"
        variables = (
            await self._exec(service.accounts().containers().workspaces().variables().list(parent=parent))
        ).get("variable", [])

        # Map variable name → tags using it (look through tag parameters)
        tags_resp = await self._exec(service.accounts().containers().workspaces().tags().list(parent=parent))
        var_to_tags: dict = {}
        for tag in tags_resp.get("tag", []):
            tag_name = tag.get("name", "")
            for param in tag.get("parameter", []):
                val = str(param.get("value", ""))
                if "{{" in val:
                    import re

                    for vname in re.findall(r"\{\{(.+?)\}\}", val):
                        var_to_tags.setdefault(vname, []).append(tag_name)

        return {
            "variables": [
                {
                    "variable_id": v.get("variableId"),
                    "variable_name": v.get("name"),
                    "variable_type": v.get("type"),
                    "parameters": v.get("parameter", []),
                    "tags_using_variable": var_to_tags.get(v.get("name", ""), []),
                }
                for v in variables
            ]
        }

    @friendly_errors("GTM")
    async def get_tag_detail(
        self, connection_id: str, account_id: str, container_id: str, tag_id: str, workspace_id: str = "0"
    ) -> dict:
        token = await self.get_token(connection_id)
        service = self._build_service(token)
        path = f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}/tags/{tag_id}"
        return await self._exec(service.accounts().containers().workspaces().tags().get(path=path))

    @friendly_errors("GTM")
    async def list_workspaces(self, connection_id: str, account_id: str, container_id: str) -> dict:
        token = await self.get_token(connection_id)
        service = self._build_service(token)
        parent = f"accounts/{account_id}/containers/{container_id}"
        workspaces = (await self._exec(service.accounts().containers().workspaces().list(parent=parent))).get(
            "workspace", []
        )
        return {
            "workspaces": [
                {
                    "workspace_id": w.get("workspaceId"),
                    "name": w.get("name"),
                    "description": w.get("description"),
                    "created_by": w.get("fingerprint"),
                    "created_at": None,
                }
                for w in workspaces
            ]
        }

    # ------------------------------------------------------------------
    # Layer 2: Audit helpers
    # ------------------------------------------------------------------

    @friendly_errors("GTM")
    async def get_publish_history(self, access_token: str, account_id: str, container_id: str) -> list:
        service = self._build_service(access_token)
        parent = f"accounts/{account_id}/containers/{container_id}"
        versions = await self._exec(service.accounts().containers().version_headers().list(parent=parent))
        return versions.get("containerVersionHeader", [])

    @friendly_errors("GTM")
    async def get_publish_history_by_conn(
        self, connection_id: str, account_id: str, container_id: str
    ) -> list:
        """Same as get_publish_history but resolves token from connection_id."""
        token = await self.get_token(connection_id)
        return await self.get_publish_history(token, account_id, container_id)

    @friendly_errors("GTM")
    async def get_version_detail(
        self, connection_id: str, account_id: str, container_id: str, version_id: str
    ) -> dict:
        """Fetch full version detail including tags, triggers, variables."""
        token = await self.get_token(connection_id)
        service = self._build_service(token)
        path = f"accounts/{account_id}/containers/{container_id}/versions/{version_id}"
        return await self._exec(service.accounts().containers().versions().get(path=path))

    @friendly_errors("GTM")
    async def get_live_version(self, connection_id: str, account_id: str, container_id: str) -> dict:
        """Fetch the currently live (published) container version."""
        token = await self.get_token(connection_id)
        service = self._build_service(token)
        parent = f"accounts/{account_id}/containers/{container_id}"
        return await self._exec(service.accounts().containers().versions().live(parent=parent))

    # ------------------------------------------------------------------
    # Layer 3: Write operations
    # ------------------------------------------------------------------

    @friendly_errors("GTM")
    async def create_workspace(
        self, connection_id: str, account_id: str, container_id: str, name: str, description: str = ""
    ) -> dict:
        token = await self.get_token(connection_id)
        service = self._build_service(token)
        parent = f"accounts/{account_id}/containers/{container_id}"
        body = {"name": name, "description": description}
        ws = await self._exec(service.accounts().containers().workspaces().create(parent=parent, body=body))
        return {
            "workspace_id": ws.get("workspaceId"),
            "name": ws.get("name"),
            "gtm_url": ws.get("tagManagerUrl"),
        }

    @friendly_errors("GTM")
    async def create_tag(
        self,
        connection_id: str,
        account_id: str,
        container_id: str,
        workspace_id: str,
        tag_name: str,
        tag_type: str,
        parameters: list,
        firing_trigger_ids: list,
        blocking_trigger_ids: list | None = None,
        notes: str | None = None,
    ) -> dict:
        token = await self.get_token(connection_id)
        service = self._build_service(token)
        parent = f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}"
        body = {
            "name": tag_name,
            "type": tag_type,
            "parameter": parameters,
            "firingTriggerId": firing_trigger_ids,
            "blockingTriggerId": blocking_trigger_ids or [],
            "notes": notes or "",
        }
        tag = await self._exec(
            service.accounts().containers().workspaces().tags().create(parent=parent, body=body)
        )
        return {
            "tag_id": tag.get("tagId"),
            "tag_name": tag.get("name"),
            "tag_type": tag.get("type"),
            "workspace_id": workspace_id,
        }

    @friendly_errors("GTM")
    async def update_tag(
        self,
        connection_id: str,
        account_id: str,
        container_id: str,
        workspace_id: str,
        tag_id: str,
        updates: dict,
    ) -> dict:
        token = await self.get_token(connection_id)
        service = self._build_service(token)
        path = f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}/tags/{tag_id}"
        tag = await self._exec(service.accounts().containers().workspaces().tags().get(path=path))
        tag.update(updates)
        result = await self._exec(
            service.accounts().containers().workspaces().tags().update(path=path, body=tag)
        )
        return {"tag_id": result.get("tagId"), "tag_name": result.get("name"), "workspace_id": workspace_id}

    @friendly_errors("GTM")
    async def delete_tag(
        self, connection_id: str, account_id: str, container_id: str, workspace_id: str, tag_id: str
    ) -> dict:
        token = await self.get_token(connection_id)
        service = self._build_service(token)
        path = f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}/tags/{tag_id}"
        tag = await self._exec(service.accounts().containers().workspaces().tags().get(path=path))
        tag_name = tag.get("name")
        await self._exec(service.accounts().containers().workspaces().tags().delete(path=path))
        return {"deleted": True, "tag_name": tag_name, "workspace_id": workspace_id}

    @friendly_errors("GTM")
    async def create_trigger(
        self,
        connection_id: str,
        account_id: str,
        container_id: str,
        workspace_id: str,
        trigger_name: str,
        trigger_type: str,
        filters: list | None = None,
        custom_event_filter: list | None = None,
    ) -> dict:
        token = await self.get_token(connection_id)
        service = self._build_service(token)
        parent = f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}"
        body = {"name": trigger_name, "type": trigger_type, "filter": filters or []}
        if custom_event_filter:
            body["customEventFilter"] = custom_event_filter
        result = await self._exec(
            service.accounts().containers().workspaces().triggers().create(parent=parent, body=body)
        )
        return {
            "trigger_id": result.get("triggerId"),
            "trigger_name": result.get("name"),
            "trigger_type": result.get("type"),
            "workspace_id": workspace_id,
        }

    @friendly_errors("GTM")
    async def update_trigger(
        self,
        connection_id: str,
        account_id: str,
        container_id: str,
        workspace_id: str,
        trigger_id: str,
        updates: dict,
    ) -> dict:
        token = await self.get_token(connection_id)
        service = self._build_service(token)
        path = (
            f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}/triggers/{trigger_id}"
        )
        trigger = await self._exec(service.accounts().containers().workspaces().triggers().get(path=path))
        trigger.update(updates)
        result = await self._exec(
            service.accounts().containers().workspaces().triggers().update(path=path, body=trigger)
        )
        return {"trigger_id": result.get("triggerId"), "trigger_name": result.get("name")}

    @friendly_errors("GTM")
    async def delete_trigger(
        self, connection_id: str, account_id: str, container_id: str, workspace_id: str, trigger_id: str
    ) -> dict:
        token = await self.get_token(connection_id)
        service = self._build_service(token)
        path = (
            f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}/triggers/{trigger_id}"
        )
        trigger = await self._exec(service.accounts().containers().workspaces().triggers().get(path=path))
        trigger_name = trigger.get("name")
        await self._exec(service.accounts().containers().workspaces().triggers().delete(path=path))
        return {"deleted": True, "trigger_name": trigger_name, "workspace_id": workspace_id}

    @friendly_errors("GTM")
    async def create_variable(
        self,
        connection_id: str,
        account_id: str,
        container_id: str,
        workspace_id: str,
        variable_name: str,
        variable_type: str,
        parameters: list,
        notes: str | None = None,
    ) -> dict:
        token = await self.get_token(connection_id)
        service = self._build_service(token)
        parent = f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}"
        body = {"name": variable_name, "type": variable_type, "parameter": parameters, "notes": notes or ""}
        result = await self._exec(
            service.accounts().containers().workspaces().variables().create(parent=parent, body=body)
        )
        return {
            "variable_id": result.get("variableId"),
            "variable_name": result.get("name"),
            "variable_type": result.get("type"),
            "workspace_id": workspace_id,
        }

    @friendly_errors("GTM")
    async def update_variable(
        self,
        connection_id: str,
        account_id: str,
        container_id: str,
        workspace_id: str,
        variable_id: str,
        updates: dict,
    ) -> dict:
        token = await self.get_token(connection_id)
        service = self._build_service(token)
        path = f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}/variables/{variable_id}"
        variable = await self._exec(service.accounts().containers().workspaces().variables().get(path=path))
        variable.update(updates)
        result = await self._exec(
            service.accounts().containers().workspaces().variables().update(path=path, body=variable)
        )
        return {"variable_id": result.get("variableId"), "variable_name": result.get("name")}

    @friendly_errors("GTM")
    async def delete_variable(
        self, connection_id: str, account_id: str, container_id: str, workspace_id: str, variable_id: str
    ) -> dict:
        token = await self.get_token(connection_id)
        service = self._build_service(token)
        path = f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}/variables/{variable_id}"
        variable = await self._exec(service.accounts().containers().workspaces().variables().get(path=path))
        variable_name = variable.get("name")
        await self._exec(service.accounts().containers().workspaces().variables().delete(path=path))
        return {"deleted": True, "variable_name": variable_name}

    @friendly_errors("GTM")
    async def get_workspace_changes(
        self, connection_id: str, account_id: str, container_id: str, workspace_id: str
    ) -> dict:
        token = await self.get_token(connection_id)
        service = self._build_service(token)
        path = f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}"
        status = await self._exec(service.accounts().containers().workspaces().getStatus(path=path))

        changes = []
        for item in status.get("workspaceChange", []):
            entity = item.get("tag") or item.get("trigger") or item.get("variable") or {}
            entity_type = "tag" if "tag" in item else ("trigger" if "trigger" in item else "variable")
            changes.append(
                {
                    "change_type": item.get("changeStatus", "").lower(),
                    "entity_type": entity_type,
                    "entity_name": entity.get("name", "unknown"),
                    "change_summary": f"{item.get('changeStatus', '').title()} {entity_type}: {entity.get('name', 'unknown')}",
                }
            )

        ws = await self._exec(service.accounts().containers().workspaces().get(path=path))
        return {
            "workspace_name": ws.get("name"),
            "workspace_id": workspace_id,
            "changes": changes,
            "total_changes": len(changes),
        }

    @friendly_errors("GTM")
    async def revert_workspace(
        self, connection_id: str, account_id: str, container_id: str, workspace_id: str
    ) -> dict:
        token = await self.get_token(connection_id)
        service = self._build_service(token)
        path = f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}"
        ws = await self._exec(service.accounts().containers().workspaces().get(path=path))
        ws_name = ws.get("name")
        await self._exec(service.accounts().containers().workspaces().delete(path=path))
        return {"reverted": True, "workspace_name": ws_name}

    @friendly_errors("GTM")
    async def publish_container(
        self,
        connection_id: str,
        account_id: str,
        container_id: str,
        workspace_id: str,
        version_name: str | None = None,
        version_notes: str | None = None,
    ) -> dict:
        token = await self.get_token(connection_id)
        service = self._build_service(token)
        path = f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}"
        body = {}
        if version_name:
            body["name"] = version_name
        if version_notes:
            body["notes"] = version_notes

        result = await self._exec(
            service.accounts().containers().workspaces().create_version(path=path, body=body)
        )
        container_version = result.get("containerVersion", {})

        # Now publish the version
        version_path = container_version.get("path", "")
        if version_path:
            pub = await self._exec(service.accounts().containers().versions().publish(path=version_path))
            version_id = pub.get("containerVersion", {}).get("containerVersionId")
        else:
            version_id = None

        return {
            "version_id": version_id,
            "version_name": version_name,
            "published_at": None,
            "warning": "Changes are now live on your website",
        }
