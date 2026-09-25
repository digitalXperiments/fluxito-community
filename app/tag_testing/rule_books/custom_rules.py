"""
Rule Books — Custom Rules DB Layer
====================================

CRUD operations for project-specific custom audit rules stored in the
``tag_custom_rules`` table (migration 052_auditing_platform.py).

These functions are called by tag_rulebook_tools.py to merge project rules
into the validator alongside the static Rule Book specs.
"""

from __future__ import annotations

import logging
import re

import app.app_state as state

logger = logging.getLogger(__name__)

_VALID_SEVERITIES = {"critical", "warning", "info"}
_VALID_OPS = {
    "eq",
    "neq",
    "gt",
    "gte",
    "lt",
    "lte",
    "in",
    "not_in",
    "regex",
    "is_array",
    "is_string",
    "is_number",
    "truthy",
    "falsy",
}


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


async def get_custom_rules(project_id: str) -> list[dict]:
    """
    Return all active custom rules for a project.
    Returns [] if the table doesn't exist yet (pre-migration graceful fallback).
    """
    try:
        from sqlalchemy import text

        async with state.db_session_factory() as db:
            result = await db.execute(
                text("""
                    SELECT id, rule_id, platform, event, name, description,
                           required_params, forbidden_params, param_assertions,
                           severity, remediation, is_active, created_at
                    FROM tag_custom_rules
                    WHERE project_id = :pid AND is_active = TRUE
                    ORDER BY created_at DESC
                """),
                {"pid": project_id},
            )
            rows = result.mappings().all()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"get_custom_rules failed (table may not exist yet): {e}")
        return []


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


async def save_custom_rule(project_id: str, user_id: str, rule: dict) -> dict:
    """
    Create or update a custom rule.

    ``rule`` must contain:
      rule_id       (string, slug-safe)
      name          (string)
      platform      (string | "*")
      event         (string | "*")
      severity      (critical|warning|info)

    Optional:
      description, required_params (list), forbidden_params (list),
      param_assertions (list of {param, op, value, message}), remediation
    """
    errors = _validate_rule_dict(rule)
    if errors:
        return {"error": True, "error_type": "validation_error", "message": "; ".join(errors)}

    rule_id = rule.get("rule_id") or ""
    if not re.match(r"^[a-z0-9_.-]+$", rule_id):
        return {
            "error": True,
            "error_type": "validation_error",
            "message": "rule_id must be lowercase alphanumeric with dots, dashes, or underscores.",
        }

    import json

    from sqlalchemy import text

    try:
        pid = str(project_id)
        uid = str(user_id)
        now_sql = "NOW()"

        async with state.db_session_factory() as db:
            # Upsert: update if same project + rule_id exists, else insert
            existing = await db.execute(
                text("SELECT id FROM tag_custom_rules WHERE project_id = :pid AND rule_id = :rid"),
                {"pid": pid, "rid": rule_id},
            )
            row = existing.fetchone()

            if row:
                await db.execute(
                    text("""
                        UPDATE tag_custom_rules
                        SET name = :name,
                            description = :desc,
                            platform = :platform,
                            event = :event,
                            required_params = :req::jsonb,
                            forbidden_params = :forb::jsonb,
                            param_assertions = :assertions::jsonb,
                            severity = :severity,
                            remediation = :remediation,
                            is_active = TRUE,
                            updated_at = NOW()
                        WHERE project_id = :pid AND rule_id = :rid
                    """),
                    {
                        "name": rule.get("name", ""),
                        "desc": rule.get("description", ""),
                        "platform": rule.get("platform", "*"),
                        "event": rule.get("event", "*"),
                        "req": json.dumps(rule.get("required_params") or []),
                        "forb": json.dumps(rule.get("forbidden_params") or []),
                        "assertions": json.dumps(rule.get("param_assertions") or []),
                        "severity": rule.get("severity", "warning"),
                        "remediation": rule.get("remediation", ""),
                        "pid": pid,
                        "rid": rule_id,
                    },
                )
                await db.commit()
                return {"success": True, "action": "updated", "rule_id": rule_id}
            else:
                await db.execute(
                    text("""
                        INSERT INTO tag_custom_rules
                            (id, project_id, rule_id, name, description, platform, event,
                             required_params, forbidden_params, param_assertions,
                             severity, remediation, is_active, created_by, created_at, updated_at)
                        VALUES
                            (gen_random_uuid(), :pid, :rid, :name, :desc, :platform, :event,
                             :req::jsonb, :forb::jsonb, :assertions::jsonb,
                             :severity, :remediation, TRUE, :uid, NOW(), NOW())
                    """),
                    {
                        "pid": pid,
                        "rid": rule_id,
                        "name": rule.get("name", ""),
                        "desc": rule.get("description", ""),
                        "platform": rule.get("platform", "*"),
                        "event": rule.get("event", "*"),
                        "req": json.dumps(rule.get("required_params") or []),
                        "forb": json.dumps(rule.get("forbidden_params") or []),
                        "assertions": json.dumps(rule.get("param_assertions") or []),
                        "severity": rule.get("severity", "warning"),
                        "remediation": rule.get("remediation", ""),
                        "uid": uid,
                    },
                )
                await db.commit()
                return {"success": True, "action": "created", "rule_id": rule_id}
    except Exception as e:
        logger.error(f"save_custom_rule failed: {e}", exc_info=True)
        return {"error": True, "error_type": "db_error", "message": str(e)}


async def delete_custom_rule(project_id: str, rule_id: str) -> dict:
    """Soft-delete a custom rule (sets is_active = FALSE)."""
    from sqlalchemy import text

    try:
        async with state.db_session_factory() as db:
            result = await db.execute(
                text("""
                    UPDATE tag_custom_rules
                    SET is_active = FALSE, updated_at = NOW()
                    WHERE project_id = :pid AND rule_id = :rid
                """),
                {"pid": str(project_id), "rid": rule_id},
            )
            await db.commit()
            if result.rowcount == 0:
                return {"error": True, "error_type": "not_found", "message": f"Rule '{rule_id}' not found."}
            return {"success": True, "rule_id": rule_id, "action": "deleted"}
    except Exception as e:
        logger.error(f"delete_custom_rule failed: {e}", exc_info=True)
        return {"error": True, "error_type": "db_error", "message": str(e)}


# ---------------------------------------------------------------------------
# Validation helper
# ---------------------------------------------------------------------------


def _validate_rule_dict(rule: dict) -> list[str]:
    """Return a list of validation error strings. Empty = valid."""
    errors: list[str] = []
    if not rule.get("rule_id"):
        errors.append("rule_id is required")
    if not rule.get("name"):
        errors.append("name is required")
    sev = rule.get("severity", "warning")
    if sev not in _VALID_SEVERITIES:
        errors.append(f"severity must be one of: {', '.join(_VALID_SEVERITIES)}")
    for assertion in rule.get("param_assertions") or []:
        op = assertion.get("op")
        if op and op not in _VALID_OPS:
            errors.append(f"param assertion op '{op}' is not valid. Valid: {', '.join(sorted(_VALID_OPS))}")
    return errors
