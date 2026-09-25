import httpx
import pytest

from app.ask.providers.base import (
    LLMMessage,
    StopReason,
    TextBlock,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
)
from app.ask.providers.openai import OpenAIProvider, ProviderError, tools_unsupported


def test_build_body_includes_stream_options_for_openai():
    p = OpenAIProvider(api_key="sk-test")
    body = p.build_body(
        model="gpt-4o",
        system="SYS",
        messages=[LLMMessage(role="user", content=[TextBlock(text="hi")])],
        tools=[],
        max_tokens=512,
    )
    assert "stream_options" in body
    assert body["stream_options"] == {"include_usage": True}


def test_build_body_omits_stream_options_when_send_usage_false():
    """send_usage=False must not include stream_options (some servers reject it)."""
    p = OpenAIProvider(api_key="sk-test", base_url="http://localhost:1234/v1", send_usage=False)
    body = p.build_body(
        model="llama3",
        system="SYS",
        messages=[LLMMessage(role="user", content=[TextBlock(text="hi")])],
        tools=[],
        max_tokens=512,
    )
    assert "stream_options" not in body


def test_base_url_stored_and_trailing_slash_stripped():
    p = OpenAIProvider(api_key="sk", base_url="http://localhost:1234/v1/")
    assert p._base_url == "http://localhost:1234/v1"


def test_empty_api_key_uses_placeholder_bearer():
    p = OpenAIProvider(api_key="")
    headers = p.build_headers()
    assert headers["Authorization"] == "Bearer none"


def test_nonempty_api_key_used_verbatim():
    p = OpenAIProvider(api_key="sk-real")
    headers = p.build_headers()
    assert headers["Authorization"] == "Bearer sk-real"


def test_build_body_maps_roles_and_tools():
    p = OpenAIProvider(api_key="sk-test")
    body = p.build_body(
        model="gpt-4o",
        system="SYS",
        messages=[
            LLMMessage(role="user", content=[TextBlock(text="hi")]),
            LLMMessage(role="assistant", content=[ToolUseBlock(id="c1", name="x", input={"a": 1})]),
            LLMMessage(role="tool", content=[ToolResultBlock(tool_use_id="c1", content="ok")]),
        ],
        tools=[ToolSpec(name="x", description="d", input_schema={"type": "object"})],
        max_tokens=1024,
    )
    assert body["model"] == "gpt-4o" and body["stream"] is True
    assert body["messages"][0] == {"role": "system", "content": "SYS"}
    assert body["messages"][1] == {"role": "user", "content": "hi"}
    assert body["messages"][2]["role"] == "assistant"
    assert body["messages"][2]["tool_calls"][0] == {
        "id": "c1",
        "type": "function",
        "function": {"name": "x", "arguments": '{"a": 1}'},
    }
    assert body["messages"][3] == {"role": "tool", "tool_call_id": "c1", "content": "ok"}
    assert body["tools"][0] == {
        "type": "function",
        "function": {"name": "x", "description": "d", "parameters": {"type": "object"}},
    }


def test_token_param_and_usage_depend_on_host():
    msgs = [LLMMessage(role="user", content=[TextBlock(text="hi")])]
    openai = OpenAIProvider("k", base_url="https://api.openai.com/v1").build_body(
        model="m", system="S", messages=msgs, tools=[], max_tokens=10
    )
    assert openai["max_completion_tokens"] == 10 and "max_tokens" not in openai
    router = OpenAIProvider("k", base_url="https://openrouter.ai/api/v1").build_body(
        model="m", system="S", messages=msgs, tools=[], max_tokens=10
    )
    assert router["max_tokens"] == 10 and "stream_options" in router
    other = OpenAIProvider("k", base_url="https://llm.example.com/v1").build_body(
        model="m", system="S", messages=msgs, tools=[], max_tokens=10
    )
    assert other["max_tokens"] == 10 and "stream_options" not in other


def test_build_body_without_tools_drops_tool_history():
    p = OpenAIProvider(api_key="sk-test")
    body = p.build_body(
        model="m",
        system="SYS",
        messages=[
            LLMMessage(role="user", content=[TextBlock(text="hi")]),
            LLMMessage(role="assistant", content=[ToolUseBlock(id="c1", name="x", input={})]),
            LLMMessage(role="tool", content=[ToolResultBlock(tool_use_id="c1", content="ok")]),
            LLMMessage(role="assistant", content=[TextBlock(text="done")]),
            LLMMessage(role="user", content=[TextBlock(text="again")]),
        ],
        tools=[],
        max_tokens=10,
    )
    assert "tools" not in body
    assert [m["role"] for m in body["messages"]] == ["system", "user", "assistant", "user"]
    assert all("tool_calls" not in m for m in body["messages"])


def test_tools_unsupported_detection():
    assert tools_unsupported(400, "This model does not support tools")
    assert tools_unsupported(404, "No endpoints found that support tool use")
    assert tools_unsupported(400, "'functions' is not supported")
    assert not tools_unsupported(401, "invalid api key for tools")
    assert not tools_unsupported(400, "context length exceeded")
    assert not tools_unsupported(None, "tool")


def test_parse_sse_in_band_error_frame():
    p = OpenAIProvider(api_key="sk-test")
    raw = 'data: {"error":{"message":"upstream down"}}\n\ndata: [DONE]\n\n'
    events = list(p.parse_sse(_chunks(raw)))
    err = next(e for e in events if e.type == "error")
    assert "upstream down" in err.error
    assert events[-1].stop_reason == StopReason.ERROR


def _mock_transport(monkeypatch, handler):
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr("app.ask.providers.openai.httpx.AsyncClient", factory)


@pytest.mark.asyncio
async def test_complete_returns_text_and_sends_non_streaming_body(monkeypatch):
    seen = {}

    def handler(request):
        import json as _json

        seen["url"] = str(request.url)
        seen["body"] = _json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    _mock_transport(monkeypatch, handler)
    p = OpenAIProvider("sk-x", base_url="https://openrouter.ai/api/v1")
    assert await p.complete(model="m", prompt="hi") == "ok"
    assert seen["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert seen["body"]["stream"] is False and seen["body"]["model"] == "m"
    assert seen["auth"] == "Bearer sk-x"


@pytest.mark.asyncio
async def test_complete_raises_provider_error_with_message(monkeypatch):
    _mock_transport(monkeypatch, lambda r: httpx.Response(401, json={"error": {"message": "bad key"}}))
    p = OpenAIProvider("sk-x", base_url="https://api.openai.com/v1")
    with pytest.raises(ProviderError) as ei:
        await p.complete(model="m", prompt="hi")
    assert ei.value.status == 401 and "bad key" in str(ei.value)


@pytest.mark.asyncio
async def test_stream_http_error_yields_status(monkeypatch):
    _mock_transport(
        monkeypatch, lambda r: httpx.Response(400, json={"error": {"message": "tools not supported"}})
    )
    p = OpenAIProvider("sk-x", base_url="https://api.openai.com/v1")
    events = [
        e
        async for e in p.stream(
            model="m",
            system="S",
            messages=[LLMMessage(role="user", content=[TextBlock(text="hi")])],
            tools=[],
            max_tokens=5,
        )
    ]
    assert events[0].type == "error" and events[0].status == 400
    assert "tools not supported" in events[0].error
    assert events[-1].stop_reason == StopReason.ERROR


@pytest.mark.asyncio
async def test_stream_connection_error_is_reported(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    _mock_transport(monkeypatch, handler)
    p = OpenAIProvider("sk-x", base_url="https://api.openai.com/v1")
    events = [
        e
        async for e in p.stream(
            model="m",
            system="S",
            messages=[LLMMessage(role="user", content=[TextBlock(text="hi")])],
            tools=[],
            max_tokens=5,
        )
    ]
    assert events[0].type == "error" and "ConnectError" in events[0].error
    assert events[-1].type == "message_done"


def test_parse_sse_accumulates_tool_args_by_index():
    p = OpenAIProvider(api_key="sk-test")
    raw = (
        'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"analytics_read","arguments":"{\\"a\\":"}}]}}]}\n\n'
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"1}"}}]}}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n'
        "data: [DONE]\n\n"
    )
    events = list(p.parse_sse(_chunks(raw)))
    text = "".join(e.text for e in events if e.type == "text_delta")
    assert text == "Hello"
    start = next(e for e in events if e.type == "tool_call_start")
    assert start.tool_id == "c1" and start.tool_name == "analytics_read"
    args = "".join(e.args_fragment for e in events if e.type == "tool_args_delta")
    assert args == '{"a":1}'
    done = next(e for e in events if e.type == "message_done")
    assert done.stop_reason == StopReason.TOOL_USE


def test_parse_sse_parallel_tool_calls_no_cross_contamination():
    """Two parallel tool calls with interleaved arg fragments must not cross-contaminate."""
    p = OpenAIProvider(api_key="sk-test")
    # index 0 = "search" with id "id-A"; index 1 = "fetch" with id "id-B"
    # arg fragments are interleaved: A chunk, B chunk, A chunk, B chunk
    raw = (
        # index 0 start (id present on first delta)
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"id-A","function":{"name":"search","arguments":""}}]}}]}\n\n'
        # index 1 start (id present on first delta)
        'data: {"choices":[{"delta":{"tool_calls":[{"index":1,"id":"id-B","function":{"name":"fetch","arguments":""}}]}}]}\n\n'
        # args for index 0 — first fragment
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"q\\":"}}]}}]}\n\n'
        # args for index 1 — first fragment (interleaved!)
        'data: {"choices":[{"delta":{"tool_calls":[{"index":1,"function":{"arguments":"{\\"url\\":"}}]}}]}\n\n'
        # args for index 0 — second fragment
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"hello\\"}"}}]}}]}\n\n'
        # args for index 1 — second fragment
        'data: {"choices":[{"delta":{"tool_calls":[{"index":1,"function":{"arguments":"\\"http://x\\"}"}}]}}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n'
        "data: [DONE]\n\n"
    )
    events = list(p.parse_sse(_chunks(raw)))

    # Two tool_call_start events with distinct ids and names
    starts = [e for e in events if e.type == "tool_call_start"]
    assert len(starts) == 2
    start_ids = {e.tool_id for e in starts}
    start_names = {e.tool_name for e in starts}
    assert start_ids == {"id-A", "id-B"}
    assert start_names == {"search", "fetch"}

    # Args fragments for each tool_id must be correct and not cross-contaminated
    args_by_id: dict[str, str] = {}
    for e in events:
        if e.type == "tool_args_delta":
            assert e.tool_id is not None, "tool_args_delta must carry tool_id"
            args_by_id[e.tool_id] = args_by_id.get(e.tool_id, "") + (e.args_fragment or "")

    assert args_by_id["id-A"] == '{"q":"hello"}'
    assert args_by_id["id-B"] == '{"url":"http://x"}'

    # A tool_call_end for each started tool
    ends = [e for e in events if e.type == "tool_call_end"]
    assert len(ends) == 2
    end_ids = {e.tool_id for e in ends}
    assert end_ids == {"id-A", "id-B"}

    # Terminal event is TOOL_USE
    done = next(e for e in events if e.type == "message_done")
    assert done.stop_reason == StopReason.TOOL_USE


def _chunks(s: str):
    for i in range(0, len(s), 9):
        yield s[i : i + 9]
