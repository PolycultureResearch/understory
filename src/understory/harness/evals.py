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

A live run spends real money, so `run_evals` guards it. It reads the key's
remaining credit before the first item, refuses to start when the balance will
not cover the set, and stops at the first out of credits error rather than
burning a request per remaining item to collect the same failure.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
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

BUDGET_PER_ITEM = 0.06
"""USD one golden item cost when measured, uncached, on claude-sonnet-5."""

KEY_URL = "https://openrouter.ai/api/v1/auth/key"
"""Where OpenRouter reports a key's usage and remaining credit."""

STOPPED_REASON = "stopped: out of credits"
"""Why an item never ran. Set on every item after a 402."""

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
    usage: dict[str, float] = Field(default_factory=dict)
    """Token counters, `requests`, `tool_calls` and `cost` in USD, summed over both turns."""
    elapsed_ms: int = 0
    error: str | None = None
    error_status: int | None = None
    """HTTP status of a provider error, when it had one. 402 means out of credits."""
    skipped: bool = False
    """True when the run stopped before this item, so nothing was measured."""


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
        out.update(self.usage_summary())
        skipped = [i.id for i in self.items if i.skipped]
        if skipped:
            out["skipped"] = skipped
            out["stopped"] = STOPPED_REASON
        out["failures"] = [i.id for i in self.items if not i.passed]
        return out

    def usage_summary(self) -> dict[str, Any]:
        """Token and cost totals, plus per item averages over the items that ran.

        An item that never ran contributes nothing and is left out of the
        denominator, so the averages describe what a question actually costs.
        """
        measured = [i.usage for i in self.items if i.usage]
        if not measured:
            return {}
        out: dict[str, Any] = {}
        for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            total = int(sum(u.get(key, 0) for u in measured))
            out[key] = total
            out[f"avg_{key}"] = round(total / len(measured), 1)
        out["total_tokens"] = int(sum(u.get("total_tokens", 0) for u in measured))
        out["requests"] = int(sum(u.get("requests", 0) for u in measured))
        cost = sum(u.get("cost", 0.0) for u in measured)
        if cost:
            out["cost_usd"] = round(cost, 6)
            out["avg_cost_usd"] = round(cost / len(measured), 6)
        out["measured_items"] = len(measured)
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
        if "input_tokens" in s:
            head += (
                f"tokens in {s['input_tokens']:,} (cache read {s['cache_read_tokens']:,}, "
                f"write {s['cache_write_tokens']:,}) out {s['output_tokens']:,}"
                f" | {_cents(s.get('cost_usd'))} over {s['measured_items']} items,"
                f" {_cents(s.get('avg_cost_usd'))} each\n\n"
            )
        if "stopped" in s:
            head += f"{s['stopped']}: {', '.join(s['skipped'])}\n\n"
        rows = [
            "| item | expected | observed | cov | res | ans | dis | cap | ok | cents | why |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for i in self.items:
            rows.append(
                f"| {i.id} | {i.expected_status} | {i.observed_status} "
                f"| {_mark(i.coverage)} | {_mark(i.resolution)} | {_mark(i.answer)} "
                f"| {_mark(i.disclosure)} | {_mark(i.capture)} "
                f"| {'pass' if i.passed else 'FAIL'} "
                f"| {_cents(i.usage.get('cost'))} "
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
# Budget guards
# --------------------------------------------------------------------------- #


class BudgetError(RuntimeError):
    """The key's remaining credit will not cover the set, and `force` was not set."""


def key_status(api_key: str | None = None, *, timeout: float = 15.0) -> dict[str, Any]:
    """What OpenRouter says about the key: usage, limit, limit_remaining.

    Returns an empty dict when there is no key or the call fails, because a
    balance we cannot read is not a reason to refuse to run.
    """
    key = api_key or os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return {}
    request = urllib.request.Request(KEY_URL, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return {}
    data = payload.get("data") if isinstance(payload, dict) else None
    return data if isinstance(data, dict) else {}


def check_budget(
    items: int,
    *,
    api_key: str | None = None,
    per_item: float = BUDGET_PER_ITEM,
    force: bool = False,
    echo: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Read the key's balance, report it, and refuse a run it cannot cover.

    Raises `BudgetError` when `limit_remaining` is below `items * per_item`. A
    key with no spend limit reports `limit_remaining: null`, which is unlimited
    credit and always passes, as does a balance we could not read at all.
    """
    status = key_status(api_key)
    estimate = items * per_item
    if echo is not None:
        if status:
            echo(
                f"openrouter key: usage ${_money(status.get('usage'))}"
                f" limit {_limit(status.get('limit'))}"
                f" remaining {_limit(status.get('limit_remaining'))}"
                f" | {items} items at ${per_item:.3f} each needs ${estimate:.2f}"
            )
        else:
            echo(f"openrouter key: balance unknown | {items} items needs about ${estimate:.2f}")

    remaining = status.get("limit_remaining")
    if not isinstance(remaining, int | float):
        return status
    if remaining >= estimate:
        return status
    if force:
        if echo is not None:
            echo(f"--force: starting anyway with ${remaining:.2f} left")
        return status
    raise BudgetError(
        f"${remaining:.2f} of credit left, but {items} items at ${per_item:.3f} each need "
        f"about ${estimate:.2f}. Lower --limit, lower --budget-per-item, or pass --force."
    )


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
    budget_per_item: float = BUDGET_PER_ITEM,
    force: bool = False,
    echo: Callable[[str], None] | None = None,
) -> EvalReport:
    """Run every golden item through the agent and score it.

    A run against a real model id checks the key's balance first and raises
    `BudgetError` rather than starting a set it cannot pay for. A `Model`
    instance skips the check, because the tests pass one and spend nothing.

    The first out of credits error stops the run. Every item after it comes back
    `skipped`, so a half funded run produces a report that says which questions
    were never asked instead of one 402 per remaining item.
    """
    if isinstance(model, str):
        check_budget(len(items), api_key=api_key, per_item=budget_per_item, force=force, echo=echo)

    started = datetime.now(UTC)
    name = model_id(model)
    stop = threading.Event()
    lock = threading.Lock()
    spent = 0.0

    def one(item: GoldenItem) -> ItemScore:
        nonlocal spent
        if stop.is_set():
            return _skipped_score(item)
        score = _score_agent_item(service, item, model=model, api_key=api_key)
        if _out_of_credits(score):
            stop.set()
        with lock:
            spent += score.usage.get("cost", 0.0)
            if echo is not None:
                echo(_progress(score, spent))
        return score

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


def _skipped_score(item: GoldenItem) -> ItemScore:
    return ItemScore(
        id=item.id,
        question=item.question,
        expected_status=item.expected.status,
        observed_status="skipped",
        skipped=True,
        passed=False,
        reasons=[STOPPED_REASON],
        error=STOPPED_REASON,
    )


def _out_of_credits(score: ItemScore) -> bool:
    """Did the provider refuse this item for money rather than for content?"""
    if score.error_status == 402:
        return True
    text = (score.error or "").lower()
    return "insufficient credit" in text or ("402" in text and "credit" in text)


def _progress(score: ItemScore, spent: float) -> str:
    cost = score.usage.get("cost")
    money = f" {_cents(cost)}, ${spent:.4f} so far" if cost else ""
    return f"  {score.id}: {'pass' if score.passed else 'FAIL'} {score.observed_status}{money}"


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
    if exp.status == "needs_clarification" and exp.answers and not first.error:
        final = run_question(
            service,
            _answer_prompt(exp.answers),
            model=model,
            api_key=api_key,
            session_key=key,
            message_history=first.messages,
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
        error_status=final.error_status or first.error_status,
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
        # A model that reads get_context and refuses without querying is right
        # too. The server never saw it, which the design notes as a telemetry
        # gap, but the answer is correct, so it scores as a prose refusal.
        refused_in_prose = "resolved" not in final.statuses and not asked and not final.governed
        score.resolution = exp.status in final.statuses or refused_in_prose
        if refused_in_prose and exp.status not in final.statuses:
            score.observed_status = "refused_in_prose"
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
        answer_norm = _norm_text(final.answer)
        missing_text = [d for d in exp.disclosures if _norm_text(d) not in answer_norm]
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


def _answer_prompt(answers: dict[str, str]) -> str:
    """The user's reply to a clarification, as the next message of the same chat."""
    pairs = "; ".join(f"{trap} = {choice}" for trap, choice in answers.items())
    return (
        f"My answers: {pairs}. Run the query again with those as the clarifications "
        "field and give me the number."
    )


def _merge_usage(first: Turn, final: Turn) -> dict[str, float]:
    out = dict(first.usage)
    if final is not first:
        for k, v in final.usage.items():
            out[k] = out.get(k, 0) + v
    return out


def _norm_text(text: str) -> str:
    """Lowercase, hyphens and underscores to spaces, whitespace collapsed."""
    return re.sub(r"\s+", " ", re.sub(r"[-_]", " ", text.lower())).strip()


def _rate(values: list[bool]) -> float | None:
    if not values:
        return None
    return round(sum(1 for v in values if v) / len(values), 4)


def _pct(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate * 100:.0f}%"


def _mark(value: bool | None) -> str:
    return "-" if value is None else ("y" if value else "N")


def _cents(usd: float | None) -> str:
    return "-" if not usd else f"{usd * 100:.2f}c"


def _money(value: Any) -> str:
    return f"{value:.4f}" if isinstance(value, int | float) else "?"


def _limit(value: Any) -> str:
    return f"${value:.2f}" if isinstance(value, int | float) else "none"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "model"


__all__ = [
    "BUDGET_PER_ITEM",
    "KEY_URL",
    "METRIC_NAMES",
    "STOPPED_REASON",
    "TOLERANCE",
    "BudgetError",
    "EvalReport",
    "ItemScore",
    "check_budget",
    "key_status",
    "numbers_missing",
    "run_evals",
    "write_report",
]
