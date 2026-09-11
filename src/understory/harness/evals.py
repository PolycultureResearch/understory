"""Scoring a golden set, and the report both eval modes share.

Design section 12 names five metrics. Each one is scored per item, and each one
is only counted on the items it applies to, so a tenant whose golden set has one
`run_sql` item does not lose coverage points for it.

| Metric              | An item passes it when                                        |
|---------------------|---------------------------------------------------------------|
| Coverage rate       | The answer came from a resolved `query_metrics`, not `run_sql` |
| Resolution accuracy | It asked exactly when the item says ask, with the same traps   |
| Answer accuracy     | Every expected number appears in the answer within tolerance   |
| Disclosure rate     | Every expected substring appears in the answer                 |
| Capture rate        | `log_answer` was called                                        |

An item passes overall when every metric that applies to it passes.

`run_evals` drives the real agent. `deterministic.run_deterministic` drives the
same golden set straight through `Service.query_metrics` with no model, and
returns the same `EvalReport`, so CI can run without an API key.
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic_ai.models import Model

from understory.harness.agent import DEFAULT_MODEL, Turn, model_id, run_question
from understory.harness.golden import GoldenItem
from understory.server.service import Service
from understory.server.session import extract_numbers

TOLERANCE = 0.005
"""Relative slack on an expected number, on top of rounding to the shown precision."""

_SUFFIX_SCALE = {
    "k": 1e3,
    "thousand": 1e3,
    "m": 1e6,
    "million": 1e6,
    "b": 1e9,
    "bn": 1e9,
    "billion": 1e9,
}

METRIC_NAMES = ("coverage", "resolution", "answer", "disclosure", "capture")


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


class ItemScore(BaseModel):
    id: str
    question: str
    passed: bool = False
    reasons: list[str] = Field(default_factory=list)
    """Why it failed. Empty on a pass."""

    expected_status: str = ""
    observed_status: str = ""

    coverage: bool | None = None
    resolution: bool | None = None
    answer: bool | None = None
    disclosure: bool | None = None
    capture: bool | None = None
    """None means the metric does not apply to this item."""

    tool_calls: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    clarifications: list[str] = Field(default_factory=list)
    answer_text: str = ""
    log_answer_status: str | None = None
    usage: dict[str, int] = Field(default_factory=dict)
    elapsed_ms: int = 0
    error: str | None = None


class EvalReport(BaseModel):
    tenant: str
    mode: Literal["agent", "deterministic"]
    model: str
    started_at: datetime
    finished_at: datetime
    items: list[ItemScore] = Field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        n = len(self.items)
        out: dict[str, Any] = {
            "tenant": self.tenant,
            "mode": self.mode,
            "model": self.model,
            "items": n,
            "passed": sum(1 for i in self.items if i.passed),
            "pass_rate": _rate([i.passed for i in self.items]),
            "duration_s": round((self.finished_at - self.started_at).total_seconds(), 1),
        }
        for name in METRIC_NAMES:
            values = [v for v in (getattr(i, name) for i in self.items) if v is not None]
            out[f"{name}_rate"] = _rate(values)
            out[f"{name}_n"] = len(values)
        tokens = sum(i.usage.get("total_tokens", 0) for i in self.items)
        if tokens:
            out["total_tokens"] = tokens
        out["failures"] = [i.id for i in self.items if not i.passed]
        return out

    def markdown(self) -> str:
        s = self.summary()
        head = (
            f"### {self.tenant} - {self.mode} - {self.model}\n\n"
            f"{s['passed']}/{s['items']} passed"
            f" | coverage {_pct(s['coverage_rate'])}"
            f" | resolution {_pct(s['resolution_rate'])}"
            f" | answer {_pct(s['answer_rate'])}"
            f" | disclosure {_pct(s['disclosure_rate'])}"
            f" | capture {_pct(s['capture_rate'])}"
            f" | {s['duration_s']}s\n\n"
        )
        rows = [
            "| item | expected | observed | cov | res | ans | dis | cap | ok | why |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for i in self.items:
            rows.append(
                f"| {i.id} | {i.expected_status} | {i.observed_status} "
                f"| {_mark(i.coverage)} | {_mark(i.resolution)} | {_mark(i.answer)} "
                f"| {_mark(i.disclosure)} | {_mark(i.capture)} "
                f"| {'pass' if i.passed else 'FAIL'} "
                f"| {'; '.join(i.reasons).replace('|', '/')[:120]} |"
            )
        return head + "\n".join(rows) + "\n"


def write_report(report: EvalReport, tenant_root: Path) -> Path:
    """Write the JSON report under `<tenant>/.evals/`. Returns the path."""
    stamp = report.started_at.strftime("%Y%m%dT%H%M%SZ")
    name = f"{stamp}-{_slug(report.model)}.json"
    out = Path(tenant_root) / ".evals" / name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.model_dump_json(indent=2))
    return out


# --------------------------------------------------------------------------- #
# The agent eval
# --------------------------------------------------------------------------- #


def run_evals(
    service: Service,
    items: list[GoldenItem],
    *,
    model: str | Model = DEFAULT_MODEL,
    api_key: str | None = None,
    concurrency: int = 1,
) -> EvalReport:
    """Run every golden item through the agent and score it."""
    started = datetime.now(UTC)
    name = model_id(model)

    def one(item: GoldenItem) -> ItemScore:
        return _score_agent_item(service, item, model=model, api_key=api_key)

    if concurrency > 1:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            scores = list(pool.map(one, items))
    else:
        scores = [one(item) for item in items]

    return EvalReport(
        tenant=service.tenant.name,
        mode="agent",
        model=name,
        started_at=started,
        finished_at=datetime.now(UTC),
        items=scores,
    )


def _score_agent_item(
    service: Service,
    item: GoldenItem,
    *,
    model: str | Model,
    api_key: str | None,
) -> ItemScore:
    t0 = time.monotonic()
    exp = item.expected
    key = f"eval:{item.id}"
    first = run_question(service, item.question, model=model, api_key=api_key, session_key=key)

    asked = [c.trap for c in first.clarifications]
    final = first
    if exp.status == "needs_clarification" and exp.answers:
        final = run_question(
            service,
            item.question,
            model=model,
            api_key=api_key,
            session_key=key,
            clarification_answers=exp.answers,
        )

    score = ItemScore(
        id=item.id,
        question=item.question,
        expected_status=exp.status,
        observed_status=_observed(first, final),
        tool_calls=[c.name for c in final.tool_calls],
        metrics=final.metrics_queried,
        clarifications=asked,
        answer_text=final.answer,
        log_answer_status=final.log_answer.status if final.log_answer else None,
        usage=_merge_usage(first, final),
        elapsed_ms=int((time.monotonic() - t0) * 1000),
        error=final.error or first.error,
    )
    reasons: list[str] = []
    if score.error:
        reasons.append(score.error)

    # Resolution accuracy.
    if exp.status == "needs_clarification":
        score.resolution = set(asked) == set(exp.clarification_traps)
        if not score.resolution:
            reasons.append(f"asked {asked or 'nothing'}, expected {exp.clarification_traps}")
    elif exp.status in ("unanswerable", "invalid"):
        score.resolution = exp.status in final.statuses
        if not score.resolution:
            reasons.append(f"statuses {final.statuses}, expected {exp.status}")
    else:
        score.resolution = not asked and "resolved" in final.statuses
        if not score.resolution:
            reasons.append(
                f"asked {asked}" if asked else f"never resolved; statuses {final.statuses}"
            )

    # Coverage.
    if exp.resolves and exp.governed:
        used_sql = any(c.name == "run_sql" for c in final.tool_calls)
        score.coverage = bool(final.governed) and not used_sql
        if not score.coverage:
            reasons.append("fell to run_sql" if used_sql else "no governed rows")
        if exp.metrics and set(exp.metrics) - set(final.metrics_queried):
            score.coverage = False
            reasons.append(f"queried {final.metrics_queried}, expected {exp.metrics}")

    # Answer accuracy.
    if exp.numbers:
        missing = numbers_missing(exp.numbers, extract_numbers(final.answer))
        score.answer = not missing
        if missing:
            reasons.append(f"numbers missing from answer: {missing}")

    # Disclosure rate.
    if exp.disclosures:
        missing_text = [d for d in exp.disclosures if d.lower() not in final.answer.lower()]
        score.disclosure = not missing_text
        if missing_text:
            reasons.append(f"disclosures missing: {missing_text}")

    # Capture rate.
    score.capture = any(c.name == "log_answer" for c in final.tool_calls)
    if not score.capture:
        reasons.append("log_answer never called")

    score.reasons = reasons
    score.passed = _passed(score)
    return score


# --------------------------------------------------------------------------- #
# Shared scoring helpers
# --------------------------------------------------------------------------- #


def numbers_missing(
    expected: list[float], found: list[tuple[str, float]], tolerance: float = TOLERANCE
) -> list[float]:
    """Expected values with no plausible rendering among `found`.

    `found` is (literal, value) pairs, as `extract_numbers` returns them.
    """
    return [e for e in expected if not any(_close(v, e, lit, tolerance) for lit, v in found)]


def _close(shown: float, expected: float, literal: str, tolerance: float) -> bool:
    """Could `literal`, read as `shown`, be a rendering of `expected`?"""
    candidates = [expected]
    if literal.strip().endswith("%"):
        candidates.append(expected * 100)
    for target in candidates:
        if shown == target:
            return True
        if target != 0 and abs(shown - target) / abs(target) <= tolerance:
            return True
        if abs(shown - target) <= _rounding_tolerance(literal):
            return True
    return False


def _rounding_tolerance(literal: str) -> float:
    body = literal.strip().lower().rstrip("%").strip()
    scale = 1.0
    for suffix, mult in _SUFFIX_SCALE.items():
        if body.endswith(suffix):
            scale = mult
            body = body[: -len(suffix)].strip()
            break
    decimals = len(body.split(".")[1]) if "." in body else 0
    return 0.5 * (10 ** (-decimals)) * scale


def _passed(score: ItemScore) -> bool:
    if score.error:
        return False
    return all(getattr(score, name) is not False for name in METRIC_NAMES)


def _observed(first: Turn, final: Turn) -> str:
    if first.clarifications:
        return "needs_clarification" if final is first else "clarified"
    for status in ("unanswerable", "invalid", "too_broad", "sql_rejected"):
        if status in final.statuses:
            return status
    if "resolved" in final.statuses:
        return "resolved"
    return final.statuses[-1] if final.statuses else "none"


def _merge_usage(first: Turn, final: Turn) -> dict[str, int]:
    out = dict(first.usage)
    if final is not first:
        for k, v in final.usage.items():
            out[k] = out.get(k, 0) + v
    return out


def _rate(values: list[bool]) -> float | None:
    if not values:
        return None
    return round(sum(1 for v in values if v) / len(values), 4)


def _pct(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate * 100:.0f}%"


def _mark(value: bool | None) -> str:
    return "-" if value is None else ("y" if value else "N")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "model"


__all__ = [
    "METRIC_NAMES",
    "TOLERANCE",
    "EvalReport",
    "ItemScore",
    "numbers_missing",
    "run_evals",
    "write_report",
]
