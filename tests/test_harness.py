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
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic_ai.exceptions import ModelHTTPError
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
from pydantic_ai.usage import RunUsage

from understory.harness import evals
from understory.harness.agent import DEFAULT_MODEL, Turn, _usage, run_question
from understory.harness.deterministic import run_deterministic
from understory.harness.evals import (
    EvalReport,
    ItemScore,
    numbers_missing,
    run_evals,
    write_report,
)
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


class _Clarification:
    """The clarification round trip, as a FunctionModel that remembers what it saw.

    The second turn continues the first turn's conversation, so this only reads
    the tool results of the current turn, the ones after the last user message.
    Everything before that is history, and `seen` keeps it so a test can check
    the second turn really was handed the first turn's messages.
    """

    def __init__(self) -> None:
        self.seen: list[list[ModelMessage]] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.seen.append(list(messages))
        return _scripted_clarification(messages, info)


def _turn_messages(messages: list[ModelMessage]) -> list[ModelMessage]:
    """The messages of the current turn: the last user message and everything after."""
    last_user = 0
    for index, m in enumerate(messages):
        if isinstance(m, ModelRequest) and any(isinstance(p, UserPromptPart) for p in m.parts):
            last_user = index
    return messages[last_user:]


def _prompts(messages: list[ModelMessage]) -> list[str]:
    return [
        str(part.content)
        for m in messages
        if isinstance(m, ModelRequest)
        for part in m.parts
        if isinstance(part, UserPromptPart)
    ]


def _returned(messages: list[ModelMessage]) -> dict[str, Any]:
    return {
        part.tool_name: part.content
        for m in messages
        if isinstance(m, ModelRequest)
        for part in m.parts
        if isinstance(part, ToolReturnPart)
    }


def _scripted_clarification(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """The clarification round trip: ask on the first turn, resolve on the second.

    get_context is checked against the whole history, the way a model with the
    context already in its window behaves, so a second turn that was handed the
    first turn's messages never fetches it twice. query_metrics and log_answer
    are checked against this turn only.
    """
    turn = _turn_messages(messages)
    answered = any("collision:revenue = net_revenue" in p for p in _prompts(turn))
    history = _returned(messages)
    returned = _returned(turn)
    if "get_context" not in history:
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

    script = _Clarification()
    service = _service(alpenglow_db)
    try:
        report = run_evals(service, items, model=FunctionModel(script))
    finally:
        service.close()

    score = report.items[0]
    assert score.passed, score.reasons
    assert score.clarifications == ["collision:revenue"]
    assert score.observed_status == "clarified"
    assert score.metrics == ["net_revenue"]
    assert score.capture is True and score.log_answer_status == "pass"
    assert report.summary()["resolution_rate"] == 1.0

    # The second turn continued the first turn's conversation. The request that
    # opens it carries both user messages and the first turn's tool results, and
    # the trace for that turn records the clarified query without a second
    # get_context.
    first_turn_requests = [m for m in script.seen if len(_prompts(m)) == 1]
    second_turn_requests = [m for m in script.seen if len(_prompts(m)) == 2]
    assert first_turn_requests, "the first turn never ran"
    assert second_turn_requests, "the second turn started a fresh conversation"

    opening = second_turn_requests[0]
    assert _prompts(opening)[0] == items[0].question
    assert _prompts(opening)[1].startswith("My answers: collision:revenue = net_revenue")
    assert "get_context" in _returned(opening), "the first turn's context was dropped"
    assert len(opening) > len(first_turn_requests[-1]), "no history was carried over"
    assert score.tool_calls == ["query_metrics", "log_answer"]
    assert "get_context" not in score.tool_calls


# --------------------------------------------------------------------------- #
# 4. Usage and cost capture.
# --------------------------------------------------------------------------- #


class _FakeResult:
    """The shape `_usage` reads: a `usage` property and `new_messages()`."""

    def __init__(self, usage: RunUsage, responses: list[Any] | None = None) -> None:
        self.usage = usage
        self._responses = responses or []

    def new_messages(self) -> list[Any]:
        return self._responses


def _run_usage() -> RunUsage:
    return RunUsage(
        input_tokens=4100,
        output_tokens=260,
        cache_read_tokens=3800,
        cache_write_tokens=1900,
        requests=4,
        tool_calls=3,
    )


def test_usage_reads_every_counter():
    usage = _usage(_FakeResult(_run_usage()))
    assert usage == {
        "input_tokens": 4100,
        "output_tokens": 260,
        "cache_read_tokens": 3800,
        "cache_write_tokens": 1900,
        "requests": 4,
        "tool_calls": 3,
        "total_tokens": 4360,
    }


def test_usage_prefers_the_cost_openrouter_reported():
    """Cost comes off provider_details, summed over the responses this run added."""
    priced = _run_usage()
    priced.cost = Decimal("0.99")  # the genai-prices estimate, the fallback only
    responses = [
        ModelResponse(parts=[], provider_name="openrouter", provider_details={"cost": 0.0021}),
        ModelResponse(parts=[], provider_name="openrouter", provider_details={"cost": 0.0009}),
        ModelRequest(parts=[UserPromptPart("not a response")]),
    ]
    usage = _usage(_FakeResult(priced, responses))
    assert usage["cost"] == pytest.approx(0.003)
    assert usage["cache_read_tokens"] == 3800


def test_usage_falls_back_to_the_price_estimate():
    priced = _run_usage()
    priced.cost = Decimal("0.0042")
    assert _usage(_FakeResult(priced))["cost"] == pytest.approx(0.0042)


def test_usage_survives_a_callable_usage():
    """`usage` was a method before pydantic-ai 2.x. Reading it as one must still work."""

    class _Old:
        def usage(self) -> RunUsage:
            return _run_usage()

        def new_messages(self) -> list[Any]:
            return []

    assert _usage(_Old())["requests"] == 4


def test_usage_of_nothing_is_empty():
    assert _usage(object()) == {}


def test_report_summarises_usage_and_cost():
    items = [
        ItemScore(
            id="a",
            question="?",
            passed=True,
            usage={
                "input_tokens": 4000,
                "output_tokens": 200,
                "cache_read_tokens": 3600,
                "cache_write_tokens": 400,
                "total_tokens": 4200,
                "requests": 4,
                "cost": 0.004,
            },
        ),
        ItemScore(
            id="b",
            question="?",
            passed=True,
            usage={
                "input_tokens": 2000,
                "output_tokens": 100,
                "cache_read_tokens": 1800,
                "cache_write_tokens": 0,
                "total_tokens": 2100,
                "requests": 3,
                "cost": 0.002,
            },
        ),
        ItemScore(id="c", question="?", skipped=True, observed_status="skipped"),
    ]
    now = datetime.now(UTC)
    report = EvalReport(
        tenant="alpenglow",
        mode="agent",
        model="anthropic/claude-haiku-4.5",
        started_at=now,
        finished_at=now,
        items=items,
    )
    s = report.summary()
    assert s["input_tokens"] == 6000
    assert s["cache_read_tokens"] == 5400
    assert s["measured_items"] == 2, "a skipped item is not in the average"
    assert s["avg_input_tokens"] == 3000.0
    assert s["avg_cache_read_tokens"] == 2700.0
    assert s["cost_usd"] == pytest.approx(0.006)
    assert s["avg_cost_usd"] == pytest.approx(0.003)
    assert s["skipped"] == ["c"]

    table = report.markdown()
    assert "cache read 5,400" in table
    assert "0.60c over 2 items, 0.30c each" in table
    assert "| cents |" in table
    assert "| 0.40c |" in table


# --------------------------------------------------------------------------- #
# 5. Budget guards: refuse a set the balance cannot cover, stop on a 402.
# --------------------------------------------------------------------------- #


def test_check_budget_refuses_when_the_credit_runs_short(monkeypatch):
    monkeypatch.setattr(
        evals, "key_status", lambda *a, **k: {"usage": 5.49, "limit": 6.0, "limit_remaining": 0.31}
    )
    lines: list[str] = []
    with pytest.raises(evals.BudgetError) as raised:
        evals.check_budget(10, per_item=0.06, echo=lines.append)
    assert "$0.31 of credit left" in str(raised.value)
    assert "--force" in str(raised.value)
    assert lines and "remaining $0.31" in lines[0] and "needs $0.60" in lines[0]


def test_check_budget_lets_a_covered_set_through(monkeypatch):
    monkeypatch.setattr(
        evals, "key_status", lambda *a, **k: {"usage": 1.0, "limit": 20.0, "limit_remaining": 19.0}
    )
    assert evals.check_budget(10, per_item=0.06)["limit_remaining"] == 19.0


def test_check_budget_force_overrides_a_short_balance(monkeypatch):
    monkeypatch.setattr(evals, "key_status", lambda *a, **k: {"limit_remaining": 0.01})
    lines: list[str] = []
    evals.check_budget(10, per_item=0.06, force=True, echo=lines.append)
    assert any("--force" in line for line in lines)


def test_check_budget_allows_an_unlimited_or_unreadable_key(monkeypatch):
    monkeypatch.setattr(
        evals, "key_status", lambda *a, **k: {"limit": None, "limit_remaining": None}
    )
    assert evals.check_budget(100) == {"limit": None, "limit_remaining": None}
    monkeypatch.setattr(evals, "key_status", lambda *a, **k: {})
    lines: list[str] = []
    assert evals.check_budget(100, echo=lines.append) == {}
    assert "balance unknown" in lines[0]


def test_key_status_without_a_key_is_empty(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert evals.key_status() == {}


@pytest.mark.fake_db
@pytest.mark.metricflow
def test_run_evals_stops_on_insufficient_credits(alpenglow_db):
    """A 402 ends the run. Every item after it is skipped, not retried."""
    items = load_golden(alpenglow_db.golden_path)[:3]
    assert len(items) == 3
    first = items[0].question

    def broke(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if any(p == first for p in _prompts(messages)):
            return ModelResponse(parts=[TextPart("The governed data cannot answer that.")])
        raise ModelHTTPError(
            status_code=402,
            model_name="anthropic/claude-haiku-4.5",
            body={"error": {"message": "Insufficient credits", "code": 402}},
        )

    lines: list[str] = []
    service = _service(alpenglow_db)
    try:
        report = run_evals(service, items, model=FunctionModel(broke), echo=lines.append)
    finally:
        service.close()

    ran, failed, skipped = report.items
    assert not ran.skipped and ran.error is None
    assert failed.error_status == 402 and not failed.skipped
    assert skipped.skipped is True
    assert skipped.observed_status == "skipped"
    assert skipped.reasons == [evals.STOPPED_REASON]
    assert skipped.passed is False

    summary = report.summary()
    assert summary["stopped"] == evals.STOPPED_REASON
    assert summary["skipped"] == [items[2].id]
    assert evals.STOPPED_REASON in report.markdown()
    assert lines, "nothing was reported as it went"


# --------------------------------------------------------------------------- #
# 6. Live: two alpenglow items through OpenRouter.
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

    model = os.environ.get("UNDERSTORY_EVAL_MODEL", DEFAULT_MODEL)
    service = _service(alpenglow_db)
    try:
        report = run_evals(service, chosen, model=model, echo=print)
    except evals.BudgetError as e:
        pytest.skip(f"not enough OpenRouter credit: {e}")
    finally:
        service.close()

    summary = report.summary()
    assert summary["items"] == 2
    for item in report.items:
        assert item.error is None, item.error
        assert item.tool_calls, f"{item.id} called no tools"
    assert summary["capture_rate"] is not None
    print(report.markdown())

    # Usage and cost came back, and on an Anthropic model the second request of
    # a run reads the prefix the first one wrote.
    assert summary["input_tokens"] > 0
    assert summary["output_tokens"] > 0
    assert summary["requests"] >= 4, "two items, several requests each"
    assert summary["cost_usd"] > 0, "OpenRouter reported no cost"
    assert summary["avg_cost_usd"] < 0.25, f"{summary['avg_cost_usd']} per question is too dear"
    if "anthropic" in model:
        assert summary["cache_write_tokens"] > 0, "nothing was ever written to the cache"
        assert summary["cache_read_tokens"] > 0, "the cached prefix was never read back"
        assert summary["cache_read_tokens"] > summary["input_tokens"] * 0.3
