"""
Email delivery subsystem.

Public surface:
  * ``EmailMessage``       — immutable dataclass representing a ready-to-send email.
  * ``EmailAttachment``    — file blob with filename + content-type.
  * ``EmailSender``        — Protocol. Concrete senders implement ``async send(msg)``.
  * ``SMTPSender``         — pure-Python async SMTP via ``aiosmtplib``.
  * ``SESSender``          — Amazon SES via ``boto3`` (wrapped in ``asyncio.to_thread``).
  * ``build_sender_from_config`` — construct a sender from a decrypted config dict.
  * ``build_sender_for_project`` — resolve the project's default sender and return it.
  * ``SendResult``         — {success, message_id?, error?} returned from ``send``.

Senders are "bring your own credentials" — we never bill users for delivery,
and no credentials ever leave the process (they're only decrypted at the
moment of sending and dropped immediately).
"""

from app.notifications.email.base import (
    EmailAttachment,
    EmailMessage,
    EmailSender,
    EmailSendError,
    SendResult,
)
from app.notifications.email.factory import (
    NoDefaultSender,
    UnsupportedSenderType,
    build_sender_for_project,
    build_sender_from_config,
    build_sender_from_row,
    sender_display_summary,
    supported_sender_types,
)
from app.notifications.email.ses_sender import SESSender
from app.notifications.email.smtp_sender import SMTPSender

__all__ = [
    "EmailAttachment",
    "EmailMessage",
    "EmailSendError",
    "EmailSender",
    "NoDefaultSender",
    "SESSender",
    "SMTPSender",
    "SendResult",
    "UnsupportedSenderType",
    "build_sender_for_project",
    "build_sender_from_config",
    "build_sender_from_row",
    "sender_display_summary",
    "supported_sender_types",
]
