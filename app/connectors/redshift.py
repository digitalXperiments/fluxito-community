"""
Amazon Redshift Connector

Uses redshift_connector library for database connections.

Auth: Credential-based (host, port, database, user, password) stored encrypted in DB.
All methods accept decrypted credentials.
Queries run in a thread pool to avoid blocking the event loop.

Layer 1 (Read): list_schemas, list_tables, get_table_schema, get_table_stats

Layer 2 (Query): run_query, preview_table, explain_query

Layer 3 (Audit): audit_schema, find_stale_tables, check_table_health, connection_health
"""

import asyncio
import datetime as _dt
import logging
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal as _Decimal

from app.connectors._conn_pool import ConnectionPool, redshift_pool
from app.connectors.errors import friendly_errors

logger = logging.getLogger(__name__)

# Shared thread pool for database queries
_db_thread_pool = ThreadPoolExecutor(max_workers=10, thread_name_prefix="redshift")


class RedshiftConnector:
    """Interfaces with Amazon Redshift using credential-based connections."""

    def _build_connection(self, host: str, port: int, database: str, user: str, password: str):
        """Build a Redshift connection object."""
        try:
            import redshift_connector

            return redshift_connector.connect(
                host=host,
                port=port,
                database=database,
                user=user,
                password=password,
            )
        except Exception as e:
            logger.error(f"Redshift connection error: {e}")
            return None

    async def _execute_query(
        self,
        host: str,
        port: int,
        database: str,
        user: str,
        password: str,
        query: str,
        fetch_all: bool = True,
    ) -> dict:
        """
        Execute a query in the thread pool.
        Returns results as list of dicts.
        """

        def _run():
            # Connection pool: reuse a warm connection if one is available
            # for this credential set. Builder is only invoked on miss.
            pool_key = ConnectionPool.key_for("rs", host, port, database, user)
            builder = lambda: self._build_connection(host, port, database, user, password)
            try:
                with redshift_pool.checkout(pool_key, builder) as conn:
                    if not conn:
                        return {"error": True, "message": "Failed to connect to Redshift"}
                    cursor = conn.cursor()
                    cursor.execute(query)
                    if fetch_all:
                        col_names = [desc[0] for desc in cursor.description] if cursor.description else []
                        rows = cursor.fetchall()
                        results = [
                            {
                                k: (
                                    float(v)
                                    if isinstance(v, _Decimal)
                                    else v.isoformat()
                                    if isinstance(v, (_dt.date, _dt.datetime))
                                    else v
                                )
                                for k, v in zip(col_names, row, strict=False)
                            }
                            for row in rows
                        ]
                        cursor.close()
                        return {"rows": results, "columns": col_names, "row_count": len(results)}
                    cursor.close()
                    return {"success": True}
            except Exception as e:
                logger.error(f"Redshift query error: {e}")
                return {"error": True, "message": str(e)}

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_db_thread_pool, _run)

    # ------------------------------------------------------------------
    # Layer 1: Data Access
    # ------------------------------------------------------------------

    @friendly_errors("Redshift")
    async def list_schemas(self, host: str, port: int, database: str, user: str, password: str) -> dict:
        """
        List all schemas in the database.
        Uses information_schema.schemata first, falls back to pg_tables
        if the Redshift user lacks USAGE on system views.
        """
        query = "SELECT schema_name FROM information_schema.schemata ORDER BY schema_name"
        result = await self._execute_query(host, port, database, user, password, query)

        if result.get("error"):
            return result

        schemas = [row["schema_name"] for row in result.get("rows", [])]

        # Fallback: if information_schema returned nothing (permission issue),
        # query pg_tables which the user likely has SELECT on.
        if not schemas:
            fallback_query = (
                "SELECT DISTINCT schemaname AS schema_name FROM pg_tables "
                "WHERE schemaname NOT IN ('pg_catalog', 'information_schema', 'pg_internal') "
                "ORDER BY schemaname"
            )
            fallback_result = await self._execute_query(host, port, database, user, password, fallback_query)
            if not fallback_result.get("error"):
                schemas = [row["schema_name"] for row in fallback_result.get("rows", [])]

        return {
            "database": database,
            "schemas": schemas,
            "total": len(schemas),
        }

    @friendly_errors("Redshift")
    async def list_tables(
        self,
        host: str,
        port: int,
        database: str,
        user: str,
        password: str,
        schema: str = "public",
    ) -> dict:
        """
        List all tables in a schema.
        SELECT table_name, table_type FROM information_schema.tables WHERE table_schema = schema
        """
        query = f"""
            SELECT table_name, table_type
            FROM information_schema.tables
            WHERE table_schema = '{schema}'
            ORDER BY table_name
        """
        result = await self._execute_query(host, port, database, user, password, query)

        if result.get("error"):
            return result

        tables = [
            {
                "table_name": row.get("table_name"),
                "table_type": row.get("table_type"),
            }
            for row in result.get("rows", [])
        ]
        return {
            "schema": schema,
            "tables": tables,
            "total": len(tables),
        }

    @friendly_errors("Redshift")
    async def get_table_schema(
        self,
        host: str,
        port: int,
        database: str,
        user: str,
        password: str,
        schema: str,
        table: str,
    ) -> dict:
        """
        Get schema (columns) for a table.
        SELECT column_name, data_type, ... FROM information_schema.columns
        """
        query = f"""
            SELECT column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = '{schema}' AND table_name = '{table}'
            ORDER BY ordinal_position
        """
        result = await self._execute_query(host, port, database, user, password, query)

        if result.get("error"):
            return result

        columns = [
            {
                "column_name": row.get("column_name"),
                "data_type": row.get("data_type"),
                "is_nullable": row.get("is_nullable"),
                "column_default": row.get("column_default"),
            }
            for row in result.get("rows", [])
        ]
        return {
            "schema": schema,
            "table": table,
            "columns": columns,
            "column_count": len(columns),
        }

    @friendly_errors("Redshift")
    async def get_table_stats(
        self,
        host: str,
        port: int,
        database: str,
        user: str,
        password: str,
        schema: str,
        table: str,
    ) -> dict:
        """
        Get table statistics from SVV_TABLE_INFO.
        Returns row count, size in MB, encoding, sort keys, dist keys.
        """
        query = f"""
            SELECT schema, table_id, tbl_rows, size, sortkey1, diststyle, unsorted, encoded
            FROM svv_table_info
            WHERE schema = '{schema}' AND "table" = '{table}'
        """
        result = await self._execute_query(host, port, database, user, password, query)

        if result.get("error"):
            return result

        if not result.get("rows"):
            return {
                "error": True,
                "message": f"Table {schema}.{table} not found in SVV_TABLE_INFO",
            }

        stats = result.get("rows", [{}])[0]
        return {
            "schema": schema,
            "table": table,
            "row_count": stats.get("tbl_rows"),
            "size_mb": stats.get("size"),
            "sortkey": stats.get("sortkey1"),
            "distkey": stats.get("diststyle"),
            "unsorted": stats.get("unsorted"),
            "encoded": stats.get("encoded"),
        }

    # ------------------------------------------------------------------
    # Layer 2: Query
    # ------------------------------------------------------------------

    @friendly_errors("Redshift")
    async def run_query(
        self,
        host: str,
        port: int,
        database: str,
        user: str,
        password: str,
        query: str,
        max_results: int = 1000,
    ) -> dict:
        """
        Execute a SELECT query safely. Read-only: a single statement that begins
        with SELECT/WITH/SHOW/DESCRIBE/EXPLAIN and contains no write verbs.
        """
        from app.sql_safety import read_only_violation

        violation = read_only_violation(
            query, allowed_prefixes=("SELECT", "WITH", "SHOW", "DESCRIBE", "DESC", "EXPLAIN")
        )
        if violation:
            return {
                "error": True,
                "error_type": "invalid_param",
                "message": f"Security violation: {violation}",
            }

        upper_query = query.upper()
        # Add LIMIT guard if not already present
        if "LIMIT" not in upper_query:
            query = f"{query} LIMIT {max_results}"

        result = await self._execute_query(host, port, database, user, password, query)
        return result

    @friendly_errors("Redshift")
    async def preview_table(
        self,
        host: str,
        port: int,
        database: str,
        user: str,
        password: str,
        schema: str,
        table: str,
        limit: int = 100,
    ) -> dict:
        """
        Preview table data (SELECT * LIMIT N).

        Identifiers are validated against a strict allowlist because SQL
        drivers cannot bind identifier names as parameters — the only
        safe options are allowlist validation or dialect-aware quoting.
        See :mod:`app.sql_safety`.
        """
        from app.sql_safety import (
            InvalidIdentifierError,
            quote_identifier,
            validate_positive_int,
        )

        try:
            safe_schema = quote_identifier(schema)
            safe_table = quote_identifier(table)
            safe_limit = validate_positive_int(limit, field_name="limit", max_value=10_000)
        except InvalidIdentifierError as e:
            return {"error": True, "error_type": "invalid_identifier", "message": str(e)}

        query = f"SELECT * FROM {safe_schema}.{safe_table} LIMIT {safe_limit}"
        return await self.run_query(host, port, database, user, password, query, max_results=safe_limit)

    @friendly_errors("Redshift")
    async def explain_query(
        self,
        host: str,
        port: int,
        database: str,
        user: str,
        password: str,
        query: str,
    ) -> dict:
        """
        Show query execution plan via EXPLAIN.
        """
        # Only EXPLAIN read-only queries.
        from app.sql_safety import read_only_violation

        if read_only_violation(query):
            return {
                "error": True,
                "message": "Cannot EXPLAIN non-SELECT queries.",
            }

        explain_query = f"EXPLAIN {query}"
        result = await self._execute_query(host, port, database, user, password, explain_query)
        return result

    # ------------------------------------------------------------------
    # Layer 3: Audit
    # ------------------------------------------------------------------

    @friendly_errors("Redshift")
    async def audit_schema(
        self,
        host: str,
        port: int,
        database: str,
        user: str,
        password: str,
        schema: str = "public",
    ) -> dict:
        """
        Audit a schema: table count, total size, largest tables.
        """
        query = f"""
            SELECT schema, "table", tbl_rows, size
            FROM svv_table_info
            WHERE schema = '{schema}'
            ORDER BY size DESC
        """
        result = await self._execute_query(host, port, database, user, password, query)

        if result.get("error"):
            return result

        tables = result.get("rows", [])
        total_size_mb = sum(t.get("size", 0) for t in tables)
        total_rows = sum(t.get("tbl_rows", 0) for t in tables)

        largest_tables = tables[:5]

        return {
            "schema": schema,
            "table_count": len(tables),
            "total_size_mb": total_size_mb,
            "total_rows": total_rows,
            "largest_tables": [
                {
                    "table": t.get("table"),
                    "size_mb": t.get("size"),
                    "rows": t.get("tbl_rows"),
                }
                for t in largest_tables
            ],
        }

    @friendly_errors("Redshift")
    async def find_stale_tables(
        self,
        host: str,
        port: int,
        database: str,
        user: str,
        password: str,
        schema: str = "public",
        days_stale: int = 30,
    ) -> dict:
        """
        Find tables that haven't been modified in N days.
        Joins svv_table_info with stl_insert to detect recent write activity.
        Falls back to create_time when no insert history is available.
        """
        query = f"""
            SELECT t.schema,
                   t."table",
                   t.tbl_rows,
                   t.size,
                   COALESCE(MAX(i.endtime), t.create_time) AS last_activity
            FROM svv_table_info t
            LEFT JOIN (
                SELECT tbl, MAX(endtime) AS endtime
                FROM stl_insert
                GROUP BY tbl
            ) i ON t.table_id = i.tbl
            WHERE t.schema = '{schema}'
            GROUP BY t.schema, t."table", t.tbl_rows, t.size, t.table_id, t.create_time
            HAVING COALESCE(MAX(i.endtime), t.create_time)
                   < (CURRENT_TIMESTAMP - INTERVAL '{days_stale} days')
            ORDER BY last_activity ASC
        """
        result = await self._execute_query(host, port, database, user, password, query)

        if result.get("error"):
            return result

        stale_tables = result.get("rows", [])
        return {
            "schema": schema,
            "days_stale": days_stale,
            "stale_table_count": len(stale_tables),
            "stale_tables": [
                {
                    "table": t.get("table"),
                    "last_activity": str(t.get("last_activity")),
                    "rows": t.get("tbl_rows"),
                    "size_mb": t.get("size"),
                }
                for t in stale_tables
            ],
        }

    @friendly_errors("Redshift")
    async def check_table_health(
        self,
        host: str,
        port: int,
        database: str,
        user: str,
        password: str,
        schema: str,
        table: str,
    ) -> dict:
        """
        Check table health: sort key effectiveness, encoding, unsorted status.
        """
        stats_result = await self.get_table_stats(host, port, database, user, password, schema, table)

        if stats_result.get("error"):
            return stats_result

        stats = stats_result
        issues = []

        # Flag unsorted status
        if stats.get("unsorted") and stats.get("unsorted") > 0.1:
            issues.append(f"Table is {stats['unsorted'] * 100:.1f}% unsorted")

        # Recommend encoding if not set
        if not stats.get("encoded"):
            issues.append("Table does not use column encoding; consider enabling")

        health_score = max(0, 100 - len(issues) * 20)

        return {
            "schema": schema,
            "table": table,
            "row_count": stats.get("row_count"),
            "size_mb": stats.get("size_mb"),
            "sortkey": stats.get("sortkey"),
            "distkey": stats.get("distkey"),
            "unsorted_ratio": stats.get("unsorted"),
            "encoded": stats.get("encoded"),
            "issues": issues,
            "health_score": health_score,
        }

    @friendly_errors("Redshift")
    async def connection_health(self, host: str, port: int, database: str, user: str, password: str) -> dict:
        """
        Verify Redshift connection health with a simple query.
        """
        query = "SELECT 1 AS connection_ok"
        result = await self._execute_query(host, port, database, user, password, query)

        if result.get("error"):
            return result

        return {
            "status": "healthy",
            "message": "Redshift connection successful",
            "database": database,
            "host": host,
        }
