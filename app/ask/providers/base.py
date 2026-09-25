"""Vendor-neutral types shared by the chat harness and the provider adapter."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, Protocol


class StopReason(str, Enum):
    END = "end"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    REFUSAL = "refusal"
    ERROR = "error"


@dataclass
class TextBlock:
    text: str
    type: Literal["text"] = "text"


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: Literal["tool_use"] = "tool_use"


@dataclass
class ToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False
    type: Literal["tool_result"] = "tool_result"


ContentBlock = TextBlock | ToolUseBlock | ToolResultBlock


@dataclass
class LLMMessage:
    role: Literal["system", "user", "assistant", "tool"]
    content: list[ContentBlock]


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class StreamEvent:
    """One normalized event in the stream sent to the SSE endpoint."""

    type: Literal[
        "text_delta",
        "tool_call_start",
        "tool_args_delta",
        "tool_call_end",
        "tool_result",  # a tool finished; carries tool_id + is_error
        "notice",  # informational message for the UI (e.g. tools disabled)
        "message_done",
        "error",
    ]
    text: str | None = None
    tool_id: str | None = None
    tool_name: str | None = None
    args_fragment: str | None = None
    stop_reason: StopReason | None = None
    usage: dict[str, Any] | None = None
    error: str | None = None
    # HTTP status of a provider error (only set on type == "error").
    status: int | None = None
    is_error: bool | None = None


def blocks_to_json(blocks: list[ContentBlock]) -> list[dict[str, Any]]:
    """Serialize content blocks to a JSONB-safe list of dicts."""
    out: list[dict[str, Any]] = []
    for b in blocks:
        if isinstance(b, TextBlock):
            out.append({"type": "text", "text": b.text})
        elif isinstance(b, ToolUseBlock):
            out.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        elif isinstance(b, ToolResultBlock):
            out.append(
                {
                    "type": "tool_result",
                    "tool_use_id": b.tool_use_id,
                    "content": b.content,
                    "is_error": b.is_error,
                }
            )
        else:  # pragma: no cover - exhaustive guard
            raise TypeError(f"Unknown content block: {b!r}")
    return out


def blocks_from_json(raw: list[dict[str, Any]] | None) -> list[ContentBlock]:
    """Inverse of blocks_to_json. Unknown block types (e.g. display blocks from
    older versions of the chat) are skipped rather than raising."""
    out: list[ContentBlock] = []
    for d in raw or []:
        if not isinstance(d, dict):
            continue
        t = d.get("type")
        if t == "text":
            out.append(TextBlock(text=str(d.get("text") or "")))
        elif t == "tool_use":
            out.append(ToolUseBlock(id=d["id"], name=d["name"], input=d.get("input") or {}))
        elif t == "tool_result":
            out.append(
                ToolResultBlock(
                    tool_use_id=d["tool_use_id"],
                    content=str(d.get("content") or ""),
                    is_error=bool(d.get("is_error", False)),
                )
            )
    return out


class Provider(Protocol):
    """The harness only ever calls .stream()."""

    name: str

    def stream(
        self,
        *,
        model: str,
        system: str,
        messages: list[LLMMessage],
        tools: list[ToolSpec],
        max_tokens: int,
    ) -> AsyncIterator[StreamEvent]: ...
