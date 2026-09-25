"""
Tag Rule Book — MCP Tool
=========================

Registers the ``tag_rulebook`` tool with the MCP server.

This tool gives Claude (or any AI client) expert-level knowledge about
tracking pixel / analytics tag specifications, enabling structured audits
of GTM containers and live network captures without needing the AI to carry
all platform knowledge in its training weights.

Actions
-------
  list_platforms         — list all supported Rule Book platforms
  get_platform_spec      — full event + param spec for one platform
  get_event_spec         — single event spec from a platform
  validate_payload       — validate a tag/event payload → per-param pass/fail
  identify_tag_type      — identify which platform a GTM tag dict belongs to
  audit_against_rulebooks— validate all GTM tags in a container
  list_custom_rules      — list project-specific custom rules
  save_custom_rule       — create/update a custom rule
  delete_custom_rule     — soft-delete a custom rule
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


def _no_project() -> dict:
    return _err("no_active_project", "No active project. Call set_active_project first.")


def _get_project_id() -> str | None:
    try:
        proj = state.current_project_ctx.get()
        return proj.project_id if proj else None
    except LookupError:
        return None


def register_tag_rulebook_tools(mcp_server) -> None:
    @mcp_server.tool("tag_rulebook")
    async def tag_rulebook(
        action: str,
        # identify / validate / spec actions
        platform: str | None = None,
        event_name: str | None = None,
        payload: dict | None = None,
        tag_dict: dict | None = None,
        # audit_against_rulebooks args
        tags: list[dict] | None = None,
        include_passed: bool = False,
        # custom rule args
        rule: dict | None = None,
        rule_id: str | None = None,
    ) -> dict:
        """
        Tag Rule Book — expert-level platform specs and payload validation.

        Gives Claude structured knowledge about tracking pixel / analytics tag
        requirements across 20 platforms without relying on training weights.
        Connector-independent — works even without a GTM connection.

        ──────────────────────────────────────────────────────────────────────

        DISCOVERY
          list_platforms
            List all supported Rule Book platforms with event counts.
            No params required.

          get_platform_spec
            Full specification for one platform: events + required/recommended
            params + global rules.
            params: platform (required)

          get_event_spec
            Single event spec from a platform.
            params: platform, event_name (both required)

        IDENTIFICATION
          identify_tag_type
            Identify which tracking platform a GTM tag dict belongs to
            using a 3-tier approach: GTM type code → HTML pattern → name hint.
            params: tag_dict (required — raw GTM API tag object)

        VALIDATION
          validate_payload
            Validate a tag/event payload against the platform's Rule Book.
            Returns per-param findings with pass/fail/warning and remediation.
            Works for both GTM config params AND live network captures.
            params: platform, event_name, payload (all required)

        BULK AUDIT
          audit_against_rulebooks
            Given a list of GTM tag dicts, identify each tag's platform,
            extract its parameters, and validate against the Rule Book.
            Returns per-platform scores + all findings.
            params: tags (list of GTM tag dicts, required)
                    include_passed (bool, default False — omit passing params)

        CUSTOM RULES
          list_custom_rules   — Project-specific rules (no params required)
          save_custom_rule    — Create/update a rule.
                                params: rule (dict with rule_id, name, platform,
                                event, severity, required_params, forbidden_params,
                                param_assertions, remediation)
          delete_custom_rule  — Soft-delete.
                                params: rule_id

        ──────────────────────────────────────────────────────────────────────

        Typical audit workflow:
          1. tag_rulebook(action="list_platforms")
             → see which Rule Books are available
          2. tag_rulebook(action="audit_against_rulebooks", tags=[...])
             → pass raw GTM list_tags output; get bulk findings + scores
          3. tag_rulebook(action="validate_payload", platform=..., event_name=..., payload=...)
             → validate a specific live network capture payload
        """
        user = get_current_user()
        if not user:
            return _err("unauthenticated", "No active session.")

        action_norm = (action or "").strip().lower()

        # ── list_platforms ───────────────────────────────────────────────────
        if action_norm == "list_platforms":
            from app.tag_testing.rule_books.manifest import list_platforms_summary

            project_id = _get_project_id()
            summaries = list_platforms_summary()

            # Annotate custom rule counts if project is active
            if project_id:
                from app.tag_testing.rule_books.custom_rules import get_custom_rules

                custom_rules = await get_custom_rules(project_id)
                by_platform: dict[str, int] = {}
                for r in custom_rules:
                    p = r.get("platform") or "*"
                    by_platform[p] = by_platform.get(p, 0) + 1
                for s in summaries:
                    s["custom_rule_count"] = by_platform.get(s["platform"], 0)
            else:
                for s in summaries:
                    s["custom_rule_count"] = 0

            return {
                "platforms": summaries,
                "total": len(summaries),
                "hint": "Call get_platform_spec(platform=...) for the full event spec.",
            }

        # ── get_platform_spec ────────────────────────────────────────────────
        if action_norm == "get_platform_spec":
            if not platform:
                return _err("bad_request", "platform is required for get_platform_spec.")
            from app.tag_testing.rule_books.manifest import get_rule_book

            rb = get_rule_book(platform)
            if not rb:
                return _err(
                    "not_found",
                    f"No Rule Book found for platform '{platform}'.",
                    available_platforms=[rb.platform for rb in _all_books()],
                )
            return rb.serialize(include_events=True)

        # ── get_event_spec ───────────────────────────────────────────────────
        if action_norm == "get_event_spec":
            if not platform:
                return _err("bad_request", "platform is required for get_event_spec.")
            if not event_name:
                return _err("bad_request", "event_name is required for get_event_spec.")
            from app.tag_testing.rule_books.base import _serialize_event
            from app.tag_testing.rule_books.manifest import get_rule_book

            rb = get_rule_book(platform)
            if not rb:
                return _err("not_found", f"No Rule Book found for platform '{platform}'.")
            ev = rb.find_event(event_name)
            if not ev:
                available = [e.event_name for e in rb.events]
                return _err(
                    "not_found",
                    f"Event '{event_name}' not found in the {rb.display_name} Rule Book.",
                    available_events=available,
                )
            return {
                "platform": platform,
                "display_name": rb.display_name,
                "event": _serialize_event(ev),
            }

        # ── identify_tag_type ────────────────────────────────────────────────
        if action_norm == "identify_tag_type":
            if not tag_dict:
                return _err("bad_request", "tag_dict is required for identify_tag_type.")
            from app.tag_testing.rule_books.identifier import identify_tag

            result = identify_tag(tag_dict)
            return result.as_dict()

        # ── validate_payload ─────────────────────────────────────────────────
        if action_norm == "validate_payload":
            if not platform:
                return _err("bad_request", "platform is required for validate_payload.")
            if not event_name:
                return _err("bad_request", "event_name is required for validate_payload.")
            if payload is None:
                return _err("bad_request", "payload (dict) is required for validate_payload.")

            from app.tag_testing.rule_books.manifest import get_rule_book
            from app.tag_testing.rule_books.validator import validate_payload as _validate

            rb = get_rule_book(platform)
            if not rb:
                return _err("not_found", f"No Rule Book found for platform '{platform}'.")

            project_id = _get_project_id()
            custom_rules = []
            if project_id:
                from app.tag_testing.rule_books.custom_rules import get_custom_rules

                custom_rules = await get_custom_rules(project_id)

            result = _validate(rb, event_name, payload, custom_rules=custom_rules)
            return result.as_dict()

        # ── audit_against_rulebooks ──────────────────────────────────────────
        if action_norm == "audit_against_rulebooks":
            if not tags:
                return _err(
                    "bad_request",
                    "tags (list of GTM tag dicts) is required for audit_against_rulebooks. "
                    "Pass the output of tagmanager_read(action='list_tags').",
                )

            from app.tag_testing.rule_books.identifier import extract_params_from_tag, identify_tag
            from app.tag_testing.rule_books.manifest import get_rule_book
            from app.tag_testing.rule_books.validator import validate_payload as _validate

            project_id = _get_project_id()
            custom_rules = []
            if project_id:
                from app.tag_testing.rule_books.custom_rules import get_custom_rules

                custom_rules = await get_custom_rules(project_id)

            per_platform: dict[str, list] = {}
            unidentified: list[dict] = []

            for tag in tags:
                identification = identify_tag(tag)
                if not identification.matched_platform:
                    unidentified.append(
                        {
                            "tag_id": identification.gtm_tag_id,
                            "tag_name": identification.gtm_tag_name,
                            "reason": identification.match_reason,
                        }
                    )
                    continue

                plat = identification.matched_platform
                rb = get_rule_book(plat)
                if not rb:
                    continue

                tag_params = extract_params_from_tag(tag, plat)
                ev_name = identification.event_name or "unknown"

                # Only validate against spec if we detected an event name
                if ev_name and ev_name != "unknown":
                    val_result = _validate(rb, ev_name, tag_params, custom_rules=custom_rules)
                    findings = (
                        val_result.as_dict()["findings"]
                        if not include_passed
                        else val_result.as_dict()["findings"]
                    )
                    if not include_passed:
                        findings = [f for f in findings if f["status"] != "pass"]
                else:
                    # No event detected — just apply global rules
                    from app.tag_testing.rule_books.validator import (
                        FindingResult,
                        ValidationResult,
                        _apply_global_rules,
                        _tally,
                    )

                    dummy = ValidationResult(
                        platform=plat, event_name="config", overall_status="pass", score=100, spec_found=False
                    )
                    global_findings: list[FindingResult] = []
                    _apply_global_rules(rb.global_rules, tag_params, global_findings)
                    _tally(dummy, global_findings)
                    findings = [f.as_dict() for f in global_findings if include_passed or f.status != "pass"]
                    val_result = dummy

                if plat not in per_platform:
                    per_platform[plat] = {
                        "platform": plat,
                        "display_name": rb.display_name,
                        "spec_version": rb.spec_version,
                        "tags_audited": 0,
                        "critical": 0,
                        "warning": 0,
                        "info": 0,
                        "passed": 0,
                        "score": 100,
                        "findings": [],
                    }

                pp = per_platform[plat]
                pp["tags_audited"] += 1
                pp["critical"] += val_result.critical_count
                pp["warning"] += val_result.warning_count
                pp["info"] += val_result.info_count
                pp["passed"] += val_result.passed_count
                pp["findings"].extend(findings)

            # Compute per-platform scores
            from app.tag_testing.rule_books.validator import compute_score

            total_critical = total_warning = total_info = total_passed = 0
            for pp in per_platform.values():
                pp["score"] = compute_score(pp["critical"], pp["warning"], pp["info"], pp["passed"])
                total_critical += pp["critical"]
                total_warning += pp["warning"]
                total_info += pp["info"]
                total_passed += pp["passed"]

            overall_score = compute_score(total_critical, total_warning, total_info, total_passed)

            return {
                "tags_audited": len(tags),
                "platforms_detected": list(per_platform.keys()),
                "unidentified_tags": len(unidentified),
                "unidentified": unidentified,
                "overall_score": overall_score,
                "critical": total_critical,
                "warning": total_warning,
                "info": total_info,
                "passed": total_passed,
                "per_platform": list(per_platform.values()),
                "custom_rules_applied": len(custom_rules),
                "note": (
                    f"Audited {len(tags)} tags across {len(per_platform)} identified platforms. "
                    f"Overall score: {overall_score}/100. "
                    "Call save_audit_result(audit_type='tag_audit', ...) to persist these findings."
                ),
            }

        # ── list_custom_rules ────────────────────────────────────────────────
        if action_norm == "list_custom_rules":
            project_id = _get_project_id()
            if not project_id:
                return _no_project()
            from app.tag_testing.rule_books.custom_rules import get_custom_rules

            rules = await get_custom_rules(project_id)
            return {
                "custom_rules": rules,
                "count": len(rules),
                "hint": "Call save_custom_rule(rule={...}) to add a new rule.",
            }

        # ── save_custom_rule ─────────────────────────────────────────────────
        if action_norm == "save_custom_rule":
            project_id = _get_project_id()
            if not project_id:
                return _no_project()
            if not rule:
                return _err("bad_request", "rule (dict) is required for save_custom_rule.")
            from app.tag_testing.rule_books.custom_rules import save_custom_rule as _save

            return await _save(project_id, user.user_id, rule)

        # ── delete_custom_rule ───────────────────────────────────────────────
        if action_norm == "delete_custom_rule":
            project_id = _get_project_id()
            if not project_id:
                return _no_project()
            if not rule_id:
                return _err("bad_request", "rule_id is required for delete_custom_rule.")
            from app.tag_testing.rule_books.custom_rules import delete_custom_rule as _delete

            return await _delete(project_id, rule_id)

        return _err(
            "bad_request",
            f"Unknown action '{action}'. Valid actions: list_platforms, get_platform_spec, "
            "get_event_spec, identify_tag_type, validate_payload, audit_against_rulebooks, "
            "list_custom_rules, save_custom_rule, delete_custom_rule.",
        )


def _all_books():
    from app.tag_testing.rule_books.manifest import RULE_BOOK_MANIFEST

    return RULE_BOOK_MANIFEST
