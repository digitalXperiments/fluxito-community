"""The chat loop: model -> (read-only tools -> model)* -> answer.

At most ``MAX_TOOL_ROUNDS`` rounds of tool calls per reply. If the endpoint
rejects the tools parameter (a model without function calling), the call is
retried once without tools and the reply continues as plain text chat.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

from app.ask.providers.base import (
    ContentBlock,
    LLMMessage,
    StopReason,
    StreamEvent,
    TextBlock,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
)
from app.ask.providers.openai import tools_unsupported

MAX_TOOL_ROUNDS = 6

TOOLS_DISABLED_NOTICE = (
    "This model doesn't support tool calls, so it can't read your platform data. Answering as plain chat."
)


class _Provider(Protocol):
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


class _Bridge(Protocol):
    def tool_specs(self) -> list[ToolSpec]: ...

    async def dispatch(self, name: str, params: dict[str, Any]) -> tuple[str, bool]: ...


class _Service(Protocol):
    async def append(
        self,
        conversation_id: Any,
        message: LLMMessage,
        *,
        token_usage: dict | None = ...,
    ) -> Any: ...


@dataclass
class HarnessDeps:
    provider: _Provider
    bridge: _Bridge
    service: _Service
    conversation_id: Any
    model: str
    system: str
    history: list[LLMMessage] | None = None
    max_tokens: int = 4096
    # Optional: rebuild the system prompt when tools get disabled mid-reply.
    system_without_tools: str | None = None


class _PendingTool:
    __slots__ = ("args", "id", "name")

    def __init__(self, tool_id: str, tool_name: str) -> None:
        self.id = tool_id
        self.name = tool_name
        self.args = ""


class Harness:
    def __init__(self, deps: HarnessDeps, *, max_tool_rounds: int = MAX_TOOL_ROUNDS) -> None:
        self._d = deps
        self._max_rounds = max_tool_rounds

    async def run(self, user_message: LLMMessage) -> AsyncIterator[StreamEvent]:
        d = self._d
        messages: list[LLMMessage] = list(d.history or [])
        messages.append(user_message)
        await d.service.append(d.conversation_id, user_message)

        tools = d.bridge.tool_specs()
        system = d.system
        tools_retry_used = False
        total_input = 0
        total_output = 0
        rounds = 0

        while True:
            text_buf: list[str] = []
            pending: dict[str, _PendingTool] = {}
            order: list[str] = []
            stop: StopReason | None = None
            usage: dict | None = None
            retry_without_tools = False

            async for ev in d.provider.stream(
                model=d.model,
                system=system,
                messages=list(messages),
                tools=tools,
                max_tokens=d.max_tokens,
            ):
                if ev.type == "text_delta":
                    text_buf.append(ev.text or "")
                    yield ev
                elif ev.type == "tool_call_start":
                    tool_id = ev.tool_id or f"call_{rounds}_{len(order)}"
                    pending[tool_id] = _PendingTool(tool_id, ev.tool_name or "")
                    order.append(tool_id)
                    yield StreamEvent(type="tool_call_start", tool_id=tool_id, tool_name=ev.tool_name)
                elif ev.type == "tool_args_delta":
                    tid = ev.tool_id if ev.tool_id in pending else (order[-1] if order else None)
                    if tid is not None:
                        pending[tid].args += ev.args_fragment or ""
                elif ev.type == "error":
                    if (
                        tools
                        and not tools_retry_used
                        and not text_buf
                        and not order
                        and tools_unsupported(ev.status, ev.error)
                    ):
                        retry_without_tools = True
                        continue
                    yield ev
                elif ev.type == "message_done":
                    stop = ev.stop_reason
                    usage = ev.usage

            if retry_without_tools:
                tools_retry_used = True
                tools = []
                system = d.system_without_tools or system
                yield StreamEvent(type="notice", text=TOOLS_DISABLED_NOTICE)
                continue

            if usage:
                total_input += usage.get("input_tokens") or usage.get("prompt_tokens") or 0
                total_output += usage.get("output_tokens") or usage.get("completion_tokens") or 0

            # Persist the assistant turn (text and/or tool calls).
            blocks: list[ContentBlock] = []
            if text_buf:
                blocks.append(TextBlock(text="".join(text_buf)))
            calls = [(tid, pending[tid].name, _safe_json(pending[tid].args)) for tid in order]
            for tid, name, args in calls:
                blocks.append(ToolUseBlock(id=tid, name=name, input=args))
            if blocks:
                assistant_msg = LLMMessage(role="assistant", content=blocks)
                messages.append(assistant_msg)
                tu = dict(usage or {})
                tu["model"] = d.model
                await d.service.append(d.conversation_id, assistant_msg, token_usage=tu)

            if not calls or stop == StopReason.ERROR:
                if calls:
                    # Keep the stored history valid: every tool call gets a result.
                    await self._persist_results(
                        messages,
                        [(tid, json.dumps({"error": "Reply interrupted."}), True) for tid, _, _ in calls],
                    )
                yield StreamEvent(
                    type="message_done",
                    stop_reason=stop or StopReason.END,
                    usage=_summed(total_input, total_output),
                )
                return

            rounds += 1
            if rounds > self._max_rounds:
                # Budget exhausted: answer the pending calls with an error so the
                # stored history stays well-formed, then stop.
                err = json.dumps({"error": f"Tool budget of {self._max_rounds} rounds reached."})
                await self._persist_results(messages, [(tid, err, True) for tid, _, _ in calls])
                yield StreamEvent(
                    type="error",
                    error=f"Stopped after {self._max_rounds} rounds of tool calls without a final answer.",
                )
                yield StreamEvent(
                    type="message_done",
                    stop_reason=StopReason.MAX_TOKENS,
                    usage=_summed(total_input, total_output),
                )
                return

            results = await asyncio.gather(*[d.bridge.dispatch(name, args) for _, name, args in calls])
            triples = [
                (tid, content, is_err) for (tid, _, _), (content, is_err) in zip(calls, results, strict=True)
            ]
            for tid, _content, is_err in triples:
                yield StreamEvent(type="tool_result", tool_id=tid, is_error=is_err)
            await self._persist_results(messages, triples)

    async def _persist_results(
        self, messages: list[LLMMessage], results: list[tuple[str, str, bool]]
    ) -> None:
        tool_msg = LLMMessage(
            role="tool",
            content=[
                ToolResultBlock(tool_use_id=tid, content=content, is_error=is_err)
                for tid, content, is_err in results
            ],
        )
        messages.append(tool_msg)
        await self._d.service.append(self._d.conversation_id, tool_msg)


def _summed(total_input: int, total_output: int) -> dict | None:
    if not (total_input or total_output):
        return None
    return {"input_tokens": total_input, "output_tokens": total_output}


def _safe_json(s: str) -> dict[str, Any]:
    s = (s or "").strip()
    if not s:
        return {}
    try:
        val = json.loads(s)
        return val if isinstance(val, dict) else {"value": val}
    except json.JSONDecodeError:
        return {}
