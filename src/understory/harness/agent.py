"""The Pydantic AI agent that drives one tenant's Service.

The harness is the reference chatbot. It talks to the same seven methods the
MCP server exposes, in process, so an eval run and a real connector exercise
the same code. The model sits behind OpenRouter, which keeps the eval bill
predictable and lets us pin a model id per run.

Two things make the harness stricter than a bring-your-own chatbot. It always
calls `get_context` first, and it always calls `log_answer` with its draft
before replying. Those are the two habits a hosted chatbot does not reliably
have, so the numbers the harness produces are an upper bound.

Tests drive the same agent with `pydantic_ai.models.test.TestModel` or
`FunctionModel`, which is why `model` accepts a `Model` instance as well as an
OpenRouter model id.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models import Model

from understory.server.service import Service
from understory.server.session import Session
from understory.types import AnswerReview, MetricSpec, Status, ToolResponse

DEFAULT_MODEL = "anthropic/claude-sonnet-5"
"""OpenRouter model id the evals pin by default. Chosen for cost per run."""

SYSTEM_PROMPT = """\
You are an analyst for {display_name}. You answer questions about the business
from its governed semantic layer, using only the tools below.

How to work:

1. Call get_context first, before anything else. It carries what the company
   does, what each metric means, and the conventions that apply.
2. Use list_metrics and describe_metric when you are unsure which metric a
   question means, and search_dimension_values before filtering on a value you
   have not seen, so "the West" becomes real values.
3. Answer numeric questions with query_metrics. Send a spec, not a question.
4. When query_metrics returns needs_clarification, do not answer and do not
   guess. Present each clarification's options to the user verbatim, with their
   hints, as a short choice, and stop there. The user replies, and you then call
   query_metrics again with the same spec plus a clarifications entry
   {{"trap": <trap id>, "choice": <option id>}} for each answer.
5. When query_metrics returns unanswerable or invalid, say plainly that the
   governed data cannot answer it and why. Offer the suggestions. Do not invent
   a number.
6. Use run_sql only when no governed metric can answer the question. Any answer
   built on run_sql must say it came from ad hoc SQL rather than governed
   metrics, and is unverified.
7. State every required_disclosure from the tool result in your answer, in your
   own words but without dropping what it says.
8. Never state a number that did not come back in a tool result. No estimates,
   no arithmetic on numbers you were not given beyond what the tools returned.
9. Call log_answer with your draft answer before you reply, every time,
   including when you conclude the question cannot be answered and never ran a
   query. That is how refusals get recorded. If it reports unsourced numbers,
   fix the draft so every number traces to a result, then reply with the
   corrected text.

Be brief. Give the number, the window it covers, and the disclosures. No
preamble about what you are about to do.
"""


# --------------------------------------------------------------------------- #
# What a run produced
# --------------------------------------------------------------------------- #


class ToolCallRecord(BaseModel):
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    """A short summary of the arguments, not necessarily the full payload."""
    status: str = "ok"
    """Status the tool reported, or `ok` for tools with no status."""


class ClarificationAsked(BaseModel):
    trap: str
    options: list[str]


class Turn(BaseModel):
    """One question through the agent, with everything the evals score."""

    question: str
    answer: str = ""
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    statuses: list[str] = Field(default_factory=list)
    """Every status any tool returned, in order."""
    metrics_queried: list[str] = Field(default_factory=list)
    """Governed metrics the resolved query_metrics calls actually ran."""
    governed: bool | None = None
    """Provenance of the last query that returned rows. None when none did."""
    clarifications: list[ClarificationAsked] = Field(default_factory=list)
    required_disclosures: list[str] = Field(default_factory=list)
    log_answer: AnswerReview | None = None
    usage: dict[str, int] = Field(default_factory=dict)
    error: str | None = None
    """Set when the model run itself failed, e.g. a provider error."""

    def called(self, name: str) -> bool:
        return any(c.name == name for c in self.tool_calls)


@dataclass
class Trace:
    """Mutable record the tool wrappers append to during a run."""

    calls: list[ToolCallRecord] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    metrics_queried: list[str] = field(default_factory=list)
    governed: bool | None = None
    clarifications: list[ClarificationAsked] = field(default_factory=list)
    required_disclosures: list[str] = field(default_factory=list)
    review: AnswerReview | None = None

    def record(self, name: str, args: dict[str, Any], status: str) -> None:
        self.calls.append(ToolCallRecord(name=name, args=args, status=status))
        self.statuses.append(status)

    def absorb(self, response: ToolResponse) -> None:
        for d in response.required_disclosures:
            if d not in self.required_disclosures:
                self.required_disclosures.append(d)
        for c in response.clarifications:
            self.clarifications.append(
                ClarificationAsked(trap=c.trap, options=[o.id for o in c.options])
            )
        if response.status != Status.resolved or response.provenance is None:
            return
        self.governed = response.provenance.governed
        for m in response.provenance.metrics:
            if m not in self.metrics_queried:
                self.metrics_queried.append(m)


# --------------------------------------------------------------------------- #
# Model resolution
# --------------------------------------------------------------------------- #


def resolve_model(model: str | Model, api_key: str | None = None) -> Model:
    """Turn a model id into an OpenRouter-backed model. A `Model` passes through.

    Passing a `Model` is how the tests run the whole agent with no API key.
    """
    if isinstance(model, Model):
        return model
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openrouter import OpenRouterProvider

    key = api_key or os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError(
            "No OpenRouter API key. Set OPENROUTER_API_KEY or pass api_key, or pass a "
            "pydantic_ai Model instance for offline runs."
        )
    return OpenAIChatModel(model, provider=OpenRouterProvider(api_key=key))


def model_id(model: str | Model) -> str:
    return model if isinstance(model, str) else getattr(model, "model_name", str(model))


# --------------------------------------------------------------------------- #
# The agent
# --------------------------------------------------------------------------- #


def build_agent(
    service: Service,
    session: Session,
    *,
    model: str | Model,
    api_key: str | None = None,
    trace: Trace | None = None,
) -> Agent:
    """An agent over one tenant, with the seven tools bound to one session.

    `trace` collects the tool calls and their statuses. Pass one in to read them
    back after the run; `run_question` does exactly that.
    """
    tr = trace if trace is not None else Trace()

    def get_context() -> str:
        """Business context for this company.

        What it does, what the metrics mean, the conventions that apply, how
        fresh the data is, and which tables run_sql may touch. Call this first,
        before any other tool.
        """
        text = service.get_context(session)
        tr.record("get_context", {}, "ok")
        return text

    def list_metrics() -> dict[str, Any]:
        """Every governed metric with its label, description, type and synonyms.

        Use it when you do not know which metric a question means. Metric names
        must be spelled exactly as listed here in a query_metrics spec.
        """
        out = service.list_metrics(session)
        tr.record("list_metrics", {}, "ok")
        return out

    def describe_metric(name: str) -> dict[str, Any]:
        """One metric in depth.

        Returns its definition, the filters baked into it, the dimensions that
        are valid with it, how fresh it is, and example specs. Use it before
        grouping or filtering, because not every dimension works with every
        metric.

        Args:
            name: Exact metric name from list_metrics.
        """
        out = service.describe_metric(session, name)
        tr.record("describe_metric", {"name": name}, str(out.get("status", "ok")))
        return out

    def search_dimension_values(dimension: str, query: str = "") -> dict[str, Any]:
        """Real values of a categorical dimension, with row counts.

        Call this before filtering on a value you have not seen, so a phrase
        like "the West" or "our wholesale channel" becomes values that exist.

        Args:
            dimension: Fully qualified name, e.g. order__country.
            query: Optional substring to match. Empty returns the top values.
        """
        out = service.search_dimension_values(session, dimension, query)
        tr.record(
            "search_dimension_values",
            {"dimension": dimension, "query": query},
            str(out.get("status", "ok")),
        )
        return out

    def query_metrics(spec: MetricSpec) -> dict[str, Any]:
        """Run a governed metric query and get rows back.

        The spec fields:

        - metrics: metric names exactly as list_metrics gives them.
        - group_by: dimension names like order__country, or metric_time for a
          time series. Add a grain suffix for time, e.g. metric_time__month.
        - where: clauses of {dimension, op, values}, op one of eq, ne, in,
          not_in, gt, gte, lt, lte.
        - time: {grain, start, end} with ISO dates. An omitted end anchors to
          the latest date with data, never to today.
        - question: the user's question, verbatim. It feeds the traps check, so
          always send it.
        - clarifications: [{trap, choice}] for every clarification the user has
          already answered.

        The response status is one of resolved, needs_clarification,
        unanswerable, invalid, too_broad or error. On needs_clarification,
        present the options to the user and stop. On resolved, read the rows and
        state every required_disclosure.

        Args:
            spec: The metric spec to run.
        """
        response = service.query_metrics(session, spec)
        tr.absorb(response)
        tr.record("query_metrics", _spec_summary(spec), str(response.status))
        return response.model_dump(mode="json")

    def run_sql(sql: str, question: str | None = None) -> dict[str, Any]:
        """Escape hatch: one read-only SELECT against the allowed schemas.

        Only for questions no governed metric can answer. The result carries
        governed: false, and any answer built on it must say it came from ad hoc
        SQL and is unverified.

        Args:
            sql: A single SELECT statement.
            question: The user's question, for the log.
        """
        response = service.run_sql(session, sql, question)
        tr.absorb(response)
        tr.record("run_sql", {"sql": _clip(sql, 200)}, str(response.status))
        return response.model_dump(mode="json")

    def log_answer(draft: str) -> dict[str, Any]:
        """Send your draft answer before you show it to the user.

        Checks that every number in the draft traces back to a result this
        session already saw, and that the required disclosures are present.
        Returns pass, or the numbers with no source. Fix the draft and call
        again if anything came back unsourced.

        Args:
            draft: The exact text you are about to reply with.
        """
        review = service.log_answer(session, draft)
        tr.review = review
        tr.record("log_answer", {"chars": len(draft)}, review.status)
        return review.model_dump(mode="json")

    return Agent(
        resolve_model(model, api_key),
        system_prompt=SYSTEM_PROMPT.format(display_name=service.tenant.display_name),
        tools=[
            get_context,
            list_metrics,
            describe_metric,
            search_dimension_values,
            query_metrics,
            run_sql,
            log_answer,
        ],
        retries=2,
    )


def run_question(
    service: Service,
    question: str,
    *,
    model: str | Model,
    api_key: str | None = None,
    session_key: str | None = None,
    clarification_answers: dict[str, str] | None = None,
) -> Turn:
    """Ask one question and return everything the evals need to score it.

    `clarification_answers` maps trap id to option id. They go into the user
    prompt so the model can pass them straight back as a `clarifications` entry
    on its next query_metrics call. That stands in for the human turn in the
    clarification round trip: the first run of a question asks, and a second run
    with the answers resolves it. Reuse the same `session_key` across both so
    the session keeps the results log_answer checks against.
    """
    session = service.sessions.get(session_key or f"harness:{id(service)}")
    trace = Trace()
    agent = build_agent(service, session, model=model, api_key=api_key, trace=trace)

    prompt = question
    if clarification_answers:
        answers = "; ".join(f"{trap} = {choice}" for trap, choice in clarification_answers.items())
        prompt = (
            f"{question}\n\n"
            "If a tool asks you to clarify, these are my answers, as trap id = option id: "
            f"{answers}. Pass them through as the clarifications field and do not ask me again."
        )

    answer = ""
    error: str | None = None
    usage: dict[str, int] = {}
    try:
        result = agent.run_sync(prompt)
        answer = str(result.output or "")
        usage = _usage(result)
    except Exception as e:  # a provider error is a failed item, not a failed run
        error = f"{type(e).__name__}: {e}"

    return Turn(
        question=question,
        answer=answer,
        tool_calls=trace.calls,
        statuses=trace.statuses,
        metrics_queried=trace.metrics_queried,
        governed=trace.governed,
        clarifications=trace.clarifications,
        required_disclosures=trace.required_disclosures,
        log_answer=trace.review,
        usage=usage,
        error=error,
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _usage(result: Any) -> dict[str, int]:
    try:
        u = result.usage()
    except Exception:
        return {}
    out: dict[str, int] = {}
    for name in ("input_tokens", "output_tokens", "requests", "tool_calls"):
        value = getattr(u, name, None)
        if isinstance(value, int):
            out[name] = value
    total = out.get("input_tokens", 0) + out.get("output_tokens", 0)
    if total:
        out["total_tokens"] = total
    return out


def _spec_summary(spec: MetricSpec | dict[str, Any]) -> dict[str, Any]:
    if isinstance(spec, dict):
        try:
            spec = MetricSpec.model_validate(spec)
        except Exception:
            return {"spec": _clip(str(spec), 200)}
    out: dict[str, Any] = {"metrics": spec.metrics}
    if spec.group_by:
        out["group_by"] = spec.group_by
    if spec.where:
        out["where"] = [f"{w.dimension} {w.op} {w.values}" for w in spec.where]
    if spec.time.start or spec.time.end or spec.time.grain:
        out["time"] = spec.time.model_dump(mode="json", exclude_none=True)
    if spec.clarifications:
        out["clarifications"] = [f"{c.trap}={c.choice}" for c in spec.clarifications]
    return out


def _clip(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


__all__ = [
    "DEFAULT_MODEL",
    "SYSTEM_PROMPT",
    "ClarificationAsked",
    "ToolCallRecord",
    "Trace",
    "Turn",
    "build_agent",
    "model_id",
    "resolve_model",
    "run_question",
]
