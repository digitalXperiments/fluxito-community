"""
Tests for sql_safety.read_only_violation — the warehouse SELECT-only guard.

Replaces the old uppercase-substring blocklist that both false-positived on
column names (updated_at → "UPDATE") and missed verbs (MERGE, COPY, …).
See REMAINING "warehouse SELECT-only".
"""

from __future__ import annotations

import pytest

from app.sql_safety import read_only_violation


@pytest.mark.parametrize(
    "query",
    [
        "SELECT 1",
        "select * from orders",
        "SELECT updated_at, created_at FROM orders",  # was a false positive
        "SELECT * FROM t WHERE status = 'CREATE' AND note = 'do not DELETE'",  # keywords in strings
        "WITH recent AS (SELECT * FROM o WHERE updated_at > now()) SELECT count(*) FROM recent",
        "SELECT * FROM t -- DROP TABLE t\n WHERE id = 1",  # keyword in a comment
        "SELECT * FROM t;",  # single trailing semicolon is fine
    ],
)
def test_allows_read_only_queries(query):
    assert read_only_violation(query) is None, f"should allow: {query!r}"


@pytest.mark.parametrize(
    "query",
    [
        "DELETE FROM orders",
        "UPDATE orders SET x = 1",
        "INSERT INTO t VALUES (1)",
        "DROP TABLE t",
        "TRUNCATE t",
        "MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN UPDATE SET x = 1",  # missed by old blocklist
        "CALL some_proc()",
        "COPY t FROM 's3://x'",
        "SELECT 1; DROP TABLE t",  # second statement
        "WITH x AS (SELECT 1) DELETE FROM t",  # write hidden behind a CTE prefix
        "SELECT * INTO new_t FROM t",  # SELECT INTO is a write
        "",
        "   ",
    ],
)
def test_blocks_writes_and_multi_statements(query):
    assert read_only_violation(query) is not None, f"should block: {query!r}"


def test_engine_specific_prefixes():
    # Snowflake/Redshift also permit SHOW/DESCRIBE/EXPLAIN.
    extra = ("SELECT", "WITH", "SHOW", "DESCRIBE", "DESC", "EXPLAIN")
    assert read_only_violation("SHOW TABLES", allowed_prefixes=extra) is None
    assert read_only_violation("EXPLAIN SELECT 1", allowed_prefixes=extra) is None
    # but SHOW is not allowed under the default (BigQuery) prefix set
    assert read_only_violation("SHOW TABLES") is not None
    # and EXPLAIN of a write is still blocked by the denylist
    assert read_only_violation("EXPLAIN DELETE FROM t", allowed_prefixes=extra) is not None
