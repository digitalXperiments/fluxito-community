"""
Slack incoming-webhook sender.

POSTs a JSON payload to a user-supplied ``https://hooks.slack.com/services/…``
URL using ``httpx`` (already a dependency of the project). One short-lived
client per send — webhook traffic is low frequency and we don't want a
long-lived client hanging on to sockets.

Why not the official ``slack_sdk``?
  The SDK pulls in aiohttp and a lot of surface area we don't need for
  simple webhook POSTs. A 20-line httpx call is easier to audit and
  doesn't expand the dependency graph.

Config shape (the dict passed to ``WebhookSender(...)``):

    {
      "webhook_url": "https://hooks.slack.com/services/T.../B.../…",  # required
      "timeout_sec": 10,           # optional, default 10
      "username":    "Analytics",  # optional display override
      "icon_emoji":  ":chart_with_upwards_trend:",  # optional
    }

We store only the URL in the encrypted blob (see
``ProjectSlackWebhook.webhook_url_encrypted``); everything else is
optional and passed per-send if needed.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.notifications.slack.base import (
    SendResult,
    SlackMessage,
    SlackSender,
    SlackSendError,
)

logger = logging.getLogger(__name__)


# Slack incoming webhooks live on this exact host. We check the prefix
# before sending so a typo or an attempt to post to an arbitrary URL
# (which would be SSRF-adjacent) surfaces immediately.
_VALID_WEBHOOK_PREFIXES = (
    "https://hooks.slack.com/services/",
    "https://hooks.slack.com/triggers/",  # Workflow Builder webhooks
)


class WebhookSender(SlackSender):
    """Incoming-webhook sender.

    Constructs from a dict so the factory can merge an encrypted blob
    with row columns uniformly (same pattern as ``EmailSender``).
    """

    def __init__(self, config: dict[str, Any]) -> None:
        url = (config.get("webhook_url") or "").strip()
        if not url:
            raise SlackSendError(
                "Slack webhook URL is missing",
                detail="set 'webhook_url' in the config dict",
            )
        if not url.startswith(_VALID_WEBHOOK_PREFIXES):
            raise SlackSendError(
                "Slack webhook URL is invalid",
                detail=("expected a URL starting with https://hooks.slack.com/services/ or .../triggers/"),
            )

        self.webhook_url: str = url
        self.timeout_sec: int = int(config.get("timeout_sec") or 10)
        self.username: str | None = (config.get("username") or "").strip() or None
        self.icon_emoji: str | None = (config.get("icon_emoji") or "").strip() or None

    # ----------------------------------------------------------------- #
    # Public API
    # ----------------------------------------------------------------- #

    async def send(self, message: SlackMessage) -> SendResult:
        """POST the message to the webhook. Returns ``SendResult``."""
        # Caller-supplied username/icon take precedence; fall back to
        # the sender-level defaults from the constructor.
        payload = message.to_webhook_payload()
        if "username" not in payload and self.username:
            payload["username"] = self.username
        if "icon_emoji" not in payload and self.icon_emoji:
            payload["icon_emoji"] = self.icon_emoji

        try:
            async with httpx.AsyncClient(timeout=self.timeout_sec) as client:
                resp = await client.post(self.webhook_url, json=payload)
        except httpx.TimeoutException as exc:
            raise SlackSendError(
                "Slack webhook timed out",
                detail=f"no response in {self.timeout_sec}s",
            ) from exc
        except httpx.TransportError as exc:
            raise SlackSendError(
                "Could not reach Slack",
                detail=f"{type(exc).__name__}: {str(exc)[:200]}",
            ) from exc

        # Slack webhooks return HTTP 200 with body "ok" on success.
        # On failure they return a 4xx/5xx with a short error string
        # like "invalid_payload", "channel_not_found", "no_service",
        # "webhook_disabled", etc. We surface those verbatim.
        body = (resp.text or "").strip()
        if resp.status_code == 200 and body.lower() == "ok":
            return SendResult(success=True)

        friendly = _friendly_webhook_error(resp.status_code, body)
        logger.warning(
            "Slack webhook send failed status=%s body=%r",
            resp.status_code,
            body[:200],
        )
        raise SlackSendError(friendly, detail=f"HTTP {resp.status_code}: {body[:200]}")

    async def verify(self) -> SendResult:
        """Post a minimal 'connection check' message to the webhook.

        Incoming webhooks have no separate validation endpoint — the
        cheapest thing we can do is send a tiny message. Callers that
        want to avoid publishing anything at all should call ``send``
        with a custom test payload instead.
        """
        msg = SlackMessage(
            text="Fluxito — webhook connection check.",
            blocks=[
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": ":white_check_mark: *Fluxito* connection check — "
                            "if you can read this, your webhook is wired up.",
                        }
                    ],
                }
            ],
        )
        return await self.send(msg)


def _friendly_webhook_error(status: int, body: str) -> str:
    """Translate a Slack webhook error response into a human-readable string."""
    lc = body.lower()
    mapping = {
        "no_service": "Slack webhook is no longer valid (was it revoked?)",
        "no_team": "Slack workspace for this webhook no longer exists",
        "webhook_disabled": "Slack webhook is disabled in the workspace",
        "channel_not_found": "Slack channel for this webhook no longer exists",
        "channel_is_archived": "Slack channel is archived",
        "action_prohibited": "Slack workspace admin has blocked this webhook",
        "invalid_payload": "Slack rejected the payload (malformed blocks?)",
        "missing_text_or_fallback_or_attachments": "Slack rejected the payload (missing fallback text)",
    }
    if lc in mapping:
        return mapping[lc]
    if status == 404:
        return "Slack webhook URL not found (check the URL)"
    if status == 403:
        return "Slack rejected the webhook (disabled or revoked)"
    if status >= 500:
        return "Slack is currently having issues — try again shortly"
    return f"Slack rejected the message ({body[:80] or 'HTTP ' + str(status)})"
