"""Pure tests for the run_sql guard. No database."""

from __future__ import annotations

import pytest

from understory.guard import GuardResult, guard_sql
from understory.tenant import SqlScope

CATALOG = ['"alpenglow"."main_marts"."fct_orders"', '"alpenglow"."main_marts"."dim_customers"']
SCOPE = SqlScope(schemas=["main_marts"])


def guard(sql: str, scope: SqlScope = SCOPE, **kw) -> GuardResult:
    return guard_sql(sql, dialect="duckdb", scope=scope, catalog_relations=CATALOG, **kw)


def test_accepts_select():
    r = guard("select order_id, total_amount from main_marts.fct_orders limit 5")
    assert r.ok, r.reason
    assert r.reason is None
    assert r.relations == ["main_marts.fct_orders"]
    assert "LIMIT 5" in r.sql


def test_accepts_cte_and_excludes_cte_names_from_relations():
    r = guard(
        "with recent as (select * from main_marts.fct_orders where order_date > '2026-01-01') "
        "select country, count(*) from recent group by 1"
    )
    assert r.ok, r.reason
    assert r.relations == ["main_marts.fct_orders"]


def test_accepts_quoted_manifest_relation():
    r = guard('select count(*) from "alpenglow"."main_marts"."fct_orders"')
    assert r.ok, r.reason
    assert r.relations == ["alpenglow.main_marts.fct_orders"]


def test_rejects_two_statements():
    r = guard("select 1; select 2")
    assert not r.ok
    assert "one statement" in r.reason


@pytest.mark.parametrize(
    "sql",
    [
        "insert into main_marts.fct_orders values (1)",
        "update main_marts.fct_orders set total_amount = 0",
        "delete from main_marts.fct_orders",
        "create table main_marts.x as select 1",
        "drop table main_marts.fct_orders",
        "copy main_marts.fct_orders to '/tmp/x.csv'",
        "attach '/tmp/other.duckdb' as other",
        "pragma database_list",
        "set threads = 1",
        "install httpfs",
        "load httpfs",
        "describe main_marts.fct_orders",
    ],
)
def test_rejects_non_select(sql):
    r = guard(sql)
    assert not r.ok, sql
    assert r.reason


def test_rejects_select_into():
    r = guard("select * into main_marts.copy_of from main_marts.fct_orders")
    assert not r.ok
    assert "INTO" in r.reason


@pytest.mark.parametrize(
    "sql",
    [
        "select * from read_parquet('/tmp/x.parquet')",
        "select * from read_csv_auto('/tmp/x.csv')",
        "select * from read_json('/tmp/x.json')",
        "select * from glob('*')",
        "select * from '/tmp/x.parquet'",
        "select * from 'https://example.com/x.parquet'",
        "select * from query('select 1')",
        "select * from main_marts.fct_orders where country = (select getenv('HOME'))",
    ],
)
def test_rejects_file_and_network_access(sql):
    r = guard(sql)
    assert not r.ok, sql
    assert r.reason


def test_rejects_out_of_scope_schema():
    r = guard("select * from main_staging.stg_orders")
    assert not r.ok
    assert "main_staging" in r.reason


def test_rejects_out_of_scope_schema_inside_subquery():
    r = guard(
        "select * from main_marts.fct_orders where customer_id in "
        "(select id from shop_db.customers)"
    )
    assert not r.ok
    assert "shop_db" in r.reason


def test_rejects_out_of_scope_schema_in_union():
    r = guard("select 1 from main_marts.fct_orders union all select 1 from web.sessions")
    assert not r.ok


def test_scope_is_case_insensitive():
    r = guard("select * from MAIN_MARTS.FCT_ORDERS")
    assert r.ok, r.reason


def test_allows_information_schema():
    r = guard("select table_name from information_schema.tables")
    assert r.ok, r.reason
    assert r.relations == ["information_schema.tables"]


def test_rejects_unqualified_table_when_scoped():
    r = guard("select * from fct_orders")
    assert not r.ok
    assert "schema-qualified" in r.reason


def test_empty_scope_derives_schemas_from_catalog():
    r = guard("select * from main_marts.fct_orders", scope=SqlScope())
    assert r.ok, r.reason
    r2 = guard("select * from web.sessions", scope=SqlScope())
    assert not r2.ok


def test_no_scope_and_no_catalog_allows_unqualified():
    r = guard_sql("select * from t", dialect="duckdb", scope=SqlScope(), catalog_relations=[])
    assert r.ok, r.reason
    assert r.relations == ["t"]


def test_deny_relations():
    scope = SqlScope(schemas=["main_marts"], deny_relations=["main_marts.dim_customers"])
    r = guard("select * from main_marts.dim_customers", scope=scope)
    assert not r.ok
    assert "dim_customers" in r.reason
    r2 = guard('select * from "alpenglow"."main_marts"."dim_customers"', scope=scope)
    assert not r2.ok


def test_disabled_scope_refuses():
    r = guard("select 1", scope=SqlScope(enabled=False))
    assert not r.ok


def test_adds_limit_when_missing():
    r = guard("select country from main_marts.fct_orders", row_cap=50)
    assert r.ok
    assert r.sql.endswith("LIMIT 50")


def test_keeps_existing_limit():
    r = guard("select country from main_marts.fct_orders limit 7", row_cap=50)
    assert r.ok
    assert r.sql.endswith("LIMIT 7")


def test_adds_limit_to_union():
    r = guard(
        "select 1 from main_marts.fct_orders union all select 2 from main_marts.dim_customers",
        row_cap=10,
    )
    assert r.ok, r.reason
    assert r.sql.endswith("LIMIT 10")
    assert r.relations == ["main_marts.fct_orders", "main_marts.dim_customers"]


def test_reports_relations_once_in_order():
    r = guard(
        "select * from main_marts.fct_orders o "
        "join main_marts.dim_customers c using (customer_id) "
        "join main_marts.fct_orders o2 on o.order_id = o2.order_id"
    )
    assert r.ok, r.reason
    assert r.relations == ["main_marts.fct_orders", "main_marts.dim_customers"]


def test_transpiles_to_target_dialect():
    r = guard_sql(
        'select order_id from "proj"."marts"."fct_orders"',
        dialect="bigquery",
        scope=SqlScope(schemas=["marts"]),
        catalog_relations=[],
        row_cap=5,
    )
    assert r.ok, r.reason
    assert r.sql == "SELECT order_id FROM `proj`.`marts`.`fct_orders` LIMIT 5"


def test_unparseable_sql_is_refused_not_raised():
    r = guard("select from where (((")
    assert not r.ok
    assert "parse" in r.reason
