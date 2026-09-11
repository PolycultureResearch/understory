"""DuckDB warehouse adapter.

Used for development, tests, and the fake_companies tenants. One DuckDB
connection is opened per warehouse instance. DuckDB connections are not safe
to share across threads, so every call takes `conn.cursor()`, which creates a
lightweight duplicate connection bound to the same database, and closes it
when done.

Timeouts
--------
DuckDB has no statement timeout setting. The adapter starts a `threading.Timer`
before executing; when it fires it calls `cursor.interrupt()` on the cursor
running the query, which makes DuckDB abort with `InterruptException`. That is
translated to `QueryError`. The timer covers both execution and the fetch of
`row_cap + 1` rows. If the timer fires after the query finished the interrupt
lands on a cursor that is about to be closed, so it has no effect.

Relations
---------
Relation strings are interpolated into SQL as given. Anything DuckDB accepts
works: `main_marts.fct_orders`, or the fully quoted form the semantic manifest
uses, `"alpenglow"."main_marts"."fct_orders"`, where the database name is the
file stem. Column names are double-quoted unless the caller already quoted
them.
"""

from __future__ import annotations

import threading
import time
from datetime import date
from typing import Any

import duckdb

from understory.protocols import QueryError
from understory.tenant import DuckDBConfig
from understory.types import Column, Result
from understory.warehouse._values import as_date, json_safe_row

SYSTEM_SCHEMAS = frozenset({"information_schema", "pg_catalog"})


def quote_ident(name: str) -> str:
    """Double-quote an identifier unless it is already quoted."""
    if name.startswith('"') and name.endswith('"'):
        return name
    return '"' + name.replace('"', '""') + '"'


class DuckDBWarehouse:
    name = "duckdb"
    dialect = "duckdb"

    def __init__(
        self,
        config: DuckDBConfig,
        *,
        schemas: list[str] | None = None,
        timeout_s: int = 60,
    ) -> None:
        """
        `schemas` is the tenant's `sql.schemas` scope; `relations()` lists
        tables within it. Empty or None means every non-system schema.
        `timeout_s` applies to the helper queries (`latest_date`,
        `dimension_values`, `relations`), which have no per-call timeout.
        """
        self.config = config
        self.schemas = [s.lower() for s in (schemas or [])]
        self.timeout_s = timeout_s
        try:
            self._conn = duckdb.connect(config.path, read_only=config.read_only)
        except duckdb.Error as e:
            raise QueryError(f"could not open DuckDB database {config.path}: {e}") from e
        self._lock = threading.Lock()

    def close(self) -> None:
        self._conn.close()

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
        sql = f"SELECT MAX({quote_ident(column)}) FROM {relation}"
        _, rows, _, _ = self._execute(sql, params=None, timeout_s=self.timeout_s, row_cap=1)
        if not rows:
            return None
        return as_date(rows[0][0])

    def dimension_values(self, relation: str, column: str, query: str, limit: int) -> Result:
        col = quote_ident(column)
        # contains() rather than LIKE so the query text carries no wildcards.
        sql = (
            f"SELECT {col} AS value, COUNT(*) AS count "
            f"FROM {relation} "
            f"WHERE {col} IS NOT NULL "
            f"AND contains(lower(CAST({col} AS VARCHAR)), lower(?)) "
            f"GROUP BY 1 ORDER BY 2 DESC, 1 ASC"
        )
        columns, rows, truncated, elapsed_ms = self._execute(
            sql, params=[query], timeout_s=self.timeout_s, row_cap=limit
        )
        return Result(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            elapsed_ms=elapsed_ms,
        )

    def relations(self) -> list[str]:
        sql = (
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_catalog = current_database() ORDER BY 1, 2"
        )
        _, rows, _, _ = self._execute(sql, params=None, timeout_s=self.timeout_s, row_cap=100_000)
        out: list[str] = []
        for schema, table in rows:
            s = schema.lower()
            if s in SYSTEM_SCHEMAS:
                continue
            if self.schemas and s not in self.schemas:
                continue
            out.append(f"{schema}.{table}")
        return out

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _cursor(self) -> duckdb.DuckDBPyConnection:
        with self._lock:
            return self._conn.cursor()

    def _execute(
        self,
        sql: str,
        *,
        params: list[Any] | None,
        timeout_s: int,
        row_cap: int,
    ) -> tuple[list[Column], list[list[Any]], bool, int]:
        """Run one statement on a fresh cursor with a timeout. Returns columns,
        JSON-safe rows (at most `row_cap`), whether more rows existed, and the
        elapsed wall time in milliseconds."""
        if row_cap < 0:
            raise ValueError("row_cap must be non-negative")
        cur = self._cursor()
        timed_out = threading.Event()

        def interrupt() -> None:
            timed_out.set()
            try:
                cur.interrupt()
            except duckdb.Error:
                pass

        timer = threading.Timer(max(timeout_s, 0), interrupt)
        timer.daemon = True
        started = time.perf_counter()
        try:
            timer.start()
            cur.execute(sql, params or [])
            raw = cur.fetchmany(row_cap + 1)
            description = cur.description or []
        except duckdb.InterruptException as e:
            raise QueryError(f"query exceeded the {timeout_s}s timeout") from e
        except duckdb.Error as e:
            if timed_out.is_set():
                raise QueryError(f"query exceeded the {timeout_s}s timeout") from e
            raise QueryError(str(e)) from e
        finally:
            timer.cancel()
            cur.close()
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        columns = [Column(name=d[0], type=str(d[1])) for d in description]
        truncated = len(raw) > row_cap
        rows = [json_safe_row(r) for r in raw[:row_cap]]
        return columns, rows, truncated, elapsed_ms
