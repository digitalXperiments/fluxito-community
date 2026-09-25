"""Resource scope: a tool may only touch the project's *linked* Google resources.

One Google login often sees many clients' GA4 properties, GTM containers and
Ads accounts. Discovery records them per project connection
(``ga4_properties`` / ``gtm_containers`` / ``google_ads_accounts``); owners
and admins choose which stay linked (``is_active``) on the Connections page.
The active project context carries only the linked ones.

``resource_violation`` checks the ids a tool call names against those lists:
a ``property_id`` / ``container_id`` / ``customer_id`` that isn't linked to the
active project is refused, so project A can't read or publish to client B's
container through A's connection. A resource type with no rows at all (e.g.
discovery failed) isn't enforced — there's nothing to compare against — and
Google itself still limits calls to what the connected account can reach.
"""

from __future__ import annotations

import re
from typing import Any

_DIGITS = re.compile(r"\D+")


def _digits(value: Any) -> str:
    return _DIGITS.sub("", str(value or ""))


def _platform(arguments: dict) -> str:
    return str(arguments.get("platform") or "").strip().lower()


def _collect(arguments: dict, key: str) -> list[str]:
    """Values of ``key`` at the top level or one level down (``params`` / ``config``)."""
    found: list[str] = []
    for scope in (arguments, arguments.get("params"), arguments.get("config"), arguments.get("args")):
        if isinstance(scope, dict) and scope.get(key) not in (None, ""):
            found.append(str(scope[key]))
    return found


def resource_violation(tool_name: str, arguments: dict | None, project_ctx: Any) -> str | None:
    """A user-facing reason when the call names a resource the project hasn't linked, else None."""
    if not isinstance(arguments, dict) or project_ctx is None:
        return None
    platform = _platform(arguments)
    name = tool_name or ""

    # GA4 properties
    if platform in ("", "ga4", "google_analytics") and not name.startswith(("marketing", "tagmanager")):
        linked = getattr(project_ctx, "ga4_properties", None) or []
        if linked:
            allowed = {_digits(p.get("property_id")) for p in linked}
            for value in _collect(arguments, "property_id"):
                if _digits(value) and _digits(value) not in allowed:
                    return f"GA4 property {value} isn't linked to this project."

    # GTM containers (numeric container id or GTM-XXXX public id)
    if name.startswith("tagmanager") or platform in ("gtm", "google_tag_manager"):
        linked = getattr(project_ctx, "gtm_containers", None) or []
        if linked:
            allowed = set()
            for c in linked:
                allowed.add(str(c.get("container_id") or "").strip())
                if c.get("public_id"):
                    allowed.add(str(c["public_id"]).strip().upper())
            for value in _collect(arguments, "container_id"):
                v = value.strip()
                if v and v not in allowed and v.upper() not in allowed:
                    return f"GTM container {value} isn't linked to this project."

    # Google Ads accounts
    if platform in ("", "google_ads", "ads") and not name.startswith(("tagmanager", "analytics")):
        linked = getattr(project_ctx, "ads_accounts", None) or []
        if linked:
            allowed = {_digits(a.get("customer_id")) for a in linked}
            for value in _collect(arguments, "customer_id"):
                if _digits(value) and _digits(value) not in allowed:
                    return f"Google Ads account {value} isn't linked to this project."
    return None


def denied_response(tool_name: str, reason: str) -> dict:
    return {
        "error": True,
        "error_type": "resource_not_linked",
        "message": (
            f"{reason} A project owner or admin can link it under Connections → Google → Resources "
            "(reconnect Google first if it's new)."
        ),
        "tool": tool_name,
    }
