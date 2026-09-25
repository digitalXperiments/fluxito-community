"""
Slack delivery subsystem (Phase 1 — incoming webhooks).

Public surface:
  * ``SlackMessage``       — immutable dataclass representing a webhook payload.
  * ``SlackSender``        — Protocol. Concrete senders implement ``async send(msg)``.
  * ``WebhookSender``      — posts a JSON payload to a user-supplied incoming webhook URL.
  * ``SendResult``         — {success, error?} returned from ``send``.
  * ``SlackSendError``     — raised on malformed config or HTTP failure.
  * ``build_webhook_sender_from_row`` — decrypt a ``ProjectSlackWebhook`` row and return a sender.
  * ``list_webhook_senders`` — fetch all webhooks for a project (for pickers).
  * ``webhook_display_summary`` — redacted dict for settings UI rendering.
  * ``render_simple_blocks``    — minimal info/test-message blocks.

Phase 2 (OAuth app + full chat.postMessage) is out of scope here — the data
model has room for either path because webhooks and OAuth bot tokens are
both stored as encrypted strings; the sender implementation is what changes.

Like the email subsystem, this is "bring your own credentials" — users
paste their own incoming webhook URL and we never bill for delivery.
"""

from app.notifications.slack.base import (
    SendResult,
    SlackMessage,
    SlackSender,
    SlackSendError,
)
from app.notifications.slack.blocks import render_simple_blocks
from app.notifications.slack.factory import (
    build_webhook_sender_from_row,
    list_webhook_senders,
    webhook_display_summary,
)
from app.notifications.slack.webhook_sender import WebhookSender

__all__ = [
    "SendResult",
    "SlackMessage",
    "SlackSendError",
    "SlackSender",
    "WebhookSender",
    "build_webhook_sender_from_row",
    "list_webhook_senders",
    "render_simple_blocks",
    "webhook_display_summary",
]
