"""
Email sender factory — build a concrete ``EmailSender`` from a stored
``ProjectEmailSender`` row (or from a raw decrypted config dict).

Public entry points:

  * ``build_sender_from_config(sender_type, config)`` — given a ``type``
    string and a fully-merged config dict (credentials + from_email +
    from_name + reply_to), return a concrete sender. Used by the "test
    connection" endpoint to construct a sender from in-memory form data
    before anything is persisted.

  * ``build_sender_from_row(row)`` — merge an already-loaded
    ``ProjectEmailSender`` row's encrypted blob with its ``from_address``
    / ``from_name`` / ``reply_to`` columns and return a concrete sender.

  * ``build_sender_for_project(db, project_id)`` — resolve the project's
    default ``ProjectEmailSender`` row and delegate to
    ``build_sender_from_row``. Used by the scheduled-report worker.

  * ``sender_display_summary(row)`` — redacted dict for settings UI
    rendering. Never decrypts the blob.

Callers only ever see the ``EmailSender`` protocol — they don't branch
on ``type`` themselves.

IMPORTANT — config-merge rule:
  The encrypted blob stores only credentials (host/port/user/pass/tls
  for SMTP; region/access_key/secret_key for SES). Display-level fields
  like ``from_address`` live in columns on the row so the settings UI
  can list them without decrypting. The factory merges the two dicts
  before calling the sender constructor.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification_channel import ProjectEmailSender
from app.notifications.email.base import EmailSender, EmailSendError
from app.notifications.email.ses_sender import SESSender
from app.notifications.email.smtp_sender import SMTPSender

logger = logging.getLogger(__name__)


class UnsupportedSenderType(EmailSendError):
    """Raised when ``sender_type`` doesn't map to a known concrete class."""


class NoDefaultSender(EmailSendError):
    """Raised by ``build_sender_for_project`` when the project has no default
    email sender configured. Call sites catch this and surface a UI error
    like "no default email sender — configure one in Project Settings"."""


# Registry keeps the factory declarative — add a new sender by appending
# one line here.
_SENDER_REGISTRY: dict[str, type[EmailSender]] = {
    "smtp": SMTPSender,
    "ses": SESSender,
}


def supported_sender_types() -> list[str]:
    """Return the list of sender type strings the UI may offer."""
    return sorted(_SENDER_REGISTRY.keys())


def build_sender_from_config(sender_type: str, config: dict[str, Any]) -> EmailSender:
    """Construct an ``EmailSender`` from a **fully-merged** config dict.

    The caller must pass a dict that already contains both credential
    fields AND display fields (``from_email``, optional ``from_name``,
    optional ``reply_to``). For stored senders, prefer
    ``build_sender_from_row`` which does the merge for you.

    Raises:
      UnsupportedSenderType — if ``sender_type`` isn't registered.
      EmailSendError        — from the concrete sender's constructor if
                              the config is malformed.
    """
    cls = _SENDER_REGISTRY.get((sender_type or "").strip().lower())
    if cls is None:
        raise UnsupportedSenderType(
            f"Unknown email sender type {sender_type!r}",
            detail=f"supported: {', '.join(supported_sender_types())}",
        )
    # Concrete __init__ raises EmailSendError for bad configs; let that
    # bubble up verbatim.
    return cls(config)


def build_sender_from_row(row: ProjectEmailSender) -> EmailSender:
    """Merge the row's encrypted blob with its display columns and construct.

    Raises:
      EmailSendError — if the blob can't be decrypted or the merged
                       config is malformed.
    """
    try:
        blob = row.get_config() or {}
    except Exception as exc:
        logger.exception("Failed to decrypt email sender config id=%s", row.id)
        raise EmailSendError(
            "Stored email sender credentials could not be decrypted",
            detail="Re-save credentials in Project Settings → Notifications.",
        ) from exc

    if not isinstance(blob, dict):
        raise EmailSendError(
            "Stored email sender credentials have an invalid shape",
            detail=f"expected dict, got {type(blob).__name__}",
        )

    merged: dict[str, Any] = {
        **blob,
        "from_email": row.from_address,
        "from_name": row.from_name or "",
    }
    return build_sender_from_config(row.type, merged)


async def build_sender_for_project(
    db: AsyncSession,
    project_id: uuid.UUID,
) -> EmailSender:
    """Load the project's default sender and return a ready-to-use instance.

    Resolution rules:
      * Must be ``is_default = True`` and belong to the given project.
      * If no row matches, ``NoDefaultSender`` is raised.
      * If more than one row matches (shouldn't happen — partial unique
        index in migration 024 prevents it — but defence-in-depth), the
        first is used and a warning is logged.
    """
    result = await db.execute(
        select(ProjectEmailSender).where(
            ProjectEmailSender.project_id == project_id,
            ProjectEmailSender.is_default.is_(True),
        )
    )
    rows = list(result.scalars().all())
    if not rows:
        raise NoDefaultSender(
            "No default email sender configured for this project",
            detail="Add one under Project Settings → Notifications.",
        )
    if len(rows) > 1:
        logger.warning(
            "Multiple default email senders for project_id=%s — using the first",
            project_id,
        )
    return build_sender_from_row(rows[0])


def sender_display_summary(row: ProjectEmailSender) -> dict[str, Any]:
    """
    Build a safe, redacted summary of a saved ``ProjectEmailSender`` for
    display in the settings UI.

    NEVER decrypts the ``config_encrypted`` blob — this is called while
    rendering the settings page and we don't want decrypted credentials
    floating anywhere near template context. The summary pulls from
    columns we already materialise.
    """
    return {
        "id": str(row.id),
        "type": row.type,
        "label": row.label or "",
        "from_address": row.from_address or "",
        "from_name": row.from_name or "",
        "is_default": bool(row.is_default),
        "last_tested_at": row.last_tested_at.isoformat() if row.last_tested_at else None,
        "last_test_status": row.last_test_status or None,
        "last_test_error": row.last_test_error or "",
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
