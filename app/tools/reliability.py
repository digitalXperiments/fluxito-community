"""
Reliability primitives for MCP tool calls.

Provides:
  - with_timeout()  : per-tool-class timeout wrapper with graceful degradation
  - CircuitBreaker  : per-target (e.g. per-connection) failure tracking with
                      open/half-open/closed states
  - RequestLogger   : structured per-call logs (tool, action, duration,
                      status, approx token counts, error type)

Design notes:
  - Tools NEVER raise — on timeout/circuit-open/any error we return a
    structured dict with error=True and a human-readable message.
  - Approximate token counts: we estimate 4 chars ≈ 1 token (a reasonable
    heuristic for JSON responses). Accurate enough for dashboards/billing.
  - Circuit breakers live in-process. For multi-worker deployments a Redis
    backend is TODO — the in-process version still prevents one bad
    connector from saturating the worker.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("mcp.reliability")

# ---------------------------------------------------------------------------
# Per-tool-class timeouts (seconds)
# ---------------------------------------------------------------------------

TIMEOUT_READ = 30  # list_*, get_*, preview_* — metadata reads
TIMEOUT_AUDIT = 60  # health/audit passes — may hit multiple APIs
TIMEOUT_QUERY = 120  # warehouse_query — SQL can be slow
TIMEOUT_WRITE = 60  # create/update/delete
TIMEOUT_CROSS = 90  # cross_platform_report — parallel fan-out
TIMEOUT_SCRIPT = 75  # run_script — outer cap is 60s, leave headroom for dispatch
DEFAULT_TIMEOUT = 45

TOOL_TIMEOUTS: dict[str, int] = {
    # Unified reads — include audit actions (hence AUDIT timeout to be safe)
    "analytics_read": TIMEOUT_AUDIT,
    "tagmanager_read": TIMEOUT_AUDIT,
    "warehouse_read": TIMEOUT_AUDIT,
    "marketing_read": TIMEOUT_AUDIT,
    "seo_read": TIMEOUT_AUDIT,
    "get_knowledge": TIMEOUT_READ,
    "get_session_context": TIMEOUT_READ,
    # Writes
    "warehouse_query": TIMEOUT_QUERY,
    "analytics_write": TIMEOUT_WRITE,
    "tagmanager_write": TIMEOUT_WRITE,
    "marketing_write": TIMEOUT_WRITE,
    "seo_write": TIMEOUT_WRITE,
    "generic_tool_read": TIMEOUT_READ,
    "generic_tool_write": TIMEOUT_WRITE,
    # Programmatic composition — caps its own inner timeout to 60s,
    # outer wrapper adds headroom for dispatch + serialization.
    "run_script": TIMEOUT_SCRIPT,
    # Legacy names still kept internally (if something reaches them by name)
    "cross_platform_report": TIMEOUT_CROSS,
}


def timeout_for(tool_name: str) -> int:
    return TOOL_TIMEOUTS.get(tool_name, DEFAULT_TIMEOUT)


async def with_timeout(
    fn: Callable[..., Awaitable[Any]],
    *args,
    timeout: int | None = None,
    tool_name: str | None = None,
    **kwargs,
) -> Any:
    """Run an async function with a timeout. Returns a structured error
    dict on timeout instead of raising."""
    t = timeout if timeout is not None else timeout_for(tool_name or "")
    try:
        return await asyncio.wait_for(fn(*args, **kwargs), timeout=t)
    except TimeoutError:
        logger.warning(f"tool timeout: {tool_name or fn.__name__} after {t}s")
        return {
            "error": True,
            "error_type": "timeout",
            "message": (
                f"Operation exceeded {t}s and was cancelled. "
                "Try a smaller date range, lower limit, or narrower filter."
            ),
            "timeout_seconds": t,
        }


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


# Circuit breaker defaults
_CB_FAILURE_THRESHOLD = 5
_CB_COOLDOWN_SECONDS = 60


@dataclass
class _BreakerState:
    failures: int = 0
    opened_at: float = 0.0
    half_open: bool = False


class CircuitBreaker:
    """Simple in-process breaker.

    After `failure_threshold` consecutive failures against a target,
    the breaker opens for `cooldown_seconds`. During that window all
    calls return a circuit-open error immediately. One probe is allowed
    after cooldown (half-open) — success closes it, failure reopens.
    """

    def __init__(
        self,
        failure_threshold: int = _CB_FAILURE_THRESHOLD,
        cooldown_seconds: int = _CB_COOLDOWN_SECONDS,
    ):
        self._states: dict[str, _BreakerState] = {}
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds

    def _state(self, key: str) -> _BreakerState:
        if key not in self._states:
            self._states[key] = _BreakerState()
        return self._states[key]

    def allow(self, key: str) -> tuple[bool, str | None]:
        """Return (allowed, reason_if_not)."""
        s = self._state(key)
        if s.failures < self.failure_threshold:
            return True, None
        # Open — check cooldown
        elapsed = time.time() - s.opened_at
        if elapsed >= self.cooldown_seconds:
            # Allow a probe
            s.half_open = True
            return True, None
        remaining = int(self.cooldown_seconds - elapsed)
        return False, (f"Circuit open for '{key}' — {s.failures} recent failures. Retry in {remaining}s.")

    def record_success(self, key: str) -> None:
        s = self._state(key)
        s.failures = 0
        s.opened_at = 0.0
        s.half_open = False

    def record_failure(self, key: str) -> None:
        s = self._state(key)
        s.failures += 1
        if s.failures >= self.failure_threshold and s.opened_at == 0.0:
            s.opened_at = time.time()
            logger.warning(f"circuit OPENED for '{key}' after {s.failures} consecutive failures")

    def snapshot(self) -> list[dict]:
        now = time.time()
        out = []
        for key, s in self._states.items():
            open_ = s.failures >= self.failure_threshold and (now - s.opened_at < self.cooldown_seconds)
            out.append(
                {
                    "target": key,
                    "failures": s.failures,
                    "state": "open" if open_ else ("half_open" if s.half_open else "closed"),
                    "opened_seconds_ago": int(now - s.opened_at) if s.opened_at else None,
                }
            )
        return out


# Shared global breaker for all tools. Scoped by "tool:target"
breaker = CircuitBreaker(failure_threshold=_CB_FAILURE_THRESHOLD, cooldown_seconds=_CB_COOLDOWN_SECONDS)


# ---------------------------------------------------------------------------
# Request-level structured logging (+ approx token tracking)
# ---------------------------------------------------------------------------


# Token estimation constants
_TOKEN_ESTIMATE_RATIO = 4  # 4 chars ≈ 1 token (GPT/Claude heuristic)
_MIN_TOKENS = 1  # Minimum token count (1 instead of 0)


def _approx_tokens(obj: Any) -> int:
    """Rough token estimate: 4 chars ≈ 1 token (GPT/Claude heuristic)."""
    try:
        if obj is None:
            return 0
        if isinstance(obj, (dict, list)):
            return max(_MIN_TOKENS, len(json.dumps(obj, default=str)) // _TOKEN_ESTIMATE_RATIO)
        return max(_MIN_TOKENS, len(str(obj)) // _TOKEN_ESTIMATE_RATIO)
    except Exception:
        return 0


@dataclass
class RequestStats:
    """In-memory request counters surfaced via /api/health."""

    total: int = 0
    success: int = 0
    error: int = 0
    denied: int = 0
    timeout: int = 0
    total_duration_ms: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    by_tool: dict[str, dict] = field(default_factory=dict)

    def record(
        self,
        tool: str,
        status: str,
        duration_ms: int,
        in_tokens: int,
        out_tokens: int,
    ) -> None:
        self.total += 1
        if status == "success":
            self.success += 1
        elif status == "denied":
            self.denied += 1
        elif status == "timeout":
            self.timeout += 1
        else:
            self.error += 1
        self.total_duration_ms += duration_ms
        self.total_input_tokens += in_tokens
        self.total_output_tokens += out_tokens
        t = self.by_tool.setdefault(
            tool,
            {
                "count": 0,
                "errors": 0,
                "duration_ms": 0,
                "in_tokens": 0,
                "out_tokens": 0,
            },
        )
        t["count"] += 1
        t["duration_ms"] += duration_ms
        t["in_tokens"] += in_tokens
        t["out_tokens"] += out_tokens
        if status not in ("success",):
            t["errors"] += 1

    def snapshot(self) -> dict:
        return {
            "total_calls": self.total,
            "success": self.success,
            "error": self.error,
            "denied": self.denied,
            "timeout": self.timeout,
            "avg_duration_ms": (self.total_duration_ms // self.total) if self.total else 0,
            "total_input_tokens_est": self.total_input_tokens,
            "total_output_tokens_est": self.total_output_tokens,
            "by_tool": self.by_tool,
        }


stats = RequestStats()


def log_request(
    tool: str,
    arguments: Any,
    result: Any,
    status: str,
    duration_ms: int,
    source_client: str | None = None,
    user_id: str | None = None,
    error_type: str | None = None,
) -> None:
    """Emit a structured log line + update in-memory counters."""
    in_tok = _approx_tokens(arguments)
    out_tok = _approx_tokens(result)
    stats.record(tool, status, duration_ms, in_tok, out_tok)

    logger.info(
        "mcp_tool_call",
        extra={
            "mcp_tool": tool,
            "action": (arguments or {}).get("action") if isinstance(arguments, dict) else None,
            "status": status,
            "duration_ms": duration_ms,
            "in_tokens_est": in_tok,
            "out_tokens_est": out_tok,
            "source_client": source_client,
            "user_id": user_id,
            "error_type": error_type,
        },
    )
