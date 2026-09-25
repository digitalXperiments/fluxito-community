"""
Slack webhook factory — resolve stored ``ProjectSlackWebhook`` rows into
ready-to-use ``WebhookSender`` instances.

Differences from the email factory:
  * There is no "default webhook" concept. Each row is its own distinct
    destination (you might send different reports to different channels).
    Callers pick a specific webhook by id or send to several in parallel.
  * The encrypted blob stores *only* the webhook URL — no display
    columns to merge. So ``build_webhook_sender_from_row`` is a
    straightforward decrypt + construct.

Public entry points:
  * ``build_webhook_sender_from_row(row)`` — decrypt and instantiate.
  * ``list_webhook_senders(db, project_id)`` — return all rows for a project,
    most-recent first. Used by the settings UI, schedule editor, and the
    scheduler worker.
  * ``webhook_display_summary(row)`` — redacted dict for settings UI;
    NEVER decrypts the blob.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification_channel import ProjectSlackWebhook
from app.notifications.slack.base import SlackSendError
from app.notifications.slack.webhook_sender import WebhookSender

logger = logging.getLogger(__name__)


def build_webhook_sender_from_row(row: ProjectSlackWebhook) -> WebhookSender:
    """Decrypt the row's stored webhook URL and return a ``WebhookSender``.

    Raises:
      SlackSendError — if the URL can't be decrypted or is malformed.
    """
    try:
        url = row.get_webhook_url() or ""
    except Exception as exc:
        logger.exception("Failed to decrypt Slack webhook id=%s", row.id)
        raise SlackSendError(
            "Stored Slack webhook URL could not be decrypted",
            detail="Re-save the webhook in Project Settings → Notifications.",
        ) from exc

    return WebhookSender({"webhook_url": url})


async def list_webhook_senders(
    db: AsyncSession,
    project_id: uuid.UUID,
) -> list[ProjectSlackWebhook]:
    """Return all webhook rows for a project, most-recent first.

    Ordering matches the settings UI: newest at top. Callers that care
    about ordering for delivery should sort the result themselves.
    """
    result = await db.execute(
        select(ProjectSlackWebhook)
        .where(ProjectSlackWebhook.project_id == project_id)
        .order_by(ProjectSlackWebhook.created_at.desc())
    )
    return list(result.scalars().all())


def webhook_display_summary(row: ProjectSlackWebhook) -> dict[str, Any]:
    """Safe, redacted summary of a saved webhook for settings UI rendering.

    NEVER decrypts the URL — the settings page doesn't need it. If we
    later want to show the last 4 characters of the URL (for disambiguating
    multiple channels), add a column that stores a hash or suffix at write
    time; don't decrypt here.
    """
    return {
        "id": str(row.id),
        "label": row.label or "",
        "last_tested_at": row.last_tested_at.isoformat() if row.last_tested_at else None,
        "last_test_status": row.last_test_status or None,
        "last_test_error": row.last_test_error or "",
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
