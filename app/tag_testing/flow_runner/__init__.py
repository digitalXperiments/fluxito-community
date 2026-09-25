"""
Test-flow runner package.
=========================

Public surface:

  * :func:`app.tag_testing.flow_runner.executor.execute_flow` — drive a flow
    in headless Chromium and capture dataLayer events + vendor beacons.
  * :func:`app.tag_testing.flow_runner.assertions.evaluate` — pure assertion
    evaluation over captured results.
  * :func:`app.tag_testing.flow_runner.service.run_flow` — orchestrate one
    run end-to-end (execute → evaluate → persist → mirror audit → notify).
"""

from app.tag_testing.flow_runner.assertions import evaluate
from app.tag_testing.flow_runner.executor import ExecutionResult, execute_flow
from app.tag_testing.flow_runner.service import run_flow

__all__ = [
    "ExecutionResult",
    "evaluate",
    "execute_flow",
    "run_flow",
]
