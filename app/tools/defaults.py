"""
Smart defaults & auto-detection helpers for MCP tools.

Reduces back-and-forth between Claude and tools by auto-resolving common
parameters when unambiguous:

  - property_id: if the user has exactly one GA4 property, use it
  - account_id: if exactly one ad account is connected for the platform
  - dataset_id: if the BQ project has a single dataset
  - date ranges: default to "last 30 days" if start/end omitted

Auto-detection is always "best-effort, non-destructive": if there is
ambiguity (multiple candidates), we return None and let the tool ask.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import app.app_state as state

# ---------------------------------------------------------------------------
# Date range defaults
# ---------------------------------------------------------------------------


def default_date_range(
    start: str | None,
    end: str | None,
    days: int = 30,
    fmt: str = "%Y-%m-%d",
) -> tuple[str, str]:
    """Return (start, end) — fill missing values with 'last N days' default.

    If both start and end are provided, pass them through untouched.
    If either is missing, compute a sensible default ending today.
    """
    today = datetime.utcnow().date()
    if not end:
        end = today.strftime(fmt)
    if not start:
        try:
            end_dt = datetime.strptime(end, fmt).date()
        except ValueError:
            end_dt = today
        start = (end_dt - timedelta(days=days)).strftime(fmt)
    return start, end


# ---------------------------------------------------------------------------
# Single-candidate auto-detection
# ---------------------------------------------------------------------------


def auto_ga4_property(property_id: str | None) -> str | None:
    """If property_id is omitted and the user has exactly one GA4 property,
    return it. Otherwise return the original value (possibly None)."""
    if property_id:
        return property_id
    user = state.current_user_ctx.get(None)
    if not user:
        return None
    props = getattr(user, "ga4_properties", None) or []
    if len(props) == 1:
        # Each entry is either an ORM row with .property_id or a dict
        first = props[0]
        return getattr(first, "property_id", None) or (
            first.get("property_id") if isinstance(first, dict) else None
        )
    return None


def auto_gtm_container(container_id: str | None) -> str | None:
    if container_id:
        return container_id
    user = state.current_user_ctx.get(None)
    if not user:
        return None
    conts = getattr(user, "gtm_containers", None) or []
    if len(conts) == 1:
        first = conts[0]
        return getattr(first, "container_id", None) or (
            first.get("container_id") if isinstance(first, dict) else None
        )
    return None


def auto_ads_account(account_id: str | None) -> str | None:
    if account_id:
        return account_id
    user = state.current_user_ctx.get(None)
    if not user:
        return None
    accts = getattr(user, "ads_accounts", None) or []
    if len(accts) == 1:
        first = accts[0]
        return getattr(first, "customer_id", None) or (
            first.get("customer_id") if isinstance(first, dict) else None
        )
    return None


async def auto_bq_dataset(
    project_id: str,
    dataset_id: str | None,
    service_account_encrypted: str,
) -> str | None:
    """If the BQ project has a single dataset, return it."""
    if dataset_id:
        return dataset_id
    try:
        result = await state.bq_connector.list_datasets(service_account_encrypted, project_id)
        datasets: list[Any] = result.get("datasets") or result.get("items") or []
        if len(datasets) == 1:
            first = datasets[0]
            return first.get("dataset_id") or first.get("id") or first.get("name")
    except Exception:
        pass
    return None
