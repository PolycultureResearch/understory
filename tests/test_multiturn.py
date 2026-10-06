"""Multi-turn golden items: the schema, the deterministic run, and the agent runner."""

from __future__ import annotations

from collections import Counter
from typing import Any

import pytest
import yaml
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from understory.harness.deterministic import run_deterministic
from understory.harness.evals import run_evals
from understory.harness.golden import GoldenItem, GoldenSet, load_golden
from understory.harness.realistic import dump_items
from understory.server.service import Service
from understory.telemetry import TelemetryWriter
from understory.tenant import LogConfig, TenantConfig
from understory.traps.schema import load_registry


def _service(cfg: TenantConfig, tmp_path) -> Service:
    log = LogConfig(events_prefix=str(tmp_path / "e"), text_prefix=str(tmp_path / "t"))
    return Service(cfg, telemetry=TelemetryWriter(log, cfg.name))


def test_said_joins_the_user_turns():
    item = GoldenItem(id="a", question="Split that by country.", earlier=["Revenue in Q1?"])
    assert item.said() == "Revenue in Q1? Split that by country."
    assert item.said(1) == "Revenue in Q1?"
    assert GoldenItem(id="b", question="One turn.").said() == "One turn."


def test_metric_spec_carries_everything_the_user_said():
    item = GoldenItem(
        id="a",
        question="Split that by country.",
        earlier=["Revenue in Q1?"],
        spec={"metrics": ["gross_revenue"]},
    )
    assert item.metric_spec().question == "Revenue in Q1? Split that by country."


def test_dump_items_round_trips_earlier_turns():
    item = GoldenItem(
        id="a", question="And by country?", earlier=["Gross revenue: March?"], kind="explicit"
    )
    [back] = GoldenSet.model_validate(yaml.safe_load(dump_items([item]))).questions
    assert back.earlier == ["Gross revenue: March?"]
    assert back.kind == "explicit"


def test_alpenglow_multiturn_set_loads(alpenglow):
    assert alpenglow.golden_set_path("multiturn") == alpenglow.multiturn_path
    items = load_golden(alpenglow.multiturn_path)
    assert len(items) == 10
    kinds = Counter(i.kind for i in items)
    assert kinds == {"carry_over": 4, "synonym": 3, "explicit": 3}
    assert sum(1 for i in items if i.earlier) >= 7
    trap_ids = set(load_registry(alpenglow.traps_path).trap_ids())
    for item in items:
        assert item.spec is not None and item.metric_spec().metrics, item.id
        assert set(item.expected.clarification_traps) <= trap_ids, item.id
        assert not item.verified, f"{item.id}: a snapshot is not verified"
    # A carry-over item keeps the trap word out of the turn that queries.
    for item in items:
        if item.kind == "carry_over":
            last = item.question.lower()
            assert not any(w in last for w in ("revenue", "margin", "returns", "sales")), item.id


@pytest.mark.fake_db
@pytest.mark.metricflow
def test_multiturn_set_passes_deterministically(alpenglow_db, tmp_path):
    items = load_golden(alpenglow_db.multiturn_path)
    service = _service(alpenglow_db, tmp_path)
    try:
        report = run_deterministic(service, items)
    finally:
        service.close()
    failures = {i.id: i.reasons for i in report.items if not i.passed}
    assert not failures, failures
    assert set(report.summary()["by_kind"]) == {"carry_over", "synonym", "explicit"}


# --------------------------------------------------------------------------- #
# The agent runner, scripted
# --------------------------------------------------------------------------- #


def _prompts(messages: list[ModelMessage]) -> list[str]:
    return [
        str(part.content)
        for m in messages
        if isinstance(m, ModelRequest)
        for part in m.parts
        if isinstance(part, UserPromptPart)
    ]


def _this_turn(messages: list[ModelMessage]) -> dict[str, Any]:
    """Tool results since the last user message."""
    last_user = 0
    for index, m in enumerate(messages):
        if isinstance(m, ModelRequest) and any(isinstance(p, UserPromptPart) for p in m.parts):
            last_user = index
    return {
        part.tool_name: part.content
        for m in messages[last_user:]
        if isinstance(m, ModelRequest)
        for part in m.parts
        if isinstance(part, ToolReturnPart)
    }


class _Drifting:
    """Net revenue with the user's words on turn one, gross with a bare follow-up on turn two."""

    def __init__(self) -> None:
        self.prompts: list[list[str]] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompts = _prompts(messages)
        self.prompts.append(prompts)
        returned = _this_turn(messages)
        if "query_metrics" in returned:
            rows = returned["query_metrics"]["result"]["rows"]
            values = [c for row in rows for c in row if isinstance(c, int | float)]
            shown = ", ".join(f"{v:,.2f}" for v in values)
            return ModelResponse(parts=[TextPart(f"It was {shown}, by where the order shipped.")])
        spec: dict[str, Any] = {"time": {"start": "2025-01-01", "end": "2025-03-31"}}
        if len(prompts) == 1:
            spec |= {"metrics": ["net_revenue"], "question": prompts[0]}
        else:
            spec |= {
                "metrics": ["gross_revenue"],
                "group_by": ["order__country"],
                "question": "split that by country",
            }
        return ModelResponse(parts=[ToolCallPart("query_metrics", {"spec": spec})])


@pytest.mark.fake_db
@pytest.mark.metricflow
def test_run_evals_runs_earlier_turns_and_replays_against_what_was_said(alpenglow_db, tmp_path):
    items = [
        i for i in load_golden(alpenglow_db.multiturn_path) if i.id == "revenue_then_by_country"
    ]
    script = _Drifting()
    service = _service(alpenglow_db, tmp_path)
    try:
        report = run_evals(service, items, model=FunctionModel(script))
    finally:
        service.close()
    score = report.items[0]
    item = items[0]

    # Both turns ran, in one conversation, and usage covers both.
    assert script.prompts[0] == [item.earlier[0]]
    assert script.prompts[-1] == [item.earlier[0], item.question]
    assert (score.turns, score.queries) == (2, 2)
    assert score.usage.get("requests") == 4

    # The first turn sent the user's words, so only the second is a bypass.
    [bypass] = score.bypasses
    assert (bypass.trap, bypass.effect, bypass.turn) == ("collision:revenue", "swap", 2)
    assert bypass.sent_question == "split that by country"

    # The last turn is the one scored: gross revenue ran where net was expected.
    assert not score.passed
    assert score.metrics == ["gross_revenue"]
    assert any("expected ['net_revenue']" in r for r in score.reasons)
    assert score.kind == "carry_over"
