from app.ask.context import MAX_HISTORY_MESSAGES, window_history
from app.ask.providers.base import LLMMessage, TextBlock, ToolResultBlock, ToolUseBlock


def _u(t):
    return LLMMessage(role="user", content=[TextBlock(text=t)])


def test_default_window_is_20_messages():
    assert MAX_HISTORY_MESSAGES == 20
    msgs = [_u(f"m{i}") for i in range(50)]
    out = window_history(msgs)
    assert len(out) == 20
    assert out[0].content[0].text == "m30"
    assert out[-1].content[0].text == "m49"


def test_window_keeps_recent_messages_under_budget():
    msgs = [_u(f"m{i}") for i in range(20)]
    out = window_history(msgs, max_messages=5)
    assert len(out) == 5
    assert out[-1].content[0].text == "m19"


def test_window_never_starts_on_an_orphan_tool_result():
    msgs = [
        LLMMessage(role="assistant", content=[ToolUseBlock(id="t1", name="x", input={})]),
        LLMMessage(role="tool", content=[ToolResultBlock(tool_use_id="t1", content="ok")]),
        _u("hello"),
    ]
    out = window_history(msgs, max_messages=2)
    assert [m.role for m in out] == ["user"]
