"""Google Data Manager API connector.

The Data Manager API calls customer audiences ``UserList`` resources.  This
connector deliberately uses the REST API instead of a generated client: the
API is new and the REST discovery document is the stable public contract.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from app.connectors.base import BaseConnector
from app.connectors.errors import friendly_errors


class DataManagerConnector(BaseConnector):
    """CRUD for Data Manager audiences and audience-member ingestion."""

    _BASE_URL = "https://datamanager.googleapis.com/v1"
    _TIMEOUT = 30.0

    async def _request(
        self,
        access_token: str,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._TIMEOUT) as client:
            response = await client.request(
                method,
                f"{self._BASE_URL}/{path.lstrip('/')}",
                headers={"Authorization": f"Bearer {access_token}"},
                params=params,
                json=json_body,
            )
        if response.status_code >= 400:
            # Include Google's structured error in the exception.  The shared
            # friendly-errors decorator maps the status to a safe user message.
            detail = response.text[:1000]
            raise RuntimeError(f"Data Manager API returned HTTP {response.status_code}: {detail}")
        if not response.content:
            return {}
        return response.json()

    @staticmethod
    def _resource_path(name: str) -> str:
        """Quote a resource name while retaining its slash separators."""
        return quote(name.strip().strip("/"), safe="/")

    @friendly_errors("Google Data Manager")
    async def list_audiences(
        self,
        connection_id: str,
        parent: str,
        *,
        page_size: int = 100,
        page_token: str | None = None,
        filter_expression: str | None = None,
    ) -> dict[str, Any]:
        token = await self.get_token(connection_id)
        params: dict[str, Any] = {"pageSize": max(1, min(int(page_size), 1000))}
        if page_token:
            params["pageToken"] = page_token
        if filter_expression:
            params["filter"] = filter_expression
        return await self._request(token, "GET", f"{self._resource_path(parent)}/userLists", params=params)

    @friendly_errors("Google Data Manager")
    async def get_audience(self, connection_id: str, name: str) -> dict[str, Any]:
        token = await self.get_token(connection_id)
        return await self._request(token, "GET", self._resource_path(name))

    @friendly_errors("Google Data Manager")
    async def create_audience(
        self, connection_id: str, parent: str, audience: dict[str, Any]
    ) -> dict[str, Any]:
        token = await self.get_token(connection_id)
        return await self._request(
            token,
            "POST",
            f"{self._resource_path(parent)}/userLists",
            json_body=audience,
        )

    @friendly_errors("Google Data Manager")
    async def update_audience(
        self,
        connection_id: str,
        name: str,
        audience: dict[str, Any],
        update_mask: str | None = None,
    ) -> dict[str, Any]:
        token = await self.get_token(connection_id)
        params = {"updateMask": update_mask} if update_mask else None
        return await self._request(
            token,
            "PATCH",
            self._resource_path(name),
            params=params,
            json_body=audience,
        )

    @friendly_errors("Google Data Manager")
    async def delete_audience(self, connection_id: str, name: str) -> dict[str, Any]:
        token = await self.get_token(connection_id)
        return await self._request(token, "DELETE", self._resource_path(name))

    @friendly_errors("Google Data Manager")
    async def ingest_audience_members(self, connection_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        token = await self.get_token(connection_id)
        return await self._request(token, "POST", "audienceMembers:ingest", json_body=payload)

    @friendly_errors("Google Data Manager")
    async def remove_audience_members(self, connection_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        token = await self.get_token(connection_id)
        return await self._request(token, "POST", "audienceMembers:remove", json_body=payload)

    @friendly_errors("Google Data Manager")
    async def remove_all_audience_members(
        self, connection_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        token = await self.get_token(connection_id)
        return await self._request(token, "POST", "audienceMembers:removeAll", json_body=payload)

    @friendly_errors("Google Data Manager")
    async def get_request_status(self, connection_id: str, request_id: str) -> dict[str, Any]:
        token = await self.get_token(connection_id)
        return await self._request(
            token,
            "GET",
            "requestStatus:retrieve",
            params={"requestId": request_id},
        )
