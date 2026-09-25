"""
Rule Books — Base Dataclasses
==============================

Core types shared by every platform Rule Book module.

  ParamSpec   — defines a single tag/event parameter: name, type, required/
                recommended, optional value constraints.
  EventSpec   — defines an event (e.g. "purchase"): required + recommended
                ParamSpecs, severity if required params are missing.
  GlobalRule  — a structural check that applies to the platform's tag
                configuration rather than a specific event payload.
  RuleBook    — the full spec for one platform: GTM type codes, detection
                patterns, events, and global rules.

Dataclasses are frozen to prevent accidental mutation at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Severity = Literal["critical", "warning", "info"]
ParamType = Literal["string", "number", "integer", "boolean", "array", "object", "any"]


@dataclass(frozen=True)
class ParamSpec:
    """Specification for a single event/tag parameter."""

    name: str
    type: ParamType = "string"
    notes: str = ""
    required: bool = False
    recommended: bool = False
    # Value constraints (all optional)
    allowed_values: tuple[str, ...] = field(default_factory=tuple)
    regex: str | None = None
    min_value: float | None = None
    max_value: float | None = None
    # If True, the *value* (not just presence) is checked
    check_value: bool = False

    def __post_init__(self):
        # At least one of required/recommended must be truthy if this is
        # spec'd at all, but we allow neither for purely informational params.
        if self.allowed_values and not isinstance(self.allowed_values, tuple):
            # Coerce list → tuple for hashability when frozen
            object.__setattr__(self, "allowed_values", tuple(self.allowed_values))


@dataclass(frozen=True)
class EventSpec:
    """
    Specification for a single event that a platform can receive.

    ``event_name`` is the canonical name as sent in the network request
    (e.g. ``"purchase"`` for GA4, ``"Purchase"`` for Meta Pixel).
    """

    event_name: str
    required_params: tuple[ParamSpec, ...] = field(default_factory=tuple)
    recommended_params: tuple[ParamSpec, ...] = field(default_factory=tuple)
    severity_if_missing_required: Severity = "critical"
    notes: str = ""
    # Aliases this event is also known as (e.g. "checkout" → "purchase")
    aliases: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        # Coerce lists to tuples
        if not isinstance(self.required_params, tuple):
            object.__setattr__(self, "required_params", tuple(self.required_params))
        if not isinstance(self.recommended_params, tuple):
            object.__setattr__(self, "recommended_params", tuple(self.recommended_params))
        if not isinstance(self.aliases, tuple):
            object.__setattr__(self, "aliases", tuple(self.aliases))

    @property
    def all_params(self) -> tuple[ParamSpec, ...]:
        return self.required_params + self.recommended_params

    def find_param(self, name: str) -> ParamSpec | None:
        """Case-insensitive lookup by param name."""
        n = name.lower()
        for p in self.all_params:
            if p.name.lower() == n:
                return p
        return None


@dataclass(frozen=True)
class GlobalRule:
    """
    A structural rule that applies to the platform's tag/container configuration
    rather than a specific event payload.

    Examples: "test_event_code must not appear in production tags",
              "Pixel base code must be present on all pages".
    """

    rule_id: str
    description: str
    severity: Severity
    remediation: str = ""
    # A substring or regex to search for in tag HTML/config that indicates
    # whether this rule passes or fails.  Absence of the pattern is the
    # default trigger condition (i.e. "this must NOT be present").
    detection_hint: str = ""
    # If True: the detection_hint MUST be present (violation = missing).
    # If False (default): the detection_hint MUST NOT be present (violation = found).
    must_be_present: bool = False


@dataclass(frozen=True)
class RuleBook:
    """
    Complete specification for one tracking platform.

    ``platform``          — unique slug (e.g. "facebook_pixel", "ga4").
    ``display_name``      — human label shown in the UI.
    ``spec_version``      — version string referencing the upstream platform docs.
    ``docs_url``          — canonical documentation URL.
    ``gtm_type_codes``    — list of native GTM tag ``type`` field values that
                            deterministically identify this platform (tier 1).
    ``detection_patterns``— regex strings matched against Custom HTML tag code
                            to identify the platform (tier 2).
    ``name_prefix_hints`` — lowercase tag-name prefixes used as a fallback
                            heuristic when the above two tiers fail (tier 3).
    ``events``            — ordered list of EventSpec (most commonly audited first).
    ``global_rules``      — structural rules applied to the tag configuration.
    """

    platform: str
    display_name: str
    spec_version: str
    docs_url: str
    gtm_type_codes: tuple[str, ...] = field(default_factory=tuple)
    detection_patterns: tuple[str, ...] = field(default_factory=tuple)
    name_prefix_hints: tuple[str, ...] = field(default_factory=tuple)
    events: tuple[EventSpec, ...] = field(default_factory=tuple)
    global_rules: tuple[GlobalRule, ...] = field(default_factory=tuple)

    def __post_init__(self):
        for attr in ("gtm_type_codes", "detection_patterns", "name_prefix_hints", "events", "global_rules"):
            v = getattr(self, attr)
            if not isinstance(v, tuple):
                object.__setattr__(self, attr, tuple(v))

    def find_event(self, event_name: str) -> EventSpec | None:
        """
        Case-insensitive lookup.  Also checks EventSpec.aliases.
        Returns the matching EventSpec or None.
        """
        target = event_name.lower().strip()
        for ev in self.events:
            if ev.event_name.lower() == target:
                return ev
            if target in (a.lower() for a in ev.aliases):
                return ev
        return None

    def serialize(self, include_events: bool = True) -> dict:
        """Return a JSON-serializable dict for MCP tool responses."""
        out: dict = {
            "platform": self.platform,
            "display_name": self.display_name,
            "spec_version": self.spec_version,
            "docs_url": self.docs_url,
            "gtm_type_codes": list(self.gtm_type_codes),
            "detection_patterns": list(self.detection_patterns),
            "name_prefix_hints": list(self.name_prefix_hints),
            "global_rules": [
                {
                    "rule_id": r.rule_id,
                    "description": r.description,
                    "severity": r.severity,
                    "remediation": r.remediation,
                    "detection_hint": r.detection_hint,
                    "must_be_present": r.must_be_present,
                }
                for r in self.global_rules
            ],
        }
        if include_events:
            out["events"] = [_serialize_event(ev) for ev in self.events]
            out["event_count"] = len(self.events)
        else:
            out["event_count"] = len(self.events)
        return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _serialize_param(p: ParamSpec) -> dict:
    d: dict = {
        "name": p.name,
        "type": p.type,
        "required": p.required,
        "recommended": p.recommended,
        "notes": p.notes,
    }
    if p.allowed_values:
        d["allowed_values"] = list(p.allowed_values)
    if p.regex:
        d["regex"] = p.regex
    if p.min_value is not None:
        d["min_value"] = p.min_value
    if p.max_value is not None:
        d["max_value"] = p.max_value
    return d


def _serialize_event(ev: EventSpec) -> dict:
    return {
        "event_name": ev.event_name,
        "aliases": list(ev.aliases),
        "notes": ev.notes,
        "severity_if_missing_required": ev.severity_if_missing_required,
        "required_params": [_serialize_param(p) for p in ev.required_params],
        "recommended_params": [_serialize_param(p) for p in ev.recommended_params],
    }
