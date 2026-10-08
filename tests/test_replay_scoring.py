"""The off/on measures for spec-keyed disclosures: silent wrong readings, relay, extra queries."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
import typer
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from understory.catalog.manifest import load_catalog
from understory.harness.cli import _spec_arms
from understory.harness.evals import (
    EvalReport,
    ItemScore,
    _mentions,
    _reports,
    compare_replay,
    run_evals,
)
from understory.harness.golden import load_golden
from understory.harness.replay import SentQuery, replay_conversation
from understory.server.service import Service
from understory.telemetry import TelemetryWriter
from understory.tenant import LogConfig, tenants_dir
from understory.traps import Registry, load_registry
from understory.types import Catalog

Q1 = {"start": "2025-01-01", "end": "2025-03-31"}


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    return load_catalog(tenants_dir() / "alpenglow" / "semantic_manifest.json")


@pytest.fixture(scope="module")
def registry() -> Registry:
    return load_registry(tenants_dir() / "alpenglow" / "traps.yml")


def _one(spec: dict[str, Any], oracle: str, registry: Registry, catalog: Catalog):
    return replay_conversation(
        [(oracle, [SentQuery(spec=spec, numbers=[1234.5])])], registry, catalog
    )


def test_a_swapped_away_candidate_is_a_wrong_reading(registry, catalog):
    spec = {"metrics": ["gross_revenue"], "time": Q1, "question": "gross revenue Q1 2025"}
    r = _one(spec, "What was revenue in Q1 2025?", registry, catalog)
    assert r.ambiguous
    [reading] = r.wrong
    assert str(reading) == "collision:revenue: gross_revenue"
    assert reading.names == ["gross_revenue", "Gross Revenue"]
    assert reading.numbers == [1234.5]

    [keyed] = r.spec_keyed
    assert (keyed.trap, keyed.candidate) == ("collision:revenue", "gross_revenue")
    assert keyed.text.startswith("This is Gross Revenue.")
    assert keyed.default_names == ["net_revenue", "Net Revenue"]


def test_a_skipped_ask_is_a_wrong_reading_set_against_every_other_candidate(registry, catalog):
    r = _one({"metrics": ["margin_rate"], "time": Q1}, "What was our margin?", registry, catalog)
    assert [str(w) for w in r.wrong] == ["collision:margin: margin_rate"]
    assert r.spec_keyed[0].default_names == ["gross_margin", "Gross Margin"]


def test_the_preferred_candidate_is_ambiguous_and_not_wrong(registry, catalog):
    spec = {"metrics": ["net_revenue"], "time": Q1, "question": "split that by country"}
    r = _one(spec, "What was revenue in Q1 2025? Split that.", registry, catalog)
    assert r.ambiguous
    assert [b.effect for b in r.bypasses] == ["disclosure"]
    assert r.wrong == [] and r.spec_keyed == []


def test_an_explicit_question_is_not_ambiguous_but_the_rule_still_speaks(registry, catalog):
    question = "What was gross revenue in Q1 2025?"
    spec = {"metrics": ["gross_revenue"], "time": Q1, "question": question}
    r = _one(spec, question, registry, catalog)
    assert not r.ambiguous
    assert r.bypasses == [] and r.wrong == []
    assert len(r.spec_keyed) == 1, "the cost side: a sentence the user did not need"


def test_reports_and_mentions():
    assert _reports("Revenue was $1,234.50 in March.", [1234.5])
    assert _reports("About 1.2K.", [1234.5])
    assert not _reports("Revenue was strong.", [1234.5])
    assert not _reports("There were 3 countries.", [3.0]), "small whole numbers are prose"
    assert not _reports("The 1,234.50 was unsourced so I will rephrase.", [1234.5])
    assert _mentions("Gross revenue was high", ["gross_revenue", "Gross Revenue"])
    assert _mentions("gross_revenue: 5", ["gross_revenue"])
    assert not _mentions("Revenue was high", ["gross_revenue", "Gross Revenue"])


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #


def _report(items: list[ItemScore], **kw: Any) -> EvalReport:
    now = datetime.now(UTC)
    return EvalReport(
        tenant="t", mode="agent", model="m", started_at=now, finished_at=now, items=items, **kw
    )


def _items() -> list[ItemScore]:
    silent = ["collision:revenue: gross_revenue"]
    return [
        ItemScore(id="a", question="q", queries=1, ambiguous=True, silent_wrong=silent),
        ItemScore(id="b", question="q", queries=3, turns=2, ambiguous=True, spec_keyed=1),
        ItemScore(
            id="c",
            question="q",
            queries=1,
            spec_keyed=2,
            spec_keyed_returned=2,
            spec_keyed_relayed=1,
        ),
        ItemScore(id="d", question="q", queries=1, first_turn_answer=True),
    ]


def test_summary_carries_the_three_measures():
    s = _report(_items()).summary()
    assert (s["silent_wrong_items"], s["silent_wrong_n"], s["silent_wrong_rate"]) == (1, 2, 0.5)
    assert s["silent_wrong"] == {"a": ["collision:revenue: gross_revenue"]}
    assert (s["spec_keyed"], s["spec_keyed_returned"], s["spec_keyed_relayed"]) == (3, 2, 1)
    assert s["spec_keyed_relay_rate"] == 0.5
    assert (s["avg_queries"], s["extra_queries"], s["avg_extra_queries"]) == (1.5, 1, 0.25)

    text = _report(_items()).markdown()
    assert "silent wrong readings: 1/2 ambiguous items (50%)" in text
    assert "spec-keyed disclosures: 3 apply, 2 returned, 1 relayed (50%)" in text
    assert "queries 1.5 per item, 0.25 extra" in text
    assert "- silent: a: collision:revenue: gross_revenue" in text


def test_relay_rate_is_none_when_nothing_was_returned():
    s = _report([ItemScore(id="a", question="q", queries=1, spec_keyed=1)]).summary()
    assert s["spec_keyed_relay_rate"] is None
    assert s["silent_wrong_rate"] is None


def test_label_and_comparison_name_the_arm():
    off, on = _report(_items()), _report(_items(), spec_disclosures=True)
    assert (off.label, on.label) == ("agent/harness", "agent/harness+spec")
    lines = compare_replay([off, on]).strip().splitlines()
    assert lines[0].startswith("| model | mode | bypass | material | silent wrong |")
    assert (
        lines[2]
        == "| m | agent/harness | 0/4 | 0/4 | 1/2 | 2/3 | 50% | 100% | 1.5 | 0.25 | - | - |"
    )
    assert lines[3].startswith("| m | agent/harness+spec |")


def test_spec_arms():
    assert _spec_arms(None, False) == [False]
    assert _spec_arms(None, True) == [True]
    assert _spec_arms("off", True) == [False]
    assert _spec_arms("on", False) == [True]
    assert _spec_arms("Both", False) == [False, True]
    with pytest.raises(typer.BadParameter):
        _spec_arms("maybe", False)


# --------------------------------------------------------------------------- #
# End to end: the same tidied spec, two replies, switch off and on
# --------------------------------------------------------------------------- #


def _replying(template: str):
    """Gross revenue for a "sales" question with the question tidied to match."""

    def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returned = {
            part.tool_name: part.content
            for m in messages
            if isinstance(m, ModelRequest)
            for part in m.parts
            if isinstance(part, ToolReturnPart)
        }
        if "query_metrics" not in returned:
            spec = {
                "metrics": ["gross_revenue"],
                "time": {"start": "2025-03-01", "end": "2025-03-31"},
                "question": "gross revenue in March 2025",
            }
            return ModelResponse(parts=[ToolCallPart("query_metrics", {"spec": spec})])
        rows = returned["query_metrics"]["result"]["rows"]
        value = next(c for row in rows for c in row if isinstance(c, int | float))
        return ModelResponse(parts=[TextPart(template.format(value=value))])

    return FunctionModel(script)


SILENT = "Sales in March 2025 were {value:,.2f}."
NAMED = "Gross revenue in March 2025 was {value:,.2f}. That is before discounts, not net revenue."


@pytest.mark.fake_db
@pytest.mark.metricflow
@pytest.mark.parametrize(
    ("on", "template", "silent", "returned", "relayed"),
    [
        (False, SILENT, True, 0, 0),
        (False, NAMED, False, 0, 0),
        (True, SILENT, True, 1, 0),
        (True, NAMED, False, 1, 1),
    ],
)
def test_run_evals_scores_the_reading(
    alpenglow_db, tmp_path, on, template, silent, returned, relayed
):
    cfg = alpenglow_db.model_copy(
        update={"checks": alpenglow_db.checks.model_copy(update={"spec_disclosures": on})}
    )
    log = LogConfig(events_prefix=str(tmp_path / "e"), text_prefix=str(tmp_path / "t"))
    service = Service(cfg, telemetry=TelemetryWriter(log, cfg.name))
    items = [i for i in load_golden(cfg.golden_path) if i.id == "sales_prefer_march_2025"]
    try:
        report = run_evals(service, items, model=_replying(template))
    finally:
        service.close()
    score = report.items[0]
    assert report.spec_disclosures is on
    assert score.ambiguous
    assert score.silent_wrong == (["collision:revenue: gross_revenue"] if silent else [])
    assert score.spec_keyed == 1, "the rule applies to the sent spec whether or not it is on"
    assert (score.spec_keyed_returned, score.spec_keyed_relayed) == (returned, relayed)
    assert (score.turns, score.queries) == (1, 1)
