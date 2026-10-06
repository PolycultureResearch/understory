"""Spec-keyed disclosures: the traps check when the question the chatbot sent is no help.

The four cases are the probe from `knowledge/grill-2026-10-05.md`: the same
gross revenue spec with the user's words, with no question, with a tidied
question, and as a follow-up turn. Plus the tenant's one ask.
"""

from __future__ import annotations

from typing import Any

import pytest

from understory.catalog.manifest import load_catalog
from understory.server.service import Service
from understory.server.session import Session
from understory.telemetry import TelemetryWriter
from understory.tenant import LogConfig, tenants_dir
from understory.traps import Registry, check, load_registry
from understory.traps.match import SPEC_SOURCE
from understory.types import Catalog, MetricSpec, TrapsOutcome

Q1 = {"start": "2025-01-01", "end": "2025-03-31"}

GROSS = (
    "This is Gross Revenue. Before discounts and refunds. "
    "'revenue' on its own is read as Net Revenue."
)
MARGIN_RATE = (
    "This is Margin Rate. Gross margin as a share of gross revenue. "
    "'margin' can also mean Gross Margin (Dollar margin before discounts). "
    "Finance reports margin in dollars and marketing in percent, and the two have been "
    "confused in board decks."
)


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    return load_catalog(tenants_dir() / "alpenglow" / "semantic_manifest.json")


@pytest.fixture(scope="module")
def registry() -> Registry:
    return load_registry(tenants_dir() / "alpenglow" / "traps.yml")


def _check(registry: Registry, catalog: Catalog, on: bool, **spec: Any) -> TrapsOutcome:
    spec.setdefault("time", Q1)
    return check(
        MetricSpec.model_validate(spec),
        registry,
        catalog,
        max_clarifications=3,
        spec_disclosures=on,
    )


def _spec_keyed(outcome: TrapsOutcome) -> list[str]:
    return [d.text for d in outcome.disclosures if d.source.startswith(SPEC_SOURCE)]


@pytest.mark.parametrize("on", [False, True])
def test_the_users_words_fire_the_phrase_and_nothing_else(registry, catalog, on):
    out = _check(
        registry, catalog, on, metrics=["gross_revenue"], question="what was revenue in Q1 2025"
    )
    assert out.spec.metrics == ["net_revenue"]
    assert out.fired == ["collision:revenue"]
    assert [d.source for d in out.disclosures] == ["collision:revenue"]


@pytest.mark.parametrize("question", [None, "gross revenue Q1 2025"])
def test_omitted_or_tidied_question_is_disclosed_and_not_swapped(registry, catalog, question):
    off = _check(registry, catalog, False, metrics=["gross_revenue"], question=question)
    assert off.disclosures == [] and off.fired == []

    on = _check(registry, catalog, True, metrics=["gross_revenue"], question=question)
    assert on.spec.metrics == ["gross_revenue"], "nothing is swapped"
    assert on.clarifications == []
    assert _spec_keyed(on) == [GROSS]
    assert [d.source for d in on.disclosures] == ["spec:collision:revenue"]
    assert on.fired == [], "fired stays a record of phrases that matched"


def test_follow_up_turn_keeps_the_role_and_adds_the_metric(registry, catalog):
    spec = {
        "metrics": ["gross_revenue"],
        "group_by": ["order__country"],
        "question": "and split that by country",
    }
    off = _check(registry, catalog, False, **spec)
    assert [d.source for d in off.disclosures] == ["dimension_role:region"]

    on = _check(registry, catalog, True, **spec)
    assert [d.source for d in on.disclosures] == [
        "dimension_role:region",
        "spec:collision:revenue",
    ]
    assert _spec_keyed(on) == [GROSS]
    assert on.fired == ["dimension_role:region"]


def test_the_one_ask_is_disclosed_with_its_why_and_not_asked(registry, catalog):
    off = _check(registry, catalog, False, metrics=["margin_rate"])
    assert off.disclosures == [] and off.clarifications == []

    on = _check(registry, catalog, True, metrics=["margin_rate"])
    assert on.clarifications == [], "no ask: the user may have said margin rate"
    assert _spec_keyed(on) == [MARGIN_RATE]


def test_the_preferred_candidate_says_nothing(registry, catalog):
    on = _check(registry, catalog, True, metrics=["net_revenue", "return_rate"])
    assert on.disclosures == []


def test_a_chosen_candidate_says_nothing(registry, catalog):
    on = _check(
        registry,
        catalog,
        True,
        metrics=["margin_rate"],
        clarifications=[{"trap": "collision:margin", "choice": "margin_rate"}],
    )
    assert on.disclosures == []


def test_a_phrase_that_fired_is_not_disclosed_twice(registry, catalog):
    on = _check(
        registry,
        catalog,
        True,
        metrics=["gross_revenue"],
        question="revenue, and I do mean gross revenue",
    )
    assert [d.source for d in on.disclosures] == ["collision:revenue"]
    assert on.disclosures[0].text.endswith("as named.")


def test_each_named_candidate_is_disclosed_once(registry, catalog):
    on = _check(registry, catalog, True, metrics=["refunds", "returned_units", "refunds"])
    texts = _spec_keyed(on)
    assert len(texts) == 2
    assert texts[0].startswith("This is Refunds. Refund dollars.")
    assert texts[0].endswith("'returns' on its own is read as Return Rate.")
    assert texts[1].startswith("This is Returned Units.")


def test_dimension_roles_follow_the_same_rule(registry, catalog):
    spec = {"metrics": ["net_revenue"], "group_by": ["customer__country"]}
    assert _check(registry, catalog, False, **spec).disclosures == []

    on = _check(registry, catalog, True, **spec)
    assert on.spec.group_by == ["customer__country"], "nothing is swapped"
    [d] = on.disclosures
    assert d.source == "spec:dimension_role:region"
    label = catalog.dimensions["customer__country"].label or "customer__country"
    preferred = catalog.dimensions["order__country"].label or "order__country"
    assert d.text.startswith(f"This uses {label}.")
    assert d.text.endswith(f"'region' on its own is read as {preferred}.")

    preferred_only = _check(
        registry, catalog, True, metrics=["net_revenue"], group_by=["order__country"]
    )
    assert preferred_only.disclosures == []


@pytest.mark.fake_db
@pytest.mark.metricflow
@pytest.mark.parametrize("on", [False, True])
def test_the_tenant_switch_reaches_query_metrics(alpenglow_db, tmp_path, on):
    assert alpenglow_db.checks.spec_disclosures is False, "off by default"
    cfg = alpenglow_db.model_copy(
        update={"checks": alpenglow_db.checks.model_copy(update={"spec_disclosures": on})}
    )
    log = LogConfig(events_prefix=str(tmp_path / "e"), text_prefix=str(tmp_path / "t"))
    service = Service(cfg, telemetry=TelemetryWriter(log, cfg.name))
    try:
        response = service.query_metrics(
            Session(key="t"), {"metrics": ["gross_revenue"], "time": Q1}
        )
    finally:
        service.close()
    assert response.provenance is not None
    assert response.provenance.metrics == ["gross_revenue"]
    assert (GROSS in response.required_disclosures) is on
