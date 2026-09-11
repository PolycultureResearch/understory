"""Telemetry: write-only Parquet log of what the server did, in two families.

See `events` for the models and Parquet schemas, `writer` for the append
writer and its lifecycle, and `hashing` for user pseudonymisation.
"""

from understory.telemetry.events import (
    EVENT_TYPES,
    EVENTS_SCHEMA,
    TEXT_SCHEMA,
    AnswerLogged,
    BaseEvent,
    ClarificationApplied,
    ClarificationReturned,
    Event,
    QueryExecuted,
    RefusalReason,
    Refused,
    TextRecord,
    ToolCalled,
)
from understory.telemetry.hashing import user_hash
from understory.telemetry.writer import TelemetryWriter

__all__ = [
    "EVENTS_SCHEMA",
    "EVENT_TYPES",
    "TEXT_SCHEMA",
    "AnswerLogged",
    "BaseEvent",
    "ClarificationApplied",
    "ClarificationReturned",
    "Event",
    "QueryExecuted",
    "RefusalReason",
    "Refused",
    "TelemetryWriter",
    "TextRecord",
    "ToolCalled",
    "user_hash",
]
