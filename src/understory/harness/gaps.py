"""Gap promotion: the backlog becomes golden items (design 10.4).

The server writes a gap every time a question falls back or is refused, and
`mart_semantic_backlog` in the `dbt_understory` package groups them on what
was missing. This module reads that mart for one tenant and drafts a golden
item per row: the question someone asked, how often and by how many people,
what was missing, why it fell back, and the SQL that answered instead.

A promoted item has `kind: gap` and no spec. Nobody can write the spec yet,
because the metric it needs does not exist. That makes it an open gap: the
evals list it and do not score it, and `fill` leaves it alone. The analyst
builds the metric, adds the spec and the expected metrics, and from then on
the item is scored like any other. When the next deploy passes it, the gap is
closed.

The server never reads its own gaps. This command is the one place Understory
reads its history, and an analyst runs it (design 10.4, roadmap non-goals).
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from understory.harness.golden import Expectation, GoldenItem
from understory.harness.realistic import _slug
from understory.protocols import Warehouse

BACKLOG_RELATION = "mart_semantic_backlog"
"""The mart's name when the package builds into the default schema. Qualify it
(`analytics_understory.mart_semantic_backlog`) wherever the client built it."""

_RELATION = re.compile(r"^[A-Za-z0-9_\-]+(\.[A-Za-z0-9_\-]+){0,2}$")
_COLUMNS = (
    "kind",
    "key",
    "occurrences",
    "users",
    "first_seen",
    "last_seen",
    "sample_question",
    "sample_reason",
    "sample_sql",
    "sample_nearest",
)
_WHY = {
    "invalid": "The spec named something the catalog does not have.",
    "unanswerable": "The traps registry declares this unanswerable.",
    "ungoverned_sql": "No governed metric covered it, so the model wrote SQL instead.",
}


class BacklogRow(BaseModel):
    """One row of `mart_semantic_backlog`: a gap key and what it has cost."""

    kind: str
    key: str
    occurrences: int
    users: int = 0
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    sample_question: str | None = None
    sample_reason: str | None = None
    sample_sql: str | None = None
    sample_nearest: list[str] = Field(default_factory=list)


def backlog_sql(relation: str, tenant: str) -> str:
    """The query over the mart, ranked by people first, then by how often."""
    if not _RELATION.match(relation):
        raise ValueError(f"not a relation name: {relation!r}")
    name = tenant.replace("'", "''")
    return (
        f"select {', '.join(_COLUMNS)} from {relation} "
        f"where tenant = '{name}' "
        "order by users desc, occurrences desc, kind, key"
    )


def read_backlog(
    warehouse: Warehouse, tenant: str, *, relation: str = BACKLOG_RELATION, cap: int = 1000
) -> list[BacklogRow]:
    """The tenant's backlog rows, most people first."""
    result = warehouse.run(backlog_sql(relation, tenant), timeout_s=120, row_cap=cap)
    names = [c.name for c in result.columns]
    return [BacklogRow.model_validate(_row(dict(zip(names, r, strict=True)))) for r in result.rows]


def _row(raw: dict[str, Any]) -> dict[str, Any]:
    raw["users"] = raw.get("users") or 0
    nearest = raw.get("sample_nearest")
    raw["sample_nearest"] = [str(n) for n in nearest] if nearest else []
    return raw


def draft_from_backlog(
    rows: list[BacklogRow], *, min_users: int = 1, min_occurrences: int = 1, limit: int = 0
) -> list[GoldenItem]:
    """One open gap item per row that clears both thresholds, in backlog order."""
    kept = [r for r in rows if r.users >= min_users and r.occurrences >= min_occurrences]
    if limit:
        kept = kept[:limit]
    return [gap_item(r) for r in kept]


def gap_item(row: BacklogRow) -> GoldenItem:
    """The golden item for one backlog row. Open: no spec, expects a governed answer."""
    return GoldenItem(
        id=gap_id(row.kind, row.key),
        question=row.sample_question or f"TODO: the question behind {row.kind} gap {row.key!r}",
        kind="gap",
        source=f"gap:{row.kind}:{row.key}",
        notes=_notes(row),
        expected=Expectation(status="resolved"),
    )


def gap_id(kind: str, key: str) -> str:
    """Stable per gap key, so a second promotion finds the item it already wrote."""
    slug = _slug(key)
    if len(slug) > 48:
        slug = f"{slug[:40].rstrip('_')}_{hashlib.sha1(key.encode()).hexdigest()[:7]}"
    return f"gap_{kind}_{slug}"


def _notes(row: BacklogRow) -> str:
    people = f"{row.users} {'person' if row.users == 1 else 'people'}"
    times = f"{row.occurrences} {'time' if row.occurrences == 1 else 'times'}"
    when = ""
    if row.first_seen and row.last_seen:
        first, last = row.first_seen.date(), row.last_seen.date()
        when = f" on {first}" if first == last else f" between {first} and {last}"
    parts = [f"Open gap. Asked {times} by {people}{when}. {_WHY.get(row.kind, '')}".rstrip()]
    parts.append(f"Missing: {row.key}.")
    if row.sample_nearest:
        parts.append(f"Nearest in the catalog: {', '.join(row.sample_nearest)}.")
    if row.sample_reason:
        parts.append(f"Why it fell back: {_one_line(row.sample_reason)}")
    if row.sample_sql:
        parts.append(f"The SQL that answered instead: {_one_line(row.sample_sql)}")
    parts.append(
        "To close it, build the metric, then add a spec and expected.metrics here. "
        "Until then the evals list this item and do not score it."
    )
    return " ".join(parts)


def _one_line(text: str) -> str:
    """Whitespace collapsed, ending in a full stop so the next sentence starts clean."""
    line = " ".join(text.split())
    return line if line.endswith((".", "?", "!", ";")) else f"{line}."
