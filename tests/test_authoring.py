"""Golden authoring: drafting from the catalog and the traps registry, appending, verifying."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from understory.harness.authoring import append_items, draft_golden, last_full_month, set_verified
from understory.harness.golden import load_golden
from understory.harness.realistic import dump_items
from understory.traps.schema import Registry
from understory.types import Catalog, DimensionInfo, MetricInfo


def _dim(name: str, model: str, column: str, type_: str = "categorical") -> DimensionInfo:
    return DimensionInfo(
        name=name, type=type_, semantic_model=model, relation=f"m.fct_{model}", column=column
    )


def _metric(name: str, label: str, dims: list[str], type_: str = "simple") -> MetricInfo:
    return MetricInfo(
        name=name,
        label=label,
        type=type_,
        dimensions=[*dims, "order__order_date", "metric_time"],
        time_dimension="order__order_date",
    )


def _catalog() -> Catalog:
    dims = {
        "metric_time": _dim("metric_time", "orders", "order_date", "time"),
        "order__order_date": _dim("order__order_date", "orders", "order_date", "time"),
        "order__country": _dim("order__country", "orders", "country"),
        "customer__country": _dim("customer__country", "customers", "country"),
        "order__device": _dim("order__device", "orders", "device"),
    }
    metrics = {
        "net_revenue": _metric(
            "net_revenue", "Net Revenue", ["order__country", "customer__country"]
        ),
        "gross_revenue": _metric("gross_revenue", "Gross Revenue", ["order__country"]),
        "revenue": _metric("revenue", "Revenue", ["order__device"]),
        "wau": _metric("wau", "WAU", ["order__device"], type_="cumulative"),
    }
    return Catalog(metrics=metrics, dimensions=dims)


def _registry() -> Registry:
    return Registry.model_validate(
        {
            "collisions": [
                {
                    "phrase": ["revenue", "sales"],
                    "candidates": ["net_revenue", "gross_revenue", "revenue"],
                    "policy": "ask",
                    "why": "Three revenues.",
                },
                {
                    "phrase": ["turnover"],
                    "candidates": ["net_revenue", "gross_revenue"],
                    "policy": "prefer net_revenue",
                },
            ],
            "dimension_roles": [
                {
                    "phrase": ["region", "country"],
                    "candidates": ["customer__country", "order__country"],
                    "policy": "prefer order__country",
                    "disclose": "Country is where the order shipped.",
                }
            ],
            "conventions": [
                {"name": "time_anchor", "value": "latest_available_date"},
                {"name": "default_window", "value": "trailing_30_days"},
            ],
            "unanswerable": [
                {"phrase": ["ltv"], "reason": "No LTV model yet. Ask for repeat share."}
            ],
        }
    )


WINDOW = (date(2024, 6, 1), date(2026, 5, 31))


def test_last_full_month():
    assert last_full_month(WINDOW) == date(2026, 5, 1)
    assert last_full_month((date(2024, 6, 1), date(2026, 5, 30))) == date(2026, 4, 1)


def test_draft_covers_the_catalog_and_the_registry():
    items = {i.id: i for i in draft_golden(_catalog(), _registry(), window=WINDOW)}

    # One single-month item per metric, one grouped by its first categorical dimension.
    assert items["net_revenue_2026_05"].question == "What was net revenue in May 2026?"
    assert items["net_revenue_2026_05"].kind == "catalog"
    assert items["net_revenue_2026_05"].source == "catalog:net_revenue"
    assert items["net_revenue_2026_05"].spec == {
        "metrics": ["net_revenue"],
        "time": {"start": date(2026, 5, 1), "end": date(2026, 5, 31)},
    }
    assert items["net_revenue_by_country_2026_05"].spec["group_by"] == ["order__country"]
    assert items["net_revenue_by_country_2026_05"].question == (
        "Net revenue by order country for May 2026"
    )

    # A cumulative metric is drafted as a weekly series, never a bare total.
    assert "wau_2026_05" not in items
    wau = items["wau_by_week_2026_05"]
    assert wau.spec["group_by"] == ["metric_time"] and wau.spec["time"]["grain"] == "week"

    # The ask: the spec names the first candidate and the item answers with it.
    ask = items["sales_ask_2026_05"]
    assert ask.kind == "trap" and ask.source == "trap:collision:revenue"
    assert ask.expected.status == "needs_clarification"
    assert ask.expected.clarification_traps == ["collision:revenue"]
    assert ask.expected.answers == {"collision:revenue": "net_revenue"}
    assert ask.expected.metrics == ["net_revenue"]

    # The prefer: the spec names the other candidate; the disclosure names the preferred.
    prefer = items["turnover_prefer_2026_05"]
    assert prefer.spec["metrics"] == ["gross_revenue"]
    assert prefer.expected.status == "resolved"
    assert prefer.expected.metrics == ["net_revenue"]
    assert prefer.expected.disclosures == ["Net Revenue"]

    # The role: a metric carrying both candidates, the other one named, the text disclosed.
    role = items["region_prefer_2026_05"]
    assert role.spec == {
        "metrics": ["net_revenue"],
        "group_by": ["customer__country"],
        "time": {"start": date(2026, 5, 1), "end": date(2026, 5, 31)},
    }
    assert role.expected.disclosures == ["Country is where the order shipped."]

    # Unanswerable and the two conventions.
    assert items["ltv_unanswerable"].expected.status == "unanswerable"
    assert items["ltv_unanswerable"].expected.disclosures == ["No LTV model yet"]
    assert items["net_revenue_no_window"].spec == {"metrics": ["net_revenue"]}
    assert items["net_revenue_no_window"].expected.disclosures == ["trailing 30 days"]
    assert items["net_revenue_open_end"].spec["time"] == {"start": date(2026, 5, 1)}
    assert items["net_revenue_open_end"].expected.disclosures == ["anchored"]


def test_draft_picks_a_phrase_no_candidate_covers():
    """'revenue' sits inside 'Gross Revenue', so it would never fire; 'sales' does."""
    items = {i.id for i in draft_golden(_catalog(), _registry(), window=WINDOW)}
    assert "sales_ask_2026_05" in items and "revenue_ask_2026_05" not in items


def test_draft_expects_the_ask_a_metric_name_cannot_avoid():
    """A metric that is itself an ask phrase drafts as the ask, answered with the metric."""
    items = {i.id: i for i in draft_golden(_catalog(), _registry(), window=WINDOW)}
    item = items["revenue_2026_05"]
    assert item.kind == "catalog"
    assert item.expected.status == "needs_clarification"
    assert item.expected.answers == {"collision:revenue": "revenue"}
    assert item.expected.metrics == ["revenue"]
    assert item.notes and "asks on 'revenue'" in item.notes
    # And a plain metric stays resolved.
    assert items["net_revenue_2026_05"].expected.status == "resolved"


def test_draft_bounded_role_expects_the_why():
    """No metric carries the preferred country beside another: the prefer stays and says why."""
    catalog = _catalog()
    catalog.metrics["net_revenue"].dimensions.remove("customer__country")
    items = {i.id: i for i in draft_golden(catalog, _registry(), window=WINDOW)}
    role = items["region_prefer_2026_05"]
    assert role.spec["group_by"] == ["customer__country"]
    assert role.expected.disclosures == ["does not carry"]


def test_draft_without_dimension_items():
    items = draft_golden(_catalog(), _registry(), window=WINDOW, by_dimension=False)
    grouped = [i.id for i in items if i.kind == "catalog" and i.spec.get("group_by")]
    assert grouped == ["wau_by_week_2026_05"], "only the cumulative series keeps a group_by"


def test_append_adds_only_new_ids(tmp_path: Path):
    items = draft_golden(_catalog(), _registry(), window=WINDOW)
    path = tmp_path / "questions.yml"
    path.write_text("# hand-written\n" + dump_items(items[:2]))
    added = append_items(path, items[:4])
    assert [i.id for i in added] == [i.id for i in items[2:4]]
    loaded = load_golden(path)
    assert [i.id for i in loaded] == [i.id for i in items[:4]]
    assert path.read_text().startswith("# hand-written\n")
    assert append_items(path, items[:4]) == []


def test_set_verified_round_trip(tmp_path: Path):
    items = draft_golden(_catalog(), _registry(), window=WINDOW)
    path = tmp_path / "questions.yml"
    path.write_text(dump_items(items))
    before = path.read_text()
    first, second = items[0].id, items[1].id

    assert set_verified(path, [first, second, "nope"]) == [first, second]
    loaded = {i.id: i for i in load_golden(path)}
    assert loaded[first].verified and loaded[second].verified
    assert not loaded[items[2].id].verified
    # The flag sits where dump_items would put it, so the file round-trips.
    assert path.read_text() == dump_items(load_golden(path))

    assert set_verified(path, [first, second], value=False) == [first, second]
    assert path.read_text() == before


# --------------------------------------------------------------------------- #
# Through the real tenants.
# --------------------------------------------------------------------------- #


@pytest.mark.fake_db
@pytest.mark.metricflow
@pytest.mark.slow
def test_drafted_set_fills_and_passes(alpenglow_db, tmp_path: Path):
    """Draft, fill, and run the deterministic eval on a scratch copy.

    One tenant here, because the four together compile some two hundred specs
    through MetricFlow. All four passed by hand on 2026-09-22
    (`knowledge/golden-authoring-2026-09-22.md`).
    """
    tenant = alpenglow_db
    from understory.harness.deterministic import run_deterministic
    from understory.harness.realistic import data_window, fill_numbers, patch_expected, write_items
    from understory.server.service import Service
    from understory.telemetry import TelemetryWriter
    from understory.tenant import LogConfig

    log = LogConfig(events_prefix="unused/events", text_prefix="unused/text", enabled=False)
    service = Service(tenant, telemetry=TelemetryWriter(log, tenant.name))
    try:
        window = data_window(service.catalog, service.warehouse)
        items = draft_golden(service.catalog, service.registry, window=window)
        path = write_items(tmp_path / "questions.yml", items)
        observations = fill_numbers(service, items)
        assert not [o.id for o in observations if o.mismatch], tenant.name
        patch_expected(path, items)
        report = run_deterministic(service, load_golden(path))
    finally:
        service.close()
    failures = [(i.id, i.reasons) for i in report.items if not i.passed]
    assert not failures, f"{tenant.name}: {failures}"
    kinds = report.summary()["by_kind"]
    assert set(kinds) == {"trap", "catalog"}, kinds
    assert kinds["catalog"]["items"] >= 2 * len(service.catalog.metrics) - 2
