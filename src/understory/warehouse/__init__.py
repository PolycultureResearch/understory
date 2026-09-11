"""Warehouse adapters: DuckDB for development and fake tenants, BigQuery for
the first client. `make_warehouse` picks one from the tenant config.

The adapters are imported lazily so that importing this package never pulls
in `duckdb` or `google-cloud-bigquery` unless that adapter is used.
"""

from __future__ import annotations

from understory.protocols import Warehouse
from understory.tenant import BigQueryConfig, DuckDBConfig


def make_warehouse(
    config: DuckDBConfig | BigQueryConfig,
    *,
    schemas: list[str] | None = None,
    timeout_s: int = 60,
) -> Warehouse:
    """Build the adapter for `config.type`.

    `schemas` is the tenant's `sql.schemas` scope, used by `relations()`.
    `timeout_s` bounds the adapter's helper queries; `run` takes its own.
    """
    if config.type == "duckdb":
        from understory.warehouse.duckdb import DuckDBWarehouse

        return DuckDBWarehouse(config, schemas=schemas, timeout_s=timeout_s)
    if config.type == "bigquery":
        from understory.warehouse.bigquery import BigQueryWarehouse

        return BigQueryWarehouse(config, schemas=schemas, timeout_s=timeout_s)
    raise ValueError(f"unknown warehouse type: {config.type!r}")


__all__ = ["make_warehouse"]
