"""Telemetry event models and the Parquet schemas they are written with.

Two families, two prefixes, two access levels (design section 8.3).

The events family carries no free text. Metric and dimension ids, trap ids,
option ids, status names, hashes, counts and latencies only. The `phrase` on a
`refused` event is the registry phrase that fired the trap, which is a
controlled vocabulary from traps.yml, not the user's words. Question text,
specs, compiled SQL and draft answers go to the text family under the same
`event_id`, so a reader with access to both can join them and a reader with
access to events alone never sees text.

Events Parquet schema (one row per event)

    event_id     string      uuid4 hex, shared with the text record
    tenant       string
    user_hash    string      HMAC of the connector subject, see hashing.py
    session_id   string      the server's per-connection session id
    ts           timestamp[us, tz=UTC]
    event        string      one of the event names below
    tool         string      tool_called only, else null
    status       string      tool_called only, else null
    governed     bool        query_executed only, else null
    latency_ms   int64       tool_called and query_executed, else null
    trap_id      string      clarification_returned and clarification_applied
    reason       string      refused only
    payload      string      JSON object with every event-specific field

The promoted columns (tool, status, governed, latency_ms, trap_id, reason) are
duplicated inside `payload` so the payload is self-contained; they exist as
columns because they are the fields the marts filter and group on most.

Event names and payload fields

    tool_called             tool, status, latency_ms
    clarification_returned  trap_id, options (list of option ids)
    clarification_applied   trap_id, choice
    query_executed          metrics, dimensions, sql_hash, spec_hash, row_count,
                            governed, cache_hit, truncated, latency_ms
    refused                 reason, phrase
    answer_logged           numbers_checked, numbers_unsourced,
                            disclosures_present, disclosures_missing

Text Parquet schema (at most one row per event)

    event_id      string
    tenant        string
    ts            timestamp[us, tz=UTC]
    question      string   nullable
    spec_json     string   nullable, the MetricSpec as JSON
    sql           string   nullable, compiled or submitted SQL
    draft_answer  string   nullable, the draft passed to log_answer

Files under both prefixes are laid out as `dt=YYYY-MM-DD/<ts>-<uuid>.parquet`.
The `dt` partition is the UTC date at write time and is meant for retention
and lifecycle rules; models should use `ts` for event dates.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

import pyarrow as pa
from pydantic import BaseModel, Field

RefusalReason = Literal["unanswerable", "invalid", "uncovered", "sql_rejected", "too_broad"]

COMMON_FIELDS = frozenset({"event_id", "tenant", "user_hash", "session_id", "ts", "event"})


def new_event_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(UTC)


class BaseEvent(BaseModel):
    """Fields every event carries. Subclasses add the event-specific ones."""

    event_id: str = Field(default_factory=new_event_id)
    tenant: str = ""
    """Filled in by the writer when left empty; a writer serves one tenant."""
    user_hash: str
    session_id: str
    ts: datetime = Field(default_factory=utcnow)
    event: str

    def payload(self) -> dict[str, Any]:
        """The event-specific fields, JSON-ready."""
        return self.model_dump(mode="json", exclude=set(COMMON_FIELDS))

    def payload_json(self) -> str:
        return json.dumps(self.payload(), sort_keys=True, separators=(",", ":"))


class ToolCalled(BaseEvent):
    event: Literal["tool_called"] = "tool_called"
    tool: str
    status: str
    """A `types.Status` value, or `error`."""
    latency_ms: int


class ClarificationReturned(BaseEvent):
    event: Literal["clarification_returned"] = "clarification_returned"
    trap_id: str
    options: list[str] = Field(default_factory=list)
    """Option ids offered, in the order shown."""


class ClarificationApplied(BaseEvent):
    event: Literal["clarification_applied"] = "clarification_applied"
    trap_id: str
    choice: str
    """Option id the user picked."""


class QueryExecuted(BaseEvent):
    event: Literal["query_executed"] = "query_executed"
    metrics: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    sql_hash: str | None = None
    spec_hash: str | None = None
    row_count: int = 0
    governed: bool
    cache_hit: bool = False
    truncated: bool = False
    latency_ms: int


class Refused(BaseEvent):
    event: Literal["refused"] = "refused"
    reason: RefusalReason
    phrase: str | None = None
    """Registry phrase that fired, never user text."""


class AnswerLogged(BaseEvent):
    event: Literal["answer_logged"] = "answer_logged"
    numbers_checked: int = 0
    numbers_unsourced: int = 0
    disclosures_present: int = 0
    disclosures_missing: int = 0


Event = (
    ToolCalled
    | ClarificationReturned
    | ClarificationApplied
    | QueryExecuted
    | Refused
    | AnswerLogged
)

EVENT_TYPES: tuple[type[BaseEvent], ...] = (
    ToolCalled,
    ClarificationReturned,
    ClarificationApplied,
    QueryExecuted,
    Refused,
    AnswerLogged,
)


class TextRecord(BaseModel):
    """The text-bearing side of an event. Written to the text prefix only."""

    event_id: str
    tenant: str = ""
    ts: datetime = Field(default_factory=utcnow)
    question: str | None = None
    spec_json: str | None = None
    sql: str | None = None
    draft_answer: str | None = None


_TS = pa.timestamp("us", tz="UTC")

EVENTS_SCHEMA = pa.schema(
    [
        pa.field("event_id", pa.string(), nullable=False),
        pa.field("tenant", pa.string(), nullable=False),
        pa.field("user_hash", pa.string(), nullable=False),
        pa.field("session_id", pa.string(), nullable=False),
        pa.field("ts", _TS, nullable=False),
        pa.field("event", pa.string(), nullable=False),
        pa.field("tool", pa.string()),
        pa.field("status", pa.string()),
        pa.field("governed", pa.bool_()),
        pa.field("latency_ms", pa.int64()),
        pa.field("trap_id", pa.string()),
        pa.field("reason", pa.string()),
        pa.field("payload", pa.string(), nullable=False),
    ]
)

TEXT_SCHEMA = pa.schema(
    [
        pa.field("event_id", pa.string(), nullable=False),
        pa.field("tenant", pa.string(), nullable=False),
        pa.field("ts", _TS, nullable=False),
        pa.field("question", pa.string()),
        pa.field("spec_json", pa.string()),
        pa.field("sql", pa.string()),
        pa.field("draft_answer", pa.string()),
    ]
)

PROMOTED_FIELDS = ("tool", "status", "governed", "latency_ms", "trap_id", "reason")


def events_to_table(events: list[BaseEvent]) -> pa.Table:
    """Flatten events into a table matching EVENTS_SCHEMA."""
    rows: dict[str, list[Any]] = {name: [] for name in EVENTS_SCHEMA.names}
    for ev in events:
        payload = ev.payload()
        rows["event_id"].append(ev.event_id)
        rows["tenant"].append(ev.tenant)
        rows["user_hash"].append(ev.user_hash)
        rows["session_id"].append(ev.session_id)
        rows["ts"].append(_as_utc(ev.ts))
        rows["event"].append(ev.event)
        for name in PROMOTED_FIELDS:
            rows[name].append(payload.get(name))
        rows["payload"].append(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return pa.Table.from_pydict(rows, schema=EVENTS_SCHEMA)


def text_to_table(records: list[TextRecord]) -> pa.Table:
    rows: dict[str, list[Any]] = {name: [] for name in TEXT_SCHEMA.names}
    for rec in records:
        rows["event_id"].append(rec.event_id)
        rows["tenant"].append(rec.tenant)
        rows["ts"].append(_as_utc(rec.ts))
        rows["question"].append(rec.question)
        rows["spec_json"].append(rec.spec_json)
        rows["sql"].append(rec.sql)
        rows["draft_answer"].append(rec.draft_answer)
    return pa.Table.from_pydict(rows, schema=TEXT_SCHEMA)


def _as_utc(ts: datetime) -> datetime:
    """Naive datetimes are taken as UTC; aware ones are converted."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)
