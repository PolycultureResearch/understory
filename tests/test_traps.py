"""Traps registry: the request flows from design section 13, plus the CI check."""

from __future__ import annotations

from datetime import date

import pytest
from typer.testing import CliRunner

from understory.catalog.manifest import load_catalog
from understory.tenant import load_tenant, tenants_dir
from understory.traps import WINDOWS, Registry, check, check_registry, load_registry
from understory.types import (
    Catalog,
    ClarificationChoice,
    DimensionInfo,
    MetricInfo,
    MetricSpec,
    TimeSpec,
    WhereClause,
)

TENANTS = ["alpenglow", "white_cube", "meridian", "bristlecone"]
LAST_QUARTER = TimeSpec(grain="month", start=date(2026, 4, 1), end=date(2026, 6, 30))


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    return load_catalog(tenants_dir() / "alpenglow" / "semantic_manifest.json")


@pytest.fixture(scope="module")
def registry() -> Registry:
    return load_registry(tenants_dir() / "alpenglow" / "traps.yml")


def _metric(name: str, label: str | None = None, synonyms: list[str] | None = None) -> MetricInfo:
    return MetricInfo(name=name, label=label, type="simple", synonyms=synonyms or [])


def _dim(name: str) -> DimensionInfo:
    return DimensionInfo(
        name=name, type="categorical", semantic_model="m", relation="r", column=name
    )


def _tiny_catalog(**metrics: MetricInfo) -> Catalog:
    return Catalog(metrics=metrics, dimensions={"d__x": _dim("d__x"), "e__x": _dim("e__x")})


# --------------------------------------------------------------------------- #
# Request flows
# --------------------------------------------------------------------------- #


def test_covered_question(registry, catalog):
    spec = MetricSpec(
        metrics=["net_revenue"],
        group_by=["order_item__category"],
        time=TimeSpec(grain="month", start=date(2026, 3, 1)),
        question="Net revenue by category over the last six months",
    )
    out = check(spec, registry, catalog, max_clarifications=3)
    assert out.refusal is None
    assert out.clarifications == []
    assert out.fired == ["convention:time_anchor"]
    assert [d.source for d in out.disclosures] == ["convention:time_anchor"]
    assert out.spec.metrics == ["net_revenue"]


def test_named_candidate_without_phrase_does_not_fire(registry, catalog):
    spec = MetricSpec(metrics=["gross_revenue"], time=LAST_QUARTER)
    out = check(spec, registry, catalog, max_clarifications=3)
    assert out.fired == []
    assert out.clarifications == [] and out.disclosures == []


def test_ambiguous_question(registry, catalog):
    spec = MetricSpec(
        metrics=["gross_revenue"],
        where=[WhereClause(dimension="customer__country", op="in", values=["US"])],
        time=LAST_QUARTER,
        question="How were sales in the West last quarter?",
    )
    out = check(spec, registry, catalog, max_clarifications=3)
    assert out.refusal is None
    assert out.fired == ["collision:revenue", "dimension_role:region"]

    [clar] = out.clarifications
    assert clar.trap == "collision:revenue"
    assert clar.slot == "metric"
    assert clar.phrase == "sales"
    assert [o.id for o in clar.options] == ["net_revenue", "gross_revenue"]
    assert [o.label for o in clar.options] == ["Net Revenue", "Gross Revenue"]
    assert clar.options[0].hint == "After discounts and refunds. Used in board reporting."

    assert out.spec.where[0].dimension == "order__country"
    assert out.spec.where[0].values == ["US"]
    [disc] = out.disclosures
    assert disc.source == "dimension_role:region"
    assert disc.text == "Country is where the order shipped, not where the customer lives."


@pytest.mark.parametrize("trap_ref", ["collision:revenue", "revenue"])
def test_clarified_question_resolves(registry, catalog, trap_ref):
    spec = MetricSpec(
        metrics=["gross_revenue"],
        where=[WhereClause(dimension="customer__country", op="in", values=["US"])],
        time=LAST_QUARTER,
        question="How were sales in the West last quarter?",
        clarifications=[ClarificationChoice(trap=trap_ref, choice="net_revenue")],
    )
    out = check(spec, registry, catalog, max_clarifications=3)
    assert out.refusal is None
    assert out.clarifications == []
    assert out.spec.metrics == ["net_revenue"]
    assert out.fired == ["collision:revenue", "dimension_role:region"]
    assert {d.source for d in out.disclosures} == {"collision:revenue", "dimension_role:region"}


def test_invalid_choice_asks_again(registry, catalog):
    spec = MetricSpec(
        metrics=["gross_revenue"],
        time=LAST_QUARTER,
        question="sales last quarter",
        clarifications=[ClarificationChoice(trap="collision:revenue", choice="orders")],
    )
    out = check(spec, registry, catalog, max_clarifications=3)
    assert [c.trap for c in out.clarifications] == ["collision:revenue"]
    assert out.spec.metrics == ["gross_revenue"]


def test_unanswerable(registry, catalog):
    spec = MetricSpec(metrics=["gross_margin"], question="What is our profit by SKU?")
    out = check(spec, registry, catalog, max_clarifications=3)
    assert out.refusal is not None
    assert out.refusal.reason == "unanswerable"
    assert out.refusal.phrase == "profit by sku"
    assert out.refusal.message.startswith("COGS is only available at category grain")
    assert out.fired == ["unanswerable:profit_by_sku"]
    assert out.clarifications == []


def test_default_window_asks_when_time_absent(registry, catalog):
    spec = MetricSpec(metrics=["net_revenue"])
    out = check(spec, registry, catalog, max_clarifications=3)
    [clar] = out.clarifications
    assert clar.trap == "convention:default_window"
    assert clar.slot == "convention"
    assert [o.id for o in clar.options] == list(WINDOWS)
    assert [d.source for d in out.disclosures] == ["convention:time_anchor"]
    assert out.fired == ["convention:time_anchor", "convention:default_window"]


def test_default_window_choice_is_disclosed_not_resolved(registry, catalog):
    spec = MetricSpec(
        metrics=["net_revenue"],
        clarifications=[
            ClarificationChoice(trap="convention:default_window", choice="last_quarter")
        ],
    )
    out = check(spec, registry, catalog, max_clarifications=3)
    assert out.clarifications == []
    assert out.spec.time == TimeSpec()
    texts = {d.source: d.text for d in out.disclosures}
    assert "Last full quarter" in texts["convention:default_window"]


def test_default_window_disclose_policy():
    registry = Registry.model_validate(
        {
            "conventions": [
                {"name": "default_window", "value": "trailing_90_days", "policy": "disclose"}
            ]
        }
    )
    out = check(MetricSpec(metrics=["a"]), registry, _tiny_catalog(), max_clarifications=3)
    assert out.clarifications == []
    [disc] = out.disclosures
    assert disc.source == "convention:default_window"
    assert "Trailing 90 days" in disc.text


def test_too_broad_sorted_by_priority_then_id():
    registry = Registry.model_validate(
        {
            "collisions": [
                {"phrase": ["alpha"], "candidates": ["a", "b"], "priority": 50},
                {"phrase": ["beta"], "candidates": ["a", "b"]},
                {"phrase": ["gamma"], "candidates": ["a", "b"], "priority": 10},
            ]
        }
    )
    catalog = _tiny_catalog(a=_metric("a"), b=_metric("b"))
    spec = MetricSpec(metrics=["c"], time=LAST_QUARTER, question="beta and alpha and gamma")
    out = check(spec, registry, catalog, max_clarifications=3)
    assert out.refusal is None
    assert [c.trap for c in out.clarifications] == [
        "collision:gamma",
        "collision:alpha",
        "collision:beta",
    ]

    out = check(spec, registry, catalog, max_clarifications=2)
    assert out.refusal is not None
    assert out.refusal.reason == "too_broad"
    assert out.refusal.suggestions == ["gamma", "alpha", "beta"]
    assert len(out.fired) == 3


def test_prefer_collision_substitutes_and_discloses():
    registry = Registry.model_validate(
        {
            "collisions": [
                {
                    "phrase": ["mrr"],
                    "candidates": ["mrr", "new_mrr"],
                    "policy": "prefer mrr",
                    "hint": {"mrr": "Active MRR."},
                }
            ]
        }
    )
    catalog = _tiny_catalog(mrr=_metric("mrr", "MRR"), new_mrr=_metric("new_mrr", "New MRR"))

    spec = MetricSpec(metrics=["new_mrr"], time=LAST_QUARTER, question="how is mrr trending")
    out = check(spec, registry, catalog, max_clarifications=3)
    assert out.spec.metrics == ["mrr"]
    assert out.fired == ["collision:mrr"]
    assert out.disclosures[0].text == "'mrr' is read as MRR. Active MRR."

    # The candidate's own label in the question does not trip the phrase.
    spec = MetricSpec(metrics=["new_mrr"], time=LAST_QUARTER, question="new mrr by month")
    out = check(spec, registry, catalog, max_clarifications=3)
    assert out.fired == []
    assert out.spec.metrics == ["new_mrr"]


def test_ask_dimension_role_with_choice():
    registry = Registry.model_validate(
        {"dimension_roles": [{"phrase": ["x"], "candidates": ["d__x", "e__x"], "policy": "ask"}]}
    )
    catalog = _tiny_catalog(a=_metric("a"))
    spec = MetricSpec(metrics=["a"], group_by=["d__x"], time=LAST_QUARTER, question="a by x")
    out = check(spec, registry, catalog, max_clarifications=3)
    assert [c.slot for c in out.clarifications] == ["dimension"]

    spec.clarifications = [ClarificationChoice(trap="dimension_role:x", choice="e__x")]
    out = check(spec, registry, catalog, max_clarifications=3)
    assert out.clarifications == []
    assert out.spec.group_by == ["e__x"]


# --------------------------------------------------------------------------- #
# Schema and loading
# --------------------------------------------------------------------------- #


def test_load_missing_and_empty(tmp_path):
    assert load_registry(tmp_path / "nope.yml").is_empty()
    empty = tmp_path / "traps.yml"
    empty.write_text("")
    assert load_registry(empty).is_empty()


def test_ids_and_policy_validation():
    reg = Registry.model_validate(
        {
            "collisions": [
                {"phrase": "top line", "candidates": ["a", "b"]},
                {"id": "custom", "phrase": ["x"], "candidates": ["a", "b"], "policy": "prefer  a"},
            ],
            "unanswerable": [{"phrase": ["profit by SKU"], "reason": "no"}],
        }
    )
    assert reg.trap_ids() == ["collision:top_line", "custom", "unanswerable:profit_by_sku"]
    assert reg.collisions[1].preferred == "a"
    with pytest.raises(ValueError):
        Registry.model_validate(
            {"collisions": [{"phrase": ["x"], "candidates": ["a"], "policy": "apply a"}]}
        )
    with pytest.raises(ValueError):
        Registry.model_validate(
            {
                "collisions": [
                    {"phrase": ["x"], "candidates": ["a"]},
                    {"phrase": ["x"], "candidates": ["b"]},
                ]
            }
        )


# --------------------------------------------------------------------------- #
# CI check
# --------------------------------------------------------------------------- #


def test_check_registry_bad_candidate_and_prefer_target():
    registry = Registry.model_validate(
        {
            "collisions": [
                {"phrase": ["r"], "candidates": ["a", "nope"], "policy": "prefer zzz"},
            ],
            "dimension_roles": [{"phrase": ["c"], "candidates": ["d__x", "d__missing"]}],
        }
    )
    problems = check_registry(registry, _tiny_catalog(a=_metric("a")))
    assert any("'nope'" in p and "collision:r" in p for p in problems)
    assert any("'zzz'" in p for p in problems)
    assert any("'d__missing'" in p for p in problems)


def test_check_registry_unadjudicated_synonym():
    catalog = _tiny_catalog(
        net=_metric("net", synonyms=["revenue", "sales"]),
        gross=_metric("gross", synonyms=["Revenue"]),
    )
    [problem] = check_registry(Registry(), catalog)
    assert "'revenue'" in problem and "gross" in problem and "net" in problem

    covered = Registry.model_validate(
        {"collisions": [{"phrase": ["revenue"], "candidates": ["net", "gross"]}]}
    )
    assert check_registry(covered, catalog) == []


def test_check_registry_duplicate_phrase_and_bad_convention():
    registry = Registry.model_validate(
        {
            "collisions": [{"phrase": ["Sales"], "candidates": ["a"]}],
            "unanswerable": [{"phrase": ["sales"], "reason": "x"}],
            "conventions": [
                {"name": "default_window", "value": "forever"},
                {"name": "time_anchor", "value": "x", "policy": "ask_if_absent"},
            ],
        }
    )
    problems = check_registry(registry, _tiny_catalog(a=_metric("a")))
    assert any("appears in both" in p for p in problems)
    assert any("'forever'" in p for p in problems)
    assert any("time_anchor cannot be asked" in p for p in problems)


@pytest.mark.parametrize("name", TENANTS)
def test_every_tenant_registry_is_clean(name):
    cfg = load_tenant(tenants_dir() / name)
    registry = load_registry(cfg.traps_path)
    assert not registry.is_empty(), name
    catalog = load_catalog(cfg.manifest_path)
    assert check_registry(registry, catalog) == []


def test_cli_check_command():
    from understory.cli import app

    result = CliRunner().invoke(app, ["traps", "check", str(tenants_dir() / "alpenglow")])
    assert result.exit_code == 0, result.output
    assert "clean" in result.output
