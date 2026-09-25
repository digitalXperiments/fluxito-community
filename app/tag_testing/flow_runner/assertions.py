"""
Test-flow assertion evaluation (pure functions).
=================================================

Given a flow's steps (each carrying optional ``datalayer_events`` and
``vendor_requests`` assertions) and the captures produced by a run, decide
which assertions passed.

This module has **no I/O and no Playwright dependency** — it operates purely
on plain dicts/lists so it can be unit-tested exhaustively.

Execution-result shape consumed by :func:`evaluate`::

    {
      "datalayer_events": [
        {"event": "purchase", "data": {...}, "step_index": 2, "ts": 123.0},
        ...
      ],
      "beacons": [
        {
          "vendor_id": "<uuid>" | None,
          "vendor_slug": "ga4",
          "url": "...",
          "params": {"en": "purchase", ...},
          "step_index": 2,
        },
        ...
      ],
    }

Ops (case-insensitive on op name): equals, contains, regex, exists,
not_empty. When a check omits ``value`` the op defaults to ``exists``.
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Field / param operators
# ---------------------------------------------------------------------------

_MISSING = object()


def _norm(v: Any) -> str:
    """Normalise a value to a string for comparison."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        # 9.0 -> "9" so it matches a "9" expectation
        return str(int(v))
    return str(v)


def check_op(op: str | None, actual: Any, expected: Any) -> bool:
    """Apply a single operator. ``actual`` may be ``_MISSING`` if absent.

    Returns True when the check passes.
    """
    present = actual is not _MISSING
    op = (op or "").strip().lower()

    # No value given → treat as an existence check regardless of declared op.
    if not op:
        op = "exists" if expected in (None, "") else "equals"

    if op == "exists":
        return present
    if op == "not_empty":
        return present and _norm(actual).strip() != ""

    # Ops below require the key to be present.
    if not present:
        return False

    a = _norm(actual)

    if op == "equals":
        return a == _norm(expected)
    if op == "contains":
        return _norm(expected) in a
    if op == "regex":
        try:
            return re.search(str(expected), a) is not None
        except re.error:
            return False

    # Unknown op — fail closed.
    return False


def _lookup(params: dict, key: str) -> Any:
    """Fetch ``key`` from a params dict, returning ``_MISSING`` if absent."""
    if not isinstance(params, dict):
        return _MISSING
    if key in params:
        return params[key]
    return _MISSING


def _fields_match(params: dict, fields: list[dict]) -> tuple[bool, list[dict]]:
    """Check every field/param spec against ``params``.

    Returns (all_passed, detail_list) where detail carries per-field outcome.
    An empty ``fields`` list matches (used for "event fired at all").
    """
    details: list[dict] = []
    all_ok = True
    for f in fields or []:
        key = f.get("key")
        op = f.get("op")
        expected = f.get("value", None)
        actual = _lookup(params, key) if key else _MISSING
        ok = check_op(op, actual, expected)
        if not ok:
            all_ok = False
        details.append(
            {
                "key": key,
                "op": (op or ("exists" if expected in (None, "") else "equals")),
                "expected": expected,
                "actual": None if actual is _MISSING else actual,
                "passed": ok,
            }
        )
    return all_ok, details


# ---------------------------------------------------------------------------
# Candidate selection (scope by ``when``)
# ---------------------------------------------------------------------------


def _dl_candidates(dl_events: list[dict], event_name: str, when: str, step_index: int) -> list[dict]:
    out = []
    for ev in dl_events:
        if (ev.get("event") or "") != event_name:
            continue
        if when == "at_step" and ev.get("step_index") != step_index:
            continue
        out.append(ev)
    return out


def _beacon_candidates(beacons: list[dict], vendor_id: Any, when: str, step_index: int) -> list[dict]:
    out = []
    vid = str(vendor_id) if vendor_id is not None else None
    for b in beacons:
        bid = b.get("vendor_id")
        if vid is not None and (bid is None or str(bid) != vid):
            continue
        if when == "at_step" and b.get("step_index") != step_index:
            continue
        out.append(b)
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def evaluate(steps: list[dict], execution: dict) -> dict:
    """Evaluate all assertions declared across ``steps`` against ``execution``.

    Returns::

        {
          "total": int,
          "passed": int,
          "per_step": [
            {"step_index": i, "results": [
                {"kind": "datalayer"|"vendor", "description": str,
                 "passed": bool, "expected": {...}, "actual": {...}},
                ...
            ]},
            ...
          ],
        }
    """
    dl_events = execution.get("datalayer_events") or []
    beacons = execution.get("beacons") or []

    total = 0
    passed = 0
    per_step: list[dict] = []

    for idx, step in enumerate(steps or []):
        assertions = (step or {}).get("assertions") or {}
        results: list[dict] = []

        # ── dataLayer event assertions ────────────────────────────────────
        for a in assertions.get("datalayer_events") or []:
            event_name = a.get("event") or ""
            mode = (a.get("mode") or "must").lower()
            when = (a.get("when") or "anytime").lower()
            fields = a.get("fields") or []

            candidates = _dl_candidates(dl_events, event_name, when, idx)

            # A candidate "matches" when its data satisfies all field checks.
            matched = []
            field_details: list[dict] = []
            for ev in candidates:
                ok, details = _fields_match(ev.get("data") or {}, fields)
                if ok:
                    matched.append(ev)
                    field_details = details
                elif not field_details:
                    field_details = details

            fired = len(matched) > 0
            ok = (not fired) if mode == "must_not" else fired

            scope = "anywhere" if when == "anytime" else f"at step {idx}"
            verb = "must NOT fire" if mode == "must_not" else "must fire"
            desc = f"dataLayer event '{event_name}' {verb} {scope}"
            if fields:
                desc += f" with {len(fields)} field check(s)"

            results.append(
                {
                    "kind": "datalayer",
                    "description": desc,
                    "passed": ok,
                    "expected": {
                        "event": event_name,
                        "mode": mode,
                        "when": when,
                        "fields": fields,
                    },
                    "actual": {
                        "fired": fired,
                        "match_count": len(matched),
                        "candidate_count": len(candidates),
                        "fields": field_details,
                    },
                }
            )
            total += 1
            if ok:
                passed += 1

        # ── vendor request assertions ─────────────────────────────────────
        for a in assertions.get("vendor_requests") or []:
            vendor_id = a.get("vendor_id")
            mode = (a.get("mode") or "must").lower()
            when = (a.get("when") or "anytime").lower()
            params_checks = a.get("params") or []

            candidates = _beacon_candidates(beacons, vendor_id, when, idx)

            matched = []
            param_details: list[dict] = []
            for b in candidates:
                ok, details = _fields_match(b.get("params") or {}, params_checks)
                if ok:
                    matched.append(b)
                    param_details = details
                elif not param_details:
                    param_details = details

            fired = len(matched) > 0
            ok = (not fired) if mode == "must_not" else fired

            scope = "anywhere" if when == "anytime" else f"at step {idx}"
            verb = "must NOT be sent" if mode == "must_not" else "must be sent"
            desc = f"vendor request '{vendor_id}' {verb} {scope}"
            if params_checks:
                desc += f" with {len(params_checks)} param check(s)"

            results.append(
                {
                    "kind": "vendor",
                    "description": desc,
                    "passed": ok,
                    "expected": {
                        "vendor_id": str(vendor_id) if vendor_id is not None else None,
                        "mode": mode,
                        "when": when,
                        "params": params_checks,
                    },
                    "actual": {
                        "fired": fired,
                        "match_count": len(matched),
                        "candidate_count": len(candidates),
                        "params": param_details,
                    },
                }
            )
            total += 1
            if ok:
                passed += 1

        per_step.append({"step_index": idx, "results": results})

    return {"total": total, "passed": passed, "per_step": per_step}
