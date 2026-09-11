"""SemanticLayer over the dbt Cloud Semantic Layer API.

Uses the sync client from `dbt-sl-sdk`, which is not a declared dependency: it
is imported lazily so tenants on MetricFlowLocal never need it. The SDK's
`compile_sql` returns the SQL dbt Cloud would run, and that SQL goes to the
Warehouse adapter exactly as the local path does.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from understory.protocols import CompileError
from understory.semantic.where import time_group_by, to_mf_where
from understory.tenant import DbtCloudConfig
from understory.types import Catalog, CompiledQuery, MetricSpec

DEFAULT_DIALECT = "bigquery"
"""dbt Cloud does not tell us the warehouse dialect. Until DbtCloudConfig grows
a `dialect` field, this default is used unless one is passed to the constructor."""


def _short_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _make_client(config: DbtCloudConfig) -> Any:
    try:
        from dbtsl import SemanticLayerClient
    except ImportError as e:
        raise CompileError(
            "The dbt Cloud semantic layer needs the `dbtsl` package. "
            "Install it with: pip install 'dbt-sl-sdk[sync]'"
        ) from e
    return SemanticLayerClient(
        environment_id=config.environment_id,
        auth_token=config.token,
        host=config.host,
    )


def compile_kwargs(spec: MetricSpec, catalog_time_dim: str | None = None) -> dict[str, Any]:
    """Arguments for `SemanticLayerClient.compile_sql`. Pure, so it is unit-testable."""
    if not spec.metrics:
        raise CompileError("A query needs at least one metric.")
    kwargs: dict[str, Any] = {"metrics": list(spec.metrics)}
    group_by = time_group_by(spec, catalog_time_dim)
    if group_by:
        kwargs["group_by"] = group_by
    where = to_mf_where(spec.where)
    if where:
        kwargs["where"] = [where]
    if spec.limit is not None:
        kwargs["limit"] = spec.limit
    return kwargs


class DbtCloud:
    """Compile MetricSpecs through dbt Cloud. See the module docstring."""

    name = "dbt_cloud"

    def __init__(
        self,
        config: DbtCloudConfig,
        manifest_path: Path,
        *,
        client: Any | None = None,
        dialect: str | None = None,
    ) -> None:
        self.config = config
        self.manifest_path = Path(manifest_path)
        self.dialect = dialect or getattr(config, "dialect", None) or DEFAULT_DIALECT
        self._client = client
        self._catalog: Catalog | None = None

    def catalog(self) -> Catalog:
        if self._catalog is None:
            from understory.catalog.manifest import load_catalog

            self._catalog = load_catalog(self.manifest_path)
        return self._catalog

    def _time_dim_for(self, spec: MetricSpec) -> str | None:
        # Best effort: the catalog only sharpens grain handling, so a missing
        # or invalid manifest (CatalogError is a ValueError) must not block a compile.
        try:
            catalog = self.catalog()
        except (ImportError, OSError, ValueError):
            return None
        for name in spec.metrics:
            info = catalog.metric(name)
            if info and info.time_dimension:
                return info.time_dimension
        return None

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = _make_client(self.config)
        return self._client

    def compile(self, spec: MetricSpec) -> CompiledQuery:
        """Compile through the API. Start and end dates become metric_time filters.

        The SDK has no start/end arguments, so the window is expressed as extra
        `TimeDimension('metric_time')` clauses in `where`, which is how dbt's own
        tooling does it.
        """
        kwargs = compile_kwargs(spec, self._time_dim_for(spec))
        window: list[str] = []
        if spec.time.start is not None:
            window.append(f"{{{{ TimeDimension('metric_time', 'day') }}}} >= '{spec.time.start}'")
        if spec.time.end is not None:
            window.append(f"{{{{ TimeDimension('metric_time', 'day') }}}} <= '{spec.time.end}'")
        if window:
            kwargs["where"] = kwargs.get("where", []) + window

        client = self.client
        try:
            with client.session():
                sql = client.compile_sql(**kwargs)
        except CompileError:
            raise
        except Exception as e:  # noqa: BLE001 - every SDK error is a compile failure to us
            raise CompileError(f"dbt Cloud could not compile the query: {e}") from e

        sql = (sql or "").strip()
        if not sql:
            raise CompileError("dbt Cloud returned no SQL for the query.")
        return CompiledQuery(
            sql=sql,
            spec_hash=spec.hash(),
            sql_hash=_short_hash(sql.encode()),
            dialect=self.dialect,
            metrics=list(spec.metrics),
            cache_hit=False,
        )
