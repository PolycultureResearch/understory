"""The two seams the design doc names: SemanticLayer and Warehouse.

A SemanticLayer knows the catalog and can compile a MetricSpec into SQL. It
never executes anything. A Warehouse executes SQL and answers freshness. The
server composes them so the governed path and `run_sql` share one connection,
one row cap, and one provenance format.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from understory.types import Catalog, CompiledQuery, MetricSpec, Result


class CompileError(Exception):
    """The semantic layer could not express this spec. Message is user-facing."""


class QueryError(Exception):
    """The warehouse rejected or failed the query. Message is user-facing."""


@runtime_checkable
class SemanticLayer(Protocol):
    name: str
    """`metricflow_local` or `dbt_cloud`, for provenance."""

    dialect: str
    """sqlglot dialect name of the SQL `compile` returns, e.g. `duckdb`, `bigquery`."""

    def catalog(self) -> Catalog: ...

    def compile(self, spec: MetricSpec) -> CompiledQuery:
        """Return SQL for the spec. Raise CompileError when it cannot."""
        ...


@runtime_checkable
class Warehouse(Protocol):
    name: str
    """`duckdb` or `bigquery`, for provenance."""

    dialect: str

    def run(self, sql: str, *, timeout_s: int, row_cap: int) -> Result:
        """Execute read-only SQL. Return at most `row_cap` rows and mark truncation."""
        ...

    def latest_date(self, relation: str, column: str) -> date | None:
        """MAX(column) on the relation, or None when the relation is empty."""
        ...

    def dimension_values(self, relation: str, column: str, query: str, limit: int) -> Result:
        """Distinct values of `column` matching `query` (case-insensitive contains), with counts."""
        ...

    def relations(self) -> list[str]:
        """Relations the service account can see within the tenant's schema scope."""
        ...
