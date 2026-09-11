"""Shared fixtures. Tenants are the four fake_companies verticals.

Tests marked `fake_db` need the DuckDB files under $FAKE_COMPANIES_DIR/out
(default ../fake_companies/out). Tests marked `metricflow` also need the `mf`
CLI and dbt project, and are slow.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from understory.tenant import TenantConfig, load_tenant, tenants_dir

TENANTS = ["alpenglow", "white_cube", "meridian", "bristlecone"]


def _fake_db_available(cfg: TenantConfig) -> bool:
    return cfg.warehouse.type == "duckdb" and Path(cfg.warehouse.path).exists()


@pytest.fixture(scope="session")
def tenants() -> dict[str, TenantConfig]:
    return {name: load_tenant(tenants_dir() / name) for name in TENANTS}


@pytest.fixture(params=TENANTS, scope="session")
def tenant(request, tenants) -> TenantConfig:
    return tenants[request.param]


@pytest.fixture(scope="session")
def alpenglow(tenants) -> TenantConfig:
    return tenants["alpenglow"]


@pytest.fixture(scope="session")
def alpenglow_db(alpenglow) -> TenantConfig:
    if not _fake_db_available(alpenglow):
        pytest.skip("fake_companies DuckDB not available")
    return alpenglow


@pytest.fixture(scope="session")
def mf_available() -> bool:
    return shutil.which("mf") is not None


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "metricflow" in item.keywords and not shutil.which("mf"):
            item.add_marker(pytest.mark.skip(reason="mf CLI not on PATH"))
        if "fake_db" in item.keywords:
            fc = Path(
                os.environ.get("FAKE_COMPANIES_DIR", "/Users/devon/Documents/code/fake_companies")
            )
            if not (fc / "out" / "alpenglow.duckdb").exists():
                item.add_marker(
                    pytest.mark.skip(reason="fake_companies DuckDB files not available")
                )
