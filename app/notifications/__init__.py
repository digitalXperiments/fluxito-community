"""
Notifications package.

This package covers two concerns:

1. **In-app notifications** — records persisted to the ``notifications`` table
   so the user can see status updates in the dashboard (e.g. "Google Suite
   Connected"). Helpers: :func:`create_notification`, :func:`get_unread_count`.

2. **Outbound delivery** — scheduled reports, test sends, and other
   system-generated messages delivered to external channels.
   Currently implemented:
     * Email (SMTP + Amazon SES, BYO credentials) — see ``app.notifications.email``.
   Planned:
     * Slack (incoming webhooks, Block Kit rendering) — see ``app.notifications.slack``.

   All senders are "bring your own credentials" — the platform never pays for
   delivery, so users provide their own SMTP / SES / Slack setup and we store
   the config (encrypted) per project.
"""

import logging
import uuid

from sqlalchemy import func, select

import app.app_state as app_state
from app.models.notification import Notification

logger = logging.getLogger(__name__)


async def create_notification(
    user_id: str | uuid.UUID,
    title: str,
    message: str,
    category: str = "system",
    severity: str = "info",
    action_url: str | None = None,
) -> Notification | None:
    """Create a notification for a user.

    Args:
        user_id: UUID of the target user
        title: Short title (e.g. "Google Suite Connected")
        message: Longer description
        category: One of: connection, dashboard, billing, system
        severity: One of: info, success, warning, error
        action_url: Optional URL to navigate to on click
    """
    try:
        async with app_state.db_session_factory() as db:
            notif = Notification(
                user_id=uuid.UUID(str(user_id)),
                title=title,
                message=message,
                category=category,
                severity=severity,
                action_url=action_url,
            )
            db.add(notif)
            await db.commit()
            await db.refresh(notif)
            return notif
    except Exception as e:
        logger.warning(f"Failed to create notification: {e}")
        return None


async def get_unread_count(user_id: str | uuid.UUID) -> int:
    """Get the count of unread notifications for a user."""
    try:
        async with app_state.db_session_factory() as db:
            result = await db.execute(
                select(func.count(Notification.id)).where(
                    Notification.user_id == uuid.UUID(str(user_id)),
                    Notification.is_read == False,
                )
            )
            return result.scalar() or 0
    except Exception:
        return 0


__all__ = ["create_notification", "get_unread_count"]
