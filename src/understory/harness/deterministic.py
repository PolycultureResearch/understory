"""The zero-LLM eval: golden specs straight through `Service.query_metrics`.

This is what CI runs. It needs no API key and no model, so a broken golden set
or a regression in the traps matcher, the time anchoring, the compile step or
the warehouse fails the build in seconds rather than showing up as a mysterious
drop in the agent scores.

What it checks per item, using the item's recorded `spec`:

- the status matches the golden item (`resolved`, `needs_clarification`,
  `unanswerable`, `invalid`),
- a `needs_clarification` item asks for exactly the trap ids it declares,
- resubmitting with the item's `answers` resolves it,
- the returned rows contain every expected number,
- the required disclosures carry every expected substring.

It scores into the same `EvalReport` as the agent eval, with `capture` left
unset because there is no draft answer to log.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from understory.harness.evals import EvalReport, ItemScore, numbers_missing
from understory.harness.golden import GoldenItem
from understory.server.service import Service
from understory.types import Status, ToolResponse


def run_deterministic(service: Service, items: list[GoldenItem]) -> EvalReport:
    """Run every golden item's spec through the service and score it. No model."""
    started = datetime.now(UTC)
    scores = [_score(service, item) for item in items]
    return EvalReport(
        tenant=service.tenant.name,
        mode="deterministic",
        model="none",
        started_at=started,
        finished_at=datetime.now(UTC),
        items=scores,
    )


def _score(service: Service, item: GoldenItem) -> ItemScore:
    t0 = time.monotonic()
    exp = item.expected
    score = ItemScore(id=item.id, question=item.question, expected_status=exp.status)
    reasons: list[str] = []

    if item.spec is None:
        score.reasons = ["no spec on the golden item"]
        score.observed_status = "no_spec"
        return score

    session = service.sessions.get(f"deterministic:{item.id}")
    try:
        first = service.query_metrics(session, item.metric_spec())
    except Exception as e:  # a compile or warehouse failure is this item's failure
        score.observed_status = "error"
        score.error = f"{type(e).__name__}: {e}"
        score.reasons = [score.error]
        score.elapsed_ms = int((time.monotonic() - t0) * 1000)
        return score

    asked = [c.trap for c in first.clarifications]
    score.clarifications = asked
    score.tool_calls = ["query_metrics"]
    final: ToolResponse = first

    # Resolution accuracy.
    if exp.status == "needs_clarification":
        score.resolution = set(asked) == set(exp.clarification_traps)
        if not score.resolution:
            reasons.append(f"asked {asked or 'nothing'}, expected {exp.clarification_traps}")
        if exp.answers:
            score.tool_calls.append("query_metrics")
            final = service.query_metrics(session, item.metric_spec(with_answers=True))
            if final.status != Status.resolved:
                reasons.append(f"second turn returned {final.status}: {_why(final)}")
    elif exp.status in ("unanswerable", "invalid"):
        score.resolution = str(first.status) == exp.status
        if not score.resolution:
            reasons.append(f"got {first.status}, expected {exp.status}: {_why(first)}")
    else:
        score.resolution = first.status == Status.resolved and not asked
        if not score.resolution:
            reasons.append(f"got {first.status}: {_why(first)}")

    score.observed_status = str(first.status)
    if final.provenance is not None:
        score.metrics = final.provenance.metrics

    # Coverage: a resolving item must come back governed, with the metrics it declares.
    if exp.resolves and exp.governed:
        score.coverage = final.status == Status.resolved and bool(
            final.provenance and final.provenance.governed
        )
        if not score.coverage:
            reasons.append(f"not a governed result: {final.status}")
        elif exp.metrics and set(exp.metrics) - set(score.metrics):
            score.coverage = False
            reasons.append(f"ran {score.metrics}, expected {exp.metrics}")

    # Answer accuracy against the rows themselves.
    if exp.numbers:
        pool = _numeric_cells(final)
        missing = numbers_missing(exp.numbers, pool)
        score.answer = not missing
        if missing:
            reasons.append(f"numbers not in the rows: {missing}")

    # Disclosure rate against required_disclosures.
    if exp.disclosures:
        blob = " ".join(final.required_disclosures).lower()
        if final.refusal is not None:
            blob = f"{blob} {final.refusal.message}".lower()
        missing_text = [d for d in exp.disclosures if d.lower() not in blob]
        score.disclosure = not missing_text
        if missing_text:
            reasons.append(f"disclosures missing: {missing_text}")

    score.answer_text = _render(final)
    score.reasons = reasons
    score.elapsed_ms = int((time.monotonic() - t0) * 1000)
    score.passed = not reasons and score.resolution is not False
    return score


def _numeric_cells(response: ToolResponse) -> list[tuple[str, float]]:
    if response.result is None:
        return []
    out: list[tuple[str, float]] = []
    for row in response.result.rows:
        for cell in row:
            if isinstance(cell, bool):
                continue
            if isinstance(cell, int | float):
                out.append((_literal(cell), float(cell)))
    return out


def _literal(cell: Any) -> str:
    """The cell as it would be written out, so rounding tolerance has a precision."""
    return repr(float(cell))


def _why(response: ToolResponse) -> str:
    if response.refusal is not None:
        return response.refusal.message
    if response.clarifications:
        return ", ".join(c.trap for c in response.clarifications)
    return ""


def _render(response: ToolResponse) -> str:
    """A one-line rendering of the outcome, for the report."""
    if response.result is not None:
        cols = ", ".join(c.name for c in response.result.columns)
        return f"{response.result.row_count} row(s) over [{cols}]"
    if response.clarifications:
        return "; ".join(
            f"{c.trap}: {', '.join(o.id for o in c.options)}" for c in response.clarifications
        )
    if response.refusal is not None:
        return response.refusal.message
    return ""


__all__ = ["run_deterministic"]
