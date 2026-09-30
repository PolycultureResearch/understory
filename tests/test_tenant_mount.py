"""A tenant directory that lives in a client's dbt repo (ADR 0001).

The layout under test is the one design section 7 describes:

    <client repo>/
      dbt_project.yml, models/, target/semantic_manifest.json
      understory/
        tenant.yml   with `dbt_project_dir: ..` and no manifest copy
        context.md, traps.yml, golden/
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from understory.catalog.manifest import load_catalog
from understory.cli import app
from understory.harness.deterministic import run_deterministic
from understory.harness.golden import load_golden
from understory.server.service import Service
from understory.telemetry import TelemetryWriter
from understory.tenant import LogConfig, load_tenant, resolve_tenant, tenants_dir

FIXTURE = tenants_dir() / "alpenglow"
FAKE_COMPANIES = Path(
    os.environ.get("FAKE_COMPANIES_DIR", "/Users/devon/Documents/code/fake_companies")
)


def _client_repo(tmp_path: Path, tenant_yml: dict, *, dbt_project: Path | None = None) -> Path:
    """A client repo with alpenglow's tenant files under understory/ and the manifest in target/."""
    repo = tmp_path / "client-dbt"
    if dbt_project:
        shutil.copytree(dbt_project, repo, ignore=shutil.ignore_patterns("logs"))
    else:
        (repo / "target").mkdir(parents=True)
        (repo / "dbt_project.yml").write_text("name: client\nprofile: client\n")
        shutil.copy(FIXTURE / "semantic_manifest.json", repo / "target")
    tenant = repo / "understory"
    tenant.mkdir()
    for name in ("context.md", "traps.yml"):
        shutil.copy(FIXTURE / name, tenant)
    shutil.copytree(FIXTURE / "golden", tenant / "golden")
    (tenant / "tenant.yml").write_text(yaml.safe_dump(tenant_yml))
    return tenant


def _tenant_yml(**overrides) -> dict:
    raw = yaml.safe_load((FIXTURE / "tenant.yml").read_text())
    raw["warehouse"]["path"] = "../warehouse.duckdb"
    raw["semantic_layer"]["dbt_project_dir"] = ".."
    raw["semantic_layer"].pop("env", None)
    raw.update(overrides)
    return raw


def test_relative_paths_resolve_against_the_tenant_directory(tmp_path, monkeypatch):
    tenant = _client_repo(tmp_path, _tenant_yml())
    monkeypatch.chdir(tmp_path)  # anywhere but the repo
    cfg = load_tenant(tenant)
    repo = tenant.parent.resolve()
    assert cfg.warehouse.path == str(repo / "warehouse.duckdb")
    assert cfg.semantic_layer.dbt_project_dir == str(repo)
    assert cfg.log.events_prefix.endswith("alpenglow/events"), "log prefixes are left alone"


def test_manifest_comes_from_the_dbt_target_when_the_tenant_has_no_copy(tmp_path):
    tenant = _client_repo(tmp_path, _tenant_yml())
    cfg = load_tenant(tenant)
    assert cfg.manifest_path == tenant.parent.resolve() / "target" / "semantic_manifest.json"
    assert load_catalog(cfg.manifest_path).metrics


def test_a_copy_in_the_tenant_directory_wins_and_manifest_overrides_both(tmp_path):
    tenant = _client_repo(tmp_path, _tenant_yml())
    shutil.copy(FIXTURE / "semantic_manifest.json", tenant)
    assert load_tenant(tenant).manifest_path == tenant.resolve() / "semantic_manifest.json"

    (tmp_path / "elsewhere").mkdir()
    shutil.copy(FIXTURE / "semantic_manifest.json", tmp_path / "elsewhere")
    (tenant / "tenant.yml").write_text(
        yaml.safe_dump(_tenant_yml(manifest="../../elsewhere/semantic_manifest.json"))
    )
    cfg = load_tenant(tenant)
    assert cfg.manifest_path == (tmp_path / "elsewhere" / "semantic_manifest.json").resolve()


def test_absolute_paths_and_urls_are_kept(tmp_path):
    raw = _tenant_yml()
    raw["warehouse"]["path"] = "/data/warehouse.duckdb"
    tenant = _client_repo(tmp_path, raw)
    assert load_tenant(tenant).warehouse.path == "/data/warehouse.duckdb"

    raw["warehouse"]["path"] = "md:analytics"
    (tenant / "tenant.yml").write_text(yaml.safe_dump(raw))
    assert load_tenant(tenant).warehouse.path == "md:analytics"


def test_a_tenant_is_found_by_directory_file_or_name(tmp_path):
    tenant = _client_repo(tmp_path, _tenant_yml())
    assert resolve_tenant(tenant) == tenant / "tenant.yml"
    assert resolve_tenant(tenant / "tenant.yml") == tenant / "tenant.yml"
    assert resolve_tenant("alpenglow") == FIXTURE / "tenant.yml"
    with pytest.raises(FileNotFoundError, match="no tenant at"):
        resolve_tenant(tmp_path / "nowhere")


def test_the_cli_reads_the_tenant_from_the_environment(tmp_path):
    tenant = _client_repo(tmp_path, _tenant_yml())
    result = CliRunner().invoke(app, ["traps", "check"], env={"UNDERSTORY_TENANT": str(tenant)})
    assert result.exit_code == 0, result.output
    assert "traps registry clean" in result.output


@pytest.mark.fake_db
@pytest.mark.metricflow
@pytest.mark.slow
def test_the_trap_set_passes_from_a_mounted_client_repo(tmp_path, monkeypatch):
    """The whole path: a copied dbt project, relative paths, no manifest copy, cwd elsewhere."""
    db = FAKE_COMPANIES / "out" / "alpenglow.duckdb"
    raw = _tenant_yml()
    raw["warehouse"]["path"] = str(db)
    raw["semantic_layer"]["env"] = {"FAKE_DB": str(db)}
    raw["semantic_layer"]["cache_dir"] = "../target/understory-cache"
    tenant = _client_repo(tmp_path, raw, dbt_project=FAKE_COMPANIES / "dbt" / "retail_dtc")
    monkeypatch.chdir(tmp_path)

    cfg = load_tenant(tenant)
    assert not (tenant / "semantic_manifest.json").exists()
    log = LogConfig(events_prefix="unused/events", text_prefix="unused/text", enabled=False)
    service = Service(cfg, telemetry=TelemetryWriter(log, cfg.name))
    try:
        items = load_golden(cfg.golden_path)
        report = run_deterministic(service, items)
    finally:
        service.close()
    failures = [(i.id, i.reasons) for i in report.items if not i.passed]
    assert not failures, failures
    assert any((tenant.parent / "target" / "understory-cache").iterdir())


def test_a_read_only_cache_is_skipped_not_fatal(tmp_path, caplog):
    from understory.semantic.metricflow_local import MetricFlowLocal

    tenant = _client_repo(tmp_path, _tenant_yml())
    cfg = load_tenant(tenant)
    mf = MetricFlowLocal(cfg.semantic_layer, cfg.manifest_path)
    mf.cache_dir = tmp_path / "ro"
    mf.cache_dir.mkdir()
    mf.cache_dir.chmod(0o500)
    try:
        mf._cache_put("abc", "select 1")
    finally:
        mf.cache_dir.chmod(0o700)
    assert "compile cache not written" in caplog.text
