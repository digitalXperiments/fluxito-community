import pytest

from app.tag_testing.live_test.sdr_context import get_sdr_context_for_url


@pytest.mark.anyio
async def test_sdr_context_is_always_empty():
    ctx = await get_sdr_context_for_url("p1", "https://example.com/checkout")

    assert ctx == {
        "project_id": "p1",
        "url": "https://example.com/checkout",
        "events": [],
        "total": 0,
        "error": None,
    }
