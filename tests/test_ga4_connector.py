import pytest

from app.connectors.base import BaseConnector
from app.connectors.ga4 import _GA4_CALL_TIMEOUT, GA4Connector


@pytest.mark.anyio
async def test_ga4_run_delegates_to_base_run_sync(monkeypatch):
    calls = []

    async def fake_run_sync(func, *args, **kwargs):
        calls.append((func, args, kwargs))
        return func(*args, **kwargs)

    monkeypatch.setattr(BaseConnector, "run_sync", staticmethod(fake_run_sync))

    connector = GA4Connector(token_manager=None)

    def sdk_call(value, timeout=None):
        return {"value": value, "timeout": timeout}

    result = await connector._run(sdk_call, "ok")

    assert result == {"value": "ok", "timeout": _GA4_CALL_TIMEOUT}
    assert calls == [(sdk_call, ("ok",), {"timeout": _GA4_CALL_TIMEOUT})]
