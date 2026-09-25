"""
KPI Executor — compute a structured KPI against its bound sources.

Execution strategy (decided during KPI library design):

* **Push-down** — when every ``KPIInput`` shares a single ``(source,
  connection_id)``, the KPI is considered single-source. Each input
  still runs as its own native query against that source (GA4 run_report
  or a BigQuery SQL aggregate), but no foreign data crosses between
  sources. Numbers come straight from the source of truth.
* **Pull-up** — when inputs span multiple sources, each input is
  fetched independently and the expression is evaluated in-app.

Both paths share the same expression evaluator. The returned ``mode``
field tells the caller (UI or MCP) which path was taken so the answer
can be qualified appropriately.

Expression grammar
------------------
Arithmetic only: ``+``, ``-``, ``*``, ``/``, ``//``, ``%``, ``**``,
numeric literals, parentheses, unary plus/minus, and ``{key}`` tokens
that resolve to scalar input values. Anything else (function calls,
attribute access, names outside the input keys) is rejected by the
AST walker.

Currently supported sources: ``ga4``, ``bigquery``. Other sources
raise ``NotImplementedError`` from the executor — the catalog can
still hold KPIs bound to them, they just can't be computed yet.
"""

from __future__ import annotations

import ast
import logging
import operator
import re
import uuid
from typing import Any

from sqlalchemy import select

import app.app_state as app_state
from app.models.bq_connection import BQConnection
from app.models.knowledge import KPI, KPIInput

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Safe expression evaluator
# ---------------------------------------------------------------------------


_ALLOWED_BINOPS: dict[type, Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_ALLOWED_UNARYOPS: dict[type, Any] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

# ``{name}`` tokens in the expression are rewritten to bare Python names
# before parsing. Only letters/digits/underscores, starting with a letter.
_TOKEN_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _eval_node(node: ast.AST, variables: dict[str, float]) -> float:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, variables)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return float(node.value)
        raise ValueError("Only numeric constants are allowed in KPI expressions")
    if isinstance(node, ast.Name):
        if node.id in variables:
            return float(variables[node.id])
        raise ValueError(f"Unknown input key '{node.id}' in expression")
    if isinstance(node, ast.BinOp):
        op = _ALLOWED_BINOPS.get(type(node.op))
        if op is None:
            raise ValueError(f"Unsupported operator {type(node.op).__name__}")
        return op(_eval_node(node.left, variables), _eval_node(node.right, variables))
    if isinstance(node, ast.UnaryOp):
        op = _ALLOWED_UNARYOPS.get(type(node.op))
        if op is None:
            raise ValueError(f"Unsupported unary operator {type(node.op).__name__}")
        return op(_eval_node(node.operand, variables))
    raise ValueError(f"Unsupported expression element: {type(node).__name__}")


def _evaluate_expression(expression: str, variables: dict[str, float]) -> float:
    py_expr = _TOKEN_RE.sub(r"\1", expression)
    tree = ast.parse(py_expr, mode="eval")
    return _eval_node(tree, variables)


# ---------------------------------------------------------------------------
# BigQuery helpers
# ---------------------------------------------------------------------------


_BQ_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_BQ_AGGS = {"sum", "avg", "count", "count_distinct", "min", "max", "none"}


def _bq_ident(value: str, label: str) -> str:
    """Defense-in-depth: BQ dataset/table/column names are already
    constrained by the picker (loaded from INFORMATION_SCHEMA), but we
    validate here too so ``binding`` data from the DB can't inject SQL."""
    if not isinstance(value, str) or not _BQ_IDENT_RE.match(value):
        raise ValueError(f"Invalid BigQuery {label}: {value!r}")
    return value


def _bq_agg_sql(agg: str, field: str) -> str:
    if agg not in _BQ_AGGS:
        raise ValueError(f"Unsupported BigQuery aggregation: {agg}")
    field_ref = f"`{_bq_ident(field, 'field')}`"
    if agg == "none":
        return field_ref
    if agg == "count_distinct":
        return f"COUNT(DISTINCT {field_ref})"
    return f"{agg.upper()}({field_ref})"


# ---------------------------------------------------------------------------
# Input fetchers
# ---------------------------------------------------------------------------


async def _fetch_ga4_value(inp: KPIInput, start: str, end: str) -> float:
    """Run a scalar (no-dimension) runReport for one GA4 metric input."""
    b = inp.binding or {}
    kind = b.get("kind", "metric")
    if kind != "metric":
        raise ValueError(f"Input '{inp.key}' is a GA4 {kind}; only metrics can be used as scalar inputs")
    api_name = b.get("api_name")
    property_id = b.get("property_id")
    if not api_name or not property_id:
        raise ValueError(f"Input '{inp.key}' is missing property_id or api_name")

    if app_state.ga4_connector is None:
        raise RuntimeError("GA4 connector is not initialized")

    resp = await app_state.ga4_connector.run_report(
        connection_id=str(inp.connection_id),
        property_id=f"properties/{property_id}",
        dimensions=[],
        metrics=[api_name],
        date_range_start=start,
        date_range_end=end,
        limit=1,
    )
    rows = resp.get("rows") or []
    if not rows or not rows[0].get("metrics"):
        return 0.0
    raw = rows[0]["metrics"][0]
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


async def _fetch_bq_value(inp: KPIInput) -> float:
    """Run a scalar SELECT aggregate against one BigQuery table."""
    b = inp.binding or {}
    dataset = _bq_ident(b.get("dataset", ""), "dataset")
    table = _bq_ident(b.get("table", ""), "table")
    agg = (b.get("aggregation") or "sum").lower()
    agg_expr = _bq_agg_sql(agg, b.get("field", ""))

    async with app_state.db_session_factory() as db:
        conn = await db.get(BQConnection, inp.connection_id)
    if not conn or not conn.is_active:
        raise ValueError(f"BigQuery connection for input '{inp.key}' is not active")

    if app_state.bq_connector is None:
        raise RuntimeError("BigQuery connector is not initialized")

    query = f"SELECT {agg_expr} AS value FROM `{conn.project_id}.{dataset}.{table}`"

    resp = await app_state.bq_connector.run_query(
        conn.service_account_encrypted, conn.project_id, query, max_results=1
    )
    if resp.get("error"):
        raise RuntimeError(resp.get("message") or "BigQuery query failed")
    rows = resp.get("rows") or []
    if not rows:
        return 0.0
    v = rows[0].get("value")
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


async def _fetch_input_value(inp: KPIInput, start: str, end: str) -> float:
    if inp.source == "ga4":
        return await _fetch_ga4_value(inp, start, end)
    if inp.source == "bigquery":
        return await _fetch_bq_value(inp)
    raise NotImplementedError(f"Source '{inp.source}' is not supported by the KPI executor yet")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _kpi_result(
    *,
    value: float | None,
    mode: str | None,
    inputs: dict[str, float],
    expression: str | None,
    warnings: list[str],
    error: str | None,
) -> dict[str, Any]:
    return {
        "value": value,
        "mode": mode,
        "inputs": inputs,
        "expression": expression,
        "warnings": warnings,
        "error": error,
    }


async def compute_kpi(
    kpi: KPI,
    date_range_start: str | None = None,
    date_range_end: str | None = None,
) -> dict[str, Any]:
    """
    Execute a KPI and return its scalar value plus diagnostics.

    ``date_range_start`` / ``date_range_end`` accept GA4's relative
    shorthand (``30daysAgo``, ``today``) or ISO ``YYYY-MM-DD``. Defaults
    to last 30 days when omitted. The range is only applied to
    time-sensitive sources (GA4); BigQuery inputs currently ignore it
    unless the user has embedded a date filter in their binding.

    Returns ``{value, mode, inputs, expression, warnings, error}``.
    """
    start = date_range_start or "30daysAgo"
    end = date_range_end or "today"

    inputs: list[KPIInput] = list(kpi.inputs or [])
    expression = (kpi.expression or "").strip()

    if not inputs:
        return _kpi_result(
            value=None,
            mode=None,
            inputs={},
            expression=expression,
            warnings=[],
            error="KPI has no bound inputs — add inputs before computing.",
        )
    if not expression:
        return _kpi_result(
            value=None,
            mode=None,
            inputs={},
            expression=None,
            warnings=[],
            error="KPI has no expression — set an expression referencing input keys.",
        )

    sources = {(i.source, str(i.connection_id)) for i in inputs}
    mode = "push_down" if len(sources) == 1 else "pull_up"

    values: dict[str, float] = {}
    warnings: list[str] = []
    for inp in inputs:
        try:
            values[inp.key] = await _fetch_input_value(inp, start, end)
        except NotImplementedError as e:
            return _kpi_result(
                value=None,
                mode=mode,
                inputs=values,
                expression=expression,
                warnings=warnings,
                error=str(e),
            )
        except Exception as e:
            logger.exception("KPI input fetch failed for key=%s", inp.key)
            return _kpi_result(
                value=None,
                mode=mode,
                inputs=values,
                expression=expression,
                warnings=warnings,
                error=f"Failed to fetch input '{inp.key}': {e}",
            )

    # Warn if any key is referenced in the expression but missing from inputs,
    # or vice versa — still attempt evaluation if all referenced keys are set.
    token_keys = set(_TOKEN_RE.findall(expression))
    input_keys = set(values.keys())
    unknown = token_keys - input_keys
    if unknown:
        return _kpi_result(
            value=None,
            mode=mode,
            inputs=values,
            expression=expression,
            warnings=warnings,
            error=f"Expression references unknown keys: {sorted(unknown)}",
        )
    unused = input_keys - token_keys
    if unused:
        warnings.append(f"Inputs {sorted(unused)} are defined but not used in the expression.")

    try:
        value = _evaluate_expression(expression, values)
    except ZeroDivisionError:
        return _kpi_result(
            value=None,
            mode=mode,
            inputs=values,
            expression=expression,
            warnings=warnings,
            error="Division by zero while evaluating expression.",
        )
    except Exception as e:
        return _kpi_result(
            value=None,
            mode=mode,
            inputs=values,
            expression=expression,
            warnings=warnings,
            error=f"Expression evaluation failed: {e}",
        )

    if kpi.expected_range_min is not None and value < float(kpi.expected_range_min):
        warnings.append(f"Value {value} is below the expected range minimum ({kpi.expected_range_min}).")
    if kpi.expected_range_max is not None and value > float(kpi.expected_range_max):
        warnings.append(f"Value {value} is above the expected range maximum ({kpi.expected_range_max}).")

    return _kpi_result(
        value=value,
        mode=mode,
        inputs=values,
        expression=expression,
        warnings=warnings,
        error=None,
    )


async def compute_kpi_by_id(
    kpi_id: uuid.UUID,
    project_id: uuid.UUID,
    date_range_start: str | None = None,
    date_range_end: str | None = None,
) -> dict[str, Any]:
    """Load a KPI by id (scoped to project) and compute it."""
    from sqlalchemy.orm import selectinload

    async with app_state.db_session_factory() as db:
        stmt = select(KPI).where(KPI.id == kpi_id).options(selectinload(KPI.inputs))
        kpi = (await db.execute(stmt)).scalar_one_or_none()

    if not kpi or kpi.project_id != project_id:
        return {"value": None, "error": "KPI not found", "mode": None, "inputs": {}, "warnings": []}

    return await compute_kpi(kpi, date_range_start, date_range_end)


async def compute_kpi_by_slug(
    slug: str,
    project_id: uuid.UUID,
    date_range_start: str | None = None,
    date_range_end: str | None = None,
) -> dict[str, Any]:
    """Load a KPI by slug (scoped to project) and compute it."""
    from sqlalchemy import func as sa_func
    from sqlalchemy.orm import selectinload

    async with app_state.db_session_factory() as db:
        stmt = (
            select(KPI)
            .where(KPI.project_id == project_id)
            .where(sa_func.lower(KPI.slug) == slug.lower())
            .options(selectinload(KPI.inputs))
        )
        kpi = (await db.execute(stmt)).scalar_one_or_none()

    if not kpi:
        return {
            "value": None,
            "error": f"No KPI with slug '{slug}' in this project.",
            "mode": None,
            "inputs": {},
            "warnings": [],
        }

    return await compute_kpi(kpi, date_range_start, date_range_end)
