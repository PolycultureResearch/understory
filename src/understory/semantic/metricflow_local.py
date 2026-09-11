"""SemanticLayer over the open-source MetricFlow CLI.

`mf query --explain` is run as a subprocess inside the client's dbt project and
its printed SQL is captured. Nothing is executed here: the SQL goes to the
Warehouse adapter so the governed path and `run_sql` share one connection.

A compile takes a few seconds because `mf` boots dbt and parses the project on
every call, so compiled SQL is cached on disk keyed by the spec hash and a hash
of the tenant's semantic manifest. A dbt deploy that changes the manifest
invalidates every cached entry.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from understory.protocols import CompileError
from understory.semantic.where import time_group_by, to_mf_where
from understory.tenant import MetricFlowLocalConfig
from understory.types import Catalog, CompiledQuery, MetricSpec

SQL_MARKER = "SQL (remove --explain"
"""The line `mf query --explain` prints right before the SQL. The emoji before
it is not matched so a terminal that mangles it does not break parsing."""

COMPILE_TIMEOUT_S = 120

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_SPINNER = re.compile(r"[⠀-⣿]\s*Initiating query…?")
_NON_TEXT = re.compile(r"[\U00010000-\U0010ffff☀-➿️]")
_ENV_VAR = re.compile(r"\{\{\s*env_var\(\s*'([^']+)'\s*(?:,\s*'([^']*)'\s*)?\)\s*\}\}")

_DIALECTS = {
    "duckdb": "duckdb",
    "bigquery": "bigquery",
    "snowflake": "snowflake",
    "postgres": "postgres",
    "redshift": "redshift",
    "databricks": "databricks",
    "trino": "trino",
}


def _short_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def explain_argv(
    spec: MetricSpec,
    *,
    mf_bin: str = "mf",
    catalog_time_dim: str | None = None,
) -> list[str]:
    """Build the `mf query --explain` argv for a spec. Pure, so it is unit-testable.

    `catalog_time_dim` is the primary metric's agg time dimension as the catalog
    names it (for example `order__order_date`); it lets a grain be applied to
    that dimension when the spec groups by it directly instead of `metric_time`.
    """
    if not spec.metrics:
        raise CompileError("A query needs at least one metric.")
    argv = [mf_bin, "query", "--metrics", ",".join(spec.metrics)]
    group_by = time_group_by(spec, catalog_time_dim)
    if group_by:
        argv += ["--group-by", ",".join(group_by)]
    if spec.time.start is not None:
        argv += ["--start-time", spec.time.start.isoformat()]
    if spec.time.end is not None:
        argv += ["--end-time", spec.time.end.isoformat()]
    where = to_mf_where(spec.where)
    if where:
        argv += ["--where", where]
    if spec.limit is not None:
        argv += ["--limit", str(spec.limit)]
    argv.append("--explain")
    return argv


def parse_explain_output(stdout: str) -> str | None:
    """Pull the SQL out of `mf query --explain` stdout.

    Looks for the marker line first and takes the first line starting with
    SELECT or WITH after it. Without a marker (for example under `--quiet`) it
    falls back to the first such line anywhere. Returns None when no SQL is found.
    """
    text = _ANSI.sub("", stdout).replace("\r", "\n")
    lines = text.split("\n")
    start = 0
    for i, line in enumerate(lines):
        if SQL_MARKER in line:
            start = i + 1
            break
    for i in range(start, len(lines)):
        head = lines[i].lstrip().upper()
        if head.startswith(("SELECT", "WITH")):
            return "\n".join(lines[i:]).rstrip()
    return None


def clean_error_output(stdout: str, stderr: str) -> str:
    """Reduce mf's failure output to the part a user can act on.

    MetricFlow prints a spinner, then `ERROR: ...` with the resolution message,
    then log-file and bug-report boilerplate. Keep the middle.
    """
    text = _ANSI.sub("", stdout + "\n" + stderr).replace("\r", "\n")
    text = _SPINNER.sub("", text)
    text = _NON_TEXT.sub("", text)
    lines = [ln.rstrip() for ln in text.split("\n")]

    start = next((i for i, ln in enumerate(lines) if ln.lstrip().startswith("ERROR")), None)
    if start is None:
        # Traceback or something unexpected. Keep the tail, which is where
        # Python puts the exception.
        kept = [ln for ln in lines if ln.strip()][-30:]
    else:
        kept = []
        for ln in lines[start:]:
            s = ln.strip()
            if s.startswith(("Log File", "Artifact Path", "Artifact Modified", "If you think")):
                break
            if s.startswith("https://github.com/dbt-labs/metricflow"):
                break
            kept.append(ln)

    # Collapse runs of blank lines and any indentation mf used for pretty printing.
    out: list[str] = []
    for ln in kept:
        s = ln.strip()
        if not s and (not out or not out[-1]):
            continue
        out.append(s)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out) or "MetricFlow failed without an error message."


def _expand_dbt_env_vars(value: Any) -> Any:
    """Minimal `{{ env_var('X', 'default') }}` expansion for profiles.yml values."""
    if isinstance(value, str):
        return _ENV_VAR.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, dict):
        return {k: _expand_dbt_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_dbt_env_vars(v) for v in value]
    return value


def read_adapter_type(dbt_project_dir: Path, profiles_dir: Path) -> str | None:
    """Return the adapter `type` of the active target in profiles.yml, or None.

    The profile name comes from dbt_project.yml and the target from the
    profile's `target` key, overridable with DBT_TARGET as dbt itself allows.
    """
    profiles_path = profiles_dir / "profiles.yml"
    if not profiles_path.exists():
        return None
    profiles = _expand_dbt_env_vars(yaml.safe_load(profiles_path.read_text()) or {})

    profile_name = None
    project_path = dbt_project_dir / "dbt_project.yml"
    if project_path.exists():
        project = yaml.safe_load(project_path.read_text()) or {}
        profile_name = project.get("profile")
    profile = profiles.get(profile_name) if profile_name else None
    if profile is None:
        # Fall back to the only profile in the file, if there is exactly one.
        candidates = [v for k, v in profiles.items() if k != "config" and isinstance(v, dict)]
        if len(candidates) != 1:
            return None
        profile = candidates[0]

    outputs = profile.get("outputs") or {}
    target = os.environ.get("DBT_TARGET") or profile.get("target")
    output = outputs.get(target) if target else None
    if output is None and len(outputs) == 1:
        output = next(iter(outputs.values()))
    if not isinstance(output, dict):
        return None
    adapter = output.get("type")
    return str(adapter).lower() if adapter else None


class MetricFlowLocal:
    """Compile MetricSpecs with a local `mf` CLI. See the module docstring."""

    name = "metricflow_local"

    def __init__(self, config: MetricFlowLocalConfig, manifest_path: Path) -> None:
        self.config = config
        self.manifest_path = Path(manifest_path)
        self.dbt_project_dir = Path(config.dbt_project_dir).expanduser().resolve()
        self.profiles_dir = (
            Path(config.profiles_dir).expanduser().resolve()
            if config.profiles_dir
            else self.dbt_project_dir
        )
        self.cache_dir = (
            Path(config.cache_dir).expanduser()
            if config.cache_dir
            else self.manifest_path.parent / ".cache" / "mf"
        )
        adapter = read_adapter_type(self.dbt_project_dir, self.profiles_dir)
        self.dialect = _DIALECTS.get(adapter or "", adapter or "duckdb")
        self._catalog: Catalog | None = None
        self._manifest_hash: tuple[tuple[int, int], str] | None = None

    # ----------------------------------------------------------------- catalog

    def catalog(self) -> Catalog:
        if self._catalog is None:
            from understory.catalog.manifest import load_catalog

            self._catalog = load_catalog(self.manifest_path)
        return self._catalog

    def _time_dim_for(self, spec: MetricSpec) -> str | None:
        """The primary metric's agg time dimension, if the catalog can tell us."""
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

    # ------------------------------------------------------------------- cache

    def manifest_hash(self) -> str:
        """Short hash of the tenant manifest bytes, memoized on (mtime_ns, size)."""
        st = self.manifest_path.stat()
        key = (st.st_mtime_ns, st.st_size)
        if self._manifest_hash is None or self._manifest_hash[0] != key:
            self._manifest_hash = (key, _short_hash(self.manifest_path.read_bytes()))
        return self._manifest_hash[1]

    def _cache_path(self, spec_hash: str) -> Path:
        return self.cache_dir / f"{self.manifest_hash()}-{spec_hash}.sql"

    def _cache_get(self, spec_hash: str) -> str | None:
        path = self._cache_path(spec_hash)
        try:
            return path.read_text()
        except FileNotFoundError:
            return None

    def _cache_put(self, spec_hash: str, sql: str) -> None:
        path = self._cache_path(spec_hash)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".sql.tmp")
        tmp.write_text(sql)
        tmp.replace(path)

    # ----------------------------------------------------------------- compile

    def explain_argv(self, spec: MetricSpec) -> list[str]:
        return explain_argv(spec, mf_bin=self._mf_bin(), catalog_time_dim=self._time_dim_for(spec))

    def _mf_bin(self) -> str:
        """Resolve the mf executable. Prefer PATH, then the running interpreter's venv."""
        mf = self.config.mf_bin
        if os.sep in mf or shutil.which(mf):
            return mf
        sibling = Path(sys.executable).parent / mf
        if sibling.exists():
            return str(sibling)
        return mf

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["DBT_PROFILES_DIR"] = str(self.profiles_dir)
        env.update(self.config.env)
        return env

    def _run_mf(self, argv: list[str]) -> str:
        try:
            proc = subprocess.run(
                argv,
                cwd=self.dbt_project_dir,
                env=self._env(),
                capture_output=True,
                text=True,
                timeout=COMPILE_TIMEOUT_S,
            )
        except FileNotFoundError as e:
            raise CompileError(
                f"MetricFlow CLI {argv[0]!r} not found. Install the `metricflow` extra "
                "or set semantic_layer.mf_bin in tenant.yml."
            ) from e
        except subprocess.TimeoutExpired as e:
            raise CompileError(
                f"MetricFlow did not finish compiling within {COMPILE_TIMEOUT_S} seconds."
            ) from e
        if proc.returncode != 0:
            raise CompileError(clean_error_output(proc.stdout, proc.stderr))
        sql = parse_explain_output(proc.stdout)
        if sql is None:
            raise CompileError(
                "MetricFlow exited cleanly but printed no SQL.\n"
                + clean_error_output(proc.stdout, proc.stderr)
            )
        return sql

    def compile(self, spec: MetricSpec) -> CompiledQuery:
        spec_hash = spec.hash()
        cached = self._cache_get(spec_hash)
        if cached is not None:
            return CompiledQuery(
                sql=cached,
                spec_hash=spec_hash,
                sql_hash=_short_hash(cached.encode()),
                dialect=self.dialect,
                metrics=list(spec.metrics),
                cache_hit=True,
            )
        sql = self._run_mf(self.explain_argv(spec))
        self._cache_put(spec_hash, sql)
        return CompiledQuery(
            sql=sql,
            spec_hash=spec_hash,
            sql_hash=_short_hash(sql.encode()),
            dialect=self.dialect,
            metrics=list(spec.metrics),
            cache_hit=False,
        )
