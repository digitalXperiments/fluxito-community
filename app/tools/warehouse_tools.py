"""
Warehouse Mega Tools — 3-tool split pattern
Mirrors the analytics_tools / gtm_tools pattern.

  warehouse_read   — discovery: list connections, datasets, tables, schemas
  warehouse_query  — execution: run SQL queries with safety guards
  warehouse_audit  — intelligence: health checks, stale tables, cost analysis

Auth: BQ uses per-user service-account credentials stored in the bq_connections
table — not OAuth. All tools resolve the connection from app_state.
User identity is never a parameter — always resolved via app_state.

Currently implemented engines: bigquery
Scaffolded (graceful stubs): redshift, snowflake
"""

from datetime import UTC
from typing import Literal

import app.app_state as state
from app.config import settings
from app.tools.shared_helpers import (
    decrypt_field,
    get_current_user,
    get_encrypted_credential_conn,
    list_active_connections,
)


def _user():
    return get_current_user()


def _paginate(result: dict, offset: int = 0, chunk_size: int | None = None) -> dict:
    """Wrap a result dict in chunk metadata so the caller can stream.

    If chunk_size is None or the row list is <=100, returns the result
    unchanged. Otherwise slices rows[offset:offset+chunk_size] and adds
    a _chunk dict with pagination hints.
    """
    if not isinstance(result, dict) or chunk_size is None:
        return result
    rows = result.get("rows")
    if not isinstance(rows, list):
        return result
    total = len(rows)
    if total <= 100 and chunk_size >= total:
        return result
    start = max(0, int(offset))
    end = min(total, start + max(1, int(chunk_size)))
    sliced = rows[start:end]
    out = dict(result)
    out["rows"] = sliced
    out["_chunk"] = {
        "chunk_index": start // max(1, int(chunk_size)),
        "chunk_rows": len(sliced),
        "chunk_offset": start,
        "total_rows": total,
        "next_offset": end if end < total else None,
        "has_more": end < total,
    }
    return out


def _no_bq_response():
    return {
        "error": True,
        "error_type": "connection_missing",
        "message": "No BigQuery connection found for your account.",
        "action_required": f"Visit {settings.APP_BASE_URL}/connect/bigquery to add a service account.",
    }


def _no_redshift_response():
    return {
        "error": True,
        "error_type": "connection_missing",
        "message": "No Redshift connection found for your account.",
        "action_required": f"Visit {settings.APP_BASE_URL}/connect/redshift to add your connection.",
    }


def _no_snowflake_response():
    return {
        "error": True,
        "error_type": "connection_missing",
        "message": "No Snowflake connection found for your account.",
        "action_required": f"Visit {settings.APP_BASE_URL}/connect/snowflake to add your connection.",
    }


async def _get_redshift_conn(user_id: str, connection_id: str = None):
    from app.models.credential_connection import RedshiftConnection

    conn = await get_encrypted_credential_conn(RedshiftConnection, user_id, connection_id)
    if not conn:
        return None
    return {
        "id": str(conn.id),
        "display_name": conn.display_name,
        "host": decrypt_field(conn.host_encrypted),
        "port": conn.port,
        "database": conn.database,
        "user": decrypt_field(conn.username_encrypted),
        "password": decrypt_field(conn.password_encrypted),
        "default_schema": conn.default_schema,
    }


async def _get_snowflake_conn(user_id: str, connection_id: str = None):
    from app.models.credential_connection import SnowflakeConnection

    conn = await get_encrypted_credential_conn(SnowflakeConnection, user_id, connection_id)
    if not conn:
        return None
    return {
        "id": str(conn.id),
        "display_name": conn.display_name,
        "account": decrypt_field(conn.account_encrypted),
        "user": decrypt_field(conn.username_encrypted),
        "password": decrypt_field(conn.password_encrypted),
        "warehouse": conn.warehouse,
        "database": conn.database,
        "default_schema": conn.default_schema,
        "role": conn.role,
    }


async def _list_all_redshift_connections(user_id: str) -> list:
    from app.models.credential_connection import RedshiftConnection

    return await list_active_connections(RedshiftConnection, user_id)


async def _list_all_snowflake_connections(user_id: str) -> list:
    from app.models.credential_connection import SnowflakeConnection

    return await list_active_connections(SnowflakeConnection, user_id)


async def _get_bq_conn(user_id: str, connection_id: str | None = None):
    """Fetch a BQConnection row for the given user from the DB.

    Smart default: if connection_id is omitted and the user has exactly one
    BQ connection, it is auto-selected (no need to ask the user)."""
    from app.models.bq_connection import BQConnection

    if not connection_id:
        conns = await list_active_connections(BQConnection, user_id)
        if len(conns) == 1:
            return conns[0]
    return await get_encrypted_credential_conn(BQConnection, user_id, connection_id)


async def _list_all_connections(user_id: str) -> list:
    from app.models.bq_connection import BQConnection

    return await list_active_connections(BQConnection, user_id)


# ---------------------------------------------------------------------------
# In-process entrypoint for warehouse_query
# ---------------------------------------------------------------------------
# The warehouse_query tool is a closure registered inside
# register_warehouse_tools(), so it cannot be imported directly. run_analysis's
# revenue adapter previously did `from app.tools.warehouse_tools import
# warehouse_query`, which ALWAYS raised ImportError — silently swallowed, so the
# warehouse revenue ground-truth path never ran (FINDINGS S1 #6).
# register_warehouse_tools() now publishes the registered function here, and
# ``warehouse_query_impl`` is the stable, importable handle for in-process callers.
_WAREHOUSE_QUERY_FN = None


async def warehouse_query_impl(**kwargs) -> dict:
    """Run a warehouse_query action in-process (engine, action, query, …).

    Returns the same shape as the warehouse_query MCP tool. Available once the
    warehouse tools have been registered (register_warehouse_tools()).
    """
    if _WAREHOUSE_QUERY_FN is None:
        return {
            "error": True,
            "error_type": "not_registered",
            "message": "Warehouse tools are not registered yet.",
        }
    return await _WAREHOUSE_QUERY_FN(**kwargs)


def register_warehouse_tools(mcp_server):
    # -------------------------------------------------------------------------
    # warehouse_read — Layer 1: Discovery
    # -------------------------------------------------------------------------

    @mcp_server.tool("warehouse_read")
    async def warehouse_read(
        engine: Literal["bigquery", "redshift", "snowflake"] | None = None,
        action: str = "",
        connection_id: str | None = None,
        dataset_id: str | None = None,
        table_id: str | None = None,
    ) -> dict:
        """Discovers warehouse metadata. Use warehouse_query to run SQL, warehouse_audit for health.

        engine: bigquery | redshift | snowflake

        All engines: list_connections, list_tables(dataset_id), get_table_schema(dataset_id+table_id)
        BQ: list_datasets(connection_id?)
        Redshift: list_schemas(connection_id?)
        Snowflake: list_databases, list_schemas(dataset_id), list_warehouses
        """
        if not engine:
            return {
                "error": True,
                "error_type": "missing_required_param",
                "message": "engine is required. Pass engine='bigquery', 'redshift', or 'snowflake' in params.",
            }
        u = _user()
        if not u:
            return _no_bq_response()

        # Validate action name upfront per engine
        _VALID_ACTIONS = {
            "bigquery": {"list_connections", "list_datasets", "list_tables", "get_table_schema"},
            "redshift": {"list_connections", "list_schemas", "list_tables", "get_table_schema"},
            "snowflake": {
                "list_connections",
                "list_databases",
                "list_schemas",
                "list_tables",
                "get_table_schema",
                "list_warehouses",
            },
        }
        valid = _VALID_ACTIONS.get(engine, set())
        if action not in valid:
            return {
                "error": True,
                "message": f"Unknown action '{action}' for {engine} warehouse_read. "
                f"Valid actions: {', '.join(sorted(valid))}",
            }

        if engine == "bigquery":
            if action == "list_connections":
                connections = await _list_all_connections(u.user_id)
                return {
                    "connections": [
                        {
                            "connection_id": str(c.id),
                            "display_name": c.display_name,
                            "project_id": c.project_id,
                            "datasets": c.datasets or [],
                            "connection_status": c.connection_status,
                            "created_at": c.created_at.isoformat() if c.created_at else None,
                        }
                        for c in connections
                    ]
                }

            # All actions below require an active connection
            conn = await _get_bq_conn(u.user_id, connection_id)
            if not conn:
                return _no_bq_response()

            from app.tools.cache import META_TTL, SCHEMA_TTL, build_key, cache_get, cache_set

            if action == "list_datasets":
                key = build_key("bq:list_datasets", u.user_id, conn.id, conn.project_id)
                hit = await cache_get(key)
                if hit is not None:
                    return hit
                res = await state.bq_connector.list_datasets(conn.service_account_encrypted, conn.project_id)
                if not (isinstance(res, dict) and res.get("error")):
                    await cache_set(key, res, ttl=META_TTL)
                return res

            elif action == "list_tables":
                if not dataset_id:
                    return {"error": True, "message": "dataset_id is required for list_tables"}
                key = build_key("bq:list_tables", u.user_id, conn.id, conn.project_id, dataset_id)
                hit = await cache_get(key)
                if hit is not None:
                    return hit
                res = await state.bq_connector.list_tables(
                    conn.service_account_encrypted, conn.project_id, dataset_id
                )
                if not (isinstance(res, dict) and res.get("error")):
                    await cache_set(key, res, ttl=META_TTL)
                return res

            elif action == "get_table_schema":
                if not dataset_id or not table_id:
                    return {
                        "error": True,
                        "message": "dataset_id and table_id are required for get_table_schema",
                    }
                key = build_key(
                    "bq:get_table_schema", u.user_id, conn.id, conn.project_id, dataset_id, table_id
                )
                hit = await cache_get(key)
                if hit is not None:
                    return hit
                res = await state.bq_connector.get_table_schema(
                    conn.service_account_encrypted, conn.project_id, dataset_id, table_id
                )
                if not (isinstance(res, dict) and res.get("error")):
                    await cache_set(key, res, ttl=SCHEMA_TTL)
                return res

            return {"error": True, "message": f"Unknown action '{action}' for warehouse_read"}

        elif engine == "redshift":
            if action == "list_connections":
                connections = await _list_all_redshift_connections(u.user_id)
                return {
                    "connections": [
                        {
                            "connection_id": str(c.id),
                            "display_name": c.display_name,
                            "database": c.database,
                            "default_schema": c.default_schema,
                            "connection_status": c.connection_status,
                            "created_at": c.created_at.isoformat() if c.created_at else None,
                        }
                        for c in connections
                    ]
                }
            creds = await _get_redshift_conn(u.user_id, connection_id)
            if not creds:
                return _no_redshift_response()
            rs = state.redshift_connector

            if action == "list_schemas":
                return await rs.list_schemas(
                    creds["host"], creds["port"], creds["database"], creds["user"], creds["password"]
                )
            elif action == "list_tables":
                schema = dataset_id or creds["default_schema"]
                return await rs.list_tables(
                    creds["host"], creds["port"], creds["database"], creds["user"], creds["password"], schema
                )
            elif action == "get_table_schema":
                if not table_id:
                    return {"error": True, "message": "table_id is required for get_table_schema"}
                schema = dataset_id or creds["default_schema"]
                return await rs.get_table_schema(
                    creds["host"],
                    creds["port"],
                    creds["database"],
                    creds["user"],
                    creds["password"],
                    schema,
                    table_id,
                )
            return {"error": True, "message": f"Unknown action '{action}' for Redshift warehouse_read"}

        elif engine == "snowflake":
            if action == "list_connections":
                connections = await _list_all_snowflake_connections(u.user_id)
                return {
                    "connections": [
                        {
                            "connection_id": str(c.id),
                            "display_name": c.display_name,
                            "warehouse": c.warehouse,
                            "database": c.database,
                            "default_schema": c.default_schema,
                            "connection_status": c.connection_status,
                            "created_at": c.created_at.isoformat() if c.created_at else None,
                        }
                        for c in connections
                    ]
                }
            creds = await _get_snowflake_conn(u.user_id, connection_id)
            if not creds:
                return _no_snowflake_response()
            sf = state.snowflake_connector

            if action == "list_databases":
                return await sf.list_databases(
                    creds["account"], creds["user"], creds["password"], creds["warehouse"], creds.get("role")
                )
            elif action == "list_schemas":
                return await sf.list_schemas(
                    creds["account"],
                    creds["user"],
                    creds["password"],
                    creds["warehouse"],
                    creds["database"],
                    creds.get("role"),
                )
            elif action == "list_tables":
                schema = dataset_id or creds["default_schema"]
                return await sf.list_tables(
                    creds["account"],
                    creds["user"],
                    creds["password"],
                    creds["warehouse"],
                    creds["database"],
                    schema,
                    creds.get("role"),
                )
            elif action == "get_table_schema":
                if not table_id:
                    return {"error": True, "message": "table_id is required for get_table_schema"}
                schema = dataset_id or creds["default_schema"]
                return await sf.get_table_schema(
                    creds["account"],
                    creds["user"],
                    creds["password"],
                    creds["warehouse"],
                    creds["database"],
                    schema,
                    table_id,
                    creds.get("role"),
                )
            elif action == "list_warehouses":
                return await sf.list_warehouses(
                    creds["account"], creds["user"], creds["password"], creds.get("role")
                )
            return {"error": True, "message": f"Unknown action '{action}' for Snowflake warehouse_read"}

        return {"error": True, "message": f"Unknown engine '{engine}'"}

    # -------------------------------------------------------------------------
    # warehouse_query — Layer 2: Query execution
    # -------------------------------------------------------------------------

    @mcp_server.tool("warehouse_query")
    async def warehouse_query(
        engine: Literal["bigquery", "redshift", "snowflake"] | None = None,
        action: str = "run_query",
        query: str | None = None,
        connection_id: str | None = None,
        dataset_id: str | None = None,
        table_id: str | None = None,
        max_results: int = 1000,
        offset: int = 0,
        chunk_size: int | None = None,
    ) -> dict:
        """Execute SQL on the warehouse. SELECT only, max 5000 rows.

        engine: bigquery | redshift | snowflake
        All: run_query(query), preview_table(dataset_id+table_id)
        BQ: dry_run(query) — estimate cost
        RS/SF: explain_query(query) — execution plan

        Chunked results: pass chunk_size (e.g. 500) to paginate large results.
        The response includes _chunk with has_more / next_offset — call again
        with offset=next_offset to continue.

        Smart defaults: if connection_id is omitted and the user has exactly
        one connection for the engine, it is auto-selected.

        Call tool_help("warehouse_query") for the full reference.
        """
        if not engine:
            return {
                "error": True,
                "error_type": "missing_required_param",
                "message": "engine is required. Pass engine='bigquery', 'redshift', or 'snowflake' in params.",
            }
        u = _user()
        if not u:
            return _no_bq_response()

        # Validate action name upfront per engine
        _VALID_QUERY_ACTIONS = {
            "bigquery": {"run_query", "preview_table", "dry_run"},
            "redshift": {"run_query", "preview_table", "explain_query"},
            "snowflake": {"run_query", "preview_table", "explain_query"},
        }
        valid_q = _VALID_QUERY_ACTIONS.get(engine, set())
        if action not in valid_q:
            return {
                "error": True,
                "message": f"Unknown action '{action}' for {engine} warehouse_query. "
                f"Valid actions: {', '.join(sorted(valid_q))}",
            }

        if engine == "bigquery":
            conn = await _get_bq_conn(u.user_id, connection_id)
            if not conn:
                return _no_bq_response()

            if action == "run_query":
                if not query:
                    return {"error": True, "message": "query is required for run_query"}
                limit = min(max_results, 5000)
                res = await state.bq_connector.run_query(
                    conn.service_account_encrypted, conn.project_id, query, limit
                )
                return _paginate(res, offset=offset, chunk_size=chunk_size)

            elif action == "preview_table":
                if not dataset_id or not table_id:
                    return {
                        "error": True,
                        "message": "dataset_id and table_id are required for preview_table",
                    }
                # Identifiers cannot be bound as query parameters. Validate them
                # against the strict allowlist and quote with BigQuery backticks
                # before splicing into SQL. See app.sql_safety.
                #
                # BigQuery project IDs may contain hyphens (e.g.
                # ``my-project-123``), which the standard allowlist rejects.
                # For the project ID we apply a BQ-specific character check
                # and then wrap in backticks manually. The dataset and table
                # names use the standard strict allowlist.
                import re as _re_bq

                from app.sql_safety import (
                    InvalidIdentifierError,
                    quote_identifier,
                    validate_identifier,
                    validate_positive_int,
                )

                try:
                    validate_identifier(dataset_id, field_name="dataset_id")
                    validate_identifier(table_id, field_name="table_id")
                    proj = str(conn.project_id or "")
                    # BigQuery project IDs: 6-30 chars, lowercase letters,
                    # digits, hyphens; must start with a letter. We stay
                    # defensive — this value comes from our DB but we never
                    # want to trust it implicitly.
                    if not _re_bq.match(r"^[a-zA-Z][a-zA-Z0-9\-]{0,63}$", proj):
                        return {
                            "error": True,
                            "error_type": "invalid_identifier",
                            "message": "connection project_id is not a valid BigQuery project ID",
                        }
                    safe_limit = validate_positive_int(
                        min(max_results, 200), field_name="max_results", max_value=10_000
                    )
                    safe_dataset = quote_identifier(dataset_id, quote="`")
                    safe_table = quote_identifier(table_id, quote="`")
                    safe_proj = f"`{proj}`"  # already character-checked above
                except InvalidIdentifierError as e:
                    return {"error": True, "error_type": "invalid_identifier", "message": str(e)}
                preview_sql = f"SELECT * FROM {safe_proj}.{safe_dataset}.{safe_table} LIMIT {safe_limit}"
                return await state.bq_connector.run_query(
                    conn.service_account_encrypted, conn.project_id, preview_sql, safe_limit
                )

            elif action == "dry_run":
                if not query:
                    return {"error": True, "message": "query is required for dry_run"}
                return await state.bq_connector.dry_run(
                    conn.service_account_encrypted, conn.project_id, query
                )

            return {"error": True, "message": f"Unknown action '{action}' for warehouse_query"}

        elif engine == "redshift":
            creds = await _get_redshift_conn(u.user_id, connection_id)
            if not creds:
                return _no_redshift_response()
            rs = state.redshift_connector

            if action == "run_query":
                if not query:
                    return {"error": True, "message": "query is required for run_query"}
                res = await rs.run_query(
                    creds["host"],
                    creds["port"],
                    creds["database"],
                    creds["user"],
                    creds["password"],
                    query,
                    min(max_results, 5000),
                )
                return _paginate(res, offset=offset, chunk_size=chunk_size)
            elif action == "preview_table":
                if not dataset_id or not table_id:
                    return {
                        "error": True,
                        "message": "dataset_id (schema) and table_id are required for preview_table",
                    }
                return await rs.preview_table(
                    creds["host"],
                    creds["port"],
                    creds["database"],
                    creds["user"],
                    creds["password"],
                    dataset_id,
                    table_id,
                    min(max_results, 200),
                )
            elif action == "explain_query":
                if not query:
                    return {"error": True, "message": "query is required for explain_query"}
                return await rs.explain_query(
                    creds["host"], creds["port"], creds["database"], creds["user"], creds["password"], query
                )
            return {"error": True, "message": f"Unknown action '{action}' for Redshift warehouse_query"}

        elif engine == "snowflake":
            creds = await _get_snowflake_conn(u.user_id, connection_id)
            if not creds:
                return _no_snowflake_response()
            sf = state.snowflake_connector

            if action == "run_query":
                if not query:
                    return {"error": True, "message": "query is required for run_query"}
                res = await sf.run_query(
                    creds["account"],
                    creds["user"],
                    creds["password"],
                    creds["warehouse"],
                    creds["database"],
                    creds.get("default_schema", "PUBLIC"),
                    query,
                    min(max_results, 5000),
                    creds.get("role"),
                )
                return _paginate(res, offset=offset, chunk_size=chunk_size)
            elif action == "preview_table":
                if not dataset_id or not table_id:
                    return {
                        "error": True,
                        "message": "dataset_id (schema) and table_id are required for preview_table",
                    }
                return await sf.preview_table(
                    creds["account"],
                    creds["user"],
                    creds["password"],
                    creds["warehouse"],
                    creds["database"],
                    dataset_id,
                    table_id,
                    min(max_results, 200),
                    creds.get("role"),
                )
            elif action == "explain_query":
                if not query:
                    return {"error": True, "message": "query is required for explain_query"}
                return await sf.explain_query(
                    creds["account"],
                    creds["user"],
                    creds["password"],
                    creds["warehouse"],
                    creds["database"],
                    creds.get("default_schema", "PUBLIC"),
                    query,
                    creds.get("role"),
                )
            return {"error": True, "message": f"Unknown action '{action}' for Snowflake warehouse_query"}

        return {"error": True, "message": f"Unknown engine '{engine}'"}

    # Publish the registered warehouse_query so in-process callers (run_analysis's
    # revenue adapter) can invoke it without an MCP round-trip. See
    # warehouse_query_impl above (FINDINGS S1 #6).
    global _WAREHOUSE_QUERY_FN
    _WAREHOUSE_QUERY_FN = warehouse_query

    # -------------------------------------------------------------------------
    # warehouse_audit — Layer 3: Health, cost and governance
    # -------------------------------------------------------------------------

    @mcp_server.tool("warehouse_audit")
    async def warehouse_audit(
        engine: Literal["bigquery", "redshift", "snowflake"] | None = None,
        action: str = "",
        connection_id: str | None = None,
        dataset_id: str | None = None,
        table_id: str | None = None,
        days_stale: int = 30,
    ) -> dict:
        """Audits warehouse health, freshness, and governance.

        engine: bigquery | redshift | snowflake

        All: connection_health, find_stale_tables(dataset_id,days_stale?=30)
        BQ: audit_dataset(dataset_id), check_empty_tables(dataset_id)
        RS: audit_schema(dataset_id), check_table_health(dataset_id+table_id)
        SF: audit_schema(dataset_id), check_clustering_health(dataset_id+table_id), get_warehouse_usage(days_stale?)
        """
        if not engine:
            return {
                "error": True,
                "error_type": "missing_required_param",
                "message": "engine is required. Pass engine='bigquery', 'redshift', or 'snowflake' in params.",
            }
        u = _user()
        if not u:
            return _no_bq_response()

        # Validate action name upfront per engine
        _VALID_AUDIT_ACTIONS = {
            "bigquery": {"connection_health", "audit_dataset", "find_stale_tables", "check_empty_tables"},
            "redshift": {"connection_health", "audit_schema", "find_stale_tables", "check_table_health"},
            "snowflake": {
                "connection_health",
                "audit_schema",
                "find_stale_tables",
                "check_clustering_health",
                "get_warehouse_usage",
            },
        }
        valid_a = _VALID_AUDIT_ACTIONS.get(engine, set())
        if action not in valid_a:
            return {
                "error": True,
                "message": f"Unknown action '{action}' for {engine} warehouse_audit. "
                f"Valid actions: {', '.join(sorted(valid_a))}",
            }

        if engine == "bigquery":
            conn = await _get_bq_conn(u.user_id, connection_id)
            if not conn:
                return _no_bq_response()

            if action == "connection_health":
                # Verify connection by listing datasets — a lightweight healthcheck
                result = await state.bq_connector.list_datasets(
                    conn.service_account_encrypted, conn.project_id
                )
                ok = "error" not in result
                return {
                    "healthy": ok,
                    "connection_id": str(conn.id),
                    "display_name": conn.display_name,
                    "project_id": conn.project_id,
                    "dataset_count": len(result.get("datasets", [])) if ok else 0,
                    "error": result.get("message") if not ok else None,
                }

            if not dataset_id:
                return {"error": True, "message": f"dataset_id is required for action '{action}'"}

            if action == "audit_dataset":
                import asyncio

                MAX_TABLES = 50
                PER_TABLE_TIMEOUT = 15  # seconds
                CONCURRENCY = 5

                # List all tables and return schema + stats for each
                tables_resp = await asyncio.wait_for(
                    state.bq_connector.list_tables(
                        conn.service_account_encrypted, conn.project_id, dataset_id
                    ),
                    timeout=30,
                )
                if "error" in tables_resp:
                    return tables_resp

                all_tables = tables_resp.get("tables", [])
                truncated = len(all_tables) > MAX_TABLES
                tables_to_scan = all_tables[:MAX_TABLES]

                semaphore = asyncio.Semaphore(CONCURRENCY)

                async def _fetch_one(t: dict) -> dict:
                    async with semaphore:
                        try:
                            schema_resp = await asyncio.wait_for(
                                state.bq_connector.get_table_schema(
                                    conn.service_account_encrypted,
                                    conn.project_id,
                                    dataset_id,
                                    t["table_id"],
                                ),
                                timeout=PER_TABLE_TIMEOUT,
                            )
                            return {
                                "table_id": t["table_id"],
                                "table_type": t.get("table_type"),
                                "num_rows": schema_resp.get("num_rows"),
                                "num_bytes": schema_resp.get("num_bytes"),
                                "column_count": len(schema_resp.get("schema", [])),
                            }
                        except TimeoutError:
                            return {
                                "table_id": t["table_id"],
                                "table_type": t.get("table_type"),
                                "num_rows": None,
                                "num_bytes": None,
                                "column_count": None,
                                "error": "Timed out fetching table metadata",
                            }

                summaries = await asyncio.gather(*[_fetch_one(t) for t in tables_to_scan])

                result = {
                    "dataset_id": dataset_id,
                    "project_id": conn.project_id,
                    "table_count": len(all_tables),
                    "tables_scanned": len(tables_to_scan),
                    "tables": list(summaries),
                }
                if truncated:
                    result["warning"] = (
                        f"Dataset has {len(all_tables)} tables; only the first {MAX_TABLES} were scanned."
                    )
                return result

            elif action == "find_stale_tables":
                # Tables whose modified_time is older than days_stale
                tables_resp = await state.bq_connector.list_tables(
                    conn.service_account_encrypted, conn.project_id, dataset_id
                )
                if "error" in tables_resp:
                    return tables_resp
                stale = []
                import asyncio
                from datetime import datetime, timedelta

                cutoff = datetime.now(UTC) - timedelta(days=days_stale)

                async def _check_table(t):
                    schema_info = await state.bq_connector.get_table_schema(
                        conn.service_account_encrypted, conn.project_id, dataset_id, t["table_id"]
                    )
                    modified = schema_info.get("modified_time")
                    if modified:
                        try:
                            mod_dt = datetime.fromisoformat(modified)
                            if mod_dt < cutoff:
                                stale.append(
                                    {
                                        "table_id": t["table_id"],
                                        "last_modified": modified,
                                        "num_rows": schema_info.get("num_rows"),
                                    }
                                )
                        except Exception:
                            pass

                await asyncio.gather(*[_check_table(t) for t in tables_resp.get("tables", [])])
                return {
                    "dataset_id": dataset_id,
                    "days_stale_threshold": days_stale,
                    "stale_table_count": len(stale),
                    "stale_tables": stale,
                }

            elif action == "check_empty_tables":
                tables_resp = await state.bq_connector.list_tables(
                    conn.service_account_encrypted, conn.project_id, dataset_id
                )
                if "error" in tables_resp:
                    return tables_resp
                empty = []
                for t in tables_resp.get("tables", []):
                    schema_info = await state.bq_connector.get_table_schema(
                        conn.service_account_encrypted, conn.project_id, dataset_id, t["table_id"]
                    )
                    if schema_info.get("num_rows", 1) == 0:
                        empty.append({"table_id": t["table_id"], "num_bytes": schema_info.get("num_bytes")})
                return {
                    "dataset_id": dataset_id,
                    "empty_table_count": len(empty),
                    "empty_tables": empty,
                }

            return {"error": True, "message": f"Unknown action '{action}' for warehouse_audit"}

        elif engine == "redshift":
            creds = await _get_redshift_conn(u.user_id, connection_id)
            if not creds:
                return _no_redshift_response()
            rs = state.redshift_connector

            if action == "connection_health":
                return await rs.connection_health(
                    creds["host"], creds["port"], creds["database"], creds["user"], creds["password"]
                )
            elif action == "audit_schema":
                schema = dataset_id or creds["default_schema"]
                return await rs.audit_schema(
                    creds["host"], creds["port"], creds["database"], creds["user"], creds["password"], schema
                )
            elif action == "find_stale_tables":
                schema = dataset_id or creds["default_schema"]
                return await rs.find_stale_tables(
                    creds["host"],
                    creds["port"],
                    creds["database"],
                    creds["user"],
                    creds["password"],
                    schema,
                    days_stale,
                )
            elif action == "check_table_health":
                if not dataset_id or not table_id:
                    return {"error": True, "message": "dataset_id (schema) and table_id are required"}
                return await rs.check_table_health(
                    creds["host"],
                    creds["port"],
                    creds["database"],
                    creds["user"],
                    creds["password"],
                    dataset_id,
                    table_id,
                )
            return {"error": True, "message": f"Unknown action '{action}' for Redshift warehouse_audit"}

        elif engine == "snowflake":
            creds = await _get_snowflake_conn(u.user_id, connection_id)
            if not creds:
                return _no_snowflake_response()
            sf = state.snowflake_connector

            if action == "connection_health":
                return await sf.connection_health(
                    creds["account"], creds["user"], creds["password"], creds["warehouse"], creds.get("role")
                )
            elif action == "audit_schema":
                schema = dataset_id or creds["default_schema"]
                return await sf.audit_schema(
                    creds["account"],
                    creds["user"],
                    creds["password"],
                    creds["warehouse"],
                    creds["database"],
                    schema,
                    creds.get("role"),
                )
            elif action == "find_stale_tables":
                schema = dataset_id or creds["default_schema"]
                return await sf.find_stale_tables(
                    creds["account"],
                    creds["user"],
                    creds["password"],
                    creds["warehouse"],
                    creds["database"],
                    schema,
                    days_stale,
                    creds.get("role"),
                )
            elif action == "check_clustering_health":
                if not dataset_id or not table_id:
                    return {"error": True, "message": "dataset_id (schema) and table_id are required"}
                return await sf.check_clustering_health(
                    creds["account"],
                    creds["user"],
                    creds["password"],
                    creds["warehouse"],
                    creds["database"],
                    dataset_id,
                    table_id,
                    creds.get("role"),
                )
            elif action == "get_warehouse_usage":
                return await sf.get_warehouse_usage(
                    creds["account"],
                    creds["user"],
                    creds["password"],
                    creds["warehouse"],
                    days_stale,
                    creds.get("role"),
                )
            return {"error": True, "message": f"Unknown action '{action}' for Snowflake warehouse_audit"}

        return {"error": True, "message": f"Unknown engine '{engine}'"}
