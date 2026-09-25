import json

import pytest

from app.ask.harness import MAX_TOOL_ROUNDS, TOOLS_DISABLED_NOTICE, Harness, HarnessDeps
from app.ask.providers.base import LLMMessage, StopReason, StreamEvent, TextBlock, ToolSpec


class FakeProvider:
    """Emits a scripted list of event-batches, one batch per .stream() call."""

    name = "fake"

    def __init__(self, scripts):
        self._scripts = list(scripts)
        self.calls = []

    async def stream(self, **kwargs):
        self.calls.append(kwargs)
        for ev in self._scripts.pop(0):
            yield ev


class FakeBridge:
    def __init__(self, specs=None, result=('{"rows": 1}', False)):
        self.calls = []
        self._specs = specs if specs is not None else [ToolSpec("analytics_read", "d", {"type": "object"})]
        self._result = result

    def tool_specs(self):
        return list(self._specs)

    async def dispatch(self, name, params):
        self.calls.append((name, params))
        return self._result


class RecordingService:
    def __init__(self):
        self.appended = []

    async def append(self, conv_id, message, token_usage=None):
        self.appended.append(message)


def _deps(provider, bridge=None, svc=None, **kw):
    return HarnessDeps(
        provider=provider,
        bridge=bridge or FakeBridge(),
        service=svc or RecordingService(),
        conversation_id="c1",
        model="m",
        system="SYS",
        **kw,
    )


def _user(text="hi"):
    return LLMMessage(role="user", content=[TextBlock(text=text)])


def _tool_call(tid="t1", name="analytics_read", args='{"action":"list"}'):
    return [
        StreamEvent(type="tool_call_start", tool_id=tid, tool_name=name),
        StreamEvent(type="tool_args_delta", args_fragment=args),
        StreamEvent(type="tool_call_end"),
        StreamEvent(type="message_done", stop_reason=StopReason.TOOL_USE),
    ]


def _answer(text="done"):
    return [
        StreamEvent(type="text_delta", text=text),
        StreamEvent(
            type="message_done",
            stop_reason=StopReason.END,
            usage={"prompt_tokens": 3, "completion_tokens": 2},
        ),
    ]


async def _run(h, msg=None):
    return [e async for e in h.run(msg or _user())]


@pytest.mark.asyncio
async def test_text_only_turn_streams_and_persists():
    svc = RecordingService()
    h = Harness(_deps(FakeProvider([_answer("Hello")]), svc=svc))
    out = await _run(h)
    assert "".join(e.text for e in out if e.type == "text_delta") == "Hello"
    assert [m.role for m in svc.appended] == ["user", "assistant"]
    done = out[-1]
    assert done.type == "message_done" and done.usage == {"input_tokens": 3, "output_tokens": 2}


@pytest.mark.asyncio
async def test_tool_call_then_final_answer():
    bridge = FakeBridge()
    svc = RecordingService()
    provider = FakeProvider([_tool_call(), _answer("42 sessions")])
    out = await _run(Harness(_deps(provider, bridge=bridge, svc=svc)))
    assert bridge.calls == [("analytics_read", {"action": "list"})]
    assert [m.role for m in svc.appended] == ["user", "assistant", "tool", "assistant"]
    result = next(e for e in out if e.type == "tool_result")
    assert result.tool_id == "t1" and result.is_error is False
    # The second model call sees the tool result.
    assert provider.calls[1]["messages"][-1].role == "tool"


@pytest.mark.asyncio
async def test_tool_loop_is_capped_at_six_rounds():
    assert MAX_TOOL_ROUNDS == 6
    bridge = FakeBridge()
    svc = RecordingService()
    provider = FakeProvider([_tool_call(tid=f"t{i}") for i in range(10)])
    out = await _run(Harness(_deps(provider, bridge=bridge, svc=svc)))
    assert len(bridge.calls) == 6
    assert len(provider.calls) == 7
    assert any(e.type == "error" and "6 rounds" in e.error for e in out)
    assert out[-1].type == "message_done"
    # Every stored tool call has a matching tool result (history stays valid).
    assert svc.appended[-1].role == "tool"
    assert svc.appended[-1].content[0].tool_use_id == "t6"
    assert svc.appended[-1].content[0].is_error


@pytest.mark.asyncio
async def test_retries_without_tools_when_model_rejects_them():
    reject = [
        StreamEvent(type="error", error="Model endpoint returned 400: tools are not supported", status=400),
        StreamEvent(type="message_done", stop_reason=StopReason.ERROR),
    ]
    provider = FakeProvider([reject, _answer("plain")])
    h = Harness(_deps(provider, system_without_tools="NO-TOOLS"))
    out = await _run(h)
    assert provider.calls[0]["tools"] and provider.calls[1]["tools"] == []
    assert provider.calls[1]["system"] == "NO-TOOLS"
    assert not any(e.type == "error" for e in out)
    notice = next(e for e in out if e.type == "notice")
    assert notice.text == TOOLS_DISABLED_NOTICE
    assert "".join(e.text for e in out if e.type == "text_delta") == "plain"


@pytest.mark.asyncio
async def test_tools_retry_happens_only_once():
    reject = [
        StreamEvent(type="error", error="tools unsupported", status=400),
        StreamEvent(type="message_done", stop_reason=StopReason.ERROR),
    ]
    provider = FakeProvider([reject, reject])
    out = await _run(Harness(_deps(provider)))
    assert len(provider.calls) == 2
    assert any(e.type == "error" for e in out)
    assert out[-1].type == "message_done"


@pytest.mark.asyncio
async def test_other_errors_are_not_retried():
    provider = FakeProvider(
        [
            [
                StreamEvent(type="error", error="invalid api key", status=401),
                StreamEvent(type="message_done", stop_reason=StopReason.ERROR),
            ]
        ]
    )
    out = await _run(Harness(_deps(provider)))
    assert len(provider.calls) == 1
    assert next(e for e in out if e.type == "error").error == "invalid api key"


@pytest.mark.asyncio
async def test_no_tools_available_sends_empty_tool_list():
    provider = FakeProvider([_answer()])
    await _run(Harness(_deps(provider, bridge=FakeBridge(specs=[]))))
    assert provider.calls[0]["tools"] == []


@pytest.mark.asyncio
async def test_parallel_tool_calls_all_dispatched_and_answered():
    bridge = FakeBridge()
    svc = RecordingService()
    batch = [
        StreamEvent(type="tool_call_start", tool_id="a", tool_name="analytics_read"),
        StreamEvent(type="tool_args_delta", tool_id="a", args_fragment='{"x":1}'),
        StreamEvent(type="tool_call_start", tool_id="b", tool_name="seo_read"),
        StreamEvent(type="tool_args_delta", tool_id="b", args_fragment='{"y":2}'),
        StreamEvent(type="message_done", stop_reason=StopReason.TOOL_USE),
    ]
    await _run(Harness(_deps(FakeProvider([batch, _answer()]), bridge=bridge, svc=svc)))
    assert bridge.calls == [("analytics_read", {"x": 1}), ("seo_read", {"y": 2})]
    tool_msg = svc.appended[2]
    assert [b.tool_use_id for b in tool_msg.content] == ["a", "b"]


@pytest.mark.asyncio
async def test_history_is_prepended():
    provider = FakeProvider([_answer()])
    history = [_user("earlier"), LLMMessage(role="assistant", content=[TextBlock(text="reply")])]
    await _run(Harness(_deps(provider, history=history)), _user("now"))
    sent = provider.calls[0]["messages"]
    assert [m.content[0].text for m in sent] == ["earlier", "reply", "now"]


@pytest.mark.asyncio
async def test_bad_tool_args_become_empty_dict():
    bridge = FakeBridge()
    provider = FakeProvider([_tool_call(args="{not json"), _answer()])
    await _run(Harness(_deps(provider, bridge=bridge)))
    assert bridge.calls == [("analytics_read", {})]


@pytest.mark.asyncio
async def test_tool_error_result_is_flagged():
    bridge = FakeBridge(result=(json.dumps({"error": "nope"}), True))
    out = await _run(Harness(_deps(FakeProvider([_tool_call(), _answer()]), bridge=bridge)))
    assert next(e for e in out if e.type == "tool_result").is_error is True
