"""End to end through the Service: real manifest, real MetricFlow, real DuckDB."""

from __future__ import annotations

from pathlib import Path

import pyarrow.parquet as pq
import pytest

from understory.server.service import Service
from understory.telemetry import TelemetryWriter
from understory.tenant import LogConfig
from understory.types import Status

pytestmark = [pytest.mark.fake_db, pytest.mark.metricflow]


@pytest.fixture(scope="module")
def service(alpenglow_db, tmp_path_factory):
    logdir = tmp_path_factory.mktemp("log")
    log = LogConfig(
        events_prefix=str(logdir / "events"),
        text_prefix=str(logdir / "text"),
        gaps_prefix=str(logdir / "gaps"),
        user_hash_secret="test",
    )
    svc = Service(alpenglow_db, telemetry=TelemetryWriter(log, alpenglow_db.name))
    yield svc
    svc.close()


@pytest.fixture
def session(service):
    return service.sessions.get("test-conn", user_hash="abc")


def test_context_and_discovery(service, session):
    ctx = service.get_context(session)
    assert "Alpenglow" in ctx and "net_revenue" in ctx
    listed = service.list_metrics(session)
    assert any(m["name"] == "net_revenue" for m in listed["metrics"])
    desc = service.describe_metric(session, "net_revenue")
    assert desc["data_through"] is not None
    assert service.describe_metric(session, "revenu")["status"] == Status.invalid
    vals = service.search_dimension_values(session, "order__country", "u")
    assert vals["status"] == Status.resolved and vals["values"]


def test_governed_query_resolves(service, session):
    r = service.query_metrics(
        session,
        {
            "metrics": ["net_revenue"],
            "group_by": ["metric_time"],
            "time": {"grain": "month", "start": "2025-01-01", "end": "2025-03-31"},
            "question": "net revenue by month in Q1 2025",
        },
    )
    assert r.status == Status.resolved, r
    assert r.result and r.result.row_count == 3
    assert r.provenance and r.provenance.governed and r.provenance.sql_hash
    assert any("not today" in d for d in r.required_disclosures)
    assert r.result_id in session.results

    # The number check accepts a number from that result and rejects a made-up one.
    value = next(v for row in r.result.rows for v in row if isinstance(v, int | float))
    ok = service.log_answer(session, f"Net revenue was {value:,.2f} in the first month.")
    assert ok.status == "pass", ok
    bad = service.log_answer(session, "Net revenue was 123,456,789.")
    assert bad.status == "unsourced_numbers"


def test_end_anchors_to_data(service, session):
    r = service.query_metrics(
        session,
        {
            "metrics": ["orders"],
            "time": {"grain": "month", "start": "2025-06-01", "end": "2099-01-01"},
        },
    )
    assert r.status == Status.resolved, r
    assert r.provenance.applied_time.end == r.provenance.data_through


def test_invalid_metric_and_dimension(service, session):
    r = service.query_metrics(session, {"metrics": ["revenu"]})
    assert r.status == Status.invalid and "net_revenue" in r.refusal.suggestions
    r = service.query_metrics(session, {"metrics": ["orders"], "group_by": ["order__promo_code"]})
    assert r.status == Status.invalid and r.refusal.suggestions


def test_run_sql_paths(service, session):
    r = service.run_sql(
        session,
        "select country, count(*) as n from main_marts.fct_orders group by 1",
        question="How many orders came from each country?",
        reason="no orders-by-country metric.",
    )
    assert r.status == Status.resolved, r
    assert r.provenance.governed is False and r.result.rows
    [disclosure] = [d for d in r.required_disclosures if "ad hoc SQL" in d]
    assert "because no orders-by-country metric." in disclosure
    assert "unverified" in disclosure

    r = service.run_sql(session, "delete from main_marts.fct_orders", question="q", reason="r")
    assert r.status == Status.sql_rejected
    r = service.run_sql(session, "select * from shop_db.orders limit 5", question="q", reason="r")
    assert r.status == Status.sql_rejected


def test_gaps_recorded_without_identity(service, session):
    invalid = service.query_metrics(
        session,
        {
            "metrics": ["return_rate"],
            "group_by": ["order__promo_code"],
            "question": "What was the return rate for orders that used a promo code?",
        },
    )
    assert invalid.status == Status.invalid
    assert invalid.refusal.missing == ["order__promo_code"]

    refused = service.query_metrics(
        session, {"metrics": ["gross_margin"], "question": "What is our profit by SKU?"}
    )
    assert refused.status == Status.unanswerable

    service.telemetry.flush()
    files = list(Path(service.telemetry.config.gaps_prefix).rglob("*.parquet"))
    assert files
    rows = [r for f in files for r in pq.read_table(f).to_pylist()]
    by_kind = {r["kind"]: r for r in rows}
    assert {"invalid", "unanswerable", "ungoverned_sql"} <= set(by_kind)
    assert "user_hash" not in rows[0] and "session_id" not in rows[0]

    assert by_kind["invalid"]["key"] == "order__promo_code"
    assert by_kind["invalid"]["question"].startswith("What was the return rate")
    assert '"missing":["order__promo_code"]' in by_kind["invalid"]["payload"]
    assert by_kind["unanswerable"]["key"] == "profit by sku"
    assert "category" in by_kind["unanswerable"]["reason"]
    sql_gap = by_kind["ungoverned_sql"]
    assert "fct_orders" in sql_gap["key"] and sql_gap["reason"] == "no orders-by-country metric"
    assert sql_gap["sql"].lower().startswith("select country")


def test_telemetry_written(service):
    service.telemetry.flush()
    cfg = service.telemetry.config
    events = list(Path(cfg.events_prefix).rglob("*.parquet"))
    text = list(Path(cfg.text_prefix).rglob("*.parquet"))
    assert events and text
