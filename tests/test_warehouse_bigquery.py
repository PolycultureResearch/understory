"""BigQuery adapter with a mocked client. No network."""

from __future__ import annotations

from concurrent.futures import TimeoutError as FutureTimeout
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("google.cloud.bigquery")

from understory.protocols import QueryError, Warehouse  # noqa: E402
from understory.tenant import BigQueryConfig  # noqa: E402
from understory.warehouse import make_warehouse  # noqa: E402
from understory.warehouse.bigquery import BigQueryWarehouse, bq_relation  # noqa: E402


def _field(name: str, field_type: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, field_type=field_type)


def _row(*values):
    return SimpleNamespace(values=lambda: values)


def _client_returning(rows, schema):
    """A fake bigquery.Client whose query() returns a job whose result() yields rows."""
    client = MagicMock()
    job = MagicMock()
    iterator = MagicMock()
    iterator.__iter__.return_value = iter(rows)
    iterator.schema = schema
    job.result.return_value = iterator
    client.query.return_value = job
    return client, job


@pytest.fixture
def config() -> BigQueryConfig:
    return BigQueryConfig(project="proj", location="EU")


def test_job_config_and_query_call(config):
    client, job = _client_returning(
        [_row(1, date(2026, 5, 31)), _row(2, date(2026, 6, 1))],
        [_field("order_id", "INTEGER"), _field("order_date", "DATE")],
    )
    wh = BigQueryWarehouse(config, client=client)
    assert isinstance(wh, Warehouse)
    assert wh.name == "bigquery" and wh.dialect == "bigquery"

    res = wh.run(
        "select order_id, order_date from `proj.marts.fct_orders`", timeout_s=30, row_cap=10
    )

    client.query.assert_called_once()
    args, kwargs = client.query.call_args
    assert args[0].startswith("select order_id")
    assert kwargs["location"] == "EU"
    assert kwargs["timeout"] == 30
    jc = kwargs["job_config"]
    assert jc.use_legacy_sql is False
    # The client keeps this as a string in its API representation.
    assert int(jc.job_timeout_ms) == 30_000
    assert jc.maximum_bytes_billed is None

    job.result.assert_called_once_with(timeout=30, max_results=11)

    assert [c.name for c in res.columns] == ["order_id", "order_date"]
    assert [c.type for c in res.columns] == ["INTEGER", "DATE"]
    assert res.rows == [[1, "2026-05-31"], [2, "2026-06-01"]]
    assert res.row_count == 2
    assert res.truncated is False


def test_maximum_bytes_billed_read_from_config_when_present():
    cfg = BigQueryConfig(project="proj")
    # tenant.py does not declare the field yet; the adapter reads it via getattr.
    object.__setattr__(cfg, "maximum_bytes_billed", 10_000_000)
    client, _ = _client_returning([], [])
    wh = BigQueryWarehouse(cfg, client=client)
    wh.run("select 1", timeout_s=5, row_cap=1)
    jc = client.query.call_args.kwargs["job_config"]
    assert jc.maximum_bytes_billed == 10_000_000


def test_truncation_when_more_than_row_cap_rows(config):
    rows = [_row(i) for i in range(6)]  # row_cap + 1 returned by the API
    client, job = _client_returning(rows, [_field("n", "INTEGER")])
    wh = BigQueryWarehouse(config, client=client)

    res = wh.run("select n from `proj.marts.t`", timeout_s=5, row_cap=5)

    job.result.assert_called_once_with(timeout=5, max_results=6)
    assert res.row_count == 5
    assert len(res.rows) == 5
    assert res.truncated is True


def test_json_safe_values(config):
    client, _ = _client_returning(
        [_row(Decimal("1.50"), datetime(2026, 1, 2, 3, 4, 5), float("nan"), b"ab")],
        [
            _field("d", "NUMERIC"),
            _field("ts", "TIMESTAMP"),
            _field("f", "FLOAT"),
            _field("b", "BYTES"),
        ],
    )
    wh = BigQueryWarehouse(config, client=client)
    res = wh.run("select 1", timeout_s=5, row_cap=10)
    assert res.rows == [[1.5, "2026-01-02T03:04:05", None, "YWI="]]


def test_timeout_cancels_job_and_raises(config):
    client = MagicMock()
    job = MagicMock()
    job.result.side_effect = FutureTimeout()
    client.query.return_value = job
    wh = BigQueryWarehouse(config, client=client)

    with pytest.raises(QueryError, match="timeout"):
        wh.run("select 1", timeout_s=2, row_cap=10)
    job.cancel.assert_called_once()


def test_api_error_becomes_query_error(config):
    from google.api_core import exceptions as gexc

    client = MagicMock()
    client.query.side_effect = gexc.BadRequest("Syntax error: unexpected end of input")
    wh = BigQueryWarehouse(config, client=client)
    with pytest.raises(QueryError, match="Syntax error"):
        wh.run("select", timeout_s=2, row_cap=10)


def test_latest_date(config):
    client, _ = _client_returning([_row(date(2026, 5, 31))], [_field("latest", "DATE")])
    wh = BigQueryWarehouse(config, client=client)
    assert wh.latest_date('"proj"."marts"."fct_orders"', "order_date") == date(2026, 5, 31)
    sql = client.query.call_args.args[0]
    assert sql == "SELECT MAX(`order_date`) AS latest FROM `proj`.`marts`.`fct_orders`"


def test_latest_date_none_when_empty(config):
    client, _ = _client_returning([_row(None)], [_field("latest", "DATE")])
    wh = BigQueryWarehouse(config, client=client)
    assert wh.latest_date("`proj.marts.fct_orders`", "order_date") is None


def test_dimension_values_uses_query_parameter(config):
    client, job = _client_returning(
        [_row("US", 100), _row("AU", 20)],
        [_field("value", "STRING"), _field("count", "INTEGER")],
    )
    wh = BigQueryWarehouse(config, client=client)
    res = wh.dimension_values("`proj.marts.fct_orders`", "country", "u", limit=25)

    sql = client.query.call_args.args[0]
    assert "STRPOS(LOWER(CAST(`country` AS STRING)), LOWER(@q)) > 0" in sql
    jc = client.query.call_args.kwargs["job_config"]
    assert [p.name for p in jc.query_parameters] == ["q"]
    assert jc.query_parameters[0].value == "u"
    job.result.assert_called_once_with(timeout=wh.timeout_s, max_results=26)
    assert res.rows == [["US", 100], ["AU", 20]]


def test_relations_filters_to_scope(config):
    client = MagicMock()
    ds_marts = SimpleNamespace(dataset_id="marts", reference="proj.marts")
    ds_raw = SimpleNamespace(dataset_id="raw", reference="proj.raw")
    client.list_datasets.return_value = [ds_marts, ds_raw]
    client.list_tables.side_effect = lambda ref: [
        SimpleNamespace(table_id="fct_orders"),
        SimpleNamespace(table_id="dim_customers"),
    ]
    wh = BigQueryWarehouse(config, client=client, schemas=["MARTS"])
    assert wh.relations() == ["marts.dim_customers", "marts.fct_orders"]
    client.list_tables.assert_called_once_with("proj.marts")


def test_bq_relation_quoting():
    assert bq_relation('"proj"."marts"."t"') == "`proj`.`marts`.`t`"
    assert bq_relation("`proj.marts.t`") == "`proj.marts.t`"
    assert bq_relation("proj.marts.t") == "proj.marts.t"


def test_make_warehouse_dispatches_to_bigquery(config, monkeypatch):
    monkeypatch.setattr(BigQueryWarehouse, "_make_client", lambda self, cfg: MagicMock())
    wh = make_warehouse(config)
    assert isinstance(wh, BigQueryWarehouse)
