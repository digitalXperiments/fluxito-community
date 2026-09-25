"""
run_audit auto-save
===================

Audit actions on the ``run_audit`` surface return findings to the AI client,
but historically nothing reached the Audit page unless the client also called
``save_audit_result`` — which clients rarely did, so audits run in chat never
showed up in the UI.

``autosave_audit_result`` closes that gap: after a successful audit action it
normalizes the result's issues/findings into AuditFinding rows and persists an
AuditRun for the active project. The saved run's id and URL are attached to the
tool response (``saved_to_fluxito``) so the client can link to it and knows not
to save again.
"""

from __future__ import annotations

import logging
import time
import uuid as _uuid
from typing import Any

import app.app_state as state
from app.tools.shared_helpers import get_current_user

logger = logging.getLogger(__name__)

# action-name prefix → AuditRun.audit_type. First match wins, so the more
# specific prefixes come first.
_AUDIT_TYPE_BY_PREFIX: tuple[tuple[str, str], ...] = (
    ("adobe_launch_", "tag_audit"),
    ("adobe_audit_consent_mode", "tag_audit"),
    ("adobe_", "data_quality"),
    ("gtm_", "tag_audit"),
    ("tag_", "tag_audit"),
    ("live_tag_", "live_tag_test"),
    ("ga4_", "data_quality"),
    ("amplitude_", "data_quality"),
    ("mixpanel_", "data_quality"),
    ("posthog_", "data_quality"),
    ("marketing_", "platform_health"),
    ("marketo_", "platform_health"),
    ("warehouse_", "warehouse"),
    ("seo_", "seo"),
)

# Actions that read metadata, explain, or manage rules — not audits. Their
# results are never persisted as runs.
_NOT_AUDITS = {
    "gtm_explain_tag",
    "gtm_simulate_event",
    "gtm_dependency_map",
    "adobe_launch_get_publish_history",
    "tag_list_platforms",
    "tag_get_platform_spec",
    "tag_get_event_spec",
    "tag_identify_type",
    "tag_list_custom_rules",
    "tag_save_custom_rule",
    "tag_delete_custom_rule",
    "live_tag_get_plan",
    "live_tag_get_sdr_context",
    "live_tag_start_session",
    "live_tag_list_plans",
    "live_tag_save_plan",
    # Audit persistence itself.
    "save_audit_result",
    "get_audit_run",
    "list_audit_runs",
    "audit_score_summary",
    "audit_score_history",
}

# Keys under which audit handlers return their finding lists.
_FINDING_KEYS = ("findings", "issues", "violations", "problems", "warnings", "errors", "recommendations")

_SEVERITY_ALIASES = {
    "critical": "critical",
    "high": "critical",
    "error": "critical",
    "fail": "critical",
    "failed": "critical",
    "warning": "warning",
    "warn": "warning",
    "medium": "warning",
    "info": "info",
    "low": "info",
    "notice": "info",
    "suggestion": "info",
    "pass": "pass",
    "passed": "pass",
    "ok": "pass",
}

# Suppress a duplicate save of the same action within this window — some
# clients retry, and a double-logged call must not produce two runs.
_DEDUPE_WINDOW_S = 90
_recent: dict[tuple[str, str], float] = {}


def audit_type_for(action: str) -> str | None:
    """The AuditRun type for a run_audit action, or None when it isn't an audit."""
    if action in _NOT_AUDITS:
        return None
    for prefix, audit_type in _AUDIT_TYPE_BY_PREFIX:
        if action.startswith(prefix):
            return audit_type
    return None


def _text(d: dict, *keys: str) -> str | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _normalize_finding(raw: Any, default_severity: str, platform: str | None) -> dict | None:
    if isinstance(raw, str):
        return {"severity": default_severity, "message": raw, "platform": platform, "source": "run_audit"}
    if not isinstance(raw, dict):
        return None
    sev_raw = str(raw.get("severity") or raw.get("level") or raw.get("status") or default_severity).lower()
    severity = _SEVERITY_ALIASES.get(sev_raw, default_severity)
    message = _text(raw, "message", "issue", "description", "title", "detail", "summary", "name")
    if not message:
        return None
    event = _text(raw, "event", "event_name")
    return {
        "severity": severity,
        "passed": severity == "pass",
        "message": message,
        "platform": (_text(raw, "platform", "vendor") or platform or "")[:64] or None,
        "rule_id": (_text(raw, "rule_id", "rule", "category", "check") or "")[:128] or None,
        "event": event[:128] if event else None,
        "entity_type": (_text(raw, "entity_type", "type") or "")[:32] or None,
        "entity_id": _text(raw, "entity_id", "tag_id", "trigger_id", "variable_id", "id"),
        "entity_label": _text(
            raw, "entity_label", "tag_name", "trigger_name", "variable_name", "tag", "trigger"
        ),
        "remediation": _text(raw, "remediation", "recommendation", "fix", "suggestion", "action"),
        "source": "run_audit",
    }


def findings_from_result(result: dict, platform: str | None = None) -> list[dict]:
    """Collect AuditFinding-shaped dicts from an audit handler's result.

    Handlers disagree on shape (``issues`` vs ``findings``, ``issue`` vs
    ``message``), so this reads every common key and normalizes severity.
    ``warnings``/``errors``/``recommendations`` lists default to the severity
    their key implies when an item carries none of its own.
    """
    defaults = {"warnings": "warning", "errors": "critical", "recommendations": "info"}
    out: list[dict] = []
    for key in _FINDING_KEYS:
        items = result.get(key)
        if not isinstance(items, list):
            continue
        for raw in items:
            f = _normalize_finding(raw, defaults.get(key, "warning"), platform)
            if f is not None:
                out.append(f)
    return out


def _score_from(result: dict) -> int | None:
    for k in ("score", "health_score", "overall_score", "compliance_score"):
        v = result.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return max(0, min(100, int(round(v))))
    return None


def _summary_from(result: dict) -> str | None:
    for k in ("summary", "overview", "message", "report"):
        v = result.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return None


def _title_for(action: str, params: dict) -> str:
    label = action.replace("_", " ").strip().title().replace("Gtm", "GTM").replace("Ga4", "GA4")
    scope = (
        params.get("public_id")
        or params.get("container_id")
        or params.get("property_id")
        or params.get("url")
        or params.get("dataset_id")
        or params.get("site_url")
    )
    return f"{label} — {scope}" if scope else label


async def autosave_audit_result(action: str, params: dict, result: Any, duration_ms: int | None) -> Any:
    """Persist a successful run_audit result and annotate the response.

    Best-effort by design: any failure is logged and the original result is
    returned untouched — saving must never break the audit itself.
    """
    audit_type = audit_type_for(action)
    if audit_type is None or not isinstance(result, dict) or result.get("error"):
        return result

    findings = findings_from_result(result, params.get("platform"))
    score = _score_from(result)
    if not findings and score is None:
        return result  # nothing auditable came back

    user = get_current_user()
    try:
        proj = state.current_project_ctx.get()
    except LookupError:
        proj = None
    if not user or not proj:
        return result

    project_id = _uuid.UUID(str(proj.project_id))
    title = _title_for(action, params)
    key = (str(project_id), title)
    now = time.monotonic()
    if now - _recent.get(key, 0.0) < _DEDUPE_WINDOW_S:
        return result
    _recent[key] = now

    from app.tools.save_audit_result_tools import persist_audit_run

    try:
        saved = await persist_audit_run(
            project_id=project_id,
            user_id=_uuid.UUID(str(user.user_id)),
            audit_type=audit_type,
            title=title,
            score=score,
            findings=findings,
            raw_summary=_summary_from(result),
            duration_ms=duration_ms,
            triggered_by="claude",
        )
    except Exception as exc:
        _recent.pop(key, None)
        logger.warning("run_audit autosave failed for %s: %s", action, exc)
        return result

    return {
        **result,
        "saved_to_fluxito": {
            "audit_run_id": saved["audit_run_id"],
            "view_url": saved["view_url"],
            "note": "Saved to the Fluxito Audit page automatically — do not call save_audit_result for this run.",
        },
    }
