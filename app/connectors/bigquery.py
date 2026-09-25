"""
BigQuery Connector

Uses google-cloud-bigquery with service account credentials stored per-user.
Provides read operations for running queries and listing datasets/tables.
"""

import asyncio
import datetime as _dt
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal as _Decimal

from app.config import settings
from app.connectors.errors import friendly_errors

# Dedicated pool for BigQuery sync SDK calls. Isolates long-running
# warehouse queries (up to 60s) from the default asyncio pool and from
# the Google SDK pool used by GA4/GTM/Ads.
_bq_thread_pool = ThreadPoolExecutor(max_workers=10, thread_name_prefix="bigquery")


async def _run_in_pool(fn):
    return await asyncio.get_event_loop().run_in_executor(_bq_thread_pool, fn)


def _decrypt(encrypted: str) -> str:
    """Decrypt a service account JSON string using the app's Fernet key."""
    from cryptography.fernet import Fernet

    f = Fernet(settings.TOKEN_ENCRYPTION_KEY.encode())
    return f.decrypt(encrypted.encode()).decode()


class BigQueryConnector:
    """
    BigQuery connector that authenticates via a user-supplied service account.
    Unlike Google OAuth connectors this does NOT extend BaseConnector —
    it reads the encrypted service-account JSON from the bq_connections table.
    """

    def _build_client(self, service_account_json: str):
        """Build a BigQuery client from raw service-account JSON string."""
        import json

        from google.cloud import bigquery
        from google.oauth2 import service_account

        info = json.loads(service_account_json)
        creds = service_account.Credentials.from_service_account_info(
            info,
            scopes=["https://www.googleapis.com/auth/bigquery"],
        )
        return bigquery.Client(project=info.get("project_id"), credentials=creds)

    # ------------------------------------------------------------------
    # Layer 1: Data Access
    # ------------------------------------------------------------------

    @friendly_errors("BigQuery")
    async def list_datasets(self, service_account_encrypted: str, project_id: str) -> dict:
        """Lists all datasets in the BigQuery project."""
        sa_json = _decrypt(service_account_encrypted)

        def _fetch():
            client = self._build_client(sa_json)
            datasets = list(client.list_datasets())
            return {
                "project_id": project_id,
                "datasets": [
                    {
                        "dataset_id": ds.dataset_id,
                        "full_id": ds.full_dataset_id,
                    }
                    for ds in datasets
                ],
            }

        return await _run_in_pool(_fetch)

    @friendly_errors("BigQuery")
    async def list_tables(self, service_account_encrypted: str, project_id: str, dataset_id: str) -> dict:
        """Lists all tables in a BigQuery dataset."""
        sa_json = _decrypt(service_account_encrypted)

        def _fetch():
            client = self._build_client(sa_json)
            tables = list(client.list_tables(f"{project_id}.{dataset_id}"))
            return {
                "dataset_id": dataset_id,
                "tables": [
                    {
                        "table_id": t.table_id,
                        "table_type": t.table_type,
                        "full_id": t.full_table_id,
                    }
                    for t in tables
                ],
            }

        return await _run_in_pool(_fetch)

    @friendly_errors("BigQuery")
    async def get_table_schema(
        self, service_account_encrypted: str, project_id: str, dataset_id: str, table_id: str
    ) -> dict:
        """Returns the schema of a BigQuery table."""
        sa_json = _decrypt(service_account_encrypted)

        def _fetch():
            client = self._build_client(sa_json)
            table = client.get_table(f"{project_id}.{dataset_id}.{table_id}")
            modified_time = None
            if table.modified is not None:
                modified_time = table.modified.isoformat()

            return {
                "table_id": table_id,
                "dataset_id": dataset_id,
                "num_rows": table.num_rows,
                "num_bytes": table.num_bytes,
                "modified_time": modified_time,
                "created_time": table.created.isoformat() if table.created is not None else None,
                "table_type": table.table_type,
                "schema": [
                    {
                        "name": field.name,
                        "field_type": field.field_type,
                        "mode": field.mode,
                        "description": field.description,
                    }
                    for field in table.schema
                ],
            }

        return await _run_in_pool(_fetch)

    @friendly_errors("BigQuery")
    async def run_query(
        self,
        service_account_encrypted: str,
        project_id: str,
        query: str,
        max_results: int = 1000,
    ) -> dict:
        """Runs a BigQuery SQL query and returns results (read-only)."""
        from app.sql_safety import read_only_violation

        violation = read_only_violation(query)
        if violation:
            return {
                "error": True,
                "error_type": "invalid_param",
                "message": f"Security violation: {violation}",
            }

        sa_json = _decrypt(service_account_encrypted)

        def _fetch():
            client = self._build_client(sa_json)

            from google.cloud.bigquery import QueryJobConfig

            job_config = QueryJobConfig(maximum_bytes_billed=10 * 1024**3)  # 10 GB safety cap

            query_job = client.query(query, job_config=job_config)
            # 110s lets the job finish before TIMEOUT_QUERY (120s) fires
            results = query_job.result(max_results=max_results, timeout=110)

            schema = [{"name": f.name, "type": f.field_type} for f in results.schema]

            def _safe(v):
                if isinstance(v, _Decimal):
                    return float(v)
                if isinstance(v, (_dt.date, _dt.datetime)):
                    return v.isoformat()
                return v

            rows = [{k: _safe(v) for k, v in dict(row).items()} for row in results]

            return {
                "project_id": project_id,
                "schema": schema,
                "rows": rows,
                "total_rows": results.total_rows,
                "bytes_processed": query_job.total_bytes_processed,
            }

        return await _run_in_pool(_fetch)

    @friendly_errors("BigQuery")
    async def dry_run(
        self,
        service_account_encrypted: str,
        project_id: str,
        query: str,
    ) -> dict:
        """Estimates bytes that would be processed by a query without executing it."""
        from app.sql_safety import read_only_violation

        violation = read_only_violation(query)
        if violation:
            return {
                "error": True,
                "error_type": "invalid_param",
                "message": f"Security violation: {violation}",
            }

        sa_json = _decrypt(service_account_encrypted)

        def _run():
            client = self._build_client(sa_json)
            from google.cloud.bigquery import QueryJobConfig

            job_config = QueryJobConfig(dry_run=True, use_query_cache=False)
            job = client.query(query, job_config=job_config)
            bytes_est = job.total_bytes_processed or 0
            gb_est = round(bytes_est / (1024**3), 4)
            # BQ on-demand pricing: $6.25 per TiB (as of 2025)
            cost_usd_est = round(gb_est / 1024 * 6.25, 6)
            return {
                "project_id": project_id,
                "estimated_bytes": bytes_est,
                "estimated_gb": gb_est,
                "estimated_cost_usd": cost_usd_est,
                "note": "Cost estimate based on BQ on-demand pricing ($6.25/TiB). Caching may reduce actual cost.",
            }

        return await _run_in_pool(_run)
