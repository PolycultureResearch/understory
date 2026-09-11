"""End-to-end compile through the real `mf` CLI against the alpenglow tenant.

Slow (a few seconds per uncached compile). Nothing is executed against the
warehouse; only SQL text is inspected.
"""

from __future__ import annotations

from datetime import date

import pytest

from understory.protocols import CompileError, SemanticLayer
from understory.semantic import make_semantic_layer
from understory.semantic.metricflow_local import MetricFlowLocal
from understory.types import MetricSpec, TimeSpec, WhereClause

pytestmark = [pytest.mark.metricflow, pytest.mark.fake_db]

Q1 = TimeSpec(grain="month", start=date(2025, 1, 1), end=date(2025, 3, 31))


@pytest.fixture(scope="module")
def layer(alpenglow, tmp_path_factory) -> MetricFlowLocal:
    cfg = alpenglow.semantic_layer.model_copy(
        update={"cache_dir": str(tmp_path_factory.mktemp("mf-cache"))}
    )
    layer = make_semantic_layer(cfg, alpenglow.manifest_path)
    assert isinstance(layer, MetricFlowLocal)
    assert isinstance(layer, SemanticLayer)
    return layer


def test_dialect_from_profile(layer):
    assert layer.dialect == "duckdb"
    assert layer.name == "metricflow_local"


def test_compile_net_revenue_by_month(layer):
    spec = MetricSpec(metrics=["net_revenue"], time=Q1)
    out = layer.compile(spec)
    assert out.cache_hit is False
    assert "fct_orders" in out.sql
    assert "DATE_TRUNC('month'" in out.sql
    assert "2025-01-01" in out.sql and "2025-03-31" in out.sql
    assert out.spec_hash == spec.hash()
    assert len(out.sql_hash) == 16
    assert out.dialect == "duckdb"
    assert out.metrics == ["net_revenue"]

    again = layer.compile(spec)
    assert again.cache_hit is True
    assert again.sql == out.sql
    assert again.sql_hash == out.sql_hash


def test_unknown_metric_raises_compile_error(layer):
    with pytest.raises(CompileError) as exc:
        layer.compile(MetricSpec(metrics=["definitely_not_a_metric"], time=Q1))
    msg = str(exc.value)
    assert "does not exactly match any known metrics" in msg
    assert "Log File" not in msg


def test_where_in_list_is_quoted(layer):
    spec = MetricSpec(
        metrics=["net_revenue"],
        group_by=["order__country"],
        where=[WhereClause(dimension="order__country", op="in", values=["US", "CA"])],
        time=Q1,
        limit=10,
    )
    out = layer.compile(spec)
    assert "'US'" in out.sql
    assert "IN ('US', 'CA')" in out.sql
    assert "LIMIT 10" in out.sql


def test_catalog_loads_if_module_present(layer):
    pytest.importorskip("understory.catalog.manifest")
    catalog = layer.catalog()
    assert "net_revenue" in catalog.metrics
