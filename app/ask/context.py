"""Conversation history assembly for the harness.

Only the most recent ``MAX_HISTORY_MESSAGES`` messages are sent to the model —
no summarisation, no memory. The kept window never begins with an orphaned
tool result (its assistant tool call was cut off), which would violate the
OpenAI message-threading rules.
"""

from __future__ import annotations

from app.ask.providers.base import LLMMessage

MAX_HISTORY_MESSAGES = 20


def window_history(
    messages: list[LLMMessage], *, max_messages: int = MAX_HISTORY_MESSAGES
) -> list[LLMMessage]:
    kept = messages[-max_messages:] if max_messages > 0 else list(messages)
    # Drop a leading tool turn whose matching assistant tool_use was cut off.
    while kept and kept[0].role == "tool":
        kept = kept[1:]
    return kept
