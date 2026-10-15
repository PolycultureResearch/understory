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


def test_trap_rewrite_cannot_bypass_validation(service, session):
    """Gross margin carries category; the chosen margin rate does not. The refusal
    is Understory's invalid message, never MetricFlow's resolution error."""
    r = service.query_metrics(
        session,
        {
            "metrics": ["gross_margin"],
            "group_by": ["order_item__category"],
            "time": {"start": "2025-01-01", "end": "2025-03-31"},
            "question": "margin by category in Q1 2025",
            "clarifications": [{"trap": "collision:margin", "choice": "margin_rate"}],
        },
    )
    assert r.status == Status.invalid, r
    assert r.refusal is not None
    assert r.refusal.message == (
        "Dimension 'order_item__category' is not available for margin_rate. "
        "Valid dimensions are listed by describe_metric."
    )
    assert r.refusal.missing == ["order_item__category"]


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
    draft = f"Net revenue was {value:,.2f} in the first month."
    bare = service.log_answer(session, draft)
    assert bare.status == "missing_disclosures", bare
    assert bare.disclosures_missing == r.required_disclosures
    assert "disclosures_missing" in bare.how_to_read
    ok = service.log_answer(session, draft + " " + " ".join(r.required_disclosures))
    assert ok.status == "pass", ok
    bad = service.log_answer(session, "Net revenue was 123,456,789.")
    assert bad.status == "unsourced_numbers"
    assert "percent change" in bad.how_to_read


def test_a_spec_without_the_question_is_refused_and_is_not_a_gap(service, session):
    before = service.telemetry.pending
    for spec in ({"metrics": ["net_revenue"]}, {"metrics": ["net_revenue"], "question": "  "}):
        r = service.query_metrics(session, spec)
        assert r.status == Status.invalid, r
        assert "spec.question" in r.refusal.message
        assert r.result is None
    assert service.telemetry.pending[2] == before[2], "no gap record"


def test_a_trap_word_in_an_earlier_turn_still_fires(service, session):
    """The follow-up from the grill: the question alone carries no trap word."""
    spec = {
        "metrics": ["gross_revenue"],
        "group_by": ["order__country"],
        "time": {"start": "2025-01-01", "end": "2025-03-31"},
        "question": "Split that by country.",
    }
    alone = service.query_metrics(session, spec)
    assert alone.provenance.metrics == ["gross_revenue"]

    heard = service.query_metrics(
        session, {**spec, "earlier": ["What was revenue in the first quarter of 2025?"]}
    )
    assert heard.provenance.metrics == ["net_revenue"]
    assert any("net revenue" in d.lower() for d in heard.required_disclosures)

    named = service.query_metrics(
        session,
        {
            **spec,
            "question": "Now show gross revenue by country.",
            "earlier": ["What was revenue in the first quarter of 2025?"],
        },
    )
    assert named.provenance.metrics == ["gross_revenue"], "the user asked for it by name"


def test_a_draft_owes_only_the_disclosures_of_the_results_it_quotes(service, session):
    asked = service.run_sql(
        session,
        "select count(*) as n from main_marts.fct_orders where country = 'US'",
        question="How many US orders?",
        reason="checking the scope of the check",
    )
    governed = service.query_metrics(
        session,
        {
            "metrics": ["orders"],
            "time": {"start": "2025-02-01", "end": "2025-02-28"},
            "question": "How many orders in February 2025?",
        },
    )
    value = governed.result.rows[0][-1]
    review = service.log_answer(
        session, f"There were {value:,} orders. " + " ".join(governed.required_disclosures)
    )
    assert review.status == "pass", review
    assert not any("ad hoc SQL" in d for d in review.disclosures_present)
    assert asked.required_disclosures[0] not in review.disclosures_missing


def test_end_anchors_to_data(service, session):
    r = service.query_metrics(
        session,
        {
            "metrics": ["orders"],
            "time": {"grain": "month", "start": "2025-06-01", "end": "2099-01-01"},
            "question": "Orders by month since June 2025",
        },
    )
    assert r.status == Status.resolved, r
    assert r.provenance.applied_time.end == r.provenance.data_through


def test_invalid_metric_and_dimension(service, session):
    r = service.query_metrics(session, {"metrics": ["revenu"], "question": "revenu"})
    assert r.status == Status.invalid and "net_revenue" in r.refusal.suggestions
    r = service.query_metrics(
        session,
        {"metrics": ["orders"], "group_by": ["order__promo_code"], "question": "by promo code"},
    )
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
