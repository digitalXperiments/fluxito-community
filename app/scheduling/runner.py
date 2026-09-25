"""
Scheduled job worker.

Single public entry point:

    await run_test_flow(flow_id)

Called by APScheduler's job function (see ``service.py``) when a test
flow's cron trigger fires.
"""

from __future__ import annotations

import uuid


async def run_test_flow(flow_id: uuid.UUID | str) -> uuid.UUID:
    """APScheduler-facing entry point for a scheduled test flow.

    Thin wrapper delegating to the flow runner's own orchestration
    (``app.tag_testing.flow_runner.service.run_flow``), which owns row
    creation, execution, evaluation, audit mirroring and notifications.

    Returns the ``TestFlowRun.id``. Raises ``LookupError`` if the flow no
    longer exists.
    """
    from app.tag_testing.flow_runner.service import run_flow

    return await run_flow(flow_id, trigger="schedule")
