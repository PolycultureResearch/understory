"""Golden question sets: `tenants/<name>/golden/questions.yml`.

A golden item is a question plus what a correct run does with it. Because the
fake_companies data is seed deterministic, the expected numbers are exact, not
ranges. An item records four things the evals score separately: whether the
question resolved or was refused, whether the right clarification was asked,
which governed metrics the final query used, and which numbers and disclosures
the answer has to carry.

Each item also records `spec`, the metric spec the question should resolve to.
That is what `deterministic.py` runs without a model, so CI can validate the
golden sets and the server together with no API key.

Shape:

```yaml
version: 1
questions:
  - id: net_revenue_q1
    question: "What was net revenue by month from January through March 2025?"
    spec:
      metrics: [net_revenue]
      group_by: [metric_time__month]
      time: {grain: month, start: 2025-01-01, end: 2025-03-31}
    expected:
      status: resolved
      metrics: [net_revenue]
      numbers: [123456.78, 234567.89, 345678.90]
      disclosures: ["2025-01-01 to 2025-03-31"]
    notes: "The metric is named directly, so the revenue collision must not fire."
```
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from understory.types import MetricSpec

GoldenStatus = Literal["resolved", "needs_clarification", "unanswerable", "invalid"]


class Expectation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: GoldenStatus = "resolved"
    clarification_traps: list[str] = Field(default_factory=list)
    """Trap ids the first turn must ask about, exactly. Only for needs_clarification."""
    answers: dict[str, str] = Field(default_factory=dict)
    """Trap id to option id. Used as the user's reply on the second turn."""
    metrics: list[str] = Field(default_factory=list)
    """Governed metrics the final query must use. Empty means do not check."""
    governed: bool = True
    """False for a question the semantic layer cannot cover, answered by run_sql."""
    numbers: list[float] = Field(default_factory=list)
    """Values that must appear in the rendered answer. Only set for resolved items."""
    disclosures: list[str] = Field(default_factory=list)
    """Substrings that must appear in the answer, matched case-insensitively."""

    @property
    def resolves(self) -> bool:
        """Does a correct run end with rows, possibly after a clarification?"""
        return self.status == "resolved" or (
            self.status == "needs_clarification" and bool(self.answers)
        )


class GoldenItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    question: str
    expected: Expectation = Field(default_factory=Expectation)
    spec: dict[str, Any] | None = None
    """The MetricSpec the question should resolve to. Required for the deterministic check."""
    notes: str | None = None

    def metric_spec(self, *, with_answers: bool = False) -> MetricSpec:
        """The item's spec as a model, optionally carrying the clarification answers."""
        if self.spec is None:
            raise ValueError(f"golden item {self.id!r} has no spec")
        raw = dict(self.spec)
        raw.setdefault("question", self.question)
        if with_answers and self.expected.answers:
            raw["clarifications"] = [
                {"trap": trap, "choice": choice} for trap, choice in self.expected.answers.items()
            ]
        return MetricSpec.model_validate(raw)


class GoldenSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    questions: list[GoldenItem] = Field(default_factory=list)


def load_golden(path: str | Path) -> list[GoldenItem]:
    """Load a tenant's golden set. A missing file is an empty set, not an error."""
    p = Path(path)
    if not p.exists():
        return []
    raw = yaml.safe_load(p.read_text())
    if raw is None:
        return []
    if isinstance(raw, list):
        raw = {"questions": raw}
    if not isinstance(raw, dict):
        raise ValueError(f"{p}: expected a mapping or a list at the top level")
    items = GoldenSet.model_validate(raw).questions
    seen: set[str] = set()
    for item in items:
        if item.id in seen:
            raise ValueError(f"{p}: duplicate golden item id {item.id!r}")
        seen.add(item.id)
    return items


__all__ = ["Expectation", "GoldenItem", "GoldenSet", "GoldenStatus", "load_golden"]
