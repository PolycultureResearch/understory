"""Catalog tests over the four fake_companies manifests."""

from __future__ import annotations

from datetime import date

import pytest

from understory.catalog import (
    METRIC_TIME,
    CatalogError,
    build_context,
    catalog_from_manifest,
    describe_metric,
    list_metrics,
    load_catalog,
    search_dimensions,
)
from understory.types import Catalog, MetricSpec

TENANTS = ["alpenglow", "white_cube", "meridian", "bristlecone"]


@pytest.fixture(scope="session")
def catalogs(tenants) -> dict[str, Catalog]:
    return {name: load_catalog(cfg.manifest_path) for name, cfg in tenants.items()}


@pytest.fixture(scope="session")
def alpenglow_catalog(catalogs) -> Catalog:
    return catalogs["alpenglow"]


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def test_all_manifests_load(catalogs):
    assert set(catalogs) == set(TENANTS)
    for name, cat in catalogs.items():
        assert cat.metrics, name
        assert cat.time_spine and "metricflow_time_spine" in cat.time_spine, name
        assert METRIC_TIME in cat.dimensions, name
        for m in cat.metrics.values():
            assert m.expr, f"{name}.{m.name} has no expr"
            assert m.dimensions[0] == METRIC_TIME, f"{name}.{m.name}"
            assert m.time_dimension in cat.dimensions, f"{name}.{m.name}"
            assert m.relations, f"{name}.{m.name}"
            for d in m.dimensions:
                assert d in cat.dimensions, f"{name}.{m.name} lists unknown dimension {d}"


def test_alpenglow_metric_shapes(alpenglow_catalog):
    cat = alpenglow_catalog
    net = cat.metric("net_revenue")
    assert net is not None
    assert net.type == "derived"
    assert net.inputs == ["gross_revenue", "discounts", "refunds"]
    assert net.expr == "gross_revenue - discounts - refunds"
    assert net.time_dimension == "order__order_date"
    assert net.meta["understory"]["time_dimensions"] == [
        "order__order_date",
        "return__request_date",
    ]

    aov = cat.metric("aov")
    assert aov.type == "ratio"
    assert aov.inputs == ["gross_revenue", "orders"]
    assert aov.expr == "gross_revenue / orders"

    orders = cat.metric("orders")
    assert orders.type == "simple"
    assert orders.expr == "sum(1)"
    assert orders.inputs == []
    assert orders.relations == ['"alpenglow"."main_marts"."fct_orders"']

    gross = cat.metric("gross_revenue")
    assert gross.expr == "sum(gross_amount)"


def test_dimension_naming(alpenglow_catalog):
    cat = alpenglow_catalog
    assert "order__country" in cat.dimensions
    country = cat.dimensions["order__country"]
    assert country.type == "categorical"
    assert country.semantic_model == "orders"
    assert country.column == "country"
    assert country.relation == '"alpenglow"."main_marts"."fct_orders"'

    order_date = cat.dimensions["order__order_date"]
    assert order_date.type == "time"
    assert order_date.time_granularity == "day"

    mt = cat.dimensions[METRIC_TIME]
    assert mt.type == "time"
    assert mt.relation == cat.time_spine


def test_reachable_dimensions_one_hop_many_to_one(alpenglow_catalog):
    cat = alpenglow_catalog
    orders = cat.metric("orders").dimensions
    # Own dimensions, plus the customers table through the customer foreign key.
    assert "order__country" in orders
    assert "customer__first_channel" in orders
    # Returns hang off orders (one-to-many from the order side), so not reachable.
    assert "return__reason" not in orders

    # Refunds live on returns; the order dimensions come through the order key.
    refunds = cat.metric("refunds").dimensions
    assert "return__reason" in refunds
    assert "order__country" in refunds

    # A derived metric only keeps dimensions every input supports.
    net = cat.metric("net_revenue").dimensions
    assert "order__country" in net
    assert "return__reason" not in net
    assert "customer__first_channel" not in net


def test_dimension_expr_is_used_as_column(catalogs):
    plan = catalogs["white_cube"].dimensions["product_event__plan"]
    assert plan.column == "plan_at_event"


def test_cumulative_metric(catalogs):
    wau = catalogs["white_cube"].metric("wau")
    assert wau.type == "cumulative"
    assert wau.expr == "count_distinct(user_id) over a trailing 7 days window"


def test_cross_table_ratio_keeps_only_shared_dimensions(catalogs):
    win = catalogs["meridian"].metric("win_rate")
    assert win.type == "ratio"
    assert win.dimensions == [METRIC_TIME]
    assert len(win.relations) == 2


def test_nearest_metrics(alpenglow_catalog):
    assert alpenglow_catalog.nearest_metrics("revenu")
    assert "net_revenue" in alpenglow_catalog.nearest_metrics("revenu")


def test_unknown_aggregation_raises():
    manifest = {
        "semantic_models": [
            {
                "name": "orders",
                "node_relation": {"relation_name": '"db"."s"."orders"'},
                "entities": [{"name": "order", "type": "primary", "expr": "order_id"}],
                "dimensions": [
                    {
                        "name": "order_date",
                        "type": "time",
                        "type_params": {"time_granularity": "day"},
                    }
                ],
                "measures": [],
                "defaults": {"agg_time_dimension": "order_date"},
            }
        ],
        "metrics": [
            {"name": "mystery", "type": "simple", "type_params": {"measure": None}},
        ],
        "project_configuration": {},
    }
    with pytest.raises(CatalogError, match="mystery"):
        catalog_from_manifest(manifest)


def test_new_spec_inline_aggregation():
    manifest = {
        "semantic_models": [
            {
                "name": "orders",
                "node_relation": {"relation_name": '"db"."s"."orders"'},
                "entities": [{"name": "order", "type": "primary", "expr": "order_id"}],
                "dimensions": [
                    {
                        "name": "order_date",
                        "type": "time",
                        "type_params": {"time_granularity": "day"},
                    },
                    {"name": "country", "type": "categorical"},
                ],
                "measures": [],
                "defaults": {"agg_time_dimension": "order_date"},
            }
        ],
        "metrics": [
            {
                "name": "revenue",
                "type": "simple",
                "filter": {
                    "where_filters": [
                        {"where_sql_template": "{{ Dimension('order__country') }} = 'US'"}
                    ]
                },
                "config": {"meta": {"polyculture": {"synonyms": ["sales", "turnover"]}}},
                "type_params": {
                    "measure": None,
                    "expr": "amount",
                    "metric_aggregation_params": {"semantic_model": "orders", "agg": "sum"},
                },
            }
        ],
        "project_configuration": {
            "time_spine_table_configurations": [
                {"location": "db.s.spine", "column_name": "date_day", "grain": "day"}
            ]
        },
    }
    cat = catalog_from_manifest(manifest)
    m = cat.metric("revenue")
    assert m.expr == "sum(amount)"
    assert m.time_dimension == "order__order_date"
    assert m.dimensions == [METRIC_TIME, "order__order_date", "order__country"]
    assert m.filters == ["{{ Dimension('order__country') }} = 'US'"]
    assert m.synonyms == ["sales", "turnover"]
    assert cat.time_spine == "db.s.spine"


# --------------------------------------------------------------------------- #
# Discovery views
# --------------------------------------------------------------------------- #


def test_list_metrics_rows(alpenglow_catalog):
    rows = list_metrics(alpenglow_catalog)
    assert len(rows) == len(alpenglow_catalog.metrics)
    row = next(r for r in rows if r["name"] == "net_revenue")
    assert row["label"] == "Net Revenue"
    assert row["type"] == "derived"
    assert row["dimensions"] >= 2
    assert row["synonyms"] == []


def test_describe_metric_examples_are_valid_specs(alpenglow_catalog):
    out = describe_metric(alpenglow_catalog, "net_revenue")
    assert out["type"] == "derived"
    assert out["inputs"] == ["gross_revenue", "discounts", "refunds"]
    assert out["examples"]
    for ex in out["examples"]:
        spec = MetricSpec.model_validate(ex)
        for m in spec.metrics:
            assert m in alpenglow_catalog.metrics
        for d in spec.group_by:
            assert d in out["dimensions"]
    names = {d["name"] for d in out["dimension_details"]}
    assert names == set(out["dimensions"])


def test_describe_unknown_metric(alpenglow_catalog):
    out = describe_metric(alpenglow_catalog, "revenu")
    assert "error" in out
    assert "net_revenue" in out["nearest"]


def test_search_dimensions(alpenglow_catalog):
    hits = {d.name for d in search_dimensions(alpenglow_catalog, "COUNTRY")}
    assert "order__country" in hits
    assert "customer__country" in hits
    assert search_dimensions(alpenglow_catalog, "") == []
    assert {d.name for d in search_dimensions(alpenglow_catalog, "time axis")} == {METRIC_TIME}


# --------------------------------------------------------------------------- #
# get_context
# --------------------------------------------------------------------------- #


def test_build_context_every_tenant(tenants, catalogs):
    for name, cfg in tenants.items():
        doc = build_context(cfg, catalogs[name])
        size = len(doc.encode())
        assert size < 8 * 1024, f"{name} context is {size} bytes"
        assert cfg.display_name in doc, name
        for metric in catalogs[name].metrics:
            assert f"- {metric} (" in doc, f"{name} context misses {metric}"
        assert "## Metrics" in doc
        assert "## Dimensions" in doc
        assert "## Conventions" in doc
        assert "## SQL scope" in doc
        for schema in cfg.sql.schemas:
            assert schema in doc
        assert "never to today" in doc


def test_context_starts_with_hand_written_file(tenants, catalogs):
    cfg = tenants["alpenglow"]
    assert cfg.context_path.exists()
    hand = cfg.context_path.read_text().strip()
    doc = build_context(cfg, catalogs["alpenglow"])
    assert doc.startswith(hand)
    assert "MCP" not in hand
    assert "run_sql" not in hand


def test_context_states_freshness(tenants, catalogs):
    cfg = tenants["alpenglow"]
    doc = build_context(
        cfg,
        catalogs["alpenglow"],
        {"order__order_date": date(2026, 5, 31), "return__request_date": date(2026, 5, 30)},
    )
    assert "order__order_date: 2026-05-31" in doc
    assert "return__request_date: 2026-05-30" in doc
