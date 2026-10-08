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
    RetryPromptPart,
    SystemPromptPart,
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
    compare,
    numbers_missing,
    run_evals,
    strip_narration,
    write_report,
)
from understory.harness.golden import load_golden
from understory.server.service import Service
from understory.server.surfaces import TOOL_DESCRIPTIONS, connector_instructions
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
        assert 8 <= len(items) <= 14, f"{name}: {len(items)} items"
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
    answered = any("collision:margin = margin_rate" in p for p in _prompts(turn))
    history = _returned(messages)
    returned = _returned(turn)
    if "get_context" not in history:
        return ModelResponse(parts=[ToolCallPart("get_context", {})])
    if "query_metrics" not in returned:
        spec: dict[str, Any] = {
            "metrics": ["gross_margin"],
            "time": {"start": "2025-04-01", "end": "2025-06-30"},
            "question": "What was our margin in the second quarter of 2025?",
        }
        if answered:
            spec["clarifications"] = [{"trap": "collision:margin", "choice": "margin_rate"}]
        return ModelResponse(parts=[ToolCallPart("query_metrics", {"spec": spec})])

    response = returned["query_metrics"]
    if response["status"] == "needs_clarification":
        options = [o["id"] for c in response["clarifications"] for o in c["options"]]
        return ModelResponse(parts=[TextPart(f"Which do you mean: {', '.join(options)}?")])

    value = next(c for row in response["result"]["rows"] for c in row if isinstance(c, int | float))
    draft = (
        f"Margin rate for the quarter was {value:.4f}, read as margin rate because you chose it. "
        "The window is anchored to the latest available data, not today."
    )
    if "log_answer" not in returned:
        return ModelResponse(parts=[ToolCallPart("log_answer", {"draft": draft})])
    return ModelResponse(parts=[TextPart(draft)])


@pytest.mark.fake_db
@pytest.mark.metricflow
def test_run_evals_scores_a_clarification_item(alpenglow_db):
    wanted = "margin_collision_q2_2025"
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
    assert score.clarifications == ["collision:margin"]
    assert score.observed_status == "clarified"
    assert score.metrics == ["margin_rate"]
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
    assert _prompts(opening)[1].startswith("My answers: collision:margin = margin_rate")
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
    wanted = ("net_revenue_by_month_q1_2025", "margin_collision_q2_2025")
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


# --------------------------------------------------------------------------- #
# The false pass from the first live run: an expected number that appears only
# in the model's sentence about dropping it must not count, and the reply that
# narrates the check is not clean.
# --------------------------------------------------------------------------- #

_LEAKED = (
    "The 315 total wasn't in a tool result, so I'll drop that computed sum and report "
    "the monthly figures instead. Trials started in Germany, Jan to Jun 2025, by month: "
    "Jan 49, Feb 42, Mar 44, Apr 52, May 67, Jun 61. The window is anchored to the "
    "latest available data, not today."
)


def test_strip_narration_removes_sentences_about_the_check():
    kept, dropped = strip_narration(_LEAKED)
    assert len(dropped) == 1 and "315" in dropped[0]
    assert "315" not in kept and "Jan 49" in kept
    kept, dropped = strip_narration("Orders were 16,016. That's all.")
    assert dropped == [] and "16,016" in kept
    assert strip_narration("") == ("", [])
    _, dropped = strip_narration("That's just the 31 in Mar 31 being flagged, not a real issue.")
    assert dropped


def _narrating(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Runs the covered query, then replies with the leaked narration."""
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
        return ModelResponse(parts=[ToolCallPart("log_answer", {"draft": _LEAKED})])
    return ModelResponse(parts=[TextPart(_LEAKED)])


@pytest.mark.fake_db
@pytest.mark.metricflow
def test_narrated_number_does_not_pass_and_reply_is_not_clean(alpenglow_db):
    from understory.harness.golden import Expectation, GoldenItem

    item = GoldenItem(
        id="narrated",
        question=_SPEC["question"],
        spec=_SPEC,
        expected=Expectation(status="resolved", metrics=["net_revenue"], numbers=[315]),
    )
    service = _service(alpenglow_db)
    try:
        report = run_evals(service, [item], model=FunctionModel(_narrating))
    finally:
        service.close()

    score = report.items[0]
    assert score.answer is False, score.reasons
    assert score.clean is False
    assert not score.passed
    assert any("narrates" in r for r in score.reasons)
    assert any("315" in r for r in score.reasons)
    assert report.summary()["clean_rate"] == 0.0
    assert "| cln |" in report.markdown()


# --------------------------------------------------------------------------- #
# 7. The realistic set's report: first-turn answer rate, over-refusal, by kind.
# --------------------------------------------------------------------------- #


def _score(id_: str, kind: str | None, **kw: Any) -> ItemScore:
    kw.setdefault("expected_status", "resolved")
    return ItemScore(id=id_, question=id_, kind=kind, **kw)


def _report(items: list[ItemScore], mode: str = "agent") -> EvalReport:
    now = datetime.now(UTC)
    return EvalReport(
        tenant="alpenglow", mode=mode, model="m", started_at=now, finished_at=now, items=items
    )


def test_report_breaks_the_headline_down_by_kind():
    report = _report(
        [
            _score("e1", "event", passed=True, first_turn_answer=True, refused=False),
            _score("e2", "event", passed=False, first_turn_answer=False, refused=True),
            _score("q1", "quiet", passed=True, first_turn_answer=True, refused=False),
            _score("o1", "over_refusal", passed=False, first_turn_answer=False, refused=True),
            _score("f1", "fault", passed=True, first_turn_answer=True, refused=False),
            # An asked item answered on the second turn: passed, but not first turn.
            _score(
                "a1",
                "event",
                passed=True,
                first_turn_answer=False,
                refused=False,
                clarifications=["collision:margin"],
                clarification_resolved=True,
            ),
            # A correct refusal: neither rate applies.
            _score("u1", "quiet", passed=True, expected_status="unanswerable"),
        ]
    )
    s = report.summary()
    assert s["first_turn_answer_n"] == 6 and s["first_turn_answer_rate"] == 0.5
    assert s["over_refusal_n"] == 6 and s["over_refusal_rate"] == round(2 / 6, 4)
    by_kind = s["by_kind"]
    assert list(by_kind) == ["event", "quiet", "over_refusal", "fault"]
    assert by_kind["event"] == {
        "items": 3,
        "passed": 2,
        "pass_rate": round(2 / 3, 4),
        "first_turn_answer_rate": round(1 / 3, 4),
        "over_refusal_rate": round(1 / 3, 4),
        "failures": ["e2"],
    }
    assert by_kind["quiet"]["first_turn_answer_rate"] == 1.0
    assert by_kind["over_refusal"]["over_refusal_rate"] == 1.0
    md = report.markdown()
    assert "first-turn 50% | over-refusal 33%" in md
    assert "| kind | items | passed | first-turn | over-refusal | failures |" in md
    assert "| over_refusal | 1 | 0 | 0% | 100% | o1 |" in md


def test_trap_set_report_has_no_kind_table():
    report = _report([_score("t1", None, passed=True, first_turn_answer=True, refused=False)])
    assert "by_kind" not in report.summary()
    assert "| kind |" not in report.markdown()


def test_compare_lists_one_row_per_report():
    default = _report([_score("e1", "event", passed=True, first_turn_answer=True, refused=False)])
    cheap = _report(
        [_score("e1", "event", passed=False, first_turn_answer=False, refused=True)], mode="byo"
    )
    cheap.model = "cheap"
    table = compare([default, cheap])
    lines = table.strip().splitlines()
    assert lines[0].startswith("| model | mode | passed | first-turn | over-refusal |")
    assert lines[2] == "| m | agent/harness | 1/1 | 100% | 0% | n/a | n/a | - |"
    assert lines[3] == "| cheap | byo | 0/1 | 0% | 100% | n/a | n/a | - |"


def test_report_label_names_the_variant():
    assert _report([]).label == "agent/harness"
    lean = _report([])
    lean.prompt, lean.enforce_check = "lean", True
    assert lean.label == "agent/lean+check"
    assert _report([], mode="byo").label == "byo"
    assert _report([], mode="deterministic").label == "deterministic"


def test_write_report_names_the_variant(tmp_path):
    assert write_report(_report([], mode="byo"), tmp_path).name.endswith("-m-byo.json")
    assert write_report(_report([]), tmp_path).name.endswith("-m-agent-harness.json")
    assert write_report(_report([], mode="deterministic"), tmp_path).name.endswith("-m.json")


def test_report_counts_passes_per_item_across_repeats():
    report = _report(
        [
            _score("e1", "event", passed=True, first_turn_answer=True, run=1),
            _score(
                "e1",
                "event",
                passed=False,
                first_turn_answer=False,
                run=2,
                reasons=["log_answer never called"],
            ),
            _score(
                "e1",
                "event",
                passed=False,
                first_turn_answer=False,
                run=3,
                reasons=["log_answer never called"],
            ),
            _score("q1", "quiet", passed=True, first_turn_answer=True, run=1),
            _score("q1", "quiet", passed=True, first_turn_answer=True, run=2),
            _score("q1", "quiet", passed=True, first_turn_answer=True, run=3),
        ]
    )
    report.repeat = 3
    by_item = report.summary()["by_item"]
    assert by_item["e1"] == {
        "runs": 3,
        "passed": 1,
        "first_turn_answer": 1,
        "reasons": {"log_answer never called": 2},
    }
    assert by_item["q1"]["passed"] == 3
    md = report.markdown()
    assert "- x3" in md.splitlines()[0]
    assert "| e1 | 1/3 | 1/3 | log_answer never called x2 |" in md
    assert "by_item" not in _report([]).summary()


# --------------------------------------------------------------------------- #
# 8. BYO mode: the connector surfaces and nothing else.
# --------------------------------------------------------------------------- #


class _Surfaces:
    """The scripted loop, recording what the model was shown."""

    def __init__(self) -> None:
        self.system: str | None = None
        self.descriptions: dict[str, str | None] = {}

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.descriptions = {t.name: t.description for t in info.function_tools}
        for part in messages[0].parts:
            if isinstance(part, SystemPromptPart):
                self.system = part.content
        return _scripted(messages, info)


@pytest.mark.fake_db
@pytest.mark.metricflow
def test_byo_mode_shows_the_connector_surfaces_only(alpenglow_db):
    script = _Surfaces()
    service = _service(alpenglow_db)
    try:
        turn = run_question(
            service,
            _SPEC["question"],
            model=FunctionModel(script),
            session_key="test-byo",
            byo=True,
        )
    finally:
        service.close()

    assert turn.error is None, turn.error
    assert script.system == connector_instructions(alpenglow_db)
    assert "You are an analyst" not in (script.system or "")
    assert script.descriptions == TOOL_DESCRIPTIONS
    # The loop itself is unchanged: same tools, same session, same trace.
    assert [c.name for c in turn.tool_calls] == ["get_context", "query_metrics", "log_answer"]
    assert turn.metrics_queried == ["net_revenue"]


@pytest.mark.fake_db
@pytest.mark.metricflow
def test_harness_mode_keeps_its_own_prompt_and_docstrings(alpenglow_db):
    script = _Surfaces()
    service = _service(alpenglow_db)
    try:
        run_question(service, _SPEC["question"], model=FunctionModel(script), session_key="t")
    finally:
        service.close()
    assert script.system is not None and script.system.startswith("You are an analyst")
    assert script.descriptions["query_metrics"] != TOOL_DESCRIPTIONS["query_metrics"]
    assert "The spec fields" in (script.descriptions["query_metrics"] or "")


@pytest.mark.fake_db
@pytest.mark.metricflow
def test_run_evals_in_byo_mode_reports_the_mode(alpenglow_db):
    wanted = "net_revenue_by_month_q1_2025"
    items = [i for i in load_golden(alpenglow_db.golden_path) if i.id == wanted]
    assert len(items) == 1
    service = _service(alpenglow_db)
    try:
        report = run_evals(service, items, model=FunctionModel(_scripted), byo=True)
    finally:
        service.close()
    assert report.mode == "byo"
    score = report.items[0]
    assert score.passed, score.reasons
    assert score.kind is None
    assert score.first_turn_answer is True and score.refused is False


# --------------------------------------------------------------------------- #
# 9. The structural closing check, the lean prompt, and repeats.
# --------------------------------------------------------------------------- #


def _retried(messages: list[ModelMessage]) -> bool:
    return any(
        isinstance(part, RetryPromptPart)
        for m in messages
        if isinstance(m, ModelRequest)
        for part in m.parts
    )


def _forgetful(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """get_context, query_metrics, then reply without ever calling log_answer."""
    returned = _returned(messages)
    if "get_context" not in returned:
        return ModelResponse(parts=[ToolCallPart("get_context", {})])
    if "query_metrics" not in returned:
        return ModelResponse(parts=[ToolCallPart("query_metrics", {"spec": _SPEC})])
    return ModelResponse(parts=[TextPart(_draft(returned))])


def _sloppy(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Like _forgetful, but the first reply carries a number no result returned.

    On the retry the harness hands back, it replies with the sourced draft.
    """
    returned = _returned(messages)
    if "get_context" not in returned:
        return ModelResponse(parts=[ToolCallPart("get_context", {})])
    if "query_metrics" not in returned:
        return ModelResponse(parts=[ToolCallPart("query_metrics", {"spec": _SPEC})])
    if _retried(messages):
        return ModelResponse(parts=[TextPart(_draft(returned))])
    return ModelResponse(
        parts=[TextPart("Net revenue for the quarter came to 9,999,999.00 all told.")]
    )


@pytest.mark.fake_db
@pytest.mark.metricflow
def test_structural_check_runs_when_the_model_forgets(alpenglow_db):
    service = _service(alpenglow_db)
    try:
        turn = run_question(
            service, _SPEC["question"], model=FunctionModel(_forgetful), session_key="t-forget"
        )
    finally:
        service.close()
    assert turn.error is None, turn.error
    assert [c.name for c in turn.tool_calls] == ["get_context", "query_metrics", "log_answer"]
    check = turn.tool_calls[-1]
    assert check.args["via"] == "structural" and check.status == "pass"
    assert turn.log_answer is not None and turn.log_answer.status == "pass"
    assert (
        turn.log_answer.how_to_read is not None
        and "addressed to the user" in turn.log_answer.how_to_read
    )
    assert "Net revenue by month was" in turn.answer


@pytest.mark.fake_db
@pytest.mark.metricflow
def test_structural_check_sends_an_unsourced_reply_back_once(alpenglow_db):
    service = _service(alpenglow_db)
    try:
        turn = run_question(
            service, _SPEC["question"], model=FunctionModel(_sloppy), session_key="t-sloppy"
        )
    finally:
        service.close()
    assert turn.error is None, turn.error
    names = [c.name for c in turn.tool_calls]
    assert names == ["get_context", "query_metrics", "log_answer", "log_answer"]
    assert [c.status for c in turn.tool_calls[2:]] == ["unsourced_numbers", "pass"]
    assert all(c.args["via"] == "structural" for c in turn.tool_calls[2:])
    assert "9,999,999" not in turn.answer
    assert "Net revenue by month was" in turn.answer
    assert turn.log_answer is not None and turn.log_answer.status == "pass"


@pytest.mark.fake_db
@pytest.mark.metricflow
def test_structural_check_is_off_in_byo_mode_and_when_asked(alpenglow_db):
    service = _service(alpenglow_db)
    try:
        byo = run_question(
            service, _SPEC["question"], model=FunctionModel(_forgetful), session_key="b", byo=True
        )
        off = run_question(
            service,
            _SPEC["question"],
            model=FunctionModel(_forgetful),
            session_key="o",
            enforce_check=False,
        )
    finally:
        service.close()
    for turn in (byo, off):
        assert [c.name for c in turn.tool_calls] == ["get_context", "query_metrics"]
        assert turn.log_answer is None


@pytest.mark.fake_db
@pytest.mark.metricflow
def test_structural_check_passes_through_a_draft_the_model_already_checked(alpenglow_db):
    service = _service(alpenglow_db)
    try:
        turn = run_question(
            service, _SPEC["question"], model=FunctionModel(_scripted), session_key="t-ok"
        )
    finally:
        service.close()
    assert [c.name for c in turn.tool_calls] == ["get_context", "query_metrics", "log_answer"]
    assert "via" not in turn.tool_calls[-1].args


@pytest.mark.fake_db
@pytest.mark.metricflow
def test_lean_prompt_is_the_connector_instructions_plus_two_lines(alpenglow_db):
    script = _Surfaces()
    service = _service(alpenglow_db)
    try:
        run_question(
            service, _SPEC["question"], model=FunctionModel(script), session_key="l", prompt="lean"
        )
    finally:
        service.close()
    assert script.system is not None
    assert script.system.startswith(connector_instructions(alpenglow_db).strip())
    assert "Call get_context first." in script.system
    assert "log_answer" not in script.system.split("\n\n", 1)[1]
    assert "brief" not in script.system
    # The harness keeps its own tool docstrings in every prompt variant.
    assert "The spec fields" in (script.descriptions["query_metrics"] or "")


@pytest.mark.fake_db
def test_unknown_prompt_is_refused(alpenglow):
    from understory.harness.agent import build_agent
    from understory.server.session import SessionStore

    service = _service(alpenglow)
    try:
        with pytest.raises(ValueError, match="unknown prompt"):
            build_agent(
                service, SessionStore().get("x"), model=FunctionModel(_scripted), prompt="nope"
            )
    finally:
        service.close()


@pytest.mark.fake_db
@pytest.mark.metricflow
def test_run_evals_repeats_and_counts_per_item(alpenglow_db):
    wanted = "net_revenue_by_month_q1_2025"
    items = [i for i in load_golden(alpenglow_db.golden_path) if i.id == wanted]
    service = _service(alpenglow_db)
    try:
        report = run_evals(service, items, model=FunctionModel(_forgetful), repeat=3, prompt="lean")
    finally:
        service.close()
    assert report.repeat == 3 and report.prompt == "lean" and report.enforce_check is True
    assert report.label == "agent/lean+check"
    assert [i.run for i in report.items] == [1, 2, 3]
    assert all(i.passed for i in report.items), [i.reasons for i in report.items]
    assert report.summary()["by_item"][wanted]["passed"] == 3
    assert report.summary()["capture_rate"] == 1.0
