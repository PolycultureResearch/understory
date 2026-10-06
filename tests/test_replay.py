"""The oracle replay: bypasses from hand-built specs, no model and no warehouse."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
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
from understory.harness.evals import EvalReport, ItemScore, run_evals
from understory.harness.golden import load_golden
from understory.harness.replay import Bypass, replay_spec, replay_turns
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


def _replay(spec: dict[str, Any], oracle: str, registry: Registry, catalog: Catalog):
    return replay_spec(spec, oracle, registry, catalog)


def test_verbatim_question_has_no_bypass(registry, catalog):
    oracle = "What was revenue in Q1 2025?"
    spec = {"metrics": ["gross_revenue"], "time": Q1, "question": oracle}
    assert _replay(spec, oracle, registry, catalog) == []


@pytest.mark.parametrize("question", [None, "gross revenue Q1 2025"])
def test_omitted_or_paraphrased_question_is_a_swap(registry, catalog, question):
    spec = {"metrics": ["gross_revenue"], "time": Q1, "question": question}
    [b] = _replay(spec, "What was revenue in Q1 2025?", registry, catalog)
    assert (b.trap, b.effect) == ("collision:revenue", "swap")
    assert b.material
    assert b.sent_question == question
    assert b.metrics == ["gross_revenue"]
    assert str(b) == "collision:revenue (swap)"


def test_follow_up_on_the_preferred_metric_only_loses_the_sentence(registry, catalog):
    spec = {
        "metrics": ["net_revenue"],
        "group_by": ["order__country"],
        "time": Q1,
        "question": "split that by country",
    }
    oracle = "What was revenue in Q1 2025? Split that by country."
    [b] = _replay(spec, oracle, registry, catalog)
    assert (b.trap, b.effect) == ("collision:revenue", "disclosure")
    assert not b.material


def test_skipped_ask_is_an_ask(registry, catalog):
    spec = {"metrics": ["margin_rate"], "time": Q1}
    [b] = _replay(spec, "What was our margin in Q1 2025?", registry, catalog)
    assert (b.trap, b.effect) == ("collision:margin", "ask")
    assert b.material


def test_reworded_unanswerable_is_a_refusal(registry, catalog):
    spec = {"metrics": ["net_revenue"], "time": Q1, "question": "customer value over time"}
    [b] = _replay(spec, "What is our LTV?", registry, catalog)
    assert (b.trap, b.effect) == ("unanswerable:ltv", "refusal")


def test_a_spec_that_does_not_validate_has_no_bypass(registry, catalog):
    assert _replay({"metrics": "not a list"}, "revenue", registry, catalog) == []


def test_turns_number_the_bypass_and_report_a_trap_once_per_turn(registry, catalog):
    first = "What was revenue in Q1 2025?"
    gross = {"metrics": ["gross_revenue"], "time": Q1, "question": "and by country"}
    turns = [
        (first, [{"metrics": ["net_revenue"], "time": Q1, "question": first}]),
        (f"{first} And by country?", [gross, gross]),
    ]
    [b] = replay_turns(turns, registry, catalog)
    assert (b.trap, b.effect, b.turn) == ("collision:revenue", "swap", 2)


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #


def _report(items: list[ItemScore], mode: str = "agent", repeat: int = 1) -> EvalReport:
    now = datetime.now(UTC)
    return EvalReport(
        tenant="t",
        mode=mode,
        model="m",
        repeat=repeat,
        started_at=now,
        finished_at=now,
        items=items,
    )


def test_report_counts_bypasses_over_the_items_that_queried():
    swap = Bypass(trap="collision:revenue", effect="swap")
    sentence = Bypass(trap="collision:returns", effect="disclosure")
    report = _report(
        [
            ItemScore(id="a", question="q", passed=True, queries=1, bypasses=[swap]),
            ItemScore(id="b", question="q", passed=True, queries=2, bypasses=[sentence]),
            ItemScore(id="c", question="q", passed=True, queries=1),
            ItemScore(id="d", question="q", passed=True, queries=1),
            ItemScore(id="prose_refusal", question="q", passed=True, queries=0),
        ]
    )
    s = report.summary()
    assert s["bypass_n"] == 4
    assert (s["bypass_items"], s["bypass_rate"]) == (2, 0.5)
    assert (s["material_bypass_items"], s["material_bypass_rate"]) == (1, 0.25)
    assert s["bypasses"] == {
        "a": ["collision:revenue (swap)"],
        "b": ["collision:returns (disclosure)"],
    }
    text = report.markdown()
    assert "oracle replay: 2/4 items bypassed a trap (50%), 1 materially (25%)" in text
    assert "- a: collision:revenue (swap)" in text


def test_report_names_the_run_of_a_repeated_bypass():
    swap = Bypass(trap="collision:revenue", effect="swap")
    report = _report(
        [
            ItemScore(id="a", question="q", queries=1, run=1),
            ItemScore(id="a", question="q", queries=1, run=2, bypasses=[swap]),
        ],
        repeat=2,
    )
    assert report.summary()["bypasses"] == {"a#2": ["collision:revenue (swap)"]}


def test_deterministic_and_unqueried_reports_carry_no_replay():
    assert "bypass_n" not in _report([ItemScore(id="a", question="q")]).summary()
    det = _report([ItemScore(id="a", question="q", queries=1)], mode="deterministic")
    assert "bypass_n" not in det.summary()
    assert "oracle replay" not in det.markdown()


# --------------------------------------------------------------------------- #
# End to end: a scripted model that tidies the question
# --------------------------------------------------------------------------- #


def _tidying(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Sends gross revenue for a "sales" question and rewrites the question to match."""
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
    return ModelResponse(parts=[TextPart("Gross revenue was high.")])


@pytest.mark.fake_db
@pytest.mark.metricflow
def test_run_evals_replays_the_specs_the_model_sent(alpenglow_db, tmp_path):
    log = LogConfig(events_prefix=str(tmp_path / "e"), text_prefix=str(tmp_path / "t"))
    service = Service(alpenglow_db, telemetry=TelemetryWriter(log, alpenglow_db.name))
    items = [i for i in load_golden(alpenglow_db.golden_path) if i.id == "sales_prefer_march_2025"]
    try:
        report = run_evals(service, items, model=FunctionModel(_tidying))
    finally:
        service.close()
    score = report.items[0]
    assert score.queries == 1
    assert [str(b) for b in score.bypasses] == ["collision:revenue (swap)"]
    assert score.bypasses[0].sent_question == "gross revenue in March 2025"
    assert report.summary()["material_bypass_rate"] == 1.0
