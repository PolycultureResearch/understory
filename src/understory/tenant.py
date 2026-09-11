"""Tenant configuration: one directory per client.

    tenants/<name>/
      tenant.yml              this file's schema
      context.md              hand-written business context for get_context
      traps.yml               traps registry (understory.traps)
      semantic_manifest.json  from `dbt parse`, copied in by the deploy
      golden/questions.yml    eval set (understory.harness)

Environment variables are expanded in string values with `${VAR}` or
`${VAR:-default}` so the same tenant.yml works locally and in a container.
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
    """Compiled SQL cache keyed by spec hash. Defaults to <tenant>/.cache."""


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
    user_hash_secret: str = "${UNDERSTORY_USER_HASH_SECRET:-dev-secret-change-me}"
    text_retention_days: int = 90
    enabled: bool = True


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
    instructions: str | None = None
    """Short text for the MCP server `instructions` field."""

    # Filled in by load()
    root: Path = Field(default=Path("."), exclude=True)

    @property
    def manifest_path(self) -> Path:
        return self.root / "semantic_manifest.json"

    @property
    def context_path(self) -> Path:
        return self.root / "context.md"

    @property
    def traps_path(self) -> Path:
        return self.root / "traps.yml"

    @property
    def golden_path(self) -> Path:
        return self.root / "golden" / "questions.yml"


def load_tenant(path: str | Path) -> TenantConfig:
    """Load a tenant from its directory or its tenant.yml."""
    p = Path(path)
    if p.is_dir():
        p = p / "tenant.yml"
    raw = yaml.safe_load(p.read_text())
    raw = expand_env(raw)
    cfg = TenantConfig.model_validate(raw)
    cfg.root = p.parent.resolve()
    return cfg


def tenants_dir() -> Path:
    """Where tenants live. Override with UNDERSTORY_TENANTS_DIR."""
    env = os.environ.get("UNDERSTORY_TENANTS_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "tenants"
