"""
Rule Books — Payload Validator
================================

Core validation logic.  All functions are stateless pure functions —
they receive Rule Book objects and payload dicts and return structured
FindingResult lists.  The MCP tool layer (tag_rulebook_tools.py) applies
DB-backed custom rules on top.

Public API
----------
  validate_payload(rule_book, event_name, payload, custom_rules=None)
      → ValidationResult

  compute_score(critical, warning, info, passed) → int  [0–100]
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.tag_testing.rule_books.base import (
    GlobalRule,
    ParamSpec,
    RuleBook,
    Severity,
)

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class FindingResult:
    param: str | None  # None for event-level / global findings
    status: Severity | str  # "pass" | "critical" | "warning" | "info"
    message: str
    remediation: str = ""
    rule_id: str = ""
    source: str = "rule_book"  # "rule_book" | "custom" | "sdr" | "heuristic"
    expected: Any = None
    actual: Any = None

    def as_dict(self) -> dict:
        d: dict = {
            "param": self.param,
            "status": self.status,
            "message": self.message,
            "source": self.source,
        }
        if self.remediation:
            d["remediation"] = self.remediation
        if self.rule_id:
            d["rule_id"] = self.rule_id
        if self.expected is not None:
            d["expected"] = self.expected
        if self.actual is not None:
            d["actual"] = self.actual
        return d


@dataclass
class ValidationResult:
    platform: str
    event_name: str
    overall_status: str  # "pass" | "fail" | "warning" | "no_spec"
    score: int  # 0–100
    findings: list[FindingResult] = field(default_factory=list)
    critical_count: int = 0
    warning_count: int = 0
    info_count: int = 0
    passed_count: int = 0
    spec_found: bool = True
    notes: str = ""

    def as_dict(self) -> dict:
        return {
            "platform": self.platform,
            "event": self.event_name,
            "status": self.overall_status,
            "score": self.score,
            "spec_found": self.spec_found,
            "critical": self.critical_count,
            "warning": self.warning_count,
            "info": self.info_count,
            "passed": self.passed_count,
            "notes": self.notes,
            "findings": [f.as_dict() for f in self.findings],
        }


# ---------------------------------------------------------------------------
# Score formula
# ---------------------------------------------------------------------------

# Weight per finding severity
_SCORE_DEDUCTIONS = {"critical": 25, "warning": 8, "info": 1}
_MAX_SCORE = 100


def compute_score(critical: int, warning: int, info: int, passed: int) -> int:
    """
    Compute a 0–100 score.

    Deductions: critical×25, warning×8, info×1.
    Score cannot go below 0.  Pure pass (0 failures) = 100.
    """
    deduction = (
        critical * _SCORE_DEDUCTIONS["critical"]
        + warning * _SCORE_DEDUCTIONS["warning"]
        + info * _SCORE_DEDUCTIONS["info"]
    )
    return max(0, _MAX_SCORE - deduction)


# ---------------------------------------------------------------------------
# Main validate_payload function
# ---------------------------------------------------------------------------


def validate_payload(
    rule_book: RuleBook,
    event_name: str,
    payload: dict,
    custom_rules: list[dict] | None = None,
) -> ValidationResult:
    """
    Validate a tag/event payload against a Rule Book's EventSpec.

    ``payload``      — flat dict of param_name → value (as sent in the
                       network request or extracted from a GTM tag).
    ``custom_rules`` — optional list of project-specific custom rule dicts
                       as returned by custom_rules.get_custom_rules().

    Returns a ValidationResult with per-param findings and an overall score.
    """
    platform = rule_book.platform
    findings: list[FindingResult] = []

    # Find the EventSpec for this event
    event_spec = rule_book.find_event(event_name)

    if event_spec is None:
        # Unknown event — still apply global rules and custom rules, but
        # note that we have no spec to validate against
        result = ValidationResult(
            platform=platform,
            event_name=event_name,
            overall_status="no_spec",
            score=100,
            spec_found=False,
            notes=(
                f"No event spec found for '{event_name}' in the {rule_book.display_name} "
                "Rule Book. Only global rules and custom rules were checked."
            ),
        )
        _apply_global_rules(rule_book.global_rules, payload, findings)
        if custom_rules:
            _apply_custom_rules(custom_rules, event_name, platform, payload, findings)
        _tally(result, findings)
        return result

    # --- Required params ---
    for param_spec in event_spec.required_params:
        _check_param(param_spec, payload, findings, default_severity=event_spec.severity_if_missing_required)

    # --- Recommended params ---
    for param_spec in event_spec.recommended_params:
        _check_param(param_spec, payload, findings, default_severity="warning")

    # --- Global rules ---
    _apply_global_rules(rule_book.global_rules, payload, findings)

    # --- Custom rules ---
    if custom_rules:
        _apply_custom_rules(custom_rules, event_name, platform, payload, findings)

    result = ValidationResult(
        platform=platform,
        event_name=event_name,
        overall_status="pass",  # updated by _tally
        score=100,
        spec_found=True,
        notes=event_spec.notes,
    )
    _tally(result, findings)
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _check_param(
    param_spec: ParamSpec,
    payload: dict,
    findings: list[FindingResult],
    *,
    default_severity: Severity,
) -> None:
    """Check a single ParamSpec against the payload and append findings."""
    # Presence check
    # We treat any truthy value — or explicit 0/False/empty-list — as present.
    # None and missing key are treated as absent.
    value = payload.get(param_spec.name)

    if value is None:
        # Check aliases / alternate key spellings (e.g. "ecomm_prodid" vs "content_ids")
        # For now: exact match only — Rule Book authors use canonical names.
        severity = default_severity if param_spec.required else "warning"
        findings.append(
            FindingResult(
                param=param_spec.name,
                status=severity,
                message=(
                    f"'{param_spec.name}' is "
                    f"{'required' if param_spec.required else 'recommended'} "
                    f"but not found in the payload."
                ),
                remediation=param_spec.notes,
                rule_id=f"{param_spec.name}.presence",
                expected=f"{param_spec.type} value",
                actual=None,
            )
        )
        return

    # Presence pass
    passed_finding = FindingResult(
        param=param_spec.name,
        status="pass",
        message=f"'{param_spec.name}' is present.",
        actual=_safe_truncate(value),
    )

    # Value constraint checks (only when check_value or there are constraints)
    if param_spec.allowed_values:
        str_val = str(value)
        if str_val not in param_spec.allowed_values:
            findings.append(
                FindingResult(
                    param=param_spec.name,
                    status="warning",
                    message=(
                        f"'{param_spec.name}' has value '{str_val}' which is not in "
                        f"the allowed list: {list(param_spec.allowed_values)}."
                    ),
                    remediation=param_spec.notes,
                    rule_id=f"{param_spec.name}.allowed_values",
                    expected=list(param_spec.allowed_values),
                    actual=str_val,
                )
            )
            return
    elif param_spec.regex:
        str_val = str(value)
        if not re.fullmatch(param_spec.regex, str_val):
            findings.append(
                FindingResult(
                    param=param_spec.name,
                    status="warning",
                    message=(
                        f"'{param_spec.name}' value '{_safe_truncate(str_val, 40)}' "
                        f"does not match expected pattern '{param_spec.regex}'."
                    ),
                    remediation=param_spec.notes,
                    rule_id=f"{param_spec.name}.regex",
                    expected=f"matches /{param_spec.regex}/",
                    actual=_safe_truncate(str_val, 40),
                )
            )
            return

    if param_spec.min_value is not None or param_spec.max_value is not None:
        try:
            num = float(value)
            if param_spec.min_value is not None and num < param_spec.min_value:
                findings.append(
                    FindingResult(
                        param=param_spec.name,
                        status="warning",
                        message=(
                            f"'{param_spec.name}' value {num} is below the minimum "
                            f"expected value of {param_spec.min_value}."
                        ),
                        rule_id=f"{param_spec.name}.min_value",
                        expected=f">= {param_spec.min_value}",
                        actual=num,
                    )
                )
                return
            if param_spec.max_value is not None and num > param_spec.max_value:
                findings.append(
                    FindingResult(
                        param=param_spec.name,
                        status="warning",
                        message=(
                            f"'{param_spec.name}' value {num} exceeds the maximum "
                            f"expected value of {param_spec.max_value}."
                        ),
                        rule_id=f"{param_spec.name}.max_value",
                        expected=f"<= {param_spec.max_value}",
                        actual=num,
                    )
                )
                return
        except (TypeError, ValueError):
            pass  # value is not numeric — skip range check

    findings.append(passed_finding)


def _apply_global_rules(
    global_rules: tuple[GlobalRule, ...],
    payload: dict,
    findings: list[FindingResult],
) -> None:
    """
    Apply GlobalRules to the payload.

    ``must_be_present=True``  → fail if detection_hint NOT found in payload values.
    ``must_be_present=False`` → fail if detection_hint IS found in payload values.
    """
    payload_str = " ".join(str(v) for v in payload.values())
    for rule in global_rules:
        if not rule.detection_hint:
            continue  # No automated check possible — skip
        found = rule.detection_hint.lower() in payload_str.lower()
        if (rule.must_be_present and not found) or (not rule.must_be_present and found):
            findings.append(
                FindingResult(
                    param=None,
                    status=rule.severity,
                    message=rule.description,
                    remediation=rule.remediation,
                    rule_id=rule.rule_id,
                    source="rule_book",
                )
            )


def _apply_custom_rules(
    custom_rules: list[dict],
    event_name: str,
    platform: str,
    payload: dict,
    findings: list[FindingResult],
) -> None:
    """
    Apply project-specific custom rules.

    Each custom rule dict has the shape from tag_custom_rules:
    {
      rule_id, platform, event, name, description,
      required_params: ["param_name", ...],
      forbidden_params: ["param_name", ...],
      param_assertions: [{param, op, value, message}],
      severity, remediation
    }
    """
    for rule in custom_rules:
        # Scope check: rule applies if platform matches ('*' = all) AND event matches
        rule_platform = rule.get("platform") or "*"
        rule_event = rule.get("event") or "*"
        if rule_platform != "*" and rule_platform != platform:
            continue
        if rule_event != "*" and rule_event.lower() != event_name.lower():
            continue

        severity = rule.get("severity") or "warning"
        rule_id = rule.get("rule_id") or "custom"
        remediation = rule.get("remediation") or rule.get("description") or ""

        # Required params
        for param_name in rule.get("required_params") or []:
            if payload.get(param_name) is None:
                findings.append(
                    FindingResult(
                        param=param_name,
                        status=severity,
                        message=f"Custom rule '{rule.get('name')}': '{param_name}' is required but missing.",
                        remediation=remediation,
                        rule_id=rule_id,
                        source="custom",
                    )
                )

        # Forbidden params
        for param_name in rule.get("forbidden_params") or []:
            if payload.get(param_name) is not None:
                findings.append(
                    FindingResult(
                        param=param_name,
                        status=severity,
                        message=f"Custom rule '{rule.get('name')}': '{param_name}' must not be present.",
                        remediation=remediation,
                        rule_id=rule_id,
                        source="custom",
                    )
                )

        # Param assertions
        for assertion in rule.get("param_assertions") or []:
            param_name = assertion.get("param")
            op = assertion.get("op")
            expected_val = assertion.get("value")
            msg = assertion.get("message") or f"Assertion '{op}' on '{param_name}' failed."
            actual_val = payload.get(param_name)

            if not _eval_assertion(op, actual_val, expected_val):
                findings.append(
                    FindingResult(
                        param=param_name,
                        status=severity,
                        message=f"Custom rule '{rule.get('name')}': {msg}",
                        remediation=remediation,
                        rule_id=rule_id,
                        source="custom",
                        expected=expected_val,
                        actual=_safe_truncate(actual_val),
                    )
                )


def _eval_assertion(op: str | None, actual: Any, expected: Any) -> bool:
    """
    Evaluate a param assertion.

    Supported ops: eq, neq, gt, gte, lt, lte, in, not_in, regex, is_array,
                   is_string, is_number, truthy, falsy.
    Returns True if the assertion PASSES (i.e. no violation).
    """
    op = (op or "eq").strip().lower()
    try:
        if op == "eq":
            return str(actual) == str(expected)
        if op == "neq":
            return str(actual) != str(expected)
        if op == "gt":
            return float(actual) > float(expected)
        if op == "gte":
            return float(actual) >= float(expected)
        if op == "lt":
            return float(actual) < float(expected)
        if op == "lte":
            return float(actual) <= float(expected)
        if op == "in":
            return str(actual) in (expected if isinstance(expected, list) else [str(expected)])
        if op == "not_in":
            return str(actual) not in (expected if isinstance(expected, list) else [str(expected)])
        if op == "regex":
            return bool(re.search(str(expected), str(actual)))
        if op == "is_array":
            return isinstance(actual, list)
        if op == "is_string":
            return isinstance(actual, str)
        if op == "is_number":
            float(actual)
            return True
        if op == "truthy":
            return bool(actual)
        if op == "falsy":
            return not bool(actual)
    except (TypeError, ValueError):
        return False
    return True  # unknown op → don't fail


def _tally(result: ValidationResult, findings: list[FindingResult]) -> None:
    """Compute counters, score, and overall_status from findings list."""
    result.findings = findings
    critical = sum(1 for f in findings if f.status == "critical")
    warning = sum(1 for f in findings if f.status == "warning")
    info = sum(1 for f in findings if f.status == "info")
    passed = sum(1 for f in findings if f.status == "pass")

    result.critical_count = critical
    result.warning_count = warning
    result.info_count = info
    result.passed_count = passed
    result.score = compute_score(critical, warning, info, passed)

    if critical > 0:
        result.overall_status = "fail"
    elif warning > 0:
        result.overall_status = "warning"
    elif result.spec_found:
        result.overall_status = "pass"
    # else stays "no_spec"


def _safe_truncate(value: Any, length: int = 200) -> Any:
    """Truncate long string values for display in findings."""
    if isinstance(value, str) and len(value) > length:
        return value[:length] + "…"
    return value
