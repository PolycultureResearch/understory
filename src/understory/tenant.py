"""Tenant configuration: one directory per client.

    <client dbt repo>/understory/     (or tenants/<name>/ for the fixtures)
      tenant.yml              this file's schema
      context.md              hand-written business context for get_context
      traps.yml               traps registry (understory.traps)
      semantic_manifest.json  from `dbt parse`; optional, see manifest_path
      golden/questions.yml    trap set (understory.harness)
      golden/realistic.yml    realistic set (understory.harness)

Environment variables are expanded in string values with `${VAR}` or
`${VAR:-default}` so the same tenant.yml works locally and in a container.

Relative paths to things the tenant reads (the DuckDB file, the dbt project,
profiles, credentials, the manifest, the compile cache) resolve against the
tenant directory, so a tenant inside a client repo can say
`dbt_project_dir: ..` and work wherever the repo is checked out or mounted.
Log prefixes are outputs and stay relative to the working directory: a
mounted client repo is no place for the log.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

_ENV = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


def expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    return value


class DuckDBConfig(BaseModel):
    type: Literal["duckdb"] = "duckdb"
    path: str
    read_only: bool = True


class BigQueryConfig(BaseModel):
    type: Literal["bigquery"] = "bigquery"
    project: str
    location: str = "US"
    credentials_file: str | None = None
    """Service account JSON. Omitted means application default credentials."""


class MetricFlowLocalConfig(BaseModel):
    type: Literal["metricflow_local"] = "metricflow_local"
    dbt_project_dir: str
    profiles_dir: str | None = None
    """Defaults to dbt_project_dir."""
    mf_bin: str = "mf"
    env: dict[str, str] = Field(default_factory=dict)
    """Extra environment for the mf subprocess, e.g. FAKE_DB."""
    cache_dir: str | None = None
    """Compiled SQL cache keyed by spec hash. Defaults to .cache/mf beside the manifest.
    A cache that cannot be written (a read-only mount) is skipped, not fatal."""


class DbtCloudConfig(BaseModel):
    type: Literal["dbt_cloud"] = "dbt_cloud"
    host: str = "semantic-layer.cloud.getdbt.com"
    environment_id: int
    token: str


class Limits(BaseModel):
    row_cap: int = 200
    timeout_s: int = 60
    max_clarifications: int = 3
    dimension_values_limit: int = 25


class Checks(BaseModel):
    """Descriptive checks on the data behind a resolved query.

    Each one adds a disclosure and never changes a number. `volume` flags a
    period inside the window whose row count fell far below the same period in
    the weeks around it, which is what an incomplete load looks like. See
    `understory.server.volume` for the rule and its two thresholds.
    """

    volume: bool = True
    volume_low_ratio: float = 0.5
    """A period under this share of its baseline is low."""
    volume_min_rows: int = 20
    """Baselines below this are too sparse to judge."""
    spec_disclosures: bool = False
    """Disclose a non-preferred trap candidate the spec names even when the
    trap's phrase is not in the question the chatbot sent. Off until the
    off/on eval says what it buys and what it costs. See `understory.traps.match`."""


class SqlScope(BaseModel):
    """What run_sql may touch. Schemas are matched case-insensitively."""

    enabled: bool = True
    schemas: list[str] = Field(default_factory=list)
    """Empty means every schema the catalog references."""
    deny_relations: list[str] = Field(default_factory=list)
    include_sql_in_governed_provenance: bool = True


class LogConfig(BaseModel):
    events_prefix: str
    """Directory or gs:// prefix for the events family."""
    text_prefix: str
    """Directory or gs:// prefix for the text family."""
    gaps_prefix: str | None = None
    """Directory or gs:// prefix for the gaps family. Defaults to `gaps` beside `events`."""
    user_hash_secret: str = "${UNDERSTORY_USER_HASH_SECRET:-dev-secret-change-me}"
    text_retention_days: int = 90
    enabled: bool = True

    def resolved_gaps_prefix(self) -> str:
        if self.gaps_prefix:
            return self.gaps_prefix
        base = self.events_prefix.rstrip("/")
        head, sep, tail = base.rpartition("/")
        return f"{head}{sep}gaps" if tail == "events" else f"{base}-gaps"


class AuthConfig(BaseModel):
    """How a chatbot authenticates to Understory. Separate from warehouse identity.

    `none` is for local development and stdio. `static` accepts a small set of
    bearer tokens, one per person or per connector, and uses the token's label
    as the subject that gets hashed into the log. IdP-backed OAuth is a
    roadmap item; the seam is the MCP SDK's TokenVerifier.
    """

    mode: Literal["none", "static"] = "none"
    tokens: dict[str, str] = Field(default_factory=dict)
    """label -> token. Values usually come from env, e.g. "${UNDERSTORY_TOKEN_DEVON}"."""
    issuer_url: str = "http://localhost:8000"
    resource_url: str | None = None


class TenantConfig(BaseModel):
    name: str
    display_name: str
    vertical: str | None = None
    warehouse: DuckDBConfig | BigQueryConfig = Field(discriminator="type")
    semantic_layer: MetricFlowLocalConfig | DbtCloudConfig = Field(discriminator="type")
    limits: Limits = Field(default_factory=Limits)
    sql: SqlScope = Field(default_factory=SqlScope)
    log: LogConfig
    auth: AuthConfig = Field(default_factory=AuthConfig)
    checks: Checks = Field(default_factory=Checks)
    instructions: str | None = None
    """Short text for the MCP server `instructions` field."""
    manifest: str | None = None
    """Path to semantic_manifest.json when it is not in the tenant directory."""

    # Filled in by load()
    root: Path = Field(default=Path("."), exclude=True)

    @property
    def manifest_path(self) -> Path:
        """The first of: `manifest`, the tenant directory's copy, the dbt project's target/.

        The last one is what `dbt parse` leaves behind, so a tenant that sits
        in the dbt repo needs no copy step. With none present the tenant
        directory's path is returned, and loading it names the missing file.
        """
        if self.manifest:
            return self.root / self.manifest
        local = self.root / "semantic_manifest.json"
        if local.exists():
            return local
        if isinstance(self.semantic_layer, MetricFlowLocalConfig):
            built = Path(self.semantic_layer.dbt_project_dir) / "target" / "semantic_manifest.json"
            if built.exists():
                return built
        return local

    @property
    def context_path(self) -> Path:
        return self.root / "context.md"

    @property
    def traps_path(self) -> Path:
        return self.root / "traps.yml"

    @property
    def golden_path(self) -> Path:
        return self.root / "golden" / "questions.yml"

    @property
    def realistic_path(self) -> Path:
        return self.root / "golden" / "realistic.yml"

    @property
    def multiturn_path(self) -> Path:
        return self.root / "golden" / "multiturn.yml"

    def golden_set_path(self, name: str) -> Path:
        """`trap`, `realistic` or `multiturn`."""
        if name == "trap":
            return self.golden_path
        if name == "realistic":
            return self.realistic_path
        if name == "multiturn":
            return self.multiturn_path
        raise ValueError(
            f"unknown golden set {name!r}; expected 'trap', 'realistic' or 'multiturn'"
        )


def _anchor(value: str | None, root: Path) -> str | None:
    """`value` made absolute against `root` when it is a relative filesystem path."""
    if not value or value == ":memory:" or "://" in value or value.startswith("md:"):
        return value  # in-memory DuckDB, a URL, a MotherDuck database
    p = Path(value).expanduser()
    return value if p.is_absolute() else str((root / p).resolve())


def _anchor_paths(cfg: TenantConfig) -> None:
    root = cfg.root
    wh, sl = cfg.warehouse, cfg.semantic_layer
    if isinstance(wh, DuckDBConfig):
        wh.path = _anchor(wh.path, root)
    else:
        wh.credentials_file = _anchor(wh.credentials_file, root)
    if isinstance(sl, MetricFlowLocalConfig):
        sl.dbt_project_dir = _anchor(sl.dbt_project_dir, root)
        sl.profiles_dir = _anchor(sl.profiles_dir, root)
        sl.cache_dir = _anchor(sl.cache_dir, root)
    cfg.manifest = _anchor(cfg.manifest, root)


def resolve_tenant(arg: str | Path) -> Path:
    """The tenant.yml for a directory, a tenant.yml, or a fixture name under tenants_dir()."""
    p = Path(arg).expanduser()
    if p.is_dir():
        p = p / "tenant.yml"
    if p.is_file():
        return p
    named = tenants_dir() / str(arg) / "tenant.yml"
    if os.sep not in str(arg) and named.is_file():
        return named
    raise FileNotFoundError(
        f"no tenant at {arg!r}: expected a directory holding tenant.yml, a tenant.yml, "
        f"or a tenant name under {tenants_dir()}"
    )


def load_tenant(path: str | Path) -> TenantConfig:
    """Load a tenant from its directory, its tenant.yml, or its name under tenants_dir()."""
    p = resolve_tenant(path)
    raw = yaml.safe_load(p.read_text())
    raw = expand_env(raw)
    cfg = TenantConfig.model_validate(raw)
    cfg.root = p.parent.resolve()
    _anchor_paths(cfg)
    return cfg


def tenants_dir() -> Path:
    """Where named tenants live. Override with UNDERSTORY_TENANTS_DIR."""
    env = os.environ.get("UNDERSTORY_TENANTS_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "tenants"
