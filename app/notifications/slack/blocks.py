"""
Block Kit helpers for Slack notifications.

Output: a list of Block Kit blocks, ready to pass to ``SlackMessage.blocks``.

Block Kit limits to remember:
  * Max 50 blocks per message.
  * Max 3000 chars per mrkdwn text field.
"""

from __future__ import annotations

from typing import Any

# Block Kit mrkdwn text_field hard limit is 3000; we use a softer internal
# cap to leave room for markdown decoration.
_MRKDWN_SOFT_LIMIT = 2800


def render_simple_blocks(
    title: str,
    body_md: str = "",
    footer: str | None = None,
) -> list[dict[str, Any]]:
    """Minimal blocks for test messages and one-line notifications.

    Used by the Slack settings "Test" button and test-flow alerts.
    """
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": _truncate(title, 150), "emoji": True},
        }
    ]
    if body_md:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": _truncate(body_md, _MRKDWN_SOFT_LIMIT)},
            }
        )
    if footer:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": _truncate(footer, 500)}],
            }
        )
    return blocks


def _truncate(text: str, limit: int) -> str:
    """Slice ``text`` to ``limit`` with a trailing ellipsis when needed."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"
