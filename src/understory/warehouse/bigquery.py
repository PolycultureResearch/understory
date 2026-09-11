"""BigQuery warehouse adapter.

`google-cloud-bigquery` is an optional extra, so it is imported inside the
class rather than at module level. Importing this module without the package
installed works; constructing `BigQueryWarehouse` does not.

Timeouts
--------
Two mechanisms, both set per call. `QueryJobConfig.job_timeout_ms` tells
BigQuery itself to abandon the job after the deadline. `job.result(timeout=)`
bounds the client-side wait; when it expires the adapter cancels the job and
raises `QueryError`. The client-side bound is what the unit tests exercise.

Cost control
------------
`maximum_bytes_billed` is read from the tenant config with `getattr` and a
None default, because `BigQueryConfig` does not declare it yet. Add the field
to `tenant.py` and it takes effect with no change here.

Relations
---------
BigQuery quotes identifiers with backticks. Relation strings that arrive with
double quotes (the dbt manifest form for DuckDB tenants) are converted by
swapping the quote character; backticked or bare `project.dataset.table`
strings are used as given.
"""

from __future__ import annotations

import time
from datetime import date
from typing import Any

from understory.protocols import QueryError
from understory.tenant import BigQueryConfig
from understory.types import Column, Result
from understory.warehouse._values import as_date, json_safe_row


def bq_relation(relation: str) -> str:
    """Convert a double-quoted relation to BigQuery backtick quoting."""
    if '"' in relation:
        return relation.replace('"', "`")
    return relation


def bq_ident(name: str) -> str:
    if name.startswith("`") and name.endswith("`"):
        return name
    return "`" + name.replace("`", "\\`") + "`"


class BigQueryWarehouse:
    name = "bigquery"
    dialect = "bigquery"

    def __init__(
        self,
        config: BigQueryConfig,
        *,
        schemas: list[str] | None = None,
        timeout_s: int = 60,
        client: Any | None = None,
    ) -> None:
        """
        `schemas` is the tenant's `sql.schemas` scope (BigQuery datasets);
        `relations()` lists tables within it. `timeout_s` applies to the helper
        queries. `client` lets tests inject a fake `bigquery.Client`.
        """
        from google.cloud import bigquery

        self._bq = bigquery
        self.config = config
        self.schemas = [s.lower() for s in (schemas or [])]
        self.timeout_s = timeout_s
        self.maximum_bytes_billed: int | None = getattr(config, "maximum_bytes_billed", None)
        self._client = client if client is not None else self._make_client(config)

    def _make_client(self, config: BigQueryConfig) -> Any:
        credentials = None
        if config.credentials_file:
            from google.oauth2 import service_account

            credentials = service_account.Credentials.from_service_account_file(
                config.credentials_file
            )
        return self._bq.Client(
            project=config.project, location=config.location, credentials=credentials
        )

    # ------------------------------------------------------------------ #
    # Warehouse protocol
    # ------------------------------------------------------------------ #

    def run(self, sql: str, *, timeout_s: int, row_cap: int) -> Result:
        columns, rows, truncated, elapsed_ms = self._execute(
            sql, params=None, timeout_s=timeout_s, row_cap=row_cap
        )
        return Result(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            elapsed_ms=elapsed_ms,
        )

    def latest_date(self, relation: str, column: str) -> date | None:
        sql = f"SELECT MAX({bq_ident(column)}) AS latest FROM {bq_relation(relation)}"
        _, rows, _, _ = self._execute(sql, params=None, timeout_s=self.timeout_s, row_cap=1)
        if not rows:
            return None
        return as_date(rows[0][0])

    def dimension_values(self, relation: str, column: str, query: str, limit: int) -> Result:
        col = bq_ident(column)
        # STRPOS rather than LIKE so the query text carries no wildcards.
        sql = (
            f"SELECT {col} AS value, COUNT(*) AS count "
            f"FROM {bq_relation(relation)} "
            f"WHERE {col} IS NOT NULL "
            f"AND STRPOS(LOWER(CAST({col} AS STRING)), LOWER(@q)) > 0 "
            f"GROUP BY 1 ORDER BY 2 DESC, 1 ASC"
        )
        params = [self._bq.ScalarQueryParameter("q", "STRING", query)]
        columns, rows, truncated, elapsed_ms = self._execute(
            sql, params=params, timeout_s=self.timeout_s, row_cap=limit
        )
        return Result(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            elapsed_ms=elapsed_ms,
        )

    def relations(self) -> list[str]:
        out: list[str] = []
        try:
            for dataset in self._client.list_datasets(project=self.config.project):
                ds = dataset.dataset_id
                if self.schemas and ds.lower() not in self.schemas:
                    continue
                for table in self._client.list_tables(dataset.reference):
                    out.append(f"{ds}.{table.table_id}")
        except Exception as e:  # google.api_core exceptions, imported lazily
            raise QueryError(f"could not list BigQuery tables: {e}") from e
        return sorted(out)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _job_config(self, timeout_s: int, params: list[Any] | None) -> Any:
        cfg = self._bq.QueryJobConfig(use_legacy_sql=False)
        cfg.job_timeout_ms = int(timeout_s * 1000)
        if self.maximum_bytes_billed is not None:
            cfg.maximum_bytes_billed = int(self.maximum_bytes_billed)
        if params:
            cfg.query_parameters = params
        return cfg

    def _execute(
        self,
        sql: str,
        *,
        params: list[Any] | None,
        timeout_s: int,
        row_cap: int,
    ) -> tuple[list[Column], list[list[Any]], bool, int]:
        from concurrent.futures import TimeoutError as FutureTimeout

        from google.api_core import exceptions as gexc

        if row_cap < 0:
            raise ValueError("row_cap must be non-negative")
        job_config = self._job_config(timeout_s, params)
        started = time.perf_counter()
        job = None
        try:
            job = self._client.query(
                sql,
                job_config=job_config,
                location=self.config.location,
                timeout=timeout_s,
            )
            iterator = job.result(timeout=timeout_s, max_results=row_cap + 1)
            raw = [list(row.values()) for row in iterator]
            schema = getattr(iterator, "schema", None) or getattr(job, "schema", None) or []
        except FutureTimeout as e:
            self._cancel(job)
            raise QueryError(f"query exceeded the {timeout_s}s timeout") from e
        except gexc.GoogleAPICallError as e:
            raise QueryError(e.message or str(e)) from e
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        columns = [Column(name=f.name, type=str(f.field_type)) for f in schema]
        truncated = len(raw) > row_cap
        rows = [json_safe_row(r) for r in raw[:row_cap]]
        return columns, rows, truncated, elapsed_ms

    @staticmethod
    def _cancel(job: Any) -> None:
        if job is None:
            return
        try:
            job.cancel()
        except Exception:
            pass
