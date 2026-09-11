"""DuckDB adapter against the alpenglow fake_companies database."""

from __future__ import annotations

import json
from datetime import date

import pytest

from understory.protocols import QueryError, Warehouse
from understory.warehouse import make_warehouse
from understory.warehouse.duckdb import DuckDBWarehouse

pytestmark = pytest.mark.fake_db

FCT_ORDERS = '"alpenglow"."main_marts"."fct_orders"'


@pytest.fixture(scope="module")
def wh(alpenglow_db) -> DuckDBWarehouse:
    w = make_warehouse(alpenglow_db.warehouse, schemas=alpenglow_db.sql.schemas)
    assert isinstance(w, DuckDBWarehouse)
    yield w
    w.close()


def test_protocol_and_provenance_names(wh):
    assert isinstance(wh, Warehouse)
    assert wh.name == "duckdb"
    assert wh.dialect == "duckdb"


def test_run_small_query_shape_and_json_safe_values(wh):
    res = wh.run(
        f"select order_id, order_date, placed_at, total_amount, is_first_order, discount_code "
        f"from {FCT_ORDERS} order by order_id limit 3",
        timeout_s=10,
        row_cap=200,
    )
    assert [c.name for c in res.columns] == [
        "order_id",
        "order_date",
        "placed_at",
        "total_amount",
        "is_first_order",
        "discount_code",
    ]
    assert [c.type for c in res.columns] == [
        "BIGINT",
        "DATE",
        "TIMESTAMP",
        "DOUBLE",
        "BOOLEAN",
        "VARCHAR",
    ]
    assert res.row_count == 3
    assert len(res.rows) == 3
    assert res.truncated is False
    assert res.elapsed_ms >= 0
    first = res.rows[0]
    assert isinstance(first[0], int)
    assert isinstance(first[1], str) and date.fromisoformat(first[1])
    assert isinstance(first[2], str) and "T" in first[2]
    assert isinstance(first[3], float)
    assert isinstance(first[4], bool)
    # Round-trips through json without a custom encoder.
    json.dumps(res.model_dump())


def test_run_converts_decimal_and_nan(wh):
    res = wh.run(
        "select 1.25::decimal(10,2) as d, 'nan'::double as n, 'inf'::double as i",
        timeout_s=10,
        row_cap=10,
    )
    assert res.rows == [[1.25, None, None]]
    assert res.columns[0].type == "DECIMAL(10,2)"


def test_row_cap_marks_truncation(wh):
    res = wh.run(f"select order_id from {FCT_ORDERS}", timeout_s=10, row_cap=5)
    assert res.row_count == 5
    assert len(res.rows) == 5
    assert res.truncated is True

    exact = wh.run(f"select order_id from {FCT_ORDERS} limit 5", timeout_s=10, row_cap=5)
    assert exact.row_count == 5
    assert exact.truncated is False


def test_unquoted_relation_also_works(wh):
    res = wh.run("select count(*) as n from main_marts.fct_orders", timeout_s=10, row_cap=1)
    assert res.rows[0][0] > 0


def test_latest_date_returns_date(wh):
    d = wh.latest_date(FCT_ORDERS, "order_date")
    assert isinstance(d, date)
    assert d.year >= 2024


def test_latest_date_on_empty_relation_is_none(wh):
    d = wh.latest_date("(select order_date from main_marts.fct_orders where 1 = 0)", "order_date")
    assert d is None


def test_dimension_values_case_insensitive_contains_with_counts(wh):
    res = wh.dimension_values(FCT_ORDERS, "country", "u", limit=25)
    assert [c.name for c in res.columns] == ["value", "count"]
    assert res.row_count >= 1
    for value, count in res.rows:
        assert "u" in value.lower()
        assert isinstance(count, int) and count > 0
    counts = [row[1] for row in res.rows]
    assert counts == sorted(counts, reverse=True)
    assert any(row[0] == "US" for row in res.rows)


def test_dimension_values_respects_limit(wh):
    res = wh.dimension_values(FCT_ORDERS, "customer_id", "1", limit=3)
    assert res.row_count == 3
    assert res.truncated is True


def test_relations_lists_scoped_schema_tables(wh):
    rels = wh.relations()
    assert "main_marts.fct_orders" in rels
    assert all(r.startswith("main_marts.") for r in rels)


def test_relations_without_scope_lists_every_schema(alpenglow_db):
    w = make_warehouse(alpenglow_db.warehouse)
    try:
        rels = w.relations()
    finally:
        w.close()
    schemas = {r.split(".")[0] for r in rels}
    assert {"main_marts", "main_staging", "shop_db"} <= schemas
    assert "information_schema" not in schemas


def test_bad_sql_raises_query_error(wh):
    with pytest.raises(QueryError):
        wh.run("select * from main_marts.no_such_table", timeout_s=10, row_cap=10)


def test_read_only_rejects_writes(wh):
    with pytest.raises(QueryError):
        wh.run("create table main_marts.scratch as select 1", timeout_s=10, row_cap=10)


def test_timeout_raises_query_error(wh):
    slow = "select count(*) from range(1000000000) a, range(1000000000) b"
    with pytest.raises(QueryError, match="timeout"):
        wh.run(slow, timeout_s=1, row_cap=10)
    # The shared connection is still usable afterwards.
    assert wh.run("select 1", timeout_s=5, row_cap=1).rows == [[1]]
