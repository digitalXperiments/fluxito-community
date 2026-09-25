"""
Shared types for the Slack delivery subsystem.

``SlackMessage`` is the domain object callers pass to a ``SlackSender``. The
concrete fields are shaped around what a Slack incoming webhook accepts —
this intentionally does not model the full chat.postMessage surface (that's
Phase 2 territory).

Block Kit payloads come through as ``blocks: list[dict]`` — we do not wrap
them in a typed schema because Block Kit evolves quickly and a loose dict
type is more forgiving than trying to mirror every block type here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class SlackSendError(Exception):
    """Raised when a Slack webhook/config problem prevents delivery.

    Carries a ``message`` (what to surface in UI) plus optional ``detail``
    (longer technical string — SMTP code, HTTP body, etc.).
    """

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


@dataclass(frozen=True)
class SlackMessage:
    """A ready-to-send Slack message.

    Fields:
      text     — fallback text (shown in notification previews and in any
                 client that cannot render blocks, including screen readers).
                 Slack strongly recommends always setting this even when
                 ``blocks`` is populated.
      blocks   — ordered list of Block Kit dicts. May be empty.
      channel  — optional channel override. Incoming webhooks usually ignore
                 this (they post to the channel they were provisioned for),
                 but Phase 2 (chat.postMessage) will honour it.
      username — optional display name override. Only supported on legacy
                 webhooks with "Post as" enabled. Safe to pass — unsupported
                 fields are ignored by Slack.
      icon_emoji — e.g. ``:chart_with_upwards_trend:``. Same caveat.
    """

    text: str
    blocks: list[dict[str, Any]] = field(default_factory=list)
    channel: str | None = None
    username: str | None = None
    icon_emoji: str | None = None

    def to_webhook_payload(self) -> dict[str, Any]:
        """Render self as the JSON body an incoming webhook expects."""
        payload: dict[str, Any] = {"text": self.text or " "}
        if self.blocks:
            payload["blocks"] = list(self.blocks)
        if self.channel:
            payload["channel"] = self.channel
        if self.username:
            payload["username"] = self.username
        if self.icon_emoji:
            payload["icon_emoji"] = self.icon_emoji
        return payload


@dataclass(frozen=True)
class SendResult:
    """Outcome of a ``SlackSender.send`` call."""

    success: bool
    error: str | None = None


@runtime_checkable
class SlackSender(Protocol):
    """Protocol for anything that can deliver a ``SlackMessage``.

    Concrete implementations:
      * ``WebhookSender`` — incoming webhooks (Phase 1)
      * (future) ``BotTokenSender`` — chat.postMessage with an OAuth bot token
    """

    async def send(self, message: SlackMessage) -> SendResult: ...

    async def verify(self) -> SendResult:
        """Cheap credential/config check — should NOT post a visible message.

        Webhooks don't have a true credential-ping endpoint, so
        ``WebhookSender.verify()`` posts a minimal "connection check" message
        that a user can safely ignore. Callers expecting "verify does not
        publish anything" should use ``send(...)`` with a dedicated test
        payload instead.
        """
        ...
