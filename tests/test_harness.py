"""The harness: golden sets, the deterministic eval, and the agent loop.

The deterministic eval is the one CI depends on. It needs no API key: every
golden item's recorded spec goes straight through the Service, and the report it
produces has the same shape as an agent run.

The agent test drives the real `build_agent` wiring with a scripted
`FunctionModel`, so the tool registration, the session binding and the Turn
record are exercised without a provider.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
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

from understory.harness.agent import DEFAULT_MODEL, Turn, run_question
from understory.harness.deterministic import run_deterministic
from understory.harness.evals import EvalReport, numbers_missing, run_evals, write_report
from understory.harness.golden import load_golden
from understory.server.service import Service
from understory.telemetry import TelemetryWriter
from understory.tenant import LogConfig, TenantConfig
from understory.traps.schema import load_registry

TENANTS = ["alpenglow", "white_cube", "meridian", "bristlecone"]


def _service(cfg: TenantConfig) -> Service:
    log = LogConfig(events_prefix="unused/events", text_prefix="unused/text", enabled=False)
    return Service(cfg, telemetry=TelemetryWriter(log, cfg.name))


# --------------------------------------------------------------------------- #
# 1. The golden sets load and line up with each tenant's traps registry.
# --------------------------------------------------------------------------- #


def test_golden_sets_load(tenants):
    for name in TENANTS:
        cfg = tenants[name]
        items = load_golden(cfg.golden_path)
        assert 8 <= len(items) <= 12, f"{name}: {len(items)} items"
        trap_ids = set(load_registry(cfg.traps_path).trap_ids())

        statuses = {i.expected.status for i in items}
        assert {"resolved", "needs_clarification", "unanswerable", "invalid"} <= statuses, name

        for item in items:
            assert item.spec is not None, f"{name}/{item.id} has no spec"
            assert item.metric_spec().metrics, f"{name}/{item.id} has an empty spec"
            for trap in item.expected.clarification_traps:
                assert trap in trap_ids, f"{name}/{item.id}: unknown trap {trap}"
            for trap in item.expected.answers:
                assert trap in trap_ids, f"{name}/{item.id}: unknown trap {trap}"
            if item.expected.status == "needs_clarification":
                assert item.expected.clarification_traps, f"{name}/{item.id} says ask but no trap"
            if item.expected.numbers:
                assert item.expected.resolves, f"{name}/{item.id} records numbers, never resolves"


def test_golden_missing_file_is_empty(tmp_path):
    assert load_golden(tmp_path / "nope.yml") == []


def test_numbers_missing_tolerance():
    found = [("1,043,396.05", 1043396.05), ("58.2%", 58.2), ("$3.0M", 3000000.0)]
    assert numbers_missing([1043396.05, 0.5821, 3018200.0], found) == []
    assert numbers_missing([999.0], found) == [999.0]


# --------------------------------------------------------------------------- #
# 2. The deterministic eval: every golden item, every tenant, no model.
# --------------------------------------------------------------------------- #


@pytest.mark.fake_db
@pytest.mark.metricflow
@pytest.mark.slow
def test_deterministic_eval_passes(tenant, tmp_path):
    service = _service(tenant)
    try:
        items = load_golden(tenant.golden_path)
        report = run_deterministic(service, items)
    finally:
        service.close()

    summary = report.summary()
    failures = [(i.id, i.reasons) for i in report.items if not i.passed]
    assert not failures, f"{tenant.name}: {failures}"
    assert summary["items"] == len(items)
    assert summary["pass_rate"] == 1.0
    assert summary["capture_rate"] is None, "deterministic runs log no draft answer"
    assert report.markdown().startswith(f"### {tenant.name}")

    path = write_report(report, tmp_path)
    assert json.loads(path.read_text())["tenant"] == tenant.name
    assert EvalReport.model_validate_json(path.read_text()).items


# --------------------------------------------------------------------------- #
# 3. The agent loop, scripted with a FunctionModel.
# --------------------------------------------------------------------------- #

_SPEC = {
    "metrics": ["net_revenue"],
    "group_by": ["metric_time"],
    "time": {"grain": "month", "start": "2025-01-01", "end": "2025-03-31"},
    "question": "What was net revenue by month in the first quarter of 2025?",
}


def _scripted(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """get_context, then query_metrics, then log_answer with a sourced draft, then reply."""
    assert {t.name for t in info.function_tools} == {
        "get_context",
        "list_metrics",
        "describe_metric",
        "search_dimension_values",
        "query_metrics",
        "run_sql",
        "log_answer",
    }
    returned = {
        part.tool_name: part.content
        for m in messages
        if isinstance(m, ModelRequest)
        for part in m.parts
        if isinstance(part, ToolReturnPart)
    }
    if "get_context" not in returned:
        return ModelResponse(parts=[ToolCallPart("get_context", {})])
    if "query_metrics" not in returned:
        return ModelResponse(parts=[ToolCallPart("query_metrics", {"spec": _SPEC})])
    if "log_answer" not in returned:
        return ModelResponse(parts=[ToolCallPart("log_answer", {"draft": _draft(returned)})])
    return ModelResponse(parts=[TextPart(_draft(returned))])


def _draft(returned: dict[str, Any]) -> str:
    response = returned["query_metrics"]
    values = [c for row in response["result"]["rows"] for c in row if isinstance(c, int | float)]
    shown = ", ".join(f"{v:,.2f}" for v in values)
    # No ISO dates in the draft: the number check reads a day-of-month as a
    # number to source, and this draft has to come back clean.
    return (
        f"Net revenue by month was {shown} over the first quarter of 2025. "
        "The window is anchored to the latest available data, not today."
    )


@pytest.mark.fake_db
@pytest.mark.metricflow
def test_run_question_records_the_loop(alpenglow_db):
    service = _service(alpenglow_db)
    try:
        turn = run_question(
            service,
            _SPEC["question"],
            model=FunctionModel(_scripted),
            session_key="test-harness",
        )
    finally:
        service.close()

    assert turn.error is None, turn.error
    assert [c.name for c in turn.tool_calls] == ["get_context", "query_metrics", "log_answer"]
    assert turn.statuses == ["ok", "resolved", "pass"]
    assert turn.metrics_queried == ["net_revenue"]
    assert turn.governed is True
    assert turn.clarifications == []
    assert turn.log_answer is not None and turn.log_answer.status == "pass"
    assert not turn.log_answer.unsourced
    assert turn.log_answer.disclosures_present
    assert "Net revenue by month was" in turn.answer

    call = next(c for c in turn.tool_calls if c.name == "query_metrics")
    assert call.args["metrics"] == ["net_revenue"]


@pytest.mark.fake_db
@pytest.mark.metricflow
def test_run_question_reports_a_model_failure(alpenglow_db):
    def explode(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise RuntimeError("provider is down")

    service = _service(alpenglow_db)
    try:
        turn = run_question(service, "anything", model=FunctionModel(explode))
    finally:
        service.close()
    assert isinstance(turn, Turn)
    assert turn.error and "provider is down" in turn.error
    assert turn.answer == ""


def _scripted_clarification(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """The clarification round trip: ask on the first run, resolve on the second."""
    prompt = next(
        part.content
        for m in messages
        if isinstance(m, ModelRequest)
        for part in m.parts
        if isinstance(part, UserPromptPart)
    )
    answered = "collision:revenue = net_revenue" in str(prompt)
    returned = {
        part.tool_name: part.content
        for m in messages
        if isinstance(m, ModelRequest)
        for part in m.parts
        if isinstance(part, ToolReturnPart)
    }
    if "get_context" not in returned:
        return ModelResponse(parts=[ToolCallPart("get_context", {})])
    if "query_metrics" not in returned:
        spec: dict[str, Any] = {
            "metrics": ["gross_revenue"],
            "time": {"start": "2025-03-01", "end": "2025-03-31"},
            "question": "How were sales in March 2025?",
        }
        if answered:
            spec["clarifications"] = [{"trap": "collision:revenue", "choice": "net_revenue"}]
        return ModelResponse(parts=[ToolCallPart("query_metrics", {"spec": spec})])

    response = returned["query_metrics"]
    if response["status"] == "needs_clarification":
        options = [o["id"] for c in response["clarifications"] for o in c["options"]]
        return ModelResponse(parts=[TextPart(f"Which do you mean: {', '.join(options)}?")])

    value = next(c for row in response["result"]["rows"] for c in row if isinstance(c, int | float))
    draft = (
        f"Net revenue in March was {value:,.2f}, read as net revenue because you chose it. "
        "The window is anchored to the latest available data, not today."
    )
    if "log_answer" not in returned:
        return ModelResponse(parts=[ToolCallPart("log_answer", {"draft": draft})])
    return ModelResponse(parts=[TextPart(draft)])


@pytest.mark.fake_db
@pytest.mark.metricflow
def test_run_evals_scores_a_clarification_item(alpenglow_db):
    wanted = "sales_collision_march_2025"
    items = [i for i in load_golden(alpenglow_db.golden_path) if i.id == wanted]
    assert len(items) == 1

    service = _service(alpenglow_db)
    try:
        report = run_evals(service, items, model=FunctionModel(_scripted_clarification))
    finally:
        service.close()

    score = report.items[0]
    assert score.passed, score.reasons
    assert score.clarifications == ["collision:revenue"]
    assert score.observed_status == "clarified"
    assert score.metrics == ["net_revenue"]
    assert score.capture is True and score.log_answer_status == "pass"
    assert report.summary()["resolution_rate"] == 1.0


# --------------------------------------------------------------------------- #
# 4. Live: two alpenglow items through OpenRouter.
# --------------------------------------------------------------------------- #


@pytest.mark.live
@pytest.mark.fake_db
@pytest.mark.metricflow
@pytest.mark.slow
def test_live_openrouter(alpenglow_db):
    if not os.environ.get("OPENROUTER_API_KEY"):
        pytest.skip("OPENROUTER_API_KEY not set")
    items = load_golden(alpenglow_db.golden_path)
    wanted = ("net_revenue_by_month_q1_2025", "sales_collision_march_2025")
    chosen = [i for i in items if i.id in wanted]
    assert len(chosen) == 2

    service = _service(alpenglow_db)
    try:
        model = os.environ.get("UNDERSTORY_EVAL_MODEL", DEFAULT_MODEL)
        report = run_evals(service, chosen, model=model)
    finally:
        service.close()

    summary = report.summary()
    assert summary["items"] == 2
    for item in report.items:
        assert item.error is None, item.error
        assert item.tool_calls, f"{item.id} called no tools"
    assert summary["capture_rate"] is not None
    print(report.markdown())
