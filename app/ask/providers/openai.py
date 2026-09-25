"""OpenAI Chat Completions adapter — raw httpx, no SDK.

The only provider the chat supports: any OpenAI-compatible endpoint (OpenAI,
OpenRouter, a self-hosted gateway, LM Studio, Ollama, ...) addressed by its
base URL.
"""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import AsyncIterator, Iterable, Iterator
from typing import Any

import httpx

from app.ask.providers.base import (
    LLMMessage,
    StopReason,
    StreamEvent,
    TextBlock,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
)

OPENAI_BASE_URL = "https://api.openai.com/v1"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

_FINISH_MAP = {
    "tool_calls": StopReason.TOOL_USE,
    "function_call": StopReason.TOOL_USE,
    "stop": StopReason.END,
    "length": StopReason.MAX_TOKENS,
    "content_filter": StopReason.REFUSAL,
    "error": StopReason.ERROR,
}


def _host(base_url: str) -> str:
    try:
        return (urllib.parse.urlsplit(base_url).hostname or "").lower()
    except ValueError:
        return ""


def _is_openai(base_url: str) -> bool:
    return _host(base_url) == "api.openai.com"


def _is_openrouter(base_url: str) -> bool:
    h = _host(base_url)
    return h == "openrouter.ai" or h.endswith(".openrouter.ai")


class OpenAIProvider:
    name = "openai-compatible"

    def __init__(
        self,
        api_key: str | None,
        *,
        base_url: str = OPENAI_BASE_URL,
        timeout: float = 120.0,
        send_usage: bool | None = None,
        token_param: str | None = None,
    ) -> None:
        self._api_key = api_key or ""
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        # stream_options.include_usage is supported by OpenAI and OpenRouter;
        # other compatible servers sometimes reject unknown params.
        if send_usage is None:
            send_usage = _is_openai(self._base_url) or _is_openrouter(self._base_url)
        self._send_usage = send_usage
        # OpenAI's newer models (gpt-5/o-series) reject "max_tokens" and require
        # "max_completion_tokens"; other OpenAI-compatible servers want "max_tokens".
        if token_param is None:
            token_param = "max_completion_tokens" if _is_openai(self._base_url) else "max_tokens"
        self._token_param = token_param

    # ---- pure helpers ---------------------------------------------------

    def build_body(
        self,
        *,
        model: str,
        system: str,
        messages: list[LLMMessage],
        tools: list[ToolSpec],
        max_tokens: int,
    ) -> dict[str, Any]:
        # Without tools (a model that can't call functions) the tool-call
        # history is dropped entirely: many servers reject tool_calls / tool
        # messages when no tools are declared.
        with_tools = bool(tools)
        wire: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for m in messages:
            if m.role == "tool":
                if not with_tools:
                    continue
                for b in m.content:
                    if isinstance(b, ToolResultBlock):
                        wire.append({"role": "tool", "tool_call_id": b.tool_use_id, "content": b.content})
                continue
            text_parts = [b.text for b in m.content if isinstance(b, TextBlock)]
            tool_calls = (
                [
                    {
                        "id": b.id,
                        "type": "function",
                        "function": {"name": b.name, "arguments": json.dumps(b.input)},
                    }
                    for b in m.content
                    if isinstance(b, ToolUseBlock)
                ]
                if with_tools
                else []
            )
            msg: dict[str, Any] = {"role": m.role}
            if text_parts:
                msg["content"] = "".join(text_parts)
            if tool_calls:
                msg["tool_calls"] = tool_calls
            if "content" not in msg and "tool_calls" not in msg:
                if not with_tools and m.role == "assistant":
                    continue  # a pure tool-call turn: nothing left to send
                msg["content"] = ""
            wire.append(msg)

        body: dict[str, Any] = {
            "model": model,
            self._token_param: max_tokens,
            "messages": wire,
            "stream": True,
        }
        if self._send_usage:
            body["stream_options"] = {"include_usage": True}
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in tools
            ]
            body["tool_choice"] = "auto"
        return body

    def build_headers(self) -> dict[str, str]:
        # Some compatible local servers (e.g. LM Studio) ignore the key but
        # still require a well-formed Authorization header.
        bearer = self._api_key if self._api_key else "none"
        return {"Authorization": f"Bearer {bearer}", "content-type": "application/json"}

    async def complete(self, *, model: str, prompt: str, max_tokens: int = 16) -> str:
        """One tiny non-streaming chat call (used by "Test connection").

        Returns the reply text; raises ``ProviderError`` on an HTTP error.
        """
        body = {
            "model": model,
            self._token_param: max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
        url = f"{self._base_url}/chat/completions"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, headers=self.build_headers(), json=body)
        if resp.status_code >= 400:
            raise ProviderError(resp.status_code, resp.text)
        try:
            data = resp.json()
        except ValueError as exc:
            raise ProviderError(resp.status_code, "The endpoint did not return JSON.") from exc
        if not isinstance(data, dict) or "choices" not in data:
            raise ProviderError(resp.status_code, "The endpoint did not return a chat completion.")
        choices = data.get("choices") or [{}]
        return str(((choices[0] or {}).get("message") or {}).get("content") or "")

    # ---- shared per-frame decoder (used by both parse_sse and stream) ---

    def _iter_frames(
        self,
        frames: Iterable[str],
        index_to_id: dict[int, str],
        started: set[int],
        finish_box: list[str | None],
        usage_box: list[dict[str, Any] | None],
    ) -> Iterator[StreamEvent | str]:
        """Decode and dispatch a sequence of raw SSE data payloads.

        Yields StreamEvent objects normally.  When a ``[DONE]`` sentinel is
        found, emits one ``tool_call_end`` per started tool call (in index
        order) and then yields the string ``"[DONE]"`` so the caller can emit
        the terminal ``message_done`` and stop iteration.

        *index_to_id* — mutable map of tool-call index → id (populated as we
          see tool-call deltas; id only present on the first delta per index).
        *started* — mutable set of tool-call indexes already announced.
        *finish_box* — single-element list used as a mutable cell for the
          latest ``finish_reason``.
        *usage_box* — single-element list used as a mutable cell for usage.
        """
        for frame in frames:
            data = _data_payload(frame)
            if data is None:
                continue
            if data == "[DONE]":
                # Emit tool_call_end for each started tool call (in index order).
                for idx in sorted(started):
                    tool_id = index_to_id.get(idx)
                    yield StreamEvent(type="tool_call_end", tool_id=tool_id)
                yield "[DONE]"
                return
            try:
                evt = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(evt, dict):
                continue
            if evt.get("error"):
                # In-band error frame (e.g. OpenRouter upstream failure).
                err = evt["error"]
                msg = err.get("message") if isinstance(err, dict) else str(err)
                finish_box[0] = "error"
                yield StreamEvent(type="error", error=f"Model endpoint error: {msg}")
                continue
            if evt.get("usage"):
                usage_box[0] = evt["usage"]
            for choice in evt.get("choices", []):
                if choice.get("finish_reason"):
                    finish_box[0] = choice["finish_reason"]
                delta = choice.get("delta", {})
                if delta.get("content"):
                    yield StreamEvent(type="text_delta", text=delta["content"])
                for tc in delta.get("tool_calls", []) or []:
                    idx = tc.get("index", 0)
                    fn = tc.get("function", {})
                    if idx not in started:
                        started.add(idx)
                        tool_id = tc.get("id")
                        if tool_id:
                            index_to_id[idx] = tool_id
                        yield StreamEvent(
                            type="tool_call_start",
                            tool_id=tool_id,
                            tool_name=fn.get("name"),
                        )
                    else:
                        # Capture id if it arrives on a later delta (shouldn't normally
                        # happen with OpenAI, but be defensive).
                        if tc.get("id") and idx not in index_to_id:
                            index_to_id[idx] = tc["id"]
                    if fn.get("arguments"):
                        yield StreamEvent(
                            type="tool_args_delta",
                            tool_id=index_to_id.get(idx),
                            args_fragment=fn["arguments"],
                        )

    # ---- sync SSE parser (unit-tested) ----------------------------------

    def parse_sse(self, chunks: Iterable[str]) -> Iterator[StreamEvent]:
        """Parse an OpenAI SSE byte/str stream into normalized StreamEvents."""
        buf = ""
        index_to_id: dict[int, str] = {}
        started: set[int] = set()
        finish_box: list[str | None] = [None]
        usage_box: list[dict[str, Any] | None] = [None]

        for chunk in chunks:
            buf += chunk
            while "\n\n" in buf:
                frame, buf = buf.split("\n\n", 1)
                for item in self._iter_frames([frame], index_to_id, started, finish_box, usage_box):
                    if item == "[DONE]":
                        yield StreamEvent(
                            type="message_done",
                            stop_reason=_FINISH_MAP.get(finish_box[0] or "", StopReason.END),
                            usage=usage_box[0],
                        )
                        return
                    yield item  # type: ignore[misc]

        # Stream ended without an explicit [DONE].
        # Still emit tool_call_end for any started tools.
        for idx in sorted(started):
            tool_id = index_to_id.get(idx)
            yield StreamEvent(type="tool_call_end", tool_id=tool_id)
        yield StreamEvent(
            type="message_done",
            stop_reason=_FINISH_MAP.get(finish_box[0] or "", StopReason.END),
            usage=usage_box[0],
        )

    # ---- network --------------------------------------------------------

    async def stream(
        self,
        *,
        model: str,
        system: str,
        messages: list[LLMMessage],
        tools: list[ToolSpec],
        max_tokens: int,
    ) -> AsyncIterator[StreamEvent]:
        try:
            async for ev in self._stream(
                model=model, system=system, messages=messages, tools=tools, max_tokens=max_tokens
            ):
                yield ev
        except httpx.HTTPError as exc:
            yield StreamEvent(
                type="error",
                error=f"Could not reach the model endpoint ({type(exc).__name__}).",
            )
            yield StreamEvent(type="message_done", stop_reason=StopReason.ERROR)

    async def _stream(
        self,
        *,
        model: str,
        system: str,
        messages: list[LLMMessage],
        tools: list[ToolSpec],
        max_tokens: int,
    ) -> AsyncIterator[StreamEvent]:
        body = self.build_body(
            model=model, system=system, messages=messages, tools=tools, max_tokens=max_tokens
        )
        url = f"{self._base_url}/chat/completions"
        async with (
            httpx.AsyncClient(timeout=self._timeout) as client,
            client.stream("POST", url, headers=self.build_headers(), json=body) as resp,
        ):
            if resp.status_code >= 400:
                detail = (await resp.aread()).decode("utf-8", "replace")
                yield StreamEvent(
                    type="error",
                    error=f"Model endpoint returned {resp.status_code}: {_short(detail)}",
                    status=resp.status_code,
                )
                yield StreamEvent(type="message_done", stop_reason=StopReason.ERROR)
                return
            buf = ""
            index_to_id: dict[int, str] = {}
            started: set[int] = set()
            finish_box: list[str | None] = [None]
            usage_box: list[dict[str, Any] | None] = [None]
            async for chunk in resp.aiter_text():
                buf += chunk
                frames: list[str] = []
                while "\n\n" in buf:
                    frame, buf = buf.split("\n\n", 1)
                    frames.append(frame)
                done = False
                for item in self._iter_frames(frames, index_to_id, started, finish_box, usage_box):
                    if item == "[DONE]":
                        yield StreamEvent(
                            type="message_done",
                            stop_reason=_FINISH_MAP.get(finish_box[0] or "", StopReason.END),
                            usage=usage_box[0],
                        )
                        done = True
                        break
                    yield item  # type: ignore[misc]
                if done:
                    return
            # Stream ended without an explicit [DONE].
            for idx in sorted(started):
                tool_id = index_to_id.get(idx)
                yield StreamEvent(type="tool_call_end", tool_id=tool_id)
            yield StreamEvent(
                type="message_done",
                stop_reason=_FINISH_MAP.get(finish_box[0] or "", StopReason.END),
                usage=usage_box[0],
            )


def _data_payload(frame: str) -> str | None:
    """Extract the ``data:`` line value from an SSE frame, or None."""
    for line in frame.splitlines():
        if line.startswith("data:"):
            return line[len("data:") :].strip()
    return None


class ProviderError(Exception):
    """An HTTP error from the model endpoint."""

    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail
        super().__init__(f"Model endpoint returned {status}: {_short(detail)}")


def _short(detail: str, limit: int = 500) -> str:
    """Compact an error body: prefer the JSON ``error.message`` when present."""
    text = (detail or "").strip()
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        err = parsed.get("error")
        if isinstance(err, dict) and err.get("message"):
            text = str(err["message"])
        elif isinstance(err, str):
            text = err
        elif parsed.get("message"):
            text = str(parsed["message"])
    return text[:limit]


def tools_unsupported(status: int | None, detail: str | None) -> bool:
    """True when an endpoint error means "this model can't call tools".

    OpenAI-compatible servers answer a tools request to a non-function-calling
    model with a 400 (OpenRouter: 404 "No endpoints found that support tool
    use") whose message mentions tools / functions.
    """
    if status not in (400, 404, 422):
        return False
    text = (detail or "").lower()
    return "tool" in text or "function" in text
