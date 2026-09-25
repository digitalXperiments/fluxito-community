"""
Live Tag Test — MCP Tool
=========================

Registers the ``live_tag_test`` tool with the MCP server.

This tool guides Claude through browser-based live tag testing using
Claude's computer-use capability.  It does NOT run a server-side browser —
instead it generates structured test plans that Claude follows while using
its own browser control, then analyzes the captured network requests.

Actions
-------
  get_test_plan         — Generate step-by-step browsing guide for a URL
  get_sdr_context       — Get SDR events expected for a URL
  analyze_captures      — Analyze network request captures from Claude browsing
  start_session         — Create a new live test session
  finish_session        — Finalize a session and compute summary
  list_test_plans       — List saved test plans
  save_test_plan        — Save a reusable test plan

Typical Workflow
----------------
  1. live_tag_test(action="get_test_plan", url="https://shop.example.com/product/123")
     → Claude gets step-by-step instructions for what to do in the browser
     → Claude opens browser, navigates, captures network requests

  2. live_tag_test(action="analyze_captures", session_id=..., network_captures=[...])
     → Claude provides the raw network requests it captured
     → Tool validates each against Rule Books + SDR

  3. save_audit_result(action="save", audit_type="live_tag_test", ...)
     → Findings saved to Fluxito UI
"""

from __future__ import annotations

import logging

import app.app_state as state
from app.tools.shared_helpers import get_current_user

logger = logging.getLogger(__name__)


def _err(error_type: str, message: str, **extra) -> dict:
    out = {"error": True, "error_type": error_type, "message": message}
    out.update(extra)
    return out


def _get_project_id() -> str | None:
    try:
        proj = state.current_project_ctx.get()
        return proj.project_id if proj else None
    except LookupError:
        return None


# URL patterns Claude should watch for in the network tab, covering all 20 platforms
_NETWORK_PATTERNS_TO_WATCH = [
    "google-analytics.com/g/collect",
    "analytics.google.com/g/collect",
    "facebook.com/tr",
    "connect.facebook.net",
    "analytics.tiktok.com",
    "sc-static.net/scevent",
    "tr.snapchat.com",
    "ct.pinterest.com",
    "ads-twitter.com",
    "bat.bing.com",
    "static.criteo.net",
    "api.amplitude.com",
    "api.segment.io",
    "api.mixpanel.com",
    "app.posthog.com",
    "t.posthog.com",
    "snap.licdn.com",
    "googleadservices.com",
    "doubleclick.net",
    "demdex.net",
    "omtrdc.net",
]


def register_live_tag_test_tools(mcp_server) -> None:
    @mcp_server.tool("live_tag_test")
    async def live_tag_test(
        action: str,
        # get_test_plan / get_sdr_context
        url: str | None = None,
        # analyze_captures
        session_id: str | None = None,
        network_captures: list[dict] | None = None,
        # finish_session
        notes: str | None = None,
        # save/list test plans
        plan: dict | None = None,
        plan_id: str | None = None,
    ) -> dict:
        """
        Live Tag Test — browser-based tag testing via Claude computer-use.

        IMPORTANT: This tool does NOT run a server-side browser.  It provides
        Claude with structured test plans and analysis utilities to use while
        controlling a browser via computer-use.

        ──────────────────────────────────────────────────────────────────────

        WORKFLOW ACTIONS

          get_test_plan
            Generate a step-by-step browsing guide for a URL.
            Call BEFORE opening the browser.
            params: url (required)
            returns: {session_id, interaction_steps, network_patterns_to_watch,
                      capture_instructions, sdr_context, rule_books_active}

          get_sdr_context
            Get SDR-defined expected events for a URL (filtered by URL pattern).
            params: url (optional)

          start_session
            Create a live test session (optional — get_test_plan creates one).
            params: url (required)

        CAPTURE ANALYSIS

          analyze_captures
            Validate network requests captured during browser testing.

            ``network_captures`` format:
            [
              {
                "step": "clicked Add to Cart button",
                "requests": [
                  {
                    "url": "https://www.google-analytics.com/g/collect?...",
                    "method": "POST",
                    "body": "v=2&tid=G-XXXXX&en=add_to_cart&ep.currency=USD..."
                  },
                  ...
                ]
              },
              ...
            ]

            params: session_id, network_captures (both required)
            returns: {findings, score, critical, warning, passed, per_platform, sdr_gaps}

          finish_session
            Finalize session and get summary.
            params: session_id (required), notes (optional)

        SAVED PLANS

          list_test_plans  — List saved test plans for the project.
          save_test_plan   — Save a reusable test plan.
                             params: plan (dict with name, url_patterns, interaction_steps)

        ──────────────────────────────────────────────────────────────────────

        After analysis, always call:
          save_audit_result(action="save", audit_type="live_tag_test", ...)
        to persist findings to the Fluxito UI.
        """
        user = get_current_user()
        if not user:
            return _err("unauthenticated", "No active session.")

        action_norm = (action or "").strip().lower()
        project_id = _get_project_id()

        # ── get_test_plan ────────────────────────────────────────────────────
        if action_norm == "get_test_plan":
            if not url:
                return _err("bad_request", "url is required for get_test_plan.")

            from app.tag_testing.live_test.sdr_context import get_sdr_context_for_url
            from app.tag_testing.live_test.session import create_session
            from app.tag_testing.rule_books.manifest import list_platforms_summary

            # Load SDR context for the URL
            sdr_ctx: dict = {}
            if project_id:
                sdr_ctx = await get_sdr_context_for_url(project_id, url)

            # Create session
            sid = await create_session(project_id or "anonymous", url, sdr_ctx)

            # Build platform awareness from SDR destinations + all active Rule Books
            active_platforms = [rb["platform"] for rb in list_platforms_summary()]
            sdr_platforms = list(
                {dest for ev in sdr_ctx.get("events", []) for dest in ev.get("destinations", [])}
            )

            # Build interaction steps based on URL type heuristic
            interaction_steps = _build_interaction_steps(url, sdr_ctx)

            return {
                "session_id": sid,
                "url": url,
                "sdr_context": sdr_ctx,
                "rule_books_active": active_platforms,
                "sdr_platforms": sdr_platforms,
                "pre_conditions": [
                    "Open Chrome DevTools (F12) → Network tab before navigating",
                    "Clear network log (circle with line icon)",
                    "Disable cache (Network tab → 'Disable cache')",
                    "Set filter to 'All' (not just XHR)",
                ],
                "capture_instructions": {
                    "network_patterns_to_watch": _NETWORK_PATTERNS_TO_WATCH,
                    "what_to_extract": (
                        "For each matching request: capture the full URL, HTTP method, "
                        "and request body (if POST). Group requests by the user action "
                        "that triggered them."
                    ),
                    "format_hint": (
                        'Pass captures as: [{"step": "action description", '
                        '"requests": [{"url": "...", "method": "POST", "body": "..."}]}]'
                    ),
                },
                "interaction_steps": interaction_steps,
                "next_step": (
                    f"Navigate to {url} in the browser using computer-use, "
                    "follow the interaction_steps above, capture network requests, "
                    f"then call live_tag_test(action='analyze_captures', session_id='{sid}', network_captures=[...])"
                ),
            }

        # ── get_sdr_context ──────────────────────────────────────────────────
        if action_norm == "get_sdr_context":
            if not project_id:
                return {
                    "events": [],
                    "total": 0,
                    "note": "No active project — SDR context is not available.",
                }
            from app.tag_testing.live_test.sdr_context import get_sdr_context_for_url

            return await get_sdr_context_for_url(project_id, url)

        # ── start_session ────────────────────────────────────────────────────
        if action_norm == "start_session":
            if not url:
                return _err("bad_request", "url is required for start_session.")
            from app.tag_testing.live_test.session import create_session

            sid = await create_session(project_id or "anonymous", url)
            return {"session_id": sid, "status": "active", "url": url}

        # ── analyze_captures ─────────────────────────────────────────────────
        if action_norm == "analyze_captures":
            if not network_captures:
                return _err(
                    "bad_request",
                    "network_captures is required for analyze_captures. "
                    "Provide the list of captured network requests from browser DevTools.",
                )

            from app.tag_testing.live_test.parser import parse_request
            from app.tag_testing.rule_books.manifest import get_rule_book
            from app.tag_testing.rule_books.validator import compute_score
            from app.tag_testing.rule_books.validator import validate_payload as _validate

            # Load session if provided
            session = None
            sdr_ctx: dict = {}
            if session_id:
                from app.tag_testing.live_test.session import get_session

                session = await get_session(session_id)
                if session:
                    sdr_ctx = session.get("sdr_context") or {}

            # Load custom rules
            custom_rules = []
            if project_id:
                from app.tag_testing.rule_books.custom_rules import get_custom_rules

                custom_rules = await get_custom_rules(project_id)

            all_findings: list[dict] = []
            per_platform: dict = {}
            fired_events: set[str] = set()
            unmatched_requests: list[dict] = []

            # Process each capture group
            for capture_group in network_captures:
                step_label = capture_group.get("step") or "unknown step"
                requests = capture_group.get("requests") or []
                if isinstance(requests, dict):
                    requests = [requests]

                for req in requests:
                    req_url = req.get("url") or ""
                    req_method = req.get("method") or "GET"
                    req_body = req.get("body")

                    parsed = parse_request(req_url, req_method, req_body)
                    if not parsed:
                        unmatched_requests.append({"url": req_url[:120], "step": step_label})
                        continue

                    rb = get_rule_book(parsed.platform)
                    if parsed.event_name:
                        fired_events.add(f"{parsed.platform}:{parsed.event_name}")

                    if rb and parsed.event_name:
                        val = _validate(rb, parsed.event_name, parsed.params, custom_rules=custom_rules)
                        findings = val.as_dict()["findings"]
                    else:
                        findings = []

                    plat = parsed.platform
                    if plat not in per_platform:
                        per_platform[plat] = {
                            "platform": plat,
                            "requests_captured": 0,
                            "critical": 0,
                            "warning": 0,
                            "info": 0,
                            "passed": 0,
                            "events": [],
                        }
                    pp = per_platform[plat]
                    pp["requests_captured"] += 1
                    if parsed.event_name and parsed.event_name not in pp["events"]:
                        pp["events"].append(parsed.event_name)

                    for f in findings:
                        if f["status"] == "critical":
                            pp["critical"] += 1
                        elif f["status"] == "warning":
                            pp["warning"] += 1
                        elif f["status"] == "info":
                            pp["info"] += 1
                        elif f["status"] == "pass":
                            pp["passed"] += 1
                        all_findings.append(
                            {
                                **f,
                                "step": step_label,
                                "platform": plat,
                                "event": parsed.event_name,
                                "request_url": req_url[:80],
                            }
                        )

            # SDR compliance check: compare expected events vs fired events
            sdr_gaps: list[dict] = []
            for ev in sdr_ctx.get("events", []):
                ev_name = ev.get("event_name")
                ev_dests = ev.get("destinations") or []
                for dest in ev_dests:
                    key = f"{dest}:{ev_name}"
                    if key not in fired_events:
                        sdr_gaps.append(
                            {
                                "severity": "warning",
                                "message": f"SDR event '{ev_name}' expected on {dest} but not detected.",
                                "platform": dest,
                                "event": ev_name,
                                "source": "sdr",
                            }
                        )

            all_findings.extend(sdr_gaps)

            # Compute totals
            total_critical = sum(1 for f in all_findings if f.get("status") == "critical")
            total_warning = sum(1 for f in all_findings if f.get("status") == "warning")
            total_info = sum(1 for f in all_findings if f.get("status") == "info")
            total_passed = sum(1 for f in all_findings if f.get("status") == "pass")
            overall_score = compute_score(total_critical, total_warning, total_info, total_passed)

            # Persist to session
            if session_id:
                from app.tag_testing.live_test.session import update_session

                await update_session(
                    session_id,
                    {
                        "findings": all_findings,
                        "captures": network_captures,
                    },
                )

            return {
                "session_id": session_id,
                "overall_score": overall_score,
                "critical": total_critical,
                "warning": total_warning,
                "info": total_info,
                "passed": total_passed,
                "sdr_gaps": len(sdr_gaps),
                "unmatched_requests": len(unmatched_requests),
                "platforms_detected": list(per_platform.keys()),
                "per_platform": list(per_platform.values()),
                "findings": [f for f in all_findings if f.get("status") != "pass"],
                "save_hint": (
                    "Call save_audit_result(action='save', audit_type='live_tag_test', "
                    f"ltt_session_id='{session_id or ''}', score={overall_score}, "
                    "findings=[...]) to persist these results to the Fluxito UI."
                ),
            }

        # ── finish_session ───────────────────────────────────────────────────
        if action_norm == "finish_session":
            if not session_id:
                return _err("bad_request", "session_id is required for finish_session.")
            from app.tag_testing.live_test.session import finish_session, update_session

            if notes:
                await update_session(session_id, {"notes": notes})
            return await finish_session(session_id)

        # ── list_test_plans ──────────────────────────────────────────────────
        if action_norm == "list_test_plans":
            if not project_id:
                return {"plans": [], "count": 0, "note": "No active project."}
            return await _list_test_plans(project_id)

        # ── save_test_plan ───────────────────────────────────────────────────
        if action_norm == "save_test_plan":
            if not project_id:
                return _err("no_active_project", "No active project. Call set_active_project first.")
            if not plan:
                return _err("bad_request", "plan (dict) is required for save_test_plan.")
            return await _save_test_plan(project_id, user.user_id, plan)

        return _err(
            "bad_request",
            f"Unknown action '{action}'. Valid: get_test_plan, get_sdr_context, "
            "start_session, analyze_captures, finish_session, list_test_plans, save_test_plan.",
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_interaction_steps(url: str, sdr_ctx: dict) -> list[str]:
    """Build interaction steps based on URL and SDR events."""
    steps = [f"1. Navigate to: {url}"]
    sdr_events = sdr_ctx.get("events") or []

    # Infer page type from URL
    url_lower = url.lower()
    if any(x in url_lower for x in ("/product/", "/p/", "/item/", "/pd/")):
        steps += [
            "2. Wait for the page to fully load (all scripts, no pending requests)",
            "3. Capture page_view / view_item events from network tab",
            "4. Click 'Add to Cart' button",
            "5. Capture add_to_cart / AddToCart events from network tab",
        ]
    elif any(x in url_lower for x in ("/cart", "/basket", "/bag")):
        steps += [
            "2. Wait for the page to fully load",
            "3. Capture cart view events from network tab",
            "4. Click 'Proceed to Checkout' or 'Begin Checkout' button",
            "5. Capture begin_checkout / InitiateCheckout events",
        ]
    elif any(x in url_lower for x in ("/checkout",)):
        steps += [
            "2. Wait for the page to fully load",
            "3. Capture checkout_start events",
            "4. Fill in shipping info (use test data)",
            "5. Capture shipping info events",
            "6. Fill in payment info (use test card)",
            "7. Capture payment info events",
            "8. Complete purchase (if using test environment)",
            "9. Capture purchase / Order Completed events",
        ]
    elif any(x in url_lower for x in ("/category/", "/c/", "/collection/", "/search")):
        steps += [
            "2. Wait for the page to fully load",
            "3. Capture view_item_list / Product List Viewed events",
            "4. Click on a product",
            "5. Capture select_item / Product Clicked events",
        ]
    else:
        steps += [
            "2. Wait for the page to fully load",
            "3. Capture page_view and any auto-fired events",
        ]

    # Add SDR-specific steps
    if sdr_events:
        steps.append("")
        steps.append("SDR-expected events to verify:")
        for ev in sdr_events[:8]:
            steps.append(
                f"  ✓ Verify '{ev['event_name']}' fires with: "
                + ", ".join(p["name"] for p in ev.get("parameters", [])[:4])
            )

    return steps


async def _list_test_plans(project_id: str) -> dict:
    """List saved test plans from DB."""
    try:
        from sqlalchemy import text

        async with state.db_session_factory() as db:
            result = await db.execute(
                text("""
                    SELECT id, name, url_patterns, interaction_steps, expected_platforms, created_at
                    FROM ltt_test_plans
                    WHERE project_id = :pid
                    ORDER BY created_at DESC
                    LIMIT 50
                """),
                {"pid": project_id},
            )
            rows = result.mappings().all()
        return {"plans": [dict(r) for r in rows], "count": len(rows)}
    except Exception as e:
        logger.warning(f"list_test_plans failed: {e}")
        return {"plans": [], "count": 0, "error": str(e)}


async def _save_test_plan(project_id: str, user_id: str, plan: dict) -> dict:
    """Save a test plan to DB."""
    import json

    try:
        from sqlalchemy import text

        async with state.db_session_factory() as db:
            await db.execute(
                text("""
                    INSERT INTO ltt_test_plans
                        (id, project_id, name, url_patterns, interaction_steps, expected_platforms,
                         created_by, created_at)
                    VALUES
                        (gen_random_uuid(), :pid, :name, :url_patterns::jsonb,
                         :steps::jsonb, :platforms::jsonb, :uid, NOW())
                """),
                {
                    "pid": project_id,
                    "name": plan.get("name") or "Untitled Plan",
                    "url_patterns": json.dumps(plan.get("url_patterns") or []),
                    "steps": json.dumps(plan.get("interaction_steps") or []),
                    "platforms": json.dumps(plan.get("expected_platforms") or []),
                    "uid": user_id,
                },
            )
            await db.commit()
        return {"success": True, "message": "Test plan saved."}
    except Exception as e:
        logger.error(f"save_test_plan failed: {e}", exc_info=True)
        return {"error": True, "error_type": "db_error", "message": str(e)}
