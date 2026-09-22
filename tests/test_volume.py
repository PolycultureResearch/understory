"""The volume check: a period that loaded only partly is disclosed, never corrected."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from understory.server.volume import Dropout, describe, find_dropouts


def _daily(start: date, days: int, level: int) -> list[tuple[date, int]]:
    return [(start + timedelta(days=i), level) for i in range(days)]


def _weekly(start: date, weeks: int, level: int) -> list[tuple[date, int]]:
    return [(start + timedelta(days=7 * i), level) for i in range(weeks)]


def _set(series: list[tuple[date, int]], when: date, count: int) -> list[tuple[date, int]]:
    return [(d, count if d == when else n) for d, n in series]


MONDAY = date(2025, 1, 6)


def test_weekly_feed_flags_a_single_low_week():
    series = _set(_weekly(date(2024, 7, 1), 60, 180), date(2025, 1, 27), 38)
    found = find_dropouts(series)
    assert len(found) == 1
    drop = found[0]
    assert (drop.start, drop.end, drop.spacing) == (date(2025, 1, 27), date(2025, 2, 2), 7)
    assert (drop.rows, drop.expected) == (38, 180.0)
    assert round(drop.shortfall, 2) == 0.79
    assert drop.overlaps(date(2025, 1, 1), date(2025, 1, 31))
    assert not drop.overlaps(date(2025, 2, 3), date(2025, 2, 28))


def test_daily_feed_flags_a_run_of_two_days_but_not_one():
    base = _daily(MONDAY, 120, 300)
    one = _set(base, MONDAY + timedelta(days=45), 40)
    assert find_dropouts(one) == [], "a single low day reads as a holiday"
    two = _set(one, MONDAY + timedelta(days=46), 55)
    found = find_dropouts(two)
    assert len(found) == 1
    drop = found[0]
    assert (drop.start, drop.end, drop.spacing) == (
        MONDAY + timedelta(days=45),
        MONDAY + timedelta(days=46),
        1,
    )
    assert drop.rows == 95 and drop.expected == 600.0


def test_daily_feed_judges_against_the_same_weekday():
    """Quiet weekends are seasonality, not dropouts."""
    series = [
        (MONDAY + timedelta(days=i), 20 if (MONDAY + timedelta(days=i)).weekday() >= 5 else 300)
        for i in range(120)
    ]
    assert find_dropouts(series) == []


def test_ramp_up_and_tail_off_are_not_dropouts():
    rising = [(MONDAY + timedelta(days=i), 5 + 10 * i) for i in range(120)]
    assert find_dropouts(rising) == []
    falling = [(MONDAY + timedelta(days=i), max(0, 1200 - 10 * i)) for i in range(120)]
    assert find_dropouts(falling) == []


def test_sparse_and_short_series_are_left_alone():
    sparse = _set(_daily(MONDAY, 120, 12), MONDAY + timedelta(days=40), 1)
    sparse = _set(sparse, MONDAY + timedelta(days=41), 1)
    assert find_dropouts(sparse) == [], "a baseline under min_rows is too noisy to judge"
    assert find_dropouts(_weekly(MONDAY, 8, 100)) == []
    assert find_dropouts([]) == []


def test_thresholds_are_tunable():
    series = _set(_weekly(date(2024, 7, 1), 60, 180), date(2025, 1, 27), 100)
    assert find_dropouts(series) == []
    assert len(find_dropouts(series, low=0.6)) == 1


def test_describe_names_the_period_and_the_shortfall():
    week = Dropout(date(2025, 1, 27), date(2025, 2, 2), 7, 38, 180.0)
    text = describe(week, "scan__week_start")
    assert text.startswith("Row volume on scan__week_start from 2025-01-27 to 2025-02-02 is 79%")
    assert "same weeks around it (38 rows against about 180)" in text
    assert "incomplete" in text and "floor" in text
    day = Dropout(date(2024, 11, 12), date(2024, 11, 12), 1, 934, 2836.0)
    assert "on 2024-11-12 is 67% below the same days" in describe(day, "product_event__event_date")


# --------------------------------------------------------------------------- #
# Through the warehouse and the service.
# --------------------------------------------------------------------------- #


@pytest.mark.fake_db
def test_duckdb_daily_counts(alpenglow_db):
    from understory.warehouse import make_warehouse

    wh = make_warehouse(alpenglow_db.warehouse, schemas=alpenglow_db.sql.schemas)
    try:
        series = wh.daily_counts('"alpenglow"."main_marts"."fct_orders"', "order_date")
    finally:
        wh.close()
    assert len(series) > 700
    assert all(isinstance(d, date) and isinstance(n, int) and n > 0 for d, n in series)
    assert [d for d, _ in series] == sorted(d for d, _ in series)


@pytest.fixture(scope="module")
def bristlecone(tenants):
    from pathlib import Path

    cfg = tenants["bristlecone"]
    if not Path(cfg.warehouse.path).exists():
        pytest.skip("fake_companies DuckDB not available")
    return cfg


def _service(cfg):
    from understory.server.service import Service
    from understory.telemetry import TelemetryWriter
    from understory.tenant import LogConfig

    log = LogConfig(events_prefix="unused/events", text_prefix="unused/text", enabled=False)
    return Service(cfg, telemetry=TelemetryWriter(log, cfg.name))


_OUTAGE_WEEKS = {
    "metrics": ["pos_units"],
    "group_by": ["metric_time"],
    "time": {"grain": "week", "start": "2025-01-13", "end": "2025-02-16"},
    "question": "POS units by week from mid January to mid February 2025",
}


@pytest.mark.fake_db
@pytest.mark.metricflow
def test_service_discloses_the_pos_feed_outage(bristlecone):
    service = _service(bristlecone)
    try:
        r = service.query_metrics(service.sessions.get("v"), _OUTAGE_WEEKS)
        assert r.status == "resolved", r
        volume = [d for d in r.required_disclosures if d.startswith("Row volume")]
        assert len(volume) == 1, r.required_disclosures
        assert "scan__week_start from 2025-01-27 to 2025-02-02 is 79%" in volume[0]
        # The number is untouched: the low week is in the rows as returned.
        assert any(21625.0 in row for row in r.result.rows)

        # A window that misses the week carries no volume disclosure.
        clean = service.query_metrics(
            service.sessions.get("v"),
            {
                **_OUTAGE_WEEKS,
                "time": {"grain": "week", "start": "2025-02-03", "end": "2025-03-02"},
            },
        )
        assert not [d for d in clean.required_disclosures if d.startswith("Row volume")]
    finally:
        service.close()


@pytest.mark.fake_db
@pytest.mark.metricflow
def test_volume_check_is_a_tenant_switch(bristlecone):
    cfg = bristlecone.model_copy(
        update={"checks": bristlecone.checks.model_copy(update={"volume": False})}
    )
    service = _service(cfg)
    try:
        r = service.query_metrics(service.sessions.get("v"), _OUTAGE_WEEKS)
        assert r.status == "resolved"
        assert not [d for d in r.required_disclosures if d.startswith("Row volume")]
    finally:
        service.close()
