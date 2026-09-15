"""Traps registry: the request flows from design section 13, plus the CI check."""

from __future__ import annotations

from datetime import date

import pytest
from typer.testing import CliRunner

from understory.catalog.manifest import load_catalog
from understory.tenant import load_tenant, tenants_dir
from understory.traps import Registry, check, check_registry, load_registry
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


def _metric(
    name: str,
    label: str | None = None,
    synonyms: list[str] | None = None,
    dimensions: list[str] | None = None,
) -> MetricInfo:
    return MetricInfo(
        name=name, label=label, type="simple", synonyms=synonyms or [], dimensions=dimensions or []
    )


def _tenant(name: str) -> tuple[Registry, Catalog]:
    cfg = load_tenant(tenants_dir() / name)
    return load_registry(cfg.traps_path), load_catalog(cfg.manifest_path)


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


def test_preferred_question(registry, catalog):
    """The norm: 'sales' prefers net revenue, 'the West' prefers order country, one turn."""
    spec = MetricSpec(
        metrics=["gross_revenue"],
        where=[WhereClause(dimension="customer__country", op="in", values=["US"])],
        time=LAST_QUARTER,
        question="How were sales in the West last quarter?",
    )
    out = check(spec, registry, catalog, max_clarifications=3)
    assert out.refusal is None
    assert out.clarifications == []
    assert out.fired == ["dimension_role:region", "collision:revenue"]
    assert out.spec.metrics == ["net_revenue"]
    assert out.spec.where[0].dimension == "order__country"
    assert out.spec.where[0].values == ["US"]

    texts = {d.source: d.text for d in out.disclosures}
    assert texts["collision:revenue"] == (
        "'sales' is read as Net Revenue. After discounts and refunds. Used in board reporting. "
        "Ask for Gross Revenue (Before discounts and refunds) instead."
    )
    assert texts["dimension_role:region"] == (
        "Country is where the order shipped, not where the customer lives."
    )


def test_asked_question(registry, catalog):
    """The exception: margin is the tenant's one ask, so the server stops with options."""
    spec = MetricSpec(
        metrics=["gross_margin"],
        where=[WhereClause(dimension="customer__country", op="in", values=["US"])],
        time=LAST_QUARTER,
        question="What was our margin in the West last quarter?",
    )
    out = check(spec, registry, catalog, max_clarifications=3)
    assert out.refusal is None
    assert out.fired == ["dimension_role:region", "collision:margin"]

    [clar] = out.clarifications
    assert clar.trap == "collision:margin"
    assert clar.slot == "metric"
    assert clar.phrase == "margin"
    assert [o.id for o in clar.options] == ["gross_margin", "margin_rate"]
    assert [o.label for o in clar.options] == ["Gross Margin", "Margin Rate"]
    assert clar.options[0].hint == "Dollar margin before discounts."

    # The prefer role still applied while the ask is pending.
    assert out.spec.where[0].dimension == "order__country"
    [disc] = out.disclosures
    assert disc.source == "dimension_role:region"


@pytest.mark.parametrize("trap_ref", ["collision:margin", "margin"])
def test_clarified_question_resolves(registry, catalog, trap_ref):
    spec = MetricSpec(
        metrics=["gross_margin"],
        where=[WhereClause(dimension="customer__country", op="in", values=["US"])],
        time=LAST_QUARTER,
        question="What was our margin in the West last quarter?",
        clarifications=[ClarificationChoice(trap=trap_ref, choice="margin_rate")],
    )
    out = check(spec, registry, catalog, max_clarifications=3)
    assert out.refusal is None
    assert out.clarifications == []
    assert out.spec.metrics == ["margin_rate"]
    assert out.fired == ["dimension_role:region", "collision:margin"]
    assert {d.source for d in out.disclosures} == {"collision:margin", "dimension_role:region"}


def test_invalid_choice_asks_again(registry, catalog):
    spec = MetricSpec(
        metrics=["gross_margin"],
        time=LAST_QUARTER,
        question="margin last quarter",
        clarifications=[ClarificationChoice(trap="collision:margin", choice="orders")],
    )
    out = check(spec, registry, catalog, max_clarifications=3)
    assert [c.trap for c in out.clarifications] == ["collision:margin"]
    assert out.spec.metrics == ["gross_margin"]


def test_unanswerable(registry, catalog):
    spec = MetricSpec(metrics=["gross_margin"], question="What is our profit by SKU?")
    out = check(spec, registry, catalog, max_clarifications=3)
    assert out.refusal is not None
    assert out.refusal.reason == "unanswerable"
    assert out.refusal.phrase == "profit by sku"
    assert out.refusal.message.startswith("COGS is only available at category grain")
    assert out.fired == ["unanswerable:profit_by_sku"]
    assert out.clarifications == []


def test_default_window_is_preferred_not_asked(registry, catalog):
    spec = MetricSpec(metrics=["net_revenue"])
    out = check(spec, registry, catalog, max_clarifications=3)
    assert out.clarifications == []
    assert out.spec.time == TimeSpec()
    assert out.fired == ["convention:time_anchor", "convention:default_window"]
    texts = {d.source: d.text for d in out.disclosures}
    assert "trailing 30 days" in texts["convention:default_window"]
    assert "Name a period" in texts["convention:default_window"]


def test_default_window_silent_when_disclose_false():
    registry = Registry.model_validate(
        {"conventions": [{"name": "default_window", "value": "last_week", "disclose": False}]}
    )
    out = check(MetricSpec(metrics=["a"]), registry, _tiny_catalog(), max_clarifications=3)
    assert out.clarifications == [] and out.disclosures == [] and out.fired == []


def test_default_window_prefer_policy_explicit():
    registry = Registry.model_validate(
        {
            "conventions": [
                {"name": "default_window", "value": "trailing_90_days", "policy": "prefer"}
            ]
        }
    )
    out = check(MetricSpec(metrics=["a"]), registry, _tiny_catalog(), max_clarifications=3)
    assert out.clarifications == []
    [disc] = out.disclosures
    assert disc.source == "convention:default_window"
    assert "trailing 90 days" in disc.text


def test_convention_cannot_ask():
    for gone in ("ask_if_absent", "ask", "disclose"):
        with pytest.raises(ValueError, match="always preferred"):
            Registry.model_validate(
                {"conventions": [{"name": "default_window", "value": "last_week", "policy": gone}]}
            )


def test_too_broad_sorted_by_priority_then_id():
    registry = Registry.model_validate(
        {
            "collisions": [
                {"phrase": ["alpha"], "candidates": ["a", "b"], "policy": "ask", "priority": 50},
                {"phrase": ["beta"], "candidates": ["a", "b"], "policy": "ask"},
                {"phrase": ["gamma"], "candidates": ["a", "b"], "policy": "ask", "priority": 10},
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
    assert out.disclosures[0].text == "'mrr' is read as MRR. Active MRR. Ask for New MRR instead."

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
# A prefer is bounded by the catalog
# --------------------------------------------------------------------------- #

SALES = Registry.model_validate(
    {
        "collisions": [
            {
                "phrase": ["sales"],
                "candidates": ["net", "pos"],
                "policy": "prefer net",
                "hint": {"net": "Shipment value.", "pos": "Scanned at retail."},
            }
        ]
    }
)
SALES_CATALOG = _tiny_catalog(
    net=_metric("net", "Net Revenue", dimensions=["metric_time", "d__x"]),
    pos=_metric("pos", "POS Dollars", dimensions=["metric_time", "d__x", "e__x"]),
)


def test_prefer_skipped_when_where_needs_the_spec_metric():
    """'sales' prefers net, but the filter is on a dimension only pos carries."""
    spec = MetricSpec(
        metrics=["pos"],
        where=[WhereClause(dimension="e__x", op="eq", values=["whole_earth"])],
        time=LAST_QUARTER,
        question="How did whole earth sales look?",
    )
    out = check(spec, SALES, SALES_CATALOG, max_clarifications=3)
    assert out.spec.metrics == ["pos"]
    assert out.spec.where[0].dimension == "e__x"
    assert out.fired == ["collision:sales"]
    [disc] = out.disclosures
    assert disc.text == (
        "'sales' is read as POS Dollars because of the filter on e__x, "
        "which Net Revenue does not carry."
    )

    # The same filter on a dimension both carry lets the prefer apply as usual.
    spec.where[0].dimension = "d__x"
    out = check(spec, SALES, SALES_CATALOG, max_clarifications=3)
    assert out.spec.metrics == ["net"]
    assert out.disclosures[0].text.startswith("'sales' is read as Net Revenue. Shipment value.")


def test_prefer_skipped_when_group_by_needs_the_spec_metric():
    spec = MetricSpec(
        metrics=["pos"],
        group_by=["e__x", "metric_time__month"],
        time=LAST_QUARTER,
        question="sales by banner by month",
    )
    out = check(spec, SALES, SALES_CATALOG, max_clarifications=3)
    assert out.spec.metrics == ["pos"]
    assert out.spec.group_by == ["e__x", "metric_time__month"]
    [disc] = out.disclosures
    assert disc.text == (
        "'sales' is read as POS Dollars because of the group by e__x, "
        "which Net Revenue does not carry."
    )


def test_role_prefer_skipped_when_metric_lacks_the_preferred_dimension():
    registry = Registry.model_validate(
        {
            "dimension_roles": [
                {"phrase": ["x"], "candidates": ["d__x", "e__x"], "policy": "prefer e__x"}
            ]
        }
    )
    catalog = _tiny_catalog(
        a=_metric("a", "A", dimensions=["metric_time", "d__x"]),
        b=_metric("b", "B", dimensions=["metric_time", "d__x", "e__x"]),
    )
    spec = MetricSpec(metrics=["a"], group_by=["d__x"], time=LAST_QUARTER, question="a by x")
    out = check(spec, registry, catalog, max_clarifications=3)
    assert out.spec.group_by == ["d__x"]
    assert out.fired == ["dimension_role:x"]
    [disc] = out.disclosures
    assert disc.text == "'x' is read as d__x because A does not carry e__x."

    # A where clause is judged the same way, and a metric that carries it swaps.
    spec = MetricSpec(
        metrics=["b"],
        where=[WhereClause(dimension="d__x", op="eq", values=["pro"])],
        time=LAST_QUARTER,
        question="b on the x",
    )
    out = check(spec, registry, catalog, max_clarifications=3)
    assert out.spec.where[0].dimension == "e__x"
    assert out.disclosures[0].text.startswith("'x' is read as e__x.")


def test_roles_settle_dimensions_before_collisions_judge_them():
    """'sales in the West': the West becomes order country first, so net revenue,
    which carries order country but not customer country, can still apply."""
    registry, catalog = _tenant("alpenglow")
    spec = MetricSpec(
        metrics=["gross_revenue"],
        where=[WhereClause(dimension="customer__country", op="in", values=["US"])],
        time=LAST_QUARTER,
        question="How were sales in the West last quarter?",
    )
    out = check(spec, registry, catalog, max_clarifications=3)
    assert out.spec.metrics == ["net_revenue"]
    assert out.spec.where[0].dimension == "order__country"
    assert "customer__country" not in catalog.metrics["net_revenue"].dimensions


def test_ask_still_asks_when_the_where_fits_only_the_spec_metric():
    registry, catalog = _tenant("bristlecone")
    spec = MetricSpec(
        metrics=["pos_units"],
        where=[WhereClause(dimension="scan__retailer_banner", op="eq", values=["greenfields"])],
        time=LAST_QUARTER,
        question="What was our volume at greenfields?",
    )
    out = check(spec, registry, catalog, max_clarifications=3)
    assert [c.trap for c in out.clarifications] == ["collision:units"]
    assert out.spec.metrics == ["pos_units"]


# --------------------------------------------------------------------------- #
# A phrase inside a metric the spec names outside the candidates is covered
# --------------------------------------------------------------------------- #


def test_phrase_inside_spec_metric_label_is_covered():
    """'sales cycle' contains the revenue ask's phrase; 'deals' hits the deals prefer.
    The spec names Sales Cycle Days, which neither trap governs, so neither fires."""
    registry, catalog = _tenant("meridian")
    spec = MetricSpec(
        metrics=["sales_cycle_days"],
        group_by=["metric_time"],
        time=TimeSpec(grain="month", start=date(2025, 4, 1), end=date(2025, 7, 31)),
        question="Are deals taking longer to close? Sales cycle by month, April through July 2025.",
    )
    out = check(spec, registry, catalog, max_clarifications=3)
    assert out.clarifications == []
    assert out.fired == []
    assert out.disclosures == []
    assert out.spec.metrics == ["sales_cycle_days"]

    # A candidate alongside the covering metric still fires on its own phrase.
    spec.metrics = ["sales_cycle_days", "bookings"]
    spec.question = "Sales cycle and revenue by month"
    out = check(spec, registry, catalog, max_clarifications=3)
    assert [c.phrase for c in out.clarifications] == ["revenue"]


def test_covering_applies_to_dimension_roles_too():
    registry = Registry.model_validate(
        {
            "dimension_roles": [
                {"phrase": ["country"], "candidates": ["d__x", "e__x"], "policy": "prefer e__x"}
            ]
        }
    )
    catalog = Catalog(
        metrics={"a": _metric("a", dimensions=["f__country"])},
        dimensions={
            "d__x": _dim("d__x"),
            "e__x": _dim("e__x"),
            "f__country": _dim("f__country"),
        },
    )
    spec = MetricSpec(
        metrics=["a"], group_by=["f__country"], time=LAST_QUARTER, question="a by country"
    )
    out = check(spec, registry, catalog, max_clarifications=3)
    assert out.fired == [] and out.disclosures == []
    assert out.spec.group_by == ["f__country"]


# --------------------------------------------------------------------------- #
# A prefer disclosure describes what happened to the spec
# --------------------------------------------------------------------------- #


def test_prefer_discloses_only_what_it_did():
    registry, catalog = _tenant("meridian")

    # The spec names none of the candidates: nothing to read, nothing to say.
    spec = MetricSpec(
        metrics=["deals_lost"], time=LAST_QUARTER, question="How many deals did we lose?"
    )
    out = check(spec, registry, catalog, max_clarifications=3)
    assert out.spec.metrics == ["deals_lost"]
    assert out.fired == [] and out.disclosures == []

    # The spec already names the preferred candidate: the phrase was ambiguous,
    # so the reading is still disclosed with its alternatives.
    spec = MetricSpec(metrics=["deals_won"], time=LAST_QUARTER, question="How many deals closed?")
    out = check(spec, registry, catalog, max_clarifications=3)
    assert out.fired == ["collision:deals"]
    assert out.disclosures[0].text.startswith("'deals' is read as Deals Won.")


def test_prefer_kept_explicit_candidate_is_disclosed_as_named():
    registry, catalog = _tenant("white_cube")
    spec = MetricSpec(
        metrics=["net_new_mrr"],
        time=LAST_QUARTER,
        question="What's our net new MRR for Q1? Not total MRR, the movement.",
    )
    out = check(spec, registry, catalog, max_clarifications=3)
    assert out.spec.metrics == ["net_new_mrr"]
    [disc] = [d for d in out.disclosures if d.source == "collision:mrr"]
    assert disc.text == "'mrr' is read as Net New MRR, as named."


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
                {"phrase": "top line", "candidates": ["a", "b"], "policy": "ask", "why": "w"},
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
    with pytest.raises(ValueError, match="policy"):  # policy is always stated
        Registry.model_validate({"collisions": [{"phrase": ["x"], "candidates": ["a", "b"]}]})
    with pytest.raises(ValueError):
        Registry.model_validate(
            {
                "collisions": [
                    {"phrase": ["x"], "candidates": ["a"], "policy": "ask"},
                    {"phrase": ["x"], "candidates": ["b"], "policy": "ask"},
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
            "dimension_roles": [
                {"phrase": ["c"], "candidates": ["d__x", "d__missing"], "policy": "ask", "why": "w"}
            ],
        }
    )
    problems = check_registry(registry, _tiny_catalog(a=_metric("a")))
    assert any("'nope'" in p and "collision:r" in p for p in problems)
    assert any("'zzz'" in p for p in problems)
    assert any("'d__missing'" in p for p in problems)


def test_check_registry_ask_needs_why():
    catalog = _tiny_catalog(a=_metric("a"), b=_metric("b"))
    bare = Registry.model_validate(
        {
            "collisions": [{"phrase": ["r"], "candidates": ["a", "b"], "policy": "ask"}],
            "dimension_roles": [{"phrase": ["c"], "candidates": ["d__x", "e__x"], "policy": "ask"}],
        }
    )
    problems = check_registry(bare, catalog)
    assert [p for p in problems if "no why" in p and "collision:r" in p]
    assert [p for p in problems if "no why" in p and "dimension_role:c" in p]

    reasoned = Registry.model_validate(
        {
            "collisions": [
                {"phrase": ["r"], "candidates": ["a", "b"], "policy": "ask", "why": "they differ"}
            ],
            "dimension_roles": [
                {"phrase": ["c"], "candidates": ["d__x", "e__x"], "policy": "prefer d__x"}
            ],
        }
    )
    assert check_registry(reasoned, catalog) == []


def test_check_registry_unadjudicated_synonym():
    catalog = _tiny_catalog(
        net=_metric("net", synonyms=["revenue", "sales"]),
        gross=_metric("gross", synonyms=["Revenue"]),
    )
    [problem] = check_registry(Registry(), catalog)
    assert "'revenue'" in problem and "gross" in problem and "net" in problem

    covered = Registry.model_validate(
        {
            "collisions": [
                {"phrase": ["revenue"], "candidates": ["net", "gross"], "policy": "prefer net"}
            ]
        }
    )
    assert check_registry(covered, catalog) == []


def test_check_registry_duplicate_phrase_and_bad_convention():
    registry = Registry.model_validate(
        {
            "collisions": [{"phrase": ["Sales"], "candidates": ["a"], "policy": "prefer a"}],
            "unanswerable": [{"phrase": ["sales"], "reason": "x"}],
            "conventions": [
                {"name": "default_window", "value": "forever"},
                {"name": "fiscal_week", "value": "x"},
            ],
        }
    )
    problems = check_registry(registry, _tiny_catalog(a=_metric("a")))
    assert any("appears in both" in p for p in problems)
    assert any("'forever'" in p for p in problems)
    assert any("unknown convention" in p for p in problems)


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
