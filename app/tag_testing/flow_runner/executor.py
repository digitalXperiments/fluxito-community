"""
Test-flow browser executor.
============================

Drives a saved :class:`~app.models.test_flows.TestFlow` through a headless
Chromium session (Playwright async API), capturing:

  * every ``window.dataLayer.push`` (plus pre-existing entries) as a
    structured dataLayer event, tagged with the step index it happened in;
  * every network request whose URL matches a project vendor's ``url_pattern``
    (substring) OR one of the rule-book network patterns to watch, parsed into
    platform + params via the live-test parser where possible.

Public API::

    result = await execute_flow(flow, vendors)   # -> ExecutionResult

The result feeds :func:`app.tag_testing.flow_runner.assertions.evaluate`.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from app.models.test_flows import AuditVendor, TestFlow
from app.tag_testing.live_test.parser import parse_request

logger = logging.getLogger(__name__)

# Reuse the rule-book manifest's watch list so flows capture the same beacons
# the live tag test does, even for vendors the project hasn't declared.
try:
    from app.tools.live_tag_test_tools import _NETWORK_PATTERNS_TO_WATCH
except Exception:  # pragma: no cover - defensive; keep executor importable
    _NETWORK_PATTERNS_TO_WATCH = []

# Hard limits.
_NAV_TIMEOUT_MS = 20_000
_CLICK_TIMEOUT_MS = 10_000
_WHOLE_FLOW_TIMEOUT_S = 300  # 5 minutes
_WAIT_CAP_MS = 30_000
_POST_DATA_TRUNCATE = 10_240  # 10 KB

# Mobile device descriptor name (resolved from playwright.devices at runtime).
_MOBILE_DEVICE = "iPhone 13"


@dataclass
class ExecutionResult:
    """Everything a run produced, ready for assertion evaluation + persistence."""

    ok: bool
    error: str | None = None
    datalayer_events: list[dict] = field(default_factory=list)
    beacons: list[dict] = field(default_factory=list)
    step_results: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "error": self.error,
            "datalayer_events": self.datalayer_events,
            "beacons": self.beacons,
            "step_results": self.step_results,
        }


# JS injected before any navigation. Wraps dataLayer.push and records any
# entries already present, forwarding each into an exposed Python binding.
_INIT_SCRIPT = """
(() => {
  const forward = (entry) => {
    try {
      if (window.__fluxRecordDL) { window.__fluxRecordDL(entry); }
    } catch (e) {}
  };
  const install = () => {
    try {
      window.dataLayer = window.dataLayer || [];
      // Replay any pre-existing entries once.
      if (!window.__fluxDLReplayed) {
        window.__fluxDLReplayed = true;
        for (const e of window.dataLayer) { forward(e); }
      }
      const origPush = window.dataLayer.push.bind(window.dataLayer);
      if (!window.dataLayer.__fluxWrapped) {
        window.dataLayer.push = function () {
          for (const a of arguments) { forward(a); }
          return origPush.apply(this, arguments);
        };
        window.dataLayer.__fluxWrapped = true;
      }
    } catch (e) {}
  };
  install();
})();
"""


# Hostnames we never let the headless browser navigate to (SSRF guard).
_BLOCKED_HOSTNAMES = {"localhost", "metadata", "metadata.google.internal"}


def is_safe_http_url(url: str) -> bool:
    """True iff ``url`` is an http(s) URL to a public host.

    Blocks non-http(s) schemes (data:, javascript:, file:, …) and hosts that
    are loopback / private (RFC1918) / link-local (incl. the 169.254.169.254
    cloud-metadata endpoint) / reserved / multicast when the host is an IP
    literal, plus a small set of internal hostnames. This is the authoritative
    gate applied at flow-save time and re-checked here before navigation.

    Note: hostnames that *resolve* to a private address via DNS are not caught
    (no DNS rebinding protection) — that would require socket-level pinning.
    """
    try:
        parts = urllib.parse.urlsplit((url or "").strip())
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    host = parts.hostname
    if not host:
        return False
    h = host.lower()
    if h in _BLOCKED_HOSTNAMES or h.endswith(".localhost"):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True  # not an IP literal — a public hostname
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _entry_event_name(entry: Any) -> str | None:
    """Best-effort extraction of an event name from a dataLayer entry."""
    if isinstance(entry, dict):
        return entry.get("event") or entry.get("event_name")
    return None


def _match_vendor(url: str, vendors: list[AuditVendor]) -> AuditVendor | None:
    for v in vendors:
        pat = (v.url_pattern or "").strip()
        if pat and pat in url:
            return v
    return None


def _matches_watch(url: str) -> bool:
    return any(p in url for p in _NETWORK_PATTERNS_TO_WATCH)


def _parse_beacon_params(url: str, method: str, post_data: str | None) -> dict:
    """Parse params from a captured request.

    Prefer the platform-aware live-test parser; fall back to top-level query
    params + form-encoded / JSON body keys.
    """
    parsed = parse_request(url, method, post_data)
    if parsed is not None:
        params = dict(parsed.params or {})
        if parsed.event_name is not None:
            params.setdefault("event_name", parsed.event_name)
        return params

    params: dict = {}
    # Query string.
    if "?" in url:
        try:
            for k, v in urllib.parse.parse_qs(url.split("?", 1)[1]).items():
                params[k] = v[0] if v else ""
        except Exception:
            pass
    # Body: form-encoded or JSON top-level keys.
    if post_data:
        body = post_data.strip()
        if body.startswith("{"):
            try:
                import json

                data = json.loads(body)
                if isinstance(data, dict):
                    for k, v in data.items():
                        params.setdefault(k, v)
            except Exception:
                pass
        else:
            try:
                for k, v in urllib.parse.parse_qs(body).items():
                    params.setdefault(k, v[0] if v else "")
            except Exception:
                pass
    return params


async def execute_flow(flow: TestFlow, vendors: list[AuditVendor]) -> ExecutionResult:
    """Run ``flow`` end-to-end in headless Chromium and return captures."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - dependency missing
        return ExecutionResult(ok=False, error=f"Playwright not installed: {exc}")

    try:
        async with asyncio.timeout(_WHOLE_FLOW_TIMEOUT_S):
            return await _run(async_playwright, flow, vendors)
    except TimeoutError:
        return ExecutionResult(
            ok=False,
            error=f"Flow exceeded the {_WHOLE_FLOW_TIMEOUT_S}s time budget",
        )
    except Exception as exc:  # pragma: no cover - unexpected crash
        logger.exception("execute_flow crashed for flow %s", getattr(flow, "id", "?"))
        return ExecutionResult(ok=False, error=f"Flow execution crashed: {exc}")


async def _run(async_playwright, flow: TestFlow, vendors: list[AuditVendor]) -> ExecutionResult:
    dl_events: list[dict] = []
    beacons: list[dict] = []
    # Current step index — closures read this to tag captures.
    state = {"step": 0}

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
        except Exception as exc:
            return ExecutionResult(ok=False, error=f"Could not launch Chromium: {exc}")

        context = None
        try:
            context_kwargs: dict = {}
            if (flow.device or "desktop") == "mobile_web":
                try:
                    context_kwargs = dict(p.devices[_MOBILE_DEVICE])
                except Exception:
                    context_kwargs = {
                        "viewport": {"width": 390, "height": 844},
                        "is_mobile": True,
                        "has_touch": True,
                        "user_agent": (
                            "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 "
                            "Mobile/15E148 Safari/604.1"
                        ),
                    }
            context = await browser.new_context(**context_kwargs)

            def _record_dl(entry: Any) -> None:
                dl_events.append(
                    {
                        "event": _entry_event_name(entry),
                        "data": entry if isinstance(entry, dict) else {"value": entry},
                        "step_index": state["step"],
                        "ts": time.time(),
                    }
                )

            await context.expose_binding("__fluxRecordDL", lambda _source, entry: _record_dl(entry))
            await context.add_init_script(_INIT_SCRIPT)

            page = await context.new_page()

            def _on_request(request: Any) -> None:
                try:
                    url = request.url
                    vendor = _match_vendor(url, vendors)
                    if vendor is None and not _matches_watch(url):
                        return
                    post_data = request.post_data
                    if post_data and len(post_data) > _POST_DATA_TRUNCATE:
                        post_data = post_data[:_POST_DATA_TRUNCATE]
                    params = _parse_beacon_params(url, request.method, post_data)
                    beacons.append(
                        {
                            "vendor_id": str(vendor.id) if vendor else None,
                            "vendor_slug": (vendor.slug if vendor else None),
                            "url": url[:2000],
                            "method": request.method,
                            "resource_type": request.resource_type,
                            "post_data": post_data,
                            "params": params,
                            "step_index": state["step"],
                        }
                    )
                except Exception:
                    logger.debug("failed to capture request", exc_info=True)

            page.on("request", _on_request)

            base_url = (flow.base_url or "").strip()
            steps = flow.steps or []
            step_results: list[dict] = []
            crashed = False

            for idx, step in enumerate(steps):
                state["step"] = idx
                action = (step.get("action") or "").lower()
                label = step.get("label") or action or f"step {idx}"
                dl_before = len(dl_events)
                beacon_before = len(beacons)
                step_ok = True
                step_err: str | None = None

                try:
                    if action == "navigate":
                        target = _resolve_url(base_url, step.get("url"))
                        if not is_safe_http_url(target):
                            raise ValueError(f"navigation to a disallowed URL was blocked: {target[:200]}")
                        await page.goto(target, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
                    elif action == "click":
                        await page.click(step["selector"], timeout=_CLICK_TIMEOUT_MS)
                    elif action == "type":
                        await page.fill(step["selector"], step.get("text") or "", timeout=_CLICK_TIMEOUT_MS)
                    elif action == "wait":
                        ms = min(int(step.get("ms") or 0), _WAIT_CAP_MS)
                        await page.wait_for_timeout(ms)
                    else:
                        step_ok = False
                        step_err = f"unknown action '{action}'"
                except Exception as exc:
                    step_ok = False
                    step_err = str(exc)
                    crashed = True

                step_results.append(
                    {
                        "step_index": idx,
                        "action": action,
                        "label": label,
                        "ok": step_ok,
                        "error": step_err,
                        "datalayer_events": dl_events[dl_before:],
                        "beacons": beacons[beacon_before:],
                    }
                )

                if not step_ok:
                    # A hard failure (bad selector, nav error) stops the flow —
                    # later steps depend on this one's state.
                    break

            return ExecutionResult(
                ok=not crashed,
                error=None,
                datalayer_events=dl_events,
                beacons=beacons,
                step_results=step_results,
            )
        finally:
            try:
                if context is not None:
                    await context.close()
            finally:
                await browser.close()


def _resolve_url(base_url: str, url: str | None) -> str:
    """Resolve a step URL against the flow's base_url.

    Absolute URLs pass through; relative/empty ones join to base_url.
    """
    u = (url or "").strip()
    if not u:
        return base_url
    if u.startswith("http://") or u.startswith("https://"):
        return u
    if base_url:
        return urllib.parse.urljoin(base_url if base_url.endswith("/") else base_url + "/", u.lstrip("/"))
    return u
