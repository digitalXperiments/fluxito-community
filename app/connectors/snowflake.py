"""
Snowflake Connector

Uses snowflake-connector-python library for database connections.

Auth: Credential-based (account, user, password, warehouse, database, schema, role).
All methods accept decrypted credentials.
Queries run in a thread pool to avoid blocking the event loop.

Layer 1 (Read): list_databases, list_schemas, list_tables, get_table_schema, list_warehouses

Layer 2 (Query): run_query, preview_table, explain_query

Layer 3 (Audit): audit_schema, find_stale_tables, check_clustering_health, get_warehouse_usage, connection_health
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from app.connectors._conn_pool import ConnectionPool, snowflake_pool
from app.connectors.errors import friendly_errors

logger = logging.getLogger(__name__)

# Shared thread pool for database queries
_db_thread_pool = ThreadPoolExecutor(max_workers=10, thread_name_prefix="snowflake")


class SnowflakeConnector:
    """Interfaces with Snowflake using credential-based connections."""

    def _build_connection(
        self,
        account: str,
        user: str,
        password: str,
        warehouse: str,
        database: str | None = None,
        schema: str | None = None,
        role: str | None = None,
    ):
        """Build a Snowflake connection object."""
        try:
            import snowflake.connector

            config = {
                "account": account,
                "user": user,
                "password": password,
                "warehouse": warehouse,
            }
            if database:
                config["database"] = database
            if schema:
                config["schema"] = schema
            if role:
                config["role"] = role

            return snowflake.connector.connect(**config)
        except Exception as e:
            logger.error(f"Snowflake connection error: {e}")
            return None

    async def _execute_query(
        self,
        account: str,
        user: str,
        password: str,
        warehouse: str,
        database: str | None,
        schema: str | None,
        query: str,
        fetch_all: bool = True,
        role: str | None = None,
    ) -> dict:
        """
        Execute a query in the thread pool.
        Returns results as list of dicts.
        """

        def _run():
            # Connection pool: reuse a warm connection if one is available
            # for this credential set. Builder is only invoked on miss.
            pool_key = ConnectionPool.key_for("sf", account, user, warehouse, database, schema, role)
            builder = lambda: self._build_connection(
                account, user, password, warehouse, database, schema, role
            )
            try:
                with snowflake_pool.checkout(pool_key, builder) as conn:
                    if not conn:
                        return {"error": True, "message": "Failed to connect to Snowflake"}
                    cursor = conn.cursor()
                    cursor.execute(query)
                    if fetch_all:
                        col_names = [desc[0] for desc in cursor.description] if cursor.description else []
                        rows = cursor.fetchall()
                        results = [dict(zip(col_names, row, strict=False)) for row in rows]
                        cursor.close()
                        return {"rows": results, "columns": col_names, "row_count": len(results)}
                    cursor.close()
                    return {"success": True}
            except Exception as e:
                logger.error(f"Snowflake query error: {e}")
                return {"error": True, "message": str(e)}

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_db_thread_pool, _run)

    # ------------------------------------------------------------------
    # Layer 1: Data Access
    # ------------------------------------------------------------------

    @friendly_errors("Snowflake")
    async def list_databases(
        self,
        account: str,
        user: str,
        password: str,
        warehouse: str,
        role: str | None = None,
    ) -> dict:
        """
        List all databases.
        SHOW DATABASES
        """
        query = "SHOW DATABASES"
        result = await self._execute_query(account, user, password, warehouse, None, None, query, role=role)

        if result.get("error"):
            return result

        databases = [row.get("name") for row in result.get("rows", [])]
        return {
            "databases": databases,
            "total": len(databases),
        }

    @friendly_errors("Snowflake")
    async def list_schemas(
        self,
        account: str,
        user: str,
        password: str,
        warehouse: str,
        database: str,
        role: str | None = None,
    ) -> dict:
        """
        List all schemas in a database.
        SHOW SCHEMAS IN DATABASE {database}
        """
        query = f"SHOW SCHEMAS IN DATABASE {database}"
        result = await self._execute_query(
            account, user, password, warehouse, database, None, query, role=role
        )

        if result.get("error"):
            return result

        schemas = [row.get("name") for row in result.get("rows", [])]
        return {
            "database": database,
            "schemas": schemas,
            "total": len(schemas),
        }

    @friendly_errors("Snowflake")
    async def list_tables(
        self,
        account: str,
        user: str,
        password: str,
        warehouse: str,
        database: str,
        schema: str = "PUBLIC",
        role: str | None = None,
    ) -> dict:
        """
        List all tables in a schema.
        SHOW TABLES IN SCHEMA {database}.{schema}
        """
        query = f"SHOW TABLES IN SCHEMA {database}.{schema}"
        result = await self._execute_query(
            account, user, password, warehouse, database, schema, query, role=role
        )

        if result.get("error"):
            return result

        tables = [row.get("name") for row in result.get("rows", [])]
        return {
            "database": database,
            "schema": schema,
            "tables": tables,
            "total": len(tables),
        }

    @friendly_errors("Snowflake")
    async def get_table_schema(
        self,
        account: str,
        user: str,
        password: str,
        warehouse: str,
        database: str,
        schema: str,
        table: str,
        role: str | None = None,
    ) -> dict:
        """
        Get table schema.
        DESCRIBE TABLE {database}.{schema}.{table}
        """
        query = f"DESCRIBE TABLE {database}.{schema}.{table}"
        result = await self._execute_query(
            account, user, password, warehouse, database, schema, query, role=role
        )

        if result.get("error"):
            return result

        columns = [
            {
                "name": row.get("name"),
                "type": row.get("type"),
                "nullable": row.get("null?") == "Y",
                "default": row.get("default"),
            }
            for row in result.get("rows", [])
        ]

        return {
            "database": database,
            "schema": schema,
            "table": table,
            "columns": columns,
            "column_count": len(columns),
        }

    @friendly_errors("Snowflake")
    async def list_warehouses(
        self,
        account: str,
        user: str,
        password: str,
        role: str | None = None,
    ) -> dict:
        """
        List all warehouses.
        SHOW WAREHOUSES
        """
        query = "SHOW WAREHOUSES"
        result = await self._execute_query(account, user, password, "", None, None, query, role=role)

        if result.get("error"):
            return result

        warehouses = [row.get("name") for row in result.get("rows", [])]
        return {
            "warehouses": warehouses,
            "total": len(warehouses),
        }

    # ------------------------------------------------------------------
    # Layer 2: Query
    # ------------------------------------------------------------------

    @friendly_errors("Snowflake")
    async def run_query(
        self,
        account: str,
        user: str,
        password: str,
        warehouse: str,
        database: str,
        schema: str,
        query: str,
        max_results: int = 1000,
        role: str | None = None,
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

        result = await self._execute_query(
            account, user, password, warehouse, database, schema, query, role=role
        )
        return result

    @friendly_errors("Snowflake")
    async def preview_table(
        self,
        account: str,
        user: str,
        password: str,
        warehouse: str,
        database: str,
        schema: str,
        table: str,
        limit: int = 100,
        role: str | None = None,
    ) -> dict:
        """
        Preview table data (SELECT * LIMIT N).

        Identifiers are validated against a strict allowlist because SQL
        drivers cannot bind identifier names as parameters. See
        :mod:`app.sql_safety`.
        """
        from app.sql_safety import (
            InvalidIdentifierError,
            quote_identifier,
            validate_positive_int,
        )

        try:
            safe_database = quote_identifier(database)
            safe_schema = quote_identifier(schema)
            safe_table = quote_identifier(table)
            safe_limit = validate_positive_int(limit, field_name="limit", max_value=10_000)
        except InvalidIdentifierError as e:
            return {"error": True, "error_type": "invalid_identifier", "message": str(e)}

        query = f"SELECT * FROM {safe_database}.{safe_schema}.{safe_table} LIMIT {safe_limit}"
        return await self.run_query(
            account, user, password, warehouse, database, schema, query, safe_limit, role
        )

    @friendly_errors("Snowflake")
    async def explain_query(
        self,
        account: str,
        user: str,
        password: str,
        warehouse: str,
        database: str,
        schema: str,
        query: str,
        role: str | None = None,
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
        result = await self._execute_query(
            account, user, password, warehouse, database, schema, explain_query, role=role
        )
        return result

    # ------------------------------------------------------------------
    # Layer 3: Audit
    # ------------------------------------------------------------------

    @friendly_errors("Snowflake")
    async def audit_schema(
        self,
        account: str,
        user: str,
        password: str,
        warehouse: str,
        database: str,
        schema: str = "PUBLIC",
        role: str | None = None,
    ) -> dict:
        """
        Audit a schema: table count, sizes from INFORMATION_SCHEMA.TABLE_STORAGE_METRICS.
        """
        query = f"""
            SELECT table_name, row_count, bytes
            FROM information_schema.table_storage_metrics
            WHERE table_catalog = '{database}' AND table_schema = '{schema}'
            ORDER BY bytes DESC
        """
        result = await self._execute_query(
            account, user, password, warehouse, database, schema, query, role=role
        )

        if result.get("error"):
            return result

        tables = result.get("rows", [])
        total_bytes = sum(t.get("bytes", 0) for t in tables)
        total_rows = sum(t.get("row_count", 0) for t in tables)
        total_gb = total_bytes / (1024**3) if total_bytes else 0

        largest_tables = tables[:5]

        return {
            "database": database,
            "schema": schema,
            "table_count": len(tables),
            "total_size_gb": round(total_gb, 2),
            "total_rows": total_rows,
            "largest_tables": [
                {
                    "table": t.get("table_name"),
                    "size_gb": round(t.get("bytes", 0) / (1024**3), 2),
                    "rows": t.get("row_count"),
                }
                for t in largest_tables
            ],
        }

    @friendly_errors("Snowflake")
    async def find_stale_tables(
        self,
        account: str,
        user: str,
        password: str,
        warehouse: str,
        database: str,
        schema: str = "PUBLIC",
        days_stale: int = 30,
        role: str | None = None,
    ) -> dict:
        """
        Find tables not modified in N days using INFORMATION_SCHEMA.TABLES.
        """
        query = f"""
            SELECT table_name, row_count, last_altered
            FROM information_schema.tables
            WHERE table_catalog = '{database}'
              AND table_schema = '{schema}'
              AND last_altered < DATEADD(day, -{days_stale}, CURRENT_TIMESTAMP)
            ORDER BY last_altered ASC
        """
        result = await self._execute_query(
            account, user, password, warehouse, database, schema, query, role=role
        )

        if result.get("error"):
            return result

        stale_tables = result.get("rows", [])
        return {
            "database": database,
            "schema": schema,
            "days_stale": days_stale,
            "stale_table_count": len(stale_tables),
            "stale_tables": [
                {
                    "table": t.get("table_name"),
                    "last_altered": str(t.get("last_altered")),
                    "rows": t.get("row_count"),
                }
                for t in stale_tables
            ],
        }

    @friendly_errors("Snowflake")
    async def check_clustering_health(
        self,
        account: str,
        user: str,
        password: str,
        warehouse: str,
        database: str,
        schema: str,
        table: str,
        role: str | None = None,
    ) -> dict:
        """
        Check table clustering health via SYSTEM$CLUSTERING_INFORMATION.
        Returns clustering depth and efficiency metrics.
        """
        query = f"""
            SELECT SYSTEM$CLUSTERING_INFORMATION('{database}.{schema}.{table}')
        """
        result = await self._execute_query(
            account, user, password, warehouse, database, schema, query, role=role
        )

        if result.get("error"):
            return result

        # Parse clustering info (typically returned as JSON string)
        clustering_info = result.get("rows", [{}])[0]

        return {
            "database": database,
            "schema": schema,
            "table": table,
            "clustering_info": clustering_info,
        }

    @friendly_errors("Snowflake")
    async def get_warehouse_usage(
        self,
        account: str,
        user: str,
        password: str,
        warehouse: str,
        days_back: int = 7,
        role: str | None = None,
    ) -> dict:
        """
        Get warehouse usage from ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY.
        """
        query = f"""
            SELECT warehouse_name, DATE_TRUNC('day', start_time) as date,
                   SUM(credits_used) as credits_used,
                   COUNT(*) as query_count
            FROM snowflake.account_usage.warehouse_metering_history
            WHERE warehouse_name = '{warehouse}'
              AND start_time >= DATEADD(day, -{days_back}, CURRENT_TIMESTAMP)
            GROUP BY warehouse_name, DATE_TRUNC('day', start_time)
            ORDER BY date DESC
        """
        result = await self._execute_query(account, user, password, warehouse, None, None, query, role=role)

        if result.get("error"):
            return result

        usage_data = result.get("rows", [])
        total_credits = sum(u.get("credits_used", 0) for u in usage_data)

        return {
            "warehouse": warehouse,
            "days_back": days_back,
            "total_credits": round(total_credits, 2),
            "usage_by_day": [
                {
                    "date": str(u.get("date")),
                    "credits_used": u.get("credits_used"),
                    "query_count": u.get("query_count"),
                }
                for u in usage_data
            ],
        }

    @friendly_errors("Snowflake")
    async def connection_health(
        self,
        account: str,
        user: str,
        password: str,
        warehouse: str,
        role: str | None = None,
    ) -> dict:
        """
        Verify Snowflake connection health.
        SELECT CURRENT_VERSION()
        """
        query = "SELECT CURRENT_VERSION()"
        result = await self._execute_query(account, user, password, warehouse, None, None, query, role=role)

        if result.get("error"):
            return result

        version = result.get("rows", [{}])[0].get("CURRENT_VERSION()")

        return {
            "status": "healthy",
            "message": "Snowflake connection successful",
            "account": account,
            "warehouse": warehouse,
            "version": version,
        }
