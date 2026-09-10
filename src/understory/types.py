"""Shared types for every Understory module.

These are the contracts the modules agree on. The catalog produces `Catalog`,
the traps matcher consumes `MetricSpec` and produces `Clarification`s, the
semantic layer turns a `MetricSpec` into a `CompiledQuery`, the warehouse turns
SQL into a `Result`, and the server wraps everything in a `ToolResponse`.

Keep this file small and stable. Adding a field is fine; renaming one breaks
every module at once.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# Catalog: what the semantic layer exposes
# --------------------------------------------------------------------------- #


class DimensionInfo(BaseModel):
    """A dimension as the chatbot may reference it, e.g. `order__country`."""

    name: str
    """Fully qualified `entity__dimension` name usable in `group_by`/`where`."""
    label: str | None = None
    description: str | None = None
    type: Literal["categorical", "time"]
    time_granularity: str | None = None
    semantic_model: str
    relation: str
    """Warehouse relation the dimension lives on, quoted, e.g. "db"."schema"."table"."""
    column: str
    """Expression or column on that relation."""


class MetricInfo(BaseModel):
    name: str
    label: str | None = None
    description: str | None = None
    type: Literal["simple", "ratio", "derived", "cumulative", "conversion"]
    expr: str | None = None
    """Human-readable definition: measure with aggregation, or the derived formula."""
    filters: list[str] = Field(default_factory=list)
    """Filters baked into the metric definition, as written in YAML."""
    inputs: list[str] = Field(default_factory=list)
    """Other metrics this one is built from (derived/ratio)."""
    dimensions: list[str] = Field(default_factory=list)
    """Dimension names (`entity__dimension`) valid for this metric."""
    time_dimension: str | None = None
    """Agg time dimension name, e.g. `order__order_date`."""
    relations: list[str] = Field(default_factory=list)
    synonyms: list[str] = Field(default_factory=list)
    """From `config.meta.polyculture.synonyms` if present."""
    meta: dict[str, Any] = Field(default_factory=dict)


class Catalog(BaseModel):
    metrics: dict[str, MetricInfo]
    dimensions: dict[str, DimensionInfo]
    time_spine: str | None = None
    """Relation of the metricflow time spine, if declared."""

    def metric(self, name: str) -> MetricInfo | None:
        return self.metrics.get(name)

    def nearest_metrics(self, name: str, n: int = 3) -> list[str]:
        """Cheap fuzzy match for `invalid` responses."""
        import difflib

        return difflib.get_close_matches(name, list(self.metrics), n=n, cutoff=0.4)

    def nearest_dimensions(self, name: str, n: int = 3) -> list[str]:
        import difflib

        return difflib.get_close_matches(name, list(self.dimensions), n=n, cutoff=0.4)


# --------------------------------------------------------------------------- #
# Query spec: what the chatbot sends to query_metrics
# --------------------------------------------------------------------------- #

WhereOp = Literal["eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte"]


class WhereClause(BaseModel):
    dimension: str
    op: WhereOp
    values: list[str | int | float]


class TimeSpec(BaseModel):
    grain: Literal["day", "week", "month", "quarter", "year"] | None = None
    start: date | None = None
    end: date | None = None
    """Omitted `end` resolves to the metric's latest available date, never today."""


class ClarificationChoice(BaseModel):
    trap: str
    """Trap id from the registry."""
    choice: str
    """Option id the user picked."""


class MetricSpec(BaseModel):
    metrics: list[str]
    group_by: list[str] = Field(default_factory=list)
    where: list[WhereClause] = Field(default_factory=list)
    time: TimeSpec = Field(default_factory=TimeSpec)
    question: str | None = None
    """The user's question, for the traps matcher and the log. Optional."""
    clarifications: list[ClarificationChoice] = Field(default_factory=list)
    limit: int | None = None

    def hash(self) -> str:
        """Stable hash of the parts that affect SQL. Question and clarifications excluded."""
        payload = self.model_dump(mode="json", exclude={"question", "clarifications"})
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Traps: what the registry returns
# --------------------------------------------------------------------------- #


class Option(BaseModel):
    id: str
    label: str
    hint: str | None = None


class Clarification(BaseModel):
    trap: str
    slot: Literal["metric", "dimension", "convention"]
    phrase: str
    """The phrase in the question or spec that fired the trap."""
    options: list[Option]
    priority: int = 100


class Disclosure(BaseModel):
    text: str
    source: str
    """Trap id, dimension name, or convention name that produced it."""


class Refusal(BaseModel):
    reason: Literal["unanswerable", "invalid", "uncovered", "sql_rejected", "too_broad"]
    message: str
    phrase: str | None = None
    suggestions: list[str] = Field(default_factory=list)


class TrapsOutcome(BaseModel):
    """What the matcher decided for one spec. The server turns this into a status."""

    spec: MetricSpec
    """The spec after `prefer` policies were applied."""
    clarifications: list[Clarification] = Field(default_factory=list)
    disclosures: list[Disclosure] = Field(default_factory=list)
    refusal: Refusal | None = None
    fired: list[str] = Field(default_factory=list)
    """Every trap id that matched, satisfied or not, for the log."""


# --------------------------------------------------------------------------- #
# Compile and execute
# --------------------------------------------------------------------------- #


class CompiledQuery(BaseModel):
    sql: str
    spec_hash: str
    sql_hash: str
    dialect: str
    metrics: list[str]
    cache_hit: bool = False


class Column(BaseModel):
    name: str
    type: str


class Result(BaseModel):
    columns: list[Column]
    rows: list[list[Any]]
    row_count: int
    """Rows returned, after the cap."""
    truncated: bool = False
    elapsed_ms: int = 0


# --------------------------------------------------------------------------- #
# Tool responses
# --------------------------------------------------------------------------- #


class Status(StrEnum):
    resolved = "resolved"
    needs_clarification = "needs_clarification"
    unanswerable = "unanswerable"
    invalid = "invalid"
    uncovered = "uncovered"
    too_broad = "too_broad"
    sql_rejected = "sql_rejected"
    error = "error"


class Provenance(BaseModel):
    governed: bool
    """True when the SQL came from the semantic layer. False for run_sql."""
    metrics: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    relations: list[str] = Field(default_factory=list)
    sql: str | None = None
    """Included for run_sql always, and for query_metrics when the tenant allows it."""
    sql_hash: str | None = None
    spec_hash: str | None = None
    data_through: date | None = None
    """Latest date available for the primary metric's time dimension."""
    applied_time: TimeSpec | None = None
    cache_hit: bool = False
    semantic_layer: str | None = None
    """`metricflow_local` or `dbt_cloud`."""


class ToolResponse(BaseModel):
    status: Status
    result: Result | None = None
    clarifications: list[Clarification] = Field(default_factory=list)
    required_disclosures: list[str] = Field(default_factory=list)
    refusal: Refusal | None = None
    provenance: Provenance | None = None
    result_id: str | None = None
    """Id the session store uses so log_answer can trace numbers back."""
    how_to_read: str | None = None
    """Rendering guidance for the chatbot. Short and imperative."""


class NumberCheck(BaseModel):
    number: str
    """As it appeared in the draft."""
    value: float
    sourced: bool
    result_id: str | None = None
    column: str | None = None


class AnswerReview(BaseModel):
    status: Literal["pass", "unsourced_numbers", "no_results_in_session"]
    checked: list[NumberCheck] = Field(default_factory=list)
    unsourced: list[NumberCheck] = Field(default_factory=list)
    disclosures_present: list[str] = Field(default_factory=list)
    disclosures_missing: list[str] = Field(default_factory=list)
