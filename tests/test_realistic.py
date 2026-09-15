"""The realistic set: drafting from ground truth, the snapshot, and the files.

1. Drafting is pure: a fake catalog and a list of events in, golden items out.
2. The file round trip keeps the hand-edited layout and the new item fields.
3. The four tenants' realistic sets load, carry a kind, and (slow) run through
   the service with every recorded number still true.
"""

from __future__ import annotations

import textwrap
from datetime import date

import pytest

from understory.harness.golden import GoldenItem, load_golden
from understory.harness.realistic import (
    GroundTruthEvent,
    Observation,
    draft_realistic,
    dump_items,
    patch_expected,
    read_ground_truth,
)
from understory.tenant import TenantConfig
from understory.types import Catalog, Column, DimensionInfo, MetricInfo, Result

TENANTS = ["alpenglow", "white_cube", "meridian", "bristlecone"]
KINDS = {"event", "quiet", "over_refusal"}


# --------------------------------------------------------------------------- #
# 1. Drafting
# --------------------------------------------------------------------------- #


def _dim(name: str, model: str, column: str, type_: str = "categorical") -> DimensionInfo:
    return DimensionInfo(
        name=name,
        type=type_,
        semantic_model=model,
        relation=f"main_marts.fct_{model}",
        column=column,
    )


def _catalog() -> Catalog:
    dims = {
        "metric_time": _dim("metric_time", "orders", "order_date", "time"),
        "order__order_date": _dim("order__order_date", "orders", "order_date", "time"),
        "order__device": _dim("order__device", "orders", "device"),
        "spend__spend_date": _dim("spend__spend_date", "spend", "spend_date", "time"),
        "spend__channel": _dim("spend__channel", "spend", "channel"),
    }
    metrics = {
        "marketing_spend": MetricInfo(
            name="marketing_spend",
            label="Marketing Spend",
            type="simple",
            dimensions=["spend__channel", "spend__spend_date", "metric_time"],
            time_dimension="spend__spend_date",
        ),
        "orders": MetricInfo(
            name="orders",
            label="Orders",
            type="simple",
            dimensions=["order__device", "order__order_date", "metric_time"],
            time_dimension="order__order_date",
        ),
        "net_revenue": MetricInfo(
            name="net_revenue",
            label="Net Revenue",
            type="derived",
            dimensions=["order__device", "order__order_date", "metric_time"],
            time_dimension="order__order_date",
        ),
    }
    return Catalog(metrics=metrics, dimensions=dims)


def _event(**kw) -> GroundTruthEvent:
    base = dict(
        id="budget_cut",
        kind="rate",
        type="level_shift",
        target="spend.paid_social",
        start=date(2025, 2, 3),
        end=date(2025, 2, 23),
        magnitude=0.5,
        affected_metrics=("marketing_spend", "orders", "net_revenue"),
    )
    base.update(kw)
    return GroundTruthEvent(**base)


def test_event_month_is_the_month_with_most_days():
    spanning = _event(start=date(2024, 10, 25), end=date(2024, 11, 20))
    assert spanning.month == date(2024, 11, 1)
    assert _event().month == date(2025, 2, 1)


def test_draft_makes_a_by_month_item_and_a_tail_item_per_event():
    items = draft_realistic(
        _catalog(), [_event()], window=(date(2024, 6, 1), date(2026, 5, 31)), quiet_items=0
    )
    by_id = {i.id: i for i in items}
    head = by_id["marketing_spend_by_month_2025_02"]
    assert head.kind == "event"
    assert head.source == "ground_truth:budget_cut"
    assert head.spec == {
        "metrics": ["marketing_spend"],
        "group_by": ["metric_time"],
        "time": {"grain": "month", "start": date(2025, 1, 1), "end": date(2025, 3, 31)},
    }
    assert "January through March 2025" in head.question
    tail = by_id["net_revenue_2025_02"]
    assert tail.spec["metrics"] == ["net_revenue"]
    assert tail.spec["time"] == {"start": date(2025, 2, 1), "end": date(2025, 2, 28)}
    assert not head.expected.numbers, "the draft carries no numbers; fill does"


def test_draft_adds_a_filtered_item_when_the_event_names_a_segment():
    e = _event(
        id="mobile_bug",
        target="orders.mobile",
        segment={"device": "mobile"},
        affected_metrics=("orders",),
    )
    items = draft_realistic(
        _catalog(), [e], window=(date(2024, 6, 1), date(2026, 5, 31)), quiet_items=0
    )
    seg = next(i for i in items if i.id == "orders_mobile_2025_02")
    assert seg.spec["where"] == [{"dimension": "order__device", "op": "eq", "values": ["mobile"]}]


def test_draft_skips_events_outside_the_full_months_and_dq_events():
    too_late = _event(id="late", start=date(2026, 6, 2), end=date(2026, 6, 10))
    dq = _event(id="dupes", kind="dq", type="duplicate_rows", affected_metrics=())
    items = draft_realistic(
        _catalog(),
        [too_late, dq],
        window=(date(2024, 6, 1), date(2026, 5, 31)),
        quiet_items=0,
    )
    assert items == []


def test_draft_pads_with_quiet_months_that_no_event_touches():
    items = draft_realistic(
        _catalog(), [_event()], window=(date(2024, 6, 1), date(2026, 5, 31)), quiet_items=4
    )
    quiet = [i for i in items if i.kind == "quiet"]
    assert len(quiet) == 4
    busy = {date(2025, 1, 1), date(2025, 2, 1), date(2025, 3, 1)}
    for item in quiet:
        assert item.spec["time"]["start"] not in busy
        assert item.source == "quiet_month"
    ids = [i.id for i in items]
    assert len(ids) == len(set(ids)), "ids are unique"


class _Warehouse:
    name = "duckdb"
    dialect = "duckdb"

    def __init__(self, rows):
        self.rows = rows

    def run(self, sql, *, timeout_s, row_cap):
        cols = [
            "id", "kind", "type", "target", "segment", "start_date", "end_date",
            "magnitude", "affected_metrics",
        ]  # fmt: skip
        return Result(
            columns=[Column(name=c, type="VARCHAR") for c in cols],
            rows=self.rows,
            row_count=len(self.rows),
        )


def test_read_ground_truth_parses_json_columns():
    rows = [
        [
            "cut",
            "rate",
            "level_shift",
            "spend.paid_social",
            '{"device": "mobile"}',
            "2025-02-03",
            date(2025, 2, 23),
            0.5,
            '["marketing_spend", "orders"]',
        ]  # fmt: skip
    ]
    events = read_ground_truth(_Warehouse(rows))
    assert events == [
        GroundTruthEvent(
            id="cut",
            kind="rate",
            type="level_shift",
            target="spend.paid_social",
            start=date(2025, 2, 3),
            end=date(2025, 2, 23),
            magnitude=0.5,
            affected_metrics=("marketing_spend", "orders"),
            segment={"device": "mobile"},
        )
    ]


class _Broken:
    name = "duckdb"
    dialect = "duckdb"

    def run(self, sql, *, timeout_s, row_cap):
        raise RuntimeError("no such relation")


def test_read_ground_truth_is_empty_without_the_relation():
    assert read_ground_truth(_Broken()) == []


# --------------------------------------------------------------------------- #
# 2. The file
# --------------------------------------------------------------------------- #


def test_dump_and_load_round_trip(tmp_path):
    items = draft_realistic(
        _catalog(),
        [_event(segment={"device": "mobile"}, affected_metrics=("orders",))],
        window=(date(2024, 6, 1), date(2026, 5, 31)),
        quiet_items=1,
    )
    path = tmp_path / "realistic.yml"
    path.write_text(dump_items(items, header="A test set.\nTwo lines."))
    text = path.read_text()
    assert text.startswith("# A test set.\n# Two lines.\nversion: 1\n")
    loaded = load_golden(path)
    assert [i.id for i in loaded] == [i.id for i in items]
    for a, b in zip(loaded, items, strict=True):
        assert a.kind == b.kind and a.source == b.source and a.spec == b.spec
        assert a.verified is False


def test_golden_item_rejects_an_unknown_kind():
    with pytest.raises(ValueError):
        GoldenItem(id="x", question="q", kind="weird")


_HAND_EDITED = textwrap.dedent(
    """\
    # Header kept.
    version: 1

    questions:
      # -- event: a section comment that must survive
      - id: sales_2025_02
        question: "Did sales drop in February 2025?"
        kind: event
        notes: >
          A note.
        spec:
          metrics: [net_revenue]
          time: {start: 2025-02-01, end: 2025-02-28}
        expected:
          status: resolved
          disclosures: ["net revenue"]
          numbers: [1.0]

      - id: untouched
        question: "Left alone"
        spec:
          metrics: [orders]
        expected:
          status: invalid
    """
)


def test_patch_expected_edits_in_place_and_keeps_comments(tmp_path):
    path = tmp_path / "realistic.yml"
    path.write_text(_HAND_EDITED)
    items = load_golden(path)
    items[0].expected.metrics = ["net_revenue"]
    items[0].expected.numbers = [951335.65]
    patch_expected(path, items)
    text = path.read_text()
    assert "# -- event: a section comment that must survive" in text
    assert "# Header kept." in text
    assert "      A note." in text
    block = text.split("- id: sales_2025_02")[1].split("- id: untouched")[0]
    expected_lines = [ln.strip() for ln in block.split("expected:")[1].strip().splitlines()]
    assert expected_lines == [
        "status: resolved",
        "metrics: [net_revenue]",
        'disclosures: ["net revenue"]',
        "numbers: [951335.65]",
    ]
    reloaded = load_golden(path)
    assert reloaded[0].expected.numbers == [951335.65]
    assert reloaded[1].expected.status == "invalid"
    assert reloaded[1].expected.numbers == []


def test_observation_defaults():
    obs = Observation(
        id="x", status="resolved", metrics=[], numbers=[], disclosures=[], clarifications=[]
    )
    assert obs.mismatch is False and obs.error is None


# --------------------------------------------------------------------------- #
# 3. The tenants' sets
# --------------------------------------------------------------------------- #


def test_golden_set_path(alpenglow: TenantConfig):
    assert alpenglow.golden_set_path("trap") == alpenglow.golden_path
    assert alpenglow.golden_set_path("realistic") == alpenglow.realistic_path
    assert alpenglow.realistic_path.name == "realistic.yml"
    with pytest.raises(ValueError):
        alpenglow.golden_set_path("other")


def test_realistic_sets_load(tenants):
    for name in TENANTS:
        cfg = tenants[name]
        items = load_golden(cfg.realistic_path)
        assert 15 <= len(items) <= 30, f"{name}: {len(items)} items"
        trap_ids = {i.id for i in load_golden(cfg.golden_path)}
        kinds = {i.kind for i in items}
        assert KINDS <= kinds, f"{name}: kinds {kinds}"
        unfilled: list[str] = []
        for item in items:
            assert item.kind in KINDS, f"{name}/{item.id}: kind {item.kind}"
            assert item.source, f"{name}/{item.id} has no source"
            assert item.spec is not None, f"{name}/{item.id} has no spec"
            assert item.id not in trap_ids, f"{name}/{item.id} duplicates a trap item"
            if item.expected.resolves and not item.expected.numbers:
                # An unfilled item is open server work and must say so.
                assert item.notes, f"{name}/{item.id} resolves, has no numbers and no note"
                unfilled.append(item.id)
            if item.expected.status == "needs_clarification":
                assert item.expected.answers, f"{name}/{item.id} asks but records no answer"
        assert len(unfilled) <= len(items) // 5, f"{name}: too many unfilled: {unfilled}"
        over = [i for i in items if i.kind == "over_refusal"]
        assert len(over) >= 4, f"{name}: only {len(over)} over-refusal items"


@pytest.mark.fake_db
@pytest.mark.metricflow
@pytest.mark.slow
def test_realistic_numbers_still_hold(tenant, tmp_path):
    """Every snapshotted number is still what the service returns.

    Status is not asserted at 100%: the set records what a correct run does,
    and some items are open server work (see the realistic report). What must
    never drift silently is a number or a disclosure on an item that resolves.
    """
    from understory.harness.deterministic import run_deterministic
    from understory.server.service import Service
    from understory.telemetry import TelemetryWriter
    from understory.tenant import LogConfig

    log = LogConfig(events_prefix="unused/events", text_prefix="unused/text", enabled=False)
    service = Service(tenant, telemetry=TelemetryWriter(log, tenant.name))
    try:
        items = load_golden(tenant.realistic_path)
        report = run_deterministic(service, items)
    finally:
        service.close()

    errors = [i.id for i in report.items if i.observed_status == "error"]
    assert not errors, f"{tenant.name}: {errors}"
    drifted = [
        (i.id, i.reasons) for i in report.items if i.answer is False or i.disclosure is False
    ]
    assert not drifted, f"{tenant.name}: {drifted}"
    assert report.summary()["pass_rate"] >= 0.8, report.markdown()
