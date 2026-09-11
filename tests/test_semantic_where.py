"""Pure tests for the MetricFlow where/grain translation and argv building."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from understory.protocols import CompileError, SemanticLayer
from understory.semantic import make_semantic_layer
from understory.semantic.dbt_cloud import DbtCloud, compile_kwargs
from understory.semantic.metricflow_local import (
    clean_error_output,
    explain_argv,
    parse_explain_output,
)
from understory.semantic.where import clause_to_mf, time_group_by, to_mf_where
from understory.tenant import DbtCloudConfig
from understory.types import MetricSpec, TimeSpec, WhereClause


def clause(op, *values, dimension="order__country"):
    return WhereClause(dimension=dimension, op=op, values=list(values))


@pytest.mark.parametrize(
    ("op", "values", "expected"),
    [
        ("eq", ["US"], "{{ Dimension('order__country') }} = 'US'"),
        ("ne", ["US"], "{{ Dimension('order__country') }} <> 'US'"),
        ("in", ["US", "CA"], "{{ Dimension('order__country') }} IN ('US', 'CA')"),
        ("not_in", ["US", "CA"], "{{ Dimension('order__country') }} NOT IN ('US', 'CA')"),
        ("gt", [100], "{{ Dimension('order__country') }} > 100"),
        ("gte", [100], "{{ Dimension('order__country') }} >= 100"),
        ("lt", [2.5], "{{ Dimension('order__country') }} < 2.5"),
        ("lte", [2.5], "{{ Dimension('order__country') }} <= 2.5"),
    ],
)
def test_each_op(op, values, expected):
    assert clause_to_mf(clause(op, *values)) == expected


def test_string_with_embedded_quote_is_doubled():
    out = clause_to_mf(clause("eq", "O'Brien", dimension="customer__last_name"))
    assert out == "{{ Dimension('customer__last_name') }} = 'O''Brien'"


def test_numeric_values_unquoted_and_strings_quoted_in_same_list():
    out = clause_to_mf(clause("in", 1, "2", 3.0, dimension="order__bucket"))
    assert out == "{{ Dimension('order__bucket') }} IN (1, '2', 3.0)"


def test_empty_where_is_none_and_clauses_join_with_and():
    assert to_mf_where([]) is None
    out = to_mf_where([clause("eq", "US"), clause("gt", 10, dimension="order__units")])
    assert (
        out == "{{ Dimension('order__country') }} = 'US' AND {{ Dimension('order__units') }} > 10"
    )


def test_scalar_op_requires_exactly_one_value():
    with pytest.raises(ValueError):
        clause_to_mf(clause("eq", "US", "CA"))


def test_empty_in_list_renders_a_false_predicate():
    assert clause_to_mf(clause("in")) == "1 = 0"
    assert clause_to_mf(clause("not_in")) == "1 = 1"


# ------------------------------------------------------------------ grain


def spec(**kw) -> MetricSpec:
    return MetricSpec(metrics=["net_revenue"], **kw)


def test_grain_without_time_dimension_adds_metric_time():
    s = spec(group_by=["order__country"], time=TimeSpec(grain="month"))
    assert time_group_by(s, None) == ["order__country", "metric_time__month"]


def test_grain_applies_to_bare_metric_time():
    s = spec(group_by=["metric_time", "order__country"], time=TimeSpec(grain="week"))
    assert time_group_by(s, None) == ["metric_time__week", "order__country"]


def test_grain_applies_to_catalog_time_dimension():
    s = spec(group_by=["order__order_date"], time=TimeSpec(grain="quarter"))
    assert time_group_by(s, "order__order_date") == ["order__order_date__quarter"]


def test_explicit_grain_suffix_is_kept():
    s = spec(group_by=["metric_time__day"], time=TimeSpec(grain="month"))
    assert time_group_by(s, None) == ["metric_time__day"]


def test_no_grain_leaves_group_by_alone():
    s = spec(group_by=["metric_time", "order__country"])
    assert time_group_by(s, None) == ["metric_time", "order__country"]


# ------------------------------------------------------------------ argv


def test_explain_argv_full_spec():
    s = MetricSpec(
        metrics=["net_revenue", "orders"],
        group_by=["order__country"],
        where=[clause("in", "US", "CA")],
        time=TimeSpec(grain="month", start=date(2025, 1, 1), end=date(2025, 3, 31)),
        limit=50,
    )
    assert explain_argv(s) == [
        "mf",
        "query",
        "--metrics",
        "net_revenue,orders",
        "--group-by",
        "order__country,metric_time__month",
        "--start-time",
        "2025-01-01",
        "--end-time",
        "2025-03-31",
        "--where",
        "{{ Dimension('order__country') }} IN ('US', 'CA')",
        "--limit",
        "50",
        "--explain",
    ]


def test_explain_argv_minimal_spec_omits_optional_flags():
    assert explain_argv(MetricSpec(metrics=["orders"]), mf_bin="/x/mf") == [
        "/x/mf",
        "query",
        "--metrics",
        "orders",
        "--explain",
    ]


def test_explain_argv_needs_a_metric():
    with pytest.raises(CompileError):
        explain_argv(MetricSpec(metrics=[]))


# ------------------------------------------------------------------ output parsing

EXPLAIN_STDOUT = (
    "\r\r⠋ Initiating query…\r\r⠙ Initiating query…\r"
    "✔️ Success \U0001f389 - query completed after 0.04 seconds\n"
    "\U0001f50e SQL (remove --explain to see data or add --show-dataflow-plan ...):\n"
    "\n"
    "SELECT\n"
    "  metric_time__month\n"
    'FROM "alpenglow"."main_marts"."fct_orders"\n'
    "\n"
)


def test_parse_explain_output_after_marker():
    sql = parse_explain_output(EXPLAIN_STDOUT)
    assert sql is not None
    assert sql.startswith("SELECT\n")
    assert sql.endswith('"fct_orders"')


def test_parse_explain_output_without_marker_falls_back_to_first_select():
    quiet = "\r⠋ Initiating query…\nWITH x AS (SELECT 1)\nSELECT * FROM x\n"
    assert parse_explain_output(quiet) == "WITH x AS (SELECT 1)\nSELECT * FROM x"


def test_parse_explain_output_returns_none_without_sql():
    assert parse_explain_output("⠋ Initiating query…\nERROR: nope\n") is None


def test_clean_error_output_keeps_message_drops_boilerplate():
    stdout = (
        "⠋ Initiating query…⠙ Initiating query…\n"
        "ERROR: Got error(s) during query resolution.\n"
        "\n"
        "Error #1:\n"
        "  Message:\n"
        "\n"
        "    The given input does not exactly match any known metrics.\n"
        "\n"
        "Log File:: /somewhere/metricflow.log\n"
        "Artifact Path: /somewhere/target/semantic_manifest.json\n"
        "\n"
        "If you think you found a bug, please report it here:\n"
        "    https://github.com/dbt-labs/metricflow/issues\n"
    )
    msg = clean_error_output(stdout, "")
    assert msg.startswith("ERROR: Got error(s) during query resolution.")
    assert "does not exactly match any known metrics" in msg
    assert "Log File" not in msg
    assert "Initiating" not in msg
    assert "github.com" not in msg


# ------------------------------------------------------------------ dbt cloud (mocked)


def _cloud(client) -> DbtCloud:
    cfg = DbtCloudConfig(environment_id=1, token="t")
    return DbtCloud(cfg, Path("/nonexistent/semantic_manifest.json"), client=client)


def _mock_client(sql="SELECT 1 AS net_revenue"):
    client = MagicMock()
    client.session.return_value.__enter__.return_value = None
    client.session.return_value.__exit__.return_value = False
    client.compile_sql.return_value = sql
    return client


def test_dbt_cloud_compile_kwargs_and_result():
    client = _mock_client()
    layer = _cloud(client)
    s = MetricSpec(
        metrics=["net_revenue"],
        group_by=["order__country"],
        where=[clause("in", "US", "CA")],
        time=TimeSpec(grain="month", start=date(2025, 1, 1), end=date(2025, 3, 31)),
        limit=10,
    )
    out = layer.compile(s)
    client.compile_sql.assert_called_once_with(
        metrics=["net_revenue"],
        group_by=["order__country", "metric_time__month"],
        where=[
            "{{ Dimension('order__country') }} IN ('US', 'CA')",
            "{{ TimeDimension('metric_time', 'day') }} >= '2025-01-01'",
            "{{ TimeDimension('metric_time', 'day') }} <= '2025-03-31'",
        ],
        limit=10,
    )
    assert out.sql == "SELECT 1 AS net_revenue"
    assert out.spec_hash == s.hash()
    assert len(out.sql_hash) == 16
    assert out.metrics == ["net_revenue"]
    assert out.cache_hit is False
    assert layer.name == "dbt_cloud"
    assert isinstance(layer, SemanticLayer)


def test_dbt_cloud_sdk_error_becomes_compile_error():
    client = _mock_client()
    client.compile_sql.side_effect = RuntimeError("no such metric")
    with pytest.raises(CompileError, match="no such metric"):
        _cloud(client).compile(MetricSpec(metrics=["nope"]))


def test_dbt_cloud_empty_sql_is_compile_error():
    with pytest.raises(CompileError):
        _cloud(_mock_client(sql="")).compile(MetricSpec(metrics=["orders"]))


def test_dbt_cloud_missing_sdk_names_the_install():
    layer = _cloud(client=None)
    try:
        import dbtsl  # noqa: F401
    except ImportError:
        with pytest.raises(CompileError, match="dbt-sl-sdk"):
            layer.compile(MetricSpec(metrics=["orders"]))
    else:
        pytest.skip("dbtsl installed; lazy-import error path not exercised")


def test_compile_kwargs_minimal():
    assert compile_kwargs(MetricSpec(metrics=["orders"])) == {"metrics": ["orders"]}


def test_make_semantic_layer_dispatch():
    layer = make_semantic_layer(
        DbtCloudConfig(environment_id=1, token="t"), Path("/nonexistent/manifest.json")
    )
    assert layer.name == "dbt_cloud"
