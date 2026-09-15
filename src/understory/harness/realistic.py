"""Drafting a tenant's realistic set from the seeded ground truth.

The realistic set guards adoption: questions weighted toward what stakeholders
actually ask, scored on first-turn answer rate with correct disclosures. On the
fake tenants the best source of such questions is the ground truth the data
generator recorded: people ask about a month when something happened in it. So
the draft is drawn from the labeled rate events, padded with questions about
months where nothing happened, and then hand-edited to sound like a person.

Three steps, each its own function so the middle one can be a human:

1. `draft_realistic` reads `meta.ground_truth` and writes template questions
   with correct specs and no numbers. Wording is deliberately flat.
2. Someone rewrites the wording, adds over-refusal items (answerable questions
   that look risky) and drops what is dull. `source` keeps the lineage.
3. `fill_numbers` runs every spec through the service once and records the
   status, metrics and numbers it saw, as a snapshot with `verified: false`.

Data-quality faults from the ground truth are read but not drafted. Duplicates
and loading delays are invisible in a static warehouse (staging dedupes on
`_loaded_at`), and a volume dropout only shows as a smaller number, which the
server cannot yet tell from a quiet week. Fault items wait for that check.
"""

from __future__ import annotations

import calendar
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from understory.harness.golden import GoldenItem, GoldenKind
from understory.protocols import Warehouse
from understory.server.service import Service
from understory.types import Catalog, MetricInfo, Status, ToolResponse

GROUND_TRUTH_RELATION = "meta.ground_truth"


@dataclass(frozen=True)
class GroundTruthEvent:
    """One row of `meta.ground_truth`. `kind` is `rate` or `dq`."""

    id: str
    kind: str
    type: str
    target: str
    start: date
    end: date
    magnitude: float
    affected_metrics: tuple[str, ...] = ()
    segment: dict[str, str] = field(default_factory=dict)

    @property
    def month(self) -> date:
        """First day of the month the event mostly falls in."""
        best, best_days = _month_start(self.start), -1
        cursor = _month_start(self.start)
        while cursor <= self.end:
            nxt = _next_month(cursor)
            days = (min(self.end, nxt - timedelta(days=1)) - max(self.start, cursor)).days + 1
            if days > best_days:
                best, best_days = cursor, days
            cursor = nxt
        return best


def read_ground_truth(warehouse: Warehouse, *, timeout_s: int = 30) -> list[GroundTruthEvent]:
    """Every event the generator recorded, oldest first. Empty when the relation is absent."""
    sql = (
        "SELECT id, kind, type, target, segment, start_date, end_date, magnitude, "
        f"affected_metrics FROM {GROUND_TRUTH_RELATION} ORDER BY start_date, id"
    )
    try:
        result = warehouse.run(sql, timeout_s=timeout_s, row_cap=1000)
    except Exception:
        return []
    out: list[GroundTruthEvent] = []
    for row in result.rows:
        r = dict(zip([c.name for c in result.columns], row, strict=True))
        out.append(
            GroundTruthEvent(
                id=str(r["id"]),
                kind=str(r["kind"]),
                type=str(r["type"]),
                target=str(r["target"]),
                start=_as_date(r["start_date"]),
                end=_as_date(r["end_date"]),
                magnitude=float(r["magnitude"] or 0.0),
                affected_metrics=tuple(_as_list(r["affected_metrics"])),
                segment={str(k): str(v) for k, v in (_as_dict(r["segment"])).items()},
            )
        )
    return out


def data_window(
    catalog: Catalog, warehouse: Warehouse, *, timeout_s: int = 30
) -> tuple[date, date]:
    """The span the metrics' own time dimensions cover, min to max, across relations.

    The time spine runs years past the data, so it is skipped; the answer is the
    first and last day that a metric could actually have a value for.
    """
    lo: date | None = None
    hi: date | None = None
    seen: set[tuple[str, str]] = set()
    for m in catalog.metrics.values():
        if not m.time_dimension:
            continue
        dim = catalog.dimensions.get(m.time_dimension)
        if dim is None or (dim.relation, dim.column) in seen:
            continue
        seen.add((dim.relation, dim.column))
        sql = f"SELECT MIN({dim.column}), MAX({dim.column}) FROM {dim.relation}"
        result = warehouse.run(sql, timeout_s=timeout_s, row_cap=1)
        if not result.rows or result.rows[0][0] is None:
            continue
        a, b = _as_date(result.rows[0][0]), _as_date(result.rows[0][1])
        lo = a if lo is None or a < lo else lo
        hi = b if hi is None or b > hi else hi
    if lo is None or hi is None:
        raise ValueError("no metric has a time dimension with data")
    return lo, hi


# --------------------------------------------------------------------------- #
# Drafting
# --------------------------------------------------------------------------- #


def draft_realistic(
    catalog: Catalog,
    events: Iterable[GroundTruthEvent],
    *,
    window: tuple[date, date],
    quiet_items: int = 6,
) -> list[GoldenItem]:
    """Template questions from the rate events, padded with quiet months.

    Per rate event whose month has a full month of data: one by-month item on
    the first affected metric the catalog knows (the headline, spanning the
    month before through the month after), one single-month item on the last
    affected metric when it differs, and one filtered item when the event names
    a segment. Quiet items round-robin the catalog's metrics over months no rate
    event touches.
    """
    lo, hi = window
    first_full = _month_start(lo) if lo.day == 1 else _next_month(_month_start(lo))
    last_full = (
        _month_start(hi)
        if _next_month(_month_start(hi)) - timedelta(days=1) == hi
        else (_month_start(hi) - timedelta(days=1)).replace(day=1)
    )

    items: list[GoldenItem] = []
    rate_events = [e for e in events if e.kind == "rate"]
    busy: set[date] = set()
    for e in rate_events:
        cursor = _month_start(e.start)
        while cursor <= e.end:
            busy.add(cursor)
            cursor = _next_month(cursor)

    for e in rate_events:
        month = e.month
        if month < first_full or month > last_full:
            continue
        known = [m for m in e.affected_metrics if m in catalog.metrics]
        if not known:
            continue
        head, tail = known[0], known[-1]
        prev, nxt = month - timedelta(days=1), _next_month(month)
        prev = prev.replace(day=1)
        start = prev if prev >= first_full else month
        end_month = nxt if nxt <= last_full else month
        items.append(
            _item(
                id=f"{head}_by_month_{_slug_month(month)}",
                question=(
                    f"How did {_label(head, catalog)} trend by month from "
                    f"{_month_name(start)} through {_month_name(end_month)} {end_month.year}?"
                ),
                kind="event",
                source=f"ground_truth:{e.id}",
                spec={
                    "metrics": [head],
                    "group_by": ["metric_time"],
                    "time": {"grain": "month", "start": start, "end": _month_end(end_month)},
                },
            )
        )
        if tail != head:
            items.append(
                _item(
                    id=f"{tail}_{_slug_month(month)}",
                    question=_single_month(tail, month, catalog),
                    kind="event",
                    source=f"ground_truth:{e.id}",
                    spec={"metrics": [tail], "time": {"start": month, "end": _month_end(month)}},
                )
            )
        for key, value in e.segment.items():
            dim = _segment_dimension(catalog.metrics[head], key, catalog)
            if dim is None:
                continue
            items.append(
                _item(
                    id=f"{head}_{_slug(value)}_{_slug_month(month)}",
                    question=(
                        f"What was {_label(head, catalog)} for {value} {key} "
                        f"in {_month_name(month)} {month.year}?"
                    ),
                    kind="event",
                    source=f"ground_truth:{e.id}",
                    spec={
                        "metrics": [head],
                        "where": [{"dimension": dim, "op": "eq", "values": [value]}],
                        "time": {"start": month, "end": _month_end(month)},
                    },
                )
            )

    quiet_months = [
        m
        for m in _months_between(first_full, last_full)
        if m not in busy
        and _next_month(m) not in busy
        and (m - timedelta(days=1)).replace(day=1) not in busy
    ]
    if quiet_months and quiet_items:
        metrics = [m for m in catalog.metrics if m not in _drafted_metrics(items)] or list(
            catalog.metrics
        )
        step = max(1, len(quiet_months) // quiet_items)
        picked = quiet_months[::step][:quiet_items]
        for i, month in enumerate(picked):
            name = metrics[i % len(metrics)]
            items.append(
                _item(
                    id=f"{name}_{_slug_month(month)}",
                    question=_single_month(name, month, catalog),
                    kind="quiet",
                    source="quiet_month",
                    spec={"metrics": [name], "time": {"start": month, "end": _month_end(month)}},
                )
            )

    return _dedupe(items)


def _single_month(metric: str, month: date, catalog: Catalog) -> str:
    return f"What was {_label(metric, catalog)} in {_month_name(month)} {month.year}?"


def _item(
    *, id: str, question: str, kind: GoldenKind, source: str, spec: dict[str, Any]
) -> GoldenItem:
    return GoldenItem(id=id, question=question, kind=kind, source=source, spec=spec)


def _segment_dimension(metric: MetricInfo, key: str, catalog: Catalog) -> str | None:
    """The metric's dimension whose name ends with `__<key>`, if it has one."""
    suffix = f"__{key}"
    for name in metric.dimensions:
        if name.endswith(suffix) and name in catalog.dimensions:
            return name
    return None


def _drafted_metrics(items: list[GoldenItem]) -> set[str]:
    out: set[str] = set()
    for item in items:
        out.update(item.spec.get("metrics", []) if item.spec else [])
    return out


def _dedupe(items: list[GoldenItem]) -> list[GoldenItem]:
    seen: dict[str, int] = {}
    out: list[GoldenItem] = []
    for item in items:
        n = seen.get(item.id, 0)
        seen[item.id] = n + 1
        if n:
            item = item.model_copy(update={"id": f"{item.id}_{n + 1}"})
        out.append(item)
    return out


# --------------------------------------------------------------------------- #
# Filling numbers: the snapshot
# --------------------------------------------------------------------------- #


@dataclass
class Observation:
    """What one spec did when run, for the person editing the file."""

    id: str
    status: str
    metrics: list[str]
    numbers: list[float]
    disclosures: list[str]
    clarifications: list[str]
    error: str | None = None
    mismatch: bool = False
    """True when the run did not do what `expected.status` says, so nothing was filled."""


def fill_numbers(service: Service, items: list[GoldenItem]) -> list[Observation]:
    """Run each item's spec once and record the numbers it returned, in place.

    The item's `expected.status` is the author's judgment of what a correct run
    does and is never changed here. When the run agrees with it, `metrics` (if
    not already pinned) and `numbers` are filled from the rows, and a
    `needs_clarification` item gets the traps it was asked; when the item
    carries `answers`, the second turn's numbers are recorded. When the run
    disagrees, the item is left alone and the observation says so: that is a
    failure the deterministic eval will report, not a number to snapshot.
    `disclosures` are left alone too: the substrings worth asserting are a
    judgment, written by hand from the observations. `verified` stays false.
    """
    observations: list[Observation] = []
    for item in items:
        obs = _observe(service, item)
        observations.append(obs)
        exp = item.expected
        if obs.error or obs.status != exp.status:
            obs.mismatch = True
            continue
        if obs.clarifications and not exp.clarification_traps:
            exp.clarification_traps = obs.clarifications
        if not exp.metrics:
            exp.metrics = obs.metrics
        exp.numbers = obs.numbers
    return observations


def _observe(service: Service, item: GoldenItem) -> Observation:
    session = service.sessions.get(f"fill:{item.id}")
    try:
        first = service.query_metrics(session, item.metric_spec())
    except Exception as e:
        return Observation(item.id, "error", [], [], [], [], error=f"{type(e).__name__}: {e}")
    asked = [c.trap for c in first.clarifications]
    final: ToolResponse = first
    if asked and item.expected.answers:
        final = service.query_metrics(session, item.metric_spec(with_answers=True))
    status = str(first.status)
    if status not in ("resolved", "needs_clarification", "unanswerable", "invalid"):
        return Observation(item.id, status, [], [], _texts(final), asked, error=_why(final))
    metrics = list(final.provenance.metrics) if final.provenance else []
    return Observation(
        id=item.id,
        status=status,
        metrics=metrics if final.status == Status.resolved else [],
        numbers=_metric_numbers(final, metrics) if final.status == Status.resolved else [],
        disclosures=_texts(final),
        clarifications=asked,
    )


def _metric_numbers(response: ToolResponse, metrics: list[str]) -> list[float]:
    """The values in the metric columns, rounded the way the golden files are.

    Rows are sorted by their non-metric columns (time, then dimensions) first:
    MetricFlow returns them in no stable order, and a refill that permutes
    every numbers list would bury the one number that actually changed.
    """
    if response.result is None:
        return []
    cols = [i for i, c in enumerate(response.result.columns) if c.name in metrics]
    keys = [i for i in range(len(response.result.columns)) if i not in cols]
    rows = sorted(response.result.rows, key=lambda r: [str(r[i]) for i in keys])
    if not cols:
        cols = [
            i
            for i, _ in enumerate(response.result.columns)
            if any(
                isinstance(row[i], (int, float)) and not isinstance(row[i], bool)
                for row in response.result.rows
            )
        ]
    out: list[float] = []
    for row in rows:
        for i in cols:
            v = row[i]
            if v is None or isinstance(v, bool):
                continue
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            out.append(round(f, 4) if abs(f) < 1 else round(f, 2))
    return out


def _texts(response: ToolResponse) -> list[str]:
    out = list(response.required_disclosures)
    if response.refusal is not None:
        out.append(response.refusal.message)
    return out


def _why(response: ToolResponse) -> str:
    if response.refusal is not None:
        return response.refusal.message
    return str(response.status)


# --------------------------------------------------------------------------- #
# Writing the file
# --------------------------------------------------------------------------- #


def dump_items(items: list[GoldenItem], *, header: str = "") -> str:
    """The set as YAML in the house style: one block per item, flow-style specs.

    `yaml.safe_dump` would do, but it scatters keys alphabetically and breaks
    every short mapping onto many lines, and the file is meant to be read.
    """
    lines: list[str] = []
    if header:
        lines.extend(f"# {ln}".rstrip() for ln in header.strip().splitlines())
    lines.append("version: 1")
    lines.append("")
    lines.append("questions:")
    for item in items:
        lines.append(f"  - id: {item.id}")
        lines.append(f"    question: {_q(item.question)}")
        if item.kind:
            lines.append(f"    kind: {item.kind}")
        if item.source:
            lines.append(f"    source: {item.source}")
        if item.verified:
            lines.append("    verified: true")
        if item.notes:
            lines.append("    notes: >")
            lines.extend(f"      {ln}" for ln in _wrap(item.notes.strip(), 78))
        if item.spec is not None:
            lines.append("    spec:")
            spec = dict(item.spec)
            spec.pop("question", None)
            for key, value in spec.items():
                if key == "where" and isinstance(value, list):
                    lines.append("      where:")
                    lines.extend(f"        - {_flow(w)}" for w in value)
                else:
                    lines.append(f"      {key}: {_flow(value)}")
        exp = item.expected
        lines.append("    expected:")
        lines.append(f"      status: {exp.status}")
        if exp.clarification_traps:
            lines.append(f"      clarification_traps: {_flow(exp.clarification_traps)}")
        if exp.answers:
            lines.append(f"      answers: {_flow(exp.answers)}")
        if exp.metrics:
            lines.append(f"      metrics: {_flow(exp.metrics)}")
        if not exp.governed:
            lines.append("      governed: false")
        if exp.disclosures:
            lines.append(f"      disclosures: {_flow(exp.disclosures)}")
        if exp.numbers:
            lines.append(f"      numbers: {_flow(exp.numbers)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_items(path: str | Path, items: list[GoldenItem], *, header: str = "") -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dump_items(items, header=header))
    return p


_FILLED_KEYS = ("clarification_traps", "metrics", "numbers")


def patch_expected(path: str | Path, items: list[GoldenItem]) -> Path:
    """Write the filled `expected` fields back into an existing file, in place.

    The file is hand-edited, with section comments and notes worth keeping, so
    this edits lines rather than re-emitting the document. For each item found
    by id, the `clarification_traps`, `metrics` and `numbers` lines under
    `expected` are replaced or added; every other line is left as it is.
    """
    p = Path(path)
    lines = p.read_text().splitlines()
    by_id = {item.id: item for item in items}
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        if not line.startswith("  - id: "):
            continue
        item = by_id.get(line[len("  - id: ") :].strip())
        # Copy the block up to and including `    expected:`.
        while (
            i < len(lines)
            and not lines[i].startswith("  - id: ")
            and lines[i].strip() != "expected:"
        ):
            out.append(lines[i])
            i += 1
        if i >= len(lines) or lines[i].strip() != "expected:" or item is None:
            continue
        out.append(lines[i])
        i += 1
        kept: list[str] = []
        while i < len(lines) and lines[i].startswith("      "):
            key = lines[i].strip().split(":", 1)[0]
            if key not in _FILLED_KEYS:
                kept.append(lines[i])
            i += 1
        exp = item.expected
        filled: list[str] = []
        if exp.clarification_traps:
            filled.append(f"      clarification_traps: {_flow(exp.clarification_traps)}")
        if exp.metrics:
            filled.append(f"      metrics: {_flow(exp.metrics)}")
        if exp.numbers:
            filled.append(f"      numbers: {_flow(exp.numbers)}")
        # Order: status, clarification_traps, answers, metrics, disclosures, numbers.
        ordered = _order_expected(kept, filled)
        out.extend(ordered)
    p.write_text("\n".join(out).rstrip("\n") + "\n")
    return p


_EXPECTED_ORDER = (
    "status",
    "clarification_traps",
    "answers",
    "metrics",
    "governed",
    "disclosures",
    "numbers",
)


def _order_expected(kept: list[str], filled: list[str]) -> list[str]:
    def rank(line: str) -> int:
        key = line.strip().split(":", 1)[0]
        return _EXPECTED_ORDER.index(key) if key in _EXPECTED_ORDER else len(_EXPECTED_ORDER)

    return sorted(kept + filled, key=rank)


def _flow(value: Any) -> str:
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {_flow(v)}" for k, v in value.items()) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_flow(v) for v in value) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, int):
        return str(value)
    text = str(value)
    if text.startswith(("collision:", "role:", "convention:")) or _plain(text):
        return text
    return json.dumps(text)


def _plain(text: str) -> bool:
    """Safe to write unquoted: identifiers, dates and simple words."""
    if not text or text.strip() != text:
        return False
    if text.lower() in ("true", "false", "null", "yes", "no", "on", "off", "~"):
        return False
    if any(ch in text for ch in ":#{}[],&*!|>'\"%@`"):
        return False
    try:
        float(text)
        return False
    except ValueError:
        pass
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return False
    return True


def _q(text: str) -> str:
    return json.dumps(text)


def _wrap(text: str, width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}" if cur else w
    if cur:
        lines.append(cur)
    return lines


# --------------------------------------------------------------------------- #
# Dates and names
# --------------------------------------------------------------------------- #


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _next_month(d: date) -> date:
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1)


def _month_end(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def _months_between(a: date, b: date) -> list[date]:
    out: list[date] = []
    cursor = _month_start(a)
    while cursor <= b:
        out.append(cursor)
        cursor = _next_month(cursor)
    return out


def _month_name(d: date) -> str:
    return calendar.month_name[d.month]


def _slug_month(d: date) -> str:
    return d.strftime("%Y_%m")


def _slug(text: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in text.lower()).strip("_")


def _label(name: str, catalog: Catalog) -> str:
    m = catalog.metrics.get(name)
    if m is not None and m.label:
        return m.label.lower()
    return name.replace("_", " ")


def _as_date(v: Any) -> date:
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def _as_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    try:
        parsed = json.loads(v)
    except (TypeError, ValueError):
        return [str(v)]
    return [str(x) for x in parsed] if isinstance(parsed, list) else []


def _as_dict(v: Any) -> dict[str, Any]:
    if v is None:
        return {}
    if isinstance(v, dict):
        return v
    try:
        parsed = json.loads(v)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


__all__ = [
    "GroundTruthEvent",
    "Observation",
    "data_window",
    "draft_realistic",
    "dump_items",
    "fill_numbers",
    "patch_expected",
    "read_ground_truth",
    "write_items",
]
