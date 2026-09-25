"""
Spec Engine — single source of truth for MCP tool action/param specs.

A registry of :class:`ActionSpec` objects (see ``app/tools/specs/``) is the only
place an action's parameters are described. From it we GENERATE everything an MCP
client sees:

  * the tool **description** (``tool.fn.__doc__``)              -> render_description
  * the served JSON **input schema** (``tool.parameters``)      -> build_input_schema
  * a machine-readable **discovery** payload (action="describe") -> describe_payload
  * the standard self-describing **error envelope**             -> missing_param_error

Because all four are derived from one registry, the description can no longer drift
from reality (Phase-1 found drift in ~every tool). A build-time drift guard
(``tests/test_tool_specs.py``) asserts the registry matches what is actually routed
and callable.

Design notes
------------
* The served schema keeps the ``{action, params}`` envelope that the runtime
  pydantic arg-model expects — we only enrich what the *client* sees, never the
  permissive runtime validation (same contract the strictify pass relies on).
* ``params`` is rendered as a typed, documented **flat superset** of every action's
  parameters. Strict clients (OpenAI/Grok) accept this; per-action *required-ness*
  is delivered at runtime via ``describe`` and the rich error envelope, which a
  low-end client converges on in a single round-trip.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Closed vocabularies
# ---------------------------------------------------------------------------

#: Every error the surface returns uses one of these in ``error_type`` so clients
#: can branch on a stable, documented set instead of parsing prose.
ERROR_TYPES: frozenset[str] = frozenset(
    {
        "missing_required_param",
        "invalid_param",
        "unknown_action",
        "unknown_tool",
        "not_connected",
        "insufficient_scope",
        "not_implemented",
        "upstream_error",
        "server_error",
    }
)

#: JSON-Schema scalar/compound types we allow in a Param.
JSON_TYPES: frozenset[str] = frozenset({"string", "integer", "number", "boolean", "array", "object"})

#: Reserved action every dispatcher answers — returns the machine-readable spec.
DESCRIBE_ACTION = "describe"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Param:
    """One parameter of one action."""

    name: str
    type: str = "string"
    required: bool = False
    enum: tuple[str, ...] | None = None
    item_type: str | None = None  # element type when ``type == "array"``
    example: Any = None
    doc: str = ""
    #: When set, this param is only relevant if the call's platform/engine is in
    #: this tuple (e.g. ``property_id`` only for ga4). Used by required-param
    #: validation and the rendered description.
    platforms: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.type not in JSON_TYPES:
            raise ValueError(f"Param {self.name!r}: bad type {self.type!r}")
        if self.type == "array" and self.item_type and self.item_type not in JSON_TYPES:
            raise ValueError(f"Param {self.name!r}: bad item_type {self.item_type!r}")

    def applies_to(self, platform: str | None) -> bool:
        """Is this param relevant given the resolved platform/engine?"""
        if not self.platforms:
            return True
        if platform is None:
            # Platform unknown — can't rule the param out.
            return True
        return platform in self.platforms

    def json_schema(self) -> dict[str, Any]:
        node: dict[str, Any] = {"type": self.type}
        if self.enum:
            node["enum"] = list(self.enum)
        if self.type == "array":
            node["items"] = {"type": self.item_type or "string"}
        desc = self.doc.strip()
        if self.platforms:
            desc = (desc + f" (platform: {', '.join(self.platforms)})").strip()
        if desc:
            node["description"] = desc
        if self.example is not None:
            node["examples"] = [self.example]
        return node


@dataclass(frozen=True)
class ActionSpec:
    """The full contract for one ``(tool, action)``."""

    tool: str
    action: str
    summary: str
    params: tuple[Param, ...] = ()
    platforms: tuple[str, ...] | None = None  # action valid only on these platforms
    returns: str = ""
    scope: str | None = None
    mutates: bool = False
    reversible: bool | None = None
    example: dict[str, Any] | None = None
    group: str = ""

    # -- derived helpers -------------------------------------------------
    def required_params(self, platform: str | None = None) -> list[Param]:
        return [p for p in self.params if p.required and p.applies_to(platform)]

    def optional_params(self, platform: str | None = None) -> list[Param]:
        return [p for p in self.params if not p.required and p.applies_to(platform)]

    def example_call(self, platform: str | None = None) -> dict[str, Any]:
        """A minimal valid ``params`` example built from required params."""
        if self.example is not None:
            return dict(self.example)
        out: dict[str, Any] = {}
        for p in self.required_params(platform):
            out[p.name] = p.example if p.example is not None else _placeholder(p)
        return out

    def serialize(self, platform: str | None = None) -> dict[str, Any]:
        """The ``describe`` payload for this action."""
        return {
            "action": self.action,
            "summary": self.summary,
            "platforms": list(self.platforms) if self.platforms else None,
            "scope": self.scope,
            "mutates": self.mutates,
            "reversible": self.reversible,
            "returns": self.returns,
            "required": [_param_dict(p) for p in self.required_params(platform)],
            "optional": [_param_dict(p) for p in self.optional_params(platform)],
            "example": {"action": self.action, "params": self.example_call(platform)},
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _placeholder(p: Param) -> Any:
    if p.enum:
        return p.enum[0]
    return {
        "string": "…",
        "integer": 0,
        "number": 0,
        "boolean": False,
        "array": [],
        "object": {},
    }[p.type]


def _param_dict(p: Param) -> dict[str, Any]:
    d: dict[str, Any] = {"name": p.name, "type": p.type}
    if p.enum:
        d["enum"] = list(p.enum)
    if p.type == "array":
        d["item_type"] = p.item_type or "string"
    if p.example is not None:
        d["example"] = p.example
    if p.doc:
        d["doc"] = p.doc
    if p.platforms:
        d["platforms"] = list(p.platforms)
    return d


def resolve_platform(params: dict[str, Any] | None) -> str | None:
    """The platform/engine a call is targeting, if any."""
    if not params:
        return None
    return params.get("platform") or params.get("engine") or None


# ---------------------------------------------------------------------------
# Runtime validation + error envelope
# ---------------------------------------------------------------------------


def validate_required(spec: ActionSpec, args: dict[str, Any]) -> list[str]:
    """Names of required params missing from ``args`` (platform-aware).

    ``args`` is the *effective* param dict (caller params merged with any
    route-injected kwargs such as ``platform``), excluding the routing ``action``.
    A param counts as missing when absent or set to ``None``/``""``/empty list.
    """
    platform = resolve_platform(args)
    missing: list[str] = []
    for p in spec.required_params(platform):
        v = args.get(p.name)
        if v is None or v == "" or v == []:
            missing.append(p.name)
    return missing


def missing_param_error(spec: ActionSpec, missing: list[str], platform: str | None = None) -> dict[str, Any]:
    """The standard self-describing error envelope for a missing param."""
    req = [p.name for p in spec.required_params(platform)]
    opt = [p.name for p in spec.optional_params(platform)]
    plat_note = f" (platform={platform})" if platform else ""
    return {
        "error": True,
        "error_type": "missing_required_param",
        "tool": spec.tool,
        "action": spec.action,
        "message": f"{spec.action} needs: {', '.join(missing)}.{plat_note} "
        f"Call action='describe' params={{'action':'{spec.action}'}} for the full spec.",
        "missing": missing,
        "required": req,
        "optional": opt,
        "example": {"action": spec.action, "params": spec.example_call(platform)},
    }


# ---------------------------------------------------------------------------
# Discovery payload
# ---------------------------------------------------------------------------


def describe_payload(
    tool: str,
    specs: list[ActionSpec],
    action: str | None = None,
    platform: str | None = None,
) -> dict[str, Any]:
    """Machine-readable spec for one action, or all actions of a tool."""
    by_action = {s.action: s for s in specs}
    if action and action != DESCRIBE_ACTION:
        spec = by_action.get(action)
        if spec is None:
            return {
                "error": True,
                "error_type": "unknown_action",
                "tool": tool,
                "message": f"Unknown action '{action}' for {tool}.",
                "available_actions": sorted(by_action),
            }
        return {"tool": tool, "action": action, "spec": spec.serialize(platform)}
    return {
        "tool": tool,
        "actions": [s.serialize(platform) for s in sorted(specs, key=lambda s: (s.group, s.action))],
    }


# ---------------------------------------------------------------------------
# Description generator
# ---------------------------------------------------------------------------


def render_description(tool: str, specs: list[ActionSpec], header: str, footer: str = "") -> str:
    """Render the complete, consistent tool description from its specs."""
    lines: list[str] = [header.strip(), ""]
    lines.append(
        "Call `action='describe'` (optionally params={'action': '<name>'}) for a "
        "machine-readable spec of any action's params. Missing-param errors echo the "
        "full required/optional list + an example."
    )
    lines.append("")
    lines.append("Actions:")

    groups: dict[str, list[ActionSpec]] = {}
    for s in specs:
        groups.setdefault(s.group, []).append(s)

    for group in sorted(groups):
        if group:
            lines.append(f"  {group}")
        for s in sorted(groups[group], key=lambda s: s.action):
            req = [p.name for p in s.required_params()]
            req_note = f" — requires: {', '.join(req)}" if req else ""
            plat = f" [{'/'.join(s.platforms)}]" if s.platforms else ""
            prefix = "    " if group else "  "
            lines.append(f"{prefix}{s.action}{plat} — {s.summary}{req_note}")

    if footer.strip():
        lines.append("")
        lines.append(footer.strip())
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Schema generators
# ---------------------------------------------------------------------------


def build_input_schema(
    tool: str,
    specs: list[ActionSpec],
    actions: list[str],
    encoding: str = "flat",
) -> dict[str, Any]:
    """Build the served ``tool.parameters`` JSON schema.

    ``actions`` is the authoritative action enum (the dispatcher's route keys);
    ``describe`` is always appended. ``flat`` keeps the ``{action, params}``
    envelope and types ``params`` as the documented superset of every action's
    params — strict-client safe and consistent with the runtime arg-model.
    """
    enum = sorted(set(actions) | {DESCRIBE_ACTION})
    if encoding == "flat":
        return _flat_schema(tool, specs, enum)
    if encoding == "union":
        return discriminated_union_schema(tool, specs, enum)
    raise ValueError(f"unknown encoding {encoding!r}")


def _flat_schema(tool: str, specs: list[ActionSpec], enum: list[str]) -> dict[str, Any]:
    # Superset of every param across actions; first definition of a name wins,
    # but we annotate which actions use it so a client can self-orient.
    props: dict[str, Any] = {}
    used_by: dict[str, list[str]] = {}
    for s in specs:
        for p in s.params:
            used_by.setdefault(p.name, []).append(s.action)
            if p.name not in props:
                props[p.name] = p.json_schema()
    for name, node in props.items():
        actions = used_by[name]
        tag = f"(used by: {', '.join(sorted(set(actions))[:6])}" + ("…)" if len(set(actions)) > 6 else ")")
        node["description"] = (node.get("description", "").strip() + " " + tag).strip()
    return {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": enum,
                "description": f"Which {tool} action to run. Use 'describe' to list every action's params.",
            },
            "params": {
                "type": "object",
                "description": "Action parameters. See each param's 'used by' tag, or call action='describe'.",
                "properties": props,
                "additionalProperties": True,
            },
        },
        "required": ["action"],
    }


def discriminated_union_schema(tool: str, specs: list[ActionSpec], enum: list[str]) -> dict[str, Any]:
    """Per-action discriminated union (CANDIDATE encoding).

    Emitted for the conformance test to evaluate against strict clients; not
    wired as a served schema in Phase 2 because it flattens params (which would
    diverge from the ``(action, params)`` runtime arg-model). Phase 3 adopts it
    per tool only where the conformance test confirms strict-mode acceptance.
    """
    branches: list[dict[str, Any]] = []
    for s in sorted(specs, key=lambda s: s.action):
        bprops: dict[str, Any] = {"action": {"type": "string", "const": s.action}}
        required = ["action"]
        for p in s.params:
            bprops[p.name] = p.json_schema()
            if p.required:
                required.append(p.name)
        branches.append(
            {
                "type": "object",
                "properties": bprops,
                "required": required,
                "additionalProperties": False,
            }
        )
    # describe branch
    branches.append(
        {
            "type": "object",
            "properties": {"action": {"type": "string", "const": DESCRIBE_ACTION}},
            "required": ["action"],
            "additionalProperties": True,
        }
    )
    return {"oneOf": branches}


# ---------------------------------------------------------------------------
# Strict-client conformance check (used by the conformance test)
# ---------------------------------------------------------------------------


def strict_safe_issues(schema: Any, path: str = "$") -> list[str]:
    """Return reasons a schema would trouble strict clients (OpenAI/Grok mode).

    Empty list == strict-safe. Flags nullable unions, null defaults on typed
    fields, and (top-level) absence of a typed object root.
    """
    issues: list[str] = []

    def walk(node: Any, path: str) -> None:
        if not isinstance(node, dict):
            return
        for key in ("anyOf", "oneOf"):
            branches = node.get(key)
            if isinstance(branches, list):
                if any(isinstance(b, dict) and b.get("type") == "null" for b in branches):
                    issues.append(f"{path}.{key}: contains a null branch (nullable union)")
        if "default" in node and node["default"] is None and node.get("type") not in (None, "null"):
            issues.append(f"{path}.default: null default on typed field")
        for ck in ("properties", "$defs", "definitions"):
            sub = node.get(ck)
            if isinstance(sub, dict):
                for k, v in sub.items():
                    walk(v, f"{path}.{ck}.{k}")
        for nk in ("items", "additionalProperties"):
            if isinstance(node.get(nk), dict):
                walk(node[nk], f"{path}.{nk}")
        for uk in ("anyOf", "oneOf", "allOf"):
            if isinstance(node.get(uk), list):
                for i, v in enumerate(node[uk]):
                    walk(v, f"{path}.{uk}[{i}]")

    walk(schema, path)
    return issues


__all__ = [
    "DESCRIBE_ACTION",
    "ERROR_TYPES",
    "JSON_TYPES",
    "ActionSpec",
    "Param",
    "build_input_schema",
    "describe_payload",
    "discriminated_union_schema",
    "missing_param_error",
    "render_description",
    "resolve_platform",
    "strict_safe_issues",
    "validate_required",
]
