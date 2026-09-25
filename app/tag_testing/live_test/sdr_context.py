"""
Live-tag-testing SDR context.

The community edition has no tracking plan, so there are never any expected
events: live tests audit what fires against the rule books alone. The
signature is kept so ``live_tag_test_tools`` and the ``get_sdr_context``
action keep working.
"""

from __future__ import annotations


async def get_sdr_context_for_url(
    project_id: str,
    url: str | None = None,
) -> dict:
    """Return the (always empty) expected-events context for ``url``."""
    return {
        "project_id": project_id,
        "url": url,
        "events": [],
        "total": 0,
        "error": None,
    }
