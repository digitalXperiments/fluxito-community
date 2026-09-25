"""
SQL identifier validation helpers.

Background
----------
Several tool and connector entry points accept identifier fragments
(schema, table, column, dataset, etc.) from user input and interpolate
them into SQL queries. Identifiers CANNOT be passed as SQL parameters —
every driver we use treats ``?`` / ``$1`` / ``%s`` as value placeholders,
not name placeholders. The only safe alternatives are:

    1. Validate against a strict regex allowlist, or
    2. Quote with the dialect-specific quoting rules.

This module does both. Use :func:`validate_identifier` for the allowlist
check (rejects anything that isn't ``[A-Za-z_][A-Za-z0-9_]*``). For
BigQuery-style qualified names, :func:`validate_qualified_identifier`
validates each dot-separated part. Use :func:`quote_identifier` if you
need to escape and emit a safe quoted form.

These helpers raise :class:`InvalidIdentifierError` on failure so callers
can propagate a structured error to the user without risking SQL injection.
"""

from __future__ import annotations

import re
from typing import Final

# Conservative allowlist — standard SQL unquoted identifiers.
# Accepts: starts with letter or underscore, contains only
# letters/digits/underscores, max 128 chars.
#
# Rejects: hyphens, quotes, semicolons, spaces, unicode, `--`, `/*`,
# and every other injection-vector character.
_IDENTIFIER_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")

# Max length for a fully qualified identifier (`project.dataset.table`)
_MAX_QUALIFIED_PARTS: Final[int] = 3
_MAX_IDENTIFIER_LENGTH: Final[int] = 128


class InvalidIdentifierError(ValueError):
    """Raised when an identifier fails validation."""

    def __init__(self, identifier: str, reason: str = "invalid format"):
        self.identifier = identifier
        super().__init__(
            f"Invalid SQL identifier {identifier!r}: {reason}. "
            f"Identifiers must match ^[A-Za-z_][A-Za-z0-9_]*$ and be <= 128 chars."
        )


def validate_identifier(value: str, *, field_name: str = "identifier") -> str:
    """
    Validate a single unquoted SQL identifier.

    Returns the (unchanged) value on success; raises
    :class:`InvalidIdentifierError` on failure.

    >>> validate_identifier("my_table")
    'my_table'
    >>> validate_identifier("users")
    'users'
    >>> validate_identifier("drop table users; --")  # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
    InvalidIdentifierError: ...
    """
    if not isinstance(value, str):
        raise InvalidIdentifierError(str(value), f"{field_name} must be a string")
    if not value:
        raise InvalidIdentifierError(value, f"{field_name} is empty")
    # NOTE: use ``fullmatch`` rather than ``match``. With the default
    # (non-MULTILINE) flag, ``$`` still matches *before* a trailing
    # newline, so ``"users\n"`` would otherwise sneak past this regex.
    # ``fullmatch`` anchors both ends strictly and closes that gap.
    if not _IDENTIFIER_RE.fullmatch(value):
        raise InvalidIdentifierError(value, f"{field_name} contains disallowed characters")
    return value


def validate_qualified_identifier(
    value: str,
    *,
    field_name: str = "qualified_identifier",
    max_parts: int = _MAX_QUALIFIED_PARTS,
) -> tuple[str, ...]:
    """
    Validate a dot-separated qualified identifier like ``project.dataset.table``.

    Each part must individually pass :func:`validate_identifier`. At most
    ``max_parts`` parts are permitted (default 3).

    Returns the validated parts as a tuple.
    """
    if not isinstance(value, str) or not value:
        raise InvalidIdentifierError(str(value), f"{field_name} is empty or not a string")
    parts = tuple(value.split("."))
    if len(parts) > max_parts:
        raise InvalidIdentifierError(
            value,
            f"{field_name} has too many parts ({len(parts)} > {max_parts})",
        )
    for i, part in enumerate(parts):
        try:
            validate_identifier(part, field_name=f"{field_name}[{i}]")
        except InvalidIdentifierError as e:
            raise InvalidIdentifierError(value, str(e)) from None
    return parts


def validate_positive_int(value: int, *, field_name: str, max_value: int = 10_000) -> int:
    """Validate an integer limit that will be interpolated into SQL.

    Rejects anything that isn't a plain positive int within ``max_value``.
    Booleans are explicitly rejected (bool is a subclass of int in Python).
    Returns the validated int.

    Raises:
        InvalidIdentifierError: If validation fails
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidIdentifierError(str(value), f"{field_name} must be an int, got {type(value).__name__}")
    if value <= 0:
        raise InvalidIdentifierError(str(value), f"{field_name} must be > 0")
    if value > max_value:
        raise InvalidIdentifierError(str(value), f"{field_name} must be <= {max_value}")
    return value


def quote_identifier(value: str, *, quote: str = '"') -> str:
    """
    Return a safely-quoted identifier for direct interpolation.

    The value is first validated; then any internal occurrence of the
    quote character is doubled (standard SQL escaping). Use this when
    you need to interpolate into a query string and the target dialect
    supports standard double-quoted identifiers (Postgres, Redshift,
    Snowflake). For BigQuery, pass ``quote='`'``.

    >>> quote_identifier("my_table")
    '"my_table"'
    >>> quote_identifier("table", quote="`")
    '`table`'
    """
    validate_identifier(value)
    escaped = value.replace(quote, quote + quote)
    return f"{quote}{escaped}{quote}"


# ---------------------------------------------------------------------------
# Read-only query enforcement
# ---------------------------------------------------------------------------
# Replaces the old per-connector uppercase-substring blocklist
# (``any(kw in query.upper())``), which both:
#   * false-positived on column names — `SELECT updated_at` matched "UPDATE",
#     `SELECT created_at` matched "CREATE"; and
#   * missed write verbs not on the list — MERGE, UPSERT, CALL, COPY, etc.
# This strips comments + string literals, requires a SINGLE statement that
# begins with an allowed prefix, and runs a word-boundary denylist as defense in
# depth. (stress-test 2026-06-12, REMAINING "warehouse SELECT-only".)

_SQL_COMMENT_RE: Final[re.Pattern[str]] = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_SQL_STRING_RE: Final[re.Pattern[str]] = re.compile(r"'(?:[^']|'')*'")
_FORBIDDEN_SQL_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|UPSERT|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|"
    r"REPLACE|CALL|EXEC|EXECUTE|COPY|UNLOAD|VACUUM|LOCK|PUT|REMOVE|INTO)\b",
    re.IGNORECASE,
)

_DEFAULT_READ_PREFIXES: Final[tuple[str, ...]] = ("SELECT", "WITH")


def read_only_violation(
    query: str, *, allowed_prefixes: tuple[str, ...] = _DEFAULT_READ_PREFIXES
) -> str | None:
    """Return an error message if ``query`` is not a single read-only statement.

    Returns ``None`` when the query is safe to run. ``allowed_prefixes`` is the
    set of statement-leading keywords permitted (default SELECT/WITH; engines
    that also expose SHOW/DESCRIBE/EXPLAIN pass those in).
    """
    if not query or not query.strip():
        return "Empty query."
    # Remove comments, then neutralise string literals so keywords inside them
    # (e.g. WHERE status = 'CREATE') and ';' inside them don't trip the checks.
    cleaned = _SQL_STRING_RE.sub("''", _SQL_COMMENT_RE.sub(" ", query))
    statements = [s for s in cleaned.split(";") if s.strip()]
    if len(statements) > 1:
        return "Only a single statement is permitted (found multiple ';'-separated statements)."
    stmt = (statements[0] if statements else "").strip()
    tokens = stmt.split(None, 1)
    first = tokens[0].upper() if tokens else ""
    allowed = tuple(p.upper() for p in allowed_prefixes)
    if first not in allowed:
        return (
            f"Only {', '.join(allowed)} queries are permitted "
            f"(statement begins with '{first or 'nothing'}')."
        )
    match = _FORBIDDEN_SQL_RE.search(stmt)
    if match:
        return f"Forbidden keyword '{match.group(0).upper()}' is not allowed in a read-only query."
    return None
