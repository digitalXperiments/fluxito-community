"""
Regression: get_score_history / run_audit(audit_score_history) SQL.

The original window clause was ``INTERVAL ':days days'`` — the ``:days`` bind sat
inside a quoted string literal, so Postgres received the placeholder as literal
text and the bound parameter never substituted (parameter-count mismatch at
execution). The fix multiplies a fixed 1-day interval by the bind so it sits
outside any literal. See FINDINGS S0/SQL.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.dialects import postgresql

from app.tools.save_audit_result_tools import _SCORE_HISTORY_SQL


def _render(sql: str) -> str:
    return str(text(sql).compile(dialect=postgresql.dialect()))


def test_days_bind_is_outside_any_string_literal():
    rendered = _render(_SCORE_HISTORY_SQL)
    # The day unit is a fixed literal; the count is a real bind multiplied in.
    assert "INTERVAL '1 day'" in rendered
    # The param must NOT be rendered inside a quoted string literal — that's the
    # bug (Postgres treats text in '...' literally, so the bind never applies).
    assert "'%(days)s" not in rendered
    # And `days` is a genuine bound parameter.
    assert "days" in text(_SCORE_HISTORY_SQL)._bindparams


def test_old_buggy_pattern_is_detectable():
    # Guard the guard: the previous pattern renders the placeholder INSIDE the
    # quoted literal, which this style of assertion catches.
    old = "SELECT 1 WHERE x >= NOW() - INTERVAL ':days days'"
    assert "'%(days)s" in _render(old)
